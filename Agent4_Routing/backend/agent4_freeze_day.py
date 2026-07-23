"""
Agent 4 — freeze-day engine (day-level demand simulation).

New, additive module. Imports shared primitives from agent4.py instead of
duplicating them — vehicle sizing, distance/transit helpers, rate-card
loading, position/frequency derivation, and bearing clustering are all
reused as-is. Nothing in agent4.py is modified by this module; the
Phase-2-frozen functions there (run_agent4_for_mh, MHConfig, Agent4MHResult,
etc.) are untouched.

This module answers a different question than the legacy snapshot engine:
instead of costing routes against one aggregate demand number, it tests
freezing a route plan against every day (+ 7 synthetic peak/median days) in
the chosen window, simulates the other 30 real days of demand against each
candidate frozen plan (ad-hoc/spillover cost for whatever doesn't fit), and
picks whichever frozen day minimizes committed + adhoc cost.
"""
from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from dataclasses import replace as _dc_replace
from typing import Any, Optional

import numpy as np
import pandas as pd
from pulp import LpBinary, LpMinimize, LpProblem, LpVariable, lpSum, PULP_CBC_CMD

import agent4 as a4

# ---------------------------------------------------------------------------
# Location file assembly: normalization + synthetic extreme days
# ---------------------------------------------------------------------------


def _normalize_dh_row(values: list[float], zero_threshold: int) -> list[float]:
    """Fill zero-demand days for one DH's day series. Total volume is preserved.

    >= zero_threshold zero days -> circular redistribution (Case A).
    < zero_threshold zero days  -> local interpolation from nearest non-zero
    neighbours on each side (Case B).
    """
    v = list(values)
    n = len(v)
    zero_count = sum(1 for x in v if x == 0)
    if zero_count == 0:
        return v

    if zero_count >= zero_threshold:
        for _ in range(n * 2):
            if all(x > 0 for x in v):
                break
            i = 0
            while i < n:
                if v[i] == 0:
                    j = i
                    while j < n and v[j] == 0:
                        j += 1
                    group_indices = list(range(i, j))
                    found = None
                    for k_off in range(1, n + 1):
                        candidate = (j + k_off - 1) % n
                        if v[candidate] > 0:
                            found = candidate
                            break
                    if found is None:
                        break
                    divisor = len(group_indices) + 1
                    new_val = v[found] / divisor
                    for gi in group_indices:
                        v[gi] = new_val
                    v[found] = new_val
                    i = j
                else:
                    i += 1
        return v

    result = list(v)
    for i in range(n):
        if v[i] != 0:
            continue
        before: list[float] = []
        after: list[float] = []
        for k in range(1, n):
            bi = (i - k) % n
            if v[bi] != 0:
                before.append(v[bi])
                if len(before) == 2:
                    break
        for k in range(1, n):
            ai = (i + k) % n
            if v[ai] != 0:
                after.append(v[ai])
                if len(after) == 2:
                    break
        neighbors = before + after
        result[i] = sum(neighbors) / len(neighbors) if neighbors else 0.0
    return result


def _normalize_daywise_demand(df: pd.DataFrame, day_cols: list[str], zero_threshold: int) -> pd.DataFrame:
    df = df.copy()
    cft_cols = [f"{c}_cft" for c in day_cols if f"{c}_cft" in df.columns]
    for c in day_cols + cft_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    for idx, row in df.iterrows():
        values = [float(row[c]) for c in day_cols]
        norm = _normalize_dh_row(values, zero_threshold)
        for j, c in enumerate(day_cols):
            df.at[idx, c] = round(norm[j], 2)
        if cft_cols:
            cft_values = [float(row[f"{c}_cft"]) for c in day_cols]
            cft_norm = _normalize_dh_row(cft_values, zero_threshold)
            for j, c in enumerate(day_cols):
                df.at[idx, f"{c}_cft"] = round(cft_norm[j], 2)
    return df


def _add_synthetic_days(df: pd.DataFrame, day_cols: list[str]) -> pd.DataFrame:
    """Append 7 synthetic extreme days after the base window: peak, median, and
    5 linearly-interpolated steps between them (matches the original 30-day ->
    D31..D37 scheme, generalised to any base window length n)."""
    df = df.copy()
    # Number synthetic columns from the actual max day number present, NOT from
    # len(day_cols) -- day columns are named after the source day_N numbers
    # (e.g. D32..D61 for a June window starting at day_32), which are almost
    # never contiguous from 1. Using len() here previously caused synthetic
    # columns (D{len+1}..D{len+7}) to collide with and silently overwrite real
    # day columns whenever the window didn't start at day_1.
    max_real_day = max(int(c[1:]) for c in day_cols)
    synth_start = max_real_day + 1
    synth_end = max_real_day + 7
    cft_cols = [f"{c}_cft" for c in day_cols if f"{c}_cft" in df.columns]
    has_cft = bool(cft_cols)

    for k in range(synth_start, synth_end + 1):
        df[f"D{k}"] = 0.0
        if has_cft:
            df[f"D{k}_cft"] = 0.0

    for idx, row in df.iterrows():
        vals = [float(row[c]) for c in day_cols]
        peak, median = max(vals), float(np.median(vals))
        span = peak - median
        df.at[idx, f"D{synth_start}"] = round(peak, 2)
        df.at[idx, f"D{synth_end}"] = round(median, 2)
        for k in range(synth_start + 1, synth_end):
            step = (synth_end - k) / 6.0
            df.at[idx, f"D{k}"] = round(median + step * span, 2)

        if has_cft:
            cvals = [float(row[f"{c}_cft"]) for c in day_cols]
            cpeak, cmedian = max(cvals), float(np.median(cvals))
            cspan = cpeak - cmedian
            df.at[idx, f"D{synth_start}_cft"] = round(cpeak, 2)
            df.at[idx, f"D{synth_end}_cft"] = round(cmedian, 2)
            for k in range(synth_start + 1, synth_end):
                step = (synth_end - k) / 6.0
                df.at[idx, f"D{k}_cft"] = round(cmedian + step * cspan, 2)

    return df


def build_freeze_day_location_file(
    agent3_assignment_df: pd.DataFrame,
    dh_feasibility_df: pd.DataFrame,
    h2h_df: pd.DataFrame,
    daywise_df: pd.DataFrame,
    mh_configs: dict[str, "a4.MHConfig"],
    cfg: dict[str, Any],
    phase2_accepted_changes: Optional[dict[str, str]] = None,
    time_window_overrides: Optional[dict[str, dict[str, Any]]] = None,
) -> dict:
    """Assemble the location file for the freeze-day engine.

    Calls agent4.build_location_file() for the base columns (reused, not
    replicated), then normalizes zero-demand days, appends 7 synthetic
    extreme days, and derives allowed_positions / Freq_Allowed per DH using
    each DH's assigned MH's threshold_a/threshold_b (from mh_configs).
    """
    base = a4.build_location_file(
        agent3_assignment_df, dh_feasibility_df,
        phase2_accepted_changes=phase2_accepted_changes,
        time_window_overrides=time_window_overrides,
        h2h_df=h2h_df, daywise_df=daywise_df, cfg=cfg,
    )
    if base["status"] == "failed":
        return base

    issues = list(base["issues"])
    df = base["data"].copy()

    zero_threshold = int(cfg.get("zero_day_threshold", 5))
    day_cols = sorted(
        (c for c in df.columns if c.startswith("D") and c[1:].isdigit()),
        key=lambda c: int(c[1:]),
    )
    if not day_cols:
        issues.append({
            "type": "no_daywise_columns",
            "detail": "No D1..Dn columns found in location file — was daywise_df supplied?",
        })
    else:
        df = _normalize_daywise_demand(df, day_cols, zero_threshold)
        df = _add_synthetic_days(df, day_cols)

    top266_col = cfg.get("col_top266_load", "top266_shipments")
    mh_col = cfg.get("col_mh_assignment", "current_fc_mh")
    allowed_positions_list = []
    freq_allowed_list = []
    for _, row in df.iterrows():
        t266 = row.get(top266_col, 0)
        t266 = float(t266) if pd.notna(t266) else 0.0
        mh_cfg = mh_configs.get(str(row[mh_col]))
        if mh_cfg is not None:
            th_a, th_b = mh_cfg.threshold_a, mh_cfg.threshold_b
        else:
            th_a, th_b = cfg["default_threshold_a"], cfg["default_threshold_b"]
        allowed_positions_list.append(a4.derive_allowed_positions(t266, th_a, th_b))
        freq_allowed_list.append(a4.derive_freq_allowed(t266))
    df["allowed_positions"] = allowed_positions_list
    df["Freq_Allowed"] = freq_allowed_list

    status = "partial" if issues else "ok"
    return {"status": status, "data": df, "issues": issues}


# ---------------------------------------------------------------------------
# Freeze-day candidate costing — reuses agent4.run_agent4_for_mh verbatim.
# The legacy engine already does bearing clustering, permutation generation,
# soft allowed_positions + Freq_Allowed constraints, local/zonal costing, and
# ILP set-cover for one demand snapshot. A freeze-day "candidate" IS one demand
# snapshot (one day's CFT), so there is nothing to reimplement here — just
# swap the demand column and call it.
# ---------------------------------------------------------------------------


def run_freeze_day_candidate(
    mh_name: str,
    mh_cfg: "a4.MHConfig",
    dh_df: pd.DataFrame,
    day_col: str,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
) -> "a4.Agent4MHResult":
    """Cost one freeze-day candidate (e.g. day_col='D5' or a synthetic 'D31').

    Builds a per-day demand snapshot (cfg['col_demand'] <- dh_df[f'{day_col}_cft'])
    and calls agent4.run_agent4_for_mh on it unchanged.
    """
    snapshot = dh_df.copy()
    snapshot[cfg["col_demand"]] = snapshot[f"{day_col}_cft"]
    return a4.run_agent4_for_mh(mh_name, mh_cfg, snapshot, dist_dict, latlong, cfg)


# ---------------------------------------------------------------------------
# Spillover simulation — the genuinely new piece. Given a frozen route plan
# (one Agent4MHResult) and a real day's demand, compute how much demand
# doesn't fit the frozen vehicles and cost ad-hoc trucks for the excess.
# ---------------------------------------------------------------------------


def _route_capacity(vehicle_length: float) -> float:
    cap = a4.ML_VEHICLE_CAPACITY.get(vehicle_length)
    if cap is not None:
        return float(cap)
    known = sorted(a4.ML_VEHICLE_CAPACITY.keys())
    return float(a4.ML_VEHICLE_CAPACITY[min(known, key=lambda k: abs(k - vehicle_length))])


def _mh_rate_card(mh_cfg: "a4.MHConfig", dist_km: float, cfg: dict[str, Any]) -> dict[float, float]:
    thresh = cfg["local_zonal_distance_threshold_km"]
    return mh_cfg.local_rate_card if dist_km <= thresh else mh_cfg.zonal_rate_card


def _adhoc_route_cost(
    seq: list[str],
    total_vol: float,
    depot_name: str,
    mh_cfg: "a4.MHConfig",
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    ml_caps: dict[str, float],
) -> Optional[tuple[float, float, float]]:
    """Returns (cost, vehicle_length, dist_km) for one adhoc route, or None if
    infeasible (no distance data, or every DH's ML too small for the demand)."""
    max_ml = min(ml_caps.get(dh, 40.0) for dh in seq)
    v = min(a4.assign_vehicle_length(total_vol), max_ml)
    stops = [depot_name] + list(seq) + [depot_name]
    d_total = 0.0
    for i in range(len(stops) - 1):
        km = a4.get_distance(stops[i], stops[i + 1], dist_dict, latlong)
        if km is None:
            return None
        d_total += km
    rate_card = _mh_rate_card(mh_cfg, d_total, cfg)
    premium = float(cfg.get("adhoc_premium", 1.25))
    floor_monthly = float(cfg.get("adhoc_floor_monthly", 90000))
    floor_daily = (floor_monthly / 30.0) * premium
    cost = max(d_total * rate_card.get(v, 999) * premium, floor_daily)
    return cost, v, d_total


def _check_seq_feasibility(
    seq: list[str],
    depot_name: str,
    start_time: float,
    attr: dict[str, dict[str, Any]],
    mh_cfg: "a4.MHConfig",
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
) -> Optional[dict[str, tuple[float, float]]]:
    """Simulate timing for an adhoc sequence departing depot at start_time.
    Returns {dh: (arrival, departure)} or None if any DH's time window is missed."""
    cur_t = start_time
    prev = depot_name
    timing: dict[str, tuple[float, float]] = {}
    for dh in seq:
        km = a4.get_distance(prev, dh, dist_dict, latlong)
        if km is None:
            return None
        arr = cur_t + a4.get_transit_time(km)
        tw_end = attr.get(dh, {}).get("time_window_end", 1800)
        if arr > tw_end:
            return None
        tw_start = attr.get(dh, {}).get("time_window_start", 720)
        dep = max(arr, tw_start) + mh_cfg.service_time_min
        timing[dh] = (arr, dep)
        cur_t = dep
        prev = dh
    return timing


def optimize_adhoc_routes(
    spill_pool: list[dict[str, Any]],
    depot_name: str,
    mh_cfg: "a4.MHConfig",
    attr: dict[str, dict[str, Any]],
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    """Partition spilled demand into the cheapest set of ad-hoc truck routes
    (each spilled item covered exactly once), subject to a merge time window."""
    if not spill_pool:
        return 0.0, []

    n = len(spill_pool)
    merge_window = float(cfg.get("merge_window_min", 120))
    max_hops = mh_cfg.max_hops
    ml_caps = {item["dh"]: attr.get(item["dh"], {}).get("ML", 40.0) for item in spill_pool}

    candidates: list[dict[str, Any]] = []
    for size in range(1, min(max_hops, n) + 1):
        for combo in itertools.combinations(range(n), size):
            cutoffs = [spill_pool[i]["cutoff"] for i in combo]
            if max(cutoffs) - min(cutoffs) > merge_window:
                continue
            dep_t = max(cutoffs)
            dhs = [spill_pool[i]["dh"] for i in combo]
            vols = [spill_pool[i]["vol"] for i in combo]

            best_cost, best_cand = float("inf"), None
            for perm in itertools.permutations(range(size)):
                seq = [dhs[p] for p in perm]
                timing = _check_seq_feasibility(seq, depot_name, dep_t, attr, mh_cfg, dist_dict, latlong)
                if timing is None:
                    continue
                result = _adhoc_route_cost(seq, sum(vols), depot_name, mh_cfg, dist_dict, latlong, cfg, ml_caps)
                if result is None:
                    continue
                cost, vehicle, dist_km = result
                if cost < best_cost:
                    best_cost = cost
                    best_cand = {
                        "indices": list(combo), "seq": seq,
                        "cost": cost, "vehicle": vehicle, "dist": dist_km,
                        "timing": timing, "merged": size > 1, "mh_dep": dep_t,
                    }
            if best_cand is not None:
                # vols/items keyed by dh, taken directly from spill_pool (order-independent)
                best_cand["vols"] = {spill_pool[i]["dh"]: spill_pool[i]["vol"] for i in combo}
                best_cand["items"] = [spill_pool[i] for i in combo]
                candidates.append(best_cand)

    if not candidates:
        return 0.0, []

    prob = LpProblem("AdhocPartition", LpMinimize)
    xvars = LpVariable.dicts("C", range(len(candidates)), cat=LpBinary)
    prob += lpSum(candidates[k]["cost"] * xvars[k] for k in range(len(candidates)))
    for i in range(n):
        prob += lpSum(xvars[k] for k in range(len(candidates)) if i in candidates[k]["indices"]) == 1
    prob.solve(PULP_CBC_CMD(msg=0))

    selected: list[dict[str, Any]] = []
    total_cost = 0.0
    if prob.status == 1:
        for k in range(len(candidates)):
            if xvars[k].varValue and xvars[k].varValue > 0.5:
                selected.append(candidates[k])
                total_cost += candidates[k]["cost"]
    else:
        # Solo fallback: one truck per spilled item
        for item in spill_pool:
            dh, vol, dep_t = item["dh"], item["vol"], item["cutoff"]
            timing = _check_seq_feasibility([dh], depot_name, dep_t, attr, mh_cfg, dist_dict, latlong)
            result = _adhoc_route_cost([dh], vol, depot_name, mh_cfg, dist_dict, latlong, cfg, ml_caps)
            if timing is None or result is None:
                continue
            cost, vehicle, dist_km = result
            selected.append({
                "seq": [dh], "vols": {dh: vol}, "cost": cost, "vehicle": vehicle,
                "dist": dist_km, "timing": timing, "merged": False, "mh_dep": dep_t,
                "items": [item],
            })
            total_cost += cost

    return total_cost, selected


def _build_cutoff_map(final_assignment_df: pd.DataFrame) -> dict[str, float]:
    """DH -> the shift-adjusted departure time of the route serving it.
    Milkrun routes take priority; FTL/dedicated rows only fill gaps, matching
    the original departure-map rule (MR first, dedicated second, never
    overwrite an MR entry a DH already has)."""
    cutoff_map: dict[str, float] = {}
    fa = final_assignment_df
    for _, r in fa[fa["Route_Type"] == "Milkrun"].iterrows():
        dep = r.get("updated_depot_departure", 0.0)
        for dh in r["hubs"]:
            cutoff_map[dh] = dep
    for _, r in fa[fa["Route_Type"] == "FTL_Dedicated"].iterrows():
        dh = r["hubs"][0]
        if dh not in cutoff_map:
            cutoff_map[dh] = r.get("updated_depot_departure", 0.0)
    return cutoff_map


def _spill_item(
    dh: str, vol: float, attr: dict[str, dict[str, Any]], cutoff_map: dict[str, float],
    spill_type: str = "mr_adhoc",
) -> dict[str, Any]:
    a = attr.get(dh, {})
    return {
        "dh": dh,
        "vol": vol,
        "cutoff": cutoff_map.get(dh, a.get("depot_departure", 0.0)),
        "tw_start": a.get("time_window_start", 720),
        "tw_end": a.get("time_window_end", 1800),
        "type": spill_type,
    }


def compute_spillover_day(
    day_demand: dict[str, float],
    mh_result: "a4.Agent4MHResult",
    mh_name: str,
    mh_cfg: "a4.MHConfig",
    attr: dict[str, dict[str, Any]],
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    """Given a frozen route plan and one real day's demand, compute the demand
    that doesn't fit the frozen vehicles and cost ad-hoc trucks for it."""
    fa = mh_result.final_assignment_df
    if fa is None or fa.empty:
        return 0.0, []

    cutoff_map = _build_cutoff_map(fa)
    spill_pool: list[dict[str, Any]] = []

    # A. Dedicated (FTL) overflow: demand beyond the frozen trucks' capacity
    ftl_counts: dict[str, int] = {}
    for _, r in fa[fa["Route_Type"] == "FTL_Dedicated"].iterrows():
        dh = r["hubs"][0]
        ftl_counts[dh] = ftl_counts.get(dh, 0) + 1

    def _mr_residual(dh: str, ml_override: Optional[float] = None) -> float:
        """Port of colab's mr_residual(dh): a DH's milkrun-relevant demand for
        this day is its raw demand minus whatever its OWN frozen FTL trucks
        already absorb -- a DH can be both FTL-dedicated (partially) AND still
        ride a milkrun route for its residual. Without this, the milkrun
        capacity check compares against the DH's full raw demand instead of
        the (much smaller) residual the milkrun route was actually sized for,
        making it look like it overflows almost every day when it doesn't."""
        n = ftl_counts.get(dh, 0)
        ml = ml_override if ml_override is not None else attr.get(dh, {}).get("ML", 40.0)
        cap = _route_capacity(ml)
        a = max(0.0, day_demand.get(dh, 0.0) - n * cap)
        while a > cap:
            a -= cap
        return a

    for dh, n_frozen in ftl_counts.items():
        ml = attr.get(dh, {}).get("ML", 40.0)
        cap = _route_capacity(ml)
        after = max(0.0, day_demand.get(dh, 0.0) - n_frozen * cap)
        # Only genuine EXTRA full-truck-loads (beyond the frozen FTL count)
        # become ad-hoc dedicated spills here -- a leftover <= cap is NOT
        # added in this section (matches colab: only `extra`, accumulated in
        # cap-sized chunks, is spilled). That leftover is exactly what the
        # DH's own frozen MILKRUN route (if it has one) was sized to carry --
        # section B's _mr_residual re-derives and checks it there. Adding it
        # here too would double-count the same demand as needing two
        # different trucks.
        while after > cap:
            spill_pool.append(_spill_item(dh, cap, attr, cutoff_map, "dedicated_adhoc"))
            after -= cap

    # B. Milkrun overflow: process stops back-to-front. Once the vehicle fills,
    # remaining (earlier-loaded) capacity is gone, so every earlier stop spills
    # in full too — matches the original cascading-spillover rule. Uses
    # _mr_residual (not raw day_demand) since a DH may also have its own
    # frozen FTL trucks absorbing part of its demand already.
    for _, r in fa[fa["Route_Type"] == "Milkrun"].iterrows():
        hubs = list(r["hubs"])
        cap = _route_capacity(r["assigned_vehicle_length"])
        rem = cap
        spilling = False
        for dh in reversed(hubs):
            demand = _mr_residual(dh)
            if not spilling and demand <= rem:
                rem -= demand
                continue
            spilling = True
            spill_vol = demand if rem <= 0 else demand - rem
            rem = 0
            if spill_vol > 0:
                spill_pool.append(_spill_item(dh, spill_vol, attr, cutoff_map, "mr_adhoc"))

    if not spill_pool:
        return 0.0, []
    spill_pool.sort(key=lambda x: x["cutoff"])
    return optimize_adhoc_routes(spill_pool, mh_name, mh_cfg, attr, dist_dict, latlong, cfg)


def _freq2_dhs_from_final_assignment(final_assignment_df: pd.DataFrame) -> set[str]:
    """DHs on any Freq==2 route in this plan -- works uniformly for both the
    ILP-optimized freeze-day plan and the baseline's current-routes plan,
    since both carry a Freq column (ILP's own choice, or Current_Freq from H2H)."""
    freq2: set[str] = set()
    if final_assignment_df is None or final_assignment_df.empty:
        return freq2
    for _, r in final_assignment_df.iterrows():
        try:
            is_freq2 = int(r.get("Freq", 1)) == 2
        except (TypeError, ValueError):
            is_freq2 = False
        if is_freq2:
            freq2.update(r["hubs"])
    return freq2


def _build_freq_reverted_demands(
    day_cft_series: dict[int, dict[str, float]],
    freq2_dhs: set[str],
) -> dict[int, dict[str, float]]:
    """Port of colab's _build_freq_reverted_demands (N=2 pair-merging only --
    Freq is never anything but 1 or 2 by ILP design). A Freq==2 route runs
    every OTHER day carrying 2 days' combined demand: for DHs on such a route,
    merge each day-pair into the second day (day i -> 0, day i+1 += day i),
    for i = 0, 2, 4, .... Freq-1 DHs are left untouched."""
    n_days = len(day_cft_series)
    day_indices = sorted(day_cft_series.keys())

    all_dhs: set[str] = set()
    for d in day_indices:
        all_dhs.update(day_cft_series[d].keys())

    series: dict[str, list[float]] = {}
    for dh in all_dhs:
        raw = [day_cft_series[d].get(dh, 0.0) for d in day_indices]
        if dh in freq2_dhs:
            arr = list(raw)
            for i in range(0, n_days - 1, 2):
                arr[i + 1] += arr[i]
                arr[i] = 0.0
            series[dh] = arr
        else:
            series[dh] = raw

    result: dict[int, dict[str, float]] = {}
    for pos, d in enumerate(day_indices):
        result[d] = {dh: series[dh][pos] for dh in all_dhs}
    return result


def run_spillover_simulation(
    day_cft_series: dict[int, dict[str, float]],
    mh_result: "a4.Agent4MHResult",
    mh_name: str,
    mh_cfg: "a4.MHConfig",
    attr: dict[str, dict[str, Any]],
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
) -> tuple[float, list[dict[str, Any]], dict[int, int]]:
    """Runs compute_spillover_day across every real day. Returns
    (total_adhoc_cost, per-day detail, spill_days_per_milkrun_route_index).

    Freq==2 DHs (per this plan's own Freq assignment) have their demand
    series day-reverted first -- see _build_freq_reverted_demands."""
    fa = mh_result.final_assignment_df
    mr_rows = fa[fa["Route_Type"] == "Milkrun"].reset_index(drop=True) if fa is not None and not fa.empty else pd.DataFrame()
    dh_to_mr_idx: dict[str, int] = {}
    for idx, r in mr_rows.iterrows():
        for dh in r["hubs"]:
            dh_to_mr_idx[dh] = idx

    freq2_dhs = _freq2_dhs_from_final_assignment(fa)
    reverted_series = _build_freq_reverted_demands(day_cft_series, freq2_dhs) if freq2_dhs else day_cft_series

    total_adhoc_cost = 0.0
    detail: list[dict[str, Any]] = []
    spill_days: dict[int, int] = {i: 0 for i in range(len(mr_rows))}

    for day_idx, demand_map in reverted_series.items():
        cost, routes = compute_spillover_day(demand_map, mh_result, mh_name, mh_cfg, attr, dist_dict, latlong, cfg)
        total_adhoc_cost += cost
        if routes:
            detail.append({"day_idx": day_idx, "routes": routes, "adhoc_cost": cost, "n_adhoc_routes": len(routes)})
            spilled_today = {dh_to_mr_idx[dh] for route in routes for dh in route["seq"] if dh in dh_to_mr_idx}
            for idx in spilled_today:
                spill_days[idx] += 1

    return total_adhoc_cost, detail, spill_days


# ---------------------------------------------------------------------------
# Truck-upgrade loop: routes that spill too often get their vehicle bumped to
# the next size (within the route's DHs' ML ceiling) if that lowers total cost.
# ---------------------------------------------------------------------------


def _route_max_ml(hubs: list[str], attr: dict[str, dict[str, Any]]) -> float:
    return min(attr.get(dh, {}).get("ML", 40.0) for dh in hubs)


def _next_vehicle(current: float, max_ml: float) -> Optional[float]:
    for v in sorted(a4.ML_VEHICLE_CAPACITY.keys()):
        if v > current and v <= max_ml:
            return v
    return None


def _route_cost_at_vehicle(dist_km: float, vehicle: float, freq: int, mh_cfg: "a4.MHConfig", cfg: dict[str, Any]) -> float:
    rate_card = _mh_rate_card(mh_cfg, dist_km, cfg)
    daily = dist_km * rate_card.get(vehicle, 999)
    if freq == 1:
        return max(daily * 30, 90000)
    return max(daily * 15, 90000) * 1.1


def _replace_final_assignment(mh_result: "a4.Agent4MHResult", new_fa: pd.DataFrame) -> "a4.Agent4MHResult":
    return _dc_replace(
        mh_result,
        final_assignment_df=new_fa,
        total_monthly_cost=float(new_fa["monthly_cost"].sum()),
    )


def run_truck_upgrade_loop(
    mh_result: "a4.Agent4MHResult",
    day_cft_series: dict[int, dict[str, float]],
    mh_name: str,
    mh_cfg: "a4.MHConfig",
    attr: dict[str, dict[str, Any]],
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    on_progress: Optional[Any] = None,
) -> tuple["a4.Agent4MHResult", float, list[dict[str, Any]], float, float, dict[int, int]]:
    """Returns (result_with_upgraded_routes, adhoc_cost, spillover_detail,
    committed_cost, total_cost, spill_days_per_milkrun_route_index) for the
    best vehicle sizing found."""
    def _emit(msg: str) -> None:
        if callable(on_progress):
            on_progress(msg)

    spill_threshold_pct = float(cfg.get("spill_threshold_pct", 0.20))

    best_result = mh_result
    best_adhoc_cost, best_spill_detail, best_spill_days = run_spillover_simulation(
        day_cft_series, best_result, mh_name, mh_cfg, attr, dist_dict, latlong, cfg)
    best_committed = float(best_result.final_assignment_df["monthly_cost"].sum())
    best_total = best_committed + best_adhoc_cost

    while True:
        fa = best_result.final_assignment_df
        mr_positions = fa.index[fa["Route_Type"] == "Milkrun"].tolist()
        mr_rows = fa.loc[mr_positions].reset_index(drop=True)

        breaching: list[tuple[int, float]] = []
        for mr_idx, row in mr_rows.iterrows():
            freq = row["Freq"]
            regular_trips = 30 if freq == 1 else 15
            threshold = spill_threshold_pct * regular_trips
            if best_spill_days.get(mr_idx, 0) > threshold:
                max_ml = _route_max_ml(row["hubs"], attr)
                nxt = _next_vehicle(row["assigned_vehicle_length"], max_ml)
                if nxt is not None:
                    breaching.append((mr_idx, nxt))

        if not breaching:
            break

        _emit(f"    [Upgrade loop] {len(breaching)} route(s) breaching spill threshold, attempting upgrade")

        trial_fa = fa.copy()
        for mr_idx, nxt in breaching:
            pos = mr_positions[mr_idx]
            dist_km = trial_fa.at[pos, "dist"]
            freq = trial_fa.at[pos, "Freq"]
            prev_v = trial_fa.at[pos, "assigned_vehicle_length"]
            trial_fa.at[pos, "assigned_vehicle_length"] = nxt
            trial_fa.at[pos, "monthly_cost"] = round(_route_cost_at_vehicle(dist_km, nxt, freq, mh_cfg, cfg), 2)
            _emit(f"      Route {pos + 1}: {prev_v}ft -> {nxt}ft")

        trial_result = _replace_final_assignment(best_result, trial_fa)
        trial_adhoc_cost, trial_spill_detail, trial_spill_days = run_spillover_simulation(
            day_cft_series, trial_result, mh_name, mh_cfg, attr, dist_dict, latlong, cfg)
        trial_committed = float(trial_fa["monthly_cost"].sum())
        trial_total = trial_committed + trial_adhoc_cost
        _emit(f"      Trial total: Rs {trial_total:,.0f}  prev: Rs {best_total:,.0f}  "
              f"delta: {'+' if trial_total >= best_total else ''}Rs {trial_total - best_total:,.0f}")

        if trial_total < best_total:
            best_result = trial_result
            best_adhoc_cost, best_spill_detail, best_spill_days = trial_adhoc_cost, trial_spill_detail, trial_spill_days
            best_committed, best_total = trial_committed, trial_total
            _emit("      Upgrade accepted")
        else:
            _emit("      Upgrade rejected -- reverting")
            break

    return best_result, best_adhoc_cost, best_spill_detail, best_committed, best_total, best_spill_days


# ---------------------------------------------------------------------------
# Baseline engine — costs the CURRENT (H2H) route network for comparison
# against the freeze-day optimum. Reuses compute_spillover_day/
# run_spillover_simulation by packaging the baseline's fixed routes into an
# Agent4MHResult-shaped final_assignment_df.
# ---------------------------------------------------------------------------


def parse_current_routes(dh_rows: pd.DataFrame, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Groups DHs by Current_MR. Missing/blank/"Direct" -> its own singleton group."""
    loc_col = cfg["col_location_name"]
    groups: dict[str, list[str]] = defaultdict(list)
    freqs: dict[str, list[int]] = defaultdict(list)
    direct_counter = 0

    for _, row in dh_rows.iterrows():
        dh = str(row[loc_col])
        mr_raw = row.get("Current_MR")
        is_direct = pd.isna(mr_raw) or str(mr_raw).strip().lower() in ("", "nan", "none", "direct")

        freq_raw = row.get("Current_Freq", 1)
        try:
            freq_raw = int(float(freq_raw)) if pd.notna(freq_raw) else 1
        except (TypeError, ValueError):
            freq_raw = 1
        freq_raw = max(1, min(2, freq_raw))

        if is_direct:
            key = f"__DIRECT_{direct_counter}__"
            direct_counter += 1
        else:
            key = str(mr_raw).strip()
        groups[key].append(dh)
        freqs[key].append(freq_raw)

    route_groups = []
    for key, dhs in groups.items():
        freq_list = freqs[key]
        freq = Counter(freq_list).most_common(1)[0][0] if freq_list else 1
        route_groups.append({"group_id": key, "dhs": dhs, "freq": freq, "is_direct": key.startswith("__DIRECT_")})
    return route_groups


def _best_route_distance(
    depot_name: str, dhs: list[str],
    dist_dict: dict[tuple[str, str], float], latlong: dict[str, tuple[float, float]],
) -> tuple[float, list[str]]:
    if len(dhs) == 1:
        dh = dhs[0]
        fwd = a4.get_distance(depot_name, dh, dist_dict, latlong) or 0.0
        bck = a4.get_distance(dh, depot_name, dist_dict, latlong) or 0.0
        return fwd + bck, [dh]

    best_dist, best_order = float("inf"), list(dhs)
    for perm in itertools.permutations(dhs):
        seq = [depot_name] + list(perm) + [depot_name]
        total, feasible = 0.0, True
        for i in range(len(seq) - 1):
            km = a4.get_distance(seq[i], seq[i + 1], dist_dict, latlong)
            if km is None:
                feasible = False
                break
            total += km
        if feasible and total < best_dist:
            best_dist, best_order = total, list(perm)
    return best_dist, best_order


def _size_single_truck(
    dhs: list[str], scaled_demand: dict[str, float], max_ml: float, depot_name: str,
    mh_cfg: "a4.MHConfig", freq: int, dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]], cfg: dict[str, Any],
) -> dict[str, Any]:
    total_demand = sum(scaled_demand.get(dh, 0) for dh in dhs)
    v_len = min(a4.assign_vehicle_length(total_demand), max_ml)
    dist, ordered = _best_route_distance(depot_name, dhs, dist_dict, latlong)
    cost = _route_cost_at_vehicle(dist, v_len, freq, mh_cfg, cfg)
    seq_str = depot_name + " -> " + " -> ".join(ordered) + " -> " + depot_name
    return {
        "route_type": "Dedicated" if len(dhs) == 1 else "MilkRun",
        "route_sequence": seq_str, "hubs": ordered, "dist": dist,
        "assigned_vehicle_length": v_len, "total_demand": round(total_demand, 2),
        "monthly_cost": round(cost, 2), "Freq": freq, "trucks_on_route": 1,
    }


def _size_subset(
    dhs: list[str], scaled_demand: dict[str, float], max_ml: float, depot_name: str,
    mh_cfg: "a4.MHConfig", freq: int, dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]], cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    total_demand = sum(scaled_demand.get(dh, 0) for dh in dhs)
    cap = _route_capacity(max_ml)
    if total_demand <= cap:
        return [_size_single_truck(dhs, scaled_demand, max_ml, depot_name, mh_cfg, freq, dist_dict, latlong, cfg)]
    return _size_two_truck_split(dhs, scaled_demand, max_ml, depot_name, mh_cfg, freq, dist_dict, latlong, cfg)


def _size_two_truck_split(
    dhs: list[str], scaled_demand: dict[str, float], max_ml: float, depot_name: str,
    mh_cfg: "a4.MHConfig", freq: int, dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]], cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    if len(dhs) == 1:
        return [_size_single_truck(dhs, scaled_demand, max_ml, depot_name, mh_cfg, freq, dist_dict, latlong, cfg)]

    best_cost, best_routes = float("inf"), None
    n = len(dhs)
    for mask in range(1, 1 << n):
        if bin(mask).count("1") == n:
            continue
        subset_a = [dhs[i] for i in range(n) if (mask >> i) & 1]
        subset_b = [dhs[i] for i in range(n) if not ((mask >> i) & 1)]
        if not subset_a or not subset_b:
            continue
        routes_a = _size_subset(subset_a, scaled_demand, max_ml, depot_name, mh_cfg, freq, dist_dict, latlong, cfg)
        routes_b = _size_subset(subset_b, scaled_demand, max_ml, depot_name, mh_cfg, freq, dist_dict, latlong, cfg)
        total = sum(r["monthly_cost"] for r in routes_a + routes_b)
        if total < best_cost:
            best_cost, best_routes = total, routes_a + routes_b

    return best_routes if best_routes is not None else [
        _size_single_truck(dhs, scaled_demand, max_ml, depot_name, mh_cfg, freq, dist_dict, latlong, cfg)
    ]


def _compute_shifted_mh_dep(
    route_sequence: str,
    attr: dict[str, dict[str, Any]],
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    service_time_min: float,
) -> float:
    """Port of colab's _compute_shifted_mh_dep. Starting from the first stop's
    depot_departure, simulates arrival/departure through the whole route,
    tracks buffer = time_window_end - arrival at each stop, then pushes the
    departure as late as possible (shift = max(0, min(buffers))) without any
    stop missing its time window."""
    stops = [s.strip() for s in route_sequence.split("->")]
    dh_stops = stops[1:-1]
    depot = stops[0]
    if not dh_stops:
        return attr.get(depot, {}).get("depot_departure", 0.0)

    base_dep = attr.get(dh_stops[0], {}).get("depot_departure", 0.0)
    cur_t = base_dep
    prev = depot
    buffers: list[float] = []
    for dh in dh_stops:
        km = a4.get_distance(prev, dh, dist_dict, latlong)
        km = km if km is not None else 0.0
        arr = cur_t + a4.get_transit_time(km)
        tw_end = attr.get(dh, {}).get("time_window_end", 1800.0)
        tw_start = attr.get(dh, {}).get("time_window_start", 720.0)
        buffers.append(tw_end - arr)
        dep = max(arr, tw_start) + service_time_min
        cur_t = dep
        prev = dh
    shift = max(0.0, min(buffers)) if buffers else 0.0
    return base_dep + shift


def build_baseline_for_mh(
    mh_name: str,
    mh_cfg: "a4.MHConfig",
    dh_rows: pd.DataFrame,
    day_cols: list[str],
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Cost the CURRENT (H2H Current_MR/Current_Freq) route network for one MH."""
    def _emit(msg: str) -> None:
        if callable(on_progress):
            on_progress(msg)

    _emit(f"  BASELINE: {mh_name}")
    depot_name = mh_name
    loc_col = cfg["col_location_name"]
    ml_col = cfg["col_ml"]

    n_days = len(day_cols)
    day_demand_by_day: dict[int, dict[str, float]] = {}
    for d_idx, col in enumerate(day_cols):
        day_demand_by_day[d_idx] = dict(zip(dh_rows[loc_col].astype(str), dh_rows[f"{col}_cft"]))

    avg_demand: dict[str, float] = {}
    for dh in dh_rows[loc_col].astype(str):
        avg_demand[dh] = (
            sum(day_demand_by_day[d].get(dh, 0) for d in range(n_days)) / n_days if n_days else 0.0
        )

    attr_ml = dict(zip(dh_rows[loc_col].astype(str), dh_rows[ml_col]))
    attr = _attr_from_dh_rows(dh_rows, cfg)
    route_groups = parse_current_routes(dh_rows, cfg)

    ded_routes: list[dict[str, Any]] = []
    mr_routes: list[dict[str, Any]] = []
    ded_counts: dict[str, int] = {dh: 0 for dh in dh_rows[loc_col].astype(str)}

    for grp in route_groups:
        dhs = grp["dhs"]
        freq = grp["freq"]
        max_ml = min(attr_ml.get(dh, 40.0) for dh in dhs)
        scaled_avg = {dh: avg_demand.get(dh, 0) * freq for dh in dhs}

        if grp["is_direct"]:
            dh = dhs[0]
            ml_dh = attr_ml.get(dh, 40.0)
            cap = _route_capacity(ml_dh)
            dem = scaled_avg.get(dh, 0)
            n_ded = 0
            while dem > cap:
                fwd = a4.get_distance(depot_name, dh, dist_dict, latlong) or 0.0
                bck = a4.get_distance(dh, depot_name, dist_dict, latlong) or 0.0
                dist = fwd + bck
                cost = _route_cost_at_vehicle(dist, ml_dh, freq, mh_cfg, cfg)
                ded_routes.append({
                    "route_type": "Dedicated", "route_sequence": f"{depot_name} -> {dh} -> {depot_name}",
                    "hubs": [dh], "dist": dist, "assigned_vehicle_length": ml_dh,
                    "total_demand": round(cap, 2), "monthly_cost": round(cost, 2),
                    "Freq": freq, "trucks_on_route": 1,
                })
                dem -= cap
                n_ded += 1
            if dem > 0:
                fwd = a4.get_distance(depot_name, dh, dist_dict, latlong) or 0.0
                bck = a4.get_distance(dh, depot_name, dist_dict, latlong) or 0.0
                dist = fwd + bck
                v_len = min(a4.assign_vehicle_length(dem), ml_dh)
                cost = _route_cost_at_vehicle(dist, v_len, freq, mh_cfg, cfg)
                mr_routes.append({
                    "route_type": "Dedicated", "route_sequence": f"{depot_name} -> {dh} -> {depot_name}",
                    "hubs": [dh], "dist": dist, "assigned_vehicle_length": v_len,
                    "total_demand": round(dem, 2), "monthly_cost": round(cost, 2),
                    "Freq": freq, "trucks_on_route": 1,
                })
            ded_counts[dh] = n_ded
        else:
            trucks = _size_subset(dhs, scaled_avg, max_ml, depot_name, mh_cfg, freq, dist_dict, latlong, cfg)
            n_trucks = len(trucks)
            for t in trucks:
                t["trucks_on_route"] = n_trucks
            mr_routes.extend(trucks)

    committed_monthly = sum(r["monthly_cost"] for r in ded_routes) + sum(r["monthly_cost"] for r in mr_routes)

    fa_rows = []
    for r in ded_routes:
        dep = _compute_shifted_mh_dep(r["route_sequence"], attr, dist_dict, latlong, mh_cfg.service_time_min)
        fa_rows.append({**r, "Route_Type": "FTL_Dedicated", "updated_depot_departure": dep})
    for r in mr_routes:
        dep = _compute_shifted_mh_dep(r["route_sequence"], attr, dist_dict, latlong, mh_cfg.service_time_min)
        fa_rows.append({**r, "Route_Type": "Milkrun", "updated_depot_departure": dep})
    fa_df = pd.DataFrame(fa_rows) if fa_rows else pd.DataFrame(
        columns=["route_type", "route_sequence", "hubs", "dist", "assigned_vehicle_length",
                 "total_demand", "monthly_cost", "Freq", "trucks_on_route", "Route_Type", "updated_depot_departure"]
    )
    fake_result = a4.Agent4MHResult(
        mh_name=mh_name, clustering_df=pd.DataFrame(), filtered_routes_df=pd.DataFrame(),
        final_assignment_df=fa_df, expanded_schedule_df=pd.DataFrame(), validation_lines=[],
        total_monthly_cost=committed_monthly, n_clusters=0, n_perms_checked=0,
        n_routes_survived=len(fa_rows), ilp_status={}, missing_dhs=[],
        absorbed_residuals_df=pd.DataFrame(), dh_summary_df=pd.DataFrame(),
    )

    adhoc_cost, spill_detail, spill_days = run_spillover_simulation(
        day_demand_by_day, fake_result, mh_name, mh_cfg, attr, dist_dict, latlong, cfg
    )

    total_monthly = committed_monthly + adhoc_cost
    _emit(f"  Committed routes : {len(ded_routes)} dedicated + {len(mr_routes)} MR")
    _emit(f"  Committed cost   : Rs {committed_monthly:,.0f}")
    _emit(f"  Adhoc cost       : Rs {adhoc_cost:,.0f}  ({sum(d['n_adhoc_routes'] for d in spill_detail)} routes across {len(spill_detail)} days)")
    _emit(f"  TOTAL baseline   : Rs {total_monthly:,.0f}")
    return {
        "mh_name": mh_name,
        "ded_routes": ded_routes,
        "mr_routes": mr_routes,
        "ded_counts": ded_counts,
        "committed_monthly": committed_monthly,
        "adhoc_monthly": adhoc_cost,
        "total_monthly": total_monthly,
        "spillover_detail": spill_detail,
        "spill_days_per_route": spill_days,
        "route_groups": route_groups,
    }


# ---------------------------------------------------------------------------
# Freeze-day orchestrator for one MH
# ---------------------------------------------------------------------------


def _attr_from_dh_rows(dh_rows: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    loc_col = cfg["col_location_name"]
    ml_col = cfg["col_ml"]
    attr: dict[str, dict[str, Any]] = {}
    for _, row in dh_rows.iterrows():
        dh = str(row[loc_col])
        attr[dh] = {
            "ML": float(row[ml_col]) if pd.notna(row.get(ml_col)) else 40.0,
            "time_window_start": float(row["time_window_start"]) if pd.notna(row.get("time_window_start")) else cfg["default_time_window_start_min"],
            "time_window_end": float(row["time_window_end"]) if pd.notna(row.get("time_window_end")) else cfg["default_time_window_end_min"],
            "depot_departure": float(row["depot_departure"]) if pd.notna(row.get("depot_departure")) else cfg["default_depot_departure_min"],
        }
    return attr


def run_single_mh_freeze_day(
    mh_name: str,
    mh_cfg: "a4.MHConfig",
    dh_rows: pd.DataFrame,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Tests every day (+7 synthetic extremes) as a frozen route plan, simulates
    the real days' demand against each candidate, and picks whichever minimizes
    committed + adhoc cost subject to adhoc% <= cfg['adhoc_pct_limit'].
    """
    def _emit(msg: str) -> None:
        if callable(on_progress):
            on_progress(msg)

    day_cols_all = sorted(
        (c for c in dh_rows.columns if c.startswith("D") and c[1:].isdigit() and f"{c}_cft" in dh_rows.columns),
        key=lambda c: int(c[1:]),
    )
    if not day_cols_all:
        return {"mh_name": mh_name, "status": "no_day_columns", "results": []}

    real_day_cols = day_cols_all[:-7] if len(day_cols_all) > 7 else day_cols_all
    # Compare against the actual max real day NUMBER (e.g. 61 for a D32..D61
    # window), not the day COUNT (30) -- day columns are named after source
    # day_N numbers and are rarely 1-based, so a count-based threshold
    # mislabels real days (e.g. D54) as synthetic whenever the window doesn't
    # start at day_1.
    max_real_day_num = max(int(c[1:]) for c in real_day_cols)

    loc_col = cfg["col_location_name"]
    day_cft_series: dict[int, dict[str, float]] = {}
    for d_idx, col in enumerate(real_day_cols):
        day_cft_series[d_idx] = dict(zip(dh_rows[loc_col].astype(str), dh_rows[f"{col}_cft"]))

    adhoc_pct_limit = float(cfg.get("adhoc_pct_limit", 0.10))
    attr = _attr_from_dh_rows(dh_rows, cfg)

    _emit(f"  MH: {mh_name}  |  DHs: {len(dh_rows)}  |  freeze candidates: {len(day_cols_all)}  |  real days: {len(real_day_cols)}")

    results: list[dict[str, Any]] = []
    for rank, freeze_col in enumerate(day_cols_all, start=1):
        net_demand = float(dh_rows[f"{freeze_col}_cft"].sum())
        _emit(f"  -- Freeze day {rank}/{len(day_cols_all)}: {freeze_col} (net={net_demand:,.0f} CFT) --")

        mh_result = run_freeze_day_candidate(mh_name, mh_cfg, dh_rows, freeze_col, dist_dict, latlong, cfg)
        fa = mh_result.final_assignment_df
        if fa is None or fa.empty:
            _emit(f"    SKIP: no routes produced for {freeze_col}")
            continue

        n_ded = int((fa["Route_Type"] == "FTL_Dedicated").sum())
        n_mr = int((fa["Route_Type"] == "Milkrun").sum())
        n_freq2 = int((fa["Freq"] == 2).sum())
        _emit(f"    Dedicated trucks: {n_ded}  |  MR routes: {n_mr} (freq=2: {n_freq2}, freq=1: {n_mr - n_freq2})")

        upgraded_result, adhoc_cost, spill_detail, committed, total, spill_days = run_truck_upgrade_loop(
            mh_result, day_cft_series, mh_name, mh_cfg, attr, dist_dict, latlong, cfg, on_progress=on_progress
        )

        reg_trips = int(sum(30 if r["Freq"] == 1 else 15 for _, r in upgraded_result.final_assignment_df.iterrows()))
        n_adhoc_routes = sum(d["n_adhoc_routes"] for d in spill_detail)
        tot_trips = reg_trips + n_adhoc_routes
        adhoc_pct = (n_adhoc_routes / tot_trips) if tot_trips else 0.0
        _emit(f"    Committed: Rs {committed:,.0f}  |  Adhoc: Rs {adhoc_cost:,.0f} ({n_adhoc_routes} routes)  |  "
              f"TOTAL: Rs {total:,.0f}  |  adhoc%={adhoc_pct*100:.1f}")

        results.append({
            "freeze_day": freeze_col,
            "is_synthetic": int(freeze_col[1:]) > max_real_day_num,
            "result": upgraded_result,
            "committed_monthly": committed,
            "adhoc_monthly": adhoc_cost,
            "total_monthly": total,
            "spillover_detail": spill_detail,
            "spill_days_per_route": spill_days,
            "n_adhoc_routes_total": n_adhoc_routes,
            "regular_trips": reg_trips,
            "adhoc_pct": adhoc_pct,
            "net_demand": net_demand,
        })

    if not results:
        return {"mh_name": mh_name, "status": "no_feasible_candidates", "results": []}

    results_sorted = sorted(results, key=lambda r: r["total_monthly"])
    eligible = [r for r in results_sorted if r["adhoc_pct"] <= adhoc_pct_limit]
    best = eligible[0] if eligible else results_sorted[0]
    unconstrained_best = results_sorted[0]

    if not eligible:
        _emit(f"  WARNING [{mh_name}]: no day <= {adhoc_pct_limit*100:.0f}% adhoc -- using unconstrained optimum.")
    synth_str = " [SYNTHETIC]" if best["is_synthetic"] else ""
    _emit(f"  *** OPTIMAL [{mh_name}]: {best['freeze_day']}{synth_str}  Rs {best['total_monthly']:,.0f}  adhoc={best['adhoc_pct']*100:.1f}%")

    return {
        "mh_name": mh_name,
        "status": "ok",
        "results": results,
        "best": best,
        "unconstrained_best": unconstrained_best,
        "constrained": bool(eligible),
    }


# ---------------------------------------------------------------------------
# Ad-hoc single-day runner (port of colab Block 14). For Claude, not the user
# directly -- lets Claude answer "run PAT6 for day 16" without a full
# 37-candidate freeze-day search and without touching agent4.py or this
# module's core logic. Reuses every existing helper; adds no new route/cost
# logic of its own.
# ---------------------------------------------------------------------------


def run_freeze_day_single_day(
    mh_name: str,
    mh_cfg: "a4.MHConfig",
    dh_rows: pd.DataFrame,
    chosen_day: str,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    out_dir=None,
    baseline: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Freezes the route plan at exactly `chosen_day` (e.g. 'D16') instead of
    searching all 37 candidates, then simulates the real days against it --
    same committed/adhoc costing as the main pipeline, just for one named day.

    chosen_day must be a REAL day column present in dh_rows (e.g. 'D16'), not
    one of the 7 synthetic extreme days -- simulating a single synthetic day
    against real demand isn't a meaningful question to ask.

    baseline: optionally pass an already-computed build_baseline_for_mh(...)
    result to get a Chosen-day-vs-current-routes comparison row too.

    out_dir: optionally write FA_<mh>_<day>.csv, ES_<mh>_<day>.csv,
    SP_<mh>_<day>.csv, and (if baseline given) BVO_<mh>_<day>.csv.
    """
    day_cols_all = sorted(
        (c for c in dh_rows.columns if c.startswith("D") and c[1:].isdigit() and f"{c}_cft" in dh_rows.columns),
        key=lambda c: int(c[1:]),
    )
    if not day_cols_all:
        return {"status": "failed", "issues": [{"type": "no_day_columns", "detail": "No D<n>_cft columns in dh_rows"}], "data": None}

    real_day_cols = day_cols_all[:-7] if len(day_cols_all) > 7 else day_cols_all
    if chosen_day not in real_day_cols:
        return {
            "status": "failed",
            "issues": [{
                "type": "invalid_chosen_day",
                "detail": f"'{chosen_day}' is not a real day column for this MH. "
                          f"Real days available: {real_day_cols[0]}..{real_day_cols[-1]}",
            }],
            "data": None,
        }

    loc_col = cfg["col_location_name"]
    day_cft_series: dict[int, dict[str, float]] = {}
    for d_idx, col in enumerate(real_day_cols):
        day_cft_series[d_idx] = dict(zip(dh_rows[loc_col].astype(str), dh_rows[f"{col}_cft"]))

    attr = _attr_from_dh_rows(dh_rows, cfg)
    mh_result = run_freeze_day_candidate(mh_name, mh_cfg, dh_rows, chosen_day, dist_dict, latlong, cfg)
    fa = mh_result.final_assignment_df
    if fa is None or fa.empty:
        return {"status": "failed", "issues": [{"type": "no_routes", "detail": f"No routes produced for {chosen_day}"}], "data": None}

    upgraded_result, adhoc_cost, spill_detail, committed, total, spill_days = run_truck_upgrade_loop(
        mh_result, day_cft_series, mh_name, mh_cfg, attr, dist_dict, latlong, cfg
    )
    reg_trips = int(sum(30 if r["Freq"] == 1 else 15 for _, r in upgraded_result.final_assignment_df.iterrows()))
    n_adhoc_routes = sum(d["n_adhoc_routes"] for d in spill_detail)
    tot_trips = reg_trips + n_adhoc_routes
    adhoc_pct = (n_adhoc_routes / tot_trips) if tot_trips else 0.0
    max_real_day_num = max(int(c[1:]) for c in real_day_cols)

    candidate = {
        "freeze_day": chosen_day,
        "is_synthetic": int(chosen_day[1:]) > max_real_day_num,
        "result": upgraded_result,
        "committed_monthly": committed,
        "adhoc_monthly": adhoc_cost,
        "total_monthly": total,
        "spillover_detail": spill_detail,
        "spill_days_per_route": spill_days,
        "n_adhoc_routes_total": n_adhoc_routes,
        "regular_trips": reg_trips,
        "adhoc_pct": adhoc_pct,
        "net_demand": float(dh_rows[f"{chosen_day}_cft"].sum()),
    }

    final_rows, schedule_rows = _build_final_assignment_and_schedule_rows(candidate, mh_name)
    spill_rows = _build_spillover_detail_rows(candidate, mh_name)
    route_log_rows = _build_per_day_route_log_rows(candidate, mh_name, chosen_day)
    adhoc_rows = _build_adhoc_summary(spill_detail, mh_name, cfg, "Chosen_Day")

    comparison_rows: list[dict[str, Any]] = []
    if baseline is not None:
        bc, ba, bt = baseline["committed_monthly"], baseline["adhoc_monthly"], baseline["total_monthly"]
        savings = bt - total
        comparison_rows.append({
            "MH": mh_name, "Chosen_Day": chosen_day,
            "Current_Committed": round(bc, 2), "Chosen_Committed": round(committed, 2),
            "Current_Adhoc": round(ba, 2), "Chosen_Adhoc": round(adhoc_cost, 2),
            "Current_Total": round(bt, 2), "Chosen_Total": round(total, 2),
            "Savings_vs_Current": round(savings, 2),
            "Savings_Pct": round(savings / bt * 100, 1) if bt else 0.0,
        })

    data = {
        "candidate": candidate,
        "final_assignment": pd.DataFrame(final_rows),
        "expanded_schedule": pd.DataFrame(schedule_rows),
        "spillover_detail_rows": pd.DataFrame(spill_rows),
        "per_day_route_log": pd.DataFrame(route_log_rows),
        "adhoc_route_summary": pd.DataFrame(adhoc_rows),
        "baseline_vs_chosen_day": pd.DataFrame(comparison_rows),
    }

    if out_dir is not None:
        from pathlib import Path
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_mh = mh_name.replace("/", "_").replace(" ", "_")
        data["final_assignment"].to_csv(out_dir / f"FA_{safe_mh}_{chosen_day}.csv", index=False)
        data["expanded_schedule"].to_csv(out_dir / f"ES_{safe_mh}_{chosen_day}.csv", index=False)
        data["spillover_detail_rows"].to_csv(out_dir / f"SP_{safe_mh}_{chosen_day}.csv", index=False)
        if not data["baseline_vs_chosen_day"].empty:
            data["baseline_vs_chosen_day"].to_csv(out_dir / f"BVO_{safe_mh}_{chosen_day}.csv", index=False)

    return {"status": "ok", "issues": [], "data": data}


# ---------------------------------------------------------------------------
# Top-level pipeline: loops over MHs, builds baseline + freeze-day plan,
# writes outputs. Sits alongside agent4.run_agent4_pipeline (does not replace
# it) -- Phase 2 keeps calling the legacy functions unchanged.
# ---------------------------------------------------------------------------


def _build_per_day_route_log_rows(
    candidate: dict[str, Any], mh_name: str, optimal_freeze_day: str,
) -> list[dict[str, Any]]:
    """One row per route for one freeze-day candidate (port of colab's
    Per_Day_Route_Log builder). Called for EVERY candidate, not just optimal."""
    rows: list[dict[str, Any]] = []
    fa = candidate["result"].final_assignment_df
    spill_days = candidate.get("spill_days_per_route", {})
    mr_idx = 0
    for idx, route in fa.reset_index(drop=True).iterrows():
        is_ded = route["Route_Type"] != "Milkrun"
        if is_ded:
            spill_day_val = 0
        else:
            spill_day_val = spill_days.get(mr_idx, 0)
            mr_idx += 1
        rows.append({
            "MH": mh_name,
            "Freeze_Day": candidate["freeze_day"],
            "Is_Synthetic": candidate["is_synthetic"],
            "Net_Demand": round(candidate.get("net_demand", 0.0), 1),
            "Route_ID": idx + 1,
            "Route_Type": route["Route_Type"],
            "Route_Sequence": route["route_sequence"],
            "Vehicle_Length": route["assigned_vehicle_length"],
            "Total_Demand": round(route.get("total_demand", 0), 2),
            "Monthly_Cost": round(route.get("monthly_cost", 0), 2),
            "Dist_km": round(route.get("dist", 0), 2),
            "Freq": route["Freq"],
            "Spill_Days": spill_day_val,
            "Committed_Monthly_Total": round(candidate["committed_monthly"], 2),
            "Adhoc_Monthly_Total": round(candidate["adhoc_monthly"], 2),
            "Grand_Total_Monthly": round(candidate["total_monthly"], 2),
            "Is_Optimal": "YES" if candidate["freeze_day"] == optimal_freeze_day else "",
        })
    return rows


def _build_spillover_detail_rows(candidate: dict[str, Any], mh_name: str) -> list[dict[str, Any]]:
    """One row per stop per ad-hoc route per real day, for one freeze-day
    candidate (port of colab's _build_spillover_rows)."""
    rows: list[dict[str, Any]] = []
    for day_spill in candidate["spillover_detail"]:
        day_adhoc_cost = day_spill["adhoc_cost"]
        for ar_id, opt_route in enumerate(day_spill["routes"], start=1):
            seq_dhs = opt_route["seq"]
            timing = opt_route["timing"]
            vols = opt_route["vols"]
            items = opt_route.get("items", [])
            vehicle, dist, cost = opt_route["vehicle"], opt_route["dist"], opt_route["cost"]
            merged = "Yes" if opt_route["merged"] else "No"
            depot_dep = opt_route.get("mh_dep", 0.0)
            full_seq_str = f"{mh_name} -> " + " -> ".join(seq_dhs) + f" -> {mh_name}"

            def row(stop_no, loc, loc_type, arr_m=None, dep_m=None, vol="", stype=""):
                return {
                    "MH": mh_name,
                    "Freeze_Day": candidate["freeze_day"],
                    "Heavy_Day_Index": day_spill["day_idx"],
                    "Adhoc_Route_ID": f"AR{ar_id}",
                    "Adhoc_Route_Seq": full_seq_str,
                    "Stop_No": stop_no,
                    "Location": loc,
                    "Location_Type": loc_type,
                    "Arrival_Time": round(arr_m, 1) if arr_m is not None else None,
                    "Departure_Time": round(dep_m, 1) if dep_m is not None else None,
                    "Spill_Vol": round(vol, 2) if vol != "" else "",
                    "Spill_Type": stype,
                    "Vehicle_Length": vehicle,
                    "Adhoc_Dist_km": round(dist, 1),
                    "Adhoc_Route_Cost": round(cost, 2),
                    "Merged": merged,
                    "Day_Total_Adhoc": round(day_adhoc_cost, 2),
                }

            rows.append(row(0, mh_name, "Depot", dep_m=depot_dep))
            for sno, dh in enumerate(seq_dhs, start=1):
                arr_m, dep_m = timing.get(dh, (None, None))
                item_type = next((it["type"] for it in items if it["dh"] == dh), "mr_adhoc")
                rows.append(row(sno, dh, "DH", arr_m=arr_m, dep_m=dep_m,
                                 vol=round(vols.get(dh, 0), 2), stype=item_type))
            if seq_dhs:
                last_dh = seq_dhs[-1]
                _, last_dep = timing.get(last_dh, (None, None))
                if last_dep is not None:
                    rows.append(row(len(seq_dhs) + 1, mh_name, "Depot", arr_m=last_dep))
    return rows


def _build_adhoc_summary(
    spillover_detail: list[dict[str, Any]], mh_name: str, cfg: dict[str, Any], source: str,
) -> list[dict[str, Any]]:
    """One row per distinct ad-hoc route sequence used across the month, with
    a 'standing backup route' suggestion when a route recurs >=
    adhoc_repeat_threshold_days times (port of colab's _build_adhoc_summary /
    the baseline equivalent in _build_baseline_sheet_rows). `source` is
    'Optimal' or 'Baseline' so both can land in one output file."""
    threshold = int(cfg.get("adhoc_repeat_threshold_days", 7))
    route_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "vehicle": 0, "dist": 0.0, "cost_per_trip": 0.0, "count": 0, "total_cost": 0.0
    })
    for day_spill in spillover_detail:
        for opt_route in day_spill["routes"]:
            seq_str = f"{mh_name} -> " + " -> ".join(opt_route["seq"]) + f" -> {mh_name}"
            rs = route_stats[seq_str]
            rs["vehicle"] = opt_route["vehicle"]
            rs["dist"] = round(opt_route["dist"], 1)
            rs["cost_per_trip"] = round(opt_route["cost"], 2)
            rs["count"] += 1
            rs["total_cost"] += opt_route["cost"]

    rows = []
    for seq_str, rs in sorted(route_stats.items(), key=lambda x: -x[1]["count"]):
        suggestion = "Consider as standing backup route" if rs["count"] >= threshold else ""
        rows.append({
            "MH": mh_name,
            "Source": source,
            "Adhoc_Route_Seq": seq_str,
            "Vehicle_Length": rs["vehicle"],
            "Adhoc_Dist_km": rs["dist"],
            "Cost_Per_Trip": rs["cost_per_trip"],
            "Times_Used": rs["count"],
            "Total_Adhoc_Cost": round(rs["total_cost"], 2),
            "Suggestion": suggestion,
        })
    return rows


def _route_coords(route_sequence: str, latlong: dict[str, tuple[float, float]]) -> list[dict[str, Any]]:
    coords = []
    for stop in [s.strip() for s in route_sequence.split("->")]:
        ll = latlong.get(stop)
        if ll is not None and ll[0] is not None:
            coords.append({"name": stop, "lat": ll[0], "lng": ll[1]})
    return coords


def build_route_visualizer_data(
    per_mh_results: dict[str, Any],
    latlong: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    """Assembles the route_data.json-equivalent structure (port of colab Block
    10/10.5's data prep) from this engine's per_mh_results (as returned in
    run_agent4_freeze_day_pipeline's result["data"]["per_mh_results"])."""
    days: dict[str, Any] = {}
    optimal_per_mh: dict[str, str] = {}
    baseline_viz: dict[str, Any] = {}

    for mh_name, mh_data in per_mh_results.items():
        freeze = mh_data["freeze"]
        baseline = mh_data["baseline"]
        if freeze.get("status") != "ok":
            continue

        optimal_day = freeze["best"]["freeze_day"]
        for r in freeze["results"]:
            fa = r["result"].final_assignment_df
            routes_out = []
            n_dedicated = n_mr = 0
            for _, row in fa.iterrows():
                is_ded = row["Route_Type"] == "FTL_Dedicated"
                n_dedicated += int(is_ded)
                n_mr += int(not is_ded)
                routes_out.append({
                    "type": row["Route_Type"], "seq": row["route_sequence"],
                    "vehicle": row["assigned_vehicle_length"],
                    "demand": round(row.get("total_demand", 0), 1),
                    "cost": round(row.get("monthly_cost", 0), 0),
                    "freq": row.get("Freq", 1),
                    "coords": _route_coords(row["route_sequence"], latlong),
                })
            key = f"{mh_name}__{r['freeze_day']}"
            days[key] = {
                "mh": mh_name, "freeze_day": r["freeze_day"],
                "freeze_day_index": int(r["freeze_day"][1:]),
                "is_synthetic": r["is_synthetic"],
                "net_demand": round(r.get("net_demand", 0.0), 1),
                "n_dedicated": n_dedicated, "n_mr_routes": n_mr,
                "regular_trips": r["regular_trips"], "adhoc_trips": r["n_adhoc_routes_total"],
                "adhoc_pct": round(r["adhoc_pct"] * 100, 1),
                "committed_monthly": round(r["committed_monthly"], 0),
                "adhoc_monthly": round(r["adhoc_monthly"], 0),
                "total_monthly": round(r["total_monthly"], 0),
                "is_optimal": r["freeze_day"] == optimal_day,
                "routes": routes_out,
            }
        optimal_per_mh[mh_name] = f"{mh_name}__{optimal_day}"

        b_routes_out = []
        for r in baseline["ded_routes"] + baseline["mr_routes"]:
            b_routes_out.append({
                "type": r["route_type"], "seq": r["route_sequence"],
                "vehicle": r["assigned_vehicle_length"],
                "demand": round(r.get("total_demand", 0), 1),
                "cost": round(r.get("monthly_cost", 0), 0),
                "freq": r.get("Freq", 1), "trucks": r.get("trucks_on_route", 1),
                "coords": _route_coords(r["route_sequence"], latlong),
            })
        b_reg = sum(30 if r.get("Freq", 1) == 1 else 15 for r in baseline["ded_routes"] + baseline["mr_routes"])
        b_adh = sum(d["n_adhoc_routes"] for d in baseline["spillover_detail"])
        b_tot = b_reg + b_adh
        baseline_viz[mh_name] = {
            "mh": mh_name,
            "committed_monthly": round(baseline["committed_monthly"], 0),
            "adhoc_monthly": round(baseline["adhoc_monthly"], 0),
            "total_monthly": round(baseline["total_monthly"], 0),
            "regular_trips": b_reg, "adhoc_trips": b_adh,
            "adhoc_pct": round(b_adh / b_tot * 100, 1) if b_tot else 0.0,
            "n_dedicated": len(baseline["ded_routes"]), "n_mr_routes": len(baseline["mr_routes"]),
            "routes": b_routes_out,
        }

    return {"optimal_per_mh": optimal_per_mh, "days": days, "baseline": baseline_viz}


_VISUALIZER_PALETTE = [
    "#185FA5", "#D85A30", "#1D9E75", "#7F77DD", "#BA7517",
    "#D4537E", "#378ADD", "#993C1D", "#085041", "#3C3489",
    "#E24B4A", "#0F6E56", "#854F0B", "#639922", "#993556",
    "#444441", "#534AB7", "#0C447C", "#5DCAA5", "#EF9F27",
]
_VISUALIZER_BASELINE_COLOR = "#8B6F47"


def _build_visualizer_html(data: dict[str, Any], adhoc_limit_pct: float) -> str:
    """Port of colab's route_visualizer_builder.build_html -- same Leaflet-based
    HTML/JS template, sourced from this engine's data structures instead of a
    Colab-populated route_data.json."""
    import json as _json

    optimal_per_mh = data.get("optimal_per_mh", {})
    days_data = data["days"]
    baseline_data = data.get("baseline", {})
    mh_names = list(dict.fromkeys(v["mh"] for v in days_data.values()))

    data_json = _json.dumps(days_data, ensure_ascii=False)
    optimal_json = _json.dumps(optimal_per_mh, ensure_ascii=False)
    mh_names_json = _json.dumps(mh_names, ensure_ascii=False)
    palette_json = _json.dumps(_VISUALIZER_PALETTE, ensure_ascii=False)
    baseline_json = _json.dumps(baseline_data, ensure_ascii=False)
    baseline_color = _VISUALIZER_BASELINE_COLOR
    adhoc_limit_str = str(adhoc_limit_pct)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Route Visualizer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;
  display:flex;height:100vh;overflow:hidden;background:#f5f5f3;color:#1a1a1a}}
#sidebar{{width:360px;min-width:280px;display:flex;flex-direction:column;
  border-right:1px solid #ddd;background:#fff;overflow:hidden}}
#sidebar-header{{padding:14px 16px 10px;border-bottom:1px solid #eee;flex-shrink:0}}
.sh-title{{font-size:14px;font-weight:600;color:#1a1a1a;margin-bottom:3px}}
.sh-subtitle{{font-size:11px;color:#999;margin-bottom:10px;line-height:1.4}}
.sh-mh-row{{display:flex;align-items:center;gap:8px}}
.sh-label{{font-size:10px;font-weight:500;letter-spacing:.07em;text-transform:uppercase;
  color:#aaa;white-space:nowrap;flex-shrink:0}}
#mh-select{{
  flex:1;font-size:12px;font-weight:500;
  border:.5px solid #d0d0ce;border-radius:6px;
  padding:5px 28px 5px 9px;background:#fafaf9;color:#1a1a1a;
  cursor:pointer;outline:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='11' height='11' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 9px center;
}}
#mh-select:hover{{border-color:#b0b0ae;background-color:#f5f5f3}}
#mh-select:focus{{border-color:#185FA5;box-shadow:0 0 0 2px rgba(24,95,165,.12)}}
#controls{{padding:8px 14px;border-bottom:1px solid #eee;display:flex;gap:6px;
  flex-wrap:wrap;flex-shrink:0}}
#controls button{{font-size:11px;padding:4px 10px;border:.5px solid #ccc;
  border-radius:6px;background:#fff;cursor:pointer;color:#444}}
#controls button:hover{{background:#f5f5f3;border-color:#aaa}}
#controls button.baseline-btn{{border-color:{baseline_color};color:{baseline_color}}}
#controls button.baseline-btn:hover{{background:#f9f5f0}}
#day-list{{flex:1;overflow-y:auto;padding:4px 0}}
.baseline-row{{display:flex;align-items:flex-start;padding:8px 14px;gap:10px;
  cursor:pointer;background:#fdf8f3;border-bottom:1.5px solid #e8d8c4;
  transition:background .1s}}
.baseline-row:hover{{background:#f5ede0}}
.baseline-row.selected{{background:#f0e4d0}}
.bl-icon{{width:9px;height:9px;border-radius:2px;margin-top:4px;flex-shrink:0;
  background:{baseline_color}}}
.bl-label{{font-weight:600;font-size:12px;color:{baseline_color}}}
.bl-meta{{font-size:11px;color:#a08060;margin-top:2px;line-height:1.5}}
.day-row{{display:flex;align-items:flex-start;padding:7px 14px;gap:10px;
  cursor:pointer;border-bottom:.5px solid #f0f0ee;transition:background .1s}}
.day-row:hover{{background:#f8f8f6}}
.day-row.selected{{background:#EAF3DE}}
.day-cb{{margin-top:3px;accent-color:#185FA5;flex-shrink:0}}
.day-swatch{{width:9px;height:9px;border-radius:2px;margin-top:4px;flex-shrink:0}}
.day-info{{flex:1;min-width:0}}
.day-name{{font-weight:500;font-size:13px;display:flex;align-items:center;
  gap:5px;flex-wrap:wrap}}
.day-meta{{font-size:11px;color:#888;margin-top:2px;line-height:1.5}}
.badge{{display:inline-block;font-size:10px;padding:1px 6px;border-radius:4px;
  font-weight:500;white-space:nowrap}}
.bg{{background:#EAF3DE;color:#3B6D11}}
.br{{background:#FCEBEB;color:#A32D2D}}
.bb{{background:#E6F1FB;color:#185FA5}}
.bsav{{background:#FEF3E2;color:#8B6F47}}
#summary-panel{{padding:10px 14px;border-top:1px solid #eee;
  max-height:240px;overflow-y:auto;flex-shrink:0}}
#summary-panel h2{{font-size:10px;font-weight:500;color:#aaa;margin-bottom:8px;
  letter-spacing:.06em;text-transform:uppercase}}
#summary-table{{width:100%;border-collapse:collapse;font-size:11px}}
#summary-table th{{text-align:left;color:#aaa;font-weight:400;
  padding:3px 6px 3px 0;border-bottom:.5px solid #eee;white-space:nowrap}}
#summary-table td{{padding:4px 6px 4px 0;border-bottom:.5px solid #f5f5f3}}
#summary-table tr.opt-row td{{font-weight:500;background:#EAF3DE}}
#summary-table tr.baseline-trow td{{font-weight:500;background:#fdf0e0;
  color:{baseline_color}}}
#map{{flex:1}}
.route-tip{{font-size:12px;line-height:1.6}}
</style>
</head>
<body>
<div id="sidebar">
  <div id="sidebar-header">
    <div class="sh-title">Route visualizer</div>
    <div class="sh-subtitle">Toggle freeze days or current routes to overlay on the map.</div>
    <div class="sh-mh-row">
      <span class="sh-label">Motherhub</span>
      <select id="mh-select" onchange="switchMH(this.value)"></select>
    </div>
  </div>
  <div id="controls">
    <button onclick="clearAll()">Clear all</button>
    <button onclick="selectOptimal()">Optimal only</button>
    <button onclick="selectTop3()">Top 3 eligible</button>
    <button class="baseline-btn" onclick="toggleBaseline()">Current routes</button>
  </div>
  <div id="day-list"></div>
  <div id="summary-panel">
    <h2>Selected — trip summary</h2>
    <table id="summary-table">
      <thead><tr>
        <th>Day / Source</th><th>MH</th><th>Plan trips</th><th>Adhoc</th>
        <th>Adhoc%</th><th>Committed</th><th>Adhoc cost</th><th>Total</th>
      </tr></thead>
      <tbody id="summary-body"></tbody>
    </table>
  </div>
</div>
<div id="map"></div>

<script>
const ALL           = {data_json};
const OPT_PER_MH    = {optimal_json};
const MH_NAMES      = {mh_names_json};
const PAL           = {palette_json};
const BASELINE      = {baseline_json};
const BASELINE_CLR  = '{baseline_color}';
const LIM           = {adhoc_limit_str};

const map = L.map('map').setView([28.5, 77.0], 6);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{
  attribution:'© OpenStreetMap contributors',maxZoom:14
}}).addTo(map);

let currentMH     = MH_NAMES[0] || null;
let baselineShown = false;
const sel         = new Set();
const layers      = {{}};
const clr         = {{}};
let   baselineLayer = null;

Object.keys(ALL).forEach((k,i) => clr[k] = PAL[i % PAL.length]);

const fmt  = n => '₹'+(n/1e5).toFixed(1)+'L';
const fmtK = n => '₹'+(n/1000).toFixed(0)+'K';

function savingsVsBaseline(mh, total) {{
  if (!BASELINE[mh]) return null;
  return BASELINE[mh].total_monthly - total;
}}

function keysForMH(mh) {{
  return Object.keys(ALL)
    .filter(k => ALL[k].mh === mh)
    .sort((a,b) => ALL[a].freeze_day_index - ALL[b].freeze_day_index);
}}

function buildDropdown() {{
  const el = document.getElementById('mh-select');
  el.innerHTML = '';
  MH_NAMES.forEach(mh => {{
    const opt = document.createElement('option');
    opt.value = mh; opt.textContent = mh;
    if (mh === currentMH) opt.selected = true;
    el.appendChild(opt);
  }});
}}

function buildSidebar() {{
  const list = document.getElementById('day-list');
  list.innerHTML = '';

  const bdata = BASELINE[currentMH];
  if (bdata) {{
    const bdiv = document.createElement('div');
    bdiv.className = 'baseline-row' + (baselineShown ? ' selected' : '');
    bdiv.id = 'baseline-row';
    bdiv.innerHTML = `
      <input type="checkbox" class="day-cb" id="cb-baseline"
             ${{baselineShown ? 'checked' : ''}}
             style="accent-color:${{BASELINE_CLR}}">
      <div class="bl-icon"></div>
      <div class="day-info">
        <div class="day-name" style="color:${{BASELINE_CLR}}">
          Current Routes
          <span class="badge" style="background:#f0e4d0;color:${{BASELINE_CLR}}">baseline</span>
        </div>
        <div class="bl-meta">
          ${{bdata.n_dedicated}} ded · ${{bdata.n_mr_routes}} MR ·
          ${{fmt(bdata.committed_monthly)}} committed · ${{fmt(bdata.adhoc_monthly)}} adhoc
        </div>
      </div>`;
    bdiv.querySelector('input').addEventListener('change', e => {{
      e.stopPropagation(); toggleBaseline();
    }});
    bdiv.addEventListener('click', e => {{
      if (e.target.tagName === 'INPUT') return;
      document.getElementById('cb-baseline').checked =
        !document.getElementById('cb-baseline').checked;
      toggleBaseline();
    }});
    list.appendChild(bdiv);
  }}

  const keys   = keysForMH(currentMH);
  const optKey = OPT_PER_MH[currentMH];
  keys.forEach(k => {{
    const d   = ALL[k];
    const pct = d.adhoc_pct;
    const ok  = pct <= LIM;
    const isO = k === optKey;

    const sav = savingsVsBaseline(currentMH, d.total_monthly);
    const savBadge = (sav !== null && sav > 0 && isO)
      ? `<span class="badge bsav">saves ${{fmtK(sav)}}</span>` : '';

    const pb = pct === 0
      ? `<span class="badge bg">0% adhoc</span>`
      : ok ? `<span class="badge bg">${{pct}}%</span>`
           : `<span class="badge br">${{pct}}% !</span>`;
    const ob = isO ? `<span class="badge bb">optimal</span>` : '';

    const div = document.createElement('div');
    div.className = 'day-row' + (sel.has(k) ? ' selected' : '');
    div.id = 'row-' + k;
    div.innerHTML = `
      <input type="checkbox" class="day-cb" id="cb-${{k}}"
             ${{sel.has(k) ? 'checked' : ''}}>
      <div class="day-swatch" style="background:${{clr[k]}}"></div>
      <div class="day-info">
        <div class="day-name">${{d.freeze_day}} — ${{fmt(d.total_monthly)}} ${{ob}} ${{pb}} ${{savBadge}}</div>
        <div class="day-meta">
          ${{d.n_dedicated}} ded · ${{d.n_mr_routes}} MR ·
          ${{d.adhoc_trips}} adhoc / ${{d.regular_trips + d.adhoc_trips}} total trips
        </div>
      </div>`;
    div.querySelector('input').addEventListener('change', e => {{
      e.stopPropagation(); toggle(k);
    }});
    div.addEventListener('click', e => {{
      if (e.target.tagName === 'INPUT') return;
      document.getElementById('cb-' + k).checked =
        !document.getElementById('cb-' + k).checked;
      toggle(k);
    }});
    list.appendChild(div);
  }});
}}

function toggle(k) {{ sel.has(k) ? deselect(k) : select(k); updateTable(); }}

function select(k) {{
  sel.add(k);
  const rowEl = document.getElementById('row-' + k);
  if (rowEl) rowEl.classList.add('selected');
  const cbEl = document.getElementById('cb-' + k);
  if (cbEl) cbEl.checked = true;
  drawRoutes(k);
}}

function deselect(k) {{
  sel.delete(k);
  const rowEl = document.getElementById('row-' + k);
  if (rowEl) rowEl.classList.remove('selected');
  const cbEl = document.getElementById('cb-' + k);
  if (cbEl) cbEl.checked = false;
  if (layers[k]) {{ map.removeLayer(layers[k]); delete layers[k]; }}
}}

function toggleBaseline() {{
  baselineShown = !baselineShown;
  const rowEl = document.getElementById('baseline-row');
  const cbEl  = document.getElementById('cb-baseline');
  if (rowEl) rowEl.classList.toggle('selected', baselineShown);
  if (cbEl)  cbEl.checked = baselineShown;
  if (baselineShown) {{
    drawBaselineRoutes();
  }} else {{
    if (baselineLayer) {{ map.removeLayer(baselineLayer); baselineLayer = null; }}
  }}
  updateTable();
}}

function clearAll() {{
  [...sel].forEach(deselect);
  if (baselineShown) toggleBaseline();
  updateTable();
}}

function selectOptimal() {{
  clearAll();
  const optKey = OPT_PER_MH[currentMH];
  if (optKey && ALL[optKey]) select(optKey);
  updateTable();
}}

function selectTop3() {{
  clearAll();
  const keys = keysForMH(currentMH)
    .filter(k => ALL[k].adhoc_pct <= LIM)
    .sort((a,b) => ALL[a].total_monthly - ALL[b].total_monthly);
  keys.slice(0,3).forEach(select);
  updateTable();
}}

function switchMH(mh) {{
  clearAll();
  currentMH = mh;
  baselineShown = false;
  buildSidebar();
  const optKey = OPT_PER_MH[mh];
  if (optKey && ALL[optKey]) select(optKey);
  updateTable();
}}

function drawRoutes(k) {{
  const d  = ALL[k];
  const c  = clr[k];
  const lg = L.layerGroup();
  d.routes.forEach(r => {{
    if (r.coords.length < 2) return;
    const ll    = r.coords.map(p => [p.lat, p.lng]);
    const isDed = r.type === 'FTL_Dedicated';
    L.polyline(ll, {{
      color: c, weight: isDed ? 4 : 2.5,
      opacity: isDed ? .95 : .75,
      dashArray: isDed ? null : '7,4'
    }}).bindTooltip(`<div class="route-tip">
      <b>${{d.mh}} · ${{d.freeze_day}}</b> — ${{r.type}}<br>
      ${{r.seq}}<br>
      Vehicle ${{r.vehicle}}ft · ${{r.demand}} CFT · ${{fmt(r.cost)}}/month ·
      Freq=${{r.freq || 1}}
    </div>`, {{sticky:true}}).addTo(lg);
    r.coords.forEach((p,i) => {{
      if (i===0 || i===r.coords.length-1) return;
      L.circleMarker([p.lat,p.lng], {{
        radius:5, color:c, fillColor:c, fillOpacity:.88, weight:1.5
      }}).bindTooltip(`<b>${{p.name}}</b><br>${{d.mh}} · ${{d.freeze_day}}`,
                      {{sticky:true}}).addTo(lg);
    }});
  }});
  layers[k] = lg;
  lg.addTo(map);
}}

function drawBaselineRoutes() {{
  const bdata = BASELINE[currentMH];
  if (!bdata) return;
  if (baselineLayer) {{ map.removeLayer(baselineLayer); }}
  const lg = L.layerGroup();
  const c  = BASELINE_CLR;

  bdata.routes.forEach(r => {{
    if (!r.coords || r.coords.length < 2) return;
    const ll    = r.coords.map(p => [p.lat, p.lng]);
    const isDed = r.type === 'FTL_Dedicated';
    const nTrk  = r.trucks || 1;

    L.polyline(ll, {{
      color     : c,
      weight    : isDed ? 5 : (nTrk > 1 ? 4 : 2.5),
      opacity   : 0.70,
      dashArray : isDed ? null : (nTrk > 1 ? '3,5' : '4,7'),
    }}).bindTooltip(`<div class="route-tip">
      <b style="color:${{c}}">${{currentMH}} · Current Routes</b> — ${{r.type}}<br>
      ${{r.seq}}<br>
      Vehicle ${{r.vehicle}}ft · ${{r.demand}} CFT avg · ${{fmt(r.cost)}}/month ·
      Freq=${{r.freq || 1}}${{nTrk > 1 ? ' · <b>2-truck split</b>' : ''}}
    </div>`, {{sticky:true}}).addTo(lg);

    r.coords.forEach((p,i) => {{
      if (i===0 || i===r.coords.length-1) return;
      L.circleMarker([p.lat,p.lng], {{
        radius:5, color:c, fillColor:'#f5e6d0', fillOpacity:.90, weight:2
      }}).bindTooltip(`<b>${{p.name}}</b><br>Current Routes`,
                      {{sticky:true}}).addTo(lg);
    }});
  }});

  baselineLayer = lg;
  lg.addTo(map);
}}

function updateTable() {{
  const tb = document.getElementById('summary-body');
  tb.innerHTML = '';

  const rows = [];

  if (baselineShown && BASELINE[currentMH]) {{
    const b = BASELINE[currentMH];
    rows.push({{
      isBaseline : true, label: 'Current Routes', mh: currentMH,
      reg: b.regular_trips, adh: b.adhoc_trips, apct: b.adhoc_pct,
      committed: b.committed_monthly, adhoc_cost: b.adhoc_monthly, total: b.total_monthly,
      isOpt: false,
    }});
  }}

  [...sel]
    .sort((a,b) => ALL[a].total_monthly - ALL[b].total_monthly)
    .forEach(k => {{
      const d = ALL[k];
      rows.push({{
        isBaseline: false, key: k, label: d.freeze_day, mh: d.mh,
        reg: d.regular_trips, adh: d.adhoc_trips, apct: d.adhoc_pct,
        committed: d.committed_monthly, adhoc_cost: d.adhoc_monthly, total: d.total_monthly,
        isOpt: k === OPT_PER_MH[d.mh],
      }});
    }});

  if (rows.length === 0) {{
    tb.innerHTML =
      '<tr><td colspan="8" style="color:#ccc;padding:8px 0">No days selected</td></tr>';
    return;
  }}

  rows.forEach(row => {{
    const ok  = row.apct <= LIM;
    const tr  = document.createElement('tr');
    if (row.isBaseline) tr.className = 'baseline-trow';
    else if (row.isOpt) tr.className = 'opt-row';

    const dot = row.isBaseline
      ? `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;
           background:${{BASELINE_CLR}};margin-right:4px;vertical-align:middle;"></span>`
      : `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;
           background:${{clr[row.key] || '#888'}};margin-right:4px;vertical-align:middle;"></span>`;

    let savStr = '';
    if (!row.isBaseline && BASELINE[currentMH]) {{
      const sav = BASELINE[currentMH].total_monthly - row.total;
      if (sav > 0) savStr = ` <span style="color:#8B6F47;font-size:10px">(saves ${{fmtK(sav)}})</span>`;
      else if (sav < 0) savStr = ` <span style="color:#A32D2D;font-size:10px">(${{fmtK(-sav)}} more)</span>`;
    }}

    tr.innerHTML = `
      <td>${{dot}}${{row.label}}${{savStr}}</td>
      <td style="color:#888">${{row.mh}}</td>
      <td>${{row.reg}}</td>
      <td>${{row.adh}}</td>
      <td style="color:${{ok?'#3B6D11':'#A32D2D'}};font-weight:500">${{row.apct}}%</td>
      <td>${{fmt(row.committed)}}</td>
      <td>${{fmt(row.adhoc_cost)}}</td>
      <td style="font-weight:600">${{fmt(row.total)}}</td>`;
    tb.appendChild(tr);
  }});
}}

buildDropdown();
if (currentMH) {{
  buildSidebar();
  const optKey = OPT_PER_MH[currentMH];
  if (optKey && ALL[optKey]) select(optKey);
  updateTable();
}}
</script>
</body>
</html>"""


def write_route_visualizer(
    per_mh_results: dict[str, Any],
    latlong: dict[str, tuple[float, float]],
    out_dir,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Builds route_data.json + Route_Visualizer.html in out_dir (port of colab
    Blocks 10/10.5/13). Call after run_agent4_freeze_day_pipeline, passing its
    result["data"]["per_mh_results"]."""
    from pathlib import Path
    import json as _json
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    viz_data = build_route_visualizer_data(per_mh_results, latlong)
    with open(out_dir / "route_data.json", "w", encoding="utf-8") as f:
        _json.dump(viz_data, f, ensure_ascii=False)

    adhoc_limit_pct = float(cfg.get("adhoc_pct_limit", 0.10)) * 100
    html = _build_visualizer_html(viz_data, adhoc_limit_pct)
    with open(out_dir / "Route_Visualizer.html", "w", encoding="utf-8") as f:
        f.write(html)

    return {
        "status": "ok",
        "data": {"route_data_json": out_dir / "route_data.json", "html_path": out_dir / "Route_Visualizer.html"},
        "issues": [],
    }


def _build_final_assignment_and_schedule_rows(
    candidate: dict[str, Any], mh_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(final_assignment_rows, expanded_schedule_rows) for one freeze-day
    candidate's frozen plan. Shared by the main pipeline (for the optimal
    candidate) and the single-day ad-hoc runner (Task 13)."""
    final_rows: list[dict[str, Any]] = []
    schedule_rows: list[dict[str, Any]] = []
    fa = candidate["result"].final_assignment_df
    for route_idx, row in fa.reset_index(drop=True).iterrows():
        final_rows.append({
            "MH": mh_name,
            "Route_ID": route_idx + 1,
            "Route_Type": row["Route_Type"],
            "Route_Sequence": row["route_sequence"],
            "Vehicle_Length": row["assigned_vehicle_length"],
            "Total_Demand": round(row.get("total_demand", 0), 2),
            "Monthly_Cost": round(row.get("monthly_cost", 0), 2),
            "Freq": row["Freq"],
            "Dist_km": round(row.get("dist", 0), 2),
            "Freeze_Day": candidate["freeze_day"],
        })
        stops = [s.strip() for s in row["route_sequence"].split("->")]
        for stop in stops:
            schedule_rows.append({
                "MH": mh_name,
                "Route_ID": route_idx + 1,
                "Route_Type": row["Route_Type"],
                "Location": stop,
                "Vehicle_Length": row["assigned_vehicle_length"],
                "Total_Demand": round(row.get("total_demand", 0), 2),
                "Monthly_Cost": round(row.get("monthly_cost", 0), 2),
                "Freq": row["Freq"],
                "Route_Sequence": row["route_sequence"],
                "Freeze_Day": candidate["freeze_day"],
            })
    return final_rows, schedule_rows


def run_agent4_freeze_day_pipeline(
    location_df: pd.DataFrame,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    mh_configs: dict[str, "a4.MHConfig"],
    out_dir,
    cfg: dict[str, Any],
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """location_df must already be the output of build_freeze_day_location_file
    (has D1..Dn/_cft columns, Current_MR/Current_Freq, allowed_positions,
    Freq_Allowed). Writes Location_File.csv, Freeze_Day_Comparison.csv,
    Final_Assignment.csv, Expanded_Schedule.csv, Baseline.csv,
    Baseline_vs_Optimal.csv, Network_Summary.csv to out_dir.

    Location_File.csv is always written here (not left in-memory only) so the
    exact location file used for this run is preserved alongside its outputs,
    scoped to this run's own output folder -- never Inputs\\ or a shared path.

    on_progress: optional callback receiving progress strings from every
    level -- per-MH, per-freeze-day-candidate, per-truck-upgrade-iteration,
    baseline summary -- matching legacy agent4.py's on_progress convention.
    """
    def _emit(msg: str) -> None:
        if callable(on_progress):
            on_progress(msg)

    from pathlib import Path
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    location_df.to_csv(out_dir / "Location_File.csv", index=False)

    loc_col = cfg["col_location_name"]
    mh_col = cfg["col_mh_assignment"]

    mh_names = [m for m in location_df[mh_col].dropna().astype(str).unique() if m in mh_configs]
    skipped_mhs = [m for m in location_df[mh_col].dropna().astype(str).unique() if m not in mh_configs]

    freeze_day_rows: list[dict[str, Any]] = []
    final_assignment_rows: list[dict[str, Any]] = []
    expanded_schedule_rows: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    network_summary_rows: list[dict[str, Any]] = []
    per_day_route_log_rows: list[dict[str, Any]] = []
    all_days_spillover_rows: list[dict[str, Any]] = []
    best_network_spillover_rows: list[dict[str, Any]] = []
    adhoc_summary_rows: list[dict[str, Any]] = []
    per_mh_results: dict[str, Any] = {}

    for mh_i, mh_name in enumerate(mh_names, start=1):
        mh_cfg = mh_configs[mh_name]
        dh_rows = location_df[location_df[mh_col].astype(str) == mh_name].copy()
        if dh_rows.empty:
            continue

        _emit(f"=== [{mh_i}/{len(mh_names)}] MH: {mh_name} ===")

        day_cols_all = sorted(
            (c for c in dh_rows.columns if c.startswith("D") and c[1:].isdigit() and f"{c}_cft" in dh_rows.columns),
            key=lambda c: int(c[1:]),
        )
        real_day_cols = day_cols_all[:-7] if len(day_cols_all) > 7 else day_cols_all

        baseline = build_baseline_for_mh(mh_name, mh_cfg, dh_rows, real_day_cols, dist_dict, latlong, cfg, on_progress=on_progress)
        freeze = run_single_mh_freeze_day(mh_name, mh_cfg, dh_rows, dist_dict, latlong, cfg, on_progress=on_progress)
        per_mh_results[mh_name] = {"baseline": baseline, "freeze": freeze}

        if freeze["status"] != "ok":
            continue

        optimal_day = freeze["best"]["freeze_day"]
        for r in freeze["results"]:
            freeze_day_rows.append({
                "MH": mh_name,
                "freeze_day": r["freeze_day"],
                "is_synthetic": r["is_synthetic"],
                "committed_monthly": round(r["committed_monthly"], 2),
                "adhoc_monthly": round(r["adhoc_monthly"], 2),
                "total_monthly": round(r["total_monthly"], 2),
                "n_adhoc_routes_total": r["n_adhoc_routes_total"],
                "regular_trips": r["regular_trips"],
                "adhoc_pct": round(r["adhoc_pct"] * 100, 1),
                "is_optimal": r["freeze_day"] == optimal_day,
            })
            per_day_route_log_rows.extend(_build_per_day_route_log_rows(r, mh_name, optimal_day))
            spill_rows = _build_spillover_detail_rows(r, mh_name)
            all_days_spillover_rows.extend(spill_rows)
            if r["freeze_day"] == optimal_day:
                best_network_spillover_rows.extend(spill_rows)

        best = freeze["best"]
        adhoc_summary_rows.extend(_build_adhoc_summary(best["spillover_detail"], mh_name, cfg, "Optimal"))
        adhoc_summary_rows.extend(_build_adhoc_summary(baseline["spillover_detail"], mh_name, cfg, "Baseline"))
        fa_rows_best, sched_rows_best = _build_final_assignment_and_schedule_rows(best, mh_name)
        final_assignment_rows.extend(fa_rows_best)
        expanded_schedule_rows.extend(sched_rows_best)

        for r in baseline["ded_routes"] + baseline["mr_routes"]:
            baseline_rows.append({
                "MH": mh_name,
                "Route_Type": r["route_type"],
                "Route_Sequence": r["route_sequence"],
                "Vehicle_Length": r["assigned_vehicle_length"],
                "Total_Demand": round(r.get("total_demand", 0), 2),
                "Monthly_Cost": round(r.get("monthly_cost", 0), 2),
                "Freq": r["Freq"],
                "Dist_km": round(r.get("dist", 0), 2),
                "Trucks_On_Route": r.get("trucks_on_route", 1),
            })

        bc, ba, bt = baseline["committed_monthly"], baseline["adhoc_monthly"], baseline["total_monthly"]
        oc, oa, ot = best["committed_monthly"], best["adhoc_monthly"], best["total_monthly"]
        savings = bt - ot
        comparison_rows.append({
            "MH": mh_name,
            "Optimal_Freeze_Day": best["freeze_day"],
            "Current_Committed": round(bc, 2), "Optimal_Committed": round(oc, 2),
            "Current_Adhoc": round(ba, 2), "Optimal_Adhoc": round(oa, 2),
            "Current_Total": round(bt, 2), "Optimal_Total": round(ot, 2),
            "Total_Savings": round(savings, 2),
            "Savings_Pct": round(savings / bt * 100, 1) if bt else 0.0,
        })

        network_summary_rows.append({
            "MH": mh_name,
            "n_dhs": len(dh_rows),
            "optimal_freeze_day": best["freeze_day"],
            "is_synthetic_optimal": best["is_synthetic"],
            "adhoc_pct": round(best["adhoc_pct"] * 100, 1),
            "committed_monthly": round(best["committed_monthly"], 2),
            "adhoc_monthly": round(best["adhoc_monthly"], 2),
            "total_monthly": round(best["total_monthly"], 2),
            "baseline_total_monthly": round(bt, 2),
            "savings_vs_baseline": round(savings, 2),
        })

    freeze_day_df = pd.DataFrame(freeze_day_rows)
    final_assignment_df = pd.DataFrame(final_assignment_rows)
    expanded_schedule_df = pd.DataFrame(expanded_schedule_rows)
    baseline_df = pd.DataFrame(baseline_rows)
    comparison_df = pd.DataFrame(comparison_rows)
    network_summary_df = pd.DataFrame(network_summary_rows)
    per_day_route_log_df = pd.DataFrame(per_day_route_log_rows)
    all_days_spillover_df = pd.DataFrame(all_days_spillover_rows)
    best_network_spillover_df = pd.DataFrame(best_network_spillover_rows)
    adhoc_summary_df = pd.DataFrame(adhoc_summary_rows)

    freeze_day_df.to_csv(out_dir / "Freeze_Day_Comparison.csv", index=False)
    final_assignment_df.to_csv(out_dir / "Final_Assignment.csv", index=False)
    expanded_schedule_df.to_csv(out_dir / "Expanded_Schedule.csv", index=False)
    baseline_df.to_csv(out_dir / "Baseline.csv", index=False)
    comparison_df.to_csv(out_dir / "Baseline_vs_Optimal.csv", index=False)
    network_summary_df.to_csv(out_dir / "Network_Summary.csv", index=False)
    per_day_route_log_df.to_csv(out_dir / "Per_Day_Route_Log.csv", index=False)
    all_days_spillover_df.to_csv(out_dir / "All_Days_Spillover.csv", index=False)
    best_network_spillover_df.to_csv(out_dir / "Best_Network_Spillover.csv", index=False)
    adhoc_summary_df.to_csv(out_dir / "Adhoc_Route_Summary.csv", index=False)

    return {
        "status": "ok" if not skipped_mhs else "partial",
        "issues": [{"type": "mh_not_in_rate_card", "detail": m} for m in skipped_mhs],
        "data": {
            "freeze_day_comparison": freeze_day_df,
            "final_assignment": final_assignment_df,
            "expanded_schedule": expanded_schedule_df,
            "baseline": baseline_df,
            "baseline_vs_optimal": comparison_df,
            "per_day_route_log": per_day_route_log_df,
            "all_days_spillover": all_days_spillover_df,
            "best_network_spillover": best_network_spillover_df,
            "adhoc_route_summary": adhoc_summary_df,
            "network_summary": network_summary_df,
            "per_mh_results": per_mh_results,
        },
    }
