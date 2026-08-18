"""Build a global force model from camera-consistent manual TEST2-TEST6 data."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from build_manual_linear_models import (
    ARCHIVE, OPTICAL_COLS, STATS, extract, fit_ridge, predict,
    regression_metrics, truthy, weights_by_session,
)
from build_global_force_model import aggregate_features, engineered_features


OUT = Path(__file__).resolve().parent / "clean_global_force_model"
RUNS = [2, 3, 4, 5, 6]
RIDGE = 0.03


def add_run_number(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    result["run_number"] = result.session.str.extract(r"_TEST(\d+)$")[0].astype(float)
    return result


def validate(data: pd.DataFrame, feature_mode: str, delay_frames: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str], list[dict]]:
    x, names = engineered_features(data, feature_mode)
    current_y = data.force_physical_N.to_numpy(float)
    target = current_y.copy()
    usable = np.ones(len(data), bool)
    if delay_frames:
        target[:] = np.nan
        usable[:] = False
        for _, idx in data.groupby("session", sort=False).indices.items():
            idx = np.asarray(idx)
            target[idx[delay_frames:]] = current_y[idx[:-delay_frames]]
            usable[idx[delay_frames:]] = True
    sessions = data.session.to_numpy()
    run = data.run_number.to_numpy(int)
    oof = np.full(len(data), np.nan)
    fold_rows = []
    for held_out in RUNS:
        train = (run != held_out) & usable
        test = (run == held_out) & usable
        model = fit_ridge(x[train], target[train], weights_by_session(sessions[train]), ridge=RIDGE)
        oof[test] = np.maximum(0, predict(model, x[test]))
        fold_rows.append({"held_out_test": held_out, **regression_metrics(target[test], oof[test])})
    return oof, target, usable, names, fold_rows


def serialize(model: dict, names: list[str]) -> dict:
    return {
        "feature_names": names,
        "feature_mean": np.asarray(model["mean"]).tolist(),
        "feature_scale": np.asarray(model["scale"]).tolist(),
        "target_mean": np.asarray(model["target_mean"]).tolist(),
        "standardized_coefficients": np.asarray(model["coef"]).tolist(),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    raw = add_run_number(extract())
    valid_flags = truthy(raw.frame_valid) & truthy(raw.baseline_valid) & truthy(raw.loadcell_valid) & truthy(raw.synchronization_valid)
    selected = raw.run_number.isin(RUNS).to_numpy()
    finite = raw[["force_N", *OPTICAL_COLS]].notna().all(axis=1).to_numpy()
    data = raw.loc[valid_flags & selected & finite].reset_index(drop=True)
    data["run_number"] = data.run_number.astype(int)
    data["target_roi"] = data.target_roi_ground_truth.astype(int)
    data["force_physical_N"] = np.maximum(data.force_N.to_numpy(float), 0)

    candidates = [
        ("total_light_linear", "total_light_linear", 0),
        ("global_multichannel_linear", "global_multichannel_linear", 0),
        ("global_nonlinear_basis", "global_nonlinear_basis", 0),
        ("global_nonlinear_basis_1frame_delayed", "global_nonlinear_basis", 1),
    ]
    comparisons, predictions, candidate_details = {}, {}, {}
    for name, mode, delay in candidates:
        oof, target, usable, names, folds = validate(data, mode, delay)
        comparisons[name] = regression_metrics(target[usable], oof[usable])
        comparisons[name]["target_delay_frames"] = delay
        predictions[name] = oof
        candidate_details[name] = {"mode": mode, "delay": delay, "target": target, "usable": usable, "names": names, "folds": folds}

    best_name = min(comparisons, key=lambda name: comparisons[name]["rmse_N"])
    best = candidate_details[best_name]
    final_x, final_names = engineered_features(data, best["mode"])
    final_model = fit_ridge(
        final_x[best["usable"]], best["target"][best["usable"]],
        weights_by_session(data.session.to_numpy()[best["usable"]]), ridge=RIDGE,
    )

    best_oof = predictions[best_name]
    usable = best["usable"]
    target = best["target"]
    low_force = usable & (target < 0.05)
    per_roi = []
    for roi in range(1, 10):
        mask = usable & (data.target_roi.to_numpy() == roi)
        per_roi.append({"roi": roi, **regression_metrics(target[mask], best_oof[mask])})

    artifact = {
        "model_version": "clean-global-force-1.0",
        "model_family": "ridge-regularized global regression",
        "uses_roi_label": False,
        "selected_feature_model": best["mode"],
        "target_delay_frames": best["delay"],
        "camera_requirements": {
            "exposure": -2.0, "gain": 0.0, "brightness": 228.0,
            "contrast": 130.0, "saturation": 54.0, "sharpness": 38.0,
            "gamma": 110.0, "white_balance": 4600.0,
        },
        "training_scope": {
            "archive": str(ARCHIVE), "included_runs": RUNS,
            "sessions": int(data.session.nunique()), "frames": int(len(data)),
            "sessions_per_roi": data[["session", "target_roi"]].drop_duplicates().target_roi.value_counts().sort_index().astype(int).to_dict(),
            "force_range_N": [float(data.force_physical_N.min()), float(data.force_physical_N.max())],
        },
        "model": {**serialize(final_model, final_names), "prediction_floor_N": 0.0},
    }
    validation = {
        "design": "Leave-one-complete-test-number-out validation across TEST2-TEST6; all nine ROI sessions from the held-out run are excluded from training.",
        "candidate_results": comparisons,
        "selected_model": best_name,
        "selected_model_fold_results": best["folds"],
        "selected_model_per_roi": per_roi,
        "low_force_frames": int(low_force.sum()),
        "low_force_mae_N": float(np.mean(np.abs(best_oof[low_force] - target[low_force]))) if low_force.any() else None,
        "low_force_false_contact_rate_at_0_05N": float(np.mean(best_oof[low_force] >= 0.05)) if low_force.any() else None,
        "excluded_data": "TEST1, TEST7, and NOCONTACT were excluded because their saturation/sharpness controls do not match TEST2-TEST6.",
    }

    (OUT / "clean_global_force_model.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    (OUT / "validation_metrics.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    pd.DataFrame(comparisons).T.to_csv(OUT / "model_comparison.csv")
    pd.DataFrame(per_roi).to_csv(OUT / "per_roi_metrics.csv", index=False)
    output = pd.DataFrame({
        "session": data.session, "test_number": data.run_number, "target_roi": data.target_roi,
        "force_target_N": target, "prediction_N": best_oof, "usable": usable,
    })
    output.to_csv(OUT / "out_of_session_predictions.csv.gz", index=False, compression="gzip")
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    main()
