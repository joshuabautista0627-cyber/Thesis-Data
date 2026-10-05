"""Nested, whole-session manual-only model/feature/threshold optimization.

Run with .venv/Scripts/python.exe -m scripts.tune_manual_models --jobs 8.
Uses the previous experiment's fold-local lag decisions to keep its evaluation
rows and targets identical. All candidates and policies are frozen before fits.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from itertools import combinations
import json
import os
from pathlib import Path
import time
import warnings

for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_var] = "1"

import joblib
import numpy as np
import pandas as pd
from scipy.special import expit, softmax
from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor, RandomForestClassifier, RandomForestRegressor, HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, roc_auc_score
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC, SVR
from threadpoolctl import threadpool_limits
from xgboost import XGBClassifier, XGBRegressor

from scripts.retrain_manual_models import (
    load_data, write_json, session_weights, _balanced_weights, _fit_floors,
    pair_lagged_force, AREA_COLUMNS, RAW_LIGHT_COLUMNS, SIGNED, ACTIVE_COLUMNS,
)
from scripts.live_sensor_common import PROJECT_ROOT, sha256_file

SEED = 1741
TASKS = ("force", "localization", "contact")
HISTORY = (0.0, 0.3, 1.0)
BASE_RUN = PROJECT_ROOT / "analysis_outputs/live_sensor_manual_only/model_runs/retraining_20260909"
DEFAULT_OUT = BASE_RUN.parent / "tuning_20260909"
EXTRA_SUFFIXES = ("signed_delta_v_median", "signed_delta_v_mad", "centroid_x", "centroid_y", "raw_v_median", "raw_v_max", "x", "y", "width", "height")
EXTRA_COLUMNS = [f"roi{r}_{s}" for s in EXTRA_SUFFIXES for r in range(1,10)]


def load_rich_data():
    frames, provenance = load_data()
    parts = []
    for path in sorted((Path(provenance["feature_directory"]) / "sessions/archive_id=manual").glob("*.parquet")):
        parts.append(pd.read_parquet(path, columns=["frame_uid"] + EXTRA_COLUMNS))
    frames = frames.merge(pd.concat(parts, ignore_index=True), on="frame_uid", validate="one_to_one")
    frames = frames.sort_values(["session_id", "capture_monotonic_relative_s", "video_frame_index"]).reset_index(drop=True)
    return frames, provenance


def causal_ema(values, session_ids, times, tau):
    """Only current/past rows; restart at session boundary or >0.3 s gap."""
    if tau == 0:
        return np.asarray(values).copy()
    values = np.asarray(values)
    result = np.empty_like(values, dtype=float)
    for i in range(len(values)):
        dt = times[i] - times[i-1] if i else 0
        if i == 0 or session_ids[i] != session_ids[i-1] or dt > 0.3 or dt <= 0:
            result[i] = values[i]
        else:
            a = -np.expm1(-dt / tau)
            result[i] = result[i-1] + a * (values[i] - result[i-1])
    return result


def optical_features(frames, floors):
    """Return fixed optical feature packs; labels/force/absolute time are absent."""
    def cols(s): return frames[[f"roi{r}_{s}" for r in range(1,10)]].to_numpy(float)
    area = frames[AREA_COLUMNS].to_numpy(float)
    if not (np.isfinite(area).all() and (area > 0).all()):
        raise ValueError("Invalid ROI areas")
    positive = np.maximum(frames[RAW_LIGHT_COLUMNS].to_numpy(float) - floors, 0) / area
    signed = frames[SIGNED].to_numpy(float) / area
    active = frames[ACTIVE_COLUMNS].to_numpy(float)
    optical = np.column_stack([positive, signed, active])
    if not np.isfinite(optical).all(): raise ValueError("Nonfinite basic optical features")
    def unit(x): return x / (np.sum(np.abs(x), axis=1, keepdims=True) + 1e-6)
    def contrast(x): return x - np.median(x, axis=1, keepdims=True)
    med, mad = cols("signed_delta_v_median"), cols("signed_delta_v_mad")
    raw = cols("raw_v_mean")
    rawmed, rawmax = cols("raw_v_median"), cols("raw_v_max")
    cx = (cols("centroid_x") - cols("x")) / np.maximum(cols("width"), 1)
    cy = (cols("centroid_y") - cols("y")) / np.maximum(cols("height"), 1)
    present = (np.isfinite(cx) & np.isfinite(cy)).astype(float)
    cx, cy = np.nan_to_num(cx, nan=0.5), np.nan_to_num(cy, nan=0.5)
    shape = np.column_stack([unit(positive), unit(contrast(signed)), unit(active),
        contrast(signed), contrast(active), unit(contrast(raw)), unit(contrast(med))])
    spatial = np.column_stack([optical, shape, med, mad, cx, cy, present,
        contrast(raw), contrast(rawmed), rawmax / (raw + 1),
        np.log1p(positive), np.sign(signed) * np.log1p(np.abs(signed))])
    invariant = np.column_stack([shape, unit(mad), cx, cy, present,
        unit(contrast(rawmed)), rawmax / (raw + 1)])
    ids = frames.session_id.to_numpy()
    times = frames.capture_monotonic_relative_s.to_numpy(float)
    fast = causal_ema(optical, ids, times, .3)
    slow = causal_ema(optical, ids, times, 1.0)
    temporal = np.column_stack([spatial, fast, slow, optical-fast, fast-slow])
    packs = dict(positive=positive, optical=optical,
        shape=np.column_stack([optical, shape]), spatial=spatial,
        invariant=invariant, temporal=temporal)
    for name, x in packs.items():
        if not np.isfinite(x).all(): raise ValueError(f"Nonfinite {name} features")
    return packs


def candidate_grid():
    configs = []
    def add(task, family, features, params, weights="session"):
        spec = dict(task=task, family=family, features=features, params=params, weights=weights)
        spec["id"] = f"{task[:3]}_{len([c for c in configs if c['task']==task]):03d}"
        configs.append(spec)
    add("force", "Extra Trees", "positive", dict(min_samples_leaf=10, max_features=1.0), "bins")
    add("force", "Extra Trees", "positive", dict(min_samples_leaf=10, max_features=1.0))
    for pack in ("optical", "shape", "spatial", "invariant", "temporal"):
        for leaf, mf in ((3,.75),(15,.75),(30,1.0)):
            add("force", "Extra Trees", pack, dict(min_samples_leaf=leaf,max_features=mf))
        for leaf in (5,20):
            add("force", "Random Forest", pack, dict(min_samples_leaf=leaf,max_features=.75))
        for leaf, loss in ((15,"squared_error"),(31,"squared_error"),(15,"absolute_error")):
            add("force", "HistGB", pack, dict(max_leaf_nodes=leaf,loss=loss,l2_regularization=10))
        for depth in (3,5):
            add("force", "XGBoost", pack, dict(max_depth=depth,reg_lambda=20))
        if pack != "temporal":
            for c in (2,20): add("force","RBF SVR",pack,dict(C=c,epsilon=.05,gamma="scale"))
    for pack in ("optical", "spatial"):
        add("force","MLP",pack,dict(hidden_layer_sizes=(64,32),alpha=1.0))
    for pack in ("optical","shape","spatial","invariant","temporal"):
        for c in (.03,.3,3,30): add("localization","Logistic regression",pack,dict(C=c))
        for c in (1,10,100):
            for g in (.1,1.0): add("localization","RBF SVC",pack,dict(C=c,gamma_multiplier=g))
        for leaf in (2,10): add("localization","Extra Trees",pack,dict(min_samples_leaf=leaf,max_features=.75))
    for pack in ("spatial","invariant"):
        add("localization","HistGB",pack,dict(max_leaf_nodes=15,l2_regularization=10))
        add("localization","MLP",pack,dict(hidden_layer_sizes=(64,32),alpha=1.0))
    for pack in ("optical","shape","spatial","temporal"):
        for leaf in (2,10,30): add("contact","Random Forest",pack,dict(min_samples_leaf=leaf,max_features=.75))
        for leaf in (3,15): add("contact","Extra Trees",pack,dict(min_samples_leaf=leaf,max_features=.75))
        for leaf in (7,15): add("contact","HistGB",pack,dict(max_leaf_nodes=leaf,l2_regularization=10))
        for c in (.1,10): add("contact","Logistic regression",pack,dict(C=c))
        for c in (1,10): add("contact","RBF SVC",pack,dict(C=c,gamma_multiplier=1.0))
        for depth in (2,4): add("contact","XGBoost",pack,dict(max_depth=depth,reg_lambda=20))
    return configs


def estimator(config, n_features):
    task, family, p = config["task"], config["family"], dict(config["params"])
    regression = task == "force"
    if family in ("Extra Trees", "Random Forest"):
        cls = {("Extra Trees",True):ExtraTreesRegressor,("Extra Trees",False):ExtraTreesClassifier,
            ("Random Forest",True):RandomForestRegressor,("Random Forest",False):RandomForestClassifier}[family,regression]
        return cls(n_estimators=180,n_jobs=1,random_state=SEED,**p)
    if family == "HistGB":
        cls = HistGradientBoostingRegressor if regression else HistGradientBoostingClassifier
        return cls(max_iter=180,learning_rate=.05,min_samples_leaf=20,early_stopping=False,random_state=SEED,**p)
    if family == "XGBoost":
        cls = XGBRegressor if regression else XGBClassifier
        return cls(n_estimators=220,learning_rate=.04,min_child_weight=5,tree_method="hist",n_jobs=1,random_state=SEED,**p)
    if family == "Logistic regression": return LogisticRegression(max_iter=2500,random_state=SEED,**p)
    if family == "RBF SVR": return SVR(cache_size=256,**p)
    if family == "RBF SVC":
        p["gamma"] = p.pop("gamma_multiplier") / n_features
        return SVC(cache_size=256,decision_function_shape="ovr",random_state=SEED,**p)
    if family == "MLP":
        cls = MLPRegressor if regression else MLPClassifier
        return cls(max_iter=350,early_stopping=False,learning_rate_init=.001,random_state=SEED,**p)
    raise ValueError(family)


def scores(model, x, task, family):
    if task == "force": return np.maximum(model.predict(x),0)
    if family == "RBF SVC":
        d = model.decision_function(x)
        return expit(d) if task == "contact" else softmax(d,axis=1)
    p = model.predict_proba(x)
    return p[:,1] if task == "contact" else p


def task_data(frame, force, packs, task):
    press = frame.scientific_role.eq("model_primary").to_numpy()
    eligible = press & np.isfinite(force) & (force >= .05) & (force <= 3)
    mask = eligible | frame.scientific_role.eq("no_contact").to_numpy() if task == "contact" else eligible
    rows = frame.loc[mask,["frame_uid","session_id","group","scientific_role","target_roi","capture_monotonic_relative_s"]].copy()
    y = force[mask] if task == "force" else (rows.target_roi.to_numpy(int) if task == "localization" else press[mask].astype(int))
    rows["truth"], rows["reference_force_N"] = y,force[mask]
    weights = session_weights(rows,task=="contact")
    return dict(rows=rows,mask=mask,y=y,weights=weights,features=packs,
        session=frame.session_id.to_numpy(),times=frame.capture_monotonic_relative_s.to_numpy(float))


def fit_candidate(config, train, val, save_path=None):
    start = time.perf_counter()
    task, family = config["task"],config["family"]
    x0=train["features"][config["features"]][train["mask"]]
    xv=val["features"][config["features"]]
    w = _balanced_weights(train["rows"].session_id.to_numpy(),train["y"]) if config["weights"]=="bins" else train["weights"]
    scaler=StandardScaler(with_mean=not (task=="force" and config["features"]=="positive")).fit(x0,sample_weight=w)
    x,xv=scaler.transform(x0),scaler.transform(xv)
    model=estimator(config,x.shape[1])
    with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        model.fit(x,train["y"],sample_weight=w)
        full=scores(model,xv,task,family)
    variants=np.stack([causal_ema(full,val["session"],val["times"],tau)[val["mask"]] for tau in HISTORY])
    if not np.isfinite(variants).all(): raise ValueError(f"Nonfinite predictions: {config['id']}")
    if save_path is not None:
        joblib.dump(dict(config=config,model=model,scaler=scaler),save_path,compress=3)
    return dict(id=config["id"],values=variants.astype(np.float32),seconds=time.perf_counter()-start,
        warnings=sorted(set(str(w.message) for w in caught)))


def fast_metrics(y,p,w,task):
    if task == "force":
        e=p-y; mse=np.average(e*e,weights=w)
        return dict(mae_N=np.average(abs(e),weights=w),rmse_N=np.sqrt(mse),bias_N=np.average(e,weights=w),
            r2=1-mse/np.average((y-np.average(y,weights=w))**2,weights=w))
    labels=[0,1] if task=="contact" else list(range(1,10))
    cm=confusion_matrix(y,p,labels=labels,sample_weight=w)
    tp=np.diag(cm); denom=cm.sum(axis=0)+cm.sum(axis=1)
    recalls=np.divide(tp,cm.sum(axis=1),out=np.zeros_like(tp),where=cm.sum(axis=1)>0)
    m=dict(accuracy=tp.sum()/cm.sum(),balanced_accuracy=recalls.mean(),
        macro_f1=np.divide(2*tp,denom,out=np.zeros_like(tp),where=denom>0).mean())
    if task=="contact":m.update(recall=recalls[1],fpr=1-recalls[0],precision=tp[1]/max(cm[:,1].sum(),1e-12))
    return m


def weighted_quantile(v,w,q):
    order=np.argsort(v); v,w=v[order],w[order]
    return float(np.interp(q*w.sum(),np.cumsum(w)-.5*w,v))


def threshold_options(y,s,w):
    """Exact weighted ROC operating points; threshold tuning uses inner OOF only."""
    order=np.argsort(s,kind="stable")[::-1]; ss=s[order]; yy=y[order]; ww=w[order]
    ends=np.r_[np.where(np.diff(ss)!=0)[0],len(ss)-1]
    recall=np.r_[0,np.cumsum(ww*(yy==1))[ends]/ww[yy==1].sum()]
    fpr=np.r_[0,np.cumsum(ww*(yy==0))[ends]/ww[yy==0].sum()]
    thresholds=np.r_[np.nextafter(ss[0],np.inf),ss[ends]]
    bacc=(recall+1-fpr)/2
    best=int(np.argmax(bacc))
    feasible=np.where(fpr<=.05+1e-12)[0]
    constrained=int(feasible[np.argmax(recall[feasible])])
    return [("balanced",float(thresholds[best])),("fpr5",float(thresholds[constrained]))]


class Search:
    def __init__(self,frames,out,configs,jobs):
        self.frames,self.out,self.configs,self.jobs=frames,out,configs,jobs
        self.lookup={c["id"]:c for c in configs}
        self.lags={tuple(r["train_groups"]):r["selected_lag_ms"] for r in json.loads((BASE_RUN/"lag_selection.json").read_text())}
        self.force={lag:pair_lagged_force(frames,lag) for lag in set(self.lags.values())}
        self.timings=[]

    def prepared(self,groups,final=False):
        key=tuple(sorted(groups)); lag=self.lags[key]
        train=self.frames[self.frames.group.isin(key)].copy()
        val=train.copy() if final else self.frames[~self.frames.group.isin(key)].copy()
        if not final: assert set(train.session_id).isdisjoint(set(val.session_id))
        floors=_fit_floors(train)
        tp,vp=optical_features(train,floors),optical_features(val,floors)
        return {task:(task_data(train,self.force[lag].loc[train.index].to_numpy(float),tp,task),
                      task_data(val,self.force[lag].loc[val.index].to_numpy(float),vp,task)) for task in TASKS},floors,lag

    def inner_subset(self,groups):
        key=tuple(sorted(groups)); folder=self.out/"inner_cache"/("train_"+"".join(map(str,key)))
        if (folder/"complete.json").exists(): return folder
        folder.mkdir(parents=True,exist_ok=True)
        data,_,lag=self.prepared(key)
        for task,(tr,va) in data.items(): va["rows"].to_parquet(folder/f"{task}_rows.parquet",index=False)
        pending=[c for c in self.configs if not (folder/f"{c['id']}.npz").exists()]
        print(f"START inner groups={key} lag={lag} ms candidates={len(pending)}",flush=True)
        with joblib.Parallel(n_jobs=self.jobs,return_as="generator_unordered",backend="loky",inner_max_num_threads=1) as parallel:
            for i,r in enumerate(parallel(joblib.delayed(fit_candidate)(c,*data[c['task']]) for c in pending)):
                np.savez_compressed(folder/f"{r['id']}.npz",values=r["values"])
                self.timings.append(dict(train_groups=key,id=r["id"],seconds=r["seconds"],warnings=" | ".join(r["warnings"])))
                if (i+1)%20==0: print(f"  inner groups={key} completed {i+1}/{len(pending)}",flush=True)
        write_json(folder/"complete.json",dict(groups=key,lag_ms=lag,candidates=len(self.configs)))
        return folder

    def collect_inner(self,outer_fold,task):
        """Each outer-training group validated once, from disjoint 4-group fits."""
        training=set(range(1,7))-{outer_fold} if outer_fold else set(range(1,7))
        chunks=[]; arrays={c["id"]:[] for c in self.configs if c["task"]==task}
        pairs=[(training-{v},v) for v in sorted(training)] if outer_fold else [
            (set(g),v) for g in combinations(range(1,7),4) for v in sorted(set(range(1,7))-set(g))]
        for fit_groups,v in pairs:
            folder=self.out/"inner_cache"/("train_"+"".join(map(str,sorted(fit_groups))))
            r=pd.read_parquet(folder/f"{task}_rows.parquet"); mask=r.group.to_numpy()==v
            chunks.append(r.loc[mask])
            for cid in arrays:
                with np.load(folder/f"{cid}.npz") as z: arrays[cid].append(z["values"][:,mask])
        rows=pd.concat(chunks,ignore_index=True)
        return rows,{cid:np.concatenate(parts,axis=1) for cid,parts in arrays.items()}

    def choose(self,rows,arrays,task):
        y=rows.truth.to_numpy();w=session_weights(rows)
        candidates=[]
        for cid,variants in arrays.items():
            for h,values in enumerate(variants):
                base=dict(id=cid,family=self.lookup[cid]["family"],tau=HISTORY[h],h=h)
                if task=="force":
                    residual=y-values
                    for correction,offset in (("none",0.),("mean",np.average(residual,weights=w)),("median",weighted_quantile(residual,w,.5))):
                        m=fast_metrics(y,np.maximum(values+offset,0),w,task)
                        candidates.append(dict(**base,offset=float(offset),correction=correction,**m))
                elif task=="localization":
                    candidates.append(dict(**base,**fast_metrics(y,values.argmax(axis=1)+1,w,task)))
                else:
                    for policy,threshold in threshold_options(y,values,w):
                        candidates.append(dict(**base,threshold=threshold,threshold_policy=policy,
                            **fast_metrics(y,(values>=threshold).astype(int),w,task)))
        metric="mae_N" if task=="force" else ("macro_f1" if task=="localization" else "balanced_accuracy")
        def key(r): return r[metric] if task=="force" else -r[metric]
        unrestricted=[r for r in candidates if task!="contact" or r["threshold_policy"]=="balanced"]
        chosen={"Tuned selected":min(unrestricted,key=key)}
        for family in sorted({r["family"] for r in candidates}):
            chosen[f"Tuned {family}"]=min([r for r in unrestricted if r["family"]==family],key=key)
        if task=="contact":
            chosen["Tuned FPR <=5%"] = min([r for r in candidates if r["threshold_policy"]=="fpr5"],key=lambda r:(-r["recall"],r["fpr"]))
        else:
            members=sorted([v for k,v in chosen.items() if k!="Tuned selected"],key=key)[:3]
            combined=np.mean([self.apply_choice(arrays[m["id"]],m,task,raw=True) for m in members],axis=0)
            pred=combined if task=="force" else combined.argmax(axis=1)+1
            ensemble=dict(id="ensemble",family="Ensemble",members=members,**fast_metrics(y,pred,w,task))
            chosen["Tuned ensemble"]=ensemble
            if key(ensemble)<key(chosen["Tuned selected"]): chosen["Tuned selected"]=ensemble
        return chosen,pd.DataFrame(candidates)

    @staticmethod
    def apply_choice(variants,choice,task,raw=False):
        v=variants[choice["h"]].astype(float)
        if task=="force": return np.maximum(v+choice["offset"],0)
        if raw: return v
        return v.argmax(axis=1)+1 if task=="localization" else (v>=choice["threshold"]).astype(int)

    def outer(self,fold):
        selections={}
        for task in TASKS:
            rows,arrays=self.collect_inner(fold,task)
            selections[task],trials=self.choose(rows,arrays,task)
            trials.to_csv(self.out/f"inner_trials_fold{fold}_{task}.csv",index=False)
        # Seal choices before generating held-out predictions.
        write_json(self.out/f"selections_fold{fold}.json",selections)
        data,_,lag=self.prepared(set(range(1,7))-{fold})
        wanted=set()
        for choices in selections.values():
            for choice in choices.values():
                wanted.update(m["id"] for m in choice["members"]) if choice["id"]=="ensemble" else wanted.add(choice["id"])
        fitted={}
        with joblib.Parallel(n_jobs=self.jobs,return_as="generator_unordered",backend="loky",inner_max_num_threads=1) as parallel:
            for r in parallel(joblib.delayed(fit_candidate)(self.lookup[cid],*data[self.lookup[cid]["task"]]) for cid in sorted(wanted)):
                fitted[r["id"]]=r["values"]
                self.timings.append(dict(train_groups=tuple(sorted(set(range(1,7))-{fold})),id=r["id"],seconds=r["seconds"],warnings=" | ".join(r["warnings"])))
        results=[]
        for task,choices in selections.items():
            for label,choice in choices.items():
                if choice["id"]=="ensemble":
                    combined=np.mean([self.apply_choice(fitted[m["id"]],m,task,raw=True) for m in choice["members"]],axis=0)
                    pred=combined if task=="force" else combined.argmax(axis=1)+1
                else:pred=self.apply_choice(fitted[choice["id"]],choice,task)
                r=data[task][1]["rows"].assign(task=task,model=label,outer_fold=fold,prediction=pred)
                if task=="contact":r["score"]=self.apply_choice(fitted[choice["id"]],choice,task,raw=True)
                results.append(r)
        pd.concat(results,ignore_index=True).to_parquet(self.out/f"outer_fold{fold}.parquet",index=False)
        print(f"OUTER {fold}/6 finished; lag={lag} ms, fitted={len(fitted)}",flush=True)
        return selections

    def final_refit(self):
        selections={}
        for task in TASKS:
            rows,arrays=self.collect_inner(None,task)
            selections[task],trials=self.choose(rows,arrays,task)
            trials.to_csv(self.out/f"final_training_trials_{task}.csv",index=False)
        write_json(self.out/"final_selections.json",selections)
        data,floors,lag=self.prepared(range(1,7),final=True)
        modeldir=self.out/"models";modeldir.mkdir(exist_ok=True)
        chosen={task:choices["Tuned selected"] for task,choices in selections.items()}
        chosen["contact_fpr5"]=selections["contact"]["Tuned FPR <=5%"]
        for name,choice in chosen.items():
            task="contact" if name=="contact_fpr5" else name
            members=choice["members"] if choice["id"]=="ensemble" else [choice]
            bundles=[]
            for member in members:
                path=modeldir/f"{name}_{member['id']}.joblib"
                r=fit_candidate(self.lookup[member["id"]],*data[task],save_path=path)
                bundles.append(dict(file=path.name,choice=member,sha256=sha256_file(path)))
                self.timings.append(dict(train_groups=tuple(range(1,7)),id=member["id"],seconds=r["seconds"],warnings=" | ".join(r["warnings"])))
            write_json(modeldir/f"{name}.json",dict(task=task,choice=choice,members=bundles,floors=floors,
                lag_ms=lag,status="experimental_offline_only",history_reset="session boundary or gap >300 ms",
                training_sessions=sorted(self.frames.session_id.unique())))
        write_json(modeldir/"manifest.json",dict(status="experimental_offline_only",models=list(chosen),
            files={p.name:sha256_file(p) for p in modeldir.iterdir() if p.name!="manifest.json"},
            final_hyperparameter_selection="All 15 leave-two-groups-out training CV splits; no all-data fit used for reported metrics",
            warning="Load only these trusted locally produced research artifacts. Causal history requires ordered session streams."))


def summarize(out):
    predictions=pd.concat([pd.read_parquet(out/f"outer_fold{i}.parquet") for i in range(1,7)],ignore_index=True)
    predictions.to_parquet(out/"outer_predictions.parquet",index=False)
    summaries=[];folds=[]
    for (task,model),r in predictions.groupby(["task","model"]):
        summaries.append(dict(task=task,model=model,frames=len(r),sessions=r.session_id.nunique(),
            **fast_metrics(r.truth.to_numpy(),r.prediction.to_numpy(),session_weights(r),task)))
        for fold,g in r.groupby("outer_fold"):
            folds.append(dict(task=task,model=model,outer_fold=fold,frames=len(g),sessions=g.session_id.nunique(),
                **fast_metrics(g.truth.to_numpy(),g.prediction.to_numpy(),session_weights(g),task)))
    pd.DataFrame(summaries).to_csv(out/"model_comparison.csv",index=False)
    pd.DataFrame(folds).to_csv(out/"fold_metrics.csv",index=False)
    selected=pd.DataFrame(summaries).query("model == 'Tuned selected' or model == 'Tuned FPR <=5%'")
    print(selected.to_string(index=False),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=DEFAULT_OUT)
    parser.add_argument("--jobs",type=int,default=8)
    parser.add_argument("--stage",choices=["all","inner","outer","refit","summary"],default="all")
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    frames,provenance=load_rich_data();configs=candidate_grid()
    protocol=dict(created_at=datetime.now(timezone.utc).isoformat(),dataset_policy="manual_only",seed=SEED,
        base_run=str(BASE_RUN),interval_N=[.05,3],evaluation="Same held-out frames, targets, equal-session weighting and fold-local lags as original run",
        tuning="Every outer training set uses all five whole-group inner folds. Training-only feature floors and weighted scaling.",
        final_selection="All 15 leave-two-groups-out splits on the complete training pool",
        source_code_sha256=sha256_file(Path(__file__)),candidates=configs,
        lag_source_sha256=sha256_file(BASE_RUN/"lag_selection.json"),
        inference_features="Optical values only; no force, ROI target, TEST/session identity, absolute time, or recording duration in estimator input",
        history=dict(feature_tau_s=[.3,1.0],output_tau_s=list(HISTORY),causal=True,reset_gap_s=.3),
        force_postprocessing="Nonnegative output, inner-OOF residual offset none/mean/median; choose by inner MAE",
        classification_selection="Localization macro F1; contact balanced accuracy plus separate FPR<=5% policy, all inner-OOF",
        ensembles="Equal average of three best independently tuned families for force/localization; include if inner primary score improves",
        interpretation="Retrospective development on previously examined manual data; no untouched prospective test set. Temporal results describe causal streams at stationary per-session ROIs.",
        deployment="Offline research artifacts; live GUI bundle unchanged")
    if (out/"protocol.json").exists():
        old=json.loads((out/"protocol.json").read_text())
        assert old["candidates"]==json.loads(json.dumps(configs)),"Candidate search cannot change on resume"
        assert old["source_code_sha256"]==protocol["source_code_sha256"],"Source changed: use a new output directory"
    else:
        write_json(out/"protocol.json",protocol);write_json(out/"provenance.json",provenance)
        frames[["frame_uid","session_id","group","scientific_role","target_roi"]].to_parquet(out/"session_assignment.parquet",index=False)
    print(f"CANDIDATES {pd.Series([c['task'] for c in configs]).value_counts().to_dict()} jobs={args.jobs}",flush=True)
    experiment=Search(frames,out,configs,args.jobs)
    if args.stage in ("all","inner"):
        for subset in combinations(range(1,7),4):
            experiment.inner_subset(subset)
            pd.DataFrame(experiment.timings).to_csv(out/"fit_timings_inner.csv",index=False)
    if args.stage in ("all","outer"):
        for fold in range(1,7):
            if not (out/f"outer_fold{fold}.parquet").exists():experiment.outer(fold)
        summarize(out)
        pd.DataFrame(experiment.timings).to_csv(out/"fit_timings_outer.csv",index=False)
    if args.stage in ("all","refit"):
        experiment.final_refit()
        pd.DataFrame(experiment.timings).to_csv(out/"fit_timings_refit.csv",index=False)
    if args.stage=="summary":summarize(out)
    write_json(out/f"completion_{args.stage}.json",dict(completed_at=datetime.now(timezone.utc).isoformat(),stage=args.stage))


if __name__=="__main__":main()
