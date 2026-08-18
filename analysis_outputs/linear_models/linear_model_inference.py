"""Inference helper for the experimental Auto Calibration linear models.

This model did not meet deployment-quality validation thresholds.  It must not
be used for force safety limits or treated as a calibrated force instrument.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import numpy as np


MODEL_PATH = Path(__file__).with_name("auto_calibration_linear_models.json")
DELTA_NAMES = [f"roi{i}_delta_v_mean" for i in range(1, 10)]
ACTIVE_NAMES = [f"roi{i}_active_fraction" for i in range(1, 10)]


def load_model(path: str | Path = MODEL_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _linear(saved: dict, x: np.ndarray) -> np.ndarray:
    mean = np.asarray(saved["feature_mean"], dtype=float)
    scale = np.asarray(saved["feature_scale"], dtype=float)
    coef = np.asarray(saved["standardized_coefficients"], dtype=float)
    intercept = np.asarray(saved["target_mean_or_intercepts"], dtype=float)
    return (x - mean) / scale @ coef + intercept


def engineer_features(frame: dict[str, float], camera_fingerprint: str, model: dict) -> tuple[np.ndarray, np.ndarray]:
    delta = np.asarray([frame[name] for name in DELTA_NAMES], dtype=float)
    active = np.asarray([frame[name] for name in ACTIVE_NAMES], dtype=float)
    base = np.r_[delta, active]
    camera_b = float(camera_fingerprint == model["camera_configurations"]["B"])
    if camera_fingerprint not in model["camera_configurations"].values():
        raise ValueError("Unknown camera fingerprint; this model only supports configurations A and B")
    force_x = np.r_[base, camera_b, base * camera_b]
    dpos = np.maximum(delta, 0)
    apos = np.maximum(active, 0)
    dshare = dpos / max(float(dpos.sum()), 1e-9)
    ashare = apos / max(float(apos.sum()), 1e-9)
    loc_x = np.r_[force_x, dshare, ashare]
    return force_x, loc_x


def predict_frame(frame: dict[str, float], camera_fingerprint: str, model: dict | None = None) -> dict:
    model = model or load_model()
    force_x, loc_x = engineer_features(frame, camera_fingerprint, model)
    force_n = max(0.0, float(_linear(model["force_model"], force_x)))
    scores = np.asarray(_linear(model["localization_model"], loc_x), dtype=float)
    return {
        "force_N_experimental": force_n,
        "predicted_roi_experimental": int(np.argmax(scores) + 1),
        "roi_scores": scores.tolist(),
        "model_ready_for_deployment": False,
    }


class FiveFrameLocalizer:
    """Average five consecutive ROI score vectors before selecting the box."""

    def __init__(self, model: dict | None = None, window: int = 5):
        self.model = model or load_model()
        self.scores = deque(maxlen=window)

    def update(self, frame: dict[str, float], camera_fingerprint: str) -> dict:
        result = predict_frame(frame, camera_fingerprint, self.model)
        self.scores.append(result["roi_scores"])
        mean_scores = np.mean(np.asarray(self.scores), axis=0)
        result["predicted_roi_experimental"] = int(np.argmax(mean_scores) + 1)
        result["frames_in_window"] = len(self.scores)
        return result
