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
- MHs routed: 36 (AJLX and IXA3X skipped — missing MHDH rate card entries; 4 DHs excluded)
- Total routes: 440 (milkrun + FTL combined)
- OSRM calls: 9,755 (all distance gaps resolved via OSRM at runtime)
- Total monthly cost: ₹10,34,09,227 (Rs 10.34 Cr/mo)
- Status: ok (pipeline return status=ok)
- Output folder: Agent4_Routing\output\run_20260716b

### Per-MH Summary

| MH | DHs | Milkrun | FTL | Missing | Cost (₹/mo) |
|---|---|---|---|---|---|
| BAG | 9 | 4 | 0 | 0 | 6,10,122 |
| FPT | 30 | 15 | 2 | 0 | 33,04,182 |
| FRN | 33 | 19 | 13 | 0 | 41,23,531 |
| L_AGR2 | 12 | 8 | 0 | 0 | 13,73,688 |
| L_AMD3 | 21 | 12 | 2 | 0 | 24,87,244 |
| L_AURPRC1 | 6 | 4 | 0 | 0 | 6,15,122 |
| L_BHB1 | 37 | 15 | 0 | 0 | 50,67,421 |
| L_BHO1 | 6 | 3 | 0 | 0 | 5,23,606 |
| L_CJB3 | 36 | 19 | 2 | 0 | 56,63,169 |
| L_GOP1 | 5 | 4 | 0 | 0 | 4,68,906 |
| L_HBL1 | 20 | 14 | 1 | 0 | 37,12,577 |
| L_IDRL1 | 13 | 7 | 0 | 0 | 16,78,950 |
| L_JAIX4 | 12 | 8 | 0 | 0 | 13,82,301 |
| L_KLM1 | 5 | 3 | 0 | 0 | 4,13,146 |
| L_KOL5 | 54 | 0 | 4 | 1 | 4,67,741 |
| L_LKO3 | 32 | 20 | 0 | 0 | 44,79,467 |
| L_MUMX | 30 | 19 | 7 | 0 | 45,35,247 |
| L_NGP1 | 15 | 9 | 0 | 0 | 17,43,162 |
| L_PAT6 | 65 | 0 | 2 | 1 | 2,32,317 |
| L_PUNML1 | 10 | 7 | 1 | 0 | 8,94,684 |
| L_RAI1 | 7 | 4 | 0 | 0 | 5,74,665 |
| L_RJKPRC1 | 7 | 5 | 0 | 0 | 10,66,427 |
| L_RNCSF1 | 23 | 11 | 0 | 0 | 34,47,094 |
| L_SGR1 | 53 | 27 | 1 | 0 | 73,20,404 |
| L_SIL1 | 13 | 7 | 2 | 0 | 13,88,260 |
| L_SRTSFL1 | 6 | 2 | 0 | 0 | 4,44,560 |
| L_VGA1 | 14 | 9 | 0 | 0 | 15,61,751 |
| L_VNS4 | 17 | 10 | 0 | 0 | 16,70,782 |
| L_VZG1 | 18 | 10 | 0 | 0 | 22,03,443 |
| L_YKBX1 | 35 | 19 | 0 | 0 | 71,91,254 |
| MPL1 | 63 | 40 | 9 | 0 | 1,09,16,648 |
| PBI | 51 | 25 | 3 | 0 | 1,76,86,908 |
| THV | 33 | 24 | 8 | 0 | 41,60,448 |

### Notes
- AJLX and IXA3X skipped per user instruction — MHDH rate card still missing for these 2 MHs; their DHs (ABA, BLO, LGI, LWT) excluded from this run
- L_KOL5: ILP FAILED for milkrun cluster — SATELLITEHUB_KALIACHAK uncovered; 0 milkrun routes, 4 FTL only
- L_PAT6: ILP FAILED for milkrun cluster — SATELLITEHUB_BIHTA uncovered; 0 milkrun routes, 2 FTL only (cost ₹2.32L is FTL-only, highly understated)
- OSRM blocked on MNR→PITHORAGARH and GHAZIABAD→SHAMLID/ROORKEE/RUDRAPRAYAG (WinError 10013 firewall); haversine fallback used
- 4 stale lat/long rows in Lat Longs.xlsx (REVX, AMD_FLEX, CJB_flex, LKO_FLex) cause preflight status=partial noise but do not affect routing
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
- Run scope: CENTRALHUB_L_PAT6 only (most DHs in network: 65)
- Phase 2 changes applied to location file before Agent 4 (VNS4→LKO3 5 DHs overridden)
- Routes: 31 milkrun + 2 FTL (DANAPUR 24ft, FKKNK 24ft) = 33 total
- Missing DHs: 0 (all 65 covered)
- Monthly cost: ₹1,07,81,103
- Status: ok
- Output: Agent4_Routing\output\run_20260721\

### Notes
- Agent 4 scoped to PAT6 only per user instruction ("run for only one MH, the one with most DHs")
- First run had ILP failure (BIHTA uncovered) → user added BIHTA to Lat Longs.xlsx → rerun resolved all 65 DHs
- OSRM timed out on several NAUGACHIA and SIWANSPLIT inter-hub pairs (firewall); haversine fallback used
- Known pre-run blockers unchanged: AJLX, IXA3X missing rate card; JLRSF1, KLM1 rate card missing (pre-existing)
- Preflight status=failed due to AJLX/IXA3X (known) and 11 DHs missing distance data — not blocking for PAT6 run

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
- Full internal progress logging (per-MH, per-freeze-day-candidate, per-truck-upgrade-iteration) added this cycle via an `on_progress` callback, matching legacy `agent4.py`'s convention — verified streaming correctly to a background task log.
- The peak-day-should-be-0%-adhoc question that surfaced the third bug is a good general sanity check to re-run after any future change to the spillover/FTL logic — verify `compute_spillover_day` on the max-demand synthetic day shows zero spillover before trusting a run's numbers.
