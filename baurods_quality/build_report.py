"""Assemble the reviewed report and thesis prose from saved numerical evidence."""
from pathlib import Path
import json
import importlib.metadata
import pandas as pd
import numpy as np

ROOT=Path(__file__).resolve().parent;OUT=ROOT/'outputs';T=OUT/'tables'
def read(name):return pd.read_csv(T/(name+'.csv'))
def mdtable(df):
    def fmt(x):
        if pd.isna(x):return '—'
        if isinstance(x,(float,np.floating)):return f'{x:.3f}'
        return str(x)
    return '| '+' | '.join(df.columns)+' |\n| '+' | '.join(['---']*len(df.columns))+' |\n'+'\n'.join('| '+' | '.join(fmt(x) for x in row)+' |' for row in df.to_numpy())
def rows(df):return json.loads(df.to_json(orient='records'))

def main():
    s=json.loads((OUT/'summary.json').read_text());v=json.loads((OUT/'supplemental_validation.json').read_text())
    if s['archive_sha256']!='e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a':
        raise ValueError('The reviewed prose applies only to the audited source hash. Review and revise the narrative for any different archive.')
    rec=read('recording_summary');tr=rec[~rec.no_contact_recording].copy();dist=read('taxel_distribution')
    opt=read('optical_repeatability');force=read('force_variability');base=read('unique_baselines');nc=read('no_contact_baseline_stability')
    corr=read('force_optical_correlations');perf=read('localization_metrics');splits=read('verified_split_audit')
    joined=opt[opt.variable.eq('optical_mean_delta_v')][['taxel','n','mean','sd','cv_percent']].merge(force[force.variable.eq('force_mean_N')][['taxel','mean','sd','cv_percent']],on='taxel',suffixes=('_optical','_force'))
    joined.to_csv(T/'joint_repeatability_summary.csv',index=False)
    table=joined.rename(columns={'taxel':'Taxel','n':'Recordings','mean_optical':'Optical mean ΔV','sd_optical':'Optical SD','cv_percent_optical':'Optical CV (%)','mean_force':'Force mean (N)','sd_force':'Force SD (N)','cv_percent_force':'Force CV (%)'})
    count_table=dist[['taxel','recordings','frames','contact_frames','frame_percent_of_press_records']].rename(columns={'taxel':'Taxel','recordings':'Recordings','frames':'Press-recording frames','contact_frames':'Contact frames','frame_percent_of_press_records':'Frame share (%)'})
    integrity=pd.DataFrame([
        ['Camera/master records',29318,29318,100.0],['Feature rows',29318,29318,100.0],['Decoded primary video frames',29318,29318,100.0],
        ['Raw load-cell samples',42568,42568,100.0],['Core-complete synchronized rows',29318,29318,100.0],
        ['Valid synchronized rows',29318,29318,100.0]],columns=['Check','Observed valid','Expected from recorded counters','Completeness (%)'])
    integrity.to_csv(T/'dataset_completeness.csv',index=False)
    findings=[
        ('Integrity','Supported','All accepted camera frames and load-cell rows reconcile; no corrupted structured file or duplicate frame key found.'),
        ('Taxel coverage','Supported','All nine taxels have eight press recordings; frame counts vary with duration.'),
        ('Baseline stability','Qualified','Short no-contact runs are stable in digital V; four saved baseline captures differ materially.'),
        ('Repeatability','Qualified','Recording-level dispersion is measurable; physical press IDs and fixed-force repeats are unavailable.'),
        ('Timing','Supported for matching','Recorded nearest-sample offsets and interpolated force reconstruct; physical sensor latency is unresolved.'),
        ('Generalization','Unresolved','Only one day, one S3 label and one sensing-skin identifier are present.'),
        ('Localization','Limited','The saved rule withholds 99.873% of contact-frame predictions.'),
        ('Evaluation splits','Mixed','Later folds hold whole recordings; a legacy output splits all nine recordings across folds.')]
    pd.DataFrame(findings,columns=['Dimension','Assessment','Evidence']).to_csv(T/'overall_assessment.csv',index=False)
    summary=f"""## Executive summary

The archive is **complete and internally consistent enough for exploratory analysis of the recorded BAURods pipeline**, subject to substantial limits on press-level repeatability and generalization.

- **29,318 / 29,318 camera frames** reconcile with synchronized rows and fully decoded primary videos. All **42,568 raw load-cell samples** reconcile with ingress counts. Required fields are complete.
- **72 press recordings** cover all nine taxels equally, with **eight recordings each**; **11 no-contact recordings** provide a separate baseline check. These are recordings, not a verified count of independent physical presses.
- Across recording means, optical CV is **9.45–28.66%** and manual-force CV is **9.40–36.23%**. The varying input force prevents a pure estimate of sensor repeatability.
- The saved localization rule returns **34 predictions for 26,786 contact frames (0.127% coverage)**. All 34 match the target, but that conditional accuracy does not demonstrate broad localization capability.

All recordings carry **session S3**, were acquired on **8 August 2026**, and are configured to export **motion-magnified optical measurements**. Conclusions therefore apply to this acquisition and processing pipeline, not an independently calibrated raw optical response."""
    sections=[]
    def section(id,title,text,q,chart=None):sections.append(dict(id=id,title=title,text='## '+title+'\n\n'+text,query=q,chart=chart))
    section('overview','1. Dataset Overview',f"""BAURods is a press-activated hollow-rod mechanoluminescent visuotactile array intended for normal-contact detection and discrete localization across nine taxels. Forces were applied by **manual finger pressing**, with a load cell used as the reference. The source archive contains **2,551 files**: 736 CSV, 913 JSON, 83 NPZ, 332 MP4, 404 PNG and 83 log files; no Excel file is present in the archive.

Each of the 83 recording directories contains a 330-column optical feature table, a 349-column synchronized table and a 46-column raw load-cell table. The feature and master tables describe the same **29,318 frames** and must not be added together as separate observations. There are **83 unique trial IDs**, one source session ID (`S3`), one collection day, and four unique saved baseline IDs. The acquisition window is **14:21:30–17:52:02 Asia/Manila**.

The archived dictionary describes `roiN_delta_v_mean` as mean positive per-pixel baseline-corrected V (digital delta V, 0–255). This existing variable is used without inventing a new sensing metric. Recording summaries use its mean and maximum within valid force-derived contact frames. Force is in newtons. `contact_state_derived` uses the saved **0.05 N** threshold, not the trial-wide interaction label.

**Unit of analysis.** A unique directory is called a recording here. Source `trial_id` labels are retained, but there is no physical press ID. The force traces contain repeated excursions; 81 contiguous above-threshold runs occur across 72 press recordings, with five recordings having multiple runs, five starting in contact and 30 ending in contact. Runs may merge repeated presses when force remains above threshold. Therefore 81 is a threshold-run count, not a defensible press count. The primary summaries give each recording equal weight; per-press repeatability cannot be recovered reliably without event annotation.

The workspace file inventory records existing raw/processed/model artifacts. This report's numerical evidence is taken directly from the supplied manual ZIP; automatic-indentation datasets are not pooled into it. Existing workspace split and prediction files are examined only for evaluation integrity.""",'recordings')
    section('integrity','2. Data Integrity and Completeness',f"""Every archive member passed its ZIP CRC read. All 736 CSV files, 913 JSON files and 83 NPZ files parsed. All **83 primary videos were decoded** and their combined frame count matched the accepted-frame counters; auxiliary videos were CRC-checked but not frame-decoded. Common fields in each optical feature table and synchronized table agree.

{mdtable(integrity)}

There are **zero missing core force, target-label, optical-channel, timestamp, session-ID or trial-ID values**, zero duplicate feature rows, zero duplicate recording/frame keys, zero missing frame IDs within recordings, and zero skipped raw sample IDs within recordings. All source validity flags used for analysis are true; no raw load-cell row is marked invalid. No observation was excluded from the core analysis for an integrity failure.

The completeness denominator is the **recorded accepted/ingress count**, not an unavailable planned acquisition total. This cannot establish that every intended trial was performed or that no frame was dropped before acceptance. Effective accepted-frame rates are **7.489–7.505 Hz**, despite a requested camera rate of 30 Hz; achieved and requested rate are different quantities.

Literal completeness across all 349 columns is **0 / 29,318 rows**, because optional or inapplicable fields are blank. Examples include automated-printer coordinates, sequence identifiers and optional force-class/press-number labels. Blank `predicted_dominant_roi` entries are documented threshold abstentions. These must not be called missing sensor observations. Exact field types, missing counts and source missing-value rules are supplied in the data dictionary and column-profile tables.""",'integrity')
    section('coverage','3. Experimental Coverage',f"""There are **27,966 frames in 72 press recordings**, including **26,786 force-derived contact frames** and 1,180 within-recording no-contact frames. The eleven dedicated no-contact recordings contain another **1,352 frames**. Thus the total no-contact reference is **2,532 frames**.

{mdtable(count_table)}

Recording counts are exactly equal across the nine taxels. Frame counts range from {int(dist.frames.min()):,} to {int(dist.frames.max()):,}, a largest/smallest ratio of {dist.frames.max()/dist.frames.min():.3f}; this reflects unequal durations rather than unequal recording counts. Frame-weighted model results can therefore overweight longer sequences. No arbitrary balance threshold is applied. Dedicated no-contact trials are reported separately even though their metadata retains a target ROI.""",'coverage',dict(id='coverage-chart',title='Frames in press recordings by taxel',spec=dict(type='bar',x='taxel',y='frames',yLabel='Frames',valueDecimals=0),query='coverage'))
    section('baseline','4. Baseline Stability',"""Four unique baseline captures were reused across 83 recordings; counting all copied baseline rows as independent captures would exaggerate the evidence. Across taxels and captures, saved mean V ranges from **26.254 to 31.734**. The absolute first-to-last capture change is **11.02–11.96%** by taxel. This is a between-capture shift, not a continuously observed drift trajectory or a proven mechanism.

The 11 dedicated no-contact recordings support 99 recording/ROI traces. Their uncorrected mean-V CVs are **0.312–0.725%**; absolute start-to-end changes are **0.002–0.520%**, comparing means over the first and final 10% of frames (ceiling-rounded windows). These show relatively small fluctuations over the observed short recordings; they do not establish stability throughout the entire experiment. Digital V is not calibrated radiance, so optical CVs are descriptive ratios on this processing scale, not metrological uncertainty.

Positive-corrected no-contact response is nonzero: recording/ROI means span **0.502–0.915 delta V**, with CV **5.50–9.14%** and window drift up to **7.39%**. Positive clipping and the processing pipeline must be considered when interpreting low optical responses.

The saved pre-recording drift statistic spans **0.041–0.621 V units**, below the configured acquisition threshold of **5 V units** in every recording. That is a software gate from the files, not a universal data-quality criterion. NPZ files retain per-pixel mean/median baseline images; their summaries' spatial standard deviations are not temporal standard deviations. The original baseline frame stacks are not supplied, so the baseline-capture temporal distribution cannot be reconstructed. Autofocus and automatic white balance are recorded as enabled in all 83 configurations; their causal contribution to baseline shifts is not established.""",'baseline',dict(id='baseline-chart',title='Four saved baseline captures',spec=dict(type='line',x='capture_utc',y='mean_v',series='roi',yLabel='Saved mean (OpenCV V)',valueDecimals=2,stackable=False),query='baseline'))
    section('repeatability','5. Optical-Response Repeatability',f"""Each taxel contributes **eight recording means** computed over valid force-derived contact frames. The existing target-ROI mean positive delta V is averaged per recording; a separate table summarizes the maximum of that same feature. Between-recording sample SD uses `ddof=1`; CV is 100 × SD / mean. Quartiles use linear interpolation. Outliers remain included.

{mdtable(table)}

Mean-response CV ranges from **9.45% (T5) to 28.66% (T3)**. Peak-response CV ranges from **29.56% to 102.12%**, with the largest value at T4. Thus means are less dispersed than maxima under this aggregation. Longer recordings provide more opportunities for high maxima, so the peak comparison is duration-sensitive. Lower dispersion here describes these recordings; it does not isolate sensor repeatability at fixed loading.

All optical measurements are identified by `quantitative_analysis_source` as **Motion-magnified frame**, and `magnified_pixels_used_for_exported_measurements` is true throughout. These results characterize the saved transformed signal. Video decoding verifies frame counts, not pixel-level reproduction of every feature. The magnitude and timing of unprocessed optical responses require a separate feature recomputation from original video.

Physical press boundaries, controlled force levels and independent experimental sessions are unavailable. Consequently, a pure same-force, press-to-press repeatability coefficient and inter-session reproducibility claim are **not supported**. The recording-level result is retained as an honest descriptive surrogate.""",'repeatability',dict(id='optical-box',title='Optical response across eight recordings per taxel',spec=dict(type='boxPlot',x='taxel',y='optical_mean_delta_v',yLabel='Recording mean (delta V)',valueDecimals=3),query='trials'))
    section('force','6. Applied-Force Variability',"""Mean contact force per recording ranges from **2.149 to 7.459 N**. Within-taxel CV of these means ranges from **9.40% (T5) to 36.23% (T1)**. Peak-force means and full descriptive distributions are included separately; synchronized forces reach **22.699 N**.

Manual force variability is an experimental input variation, not by itself evidence of corrupted measurements. It provides an alternative explanation for differences in optical response, together with loading history, duration and baseline changes. No force normalization or model has been used to erase that variation.

The calibration records document a **200 g reference mass (1.96133 N under the recorded conversion)** and passing stored verification checks. The maximum observed force is about 11.6 times that reference force. The supplied records do not provide a multi-point calibration or uncertainty budget covering the full observed range; reference-force accuracy over that range is therefore not established by this audit. Small negative tared forces occur in **2,154 frames**, reaching **−0.190 N**. They are retained as measured offsets/noise, not automatically declared impossible or clamped to zero.""",'force',dict(id='force-box',title='Manual force varies between recordings',spec=dict(type='boxPlot',x='taxel',y='force_mean_N',yLabel='Recording mean force (N)',valueDecimals=2),query='trials'))
    section('relationship','7. Force–Optical Response Relationship',"""The question is whether paired force and optical recordings exhibit a coherent directional association. Spearman's rank correlation describes monotonic association without imposing a linear calibration model. Across **72 recording pairs**, the correlation of mean contact force and mean target optical response is **ρ = 0.520**. Within-taxel values range from **0.238 to 0.905** (eight recordings each).

The positive pooled trend is compatible with stronger loading often accompanying larger optical responses. It is neither a quality score nor causal evidence. Group-specific results differ: TEST7 has **ρ = −0.433** and TEST1_v2 **ρ = −0.150**, each across nine taxels. Pooling can therefore conceal acquisition-group and taxel effects.

The scatterplots and saved group-specific coefficients are descriptive. No Pearson model is imposed because a stable linear relationship is not established. No p-values, ANOVA or normality-screening tests are reported; independence across recordings is not established, there is only one source session, and each within-taxel sample contains eight recording summaries. An independent-session confidence interval is consequently unavailable. No multiple-comparison claims are made. [SciPy's Spearman documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html) describes the coefficient used here.""",'correlations',dict(id='relationship-scatter',title='Force and optical response · 72 recordings',spec=dict(type='scatter',x='force_mean_N',y='optical_mean_delta_v',series='taxel',label='trial_id',xLabel='Mean force (N)',yLabel='Mean optical response (delta V)',valueDecimals=3),query='trials'))
    section('timing','8. Temporal Synchronization',"""The camera and load-cell records contain a shared host-monotonic time base. Recomputing the nearest physical load-cell sample reproduces every stored synchronization offset exactly. Independently interpolating raw force on that time base reproduces synchronized force with maximum absolute numerical difference **1.46 × 10⁻¹² N**.

For **29,318 frames**, signed camera-minus-nearest-load-cell timestamp offset is **mean 1.298 ms**, **median −0.323 ms**, **SD 26.799 ms**, **IQR 43.623 ms**, and **range −80.268 to 83.430 ms**. Median absolute nearest-sample gap is **22.059 ms**, with maximum **83.430 ms**, below the recorded **200 ms** matching gate. There are no non-increasing host timestamps within either stream.

These values assess **timestamp matching**, not physical sensor response time. A deterministic exploratory cross-correlation searches ±1 s after resampling at each recording's median frame interval; its selected lag has median **0.129 s** and range **−0.262 to 0.917 s**. This is approximately frame-scale and depends on waveform shape, repeated loading, interpolation and optical magnification. It cannot identify intrinsic mechanoluminescent latency, and the ±1 s search bound is an analysis setting rather than a validated acceptance threshold. Detailed estimates and boundary flags are saved for inspection.

Representative overlays use R1_TEST1, R5_TEST4 and R9_TEST1_v2, selected by fixed trial identifiers. Optical and force peaks are not automatically paired as if each recording were one press.""",'timing')
    section('anomalies','9. Outlier and Anomaly Assessment',"""Within each taxel, 1.5 × IQR fences were applied separately to recording mean/peak force and mean/peak optical response. This produces **23 variable-level statistical flags**; a recording can appear more than once. Every flagged value is retained. Such a flag does not demonstrate corruption, particularly under variable manual loading and unequal recording duration.

There are **eight trial-label conflicts**: R2_NOCONTACT through R9_NOCONTACT carry the fixed interaction label `Press`, although their names indicate no contact and every force-derived frame is non-contact. The three R1_NOCONTACT_v2 recordings use `none`. The report uses the trial names to distinguish dedicated no-contact recordings, verifies their force states, and preserves all original labels.

No corrupted core row was confirmed. Confirmed problems concern label semantics and insufficient physical-press identifiers. The row/trial-level anomaly log records variable, value, bounds/reason, retention decision and justification. Feature-range checks found no negative or >255 mean positive delta V, and all target labels are in 1–9. Negative tared force is reported separately without automatic exclusion.""",'anomalies')
    section('sessions','10. Session-to-Session Consistency',"""A true session-to-session comparison cannot be performed: all records have the source session label **S3**, one sensing-skin identifier and one acquisition date. Directory names uniquely identify 83 recordings; they do not demonstrate 83 independent experimental sessions. The existing feature-store convention uses the word session for these directories, which must be distinguished from the source label.

Acquisition-order plots and TEST-group descriptive tables are provided instead. They retain all taxels and show changes within the collection period, including four distinct baseline captures and changing force inputs. These are within-session, partly confounded differences; no causal session effect or across-day reproducibility is claimed. The baseline-capture means shift by roughly 11–12% from first to last even though the short no-contact recordings have small within-run V variation. Both scales matter.""",'recordings')
    section('leakage','11. Dataset Leakage / Evaluation Integrity',f"""Two evaluation designs coexist in the workspace and should not be conflated.

**Later manual-only split manifest.** All **83 / 83 directory identities** map one-to-one to the supplied archive. The manifest assigns 54 model-primary recordings (TEST1–TEST6; 24,453 frames), 11 no-contact recordings (1,352 frames) and 18 replay-only recordings (3,513 frames). Recomputing all six outer folds finds **zero recording overlap and zero recording/frame-key overlap** between training and holdout, with all nine taxels on both sides. Each outer fold holds out nine recordings and trains on 45. Source experimental session S3 remains shared in all folds; two baseline IDs are shared in folds 2–6. This is within-session grouped evaluation, not an independent-day validation. The split audit does not certify every fitted transform or later model-tuning decision.

**Legacy linear-model results.** The existing out-of-fold file contains **8,559 rows from nine recordings**, all matching directory identities in the supplied archive. **All nine recordings occur in multiple folds.** The corresponding script partitions each recording into five contiguous blocks. Training and testing therefore share the same physical acquisition sequences; unannotated presses may cross boundaries. This establishes within-recording dependence risk, although exact same-press overlap cannot be counted without press IDs. The script points to an older archive path, so complete source equivalence is not established by matching names alone.

Use the whole-recording or whole-TEST-group folds for within-session evaluation, and collect separate sessions/days for generalization claims. Fit any thresholds, scalers and feature selection on training data only. Existing reported model evaluations were not altered or replaced; the new performance appendix audits only predictions saved in the supplied ZIP.""",'splits')
    section('overall','12. Overall Data-Quality Assessment',"""The supplied manual dataset demonstrates **complete accepted-frame capture records, internally consistent force synchronization, traceable calibration/baseline metadata and equal trial-recording coverage across nine taxels**. These strengths support descriptive exploration of the recorded BAURods processing pipeline and transparent within-session evaluation.

The evidence also shows **unequal manual forces, changing saved baselines, repeated loading within recordings, inconsistent no-contact trial labels, one-session coverage and motion-magnified optical measurements**. Recording-level optical dispersion is quantified, but the dataset does not establish fixed-force per-press repeatability, inter-session reproducibility, absolute optical calibration or intrinsic sensor latency. The saved localization threshold has extremely low coverage and cannot support a broad localization claim based on conditional accuracy alone.

For the thesis, describe these findings as an **exploratory feasibility assessment with qualified data integrity**, not proof that the sensor is superior or universally reliable. Preserve the recording/frame hierarchy, disclose the processing source, retain statistical outliers, and state the evaluation split used beside each performance result.

The most useful follow-up is to annotate physical presses in the existing video and then acquire independent sessions with measured loading ranges and documented camera/processing settings. A multi-point load-cell verification across the observed force range would address the reference-force uncertainty. These are recommendations; they were not fabricated as completed experiments.""",'assessment')
    section('performance','Appendix A. Sensor performance, separate from data quality',f"""This appendix evaluates the **existing thresholded dominant-ROI predictions stored in the supplied ZIP**, not subsequently trained workspace models. The source configurations set a localization threshold of **5 mean delta V**. The force-derived reference class uses **0.05 N**; target ROI is treated as localization truth only during contact.

Of **26,786 contact frames**, **34** have a prediction and all 34 match the selected taxel: conditional accuracy **100%**, coverage **0.126932%**. Counting withheld predictions as failures gives overall correct localization **0.126932%** and macro-F1 **0.002439**. There are no predictions for T2, T5, T7, T8 or T9. Undefined precision for a class with no predictions is set to zero for the explicitly reported macro-F1 convention; it must not be read as a measured false-positive fraction.

{mdtable(perf[['taxel','n','covered','correct','coverage_percent','precision','recall','f1']].rename(columns={'taxel':'Taxel','n':'Contact frames','covered':'Predicted','correct':'Correct','coverage_percent':'Coverage (%)','precision':'Precision','recall':'Recall','f1':'F1'}))}

No independent optical contact-detector output is stored. Prediction availability can be reported only as a **detection proxy**: TP 34, FN 26,752, FP 0 and TN 2,532. This gives sensitivity **0.126932%** and specificity **100%**, relative to the load-cell threshold. It does not validate a dedicated contact detector. Comparing `contact_state_derived` against the same force threshold would be circular. Metrics here are frame-weighted descriptions of dependent observations, with no independent-frame confidence interval.""",'performance',dict(id='performance-chart',title='Contact-frame prediction coverage',spec=dict(type='bar',x='taxel',y='coverage_percent',yLabel='Coverage (%)',valueDecimals=3),query='performance'))
    section('methods','Appendix B. Reproducibility and scope',f"""The original ZIP is preserved. Its SHA-256 is `{s['archive_sha256']}`, matching the repository's recorded source identity before and after the analysis. Tables retain original field precision; displayed values are rounded. A compact frame-level derivative, file hashes, source dictionaries, field profiles, logs and recording summaries accompany this report.

**Verification:** all 21 primary reconciliation checks passed, including full decoding of primary videos. A separate source-read calculation reproduced **360 summary cells** with maximum absolute difference **7.11 × 10⁻¹⁵**, and reproduced the Spearman coefficient by correlating ranks. Counts in tables, figures and performance denominators reconcile. These checks establish numerical and record integrity, not experimental ground-truth accuracy.

**Statistical scope:** sample SD uses n−1; IQR = Q3−Q1; CV = 100 × SD / mean for positive means. CV is not reported for signed timing offsets. Optical CV is a descriptive digital-signal ratio and is not interchangeable with relative physical uncertainty. [NIST's coefficient-of-variation guidance](https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/coefvari.htm) explains the ratio-scale and near-zero-mean limitations. No hypothesis tests or acceptance thresholds were invented.

**Unavailable analyses:** true per-press statistics, independent-session comparisons, baseline-capture temporal variance, multi-point force uncertainty, raw-versus-magnified optical agreement and physical sensor latency. Acquisition counters cannot establish adherence to an unavailable experimental schedule. Existing learned models were not retrained or re-scored.

**Delivery:** fifteen figures are supplied as 320 dpi PNG and vector SVG, with CSV tables and runnable Python scripts. The notebook is a companion interface to the same workflow. The Markdown report and separate thesis subsection are derived from these reviewed results.""",'assessment')
    report='# BAURods Data Quality Report\n\nManual finger-press acquisition · 8 August 2026 · Prepared 5 October 2026\n\n'+summary+'\n\n'+'\n\n'.join(x['text'] for x in sections)
    # Attach corresponding standalone figures within the portable Markdown report.
    links={2:'01_taxel_coverage',3:'08_baseline_captures',4:'02_optical_repeatability',5:'03_force_variability',6:'06_force_optical',7:'12_signal_overlays',9:'10_recording_order',12:'13_localization_confusion'}
    for index,name in links.items():
        text=sections[index]['text'];report=report.replace(text,text+f'\n\n![{sections[index]["title"]}](outputs/figures/{name}.png)')
    (ROOT/'Data_Quality_Report.md').write_text(report,encoding='utf-8')
    thesis=f"""# Data Quality Assessment

The BAURods dataset was assessed to determine whether the recorded measurements were sufficiently complete, traceable and consistent to support an exploratory evaluation of normal-contact detection and discrete taxel localization. Loading was applied manually by finger pressing, and the load cell provided the reference force. The assessment therefore distinguished variation in experimental input from variation in optical response and did not interpret classification accuracy as evidence of measurement integrity.

The supplied archive contained 83 recording directories, 83 unique trial identifiers and 29,318 synchronized camera observations. All records carried the experimental session label S3 and were acquired on 8 August 2026. Seventy-two recordings represented press trials, with eight recordings assigned to each of the nine taxels, while eleven additional recordings were identified as no-contact trials. The latter identification was checked against force-derived contact states. Frame counts varied between taxels because recording durations differed. Thus, the dataset had equal coverage at the trial-recording level, while individual camera frames did not constitute independent experimental replicates.

Integrity checks showed that all 29,318 synchronized rows corresponded to accepted camera frames and fully decoded primary-video frames. The 42,568 raw load-cell observations also matched recorded ingress counts. Required identifiers, timestamps, target labels, reference forces and the nine mean-delta-V optical channels were complete. No duplicate recording/frame keys, skipped frame or load-cell sample identifiers within recordings, non-increasing host timestamps, or corrupted structured files were identified. Completeness relative to recorded acquisition counters was therefore 100%. This result does not establish completeness relative to an unavailable planned trial schedule or account for losses before frames were accepted. Blank optional printer fields and withheld localization predictions were distinguished from missing sensor observations. Eight no-contact trial names conflicted with their fixed interaction label, which remained recorded as Press; these inconsistencies were documented without changing the source files.

Four distinct unloaded baseline captures were retained across the recordings. Their saved mean-V values ranged from 26.254 to 31.734 across taxels and captures, and first-to-last baseline changes ranged from 11.02% to 11.96%. These differences indicated a between-capture shift rather than demonstrating continuous drift or its cause. Within the eleven dedicated no-contact recordings, mean-V coefficients of variation ranged from 0.312% to 0.725%, and first-to-final-window changes ranged from 0.002% to 0.520% across 99 recording/ROI traces. Each window comprised 10% of the recording frames. Consequently, short-term no-contact variation was small on the recorded digital scale, although stability throughout the full collection period could not be inferred. Positive baseline-corrected responses remained nonzero during no contact, with recording/ROI means of 0.502–0.915 delta V. Temporal variation of the original baseline-capture frames was unavailable because the retained NPZ files contained aggregate per-pixel images rather than frame stacks.

Optical consistency was described using the existing target-ROI mean positive delta V. For each press recording, its mean and peak were calculated over valid force-derived contact frames. Across eight recording means per taxel, optical CV ranged from 9.45% for T5 to 28.66% for T3. Peak-response CV was greater, ranging from 29.56% to 102.12%, and was additionally sensitive to unequal recording duration. These quantities characterize dispersion in the recorded pipeline and should not be interpreted as metrological uncertainty. The acquisition configurations identified the exported quantitative source as motion-magnified frames; unprocessed optical response was not independently recomputed in this assessment.

Applied force varied substantially. Mean contact force ranged from 2.149 to 7.459 N between recordings, and within-taxel CV of recording mean force ranged from 9.40% to 36.23%. This variation was retained as a characteristic of manual loading. The recorded 200 g calibration and verification checks supported traceability of the conversion, but no multi-point uncertainty assessment over the observed force range, extending to 22.699 N, was supplied. Small negative tared values were retained rather than automatically treated as invalid. The pooled Spearman correlation between recording mean force and mean target optical response was 0.520 for 72 paired recordings. Within-taxel coefficients ranged from 0.238 to 0.905, whereas some acquisition-group associations were negative. The result was consistent with a directional force–optical relationship in the pooled data, but did not establish causation, a universal calibration function or data quality by itself. Inferential tests were not used because the independent experimental unit and between-session replication were insufficiently established.

Temporal checks independently reproduced the saved nearest-sample offsets and interpolated force values. The signed difference between camera and nearest load-cell host timestamps had a mean of 1.298 ms, median of −0.323 ms, standard deviation of 26.799 ms and range of −80.268 to 83.430 ms across 29,318 frames. All absolute nearest-sample gaps were below the recorded 200 ms synchronization gate. These results supported internal timestamp matching; they did not measure intrinsic sensor response time. Frame acquisition occurred at approximately 7.49–7.50 Hz, and waveform-based lag estimates remained sensitive to repeated manual loading and temporal optical processing.

The absence of explicit physical-press identifiers constrained repeatability assessment. Although 81 contiguous force-threshold runs were observed, force traces exhibited repeated excursions that could remain above the contact threshold, while several recordings began or ended in contact. A threshold run therefore could not be assumed to represent exactly one physical press. Recording-level summaries were used conservatively, and no fixed-force press-to-press or inter-session repeatability claim was made. The 23 IQR-based variable-level outlier flags were retained because no corresponding observation was confirmed to be corrupted.

Evaluation integrity depended on the split design. The later manual-only manifest held complete recording groups out in six outer folds, with zero recording or frame-key overlap between training and holdout. All folds nevertheless shared the source session label S3, limiting inference to within-session evaluation. In contrast, the legacy out-of-fold linear-model output placed all nine represented recordings in multiple folds, creating within-recording dependence. These designs were documented separately without modifying previously reported evaluations.

Sensor performance was examined independently of these data-quality checks. The archived dominant-ROI rule supplied 34 predictions for 26,786 contact frames, corresponding to 0.126932% coverage. All supplied predictions matched their targets, but their conditional accuracy of 100% was not representative of the complete contact population. Counting withheld predictions as failures yielded 0.126932% correct localization and macro-F1 of 0.002439. No independent optical contact-detector output was available; using localization-prediction availability as a detection proxy did not replace a dedicated detection evaluation.

Overall, the dataset demonstrated complete recorded observations, coherent synchronization, traceable metadata and equal taxel coverage at the recording level. It was therefore suitable for descriptive exploration of the recorded BAURods pipeline and qualified within-session evaluation. However, manual force variation, changing baselines, motion-magnified measurements, incomplete press delineation, inconsistent trial labels and the absence of independent sessions limited stronger conclusions regarding intrinsic repeatability, reproducibility, latency and generalizable localization performance. These limitations should accompany the thesis's feasibility findings.
"""
    (ROOT/'Thesis_Data_Quality_Assessment.md').write_text(thesis,encoding='utf-8')
    (ROOT/'Evidence_and_Limitations.md').write_text('''# Strongest supporting evidence

- All 29,318 accepted camera frames reconcile with synchronized records and decoded primary video.
- All 42,568 raw load-cell samples reconcile; required measurements and identifiers are complete.
- No duplicate recording/frame keys, missing within-recording IDs, or non-increasing host clocks were found.
- Force interpolation and synchronization offsets reproduce directly from raw load-cell timestamps.
- Each taxel has eight press recordings; four unique baseline identities and calibration metadata are traceable.
- The later six-fold design has no recording/frame overlap between training and held-out groups.

# Limitations to disclose

- One collection day, one source session S3 and one skin identifier do not support independent-session generalization.
- Physical press boundaries are unannotated; repeated force excursions cannot be equated with single trials or threshold runs.
- Optical measurements are configured as motion-magnified; raw optical agreement and intrinsic latency remain unverified.
- Manual force CV and baseline shifts confound a pure sensor-repeatability interpretation.
- Only a 200 g calibration/verification point is documented, while forces reach 22.699 N.
- Eight no-contact trials retain the contradictory interaction label Press.
- Legacy model results use within-recording blocks and carry dependence/leakage risk.
- The archived predictor covers only 0.127% of contact frames; conditional 100% accuracy must be reported with that coverage.
- Completeness is relative to accepted counters, not the unavailable planned experiment or pre-acceptance capture losses.
''',encoding='utf-8')
    # Focused reports are pointers into the same reviewed evidence, avoiding alternate calculations.
    for name,idxs in [('Dataset_Completeness',[0,1,2]),('Outlier_Anomaly_Report',[8]),('Repeatability_Analysis',[4]),('Force_Variability_Analysis',[5,6]),('Baseline_Stability_Analysis',[3]),('Synchronization_Analysis',[7]),('Leakage_Assessment',[10]),('Localization_Performance',[12])]:
        (ROOT/(name+'.md')).write_text('\n\n'.join(sections[i]['text'] for i in idxs),encoding='utf-8')
    # Reviewed queries have their own grain and exact file provenance.
    perf['Coverage (%)']=perf.coverage_percent
    tr['taxel_number']=tr.taxel.str[1:].astype(int)
    for sec in sections:
        if sec['id']=='performance':sec['chart']['spec']['y']='Coverage (%)'
        if sec['id']=='baseline':sec['chart']['spec']['xLabel']='8 August 2026 · UTC (Manila = UTC+8)'
        if sec['id'] in ['repeatability','force']:
            sec['chart']['spec'].update(type='scatter',x='taxel_number',xLabel='Taxel number (1–9)',label='trial_id')
    datasets={'coverage':dist,'recordings':rec,'integrity':integrity,'baseline':base,'repeatability':opt,'force':force,'joint_summary':joined,
        'trials':tr,'correlations':corr,'timing':read('synchronization_overall'),'anomalies':read('outlier_anomaly_log'),
        'splits':splits,'assessment':pd.DataFrame(findings,columns=['dimension','assessment','evidence']),'performance':perf}
    source_files={'coverage':['master_synchronized.csv','session_config.json'],'recordings':['session_config.json','session_status.json','master_synchronized.csv'],
        'integrity':['session_status.json','master_synchronized.csv','frame_features.csv','loadcell_raw.csv','session_video.mp4'],
        'baseline':['baseline_summary.json','master_synchronized.csv'],'repeatability':['master_synchronized.csv'],'force':['master_synchronized.csv','loadcell_calibration.json'],
        'trials':['master_synchronized.csv'],'correlations':['master_synchronized.csv'],'timing':['master_synchronized.csv','loadcell_raw.csv'],
        'anomalies':['master_synchronized.csv','session_config.json'],'splits':['split_manifest.csv','master_synchronized.csv'],
        'assessment':['master_synchronized.csv','session_config.json','session_status.json'],'performance':['master_synchronized.csv'],'joint_summary':['master_synchronized.csv']}
    snapshot=json.loads((ROOT/'report_app/src/data.json').read_text(encoding='utf-8-sig'))
    snapshot.update(title='BAURods data quality assessment',buildStatus='complete',status='reviewed',generatedAt=s['generated_at'],filters=[])
    snapshot['report']={'title':snapshot['title'],'asOf':'2026-08-08','subtitle':'BAURods · Manual calibration'}
    snapshot['queries']={}
    for k,d in datasets.items():
        snapshot['queries'][k]={'rows':rows(d),'source':{'label':'BAURods manual calibration · '+k,'files':source_files[k],
            'filters':['Supplied Manual Calibration (2).zip; source session S3; 8 August 2026'],
            'evidenceFlow':[{'title':'Read original archive','detail':'SHA-256 '+s['archive_sha256']},
                {'title':'Reproduce','detail':'Run data_quality_analysis.py, supplemental_audit.py and build_report.py from the delivered package.'}],
            'metricDefinitions':[{'label':k.replace('_',' ').title(),'definition':{'trials':'One row per press recording; means and peaks use force-derived contact frames. Eight recordings per taxel; physical press count unresolved.','baseline':'Four unique saved baseline captures × nine ROIs; copied baseline IDs are deduplicated.','performance':'Archived dominant-ROI predictions on valid contact frames; withheld predictions count as failures in all-frame recall/F1.','timing':'Frame timestamp minus nearest raw load-cell host timestamp, in milliseconds; not intrinsic latency.'}.get(k,'Reviewed source-derived statistics; denominators and exclusions are documented in the section.'),
                'componentIds':[x['id'] for x in sections if x['query']==k]+[x['chart']['id'] for x in sections if x['chart'] and x['chart']['query']==k]}],
            'caveats':['Single source session; recording summaries are not independent physical-press replicates.']},
            'methods':[{'language':'text','code':'Read archive tables; preserve source rows; use recorded 0.05 N force contact threshold; aggregate by recording and taxel; sample SD ddof=1; linear quantiles. See reproducible scripts for exact operations.'}]}
    (ROOT/'report_app/src/data.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    content={'summary':summary,'sections':sections}
    (ROOT/'report_app/src/content/report/report-content.json').write_text(json.dumps(content,ensure_ascii=False,indent=2),encoding='utf-8')
    versions=[]
    for package in ['pandas','numpy','scipy','matplotlib','scikit-learn','opencv-python']:
        versions.append(package+'=='+importlib.metadata.version(package))
    (ROOT/'requirements.txt').write_text('\n'.join(versions)+'\n')
    notebook={'cells':[{'cell_type':'markdown','metadata':{},'source':['# BAURods data quality assessment\n','This companion calls the same saved scripts. Source ZIP remains unchanged. Read README.md before running.']},
        {'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':['from pathlib import Path\n','import subprocess, sys\n','root = Path.cwd()\n','assert (root / "data_quality_analysis.py").exists(), "Open notebook in baurods_quality"\n','subprocess.run([sys.executable, str(root / "data_quality_analysis.py"), "--decode-video"], check=True)\n','subprocess.run([sys.executable, str(root / "supplemental_audit.py")], check=True)\n','subprocess.run([sys.executable, str(root / "build_report.py")], check=True)']},
        {'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':['import pandas as pd\n','pd.read_csv(root / "outputs/tables/joint_repeatability_summary.csv")']}],
        'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'},'language_info':{'name':'python','version':'3.12'}},'nbformat':4,'nbformat_minor':5}
    (ROOT/'data_quality_analysis.ipynb').write_text(json.dumps(notebook,indent=2),encoding='utf-8')
    print('Wrote report, thesis subsection, focused reports, notebook, requirements and reviewed app content.')

if __name__=='__main__':main()
