from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable

import pandas as pd

METER_ALIASES = [
    "meter no", "meter number", "meter no.", "meter id", "meterid",
    "meter_no", "meter_number", "meter", "consumer meter no", "consumer meter number"
]
FEEDER_ALIASES = [
    "name of feeder (power transformer line)", "name of feeder", "feeder name",
    "feeder", "feeder_name", "feedername"
]
SUBSTATION_ALIASES = [
    "name of sub-station", "name of substation", "substation name",
    "sub-station", "substation", "sub_station"
]


def norm(s: object) -> str:
    s = "" if s is None else str(s)
    s = re.sub(r"\s+", " ", s.strip().lower())
    return s


def find_column(columns: Iterable[object], aliases: list[str]) -> str | None:
    cols = list(columns)
    mapping = {norm(c): c for c in cols}
    for alias in aliases:
        if norm(alias) in mapping:
            return mapping[norm(alias)]
    # More tolerant fallback: remove punctuation.
    def compact(x: object) -> str:
        return re.sub(r"[^a-z0-9]", "", norm(x))
    cm = {compact(c): c for c in cols}
    for alias in aliases:
        if compact(alias) in cm:
            return cm[compact(alias)]
    return None


def read_input(path: Path) -> tuple[pd.DataFrame, str]:
    if not path.exists():
        raise FileNotFoundError(f"Input Excel not found: {path}")
    if path.suffix.lower() not in {".xls", ".xlsx", ".xlsm"}:
        raise ValueError("Input must be an Excel file (.xls, .xlsx or .xlsm).")

    # The user's feeder workbook is a formatted report with title/merged rows
    # above the actual column header (Meter No is around row 3 in the shown file).
    # Detect the header row instead of assuming row 1.
    preview = pd.read_excel(path, header=None, dtype=str, nrows=20)
    header_row = None
    for i in range(len(preview)):
        vals = [str(v).strip() for v in preview.iloc[i].tolist() if pd.notna(v)]
        if find_column(vals, METER_ALIASES):
            header_row = i
            break
    if header_row is None:
        raise ValueError(
            "Excel must contain a meter-number header. Accepted examples: "
            "Meter No, Meter Number, Meter No., Meter ID. "
            "The agent also searches the first 20 rows for the header."
        )

    df = pd.read_excel(path, header=header_row, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    meter_col = find_column(df.columns, METER_ALIASES)
    if not meter_col:
        raise ValueError(
            "Excel must contain a meter-number column. Accepted examples: "
            "Meter No, Meter Number, Meter No., Meter ID."
        )

    # Preserve merged/group headers before filtering out non-meter rows. In the
    # APDCL workbook the substation name is often written only on the first
    # feeder/group row, while subsequent meter rows contain blank cells.
    # Filtering first loses that hierarchy permanently.
    feeder_col = find_column(df.columns, FEEDER_ALIASES)
    sub_col = find_column(df.columns, SUBSTATION_ALIASES)
    if feeder_col:
        df[feeder_col] = df[feeder_col].replace({"nan": "", "NaN": ""}).ffill()
    if sub_col:
        df[sub_col] = df[sub_col].replace({"nan": "", "NaN": ""}).ffill()

    df[meter_col] = df[meter_col].fillna("").astype(str).str.strip()
    df = df[df[meter_col].ne("") & df[meter_col].str.lower().ne("nan")].copy()
    df["_meter_col"] = meter_col
    if feeder_col:
        df["_feeder"] = df[feeder_col].fillna("").astype(str).str.strip()
    else:
        df["_feeder"] = ""
    if sub_col:
        df["_substation"] = df[sub_col].fillna("").astype(str).str.strip()
    else:
        df["_substation"] = ""
    df["_meter_no"] = df[meter_col]
    return df, meter_col
