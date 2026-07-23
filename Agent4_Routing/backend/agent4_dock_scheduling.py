"""
Agent 4 — dock scheduling + CX-cutoff + speed engine.

New, additive module. Given the FINAL selected freeze-day candidate's route
plan for one MH (post truck-upgrade-loop -- called once, not per candidate,
since dock scheduling never changes cost, only departure timing), computes
dock-feasible actual departure times (TMS) subject to a limited number of
physical docks, and the resulting "Actual D1%" / speed metric.

The objective is a weighted maximization of Top266 shipments captured onto a
truck AND delivered by the true D1% threshold (1800 min = 6AM D+1) -- trading
off lower-priority DHs' speed for higher-priority DHs' when dock contention
forces a choice. This is a real trade-off (not a strict priority-preservation
rule), so it is solved as an ILP, not a greedy priority-ordered heuristic.

Two distinct time concepts feed this, per the design discussion:
  - Route TMS (departure from MH) -- the physical scheduling decision.
  - CX (customer) cutoff = TMS - (3h multi-DH / 2h single-DH) - 1h processing.
    The CX cutoff is looked up against Load Profile.csv's cumulative
    order-placement fraction (reusing agent3.build_load_profile_interp) to
    determine what fraction of a DH's daily Top266 volume is actually
    captured onto that specific truck -- later TMS captures more volume,
    which is *why* routes prefer to depart as late as feasible.

Dock occupancy is a SEPARATE concept from the CX-cutoff buffer: a dock is
physically blocked from (TMS - loading_duration) to (TMS + transition_buffer),
where loading_duration = shipments_on_route / dock_productivity_per_hour.

Does not touch route generation, ILP set-cover, or cost at all -- this is a
pure post-processing layer on top of an already-selected route plan.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd
from pulp import LpBinary, LpMaximize, LpProblem, LpVariable, lpSum, PULP_CBC_CMD

import agent4 as a4
import agent4_freeze_day as fd


def _route_stop_offsets(
    route_sequence: str,
    attr: dict[str, dict[str, Any]],
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    service_time_min: float,
) -> dict[str, float]:
    """Per-DH arrival-time offset from the route's own departure (TMS),
    simulated once so the whole chain can be treated as shifting rigidly
    with TMS. Approximation: assumes preponing TMS doesn't newly trigger a
    time_window_start wait at an intermediate stop that wasn't already
    happening at the route's natural TMS -- reasonable since preponing only
    makes such waits MORE likely, never less, and a route that's already
    past every DH's tw_start at its natural (late) TMS will almost always
    stay past it even preponed by the kind of margins dock conflicts require."""
    stops = [s.strip() for s in route_sequence.split("->")]
    dh_stops = stops[1:-1]
    depot = stops[0]
    offsets: dict[str, float] = {}
    cur_t = 0.0
    prev = depot
    for dh in dh_stops:
        km = a4.get_distance(prev, dh, dist_dict, latlong)
        km = km if km is not None else 0.0
        arr = cur_t + a4.get_transit_time(km)
        offsets[dh] = arr
        tw_start = attr.get(dh, {}).get("time_window_start", 720.0)
        dep = max(arr, tw_start) + service_time_min
        cur_t = dep
        prev = dh
    return offsets


def _cx_cutoff_hour(t_minutes: float, is_multi_dh: bool, cfg: dict[str, Any]) -> Optional[float]:
    """CX cutoff = TMS - (3h multi-DH / 2h single-DH) - 1h processing.

    Returns an hour-of-day (0-24) for the load-profile lookup (the profile is
    a repeating daily pattern), EXCEPT when the cutoff has pushed a full day
    or more past the reference midnight (cutoff_minutes >= 1440) -- in that
    case returns None, signalling 100% capture. D1 means "delivered by the
    day after the customer ordered"; if a full day-0 cycle has elapsed by the
    cutoff, that day's order volume is certainly fully placed -- it should
    NOT be treated as a fresh, barely-started day-1 cycle (wrapping via a
    plain modulo would incorrectly reset capture to near-zero right after
    midnight, which flips the economics: a route legitimately departing very
    late overnight would look artificially empty instead of fully loaded)."""
    buffer_hours = (
        cfg.get("cx_cutoff_multi_dh_hours", 3) if is_multi_dh else cfg.get("cx_cutoff_single_dh_hours", 2)
    ) + cfg.get("cx_cutoff_processing_hours", 1)
    cutoff_minutes = t_minutes - buffer_hours * 60.0
    # Clamp, don't wrap, on both sides -- a modulo would create the mirror-image
    # artifact on the early side (preponing far enough to go negative would
    # wrap to "late previous day" and get an undeserved high-capture bonus,
    # exactly backwards: pushed that early, almost nothing has been ordered
    # yet). >=1440 is the one deliberate exception (100%, per the D1 rule
    # above); <0 has no such exception -- it's simply too early.
    if cutoff_minutes >= 1440.0:
        return None
    if cutoff_minutes < 0.0:
        return 0.0
    return cutoff_minutes / 60.0


def schedule_docks_and_compute_speed(
    final_assignment_df: pd.DataFrame,
    dh_rows: pd.DataFrame,
    mh_name: str,
    mh_cfg: "a4.MHConfig",
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    load_profile_interp,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    final_assignment_df: the OPTIMAL freeze-day candidate's routes (milkrun +
        FTL_Dedicated) for this MH, post truck-upgrade-loop.
    dh_rows: this MH's slice of the freeze-day location file (needs
        top266_shipments, total_shipments, d1_true_threshold, ML, time
        windows -- all already present from build_freeze_day_location_file).
    load_profile_interp: the callable from a3.build_load_profile_interp(...)
        (reused directly, not re-implemented).

    Returns {"status": "ok"|"failed", "data": {...}, "issues": [...]}.
    data (on success): {
        "schedule_df": final_assignment_df + TMS/Placement_Time columns,
        "route_speed_df": one row per route (CX cutoff, capture fraction, speed%),
        "dh_speed_df": one row per DH (arrival, D1 pass/fail, weighted contribution),
        "mh_speed_pct": float,
        "n_docks_committed": int,
    }
    """
    loc_col = cfg["col_location_name"]
    attr = fd._attr_from_dh_rows(dh_rows, cfg)
    top266_map = dict(zip(dh_rows[loc_col].astype(str), dh_rows[cfg["col_top266_load"]]))
    d1_true_map = dict(zip(dh_rows[loc_col].astype(str), dh_rows["d1_true_threshold"]))
    shipments_map = dict(zip(dh_rows[loc_col].astype(str), dh_rows["total_shipments"]))

    dock_productivity = float(cfg.get("dock_productivity_per_hour", 100))
    transition_buffer = float(cfg.get("dock_transition_buffer_min", 30))
    granularity = max(float(cfg.get("dock_time_granularity_min", 10)), 1.0)
    search_window_hours = float(cfg.get("dock_search_window_hours", 18))
    adhoc_reserve_pct = float(cfg.get("adhoc_dock_reserve_pct", 0.05))
    n_docks_total = mh_cfg.n_docks
    n_docks_committed = max(1, n_docks_total - round(n_docks_total * adhoc_reserve_pct))

    routes = final_assignment_df.reset_index(drop=True)
    if routes.empty:
        return {"status": "ok", "issues": [], "data": {
            "schedule_df": routes.copy(), "route_speed_df": pd.DataFrame(),
            "dh_speed_df": pd.DataFrame(), "mh_speed_pct": None,
            "n_docks_committed": n_docks_committed,
        }}

    route_info: list[dict[str, Any]] = []
    for idx, row in routes.iterrows():
        hubs = list(row["hubs"])
        is_multi = len(hubs) > 1
        tms0 = fd._compute_shifted_mh_dep(row["route_sequence"], attr, dist_dict, latlong, mh_cfg.service_time_min)
        offsets = _route_stop_offsets(row["route_sequence"], attr, dist_dict, latlong, mh_cfg.service_time_min)
        shipments = sum(shipments_map.get(dh, 0) for dh in hubs)
        duration = (shipments / dock_productivity) * 60.0 if dock_productivity > 0 else 0.0
        floor = tms0 - search_window_hours * 60.0
        n_steps = int((tms0 - floor) / granularity) + 1
        candidates = [tms0 - k * granularity for k in range(n_steps)]
        route_info.append({
            "idx": idx, "hubs": hubs, "is_multi": is_multi, "tms0": tms0,
            "offsets": offsets, "duration": duration, "candidates": candidates,
        })

    # Precompute speed_value[t] per route/candidate -- weighted Top266
    # shipments captured AND delivered by the true D1% threshold.
    for r in route_info:
        r["speed_value"] = {}
        for t in r["candidates"]:
            cutoff_hour = _cx_cutoff_hour(t, r["is_multi"], cfg)
            capture_fraction = 1.0 if cutoff_hour is None else load_profile_interp(cutoff_hour)
            val = 0.0
            for dh in r["hubs"]:
                arrival = t + r["offsets"].get(dh, 0.0)
                if arrival <= d1_true_map.get(dh, cfg["d1_true_threshold_min"]):
                    val += top266_map.get(dh, 0.0) * capture_fraction
            r["speed_value"][t] = val

    prob = LpProblem(f"DockSchedule_{mh_name.replace('/', '_').replace(' ', '_')}", LpMaximize)
    x: dict[tuple[int, float], Any] = {}
    for r in route_info:
        for t in r["candidates"]:
            x[(r["idx"], t)] = LpVariable(f"x_{r['idx']}_{round(t, 2)}".replace(".", "_").replace("-", "n"), cat=LpBinary)

    # Tiny tie-break favoring later TMS among objective-tied candidates
    # (matches "push as late as feasible" everywhere else in this engine --
    # e.g. capture_fraction plateaus at 1.0 across a whole range once the
    # cutoff crosses past-midnight, so without this the solver may pick any
    # tied-optimal time, not necessarily the latest one). Scaled small enough
    # to never override the primary speed objective.
    tie_break_eps = 1e-3
    prob += (
        lpSum(x[(r["idx"], t)] * r["speed_value"][t] for r in route_info for t in r["candidates"])
        + tie_break_eps * lpSum(x[(r["idx"], t)] * t for r in route_info for t in r["candidates"])
    )

    for r in route_info:
        prob += lpSum(x[(r["idx"], t)] for t in r["candidates"]) == 1

    # Dock capacity: at every grid point, at most n_docks_committed routes'
    # occupancy windows [t - duration, t + transition_buffer] may be active.
    grid_start = min(min(r["candidates"]) for r in route_info) - transition_buffer
    grid_end = max(r["tms0"] for r in route_info) + transition_buffer
    n_grid = int((grid_end - grid_start) / granularity) + 1
    grid_points = [grid_start + k * granularity for k in range(n_grid)]

    for tau in grid_points:
        covering = []
        for r in route_info:
            for t in r["candidates"]:
                window_start = t - r["duration"]
                window_end = t + transition_buffer
                if window_start <= tau <= window_end:
                    covering.append(x[(r["idx"], t)])
        if covering:
            prob += lpSum(covering) <= n_docks_committed

    prob.solve(PULP_CBC_CMD(msg=0, gapRel=0))

    if prob.status != 1:
        return {
            "status": "failed",
            "issues": [{
                "type": "dock_schedule_infeasible",
                "detail": f"{mh_name}: could not schedule all {len(route_info)} routes within "
                          f"{n_docks_committed} committed docks even preponed up to {search_window_hours}h. "
                          f"Needs more docks, fewer/shorter routes, or a wider search window.",
            }],
            "data": None,
        }

    schedule_rows, dh_speed_rows, route_speed_rows = [], [], []
    mh_weighted_num = mh_weighted_den = 0.0

    for r in route_info:
        chosen_t = next(t for t in r["candidates"] if x[(r["idx"], t)].varValue and x[(r["idx"], t)].varValue > 0.5)
        row = routes.loc[r["idx"]].to_dict()
        row["TMS"] = round(chosen_t, 2)
        row["Placement_Time"] = round(chosen_t - r["duration"], 2)
        schedule_rows.append(row)

        cutoff_hour = _cx_cutoff_hour(chosen_t, r["is_multi"], cfg)
        capture_fraction = 1.0 if cutoff_hour is None else load_profile_interp(cutoff_hour)
        route_top266_total = sum(top266_map.get(dh, 0.0) for dh in r["hubs"])
        route_speed_value = r["speed_value"][chosen_t]
        route_speed_rows.append({
            "MH": mh_name, "Route_ID": r["idx"] + 1, "TMS": round(chosen_t, 2),
            "Ideal_TMS": round(r["tms0"], 2),
            "CX_Cutoff_Hour": round(cutoff_hour, 2) if cutoff_hour is not None else "past-midnight (100%)",
            "Capture_Fraction": round(capture_fraction, 3), "Top266_Total": route_top266_total,
            "Top266_Speed_Value": round(route_speed_value, 2),
            "Route_Speed_Pct": round(route_speed_value / route_top266_total * 100, 1) if route_top266_total else None,
        })

        for dh in r["hubs"]:
            arrival = chosen_t + r["offsets"].get(dh, 0.0)
            threshold = d1_true_map.get(dh, cfg["d1_true_threshold_min"])
            passed = arrival <= threshold
            top266 = top266_map.get(dh, 0.0)
            dh_speed_rows.append({
                "MH": mh_name, "destination_hub_key": dh, "Route_ID": r["idx"] + 1,
                "Arrival_Time": round(arrival, 2), "D1_True_Threshold": threshold,
                "D1_Achieved": passed, "Top266_Shipments": top266,
                "Capture_Fraction": round(capture_fraction, 3) if passed else 0.0,
            })
            mh_weighted_num += top266 * (capture_fraction if passed else 0.0)
            mh_weighted_den += top266

    mh_speed_pct = round(mh_weighted_num / mh_weighted_den * 100, 1) if mh_weighted_den else None

    return {
        "status": "ok",
        "issues": [],
        "data": {
            "schedule_df": pd.DataFrame(schedule_rows),
            "route_speed_df": pd.DataFrame(route_speed_rows),
            "dh_speed_df": pd.DataFrame(dh_speed_rows),
            "mh_speed_pct": mh_speed_pct,
            "n_docks_committed": n_docks_committed,
        },
    }


def run_dock_scheduling_for_all_mhs(
    per_mh_results: dict[str, Any],
    location_df: pd.DataFrame,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    mh_configs: dict[str, "a4.MHConfig"],
    load_profile_interp,
    cfg: dict[str, Any],
    out_dir,
) -> dict[str, Any]:
    """Post-processing step, called separately after run_agent4_freeze_day_pipeline
    (same pattern as write_route_visualizer) -- avoids a circular import, since
    this module already imports agent4_freeze_day for its helpers.

    per_mh_results: from run_agent4_freeze_day_pipeline's result["data"]["per_mh_results"].
    location_df: the same freeze-day location file passed to the pipeline.
    load_profile_interp: from agent3.build_load_profile_interp(load_profile_df)["data"].

    Writes Dock_Schedule.csv, Route_Speed.csv, DH_Speed.csv, Speed_Summary.csv to out_dir.
    """
    from pathlib import Path
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    mh_col = cfg["col_mh_assignment"]
    schedule_rows, route_speed_rows, dh_speed_rows, speed_summary_rows = [], [], [], []
    issues: list[dict[str, Any]] = []

    for mh_name, mh_data in per_mh_results.items():
        freeze = mh_data.get("freeze")
        if not freeze or freeze.get("status") != "ok":
            continue
        mh_cfg = mh_configs.get(mh_name)
        if mh_cfg is None:
            continue

        dh_rows = location_df[location_df[mh_col].astype(str) == mh_name].copy()
        best = freeze["best"]
        fa = best["result"].final_assignment_df

        result = schedule_docks_and_compute_speed(
            fa, dh_rows, mh_name, mh_cfg, dist_dict, latlong, load_profile_interp, cfg
        )
        if result["status"] != "ok":
            issues.extend(result["issues"])
            continue

        d = result["data"]
        schedule_rows.append(d["schedule_df"])
        route_speed_rows.append(d["route_speed_df"])
        dh_speed_rows.append(d["dh_speed_df"])
        speed_summary_rows.append({
            "MH": mh_name,
            "n_docks_total": mh_cfg.n_docks,
            "n_docks_committed": d["n_docks_committed"],
            "n_routes": len(fa),
            "mh_speed_pct": d["mh_speed_pct"],
        })

    schedule_df = pd.concat(schedule_rows, ignore_index=True) if schedule_rows else pd.DataFrame()
    route_speed_df = pd.concat(route_speed_rows, ignore_index=True) if route_speed_rows else pd.DataFrame()
    dh_speed_df = pd.concat(dh_speed_rows, ignore_index=True) if dh_speed_rows else pd.DataFrame()
    speed_summary_df = pd.DataFrame(speed_summary_rows)

    schedule_df.to_csv(out_dir / "Dock_Schedule.csv", index=False)
    route_speed_df.to_csv(out_dir / "Route_Speed.csv", index=False)
    dh_speed_df.to_csv(out_dir / "DH_Speed.csv", index=False)
    speed_summary_df.to_csv(out_dir / "Speed_Summary.csv", index=False)

    return {
        "status": "partial" if issues else "ok",
        "issues": issues,
        "data": {
            "schedule_df": schedule_df,
            "route_speed_df": route_speed_df,
            "dh_speed_df": dh_speed_df,
            "speed_summary_df": speed_summary_df,
        },
    }
