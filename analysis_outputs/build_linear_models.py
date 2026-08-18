"""Build and validate linear models from the full Auto Calibration archive.

The validation unit is an entire acquisition session.  This prevents adjacent
video frames from the same trial appearing in both train and test data.
"""

from __future__ import annotations

import csv
import io
import json
import math
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(r"C:\Users\DLSU\OneDrive\Documents\SOFTWARE CALIBRATION")
OUT = ROOT / "analysis_outputs" / "linear_models"
ARCHIVE = Path(r"C:\Users\DLSU\Downloads\Data Collection\Auto Calibration.zip")
SUMMARY = ROOT / "analysis_outputs" / "session_summary.csv"
CONTACT_N = 0.05
RIDGE = 0.01
N_FOLDS = 5

DELTA_COLS = [f"roi{i}_delta_v_mean" for i in range(1, 10)]
ACTIVE_COLS = [f"roi{i}_active_fraction" for i in range(1, 10)]
USECOLS = [
    "target_roi_ground_truth", "force_N", "frame_valid", "baseline_valid",
    "loadcell_valid", *DELTA_COLS, *ACTIVE_COLS,
]


def truthy(series: pd.Series) -> np.ndarray:
    return series.astype(str).str.lower().isin(["true", "1", "yes"]).to_numpy()


def extract_frames() -> pd.DataFrame:
    summary = pd.read_csv(SUMMARY)
    meta = summary.set_index("session_folder")[["target_roi", "camera_fingerprint"]].to_dict("index")
    parts = []
    with zipfile.ZipFile(ARCHIVE) as zf:
        members = sorted(n for n in zf.namelist() if n.endswith("/master_synchronized.csv"))
        for number, member in enumerate(members, 1):
            session = Path(member).parent.name
            with zf.open(member) as raw:
                frame = pd.read_csv(raw, usecols=USECOLS, low_memory=False)
            valid = truthy(frame["frame_valid"]) & truthy(frame["baseline_valid"]) & truthy(frame["loadcell_valid"])
            frame = frame.loc[valid, ["target_roi_ground_truth", "force_N", *DELTA_COLS, *ACTIVE_COLS]].copy()
            for col in ["target_roi_ground_truth", "force_N", *DELTA_COLS, *ACTIVE_COLS]:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
            frame = frame.dropna()
            frame["session"] = session
            frame["target_roi"] = int(meta[session]["target_roi"])
            frame["camera_fingerprint"] = meta[session]["camera_fingerprint"]
            parts.append(frame)
            if number % 25 == 0 or number == len(members):
                print(f"Extracted {number}/{len(members)} sessions")
    data = pd.concat(parts, ignore_index=True)
    data = data[(data.target_roi_ground_truth.astype(int) == data.target_roi.astype(int))]
    return data


def assign_folds(data: pd.DataFrame, camera_b: str) -> dict[str, int]:
    sessions = data[["session", "target_roi", "camera_fingerprint"]].drop_duplicates()
    folds = {}
    for (_, _), group in sessions.groupby(["target_roi", "camera_fingerprint"], sort=True):
        for i, session in enumerate(sorted(group.session)):
            folds[session] = i % N_FOLDS
    return folds


def raw_features(data: pd.DataFrame, camera_b: str) -> tuple[np.ndarray, list[str]]:
    base = data[DELTA_COLS + ACTIVE_COLS].to_numpy(float)
    cam = (data.camera_fingerprint.to_numpy() == camera_b).astype(float)[:, None]
    x = np.column_stack([base, cam, base * cam])
    names = DELTA_COLS + ACTIVE_COLS + ["camera_B"] + [f"{n}_x_camera_B" for n in DELTA_COLS + ACTIVE_COLS]
    return x, names


def localization_features(data: pd.DataFrame, camera_b: str) -> tuple[np.ndarray, list[str]]:
    base, base_names = raw_features(data, camera_b)
    delta = np.maximum(data[DELTA_COLS].to_numpy(float), 0)
    active = np.maximum(data[ACTIVE_COLS].to_numpy(float), 0)
    dshare = delta / np.maximum(delta.sum(axis=1, keepdims=True), 1e-9)
    ashare = active / np.maximum(active.sum(axis=1, keepdims=True), 1e-9)
    x = np.column_stack([base, dshare, ashare])
    names = base_names + [f"roi{i}_delta_share" for i in range(1, 10)] + [f"roi{i}_active_share" for i in range(1, 10)]
    return x, names


def session_weights(sessions: np.ndarray) -> np.ndarray:
    counts = Counter(sessions)
    w = np.array([1.0 / counts[s] for s in sessions], float)
    return w / w.mean()


def fit_ridge(x: np.ndarray, y: np.ndarray, w: np.ndarray, ridge: float = RIDGE) -> dict:
    wsum = w.sum()
    mean = (x * w[:, None]).sum(axis=0) / wsum
    var = (((x - mean) ** 2) * w[:, None]).sum(axis=0) / wsum
    scale = np.sqrt(np.maximum(var, 1e-12))
    xs = (x - mean) / scale
    if y.ndim == 1:
        ymean = float((y * w).sum() / wsum)
        yc = y - ymean
    else:
        ymean = (y * w[:, None]).sum(axis=0) / wsum
        yc = y - ymean
    a = (xs.T * w) @ xs / wsum + ridge * np.eye(xs.shape[1])
    b = (xs.T * w) @ yc / wsum
    coef = np.linalg.solve(a, b)
    return {"mean": mean, "scale": scale, "ymean": ymean, "coef": coef}


def predict(model: dict, x: np.ndarray) -> np.ndarray:
    return (x - model["mean"]) / model["scale"] @ model["coef"] + model["ymean"]


def regression_metrics(y: np.ndarray, pred: np.ndarray) -> dict:
    err = pred - y
    return {
        "n_frames": int(len(y)),
        "mae_N": float(np.mean(np.abs(err))),
        "rmse_N": float(np.sqrt(np.mean(err ** 2))),
        "r2": float(1 - np.sum(err ** 2) / np.sum((y - y.mean()) ** 2)) if np.var(y) else None,
        "bias_N": float(np.mean(err)),
    }


def to_jsonable_model(model: dict, names: list[str]) -> dict:
    coef = model["coef"]
    return {
        "feature_names": names,
        "feature_mean": np.asarray(model["mean"]).tolist(),
        "feature_scale": np.asarray(model["scale"]).tolist(),
        "target_mean_or_intercepts": np.asarray(model["ymean"]).tolist(),
        "standardized_coefficients": np.asarray(coef).tolist(),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = extract_frames()
    cameras = data.camera_fingerprint.value_counts()
    camera_a, camera_b = cameras.index[0], cameras.index[1]
    folds = assign_folds(data, camera_b)
    fold = data.session.map(folds).to_numpy(int)
    sessions = data.session.to_numpy()
    y_force = data.force_N.to_numpy(float)
    y_roi = data.target_roi.to_numpy(int)
    xf, force_names = raw_features(data, camera_b)
    xl, loc_names = localization_features(data, camera_b)
    contact = y_force >= CONTACT_N

    oof_force = np.full(len(data), np.nan)
    oof_scores = np.full((len(data), 9), np.nan)
    for k in range(N_FOLDS):
        train = fold != k
        test = fold == k
        force_model = fit_ridge(xf[train], y_force[train], session_weights(sessions[train]))
        oof_force[test] = np.maximum(0, predict(force_model, xf[test]))

        trc = train & contact
        tec = test & contact
        onehot = np.eye(9)[y_roi[trc] - 1]
        loc_model = fit_ridge(xl[trc], onehot, session_weights(sessions[trc]))
        oof_scores[tec] = predict(loc_model, xl[tec])
        print(f"Validated fold {k + 1}/{N_FOLDS}")

    force_all = regression_metrics(y_force, oof_force)
    force_contact = regression_metrics(y_force[contact], oof_force[contact])
    noncontact = ~contact
    force_all["noncontact_predicted_contact_rate"] = float(np.mean(oof_force[noncontact] >= CONTACT_N))
    force_all["contact_detection_sensitivity"] = float(np.mean(oof_force[contact] >= CONTACT_N))

    frame_pred = np.argmax(oof_scores[contact], axis=1) + 1
    frame_true = y_roi[contact]
    frame_acc = float(np.mean(frame_pred == frame_true))
    confusion = np.zeros((9, 9), int)
    for t, p in zip(frame_true, frame_pred):
        confusion[t - 1, p - 1] += 1

    contact_indices = np.flatnonzero(contact)
    contact_df = pd.DataFrame({
        "session": sessions[contact], "target_roi": frame_true,
        "camera_fingerprint": data.camera_fingerprint.to_numpy()[contact],
        "fold": fold[contact], "predicted_roi": frame_pred,
    })
    for j in range(9):
        contact_df[f"score_roi{j+1}"] = oof_scores[contact_indices, j]

    session_rows = []
    for session, group in contact_df.groupby("session", sort=True):
        means = group[[f"score_roi{i}" for i in range(1, 10)]].mean().to_numpy(float)
        order = np.argsort(means)
        pred_roi = int(order[-1] + 1)
        session_rows.append({
            "session": session,
            "target_roi": int(group.target_roi.iloc[0]),
            "predicted_roi": pred_roi,
            "correct": pred_roi == int(group.target_roi.iloc[0]),
            "camera_fingerprint": group.camera_fingerprint.iloc[0],
            "camera_configuration": "B" if group.camera_fingerprint.iloc[0] == camera_b else "A",
            "fold": int(group.fold.iloc[0]),
            "contact_frames": int(len(group)),
            "score_margin": float(means[order[-1]] - means[order[-2]]),
        })
    session_results = pd.DataFrame(session_rows)

    per_roi = []
    for roi, group in session_results.groupby("target_roi"):
        per_roi.append({"target_roi": int(roi), "sessions": int(len(group)), "session_accuracy": float(group.correct.mean())})
    per_camera = []
    for config, group in session_results.groupby("camera_configuration"):
        per_camera.append({"camera_configuration": config, "sessions": int(len(group)), "session_accuracy": float(group.correct.mean())})

    # Train deployment models on all valid frames.
    final_force = fit_ridge(xf, y_force, session_weights(sessions))
    final_loc = fit_ridge(xl[contact], np.eye(9)[y_roi[contact] - 1], session_weights(sessions[contact]))
    artifact = {
        "model_version": "1.0",
        "model_family": "ridge-regularized linear regression",
        "ridge_lambda": RIDGE,
        "contact_threshold_N": CONTACT_N,
        "training_scope": {
            "archive": str(ARCHIVE), "sessions": int(data.session.nunique()),
            "valid_frames": int(len(data)), "contact_frames": int(contact.sum()),
            "same_physical_skin": True, "camera_change_intentional": True,
        },
        "camera_configurations": {"A": camera_a, "B": camera_b},
        "force_model": {**to_jsonable_model(final_force, force_names), "prediction_floor_N": 0.0},
        "localization_model": {**to_jsonable_model(final_loc, loc_names), "classes": list(range(1, 10)), "decision": "argmax; average scores over 5 consecutive contact frames"},
    }
    validation = {
        "method": "5-fold grouped cross-validation; entire sessions held out; session-balanced training weights",
        "fold_session_counts": {str(k): int(data.loc[fold == k, "session"].nunique()) for k in range(N_FOLDS)},
        "force_all_frames": force_all,
        "force_contact_frames": force_contact,
        "localization_contact_frame_accuracy": frame_acc,
        "localization_session_accuracy": float(session_results.correct.mean()),
        "localization_session_correct": int(session_results.correct.sum()),
        "localization_sessions": int(len(session_results)),
        "localization_per_roi": per_roi,
        "localization_per_camera": per_camera,
        "deployment_assessment": "NOT READY for reliable force estimation or nine-ROI localization at the observed validation accuracy.",
        "limitations": [
            "No independent fixed-force run at every ROI was available, so spatial optical sensitivity and local mechanics cannot be fully separated.",
            "Validation estimates repeat-session performance on the same sensing skin and the two observed camera configurations, not transfer to a new skin or camera setup.",
            "Force ground truth is frame-aligned/interpolated load-cell data; dynamic timing error may limit instantaneous estimates.",
        ],
    }

    with open(OUT / "auto_calibration_linear_models.json", "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2)
    with open(OUT / "validation_metrics.json", "w", encoding="utf-8") as f:
        json.dump(validation, f, indent=2)
    session_results.to_csv(OUT / "session_level_oof_predictions.csv", index=False)
    pd.DataFrame(confusion, index=[f"actual_{i}" for i in range(1, 10)], columns=[f"pred_{i}" for i in range(1, 10)]).to_csv(OUT / "localization_confusion_frames.csv")
    pd.DataFrame({
        "session": sessions, "fold": fold, "target_roi": y_roi, "force_N": y_force,
        "predicted_force_N": oof_force,
    }).to_csv(OUT / "force_oof_predictions.csv.gz", index=False, compression="gzip")
    data[["session", "target_roi", "camera_fingerprint", "force_N", *DELTA_COLS, *ACTIVE_COLS]].to_parquet(OUT / "training_frames.parquet", index=False) if False else None

    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    main()
