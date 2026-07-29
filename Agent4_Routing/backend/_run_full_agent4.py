"""Full-network Agent 4 — freeze-day + dock scheduling, single output folder."""
import io
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = Path(__file__).resolve().parents[2]
INP = ROOT / "Inputs"
BACK = ROOT / "Agent4_Routing" / "backend"
A3 = ROOT / "Agent3_Clustering" / "output" / "run_20260727"
DAYWISE = ROOT / "Agent1_DataPrep" / "output" / "run_freeze_day_test_20260722" / "dh_daywise_volume.csv"

sys.path.insert(0, str(ROOT / "Agent3_Clustering" / "backend"))
sys.path.insert(0, str(BACK))

import pandas as pd
import agent3 as a3
import agent4 as a4
import agent4_freeze_day as fd
import agent4_dock_scheduling as ds

stamp = datetime.now().strftime("%d%m%y_%H%M")
out_dir = ROOT / "Agent4_Routing" / "output" / f"Agent_4_{stamp}_FULL"
out_dir.mkdir(parents=True, exist_ok=True)
_log_path = out_dir / "run_log.txt"
_log_fh = open(_log_path, "a", encoding="utf-8")


class _Tee(io.TextIOWrapper):
    """ponytail: minimal tee without replacing sys.stdout type."""

_orig_write = sys.stdout.write

def _tee_write(s):
    _orig_write(s)
    _log_fh.write(s)
    _log_fh.flush()

sys.stdout.write = _tee_write

t0 = time.time()
print(f"=== Agent 4 full network run started {datetime.now().isoformat()} ===")
print(f"Output: {out_dir}")

try:
    cfg = a4.load_agent4_config(BACK / "agent4_config.json")
    assign = pd.read_csv(A3 / "dh_fc_mh_assignment_final.csv", low_memory=False)
    daywise = pd.read_csv(DAYWISE)
    h2h = pd.read_csv(INP / "Consolidated H2H June'26 Network - June'26 H2H.csv", low_memory=False)
    feas = pd.read_csv(INP / "DH Feasibility.csv", low_memory=False)
    dist_df = pd.read_csv(INP / "Distance Matrix.csv", dtype=str)
    dist_df["distance"] = pd.to_numeric(dist_df["distance"], errors="coerce")
    lat_df = pd.read_excel(INP / "Lat Longs.xlsx", engine="openpyxl")
    load_df = pd.read_csv(INP / "Load Profile.csv")
    mhdh_df = pd.read_excel(INP / "MHDH_RateCard.xlsx", engine="openpyxl")
    mh_configs = a4.load_rate_card(INP / "MHDH_RateCard.xlsx", cfg)

    loc_res = fd.build_freeze_day_location_file(assign, feas, h2h, daywise, mh_configs, cfg)
    loc_df = loc_res["data"].dropna(subset=["ML"]).copy()
    pf = a4.preflight_check(loc_df, dist_df, mhdh_df, cfg)
    print(f"build_freeze_day_location_file: {loc_res['status']} ({len(loc_res.get('issues', []))} issues)")
    print(f"preflight_check: {pf['status']} ({len(pf.get('issues', []))} issues)")
    for issue in pf.get("issues", []):
        print(f"  [{issue.get('type')}] {issue.get('detail')}")
    if pf["status"] == "failed":
        print("NOTE: proceeding per user — OSRM covers missing distance matrix gaps.")

    dist_dict = a4.build_distance_dict(dist_df)["data"]
    latlong = a4.build_latlong_dict(lat_df)["data"]
    load_interp = a3.build_load_profile_interp(load_df)["data"]

    pipeline_res = fd.run_agent4_freeze_day_pipeline(
        loc_df, dist_dict, latlong, mh_configs, out_dir, cfg,
        on_progress=lambda msg: print(msg, flush=True),
    )
    print(f"freeze-day pipeline: {pipeline_res['status']}")
    if pipeline_res.get("issues"):
        for issue in pipeline_res["issues"][:20]:
            print(f"  pipeline issue: {issue}")
        if len(pipeline_res["issues"]) > 20:
            print(f"  ... +{len(pipeline_res['issues']) - 20} more")

    fd.write_route_visualizer(pipeline_res["data"]["per_mh_results"], latlong, out_dir, cfg)

    dock_res = ds.run_dock_scheduling_for_all_mhs(
        pipeline_res["data"]["per_mh_results"],
        loc_df, dist_dict, latlong, mh_configs, load_interp, cfg, out_dir,
        h2h_df=h2h,
    )
    print(f"dock scheduling: {dock_res['status']}")
    if dock_res.get("issues"):
        for issue in dock_res["issues"][:30]:
            print(f"  dock issue: {issue}")
        if len(dock_res["issues"]) > 30:
            print(f"  ... +{len(dock_res['issues']) - 30} more")

    ns = pd.read_csv(out_dir / "Network_Summary.csv")
    print("\n=== Network_Summary ===")
    print(ns.to_string(index=False))
    if (out_dir / "Speed_Summary.csv").exists():
        ss = pd.read_csv(out_dir / "Speed_Summary.csv")
        print("\n=== Speed_Summary (aggregate) ===")
        print(f"MHs with speed: {ss['mh_speed_pct'].notna().sum()}/{len(ss)}")
        print(f"Mean proposal speed%: {ss['mh_speed_pct'].mean():.1f}")
        print(f"Mean baseline speed%: {ss['baseline_mh_speed_pct'].mean():.1f}")

    elapsed = round(time.time() - t0)
    print(f"\nDONE elapsed_s={elapsed} status=pipeline:{pipeline_res['status']} dock:{dock_res['status']}")
    print(f"Output folder: {out_dir}")

except Exception:
    traceback.print_exc()
    print(f"FAILED elapsed_s={round(time.time() - t0)}")
    raise
finally:
    _log_fh.close()
