# AGENT4 — Route Optimizer Tool Library Reference

## ⚠ Refocusing Checkpoint — Read Before Proceeding

Before using anything in this file, verify you can answer these questions from memory:
- What is ML, why does it vary by DH, and what happens if a DH has no ML value?
- What is the difference between a milkrun and an FTL dedicated truck, and what determines which a DH gets?
- Why must preflight_check pass before run_agent4_pipeline is called — what does it actually protect against?

If you cannot answer all three — stop. Re-read `SUPPLY_CHAIN_CONTEXT.md §4` (MH→DH Milkrun Leg) and `INPUT_CONTEXT.md` entries IN0201, IN0301 before continuing.

Also check: have you read `PROJECT_CONTEXT.md §9` (Problem-First Framework) this session? If not, read it now.

---

## 1. Purpose & Supply Chain Role

Agent 4 solves the last-mile milkrun scheduling problem: given a confirmed DH→FC_MH assignment from Agent 3, it builds cost-optimal truck routes from each MH depot to its assigned DHs. For each MH, the pipeline proceeds in four stages: (1) bearing-based clustering groups DHs by compass direction from the depot so permutations are only generated within geographically coherent groups; (2) permutation generation enumerates all valid stop sequences up to `max_hops` within each cluster, filtering by time-window feasibility and distance data availability; (3) cost scoring and domination pruning assigns vehicle size and monthly cost to each route and eliminates dominated options (same hub-set, higher cost); (4) ILP set-cover selects the minimum-cost subset of routes that covers every DH in the cluster exactly once. DHs whose total demand exceeds the ML vehicle capacity gate are split into dedicated FTL trucks before the milkrun step, with any sub-threshold residual either absorbed into FTL or re-entered into milkrun. The output is a full operational route schedule: which truck visits which DHs, in what order, at what frequency, with arrival and departure times at every stop.

**Pipeline position:** Agent 4 is the terminal agent. It consumes:
- Agent 3's `dh_fc_mh_assignment.csv` (DH→FC_MH assignment with demand, CFT, top-266 load)
- Agent 2's Distance Matrix CSV (pairwise km between all hubs)
- Agent 2's MHDH_RateCard.xlsx (per-MH truck rates by vehicle size, local vs zonal)
- DH Feasibility CSV (per-DH ML constraint — max vehicle length allowed at that DH)
- Lat Longs XLSX (hub coordinates for OSRM fallback and bearing clustering)

Its outputs are the actual truck movements used for operational planning and accruals.

---

## 2. Two-Step Pre-run Process (Claude's Job)

Agent 4 requires a mandatory two-step setup before `run_agent4_pipeline` can be called. This is different from Agents 1–3, which have no mandatory gate.

### Step 1 — Build the Location File

Call `build_location_file(agent3_assignment_df, dh_feasibility_df)` to merge Agent 3's output with the DH Feasibility file and produce the location file DataFrame. This merges on `destination_hub_key`, renames `assigned_fc_mh` to `current_fc_mh`, applies time-window defaults, and flags any DHs whose ML is missing from the Feasibility file.

`build_location_file` never returns `status="failed"`. It returns:
- `status="ok"` — all DHs have an ML value; location file is ready for preflight
- `status="partial"` — one or more DHs have no ML in DH Feasibility; 12 null-ML rows are included in `data` so the caller can see which DHs are affected

**If `status="partial"`, do not proceed.** Surface the `missing_ml` issues to the user. The user must add ML values for those DHs in `DH Feasibility.csv` before continuing.

### Step 2 — Run Preflight Check (Hard Gate)

Call `preflight_check(location_file_df, dist_df, mhdh_rate_card_df, cfg)`. This is a binary hard gate: it returns either `status="ok"` (all 5 checks pass) or `status="failed"` (one or more checks failed). Never call `run_agent4_pipeline` unless `preflight_check` returns `status="ok"`.

If `preflight_check` fails, show all issues to the user, wait for source-data fixes, then re-run preflight. Do not bypass or skip this check.

### Decision Tree

```
1. build_location_file()
   └─ status="ok"    → proceed to step 2
   └─ status="partial"
       └─ show missing_ml issues to user
       └─ user updates DH Feasibility.csv with correct ML values
       └─ re-run build_location_file()
       └─ repeat until status="ok"

2. preflight_check()
   └─ status="ok"    → call run_agent4_pipeline()
   └─ status="failed"
       └─ show ALL issues to user (null_ml / invalid_ml / mh_not_in_rate_card /
          dh_missing_distance / mh_missing_distance)
       └─ user fixes source data (rate card, distance matrix, feasibility file)
       └─ re-run preflight_check()
       └─ repeat until status="ok"
```

---

## 3. Pre-call Checklist (Claude's Job)

Before calling any public function, verify the preconditions below. These are static checks — Claude performs them, not the functions.

| Function | Precondition | What to check |
|---|---|---|
| `load_agent4_config` | None | Safe to call with no arguments; returns a complete 14-key dict using built-in defaults. Pass `config_path=Path(...)` to override individual keys from file. |
| `build_location_file` | `agent3_assignment_df` must have `destination_hub_key`, `assigned_fc_mh`, `total_cft`, `top266_shipments`, `total_shipments` columns. `dh_feasibility_df` must have `destination_hub_key`, `ML` columns. | Load Agent 3 output with `pd.read_csv`. Load DH Feasibility with `pd.read_csv`. Do NOT pass filepaths — pass DataFrames. |
| `preflight_check` | `location_file_df` must be the `data` from `build_location_file` result (or a DataFrame with the same schema). `dist_df` must have `S_Code`, `D_Code`, `distance` columns. `mhdh_rate_card_df` must have `MH1` column. `cfg` must be from `load_agent4_config`. | Load distance matrix with `pd.read_csv(path, dtype=str)`. Load rate card with `pd.read_excel(path)`. Call this function before `run_agent4_pipeline` — never skip. |
| `build_distance_dict` | `dist_df` must have `S_Code`, `D_Code`, `distance` columns. Distance values should be numeric; non-numeric rows produce `missing_distance` issues but do not fail the call. | Agent 4 does NOT read distance matrix with `dtype=str` internally — but calling code should pass the raw DataFrame. Hub name case must exactly match the location file (`.strip()` only, no case normalisation). |
| `build_latlong_dict` | `ll_df` must have `Site_name`, `Latitude`, `Longitude` columns. | Load from `Lat Longs.xlsx` with `pd.read_excel`. Non-numeric lat/lon rows emit `invalid_latlong` issues. |
| `run_agent4_pipeline` | (a) `preflight_check` must have returned `status="ok"` for the same `location_file_df`. (b) All null-ML rows must be dropped from `location_file_df` before calling (pipeline uses ML as a float; null ML causes `float(NaN)` and defaults to 40ft, which has sentinel cost 999 — those DHs will silently get no viable route). (c) `mhdh_rate_card_path` must be a `Path` object pointing to an existing file. (d) `out_dir` will be created if it does not exist. | Drop nulls: `loc_df = loc_df.dropna(subset=["ML"]).copy()`. |
| `run_agent4_for_mh` | **Claude Code should not call this directly.** Phase 2 calls it directly; the orchestration layer should always call `run_agent4_pipeline`. If calling directly: `dist_dict` must be a mutable dict (the function caches OSRM results in it), `dh_df` must contain only rows for the single MH being processed. | Call `run_agent4_pipeline` instead. |
| `load_rate_card` | `path` must be a `Path` to an existing `MHDH_RateCard.xlsx`. `cfg` must be from `load_agent4_config`. | Called internally by `run_agent4_pipeline`. Direct calls are for inspection only. |
| `get_distance` | `dist_dict` must be the `data` from a `build_distance_dict` result. OSRM fallback requires internet access. | Pass `use_osrm_fallback: false` in config override to disable OSRM for offline runs (see §6). |
| `assign_vehicle_length` | `total_demand` must be a float (CFT). | Returns 0.0 for demand ≤ 0. |
| `derive_freq_allowed` | `top266_load` must be a float (count of Top-266 shipments). | Returns `int`. See §4 for interpretation. |
| `preprocess_ftl_splits` | Called internally by `run_agent4_for_mh`. Not for direct use by orchestrator. | — |
| `assign_bearing_clusters` | Called internally. Requires `_lat`, `_lon`, `_depot_departure` columns in the input DataFrame. | — |

---

## 4. Function Reference

### `load_agent4_config(config_path=None)`
```
Returns: dict[str, Any]   (plain return, no result dict)
```
Loads the 14-key config dict. If `config_path` points to a valid JSON file, merges its values on top of the built-in defaults — keys absent from the file keep their defaults. Returns the full dict unconditionally (never raises). Use the returned dict as `cfg` throughout all other Agent 4 calls.

---

### `build_location_file(agent3_assignment_df, dh_feasibility_df, phase2_accepted_changes=None, time_window_overrides=None)`
```
Returns: {"status": "ok" | "partial", "data": DataFrame, "issues": [...]}
```
Left-joins `agent3_assignment_df` onto `dh_feasibility_df` on `destination_hub_key` to pull in the `ML` column. Output columns: `destination_hub_key`, `current_fc_mh`, `total_cft`, `top266_shipments`, `total_shipments`, `ML`, `time_window_start`, `depot_departure`, `time_window_end`.

**MH assignment source — resort baseline, not Phase 1 proposal.** The function uses `current_fc_mh` from `agent3_assignment_df` (the original resort mapping that Agent 3 carried forward) — NOT `assigned_fc_mh` (Agent 3's Phase 1 proposal). Agent 3's proposal is a recommendation only; the confirmed baseline is always the resort. `assigned_fc_mh` is dropped from the working copy before any join.

**`phase2_accepted_changes` format:**
```python
{
    "SATELLITEHUB_PUNE1":  "CENTRALHUB_L_MUMX",   # DH -> new MH (Phase 2 accepted)
    "SATELLITEHUB_NASIK1": "CENTRALHUB_L_MUMX",
}
```
Only DHs explicitly in this dict get the new MH. All other DHs keep `current_fc_mh` from the resort baseline. Pass `None` (default) if Phase 2 was not run or no pair changes were accepted. DHs in `phase2_accepted_changes` that are not found in the assignment DataFrame emit a `phase2_dh_not_found` issue (non-fatal).

Default time-window values applied to every row:
- `time_window_start = 0` (minutes from midnight)
- `depot_departure = 0`
- `time_window_end = 1800` (30 hours — allows overnight delivery windows)

**`time_window_overrides` format:**
```python
{
    "CENTRALHUB_STV1":          {"depot_departure": 60},            # MH-level: all DHs under this MH
    "SATELLITEHUB_PUNE1":       {"time_window_start": 120,
                                 "time_window_end":   1440},         # DH-level: this DH only
}
```
Keys can be MH names or DH keys. MH-level overrides are applied first; DH-level overrides are applied second and win. Only the three time-window columns can be overridden; other columns are not modifiable through this dict.

**`status="partial"` means:** at least one DH had no matching row in `dh_feasibility_df`. The 12 null-ML DHs are included in `data` (caller decides whether to drop or fill). The `issues` list contains one `missing_ml` entry per affected DH. Do not pass the DataFrame to `run_agent4_pipeline` until all null-ML rows are dropped.

---

### `preflight_check(location_file_df, dist_df, mhdh_rate_card_df, cfg)`
```
Returns: {"status": "ok" | "failed", "data": None, "issues": [...]}
```
Hard gate. Never returns `status="partial"`. Returns `status="ok"` only if ALL five checks pass simultaneously.

**5 checks performed (in order):**

| # | Check | Issue type on failure |
|---|---|---|
| 1 | No null ML values in `location_file_df["ML"]` | `null_ml` |
| 2 | All non-null ML values are in `{6.5, 8.0, 10.0, 14.0, 17.0, 20.0, 22.0, 24.0, 32.0, 40.0}` | `invalid_ml` |
| 3 | All `current_fc_mh` values appear in the rate card's `MH1` column | `mh_not_in_rate_card` |
| 4 | All `destination_hub_key` values appear in the distance matrix (as either `S_Code` or `D_Code`) | `dh_missing_distance` |
| 5 | All `current_fc_mh` values appear in the distance matrix | `mh_missing_distance` |

**Known pre-run blockers (as of last test run):**
- Checks 3 fails for 4 MHs not in the rate card: `CENTRALHUB_LM_AJLX`, `CENTRALHUB_LM_IXA3X`, `CENTRALHUB_L_JLRSF1`, `CENTRALHUB_L_KLM1`. These must be added to `MHDH_RateCard.xlsx` before the first production run.
- Check 5 fails for 2 MHs not in the distance matrix: `CENTRALHUB_L_AURPRC1`, `CENTRALHUB_L_SRTSFL1`. These must be added to `Distance Matrix.csv`.

When `status="failed"`, the pipeline can still run (the function does not block it), but `run_agent4_pipeline` will use default rate cards for missing MHs and those MHs' routes may have incorrect costs. **Never bypass this gate for production runs.**

---

### `build_distance_dict(dist_df)`
```
Returns: {"status": "ok", "data": dict[tuple[str, str], float], "issues": [...]}
```
Builds `(origin_name, dest_name) → km` dict from the Distance Matrix DataFrame. Always returns `status="ok"` regardless of issues. Non-numeric distance values emit `missing_distance` issues but do not prevent the dict from being built for valid rows. Deduplicates by keeping the first occurrence per ordered pair.

**Important — hub name matching:** Agent 4 strips whitespace (`.strip()`) but does NOT normalise case. Hub names in the distance matrix must exactly match (case-sensitive) the names used in the location file and rate card. Agent 3 normalises to uppercase during clustering; if the distance matrix uses mixed case for any hub, distances will return `None` silently and those pairs will fall through to OSRM.

---

### `build_latlong_dict(ll_df)`
```
Returns: {"status": "ok", "data": dict[str, tuple[float, float]], "issues": [...]}
```
Builds `site_name → (lat, lon)` dict. Always returns `status="ok"`. Non-numeric lat/lon values emit `invalid_latlong` issues. Deduplicates by keeping the first occurrence per site name. Used by OSRM fallback and bearing cluster computation — an MH with no lat/long will be skipped entirely by the pipeline (`ERROR: No lat/long for depot`).

---

### `preflight_check` — see above

---

### `load_rate_card(path, cfg)`
```
Returns: dict[str, MHConfig]   (plain return)
```
Reads `MHDH_RateCard.xlsx`. Returns a dict from MH name → `MHConfig`. Rate card columns:
- `MH1`: MH hub name
- `Local:<size>` (e.g. `Local:6.5`, `Local:10`): rate per km per day for local routes
- `Zonal:<size>`: rate per km per day for zonal routes
- `max_hops`, `threshold_a`, `threshold_b`, `service_time`: optional per-MH overrides; falls back to config defaults if absent or null
- `City`, `Tag`: metadata fields stored in MHConfig; Tag is used by Phase 2 to control `min_vehicle_ft`

---

### `get_distance(origin, dest, dist_dict, latlong)`
```
Returns: Optional[float]   (plain return — km or None)
```
Returns km from `dist_dict[(origin, dest)]`. If not found, calls OSRM and caches the result in `dist_dict`. Returns `None` if both dist_dict lookup and OSRM fail. Signature is unchanged from the original pipeline — Phase 2 calls this directly.

---

### `get_transit_time(dist_km)`
```
Returns: float   (minutes)
```
`dist_km × 2.0`. Assumes 30 km/h average speed. No config key — hardcoded formula.

---

### `assign_vehicle_length(total_demand)`
```
Returns: float   (vehicle length in feet)
```
Maps total route demand (CFT) to vehicle size. Phase 2 calls this directly.

| Demand (CFT) | Vehicle size (ft) | ML capacity (CFT) |
|---|---|---|
| > 2550 | 40.0 | ∞ (sentinel 9,999,999) |
| > 1550 | 32.0 | 2550 |
| > 1325 | 24.0 | 1550 |
| > 1255 | 22.0 | 1325 |
| > 893 | 20.0 | 1255 |
| > 686 | 17.0 | 893 |
| > 400 | 14.0 | 686 |
| > 250 | 10.0 | 400 |
| > 180 | 8.0 | 250 |
| > 0 | 6.5 | 180 |
| = 0 | 0.0 | — |

The 40 ft vehicle size is effectively unusable: `ML_VEHICLE_CAPACITY[40] = 9_999_999` means it handles any demand, but the rate card has no entry for 40 ft, so `rate_card.get(40, 999)` returns 999 — a sentinel value that produces a very high cost. No route using a 40 ft vehicle will be selected by ILP. If a DH has `ML=40` in the Feasibility file, it should still work correctly (40 ft ML constraint means any vehicle is allowed), but the cost computation may behave unexpectedly. See §10.

---

### `derive_freq_allowed(top266_load)`
```
Returns: int   (0 or 1)
```
Phase 2 calls this directly. Controls whether a DH can participate in a freq-2 (twice-daily) route.

| `top266_load` | `freq_allowed` | Meaning |
|---|---|---|
| `== 0` | `1` | DH has no Top-266 shipments; freq-2 allowed |
| `> 0` | `0` | DH serves Top-266 shipments; freq-2 blocked |

**Operational interpretation:** `freq_allowed = 1` means **freq-2 IS allowed** for that DH. In the routing cost step, `freq_ok = all(attr[h]["freq_allowed"] == 1 for h in route_hubs)`. If any DH on a route has `freq_allowed = 0`, the entire route is forced to freq-1. A freq-2 route visits every DH twice per day; a freq-1 route visits each DH once per day. The cost comparison selects whichever frequency is cheaper for the hub-set.

---

### `preprocess_ftl_splits(...)`
```
Returns: tuple(milkrun_attr, ftl_assignment_rows, ftl_expanded_rows, absorbed_list, val_lines)
```
Internal — called by `run_agent4_for_mh`. For any DH whose demand > ML vehicle capacity, allocates `floor(demand / ml_capacity)` dedicated FTL trucks. Residual demand below `residual_threshold` (default 100 CFT) is absorbed into FTL (DH removed from milkrun entirely); residual above threshold is reduced and re-entered into milkrun. If no distance data exists for a DH's FTL route, it remains in milkrun as-is with a warning.

---

### `assign_bearing_clusters(destinations, depot_lat, depot_lon, max_hops, max_comb_limit)`
```
Returns: pd.DataFrame   (destinations with _bearing, _bearing_group, _final_group columns added)
```
Internal — called by `run_agent4_for_mh`. Sorts DHs by bearing angle from depot (0°=North, 90°=East). Splits into the minimum k groups such that total permutations across all groups ≤ `max_comb_limit`. Within each group, permutations are further split by `depot_departure` time window, producing `_final_group` labels of the form `"<departure_time>-<bearing_group_index>"`.

---

### `run_agent4_for_mh(mh_name, mh_cfg, dh_df, dist_dict, latlong, cfg, on_progress=None, residual_threshold=100.0)`
```
Returns: Agent4MHResult   (dataclass)
```
Runs the complete 4-step routing pipeline (bearing cluster → permutations → cost/pruning → ILP) for a single MH. Phase 2 calls this directly with its own `dist_dict` and `dh_df`; Claude Code should not call this function directly — use `run_agent4_pipeline` instead.

**OSRM logging:** The function creates a local `osrm_log: list` and exposes it via a `contextvars.ContextVar`. Any OSRM calls made during the run (by `get_distance → _osrm_distance_km`) append to this list. After the function returns, the contextvar is reset so subsequent calls have a clean log. The log is stored in `Agent4MHResult.osrm_log`.

**`Agent4MHResult` fields:**

| Field | Type | Description |
|---|---|---|
| `mh_name` | `str` | MH depot name |
| `clustering_df` | `DataFrame` | One row per DH: bearing, bearing_group, final_group, demand, ML, freq_allowed, allowed_positions |
| `filtered_routes_df` | `DataFrame` | All feasible routes after costing and domination pruning |
| `final_assignment_df` | `DataFrame` | ILP-selected routes + FTL dedicated trucks |
| `expanded_schedule_df` | `DataFrame` | Stop-level schedule with arrival/departure times at each location |
| `validation_lines` | `list[str]` | Log lines from this MH's run; appended to validation report |
| `total_monthly_cost` | `float` | Combined milkrun + FTL monthly cost for this MH (INR) |
| `n_clusters` | `int` | Number of bearing clusters created |
| `n_perms_checked` | `int` | Total permutations evaluated |
| `n_routes_survived` | `int` | Routes passing time-window and distance filters |
| `ilp_status` | `dict[str, str]` | cluster_id → `"SUCCESS"` or `"FAILED"` |
| `missing_dhs` | `list[str]` | DHs not covered by any ILP solution (data quality or infeasibility) |
| `absorbed_residuals_df` | `DataFrame` | DHs whose milkrun residual was absorbed into FTL |
| `dh_summary_df` | `DataFrame` | One row per original DH: route type, FTL count, residual, milkrun demand |
| `osrm_log` | `list` | OSRM calls made during this MH's run (each: origin, destination, distance_km, transit_minutes) |

---

### `run_agent4_pipeline(location_file_df, lat_long_df=None, dist_df=None, mhdh_rate_card_path=None, out_dir=None, cfg=None, ...)`
```
Returns: {"status": "ok" | "partial" | "failed", "data": {...}, "issues": [...]}
```
Runs `run_agent4_for_mh` for every MH in `location_file_df`, writes 8 output files to `out_dir`, and returns an aggregated result dict. Accepts **DataFrames or file paths** for `lat_long` and `distance_matrix` — use whichever the caller already has. DataFrame takes priority if both are provided for the same input.

**Full signature:**
```python
run_agent4_pipeline(
    location_file_df,                # pd.DataFrame — always required
    lat_long_df=None,                # pd.DataFrame — or pass lat_long_path
    dist_df=None,                    # pd.DataFrame — or pass distance_matrix_path
    mhdh_rate_card_path=None,        # Path to MHDH_RateCard.xlsx — required
    out_dir=None,                    # Path; defaults to "." if None
    cfg=None,                        # dict; load_agent4_config() used if None
    threshold_a_override=None,       # float — overrides all per-MH threshold_a
    threshold_b_override=None,       # float — overrides all per-MH threshold_b
    on_progress=None,                # callable(str) for live progress
    residual_threshold=100.0,        # CFT below which FTL residual is absorbed
    # Legacy path params:
    lat_long_path=None,              # Path — used when lat_long_df not provided
    distance_matrix_path=None,       # Path — used when dist_df not provided
    mh_rate_card_path=None,          # alias for mhdh_rate_card_path
)
```

**Key parameter notes:**
- `location_file_df`: DataFrame from `build_location_file` — null-ML rows must be dropped before calling
- `lat_long_df` / `lat_long_path`: provide one; DataFrame takes priority if both provided
- `dist_df` / `distance_matrix_path`: provide one; DataFrame takes priority if both provided
- `mhdh_rate_card_path` (or alias `mh_rate_card_path`): Path to `MHDH_RateCard.xlsx` — loaded internally; must be a path (not a DataFrame)
- `out_dir`: `Path`; created if it does not exist; defaults to `"."` if `None`
- `cfg`: if `None`, `load_agent4_config()` is called automatically
- `threshold_a_override`, `threshold_b_override`: when set, override per-MH rate card values globally (all MHs use the same threshold)
- `residual_threshold`: float CFT; FTL residual below this is absorbed into dedicated FTL (default 100)

**`data` keys in result:**

| Key | Type | Description |
|---|---|---|
| `per_mh` | `dict[str, Agent4MHResult]` | Per-MH results; key is MH name |
| `clustering_df` | `pd.DataFrame` | Combined clustering output across all MHs |
| `final_assignment_df` | `pd.DataFrame` | Combined ILP-selected routes + FTL trucks across all MHs |
| `expanded_schedule_df` | `pd.DataFrame` | Combined stop-level schedule across all MHs |
| `dh_route_summary_df` | `pd.DataFrame` | Combined per-DH route summary across all MHs |
| `absorbed_residuals_df` | `pd.DataFrame` | Combined absorbed FTL residuals across all MHs |
| `osrm_fallback_df` | `pd.DataFrame` | All OSRM calls made (prescan + per-MH) |
| `total_monthly_cost` | `float` | Sum of all MH monthly costs (INR) |
| `validation_report` | `str` | Full text of validation report (also written to disk) |
| `grand_total_monthly_cost` | `float` | Alias for `total_monthly_cost` |
| `n_mhs` | `int` | Number of MHs processed |
| `n_routes` | `int` | Total rows in final assignment |
| `n_osrm_calls` | `int` | Total OSRM calls (prescan + per-MH) |
| `out_dir` | `Path` | Output directory |
| `output_files` | `dict[str, Path]` | Logical key → full path for each of the 8 output files |

**`status` interpretation:**
- `"ok"` — no issues; all MHs had complete rate card and distance data
- `"partial"` — some issues (e.g. invalid lat/long, MH not in rate card, non-numeric distance rows); pipeline completed but some MHs used defaults or were skipped
- `"failed"` — a required input was missing or a required column was absent from `location_file_df`; pipeline did not run

---

## 5. Location File Reference

The location file is a DataFrame generated by `build_location_file`. It is NOT a manually created file — always generate it from Agent 3 output + DH Feasibility.

### Input 1 — Agent 3 assignment output

Path: `Agent3_Clustering\output\<run_name>\dh_fc_mh_assignment.csv`

Columns consumed by `build_location_file`:

| Column | Use |
|---|---|
| `destination_hub_key` | DH identifier; join key to DH Feasibility |
| `current_fc_mh` | **Resort baseline** MH assignment — used directly as `current_fc_mh` in output. This is the original resort mapping carried forward by Agent 3, not Agent 3's Phase 1 proposal. |
| `total_cft` | Total CFT demand for this DH; used as route demand |
| `top266_shipments` | Top-266 load count; used to compute `freq_allowed` and position constraints |
| `total_shipments` | Total shipments; included in location file for reference |

`assigned_fc_mh` (Agent 3's Phase 1 proposal) is **dropped** — it is never used as the MH baseline. Use `phase2_accepted_changes` to apply specific overrides after Phase 2.

All other columns in the assignment file are dropped by `build_location_file`.

### Input 2 — DH Feasibility

**Path:** `Inputs\DH Feasibility.csv`

Two columns only:

| Column | Description |
|---|---|
| `destination_hub_key` | DH identifier; must match Agent 3 output exactly (case-sensitive) |
| `ML` | Maximum vehicle length allowed at this DH (feet); valid values: 6.5, 8, 10, 14, 17, 20, 22, 24, 32, 40 |

### Default time-window values

Applied by `build_location_file` to every row before overrides:

| Column | Default | Unit |
|---|---|---|
| `time_window_start` | 0 | Minutes from midnight |
| `depot_departure` | 0 | Minutes from midnight |
| `time_window_end` | 1800 | Minutes from midnight (= 30 hours) |

Note: `cfg["default_time_window_end_min"]` is 1440 (24 hours). This value is used only as a fallback inside `run_agent4_for_mh` if the location file somehow lacks the `time_window_end` column. The `build_location_file` function always sets `time_window_end = 1800`, so the config default of 1440 is never reached in normal operation.

### Override format

```python
time_window_overrides = {
    # MH-level: applies to all DHs assigned to this MH
    "CENTRALHUB_STV1": {
        "depot_departure": 60,       # trucks depart 60 min after midnight
    },
    # DH-level: applies to this DH only; beats MH-level if both match
    "SATELLITEHUB_PUNE1": {
        "time_window_start": 120,    # DH opens at 2:00 AM
        "time_window_end":   1200,   # DH closes at 20:00 (8 PM = 1200 min)
    },
}
```

Override keys can only be `time_window_start`, `time_window_end`, `depot_departure`. Other column names in the override dict are ignored.

### 12 DHs currently missing from DH Feasibility

These 12 DHs appear in Agent 3's output but have no row in `DH Feasibility.csv`. They must have ML values added before the first production run. Until then, `build_location_file` will return `status="partial"` and these DHs must be dropped before calling the pipeline.

| DH key |
|---|
| `SATELLITEHUB_ABHOR1` |
| `SATELLITEHUB_BARNALA1` |
| `SATELLITEHUB_BATALA1` |
| `SATELLITEHUB_FKHMBD` |
| `SATELLITEHUB_FKMYS2` |
| `SATELLITEHUB_FRDNEW` |
| `SATELLITEHUB_KHANNA1` |
| `SATELLITEHUB_KIRARI` |
| `SATELLITEHUB_MANSAROVER` |
| `SATELLITEHUB_NOIDAPHASE2` |
| `SATELLITEHUB_ROBERTSGANJ` |
| `SATELLITEHUB_SIKANDRA` |

---

## 6. Config Reference

Config file: `backend/agent4_config.json`. All 14 keys are present in the file — unlike some other agents, there are no code-only hidden defaults that the config file omits. Loaded by `load_agent4_config(config_path)`.

| Key | Default | Operational meaning |
|---|---|---|
| `max_comb_limit` | `20000000` | Maximum total permutations allowed across all bearing clusters for one MH. If splitting into k groups brings total permutations below this limit, k is chosen. Prevents combinatorial explosion. |
| `default_service_time_min` | `120` | Dwell time at each DH stop (minutes) when no per-MH `service_time` override is in the rate card. 120 minutes = 2 hours per DH stop. |
| `default_max_hops` | `4` | Maximum number of DH stops per route when no per-MH `max_hops` is in the rate card. A route visiting 4 DHs has 5 stops including the return to depot. |
| `default_threshold_a` | `50` | Per-MH `threshold_a` fallback. DHs with `top266_load < threshold_a` have no position constraint (can appear at any stop in a route). |
| `default_threshold_b` | `150` | Per-MH `threshold_b` fallback. DHs with `threshold_a ≤ top266_load ≤ threshold_b` must appear at stop 1 or 2. DHs with `top266_load > threshold_b` must be the first stop. |
| `default_depot_departure_min` | `0` | Depot departure time (minutes) when no override in location file. 0 = trucks can depart at midnight. |
| `default_time_window_start_min` | `0` | DH time window open (minutes) fallback — used only if location file row is missing `time_window_start`. |
| `default_time_window_end_min` | `1440` | DH time window close (minutes) fallback — used only if location file row is missing `time_window_end`. Note: `build_location_file` sets `time_window_end = 1800` for every row, so this fallback is not reached in normal operation. |
| `local_zonal_distance_threshold_km` | `200` | Round-trip distance threshold for local vs zonal rate card selection. Routes with total km ≤ 200 use the local rate card; routes > 200 km use the zonal rate card. |
| `col_location_name` | `"destination_hub_key"` | Column name in location file for DH identifiers. Must match actual column name exactly. |
| `col_mh_assignment` | `"current_fc_mh"` | Column name in location file for MH assignment. |
| `col_demand` | `"total_cft"` | Column name in location file for DH demand (CFT). |
| `col_top266_load` | `"top266_shipments"` | Column name in location file for Top-266 shipment count. Controls position constraints and freq-2 eligibility. |
| `col_ml` | `"ML"` | Column name in location file for maximum vehicle length constraint. |

---

## 7. Output Files Reference

All files written to `out_dir` by `run_agent4_pipeline`. Retrieved via `result["data"]["output_files"][key]` for the file path, or directly as a DataFrame via `result["data"]["<df_key>"]` (e.g. `result["data"]["final_assignment_df"]`). The DataFrames in the return dict are identical to what is written to disk — no need to re-read files from disk after the pipeline returns.

| Filename | Logical key | Grain | Key columns | Downstream consumer | Notes |
|---|---|---|---|---|---|
| `Clustering_Output.csv` | `clustering` | One row per DH per MH | `MH`, `location_name`, `bearing_group`, `final_group`, `bearing`, `demand`, `ML`, `freq_allowed`, `allowed_positions` | Debugging; not consumed by other agents | Shows how DHs were grouped into bearing clusters before permutation generation |
| `Filtered_Routes.csv` | `filtered_routes` | One row per feasible route per MH | `MH`, `route_sequence`, `hubs`, `dist`, `group`, `monthly_cost`, `Freq`, `total_demand`, `assigned_vehicle_length`, `local_or_zonal` | Debugging; not consumed by other agents | All routes that survived time-window and distance filters, after domination pruning. Input candidate set for ILP. |
| `Final_Assignment.csv` | `final_assignment` | One row per assigned route per MH (milkrun + FTL) | `MH`, `Route_ID`, `route_sequence`, `hubs`, `dist`, `monthly_cost`, `Freq`, `Route_Type`, `assigned_vehicle_length`, `arrival_times`, `departure_times` | Phase 2 reads `total_monthly_cost` via `Agent4MHResult`; operations team reads this file for truck planning | Primary operational output. Contains ILP-selected milkrun routes and dedicated FTL trucks. `Route_Type` = `Milkrun` or `FTL_Dedicated`. |
| `Expanded_Schedule.csv` | `expanded_schedule` | One row per stop per route per MH | `MH`, `Route_ID`, `Location`, `Arrival_Time`, `Departure_Time`, `Freq`, `Vehicle_Length`, `Total_Demand`, `Route_Sequence`, `Route_Type` | Operations team; accruals team | **Primary operational output.** Stop-level schedule. Arrival_Time/Departure_Time in minutes from midnight. First stop (depot) has no Arrival_Time (NaN). Last stop (depot return) has no Departure_Time (NaN). |
| `osrm_fallback_log.csv` | `osrm_fallback` | One row per OSRM call | `origin`, `destination`, `distance_km`, `transit_minutes` | Debugging; data team to back-fill distance matrix | Populated only when OSRM is reachable (requires internet). Empty in offline runs. Pairs here should be added to the distance matrix to avoid future OSRM dependency. |
| `DH_Route_Summary.csv` | `dh_summary` | One row per DH per MH | `MH`, `DH`, `original_demand`, `ML`, `ml_capacity`, `n_ftl_trucks`, `residual_cft`, `residual_absorbed`, `milkrun_demand_cft`, `route_type`, `in_milkrun_assignment` | Accruals team; operations team | Per-DH summary of how demand was handled. `route_type` = `Milkrun`, `FTL_Dedicated`, or `FTL+Milkrun`. |
| `Absorbed_Residuals.csv` | `absorbed_residuals` | One row per DH whose milkrun residual was absorbed | `MH`, `DH`, `original_demand`, `ML`, `ml_capacity`, `n_ftl_trucks`, `residual_cft`, `residual_threshold` | Accruals team | DHs where residual after FTL allocation fell below `residual_threshold` and was absorbed entirely into FTL, removing the DH from milkrun. |
| `validation_report_agent4.txt` | `validation_report` | Text; one section per MH | N/A | Debugging; operations review | Full run log including per-MH step output, total cost, FTL summary, 40 ft vehicle note, threshold override note if applicable. Written as UTF-8. |

---

## 7a. Freeze-Day Engine Output Files (`agent4_freeze_day.py`)

A separate, additive engine sitting alongside the legacy `run_agent4_pipeline` above — see module docstring for the day-simulation model. `run_agent4_freeze_day_pipeline(location_df, ...)` writes to its own `out_dir`:

| Filename | Grain | Notes |
|---|---|---|
| `Location_File.csv` | One row per DH | **The exact location file used for this run** — base assignment columns + `D<n>`/`D<n>_cft` (day-wise demand, real window + 7 synthetic extreme days) + `Current_MR`/`Current_Freq` (H2H baseline) + `allowed_positions`/`Freq_Allowed`. |
| `Freeze_Day_Comparison.csv` | One row per (MH, candidate freeze day) | Committed/adhoc/total cost and adhoc% for every day tested, real and synthetic. |
| `Final_Assignment.csv` | One row per route per MH | The optimal frozen route plan (milkrun + dedicated), after the truck-upgrade loop. |
| `Expanded_Schedule.csv` | One row per stop per route | Stop-level detail for the optimal plan. |
| `Baseline.csv` | One row per current route per MH | Current (H2H `Current_MR`/`Current_Freq`) route network, costed for comparison. |
| `Baseline_vs_Optimal.csv` | One row per MH | Current vs. optimal cost/savings summary. |
| `Network_Summary.csv` | One row per MH | Optimal freeze day, adhoc%, committed/adhoc/total cost, savings vs. baseline. |

**Standing rule — Location_File.csv always lives in the run's own output folder.** `run_agent4_freeze_day_pipeline` writes it to `out_dir` automatically, alongside the other outputs — never to `Inputs\` or any shared/reused path. Each run gets its own output folder already; the location file is scoped to that folder so it's always traceable to the exact run it was used for, and never silently reused stale across runs.

---

## 7b. Dock Scheduling + CX-Cutoff + Speed Engine (`agent4_dock_scheduling.py`)

A third additive module (alongside `agent4.py` and `agent4_freeze_day.py`), run as a **separate post-processing step after** `run_agent4_freeze_day_pipeline` — same pattern as `write_route_visualizer` (not called from inside the pipeline, to avoid a circular import: this module imports `agent4_freeze_day` for `_compute_shifted_mh_dep`/`_attr_from_dh_rows`).

**What it does:** for the optimal freeze-day candidate's route plan (never all 37 candidates — dock scheduling only changes departure timing, never cost, so it can't affect which day is optimal), decides each route's *actual* dock-feasible departure time (TMS) given a limited number of physical docks per MH, and computes a genuine "Actual D1%"/speed metric — distinct from Agent 3's predictive D1% (which assumes direct MH-DH transit with no route/dock constraints and covers shipments still in transit to the MH; this one measures what actually happens given real routes and real dock contention).

**Two distinct time concepts:**
- **Dock occupancy** — a dock is physically blocked from `TMS - (shipments_on_route / dock_productivity_per_hour)` to `TMS + dock_transition_buffer_min`. Volume/productivity-based, unrelated to the CX-cutoff buffer below.
- **CX (customer order) cutoff** — `TMS - (cx_cutoff_multi_dh_hours or cx_cutoff_single_dh_hours) - cx_cutoff_processing_hours`. Looked up against `Load Profile.csv`'s cumulative order-placement fraction (reusing `agent3.build_load_profile_interp` directly — not re-implemented) to determine what fraction of a DH's daily Top266 volume is actually captured onto that truck. This is *why* a later TMS is better: it captures more of the day's late-placed orders.

**The objective is a weighted trade-off (an ILP), not strict priority preservation**: `maximize Σ top266_shipments × capture_fraction`, summed over DHs whose arrival also clears the **true** D1% threshold (`d1_true_threshold`, always 1800 — never the rollover-relaxed feasibility window). A higher-priority route can still be nudged if the aggregate benefit is large enough; see `low_priority_top266_threshold`/rollover mechanism below for the companion piece that widens *feasibility* for low-priority DHs.

**Day-boundary handling (important, found via testing):** the CX-cutoff hour-of-day is **clamped, not wrapped**, on both sides — `>= 1440` (a full day-0 cycle has elapsed) → treated as 100% capture; `< 0` (preponed before the reference start) → treated as ~0% capture (too early, nothing placed yet). A naive `% 1440` modulo creates a mirror-image artifact on *both* sides: wrapping a late departure into "start of a fresh cycle" (artificially near-zero) or wrapping an over-preponed departure into "late previous cycle" (artificially near-100%, which nearly caused a real regression during testing — see PLAYBOOK.md).

**Config:** `dock_productivity_per_hour` (100), `dock_transition_buffer_min` (30), `adhoc_dock_reserve_pct` (0.05 — this fraction of a MH's docks is set aside for ad-hoc trucks so they're never modeled in the committed-route ILP), `low_priority_top266_threshold` (10), `cx_cutoff_multi_dh_hours` (3), `cx_cutoff_single_dh_hours` (2), `cx_cutoff_processing_hours` (1), `dock_time_granularity_min` (10, discretization for the ILP's candidate departure times), `dock_search_window_hours` (18, a computational bound only — not a business floor; there is deliberately no minimum-departure-time constraint). `MHConfig.n_docks` comes from `MHDH_RateCard.xlsx`'s `Docks` column.

**Rollover mechanism (companion piece, lives in `agent4_freeze_day.build_freeze_day_location_file`):** a DH with `top266_shipments < low_priority_top266_threshold` gets its *feasibility* window (`time_window_end`) relaxed by +1 day (e.g. 1800 → 3240), allowing routes to be generated with a much later TMS than that DH's true deadline would otherwise permit — trading that DH's own D1% (it explicitly rolls to D+2) for better speed on the higher-priority DHs sharing its route. `d1_true_threshold` (always the unrelaxed 1800) is tracked separately and used only for speed measurement, never for feasibility.

**Outputs** (written to the same `out_dir` as the freeze-day pipeline): `Dock_Schedule.csv` (per-route TMS + internal Placement_Time), `Route_Speed.csv` (per-route CX cutoff, capture fraction, speed%), `DH_Speed.csv` (per-DH arrival, true-threshold pass/fail, weighted contribution), `Speed_Summary.csv` (per-MH: docks total/committed, route count, weighted speed%).

**Failure reporting**: if no dock-feasible schedule exists for an MH even preponed to the search-window bound, this returns `status="failed"` with an explicit `dock_schedule_infeasible` issue — never silently forces an infeasible schedule. Same "never hide a failure" principle as the ILP coverage-failure rule below.

---

## OSRM Reporting — Mandatory

After every Agent 4 run, report:
- Total OSRM calls attempted
- Calls succeeded / failed
- If any failed: list the exact pairs (origin → destination)

Never summarise as "some failed" — always give the exact count and pairs.

Data source: `r['data']['n_osrm_calls']` (total attempted) and `r['data']['osrm_fallback_df']` (one row per OSRM-filled pair, columns: `origin`, `destination`, `distance_km`, `transit_minutes`). Also written to `osrm_fallback_log.csv` in the output directory.

### Enriching Distance Matrix after OSRM fallbacks

If `r['data']['osrm_fallback_df']` is non-empty:

1. Show the user the pairs (`origin`, `destination`, `distance_km`)
2. Ask: "Should I add these N pairs to Distance Matrix.csv?"
3. On approval: read `Inputs\Distance Matrix.csv`, add a `source` column (existing rows = `original`, new rows = `osrm_fallback`), append the new pairs mapping `origin`→`S_Code` and `destination`→`D_Code`, deduplicate on (`S_Code`, `D_Code`) keeping existing rows, save back to `Inputs\Distance Matrix.csv`.
4. Confirm how many rows were added.

---

## ILP Failure Reporting — Mandatory

**Never report a cost number for an MH where any bearing cluster's ILP failed.** A failed cluster means every DH in that cluster is uncovered — the reported cost for that MH is silently missing that entire cluster's milkrun cost, not just "slightly off." `status="ok"` at the pipeline level does **not** mean every MH's cost is complete — it only reflects that the run finished without a hard error.

**Detection — check after every run, per MH, before trusting its cost:**
- `Agent4MHResult.ilp_status` — dict of `cluster_id → "SUCCESS" | "FAILED"`. Any `"FAILED"` value means that cluster's cost is missing.
- `Agent4MHResult.missing_dhs` — non-empty means those DHs have zero routes and zero cost attributed to them.
- `validation_report_agent4.txt` / `on_progress` log lines — look for `WARN: ILP FAILED for cluster X; uncovered: [...]`.
- **Freeze-day engine (`agent4_freeze_day.py`):** `run_freeze_day_candidate` calls the same underlying `run_agent4_for_mh` per candidate day, so `ilp_status`/`missing_dhs` are available on `mh_result` for **every candidate**, not just the optimal one. Check all of them, not only the day that gets selected as optimal — a structural failure (bad distance data, an impossible time window) depends only on distance/time-window/position constraints, none of which vary by simulated day, so it will normally repeat identically across every real and synthetic candidate for that MH. If a DH is uncovered on one candidate, expect it uncovered on all of them.

**What to report instead of a cost number:**
> "Computation FAILED for [MH name] — cluster [id] could not be covered. Uncovered DH(s): [list]. Reported cost for this MH is INCOMPLETE and should not be used."

Then give the likely root cause and fix, using this checklist (in order of likelihood):
1. **Missing lat/long** — DH absent from `Lat Longs.xlsx` → bearing defaults to 0° (due north placeholder) → no valid permutations. This is the most common cause (see PLAYBOOK.md "ILP cluster failure — DH missing from Lat Longs.xlsx"). Diagnose: check if every uncovered DH is present in `Lat Longs.xlsx`'s `Site_name` column.
2. **Missing distance data** — a required MH↔DH or DH↔DH leg absent from the distance matrix and OSRM also failed/unavailable. Diagnose: check `dist_dict` for the uncovered DH's pairs; check `osrm_fallback_log.csv`/OSRM reporting section above for failures involving that DH.
3. **Genuinely infeasible time window** — the DH is far enough from its MH that even a direct single-stop route (`transit_time(distance) + service_time`) would arrive after `time_window_end` for every possible position, no matter the route composition. Diagnose: manually compute `depot_departure + get_transit_time(dist_to_dh)` and compare against that DH's `time_window_end`. Fix options: widen the DH's time window (if the SLA data supports it), reduce its `depot_departure`, or reassign it to a nearer MH (an Agent 3 question, not an Agent 4 one).

**Never** paper over this by reporting the MH's cost as-is with a footnote — the failure must be the headline of that MH's result, not a side note after a wrong number.

---

## 8. Issue Types Reference

Every `result["issues"]` entry has shape `{"type": str, "detail": str}`.

| `type` | Source function | What it means | How to respond |
|---|---|---|---|
| `missing_ml` | `build_location_file` | A DH in Agent 3's assignment output has no matching row in DH Feasibility.csv | Add the DH's ML value to DH Feasibility.csv. Re-run `build_location_file`. Do not call the pipeline until all missing_ml issues are resolved and null-ML rows are dropped. |
| `null_ml` | `preflight_check` | One or more rows in the location file have null ML | Drop null-ML rows from the location file (`loc_df.dropna(subset=["ML"])`). If those DHs must be included, add them to DH Feasibility.csv first. |
| `invalid_ml` | `preflight_check` | An ML value is not in `{6.5, 8, 10, 14, 17, 20, 22, 24, 32, 40}` | Correct the ML value in DH Feasibility.csv. Re-run `build_location_file` and `preflight_check`. |
| `mh_not_in_rate_card` | `preflight_check`, `run_agent4_pipeline` | One or more `current_fc_mh` values do not appear in `MHDH_RateCard.xlsx` | Add the MH to the rate card with appropriate local/zonal rates per vehicle size. Currently known: `CENTRALHUB_LM_AJLX`, `CENTRALHUB_LM_IXA3X`, `CENTRALHUB_L_JLRSF1`, `CENTRALHUB_L_KLM1`. When emitted by `run_agent4_pipeline` (not `preflight_check`), the pipeline continues with default rate cards — routes for this MH will have cost of zero or sentinel values. |
| `dh_missing_distance` | `preflight_check` | One or more DHs have no entry (as either S_Code or D_Code) in the distance matrix | Add the DH's distance pairs to the distance matrix. Or, if OSRM is available, distances will be fetched at runtime but should be back-filled afterwards. |
| `mh_missing_distance` | `preflight_check` | One or more MHs have no entry in the distance matrix | Add the MH's distance pairs to the distance matrix. Currently known: `CENTRALHUB_L_AURPRC1`, `CENTRALHUB_L_SRTSFL1`. |
| `missing_distance` | `build_distance_dict` | A row in the distance matrix has a non-numeric `distance` value | Inspect the source row in `Distance Matrix.csv`. Fix or remove the malformed row. The pair is skipped; OSRM will attempt to fill it at runtime. |
| `invalid_latlong` | `build_latlong_dict` | A row in `Lat Longs.xlsx` has non-numeric Latitude or Longitude | Fix the value in the source file. The affected site will have no lat/long in the dict; if it is an MH depot, `run_agent4_for_mh` will skip the entire MH. |
| `missing_column` | `run_agent4_pipeline` | A required column (from config key definitions) is absent in `location_file_df` | The pipeline aborts immediately with `status="failed"`. Check that `build_location_file` was used to generate the DataFrame, or that manual construction includes all required columns. |
| `no_feasible_routes` (ILP FAILED) | `run_agent4_for_mh` | The ILP solver could not find a feasible cover for a bearing cluster | Not reported as a result dict `issue`; tracked in `Agent4MHResult.ilp_status[cluster_id] = "FAILED"` and `Agent4MHResult.missing_dhs`. DHs in the failed cluster appear in `missing_dhs`. Cause is usually: all routes in the cluster were pruned (no valid permutation passed time-window or distance filters). Investigate by checking distance data availability and time-window constraints for the affected DHs. |

---

## 9. Phase 2 Interface

Phase 2 (`agent3_phase2.py`) imports 5 names directly from the Agent 4 backend. These signatures are frozen — do not change them.

### Exact function signatures Phase 2 calls

```python
from agent4 import (
    run_agent4_for_mh,      # exact signature preserved
    derive_freq_allowed,    # exact signature preserved
    assign_vehicle_length,  # exact signature preserved
    Agent4MHResult,         # dataclass — osrm_log field added at end with default=[]; safe
    MHConfig,               # dataclass — unchanged
)
```

**`run_agent4_for_mh` signature (Phase 2 calls this directly):**
```python
run_agent4_for_mh(
    mh_name: str,
    mh_cfg: MHConfig,
    dh_df: pd.DataFrame,
    dist_dict: dict[tuple[str, str], float],
    latlong: dict[str, tuple[float, float]],
    cfg: dict[str, Any],
    on_progress: Optional[Any] = None,
    residual_threshold: float = 100.0,
) -> Agent4MHResult
```

**`MHConfig` fields (Phase 2 sets `min_vehicle_ft = 20.0` for its runs):**
```python
@dataclass
class MHConfig:
    mh_name: str
    local_rate_card: dict[float, float]
    zonal_rate_card: dict[float, float]
    max_hops: int
    threshold_a: float
    threshold_b: float
    service_time_min: int
    city: str = ""
    tag: str = ""
    min_vehicle_ft: float = 6.5   # Phase 2 overrides this to 20.0
```

---

## 10. Known Limitations

**Hub name matching is case-sensitive.** `build_distance_dict` applies `.strip()` only; it does not normalise case. Agent 3 normalises hub names to uppercase during clustering. If the distance matrix file stores any hub name in mixed case (e.g. `Centralhub_STV1` instead of `CENTRALHUB_STV1`), `get_distance` will return `None` for that pair — silently, with no error. The route involving that DH will fail to generate valid permutations. Always verify that hub name casing matches between the distance matrix, location file, and rate card.

**4 MHs missing from rate card.** `CENTRALHUB_LM_AJLX`, `CENTRALHUB_LM_IXA3X`, `CENTRALHUB_L_JLRSF1`, `CENTRALHUB_L_KLM1` are in the location file but not in `MHDH_RateCard.xlsx`. `preflight_check` will fail on check 3 until these are added. When run despite the failure (not recommended), these MHs use default empty rate cards — all route costs compute to `999 × dist × 30`, making every route very expensive but not preventing route selection.

**2 MHs missing from distance matrix.** `CENTRALHUB_L_AURPRC1` and `CENTRALHUB_L_SRTSFL1` have no distance data. `preflight_check` will fail on check 5. Routes for these MHs will depend entirely on OSRM, which requires internet access. In offline environments, these MHs will produce no valid routes.

**40 ft vehicle size gets sentinel cost.** `assign_vehicle_length` returns 40 ft for demand > 2550 CFT, but the rate card has no `Local:40` or `Zonal:40` column. `rate_card.get(40, 999)` returns 999. Monthly cost = `dist × 999 × 30` — no 40 ft route will be selected by ILP. A DH that can only be served by a 40 ft vehicle (ML=40, demand > 2550) will likely end up in `missing_dhs`. To allow 40 ft vehicles, add `Local:40` and `Zonal:40` columns to the rate card.

**OSRM requires internet access.** `get_distance` falls back to `http://router.project-osrm.org/route/v1/driving/...`. In offline environments all OSRM calls fail silently (logged to `osrm_log` as failed attempts if successful — but failures are just logged at WARNING level and `None` is returned). The pipeline continues; pairs with no distance data simply produce no valid routes for those DH-MH combinations. To disable OSRM: currently there is no `use_osrm_fallback` config key — the fallback is always attempted. To suppress it for offline runs, temporarily set all required pairs in the distance matrix before running.

