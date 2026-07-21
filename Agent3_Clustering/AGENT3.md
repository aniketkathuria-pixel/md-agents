# AGENT3 — DH-to-FC_MH Clustering Reference

## ⚠ Refocusing Checkpoint — Read Before Proceeding

Before using anything in this file, verify you can answer these questions from memory:
- What is the difference between a speed assignment and a cost assignment, and what triggers each?
- What are the Top 266 cities, and why do they get special treatment?
- Why is current_fc_mh (resort baseline) used instead of assigned_fc_mh (Phase 1 proposal) in build_location_file?

If you cannot answer all three — stop. Re-read `SUPPLY_CHAIN_CONTEXT.md §§6-8` and `INPUT_CONTEXT.md` entries IN0101, IN0901 before continuing.

Also check: have you read `PROJECT_CONTEXT.md §9` (Problem-First Framework) this session? If not, read it now.

---

## 1. Purpose & Supply Chain Role

Agent 3 answers a single question for every Destination Hub (DH) in the network: which FC_MH should serve it? For each DH, it identifies up to 4 candidate FC_MH sites by haversine proximity, then picks the best one using one of two criteria. If the DH has more than `default_top266_threshold` (default: 5) daily Top266 shipments, it assigns by **speed** — the candidate that maximises D1% (fraction of Top266 shipments arriving at the DH by 6 AM Day N+1, computed from truck speed, load profile, and FBF pathway inventory fractions). For all other DHs it assigns by **cost** — the candidate that minimises total daily Rs (MH→MH trunk cost summed over all hops from seller origin to the FC_MH, plus the MH→DH last-leg cost from the FC_MH to the DH). Agent 3 sits at the junction between Agent 1 (which produces the plan volume rows and FBF pathway data) and Agent 4 (which builds milkrun routes from each FC_MH to its assigned DHs). A wrong FC_MH assignment here cascades directly: the wrong trunk lanes get costed, the wrong milkrun clusters form, and both cost and SLA are wrong from that point forward. Agent 3's output — `dh_fc_mh_assignment.csv` — is the primary topology input Agent 4 consumes.

---

## 2. Pre-call Checklist (Claude's Job)

| Function | Required inputs | Required columns / conditions |
|---|---|---|
| `load_agent3_config` | `path: Path` (optional) | File must be valid JSON if provided; missing keys fall back to defaults |
| `build_distance_lookup` | `dist_df: DataFrame` | `S_Code` (or `S_CODE`), `D_Code` (or `D_CODE`), `distance` — all case/space-insensitive; `distance` must be numeric after `pd.to_numeric` |
| `build_cost_lookup` | `rate_card_df: DataFrame` | `MH1`, `MH2` — normalised match; `C/T` or column containing `/` and `C` — numeric after coerce |
| `build_load_profile_interp` | `load_profile_df: DataFrame` | Column containing both `fulfill_item` and `hr` (case-insensitive); column containing both `order` and `profile` (case-insensitive) |
| `build_route_lookup` | `plan_vol_df: DataFrame` | Columns matching `^MH\d+$` (e.g. `MH1`, `MH2`, …, `MH7`) — at least two such columns with non-null values per row |
| `compute_trip_cost` | `cost_lookup` from `build_cost_lookup`; `dist_lookup` from `build_distance_lookup` (optional); `cfg` dict | `u`, `v` must be hub key strings; `dist_lookup` required for distance-fallback; `hub_lat_lkp` required for OSRM fallback |
| `compute_mhmh_cost` | `plan_vol_df`; `cost_lookup`; `cfg`; optionally `dist_lookup`, `route_lookup`, `pathway_df`, `hub_lat_lkp` | `plan_vol_df` must have `LMHub` column and `MH1`…`MHn` columns; `pathway_df` needed for FBF P2 leg cost (columns: P1 central hub, P2 central hub, P2 pct — case-insensitive contains match) |
| `compute_mhdh_cost` | `dist_lookup` from `build_distance_lookup`; `cfg`; `total_cft: float` | `dh_key` and `candidate_key` must resolve to a pair in `dist_lookup`; OSRM used if missing and `hub_lat_lkp` provided |
| `compute_speed` | `pathway_df`; `dist_lookup`; `load_fn` from `build_load_profile_interp`; `cfg` | `pathway_df` must have P1 central hub column (case-insensitive `p1`+`central`); P2 columns optional |
| `build_dh_portfolio` | `plan_vol_df`; `fbf_agg_df` | `plan_vol_df`: `LMHub`, `stream`, `median_demand_shipments`, `plan_median_cft_volume`; `fbf_agg_df`: `destination_hub`, `fbf_avg_daily_shipments_all`, `cft_cuft_day_avg_all` |
| `build_smh_mhlast_report` | `plan_vol_df`; `cost_lookup`; `cfg`; optionally `dist_lookup`, `hub_lat_lkp` | `plan_vol_df`: `MH1`…`MHn`, `plan_median_cft_volume`, `median_demand_shipments`; optionally `source_type` (controls PH/ALITE zero-first logic) |
| `run_agent3` | All 8 DataFrames + `cfg` + `output_dir` | See individual function requirements above; `fc_mh_df` accepted as raw Plan fbf master (`MH1`+`Tag`) or pre-processed; `lat_long_df` accepted as raw (`Site_name`+`Latitude`+`Longitude`) or pre-processed |
| `run_phase2` | `agent3_output_dir: Path`; `approved_mh_pairs: list[tuple[str,str]]`; `plan_vol_df`, `dist_df`, `cost_df`, `mhdh_rate_card_df`, `location_file_df`, `lat_long_df`, `h2h_df` DataFrames; `cfg`; `output_dir`; `agent4_backend_path: Path` | `agent3_output_dir` must contain `dh_fc_mh_assignment.csv` and `smh_mhlast_cost_per_shipment.csv`; `agent4_backend_path` must be a directory containing `agent4_pipeline.py` |

---

## 3. Function Reference

### `load_agent3_config`
```python
load_agent3_config(path: Optional[Path] = None) -> dict[str, Any]
```
Returns a plain dict (not a result dict). Loads `agent3_config.json` from `path` if provided and the file exists; merges over `_CONFIG_DEFAULTS`. Missing keys always fall back to defaults. JSON parse errors are silently ignored and defaults are used. **Does not raise.**

---

### `build_distance_lookup`
```python
build_distance_lookup(dist_df: pd.DataFrame) -> dict[str, Any]
```
Returns `{"status": "ok"|"failed", "data": dict[(hub_key, hub_key) → km], "issues": [...]}`.

Normalises hub codes with `_norm_hub_key` (strips whitespace, uppercases, removes internal spaces). Skips rows where distance is non-numeric. Column detection is case/space-insensitive: `S_Code`, `S_CODE`, `SCODE` all match.

---

### `build_cost_lookup`
```python
build_cost_lookup(rate_card_df: pd.DataFrame) -> dict[str, Any]
```
Returns `{"status": "ok"|"failed", "data": dict[(MH1_key, MH2_key) → C/T Rs], "issues": [...]}`.

Matches cost column by normalised name `C/T`, `C_T`, `CT`, `COST`, or any column whose raw name contains `/` and `C`. Skips non-numeric rows.

---

### `build_load_profile_interp`
```python
build_load_profile_interp(load_profile_df: pd.DataFrame) -> dict[str, Any]
```
Returns `{"status": "ok"|"failed", "data": Callable[[float], float], "issues": [...]}`.

`data` is an interpolating function `interp(hour: float) → cumulative_fraction`. Input hours are clamped to [0, 24]. Used by `compute_speed` to compute D1%.

---

### `build_route_lookup`
```python
build_route_lookup(plan_vol_df: pd.DataFrame) -> dict[str, Any]
```
Returns `{"status": "ok"|"failed", "data": dict[(SMH_key, DMH_last_key) → [hop1, hop2, ...]], "issues": [...]}`.

Builds a map from `(source MH, last MH)` to the ordered list of MH hops on that lane, extracted from `MH1`…`MHn` columns via `_extract_hops_from_plan_row` (which reads pre-normalised `_k_MH*` columns if present, otherwise uses the raw `MHn` values directly).

**Why this matters for `compute_mhmh_cost`:** passing `route_lookup` to `compute_mhmh_cost` means it resolves the exact sequence of hops from seller origin MH to candidate FC_MH from the actual plan volume data, giving exact hop-by-hop rate card costs. Passing `route_lookup=None` (exploration mode) makes `compute_mhmh_cost` fall back to a shortest-path heuristic — accurate enough for evaluating hypothetical MH pairs that are not in the current plan, but not bitwise-identical to the full pipeline result. Always pass `route_lookup` for production scoring; use `None` only when evaluating a new MH that has no plan rows yet.

---

### `compute_trip_cost`
```python
compute_trip_cost(
    u: str, v: str,
    cost_lookup: dict, dist_lookup: Optional[dict],
    cfg: dict,
    hub_lat_lkp: Optional[dict] = None
) -> dict[str, Any]
```
Returns `{"status": "ok"|"failed", "data": cost_rs or None, "issues": [...]}`.

Thin public wrapper around `_trip_cost_with_fallback`. Fallback chain:
1. `cost_lookup[(u,v)]`
2. `cost_lookup[(v,u)]` (reverse lookup)
3. `dist_lookup[(u,v)] × mh_mh_cost_per_km_fallback` (default 49 Rs/km)
4. OSRM road distance × 49 Rs/km (requires `hub_lat_lkp` and internet)
5. `None` → `status: "failed"`

Issue type `estimated_via_distance` is added (not `failed`) when fallbacks 3 or 4 are used.

---

### `compute_mhmh_cost`
```python
compute_mhmh_cost(
    dh_key: str, candidate_key: str,
    plan_vol_df: pd.DataFrame,
    cost_lookup: dict,
    cfg: dict,
    *,
    dist_lookup: Optional[dict] = None,
    route_lookup: Optional[dict] = None,
    pathway_df: Optional[pd.DataFrame] = None,
    hub_lat_lkp: Optional[dict] = None,
) -> dict[str, Any]
```
Returns `{"status": "ok"|"partial"|"failed", "data": cost_rs, "issues": [...]}`.

`status: "partial"` means cost was computed but one or more edges were missing from the rate card (distance fallback was used; cost may be underestimated if distance data is also sparse).

**route_lookup=None (exploration mode):** when evaluating a candidate FC_MH that has no existing plan rows for this DH (e.g. a new MH or a Phase 2 cross-MH candidate not in the top-4), pass `route_lookup=None`. The function falls back to a heuristic hop resolution. Results are directionally correct but not identical to the full pipeline.

**FBF P2 leg:** the old `compute_mhmh_for_pairs` always omitted the FBF P2 leg cost (goods at P2 Central Hub must travel to P1 before dispatch). `compute_mhmh_cost` includes this leg when `pathway_df` is provided. Always pass `pathway_df` for accurate FBF costs.

---

### `compute_mhdh_cost`
```python
compute_mhdh_cost(
    dh_key: str, candidate_key: str,
    total_cft: float,
    dist_lookup: dict,
    cfg: dict,
    hub_lat_lkp: Optional[dict] = None,
) -> dict[str, Any]
```
Returns `{"status": "ok"|"failed", "data": cost_rs or None, "issues": [...]}`.

Formula: `(total_cft / truck_cft_mh_dh_base) × 2 × distance_km × mh_dh_cost_rs_per_km`. The `× 2` is a round-trip factor. Distance lookup tries `(candidate_key, dh_key)` then OSRM if `hub_lat_lkp` provided.

---

### `compute_speed`
```python
compute_speed(
    dh_key: str, candidate_key: str,
    pathway_df: pd.DataFrame,
    dist_lookup: dict,
    load_fn: Callable[[float], float],
    cfg: dict,
    hub_lat_lkp: Optional[dict] = None,
) -> dict[str, Any]
```
Returns `{"status": "ok"|"partial"|"failed", "data": {"d1_fraction": float, "p1_contrib": float, "p2_contrib": float}, "issues": [...]}`.

D1% = fraction of Top266 shipments arriving at the DH by `dh_arrival_cutoff_hour` (default 6 AM). Computed from truck speed, distance from P1 (and P2 if pathway has P2 inventory), MH processing hours, and load profile interpolator. `status: "partial"` means distance was missing for one of the legs.

---

### `build_dh_portfolio`
```python
build_dh_portfolio(
    plan_vol_df: pd.DataFrame,
    fbf_agg_df: pd.DataFrame,
) -> dict[str, Any]
```
Returns `{"status": "ok"|"partial"|"failed", "data": DataFrame, "issues": [...]}`.

One row per DH. Aggregates NFBF and ALITE volumes from `plan_vol_df` by stream, then left-merges FBF aggregate from `fbf_agg_df`. Output columns: `destination_hub_key`, `nfbf_shipments`, `nfbf_cft`, `alphalite_shipments`, `alphalite_cft`, `plan_rows`, `fbf_shipments`, `fbf_cft`, `top266_shipments`, `lbu_shipments`, `total_dh_cft`.

---

### `build_smh_mhlast_report`
```python
build_smh_mhlast_report(
    plan_vol_df: pd.DataFrame,
    cost_lookup: dict,
    cfg: dict,
    *,
    dist_lookup: Optional[dict] = None,
    hub_lat_lkp: Optional[dict] = None,
) -> dict[str, Any]
```
Returns `{"status": "ok"|"partial", "data": DataFrame, "issues": [...]}`.

Output columns: `smh`, `mh_last`, `total_cft`, `total_shipments`, `total_mh_mh_cost_rs`, `mh_mh_cost_per_shipment_rs`, `cost_complete`, `edges_missing_from_rate_card`.

**Bug that was fixed:** the inline block in `agent3_pipeline.py` used `cost_lookup.get((u,v)) or cost_lookup.get((v,u))` — if neither key existed the edge silently contributed Rs 0, with no logging. Cost-per-shipment was understated for any lane with a missing rate card edge, with no indication in the output.

**After fix:** each edge calls `compute_trip_cost`, which applies the full fallback chain. If the edge still resolves to `None` after all fallbacks (no rate card, no distance, no OSRM), `cost_complete` is set `False` for that row and the missing edge is recorded in `edges_missing_from_rate_card` (pipe-separated, e.g. `CENTRALHUB_L_MUMB->CENTRALHUB_L_MUMX`). The cost contribution from that edge is 0 (not the full fallback estimate) — the flag signals that the reported cost may still be understated, but at least the gap is visible.

**`smh_missing_rate_card_edges.csv`** is written by `run_agent3` as a filtered extract of this report (rows where `cost_complete=False`) so Claude can surface missing edges without re-reading the full report.

---

### `run_agent3`
```python
run_agent3(
    plan_vol_df, fbf_agg_df, pathway_df,
    fc_mh_df, lat_long_df, load_profile_df,
    dist_df, cost_df,
    cfg: dict,
    output_dir: Path,
    *,
    top266_threshold: Optional[float] = None,
    proximity_km_threshold: Optional[float] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    on_dh_progress: Optional[Callable[[int, int, int, int, int], None]] = None,
) -> dict[str, Any]
```
Returns `{"status": "ok"|"partial"|"failed", "data": {paths dict + counts}, "issues": [...]}`.

`fc_mh_df` and `lat_long_df` are normalised internally — pass raw file DataFrames directly (`Plan fbf master.xlsx` with `MH1`+`Tag`; `Lat Longs.xlsx` with `Site_name`+`Latitude`+`Longitude`). Writes 7 output files to `output_dir` (see §6). `on_dh_progress` callback receives `(n_done, n_total, n_speed, n_cost, n_error)`.

---

### `run_phase2`
```python
run_phase2(
    agent3_output_dir: Path,
    approved_mh_pairs: list[tuple[str, str]],
    plan_vol_df, dist_df, cost_df,
    mhdh_rate_card_df, location_file_df,
    lat_long_df, h2h_df: pd.DataFrame,
    cfg: dict,
    output_dir: Path,
    *,
    agent4_backend_path: Path,
    residual_threshold: float = 100.0,
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]
```
Returns `{"status": "ok"|"partial"|"failed", "data": {"excel_paths": [...], "summary_df": DataFrame, "n_pairs": int}, "issues": [...]}`.

**`approved_mh_pairs` must be explicitly passed.** Phase 2 never auto-selects pairs. Claude surfaces savings opportunities from `dh_fc_mh_assignment.csv` (see §8), presents them to the user, and passes only the user-approved pairs. Passing all candidate pairs without review will run Agent 4's ILP dozens or hundreds of times and may take minutes to hours.

**Tuple semantics:** each `(from_mh, to_mh)` tuple means `from_mh` is the **current** assignment of the flagged DHs and `to_mh` is the **proposed new** assignment. `run_phase2` filters `dh_fc_mh_assignment.csv` on `current_fc_mh == from_mh AND assigned_fc_mh == to_mh`. Passing them reversed will find zero flagged DHs and produce an empty pool.

**`agent4_backend_path`:** must be the directory containing `agent4_pipeline.py`. Current value: `C:\Users\aniket.kathuria\Desktop\Claude\Agent 4\backend`. After Agent 4 rewrite: `C:\Users\aniket.kathuria\Desktop\Agentic tools\Agent4_Routing\backend`. The function inserts this path into `sys.path` at call time and imports `agent4_pipeline as p4`. See §8 for the full Agent 4 interface requirement.

---

## 4. Composing Functions

### Full pipeline: load files → build lookups → run_agent3 → outputs

```python
import pandas as pd
from pathlib import Path
import sys
sys.path.insert(0, r"C:\Users\aniket.kathuria\Desktop\Agentic tools\Agent3_Clustering\backend")
sys.path.insert(0, r"C:\Users\aniket.kathuria\Desktop\Claude\Agent 3\backend")  # for agent3_pipeline (imported by agent3.py)
import agent3 as a3

cfg = a3.load_agent3_config(Path(r"...\backend\agent3_config.json"))

plan_vol_df  = pd.read_csv(plan_vol_path, dtype=str)
fbf_agg_df   = pd.read_csv(fbf_agg_path, dtype=str)
pathway_df   = pd.read_csv(pathway_path, dtype=str)
fc_mh_df     = pd.read_excel(plan_fbf_master_path)           # MH1 + Tag columns
lat_long_df  = pd.read_excel(lat_long_path)                  # Site_name + Latitude + Longitude
load_profile_df = pd.read_csv(load_profile_path)
dist_df      = pd.read_csv(dist_matrix_path, dtype=str)
cost_df      = pd.read_csv(mh1_mh2_rate_card_path)

result = a3.run_agent3(
    plan_vol_df, fbf_agg_df, pathway_df, fc_mh_df, lat_long_df,
    load_profile_df, dist_df, cost_df, cfg,
    output_dir=Path(r"...\output\run_YYYYMMDD_HHMMSS"),
)
# result["data"] contains {"assignment_csv": Path, "n_dh": int, "n_speed": int, "n_cost": int, ...}
```

---

### Standalone MH-MH cost query: known DH × candidate

```python
dist_lkp   = a3.build_distance_lookup(dist_df)["data"]
cost_lkp   = a3.build_cost_lookup(cost_df)["data"]
route_lkp  = a3.build_route_lookup(plan_vol_df)["data"]

r = a3.compute_mhmh_cost(
    "SATELLITEHUB_DH_XYZ", "CENTRALHUB_L_BLR1",
    plan_vol_df, cost_lkp, cfg,
    dist_lookup=dist_lkp,
    route_lookup=route_lkp,   # exact hop sequence from plan data
    pathway_df=pathway_df,     # FBF P2 leg included
)
# r["data"] = daily MH-MH cost in Rs; r["status"] = "ok" or "partial"
```

---

### New MH exploration: route_lookup=None

```python
r = a3.compute_mhmh_cost(
    "SATELLITEHUB_DH_XYZ", "CENTRALHUB_L_NEWMH",
    plan_vol_df, cost_lkp, cfg,
    dist_lookup=dist_lkp,
    route_lookup=None,   # no plan rows exist yet for this candidate — heuristic hop resolution
    pathway_df=pathway_df,
)
```

`route_lookup=None` is the right mode when evaluating a candidate MH that has no existing `plan_volume` rows for this DH — e.g. a net-new MH, a Phase 2 cross-MH candidate not in the top-4, or a what-if analysis. The cost will be directionally correct but not bitwise-identical to what the full pipeline would produce once that candidate is actually in plan. Do not use `route_lookup=None` for production assignment decisions.

---

### Phase 2 workflow

```python
# 1. Run Agent 3 Phase 1
r = a3.run_agent3(..., output_dir=agent3_out)

# 2. Claude reads dh_fc_mh_assignment.csv, surfaces savings pairs to user
#    (see §8 for how to read cost_delta_rs)

# 3. User approves specific pairs
approved = [("CENTRALHUB_L_BLR1", "CENTRALHUB_L_AMD1")]

# 4. Run Phase 2 with approved pairs only
r2 = a3.run_phase2(
    agent3_output_dir=agent3_out,
    approved_mh_pairs=approved,
    plan_vol_df=plan_vol_df,
    dist_df=dist_df,
    cost_df=cost_df,
    mhdh_rate_card_df=pd.read_excel(mhdh_rate_card_path),
    location_file_df=pd.read_excel(location_file_path),
    lat_long_df=lat_long_df,
    h2h_df=pd.read_csv(h2h_path),
    cfg=cfg,
    output_dir=phase2_out,
    agent4_backend_path=Path(r"C:\Users\aniket.kathuria\Desktop\Claude\Agent 4\backend"),
)
```

---

### Checkpoint 1 — How to present to user

After `run_agent3` completes, Claude must call two helper functions and present the results as **two separate tables**. Never mix these populations.

```python
candidates = a3.build_phase2_candidates(agent3_df)       # DHs Agent 3 moved
cost_opps   = a3.build_cost_only_opportunities(agent3_df) # DHs Agent 3 kept (informational)
```

**Table 1 — Phase 2 candidates** (valid Phase 2 inputs; user selects from this table only):

| from_mh | to_mh | dh_count | monthly_saving_rs | current_cost_rs | total_cost_rs |
|---|---|---|---|---|---|
| … | … | … | … | … | … |

- Source: `build_phase2_candidates(agent3_df)`
- These are DHs where Agent 3 proposed a move from the resort baseline (`current_fc_mh ≠ assigned_fc_mh`).
- User selects `(from_mh, to_mh)` tuples from this table for `approved_mh_pairs`.

**Table 2 — Cost opportunities** (informational only; NOT valid Phase 2 inputs):

| destination_hub_key | assigned_fc_mh | assignment_basis | cost_delta_rs |
|---|---|---|---|
| … | … | speed | … |

- Source: `build_cost_only_opportunities(agent3_df)`
- These are DHs where Agent 3 kept the resort assignment but a cheaper candidate existed. Speed guardrails prevented the move.
- Present as FYI only. Do NOT offer these as Phase 2 candidates — running Phase 2 on them would flip a speed-constrained DH to a cheaper-but-slower MH, breaking D1% guarantees.

**What Claude asks (after presenting both tables):**
> "Table 1 shows the MH pairs where Agent 3 moved DHs — these are valid Phase 2 candidates. Table 2 shows DHs that stayed put due to speed constraints (FYI only — Phase 2 cannot run on these). Which pairs from Table 1 do you want to run Phase 2 on? (Or type 'none' to skip Phase 2 and proceed to Agent 4.)"

**Never proceed to Phase 2 without a named list of `(from_mh, to_mh)` pairs chosen from Table 1.**

---

## 5. Config Reference

`agent3_config.json` is at `C:\Users\aniket.kathuria\Desktop\Claude\Agent 3\backend\agent3_config.json`. `load_agent3_config` merges file values over `_CONFIG_DEFAULTS` — only keys present in `_CONFIG_DEFAULTS` are accepted from the file; unknown keys in the JSON are ignored.

| Key | Default | In JSON file | Description |
|---|---|---|---|
| `truck_speed_kmh` | `30.0` | Yes | Truck road speed used for transit time calculation (km/h) |
| `mh_dh_processing_hours` | `2.0` | Yes | Hours the MH needs to process shipments before the truck departs for DH |
| `mh_mh_processing_hours` | `6.0` | Yes | Hours processing at an intermediate MH before onward dispatch |
| `truck_cft_mh_mh` | `2400.0` | Yes | Truck capacity for MH→MH legs (cubic feet) — denominator in trip cost formula |
| `truck_cft_mh_dh_base` | `1500.0` | Yes | Truck capacity for MH→DH legs (cubic feet) — denominator in MH-DH cost formula |
| `mh_dh_cost_rs_per_km` | `26.0` | Yes | Rs/km rate for MH→DH last-leg cost |
| `dh_arrival_cutoff_hour` | `6.0` | Yes | Hour of day (0–24) by which Top266 shipments must arrive to count as D1 |
| `default_top266_threshold` | `5.0` | Yes | Daily Top266 shipments above which speed mode is used instead of cost mode |
| `plan_fbf_master_sheet` | `None` | Yes | Excel sheet name for Plan fbf master; `None` = first sheet |
| `lat_long_sheet` | `None` | Yes | Excel sheet name for Lat Longs; `None` = first sheet |
| `fc_mh_tag_value` | `"FC_MH"` | Yes | Tag value in the `Tag` column of Plan fbf master that marks FC_MH rows |
| `use_osrm_fallback` | `True` | **No — code default only** | Whether to attempt OSRM HTTP requests when distance is missing; requires internet |
| `osrm_base_url` | `"http://router.project-osrm.org"` | **No — code default only** | Base URL for OSRM API requests |
| `osrm_request_timeout_s` | `8` | **No — code default only** | Per-request HTTP timeout for OSRM calls (seconds) |
| `osrm_rate_limit_s` | `0.15` | **No — code default only** | Minimum seconds between consecutive OSRM requests |
| `osrm_batch_workers` | `4` | **No — code default only** | ThreadPoolExecutor workers for parallel OSRM pre-fetch |
| `mh_mh_cost_per_km_fallback` | `49.0` | **No — code default only** | Rs/km used when an MH→MH edge has no rate card row and distance is available |
| `default_proximity_km_threshold` | `80.0` | **No — code default only** | Max haversine km for a candidate FC_MH to be included; `≤ 0` disables proximity filter |
| `mh_dh_cost_buffer` | `1.15` | **No — code default only** | Multiplier applied to MH-DH cost in the cost-vs-speed decision; 1.15 = 15% buffer favouring cost-mode candidates |

The 6 keys absent from `agent3_config.json` (`use_osrm_fallback`, `osrm_base_url`, `osrm_request_timeout_s`, `osrm_rate_limit_s`, `osrm_batch_workers`, `mh_mh_cost_per_km_fallback`, `default_proximity_km_threshold`, `mh_dh_cost_buffer`) can be added to the JSON file to override without code changes.

---

## 6. Output Files Reference

All 7 files are written to the `output_dir` passed to `run_agent3`. No timestamped subdirectory is created by `agent3.py` — the caller must construct and pass the desired path.

| Filename | Grain | Key columns | Downstream consumer | Notes |
|---|---|---|---|---|
| `dh_fc_mh_assignment.csv` | One row per DH | `destination_hub_key`, `assigned_fc_mh`, `assignment_mode` (`speed`/`cost`), `candidate_1`…`candidate_4`, `candidate_1_mhmh_cost_rs`…`candidate_4_mhmh_cost_rs`, `candidate_1_mhdh_cost_rs`…`candidate_4_mhdh_cost_rs`, `candidate_1_d1_pct`…`candidate_4_d1_pct`, `top266_shipments` | Agent 4 (topology input), Phase 2, Claude (savings analysis) | Primary output. `cost_delta_rs` column (if present) = cost difference between assigned and cheapest unassigned candidate — used by Claude to surface Phase 2 savings pairs |
| `smh_mhlast_cost_per_shipment.csv` | One row per (SMH, MH_Last) lane | `smh`, `mh_last`, `total_cft`, `total_shipments`, `total_mh_mh_cost_rs`, `mh_mh_cost_per_shipment_rs`, `cost_complete`, `edges_missing_from_rate_card` | Phase 2 (MHMH trunk cost per DH), Claude (cost analysis) | Primary input to Phase 2 gap-fill. `cost_complete=False` rows mean costs may still be understated even after the bug fix — if `dist_lookup` also had gaps, the distance fallback also failed and contributed 0. Review `edges_missing_from_rate_card` and cross-reference with Agent 2's distance matrix before trusting cost-mode assignments on those lanes. |
| `smh_missing_rate_card_edges.csv` | One row per incomplete (SMH, MH_Last) lane | Same columns as above, filtered to `cost_complete=False` | Claude (data quality triage) | New output added by this rewrite. Empty file (header only) if all edges resolved. Claude should surface this to the user before running Phase 2. |
| `agent3_summary.csv` | One row per DH | `destination_hub_key`, `assigned_fc_mh`, `assignment_mode`, `total_dh_cft`, `top266_shipments`, `mhmh_cost_rs`, `mhdh_cost_rs`, `d1_pct` | Claude (summary reporting) | Collapsed view; lacks per-candidate detail. Use `dh_fc_mh_assignment.csv` for Phase 2 inputs. |
| `agent3_missing_distance_pairs.csv` | One row per missing (origin, dest) pair | `origin`, `destination`, `reason` | Claude (data quality triage), Agent 2 (gap-fill trigger) | Pairs where distance was missing from the matrix and OSRM also failed (or was disabled). Agent 2 can be triggered to patch these via OSRM or haversine. |
| `validation_report_agent3.txt` | Free-text | N/A | Claude (post-run diagnostics) | Human-readable run summary: counts of speed/cost/error assignments, OSRM hits, missing pairs. |
| `hub_network_map.html` | Interactive HTML | N/A | Human review | Leaflet-based map of assigned DH→FC_MH arcs. Imported from `agent3_pipeline._build_network_map_html` — not duplicated in `agent3.py`. |

---

## OSRM Reporting — Mandatory

After every Agent 3 run, report:
- Total OSRM calls attempted
- Calls succeeded / failed
- If any failed: list the exact pairs (origin → destination)

Never summarise as "some failed" — always give the exact count and pairs.

Data source: read `agent3_missing_distance_pairs.csv` (path in `r['data']['missing_pairs_csv']`). Columns: `from_hub_key`, `to_hub_key`, `reason`, `assumed_distance_km`, `assumed_cost_per_trip_rs`. Rows where `reason == "osrm_fallback"` are pairs OSRM filled successfully. Rows with other reasons (`missing_distance`, etc.) are pairs where OSRM also failed and no distance was used.

### Enriching Distance Matrix after OSRM fallbacks

If `agent3_missing_distance_pairs.csv` contains rows with `reason == "osrm_fallback"`:

1. Show the user the pairs (`from_hub_key`, `to_hub_key`, `assumed_distance_km`)
2. Ask: "Should I add these N pairs to Distance Matrix.csv?"
3. On approval: read `Inputs\Distance Matrix.csv`, add a `source` column (existing rows = `original`, new rows = `osrm_fallback`), append the new pairs mapping `from_hub_key`→`S_Code` and `to_hub_key`→`D_Code`, deduplicate on (`S_Code`, `D_Code`) keeping existing rows, save back to `Inputs\Distance Matrix.csv`.
4. Confirm how many rows were added.

---

## 7. Issue Types Reference

All public functions return `{"status": ..., "data": ..., "issues": [...]}`. `issues` is always a list; it may be empty even on `status: "ok"`. `status: "partial"` means the function completed but with degraded accuracy; `status: "failed"` means `data` is `None`.

| Issue type | Which functions | What it means | What Claude should do |
|---|---|---|---|
| `missing_columns` | `build_distance_lookup`, `build_cost_lookup`, `build_load_profile_interp`, `compute_mhmh_cost`, `build_dh_portfolio`, `compute_speed` | A required column was not found in the input DataFrame (after case/space normalisation) | Abort. Report which columns are missing. Re-check the DataFrame being passed — wrong file, wrong sheet, or column renamed. |
| `missing_edge` | `compute_trip_cost`, `compute_mhmh_cost` | A specific (u,v) MH→MH edge was not found in the rate card after all fallbacks, or resolved to `None` | For `compute_trip_cost`: `status: "failed"` — the edge is unresolvable; add it to the rate card or provide `dist_lookup`. For `compute_mhmh_cost`: `status: "partial"` — cost is underestimated; log and continue. |
| `estimated_via_distance` | `compute_trip_cost` | Rate card had no entry; cost was estimated as `distance × mh_mh_cost_per_km_fallback` | Note in output. Cost is an approximation; finance should confirm the actual rate. |
| `missing_distance` | `compute_mhdh_cost`, `compute_speed` | Distance matrix had no entry for a required (origin, dest) pair and OSRM was unavailable or failed | For `compute_mhdh_cost`: `status: "failed"`. For `compute_speed`: `status: "partial"`. Add pair to distance matrix or enable OSRM. |
| `no_plan_rows` | `compute_mhmh_cost` | `plan_vol_df` has no rows for the given DH key | `status: "failed"`. DH key may be mis-normalised (check capitalisation/spaces) or absent from plan — verify DH is in the plan volume file. |
| `missing_rate_card_edges` | `build_smh_mhlast_report` | One or more MH→MH edges across all lanes could not be resolved; `cost_complete=False` rows exist | `status: "partial"`. Surface `smh_missing_rate_card_edges.csv` to user. Identify lanes via `edges_missing_from_rate_card` column and request rate card update from the data owner. |
| `fc_mh_normalization_failed` | `run_agent3` | `fc_mh_df` was passed but had neither `fc_mh_key` column (pre-processed) nor a `Tag` + `MH1` column combination (raw Plan fbf master) | `status` will eventually be `failed` if no FC_MH candidates can be identified. Check the file being passed for `fc_mh_df`. |
| `pipeline_error` | `run_agent3`, `run_phase2` | An unexpected Python exception occurred in the pipeline body | `status: "failed"`. The full traceback is in `detail`. Fix the underlying error before retrying. |
| `error` | All lookup builders | Unexpected Python exception | `status: "failed"`. Check `detail` for traceback. Usually a dtype or file format issue. |

---

## 8. Phase 2 Deep Reference

Phase 2 re-evaluates the DH→FC_MH assignment for "contested" DHs — those that could plausibly be cheaper or faster at a neighbouring MH. It is invoked per MH pair, not per DH, and always requires human approval before running.

### How the search works

For each approved `(mh1, mh2)` pair, Phase 2 identifies the "pool" of DHs currently assigned to `mh1` that are within the H2H MR-group of `mh2` (via `expand_pool`). It then finds the cheapest assignment of these pool DHs between the two MHs:

**Full enumeration** (`|pool| ≤ 15`)  
Iterates all `2^N` binary assignments (each DH goes to `mh1` or `mh2`). Evaluates every candidate with Agent 4's full ILP. Total Agent 4 ILP calls = **`2^N × 2`** (two MHs per evaluation).

**LNS — Large Neighborhood Search** (`|pool| > 15`, `LNS_ITERATIONS = 500`)  
Starts from current assignment. Each iteration: destroys `k = max(2, N//5)` DHs (random, seed=42), repairs greedily using `_dh_direct_cost + MHMH` per DH, then evaluates the repaired full assignment with Agent 4 ILP. Total Agent 4 ILP calls = **`500 × 2`** = 1000 (plus 2 for the initial baseline).

**Scoring** = Agent 4 MHDH routing cost (`r1.total_monthly_cost + r2.total_monthly_cost`) + pre-computed MHMH trunk cost per DH per MH. MHMH costs are fetched from Agent 3 candidate columns (exact) or recomputed via `compute_mhmh_cost` (gap-fill, includes FBF P2 leg via `pathway_df`).

### How Claude surfaces savings opportunities

Read `dh_fc_mh_assignment.csv` after `run_agent3`. The column `cost_delta_rs` (if present) is the daily cost difference between the assigned FC_MH and the cheapest alternative candidate. Group by `assigned_fc_mh` to find MH pairs where a material number of DHs have non-trivial `cost_delta_rs`. Candidate savings pairs are MH pairs where the second-cheapest FC_MH is at a different MH and the aggregate daily delta × 30 exceeds a meaningful threshold (e.g. Rs 50,000/month). Present these pairs to the user with estimated monthly savings and pool size (which determines runtime — see formula above). The user approves or rejects each pair; pass only approved pairs to `run_phase2`.

### Agent 4 interface requirement

Phase 2 calls the following from `agent4_pipeline`:
- `p4.run_agent4_for_mh(mh_name, mh_cfg, dh_df, dist_dict, latlong, cfg, residual_threshold)` → `Agent4MHResult` with `.total_monthly_cost` attribute
- `p4.Agent4MHResult` dataclass
- `p4.MHConfig` dataclass
- `p4.derive_freq_allowed(top266_shipments)` → int (1 or 2)
- `p4.assign_vehicle_length(demand_cft)` → float (vehicle size in ft)

After the Agent 4 rewrite, the new `agent4.py` **must expose all of the above** under these exact names. If the rewrite renames or restructures any of them, `agent3_phase2.py` must be updated before Phase 2 can run.

### Agent 4 ILP call count formula

Before approving a Phase 2 run, Claude should compute and show the user:

```
pool_size = number of pool DHs for this pair
if pool_size <= 15:
    ilp_calls = 2^pool_size × 2      # full enumeration
else:
    ilp_calls = (500 + 1) × 2        # LNS: 500 iterations + 1 baseline
```

Each Agent 4 ILP call typically takes 1–10 seconds depending on pool size and cluster count. Warn the user if `ilp_calls > 200` (> ~10 minutes expected).

### `agent4_backend_path` — current vs post-rewrite

| State | Path |
|---|---|
| **Current (old Agent 4)** | `C:\Users\aniket.kathuria\Desktop\Claude\Agent 4\backend` |
| **After Agent 4 rewrite** | `C:\Users\aniket.kathuria\Desktop\Agentic tools\Agent4_Routing\backend` |

`run_phase2` inserts this path into `sys.path` at call time. After the Agent 4 rewrite, update this path in every call to `run_phase2`. The old Agent 4 code at `Claude\Agent 4\backend` should still work for Phase 2 as long as its `agent4_pipeline.py` exposes the interface above.

---

## 9. Known Limitations

| Limitation | Detail |
|---|---|
| MFC city heuristic limited to FRN and BAG patterns | `_is_real_central_hub` rejects values starting with `"NO P"` (sentinel for "no P2 hub"). Other null sentinel patterns used in the data source are not handled — they would be treated as real hubs and produce wrong pathway lookups. |
| OSRM fallback requires internet | `use_osrm_fallback: True` (default) will attempt HTTP requests to `router.project-osrm.org`. In environments without internet (VPN-only, offline batch), set `use_osrm_fallback: false` in `agent3_config.json` or pass `cfg["use_osrm_fallback"] = False` before calling `run_agent3`. |
| Phase 2 not tested end-to-end with new `agent3.py` | `run_phase2` in `agent3.py` was written and statically validated but not exercised with a live Agent 3 output in the current session. The first real Phase 2 run should be manually validated: check that `dh_fc_mh_assignment.csv` is being read correctly, that MH-MH gap-fill produces non-null costs, and that Agent 4 output Excel files are written with the expected sheets. |
| `compute_mhmh_for_pairs` removed | The old `agent3_phase2.py` gap-fill used `compute_mhmh_for_pairs` (from `agent3_pipeline`), which always called with `route_lookup=None` and no `pathway_df`. This function is not exposed in `agent3.py`. Phase 2 gap-fill now calls `compute_mhmh_cost` directly with `route_lookup` and `pathway_df`. If `agent3_phase2.py` is called directly (not via `agent3.py`), it still uses the old gap-fill path. |
| `cost_complete=False` rows may still be underestimated | Even after the silent-zero bug fix, a lane where the distance fallback also failed (no rate card entry, no distance matrix entry, no OSRM) will have `cost_complete=False` and the missing edge contributes Rs 0 to the reported cost. The fix makes the gap visible; it does not fill it. |
| Proximity filter may eliminate correct assignments | `default_proximity_km_threshold: 80.0` discards FC_MH candidates beyond 80 km haversine. For DHs in sparse network areas where the nearest FC_MH is >80 km away, all 4 candidates may be filtered, and the DH falls back to current FC enforcement (the DH's existing FC_MH is forced in). Set `proximity_km_threshold=0` to disable this filter if DH assignment errors are observed for remote DHs. |
