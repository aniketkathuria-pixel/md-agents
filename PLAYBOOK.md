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
