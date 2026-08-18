import ast
import contextlib
import io
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "clean_global_force_model"
NOTEBOOK = ROOT / "clean_global_force_model_analysis.ipynb"


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)}


def code(text):
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": text.splitlines(True)}


nb = {
    "nbformat": 4, "nbformat_minor": 5,
    "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python", "version": "3.12"}},
    "cells": [
        md("""# Clean Global Force Model — TEST2 to TEST6

## tl;dr

- The model uses **45 complete sessions**, five repeats at every ROI, and **15,894 frames** collected with saturation 54 and sharpness 38.
- It is global: no ROI label is used.
- Entire numbered test runs are held out during validation.
- Best result: **MAE 1.65 N**, **RMSE 2.13 N**, **R² 0.362** with a one-frame output delay.
- It is suitable as an experimental force estimator after contact is detected, but it cannot reliably decide zero/no-contact because matching no-contact recordings were not available for this camera configuration.
"""),
        md("""## Context & Methods

The source is the updated `Manual Calibration.zip`. TEST1, TEST7, and NOCONTACT are deliberately excluded because their saturation or sharpness settings differ from TEST2–TEST6.

### Key Assumptions

- Negative load-cell readings are zeroed to 0 N.
- The force estimate may lag by one camera frame because this alignment validated better than zero-lag prediction.
- The model is evaluated by leaving out all nine ROI sessions belonging to one complete test number, then repeating for TEST2 through TEST6.
"""),
        code("""from pathlib import Path
import json
import numpy as np
import pandas as pd

ROOT = Path.cwd()
if not (ROOT / 'clean_global_force_model').exists():
    ROOT = Path(r'C:\\Users\\DLSU\\OneDrive\\Documents\\SOFTWARE CALIBRATION\\analysis_outputs')
MODEL_DIR = ROOT / 'clean_global_force_model'
model = json.loads((MODEL_DIR / 'clean_global_force_model.json').read_text())
metrics = json.loads((MODEL_DIR / 'validation_metrics.json').read_text())
comparison = pd.read_csv(MODEL_DIR / 'model_comparison.csv', index_col=0)
per_roi = pd.read_csv(MODEL_DIR / 'per_roi_metrics.csv')
oof = pd.read_csv(MODEL_DIR / 'out_of_session_predictions.csv.gz')
model['training_scope']
"""),
        md("## Data\n\n### 1. Included scope"),
        code("""pd.Series({
    'sessions': model['training_scope']['sessions'],
    'frames': model['training_scope']['frames'],
    'minimum force (N)': model['training_scope']['force_range_N'][0],
    'maximum force (N)': model['training_scope']['force_range_N'][1],
    'included test numbers': model['training_scope']['included_runs'],
    'sessions per ROI': sorted(set(model['training_scope']['sessions_per_roi'].values())),
})
"""),
        md("## Results\n\n### 2. Candidate comparison"),
        code("""comparison[['mae_N', 'rmse_N', 'r2', 'bias_N', 'target_delay_frames']]
"""),
        md("### 3. Complete-run holdouts"),
        code("""pd.DataFrame(metrics['selected_model_fold_results']).set_index('held_out_test')
"""),
        md("### 4. Performance by physical ROI"),
        code("""per_roi.set_index('roi')[['mae_N', 'rmse_N', 'r2', 'bias_N']]
"""),
        md("""The model is materially better than the earlier one-session-per-ROI analysis. Performance is reasonably consistent across held-out test numbers, but ROI 1 remains the weakest location with MAE 2.34 N. This is useful engineering evidence that the added repeats helped.

The low-force check still fails: all held-out frames below 0.05 N receive predictions above the contact threshold. This occurs because the compatible TEST2–TEST6 data contains relatively little sustained zero-force data. Use an independent contact detector and only display the force estimate after contact.
"""),
        md("## Takeaways"),
        code("""checks = {
    '45 sessions included': model['training_scope']['sessions'] == 45,
    'five sessions per ROI': set(model['training_scope']['sessions_per_roi'].values()) == {5},
    'all intended tests represented': set(oof.test_number.unique()) == {2, 3, 4, 5, 6},
    'one prediction per usable row': oof.loc[oof.usable, 'prediction_N'].notna().all(),
    'coefficients match features': len(model['model']['feature_names']) == len(model['model']['standardized_coefficients']),
    'finite predictions': np.isfinite(oof.loc[oof.usable, 'prediction_N']).all(),
}
assert all(checks.values())
checks
"""),
        md("""1. Integrate this as a **contact-phase global force estimator**, not as the contact detector.
2. Enforce the recorded camera requirements—especially saturation 54 and sharpness 38.
3. Collect matching no-contact data before expecting the model to output a reliable zero.
4. Keep the one-frame delay explicit in the user interface and do not use this estimate for emergency force limits.
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
