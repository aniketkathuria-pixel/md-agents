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

### MH1 name mismatch causes silent zero costs in Agent 3
**Problem:** Agent 3 produces very low or zero MH→MH costs for all lanes, making cost_delta_rs appear huge for every DH
**Root cause:** Hub names in MH1-MH2 rate card do not match hub names in plan_volume.csv. cost_lookup returns None for every edge → silent zero cost
**Solution:** Cross-check MH1 column values in rate card against MH1/MH2 columns in plan_volume. Normalise naming (uppercase, no extra spaces) in rate card to match plan_volume format. Rebuild cost_lookup and re-run Agent 3.
**Agents involved:** Agent 2, Agent 3
**Date:** 2026-07-15
