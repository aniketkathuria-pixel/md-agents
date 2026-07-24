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

## Known patterns

### SD plan returns empty_result
**Problem:** `build_sd_plan_aggregate` or `build_fbf_aggregate` returns `status="failed"` with `empty_result` issue
**Root cause:** Day column naming in source file does not match `day_N` pattern, OR `fbf_plan_day_start`/`fbf_plan_day_end` window is outside the available column range
**Solution:** Check column names in the SD plan file. Rename to `day_1`...`day_N` if needed. Confirm window config matches available columns: `cfg["fbf_plan_day_start"]`, `cfg["fbf_plan_day_end"]`, `cfg["fbf_plan_avg_divisor"]`
**Agents involved:** Agent 1
**Date:** 2026-07-15

### Phase 2 merge — no merge function exists
**Problem:** After Phase 2 produces Excel workbooks with revised DH assignments, there is no `merge_phase2_changes()` function in agent3.py to automatically apply accepted changes back to `dh_fc_mh_assignment.csv`
**Root cause:** Phase 2 merge function not yet built
**Solution:** Manual merge — Claude reads the accepted Phase 2 Excel workbooks, extracts the revised `assigned_fc_mh` values for affected DHs, and updates `dh_fc_mh_assignment.csv` row by row before passing to Agent 4. Verify by checking that affected DH rows now show the new FC_MH value.
**Agents involved:** Agent 3 Phase 2
**Date:** 2026-07-15

### MANDATORY — Never report a cost number for an MH where ILP failed for any cluster
**Rule:** `status="ok"` at the pipeline level does not mean every MH's cost is complete — it only means the run finished without a hard error. If `Agent4MHResult.ilp_status` shows `"FAILED"` for any cluster, or `missing_dhs` is non-empty, that MH's reported cost is silently missing an entire cluster's worth of milkrun cost. This must be the headline of that MH's result ("Computation FAILED for [MH] — cluster [id] uncovered, DHs: [list], cost is INCOMPLETE"), never a footnote after presenting a number.
**Root-cause checklist to give alongside the failure** (in order of likelihood): (1) DH missing from `Lat Longs.xlsx` → bearing defaults to 0° → no valid permutations (see the specific pattern below); (2) missing distance data for a required leg, OSRM also failed; (3) genuinely infeasible time window — the DH is too far from its MH for any route composition to arrive before `time_window_end` (check `depot_departure + get_transit_time(dist)` against `time_window_end` directly).
**Freeze-day engine note:** `run_freeze_day_candidate` (`agent4_freeze_day.py`) calls the same `run_agent4_for_mh` per candidate day, so `ilp_status`/`missing_dhs` are available per candidate, not just the optimal one. Since time-window/distance/position constraints don't vary by simulated day, a structural failure will typically repeat identically across every real and synthetic candidate for that MH — check more than just the winning day.
**Agents involved:** Agent 4 (both legacy and freeze-day engine)
**Date:** 2026-07-23

---

### ILP cluster failure — DH missing from Lat Longs.xlsx → incomplete output
**Problem:** `run_agent4_pipeline` logs `WARN: ILP FAILED for cluster X; uncovered: ['DH_NAME']` and `Step 4 ILP done: 0 routes assigned` for an MH. The result is **incomplete output** — not just for the named DH, but for every milkrun DH in that MH. None of them get a route. This is not a partial result; it is a silent gap in the plan. The pipeline returns `status="ok"` and reports a cost figure, but that cost is FTL-only and dramatically understated.

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

**Solution:** Add the missing DH(s) to `Lat Longs.xlsx` (columns: `Site_name`, `Latitude`, `Longitude`), then re-run Agent 4 for the affected MH only — no need to rerun the full pipeline:
```python
lat_long_df   = pd.read_excel(inp / 'Lat Longs.xlsx')   # reload after fix
single_mh_loc = loc_df[loc_df['current_fc_mh'] == affected_mh].copy()
r = a4.run_agent4_pipeline(
    location_file_df    = single_mh_loc,
    lat_long_df         = lat_long_df,
    dist_df             = dist_df,
    mhdh_rate_card_path = inp / 'MHDH_RateCard.xlsx',
    out_dir             = out_dir,
    cfg                 = cfg,
    on_progress         = lambda msg: print(msg, flush=True),
)
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

**`cost_delta_rs` is per day** — multiply by 30 for monthly savings before presenting to the user.

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

### Building the Location File (Agent 4 pre-run)

**MH source logic in `build_location_file`:**
- All DHs use `current_fc_mh` (resort baseline) as their MH — this is the Phase 1 baseline
- `assigned_fc_mh` (Agent 3 Phase 1 proposal) is dropped — NOT used
- Only DHs explicitly listed in `phase2_accepted_changes` get a different MH

**`phase2_accepted_changes` format:** `{DH_key: new_MH_name}` — read from the `Per_DH_Detail` sheet of accepted Phase 2 workbooks:
```python
accepted_changes = {}
for fname in accepted_phase2_files:
    detail = pd.read_excel(phase2_out / fname, sheet_name='Per_DH_Detail')
    for _, row in detail.iterrows():
        accepted_changes[str(row['DH']).strip()] = str(row['Assigned_MH']).strip()

loc_result = a4.build_location_file(
    agent3_assignment_df    = assign_df,
    dh_feasibility_df       = feasibility_df,
    phase2_accepted_changes = accepted_changes,
)
loc_df = loc_result['data']
```

**WARNING — bad key in `phase2_accepted_changes` is a silent drop, not an error.** If a DH key does not exist in `assign_df`, the function skips it, appends a `phase2_dh_not_found` issue, and continues. Critically, `status` is driven by null-ML DHs only — so `status="ok"` does NOT guarantee all Phase 2 overrides applied. Always check explicitly:
```python
# after build_location_file call
phase2_issues = [i for i in loc_result['issues'] if i['type'] == 'phase2_dh_not_found']
if phase2_issues:
    print("WARNING — phase2 DH keys not found:", phase2_issues)
```
This was not observed in the June'26 run (all 8 DH keys were valid), but is a risk whenever accepted_changes dict is built from external workbooks with manual edits.

**After building:** Drop null-ML rows before passing to pipeline:
```python
loc_df = loc_df.dropna(subset=['ML']).copy()
```

---

### Agent 4 Pre-run Blockers — Known for June'26

Before running, verify each of these. All four were present at the start of this run:

| Blocker | Status after this run | Resolution |
|---|---|---|
| 4 MHs missing from MHDH rate card (AJLX, IXA3X, JLRSF1, KLM1) | JLRSF1 + KLM1 added by user; AJLX + IXA3X still missing | Add missing MHs to `MHDH_RateCard.xlsx`. For AJLX/IXA3X, skip their DHs for now (see below). |
| 2 MHs missing from distance matrix | Resolved by OSRM fallback at runtime | No action needed — OSRM handles missing distances automatically. |
| 12 SATELLITEHUB DHs missing ML in DH Feasibility.csv | Fixed by user | Fill ML values in `DH Feasibility.csv` and re-run `build_location_file`. |
| 4 stale lat/lon rows (REVX, AMD_FLEX, CJB_flex, LKO_FLex) | Still present | These are not in the location file and don't affect routing — clean up Lat Longs.xlsx when convenient. |

**Skipping DHs for MHs with no rate card entry:**
```python
skip_mhs = {'CENTRALHUB_LM_AJLX', 'CENTRALHUB_LM_IXA3X'}
loc_df = loc_df[~loc_df['current_fc_mh'].astype(str).str.strip().isin(skip_mhs)].copy()
```
DHs excluded this run: ABA, BLO, LGI, LWT (4 DHs under AJLX/IXA3X).

---

### Agent 4 — Full Pipeline Run

**UTF-8 required on Windows** — Agent 4 prints `→` and `₹` characters. Add at top of any run script:
```python
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
```
Without this: `UnicodeEncodeError` on Windows when the pipeline logs milkrun progress.

**Run in background with a long timeout** — full pipeline takes ~30–60 min for 800+ DHs. Use `Bash run_in_background=True` with a 1-hour timeout. Progress logs stream to the background task panel.

**Full call:**
```python
r = a4.run_agent4_pipeline(
    location_file_df    = loc_df,
    lat_long_df         = lat_long_df,
    dist_df             = dist_df,
    mhdh_rate_card_path = inp / 'MHDH_RateCard.xlsx',
    out_dir             = out_dir,
    cfg                 = cfg,
    on_progress         = lambda msg: print(msg, flush=True),
)
```

**Fresh import required** — if agent4 was imported earlier in the same session, purge the module cache first:
```python
for key in list(sys.modules.keys()):
    if 'agent4' in key: del sys.modules[key]
import agent4 as a4
```

**`r['data']` key reference** — full result dict from `run_agent4_pipeline`:

| Key | Type | Description |
|---|---|---|
| `expanded_schedule_df` | DataFrame | All milkrun routes with stops, distances, costs |
| `final_assignment_df` | DataFrame | DH → truck → MH final mapping |
| `clustering_output_df` | DataFrame | Bearing-cluster groupings per MH |
| `dh_route_summary_df` | DataFrame | Per-DH: route type, truck, cost, `in_milkrun_assignment` flag |
| `absorbed_residuals_df` | DataFrame | DHs absorbed from FTL residuals into milkrun |
| `osrm_fallback_df` | DataFrame | Pairs where OSRM failed, haversine used instead |
| `total_monthly_cost` | float | Grand total Rs/month across all MHs |
| `grand_total_monthly_cost` | float | Alias for `total_monthly_cost` |
| `validation_report` | str | Full text validation report |
| `n_mhs` | int | MHs processed |
| `n_routes` | int | Total routes assigned (milkrun + FTL) |
| `n_osrm_calls` | int | OSRM calls made at runtime |
| `out_dir` | Path | Output folder used |
| `output_files` | dict[str, Path] | Paths to each written CSV/txt output file |

Output files land in the `out_dir` passed to the function. This run: `Agent4_Routing\output\run_20260716b\`.

**Results — June'26 run:**
- 36 MHs routed, 440 routes, 9,755 OSRM calls
- Total monthly cost: ₹10,34,09,227 (₹10.34 Cr/mo)
- Status: ok
- ILP failures: CENTRALHUB_L_KOL5 (KALIACHAK missing lat/lon), CENTRALHUB_L_PAT6 (BIHTA missing lat/lon) — see standalone pattern below

---

## Agent 4 Freeze-Day Engine (`agent4_freeze_day.py`) — build & run reference

**Date introduced:** 2026-07-22/23
**Agents involved:** Agent 1 (extended), Agent 4 (new additive module)

### What it is

A new engine sitting *alongside* the legacy `agent4.py` (`run_agent4_pipeline`, `run_agent4_for_mh` etc.) — it does not replace or modify anything Phase 2 depends on. Instead of costing routes against one aggregate demand snapshot, it tests **every day in the SD-plan window + 7 synthetic extreme days** (peak, median, 5 linear interpolation steps) as a candidate frozen route plan, simulates the real days' demand against each candidate (ad-hoc/spillover cost for whatever doesn't fit), and picks whichever minimizes `committed + adhoc` cost subject to `adhoc% <= cfg['adhoc_pct_limit']`. Also builds a costed baseline of the *current* H2H route network for comparison, and includes a truck-upgrade loop (bump vehicle size on routes that spill too often, keep if it lowers total cost).

**Key design decision:** the per-day candidate costing reuses `agent4.run_agent4_for_mh` verbatim (bearing clustering, FTL stripping, soft position constraints, local/zonal costing, ILP set-cover — all already correct there) rather than reimplementing route generation. Only the day-simulation/spillover/baseline layer on top is genuinely new code.

### New Agent 1 output (extends `build_sd_plan_aggregate`, doesn't add a new function)

`build_sd_plan_aggregate(..., include_daywise=True)` now also returns `result["daywise_data"]` — a DH-level (not MH1×DH) day-by-day table: `destination_hub_key`, `D<n>` (shipment counts), `D<n>_cft` (CFT volume), computed from the **same chunked pass** used for the existing lane aggregate (no second read of the 40GB NFBF file). Default `include_daywise=False` preserves the exact prior behavior/performance. Save via `save_dataframe(result["daywise_data"], out_dir / "dh_daywise_volume.csv")`.

### New agent4.py extension (still backward-compatible)

`build_location_file(..., h2h_df=None, daywise_df=None, cfg=None)` — 3 new optional params, all default `None`/no-op. When `h2h_df` given, merges `Current_MR`/`Current_Freq` from the H2H file (columns: `Dest`, `MR Number`, `frequency Final` — case-insensitive DH-name join, since the raw H2H file has inconsistent casing). When `daywise_df` given, merges the day-wise columns above. Existing callers (Phase 2) passing neither param get byte-identical output.

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

**Setup:** Reran the full freeze-day pipeline (not just dock scheduling) for CENTRALHUB_L_PAT6 (65 DHs, 30 docks) and CENTRALHUB_FPT (30 DHs, 15 docks) from `MHDH_RateCard.xlsx`'s `Docks` column, using the same `run_freeze_day_test_20260722` Agent 1/Agent 3 base data plus `Inputs\Load Profile.csv` (reused via `a3.build_load_profile_interp`, not re-implemented). A rerun of the full freeze-day search was required (not just the dock step) because the rollover mechanism changes `time_window_end` feasibility, which can change which candidate day is optimal.

**Result — freeze-day optimum (with rollover mechanism live for the first time on real data):**

| MH | Optimal day | Adhoc% | Total/mo | Baseline/mo | Savings/mo |
|---|---|---|---|---|---|
| CENTRALHUB_L_PAT6 | D67 (synthetic) | 9.3% | ₹1,24,02,461 | ₹1,49,26,163 | ₹25,23,702 (~16.9%) |
| CENTRALHUB_FPT | D60 (real day) | 8.6% | ₹37,81,737 | ₹41,32,442 | ₹3,50,705 (~8.5%) |

Both figures differ from the pre-rollover PAT6/FPT test run above (PAT6 was D47/real-day/9.2%/₹1.26Cr; FPT was D33/real-day/8.6%/₹37.7L) — this is an **expected consequence of adding the rollover mechanism**, not a regression: relaxing the feasibility window for low-Top266 DHs (`top266_shipments < low_priority_top266_threshold`, default 10) changes which routes are generable for some candidate days, which can shift the optimum. Always expect freeze-day comparison numbers to move whenever the rollover threshold config changes, even with identical demand data.

**Result — dock scheduling + speed:**

| MH | Docks total | Docks committed (95% after adhoc reserve) | Routes | Weighted Actual D1% (speed) |
|---|---|---|---|---|
| CENTRALHUB_L_PAT6 | 30 | 28 | 36 | 72.8% |
| CENTRALHUB_FPT | 15 | 14 | 19 | 75.5% |

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

**Agents involved:** Agent 4 (dock-scheduling module)
**Date:** 2026-07-23
