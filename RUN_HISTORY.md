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
