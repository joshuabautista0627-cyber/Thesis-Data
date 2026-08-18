"""Compare grouped event-localization models for the experimental hybrid sensor.

All results are post-hoc because the six manual-only TEST groups were already
inspected.  Every reported outer prediction is nevertheless produced by a
model that excludes that complete TEST group and its sessions.
"""

from __future__ import annotations

import argparse
from collections import deque
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score

from scripts.build_experimental_event_signal import (
    _select_confidence_threshold,
    _session_weights,
)
from scripts.build_experimental_manual_recovery import (
    ACTIVE_COLUMNS,
    SIGNED_COLUMNS,
    _calibration_and_test_indices,
    _fallback_threshold,
    _load_inputs,
    _normalizer,
    _normalized_smoothed,
    _outer_frames,
    _training_frames,
    _write_json,
)
from scripts.live_sensor_common import PROJECT_ROOT


DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "model_runs"
    / "event_localization_candidate_benchmark.json"
)
WINDOW_FRAMES = 9
SCALE_FLOOR = 1e-6
ACQUIRE_FRAMES = 2
CLEAR_FRAMES = 2
SEED = 1729


def _event_feature_rows(
    frames: pd.DataFrame,
    scores: np.ndarray,
    active: np.ndarray,
    threshold: float,
) -> pd.DataFrame:
    events: list[dict[str, Any]] = []
    for session_id, indices in frames.groupby("session_id", sort=False).indices.items():
        ordered = np.asarray(indices, dtype=int)
        pretrigger: deque[tuple[int, float, np.ndarray, bool]] = deque(
            maxlen=ACQUIRE_FRAMES
        )
        contact_active = False
        positive_count = 0
        negative_count = 0
        accumulator = np.zeros(9, dtype=float)
        peak_ratio = math.nan
        included_frames = 0

        def add_sample(score: float, values: np.ndarray) -> None:
            nonlocal accumulator, peak_ratio, included_frames
            accumulator += np.maximum(values, 0.0)
            ratio = float(score / threshold)
            peak_ratio = ratio if not math.isfinite(peak_ratio) else max(peak_ratio, ratio)
            included_frames += 1

        def finish() -> None:
            nonlocal accumulator, peak_ratio, included_frames
            total = float(np.sum(accumulator))
            if total > 0.0 and included_frames > 0:
                first = frames.iloc[ordered[0]]
                row: dict[str, Any] = {
                    "session_id": str(session_id),
                    "test_group": str(first["test_group"]),
                    "target_roi": int(first["target_roi"]),
                    "predicted_roi": int(np.argmax(accumulator)) + 1,
                    "peak_signal_ratio": float(peak_ratio),
                    "included_frames": int(included_frames),
                    "activity_total": total,
                }
                composition = accumulator / total
                ordered_values = np.sort(composition)
                row["baseline_confidence"] = float(
                    ordered_values[-1] - ordered_values[-2]
                )
                for roi in range(1, 10):
                    row[f"activity_share_roi{roi}"] = float(composition[roi - 1])
                events.append(row)
            accumulator = np.zeros(9, dtype=float)
            peak_ratio = math.nan
            included_frames = 0

        for index in ordered:
            score = float(scores[index])
            values = np.asarray(active[index], dtype=float)
            raw_contact = score >= threshold
            pretrigger.append((index, score, values, raw_contact))
            was_active = contact_active
            if raw_contact:
                positive_count += 1
                negative_count = 0
                if not contact_active and positive_count >= ACQUIRE_FRAMES:
                    contact_active = True
            else:
                negative_count += 1
                positive_count = 0
                if contact_active and negative_count >= CLEAR_FRAMES:
                    contact_active = False
            entered = not was_active and contact_active
            exited = was_active and not contact_active
            if entered:
                trigger: list[tuple[int, float, np.ndarray, bool]] = []
                for sample in reversed(pretrigger):
                    if not sample[3]:
                        break
                    trigger.append(sample)
                for _, trigger_score, trigger_active, _ in reversed(trigger):
                    add_sample(trigger_score, trigger_active)
            elif contact_active and raw_contact:
                add_sample(score, values)
            if exited:
                finish()
        if contact_active:
            finish()
    return pd.DataFrame(events)


def _features(events: pd.DataFrame) -> np.ndarray:
    shares = events[
        [f"activity_share_roi{roi}" for roi in range(1, 10)]
    ].to_numpy(float)
    extras = np.column_stack(
        [
            np.log1p(events["activity_total"].to_numpy(float)),
            np.log1p(events["included_frames"].to_numpy(float)),
            np.log1p(events["peak_signal_ratio"].to_numpy(float)),
        ]
    )
    return np.column_stack([shares, extras])


def _fit_predict(
    family: str,
    training: pd.DataFrame,
    validation: pd.DataFrame,
    *,
    c_value: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    if family == "argmax":
        prediction = validation["predicted_roi"].astype(int).to_numpy()
        confidence = validation["baseline_confidence"].to_numpy(float)
        return prediction, confidence
    x_training = _features(training)
    x_validation = _features(validation)
    truth = training["target_roi"].astype(int).to_numpy()
    weights = _session_weights(training)
    if family == "logistic":
        model: Any = LogisticRegression(
            C=c_value,
            solver="lbfgs",
            max_iter=5000,
            random_state=SEED,
        )
    elif family == "shrinkage_lda":
        model = LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto")
    elif family == "extra_trees":
        model = ExtraTreesClassifier(
            n_estimators=300,
            min_samples_leaf=2,
            max_features=0.75,
            class_weight="balanced",
            random_state=SEED,
            n_jobs=-1,
        )
    else:
        raise ValueError(f"unsupported family: {family}")
    if family == "shrinkage_lda":
        # scikit-learn's LDA API has no sample_weight parameter.  Session
        # balancing is still applied to every reported validation metric.
        model.fit(x_training, truth)
    else:
        model.fit(x_training, truth, sample_weight=weights)
    probabilities = np.asarray(model.predict_proba(x_validation), dtype=float)
    classes = np.asarray(model.classes_, dtype=int)
    order = np.argsort(probabilities, axis=1)
    prediction = classes[order[:, -1]]
    confidence = probabilities[np.arange(len(probabilities)), order[:, -1]] - probabilities[
        np.arange(len(probabilities)), order[:, -2]
    ]
    return prediction, confidence


def _weighted_metrics(events: pd.DataFrame) -> dict[str, float]:
    weights = _session_weights(events)
    return {
        "accuracy": float(
            accuracy_score(
                events["target_roi"], events["model_prediction"], sample_weight=weights
            )
        ),
        "macro_f1": float(
            f1_score(
                events["target_roi"],
                events["model_prediction"],
                labels=list(range(1, 10)),
                average="macro",
                sample_weight=weights,
                zero_division=0,
            )
        ),
    }


def benchmark() -> dict[str, Any]:
    frames, _split, feature_report, split_report, _lag_by_fold = _load_inputs()
    families = ("argmax", "logistic", "shrinkage_lda", "extra_trees")
    predictions: dict[str, list[pd.DataFrame]] = {family: [] for family in families}
    fold_rows: list[dict[str, Any]] = []

    for fold in range(1, 7):
        training = _training_frames(frames, fold).sort_values(
            ["session_id", "video_frame_index"]
        ).reset_index(drop=True)
        outer = _outer_frames(frames, fold).sort_values(
            ["session_id", "video_frame_index"]
        ).reset_index(drop=True)
        no_contact = training[training["scientific_role"] == "no_contact"]
        signed_center, signed_scale = _normalizer(
            no_contact, SIGNED_COLUMNS, SCALE_FLOOR
        )
        active_center, active_scale = _normalizer(
            no_contact, ACTIVE_COLUMNS, SCALE_FLOOR
        )
        train_signed = _normalized_smoothed(
            training, SIGNED_COLUMNS, signed_center, signed_scale, WINDOW_FRAMES
        )
        train_active = _normalized_smoothed(
            training, ACTIVE_COLUMNS, active_center, active_scale, WINDOW_FRAMES
        )
        outer_signed = _normalized_smoothed(
            outer, SIGNED_COLUMNS, signed_center, signed_scale, WINDOW_FRAMES
        )
        outer_active = _normalized_smoothed(
            outer, ACTIVE_COLUMNS, active_center, active_scale, WINDOW_FRAMES
        )
        training_scores = np.ptp(train_signed, axis=1)
        outer_scores = np.ptp(outer_signed, axis=1)
        train_no_contact_mask = training["scientific_role"].eq("no_contact").to_numpy()
        fallback = _fallback_threshold(
            training_scores[train_no_contact_mask],
            training.loc[train_no_contact_mask].reset_index(drop=True),
            0.95,
        )
        outer_no_contact = outer[outer["scientific_role"] == "no_contact"]
        calibration_index, _ = _calibration_and_test_indices(outer_no_contact, 0.5)
        warmup = float(
            np.quantile(outer_scores[calibration_index], 0.99, method="higher")
        )
        threshold = max(fallback, warmup)
        train_primary = training["scientific_role"].eq("model_primary").to_numpy()
        outer_primary = outer["scientific_role"].eq("model_primary").to_numpy()
        training_events = _event_feature_rows(
            training.loc[train_primary].reset_index(drop=True),
            training_scores[train_primary],
            train_active[train_primary],
            fallback,
        )
        outer_events = _event_feature_rows(
            outer.loc[outer_primary].reset_index(drop=True),
            outer_scores[outer_primary],
            outer_active[outer_primary],
            threshold,
        )
        if training_events["target_roi"].nunique() != 9:
            raise ValueError("all nine ROI classes are required in every training partition")

        logistic_scores: dict[float, list[float]] = {}
        for c_value in (0.1, 1.0, 10.0, 100.0):
            values: list[float] = []
            for group in sorted(training_events["test_group"].unique()):
                fit = training_events[training_events["test_group"] != group]
                validation = training_events[training_events["test_group"] == group]
                prediction, _ = _fit_predict(
                    "logistic", fit, validation, c_value=c_value
                )
                scored = validation.assign(model_prediction=prediction)
                values.append(_weighted_metrics(scored)["macro_f1"])
            logistic_scores[c_value] = values
        selected_c = max(
            logistic_scores,
            key=lambda value: (float(np.mean(logistic_scores[value])), -value),
        )
        fold_result: dict[str, Any] = {
            "outer_fold": fold,
            "training_event_count": int(len(training_events)),
            "outer_event_count": int(len(outer_events)),
            "selected_logistic_C": selected_c,
            "models": {},
        }
        for family in families:
            prediction, confidence = _fit_predict(
                family,
                training_events,
                outer_events,
                c_value=selected_c,
            )
            scored = outer_events.assign(
                outer_fold=fold,
                model_prediction=prediction,
                model_confidence=confidence,
            )
            metrics = _weighted_metrics(scored)
            fold_result["models"][family] = metrics
            predictions[family].append(scored)
        fold_rows.append(fold_result)

    aggregate: dict[str, Any] = {}
    for family, values in predictions.items():
        combined = pd.concat(values, ignore_index=True)
        aggregate[family] = {
            **_weighted_metrics(combined),
            "event_count": int(len(combined)),
            "session_count": int(combined["session_id"].nunique()),
            "minimum_fold_macro_f1": float(
                min(row["models"][family]["macro_f1"] for row in fold_rows)
            ),
        }
    selected_family = max(
        families,
        key=lambda family: (
            aggregate[family]["macro_f1"],
            aggregate[family]["minimum_fold_macro_f1"],
            family == "argmax",
        ),
    )
    return {
        "schema_version": "1.0.0",
        "status": "posthoc_experimental_model_selection",
        "disclosure": (
            "The six existing outer TEST groups were reused post-hoc. Every outer "
            "prediction excludes its complete TEST group, while logistic regularization "
            "is chosen only within the outer training partition."
        ),
        "data_quality": {
            "source_manifest_hash": feature_report["source_manifest_hash"],
            "split_hash": split_report["split_hash"],
            "session_grouped": True,
            "frame_labels_are_aggregated_before_event_classification": True,
        },
        "selected_family": selected_family,
        "aggregate": aggregate,
        "folds": fold_rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = benchmark()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(args.output, report)
    print(json.dumps({"selected_family": report["selected_family"], **report["aggregate"]}, indent=2))
    print(str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
