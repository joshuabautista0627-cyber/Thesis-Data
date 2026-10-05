# Manual-only model tuning

Audited retrospective results using the same held-out recordings and 0.05–3 N frame set as the original experiment.

| Task / metric | Previous best | Tuned selection | Current frame only |
|---|---:|---:|---:|
| force: mae_N | 0.6372 | 0.4991 | 0.5393 |
| force: rmse_N | 0.7705 | 0.6323 | 0.6827 |
| force: bias_N | -0.2894 | 0.0784 | 0.1118 |
| force: r2 | -0.0303 | 0.3061 | 0.1910 |
| localization: accuracy | 73.79% | 84.40% | 82.84% |
| localization: macro_f1 | 74.00% | 84.75% | 82.68% |
| contact: balanced_accuracy | 91.86% | 98.50% | 97.09% |
| contact: recall | 91.93% | 97.00% | 95.82% |
| contact: fpr | 8.20% | 0.00% | 1.64% |
| contact: precision | 98.22% | 100.00% | 99.65% |
| contact: macro_f1 | 87.15% | 95.81% | 93.78% |

## Reproduce

Run from `calibration_gui` in the pinned `.venv`. The canonical imported entry point avoids Windows process-serialization issues:

```powershell
.venv/Scripts/python.exe -u -c "from scripts.tune_manual_models import main; main()" --jobs 8 --output analysis_outputs/live_sensor_manual_only/model_runs/tuning_repeat
.venv/Scripts/python.exe -m scripts.evaluate_manual_frame_only --run analysis_outputs/live_sensor_manual_only/model_runs/tuning_repeat
.venv/Scripts/python.exe -m scripts.audit_tuned_manual --run analysis_outputs/live_sensor_manual_only/model_runs/tuning_repeat
.venv/Scripts/python.exe -m scripts.report_tuned_manual --run analysis_outputs/live_sensor_manual_only/model_runs/tuning_repeat
```

## Saved inference

`models/` contains the causal tuned selector and alternate contact FPR policy. `models_frame_only/` contains models requiring only the current frame. Manifests and task JSON files identify exact families, parameters, features, corrections, smoothing, thresholds, checksums and training recordings.

Use `scripts.predict_tuned_manual.infer_sequence` with trusted local models loaded by `load_models`. For causal models provide each complete recording in chronological order with original optical timestamps. History resets at a new recording or a gap above 300 ms. Each prediction uses only current/past optical data. All optical spatial columns are required; force labels and target ROI are unused.

## Interpretation

The main comparison is the nested tuning procedure, whose selected family can differ by fold. Family rows are nested tuned family procedures; their ranking across outer results is retrospective. The final full-data models are selected using all 15 leave-two-groups-out training splits. All-data fitted performance is not a test result.

Only the manual calibration archive was used; 18 replay-only recordings are excluded. The force interval remains exploratory, not validated. Temporal smoothing can delay responses, and the archived ROI stays constant within each recording. No continuous-position error or deployed onset delay is claimed. The live application model bundles are unchanged.

The full report is `artifact.json`; exact model and baseline metrics are `model_comparison.csv` and `before_after.csv`. Audit evidence, per-group/ROI/bin scores, all inner trials, cached predictions, frozen protocols, source hashes and paired uncertainty estimates are retained here.

For one-frame-at-a-time use, create `StreamingResearchPredictor(load_models(model_directory))` once and call `predict_one` for each ordered feature row. Preserve the object between frames; call `reset` at the start of a new recording. The audit compares this stateful path against full-sequence inference. Offline timing is recorded in `inference_timing.json`.
