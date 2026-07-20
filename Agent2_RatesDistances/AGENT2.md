# AGENT2 — Rates & Distances Reference

## ⚠ Refocusing Checkpoint — Read Before Proceeding

Before using anything in this file, verify you can answer these questions from memory:
- What is the difference between MH-MH cost structure (C/T) and MH-DH cost structure (Rs/km), and why are they different?
- What is the local vs zonal distinction, and what drives it?
- Why does a missing distance pair matter more than a missing rate card entry?

If you cannot answer all three — stop. Re-read `SUPPLY_CHAIN_CONTEXT.md §4` (The Two Linehaul Legs) and `INPUT_CONTEXT.md` entries IN0201, IN0202, IN0802 before continuing.

---

## 1. Purpose & Supply Chain Role

Agent 2 provides the two cost primitives that make the linehaul network solvable: road distances between hubs and rupee-per-trip rate cards by truck size. In the Flipkart linehaul topology, shipments move on two legs — a first leg from origin MH to an intermediate or destination MH (MH→MH), and a second leg from that MH to a Destination Hub (MH→DH). Agent 2 covers both legs. The distance matrix holds road distances (km) and drive durations for every MH→DH and MH→MH pair that is in scope; it does not cover first-mile (FC→MH) or last-mile (DH→customer) movement. The rate cards translate a (hub pair, truck size) combination into a rupee trip cost: the MH1–MH2 rate card is used for MH→MH legs (expressed as cost per trip, C/T), and the MHDH rate card is used for MH→DH legs (expressed as Local or Zonal Rs/km by truck size). Without these two inputs, Agent 3 cannot score FC_MH assignment candidates against cost, and Agent 4 cannot compute milkrun route costs or compare route permutations.

---

## 2. Files Managed by Agent 2

| File role | Typical filename | Format | Used by | Purpose |
|---|---|---|---|---|
| `distance_matrix` | `Distance Matrix.csv` | CSV | Agent 3, Agent 4 | Road distance (km) and drive duration between every hub pair in scope |
| `mh1_mh2_rate_card` | `MH1-MH2 Rate Card.csv` | CSV | Agent 3 | Cost per trip (C/T) for each MH→MH lane by truck configuration |
| `mh_rate_card` | `MHDH_RateCard.xlsx` | XLSX | Agent 4 | Local and Zonal Rs/km cost by truck size for MH→DH routes |

---

## 3. Required Columns per File

### 3a. Distance Matrix

| Column | Supply chain meaning | Header matching rule |
|---|---|---|
| `S_Code` | Origin hub code — the MH or DH the truck departs from | Exact normalised match: `s_code` (lowercase + strip) |
| `D_Code` | Destination hub code — the MH or DH the truck arrives at | Exact normalised match: `d_code` |
| `distance` | Road distance in kilometres between the hub pair | Exact normalised match: `distance` |
| `duration` | Estimated drive time for the hub pair (unit: minutes or hours depending on data source — check values) | Exact normalised match: `duration` |

All four columns must be present. No partial match — if any is missing the file fails validation.

Agent 3 uses `distance` to compute the MH→DH second-leg cost (`distance_km × 26 Rs/km` where no rate card row exists) and as an OSRM fallback trigger. Agent 4 uses `distance` as the primary input to trip costing (`distance_km × 2` for transit time; `distance ≤ 200 km` → Local rate, `> 200 km` → Zonal rate).

---

### 3b. MH1–MH2 Rate Card

| Column | Supply chain meaning | Header matching rule |
|---|---|---|
| `MH1` | Origin Middle Hub for the MH→MH lane | Exact normalised match: `mh1` |
| `MH2` | Destination Middle Hub for the MH→MH lane | Exact normalised match: `mh2` |
| `C/T` (or cost/rate column) | Cost per trip in Rs for the MH1→MH2 lane | Matches `c/t` exactly, OR any column (other than `mh1`/`mh2`) where the normalised name contains `cost`, equals `rate`, contains `rate_`, or ends with `_rate`. First such column found is used. |

`MH1` and `MH2` are both required with no fallback. The cost column is matched loosely because source files use names like `C/T`, `Cost`, `Rate`, `MH_Rate` interchangeably.

This rate card is only applied for rows where `source_type` is `MH` or `FC_MH` (Agent 3: `_plan_row_uses_mh_mh_rate_card` returns `True`). Lanes with `source_type = PH` or `ALITE` have their MH→MH first leg cost set to zero — the rate card is not looked up for those rows.

---

### 3c. MHDH Rate Card

| Column | Supply chain meaning | Header matching rule |
|---|---|---|
| `MH1` | Origin Middle Hub for the MH→DH route | Exact normalised match: `mh1` |
| `Local: <size>` columns | Rs/km cost for local trips (≤ 200 km) by truck size (e.g. `Local: 6.5`, `Local: 14`, `Local: 40`) | Any column whose normalised name starts with `local:` — one or more must be present |
| `Zonal: <size>` columns | Rs/km cost for zonal trips (> 200 km) by truck size (e.g. `Zonal: 6.5`, `Zonal: 14`, `Zonal: 40`) | Any column whose normalised name starts with `zonal:` — one or more must be present |

The truck size suffix after `local:` / `zonal:` is the vehicle size in tonnes (e.g. `6.5`). Agent 4 looks up the cost for a specific vehicle size by matching the suffix. If a vehicle size is not in the rate card (e.g. vehicle size 40), Agent 4 defaults to `distance × 999` — effectively preventing that vehicle from being assigned by the ILP. Validate that vehicle size `6.5` columns exist under both `local:` and `zonal:` prefixes, as 6.5T is the primary vehicle size used in Agent 4 (Phase 3A default).

---

## 4. How Claude Code Uses These Files

Claude acts as the orchestrator — it checks files, loads them, and passes DataFrames to Agent 3 and Agent 4 functions. No `agent2.py` exists; Claude performs these steps directly.

**Step 1 — Receive input folder path.**
The caller provides an Agent 2 root directory (e.g. `C:\...\Agent 2`). The files are expected under `<root>/Inputs/`.

**Step 2 — Check file existence.**
```python
from pathlib import Path
root = Path(agent2_root)
dm_path   = root / "Inputs" / "Distance Matrix.csv"
rc_path   = root / "Inputs" / "MH1-MH2 Rate Card.csv"
mhdh_path = root / "Inputs" / "MHDH_RateCard.xlsx"
assert dm_path.exists(),   f"Distance Matrix not found: {dm_path}"
assert rc_path.exists(),   f"MH1-MH2 Rate Card not found: {rc_path}"
assert mhdh_path.exists(), f"MHDH Rate Card not found: {mhdh_path}"
```

**Step 3 — Check required columns using header-only read.**
```python
import pandas as pd

def _norm(s): return str(s).strip().lower()

dm_cols   = {_norm(c) for c in pd.read_csv(dm_path,   nrows=0).columns}
rc_cols   = {_norm(c) for c in pd.read_csv(rc_path,   nrows=0).columns}
mhdh_cols = {_norm(c) for c in pd.read_excel(mhdh_path, nrows=0).columns}
```
Apply the rules in §3 to validate. Do not load full data until column check passes.

**Step 4 — Load full DataFrames.**
```python
dist_df  = pd.read_csv(dm_path,  dtype=str)            # Agent 3 reads as str for hub code safety
rate_df  = pd.read_csv(rc_path,  low_memory=False)
mhdh_df  = pd.read_excel(mhdh_path, engine="openpyxl")
```
Agent 3 reads the distance matrix with `dtype=str` to avoid coercing hub codes like `"BLR1"` to NaN. Convert `distance` and `duration` columns to numeric after load: `dist_df["distance"] = pd.to_numeric(dist_df["distance"], errors="coerce")`.

**Step 5 — Pass to Agent 3.**
Agent 3's pipeline function (`agent3_pipeline.run_agent3`) accepts:
- `distance_matrix_path: Path` — pass `dm_path`
- `cost_matrix` / `mh1_mh2_rate_card`: either `rc_path` (path) or the loaded DataFrame depending on call signature

Agent 3 internally expects these column names in the distance matrix DataFrame:
- `S_Code` / `s_code` — normalised to lowercase on load
- `D_Code` / `d_code`
- `distance` — numeric km
- `duration` — numeric (used as transit time proxy; Agent 3 overrides with `distance_km × 2`)

Agent 3 expects these columns in the MH1–MH2 rate card:
- `MH1`, `MH2` — hub name strings matching the values in `plan_volume.csv`
- `C/T` or equivalent cost column — numeric Rs per trip

**Step 6 — Pass to Agent 4.**
Agent 4's pipeline (`agent4_pipeline.run_agent4`) accepts:
- `distance_matrix: Path` — pass `dm_path`
- `mh_rate_card: Path` — pass `mhdh_path`

Agent 4 internally expects these column names in the distance matrix:
- `S_Code`, `D_Code`, `distance` — same as Agent 3; `duration` column present but overridden (`transit_time = distance × 2`)

Agent 4 internally expects these columns in the MHDH rate card:
- `MH1` — origin hub name matching `current_fc_mh` values in Location File
- `Local: 6.5`, `Zonal: 6.5` — primary vehicle size columns (and other sizes present in data)
- Vehicle size 40 may be present but will produce `cost = distance × 999` (sentinel — ILP never assigns it)

---

## 5. Validation Rules (Claude's Job)

| Check | How to verify | What to do if it fails |
|---|---|---|
| Distance matrix file exists | `Path(dm_path).exists()` | Abort. Report missing path. Ask caller to confirm Agent 2 Inputs folder location. |
| MH1–MH2 rate card file exists | `Path(rc_path).exists()` | Abort Agent 3 pipeline. Agent 4 can proceed without it. |
| MHDH rate card file exists | `Path(mhdh_path).exists()` | Abort Agent 4 pipeline. Agent 3 can proceed without it. |
| Distance matrix has all 4 required columns | `{"s_code","d_code","distance","duration"} <= norm_cols` after header-only read | Abort. List which columns are missing. File may be wrong version or have renamed columns. |
| MH1–MH2 rate card has `mh1` and `mh2` | `{"mh1","mh2"} <= norm_cols` | Abort. These are non-negotiable keys. |
| MH1–MH2 rate card has a cost column | `"c/t" in norm_cols` OR any non-key column with `cost`/`rate` in name | Abort. Without a cost column the rate card cannot be used for MH→MH leg pricing. |
| MH1–MH2 rate card cost column is numeric | After full load: `pd.to_numeric(df[cost_col], errors="coerce").isna().mean() < 0.05` | Warn if > 5% null after coerce; abort if > 50% null. Likely a formatting issue (commas in numbers, currency symbols). |
| MHDH rate card has `mh1` | `"mh1" in norm_cols` | Abort Agent 4. |
| MHDH rate card has at least one `local:` column | `any(c.startswith("local:") for c in norm_cols)` | Abort Agent 4. No local cost means route costing is impossible. |
| MHDH rate card has at least one `zonal:` column | `any(c.startswith("zonal:") for c in norm_cols)` | Abort Agent 4. |
| MHDH rate card has `local: 6.5` and `zonal: 6.5` | `"local: 6.5" in norm_cols and "zonal: 6.5" in norm_cols` | Warn. Agent 4 will use `distance × 999` as fallback for all 6.5T trips, making ILP output unreliable. Confirm vehicle size column naming with data owner. |
| Distance matrix has no null S_Code or D_Code | `dist_df["S_Code"].isna().sum() == 0 and dist_df["D_Code"].isna().sum() == 0` (after full load) | Warn with count. Null hub codes mean those lanes will not match any lookup. Drop null rows before passing downstream. |
| Distance matrix has no null distance values | `pd.to_numeric(dist_df["distance"], errors="coerce").isna().sum() == 0` | Warn with count. Rows with null distance will trigger OSRM fallback in Agent 3 (if enabled) or produce zero-cost assignments. Log affected hub pairs. |
| Distance matrix has no duplicate (S_Code, D_Code) pairs | `dist_df.duplicated(subset=["S_Code","D_Code"]).sum() == 0` | Warn with count. Downstream lookups take the first match; duplicates may mean stale or merged data. Deduplicate keeping the row with the most recent source if possible, else keep first. |
| MH1–MH2 rate card has no duplicate (MH1, MH2) pairs | `rc_df.duplicated(subset=["MH1","MH2"]).sum() == 0` | Warn. Agent 3 inner loop takes first match; duplicate rows cause non-deterministic cost results. Deduplicate before passing. |

---

## 6. When Agent 2 Python Code Will Be Needed

No `agent2.py` exists. The current design — Claude reads the files and passes DataFrames directly — is sufficient as long as inputs are clean and static. A Python agent file becomes justified when any of the following tasks are required:

- **OSRM fallback for missing distance pairs**: Agent 3 already has OSRM fallback logic in its pipeline, but it triggers at Agent 3 runtime against the distance matrix gaps. If Agent 2 is expected to pre-fill missing pairs and emit an augmented distance matrix, that logic belongs in `agent2.py`. The trigger is the `agent3_missing_distance_pairs.csv` output from Agent 3 — if this file has rows, Agent 2 should be invoked to patch the distance matrix.
- **Rate card normalisation**: If `MHDH_RateCard.xlsx` or `MH1-MH2 Rate Card.csv` arrive with inconsistent column naming, currency-formatted numbers, merged header rows, or multiple vehicle-size sheets that need pivoting, a structured normalisation step in `agent2.py` is cleaner than doing it inline in Claude.
- **Cost overrides**: If specific (MH1, MH2) or (MH1, DH) lane costs are overridden by finance or ops (e.g. negotiated rates, seasonal surcharges), Agent 2 should apply these on top of the base rate card before downstream agents see the data.
- **Distance matrix gap-fill from lat/long**: If new DHs or MHs are added to the network and no road distance exists for them, Agent 2 should compute haversine or OSRM distances and insert them into the matrix. This requires a lat/long input file (already present in Agent 1 Inputs).
- **Rate card version management**: If multiple rate card vintages are tracked (e.g. per planning cycle), Agent 2 needs logic to select the correct version rather than relying on newest-mtime heuristics.

Until one of these is required, `agent2.py` does not need to exist.

---

## 7. Connection to Other Agents

### Agent 3 — FC_MH Assignment Scoring

Agent 3 assigns each Destination Hub (DH) to one of up to 4 candidate FC_MH sites. The assignment decision uses two cost signals from Agent 2's files. First, the MH→MH leg cost: for lanes where `source_type` is `MH` or `FC_MH`, Agent 3 charges each hop along the PATH from the candidate FC_MH to the DH using the MH1–MH2 rate card C/T value. This is why the rate card must have matching hub names — if `MH1` in the rate card does not match the `MH1` values in `plan_volume.csv`, the C/T lookup returns zero and the FC_MH appears artificially cheap. Second, the MH→DH leg cost: Agent 3 computes `distance_km × 26 Rs/km` for the final MH→DH segment, drawing `distance_km` from the distance matrix for the `(last_mh, DH)` pair. If that pair is missing from the matrix, Agent 3 either triggers OSRM fallback (if lat/long is available) or logs it to `agent3_missing_distance_pairs.csv`. Lanes with `source_type = PH` or `ALITE` skip the MH→MH rate card entirely — their first leg cost is zero — so only the MH→DH distance term matters for those lanes.

### Agent 4 — Milkrun Route Costing

Agent 4 builds multi-stop truck routes from one MH to multiple DHs in a single trip (milkrun). The MHDH rate card determines whether a route is priced at Local or Zonal rates: if total route distance ≤ 200 km, the `Local: <size>` column value (Rs/km) is used; if > 200 km, the `Zonal: <size>` column value is used. The distance matrix provides the `distance_km` between each (MH, DH) stop pair; Agent 4 sums these for multi-stop routes to get total route distance. Transit time is computed as `distance_km × 2` — the `duration` column from the distance matrix is present but explicitly overridden. The rate card's vehicle size columns (6.5T primarily) gate which vehicle sizes can be viably assigned: sizes not in the rate card get a sentinel cost of `distance × 999` which the ILP set-cover solver will never select. This means the rate card's column completeness directly determines which vehicle mix the solver can consider, making it a hard constraint on Agent 4's output quality.
