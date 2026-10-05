## 1. Dataset Overview

BAURods is a press-activated hollow-rod mechanoluminescent visuotactile array intended for normal-contact detection and discrete localization across nine taxels. Forces were applied by **manual finger pressing**, with a load cell used as the reference. The source archive contains **2,551 files**: 736 CSV, 913 JSON, 83 NPZ, 332 MP4, 404 PNG and 83 log files; no Excel file is present in the archive.

Each of the 83 recording directories contains a 330-column optical feature table, a 349-column synchronized table and a 46-column raw load-cell table. The feature and master tables describe the same **29,318 frames** and must not be added together as separate observations. There are **83 unique trial IDs**, one source session ID (`S3`), one collection day, and four unique saved baseline IDs. The acquisition window is **14:21:30–17:52:02 Asia/Manila**.

The archived dictionary describes `roiN_delta_v_mean` as mean positive per-pixel baseline-corrected V (digital delta V, 0–255). This existing variable is used without inventing a new sensing metric. Recording summaries use its mean and maximum within valid force-derived contact frames. Force is in newtons. `contact_state_derived` uses the saved **0.05 N** threshold, not the trial-wide interaction label.

**Unit of analysis.** A unique directory is called a recording here. Source `trial_id` labels are retained, but there is no physical press ID. The force traces contain repeated excursions; 81 contiguous above-threshold runs occur across 72 press recordings, with five recordings having multiple runs, five starting in contact and 30 ending in contact. Runs may merge repeated presses when force remains above threshold. Therefore 81 is a threshold-run count, not a defensible press count. The primary summaries give each recording equal weight; per-press repeatability cannot be recovered reliably without event annotation.

The workspace file inventory records existing raw/processed/model artifacts. This report's numerical evidence is taken directly from the supplied manual ZIP; automatic-indentation datasets are not pooled into it. Existing workspace split and prediction files are examined only for evaluation integrity.

## 2. Data Integrity and Completeness

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

## 3. Experimental Coverage

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