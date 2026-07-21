"""
Agent 3 — composable tool library for DH-to-FC_MH clustering.

All public functions return a standard result dict:
    {"status": "ok" | "partial" | "failed",
     "data":   <function-specific value>,
     "issues": [{"type": str, "detail": str}, ...]}

No orchestrator imports.  No sys.exit().  No file-path construction.
Caller passes DataFrames in; caller passes output_dir for writing outputs.
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_DEFAULTS: dict[str, Any] = {
    "truck_speed_kmh": 30.0,
    "mh_dh_processing_hours": 2.0,
    "mh_mh_processing_hours": 6.0,
    "truck_cft_mh_mh": 2400.0,
    "truck_cft_mh_dh_base": 1500.0,
    "mh_dh_cost_rs_per_km": 26.0,
    "dh_arrival_cutoff_hour": 6.0,
    "default_top266_threshold": 5.0,
    "plan_fbf_master_sheet": None,
    "lat_long_sheet": None,
    "fc_mh_tag_value": "FC_MH",
    "use_osrm_fallback": True,
    "osrm_base_url": "http://router.project-osrm.org",
    "osrm_request_timeout_s": 8,
    "osrm_rate_limit_s": 0.15,
    "osrm_batch_workers": 4,
    "mh_mh_cost_per_km_fallback": 49.0,
    "default_proximity_km_threshold": 80.0,
    "mh_dh_cost_buffer": 1.15,
}


def load_agent3_config(path: Optional[Path] = None) -> dict[str, Any]:
    """Load agent3_config.json; fall back to defaults for any missing keys."""
    cfg = dict(_CONFIG_DEFAULTS)
    if path and Path(path).is_file():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in cfg:
                        cfg[k] = v
        except json.JSONDecodeError:
            pass
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers  (business-logic faithful copies from agent3_pipeline.py)
# ─────────────────────────────────────────────────────────────────────────────

def _norm_hub(s: Any) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip().upper()


def _norm_hub_key(s: Any) -> str:
    t = _norm_hub(s)
    return re.sub(r"\s+", "", t)


def _is_real_central_hub(val: Any) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    t = str(val).strip().upper()
    if not t:
        return False
    if t.startswith("NO P"):
        return False
    return True


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _fetch_osrm_distance_km(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    *,
    base_url: str = "http://router.project-osrm.org",
    timeout: int = 8,
) -> Optional[float]:
    url = (
        f"{base_url.rstrip('/')}/route/v1/driving/"
        f"{lon1},{lat1};{lon2},{lat2}?overview=false"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Agent3/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != "Ok":
            return None
        routes = data.get("routes")
        if not routes:
            return None
        return round(float(routes[0]["distance"]) / 1000.0, 3)
    except Exception:
        return None


def _batch_fetch_osrm_pairs(
    pairs: set[tuple[str, str]],
    hub_lat_lkp: dict[str, tuple[float, float]],
    dist_lookup: dict[tuple[str, str], float],
    cfg: dict[str, Any],
    *,
    max_workers: int = 4,
    emit: Optional[Callable[[str], None]] = None,
) -> int:
    to_fetch = [
        (a, b) for a, b in pairs
        if a in hub_lat_lkp and b in hub_lat_lkp
        and (a, b) not in dist_lookup and (b, a) not in dist_lookup
    ]
    if not to_fetch:
        return 0
    base_url = cfg.get("osrm_base_url", "http://router.project-osrm.org")
    timeout = int(cfg.get("osrm_request_timeout_s", 8))
    if emit:
        emit(f"  OSRM batch: fetching {len(to_fetch)} missing pairs (workers={max_workers})…")
    fetched = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {}
        for (a, b) in to_fetch:
            pos_a, pos_b = hub_lat_lkp[a], hub_lat_lkp[b]
            fut = pool.submit(
                _fetch_osrm_distance_km,
                pos_a[0], pos_a[1], pos_b[0], pos_b[1],
                base_url=base_url, timeout=timeout,
            )
            fut_map[fut] = (a, b)
        for fut in as_completed(fut_map):
            a, b = fut_map[fut]
            d = fut.result()
            if d is not None:
                dist_lookup[(a, b)] = d
                fetched += 1
    if emit:
        emit(f"  OSRM batch: resolved {fetched}/{len(to_fetch)} pairs")
    return fetched


def _parse_pct(val: Any) -> float:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        x = float(val)
        return x if x <= 1.0 else x / 100.0
    s = str(val).strip().replace("%", "")
    try:
        x = float(s)
        return x / 100.0 if x > 1.0 else x
    except ValueError:
        return 0.0


def _distance_km(
    lookup: dict[tuple[str, str], float],
    a: Any,
    b: Any,
    *,
    missing_pairs: Optional[list] = None,
    reverse_reason: str = "distance_reverse_only",
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
    osrm_log: Optional[list] = None,
    osrm_cfg: Optional[dict[str, Any]] = None,
) -> Optional[float]:
    ka, kb = _norm_hub_key(a), _norm_hub_key(b)
    if not ka or not kb:
        return None
    if ka == kb:
        return 0.0
    d = lookup.get((ka, kb))
    if d is not None:
        return d
    d = lookup.get((kb, ka))
    if d is not None:
        if missing_pairs is not None:
            missing_pairs.append((a, b, reverse_reason))
        return d
    if hub_lat_lkp is not None:
        pos_a = hub_lat_lkp.get(ka)
        pos_b = hub_lat_lkp.get(kb)
        if pos_a and pos_b:
            cfg = osrm_cfg or {}
            d_osrm = _fetch_osrm_distance_km(
                pos_a[0], pos_a[1], pos_b[0], pos_b[1],
                base_url=cfg.get("osrm_base_url", "http://router.project-osrm.org"),
                timeout=int(cfg.get("osrm_request_timeout_s", 8)),
            )
            if d_osrm is not None:
                lookup[(ka, kb)] = d_osrm
                if osrm_log is not None:
                    osrm_log.append((ka, kb, d_osrm))
                rate = float(cfg.get("osrm_rate_limit_s", 0.15))
                if rate > 0:
                    time.sleep(rate)
                return d_osrm
    return None


def _plan_row_uses_mh_mh_rate_card(source_type: Any) -> bool:
    if source_type is None or (isinstance(source_type, float) and pd.isna(source_type)):
        return True
    s = str(source_type).strip()
    if not s or str(s).lower() == "nan":
        return True
    u = re.sub(r"\s+", "", s.upper())
    return u in ("FC_MH", "FCMH", "MH")


def _extract_hops_from_plan_row(row: pd.Series, mh_cols: list[str]) -> list[str]:
    hops: list[str] = []
    for c in mh_cols:
        key_col = f"_k_{c}"
        if key_col in row.index:
            k = row[key_col]
            if not k:
                break
        else:
            if c not in row.index:
                break
            v = row[c]
            if pd.isna(v) or str(v).strip() == "" or str(v).strip().lower() == "nan":
                break
            k = _norm_hub_key(v)
        if not hops or hops[-1] != k:
            hops.append(k)
    if "last_mh" in row.index and pd.notna(row["last_mh"]):
        k = _norm_hub_key(row["last_mh"])
        if k and (not hops or hops[-1] != k):
            hops.append(k)
    return hops


def _pathway_mh_key(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return ""
    su = re.sub(r"\s+", "", s.upper())
    if su.startswith("NOP") and len(su) <= 5:
        return ""
    return _norm_hub_key(val)


def _pick_pathway_row_for_p1(
    pathway: pd.DataFrame,
    p1_key: str,
    p1c: str,
    _p1_index: Optional[dict[str, Any]] = None,
) -> Optional[pd.Series]:
    if _p1_index is not None:
        row_idx = _p1_index.get(p1_key)
        if row_idx is None:
            return None
        return pathway.loc[row_idx]
    m = pathway[p1c].map(_norm_hub_key) == p1_key
    sub = pathway.loc[m]
    return sub.iloc[0] if len(sub) > 0 else None


def _build_pathway_p1_index(pathway: pd.DataFrame, p1c: str) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for row_idx, raw_val in pathway[p1c].items():
        nk = _norm_hub_key(raw_val)
        if nk not in index:
            index[nk] = row_idx
    return index


def _resolve_smh_to_dmh(
    smh_key: str,
    dmh_x_key: str,
    dmh_y_key: str,
    original_hops: list[str],
    route_lookup: dict[tuple[str, str], list[str]],
) -> list[str]:
    if smh_key == dmh_y_key:
        return [smh_key]
    found = route_lookup.get((smh_key, dmh_y_key))
    if found:
        return found
    base = list(original_hops)
    if not base or base[-1] == dmh_y_key:
        return base
    return base + [dmh_y_key]


def _trip_cost_with_fallback(
    u: str,
    v: str,
    cost_lookup: dict[tuple[str, str], float],
    dist_lookup: Optional[dict[tuple[str, str], float]],
    mh_mh_cost_per_km: float,
    missing_pairs: list,
    mh_mh_est_log: Optional[list],
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]],
    osrm_log: Optional[list],
    osrm_cfg: Optional[dict[str, Any]],
) -> Optional[float]:
    trip = cost_lookup.get((u, v)) or cost_lookup.get((v, u))
    if trip is not None:
        return float(trip)
    if dist_lookup is not None and mh_mh_cost_per_km > 0:
        d = _distance_km(
            dist_lookup, u, v,
            hub_lat_lkp=hub_lat_lkp,
            osrm_log=osrm_log,
            osrm_cfg=osrm_cfg,
        )
        if d is not None:
            est = d * mh_mh_cost_per_km
            if mh_mh_est_log is not None:
                mh_mh_est_log.append((u, v, d, est))
            return est
    missing_pairs.append((u, v, "mh_mh_cost"))
    return None


def _compute_mh_mh_cost_for_candidate(
    plan_vol: pd.DataFrame,
    dh_key: str,
    candidate_key: str,
    cost_lookup: dict[tuple[str, str], float],
    mh_cols: list[str],
    dh_col: str,
    missing_pairs: list,
    truck_cft_mh_mh: float,
    *,
    plan_slice: Optional[pd.DataFrame] = None,
    route_lookup: Optional[dict[tuple[str, str], list[str]]] = None,
    p2_hub_key: str = "",
    p2_inv: float = 0.0,
    fbf_cft: float = 0.0,
    dist_lookup: Optional[dict] = None,
    hub_lat_lkp: Optional[dict] = None,
    osrm_log: Optional[list] = None,
    osrm_cfg: Optional[dict] = None,
    mh_mh_cost_per_km: float = 0.0,
    mh_mh_est_log: Optional[list] = None,
) -> tuple[float, bool]:
    total = 0.0
    ok = True
    _rlkp = route_lookup or {}
    _cost_kw = dict(
        cost_lookup=cost_lookup,
        dist_lookup=dist_lookup,
        mh_mh_cost_per_km=mh_mh_cost_per_km,
        missing_pairs=missing_pairs,
        mh_mh_est_log=mh_mh_est_log,
        hub_lat_lkp=hub_lat_lkp,
        osrm_log=osrm_log,
        osrm_cfg=osrm_cfg,
    )

    sub = plan_slice if plan_slice is not None else plan_vol[plan_vol[dh_col].map(_norm_hub_key) == dh_key]
    use_rate_card_col = "source_type" in plan_vol.columns

    for _, row in sub.iterrows():
        stream = str(row.get("stream", "")).strip().upper()
        if stream == "FBF":
            continue
        hops = _extract_hops_from_plan_row(row, mh_cols)
        if not hops:
            continue
        _cft_raw = pd.to_numeric(row.get("plan_median_cft_volume", 0), errors="coerce")
        cft = 0.0 if (pd.isna(_cft_raw) or _cft_raw is None) else float(_cft_raw)
        _ship_raw = pd.to_numeric(row.get("median_demand_shipments", 0), errors="coerce")
        ship = 0.0 if (pd.isna(_ship_raw) or _ship_raw is None) else float(_ship_raw)
        if cft <= 0 and ship <= 0:
            continue
        smh_key = hops[0]
        dmh_x_key = hops[-1]
        resolved = _resolve_smh_to_dmh(smh_key, dmh_x_key, candidate_key, hops, _rlkp)
        edges = [(resolved[i], resolved[i + 1]) for i in range(len(resolved) - 1)]
        if not edges:
            continue
        zero_first = use_rate_card_col and not _plan_row_uses_mh_mh_rate_card(row.get("source_type"))
        for ei, (u, v) in enumerate(edges):
            if zero_first and ei == 0:
                continue
            trip = _trip_cost_with_fallback(u, v, **_cost_kw)
            if trip is None:
                ok = False
                continue
            total += (cft / max(truck_cft_mh_mh, 1e-9)) * trip

    # FBF P2 leg
    if p2_inv > 0.0 and p2_hub_key and fbf_cft > 0.0:
        p2_key = _norm_hub_key(p2_hub_key)
        p2_vol_cft = p2_inv * fbf_cft
        if p2_key and p2_key != candidate_key:
            p2_route = _rlkp.get((p2_key, candidate_key))
            p2_hops = p2_route if p2_route else [p2_key, candidate_key]
            for u, v in [(p2_hops[i], p2_hops[i + 1]) for i in range(len(p2_hops) - 1)]:
                trip = _trip_cost_with_fallback(u, v, **_cost_kw)
                if trip is None:
                    ok = False
                    continue
                total += (p2_vol_cft / max(truck_cft_mh_mh, 1e-9)) * trip

    return total, ok


def _compute_mh_dh_cost_raw(
    dh_key: str,
    candidate_key: str,
    total_cft: float,
    dist_lookup: dict,
    cfg: dict,
    missing_pairs: list,
    *,
    hub_lat_lkp: Optional[dict] = None,
    osrm_log: Optional[list] = None,
) -> tuple[float, bool]:
    d = _distance_km(
        dist_lookup,
        candidate_key,
        dh_key,
        missing_pairs=missing_pairs,
        reverse_reason="mh_dh_cost_reverse_only",
        hub_lat_lkp=hub_lat_lkp,
        osrm_log=osrm_log,
        osrm_cfg=cfg,
    )
    if d is None:
        missing_pairs.append((candidate_key, dh_key, "mh_dh_cost"))
        return 0.0, False
    base = float(cfg["truck_cft_mh_dh_base"])
    rate = float(cfg["mh_dh_cost_rs_per_km"])
    cost = (max(total_cft, 0.0) / base) * 2.0 * d * rate
    return float(cost), True


def _compute_speed_metrics_raw(
    dh_key: str,
    p1_hub_key: str,
    p2_hub_key: str,
    p1_inv: float,
    p2_inv: float,
    dist_lookup: dict,
    load_fn: Callable[[float], float],
    cfg: dict,
    *,
    missing: list,
    hub_lat_lkp: Optional[dict] = None,
    osrm_log: Optional[list] = None,
) -> tuple[float, bool, float, float]:
    p1_inv = max(0.0, min(1.0, float(p1_inv)))
    p2_inv = max(0.0, min(1.0, float(p2_inv)))
    if p1_inv <= 0.0 and p2_inv <= 0.0:
        return 0.0, True, 0.0, 0.0
    v_kmh = float(cfg["truck_speed_kmh"])
    t_mh_dh = float(cfg["mh_dh_processing_hours"])
    t_mh_mh = float(cfg["mh_mh_processing_hours"])
    h_cut_abs = float(cfg["dh_arrival_cutoff_hour"]) + 24.0
    _kw = dict(hub_lat_lkp=hub_lat_lkp, osrm_log=osrm_log, osrm_cfg=cfg)
    d_p1_dh = _distance_km(dist_lookup, p1_hub_key, dh_key, **_kw)
    if d_p1_dh is None:
        missing.append((p1_hub_key, dh_key, "speed_p1_dh"))
        return 0.0, False, 0.0, 0.0
    depart_p1 = h_cut_abs - d_p1_dh / v_kmh - t_mh_dh
    d1_p1 = load_fn(depart_p1)
    d1_p2 = 0.0
    if p2_inv > 0.0 and p2_hub_key:
        d_p2_p1 = _distance_km(dist_lookup, p2_hub_key, p1_hub_key, **_kw)
        if d_p2_p1 is None:
            missing.append((p2_hub_key, p1_hub_key, "speed_p2_p1"))
            return max(0.0, min(1.0, d1_p1 * p1_inv)), False, d1_p1 * p1_inv, 0.0
        depart_p2 = h_cut_abs - d_p2_p1 / v_kmh - t_mh_mh - d_p1_dh / v_kmh - t_mh_dh
        d1_p2 = load_fn(depart_p2)
    p1_contrib = d1_p1 * p1_inv
    p2_contrib = d1_p2 * p2_inv
    final_d1 = max(0.0, min(1.0, p1_contrib + p2_contrib))
    return final_d1, True, p1_contrib, p2_contrib


TOP266_COLS = [
    "fbf_avg_daily_5sc_top16_pin",
    "fbf_avg_daily_sha_top16_pin",
    "fbf_avg_daily_5sc_next50_pin",
    "fbf_avg_daily_sha_next50_pin",
    "fbf_avg_daily_5sc_next200_pin",
    "fbf_avg_daily_sha_next200_pin",
]
LBU_SHIP_COLS = ["fbf_avg_daily_shipments_5sc_core", "fbf_avg_daily_shipments_sha_core"]


def _make_error_assign_row(
    prow: pd.Series,
    reason: str,
    cand_names: Optional[list[str]] = None,
    cand_cost_map: Optional[dict] = None,
    cand_mhmh_map: Optional[dict] = None,
    cand_mhdh_map: Optional[dict] = None,
) -> dict[str, Any]:
    names = (cand_names or []) + ["", "", "", ""]
    names = names[:4]
    row: dict[str, Any] = {
        "destination_hub_key": str(prow.get("destination_hub_key", "")),
        "assigned_fc_mh": "",
        "assignment_basis": "error",
        "final_d1_pct": None,
        "p1_d1_pct": None,
        "p2_d1_pct": None,
        "d1_shipments_equiv": None,
        "mh_mh_cost_rs": None,
        "mh_dh_cost_rs": None,
        "total_cost_rs": None,
        "current_fc_mh": None,
        "current_fc_cost_rs": None,
        "cost_delta_rs": None,
        "top266_shipments": prow.get("top266_shipments"),
        "total_shipments": None,
        "total_cft": prow.get("total_dh_cft"),
        "fbf_shipments": prow.get("fbf_shipments"),
        "nfbf_shipments": prow.get("nfbf_shipments"),
        "alphalite_shipments": prow.get("alphalite_shipments"),
        "notes": reason,
    }
    for i, nm in enumerate(names, 1):
        row[f"candidate_{i}"] = nm
        row[f"candidate_{i}_cost_rs"] = (cand_cost_map or {}).get(nm)
        row[f"candidate_{i}_mhmh_cost_rs"] = (cand_mhmh_map or {}).get(nm)
        row[f"candidate_{i}_mhdh_cost_rs"] = (cand_mhdh_map or {}).get(nm)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Lookup builders  (public — return result dicts)
# ─────────────────────────────────────────────────────────────────────────────

def build_distance_lookup(dist_df: pd.DataFrame) -> dict[str, Any]:
    """Build (S_Code, D_Code) → km lookup from distance matrix DataFrame."""
    issues: list[dict] = []
    try:
        sc = next((dist_df[c] for c in dist_df.columns if _norm_hub_key(c) in ("S_CODE", "SCODE")), None)
        dc = next((dist_df[c] for c in dist_df.columns if _norm_hub_key(c) in ("D_CODE", "DCODE")), None)
        dist_col = next(
            (dist_df[c] for c in dist_df.columns
             if "distance" in _norm_hub_key(c).lower() or _norm_hub_key(c) == "DISTANCE"),
            None,
        )
        if sc is None or dc is None or dist_col is None:
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_columns", "detail": "Needs S_Code, D_Code, distance"}]}
        out: dict[tuple[str, str], float] = {}
        for a, b, d in zip(sc, dc, dist_col):
            ka, kb = _norm_hub_key(a), _norm_hub_key(b)
            if not ka or not kb:
                continue
            km = float(pd.to_numeric(d, errors="coerce"))
            if np.isnan(km):
                continue
            out[(ka, kb)] = km
        return {"status": "ok", "data": out, "issues": issues}
    except Exception as exc:
        return {"status": "failed", "data": None, "issues": [{"type": "error", "detail": str(exc)}]}


def build_cost_lookup(rate_card_df: pd.DataFrame) -> dict[str, Any]:
    """Build (MH1, MH2) → C/T cost lookup from MH1–MH2 rate card DataFrame."""
    issues: list[dict] = []
    try:
        mh1 = next((rate_card_df[c] for c in rate_card_df.columns if _norm_hub_key(c) == "MH1"), None)
        mh2 = next((rate_card_df[c] for c in rate_card_df.columns if _norm_hub_key(c) == "MH2"), None)
        cost_c = None
        for c in rate_card_df.columns:
            nk = _norm_hub_key(c)
            if nk in ("C/T", "C_T", "CT", "COST"):
                cost_c = rate_card_df[c]
                break
        if cost_c is None:
            for c in rate_card_df.columns:
                if "/" in str(c) and "C" in str(c).upper():
                    cost_c = rate_card_df[c]
                    break
        if mh1 is None or mh2 is None or cost_c is None:
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_columns", "detail": "Needs MH1, MH2, C/T"}]}
        out: dict[tuple[str, str], float] = {}
        for u, v, ct in zip(mh1, mh2, cost_c):
            ku, kv = _norm_hub_key(u), _norm_hub_key(v)
            if not ku or not kv:
                continue
            x = float(pd.to_numeric(ct, errors="coerce"))
            if np.isnan(x):
                continue
            out[(ku, kv)] = x
        return {"status": "ok", "data": out, "issues": issues}
    except Exception as exc:
        return {"status": "failed", "data": None, "issues": [{"type": "error", "detail": str(exc)}]}


def build_load_profile_interp(load_profile_df: pd.DataFrame) -> dict[str, Any]:
    """Build hour-of-day → cumulative order fraction interpolator from load profile DataFrame."""
    issues: list[dict] = []
    try:
        hc = next(
            (c for c in load_profile_df.columns
             if "fulfill_item" in str(c).lower() and "hr" in str(c).lower()), None,
        )
        if hc is None:
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_columns", "detail": "Missing fulfill_item hour column"}]}
        pc = next(
            (c for c in load_profile_df.columns
             if "order" in str(c).lower() and "profile" in str(c).lower()), None,
        )
        if pc is None:
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_columns", "detail": "Missing order profile % column"}]}
        tmp = load_profile_df[[hc, pc]].copy()
        tmp["_h"] = pd.to_numeric(tmp[hc], errors="coerce").astype("Int64")
        tmp["_p"] = tmp[pc].map(_parse_pct)
        tmp = tmp.dropna(subset=["_h"])
        cum: dict[int, float] = {}
        for _, r in tmp.iterrows():
            h = int(r["_h"])
            cum[h] = max(cum.get(h, 0.0), float(r["_p"]))

        def interp(t: float) -> float:
            t = max(0.0, min(24.0, float(t)))
            h0 = int(math.floor(t))
            h1 = min(24, h0 + 1)
            frac = t - h0
            v0 = cum.get(h0, cum.get(max(0, h0 - 1), 0.0))
            v1 = cum.get(h1, v0)
            return float(v0 * (1 - frac) + v1 * frac)

        return {"status": "ok", "data": interp, "issues": issues}
    except Exception as exc:
        return {"status": "failed", "data": None, "issues": [{"type": "error", "detail": str(exc)}]}


def build_route_lookup(plan_vol_df: pd.DataFrame) -> dict[str, Any]:
    """Build (SMH, DMH_last) → [hop_key_1, ..., hop_key_n] from plan_volume DataFrame."""
    issues: list[dict] = []
    try:
        mh_cols = [c for c in plan_vol_df.columns if re.match(r"^MH\d+$", str(c), re.I)]
        out: dict[tuple[str, str], list[str]] = {}
        for _, row in plan_vol_df.iterrows():
            hops = _extract_hops_from_plan_row(row, mh_cols)
            if len(hops) >= 2:
                out[(hops[0], hops[-1])] = hops
        return {"status": "ok", "data": out, "issues": issues}
    except Exception as exc:
        return {"status": "failed", "data": None, "issues": [{"type": "error", "detail": str(exc)}]}


# ─────────────────────────────────────────────────────────────────────────────
# Atomic cost functions  (public — return result dicts)
# ─────────────────────────────────────────────────────────────────────────────

def compute_trip_cost(
    u: str,
    v: str,
    cost_lookup: dict[tuple[str, str], float],
    dist_lookup: Optional[dict[tuple[str, str], float]],
    cfg: dict[str, Any],
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
) -> dict[str, Any]:
    """
    Cost (Rs) for a single MH→MH edge u→v.

    Fallback chain: rate card (u,v) → rate card (v,u) → distance × 49 Rs/km → OSRM → None.
    Returns {"status": "ok"|"failed", "data": cost_rs or None, "issues": [...]}.
    """
    missing: list = []
    est_log: list = []
    osrm_log: list = []
    ku, kv = _norm_hub_key(u), _norm_hub_key(v)
    mh_mh_cost_per_km = float(cfg.get("mh_mh_cost_per_km_fallback", 49.0))
    trip = _trip_cost_with_fallback(
        ku, kv,
        cost_lookup=cost_lookup,
        dist_lookup=dist_lookup,
        mh_mh_cost_per_km=mh_mh_cost_per_km,
        missing_pairs=missing,
        mh_mh_est_log=est_log,
        hub_lat_lkp=hub_lat_lkp,
        osrm_log=osrm_log,
        osrm_cfg=cfg,
    )
    issues: list[dict] = []
    if trip is None:
        issues.append({"type": "missing_edge", "detail": f"No cost resolved for {ku}->{kv}"})
        return {"status": "failed", "data": None, "issues": issues}
    if est_log:
        issues.append({"type": "estimated_via_distance", "detail": f"{ku}->{kv} estimated at Rs {trip:.2f}"})
    return {"status": "ok", "data": round(trip, 2), "issues": issues}


def compute_mhmh_cost(
    dh_key: str,
    candidate_key: str,
    plan_vol_df: pd.DataFrame,
    cost_lookup: dict[tuple[str, str], float],
    cfg: dict[str, Any],
    *,
    dist_lookup: Optional[dict[tuple[str, str], float]] = None,
    route_lookup: Optional[dict[tuple[str, str], list[str]]] = None,
    pathway_df: Optional[pd.DataFrame] = None,
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
) -> dict[str, Any]:
    """
    MH-MH cost (Rs/day) for a DH served from candidate_key.

    Includes FBF P2 leg when pathway_df is provided — this is the fix over the
    old compute_mhmh_for_pairs which always omitted the P2 leg.

    Returns {"status": "ok"|"partial"|"failed", "data": cost_rs, "issues": [...]}.
    status "partial" means some edges were missing from the rate card (cost may be underestimated).
    """
    issues: list[dict] = []
    mh_cols = [c for c in plan_vol_df.columns if re.match(r"^MH\d+$", str(c), re.I)]
    dh_col = "LMHub" if "LMHub" in plan_vol_df.columns else next(
        (c for c in plan_vol_df.columns if str(c).lower() == "lmhub"), None
    )
    if dh_col is None:
        return {"status": "failed", "data": None,
                "issues": [{"type": "missing_columns", "detail": "plan_vol_df missing LMHub"}]}

    dh_norm = _norm_hub_key(dh_key)
    cand_norm = _norm_hub_key(candidate_key)
    plan_slice = plan_vol_df[plan_vol_df[dh_col].map(_norm_hub_key) == dh_norm]

    if plan_slice.empty:
        return {"status": "failed", "data": None,
                "issues": [{"type": "no_plan_rows", "detail": f"No plan rows for DH {dh_key}"}]}

    # FBF CFT for P2 leg
    fbf_cft = 0.0
    if "stream" in plan_vol_df.columns:
        fbf_rows = plan_slice[plan_slice["stream"].astype(str).str.upper() == "FBF"]
        _cft_col = pd.to_numeric(fbf_rows.get("plan_median_cft_volume", pd.Series(dtype=float)), errors="coerce")
        fbf_cft = float(_cft_col.fillna(0).sum())

    # Pathway P2 hub info for FBF leg
    p2_hub_key = ""
    p2_inv = 0.0
    if pathway_df is not None and len(pathway_df) > 0:
        p1c = next((c for c in pathway_df.columns if "p1" in str(c).lower() and "central" in str(c).lower()), None)
        p2c = next((c for c in pathway_df.columns if "p2" in str(c).lower() and "central" in str(c).lower()), None)
        p2pct = next((c for c in pathway_df.columns if "p2" in str(c).lower() and "pct" in str(c).lower()), None)
        if p1c:
            pr = _pick_pathway_row_for_p1(pathway_df, cand_norm, p1c)
            if pr is not None:
                if p2c and p2c in pr.index:
                    p2_hub_key = _pathway_mh_key(pr[p2c])
                if p2pct and p2pct in pr.index and p2_hub_key:
                    p2_val = pr[p2c] if p2c and p2c in pr.index else None
                    if _is_real_central_hub(p2_val):
                        p2_inv = _parse_pct(pr[p2pct])

    missing: list = []
    est_log: list = []

    cost, ok = _compute_mh_mh_cost_for_candidate(
        plan_vol=plan_vol_df,
        dh_key=dh_norm,
        candidate_key=cand_norm,
        cost_lookup=cost_lookup,
        mh_cols=mh_cols,
        dh_col=dh_col,
        missing_pairs=missing,
        truck_cft_mh_mh=float(cfg.get("truck_cft_mh_mh", 2400.0)),
        plan_slice=plan_slice,
        route_lookup=route_lookup or {},
        p2_hub_key=p2_hub_key,
        p2_inv=p2_inv,
        fbf_cft=fbf_cft,
        dist_lookup=dist_lookup,
        hub_lat_lkp=hub_lat_lkp,
        mh_mh_cost_per_km=float(cfg.get("mh_mh_cost_per_km_fallback", 49.0)),
        mh_mh_est_log=est_log,
    )

    for f, t, r in missing:
        issues.append({"type": "missing_edge", "detail": f"{f}->{t} ({r})"})

    status = "ok" if ok else "partial"
    return {"status": status, "data": round(cost, 2), "issues": issues}


def compute_mhdh_cost(
    dh_key: str,
    candidate_key: str,
    total_cft: float,
    dist_lookup: dict[tuple[str, str], float],
    cfg: dict[str, Any],
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
) -> dict[str, Any]:
    """
    MH-DH cost (Rs/day) for a DH served from candidate_key.
    Formula: (total_cft / truck_cft_mh_dh_base) × 2 × distance_km × mh_dh_cost_rs_per_km.
    Returns {"status": "ok"|"failed", "data": cost_rs, "issues": [...]}.
    """
    missing: list = []
    osrm_log: list = []
    cost, ok = _compute_mh_dh_cost_raw(
        dh_key=_norm_hub_key(dh_key),
        candidate_key=_norm_hub_key(candidate_key),
        total_cft=total_cft,
        dist_lookup=dist_lookup,
        cfg=cfg,
        missing_pairs=missing,
        hub_lat_lkp=hub_lat_lkp,
        osrm_log=osrm_log,
    )
    issues: list[dict] = [{"type": "missing_distance", "detail": f"{f}->{t}"} for f, t, _ in missing]
    return {
        "status": "ok" if ok else "failed",
        "data": round(cost, 2) if ok else None,
        "issues": issues,
    }


def compute_speed(
    dh_key: str,
    candidate_key: str,
    pathway_df: pd.DataFrame,
    dist_lookup: dict[tuple[str, str], float],
    load_fn: Callable[[float], float],
    cfg: dict[str, Any],
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
) -> dict[str, Any]:
    """
    D1% speed metric for a DH served from candidate_key (as P1 central hub).
    Returns {"status": "ok"|"partial"|"failed",
             "data": {"d1_fraction": float, "p1_contrib": float, "p2_contrib": float},
             "issues": [...]}.
    """
    dh_norm = _norm_hub_key(dh_key)
    cand_norm = _norm_hub_key(candidate_key)

    p1c = next((c for c in pathway_df.columns if "p1" in str(c).lower() and "central" in str(c).lower()), None)
    p2c = next((c for c in pathway_df.columns if "p2" in str(c).lower() and "central" in str(c).lower()), None)
    p1pct = next((c for c in pathway_df.columns if "p1" in str(c).lower() and "pct" in str(c).lower()), None)
    p2pct = next((c for c in pathway_df.columns if "p2" in str(c).lower() and "pct" in str(c).lower()), None)

    if p1c is None:
        return {"status": "failed", "data": None,
                "issues": [{"type": "missing_columns", "detail": "pathway_df missing P1 central hub column"}]}

    pr = _pick_pathway_row_for_p1(pathway_df, cand_norm, p1c)
    if pr is None:
        return {"status": "ok", "data": {"d1_fraction": 0.0, "p1_contrib": 0.0, "p2_contrib": 0.0}, "issues": []}

    p1_hub_key = _norm_hub_key(pr[p1c])
    p2_hub_key = _pathway_mh_key(pr[p2c]) if p2c and p2c in pr.index else ""
    p1_inv = _parse_pct(pr[p1pct]) if p1pct and p1pct in pr.index and p1_hub_key == cand_norm else 0.0
    p2_val = pr[p2c] if p2c and p2c in pr.index else None
    p2_inv = (
        _parse_pct(pr[p2pct])
        if p2_hub_key and p2pct and p2pct in pr.index and _is_real_central_hub(p2_val)
        else 0.0
    )

    missing: list = []
    d1, ok, p1_contrib, p2_contrib = _compute_speed_metrics_raw(
        dh_norm, p1_hub_key, p2_hub_key, p1_inv, p2_inv,
        dist_lookup, load_fn, cfg,
        missing=missing, hub_lat_lkp=hub_lat_lkp,
    )
    issues = [{"type": "missing_distance", "detail": f"{f}->{t} ({r})"} for f, t, r in missing]
    return {
        "status": "ok" if ok else "partial",
        "data": {"d1_fraction": d1, "p1_contrib": p1_contrib, "p2_contrib": p2_contrib},
        "issues": issues,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio builders  (public — return result dicts)
# ─────────────────────────────────────────────────────────────────────────────

def build_dh_portfolio(
    plan_vol_df: pd.DataFrame,
    fbf_agg_df: pd.DataFrame,
) -> dict[str, Any]:
    """
    One row per DH combining NFBF/ALITE volumes from plan_volume with FBF
    volumes from fbf_plan_dh_aggregate.
    Returns {"status": "ok"|"partial"|"failed", "data": DataFrame, "issues": [...]}.
    """
    issues: list[dict] = []
    try:
        mh_cols = [c for c in plan_vol_df.columns if re.match(r"^MH\d+$", str(c), re.I)]
        dh_col = "LMHub" if "LMHub" in plan_vol_df.columns else next(
            (c for c in plan_vol_df.columns if str(c).lower() == "lmhub"), None
        )
        if dh_col is None:
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_columns", "detail": "plan_vol_df missing LMHub"}]}

        st = plan_vol_df.get("stream", pd.Series("", index=plan_vol_df.index)).astype(str).str.upper()
        ship = pd.to_numeric(plan_vol_df.get("median_demand_shipments", 0), errors="coerce").fillna(0.0)
        cft = pd.to_numeric(plan_vol_df.get("plan_median_cft_volume", 0), errors="coerce").fillna(0.0)

        rows: list[dict] = []
        for dh, g in plan_vol_df.groupby(plan_vol_df[dh_col].map(_norm_hub_key)):
            if not dh:
                continue
            m_n = st.loc[g.index] == "NFBF"
            m_a = st.loc[g.index] == "ALITE"
            rows.append({
                "destination_hub_key": dh,
                "nfbf_shipments": float(ship.loc[g.index][m_n].sum()),
                "nfbf_cft": float(cft.loc[g.index][m_n].sum()),
                "alphalite_shipments": float(ship.loc[g.index][m_a].sum()),
                "alphalite_cft": float(cft.loc[g.index][m_a].sum()),
                "plan_rows": int(len(g)),
            })
        port = pd.DataFrame(rows)

        fbf = fbf_agg_df.copy()
        if "destination_hub" not in fbf.columns:
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_columns",
                                "detail": "fbf_agg_df missing destination_hub"}]}
        fbf["destination_hub_key"] = fbf["destination_hub"].map(_norm_hub_key)

        for req in ("fbf_avg_daily_shipments_all", "cft_cuft_day_avg_all"):
            if req not in fbf.columns:
                return {"status": "failed", "data": None,
                        "issues": [{"type": "missing_columns",
                                    "detail": f"fbf_agg_df missing {req}"}]}

        merge_cols = ["fbf_avg_daily_shipments_all", "cft_cuft_day_avg_all"]
        for c in TOP266_COLS + LBU_SHIP_COLS:
            if c in fbf.columns:
                merge_cols.append(c)

        extra = fbf.set_index("destination_hub_key")[merge_cols].rename(
            columns={"fbf_avg_daily_shipments_all": "fbf_shipments",
                     "cft_cuft_day_avg_all": "fbf_cft"}
        )
        port = port.merge(extra, left_on="destination_hub_key", right_index=True, how="left")

        for c in TOP266_COLS + LBU_SHIP_COLS + ["fbf_shipments", "fbf_cft"]:
            if c in port.columns:
                port[c] = pd.to_numeric(port[c], errors="coerce").fillna(0.0)

        t266_cols = [c for c in TOP266_COLS if c in port.columns]
        port["top266_shipments"] = port[t266_cols].sum(axis=1) if t266_cols else pd.Series(0.0, index=port.index)
        lbu_cols = [c for c in LBU_SHIP_COLS if c in port.columns]
        port["lbu_shipments"] = port[lbu_cols].sum(axis=1) if lbu_cols else pd.Series(0.0, index=port.index)
        port["total_dh_cft"] = (
            port.get("nfbf_cft", 0) + port.get("alphalite_cft", 0) + port.get("fbf_cft", 0)
        ).astype(float)
        return {"status": "ok", "data": port, "issues": issues}
    except Exception as exc:
        return {"status": "failed", "data": None, "issues": [{"type": "error", "detail": str(exc)}]}


def build_smh_mhlast_report(
    plan_vol_df: pd.DataFrame,
    cost_lookup: dict[tuple[str, str], float],
    cfg: dict[str, Any],
    *,
    dist_lookup: Optional[dict[tuple[str, str], float]] = None,
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
) -> dict[str, Any]:
    """
    SMH × MH_Last cost-per-shipment report.

    BUG FIX vs inline block in agent3_pipeline.py:
    - Uses compute_trip_cost() with full fallback chain (rate card → distance×49 → OSRM)
      instead of silent-zero when an edge is missing from the rate card.
    - Adds cost_complete column (False when any edge on the lane was not in the rate card).
    - Adds edges_missing_from_rate_card column (pipe-separated list of missing edges).

    Returns {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}.
    """
    issues: list[dict] = []
    truck_mh_mh = float(cfg.get("truck_cft_mh_mh", 2400.0))
    mh_cols = [c for c in plan_vol_df.columns if re.match(r"^MH\d+$", str(c), re.I)]
    use_rc_col = "source_type" in plan_vol_df.columns

    smh_rows: list[dict] = []
    all_missing_edges: set[tuple[str, str]] = set()

    for _, row in plan_vol_df.iterrows():
        hops = _extract_hops_from_plan_row(row, mh_cols)
        if len(hops) < 2:
            continue
        smh = hops[0]
        mh_last = hops[-1]
        _cft_r = pd.to_numeric(row.get("plan_median_cft_volume", 0), errors="coerce")
        cft = 0.0 if pd.isna(_cft_r) else float(_cft_r)
        _ship_r = pd.to_numeric(row.get("median_demand_shipments", 0), errors="coerce")
        ship = 0.0 if pd.isna(_ship_r) else float(_ship_r)
        if cft <= 0 and ship <= 0:
            continue

        zero_first = use_rc_col and not _plan_row_uses_mh_mh_rate_card(row.get("source_type"))
        lane_cost = 0.0
        row_cost_complete = True
        row_missing: list[str] = []

        for ei, (u, v) in enumerate(zip(hops[:-1], hops[1:])):
            if zero_first and ei == 0:
                continue
            # BUG FIX: use compute_trip_cost (full fallback) instead of silent-zero pattern
            trip_result = compute_trip_cost(u, v, cost_lookup, dist_lookup, cfg, hub_lat_lkp)
            if trip_result["data"] is None:
                row_cost_complete = False
                row_missing.append(f"{u}->{v}")
                all_missing_edges.add((u, v))
            else:
                lane_cost += (cft / max(truck_mh_mh, 1e-9)) * trip_result["data"]

        smh_rows.append({
            "smh": smh,
            "mh_last": mh_last,
            "plan_median_cft": cft,
            "median_demand_shipments": ship,
            "mh_mh_cost_rs": lane_cost,
            "cost_complete": row_cost_complete,
            "edges_missing_from_rate_card": "|".join(row_missing),
        })

    if smh_rows:
        df = pd.DataFrame(smh_rows)
        smh_agg = (
            df.groupby(["smh", "mh_last"])
            .agg(
                total_cft=("plan_median_cft", "sum"),
                total_shipments=("median_demand_shipments", "sum"),
                total_mh_mh_cost_rs=("mh_mh_cost_rs", "sum"),
                cost_complete=("cost_complete", "all"),
                edges_missing_from_rate_card=(
                    "edges_missing_from_rate_card",
                    lambda x: "|".join(sorted({e for v in x for e in v.split("|") if e})),
                ),
            )
            .reset_index()
        )
        smh_agg["mh_mh_cost_per_shipment_rs"] = (
            smh_agg["total_mh_mh_cost_rs"]
            / smh_agg["total_shipments"].replace(0, float("nan"))
        )
    else:
        smh_agg = pd.DataFrame(columns=[
            "smh", "mh_last", "total_cft", "total_shipments",
            "total_mh_mh_cost_rs", "mh_mh_cost_per_shipment_rs",
            "cost_complete", "edges_missing_from_rate_card",
        ])

    if all_missing_edges:
        issues.append({
            "type": "missing_rate_card_edges",
            "detail": (f"{len(all_missing_edges)} unique edges missing from rate card; "
                       "cost_complete=False for affected lanes"),
        })

    return {
        "status": "partial" if issues else "ok",
        "data": smh_agg,
        "issues": issues,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML network map  (private helper — no dependency on agent3_pipeline.py)
# ─────────────────────────────────────────────────────────────────────────────

def _build_network_map_html(
    assign_df: pd.DataFrame,
    lat_df: pd.DataFrame,
    out_path: Path,
) -> None:
    """Generate a self-contained Leaflet HTML network map for MH→DH assignments."""
    import colorsys
    import json as _json

    # lat lookup: normalized site_key → (lat, lon)
    lat_lkp: dict[str, tuple[float, float]] = {
        _norm_hub_key(str(r["site_key"])): (float(r["lat"]), float(r["lon"]))
        for _, r in lat_df.iterrows()
        if pd.notna(r.get("lat")) and pd.notna(r.get("lon"))
    }

    # Distinct MHs that actually have assignments
    ok_rows = assign_df[assign_df["assigned_fc_mh"].astype(str).str.len() > 0]
    mh_list = sorted(ok_rows["assigned_fc_mh"].dropna().unique().tolist())

    # Generate distinct hue-spaced colors
    def _hsl_hex(h_norm: float, s: float = 0.60, l: float = 0.52) -> str:
        r, g, b = colorsys.hls_to_rgb(h_norm, l, s)
        return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

    n = max(len(mh_list), 1)
    mh_color: dict[str, str] = {mh: _hsl_hex(i / n) for i, mh in enumerate(mh_list)}

    # MH_DATA
    mh_data: dict[str, dict] = {}
    for mh in mh_list:
        pos = lat_lkp.get(_norm_hub_key(mh))
        if pos:
            mh_data[mh] = {"lat": round(pos[0], 6), "lon": round(pos[1], 6)}

    # DH_DATA
    dh_rows = []
    for _, row in assign_df.iterrows():
        dh = str(row["destination_hub_key"])
        mh = str(row.get("assigned_fc_mh", ""))
        if not mh or mh not in mh_data:
            continue
        pos = lat_lkp.get(_norm_hub_key(dh))
        if pos is None:
            continue
        d1_raw = row.get("final_d1_pct")
        if isinstance(d1_raw, str):
            d1 = float(d1_raw.rstrip("%")) / 100.0 if d1_raw.strip() else None
        elif pd.notna(d1_raw):
            v = float(d1_raw)
            d1 = v / 100.0 if v > 1.5 else v
        else:
            d1 = None
        def _safe_float(v: Any) -> Optional[float]:
            try:
                return round(float(str(v).replace(",", "")), 3)
            except (ValueError, TypeError):
                return None
        dh_rows.append({
            "name": dh,
            "mh": mh,
            "lat": round(pos[0], 6),
            "lon": round(pos[1], 6),
            "assignment_basis": str(row.get("assignment_basis", "")),
            "final_d1_pct": round(d1, 6) if d1 is not None else None,
            "top266_shipments": _safe_float(row.get("top266_shipments")),
            "total_shipments": _safe_float(row.get("total_shipments")),
            "total_cost_rs": _safe_float(row.get("total_cost_rs")),
            "current_fc": str(row.get("current_fc_mh") or "") or None,
            "current_fc_cost": _safe_float(row.get("current_fc_cost_rs")),
            "cost_delta": _safe_float(row.get("cost_delta_rs")),
        })

    dh_counts: dict[str, int] = {}
    dh_by_mh: dict[str, list[dict]] = {}
    mh_stats: dict[str, dict] = {}
    for r in dh_rows:
        mh = r["mh"]
        dh_counts[mh] = dh_counts.get(mh, 0) + 1
        dh_by_mh.setdefault(mh, []).append({
            "name": r["name"],
            "total_shipments": r["total_shipments"],
            "top266_shipments": r["top266_shipments"],
        })
    for mh, dhs in dh_by_mh.items():
        def _s(key: str) -> float:
            return sum(d[key] or 0 for d in dhs)
        mh_stats[mh] = {
            "total_shipments": round(_s("total_shipments"), 1),
            "top266_shipments": round(_s("top266_shipments"), 1),
        }

    n_mh = len(mh_data)
    n_dh = len(dh_rows)

    # Delta tier colors (globally ranked across all DHs by cost_delta descending)
    _delta_pairs = [
        (r["name"], r["cost_delta"]) for r in dh_rows
        if r.get("cost_delta") is not None and r["cost_delta"] > 0
    ]
    _delta_pairs.sort(key=lambda x: x[1], reverse=True)
    dh_delta_color: dict[str, str] = {}
    for _di, (_dn, _dv) in enumerate(_delta_pairs):
        if _di < 10:          dh_delta_color[_dn] = "#cc0000"
        elif _di < 30:        dh_delta_color[_dn] = "#e85d04"
        elif _dv >= 3000:     dh_delta_color[_dn] = "#f4a100"
        else:                 dh_delta_color[_dn] = "#374151"

    mh_data_js        = _json.dumps(mh_data,        ensure_ascii=False)
    dh_data_js        = _json.dumps(dh_rows,        ensure_ascii=False)
    mh_color_js       = _json.dumps(mh_color,       ensure_ascii=False)
    dh_counts_js      = _json.dumps(dh_counts,      ensure_ascii=False)
    dh_by_mh_js       = _json.dumps(dh_by_mh,       ensure_ascii=False)
    mh_stats_js       = _json.dumps(mh_stats,       ensure_ascii=False)
    dh_delta_color_js = _json.dumps(dh_delta_color, ensure_ascii=False)

    dh_tier_dict: dict[str, str] = {}
    for _di, (_dn, _dv) in enumerate(_delta_pairs):
        if _di < 10:       dh_tier_dict[_dn] = "red"
        elif _di < 30:     dh_tier_dict[_dn] = "orange"
        elif _dv >= 3000:  dh_tier_dict[_dn] = "amber"
        else:              dh_tier_dict[_dn] = "charcoal"
    dh_tier_js = _json.dumps(dh_tier_dict, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MH–DH Network Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:"Segoe UI",sans-serif; background:#f0f2f5; color:#2d3748; display:flex; height:100vh; overflow:hidden; }}
  #sidebar {{ width:290px; min-width:250px; max-width:320px; background:#fff; display:flex; flex-direction:column; border-right:1px solid #dde3ea; overflow:hidden; box-shadow:2px 0 8px rgba(0,0,0,.06); }}
  #sidebar-header {{ padding:14px 16px; background:#f8fafc; border-bottom:1px solid #dde3ea; }}
  #sidebar-header h1 {{ font-size:15px; font-weight:700; color:#1a202c; margin-bottom:3px; }}
  #sidebar-header p {{ font-size:11px; color:#718096; }}
  #search-box {{ margin:10px 12px; padding:7px 12px; background:#f8fafc; border:1px solid #dde3ea; border-radius:6px; color:#2d3748; font-size:12px; outline:none; width:calc(100% - 24px); }}
  #search-box:focus {{ border-color:#a0aec0; }}
  #search-box::placeholder {{ color:#a0aec0; }}
  #controls {{ padding:0 12px 8px; display:flex; gap:6px; }}
  #controls button {{ flex:1; padding:5px; font-size:11px; border-radius:5px; border:1px solid #dde3ea; cursor:pointer; background:#f8fafc; color:#718096; transition:all .15s; }}
  #controls button:hover {{ background:#edf2f7; color:#2d3748; }}
  #legend {{ overflow-y:auto; flex:1; padding:0 8px 12px; }}
  .legend-item {{ display:flex; align-items:center; gap:8px; padding:5px 6px; border-radius:6px; cursor:pointer; transition:background .12s; user-select:none; }}
  .legend-item:hover {{ background:#f0f4f8; }}
  .legend-item.inactive {{ opacity:.35; }}
  .legend-dot {{ width:13px; height:13px; border-radius:50%; flex-shrink:0; border:1.5px solid rgba(0,0,0,.12); }}
  .legend-label {{ font-size:11px; color:#4a5568; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .legend-count {{ font-size:10px; color:#718096; background:#edf2f7; padding:1px 6px; border-radius:10px; flex-shrink:0; }}
  #map {{ flex:1; }}
  #info-panel {{ position:absolute; top:12px; right:12px; background:#fff; border:1px solid #dde3ea; border-radius:10px; padding:14px 16px; min-width:260px; max-width:330px; max-height:80vh; overflow-y:auto; display:none; z-index:1000; font-size:12px; box-shadow:0 4px 20px rgba(0,0,0,.12); }}
  #info-panel h3 {{ font-size:13px; font-weight:600; margin-bottom:10px; word-break:break-all; line-height:1.4; color:#1a202c; }}
  #info-panel .tag {{ display:inline-block; padding:2px 9px; border-radius:12px; font-size:10px; font-weight:700; margin-bottom:7px; color:#fff; }}
  .dh-list-header {{ font-size:10px; font-weight:700; color:#718096; text-transform:uppercase; letter-spacing:.04em; padding:8px 4px 4px; border-top:1px solid #edf2f7; margin-top:6px; }}
  .dh-list {{ max-height:240px; overflow-y:auto; border:1px solid #edf2f7; border-radius:6px; margin-top:2px; }}
  .dh-list-row {{ display:grid; grid-template-columns:1fr auto auto; gap:4px; align-items:center; padding:5px 8px; border-bottom:1px solid #f7fafc; font-size:10.5px; }}
  .dh-list-row:last-child {{ border-bottom:none; }}
  .dh-list-row:hover {{ background:#f7fafc; }}
  .dh-list-name {{ color:#2d3748; font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .dh-list-val {{ color:#2d3748; font-weight:600; text-align:right; white-space:nowrap; font-size:10px; }}
  .dh-list-val.dim {{ color:#a0aec0; }}
  .dh-col-head {{ display:grid; grid-template-columns:1fr auto auto; gap:4px; padding:3px 8px; font-size:9.5px; color:#a0aec0; font-weight:700; text-transform:uppercase; }}
  #info-close {{ position:absolute; top:8px; right:10px; cursor:pointer; color:#a0aec0; font-size:18px; line-height:1; }}
  #info-close:hover {{ color:#2d3748; }}
  .info-table {{ width:100%; border-collapse:collapse; margin-top:4px; }}
  .info-table tr {{ border-bottom:1px solid #edf2f7; }}
  .info-table tr:last-child {{ border-bottom:none; }}
  .info-table td {{ padding:6px 4px; font-size:11px; }}
  .info-table td:first-child {{ color:#718096; width:55%; }}
  .info-table td:last-child {{ color:#2d3748; font-weight:600; text-align:right; }}
  .info-table tr.delta-highlight td:last-child {{ color:#ef4444; }}
  #view-toggle {{ display:flex; gap:6px; padding:10px 12px 4px; }}
  .view-btn {{ flex:1; padding:6px; font-size:11px; border-radius:6px; border:1px solid #dde3ea; cursor:pointer; background:#f8fafc; color:#718096; transition:all .15s; font-weight:500; }}
  .view-btn.active {{ background:#3b82f6; color:#fff; border-color:#3b82f6; font-weight:700; }}
  .view-btn:hover:not(.active) {{ background:#edf2f7; color:#2d3748; }}
  #delta-legend {{ padding:6px 10px 10px; border-bottom:1px solid #dde3ea; }}
  #delta-legend .tier-row {{ display:flex; align-items:center; gap:7px; padding:3px 6px; font-size:10.5px; color:#4a5568; cursor:pointer; border-radius:4px; margin:0 -6px; transition:background .12s; }}
  #delta-legend .tier-row:hover {{ background:#f0f4f8; }}
  #delta-legend .tier-row.inactive {{ opacity:.4; }}
  #delta-legend .tier-dot {{ width:11px; height:11px; border-radius:50%; flex-shrink:0; border:1px solid rgba(0,0,0,.15); }}
  .info-mh-row {{ display:flex; align-items:center; gap:6px; padding:6px 4px; border-bottom:1px solid #edf2f7; font-size:11px; }}
  .info-mh-dot {{ width:10px; height:10px; border-radius:50%; flex-shrink:0; border:1px solid rgba(0,0,0,.15); }}
  .info-mh-name {{ color:#2d3748; font-weight:600; word-break:break-all; }}
  #stats {{ position:absolute; bottom:20px; right:12px; background:#fff; border:1px solid #dde3ea; border-radius:8px; padding:7px 14px; z-index:1000; font-size:11px; color:#718096; display:flex; gap:16px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
  #stats span {{ color:#2d3748; font-weight:600; }}
  .leaflet-tooltip {{ background:#fff; border:1px solid #dde3ea; color:#2d3748; font-size:11px; padding:4px 8px; border-radius:5px; box-shadow:0 2px 6px rgba(0,0,0,.1); }}
  .leaflet-tooltip::before {{ display:none; }}
</style>
</head>
<body>
<div id="sidebar">
  <div id="sidebar-header">
    <h1>MH &ndash; DH Network</h1>
    <p>Click a hub on the map or legend to explore</p>
  </div>
  <div id="view-toggle">
    <button class="view-btn active" id="btn-mh-view">MH View</button>
    <button class="view-btn" id="btn-delta-view">&#9650; Cost Delta</button>
  </div>
  <input id="search-box" type="text" placeholder="&#128269; Search MH name...">
  <div id="controls">
    <button id="btn-show-all">Show All</button>
    <button id="btn-hide-all">Hide All</button>
  </div>
  <div id="delta-legend">
    <div style="font-size:10px;color:#a0aec0;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:0 0 5px">Cost Delta Filter <span style="font-weight:400;color:#c4c9d1">(delta view only)</span></div>
    <div class="tier-row" onclick="toggleTier('red',this)"><div class="tier-dot" style="background:#cc0000"></div><span>Top 10 highest delta</span></div>
    <div class="tier-row" onclick="toggleTier('orange',this)"><div class="tier-dot" style="background:#e85d04"></div><span>Next 20</span></div>
    <div class="tier-row" onclick="toggleTier('amber',this)"><div class="tier-dot" style="background:#f4a100"></div><span>Rest &ge; &#8377;3,000</span></div>
    <div class="tier-row" onclick="toggleTier('charcoal',this)"><div class="tier-dot" style="background:#374151"></div><span>&lt; &#8377;3,000 delta</span></div>
    <div class="tier-row" onclick="toggleTier('grey',this)"><div class="tier-dot" style="background:#c8ccd0"></div><span>No change / unassigned</span></div>
  </div>
  <div style="padding:8px 12px 2px;font-size:10px;color:#a0aec0;font-weight:700;text-transform:uppercase;letter-spacing:.05em;border-top:1px solid #dde3ea">MH Filter</div>
  <div id="legend"></div>
</div>
<div id="map"></div>
<div id="info-panel">
  <span id="info-close">&times;</span>
  <div id="info-tag" class="tag"></div>
  <h3 id="info-name"></h3>
  <p id="info-body"></p>
</div>
<div id="stats">
  <div>MHs: <span>{n_mh}</span></div>
  <div>DHs: <span>{n_dh}</span></div>
  <div>Visible: <span id="visible-count">{n_dh}</span></div>
</div>
<script>
const MH_DATA = {mh_data_js};
const DH_DATA = {dh_data_js};
const MH_COLOR = {mh_color_js};
const DH_COUNTS = {dh_counts_js};
const DH_BY_MH = {dh_by_mh_js};
const MH_STATS = {mh_stats_js};
const DH_DELTA_COLOR = {dh_delta_color_js};
const DH_TIER = {dh_tier_js};
const activeTiers = new Set(['red','orange','amber','charcoal','grey']);

let currentView = 'mh';
const dhMarkerRefs = [];
const mhMarkerRefs = [];

const map = L.map('map', {{ center:[22.5,82.5], zoom:5, zoomControl:true, preferCanvas:true }});
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
  maxZoom:18
}}).addTo(map);

const layerGroups = {{}};
const activeSet = new Set(Object.keys(MH_DATA));

function makeDHIcon(color) {{
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='7' height='7' viewBox='0 0 7 7'><circle cx='3.5' cy='3.5' r='2.8' fill='${{color}}' stroke='white' stroke-width='1'/></svg>`;
  return L.divIcon({{ html:svg, className:'', iconSize:[7,7], iconAnchor:[3.5,3.5] }});
}}
function makeDHIconLarge(color) {{
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'><circle cx='5' cy='5' r='4' fill='${{color}}' stroke='white' stroke-width='1.2'/></svg>`;
  return L.divIcon({{ html:svg, className:'', iconSize:[10,10], iconAnchor:[5,5] }});
}}
function makeMHIcon(color) {{
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='20' height='26' viewBox='0 0 28 36'>
    <path d='M14 0 C6.268 0 0 6.268 0 14 C0 24.5 14 36 14 36 C14 36 28 24.5 28 14 C28 6.268 21.732 0 14 0 Z' fill='${{color}}' stroke='white' stroke-width='1.5'/>
    <path d='M14 6 L8 12 L9.5 12 L9.5 20 L12.5 20 L12.5 16 L15.5 16 L15.5 20 L18.5 20 L18.5 12 L20 12 Z' fill='white' opacity='0.92'/></svg>`;
  return L.divIcon({{ html:svg, className:'', iconSize:[20,26], iconAnchor:[10,26] }});
}}
function showInfo(name, isMH, color, bodyHTML) {{
  const panel = document.getElementById('info-panel');
  const tag = document.getElementById('info-tag');
  tag.textContent = isMH ? 'MOTHER HUB' : 'DESTINATION HUB';
  tag.style.background = color;
  document.getElementById('info-name').textContent = name;
  document.getElementById('info-body').innerHTML = bodyHTML;
  panel.style.display = 'block';
}}
function fmtNum(v) {{
  if (v===null||v===undefined) return '—';
  return typeof v==='number' && !isNaN(v) ? v.toLocaleString(undefined,{{maximumFractionDigits:2}}) : v;
}}
function fmtPct(v) {{
  if (v===null||v===undefined) return '—';
  return typeof v==='number' ? (v*100).toFixed(1)+'%' : v;
}}
function switchView(v) {{
  currentView = v;
  document.getElementById('btn-mh-view').classList.toggle('active', v === 'mh');
  document.getElementById('btn-delta-view').classList.toggle('active', v === 'delta');
  dhMarkerRefs.forEach(function(ref) {{
    if (v === 'delta') {{
      ref.marker.setIcon(makeDHIconLarge(DH_DELTA_COLOR[ref.name] || '#c8ccd0'));
    }} else {{
      ref.marker.setIcon(makeDHIcon(MH_COLOR[ref.mh] || '#aaa'));
    }}
  }});
  mhMarkerRefs.forEach(function(ref) {{
    if (v === 'delta') {{
      ref.marker.setIcon(makeMHIcon('#6b7280'));
    }} else {{
      ref.marker.setIcon(makeMHIcon(MH_COLOR[ref.mh] || '#aaa'));
    }}
  }});
  if (v === 'delta') applyTierFilter(); else clearTierFilter();
}}
function applyTierFilter() {{
  dhMarkerRefs.forEach(function(ref) {{
    var show = activeTiers.has(ref.tier);
    var el = ref.marker.getElement();
    if (el) el.style.display = show ? '' : 'none';
    if (ref.poly) ref.poly.setStyle({{opacity: show ? 0.35 : 0}});
  }});
}}
function clearTierFilter() {{
  dhMarkerRefs.forEach(function(ref) {{
    var el = ref.marker.getElement();
    if (el) el.style.display = '';
    if (ref.poly) ref.poly.setStyle({{opacity: 0.35}});
  }});
}}
function toggleTier(tier, el) {{
  if (activeTiers.has(tier)) {{ activeTiers.delete(tier); el.classList.add('inactive'); }}
  else {{ activeTiers.add(tier); el.classList.remove('inactive'); }}
  if (currentView === 'delta') applyTierFilter();
}}
document.getElementById('btn-mh-view').addEventListener('click', () => switchView('mh'));
document.getElementById('btn-delta-view').addEventListener('click', () => switchView('delta'));

document.getElementById('info-close').addEventListener('click',()=>{{ document.getElementById('info-panel').style.display='none'; }});
map.on('click',()=>{{ document.getElementById('info-panel').style.display='none'; }});

Object.keys(MH_DATA).forEach(mh=>{{ layerGroups[mh]=L.layerGroup().addTo(map); }});

DH_DATA.forEach(dh=>{{
  if (!dh.lat||!dh.lon) return;
  const mhColor=MH_COLOR[dh.mh]||'#aaa';
  const group=layerGroups[dh.mh];
  if (!group) return;
  const mhPos=MH_DATA[dh.mh];
  let poly=null;
  if (mhPos&&mhPos.lat&&mhPos.lon)
    poly=L.polyline([[dh.lat,dh.lon],[mhPos.lat,mhPos.lon]],{{color:mhColor,weight:1,opacity:0.35}}).addTo(group);
  const marker=L.marker([dh.lat,dh.lon],{{icon:makeDHIcon(mhColor)}}).addTo(group);
  const tier=DH_TIER[dh.name]||'grey';
  dhMarkerRefs.push({{marker:marker, poly:poly, name:dh.name, mh:dh.mh, tier:tier}});
  marker.on('click',e=>{{
    L.DomEvent.stopPropagation(e);
    const color = currentView==='delta' ? (DH_DELTA_COLOR[dh.name]||'#c8ccd0') : mhColor;
    const deltaClass = (dh.cost_delta!==null && dh.cost_delta>0) ? ' class="delta-highlight"' : '';
    const bodyHTML=`
      <div class="info-mh-row"><div class="info-mh-dot" style="background:${{mhColor}}"></div><span class="info-mh-name">${{dh.mh}}</span></div>
      <table class="info-table">
        <tr><td>Assignment Basis</td><td>${{dh.assignment_basis||'—'}}</td></tr>
        <tr><td>D1 %</td><td>${{fmtPct(dh.final_d1_pct)}}</td></tr>
        <tr><td>Top 266 Shipments</td><td>${{fmtNum(dh.top266_shipments)}}</td></tr>
        <tr><td>Total Shipments</td><td>${{fmtNum(dh.total_shipments)}}</td></tr>
        <tr><td>Suggested FC Cost (Rs)</td><td>${{fmtNum(dh.total_cost_rs)}}</td></tr>
        <tr><td>Current FC</td><td>${{dh.current_fc||'—'}}</td></tr>
        <tr><td>Current FC Cost (Rs)</td><td>${{fmtNum(dh.current_fc_cost)}}</td></tr>
        <tr${{deltaClass}}><td>Cost Delta (Rs)</td><td>${{dh.cost_delta!==null ? fmtNum(dh.cost_delta) : '—'}}</td></tr>
        <tr><td>Coordinates</td><td>${{dh.lat.toFixed(4)}}, ${{dh.lon.toFixed(4)}}</td></tr>
      </table>`;
    showInfo(dh.name,false,color,bodyHTML);
  }});
  marker.on('mouseover',()=>{{ marker.bindTooltip(dh.name,{{permanent:false,direction:'top'}}).openTooltip(); }});
}});

Object.entries(MH_DATA).forEach(([mh,pos])=>{{
  if (!pos.lat||!pos.lon) return;
  const color=MH_COLOR[mh];
  const marker=L.marker([pos.lat,pos.lon],{{icon:makeMHIcon(color),zIndexOffset:1000}}).addTo(layerGroups[mh]);
  mhMarkerRefs.push({{marker:marker, mh:mh}});
  marker.on('click',e=>{{
    L.DomEvent.stopPropagation(e);
    const mColor = currentView==='delta' ? '#1e40af' : color;
    const stats=MH_STATS[mh]||{{}};
    const dhs=(DH_BY_MH[mh]||[]).slice().sort((a,b)=>(b.total_shipments||0)-(a.total_shipments||0));
    let dhRows='';
    dhs.forEach(d=>{{
      dhRows+=`<div class="dh-list-row">
        <span class="dh-list-name" title="${{d.name}}">${{d.name}}</span>
        <span class="dh-list-val">${{fmtNum(d.total_shipments)}}</span>
        <span class="dh-list-val dim">${{fmtNum(d.top266_shipments)}}</span>
      </div>`;
    }});
    const bodyHTML=`
      <table class="info-table">
        <tr><td>DHs Connected</td><td>${{DH_COUNTS[mh]||0}}</td></tr>
        <tr><td>Total Shipments</td><td>${{fmtNum(stats.total_shipments)}}</td></tr>
        <tr><td>Top 266 Shipments</td><td>${{fmtNum(stats.top266_shipments)}}</td></tr>
        <tr><td>Coordinates</td><td>${{pos.lat.toFixed(4)}}, ${{pos.lon.toFixed(4)}}</td></tr>
      </table>
      <div class="dh-list-header">Connected DHs</div>
      <div class="dh-col-head"><span>DH</span><span>Total Ship.</span><span>Top266</span></div>
      <div class="dh-list">${{dhRows||'<div style="padding:8px;color:#a0aec0;font-size:11px">No DHs</div>'}}</div>`;
    showInfo(mh,true,mColor,bodyHTML);
  }});
  marker.on('mouseover',()=>{{
    const stats=MH_STATS[mh]||{{}};
    marker.bindTooltip(`<b>${{mh}}</b><br>${{DH_COUNTS[mh]||0}} DHs &nbsp;·&nbsp; ${{fmtNum(stats.total_shipments)}} shipments`,{{permanent:false,direction:'top'}}).openTooltip();
  }});
}});

function buildLegend(filter) {{
  const legend=document.getElementById('legend');
  legend.innerHTML='';
  const fl=(filter||'').toLowerCase();
  Object.keys(MH_DATA).forEach(mh=>{{
    if (fl&&!mh.toLowerCase().includes(fl)) return;
    const item=document.createElement('div');
    item.className='legend-item'+(activeSet.has(mh)?'':' inactive');
    item.dataset.mh=mh;
    item.innerHTML=`<div class="legend-dot" style="background:${{MH_COLOR[mh]}}"></div><span class="legend-label" title="${{mh}}">${{mh}}</span><span class="legend-count">${{DH_COUNTS[mh]||0}}</span>`;
    item.addEventListener('click',()=>toggleMH(mh,item));
    legend.appendChild(item);
  }});
}}
function toggleMH(mh,item) {{
  if (activeSet.has(mh)) {{ activeSet.delete(mh); map.removeLayer(layerGroups[mh]); item.classList.add('inactive'); }}
  else {{
    activeSet.add(mh); map.addLayer(layerGroups[mh]); item.classList.remove('inactive');
    if (currentView === 'delta') setTimeout(applyTierFilter, 0);
  }}
  updateVisibleCount();
}}
function toggleAll(show) {{
  Object.keys(MH_DATA).forEach(mh=>{{
    if (show) {{ activeSet.add(mh); if(!map.hasLayer(layerGroups[mh])) map.addLayer(layerGroups[mh]); }}
    else {{ activeSet.delete(mh); map.removeLayer(layerGroups[mh]); }}
  }});
  document.querySelectorAll('.legend-item').forEach(el=>{{ el.classList.toggle('inactive',!show); }});
  updateVisibleCount();
}}
function updateVisibleCount() {{
  let cnt=0; activeSet.forEach(mh=>{{ cnt+=DH_COUNTS[mh]||0; }}); document.getElementById('visible-count').textContent=cnt;
}}
document.getElementById('btn-show-all').addEventListener('click',()=>toggleAll(true));
document.getElementById('btn-hide-all').addEventListener('click',()=>toggleAll(false));
document.getElementById('search-box').addEventListener('input',e=>buildLegend(e.target.value));
buildLegend('');
</script>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint 1 helpers  (public — call after run_agent3 to build presentation tables)
# ─────────────────────────────────────────────────────────────────────────────

def build_phase2_candidates(agent3_df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns only DHs where Agent 3 proposed a change from the resort baseline.
    These are the ONLY valid Phase 2 inputs.

    current_fc_mh  = resort baseline MH (Agent 3 carried forward from resort)
    assigned_fc_mh = Agent 3's Phase 1 proposal

    A pair (from_mh, to_mh) is valid for Phase 2 only if at least one DH
    has current_fc_mh=from_mh AND assigned_fc_mh=to_mh.

    Returns DataFrame with columns:
      from_mh, to_mh, dh_count, dh_list, total_cost_rs,
      current_cost_rs, monthly_saving_rs
    sorted by monthly_saving_rs descending.
    """
    moved = agent3_df[
        agent3_df["current_fc_mh"].astype(str).str.strip()
        != agent3_df["assigned_fc_mh"].astype(str).str.strip()
    ].copy()

    for col in ("total_cost_rs", "current_fc_cost_rs", "cost_delta_rs"):
        moved[col] = pd.to_numeric(moved[col], errors="coerce")

    pairs = (
        moved.groupby(
            [moved["current_fc_mh"].str.strip(), moved["assigned_fc_mh"].str.strip()],
            sort=False,
        )
        .agg(
            dh_count=("destination_hub_key", "count"),
            dh_list=("destination_hub_key", list),
            total_cost_rs=("total_cost_rs", "sum"),
            current_cost_rs=("current_fc_cost_rs", "sum"),
            monthly_saving_rs=("cost_delta_rs", "sum"),
        )
        .reset_index()
    )
    pairs.columns = [
        "from_mh", "to_mh", "dh_count",
        "dh_list", "total_cost_rs",
        "current_cost_rs", "monthly_saving_rs",
    ]
    return pairs.sort_values("monthly_saving_rs", ascending=False).reset_index(drop=True)


def build_cost_only_opportunities(agent3_df: pd.DataFrame) -> pd.DataFrame:
    """
    DHs where Agent 3 kept the resort assignment but a cheaper candidate exists.
    Informational only — NOT valid Phase 2 inputs.

    These are speed-assigned DHs where cost was sacrificed for D1% compliance.
    The assignment_basis column will typically be 'speed' for these rows.

    Returns DataFrame sorted by cost_delta_rs ascending (largest cost sacrifice first).
    """
    for col in ("cost_delta_rs", "total_cost_rs", "current_fc_cost_rs"):
        agent3_df = agent3_df.copy()
        agent3_df[col] = pd.to_numeric(agent3_df[col], errors="coerce")

    kept = agent3_df[
        (agent3_df["current_fc_mh"].astype(str).str.strip()
         == agent3_df["assigned_fc_mh"].astype(str).str.strip())
        & agent3_df["cost_delta_rs"].notna()
        & (agent3_df["cost_delta_rs"] < 0)
    ].copy()

    return (
        kept[[
            "destination_hub_key", "assigned_fc_mh",
            "assignment_basis", "total_cost_rs",
            "current_fc_cost_rs", "cost_delta_rs",
        ]]
        .sort_values("cost_delta_rs")
        .reset_index(drop=True)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Full pipeline  (public — returns result dict)
# ─────────────────────────────────────────────────────────────────────────────

def run_agent3(
    plan_vol_df: pd.DataFrame,
    fbf_agg_df: pd.DataFrame,
    pathway_df: pd.DataFrame,
    fc_mh_df: pd.DataFrame,
    lat_long_df: pd.DataFrame,
    load_profile_df: pd.DataFrame,
    dist_df: pd.DataFrame,
    cost_df: pd.DataFrame,
    cfg: dict[str, Any],
    output_dir: Path,
    *,
    top266_threshold: Optional[float] = None,
    proximity_km_threshold: Optional[float] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    on_dh_progress: Optional[Callable[[int, int, int, int, int], None]] = None,
) -> dict[str, Any]:
    """
    Full Agent 3 pipeline.  DataFrames in; writes 7 CSV/HTML outputs to output_dir.

    fc_mh_df   — raw Plan fbf master (MH1 + Tag) OR pre-processed (fc_mh + fc_mh_key); normalized internally
    lat_long_df — raw Lat Longs (Site_name + Latitude + Longitude) OR pre-processed (site_key + lat + lon); normalized internally

    Returns {"status": "ok"|"partial"|"failed", "data": {paths + counts}, "issues": [...]}.
    """
    issues: list[dict] = []

    def _emit(msg: str) -> None:
        print(msg)
        if on_progress:
            on_progress(msg)

    try:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        thr = float(top266_threshold if top266_threshold is not None
                    else cfg.get("default_top266_threshold", 5.0))

        if proximity_km_threshold is not None and float(proximity_km_threshold) <= 0:
            prox_filter_on, prox_km = False, 0.0
        elif proximity_km_threshold is not None:
            prox_filter_on, prox_km = True, float(proximity_km_threshold)
        else:
            prox_filter_on = True
            prox_km = float(cfg.get("default_proximity_km_threshold", 80.0))

        mh_dh_cost_buffer = float(cfg.get("mh_dh_cost_buffer", 1.15))
        use_osrm = bool(cfg.get("use_osrm_fallback", True))
        mh_mh_cost_per_km = float(cfg.get("mh_mh_cost_per_km_fallback", 49.0))

        _emit("Building lookups …")
        dl_result = build_distance_lookup(dist_df)
        if dl_result["status"] == "failed":
            return {"status": "failed", "data": None, "issues": dl_result["issues"]}
        dist_lookup: dict = dl_result["data"]

        cl_result = build_cost_lookup(cost_df)
        if cl_result["status"] == "failed":
            return {"status": "failed", "data": None, "issues": cl_result["issues"]}
        cost_lookup: dict = cl_result["data"]

        lp_result = build_load_profile_interp(load_profile_df)
        if lp_result["status"] == "failed":
            return {"status": "failed", "data": None, "issues": lp_result["issues"]}
        load_fn = lp_result["data"]

        _emit(f"  distance pairs:  {len(dist_lookup)}")
        _emit(f"  cost pairs:      {len(cost_lookup)}")

        # Normalize plan_vol MH key columns
        plan_vol = plan_vol_df.copy()
        mh_cols = [c for c in plan_vol.columns if re.match(r"^MH\d+$", str(c), re.I)]
        dh_col = "LMHub" if "LMHub" in plan_vol.columns else next(
            (c for c in plan_vol.columns if str(c).lower() == "lmhub"), None
        )
        if dh_col is None:
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_columns", "detail": "plan_vol_df missing LMHub"}]}
        for _mc in mh_cols:
            plan_vol[f"_k_{_mc}"] = plan_vol[_mc].map(_norm_hub_key)

        # Pathway column discovery
        _pway_p1c = next((c for c in pathway_df.columns if "p1" in str(c).lower() and "central" in str(c).lower()), None)
        _pway_p2c = next((c for c in pathway_df.columns if "p2" in str(c).lower() and "central" in str(c).lower()), None)
        _pway_p1pct = next((c for c in pathway_df.columns if "p1" in str(c).lower() and "pct" in str(c).lower()), None)
        _pway_p2pct = next((c for c in pathway_df.columns if "p2" in str(c).lower() and "pct" in str(c).lower()), None)
        missing_pway = [n for n, v in (
            ("p1 central hub", _pway_p1c), ("p2 central hub", _pway_p2c),
            ("p1 pct", _pway_p1pct), ("p2 pct", _pway_p2pct)
        ) if v is None]
        if missing_pway:
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_columns",
                                "detail": f"pathway_df missing: {missing_pway}"}]}
        _pathway_p1_index = _build_pathway_p1_index(pathway_df, _pway_p1c)

        # Route lookup
        rl_result = build_route_lookup(plan_vol)
        _route_lookup: dict = rl_result["data"]
        _emit(f"  Route lookup entries: {len(_route_lookup)}")

        # Normalize fc_mh_df if it's the raw Plan fbf master (MH1 + Tag columns)
        # rather than a pre-processed table (fc_mh + fc_mh_key columns)
        fc_mh = fc_mh_df.copy()
        if "fc_mh_key" not in fc_mh.columns:
            _tag_col = next((c for c in fc_mh.columns if "tag" in str(c).lower()), None)
            _mh1_col = next((c for c in fc_mh.columns if _norm_hub_key(str(c)).upper() == "MH1"), None)
            _tag_val = str(cfg.get("fc_mh_tag_value", "FC_MH")).strip().upper()
            if _tag_col and _mh1_col:
                _m = fc_mh[_tag_col].astype(str).str.strip().str.upper() == _tag_val
                fc_mh = fc_mh.loc[_m, [_mh1_col]].copy()
                fc_mh.columns = ["fc_mh"]
                fc_mh["fc_mh_key"] = fc_mh["fc_mh"].map(_norm_hub_key)
                fc_mh = fc_mh.drop_duplicates(subset=["fc_mh_key"]).reset_index(drop=True)
            else:
                issues.append({"type": "fc_mh_normalization_failed",
                                "detail": f"fc_mh_df missing tag/MH1 columns; found: {fc_mh.columns.tolist()}"})

        # Merge fc_mh_df with lat_long_df
        lat_df = lat_long_df.copy()
        # Normalize lat_long_df columns: Site_name/site_name → site_key, Latitude/lat → lat, Longitude/lon → lon
        _lat_col_map: dict[str, str] = {}
        for _c in lat_df.columns:
            _cl = str(_c).lower().strip()
            if "site" in _cl and "name" in _cl and "site_key" not in _lat_col_map.values():
                _lat_col_map[_c] = "site_key"
            elif _cl.startswith("lat") and "lat" not in _lat_col_map.values():
                _lat_col_map[_c] = "lat"
            elif (_cl.startswith("lon") or _cl.startswith("lng")) and "lon" not in _lat_col_map.values():
                _lat_col_map[_c] = "lon"
        if _lat_col_map:
            lat_df = lat_df.rename(columns=_lat_col_map)
        if "site_key" in lat_df.columns:
            lat_df["site_key"] = lat_df["site_key"].map(_norm_hub_key)
        for _num_col in ("lat", "lon"):
            if _num_col in lat_df.columns:
                lat_df[_num_col] = pd.to_numeric(lat_df[_num_col], errors="coerce")
        lat_df = lat_df.dropna(subset=[c for c in ("site_key", "lat", "lon") if c in lat_df.columns])
        lat_df = lat_df.drop_duplicates(subset=["site_key"], keep="first").reset_index(drop=True) if "site_key" in lat_df.columns else lat_df
        fc_mh = fc_mh.merge(lat_df, left_on="fc_mh_key", right_on="site_key", how="left")
        if "site_key" in fc_mh.columns:
            fc_mh = fc_mh.drop(columns=["site_key"])

        # Hub lat/lon lookup
        _hub_lat_lkp: dict[str, tuple[float, float]] = {
            str(r["site_key"]): (float(r["lat"]), float(r["lon"]))
            for _, r in lat_df.iterrows()
            if pd.notna(r.get("lat")) and pd.notna(r.get("lon"))
        } if use_osrm else {}
        _osrm_log: list = []
        _mh_mh_est_log: list = []

        # Portfolio
        _emit("Building DH portfolio …")
        port_result = build_dh_portfolio(plan_vol, fbf_agg_df)
        if port_result["status"] == "failed":
            return {"status": "failed", "data": None, "issues": port_result["issues"]}
        portfolio: pd.DataFrame = port_result["data"]
        portfolio = portfolio.merge(
            lat_df.rename(columns={"site_key": "destination_hub_key"}),
            on="destination_hub_key", how="left",
        )
        n_dh = len(portfolio)
        _emit(f"  Portfolio: {n_dh} DHs | threshold={thr}")

        # Non-FBF plan slice pre-filter
        if "stream" in plan_vol.columns:
            _plan_vol_nonfbf = plan_vol[plan_vol["stream"].astype(str).str.upper() != "FBF"]
        else:
            _plan_vol_nonfbf = plan_vol
        plan_vol_by_dh: dict[str, pd.DataFrame] = {
            str(k): g
            for k, g in _plan_vol_nonfbf.groupby(_plan_vol_nonfbf[dh_col].map(_norm_hub_key), sort=False)
            if str(k)
        }

        # FC position lookup
        _fc_pos_lkp: dict[str, tuple[float, float]] = {
            str(r["fc_mh_key"]): (float(r["lat"]), float(r["lon"]))
            for _, r in fc_mh.iterrows()
            if pd.notna(r.get("lat")) and pd.notna(r.get("lon"))
        }

        # Current FC lookup
        _curr_fc_by_dh: dict[str, list[str]] = {}
        if "last_mh" in plan_vol.columns:
            for _k, _g in plan_vol.groupby(plan_vol[dh_col].map(_norm_hub_key)):
                if not _k:
                    continue
                _keys = _g["last_mh"].dropna().map(_norm_hub_key).dropna().unique()
                _curr_fc_by_dh[str(_k)] = [k for k in _keys if k]

        # OSRM batch pre-scan
        if use_osrm and _hub_lat_lkp:
            _emit("Pre-scanning distance pairs for OSRM batch …")
            _prescan: set[tuple[str, str]] = set()
            for _rh in _route_lookup.values():
                for _i in range(len(_rh) - 1):
                    _prescan.add((_rh[_i], _rh[_i + 1]))
            for _, _prow in portfolio.iterrows():
                _lat_d, _lon_d = _prow.get("lat"), _prow.get("lon")
                if pd.isna(_lat_d) or pd.isna(_lon_d):
                    continue
                _dh_k = str(_prow["destination_hub_key"])
                _cands_pre: list[tuple[str, float]] = []
                for _, _crow in fc_mh.iterrows():
                    if pd.isna(_crow.get("lat")) or pd.isna(_crow.get("lon")):
                        continue
                    _cands_pre.append((
                        str(_crow["fc_mh_key"]),
                        _haversine_km(float(_lat_d), float(_lon_d), float(_crow["lat"]), float(_crow["lon"])),
                    ))
                _cands_pre.sort(key=lambda x: x[1])
                _within_p = [c for c in _cands_pre[:4] if c[1] <= prox_km] if prox_filter_on else []
                _cands_pre = _within_p if _within_p else _cands_pre[:4]
                for _ck_pre, _ in _cands_pre:
                    _prescan.add((_ck_pre, _dh_k))
                    _pr_pre = _pick_pathway_row_for_p1(pathway_df, _ck_pre, _pway_p1c, _pathway_p1_index)
                    if _pr_pre is not None:
                        _p1h = _norm_hub_key(_pr_pre[_pway_p1c])
                        _prescan.add((_p1h, _dh_k))
                        _p2h = _pathway_mh_key(_pr_pre[_pway_p2c]) if _pway_p2c and _pway_p2c in _pr_pre.index else ""
                        if _p2h:
                            _prescan.add((_p2h, _p1h))
            _batch_fetch_osrm_pairs(
                _prescan, _hub_lat_lkp, dist_lookup, cfg,
                max_workers=int(cfg.get("osrm_batch_workers", 4)), emit=_emit,
            )

        truck_mh_mh = float(cfg["truck_cft_mh_mh"])
        missing_all: list = []
        assign_rows: list[dict] = []
        _n_speed = _n_cost = _n_error = 0

        def _tick(result: str) -> None:
            nonlocal _n_speed, _n_cost, _n_error
            if result == "speed":
                _n_speed += 1
            elif result == "cost":
                _n_cost += 1
            else:
                _n_error += 1
            if on_dh_progress is not None:
                on_dh_progress(_dh_idx, n_dh, _n_speed, _n_cost, _n_error)

        _emit(f"Assigning FC_MH for {n_dh} DHs …")
        for _dh_idx, (_, prow) in enumerate(portfolio.iterrows(), start=1):
            dh_key = str(prow["destination_hub_key"])
            lat_d, lon_d = prow.get("lat"), prow.get("lon")
            if pd.isna(lat_d) or pd.isna(lon_d):
                assign_rows.append(_make_error_assign_row(prow, "missing_lat_lon_for_dh"))
                _tick("error")
                continue

            # 4 nearest FC_MH
            cand_list: list[tuple[str, float, float, float]] = []
            for _, crow in fc_mh.iterrows():
                if pd.isna(crow.get("lat")) or pd.isna(crow.get("lon")):
                    continue
                dkm = _haversine_km(float(lat_d), float(lon_d), float(crow["lat"]), float(crow["lon"]))
                cand_list.append((str(crow["fc_mh_key"]), float(crow["lat"]), float(crow["lon"]), dkm))
            cand_list.sort(key=lambda x: x[3])
            cand_list = cand_list[:4]
            if prox_filter_on:
                within_prox = [c for c in cand_list if c[3] <= prox_km]
                if within_prox:
                    cand_list = within_prox

            # Current FC enforcement
            _curr_fc_keys = _curr_fc_by_dh.get(dh_key, [])
            _cand_key_set = {c[0] for c in cand_list}
            if _curr_fc_keys and not any(k in _cand_key_set for k in _curr_fc_keys):
                _forced: list = []
                for _cfk in _curr_fc_keys:
                    _pos = _fc_pos_lkp.get(_cfk)
                    _clat = _pos[0] if _pos else float("nan")
                    _clon = _pos[1] if _pos else float("nan")
                    _cdkm = _haversine_km(float(lat_d), float(lon_d), _clat, _clon) if _pos else float("inf")
                    _forced.append((_cfk, _clat, _clon, _cdkm))
                _forced_keys = {c[0] for c in _forced}
                _nearest_3 = [c for c in cand_list if c[0] not in _forced_keys][:3]
                cand_list = _forced + _nearest_3

            if not cand_list:
                assign_rows.append(_make_error_assign_row(prow, "no_fc_mh_with_lat_lon"))
                _tick("error")
                continue

            top266 = float(prow.get("top266_shipments", 0))
            total_cft = float(prow.get("total_dh_cft", 0))
            use_speed = top266 > thr
            cand_names = [c[0] for c in cand_list] + ["", "", "", ""]
            cand_names = cand_names[:4]

            _dh_fbf_cft = float(prow.get("fbf_cft", 0) or 0)

            evals: list[dict] = []
            for ck, _lat, _lon, _dkm in cand_list:
                pr = _pick_pathway_row_for_p1(pathway_df, ck, _pway_p1c, _pathway_p1_index)
                if pr is None:
                    d1, ok_sp, p1_contrib, p2_contrib = 0.0, True, 0.0, 0.0
                    _p2_hub, _p2_inv = "", 0.0
                else:
                    p1_hub = _norm_hub_key(pr[_pway_p1c])
                    p2_hub = _pathway_mh_key(pr[_pway_p2c]) if _pway_p2c and _pway_p2c in pr.index else ""
                    p1_inv = _parse_pct(pr[_pway_p1pct]) if p1_hub == ck else 0.0
                    p2_raw = pr[_pway_p2c] if _pway_p2c and _pway_p2c in pr.index else None
                    p2_inv = (
                        _parse_pct(pr[_pway_p2pct])
                        if p2_hub and _pway_p2pct and _pway_p2pct in pr.index and _is_real_central_hub(p2_raw)
                        else 0.0
                    )
                    _p2_hub, _p2_inv = p2_hub, p2_inv
                    d1, ok_sp, p1_contrib, p2_contrib = _compute_speed_metrics_raw(
                        dh_key, p1_hub, p2_hub, p1_inv, p2_inv,
                        dist_lookup, load_fn, cfg,
                        missing=missing_all, hub_lat_lkp=_hub_lat_lkp, osrm_log=_osrm_log,
                    )

                slice_dh = plan_vol_by_dh.get(dh_key, _plan_vol_nonfbf.iloc[:0])
                mh_mh, ok_m = _compute_mh_mh_cost_for_candidate(
                    plan_vol, dh_key, ck, cost_lookup, mh_cols, dh_col, missing_all, truck_mh_mh,
                    plan_slice=slice_dh, route_lookup=_route_lookup,
                    p2_hub_key=_p2_hub, p2_inv=_p2_inv, fbf_cft=_dh_fbf_cft,
                    dist_lookup=dist_lookup, hub_lat_lkp=_hub_lat_lkp,
                    osrm_log=_osrm_log, osrm_cfg=cfg,
                    mh_mh_cost_per_km=mh_mh_cost_per_km, mh_mh_est_log=_mh_mh_est_log,
                )
                mh_dh, ok_d = _compute_mh_dh_cost_raw(
                    dh_key, ck, total_cft, dist_lookup, cfg, missing_all,
                    hub_lat_lkp=_hub_lat_lkp, osrm_log=_osrm_log,
                )
                evals.append({
                    "fc_mh": ck,
                    "final_d1_pct": d1 * 100.0,
                    "p1_d1_pct": p1_contrib * 100.0,
                    "p2_d1_pct": p2_contrib * 100.0,
                    "total_cost_rs": mh_mh + mh_dh,
                    "comparison_cost": mh_mh + mh_dh_cost_buffer * mh_dh,
                    "ok": ok_sp and ok_m and ok_d,
                    "mh_mh_rs": mh_mh,
                    "mh_dh_rs": mh_dh,
                })

            cand_cost_map = {e["fc_mh"]: e["total_cost_rs"] for e in evals}
            cand_mhmh_map = {e["fc_mh"]: e.get("mh_mh_rs") for e in evals}
            cand_mhdh_map = {e["fc_mh"]: e.get("mh_dh_rs") for e in evals}

            feasible = [e for e in evals if e["ok"]]
            if not feasible:
                assign_rows.append(_make_error_assign_row(
                    prow, "no_feasible_candidate_missing_distance_or_cost",
                    cand_names=cand_names, cand_cost_map=cand_cost_map,
                    cand_mhmh_map=cand_mhmh_map, cand_mhdh_map=cand_mhdh_map,
                ))
                _tick("error")
                continue

            pick = max(feasible, key=lambda e: e["final_d1_pct"]) if use_speed else min(feasible, key=lambda e: e["comparison_cost"])
            best_name = pick["fc_mh"]

            _curr_fc_evals = [e for e in evals if e["fc_mh"] in set(_curr_fc_keys)]
            _curr_fc_pick = (
                min([e for e in _curr_fc_evals if e.get("ok")] or _curr_fc_evals, key=lambda e: e["total_cost_rs"])
                if _curr_fc_evals else None
            )
            _curr_fc_out_cost = _curr_fc_pick["total_cost_rs"] if _curr_fc_pick else None
            _cost_delta = max(0.0, _curr_fc_out_cost - pick["total_cost_rs"]) if _curr_fc_out_cost is not None else None

            assign_rows.append({
                "destination_hub_key": dh_key,
                "assigned_fc_mh": best_name,
                "assignment_basis": "speed" if use_speed else "cost",
                "final_d1_pct": pick["final_d1_pct"],
                "p1_d1_pct": pick["p1_d1_pct"],
                "p2_d1_pct": pick["p2_d1_pct"],
                "d1_shipments_equiv": (pick["final_d1_pct"] / 100.0) * top266,
                "mh_mh_cost_rs": pick["mh_mh_rs"],
                "mh_dh_cost_rs": pick["mh_dh_rs"],
                "total_cost_rs": pick["total_cost_rs"],
                "current_fc_mh": _curr_fc_pick["fc_mh"] if _curr_fc_pick else None,
                "current_fc_cost_rs": _curr_fc_out_cost,
                "cost_delta_rs": _cost_delta,
                "top266_shipments": top266,
                "total_shipments": float(prow.get("fbf_shipments", 0) or 0)
                                   + float(prow.get("nfbf_shipments", 0) or 0)
                                   + float(prow.get("alphalite_shipments", 0) or 0),
                "total_cft": total_cft,
                "fbf_shipments": prow.get("fbf_shipments"),
                "nfbf_shipments": prow.get("nfbf_shipments"),
                "alphalite_shipments": prow.get("alphalite_shipments"),
                "candidate_1": cand_names[0], "candidate_1_cost_rs": cand_cost_map.get(cand_names[0]),
                "candidate_1_mhmh_cost_rs": cand_mhmh_map.get(cand_names[0]),
                "candidate_1_mhdh_cost_rs": cand_mhdh_map.get(cand_names[0]),
                "candidate_2": cand_names[1], "candidate_2_cost_rs": cand_cost_map.get(cand_names[1]),
                "candidate_2_mhmh_cost_rs": cand_mhmh_map.get(cand_names[1]),
                "candidate_2_mhdh_cost_rs": cand_mhdh_map.get(cand_names[1]),
                "candidate_3": cand_names[2], "candidate_3_cost_rs": cand_cost_map.get(cand_names[2]),
                "candidate_3_mhmh_cost_rs": cand_mhmh_map.get(cand_names[2]),
                "candidate_3_mhdh_cost_rs": cand_mhdh_map.get(cand_names[2]),
                "candidate_4": cand_names[3], "candidate_4_cost_rs": cand_cost_map.get(cand_names[3]),
                "candidate_4_mhmh_cost_rs": cand_mhmh_map.get(cand_names[3]),
                "candidate_4_mhdh_cost_rs": cand_mhdh_map.get(cand_names[3]),
                "notes": "",
            })
            _tick("speed" if use_speed else "cost")

        _emit(f"All {n_dh} DHs processed. Writing outputs …")
        assign_df = pd.DataFrame(assign_rows)

        # Format D1 % columns for CSV
        assign_display = assign_df.copy()
        for _pct_col in ("final_d1_pct", "p1_d1_pct", "p2_d1_pct"):
            if _pct_col in assign_display.columns:
                assign_display[_pct_col] = assign_display[_pct_col].apply(
                    lambda x: f"{x:.4f}%" if pd.notna(x) else ""
                )
        assign_path = output_dir / "dh_fc_mh_assignment.csv"
        assign_display.to_csv(assign_path, index=False)

        # smh_mhlast — BUG FIX via build_smh_mhlast_report
        _emit("Building SMH × MH_Last cost report (with edge completeness check) …")
        smh_result = build_smh_mhlast_report(
            plan_vol, cost_lookup, cfg,
            dist_lookup=dist_lookup, hub_lat_lkp=_hub_lat_lkp,
        )
        smh_agg = smh_result["data"]
        smh_cps_path = output_dir / "smh_mhlast_cost_per_shipment.csv"
        smh_agg.to_csv(smh_cps_path, index=False)

        # Write separate missing-edges file (new output)
        if smh_result["issues"]:
            issues.extend(smh_result["issues"])
        smh_missing_edges_path = output_dir / "smh_missing_rate_card_edges.csv"
        if "edges_missing_from_rate_card" in smh_agg.columns:
            miss_edge_rows = smh_agg[smh_agg["edges_missing_from_rate_card"].astype(str).str.len() > 0][
                ["smh", "mh_last", "edges_missing_from_rate_card"]
            ]
        else:
            miss_edge_rows = pd.DataFrame(columns=["smh", "mh_last", "edges_missing_from_rate_card"])
        miss_edge_rows.to_csv(smh_missing_edges_path, index=False)

        # Missing distance/cost pairs
        miss_set = sorted(set(missing_all))
        miss_rows = [{"from_hub_key": f, "to_hub_key": t, "reason": r,
                      "assumed_distance_km": None, "assumed_cost_per_trip_rs": None}
                     for f, t, r in miss_set]
        osrm_set = sorted(set(_osrm_log), key=lambda x: (x[0], x[1]))
        for f, t, d in osrm_set:
            miss_rows.append({"from_hub_key": f, "to_hub_key": t,
                               "reason": "osrm_fallback", "assumed_distance_km": d,
                               "assumed_cost_per_trip_rs": None})
        est_set = sorted({(f, t, d, c) for f, t, d, c in _mh_mh_est_log}, key=lambda x: (x[0], x[1]))
        for f, t, d, c in est_set:
            miss_rows.append({"from_hub_key": f, "to_hub_key": t,
                               "reason": "mh_mh_cost_estimated",
                               "assumed_distance_km": round(d, 3),
                               "assumed_cost_per_trip_rs": round(c, 2)})
        miss_df = pd.DataFrame(miss_rows, columns=[
            "from_hub_key", "to_hub_key", "reason", "assumed_distance_km", "assumed_cost_per_trip_rs"
        ])
        miss_path = output_dir / "agent3_missing_distance_pairs.csv"
        miss_df.to_csv(miss_path, index=False)

        # Summary
        ok_assign = assign_df[assign_df["assigned_fc_mh"].astype(str).str.len() > 0]
        w_num = float((ok_assign["final_d1_pct"] / 100.0 * ok_assign["top266_shipments"]).sum())
        w_den = float(ok_assign["top266_shipments"].sum()) or 1.0
        summary = pd.DataFrame([{
            "weighted_avg_d1_pct": 100.0 * w_num / w_den,
            "total_network_cost_rs": float(ok_assign["total_cost_rs"].sum()),
            "n_speed_assigned": _n_speed,
            "n_cost_assigned": _n_cost,
            "n_error_rows": _n_error,
            "top266_threshold_used": thr,
            "n_dh_total": len(assign_df),
        }])
        summary_path = output_dir / "agent3_summary.csv"
        summary.to_csv(summary_path, index=False)

        # Validation report
        rep_path = output_dir / "validation_report_agent3.txt"
        body = [
            "=== Agent 3 run ===",
            f"threshold={thr}",
            "",
            "=== Missing distance / cost edges ===",
            f"Count: {len(miss_set)}",
            miss_df.to_string(index=False) if len(miss_df) else "(none)",
            "",
            "=== Summary ===",
            summary.to_string(index=False),
        ]
        rep_path.write_text("\n".join(body), encoding="utf-8")

        # Network map
        _emit("Generating network map HTML …")
        map_path: Optional[Path] = None
        try:
            map_path = output_dir / "hub_network_map.html"
            _build_network_map_html(assign_df, lat_df, map_path)
            _emit(f"  wrote {map_path.name}")
        except Exception as _me:
            _emit(f"  WARNING: network map not written: {_me}")
            map_path = None

        _emit(f"Done. speed={_n_speed}  cost={_n_cost}  errors={_n_error}  "
              f"missing_pairs={len(miss_set)}")

        data = {
            "assignment_csv": str(assign_path),
            "smh_mhlast_cps_csv": str(smh_cps_path),
            "smh_missing_rate_card_edges_csv": str(smh_missing_edges_path),
            "summary_csv": str(summary_path),
            "missing_pairs_csv": str(miss_path),
            "validation_report": str(rep_path),
            "network_map_html": str(map_path) if map_path else None,
            "n_missing_pairs": len(miss_set),
            "n_dh": len(assign_df),
        }
        status = "partial" if issues else "ok"
        return {"status": status, "data": data, "issues": issues}

    except Exception as exc:
        import traceback
        return {
            "status": "failed",
            "data": None,
            "issues": [{"type": "pipeline_error", "detail": traceback.format_exc()}],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2  (public — returns result dict)
# ─────────────────────────────────────────────────────────────────────────────

def run_phase2(
    agent3_output_dir: Path,
    approved_mh_pairs: list[tuple[str, str]],
    plan_vol_df: pd.DataFrame,
    dist_df: pd.DataFrame,
    cost_df: pd.DataFrame,
    mhdh_rate_card_df: pd.DataFrame,
    location_file_df: pd.DataFrame,
    lat_long_df: pd.DataFrame,
    h2h_df: pd.DataFrame,
    cfg: dict[str, Any],
    output_dir: Path,
    *,
    agent4_backend_path: Path,
    pathway_df: Optional[pd.DataFrame] = None,
    residual_threshold: float = 100.0,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """
    Phase 2 cross-MH DH reassignment.

    BUG FIX vs agent3_phase2.run_phase2_pipeline:
    - approved_mh_pairs is an explicit list — no auto-selection via compute_mh_pair_savings.
    - Gap-fill MH-MH costs computed via compute_mhmh_cost (includes FBF P2 leg and route_lookup)
      instead of compute_mhmh_for_pairs (which omitted both).

    agent4_backend_path — directory containing agent4.py.
    Current path: C:\\Users\\aniket.kathuria\\Desktop\\Agentic tools\\Agent4_Routing\\backend

    Returns {"status": "ok"|"partial"|"failed",
             "data": {"excel_paths": [...], "summary_df": DataFrame, "n_pairs": int},
             "issues": [...]}.
    """
    issues: list[dict] = []

    def _prog(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        else:
            print(msg)

    try:
        import sys, tempfile, os
        from agent3_phase2 import (
            load_h2h,
            expand_pool,
            optimize_pool_assignment,
            build_pair_output,
            write_excel_outputs,
            MHPairResult,
            Phase2Result,
            _mhmh_cost_at_mh,
            _run_a4_subset,
            DAYS_PER_MONTH,
        )
        # Agent 4 backend path must point to the rewritten agent4.py location after Agent 4 rewrite is complete.
        _a4_path = str(Path(agent4_backend_path).resolve())
        if _a4_path not in sys.path:
            sys.path.insert(0, _a4_path)
        import agent4_pipeline as p4

        agent3_output_dir = Path(agent3_output_dir)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build lookups
        dl_result = build_distance_lookup(dist_df)
        if dl_result["status"] == "failed":
            return {"status": "failed", "data": None, "issues": dl_result["issues"]}
        dist_lookup: dict = dl_result["data"]

        cl_result = build_cost_lookup(cost_df)
        if cl_result["status"] == "failed":
            return {"status": "failed", "data": None, "issues": cl_result["issues"]}
        cost_lookup: dict = cl_result["data"]

        rl_result = build_route_lookup(plan_vol_df)
        route_lookup: dict = rl_result["data"]

        # Load Agent 3 assignment CSV
        assign_csv = agent3_output_dir / "dh_fc_mh_assignment.csv"
        if not assign_csv.is_file():
            return {"status": "failed", "data": None,
                    "issues": [{"type": "missing_file",
                                "detail": f"dh_fc_mh_assignment.csv not found in {agent3_output_dir}"}]}
        agent3_df = pd.read_csv(assign_csv, dtype=str)

        # H2H network (pre-loaded DataFrame)
        # load_h2h in agent3_phase2 expects a Path; give it a temp file
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as _tmp_h2h:
            h2h_df.to_csv(_tmp_h2h, index=False)
            _tmp_h2h_path = _tmp_h2h.name
        try:
            h2h = load_h2h(Path(_tmp_h2h_path))
        finally:
            os.unlink(_tmp_h2h_path)

        # Location file numeric cols
        location_df = location_file_df.copy()
        location_df.columns = location_df.columns.str.strip()
        for col in ("total_cft", "top266_shipments", "ML",
                    "time_window_start (minutes)", "time_window_end (minutes)",
                    "depot_departure (minutes)"):
            if col in location_df.columns:
                location_df[col] = pd.to_numeric(location_df[col], errors="coerce")

        # Lat/long dict
        latlong: dict[str, tuple[float, float]] = {}
        for _, row in lat_long_df.iterrows():
            name = str(row.get("Site_name", "") or row.get("site_name", "")).strip()
            try:
                latlong[name] = (float(row["Latitude"]), float(row["Longitude"]))
            except (ValueError, TypeError, KeyError):
                pass

        # Distance dict (string keys for agent4)
        dist_dict: dict[tuple[str, str], float] = {}
        for _, row in dist_df.iterrows():
            src = str(row.get("S_Code", "") or row.get("s_code", "")).strip()
            dst = str(row.get("D_Code", "") or row.get("d_code", "")).strip()
            try:
                dist_dict[(src, dst)] = float(row["distance"])
            except (ValueError, TypeError, KeyError):
                pass

        # Load Agent 4 config + rate card (rate card requires temp file for p4.load_rate_card)
        # Merge Agent 4 defaults over Agent 3 cfg so Agent 4 functions get their required keys
        a4_cfg = p4.load_agent4_config() if hasattr(p4, "load_agent4_config") else {}
        merged_cfg = {**a4_cfg, **cfg}
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as _tmp_rc:
            _tmp_rc_path = _tmp_rc.name
        try:
            mhdh_rate_card_df.to_excel(_tmp_rc_path, index=False, engine="openpyxl")
            mh_configs = p4.load_rate_card(Path(_tmp_rc_path), merged_cfg)
        finally:
            os.unlink(_tmp_rc_path)

        _prog(f"Phase 2 — processing {len(approved_mh_pairs)} approved pair(s)")

        pair_results: list = []

        for enum_idx, (from_mh, to_mh) in enumerate(approved_mh_pairs):
            from_mh = str(from_mh).strip()
            to_mh = str(to_mh).strip()

            # Find flagged DHs for this pair from Agent 3 output.
            # Must filter on BOTH current_fc_mh == from_mh AND assigned_fc_mh == to_mh
            # so that DHs reassigning from a *different* from_mh to the same to_mh are
            # not incorrectly included in this pair's pool.
            flagged_rows = agent3_df[
                (agent3_df["current_fc_mh"].astype(str).str.strip() == from_mh) &
                (agent3_df["assigned_fc_mh"].astype(str).str.strip() == to_mh)
            ]
            flagged_orig: list[str] = flagged_rows["destination_hub_key"].astype(str).tolist()

            # Pool expansion via H2H
            pool = expand_pool(flagged_orig, from_mh, to_mh, h2h, agent3_df, location_df)

            _prog(f"[{enum_idx + 1}/{len(approved_mh_pairs)}] {from_mh} -> {to_mh}  "
                  f"({len(flagged_orig)} flagged | pool={len(pool)})")

            # BUG FIX: gap-fill MH-MH costs using compute_mhmh_cost (includes FBF P2 leg)
            mhmh_cache: dict[tuple[str, str], Optional[float]] = {}
            need_gap_fill: list[tuple[str, str]] = []
            for dh in pool:
                r = agent3_df[agent3_df["destination_hub_key"] == dh]
                found_in_candidates = False
                if not r.empty:
                    rv = r.iloc[0]
                    for i in range(1, 5):
                        cand_mh = str(rv.get(f"candidate_{i}", "") or "").strip()
                        if cand_mh.upper() == to_mh.upper():
                            mhmh_val = pd.to_numeric(rv.get(f"candidate_{i}_mhmh_cost_rs"), errors="coerce")
                            if pd.notna(mhmh_val):
                                found_in_candidates = True
                                break
                if not found_in_candidates:
                    need_gap_fill.append((dh, to_mh))

            if need_gap_fill:
                _prog(f"  Gap-fill (compute_mhmh_cost): {len(need_gap_fill)} DH(s) …")
                for dh_gf, mh_gf in need_gap_fill:
                    gf_result = compute_mhmh_cost(
                        dh_key=dh_gf,
                        candidate_key=mh_gf,
                        plan_vol_df=plan_vol_df,
                        cost_lookup=cost_lookup,
                        cfg=cfg,
                        dist_lookup=dist_lookup,
                        route_lookup=route_lookup,
                        pathway_df=pathway_df,
                    )
                    if gf_result["status"] in ("ok", "partial") and gf_result["data"] is not None:
                        mhmh_cache[(dh_gf, mh_gf)] = gf_result["data"]
                    else:
                        mhmh_cache[(dh_gf, mh_gf)] = None
                filled = sum(1 for v in mhmh_cache.values() if v is not None)
                _prog(f"  Gap-fill done: {filled}/{len(need_gap_fill)} resolved")

            # MHMH monthly costs for optimizer scoring
            _DAYS = DAYS_PER_MONTH
            mhmh_monthly_mh1: dict[str, float] = {}
            mhmh_monthly_mh2: dict[str, float] = {}
            for dh in pool:
                _c1, _ = _mhmh_cost_at_mh(dh, from_mh, agent3_df, mhmh_cache)
                _c2, _ = _mhmh_cost_at_mh(dh, to_mh, agent3_df, mhmh_cache)
                mhmh_monthly_mh1[dh] = float(_c1) * _DAYS if _c1 is not None else 0.0
                mhmh_monthly_mh2[dh] = float(_c2) * _DAYS if _c2 is not None else 0.0

            # Before assignments (from current_fc_mh in Agent 3 output)
            before_assign: dict[str, str] = {}
            a3_assign: dict[str, str] = {}
            for dh in pool:
                r = agent3_df[agent3_df["destination_hub_key"] == dh]
                if not r.empty:
                    _a3_rec = str(r.iloc[0].get("assigned_fc_mh", from_mh))
                    a3_assign[dh] = _a3_rec if _a3_rec in (from_mh, to_mh) else from_mh
                    before_assign[dh] = str(r.iloc[0].get("current_fc_mh", from_mh))
                else:
                    a3_assign[dh] = from_mh
                    before_assign[dh] = from_mh

            # Optimize
            _prog(f"  Optimising {len(pool)} pool DHs …")
            best, best_afr, best_atr = optimize_pool_assignment(
                pool_dhs=pool,
                mh1=from_mh, mh2=to_mh,
                dist_dict=dist_dict, latlong=latlong,
                mh_configs=mh_configs, location_df=location_df,
                cfg=merged_cfg, residual_threshold=residual_threshold,
                initial_assignment=a3_assign,
                on_progress=_prog,
                mhmh_monthly_mh1=mhmh_monthly_mh1,
                mhmh_monthly_mh2=mhmh_monthly_mh2,
            )

            # Baseline costs
            from_before = [d for d in pool if before_assign.get(d, from_mh) == from_mh]
            to_before = [d for d in pool if before_assign.get(d, from_mh) == to_mh]
            bfr = _run_a4_subset(from_mh, from_before, dist_dict, latlong,
                                 mh_configs, location_df, merged_cfg, residual_threshold)
            btr = _run_a4_subset(to_mh, to_before, dist_dict, latlong,
                                 mh_configs, location_df, merged_cfg, residual_threshold)
            afr, atr = best_afr, best_atr

            pool_cost_before = bfr.total_monthly_cost + btr.total_monthly_cost
            pool_cost_after = afr.total_monthly_cost + atr.total_monthly_cost
            savings = pool_cost_before - pool_cost_after

            dhs_moved = [d for d in pool if best.get(d) == to_mh]
            dhs_stayed = [d for d in pool if best.get(d, from_mh) == from_mh]

            sheets, pair_mhmh_delta, pair_a3_est_saving = build_pair_output(
                from_mh=from_mh, to_mh=to_mh,
                pool_dhs=pool, best_assignment=best,
                agent3_df=agent3_df, location_df=location_df,
                h2h_df=h2h, dist_dict=dist_dict,
                mh_configs=mh_configs, cfg=merged_cfg,
                before_from_result=bfr, before_to_result=btr,
                after_from_result=afr, after_to_result=atr,
                mhmh_cache=mhmh_cache if mhmh_cache else None,
                flagged_dhs=flagged_orig,
            )

            total_savings_rs = savings + (pair_mhmh_delta or 0.0)
            pair_results.append(MHPairResult(
                from_mh=from_mh, to_mh=to_mh,
                flagged_dhs=flagged_orig, pool_dhs=pool,
                best_assignment=best,
                pool_cost_before=pool_cost_before,
                pool_cost_after=pool_cost_after,
                cost_before=pool_cost_before,
                cost_after=pool_cost_after,
                savings=savings,
                mhmh_delta_rs=pair_mhmh_delta,
                agent3_est_saving_rs=pair_a3_est_saving,
                dhs_moved=dhs_moved, dhs_stayed=dhs_stayed,
                before_from_result=bfr, before_to_result=btr,
                after_from_result=afr, after_to_result=atr,
                sheets=sheets,
                map_html=None,
            ))
            _prog(f"  DONE  MHDH saving: Rs {savings:,.0f}/month  "
                  f"Total: Rs {total_savings_rs:,.0f}/month  "
                  f"({len(dhs_moved)} DHs moved)")

        excel_paths = write_excel_outputs(pair_results, output_dir)
        phase2_result = Phase2Result(
            pair_results=pair_results,
            summary_df=pd.DataFrame([{
                "From_MH": pr.from_mh, "To_MH": pr.to_mh,
                "Agent3_Flagged_DHs": len(pr.flagged_dhs),
                "Pool_DHs": len(pr.pool_dhs),
                "DHs_Moved": len(pr.dhs_moved),
                "MHDH_Saving_Rs": round(pr.savings, 2),
                "MHMH_Saving_Rs": round(pr.mhmh_delta_rs, 2) if pr.mhmh_delta_rs is not None else None,
                "Total_Savings_Rs": round(pr.savings + (pr.mhmh_delta_rs or 0.0), 2),
            } for pr in pair_results]),
            excel_paths=excel_paths,
        )

        return {
            "status": "partial" if issues else "ok",
            "data": {
                "excel_paths": [str(p) for p in excel_paths],
                "summary_df": phase2_result.summary_df,
                "n_pairs": len(pair_results),
            },
            "issues": issues,
        }

    except Exception as exc:
        import traceback
        return {
            "status": "failed",
            "data": None,
            "issues": [{"type": "phase2_error", "detail": traceback.format_exc()}],
        }
