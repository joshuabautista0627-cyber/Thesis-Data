import ast
import contextlib
import io
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "manual_linear_models"
NOTEBOOK = ROOT / "manual_calibration_linear_models.ipynb"


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)}


def code(text):
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": text.splitlines(True)}


nb = {
    "nbformat": 4, "nbformat_minor": 5,
    "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python", "version": "3.12"}},
    "cells": [
        md("""# Manual Calibration Linear Models

## tl;dr

- All **8,559 frames** from **nine manual sessions** passed the recorded validity checks; there is one session for each ROI and one camera-control combination.
- The nine-ROI linear classifier reaches **79.7% contact-frame accuracy** under blocked within-session validation.
- The ROI-conditioned force regression remains weak: **MAE 2.53 N**, **RMSE 3.19 N**, and **R² 0.115** when the ROI is known. End-to-end force prediction is slightly worse.
- This is an experimental calibration artifact, not a deployment-ready model. There is no independent repeat manual session for any ROI, so localization generalization cannot yet be measured honestly.
"""),
        md("""## Context & Methods

The source is `Manual Calibration.zip`, collected August 8, 2026. The model pipeline first assigns one of nine ROI scores, then uses that selected ROI in a single ridge-regularized linear force equation with ROI interaction terms.

### Key Assumptions

- `target_roi_ground_truth` is the manually pressed ROI for the entire session.
- Negative load-cell readings are zero-load noise and are floored to 0 N for fitting.
- ROI `delta_v_mean` is already area-normalized; no cycle normalization is used because these are instantaneous manual frames.
- Five contiguous temporal fifths are used for validation. Training and validation are separated in time, but they still come from the same recording and skin position.
"""),
        code("""from pathlib import Path
import json
import numpy as np
import pandas as pd

ROOT = Path.cwd()
if not (ROOT / 'manual_linear_models').exists():
    ROOT = Path(r'C:\\Users\\DLSU\\OneDrive\\Documents\\SOFTWARE CALIBRATION\\analysis_outputs')
MODEL_DIR = ROOT / 'manual_linear_models'
metrics = json.loads((MODEL_DIR / 'validation_metrics.json').read_text())
model = json.loads((MODEL_DIR / 'manual_linear_models.json').read_text())
per_roi = pd.read_csv(MODEL_DIR / 'per_roi_metrics.csv')
confusion = pd.read_csv(MODEL_DIR / 'localization_confusion.csv', index_col=0)
oof = pd.read_csv(MODEL_DIR / 'out_of_fold_predictions.csv.gz')
metrics['data_quality']
"""),
        md("## Data\n\n### 1. Quality and coverage"),
        code("""pd.Series({
    'sessions': metrics['data_quality']['raw_sessions'],
    'raw frames': metrics['data_quality']['raw_frames'],
    'valid modeling frames': metrics['data_quality']['valid_model_frames'],
    'invalid/nonfinite frames': metrics['data_quality']['invalid_or_nonfinite_frames'],
    'duplicate session-frame IDs': metrics['data_quality']['duplicate_session_frame_ids'],
    'saturation warnings': metrics['data_quality']['saturation_warning_frames'],
    'minimum force (N)': float(oof.force_N.min()),
    'maximum force (N)': float(oof.force_N.max()),
})
"""),
        md("## Results\n\n### 2. Localization"),
        code("""pd.Series({
    'blocked contact-frame accuracy': metrics['localization_contact_frame_accuracy'],
    'chance accuracy': 1/9,
    'contact frames': int(oof.contact.sum()),
})
"""),
        code("""per_roi[['roi', 'contact_frames', 'localization_accuracy']].set_index('roi')
"""),
        md("### 3. Force regression"),
        code("""pd.DataFrame({
    'known ROI': metrics['force_all_frames_known_roi'],
    'end-to-end predicted ROI': metrics['force_all_frames_end_to_end'],
})
"""),
        code("""per_roi[['roi', 'force_mae_N_known_roi', 'force_mae_N_end_to_end']].set_index('roi')
"""),
        md("""The manual set improves the optical-force relationship over the automated baseline in R² terms, but an R² of 0.115 still means most frame-to-frame force variation is unexplained. Localization errors add only a small amount of force error; the main bottleneck is the weak optical-to-force relationship itself.

The 79.7% localization number is not directly comparable to a held-out-session score because every ROI has only one recording. Static position/session signatures may therefore inflate it.
"""),
        md("## Takeaways"),
        code("""checks = {
    'all source frames retained': metrics['data_quality']['raw_frames'] == metrics['data_quality']['valid_model_frames'],
    'all nine ROI labels present': set(oof.actual_roi.unique()) == set(range(1, 10)),
    'one fold per row': oof.fold.between(0, 4).all(),
    'confusion reconciles to contact rows': int(confusion.to_numpy().sum()) == int(oof.contact.sum()),
    'force predictions finite': np.isfinite(oof.predicted_force_end_to_end_N).all(),
    'model coefficients finite': np.isfinite(model['force_model']['standardized_coefficients']).all(),
}
assert all(checks.values())
checks
"""),
        md("""1. The fitted model and inference helper are usable for software integration experiments.
2. Do not use the estimated force for safety control or calibrated reporting.
3. The smallest decisive validation is one additional complete nine-ROI manual run under unchanged camera controls. Keep it untouched until this model is frozen, then report session-held-out force and localization performance.
4. In the application, average five frames before showing the ROI box to reduce flicker, and expose a low-confidence/no-decision state rather than always forcing one of nine boxes.
"""),
    ],
}


namespace = {"__name__": "__main__"}
os.chdir(ROOT)
execution_count = 0
for cell in nb["cells"]:
    if cell["cell_type"] != "code":
        continue
    execution_count += 1
    tree = ast.parse("".join(cell["source"]), mode="exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tree.body[-1] = ast.Assign([ast.Name("_last", ast.Store())], tree.body[-1].value)
        ast.fix_missing_locations(tree)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(tree, f"{NOTEBOOK.name}:cell-{execution_count}", "exec"), namespace)
        value = namespace.pop("_last", None)
        if value is not None:
            print(repr(value))
    cell["execution_count"] = execution_count
    if buffer.getvalue():
        cell["outputs"] = [{"name": "stdout", "output_type": "stream", "text": buffer.getvalue().splitlines(True)}]

NOTEBOOK.write_text(json.dumps(nb, indent=1), encoding="utf-8")
print(NOTEBOOK)
