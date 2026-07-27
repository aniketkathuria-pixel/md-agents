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

Two fixes (2026-07-27), both flowing from the same root cause -- see
PLAYBOOK.md for the full write-up:

1. A DH split "FTL + Milkrun" (bulk demand on a dedicated truck, residual on
   a shared route) now gets ONE cutoff, not two independent ones -- the FTL
   route is synced to its Milkrun route's chosen departure time rather than
   scheduled as an unrelated truck (see `linked_ftl` in
   schedule_docks_and_compute_speed).

2. The whole model now treats time as a repeating 24h cycle, not an
   unbounded line. This network runs the SAME schedule every single day --
   a "TMS" is a recurring daily clock value, not a one-time absolute instant
   days out. Two symptoms of the old (wrong) linear framing are fixed by
   this reframing together, not separately:
     - A route made up entirely of low-Top266 (rollover-relaxed) DHs used to
       drift its "ideal" TMS out past 24h (sometimes 48h+), since nothing
       constrained it and the objective had no Top266 volume to score there.
       The anchor is now computed against each DH's TRUE, un-relaxed deadline
       (d1_true_threshold) and folded into a real single-day clock value via
       modulo -- see `true_attr` below.
     - Two routes near opposite sides of midnight (e.g. 23:00 and 01:00)
       used to be invisible to each other's dock-capacity check, because an
       unbounded linear timeline doesn't know they're only ~2 real clock
       hours apart on a schedule that repeats forever. Dock-capacity windows
       are now reduced circularly (`_circular_windows`) against one fixed
       daily grid, so a window straddling midnight is correctly checked
       against whatever sits right after it.
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


_DAY_MIN = 1440.0   # one repeating operational day, in minutes


def _cx_cutoff_hour(t_minutes: float, is_multi_dh: bool, cfg: dict[str, Any]) -> float:
    """CX cutoff = TMS - (3h multi-DH / 2h single-DH) - 1h processing, reduced
    to an hour-of-day (0-24) for the load-profile lookup.

    t_minutes is always a genuine daily-clock value in [0, _DAY_MIN) now (see
    schedule_docks_and_compute_speed) -- the truck leaves at the SAME clock
    time every single day, forever, so there is no "which calendar day"
    ambiguity to special-case anymore. A cutoff that lands before midnight
    (e.g. a truck leaving at 00:30 with a 4h buffer -> cutoff -3:30) simply
    falls on the previous evening; Load Profile.csv is itself one repeating
    daily curve, so a plain modulo is the correct, exact reduction here, not
    an approximation -- the old clamp-at-both-ends logic was a symptom of
    treating time as an unbounded line instead of a repeating cycle (see
    PLAYBOOK.md's "daily-cycle dock scheduling" entry)."""
    buffer_hours = (
        cfg.get("cx_cutoff_multi_dh_hours", 3) if is_multi_dh else cfg.get("cx_cutoff_single_dh_hours", 2)
    ) + cfg.get("cx_cutoff_processing_hours", 1)
    cutoff_minutes = (t_minutes - buffer_hours * 60.0) % _DAY_MIN
    return cutoff_minutes / 60.0


def _circular_windows(start: float, end: float, period: float = _DAY_MIN) -> list[tuple[float, float]]:
    """Reduce an occupancy window [start, end) into 1-2 sub-intervals inside
    [0, period), wrapping at midnight. The dock schedule repeats every single
    day, so a window like 23:40-00:20 genuinely straddles the SAME recurring
    dock slot on two consecutive calendar days -- both halves must land in
    [0, period) for a fixed one-day grid to see them as adjacent to whatever
    sits right after midnight, which is the whole point of this fix."""
    length = end - start
    if length >= period:
        return [(0.0, period)]   # occupies the entire day -- shouldn't normally happen
    a = start % period
    b = a + length
    if b <= period:
        return [(a, b)]
    return [(a, period), (0.0, b - period)]


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

    # Anchor the dock-scheduling "ideal" departure against each DH's TRUE,
    # un-relaxed deadline (d1_true_threshold), never the rollover-relaxed
    # time_window_end. Rollover exists to let ROUTE GENERATION (agent4.py)
    # compose a route with a later TMS by sacrificing a low-priority DH's own
    # same-day D1% -- it was never meant to give the physical TRUCK license
    # to depart a calendar day late. d1_true_threshold is uniformly ~1800 min
    # (the genuine "arrive by 6AM D+1" ceiling) for every DH regardless of
    # rollover, so anchoring against it here keeps the legitimate up-to-30h
    # overnight allowance while removing the +1440 rollover inflation that
    # was making some routes' "ideal" TMS balloon to 3,000+ minutes.
    true_attr = {dh: {**a, "time_window_end": d1_true_map.get(dh, a.get("time_window_end"))}
                 for dh, a in attr.items()}

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

    # DH -> its Milkrun route index, if any. Used to link a DH's dedicated
    # FTL truck(s) to the SAME departure as its Milkrun route (a DH split
    # "FTL + Milkrun" is one customer cutoff, not two independent ones).
    dh_to_mr_idx: dict[str, int] = {}
    for idx, row in routes.iterrows():
        if row.get("Route_Type") == "Milkrun":
            for dh in row["hubs"]:
                dh_to_mr_idx[dh] = idx

    route_info: list[dict[str, Any]] = []
    linked_ftl: list[dict[str, Any]] = []   # FTL routes synced to a Milkrun anchor -- no own decision variable
    for idx, row in routes.iterrows():
        hubs = list(row["hubs"])
        is_ftl = row.get("Route_Type") != "Milkrun"

        if is_ftl and len(hubs) == 1 and hubs[0] in dh_to_mr_idx:
            dh = hubs[0]
            offsets = _route_stop_offsets(row["route_sequence"], attr, dist_dict, latlong, mh_cfg.service_time_min)
            shipments = shipments_map.get(dh, 0)
            duration = (shipments / dock_productivity) * 60.0 if dock_productivity > 0 else 0.0
            linked_ftl.append({
                "idx": idx, "hubs": hubs, "anchor_idx": dh_to_mr_idx[dh],
                "offsets": offsets, "duration": duration,
            })
            continue

        is_multi = len(hubs) > 1
        # Ideal TMS, anchored against TRUE deadlines, then folded into a
        # genuine single daily clock value -- the truck departs at the same
        # clock time every day forever, so there is no "day 2" for it to
        # legitimately drift into (Issue 2 fix).
        tms0_raw = fd._compute_shifted_mh_dep(row["route_sequence"], true_attr, dist_dict, latlong, mh_cfg.service_time_min)
        tms0 = tms0_raw % _DAY_MIN
        offsets = _route_stop_offsets(row["route_sequence"], attr, dist_dict, latlong, mh_cfg.service_time_min)
        shipments = sum(shipments_map.get(dh, 0) for dh in hubs)
        duration = (shipments / dock_productivity) * 60.0 if dock_productivity > 0 else 0.0

        # Candidate departure times, preponing from tms0 within the day --
        # capped at one full lap of the clock so modulo can't retread ground.
        # Track each candidate's prepone STEP k (not just its raw clock value)
        # so the tie-break below can prefer "closest to the natural ideal",
        # never "numerically largest t" -- those stop being the same thing
        # once preponing can wrap past midnight (a candidate reached by
        # preponing deep into "the previous evening" can have a large raw t,
        # e.g. 23:50, while still representing a big step away from ideal).
        max_steps = min(int(search_window_hours * 60.0 / granularity) + 1, int(_DAY_MIN / granularity))
        seen: set[float] = set()
        candidates: list[float] = []
        step_of: dict[float, int] = {}
        for k in range(max_steps):
            c = (tms0 - k * granularity) % _DAY_MIN
            key = round(c, 6)
            if key not in seen:
                seen.add(key)
                candidates.append(c)
                step_of[c] = k

        route_info.append({
            "idx": idx, "hubs": hubs, "is_multi": is_multi, "tms0": tms0,
            "offsets": offsets, "duration": duration, "candidates": candidates,
            "step_of": step_of,
        })

    # Precompute speed_value[t] per route/candidate -- weighted Top266
    # shipments captured AND delivered by the true D1% threshold.
    for r in route_info:
        r["speed_value"] = {}
        for t in r["candidates"]:
            cutoff_hour = _cx_cutoff_hour(t, r["is_multi"], cfg)
            capture_fraction = load_profile_interp(cutoff_hour)
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

    # Tiny tie-break favoring the SMALLEST prepone step among objective-tied
    # candidates -- i.e. stay as close to the natural (ideal) TMS as
    # possible, only preponing as far as dock contention actually forces
    # (matches "push as late as feasible" everywhere else in this engine).
    # This must be step k, not raw t: once preponing can wrap past midnight,
    # a deep prepone can land on a numerically LARGE t (e.g. preponing 5.8h
    # from 05:40 wraps to 23:50), so "prefer larger t" and "prefer less
    # preponing" stop being the same thing -- rewarding raw t would silently
    # reintroduce a milder version of the very drift-away-from-ideal bug
    # this whole rework fixes. Scaled small enough to never override the
    # primary speed objective.
    tie_break_eps = 1e-3
    prob += (
        lpSum(x[(r["idx"], t)] * r["speed_value"][t] for r in route_info for t in r["candidates"])
        - tie_break_eps * lpSum(x[(r["idx"], t)] * r["step_of"][t] for r in route_info for t in r["candidates"])
    )

    for r in route_info:
        prob += lpSum(x[(r["idx"], t)] for t in r["candidates"]) == 1

    # Dock capacity: at every grid point (one fixed daily cycle, [0, 1440)),
    # at most n_docks_committed occupancy windows may be active. Windows are
    # reduced circularly (_circular_windows) so a window straddling midnight
    # is correctly seen as adjacent to whatever sits right after it, instead
    # of being invisible to a route on the "other side" of midnight (Issue 2).
    # Linked FTL routes contribute their OWN occupancy window at their
    # anchor Milkrun route's chosen time -- reusing that same x variable
    # (never a new one), so choosing a time for the Milkrun route correctly
    # consumes 2 physical docks at once if both windows overlap a grid point.
    route_by_idx = {r["idx"]: r for r in route_info}
    n_grid = int(_DAY_MIN / granularity)
    grid_points = [k * granularity for k in range(n_grid)]

    for tau in grid_points:
        covering = []
        for r in route_info:
            for t in r["candidates"]:
                window_start = t - r["duration"]
                window_end = t + transition_buffer
                for a, b in _circular_windows(window_start, window_end):
                    if a <= tau < b:
                        covering.append(x[(r["idx"], t)])
                        break
        for lf in linked_ftl:
            anchor = route_by_idx[lf["anchor_idx"]]
            for t in anchor["candidates"]:
                window_start = t - lf["duration"]
                window_end = t + transition_buffer
                for a, b in _circular_windows(window_start, window_end):
                    if a <= tau < b:
                        covering.append(x[(anchor["idx"], t)])
                        break
        if covering:
            prob += lpSum(covering) <= n_docks_committed

    prob.solve(PULP_CBC_CMD(msg=0, gapRel=0))

    if prob.status != 1:
        return {
            "status": "failed",
            "issues": [{
                "type": "dock_schedule_infeasible",
                "detail": f"{mh_name}: could not schedule all {len(route_info)} routes (+{len(linked_ftl)} "
                          f"FTL synced to a Milkrun DH) within {n_docks_committed} committed docks even "
                          f"preponed up to {search_window_hours}h within the day. "
                          f"Needs more docks, fewer/shorter routes, or a wider search window.",
            }],
            "data": None,
        }

    schedule_rows, dh_speed_rows, route_speed_rows = [], [], []
    mh_weighted_num = mh_weighted_den = 0.0
    anchor_chosen_t: dict[int, float] = {}

    for r in route_info:
        chosen_t = next(t for t in r["candidates"] if x[(r["idx"], t)].varValue and x[(r["idx"], t)].varValue > 0.5)
        anchor_chosen_t[r["idx"]] = chosen_t
        row = routes.loc[r["idx"]].to_dict()
        row["TMS"] = round(chosen_t, 2)
        row["Placement_Time"] = round(chosen_t - r["duration"], 2)
        schedule_rows.append(row)

        cutoff_hour = _cx_cutoff_hour(chosen_t, r["is_multi"], cfg)
        capture_fraction = load_profile_interp(cutoff_hour)
        route_top266_total = sum(top266_map.get(dh, 0.0) for dh in r["hubs"])
        route_speed_value = r["speed_value"][chosen_t]
        route_speed_rows.append({
            "MH": mh_name, "Route_ID": r["idx"] + 1, "TMS": round(chosen_t, 2),
            "Ideal_TMS": round(r["tms0"], 2),
            "CX_Cutoff_Hour": round(cutoff_hour, 2),
            "Capture_Fraction": round(capture_fraction, 3), "Top266_Total": route_top266_total,
            "Top266_Speed_Value": round(route_speed_value, 2),
            "Route_Speed_Pct": round(route_speed_value / route_top266_total * 100, 1) if route_top266_total else None,
            "Synced_To_Route_ID": None,
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

    # Linked FTL routes: reported for visibility (Dock_Schedule.csv/
    # Route_Speed.csv), synced to their anchor Milkrun route's chosen time
    # (Issue 1 fix). Deliberately NOT added to dh_speed_rows/mh_weighted_*:
    # their DH's Top266 volume is already scored once via the anchor
    # Milkrun route above -- adding it again here would double-count the
    # same DH's speed contribution across two trucks that now share one
    # cutoff by design.
    for lf in linked_ftl:
        chosen_t = anchor_chosen_t[lf["anchor_idx"]]
        row = routes.loc[lf["idx"]].to_dict()
        row["TMS"] = round(chosen_t, 2)
        row["Placement_Time"] = round(chosen_t - lf["duration"], 2)
        schedule_rows.append(row)

        cutoff_hour = _cx_cutoff_hour(chosen_t, False, cfg)   # FTL dedicated = single-DH cutoff
        capture_fraction = load_profile_interp(cutoff_hour)
        route_speed_rows.append({
            "MH": mh_name, "Route_ID": lf["idx"] + 1, "TMS": round(chosen_t, 2),
            "Ideal_TMS": round(chosen_t, 2),   # no independent ideal -- always synced to the anchor
            "CX_Cutoff_Hour": round(cutoff_hour, 2),
            "Capture_Fraction": round(capture_fraction, 3), "Top266_Total": 0.0,
            "Top266_Speed_Value": 0.0, "Route_Speed_Pct": None,
            "Synced_To_Route_ID": lf["anchor_idx"] + 1,
        })

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


# ---------------------------------------------------------------------------
# Dock utilization visualizer -- a per-MH timeline (Gantt-style) chart showing
# how many docks exist, how many are committed vs reserved for ad-hoc, and
# which route occupies which dock-slot over the day. Pure post-processing on
# top of schedule_docks_and_compute_speed's own output; does not touch the
# ILP, the schedule, or the speed metric.
# ---------------------------------------------------------------------------

_DOCK_VIZ_STATUS: dict[str, str] = {
    "good":     "#0ca30c",
    "warning":  "#fab219",
    "serious":  "#ec835a",
    "critical": "#d03b3b",
    "unmeasured": "#898781",
}


def _speed_status(pct: Optional[float]) -> tuple[str, str]:
    """Bucket a route's Route_Speed_Pct into a status key + human label.
    None (route carries no Top266 load, so nothing to measure) maps to a
    distinct neutral bucket -- never silently folded into "good"."""
    if pct is None:
        return "unmeasured", "No Top266 load on this route"
    if pct >= 90:
        return "good", "On-time (≥ 90%)"
    if pct >= 75:
        return "warning", "Mostly on-time (75–90%)"
    if pct >= 50:
        return "serious", "Degraded (50–75%)"
    return "critical", "Missed (< 50%)"


def _routes_circular_overlap(
    a: dict[str, Any], b: dict[str, Any], period: float = _DAY_MIN,
) -> bool:
    """True when two routes' dock-occupancy windows share any minute on the
    repeating daily clock. Uses the same _circular_windows reduction as the ILP."""
    for a0, a1 in _circular_windows(a["start"], a["end"], period):
        for b0, b1 in _circular_windows(b["start"], b["end"], period):
            if max(a0, b0) < min(a1, b1):
                return True
    return False


def _dock_viz_segments(start: float, end: float, period: float = _DAY_MIN) -> list[dict[str, float]]:
    """Reduce a raw Placement_Time→end window to 1-2 chart segments in [0, period)."""
    return [{"start": a, "end": b} for a, b in _circular_windows(start, end, period)]


def _assign_dock_rows(routes: list[dict[str, Any]]) -> dict[str, int]:
    """Greedy interval partitioning on the repeating daily clock -- assigns each
    route a 0-based display dock-row for the timeline chart.

    This is a VISUALIZATION construct only, not the ILP's own decision. Overlap
    is checked circularly (midnight wrap) so two routes that only clash across
    midnight are not placed on the same lane."""
    order = sorted(
        routes,
        key=lambda r: (min(s["start"] for s in r["segments"]), r["route_key"]),
    )
    rows: list[list[dict[str, Any]]] = []
    assignment: dict[str, int] = {}
    for r in order:
        placed = False
        for row_idx, row_routes in enumerate(rows):
            if not any(_routes_circular_overlap(r, other) for other in row_routes):
                row_routes.append(r)
                assignment[r["route_key"]] = row_idx
                placed = True
                break
        if not placed:
            assignment[r["route_key"]] = len(rows)
            rows.append([r])
    return assignment


def build_dock_utilization_data(
    schedule_df: pd.DataFrame,
    route_speed_df: pd.DataFrame,
    mh_configs: dict[str, "a4.MHConfig"],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Assembles the per-MH dock-occupancy timeline structure consumed by the
    visualizer HTML. Inputs are exactly what run_dock_scheduling_for_all_mhs
    already returns: result["data"]["schedule_df"] and ["route_speed_df"]
    (or the equivalent CSVs reloaded, with `hubs` parsed back from its string
    repr via ast.literal_eval -- to_csv stringifies list columns)."""
    transition_buffer = float(cfg.get("dock_transition_buffer_min", 30))
    adhoc_reserve_pct = float(cfg.get("adhoc_dock_reserve_pct", 0.05))

    mhs: dict[str, Any] = {}
    if schedule_df is None or schedule_df.empty:
        return {"mhs": mhs}

    rs_idx: dict[tuple[str, Any], pd.Series] = {}
    if route_speed_df is not None and not route_speed_df.empty:
        for _, r in route_speed_df.iterrows():
            rs_idx[(r["MH"], r["Route_ID"])] = r

    for mh_name, grp in schedule_df.groupby("MH"):
        mh_cfg = mh_configs.get(mh_name)
        n_total = mh_cfg.n_docks if mh_cfg is not None else None

        routes: list[dict[str, Any]] = []
        for _, row in grp.iterrows():
            route_id = row.get("Route_ID")
            tms = float(row["TMS"])
            placement = float(row["Placement_Time"])
            end = tms + transition_buffer
            rs = rs_idx.get((mh_name, route_id))

            speed_pct = (
                float(rs["Route_Speed_Pct"])
                if rs is not None and pd.notna(rs.get("Route_Speed_Pct")) else None
            )
            ideal_tms = float(rs["Ideal_TMS"]) if rs is not None and pd.notna(rs.get("Ideal_TMS")) else tms
            status_key, status_label = _speed_status(speed_pct)
            hubs = row.get("hubs") or []
            if isinstance(hubs, str):
                import ast
                try:
                    hubs = ast.literal_eval(hubs)
                except (ValueError, SyntaxError):
                    hubs = [hubs]

            segments = _dock_viz_segments(placement, end)
            routes.append({
                "route_key":  f"{mh_name}__{route_id}",
                "route_id":   int(route_id) if pd.notna(route_id) else None,
                "start": placement, "end": end, "tms": tms, "ideal_tms": ideal_tms,
                "ideal_tms_clock": ideal_tms % _DAY_MIN,
                "segments": segments,
                "wraps_midnight": len(segments) > 1,
                "shifted": abs(tms - ideal_tms) > 0.01,
                "route_type": row.get("Route_Type", "Milkrun"),
                "hubs": list(hubs), "n_stops": len(hubs),
                "sequence": row.get("route_sequence", ""),
                "vehicle_length": row.get("assigned_vehicle_length"),
                "monthly_cost": row.get("monthly_cost"),
                "freq": row.get("Freq", 1),
                "speed_pct": speed_pct,
                "status_key": status_key, "status_label": status_label,
                "top266_total": (
                    float(rs["Top266_Total"])
                    if rs is not None and pd.notna(rs.get("Top266_Total")) else None
                ),
                "cx_cutoff_hour": rs.get("CX_Cutoff_Hour") if rs is not None else None,
                "capture_fraction": (
                    float(rs["Capture_Fraction"])
                    if rs is not None and pd.notna(rs.get("Capture_Fraction")) else None
                ),
            })

        assignment = _assign_dock_rows(routes)
        n_used = (max(assignment.values()) + 1) if assignment else 0
        for r in routes:
            r["dock_row"] = assignment[r["route_key"]]

        n_committed = max(1, round(n_total - n_total * adhoc_reserve_pct)) if n_total else n_used
        n_reserved = (n_total - n_committed) if n_total else 0

        mhs[mh_name] = {
            "n_docks_total":     n_total,
            "n_docks_committed": n_committed,
            "n_docks_reserved":  n_reserved,
            "n_docks_used":      n_used,
            "n_routes":          len(routes),
            "n_shifted":         sum(1 for r in routes if r["shifted"]),
            "routes":            routes,
            "window_start":      0.0,
            "window_end":        _DAY_MIN,
        }

    return {"mhs": mhs}


def _build_dock_utilization_html(data: dict[str, Any]) -> str:
    """Self-contained HTML/CSS/JS timeline chart -- no external chart library,
    consistent with agent4_freeze_day.py's Route_Visualizer.html (which uses
    Leaflet only because it needs a real map; this needs none)."""
    import json as _json

    mh_data = data.get("mhs", {})
    mh_names = list(mh_data.keys())
    data_json = _json.dumps(mh_data, ensure_ascii=False)
    mh_names_json = _json.dumps(mh_names, ensure_ascii=False)
    status_json = _json.dumps(_DOCK_VIZ_STATUS, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Dock Utilization</title>
<style>
:root {{
  color-scheme: light;
  --surface-1:      #fcfcfb;
  --page:           #f9f9f7;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:     #898781;
  --grid:           #e1e0d9;
  --baseline:       #c3c2b7;
  --border:         rgba(11,11,11,0.10);
  --reserved-fill:  #eceae4;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --surface-1:      #1a1a19;
    --page:           #0d0d0d;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --grid:           #2c2c2a;
    --baseline:       #383835;
    --border:         rgba(255,255,255,0.10);
    --reserved-fill:  #26251f;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface-1:      #1a1a19;
  --page:           #0d0d0d;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #898781;
  --grid:           #2c2c2a;
  --baseline:       #383835;
  --border:         rgba(255,255,255,0.10);
  --reserved-fill:  #26251f;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;
  background:var(--page);color:var(--text-primary);height:100vh;overflow:hidden;
  display:flex;flex-direction:column}}
#topbar{{display:flex;align-items:center;gap:14px;padding:12px 18px;
  background:var(--surface-1);border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap}}
#topbar .title{{font-size:15px;font-weight:600}}
#topbar .subtitle{{font-size:11px;color:var(--text-muted)}}
.field{{display:flex;align-items:center;gap:6px;margin-left:auto}}
.field label{{font-size:10px;font-weight:500;letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted)}}
select{{font-size:12px;font-weight:500;border:1px solid var(--baseline);border-radius:6px;
  padding:5px 26px 5px 9px;background:var(--surface-1);color:var(--text-primary);cursor:pointer;
  outline:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='11' height='11' viewBox='0 0 24 24' fill='none' stroke='%23898781' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 8px center}}
button.tglbtn{{font-size:11px;padding:5px 11px;border:1px solid var(--baseline);border-radius:6px;
  background:var(--surface-1);color:var(--text-secondary);cursor:pointer}}
button.tglbtn.active{{background:var(--text-primary);color:var(--surface-1);border-color:var(--text-primary)}}
#stats{{display:flex;gap:22px;padding:10px 18px;background:var(--surface-1);border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap}}
.stat{{display:flex;flex-direction:column;gap:1px}}
.stat .v{{font-size:18px;font-weight:600}}
.stat .l{{font-size:10px;color:var(--text-muted);letter-spacing:.04em;text-transform:uppercase}}
#legend{{display:flex;gap:16px;padding:8px 18px;background:var(--surface-1);border-bottom:1px solid var(--border);
  flex-wrap:wrap;flex-shrink:0;font-size:11px;color:var(--text-secondary)}}
.leg-item{{display:flex;align-items:center;gap:5px}}
.leg-swatch{{width:11px;height:11px;border-radius:3px;flex-shrink:0}}
.leg-border{{width:16px;height:9px;border-radius:2px;flex-shrink:0;background:transparent}}
#body{{flex:1;overflow:auto;padding:18px}}
#chart-wrap{{position:relative}}
.dock-row-label{{position:absolute;left:0;width:96px;font-size:11px;color:var(--text-secondary);
  display:flex;align-items:center;height:30px}}
.reserved-label{{color:var(--text-muted);font-style:italic}}
#timeline{{position:relative;margin-left:104px;border-left:1px solid var(--baseline)}}
.dock-lane{{position:relative;height:30px;border-bottom:1px solid var(--grid)}}
.dock-lane.reserved{{background:var(--reserved-fill)}}
.route-bar{{position:absolute;top:3px;height:24px;border-radius:4px;cursor:pointer;
  display:flex;align-items:center;padding:0 6px;font-size:10.5px;font-weight:500;color:#fff;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.route-bar.milkrun{{border:2px dashed rgba(255,255,255,.65)}}
.route-bar.ftl{{border:2px solid rgba(255,255,255,.85)}}
.route-bar:hover{{outline:2px solid var(--text-primary);outline-offset:1px}}
.shift-mark{{position:absolute;top:-2px;width:2px;height:34px;background:var(--text-primary);opacity:.55}}
#axis{{position:relative;height:20px;margin-left:104px;margin-top:2px}}
.tick{{position:absolute;font-size:10px;color:var(--text-muted);transform:translateX(-50%)}}
#tooltip{{position:fixed;pointer-events:none;background:var(--surface-1);color:var(--text-primary);
  border:1px solid var(--border);border-radius:8px;padding:9px 11px;font-size:11.5px;line-height:1.6;
  box-shadow:0 4px 16px rgba(0,0,0,.18);display:none;z-index:50;max-width:280px}}
#tooltip b{{font-weight:600}}
#table-view{{display:none;padding:0 18px 18px}}
table{{width:100%;border-collapse:collapse;font-size:11.5px}}
th{{text-align:left;color:var(--text-muted);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--baseline);white-space:nowrap}}
td{{padding:5px 8px;border-bottom:1px solid var(--grid);white-space:nowrap;font-variant-numeric:tabular-nums}}
.status-pill{{display:inline-flex;align-items:center;gap:5px;font-size:10.5px}}
.status-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
</style>
</head>
<body>
<div id="topbar">
  <div>
    <div class="title">Dock Utilization</div>
    <div class="subtitle">Which route occupies which dock, and when — per Mother Hub.</div>
  </div>
  <div class="field">
    <label>Motherhub</label>
    <select id="mh-select" onchange="switchMH(this.value)"></select>
  </div>
  <button class="tglbtn" id="view-toggle" onclick="toggleView()">Table view</button>
</div>
<div id="stats"></div>
<div id="legend"></div>
<div id="body">
  <div id="chart-wrap">
    <div id="timeline"></div>
    <div id="axis"></div>
  </div>
</div>
<div id="table-view"><table><thead><tr>
  <th>Route</th><th>Type</th><th>Dock row</th><th>Stops</th><th>TMS</th><th>Ideal TMS</th>
  <th>Shifted</th><th>Vehicle</th><th>Freq</th><th>Speed%</th><th>Status</th><th>Monthly cost</th>
</tr></thead><tbody id="table-body"></tbody></table></div>
<div id="tooltip"></div>

<script>
const ALL      = {data_json};
const MH_NAMES = {mh_names_json};
const STATUS   = {status_json};
let currentMH  = MH_NAMES[0] || null;
let tableMode  = false;
const PX_PER_MIN = 0.9;
const ROW_H = 30;

function fmtClock(min) {{
  const dayOffset = Math.floor(min / 1440);
  const hod = min - dayOffset * 1440;
  const h = Math.floor(hod / 60), m = Math.round(hod % 60);
  let s = String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0');
  if (dayOffset !== 0) s += ' (D' + (dayOffset > 0 ? '+' : '') + dayOffset + ')';
  return s;
}}
function fmtCost(n) {{ return '₹' + (n/1000).toFixed(0) + 'K'; }}

function buildDropdown() {{
  const el = document.getElementById('mh-select');
  el.innerHTML = '';
  MH_NAMES.forEach(mh => {{
    const opt = document.createElement('option');
    opt.value = mh; opt.textContent = mh + ' (' + ALL[mh].n_routes + ' routes)';
    if (mh === currentMH) opt.selected = true;
    el.appendChild(opt);
  }});
}}

function buildStats() {{
  const d = ALL[currentMH];
  const el = document.getElementById('stats');
  const rows = [
    ['Docks (physical)', d.n_docks_total ?? '–'],
    ['Committed to routes', d.n_docks_committed],
    ['Reserved for ad-hoc', d.n_docks_reserved ?? '–'],
    ['Docks actually used', d.n_docks_used],
    ['Routes scheduled', d.n_routes],
    ['Routes dock-shifted', d.n_shifted],
  ];
  el.innerHTML = rows.map(([l,v]) => `<div class="stat"><div class="v">${{v}}</div><div class="l">${{l}}</div></div>`).join('');
}}

function buildLegend() {{
  const el = document.getElementById('legend');
  const statusLabels = {{good:'On-time (≥90%)', warning:'Mostly on-time', serious:'Degraded', critical:'Missed (<50%)', unmeasured:'No Top266 load'}};
  let html = Object.keys(statusLabels).map(k =>
    `<div class="leg-item"><div class="leg-swatch" style="background:${{STATUS[k]}}"></div>${{statusLabels[k]}}</div>`
  ).join('');
  html += `<div class="leg-item"><div class="leg-border" style="border:2px dashed var(--text-secondary)"></div>Milkrun</div>`;
  html += `<div class="leg-item"><div class="leg-border" style="border:2px solid var(--text-secondary)"></div>FTL dedicated</div>`;
  html += `<div class="leg-item"><div style="width:2px;height:11px;background:var(--text-primary);opacity:.55"></div>Dock-forced shift from ideal TMS</div>`;
  el.innerHTML = html;
}}

function buildChart() {{
  const d = ALL[currentMH];
  const timeline = document.getElementById('timeline');
  const axis = document.getElementById('axis');
  timeline.innerHTML = ''; axis.innerHTML = '';

  const nRows = Math.max(d.n_docks_committed, d.n_docks_used, 1);
  const nReservedRows = d.n_docks_reserved || 0;
  const totalRows = nRows + nReservedRows;
  const start = d.window_start;
  const span = d.window_end - d.window_start;
  const chartW = Math.max(600, span * PX_PER_MIN + 40);
  timeline.style.width = chartW + 'px';
  timeline.style.height = (totalRows * ROW_H) + 'px';
  axis.style.width = chartW + 'px';

  for (let i = 0; i < totalRows; i++) {{
    const lane = document.createElement('div');
    lane.className = 'dock-lane' + (i >= nRows ? ' reserved' : '');
    lane.style.top = (i * ROW_H) + 'px';
    lane.style.position = 'absolute'; lane.style.left = '0'; lane.style.right = '0';
    timeline.appendChild(lane);

    const label = document.createElement('div');
    label.className = 'dock-row-label' + (i >= nRows ? ' reserved-label' : '');
    label.style.top = (i * ROW_H) + 'px';
    label.textContent = i >= nRows ? ('Reserved ' + (i - nRows + 1)) : ('Dock ' + (i + 1));
    label.style.position = 'absolute';
    document.getElementById('chart-wrap').appendChild(label);
  }}

  d.routes.forEach(r => {{
    const segs = r.segments && r.segments.length ? r.segments : [{{start: r.start, end: r.end}}];
    segs.forEach((seg, segIdx) => {{
      const bar = document.createElement('div');
      bar.className = 'route-bar ' + (r.route_type === 'Milkrun' ? 'milkrun' : 'ftl');
      bar.style.left = ((seg.start - start) * PX_PER_MIN) + 'px';
      bar.style.width = Math.max(6, (seg.end - seg.start) * PX_PER_MIN) + 'px';
      bar.style.top = (r.dock_row * ROW_H + 3) + 'px';
      bar.style.background = STATUS[r.status_key];
      const wrapHint = r.wraps_midnight && segs.length > 1 ? ' · wraps midnight' : '';
      bar.textContent = 'R' + r.route_id + ' · ' + r.n_stops + ' stop' + (r.n_stops === 1 ? '' : 's') + wrapHint;
      bar.addEventListener('mouseenter', e => showTip(e, r));
      bar.addEventListener('mousemove', e => moveTip(e));
      bar.addEventListener('mouseleave', hideTip);
      timeline.appendChild(bar);
    }});

    if (r.shifted) {{
      const mark = document.createElement('div');
      mark.className = 'shift-mark';
      const idealClock = r.ideal_tms_clock != null ? r.ideal_tms_clock : (r.ideal_tms % 1440);
      mark.style.left = ((idealClock - start) * PX_PER_MIN) + 'px';
      mark.style.top = (r.dock_row * ROW_H) + 'px';
      timeline.appendChild(mark);
    }}
  }});

  const tickStep = 120;
  for (let t = start; t <= d.window_end; t += tickStep) {{
    const tick = document.createElement('div');
    tick.className = 'tick';
    tick.style.left = ((t - start) * PX_PER_MIN) + 'px';
    tick.textContent = fmtClock(t);
    axis.appendChild(tick);
  }}
}}

function buildTable() {{
  const d = ALL[currentMH];
  const body = document.getElementById('table-body');
  body.innerHTML = d.routes
    .slice().sort((a,b) => a.dock_row - b.dock_row || a.start - b.start)
    .map(r => `<tr>
      <td>R${{r.route_id}}</td>
      <td>${{r.route_type}}</td>
      <td>Dock ${{r.dock_row + 1}}</td>
      <td>${{r.n_stops}}</td>
      <td>${{fmtClock(r.tms)}}</td>
      <td>${{fmtClock(r.ideal_tms)}}</td>
      <td>${{r.shifted ? (Math.round(r.ideal_tms - r.tms)) + ' min' : '–'}}</td>
      <td>${{r.vehicle_length}} ft</td>
      <td>${{r.freq}}</td>
      <td>${{r.speed_pct != null ? r.speed_pct + '%' : '–'}}</td>
      <td><span class="status-pill"><span class="status-dot" style="background:${{STATUS[r.status_key]}}"></span>${{r.status_label}}</span></td>
      <td>${{fmtCost(r.monthly_cost)}}</td>
    </tr>`).join('');
}}

function showTip(e, r) {{
  const tip = document.getElementById('tooltip');
  tip.innerHTML = `<b>Route ${{r.route_id}}</b> · ${{r.route_type}}<br>
    ${{r.sequence}}<br>
    TMS ${{fmtClock(r.tms)}}${{r.shifted ? ' (ideal ' + fmtClock(r.ideal_tms) + ')' : ''}} ·
    Vehicle ${{r.vehicle_length}}ft · Freq=${{r.freq}}<br>
    Speed: ${{r.speed_pct != null ? r.speed_pct + '%' : 'n/a'}} — ${{r.status_label}}<br>
    ${{r.top266_total != null ? 'Top266 on route: ' + r.top266_total.toFixed(0) : ''}}
    ${{r.capture_fraction != null ? ' · capture ' + (r.capture_fraction*100).toFixed(0) + '%' : ''}}<br>
    Monthly cost: ${{fmtCost(r.monthly_cost)}}`;
  tip.style.display = 'block';
  moveTip(e);
}}
function moveTip(e) {{
  const tip = document.getElementById('tooltip');
  tip.style.left = (e.clientX + 14) + 'px';
  tip.style.top  = (e.clientY + 14) + 'px';
}}
function hideTip() {{ document.getElementById('tooltip').style.display = 'none'; }}

function switchMH(mh) {{
  currentMH = mh;
  buildStats(); buildChart(); buildTable();
}}

function toggleView() {{
  tableMode = !tableMode;
  document.getElementById('view-toggle').classList.toggle('active', tableMode);
  document.getElementById('body').style.display = tableMode ? 'none' : 'block';
  document.getElementById('table-view').style.display = tableMode ? 'block' : 'none';
}}

buildDropdown();
buildLegend();
if (currentMH) {{ buildStats(); buildChart(); buildTable(); }}
</script>
</body>
</html>"""


def write_dock_utilization_visualizer(
    schedule_df: pd.DataFrame,
    route_speed_df: pd.DataFrame,
    mh_configs: dict[str, "a4.MHConfig"],
    cfg: dict[str, Any],
    out_dir,
) -> dict[str, Any]:
    """Builds Dock_Utilization.html (+ dock_utilization_data.json) in out_dir.
    Call after run_dock_scheduling_for_all_mhs, passing its own
    result["data"]["schedule_df"] / ["route_speed_df"] (or the equivalent
    CSVs reloaded from disk -- see build_dock_utilization_data's docstring
    for the `hubs` column caveat when reloading from CSV)."""
    from pathlib import Path
    import json as _json
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = build_dock_utilization_data(schedule_df, route_speed_df, mh_configs, cfg)
    with open(out_dir / "dock_utilization_data.json", "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False)

    html = _build_dock_utilization_html(data)
    with open(out_dir / "Dock_Utilization.html", "w", encoding="utf-8") as f:
        f.write(html)

    return {
        "status": "ok",
        "data": {
            "dock_utilization_json": out_dir / "dock_utilization_data.json",
            "html_path": out_dir / "Dock_Utilization.html",
        },
        "issues": [],
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

    # Dock_Utilization.html is a default output of this function -- always
    # generated alongside the CSVs above, not a separate manual step. Safe to
    # call even when schedule_df is empty (build_dock_utilization_data returns
    # an empty {"mhs": {}} and the HTML renders with no MH options).
    viz_result = write_dock_utilization_visualizer(schedule_df, route_speed_df, mh_configs, cfg, out_dir)

    return {
        "status": "partial" if issues else "ok",
        "issues": issues,
        "data": {
            "schedule_df": schedule_df,
            "route_speed_df": route_speed_df,
            "dh_speed_df": dh_speed_df,
            "speed_summary_df": speed_summary_df,
            "dock_utilization_html": viz_result["data"]["html_path"],
            "dock_utilization_json": viz_result["data"]["dock_utilization_json"],
        },
    }


def _dock_viz_self_check() -> None:
    """ponytail: smoke-test circular segment split + dock-row overlap detection."""
    import pandas as pd

    sched = pd.DataFrame([
        {"MH": "T", "Route_ID": 1, "TMS": 30.0, "Placement_Time": -90.0,
         "Route_Type": "Milkrun", "hubs": ["A"], "route_sequence": "x",
         "assigned_vehicle_length": 14, "monthly_cost": 1, "Freq": 1},
        {"MH": "T", "Route_ID": 2, "TMS": 1380.0, "Placement_Time": 1320.0,
         "Route_Type": "Milkrun", "hubs": ["B"], "route_sequence": "y",
         "assigned_vehicle_length": 14, "monthly_cost": 1, "Freq": 1},
    ])
    rs = pd.DataFrame([
        {"MH": "T", "Route_ID": 1, "Route_Speed_Pct": 95.0, "Ideal_TMS": 30.0,
         "Top266_Total": 1, "CX_Cutoff_Hour": 1.0, "Capture_Fraction": 0.8},
        {"MH": "T", "Route_ID": 2, "Route_Speed_Pct": 88.0, "Ideal_TMS": 1380.0,
         "Top266_Total": 1, "CX_Cutoff_Hour": 22.0, "Capture_Fraction": 0.7},
    ])

    class _Cfg:
        n_docks = 2

    data = build_dock_utilization_data(sched, rs, {"T": _Cfg()}, {"dock_transition_buffer_min": 30})
    mh = data["mhs"]["T"]
    r1, r2 = mh["routes"][0], mh["routes"][1]
    assert mh["window_start"] == 0.0 and mh["window_end"] == _DAY_MIN
    assert r1["wraps_midnight"] and len(r1["segments"]) == 2
    assert r1["segments"] == [{"start": 1350.0, "end": 1440.0}, {"start": 0.0, "end": 60.0}]
    assert r1["dock_row"] != r2["dock_row"], "circular overlap must land on different dock rows"
    html = _build_dock_utilization_html(data)
    assert "wraps midnight" in html and "r.segments" in html


if __name__ == "__main__":
    _dock_viz_self_check()
    print("dock viz self-check ok")
