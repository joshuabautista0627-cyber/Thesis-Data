"""Fit manual-calibration linear force and ROI models with blocked validation."""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(r"C:\Users\DLSU\OneDrive\Documents\SOFTWARE CALIBRATION")
ARCHIVE = Path(r"C:\Users\DLSU\Downloads\Data Collection\Manual Calibration.zip")
OUT = ROOT / "analysis_outputs" / "manual_linear_models"
CONTACT_N = 0.05
RIDGE = 0.03
N_FOLDS = 5
STATS = ["delta_v_mean", "delta_v_max", "delta_v_p95", "delta_v_std", "active_fraction", "normalized_intensity"]
OPTICAL_COLS = [f"roi{roi}_{stat}" for roi in range(1, 10) for stat in STATS]
REQUIRED = [
    "capture_frame_id", "elapsed_time_s", "target_roi_ground_truth", "force_N",
    "frame_valid", "baseline_valid", "loadcell_valid", "synchronization_valid",
    "saturation_warning", "applied_exposure", "applied_gain", "applied_white_balance",
    *OPTICAL_COLS,
]


def truthy(series: pd.Series) -> np.ndarray:
    return series.astype(str).str.lower().isin(["true", "1", "yes"]).to_numpy()


def extract() -> pd.DataFrame:
    parts = []
    with zipfile.ZipFile(ARCHIVE) as zf:
        members = sorted(n for n in zf.namelist() if n.endswith("master_synchronized.csv"))
        for member in members:
            frame = pd.read_csv(zf.open(member), usecols=REQUIRED, low_memory=False)
            frame["session"] = Path(member).parent.name
            frame["source_member"] = member
            frame["row_in_session"] = np.arange(len(frame))
            parts.append(frame)
    data = pd.concat(parts, ignore_index=True)
    numeric = ["capture_frame_id", "elapsed_time_s", "target_roi_ground_truth", "force_N", "applied_exposure", "applied_gain", "applied_white_balance", *OPTICAL_COLS]
    for col in numeric:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    return data


def weights_by_session(sessions: np.ndarray) -> np.ndarray:
    counts = Counter(sessions)
    weights = np.asarray([1 / counts[s] for s in sessions], float)
    return weights / weights.mean()


def fit_ridge(x: np.ndarray, y: np.ndarray, weights: np.ndarray, ridge: float = RIDGE) -> dict:
    total = weights.sum()
    mean = (x * weights[:, None]).sum(axis=0) / total
    scale = np.sqrt(np.maximum((((x - mean) ** 2) * weights[:, None]).sum(axis=0) / total, 1e-12))
    xs = (x - mean) / scale
    if y.ndim == 1:
        target_mean = float((y * weights).sum() / total)
        centered = y - target_mean
    else:
        target_mean = (y * weights[:, None]).sum(axis=0) / total
        centered = y - target_mean
    lhs = (xs.T * weights) @ xs / total + ridge * np.eye(xs.shape[1])
    rhs = (xs.T * weights) @ centered / total
    coef = np.linalg.solve(lhs, rhs)
    return {"mean": mean, "scale": scale, "target_mean": target_mean, "coef": coef}


def predict(model: dict, x: np.ndarray) -> np.ndarray:
    return (x - model["mean"]) / model["scale"] @ model["coef"] + model["target_mean"]


def localization_features(data: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    raw = data[OPTICAL_COLS].to_numpy(float)
    delta = np.maximum(data[[f"roi{i}_delta_v_mean" for i in range(1, 10)]].to_numpy(float), 0)
    active = np.maximum(data[[f"roi{i}_active_fraction" for i in range(1, 10)]].to_numpy(float), 0)
    delta_share = delta / np.maximum(delta.sum(axis=1, keepdims=True), 1e-9)
    active_share = active / np.maximum(active.sum(axis=1, keepdims=True), 1e-9)
    names = OPTICAL_COLS + [f"roi{i}_delta_share" for i in range(1, 10)] + [f"roi{i}_active_share" for i in range(1, 10)]
    return np.column_stack([raw, delta_share, active_share]), names


def force_features(data: pd.DataFrame, selected_roi: np.ndarray) -> tuple[np.ndarray, list[str]]:
    raw = data[OPTICAL_COLS].to_numpy(float)
    onehot = np.eye(9)[selected_roi.astype(int) - 1]
    cube = data[[f"roi{roi}_{stat}" for roi in range(1, 10) for stat in STATS]].to_numpy(float).reshape(-1, 9, len(STATS))
    chosen = cube[np.arange(len(data)), selected_roi.astype(int) - 1]
    interactions = np.einsum("ni,nj->nij", onehot, chosen).reshape(len(data), -1)
    names = OPTICAL_COLS + [f"selected_roi_{i}" for i in range(1, 10)] + [f"selected_roi_{roi}_x_{stat}" for roi in range(1, 10) for stat in STATS]
    return np.column_stack([raw, onehot, interactions]), names


def regression_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict:
    error = predicted - actual
    denom = np.sum((actual - actual.mean()) ** 2)
    return {
        "frames": int(len(actual)),
        "mae_N": float(np.mean(np.abs(error))),
        "rmse_N": float(np.sqrt(np.mean(error ** 2))),
        "r2": float(1 - np.sum(error ** 2) / denom) if denom else None,
        "bias_N": float(np.mean(error)),
    }


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
    raw_data = extract()
    flag_valid = truthy(raw_data.frame_valid) & truthy(raw_data.baseline_valid) & truthy(raw_data.loadcell_valid) & truthy(raw_data.synchronization_valid)
    finite_valid = raw_data[["force_N", "target_roi_ground_truth", *OPTICAL_COLS]].notna().all(axis=1).to_numpy()
    data = raw_data.loc[flag_valid & finite_valid].reset_index(drop=True)
    data["target_roi"] = data.target_roi_ground_truth.astype(int)
    data["force_physical_N"] = np.maximum(data.force_N.to_numpy(float), 0)

    # Each fold is a contiguous fifth of every ROI's single recording.
    fold = np.empty(len(data), int)
    for _, idx in data.groupby("session", sort=False).indices.items():
        idx = np.asarray(idx)
        fold[idx] = np.minimum(np.arange(len(idx)) * N_FOLDS // len(idx), N_FOLDS - 1)
    data["fold"] = fold

    loc_x, loc_names = localization_features(data)
    true_roi = data.target_roi.to_numpy(int)
    force_y = data.force_physical_N.to_numpy(float)
    sessions = data.session.to_numpy()
    contact = force_y >= CONTACT_N
    oof_scores = np.full((len(data), 9), np.nan)

    # First obtain out-of-fold ROI predictions.
    for k in range(N_FOLDS):
        train = (fold != k) & contact
        test = (fold == k) & contact
        model = fit_ridge(loc_x[train], np.eye(9)[true_roi[train] - 1], weights_by_session(sessions[train]))
        oof_scores[test] = predict(model, loc_x[test])
    predicted_roi = np.argmax(oof_scores[contact], axis=1) + 1

    # Force validation is reported both with known ROI and end-to-end predicted ROI.
    oof_force_oracle = np.full(len(data), np.nan)
    oof_force_pipeline = np.full(len(data), np.nan)
    pipeline_roi_all = true_roi.copy()
    pipeline_roi_all[contact] = predicted_roi
    for k in range(N_FOLDS):
        train = fold != k
        test = fold == k
        train_x, force_names = force_features(data.loc[train], true_roi[train])
        test_oracle_x, _ = force_features(data.loc[test], true_roi[test])
        test_pipeline_x, _ = force_features(data.loc[test], pipeline_roi_all[test])
        force_model = fit_ridge(train_x, force_y[train], weights_by_session(sessions[train]))
        oof_force_oracle[test] = np.maximum(0, predict(force_model, test_oracle_x))
        oof_force_pipeline[test] = np.maximum(0, predict(force_model, test_pipeline_x))

    contact_idx = np.flatnonzero(contact)
    frame_pred = np.argmax(oof_scores[contact], axis=1) + 1
    confusion = np.zeros((9, 9), int)
    for actual, pred in zip(true_roi[contact], frame_pred):
        confusion[actual - 1, pred - 1] += 1

    per_roi = []
    for roi in range(1, 10):
        mask = contact & (true_roi == roi)
        preds = np.argmax(oof_scores[mask], axis=1) + 1
        per_roi.append({
            "roi": roi, "contact_frames": int(mask.sum()),
            "localization_accuracy": float(np.mean(preds == roi)),
            "force_mae_N_known_roi": float(np.mean(np.abs(oof_force_oracle[mask] - force_y[mask]))),
            "force_mae_N_end_to_end": float(np.mean(np.abs(oof_force_pipeline[mask] - force_y[mask]))),
        })

    # Final models use every valid manual-calibration frame.
    final_loc = fit_ridge(loc_x[contact], np.eye(9)[true_roi[contact] - 1], weights_by_session(sessions[contact]))
    final_force_x, force_names = force_features(data, true_roi)
    final_force = fit_ridge(final_force_x, force_y, weights_by_session(sessions))

    camera_controls = data[["applied_exposure", "applied_gain", "applied_white_balance"]].drop_duplicates().to_dict("records")
    artifact = {
        "model_version": "manual-1.0",
        "model_family": "ridge-regularized linear regression",
        "ridge_lambda": RIDGE,
        "contact_threshold_N": CONTACT_N,
        "training_scope": {
            "archive": str(ARCHIVE), "sessions": int(data.session.nunique()), "valid_frames": int(len(data)),
            "contact_frames": int(contact.sum()), "rois": sorted(data.target_roi.unique().tolist()),
            "force_range_N": [float(force_y.min()), float(force_y.max())], "camera_controls": camera_controls,
        },
        "pipeline": "localization scores -> selected ROI -> ROI-conditioned force regression",
        "localization_model": {**serialize(final_loc, loc_names), "classes": list(range(1, 10)), "decision": "argmax after averaging 5 consecutive frames"},
        "force_model": {**serialize(final_force, force_names), "prediction_floor_N": 0.0},
        "deployment_ready": False,
    }

    duplicate_frame_keys = int(raw_data.duplicated(["session", "capture_frame_id"]).sum())
    validation = {
        "data_quality": {
            "raw_sessions": int(raw_data.session.nunique()), "raw_frames": int(len(raw_data)), "valid_model_frames": int(len(data)),
            "invalid_or_nonfinite_frames": int(len(raw_data) - len(data)), "duplicate_session_frame_ids": duplicate_frame_keys,
            "saturation_warning_frames": int(truthy(raw_data.saturation_warning).sum()),
            "target_session_counts": raw_data[["session", "target_roi_ground_truth"]].drop_duplicates().target_roi_ground_truth.value_counts().sort_index().astype(int).to_dict(),
            "camera_control_combinations": camera_controls,
        },
        "validation_design": "Five-fold blocked within-session validation: each fold holds one contiguous fifth of every ROI recording.",
        "validation_limit": "Only one recording exists per ROI, so neither localization nor ROI-specific force calibration has independent repeat-session validation.",
        "localization_contact_frame_accuracy": float(np.mean(frame_pred == true_roi[contact])),
        "force_all_frames_known_roi": regression_metrics(force_y, oof_force_oracle),
        "force_all_frames_end_to_end": regression_metrics(force_y, oof_force_pipeline),
        "force_contact_frames_known_roi": regression_metrics(force_y[contact], oof_force_oracle[contact]),
        "force_contact_frames_end_to_end": regression_metrics(force_y[contact], oof_force_pipeline[contact]),
        "per_roi": per_roi,
        "assessment": "Experimental calibration model; requires new repeat trials for an honest deployment decision.",
    }

    (OUT / "manual_linear_models.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    (OUT / "validation_metrics.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    pd.DataFrame(confusion, index=[f"actual_{i}" for i in range(1, 10)], columns=[f"pred_{i}" for i in range(1, 10)]).to_csv(OUT / "localization_confusion.csv")
    pd.DataFrame(per_roi).to_csv(OUT / "per_roi_metrics.csv", index=False)
    pd.DataFrame({
        "session": sessions, "row_in_session": data.row_in_session, "fold": fold, "actual_roi": true_roi,
        "force_N": force_y, "predicted_force_known_roi_N": oof_force_oracle,
        "predicted_force_end_to_end_N": oof_force_pipeline,
        "predicted_roi": np.where(contact, pipeline_roi_all, 0), "contact": contact,
    }).to_csv(OUT / "out_of_fold_predictions.csv.gz", index=False, compression="gzip")
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    main()
