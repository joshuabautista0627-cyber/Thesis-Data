# BAURods data quality assessment

Start with **Data_Quality_Report.md** or the local visual report at <http://127.0.0.1:4187/> while its preview server is running. **Thesis_Data_Quality_Assessment.md** contains the separate formal subsection. The original ZIP was not modified.

## Contents

- `data_quality_analysis.py`: original-archive integrity checks, descriptive statistics, timing reconstruction, outlier flags, archived-predictor audit and 15 figures.
- `supplemental_audit.py`: independent source recomputation of 360 summary cells, workspace inventory and existing split/legacy prediction audit.
- `build_report.py`: reviewed prose, focused reports, app snapshot, exact requirements and companion notebook. The narrative is intentionally locked to the verified archive hash and refuses a different dataset until reviewed.
- `outputs/tables/`: machine-readable CSV summaries, full source dictionary, field profiles, 2,551-member archive inventory and row/trial-level anomaly log.
- `outputs/processed/audited_frames.csv.gz`: compact frame-level derivative with original row number and recording identity. It is not a replacement for the original archive.
- `outputs/figures/`: 15 figures, each in 320 dpi PNG and vector SVG. The optical/force boxplots use observed quartiles and IQR whiskers; all individual recording values remain visible. Mean ± SD error bars describe sample dispersion, not confidence intervals.
- `outputs/summary.json`, `validation_checks.json`, `supplemental_validation.json`, `analysis.log`: calculation and verification records.
- `report_app/`: editable visual-report source and compiled `dist/` preview. Its numeric data match the reviewed CSV summaries. Browser charts show individual recording values; thesis export figures include boxplots and mean ± SD plots.
- `data_quality_analysis.ipynb`: an unexecuted convenience interface to the same scripts. The scripts themselves were executed and verified.
- Focused Markdown reports: completeness, baseline, repeatability, force, timing, anomalies, leakage and localization.

## Reproduce

The existing project environment was used without installing packages:

```powershell
& '..\calibration_gui\.venv\Scripts\python.exe' '.\data_quality_analysis.py' --archive 'C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\Manual Calibration (2).zip' --decode-video
& '..\calibration_gui\.venv\Scripts\python.exe' '.\supplemental_audit.py'
& '..\calibration_gui\.venv\Scripts\python.exe' '.\build_report.py'
```

Run these from `baurods_quality`. For a separate Python environment, install the pinned `requirements.txt` first. The numerical script accepts `--archive` and `--output`; report assembly expects the default `outputs` directory. Without `--decode-video`, the video-decode check is explicitly marked unperformed. The delivered run includes full primary-video decoding. Auxiliary videos were checked for archive CRC integrity only.

The supplemental script expects its parent to be the supplied SOFTWARE CALIBRATION workspace, including the existing manual-only split manifest and legacy prediction file. Those existing model files were read, not changed. The ZIP-only analysis can run without them; evaluation-leakage conclusions require them.

To serve the already-built visual report:

```powershell
python -m http.server 4187 --bind 127.0.0.1 --directory report_app\dist
```

The entire `dist` directory is required because the reviewed snapshot is a content-addressed sidecar. A portable standalone HTML export is also included in the final package. It preserves the reviewed report but does not execute Python or refresh the source archive. The local preview is not a published website.

The report app uses the installed Data runtime. Rebuild its authored content with the installed `data-analytics/scripts/data-app.mjs build --project-dir <report_app> --separate-data` command; its `AGENTS.md` describes the supported workflow. Do not substitute stale output after changing the data.

## Interpretation rules

The source has one session label, S3, one day and 83 recording directories. Seventy-two are press recordings; eleven are filename-indicated no-contact recordings. Eight no-contact recordings retain contradictory `Press` trial labels. Their force-derived contact states are all false; originals are preserved.

Primary repeatability summaries give each recording equal weight. There are no trusted physical-press identifiers. Multiple force pulses can remain above the 0.05 N saved contact threshold, so contiguous threshold runs are not assumed to equal presses. Frames are not independent experimental replicates.

`roiN_delta_v_mean` is an existing digital optical feature. All configurations identify exported quantitative measurements as motion-magnified. No raw-video feature recomputation, new classifier fitting, outlier deletion, force clipping or experimental threshold optimization was performed.

The current archive's saved dominant-ROI predictions are distinct from subsequently trained models. Their coverage is 34/26,786 contact frames. Conditional accuracy must always be accompanied by coverage. Contact labels derive from the load cell; using the same force threshold as both predictor and truth would be circular.

The analysis is descriptive. No inferential p-values or independent-session confidence intervals are claimed. CVs are relative dispersion on the saved scale, not universal quality thresholds or physical measurement uncertainty. Signed timing-offset CV is left blank. Empty/undefined results remain blank, not zero, except the documented zero-division convention used for classification precision/F1.

## Source identity

Archive SHA-256:

`e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a`

Acquisition: 8 August 2026, 14:21:30–17:52:02 Asia/Manila. Assessment prepared 5 October 2026. All original archive members have individual hashes in `archive_inventory.csv`.

## Methods references

- [NIST coefficient of variation](https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/coefvari.htm): scale and near-zero-mean cautions.
- [SciPy Spearman correlation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html): monotonic association coefficient.

Neither reference supplies an acceptance threshold for this sensor.
