"""Finish dependent outer fits as soon as their inner caches are ready.

This one-run coordinator may run beside the main --stage inner process. It
never changes the frozen candidates or uses outer outcomes for inner selection.
"""
import argparse
from itertools import combinations
import json
from pathlib import Path
import sys
import time
import pandas as pd
from scripts.tune_manual_models import DEFAULT_OUT, Search, candidate_grid, load_rich_data, summarize, write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,default=DEFAULT_OUT)
    parser.add_argument("--jobs",type=int,default=4)
    args=parser.parse_args();out=args.run
    frames,_=load_rich_data();experiment=Search(frames,out,candidate_grid(),args.jobs)
    deadline=time.monotonic()+7200
    remaining=set(range(1,7))
    while remaining:
        progress=False
        for fold in sorted(remaining,reverse=True):
            if (out/f"outer_fold{fold}.parquet").exists():
                remaining.remove(fold);progress=True;continue
            training=set(range(1,7))-{fold}
            required=[out/"inner_cache"/("train_"+"".join(map(str,c)))/"complete.json" for c in combinations(sorted(training),4)]
            if all(path.exists() for path in required):
                print(f"Inner cache ready for outer fold {fold}; evaluating sealed choices",flush=True)
                experiment.outer(fold)
                pd.DataFrame(experiment.timings).to_csv(out/"fit_timings_outer.csv",index=False)
                remaining.remove(fold);progress=True
        if remaining and not progress:
            if time.monotonic()>deadline:raise TimeoutError("Inner training did not complete within coordinator limit")
            time.sleep(10)
    summarize(out)
    print("Refitting final training-selected models",flush=True)
    experiment.timings=[]
    experiment.final_refit()
    pd.DataFrame(experiment.timings).to_csv(out/"fit_timings_refit.csv",index=False)
    print("Evaluating the predeclared current-frame-only policy",flush=True)
    sys.argv=["evaluate_manual_frame_only","--run",str(out)]
    from scripts.evaluate_manual_frame_only import main as frame_main
    frame_main()
    print("Running independent metric and saved-inference audit",flush=True)
    sys.argv=["audit_tuned_manual","--run",str(out)]
    from scripts.audit_tuned_manual import main as audit_main
    audit_main()
    print("Building the before/after report",flush=True)
    sys.argv=["report_tuned_manual","--run",str(out)]
    from scripts.report_tuned_manual import main as report_main
    report_main()
    write_json(out/"completion_all.json",dict(status="complete",source="finish_manual_tuning coordinator",outer_folds=6,audit="passed"))
    print("All training, comparisons, final refits, and audits complete",flush=True)


if __name__=="__main__":main()
