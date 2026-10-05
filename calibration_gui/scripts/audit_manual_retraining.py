"""Independently audit saved OOF metrics and the optical-only offline refit."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import numpy as np
import pandas as pd
from sklearn.metrics import confusion_matrix
from threadpoolctl import threadpool_limits
from scripts.retrain_manual_models import write_json,load_data,pair_lagged_force
from scripts.predict_retrained_manual import infer,load_research_models
from scripts.live_sensor_common import sha256_file


def independent_metrics(r,task):
    counts=r.groupby("session_id").size()
    w=1/counts.reindex(r.session_id).to_numpy(float)
    y,p=r.truth.to_numpy(),r.prediction.to_numpy()
    if task=="force":
        errors=pd.DataFrame(dict(session=r.session_id.to_numpy(),ae=abs(y-p),se=(y-p)**2,bias=p-y))
        avg=errors.groupby("session")[["ae","se","bias"]].mean().mean()
        return dict(mae_N=avg.ae,rmse_N=np.sqrt(avg.se),bias_N=avg.bias,
            r2=1-np.sum(w*(y-p)**2)/np.sum(w*(y-np.average(y,weights=w))**2))
    labels=[0,1] if task=="contact" else list(range(1,10))
    cm=confusion_matrix(y,p,labels=labels,sample_weight=w)
    tp=np.diag(cm); recall=tp/cm.sum(axis=1)
    f1=2*tp/(cm.sum(axis=0)+cm.sum(axis=1))
    m=dict(accuracy=tp.sum()/cm.sum(),balanced_accuracy=recall.mean(),macro_f1=f1.mean())
    if task=="contact": m.update(recall=recall[1],fpr=cm[0,1]/cm[0].sum(),precision=cm[1,1]/cm[:,1].sum())
    return m


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--run",type=Path,required=True); a=p.parse_args(); out=a.run
    pred=pd.read_parquet(out/"outer_predictions.parquet")
    nested=pd.read_parquet(out/"nested_selected_predictions.parquet")
    comp=pd.read_csv(out/"model_comparison.csv")
    # The nested selector may mix SVC decision scores and forest probabilities.
    # Those score scales cannot be pooled into one ROC-AUC.
    comp.loc[(comp.task=="contact")&(comp.model=="Nested selected"),"roc_auc"]=np.nan
    comp.to_csv(out/"model_comparison.csv",index=False)
    winners=json.loads((out/"winners.json").read_text())
    prov=json.loads((out/"provenance.json").read_text())
    assignments=pd.read_parquet(out/"eligible_session_assignment.parquet")
    frames,_=load_data()
    lag_records=json.loads((out/"lag_selection.json").read_text())
    coverage=[]
    for fold in range(1,7):
        fitted=next(r for r in lag_records if set(r["train_groups"])==set(range(1,7))-{fold})
        f=frames[(frames.group==fold)&frames.scientific_role.eq("model_primary")]
        force=pair_lagged_force(f,fitted["selected_lag_ms"]).to_numpy()
        coverage.append(dict(outer_fold=fold,lag_ms=fitted["selected_lag_ms"],primary_frames=len(f),
            unpaired_frames=int((~np.isfinite(force)).sum()),below_005_N=int((force<.05).sum()),
            above_3_N=int((force>3).sum()),evaluated_frames=int(((force>=.05)&(force<=3)).sum())))
    pd.DataFrame(coverage).to_csv(out/"evaluation_coverage.csv",index=False)
    assert sum(x["evaluated_frames"] for x in coverage)==int(comp[comp.task=="force"].iloc[0].frames)
    assert not assignments.frame_uid.duplicated().any()
    assert set(assignments.scientific_role)=={"model_primary","no_contact"}
    assert assignments.groupby("session_id").group.nunique().max()==1
    assert set(pred.outer_fold)==set(range(1,7))
    checks=[]
    for (task,name),r in pd.concat([pred,nested]).groupby(["task","model"]):
        assert not r.frame_uid.duplicated().any()
        assert (r.group==r.outer_fold).all()
        actual=independent_metrics(r,task)
        expected=comp[(comp.task==task)&(comp.model==name)].iloc[0]
        for key,value in actual.items():
            delta=abs(value-expected[key]);assert delta<1e-10,(task,name,key,delta)
            checks.append(dict(task=task,model=name,metric=key,absolute_difference=delta))
    # Every candidate in a task sees exactly the same held-out rows.
    for task,rows in pred.groupby("task"):
        sets=[set(g.frame_uid) for _,g in rows.groupby("model")]
        assert all(s==sets[0] for s in sets)
    chosen=json.loads((out/"inner_selections.json").read_text())
    for item in chosen:
        f,task=item["outer_fold"],item["task"]
        original=pred[(pred.outer_fold==f)&(pred.task==task)&(pred.model==item["chosen"])].sort_values("frame_uid")
        selected=nested[(nested.outer_fold==f)&(nested.task==task)].sort_values("frame_uid")
        np.testing.assert_array_equal(original.prediction,selected.prediction)
    joined=pred[(pred.task=="force")&(pred.model==winners["force"])].copy()
    for task in ("contact","localization"):
        r=pred[(pred.task==task)&(pred.model==winners[task])]
        joined=joined.merge(r[["frame_uid","prediction"]].rename(columns={"prediction":task}),on="frame_uid",validate="one_to_one")
    joined["force_estimate"]=joined.prediction
    joined["prediction"]=np.where(joined.contact==1,joined.force_estimate,0)
    e2e=independent_metrics(joined,"force")
    rates=joined.assign(correct_roi=joined.localization==joined.target_roi,
        joint_correct=(joined.localization==joined.target_roi)&(joined.contact==1))
    e2e.update(joint_contact_and_roi_accuracy=rates.groupby("session_id").joint_correct.mean().mean(),
        contact_recall=rates.groupby("session_id").contact.mean().mean(),
        primary_frames=len(joined),primary_sessions=joined.session_id.nunique())
    write_json(out/"end_to_end_metrics.json",e2e)
    bins=[]
    for lo,hi in ((.05,.5),(.5,1),(1,1.5),(1.5,2),(2,2.5),(2.5,3.000001)):
        r=rates[(rates.truth>=lo)&(rates.truth<hi)]
        if r.empty: continue
        r=r.copy();r["prediction"]=r.force_estimate
        bins.append(dict(force_bin=f"{lo:g}-{min(hi,3):g} N",frames=len(r),sessions=r.session_id.nunique(),
            force_mae_N=independent_metrics(r,"force")["mae_N"],
            contact_recall=r.groupby("session_id").contact.mean().mean(),
            localization_accuracy=r.groupby("session_id").correct_roi.mean().mean()))
    pd.DataFrame(bins).to_csv(out/"force_bin_metrics.csv",index=False)
    false_contacts=[]
    for name,rows in pred[(pred.task=="contact")&(pred.truth==0)].groupby("model"):
        episodes=0;duration=0
        for _,r in rows.groupby("session_id"):
            r=r.sort_values("capture_monotonic_relative_s"); flags=r.prediction.to_numpy(int)
            episodes+=int(np.sum((flags==1)&(np.r_[0,flags[:-1]]==0)))
            t=r.capture_monotonic_relative_s.to_numpy(); duration+=t[-1]-t[0]+np.median(np.diff(t))
        false_contacts.append(dict(model=name,false_contact_episodes=episodes,no_contact_minutes=duration/60,episodes_per_minute=episodes/(duration/60)))
    pd.DataFrame(false_contacts).to_csv(out/"no_contact_episodes.csv",index=False)
    # Model reload, frame-level inference, reference invariance, and batch parity.
    models=load_research_models(out/"models")
    feature_dir=Path(prov["feature_directory"])/"sessions/archive_id=manual"
    raw=pd.concat([pd.read_parquet(path).iloc[:2] for path in sorted(feature_dir.glob("*.parquet"))],ignore_index=True)
    with threadpool_limits(limits=2):
        batch=infer(raw,models)
        alternate=raw.copy();alternate["reference_force_corrected_N"]=999
        pd.testing.assert_frame_equal(batch,infer(alternate,models))
        single=pd.concat([infer(raw.iloc[i:i+1],models) for i in range(len(raw))])
        np.testing.assert_allclose(batch.force,single.force,rtol=1e-5,atol=1e-6)
        np.testing.assert_array_equal(batch.contact,single.contact)
        np.testing.assert_array_equal(batch.localization,single.localization)
        latency=[]
        for _ in range(60):
            start=time.perf_counter();infer(raw.iloc[:1],models);latency.append((time.perf_counter()-start)*1000)
    batch.to_csv(out/"reload_inference_smoke.csv",index=False)
    timing=dict(optical_features_to_three_predictions_p50_ms=np.median(latency),
        optical_features_to_three_predictions_p95_ms=np.quantile(latency,.95),iterations=60,
        excludes="camera acquisition, pixel feature extraction, GUI display",includes="DataFrame slicing, feature normalization and all three estimators")
    write_json(out/"inference_timing.json",timing)
    protocol=json.loads((out/"protocol.json").read_text())
    assert protocol["source_code_sha256"]==sha256_file(Path(__file__).with_name("retrain_manual_models.py"))
    audit=dict(status="passed",metric_spot_checks=len(checks),maximum_metric_difference=max(c["absolute_difference"] for c in checks),
        whole_session_outer_splits=True,identical_candidate_evaluation_rows=True,no_replay_rows=True,
        nested_selection_predictions_match=True,model_hashes_valid=True,reference_force_not_used_by_inference=True,
        reload_single_batch_parity=True,training_source_code_unchanged=True,
        source_partition_hashes_verified=prov["source_partition_hashes_verified"],
        corrected_metrics={"nested_contact_pooled_roc_auc":"omitted: selected estimators use incompatible score scales"},
        assessment="share with caveats: retrospective, exploratory force interval, six dependent CV folds; no prospective live validation",
        omitted_metrics={"localization_mm_error":"nine ROI labels; no calibrated continuous spatial ground truth",
            "live_contact_onset_delay":"this benchmark scores individual frames; no deployed event/debounce pipeline",
            "validated_safe_range":"prior manual-only scientific range gate remains failed"})
    write_json(out/"validation_audit.json",audit)
    print(json.dumps(dict(audit=audit,end_to_end=e2e,timing=timing),indent=2))


if __name__=="__main__": main()
