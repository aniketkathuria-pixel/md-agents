"""
Agent 3 — DH to FC_MH clustering (speed vs cost by Top266 threshold).

See ``docs/`` for maths, inputs, and practices.
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


def _norm_hub(s: Any) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    return str(s).strip().upper()


def _norm_hub_key(s: Any) -> str:
    """Normalize hub strings for joins (case-insensitive)."""
    t = _norm_hub(s)
    return re.sub(r"\s+", "", t)


def _is_real_central_hub(val: Any) -> bool:
    """False for blanks and pathway placeholders like ``No P2``."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    t = str(val).strip().upper()
    if not t:
        return False
    if t.startswith("NO P"):
        return False
    return True


def load_agent3_config(path: Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {
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
    }
    if not path.is_file():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in defaults:
                    defaults[k] = v
    except json.JSONDecodeError:
        pass
    return defaults


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance on WGS84 sphere (km)."""
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
    """Fetch road distance (km) between two lat/lon points via OSRM routing API.

    Returns None on any network error, timeout, or unexpected response.
    Note: OSRM expects coordinates as lon,lat (not lat,lon).
    """
    url = (
        f"{base_url.rstrip('/')}/route/v1/driving/"
        f"{lon1},{lat1};{lon2},{lat2}?overview=false"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Agent3-DistanceFallback/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != "Ok":
            return None
        routes = data.get("routes")
        if not routes:
            return None
        dist_m = float(routes[0]["distance"])
        return round(dist_m / 1000.0, 3)
    except Exception:  # noqa: BLE001 — network errors, timeouts, parse errors
        return None


def parse_pct(val: Any) -> float:
    """Parse '69%', '0.69', 0.69 → fraction in [0,1]."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        x = float(val)
        return x if x <= 1.0 else x / 100.0
    s = str(val).strip()
    s = s.replace("%", "")
    try:
        x = float(s)
        return x / 100.0 if x > 1.0 else x
    except ValueError:
        return 0.0


def _read_table(path: Path, sheet: Optional[str] = None) -> pd.DataFrame:
    suf = path.suffix.lower()
    if suf == ".csv":
        return pd.read_csv(path, low_memory=False, encoding="utf-8-sig")
    if suf in (".xlsx", ".xls"):
        return pd.read_excel(path, sheet_name=sheet if sheet else 0, engine="openpyxl")
    raise ValueError(f"Unsupported format: {path}")


def build_distance_lookup(df: pd.DataFrame) -> tuple[dict[tuple[str, str], float], list[str]]:
    """(S_Code, D_Code) → km; directional lookup (forward only). Returns (lookup, column_notes).

    Stores only the direction as written in the CSV.  Reverse-direction fallback is handled
    at query time by ``distance_km`` so that FC→DH is always the primary direction.
    """
    sc = next((df[c] for c in df.columns if _norm_hub_key(c) in ("S_CODE", "SCODE")), None)
    dc = next((df[c] for c in df.columns if _norm_hub_key(c) in ("D_CODE", "DCODE")), None)
    dist_col = next(
        (df[c] for c in df.columns if "distance" in _norm_hub_key(c).lower() or _norm_hub_key(c) == "DISTANCE"),
        None,
    )
    if sc is None or dc is None or dist_col is None:
        raise KeyError("Distance matrix needs S_Code, D_Code, and distance columns")
    out: dict[tuple[str, str], float] = {}
    for a, b, d in zip(sc, dc, dist_col):
        ka, kb = _norm_hub_key(a), _norm_hub_key(b)
        if not ka or not kb:
            continue
        km = float(pd.to_numeric(d, errors="coerce"))
        if np.isnan(km):
            continue
        out[(ka, kb)] = km          # forward direction only; no auto-reverse
    return out, []


def distance_km(
    lookup: dict[tuple[str, str], float],
    a: Any,
    b: Any,
    *,
    missing_pairs: Optional[list[tuple[str, str, str]]] = None,
    reverse_reason: str = "distance_reverse_only",
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
    osrm_log: Optional[list[tuple[str, str, float]]] = None,
    osrm_cfg: Optional[dict[str, Any]] = None,
) -> Optional[float]:
    """Look up distance a→b.

    Priority:
      1. Forward  (a, b) — the canonical direction.
      2. Reverse  (b, a) — fallback; logged in missing_pairs.
      3. OSRM road distance — if hub_lat_lkp is provided and both hubs have
         coordinates, fetches via OSRM, caches in lookup, and logs to osrm_log.
      4. Returns None if all methods fail.
    """
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
    # OSRM fallback
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
                lookup[(ka, kb)] = d_osrm          # cache so subsequent calls skip OSRM
                if osrm_log is not None:
                    osrm_log.append((ka, kb, d_osrm))
                rate = float(cfg.get("osrm_rate_limit_s", 0.15))
                if rate > 0:
                    time.sleep(rate)
                return d_osrm
    return None


def build_cost_lookup(df: pd.DataFrame) -> dict[tuple[str, str], float]:
    """Directed (MH1 → MH2) cost per 2400 CFT trip (Rs)."""
    mh1 = next((df[c] for c in df.columns if _norm_hub_key(c) == "MH1"), None)
    mh2 = next((df[c] for c in df.columns if _norm_hub_key(c) == "MH2"), None)
    cost_c = None
    for c in df.columns:
        nk = _norm_hub_key(c)
        if nk in ("C/T", "C_T", "CT", "COST"):
            cost_c = df[c]
            break
    if cost_c is None:
        for c in df.columns:
            if "/" in str(c) and "C" in str(c).upper():
                cost_c = df[c]
                break
    if mh1 is None or mh2 is None or cost_c is None:
        raise KeyError("Cost matrix needs MH1, MH2, and cost-per-trip column (e.g. C/T)")
    out: dict[tuple[str, str], float] = {}
    for u, v, ct in zip(mh1, mh2, cost_c):
        ku, kv = _norm_hub_key(u), _norm_hub_key(v)
        if not ku or not kv:
            continue
        x = float(pd.to_numeric(ct, errors="coerce"))
        if np.isnan(x):
            continue
        out[(ku, kv)] = x
    return out


def build_load_profile_interp(df: pd.DataFrame) -> Callable[[float], float]:
    """
    Hour-of-day t in [0, 24] → cumulative order fraction [0, 1].
    Uses columns fulfill_item_unit_created_at_hr (int) and Order Profile % (parse).
    """
    hc = next(
        (c for c in df.columns if "fulfill_item" in str(c).lower() and "hr" in str(c).lower()),
        None,
    )
    if hc is None:
        raise KeyError(
            "Load Profile file is missing the hour column. "
            "Expected a column containing 'fulfill_item' and 'hr' "
            f"(e.g. 'fulfill_item_unit_created_at_hr'). Columns found: {list(df.columns)}"
        )
    pc = next(
        (c for c in df.columns if "order" in str(c).lower() and "profile" in str(c).lower()),
        None,
    )
    if pc is None:
        raise KeyError(
            "Load Profile file is missing the order profile % column. "
            "Expected a column containing 'order' and 'profile' "
            f"(e.g. 'Order Profile%'). Columns found: {list(df.columns)}"
        )
    tmp = df[[hc, pc]].copy()
    tmp["_h"] = pd.to_numeric(tmp[hc], errors="coerce").astype("Int64")
    tmp["_p"] = tmp[pc].map(parse_pct)
    tmp = tmp.dropna(subset=["_h"])
    hours = sorted(tmp["_h"].astype(int).unique().tolist())
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

    return interp


def load_fc_mh_table(path: Path, sheet: Optional[str], tag_value: str) -> pd.DataFrame:
    df = _read_table(path, sheet)
    tag_col = next((c for c in df.columns if "tag" in str(c).lower()), None)
    if tag_col is None:
        raise KeyError(
            f"Plan FBF master file '{path.name}' is missing a tag column. "
            "Expected a column whose name contains 'tag' (e.g. 'Tag', 'Tagging'). "
            f"Columns found: {list(df.columns)}"
        )
    mh_col = next((c for c in df.columns if _norm_hub_key(c) == "MH1"), None)
    if mh_col is None:
        raise KeyError(
            f"Plan FBF master file '{path.name}' is missing the MH1 column. "
            "Expected a column normalising to 'MH1' (e.g. 'MH1', 'MH 1'). "
            f"Columns found: {list(df.columns)}"
        )
    tv = str(tag_value).strip().upper()
    m = df[tag_col].astype(str).str.strip().str.upper() == tv
    out = df.loc[m, [mh_col]].copy()
    out.columns = ["fc_mh"]
    out["fc_mh_key"] = out["fc_mh"].map(_norm_hub_key)
    return out.drop_duplicates(subset=["fc_mh_key"])


def load_lat_long_table(path: Path, sheet: Optional[str]) -> pd.DataFrame:
    df = _read_table(path, sheet)
    sc = next(
        (c for c in df.columns if "site" in str(c).lower() and "name" in str(c).lower()),
        None,
    )
    if sc is None:
        raise KeyError(
            f"Lat/Long file '{path.name}' is missing a site name column. "
            "Expected a column whose name contains both 'site' and 'name' "
            f"(e.g. 'Site_name'). Columns found: {list(df.columns)}"
        )
    latc = next((c for c in df.columns if str(c).lower().startswith("lat")), None)
    if latc is None:
        raise KeyError(
            f"Lat/Long file '{path.name}' is missing a latitude column. "
            "Expected a column starting with 'lat' (e.g. 'Latitude', 'lat'). "
            f"Columns found: {list(df.columns)}"
        )
    lonc = next((c for c in df.columns if str(c).lower().startswith("lon")), None)
    if lonc is None:
        raise KeyError(
            f"Lat/Long file '{path.name}' is missing a longitude column. "
            "Expected a column starting with 'lon' (e.g. 'Longitude', 'lon'). "
            f"Columns found: {list(df.columns)}"
        )
    out = pd.DataFrame(
        {
            "site_key": df[sc].map(_norm_hub_key),
            "lat": pd.to_numeric(df[latc], errors="coerce"),
            "lon": pd.to_numeric(df[lonc], errors="coerce"),
        }
    )
    out = out.dropna(subset=["site_key", "lat", "lon"])
    # One row per hub: source sheets often repeat Site_name; merge would duplicate portfolio rows.
    return out.drop_duplicates(subset=["site_key"], keep="first").reset_index(drop=True)


def _pathway_mh_key(val: Any) -> str:
    """Pathway Excel uses 'No P3' style placeholders — treat as absent."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return ""
    su = re.sub(r"\s+", "", s.upper())
    if su.startswith("NOP") and len(su) <= 5:
        return ""
    return _norm_hub_key(val)


def extract_hops_from_plan_row(row: pd.Series, mh_cols: list[str]) -> list[str]:
    """Extract ordered hub-key hops from a plan_vol row.

    Uses prenormalized ``_k_<col>`` columns when present (added by ``run_agent3`` before the
    main loop) to avoid repeated ``_norm_hub_key`` calls per row.  Falls back to live
    normalization for callers that pass rows without those columns.
    """
    hops: list[str] = []
    for c in mh_cols:
        key_col = f"_k_{c}"
        if key_col in row.index:
            k = row[key_col]
            if not k:  # empty string → end of hop chain
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


def _plan_row_uses_mh_mh_rate_card(source_type: Any) -> bool:
    """
    Whether the **resort / first-hop** policy uses the MH1–MH2 rate card for **every** MH–MH leg on the row.

    When **True** (**MH** or **FC_MH**): all edges from :func:`edges_to_candidate` use the rate card.

    When **False** (**PH**, **ALITE**, **FC**, **MFC**, …): only the **first** MH→MH leg (MH1→MH2 along the
    built hop list) is charged **0**; **MH2→MH3→…** still use the rate card and require matrix pairs.
    """
    if source_type is None or (isinstance(source_type, float) and pd.isna(source_type)):
        return True
    s = str(source_type).strip()
    if not s or str(s).lower() == "nan":
        return True
    u = re.sub(r"\s+", "", s.upper())
    if u in ("FC_MH", "FCMH"):
        return True
    if u == "MH":
        return True
    return False


def edges_to_candidate(hops: list[str], candidate_key: str) -> list[tuple[str, str]]:
    """Ordered MH–MH edges from first hop to candidate (append last→candidate if needed)."""
    if not hops:
        return []
    if candidate_key in hops:
        idx = hops.index(candidate_key)
        seq = hops[: idx + 1]
    else:
        seq = list(hops)
        if seq[-1] != candidate_key:
            seq = seq + [candidate_key]
    return [(seq[i], seq[i + 1]) for i in range(len(seq) - 1)]


TOP266_COLS = [
    "fbf_avg_daily_5sc_top16_pin",
    "fbf_avg_daily_sha_top16_pin",
    "fbf_avg_daily_5sc_next50_pin",
    "fbf_avg_daily_sha_next50_pin",
    "fbf_avg_daily_5sc_next200_pin",
    "fbf_avg_daily_sha_next200_pin",
]

LBU_SHIP_COLS = ["fbf_avg_daily_shipments_5sc_core", "fbf_avg_daily_shipments_sha_core"]


def build_dh_portfolio(
    plan_vol: pd.DataFrame,
    fbf_agg: pd.DataFrame,
    *,
    mh_cols: Optional[list[str]] = None,
) -> pd.DataFrame:
    """One row per DH (LMHub / destination_hub key = site_key)."""
    if mh_cols is None:
        mh_cols = [c for c in plan_vol.columns if re.match(r"^MH\d+$", str(c), re.I)]
    dh_col = "LMHub" if "LMHub" in plan_vol.columns else "lmhub"
    if dh_col not in plan_vol.columns:
        raise KeyError("plan_volume must contain LMHub")
    st = plan_vol.get("stream", pd.Series("", index=plan_vol.index)).astype(str).str.upper()
    ship = pd.to_numeric(plan_vol.get("median_demand_shipments", 0), errors="coerce").fillna(0.0)
    cft = pd.to_numeric(plan_vol.get("plan_median_cft_volume", 0), errors="coerce").fillna(0.0)

    rows: list[dict[str, Any]] = []
    for dh, g in plan_vol.groupby(plan_vol[dh_col].map(_norm_hub_key)):
        if not dh:
            continue
        m_n = st.loc[g.index] == "NFBF"
        m_a = st.loc[g.index] == "ALITE"
        nfbf_s = float(ship.loc[g.index][m_n].sum())
        nfbf_c = float(cft.loc[g.index][m_n].sum())
        al_s = float(ship.loc[g.index][m_a].sum())
        al_c = float(cft.loc[g.index][m_a].sum())
        rows.append(
            {
                "destination_hub_key": dh,
                "nfbf_shipments": nfbf_s,
                "nfbf_cft": nfbf_c,
                "alphalite_shipments": al_s,
                "alphalite_cft": al_c,
                "plan_rows": int(len(g)),
            }
        )
    port = pd.DataFrame(rows)
    fbf_agg = fbf_agg.copy()
    if "destination_hub" not in fbf_agg.columns:
        raise KeyError("fbf_plan_dh_aggregate.csv must contain column 'destination_hub'")
    fbf_agg["destination_hub_key"] = fbf_agg["destination_hub"].map(_norm_hub_key)
    merge_cols = ["fbf_avg_daily_shipments_all", "cft_cuft_day_avg_all"]
    for c in TOP266_COLS + LBU_SHIP_COLS:
        if c in fbf_agg.columns:
            merge_cols.append(c)
    missing_required = [c for c in ("fbf_avg_daily_shipments_all", "cft_cuft_day_avg_all") if c not in fbf_agg.columns]
    if missing_required:
        raise KeyError(
            "fbf_plan_dh_aggregate.csv missing columns: "
            + ", ".join(missing_required)
            + ". Run Agent 1 FBF plan aggregate."
        )
    extra = fbf_agg.set_index("destination_hub_key")[merge_cols].rename(
        columns={
            "fbf_avg_daily_shipments_all": "fbf_shipments",
            "cft_cuft_day_avg_all": "fbf_cft",
        }
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
    return port


def pick_pathway_row_for_p1(
    pathway: pd.DataFrame,
    p1_key: str,
    p1c: str,
    _p1_index: Optional[dict[str, Any]] = None,
) -> Optional[pd.Series]:
    """Return first pathway row whose normalised p1c value equals p1_key.

    Pass ``_p1_index`` (built once before the loop via ``build_pathway_p1_index``) to
    avoid re-normalising the full column on every call (O(1) lookup vs O(n) map).
    Falls back to the column-scan path when the index is not provided.
    """
    if _p1_index is not None:
        row_idx = _p1_index.get(p1_key)
        if row_idx is None:
            return None
        return pathway.loc[row_idx]
    # Fallback (backward-compatible; used by callers that don't pass an index)
    m = pathway[p1c].map(_norm_hub_key) == p1_key
    sub = pathway.loc[m]
    if len(sub) == 0:
        return None
    return sub.iloc[0]


def build_pathway_p1_index(pathway: pd.DataFrame, p1c: str) -> dict[str, Any]:
    """Pre-compute normalised-P1-key → DataFrame row index (first occurrence wins).

    Build once before the per-DH loop; pass the result to ``pick_pathway_row_for_p1``
    to replace O(n) column normalisation with an O(1) dict lookup per candidate.
    """
    index: dict[str, Any] = {}
    for row_idx, raw_val in pathway[p1c].items():
        nk = _norm_hub_key(raw_val)
        if nk not in index:
            index[nk] = row_idx
    return index


def compute_speed_metrics(
    dh_key: str,
    p1_hub_key: str,
    p2_hub_key: str,
    p1_inv: float,
    p2_inv: float,
    dist_lookup: dict[tuple[str, str], float],
    load_fn: Callable[[float], float],
    cfg: dict[str, Any],
    *,
    missing: list[tuple[str, str, str]],
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
    osrm_log: Optional[list[tuple[str, str, float]]] = None,
) -> tuple[float, bool]:
    """
    Returns (final_d1_fraction, ok_all_distances).
    ``p1_inv`` / ``p2_inv`` are pathway inventory fractions in [0, 1] (P1 only when FC_MH == P1 central; see docs).
    """
    p1_inv = max(0.0, min(1.0, float(p1_inv)))
    p2_inv = max(0.0, min(1.0, float(p2_inv)))
    if p1_inv <= 0.0 and p2_inv <= 0.0:
        return 0.0, True, 0.0, 0.0

    v_kmh = float(cfg["truck_speed_kmh"])
    t_mh_dh = float(cfg["mh_dh_processing_hours"])
    t_mh_mh = float(cfg["mh_mh_processing_hours"])
    # h_cut is 6am Day N+1; measured from Day N midnight that is hour 30
    h_cut_abs = float(cfg["dh_arrival_cutoff_hour"]) + 24.0

    _osrm_kw = dict(hub_lat_lkp=hub_lat_lkp, osrm_log=osrm_log, osrm_cfg=cfg)
    d_p1_dh = distance_km(dist_lookup, p1_hub_key, dh_key, **_osrm_kw)
    if d_p1_dh is None:
        missing.append((p1_hub_key, dh_key, "speed_p1_dh"))
        return 0.0, False, 0.0, 0.0
    tt_p1 = d_p1_dh / v_kmh
    # Latest hour on Day N the truck can depart P1 and still arrive at DH by h_cut_abs
    depart_p1 = h_cut_abs - tt_p1 - t_mh_dh
    d1_p1 = load_fn(depart_p1)  # load_fn clamps to [0,24]; returns 0 if infeasible, 1 if trivially feasible

    d1_p2 = 0.0
    if p2_inv > 0.0 and p2_hub_key:
        d_p2_p1 = distance_km(dist_lookup, p2_hub_key, p1_hub_key, **_osrm_kw)
        if d_p2_p1 is None:
            missing.append((p2_hub_key, p1_hub_key, "speed_p2_p1"))
            p1_only = max(0.0, min(1.0, d1_p1 * p1_inv))
            return p1_only, False, d1_p1 * p1_inv, 0.0
        tt_p2 = d_p2_p1 / v_kmh
        # Latest hour on Day N the truck can depart P2 for the full P2→P1→DH chain
        depart_p2 = h_cut_abs - tt_p2 - t_mh_mh - tt_p1 - t_mh_dh
        d1_p2 = load_fn(depart_p2)

    p1_contrib = d1_p1 * p1_inv
    p2_contrib = d1_p2 * p2_inv
    final_d1 = max(0.0, min(1.0, p1_contrib + p2_contrib))
    return final_d1, True, p1_contrib, p2_contrib


def _resolve_smh_to_dmh(
    smh_key: str,
    dmh_x_key: str,
    dmh_y_key: str,
    original_hops: list[str],
    route_lookup: dict[tuple[str, str], list[str]],
) -> list[str]:
    """Return the hop sequence from SMH to DMH(y).

    Priority:
    1. Direct lookup: any existing plan row with (SMH, DMH_y).
    2. Fallback: original chain (SMH→…→DMH_x) + DMH_y appended.
    """
    if smh_key == dmh_y_key:
        return [smh_key]
    found = route_lookup.get((smh_key, dmh_y_key))
    if found:
        return found
    # Fallback — extend original chain to reach DMH_y
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
    missing_pairs: list[tuple[str, str, str]],
    mh_mh_est_log: Optional[list[tuple[str, str, float, float]]],
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]],
    osrm_log: Optional[list[tuple[str, str, float]]],
    osrm_cfg: Optional[dict[str, Any]],
) -> Optional[float]:
    """Return trip cost (Rs) for edge u→v.

    Priority:
    1. Rate card forward (u, v).
    2. Rate card reverse (v, u).
    3. Distance × mh_mh_cost_per_km fallback (uses dist_lookup + OSRM).
       Logs to mh_mh_est_log so the estimate is visible in the missing pairs CSV.
    Returns None only when all three methods fail.
    """
    trip = cost_lookup.get((u, v)) or cost_lookup.get((v, u))
    if trip is not None:
        return float(trip)
    # Distance-based fallback
    if dist_lookup is not None and mh_mh_cost_per_km > 0:
        d = distance_km(
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


def compute_mh_mh_cost_for_candidate(
    plan_vol: pd.DataFrame,
    dh_key: str,
    candidate_key: str,
    cost_lookup: dict[tuple[str, str], float],
    mh_cols: list[str],
    dh_col: str,
    missing_pairs: list[tuple[str, str, str]],
    truck_cft_mh_mh: float,
    *,
    plan_slice: Optional[pd.DataFrame] = None,
    route_lookup: Optional[dict[tuple[str, str], list[str]]] = None,
    p2_hub_key: str = "",
    p2_inv: float = 0.0,
    fbf_cft: float = 0.0,
    dist_lookup: Optional[dict[tuple[str, str], float]] = None,
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
    osrm_log: Optional[list[tuple[str, str, float]]] = None,
    osrm_cfg: Optional[dict[str, Any]] = None,
    mh_mh_cost_per_km: float = 0.0,
    mh_mh_est_log: Optional[list[tuple[str, str, float, float]]] = None,
) -> tuple[float, bool]:
    """
    MH-MH cost for a candidate FC_MH (= DMH_y) serving a DH.

    NFBF / ALITE lanes: SMH is fixed (seller origin).  Route is re-resolved:
      1. Look up (SMH, DMH_y) in route_lookup for an existing plan path.
      2. Fallback: original lane chain + DMH_y appended.

    FBF lanes: ignored here — cost comes from the P2 leg below.

    FBF P2 leg: P2_hub → DMH_y (candidate), volume = p2_inv × fbf_cft.
      Route also resolved via route_lookup; direct edge used as fallback.

    For any edge missing from the rate card, falls back to distance × mh_mh_cost_per_km.
    Estimated edges are logged to mh_mh_est_log.
    """
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

    if plan_slice is not None:
        sub = plan_slice
    else:
        sub = plan_vol[plan_vol[dh_col].map(_norm_hub_key) == dh_key]

    use_rate_card_col = "source_type" in plan_vol.columns

    for _, row in sub.iterrows():
        stream = str(row.get("stream", "")).strip().upper()
        if stream == "FBF":
            continue

        hops = extract_hops_from_plan_row(row, mh_cols)
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

        zero_first_mh_mh_leg = use_rate_card_col and not _plan_row_uses_mh_mh_rate_card(row.get("source_type"))
        for ei, (u, v) in enumerate(edges):
            if zero_first_mh_mh_leg and ei == 0:
                continue
            trip = _trip_cost_with_fallback(u, v, **_cost_kw)
            if trip is None:
                ok = False
                continue
            total += (cft / max(truck_cft_mh_mh, 1e-9)) * trip

    # FBF P2 leg: goods at P2 Central Hub need to travel to P1 (= candidate FC_MH)
    if p2_inv > 0.0 and p2_hub_key and fbf_cft > 0.0:
        p2_key = _norm_hub_key(p2_hub_key)
        p2_vol_cft = p2_inv * fbf_cft
        if p2_key and p2_key != candidate_key:
            p2_route = _rlkp.get((p2_key, candidate_key))
            p2_hops = p2_route if p2_route else [p2_key, candidate_key]
            p2_edges = [(p2_hops[i], p2_hops[i + 1]) for i in range(len(p2_hops) - 1)]
            for u, v in p2_edges:
                trip = _trip_cost_with_fallback(u, v, **_cost_kw)
                if trip is None:
                    ok = False
                    continue
                total += (p2_vol_cft / max(truck_cft_mh_mh, 1e-9)) * trip

    return total, ok


def compute_mh_dh_cost(
    dh_key: str,
    candidate_key: str,
    total_cft: float,
    dist_lookup: dict[tuple[str, str], float],
    cfg: dict[str, Any],
    missing_pairs: list[tuple[str, str, str]],
    *,
    hub_lat_lkp: Optional[dict[str, tuple[float, float]]] = None,
    osrm_log: Optional[list[tuple[str, str, float]]] = None,
) -> tuple[float, bool]:
    # Primary direction: FC (candidate_key) → DH; fallback: DH → FC (reverse logged as missing)
    d = distance_km(
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
    two_way = 2.0 * d
    cost = (max(total_cft, 0.0) / base) * two_way * rate
    return float(cost), True


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
        if _di < 10:          dh_delta_color[_dn] = "#cc0000"   # top 10 → deep red
        elif _di < 30:        dh_delta_color[_dn] = "#e85d04"   # next 20 → vivid deep orange
        elif _dv >= 3000:     dh_delta_color[_dn] = "#f4a100"   # rest ≥ 3k → golden amber
        else:                 dh_delta_color[_dn] = "#374151"   # < 3k but > 0 → dark charcoal
    # delta == 0 or None → #c8ccd0 (light grey, applied as JS default)

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
    # DHs with delta == 0 or None → tier 'grey' (JS default)
    dh_tier_js = _json.dumps(dh_tier_dict, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MH\u2013DH Network Map</title>
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
  if (v===null||v===undefined) return '\u2014';
  return typeof v==='number' && !isNaN(v) ? v.toLocaleString(undefined,{{maximumFractionDigits:2}}) : v;
}}
function fmtPct(v) {{
  if (v===null||v===undefined) return '\u2014';
  return typeof v==='number' ? (v*100).toFixed(1)+'%' : v;
}}
function switchView(v) {{
  currentView = v;
  document.getElementById('btn-mh-view').classList.toggle('active', v === 'mh');
  document.getElementById('btn-delta-view').classList.toggle('active', v === 'delta');
  // delta-legend always visible; tier filter only applies in delta view
  dhMarkerRefs.forEach(function(ref) {{
    if (v === 'delta') {{
      ref.marker.setIcon(makeDHIconLarge(DH_DELTA_COLOR[ref.name] || '#c8ccd0'));
    }} else {{
      ref.marker.setIcon(makeDHIcon(MH_COLOR[ref.mh] || '#aaa'));
    }}
  }});
  mhMarkerRefs.forEach(function(ref) {{
    if (v === 'delta') {{
      ref.marker.setIcon(makeMHIcon('#6b7280'));   // neutral grey pin in delta view (stays visible)
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
        <tr><td>Assignment Basis</td><td>${{dh.assignment_basis||'\u2014'}}</td></tr>
        <tr><td>D1 %</td><td>${{fmtPct(dh.final_d1_pct)}}</td></tr>
        <tr><td>Top 266 Shipments</td><td>${{fmtNum(dh.top266_shipments)}}</td></tr>
        <tr><td>Total Shipments</td><td>${{fmtNum(dh.total_shipments)}}</td></tr>
        <tr><td>Suggested FC Cost (Rs)</td><td>${{fmtNum(dh.total_cost_rs)}}</td></tr>
        <tr><td>Current FC</td><td>${{dh.current_fc||'\u2014'}}</td></tr>
        <tr><td>Current FC Cost (Rs)</td><td>${{fmtNum(dh.current_fc_cost)}}</td></tr>
        <tr${{deltaClass}}><td>Cost Delta (Rs)</td><td>${{dh.cost_delta!==null ? fmtNum(dh.cost_delta) : '\u2014'}}</td></tr>
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


def _make_error_assign_row(
    prow: pd.Series,
    notes: str,
    *,
    cand_names: Optional[list[str]] = None,
    cand_cost_map:  Optional[dict[str, Any]] = None,
    cand_mhmh_map:  Optional[dict[str, Any]] = None,
    cand_mhdh_map:  Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a standardised error row for the assignment output."""
    fbf_s  = float(prow.get("fbf_shipments") or 0)
    nfbf_s = float(prow.get("nfbf_shipments") or 0)
    al_s   = float(prow.get("alphalite_shipments") or 0)
    _cn   = list(cand_names or []) + ["", "", "", ""]
    _cm   = cand_cost_map  or {}
    _cmh  = cand_mhmh_map  or {}
    _cmdh = cand_mhdh_map  or {}
    return {
        "destination_hub_key": str(prow["destination_hub_key"]),
        "assigned_fc_mh": "",
        "assignment_basis": "error",
        "final_d1_pct": None,
        "p1_d1_pct": None,
        "p2_d1_pct": None,
        "d1_shipments_equiv": None,
        "mh_mh_cost_rs": None,
        "mh_dh_cost_rs": None,
        "total_cost_rs": None,
        "top266_shipments": float(prow.get("top266_shipments", 0)),
        "total_shipments": fbf_s + nfbf_s + al_s,
        "total_cft": float(prow.get("total_dh_cft", 0)),
        "fbf_shipments": prow.get("fbf_shipments"),
        "nfbf_shipments": prow.get("nfbf_shipments"),
        "alphalite_shipments": prow.get("alphalite_shipments"),
        "candidate_1": _cn[0],
        "candidate_1_cost_rs":      _cm.get(_cn[0])   if _cn[0] else None,
        "candidate_1_mhmh_cost_rs": _cmh.get(_cn[0])  if _cn[0] else None,
        "candidate_1_mhdh_cost_rs": _cmdh.get(_cn[0]) if _cn[0] else None,
        "candidate_2": _cn[1],
        "candidate_2_cost_rs":      _cm.get(_cn[1])   if _cn[1] else None,
        "candidate_2_mhmh_cost_rs": _cmh.get(_cn[1])  if _cn[1] else None,
        "candidate_2_mhdh_cost_rs": _cmdh.get(_cn[1]) if _cn[1] else None,
        "candidate_3": _cn[2],
        "candidate_3_cost_rs":      _cm.get(_cn[2])   if _cn[2] else None,
        "candidate_3_mhmh_cost_rs": _cmh.get(_cn[2])  if _cn[2] else None,
        "candidate_3_mhdh_cost_rs": _cmdh.get(_cn[2]) if _cn[2] else None,
        "candidate_4": _cn[3],
        "candidate_4_cost_rs":      _cm.get(_cn[3])   if _cn[3] else None,
        "candidate_4_mhmh_cost_rs": _cmh.get(_cn[3])  if _cn[3] else None,
        "candidate_4_mhdh_cost_rs": _cmdh.get(_cn[3]) if _cn[3] else None,
        "notes": notes,
    }


def _batch_fetch_osrm_pairs(
    pairs: set[tuple[str, str]],
    hub_lat_lkp: dict[str, tuple[float, float]],
    dist_lookup: dict[tuple[str, str], float],
    cfg: dict[str, Any],
    *,
    max_workers: int = 4,
    emit: Optional[Callable[[str], None]] = None,
) -> int:
    """Fetch all missing (a, b) pairs from OSRM in parallel; cache results in dist_lookup.

    Only fetches pairs where both hubs have coordinates and the pair (or its reverse) is not
    already in dist_lookup.  Returns the count of newly resolved pairs.
    """
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
                base_url=base_url,
                timeout=timeout,
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


def run_agent3(
    *,
    plan_volume_path: Path,
    fbf_dh_aggregate_path: Path,
    pathway_wide_path: Path,
    plan_fbf_master_path: Path,
    lat_long_path: Path,
    load_profile_path: Path,
    distance_matrix_path: Path,
    cost_matrix_path: Path,
    out_dir: Path,
    cfg: dict[str, Any],
    top266_threshold: Optional[float] = None,
    proximity_km_threshold: Optional[float] = None,
    plan_fbf_sheet: Optional[str] = None,
    lat_long_sheet: Optional[str] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    on_dh_progress: Optional[Callable[[int, int, int, int, int], None]] = None,
) -> dict[str, Any]:
    """
    Execute full Agent 3 pipeline. Writes CSVs and ``validation_report_agent3.txt``.

    Returns a small report dict (counts, paths).
    ``on_progress`` is called with a status string at each major stage and per DH.
    """
    def _emit(msg: str) -> None:
        print(msg)
        if on_progress is not None:
            on_progress(msg)

    if not fbf_dh_aggregate_path.is_file():
        raise FileNotFoundError(
            f"Missing FBF DH aggregate: {fbf_dh_aggregate_path}. "
            "Run Agent 1 `backend/fbf_plan_aggregate.py` to produce fbf_plan_dh_aggregate.csv."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    thr = float(top266_threshold if top266_threshold is not None else cfg.get("default_top266_threshold", 5.0))
    # Proximity: among the 4 haversine-nearest FC_MHs, if any is within prox_km, keep only those
    # within prox_km (can shrink to 1–3 candidates). <= 0 turns this off (always keep all 4 nearest).
    if proximity_km_threshold is not None and float(proximity_km_threshold) <= 0:
        prox_filter_on = False
        prox_km = 0.0
    elif proximity_km_threshold is not None:
        prox_filter_on = True
        prox_km = float(proximity_km_threshold)
    else:
        prox_filter_on = True
        prox_km = float(cfg.get("default_proximity_km_threshold", 80.0))
    mh_dh_cost_buffer = float(cfg.get("mh_dh_cost_buffer", 1.15))
    use_osrm = bool(cfg.get("use_osrm_fallback", True))
    mh_mh_cost_per_km = float(cfg.get("mh_mh_cost_per_km_fallback", 49.0))
    tag_val = str(cfg.get("fc_mh_tag_value", "FC_MH"))
    sheet_master = plan_fbf_sheet or cfg.get("plan_fbf_master_sheet")
    sheet_ll = lat_long_sheet or cfg.get("lat_long_sheet")

    _emit("Loading input files…")
    plan_vol = _read_table(plan_volume_path)
    fbf_agg = _read_table(fbf_dh_aggregate_path)
    pathway = _read_table(pathway_wide_path)
    load_pf = _read_table(load_profile_path)
    dist_df = _read_table(distance_matrix_path)
    cost_df = _read_table(cost_matrix_path)
    _emit(f"  plan_volume:     {len(plan_vol)} rows")
    _emit(f"  fbf_dh_agg:      {len(fbf_agg)} rows")
    _emit(f"  pathway_wide:    {len(pathway)} rows")
    _emit(f"  distance_matrix: {len(dist_df)} rows")

    _emit("Building lookups (distance, cost, load profile)…")
    dist_lookup, _ = build_distance_lookup(dist_df)
    cost_lookup = build_cost_lookup(cost_df)
    load_fn = build_load_profile_interp(load_pf)
    _emit(f"  distance pairs:  {len(dist_lookup)}")
    _emit(f"  cost pairs:      {len(cost_lookup)}")

    _emit("Loading FC_MH table and lat/long…")
    fc_mh_df = load_fc_mh_table(plan_fbf_master_path, sheet_master, tag_val)
    lat_df = load_lat_long_table(lat_long_path, sheet_ll)
    _emit(f"  FC_MH hubs:      {len(fc_mh_df)}")
    _emit(f"  lat/long sites:  {len(lat_df)}")

    # Hub lat/lon lookup for OSRM fallback: normalized site_key → (lat, lon)
    _hub_lat_lkp: dict[str, tuple[float, float]] = {
        str(r["site_key"]): (float(r["lat"]), float(r["lon"]))
        for _, r in lat_df.iterrows()
        if pd.notna(r.get("lat")) and pd.notna(r.get("lon"))
    } if use_osrm else {}
    _osrm_log: list[tuple[str, str, float]] = []
    _mh_mh_est_log: list[tuple[str, str, float, float]] = []   # (from, to, dist_km, est_cost_per_trip)
    if use_osrm:
        _emit(f"  OSRM fallback enabled ({len(_hub_lat_lkp)} hubs with coordinates)")
    _emit(f"  MH-MH cost fallback: Rs {mh_mh_cost_per_km}/km when rate card missing")

    mh_cols = [c for c in plan_vol.columns if re.match(r"^MH\d+$", str(c), re.I)]
    dh_col = "LMHub" if "LMHub" in plan_vol.columns else next(
        (c for c in plan_vol.columns if str(c).lower() == "lmhub"), None
    )
    if dh_col is None:
        raise KeyError(
            "plan_volume.csv is missing the LMHub column. "
            f"Columns found: {list(plan_vol.columns)}"
        )

    # Pre-normalize MH hub key columns once — extract_hops_from_plan_row reads _k_MH* instead of
    # calling _norm_hub_key on every cell for every row during route_lookup build and per-DH loop.
    for _mc in mh_cols:
        plan_vol[f"_k_{_mc}"] = plan_vol[_mc].map(_norm_hub_key)

    # Validate pathway column names once before the per-DH loop.
    _pway_p1c = next(
        (c for c in pathway.columns if "p1" in str(c).lower() and "central" in str(c).lower()), None
    )
    _pway_p2c = next(
        (c for c in pathway.columns if "p2" in str(c).lower() and "central" in str(c).lower()), None
    )
    _pway_p1pct = next(
        (c for c in pathway.columns if "p1" in str(c).lower() and "pct" in str(c).lower()), None
    )
    _pway_p2pct = next(
        (c for c in pathway.columns if "p2" in str(c).lower() and "pct" in str(c).lower()), None
    )
    _missing_pway = [
        name
        for name, val in (
            ("p1 central hub", _pway_p1c),
            ("p2 central hub", _pway_p2c),
            ("p1 pct/share", _pway_p1pct),
            ("p2 pct/share", _pway_p2pct),
        )
        if val is None
    ]
    # Pre-build P1 lookup index once — avoids re-normalising the full pathway column per DH×candidate.
    _pathway_p1_index: dict[str, Any] = build_pathway_p1_index(pathway, _pway_p1c) if _pway_p1c else {}

    if _missing_pway:
        raise KeyError(
            f"fbf_network_pathway_wide.csv is missing required columns: {_missing_pway}. "
            f"Columns found: {list(pathway.columns)}"
        )

    # Exclude FBF rows from the per-DH slice used in MH-MH cost iteration.
    # FBF rows are always skipped inside compute_mh_mh_cost_for_candidate (FBF cost comes from
    # the P2 leg, not from iterating plan rows).  Filtering here avoids loading and discarding
    # them on every DH × candidate pass.  Full plan_vol (with FBF) is still used for
    # _route_lookup and build_dh_portfolio so FBF routes and volumes are preserved.
    if "stream" in plan_vol.columns:
        _plan_vol_nonfbf = plan_vol[plan_vol["stream"].astype(str).str.upper() != "FBF"]
    else:
        _plan_vol_nonfbf = plan_vol
    plan_vol_by_dh: dict[str, pd.DataFrame] = {
        str(k): g
        for k, g in _plan_vol_nonfbf.groupby(_plan_vol_nonfbf[dh_col].map(_norm_hub_key), sort=False)
        if str(k)
    }

    # Build global (SMH, DMH) → hop_list lookup for route resolution.
    # Used by compute_mh_mh_cost_for_candidate to find SMH→DMH(y) routes.
    _route_lookup: dict[tuple[str, str], list[str]] = {}
    for _, _rv in plan_vol.iterrows():
        _rh = extract_hops_from_plan_row(_rv, mh_cols)
        if len(_rh) >= 2:
            _route_lookup[(_rh[0], _rh[-1])] = _rh
    _emit(f"  Route lookup entries: {len(_route_lookup)}")

    _emit("Building DH portfolio…")
    portfolio = build_dh_portfolio(plan_vol, fbf_agg, mh_cols=mh_cols)
    # attach lat/lon for DH
    portfolio = portfolio.merge(
        lat_df.rename(columns={"site_key": "destination_hub_key"}),
        on="destination_hub_key",
        how="left",
    )
    fc_mh_df = fc_mh_df.merge(lat_df, left_on="fc_mh_key", right_on="site_key", how="left")
    if "site_key" in fc_mh_df.columns:
        fc_mh_df = fc_mh_df.drop(columns=["site_key"])
    n_dh = len(portfolio)
    _emit(
        f"  Portfolio: {n_dh} destination hubs  |  Top266 threshold={thr}"
        + (
            f"  |  proximity filter OFF (4 nearest FC_MH each DH)"
            if not prox_filter_on
            else f"  |  proximity ≤ {prox_km} km haversine (may reduce candidates)"
        )
    )

    # Pre-scan all distance pairs needed across every DH → batch-fetch missing ones from OSRM.
    # This avoids sequential one-at-a-time OSRM calls (each with a rate-limit sleep) during the
    # main loop.  Pairs are fetched in parallel; the main loop then finds them already in dist_lookup.
    if use_osrm and _hub_lat_lkp:
        _emit("Pre-scanning needed distance pairs for OSRM batch…")
        _prescan_pairs: set[tuple[str, str]] = set()

        # MH-MH edges: every consecutive pair in every plan route
        for _rh in _route_lookup.values():
            for _i in range(len(_rh) - 1):
                _prescan_pairs.add((_rh[_i], _rh[_i + 1]))

        # MH-DH and speed (P1-DH, P2-P1) pairs per DH × top-4 FC candidates
        for _, _prow in portfolio.iterrows():
            _lat_d = _prow.get("lat")
            _lon_d = _prow.get("lon")
            if pd.isna(_lat_d) or pd.isna(_lon_d):
                continue
            _dh_k = str(_prow["destination_hub_key"])
            _cands_pre: list[tuple[str, float]] = []
            for _, _crow in fc_mh_df.iterrows():
                if pd.isna(_crow.get("lat")) or pd.isna(_crow.get("lon")):
                    continue
                _cands_pre.append((
                    str(_crow["fc_mh_key"]),
                    haversine_km(float(_lat_d), float(_lon_d), float(_crow["lat"]), float(_crow["lon"])),
                ))
            _cands_pre.sort(key=lambda x: x[1])
            if prox_filter_on:
                _within = [c for c in _cands_pre[:4] if c[1] <= prox_km]
                _cands_pre = _within if _within else _cands_pre[:4]
            else:
                _cands_pre = _cands_pre[:4]
            for _ck_pre, _ in _cands_pre:
                _prescan_pairs.add((_ck_pre, _dh_k))   # MH-DH cost
                _pr_pre = pick_pathway_row_for_p1(pathway, _ck_pre, _pway_p1c, _pathway_p1_index)
                if _pr_pre is not None:
                    _p1h = _norm_hub_key(_pr_pre[_pway_p1c])
                    _prescan_pairs.add((_p1h, _dh_k))  # speed P1→DH
                    _p2h = _pathway_mh_key(_pr_pre[_pway_p2c]) if _pway_p2c and _pway_p2c in _pr_pre.index else ""
                    if _p2h:
                        _prescan_pairs.add((_p2h, _p1h))  # speed P2→P1

        _osrm_batch_workers = int(cfg.get("osrm_batch_workers", 4))
        _batch_fetch_osrm_pairs(
            _prescan_pairs, _hub_lat_lkp, dist_lookup, cfg,
            max_workers=_osrm_batch_workers, emit=_emit,
        )

    truck_mh_mh = float(cfg["truck_cft_mh_mh"])
    missing_all: list[tuple[str, str, str]] = []
    assign_rows: list[dict[str, Any]] = []
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

    # Current FC lookup: DH key → list of normalized last_mh hub keys from plan_vol
    _curr_fc_by_dh: dict[str, list[str]] = {}
    if "last_mh" in plan_vol.columns:
        for _k, _g in plan_vol.groupby(plan_vol[dh_col].map(_norm_hub_key)):
            if not _k:
                continue
            _keys = _g["last_mh"].dropna().map(_norm_hub_key).dropna().unique()
            _curr_fc_by_dh[str(_k)] = [k for k in _keys if k]
    # FC position lookup (always available, independent of OSRM flag)
    _fc_pos_lkp: dict[str, tuple[float, float]] = {
        str(r["fc_mh_key"]): (float(r["lat"]), float(r["lon"]))
        for _, r in fc_mh_df.iterrows()
        if pd.notna(r.get("lat")) and pd.notna(r.get("lon"))
    }

    _emit(f"Assigning FC_MH for {n_dh} DHs…")
    for _dh_idx, (_, prow) in enumerate(portfolio.iterrows(), start=1):
        dh_key = str(prow["destination_hub_key"])
        lat_d, lon_d = prow.get("lat"), prow.get("lon")
        if pd.isna(lat_d) or pd.isna(lon_d):
            assign_rows.append(_make_error_assign_row(prow, "missing_lat_lon_for_dh"))
            _tick("error")
            continue

        # 4 nearest FC_MH with coordinates
        cand_list: list[tuple[str, float, float, float]] = []
        for _, crow in fc_mh_df.iterrows():
            if pd.isna(crow.get("lat")) or pd.isna(crow.get("lon")):
                continue
            dkm = haversine_km(float(lat_d), float(lon_d), float(crow["lat"]), float(crow["lon"]))
            cand_list.append((str(crow["fc_mh_key"]), float(crow["lat"]), float(crow["lon"]), dkm))
        cand_list.sort(key=lambda x: x[3])
        cand_list = cand_list[:4]
        # Proximity filter: if any of the top-4 is within prox_km, keep only those (often 1 FC near a DH).
        if prox_filter_on:
            within_prox = [c for c in cand_list if c[3] <= prox_km]
            if within_prox:
                cand_list = within_prox

        # Current FC enforcement: if none of the DH's current FC(s) are in candidates, force them
        # in unconditionally and keep the 3 nearest from normal rules (total ≤ 3 + n_current_fcs).
        _curr_fc_keys = _curr_fc_by_dh.get(dh_key, [])
        _cand_key_set = {c[0] for c in cand_list}
        if _curr_fc_keys and not any(k in _cand_key_set for k in _curr_fc_keys):
            _forced: list[tuple[str, float, float, float]] = []
            for _cfk in _curr_fc_keys:
                _pos = _fc_pos_lkp.get(_cfk)
                _clat = _pos[0] if _pos else float("nan")
                _clon = _pos[1] if _pos else float("nan")
                _cdkm = haversine_km(float(lat_d), float(lon_d), _clat, _clon) if _pos else float("inf")
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

        best_name = ""
        best_basis = "cost" if not use_speed else "speed"
        best_d1 = 0.0
        best_cost = float("inf")
        cand_names = [c[0] for c in cand_list] + ["", "", "", ""]
        cand_names = cand_names[:4]

        _dh_fbf_cft = float(prow.get("fbf_cft", 0) or 0)

        evals: list[dict[str, Any]] = []
        for ck, _lat, _lon, _dkm in cand_list:
            pr = pick_pathway_row_for_p1(pathway, ck, _pway_p1c, _pathway_p1_index)
            if pr is None:
                d1, ok_sp, p1_contrib, p2_contrib = 0.0, True, 0.0, 0.0
                _p2_hub = ""
                _p2_inv = 0.0
            else:
                p1c, p2c, p1pct, p2pct = _pway_p1c, _pway_p2c, _pway_p1pct, _pway_p2pct
                # Row is chosen where pathway P1 central hub equals this FC_MH candidate (``ck``).
                p1_hub = _norm_hub_key(pr[p1c])
                p2_hub = _pathway_mh_key(pr[p2c]) if p2c in pr.index else ""
                p1_inv = parse_pct(pr[p1pct]) if p1_hub == ck else 0.0
                p2_raw = pr[p2c] if p2c in pr.index else None
                p2_inv = (
                    parse_pct(pr[p2pct])
                    if p2_hub and p2pct in pr.index and _is_real_central_hub(p2_raw)
                    else 0.0
                )
                _p2_hub = p2_hub
                _p2_inv = p2_inv
                d1, ok_sp, p1_contrib, p2_contrib = compute_speed_metrics(
                    dh_key,
                    p1_hub,
                    p2_hub,
                    p1_inv,
                    p2_inv,
                    dist_lookup,
                    load_fn,
                    cfg,
                    missing=missing_all,
                    hub_lat_lkp=_hub_lat_lkp,
                    osrm_log=_osrm_log,
                )
            slice_dh = plan_vol_by_dh.get(dh_key, _plan_vol_nonfbf.iloc[:0])
            mh_mh, ok_m = compute_mh_mh_cost_for_candidate(
                plan_vol,
                dh_key,
                ck,
                cost_lookup,
                mh_cols,
                dh_col,
                missing_all,
                truck_mh_mh,
                plan_slice=slice_dh,
                route_lookup=_route_lookup,
                p2_hub_key=_p2_hub,
                p2_inv=_p2_inv,
                fbf_cft=_dh_fbf_cft,
                dist_lookup=dist_lookup,
                hub_lat_lkp=_hub_lat_lkp,
                osrm_log=_osrm_log,
                osrm_cfg=cfg,
                mh_mh_cost_per_km=mh_mh_cost_per_km,
                mh_mh_est_log=_mh_mh_est_log,
            )
            mh_dh, ok_d = compute_mh_dh_cost(dh_key, ck, total_cft, dist_lookup, cfg, missing_all,
                                              hub_lat_lkp=_hub_lat_lkp, osrm_log=_osrm_log)
            tot_cost = mh_mh + mh_dh
            # Buffered cost used only for comparison/selection; real cost stored in output
            comparison_cost = mh_mh + mh_dh_cost_buffer * mh_dh
            ok_cand = ok_sp and ok_m and ok_d
            evals.append(
                {
                    "fc_mh": ck,
                    "final_d1_pct": d1 * 100.0,
                    "p1_d1_pct": p1_contrib * 100.0,
                    "p2_d1_pct": p2_contrib * 100.0,
                    "total_cost_rs": tot_cost,
                    "comparison_cost": comparison_cost,
                    "ok": ok_cand,
                    "mh_mh_rs": mh_mh,
                    "mh_dh_rs": mh_dh,
                }
            )

        # Per-candidate cost lookups (keyed by fc_mh name) for the output columns
        cand_cost_map:  dict[str, float | None] = {e["fc_mh"]: e["total_cost_rs"]    for e in evals}
        cand_mhmh_map:  dict[str, float | None] = {e["fc_mh"]: e.get("mh_mh_rs")    for e in evals}
        cand_mhdh_map:  dict[str, float | None] = {e["fc_mh"]: e.get("mh_dh_rs")    for e in evals}

        feasible = [e for e in evals if e["ok"]]
        if not feasible:
            assign_rows.append(_make_error_assign_row(
                prow, "no_feasible_candidate_missing_distance_or_cost",
                cand_names=cand_names, cand_cost_map=cand_cost_map,
                cand_mhmh_map=cand_mhmh_map, cand_mhdh_map=cand_mhdh_map,
            ))
            _tick("error")
            continue

        if use_speed:
            pick = max(feasible, key=lambda e: e["final_d1_pct"])
        else:
            pick = min(feasible, key=lambda e: e["comparison_cost"])
        best_name = pick["fc_mh"]
        best_d1 = float(pick["final_d1_pct"]) / 100.0
        best_cost = float(pick["total_cost_rs"])
        best_basis = "speed" if use_speed else "cost"
        d1_ship = best_d1 * top266

        # Cost delta: current FC cost vs suggested FC cost (always >= 0)
        _curr_fc_key_set = set(_curr_fc_keys)
        _curr_fc_evals = [e for e in evals if e["fc_mh"] in _curr_fc_key_set]
        _curr_fc_feasible = [e for e in _curr_fc_evals if e.get("ok")]
        _curr_fc_pick = (
            min(_curr_fc_feasible, key=lambda e: e["total_cost_rs"]) if _curr_fc_feasible
            else (min(_curr_fc_evals, key=lambda e: e["total_cost_rs"]) if _curr_fc_evals else None)
        )
        _curr_fc_out_name = _curr_fc_pick["fc_mh"] if _curr_fc_pick is not None else None
        _curr_fc_out_cost = _curr_fc_pick["total_cost_rs"] if _curr_fc_pick is not None else None
        _cost_delta = max(0.0, _curr_fc_out_cost - best_cost) if _curr_fc_out_cost is not None else None

        _fbf_ok = float(prow.get("fbf_shipments") or 0)
        _nfbf_ok = float(prow.get("nfbf_shipments") or 0)
        _al_ok = float(prow.get("alphalite_shipments") or 0)
        assign_rows.append(
            {
                "destination_hub_key": dh_key,
                "assigned_fc_mh": best_name,
                "assignment_basis": best_basis,
                # Speed metrics
                "final_d1_pct": pick["final_d1_pct"],
                "p1_d1_pct": pick["p1_d1_pct"],
                "p2_d1_pct": pick["p2_d1_pct"],
                "d1_shipments_equiv": d1_ship,
                # Cost breakdown
                "mh_mh_cost_rs": pick["mh_mh_rs"],
                "mh_dh_cost_rs": pick["mh_dh_rs"],
                "total_cost_rs": best_cost,
                # Current FC comparison
                "current_fc_mh": _curr_fc_out_name,
                "current_fc_cost_rs": _curr_fc_out_cost,
                "cost_delta_rs": _cost_delta,
                # Volumes
                "top266_shipments": top266,
                "total_shipments": _fbf_ok + _nfbf_ok + _al_ok,
                "total_cft": total_cft,
                "fbf_shipments": _fbf_ok,
                "nfbf_shipments": _nfbf_ok,
                "alphalite_shipments": _al_ok,
                # All 4 candidates — total cost + MH-MH and MH-DH split
                "candidate_1": cand_names[0],
                "candidate_1_cost_rs":      cand_cost_map.get(cand_names[0]),
                "candidate_1_mhmh_cost_rs": cand_mhmh_map.get(cand_names[0]),
                "candidate_1_mhdh_cost_rs": cand_mhdh_map.get(cand_names[0]),
                "candidate_2": cand_names[1],
                "candidate_2_cost_rs":      cand_cost_map.get(cand_names[1]),
                "candidate_2_mhmh_cost_rs": cand_mhmh_map.get(cand_names[1]),
                "candidate_2_mhdh_cost_rs": cand_mhdh_map.get(cand_names[1]),
                "candidate_3": cand_names[2],
                "candidate_3_cost_rs":      cand_cost_map.get(cand_names[2]),
                "candidate_3_mhmh_cost_rs": cand_mhmh_map.get(cand_names[2]),
                "candidate_3_mhdh_cost_rs": cand_mhdh_map.get(cand_names[2]),
                "candidate_4": cand_names[3],
                "candidate_4_cost_rs":      cand_cost_map.get(cand_names[3]),
                "candidate_4_mhmh_cost_rs": cand_mhmh_map.get(cand_names[3]),
                "candidate_4_mhdh_cost_rs": cand_mhdh_map.get(cand_names[3]),
                "notes": "",
            }
        )
        _tick(best_basis)

    _emit(f"All {n_dh} DHs processed. Writing outputs…")
    assign_df = pd.DataFrame(assign_rows)
    assign_path = out_dir / "dh_fc_mh_assignment.csv"
    # Write CSV with d1 columns displayed as "XX.XXXX%" (numeric kept in assign_df for summary below)
    assign_display = assign_df.copy()
    for _pct_col in ("final_d1_pct", "p1_d1_pct", "p2_d1_pct"):
        if _pct_col in assign_display.columns:
            assign_display[_pct_col] = assign_display[_pct_col].apply(
                lambda x: f"{x:.4f}%" if pd.notna(x) else ""
            )
    assign_display.to_csv(assign_path, index=False)

    # SMH × MH_Last cost-per-shipment (lane-level from plan volume)
    smh_rows: list[dict[str, Any]] = []
    use_rc_col = "source_type" in plan_vol.columns
    for _, row in plan_vol.iterrows():
        hops = extract_hops_from_plan_row(row, mh_cols)
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
        for ei, (u, v) in enumerate(zip(hops[:-1], hops[1:])):
            if zero_first and ei == 0:
                continue
            trip = cost_lookup.get((u, v)) or cost_lookup.get((v, u))
            if trip is None:
                continue
            lane_cost += (cft / max(truck_mh_mh, 1e-9)) * float(trip)
        smh_rows.append({"smh": smh, "mh_last": mh_last,
                         "plan_median_cft": cft, "median_demand_shipments": ship,
                         "mh_mh_cost_rs": lane_cost})

    if smh_rows:
        smh_df = pd.DataFrame(smh_rows)
        smh_agg = (
            smh_df.groupby(["smh", "mh_last"])
            .agg(
                total_cft=("plan_median_cft", "sum"),
                total_shipments=("median_demand_shipments", "sum"),
                total_mh_mh_cost_rs=("mh_mh_cost_rs", "sum"),
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
        ])
    smh_cps_path = out_dir / "smh_mhlast_cost_per_shipment.csv"
    smh_agg.to_csv(smh_cps_path, index=False)

    # de-duplicate missing pairs for export
    miss_set = sorted(set(missing_all))
    miss_rows = [{"from_hub_key": f, "to_hub_key": t, "reason": r,
                  "assumed_distance_km": None, "assumed_cost_per_trip_rs": None}
                 for f, t, r in miss_set]
    # OSRM-resolved distance pairs
    osrm_set = sorted(set(_osrm_log), key=lambda x: (x[0], x[1]))
    for f, t, d in osrm_set:
        miss_rows.append({"from_hub_key": f, "to_hub_key": t,
                          "reason": "osrm_fallback", "assumed_distance_km": d,
                          "assumed_cost_per_trip_rs": None})
    # MH-MH cost estimated via distance × rate
    est_set = sorted({(f, t, d, c) for f, t, d, c in _mh_mh_est_log}, key=lambda x: (x[0], x[1]))
    for f, t, d, c in est_set:
        miss_rows.append({"from_hub_key": f, "to_hub_key": t,
                          "reason": "mh_mh_cost_estimated",
                          "assumed_distance_km": round(d, 3),
                          "assumed_cost_per_trip_rs": round(c, 2)})
    miss_df = pd.DataFrame(miss_rows, columns=[
        "from_hub_key", "to_hub_key", "reason", "assumed_distance_km", "assumed_cost_per_trip_rs"
    ])
    miss_path = out_dir / "agent3_missing_distance_pairs.csv"
    miss_df.to_csv(miss_path, index=False)
    if osrm_set:
        _emit(f"  OSRM resolved {len(osrm_set)} distance pairs (see missing pairs CSV)")
    if est_set:
        _emit(f"  MH-MH cost estimated (distance×{mh_mh_cost_per_km}) for {len(est_set)} pairs (see missing pairs CSV)")

    ok_assign = assign_df[assign_df["assigned_fc_mh"].astype(str).str.len() > 0]
    w_num = float((ok_assign["final_d1_pct"] / 100.0 * ok_assign["top266_shipments"]).sum())
    w_den = float(ok_assign["top266_shipments"].sum()) or 1.0
    weighted_d1_pct = 100.0 * w_num / w_den
    total_cost = float(ok_assign["total_cost_rs"].sum())
    n_speed = int((assign_df["assignment_basis"] == "speed").sum())
    n_cost = int((assign_df["assignment_basis"] == "cost").sum())
    n_err = int((assign_df["assignment_basis"] == "error").sum())

    summary = pd.DataFrame(
        [
            {
                "weighted_avg_d1_pct": weighted_d1_pct,
                "total_network_cost_rs": total_cost,
                "n_speed_assigned": n_speed,
                "n_cost_assigned": n_cost,
                "n_error_rows": n_err,
                "top266_threshold_used": thr,
                "n_dh_total": len(assign_df),
            }
        ]
    )
    summary_path = out_dir / "agent3_summary.csv"
    summary.to_csv(summary_path, index=False)

    rep_path = out_dir / "validation_report_agent3.txt"
    body = []
    body.append("=== Agent 3 run ===")
    body.append(json.dumps({"paths": str(plan_volume_path), "threshold": thr}, indent=2))
    body.append("")
    body.append("=== Missing distance / cost edges (deduplicated) ===")
    body.append(f"Count: {len(miss_set)}")
    body.append(miss_df.to_string(index=False) if len(miss_df) else "(none)")
    body.append("")
    body.append("=== Summary ===")
    body.append(summary.to_string(index=False))
    rep_path.write_text("\n".join(body), encoding="utf-8")

    _emit("Generating network map HTML…")
    map_path = out_dir / "hub_network_map.html"
    try:
        _build_network_map_html(assign_df, lat_df, map_path)
        _emit(f"  wrote {map_path.name}")
    except Exception as _map_err:  # noqa: BLE001
        _emit(f"  WARNING: network map not written: {_map_err}")
        map_path = None

    _emit(
        f"Done. speed={n_speed}  cost={n_cost}  errors={n_err}  "
        f"missing_pairs={len(miss_set)}  weighted_d1={weighted_d1_pct:.1f}%"
    )

    return {
        "assignment_csv": str(assign_path),
        "smh_mhlast_cps_csv": str(smh_cps_path),
        "summary_csv": str(summary_path),
        "missing_pairs_csv": str(miss_path),
        "validation_report": str(rep_path),
        "network_map_html": str(map_path) if map_path else None,
        "n_missing_pairs": len(miss_set),
        "n_dh": len(assign_df),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public on-demand MH-MH cost resolver (called by Agent 3 Phase 2 for gaps)
# ─────────────────────────────────────────────────────────────────────────────

def compute_mhmh_for_pairs(
    dh_candidate_pairs: list[tuple[str, str]],
    *,
    plan_vol_path: Path,
    cost_matrix_path: Path,
    config_path: Optional[Path] = None,
) -> dict[tuple[str, str], Optional[float]]:
    """
    On-demand MH-MH cost computation for (dh_key, candidate_mh) pairs.

    Uses Agent 3's exact same logic as the main pipeline — multi-hop routing,
    stream-level volumes, per-edge MH1→MH2 rate card.

    Called by Phase 2 for DHs that are in the pool but do not have a matching
    ``candidate_X_mhmh_cost_rs`` column in the Agent 3 output (route-mates
    added by H2H expansion, or DHs whose target MH wasn't in the top-4).

    Parameters
    ----------
    dh_candidate_pairs  : list of (dh_key, candidate_mh) pairs to resolve
    plan_vol_path       : path to plan_volume.csv (Agent 1 output)
    cost_matrix_path    : path to MH1–MH2 rate card (Agent 2 output, C/T col)
    config_path         : optional agent3_config.json for truck_cft_mh_mh

    Returns
    -------
    dict keyed by (dh_key, candidate_mh) → mhmh_cost_rs or None
    """
    import re as _re

    cfg: dict[str, Any] = {}
    if config_path and Path(config_path).is_file():
        try:
            import json as _json
            cfg = _json.loads(Path(config_path).read_text(encoding="utf-8"))
        except Exception:
            pass

    truck_cft = float(cfg.get("truck_cft_mh_mh", 2400.0))

    # Load plan volume
    plan_vol = pd.read_csv(plan_vol_path, dtype=str)
    plan_vol.columns = plan_vol.columns.str.strip()
    for col in plan_vol.columns:
        if _re.match(r"^MH\d+$", col, _re.I) or col in (
            "plan_median_cft_volume", "median_demand_shipments",
        ):
            plan_vol[col] = pd.to_numeric(plan_vol[col], errors="coerce")

    mh_cols = [c for c in plan_vol.columns if _re.match(r"^MH\d+$", c, _re.I)]
    dh_col  = "LMHub" if "LMHub" in plan_vol.columns else "lmhub"

    # Pre-key plan_vol by normalised DH key for fast slicing
    if dh_col in plan_vol.columns:
        plan_vol["_dh_key_norm"] = plan_vol[dh_col].map(_norm_hub_key)
    else:
        return {}

    # Load MH1→MH2 cost matrix
    cost_df = pd.read_csv(cost_matrix_path, dtype=str)
    cost_df.columns = cost_df.columns.str.strip()
    cost_lookup = build_cost_lookup(cost_df)

    missing_pairs: list[tuple[str, str, str]] = []
    results: dict[tuple[str, str], Optional[float]] = {}

    for dh_key, candidate_mh in dh_candidate_pairs:
        dh_norm = _norm_hub_key(dh_key)
        cand_norm = _norm_hub_key(candidate_mh)
        plan_slice = plan_vol[plan_vol["_dh_key_norm"] == dh_norm]

        if plan_slice.empty:
            results[(dh_key, candidate_mh)] = None
            continue

        cost, ok = compute_mh_mh_cost_for_candidate(
            plan_vol=plan_vol,
            dh_key=dh_norm,
            candidate_key=cand_norm,
            cost_lookup=cost_lookup,
            mh_cols=mh_cols,
            dh_col=dh_col,
            missing_pairs=missing_pairs,
            truck_cft_mh_mh=truck_cft,
            plan_slice=plan_slice,
        )
        results[(dh_key, candidate_mh)] = round(cost, 2) if ok else None

    return results
