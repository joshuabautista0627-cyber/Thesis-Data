"""Predeclared current-frame-only comparison using the frozen tuning cache.

Separates richer features/hyperparameters from gains that require signal history.
Run after the main outer evaluation and final refit. Does not alter that search.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from scripts.tune_manual_models import (
    DEFAULT_OUT, TASKS, Search, candidate_grid, load_rich_data,
    fit_candidate, fast_metrics, session_weights, write_json,
)
from scripts.live_sensor_common import sha256_file


def choose_frame_only(trials,lookup,task):
    rows=trials[(trials.h==0)&trials.id.map(lambda cid:lookup[cid]["features"]!="temporal")]
    if task=="contact":rows=rows[rows.threshold_policy=="balanced"]
    metric="mae_N" if task=="force" else "macro_f1" if task=="localization" else "balanced_accuracy"
    chosen=rows.sort_values(metric,ascending=task=="force",kind="stable").iloc[0].dropna().to_dict()
    chosen["h"]=int(chosen["h"])
    return chosen


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,default=DEFAULT_OUT)
    parser.add_argument("--plan-only",action="store_true")
    args=parser.parse_args();out=args.run;out.mkdir(parents=True,exist_ok=True)
    specpath=out/"frame_only_protocol.json"
    spec=dict(created_at=datetime.now(timezone.utc).isoformat(),purpose="Separate improvements from current optical features and tuning from history-dependent gains",
        selection="Same inner grouped trials; exclude temporal feature pack, require output tau=0; minimum MAE / maximum ROI macro F1 / maximum contact balanced accuracy",
        ensembles=False,outer_evaluation="Same six held-out groups and frame set as original and main tuning run",
        source_code_sha256=sha256_file(Path(__file__)))
    if not specpath.exists():write_json(specpath,spec)
    else:assert json.loads(specpath.read_text())["source_code_sha256"]==spec["source_code_sha256"]
    if args.plan_only:
        print("Recorded frame-only evaluation plan before held-out tuning results")
        return
    frames,_=load_rich_data();configs=candidate_grid();experiment=Search(frames,out,configs,8)
    results=[];selections=[]
    for fold in range(1,7):
        choices={task:choose_frame_only(pd.read_csv(out/f"inner_trials_fold{fold}_{task}.csv"),experiment.lookup,task) for task in TASKS}
        write_json(out/f"frame_only_choices_fold{fold}.json",choices)
        data,_,_=experiment.prepared(set(range(1,7))-{fold})
        for task,choice in choices.items():
            r=fit_candidate(experiment.lookup[choice["id"]],*data[task])
            predictions=experiment.apply_choice(r["values"],choice,task)
            rows=data[task][1]["rows"].assign(task=task,model="Tuned frame-only",outer_fold=fold,prediction=predictions)
            if task=="contact":rows["score"]=experiment.apply_choice(r["values"],choice,task,raw=True)
            results.append(rows);selections.append(dict(outer_fold=fold,task=task,**choice))
        print(f"Frame-only outer fold {fold}/6 completed",flush=True)
    extra=pd.concat(results,ignore_index=True);extra.to_parquet(out/"frame_only_outer.parquet",index=False)
    pred=pd.read_parquet(out/"outer_predictions.parquet")
    pred=pd.concat([pred[pred.model!="Tuned frame-only"],extra],ignore_index=True)
    pred.to_parquet(out/"outer_predictions.parquet",index=False)
    comp=[];folds=[]
    for (task,model),r in pred.groupby(["task","model"]):
        comp.append(dict(task=task,model=model,frames=len(r),sessions=r.session_id.nunique(),**fast_metrics(r.truth.to_numpy(),r.prediction.to_numpy(),session_weights(r),task)))
        for fold,g in r.groupby("outer_fold"):
            folds.append(dict(task=task,model=model,outer_fold=fold,frames=len(g),sessions=g.session_id.nunique(),**fast_metrics(g.truth.to_numpy(),g.prediction.to_numpy(),session_weights(g),task)))
    pd.DataFrame(comp).to_csv(out/"model_comparison.csv",index=False)
    pd.DataFrame(folds).to_csv(out/"fold_metrics.csv",index=False)
    write_json(out/"frame_only_selections.json",selections)
    data,floors,lag=experiment.prepared(range(1,7),final=True)
    modeldir=out/"models_frame_only";modeldir.mkdir(exist_ok=True)
    for task in TASKS:
        choice=choose_frame_only(pd.read_csv(out/f"final_training_trials_{task}.csv"),experiment.lookup,task)
        path=modeldir/f"{task}_{choice['id']}.joblib"
        fit_candidate(experiment.lookup[choice["id"]],*data[task],save_path=path)
        write_json(modeldir/f"{task}.json",dict(task=task,choice=choice,
            members=[dict(file=path.name,choice=choice,sha256=sha256_file(path))],
            floors=floors,lag_ms=lag,status="experimental_offline_only",training_sessions=sorted(frames.session_id.unique())))
    write_json(modeldir/"manifest.json",dict(status="experimental_offline_only",models=list(TASKS),
        files={p.name:sha256_file(p) for p in modeldir.iterdir() if p.name!="manifest.json"},
        history="No input/output history used by selected models",warning="Trusted local research artifacts only"))
    print(pd.DataFrame(comp).query("model=='Tuned frame-only'").to_string(index=False))


if __name__=="__main__":main()
