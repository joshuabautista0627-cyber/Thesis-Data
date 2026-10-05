# BAURods manual calibration data quality technical report

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

| Setting | Recorded value | Recordings |
| --- | --- | --- |
| Simulation mode flag | False | 83 |
| Quantitative image source | Motion-magnified frame | 83 |
| Magnified pixels used for measurements | True | 83 |
| Requested camera rate in frames per second | 30.000 | 83 |
| Camera stream width in pixels before rotation | 640 | 83 |
| Camera stream height in pixels before rotation | 480 | 83 |
| Printer motion enabled for recording | False | 83 |
| Magnification mode | Color magnification | 83 |
| Configured amplification | 30.000 | 83 |
| Configured lower cutoff in Hz | 1.100 | 83 |
| Configured upper cutoff in Hz | 1.200 | 83 |
| Configured baseline duration in seconds | 3.000 | 83 |
| Configured minimum baseline frames | 20 | 83 |
| Localization threshold in mean delta V | 5.000 | 83 |
| Force contact threshold in N | 0.050 | 83 |
| Maximum matching gap in ms | 200.000 | 83 |
| Recorded serial device identity | HX711_NANO | 83 |

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

### 1. Dataset Overview

BAURods is a press-activated hollow-rod mechanoluminescent visuotactile array intended for normal-contact detection and discrete localization across nine taxels. Forces were applied by **manual finger pressing**, with a load cell used as the reference. The source archive contains **2,551 files**: 736 CSV, 913 JSON, 83 NPZ, 332 MP4, 404 PNG and 83 log files; no Excel file is present in the archive.

Each of the 83 recording directories contains a 330-column optical feature table, a 349-column synchronized table and a 46-column raw load-cell table. The feature and master tables describe the same **29,318 frames** and must not be added together as separate observations. There are **83 unique trial IDs**, one source session ID (`S3`), one collection day, and four unique saved baseline IDs. The acquisition window is **14:21:30–17:52:02 Asia/Manila**.

The archived dictionary describes `roiN_delta_v_mean` as mean positive per-pixel baseline-corrected V (digital delta V, 0–255). This existing variable is used without inventing a new sensing metric. Recording summaries use its mean and maximum within valid force-derived contact frames. Force is in newtons. `contact_state_derived` uses the saved **0.05 N** threshold, not the trial-wide interaction label.

**Unit of analysis.** A unique directory is called a recording here. Source `trial_id` labels are retained, but there is no physical press ID. The force traces contain repeated excursions; 81 contiguous above-threshold runs occur across 72 press recordings, with five recordings having multiple runs, five starting in contact and 30 ending in contact. Runs may merge repeated presses when force remains above threshold. Therefore 81 is a threshold-run count, not a defensible press count. The primary summaries give each recording equal weight; per-press repeatability cannot be recovered reliably without event annotation.

The workspace file inventory records existing raw/processed/model artifacts. This report's numerical evidence is taken directly from the supplied manual ZIP; automatic-indentation datasets are not pooled into it. Existing workspace split and prediction files are examined only for evaluation integrity.

### 2. Data Integrity and Completeness

Every archive member passed its ZIP CRC read. All 736 CSV files, 913 JSON files and 83 NPZ files parsed. All **83 primary videos were decoded** and their combined frame count matched the accepted-frame counters; auxiliary videos were CRC-checked but not frame-decoded. Common fields in each optical feature table and synchronized table agree.

| Check | Observed valid | Expected from recorded counters | Completeness (%) |
| --- | --- | --- | --- |
| Camera/master records | 29318 | 29318 | 100.000 |
| Feature rows | 29318 | 29318 | 100.000 |
| Decoded primary video frames | 29318 | 29318 | 100.000 |
| Raw load-cell samples | 42568 | 42568 | 100.000 |
| Core-complete synchronized rows | 29318 | 29318 | 100.000 |
| Valid synchronized rows | 29318 | 29318 | 100.000 |

There are **zero missing core force, target-label, optical-channel, timestamp, session-ID or trial-ID values**, zero duplicate feature rows, zero duplicate recording/frame keys, zero missing frame IDs within recordings, and zero skipped raw sample IDs within recordings. All source validity flags used for analysis are true; no raw load-cell row is marked invalid. No observation was excluded from the core analysis for an integrity failure.

The completeness denominator is the **recorded accepted/ingress count**, not an unavailable planned acquisition total. This cannot establish that every intended trial was performed or that no frame was dropped before acceptance. Effective accepted-frame rates are **7.489–7.505 Hz**, despite a requested camera rate of 30 Hz; achieved and requested rate are different quantities.

Literal completeness across all 349 columns is **0 / 29,318 rows**, because optional or inapplicable fields are blank. Examples include automated-printer coordinates, sequence identifiers and optional force-class/press-number labels. Blank `predicted_dominant_roi` entries are documented threshold abstentions. These must not be called missing sensor observations. Exact field types, missing counts and source missing-value rules are supplied in the data dictionary and column-profile tables.

### 3. Experimental Coverage

There are **27,966 frames in 72 press recordings**, including **26,786 force-derived contact frames** and 1,180 within-recording no-contact frames. The eleven dedicated no-contact recordings contain another **1,352 frames**. Thus the total no-contact reference is **2,532 frames**.

| Taxel | Recordings | Press-recording frames | Contact frames | Frame share (%) |
| --- | --- | --- | --- | --- |
| T1 | 8 | 3576 | 3454 | 12.787 |
| T2 | 8 | 3217 | 3095 | 11.503 |
| T3 | 8 | 2949 | 2798 | 10.545 |
| T4 | 8 | 3240 | 3130 | 11.585 |
| T5 | 8 | 3324 | 3218 | 11.886 |
| T6 | 8 | 3223 | 3102 | 11.525 |
| T7 | 8 | 2995 | 2862 | 10.709 |
| T8 | 8 | 2763 | 2627 | 9.880 |
| T9 | 8 | 2679 | 2500 | 9.579 |

Recording counts are exactly equal across the nine taxels. Frame counts range from 2,679 to 3,576, a largest/smallest ratio of 1.335; this reflects unequal durations rather than unequal recording counts. Frame-weighted model results can therefore overweight longer sequences. No arbitrary balance threshold is applied. Dedicated no-contact trials are reported separately even though their metadata retains a target ROI.

![3. Experimental Coverage](../outputs/figures/01_taxel_coverage.png)

### 4. Baseline Stability

Four unique baseline captures were reused across 83 recordings; counting all copied baseline rows as independent captures would exaggerate the evidence. Across taxels and captures, saved mean V ranges from **26.254 to 31.734**. The absolute first-to-last capture change is **11.02–11.96%** by taxel. This is a between-capture shift, not a continuously observed drift trajectory or a proven mechanism.

The 11 dedicated no-contact recordings support 99 recording/ROI traces. Their uncorrected mean-V CVs are **0.312–0.725%**; absolute start-to-end changes are **0.002–0.520%**, comparing means over the first and final 10% of frames (ceiling-rounded windows). These show relatively small fluctuations over the observed short recordings; they do not establish stability throughout the entire experiment. Digital V is not calibrated radiance, so optical CVs are descriptive ratios on this processing scale, not metrological uncertainty.

Positive-corrected no-contact response is nonzero: recording/ROI means span **0.502–0.915 delta V**, with CV **5.50–9.14%** and window drift up to **7.39%**. Positive clipping and the processing pipeline must be considered when interpreting low optical responses.

The saved pre-recording drift statistic spans **0.041–0.621 V units**, below the configured acquisition threshold of **5 V units** in every recording. That is a software gate from the files, not a universal data-quality criterion. NPZ files retain per-pixel mean/median baseline images; their summaries' spatial standard deviations are not temporal standard deviations. The original baseline frame stacks are not supplied, so the baseline-capture temporal distribution cannot be reconstructed. Autofocus and automatic white balance are recorded as enabled in all 83 configurations; their causal contribution to baseline shifts is not established.

![4. Baseline Stability](../outputs/figures/08_baseline_captures.png)

### 5. Optical-Response Repeatability

Each taxel contributes **eight recording means** computed over valid force-derived contact frames. The existing target-ROI mean positive delta V is averaged per recording; a separate table summarizes the maximum of that same feature. Between-recording sample SD uses `ddof=1`; CV is 100 × SD / mean. Quartiles use linear interpolation. Outliers remain included.

| Taxel | Recordings | Optical mean ΔV | Optical SD | Optical CV (%) | Force mean (N) | Force SD (N) | Force CV (%) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | 8 | 1.161 | 0.277 | 23.846 | 5.422 | 1.964 | 36.230 |
| T2 | 8 | 1.131 | 0.139 | 12.302 | 5.020 | 1.510 | 30.074 |
| T3 | 8 | 1.682 | 0.482 | 28.655 | 4.588 | 1.215 | 26.486 |
| T4 | 8 | 0.947 | 0.200 | 21.112 | 3.744 | 0.823 | 21.991 |
| T5 | 8 | 0.856 | 0.081 | 9.453 | 3.310 | 0.311 | 9.405 |
| T6 | 8 | 1.163 | 0.287 | 24.709 | 3.781 | 1.154 | 30.518 |
| T7 | 8 | 1.087 | 0.186 | 17.134 | 3.241 | 0.700 | 21.592 |
| T8 | 8 | 1.356 | 0.297 | 21.898 | 3.529 | 0.892 | 25.277 |
| T9 | 8 | 1.467 | 0.248 | 16.931 | 3.709 | 1.087 | 29.299 |

Mean-response CV ranges from **9.45% (T5) to 28.66% (T3)**. Peak-response CV ranges from **29.56% to 102.12%**, with the largest value at T4. Thus means are less dispersed than maxima under this aggregation. Longer recordings provide more opportunities for high maxima, so the peak comparison is duration-sensitive. Lower dispersion here describes these recordings; it does not isolate sensor repeatability at fixed loading.

All optical measurements are identified by `quantitative_analysis_source` as **Motion-magnified frame**, and `magnified_pixels_used_for_exported_measurements` is true throughout. These results characterize the saved transformed signal. Video decoding verifies frame counts, not pixel-level reproduction of every feature. The magnitude and timing of unprocessed optical responses require a separate feature recomputation from original video.

Physical press boundaries, controlled force levels and independent experimental sessions are unavailable. Consequently, a pure same-force, press-to-press repeatability coefficient and inter-session reproducibility claim are **not supported**. The recording-level result is retained as an honest descriptive surrogate.

![5. Optical-Response Repeatability](../outputs/figures/02_optical_repeatability.png)

### 6. Applied-Force Variability

Mean contact force per recording ranges from **2.149 to 7.459 N**. Within-taxel CV of these means ranges from **9.40% (T5) to 36.23% (T1)**. Peak-force means and full descriptive distributions are included separately; synchronized forces reach **22.699 N**.

Manual force variability is an experimental input variation, not by itself evidence of corrupted measurements. It provides an alternative explanation for differences in optical response, together with loading history, duration and baseline changes. No force normalization or model has been used to erase that variation.

The calibration records document a **200 g reference mass (1.96133 N under the recorded conversion)** and passing stored verification checks. The maximum observed force is about 11.6 times that reference force. The supplied records do not provide a multi-point calibration or uncertainty budget covering the full observed range; reference-force accuracy over that range is therefore not established by this audit. Small negative tared forces occur in **2,154 frames**, reaching **−0.190 N**. They are retained as measured offsets/noise, not automatically declared impossible or clamped to zero.

![6. Applied-Force Variability](../outputs/figures/03_force_variability.png)

### 7. Force–Optical Response Relationship

The question is whether paired force and optical recordings exhibit a coherent directional association. Spearman's rank correlation describes monotonic association without imposing a linear calibration model. Across **72 recording pairs**, the correlation of mean contact force and mean target optical response is **ρ = 0.520**. Within-taxel values range from **0.238 to 0.905** (eight recordings each).

The positive pooled trend is compatible with stronger loading often accompanying larger optical responses. It is neither a quality score nor causal evidence. Group-specific results differ: TEST7 has **ρ = −0.433** and TEST1_v2 **ρ = −0.150**, each across nine taxels. Pooling can therefore conceal acquisition-group and taxel effects.

The scatterplots and saved group-specific coefficients are descriptive. No Pearson model is imposed because a stable linear relationship is not established. No p-values, ANOVA or normality-screening tests are reported; independence across recordings is not established, there is only one source session, and each within-taxel sample contains eight recording summaries. An independent-session confidence interval is consequently unavailable. No multiple-comparison claims are made. [SciPy's Spearman documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html) describes the coefficient used here.

![7. Force–Optical Response Relationship](../outputs/figures/06_force_optical.png)

### 8. Temporal Synchronization

The camera and load-cell records contain a shared host-monotonic time base. Recomputing the nearest physical load-cell sample reproduces every stored synchronization offset exactly. Independently interpolating raw force on that time base reproduces synchronized force with maximum absolute numerical difference **1.46 × 10⁻¹² N**.

For **29,318 frames**, signed camera-minus-nearest-load-cell timestamp offset is **mean 1.298 ms**, **median −0.323 ms**, **SD 26.799 ms**, **IQR 43.623 ms**, and **range −80.268 to 83.430 ms**. Median absolute nearest-sample gap is **22.059 ms**, with maximum **83.430 ms**, below the recorded **200 ms** matching gate. There are no non-increasing host timestamps within either stream.

These values assess **timestamp matching**, not physical sensor response time. A deterministic exploratory cross-correlation searches ±1 s after resampling at each recording's median frame interval; its selected lag has median **0.129 s** and range **−0.262 to 0.917 s**. This is approximately frame-scale and depends on waveform shape, repeated loading, interpolation and optical magnification. It cannot identify intrinsic mechanoluminescent latency, and the ±1 s search bound is an analysis setting rather than a validated acceptance threshold. Detailed estimates and boundary flags are saved for inspection.

Representative overlays use R1_TEST1, R5_TEST4 and R9_TEST1_v2, selected by fixed trial identifiers. Optical and force peaks are not automatically paired as if each recording were one press.

![8. Temporal Synchronization](../outputs/figures/12_signal_overlays.png)

### 9. Outlier and Anomaly Assessment

Within each taxel, 1.5 × IQR fences were applied separately to recording mean/peak force and mean/peak optical response. This produces **23 variable-level statistical flags**; a recording can appear more than once. Every flagged value is retained. Such a flag does not demonstrate corruption, particularly under variable manual loading and unequal recording duration.

There are **eight trial-label conflicts**: R2_NOCONTACT through R9_NOCONTACT carry the fixed interaction label `Press`, although their names indicate no contact and every force-derived frame is non-contact. The three R1_NOCONTACT_v2 recordings use `none`. The report uses the trial names to distinguish dedicated no-contact recordings, verifies their force states, and preserves all original labels.

No corrupted core row was confirmed. Confirmed problems concern label semantics and insufficient physical-press identifiers. The row/trial-level anomaly log records variable, value, bounds/reason, retention decision and justification. Feature-range checks found no negative or >255 mean positive delta V, and all target labels are in 1–9. Negative tared force is reported separately without automatic exclusion.

### 10. Session-to-Session Consistency

A true session-to-session comparison cannot be performed: all records have the source session label **S3**, one sensing-skin identifier and one acquisition date. Directory names uniquely identify 83 recordings; they do not demonstrate 83 independent experimental sessions. The existing feature-store convention uses the word session for these directories, which must be distinguished from the source label.

Acquisition-order plots and TEST-group descriptive tables are provided instead. They retain all taxels and show changes within the collection period, including four distinct baseline captures and changing force inputs. These are within-session, partly confounded differences; no causal session effect or across-day reproducibility is claimed. The baseline-capture means shift by roughly 11–12% from first to last even though the short no-contact recordings have small within-run V variation. Both scales matter.

![10. Session-to-Session Consistency](../outputs/figures/10_recording_order.png)

### 11. Dataset Leakage / Evaluation Integrity

Two evaluation designs coexist in the workspace and should not be conflated.

**Later manual-only split manifest.** All **83 / 83 directory identities** map one-to-one to the supplied archive. The manifest assigns 54 model-primary recordings (TEST1–TEST6; 24,453 frames), 11 no-contact recordings (1,352 frames) and 18 replay-only recordings (3,513 frames). Recomputing all six outer folds finds **zero recording overlap and zero recording/frame-key overlap** between training and holdout, with all nine taxels on both sides. Each outer fold holds out nine recordings and trains on 45. Source experimental session S3 remains shared in all folds; two baseline IDs are shared in folds 2–6. This is within-session grouped evaluation, not an independent-day validation. The split audit does not certify every fitted transform or later model-tuning decision.

**Legacy linear-model results.** The existing out-of-fold file contains **8,559 rows from nine recordings**, all matching directory identities in the supplied archive. **All nine recordings occur in multiple folds.** The corresponding script partitions each recording into five contiguous blocks. Training and testing therefore share the same physical acquisition sequences; unannotated presses may cross boundaries. This establishes within-recording dependence risk, although exact same-press overlap cannot be counted without press IDs. The script points to an older archive path, so complete source equivalence is not established by matching names alone.

Use the whole-recording or whole-TEST-group folds for within-session evaluation, and collect separate sessions/days for generalization claims. Fit any thresholds, scalers and feature selection on training data only. Existing reported model evaluations were not altered or replaced; the new performance appendix audits only predictions saved in the supplied ZIP.

### 12. Overall Data-Quality Assessment

The supplied manual dataset demonstrates **complete accepted-frame capture records, internally consistent force synchronization, traceable calibration/baseline metadata and equal trial-recording coverage across nine taxels**. These strengths support descriptive exploration of the recorded BAURods processing pipeline and transparent within-session evaluation.

The evidence also shows **unequal manual forces, changing saved baselines, repeated loading within recordings, inconsistent no-contact trial labels, one-session coverage and motion-magnified optical measurements**. Recording-level optical dispersion is quantified, but the dataset does not establish fixed-force per-press repeatability, inter-session reproducibility, absolute optical calibration or intrinsic sensor latency. The saved localization threshold has extremely low coverage and cannot support a broad localization claim based on conditional accuracy alone.

For the thesis, describe these findings as an **exploratory feasibility assessment with qualified data integrity**, not proof that the sensor is superior or universally reliable. Preserve the recording/frame hierarchy, disclose the processing source, retain statistical outliers, and state the evaluation split used beside each performance result.

The most useful follow-up is to annotate physical presses in the existing video and then acquire independent sessions with measured loading ranges and documented camera/processing settings. A multi-point load-cell verification across the observed force range would address the reference-force uncertainty. These are recommendations; they were not fabricated as completed experiments.

### Appendix A. Sensor performance, separate from data quality

This appendix evaluates the **existing thresholded dominant-ROI predictions stored in the supplied ZIP**, not subsequently trained workspace models. The source configurations set a localization threshold of **5 mean delta V**. The force-derived reference class uses **0.05 N**; target ROI is treated as localization truth only during contact.

Of **26,786 contact frames**, **34** have a prediction and all 34 match the selected taxel: conditional accuracy **100%**, coverage **0.126932%**. Counting withheld predictions as failures gives overall correct localization **0.126932%** and macro-F1 **0.002439**. There are no predictions for T2, T5, T7, T8 or T9. Undefined precision for a class with no predictions is set to zero for the explicitly reported macro-F1 convention; it must not be read as a measured false-positive fraction.

| Taxel | Contact frames | Predicted | Correct | Coverage (%) | Precision | Recall | F1 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | 3454 | 12 | 12 | 0.347 | 1.000 | 0.003 | 0.007 |
| T2 | 3095 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| T3 | 2798 | 13 | 13 | 0.465 | 1.000 | 0.005 | 0.009 |
| T4 | 3130 | 2 | 2 | 0.064 | 1.000 | 0.001 | 0.001 |
| T5 | 3218 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| T6 | 3102 | 7 | 7 | 0.226 | 1.000 | 0.002 | 0.005 |
| T7 | 2862 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| T8 | 2627 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| T9 | 2500 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |

No independent optical contact-detector output is stored. Prediction availability can be reported only as a **detection proxy**: TP 34, FN 26,752, FP 0 and TN 2,532. This gives sensitivity **0.126932%** and specificity **100%**, relative to the load-cell threshold. It does not validate a dedicated contact detector. Comparing `contact_state_derived` against the same force threshold would be circular. Metrics here are frame-weighted descriptions of dependent observations, with no independent-frame confidence interval.

![Appendix A. Sensor performance, separate from data quality](../outputs/figures/13_localization_confusion.png)

### Appendix B. Reproducibility and scope

The original ZIP is preserved. Its SHA-256 is `e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a`, matching the repository's recorded source identity before and after the analysis. Tables retain original field precision; displayed values are rounded. A compact frame-level derivative, file hashes, source dictionaries, field profiles, logs and recording summaries accompany this report.

**Verification:** all 21 primary reconciliation checks passed, including full decoding of primary videos. A separate source-read calculation reproduced **360 summary cells** with maximum absolute difference **7.11 × 10⁻¹⁵**, and reproduced the Spearman coefficient by correlating ranks. Counts in tables, figures and performance denominators reconcile. These checks establish numerical and record integrity, not experimental ground-truth accuracy.

**Statistical scope:** sample SD uses n−1; IQR = Q3−Q1; CV = 100 × SD / mean for positive means. CV is not reported for signed timing offsets. Optical CV is a descriptive digital-signal ratio and is not interchangeable with relative physical uncertainty. [NIST's coefficient-of-variation guidance](https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/coefvari.htm) explains the ratio-scale and near-zero-mean limitations. No hypothesis tests or acceptance thresholds were invented.

**Unavailable analyses:** true per-press statistics, independent-session comparisons, baseline-capture temporal variance, multi-point force uncertainty, raw-versus-magnified optical agreement and physical sensor latency. Acquisition counters cannot establish adherence to an unavailable experimental schedule. Existing learned models were not retrained or re-scored.

**Delivery:** fifteen figures are supplied as 320 dpi PNG and vector SVG, with CSV tables and runnable Python scripts. The notebook is a companion interface to the same workflow. The Markdown report and separate thesis subsection are derived from these reviewed results.

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

| Figure filename | Suggested caption |
| --- | --- |
| 01_taxel_coverage | Camera-frame coverage across the nine taxels in press-designated recordings. Each taxel has eight recordings; frame counts differ. |
| 02_optical_repeatability | Distribution of recording-level target optical summaries by taxel, with observed values and boxplots. Variation includes uncontrolled input-force differences. |
| 03_force_variability | Distributions of manual force recording summaries by taxel. Force variability describes the applied experimental input. |
| 04_mean_sd | Across-recording mean and sample SD of optical and force summaries. Error bars denote dispersion among eight recordings per taxel, not confidence intervals. |
| 05_cv_comparison | CV of optical and force recording means by taxel. No universal acceptance threshold is imposed. |
| 06_force_optical | Paired mean contact force and target optical response for 72 recordings. The pooled Spearman association is descriptive. |
| 07_force_optical_by_taxel | Force–optical association within each taxel, with eight recording summaries per panel. |
| 08_baseline_captures | ROI mean V across four unique saved baseline captures. Differences between captures are not a continuous-drift measurement. |
| 09_no_contact_stability | ROI mean V over time in the earliest and latest dedicated no-contact recordings. These are illustrative traces; all 11 recordings are summarized in the stability table. |
| 10_recording_order | Optical and force summaries in acquisition order within source session S3. Within-session changes do not establish a session effect. |
| 11_timestamp_offsets | Distribution of signed camera-minus-nearest-load-cell timestamp offsets. These are matching diagnostics, not intrinsic physical latency. |
| 12_signal_overlays | Representative force and target optical time series for fixed trial identifiers. Peaks are not assumed to identify independent physical presses. |
| 13_localization_confusion | Confusion counts for the archived thresholded predictor, including a withheld column. The evaluated population is 26,786 contact frames. |
| 14_localization_coverage | Per-taxel prediction coverage for the archived rule, expressed as predicted contact frames divided by all contact frames for that taxel. |
| 15_force_distribution | Distribution of mean contact force across 72 press recordings, with one value per recording. Recording summaries do not establish independent physical-press replication. |

### Numerical tables for the main chapter

The central main-chapter tables are dataset completeness, taxel distribution, the joint optical/force recording-summary table, synchronization statistics, and the separately labeled archived-predictor results. More detailed distributions belong in an appendix if page space is limited. Part III already contains the primary summaries. The following distribution tables add full descriptive context for the principal recording means, with eight recordings per taxel.

#### Optical recording means in digital delta V

| Taxel | n | Mean | SD | Median | IQR | Min | Max | CV (%) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | 8 | 1.161 | 0.277 | 1.052 | 0.334 | 0.918 | 1.727 | 23.846 |
| T2 | 8 | 1.131 | 0.139 | 1.110 | 0.206 | 0.956 | 1.343 | 12.302 |
| T3 | 8 | 1.682 | 0.482 | 1.671 | 0.664 | 0.954 | 2.322 | 28.655 |
| T4 | 8 | 0.947 | 0.200 | 0.903 | 0.143 | 0.712 | 1.344 | 21.112 |
| T5 | 8 | 0.856 | 0.081 | 0.823 | 0.107 | 0.776 | 1.013 | 9.453 |
| T6 | 8 | 1.163 | 0.287 | 1.092 | 0.059 | 0.886 | 1.846 | 24.709 |
| T7 | 8 | 1.087 | 0.186 | 1.088 | 0.163 | 0.862 | 1.465 | 17.134 |
| T8 | 8 | 1.356 | 0.297 | 1.359 | 0.405 | 0.974 | 1.765 | 21.898 |
| T9 | 8 | 1.467 | 0.248 | 1.552 | 0.184 | 0.921 | 1.689 | 16.931 |

#### Force recording means in newtons

| Taxel | n | Mean | SD | Median | IQR | Min | Max | CV (%) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | 8 | 5.422 | 1.964 | 6.062 | 3.052 | 2.666 | 7.459 | 36.230 |
| T2 | 8 | 5.020 | 1.510 | 5.240 | 2.277 | 2.690 | 7.067 | 30.074 |
| T3 | 8 | 4.588 | 1.215 | 4.590 | 1.632 | 2.970 | 6.169 | 26.486 |
| T4 | 8 | 3.744 | 0.823 | 3.654 | 0.741 | 2.684 | 5.453 | 21.991 |
| T5 | 8 | 3.310 | 0.311 | 3.238 | 0.488 | 2.976 | 3.812 | 9.405 |
| T6 | 8 | 3.781 | 1.154 | 3.517 | 1.167 | 2.574 | 6.202 | 30.518 |
| T7 | 8 | 3.241 | 0.700 | 3.246 | 0.638 | 2.216 | 4.501 | 21.592 |
| T8 | 8 | 3.529 | 0.892 | 3.756 | 0.885 | 2.149 | 4.750 | 25.277 |
| T9 | 8 | 3.709 | 1.087 | 3.499 | 0.342 | 2.419 | 6.121 | 29.299 |

The n in these tables is the number of recording means, not the number of frames or independently verified physical presses. Min and max refer to those means, not the within-recording peaks.

## Part VI  Reproducibility

The numerical workflow uses Python, pandas, NumPy, SciPy, Matplotlib, scikit-learn for classification bookkeeping, and OpenCV for primary-video decoding. The exact environment versions used for the completed audit are recorded in `requirements.txt`:

```text
pandas==3.0.5
numpy==2.5.1
scipy==1.18.0
matplotlib==3.11.1
scikit-learn==1.9.0
opencv-python==4.14.0.94
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
