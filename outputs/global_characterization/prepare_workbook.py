"""Prepare typed, verified analysis tables for the companion XLSX builder."""
import json
from pathlib import Path
import pandas as pd
import numpy as np
from datetime import datetime, timezone
HERE=Path(__file__).resolve().parent
OUT=HERE.parent
s=json.loads((HERE/'analysis_results.json').read_text())
e=pd.read_csv(OUT/'tables/global_force_response_data.csv')
m=pd.read_csv(OUT/'tables/fitted_model_comparison.csv').set_index('model').loc[s['selected_model']]
det=s['detection'];loc=s['localization'];b=s['baseline']
summary=[['Characteristic','Result','Units','Scope'],
['Valid segmented presses',len(e),'events','Distinct inferred physical presses, clustered within 72 recordings'],
['Press recordings',e.recording.nunique(),'recordings','Eight recordings per location; one acquisition session S3'],
['Minimum press peak force',float(e.force_peak_N.min()),'N','Retained events, not a force detection threshold'],
['Maximum press peak force',float(e.force_peak_N.max()),'N','Tested maximum, not sensor capacity'],
['Mean press peak force',float(e.force_peak_N.mean()),'N','One observation per retained press'],
['Mean global optical response',float(e.optical_peak_V.mean()),'V digital units','Peak maximum ROI mean positive delta-V, color-magnified output'],
['Global apparent slope',s['selected_parameters'][1],'V digital units/N','Descriptive linear slope of acquisition pipeline; not intrinsic sensitivity'],
['Slope CI lower',s['parameter_ci95'][0][1],'V digital units/N','95% recording-cluster bootstrap, stratified by taxel'],
['Slope CI upper',s['parameter_ci95'][1][1],'V digital units/N','2,000 replicates; conditional on one skin and collection session'],
['Global intercept',s['selected_parameters'][0],'V digital units','Positive clipped-baseline noise floor retained'],
['Global model R-squared',float(m.r2),'proportion','Descriptive fit, not proof of linearity'],
['Global model RMSE',float(m.rmse),'V digital units','In-sample error'],
['Recording-held-out RMSE',float(m.recording_cv_rmse),'V digital units','No recording is split between training and validation'],
['Global residual SD',float(e.residual_V.std()),'V digital units','Manual force-conditioned dispersion, not controlled repeatability'],
['No-contact global mean',b['mean_V'],'V digital units','Recording-equal mean across 11 unloaded recordings'],
['Baseline temporal noise SD',b['noise_sd_V'],'V digital units','RMS of within-recording temporal SDs'],
['Median SNR proxy',float(e.snr_proxy.median()),'dimensionless','Peak excess above no-contact mean / baseline temporal noise'],
['Contact detection accuracy',det['accuracy'],'proportion','Highly unequal positive/negative counts'],
['Contact detection recall',det['sensitivity'],'proportion',f"{det['TP']} of {det['n_positive']} known presses"],
['Contact detection specificity',det['specificity'],'proportion','11 independent recording windows, not all no-contact frames'],
['Contact detection precision',det['precision'],'proportion','At archived 5 V optical criterion'],
['Contact detection F1',det['f1'],'proportion','At archived 5 V optical criterion'],
['Exploratory contact ROC-AUC',det['roc_auc'],'proportion','Event maximum optical score; only 11 negative controls'],
['Overall localization accuracy',loc['accuracy'],'proportion','No detection counts as localization failure'],
['Macro localization F1',loc['macro_f1'],'proportion','Nine target classes'],
['Known-contact ungated accuracy',loc['ungated_accuracy'],'proportion','Offline argmax on all presses; not archived end-to-end performance'],
['Known-contact ungated macro F1',float(np.mean([2*((e.target.eq(i)&e.ungated_predicted_taxel.eq(i)).sum())/(e.target.eq(i).sum()+e.ungated_predicted_taxel.eq(i).sum()) for i in range(1,10)])),'proportion','Offline location discrimination with contact externally known'],
['Force detection threshold','Not quantitatively assessed','N','No replicated independent low-force staircase'],
['Response and recovery time','Not quantitatively assessed','s','Sparse sampling, temporal filter and unverified release baseline'],
['Hysteresis','Not quantitatively assessed','percent','No controlled comparable loading and unloading trajectories'],
['Saturation / operating limit','Not quantitatively assessed','N','Tested force range only']]

model_results_path=HERE/'model_assisted_results.json'
model_sheet=None
if model_results_path.exists():
    model_results=json.loads(model_results_path.read_text())
    pairs=[('Contact balanced accuracy','contact','balanced_accuracy'),('Contact accuracy','contact','accuracy'),
        ('Contact recall','contact','recall'),('Contact specificity','contact','specificity'),
        ('Contact macro F1','contact','macro_f1'),('Nine-region localization accuracy','localization','accuracy'),
        ('Localization macro F1','localization','macro_f1'),
        ('Joint correct contact and region','combined','joint_contact_and_roi_accuracy'),
        ('Force MAE','force','mae_N'),('Force RMSE','force','rmse_N'),('Force R-squared','force','r2')]
    model_rows=[['Characteristic','Fixed benchmark','Tuned selector','Units','Frames','Recordings','Evaluation scope']]
    for label,task,key in pairs:
        data=model_results['tuned'][task]
        units='N' if key.endswith('_N') else 'proportion'
        scope='Offline nested evaluation; six TEST groups; equal recording weights; press force 0.05-3 N'
        model_rows.append([label,model_results['fixed'][task][key],data[key],units,data.get('frames',7154),data.get('recordings',54),scope])
    model_rows.append(['Median streaming inference',None,model_results['timing']['streaming_p50_ms'],'ms',None,None,
        'Software only; excludes acquisition, decoding, pixel extraction and display; not sensor response time'])
    model_sheet=dict(name='Model-assisted evaluation',rows=model_rows)
    top_labels={'Contact balanced accuracy','Contact recall','Nine-region localization accuracy','Localization macro F1',
                'Joint correct contact and region','Force MAE','Force R-squared'}
    added=[['Model-assisted '+row[0].lower(),row[2],row[3],row[6]] for row in model_rows[1:] if row[0] in top_labels]
    for row in summary[1:]:
        if row[0] in ['Contact detection accuracy','Contact detection recall','Contact detection specificity',
                      'Contact detection precision','Contact detection F1','Overall localization accuracy','Macro localization F1']:
            row[0]='Archived gate: '+row[0].lower()
    summary=summary[:1]+added+summary[1:]

def clean(v):
    if v is None or (isinstance(v,(float,np.floating)) and not np.isfinite(v)):return None
    if isinstance(v,np.integer):return int(v)
    if isinstance(v,np.floating):return float(v)
    if isinstance(v,np.bool_):return bool(v)
    return v

def sheet(name,df):
    df=df.copy()
    epoch=datetime(1899,12,30,tzinfo=timezone.utc)
    for col in list(df.columns):
        sample=df[col].dropna()
        if len(sample) and isinstance(sample.iloc[0],str) and 'T' in sample.iloc[0] and sample.iloc[0].endswith('+00:00'):
            df[col]=df[col].map(lambda value:(datetime.fromisoformat(value)-epoch).total_seconds()/86400 if isinstance(value,str) else np.nan)
            df=df.rename(columns={col:col.replace('_iso','')+'_UTC'})
    return dict(name=name,rows=[df.columns.tolist()]+[[clean(v) for v in row] for row in df.itertuples(index=False,name=None)])

sheets=[dict(name='Summary',rows=summary)]
if model_sheet:sheets.append(model_sheet)
for name,filename in [('Press events','global_force_response_data'),('Descriptive statistics','global_descriptive_statistics'),
    ('Model comparison','fitted_model_comparison'),('Model parameters','fitted_model_parameters'),('Repeatability','repeatability_statistics'),
    ('Baseline','baseline_statistics'),('Localization','localization_confusion_matrix'),('Localization classes','localization_per_class'),
    ('Detection','contact_detection_metrics'),('Segmentation checks','segmentation_sensitivity'),('Response definitions','response_definition_comparison'),
    ('Spatial and session models','session_spatial_model_comparison'),('Recording inventory','recording_inventory')]:
    frame=pd.read_csv(OUT/'tables'/f'{filename}.csv')
    if filename=='baseline_statistics':frame=frame.sort_values('start_iso')
    sheets.append(sheet(name,frame))
sheets.append(sheet('Spatial consistency',pd.read_csv(OUT/'spatial_diagnostics/spatial_consistency.csv')))
sheets.append(dict(name='Methods',rows=[['Topic','Definition or limitation'],
['Controlling source','Manual Calibration (2).zip; collected 8 August 2026'],
['Source SHA-256',s['archive_sha256']],
['Optical source','Archived master_synchronized.csv; all sessions declare Motion-magnified frame'],
['Global response','For each press: maximum over time of maximum across nine ROI mean positive baseline-corrected V signals.'],
['Units','V digital units are image brightness values, not volts. Reference force is in N.'],
['Physical presses','Operator confirmed separate repeated finger presses. Force-only segmentation uses prominence 0.5 N and minimum spacing 0.15 s.'],
['Eligibility','Rise and fall >=0.5 N; peak >0.05 N; >=3 camera samples; sample within 0.15 s of force peak.'],
['Independence','Presses are observations; confidence intervals resample complete recordings within each taxel. No frame-level pseudoreplication.'],
['Optical gate','Saved localization rule: dominant mean positive delta-V >=5 V units. Below-threshold events remain failures.'],
['No-contact controls','Eight filenames NOCONTACT contradict retained Press metadata; near-zero measured force confirms control status.'],
['Baselines','Four captured baseline IDs within one session S3. No-contact recordings cover only later blocks.'],
['Model comparison','Linear, quadratic, logarithmic and power-law; grouped validation and constant benchmark.'],
['Reported intervals','2,000 cluster bootstrap replicates, seed 20261005. Intervals are conditional on this skin and session.'],
['Workbook role','Frozen numerical export of reproducible Python analysis, not an editable calibration calculator.'],
['Reproduction','Run global_sensor_characterization.py, then prepare_workbook.py and build_statistics_workbook.mjs.'],
['Unsupported metrics','No intrinsic sensitivity, certified linearity, force limit of detection, intrinsic response/recovery, hysteresis or saturation limit.']]))
if model_sheet:
    sheets[-1]['rows'] += [
        ['Model source','Manual_Calibration_Models_Technical_Report.pdf, Tables 9-11, 19, 23; copied under model_report_review/evidence'],
        ['Model verification','Saved fixed and tuned outer_predictions.parquet independently recomputed; 36 metrics checked; matched held-out frame identities and truth.'],
        ['Model unit','Correlated frames with equal recording weights, not independent physical events. Original session_video.mp4 features.'],
        ['Model scope','54 primary press recordings; 11 controls; six held-out TEST groups; 18 replay recordings excluded. Retrospective research evaluation, not live deployment.'],
        ['Model limitations','Force MAE worsened in bins below 2 N; TEST1 localization 42.71%; ROI 2 recall 56%; zero observed FPR based on 11 controls.'],
        ['Model reproduction','Run integrate_model_characterization.py after the core physical script and before this workbook builder.']]
(HERE/'workbook_data.json').write_text(json.dumps(dict(sheets=sheets),allow_nan=False),encoding='utf-8')
print('Prepared',len(sheets),'sheets')
