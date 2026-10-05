"""Assemble the thesis-writing handoff from reviewed audit evidence.

This packages the completed manual-calibration audit; it does not train models
or repeat the acquisition. The findings retain the reviewed report verbatim.
"""
from pathlib import Path
from collections import Counter, defaultdict
import csv
import hashlib
import json
import re
import zipfile
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'outputs'
DEST = ROOT / 'thesis_handoff'
DEST.mkdir(exist_ok=True)
summary = json.loads((OUT / 'summary.json').read_text(encoding='utf-8'))
assert summary['archive_sha256'] == 'e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a'
assert all(summary['checks'].values())

def table(frame, digits=3):
    def cell(value):
        if pd.isna(value): return 'Not defined'
        if isinstance(value, float): return f'{value:.{digits}f}'
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['| ' + ' | '.join(map(str, frame.columns)) + ' |',
             '| ' + ' | '.join(['---'] * len(frame.columns)) + ' |']
    lines.extend('| ' + ' | '.join(cell(v) for v in row) + ' |'
                 for row in frame.itertuples(index=False, name=None))
    return '\n'.join(lines)

config_paths = {
    'simulation_mode': 'Simulation mode flag',
    'quantitative_analysis_source': 'Quantitative image source',
    'magnified_pixels_used_for_exported_measurements': 'Magnified pixels used for measurements',
    'camera.requested_fps': 'Requested camera rate in frames per second',
    'camera_actual.actual_width': 'Camera stream width in pixels before rotation',
    'camera_actual.actual_height': 'Camera stream height in pixels before rotation',
    'printer_motion.enabled_for_session': 'Printer motion enabled for recording',
    'motion_magnification.mode': 'Magnification mode',
    'motion_magnification.amplification': 'Configured amplification',
    'motion_magnification.lower_cutoff_hz': 'Configured lower cutoff in Hz',
    'motion_magnification.upper_cutoff_hz': 'Configured upper cutoff in Hz',
    'processing.baseline_duration_s': 'Configured baseline duration in seconds',
    'processing.minimum_baseline_frames': 'Configured minimum baseline frames',
    'processing.localization_min_mean_delta_v': 'Localization threshold in mean delta V',
    'contact_threshold_N': 'Force contact threshold in N',
    'max_sync_gap_ms': 'Maximum matching gap in ms',
    'serial_identity.device_name': 'Recorded serial device identity',
}
counts = defaultdict(Counter)
with zipfile.ZipFile(summary['archive']) as z:
    config_names = [n for n in z.namelist() if n.endswith('/session_config.json')]
    assert len(config_names) == 83
    for name in config_names:
        config = json.loads(z.read(name))
        for path in config_paths:
            value = config
            for key in path.split('.'):
                value = value.get(key) if isinstance(value, dict) else None
            counts[path][json.dumps(value)] += 1
config_rows = [{'Setting': config_paths[path], 'Recorded value': json.loads(value),
                'Recordings': count, 'Source field': path}
               for path in config_paths for value, count in counts[path].items()]
config_frame = pd.DataFrame(config_rows)
config_frame.to_csv(DEST / 'verified_acquisition_settings.csv', index=False)

intro = r'''# BAURods manual calibration data quality technical report

**Purpose:** a self-contained evidence and methodology handoff for drafting the thesis sections on manual-calibration data quality.

**Assessment date:** 5 October 2026, Asia/Manila. **Acquisition date:** 8 August 2026. **Version:** 1.0.

This report documents the assessment already completed on `Manual Calibration (2).zip`. It explains the data source, recorded acquisition context, analytical units, eligibility rules, calculations, verification, findings, and defensible thesis interpretations. It is designed to remain understandable when uploaded without the surrounding chat. It covers the manual-calibration assessment only. A separate technical report covers later model training; that report must remain the source for trained-model architecture, tuning, and performance.

The main conclusion is that the accepted acquisition records are complete and internally consistent enough for descriptive analysis of the recorded BAURods pipeline. However, variable finger-applied force, one source session, changing baselines, motion-magnified optical measurements, and missing physical-press identifiers restrict repeatability and generalization claims. Data integrity and sensor performance are evaluated separately throughout.

## How to use this report

The methods below support the thesis Methodology chapter. The findings section supports Results and Discussion. The claim boundaries, missing-information register, and figure index support careful synthesis. The final writing brief is a suggested instruction for the separate writing tool, not an experimental observation.

All substantive numerical findings are included in the text or tables. The accompanying CSV files provide full precision and more detailed breakdowns. Local paths identify evidence; a writing tool cannot access those paths unless the corresponding files are also supplied. Figure files are optional for understanding the numerical findings but necessary for inserting the actual figures. The `.txt` copy contains the same report text for upload; the `.md` copy supports readable headings, tables, and figure links.

### Evidence categories

| Category | Meaning in this report |
| --- | --- |
| Recorded fact | A field, label, setting, or counter saved in the original acquisition archive. It is evidence of what was recorded, not independent proof of physical accuracy. |
| Recomputed result | A number calculated from the archived rows and checked by the audit workflow. |
| Interpretation | A bounded explanation of what the result supports. Possible causes are not presented as established causes. |
| Unavailable information | A requested quantity that the supplied records cannot establish. It must remain unknown in the thesis until additional evidence is provided. |
| Recommendation | A proposed future action. It must not be rewritten as work already performed. |

## Part I  Scope and experimental context

### Research questions addressed

The assessment asked whether the recorded observations are structurally complete, internally valid, and traceable; how observations are distributed across the nine taxels; how unloaded optical signals and saved baselines vary; how optical-response summaries vary across recordings; how much manually applied force varies; whether force and optical summaries show a directional association; whether camera and load-cell timestamps agree; whether unusual observations indicate invalidity; and whether evaluation splits preserve recording separation. A separate appendix describes the existing archived localization rule and its abstentions.

The assessment did not seek to prove material superiority, validate fixed-force mechanical repeatability, establish intrinsic sensor latency, calibrate absolute optical radiance, or determine independent-day generalization. It did not fabricate unavailable experimental controls or infer the number of physical presses from video-frame counts.

### Source identity and preservation

The primary source was `Manual Calibration (2).zip`, read from `C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\Manual Calibration (2).zip`. Its SHA-256 is:

`e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a`

The original ZIP was not edited. Derived tables, figures, reports, logs, and compact frame data were saved separately under `baurods_quality`. Individual archive-member hashes are retained in `outputs/tables/archive_inventory.csv`. Before-and-after source hashing, member CRC checks, and structured-file parsing form separate integrity checks: hashing identifies content, CRC checks detect archive read failures, and parsing checks establish readable structure.

The source contains 2,551 files: 736 CSV, 913 JSON, 83 NPZ, 332 MP4, 404 PNG, and 83 LOG files. There are 83 recording directories and 83 distinct trial identifiers. All records carry source session label S3, with one sensing-skin identifier and one acquisition date. The observed collection interval is approximately 14:21:30–17:52:02 Asia/Manila on 8 August 2026. This wall-clock span includes gaps and is not continuous recording duration.

### Acquisition process supported by the records

The user-provided experimental description identifies manual finger pressing of a nine-taxel BAURods sensing arrangement. Camera-derived optical measurements and load-cell measurements were recorded, with the selected taxel stored as the trial target. The saved configuration reports printer motion disabled for all 83 recordings, supporting the distinction from automated indentation. The source metadata identify a serial device as HX711_NANO. Exact load-cell model, rated capacity, mounting geometry, fingertip contact geometry, and operator procedures are not established by this audit.

The logical recorded sequence is camera and serial readiness, load-cell calibration and verification, unloaded baseline acquisition, trial labeling and recording, optical feature extraction, timestamp-based force alignment, and exported acquisition artifacts. This sequence describes dependencies supported by the saved settings and outputs. It is not a reconstructed operator log of every physical action.

All configurations state that exported quantitative measurements came from a motion-magnified frame. Therefore, the optical analysis concerns the saved processing pipeline, not a newly computed raw-camera measurement. The original optical features were not recomputed from pixels during this assessment. A source camera description includes an operator-confirmation caveat; the thesis should not turn that candidate label into a confirmed camera-model specification without the researcher's equipment records.

The following settings were read again from all 83 original `session_config.json` files when assembling this handoff. Requested settings and actual observed behavior are distinguished. A configured filter band does not establish a measured sensor frequency response or guarantee that the effective processing transfer function matches its nominal settings.

{{CONFIG_TABLE}}

Autofocus and automatic white balance are recorded as enabled in all 83 recording summaries. Their possible influence on optical measurements is a limitation; this audit did not establish that either caused a particular change. The accepted-frame cadence is about 7.49–7.50 Hz, despite a requested camera rate of 30 Hz. Requested camera rate, accepted-frame cadence, magnification settings, and sensor bandwidth are different quantities.

### Data hierarchy and the experimental unit

| Unit | Observed count or definition | Correct use |
| --- | --- | --- |
| Source experimental session | One label, S3 | Describes the available acquisition session; does not establish across-day replication. |
| Recording | 83 directories with unique identities | Primary traceability unit and the grouping used to avoid counting neighboring frames as independent replicates. |
| Press-designated recording | 72, eight per taxel | Used for equal-weight recording-level optical and force summaries. |
| Dedicated no-contact recording | 11, identified from trial names and checked against force contact states | Used for unloaded temporal-signal assessment. |
| Camera frame | 29,318 | A time-series observation; neighboring frames are dependent. |
| Raw load-cell sample | 42,568 | An independently timestamped force-stream record, not one-to-one with frames. |
| Contact frame | 26,786 under the saved 0.05 N threshold | Eligible time points for contact means and archived localization assessment. |
| Physical press | Not reliably enumerated | Cannot be treated as equivalent to a recording or threshold-connected run. |
| Saved baseline capture | Four unique baseline IDs | Reuse across recordings must not multiply the number of baseline captures. |

Press recordings contain 27,966 frames: 26,786 contact and 1,180 non-contact frames. Dedicated no-contact recordings contribute 1,352 further frames. Thus 29,318 total frames comprise 26,786 contact and 2,532 non-contact observations. The feature CSV and synchronized master CSV represent the same camera frames, not two independent sets of observations.

The force threshold identifies 81 contiguous contact runs across the 72 press recordings. Five recordings contain multiple runs; five begin in contact and 30 end in contact. A physical press can merge with another if force does not cross below the threshold, and an observed run can be truncated by a recording boundary. Consequently, 81 is a count of threshold-connected runs, not a validated press count. Eight recording summaries per taxel are also not proof of eight independent, identically controlled physical presses.

### Core source files and variables

| Artifact or field | Role and interpretation |
| --- | --- |
| `frame_features.csv` | 330-column camera feature table; one row per accepted camera frame. |
| `master_synchronized.csv` | 349-column synchronized table, carrying camera features, labels, force, and matching diagnostics. |
| `loadcell_raw.csv` | 46-column force-stream table; includes raw acquisition quantities and calculated signed force. The word raw does not make all columns unprocessed. |
| `session_config.json` and `session_status.json` | Settings, labels, validity/readiness states, accepted counters, and completion information. |
| `baseline_summary.json` and NPZ baseline assets | Saved baseline identities, ROI summaries, and mean/median images; original baseline frame stacks are unavailable. |
| `loadcell_calibration.json` | Reference mass, tare/calibration metadata, stored quality gates, and verification outcome. |
| `session_video.mp4` | Primary video; fully decoded during integrity verification. |
| `data_dictionary.csv` | Definitions, units, types, and missing-value semantics exported by the acquisition software. |
| `capture_frame_id` | Frame identity, unique within a recording. Combine with recording ID for a globally unique key. |
| `host_monotonic_ns` | Host-monotonic clock used for within-recording temporal reconciliation. |
| `wall_clock_iso` | Acquisition wall time, used for chronological descriptions rather than replacing monotonic timing. |
| `target_roi_ground_truth` | Trial-level selected target, values 1–9. It is the evaluation reference; no independent physical contact-location audit was performed. |
| `roiN_delta_v_mean` | Existing mean positive per-pixel baseline-corrected HSV V for ROI N, in digital delta V units on the saved 0–255 scale. |
| `force_N` | Signed synchronized force at a frame timestamp; calculated force at raw load-cell timestamps in the raw-stream table. |
| `contact_state_derived` | Contact computed from synchronized force, not inferred from the trial interaction label. |
| `predicted_dominant_roi` | Existing archived thresholded localization result; missing predictions represent withheld outputs in this evaluation. |

Here V denotes the HSV value channel, not voltage. Delta V is not displacement, pressure, radiance, luminance in physical units, or electrical volts. The dictionary definition describes a positive per-pixel correction, so the mean of clipped pixel differences is not necessarily the positive part of a difference between two ROI means. This audit used the existing exported feature rather than substituting a new optical formula.

## Part II  Assessment methodology

### End-to-end analytical workflow

1. Identify the supplied ZIP and record its hash; keep the original unchanged.
2. Inventory every member, read it to exercise CRC checks, and parse every CSV, JSON, and NPZ. Retain errors if encountered.
3. Read all recording configurations, statuses, dictionaries, baselines, feature tables, synchronized tables, and load-cell streams.
4. Reconcile recorded counters, frame identities, shared feature columns, primary video frame counts, and clock ordering.
5. Define core completeness and validity; profile every field separately so optional blanks are not mislabeled as lost measurements.
6. Preserve the recording hierarchy, distinguish dedicated no-contact recordings, and calculate equal-weight recording summaries for contact measurements.
7. Quantify taxel coverage, unloaded behavior, recording-level optical and force variability, association, timing, and exploratory anomalies.
8. Inspect existing split manifests and a legacy evaluation artifact solely for overlap/dependence risks. Do not retrain or change model evaluations.
9. Evaluate the archived localization rule separately, including withheld predictions and their denominators.
10. Independently recompute summary values from original synchronized CSV files, reconcile tables and figures, and preserve logs and reproducibility files.

### Integrity and completeness definitions

All archive members were read successfully. Every structured CSV, JSON, and NPZ parsed; NPZ arrays were accessed with pickle disabled. Primary videos were decoded to their readable end and the resulting frame counts compared against synchronized rows and recorded counters. Auxiliary MP4 files received archive CRC checks, but were not all decoded. The assessment does not claim a frame-by-frame human visual inspection of every video or PNG.

The core fields comprise frame ID, monotonic timestamp, elapsed time, wall-clock timestamp, session ID, trial ID, target ROI, synchronized force, and the nine ROI delta-V means. A core-complete row requires all these values. An eligible valid row additionally requires true frame, baseline, load-cell, and synchronization validity flags. Missingness was profiled across the full tables before applying this definition. The original source rows were not dropped or overwritten.

Completeness was calculated as observed valid records divided by the expected count available from the saved acquisition counters, multiplied by 100. This establishes reconciliation of accepted observations. The intended number of experimental presses and any frames never accepted before those counters were formed are unknown. No claim of perfect adherence to an unavailable experimental schedule follows from 100% accepted-record completeness.

Shared feature columns were compared between the feature and master tables, after checking frame alignment. Numeric equality used tolerances of 1e-10 for relative and absolute differences with missing values treated consistently; nonnumeric fields used equality or matching missingness. Duplicate feature rows, duplicate frame IDs, gaps in frame/sample identities, and non-increasing timestamps were inspected. Possible ranges were checked against stored semantics, including target labels 1–9 and positive delta-V means between 0 and 255. Negative tared force was retained and described, not automatically clipped.

Literal completeness across every master-table column was zero because each row contains at least one blank optional or inapplicable field. Printer-related fields in a manual recording and deliberately withheld predictions illustrate why full-row missingness is not an appropriate sole quality criterion. The full field profile remains available for a reader to examine these categories.

### Analysis populations and weighting

For each press-designated recording r, C(r) is the set of core-valid frames with force-derived contact. The target optical series uses the ROI selected by that recording's target label. The principal optical summary is the mean of that existing feature over C(r); the force summary is the mean synchronized force over the same C(r). Peak summaries are the maxima of the same variables over C(r). Peak optical response here means the maximum frame-level ROI mean, not the maximum individual pixel.

```
Recording optical mean: O_bar(r) = sum[O(i), i in C(r)] / number of C(r)
Recording force mean:   F_bar(r) = sum[F(i), i in C(r)] / number of C(r)
Recording optical peak: O_peak(r) = max[O(i), i in C(r)]
Recording force peak:   F_peak(r) = max[F(i), i in C(r)]
```

Each taxel contributes eight recording means. Across-recording statistics assign one weight to each recording, so a longer recording does not dominate the repeatability summary solely through its frame count. Frames within each recording contribute equally to that recording's arithmetic mean; this is not an exact time-integral estimator. Camera cadence is close to regular in the source, but the distinction should remain clear. Means and maxima of force and optical response refer to the same recording; their peaks need not occur at the same instant or physical press.

No new normalization by force, response threshold tuning, imputation, raw-pixel processing, optical detrending, outlier deletion, or classifier fitting was performed. Descriptive numeric reducers coerce unexpected nonnumeric values to missing and omit missing values for that affected statistic; n records the actual count used. In the delivered core data, all required rows were complete and valid, so this rule did not remove core observations.

### Descriptive statistics and formulas

For a set of n eligible recording summaries x:

```
Mean = sum(x) / n
Sample SD = sqrt(sum((x - mean)^2) / (n - 1))
IQR = Q3 - Q1
CV (%) = 100 * sample SD / mean
```

Median, Q1, Q3, minimum, maximum, and n were also saved. Quartiles use the default linear interpolation in the analysis workflow. SD uses n−1. CV is reported only when there is more than one observation and the mean is positive; otherwise it is undefined. Signed synchronization-offset CV was intentionally left undefined because its zero-centered mean makes the ratio misleading. Mean ± SD plots show dispersion, not a confidence interval for a population mean.

Optical CV describes relative dispersion on the saved digital processing scale. It must not be interpreted automatically as metrological relative uncertainty or judged against an invented universal acceptance limit. Lower observed dispersion under these conditions is descriptive; it does not isolate intrinsic sensor repeatability when force, timing, baseline context, and processing can vary.

### Taxel coverage

For each taxel, the analysis counts recordings, distinct trial IDs, distinct source session labels, press-recording frames, and contact frames. The percentage column in the taxel-distribution table uses all 27,966 frames in press-designated recordings as its denominator. Relative-to-equal coverage divides a taxel's frame count by 27,966/9. Dedicated no-contact recordings are described separately. Equal recording counts do not imply equal frame counts, equal force exposure, or equal independent replication.

### Baseline and no-contact stability

Saved baseline summaries were deduplicated using baseline ID and ROI, yielding four distinct captures per ROI. The distribution of these capture-level means was summarized separately from temporal variation within unloaded recordings. The saved `std_v` is spatial variability of the baseline representation, not the temporal SD of the missing original baseline frame stack.

For each of 11 dedicated no-contact recordings and each of nine ROIs, the assessment separately summarized the existing `roiN_mean_v` and `roiN_delta_v_mean` time series. There are 99 recording/ROI combinations per signal family. Start and end estimates are the means of the first and last ceil(0.10 × number of frames) observations, with at least one frame in each window.

```
Absolute window drift (%) = 100 * abs(end-window mean - start-window mean)
                           / abs(start-window mean)
```

This ratio is undefined for a zero start mean. Signed end-minus-start differences are also retained. First-to-last changes in the four saved capture means use the same absolute relative-change form. A difference between two saved captures is not proof of a continuous drift trajectory. Temporal no-contact plots and capture comparisons answer different questions.

### Force and reference calibration

The force analysis uses the signed synchronized force in newtons. The raw force conversion was checked against the saved gram-force quantity using standard gravity: force in N = gram-force × 0.00980665. This is an internal conversion check, not an independent calibration experiment. Saved calibration records identify a 200 g reference, equivalent to 1.96133 N under that conversion, with passing stored verification checks. The audit neither repeated that physical calibration nor assigned a full-range uncertainty from its single reference point.

Manual-force variation was summarized alongside optical variation because it changes the input stimulus. It is not by itself a corrupted-data indicator. Force maxima can also depend on recording duration and the number of loading episodes. Differences in optical peaks must therefore be read with the force and duration context.

### Force and optical association

The main association pairs mean force and mean target optical response for the 72 press recordings. Spearman's rho is the correlation between ranks; tie handling follows the library implementation. Calculations were also made within taxels and TEST groups, and for paired recording peaks. Peak-pair association is an association of recording maxima, not matched-event peak analysis.

The analysis uses descriptive rank association because a stable linear calibration relationship was not established. It reports rho and the recording count, without inferential p-values or independent-session confidence intervals. ANOVA, t-tests, and automatic normality screening were not performed. The one-session design, reused baselines, and unannotated physical presses do not justify treating all frames as independent replicates. Correlation does not quantify data quality, establish causation, or show that force alone explains optical variation.

### Timestamp reconciliation and exploratory waveform alignment

For each frame timestamp, the nearest load-cell timestamp was reconstructed on the recorded host-monotonic clock. The signed offset is frame time minus nearest physical load-cell sample time, converted from nanoseconds to milliseconds. Positive offset means the frame timestamp is later. The absolute gap is a separate nonnegative quantity. On an exactly equidistant pair the reconstruction selects the earlier sample.

Raw force was also interpolated to frame times with NumPy's one-dimensional interpolation, using timestamps relative to the first raw sample to reduce numeric scale. Interior values use linear interpolation; NumPy uses endpoint values outside the raw sample range. Agreement with the stored synchronized force therefore verifies the saved mapping numerically, not a hardware synchronization guarantee or physical sensor latency.

For an exploratory waveform comparison, each press recording was resampled by interpolation onto a grid at its median frame interval. Candidate shifts were integer multiples of that interval within ±1 s, using only overlapping samples at each shift. The shift maximizing Pearson correlation of the two overlapping waveforms was saved. Positive lag compares earlier force to later optical response. This use of Pearson correlation to choose a waveform shift is distinct from imposing a linear force–optical calibration model across recordings.

The search bound is an analysis choice, not a sensor specification. Boundary-hit flags and correlation at the selected lag are retained. Magnification, waveform shape, repetitive loading, interpolation, and frame cadence can all affect this estimate. The result is not intrinsic mechanoluminescent response time. Representative plots use the fixed trial identifiers R1_TEST1, R5_TEST4, and R9_TEST1_v2; the complete dataset remains in the summaries.

### Outliers and label inconsistencies

Within each taxel, separate exploratory fences were calculated for recording mean force, peak force, mean optical response, and peak optical response. A value below Q1−1.5×IQR or above Q3+1.5×IQR was flagged. The rule produces variable-level flags: the same recording can be flagged for more than one variable. It is not a count of unique bad recordings.

All statistical flags were retained. No quality threshold was invented to make a desired number of recordings pass. Dedicated no-contact recordings were identified from the trial ID, checked against force-derived contact, and compared against the fixed trial interaction label. Label conflicts were preserved in the source and documented rather than silently repaired.

### Evaluation-integrity check and separation from trained models

The audit inspected the existing manual-only split manifest and legacy out-of-fold artifact to assess recording/frame overlap. It did not rerun model training, certify all fitted preprocessing steps, or replace the separately reported trained-model evaluations. The manifest's field named `session_id` refers to recording-directory identity; the source acquisition session label remains S3. Confusing these meanings would exaggerate the independence of the evaluation.

For each of six outer folds, training and held-out recording sets were reconstructed, then intersected at the recording-ID and recording/frame-ID levels. Taxel coverage and shared baseline/source-session identities were inspected. A shared baseline or acquisition session limits generalization but is not automatically proof of label leakage. Legacy within-recording blocks establish dependence risk; exact same-press overlap remains unknown without validated press IDs.

### Archived localization and contact-proxy calculations

This calculation concerns only `predicted_dominant_roi` already present in the ZIP. It must never be substituted for the accuracy of later trained models. Eligible reference frames are core-valid contact frames in press-designated recordings. Missing predictions are assigned a bookkeeping label zero, called withheld, solely to retain them in the confusion table. Zero is not a tenth physical taxel.

```
Coverage (%) = 100 * predicted contact frames / all eligible contact frames
Conditional accuracy (%) = 100 * correct predictions / predicted contact frames
Overall correct localization (%) = 100 * correct predictions / all eligible contact frames
Precision(k) = TP(k) / (TP(k) + FP(k))
Recall(k) = TP(k) / (TP(k) + FN(k))
F1(k) = 2 * precision(k) * recall(k) / (precision(k) + recall(k))
Macro-F1 = arithmetic mean of F1 over T1 through T9
```

Withheld outputs count as false negatives for their true class. For classes without predictions, mathematical precision is undefined; the implementation sets it to zero under an explicit zero-division convention for the reported class metrics and macro-F1. Conditional accuracy remains undefined when no prediction exists. Missingness must not otherwise be converted into zero measurements.

Availability of any localization output is additionally compared against force-defined contact as a detection proxy, using all 29,318 core-valid frames. It is not a separate validated optical contact detector. Comparing force-derived contact to the same force threshold would be circular. All these performance proportions are frame-weighted descriptions of dependent time-series observations; no independent-frame confidence intervals are claimed.

## Part III  Verified findings and interpretation

The following findings reproduce the reviewed assessment. Section-level figure links point to the existing exports. Numerical tables retain exact counts and rounded presentation values; the associated CSV files retain stored precision. References to model-related artifacts below concern only the bounded evaluation-integrity check.

'''

findings = (ROOT / 'Data_Quality_Report.md').read_text(encoding='utf-8')
# Retain the complete substantive reviewed findings without duplicating its title/summary.
start = findings.index('## 1. Dataset Overview')
findings = findings[start:]
findings = re.sub(r'^## ', '### ', findings, flags=re.M)
findings = findings.replace('(outputs/figures/', '(../outputs/figures/')

closing = r'''

## Part IV  Evidence traceability and thesis use

### Traceability from questions to outputs

All paths in the following table are relative to `baurods_quality/outputs`. CSV files are evidence for the calculation indicated; they do not independently validate experimental assumptions.

| Question | Primary output | Relevant method or boundary |
| --- | --- | --- |
| What is present and readable? | `tables/archive_inventory.csv` | CRC, structured parsing, file sizes and per-member hashes. |
| What does each field mean? | `tables/source_data_dictionary.csv`, `tables/data_dictionary_with_profile.csv` | Original definitions and observed field profiles. |
| Are records complete? | `tables/dataset_completeness.csv`, `tables/column_profile_by_recording.csv` | Accepted counters and core fields versus optional blanks. |
| Are all taxels represented? | `tables/taxel_distribution.csv`, `tables/completeness_by_taxel.csv`, `tables/completeness_by_group.csv` | Separate recording, frame, contact, and source-session counts. |
| What is one recording? | `tables/recording_summary.csv` | Identity, group, target, duration, counts, means, peaks, quality flags, settings. |
| How do baselines differ? | `tables/unique_baselines.csv`, `tables/baseline_across_captures.csv`, `tables/baseline_first_last_change.csv` | Four unique saved identities, not 83 distinct captures. |
| How stable are unloaded traces? | `tables/no_contact_baseline_stability.csv` | First/final 10% windows and time-series descriptive statistics. |
| How variable are optical recordings? | `tables/optical_repeatability.csv`, `tables/joint_repeatability_summary.csv` | Equal-weight recording summaries; no fixed-force replication claim. |
| How variable is input force? | `tables/force_variability.csv`, `tables/calibration_provenance.csv` | Recorded force distributions and saved calibration provenance. |
| Does force covary with response? | `tables/force_optical_correlations.csv` | Descriptive rank association by overall/taxel/TEST group. |
| Do timestamps reconcile? | `tables/synchronization_overall.csv`, `tables/synchronization_by_recording.csv` | Signed nearest-sample offsets and absolute gaps. |
| What waveform lag is selected? | `tables/exploratory_cross_correlation.csv` | Grid, shift, selected correlation and boundary flag; not intrinsic latency. |
| What was unusual or conflicting? | `tables/outlier_anomaly_log.csv` | Flag identity, value, reason, status, and retention justification. |
| Is there independent-session evidence? | `tables/session_descriptives.csv`, `tables/trial_group_descriptives.csv` | One source session; groups are within-session comparisons. |
| Do evaluation partitions overlap? | `tables/verified_split_audit.csv`, `tables/existing_split_roles.csv` | Recording/frame-key overlap and shared source context. |
| What does the archived predictor do? | `tables/localization_metrics.csv`, `tables/localization_confusion_counts.csv`, `tables/contact_detection_proxy.csv` | Abstentions retained; no trained-model re-evaluation. |
| Was the analysis verified? | `validation_checks.json`, `supplemental_validation.json`, `delivery_verification.json`, `analysis.log` | 21 checks, independent recomputation, and delivery review. |

The compact derivative `processed/audited_frames.csv.gz` preserves recording ID and original CSV row number for tracing included measurements back to the archive. It is not a replacement for the original source. The `figure_data_*.csv` tables support the interactive report displays.

### Verification performed and its limits

All 21 primary checks passed. The independent supplemental calculation reread the original master CSVs, recalculated mean/peak force and optical summaries, and independently reconstructed means, sample SDs, quartiles, ranges, and CVs across taxels. It compared 360 summary cells with a maximum absolute difference of 7.1054×10⁻¹⁵. A separate rank-correlation calculation reproduced rho = 0.5201620683. This is an independent implementation check within the audit workflow, not an independent laboratory replication.

All 15 static figures were visually reviewed, with detailed inspection of representative dense plots. The visual report was checked at desktop and narrow-screen widths; its six charts, four semantic tables, source inspector, and legend interaction were reviewed. These rendering checks assess presentation and consistency, not the physical truth of acquisition labels. The delivered Python scripts were executed. The notebook is an unexecuted convenience interface to those scripts and should not be described as an executed notebook record.

### Recommended thesis placement

| Thesis section | Material to use | Writing emphasis |
| --- | --- | --- |
| Methodology | Parts I and II | Source, acquisition context, analysis populations, formulas, selection rules, integrity checks, and why descriptive statistics were used. |
| Results | Part III numeric findings and referenced tables | State observed values with the correct unit and denominator. Separate core completeness from all-column missingness. |
| Discussion | Interpretations in Part III and claim boundaries below | Explain confounding by manual force, baseline changes, processing, one-session coverage, and missing press IDs. |
| Limitations | Unavailable-information register | Distinguish limitations of the dataset, assessment scope, reference measurement, and evaluation design. |
| Conclusion | The bounded overall assessment | Summarize concrete integrity strengths and the analyses they support. Avoid universal reliability claims. |
| Appendices or supplement | Scripts, source dictionary, detailed CSVs, logs, and full figure set | Enable readers to inspect provenance and reproduce calculations. |

The user already has a model-training technical report. Incorporate that source separately when drafting its chapter. Do not merge the 0.127% archived prediction coverage with learned-model accuracy, use it to overwrite later model results, or treat it as the performance of all BAURods localization approaches.

### Claim boundaries

| Supported wording | Unsupported extension to avoid |
| --- | --- |
| All accepted camera/master records reconcile with saved counters and decoded primary video counts. | No measurement was ever missed at any stage, or every planned press was captured. |
| Required fields and saved validity flags are complete for all 29,318 synchronized rows. | Every optional field is present, or every physical measurement is accurate. |
| Each taxel contributes eight press-designated recordings. | Each taxel has eight independent standardized physical presses. |
| Optical recording-mean CV ranges from 9.45% to 28.66% under variable manual loading. | Intrinsic fixed-force sensor repeatability has been established. |
| Short no-contact V traces show low observed within-run dispersion. | The optical baseline was constant throughout the session or across days. |
| Pooled mean-force/mean-response rank correlation is positive. | Force causes all optical changes or the sensor has a validated linear calibration. |
| Timestamp offsets and interpolated force reproduce the saved synchronization. | Physical sensor latency is 1.298 ms or 0.129 s. |
| Grouped folds have zero recording/frame-key overlap. | Every preprocessing and tuning step is leakage-free or the test is cross-session. |
| All 34 issued archived predictions are correct, with 0.126932% contact-frame coverage. | The sensor has 100% overall localization accuracy. |
| The saved metadata describe non-simulated acquisition with printer motion disabled. | The audit independently witnessed every hardware operation or verified all equipment specifications. |

### Information that must remain unresolved until the researcher supplies evidence

1. Physical construction, material composition, dimensions, fabrication procedure, and mechanical mounting beyond this archive's recorded identifiers.
2. Confirmed camera model, lens, working distance, lighting conditions, and environmental measurements; a candidate device label is insufficient.
3. Load-cell model, capacity, traceable reference-mass uncertainty, multi-point calibration over the observed range, and uncertainty budget.
4. Number of operators or participants, their characteristics, and the experimental procedure for finger placement, loading rate, dwell, recovery, and repositioning. A specimen-or-participant label does not establish these details.
5. Intended experimental schedule and expected total presses, validated physical press boundaries, and the independence of individual loading episodes.
6. Independent source sessions, days, sensing skins, or held-out operating conditions; only S3 is present in the audited source.
7. Temporal variation within original baseline-capture frame stacks, which were not provided as stacks in the saved NPZ representation.
8. Agreement between raw-frame optical measurements and the motion-magnified exported features, along with measured processing delay or transfer response.
9. A validated external timing reference and event-definition protocol required to infer intrinsic physical response latency.
10. Independent verification of physical contact position. The selected target label is the stored localization reference, not a separate measured coordinate.

These gaps do not invalidate all descriptive analysis. They constrain the scope of the thesis claims. New experiments, external equipment documentation, or researcher clarification can address them, but no such evidence was fabricated for this report.

### Practical improvements proposed from the audit

Retain the archive and its hashes. Preserve the documented no-contact label conflict and use an explicit derived label in any future cleaned copy. Annotate physical presses with traceable video/frame boundaries before presenting per-press results. For stronger repeatability evidence, collect independent sessions with documented loading ranges and acquisition settings. Evaluate the reference-force system at multiple points spanning the observed range. Compare raw and magnified optical features if material-response claims depend on the processing choice. Fit all model-dependent transforms only on the appropriate training partition and state the grouping level next to every performance result. These are future recommendations, not completed work.

## Part V  Figure and table handoff

### Figure index and suggested captions

Each numbered figure exists as a 320 dpi PNG and a vector SVG under `outputs/figures`. The filenames below omit the extension. Thesis numbering can be adapted to the final chapter. Retain the analysis unit and caveat in each caption; do not present SD bars as confidence intervals.

{{FIGURE_TABLE}}

### Numerical tables for the main chapter

The central main-chapter tables are dataset completeness, taxel distribution, the joint optical/force recording-summary table, synchronization statistics, and the separately labeled archived-predictor results. More detailed distributions belong in an appendix if page space is limited. Part III already contains the primary summaries. The following distribution tables add full descriptive context for the principal recording means, with eight recordings per taxel.

#### Optical recording means in digital delta V

{{OPTICAL_TABLE}}

#### Force recording means in newtons

{{FORCE_TABLE}}

The n in these tables is the number of recording means, not the number of frames or independently verified physical presses. Min and max refer to those means, not the within-recording peaks.

## Part VI  Reproducibility

The numerical workflow uses Python, pandas, NumPy, SciPy, Matplotlib, scikit-learn for classification bookkeeping, and OpenCV for primary-video decoding. The exact environment versions used for the completed audit are recorded in `requirements.txt`:

```text
{{REQUIREMENTS}}
```

Run the following from the `baurods_quality` directory in the original workspace:

```powershell
& '..\calibration_gui\.venv\Scripts\python.exe' '.\data_quality_analysis.py' --archive 'C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\Manual Calibration (2).zip' --decode-video
& '..\calibration_gui\.venv\Scripts\python.exe' '.\supplemental_audit.py'
& '..\calibration_gui\.venv\Scripts\python.exe' '.\build_report.py'
& '..\calibration_gui\.venv\Scripts\python.exe' '.\build_technical_handoff.py'
```

On another machine, use a Python environment with the supplied dependencies and change the archive path. The main script accepts `--archive` and `--output`; the report builders expect the default `outputs` location. Omitting `--decode-video` leaves that check explicitly unperformed. The completed delivered run included the option. The supplemental script expects the original workspace's split manifest and legacy prediction file; its evaluation-integrity findings cannot be regenerated from the ZIP alone. The report builders deliberately preserve reviewed source-specific prose and must not be applied unreviewed to a different dataset. The archive hash guard rejects a different primary source.

The compact thesis-handoff ZIP contains the text, tables, figures, and audit scripts. Rebuilding the earlier interactive report with `build_report.py` additionally requires its existing `report_app` source tree, supplied in the separate complete Data Quality Package. If using only the compact handoff on another machine, preserve the included reviewed `Data_Quality_Report.md`; the handoff builder reads that text and does not need to rebuild the interactive app.

The analysis uses a fixed seed of 20261005 for any seeded graphical jitter. Summary statistics and the deterministic timing/correlation calculations do not rely on a Monte Carlo procedure. The workflow saves output tables separately, retains original row references in the derivative, and does not modify model-training results.

### Primary evidence and methodological references

The acquisition archive, exported data dictionaries/configurations, reproducible audit scripts, CSV results, and verification JSON files are the primary evidence for this report's numerical findings. The existing split manifest and legacy out-of-fold file are the bounded supplementary evidence for evaluation-integrity findings, with hashes and paths preserved in `supplemental_validation.json`.

The following methodological sources support the use and interpretation of the corresponding statistical tools; neither supplies a sensor-specific acceptance threshold:

- NIST, *Coefficient of Variation*: https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/coefvari.htm
- SciPy, *scipy.stats.spearmanr*: https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html

For the final thesis, apply the institution's citation style and verify any additional literature independently. Do not invent publication years, DOIs, sensor standards, or literature results. Internal artifact names are evidence pointers, not substitutes for external scholarly citations where theory requires them.

## Part VII  Writing brief for the separate thesis tool

Use the following as a drafting brief after attaching this report. It can also be found in `ChatGPT_Writing_Prompt.txt`.

> Draft the Methodology and Results and Discussion sections for the manual-calibration data quality assessment of my BAURods thesis using the attached technical report as the factual source. Keep this scope separate from my model-training report. Explain what was checked, why it was checked, how it was computed, what was found, and what the findings permit us to conclude. Use formal engineering-thesis prose, clear subsection headings, equations where useful, and suggested table/figure placements. Preserve exact sample sizes, units, denominator definitions, processing provenance, and limitations. Distinguish camera frames, load-cell samples, recordings, physical presses, TEST groups, and source session S3. Describe manual finger pressing accurately. Do not infer controlled indentation, fixed-force repeatability, multiple independent sessions, raw optical measurements, intrinsic sensor latency, or full-range calibration uncertainty. Treat the 34 archived localization predictions and their 0.126932% coverage as a separate archived-rule result, never as the later trained-model performance. Explain undefined class precision and the zero-division reporting convention if discussing that table. Do not invent methods, results, references, experimental controls, significance tests, or missing equipment details. When information needed for a section is absent, mark it as [RESEARCHER TO SUPPLY: specific detail] and continue with supported material. Keep hypotheses and proposed future experiments distinct from completed observations. End with a concise list of unresolved researcher inputs and any source conflicts requiring reconciliation. Use the supplied numerical tables and figure index; do not recreate numerical results from visual estimation. Draft these assessment sections, not an entire thesis or new model-training chapter.

### Suggested thesis conclusion for this assessment

The manual-calibration dataset demonstrated complete accepted-record reconciliation, valid core fields, internally consistent timestamp-based force alignment, and equal recording coverage across the nine taxels. These properties support descriptive analysis and transparent within-session evaluation of the saved BAURods processing pipeline. Optical variability was nevertheless observed under unequal manually applied forces and changing baseline context, while the exported measurements were derived from motion-magnified frames. Because the source contains one experimental session and lacks validated physical-press identifiers, the results do not establish fixed-force per-press repeatability, independent-session reproducibility, or intrinsic sensor latency. The archived localization rule further illustrates why conditional accuracy must be reported with coverage. The assessment therefore provides qualified evidence for exploratory analysis and identifies the controls and additional records needed for stronger sensing claims.
'''

figure_descriptions = [
('01_taxel_coverage', 'Camera-frame coverage across the nine taxels in press-designated recordings. Each taxel has eight recordings; frame counts differ.'),
('02_optical_repeatability', 'Distribution of recording-level target optical summaries by taxel, with observed values and boxplots. Variation includes uncontrolled input-force differences.'),
('03_force_variability', 'Distributions of manual force recording summaries by taxel. Force variability describes the applied experimental input.'),
('04_mean_sd', 'Across-recording mean and sample SD of optical and force summaries. Error bars denote dispersion among eight recordings per taxel, not confidence intervals.'),
('05_cv_comparison', 'CV of optical and force recording means by taxel. No universal acceptance threshold is imposed.'),
('06_force_optical', 'Paired mean contact force and target optical response for 72 recordings. The pooled Spearman association is descriptive.'),
('07_force_optical_by_taxel', 'Force–optical association within each taxel, with eight recording summaries per panel.'),
('08_baseline_captures', 'ROI mean V across four unique saved baseline captures. Differences between captures are not a continuous-drift measurement.'),
('09_no_contact_stability', 'ROI mean V over time in the earliest and latest dedicated no-contact recordings. These are illustrative traces; all 11 recordings are summarized in the stability table.'),
('10_recording_order', 'Optical and force summaries in acquisition order within source session S3. Within-session changes do not establish a session effect.'),
('11_timestamp_offsets', 'Distribution of signed camera-minus-nearest-load-cell timestamp offsets. These are matching diagnostics, not intrinsic physical latency.'),
('12_signal_overlays', 'Representative force and target optical time series for fixed trial identifiers. Peaks are not assumed to identify independent physical presses.'),
('13_localization_confusion', 'Confusion counts for the archived thresholded predictor, including a withheld column. The evaluated population is 26,786 contact frames.'),
('14_localization_coverage', 'Per-taxel prediction coverage for the archived rule, expressed as predicted contact frames divided by all contact frames for that taxel.'),
('15_force_distribution', 'Distribution of mean contact force across 72 press recordings, with one value per recording. Recording summaries do not establish independent physical-press replication.'),
]
fig_table = table(pd.DataFrame(figure_descriptions, columns=['Figure filename', 'Suggested caption']))
def descriptive_table(filename, variable):
    df = pd.read_csv(OUT / 'tables' / filename)
    df = df.loc[df.variable.eq(variable), ['taxel','n','mean','sd','median','iqr','minimum','maximum','cv_percent']]
    return table(df.rename(columns={'taxel':'Taxel','n':'n','mean':'Mean','sd':'SD','median':'Median','iqr':'IQR','minimum':'Min','maximum':'Max','cv_percent':'CV (%)'}))
report = intro.replace('{{CONFIG_TABLE}}', table(config_frame.drop(columns='Source field'))) + findings + closing
report = report.replace('{{FIGURE_TABLE}}', fig_table)
report = report.replace('{{OPTICAL_TABLE}}', descriptive_table('optical_repeatability.csv','optical_mean_delta_v'))
report = report.replace('{{FORCE_TABLE}}', descriptive_table('force_variability.csv','force_mean_N'))
report = report.replace('{{REQUIREMENTS}}', (ROOT / 'requirements.txt').read_text().strip())
assert '{{' not in report
name = 'BAURods_Manual_Calibration_Technical_Report'
(DEST / f'{name}.md').write_text(report, encoding='utf-8')
(DEST / f'{name}.txt').write_text(report, encoding='utf-8')
prompt = re.search(r'^> Draft the Methodology.*$',report,re.M).group()[2:]
(DEST / 'ChatGPT_Writing_Prompt.txt').write_text(prompt+'\n',encoding='utf-8')
readme = '''# Manual calibration thesis writing handoff

Upload BAURods_Manual_Calibration_Technical_Report.txt as the self-contained factual source. The Markdown copy contains the same content. Paste ChatGPT_Writing_Prompt.txt as the drafting request. Your separate model-training report remains the source for later trained models.

The main report includes acquisition context, the complete audit workflow and formulas, verified findings, interpretation limits, source pointers, figure captions, reproducibility instructions, and missing researcher inputs. The package adds the 35 existing CSV evidence tables, 15 PNG/SVG figure pairs, verification records, source analysis scripts, requirements, and a file-hash manifest. It does not include the original 1.28 GB archive or the existing workspace model files required to repeat the split audit.

This handoff packages the assessment already completed; it is not a newly collected experiment, new model evaluation, or additional independent laboratory validation. The extra acquisition-setting table was reread from all 83 original configurations for this handoff. No original acquisition or trained-model files were changed.
'''
(DEST/'README.md').write_text(readme,encoding='utf-8')

# Check every referenced figure exists and the principal denominators reconcile.
for filename,_ in figure_descriptions:
    for ext in ['png','svg']:
        assert (OUT/'figures'/f'{filename}.{ext}').exists()
assert summary['frames'] == summary['contact_frames'] + summary['no_contact_frames']
assert summary['press_recordings'] + summary['no_contact_recordings'] == summary['recordings']
cov = pd.read_csv(OUT/'tables/taxel_distribution.csv')
assert int(cov.frames.sum()) + summary['no_contact_recording_frames'] == summary['frames']
assert int(cov.contact_frames.sum()) == summary['performance']['n']
assert cov.recordings.eq(8).all()
assert all(sum(c.values()) == 83 for c in counts.values())
check = {'scope':'Completed manual-calibration data quality assessment only',
    'source_archive_sha256': summary['archive_sha256'],
    'report_words':len(report.split()), 'configuration_files_reread':83,
    'source_table_count':len(list((OUT/'tables').glob('*.csv'))),
    'figure_pairs':len(figure_descriptions), 'figures_exist':True,
    'counts_reconcile':True, 'reviewed_findings_preserved':True,
    'report_sha256':hashlib.sha256(report.encode('utf-8')).hexdigest()}
(DEST/'handoff_verification.json').write_text(json.dumps(check,indent=2),encoding='utf-8')

files = list(DEST.glob('*'))
files = [p for p in files if p.is_file() and p.name != 'PACKAGE_MANIFEST.csv']
files += list((OUT/'tables').glob('*.csv')) + list((OUT/'figures').glob('*.png')) + list((OUT/'figures').glob('*.svg'))
files += [OUT/n for n in ['summary.json','validation_checks.json','supplemental_validation.json','delivery_verification.json','analysis.log']]
files += [ROOT/n for n in ['data_quality_analysis.py','supplemental_audit.py','build_report.py','build_technical_handoff.py','requirements.txt','README.md','Data_Quality_Report.md','Thesis_Data_Quality_Assessment.md']]
with (DEST/'PACKAGE_MANIFEST.csv').open('w',newline='',encoding='utf-8') as f:
    w=csv.writer(f);w.writerow(['path_relative_to_baurods_quality','bytes','sha256'])
    for p in sorted(files):w.writerow([p.relative_to(ROOT).as_posix(),p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest()])
files.append(DEST/'PACKAGE_MANIFEST.csv')
archive_path=ROOT.parent/'BAURods_Manual_Calibration_Thesis_Handoff.zip'
with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_DEFLATED,compresslevel=6,strict_timestamps=False) as z:
    for p in files:z.write(p,Path(ROOT.name)/p.relative_to(ROOT))
with zipfile.ZipFile(archive_path) as z:assert z.testzip() is None
print(json.dumps({**check,'package':str(archive_path),'package_files':len(files),'package_bytes':archive_path.stat().st_size},indent=2))
