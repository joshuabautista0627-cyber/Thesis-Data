import ast
import contextlib
import io
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
NOTEBOOK = ROOT / "auto_calibration_analysis.ipynb"


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)}


def code(text):
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": text.splitlines(True),
    }


nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []}
nb["metadata"]["kernelspec"] = {
    "display_name": "Python 3",
    "language": "python",
    "name": "python3",
}
nb["metadata"]["language_info"] = {"name": "python", "version": "3.12"}
nb["cells"] = [
    md(
        """# Auto-Calibration Data Analysis

## tl;dr

- All **162 sessions** are structurally complete; status, frame, video, and load-cell row counts reconcile exactly.
- Spatial localization is not calibration-ready: the target ROI is the peak optical winner in **22/162 sessions (13.6%)**, with winners concentrated in ROIs 1 and 3.
- A camera-control change created a major comparability break. For repeated ROIs 1–6, the darker configuration retained a median **16.0%** of the earlier peak signal and **15.2%** of integrated signal per programmed cycle.
- Mechanical response is the strongest signal: within comparable target/protocol groups, displacement magnitude has a median Spearman correlation of about **0.95** with maximum force; integrated optical response is positive in 16 of 17 comparable groups but weaker.
"""
    ),
    md(
        """## Context & Methods

This is a data-quality and interim-calibration review of `Auto Calibration.zip`, covering sessions named from August 6–7, 2026. The grain is one acquisition session for summary metrics and one captured frame/load-cell sample for validity checks.

### Key Assumptions

- `target_roi_ground_truth` is the intended pressed location.
- The ROI with the largest generated summary value is treated as the spatial winner.
- Camera configurations are compared descriptively, not causally, because cycle count, collection order, target location, and camera controls changed together.
- Session-name timestamps are local acquisition labels; JSON timestamps are UTC. The analysis does not combine them into a cross-time duration metric.

The full archive scan is implemented in `analyze_auto_calibration.py`; set `REBUILD_FROM_ARCHIVE=True` below to rerun it before loading the compact outputs.
"""
    ),
    code(
        """from pathlib import Path
import runpy
import pandas as pd

ROOT = Path.cwd()
if not (ROOT / 'session_summary.csv').exists():
    ROOT = Path(r'C:\\Users\\DLSU\\OneDrive\\Documents\\SOFTWARE CALIBRATION\\analysis_outputs')
REBUILD_FROM_ARCHIVE = False
if REBUILD_FROM_ARCHIVE:
    runpy.run_path(str(ROOT / 'analyze_auto_calibration.py'), run_name='__main__')

sessions = pd.read_csv(ROOT / 'session_summary.csv')
sessions['integrated_per_cycle_millions'] = sessions['integrated_target_value'] / sessions['motion_cycles'] / 1_000_000
len(sessions)
"""
    ),
    md("## Data\n\n### 1. Reconcile collection counts"),
    code(
        """reconciliation = pd.DataFrame({
    'check': [
        'Unique session folders',
        'Accepted frames = feature rows',
        'Feature rows = video frames',
        'Load-cell metadata = parsed rows',
        'Complete sessions',
        'Sessions with status errors',
    ],
    'result': [
        sessions.session_folder.nunique(),
        int((sessions.accepted_frame_count == sessions.frame_rows_profiled).sum()),
        int((sessions.feature_row_count == sessions.video_frame_count).sum()),
        int((sessions.loadcell_sample_count == sessions.loadcell_rows_profiled).sum()),
        int(sessions.complete.sum()),
        int((sessions.status_error_count > 0).sum()),
    ],
    'expected': [162, 162, 162, 162, 162, 0],
})
reconciliation
"""
    ),
    code(
        """quality_profile = pd.Series({
    'frame_rows': int(sessions.frame_rows_profiled.sum()),
    'loadcell_rows': int(sessions.loadcell_rows_profiled.sum()),
    'frame_valid_rate': (sessions.frame_valid_rate * sessions.frame_rows_profiled).sum() / sessions.frame_rows_profiled.sum(),
    'loadcell_valid_rate': (sessions.loadcell_valid_rate * sessions.loadcell_rows_profiled).sum() / sessions.loadcell_rows_profiled.sum(),
    'baseline_drift_pass_sessions': int(sessions.baseline_drift_passed.sum()),
    'calibration_quality_pass_sessions': int(sessions.calibration_quality_passed.sum()),
    'not_ready_at_start': int((~sessions.readiness_at_start).sum()),
    'force_limit_exceeded_rows': int(sessions.force_limit_exceeded_rows.sum()),
    'sequence_aborted_rows': int(sessions.sequence_aborted_rows.sum()),
})
quality_profile
"""
    ),
    md("## Results\n\n### 2. Spatial localization performance"),
    code(
        """target_summary = sessions.groupby('target_roi').apply(
    lambda g: pd.Series({
        'sessions': len(g),
        'peak_winner_rate': (g.peak_winner_roi == g.name).mean(),
        'integrated_winner_rate': (g.integrated_winner_roi == g.name).mean(),
        'mean_peak_delta_v': g.peak_target_value.mean(),
    }),
    include_groups=False,
)
target_summary
"""
    ),
    code(
        """winner_distribution = pd.DataFrame({
    'peak_winner_sessions': sessions.peak_winner_roi.value_counts().sort_index(),
    'integrated_winner_sessions': sessions.integrated_winner_roi.value_counts().sort_index(),
    'mean_contact_winner_sessions': sessions.mean_contact_winner_roi.value_counts().sort_index(),
}).fillna(0).astype(int)
winner_distribution
"""
    ),
    md("### 3. Camera configuration break"),
    code(
        """sessions['camera_config'] = sessions.camera_fingerprint.str[:8].map({
    '5a40306c': 'Config A: brighter',
    '4cde1776': 'Config B: darker',
})
camera_profile = sessions.groupby('camera_config').agg(
    sessions=('session_folder', 'size'),
    exposure=('camera_exposure', 'first'),
    brightness=('camera_brightness', 'first'),
    contrast=('camera_contrast', 'first'),
    baseline_mean_v=('baseline_target_mean_v', 'mean'),
    peak_target_delta_v=('peak_target_value', 'mean'),
)
camera_profile
"""
    ),
    code(
        """same_targets = sessions[sessions.target_roi <= 6].groupby(['target_roi', 'camera_config']).agg(
    peak=('peak_target_value', 'mean'),
    integrated_per_cycle=('integrated_per_cycle_millions', 'mean'),
    median_max_force=('force_N_max', 'median'),
).unstack()
comparison = pd.DataFrame({
    'peak_B_to_A_ratio': same_targets['peak']['Config B: darker'] / same_targets['peak']['Config A: brighter'],
    'integrated_per_cycle_B_to_A_ratio': same_targets['integrated_per_cycle']['Config B: darker'] / same_targets['integrated_per_cycle']['Config A: brighter'],
    'force_B_to_A_ratio': same_targets['median_max_force']['Config B: darker'] / same_targets['median_max_force']['Config A: brighter'],
})
comparison
"""
    ),
    md("### 4. Mechanical and optical response to displacement"),
    code(
        """def spearman_without_scipy(a, b):
    return a.rank().corr(b.rank())

sessions['displacement_abs_mm'] = sessions.displacement_mm.abs()
correlations = []
for keys, group in sessions.groupby(['batch', 'target_roi', 'motion_cycles', 'roi_only_magnification', 'lower_cutoff_hz']):
    if len(group) >= 6 and group.displacement_abs_mm.nunique() >= 3:
        correlations.append({
            'group': str(keys),
            'n': len(group),
            'displacement_vs_force': spearman_without_scipy(group.displacement_abs_mm, group.force_N_max),
            'displacement_vs_integrated': spearman_without_scipy(group.displacement_abs_mm, group.integrated_target_value),
            'force_vs_integrated': spearman_without_scipy(group.force_N_max, group.integrated_target_value),
        })
correlations = pd.DataFrame(correlations)
correlations[['displacement_vs_force', 'displacement_vs_integrated', 'force_vs_integrated']].agg(['median', lambda s: (s > 0).mean()]).rename(index={'<lambda>': 'positive_share'})
"""
    ),
    md(
        """## Takeaways

1. **Preserve the load-cell pipeline.** It is complete, valid, and strongly ordered by commanded displacement.
2. **Freeze camera controls before collecting calibration data.** The two camera fingerprints define materially different signal scales; current optical values should not be pooled without configuration-aware normalization or recollection.
3. **Rework spatial localization.** Inspect ROI-specific baselines, global illumination effects, and the winner rule; use per-ROI normalization and evaluate with a session-level confusion matrix.
4. **Repeat a balanced validation set.** Use the same camera fingerprint, motion-magnification settings, cycle count, displacement grid, target order, and force distribution for all nine ROIs.

Overall validation status: **Share with caveats**. The collection is structurally trustworthy, but the optical calibration conclusion is not yet trustworthy across camera configurations or ROI positions.
"""
    ),
]

namespace = {"__name__": "__main__"}
os.chdir(ROOT)
execution_count = 0
for cell in nb["cells"]:
    if cell["cell_type"] != "code":
        continue
    execution_count += 1
    source = "".join(cell["source"])
    tree = ast.parse(source, mode="exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tree.body[-1] = ast.Assign(
            targets=[ast.Name(id="_notebook_last_value", ctx=ast.Store())],
            value=tree.body[-1].value,
        )
        ast.fix_missing_locations(tree)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(tree, f"{NOTEBOOK.name}:cell-{execution_count}", "exec"), namespace)
        if "_notebook_last_value" in namespace:
            value = namespace.pop("_notebook_last_value")
            if value is not None:
                print(repr(value))
    cell["execution_count"] = execution_count
    output = buffer.getvalue()
    if output:
        cell["outputs"] = [{"name": "stdout", "output_type": "stream", "text": output.splitlines(True)}]

NOTEBOOK.write_text(json.dumps(nb, indent=1), encoding="utf-8")
print(NOTEBOOK)
