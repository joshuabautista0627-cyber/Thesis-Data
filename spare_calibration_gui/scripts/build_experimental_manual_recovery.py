"""Build the disclosed manual-only post-hoc recovery model and replay bundle.

This command deliberately does not change the failed frozen preprocessing run.
It creates a separate ``experimental`` artifact whose release decision records
that the existing outer folds were inspected during recovery work and that the
original all-ROI common-range gate remains failed.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import accuracy_score, f1_score, recall_score
from scipy.stats import spearmanr

from scripts.fit_manual_only_preprocessing import pair_lagged_force
from scripts.live_sensor_common import PROJECT_ROOT, sha256_file


SCHEMA_VERSION = "1.0.0"
CONFIG_PATH = PROJECT_ROOT / "config" / "live_sensor_manual_only.json"
SPEC_PATH = PROJECT_ROOT / "config" / "manual_only_experimental_recovery.json"
FEATURE_ROOT = PROJECT_ROOT / "analysis_outputs" / "live_sensor_manual_only" / "feature_store"
SPLIT_ROOT = PROJECT_ROOT / "analysis_outputs" / "live_sensor_manual_only" / "splits"
PREPROCESSING_ROOT = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "model_runs"
    / "preprocessing"
)
RUN_ROOT = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "model_runs"
    / "experimental_recovery"
)
DEFAULT_BUNDLE = PROJECT_ROOT / "models" / "live_sensor_experimental_manual_recovery_v1"

SIGNED_COLUMNS = [f"roi{roi}_signed_delta_v_sum" for roi in range(1, 10)]
ACTIVE_COLUMNS = [f"roi{roi}_active_fraction" for roi in range(1, 10)]
BASE_COLUMNS = [
    "session_id",
    "scientific_role",
    "test_group",
    "target_roi",
    "video_frame_index",
    "capture_monotonic_relative_s",
    "synchronized_monotonic_relative_s",
    "reference_force_corrected_N",
    "force_valid",
    "optical_valid",
]


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _latest_passing(root: Path, report_name: str) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for report_path in root.glob(f"*/{report_name}"):
        report = _load_json(report_path)
        if report.get("gate_pass") and report.get("dataset_policy") == "manual_only":
            matches.append((report_path.parent, report))
    if not matches:
        raise FileNotFoundError(f"no passing manual-only {report_name} under {root}")
    return sorted(matches, key=lambda item: item[0].name)[-1]


def _load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, Any], dict[int, int]]:
    feature_dir, feature_report = _latest_passing(FEATURE_ROOT, "reconciliation_report.json")
    split_dir, split_report = _latest_passing(SPLIT_ROOT, "leakage_audit.json")
    if feature_report["source_manifest_hash"] != split_report["source_manifest_hash"]:
        raise ValueError("feature and split source-manifest hashes differ")
    split = pd.read_parquet(split_dir / "split_manifest.parquet")
    paths = sorted((feature_dir / "sessions" / "archive_id=manual").glob("*.parquet"))
    if len(paths) != 83:
        raise ValueError(f"expected 83 manual-only partitions; found {len(paths)}")
    frames = pd.concat(
        (pd.read_parquet(path, columns=BASE_COLUMNS + SIGNED_COLUMNS + ACTIVE_COLUMNS) for path in paths),
        ignore_index=True,
    )
    frames = frames.merge(
        split[["session_id", "outer_fold", "no_contact_fold"]],
        on="session_id",
        how="left",
        validate="many_to_one",
    )
    if set(frames["scientific_role"].astype(str)) != {
        "model_primary",
        "no_contact",
        "replay_only",
    }:
        raise ValueError("unexpected scientific role in the feature store")
    if not frames["force_valid"].all() or not frames["optical_valid"].all():
        raise ValueError("invalid feature rows reached recovery modeling")
    summaries = sorted(PREPROCESSING_ROOT.glob("*/preprocessing_summary.json"))
    if not summaries:
        raise FileNotFoundError("the authoritative failed preprocessing summary is missing")
    preprocessing = _load_json(summaries[-1])
    if preprocessing.get("gate_pass") is not False:
        raise ValueError("recovery expects the authoritative preprocessing gate to remain failed")
    lag_by_fold = {
        int(key): int(value["selected_lag_ms"])
        for key, value in preprocessing["folds"].items()
        if key != "development"
    }
    if set(lag_by_fold) != set(range(1, 7)):
        raise ValueError("six fixed outer-fold lags are required")
    return frames, split, feature_report, split_report, lag_by_fold


def _training_frames(frames: pd.DataFrame, fold: int) -> pd.DataFrame:
    primary = (frames["scientific_role"] == "model_primary") & (frames["outer_fold"] != fold)
    no_contact = (frames["scientific_role"] == "no_contact") & (
        frames["no_contact_fold"] != fold
    )
    return frames.loc[primary | no_contact].copy()


def _outer_frames(frames: pd.DataFrame, fold: int) -> pd.DataFrame:
    primary = (frames["scientific_role"] == "model_primary") & (frames["outer_fold"] == fold)
    no_contact = (frames["scientific_role"] == "no_contact") & (
        frames["no_contact_fold"] == fold
    )
    return frames.loc[primary | no_contact].copy()


def _normalizer(no_contact: pd.DataFrame, columns: list[str], scale_floor: float) -> tuple[np.ndarray, np.ndarray]:
    if no_contact["session_id"].nunique() < 2:
        raise ValueError("at least two no-contact sessions are required for normalization")
    center = no_contact.groupby("session_id")[columns].median().median(axis=0).to_numpy(float)
    deviations = [
        np.quantile(np.abs(group[columns].to_numpy(float) - center), 0.99, axis=0)
        for _, group in no_contact.groupby("session_id", sort=False)
    ]
    scale = np.maximum(np.median(np.vstack(deviations), axis=0), scale_floor)
    if not np.isfinite(center).all() or not np.isfinite(scale).all():
        raise ValueError("normalization produced non-finite values")
    return center, scale


def _causal_mean(values: np.ndarray, session_ids: Iterable[object], window: int) -> np.ndarray:
    frame = pd.DataFrame(np.asarray(values, dtype=float))
    frame.insert(0, "session_id", np.asarray(list(session_ids)).astype(str))
    result = np.empty((len(frame), values.shape[1]), dtype=float)
    for indices in frame.groupby("session_id", sort=False).indices.values():
        index = np.asarray(indices)
        result[index] = (
            frame.iloc[index, 1:].rolling(window, min_periods=1).mean().to_numpy(float)
        )
    return result


def _normalized_smoothed(
    frames: pd.DataFrame,
    columns: list[str],
    center: np.ndarray,
    scale: np.ndarray,
    window: int,
) -> np.ndarray:
    normalized = (frames[columns].to_numpy(float) - center.reshape(1, -1)) / scale.reshape(1, -1)
    return _causal_mean(normalized, frames["session_id"], window)


def _session_balanced_weights(session_ids: Iterable[object], force: np.ndarray) -> np.ndarray:
    sessions = np.asarray(list(session_ids)).astype(str)
    bins = np.floor(np.clip(force, 1.7, 2.999999) / 0.25).astype(int)
    table = pd.DataFrame({"session": sessions, "bin": bins})
    counts = table.groupby(["session", "bin"]).size()
    bins_per_session = counts.reset_index().groupby("session")["bin"].nunique()
    keys = pd.MultiIndex.from_arrays([table["session"], table["bin"]])
    weights = 1.0 / (
        counts.reindex(keys).to_numpy(float)
        * bins_per_session.reindex(table["session"]).to_numpy(float)
    )
    return weights / weights.mean()


def _fallback_threshold(scores: np.ndarray, frames: pd.DataFrame, quantile: float) -> float:
    values = pd.Series(scores, index=frames.index)
    thresholds = sorted(
        float(np.quantile(values.loc[group.index], quantile, method="higher"))
        for _, group in frames.groupby("session_id", sort=False)
    )
    if len(thresholds) < 2:
        raise ValueError("fallback threshold requires at least two sessions")
    return float(np.median(thresholds))


def _calibration_and_test_indices(frames: pd.DataFrame, fraction: float) -> tuple[np.ndarray, np.ndarray]:
    calibration: list[int] = []
    test: list[int] = []
    for _, group in frames.groupby("session_id", sort=False):
        ordered = group.sort_values("video_frame_index")
        split_at = max(1, min(len(ordered) - 1, int(math.floor(len(ordered) * fraction))))
        calibration.extend(ordered.index[:split_at])
        test.extend(ordered.index[split_at:])
    return np.asarray(calibration, dtype=int), np.asarray(test, dtype=int)


def _debounce(flags: np.ndarray, session_ids: Iterable[object], acquire: int, clear: int) -> np.ndarray:
    result = np.zeros(len(flags), dtype=bool)
    table = pd.DataFrame({"session": np.asarray(list(session_ids)).astype(str)})
    for indices in table.groupby("session", sort=False).indices.values():
        active = False
        yes = 0
        no = 0
        for index in np.asarray(indices):
            if bool(flags[index]):
                yes += 1
                no = 0
                if not active and yes >= acquire:
                    active = True
            else:
                no += 1
                yes = 0
                if active and no >= clear:
                    active = False
            result[index] = active
    return result


def _roi_switch_debounce(
    predicted_roi: np.ndarray,
    session_ids: Iterable[object],
    switch_frames: int,
) -> np.ndarray:
    """Keep a forced ROI on every frame while requiring stable switches."""

    values = np.asarray(predicted_roi, dtype=int)
    if switch_frames <= 1:
        return values.copy()
    result = np.empty(len(values), dtype=int)
    table = pd.DataFrame({"session": np.asarray(list(session_ids)).astype(str)})
    for indices in table.groupby("session", sort=False).indices.values():
        current: int | None = None
        candidate: int | None = None
        candidate_count = 0
        for index in np.asarray(indices):
            value = int(values[index])
            if current is None:
                current = value
            elif value == current:
                candidate = None
                candidate_count = 0
            elif value == candidate:
                candidate_count += 1
                if candidate_count >= switch_frames:
                    current = value
                    candidate = None
                    candidate_count = 0
            else:
                candidate = value
                candidate_count = 1
            result[index] = current
    return result


def _top_two_margin(values: np.ndarray) -> np.ndarray:
    ordered = np.sort(np.asarray(values, dtype=float), axis=1)
    return ordered[:, -1] - ordered[:, -2]


def _select_localization_threshold(
    margins: np.ndarray,
    true_roi: np.ndarray,
    predicted_roi: np.ndarray,
    minimum_coverage: float,
) -> float:
    """Fit a deterministic selective-output threshold on training frames only."""

    margins = np.asarray(margins, dtype=float)
    if not 0.0 < minimum_coverage <= 1.0:
        raise ValueError("selective localization coverage must be in (0, 1]")
    candidates = np.unique(
        np.quantile(margins, np.linspace(0.0, 1.0 - minimum_coverage, 41))
    )
    best_score = -math.inf
    best_threshold = float(candidates[0])
    for threshold in candidates:
        retained = margins >= threshold
        if float(np.mean(retained)) + 1e-12 < minimum_coverage:
            continue
        score = float(
            f1_score(
                true_roi[retained],
                predicted_roi[retained],
                labels=list(range(1, 10)),
                average="macro",
                zero_division=0,
            )
        )
        if score > best_score + 1e-12 or (
            abs(score - best_score) <= 1e-12 and threshold < best_threshold
        ):
            best_score = score
            best_threshold = float(threshold)
    return best_threshold


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    sorted_values = np.asarray(values, dtype=float)[order]
    sorted_weights = np.asarray(weights, dtype=float)[order]
    cutoff = 0.5 * float(np.sum(sorted_weights))
    return float(sorted_values[np.searchsorted(np.cumsum(sorted_weights), cutoff, side="left")])


def _force_resolution_metrics(true_force: np.ndarray, predicted_force: np.ndarray) -> dict[str, float]:
    true_force = np.asarray(true_force, dtype=float)
    predicted_force = np.asarray(predicted_force, dtype=float)
    pearson = float(np.corrcoef(true_force, predicted_force)[0, 1])
    spearman = float(spearmanr(true_force, predicted_force).statistic)
    true_sd = float(np.std(true_force))
    predicted_sd = float(np.std(predicted_force))
    if not math.isfinite(pearson):
        pearson = 0.0
    if not math.isfinite(spearman):
        spearman = 0.0
    return {
        "force_pearson_r": pearson,
        "force_spearman_rho": spearman,
        "true_force_sd_N": true_sd,
        "predicted_force_sd_N": predicted_sd,
        "predicted_to_true_sd_ratio": predicted_sd / max(true_sd, 1e-12),
    }


def _episode_count(flags: np.ndarray) -> int:
    if not len(flags):
        return 0
    previous = np.r_[False, flags[:-1]]
    return int(np.sum(flags & ~previous))


def _balanced_mean(table: pd.DataFrame, value: str) -> float:
    return float(table.groupby("session_id")[value].mean().mean())


def _evaluate_fold(
    frames: pd.DataFrame,
    fold: int,
    lag_ms: int,
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    window = int(spec["temporal_filter"]["window_frames"])
    minimum = float(spec["operating_range_N"]["minimum"])
    maximum = float(spec["operating_range_N"]["maximum"])
    scale_floor = float(spec["normalization"]["scale_floor"])
    training = _training_frames(frames, fold).sort_values(["session_id", "video_frame_index"]).reset_index(drop=True)
    outer = _outer_frames(frames, fold).sort_values(["session_id", "video_frame_index"]).reset_index(drop=True)
    train_no_contact = training[training["scientific_role"] == "no_contact"]
    outer_no_contact = outer[outer["scientific_role"] == "no_contact"]
    outer_primary = outer[outer["scientific_role"] == "model_primary"]

    signed_center, signed_scale = _normalizer(train_no_contact, SIGNED_COLUMNS, scale_floor)
    active_center, active_scale = _normalizer(train_no_contact, ACTIVE_COLUMNS, scale_floor)
    train_signed = _normalized_smoothed(training, SIGNED_COLUMNS, signed_center, signed_scale, window)
    outer_signed = _normalized_smoothed(outer, SIGNED_COLUMNS, signed_center, signed_scale, window)
    train_active = _normalized_smoothed(training, ACTIVE_COLUMNS, active_center, active_scale, window)
    outer_active = _normalized_smoothed(outer, ACTIVE_COLUMNS, active_center, active_scale, window)
    train_scores = np.ptp(train_signed, axis=1)
    outer_scores = np.ptp(outer_signed, axis=1)

    train_no_contact_mask = training["scientific_role"].eq("no_contact").to_numpy()
    fallback = _fallback_threshold(
        train_scores[train_no_contact_mask],
        training.loc[train_no_contact_mask].reset_index(drop=True),
        0.95,
    )
    calibration_index, test_index = _calibration_and_test_indices(
        outer_no_contact,
        float(spec["contact"]["warmup_fraction_for_replay"]),
    )
    calibration_threshold = float(
        np.quantile(
            outer_scores[calibration_index],
            float(spec["contact"]["unloaded_warmup_quantile"]),
            method="higher",
        )
    )
    threshold = max(fallback, calibration_threshold)
    raw_no_contact = outer_scores[test_index] >= threshold
    raw_fpr = float(np.mean(raw_no_contact))
    debounced_no_contact = _debounce(
        raw_no_contact,
        outer.loc[test_index, "session_id"],
        int(spec["contact"]["acquire_frames"]),
        int(spec["contact"]["clear_frames"]),
    )

    training_force = pair_lagged_force(training, lag_ms)
    outer_force = pair_lagged_force(outer, lag_ms)
    training_primary = training["scientific_role"].eq("model_primary").to_numpy()
    outer_primary_mask = outer["scientific_role"].eq("model_primary").to_numpy()
    train_y = training_force.to_numpy(float)
    outer_y = outer_force.to_numpy(float)
    fit_mask = training_primary & np.isfinite(train_y) & (train_y >= minimum) & (train_y <= maximum)
    evaluation_mask = outer_primary_mask & np.isfinite(outer_y) & (outer_y >= minimum) & (outer_y <= maximum)
    weights = _session_balanced_weights(training.loc[fit_mask, "session_id"], train_y[fit_mask])
    force_model = IsotonicRegression(y_min=minimum, y_max=maximum, out_of_bounds="clip")
    force_model.fit(train_scores[fit_mask], train_y[fit_mask], sample_weight=weights)
    conditional_prediction = force_model.predict(outer_scores[evaluation_mask])
    detected_raw = outer_scores[evaluation_mask] >= threshold
    if bool(spec.get("evaluation", {}).get("full_sequence_contact_state", False)):
        full_sequence_detected = _debounce(
            outer_scores >= threshold,
            outer["session_id"],
            int(spec["contact"]["acquire_frames"]),
            int(spec["contact"]["clear_frames"]),
        )
        detected = full_sequence_detected[evaluation_mask]
    else:
        detected = _debounce(
            detected_raw,
            outer.loc[evaluation_mask, "session_id"],
            int(spec["contact"]["acquire_frames"]),
            int(spec["contact"]["clear_frames"]),
        )
    displayed_prediction = np.where(detected, conditional_prediction, 0.0)
    true_force = outer_y[evaluation_mask]
    true_roi = outer.loc[evaluation_mask, "target_roi"].astype(int).to_numpy()
    session_ids = outer.loc[evaluation_mask, "session_id"].astype(str).to_numpy()
    raw_predicted_roi = np.argmax(outer_active[evaluation_mask], axis=1) + 1
    switch_frames = int(spec.get("localization", {}).get("switch_frames", 1))
    predicted_roi = _roi_switch_debounce(raw_predicted_roi, session_ids, switch_frames)

    train_true_roi = training.loc[fit_mask, "target_roi"].astype(int).to_numpy()
    train_session_ids = training.loc[fit_mask, "session_id"].astype(str).to_numpy()
    train_raw_roi = np.argmax(train_active[fit_mask], axis=1) + 1
    train_predicted_roi = _roi_switch_debounce(
        train_raw_roi, train_session_ids, switch_frames
    )
    selective = spec.get("localization", {}).get("selective_output", {})
    if bool(selective.get("enabled", False)):
        localization_threshold = _select_localization_threshold(
            _top_two_margin(train_active[fit_mask]),
            train_true_roi,
            train_predicted_roi,
            float(selective["minimum_training_coverage"]),
        )
    else:
        localization_threshold = float(selective.get("fixed_margin_threshold", 0.5))
    localization_margin = _top_two_margin(outer_active[evaluation_mask])
    localization_retained = localization_margin >= localization_threshold

    constant_force = _weighted_median(train_y[fit_mask], weights)
    constant_error = np.full(len(true_force), constant_force) - true_force
    prediction_rows = pd.DataFrame(
        {
            "outer_fold": fold,
            "session_id": session_ids,
            "video_frame_index": outer.loc[evaluation_mask, "video_frame_index"].astype(int).to_numpy(),
            "target_roi": true_roi,
            "force_N": true_force,
            "contact_score": outer_scores[evaluation_mask],
            "contact_threshold": threshold,
            "detected_raw": detected_raw,
            "detected_debounced": detected,
            "conditional_force_N": conditional_prediction,
            "displayed_force_N": displayed_prediction,
            "constant_force_N": constant_force,
            "raw_predicted_roi": raw_predicted_roi,
            "predicted_roi": predicted_roi,
            "localization_margin": localization_margin,
            "localization_confidence_threshold": localization_threshold,
            "localization_retained": localization_retained,
        }
    )
    prediction_rows["conditional_error_N"] = conditional_prediction - true_force
    prediction_rows["displayed_error_N"] = displayed_prediction - true_force
    prediction_rows["constant_error_N"] = constant_error
    prediction_rows["location_correct"] = predicted_roi == true_roi

    raw_recall = _balanced_mean(
        prediction_rows.assign(value=detected_raw.astype(float)), "value"
    )
    recall = _balanced_mean(
        prediction_rows.assign(value=detected.astype(float)), "value"
    )
    reported_recall = (
        recall
        if bool(
            spec.get("evaluation", {}).get(
                "report_debounced_contact_recall", False
            )
        )
        else raw_recall
    )
    conditional_mae = _balanced_mean(
        prediction_rows.assign(value=np.abs(prediction_rows["conditional_error_N"])), "value"
    )
    displayed_mae = _balanced_mean(
        prediction_rows.assign(value=np.abs(prediction_rows["displayed_error_N"])), "value"
    )
    rmse = math.sqrt(
        _balanced_mean(prediction_rows.assign(value=prediction_rows["conditional_error_N"] ** 2), "value")
    )
    bias = _balanced_mean(prediction_rows.assign(value=prediction_rows["conditional_error_N"]), "value")
    constant_mae = _balanced_mean(
        prediction_rows.assign(value=np.abs(prediction_rows["constant_error_N"])), "value"
    )
    duration_s = 0.0
    for _, group in outer.loc[test_index].groupby("session_id"):
        times = group["capture_monotonic_relative_s"].to_numpy(float)
        if len(times) > 1:
            duration_s += max(0.0, float(np.nanmax(times) - np.nanmin(times)))
    episodes = _episode_count(debounced_no_contact)
    episodes_per_minute = episodes / max(duration_s / 60.0, 1e-9)
    metrics = {
        "outer_fold": fold,
        "lag_ms": lag_ms,
        "threshold": threshold,
        "fallback_threshold": fallback,
        "warmup_threshold": calibration_threshold,
        "no_contact_test_frames": int(len(test_index)),
        "no_contact_frame_fpr": raw_fpr,
        "no_contact_debounced_frame_fpr": float(np.mean(debounced_no_contact)),
        "false_contact_episodes": episodes,
        "false_contact_episodes_per_minute": episodes_per_minute,
        "raw_contact_recall": raw_recall,
        "debounced_contact_recall": recall,
        "contact_recall": reported_recall,
        "conditional_force_mae_N": conditional_mae,
        "displayed_force_mae_N": displayed_mae,
        "weighted_median_constant_N": constant_force,
        "weighted_median_constant_mae_N": constant_mae,
        "conditional_mae_gain_over_constant_N": constant_mae - conditional_mae,
        "force_rmse_N": rmse,
        "force_bias_N": bias,
        **_force_resolution_metrics(true_force, conditional_prediction),
        "raw_localization_accuracy": float(accuracy_score(true_roi, raw_predicted_roi)),
        "raw_localization_macro_f1": float(
            f1_score(true_roi, raw_predicted_roi, average="macro")
        ),
        "forced_localization_accuracy": float(accuracy_score(true_roi, predicted_roi)),
        "forced_localization_macro_f1": float(f1_score(true_roi, predicted_roi, average="macro")),
        "selective_localization_threshold": localization_threshold,
        "selective_localization_coverage": float(np.mean(localization_retained)),
        "selective_localization_accuracy": float(
            accuracy_score(true_roi[localization_retained], predicted_roi[localization_retained])
        ),
        "selective_localization_macro_f1": float(
            f1_score(
                true_roi[localization_retained],
                predicted_roi[localization_retained],
                labels=list(range(1, 10)),
                average="macro",
                zero_division=0,
            )
        ),
    }
    return metrics, prediction_rows


def _aggregate(
    folds: list[dict[str, Any]],
    predictions: pd.DataFrame,
    spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    true_roi = predictions["target_roi"].astype(int)
    predicted_roi = predictions["predicted_roi"].astype(int)
    raw_predicted_roi = predictions["raw_predicted_roi"].astype(int)
    retained = predictions["localization_retained"].astype(bool)
    per_roi_force = {
        str(roi): float(
            predictions[predictions["target_roi"] == roi]
            .groupby("session_id")["conditional_error_N"]
            .apply(lambda value: np.mean(np.abs(value)))
            .mean()
        )
        for roi in range(1, 10)
    }
    per_roi_recall = recall_score(
        true_roi, predicted_roi, labels=list(range(1, 10)), average=None, zero_division=0
    )
    mean_conditional_mae = float(
        np.mean([row["conditional_force_mae_N"] for row in folds])
    )
    mean_constant_mae = float(
        np.mean([row["weighted_median_constant_mae_N"] for row in folds])
    )
    mean_spearman = float(np.mean([row["force_spearman_rho"] for row in folds]))
    mean_sd_ratio = float(
        np.mean([row["predicted_to_true_sd_ratio"] for row in folds])
    )
    resolution = (spec or {}).get("force", {}).get("resolution_checks", {})
    minimum_relative_gain = float(
        resolution.get("minimum_relative_mae_gain_over_constant", 0.05)
    )
    minimum_spearman = float(resolution.get("minimum_mean_spearman_rho", 0.30))
    minimum_sd_ratio = float(
        resolution.get("minimum_mean_predicted_to_true_sd_ratio", 0.25)
    )
    relative_gain = (mean_constant_mae - mean_conditional_mae) / max(
        mean_constant_mae, 1e-12
    )
    return {
        "fold_count": 6,
        "operating_range_N": [1.7, 3.0],
        "mean_no_contact_frame_fpr": float(np.mean([row["no_contact_frame_fpr"] for row in folds])),
        "max_no_contact_frame_fpr": float(np.max([row["no_contact_frame_fpr"] for row in folds])),
        "mean_contact_recall": float(np.mean([row["contact_recall"] for row in folds])),
        "min_contact_recall": float(np.min([row["contact_recall"] for row in folds])),
        "mean_raw_contact_recall": float(
            np.mean([row["raw_contact_recall"] for row in folds])
        ),
        "mean_conditional_force_mae_N": mean_conditional_mae,
        "mean_displayed_force_mae_N": float(np.mean([row["displayed_force_mae_N"] for row in folds])),
        "mean_weighted_median_constant_mae_N": mean_constant_mae,
        "conditional_mae_gain_over_constant_N": mean_constant_mae
        - mean_conditional_mae,
        "conditional_mae_relative_gain_over_constant": relative_gain,
        "mean_force_rmse_N": float(np.mean([row["force_rmse_N"] for row in folds])),
        "absolute_force_bias_N": abs(float(np.mean([row["force_bias_N"] for row in folds]))),
        "mean_force_pearson_r": float(
            np.mean([row["force_pearson_r"] for row in folds])
        ),
        "mean_force_spearman_rho": mean_spearman,
        "mean_predicted_to_true_sd_ratio": mean_sd_ratio,
        "force_resolution_established": bool(
            relative_gain >= minimum_relative_gain
            and mean_spearman >= minimum_spearman
            and mean_sd_ratio >= minimum_sd_ratio
        ),
        "raw_localization_accuracy": float(
            accuracy_score(true_roi, raw_predicted_roi)
        ),
        "raw_localization_macro_f1": float(
            f1_score(true_roi, raw_predicted_roi, average="macro")
        ),
        "forced_localization_accuracy": float(accuracy_score(true_roi, predicted_roi)),
        "forced_localization_macro_f1": float(f1_score(true_roi, predicted_roi, average="macro")),
        "selective_localization_coverage": float(np.mean(retained)),
        "selective_localization_accuracy": float(
            accuracy_score(true_roi[retained], predicted_roi[retained])
        ),
        "selective_localization_macro_f1": float(
            f1_score(
                true_roi[retained],
                predicted_roi[retained],
                labels=list(range(1, 10)),
                average="macro",
                zero_division=0,
            )
        ),
        "per_roi_localization_recall": {
            str(roi): float(per_roi_recall[roi - 1]) for roi in range(1, 10)
        },
        "per_roi_conditional_force_mae_N": per_roi_force,
        "max_false_contact_episodes_per_minute": float(
            np.max([row["false_contact_episodes_per_minute"] for row in folds])
        ),
        "cross_validated_p95_absolute_error_N": float(
            np.quantile(np.abs(predictions["conditional_error_N"]), 0.95)
        ),
    }


def _fit_final_model(frames: pd.DataFrame, spec: Mapping[str, Any], lag_ms: int) -> dict[str, Any]:
    eligible = frames[frames["scientific_role"].isin(["model_primary", "no_contact"])].copy()
    eligible = eligible.sort_values(["session_id", "video_frame_index"]).reset_index(drop=True)
    no_contact = eligible[eligible["scientific_role"] == "no_contact"]
    scale_floor = float(spec["normalization"]["scale_floor"])
    window = int(spec["temporal_filter"]["window_frames"])
    signed_center, signed_scale = _normalizer(no_contact, SIGNED_COLUMNS, scale_floor)
    active_center, active_scale = _normalizer(no_contact, ACTIVE_COLUMNS, scale_floor)
    signed = _normalized_smoothed(eligible, SIGNED_COLUMNS, signed_center, signed_scale, window)
    active = _normalized_smoothed(
        eligible, ACTIVE_COLUMNS, active_center, active_scale, window
    )
    score = np.ptp(signed, axis=1)
    no_contact_mask = eligible["scientific_role"].eq("no_contact").to_numpy()
    fallback = _fallback_threshold(
        score[no_contact_mask], eligible.loc[no_contact_mask].reset_index(drop=True), 0.95
    )
    force = pair_lagged_force(eligible, lag_ms).to_numpy(float)
    minimum = float(spec["operating_range_N"]["minimum"])
    maximum = float(spec["operating_range_N"]["maximum"])
    fit = (
        eligible["scientific_role"].eq("model_primary").to_numpy()
        & np.isfinite(force)
        & (force >= minimum)
        & (force <= maximum)
    )
    weights = _session_balanced_weights(eligible.loc[fit, "session_id"], force[fit])
    model = IsotonicRegression(y_min=minimum, y_max=maximum, out_of_bounds="clip")
    model.fit(score[fit], force[fit], sample_weight=weights)
    switch_frames = int(spec.get("localization", {}).get("switch_frames", 1))
    raw_roi = np.argmax(active[fit], axis=1) + 1
    true_roi = eligible.loc[fit, "target_roi"].astype(int).to_numpy()
    session_ids = eligible.loc[fit, "session_id"].astype(str).to_numpy()
    predicted_roi = _roi_switch_debounce(raw_roi, session_ids, switch_frames)
    selective = spec.get("localization", {}).get("selective_output", {})
    if bool(selective.get("enabled", False)):
        localization_threshold = _select_localization_threshold(
            _top_two_margin(active[fit]),
            true_roi,
            predicted_roi,
            float(selective["minimum_training_coverage"]),
        )
    else:
        localization_threshold = float(selective.get("fixed_margin_threshold", 0.5))
    return {
        "signed_center": signed_center,
        "signed_scale": signed_scale,
        "active_center": active_center,
        "active_scale": active_scale,
        "fallback_threshold": fallback,
        "force_x": np.asarray(model.X_thresholds_, dtype=float),
        "force_y": np.asarray(model.y_thresholds_, dtype=float),
        "localization_confidence_threshold": localization_threshold,
        "score": score,
        "eligible": eligible,
    }


def _write_demo(path: Path, frames: pd.DataFrame, final: Mapping[str, Any]) -> dict[str, Any]:
    no_contact = frames[frames["scientific_role"] == "no_contact"].copy()
    no_contact = no_contact.sort_values(["session_id", "video_frame_index"])
    calibration_session = str(no_contact["session_id"].iloc[0])
    calibration = no_contact[no_contact["session_id"] == calibration_session].head(160)
    replay = frames[frames["scientific_role"] == "replay_only"].copy()
    signed = (replay[SIGNED_COLUMNS].to_numpy(float) - final["signed_center"]) / final["signed_scale"]
    replay = replay.assign(_score=np.ptp(signed, axis=1))
    chosen = (
        replay.groupby("session_id")["_score"].quantile(0.9).sort_values(ascending=False).index[0]
    )
    contact = replay[replay["session_id"] == chosen].sort_values("video_frame_index").head(360)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        frame_id = 0
        for phase, source in (("unloaded_warmup", calibration), ("replay", contact)):
            for _, row in source.iterrows():
                payload: dict[str, Any] = {"phase": phase, "capture_frame_id": frame_id}
                for column in SIGNED_COLUMNS + ACTIVE_COLUMNS:
                    payload[column] = float(row[column])
                handle.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")
                frame_id += 1
    return {
        "calibration_session_id": calibration_session,
        "replay_session_id": str(chosen),
        "frame_count": frame_id,
        "model_fit_role": False,
    }


def _write_bundle(
    directory: Path,
    final: Mapping[str, Any],
    metrics: Mapping[str, Any],
    provenance: Mapping[str, Any],
    spec: Mapping[str, Any],
    frames: pd.DataFrame,
) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    preprocessing = {
        "schema_version": SCHEMA_VERSION,
        "feature_order": {"signed": SIGNED_COLUMNS, "active": ACTIVE_COLUMNS},
        "signed_center": final["signed_center"].tolist(),
        "signed_scale": final["signed_scale"].tolist(),
        "active_center": final["active_center"].tolist(),
        "active_scale": final["active_scale"].tolist(),
        "temporal_filter": spec["temporal_filter"],
        "contact": {
            "score": spec["contact"]["score"],
            "fallback_threshold": float(final["fallback_threshold"]),
            "unloaded_warmup_quantile": spec["contact"]["unloaded_warmup_quantile"],
            "minimum_warmup_frames": spec["contact"]["minimum_warmup_frames"],
            "acquire_frames": spec["contact"]["acquire_frames"],
            "clear_frames": spec["contact"]["clear_frames"],
        },
    }
    force_model = {
        "schema_version": SCHEMA_VERSION,
        "model_type": "isotonic_piecewise_linear",
        "input": "filtered_normalized_signed_spatial_range",
        "x_thresholds": final["force_x"].tolist(),
        "y_thresholds_N": final["force_y"].tolist(),
        "operating_range_N": spec["operating_range_N"],
        "out_of_bounds": "unavailable",
        "cross_validated_p95_absolute_error_N": metrics["aggregate"][
            "cross_validated_p95_absolute_error_N"
        ],
    }
    switch_frames = int(spec.get("localization", {}).get("switch_frames", 1))
    selective = spec.get("localization", {}).get("selective_output", {})
    localization_model = {
        "schema_version": SCHEMA_VERSION,
        "model_type": (
            "normalized_active_fraction_argmax_switch_debounce"
            if switch_frames > 1
            else "normalized_active_fraction_argmax"
        ),
        "class_order": list(range(1, 10)),
        "input": "filtered_normalized_active_fraction",
        "claim": "tentative_roi",
        "switch_frames": switch_frames,
        "selective_output_enabled": bool(selective.get("enabled", False)),
        "confidence_margin_threshold": float(
            final["localization_confidence_threshold"]
        ),
        "minimum_training_coverage": float(
            selective.get("minimum_training_coverage", 1.0)
        ),
    }
    blocking_reasons = [
        "Candidate selection was post-hoc after inspecting the existing folds; no untouched confirmation data remain.",
        "The authoritative frozen all-ROI common safe-range procedure failed.",
        "Known-force physical, latency/soak, and representative-user validation were not performed.",
    ]
    if not metrics["aggregate"]["force_resolution_established"]:
        blocking_reasons.append(
            "The force estimator did not satisfy the combined weighted-median improvement and response-resolution gate."
        )
    release = {
        "schema_version": SCHEMA_VERSION,
        "status": "experimental",
        "model_eligible_under_authoritative_plan": False,
        "validated_claim_allowed": False,
        "posthoc_outer_fold_reuse": True,
        "physical_validation_complete": False,
        "usability_validation_complete": False,
        "original_common_safe_range_gate_pass": False,
        "numerical_recovery_checks": {
            "max_no_contact_frame_fpr_at_most_0_05": metrics["aggregate"]["max_no_contact_frame_fpr"] <= 0.05,
            "min_contact_recall_at_least_0_90": metrics["aggregate"]["min_contact_recall"] >= 0.90,
            "conditional_mae_at_most_0_75N": metrics["aggregate"]["mean_conditional_force_mae_N"] <= 0.75,
            "displayed_mae_at_most_0_75N": metrics["aggregate"]["mean_displayed_force_mae_N"] <= 0.75,
            "force_rmse_at_most_1N": metrics["aggregate"]["mean_force_rmse_N"] <= 1.0,
            "absolute_bias_at_most_0_20N": metrics["aggregate"]["absolute_force_bias_N"] <= 0.20,
            "localization_macro_f1_at_least_0_80": metrics["aggregate"]["forced_localization_macro_f1"] >= 0.80,
            "force_resolution_established": metrics["aggregate"]["force_resolution_established"],
        },
        "blocking_reasons": blocking_reasons,
    }
    _write_json(directory / "preprocessing.json", preprocessing)
    _write_json(directory / "force_model.json", force_model)
    _write_json(directory / "localization_model.json", localization_model)
    _write_json(directory / "metrics.json", metrics)
    _write_json(directory / "release_decision.json", release)
    demo = _write_demo(directory / "replay_demo.jsonl", frames, final)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": str(
            spec.get("bundle_id", "live-sensor-experimental-manual-recovery-v1")
        ),
        "status": "experimental",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": provenance,
        "spec_hash": _canonical_hash(spec),
        "runtime": "reviewed_numpy_json",
        "manual_only": True,
        "operating_range_N": spec["operating_range_N"],
        "replay_demo": demo,
        "files": {},
    }
    for path in sorted(directory.iterdir()):
        if path.name in {"manifest.json", "SHA256SUMS"}:
            continue
        manifest["files"][path.name] = {
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    _write_json(directory / "manifest.json", manifest)
    hashes = []
    for path in sorted(directory.iterdir()):
        if path.name == "SHA256SUMS":
            continue
        hashes.append(f"{sha256_file(path)}  {path.name}")
    (directory / "SHA256SUMS").write_text("\n".join(hashes) + "\n", encoding="utf-8")


def _publish(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        backup = destination.with_name(destination.name + ".previous")
        if backup.exists():
            raise FileExistsError(f"refusing to replace bundle while backup exists: {backup}")
        os.replace(destination, backup)
        try:
            os.replace(source, destination)
        except Exception:
            os.replace(backup, destination)
            raise
        shutil.rmtree(backup)
    else:
        os.replace(source, destination)


def build(bundle_path: Path, spec_path: Path = SPEC_PATH) -> tuple[Path, Path]:
    study = _load_json(CONFIG_PATH)
    spec = _load_json(spec_path)
    manual_path = Path(study["sources"]["manual"]["archive_path"])
    expected_hash = str(study["sources"]["manual"]["expected_sha256"])
    before_hash = sha256_file(manual_path)
    if before_hash != expected_hash:
        raise ValueError("manual archive hash does not match the frozen allowlist")
    frames, _split, feature_report, split_report, lag_by_fold = _load_inputs()
    fold_metrics: list[dict[str, Any]] = []
    prediction_frames: list[pd.DataFrame] = []
    for fold in range(1, 7):
        metrics, predictions = _evaluate_fold(frames, fold, lag_by_fold[fold], spec)
        fold_metrics.append(metrics)
        prediction_frames.append(predictions)
    predictions = pd.concat(prediction_frames, ignore_index=True)
    aggregate = _aggregate(fold_metrics, predictions, spec)
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "status": "posthoc_experimental",
        "disclosure": spec["disclosure"],
        "aggregate": aggregate,
        "folds": fold_metrics,
    }
    final_lag_ms = int(np.median(list(lag_by_fold.values())))
    final = _fit_final_model(frames, spec, final_lag_ms)
    provenance = {
        "dataset_policy": "manual_only",
        "manual_archive_sha256": before_hash,
        "source_manifest_hash": feature_report["source_manifest_hash"],
        "split_hash": split_report["split_hash"],
        "feature_spec_hash": feature_report["feature_spec_hash"],
        "final_lag_ms": final_lag_ms,
    }
    run_hash = hashlib.sha256(
        json.dumps({"spec": _canonical_hash(spec), **provenance}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    run_id = f"experimental-recovery-{run_hash}"
    run_directory = RUN_ROOT / run_id
    if run_directory.exists():
        raise FileExistsError(f"run already exists: {run_directory}")
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=run_id + "-", dir=RUN_ROOT) as temporary:
        temp_run = Path(temporary)
        predictions.to_parquet(temp_run / "outer_predictions.parquet", index=False)
        predictions.to_csv(temp_run / "outer_predictions.csv", index=False)
        _write_json(temp_run / "metrics.json", metrics)
        _write_json(
            temp_run / "run_manifest.json",
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "status": "posthoc_experimental",
                "source": provenance,
                "spec_hash": _canonical_hash(spec),
                "manual_archive_hash_before": before_hash,
                "manual_archive_hash_after": sha256_file(manual_path),
            },
        )
        os.replace(temp_run, run_directory)
    if sha256_file(manual_path) != before_hash:
        raise RuntimeError("manual archive changed during model construction")
    bundle_parent = bundle_path.parent
    bundle_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=bundle_path.name + "-", dir=bundle_parent) as temporary:
        temp_bundle = Path(temporary) / "bundle"
        _write_bundle(temp_bundle, final, metrics, provenance, spec, frames)
        _publish(temp_bundle, bundle_path)
    return run_directory, bundle_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run, bundle = build(args.bundle.resolve(), args.spec.resolve())
    print(json.dumps({"run": str(run), "bundle": str(bundle)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
