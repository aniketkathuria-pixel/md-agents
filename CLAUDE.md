# CLAUDE.md — Linehaul Planning Engine

Read this file at the start of every session and re-read whenever reminded.

---

## Working Style

Work like a capable employee — take ownership, execute, report back.

- **Do first, report after** — don't ask for permission on steps that are already defined in the MDs. Read the MDs, understand the task, execute it.
- **Confirm only when it matters** — ask the user when: (a) a required input is missing, (b) a result is ambiguous and the wrong choice could waste significant time, (c) a human checkpoint is reached (see Critical Rules). Don't ask about things already answered in the MDs.
- **Be concise** — report results in a short summary table. No lengthy explanations unless something went wrong.
- **Flag blockers clearly** — if something fails or is partial, state what failed, why, and what the user needs to do to fix it. One paragraph max.
- **Never ask what you can figure out yourself** — if a file's role is ambiguous, check its column headers against AGENT MDs before asking. If a config value has a sensible default, use it and mention it in the summary.
- **Own the run** — once the user gives a task, drive it to completion. Don't stop after each step waiting for approval unless a checkpoint is reached.

---

## Project Root
`Agentic tools\` (the folder containing this file)

## All Inputs — Single Location
`Inputs\`
Never read inputs from any other location. If a file is missing here, stop and ask.

## Mandatory Self-Check — Before Every Action

Before doing anything, ask yourself: have I read the required MDs for this task in this session?
If the answer is no, or you are unsure — stop and read them now. Do not rely on memory from a previous session or from earlier in this chat. MDs are the source of truth, not your recall.

If you are mid-task and realise you missed a required MD — stop, read it, then continue. Do not guess.

---

## Reading Index — What to Read for Each Task Type

| Task | Must Read (in order) |
|---|---|
| Any pipeline run (Agent 1–4) | SUPPLY_CHAIN_CONTEXT.md → PROJECT_CONTEXT.md → INPUT_CONTEXT.md → relevant AGENT MD → PLAYBOOK.md |
| Understanding an input file | INPUT_CONTEXT.md → SUPPLY_CHAIN_CONTEXT.md |
| Writing new logic or a new function | SUPPLY_CHAIN_CONTEXT.md → PROJECT_CONTEXT.md → relevant AGENT MD → SANDBOX\README.md |
| Debugging a pipeline failure | Relevant AGENT MD → PROJECT_CONTEXT.md → PLAYBOOK.md |
| Phase 2 run | SUPPLY_CHAIN_CONTEXT.md → AGENT3.md → AGENT4.md → PROJECT_CONTEXT.md |
| Any task not listed above | SUPPLY_CHAIN_CONTEXT.md → PROJECT_CONTEXT.md → INPUT_CONTEXT.md |

---

## Read These MDs Before Any Run
1. `SUPPLY_CHAIN_CONTEXT.md` — network terminology, hub hierarchy, cost primitives
2. `PROJECT_CONTEXT.md` — data flow, checkpoints, config management, problem-first framework
3. `INPUT_CONTEXT.md` — what every input file means in supply chain terms
4. `PLAYBOOK.md` — check for relevant patterns before starting
5. `Agent1_DataPrep\AGENT1.md`
6. `Agent2_RatesDistances\AGENT2.md`
7. `Agent3_Clustering\AGENT3.md`
8. `Agent4_Routing\AGENT4.md`

## Critical Rules

### Agent files are tools — never rewrite them
- agent1.py, agent2 (no file), agent3.py, agent4.py are callable libraries — import and call their functions directly
- Never write your own data processing code that replicates what an agent function already does
- Never modify any agent Python file (agent1.py, agent3.py, agent4.py)
- Never modify any MD file (AGENT1.md, AGENT2.md, AGENT3.md, AGENT4.md, SUPPLY_CHAIN_CONTEXT.md, PROJECT_CONTEXT.md, CLAUDE.md)
- The only files Claude can write to or modify are: RUN_HISTORY.md and PLAYBOOK.md (only after user approval for PLAYBOOK)
- Never ask the user to identify a file by name or purpose — read the file's column headers and match against the pre-call checklist in the relevant AGENT MD to identify its role. Only ask the user if two files have identical column sets and the role is genuinely ambiguous after header inspection.
- Never write throwaway run scripts — call agent functions directly in the session
- File size does not affect Claude's token usage — pandas handles large files in Python memory, Claude only sees the result dict
- If a function call fails mid-run, re-call only the failed function with the same inputs — do not restart from scratch

### When to write your own code
- Only for file manipulation tasks that no agent function covers (e.g. renaming columns to match expected format, merging two CSVs, filtering rows before passing to an agent)
- Always a thin wrapper — load file, transform, pass to agent function. Never replicate agent logic.
- Always use the agent's own config loader function — never load config JSON directly with json.load(). Use: a1.load_agent1_config(), a3.load_agent3_config(), a4.load_agent4_config()

### Before writing any new logic — mandatory sequence
1. Understand the problem in supply chain terms first (re-read SUPPLY_CHAIN_CONTEXT.md if needed)
2. Check if an existing agent function already solves it — read the relevant AGENT MD
3. Check SANDBOX\ for any previously approved functions that solve it
4. If nothing exists: design the approach, explain it to the user in plain terms, wait for approval
5. Only after approval: write the code and save it to SANDBOX\
6. After saving: draft a PLAYBOOK.md entry and ask the user if it should be added

### SANDBOX — approved custom functions
`SANDBOX\` is the only folder where Claude can create new Python files.
- Read `SANDBOX\README.md` before writing anything there
- Every file saved here must have a corresponding PLAYBOOK.md entry (after user approval)
- Never save to SANDBOX without user approval of the approach first

### Pipeline rules
- Never run Phase 2 without explicit user approval of MH pairs
- Never auto-append to PLAYBOOK.md — draft entry and ask user first
- Never bypass preflight_check before run_agent4_pipeline
- status="failed" → stop, surface issues, do not proceed
- status="partial" → log issues, surface non-trivial ones before proceeding

### Checkpoints — never bypass
- Checkpoint 1 (after Agent 3): present savings opportunities, ask which MH pairs for Phase 2
- Checkpoint 2 (after Phase 2): present before/after comparison, ask accept/reject per pair
- Checkpoint 3 (after build_location_file): present missing ML DHs and preflight failures, wait for fixes

### Mandatory run-start question
Before any Agent 1 call:
> "Which 30-day window from the SD plan files? (Month 1: day_1–30, Month 2: day_31–60, Month 3: day_61–91, or custom)"

## Agent Files
| Agent | Python file | MD |
|---|---|---|
| Agent 1 | `Agent1_DataPrep\backend\agent1.py` | `Agent1_DataPrep\AGENT1.md` |
| Agent 2 | No Python file | `Agent2_RatesDistances\AGENT2.md` |
| Agent 3 | `Agent3_Clustering\backend\agent3.py` | `Agent3_Clustering\AGENT3.md` |
| Agent 4 | `Agent4_Routing\backend\agent4.py` | `Agent4_Routing\AGENT4.md` |

## Known Pre-run Blockers
- 4 MHs missing from MHDH rate card: CENTRALHUB_LM_AJLX, IXA3X, JLRSF1, KLM1
- 2 MHs missing from distance matrix
- 12 SATELLITEHUB DHs missing ML in DH Feasibility.csv
- Phase 2 merge function not built yet (manual merge required — see PLAYBOOK.md)

## RUN_HISTORY
Append one entry to `RUN_HISTORY.md` after every completed run. Format in §9 of PROJECT_CONTEXT.md.
