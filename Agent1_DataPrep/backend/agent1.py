"""
Agent 1 — Data Preparation tool library.

Composable, independently callable functions. No orchestrator, no sys.exit,
no file discovery, no timestamped folders.

Every function returns:
    {
        "status": "ok" | "partial" | "failed",
        "data":   <DataFrame | dict | None>,
        "issues": [{"type": str, "detail": str}, ...]
    }

"ok"      — completed fully; issues may still contain informational counts.
"partial" — completed with degraded quality (missing optional input, some rows
             skipped, a column missing so a sub-step was bypassed).
"failed"  — could not produce usable output (missing required column, unreadable
             file, etc.).

Config is read from agent1_config.json next to this file. All transformation
functions also accept an explicit `config` dict that overrides file values.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Embedded constants (from fbf_plan_config.py)
# ---------------------------------------------------------------------------

_FBF_PLAN_VENDORS: tuple[str, ...] = ("ekl",)
_FBF_PLAN_DAY_START: int = 1
_FBF_PLAN_DAY_END: int = 30
_FBF_PLAN_AVG_DIVISOR: int = 30
_FBF_PLAN_MISSING_CFT_FALLBACK: float = 7.0
_SD_RETURNS_FACTOR: float = 0.22

_CORE_SC_TO_FBF_BAND: dict[str, str] = {
    "coreea": "SHA",
    "washingmachinedryer": "5SC",
    "refrigerator": "5SC",
    "homeentertainmentlarge": "5SC",
    "seasonalea": "SHA",
    "premiumea": "SHA",
    "airconditioner": "5SC",
    "microwave": "5SC",
}

_NFBF_SUFFIX_STRIP_RE = re.compile(r"_FURNITURE$|_LARGE$", re.IGNORECASE)
_MH_NUM_RE = re.compile(r"^MH(\d+)$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _ok(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data, "issues": []}

def _partial(data: Any, issues: list[dict]) -> dict[str, Any]:
    return {"status": "partial", "data": data, "issues": issues}

def _failed(issues: list[dict], data: Any = None) -> dict[str, Any]:
    return {"status": "failed", "data": data, "issues": issues}

def _issue(type_: str, detail: str) -> dict[str, str]:
    return {"type": type_, "detail": detail}

# ---------------------------------------------------------------------------
# String / column normalisation helpers
# ---------------------------------------------------------------------------

def _norm_str(s: Any) -> str:
    """Lowercase + strip + strip UTF-8 BOM."""
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return ""
    return str(s).strip().lower().lstrip("﻿")

def _norm_header_key(name: Any) -> str:
    """Like _norm_str but also collapses spaces / dashes / slashes → underscores."""
    t = _norm_str(name)
    if not t:
        return ""
    return re.sub(r"[\s\-/]+", "_", t).strip("_")

def _find_col(df: pd.DataFrame, logical: str) -> Optional[str]:
    """Return the first column whose _norm_str matches the normalized logical name."""
    want = _norm_str(logical)
    for c in df.columns:
        if _norm_str(c) == want:
            return str(c)
    return None

def _find_col_hk(df: pd.DataFrame, logical: str) -> Optional[str]:
    """Return the first column matching _norm_header_key (spaces/dashes → underscores)."""
    want = _norm_header_key(logical)
    if not want:
        return None
    for c in df.columns:
        if _norm_header_key(c) == want:
            return str(c)
    return None

def _norm_pincode(val: Any) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return ""
    try:
        return str(int(float(val))).zfill(6)
    except (TypeError, ValueError):
        s = str(val).strip().split(".")[0]
        return s.zfill(6) if s.isdigit() else s.lower()

# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def _read_file(path: Path, sheet: Optional[str] = None) -> pd.DataFrame:
    suf = path.suffix.lower()
    if suf == ".csv":
        return pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    if suf == ".xlsx":
        return pd.read_excel(path, sheet_name=sheet if sheet is not None else 0, engine="openpyxl")
    if suf == ".xlsb":
        return pd.read_excel(path, sheet_name=sheet if sheet is not None else 0, engine="pyxlsb")
    raise ValueError(f"Unsupported file format: {path.suffix!r}")

def _load_config(config: Optional[dict] = None) -> dict[str, Any]:
    """Merge agent1_config.json with caller-supplied overrides (caller wins)."""
    defaults: dict[str, Any] = {
        "default_cft_nfb": 3.5,
        "default_cft_fbf": 7.0,
        "plan_cft_fallback_alpha": 7.0,
        "plan_cft_fallback_alite": 5.0,
        "plan_cft_fallback_nfbf": 3.5,
        "lm_fdp_exclude_logistics_carriers": ["3PL"],
        "fbf_plan_vendors": list(_FBF_PLAN_VENDORS),
        "fbf_plan_day_start": _FBF_PLAN_DAY_START,
        "fbf_plan_day_end": _FBF_PLAN_DAY_END,
        "fbf_plan_avg_divisor": _FBF_PLAN_AVG_DIVISOR,
        "fbf_plan_missing_cft_fallback_cuft": _FBF_PLAN_MISSING_CFT_FALLBACK,
        "sd_returns_factor": _SD_RETURNS_FACTOR,
        "core_sc_to_fbf_band": dict(_CORE_SC_TO_FBF_BAND),
    }
    cfg_path = Path(__file__).resolve().parent / "agent1_config.json"
    if cfg_path.is_file():
        try:
            user = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                defaults.update(user)
        except (json.JSONDecodeError, OSError):
            pass
    if config:
        defaults.update(config)
    return defaults


def load_agent1_config(config_path=None):
    """
    Load agent1 config from JSON file, merging over built-in defaults.
    Returns a complete config dict. Safe to call with no arguments.
    """
    return _load_config(config_path)


# ---------------------------------------------------------------------------
# LOADING FUNCTIONS
# ---------------------------------------------------------------------------

def load_resort(path: Any) -> dict[str, Any]:
    """Read resort file; validate MH1, LMHub, PATH columns exist."""
    path = Path(path)
    try:
        df = _read_file(path)
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    missing = []
    if _find_col(df, "MH1") is None:
        missing.append("MH1")
    if _find_col(df, "LMHub") is None:
        missing.append("LMHub")
    if _find_col(df, "PATH") is None and _find_col(df, "paths") is None:
        missing.append("PATH or paths")
    if missing:
        return _failed([_issue("missing_columns", f"Resort missing: {', '.join(missing)}")])
    return _ok(df)


def load_lm_fdp(path: Any) -> dict[str, Any]:
    """Read LM FDP actuals; warn on any missing required columns (returns partial, not failed)."""
    path = Path(path)
    try:
        df = _read_file(path)
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    required = [
        "fulfill_item_service_profile",
        "order_item_unit_source_facility",
        "source_pincode",
        "customer_pincode",
        "analytic_vertical",
        "logistics_carrier",
    ]
    missing = [r for r in required if _find_col(df, r) is None]
    if missing:
        return _partial(df, [_issue("missing_columns", f"LM FDP missing: {', '.join(missing)}")])
    return _ok(df)


def load_cft_vertical(path: Any, sheet: Optional[str] = None) -> dict[str, Any]:
    """Read CFT vertical lookup; returns DataFrame with {vertical, avg_cft_cuft, vertical_norm}."""
    path = Path(path)
    try:
        raw = _read_file(path, sheet)
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    vcol = next((c for c in raw.columns if _norm_str(c) == "vert"), None)
    ncol = next((c for c in raw.columns if "vol" in _norm_str(c) and "ship" in _norm_str(c)), None)
    if vcol is None:
        return _failed([_issue("missing_columns", "CFT vertical: no 'vert' column")])
    if ncol is None:
        return _failed([_issue("missing_columns", "CFT vertical: no volume-per-shipment column")])

    out = pd.DataFrame({
        "vertical": raw[vcol].astype(str).str.strip(),
        "avg_cft_cuft": pd.to_numeric(raw[ncol], errors="coerce"),
    })
    out = out.dropna(subset=["vertical"])
    out["vertical_norm"] = out["vertical"].map(_norm_str)
    return _ok(out)


def load_fbf_day_plan(path: Any) -> dict[str, Any]:
    """Read FBF day-level plan file; validate key columns and presence of day_N columns."""
    path = Path(path)
    try:
        df = _read_file(path)
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    required = ["source", "destination_hub", "destination_pincode", "seller", "sc", "vendor"]
    lc = {_norm_str(c) for c in df.columns}
    missing = [r for r in required if _norm_str(r) not in lc]
    day_count = sum(
        1 for c in df.columns
        if _norm_str(c).startswith("day_") and _norm_str(c)[4:].isdigit()
    )

    issues: list[dict] = []
    if missing:
        issues.append(_issue("missing_columns", f"FBF day plan missing: {', '.join(missing)}"))
    if day_count == 0:
        issues.append(_issue("missing_columns", "FBF day plan has no day_N columns"))

    if issues:
        return _partial(df, issues)
    return _ok(df)


_LARGE_FILE_THRESHOLD_BYTES: int = 500 * 1024 * 1024  # 500 MB


def _read_csv_chunked_ekl(path: Path) -> pd.DataFrame:
    """Read a large CSV with per-chunk EKL vendor filter. Returns concatenated DataFrame."""
    # Detect vendor column name from header row only
    header_df = pd.read_csv(path, nrows=0, encoding="utf-8-sig")
    vendor_col: Optional[str] = None
    for c in header_df.columns:
        if _norm_str(c) == "vendor":
            vendor_col = str(c)
            break

    chunks = []
    for chunk in pd.read_csv(path, chunksize=500_000, low_memory=False, encoding="utf-8-sig"):
        if vendor_col and vendor_col in chunk.columns:
            chunk = chunk[chunk[vendor_col].astype(str).str.strip().str.lower() == "ekl"]
        if not chunk.empty:
            chunks.append(chunk)

    if not chunks:
        return pd.DataFrame(columns=list(header_df.columns))
    return pd.concat(chunks, ignore_index=True)


def load_sd_plans(
    alpha_path: Any, alite_path: Any, nfbf_path: Any
) -> dict[str, Any]:
    """Read Alpha (FBF), Alite, and NFBF SD plan files; returns data={"alpha":df,"alite":df,"nfbf":df}.

    Automatically uses chunked reading with EKL vendor pre-filter for CSV files > 500 MB.
    Caller does not need to handle large files specially.
    """
    issues: list[dict] = []
    dfs: dict[str, Optional[pd.DataFrame]] = {"alpha": None, "alite": None, "nfbf": None}

    for name, path in [("alpha", alpha_path), ("alite", alite_path), ("nfbf", nfbf_path)]:
        try:
            p = Path(path)
            if p.suffix.lower() == ".csv" and p.stat().st_size > _LARGE_FILE_THRESHOLD_BYTES:
                dfs[name] = _read_csv_chunked_ekl(p)
            else:
                dfs[name] = _read_file(p)
        except Exception as e:
            issues.append(_issue("read_error", f"{name}: {e}"))

    n_failed = sum(1 for v in dfs.values() if v is None)
    if n_failed == 3:
        return _failed(issues, data=dfs)
    if n_failed > 0 or issues:
        return _partial(dfs, issues)
    return _ok(dfs)


def load_fbf_network_pathway(path: Any, sheet: Optional[str] = None) -> dict[str, Any]:
    """Read FBF P1–P5 network pathway file (Excel with auto-detected sheet, or CSV)."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(
                path, header=0, encoding="utf-8-sig",
                low_memory=False, encoding_errors="replace",
            )
        else:
            sh = sheet
            if sh is None:
                xl = pd.ExcelFile(path, engine="openpyxl")
                for name in xl.sheet_names:
                    kn = _norm_str(name).replace(" ", "").replace("'", "")
                    if all(x in kn for x in ("p1", "p2", "comb")):
                        sh = name
                        break
                if sh is None and xl.sheet_names:
                    sh = xl.sheet_names[0]
            df = pd.read_excel(path, sheet_name=sh, header=0, engine="openpyxl")
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    cols = [str(c) for c in df.columns if c is not None]
    if not (_find_p_dc_columns(cols) and _find_p_pct_columns(cols)):
        return _partial(df, [_issue("missing_columns", "File lacks P1–P5 DC/FC + % columns")])
    return _ok(df)


def load_mh1_tagging(path: Any) -> dict[str, Any]:
    """Read MH1 source tagging file; validate MH1 + tag columns."""
    path = Path(path)
    try:
        df = _read_file(path)
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    issues: list[dict] = []
    if _find_col(df, "MH1") is None:
        issues.append(_issue("missing_columns", "MH1 tagging: no MH1 column"))
    if not any("tag" in _norm_str(c) for c in df.columns):
        issues.append(_issue("missing_columns", "MH1 tagging: no tag column"))
    if issues:
        return _failed(issues)
    return _ok(df)


def load_lm_pbh(path: Any) -> dict[str, Any]:
    """Read LM PBH (customer pincode → DH hub) file."""
    path = Path(path)
    try:
        df = _read_file(path)
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    if _find_col(df, "pincode") is None:
        return _partial(df, [_issue("missing_columns", "LM PBH: no pincode column")])
    return _ok(df)


def load_fm_pbh(path: Any) -> dict[str, Any]:
    """Read FM PBH (source pincode → MH) file."""
    path = Path(path)
    try:
        df = _read_file(path)
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    if _find_col(df, "pincode") is None:
        return _partial(df, [_issue("missing_columns", "FM PBH: no pincode column")])
    return _ok(df)


def load_fc_map(path: Any) -> dict[str, Any]:
    """Read FC / Alite facility code → hub name mapping file."""
    path = Path(path)
    try:
        df = _read_file(path)
    except Exception as e:
        return _failed([_issue("read_error", str(e))])

    issues: list[dict] = []
    if _find_col(df, "mh_code") is None:
        issues.append(_issue("missing_columns", "FC map: no mh_code column"))
    if _find_col(df, "mh_name") is None:
        issues.append(_issue("missing_columns", "FC map: no mh_name column"))
    if issues:
        return _partial(df, issues)
    return _ok(df)


# ---------------------------------------------------------------------------
# TRANSFORMATION FUNCTIONS
# ---------------------------------------------------------------------------

def parse_resort(resort_df: pd.DataFrame) -> dict[str, Any]:
    """
    Normalise resort to canonical columns; add path_hops, hop_count, last_mh,
    second_last_mh, DMH, path_terminal, lmhub_check_ok.
    Returns failed if MH1 / LMHub / PATH columns are missing.
    Reports LMHub/PATH[-1] mismatches and duplicate lanes as issues (status=partial).
    """
    df = resort_df.copy()

    mh1_col   = _find_col(df, "MH1")
    lmhub_col = _find_col(df, "LMHub")
    path_col  = _find_col(df, "PATH") or _find_col(df, "paths")
    dmh_col   = _find_col(df, "DMH")

    missing = []
    if mh1_col is None:
        missing.append("MH1")
    if lmhub_col is None:
        missing.append("LMHub")
    if path_col is None:
        missing.append("PATH/paths")
    if missing:
        return _failed([_issue("missing_columns", f"parse_resort: {', '.join(missing)}")])

    if mh1_col != "MH1":
        df["MH1"] = df[mh1_col]
    if lmhub_col != "LMHub":
        df["LMHub"] = df[lmhub_col]
    if path_col != "PATH":
        df["PATH"] = df[path_col]

    df["path_hops"] = (
        df["PATH"].astype(str).str.split(";")
        .map(lambda xs: [x.strip() for x in xs if str(x).strip()])
    )
    df["hop_count"]       = df["path_hops"].map(len)
    df["last_mh"]         = df["path_hops"].map(lambda h: h[-2] if len(h) >= 2 else None)
    df["second_last_mh"]  = df["path_hops"].map(lambda h: h[-3] if len(h) >= 4 else None)

    if dmh_col is None:
        df["DMH"] = df["last_mh"]
    elif dmh_col != "DMH":
        df["DMH"] = df[dmh_col]

    df["path_terminal"]  = df["path_hops"].map(lambda h: h[-1] if h else None)
    df["lmhub_check_ok"] = df["path_terminal"].map(_norm_str) == df["LMHub"].map(_norm_str)

    issues: list[dict] = []
    n_mismatch = int((~df["lmhub_check_ok"]).sum())
    if n_mismatch:
        issues.append(_issue("data_quality", f"LMHub vs PATH[-1] mismatches: {n_mismatch} rows"))

    key  = df["MH1"].map(_norm_str) + "\x00" + df["LMHub"].map(_norm_str)
    vc   = key.value_counts()
    n_dup = int((vc > 1).sum())
    if n_dup:
        issues.append(_issue("duplicate_lanes", f"Duplicate (MH1, LMHub) keys: {n_dup} pairs"))

    return _ok(df) if not issues else _partial(df, issues)


def tag_mh1(
    resort_df: pd.DataFrame,
    tagging_df: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """
    Add source_type and stream columns from MH1 tagging file.
    If tagging_df is None, returns resort_df unchanged (status="ok").
    Classification logic:
      - MH1 contains "centralhub_alite" → ALITE / ALITE
      - tag == "fc_mh"                  → FC_MH / FBF
      - tag == "mh"                     → MH    / NFBF
      - otherwise                       → PH    / NFBF
    """
    if tagging_df is None:
        return _ok(resort_df.copy())

    df = resort_df.copy()
    t  = tagging_df.copy()

    mh_col  = "MH1" if "MH1" in t.columns else next(
        (c for c in t.columns if _norm_str(c) == "mh1"), None
    )
    tag_col = next((c for c in t.columns if "tag" in _norm_str(c)), None)

    if mh_col is None or tag_col is None:
        return _partial(
            df, [_issue("missing_columns", "Tagging file missing MH1 or tag column; no tagging applied")]
        )

    t["_mk"] = t[mh_col].map(_norm_str)
    tag_lookup: dict[str, str] = dict(zip(t["_mk"], t[tag_col].map(_norm_str)))

    df["mh1_norm"] = df["MH1"].map(_norm_str)

    def _classify(norm: str) -> tuple[str, str]:
        if "centralhub_alite" in norm:
            return "ALITE", "ALITE"
        tv = tag_lookup.get(norm, "")
        if tv == "fc_mh":
            return "FC_MH", "FBF"
        if tv == "mh":
            return "MH", "NFBF"
        return "PH", "NFBF"

    classified = df["mh1_norm"].map(_classify)
    df["source_type"] = [x[0] for x in classified]
    df["stream"]      = [x[1] for x in classified]
    return _ok(df)


def join_demand(
    resort_tagged_df: pd.DataFrame,
    demand_df: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """
    Left-merge SD plan demand (plan_demand_sd schema) onto resort lanes.
    Handles stream/source_type overrides: SD plan overwrites FBF/ALITE lanes;
    NFBF lanes keep the tagging-file source_type (MH vs PH).
    demand_df=None returns resort with empty demand columns (status=partial).
    """
    _STREAM_SPLIT_COLS = [
        "augmedian_fbf",   "augpeak_fbf",   "plan_median_cft_fbf",   "plan_peak_cft_fbf",
        "augmedian_alite", "augpeak_alite", "plan_median_cft_alite", "plan_peak_cft_alite",
        "augmedian_nfbf",  "augpeak_nfbf",  "plan_median_cft_nfbf",  "plan_peak_cft_nfbf",
        "plan_median_cft_volume", "plan_peak_cft_volume",
        "has_mixed_streams", "minority_stream_shipments",
    ]

    df = resort_tagged_df.copy()
    issues: list[dict] = []

    if demand_df is None:
        df["augmedian"]      = np.nan
        df["augpeak"]        = np.nan
        df["newdemandret"]   = np.nan
        df["has_demand_data"] = False
        df["has_cft_anchor"]  = False
        issues.append(_issue("missing_input", "No demand_df provided; demand fields set to NaN"))
        return _partial(df, issues)

    df["_mh1j"] = df["MH1"].map(_norm_str)
    df["_lmj"]  = df["LMHub"].map(_norm_str)

    pk = demand_df.copy()
    pk["_mh1j"] = pk["MH1"].map(_norm_str)
    pk["_lmj"]  = pk["LMHub"].map(_norm_str)

    for c in ["augmedian", "augpeak", "newdemandret"]:
        if c not in pk.columns:
            pk[c] = np.nan

    _has_sd_stream = "stream" in pk.columns
    if _has_sd_stream:
        pk = pk.rename(columns={"stream": "_sd_stream", "source_type": "_sd_source_type"})

    extra_cols  = [c for c in _STREAM_SPLIT_COLS if c in pk.columns]
    sd_tag_cols = (["_sd_stream", "_sd_source_type"] if _has_sd_stream else [])
    pk_cols     = ["_mh1j", "_lmj", "augmedian", "augpeak", "newdemandret"] + extra_cols + sd_tag_cols

    agg_spec: dict[str, Any] = {
        "augmedian":    ("augmedian",    "mean"),
        "augpeak":      ("augpeak",      "mean"),
        "newdemandret": ("newdemandret", "mean"),
    }
    for ec in extra_cols:
        agg_spec[ec] = (ec, "mean")
    if _has_sd_stream:
        agg_spec["_sd_stream"] = ("_sd_stream", "first")
        if "_sd_source_type" in pk.columns:
            agg_spec["_sd_source_type"] = ("_sd_source_type", "first")

    pk_agg = (
        pk[[c for c in pk_cols if c in pk.columns]]
        .groupby(["_mh1j", "_lmj"], as_index=False)
        .agg(**agg_spec)
    )

    merged = df.merge(pk_agg, on=["_mh1j", "_lmj"], how="left", indicator=True)
    merged["has_demand_data"] = merged["_merge"] == "both"
    merged = merged.drop(columns=["_merge"])
    df = merged
    df["has_cft_anchor"] = df["has_demand_data"].fillna(False)

    # Apply SD stream tag: FBF/ALITE lanes overwritten; NFBF lanes keep tagging-file value.
    if _has_sd_stream and "_sd_stream" in df.columns:
        matched = df["has_demand_data"].fillna(False)
        df.loc[matched, "stream"] = df.loc[matched, "_sd_stream"]
        if "_sd_source_type" in df.columns:
            has_tagging = "source_type" in df.columns
            if has_tagging:
                is_nfbf    = df["_sd_stream"].astype(str).str.strip().str.upper() == "NFBF"
                overwrite  = matched & ~is_nfbf
            else:
                overwrite  = matched
            df.loc[overwrite, "source_type"] = df.loc[overwrite, "_sd_source_type"]
        df = df.drop(columns=["_sd_stream", "_sd_source_type"], errors="ignore")

    # Derive plan_returns_cft_volume when not already present.
    if "plan_returns_cft_volume" not in df.columns and "plan_peak_cft_volume" in df.columns:
        peak_nonzero             = df["augpeak"].replace(0, np.nan)
        df["plan_returns_cft_volume"] = df["newdemandret"] * (df["plan_peak_cft_volume"] / peak_nonzero)

    unmatched = int((~df["has_demand_data"].fillna(False)).sum())
    if unmatched:
        issues.append(_issue("unmatched_lanes", f"{unmatched} resort lanes had no demand match"))

    return _ok(df) if not issues else _partial(df, issues)


def build_plan_volume(
    plan_df: pd.DataFrame,
    tagging_df: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """
    Select and order final plan volume columns; filter rows to has_demand_data == True.
    Omits stream/source_type when tagging was not applied.
    Omits plan CFT columns when no lane has a CFT anchor.

    tagging_df: optional MH1-tagging file (output of load_mh1_tagging["data"]).
      When provided, source_type for NFBF rows is re-classified from the tagging file
      instead of the stream-derived default ("MH"), preserving "PH" for hubs that are
      not in the tagging file. Without tagging_df, behaviour is unchanged.
      Classification: fc_mh tag → FC_MH, mh tag → MH, no tag → PH, alite in name → ALITE.
    """
    out = plan_df.copy()
    out["median_demand_shipments"] = out.get("augmedian",  pd.Series(np.nan, index=out.index))
    out["peak_demand_shipments"]   = out.get("augpeak",    pd.Series(np.nan, index=out.index))

    # Collect intermediate MH columns (MH2, MH3, …).
    by_n: dict[int, str] = {}
    for c in out.columns:
        m = _MH_NUM_RE.match(str(c).strip())
        if m:
            n = int(m.group(1))
            if n >= 2 and n not in by_n:
                by_n[n] = c
    mh_mid = [f"MH{n}" for n in sorted(by_n)]

    include_tagging = "source_type" in out.columns and "stream" in out.columns
    include_cft = (
        bool(out["has_cft_anchor"].fillna(False).any())
        if "has_cft_anchor" in out.columns else False
    )

    head = ["MH1"] + mh_mid
    mid  = ["LMHub"]
    if include_tagging:
        mid += ["source_type", "stream"]
    mid += ["hop_count", "last_mh", "second_last_mh", "DMH",
            "median_demand_shipments", "peak_demand_shipments"]
    if include_cft:
        cft_base = [
            "plan_median_cft_volume", "plan_peak_cft_volume",
            "plan_returns_cft_volume", "has_cft_anchor",
        ]
        sd_split = [
            "augmedian_fbf",   "augpeak_fbf",   "plan_median_cft_fbf",   "plan_peak_cft_fbf",
            "augmedian_alite", "augpeak_alite", "plan_median_cft_alite", "plan_peak_cft_alite",
            "augmedian_nfbf",  "augpeak_nfbf",  "plan_median_cft_nfbf",  "plan_peak_cft_nfbf",
            "has_mixed_streams", "minority_stream_shipments",
        ]
        mid += cft_base + [c for c in sd_split if c in out.columns]
    tail = ["has_demand_data"]

    ordered = [c for c in head + mid + tail if c in out.columns]
    out = out[ordered]

    if "has_demand_data" in out.columns:
        mask = out["has_demand_data"].fillna(False).astype(bool)
        out  = out.loc[mask].copy()

    # Re-classify source_type for NFBF rows using tagging file so PH hubs are not
    # silently promoted to "MH" by the stream-based default in build_sd_plan_aggregate.
    if (
        tagging_df is not None
        and include_tagging
        and "MH1" in out.columns
        and "stream" in out.columns
    ):
        t = tagging_df.copy()
        mh_col = "MH1" if "MH1" in t.columns else next(
            (c for c in t.columns if _norm_str(c) == "mh1"), None
        )
        tag_col = next((c for c in t.columns if "tag" in _norm_str(c)), None)
        if mh_col and tag_col:
            t["_mk"] = t[mh_col].map(_norm_str)
            tag_lookup: dict[str, str] = dict(zip(t["_mk"], t[tag_col].map(_norm_str)))

            def _classify(norm: str) -> str:
                if "centralhub_alite" in norm:
                    return "ALITE"
                tv = tag_lookup.get(norm, "")
                if tv == "fc_mh":
                    return "FC_MH"
                if tv == "mh":
                    return "MH"
                return "PH"

            nfbf_mask = out["stream"] == "NFBF"
            if nfbf_mask.any():
                out.loc[nfbf_mask, "source_type"] = (
                    out.loc[nfbf_mask, "MH1"].map(_norm_str).map(_classify)
                )

    return _ok(out)


def filter_actuals(
    fdp_df: pd.DataFrame,
    exclude_carriers: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Apply carrier exclusion (default ["3PL"]) and drop blank service_profile rows.
    Reports counts in issues. status="ok" even if rows are dropped (expected behaviour).
    status="partial" if a required column is missing so a step was skipped.
    """
    if exclude_carriers is None:
        exclude_carriers = ["3PL"]

    df     = fdp_df.copy()
    issues: list[dict] = []
    has_structural = False

    lc_col = _find_col(df, "logistics_carrier")
    if lc_col is None:
        issues.append(_issue("missing_columns", "logistics_carrier not found; carrier filter skipped"))
        has_structural = True
    else:
        ex_set = {str(x).strip().upper() for x in exclude_carriers if str(x).strip()}
        s      = df[lc_col].fillna("").astype(str).str.strip().str.upper()
        excl   = s.isin(ex_set)
        n_excl = int(excl.sum())
        df     = df.loc[~excl].copy()
        if n_excl:
            issues.append(_issue("rows_excluded", f"Dropped {n_excl} rows with carrier in {exclude_carriers}"))

    prof_col = _find_col(df, "fulfill_item_service_profile")
    if prof_col is None:
        issues.append(_issue("missing_columns", "fulfill_item_service_profile not found; profile filter skipped"))
        has_structural = True
    else:
        blank  = (
            df[prof_col].isna()
            | (df[prof_col].astype(str).str.strip() == "")
            | (df[prof_col].astype(str).str.upper() == "NAN")
        )
        n_blank = int(blank.sum())
        df      = df.loc[~blank].copy()
        if n_blank:
            issues.append(_issue("rows_excluded", f"Dropped {n_blank} rows with blank service_profile"))

    if has_structural:
        return _partial(df, issues)
    return _ok(df) if not issues else _ok(df)  # row-count issues are informational, not degraded


def resolve_destinations(
    fdp_df: pd.DataFrame,
    lm_pbh_df: Optional[pd.DataFrame],
) -> dict[str, Any]:
    """
    Map customer_pincode → DH name via LM PBH lookup.
    Unresolved rows get destination = "UNKNOWN_DH_<pincode>".
    Reports unresolved count in issues (status=partial when lm_pbh_df is None or any unresolved).
    """
    df = fdp_df.copy()
    df["_cust_pc"]  = df["customer_pincode"].map(_norm_pincode)
    df["destination"] = ""
    issues: list[dict] = []

    if lm_pbh_df is None:
        df["destination"]          = df["_cust_pc"].map(
            lambda p: f"UNKNOWN_DH_{p}" if p else "UNKNOWN_DH_"
        )
        df["destination_resolved"] = False
        n = len(df)
        issues.append(_issue("missing_input", f"No lm_pbh_df; all {n} destinations unresolved"))
        return _partial(df, issues)

    pb = lm_pbh_df.copy()
    pcol  = next((c for c in pb.columns if _norm_str(c) == "pincode"), None)
    hub_col = next(
        (
            c for c in pb.columns
            if "hub" in _norm_str(c)
            and "type" not in _norm_str(c)
            and "state" not in _norm_str(c)
            and "city" not in _norm_str(c)
        ),
        None,
    )
    if pcol is None or hub_col is None:
        df["destination"]          = df["_cust_pc"].map(
            lambda p: f"UNKNOWN_DH_{p}" if p else "UNKNOWN_DH_"
        )
        df["destination_resolved"] = False
        issues.append(_issue("missing_columns", "LM PBH missing pincode or hub column; all unresolved"))
        return _partial(df, issues)

    pb["_pc"]      = pb[pcol].map(_norm_pincode)
    pb["_is_dh"]   = pb[hub_col].astype(str).str.contains(r"(?i)satellite|bulk")
    pbh_map        = pb.sort_values("_is_dh", ascending=False).drop_duplicates("_pc", keep="first")
    mp             = dict(zip(pbh_map["_pc"], pbh_map[hub_col].astype(str)))

    df["destination"] = df["_cust_pc"].map(
        lambda pc: mp.get(pc, f"UNKNOWN_DH_{pc}") if pc else "UNKNOWN_DH_"
    )
    n_unres = int(df["destination"].str.startswith("UNKNOWN_DH_").sum())
    df["destination_resolved"] = ~df["destination"].astype(str).str.upper().str.startswith("UNKNOWN_DH_")

    if n_unres:
        issues.append(_issue("unresolved_destinations", f"{n_unres} rows have UNKNOWN_DH_"))
    return _ok(df) if not issues else _partial(df, issues)


def resolve_sources(
    fdp_df: pd.DataFrame,
    fm_pbh_df: Optional[pd.DataFrame],
    fc_map_df: Optional[pd.DataFrame],
    resort_df: Optional[pd.DataFrame],
) -> dict[str, Any]:
    """
    Classify rows as NFBF / FBF / ALITE / MFC and map facilities to hub names.
    Unresolved rows get UNKNOWN_PH_ / UNKNOWN_FC_ / UNKNOWN_ALITE_ / UNKNOWN_MFC tokens.
    resort_df (parsed, with last_mh / second_last_mh) used for MFC city resolution.
    Reports UNKNOWN_ counts per stream in issues.
    """
    df = fdp_df.copy().reset_index(drop=True)
    issues: list[dict] = []

    df["_fac_lower"] = df["order_item_unit_source_facility"].astype(str).str.lower()
    df["_prof"]      = df["fulfill_item_service_profile"].astype(str).str.upper()
    df["_spc"]       = df["source_pincode"].map(_norm_pincode)
    df["_dest_norm"] = df.get("destination", pd.Series("", index=df.index)).map(_norm_str)

    # FM PBH: source pincode → MH name for NFBF
    fm_map: dict[str, str] = {}
    if fm_pbh_df is not None:
        pb   = fm_pbh_df.copy()
        pcol = next((c for c in pb.columns if _norm_str(c) == "pincode"), None)
        mh_col = next(
            (
                c for c in pb.columns
                if ("associated" in _norm_str(c) and "mh" in _norm_str(c))
                or ("central" in _norm_str(c) and "hub" in _norm_str(c))
            ),
            None,
        )
        if pcol and mh_col:
            pb["_pc"] = pb[pcol].map(_norm_pincode)
            fm_map    = dict(zip(pb["_pc"], pb[mh_col].astype(str)))

    # FC map: facility code → hub name for FBF / Alite
    fc_by_code: dict[str, str] = {}
    if fc_map_df is not None:
        fc       = fc_map_df.copy()
        code_col = next((c for c in fc.columns if _norm_str(c) == "mh_code"), None)
        name_col = next((c for c in fc.columns if _norm_str(c) == "mh_name"), None)
        if not code_col:
            code_col = next((c for c in fc.columns if "code" in _norm_str(c)), None)
        if not name_col:
            name_col = next((c for c in fc.columns if "name" in _norm_str(c)), None)
        if code_col and name_col:
            fc["_ck"] = fc[code_col].astype(str).str.lower().str.strip()
            fc_by_code = dict(zip(fc["_ck"], fc[name_col].astype(str)))

    # Resort → dominant last_mh per LMHub (for MFC city resolution)
    resort_lmhub = pd.DataFrame(columns=["_lm_norm", "last_mh", "second_last_mh"])
    if resort_df is not None and "last_mh" in resort_df.columns:
        resort_lmhub = _build_resort_lmhub_dominant(resort_df)

    df["stream"]       = ""
    df["source_type"]  = ""
    df["source"]       = ""

    nfb = df["_prof"] != "FBF"
    df.loc[nfb, "stream"]      = "NFBF"
    df.loc[nfb, "source_type"] = "PH"
    df.loc[nfb, "source"] = df.loc[nfb, "_spc"].map(
        lambda p: fm_map.get(p, f"UNKNOWN_PH_{p}") if p else "UNKNOWN_PH_"
    )

    fbf       = ~nfb
    fac       = df["_fac_lower"]
    mfc_m     = fbf & fac.str.contains("_al_mcr_",  na=False)
    alite_m   = fbf & fac.str.contains("_al_", na=False) & ~fac.str.contains("_al_mcr_", na=False)
    fc_m      = fbf & ~mfc_m & ~alite_m

    if len(resort_lmhub) > 0:
        rsub = resort_lmhub.rename(columns={"_lm_norm": "_dest_norm"})
        df   = df.merge(rsub, on="_dest_norm", how="left", suffixes=("", "_r"))
    else:
        df["last_mh"]        = np.nan
        df["second_last_mh"] = np.nan

    # MFC
    df.loc[mfc_m, "stream"]      = "FBF"
    df.loc[mfc_m, "source_type"] = "MFC"
    if mfc_m.any():
        lm_ser = df.loc[mfc_m, "last_mh"]
        sl_ser = df.loc[mfc_m, "second_last_mh"]
        v_mfc  = np.vectorize(_mfc_city_source, otypes=[object])
        mfc_src = np.asarray(v_mfc(lm_ser.values, sl_ser.values), dtype=object)
        bad     = (mfc_src == "") | pd.isna(mfc_src)
        mfc_src = np.where(bad, "UNKNOWN_MFC", mfc_src)
        df.loc[mfc_m, "source"] = mfc_src

    raw_fac    = df["order_item_unit_source_facility"].astype(str)
    fc_lookup  = fac.map(lambda x: fc_by_code.get(str(x).strip(), np.nan))

    # Alite
    df.loc[alite_m, "stream"]      = "ALITE"
    df.loc[alite_m, "source_type"] = "ALITE"
    al_name = fc_lookup[alite_m]
    df.loc[alite_m, "source"] = np.where(
        al_name.notna(), al_name,
        "UNKNOWN_ALITE_" + raw_fac[alite_m].astype(str),
    )

    # FBF FC
    df.loc[fc_m, "stream"]      = "FBF"
    df.loc[fc_m, "source_type"] = "FC"
    fc_name = fc_lookup[fc_m]
    df.loc[fc_m, "source"] = np.where(
        fc_name.notna(), fc_name,
        "UNKNOWN_FC_" + raw_fac[fc_m].astype(str),
    )

    df["source_resolved"] = ~df["source"].astype(str).str.contains("UNKNOWN_", na=False)

    def _cnt(mask: pd.Series) -> tuple[int, int]:
        sub = df.loc[mask, "source"].astype(str)
        un  = sub.str.contains("UNKNOWN_", na=False)
        return int((~un).sum()), int(un.sum())

    for label, mask in [("NFBF", nfb), ("FC", fc_m), ("MFC", mfc_m), ("ALITE", alite_m)]:
        res, unres = _cnt(mask)
        if unres:
            issues.append(_issue("unresolved_sources", f"{label}: {unres} unresolved of {res + unres}"))

    return _ok(df) if not issues else _partial(df, issues)


def compute_cft(
    fdp_df: pd.DataFrame,
    cft_lookup_df: pd.DataFrame,
    config: Optional[dict] = None,
) -> dict[str, Any]:
    """
    Join CFT vertical lookup onto FDP rows; apply stream-based defaults for misses.
    cft_lookup_df is the output of load_cft_vertical (has vertical_norm, avg_cft_cuft).
    Adds shipment_cft and cft_origin columns. Reports miss count in issues.
    """
    cfg     = _load_config(config)
    d_nfb   = float(cfg.get("default_cft_nfb", 3.5))
    d_fbf   = float(cfg.get("default_cft_fbf", 7.0))

    df      = fdp_df.copy()
    lu      = cft_lookup_df.copy()

    mp: dict[str, float] = {}
    for _, row in lu.iterrows():
        k = str(row.get("vertical_norm", _norm_str(row.get("vertical", ""))))
        v = row.get("avg_cft_cuft")
        if k and pd.notna(v):
            mp[k] = float(v)

    df["_vert_norm"]  = df["analytic_vertical"].map(_norm_str)
    prof              = df["fulfill_item_service_profile"].astype(str).str.upper()
    looked            = df["_vert_norm"].map(lambda k: mp.get(k))
    miss              = looked.isna()
    is_nfb            = prof == "NON_FBF"
    is_fbf            = prof == "FBF"

    df["shipment_cft"] = looked.astype(float)
    df.loc[miss & is_nfb, "shipment_cft"] = d_nfb
    df.loc[miss & is_fbf, "shipment_cft"] = d_fbf

    df["cft_origin"] = np.where(
        ~miss, "lookup",
        np.where(is_nfb, "default_nfb",
        np.where(is_fbf, "default_fbf", "still_null"))
    )
    still_null = df["shipment_cft"].isna()
    df.loc[still_null, "cft_origin"] = "still_null"

    issues: list[dict] = []
    n_miss = int(miss.sum())
    if n_miss:
        issues.append(_issue("cft_miss", f"{n_miss} rows had no vertical match; defaults applied"))
    n_null = int(still_null.sum())
    if n_null:
        issues.append(_issue("cft_null", f"{n_null} rows still have null CFT (unexpected profile)"))

    return _ok(df) if not issues else _partial(df, issues)


def aggregate_actuals(fdp_df: pd.DataFrame) -> dict[str, Any]:
    """
    Aggregate FDP into granular (source×dest×stream×vertical) and rollup (source×dest×stream).
    Returns data={"granular": df, "rollup": df}.
    """
    keys_g = ["source", "destination", "stream", "source_type", "analytic_vertical"]
    keys_r = ["source", "destination", "stream", "source_type"]

    g_n = fdp_df.groupby(keys_g, dropna=False).size().reset_index(name="actual_shipments")
    g_c = fdp_df.groupby(keys_g, dropna=False)["shipment_cft"].sum().reset_index(name="actual_cft")
    gran = g_n.merge(g_c, on=keys_g)

    r_n  = fdp_df.groupby(keys_r, dropna=False).size().reset_index(name="actual_shipments")
    r_c  = fdp_df.groupby(keys_r, dropna=False)["shipment_cft"].sum().reset_index(name="actual_cft")
    roll = r_n.merge(r_c, on=keys_r)

    return _ok({"granular": gran, "rollup": roll})


# ---------------------------------------------------------------------------
# Pre-flight column validators (Gap 3 fix)
# ---------------------------------------------------------------------------

def validate_fbf_plan_columns(fbf_day_plan_path: Any, day_start: int, day_end: int) -> dict[str, Any]:
    """
    Read header only (nrows=0) of FBF day plan file. Verify required columns and day range.
    Call before build_fbf_aggregate (path mode) to fail fast before streaming a large file.

    Returns standard result dict:
      status="ok"      — all required columns and day columns present
      status="partial" — required columns present but some day_N columns missing in requested range
      status="failed"  — one or more required columns absent
    """
    required = {"source", "destination_hub", "destination_pincode", "seller", "sc", "vendor"}
    issues: list[dict] = []
    try:
        head = pd.read_csv(Path(fbf_day_plan_path), nrows=0, encoding="utf-8-sig")
    except Exception as e:
        return _failed([_issue("read_error", f"Cannot read FBF day plan header: {e}")])

    present = {_norm_str(c) for c in head.columns}
    missing_req = sorted(required - present)
    if missing_req:
        return _failed([_issue("missing_columns",
            f"FBF day plan missing required columns: {missing_req}")])

    want_days = {f"day_{i}" for i in range(day_start, day_end + 1)}
    missing_days = sorted(want_days - present, key=lambda x: int(x.split("_")[1]))
    if missing_days:
        issues.append(_issue("missing_columns",
            f"FBF day plan missing day columns for window day_{day_start}..day_{day_end}: "
            f"{missing_days[:5]}{'...' if len(missing_days) > 5 else ''}"))
        return _partial({"columns": list(head.columns)}, issues)

    return _ok({"columns": list(head.columns)})


def validate_sd_plan_columns(
    alpha_path: Any,
    alite_path: Any,
    nfbf_path: Any,
    day_start: int,
    day_end: int,
) -> dict[str, Any]:
    """
    Read headers only (nrows=0) of all three SD plan files. Verify required columns and day range.
    Call before build_sd_plan_aggregate (path mode) to fail fast before streaming large files.

    Returns standard result dict:
      status="ok"      — all files pass all checks
      status="partial" — required columns present but some day_N columns missing in range
      status="failed"  — one or more required columns absent in any file
    """
    stream_specs = {
        "alpha": (alpha_path, {"source", "destination_hub", "sc", "vendor"}),
        "alite": (alite_path, {"source", "sc", "vendor"}),   # hub OR destination_hub accepted
        "nfbf":  (nfbf_path,  {"source", "destination_hub", "vertical", "vendor"}),
    }
    want_days = {f"day_{i}" for i in range(day_start, day_end + 1)}
    issues: list[dict] = []
    failed = False

    for stream, (path, required) in stream_specs.items():
        try:
            head = pd.read_csv(Path(path), nrows=0, encoding="utf-8-sig")
        except Exception as e:
            issues.append(_issue("read_error", f"{stream}: cannot read header: {e}"))
            failed = True
            continue

        present = {_norm_str(c) for c in head.columns}

        # Alite accepts hub OR destination_hub as destination column
        check_req = required.copy()
        if stream == "alite" and ("hub" in present or "destination_hub" in present):
            pass  # at least one dest column present
        elif stream == "alite":
            issues.append(_issue("missing_columns", "alite: missing both 'hub' and 'destination_hub'"))
            failed = True

        missing_req = sorted(check_req - present) if stream != "alite" else sorted((check_req - {"hub", "destination_hub"}) - present)
        if missing_req:
            issues.append(_issue("missing_columns",
                f"{stream}: missing required columns: {missing_req}"))
            failed = True

        missing_days = sorted(want_days - present, key=lambda x: int(x.split("_")[1]))
        if missing_days:
            issues.append(_issue("missing_columns",
                f"{stream}: missing day columns for window day_{day_start}..day_{day_end}: "
                f"{missing_days[:5]}{'...' if len(missing_days) > 5 else ''}"))
            failed = True  # treat missing day columns as failed — will produce empty result

    if failed:
        return _failed(issues)
    return _ok({}) if not issues else _partial({}, issues)


def build_fbf_aggregate(
    fbf_day_plan_df: Optional[pd.DataFrame] = None,
    top266_df: Optional[pd.DataFrame] = None,
    cft_vertical_df: Optional[pd.DataFrame] = None,
    config: Optional[dict] = None,
    fbf_day_plan_path: Optional[Any] = None,
    chunksize: int = 80_000,
) -> dict[str, Any]:
    """
    Aggregate FBF day-level plan to DH-level avg daily loads + CFT volumes by 5SC/SHA band
    and Top16/Next50/Next200 pincode tiers.

    Two calling modes:
      Path mode (recommended for large files):
        build_fbf_aggregate(fbf_day_plan_path=..., top266_df=..., cft_vertical_df=..., ...)
        Streams file in 80k-row chunks, loading only needed columns (usecols). Vendor+seller
        filter applied per chunk, immediate groupby reduce per chunk. Peak memory = one chunk.

      DataFrame mode (small files or pre-loaded data):
        build_fbf_aggregate(fbf_day_plan_df=..., top266_df=..., cft_vertical_df=..., ...)

    top266_df: raw DataFrame with pincode + "Final Mapping" columns (from pd.read_csv).
    cft_vertical_df: output of load_cft_vertical["data"] (has vertical_norm, avg_cft_cuft).

    Day window must be set by caller via config before calling this function. See AGENT1.md W5.
    """
    if top266_df is None:
        return _failed([_issue("missing_input", "top266_df is required")])
    if cft_vertical_df is None:
        return _failed([_issue("missing_input", "cft_vertical_df is required")])
    if fbf_day_plan_df is None and fbf_day_plan_path is None:
        return _failed([_issue("missing_input", "provide fbf_day_plan_df or fbf_day_plan_path")])

    cfg            = _load_config(config)
    day_start      = int(cfg.get("fbf_plan_day_start", _FBF_PLAN_DAY_START))
    day_end        = int(cfg.get("fbf_plan_day_end",   _FBF_PLAN_DAY_END))
    avg_divisor    = float(cfg.get("fbf_plan_avg_divisor", _FBF_PLAN_AVG_DIVISOR))
    vendors        = {v.lower() for v in cfg.get("fbf_plan_vendors", list(_FBF_PLAN_VENDORS))}
    missing_cft    = float(cfg.get("fbf_plan_missing_cft_fallback_cuft", _FBF_PLAN_MISSING_CFT_FALLBACK))
    sc_band_map: dict[str, str] = cfg.get("core_sc_to_fbf_band", dict(_CORE_SC_TO_FBF_BAND))

    issues: list[dict] = []

    # Build pincode → tier map (always from DataFrame — top266 is small)
    pcol = next((c for c in top266_df.columns if _norm_str(c) == "pincode"), None)
    tcol = next((c for c in top266_df.columns if "final" in _norm_str(c) and "mapping" in _norm_str(c)), None)
    if pcol is None or tcol is None:
        issues.append(_issue("missing_columns", "top266_df missing pincode or Final Mapping column"))
        pin_tier: pd.Series = pd.Series(dtype=str)
    else:
        t266 = top266_df[[pcol, tcol]].copy()
        t266["_pin"]  = pd.to_numeric(t266[pcol], errors="coerce").fillna(0).astype(np.int64).astype(str)
        t266["_tier"] = t266[tcol].astype(str).str.strip()
        pin_tier = t266.drop_duplicates("_pin").set_index("_pin")["_tier"]

    # Build sc → avg_cft map (always from DataFrame — CFT vertical is small)
    cft_by_sc: dict[str, float] = {}
    if "vertical_norm" in cft_vertical_df.columns and "avg_cft_cuft" in cft_vertical_df.columns:
        for _, row in cft_vertical_df.iterrows():
            k = str(row["vertical_norm"])
            v = row["avg_cft_cuft"]
            if k and pd.notna(v):
                cft_by_sc[k] = float(v)

    sum_cols = [
        "load_all", "cft_vol_all",
        "load_5sc_core", "cft_vol_5sc_core", "load_sha_core", "cft_vol_sha_core",
        "load_5sc_top16", "cft_vol_5sc_top16", "load_sha_top16", "cft_vol_sha_top16",
        "load_5sc_next50", "cft_vol_5sc_next50", "load_sha_next50", "cft_vol_sha_next50",
        "load_5sc_next200", "cft_vol_5sc_next200", "load_sha_next200", "cft_vol_sha_next200",
        "rows", "rows_missing_cft", "rows_non_core_sc",
    ]

    rename = {
        "load_all":            "fbf_avg_daily_shipments_all",
        "cft_vol_all":         "cft_cuft_day_avg_all",
        "load_5sc_core":       "fbf_avg_daily_shipments_5sc_core",
        "cft_vol_5sc_core":    "cft_cuft_day_avg_5sc_core",
        "load_sha_core":       "fbf_avg_daily_shipments_sha_core",
        "cft_vol_sha_core":    "cft_cuft_day_avg_sha_core",
        "load_5sc_top16":      "fbf_avg_daily_5sc_top16_pin",
        "cft_vol_5sc_top16":   "cft_cuft_day_avg_5sc_top16_pin",
        "load_sha_top16":      "fbf_avg_daily_sha_top16_pin",
        "cft_vol_sha_top16":   "cft_cuft_day_avg_sha_top16_pin",
        "load_5sc_next50":     "fbf_avg_daily_5sc_next50_pin",
        "cft_vol_5sc_next50":  "cft_cuft_day_avg_5sc_next50_pin",
        "load_sha_next50":     "fbf_avg_daily_sha_next50_pin",
        "cft_vol_sha_next50":  "cft_cuft_day_avg_sha_next50_pin",
        "load_5sc_next200":    "fbf_avg_daily_5sc_next200_pin",
        "cft_vol_5sc_next200": "cft_cuft_day_avg_5sc_next200_pin",
        "load_sha_next200":    "fbf_avg_daily_sha_next200_pin",
        "cft_vol_sha_next200": "cft_cuft_day_avg_sha_next200_pin",
        "rows":                "source_rows_aggregated",
        "rows_missing_cft":    "rows_missing_cft_vertical",
        "rows_non_core_sc":    "rows_sc_outside_core_5sc_sha",
    }

    def _transform_and_reduce(df: pd.DataFrame, day_cols: list[str]) -> Optional[pd.DataFrame]:
        """Apply vendor+seller filter, compute split columns, group by destination_hub."""
        vendor_col = next((c for c in df.columns if _norm_str(c) == "vendor"), None)
        seller_col = next((c for c in df.columns if _norm_str(c) == "seller"), None)
        if vendor_col:
            df = df.loc[df[vendor_col].astype(str).str.strip().str.lower().isin(vendors)].copy()
        if seller_col:
            df = df.loc[df[seller_col].astype(str).str.strip().str.lower() == "fbf"].copy()
        if df.empty:
            return None

        sc_col  = next((c for c in df.columns if _norm_str(c) == "sc"), None)
        pin_col = next((c for c in df.columns if _norm_str(c) == "destination_pincode"), None)
        hub_col = next((c for c in df.columns if _norm_str(c) == "destination_hub"), None)

        df["_sc"]   = df[sc_col].map(_norm_str) if sc_col else ""
        df["_band"] = df["_sc"].map(lambda k: sc_band_map.get(k))

        if pin_col:
            df["_pin"]  = pd.to_numeric(df[pin_col], errors="coerce").fillna(0).astype(np.int64).astype(str)
            df["_tier"] = df["_pin"].map(pin_tier) if len(pin_tier) else np.nan
        else:
            df["_tier"] = np.nan

        day_present = [c for c in day_cols if c in df.columns]
        day_vals = df[day_present].apply(pd.to_numeric, errors="coerce").fillna(0)
        df["_avg_daily"] = day_vals.sum(axis=1) / avg_divisor
        df["_cft"]       = df["_sc"].map(lambda k: cft_by_sc.get(k, np.nan))
        df["_cft_vol"]   = df["_avg_daily"] * df["_cft"].fillna(missing_cft)

        is_5   = df["_band"] == "5SC"
        is_sha = df["_band"] == "SHA"
        t16    = df["_tier"] == "Top 16"
        n50    = df["_tier"] == "Next 50"
        n200   = df["_tier"] == "Next 200"

        df["load_all"]    = df["_avg_daily"]
        df["cft_vol_all"] = df["_cft_vol"]
        df["load_5sc_core"]    = np.where(is_5,   df["_avg_daily"], 0.0)
        df["cft_vol_5sc_core"] = np.where(is_5,   df["_cft_vol"],   0.0)
        df["load_sha_core"]    = np.where(is_sha, df["_avg_daily"], 0.0)
        df["cft_vol_sha_core"] = np.where(is_sha, df["_cft_vol"],   0.0)
        for tier_name, tier_m in (("top16", t16), ("next50", n50), ("next200", n200)):
            df[f"load_5sc_{tier_name}"]    = np.where(is_5   & tier_m, df["_avg_daily"], 0.0)
            df[f"cft_vol_5sc_{tier_name}"] = np.where(is_5   & tier_m, df["_cft_vol"],   0.0)
            df[f"load_sha_{tier_name}"]    = np.where(is_sha & tier_m, df["_avg_daily"], 0.0)
            df[f"cft_vol_sha_{tier_name}"] = np.where(is_sha & tier_m, df["_cft_vol"],   0.0)
        df["rows"]             = 1
        df["rows_missing_cft"] = df["_cft"].isna().astype(np.int64)
        df["rows_non_core_sc"] = df["_band"].isna().astype(np.int64)

        dh = hub_col or "destination_hub"
        if dh not in df.columns:
            return None
        df[dh] = df[dh].astype(str)
        return df.groupby(dh, as_index=False)[sum_cols].sum(numeric_only=True)

    # ── Path mode: chunked streaming with usecols ─────────────────────────────
    if fbf_day_plan_path is not None:
        p = Path(fbf_day_plan_path)
        try:
            head = pd.read_csv(p, nrows=0, encoding="utf-8-sig")
        except Exception as e:
            return _failed([_issue("read_error", f"Cannot read FBF day plan: {e}")])

        day_cols = sorted(
            [c for c in head.columns
             if _norm_str(c).startswith("day_") and _norm_str(c)[4:].isdigit()
             and day_start <= int(_norm_str(c)[4:]) <= day_end],
            key=lambda c: int(_norm_str(c)[4:]),
        )
        if not day_cols:
            return _failed([_issue("missing_columns",
                f"No day_{day_start}..day_{day_end} columns in {p.name}")])

        usecols = [c for c in ["source", "destination_hub", "destination_pincode",
                                "seller", "sc", "vendor"] if c in head.columns] + day_cols
        missing_req = [c for c in ["source", "destination_hub", "seller", "sc", "vendor"]
                       if c not in head.columns]
        if missing_req:
            return _failed([_issue("missing_columns",
                f"FBF day plan missing required columns: {missing_req}")])

        partials: list[pd.DataFrame] = []
        for chunk in pd.read_csv(p, usecols=usecols, chunksize=chunksize, low_memory=False, encoding="utf-8-sig"):
            reduced = _transform_and_reduce(chunk, day_cols)
            if reduced is not None and not reduced.empty:
                partials.append(reduced)

        if not partials:
            return _partial(pd.DataFrame(columns=["destination_hub"]),
                            issues + [_issue("empty_result", "No rows after vendor/seller filter")])

        stacked = pd.concat(partials, ignore_index=True)
        agg = stacked.groupby("destination_hub", as_index=False)[sum_cols].sum().sort_values("destination_hub")
        export = agg.rename(columns=rename)
        return _ok(export) if not issues else _partial(export, issues)

    # ── DataFrame mode ────────────────────────────────────────────────────────
    day_cols = sorted(
        [c for c in fbf_day_plan_df.columns
         if _norm_str(c).startswith("day_") and _norm_str(c)[4:].isdigit()
         and day_start <= int(_norm_str(c)[4:]) <= day_end],
        key=lambda c: int(_norm_str(c)[4:]),
    )
    if not day_cols:
        return _failed([_issue("missing_columns",
            f"No day_{day_start}..day_{day_end} columns in FBF day plan")])

    reduced = _transform_and_reduce(fbf_day_plan_df, day_cols)
    if reduced is None or reduced.empty:
        return _partial(pd.DataFrame(columns=["destination_hub"]),
                        issues + [_issue("empty_result", "No rows after vendor/seller filter")])

    agg = reduced.groupby("destination_hub", as_index=False)[sum_cols].sum().sort_values("destination_hub")
    export = agg.rename(columns=rename)
    return _ok(export) if not issues else _partial(export, issues)


def build_sd_plan_aggregate(
    alpha_df=None,
    alite_df=None,
    nfbf_df=None,
    alpha_path=None,
    alite_path=None,
    nfbf_path=None,
    mh_dh_mapping_df: Optional[pd.DataFrame] = None,
    cft_vertical_df: Optional[pd.DataFrame] = None,
    config: Optional[dict] = None,
    chunksize: int = 100_000,
) -> dict[str, Any]:
    """
    Combine Alpha (FBF) + Alite + NFBF SD plan data into one (MH1 × LMHub) demand table.

    Two calling modes — mix and match per stream:
      Path mode (recommended for large files):
        build_sd_plan_aggregate(alpha_path=..., alite_path=..., nfbf_path=..., ...)
        Streams each file in chunks of `chunksize` rows. EKL filter and groupby reduction
        applied per chunk — peak memory = one raw chunk + accumulated small aggregates.
        Handles the 39 GB NFBF file without OOM.

      DataFrame mode (for pre-loaded data or small files):
        build_sd_plan_aggregate(alpha_df=..., alite_df=..., nfbf_df=..., ...)
        Processes full DataFrames in memory. Equivalent to original behaviour.

    mh_dh_mapping_df: raw DataFrame with DC/PH → MH-1 columns (from pd.read_csv).
    cft_vertical_df: output of load_cft_vertical["data"].
    Output columns match plan_demand_sd.csv schema (augmedian, augpeak, stream, CFT split, etc.).

    Day window must be set by caller via config before calling this function. See AGENT1.md W5.
    """
    cfg            = _load_config(config)
    day_start      = int(cfg.get("fbf_plan_day_start",    _FBF_PLAN_DAY_START))
    day_end        = int(cfg.get("fbf_plan_day_end",      _FBF_PLAN_DAY_END))
    avg_divisor    = float(cfg.get("fbf_plan_avg_divisor", _FBF_PLAN_AVG_DIVISOR))
    alpha_fallback = float(cfg.get("plan_cft_fallback_alpha", 7.0))
    alite_fallback = float(cfg.get("plan_cft_fallback_alite", 5.0))
    nfbf_fallback  = float(cfg.get("plan_cft_fallback_nfbf",  3.5))
    returns_factor = float(cfg.get("sd_returns_factor",   _SD_RETURNS_FACTOR))
    vendors        = {v.lower() for v in cfg.get("fbf_plan_vendors", list(_FBF_PLAN_VENDORS))}

    issues: list[dict] = []

    if mh_dh_mapping_df is None:
        return _failed([_issue("missing_input", "mh_dh_mapping_df is required")])
    if cft_vertical_df is None:
        return _failed([_issue("missing_input", "cft_vertical_df is required")])

    # MH mapping: DC/PH (uppercase) → MH-1
    mh_mapping = _build_mh_mapping_from_df(mh_dh_mapping_df)
    if not mh_mapping:
        issues.append(_issue("empty_lookup", "MH-DH mapping produced no keys; source→MH1 resolution will fail"))

    # CFT map: normalized vertical/sc → avg_cft_cuft
    cft_map: dict[str, float] = {}
    if "vertical_norm" in cft_vertical_df.columns and "avg_cft_cuft" in cft_vertical_df.columns:
        for _, row in cft_vertical_df.iterrows():
            k = str(row["vertical_norm"])
            v = row["avg_cft_cuft"]
            if k and pd.notna(v):
                cft_map[k] = float(v)

    def _day_cols_from_headers(columns: list[str]) -> list[str]:
        return [
            c for c in columns
            if _norm_str(c).startswith("day_")
            and _norm_str(c)[4:].isdigit()
            and day_start <= int(_norm_str(c)[4:]) <= day_end
        ]

    def _process_chunk(
        chunk: pd.DataFrame,
        stream: str,
        cft_col: str,
        cft_fallback: float,
        day_cols: list[str],
    ) -> Optional[pd.DataFrame]:
        """
        Per-chunk processing: EKL filter → normalise → CFT → groupby reduce.
        Returns a small aggregated DataFrame (source, destination_hub, day_cols, _avg_daily, _cft_vol)
        or None if chunk is empty after filter.
        """
        d = chunk.copy()
        vendor_col = next((c for c in d.columns if _norm_str(c) == "vendor"), None)
        if vendor_col is None:
            return None
        ekl_m = d[vendor_col].astype(str).str.strip().str.lower().isin(vendors)
        d = d.loc[ekl_m].copy()
        if d.empty:
            return None

        if stream == "alite":
            hub_c  = next((c for c in d.columns if _norm_str(c) == "hub"), None)
            dest_c = next((c for c in d.columns if _norm_str(c) == "destination_hub"), None)
            if hub_c and not dest_c:
                d = d.rename(columns={hub_c: "destination_hub"})

        dest_c = next((c for c in d.columns if _norm_str(c) == "destination_hub"), None)
        src_c  = next((c for c in d.columns if _norm_str(c) == "source"), None)
        if dest_c is None or src_c is None:
            return None

        d["destination_hub"] = d[dest_c].astype(str).str.strip().str.upper()
        d["source"]          = d[src_c].astype(str).str.strip().str.upper()

        if stream == "nfbf":
            d["source"] = d["source"].str.replace("_FURNITURE", "", regex=False)\
                                     .str.replace("_LARGE",     "", regex=False)

        cft_lookup_col = next((c for c in d.columns if _norm_str(c) == _norm_str(cft_col)), None)
        if cft_lookup_col:
            d["_cft_key"] = d[cft_lookup_col].map(_norm_str)
            d["_cft"]     = d["_cft_key"].map(cft_map).fillna(cft_fallback)
        else:
            d["_cft"] = cft_fallback

        day_present = [c for c in day_cols if c in d.columns]
        if not day_present:
            return None

        d["_avg_daily"] = (
            d[day_present].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
            / avg_divisor
        )
        d["_cft_vol"] = d["_avg_daily"] * d["_cft"]

        keep = ["source", "destination_hub"] + day_present + ["_avg_daily", "_cft_vol"]
        d = d[[c for c in keep if c in d.columns]]

        day_part = d.groupby(["source", "destination_hub"], sort=False)[day_present].sum().reset_index()
        cft_part = d.groupby(["source", "destination_hub"], sort=False)[["_avg_daily", "_cft_vol"]].sum().reset_index()
        return day_part.merge(cft_part, on=["source", "destination_hub"], how="left")

    def _aggregate_stream_from_path(
        path: Path,
        stream: str,
        cft_col: str,
        cft_fallback: float,
    ) -> Optional[pd.DataFrame]:
        """Incremental chunked aggregation from a file path. Peak memory = one chunk + small partials."""
        head = pd.read_csv(path, nrows=0, encoding="utf-8-sig")
        day_cols = _day_cols_from_headers(list(head.columns))
        if not day_cols:
            issues.append(_issue("missing_columns", f"{stream}: no day_{day_start}..day_{day_end} columns in {path.name}"))
            return None

        day_partials: list[pd.DataFrame] = []
        cft_partials: list[pd.DataFrame] = []
        rows_seen = rows_kept = 0

        for chunk in pd.read_csv(path, chunksize=chunksize, low_memory=False, encoding="utf-8-sig"):
            rows_seen += len(chunk)
            proc = _process_chunk(chunk, stream, cft_col, cft_fallback, day_cols)
            if proc is None or proc.empty:
                continue
            rows_kept += len(proc)
            day_partials.append(proc[["source", "destination_hub"] + [c for c in day_cols if c in proc.columns]])
            cft_partials.append(proc[["source", "destination_hub", "_avg_daily", "_cft_vol"]])

        if not day_partials:
            issues.append(_issue("empty_result", f"{stream}: no EKL rows found after chunked read"))
            return None

        day_agg = (
            pd.concat(day_partials, ignore_index=True)
            .groupby(["source", "destination_hub"], sort=False)[[c for c in day_cols if c in day_partials[0].columns]]
            .sum().reset_index()
        )
        cft_agg = (
            pd.concat(cft_partials, ignore_index=True)
            .groupby(["source", "destination_hub"], sort=False)[["_avg_daily", "_cft_vol"]]
            .sum().reset_index()
        )
        return _finalise_stream_lane(day_agg, cft_agg, stream, [c for c in day_cols if c in day_agg.columns])

    def _aggregate_stream_from_df(
        df: pd.DataFrame,
        stream: str,
        cft_col: str,
        cft_fallback: float,
    ) -> Optional[pd.DataFrame]:
        """In-memory path for pre-loaded DataFrames."""
        day_cols = _day_cols_from_headers(list(df.columns))
        proc = _process_chunk(df, stream, cft_col, cft_fallback, day_cols)
        if proc is None or proc.empty:
            return None
        present_day_cols = [c for c in day_cols if c in proc.columns]
        day_agg = proc[["source", "destination_hub"] + present_day_cols].copy()
        cft_agg = proc[["source", "destination_hub", "_avg_daily", "_cft_vol"]].copy()
        return _finalise_stream_lane(day_agg, cft_agg, stream, present_day_cols)

    def _finalise_stream_lane(
        day_agg: pd.DataFrame,
        cft_agg: pd.DataFrame,
        stream: str,
        day_cols: list[str],
    ) -> Optional[pd.DataFrame]:
        """Compute peak/median, map source→MH1, group by (MH1, destination_hub)."""
        vals = day_agg[day_cols].apply(pd.to_numeric, errors="coerce").fillna(0) if day_cols else pd.DataFrame()
        day_agg["augpeak"]   = vals.max(axis=1) if not vals.empty else 0.0
        day_agg["augmedian"] = vals.median(axis=1) if not vals.empty else 0.0

        day_agg["MH1"] = day_agg["source"].map(mh_mapping)
        n_null = int(day_agg["MH1"].isna().sum())
        if n_null:
            issues.append(_issue("unresolved_sources", f"{stream}: {n_null} (source, DH) pairs dropped (no MH1 mapping)"))
        day_agg = day_agg.dropna(subset=["MH1"])
        if day_agg.empty:
            return None

        merged = day_agg.merge(
            cft_agg[["source", "destination_hub", "_avg_daily", "_cft_vol"]],
            on=["source", "destination_hub"], how="left",
        )
        merged["_avg_daily"] = merged["_avg_daily"].fillna(0.0)
        merged["_cft_vol"]   = merged["_cft_vol"].fillna(0.0)

        return merged.groupby(["MH1", "destination_hub"], sort=False).agg(**{
            f"augpeak_{stream}":    ("augpeak",   "sum"),
            f"augmedian_{stream}":  ("augmedian", "sum"),
            f"_cft_vol_{stream}":   ("_cft_vol",  "sum"),
            f"_avg_daily_{stream}": ("_avg_daily", "sum"),
        }).reset_index()

    # Route each stream to chunked-path or in-memory path
    def _process(df, path, stream, cft_col, fallback):
        if path is not None:
            return _aggregate_stream_from_path(Path(path), stream, cft_col, fallback)
        if df is not None:
            return _aggregate_stream_from_df(df, stream, cft_col, fallback)
        issues.append(_issue("missing_input", f"{stream}: neither df nor path provided"))
        return None

    alpha_lane = _process(alpha_df, alpha_path, "fbf",   "sc",       alpha_fallback)
    alite_lane = _process(alite_df, alite_path, "alite", "sc",       alite_fallback)
    nfbf_lane  = _process(nfbf_df,  nfbf_path,  "nfbf",  "vertical", nfbf_fallback)

    if all(x is None for x in (alpha_lane, alite_lane, nfbf_lane)):
        return _failed(
            issues + [_issue("empty_result", "All three streams produced no rows after processing")],
        )

    def _empty_lane(stream: str) -> pd.DataFrame:
        return pd.DataFrame(columns=[
            "MH1", "destination_hub",
            f"augpeak_{stream}", f"augmedian_{stream}",
            f"_cft_vol_{stream}", f"_avg_daily_{stream}",
        ])

    alpha_lane = alpha_lane if alpha_lane is not None else _empty_lane("fbf")
    alite_lane = alite_lane if alite_lane is not None else _empty_lane("alite")
    nfbf_lane  = nfbf_lane  if nfbf_lane  is not None else _empty_lane("nfbf")

    base = (
        alpha_lane
        .merge(alite_lane, on=["MH1", "destination_hub"], how="outer")
        .merge(nfbf_lane,  on=["MH1", "destination_hub"], how="outer")
    )
    for col in [c for c in base.columns if c not in ("MH1", "destination_hub")]:
        base[col] = base[col].fillna(0.0)

    base["augpeak"]      = base["augpeak_fbf"]   + base["augpeak_alite"]   + base["augpeak_nfbf"]
    base["augmedian"]    = base["augmedian_fbf"]  + base["augmedian_alite"] + base["augmedian_nfbf"]
    base["newdemandret"] = base["augpeak"] * returns_factor

    # Per-stream weighted avg CFT → plan median/peak CFT volumes
    for stream in ("fbf", "alite", "nfbf"):
        avg_d = base[f"_avg_daily_{stream}"].replace(0, np.nan)
        avg_cft = base[f"_cft_vol_{stream}"] / avg_d
        base[f"plan_median_cft_{stream}"] = (base[f"augmedian_{stream}"] * avg_cft).fillna(0.0)
        base[f"plan_peak_cft_{stream}"]   = (base[f"augpeak_{stream}"]   * avg_cft).fillna(0.0)

    base["plan_median_cft_volume"] = (
        base["plan_median_cft_fbf"] + base["plan_median_cft_alite"] + base["plan_median_cft_nfbf"]
    )
    base["plan_peak_cft_volume"] = (
        base["plan_peak_cft_fbf"] + base["plan_peak_cft_alite"] + base["plan_peak_cft_nfbf"]
    )

    # Stream tag: ALITE priority, then FBF (if fbf >= nfbf), else NFBF
    base["stream"] = np.select(
        [base["augmedian_alite"] > 0, base["augmedian_fbf"] >= base["augmedian_nfbf"]],
        ["ALITE", "FBF"],
        default="NFBF",
    )
    base["source_type"] = base["stream"].map({"FBF": "FC_MH", "ALITE": "ALITE", "NFBF": "MH"})

    # Mixed-stream flag
    base["has_mixed_streams"] = (base["augmedian_fbf"] > 0) & (base["augmedian_nfbf"] > 0)
    base["minority_stream_shipments"] = np.where(
        base["has_mixed_streams"],
        np.minimum(base["augmedian_fbf"], base["augmedian_nfbf"]),
        0.0,
    )

    base = base.rename(columns={"destination_hub": "LMHub"})

    col_order = [
        "MH1", "LMHub",
        "stream", "source_type", "has_mixed_streams", "minority_stream_shipments",
        "augmedian", "augpeak", "newdemandret",
        "plan_median_cft_volume", "plan_peak_cft_volume",
        "augmedian_fbf", "augpeak_fbf", "plan_median_cft_fbf", "plan_peak_cft_fbf",
        "augmedian_alite", "augpeak_alite", "plan_median_cft_alite", "plan_peak_cft_alite",
        "augmedian_nfbf", "augpeak_nfbf", "plan_median_cft_nfbf", "plan_peak_cft_nfbf",
    ]
    base = base[[c for c in col_order if c in base.columns]]

    return _ok(base) if not issues else _partial(base, issues)


def build_fbf_network_pathway(
    pathway_df: pd.DataFrame,
    tagging_df: Optional[pd.DataFrame] = None,
    fc_map_df: Optional[pd.DataFrame] = None,
    mh_dh_map_df: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """
    Build wide P1–P5 pathway table from raw pathway DataFrame.
    Resolves DC/FC names → Central Hub strings via MH1 tagging, FC map, MH-DH mapping (in that priority).
    Returns one row per input row with columns p1..p5_dc_raw, p1..p5_central_hub,
    p1..p5_mapped_via, p1..p5_pct, pathway_signature.
    """
    issues: list[dict] = []
    cols  = [str(c) for c in pathway_df.columns if c is not None]
    p_dc  = _find_p_dc_columns(cols)
    p_pct = _find_p_pct_columns(cols)

    if not p_dc or not p_pct:
        return _failed(
            [_issue("missing_columns", "Pathway DataFrame lacks P1–P5 DC/FC + % columns")]
        )

    mh1_by  = _mh1_lookup_map(tagging_df) if tagging_df is not None else {}
    fc_by   = _fc_map_lookup(fc_map_df)
    mh_dh_by = _mh_dh_lookup(mh_dh_map_df)

    dh_col  = _detect_dh_col(cols)
    rows_out: list[dict] = []
    n_unmapped = 0

    for ridx, row in pathway_df.iterrows():
        w: dict[str, Any] = {}
        if dh_col and dh_col in row.index:
            w["destination_hub"] = row[dh_col]
        w["source_row"] = ridx
        segs: list[str] = []
        for i in range(1, 6):
            cdc  = p_dc.get(i)
            cpct = p_pct.get(i)
            raw_dc = row[cdc] if cdc and cdc in row.index else np.nan
            ch, how = _map_dc_to_central_hub(raw_dc, mh1_by=mh1_by, fc_by=fc_by, mh_dh_by=mh_dh_by)
            w[f"p{i}_dc_raw"]       = raw_dc
            w[f"p{i}_central_hub"]  = ch
            w[f"p{i}_mapped_via"]   = how
            pct_val = row[cpct] if cpct and cpct in row.index else np.nan
            w[f"p{i}_pct"] = pct_val
            if how == "as_is" and cdc and pd.notna(raw_dc) and str(raw_dc).strip():
                n_unmapped += 1
            if ch and str(ch).strip():
                segs.append(f"P{i}={ch}")
        w["pathway_signature"] = " | ".join(segs) if segs else ""
        rows_out.append(w)

    wide = pd.DataFrame(rows_out)
    if n_unmapped:
        issues.append(_issue("unmapped_dc", f"{n_unmapped} DC/FC values passed through as-is (no hub mapping)"))

    return _ok(wide) if not issues else _partial(wide, issues)


# ---------------------------------------------------------------------------
# UTILITY
# ---------------------------------------------------------------------------

def save_dataframe(df: pd.DataFrame, path: Any) -> dict[str, Any]:
    """Write DataFrame as CSV to the exact path given; creates parent dirs as needed."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
        return _ok({"path": str(path), "rows": len(df), "columns": len(df.columns)})
    except Exception as e:
        return _failed([_issue("write_error", str(e))])


def get_required_columns(function_name: str) -> dict[str, list[str]]:
    """
    Return required columns per input parameter so callers can do header checks
    before calling. Keys are parameter names; values are lists of required column names
    (using original / expected casing, not normalised).
    """
    _REGISTRY: dict[str, dict[str, list[str]]] = {
        # Config loader
        "load_agent1_config":       {},
        # Loading functions: key is "path"
        "load_resort":              {"path": ["MH1", "LMHub", "PATH"]},
        "load_lm_fdp":              {"path": [
            "fulfill_item_service_profile", "order_item_unit_source_facility",
            "source_pincode", "customer_pincode", "analytic_vertical", "logistics_carrier",
        ]},
        "load_cft_vertical":        {"path": ["vert", "<any vol+ship column>"]},
        "load_fbf_day_plan":        {"path": [
            "source", "destination_hub", "destination_pincode",
            "seller", "sc", "vendor", "day_1", "...", "day_30",
        ]},
        "load_sd_plans":            {
            "alpha_path": ["source", "destination_hub", "sc", "vendor", "day_1..day_30"],
            "alite_path": ["hub", "source", "sc", "vendor", "day_1..day_30"],
            "nfbf_path":  ["source", "destination_hub", "vertical", "vendor", "day_1..day_30"],
        },
        "load_fbf_network_pathway": {"path": ["P1 DC", "P1%", "P2 DC", "P2%", "..."]},
        "load_mh1_tagging":         {"path": ["MH1", "<tag column>"]},
        "load_lm_pbh":              {"path": ["pincode", "<hub column>"]},
        "load_fm_pbh":              {"path": ["pincode", "<associated MH / central hub column>"]},
        "load_fc_map":              {"path": ["mh_code", "mh_name"]},
        # Transformation functions: keys are DataFrame parameter names
        "parse_resort":             {"resort_df": ["MH1", "LMHub", "PATH"]},
        "tag_mh1":                  {
            "resort_df":  ["MH1"],
            "tagging_df": ["MH1", "<tag column>"],
        },
        "join_demand":              {
            "resort_tagged_df": ["MH1", "LMHub"],
            "demand_df":        ["MH1", "LMHub", "augmedian", "augpeak", "newdemandret"],
        },
        "build_plan_volume":        {"plan_df": ["MH1", "LMHub", "has_demand_data"]},
        "filter_actuals":           {"fdp_df": ["logistics_carrier", "fulfill_item_service_profile"]},
        "resolve_destinations":     {
            "fdp_df":    ["customer_pincode"],
            "lm_pbh_df": ["pincode", "<hub column>"],
        },
        "resolve_sources":          {
            "fdp_df":    [
                "order_item_unit_source_facility", "fulfill_item_service_profile",
                "source_pincode", "analytic_vertical",
            ],
            "fm_pbh_df": ["pincode", "<associated MH / central hub column>"],
            "fc_map_df": ["mh_code", "mh_name"],
            "resort_df": ["MH1", "LMHub", "last_mh", "second_last_mh"],
        },
        "compute_cft":              {
            "fdp_df":        ["analytic_vertical", "fulfill_item_service_profile"],
            "cft_lookup_df": ["vertical_norm", "avg_cft_cuft"],
        },
        "aggregate_actuals":        {"fdp_df": [
            "source", "destination", "stream", "source_type",
            "analytic_vertical", "shipment_cft",
        ]},
        "build_fbf_aggregate":      {
            "fbf_day_plan_df": ["source", "destination_hub", "destination_pincode",
                                "seller", "sc", "vendor", "day_1..day_30"],
            "top266_df":       ["pincode", "Final Mapping"],
            "cft_vertical_df": ["vertical_norm", "avg_cft_cuft"],
        },
        "build_sd_plan_aggregate":  {
            "alpha_df":         ["source", "destination_hub", "sc", "vendor", "day_1..day_30"],
            "alite_df":         ["hub", "source", "sc", "vendor", "day_1..day_30"],
            "nfbf_df":          ["source", "destination_hub", "vertical", "vendor", "day_1..day_30"],
            "mh_dh_mapping_df": ["DC / PH", "MH-1"],
            "cft_vertical_df":  ["vertical_norm", "avg_cft_cuft"],
        },
        "build_fbf_network_pathway": {
            "pathway_df": ["P1 DC", "P1%", "..."],
            "tagging_df": ["MH1"],
            "fc_map_df":  ["mh_code", "mh_name"],
            "mh_dh_map_df": ["DC / PH", "MH-1"],
        },
    }
    return _REGISTRY.get(function_name, {})


# ---------------------------------------------------------------------------
# Internal helpers for build_fbf_network_pathway
# ---------------------------------------------------------------------------

def _find_p_dc_columns(cols: list[str]) -> dict[int, str]:
    """Map P-index 1..5 → actual column name for Pk DC/FC."""
    out: dict[int, str] = {}
    for c in cols:
        if "%" in str(c):
            continue
        k = _norm_header_key(c)
        if "pct" in k or "percent" in k or "share" in k:
            continue
        for i in range(1, 6):
            if re.fullmatch(rf"p{i}(_|)(dc|fc)", k) or re.fullmatch(rf"p{i}(dc|fc)", k):
                if i not in out:
                    out[i] = c
                break
    return out

def _find_p_pct_columns(cols: list[str]) -> dict[int, str]:
    """Map P-index 1..5 → % / share column name."""
    out: dict[int, str] = {}
    for c in cols:
        k = _norm_header_key(c)
        for i in range(1, 6):
            ok = k in (f"p{i}_%", f"p{i}%", f"p{i}_pct", f"p{i}_percent", f"p{i}_share")
            ok = ok or (k == f"p{i}" and "%" in str(c))
            if not ok and k.startswith(f"p{i}"):
                ok = ("pct" in k or "percent" in k or "share" in k) and "dc" not in k and "fc" not in k
            if ok and i not in out:
                out[i] = c
                break
    return out

def _mh1_lookup_map(tagging: pd.DataFrame) -> dict[str, str]:
    """Normalised MH1 key → canonical string (for DC → central hub resolution)."""
    t   = tagging.copy()
    col = "MH1" if "MH1" in t.columns else next((c for c in t.columns if _norm_str(c) == "mh1"), None)
    if col is None:
        return {}
    m: dict[str, str] = {}
    for val in t[col].dropna().unique():
        s = str(val).strip()
        if s:
            m[_norm_str(s)] = s
    return m

def _fc_map_lookup(fc_map: Optional[pd.DataFrame]) -> dict[str, str]:
    if fc_map is None or len(fc_map) == 0:
        return {}
    fc       = fc_map.copy()
    code_col = next((c for c in fc.columns if _norm_str(c) == "mh_code"), None)
    name_col = next((c for c in fc.columns if _norm_str(c) == "mh_name"), None)
    if not code_col or not name_col:
        return {}
    m: dict[str, str] = {}
    for _, r in fc.iterrows():
        code, name = r.get(code_col), r.get(name_col)
        if pd.isna(code) or pd.isna(name):
            continue
        m[_norm_str(str(code).strip())] = str(name).strip()
    return m

def _mh_dh_lookup(mh_dh: Optional[pd.DataFrame]) -> dict[str, str]:
    """DC/PH (norm) → MH-1 string lookup."""
    if mh_dh is None or len(mh_dh) == 0:
        return {}
    df    = mh_dh.copy()
    key_c = next((c for c in df.columns if _norm_header_key(c) == "dc_ph"), None)
    val_c = next((c for c in df.columns if _norm_header_key(c) == "mh_1"), None)
    if not key_c or not val_c:
        key_c, val_c = str(df.columns[0]), str(df.columns[1])
    m: dict[str, str] = {}
    for _, r in df.iterrows():
        k, v = r.get(key_c), r.get(val_c)
        if pd.isna(k) or pd.isna(v):
            continue
        ks = str(k).strip()
        if ks:
            m[_norm_str(ks)] = str(v).strip()
    return m

def _map_dc_to_central_hub(
    raw: Any,
    *,
    mh1_by: dict[str, str],
    fc_by: dict[str, str],
    mh_dh_by: dict[str, str],
) -> tuple[str, str]:
    """
    Resolve a DC/FC name to a central hub string.
    Precedence: MH1 tagging → FC map → MH-DH mapping → passthrough ("as_is").
    """
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return "", "blank"
    s = str(raw).strip()
    if not s:
        return "", "blank"
    n = _norm_str(s)
    if n in mh1_by:
        return mh1_by[n], "mh1_tag"
    if n in fc_by:
        return fc_by[n], "fc_map"
    if n in mh_dh_by:
        return mh_dh_by[n], "mh_dh"
    return s, "as_is"

def _detect_dh_col(cols: list[str]) -> Optional[str]:
    for c in cols:
        k = _norm_header_key(c)
        if k in ("destination_hub", "dest_hub", "destination_h", "dh", "delivery_hub", "d_h"):
            return c
    for c in cols:
        if "destination" in _norm_header_key(c) and "hub" in _norm_header_key(c):
            return c
    return None


# ---------------------------------------------------------------------------
# Internal helpers for resolve_sources
# ---------------------------------------------------------------------------

def _build_resort_lmhub_dominant(resort_parsed: pd.DataFrame) -> pd.DataFrame:
    """LMHub (norm) → dominant last_mh, second_last_mh for MFC resolution."""
    sub = resort_parsed.copy()
    sub["_lm_norm"] = sub["LMHub"].map(_norm_str)
    def dom(series: pd.Series) -> Any:
        vc = series.dropna().astype(str).value_counts()
        return vc.index[0] if len(vc) else None
    return sub.groupby("_lm_norm", as_index=False).agg(
        last_mh=("last_mh", dom),
        second_last_mh=("second_last_mh", dom),
    )

def _clean_hop(val: Any) -> Optional[str]:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    s = str(val).strip()
    return s if s else None

def _mfc_city_source(last_mh: Any, second_last: Any) -> str:
    """
    Determine MFC source city from PATH hops.
    Heuristics: FRN + (YKB or KAL) → "FRN"; BAG + KOL5 → "BAG"; else last_mh.
    """
    a, b = _clean_hop(last_mh), _clean_hop(second_last)
    hops = [_norm_str(h) for h in (a, b) if h]
    if not hops:
        return ""
    has_frn     = any("frn" in h for h in hops)
    has_ykb_kal = any("ykb" in h or "kal" in h for h in hops)
    if has_frn and has_ykb_kal:
        return "FRN"
    has_bag  = any("bag" in h for h in hops)
    has_kol5 = any("kol5" in h for h in hops)
    if has_bag and has_kol5:
        return "BAG"
    return (a or "").strip() if a else ""


# ---------------------------------------------------------------------------
# Internal helper for build_sd_plan_aggregate
# ---------------------------------------------------------------------------

def _build_mh_mapping_from_df(df: pd.DataFrame) -> dict[str, str]:
    """
    Build DC/PH (uppercase) → MH-1 dict from mh_dh_mapping DataFrame.
    Tries _norm_header_key match for 'dc_ph' and 'mh_1'; falls back to positional cols 0, 1.
    """
    key_c = next((c for c in df.columns if _norm_header_key(c) == "dc_ph"), None)
    val_c = next((c for c in df.columns if _norm_header_key(c) == "mh_1"), None)
    if key_c is None or val_c is None:
        # Positional fallback (matches original sd_plan_aggregate.py behaviour)
        key_c, val_c = str(df.columns[0]), str(df.columns[1])
    mapping: dict[str, str] = {}
    for k, v in zip(df[key_c], df[val_c]):
        if pd.notna(k) and pd.notna(v):
            mapping[str(k).strip().upper()] = str(v).strip()
    return mapping
