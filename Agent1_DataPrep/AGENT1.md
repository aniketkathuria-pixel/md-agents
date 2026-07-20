# AGENT1 — Data Preparation Tool Library Reference

## ⚠ Refocusing Checkpoint — Read Before Proceeding

Before using anything in this file, verify you can answer these questions from memory:
- What are the three source types (MH1 types) in the network, and how does each affect cost treatment?
- What is the difference between FBF, NFBF, and Alite streams?
- What does the Resort file represent, and why is it the ground truth?

If you cannot answer all three — stop. Re-read `SUPPLY_CHAIN_CONTEXT.md` and `INPUT_CONTEXT.md` before continuing.

Also check: have you read `PROJECT_CONTEXT.md §9` (Problem-First Framework) this session? If not, read it now.

---

## 1. Purpose

Agent 1 is a composable Python tool library (`backend/agent1.py`) that transforms raw Flipkart linehaul input files into structured demand and actuals tables consumed by Agents 3 and 4. It loads input files into DataFrames, normalises columns, performs tagging and volume aggregation, and returns results as standard dicts. Agent 1 does **not** perform file discovery (the caller provides all paths), does **not** create timestamped output directories (the caller controls where output goes), does **not** call any orchestrator or register state, does **not** call `sys.exit()` under any condition, and does **not** enforce a validation gate that blocks downstream work — every function returns `status="partial"` with `issues` populated rather than failing hard when optional inputs are missing or data quality is degraded.

**Note on stream naming:** "Alpha" and "FBF" are used interchangeably in Flipkart's input files. The JJA Alpha SD Plan file IS the FBF day plan. When AGENT1.md refers to "FBF day plan", it means the Alpha SD plan file.

---

## 2. Pre-call Checklist (Claude's Job)

Before calling any function, Claude must verify that the required files exist and contain the required columns. Use `get_required_columns(function_name)` to retrieve the authoritative list programmatically. The table below is the complete static reference.

| Function | Input parameter | Required columns | Typical filename pattern |
|---|---|---|---|
| `load_resort` | `path` | `MH1`, `LMHub`, `PATH` | |
| `load_lm_fdp` | `path` | `fulfill_item_service_profile`, `order_item_unit_source_facility`, `source_pincode`, `customer_pincode`, `analytic_vertical`, `logistics_carrier` | |
| `load_cft_vertical` | `path` | Any column whose `_norm_str` equals `vert`; any column containing both `vol` and `ship` in its normalised name | |
| `load_fbf_day_plan` | `path` | `source`, `destination_hub`, `destination_pincode`, `seller`, `sc`, `vendor`, `day_1` through `day_30` | |
| `load_sd_plans` | `alpha_path` | `source`, `destination_hub`, `sc`, `vendor`, `day_1` … `day_30` | |
| `load_sd_plans` | `alite_path` | `hub` (not `destination_hub`), `source`, `sc`, `vendor`, `day_1` … `day_30` | |
| `load_sd_plans` | `nfbf_path` | `source`, `destination_hub`, `vertical`, `vendor`, `day_1` … `day_30` | |
| `load_fbf_network_pathway` | `path` | Columns matching `P{i} DC`/`P{i} FC` pattern for i=1..5; columns matching `P{i}%`/`P{i}_pct`/`P{i}_share` pattern for i=1..5 | |
| `load_mh1_tagging` | `path` | `MH1`, any column containing `tag` in its normalised name | |
| `load_lm_pbh` | `path` | `pincode`, any column containing `hub` but not `type`/`state`/`city` in its normalised name | |
| `load_fm_pbh` | `path` | `pincode`, any column whose normalised name contains both `associated` and `mh`, or both `central` and `hub` | |
| `load_fc_map` | `path` | `mh_code`, `mh_name` | |
| `parse_resort` | `resort_df` | `MH1`, `LMHub`, `PATH` | |
| `tag_mh1` | `resort_df` | `MH1` | |
| `tag_mh1` | `tagging_df` (optional) | `MH1`, any column containing `tag` in its normalised name | |
| `join_demand` | `resort_tagged_df` | `MH1`, `LMHub` | |
| `join_demand` | `demand_df` (optional) | `MH1`, `LMHub`, `augmedian`, `augpeak`, `newdemandret` | |
| `build_plan_volume` | `plan_df` | `MH1`, `LMHub`, `has_demand_data` | |
| `build_plan_volume` | `tagging_df` (optional) | `MH1`, column containing `tag` in normalised name | Same tagging file as `load_mh1_tagging`; required to preserve `source_type="PH"` for untagged hubs |
| `filter_actuals` | `fdp_df` | `logistics_carrier`, `fulfill_item_service_profile` | |
| `resolve_destinations` | `fdp_df` | `customer_pincode` | |
| `resolve_destinations` | `lm_pbh_df` (optional) | `pincode`, hub column (see `load_lm_pbh` above) | |
| `resolve_sources` | `fdp_df` | `order_item_unit_source_facility`, `fulfill_item_service_profile`, `source_pincode`, `analytic_vertical` | |
| `resolve_sources` | `fm_pbh_df` (optional) | `pincode`, associated MH / central hub column | |
| `resolve_sources` | `fc_map_df` (optional) | `mh_code`, `mh_name` | |
| `resolve_sources` | `resort_df` (optional) | `MH1`, `LMHub`, `last_mh`, `second_last_mh` (output of `parse_resort`) | |
| `compute_cft` | `fdp_df` | `analytic_vertical`, `fulfill_item_service_profile` | |
| `compute_cft` | `cft_lookup_df` | `vertical_norm`, `avg_cft_cuft` (output of `load_cft_vertical`) | |
| `aggregate_actuals` | `fdp_df` | `source`, `destination`, `stream`, `source_type`, `analytic_vertical`, `shipment_cft` | |
| `validate_fbf_plan_columns` | `fbf_day_plan_path` | `source`, `destination_hub`, `destination_pincode`, `seller`, `sc`, `vendor`, `day_{start}` … `day_{end}` | "Alpha SD Plan" in filename |
| `validate_sd_plan_columns` | `alpha_path` | `source`, `destination_hub`, `sc`, `vendor`, `day_{start}` … `day_{end}` | "Alpha SD Plan" in filename |
| `validate_sd_plan_columns` | `alite_path` | `source`, `hub` or `destination_hub`, `sc`, `vendor`, `day_{start}` … `day_{end}` | "Alite SD Plan" in filename |
| `validate_sd_plan_columns` | `nfbf_path` | `source`, `destination_hub`, `vertical`, `vendor`, `day_{start}` … `day_{end}` | "NFBF SD Plan" in filename |
| `build_fbf_aggregate` | `fbf_day_plan_df` or `fbf_day_plan_path` | `source`, `destination_hub`, `destination_pincode`, `seller`, `sc`, `vendor`, `day_{start}` … `day_{end}` | "Alpha SD Plan" in filename |
| `build_fbf_aggregate` | `top266_df` | `pincode`, column whose normalised name contains both `final` and `mapping` | "top266", "Top 266", or "pincode" in filename; verify has `pincode` + `Final Mapping` columns |
| `build_fbf_aggregate` | `cft_vertical_df` | `vertical_norm`, `avg_cft_cuft` (output of `load_cft_vertical`) | |
| `build_sd_plan_aggregate` | `alpha_df` | `source`, `destination_hub`, `sc`, `vendor`, `day_1` … `day_30` | "Alpha SD Plan" in filename |
| `build_sd_plan_aggregate` | `alite_df` | `hub`, `source`, `sc`, `vendor`, `day_1` … `day_30` | "Alite SD Plan" in filename |
| `build_sd_plan_aggregate` | `nfbf_df` | `source`, `destination_hub`, `vertical`, `vendor`, `day_1` … `day_30` | "NFBF SD Plan" in filename |
| `build_sd_plan_aggregate` | `mh_dh_mapping_df` | Column whose `_norm_header_key` equals `dc_ph`; column whose `_norm_header_key` equals `mh_1`. Positional fallback: columns[0] = DC/PH key, columns[1] = MH-1 value. | |
| `build_sd_plan_aggregate` | `cft_vertical_df` | `vertical_norm`, `avg_cft_cuft` | |
| `build_fbf_network_pathway` | `pathway_df` | P1..P5 DC/FC columns, P1..P5 % columns | |
| `build_fbf_network_pathway` | `tagging_df` (optional) | `MH1` | |
| `build_fbf_network_pathway` | `fc_map_df` (optional) | `mh_code`, `mh_name` | |
| `build_fbf_network_pathway` | `mh_dh_map_df` (optional) | DC/PH column, MH-1 column (same as `mh_dh_mapping_df` above) | |
| `save_dataframe` | `df`, `path` | None — writes whatever DataFrame is passed | |
| `get_required_columns` | `function_name` (str) | None | |

---

## 3. Function Reference

### `load_agent1_config(config_path=None)`
```
Returns: dict (complete config with all keys populated from defaults + JSON + any overrides)
```
Public config loader. Merges `agent1_config.json` over built-in defaults. Safe to call with no arguments — reads the JSON next to `agent1.py` automatically. To apply per-run overrides, pass the returned dict directly to any agent function via its `config=` parameter rather than modifying the JSON file.
```python
cfg = load_agent1_config()
cfg["fbf_plan_day_start"] = 31
cfg["fbf_plan_day_end"]   = 60
cfg["fbf_plan_avg_divisor"] = 30
result = build_sd_plan_aggregate(..., config=cfg)
```

---

### `load_resort(path)`
```
Returns: {"status": "ok"|"failed", "data": DataFrame|None, "issues": [...]}
```
Reads CSV, XLSX, or XLSB. Returns `failed` if any of MH1, LMHub, PATH/paths columns are absent. Returns raw DataFrame — no transformation applied; call `parse_resort` next.

---

### `load_lm_fdp(path)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Returns `partial` (not `failed`) when required columns are missing — the caller decides whether to abort. Suitable for partial actuals processing when not all columns are needed.

---

### `load_cft_vertical(path, sheet=None)`
```
Returns: {"status": "ok"|"failed", "data": DataFrame|None, "issues": [...]}
```
Column detection: looks for `_norm_str(col) == "vert"` for the vertical name column; looks for both `"vol"` and `"ship"` in `_norm_str(col)` for the CFT volume column. Returns a DataFrame with columns `vertical`, `avg_cft_cuft`, `vertical_norm`. Pass the `data` directly as `cft_lookup_df` or `cft_vertical_df` to downstream functions.

---

### `load_fbf_day_plan(path)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Returns `partial` if required columns or day_N columns are missing — does not fail hard because the caller may still want to inspect or log the file.

---

### `load_sd_plans(alpha_path, alite_path, nfbf_path)`
```
Returns: {"status": "ok"|"partial"|"failed", "data": {"alpha": DataFrame|None, "alite": DataFrame|None, "nfbf": DataFrame|None}, "issues": [...]}
```
Returns `failed` only when all three reads fail. Returns `partial` when one or two fail. **Important:** `result["data"]` is a dict, not a DataFrame. Unpack before passing to `build_sd_plan_aggregate`:
```python
plans = result["data"]
build_sd_plan_aggregate(plans["alpha"], plans["alite"], plans["nfbf"], ...)
```
If one stream is `None`, pass an empty DataFrame: `pd.DataFrame()` — `build_sd_plan_aggregate` handles empty streams gracefully.

**Large file handling:** Automatically uses chunked reading (500k rows/chunk) with EKL vendor filter applied per chunk for any CSV file exceeding 500 MB. The 39 GB NFBF file is handled transparently — caller does not need to pre-filter or chunk manually.

---

### `load_fbf_network_pathway(path, sheet=None)`
```
Returns: {"status": "ok"|"partial"|"failed", "data": DataFrame|None, "issues": [...]}
```
For Excel, auto-detects the sheet whose normalised name (spaces/apostrophes stripped) contains `p1`, `p2`, and `comb`. Falls back to `sheet_names[0]`. Returns `partial` when P1–P5 columns are not detectable (file loaded but not usable as pathway input).

---

### `load_mh1_tagging(path)`
```
Returns: {"status": "ok"|"failed", "data": DataFrame|None, "issues": [...]}
```
Returns `failed` if MH1 column or any tag column is absent. Tag column is detected by presence of `"tag"` in `_norm_str(column_name)`.

---

### `load_lm_pbh(path)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Returns `partial` if `pincode` column is absent. Hub column is detected heuristically: normalised name contains `"hub"` but not `"type"`, `"state"`, or `"city"`.

---

### `load_fm_pbh(path)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Returns `partial` if `pincode` column is absent. MH column matched by: normalised name contains both `"associated"` and `"mh"`, or both `"central"` and `"hub"`.

---

### `load_fc_map(path)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Returns `partial` (not `failed`) when `mh_code` or `mh_name` are absent. `resolve_sources` and `build_fbf_network_pathway` handle `None` fc_map_df gracefully.

---

### `parse_resort(resort_df)`
```
Returns: {"status": "ok"|"partial"|"failed", "data": DataFrame|None, "issues": [...]}
```
Adds columns: `path_hops` (list), `hop_count`, `last_mh`, `second_last_mh`, `DMH`, `path_terminal`, `lmhub_check_ok`. If `DMH` column already exists in input, keeps it; otherwise derives from `last_mh`. LMHub vs PATH[-1] mismatches and duplicate (MH1, LMHub) keys are reported in `issues` but do not block output (status=`partial`). Returns `failed` only when MH1/LMHub/PATH columns are absent.

---

### `tag_mh1(resort_df, tagging_df=None)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
If `tagging_df=None`, returns `resort_df` unchanged with `status="ok"`. Classification logic:
- MH1 normalised name contains `"centralhub_alite"` → `source_type="ALITE"`, `stream="ALITE"`
- tag == `"fc_mh"` → `source_type="FC_MH"`, `stream="FBF"`
- tag == `"mh"` → `source_type="MH"`, `stream="NFBF"`
- any other tag → `source_type="PH"`, `stream="NFBF"`

MH hubs (real MH-origin NFBF) are **only** those tagged `"mh"` in the tagging file — typically 3–6 hubs (e.g. `CENTRALHUB_STV1`). All other NFBF origin hubs are `PH`. This distinction is critical for Agent 3 cost computation: PH lanes have zero MH1→MH2 first-leg cost; MH lanes are charged.

---

### `join_demand(resort_tagged_df, demand_df=None)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Left-merges SD plan demand keyed on `(MH1_norm, LMHub_norm)`. Adds: `augmedian`, `augpeak`, `newdemandret`, `has_demand_data`, `has_cft_anchor`, and all CFT split columns present in `demand_df`. **Stream/source_type override rule:** For FBF and ALITE matched lanes, overwrites `stream` and `source_type` from SD plan. For NFBF matched lanes, keeps the tagging-file `source_type` (MH or PH) — the SD plan maps NFBF to `source_type="MH"` which must NOT overwrite PH classification from the tagging file. If `demand_df=None`, all demand fields are set to NaN and `status="partial"`.

---

### `build_plan_volume(plan_df, tagging_df=None)`
```
Returns: {"status": "ok", "data": DataFrame, "issues": [...]}
```
Selects and orders final columns. Adds `median_demand_shipments` and `peak_demand_shipments` from `augmedian`/`augpeak`. Includes `source_type`/`stream` columns only when tagging was applied. Includes CFT columns only when any lane has `has_cft_anchor=True`. Filters to rows where `has_demand_data=True`. Intermediate MH columns (MH2, MH3, …) are detected by pattern `MH{n}` and inserted after MH1 in column order.

**`tagging_df` (optional):** When provided, `source_type` for NFBF rows is re-classified using the tagging file lookup, overriding the stream-derived default (`"MH"`). This preserves `"PH"` for hubs that are not in the tagging file. Classification: `fc_mh` tag → `FC_MH`, `mh` tag → `MH`, no tag → `PH`, `alite` in MH1 name → `ALITE`. Without `tagging_df`, behaviour is unchanged (NFBF rows keep whatever `source_type` the upstream function set). Pass `r_tagging["data"]` from `load_mh1_tagging`.

**Why this matters for Agent 3:** `compute_mhmh_cost` checks `source_type` to decide whether to zero the first hop cost. If a PH hub has `source_type="MH"`, Agent 3 charges the PH→MH2 edge from the MH-MH rate card — an entry that may not exist, producing either a wrong cost or a missing-pair warning.

---

### `filter_actuals(fdp_df, exclude_carriers=None)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Default `exclude_carriers=["3PL"]`. Drops rows where `logistics_carrier` (case-insensitive) is in the exclusion list, then drops rows with blank/NaN `fulfill_item_service_profile`. Row counts reported in `issues` as informational. Returns `partial` only when a required column is missing and a filter step was skipped.

---

### `resolve_destinations(fdp_df, lm_pbh_df)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Maps `customer_pincode` (zero-padded 6-digit string) → DH name via LM PBH. When multiple PBH rows match a pincode, prefers rows where hub name contains `"satellite"` or `"bulk"`. Unresolved rows get `destination="UNKNOWN_DH_<pincode>"`. Adds `destination_resolved` boolean column. Returns `partial` when `lm_pbh_df=None`, when hub/pincode columns are absent, or when any pincodes are unresolved.

---

### `resolve_sources(fdp_df, fm_pbh_df, fc_map_df, resort_df)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
All four parameters are optional (pass `None` when unavailable). Classification by `fulfill_item_service_profile`:
- `profile != "FBF"` → `stream="NFBF"`, `source_type="PH"`, source from FM PBH pincode lookup
- `profile == "FBF"` and facility contains `"_al_mcr_"` → `stream="FBF"`, `source_type="MFC"`
- `profile == "FBF"` and facility contains `"_al_"` (but not `"_al_mcr_"`) → `stream="ALITE"`, `source_type="ALITE"`
- `profile == "FBF"` otherwise → `stream="FBF"`, `source_type="FC"`

MFC source resolution uses `resort_df` to derive dominant `last_mh`/`second_last_mh` per LMHub, then applies the MFC city heuristic (see §7). Adds `source_resolved` boolean column. Unresolved tokens: `UNKNOWN_PH_<pincode>`, `UNKNOWN_FC_<facility>`, `UNKNOWN_ALITE_<facility>`, `UNKNOWN_MFC`.

---

### `compute_cft(fdp_df, cft_lookup_df, config=None)`
```
Returns: {"status": "ok"|"partial", "data": DataFrame, "issues": [...]}
```
Joins `cft_lookup_df` on `analytic_vertical` (normalised). Defaults for misses: `default_cft_nfb` (3.5) for NON_FBF rows, `default_cft_fbf` (7.0) for FBF rows. Adds `shipment_cft` and `cft_origin` columns. `cft_origin` values: `"lookup"`, `"default_nfb"`, `"default_fbf"`, `"still_null"`. Returns `partial` when any misses occur.

---

### `aggregate_actuals(fdp_df)`
```
Returns: {"status": "ok", "data": {"granular": DataFrame, "rollup": DataFrame}, "issues": [...]}
```
`data` is a dict with two DataFrames. `granular` groups by `(source, destination, stream, source_type, analytic_vertical)`. `rollup` groups by `(source, destination, stream, source_type)`. Both include `actual_shipments` (count) and `actual_cft` (sum of `shipment_cft`).

---

### `validate_fbf_plan_columns(fbf_day_plan_path, day_start, day_end)`
```
Returns: {"status": "ok"|"partial"|"failed", "data": None, "issues": [...]}
```
Reads only the header row (nrows=0) of the FBF day plan file. Checks that all required columns are present (`source`, `destination_hub`, `destination_pincode`, `seller`, `sc`, `vendor`) and that the day range `day_{day_start}` to `day_{day_end}` is fully covered. Returns `"failed"` if any required column is absent. Returns `"partial"` if the day range has gaps. Returns `"ok"` when all columns and day range are present. Call this before `build_fbf_aggregate` to fail fast.

---

### `validate_sd_plan_columns(alpha_path, alite_path, nfbf_path, day_start, day_end)`
```
Returns: {"status": "ok"|"failed", "data": None, "issues": [...]}
```
Reads only the header row of each of the three SD plan files. Per-stream required columns:
- Alpha: `source`, `destination_hub`, `sc`, `vendor`
- Alite: `source`, `hub` or `destination_hub`, `sc`, `vendor`
- NFBF: `source`, `destination_hub`, `vertical`, `vendor`

All three streams must also have the full day range `day_{day_start}` to `day_{day_end}`. Returns `"failed"` if any required column or day column is missing in any file. Reports all missing items in `issues`. Call this before `build_sd_plan_aggregate` to fail fast without streaming any data.

---

### `build_fbf_aggregate(fbf_day_plan_df=None, top266_df=None, cft_vertical_df=None, config=None, fbf_day_plan_path=None, chunksize=80_000)`
```
Returns: {"status": "ok"|"partial"|"failed", "data": DataFrame|None, "issues": [...]}
```
Two calling modes:

**Path mode (recommended for large files):**
```python
r_agg = build_fbf_aggregate(
    fbf_day_plan_path = fbf_day_plan_path,
    top266_df         = top266_df,
    cft_vertical_df   = r_cft["data"],
)
```
Reads header to detect day columns and `usecols`, then streams the file in 80k-row chunks. Vendor + seller filter applied per chunk, immediate `destination_hub` groupby reduction per chunk, small partials accumulated, final concat+groupby at end. Peak memory = one raw chunk + accumulated partials.

**DataFrame mode (small files or pre-loaded data):**
```python
r_agg = build_fbf_aggregate(
    fbf_day_plan_df = r_plan["data"],
    top266_df       = top266_df,
    cft_vertical_df = r_cft["data"],
)
```

Filters to EKL vendor + FBF seller. Computes avg daily shipments over the configured day window (default `day_1..day_30`). Groups to `destination_hub` level. Output columns: `fbf_avg_daily_shipments_all`, `cft_cuft_day_avg_all`, `fbf_avg_daily_shipments_5sc_core`, `cft_cuft_day_avg_5sc_core`, `fbf_avg_daily_shipments_sha_core`, `cft_cuft_day_avg_sha_core`, plus `_5sc_top16_pin`, `_5sc_next50_pin`, `_5sc_next200_pin`, `_sha_top16_pin`, `_sha_next50_pin`, `_sha_next200_pin` variants (both shipments and cft), plus `source_rows_aggregated`, `rows_missing_cft_vertical`, `rows_sc_outside_core_5sc_sha`. Returns `failed` if no day_N columns found in the configured window.

---

### `build_sd_plan_aggregate(alpha_df=None, alite_df=None, nfbf_df=None, alpha_path=None, alite_path=None, nfbf_path=None, mh_dh_mapping_df=None, cft_vertical_df=None, config=None, chunksize=100_000)`
```
Returns: {"status": "ok"|"partial"|"failed", "data": DataFrame|None, "issues": [...]}
```
Two calling modes — mix and match per stream:

**Path mode (recommended for large files — use this for NFBF):**
```python
r_agg = build_sd_plan_aggregate(
    alpha_path=alpha_path, alite_path=alite_path, nfbf_path=nfbf_path,
    mh_dh_mapping_df=mh_dh_df, cft_vertical_df=r_cft["data"],
)
```
Streams each file in `chunksize` chunks (default 100k rows). EKL vendor filter and groupby reduction applied per chunk — only small aggregated partials accumulate in memory. Peak memory ≈ one raw chunk + accumulated (source × DH) aggregates. Handles the 39 GB NFBF file without OOM.

**DataFrame mode (for pre-loaded data):**
```python
r_agg = build_sd_plan_aggregate(
    alpha_df=alpha_df, alite_df=alite_df, nfbf_df=nfbf_df,
    mh_dh_mapping_df=mh_dh_df, cft_vertical_df=r_cft["data"],
)
```
Processes full DataFrames in memory. Use only for small files or when DataFrames are already loaded for other purposes.

Per-stream logic (both modes): filters EKL vendor, strips `_FURNITURE`/`_LARGE` suffixes from NFBF source names, maps source → MH1 via `mh_dh_mapping_df`, computes augpeak (max across day columns) and augmedian (median). Streams outer-joined on `(MH1, LMHub)`. Stream tag priority: `ALITE` > `FBF` (if fbf ≥ nfbf) > `NFBF`. `newdemandret = augpeak × sd_returns_factor` (default 0.22). Returns `failed` only when all three streams produce zero rows.

---

### `build_fbf_network_pathway(pathway_df, tagging_df=None, fc_map_df=None, mh_dh_map_df=None)`
```
Returns: {"status": "ok"|"partial"|"failed", "data": DataFrame|None, "issues": [...]}
```
Builds one output row per input row. Output columns per P-position (i=1..5): `p{i}_dc_raw`, `p{i}_central_hub`, `p{i}_mapped_via`, `p{i}_pct`. Also: `destination_hub` (if detectable), `source_row` (original index), `pathway_signature` (e.g. `"P1=CENTRALHUB_STV1 | P2=CENTRALHUB_BLR1"`). DC → central hub resolution precedence: MH1 tagging lookup → FC map → MH-DH mapping → passthrough (`mapped_via="as_is"`). Returns `failed` when P1–P5 column detection fails.

---

### `save_dataframe(df, path)`
```
Returns: {"status": "ok"|"failed", "data": {"path": str, "rows": int, "columns": int}|None, "issues": [...]}
```
Creates parent directories as needed. Writes CSV with `index=False`. Caller controls the full path — no timestamped folders are created.

---

### `get_required_columns(function_name)`
```
Returns: dict[str, list[str]]
```
Returns a dict keyed by parameter name, values are lists of required column names. Use for pre-call header inspection. Returns empty dict for unknown function names. Does not return a result-dict — it returns the registry dict directly.

---

## 4. Composing Functions (Pipelines)

### Pipeline A — Plan Volume

Produces `plan_volume.csv`: one row per (MH1 × LMHub) lane with demand, CFT anchor, and source classification.

```python
# Step 1: Load inputs
r_resort    = load_resort(resort_path)
r_tagging   = load_mh1_tagging(tagging_path)      # optional but recommended
r_demand    = build_sd_plan_aggregate(...)         # or load prebuilt plan_demand_sd.csv

# Step 2: Parse resort topology
r_parsed    = parse_resort(r_resort["data"])

# Step 3: Classify each MH1 hub
r_tagged    = tag_mh1(r_parsed["data"], r_tagging["data"])

# Step 4: Merge demand onto lanes
r_joined    = join_demand(r_tagged["data"], r_demand["data"])

# Step 5: Select and filter to output schema
#         Pass tagging_df so NFBF rows not in the tagging file get source_type="PH"
#         instead of the stream-derived default "MH".
r_plan      = build_plan_volume(r_joined["data"], tagging_df=r_tagging["data"])

# Step 6: Write
save_dataframe(r_plan["data"], output_path / "plan_volume.csv")
```

**Notes:**
- `tag_mh1` must run before `join_demand` so that NFBF lane `source_type` (MH vs PH) is set by tagging, not overwritten by SD plan.
- Pass the same `r_tagging["data"]` to both `tag_mh1` and `build_plan_volume`. This ensures `build_plan_volume` can correct any NFBF rows that arrived without a tagging-derived `source_type` (e.g. when `build_sd_plan_aggregate` produced them without tagging context).
- `parse_resort` output (with `last_mh`/`second_last_mh`) should also be passed to `resolve_sources` in Pipeline B.

---

### Pipeline B — Actuals Volume

Produces `actuals_volume.csv` (granular) and `actuals_volume_rollup.csv`.

```python
# Step 1: Load inputs
r_fdp       = load_lm_fdp(fdp_path)
r_lm_pbh    = load_lm_pbh(lm_pbh_path)
r_fm_pbh    = load_fm_pbh(fm_pbh_path)
r_fc_map    = load_fc_map(fc_map_path)
r_cft       = load_cft_vertical(cft_path)
r_resort    = load_resort(resort_path)
r_parsed    = parse_resort(r_resort["data"])       # needed for MFC resolution

# Step 2: Carrier and profile exclusion
r_filtered  = filter_actuals(r_fdp["data"])

# Step 3: Map customer pincodes → DH
r_dest      = resolve_destinations(r_filtered["data"], r_lm_pbh["data"])

# Step 4: Classify source facilities → stream/source_type/hub name
r_src       = resolve_sources(
    r_dest["data"],
    fm_pbh_df  = r_fm_pbh["data"],
    fc_map_df  = r_fc_map["data"],
    resort_df  = r_parsed["data"],
)

# Step 5: Attach CFT per shipment
r_cft_rows  = compute_cft(r_src["data"], r_cft["data"])

# Step 6: Aggregate
r_agg       = aggregate_actuals(r_cft_rows["data"])

# Step 7: Write
save_dataframe(r_agg["data"]["granular"], output_path / "actuals_volume.csv")
save_dataframe(r_agg["data"]["rollup"],   output_path / "actuals_volume_rollup.csv")
```

---

### Pipeline C — FBF DH Aggregate

Produces `fbf_plan_dh_aggregate.csv` (input to Agent 3 speed scoring).

```python
# Step 0: Pre-flight column check (fail fast before streaming)
r_val = validate_fbf_plan_columns(fbf_day_plan_path, day_start=cfg["fbf_plan_day_start"], day_end=cfg["fbf_plan_day_end"])
if r_val["status"] == "failed":
    raise RuntimeError(r_val["issues"])

# Step 1: Load supporting inputs
r_cft       = load_cft_vertical(cft_path)
top266_df   = pd.read_csv(top266_path)            # caller reads directly; pass raw df

# Step 2: Aggregate (path mode — recommended for large FBF day plan files)
r_agg       = build_fbf_aggregate(
    fbf_day_plan_path = fbf_day_plan_path,
    top266_df         = top266_df,
    cft_vertical_df   = r_cft["data"],
    config            = cfg,
)

# Step 3: Write
save_dataframe(r_agg["data"], output_path / "fbf_plan_dh_aggregate.csv")
```

---

### Pipeline D — SD Plan Aggregate

Produces `plan_demand_sd.csv` (demand input to Pipeline A).

**Recommended: path mode (handles 39 GB NFBF without OOM)**
```python
# Step 0: Pre-flight column check (fail fast — reads headers only, no streaming)
r_val = validate_sd_plan_columns(alpha_path, alite_path, nfbf_path,
                                  day_start=cfg["fbf_plan_day_start"], day_end=cfg["fbf_plan_day_end"])
if r_val["status"] == "failed":
    raise RuntimeError(r_val["issues"])

# Step 1: Load supporting inputs only (not the large SD plan files)
r_cft    = load_cft_vertical(cft_path)
mh_dh_df = pd.read_csv(mh_dh_mapping_path)

# Step 2: Pass file paths directly — chunked streaming handled internally
r_agg    = build_sd_plan_aggregate(
    alpha_path       = alpha_path,
    alite_path       = alite_path,
    nfbf_path        = nfbf_path,
    mh_dh_mapping_df = mh_dh_df,
    cft_vertical_df  = r_cft["data"],
    chunksize        = 100_000,      # default; increase to 200_000 if RAM allows
)

# Step 3: Write
save_dataframe(r_agg["data"], output_path / "plan_demand_sd.csv")
```

**Alternative: DataFrame mode (small files only)**
```python
# Use load_sd_plans only when files are small enough to fit in RAM
r_sd     = load_sd_plans(alpha_path, alite_path, nfbf_path)
plans    = r_sd["data"]
r_agg    = build_sd_plan_aggregate(
    alpha_df         = plans["alpha"] if plans["alpha"] is not None else pd.DataFrame(),
    alite_df         = plans["alite"] if plans["alite"] is not None else pd.DataFrame(),
    nfbf_df          = plans["nfbf"]  if plans["nfbf"]  is not None else pd.DataFrame(),
    mh_dh_mapping_df = mh_dh_df,
    cft_vertical_df  = r_cft["data"],
)
save_dataframe(r_agg["data"], output_path / "plan_demand_sd.csv")
```

---

### Pipeline E — FBF Network Pathway

Produces `fbf_network_pathway_wide.csv` (input to Agent 3 D1% speed scoring).

```python
# Step 1: Load inputs
r_pathway   = load_fbf_network_pathway(pathway_path)
r_tagging   = load_mh1_tagging(tagging_path)     # optional
r_fc_map    = load_fc_map(fc_map_path)            # optional
mh_dh_df    = pd.read_csv(mh_dh_mapping_path)    # optional; pass None if unavailable

# Step 2: Build wide table
r_wide      = build_fbf_network_pathway(
    pathway_df    = r_pathway["data"],
    tagging_df    = r_tagging["data"],
    fc_map_df     = r_fc_map["data"],
    mh_dh_map_df  = mh_dh_df,
)

# Step 3: Write
save_dataframe(r_wide["data"], output_path / "fbf_network_pathway_wide.csv")
```

---

## 5. Config Reference

Config file: `backend/agent1_config.json` (loaded automatically at call time; caller can override any key by passing a `config` dict to transformation functions).

| Key | Default | Controls |
|---|---|---|
| `default_cft_nfb` | `3.5` | CFT fallback for NFBF / NON_FBF shipments with no vertical match in `compute_cft` |
| `default_cft_fbf` | `7.0` | CFT fallback for FBF shipments with no vertical match in `compute_cft` |
| `plan_cft_fallback_alpha` | `7.0` | CFT fallback for FBF/Alpha SD plan rows with no SC match in `build_sd_plan_aggregate` |
| `plan_cft_fallback_alite` | `5.0` | CFT fallback for Alite SD plan rows with no SC match in `build_sd_plan_aggregate` |
| `plan_cft_fallback_nfbf` | `3.5` | CFT fallback for NFBF SD plan rows with no vertical match in `build_sd_plan_aggregate` |
| `lm_fdp_exclude_logistics_carriers` | `["3PL"]` | List of carrier strings excluded by `filter_actuals`; matched case-insensitively after strip |
| `fail_on_resort_duplicate_mh1_lmhub` | `false` | Not enforced in agent1.py — kept for compatibility. `parse_resort` always reports duplicates as `issues` rather than failing. |
| `input_error_rate_warn` | `0.2` | Not enforced in agent1.py — kept for compatibility. Callers can inspect issue counts against this threshold. |
| `input_error_rate_fail` | `null` | Not enforced in agent1.py — kept for compatibility. |
| `fbf_plan_vendors` | `["ekl"]` | Vendor allowlist for FBF day plan and SD plan (EKL only); matched lowercase after strip |
| `fbf_plan_day_start` | `1` | First day index for day_N column aggregation in `build_fbf_aggregate` and `build_sd_plan_aggregate` |
| `fbf_plan_day_end` | `30` | Last day index for day_N column aggregation |
| `fbf_plan_avg_divisor` | `30` | Denominator for avg daily shipments = sum(day_1..day_30) / divisor |
| `fbf_plan_missing_cft_fallback_cuft` | `7.0` | CFT fallback applied per-row in `build_fbf_aggregate` when SC has no vertical match |
| `sd_returns_factor` | `0.22` | `newdemandret = augpeak × sd_returns_factor` in `build_sd_plan_aggregate` |
| `core_sc_to_fbf_band` | `{"coreea":"SHA","washingmachinedryer":"5SC","refrigerator":"5SC","homeentertainmentlarge":"5SC","seasonalea":"SHA","premiumea":"SHA","airconditioner":"5SC","microwave":"5SC"}` | Maps normalised supercategory → `"5SC"` or `"SHA"` band for `build_fbf_aggregate` tier breakdown |

---

## 6. Issue Types Reference

Every `result["issues"]` entry has the shape `{"type": str, "detail": str}`.

| `type` | What it means | How to respond |
|---|---|---|
| `read_error` | File could not be opened (path wrong, encoding error, unsupported format, corrupt file) | Abort this pipeline branch. Re-check the file path provided by the caller. Log the `detail` string. |
| `missing_columns` | One or more expected columns were not found in the file or DataFrame | For loading functions: the file may have renamed/reformatted columns. Inspect actual column names. For transformation functions: a sub-step was skipped; check whether output is still usable for downstream steps. |
| `data_quality` | `parse_resort`: PATH[-1] does not match LMHub for some rows | Log count. These rows will have `lmhub_check_ok=False`. They are retained in output. Review resort file for stale topology entries. |
| `duplicate_lanes` | `parse_resort`: duplicate (MH1, LMHub) pairs in resort | Log count. Both rows are retained. Downstream aggregation may double-count unless deduplicated by caller. |
| `missing_input` | Optional input DataFrame was `None`; a sub-step was skipped and relevant output fields are NaN | Decide whether NaN demand/destination/source fields are acceptable for the downstream consumer. Provide the missing input and re-run if not. |
| `unmatched_lanes` | `join_demand`: resort lanes with no matching (MH1, LMHub) in demand_df | Check whether demand file covers all MH1 × LMHub combinations. Lanes without demand will have `has_demand_data=False` and will be filtered out by `build_plan_volume`. |
| `unresolved_destinations` | `resolve_destinations`: customer pincodes not found in LM PBH | `destination` is `UNKNOWN_DH_<pincode>`. Check if LM PBH file has coverage for these pincodes. These rows will appear as UNKNOWN in actuals output. |
| `unresolved_sources` | `resolve_sources` or `build_sd_plan_aggregate`: source facility/pincode not resolved to a hub name | `source` is `UNKNOWN_PH_*` / `UNKNOWN_FC_*` / `UNKNOWN_ALITE_*` / `UNKNOWN_MFC`. For NFBF: check FM PBH pincode coverage. For FBF/MFC/ALITE: check FC map. In SD plan: rows dropped entirely (no MH1 mapping). |
| `cft_miss` | `compute_cft`: rows had no vertical match in CFT lookup | Default CFT applied (`cft_origin` = `"default_nfb"` or `"default_fbf"`). Acceptable operationally. If count is very high (> 20% of rows), the CFT vertical file may be stale or the `analytic_vertical` column has unexpected values. |
| `cft_null` | `compute_cft`: rows still have null CFT after defaults (unexpected service profile value) | Rows have `cft_origin="still_null"`. Investigate what value `fulfill_item_service_profile` has for these rows. |
| `rows_excluded` | `filter_actuals`: rows dropped due to carrier exclusion or blank service profile | Informational. No action required unless count is unexpectedly high (> 50% of input). |
| `empty_lookup` | `build_sd_plan_aggregate`: MH-DH mapping produced zero keys | All SD plan source→MH1 resolutions will fail. This almost always means the wrong mapping file was provided or column headers do not match `dc_ph`/`mh_1` pattern and positional fallback also failed. |
| `empty_result` | A processing step produced zero rows after filtering | No output DataFrame is returned. Check whether vendor/seller filter is too restrictive or input file has no data rows. |
| `unmapped_dc` | `build_fbf_network_pathway`: DC/FC values passed through as-is (no hub mapping found) | `p{i}_mapped_via="as_is"` for these rows. Provide tagging_df, fc_map_df, or mh_dh_map_df to improve resolution. |
| `write_error` | `save_dataframe`: could not write file (permission error, disk full, invalid path) | Abort write step. Check output path and disk space. |

---

## 7. Usage Warnings

### W1 — Large file memory: `build_fbf_aggregate` and `build_sd_plan_aggregate`

Both functions support path mode — pass `fbf_day_plan_path=` or `alpha_path=`/`alite_path=`/`nfbf_path=` instead of loading DataFrames first. Path mode streams in chunks (80k rows for FBF, 100k for SD plan), applies vendor/seller filter per chunk, and reduces to small hub-level aggregates per chunk. Peak memory ≈ one raw chunk + accumulated aggregates — the 42 GB NFBF file and the FBF day plan file are fully handled without OOM.

**Use DataFrame mode only when files are already loaded for another purpose and are small enough to fit in RAM.** Do not use `load_sd_plans` followed by DataFrame mode for large files — that loads the full file into RAM before `build_sd_plan_aggregate` even starts.

### W2 — `top266_df` column name matching in `build_fbf_aggregate`

The column containing pincode tier labels is detected by: `"final"` in `_norm_str(column_name)` AND `"mapping"` in `_norm_str(column_name)`. This matches `"Final Mapping"`, `"Final_Mapping"`, `"final mapping"`. It does **not** match columns named `"Tier"`, `"Pin Tier"`, `"Classification"`, or similar. If the top266 file uses a different column name, rename it to `"Final Mapping"` before passing the DataFrame, or pass `top266_df` with columns already renamed. If the column is not found, `build_fbf_aggregate` continues without tier breakdown (all tier-split columns will be zero) and reports `missing_columns` in issues.

### W3 — `load_sd_plans` returns a dict, not a DataFrame

`result["data"]` from `load_sd_plans` is `{"alpha": df, "alite": df, "nfbf": df}`. Passing `result["data"]` directly to any function expecting a DataFrame will fail. Always unpack first:
```python
plans = load_sd_plans(a, b, c)["data"]
alpha_df = plans["alpha"] if plans["alpha"] is not None else pd.DataFrame()
```
A stream value of `None` means that file failed to load. Pass `pd.DataFrame()` (empty, not `None`) to `build_sd_plan_aggregate` for failed streams — the function handles empty DataFrames by treating that stream as contributing zero demand, rather than raising an error.

### W5 — SD plan and FBF day plan day-window selection (mandatory per run)

The SD plan files (Alpha/Alite/NFBF) and FBF day plan file contain day-numbered columns spanning multiple months (e.g. day_1 to day_91 = 3 months of data). Agent 1 selects only the columns between `fbf_plan_day_start` and `fbf_plan_day_end` from config and averages them using `fbf_plan_avg_divisor` as the denominator. Default is day_1 to day_30 (first month).

This window must be set explicitly at the start of every run — the default is rarely correct. Claude must ask the user which 30-day window to use **before calling any of the following**: `validate_fbf_plan_columns`, `validate_sd_plan_columns`, `build_fbf_aggregate`, `build_sd_plan_aggregate`. The validate functions need `day_start`/`day_end` to check column presence — they must be called with the correct window, not the default. Set the window by overriding config before calling:
```python
cfg["fbf_plan_day_start"] = 31   # example: second month
cfg["fbf_plan_day_end"] = 60
cfg["fbf_plan_avg_divisor"] = 30
```

If the column naming convention is not `day_N` (e.g. date strings or `D1`/`D2`), the function returns `status="failed"` with `empty_result`. Fix by renaming columns to `day_1`...`day_91` before passing.

---

### W6 — Always use load_agent1_config(), never raw json.load()

Agent 1 config must be loaded via `load_agent1_config()`, not `json.load()` directly. The function now exists as a public wrapper in `agent1.py`. It merges `agent1_config.json` over built-in defaults — missing keys get correct defaults automatically. Raw `json.load()` returns only what is in the file; any key absent from the JSON (including the 3 legacy compatibility keys) will raise a KeyError when accessed downstream.

```python
import agent1 as a1
cfg = a1.load_agent1_config()          # loads defaults + agent1_config.json
cfg["fbf_plan_day_start"] = 31         # override for this run
```

---

### W4 — MFC city heuristic coverage in `resolve_sources`

The `_mfc_city_source` heuristic covers exactly two city patterns:
- **FRN**: `last_mh` or `second_last_mh` contains `"frn"` AND one contains `"ykb"` or `"kal"` → returns `"FRN"`
- **BAG**: contains `"bag"` AND contains `"kol5"` → returns `"BAG"`

All other MFC facilities fall back to `last_mh` as-is. If `last_mh` is null (resort not provided or resort has no match for that LMHub), the source becomes `"UNKNOWN_MFC"`. The heuristic was ported exactly from `agent1_pipeline.py` and is hardcoded — it does not read from config. Any new MFC city pattern requires a code change to `_mfc_city_source`.
