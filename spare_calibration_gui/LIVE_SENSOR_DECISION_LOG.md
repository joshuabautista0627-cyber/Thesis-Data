# Live Sensor Decision Log

Entries are append-only. A changed frozen config, gate, fold, seed, candidate list, or claim boundary requires a new entry and the stated rerun scope.

## 2026-08-13 — Adopt the supplied master plan as the controlling specification

- Reason: the user explicitly instructed Codex to read and follow `LIVE_SENSOR_GUI_MASTER_PLAN.md` with both supplied archives.
- Decision: implement milestones M0–M6 in dependency order and stop at failed or human-dependent gates.
- Alternatives rejected: wiring an existing full-range model directly into the GUI; pooling automated cycles with independent manual TEST groups; using the legacy 640×480 ROI layout.
- Affected artifacts: `config/live_sensor_study.json`, `config/live_sensor_release_gates.json`, `assets/live_sensor/roi_layout.json`.
- Rerun scope: any later change to these frozen inputs invalidates affected inventories, splits, characterization, training, validation, bundle, and replay evidence.

## 2026-08-13 — Preserve separate legacy and live ROI layouts

- Reason: the legacy calibration GUI uses `roi-4691aef42361b7430d3b`, while both evidence archives use `roi-9fbc67c50ee3bc7145fa` after 90° clockwise rotation and horizontal mirroring.
- Decision: do not overwrite `roi_layout.json`; place the immutable live-model layout in `assets/live_sensor/roi_layout.json` and require exact live-bundle compatibility.
- Alternatives rejected: resizing every ROI to a common rectangle; transforming the ROI boxes a second time; changing the calibration GUI’s working layout.
- Affected artifacts: canonical live ROI asset and all future live schemas/bundles.
- Rerun scope: all live feature extraction, split, model, validation, and replay outputs if the live layout changes.

## 2026-08-13 — Keep the initial live runtime backend model-agnostic

- Reason: the selected deployment model is intentionally unknown until M3.
- Decision: analysis may use the full candidate stack; the initial live requirements include shared GUI/numerical/schema dependencies only. The selected safe evaluator is added and pinned only after export-parity evidence exists.
- Alternatives rejected: shipping XGBoost or ONNX Runtime before model selection; loading pickle/joblib in the release application.
- Affected artifacts: `requirements-analysis.txt`, `requirements-live.txt`.
- Rerun scope: live dependency lock, bundle load/parity, latency, size, clean-install, and soak tests after backend selection.

## 2026-08-13 — Stop after the independent range and monotonicity gate failed

- Reason: the frozen original-frame characterization at 200 mm/min did not establish a safe common force range. With the required minimum of three independent sessions per 0.25 N bin, the highest supported bin was 0.625–1.625 N across ROI 1–9, and every ROI failed the preregistered monotonic-evidence rule.
- Decision: retain `characterization-dade466f019e6d15` as a failed scientific result, set provisional common `Fmax` to null, and stop before force/localization training, bundle promotion, or GUI integration.
- Alternatives rejected: lowering `Fmax` after seeing the outcome; treating two paired 10-cycle/20-cycle sessions as three independent replicates; changing the increasing-light premise, feature sign, force bins, or bootstrap gate post hoc; opening manual outer-fold predictions to rescue the release range.
- Affected artifacts: feature store `feature-store-c0ec762f1dc888a7`, split `splits-fc7de7cf0b88479e`, characterization `characterization-dade466f019e6d15`.
- Rerun scope: any new collection or separately approved scientific-contract change requires a new config hash and a full affected M1/M2 rerun before M3 can begin.

## 2026-08-13 — Supersede the mixed dependency with two authoritative plans

- Reason: the combined master plan caused the automatic characterization range gate to be treated as a prerequisite for manual force/localization modeling and live GUI implementation, confusing the scientific roles of the two archives.
- Decision: replace `LIVE_SENSOR_GUI_MASTER_PLAN.md` with a routing index. `SENSOR_CHARACTERIZATION_MASTER_PLAN.md` now controls sensor-property analysis using both archives in distinct roles. `LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md` now controls a strictly manual-only application/model track. Neither track blocks or authorizes the other.
- Characterization disposition: preserve `characterization-dade466f019e6d15`, its failed former range/monotonicity result, and all qualified tables as valid characterization evidence. Do not alter or rerun it merely because governance changed.
- Application firewall: the manual archive is the only permitted application modeling source. The automatic archive and every derived artifact are prohibited from application preprocessing, thresholds, lag, `Fdetect`, `Fmax`, optical-support fitting, model selection, bundle promotion, replay validation, and acceptance evidence.
- Existing-output disposition: combined inventory/feature/split/characterization artifacts retain historical provenance and cannot automatically be relabelled as manual-only application evidence. Shared source hashes, canonical ROI assets, and tested extraction code may be reused, but application outputs require new manual-only configuration, run IDs, hashes, manifests, feature store, splits, range/model results, and lineage checks.
- GUI status: the former characterization failure no longer blocks application work. The application track is awaiting a new manual-only source firewall and range/model analysis; no GUI model is eligible yet.
- Alternatives rejected: copying the mixed plan into two files without removing the dependency; using automatic evidence only for the application range; silently filtering the combined feature store while retaining its old run identity; discarding the failed characterization result.
- Affected documents: both new authoritative plans, the routing index, README, implementation-status ledger, and historical characterization failure report.
- Rerun scope: no characterization rerun is caused by this decision. All future application range/model/bundle evidence must start under a new manual-only study/run identity.

## 2026-08-13 — Stop manual-only application promotion at the preprocessing gate

- Reason: the strict manual-only run completed its source inventory, immutable feature store, leakage-safe split, and nested training-only preprocessing. All six outer-training folds fail to derive an `Fdetect` satisfying the frozen 90% session-balanced contact-recall target while holding no-contact frame FPR at or below 5%.
- Decision: retain `manual-preprocessing-502142bacd6c35bb` as a failed application result and stop before force/localization candidate comparison, bundle promotion, replay acceptance, or live GUI integration.
- Additional evidence: the development refit reaches 85.16% maximum force-bin recall at 2.75–3.0 N and 4.96% no-contact frame FPR. It also lacks a common safe range because ROIs 1–3 have unsupported required bins, ROI 5 has a nonpositive initial slope, and ROI 7 has a 0.5625 N training-only safe limit. The frozen inner optical-support retention target also fails.
- Alternatives rejected: lowering contact/range/support gates after seeing training results; importing automatic-archive or characterization-derived evidence; opening outer held-out predictions; promoting a partial or unsafe bundle; building the live GUI around an ineligible model.
- Affected artifacts: `archive-audit-dd1ed9cb68dab34b`, `feature-store-fa2500440adab6ca`, split hash `452064a320d05b6e8e8a6a2ba69f2ac6223aa4385db47dd5506eb9ec7ba14cc1`, and `manual-preprocessing-502142bacd6c35bb`.
- Rerun scope: new prospectively governed manual data or an independently approved pre-analysis contract change requires new config, source-manifest, feature-store, split, and preprocessing run identities. The failed result remains immutable evidence.

## 2026-08-13 — Add a disclosed post-hoc recovery application without changing eligibility

- Reason: the user cannot collect another dataset and explicitly requested a best-effort working application from the immutable manual archive.
- Decision: preserve the frozen failed result, then create a separate `posthoc_experimental` specification and run. The recovery detector uses a causal 9-frame signed-light spatial range, a median session-q95 training fallback, and a mandatory unloaded 99th-percentile live warm-up threshold. Force uses portable monotonic isotonic JSON knots only in the declared 1.7–3.0 N band. Localization is the causal normalized active-fraction argmax. The application never loads pickle/joblib and never consumes the load-cell value for prediction.
- Evidence: `experimental-recovery-d575bafff7cc8bca` reports displayed-force MAE 0.479 N, conditional MAE 0.328 N, RMSE 0.384 N, absolute bias 0.115 N, max no-contact frame FPR 2.44%, minimum fold contact recall 90.95%, forced macro ROI F1 88.04%, and all per-ROI recalls above 70%.
- Claim boundary: candidate selection reused the six existing folds after the original gate failure, the original all-ROI common safe-range result remains failed, and physical/usability evidence is absent. The bundle and UI are therefore permanently labeled `experimental` unless new prospective evidence supports a new release identity.
- Alternatives rejected: silently replacing the failed preprocessing result; calling post-hoc cross-validation an untouched test; loading an unsafe serialized tree model; using the forbidden archive; displaying exact values outside optical/model support; feeding the live load cell into optical prediction.
- Affected artifacts: `config/manual_only_experimental_recovery.json`, `scripts/build_experimental_manual_recovery.py`, `models/live_sensor_experimental_manual_recovery_v1`, `core/experimental_live_sensor.py`, `gui/live_sensor_tab.py`, and recovery tests/report.
- Rerun scope: any model/spec change requires a new recovery run/bundle hash. Confirmatory or validated claims require new prospectively governed data and the full authoritative gate sequence.

## 2026-08-14 — Correct runtime-parity evaluation and add selective ROI output

- Reason: no new physical dataset is available, and diagnostic replay showed that v1 applied contact debounce only after filtering to 1.7–3.0 N. The live engine processes the complete frame sequence, so v1's displayed-force metric was not runtime-equivalent. Localization also lacked the authoritative two-frame switch behavior.
- Decision: preserve v1 as rollback and publish a separate post-hoc v2 identity. V2 applies contact acquire/clear over complete session sequences before selecting evaluation rows, requires two consecutive frames before a forced ROI switch, and fits a training-only top-two-margin threshold for GUI withholding. Forced localization remains the primary denominator and selective coverage is always reported.
- Evidence: `experimental-recovery-5949deea3c881f09` reports displayed-force MAE 0.380 N, conditional MAE 0.328 N, max no-contact FPR 2.44%, minimum fold debounced recall 91.11%, forced localization macro F1 89.13%, and selective macro F1 97.63% at 79.71% coverage.
- Force-resolution disposition: the estimator improves over a fold-fitted weighted-median baseline by only 0.013 N/3.81%; mean Spearman correlation is 0.207 and predicted-to-true force spread is 0.207. It fails the frozen combined 5%/0.30/0.25 resolution gate, so no improved or validated force-resolution claim is allowed.
- Alternatives rejected: recomputing only favorable frames; replacing forced localization with selective-only metrics; shrinking the force range; fitting replay-only sessions; importing the forbidden archive; describing the displayed-MAE correction as a new force-regression improvement.
- Affected artifacts: `config/manual_only_experimental_recovery_v2.json`, recovery builder/runtime/GUI, `models/live_sensor_experimental_manual_recovery_v2`, recovery tests, status ledger, and recovery report.
- Rerun scope: any threshold, temporal, localization, model, or evaluation-contract change requires a new v2 run and bundle hash. Confirmatory or validated claims still require prospective data and the full physical/usability gate sequence.

## 2026-08-17 — Make event signal plus discrete ROI the default experimental contract

- Reason: the reviewed papers most consistently treat mechanoluminescence as a transient event. The best-matching software elements are Sou et al.'s threshold-defined event processing and accumulated spatial evidence, while calibrated gray-value, ratiometric, DIC, ODE, and continuous-centroid methods require force-light pairs, spectral channels, texture-resolved images, different materials, or finer spatial labels that this archive does not contain.
- Decision: preserve v1/v2 as loadable force-mode rollback bundles and publish a separate v3 `event_signal` identity as the GUI default. V3 retains the causal nine-frame normalization/filter, unloaded live threshold, and 2-frame acquire/clear logic. During each debounced event it reports the maximum unitless `contact_score / warmup_threshold`, accumulates positive normalized active-fraction evidence on raw-contact frames, returns the accumulator argmax as one of nine ROIs, and withholds ROI below a training-derived event-confidence margin. It never fits, stores, formats, or returns Newton-valued force.
- Evidence: `experimental-event-signal-badb34abe2397339` contains 102 outer-fold events from 54 independent primary sessions. Maximum no-contact frame FPR is 2.44%, minimum fold contact recall in the archived 1.7–3.0 N evaluation band is 94.20%, forced event-localization macro F1 is 93.43%, and selective event-localization macro F1 is 100% at 82.93% coverage. These remain reused-fold post-hoc recovery results. The bundle now also fail-closes on the canonical ROI-layout hash/orientation verified against the attached camera.
- Claim boundary: the peak is an optical threshold ratio, not force. The 1.7–3.0 N values are used only to evaluate contact recall; there are no event-peak calibration targets supporting a Newton conversion. Physical, soak, and representative-user validation remain absent.
- Alternatives rejected: retaining the weak v2 isotonic force display as the default; fitting event force from frame-level labels; presenting a continuous centroid from nine aggregate ROIs; adding CNN, DIC, ratiometric, or ODE models without their required data/hardware.
- Affected artifacts: `analysis_outputs/live_sensor_manual_only/LITERATURE_SOFTWARE_METHOD_AUDIT.csv`, `config/manual_only_experimental_event_signal_v3.json`, `scripts/build_experimental_event_signal.py`, `models/live_sensor_experimental_event_signal_v3`, the event runtime/UI, status/report documents, and regression tests.
- Rerun scope: any change to event boundaries, filtering, thresholding, localization accumulator/confidence, source data, or evaluation rules requires a new run/bundle identity. A Newton output requires separate event-level force calibration and prospective validation rather than a v3 patch.

## 2026-08-17 — Publish v4 hybrid force/event output after grouped model comparison

- Reason: the user explicitly requested force estimation alongside localization and authorized alternative learning models and tuning. The v3 event contract is the strongest localization result, while v2 is the corrected runtime-equivalent force recovery artifact.
- Decision: retain v1/v2/v3 as rollback identities and publish `hybrid_force_event` v4 as the GUI default. V4 uses v3 contact segmentation, threshold ratio, accumulated positive activity, selective event ROI, and canonical-layout enforcement. It adds v2's monotonic isotonic frame estimate during active contact and holds the maximum in-support estimate at event completion. Force is withheld outside fitted optical support.
- Data-quality boundary: 29,318 valid frames from 83 distinct sessions have no duplicate `(session_id, video_frame_index)` keys, missing corrected-force labels, or non-monotonic session timestamps. Model selection remains grouped by complete TEST/session boundaries. Only the archived 1.7–3.0 N band is used for force evaluation even though higher-force rows exist.
- Force comparison: nested grouped tuning compared 5/9/13-frame filters with isotonic, ridge, elastic-net, Extra Trees, and histogram gradient boosting. The selected-per-fold search reached 0.326 N conditional MAE versus v2's 0.328 N, but only 4.49% improvement over the session-balanced constant, mean within-session Spearman 0.136, and unstable family choices (Extra Trees in four folds, isotonic and ridge in one each). The added complexity was rejected as immaterial and fragile; v2 isotonic was retained.
- Localization comparison: over the same 102 outer events/54 primary sessions, accumulated argmax achieved 93.43% macro F1, versus logistic 85.27%, shrinkage LDA 89.25%, and Extra Trees 91.14%. V3 accumulated argmax was retained.
- Deployment boundary: v4 is pure camera plus mechanoluminescent skin. Its bundle declares only `signed_delta_v_sum` and `active_fraction` optical inputs and explicitly forbids load-cell and printer inputs. Runtime regression tests prove that arbitrary load-cell/printer fields cannot change force or localization output. Offline reference-force labels used to fit v2 are not runtime inputs.
- Claim boundary: v4's Newton value is approximate and not prospectively validated. Conditional MAE is 0.328 N, p95 absolute error is 0.643 N, and the original force-resolution/common-range gates remain failed. A load cell or printer is not a deployment prerequisite; any future reference instrumentation would belong only to a separate research-validation protocol. The application must continue to say `EXPERIMENTAL — NOT VALIDATED` and must not be used for control or safety.
- Evidence: `experimental-hybrid-bedb31cb1e7c1541`, `models/live_sensor_experimental_hybrid_v4`, `hybrid_candidate_benchmark.json`, and `event_localization_candidate_benchmark.json`.
- Rerun scope: any change to the component models, filter/event rules, feature normalization, range/support policy, source data, or evaluation design requires a new bundle identity. A validated force claim requires new prospectively governed known-force trials plus physical, latency/soak, and usability evidence.

## 2026-08-17 — Make timestamp alignment an explicit calibration contract

- Reason: a frame/label offset can be confused with a CSV-row shift even though the archive has irregular feature cadence. The calibration implementation needed an auditable contract that states which clocks are paired and when interpolation is withheld.
- Decision: align each optical capture timestamp to the linearly interpolated corrected reference force timestamp within the same session. Positive lag means optical `t` pairs with reference `t - lag`. Forbid frame-index/row shifting, extrapolation, cross-session pairing, and interpolation across reference gaps above 300 ms; reduce duplicate reference timestamps by median.
- Evidence: `config/manual_timestamp_alignment.json`, `core/timestamp_alignment.py`, focused irregular-cadence/session-boundary tests, and `manual-preprocessing-952f8d4e26e4d95b`. The four numerical preprocessing tables are exactly unchanged from `manual-preprocessing-502142bacd6c35bb`; selected lags are unchanged.
- Deployment boundary: this contract applies only to archived reference labels during offline calibration/model evaluation. V4 live inference still accepts only camera-derived mechanoluminescent features and does not require or consume a load cell or printer.
- Claim boundary: timestamp alignment does not repair the failed force-resolution/common-range gates and does not create new confirmation evidence. The current v4 bundle remains unchanged because its labels were already timestamp-interpolated and the hardened contract produced identical numerical inputs.
