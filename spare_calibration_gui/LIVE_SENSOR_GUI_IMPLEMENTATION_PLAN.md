# Live Optical Force Sensor GUI — Authoritative Implementation Plan

**Status:** Authoritative for manual-only force/localization modeling and live application implementation  
**Reviewed:** 2026-08-13  
**Hardware:** The actual Arducam IMX179 optical sensor is connected to the laptop through USB and has passed read-only discovery/frame capture  
**Goal:** Deliver a user-friendly PySide6 application that detects contact, estimates normal force inside a validated manual-only range, and localizes a single contact to one of nine fixed ROIs.

## 1. Dataset firewall and scope

This application has a strict source whitelist:

```text
Allowed dataset:
Manual Calibration (2).zip

Forbidden GUI/model input:
Auto Calibration.zip
```

The forbidden archive and every artifact derived from it must never be read or used for application-model fitting, preprocessing, feature normalization, lag selection, contact thresholds, `Fdetect`, `Fmax`, optical-support limits, model selection, bundle promotion, replay validation, or application acceptance. Characterization results are a separate thesis track and cannot block, rescue, or tune this plan.

Use from the allowed manual archive only:

- 54 primary press sessions: six complete TEST sessions for every ROI 1–9;
- 11 dedicated no-contact recordings, each observing all nine ROIs;
- 18 manual replay-only recordings for software/parity testing where their role permits; and
- original `session_video.mp4`, synchronized force/timestamps, matching baseline, camera settings, and session metadata.

Any existing mixed-archive inventory, feature store, split, range result, or characterization run is historical evidence and cannot be relabelled as a manual-only application artifact. Create new manual-only run IDs, manifests, feature-store IDs, split hashes, range/model outputs, and provenance. The shared extraction code and immutable source ZIP may be reused after tests confirm identical manual-frame features.

Version 1 supports:

- one normal contact at a time;
- nine fixed, non-editable ROIs;
- force target up to 3.0 N, limited by a manual-only, training-derived common safe range;
- contact state, active ROI number/box, confidence state, and heuristic centroid;
- live sensing, replay/demo, recording, and optional load-cell reference in Validation mode; and
- the same sensing skin, camera, lighting geometry, lens position, orientation, and validated acquisition conditions.

Out of scope: multiple simultaneous force estimates, shear force, exact force above the validated range, another sensing skin/camera/layout, and millimetre-accurate centroid claims without separate spatial calibration.

## 2. Fixed imaging and ROI contract

Quantitative input is the unannotated original frame. Motion magnification is visualization-only.

```text
raw camera frame 640×480
→ rotate 90° clockwise
→ mirror horizontally
→ final analysis frame 480×640
```

Canonical layout ID: `roi-9fbc67c50ee3bc7145fa`.

| ROI | x | y | width | height |
|---:|---:|---:|---:|---:|
| 1 | 98 | 161 | 82 | 89 |
| 2 | 197 | 156 | 76 | 99 |
| 3 | 312 | 154 | 58 | 97 |
| 4 | 100 | 268 | 73 | 89 |
| 5 | 204 | 264 | 76 | 96 |
| 6 | 314 | 260 | 66 | 94 |
| 7 | 104 | 383 | 73 | 77 |
| 8 | 194 | 394 | 87 | 66 |
| 9 | 312 | 379 | 61 | 77 |

The rectangles are already defined after orientation/mirroring. Apply the transform exactly once. Keep the legacy calibration layout separate; inference fails closed if layout ID, dimensions, orientation, transform order, or coordinates differ. Area-normalized features prevent larger rectangles from winning by area alone. ROI 1–9 have equal six-session coverage; ROI 9 requires no additional data based solely on session count. Sharpness is non-blocking provenance and never filters a session or disables inference by itself.

## 3. Manual-only scientific contract

### 3.1 Baseline and optical features

For ROI `r`, frame `t`, HSV V image `V`, and accepted unloaded per-pixel median baseline `B`:

```text
signed_delta_r,t(x,y)   = V_t(x,y) - B_r(x,y)
positive_delta_r,t(x,y) = max(signed_delta_r,t(x,y), 0)
raw_light_r,t           = sum over ROI r of positive_delta_r,t(x,y)
light_r,t               = max(raw_light_r,t - no_contact_floor_r, 0)
```

The immutable manual-only frame store contains, for every ROI, signed sum/mean/median/MAD, positive sum/per-pixel mean, active fraction, weighted centroid, raw V summaries, ROI area, baseline ID, frame/session/TEST identity, synchronized force, phase, settings, and exclusion reason. Do not globally bake floors, scales, lag, thresholds, imputation, `Fdetect`, `Fmax`, calibration, or model transforms into it; fit them inside each training fold.

Live baseline behavior:

- wait the configured 3-second camera warm-up;
- collect unannotated frames for 3 seconds and require at least 20 fresh valid frames;
- compute per-pixel median V and per-ROI unloaded summaries;
- reject contact, excessive spread/drift, stale frames, unsupported lighting, or incompatible camera/layout/model context;
- bind the baseline to device/backend/mode, geometry, processing/feature version, layout, and bundle; and
- invalidate it after restart, camera/mode/layout/bundle/lighting change, or failed drift check.

### 3.2 Contact detection and `Fdetect`

Contact detection is separate from force regression. Zero force is a discrete `No contact` state rather than a regression sample.

Within each outer fold:

1. Fit per-ROI no-contact floors and robust response scales using outer-training press/no-contact sessions only.
2. Choose the smallest contact threshold that satisfies the frozen no-contact false-positive target, then maximizes training/inner-fold contact recall.
3. Derive `Fdetect` from outer-training contact/no-contact evidence only.
4. Apply threshold, two-frame acquire/clear debounce, and `Fdetect` unchanged to the held-out TEST group and held-out no-contact sessions.
5. Report frame false-positive rate, false-contact episodes/minute, contact recall, onset delay, and end-to-end displayed-force error with a missed contact represented as 0.00 N.

Dedicated no-contact sessions are the only controlling threshold/noise evidence. Deterministically assign complete no-contact sessions to outer folds, stratified by collection date where possible; a session may never contribute to both fitted and held-out threshold evidence.

### 3.3 Manual-only lag and safe force range

Define positive lag `L` as optical response trailing the reference, pairing an image feature at `t` with interpolated `F(t-L)`. In every outer fold, search the frozen lag grid using only outer-training TEST groups and the same simple NNLS baseline; select by inner grouped session-balanced MAE, preferring the smaller lag when scores are within 0.5%. Apply one selected global lag to every force candidate in that outer fold. Never optimize lag on the held-out TEST group or live load-cell data.

Determine a fold-specific common safe `Fmax` using only the five outer-training TEST groups:

1. Split loading/unloading from smoothed force derivative; use loading for the range decision.
2. Bin force in 0.25 N intervals and calculate session-balanced median light per ROI.
3. Require at least three independent training sessions for a supported bin.
4. Fit a monotonic segmented response per ROI.
5. Mark a plateau knee when incremental light sensitivity falls below 20% of the initial low-force slope for three consecutive supported bins.
6. Set each ROI safe limit 10% below its knee and the fold common limit to the minimum of 3.0 N and all nine safe limits.
7. If any ROI lacks support, the fold has no eligible common range; record failure without changing rules or looking at held-out outcomes.

Evaluate that fold only from its training-derived `Fdetect` through its training-derived common `Fmax`. Save all six fold ranges and coverage. After model selection, refit the same range procedure on all six manual TEST groups for an experimental bundle; physical known-force/range tests remain required before `validated` status. Never lower the range after seeing outer errors, and never import external characterization evidence to rescue it.

### 3.4 Optical-support guard

Fit the support guard inside every training fold from the ordered manual-only optical feature vector:

- reject wrong shape/order, nonfinite values, impossible nonnegative-feature values, or raw-V saturation/invalid fractions;
- fit session-balanced robust per-feature envelopes using prespecified quantiles/margins;
- fit a joint standardized nearest-reference distance with deterministic sampling stratified by session, ROI, contact state, and force bin;
- tune allowed margin options only in inner folds; and
- produce `Outside calibrated optical response`, no exact force, and no confident localization whenever either guard fails.

The guard detects departure from calibrated support; it is not a universal anomaly detector.

## 4. Model comparison and selection

### 4.1 Validation design

- Outer validation holds out one complete TEST number at a time across all nine ROIs: six outer folds total.
- Inner grouped folds fit/tune every floor, scale, imputation, lag, `Fdetect`, `Fmax`, support envelope, hyperparameter, calibration, and uncertainty threshold.
- Adjacent frames never cross a group boundary or count as independent replicates.
- Balance training by complete session, ROI, and 0.25 N force bin.
- Freeze split manifest, seeds, candidates, search budgets, metrics, and gates before opening outer predictions.
- Save raw per-frame predictions, probabilities, states, timings, fold artifacts, and all six TEST-group metrics for every candidate.
- Calculate uncertainty from complete TEST groups/sessions, never frames; show all six group values because the interval is coarse.

### 4.2 Force candidates

Use the same nine corrected-light inputs and fold-specific valid range for:

| Family | Candidate | Deployment rule |
|---|---|---|
| Simple linear | Total-light zero-intercept regression | Nonnegative slope |
| Physics-aligned linear | Nine-channel NNLS | Nonnegative coefficients |
| Regularized linear | Positive Ridge | Nonnegative coefficients |
| Sparse regularized | Positive Elastic Net | Nonnegative coefficients |
| Monotonic univariate | Isotonic total-light regression | Increasing response |
| Monotonic nonlinear | Constrained piecewise-linear or monotonic spline | Increasing response |
| Monotonic boosting | Histogram gradient boosting | Increasing constraint on all nine inputs |
| Monotonic XGBoost | `XGBRegressor` with nine `+1` constraints | Increasing, nonnegative, resource-safe |
| Exploratory kernel | RBF SVR | Benchmark unless full monotonic audit passes |
| Exploratory trees | Random Forest/Extra Trees | Benchmark unless full monotonic audit passes |
| Exploratory neural | Small MLP | Benchmark unless monotonic, stable, and resource-safe |

Reject deployment candidates with negative predictions, finite-difference monotonic violations, failed range/contact/ROI gates, unsafe serialization, excess latency/size, or failed outer metrics. Rank eligible candidates by session-balanced outer end-to-end displayed-force MAE; report conditional regression MAE separately. If within one standard error, choose the simpler/faster model in the frozen order: constrained linear, regularized linear, monotonic 1D, monotonic piecewise/spline, histogram boosting, XGBoost.

### 4.3 Fixed-ROI localization candidates

Localization is nine-class classification on eligible contact-positive frames, never bounding-box regression. Compare:

- normalized-light argmax baseline;
- multinomial logistic regression;
- k-nearest neighbours;
- RBF support-vector classifier;
- Random Forest and Extra Trees;
- histogram gradient boosting;
- XGBoost nine-class classifier; and
- a small MLP benchmark.

Features include nine corrected sums, per-pixel values, active fractions, normalized spatial profile, top-two margin/ratio, and weighted centroid. Optional previous-frame context is allowed only if it improves outer performance; reset it at every session, reconnect, baseline, pause/resume, or frame gap. Never use force, target label, TEST name, setting label, load cell, or future frames as inputs.

Primary macro accuracy/F1 and per-ROI recall use a forced class on every eligible held-out contact frame. Separately report selective accuracy/F1 versus retained coverage after the training-fitted uncertainty rule. Do not inflate primary performance by dropping uncertain frames. Select highest eligible macro F1, applying the one-standard-error simplicity rule.

## 5. Runtime and application behavior

Reuse the existing camera lifecycle, rotate/mirror path, baseline capture, HSV extraction, ROI validation, synchronization, recording, simulation, and newest-frame processing where their contracts match.

Implement one explicit state machine:

```text
STARTUP → SETUP_BLOCKED → BASELINE_CAPTURING → READY
READY → LIVE_NO_CONTACT ↔ LIVE_CONTACT
LIVE_CONTACT → LIVE_UNCERTAIN | LIVE_LIMIT | LIVE_NO_CONTACT
any live state → PAUSED | RECONNECTING | SETUP_BLOCKED | ERROR
```

- Invalid camera, layout, bundle, baseline, freshness, or optical support clears force/location immediately.
- Ambiguous/likely multi-contact shows an amber fixed box and force `—`.
- At/above the range limit shows a red fixed box and `Force ≥ Fmax — optical limit reached`, never an exact/clamped number.
- No contact shows no active box and `0.00 N` only after the debounced gate passes.
- Valid single contact shows the predicted fixed green ROI box, force, confidence, and optional within-ROI weighted centroid.
- Recording is orthogonal; a recorder fault stops/marks recording but may leave scientifically safe sensing active.

Concurrency contract:

- camera, inference, recording, and export stay off the Qt UI thread;
- inference uses a newest-only queue of size one and monotonically increasing frame IDs;
- preview, overlay, force, and location render only as one result with the same frame ID;
- recording has its own bounded queue and saves unannotated original frames plus synchronized metadata;
- monotonic time governs age, debounce, lag, and latency;
- stale or out-of-order results are discarded and exact outputs cleared after the frozen timeout; and
- JSONL logs preserve state transitions, warnings, frame gaps, bundle/baseline IDs, recording faults, and operator actions.

Replay must be deterministic for identical input, baseline, bundle, config, and seed.

## 6. User-friendly GUI workflow

### Guided Setup

1. Detect the USB-connected sensor without assuming the first camera index; verify Arducam identity/backend/mode and frame read.
2. Apply the fixed orientation and show the final preview.
3. Overlay all nine gray ROI boxes and require operator alignment confirmation.
4. Load and verify the default experimental/validated bundle.
5. Ask the operator to remove all load, then capture and validate a fresh baseline.
6. Run no-contact drift/readiness checks.
7. Enable one prominent **Start Live Sensor** action only when all checks pass.

No ROI editing controls appear.

### Live Sensor

- Keep preview, contact state, large force/unit, active ROI, range warning, baseline/camera/model health, and recording state visible without scrolling.
- Put FPS, latency, IDs, feature values, and diagnostic plots in an expandable Diagnostics drawer.
- Provide Start/Stop recording, Pause sensing, Recapture baseline, Help, and clear recovery actions.
- Replace unavailable force with `—` plus the reason; never retain the previous value.
- Use text and icon/pattern with green/amber/red; never rely on color alone.

### Validation / Advanced

- Optional synchronized optical and load-cell reference traces and error plots.
- Label the load cell `Reference only — not used by optical inference` in the screen and exports.
- No API or button may feed the live load-cell value into optical prediction or correction.

Support keyboard navigation, accessible names, visible focus, logical tab order, Windows scaling 100/125/150/200%, and a minimum 1366×768 display. Operations above 250 ms show progress. Confirm before closing/changing model/camera while recording or discarding a baseline.

## 7. Safe model bundle

Use a versioned directory with schema/checksum validation:

```text
models/live_sensor_v1/
  manifest.json
  roi_layout.json
  preprocessing.json
  preprocessing_arrays.npz
  force_model.json|ubj|onnx
  localization_model.json|ubj|onnx
  calibration.json
  optical_support.json
  validation_summary.json
  release_decision.json
  SHA256SUMS
```

Allow reviewed custom JSON/NPZ, native XGBoost JSON/UBJ, or supported ONNX. Never load pickle/joblib in the application. Verify schema, paths, sizes, hashes, versions, layout, feature/class order, shapes, finite values, thresholds, range, support envelope, runtime backend, and export parity before atomically publishing a bundle. Failed replacement leaves the previous validated bundle unchanged.

Promotion to `validated` requires a gate evaluator that references manual-only source/split/prediction hashes and all applicable software, physical, and usability evidence. If any applicable gate is missing or failed, status remains `experimental`.

## 8. Acceptance gates

| Gate | Target |
|---|---:|
| End-to-end displayed-force MAE over fold-training-derived `Fdetect–Fmax` | ≤ 0.75 N overall |
| Conditional regression MAE | ≤ 0.75 N overall |
| Force MAE for every ROI | ≤ 1.00 N |
| Force RMSE | ≤ 1.00 N |
| Absolute force bias | ≤ 0.20 N |
| Contact recall over validated range | ≥ 90% |
| Dedicated no-contact frame false-positive rate | ≤ 5% |
| False-contact episodes | ≤ 1 per 5 minutes, with archive and bench evidence reported separately |
| Forced localization macro accuracy / macro F1 | ≥ 80% / ≥ 80% |
| Localization recall for every ROI | ≥ 70% |
| Predicted box geometry | 100% exact stored-box match |
| Raw negative predictions / monotonic violations | 0 beyond frozen `-1e-6 N` tolerance |
| Exact output on invalid, stale, OOD, or above-range input | 0 |
| Contact/box acquire, switch, and clear | Within 2 processed frames |
| Selected-model p95 inference | ≤ 30 ms |
| Camera-to-display p95 latency | ≤ 300 ms |
| Bundle size / integrity-load time | ≤ 50 MB / ≤ 2 s |
| 60-minute soak | 0 crashes/deadlocks/unbounded queues/stale valid output; memory growth ≤ 100 MB after warm-up |

Usability gates require at least three formative users before interface lock and at least five representative first-time users afterward: ≥90% unassisted setup/task completion, median first-time ready ≤5 minutes, returning ready ≤2 minutes, 100% optical-limit recognition, zero critical safety errors, and no clipped critical control at supported scaling.

## 9. Implementation workflow and current state

1. Preserve existing source ZIP and shared extractor; create a separate manual-only config, source inventory, output root, and schemas/provenance.
2. Audit all allowed manual sessions and materialize a new manual-only immutable feature store.
3. Freeze a new manual-only split manifest, including TEST groups and held-out no-contact assignments; run leakage tests.
4. Run manual-only lag, `Fdetect`, range/plateau, noise, coverage, and optical-support procedures inside nested folds.
5. If no common safe range exists for any fold or the all-development refit, record the failure and stop force-model promotion without using forbidden evidence.
6. Run force and localization candidate comparisons with the same split/populations and save all outer predictions.
7. Select eligible models using frozen rules and promote an experimental safe bundle.
8. Build deterministic replay and all GUI states, then live camera integration.
9. With operator confirmation of device, alignment, and unloaded/applied-load state, run no-contact, known-force, every-ROI, slow load/unload, ambiguous-contact, above-range, reconnect, recording-failure, drift, latency, and soak tests.
10. Complete accessibility/scaling checks and representative-user testing.
11. Promote to validated only when every applicable manual-only scientific, runtime, physical, and usability gate has traceable evidence.

Current verified prerequisites:

- repository/environment preflight and legacy tests passed in the prior run;
- the immutable manual archive hash/integrity, canonical ROI asset, and shared signed/positive extractor are available;
- read-only sensor discovery found Arducam IMX179 through DirectShow index 0 at 640×480; and
- no manual-only range/model run or eligible application bundle exists yet.

The former characterization gate failure is not an application blocker. The exact next action is a new manual-only inventory/feature/split/range run with new IDs and no forbidden-archive inputs.

## 10. Tests and stop conditions

Required automated tests:

- source firewall rejects any non-whitelisted archive/artifact before loading;
- manual-only run manifests and output hashes contain no forbidden source lineage;
- signed/positive feature fixtures and offline/live feature parity;
- complete-session split exclusivity and fold-fitted transformation leakage tests;
- held-out changes cannot alter training floor, scale, lag, threshold, range, support, calibration, or hyperparameters;
- all force candidates use identical fold populations and resource budgets;
- forced versus selective localization denominators remain separate;
- exact ROI transform/box mapping, area normalization, and class order 1–9;
- stale/OOD/invalid/baseline/limit/ambiguous state transitions clear output;
- preview/overlay/prediction frame-ID atomicity and bounded queues;
- safe loader rejects path traversal, corrupt hashes, unsafe formats, nonfinite arrays, and partial bundles;
- exported model reproduces analysis estimator within frozen tolerance;
- deterministic replay, clean installation, rollback, and 60-minute soak; and
- load-cell values cannot enter optical inference APIs.

Stop the affected phase and retain evidence if the source firewall fails, archive hash changes, frame reconciliation is unexplained, layout/baseline association is unsafe, split leakage occurs, a common manual-only range is unsupported, no model passes, bundle parity/integrity fails, or physical/usability work needs unprovided human action. Never weaken a gate, omit an ROI, shrink the range after viewing outer errors, or import prohibited evidence to manufacture success.

## 11. Definition of done

- **Manual-only analysis complete:** new source/feature/split artifacts, nested range/contact/model results, outer predictions, and figures are reproducible with whitelisted lineage.
- **Model eligible:** one contact/force/localization specification passes the frozen manual-only archive gates and has a supported all-development range.
- **GUI implemented:** Setup, Live, Validation, recording, replay, state machine, accessibility, safety guards, and automated tests work against an experimental bundle.
- **Validated release:** model eligibility plus physical, latency/soak, clean-install/rollback, and representative-user gates pass, with the exact bundle marked `validated`.
- **Thesis claim complete:** all reported application metrics identify the manual-only population and nested TEST-group design; no untouched-test claim is made unless new frozen confirmatory sessions are collected.
