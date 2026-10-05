"""Verify saved outer predictions and integrate model evidence into characterization.

Run after global_sensor_characterization.py; this does not retrain or alter models.
The physical press analysis and the retrospective frame evaluation retain separate
responses, denominators, weights, and force intervals throughout.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import shutil
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNS = ROOT / 'calibration_gui/analysis_outputs/live_sensor_manual_only/model_runs'
DEFAULT_PDF = Path(r'C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\Manual_Calibration_Models_Technical_Report.pdf')
WINNERS = {'force': 'Extra Trees', 'localization': 'Logistic regression', 'contact': 'Random Forest'}

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def weights(df):
    w = 1 / df.groupby('session_id').session_id.transform('size').to_numpy(float)
    return w / w.sum()

def metrics(df, task):
    y, p, w = df.truth.to_numpy(), df.prediction.to_numpy(), weights(df)
    if task == 'force':
        e = p - y
        return dict(mae_N=np.sum(w*abs(e)), rmse_N=np.sqrt(np.sum(w*e**2)),
                    bias_N=np.sum(w*e), r2=1-np.sum(w*e**2)/np.sum(w*(y-np.sum(w*y))**2))
    labels = np.arange(2) if task == 'contact' else np.arange(1,10)
    cm = np.array([[w[(y==a)&(p==b)].sum() for b in labels] for a in labels])
    diag, support, predicted = np.diag(cm), cm.sum(axis=1), cm.sum(axis=0)
    recall = np.divide(diag,support,out=np.zeros(len(labels)),where=support!=0)
    f1 = np.divide(2*diag,support+predicted,out=np.zeros(len(labels)),where=support+predicted!=0)
    result = dict(accuracy=np.trace(cm),balanced_accuracy=recall.mean(),macro_f1=f1.mean())
    if task == 'contact':
        result.update(recall=recall[1],specificity=recall[0],fpr=1-recall[0],
                      precision=diag[1]/predicted[1],positive_f1=f1[1])
    return result

def mdtable(df):
    rows = [df.columns.tolist()] + df.astype(str).values.tolist()
    return '\n'.join(['| '+' | '.join(rows[0])+' |','| '+' | '.join(['---']*len(rows[0]))+' |']+
                     ['| '+' | '.join(row)+' |' for row in rows[1:]])

def integrate(out=ROOT/'outputs', runs=DEFAULT_RUNS, pdf=DEFAULT_PDF):
    out, runs, pdf = Path(out), Path(runs), Path(pdf)
    here, tables = out/'global_characterization', out/'tables'
    evidence = here/'model_report_review/evidence'
    evidence.mkdir(parents=True,exist_ok=True)
    core = json.loads((here/'analysis_results.json').read_text())
    manifest, audits, results, selected_rows = {}, [], {}, []
    raw = {}
    for version, dirname in [('fixed','retraining_20260909'),('tuned','tuning_20260909')]:
        directory = runs/dirname
        predictions = pd.read_parquet(directory/'outer_predictions.parquet')
        comparison = pd.read_csv(directory/'model_comparison.csv')
        provenance = json.loads((directory/'provenance.json').read_text())
        assert provenance['feature_report']['archive_hashes_after']['manual'] == core['archive_sha256']
        manifest[str(directory/'outer_predictions.parquet')] = sha(directory/'outer_predictions.parquet')
        results[version], raw[version] = {}, {}
        for task in WINNERS:
            model = 'Tuned selected' if version == 'tuned' else WINNERS[task]
            df = predictions.loc[predictions.task.eq(task)&predictions.model.eq(model)].copy()
            assert not df.frame_uid.duplicated().any()
            assert df.groupby('session_id').outer_fold.nunique().max() == 1
            assert df.outer_fold.nunique() == 6
            if task != 'contact':
                assert len(df)==7154 and df.session_id.nunique()==54
                assert df.reference_force_N.between(.05,3).all()
            else:
                assert len(df)==8506 and df.session_id.nunique()==65
                assert df.loc[df.truth.eq(0)].session_id.nunique()==11
            got = metrics(df,task)
            saved = comparison.loc[comparison.task.eq(task)&comparison.model.eq(model)].iloc[0]
            for key,val in got.items():
                if key in saved and pd.notna(saved[key]):
                    discrepancy=abs(float(saved[key])-val)
                    audits.append(dict(version=version,task=task,metric=key,recomputed=val,saved=float(saved[key]),absolute_difference=discrepancy))
                    assert discrepancy < 1e-12,(task,key,discrepancy)
            results[version][task] = dict(**got,frames=len(df),recordings=df.session_id.nunique(),groups=6)
            raw[version][task]=df
            df['comparison_version']=version
            selected_rows.append(df)
        for name in ['model_comparison.csv','force_bin_metrics.csv','roi_metrics.csv','fold_metrics.csv',
                     'end_to_end_metrics.json','provenance.json','protocol.json']:
            source=directory/name
            target=evidence/f'{version}_{name}'
            shutil.copyfile(source,target)
            manifest[str(source)]=sha(source)
    for task in WINNERS:
        a,b=raw['fixed'][task].set_index('frame_uid'),raw['tuned'][task].set_index('frame_uid')
        assert set(a.index)==set(b.index)
        assert np.allclose(a.truth,b.loc[a.index].truth,rtol=0,atol=0)
        assert (a.session_id==b.loc[a.index].session_id).all()
    # Recompute the combined system score by joining the same held-out frame IDs.
    for version in raw:
        f=raw[version]['force'].set_index('frame_uid')
        c=raw[version]['contact'].set_index('frame_uid').loc[f.index]
        l=raw[version]['localization'].set_index('frame_uid').loc[f.index]
        joint=float(np.sum(weights(f)*(c.prediction.eq(1)&l.prediction.eq(l.truth)).to_numpy()))
        gated=f.copy();gated['prediction']=f.prediction*c.prediction
        combined=dict(**metrics(gated,'force'),joint_contact_and_roi_accuracy=joint)
        saved=json.loads((runs/('tuning_20260909' if version=='tuned' else 'retraining_20260909')/'end_to_end_metrics.json').read_text())
        for key,val in combined.items():
            assert abs(val-saved[key])<1e-12,(version,key,val,saved[key])
            audits.append(dict(version=version,task='combined',metric=key,recomputed=val,saved=saved[key],absolute_difference=abs(val-saved[key])))
        results[version]['combined']=combined
    timing=json.loads((runs/'tuning_20260909/inference_timing.json').read_text())
    manifest[str(runs/'tuning_20260909/inference_timing.json')]=sha(runs/'tuning_20260909/inference_timing.json')
    shutil.copyfile(runs/'tuning_20260909/inference_timing.json',evidence/'inference_timing.json')
    assert pdf.exists()
    manifest[str(pdf)]=sha(pdf)
    shutil.copyfile(pdf,evidence/'Manual_Calibration_Models_Technical_Report.pdf')
    pd.concat(selected_rows).to_csv(tables/'model_assisted_outer_predictions.csv',index=False)
    pd.DataFrame(audits).to_csv(tables/'model_assisted_metric_validation.csv',index=False)
    rows=[]
    for version,tasks in results.items():
        for task,data in tasks.items():
            for metric,value in data.items():
                if metric in ['frames','recordings','groups']:continue
                rows.append(dict(procedure=version,task=task,metric=metric,value=value,
                                 units='N' if metric.endswith('_N') else 'proportion',
                                 frames=data.get('frames',7154),recordings=data.get('recordings',54),groups=6,
                                 scope='Retrospective outer-frame evaluation; equal recording weights; press force 0.05-3 N'))
    metric_table=pd.DataFrame(rows)
    metric_table.to_csv(tables/'model_assisted_performance.csv',index=False)
    results['timing']=timing
    results['scope']={'observation':'frame','weighting':'equal recording weights','outer_groups':6,
                     'press_force_interval_N':[.05,3],'primary_press_recordings':54,'excluded_replay_recordings':18,
                     'eligible_press_frames':7154,'dedicated_no_contact_recordings':11,'no_contact_frames':1352,
                     'image_source':'original session_video.mp4','status':'retrospective offline research evaluation; not deployed'}
    (here/'model_assisted_results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    validation=dict(status='PASS',metrics_recomputed=len(audits),maximum_absolute_difference=max(x['absolute_difference'] for x in audits),
                    paired_frame_ids_and_truth_equal=True,one_outer_fold_per_recording=True,
                    manual_archive_sha256=core['archive_sha256'],source_hashes=manifest,
                    scope='Independent arithmetic verification of saved outer predictions; no retraining or prospective validation')
    (here/'model_report_validation.json').write_text(json.dumps(validation,indent=2),encoding='utf-8')
    make_figures(out,results,core)
    update_text(here,tables,results)
    print(json.dumps({k:v for k,v in validation.items() if k!='source_hashes'},indent=2))
    return results

def make_figures(out,res,core):
    import os
    cache=out/'global_characterization/.matplotlib'
    cache.mkdir(exist_ok=True)
    os.environ.setdefault('MPLCONFIGDIR',str(cache.resolve()))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from global_sensor_characterization import configure_plots,savefig
    configure_plots()
    figure_dir=out/'figures'
    blue,gray='#285A77','#BEC9CF'
    fig,axs=plt.subplots(1,3,figsize=(11,4.8))
    specs=[('Contact discrimination','contact','balanced_accuracy','Balanced accuracy (%)',100),
           ('Nine-region localization','localization','accuracy','Accuracy (%)',100),
           ('Force estimation','force','mae_N','MAE (N); lower is better',1)]
    for ax,(title,task,key,label,mult) in zip(axs,specs):
        vals=[res[v][task][key]*mult for v in ['fixed','tuned']]
        ax.bar([0,1],vals,color=[gray,blue],width=.55)
        ax.set_xticks([0,1],['Previous fixed\nbenchmark','Tuned nested\nselector'])
        ax.set(title=title,ylabel=label,ylim=(0,108 if mult==100 else .8))
        for i,val in enumerate(vals):
            ax.text(i,val+(1.5 if mult==100 else .014),f'{val:.2f}%' if mult==100 else f'{val:.3f} N',ha='center',fontsize=10)
        ax.grid(axis='x',visible=False)
    fig.suptitle('BAURods model-assisted performance on held-out recordings',fontsize=14,y=.97)
    fig.subplots_adjust(top=.82,bottom=.30,wspace=.43)
    fig.text(.5,.09,'Equal recording weights; six outer TEST groups; eligible press frames at 0.05–3 N.\nForce/localization: 7,154 frames, 54 recordings. Contact: 8,506 frames, 65 recordings.\nRetrospective point estimates; no prospective performance interval established.',ha='center',fontsize=9)
    savefig(fig,figure_dir,'fig10_model_assisted_performance')
    # Re-render existing gate figures with unambiguous pipeline labels; values unchanged.
    d=core['detection']
    fig,axs=plt.subplots(1,2,figsize=(9,4.5),layout='constrained')
    mat=np.array([[d['TN'],d['FP']],[d['FN'],d['TP']]])
    axs[0].imshow(mat,cmap='Blues')
    axs[0].set_xticks([0,1],['No detection','Detection']);axs[0].set_yticks([0,1],['No contact','Press'])
    for (i,j),val in np.ndenumerate(mat):axs[0].text(j,i,str(val),ha='center',va='center',color='white' if val>mat.max()/2 else 'black',fontsize=14)
    axs[0].set(title='Archived gate: event detection',xlabel='Predicted',ylabel='Reference');axs[0].grid(False)
    rates=[d['sensitivity'],d['specificity'],d['precision'],d['f1']]
    axs[1].barh(['Recall','Specificity','Precision','F1'],rates,color=blue);axs[1].set_xlim(0,1.13)
    for i,val in enumerate(rates):axs[1].text(val+.02,i,f'{100*val:.1f}%',va='center',fontsize=9)
    axs[1].set(title='Saved criterion: 5 V digital units',xlabel='Proportion')
    savefig(fig,figure_dir,'fig08_global_contact_detection')
    cm=pd.read_csv(out/'tables/localization_confusion_matrix.csv')
    matrix=cm.drop(columns='actual_taxel').to_numpy();fractions=matrix/matrix.sum(axis=1,keepdims=True)
    fig,ax=plt.subplots(figsize=(9,6),layout='constrained')
    im=ax.imshow(fractions,cmap='Blues',vmin=0,vmax=1)
    fig.colorbar(im,ax=ax,fraction=.04,pad=.03,label='Fraction within actual class')
    for (i,j),n in np.ndenumerate(matrix):ax.text(j,i,str(n),ha='center',va='center',color='white' if fractions[i,j]>.5 else 'black',fontsize=8)
    ax.set_xticks(range(10),['None']+[f'T{i}' for i in range(1,10)]);ax.set_yticks(range(9),[f'T{i}' for i in range(1,10)])
    ax.set(title='Archived 5-V gate: press-level localization',xlabel='Predicted taxel (None = below 5 V digital units)',ylabel='Pressed taxel');ax.grid(False)
    savefig(fig,figure_dir,'fig09_overall_localization_confusion')

def update_text(here,tables,res):
    b,t=res['fixed'],res['tuned']
    specs=[('Contact balanced accuracy','contact','balanced_accuracy','percent'),
           ('Contact accuracy','contact','accuracy','percent'),('Contact recall','contact','recall','percent'),
           ('Contact specificity','contact','specificity','percent'),('Contact macro F1','contact','macro_f1','ratio'),
           ('Nine-region localization accuracy','localization','accuracy','percent'),
           ('Localization macro F1','localization','macro_f1','ratio'),
           ('Joint correct contact and region','combined','joint_contact_and_roi_accuracy','percent'),
           ('Force MAE','force','mae_N','N'),('Force RMSE','force','rmse_N','N'),('Force R²','force','r2','ratio')]
    fmt=lambda x,unit:f'{100*x:.2f}%' if unit=='percent' else f'{x:.3f}'+(' N' if unit=='N' else '')
    comparison=pd.DataFrame([[label,fmt(b[task][key],unit),fmt(t[task][key],unit)] for label,task,key,unit in specs],
                            columns=['Model-assisted characteristic','Previous fixed model','Tuned nested selector'])
    comparison.to_csv(tables/'model_assisted_thesis_table.csv',index=False)
    evidence_scope='''The model results use original, unannotated video features and six complete held-out TEST groups, with equal total weight for each recording. Force and localization use 7,154 eligible frames from 54 primary press recordings within 0.05–3 N. Contact adds 1,352 no-contact frames from 11 recordings (8,506 frames; 65 recordings total). These are correlated frame observations, not 8,506 independent contact events. The model protocol excludes 18 replay-designated recordings included in the broader 72-recording pulse analysis. Both analyses trace to the same manual archive; they are complementary evaluations, not independent replication. The recordings were collected on 8 August 2026; 9 September refers to saved model experiments, not new acquisition.'''
    lead=f'''BAURods demonstrates useful **model-assisted contact discrimination and nine-region localization** across the integrated 3 × 3 sensing skin. The strongest supported system-level results are **{100*t['contact']['balanced_accuracy']:.2f}% contact balanced accuracy**, **{100*t['localization']['accuracy']:.2f}% localization accuracy**, and **{t['force']['mae_N']:.3f} N force MAE**, from retrospective nested grouped evaluation. These results complement the global physical response analysis of **5,357 retained repeated presses across 72 recordings**. The physical analysis establishes measurable optical response and known-contact spatial discrimination; the learned models quantify what can be inferred from richer optical features within their evaluated interval.

{evidence_scope}

The learned pipelines were evaluated offline and were not installed into the live acquisition GUI. The archived 5-V acquisition gate remains a separate diagnostic with 0.597% press recall. Its result does not describe the later learned detector. Conversely, the learned detector's frame scores cannot be substituted for that gate's event-level performance or presented as prospective sensor validation.

### Model-assisted sensing performance

{mdtable(comparison)}

The fixed comparators are Extra Trees for force, logistic regression for localization and Random Forest for contact, selected retrospectively from the fixed benchmark. The tuned headline is the complete nested selector, not the most favorable family chosen after inspecting outer scores. Force MAE decreased by {(b['force']['mae_N']-t['force']['mae_N'])/b['force']['mae_N']*100:.2f}%, localization accuracy increased by {(t['localization']['accuracy']-b['localization']['accuracy'])*100:.2f} percentage points, and contact balanced accuracy increased by {(t['contact']['balanced_accuracy']-b['contact']['balanced_accuracy'])*100:.2f} points on matched held-out frame identities. These gains are retrospective and were not uniform across force bins or locations. [Model report, Tables 9–20](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

'''
    report_path=here/'Global_Sensor_Characterization_Report.md'
    report=report_path.read_text(encoding='utf-8')
    report=report.replace('Analysis date: 5 October 2026.','Physical analysis: 5 October 2026. Model evidence integration: 6 October 2026.')
    # Replace the introduction without disturbing the twelve characterization sections.
    start=report.index('\n\n',report.index('Experimental acquisition:'))+2
    end=report.index('## Global Characterization of the BAURods Visuotactile Sensing Skin')
    report=report[:start]+lead+report[end:]
    report=report.replace('### Model-assisted sensing performance\n\n','### Model-assisted sensing performance\n\n![Model-assisted performance on held-out recordings](../figures/fig10_model_assisted_performance.png)\n\n',1)
    # Keep original numerical results and label their acquisition-rule scope explicitly.
    for old,new in [('Contact-detection accuracy','Archived-gate contact accuracy'),
                    ('Contact-detection recall','Archived-gate contact recall'),('Contact-detection specificity','Archived-gate specificity'),
                    ('Overall localization accuracy','Archived-gate localization accuracy'),('Macro-averaged localization F1','Archived-gate localization macro F1')]:
        report=report.replace('| '+old+' |','| '+new+' |')
    inserts={
      '## 4. Sensitivity and Linearity':f'''### Model-assisted force estimation

The learned optical-to-force pipeline attained **MAE {t['force']['mae_N']:.3f} N, RMSE {t['force']['rmse_N']:.3f} N and R² {t['force']['r2']:.3f}**, with signed bias +{t['force']['bias_N']:.3f} N, on recording-balanced held-out frames within 0.05–3 N. This provides a practical force-estimation result alongside the scalar response curve. The two regressions have different targets: the physical curve predicts V digital units from peak force; the learned model predicts N from multivariate original-video features. Their RMSE and R² must not be ranked against one another. The learned model does not establish intrinsic sensitivity or linearity.

The pooled MAE reduction of {(1-t['force']['mae_N']/b['force']['mae_N'])*100:.2f}% supports improved retrospective estimation, with a clear remaining limitation: force MAE worsened in each of the four bins below 2 N. Below 0.5 N, tuned MAE was 1.241 N. Gains therefore concern the pooled evaluated distribution, not uniformly precise light-contact sensing. [Model report, Tables 9 and 17](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

### Apparent optical slope

''',
      '## 7. Detection Characteristics':f'''### Learned contact discrimination

The tuned nested detector achieved **{100*t['contact']['balanced_accuracy']:.2f}% balanced accuracy**, **{100*t['contact']['recall']:.2f}% recall**, **{100*t['contact']['accuracy']:.2f}% accuracy**, and **macro F1 {t['contact']['macro_f1']:.4f}**. Specificity and precision were both 100% on the observed dedicated controls. This is the principal model-assisted contact result: optical features distinguish eligible contact frames from unloaded recordings well under the archived conditions. The zero observed false-positive rate rests on only 11 no-contact recordings and does not establish zero deployment risk. No pooled tuned ROC-AUC is claimed because fold-selected score scales differ.

These metrics use recording-balanced frames within the model study's 0.05–3 N press interval. They do not count physical contact episodes or characterize onset/release. The final saved contact score threshold, 0.3268598318, belongs to its trained preprocessing, SVC score mapping and causal smoothing. It is neither an optical-brightness threshold nor a force limit of detection. [Model report, Tables 11, 14 and 22](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

### Archived acquisition gate and baseline diagnostics

''',
      '## 8. Temporal Characteristics':f'''The model report supplies a separate **computational timing** result: offline stateful inference took a median **{res['timing']['streaming_p50_ms']:.2f} ms** and 95th percentile **{res['timing']['streaming_p95_ms']:.2f} ms**. That measurement includes optical-tabular preprocessing, causal state, the three main tasks and an alternate contact policy. It excludes camera acquisition, decoding, pixel extraction and display. Contact/localization output smoothing in the saved causal models uses a 1-s time constant; that parameter is not a measured onset or recovery time. The runtime measurement supports software feasibility only and does not fill the intrinsic response-time or recovery-time entries. [Model report, Tables 21 and 23](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

''',
      '## 11. Localization Performance':f'''### Learned nine-region localization

Overall model-assisted BAURods localization accuracy was **{100*t['localization']['accuracy']:.2f}%**, with **macro F1 {t['localization']['macro_f1']:.4f}**, across 7,154 eligible held-out contact frames from 54 recordings. This describes nine discrete regions of one sensing skin. Requiring both successful learned contact detection and the correct region gave **{100*t['combined']['joint_contact_and_roi_accuracy']:.2f}% joint accuracy** on the same eligible positive frames. It is not a millimetre spatial-resolution result. [Model report, Tables 10 and 19](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

The model therefore supplies evidence of useful region discrimination on held-out recordings. Spatial/generalization differences remain material: TEST1 accuracy was 42.71%, versus 87.43–97.87% for TEST2–TEST6, and ROI 2 recall was 56.00%. No group or location was removed to improve the headline. The earlier known-contact argmax accuracy of 94.31% comes from different features, press peaks, weighting, force coverage and recording inclusion; it cannot be presented as outperforming the learned model or averaged with its score.

### Archived gate and known-contact press diagnostics

'''}
    for heading,addition in inserts.items():
        pattern=re.escape(heading)+r'\n\n(?:<!-- MODEL EVIDENCE START -->.*?<!-- MODEL EVIDENCE END -->\n\n)?'
        report=re.sub(pattern,lambda m:heading+'\n\n<!-- MODEL EVIDENCE START -->\n'+addition+'<!-- MODEL EVIDENCE END -->\n\n',report,count=1,flags=re.S)
    report=report.replace('The archive does not include event predictions from the later learned localization models, so no performance of those later models is claimed here.',
       'The supplied technical report and saved outer predictions now support the separate frame-level learned-model results above. Those predictions have not been converted into a new press-level model score.')
    start=report.index('## 12. Characterization Summary')
    end=report.index('### Reproducibility and evidence',start)
    report=report[:start]+f'''## 12. Characterization Summary

The combined evidence supports BAURods as an integrated 3 × 3 visuotactile skin with measurable force-associated optical response, useful spatial information and strong retrospective model-assisted contact discrimination. The clearest application result is {100*t['contact']['balanced_accuracy']:.2f}% contact balanced accuracy, complemented by {100*t['localization']['accuracy']:.2f}% nine-region localization accuracy and {100*t['combined']['joint_contact_and_roi_accuracy']:.2f}% joint contact-and-region accuracy within the specified frame evaluation. Force estimation improved to {t['force']['mae_N']:.3f} N MAE but remains less mature, particularly for lighter contacts.

The physical press analysis and learned prediction study answer complementary questions. The first quantifies the recorded optical behavior across the tested skin; the second measures the usefulness of richer original-video features for inference on held-out recordings. Together they support the feasibility of computational interpretation of the sensing skin under the archived manual conditions. They do not isolate which modeling component caused each gain or establish prospective live-system performance. The low recall of the earlier 5-V gate remains visible as a limitation of that acquisition configuration.

The experiment supports tested force/response ranges, global descriptive statistics, a pooled response model, apparent slope, force-conditioned dispersion, sampled baseline statistics and separately scoped detection/localization results. Intrinsic force sensitivity, a validated linear operating interval, a reliable force detection limit, intrinsic response/recovery, hysteresis, saturation capacity, long-term drift and between-device reproducibility remain **not quantitatively assessed**. Neither the 0.05–3 N model interval nor the 23.3004 N observed press maximum establishes a validated operating limit.

'''+report[end:]
    report=re.sub(r'\n<!-- MODEL SOURCES START -->.*?<!-- MODEL SOURCES END -->','',report,flags=re.S).rstrip()+'\n'
    report+='''
<!-- MODEL SOURCES START -->
The model extension is reproduced with `integrate_model_characterization.py`. It independently recomputes selected-model metrics and combined contact/region outputs from saved outer predictions, checks matched fixed/tuned frame IDs and truth, and confirms the manual archive hash. `model_report_validation.json` records the arithmetic checks and input hashes; `model_assisted_outer_predictions.csv` retains the evaluated predictions. Copied source tables and the supplied PDF are in `model_report_review/evidence/`. No models were retrained or installed into the GUI during this integration.
<!-- MODEL SOURCES END -->
'''
    report_path.write_text(report,encoding='utf-8')
    thesis=f'''# Results and Discussion: Global Characterization and Model-Assisted Sensing of BAURods

The BAURods 3 × 3 visuotactile skin exhibited measurable optical responses across all nine sensing regions and supported model-assisted contact detection, region localization and force estimation. Retrospective nested grouped evaluation yielded **{100*t['contact']['balanced_accuracy']:.2f}% contact balanced accuracy**, **{100*t['localization']['accuracy']:.2f}% localization accuracy**, and **{t['force']['mae_N']:.3f} N force mean absolute error**. These outcomes establish the strongest supported computational capabilities of the integrated skin under the archived manual conditions. Global physical-response characterization complements these predictive results by describing the sensor's recorded optical behavior across 5,357 retained repeated presses. [Technical report, Tables 9–11](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

{evidence_scope}

Each retained physical press contributed one force–response observation, and uncertainty was assessed by resampling complete recordings within the nine fixed locations. The 29,318 camera samples were not treated as independent physical trials. Peak force was paired with the greatest global optical response within the same force-defined interval. The primary global response was the maximum across nine ROI means of positive baseline-corrected V-channel change, followed by the maximum within each press. This response used the archived color-magnified output, whereas the learned models used multivariate original-video features. Their results consequently characterize different stages of the sensing system.

Retained press peaks covered 1.151–23.300 N, with optical responses of 0.707–8.570 V digital units. The pooled relationship was S = 0.90855 + 0.12106 F, corresponding to an apparent slope of 0.12106 V digital units/N (95% recording-bootstrap interval 0.08885–0.14901). The positive slope supports a force-associated optical response at the global level. Its R² of 0.2229 and recording-held-out R² of 0.1934 indicate substantial remaining dispersion. The slope is processing- and protocol-dependent and should not be treated as intrinsic material sensitivity or a transferable linear force calibration. The tested maximum does not establish sensor capacity.

The 11 no-contact recordings had a recording-equal mean response of 0.8980 V digital units and RMS temporal standard deviation of 0.0437 V units. The median press-peak excess relative to this baseline noise was 18.406. This ratio provides an exploratory measure of response contrast under the recorded processing configuration, rather than physical optical-power SNR. Global residual SD was 0.7046 V units; it describes force-conditioned dispersion under manual loading, including spatial, acquisition and loading-rate variation. Controlled repeatability and long-term drift remain unquantified.

The learned contact detector was the strongest predictive component. It attained {100*t['contact']['recall']:.2f}% recall and {100*t['contact']['accuracy']:.2f}% accuracy, with macro F1 {t['contact']['macro_f1']:.4f}. Balanced accuracy increased from {100*b['contact']['balanced_accuracy']:.2f}% for the previous fixed Random Forest to {100*t['contact']['balanced_accuracy']:.2f}% for the complete tuned selection procedure. No false positives were observed on the 11 dedicated no-contact recordings. This supports effective discrimination of the evaluated contact and unloaded conditions, while the small number and limited diversity of controls restrict generalization to sustained live operation. These are recording-balanced frame scores and do not establish event-onset detection or a force detection limit. [Technical report, Tables 11 and 14](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

Nine-region localization accuracy improved from {100*b['localization']['accuracy']:.2f}% to {100*t['localization']['accuracy']:.2f}%, with macro F1 increasing from {b['localization']['macro_f1']:.4f} to {t['localization']['macro_f1']:.4f}. Requiring both a positive contact decision and the correct region yielded {100*t['combined']['joint_contact_and_roi_accuracy']:.2f}% joint accuracy on eligible positive frames. These findings support useful spatial discrimination by the complete sensing skin without treating the regions as separate sensors. Performance varied across held-out groups and locations: TEST1 reached 42.71% accuracy and ROI 2 recall was 56.00%. The overall gain therefore coexists with spatial and acquisition-dependent weaknesses. The regions are discrete labels; no continuous localization error in millimetres was measured. [Technical report, Tables 10, 15–16 and 19](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

Force estimation benefited from the expanded modeling procedure. MAE decreased from {b['force']['mae_N']:.3f} to {t['force']['mae_N']:.3f} N, a {(1-t['force']['mae_N']/b['force']['mae_N'])*100:.2f}% reduction, while RMSE decreased from {b['force']['rmse_N']:.3f} to {t['force']['rmse_N']:.3f} N. R² increased from {b['force']['r2']:.3f} to {t['force']['r2']:.3f}, and bias changed from {b['force']['bias_N']:.3f} to +{t['force']['bias_N']:.3f} N. This supports improved retrospective force inference within 0.05–3 N, although force estimation remains the least mature quantitative function. Error increased in all four diagnostic bins below 2 N, reaching 1.241 N MAE below 0.5 N. The 0.05 N inclusion boundary is not a validated detection limit, and 3 N is not a maximum operating specification. [Technical report, Tables 9 and 17](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

The archived acquisition gate was evaluated separately to explain the behavior of the earlier software configuration. Its fixed 5-V digital-unit criterion detected only 32 of 5,357 press events (0.597% recall); all 32 were correctly localized. When contact was externally known and the gate was removed, the strongest-ROI rule achieved 94.31% press-level accuracy and macro F1 0.94437. This indicates that spatial information was present even when the configured gate suppressed outputs. The later learned detector provides a separate demonstration of contact discrimination from richer features. Because input processing, eligibility, weighting and evaluation units differ, these numbers are not a direct before/after comparison of the gate and learned detector.

Offline stateful model inference took a median {res['timing']['streaming_p50_ms']:.2f} ms per frame (95th percentile {res['timing']['streaming_p95_ms']:.2f} ms), covering the model tasks and tabular preprocessing but excluding acquisition, decoding, pixel extraction and display. This is a computational timing measurement, not sensor response time. Intrinsic response/recovery and hysteresis were not quantitatively assessed: manual presses lacked controlled loading/release conditions, optical sampling was sparse, and the archived primary response underwent temporal filtering. The saved causal model also uses optical history and smoothing, whose onset/release effects require separate measurement. [Technical report, Tables 21 and 23](model_report_review/evidence/Manual_Calibration_Models_Technical_Report.pdf).

Overall, the combined results support BAURods as a sensing skin with useful optical and spatial information that can be interpreted computationally for contact detection, region classification and exploratory force estimation. Contact discrimination and localization provide the strongest demonstrated capabilities. Further work should prioritize light-force estimation, acquisition robustness and prospective validation with frozen models. The reported evidence remains retrospective, comes from one skin and collection session, and does not establish superiority over other sensors or validated live deployment.
'''
    (here/'Thesis_Results_and_Discussion.md').write_text(thesis,encoding='utf-8')
    # Extend both summary exports, preserving every original physical result.
    for path in [here/'global_characterization_summary.csv',tables/'global_characterization_summary.csv']:
        df=pd.read_csv(path)
        df=df.loc[~df.iloc[:,0].str.startswith('Model-assisted')].copy()
        df.iloc[:,0]=df.iloc[:,0].replace({'Contact-detection accuracy':'Archived-gate contact accuracy',
          'Contact-detection recall':'Archived-gate contact recall','Contact-detection specificity':'Archived-gate specificity',
          'Overall localization accuracy':'Archived-gate localization accuracy','Macro-averaged localization F1':'Archived-gate localization macro F1'})
        add=pd.DataFrame([[f'Model-assisted {label.lower()}',fmt(t[task][key],unit),
             'Retrospective nested outer frames; equal recording weights; eligible presses 0.05–3 N; not press-event or deployed accuracy']
             for label,task,key,unit in specs],columns=df.columns)
        pd.concat([add,df],ignore_index=True).to_csv(path,index=False)
    readme=here/'README.md'
    text=readme.read_text(encoding='utf-8').split('\n## Model evidence integration')[0]
    text+='''
## Model evidence integration

The report now includes separately scoped learned-model results from the supplied technical report, independently recomputed from saved outer predictions. The physical press analysis is unchanged. Model frame scores use original-video features, six held-out TEST groups and equal recording weights within the 0.05–3 N press interval. They do not replace physical response specifications or imply live deployment.

After the core script, run `calibration_gui/.venv/Scripts/python.exe integrate_model_characterization.py` from the workspace. The default inputs are the original fixed/tuned model run directories and supplied PDF. Use `--model-runs`, `--model-report` and `--output` to relocate inputs/outputs. Then run `prepare_workbook.py`, the existing XLSX builder, and `verify_and_package.py` with their documented runtimes. The package contains reviewed prediction rows, copied metrics/protocols, validation results and the source PDF; raw archive, full model binaries and feature store remain external. No retraining is needed to reproduce this integration.
'''
    readme.write_text(text,encoding='utf-8')

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,default=ROOT/'outputs')
    ap.add_argument('--model-runs',type=Path,default=DEFAULT_RUNS)
    ap.add_argument('--model-report',type=Path,default=DEFAULT_PDF)
    args=ap.parse_args()
    integrate(args.output,args.model_runs,args.model_report)
