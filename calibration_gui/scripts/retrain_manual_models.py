"""Reproducible retrospective manual-only force/contact/ROI benchmark.

Run from calibration_gui with: .venv/Scripts/python.exe -m scripts.retrain_manual_models
Artifacts are for offline research; this command never changes the live bundle.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "2")

import joblib
import numpy as np
import pandas as pd
import sklearn
import xgboost
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import (ExtraTreesClassifier, ExtraTreesRegressor,
    HistGradientBoostingClassifier, HistGradientBoostingRegressor,
    RandomForestClassifier)
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, f1_score,
    mean_absolute_error, mean_squared_error, precision_score, recall_score,
    r2_score, roc_auc_score, confusion_matrix)
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC, SVR
from threadpoolctl import threadpool_limits

from scripts.fit_manual_only_preprocessing import (
    RAW_LIGHT_COLUMNS, AREA_COLUMNS, BASE_COLUMNS, ACTIVE_COLUMNS,
    _load_inputs, _fit_floors, _corrected_light, _balanced_weights,
    _select_lag, pair_lagged_force,
)
from scripts.live_sensor_common import PROJECT_ROOT, load_study_config, sha256_file

SEED = 1729
SIGNED = [f"roi{r}_signed_delta_v_sum" for r in range(1, 10)]
FORCE_NAMES = ["Median baseline", "Total light linear", "NNLS", "Positive Ridge",
    "Isotonic", "Monotonic HistGB", "Monotonic XGBoost", "Extra Trees", "RBF SVR"]
CLASS_NAMES = ["Logistic regression", "RBF SVC", "Random Forest", "Extra Trees",
    "HistGB", "XGBoost"]
LAG_SPEC = dict(fit_force_min_N=0.05, fit_force_max_N=3.0, tie_relative_tolerance=0.005)


def write_json(path, value):
    def safe(v):
        if isinstance(v, dict): return {str(k): safe(x) for k, x in v.items()}
        if isinstance(v, (tuple, list)): return [safe(x) for x in v]
        if isinstance(v, np.ndarray): return safe(v.tolist())
        if isinstance(v, np.generic): return safe(v.item())
        if isinstance(v, float) and not np.isfinite(v): return None
        return v
    Path(path).write_text(json.dumps(safe(value), indent=2, allow_nan=False) + "\n", encoding="utf-8")


def session_weights(frame, balance_classes=False):
    w = 1.0 / frame.groupby("session_id")["session_id"].transform("size").to_numpy(float)
    if balance_classes:
        labels = frame["scientific_role"].eq("model_primary").to_numpy(int)
        for label in np.unique(labels): w[labels == label] /= w[labels == label].sum()
    return w / w.mean()


def make_model(task, name):
    if task == "force":
        return {
            "Median baseline": lambda: DummyRegressor(strategy="median"),
            "Total light linear": lambda: LinearRegression(positive=True, fit_intercept=False),
            "NNLS": lambda: LinearRegression(positive=True, fit_intercept=False),
            "Positive Ridge": lambda: Ridge(alpha=10.0, positive=True, fit_intercept=False),
            "Isotonic": lambda: IsotonicRegression(increasing=True, out_of_bounds="clip"),
            "Monotonic HistGB": lambda: HistGradientBoostingRegressor(max_iter=150,
                max_leaf_nodes=7, min_samples_leaf=30, l2_regularization=10,
                learning_rate=0.05, monotonic_cst=[1]*9, early_stopping=False, random_state=SEED),
            "Monotonic XGBoost": lambda: xgboost.XGBRegressor(n_estimators=150,
                max_depth=3, learning_rate=0.05, reg_lambda=10, min_child_weight=10,
                monotone_constraints="(1,1,1,1,1,1,1,1,1)", tree_method="hist", n_jobs=2, random_state=SEED),
            "Extra Trees": lambda: ExtraTreesRegressor(n_estimators=150,
                min_samples_leaf=10, max_features=1.0, n_jobs=2, random_state=SEED),
            "RBF SVR": lambda: SVR(C=10, epsilon=0.1, gamma="scale"),
        }[name]()
    return {
        "Logistic regression": lambda: LogisticRegression(C=1, max_iter=3000, random_state=SEED),
        "RBF SVC": lambda: SVC(C=10, gamma="scale", probability=False, random_state=SEED),
        "Random Forest": lambda: RandomForestClassifier(n_estimators=150,
            min_samples_leaf=5, max_features="sqrt", n_jobs=2, random_state=SEED),
        "Extra Trees": lambda: ExtraTreesClassifier(n_estimators=150,
            min_samples_leaf=5, max_features=0.75, n_jobs=2, random_state=SEED),
        "HistGB": lambda: HistGradientBoostingClassifier(max_iter=120,
            max_leaf_nodes=7, min_samples_leaf=25, l2_regularization=10,
            learning_rate=0.05, early_stopping=False, random_state=SEED),
        "XGBoost": lambda: xgboost.XGBClassifier(n_estimators=120, max_depth=3,
            learning_rate=0.05, reg_lambda=10, min_child_weight=5,
            tree_method="hist", n_jobs=2, random_state=SEED),
    }[name]()


def model_x(x, task, name):
    if task == "force" and name in ("Total light linear", "Isotonic"):
        total = x.sum(axis=1)
        return total if name == "Isotonic" else total[:, None]
    return x


def predict(model, x, task, name):
    z = model_x(x, task, name)
    y = np.asarray(model.predict(z))
    if task == "localization" and name == "XGBoost": y = y + 1
    score = None
    if task == "contact":
        score = (model.decision_function(z) if name == "RBF SVC"
                 else model.predict_proba(z)[:, 1])
    return y, score


def metrics(rows, task):
    w = session_weights(rows)
    y, p = rows.truth.to_numpy(), rows.prediction.to_numpy()
    if task == "force":
        return dict(mae_N=mean_absolute_error(y,p,sample_weight=w),
            rmse_N=np.sqrt(mean_squared_error(y,p,sample_weight=w)),
            bias_N=np.average(p-y,weights=w), r2=r2_score(y,p,sample_weight=w),
            negative_prediction_rate=np.average(p < -1e-6,weights=w))
    result = dict(accuracy=accuracy_score(y,p,sample_weight=w),
        balanced_accuracy=balanced_accuracy_score(y,p,sample_weight=w),
        macro_f1=f1_score(y,p,average="macro",sample_weight=w,zero_division=0))
    if task == "contact":
        result.update(recall=recall_score(y,p,sample_weight=w,zero_division=0),
            precision=precision_score(y,p,sample_weight=w,zero_division=0),
            fpr=np.average(p[y==0]!=0,weights=w[y==0]),
            roc_auc=roc_auc_score(y,rows.score,sample_weight=w))
    return result


def objective(rows, task):
    m = metrics(rows, task)
    return m["mae_N"] if task == "force" else -m["balanced_accuracy" if task == "contact" else "macro_f1"]


def load_data():
    config, config_hash, _ = load_study_config(PROJECT_ROOT / "config/live_sensor_manual_only.json")
    frames, feature_dir, feature_report, split_dir, split_report = _load_inputs(config, config_hash)
    index = pd.read_parquet(feature_dir / "session_index.parquet").set_index("session_id")
    extra = []
    for path in sorted((feature_dir / "sessions/archive_id=manual").glob("*.parquet")):
        e = pd.read_parquet(path, columns=["frame_uid", "session_id"] + SIGNED)
        sid = str(e.session_id.iloc[0])
        if sha256_file(path) != index.loc[sid, "output_sha256"]:
            raise ValueError(f"feature partition hash mismatch: {sid}")
        extra.append(e.drop(columns="session_id"))
    frames = frames.merge(pd.concat(extra, ignore_index=True), on="frame_uid", validate="one_to_one")
    frames = frames.sort_values(["session_id", "capture_monotonic_relative_s", "video_frame_index"]).reset_index(drop=True)
    roles = frames.groupby("scientific_role").agg(sessions=("session_id","nunique"), frames=("frame_uid","size"))
    if roles.sessions.to_dict() != {"model_primary":54,"no_contact":11,"replay_only":18}:
        raise ValueError("unexpected role coverage")
    frames = frames[frames.scientific_role.isin(["model_primary","no_contact"])].copy()
    frames["group"] = np.where(frames.scientific_role.eq("model_primary"), frames.outer_fold, frames.no_contact_fold).astype(int)
    provenance = dict(dataset_policy="manual_only", feature_report=feature_report,
        split_report=split_report, session_roles=roles.reset_index().to_dict("records"),
        feature_directory=str(feature_dir), split_directory=str(split_dir),
        source_partition_hashes_verified=True, raw_archive_read_this_run=False)
    return frames, provenance


class Experiment:
    def __init__(self, frames, output):
        self.frames, self.output = frames, output
        self.lagged = {lag: pair_lagged_force(frames,lag) for lag in range(0,501,100)}
        self.cache, self.lag_records, self.timings = {}, [], []

    def subset(self, groups):
        return self.frames[self.frames.group.isin(groups)].copy()

    def fit_subset(self, groups, final=False):
        key = tuple(sorted(groups))
        if key in self.cache: return self.cache[key]
        train = self.subset(key)
        val = self.subset(set(range(1,7))-set(key)) if not final else train.copy()
        if not final: assert set(train.session_id).isdisjoint(set(val.session_id))
        lag, lag_search = _select_lag(train, self.lagged, LAG_SPEC, {f"TEST{i}":i for i in range(1,7)})
        self.lag_records.append(dict(train_groups=key, selected_lag_ms=lag, search=lag_search))
        floors = _fit_floors(train)
        all_features = {}
        for which, f in (("train",train),("val",val)):
            area = f[AREA_COLUMNS].to_numpy(float)
            positive = _corrected_light(f,floors)/area
            optical = np.column_stack([positive, f[SIGNED].to_numpy(float)/area, f[ACTIVE_COLUMNS].to_numpy(float)])
            force = self.lagged[lag].loc[f.index].to_numpy(float)
            press = f.scientific_role.eq("model_primary").to_numpy()
            valid = press & np.isfinite(force) & (force>=0.05) & (force<=3)
            all_features[which] = (positive,optical,force,valid)
        outputs, fitted = {}, {}
        for task in ("force","localization","contact"):
            names = FORCE_NAMES if task == "force" else CLASS_NAMES
            xp, xc, yf, valid = all_features["train"]
            vp, vc, vf, vvalid = all_features["val"]
            mask = valid if task != "contact" else valid | train.scientific_role.eq("no_contact").to_numpy()
            vm = vvalid if task != "contact" else vvalid | val.scientific_role.eq("no_contact").to_numpy()
            tf, ev = train.loc[mask].copy(), val.loc[vm].copy()
            x0, v0 = (xp[mask],vp[vm]) if task=="force" else (xc[mask],vc[vm])
            y = yf[mask] if task=="force" else (tf.target_roi.to_numpy(int) if task=="localization" else tf.scientific_role.eq("model_primary").to_numpy(int))
            vy = vf[vm] if task=="force" else (ev.target_roi.to_numpy(int) if task=="localization" else ev.scientific_role.eq("model_primary").to_numpy(int))
            weights = _balanced_weights(tf.session_id.to_numpy(),y) if task=="force" else session_weights(tf,task=="contact")
            scaler = StandardScaler(with_mean=(task!="force")).fit(x0,sample_weight=weights)
            x, xv = scaler.transform(x0), scaler.transform(v0)
            if task=="localization": assert set(y)==set(range(1,10))
            for name in names:
                start = time.perf_counter()
                model = make_model(task,name)
                fit_y = y-1 if task=="localization" and name=="XGBoost" else y
                model.fit(model_x(x,task,name),fit_y,sample_weight=weights)
                fit_s = time.perf_counter()-start
                start = time.perf_counter()
                pred,score = predict(model,xv,task,name)
                pred_s = time.perf_counter()-start
                r = ev[["frame_uid","session_id","group","scientific_role","target_roi","capture_monotonic_relative_s"]].copy()
                r["truth"],r["prediction"],r["reference_force_N"] = vy,pred,vf[vm]
                if score is not None: r["score"] = score
                outputs[(task,name)] = r
                self.timings.append(dict(train_groups=key,task=task,model=name,fit_s=fit_s,
                    batch_predict_s=pred_s,training_frames=len(tf),evaluation_frames=len(ev)))
                if final:
                    fitted[(task,name)] = dict(model=model,scaler=scaler,floors=floors,
                        task=task,name=name,lag_ms=lag,raw_feature_order=RAW_LIGHT_COLUMNS+SIGNED+ACTIVE_COLUMNS,
                        force_inputs="max(positive_sum - fitted_no_contact_floor, 0) / ROI area",
                        classification_inputs="nine force inputs + nine signed sums/area + nine active fractions",
                        status="experimental_offline_only",training_sessions=sorted(train.session_id.unique()))
            print(f"fit groups={key} lag={lag} ms task={task} models={len(names)} train={len(tf)} eval={len(ev)}",flush=True)
        self.cache[key] = outputs
        if final: self.final_models = fitted
        return outputs


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=PROJECT_ROOT/"analysis_outputs/live_sensor_manual_only/model_runs/retraining_20260909")
    args=parser.parse_args()
    out=args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    frames,provenance=load_data()
    protocol=dict(created_at=datetime.now(timezone.utc).isoformat(),seed=SEED,
        status="retrospective_exploratory_not_prospective_validation",dataset_policy="manual_only",
        outer_folds="leave one complete TEST group and assigned no-contact sessions out; six folds",
        inner_selection="all five remaining groups; preprocessing and lag refitted without inner validation groups",
        lag_grid_ms=list(range(0,501,100)),lag_rule="inner grouped NNLS MAE; smallest lag within 0.5%",
        force_interval_N=[0.05,3.0],range_status="exploratory nominal interval; no validated common Fdetect/Fmax",
        force_prediction_clipping=False,feature_smoothing="none; individual camera frame",
        model_names=dict(force=FORCE_NAMES,contact=CLASS_NAMES,localization=CLASS_NAMES),
        hyperparameters="fixed before new outer predictions; exact settings in source and estimator_parameters.json",
        selection_metrics=dict(force="session-balanced MAE",contact="session-balanced balanced accuracy",localization="session-balanced macro F1"),
        model_fitting_weights="force: equal sessions and 0.25 N bins; localization: equal sessions; contact: equal class and session weights",
        evaluation_weights="equal sessions; frames within each session sum to equal weight",
        contact_labels="dedicated no-contact=0, aligned primary press force 0.05-3 N=1; other press frames excluded",
        contact_threshold="native classifier threshold 0.5 probability or 0 SVC decision; no threshold tuning or debounce",
        deployment="research artifacts only; no live bundle changed",
        source_code_sha256=sha256_file(Path(__file__)),python=os.sys.version,
        versions=dict(numpy=np.__version__,pandas=pd.__version__,sklearn=sklearn.__version__,xgboost=xgboost.__version__))
    write_json(out/"protocol.json",protocol)
    write_json(out/"provenance.json",provenance)
    write_json(out/"estimator_parameters.json",{task:{name:make_model(task,name).get_params() for name in (FORCE_NAMES if task=="force" else CLASS_NAMES)} for task in ("force","contact","localization")})
    frames[["frame_uid","session_id","group","scientific_role","target_roi"]].to_parquet(out/"eligible_session_assignment.parquet",index=False)
    exp=Experiment(frames,out)
    outer, selections, nested = [],[],[]
    with threadpool_limits(limits=2):
        for fold in range(1,7):
            training=set(range(1,7))-{fold}
            inner={}
            for inner_fold in sorted(training):
                result=exp.fit_subset(training-{inner_fold})
                for key,r in result.items():
                    inner.setdefault(key,[]).append(r[r.group==inner_fold])
            chosen={}
            for task in ("force","contact","localization"):
                scores={name:objective(pd.concat(rows,ignore_index=True),task)
                        for (t,name),rows in inner.items() if t==task}
                chosen[task]=min(scores,key=scores.get)
                selections.append(dict(outer_fold=fold,task=task,chosen=chosen[task],inner_scores=scores))
            result=exp.fit_subset(training)
            for (task,name),r in result.items():
                q=r.assign(task=task,model=name,outer_fold=fold)
                outer.append(q)
                if name==chosen[task]: nested.append(q.assign(model="Nested selected"))
            print(f"OUTER FOLD {fold}/6 complete. Inner-selected: {chosen}",flush=True)
            write_json(out/"progress.json",dict(completed_outer_folds=fold,selections=selections))
        predictions=pd.concat(outer,ignore_index=True)
        nested_predictions=pd.concat(nested,ignore_index=True)
        predictions.to_parquet(out/"outer_predictions.parquet",index=False)
        nested_predictions.to_parquet(out/"nested_selected_predictions.parquet",index=False)
        all_pred=pd.concat([predictions,nested_predictions],ignore_index=True)
        summary,fold_metrics,roi_metrics=[],[],[]
        rng=np.random.default_rng(SEED)
        draws=rng.integers(1,7,size=(2000,6))
        for (task,name),r in all_pred.groupby(["task","model"],sort=False):
            m=metrics(r,task)
            fm=[]
            for fold,g in r.groupby("outer_fold"):
                item=dict(task=task,model=name,outer_fold=int(fold),frames=len(g),sessions=g.session_id.nunique(),**metrics(g,task))
                fold_metrics.append(item); fm.append(item)
            primary="mae_N" if task=="force" else ("balanced_accuracy" if task=="contact" else "macro_f1")
            vals=np.array([v[primary] for v in fm])
            boot=vals[draws-1].mean(axis=1)
            summary.append(dict(task=task,model=name,frames=len(r),sessions=r.session_id.nunique(),**m,
                primary_metric=primary,fold_mean_primary=vals.mean(),fold_min_primary=vals.min(),fold_max_primary=vals.max(),
                fold_bootstrap95_low=np.quantile(boot,.025),fold_bootstrap95_high=np.quantile(boot,.975)))
            if task!="contact":
                for roi,g in r.groupby("target_roi"):
                    rm=(metrics(g,task) if task=="force" else {"recall":np.average(g.truth==g.prediction,weights=session_weights(g))})
                    roi_metrics.append(dict(task=task,model=name,roi=int(roi),frames=len(g),sessions=g.session_id.nunique(),**rm))
        summary=pd.DataFrame(summary)
        summary.to_csv(out/"model_comparison.csv",index=False)
        pd.DataFrame(fold_metrics).to_csv(out/"fold_metrics.csv",index=False)
        pd.DataFrame(roi_metrics).to_csv(out/"roi_metrics.csv",index=False)
        write_json(out/"inner_selections.json",selections)
        write_json(out/"lag_selection.json",exp.lag_records)
        winners={}
        for task in ("force","contact","localization"):
            rows=summary[(summary.task==task)&(summary.model!="Nested selected")]
            key="mae_N" if task=="force" else ("balanced_accuracy" if task=="contact" else "macro_f1")
            winners[task]=rows.sort_values(key,ascending=(task=="force")).iloc[0].model
        write_json(out/"winners.json",winners)
        exp.fit_subset(set(range(1,7)),final=True)
        model_dir=out/"models"; model_dir.mkdir()
        for task,name in winners.items():
            bundle=exp.final_models[(task,name)]
            joblib.dump(bundle,model_dir/f"{task}.joblib",compress=3)
            if name in ("XGBoost","Monotonic XGBoost"):
                bundle["model"].save_model(model_dir/f"{task}_estimator.json")
            r=predictions[(predictions.task==task)&(predictions.model==name)]
            if task=="localization":
                cm=confusion_matrix(r.truth,r.prediction,labels=range(1,10),sample_weight=session_weights(r))
                cm=cm/cm.sum(axis=1,keepdims=True)
                pd.DataFrame(cm,index=range(1,10),columns=range(1,10)).to_csv(out/"localization_confusion_normalized.csv")
        write_json(out/"lag_selection.json",exp.lag_records)
        pd.DataFrame(exp.timings).to_csv(out/"fit_timings.csv",index=False)
        write_json(model_dir/"manifest.json",dict(status="experimental_offline_only",winners=winners,
            files={p.name:sha256_file(p) for p in model_dir.iterdir() if p.is_file()},
            warning="joblib files are trusted local research artifacts, never application bundles; load only files produced by this run",
            feature_spec_hash=provenance["feature_report"]["feature_spec_hash"],
            training_sessions=65,held_out_performance_source="outer_predictions.parquet; all-data refit not scored as validation"))
        write_json(out/"completion.json",dict(completed_at=datetime.now(timezone.utc).isoformat(),winners=winners,
            outer_folds=6,fit_subsets=len(exp.cache),model_fits=len(exp.timings),all_outputs_finite=bool(np.isfinite(predictions.prediction).all())))
    print(summary[["task","model","mae_N","rmse_N","r2","accuracy","macro_f1","recall","fpr"]].to_string(index=False),flush=True)
    print(f"Completed: {out}",flush=True)


if __name__=="__main__": main()
