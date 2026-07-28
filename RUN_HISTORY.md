# RUN_HISTORY.md

One entry per completed planning cycle. Claude appends automatically after each run completes.

---

## Entry format

```markdown
## [Cycle Name] — [YYYY-MM-DD]

### Inputs Used
- Resort file: [filename]
- SD plan window: day_[X] to day_[Y]
- Distance matrix: [filename + date]
- Rate cards: [filenames]
- DH Feasibility: [filename]

### Agent 3 Results
- DHs assigned: [total] (speed: [n], cost: [n], errors: [n])
- Top266 threshold used: [value]
- Weighted avg D1%: [value]
- Total network cost: ₹[value]/month
- MH pairs flagged for Phase 2: [list or "none"]

### Phase 2 Results
- Pairs evaluated: [list]
- Pairs accepted: [list or "none"]
- Pairs rejected: [list or "none"]
- Net monthly savings from accepted changes: ₹[value]

### Agent 4 Results
- MHs routed: [n]
- Total routes: [n]
- Total monthly cost: ₹[value]
- Status: [ok / partial — list issues]
- Output folder: [path]

### Notes
- [Any manual fixes applied, data quality issues found, decisions made]
```

---

## Run log

## June'26 Agent 1 Run — 2026-07-15

### Inputs Used
- Resort file: D S3 FRW June'26 Resort V4 CSV Tool OP.xlsb (452,390 rows)
- SD plan window: day_32 to day_61 (divisor=30, June)
- Alpha SD plan: JJA Alpha SD Plan (Day 1_1 May).csv
- Alite SD plan: JJA_Alite SD Plan (Day 1_1 May).csv
- NFBF SD plan: JJA_NFBF SD Plan (Day 1_1 May).csv (42 GB, path mode chunked)
- CFT vertical: CFT Vertical.csv (375 rows)
- MH-DH mapping: mh_dh_mapping.csv (2,616 rows)
- MH1 tagging: not provided (optional; skipped)

### Agent 1 Results
- SD aggregate (Pipeline D): ok — 216,169 (MH1 × LMHub) demand rows
- Join demand: partial — 246,284 resort lanes had no SD plan match (expected; filtered by build_plan_volume)
- plan_volume.csv: 206,106 rows × 35 columns
- Output: Agent1_DataPrep\output\run_20260715\plan_volume.csv

### Agent 3 Results
- Not yet run

### Phase 2 Results
- Not yet run

### Agent 4 Results
- Not yet run

### Notes
- build_sd_plan_aggregate called in path mode (not DataFrame mode) to avoid OOM on 42 GB NFBF file
- No MH1 tagging file in Inputs — source_type/stream for NFBF lanes derived from SD plan only (MH vs PH distinction not applied)

---

## June'26 Full Pipeline Run — 2026-07-20

### Inputs Used
- Resort file: D S3 FRW June'26 Resort V4 CSV Tool OP.xlsb (carry-forward from Agent 1 run)
- SD plan window: day_32 to day_61 (divisor=30, June)
- Agent 3 output: Agent3_Clustering\output\run_20260716b\dh_fc_mh_assignment.csv (820 rows)
- Phase 2 output: Agent3_Clustering\output\run_20260716b_phase2c\
- Location file: Inputs\Location_File_final.xlsx (820 rows, 0 null ML after user fix)
- Distance matrix: Inputs\Distance Matrix.csv
- MHDH rate card: Inputs\MHDH_RateCard.xlsx (JLRSF1, KLM1 added by user this session)
- DH Feasibility: Inputs\DH Feasibility.csv (12 null-ML rows fixed by user this session)
- Lat Longs: Inputs\Lat Longs.xlsx

### Agent 3 Results
- DHs assigned: 820 total (132 moved by Agent 3, 688 unchanged)
- Phase 2 candidates (build_phase2_candidates): 5 valid pairs
- Savings table was regenerated this session — previous table had bug grouping by assigned_fc_mh instead of current_fc_mh, causing false pairs (e.g. VZG1→VGA1 had 0 flagged DHs)

### Phase 2 Results
- Pairs evaluated: VNS4→LKO3, VNS4→GOP1, PAT6→GOP1
- Pairs accepted: VNS4→LKO3 (4 DHs, ₹1.91L/mo saving), VNS4→GOP1 (4 DHs, ₹4.29L/mo saving)
- Pairs rejected: PAT6→GOP1
- Net monthly savings from accepted changes: ₹6.20L/mo (8 DHs overridden in location file)
- Phase 2 run: run_20260716b_phase2c

### Agent 4 Results
- Not run (historical snapshot run removed — use freeze-day + dock scheduling per PLAYBOOK.md)


### Notes
- Phase 2 savings table bug discovered and documented this session — see PLAYBOOK.md for pattern

---

## June'26 Refresh Run (run_20260721) — 2026-07-21

### Inputs Used
- SD plan window: day_32 to day_61 (divisor=30, June; Day_1 = 1 May)
- Alpha SD plan: JJA Alpha SD Plan (Day 1_1 May).csv
- Alite SD plan: JJA_Alite SD Plan (Day 1_1 May).csv
- NFBF SD plan: JJA_NFBF SD Plan (Day 1_1 May).csv
- CFT vertical: CFT Vertical.csv
- MH-DH mapping: mh_dh_mapping.csv
- FBF master: Actuals FBF Master.xlsx, Plan fbf master.xlsx
- FBF network pathway: Consolidated H2H June'26 Network - June'26 H2H.csv
- Distance matrix: Inputs\Distance Matrix.csv
- MHDH rate card: Inputs\MHDH_RateCard.xlsx
- DH Feasibility: Inputs\DH Feasibility.csv
- Lat Longs: Inputs\Lat Longs.xlsx
- Run tag: run_20260721

### Agent 1 Results
- Pipeline A (plan_volume): ok — 206,106 rows
- Pipeline C (fbf_plan_dh_aggregate): ok — 820 rows
- Pipeline E (fbf_network_pathway_wide): partial — 38 rows (79 unmapped DCs expected)
- Output: Agent1_DataPrep\output\run_20260721\

### Agent 3 Results
- DHs assigned: 820 total (552 speed-based D1%, 268 cost-based)
- DHs moved from prior assignment: 132
- OSRM auto-fetch: 254 missing distance pairs resolved at runtime
- Top 3 Phase 2 candidates: VNS4→LKO3 (₹13.04L est), VNS4→GOP1 (₹6.25L est), PAT6→GOP1 (₹6.51L est)
- Output: Agent3_Clustering\output\run_20260721\dh_fc_mh_assignment.csv

### Phase 2 Results
- Pairs evaluated: VNS4→LKO3, VNS4→GOP1, PAT6→GOP1 (top 3 from Agent 3)
- ₹5L/month filter applied (ILP-confirmed actual savings)
- Pairs accepted: VNS4→LKO3 — ₹8.07L/month (5 DHs: ALD, ALDNAINI, LALGANJAJHARA, PTG1, SLN)
- Pairs rejected: VNS4→GOP1 (₹3.85L < ₹5L), PAT6→GOP1 (₹4.06L < ₹5L)
- Net monthly saving: ₹8.07L/month
- Three runtime monkey-patches applied to bridge agent3.py / agent3_phase2.py / agent4.py compatibility (see PLAYBOOK.md)

### Agent 4 Results
- Not run (historical snapshot run removed — use freeze-day + dock scheduling per PLAYBOOK.md)


### Notes
- Phase 2 monkey-patches applied this session (see PLAYBOOK.md) — resolved before subsequent runs

---

## Agent 4 Freeze-Day Engine — First Real-Data Test (PAT6 + FPT) — 2026-07-23

### Inputs Used
- Reused Agent 1 output from `run_freeze_day_test_20260722` (day window: day_32 to day_61, June, Day_1 = 1 May)
- New: `dh_daywise_volume.csv` — day-wise DH demand from `build_sd_plan_aggregate(..., include_daywise=True)`
- Reused Agent 3 output from `run_freeze_day_test_20260722`: `dh_fc_mh_assignment.csv` (820 DHs)
- H2H: Consolidated H2H June'26 Network - June'26 H2H.csv (for Current_MR/Current_Freq baseline)
- Distance matrix: Inputs\Distance Matrix.csv
- MHDH rate card: Inputs\MHDH_RateCard.xlsx
- DH Feasibility: Inputs\DH Feasibility.csv
- Lat Longs: Inputs\Lat Longs.xlsx
- Scope: CENTRALHUB_L_PAT6 (65 DHs) + CENTRALHUB_FPT (30 DHs) only, 95 DHs total

### Agent 3 Results
- Carried forward unchanged from `run_freeze_day_test_20260722` (552 speed-assigned, 268 cost-assigned, 0 errors)

### Phase 2 Results
- Not run this cycle (new-engine validation only)

### Agent 4 Results (new freeze-day engine, `agent4_freeze_day.py`)
- **This was a 3-pass run.** Pass 1 surfaced two real bugs (day-column-numbering collision corrupting real demand, and a mislabeling bug from the same root cause). Pass 2 (after those fixes) prompted a user question — "why isn't the peak day's adhoc% zero?" — which led to discovering a third bug: FTL/dedicated residual double-counted against milkrun capacity, affecting every spillover simulation call in the engine. Pass 3 (below) is the final corrected result. See PLAYBOOK.md "Known patterns" for all three.
- Also implemented and verified during this cycle: freq-2 day-reversion for spillover simulation, real shift-adjusted baseline departure timing (was hardcoded to 0), `Per_Day_Route_Log.csv`/`All_Days_Spillover.csv`/`Best_Network_Spillover.csv`, `Adhoc_Route_Summary.csv` (standing-backup-route suggestions), `Route_Visualizer.html`, and a callable single-day ad-hoc runner (`run_freeze_day_single_day`) for out-of-band requests like "run PAT6 for day 16".
- **Final corrected results (post all 3 bug fixes):**

| MH | DHs | Optimal day | Adhoc% | Committed/mo | Adhoc/mo | Total/mo | Baseline/mo | Savings/mo |
|---|---|---|---|---|---|---|---|---|
| CENTRALHUB_L_PAT6 | 65 | D47 (real day) | 9.2% | ₹1,18,12,529 | ₹7,57,089 | ₹1,25,69,618 | ₹1,49,62,039 | ₹23,92,421 (~16.0%) |
| CENTRALHUB_FPT | 30 | D33 (real day) | 8.6% | ₹35,69,667 | ₹2,04,453 | ₹37,74,120 | ₹41,18,416 | ₹3,44,297 (~8.4%) |

- Status: ok
- Output folder: `Agent4_Routing\output\run_freeze_day_final_20260723\`

### Notes
- FPT now finds a day within the 10% `adhoc_pct_limit` (8.6%) for the first time — the earlier 29–31% figures (pass 2) were themselves a symptom of the double-counted-residual bug, not a real network characteristic.
- Mandatory OSRM reporting: 2 pairs failed via network timeout (not missing data) — `SATELLITEHUB_KURALI → SATELLITEHUB_YAMUNANAGAR`, `SATELLITEHUB_KURALI → SATELLITEHUB_BARNALA1`. Retry if these pairs need to be filled.
- Several ad-hoc routes flagged "Consider as standing backup route" (recur ≥7 times/month), e.g. `CENTRALHUB_L_PAT6 → SATELLITEHUB_TAMKUHIRAJ → CENTRALHUB_L_PAT6` (9 uses, ₹74,809 total) — candidates for promotion to a standing route.
- Full internal progress logging (per-MH, per-freeze-day-candidate, per-truck-upgrade-iteration) added this cycle via an `on_progress` callback, matching the freeze-day engine's `on_progress` convention — verified streaming correctly to a background task log.
- The peak-day-should-be-0%-adhoc question that surfaced the third bug is a good general sanity check to re-run after any future change to the spillover/FTL logic — verify `compute_spillover_day` on the max-demand synthetic day shows zero spillover before trusting a run's numbers.

---

## Agent 4 Dock Scheduling + CX-Cutoff + Speed Engine — First Real-Data Test (PAT6 + FPT) — 2026-07-23

### Inputs Used
- Agent 1 output: `run_freeze_day_test_20260722` (`dh_daywise_volume.csv`, day window day_32–day_61)
- Agent 3 output: `run_20260716b\dh_fc_mh_assignment.csv`, filtered to CENTRALHUB_L_PAT6 + CENTRALHUB_FPT
- H2H: Consolidated H2H June'26 Network - June'26 H2H.csv
- Distance matrix: Inputs\Distance Matrix.csv
- MHDH rate card: Inputs\MHDH_RateCard.xlsx — first run to use its new `Docks` column (PAT6=30, FPT=15)
- DH Feasibility: Inputs\DH Feasibility.csv
- Lat Longs: Inputs\Lat Longs.xlsx
- **New:** Load Profile.csv — first run where this becomes a required Agent 4 input (CX-cutoff capture-fraction lookup via `a3.build_load_profile_interp`)
- Scope: CENTRALHUB_L_PAT6 (65 DHs) + CENTRALHUB_FPT (30 DHs) only

### Agent 3 Results
- Carried forward unchanged from prior assignment (no re-run this cycle)

### Phase 2 Results
- Not run this cycle

### Agent 4 Results (freeze-day engine + new `agent4_dock_scheduling.py`)
- Full freeze-day search rerun for both MHs (required — the new rollover mechanism changes route feasibility, which can shift the optimal candidate day) followed by dock scheduling as a separate post-processing step (`run_dock_scheduling_for_all_mhs`), per the documented call pattern.

| MH | DHs | Optimal day | Adhoc% | Total/mo | Baseline/mo | Savings/mo | Docks (total/committed) | Routes | Speed% |
|---|---|---|---|---|---|---|---|---|---|
| CENTRALHUB_L_PAT6 | 65 | D67 (synthetic) | 9.3% | ₹1,24,02,461 | ₹1,49,26,163 | ₹25,23,702 (~16.9%) | 30 / 28 | 36 | 72.8%* |
| CENTRALHUB_FPT | 30 | D60 (real day) | 8.6% | ₹37,81,737 | ₹41,32,442 | ₹3,50,705 (~8.5%) | 15 / 14 | 19 | 75.5%* |

- Status: ok (both the freeze-day pipeline and dock scheduling; no ILP cluster failures, no `dock_schedule_infeasible`)
- Output folder: `Agent4_Routing\output\run_dock_sched_20260723\`
- \*Speed% superseded — authoritative FPT post D1 fix: **75.0%** (`Agent_4_280726_1709_FPT`). PAT6 needs re-run post-fix.

### Notes
- Optimal freeze days moved vs. the pre-rollover test run (PAT6: D47→D67, FPT: D33→D60) — expected, caused by the rollover mechanism relaxing feasibility windows for low-Top266 DHs (`top266_shipments < 10`), not a bug. See PLAYBOOK.md.
- Only 1 of 55 total routes across both MHs needed a dock-forced departure shift away from its unconstrained-ideal TMS (PAT6 Route 30, preponed 180 min, speed dropped to 35.3% on that route) — dock contention is rare at current dock counts.
- Weighted Actual D1%/speed (genuine post-routing metric, distinct from Agent 3's predictive D1%): PAT6 72.8%, FPT 75.5%.

---

## Agent 3 Run — 2026-07-27

### Inputs Used
- Agent 1 output (carry-forward): `Agent1_DataPrep\output\run_20260715\` (plan_volume 206,106 rows; day_32–61 June window)
- Distance matrix: `Inputs\Distance Matrix.csv`
- MH1-MH2 rate card: `Inputs\MH1-MH2 Rate Card.csv`
- Plan fbf master: `Inputs\Plan fbf master.xlsx`
- Lat Longs: `Inputs\Lat Longs.xlsx`
- Load Profile: `Inputs\Load Profile.csv`

### Agent 3 Results
- Status: partial (1 missing rate-card edge group: `CENTRALHUB_L_MUMB→CENTRALHUB_L_MUMX`)
- DHs assigned: 820 (552 speed, 268 cost, 0 errors)
- DHs moved from resort: 131
- OSRM: 254 missing distance pairs resolved at runtime
- Output: `Agent3_Clustering\output\run_20260727\`

### Phase 2 Results
- Pairs evaluated: VNS4→GOP1 (user-selected)
- Pairs accepted: VNS4→GOP1 — 3 DHs moved (BASTI, MAHARAJGANJ, MHJ → GOP1); net ₹3.85L/mo saving (MHDH +₹4.52L, MHMH −₹0.68L)
- Pairs rejected: none
- `plan_volume_updated.csv` saved to Phase 2 folder (206,104 rows); Agent 1 `run_20260715\plan_volume.csv` restored to pristine 206,106 rows (not overwritten)
- Output: `Agent3_Clustering\output\run_20260727_phase2\` (legacy naming — new runs use `Agent_3_{DDMMYY}_{HHMM}` per PROJECT_CONTEXT §2a)

### Agent 4 Results
- Scope: `CENTRALHUB_FPT` only (30 DHs) — freeze-day + dock scheduling (correct pipeline)
- Optimal freeze day: **D60** (real day), adhoc **8.6%**
- Total/mo: **₹37,81,737** (committed ₹35,72,179 + adhoc ₹2,09,557)
- Baseline/mo: ₹41,32,442 → savings **₹3,50,705 (~8.5%)**
- Speed% (post-dock): **90.2%** — **invalid** (D1 checked on recurring clock, not absolute; superseded)
- Preflight: failed on 4 DHs missing distance (OSRM at runtime — all have lat/long)
- Daywise source: `run_freeze_day_test_20260722` (run_20260715 lacks daywise)
- Assignment source: `run_20260727\dh_fc_mh_assignment_final.csv`
- Status: ok (freeze-day + dock scheduling)
- Runtime: ~252s
- Output: `Agent4_Routing\output\Agent_4_280726_1557_FPT\` (superseded)

### Agent 4 Re-run (baseline speed + expanded schedule) — 2026-07-28
- Scope: `CENTRALHUB_FPT` only (30 DHs) — full freeze-day + dock scheduling with `h2h_df` for baseline speed
- Optimal freeze day: **D60** (real day), adhoc **8.6%**
- Cost/mo: **₹37,81,737** optimal vs **₹41,32,442** baseline → savings **₹3,50,705 (~8.5%)** (unchanged vs prior run)
- Speed/mo: **63.4%** baseline → **90.2%** proposal (+26.8 pp) — **invalid** (D1 on recurring clock; superseded)
- Top DH speed gains / regression from this run are not authoritative
- Routes/docks: 19 routes | 15 docks (14 committed)
- Preflight: failed on 4 DHs missing distance (OSRM at runtime)
- Daywise: `run_freeze_day_test_20260722` | Assignment: `run_20260727\dh_fc_mh_assignment_final.csv`
- Status: ok (freeze-day); partial (dock — 66 `baseline_tms_inconsistent` issues from full-network H2H scan, 2 FPT-relevant: JMU1 MR, Ambala-Yamunanagar MR)
- Runtime: ~325s
- Output: `Agent4_Routing\output\Agent_4_280726_1644_FPT\` (superseded — see D1 fix re-run below)

### Agent 4 Re-run (D1 absolute timeline fix) — 2026-07-28
- Scope: `CENTRALHUB_FPT` only (30 DHs) — freeze-day + dock scheduling after D1/speed fix (Day 0 midnight anchor; 6 AM Day 1 = 1800 min absolute)
- Optimal freeze day: **D60** (real day), adhoc **8.6%** (unchanged)
- Cost/mo: **₹37,81,737** optimal vs **₹41,32,442** baseline → savings **₹3,50,705 (~8.5%)** (unchanged)
- Speed/mo: **63.4%** baseline (H2H TMS) → **75.0%** proposal (dock-scheduled) → **+11.6 pp**
- Route 15 regression: TMS 234.5 → 1104.5 (evening); Route_Speed **42.5%** (was 99.6% pre-fix); KHANNA1 `Arrival_Time` **1800** abs
- Routes/docks: 19 routes | 15 docks (14 committed)
- Preflight: failed on 4 DHs missing distance (OSRM at runtime)
- Daywise: `run_freeze_day_test_20260722` | Assignment: `run_20260727\dh_fc_mh_assignment_final.csv`
- Status: ok (freeze-day); partial (dock — `baseline_tms_inconsistent` from H2H scan)
- Runtime: ~272s
- Output: `Agent4_Routing\output\Agent_4_280726_1709_FPT\` — **authoritative FPT speed/cost run**

### Notes
- Final assignment: `Agent3_Clustering\output\run_20260727\dh_fc_mh_assignment_final.csv`
- Phase 2 kept GHAZIPUR and MAU at VNS4 (pool DHs, not moved by ILP)
- Speed% from runs before `Agent_4_280726_1709_FPT` (including 1557/1644 and Jul-23 PAT6+FPT combined 72.8%/75.5%) used pre-fix D1 logic — costs remain valid; speed columns need re-run for authoritative numbers
- PLAYBOOK updated 2026-07-28: **"D1 speed scoring — absolute order-day timeline"**
