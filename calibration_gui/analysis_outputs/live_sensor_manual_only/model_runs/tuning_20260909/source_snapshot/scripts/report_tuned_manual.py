"""Create the audited before/after report and model cards for manual tuning."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import pandas as pd
from scripts.tune_manual_models import DEFAULT_OUT, BASE_RUN, write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--run",type=Path,default=DEFAULT_OUT)
    args=parser.parse_args();out=args.run
    comp=pd.read_csv(out/"model_comparison.csv");old=pd.read_csv(BASE_RUN/"model_comparison.csv")
    folds=pd.read_csv(out/"fold_metrics.csv");oldfolds=pd.read_csv(BASE_RUN/"fold_metrics.csv")
    protocol=json.loads((out/"protocol.json").read_text());audit=json.loads((out/"validation_audit.json").read_text())
    assert audit["status"]=="passed"
    winners=json.loads((BASE_RUN/"winners.json").read_text())
    e2e=json.loads((out/"end_to_end_metrics.json").read_text());oldend=json.loads((BASE_RUN/"end_to_end_metrics.json").read_text())
    selected={task:comp[(comp.task==task)&(comp.model=="Tuned selected")].iloc[0] for task in winners}
    baseline={task:old[(old.task==task)&(old.model==name)].iloc[0] for task,name in winners.items()}
    frame_only={task:comp[(comp.task==task)&(comp.model=="Tuned frame-only")].iloc[0] for task in winners}
    datasets={}
    for task in winners:
        rows=comp[comp.task==task].copy()
        base=baseline[task].to_frame().T.copy();base["model"]="Previous "+winners[task]
        previous_nested=old[(old.task==task)&(old.model=="Nested selected")].copy();previous_nested["model"]="Previous nested selector"
        datasets[task]=pd.concat([base,previous_nested,rows],ignore_index=True).to_dict("records")
        datasets[task+"_overview"]=[dict(pipeline=label,**{k:float(r[k]) for k in ("mae_N","rmse_N","bias_N","r2") if task=="force"}) if task=="force" else
            dict(pipeline=label,**{k:float(r[k]) for k in ("accuracy","macro_f1","balanced_accuracy")},**({k:float(r[k]) for k in ("recall","fpr","precision")} if task=="contact" else {}))
            for label,r in (("Previous best",baseline[task]),("Tuned",selected[task]),("Current frame",frame_only[task]))]
    datasets["bins"]=pd.read_csv(out/"force_bin_metrics.csv").to_dict("records")
    roi=pd.read_csv(out/"roi_metrics.csv")
    datasets["roi"]=roi[roi.task=="force"][["roi","mae_N","rmse_N","bias_N","frames"]].merge(
        roi[roi.task=="localization"][["roi","recall"]],on="roi",validate="one_to_one").to_dict("records")
    group_rows=[]
    for label,df,model in (("Previous",oldfolds,winners["localization"]),("Tuned",folds,"Tuned selected"),("Current frame",folds,"Tuned frame-only")):
        for _,r in df[(df.task=="localization")&(df.model==model)].iterrows():
            group_rows.append(dict(test_group="TEST"+str(int(r.outer_fold)),pipeline=label,accuracy=r.accuracy))
    datasets["groups"]=group_rows
    parameter_rows=[]
    for directory,label in (("models","Causal tuning"),("models_frame_only","Current-frame tuning")):
        manifest=json.loads((out/directory/"manifest.json").read_text())
        for name in manifest["models"]:
            spec=json.loads((out/directory/f"{name}.json").read_text())
            for m in spec["members"]:
                config=next(c for c in protocol["candidates"] if c["id"]==m["choice"]["id"])
                parameter_rows.append(dict(pipeline=label,task=name,family=config["family"],features=config["features"],
                    smoothing_s=m["choice"]["tau"],configuration=json.dumps(config["params"]),
                    offset_N=m["choice"].get("offset"),threshold=m["choice"].get("threshold")))
    datasets["parameters"]=parameter_rows
    datasets["overview"]=pd.read_csv(out/"before_after.csv").to_dict("records")
    datasets["combined"]=[dict(version=label,**value) for label,value in (("Previous",oldend),("Tuned",e2e))]
    datasets["method"]=[dict(item=k,detail=json.dumps(v)) for k,v in protocol.items() if k!="candidates"]
    datasets["method"] += [dict(item="Candidate "+c["id"],detail=json.dumps(c)) for c in protocol["candidates"]]
    datasets["audit"]=[dict(check=k,result=json.dumps(v)) for k,v in audit.items()]
    datasets["timing"]=[json.loads((out/"inference_timing.json").read_text())]
    # Materialize the audited evidence and execute each visible table's query.
    queries={}
    with sqlite3.connect(out/"report_evidence.sqlite") as connection:
        for name,rows in datasets.items():
            pd.DataFrame(rows).to_sql(name,connection,index=False,if_exists="replace")
            query=f'SELECT * FROM main."{name}"';queries[name]=query
            datasets[name]=pd.read_sql_query(query,connection).to_dict("records")
    sources=[]
    for name in datasets:
        sources.append(dict(id=name,label="Audited manual calibration tuning results",path="report_evidence.sqlite",
            query=dict(engine="sqlite",language="sql",sql=queries[name],tables_used=[f"main.{name}"],
                description="Executed retrieval from audited original and tuned manual-only prediction metrics. Source CSVs, fold predictions, protocols and model files are retained beside this evidence snapshot.",
                filters=["Manual Calibration (2).zip only","Whole TEST groups held out","Same 0.05-3 N primary rows and dedicated no-contact rows as previous run"],
                metric_definitions=dict(mae_N="Equal-session mean absolute force error in N",rmse_N="Root equal-session mean squared error in N",
                    accuracy="Equal-session fraction correct",macro_f1="Mean class F1 with equal-session sample weights",
                    balanced_accuracy="Mean contact/non-contact recall",fpr="Equal-session fraction of dedicated no-contact frames classified positive"))))
    charts=[];tables=[];blocks=[]
    def md(id,body,source=None):
        b=dict(id=id,type="markdown",body=body)
        if source:b["sourceId"]=source
        blocks.append(b)
    def chart(id,title,dataset,x,y,ylabel,percent=False,color=None):
        enc=dict(x=dict(field=x,type="nominal",label="Pipeline" if x=="pipeline" else "TEST group"),
            y=dict(field=y,type="quantitative",label=ylabel,format="percent" if percent else "number"))
        if color:enc["color"]=dict(field=color,type="nominal",label="Pipeline")
        charts.append(dict(id=id,title=title,type="bar",dataset=dataset,sourceId=dataset,encodings=enc,
            options=dict(showValues=True,grouping="grouped"),valueFormat="percent" if percent else "number"))
        blocks.append(dict(id=id+"_block",type="chart",chartId=id,layout="full"))
    def table(id,title,dataset,columns,sort,desc=False):
        tables.append(dict(id=id,title=title,dataset=dataset,sourceId=dataset,
            columns=[dict(field=k,label=label,format=fmt) for k,label,fmt in columns],
            defaultSort=dict(field=sort,direction="desc" if desc else "asc")))
        blocks.append(dict(id=id+"_block",type="table",tableId=id,layout="full"))
    title="Manual calibration model tuning: before and after"
    md("title",f"# {title}")
    f,l,c=selected["force"],selected["localization"],selected["contact"]
    bf,bl,bc=baseline["force"],baseline["localization"],baseline["contact"]
    md("summary",f"## Technical summary\n\n**Force:** MAE {bf.mae_N:.3f} → **{f.mae_N:.3f} N**, RMSE {bf.rmse_N:.3f} → **{f.rmse_N:.3f} N**, and R² {bf.r2:.3f} → **{f.r2:.3f}**.\n\n**Nine-ROI localization:** accuracy {bl.accuracy:.2%} → **{l.accuracy:.2%}**, macro F1 {bl.macro_f1:.2%} → **{l.macro_f1:.2%}**.\n\n**Contact detection:** balanced accuracy {bc.balanced_accuracy:.2%} → **{c.balanced_accuracy:.2%}**, recall {bc.recall:.2%} → **{c.recall:.2%}**, and false positives {bc.fpr:.2%} → **{c.fpr:.2%}**.\n\nThese compare the previous best fixed models with a new pipeline whose model, features and postprocessing are selected inside each training fold. All scores use the same held-out frames and targets. The source remains manual calibration only. These are retrospective development results, with no unused prospective test set.","overview")
    md("evaluation","## Evaluation and interpretation\n\nThe benchmark retains **7,154 press frames from 54 press recordings** for force and localization, and adds **1,352 frames from 11 dedicated no-contact recordings** for detection. The manual archive's 18 replay-only recordings are excluded. Whole TEST groups and assigned no-contact recordings are held out across six outer folds. All preprocessing is fitted within training groups, and the previous fold-local timestamp lags and the **0.05–3 N** interval remain unchanged. Metrics give sessions equal weight.\n\nThe causal pipeline can use earlier optical frames, including earlier portions of the same held-out recording. It never uses future frames, force labels, target ROI, recording duration, or TEST/session identity as a model feature. ROI is stationary within each recording, so temporal improvements do not establish accuracy during rapid movement between ROIs. The **Current frame** comparison uses neither temporal input features nor output smoothing.","force")
    md("force_section",f"## Force estimation\n\nThe tuned selection changes force MAE by {f.mae_N-bf.mae_N:+.3f} N and bias from {bf.bias_N:.3f} to {f.bias_N:.3f} N. Current-frame-only tuning obtains {frame_only['force'].mae_N:.3f} N MAE. Lower MAE and RMSE are better; bias closer to zero and larger R² are better. The table includes each independently tuned model family and the previous nested selector.","force")
    chart("force_chart","Force mean absolute error","force_overview","pipeline","mae_N","MAE (N)")
    table("force_table","Force model comparison","force",[("model","Model or selection procedure","text"),("mae_N","MAE (N)","number"),("rmse_N","RMSE (N)","number"),("bias_N","Bias (N)","number"),("r2","R²","number")],"mae_N")
    md("localization_section",f"## Localization\n\nThe tuned selector reaches {l.accuracy:.2%} accuracy and {l.macro_f1:.2%} macro F1 across all nine ROIs. Current-frame tuning reaches {frame_only['localization'].accuracy:.2%} accuracy and {frame_only['localization'].macro_f1:.2%} macro F1. These are ROI classification metrics; calibrated continuous spatial ground truth is unavailable, so no millimetre error is reported.","localization")
    chart("localization_chart","Localization macro F1","localization_overview","pipeline","macro_f1","Macro F1",True)
    table("localization_table","Localization model comparison","localization",[("model","Model or selection procedure","text"),("accuracy","Accuracy","percent"),("macro_f1","Macro F1","percent")],"macro_f1",True)
    constrained=comp[(comp.task=="contact")&(comp.model=="Tuned FPR <=5%")].iloc[0]
    md("contact_section",f"## Contact detection and false alarms\n\nThe main pipeline chooses its threshold by inner-validation balanced accuracy. Its held-out recall is {c.recall:.2%}, FPR is {c.fpr:.2%}, precision is {c.precision:.2%}, and macro F1 is {c.macro_f1:.2%}. A separate policy chooses a threshold with inner FPR ≤5%; its **actual held-out** recall is {constrained.recall:.2%} and FPR is {constrained.fpr:.2%}. The constraint applies during training and does not guarantee a 5% FPR on new recordings.\n\nPrecision depends on the archive's contact/no-contact mix. These frame metrics do not measure deployed onset delay or sustained false-contact episodes after an application debounce layer.","contact")
    chart("contact_chart","Contact detection balanced accuracy","contact_overview","pipeline","balanced_accuracy","Balanced accuracy",True)
    table("contact_table","Contact detection model comparison","contact",[("model","Model or selection procedure","text"),("balanced_accuracy","Balanced accuracy","percent"),("recall","Recall","percent"),("fpr","False positives","percent"),("precision","Precision","percent"),("macro_f1","Macro F1","percent")],"balanced_accuracy",True)
    md("groups_section","## Performance across recording groups\n\nThe previous experiment's weakest localization group was TEST1, whose raw brightness distribution differed from the other groups. The grouped comparison retains TEST1 and every other group. Differences here describe performance across acquisition groups; the experiment does not establish which acquisition condition caused the original gap.","groups")
    chart("groups_chart","Localization accuracy by TEST group","groups","test_group","accuracy","Accuracy",True,"pipeline")
    md("roi_section","## ROI and force-bin diagnostics\n\nThe following diagnostics describe the tuned causal selector. Every ROI has six press recordings. Force bins with no eligible samples in a recording do not include that recording in the bin's average. Bins are diagnostic and do not redefine a validated operating range.","roi")
    table("roi_table","Tuned performance by ROI","roi",[("roi","ROI","number"),("mae_N","Force MAE (N)","number"),("bias_N","Force bias (N)","number"),("recall","Localization recall","percent"),("frames","Frames","number")],"roi")
    table("bins_table","Tuned performance by force bin","bins",[("force_bin","Force bin","text"),("force_mae_N","MAE (N)","number"),("contact_recall","Contact recall","percent"),("localization_accuracy","Localization accuracy","percent"),("frames","Frames","number")],"force_bin")
    md("combined",f"## Combined predictions\n\nWhen a missed contact produces zero force, combined force MAE changes from {oldend['mae_N']:.3f} to **{e2e['mae_N']:.3f} N** and RMSE from {oldend['rmse_N']:.3f} to **{e2e['rmse_N']:.3f} N**. Correct contact and correct ROI occur together on **{e2e['joint_contact_and_roi_accuracy']:.2%}** of positive frames, compared with {oldend['joint_contact_and_roi_accuracy']:.2%} previously. Sessions retain equal weight. This is an offline combination of research models.","force")
    counts=Counter(x["task"] for x in protocol["candidates"])
    md("search",f"## Search design\n\nThe frozen grid contains **{len(protocol['candidates'])} configurations**: {counts['force']} force, {counts['localization']} localization and {counts['contact']} contact candidates. Fifteen unique four-group training subsets support all six nested evaluations, for **{len(protocol['candidates'])*15:,} inner model fits**, followed by the selected outer fits and final refits. Trees, boosting, support-vector models, logistic regression and neural networks were tested where applicable. Tree depth/leaf size, regularization, kernel settings, and feature packs vary.\n\nFeature packs range from nine positive-light channels to signed light, active fractions, normalized spatial patterns, pixel medians/variation, and local light centroids. The causal variant adds exponential histories with 0.3 s and 1.0 s time constants. Output smoothing uses 0, 0.3 or 1.0 s. Force candidates compare equal-session training with the former session/bin weighting, and train-only residual offsets. Ensembles average the three best tuned families when that improves inner validation.\n\nThresholds, corrections, features and models are selected entirely inside the outer training groups. The procedure follows [nested cross-validation](https://scikit-learn.org/stable/auto_examples/model_selection/plot_nested_cross_validation_iris.html) and [training-only threshold tuning](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TunedThresholdClassifierCV.html). Final artifacts are refitted on all eligible manual training data after a complete leave-two-groups-out training search. Their training fit is not presented as held-out accuracy.","parameters")
    table("parameters_table","Saved model configurations","parameters",[("pipeline","Pipeline","text"),("task","Task","text"),("family","Family","text"),("features","Features","text"),("smoothing_s","Smoothing (s)","number"),("configuration","Hyperparameters","text")],"task")
    md("validation",f"## Verification and limits\n\nIndependent calculations matched **{audit['metric_checks']} metrics**. The audit verified identical held-out frame IDs and reference targets, manual feature-partition hashes, complete-session splits, saved model checksums, and a refit that reproduced TEST1 predictions for all three primary tasks. Reloaded inference passed force/target-label invariance, causal-prefix, future-frame perturbation and session-reset checks. Twenty pipeline and existing firewall/preprocessing tests passed.\n\nThis development reused manual recordings already examined in previous experiments, so nested evaluation controls this search's fitting leakage but does not create a new prospective test set. Six groups provide limited uncertainty information; paired group-bootstrap results are saved in `paired_uncertainty.json` and depend on overlapping CV training sets. The original scientific operating-range and live application gates have not been revalidated. No live GUI bundle was replaced.","force")
    md("next","## Recommended use\n\nUse the saved models as the next manual-only research candidates. Review the current-frame results when response latency matters, and use the causal variant only with its documented history/reset behavior. Preserve this model version for prospective manual recordings that include low-force contacts, sustained no-contact periods, changed lighting, and movement between ROIs. Those recordings are needed to establish live accuracy, false alarms, response delay and operating range.")
    # Bind narrative claims to the corresponding evidence datasets.
    for block in blocks:
        if block["id"]=="evaluation":block["sourceId"]="method"
        elif block["id"]=="combined":block["sourceId"]="combined"
        elif block["id"]=="search":block["sourceId"]="method"
        elif block["id"]=="validation":block["sourceId"]="audit"
    artifact=dict(surface="report",manifest=dict(version=1,title=title,surface="report",sources=sources,charts=charts,tables=tables,blocks=blocks),snapshot=dict(version=1,status="ready",datasets=datasets))
    write_json(out/"artifact.json",artifact)
    write_json(out/"report_notes.json",dict(delivery_mode="mcp-app",audience="technical",as_of="9 September 2026",
        source="Hash-verified manual-only feature store and audited same-row before/after predictions",
        chart_contracts=[dict(id=c["id"],question=c["title"],grain=c["dataset"],zero_baseline=True,units=c["encodings"]["y"]["label"]) for c in charts]))
    lines=["# Manual-only model tuning", "", "Audited retrospective results using the same held-out recordings and 0.05–3 N frame set as the original experiment.", "",
        "| Task / metric | Previous best | Tuned selection | Current frame only |", "|---|---:|---:|---:|"]
    for task,metrics in (("force",["mae_N","rmse_N","bias_N","r2"]),("localization",["accuracy","macro_f1"]),("contact",["balanced_accuracy","recall","fpr","precision","macro_f1"])):
        for metric in metrics:
            vals=[baseline[task][metric],selected[task][metric],frame_only[task][metric]]
            formatted=[f"{v:.4f}" if task=="force" else f"{v:.2%}" for v in vals]
            lines.append(f"| {task}: {metric} | "+" | ".join(formatted)+" |")
    lines += ["", "## Reproduce", "", "Run from `calibration_gui` in the pinned `.venv`. The canonical imported entry point avoids Windows process-serialization issues:", "", "```powershell",
        '.venv/Scripts/python.exe -u -c "from scripts.tune_manual_models import main; main()" --jobs 8 --output analysis_outputs/live_sensor_manual_only/model_runs/tuning_repeat',
        '.venv/Scripts/python.exe -m scripts.evaluate_manual_frame_only --run analysis_outputs/live_sensor_manual_only/model_runs/tuning_repeat',
        '.venv/Scripts/python.exe -m scripts.audit_tuned_manual --run analysis_outputs/live_sensor_manual_only/model_runs/tuning_repeat',
        '.venv/Scripts/python.exe -m scripts.report_tuned_manual --run analysis_outputs/live_sensor_manual_only/model_runs/tuning_repeat',
        "```", "", "## Saved inference", "", "`models/` contains the causal tuned selector and alternate contact FPR policy. `models_frame_only/` contains models requiring only the current frame. Manifests and task JSON files identify exact families, parameters, features, corrections, smoothing, thresholds, checksums and training recordings.", "",
        "Use `scripts.predict_tuned_manual.infer_sequence` with trusted local models loaded by `load_models`. For causal models provide each complete recording in chronological order with original optical timestamps. History resets at a new recording or a gap above 300 ms. Each prediction uses only current/past optical data. All optical spatial columns are required; force labels and target ROI are unused.", "",
        "## Interpretation", "", "The main comparison is the nested tuning procedure, whose selected family can differ by fold. Family rows are nested tuned family procedures; their ranking across outer results is retrospective. The final full-data models are selected using all 15 leave-two-groups-out training splits. All-data fitted performance is not a test result.", "",
        "Only the manual calibration archive was used; 18 replay-only recordings are excluded. The force interval remains exploratory, not validated. Temporal smoothing can delay responses, and the archived ROI stays constant within each recording. No continuous-position error or deployed onset delay is claimed. The live application model bundles are unchanged.", "",
        "The full report is `artifact.json`; exact model and baseline metrics are `model_comparison.csv` and `before_after.csv`. Audit evidence, per-group/ROI/bin scores, all inner trials, cached predictions, frozen protocols, source hashes and paired uncertainty estimates are retained here."]
    lines += ["", "For one-frame-at-a-time use, create `StreamingResearchPredictor(load_models(model_directory))` once and call `predict_one` for each ordered feature row. Preserve the object between frames; call `reset` at the start of a new recording. The audit compares this stateful path against full-sequence inference. Offline timing is recorded in `inference_timing.json`."]
    (out/"README.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps(dict(report=str(out/"artifact.json"),charts=len(charts),tables=len(tables),blocks=len(blocks))))


if __name__=="__main__":main()
