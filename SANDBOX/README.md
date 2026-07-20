# SANDBOX — Approved Custom Functions

This folder is the only place where Claude can create new Python files.
Everything here has been explicitly approved by the user before being written.

---

## Rules

1. **Never write here without user approval** — present the approach first, wait for go-ahead, then write
2. **One file per problem** — name files descriptively (e.g. `merge_phase2_changes.py`, `fill_distance_gaps.py`)
3. **Every file here must have a PLAYBOOK.md entry** — draft the entry and ask the user before adding it
4. **Use agent functions inside sandbox code** — import from agent1.py, agent3.py, agent4.py directly. Never replicate agent logic.
5. **No file here modifies agent Python files** — sandbox code calls agents, never edits them

## What belongs here

- Custom connectors between agents that no existing function covers
- One-off analysis scripts approved by the user
- Utility functions for data manipulation (thin wrappers only)
- Any new logic designed in collaboration with the user

## What does not belong here

- Rewrites or copies of existing agent functions
- Throwaway scripts that were run once and are no longer needed — delete those
- Anything not yet approved by the user

## Current contents

*(empty — add entries here as files are created)*

