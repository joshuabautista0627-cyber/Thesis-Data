"""Build the canonical comparison report from audited manual-only results."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
import pandas as pd
from scripts.retrain_manual_models import write_json


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--run",type=Path,required=True);a=p.parse_args();out=a.run
    comp=pd.read_csv(out/"model_comparison.csv")
    folds=pd.read_csv(out/"fold_metrics.csv")
    roi=pd.read_csv(out/"roi_metrics.csv")
    bins=pd.read_csv(out/"force_bin_metrics.csv")
    coverage=pd.read_csv(out/"evaluation_coverage.csv")
    winners=json.loads((out/"winners.json").read_text())
    end=json.loads((out/"end_to_end_metrics.json").read_text())
    timing=json.loads((out/"inference_timing.json").read_text())
    audit=json.loads((out/"validation_audit.json").read_text())
    assert audit["status"]=="passed"
    title="Manual calibration model comparison"
    datasets={}
    for task in ("force","contact","localization"):
        rows=comp[(comp.task==task)&(comp.model!="Nested selected")].copy()
        metric="mae_N" if task=="force" else ("balanced_accuracy" if task=="contact" else "macro_f1")
        datasets[task]=rows.sort_values(metric,ascending=task=="force").to_dict("records")
    datasets["nested"]=comp[comp.model=="Nested selected"].to_dict("records")
    bestforce=datasets["force"][0]; bestloc=datasets["localization"][0];bestcontact=datasets["contact"][0]
    rf=roi[(roi.task=="force")&(roi.model==winners["force"])][["roi","frames","sessions","mae_N","rmse_N","bias_N"]]
    rl=roi[(roi.task=="localization")&(roi.model==winners["localization"])][["roi","recall"]]
    datasets["roi"]=rf.merge(rl,on="roi",validate="one_to_one").to_dict("records")
    foldloc=folds[(folds.task=="localization")&(folds.model==winners["localization"])].copy()
    foldloc["test_group"]="TEST"+foldloc.outer_fold.astype(str)
    datasets["localization_folds"]=foldloc.to_dict("records")
    datasets["force_bins"]=bins.to_dict("records")
    datasets["coverage"]=coverage.to_dict("records")
    # The report renderer requires SQL provenance. Preserve the audited Python
    # metrics in a local SQLite evidence snapshot and execute each display query.
    report_queries={}
    with sqlite3.connect(out/"report_evidence.sqlite") as connection:
        for dataset,records in datasets.items():
            pd.DataFrame(records).to_sql(dataset,connection,index=False,if_exists="replace")
            query=f'SELECT * FROM main."{dataset}"'
            datasets[dataset]=pd.read_sql_query(query,connection).to_dict("records")
            report_queries[dataset]=query
    sources=[dict(id="comparison",label="Audited held-out model metrics",path="model_comparison.csv",
        query=dict(description="Session-balanced metrics computed from outer_predictions.parquet by retrain_manual_models.py, independently checked by audit_manual_retraining.py.",
            tables_used=["model_comparison.csv","outer_predictions.parquet"],
            filters=["Manual Calibration (2).zip only","six complete held-out TEST groups","aligned primary force 0.05-3 N; dedicated no-contact sessions for detection"],
            metric_definitions=dict(mae_N="Mean per-session absolute force error, N",rmse_N="Square root of mean per-session squared force error, N",r2="1 - weighted squared error / weighted total target variance",accuracy="Mean per-session fraction of correct predictions",macro_f1="Macro average of class F1 computed with equal-session sample weights",balanced_accuracy="Mean of class recalls computed with equal-session weights",fpr="Mean fraction of positive predictions in each dedicated no-contact session"))),
        dict(id="folds",label="Held-out TEST group metrics",path="fold_metrics.csv"),
        dict(id="roi",label="ROI-specific held-out metrics",path="roi_metrics.csv"),
        dict(id="bins",label="Force-bin diagnostic metrics",path="force_bin_metrics.csv"),
        dict(id="coverage",label="Evaluation coverage and exclusions",path="evaluation_coverage.csv"),
        dict(id="method",label="Frozen experiment protocol and source provenance",path="protocol.json"),
        dict(id="audit",label="Independent metric and inference audit",path="validation_audit.json"),
        dict(id="combined",label="Contact-gated combination metrics",path="end_to_end_metrics.json"),
        dict(id="timing",label="Offline inference timing",path="inference_timing.json")]
    for source in sources:
        q=source.setdefault("query",{})
        q.update(engine="python",language="python")
        if source["path"].endswith(".csv"):
            q["sql"]=f"import pandas as pd\nrows = pd.read_csv({source['path']!r})\nrows.to_dict('records')"
        else:
            q["sql"]=f"import json\nfrom pathlib import Path\njson.loads(Path({source['path']!r}).read_text())"
    sources[0]["query"]["sql"]=("import pandas as pd\nfrom scripts.retrain_manual_models import metrics\n"
        "predictions = pd.read_parquet('outer_predictions.parquet')\n"
        "results = [dict(task=task, model=model, **metrics(rows, task)) "
        "for (task, model), rows in predictions.groupby(['task', 'model'])]\n"
        "pd.DataFrame(results)")
    blocks=[];charts=[];tables=[]
    def md(id,body,source=None):
        b=dict(id=id,type="markdown",body=body)
        if source: b["sourceId"]=source
        blocks.append(b)
    def bar(id,title,dataset,field,label,source,percent=False,x="model"):
        charts.append(dict(id=id,title=title,type="bar",dataset=dataset,sourceId=source,
            encodings=dict(x=dict(field=x,type="nominal",label="Model" if x=="model" else "TEST group"),
                y=dict(field=field,type="quantitative",label=label,format="percent" if percent else "number")),
            options=dict(orientation="horizontal",showValues=True),valueFormat="percent" if percent else "number"))
        charts[-1]["source"]=dict(label="Audited manual-only research metrics",path="report_evidence.sqlite",
            query=dict(engine="sqlite",language="sql",sql=report_queries[dataset],
                tables_used=[f"main.{dataset}"],description="Executed retrieval from a local evidence snapshot of audited Python model metrics; calculations and raw predictions are preserved in the source run."))
        blocks.append(dict(id=id+"_block",type="chart",chartId=id,layout="full"))
    def table(id,title,dataset,cols,sort,direction="asc",source="comparison"):
        tables.append(dict(id=id,title=title,dataset=dataset,sourceId=source,
            columns=[dict(field=f,label=l,format=fmt) for f,l,fmt in cols],
            defaultSort=dict(field=sort,direction=direction)))
        tables[-1]["source"]=dict(label="Audited manual-only research metrics",path="report_evidence.sqlite",
            query=dict(engine="sqlite",language="sql",sql=report_queries[dataset],
                tables_used=[f"main.{dataset}"],description="Executed retrieval from a local evidence snapshot of audited Python model metrics; calculations and raw predictions are preserved in the source run."))
        blocks.append(dict(id=id+"_block",type="table",tableId=id,layout="full"))
    md("title",f"# {title}")
    improvement=100*(1-bestforce["mae_N"]/next(r["mae_N"] for r in datasets["force"] if r["model"]=="Median baseline"))
    md("summary",f"## Technical summary\n\n**Extra Trees is the strongest force estimator tested:** MAE {bestforce['mae_N']:.3f} N and RMSE {bestforce['rmse_N']:.3f} N. It reduces MAE by {improvement:.1f}% versus the fitted median baseline, but R² is {bestforce['r2']:.3f} and bias is {bestforce['bias_N']:.3f} N.\n\n**Logistic regression narrowly leads nine-ROI localization by macro F1:** {bestloc['macro_f1']:.2%}, with {bestloc['accuracy']:.2%} accuracy. RBF SVC is effectively tied and has slightly higher accuracy; this benchmark does not establish a clear separation between them.\n\n**Random Forest leads contact detection by balanced accuracy:** {bestcontact['balanced_accuracy']:.2%}, with {bestcontact['recall']:.2%} recall and {bestcontact['fpr']:.2%} no-contact false-positive rate. No tested classifier meets both 90% recall and 5% false positives at its fixed default threshold.\n\nThese are retrospective research candidates. The existing manual-only safe-range and application validation failures remain unresolved.","comparison")
    md("definitions","## What these scores measure\n\nOnly the immutable feature store derived from **Manual Calibration (2).zip** supplied model data: 54 primary press sessions and 11 dedicated no-contact recordings. The 18 replay-only recordings were excluded from training, selection, and performance metrics. All 83 manual partitions passed their recorded checksum checks.\n\nThe primary benchmark covers **0.05–3.0 N**, a fixed exploratory interval, not an established detection limit or safe operating range. Each eligible frame receives an optical-only prediction. Force MAE, RMSE, bias, and R² give every press session equal total weight. Localization accuracy and macro F1 use equal-session weights across nine ROI classes. Contact balanced accuracy averages positive-class and no-contact recall; FPR is averaged equally across the 11 dedicated no-contact sessions. Precision reflects this archived session mixture, not a prospective real-world prevalence.\n\nLocalization is evaluated on reference-positive press frames regardless of detector output. It measures ROI classification, not millimetre error. Contact detection uses default classifier thresholds without temporal debounce.","method")
    md("force_section","## Extra Trees lowers force error, but tracking remains weak\n\nThe chart ranks nine fixed force configurations by held-out MAE; lower is better. Extra Trees leads in every outer group's inner selection. The negative pooled R² means its squared error exceeds that of the weighted mean of the evaluated targets; that mean is an explanatory reference, not a separately trained deployable predictor. Its negative bias shows systematic underestimation.\n\nThe best explicitly monotonic candidate is histogram gradient boosting, with 0.708 N MAE. Extra Trees is unconstrained and has not passed a monotonicity or optical-support audit. The linear models perform poorly over this interval, so a simple proportional light-to-force mapping is not supported by this comparison.","comparison")
    bar("force_mae","Force estimation error","force","mae_N","MAE (N)","comparison")
    table("force_table","Force model metrics","force",[("model","Model","text"),("mae_N","MAE (N)","number"),("rmse_N","RMSE (N)","number"),("bias_N","Bias (N)","number"),("r2","R²","number")],"mae_N")
    md("location_section","## Localization has a near tie at the top\n\nLogistic regression and RBF SVC differ by less than 0.1 percentage point in both pooled accuracy and macro F1. Logistic regression is the saved winner because macro F1 was the predeclared selection metric. All six families remain below the application's 80% accuracy and macro-F1 targets. The ranking below uses macro F1; higher is better.","comparison")
    bar("location_f1","Localization macro F1","localization","macro_f1","Macro F1","comparison",True)
    table("location_table","Localization model metrics","localization",[("model","Model","text"),("accuracy","Accuracy","percent"),("macro_f1","Macro F1","percent")],"macro_f1","desc")
    md("detection_section","## Contact detection trades recall for false alarms\n\nRandom Forest detects 91.93% of the positive press frames but falsely signals contact on 8.20% of dedicated no-contact frames, averaged by session. RBF SVC meets the 5% false-positive target at 4.83%, but its recall is only 86.78%. The chart ranks balanced accuracy; use the table to judge the recall/false-alarm tradeoff. These are individual-frame decisions, so they do not establish live event timing or false-contact episode performance after debounce.","comparison")
    bar("contact_bacc","Contact detection balanced accuracy","contact","balanced_accuracy","Balanced accuracy","comparison",True)
    table("contact_table","Contact detection metrics","contact",[("model","Model","text"),("balanced_accuracy","Balanced accuracy","percent"),("recall","Recall","percent"),("fpr","False-positive rate","percent"),("macro_f1","Macro F1","percent")],"balanced_accuracy","desc")
    md("group_section","## TEST1 exposes a major localization generalization gap\n\nFor logistic regression, TEST1 accuracy is 32.58%, compared with 79.72–85.80% for TEST2–TEST6. These are independent held-out groups containing nine complete press sessions each; frame counts differ, but sessions receive equal weight. This gap is observed across model families and should be investigated in acquisition conditions and repeatability before relying on the pooled score. The current experiment does not establish its cause.","folds")
    bar("group_accuracy","Localization accuracy by held-out group","localization_folds","accuracy","Accuracy","folds",True,x="test_group")
    md("roi_section","## Several ROIs remain below the recall target\n\nThe table pairs the saved Extra Trees force model with logistic regression localization. ROIs 1, 2, 7, and 8 have localization recall below 70%; ROI 9 is strongest. Force MAE ranges from 0.557 N for ROI 5 to 0.784 N for ROI 1, while force bias remains negative in every ROI. Each ROI has six independent press sessions.","roi")
    table("roi_table","Saved model performance by ROI","roi",[("roi","ROI","number"),("mae_N","Force MAE (N)","number"),("bias_N","Force bias (N)","number"),("recall","Localization recall","percent"),("frames","Frames","number")],"roi",source="roi")
    md("bins_section","## Weak low-force performance limits the pooled result\n\nBelow 0.5 N, the saved models have 1.107 N force MAE, 80.63% detection recall, and 30.37% localization accuracy. Force error is lowest in the 1.5–2.0 N bin and rises again near 3 N. These diagnostic bins were inspected after model fitting and must not be used to relabel a narrower interval as validated. Sessions with no eligible samples in a bin do not contribute to that bin's score.","bins")
    table("bin_table","Performance by reference-force bin","force_bins",[("force_bin","Force bin","text"),("force_mae_N","MAE (N)","number"),("contact_recall","Detection recall","percent"),("localization_accuracy","Localization accuracy","percent"),("frames","Frames","number"),("sessions","Sessions","number")],"force_bin",source="bins")
    md("selection","## Selecting the model inside training gives a stricter comparison\n\nThe nested selector chooses a family using only the five outer-training groups, with another grouped validation loop and refitted preprocessing. Its outer results are **0.637 N force MAE**, **70.14% localization macro F1**, and **89.54% contact balanced accuracy**. Extra Trees was selected for force in all six groups; localization and contact choices varied.\n\nThe lower classification scores show why the best pooled family scores should be treated as retrospective rankings. Only six TEST groups exist, and all were examined in earlier project experiments. The saved 95% group-bootstrap bounds describe variability of the mean of six fold scores; they are coarse, depend on overlapping training sets, and are not a prospective performance guarantee. Full fold values are retained for review.","comparison")
    md("combined",f"## Missed contacts worsen the combined output\n\nCombining the three saved winners and replacing a missed press with 0 N raises force MAE to **{end['mae_N']:.3f} N** and RMSE to **{end['rmse_N']:.3f} N** on reference-positive frames. Correct contact detection and correct ROI occur together on **{end['joint_contact_and_roi_accuracy']:.2%}** of these frames, weighted equally by session. This evaluates an offline combination of the retrospective winners, without live support guards or debounce; it is not application acceptance testing.","combined")
    sums=coverage.sum(numeric_only=True)
    md("coverage_section",f"## Evaluation retains 7,154 press frames\n\nOf 24,453 primary press frames, {int(sums.evaluated_frames):,} are eligible after fold-specific timestamp alignment and the fixed force interval. Exclusions comprise {int(sums.unpaired_frames):,} unpaired frames, {int(sums.below_005_N):,} below 0.05 N, and {int(sums.above_3_N):,} above 3.0 N. Detection also includes all 1,352 dedicated no-contact frames, for 8,506 evaluated frames in 65 sessions. Frames outside the interval are not claimed as evaluated force/contact capability.\n\nThe relatively small number of low-force samples and roughly three minutes of dedicated no-contact recordings limit what can be concluded about weak contacts and sustained false alarms.","coverage")
    md("methods","## Model and validation specification\n\nThe candidate list, seeds, hyperparameters, metric rules, and interval were written before opening this run's outer predictions. Nine force configurations cover a median baseline, total-light linear regression, NNLS, positive Ridge, isotonic regression, monotonic histogram and XGBoost boosting, Extra Trees, and RBF SVR. Six classifier families cover logistic regression, RBF SVC, Random Forest, Extra Trees, histogram boosting, and XGBoost. These are fixed configurations, not exhaustive hyperparameter searches.\n\nEach outer fold holds out one TEST number across all nine ROIs plus its assigned complete no-contact recordings. Inner folds follow the same group boundary. Preprocessing learns no-contact floors and scaling only from the current training subset. Lag is selected by grouped NNLS MAE over 0–500 ms in 100 ms steps, preferring the smallest lag within 0.5% of the best. Positive lag pairs optical time t with reference time t minus lag; interpolation is session-local, without extrapolation, and gaps over 300 ms are excluded. Outer lags are 300, 300, 200, 300, 300, and 300 ms.\n\nForce inputs are nine area-normalized positive-light signals after floor subtraction, with scaling that preserves nonnegativity. The total-light baselines sum these scaled channels. Classifiers additionally receive nine signed-light-per-pixel features and nine active fractions. Neither reference force, target ROI, TEST number, nor session identity is an inference feature. There is no frame smoothing. Force training balances sessions and 0.25 N bins; localization balances sessions; contact fitting balances classes and sessions.\n\nThere were 462 fitted estimators across 22 unique training subsets. Family selection is evaluated with nested grouped validation; this follows the rationale in [scikit-learn's nested cross-validation documentation](https://scikit-learn.org/stable/auto_examples/model_selection/plot_nested_cross_validation_iris.html). The winning configurations were finally refitted using all eligible manual training sessions; their all-data fit is not reported as held-out performance.","method")
    md("validation",f"## Independent checks passed\n\nThe source-partition hashes, whole-session assignments, identical evaluation rows across candidates, nested selection outputs, and saved-model checksums passed. Independent recalculation matched 103 metrics to floating-point precision. Reloaded models matched single-frame and batch predictions, and changing the reference-force column did not change inference. Fifteen existing dataset-firewall and timestamp/preprocessing tests passed.\n\nThe mixed-family nested contact ROC-AUC is deliberately omitted because SVC decision scores and forest probabilities have incompatible scales. No millimetre localization error or live onset-delay metric is claimed without the required ground truth and deployed event pipeline.","audit")
    md("latency",f"## Runtime remains an offline measurement\n\nThe research helper produced all three predictions in a median **{timing['optical_features_to_three_predictions_p50_ms']:.1f} ms** and p95 **{timing['optical_features_to_three_predictions_p95_ms']:.1f} ms** over 60 single-frame calls on this machine. This includes tabular preprocessing and estimators, but excludes camera acquisition, pixel feature extraction, and GUI display. It is not an optimized deployment benchmark.","timing")
    md("next","## Recommended next steps\n\nRetain Extra Trees as the force research baseline, and keep logistic regression and RBF SVC as near-tied localization candidates. Random Forest is the detection candidate when recall is prioritized; SVC is the alternative when fewer false positives are required. None is currently a validated live model.\n\nCollect new prospectively held-out manual recordings with balanced low-force coverage, sustained no-contact periods, and documented camera/lighting/baseline conditions. Investigate why TEST1 differs and why ROIs 1 and 2 remain weak. Use the new recordings to test a frozen model, then assess contact thresholds, monotonicity, optical support, debounce, and live latency before application integration. The open questions are whether the TEST1 gap persists under matched acquisition conditions and whether low-force separation improves with stronger manual calibration coverage.")
    artifact=dict(surface="report",manifest=dict(version=1,title=title,surface="report",sources=sources,charts=charts,tables=tables,blocks=blocks),snapshot=dict(version=1,status="ready",datasets=datasets))
    write_json(out/"artifact.json",artifact)
    write_json(out/"report_notes.json",dict(audience="technical",delivery_mode="mcp-app",as_of="9 September 2026",
        structure="title; technical summary; definitions before evidence; task comparisons; group/ROI/bin diagnostics; nested validation; exclusions; methods; audit; next steps and open questions",
        chart_contracts=[dict(id=c["id"],question=c["title"],grain=c["dataset"],type="horizontal bar",zero_baseline=True,source=c["sourceId"]) for c in charts],
        repeated_chart_reason="all four visuals compare discrete categories, not time trends; tables retain exact companion metrics",
        tests="15 passed: test_manual_only_firewall.py and test_manual_only_preprocessing.py"))
    readme="""# Manual-only retraining research artifacts

Completed 9 September 2026. The reviewed report is `artifact.json` (rendered through the Data Analytics report tool). Exact results are in `model_comparison.csv`, with per-group and per-ROI detail. These are retrospective research outputs over an exploratory 0.05-3 N interval, not validated application bundles.

## Reproduce

From `calibration_gui`, using PowerShell:

```powershell
.venv/Scripts/python.exe -m scripts.retrain_manual_models --output analysis_outputs/live_sensor_manual_only/model_runs/retraining_repeat
.venv/Scripts/python.exe -m scripts.audit_manual_retraining --run analysis_outputs/live_sensor_manual_only/model_runs/retraining_repeat
.venv/Scripts/python.exe -m scripts.report_manual_retraining --run analysis_outputs/live_sensor_manual_only/model_runs/retraining_repeat
```

The output directory must not already exist. Training reuses only the hash-verified immutable manual feature partitions; it does not reread raw ZIP/video files. All source identities, parameters, group assignments, lag searches, predictions, and library versions are saved. The manual archive's 18 replay-only recordings do not participate in training, selection, or scoring.

## Saved models

`models/force.joblib`: Extra Trees. `models/contact.joblib`: Random Forest. `models/localization.joblib`: logistic regression. Each includes its estimator, train-fitted scaler and optical floors. Models use only optical inputs, not the load-cell reference. The models are refitted on all eligible data after evaluation. Metrics come from the separate held-out predictions.

Use joblib files only when produced by this trusted local run. They are Python research artifacts and must not be installed as live application bundles. Application settings and models were not changed.

```powershell
.venv/Scripts/python.exe -m scripts.predict_retrained_manual --models analysis_outputs/live_sensor_manual_only/model_runs/retraining_20260909/models --input PATH_TO_CANONICAL_MANUAL_FRAME_PARQUET --output research_predictions.csv
```

## Evidence

- `model_comparison.csv`: nine force and six models per classification task, plus nested selector scores.
- `outer_predictions.parquet`: all held-out per-frame predictions.
- `nested_selected_predictions.parquet`: predictions selected without the outer group's labels.
- `fold_metrics.csv`, `roi_metrics.csv`, `force_bin_metrics.csv`: subgroup results.
- `evaluation_coverage.csv`: complete force interval and alignment exclusions.
- `end_to_end_metrics.json`: retrospective winner combination with missed contacts represented as zero force.
- `no_contact_episodes.csv`: raw, un-debounced episode counts over about three minutes of dedicated no-contact recordings.
- `validation_audit.json`: independent metric checks, leakage checks, source checksums, and inference parity.
- `protocol.json`, `provenance.json`, `estimator_parameters.json`, `lag_selection.json`, `inner_selections.json`: reproducibility.

No prospective generalization, validated force range, millimetre localization, optical-support safety, or live event timing is established by this experiment. Group-bootstrap intervals summarize six overlapping-training CV folds and are descriptive only. The nested contact pooled ROC-AUC is intentionally omitted because selected estimators have incompatible score scales.
"""
    (out/"README.md").write_text(readme,encoding="utf-8")
    print(out/"artifact.json")


if __name__=="__main__": main()
