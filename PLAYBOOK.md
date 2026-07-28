# PLAYBOOK.md

Reusable problem→solution patterns discovered across planning runs.
Claude consults this at the start of every task. Claude drafts a candidate entry and asks the user before appending — never auto-appends.

---

## How to add an entry
After solving a non-obvious problem, Claude drafts an entry in this format and asks:
> "Should I add this to the PLAYBOOK?"

Entry format:
```
[Short problem title]
Problem: [What went wrong or what was being solved]
Root cause: [Why it happened]
Solution: [Exact steps taken, including which functions were called and with what parameters]
Agents involved: [Agent 1 / 2 / 3 / 4 / Phase 2]
Date: [YYYY-MM-DD]
```

---

## Orchestrator flows

Task-order and decision pointers for Claude as orchestrator. Function names and call signatures live in the relevant AGENT MDs — not here.

### Choosing which Agent 1 output folder to reuse
**When:** User says "use old Agent 1 output" or "don't re-run Agent 1".
**Rule:** Load `plan_volume.csv`, `fbf_plan_dh_aggregate.csv`, and `fbf_network_pathway_wide.csv` from the **same** Agent 1 output folder — never mix files across folders.
**Which folder:**
- `run_20260715` — canonical June Agent 1 (SD window day_32–61); default when user says "old" without specifying. **Agent 4 requires `dh_daywise_volume.csv`** — if this folder lacks it, re-run Agent 1 with `include_daywise=True` or use a folder that has it.
- `run_20260721` — refresh re-run of the same June window; use only if user explicitly wants that refresh.
- Any Agent 1 folder with `dh_daywise_volume.csv` — required for Agent 4. Agent 1 must be called with `include_daywise=True` if daywise is missing.
**Agents involved:** Agent 1 (output only), Agent 3+
**Date:** 2026-07-27

### Agent 3 — post-run report before Checkpoint 1
**When:** Immediately after `run_agent3` returns.
**Present to user:**
- `status`, total DHs, speed vs cost assignment counts, error count, DHs moved (`current_fc_mh ≠ assigned_fc_mh`).
- Output folder path.
- **Table 1:** `build_phase2_candidates` only — valid Phase 2 inputs. Savings columns are **daily**; multiply by 30 before labeling as monthly (see AGENT3.md Checkpoint 1).
- **Table 2:** `build_cost_only_opportunities` — informational only; never offer as Phase 2 inputs.
- If `status=partial`: surface `smh_missing_rate_card_edges.csv` — affected lanes may have understated MH→MH cost. Proceed only if user accepts that risk.
**Agents involved:** Agent 3
**Date:** 2026-07-27

### Phase 2 — before running
**When:** User approves one or more pairs from Checkpoint 1 Table 1.
**Checks:**
- Tuple direction is `(from_mh, to_mh)` = `(current_fc_mh, assigned_fc_mh)` from Agent 3 — reversed tuple finds zero flagged DHs.
- Location file must reflect **current** Agent 3 assignment merged with DH Feasibility. Do **not** reuse `Inputs\Location_File_final.xlsx` (or any prior-run location file) unless confirmed identical to the current `dh_fc_mh_assignment.csv`.
- Write Phase 2 outputs to **`Agent_3_phase2_{DDMMYY}_{HHMM}`** inside the parent Agent 3 Phase 1 run folder (not a sibling folder at the same level).
**Agents involved:** Agent 3 Phase 2
**Date:** 2026-07-27

### Checkpoint 2 — accept close-out (mandatory, in order)
**When:** User accepts one or more Phase 2 pair results.
**Sequence** (see AGENT3.md §8b for function detail):
1. Read `Per_DH_Detail` from each accepted pair workbook.
2. Build `accepted_changes` from rows where **`Moved = True` only** — pool DHs that Phase 2 evaluated but kept at the original MH must **not** be included.
3. Patch `dh_fc_mh_assignment.csv` in the Agent 3 Phase 1 run folder: set both `current_fc_mh` and `assigned_fc_mh` to the new MH for each accepted DH. Save `dh_fc_mh_assignment_final.csv`.
4. Call `build_updated_plan_volume` using the **original** Agent 1 `plan_volume.csv` (read-only input). Save result as **`plan_volume_updated.csv` in the Phase 2 subfolder only** — never overwrite Agent 1's `plan_volume.csv`.
5. Review `build_updated_plan_volume` issues and `Path_Status` counts before treating the updated file as trusted.
6. Retain the `accepted_changes` dict for Agent 4 (see next entry).
**Agents involved:** Agent 3 Phase 2, Agent 1 output (read-only input)
**Date:** 2026-07-27

### Agent 4 after Phase 2 — passing accepted MH moves
**When:** Building the location file for Agent 4 after Phase 2 acceptance.
**Two valid approaches** (pick one; they must not contradict each other):
- **Override dict:** Pass `phase2_accepted_changes` to `build_freeze_day_location_file` — all DHs keep resort `current_fc_mh` except those in the dict.
- **Patched assignment CSV:** `current_fc_mh` already updated in `dh_fc_mh_assignment_final.csv` — pass empty dict or omit overrides.
**Rule:** If both are used, the dict and the CSV must agree on every moved DH. A bad key in the dict is a silent skip (`phase2_dh_not_found`), not an error — verify overrides applied.
**Agents involved:** Agent 4
**Date:** 2026-07-27

### Agent 4 — how to run (freeze-day + dock scheduling)
**When:** User says "run Agent 4" (any scope — full network or specific MHs).
**Rule:** Always run **both** steps unless user explicitly says to skip dock scheduling.

**Python modules:**
| Module | Role |
|---|---|
| `agent4_freeze_day.py` | Main pipeline — freeze-day search, spillover, baseline comparison |
| `agent4_dock_scheduling.py` | Mandatory post-step — dock ILP, Actual D1%/speed%, `Dock_Utilization.html` |
| `agent4.py` | **Phase 2 only** — orchestrator does not call this for Agent 4 runs |

**Required inputs** (all from `Inputs\` unless noted):
- Agent 3 assignment (`dh_fc_mh_assignment_final.csv` or equivalent)
- Agent 1 `dh_daywise_volume.csv` (same run folder as plan_volume — **must exist**)
- H2H network file (`Consolidated H2H … H2H.csv`)
- `DH Feasibility.csv`, `Distance Matrix.csv`, `Lat Longs.xlsx`, `MHDH_RateCard.xlsx`, `Load Profile.csv`

**Call sequence:**
```python
import sys, io
from pathlib import Path
import pandas as pd
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

import agent3 as a3
import agent4 as a4
import agent4_freeze_day as fd
import agent4_dock_scheduling as ds

ROOT = Path(r'...')   # project root
INP  = ROOT / 'Inputs'
BACK = ROOT / 'Agent4_Routing' / 'backend'
cfg  = a4.load_agent4_config(BACK / 'agent4_config.json')

# Load inputs
assign_df   = pd.read_csv(agent3_out / 'dh_fc_mh_assignment_final.csv', low_memory=False)
daywise_df  = pd.read_csv(agent1_out / 'dh_daywise_volume.csv')
h2h_df      = pd.read_csv(INP / "Consolidated H2H June'26 Network - June'26 H2H.csv", low_memory=False)
feas_df     = pd.read_csv(INP / 'DH Feasibility.csv', low_memory=False)
dist_df     = pd.read_csv(INP / 'Distance Matrix.csv', dtype=str)
dist_df['distance'] = pd.to_numeric(dist_df['distance'], errors='coerce')
lat_df      = pd.read_excel(INP / 'Lat Longs.xlsx', engine='openpyxl')
load_df     = pd.read_csv(INP / 'Load Profile.csv')
mh_configs  = a4.load_rate_card(INP / 'MHDH_RateCard.xlsx', cfg)
mhdh_df     = pd.read_excel(INP / 'MHDH_RateCard.xlsx', engine='openpyxl')

# Optional: scope to specific MHs
# assign_df = assign_df[assign_df['current_fc_mh'].isin(['CENTRALHUB_FPT'])].copy()
# mh_configs = {k: v for k, v in mh_configs.items() if k in {'CENTRALHUB_FPT'}}

# Step 1 — location file
loc_res = fd.build_freeze_day_location_file(
    assign_df, feas_df, h2h_df, daywise_df, mh_configs, cfg,
    phase2_accepted_changes=accepted_changes_or_none,
)
loc_df = loc_res['data'].dropna(subset=['ML']).copy()

# Step 2 — preflight (Checkpoint 3 if failed)
pf = a4.preflight_check(loc_df, dist_df, mhdh_df, cfg)

dist_dict = a4.build_distance_dict(dist_df)['data']
latlong   = a4.build_latlong_dict(lat_df)['data']
load_interp = a3.build_load_profile_interp(load_df)['data']

out_dir = ROOT / 'Agent4_Routing' / 'output' / f'Agent_4_{DDMMYY}_{HHMM}'

# Step 3 — freeze-day pipeline (always)
pipeline_res = fd.run_agent4_freeze_day_pipeline(
    loc_df, dist_dict, latlong, mh_configs, out_dir, cfg,
    on_progress=lambda msg: print(msg, flush=True),
)
fd.write_route_visualizer(pipeline_res['data']['per_mh_results'], latlong, out_dir, cfg)

# Step 4 — dock scheduling (always, unless user said skip)
dock_res = ds.run_dock_scheduling_for_all_mhs(
    pipeline_res['data']['per_mh_results'],
    loc_df, dist_dict, latlong, mh_configs, load_interp, cfg, out_dir,
    h2h_df=h2h_df,   # required for baseline speed% + DH_Speed baseline columns
)
```

**Dock scheduling outputs (when `h2h_df` passed):**
- `Speed_Summary.csv` — adds `baseline_mh_speed_pct`, `speed_delta_pct` (current H2H TMS vs dock-optimized proposal)
- `Network_Summary.csv` — adds `baseline_speed_pct`, `optimal_speed_pct`, `speed_improvement_pct`
- `DH_Speed.csv` — adds `Baseline_TMS`, `Baseline_Arrival_Time`, `Baseline_Capture_Fraction`, `Baseline_D1_Achieved`, `Speed_Delta_Contribution` per DH; **`Arrival_Time` is absolute minutes from order-day (Day 0) midnight** (same scale as `D1_True_Threshold` = 1800 = 6 AM Day 1), not the recurring TMS clock
- `Expanded_Schedule.csv` — appends `Arrival_Time` / `Departure_Time` per stop (`H:MM AM/PM`), anchored on `Route_Speed.TMS` (recurring clock for display)

**Two time bases (dock scheduling):** TMS / placement / HTML viz use the recurring 0–1440 clock; D1 pass/fail, speed objective, `DH_Speed.Arrival_Time`, and dock ILP occupancy use absolute minutes from order-day midnight. See **"D1 speed scoring — absolute order-day timeline"** below.

**UTF-8 on Windows** — required for ₹ / → in progress logs (see `sys.stdout` wrapper above).

**Run in background** for full network — freeze-day search is long per MH. Scoped single-MH runs can run foreground.

**Output folder:** `Agent4_Routing\output\Agent_4_{DDMMYY}_{HHMM}` — record in `RUN_HISTORY.md`.

**Post-run checks (mandatory):**
- Every DH in scope present in `Lat Longs.xlsx` (missing → ILP cluster failure → incomplete cost)
- Check `ilp_status` / `missing_dhs` per MH in freeze-day results — never report a cost if ILP failed
- Report OSRM pairs from runtime logs

**Ad-hoc single-day** (e.g. "run PAT6 for day 16"): `fd.run_freeze_day_single_day(...)` — still follow with dock scheduling unless user says skip.

**Agents involved:** Agent 1 (daywise), Agent 4
**Date:** 2026-07-28

### Output folder naming convention
**When:** Starting any agent run.
**Pattern** (see PROJECT_CONTEXT.md §2a):
- `Agent_{N}_{DDMMYY}_{HHMM}` where `N` = agent number (1, 3, or 4), date = day-month-year, time = 24h `HHMM`
- Agent 3 Phase 2: nested child `Agent_3_phase2_{DDMMYY}_{HHMM}` inside the parent `Agent_3_{DDMMYY}_{HHMM}` folder
- Agent 4: `Agent4_Routing\output\Agent_4_{DDMMYY}_{HHMM}`
- Agent 1: `Agent1_DataPrep\output\Agent_1_{DDMMYY}_{HHMM}`
**Rule:** Record the folder path in `RUN_HISTORY.md`. Legacy `run_YYYYMMDD` folders pre-date this convention.
**Agents involved:** All
**Date:** 2026-07-27

---

## Known patterns

### SD plan returns empty_result
**Problem:** `build_sd_plan_aggregate` or `build_fbf_aggregate` returns `status="failed"` with `empty_result` issue
**Root cause:** Day column naming in source file does not match `day_N` pattern, OR `fbf_plan_day_start`/`fbf_plan_day_end` window is outside the available column range
**Solution:** Check column names in the SD plan file. Rename to `day_1`...`day_N` if needed. Confirm window config matches available columns: `cfg["fbf_plan_day_start"]`, `cfg["fbf_plan_day_end"]`, `cfg["fbf_plan_avg_divisor"]`
**Agents involved:** Agent 1
**Date:** 2026-07-15

### Phase 2 merge — orchestrator manual steps (supersedes "no merge function")
**Problem:** After Phase 2, accepted DH moves must be applied to both the assignment file and `plan_volume.csv` before Agent 4.
**Root cause:** No single `merge_phase2_changes()` function — orchestrator performs the merge.
**Solution:** Follow **Orchestrator flows → Checkpoint 2 — accept close-out** above. Key gotcha: only `Moved=True` rows from `Per_DH_Detail` go into `accepted_changes`; pool DHs kept at the original MH must be excluded.
**Agents involved:** Agent 3 Phase 2
**Date:** 2026-07-15 (original), 2026-07-27 (flow documented)

### `run_phase2` could not actually execute — two backend modules were missing entirely
**Problem:** Calling `a3.run_phase2(...)` failed immediately with `ModuleNotFoundError: No module named 'agent3_phase2'`. Checked `git log --all` across the whole repo history — `agent3_phase2.py` and `agent4_pipeline.py` had **never existed in this repo, in any commit**. This matched AGENT3.md's own §9 note ("Phase 2 not tested end-to-end with new agent3.py") — it had never actually been runnable.
**Root cause:** This "Agentic tools" repo is a from-scratch rewrite of an older, separate project at `C:\Users\aniket.kathuria\Desktop\Claude\Agent 3\` / `Agent 4\` (a fixed-code, non-AI orchestrator). Two backend files that the new `agent3.py`'s `run_phase2()` was written to import were, by mistake, only ever saved into that old project folder — never copied into this repo. Confirmed genuine (not a coincidence) by checking `agent3_phase2.py`: it hardcodes this exact repo's own path (`C:\Users\aniket.kathuria\Desktop\Agentic tools\Agent4_Routing\backend`), and every function/class the new `run_phase2` imports from it (`load_h2h`, `expand_pool`, `optimize_pool_assignment`, `build_pair_output`, `write_excel_outputs`, `MHPairResult`, `Phase2Result`, `_mhmh_cost_at_mh`, `_run_a4_subset`, `DAYS_PER_MONTH`) exists there with an exact matching signature. The old file's own top-level orchestrator functions (`run_phase2_pipeline`, `compute_mh_pair_savings`) are the old project's fixed-code entry points and are **not** used by the new `agent3.py` (which reimplements that orchestration loop itself, since Claude is the orchestrator now) — harmless to leave present, unused.
**One real bug found in this repo's own code:** `agent3.py`'s `run_phase2()` did `import agent4_pipeline as p4` — a module name that never existed anywhere, in either project. The old `agent3_phase2.py` itself correctly does `import agent4 as p4` (targeting the real, current, rewritten `agent4.py`). The old `Agent 4\backend\agent4_pipeline.py` found alongside it is a genuinely **older, pre-rewrite version** of `agent4.py` (1146 lines vs. today's 1698 — missing `n_docks`, contextvar-based OSRM logging, etc.) and must **not** be copied in — it would shadow/conflict with the current, tested `agent4.py`.
**Solution:**
1. Copied `agent3_phase2.py` (unmodified) and its own dependency `agent3_pipeline.py` (only satisfies an unused top-level import inside `agent3_phase2.py`'s dead `run_phase2_pipeline` path — safe, standard-library + numpy/pandas only, no further cascading dependencies) into `Agent3_Clustering\backend\`.
2. Fixed the one-line import in `agent3.py`'s `run_phase2()`: `import agent4_pipeline as p4` → `import agent4 as p4`.
3. Reran Phase 2 for `CENTRALHUB_L_VNS4 → CENTRALHUB_L_GOP1` end-to-end on real data — `status="ok"`, no issues, completed the 32-combination enumeration ILP cleanly, 3/5 pool DHs moved, ₹4,52,247/mo MHDH saving, −₹67,694/mo MHMH (a cost), ₹3,84,554/mo total. Confirms the "frozen Phase 2 interface" AGENT4.md documents is genuinely intact — no other Agent 4 compatibility issues found.
**Agents involved:** Agent 3 Phase 2, Agent 4 (interface only, no changes needed)
**Date:** 2026-07-27

### `build_updated_plan_volume` — first real-data test, on the VNS4→GOP1 Phase 2 acceptance above
**Setup:** Built `accepted_changes` from the Phase 2 Excel's `Per_DH_Detail` sheet (the 3 DHs that actually moved: `SATELLITEHUB_BASTI`, `SATELLITEHUB_MAHARAJGANJ`, `SATELLITEHUB_MHJ` → `CENTRALHUB_L_GOP1`) and ran `a3.build_updated_plan_volume` against the full real `plan_volume.csv` (206,106 rows).
**Result:** Ran cleanly end-to-end, no exceptions. 206,106 → 206,104 rows (net −2, expected — each moved DH's several FBF rows collapse to exactly 2: a P1 direct row + a P2 row). `Path_Status`: 205,391 unchanged / 614 verified / 99 estimated — every NFBF/ALITE row for the 3 moved DHs across ~200+ distinct `MH1` sources each got its tail correctly patched. The FBF P1/P2 replace also worked correctly: for all 3 DHs, the P2 leg (`CENTRALHUB_L_PAT6 → CENTRALHUB_L_GOP1`) resolved as `"verified"` — a route that already exists elsewhere in the network (Agent 3's own candidate table had already flagged `PAT6→GOP1` as a separate valid pair, independently confirming this).
**Real edge case triggered:** `status="partial"`, exactly 3 issues — all `mixed_stream_fbf_caveat`, one per moved DH. All 3 DHs' FBF rows are flagged `has_mixed_streams=True` in real data, so their median/peak/CFT re-split should be reviewed rather than fully trusted — exactly the caveat the function was designed to surface rather than silently hide.
**Agents involved:** Agent 3 (`build_updated_plan_volume`)
**Date:** 2026-07-27

### MANDATORY — Never report a cost number for an MH where ILP failed for any cluster
**Rule:** `status="ok"` at the pipeline level does not mean every MH's cost is complete — it only means the run finished without a hard error. If `Agent4MHResult.ilp_status` shows `"FAILED"` for any cluster, or `missing_dhs` is non-empty, that MH's reported cost is silently missing an entire cluster's worth of milkrun cost. This must be the headline of that MH's result ("Computation FAILED for [MH] — cluster [id] uncovered, DHs: [list], cost is INCOMPLETE"), never a footnote after presenting a number.
**Root-cause checklist to give alongside the failure** (in order of likelihood): (1) DH missing from `Lat Longs.xlsx` → bearing defaults to 0° → no valid permutations (see the specific pattern below); (2) missing distance data for a required leg, OSRM also failed; (3) genuinely infeasible time window — the DH is too far from its MH for any route composition to arrive before `time_window_end` (check `depot_departure + get_transit_time(dist)` against `time_window_end` directly).
**Freeze-day engine note:** `run_freeze_day_candidate` (`agent4_freeze_day.py`) calls the same `run_agent4_for_mh` per candidate day, so `ilp_status`/`missing_dhs` are available per candidate, not just the optimal one. Since time-window/distance/position constraints don't vary by simulated day, a structural failure will typically repeat identically across every real and synthetic candidate for that MH — check more than just the winning day.
**Agents involved:** Agent 4 (freeze-day engine)
**Date:** 2026-07-23

---

### ILP cluster failure — DH missing from Lat Longs.xlsx → incomplete output
**Problem:** Freeze-day or Phase 2 routing logs `WARN: ILP FAILED for cluster X; uncovered: ['DH_NAME']` and `Step 4 ILP done: 0 routes assigned` for an MH. The result is **incomplete output** — not just for the named DH, but for every milkrun DH in that MH.

**Concrete example (run_20260721):** `CENTRALHUB_L_PAT6` had 65 DHs. `SATELLITEHUB_BIHTA` was missing from `Lat Longs.xlsx`. Result: ILP failed → 0 milkrun routes → only 2 FTL routes assigned → reported cost ₹2.32L/month. After adding BIHTA to Lat Longs and re-running: 31 milkrun routes + 2 FTL → correct cost ₹1,07,81,103/month. The difference was ₹1.05 Cr/month — entirely invisible in the first run's output.

**This is not DH-specific.** Any DH missing from Lat Longs will cause the same failure for whichever MH it belongs to. Always run the diagnosis check before treating an Agent 4 result as final.

**Root cause:** Without lat/lon, Agent 4 assigns `bearing = 0.0` (due north placeholder). The DH generates no valid candidate routes in Step 2 permutation generation. Since the ILP requires all DHs in a cluster to be covered, one uncovered DH makes the entire cluster infeasible → 0 milkrun routes for the whole MH.

**Diagnosis — run before accepting any Agent 4 result:**
```python
lat = pd.read_excel(inp / 'Lat Longs.xlsx')
loc_keys = loc_df['destination_hub_key'].unique()
missing_latlon = [k for k in loc_keys if k not in lat['Site_name'].values]
print(missing_latlon)   # must be empty before trusting output
```
Also check validation report for `WARN: ILP FAILED` — if present, output is incomplete regardless of `status="ok"`.

**Solution:** Add the missing DH(s) to `Lat Longs.xlsx`, then re-run Agent 4 for the affected MH(s) only — freeze-day + dock scheduling, scoped to those MHs:
```python
single_mh_loc = loc_df[loc_df['current_fc_mh'] == affected_mh].copy()
single_mh_configs = {affected_mh: mh_configs[affected_mh]}
# ... run build_freeze_day_location_file scoped, then pipeline + dock scheduling
```
**Note:** A DH can be present in the distance matrix and still missing from Lat Longs — the two gaps are independent. Fixing the distance matrix does not fix this.
**Agents involved:** Agent 4
**Date:** 2026-07-20 (first seen), 2026-07-21 (BIHTA/PAT6 confirmed and resolved)

---

### MH1 name mismatch causes silent zero costs in Agent 3
**Problem:** Agent 3 produces very low or zero MH→MH costs for all lanes, making cost_delta_rs appear huge for every DH
**Root cause:** Hub names in MH1-MH2 rate card do not match hub names in plan_volume.csv. cost_lookup returns None for every edge → silent zero cost
**Solution:** Cross-check MH1 column values in rate card against MH1/MH2 columns in plan_volume. Normalise naming (uppercase, no extra spaces) in rate card to match plan_volume format. Rebuild cost_lookup and re-run Agent 3.
**Agents involved:** Agent 2, Agent 3
**Date:** 2026-07-15

---

### Day-column numbering mismatch corrupts synthetic days and mislabels real days (Agent 4 freeze-day engine)
**Problem:** In `agent4_freeze_day.py`, `_add_synthetic_days` silently overwrote real demand columns, and `run_single_mh_freeze_day` mislabeled real days as synthetic in output (`is_synthetic=True` for a genuinely real day like `D54`).
**Root cause:** Day columns are named after the *source file's* day numbers (e.g. `D32`...`D61` for a June window starting at `day_32`), not renumbered from 1. Two places wrongly used the day **count** (e.g. 30) instead of the actual max day **number** (e.g. 61): (1) `_add_synthetic_days` computed `synth_start = len(day_cols) + 1` = `D31`, which collided with and overwrote real `D31`-`D37`; (2) `is_synthetic` was computed as `int(freeze_col[1:]) > len(real_day_cols)`, so any real day numbered above the day *count* (e.g. `D54 > 30`) was wrongly flagged synthetic.
**Solution:** Both fixed to use `max(int(c[1:]) for c in real_day_cols)` instead of `len(real_day_cols)`. Synthetic days are now correctly numbered `max_real_day + 1` through `max_real_day + 7` (e.g. `D62`-`D68` for a `D32`-`D61` window), and `is_synthetic` correctly compares against the max real day number. Verified: real days (`D54`, `D61`) preserved untouched and labeled `is_synthetic=False`; synthetic days (`D62`-`D68`) labeled `True`. This bug affects **any** run where the SD-plan day window doesn't start at `day_1` — i.e. every real run except a literal Month-1 window.
**Agents involved:** Agent 4 (freeze-day engine)
**Date:** 2026-07-23

---

### FTL/dedicated residual double-counted against milkrun capacity (Agent 4 freeze-day engine)
**Problem:** A frozen day sized to each DH's own peak demand should mathematically guarantee 0% ad-hoc (every real day's demand for a DH is, by definition, ≤ its own peak). Instead, real runs showed a DH needing an FTL truck (e.g. `SATELLITEHUB_DANAPUR`, real demand 1,386–2,465 CFT against a 1,550 CFT vehicle cap) spilling on nearly every real day even though its milkrun residual (≤ 915 CFT) comfortably fit its 1,255 CFT milkrun route.
**Root cause:** `compute_spillover_day`'s dedicated/FTL-overflow section (A) computed the leftover residual after the DH's frozen FTL trucks and spilled it as an ad-hoc **dedicated** route whenever it was `> 0` — but that same leftover is exactly what the DH's own frozen **milkrun** route was already sized to carry. Section B (milkrun overflow) also independently checked this DH's demand, but against the DH's **raw day-demand** instead of the demand net of its own frozen FTL trucks — colab's original code had a `mr_residual()` step for exactly this, which had been missed during porting. Net effect: the same residual demand was checked twice, against two different vehicles, using two different (both wrong) quantities.
**Solution:** Section A now only spills genuine *extra full-truck-loads* beyond the frozen FTL count (`while after > cap: spill(cap); after -= cap` — no longer any `if after > 0: spill(after)` at the end). Section B now computes each DH's demand via a ported `_mr_residual()` (raw demand minus `n_frozen_ftl × ftl_cap`, capped at one milkrun-cap-sized chunk) before comparing against the milkrun vehicle's capacity. Verified: the peak-day candidate now shows exactly 0 adhoc cost across all 30 real days, as the math requires. **This bug affected every spillover simulation call in the engine, not just peak-day candidates** — re-running PAT6/FPT after the fix changed the optimal day for both MHs and let FPT find a day within the 10% adhoc target for the first time (previously it never met the constraint).
**Agents involved:** Agent 4 (freeze-day engine)
**Date:** 2026-07-23

---

### Freq=2 routes need day-pair demand reversion before spillover simulation (Agent 4 freeze-day engine)
**Problem:** `compute_spillover_day` checked each real day's raw demand against a route's frozen vehicle capacity. For routes the ILP assigned `Freq=2` (runs every *other* day, vehicle sized for 2 days' combined demand), this silently hid real spillover — a route sized for `2×demand` was compared against only 1 day's demand every day, so it never looked full even when the truck genuinely couldn't have handled the real world's every-other-day pickup pattern.
**Root cause:** No day-reversion step existed. Freq=2 is chosen by the ILP itself per candidate freeze day (route-level, not a fixed DH property) whenever every DH on a route has `Freq_Allowed=1` (no Top266/D1% shipments) and it's cheaper — this was already correctly implemented; only the *spillover simulation* side was missing the corresponding demand adjustment.
**Solution:** Added `_freq2_dhs_from_final_assignment` (reads `Freq==2` DHs directly off a plan's own `final_assignment_df` — works identically for the optimized freeze-day plan and the baseline's H2H-derived plan, since both carry a `Freq` column) and `_build_freq_reverted_demands` (merges each pair of consecutive days into the second day for freq-2 DHs only: `demand[i+1] += demand[i]; demand[i] = 0`, for `i = 0, 2, 4, ...`). Both are called automatically inside `run_spillover_simulation` — no call-site changes needed anywhere else. Verified: a scenario with raw per-day demand of `700,700,100,100` against a `1255`-CFT cap showed **zero** spillover before the fix (each raw day under cap) and correctly showed `145 CFT` spillover on the merged day after the fix.
**Agents involved:** Agent 4 (freeze-day engine)
**Date:** 2026-07-23

---

## Full pipeline run — June'26 (Agent 1 → Agent 4)

**Date:** 2026-07-20
**Run tag:** run_20260716b / run_20260716b_phase2c (canonical for June'26)
**Agents involved:** Agent 1, Agent 3, Phase 2, Agent 4

**Run tag note:** Agent 1 was run a day earlier (2026-07-15) and its output lives under `run_20260715`. Agent 3 ran on 2026-07-16 (second attempt, hence 'b') using that Agent 1 output. `run_20260716b` is the canonical tag for the June'26 full pipeline — use it when referencing Agent 3, Phase 2, and Agent 4 outputs.

### Overview

This documents the first complete end-to-end pipeline run for the June'26 network. All non-obvious issues, fixes, and patterns are recorded below for future reruns.

---

### Agent 1 — Data Prep

**Config used:**
- SD plan window: `day_32` to `day_61` (divisor=30, June month)
- NFBF file (~42 GB): called in path mode (not DataFrame mode) to avoid OOM — pass the file path string, not a loaded DataFrame
- No MH1 tagging file — skipped (optional)

**Output:** `Agent1_DataPrep\output\run_20260715\plan_volume.csv` (206,106 rows)

**Issues:**
- `build_sd_plan_aggregate` partial: 246,284 resort lanes had no SD plan match — expected, filtered downstream by `build_plan_volume`. Not a blocker.
- plan_volume had a `resort_mh` column naming mismatch mid-run; fixed in-place without re-running Agent 1 (edit CSV directly, no rerun needed if only a column rename is required).

---

### Agent 2 — Input files

No `agent2.py` exists — Claude loads these directly and passes DataFrames to Agents 3 and 4.

| DataFrame | Source file | Format | Sheet / notes |
|---|---|---|---|
| `dist_df` | `Inputs\Distance Matrix.csv` | CSV | No sheet. Load with `dtype=str`; convert `distance` col to numeric after load. |
| `mh1mh2_rate_df` | `Inputs\MH1-MH2 Rate Card.csv` | CSV | No sheet. Columns: `MH1`, `MH2`, `C/T` (or cost-equivalent). |
| `mhdh_df` (Agent 3) | `Inputs\MHDH_RateCard.xlsx` | XLSX | First sheet (index 0) — no `sheet_name` arg. Columns: `MH1`, `Local: <size>`, `Zonal: <size>`. |
| `mhdh_rate_card_path` (Agent 4) | `Inputs\MHDH_RateCard.xlsx` | XLSX | Passed as a `Path` object, not a DataFrame — Agent 4 loads it internally from first sheet. |

```python
dist_df        = pd.read_csv(inp / 'Distance Matrix.csv', dtype=str)
dist_df['distance'] = pd.to_numeric(dist_df['distance'], errors='coerce')
mh1mh2_rate_df = pd.read_csv(inp / 'MH1-MH2 Rate Card.csv', low_memory=False)
mhdh_df        = pd.read_excel(inp / 'MHDH_RateCard.xlsx', engine='openpyxl')  # for Agent 3
# Agent 4: pass inp / 'MHDH_RateCard.xlsx' directly as mhdh_rate_card_path
```

---

### Agent 3 — Clustering & DH Assignment

**Call sequence:**
```python
cfg = a3.load_agent3_config(...)
result = a3.run_agent3_pipeline(
    plan_volume_df    = plan_volume_df,
    fbf_df            = fbf_df,
    fbf_pathway_df    = fbf_pathway_df,
    dist_df           = dist_df,
    mh1mh2_rate_df    = mh1mh2_rate_df,
    mhdh_rate_df      = mhdh_rate_df,
    cfg               = cfg,
)
agent3_df = result['data']['dh_fc_mh_assignment']
```

**Output:** `Agent3_Clustering\output\run_20260716b\dh_fc_mh_assignment.csv` (820 rows)
- 688 DHs unchanged (`current_fc_mh == assigned_fc_mh`)
- 132 DHs moved by Agent 3 (`current_fc_mh != assigned_fc_mh`)

---

### Checkpoint 1 — Savings Table

**CRITICAL — Use `build_phase2_candidates`, NOT `build_cost_only_opportunities` for Phase 2 inputs.**

```python
candidates = a3.build_phase2_candidates(agent3_df)        # valid Phase 2 pairs
cost_opps  = a3.build_cost_only_opportunities(agent3_df)  # informational only
```

**Bug found this run:** An earlier version of the savings table grouped by `assigned_fc_mh` as the "from" MH, which mixed Phase 2 candidates with cost-only opportunities and produced false pairs (e.g. VZG1→VGA1 appeared with 4 DHs but was invalid — Agent 3 never moved any DH from VZG1 to VGA1). The fix is `build_phase2_candidates`, which groups by `(current_fc_mh, assigned_fc_mh)` where they differ. Always use this function.

**`cost_delta_rs` is per day** — multiply by 30 for monthly savings before presenting to the user. **This bites `build_phase2_candidates` too:** its output column is named `monthly_saving_rs` but is literally `sum(cost_delta_rs)` per pair — a daily figure despite the name. Caught 2026-07-27 by presenting the raw column value to the user labeled "Monthly saving" (it was actually daily). Always ×30 before showing any of `total_cost_rs`/`current_cost_rs`/`monthly_saving_rs` from this table as a monthly number — see AGENT3.md §4 Checkpoint 1 for the same caveat.

**Verifying a pair is valid before Phase 2:**
- Run: `agent3_df[(agent3_df['current_fc_mh'] == from_mh) & (agent3_df['assigned_fc_mh'] == to_mh)]`
- If 0 rows → pair is invalid, do not offer it as a Phase 2 candidate
- If >0 rows → valid, proceed

---

### Phase 2

**Pairs evaluated:** VNS4→LKO3, VNS4→GOP1, PAT6→GOP1
**Pairs accepted:** VNS4→LKO3 (4 DHs, ₹1.91L/mo), VNS4→GOP1 (4 DHs, ₹4.29L/mo)
**Pairs rejected:** PAT6→GOP1

**Call sequence for each pair:**
```python
result = a3.run_phase2_analysis(
    agent3_df      = agent3_df,
    from_mh        = 'CENTRALHUB_L_VNS4',
    to_mh          = 'CENTRALHUB_L_LKO3',
    h2h_df         = h2h_df,
    dist_df        = dist_df,
    mh1mh2_rate_df = mh1mh2_rate_df,
    mhdh_rate_df   = mhdh_rate_df,
    cfg            = cfg,
    out_dir        = out_dir,
)
```

**Phase 2 flagging condition (agent3.py lines 2234–2237):** A DH is flagged for Phase 2 evaluation only if BOTH conditions hold:
- `current_fc_mh == from_mh` (resort baseline is from_mh)
- `assigned_fc_mh == to_mh` (Agent 3 proposed moving it to to_mh)

A DH where `current=MPL1, assigned=CJB3` is NOT a valid CJB3→MPL1 Phase 2 candidate — it is a MPL1→CJB3 candidate (Agent 3 moved it TO CJB3, not the other way).

---

### Agent 4 — location file, preflight, and blockers

See orchestrator flow **"Agent 4 — how to run"** at the top of this file. Use `build_freeze_day_location_file`, not `build_location_file` alone.

**MH source logic:** all DHs use `current_fc_mh` (resort); only `phase2_accepted_changes` overrides apply. Drop null-ML rows before preflight.

**Known blockers:** missing rate card MHs (skip or fix), missing distance (OSRM if lat/long exists), missing lat/long (**must fix**), missing `dh_daywise_volume.csv` (re-run Agent 1 with `include_daywise=True`), missing ML in DH Feasibility.

---

## Agent 4 Freeze-Day + Dock Scheduling — build & run reference

**Date introduced:** 2026-07-22/23 (updated 2026-07-28)
**Agents involved:** Agent 1 (daywise output), Agent 4

### What it is

The main Agent 4 pipeline. Tests every day in the SD-plan window + 7 synthetic extreme days, simulates spillover/adhoc, picks the optimum, compares against the H2H baseline, then runs dock scheduling + Actual D1%/speed% on the optimal plan.

**Orchestrator:** see **"Agent 4 — how to run"** at the top of this file. Always run freeze-day + dock scheduling together unless user explicitly says skip dock scheduling.

`agent4.py` is used by Phase 2 only — orchestrator does not call it for Agent 4 runs.

### New Agent 1 output (extends `build_sd_plan_aggregate`, doesn't add a new function)

`build_sd_plan_aggregate(..., include_daywise=True)` now also returns `result["daywise_data"]` — a DH-level (not MH1×DH) day-by-day table: `destination_hub_key`, `D<n>` (shipment counts), `D<n>_cft` (CFT volume), computed from the **same chunked pass** used for the existing lane aggregate (no second read of the 40GB NFBF file). Default `include_daywise=False` preserves the exact prior behavior/performance. Save via `save_dataframe(result["daywise_data"], out_dir / "dh_daywise_volume.csv")`.

### agent4.py — Phase 2 interface only

`agent4.py` exposes `run_agent4_for_mh` and related types for `agent3_phase2._run_a4_subset`. Phase 2 is the only orchestrator use of this module. `build_location_file` with optional `h2h_df`/`daywise_df` params is called internally by `build_freeze_day_location_file`.

### Call sequence for a real run

```python
import agent1 as a1, agent4 as a4, agent4_freeze_day as fd

# Agent 1 — day window set as usual (mandatory question), plus include_daywise=True
r_demand = a1.build_sd_plan_aggregate(
    alpha_path=..., alite_path=..., nfbf_path=...,
    mh_dh_mapping_df=mh_dh_df, cft_vertical_df=r_cft["data"],
    config=cfg1, include_daywise=True,
)
a1.save_dataframe(r_demand["daywise_data"], out_dir / "dh_daywise_volume.csv")
# ... rest of Agent 1 Pipeline A/C/E unchanged ...

# Agent 3 — completely unchanged, run as documented in AGENT3.md

# Agent 4 — new engine
agent3_df   = pd.read_csv(agent3_out / "dh_fc_mh_assignment.csv")
daywise_df  = pd.read_csv(agent1_out / "dh_daywise_volume.csv")
h2h_df      = pd.read_csv(inp / "Consolidated H2H June'26 Network - June'26 H2H.csv")
feas_df     = pd.read_csv(inp / "DH Feasibility.csv")
cfg4        = a4.load_agent4_config(agent4_backend / "agent4_config.json")
mh_configs  = a4.load_rate_card(inp / "MHDH_RateCard.xlsx", cfg4)

loc_res = fd.build_freeze_day_location_file(
    agent3_df, feas_df, h2h_df, daywise_df, mh_configs, cfg4,
)
dist_dict = a4.build_distance_dict(dist_df)["data"]
latlong   = a4.build_latlong_dict(lat_long_df)["data"]

pipeline_res = fd.run_agent4_freeze_day_pipeline(
    loc_res["data"], dist_dict, latlong, mh_configs, out_dir, cfg4,
    on_progress=lambda msg: print(msg, flush=True),   # granular per-MH/per-candidate-day/per-upgrade-iteration logs
)
fd.write_route_visualizer(pipeline_res["data"]["per_mh_results"], latlong, out_dir, cfg4)

import agent4_dock_scheduling as ds
load_interp = a3.build_load_profile_interp(load_df)["data"]
dock_res = ds.run_dock_scheduling_for_all_mhs(
    pipeline_res["data"]["per_mh_results"],
    loc_res["data"], dist_dict, latlong, mh_configs, load_interp, cfg4, out_dir,
    h2h_df=h2h_df,
)
```

**Restricting to specific MHs**: filter `agent3_df`/`mh_configs` to the target MHs *before* calling `build_freeze_day_location_file`/`run_agent4_freeze_day_pipeline` — e.g. `agent3_df[agent3_df["current_fc_mh"].isin(["CENTRALHUB_L_PAT6", "CENTRALHUB_FPT"])]`.

**Ad-hoc single-day question** (e.g. "run PAT6 for day 16"): use `fd.run_freeze_day_single_day(mh_name, mh_cfg, dh_rows, "D16", dist_dict, latlong, cfg4, out_dir=..., baseline=baseline_result)` — freezes at exactly that day instead of searching all candidates, reuses every existing helper, writes `FA_/ES_/SP_/BVO_<mh>_<day>.csv`. Rejects synthetic days (only real day columns are valid — simulating a synthetic day against real demand isn't meaningful).

### New config keys (`agent4_config.json`)

| Key | Default | Effect |
|---|---|---|
| `adhoc_premium` | 1.25 | Multiplier on ad-hoc truck cost vs. the base rate |
| `adhoc_floor_monthly` | 90000 | Floor for ad-hoc route cost (÷30 × premium = daily floor) |
| `merge_window_min` | 120 | Max cutoff-time spread (minutes) for merging spilled DHs into one ad-hoc route |
| `adhoc_pct_limit` | 0.10 | Max acceptable ad-hoc% of trips for a freeze day to be "eligible"; falls back to unconstrained optimum with a warning if no day qualifies |
| `spill_threshold_pct` | 0.20 | Spill-day fraction (of a route's monthly trips) above which the truck-upgrade loop tries a bigger vehicle |
| `adhoc_repeat_threshold_days` | 7 | An ad-hoc route recurring this many times/month gets flagged "Consider as standing backup route" |
| `zero_day_threshold` | 5 | Zero-demand-day count above which a DH's day series uses circular redistribution (Case A) instead of local interpolation (Case B) |
| `col_h2h_dh_key` / `col_h2h_mr_number` / `col_h2h_frequency` | `"Dest"` / `"MR Number"` / `"frequency Final"` | H2H column names for the Current_MR/Current_Freq merge |

### Output files (written to `out_dir` by `run_agent4_freeze_day_pipeline`)

`Location_File.csv` (always saved here — never left in-memory only, never written to `Inputs\`), `Freeze_Day_Comparison.csv`, `Final_Assignment.csv`, `Expanded_Schedule.csv`, `Baseline.csv`, `Baseline_vs_Optimal.csv`, `Network_Summary.csv`, `Per_Day_Route_Log.csv`, `All_Days_Spillover.csv`, `Best_Network_Spillover.csv`, `Adhoc_Route_Summary.csv`. Plus `route_data.json` + `Route_Visualizer.html` from `write_route_visualizer()` (a Leaflet-based interactive map — toggle freeze days, compare against current/baseline routes).

### Known limitations / not yet ported

- Matplotlib chart generation (colab Block 12) — not ported, low priority
- Google Sheets I/O — intentionally dropped; local files only

### PAT6 + FPT test run — 2026-07-23 (see RUN_HISTORY.md for full detail)

First real-data validation, on top of the June'26 Agent 1/Agent 3 outputs (`run_freeze_day_test_20260722`). Caught and fixed two real bugs (day-column-numbering collision, freq-2 spillover reversion missing) — see "Known patterns" above. Final corrected result: PAT6 optimal=D65 (synthetic), 8.7% adhoc, ₹20.17L/mo savings vs. baseline; FPT optimal=D54 (real day), 31.3% adhoc, ₹4.48L/mo savings vs. baseline.

---

## Dock Scheduling + CX-Cutoff + Speed Engine — first real-data run (PAT6 + FPT)

**Problem:** After building `agent4_dock_scheduling.py` (dock ILP, CX-cutoff capture-fraction, Actual D1%/speed metric — see AGENT4.md §7b) against synthetic conflict scenarios only, needed to confirm it runs cleanly end-to-end on real production data and produces sane numbers.

**Setup:** Full freeze-day + dock scheduling for CENTRALHUB_L_PAT6 (65 DHs, 30 docks) and CENTRALHUB_FPT (30 DHs, 15 docks). Dock scheduling is part of every Agent 4 run — not a separate optional step.

**Result — freeze-day optimum (with rollover mechanism live for the first time on real data):**

| MH | Optimal day | Adhoc% | Total/mo | Baseline/mo | Savings/mo |
|---|---|---|---|---|---|
| CENTRALHUB_L_PAT6 | D67 (synthetic) | 9.3% | ₹1,24,02,461 | ₹1,49,26,163 | ₹25,23,702 (~16.9%) |
| CENTRALHUB_FPT | D60 (real day) | 8.6% | ₹37,81,737 | ₹41,32,442 | ₹3,50,705 (~8.5%) |

Both figures differ from the pre-rollover PAT6/FPT test run above (PAT6 was D47/real-day/9.2%/₹1.26Cr; FPT was D33/real-day/8.6%/₹37.7L) — this is an **expected consequence of adding the rollover mechanism**, not a regression: relaxing the feasibility window for low-Top266 DHs (`top266_shipments < low_priority_top266_threshold`, default 10) changes which routes are generable for some candidate days, which can shift the optimum. Always expect freeze-day comparison numbers to move whenever the rollover threshold config changes, even with identical demand data.

**Result — dock scheduling + speed:**

| MH | Docks total | Docks committed (95% after adhoc reserve) | Routes | Weighted Actual D1% (speed) |
|---|---|---|---|---|
| CENTRALHUB_L_PAT6 | 30 | 28 | 36 | 72.8%* |
| CENTRALHUB_FPT | 15 | 14 | 19 | 75.5%* |

\*Speed% superseded — pre D1 absolute-timeline fix (2026-07-28). Authoritative FPT proposal speed after fix: **75.0%** (`Agent_4_280726_1709_FPT`). PAT6 not yet re-run post-fix.

Of 55 total routes across both MHs, only **1** needed a dock-forced TMS shift away from its dock-unconstrained ideal departure (PAT6 Route 30: preponed 180 min, dropping that route's own speed contribution to 35.3%) — confirms dock contention is genuinely rare at these MHs' current dock counts (28–30 committed docks against 36 routes), and the ILP only intervenes when it actually has to.

**No failures:** `ilp_status` clean (no `FAILED` clusters) across all 37×2 freeze-day candidates, and `schedule_docks_and_compute_speed` returned `status="ok"` for both MHs (no `dock_schedule_infeasible`).

**Outputs:** `Agent4_Routing\output\run_dock_sched_20260723\` — full freeze-day pipeline outputs plus `Dock_Schedule.csv`, `Route_Speed.csv`, `DH_Speed.csv`, `Speed_Summary.csv`.

**Agents involved:** Agent 4 (freeze-day engine + dock-scheduling module)
**Date:** 2026-07-23

---

### Dock Utilization visualizer — added as a default output of dock scheduling

**Problem:** The dock-scheduling CSVs (`Dock_Schedule.csv`, `Route_Speed.csv`, etc.) show what happened but not *why* — which routes actually shared a dock, whether contention was real, and which routes got dock-forced away from their ideal departure. Needed a way to see dock utilization at a glance, filterable per MH.

**Solution:** Added `build_dock_utilization_data`, `_speed_status`, `_assign_dock_rows`, `_build_dock_utilization_html`, and `write_dock_utilization_visualizer` to `agent4_dock_scheduling.py`. Renders a self-contained (no external chart library, no build step) Gantt-style HTML timeline: one lane per physical dock (committed lanes + a greyed reserved-for-adhoc band), one bar per route spanning its actual occupancy window (`Placement_Time` → `TMS + dock_transition_buffer_min`), colored by a speed-status bucket (good ≥90% / warning 75–90% / serious 50–75% / critical <50%, never color-alone — dashed vs. solid borders also distinguish Milkrun vs. FTL), a thin marker on any route whose TMS was dock-forced away from its unconstrained ideal, hover tooltips, and a Table-view toggle for full accessibility.

**Dock-row assignment is a visualization construct, not the model's own decision:** the ILP only enforces a capacity *count* at each point in time — it never assigns a specific dock identity to a route. `_assign_dock_rows` uses greedy earliest-finish-time interval partitioning to produce a display-only dock assignment. Because interval graphs are perfect graphs, this greedy assignment is mathematically guaranteed to never need more rows than the ILP's own committed-dock count already certified as feasible.

**Wired in as a default output, not a manual step** (per explicit user instruction): `run_dock_scheduling_for_all_mhs` now calls `write_dock_utilization_visualizer` automatically at the end, alongside the four CSVs — `Dock_Utilization.html` + `dock_utilization_data.json` land in the same `out_dir` on every call, and their paths are exposed in the result dict as `data["dock_utilization_html"]` / `data["dock_utilization_json"]`. Verified with a synthetic 2-route/1-dock smoke test (forces a real conflict, confirms the shift-marker renders) and against the real PAT6/FPT data (correct lane counts: PAT6 28 committed + 2 reserved = 30 lanes / 36 bars; FPT 14 committed + 1 reserved = 15 lanes / 19 bars; exactly 1 shift-mark, matching the known real dock-forced shift).

**Can also be regenerated standalone** (e.g. against previously-written CSVs without rerunning the ILP) via `write_dock_utilization_visualizer(schedule_df, route_speed_df, mh_configs, cfg, out_dir)` — if reloading `Dock_Schedule.csv` from disk, parse the `hubs` column back from its string repr with `ast.literal_eval` first (`to_csv` stringifies list columns; `build_dock_utilization_data` handles this automatically when it detects a string).

### Baseline speed from H2H TMS (`agent4_dock_scheduling.py`)
**When:** Every dock-scheduling run where `h2h_df` is passed to `run_dock_scheduling_for_all_mhs`.
**What:** Computes **current-network speed%** using actual H2H `TMS` values (no dock ILP — operational departures are taken as fixed). Same weighted Top266 formula as proposal speed (CX cutoff → load profile → D1 true threshold).
**TMS selection rules:**
- Duplicate H2H rows for same `(Src, Dest)` → **latest TMS** (max minutes — later departure captures more volume)
- Milkrun MR groups → **latest TMS across hubs** in that `(MH, MR Number)`; log `baseline_tms_inconsistent` if spread **> 30 min** (still uses latest)
- Synthetic baseline truck splits (capacity-driven) → same MR-level TMS on all split trucks
- Missing TMS → fallback to computed ideal departure + `baseline_tms_missing` issue
**Outputs:** `baseline_mh_speed_pct` / `speed_delta_pct` in `Speed_Summary.csv`; speed columns in `Network_Summary.csv`; per-DH baseline columns + `Speed_Delta_Contribution` in `DH_Speed.csv`.
**Verified:** FPT re-run `Agent_4_280726_1709_FPT` (post D1 absolute-timeline fix) — baseline **63.4%** → proposal **75.0%** (+11.6 pp); cost savings unchanged (D60, ₹37.8L/mo). Prior run `Agent_4_280726_1644_FPT` showed **90.2%** proposal — **invalid** (D1 checked on recurring clock, not absolute).

### Expanded schedule stop times (`agent4_dock_scheduling.py`)
**When:** Automatically during `run_dock_scheduling_for_all_mhs` if `Expanded_Schedule.csv` already exists in `out_dir` (written by freeze-day pipeline).
**What:** Joins each route to dock-chosen `Route_Speed.TMS`, simulates per-leg arrival/departure (same logic as legacy `agent4.py` Steps 6–9), writes `Arrival_Time` / `Departure_Time` columns back to the same file in `H:MM AM/PM` format.

**Agents involved:** Agent 4 (dock-scheduling module)
**Date:** 2026-07-23

---

### Dock scheduling: FTL/MR cutoff sync + daily-recurring time model (two real bugs, one root cause)

**Problem 1 — FTL and Milkrun for the same DH didn't share a cutoff.** A DH split "FTL + Milkrun" (bulk demand on a dedicated truck, residual on a shared route) was scheduled as two completely independent ILP decisions — the same customer's cutoff could differ between its two trucks, which is operationally wrong (colab's original model synced them).

**Problem 2 — the dock model treated time as an unbounded line instead of a repeating daily cycle.** Two symptoms, traced to one root cause:
- A route made up entirely of low-Top266 (rollover-relaxed) DHs would drift its "ideal" TMS out past 24h — sometimes 48h+ — because nothing constrained it (the rollover mechanism relaxes `time_window_end` by +1440 for route-*generation* purposes) and the speed objective has zero Top266 volume to score there, so the tie-break just let it balloon.
- Two routes near opposite sides of midnight (e.g. 23:00 and 01:00) were invisible to each other's dock-capacity check — an unbounded linear timeline doesn't know they're only ~2 real clock hours apart on a schedule that repeats every single day, forever.

**Root cause (both symptoms):** the whole model was built as if scheduling a one-time, multi-day snapshot, when the real system runs the identical schedule every single day. A "TMS" is a recurring daily clock value, not a one-time absolute instant that can legitimately land on "day 2."

**Fix (`agent4_dock_scheduling.py`):**
1. **FTL/MR sync:** a DH's FTL_Dedicated route(s), if that same DH also has a Milkrun route, get no independent ILP variable at all (`linked_ftl` in `schedule_docks_and_compute_speed`) — they inherit the Milkrun route's chosen TMS, while still consuming their own dock-capacity window (2 real trucks, 2 real docks, same clock time). Their DH's Top266 volume is deliberately NOT double-counted in the objective/`dh_speed_df` — it's already scored once via the Milkrun route.
2. **Daily-cycle TMS anchor:** the dock-scheduling "ideal" anchor is now computed against each DH's TRUE, un-relaxed deadline (`d1_true_threshold`, always ~1800 min regardless of rollover) instead of the possibly rollover-inflated `time_window_end`, then folded into a genuine single-day clock value via `% 1440`. `_cx_cutoff_hour` simplified to order-day hour lookup with a midnight plateau (see D1 absolute-timeline entry below). **Dock-capacity ILP** originally used `_circular_windows` on the recurring clock; superseded 2026-07-28 by a **linear absolute grid** (order day 0 through D1 SLA) — see next section.
3. **A bug found in the fix itself, caught by testing:** the pre-existing tie-break ("prefer larger raw TMS," used to mean "prefer less preponing" when time was linear) stopped meaning that once preponing could wrap past midnight — preponing 5.8h from 05:40 wraps to 23:50, a numerically *larger* value despite being a *bigger* step away from the ideal. Fixed by tracking each candidate's actual prepone-step count and tying-breaking on that directly (`step_of`), not on raw clock value. Caught by a synthetic test asserting a zero-Top266 route with no dock contention stays at its own natural ideal — it didn't, until this was fixed.

**Verified with 3 synthetic tests:** (1) FTL+MR for one DH land on the exact same TMS, and the DH appears exactly once in `dh_speed_df`; (2) a route made entirely of a zero-Top266, rollover-relaxed DH stays bounded in `[0,1440)` *and* stays at its true single-day ideal (old code gave ~3,220 min); (3) two routes ~30 real minutes apart across midnight, with only 1 committed dock, are correctly detected as conflicting and resolved (chosen windows verified non-overlapping via `_circular_windows`).

**Known follow-ups, not fixed (flagged, not silently ignored):** the Dock Utilization HTML visualizer still draws bars on the recurring 0–1440 clock (circular segment split for midnight wraps) — correct for daily display, but `DH_Speed.Arrival_Time` is on the absolute scale; FTL loading-duration is still computed from a DH's *full* `total_shipments` independently for both legs when split (pre-existing simplification, now more visible since the FTL/MR link is formalized).
**Agents involved:** Agent 4 (dock-scheduling module)
**Date:** 2026-07-27

---

### D1 speed scoring — absolute order-day timeline (dock scheduling bug #3)

**Problem:** After the daily-recurring dock refactor (2026-07-27), D1% checks and the speed ILP objective still compared DH **arrivals** on the recurring 0–1440 **clock** against `d1_true_threshold` (**1800** = 6 AM Day 1 absolute). Early-morning departures were falsely credited: a truck leaving at 3:55 AM (clock 235) and arriving at a DH at 6:49 AM (clock 409) compared `409 ≤ 1800` → pass, when the true absolute arrival is `1440 + 409 = 1849` → **fail**.

**Concrete example (FPT Route 15, pre-fix `Agent_4_280726_1644_FPT`):** `FPT → HOSHIARPUR → UNA → KHANNA1 → FPT`, TMS clock **234.5** (absolute **1674.5**). HOSHIARPUR arrival clock **408.5** → absolute **1848.5** > 1800. Old code scored Route_Speed **99.6%**; post-fix **42.5%** with TMS shifted to evening (**1104.5** clock).

**Root cause:** Dock occupancy correctly needed a repeating daily clock, but D1 SLA is anchored on the **order cycle** (Day 0 midnight → 6 AM Day 1), not a route-local clock that resets at midnight.

**Fix (`agent4_dock_scheduling.py`) — two time bases:**

| Concept | Time base |
|---|---|
| TMS, Placement_Time, Expanded_Schedule stop times, Dock Utilization HTML | Recurring clock **0–1440** (same departure every operational day) |
| D1 pass/fail, speed objective, `DH_Speed.Arrival_Time`, dock ILP occupancy grid | **Absolute** minutes from order-day (Day 0) midnight through D1 SLA |

**Rules:**
- `_clock_to_absolute(t)`: if `t % 1440 < 360` (before 6 AM on the clock) → calendar Day 1 morning → `1440 + t`; else Day 0 → `t`
- D1 pass: `arrival_abs = clock_to_absolute(TMS) + transit_offset ≤ d1_true_threshold` (per-DH, default **1800**)
- Load profile (**Day-0 orders only** until Agent 1 supplies Day-1 load): CX cutoff → order-day hour; **plateau at hour 24** once `cutoff_abs ≥ 1440`
- Dock ILP capacity: linear grid **0 → `dock_d1_horizon_min`** (default 1800 + `dock_transition_buffer_min`), windows via `_dock_window_abs` — not `_circular_windows`

**Helpers:** `_clock_to_absolute`, `_arrival_abs`, `_capture_fraction_from_tms`, `_cx_cutoff_order_day_hour`, `_dock_window_abs`, `_dock_horizon_min`.

**Verified:** FPT `Agent_4_280726_1709_FPT` — baseline **63.4%** → proposal **75.0%** (+11.6 pp); costs unchanged (D60, 8.6% adhoc, ₹37.8L/mo). Route 15 regression: TMS 234.5 → 1104.5, KHANNA1 `Arrival_Time` **1800** abs.

**Re-run needed:** Any speed% from runs before 2026-07-28 (including Jul-23 PAT6+FPT combined run at 72.8%/75.5%) used pre-fix D1 logic — costs remain valid; speed columns are not authoritative until re-run with fixed code.

**Agents involved:** Agent 4 (dock-scheduling module)
**Date:** 2026-07-28
