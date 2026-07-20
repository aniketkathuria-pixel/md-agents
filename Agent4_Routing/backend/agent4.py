"""
Agent 4 — composable tool library for multi-city last-mile route optimization.

Public API
----------
Result-dict functions ({"status": "ok"|"partial"|"failed", "data": ..., "issues": [...]}):
    build_location_file(agent3_assignment_df, dh_feasibility_df, time_window_overrides=None)
    preflight_check(location_file_df, dist_df, mhdh_rate_card_df, cfg)
    build_distance_dict(dist_df)
    build_latlong_dict(ll_df)
    run_agent4_pipeline(location_file_df, lat_long_df, dist_df, mhdh_rate_card_path, out_dir, cfg)

Phase-2-compatible (exact signatures preserved):
    run_agent4_for_mh(mh_name, mh_cfg, dh_df, dist_dict, latlong, cfg, ...) -> Agent4MHResult
    derive_freq_allowed(top266_load) -> int
    assign_vehicle_length(total_demand) -> float
    Agent4MHResult  (dataclass — osrm_log field added at end, default=[])
    MHConfig        (dataclass — unchanged)

Plain-return helpers (no result dict; exact signatures preserved):
    load_agent4_config(config_path=None) -> dict
    load_rate_card(path, cfg) -> dict[str, MHConfig]
    get_distance(origin, dest, dist_dict, latlong) -> Optional[float]
    get_transit_time(dist_km) -> float
    preprocess_ftl_splits(...) -> tuple
    assign_bearing_clusters(...) -> pd.DataFrame

No orchestrator imports. No sys.exit().
OSRM logging: contextvar-based (no module-level mutable global).
"""
from __future__ import annotations

import contextvars
import itertools
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests
from pulp import LpBinary, LpMinimize, LpProblem, LpVariable, lpSum, PULP_CBC_CMD

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OSRM contextvar — replaces the old module-level _osrm_fallback_log list.
# run_agent4_for_mh sets this to a local list; _osrm_distance_km appends to it.
# After the call returns the contextvar is reset, so concurrent/sequential runs
# never share log state.
# ---------------------------------------------------------------------------

_osrm_log_ctx: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "_osrm_log_ctx", default=None
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "max_comb_limit": 20_000_000,
    "default_service_time_min": 120,
    "default_max_hops": 4,
    "default_threshold_a": 50,
    "default_threshold_b": 150,
    "default_depot_departure_min": 0,
    "default_time_window_start_min": 0,
    "default_time_window_end_min": 1440,
    "local_zonal_distance_threshold_km": 200,
    "col_location_name": "destination_hub_key",
    "col_mh_assignment": "current_fc_mh",
    "col_demand": "total_cft",
    "col_top266_load": "top266_shipments",
    "col_ml": "ML",
}


def load_agent4_config(config_path: Optional[Path] = None) -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG)
    if config_path and Path(config_path).is_file():
        try:
            with open(config_path, encoding="utf-8") as fh:
                overrides = json.load(fh)
            cfg.update(overrides)
        except Exception as exc:
            logger.warning("Could not load agent4_config.json: %s — using defaults.", exc)
    return cfg


# ---------------------------------------------------------------------------
# I/O helpers (private)
# ---------------------------------------------------------------------------

def _read_full(path: Path, sheet: Optional[str] = None, **kwargs: Any) -> pd.DataFrame:
    suf = str(path).lower()
    if suf.endswith(".csv"):
        kw: dict[str, Any] = {"low_memory": False, "encoding": "utf-8-sig"}
        kw.update(kwargs)
        return pd.read_csv(path, **kw)
    if suf.endswith(".xlsx"):
        return pd.read_excel(
            path, sheet_name=sheet if sheet is not None else 0, engine="openpyxl", **kwargs
        )
    if suf.endswith(".xlsb"):
        return pd.read_excel(
            path, sheet_name=sheet if sheet is not None else 0, engine="pyxlsb", **kwargs
        )
    raise ValueError(f"Unsupported format: {path}")


def _read_table_first_row(path: Path, sheet: Optional[str] = None) -> list[str]:
    suf = str(path).lower()
    if suf.endswith(".csv"):
        return list(pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns)
    if suf.endswith(".xlsx"):
        return list(pd.read_excel(path, sheet_name=sheet or 0, engine="openpyxl", nrows=0).columns)
    return []


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def headers_match_location_file(cols: list[str]) -> bool:
    nc = {str(c).strip().lower() for c in cols}
    return (
        "destination_hub_key" in nc
        and "current_fc_mh" in nc
        and "total_shipments" in nc
        and "ml" in nc
    )


def headers_match_mh_dh_rate_card(cols: list[str]) -> bool:
    nc = {str(c).strip().lower() for c in cols}
    return (
        "mh1" in nc
        and any(c.startswith("local:") for c in nc)
        and any(c.startswith("zonal:") for c in nc)
    )


# ---------------------------------------------------------------------------
# Rate card loading
# ---------------------------------------------------------------------------

@dataclass
class MHConfig:
    mh_name: str
    local_rate_card: dict[float, float]
    zonal_rate_card: dict[float, float]
    max_hops: int
    threshold_a: float
    threshold_b: float
    service_time_min: int
    city: str = ""
    tag: str = ""
    min_vehicle_ft: float = 6.5   # floor enforced before rate lookup; Phase 2 sets 20.0


def load_rate_card(path: Path, cfg: dict[str, Any]) -> dict[str, MHConfig]:
    """Load MHDH_RateCard.xlsx; return mh_name → MHConfig."""
    df = _read_full(path)
    mh_configs: dict[str, MHConfig] = {}

    local_cols = [c for c in df.columns if str(c).startswith("Local:")]
    zonal_cols = [c for c in df.columns if str(c).startswith("Zonal:")]

    for _, row in df.iterrows():
        mh = str(row["MH1"]).strip()
        if not mh or mh.lower() == "nan":
            continue

        local_rc: dict[float, float] = {}
        for col in local_cols:
            try:
                size = float(col.split(":")[1])
                val  = float(row[col])
                local_rc[size] = val
            except (ValueError, IndexError):
                pass

        zonal_rc: dict[float, float] = {}
        for col in zonal_cols:
            try:
                size = float(col.split(":")[1])
                val  = float(row[col])
                zonal_rc[size] = val
            except (ValueError, IndexError):
                pass

        max_hops     = int(row["max_hops"])     if "max_hops"     in df.columns and pd.notna(row.get("max_hops"))     else cfg["default_max_hops"]
        threshold_a  = float(row["threshold_a"]) if "threshold_a"  in df.columns and pd.notna(row.get("threshold_a"))  else cfg["default_threshold_a"]
        threshold_b  = float(row["threshold_b"]) if "threshold_b"  in df.columns and pd.notna(row.get("threshold_b"))  else cfg["default_threshold_b"]
        service_time = int(row["service_time"])  if "service_time" in df.columns and pd.notna(row.get("service_time")) else cfg["default_service_time_min"]

        mh_configs[mh] = MHConfig(
            mh_name=mh,
            local_rate_card=local_rc,
            zonal_rate_card=zonal_rc,
            max_hops=max_hops,
            threshold_a=threshold_a,
            threshold_b=threshold_b,
            service_time_min=service_time,
            city=str(row.get("City", "")).strip(),
            tag=str(row.get("Tag",  "")).strip(),
        )

    return mh_configs


# ---------------------------------------------------------------------------
# Distance dict  (result dict)
# ---------------------------------------------------------------------------

def build_distance_dict(dist_df: pd.DataFrame) -> dict:
    """Build (origin, dest) → km dict from Distance Matrix DataFrame.

    Deduplicates by keeping the first occurrence per pair.
    Non-numeric distance rows are reported as missing_distance issues.

    Returns {"status": "ok", "data": dict, "issues": [...]}.
    """
    d: dict[tuple[str, str], float] = {}
    issues: list[dict] = []
    for _, row in dist_df.iterrows():
        k = (str(row["S_Code"]).strip(), str(row["D_Code"]).strip())
        if k not in d:
            try:
                d[k] = float(row["distance"])
            except (ValueError, TypeError):
                issues.append({
                    "type": "missing_distance",
                    "detail": f"Non-numeric distance for ({k[0]}, {k[1]}): {row['distance']!r}",
                })
    return {"status": "ok", "data": d, "issues": issues}


# ---------------------------------------------------------------------------
# Lat/long dict  (result dict)
# ---------------------------------------------------------------------------

def build_latlong_dict(ll_df: pd.DataFrame) -> dict:
    """Build site_name → (lat, lon) from a Lat Longs DataFrame.

    Deduplicates by keeping the first occurrence per site.

    Returns {"status": "ok", "data": dict, "issues": [...]}.
    """
    ll = ll_df.copy()
    ll["_key"] = ll["Site_name"].astype(str).str.strip()
    ll = ll.drop_duplicates(subset="_key", keep="first")
    result: dict[str, tuple[float, float]] = {}
    issues: list[dict] = []
    for _, row in ll.iterrows():
        try:
            result[row["_key"]] = (float(row["Latitude"]), float(row["Longitude"]))
        except (ValueError, TypeError):
            issues.append({
                "type": "invalid_latlong",
                "detail": f"Non-numeric lat/lon for {row['_key']!r}",
            })
    return {"status": "ok", "data": result, "issues": issues}


# ---------------------------------------------------------------------------
# Build location file  (new — result dict)
# ---------------------------------------------------------------------------

def build_location_file(
    agent3_assignment_df: pd.DataFrame,
    dh_feasibility_df: pd.DataFrame,
    phase2_accepted_changes: Optional[dict[str, str]] = None,
    time_window_overrides: Optional[dict[str, dict[str, Any]]] = None,
) -> dict:
    """Assemble the Location File DataFrame from Agent 3 output + DH Feasibility.

    MH assignment source
    --------------------
    Uses `current_fc_mh` from agent3_assignment_df as the baseline — this is the
    original resort mapping, not Agent 3's Phase 1 proposal (`assigned_fc_mh`).
    Agent 3's Phase 1 output is a proposal only; the confirmed baseline always
    comes from the resort.

    phase2_accepted_changes: {"DH_KEY": "NEW_FC_MH", ...}
        Only DHs explicitly listed here get the override MH. All other DHs keep
        the resort baseline from current_fc_mh. Pass None (default) if Phase 2
        was not run or no changes were accepted.

    time_window_overrides: {key: {"time_window_start": v, "time_window_end": v,
                                   "depot_departure": v}}
        key may be an MH name (applies to all DHs under that MH) or a DH key.
        DH-level overrides are applied second and beat MH-level overrides.

    Output columns: destination_hub_key, current_fc_mh, total_cft,
                    top266_shipments, total_shipments, ML,
                    time_window_start, depot_departure, time_window_end.

    Returns
    -------
    {"status": "ok" | "partial", "data": pd.DataFrame, "issues": [...]}
    status="partial" when any DH has no ML in DH Feasibility (null-ML rows are
    included in data so the caller can decide to drop or fill them).
    """
    issues: list[dict] = []

    assign      = agent3_assignment_df.copy()
    feasibility = dh_feasibility_df.copy()
    feasibility.columns = [str(c).strip() for c in feasibility.columns]

    # Resort baseline: use current_fc_mh (original resort assignment).
    # Drop assigned_fc_mh so it doesn't conflict or mislead downstream.
    if "assigned_fc_mh" in assign.columns:
        assign = assign.drop(columns=["assigned_fc_mh"])

    # Left-join to bring in ML
    merged = assign.merge(
        feasibility[["destination_hub_key", "ML"]],
        on="destination_hub_key",
        how="left",
    )

    # Apply Phase 2 accepted changes — only explicitly accepted DHs are overridden.
    if phase2_accepted_changes:
        for dh_key, new_mh in phase2_accepted_changes.items():
            mask = merged["destination_hub_key"].astype(str).str.strip() == str(dh_key).strip()
            if mask.any():
                merged.loc[mask, "current_fc_mh"] = new_mh
            else:
                issues.append({
                    "type": "phase2_dh_not_found",
                    "detail": f"phase2_accepted_changes key '{dh_key}' not found in assignment df",
                })

    # Ensure all output columns exist
    for col in ("destination_hub_key", "current_fc_mh", "total_cft",
                "top266_shipments", "total_shipments", "ML"):
        if col not in merged.columns:
            merged[col] = None

    merged = merged[[
        "destination_hub_key", "current_fc_mh", "total_cft",
        "top266_shipments", "total_shipments", "ML",
    ]].copy()

    # Time-window defaults
    merged["time_window_start"] = 0
    merged["depot_departure"]   = 0
    merged["time_window_end"]   = 1800

    # Apply overrides: MH-level first, then DH-level (DH wins)
    if time_window_overrides:
        all_mh_names = set(merged["current_fc_mh"].dropna().astype(str).unique())
        all_dh_keys  = set(merged["destination_hub_key"].dropna().astype(str).unique())
        _tw_cols     = {"time_window_start", "time_window_end", "depot_departure"}

        for key, ov in time_window_overrides.items():
            if key in all_mh_names and key not in all_dh_keys:
                mask = merged["current_fc_mh"].astype(str) == key
                for col_name, val in ov.items():
                    if col_name in _tw_cols:
                        merged.loc[mask, col_name] = val

        for key, ov in time_window_overrides.items():
            if key in all_dh_keys:
                mask = merged["destination_hub_key"].astype(str) == key
                for col_name, val in ov.items():
                    if col_name in _tw_cols:
                        merged.loc[mask, col_name] = val

    # Flag missing ML
    null_ml_dhs = merged.loc[merged["ML"].isna(), "destination_hub_key"].tolist()
    for dh in null_ml_dhs:
        issues.append({
            "type": "missing_ml",
            "detail": f"No ML found in DH Feasibility for {dh}",
        })

    status = "partial" if null_ml_dhs else "ok"
    return {"status": status, "data": merged, "issues": issues}


# ---------------------------------------------------------------------------
# Preflight check  (new — hard gate, result dict)
# ---------------------------------------------------------------------------

_VALID_ML_VALUES: frozenset[float] = frozenset({6.5, 8.0, 10.0, 14.0, 17.0, 20.0, 22.0, 24.0, 32.0, 40.0})


def preflight_check(
    location_file_df: pd.DataFrame,
    dist_df: pd.DataFrame,
    mhdh_rate_card_df: pd.DataFrame,
    cfg: dict[str, Any],
) -> dict:
    """Hard gate before running the pipeline.  All checks must pass for status="ok".

    Checks performed:
    1. No null ML values.
    2. All ML values are in the valid set {6.5, 8, 10, 14, 17, 20, 22, 24, 32, 40}.
    3. All current_fc_mh values appear in rate card MH1 column.
    4. All destination_hub_key values appear in the distance matrix (S_Code or D_Code).
    5. All current_fc_mh values appear in the distance matrix.

    Returns {"status": "ok" | "failed", "data": None, "issues": [...]}.
    Never returns status="partial".
    """
    issues: list[dict] = []

    col_loc = cfg["col_location_name"]   # "destination_hub_key"
    col_mh  = cfg["col_mh_assignment"]   # "current_fc_mh"
    col_ml  = cfg["col_ml"]              # "ML"

    loc_df = location_file_df

    # 1. Null ML
    null_ml_mask  = loc_df[col_ml].isna()
    null_ml_count = int(null_ml_mask.sum())
    if null_ml_count > 0:
        null_dhs = loc_df.loc[null_ml_mask, col_loc].tolist()
        preview  = null_dhs[:5]
        suffix   = f" ... ({null_ml_count} total)" if null_ml_count > 5 else f" ({null_ml_count} total)"
        issues.append({
            "type":   "null_ml",
            "detail": f"DHs with null ML: {preview}{suffix}",
        })

    # 2. Invalid ML values
    non_null_ml = loc_df.loc[~null_ml_mask, col_ml]
    if len(non_null_ml) > 0:
        try:
            ml_floats    = non_null_ml.astype(float)
            invalid_vals = sorted(set(ml_floats[~ml_floats.isin(_VALID_ML_VALUES)].unique()))
            if invalid_vals:
                issues.append({
                    "type":   "invalid_ml",
                    "detail": f"ML values not in {sorted(_VALID_ML_VALUES)}: {invalid_vals}",
                })
        except (ValueError, TypeError) as exc:
            issues.append({"type": "invalid_ml", "detail": f"Could not cast ML to float: {exc}"})

    # 3. current_fc_mh present in rate card
    rc_mhs      = {str(v).strip() for v in mhdh_rate_card_df["MH1"].dropna().unique()}
    loc_mhs     = {str(v).strip() for v in loc_df[col_mh].dropna().unique()}
    missing_mhs = loc_mhs - rc_mhs
    if missing_mhs:
        issues.append({
            "type":   "mh_not_in_rate_card",
            "detail": f"MHs missing from rate card: {sorted(missing_mhs)}",
        })

    # 4 & 5. Distance matrix coverage
    dist_nodes = (
        {str(v).strip() for v in dist_df["S_Code"].dropna().unique()}
        | {str(v).strip() for v in dist_df["D_Code"].dropna().unique()}
    )

    loc_dhs         = {str(v).strip() for v in loc_df[col_loc].dropna().unique()}
    missing_dh_dist = loc_dhs - dist_nodes
    if missing_dh_dist:
        preview = sorted(missing_dh_dist)[:5]
        suffix  = f" ... ({len(missing_dh_dist)} total)" if len(missing_dh_dist) > 5 else f" ({len(missing_dh_dist)} total)"
        issues.append({
            "type":   "dh_missing_distance",
            "detail": f"DHs with no distance data: {preview}{suffix}",
        })

    missing_mh_dist = loc_mhs - dist_nodes
    if missing_mh_dist:
        issues.append({
            "type":   "mh_missing_distance",
            "detail": f"MHs with no distance data: {sorted(missing_mh_dist)}",
        })

    return {"status": "ok" if not issues else "failed", "data": None, "issues": issues}


# ---------------------------------------------------------------------------
# OSRM fallback
# ---------------------------------------------------------------------------

_OSRM_BASE = "http://router.project-osrm.org/route/v1/driving"


def _osrm_distance_km(
    origin: str,
    dest: str,
    latlong: dict[str, tuple[float, float]],
    dist_dict: dict[tuple[str, str], float],
) -> Optional[float]:
    """Call OSRM for (origin, dest); cache result in dist_dict; append to context log."""
    lat1, lon1 = latlong.get(origin, (None, None))
    lat2, lon2 = latlong.get(dest,   (None, None))
    if lat1 is None or lat2 is None:
        return None
    try:
        url  = f"{_OSRM_BASE}/{lon1},{lat1};{lon2},{lat2}?overview=false"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        dist_m  = data["routes"][0]["distance"]
        dist_km = dist_m / 1000.0
        dist_dict[(origin, dest)] = dist_km
        log = _osrm_log_ctx.get()
        if log is not None:
            log.append({
                "origin":          origin,
                "destination":     dest,
                "distance_km":     round(dist_km, 3),
                "transit_minutes": round(dist_km * 2, 3),
            })
        logger.info("OSRM fallback: %s → %s = %.1f km", origin, dest, dist_km)
        return dist_km
    except Exception as exc:
        logger.warning("OSRM failed for %s → %s: %s", origin, dest, exc)
        return None


def get_distance(
    origin: str,
    dest: str,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
) -> Optional[float]:
    """Return km from dist_dict; fall back to OSRM if missing."""
    km = dist_dict.get((origin, dest))
    if km is not None:
        return km
    return _osrm_distance_km(origin, dest, latlong, dist_dict)


def get_transit_time(dist_km: float) -> float:
    """Transit time in minutes = dist_km × 2  (assumes 30 km/h average speed)."""
    return dist_km * 2.0


# ---------------------------------------------------------------------------
# Vehicle sizing
# ---------------------------------------------------------------------------

def assign_vehicle_length(total_demand: float) -> float:
    """Map demand (CFT shipments) to vehicle length in feet. Uses 6.5 (not 7)."""
    if total_demand > 2550:
        return 40.0
    if total_demand > 1550:
        return 32.0
    if total_demand > 1325:
        return 24.0
    if total_demand > 1255:
        return 22.0
    if total_demand > 893:
        return 20.0
    if total_demand > 686:
        return 17.0
    if total_demand > 400:
        return 14.0
    if total_demand > 250:
        return 10.0
    if total_demand > 180:
        return 8.0
    if total_demand > 0:
        return 6.5
    return 0.0


# Max CFT capacity per vehicle size — inverse of assign_vehicle_length breakpoints.
ML_VEHICLE_CAPACITY: dict[float, int] = {
    6.5: 180,
    8:   250,
    10:  400,
    14:  686,
    17:  893,
    20:  1255,
    22:  1325,
    24:  1550,
    32:  2550,
    40:  9_999_999,
}


def preprocess_ftl_splits(
    attr: dict[str, dict[str, Any]],
    mh_name: str,
    mh_cfg: "MHConfig",
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    residual_threshold: float,
) -> tuple[dict[str, dict[str, Any]], list[dict], list[dict], list[dict], list[str]]:
    """Split DHs whose demand > ML gate capacity into FTL dedicated trucks + milkrun residual.

    Returns (milkrun_attr, ftl_assignment_rows, ftl_expanded_rows, absorbed_list, val_lines).
    milkrun_attr has absorbed DHs removed; partial-FTL DHs have demand reduced to residual.
    """
    local_zonal_thresh = cfg["local_zonal_distance_threshold_km"]

    milkrun_attr   = {k: dict(v) for k, v in attr.items()}
    ftl_assignment: list[dict] = []
    ftl_expanded:   list[dict] = []
    absorbed_list:  list[dict] = []
    detail_lines:   list[str]  = []
    absorbed_dhs:   set[str]   = set()
    route_id = 1

    for dh in [d for d in attr if d != mh_name]:
        a       = attr[dh]
        demand  = a["demand"]
        ml_size = a["ML"]

        ml_capacity = ML_VEHICLE_CAPACITY.get(ml_size)
        if ml_capacity is None:
            known       = sorted(ML_VEHICLE_CAPACITY.keys())
            ml_capacity = ML_VEHICLE_CAPACITY[min(known, key=lambda k: abs(k - ml_size))]

        if demand <= ml_capacity:
            continue

        n_ftl    = int(demand // ml_capacity)
        residual = demand - n_ftl * ml_capacity

        d_fwd = get_distance(mh_name, dh, dist_dict, latlong)
        d_bck = get_distance(dh, mh_name, dist_dict, latlong)
        if d_fwd is None or d_bck is None:
            detail_lines.append(
                f"  WARN FTL: no distance {mh_name}↔{dh} — keeping as milkrun"
            )
            continue

        d_direct       = d_fwd + d_bck
        rate_card      = mh_cfg.local_rate_card if d_direct <= local_zonal_thresh else mh_cfg.zonal_rate_card
        local_or_zonal = "local" if d_direct <= local_zonal_thresh else "zonal"
        cost_per_truck = d_direct * rate_card.get(ml_size, 999) * 30

        dep_time     = a.get("depot_departure", cfg["default_depot_departure_min"])
        tw_start     = a.get("time_window_start", 0)
        arr_dh       = dep_time + get_transit_time(d_fwd)
        dep_dh       = max(arr_dh, tw_start) + mh_cfg.service_time_min
        arr_mh_return = dep_dh + get_transit_time(d_bck)

        for _ in range(n_ftl):
            route_seq = f"{mh_name} -> {dh} -> {mh_name}"
            ftl_assignment.append({
                "MH":                      mh_name,
                "Route_ID":                route_id,
                "route_sequence":          route_seq,
                "hubs":                    [dh],
                "dist":                    d_direct,
                "group":                   "FTL",
                "monthly_cost":            cost_per_truck,
                "Freq":                    1,
                "total_demand":            ml_capacity,
                "assigned_vehicle_length": ml_size,
                "local_or_zonal":          local_or_zonal,
                "Route_Type":              "FTL_Dedicated",
                "arrival_times":           str(round(arr_dh, 2)),
                "departure_times":         str(round(dep_dh, 2)),
                "updated_depot_departure": round(dep_time, 2),
            })
            for loc, arr, dep in [
                (mh_name, float("nan"), round(dep_time, 2)),
                (dh,      round(arr_dh, 2),       round(dep_dh, 2)),
                (mh_name, round(arr_mh_return, 2), float("nan")),
            ]:
                ftl_expanded.append({
                    "MH":             mh_name,
                    "Route_ID":       route_id,
                    "Location":       loc,
                    "Arrival_Time":   arr,
                    "Departure_Time": dep,
                    "Freq":           1,
                    "Vehicle_Length": ml_size,
                    "Total_Demand":   ml_capacity,
                    "Route_Sequence": route_seq,
                    "Route_Type":     "FTL_Dedicated",
                })
            route_id += 1

        absorbed = residual == 0 or residual < residual_threshold
        if absorbed:
            absorbed_dhs.add(dh)
            absorbed_list.append({
                "MH":                mh_name,
                "DH":                dh,
                "original_demand":   demand,
                "ML":                ml_size,
                "ml_capacity":       ml_capacity,
                "n_ftl_trucks":      n_ftl,
                "residual_cft":      round(residual, 2),
                "residual_threshold": residual_threshold,
            })
            detail_lines.append(
                f"  FTL {dh}: demand={demand:.0f} ML={ml_size}ft "
                f"→ {n_ftl} truck(s), residual={residual:.0f} absorbed (<{residual_threshold:.0f})"
            )
        else:
            milkrun_attr[dh]["demand"] = residual
            detail_lines.append(
                f"  FTL {dh}: demand={demand:.0f} ML={ml_size}ft "
                f"→ {n_ftl} truck(s), residual={residual:.0f} → milkrun"
            )

    for dh in absorbed_dhs:
        milkrun_attr.pop(dh, None)

    n_ftl_dhs    = len({r["hubs"][0] for r in ftl_assignment}) if ftl_assignment else 0
    summary_line = (
        f"  FTL pre-processing: {n_ftl_dhs} DH(s) split into "
        f"{len(ftl_assignment)} FTL truck(s), {len(absorbed_dhs)} residual(s) absorbed"
    )
    return milkrun_attr, ftl_assignment, ftl_expanded, absorbed_list, [summary_line] + detail_lines


# ---------------------------------------------------------------------------
# Derived constraints
# ---------------------------------------------------------------------------

def derive_freq_allowed(top266_load: float) -> int:
    """Depot (MH): always 1. DH: 1 if top266_load == 0 (freq-2 allowed), else 0."""
    return 1 if top266_load == 0 else 0


def derive_allowed_positions(
    top266_load: float,
    threshold_a: float,
    threshold_b: float,
) -> Optional[set[int]]:
    """None = no constraint; {1,2} = positions 1 or 2 only; {1} = must be first stop."""
    if top266_load < threshold_a:
        return None
    if top266_load <= threshold_b:
        return {1, 2}
    return {1}


# ---------------------------------------------------------------------------
# Bearing clustering
# ---------------------------------------------------------------------------

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    delta_lon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(delta_lon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(delta_lon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def assign_bearing_clusters(
    destinations: pd.DataFrame,
    depot_lat: float,
    depot_lon: float,
    max_hops: int,
    max_comb_limit: int,
) -> pd.DataFrame:
    """Sort DHs by bearing from depot; split into k groups where total permutations ≤ max_comb_limit."""
    dests = destinations.copy()
    dests["_bearing"] = dests.apply(
        lambda r: calculate_bearing(depot_lat, depot_lon, r["_lat"], r["_lon"]), axis=1
    )
    dests = dests.sort_values("_bearing").reset_index(drop=True)
    n = len(dests)
    selected_k = 1
    for k in range(1, n + 1):
        group_ids  = np.array_split(np.arange(n), k)
        total_comb = sum(math.perm(len(g), min(len(g), max_hops)) for g in group_ids)
        if total_comb <= max_comb_limit:
            labels = np.zeros(n, dtype=int)
            for gid, idxs in enumerate(group_ids):
                labels[idxs] = gid
            dests["_bearing_group"] = labels
            selected_k = k
            break
    logger.info("Bearing clusters: k=%d for %d DHs", selected_k, n)
    dests["_final_group"] = (
        dests["_depot_departure"].astype(str) + "-" + dests["_bearing_group"].astype(str)
    )
    return dests


# ---------------------------------------------------------------------------
# Per-MH pipeline
# ---------------------------------------------------------------------------

_ABSORBED_COLS = [
    "MH", "DH", "original_demand", "ML", "ml_capacity",
    "n_ftl_trucks", "residual_cft", "residual_threshold",
]


@dataclass
class Agent4MHResult:
    mh_name: str
    clustering_df: pd.DataFrame
    filtered_routes_df: pd.DataFrame
    final_assignment_df: pd.DataFrame
    expanded_schedule_df: pd.DataFrame
    validation_lines: list[str]
    total_monthly_cost: float
    n_clusters: int
    n_perms_checked: int
    n_routes_survived: int
    ilp_status: dict[str, str]   # cluster_id → "SUCCESS" | "FAILED"
    missing_dhs: list[str]
    absorbed_residuals_df: pd.DataFrame
    dh_summary_df: pd.DataFrame
    osrm_log: list = field(default_factory=list)


def run_agent4_for_mh(
    mh_name: str,
    mh_cfg: MHConfig,
    dh_df: pd.DataFrame,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    on_progress: Optional[Any] = None,
    residual_threshold: float = 100.0,
) -> Agent4MHResult:
    """Run the full routing pipeline for one MH.

    Parameters
    ----------
    mh_name   : MH depot name (must match dist_dict keys and latlong keys)
    mh_cfg    : MHConfig from load_rate_card
    dh_df     : rows for this MH only (columns as per Location File)
    dist_dict : mutable (origin, dest) → km; OSRM results are cached here
    latlong   : site_name → (lat, lon)
    cfg       : dict from load_agent4_config
    """
    # Create a local OSRM log; expose it via the contextvar so _osrm_distance_km
    # (called deep inside get_distance) can append to it without any signature change.
    osrm_log: list = []
    _token = _osrm_log_ctx.set(osrm_log)
    try:
        return _run_mh_body(
            mh_name, mh_cfg, dh_df, dist_dict, latlong, cfg,
            on_progress=on_progress,
            residual_threshold=residual_threshold,
            osrm_log=osrm_log,
        )
    finally:
        _osrm_log_ctx.reset(_token)


def _run_mh_body(
    mh_name: str,
    mh_cfg: MHConfig,
    dh_df: pd.DataFrame,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    on_progress: Optional[Any],
    residual_threshold: float,
    osrm_log: list,
) -> Agent4MHResult:
    """Inner body for run_agent4_for_mh; receives the pre-created osrm_log list."""
    val_lines: list[str] = [f"=== MH: {mh_name} ==="]

    def _emit(msg: str) -> None:
        val_lines.append(msg)
        if callable(on_progress):
            on_progress(msg)

    _emit(f"Starting {mh_name} ({len(dh_df)} DHs) ...")

    col_loc  = cfg["col_location_name"]
    col_dem  = cfg["col_demand"]
    col_t266 = cfg["col_top266_load"]
    col_ml   = cfg["col_ml"]

    dep_lat, dep_lon = latlong.get(mh_name, (None, None))
    if dep_lat is None:
        val_lines.append(f"  ERROR: No lat/long for depot {mh_name} — skipping MH.")
        return Agent4MHResult(
            mh_name=mh_name,
            clustering_df=pd.DataFrame(),
            filtered_routes_df=pd.DataFrame(),
            final_assignment_df=pd.DataFrame(),
            expanded_schedule_df=pd.DataFrame(),
            validation_lines=val_lines,
            total_monthly_cost=0.0,
            n_clusters=0,
            n_perms_checked=0,
            n_routes_survived=0,
            ilp_status={},
            missing_dhs=[],
            absorbed_residuals_df=pd.DataFrame(columns=_ABSORBED_COLS),
            dh_summary_df=pd.DataFrame(),
            osrm_log=osrm_log,
        )

    # Build per-DH attribute dict
    attr: dict[str, dict[str, Any]] = {}
    for _, row in dh_df.iterrows():
        loc  = str(row[col_loc]).strip()
        t266 = float(row[col_t266]) if pd.notna(row[col_t266]) else 0.0
        demand = float(row[col_dem]) if pd.notna(row[col_dem]) else 0.0
        ml_val = float(row[col_ml])  if pd.notna(row[col_ml])  else 40.0

        tw_start = (
            float(row["time_window_start"])
            if "time_window_start" in row.index and pd.notna(row.get("time_window_start"))
            else cfg["default_time_window_start_min"]
        )
        tw_end = (
            float(row["time_window_end"])
            if "time_window_end" in row.index and pd.notna(row.get("time_window_end"))
            else cfg["default_time_window_end_min"]
        )
        dep_dep = (
            float(row["depot_departure"])
            if "depot_departure" in row.index and pd.notna(row.get("depot_departure"))
            else cfg["default_depot_departure_min"]
        )

        ll = latlong.get(loc, (None, None))
        attr[loc] = {
            "demand":            demand,
            "top266_load":       t266,
            "ML":                ml_val,
            "time_window_start": tw_start,
            "time_window_end":   tw_end,
            "depot_departure":   dep_dep,
            "latitude":          ll[0],
            "longitude":         ll[1],
            "freq_allowed":      derive_freq_allowed(t266),
            "allowed_positions": derive_allowed_positions(t266, mh_cfg.threshold_a, mh_cfg.threshold_b),
        }

    attr[mh_name] = {
        "demand": 0, "top266_load": 0, "ML": 40,
        "latitude": dep_lat, "longitude": dep_lon,
        "time_window_start": 0, "time_window_end": 1440,
        "depot_departure":   cfg["default_depot_departure_min"],
        "freq_allowed": 1, "allowed_positions": None,
    }

    dh_names = [d for d in attr if d != mh_name]

    # FTL pre-processing
    original_attr      = {k: dict(v) for k, v in attr.items()}
    original_dh_names  = list(dh_names)

    attr, ftl_assignment_rows, ftl_expanded_rows, absorbed_list, ftl_val_lines = (
        preprocess_ftl_splits(attr, mh_name, mh_cfg, dist_dict, latlong, cfg, residual_threshold)
    )
    for msg in ftl_val_lines:
        _emit(msg)
    dh_names = [d for d in attr if d != mh_name]

    # STEP 1: Bearing clusters
    dests_rows = []
    for dh in dh_names:
        a = attr[dh]
        dests_rows.append({
            col_loc:            dh,
            "_lat":             a["latitude"] or dep_lat,
            "_lon":             a["longitude"] or dep_lon,
            "_depot_departure": a["depot_departure"],
        })
    dests_df = pd.DataFrame(dests_rows)

    _empty_cluster_cols = [
        col_loc, "_lat", "_lon", "_depot_departure",
        "_bearing", "_bearing_group", "_final_group",
    ]
    if dests_df.empty:
        dests_clustered = pd.DataFrame(columns=_empty_cluster_cols)
    else:
        dests_clustered = assign_bearing_clusters(
            dests_df, dep_lat, dep_lon, mh_cfg.max_hops, cfg["max_comb_limit"]
        )
    n_clusters = int(dests_clustered["_final_group"].nunique()) if not dests_clustered.empty else 0
    _emit(f"  Step 1 done: {n_clusters} bearing cluster(s) for {len(dh_names)} milkrun DH(s)")

    clustering_rows: list[dict[str, Any]] = []
    for dh in dh_names:
        rc = dests_clustered[dests_clustered[col_loc] == dh]
        fg = rc["_final_group"].values[0] if len(rc) else "0"
        clustering_rows.append({
            "MH":               mh_name,
            "location_name":    dh,
            "bearing_group":    rc["_bearing_group"].values[0] if len(rc) else 0,
            "final_group":      fg,
            "bearing":          rc["_bearing"].values[0] if len(rc) else 0,
            "demand":           attr[dh]["demand"],
            "top266_load":      attr[dh]["top266_load"],
            "ML":               attr[dh]["ML"],
            "freq_allowed":     attr[dh]["freq_allowed"],
            "allowed_positions": str(attr[dh]["allowed_positions"]),
        })

    # STEP 2: Permutation generation
    groups = dests_clustered["_final_group"].unique()
    raw_routes: list[dict[str, Any]] = []
    total_perms = 0

    for gid in groups:
        grp_hubs = [
            str(r[col_loc])
            for _, r in dests_clustered[dests_clustered["_final_group"] == gid].iterrows()
        ]
        for h in range(1, mh_cfg.max_hops + 1):
            for perm in itertools.permutations(grp_hubs, h):
                total_perms += 1
                valid = True

                # A. Allowed-positions check
                for idx, hub in enumerate(perm):
                    ap_set = attr[hub]["allowed_positions"]
                    if ap_set is not None and (idx + 1) not in ap_set:
                        valid = False
                        break
                if not valid:
                    continue

                # B. Time-window feasibility
                cur_time  = attr[perm[0]]["depot_departure"]
                sim_stops = [mh_name] + list(perm)
                for i in range(len(sim_stops) - 1):
                    o, d    = sim_stops[i], sim_stops[i + 1]
                    dist_km = dist_dict.get((o, d))
                    if dist_km is None:
                        dist_km = get_distance(o, d, dist_dict, latlong)
                    if dist_km is None:
                        valid = False
                        break
                    tt  = get_transit_time(dist_km)
                    arr = cur_time + tt
                    if arr > attr[d]["time_window_end"]:
                        valid = False
                        break
                    cur_time = max(arr, attr[d]["time_window_start"]) + mh_cfg.service_time_min
                if not valid:
                    continue

                # C. Total route distance
                route_seq = [mh_name] + list(perm) + [mh_name]
                d_total   = 0.0
                for i in range(len(route_seq) - 1):
                    km = dist_dict.get((route_seq[i], route_seq[i + 1]))
                    if km is None:
                        km = get_distance(route_seq[i], route_seq[i + 1], dist_dict, latlong)
                    if km is None:
                        valid = False
                        break
                    d_total += km
                if valid:
                    raw_routes.append({
                        "route_sequence": " -> ".join(route_seq),
                        "hubs":           list(perm),
                        "hubs_set":       set(perm),
                        "dist":           d_total,
                        "group":          gid,
                    })

    _emit(f"  Step 2 done: checked {total_perms:,} permutations → {len(raw_routes):,} survived filters")

    # STEP 3: Costing & domination pruning
    local_zonal_thresh = cfg["local_zonal_distance_threshold_km"]
    costed_map: dict[tuple[str, ...], dict[str, Any]] = {}

    for r in raw_routes:
        d_total   = r["dist"]
        rate_card = mh_cfg.local_rate_card if d_total <= local_zonal_thresh else mh_cfg.zonal_rate_card

        base_demand = sum(attr[h]["demand"] for h in r["hubs"])
        max_ml      = min(attr[h]["ML"] for h in r["hubs"])
        freq_ok     = all(attr[h]["freq_allowed"] == 1 for h in r["hubs"])

        v1 = max(assign_vehicle_length(base_demand),     mh_cfg.min_vehicle_ft)
        c1 = (d_total * rate_card.get(v1, 999)) * 30 if v1 <= max_ml else float("inf")
        c1 = max(c1, 90000) if c1 != float("inf") else float("inf")

        v2 = max(assign_vehicle_length(base_demand * 2), mh_cfg.min_vehicle_ft)
        c2 = (d_total * rate_card.get(v2, 999)) * 15 if freq_ok and v2 <= max_ml else float("inf")
        c2 = max(c2, 90000) if c2 != float("inf") else float("inf")

        if c1 == float("inf") and c2 == float("inf"):
            continue

        m_cost       = min(c1, 1.1 * c2)
        freq         = 2 if (1.1 * c2) < c1 else 1
        total_demand = base_demand * freq
        v_len        = max(assign_vehicle_length(total_demand), mh_cfg.min_vehicle_ft)

        h_key = tuple(sorted(r["hubs_set"]))
        if h_key not in costed_map or m_cost < costed_map[h_key]["monthly_cost"]:
            entry = dict(r)
            entry.update({
                "monthly_cost":            m_cost,
                "Freq":                    freq,
                "total_demand":            total_demand,
                "assigned_vehicle_length": v_len,
                "local_or_zonal":          "local" if d_total <= local_zonal_thresh else "zonal",
            })
            costed_map[h_key] = entry

    costed_routes = list(costed_map.values())
    _emit(f"  Step 3 done: {len(costed_routes):,} unique hub-sets after domination pruning")

    filtered_rows: list[dict[str, Any]] = []
    for r in costed_routes:
        row = {k: v for k, v in r.items() if k != "hubs_set"}
        row["MH"] = mh_name
        filtered_rows.append(row)

    # STEP 4: ILP set-cover
    final_assigned: list[dict[str, Any]] = []
    ilp_status:     dict[str, str]        = {}
    missing_dhs:    list[str]             = []

    for gid in groups:
        grp_routes = [r for r in costed_routes if r["group"] == gid]
        grp_hubs   = sorted({
            str(r[col_loc])
            for _, r in dests_clustered[dests_clustered["_final_group"] == gid].iterrows()
        })
        if not grp_hubs:
            continue

        prob   = LpProblem(f"Agent4_{mh_name}_{gid}", LpMinimize)
        rvars  = LpVariable.dicts("Route", range(len(grp_routes)), cat=LpBinary)
        prob  += lpSum(grp_routes[i]["monthly_cost"] * rvars[i] for i in range(len(grp_routes)))
        for hub in grp_hubs:
            prob += lpSum(
                rvars[i] for i in range(len(grp_routes)) if hub in grp_routes[i]["hubs_set"]
            ) == 1
        prob.solve(PULP_CBC_CMD(msg=0))

        gid_str = str(gid)
        if prob.status == 1:
            ilp_status[gid_str] = "SUCCESS"
            for i in range(len(grp_routes)):
                if rvars[i].varValue is not None and rvars[i].varValue > 0.5:
                    final_assigned.append(grp_routes[i])
        else:
            ilp_status[gid_str] = "FAILED"
            covered     = set().union(*(r["hubs_set"] for r in grp_routes))
            not_covered = [h for h in grp_hubs if h not in covered]
            missing_dhs.extend(not_covered)
            _emit(
                f"  WARN: ILP FAILED for cluster {gid}; "
                f"uncovered: {not_covered or 'none (infeasible cover)'}"
            )

    n_assigned   = len(final_assigned)
    milkrun_cost = sum(r["monthly_cost"] for r in final_assigned)
    ftl_cost     = sum(r["monthly_cost"] for r in ftl_assignment_rows)
    total_cost   = milkrun_cost + ftl_cost
    _emit(f"  Step 4 ILP done: {n_assigned} routes assigned | milkrun cost = ₹{milkrun_cost:,.0f}")

    # STEPS 6–9: Schedule expansion
    final_assignment_rows: list[dict[str, Any]] = []
    expanded_rows:         list[dict[str, Any]] = []

    for route_idx, r in enumerate(final_assigned):
        stops    = [s.strip() for s in r["route_sequence"].split("->")]
        base_dep = attr[stops[1]]["depot_departure"] if len(stops) > 1 else 0.0
        tmp_t    = base_dep
        r_times: list[dict[str, Any]] = []

        for i in range(len(stops) - 1):
            o, d = stops[i], stops[i + 1]
            km   = dist_dict.get((o, d)) or get_distance(o, d, dist_dict, latlong) or 0.0
            tt   = get_transit_time(km)
            arr  = tmp_t + tt
            if d != mh_name:
                svc = mh_cfg.service_time_min
                r_times.append({
                    "dh":  d,
                    "arr": arr,
                    "dep": max(arr, attr[d]["time_window_start"]) + svc,
                })
                tmp_t = max(arr, attr[d]["time_window_start"]) + svc
            else:
                r_times.append({"dh": d, "arr": arr, "dep": float("nan")})

        buffers = [
            attr[t["dh"]]["time_window_end"] - t["arr"]
            for t in r_times if t["dh"] != mh_name
        ]
        shift = max(0.0, min(buffers)) if buffers else 0.0

        arr_str = ":".join(str(round(t["arr"] + shift, 2)) for t in r_times)
        dep_str = ":".join(
            str(round(t["dep"] + shift, 2))
            for t in r_times
            if not (isinstance(t["dep"], float) and math.isnan(t["dep"]))
        )

        row_copy = {k: v for k, v in r.items() if k != "hubs_set"}
        row_copy.update({
            "MH":                      mh_name,
            "Route_ID":                route_idx + 1,
            "arrival_times":           arr_str,
            "departure_times":         dep_str,
            "updated_depot_departure": round(base_dep + shift, 2),
            "Route_Type":              "Milkrun",
        })
        final_assignment_rows.append(row_copy)

        for si, stop in enumerate(stops):
            ti = r_times[si - 1] if si > 0 else None
            expanded_rows.append({
                "MH":       mh_name,
                "Route_ID": route_idx + 1,
                "Location": stop,
                "Arrival_Time": float("nan") if si == 0 else round(ti["arr"] + shift, 2),
                "Departure_Time": (
                    round(base_dep + shift, 2)
                    if si == 0
                    else (
                        round(ti["dep"] + shift, 2)
                        if si < len(stops) - 1
                        and not (isinstance(ti["dep"], float) and math.isnan(ti["dep"]))
                        else float("nan")
                    )
                ),
                "Freq":           r["Freq"],
                "Vehicle_Length": r["assigned_vehicle_length"],
                "Total_Demand":   r["total_demand"],
                "Route_Sequence": r["route_sequence"],
                "Route_Type":     "Milkrun",
            })

    _emit(f"  Steps 6–9 done: schedule expanded for {n_assigned} milkrun routes")

    # Append FTL routes (offset IDs past milkrun range)
    n_milkrun = len(final_assignment_rows)
    for row in ftl_assignment_rows:
        r2 = dict(row); r2["Route_ID"] = n_milkrun + r2["Route_ID"]
        final_assignment_rows.append(r2)
    for row in ftl_expanded_rows:
        r2 = dict(row); r2["Route_ID"] = n_milkrun + r2["Route_ID"]
        expanded_rows.append(r2)

    # DH summary (one row per original DH)
    assigned_milkrun_dhs: set[str] = set()
    for r in final_assigned:
        assigned_milkrun_dhs.update(r.get("hubs_set", set()))

    dh_summary_rows: list[dict] = []
    for dh in original_dh_names:
        a_orig      = original_attr[dh]
        demand_orig = a_orig["demand"]
        ml_size     = a_orig["ML"]
        ml_cap      = ML_VEHICLE_CAPACITY.get(ml_size)
        if ml_cap is None:
            known  = sorted(ML_VEHICLE_CAPACITY.keys())
            ml_cap = ML_VEHICLE_CAPACITY[min(known, key=lambda k: abs(k - ml_size))]
        n_ftl    = int(demand_orig // ml_cap) if demand_orig > ml_cap else 0
        residual = (demand_orig - n_ftl * ml_cap) if n_ftl > 0 else 0.0
        is_abs   = any(ab["DH"] == dh for ab in absorbed_list)
        mkr_d    = 0.0 if is_abs else (residual if n_ftl > 0 else demand_orig)
        if n_ftl == 0:
            rt = "Milkrun"
        elif is_abs or residual == 0:
            rt = "FTL_Dedicated"
        else:
            rt = "FTL+Milkrun"
        dh_summary_rows.append({
            "MH":                   mh_name,
            "DH":                   dh,
            "original_demand":      round(demand_orig, 2),
            "ML":                   ml_size,
            "ml_capacity":          ml_cap,
            "n_ftl_trucks":         n_ftl,
            "residual_cft":         round(residual, 2),
            "residual_absorbed":    is_abs,
            "milkrun_demand_cft":   round(mkr_d, 2),
            "route_type":           rt,
            "in_milkrun_assignment": dh in assigned_milkrun_dhs,
        })

    _emit(
        f"  ✓ {mh_name} complete — DHs: {len(original_dh_names)} | "
        f"milkrun: {n_assigned} | FTL: {len(ftl_assignment_rows)} | "
        f"missing: {len(missing_dhs)} | total cost: ₹{total_cost:,.0f}"
    )

    return Agent4MHResult(
        mh_name=mh_name,
        clustering_df=pd.DataFrame(clustering_rows),
        filtered_routes_df=pd.DataFrame(filtered_rows),
        final_assignment_df=pd.DataFrame(final_assignment_rows),
        expanded_schedule_df=pd.DataFrame(expanded_rows),
        validation_lines=val_lines,
        total_monthly_cost=total_cost,
        n_clusters=n_clusters,
        n_perms_checked=total_perms,
        n_routes_survived=len(raw_routes),
        ilp_status=ilp_status,
        missing_dhs=missing_dhs,
        absorbed_residuals_df=(
            pd.DataFrame(absorbed_list)
            if absorbed_list
            else pd.DataFrame(columns=_ABSORBED_COLS)
        ),
        dh_summary_df=pd.DataFrame(dh_summary_rows),
        osrm_log=osrm_log,
    )


# ---------------------------------------------------------------------------
# Multi-city pipeline  (result dict)
# ---------------------------------------------------------------------------

def run_agent4_pipeline(
    location_file_df: pd.DataFrame,
    lat_long_df: Optional[pd.DataFrame] = None,
    dist_df: Optional[pd.DataFrame] = None,
    mhdh_rate_card_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    cfg: Optional[dict[str, Any]] = None,
    threshold_a_override: Optional[float] = None,
    threshold_b_override: Optional[float] = None,
    on_progress: Optional[Any] = None,
    residual_threshold: float = 100.0,
    # Legacy path params — used when DataFrames are not provided
    lat_long_path: Optional[Path] = None,
    distance_matrix_path: Optional[Path] = None,
    mh_rate_card_path: Optional[Path] = None,   # alias for mhdh_rate_card_path
) -> dict:
    """Run Agent 4 for all MHs in location_file_df; write outputs to out_dir.

    Accepts DataFrames (preferred) or file paths for lat_long and distance_matrix.
    DataFrame takes priority if both are supplied for the same input.

    Parameters
    ----------
    location_file_df     : DataFrame from build_location_file (or equivalent)
    lat_long_df          : Lat Longs DataFrame (Site_name, Latitude, Longitude)
    dist_df              : Distance Matrix DataFrame (S_Code, D_Code, distance)
    mhdh_rate_card_path  : path to MHDH_RateCard.xlsx  (also accepts mh_rate_card_path)
    out_dir              : directory to write all output CSV/TXT files
    cfg                  : dict from load_agent4_config (defaults used if None)
    threshold_a_override / threshold_b_override : when set, override every MH's
        per-row rate-card values (global UI inputs)
    lat_long_path        : legacy — path to Lat Longs file; ignored when lat_long_df provided
    distance_matrix_path : legacy — path to Distance Matrix file; ignored when dist_df provided
    mh_rate_card_path    : legacy alias for mhdh_rate_card_path

    Returns
    -------
    {"status": "ok" | "partial" | "failed",
     "data": {
         "per_mh":                   dict[str, Agent4MHResult],
         "clustering_df":            pd.DataFrame,
         "final_assignment_df":      pd.DataFrame,
         "expanded_schedule_df":     pd.DataFrame,
         "dh_route_summary_df":      pd.DataFrame,
         "absorbed_residuals_df":    pd.DataFrame,
         "osrm_fallback_df":         pd.DataFrame,
         "total_monthly_cost":       float,
         "validation_report":        str,
         "grand_total_monthly_cost": float,   (alias for total_monthly_cost)
         "n_mhs":                    int,
         "n_routes":                 int,
         "n_osrm_calls":             int,
         "out_dir":                  Path,
         "output_files":             dict[str, Path],
     },
     "issues": [...]}
    """
    issues: list[dict] = []

    # Resolve cfg default
    if cfg is None:
        cfg = load_agent4_config()

    # Resolve mhdh_rate_card_path alias
    if mhdh_rate_card_path is None and mh_rate_card_path is not None:
        mhdh_rate_card_path = Path(mh_rate_card_path)

    # Resolve DataFrames from paths when DataFrames not provided
    if lat_long_df is None:
        if lat_long_path is not None:
            lat_long_df = _read_full(Path(lat_long_path))
        else:
            return {
                "status": "failed",
                "data":   None,
                "issues": [{"type": "missing_input",
                            "detail": "Provide lat_long_df or lat_long_path"}],
            }
    if dist_df is None:
        if distance_matrix_path is not None:
            dist_df = _read_full(Path(distance_matrix_path))
        else:
            return {
                "status": "failed",
                "data":   None,
                "issues": [{"type": "missing_input",
                            "detail": "Provide dist_df or distance_matrix_path"}],
            }
    if mhdh_rate_card_path is None:
        return {
            "status": "failed",
            "data":   None,
            "issues": [{"type": "missing_input",
                        "detail": "Provide mhdh_rate_card_path (or mh_rate_card_path)"}],
        }

    out_dir = Path(out_dir) if out_dir is not None else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build runtime lookups from DataFrames
    dist_result = build_distance_dict(dist_df)
    ll_result   = build_latlong_dict(lat_long_df)
    dist_dict   = dist_result["data"]
    latlong     = ll_result["data"]
    issues.extend(dist_result["issues"])
    issues.extend(ll_result["issues"])

    mh_configs = load_rate_card(mhdh_rate_card_path, cfg)

    col_mh  = cfg["col_mh_assignment"]
    col_loc = cfg["col_location_name"]
    loc_df  = location_file_df

    # Validate required columns
    for col in (cfg["col_location_name"], cfg["col_mh_assignment"],
                cfg["col_demand"], cfg["col_top266_load"], cfg["col_ml"]):
        if col not in loc_df.columns:
            return {
                "status": "failed",
                "data":   None,
                "issues": [{"type": "missing_column",
                            "detail": f"Location file missing required column: '{col}'"}],
            }

    all_mhs = sorted(loc_df[col_mh].dropna().unique())

    # Pre-scan OSRM for missing distance pairs; captured by a pipeline-level log
    prescan_log: list = []
    _prescan_token = _osrm_log_ctx.set(prescan_log)
    logger.info("Pre-scanning distance dict for missing MH→DH pairs ...")
    try:
        for mh in all_mhs:
            if mh not in latlong:
                continue
            for _, drow in loc_df[loc_df[col_mh] == mh].iterrows():
                dh = str(drow[col_loc]).strip()
                for o, d in [(mh, dh), (dh, mh)]:
                    if (o, d) not in dist_dict:
                        get_distance(o, d, dist_dict, latlong)
    finally:
        _osrm_log_ctx.reset(_prescan_token)

    # Main loop
    all_clustering: list[pd.DataFrame] = []
    all_filtered:   list[pd.DataFrame] = []
    all_assigned:   list[pd.DataFrame] = []
    all_expanded:   list[pd.DataFrame] = []
    per_mh: dict[str, Agent4MHResult]  = {}
    report_sections: list[str]         = ["Agent 4 — Multi-City Route Optimizer", "=" * 60, ""]

    total_start = time.time()
    for mh in all_mhs:
        mh_cfg = mh_configs.get(mh)
        if mh_cfg is None:
            report_sections.append(f"SKIP {mh}: not found in rate card — using defaults.")
            issues.append({"type": "mh_not_in_rate_card",
                           "detail": f"{mh} not in rate card; defaults applied"})
            mh_cfg = MHConfig(
                mh_name=mh,
                local_rate_card={},
                zonal_rate_card={},
                max_hops=cfg["default_max_hops"],
                threshold_a=cfg["default_threshold_a"],
                threshold_b=cfg["default_threshold_b"],
                service_time_min=cfg["default_service_time_min"],
            )

        if threshold_a_override is not None:
            mh_cfg = MHConfig(
                mh_name=mh_cfg.mh_name, city=mh_cfg.city, tag=mh_cfg.tag,
                local_rate_card=mh_cfg.local_rate_card, zonal_rate_card=mh_cfg.zonal_rate_card,
                max_hops=mh_cfg.max_hops, threshold_a=threshold_a_override,
                threshold_b=mh_cfg.threshold_b, service_time_min=mh_cfg.service_time_min,
            )
        if threshold_b_override is not None:
            mh_cfg = MHConfig(
                mh_name=mh_cfg.mh_name, city=mh_cfg.city, tag=mh_cfg.tag,
                local_rate_card=mh_cfg.local_rate_card, zonal_rate_card=mh_cfg.zonal_rate_card,
                max_hops=mh_cfg.max_hops, threshold_a=mh_cfg.threshold_a,
                threshold_b=threshold_b_override, service_time_min=mh_cfg.service_time_min,
            )

        dh_df = loc_df[loc_df[col_mh] == mh].copy().reset_index(drop=True)
        if len(dh_df) == 0:
            report_sections.append(f"SKIP {mh}: 0 DHs in location file.")
            continue

        logger.info("Running MH: %s (%d DHs)", mh, len(dh_df))
        if callable(on_progress):
            on_progress(f"--- MH: {mh} ({len(dh_df)} DHs) ---")

        result = run_agent4_for_mh(
            mh, mh_cfg, dh_df, dist_dict, latlong, cfg,
            on_progress=on_progress,
            residual_threshold=residual_threshold,
        )
        per_mh[mh] = result

        report_sections.extend(result.validation_lines)
        report_sections.append("")

        if not result.clustering_df.empty:
            all_clustering.append(result.clustering_df)
        if not result.filtered_routes_df.empty:
            all_filtered.append(result.filtered_routes_df)
        if not result.final_assignment_df.empty:
            all_assigned.append(result.final_assignment_df)
        if not result.expanded_schedule_df.empty:
            all_expanded.append(result.expanded_schedule_df)

    elapsed = time.time() - total_start

    def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    clustering_out = _concat(all_clustering)
    filtered_out   = _concat(all_filtered)
    assigned_out   = _concat(all_assigned)
    expanded_out   = _concat(all_expanded)

    # Aggregate OSRM log: prescan entries + all per-MH entries
    all_osrm_entries = list(prescan_log)
    for r in per_mh.values():
        all_osrm_entries.extend(r.osrm_log)
    osrm_df = (
        pd.DataFrame(all_osrm_entries)
        if all_osrm_entries
        else pd.DataFrame(columns=["origin", "destination", "distance_km", "transit_minutes"])
    )

    all_dh_summary = [r.dh_summary_df       for r in per_mh.values() if not r.dh_summary_df.empty]
    all_absorbed   = [r.absorbed_residuals_df for r in per_mh.values() if not r.absorbed_residuals_df.empty]
    dh_summary_out = _concat(all_dh_summary)
    absorbed_out_  = _concat(all_absorbed)

    # Write output files
    files: dict[str, Path] = {
        "clustering":         out_dir / "Clustering_Output.csv",
        "filtered_routes":    out_dir / "Filtered_Routes.csv",
        "final_assignment":   out_dir / "Final_Assignment.csv",
        "expanded_schedule":  out_dir / "Expanded_Schedule.csv",
        "osrm_fallback":      out_dir / "osrm_fallback_log.csv",
        "dh_summary":         out_dir / "DH_Route_Summary.csv",
        "absorbed_residuals": out_dir / "Absorbed_Residuals.csv",
        "validation_report":  out_dir / "validation_report_agent4.txt",
    }
    clustering_out.to_csv(files["clustering"],         index=False)
    filtered_out.to_csv(  files["filtered_routes"],    index=False)
    assigned_out.to_csv(  files["final_assignment"],   index=False)
    expanded_out.to_csv(  files["expanded_schedule"],  index=False)
    osrm_df.to_csv(       files["osrm_fallback"],      index=False)
    dh_summary_out.to_csv(files["dh_summary"],         index=False)
    absorbed_out_.to_csv( files["absorbed_residuals"],  index=False)

    grand_cost = sum(r.total_monthly_cost for r in per_mh.values())
    total_ftl  = (
        int(dh_summary_out["n_ftl_trucks"].sum())
        if not dh_summary_out.empty and "n_ftl_trucks" in dh_summary_out.columns
        else 0
    )
    total_abs  = len(absorbed_out_) if not absorbed_out_.empty else 0
    n_routes   = len(assigned_out)  if not assigned_out.empty  else 0

    report_sections += [
        "=" * 60,
        f"MHs processed: {len(per_mh)}",
        f"OSRM fallback calls: {len(all_osrm_entries)}",
        f"Elapsed: {elapsed:.1f}s",
        f"Grand total monthly cost: ₹{grand_cost:,.0f}",
        "",
        f"FTL summary: {total_ftl} total FTL truck(s), {total_abs} residual(s) absorbed",
        f"FTL residual threshold: {residual_threshold:.0f} CFT",
        "",
    ]
    if threshold_a_override is not None or threshold_b_override is not None:
        report_sections += [
            "Threshold overrides applied:",
            f"  threshold_a={threshold_a_override}, threshold_b={threshold_b_override} "
            "(set via UI — supersede per-MH rate card values for all cities)",
            "",
        ]
    report_sections += [
        "Vehicle size 40 note:",
        "  assign_vehicle_length() may return 40 for demand>2550, but 40 is not in",
        "  the rate card.  rate_card.get(40, 999) returns 999 → very high cost →",
        "  no 40-ft vehicle routes are assigned.  Cap at 32 ft if needed in config.",
    ]
    validation_report = "\n".join(report_sections)
    files["validation_report"].write_text(validation_report, encoding="utf-8")
    logger.info("Agent 4 complete. Grand cost: ₹%.0f", grand_cost)

    return {
        "status": "partial" if issues else "ok",
        "data": {
            # DataFrames — accessible directly without reading files from disk
            "per_mh":                   per_mh,
            "clustering_df":            clustering_out,
            "final_assignment_df":      assigned_out,
            "expanded_schedule_df":     expanded_out,
            "dh_route_summary_df":      dh_summary_out,
            "absorbed_residuals_df":    absorbed_out_,
            "osrm_fallback_df":         osrm_df,
            "total_monthly_cost":       grand_cost,
            "validation_report":        validation_report,
            # Scalar summaries
            "grand_total_monthly_cost": grand_cost,   # alias for total_monthly_cost
            "n_mhs":                    len(per_mh),
            "n_routes":                 n_routes,
            "n_osrm_calls":             len(all_osrm_entries),
            "out_dir":                  out_dir,
            "output_files":             files,
        },
        "issues": issues,
    }
