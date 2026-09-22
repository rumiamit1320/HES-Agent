from __future__ import annotations

import logging
from pathlib import Path
import sys
import shutil
import re
import time
import signal
import pandas as pd
import yaml

from .excel_input import read_input
from .processor import (
    read_download,
    read_generic_download,
    build_events,
    build_final_meter_summary,
    deduplicate_frame,
    deduplicate_download_items,
    deduplicate_metadata,
    write_report,
)
from .hes_portal import HESPortal
from .batch_manager import (
    choose_folder,
    choose_excel_files,
    choose_excel_file,
    choose_feeder_mode,
    feeder_name_from_dataframe,
    create_feeder_workspace,
    create_batch,
    move_download_to_dataset_folder,
    safe_name,
    load_manifest,
    update_batch_checkpoint,
    checkpoint_completed,
    find_resumable_batch,
)
from .management_report import write_management_report

ROOT = Path(__file__).resolve().parents[1]
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
LOG = logging.getLogger("hes-agent")

DATA_OPTIONS = {
    "1": "Power Outage",
    "2": "Meter Information",
    "3": "File Details",
    "4": "Instant Profile",
    "5": "Daily Energy",
    "6": "Bill Profile",
    "8": "Load Data",
    "9": "Event/Tamper Data",
    "10": "Communication Settings",
    "11": "ESW Notification",
}


_ACTIVE_PORTAL = None
_SIGINT_REQUESTED = False

def _handle_sigint(signum, frame):
    """Convert Ctrl+C into a cooperative stop so Playwright can unwind cleanly."""
    global _SIGINT_REQUESTED
    if _SIGINT_REQUESTED:
        # A second Ctrl+C restores the normal immediate interrupt behavior.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        raise KeyboardInterrupt
    _SIGINT_REQUESTED = True
    print("\nCtrl+C received. Stopping the current HES operation safely...")
    portal = _ACTIVE_PORTAL
    if portal is not None:
        try:
            portal.request_stop()
        except Exception:
            pass

def _install_sigint_handler():
    try:
        signal.signal(signal.SIGINT, _handle_sigint)
    except Exception:
        pass

def _restore_sigint_handler():
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except Exception:
        pass


def choose_datasets() -> list[str]:
    print("\n============================================================")
    print("SELECT HES DATA TO DOWNLOAD")
    print("============================================================")
    for key, value in DATA_OPTIONS.items():
        print(f"  {key}. {value}")
    print("  7. ALL DATA")
    print(" 12. Feeder Reports (Power Outage + Bill Profile + Instant Profile)")
    print("\nEnter one or more numbers separated by commas.")
    print("Example: 1,2,4")

    while True:
        raw = input("Data selection: ").strip().lower()
        if raw in {"7", "all", "a"}:
            return list(DATA_OPTIONS.values())
        if raw in {"12", "reports", "report"}:
            return ["Power Outage", "Bill Profile", "Instant Profile"]
        parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        if parts and all(p in DATA_OPTIONS for p in parts):
            selected = []
            for p in parts:
                if DATA_OPTIONS[p] not in selected:
                    selected.append(DATA_OPTIONS[p])
            return selected
        print("Invalid selection. Choose 1-6 or 8-11, 12 for Feeder Reports, or 7 for ALL DATA.")


def _download_with_recovery(portal: HESPortal, meter: object, dataset: str, max_attempts: int = 3):
    """Download one meter/dataset, recovering transient network/session failures."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            if portal.session_expired():
                if not portal.recover_session():
                    raise RuntimeError("HES session expired and could not be restored.")
            return portal.download_meter_dataset(meter, dataset)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            last_exc = exc
            # Browser/context closure is a recoverable transaction failure even
            # when session_expired()/connection_offline() cannot inspect the
            # dead Playwright page. This is the key distinction for HES Blob/XLS
            # exports: Download.save_as() can close the context after the HES
            # export has been initiated.
            browser_closed = False
            try:
                browser_closed = portal._browser_context_is_closed()
            except Exception:
                browser_closed = portal.is_network_error(exc)
            session_problem = portal.session_expired()
            network_problem = portal.is_network_error(exc) or portal.connection_offline()
            recoverable = browser_closed or session_problem or network_problem
            if not recoverable or attempt >= max_attempts:
                raise
            reason = 'browser/context' if browser_closed else ('session' if session_problem else 'network')
            print(f"    Transient HES {reason} failure detected.")
            print(f"    Current meter/dataset will be retried; attempt {attempt + 1}/{max_attempts}.")
            if not portal.recover_session(max_attempts=3):
                if attempt + 1 >= max_attempts:
                    raise RuntimeError(
                        f"HES_RECOVERY_FAILED: HES recovery failed after {attempt} attempts: {exc}"
                    ) from exc
            time.sleep(min(5 * attempt, 15))
    if last_exc:
        raise last_exc
    return None


def _preload_batch_downloads(batch: Path, selected_datasets: list[str], df: pd.DataFrame,
                             dataset_frames: dict[str, list[pd.DataFrame]],
                             downloaded_items: list[dict], log_rows: list[dict],
                             raw_outage_frames: list[pd.DataFrame]) -> None:
    """Load verified files already present in a resumable batch into the report pipeline."""
    manifest = load_manifest(batch)
    checkpoints = manifest.get("checkpoints", {})
    if not checkpoints:
        return
    for entry in checkpoints.values():
        if str(entry.get("status", "")).lower() != "downloaded":
            continue
        dataset = str(entry.get("dataset", "")).strip()
        logical_dataset = dataset
        if dataset in {"Bill Profile - Running Bill", "Bill Profile - History Bill",
                       "Instant Profile - Instant Partial", "Instant Profile - Instant Full"}:
            logical_dataset = "Bill Profile" if dataset.startswith("Bill Profile") else "Instant Profile"
        if logical_dataset not in selected_datasets:
            continue
        raw_file = str(entry.get("file", "")).strip()
        if not raw_file:
            continue
        f = Path(raw_file)
        if not f.is_absolute():
            f = batch / f
        if not f.is_file() or f.stat().st_size <= 0:
            continue
        meter = str(entry.get("meter", "")).strip()
        try:
            frame = read_download(f, meter, dataset) if logical_dataset == "Power Outage" else read_generic_download(f, meter, logical_dataset)
            if dataset in {"Bill Profile - Running Bill", "Bill Profile - History Bill"}:
                frame.insert(2, "Bill Type", dataset.split("-", 1)[1].strip())
            elif dataset in {"Instant Profile - Instant Partial", "Instant Profile - Instant Full"}:
                frame.insert(2, "Instant Profile Type", dataset.split("-", 1)[1].strip())
            dataset_frames.setdefault(logical_dataset, []).append(frame)
            downloaded_items.append({"meter": meter, "dataset": logical_dataset, "frame": frame})
            if logical_dataset == "Power Outage":
                raw_outage_frames.append(frame)
            log_rows.append({
                "Meter No": meter, "Dataset": logical_dataset, "Status": "Resumed/Existing",
                "File": str(f), "Records": len(frame), "Error": "",
            })
            print(f"    Checkpoint restored: {meter} / {dataset} -> {f.name}")
        except Exception as exc:
            LOG.warning("Could not restore checkpoint file %s: %s", f, exc)


def _build_analysis_frames(df: pd.DataFrame, meter_col: str, selected_datasets: list[str],
                           batch: Path, portal: HESPortal) -> dict:
    """Run one feeder batch while preserving the existing HES processing architecture.

    Downloads are logically separated by dataset. Ctrl+C is treated as a graceful
    stop: the current browser session is allowed to close, completed files remain
    on disk, and a partial report is generated from everything acquired so far.
    """
    log_rows: list[dict] = []
    raw_outage_frames: list[pd.DataFrame] = []
    downloaded_items: list[dict] = []
    dataset_frames: dict[str, list[pd.DataFrame]] = {d: [] for d in selected_datasets}
    interrupted = False

    # A resumed batch must contribute its already completed files to the final
    # report; otherwise the report would contain only data acquired after restart.
    _preload_batch_downloads(batch, selected_datasets, df, dataset_frames,
                             downloaded_items, log_rows, raw_outage_frames)

    downloads_root = batch / "Downloads"
    downloads_root.mkdir(parents=True, exist_ok=True)
    portal.download_dir = downloads_root
    # Chrome itself always uses one stable staging/profile location. Only the
    # final agent-managed download_dir changes per dataset. This prevents a
    # browser recovery from creating ChromeProfile inside a dataset folder.
    portal.session_download_dir = portal.session_download_dir or downloads_root
    portal.navigate_to_view_meter_data()

    meters = df["_meter_no"].tolist()
    for i, meter in enumerate(meters, 1):
        portal.raise_if_stop_requested()
        if interrupted:
            break
        print(f"\n[{i}/{len(meters)}] Meter {meter}")
        for dataset in selected_datasets:
            portal.raise_if_stop_requested()
            if interrupted:
                break

            # Bill Profile is a parent tab with two actual HES views. Acquire
            # both so the billing report contains current Running Bill data and
            # History Bill data, while retaining one logical report dataset.
            operations = [(dataset, dataset)]
            if dataset == "Instant Profile":
                # HES exposes two actual child views.  Treat each as an
                # independent XLS acquisition, following the same sequence as
                # Power Outage: open meter -> Instant Profile -> child view -> XLS.
                operations = [
                    ("Instant Profile - Instant Partial", "Instant Partial"),
                    ("Instant Profile - Instant Full", "Instant Full"),
                ]
            elif dataset == "Bill Profile":
                # Bill Profile likewise contains two actual child views.
                operations = [
                    ("Bill Profile - Running Bill", "Running Bill"),
                    ("Bill Profile - History Bill", "History Bill"),
                ]

            for operation_dataset, display_dataset in operations:
                portal.raise_if_stop_requested()
                if interrupted:
                    break
                dataset_dir = downloads_root / safe_name(dataset, "Dataset")
                if dataset in {"Bill Profile", "Instant Profile"}:
                    dataset_dir = dataset_dir / safe_name(display_dataset, "View")
                dataset_dir.mkdir(parents=True, exist_ok=True)
                portal.download_dir = dataset_dir
                try:
                    completed_file = checkpoint_completed(batch, meter, operation_dataset)
                    if completed_file is not None:
                        print(f"    Checkpoint: skipping completed {display_dataset} for meter {meter}.")
                        continue

                    f = _download_with_recovery(portal, meter, operation_dataset)
                    if f:
                        # Bill/Instant child-view downloads belong directly
                        # in their child-view folder. Other datasets retain the
                        # existing dataset-folder behavior.
                        if dataset in {"Bill Profile", "Instant Profile"}:
                            if Path(f).resolve().parent != dataset_dir.resolve():
                                target = dataset_dir / Path(f).name
                                target.parent.mkdir(parents=True, exist_ok=True)
                                try:
                                    import shutil
                                    shutil.move(str(f), str(target))
                                    f = target
                                except Exception:
                                    f = move_download_to_dataset_folder(f, batch, dataset)
                        else:
                            f = move_download_to_dataset_folder(f, batch, dataset)
                        logical_dataset = dataset
                        if logical_dataset == "Power Outage":
                            d = read_download(f, meter, logical_dataset)
                        else:
                            d = read_generic_download(f, meter, logical_dataset)
                        if dataset == "Bill Profile":
                            d.insert(2, "Bill Type", display_dataset)
                        elif dataset == "Instant Profile":
                            d.insert(2, "Instant Profile Type", display_dataset)
                        downloaded_items.append({"meter": str(meter), "dataset": logical_dataset, "frame": d})
                        dataset_frames.setdefault(logical_dataset, []).append(d)
                        if logical_dataset == "Power Outage":
                            raw_outage_frames.append(d)
                        log_rows.append({
                            "Meter No": meter,
                            "Dataset": logical_dataset,
                            "Bill Type": display_dataset if dataset == "Bill Profile" else "",
                            "Status": "Downloaded",
                            "File": str(f),
                            "Records": len(d),
                            "Error": "",
                        })
                        update_batch_checkpoint(
                            batch, meter=meter, dataset=operation_dataset, status="Downloaded",
                            file=str(f),
                        )
                    else:
                        log_rows.append({
                            "Meter No": meter,
                            "Dataset": dataset,
                            "Bill Type": display_dataset if dataset == "Bill Profile" else "",
                            "Status": "No record",
                            "File": "",
                            "Records": 0,
                            "Error": f"No HES {display_dataset} record found for this meter",
                        })
                except KeyboardInterrupt:
                    interrupted = True
                    print("\nCtrl+C received. Stopping the HES download loop after the current operation.")
                    print("Completed downloads are preserved; no new meters will be started.")
                except Exception as exc:
                    LOG.exception("Meter %s / dataset %s failed", meter, operation_dataset)
                    log_rows.append({
                        "Meter No": meter,
                        "Dataset": dataset,
                        "Bill Type": display_dataset if dataset == "Bill Profile" else "",
                        "Status": "Failed",
                        "File": "",
                        "Records": 0,
                        "Error": str(exc),
                    })
                    update_batch_checkpoint(
                        batch, meter=meter, dataset=operation_dataset, status="Failed",
                        error=str(exc),
                    )
                    if ("HES_RECOVERY_FAILED" in str(exc) or
                            portal.session_expired() or portal.is_network_error(exc) or portal.connection_offline()):
                        update_batch_checkpoint(batch, status="failed")
                        raise
                finally:
                    # A Ctrl+C is a terminal stop for the current batch operation.
                    # Do NOT run cleanup navigation here: if the page/context is
                    # already in a transient state, return_to_analytics() can
                    # invoke recovery, restart Chrome and force another CAPTCHA.
                    # The persistent browser must remain untouched on Ctrl+C.
                    if not interrupted and not _SIGINT_REQUESTED and not portal.stop_requested:
                        try:
                            # Running Bill is a child Angular view whose export can
                            # leave the sidebar route in a stale state. Return
                            # directly to the known View Meter Data dashboard so
                            # the next Bill child (History Bill) starts with a
                            # fresh meter search. All other datasets keep the
                            # existing return-navigation path unchanged.
                            if operation_dataset == "Bill Profile - Running Bill":
                                portal.return_to_view_meter_data_direct()
                            else:
                                portal.return_to_analytics_view_meter_data()
                        except KeyboardInterrupt:
                            interrupted = True
                            print("\nCtrl+C received while returning to View Meter Data. Stopping gracefully.")
                        except Exception as nav_exc:
                            LOG.exception("Could not return to Analytics after meter %s / dataset %s", meter, operation_dataset)
                            log_rows.append({
                                "Meter No": meter,
                                "Dataset": dataset,
                                "Bill Type": display_dataset if dataset == "Bill Profile" else "",
                                "Status": "Navigation Failed",
                                "File": "",
                                "Records": 0,
                                "Error": f"Could not return to Analytics/View Meter Data: {nav_exc}",
                            })
                            raise

    raw = pd.concat(raw_outage_frames, ignore_index=True) if raw_outage_frames else pd.DataFrame(
        columns=["Meter No", "Dataset", "Outage Start", "Outage End", "Duration (min)"]
    )
    raw = deduplicate_frame(raw, "Power Outage")
    joined, meter_rows, events, feeder_summary, sub_summary = build_events(
        raw, df, meter_col, 5.0
    )
    if not meter_rows.empty:
        meter_summary = meter_rows.groupby(["_substation", "_feeder", "Meter No"], dropna=False).agg(
            Outage_Count=("Meter No", "size"),
            First_Outage=("Outage Start", "min"),
            Last_Outage=("Outage Start", "max"),
            Total_Outage_Minutes=("Duration (min)", "sum"),
            Max_Outage_Minutes=("Duration (min)", "max"),
        ).reset_index().rename(columns={"_substation": "Substation", "_feeder": "Feeder"})
    else:
        meter_summary = pd.DataFrame()

    log_df = pd.DataFrame(log_rows)
    downloaded_items = deduplicate_download_items(downloaded_items)
    combined_dataset_frames = {
        dataset: deduplicate_frame(pd.concat(frames, ignore_index=True), dataset) if frames else pd.DataFrame()
        for dataset, frames in dataset_frames.items()
    }
    final_summary = build_final_meter_summary(deduplicate_metadata(df), downloaded_items)

    report_dir = batch / "Reports"
    paths = write_report(
        report_dir, df, joined, meter_summary, events, feeder_summary, sub_summary,
        log_df, final_summary=final_summary, dataset_frames=combined_dataset_frames,
    )
    management_paths = write_management_report(
        report_dir, df, joined, meter_summary, events, feeder_summary, sub_summary,
        log_df, selected_datasets, batch, dataset_frames=combined_dataset_frames,
    )
    paths.update(management_paths)
    if interrupted:
        update_batch_checkpoint(batch, status="interrupted")
        print("\nPartial batch report generated from completed downloads.")
        print(f"  Batch: {batch}")
        print(f"  Downloads: {downloads_root}")
        print("  You can analyze this folder later using the folder picker.")
    else:
        update_batch_checkpoint(batch, status="complete")
    return {
        "raw": raw,
        "joined": joined,
        "meter_summary": meter_summary,
        "events": events,
        "feeder_summary": feeder_summary,
        "sub_summary": sub_summary,
        "log_df": log_df,
        "final_summary": final_summary,
        "dataset_frames": combined_dataset_frames,
        "paths": paths,
        "downloaded_items": downloaded_items,
        "interrupted": interrupted,
        "batch": batch,
    }

def _run_feeder_batch(source_excel: Path, feeder_workspace: Path, selected_datasets: list[str], portal: HESPortal) -> dict:
    print("\n============================================================")
    print(f"FEEDER: {feeder_workspace.name}")
    print("============================================================")
    print(f"Input workbook: {source_excel}")

    df, meter_col = read_input(source_excel)
    print(f"Detected meter column: {meter_col}")
    print(f"Meter rows: {len(df)}")
    print(f"Rows with feeder name: {(df['_feeder'].astype(str).str.strip() != '').sum()}")

    resumable = find_resumable_batch(feeder_workspace, source_excel, selected_datasets)
    batch = None
    if resumable is not None:
        manifest = load_manifest(resumable)
        print(f"\nFound resumable HES batch: {resumable}")
        print(f"  Previous status: {manifest.get('status', 'unknown')}")
        answer = input("Resume this batch instead of creating a new one? [Y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            batch = resumable
            print("Resuming existing batch checkpoint.")
            update_batch_checkpoint(batch, status="running")
    if batch is None:
        batch = create_batch(feeder_workspace, source_excel, selected_datasets)
    print(f"Batch folder: {batch}")
    print(f"Batch downloads will be stored under: {batch / 'Downloads'}")

    result = _build_analysis_frames(df, meter_col, selected_datasets, batch, portal)
    print("\nBatch complete.")
    for key in (
        "Feeder_Power_Outage_Monthly_Report",
        "Feeder_Billing_Profile_All_Meters_Report",
        "Feeder_Recent_Instant_Profile_Report",
        "HES_Final_Report",
        "Management_HR_Detailed_Report",
        "Management_HR_Summary_Report",
    ):
        if key in result["paths"]:
            print(f"  {key}: {result['paths'][key]}")
    return result


def _merge_for_workspace(results: list[dict], metadata_list: list[pd.DataFrame], selected_datasets: list[str], workspace: Path) -> None:
    """Create a consolidated report from multiple batches in one feeder folder."""
    if not results:
        print("No completed batches were found to consolidate.")
        return
    metadata = pd.concat(metadata_list, ignore_index=True) if metadata_list else pd.DataFrame()
    metadata = deduplicate_metadata(metadata)
    raw = pd.concat([r["raw"] for r in results if not r["raw"].empty], ignore_index=True) if any(not r["raw"].empty for r in results) else pd.DataFrame(columns=["Meter No", "Outage Start", "Outage End", "Duration (min)"])
    raw = deduplicate_frame(raw, "Power Outage")
    if not metadata.empty:
        meter_col = str(metadata.get("_meter_col", pd.Series(["Meter No"])).iloc[0])
    else:
        meter_col = "Meter No"
    joined, meter_rows, events, feeder_summary, sub_summary = build_events(raw, metadata, meter_col, 5.0)
    if not meter_rows.empty:
        meter_summary = meter_rows.groupby(["_substation", "_feeder", "Meter No"], dropna=False).agg(
            Outage_Count=("Meter No", "size"),
            First_Outage=("Outage Start", "min"),
            Last_Outage=("Outage Start", "max"),
            Total_Outage_Minutes=("Duration (min)", "sum"),
            Max_Outage_Minutes=("Duration (min)", "max"),
        ).reset_index().rename(columns={"_substation": "Substation", "_feeder": "Feeder"})
    else:
        meter_summary = pd.DataFrame()
    logs = [r["log_df"] for r in results if not r["log_df"].empty]
    log_df = pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()
    items = []
    frames_by_dataset: dict[str, list[pd.DataFrame]] = {d: [] for d in selected_datasets}
    for r in results:
        items.extend(r["downloaded_items"])
        for d, frame in r["dataset_frames"].items():
            if not frame.empty:
                frames_by_dataset.setdefault(d, []).append(frame)
    items = deduplicate_download_items(items)
    final_summary = build_final_meter_summary(metadata, items)
    combined = {
        d: deduplicate_frame(pd.concat(v, ignore_index=True), d) if v else pd.DataFrame()
        for d, v in frames_by_dataset.items()
    }

    report_dir = workspace / "Reports"
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    report_dir.mkdir(parents=True, exist_ok=True)
    batch_like_dir = workspace / f"Consolidated_{stamp}"
    batch_like_dir.mkdir(parents=True, exist_ok=True)
    paths = write_report(report_dir, metadata, joined, meter_summary, events, feeder_summary, sub_summary,
                         log_df, final_summary=final_summary, dataset_frames=combined)
    management = write_management_report(report_dir, metadata, joined, meter_summary, events, feeder_summary,
                                         sub_summary, log_df, selected_datasets, batch_like_dir, dataset_frames=combined)
    paths.update(management)
    print("\nConsolidated feeder report generated:")
    for key, value in paths.items():
        if key in {"HES_Final_Report", "Management_HR_Detailed_Report", "Management_HR_Summary_Report"}:
            print(f"  {key}: {value}")


def _dataset_from_download_path(path: Path) -> str:
    known = {
        "power outage": "Power Outage",
        "meter information": "Meter Information",
        "file details": "File Details",
        "instant profile": "Instant Profile",
        "daily energy": "Daily Energy",
        "bill profile": "Bill Profile",
        "load data": "Load Data",
        "event/tamper data": "Event/Tamper Data",
        "communication settings": "Communication Settings",
        "esw notification": "ESW Notification",
    }
    parent = path.parent.name.strip().lower()
    if parent in known:
        return known[parent]
    # Bill Profile now contains Running Bill / History Bill subfolders.
    normalized_path = str(path.parent).lower().replace("\\", "/")
    if "/bill profile/" in normalized_path or "bill_profile" in path.stem.lower():
        return "Bill Profile"
    stem = path.stem.lower()
    for key, value in known.items():
        token = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
        if token and token in re.sub(r"[^a-z0-9]+", "_", stem):
            return value
    return "Unknown"


def _meter_from_download_path(path: Path) -> str:
    m = re.match(r"^(\d+)(?:_|$)", path.stem.strip())
    if m:
        return m.group(1)
    return path.stem.split("_", 1)[0].strip()


def _collect_existing_download_files(batch: Path) -> list[tuple[str, Path]]:
    """Collect every final Excel export under a batch without double-counting legacy raw files."""
    downloads = batch / "Downloads"
    if not downloads.is_dir():
        return []

    files = [p for p in downloads.rglob("*.xls*") if p.is_file() and not p.name.startswith("~$")]
    # Ignore raw sidecars from older builds; the current build no longer creates them.
    files = [p for p in files if not p.name.lower().endswith(".raw")]

    # If a named dataset folder exists, prefer files inside it over legacy files in
    # the Downloads root. Within a meter/dataset pair prefer XLSX because it is the
    # normalized HES export rather than the original HTML-as-XLS payload.
    grouped: dict[tuple[str, str], list[Path]] = {}
    for path in files:
        dataset = _dataset_from_download_path(path)
        meter = _meter_from_download_path(path)
        if not meter:
            continue
        grouped.setdefault((meter, dataset), []).append(path)

    selected: list[tuple[str, Path]] = []
    for (meter, dataset), candidates in sorted(grouped.items()):
        dataset_dirs = [p for p in candidates if p.parent.name.lower() == dataset.lower()]
        pool = dataset_dirs or candidates
        pool.sort(key=lambda p: (p.suffix.lower() != ".xlsx", -p.stat().st_mtime, p.name))
        selected.append((dataset, pool[0]))
    return selected


def _analyze_existing_feeder(folder: Path) -> int:
    """Analyze every completed HES Excel export under a selected feeder folder."""
    print(f"\nAnalyzing existing feeder folder: {folder}")
    input_files = list((folder / "batches").rglob("Input/*.xls*")) + list(folder.glob("Input/*.xls*"))
    input_files = list(dict.fromkeys(p for p in input_files if p.is_file() and not p.name.startswith("~$")))
    if not input_files:
        print("No input Excel workbook was found in this feeder folder.")
        return 1

    results = []
    metadata_list = []
    selected_datasets: list[str] = []
    for input_file in input_files:
        try:
            df, meter_col = read_input(input_file)
        except Exception as exc:
            print(f"Skipping {input_file}: {exc}")
            continue
        metadata_list.append(df)
        batch = next((p for p in input_file.parents if p.name.startswith("Batch_")), None)
        if batch is None:
            continue

        raw_frames = []
        items = []
        frames_by_dataset: dict[str, list[pd.DataFrame]] = {}
        log_items = []
        for dataset, f in _collect_existing_download_files(batch):
            try:
                meter = _meter_from_download_path(f)
                if dataset == "Power Outage":
                    frame = read_download(f, meter, dataset)
                    raw_frames.append(frame)
                elif dataset != "Unknown":
                    frame = read_generic_download(f, meter, dataset)
                else:
                    # Unknown exports are still preserved in the combined analysis.
                    frame = read_generic_download(f, meter, dataset)
                items.append({"meter": meter, "dataset": dataset, "frame": frame})
                frames_by_dataset.setdefault(dataset, []).append(frame)
                if dataset not in selected_datasets:
                    selected_datasets.append(dataset)
                log_items.append({
                    "Meter No": meter, "Dataset": dataset, "Status": "Downloaded",
                    "File": str(f), "Records": len(frame), "Error": ""
                })
                print(f"  Read {dataset}: {f.name} ({len(frame)} records)")
            except Exception as exc:
                LOG.warning("Could not read existing HES file %s: %s", f, exc)
                log_items.append({
                    "Meter No": _meter_from_download_path(f), "Dataset": dataset,
                    "Status": "Failed", "File": str(f), "Records": 0, "Error": str(exc)
                })

        raw = pd.concat(raw_frames, ignore_index=True) if raw_frames else pd.DataFrame(
            columns=["Meter No", "Dataset", "Outage Start", "Outage End", "Duration (min)"]
        )
        raw = deduplicate_frame(raw, "Power Outage")
        joined, meter_rows, events, feeder_summary, sub_summary = build_events(raw, df, meter_col, 5.0)
        if not meter_rows.empty:
            meter_summary = meter_rows.groupby(["_substation", "_feeder", "Meter No"], dropna=False).agg(
                Outage_Count=("Meter No", "size"),
                First_Outage=("Outage Start", "min"),
                Last_Outage=("Outage Start", "max"),
                Total_Outage_Minutes=("Duration (min)", "sum"),
                Max_Outage_Minutes=("Duration (min)", "max"),
            ).reset_index().rename(columns={"_substation": "Substation", "_feeder": "Feeder"})
        else:
            meter_summary = pd.DataFrame()
        combined = {
            d: deduplicate_frame(pd.concat(v, ignore_index=True), d)
            for d, v in frames_by_dataset.items() if v
        }
        results.append({
            "raw": raw, "meter_summary": meter_summary, "events": events,
            "feeder_summary": feeder_summary, "sub_summary": sub_summary,
            "log_df": pd.DataFrame(log_items), "downloaded_items": items,
            "dataset_frames": combined,
        })

    if not results:
        print("No analyzable batches were found.")
        return 1
    _merge_for_workspace(results, metadata_list, selected_datasets or ["Power Outage"], folder)
    return 0


def _offer_folder_analysis(default_folder: Path | None = None) -> None:
    """Offer the user the requested post-run folder analysis workflow."""
    print("\n============================================================")
    print("ANALYZE DOWNLOADED HES DATA")
    print("============================================================")
    print("Completed and interrupted batches can be analyzed without opening HES.")
    raw = input("Analyze a downloaded folder now? [Y/N]: ").strip().lower()
    if raw not in {"y", "yes"}:
        return
    folder = choose_folder("Select HES feeder folder or completed workspace")
    if not folder:
        print("No folder selected.")
        return
    try:
        _analyze_existing_feeder(folder.resolve())
    except Exception as exc:
        LOG.exception("Folder analysis failed")
        print(f"Folder analysis failed: {exc}")

def _workspace_for_existing_excel(source_excel: Path, feeder_name: str | None = None) -> Path:
    """Resolve the feeder workspace without asking the user for a folder.

    If the workbook already belongs to an HES workspace (a directory whose
    child is ``batches``), preserve that workspace. Otherwise create/use a
    feeder-named workspace beside the selected workbook.
    """
    p = source_excel.resolve().parent
    for candidate in [p, *p.parents]:
        if (candidate / "batches").is_dir():
            return candidate
        if candidate.name.lower() == "input" and (candidate.parent / "batches").is_dir():
            return candidate.parent

    name = safe_name(feeder_name or source_excel.stem, "Feeder")
    return p / name


def main():
    print("\n============================================================")
    print("HES POWER OUTAGE AGENT")
    print("Feeder workspace + dated batch storage enabled")
    print("============================================================")
    cfg_path = ROOT / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    # Keep ONE HES Chrome session alive for the entire application lifetime.
    # Completing a batch or pressing Ctrl+C returns to this menu without closing
    # Chrome, so the authenticated HES session/CAPTCHA state can be reused.
    portal = HESPortal(cfg, ROOT / "data" / "downloads")
    portal_started = False
    global _ACTIVE_PORTAL, _SIGINT_REQUESTED
    _ACTIVE_PORTAL = portal
    _SIGINT_REQUESTED = False
    _install_sigint_handler()

    # Keep the application alive when a picker is cancelled or an invalid
    # existing workbook is selected. The user can return to the menu instead
    # of the program terminating.
    while True:
        mode = choose_feeder_mode()
        if mode == "4":
            print("Exiting HES Power Outage Agent.")
            try:
                portal.close()
            finally:
                _ACTIVE_PORTAL = None
                _restore_sigint_handler()
            return 0

        source_excel = None
        feeder_workspace = None
        files = []

        if mode == "1":
            print("\nSelect the EXISTING FEEDER EXCEL FILE.")
            print("This is a FILE picker, not a folder picker.")
            source_excel = choose_excel_file(
                "Select EXISTING feeder Excel file (.xls / .xlsx)"
            )

            if not source_excel:
                print("\nNo Excel file selected. Returning to the feeder menu.")
                continue

            source_excel = source_excel.resolve()
            if not source_excel.is_file() or source_excel.suffix.lower() not in {".xls", ".xlsx", ".xlsm"}:
                print(f"\nInvalid feeder Excel file: {source_excel}")
                print("Please select an .xls, .xlsx or .xlsm feeder workbook.")
                continue

            # Validate the workbook before starting the browser. If it is not
            # a valid feeder workbook, return to the menu instead of terminating.
            try:
                df_preview, _ = read_input(source_excel)
                if "_meter_no" not in df_preview.columns:
                    print("\nThe selected Excel does not contain a usable Meter No column.")
                    print("Please choose the correct feeder Excel file.")
                    continue
            except Exception as exc:
                print(f"\nCould not read the selected feeder Excel: {exc}")
                print("Please choose another feeder Excel file.")
                continue

            # The workbook's feeder-name column can begin with a power
            # transformer row. For an existing feeder workbook, the workbook
            # filename is the stable workspace identity. Do not use the first
            # dataframe feeder-name value as the workspace name.
            feeder_name = source_excel.stem
            feeder_workspace = _workspace_for_existing_excel(source_excel, feeder_name)
            feeder_workspace.mkdir(parents=True, exist_ok=True)
            (feeder_workspace / "batches").mkdir(parents=True, exist_ok=True)
            (feeder_workspace / "Reports").mkdir(parents=True, exist_ok=True)
            print(f"Existing feeder Excel: {source_excel}")
            print(f"Feeder name: {safe_name(feeder_name, source_excel.stem)}")
            print(f"Feeder workspace: {feeder_workspace}")

        elif mode == "2":
            print("\nSelect NEW FEEDER Excel file(s).")
            print("This is a FILE picker; multiple .xls/.xlsx files may be selected.")
            files = choose_excel_files(
                "Select NEW feeder Excel file(s)",
                multiple=True,
            )
            if not files:
                print("\nNo Excel file selected. Returning to the feeder menu.")
                continue

            # No second folder picker. Each selected workbook determines its
            # own feeder workspace automatically from its feeder name.
            valid_files = []
            for source in files:
                source = source.resolve()
                if not source.is_file() or source.suffix.lower() not in {".xls", ".xlsx", ".xlsm"}:
                    print(f"Skipping invalid file: {source}")
                    continue
                try:
                    df_preview, _ = read_input(source)
                    if "_meter_no" not in df_preview.columns:
                        print(f"Skipping {source.name}: no usable Meter No column.")
                        continue
                    valid_files.append(source)
                except Exception as exc:
                    print(f"Skipping {source.name}: {exc}")

            if not valid_files:
                print("\nNo valid feeder Excel files were selected.")
                print("Returning to the feeder menu; the program remains running.")
                continue
            files = valid_files

        elif mode == "3":
            folder = choose_folder("Select existing HES feeder folder to analyze")
            if not folder:
                print("\nNo folder selected. Returning to the feeder menu.")
                continue
            folder = folder.resolve()
            if not folder.is_dir():
                print("\nSelected path is not a folder. Returning to the feeder menu.")
                continue
            _analyze_existing_feeder(folder)
            print("\nReturning to the initial HES feeder menu. Chrome remains open.")
            continue

        selected_datasets = choose_datasets()
        print("\nSelected datasets:")
        for d in selected_datasets:
            print(f"  - {d}")

        # Prepare feeder jobs.
        if mode == "2":
            feeder_jobs = []
            for source in files:
                try:
                    df_preview, _ = read_input(source)
                    # The workbook column "Name of Feeder (Power transformer/ Line)"
                    # also contains transformer names (for example "10 MVA Power
                    # Transformer 1").  It is therefore not a safe workspace name.
                    # Keep feeder/transformer names as report metadata, but name the
                    # physical workspace after the source workbook so the folder
                    # identifies the actual imported dataset.
                    workspace_name = source.stem
                    workspace = create_feeder_workspace(source.parent, workspace_name)
                    feeder_jobs.append((source, workspace))
                    print(f"Prepared feeder workspace: {workspace}")
                except Exception as exc:
                    print(f"Could not prepare {source.name}: {exc}")

            if not feeder_jobs:
                print("\nNo feeder jobs could be prepared.")
                print("Returning to the feeder menu.")
                continue
        else:
            feeder_jobs = [(source_excel, feeder_workspace)]

        # Reuse the same authenticated browser across menu selections.
        interrupted = False
        completed_workspaces: list[Path] = []
        try:
            if (not portal_started) or portal._browser_context_is_closed():
                portal.start()
                portal_started = True
            portal.clear_stop_request()
            _SIGINT_REQUESTED = False
            for source, workspace in feeder_jobs:
                result = _run_feeder_batch(source, workspace, selected_datasets, portal)
                completed_workspaces.append(workspace)
                if result.get("interrupted"):
                    interrupted = True
                    break
        except KeyboardInterrupt:
            interrupted = True
            portal.clear_stop_request()
            print("\nCtrl+C received. Downloads stopped; the HES browser remains open.")
        except Exception as exc:
            print("\n============================================================")
            print("HES ACQUISITION PAUSED")
            print("============================================================")
            print(f"Reason: {exc}")
            print("Completed and verified files remain in the batch checkpoint.")
            print("Restart the agent and choose Resume when the connection/session is available.")
            LOG.exception("HES acquisition stopped after an unrecoverable error")
            interrupted = True
        finally:
            # Deliberately do NOT close Chrome here. The initial menu is part of
            # the long-lived application session. Chrome is closed only when the
            # user explicitly chooses option 4 (Exit).
            portal.clear_stop_request()
            _SIGINT_REQUESTED = False

        if interrupted:
            print("\n============================================================")
            print("HES DOWNLOAD STOPPED BY USER")
            print("============================================================")
            print("Completed files were preserved in their dataset folders.")
            print("No further meter or dataset downloads will be started.")
            _offer_folder_analysis()
            print("\nReturning to the initial HES feeder menu. Chrome remains open; no CAPTCHA re-entry is required while the HES session remains valid.")
            continue

        print("\n============================================================")
        print("ALL REQUESTED FEEDER BATCHES COMPLETE")
        print("Every batch has its own date/time folder, dataset folders, logs and")
        print("management/HR-friendly reports. Existing feeder folders remain intact.")
        print("============================================================")
        _offer_folder_analysis()
        print("\nReturning to the initial HES feeder menu. Chrome remains open; the authenticated session is reused.")
        continue


if __name__ == "__main__":
    sys.exit(main())
