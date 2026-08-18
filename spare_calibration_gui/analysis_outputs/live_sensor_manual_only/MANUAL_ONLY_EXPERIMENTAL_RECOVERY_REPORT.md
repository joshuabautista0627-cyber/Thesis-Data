# Manual-Only Experimental Recovery Report

## Outcome

A best-effort replay/live workflow was improved using only the immutable manual archive. The default v4 run is `experimental-hybrid-bedb31cb1e7c1541`; the reviewed bundle is `models/live_sensor_experimental_hybrid_v4`. V1, v2, and v3 remain loadable rollback bundles.

The application remains **experimental — not validated**. No forbidden-archive artifact or live load-cell value is used for fitting or inference.

## Default v4 hybrid result

V4 combines the best retained components: v3 event detection/localization and v2's monotonic approximate force. Deployment is pure camera plus mechanoluminescent skin. During contact, the runtime reports the current in-support frame estimate. At event completion, it holds the maximum in-support approximate force and the confidence-filtered discrete ROI. It also preserves the event's unitless peak relative to the unloaded threshold for diagnostics. Force and ROI fail closed outside optical support or below localization confidence. Load-cell and printer values are forbidden runtime inputs; their presence or absence cannot change inference.

| Reused-fold post-hoc hybrid metric | V4 |
|---|---:|
| Outer-fold event count / independent primary sessions | 102 / 54 |
| Maximum no-contact frame FPR | 2.44% |
| Minimum fold contact recall | 94.20% |
| Event localization macro F1 | 93.43% |
| Selective event localization macro F1 / coverage | 100.00% / 82.93% |
| Conditional force MAE | 0.328 N |
| Cross-validated p95 absolute force error | 0.643 N |
| Force resolution gate | Failed |

## Model-selection audit

Force tuning compared causal 5/9/13-frame filters with isotonic regression, ridge, elastic net, Extra Trees, and histogram gradient boosting. Candidate selection was nested inside each reused outer-training partition and balanced by session/force bin. The selected-per-fold search reached 0.326 N MAE, only 0.002 N below v2, with 4.49% gain over a session-balanced constant and mean within-session Spearman 0.136. Four folds selected Extra Trees, one isotonic, and one ridge. That instability and negligible error change did not justify a larger runtime model, so the auditable v2 isotonic mapping was retained.

Event localization compared v3 accumulated argmax with multinomial logistic regression, shrinkage LDA, and Extra Trees on complete held-out TEST groups. Macro F1 was 93.43%, 85.27%, 89.25%, and 91.14%, respectively, so accumulated argmax was retained.

## Historical v3 event-signal result

The literature method audit favored an event-level optical contract over frame-wise force regression. V3 detects a contact event with the existing causal nine-frame filter, live unloaded threshold, and two-frame acquire/clear rule. It reports the event's largest `contact_score / warmup_threshold` as a unitless optical peak. For localization it sums positive normalized active-fraction evidence during raw-contact frames, selects the strongest of the nine fixed ROIs, and withholds that ROI below a training-derived top-two confidence margin. No force model or Newton value exists in the bundle or runtime result.

| Reused-fold post-hoc event metric | V3 |
|---|---:|
| Outer-fold event count / independent primary sessions | 102 / 54 |
| Maximum no-contact frame FPR | 2.44% |
| Minimum fold contact recall in the archived evaluation band | 94.20% |
| Event localization accuracy | 93.41% |
| Event localization macro F1 | 93.43% |
| Selective event localization macro F1 | 100.00% |
| Selective event localization coverage | 82.93% |
| Median event peak signal | 2.46× unloaded threshold |

These numbers are recovery evidence from previously inspected folds, not untouched confirmation. V3 remains available when a unitless event-only display is preferred.

## Historical v2 force-mode result

- Contact acquire/clear state is replayed over every frame before the 1.7–3.0 N evaluation filter, matching the live engine. V1 incorrectly debounced only the already-filtered evaluation rows.
- ROI output keeps a forced class on every eligible frame but requires two consecutive frames before switching, matching the authoritative runtime limit.
- The GUI withholds low-margin ROI outputs. Forced metrics remain primary; selective metrics always report retained coverage.
- Force performance is compared with a fold-fitted, session-balanced weighted-median predictor and includes correlation and response-spread diagnostics.

## Six-fold post-hoc recovery evidence

| Metric | V1 | V2 |
|---|---:|---:|
| Displayed-force MAE, including missed contacts as 0 N | 0.479 N | 0.380 N |
| Conditional force MAE | 0.328 N | 0.328 N |
| Force RMSE | 0.384 N | 0.384 N |
| Absolute force bias | 0.115 N | 0.115 N |
| Maximum no-contact frame FPR | 2.44% | 2.44% |
| Minimum fold debounced contact recall | not reported correctly | 91.11% |
| Mean debounced contact recall | 92.77% when recomputed | 97.27% |
| Forced localization accuracy | 88.17% | 89.14% |
| Forced localization macro F1 | 88.04% | 89.13% |
| Lowest per-ROI localization recall | 72.17% (ROI 4) | 75.40% (ROI 4) |
| Selective localization macro F1 | not reported | 97.63% |
| Selective localization coverage | not reported | 79.71% |

The displayed-force change is a runtime-parity correction, not a new force regressor. The two-frame switch improves forced localization macro F1 by 1.09 percentage points without dropping uncertain frames from the primary denominator.

## Force-resolution check

The isotonic estimator's conditional MAE is 0.013 N lower than the fold-fitted weighted-median baseline, a 3.81% gain. This is not enough to establish useful force resolution: mean Spearman correlation is 0.207 and predicted force standard deviation is only 20.66% of the true-force standard deviation. The frozen v2 resolution gate requires at least a 5% MAE gain, mean Spearman 0.30, and response-spread ratio 0.25, so it fails.

Accordingly, v4's retained v2 force display is approximate inside 1.7–3.0 N and must not be described as independently validated measurement accuracy. The expanded candidate search did not change this disposition.

## Claim boundary

These are post-hoc grouped cross-validation results from six previously inspected TEST groups, not untouched confirmation. The authoritative all-ROI common safe-range procedure remains failed. Known-force physical, latency/soak, and representative-user tests were not performed. No load-cell COM port was detected during the attached-hardware check. None of v1–v4 supports a `validated` label or a physical-validation thesis claim.

## How to use

Run `app.py`, connect the camera and mechanoluminescent skin, open **Live Sensor (Experimental)**, and use an accepted unloaded optical baseline followed by the 120-frame camera warm-up. The large value is an approximate in-support force during contact and the approximate event peak after completion. A displayed ROI is confidence-filtered; the forced ROI and unitless optical peak remain diagnostics. No load cell, printer, or corresponding calibration is needed for deployment.
