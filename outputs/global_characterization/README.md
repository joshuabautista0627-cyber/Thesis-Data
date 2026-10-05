# Reproducing the BAURods global characterization

Run from the repository root with the existing analysis Python environment:

```powershell
& '.\calibration_gui\.venv\Scripts\python.exe' '.\global_sensor_characterization.py'
```

Optional flags: `--archive PATH`, `--output PATH`, `--bootstrap 2000`.
Dependencies: numpy, pandas, scipy, scikit-learn, matplotlib and pyarrow. Existing verified original-video feature-store files are used as a secondary diagnostic. The ZIP remains the controlling source for primary results. No automatic-calibration data are loaded.

Read Global_Sensor_Characterization_Report.md first. The summary CSV has one global result column. Full precision numbers are in the CSV/JSON outputs. Main figures are supplied in PNG (300 dpi), PDF and SVG; supplementary spatial figures are in figures/spatial_diagnostics. Force segmentation plots are under global_characterization/audit. Every event links back to its recording and force/frame indices.

The primary result characterizes the archived color-magnified processing pipeline. Distinct physical presses within a recording are correlated. Confidence intervals resample whole recordings within the nine fixed locations. Unsupported intrinsic characteristics are explicitly marked rather than filled with invented measurements.

## Model evidence integration

The report now includes separately scoped learned-model results from the supplied technical report, independently recomputed from saved outer predictions. The physical press analysis is unchanged. Model frame scores use original-video features, six held-out TEST groups and equal recording weights within the 0.05–3 N press interval. They do not replace physical response specifications or imply live deployment.

After the core script, run `calibration_gui/.venv/Scripts/python.exe integrate_model_characterization.py` from the workspace. The default inputs are the original fixed/tuned model run directories and supplied PDF. Use `--model-runs`, `--model-report` and `--output` to relocate inputs/outputs. Then run `prepare_workbook.py`, the existing XLSX builder, and `verify_and_package.py` with their documented runtimes. The package contains reviewed prediction rows, copied metrics/protocols, validation results and the source PDF; raw archive, full model binaries and feature store remain external. No retraining is needed to reproduce this integration.
