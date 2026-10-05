# Manual-only retraining research artifacts

Completed 9 September 2026. The reviewed report is `artifact.json` (rendered through the Data Analytics report tool). Exact results are in `model_comparison.csv`, with per-group and per-ROI detail. These are retrospective research outputs over an exploratory 0.05-3 N interval, not validated application bundles.

## Reproduce

From `calibration_gui`, using PowerShell:

```powershell
.venv/Scripts/python.exe -m scripts.retrain_manual_models --output analysis_outputs/live_sensor_manual_only/model_runs/retraining_repeat
.venv/Scripts/python.exe -m scripts.audit_manual_retraining --run analysis_outputs/live_sensor_manual_only/model_runs/retraining_repeat
.venv/Scripts/python.exe -m scripts.report_manual_retraining --run analysis_outputs/live_sensor_manual_only/model_runs/retraining_repeat
```

The output directory must not already exist. Training reuses only the hash-verified immutable manual feature partitions; it does not reread raw ZIP/video files. All source identities, parameters, group assignments, lag searches, predictions, and library versions are saved. The manual archive's 18 replay-only recordings do not participate in training, selection, or scoring.

## Saved models

`models/force.joblib`: Extra Trees. `models/contact.joblib`: Random Forest. `models/localization.joblib`: logistic regression. Each includes its estimator, train-fitted scaler and optical floors. Models use only optical inputs, not the load-cell reference. The models are refitted on all eligible data after evaluation. Metrics come from the separate held-out predictions.

Use joblib files only when produced by this trusted local run. They are Python research artifacts and must not be installed as live application bundles. Application settings and models were not changed.

```powershell
.venv/Scripts/python.exe -m scripts.predict_retrained_manual --models analysis_outputs/live_sensor_manual_only/model_runs/retraining_20260909/models --input PATH_TO_CANONICAL_MANUAL_FRAME_PARQUET --output research_predictions.csv
```

## Evidence

- `model_comparison.csv`: nine force and six models per classification task, plus nested selector scores.
- `outer_predictions.parquet`: all held-out per-frame predictions.
- `nested_selected_predictions.parquet`: predictions selected without the outer group's labels.
- `fold_metrics.csv`, `roi_metrics.csv`, `force_bin_metrics.csv`: subgroup results.
- `evaluation_coverage.csv`: complete force interval and alignment exclusions.
- `end_to_end_metrics.json`: retrospective winner combination with missed contacts represented as zero force.
- `no_contact_episodes.csv`: raw, un-debounced episode counts over about three minutes of dedicated no-contact recordings.
- `validation_audit.json`: independent metric checks, leakage checks, source checksums, and inference parity.
- `protocol.json`, `provenance.json`, `estimator_parameters.json`, `lag_selection.json`, `inner_selections.json`: reproducibility.

No prospective generalization, validated force range, millimetre localization, optical-support safety, or live event timing is established by this experiment. Group-bootstrap intervals summarize six overlapping-training CV folds and are descriptive only. The nested contact pooled ROC-AUC is intentionally omitted because selected estimators have incompatible score scales.
