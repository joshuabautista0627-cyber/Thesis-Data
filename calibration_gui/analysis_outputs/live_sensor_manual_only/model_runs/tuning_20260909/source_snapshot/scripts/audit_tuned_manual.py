"""Independent metric, provenance, held-out-row and causal-inference audit."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import numpy as np
import pandas as pd
from scripts.audit_manual_retraining import independent_metrics
from scripts.retrain_manual_models import write_json
from scripts.tune_manual_models import BASE_RUN, DEFAULT_OUT, Search, candidate_grid, load_rich_data, fit_candidate, session_weights
from scripts.predict_tuned_manual import load_models, infer_sequence, StreamingResearchPredictor
from scripts.live_sensor_common import sha256_file


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--run",type=Path,default=DEFAULT_OUT)
    args=parser.parse_args();out=args.run
    pred=pd.read_parquet(out/"outer_predictions.parquet")
    comp=pd.read_csv(out/"model_comparison.csv")
    old=pd.read_parquet(BASE_RUN/"outer_predictions.parquet")
    old_nested=pd.read_parquet(BASE_RUN/"nested_selected_predictions.parquet")
    old_winners=json.loads((BASE_RUN/"winners.json").read_text())
    frames,provenance=load_rich_data()
    protocol=json.loads((out/"protocol.json").read_text())
    assert protocol["source_code_sha256"]==sha256_file(Path(__file__).with_name("tune_manual_models.py"))
    assert protocol["lag_source_sha256"]==sha256_file(BASE_RUN/"lag_selection.json")
    frame_spec=json.loads((out/"frame_only_protocol.json").read_text())
    assert frame_spec["source_code_sha256"]==sha256_file(Path(__file__).with_name("evaluate_manual_frame_only.py"))
    assert set(frames.archive_id)=={"manual"}
    assert set(frames.scientific_role)=={"model_primary","no_contact"}
    assignment=pd.read_parquet(out/"session_assignment.parquet")
    assert assignment.groupby("session_id").group.nunique().max()==1
    assert assignment.session_id.nunique()==65
    assert not assignment.frame_uid.duplicated().any()
    checked_inner_splits=0
    for outer_fold in range(1,7):
        for validation_group in sorted(set(range(1,7))-{outer_fold}):
            training=tuple(sorted(set(range(1,7))-{outer_fold,validation_group}))
            folder=out/"inner_cache"/("train_"+"".join(map(str,training)))
            record=json.loads((folder/"complete.json").read_text())
            assert tuple(record["groups"])==training
            assert record["candidates"]==len(protocol["candidates"])
            training_sessions=set(assignment[assignment.group.isin(training)].session_id)
            validation=pd.read_parquet(folder/"force_rows.parquet")
            inner=validation[validation.group==validation_group]
            assert len(inner)>0 and set(inner.session_id).isdisjoint(training_sessions)
            assert outer_fold not in set(inner.group)
            checked_inner_splits+=1
    checks=[];before_after=[];roi=[]
    for (task,model),r in pred.groupby(["task","model"]):
        assert not r.frame_uid.duplicated().any()
        assert (r.group==r.outer_fold).all()
        baseline=old[(old.task==task)&(old.model==old_winners[task])].set_index("frame_uid").sort_index()
        actual=r.set_index("frame_uid").sort_index()
        assert actual.index.equals(baseline.index),(task,model,"frame set changed")
        np.testing.assert_array_equal(actual.truth,baseline.truth)
        np.testing.assert_array_equal(actual.session_id,baseline.session_id)
        expected=comp[(comp.task==task)&(comp.model==model)].iloc[0]
        for metric,value in independent_metrics(r,task).items():
            diff=abs(float(expected[metric])-float(value));assert diff<1e-10,(task,model,metric,diff)
            checks.append(dict(task=task,model=model,metric=metric,difference=diff))
        if model in ("Tuned selected","Tuned FPR <=5%","Tuned frame-only"):
            for baseline_label,base in (("Previous best fixed model",baseline.reset_index()),
                    ("Previous nested selector",old_nested[old_nested.task==task])):
                b=independent_metrics(base,task);a=independent_metrics(r,task)
                for metric,value in a.items():
                    before_after.append(dict(task=task,model=model,baseline=baseline_label,
                        baseline_model=old_winners[task] if baseline_label=="Previous best fixed model" else "Nested selected",
                        metric=metric,before=b[metric],after=value,change=value-b[metric],
                        improvement=(abs(b[metric])-abs(value) if metric=="bias_N" else b[metric]-value if metric in ("mae_N","rmse_N","fpr") else value-b[metric])))
        if model=="Tuned selected" and task!="contact":
            for target,g in r.groupby("target_roi"):
                m=independent_metrics(g,"force") if task=="force" else dict(recall=g.assign(correct=g.prediction==g.truth).groupby("session_id").correct.mean().mean())
                roi.append(dict(task=task,roi=target,frames=len(g),sessions=g.session_id.nunique(),**m))
    pd.DataFrame(checks).to_csv(out/"metric_audit.csv",index=False)
    pd.DataFrame(before_after).to_csv(out/"before_after.csv",index=False)
    pd.DataFrame(roi).to_csv(out/"roi_metrics.csv",index=False)
    selected=pred[pred.model=="Tuned selected"]
    joined=selected[selected.task=="force"].copy().rename(columns={"prediction":"force_estimate"})
    for task in ("contact","localization"):
        r=selected[selected.task==task]
        joined=joined.merge(r[["frame_uid","prediction"]].rename(columns={"prediction":task}),on="frame_uid",validate="one_to_one")
    joined["prediction"]=np.where(joined.contact==1,joined.force_estimate,0)
    e2e=independent_metrics(joined,"force")
    joined["correct_roi"]=joined.localization==joined.target_roi
    joined["joint_correct"]=joined.correct_roi & (joined.contact==1)
    e2e.update(joint_contact_and_roi_accuracy=joined.groupby("session_id").joint_correct.mean().mean(),
        contact_recall=joined.groupby("session_id").contact.mean().mean(),frames=len(joined),sessions=joined.session_id.nunique())
    write_json(out/"end_to_end_metrics.json",e2e)
    bins=[]
    for lo,hi in ((.05,.5),(.5,1),(1,1.5),(1.5,2),(2,2.5),(2.5,3.000001)):
        r=joined[(joined.truth>=lo)&(joined.truth<hi)].copy();r["prediction"]=r.force_estimate
        bins.append(dict(force_bin=f"{lo:g}-{min(hi,3):g} N",frames=len(r),sessions=r.session_id.nunique(),
            force_mae_N=independent_metrics(r,"force")["mae_N"],contact_recall=r.groupby("session_id").contact.mean().mean(),
            localization_accuracy=r.groupby("session_id").correct_roi.mean().mean()))
    pd.DataFrame(bins).to_csv(out/"force_bin_metrics.csv",index=False)
    # Paired six-group bootstrap for change; descriptive only after repeated development.
    rng=np.random.default_rng(1741);draws=rng.integers(0,6,(5000,6));uncertainty=[]
    for task,metric in (("force","mae_N"),("localization","macro_f1"),("contact","balanced_accuracy")):
        differences=[]
        for fold in range(1,7):
            a=selected[(selected.task==task)&(selected.outer_fold==fold)]
            b=old[(old.task==task)&(old.model==old_winners[task])&(old.outer_fold==fold)]
            differences.append(independent_metrics(a,task)[metric]-independent_metrics(b,task)[metric])
        draws_values=np.asarray(differences)[draws].mean(axis=1)
        uncertainty.append(dict(task=task,metric=metric,mean_fold_change=np.mean(differences),
            bootstrap95_low=np.quantile(draws_values,.025),bootstrap95_high=np.quantile(draws_values,.975),
            fold_changes=differences,interpretation="Descriptive paired six-group interval; CV training sets overlap, and all data have been used in prior development."))
    write_json(out/"paired_uncertainty.json",uncertainty)
    # Refit one complete outer fold to independently reproduce exported predictions.
    experiment=Search(frames,out,candidate_grid(),1)
    data,_,_=experiment.prepared((2,3,4,5,6))
    choices=json.loads((out/"selections_fold1.json").read_text())
    replay=[]
    for task in ("force","localization","contact"):
        choice=choices[task]["Tuned selected"]
        members=choice["members"] if choice["id"]=="ensemble" else [choice]
        values=[]
        for member in members:
            result=fit_candidate(experiment.lookup[member["id"]],*data[task])
            values.append(experiment.apply_choice(result["values"],member,task,raw=True))
        v=np.mean(values,axis=0)
        predictions=v if task=="force" else v.argmax(axis=1)+1 if task=="localization" else (v>=choice["threshold"]).astype(int)
        expected=pred[(pred.task==task)&(pred.model=="Tuned selected")&(pred.outer_fold==1)]
        np.testing.assert_array_equal(data[task][1]["rows"].frame_uid,expected.frame_uid)
        np.testing.assert_allclose(predictions,expected.prediction,rtol=0,atol=1e-7)
        replay.append(task)
    models=load_models(out/"models")
    for spec in models.values():
        for fit in spec["loaded_members"]:
            assert np.isfinite(fit["scaler"].scale_).all()
            assert np.isfinite(fit["scaler"].mean_).all()
    ids=[frames[frames.scientific_role.eq("model_primary") & frames.group.eq(g)].session_id.iloc[0] for g in (1,4)]
    ids.append(frames[frames.scientific_role.eq("no_contact")].session_id.iloc[0])
    sample=pd.concat([frames[frames.session_id==sid].iloc[:32] for sid in ids],ignore_index=True)
    result=infer_sequence(sample,models)
    altered=sample.copy();altered["reference_force_corrected_N"]=9999;altered["target_roi"]=99;altered["group"]=99
    pd.testing.assert_frame_equal(result,infer_sequence(altered,models))
    optical_only=sample.drop(columns=["reference_force_corrected_N","target_roi","scientific_role","group","test_group"])
    pd.testing.assert_frame_equal(result,infer_sequence(optical_only,models))
    prefix=infer_sequence(sample.iloc[:16],models)
    pd.testing.assert_frame_equal(prefix,result.iloc[:16])
    future=sample.copy()
    optical=[c for c in future if c.startswith("roi") and any(k in c for k in ("sum","median","mad","raw_v"))]
    future.loc[16:31,optical]*=3
    pd.testing.assert_frame_equal(result.iloc[:16],infer_sequence(future,models).iloc[:16])
    standalone=infer_sequence(sample.iloc[32:64],models)
    pd.testing.assert_frame_equal(standalone,result.iloc[32:64])
    streaming=StreamingResearchPredictor(models)
    stream_rows=[];stream_latency=[]
    for i in range(len(sample)):
        start=time.perf_counter();stream_rows.append(streaming.predict_one(sample.iloc[i:i+1]));stream_latency.append((time.perf_counter()-start)*1000)
    pd.testing.assert_frame_equal(pd.DataFrame(stream_rows,index=sample.index),result,rtol=1e-5,atol=1e-6)
    frame_models=load_models(out/"models_frame_only")
    for spec in frame_models.values():
        for fit in spec["loaded_members"]:assert np.isfinite(fit["scaler"].scale_).all()
    instantaneous=infer_sequence(sample,frame_models)
    singles=pd.concat([infer_sequence(sample.iloc[i:i+1],frame_models) for i in range(12)])
    pd.testing.assert_frame_equal(instantaneous.iloc[:12],singles)
    timing=[]
    for _ in range(5):
        start=time.perf_counter();infer_sequence(sample,models);timing.append((time.perf_counter()-start)*1000)
    write_json(out/"inference_timing.json",dict(frames_per_batch=len(sample),median_batch_ms=np.median(timing),
        amortized_ms_per_frame=np.median(timing)/len(sample),repeats=5,
        streaming_p50_ms=np.median(stream_latency[1:]),streaming_p95_ms=np.quantile(stream_latency[1:],.95),
        meaning="Offline optical-feature inference timing, including causal state. No camera/pixel-extraction/onset-delay claim.",
        includes="Three primary tasks plus alternate contact policy; excludes video decoding and pixel feature extraction"))
    result.to_csv(out/"reload_inference_smoke.csv",index=False)
    audit=dict(status="passed",completed_at=datetime.now(timezone.utc).isoformat(),metric_checks=len(checks),
        max_metric_difference=max(r["difference"] for r in checks),same_held_out_frames_and_truth_as_before=True,
        manual_source_hashes_verified=provenance["source_partition_hashes_verified"],whole_sessions_held_out=True,
        verified_inner_split_boundaries=checked_inner_splits,
        source_and_lag_hashes_unchanged=True,outer_fold1_refit_reproduced_tasks=replay,
        final_models_checksum_verified=True,reference_and_target_invariance=True,labels_not_required_for_inference=True,causal_prefix_and_future_invariance=True,
        session_reset_inference_parity=True,current_frame_batch_single_parity=True,streaming_sequence_parity=True,
        saved_scaler_parameters_finite=True,
        caveats=["Retrospective manual-only development; no unused prospective test recordings remain",
            "0.05-3 N remains an exploratory evaluation interval",
            "Temporal smoothing is causal but may delay responses, and ROI is stationary within each recording",
            "No live GUI bundle was replaced; continuous-location millimetre error and deployed onset delay are unavailable",
            "Contact AUC is omitted for adaptive families/thresholds with uncalibrated cross-fold score scales"])
    write_json(out/"validation_audit.json",audit)
    print(json.dumps(dict(audit=audit,end_to_end=e2e),indent=2))


if __name__=="__main__":main()
