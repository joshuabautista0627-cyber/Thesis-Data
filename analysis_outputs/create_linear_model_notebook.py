import ast
import contextlib
import io
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "linear_models"
NOTEBOOK = ROOT / "auto_calibration_linear_models.ipynb"


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)}


def code(text):
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": text.splitlines(True)}


nb = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.12"},
    },
    "cells": [
        md("""# Linear Models — Full Automated Calibration Dataset

## Decision

The requested models were fitted on **all 162 automated sessions** and **149,858 valid synchronized frames**. They are reproducible, but the held-out-session results say they are **not reliable enough for deployment**:

- Force: MAE about **0.53 N**, RMSE about **0.81 N**, and R² about **0.02**.
- Nine-ROI localization: **45.0%** contact-frame accuracy and **54.9%** session-level accuracy (chance is 11.1%).

The artifact is therefore useful as a measured baseline and implementation scaffold, not as a foolproof sensor model.
"""),
        md("""## Method

- Inputs: ROI-area-normalized `delta_v_mean` and `active_fraction` for ROIs 1–9.
- Camera handling: one camera-configuration indicator plus camera-by-feature interactions, because the intentional camera change altered the optical scale while the skin stayed the same.
- Force: ridge-regularized linear regression on every valid frame.
- Localization: nine one-vs-rest linear score regressions on contact frames (`force_N >= 0.05`), with the largest score selected.
- Validation: five grouped folds; a complete acquisition session is always entirely train or test. Training weights give every session equal total weight.
- Integrated optical response is not divided by cycle count for these instantaneous models. Mean/per-pixel ROI response is the appropriate area normalization. Force-time normalization would only be appropriate for a separate session-level energy/impulse question.
"""),
        code("""from pathlib import Path
import json
import numpy as np
import pandas as pd

ROOT = Path.cwd()
if not (ROOT / 'linear_models').exists():
    ROOT = Path(r'C:\\Users\\DLSU\\OneDrive\\Documents\\SOFTWARE CALIBRATION\\analysis_outputs')
MODEL_DIR = ROOT / 'linear_models'
metrics = json.loads((MODEL_DIR / 'validation_metrics.json').read_text())
model = json.loads((MODEL_DIR / 'auto_calibration_linear_models.json').read_text())
sessions = pd.read_csv(MODEL_DIR / 'session_level_oof_predictions.csv')
confusion = pd.read_csv(MODEL_DIR / 'localization_confusion_frames.csv', index_col=0)
model['training_scope']
"""),
        md("## Cross-validated results"),
        code("""pd.DataFrame({
    'all valid frames': metrics['force_all_frames'],
    'contact frames only': metrics['force_contact_frames'],
})
"""),
        code("""pd.Series({
    'contact-frame accuracy': metrics['localization_contact_frame_accuracy'],
    'session-level accuracy': metrics['localization_session_accuracy'],
    'correct sessions': metrics['localization_session_correct'],
    'total sessions': metrics['localization_sessions'],
    'chance accuracy': 1/9,
})
"""),
        code("""pd.DataFrame(metrics['localization_per_roi']).set_index('target_roi')
"""),
        code("""pd.DataFrame(metrics['localization_per_camera']).set_index('camera_configuration')
"""),
        md("""### Interpretation

The force model is barely better than predicting an overall mean (`R² ≈ 0.02`) and incorrectly crosses the 0.05 N contact threshold for nearly every true non-contact frame. It must not be used as a contact detector.

Localization is above chance, showing that the full nine-ROI pattern contains information, but the error is strongly location-dependent: ROI 3 is only 11.1% correct at session level, while ROIs 1 and 8 are 100% in this cross-validation. Camera configuration A is also substantially worse than B. This unevenness is exactly why a simple “brightest ROI pops up” rule is not foolproof.
"""),
        md("## Saved model and scoring equation"),
        code("""def linear_predict(saved, engineered_features):
    x = np.asarray(engineered_features, dtype=float)
    mean = np.asarray(saved['feature_mean'])
    scale = np.asarray(saved['feature_scale'])
    coef = np.asarray(saved['standardized_coefficients'])
    intercept = np.asarray(saved['target_mean_or_intercepts'])
    return (x - mean) / scale @ coef + intercept

{
    'force_features': len(model['force_model']['feature_names']),
    'localization_features': len(model['localization_model']['feature_names']),
    'camera_configurations': model['camera_configurations'],
    'localization_classes': model['localization_model']['classes'],
}
"""),
        md("""For force, clamp the scalar result to zero. For localization, average the nine linear scores over five consecutive frames that are known to be in contact, then choose the largest score. The JSON stores feature order, means, scales, coefficients, camera fingerprints, and class order.

Important: the localization feature vector contains raw ROI features, camera interactions, and positive per-frame ROI shares. `build_linear_models.py` is the reference feature-engineering implementation; using a different order or normalization invalidates the coefficients.
"""),
        md("## Validation checks"),
        code("""checks = {
    '162 unique held-out sessions': sessions.session.nunique() == 162,
    'one fold per session': sessions.groupby('session').fold.nunique().max() == 1,
    'all nine actual ROIs present': set(sessions.target_roi) == set(range(1, 10)),
    'confusion total equals contact frames': int(confusion.to_numpy().sum()) == model['training_scope']['contact_frames'],
    'two known camera configurations': len(model['camera_configurations']) == 2,
    'finite force coefficients': bool(np.isfinite(model['force_model']['standardized_coefficients']).all()),
    'finite localization coefficients': bool(np.isfinite(model['localization_model']['standardized_coefficients']).all()),
}
assert all(checks.values())
checks
"""),
        md("""## Practical next step

Because a fixed-force repeat at every ROI is unavailable, the most robust achievable localization approach is a **manual per-ROI template calibration** on the same skin and frozen camera settings: press each ROI a few times, store its nine-ROI response pattern, and match new contact patterns to those templates. Keep the load cell for force until a genuinely predictive optical force model validates on untouched sessions.

The current linear models can still be integrated behind a clearly marked experimental flag. Do not allow them to control safety limits or report force as a calibrated measurement.
"""),
    ],
}


namespace = {"__name__": "__main__"}
os.chdir(ROOT)
count = 0
for cell in nb["cells"]:
    if cell["cell_type"] != "code":
        continue
    count += 1
    tree = ast.parse("".join(cell["source"]), mode="exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tree.body[-1] = ast.Assign([ast.Name("_last", ast.Store())], tree.body[-1].value)
        ast.fix_missing_locations(tree)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(tree, f"{NOTEBOOK.name}:cell-{count}", "exec"), namespace)
        value = namespace.pop("_last", None)
        if value is not None:
            print(repr(value))
    cell["execution_count"] = count
    if buffer.getvalue():
        cell["outputs"] = [{"name": "stdout", "output_type": "stream", "text": buffer.getvalue().splitlines(True)}]

NOTEBOOK.write_text(json.dumps(nb, indent=1), encoding="utf-8")
print(NOTEBOOK)
