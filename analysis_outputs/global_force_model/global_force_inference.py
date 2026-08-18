"""Inference helper for the global manual-calibration force model.

No ROI label is required. The selected model estimates the force associated
with the previous camera frame because the optical response lagged the load
cell by about one frame in the calibration recordings.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


MODEL_PATH = Path(__file__).with_name("global_force_model.json")
STATS = ["delta_v_mean", "delta_v_max", "delta_v_p95", "delta_v_std", "active_fraction", "normalized_intensity"]
OPTICAL_NAMES = [f"roi{roi}_{stat}" for roi in range(1, 10) for stat in STATS]


def load_model(path: str | Path = MODEL_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def engineer_features(frame: dict[str, float]) -> np.ndarray:
    raw = np.asarray([frame[name] for name in OPTICAL_NAMES], float)
    cube = raw.reshape(9, len(STATS))
    aggregates = []
    for j in range(len(STATS)):
        values = cube[:, j]
        aggregates.extend([
            values.sum(), values.mean(), values.max(), values.min(),
            values.std(), np.maximum(values, 0).sum(),
        ])
    agg = np.asarray(aggregates, float)
    signed_log = np.sign(agg) * np.log1p(np.abs(agg))
    signed_square = np.sign(agg) * agg ** 2
    return np.r_[raw, agg, signed_log, signed_square]


def predict_force(frame: dict[str, float], model: dict | None = None) -> dict:
    model = model or load_model()
    saved = model["model"]
    x = engineer_features(frame)
    mean = np.asarray(saved["feature_mean"], float)
    scale = np.asarray(saved["feature_scale"], float)
    coef = np.asarray(saved["standardized_coefficients"], float)
    prediction = float((x - mean) / scale @ coef + saved["target_mean"])
    return {
        "global_force_N_experimental": max(0.0, prediction),
        "estimate_delay_frames": int(model["target_delay_frames"]),
        "uses_roi_label": False,
        "deployment_ready": False,
    }
