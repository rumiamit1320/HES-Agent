from __future__ import annotations

from pathlib import Path
import re
import pandas as pd


def _safe_sheet(name: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9 _-]", "_", str(name)).strip()[:31] or "Sheet"
    value, n = base, 2
    while value in used:
        suffix = f"_{n}"; value = base[:31-len(suffix)] + suffix; n += 1
    used.add(value)
    return value


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).strip().lower())


def _num(df: pd.DataFrame, col: str) -> float:
    return float(pd.to_numeric(df[col], errors="coerce").sum(skipna=True)) if col in df.columns else 0.0


def _metric(label: str, value: object) -> dict[str, object]:
    return {"Metric": label, "Value": value}


def _find_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    mapping = {_norm(c): c for c in df.columns}
    for alias in aliases:
        if _norm(alias) in mapping:
            return mapping[_norm(alias)]
    return None


def _exact_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    mapping = {_norm(c): c for c in df.columns}
    for alias in aliases:
        if _norm(alias) in mapping:
            return mapping[_norm(alias)]
    return None


def _metadata_lookup(metadata: pd.DataFrame) -> pd.DataFrame:
    if metadata is None or metadata.empty or "_meter_no" not in metadata.columns:
        return pd.DataFrame(columns=["Meter Number", "Feeder Number", "Feeder Name", "Substation", "Voltage Level"])
    rows = metadata.copy()
    rows["Meter Number"] = rows["_meter_no"].astype(str).str.strip()

    def val(aliases: list[str]) -> pd.Series:
        col = _exact_col(rows, aliases)
        if col:
            return rows[col].fillna("").astype(str).str.strip()
        return pd.Series([""] * len(rows), index=rows.index, dtype=str)

    out = pd.DataFrame({
        "Meter Number": rows["Meter Number"],
        "Feeder Number": val(["Feeder Number", "Feeder No", "Feeder ID", "Feeder Code"]),
        "Feeder Name": rows.get("_feeder", "").fillna("").astype(str).str.strip(),
        "Substation": rows.get("_substation", "").fillna("").astype(str).str.strip(),
        "Voltage Level": val(["Rated voltage of feeder in KV", "Rated Voltage", "Voltage Level"]),
    })
    return out.drop_duplicates("Meter Number", keep="first")


def build_power_outage_monthly_report(raw: pd.DataFrame, metadata: pd.DataFrame) -> pd.DataFrame:
    """Return the requested month-as-columns report with parameter rows."""
    empty_cols = ["Substation", "Feeder Number", "Feeder Name", "Meter Number", "Parameter"]
    if raw is None or raw.empty or "Meter No" not in raw.columns:
        return pd.DataFrame(columns=empty_cols)
    d = raw.copy()
    d["Meter Number"] = d["Meter No"].astype(str).str.strip()
    d["Outage Start"] = pd.to_datetime(d.get("Outage Start"), errors="coerce")
    d["Outage End"] = pd.to_datetime(d.get("Outage End"), errors="coerce")
    if "Duration (min)" not in d.columns:
        d["Duration (min)"] = (d["Outage End"] - d["Outage Start"]).dt.total_seconds() / 60.0
    d["Duration (min)"] = pd.to_numeric(d["Duration (min)"], errors="coerce")
    d = d.dropna(subset=["Outage Start"])
    if d.empty:
        return pd.DataFrame(columns=empty_cols)
    d["Month"] = d["Outage Start"].dt.to_period("M").astype(str)
    meta = _metadata_lookup(metadata)
    d = d.merge(meta, on="Meter Number", how="left", suffixes=("", "_meta"))
    # Metadata from the selected feeder workbook is authoritative.
    if "_feeder" in d.columns:
        d["Feeder Name"] = d["Feeder Name"].replace("", pd.NA).fillna(d["_feeder"])
    if "_substation" in d.columns:
        d["Substation"] = d["Substation"].replace("", pd.NA).fillna(d["_substation"])

    group_cols = ["Substation", "Feeder Number", "Feeder Name", "Meter Number", "Month"]
    g = d.groupby(group_cols, dropna=False)
    records = g.size().rename("Outage Records").to_frame()
    records["Interruptions <1 min"] = g["Duration (min)"].apply(lambda s: int((s < 1).sum()))
    records["Interruptions 1–5 min"] = g["Duration (min)"].apply(lambda s: int(((s >= 1) & (s <= 5)).sum()))
    records["Interruptions >5 min"] = g["Duration (min)"].apply(lambda s: int((s > 5).sum()))
    records["Total Outage Duration (min)"] = g["Duration (min)"].sum()
    records["Total Outage Duration (hr)"] = records["Total Outage Duration (min)"] / 60.0
    records["First Outage"] = g["Outage Start"].min().dt.strftime("%Y-%m-%d %H:%M:%S")
    records["Last Outage"] = g["Outage Start"].max().dt.strftime("%Y-%m-%d %H:%M:%S")
    records = records.reset_index()

    params = [
        "Outage Records", "Interruptions <1 min", "Interruptions 1–5 min", "Interruptions >5 min",
        "Total Outage Duration (min)", "Total Outage Duration (hr)", "First Outage", "Last Outage",
    ]
    id_cols = ["Substation", "Feeder Number", "Feeder Name", "Meter Number"]
    month_cols = sorted(records["Month"].dropna().unique().tolist())
    rows = []
    for keys, group in records.groupby(id_cols, dropna=False, sort=True):
        key_values = list(keys) if isinstance(keys, tuple) else [keys]
        lookup = group.set_index("Month")
        for param in params:
            row = dict(zip(id_cols, key_values))
            row["Parameter"] = param
            for month in month_cols:
                row[month] = lookup.loc[month, param] if month in lookup.index else ""
            rows.append(row)
    out = pd.DataFrame(rows, columns=id_cols + ["Parameter"] + month_cols)
    return out


def build_power_outage_monthly_detail(raw: pd.DataFrame, metadata: pd.DataFrame) -> pd.DataFrame:
    """Long-form monthly detail retained alongside the management pivot."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    d = raw.copy()
    d["Meter Number"] = d["Meter No"].astype(str).str.strip()
    d["Outage Start"] = pd.to_datetime(d.get("Outage Start"), errors="coerce")
    d["Outage End"] = pd.to_datetime(d.get("Outage End"), errors="coerce")
    d["Month"] = d["Outage Start"].dt.to_period("M").astype(str)
    meta = _metadata_lookup(metadata)
    d = d.merge(meta, on="Meter Number", how="left", suffixes=("", "_meta"))
    if "_feeder" in d.columns:
        d["Feeder Name"] = d["Feeder Name"].replace("", pd.NA).fillna(d["_feeder"])
    if "_substation" in d.columns:
        d["Substation"] = d["Substation"].replace("", pd.NA).fillna(d["_substation"])
    return d


def build_billing_profile_report(frame: pd.DataFrame | None, metadata: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or "Meter No" not in frame.columns:
        return pd.DataFrame() if frame is None else frame.copy()
    d = frame.copy(); d["Meter Number"] = d["Meter No"].astype(str).str.strip()
    meta = _metadata_lookup(metadata)
    d = d.merge(meta, on="Meter Number", how="left", suffixes=("", "_meta"))
    meta_cols = ["Meter Number", "Feeder Number", "Feeder Name", "Substation", "Voltage Level"]
    rest = [c for c in d.columns if c not in meta_cols and c != "Meter No"]
    return d[meta_cols + rest].sort_values(["Feeder Name", "Meter Number"], na_position="last")


def build_recent_instant_profile_report(frame: pd.DataFrame | None, metadata: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or "Meter No" not in frame.columns:
        return pd.DataFrame() if frame is None else frame.copy()
    d = frame.copy(); d["Meter Number"] = d["Meter No"].astype(str).str.strip()
    date_col = _find_col(d, ["Reading Date", "Meter RTC", "RTC", "Real Time Clock", "Date Time", "Timestamp"])
    if date_col:
        d["_time"] = pd.to_datetime(d[date_col], errors="coerce", dayfirst=True)
        d["_order"] = range(len(d))
        d = d.sort_values(["Meter Number", "_time", "_order"], na_position="first")
        latest = d.groupby("Meter Number", as_index=False).tail(1).copy().drop(columns=["_time", "_order"], errors="ignore")
    else:
        latest = d.groupby("Meter Number", as_index=False).tail(1).copy()
    meta = _metadata_lookup(metadata)
    latest = latest.merge(meta, on="Meter Number", how="left", suffixes=("", "_meta"))
    meta_cols = ["Meter Number", "Feeder Number", "Feeder Name", "Substation", "Voltage Level"]
    rest = [c for c in latest.columns if c not in meta_cols and c != "Meter No"]
    return latest[meta_cols + rest].sort_values(["Feeder Name", "Meter Number"], na_position="last")


def build_management_summary(metadata, raw, meter_summary, events, feeder_summary, sub_summary, log_df, selected_datasets, batch_directory):
    input_meters = int(metadata["_meter_no"].astype(str).str.strip().nunique()) if "_meter_no" in metadata else 0
    downloaded = int((log_df.get("Status", pd.Series(dtype=str)) == "Downloaded").sum())
    no_record = int((log_df.get("Status", pd.Series(dtype=str)) == "No record").sum())
    failed = int((~log_df.get("Status", pd.Series(dtype=str)).isin(["Downloaded", "No record"])).sum())
    affected_meters = int(raw["Meter No"].astype(str).nunique()) if not raw.empty and "Meter No" in raw.columns else 0
    outage_records = len(raw)
    total_minutes = _num(raw, "Duration (min)")
    rows = [
        _metric("Report Type", "HES Feeder / Outage Management Report"),
        _metric("Batch Directory", str(batch_directory)),
        _metric("Selected HES Datasets", ", ".join(selected_datasets)),
        _metric("Input Meters", input_meters),
        _metric("Downloaded Dataset Operations", downloaded),
        _metric("No-record Operations", no_record),
        _metric("Failed Operations", failed),
        _metric("Meters with Outage Records", affected_meters),
        _metric("Outage Records", outage_records),
        _metric("Outage Events", len(events)),
        _metric("Feeders with Outage Records", len(feeder_summary)),
        _metric("Substations with Outage Records", len(sub_summary)),
        _metric("Total Outage Duration (min)", round(total_minutes, 3)),
        _metric("Total Outage Duration (hr)", round(total_minutes / 60.0, 3)),
        _metric("Average Outage Record Duration (min)", round(total_minutes / outage_records, 3) if outage_records else 0),
        _metric("Interruptions <1 min", int((pd.to_numeric(raw.get("Duration (min)"), errors="coerce") < 1).sum()) if not raw.empty else 0),
        _metric("Interruptions 1–5 min", int(((pd.to_numeric(raw.get("Duration (min)"), errors="coerce") >= 1) & (pd.to_numeric(raw.get("Duration (min)"), errors="coerce") <= 5)).sum()) if not raw.empty else 0),
        _metric("Interruptions >5 min", int((pd.to_numeric(raw.get("Duration (min)"), errors="coerce") > 5).sum()) if not raw.empty else 0),
    ]
    return pd.DataFrame(rows)


def write_management_report(report_dir: Path, metadata: pd.DataFrame, raw: pd.DataFrame, meter_summary: pd.DataFrame,
                             events: pd.DataFrame, feeder_summary: pd.DataFrame, sub_summary: pd.DataFrame,
                             log_df: pd.DataFrame, selected_datasets: list[str], batch_directory: Path,
                             dataset_frames: dict[str, pd.DataFrame] | None = None) -> dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    dataset_frames = dataset_frames or {}
    summary = build_management_summary(metadata, raw, meter_summary, events, feeder_summary, sub_summary, log_df, selected_datasets, batch_directory)
    outage_monthly = build_power_outage_monthly_report(raw, metadata)
    outage_detail = build_power_outage_monthly_detail(raw, metadata)
    billing_report = build_billing_profile_report(dataset_frames.get("Bill Profile"), metadata)
    instant_recent = build_recent_instant_profile_report(dataset_frames.get("Instant Profile"), metadata)

    sheets = {
        "Executive Summary": summary,
        "Power Outage Monthly": outage_monthly,
        "Power Outage Monthly Detail": outage_detail,
        "Billing Profile All": billing_report,
        "Recent Instant Profile": instant_recent,
        "Feeder Performance": feeder_summary.copy(),
        "Substation Summary": sub_summary.copy(),
        "Meter Detail": meter_summary.copy(),
        "Outage Events": events.copy(),
        "Outage Records": raw.copy(),
        "Download Status": log_df.copy(),
    }
    for dataset, frame in dataset_frames.items():
        sheets[f"Dataset - {dataset}"] = frame.copy()

    paths: dict[str, Path] = {}
    focused = {
        "Feeder_Power_Outage_Monthly_Report.xlsx": outage_monthly,
        "Feeder_Billing_Profile_All_Meters_Report.xlsx": billing_report,
        "Feeder_Recent_Instant_Profile_Report.xlsx": instant_recent,
    }
    for filename, frame in focused.items():
        target = report_dir / filename; frame.to_excel(target, index=False); paths[target.stem] = target

    workbook = report_dir / "Management_HR_Detailed_Report.xlsx"
    used: set[str] = set()
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            sheet = _safe_sheet(name, used); frame.to_excel(writer, sheet_name=sheet, index=False)
            ws = writer.book[sheet]; ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
            for col_cells in ws.columns:
                values = ["" if c.value is None else str(c.value) for c in list(col_cells)[:1000]]
                width = min(max(max((len(v) for v in values), default=10) + 2, 12), 45)
                ws.column_dimensions[col_cells[0].column_letter].width = width
    paths["Management_HR_Detailed_Report"] = workbook

    # Keep the existing PDF output, but use the corrected KPI values.
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
        pdf = report_dir / "Management_HR_Summary_Report.pdf"
        doc = SimpleDocTemplate(str(pdf), pagesize=landscape(A4), rightMargin=12*mm, leftMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm)
        styles = getSampleStyleSheet(); story = [Paragraph("HES Feeder / Outage Management Report", styles["Title"]), Paragraph(f"Batch: {batch_directory.name}", styles["Normal"]), Spacer(1, 6*mm)]
        data = [["Metric", "Value"]] + summary.astype(str).values.tolist()
        t = Table(data, repeatRows=1); t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1f4e78")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("GRID", (0,0), (-1,-1), .4, colors.grey), ("FONTSIZE", (0,0), (-1,-1), 8), ("VALIGN", (0,0), (-1,-1), "TOP")]))
        story.append(t); story.append(PageBreak())
        for title, frame, limit in [("Feeder Performance", feeder_summary, 40), ("Substation Summary", sub_summary, 30), ("Meter Detail", meter_summary, 60)]:
            story.append(Paragraph(title, styles["Heading2"]))
            if frame.empty: story.append(Paragraph("No records available.", styles["Normal"]))
            else:
                display = frame.head(limit).fillna("").astype(str); data = [[str(x)[:45] for x in display.columns]] + [[str(x)[:45] for x in row] for row in display.values]
                t = Table(data, repeatRows=1); t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#5b9bd5")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("GRID", (0,0), (-1,-1), .25, colors.grey), ("FONTSIZE", (0,0), (-1,-1), 6), ("VALIGN", (0,0), (-1,-1), "TOP")]))
                story.append(t)
            story.append(Spacer(1, 5*mm))
        doc.build(story); paths["Management_HR_Summary_Report"] = pdf
    except Exception:
        pass
    return paths
