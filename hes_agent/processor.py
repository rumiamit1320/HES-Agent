from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import pandas as pd


def _norm(s: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).strip().lower())


def find_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    if df is None:
        return None
    m = {_norm(c): c for c in df.columns}
    for a in aliases:
        if _norm(a) in m:
            return m[_norm(a)]
    for a in aliases:
        na = _norm(a)
        for k, original in m.items():
            if na and (na in k or k in na):
                return original
    return None


def _exact_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    if df is None:
        return None
    mapping = {_norm(c): c for c in df.columns}
    for alias in aliases:
        if _norm(alias) in mapping:
            return mapping[_norm(alias)]
    return None


def _parse_hes_duration_minutes(series: pd.Series) -> pd.Series:
    """Parse HES duration fields such as Day:Hrs:Min or HH:MM:SS."""
    numeric = pd.to_numeric(series, errors="coerce")
    text = series.astype(str).str.strip()
    out = numeric.copy()
    missing = out.isna()
    if not missing.any():
        return out
    def parse_one(v: str):
        if not v or v.lower() in {"nan", "nat", "none"}:
            return float("nan")
        parts = v.split(":")
        try:
            nums = [float(x) for x in parts]
        except Exception:
            return float("nan")
        if len(nums) == 3:
            # HES uses Day:Hrs:Min for outage/tamper exports.
            return nums[0] * 1440.0 + nums[1] * 60.0 + nums[2]
        if len(nums) == 2:
            return nums[0] * 60.0 + nums[1]
        return float("nan")
    out.loc[missing] = text.loc[missing].map(parse_one)
    return out


def _derive_outage_duration_minutes(start: pd.Series, end: pd.Series, supplied: pd.Series | None = None) -> pd.Series:
    """Use timestamps as authoritative duration, falling back to HES duration text."""
    result = pd.Series(float("nan"), index=start.index, dtype=float)
    valid_ts = start.notna() & end.notna() & (end >= start)
    result.loc[valid_ts] = (end.loc[valid_ts] - start.loc[valid_ts]).dt.total_seconds() / 60.0
    if supplied is not None:
        fallback = _parse_hes_duration_minutes(supplied)
        result = result.fillna(fallback)
    return result.round(6)


def read_download(path: Path, meter_no: str, dataset: str = "Power Outage") -> pd.DataFrame:
    """Read one HES outage export and derive a reliable duration from timestamps."""
    df = pd.read_excel(path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if df.empty:
        return pd.DataFrame({"Meter No": [str(meter_no)], "Dataset": [dataset]})

    start = find_col(df, ["Last Gasp", "Last Gasp Time", "Outage Start", "Start Time", "Power Failure Time", "Event Time", "Failure Date", "Event Date"])
    end = find_col(df, ["First Breath", "First Breath Time", "Restoration Time", "Outage End", "End Time", "Power Restore Time", "Restoration Date"])
    duration = find_col(df, ["Duration (Day:Hrs:Min)", "Duration (Day: Hrs: Min)", "Duration Minutes", "Outage Duration", "Duration"])

    out = pd.DataFrame(index=df.index)
    out["Meter No"] = str(meter_no)
    out["Dataset"] = dataset
    out["Outage Start"] = pd.to_datetime(df[start], errors="coerce", dayfirst=False) if start else pd.NaT
    out["Outage End"] = pd.to_datetime(df[end], errors="coerce", dayfirst=False) if end else pd.NaT
    supplied = df[duration] if duration else None
    out["Duration (min)"] = _derive_outage_duration_minutes(out["Outage Start"], out["Outage End"], supplied)
    out["Duration Category"] = pd.cut(
        out["Duration (min)"],
        bins=[-float("inf"), 1.0, 5.0, float("inf")],
        labels=["<1 min", "1–5 min", ">5 min"],
        right=False,
    ).astype(object)
    out["HES_Source_File"] = path.name
    for c in df.columns:
        out[f"HES_{c}"] = df[c].values
    return out


def read_generic_download(path: Path, meter_no: str, dataset: str) -> pd.DataFrame:
    """Read a non-outage HES export while preserving original fields."""
    try:
        df = pd.read_excel(path, dtype=str)
    except Exception as excel_exc:
        raw = path.read_bytes()
        sample = raw[:262144].decode("utf-8-sig", errors="ignore").lower()
        try:
            import io
            if "<html" in sample or "<table" in sample:
                tables = pd.read_html(io.BytesIO(raw), flavor="lxml")
                if not tables:
                    raise ValueError("No HTML tables found")
                df = tables[0]
            elif "<worksheet" in sample or "urn:schemas-microsoft-com:office:spreadsheet" in sample:
                df = pd.read_xml(io.BytesIO(raw))
            else:
                text = raw.decode("utf-8-sig", errors="replace")
                sep = "\t" if "\t" in text.splitlines()[0] else ","
                df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str)
        except Exception:
            raise excel_exc
    df.columns = [str(c).strip() for c in df.columns]
    if df.empty:
        return pd.DataFrame({"Meter No": [str(meter_no)], "Dataset": [dataset]})
    out = df.copy()
    out.insert(0, "Dataset", dataset)
    out.insert(0, "Meter No", str(meter_no))
    out["HES_Source_File"] = path.name
    return out


def deduplicate_frame(df: pd.DataFrame | None, dataset: str = "") -> pd.DataFrame:
    """Return a canonical copy with repeated exports removed.

    HES can leave the same logical export in more than one batch/checkpoint.
    Source filenames and acquisition metadata must not make identical HES rows
    look like new records.  Deduplicate on the actual exported data columns and
    exclude provenance columns that legitimately differ between acquisitions.
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()
    d = df.copy()
    provenance = {
        "HES_Source_File", "Source File", "File", "Batch", "Batch Directory",
        "Download Time", "Downloaded At", "_source_file", "_batch",
    }
    key_cols = [c for c in d.columns if c not in provenance]
    if not key_cols:
        return d.drop_duplicates(ignore_index=True)
    # Normalize only the values used for duplicate identity; retain original data.
    key = d[key_cols].copy()
    for c in key.columns:
        key[c] = key[c].map(lambda v: "" if pd.isna(v) else str(v).strip())
    mask = ~key.duplicated(keep="first")
    return d.loc[mask].reset_index(drop=True)


def deduplicate_download_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge repeated acquisitions of the same meter/dataset and deduplicate rows."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for item in items:
        frame = item.get("frame")
        if not isinstance(frame, pd.DataFrame):
            passthrough.append(item)
            continue
        meter = str(item.get("meter", "")).strip()
        dataset = str(item.get("dataset", "")).strip()
        grouped.setdefault((meter, dataset), []).append(item)

    result = list(passthrough)
    for (meter, dataset), group in grouped.items():
        frames = [x["frame"] for x in group if isinstance(x.get("frame"), pd.DataFrame) and not x["frame"].empty]
        if not frames:
            continue
        merged = deduplicate_frame(pd.concat(frames, ignore_index=True, sort=False), dataset)
        if merged.empty:
            continue
        base = dict(group[0])
        base["meter"] = meter
        base["dataset"] = dataset
        base["frame"] = merged
        result.append(base)
    return result


def deduplicate_metadata(metadata: pd.DataFrame | None) -> pd.DataFrame:
    """Keep one authoritative feeder-master row per unique meter number."""
    if metadata is None or metadata.empty or "_meter_no" not in metadata.columns:
        return pd.DataFrame() if metadata is None else metadata.copy()
    m = metadata.copy()
    m["_meter_no"] = m["_meter_no"].astype(str).str.strip()
    m = m[m["_meter_no"].ne("") & m["_meter_no"].str.lower().ne("nan")]
    return m.drop_duplicates("_meter_no", keep="first").reset_index(drop=True)


def _first_value(frames: list[pd.DataFrame], aliases: list[str]) -> str:
    for df in frames:
        col = find_col(df, aliases)
        if col:
            s = df[col].dropna().astype(str).str.strip()
            s = s[(s != "") & (s.str.lower() != "nan")]
            if not s.empty:
                return s.iloc[0]
    return ""


def _date_values(frames: list[pd.DataFrame], aliases: list[str]) -> list[pd.Timestamp]:
    values: list[pd.Timestamp] = []
    for df in frames:
        col = find_col(df, aliases)
        if not col:
            continue
        parsed = pd.to_datetime(df[col], errors="coerce", format="mixed", dayfirst=True)
        values.extend(parsed.dropna().tolist())
    return values


def _latest_instant_values(frames: list[pd.DataFrame]) -> dict[str, object]:
    if not frames:
        return {}
    d = pd.concat(frames, ignore_index=True, sort=False)
    meter_col = find_col(d, ["Meter No", "Meter Number", "Meter ID"])
    date_col = find_col(d, ["Reading Date", "Meter RTC", "RTC", "Real Time Clock", "Date Time", "Timestamp"])
    if not meter_col or not date_col or d.empty:
        return {}
    d["_parsed_time"] = pd.to_datetime(d[date_col], errors="coerce", format="mixed", dayfirst=True)
    d["_meter_key"] = d[meter_col].astype(str).str.strip()
    d = d.sort_values(["_meter_key", "_parsed_time"], na_position="first")
    latest = d.groupby("_meter_key", sort=False).tail(1).copy()
    return latest.set_index("_meter_key").to_dict("index")


def build_final_meter_summary(metadata: pd.DataFrame, downloaded_frames: list[dict[str, Any]]) -> pd.DataFrame:
    """Build one row per input meter with correct metadata and latest electrical values."""
    metadata = deduplicate_metadata(metadata)
    downloaded_frames = deduplicate_download_items(downloaded_frames)
    by_meter: dict[str, list[dict[str, Any]]] = {}
    for item in downloaded_frames:
        by_meter.setdefault(str(item["meter"]).strip(), []).append(item)

    rows = []
    for _, meta in metadata.iterrows():
        meter = str(meta.get("_meter_no", "")).strip()
        items = by_meter.get(meter, [])
        frames = [x["frame"] for x in items if isinstance(x.get("frame"), pd.DataFrame)]

        def meta_value(aliases: list[str]) -> str:
            col = _exact_col(metadata, aliases)
            if col and col in meta.index:
                v = meta.get(col, "")
                if pd.notna(v) and str(v).strip().lower() != "nan":
                    return str(v).strip()
            return ""

        # The feeder workbook is authoritative for feeder/substation/rated voltage.
        feeder_name = str(meta.get("_feeder", "") or "").strip()
        substation = str(meta.get("_substation", "") or "").strip()
        feeder_no = meta_value(["Feeder Number", "Feeder No", "Feeder ID", "Feeder Code"])
        voltage_level = meta_value(["Rated voltage of feeder in KV", "Rated Voltage", "Voltage Level"])

        instant_frames = [x["frame"] for x in items if str(x.get("dataset")) == "Instant Profile" and isinstance(x.get("frame"), pd.DataFrame)]
        latest = _latest_instant_values(instant_frames)
        latest_row = latest.get(meter, {})
        pvals = pd.to_numeric(pd.Series([latest_row.get(c, None) for c in ["Voltage P1", "Voltage P2", "Voltage P3"]]), errors="coerce").dropna()
        ivals = pd.to_numeric(pd.Series([latest_row.get(c, None) for c in ["Current P1", "Current P2", "Current P3"]]), errors="coerce").dropna()
        current_voltage = float(pvals.mean()) if not pvals.empty else ""
        current = float(ivals.mean()) if not ivals.empty else ""
        rtc = latest_row.get(find_col(pd.DataFrame([latest_row]), ["Reading Date", "Meter RTC", "RTC", "Real Time Clock"]), "") if latest_row else ""

        outage_items = [x for x in items if str(x.get("dataset")) == "Power Outage" and isinstance(x.get("frame"), pd.DataFrame)]
        outage_count = 0
        total_outage = 0.0
        outage_dates: list[pd.Timestamp] = []
        for item in outage_items:
            f = item["frame"].copy()
            if "Duration (min)" in f.columns:
                vals = pd.to_numeric(f["Duration (min)"], errors="coerce")
                total_outage += float(vals.sum(skipna=True))
                outage_count += int(vals.notna().sum())
            if "Outage Start" in f.columns:
                outage_dates.extend(pd.to_datetime(f["Outage Start"], errors="coerce").dropna().tolist())

        all_dates = outage_dates + _date_values(frames, [
            "Data Available From", "Available From", "Data From", "From Date", "Data Start Date",
            "Start Date", "Reading Date", "Meter RTC", "RTC", "Transaction Date", "Date Time", "Timestamp",
        ])
        data_from = min(all_dates) if all_dates else pd.NaT

        rows.append({
            "Meter Number": meter,
            "Feeder Number": feeder_no,
            "Feeder Name": feeder_name,
            "Substation": substation,
            "Voltage Level": voltage_level,
            "Current Voltage": round(current_voltage, 3) if current_voltage != "" else "",
            "Current": round(current, 3) if current != "" else "",
            "Total Power Outage Duration (min)": round(total_outage, 3),
            "Total Power Outage Duration (hr)": round(total_outage / 60.0, 3),
            "Outage Records": outage_count,
            "Data Available From": data_from,
            "RTC": rtc,
            "Download Status": "Downloaded" if items else "No data downloaded",
            "Datasets Downloaded": ", ".join(dict.fromkeys(str(x.get("dataset", "")) for x in items if x.get("dataset"))),
        })
    return pd.DataFrame(rows)


def _class_counts(group: pd.DataFrame) -> pd.Series:
    d = pd.to_numeric(group["Duration (min)"], errors="coerce")
    return pd.Series({
        "Interruptions <1 min": int((d < 1).sum()),
        "Interruptions 1–5 min": int(((d >= 1) & (d <= 5)).sum()),
        "Interruptions >5 min": int((d > 5).sum()),
    })


def build_events(raw: pd.DataFrame, metadata: pd.DataFrame, meter_col: str, gap_minutes: float = 5.0):
    meta = metadata.copy()
    meta["_meter_key"] = meta["_meter_no"].astype(str).str.strip()
    r = raw.copy()
    if r.empty:
        return r, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    r["_meter_key"] = r["Meter No"].astype(str).str.strip()
    joined = r.merge(meta.drop_duplicates("_meter_key"), on="_meter_key", how="left", suffixes=("", "_meta"))
    joined["Outage Start"] = pd.to_datetime(joined["Outage Start"], errors="coerce")
    joined["Outage End"] = pd.to_datetime(joined["Outage End"], errors="coerce")
    supplied = joined["Duration (min)"] if "Duration (min)" in joined.columns else None
    joined["Duration (min)"] = _derive_outage_duration_minutes(joined["Outage Start"], joined["Outage End"], supplied)
    joined["Duration Category"] = pd.cut(joined["Duration (min)"], bins=[-float("inf"), 1, 5, float("inf")], labels=["<1 min", "1–5 min", ">5 min"], right=False).astype(object)
    joined = joined.drop(columns=["_meter_key"], errors="ignore")
    valid = joined.dropna(subset=["Outage Start"]).copy()
    if valid.empty:
        return joined, valid, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    # Feeder events are merged by overlapping/nearby outage intervals, not simply
    # by start-time gaps. Duplicate meter rows within one event are de-duplicated.
    valid = valid.sort_values(["_feeder", "Outage Start", "Outage End", "Meter No"])
    event_rows = []
    for feeder, g in valid.groupby("_feeder", dropna=False):
        current = None
        for _, row in g.iterrows():
            start = row["Outage Start"]
            end = row["Outage End"] if pd.notna(row["Outage End"]) else start
            if current is None or start > current["Event End"] + pd.Timedelta(minutes=gap_minutes):
                if current:
                    current["Meter Count"] = len(current["Meters"])
                    current["Meters"] = ", ".join(sorted(current["Meters"]))
                    event_rows.append(current)
                current = {
                    "Feeder": feeder,
                    "Substation": row.get("_substation", ""),
                    "Event Start": start,
                    "Event End": end,
                    "Meters": {str(row["Meter No"]).strip()},
                }
            else:
                current["Event End"] = max(current["Event End"], end)
                current["Meters"].add(str(row["Meter No"]).strip())
        if current:
            current["Meter Count"] = len(current["Meters"])
            current["Meters"] = ", ".join(sorted(current["Meters"]))
            event_rows.append(current)
    events = pd.DataFrame(event_rows)
    if not events.empty:
        events["Event Duration (min)"] = (pd.to_datetime(events["Event End"]) - pd.to_datetime(events["Event Start"])).dt.total_seconds() / 60.0
        events["Event Duration (hr)"] = events["Event Duration (min)"] / 60.0
        events = events[["Feeder", "Substation", "Event Start", "Event End", "Event Duration (min)", "Event Duration (hr)", "Meter Count", "Meters"]]

    feeder_summary = valid.groupby(["_substation", "_feeder"], dropna=False).agg(
        Affected_Meters=("Meter No", "nunique"), Outage_Records=("Meter No", "size"),
        First_Outage=("Outage Start", "min"), Last_Outage=("Outage Start", "max"),
        Total_Outage_Minutes=("Duration (min)", "sum"),
    ).reset_index().rename(columns={"_substation": "Substation", "_feeder": "Feeder"})
    classes = valid.groupby(["_substation", "_feeder"], dropna=False).apply(_class_counts, include_groups=False).reset_index()
    feeder_summary = feeder_summary.merge(classes, on=["_substation", "_feeder"], how="left") if "_substation" in feeder_summary.columns else feeder_summary
    # Above merge is unnecessary after rename; calculate directly for stability.
    classes = valid.groupby(["_substation", "_feeder"], dropna=False)["Duration (min)"].agg(
        **{"Interruptions <1 min": lambda s: int((s < 1).sum()),
           "Interruptions 1–5 min": lambda s: int(((s >= 1) & (s <= 5)).sum()),
           "Interruptions >5 min": lambda s: int((s > 5).sum())}
    ).reset_index().rename(columns={"_substation": "Substation", "_feeder": "Feeder"})
    feeder_summary = feeder_summary.drop(columns=["_substation", "_feeder"], errors="ignore").merge(classes, on=["Substation", "Feeder"], how="left")
    feeder_summary["Total_Outage_Hours"] = feeder_summary["Total_Outage_Minutes"] / 60.0

    sub_summary = feeder_summary.groupby("Substation", dropna=False).agg(
        Feeders_Affected=("Feeder", "nunique"), Affected_Meters=("Affected_Meters", "sum"),
        Outage_Records=("Outage_Records", "sum"), First_Outage=("First_Outage", "min"),
        Last_Outage=("Last_Outage", "max"), Total_Outage_Minutes=("Total_Outage_Minutes", "sum"),
        **{"Interruptions <1 min": ("Interruptions <1 min", "sum"), "Interruptions 1–5 min": ("Interruptions 1–5 min", "sum"), "Interruptions >5 min": ("Interruptions >5 min", "sum")},
    ).reset_index()
    sub_summary["Total_Outage_Hours"] = sub_summary["Total_Outage_Minutes"] / 60.0
    return joined, valid, events, feeder_summary, sub_summary


def write_report(output_dir: Path, metadata: pd.DataFrame, raw: pd.DataFrame, meter_summary: pd.DataFrame,
                 events: pd.DataFrame, feeder_summary: pd.DataFrame, sub_summary: pd.DataFrame, log_df: pd.DataFrame,
                 final_summary: pd.DataFrame | None = None, dataset_frames: dict[str, pd.DataFrame] | None = None):
    output_dir.mkdir(parents=True, exist_ok=True)
    sheets = {
        "Input_Meter_Master": metadata.drop(columns=["_meter_col"], errors="ignore"),
        "HES_Raw_Outage": raw,
        "Meter_Outage_Summary": meter_summary,
        "Feeder_Outage_Events": events,
        "Feeder_Summary": feeder_summary,
        "Substation_Summary": sub_summary,
        "Download_Log": log_df,
    }
    if final_summary is not None:
        sheets["Final_Meter_Summary"] = final_summary
    if dataset_frames:
        for name, frame in dataset_frames.items():
            safe = re.sub(r"[^A-Za-z0-9 _-]", "_", name).strip()[:31] or "HES_Data"
            base, n = safe, 2
            while safe in sheets:
                suffix = f"_{n}"; safe = base[:31-len(suffix)] + suffix; n += 1
            sheets[safe] = frame
    paths = {}
    for name, df in sheets.items():
        p = output_dir / f"{name}.xlsx"; df.to_excel(p, index=False); paths[name] = p
    combined = output_dir / "HES_Final_Report.xlsx"
    with pd.ExcelWriter(combined, engine="openpyxl") as writer:
        for name, df in sheets.items(): df.to_excel(writer, sheet_name=name[:31], index=False)
    paths["HES_Final_Report"] = combined
    paths["Power_Outage_Report"] = combined
    return paths
