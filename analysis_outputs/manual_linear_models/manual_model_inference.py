"""Inference for the experimental manual-calibration linear model."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import numpy as np


MODEL_PATH = Path(__file__).with_name("manual_linear_models.json")
STATS = ["delta_v_mean", "delta_v_max", "delta_v_p95", "delta_v_std", "active_fraction", "normalized_intensity"]
OPTICAL_NAMES = [f"roi{roi}_{stat}" for roi in range(1, 10) for stat in STATS]


def load_model(path: str | Path = MODEL_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _linear(saved: dict, features: np.ndarray) -> np.ndarray:
    mean = np.asarray(saved["feature_mean"], float)
    scale = np.asarray(saved["feature_scale"], float)
    coef = np.asarray(saved["standardized_coefficients"], float)
    target_mean = np.asarray(saved["target_mean"], float)
    return (features - mean) / scale @ coef + target_mean


def localization_features(frame: dict[str, float]) -> np.ndarray:
    raw = np.asarray([frame[name] for name in OPTICAL_NAMES], float)
    delta = np.maximum(np.asarray([frame[f"roi{i}_delta_v_mean"] for i in range(1, 10)], float), 0)
    active = np.maximum(np.asarray([frame[f"roi{i}_active_fraction"] for i in range(1, 10)], float), 0)
    return np.r_[raw, delta / max(float(delta.sum()), 1e-9), active / max(float(active.sum()), 1e-9)]


def force_features(frame: dict[str, float], selected_roi: int) -> np.ndarray:
    if selected_roi not in range(1, 10):
        raise ValueError("selected_roi must be 1 through 9")
    raw = np.asarray([frame[name] for name in OPTICAL_NAMES], float)
    onehot = np.eye(9)[selected_roi - 1]
    chosen = np.asarray([frame[f"roi{selected_roi}_{stat}"] for stat in STATS], float)
    interactions = np.outer(onehot, chosen).reshape(-1)
    return np.r_[raw, onehot, interactions]


def predict_frame(frame: dict[str, float], model: dict | None = None, selected_roi: int | None = None) -> dict:
    model = model or load_model()
    scores = np.asarray(_linear(model["localization_model"], localization_features(frame)), float)
    roi = selected_roi or int(np.argmax(scores) + 1)
    force_n = max(0.0, float(_linear(model["force_model"], force_features(frame, roi))))
    return {
        "predicted_roi_experimental": roi,
        "force_N_experimental": force_n,
        "roi_scores": scores.tolist(),
        "independent_repeat_validated": False,
        "deployment_ready": False,
    }


class FiveFramePredictor:
    def __init__(self, model: dict | None = None, window: int = 5):
        self.model = model or load_model()
        self.frames = deque(maxlen=window)

    def update(self, frame: dict[str, float]) -> dict:
        self.frames.append(frame)
        scores = np.mean([_linear(self.model["localization_model"], localization_features(f)) for f in self.frames], axis=0)
        roi = int(np.argmax(scores) + 1)
        forces = [_linear(self.model["force_model"], force_features(f, roi)) for f in self.frames]
        return {
            "predicted_roi_experimental": roi,
            "force_N_experimental": max(0.0, float(np.mean(forces))),
            "frames_in_window": len(self.frames),
            "independent_repeat_validated": False,
            "deployment_ready": False,
        }
