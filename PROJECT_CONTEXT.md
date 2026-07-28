# PROJECT_CONTEXT.md

Orchestration reference for Claude Code. Read this alongside SUPPLY_CHAIN_CONTEXT.md before starting any planning run.

---

## 1. Project Purpose

This is a linehaul network design engine for Flipkart's middle-mile logistics. Claude Code acts as the orchestrator — it reads these MD files, loads input files, calls agent functions, interprets result dicts, surfaces findings, and waits for human decisions at key checkpoints. The agents are tools, not autonomous pipelines. Claude never runs Phase 2 without explicit human approval of which MH pairs to evaluate, never bypasses preflight blockers silently, and never submits results to any external system — it hands outputs back to the user for review.

---

## 2. Folder Structure

```
Agentic tools\
│
├── SUPPLY_CHAIN_CONTEXT.md       — Network terminology, hub hierarchy, cost primitives, SLA
├── PROJECT_CONTEXT.md            — This file: data flow, orchestration, checkpoints
├── PLAYBOOK.md                   — Reusable problem→solution patterns (consult at run start)
├── RUN_HISTORY.md                — One entry per completed planning cycle
│
├── Inputs\                       — SINGLE SOURCE OF TRUTH for all raw input files
│   ├── CFT Vertical.csv                          — CFT lookup by product vertical
│   ├── Consolidated H2H June'26 Network - June'26 H2H.csv  — H2H topology for Phase 2 MR-group expansion
│   ├── D S3 FRW June'26 Resort V4 CSV Tool OP.xlsb         — Resort actuals (shipment-level, with pathways)
│   ├── JJA Alpha SD Plan (Day 1_1 May).csv       — Alpha stream same-day dispatch plan
│   ├── JJA_Alite SD Plan (Day 1_1 May).csv       — Alite stream same-day dispatch plan
│   └── JJA_NFBF SD Plan (Day 1_1 May).csv        — NFBF stream same-day dispatch plan
│   (remaining inputs — rate cards, feasibility, lat longs, load profile — to be copied here)
│
├── Agent1_DataPrep\
│   ├── AGENT1.md                 — Pre-call checklist, function reference, issue types
│   └── backend\
│       ├── agent1.py             — Data prep functions (composable tool library)
│       └── agent1_config.json    — Agent 1 runtime config
│
├── Agent2_RatesDistances\
│   └── AGENT2.md                 — File format specs, validation rules, no Python backend needed
│
├── Agent3_Clustering\
│   ├── AGENT3.md                 — Pre-call checklist, function reference, Phase 2 gate
│   ├── backend\
│   │   └── agent3.py             — Clustering and assignment functions (composable tool library)
│   └── output\
│       └── test_run\             — Outputs from last test run (reference only)
│           ├── dh_fc_mh_assignment.csv
│           ├── smh_mhlast_cost_per_shipment.csv
│           ├── agent3_summary.csv
│           ├── agent3_missing_distance_pairs.csv
│           ├── smh_missing_rate_card_edges.csv
│           ├── hub_network_map.html
│           └── validation_report_agent3.txt
│
└── Agent4_Routing\
    ├── AGENT4.md                 — Two-step pre-run gate, function reference, config reference
    ├── backend\
    │   └── agent4.py             — Routing and ILP functions (composable tool library)
    └── output\
        └── test_run\             — Outputs from last test run (reference only)
            ├── Expanded_Schedule.csv
            ├── Final_Assignment.csv
            ├── Clustering_Output.csv
            ├── DH_Route_Summary.csv
            ├── Filtered_Routes.csv
            ├── Absorbed_Residuals.csv
            ├── osrm_fallback_log.csv
            └── validation_report_agent4.txt
```

**Note on inputs:** `Inputs\` is the single source of truth. All input files must be here before a run. If a file is missing, stop and ask — do not look elsewhere.

---

## 2a. Run output folders (orchestrator convention)

**Rule:** All agent outputs live under that agent's `output\` folder. **Never write run outputs to `Inputs\`.** Never modify Agent 1's `plan_volume.csv` in place when Phase 2 accepts changes — write `plan_volume_updated.csv` to the Phase 2 subfolder instead (see AGENT3.md §8b).

### Folder name format

Every run gets its own folder, named by the orchestrator when the run starts:

```
Agent_{N}_{DDMMYY}_{HHMM}
```

| Part | Meaning | Example |
|---|---|---|
| `N` | Agent number (1, 3, or 4 — Agent 2 has no output folder) | `3` |
| `DDMMYY` | Run date: day, month, two-digit year | `270726` = 27 Jul 2026 |
| `HHMM` | Run start time, 24-hour clock | `1730` = 17:30 |

**Examples:**
- `Agent1_DataPrep\output\Agent_1_270726_1700\`
- `Agent3_Clustering\output\Agent_3_270726_1730\`
- `Agent4_Routing\output\Agent_4_270726_1830\`

Use the machine's local time at run start. Record the folder path in `RUN_HISTORY.md`.

### Agent 3 + Phase 2 nesting

Phase 2 always runs against a **specific** Agent 3 Phase 1 folder. Its outputs go in a **child folder** inside that Agent 3 run folder:

```
Agent3_Clustering\output\Agent_3_{DDMMYY}_{HHMM}\          ← Phase 1 outputs
Agent3_Clustering\output\Agent_3_{DDMMYY}_{HHMM}\Agent_3_phase2_{DDMMYY}_{HHMM}\   ← Phase 2 outputs
```

The Phase 2 folder name uses the same `Agent_3_phase2_{DDMMYY}_{HHMM}` pattern (Phase 2 start time, not Phase 1 time). It must contain at minimum: Phase 2 Excel workbooks, `plan_volume_updated.csv` (if any pair was accepted), and patched `dh_fc_mh_assignment_final.csv`.

**Legacy folders** (`run_20260715`, `run_20260727`, etc.) pre-date this convention — treat them as historical only; new runs use `Agent_{N}_{DDMMYY}_{HHMM}`.

---

## 3. Agent Roles

| Agent | Role | Python File | MD Reference |
|---|---|---|---|
| Agent 1 | Data prep — parse resort/plan files, compute CFT volumes, build per-DH aggregates | `Agent1_DataPrep\backend\agent1.py` | `Agent1_DataPrep\AGENT1.md` |
| Agent 2 | Rates and distances — validate and load distance matrix and rate cards (no Python backend) | — | `Agent2_RatesDistances\AGENT2.md` |
| Agent 3 | Clustering — assign DHs to FC_MHs by cost and D1%; run Phase 2 contested pair analysis | `Agent3_Clustering\backend\agent3.py` | `Agent3_Clustering\AGENT3.md` |
| Agent 4 | Freeze-day routing — optimal freeze day per MH, spillover/adhoc simulation, baseline comparison; then dock scheduling + Actual D1%/speed% | `Agent4_Routing\backend\agent4_freeze_day.py` + `agent4_dock_scheduling.py` | `Agent4_Routing\AGENT4.md` |
| Phase 2 ILP | Subset routing cost evaluation during Phase 2 pair analysis | `Agent4_Routing\backend\agent4.py` (Phase 2 only) | `Agent3_Clustering\AGENT3.md` §9 |

---

## 4. End-to-End Data Flow

```
Step 1 — Agent 1: Data Prep
  Inputs:  resort file (.xlsb), plan_volume, FBF day plan, SD plans (Alpha/Alite/NFBF),
           FBF network pathway, CFT vertical, MH1 tagging, LM/FM PBH, FC map, LM FDP actuals
  Outputs: plan_volume.csv
           fbf_plan_dh_aggregate.csv
           fbf_network_pathway_wide.csv

Step 2 — Agent 2: Load Rates and Distances (Claude Code reads directly, no Python)
  Inputs:  Distance Matrix (CSV), MH1-MH2 rate card (CSV), MHDH rate card (XLSX)
  Action:  Validate format and completeness per AGENT2.md; load into DataFrames
  Outputs: distance_df, mh1mh2_rate_df, mhdh_rate_df  (held in session)

Step 3 — Agent 3: DH Assignment
  Inputs:  plan_volume.csv, fbf_plan_dh_aggregate.csv, fbf_network_pathway_wide.csv,
           distance_df, mh1mh2_rate_df, mhdh_rate_df, MH1 tagging
  Outputs: dh_fc_mh_assignment.csv          (DH → serving FC_MH mapping with cost)
           smh_mhlast_cost_per_shipment.csv  (trunk leg cost breakdown)
           agent3_summary.csv               (per-MH totals, savings opportunities)

  ─── CHECKPOINT 1 ─── (see §5)

Step 4 — Phase 2 (optional, human-selected MH pairs only)
  Inputs:  dh_fc_mh_assignment.csv, H2H network file (MR-group memberships),
           distance_df, mh1mh2_rate_df, mhdh_rate_df
  Outputs: One Excel workbook per MH pair with revised DH assignments and cost comparison

  ─── CHECKPOINT 2 ─── (see §5)

Step 5 — Produce Final DH→MH List
  Input:   Checkpoint 2 accept/reject decisions
  Action:  Claude merges accepted Phase 2 changes into dh_fc_mh_assignment.csv (in the Agent 3 run folder)
           MANDATORY: call build_updated_plan_volume and save as plan_volume_updated.csv
           in the Phase 2 subfolder — never overwrite Agent 1's plan_volume.csv. See AGENT3.md §8b.
  Output:  dh_fc_mh_assignment_final.csv  (Agent 3 run or Phase 2 folder)
           plan_volume_updated.csv        (Phase 2 subfolder only)

Step 6 — Agent 4 Pre-flight
  Inputs:  dh_fc_mh_assignment_final.csv (or assignment + phase2_accepted_changes),
           dh_daywise_volume.csv (from Agent 1 with include_daywise=True),
           H2H network file, DH Feasibility.csv, Lat Longs, MHDH rate card,
           Distance Matrix, Load Profile.csv
  Actions: build_freeze_day_location_file(agent3_df, feas_df, h2h_df, daywise_df, mh_configs, cfg,
               phase2_accepted_changes={...})
           preflight_check() on the base location columns → validates ML, rate card, distance matrix
  Output:  freeze-day location DataFrame (or list of blockers)

  ─── CHECKPOINT 3 ─── (see §5)

Step 7 — Agent 4: Freeze-Day Routing + Dock Scheduling (always both)
  Step 7a — run_agent4_freeze_day_pipeline(location_df, dist_dict, latlong, mh_configs, out_dir, cfg)
  Inputs:  freeze-day location file, distance matrix (OSRM fallback at runtime), lat/long, MHDH rate card
  Outputs: Location_File.csv, Freeze_Day_Comparison.csv, Final_Assignment.csv, Expanded_Schedule.csv,
           Baseline.csv, Baseline_vs_Optimal.csv, Network_Summary.csv, spillover/adhoc logs,
           Route_Visualizer.html

  Step 7b — run_dock_scheduling_for_all_mhs(...)  [MANDATORY — part of every Agent 4 run unless user explicitly says skip]
  Inputs:  per_mh_results from 7a, same location_df, Load Profile (via a3.build_load_profile_interp)
  Outputs: Dock_Schedule.csv, Route_Speed.csv, DH_Speed.csv, Speed_Summary.csv, Dock_Utilization.html
```

---

## 5. Human Checkpoints

Claude must stop and wait for explicit user instruction at these three points. Even if the user says "run everything automatically", Claude still stops here.

### Checkpoint 1 — After Agent 3 main pipeline

**What Claude calls:**
```python
candidates = a3.build_phase2_candidates(agent3_df)        # DHs Agent 3 moved (valid P2 inputs)
cost_opps   = a3.build_cost_only_opportunities(agent3_df) # DHs Agent 3 kept (informational)
```

**What Claude presents — two separate tables:**

Table 1 (Phase 2 candidates — valid inputs): `build_phase2_candidates` output.
Columns: `from_mh`, `to_mh`, `dh_count`, `monthly_saving_rs`, `current_cost_rs`, `total_cost_rs`.
These are DHs where `current_fc_mh ≠ assigned_fc_mh` — Agent 3 proposed moving them.

Table 2 (Cost opportunities — informational only, NOT Phase 2 inputs): `build_cost_only_opportunities` output.
Columns: `destination_hub_key`, `assigned_fc_mh`, `assignment_basis`, `cost_delta_rs`.
These are DHs Agent 3 kept at the resort MH because speed guardrails blocked the cheaper option.
Never offer Table 2 rows as Phase 2 candidates — doing so would break D1% guarantees.

Also present:
- Any partial/failed issues from Agent 3's result dict
- List of DHs with D1% issues (Top266 DHs not meeting 6AM cutoff)

**What Claude asks:**
> "Table 1 shows MH pairs where Agent 3 moved DHs — valid Phase 2 candidates. Table 2 shows DHs that stayed put due to speed constraints (FYI only). Which pairs from Table 1 do you want to run Phase 2 on? (Or type 'none' to skip Phase 2 and proceed to Agent 4.)"

**Never proceed to Phase 2 without a named list of `(from_mh, to_mh)` pairs chosen from Table 1.**

---

### Checkpoint 2 — After Phase 2

**What Claude presents:**
- For each MH pair that ran Phase 2: before-cost, after-cost, delta, DHs moved, D1% impact
- Any Phase 2 workbooks written (file paths)

**What Claude asks:**
> "For each pair, accept or reject the Phase 2 changes. Accepted changes will be merged into the final DH assignment before Agent 4 runs."

**Claude accepts per-pair decisions.** Partial acceptance (accept some pairs, reject others) is valid.

**MANDATORY — the instant accept/reject resolves into a final `{DH_key: new_MH}` dict, before doing anything else:** call `build_updated_plan_volume`, save the result as `plan_volume_updated.csv` in the **Phase 2 subfolder** (not in Agent 1's output folder — Agent 1 outputs are immutable). Patch `dh_fc_mh_assignment.csv` in the Agent 3 run folder at the same moment. Review `result["issues"]` before trusting the updated plan volume. See AGENT3.md §8b.

---

### Checkpoint 3 — After build_freeze_day_location_file and preflight_check

**What Claude presents:**
- List of DHs missing ML values in DH Feasibility.csv (status=partial trigger)
- List of DHs missing lat/long (critical — ILP will fail for the whole MH cluster)
- List of DHs missing MHDH rate card entry
- Any preflight_check failures
- Confirmation that `dh_daywise_volume.csv` exists for the same Agent 1 run

**What Claude asks:**
> "These DHs will be excluded from Agent 4 or cause partial results. Do you want to fix the source files and re-run preflight, or proceed with the current set?"

**Claude does not call `run_agent4_freeze_day_pipeline` until the user responds.**

---

## 6. How Claude Reads Inputs

**At the start of every new session:**

1. **Read memory first.** Check `project_agentic_tools_rewrite.md` for current pre-run blockers and project state. Check `PLAYBOOK.md` for relevant patterns.

2. **Read all 6 MD files before running anything:**
   - `SUPPLY_CHAIN_CONTEXT.md`
   - `PROJECT_CONTEXT.md` (this file)
   - `Agent1_DataPrep\AGENT1.md`
   - `Agent2_RatesDistances\AGENT2.md`
   - `Agent3_Clustering\AGENT3.md`
   - `Agent4_Routing\AGENT4.md`

3. **Single input folder:** All raw input files are at `Inputs\`. Never read inputs from any other location. If a required file is not in this folder, stop and ask the user to copy it here.

4. **Key input paths:**
   ```
   DH Feasibility:    Inputs\DH Feasibility.csv
   Distance Matrix:   Inputs\Distance Matrix.csv
   MHDH Rate Card:    Inputs\MHDH_RateCard.xlsx
   MH1-MH2 Rate Card: Inputs\MH1-MH2 Rate Card.csv
   ```

5. **No orchestrator state file.** There is no `orchestrator_state.json` in the new structure. Claude discovers run state by scanning the Inputs folder and reading the pre-call checklists in each AGENT MD.

6. **File-to-role matching:** Each AGENT MD has a pre-call checklist table mapping file names to their roles. Use those tables to confirm which file in the Inputs folder maps to which input slot before passing it to any agent function. If a file name doesn't match clearly, ask the user — never guess.

7. **Missing inputs:** If a required input is absent from the Inputs folder, stop immediately and name the missing file. Do not proceed, substitute, or use stale outputs from a prior run.

---

## 7. Standard Result Dict

Every agent function returns:

```python
{
  "status": "ok" | "partial" | "failed",
  "data":   <value>,       # the actual output (DataFrame, dict, path, etc.)
  "issues": [...]          # list of issue strings; empty on status="ok"
}
```

**How to handle each status:**

| Status | Action |
|---|---|
| `ok` | Proceed to next step. Log completion. |
| `partial` | Log all issues. Consult the relevant AGENT MD §Issue Types to determine severity. Surface non-trivial issues to the user before proceeding. Do not silently swallow partial results. |
| `failed` | Stop. Surface all issues to the user. Do not proceed to the next step. Ask whether to fix inputs and retry or abort the run. |

---

## 8. Config Management

### Mandatory run-start question — SD plan day window

Before calling any Agent 1 function, Claude must ask:
> "The SD plan and FBF day plan files contain multiple months of data. Which 30-day window should be used?
> - Month 1: day_1 to day_30
> - Month 2: day_31 to day_60
> - Month 3: day_61 to day_91
> - Custom: specify start and end day numbers"

Then set before any Agent 1 call:
```python
cfg["fbf_plan_day_start"] = <start>
cfg["fbf_plan_day_end"] = <end>
cfg["fbf_plan_avg_divisor"] = <end - start + 1>
```

This affects `build_sd_plan_aggregate` and `build_fbf_aggregate`. Never use default values for a real run — always confirm with user.

---

Each agent loads its runtime config from a JSON file in its backend folder at the start of a run.

**Loading pattern:**
```python
# Agent 1
from agent1 import load_agent1_config
cfg = load_agent1_config("Agent1_DataPrep/backend/agent1_config.json")

# Agent 3
from agent3 import load_agent3_config
cfg = load_agent3_config("Agent3_Clustering/backend/agent3_config.json")

# Agent 4
from agent4 import load_agent4_config
cfg = load_agent4_config("Agent4_Routing/backend/agent4_config.json")
```

**Per-run overrides** (pass a modified dict, do not edit the JSON file mid-run):
```python
cfg = load_agent4_config(path)
cfg["default_max_hops"] = 4        # override for this run only
result = run_agent4_for_mh(..., cfg=cfg)
```

**Config keys that most affect results:**

| Key | Agent | Effect |
|---|---|---|
| `default_top266_threshold` | Agent 3 | Minimum daily Top266 shipments at a DH to trigger speed-based assignment. Below this threshold → cost-based assignment. Raising this value forces more DHs onto speed-optimised routes (potentially higher cost). Default: 5. |
| `default_max_hops` | Agent 4 | Maximum DH stops per milkrun route. Higher = longer routes, lower cost per km, but more complexity and later final-stop arrivals. |
| `local_zonal_distance_threshold_km` | Agent 4 | Distance cutoff (default 200 km) for switching from local Rs/km to zonal Rs/km. Changing this affects which DHs are classified as zonal and their cost. |

---

## 9. Problem-First Framework — How to Approach Any Task

This section applies to every task — pipeline runs, analysis, new logic, debugging, or anything else. Always follow this sequence. Do not skip steps.

### Step 1 — Understand the problem in supply chain terms
Before touching any code or file, ask: what is this task actually trying to solve in the network? Which layer does it belong to — network topology, demand, cost, speed, constraints, or baseline comparison? Re-read `SUPPLY_CHAIN_CONTEXT.md §9` (How Inputs Connect) if needed.

### Step 2 — Map to existing inputs and agents
Which input files (see `INPUT_CONTEXT.md`) are relevant? Which agent functions (see AGENT MDs) already handle this? Check `SANDBOX\` for any previously approved custom functions. Check `PLAYBOOK.md` for prior patterns.

### Step 3 — Design the approach
Write out in plain language: what will be done, in what order, using which functions and which inputs. If new code is needed, describe what it does without writing it yet.

### Step 4 — Explain and wait for approval
Present the approach to the user before doing anything. One short paragraph: what you will do and why. Wait for explicit approval before proceeding. Exception: if the task is a standard pipeline run already defined in these MDs, proceed directly.

### Step 5 — Execute using existing functions first
Use agent functions directly. Only write new code if no agent function covers the need — and only after Step 4 approval. Save any new code to `SANDBOX\`.

### Step 6 — Report and log
Report results concisely. If a new pattern was discovered, draft a PLAYBOOK entry and ask the user if it should be added.

---

## 10. PLAYBOOK and RUN_HISTORY

**PLAYBOOK.md**

Stores reusable problem→solution patterns discovered across runs (e.g. "when OSRM fails for a city cluster, pre-fill distance pairs from Google Maps export"). Claude checks PLAYBOOK at the start of every task and mentions any relevant entry to the user before starting. Claude never auto-appends to PLAYBOOK — it drafts a candidate entry and asks the user whether to add it.

**RUN_HISTORY.md**

One entry per completed planning cycle, appended automatically by Claude after a run completes. Format:

```markdown
## [Cycle Name] — [YYYY-MM-DD]
- **Inputs used:** [list key input files and their dates]
- **Agent 3 summary:** [total cost, # DHs reassigned, D1% DHs passing]
- **Phase 2 decisions:** [pairs evaluated, pairs accepted, cost delta]
- **Agent 4 summary:** [# MHs run, total routes, total cost, status]
- **Notes:** [any issues, partial results, manual fixes applied]
```

---

## 10. Known Pre-run Blockers

See `CLAUDE.md § Known Pre-run Blockers` — maintained there as the single source of truth.
