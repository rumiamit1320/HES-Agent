from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable


def safe_name(value: object, fallback: str = "Unnamed") -> str:
    text = "" if value is None else str(value).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = text.strip(" .")
    return text[:120] or fallback


def _tk_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    return root


def choose_folder(title: str) -> Path | None:
    """Native Windows folder picker. Used only when a folder is required."""
    try:
        from tkinter import filedialog
        root = _tk_root()
        path = filedialog.askdirectory(
            parent=root,
            title=title,
            mustexist=True,
        )
        root.destroy()
        return Path(path) if path else None
    except Exception as exc:
        print(f"Windows folder picker unavailable: {exc}")
        raw = input("Enter folder path manually (blank to cancel): ").strip().strip('"')
        return Path(raw) if raw else None


def choose_excel_file(title: str) -> Path | None:
    """Native Windows *file* picker for one existing/new feeder Excel file."""
    try:
        from tkinter import filedialog
        root = _tk_root()
        path = filedialog.askopenfilename(
            parent=root,
            title=title,
            filetypes=[
                ("Excel files", "*.xls *.xlsx *.xlsm"),
                ("Excel 97-2003", "*.xls"),
                ("Excel Workbook", "*.xlsx"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
        return Path(path) if path else None
    except Exception as exc:
        print(f"Windows Excel file picker unavailable: {exc}")
        raw = input("Enter Excel file path (blank to cancel): ").strip().strip('"')
        return Path(raw) if raw else None


def choose_excel_files(title: str, multiple: bool = True) -> list[Path]:
    """Native Windows Excel file picker; never opens a folder picker."""
    try:
        from tkinter import filedialog
        root = _tk_root()
        if multiple:
            paths = filedialog.askopenfilenames(
                parent=root,
                title=title,
                filetypes=[
                    ("Excel files", "*.xls *.xlsx *.xlsm"),
                    ("Excel 97-2003", "*.xls"),
                    ("Excel Workbook", "*.xlsx"),
                    ("All files", "*.*"),
                ],
            )
        else:
            paths = filedialog.askopenfilename(
                parent=root,
                title=title,
                filetypes=[
                    ("Excel files", "*.xls *.xlsx *.xlsm"),
                    ("Excel 97-2003", "*.xls"),
                    ("Excel Workbook", "*.xlsx"),
                    ("All files", "*.*"),
                ],
            )
            paths = [paths] if paths else []
        root.destroy()
        return [Path(p) for p in paths]
    except Exception as exc:
        print(f"Windows Excel file picker unavailable: {exc}")
        raw = input("Enter Excel path(s), separated by ; (blank to cancel): ").strip()
        return [Path(x.strip().strip('"')) for x in raw.split(";") if x.strip()]


def choose_feeder_mode() -> str:
    print("\n============================================================")
    print("FEEDER WORKSPACE")
    print("============================================================")
    print("  1. Continue with an existing feeder")
    print("  2. Add/upload new feeder Excel file(s)")
    print("  3. Analyze an existing feeder folder only")
    print("  4. Exit")
    while True:
        value = input("Choose option [1-4]: ").strip()
        if value in {"1", "2", "3", "4"}:
            return value
        print("Invalid choice. Enter 1, 2, 3 or 4.")


def feeder_name_from_dataframe(df, fallback: str) -> str:
    try:
        values = df.get("_feeder")
        if values is not None:
            for value in values.tolist():
                if str(value).strip() and str(value).strip().lower() != "nan":
                    return safe_name(value, fallback)
    except Exception:
        pass
    return safe_name(fallback, "Feeder")


def create_feeder_workspace(parent: Path, feeder_name: str) -> Path:
    workspace = parent / safe_name(feeder_name, "Feeder")
    (workspace / "batches").mkdir(parents=True, exist_ok=True)
    (workspace / "Input").mkdir(parents=True, exist_ok=True)
    (workspace / "Reports").mkdir(parents=True, exist_ok=True)
    return workspace


def create_batch(workspace: Path, source_excel: Path, selected_datasets: Iterable[str]) -> Path:
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("Batch_%Y%m%d_%H%M%S")
    batch = workspace / "batches" / day / stamp
    for name in ("Input", "Downloads", "Reports", "Logs"):
        (batch / name).mkdir(parents=True, exist_ok=True)

    # Preserve the source workbook in the batch so the batch remains self-contained.
    try:
        shutil.copy2(source_excel, batch / "Input" / source_excel.name)
    except Exception:
        pass

    manifest = {
        "created_at": now.isoformat(timespec="seconds"),
        "source_excel": str(source_excel.resolve()),
        "selected_datasets": list(selected_datasets),
        "batch_directory": str(batch),
        "status": "running",
        "checkpoints": {},
    }
    (batch / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return batch



def load_manifest(batch: Path) -> dict:
    """Load a batch checkpoint manifest, returning a safe default when absent."""
    path = batch / "manifest.json"
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def save_manifest(batch: Path, manifest: dict) -> None:
    """Atomically persist the batch checkpoint so interrupted runs can resume."""
    import json
    path = batch / "manifest.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def update_batch_checkpoint(batch: Path, *, meter: object | None = None,
                            dataset: str | None = None, status: str | None = None,
                            file: str | None = None, error: str | None = None) -> dict:
    """Record per-meter/per-dataset progress without changing batch layout."""
    manifest = load_manifest(batch)
    manifest.setdefault("checkpoints", {})
    if meter is not None and dataset:
        key = f"{str(meter).strip()}::{str(dataset).strip()}"
        entry = {
            "meter": str(meter).strip(),
            "dataset": str(dataset).strip(),
            "status": status or "Pending",
            "file": file or "",
        }
        if error:
            entry["error"] = str(error)
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        manifest["checkpoints"][key] = entry
    if status in {"running", "complete", "interrupted", "failed"} and meter is None:
        manifest["status"] = status
        manifest["status_updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_manifest(batch, manifest)
    return manifest


def checkpoint_completed(batch: Path, meter: object, dataset: str) -> Path | None:
    """Return a verified completed file for a meter/dataset, if one exists."""
    manifest = load_manifest(batch)
    key = f"{str(meter).strip()}::{str(dataset).strip()}"
    entry = manifest.get("checkpoints", {}).get(key, {})
    if str(entry.get("status", "")).lower() != "downloaded":
        return None
    raw = str(entry.get("file", "")).strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = batch / raw
    return p if p.is_file() and p.stat().st_size > 0 else None


def find_resumable_batch(workspace: Path, source_excel: Path, selected_datasets: Iterable[str]) -> Path | None:
    """Find the newest incomplete batch for the same source and dataset set."""
    wanted = [str(x) for x in selected_datasets]
    batches_root = workspace / "batches"
    candidates: list[Path] = []
    if not batches_root.is_dir():
        return None
    for p in batches_root.glob("*/Batch_*"):
        if not p.is_dir():
            continue
        m = load_manifest(p)
        if m.get("status") == "complete":
            continue
        source = str(m.get("source_excel", ""))
        datasets = [str(x) for x in m.get("selected_datasets", [])]
        if source and Path(source).resolve() == source_excel.resolve() and datasets == wanted:
            candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.stat().st_mtime)

def find_input_excels(folder: Path) -> list[Path]:
    candidates: list[Path] = []
    for root in (folder / "Input", folder):
        if not root.exists():
            continue
        for p in root.glob("*.xls*"):
            if p.is_file() and not p.name.startswith("~$"):
                candidates.append(p)
    # Keep stable order and remove duplicates.
    return list(dict.fromkeys(candidates))


def select_existing_input(folder: Path) -> Path | None:
    files = find_input_excels(folder)
    if not files:
        print("No feeder Excel file was found in the selected folder.")
        print("Please select the feeder Excel workbook.")
        picked = choose_excel_files("Select the existing feeder Excel workbook", multiple=False)
        return picked[0] if picked else None
    if len(files) == 1:
        return files[0]
    print("\nExisting feeder Excel files:")
    for i, p in enumerate(files, 1):
        print(f"  {i}. {p.name}")
    while True:
        raw = input("Select workbook number: ").strip()
        try:
            idx = int(raw)
            if 1 <= idx <= len(files):
                return files[idx - 1]
        except ValueError:
            pass
        print("Invalid selection.")


def move_download_to_dataset_folder(downloaded: Path, batch: Path, dataset: str) -> Path:
    target_dir = batch / "Downloads" / safe_name(dataset, "Dataset")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / downloaded.name
    if downloaded.resolve() != target.resolve():
        if target.exists():
            target.unlink()
        shutil.move(str(downloaded), str(target))
    return target


def discover_downloads(folder: Path) -> list[tuple[str, Path]]:
    """Discover previously downloaded HES files for folder-only analysis."""
    result: list[tuple[str, Path]] = []
    for p in folder.rglob("*.xls*"):
        if not p.is_file() or p.name.startswith("~$"):
            continue
        # Skip input workbooks and generated reports.
        parts = {x.lower() for x in p.parts}
        if "input" in parts or "reports" in parts:
            continue
        dataset = "Power Outage" if "power outage" in str(p.parent).lower() or "power_outage" in p.name.lower() else "Unknown"
        result.append((dataset, p))
    return result
