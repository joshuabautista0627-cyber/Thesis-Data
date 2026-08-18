"""Leakage-aware benchmark for the experimental hybrid force/event model.

This is a model-selection utility, not a release builder.  It keeps the frozen
manual-only outer TEST groups intact, performs tuning using only the remaining
TEST groups, and writes a deterministic JSON comparison for review.  The
existing outer folds have already been inspected, so every result produced by
this module remains post-hoc recovery evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import ElasticNet, Ridge
from sklearn.metrics import mean_absolute_error
from sklearn.preprocessing import StandardScaler

from scripts.build_experimental_manual_recovery import (
    ACTIVE_COLUMNS,
    SIGNED_COLUMNS,
    _load_inputs,
    _normalizer,
    _normalized_smoothed,
    _outer_frames,
    _session_balanced_weights,
    _training_frames,
    _weighted_median,
    _write_json,
)
from scripts.fit_manual_only_preprocessing import pair_lagged_force
from scripts.live_sensor_common import PROJECT_ROOT


DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "model_runs"
    / "hybrid_candidate_benchmark.json"
)
FORCE_MIN_N = 1.7
FORCE_MAX_N = 3.0
SCALE_FLOOR = 1e-6
SEED = 1729


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    family: str
    window_frames: int
    parameters: Mapping[str, Any]


def _feature_names() -> list[str]:
    names = [f"signed_roi{roi}" for roi in range(1, 10)]
    names += [f"active_roi{roi}" for roi in range(1, 10)]
    names += [
        "signed_range",
        "signed_max",
        "signed_min",
        "signed_mean",
        "signed_std",
        "positive_active_sum",
        "active_max",
        "active_mean",
        "active_std",
        "active_top_two_margin",
        "log1p_signed_range",
        "signed_range_squared",
    ]
    return names


def force_features(signed: np.ndarray, active: np.ndarray) -> np.ndarray:
    """Return deterministic features available to the NumPy live runtime."""

    signed_values = np.asarray(signed, dtype=float)
    active_values = np.asarray(active, dtype=float)
    if signed_values.shape != active_values.shape or signed_values.ndim != 2:
        raise ValueError("signed and active arrays must be equal two-dimensional arrays")
    if signed_values.shape[1] != 9:
        raise ValueError("nine ROI values are required")
    score = np.ptp(signed_values, axis=1)
    ordered_active = np.sort(active_values, axis=1)
    aggregates = np.column_stack(
        [
            score,
            np.max(signed_values, axis=1),
            np.min(signed_values, axis=1),
            np.mean(signed_values, axis=1),
            np.std(signed_values, axis=1),
            np.sum(np.maximum(active_values, 0.0), axis=1),
            np.max(active_values, axis=1),
            np.mean(active_values, axis=1),
            np.std(active_values, axis=1),
            ordered_active[:, -1] - ordered_active[:, -2],
            np.log1p(np.maximum(score, 0.0)),
            np.square(score),
        ]
    )
    result = np.column_stack([signed_values, active_values, aggregates])
    if result.shape[1] != len(_feature_names()) or not np.isfinite(result).all():
        raise ValueError("force feature engineering produced an invalid matrix")
    return result


class _StandardizedLinear:
    def __init__(self, estimator: Ridge | ElasticNet) -> None:
        self.estimator = estimator
        self.scaler = StandardScaler()

    def fit(
        self, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray
    ) -> "_StandardizedLinear":
        self.scaler.fit(x, sample_weight=sample_weight)
        transformed = self.scaler.transform(x)
        self.estimator.fit(transformed, y, sample_weight=sample_weight)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.estimator.predict(self.scaler.transform(x)), dtype=float)


class _ScoreIsotonic:
    def __init__(self) -> None:
        self.model = IsotonicRegression(
            y_min=FORCE_MIN_N, y_max=FORCE_MAX_N, out_of_bounds="clip"
        )

    def fit(
        self, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray
    ) -> "_ScoreIsotonic":
        self.model.fit(x[:, 18], y, sample_weight=sample_weight)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self.model.predict(x[:, 18]), dtype=float)


def candidates() -> list[Candidate]:
    values: list[Candidate] = []
    for window in (5, 9, 13):
        values.append(Candidate(f"isotonic_w{window}", "isotonic", window, {}))
        for alpha in (1.0, 100.0, 1000.0):
            values.append(
                Candidate(
                    f"ridge_w{window}_a{alpha:g}",
                    "ridge",
                    window,
                    {"alpha": alpha},
                )
            )
        for alpha, l1_ratio in ((0.01, 0.1),):
            values.append(
                Candidate(
                    f"elastic_w{window}_a{alpha:g}_l{l1_ratio:g}",
                    "elastic_net",
                    window,
                    {"alpha": alpha, "l1_ratio": l1_ratio},
                )
            )
        for leaf in (60,):
            values.append(
                Candidate(
                    f"extra_trees_w{window}_leaf{leaf}",
                    "extra_trees",
                    window,
                    {"min_samples_leaf": leaf},
                )
            )
        for leaves, l2 in ((7, 10.0),):
            values.append(
                Candidate(
                    f"hist_gradient_w{window}_leaves{leaves}_l2{l2:g}",
                    "hist_gradient",
                    window,
                    {"max_leaf_nodes": leaves, "l2_regularization": l2},
                )
            )
    return values


def make_estimator(candidate: Candidate) -> Any:
    if candidate.family == "isotonic":
        return _ScoreIsotonic()
    if candidate.family == "ridge":
        return _StandardizedLinear(Ridge(alpha=float(candidate.parameters["alpha"])))
    if candidate.family == "elastic_net":
        return _StandardizedLinear(
            ElasticNet(
                alpha=float(candidate.parameters["alpha"]),
                l1_ratio=float(candidate.parameters["l1_ratio"]),
                max_iter=20_000,
                tol=1e-6,
                random_state=SEED,
            )
        )
    if candidate.family == "extra_trees":
        return ExtraTreesRegressor(
            n_estimators=60,
            min_samples_leaf=int(candidate.parameters["min_samples_leaf"]),
            max_features=0.75,
            n_jobs=-1,
            random_state=SEED,
        )
    if candidate.family == "hist_gradient":
        return HistGradientBoostingRegressor(
            loss="absolute_error",
            learning_rate=0.05,
            max_iter=80,
            max_leaf_nodes=int(candidate.parameters["max_leaf_nodes"]),
            min_samples_leaf=30,
            l2_regularization=float(candidate.parameters["l2_regularization"]),
            early_stopping=False,
            random_state=SEED,
        )
    raise ValueError(f"unsupported candidate family: {candidate.family}")


def _clip(prediction: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(prediction, dtype=float), FORCE_MIN_N, FORCE_MAX_N)


def _session_balanced_mae(
    truth: np.ndarray, prediction: np.ndarray, session_ids: Iterable[object]
) -> float:
    table = pd.DataFrame(
        {
            "session_id": np.asarray(list(session_ids)).astype(str),
            "absolute_error": np.abs(np.asarray(prediction) - np.asarray(truth)),
        }
    )
    return float(table.groupby("session_id")["absolute_error"].mean().mean())


def _session_balanced_spearman(
    truth: np.ndarray, prediction: np.ndarray, session_ids: Iterable[object]
) -> float:
    table = pd.DataFrame(
        {
            "session_id": np.asarray(list(session_ids)).astype(str),
            "truth": np.asarray(truth, dtype=float),
            "prediction": np.asarray(prediction, dtype=float),
        }
    )
    correlations: list[float] = []
    for _, group in table.groupby("session_id", sort=False):
        value = float(spearmanr(group["truth"], group["prediction"]).statistic)
        correlations.append(value if math.isfinite(value) else 0.0)
    return float(np.mean(correlations))


def _normalised_arrays(
    frames: pd.DataFrame,
    training_no_contact: pd.DataFrame,
    window_frames: int,
) -> tuple[np.ndarray, np.ndarray]:
    signed_center, signed_scale = _normalizer(
        training_no_contact, SIGNED_COLUMNS, SCALE_FLOOR
    )
    active_center, active_scale = _normalizer(
        training_no_contact, ACTIVE_COLUMNS, SCALE_FLOOR
    )
    signed = _normalized_smoothed(
        frames, SIGNED_COLUMNS, signed_center, signed_scale, window_frames
    )
    active = _normalized_smoothed(
        frames, ACTIVE_COLUMNS, active_center, active_scale, window_frames
    )
    return signed, active


def _eligible(
    frames: pd.DataFrame, lag_ms: int
) -> tuple[np.ndarray, np.ndarray]:
    force = pair_lagged_force(frames, lag_ms).to_numpy(float)
    mask = (
        frames["scientific_role"].eq("model_primary").to_numpy()
        & np.isfinite(force)
        & (force >= FORCE_MIN_N)
        & (force <= FORCE_MAX_N)
    )
    return force, mask


def _fit_predict(
    candidate: Candidate,
    x_fit: np.ndarray,
    y_fit: np.ndarray,
    fit_sessions: np.ndarray,
    x_validation: np.ndarray,
) -> np.ndarray:
    weights = _session_balanced_weights(fit_sessions, y_fit)
    estimator = make_estimator(candidate)
    estimator.fit(x_fit, y_fit, sample_weight=weights)
    return _clip(estimator.predict(x_validation))


def benchmark() -> dict[str, Any]:
    frames, _split, feature_report, split_report, lag_by_fold = _load_inputs()
    choices = candidates()
    outer_rows: list[dict[str, Any]] = []
    prediction_rows: list[pd.DataFrame] = []

    for outer_fold in range(1, 7):
        training = _training_frames(frames, outer_fold).sort_values(
            ["session_id", "video_frame_index"]
        ).reset_index(drop=True)
        outer = _outer_frames(frames, outer_fold).sort_values(
            ["session_id", "video_frame_index"]
        ).reset_index(drop=True)
        lag_ms = int(lag_by_fold[outer_fold])
        training_force, training_mask = _eligible(training, lag_ms)
        outer_force, outer_mask = _eligible(outer, lag_ms)
        training_groups = training.loc[training_mask, "test_group"].astype(str).to_numpy()
        training_sessions = training.loc[training_mask, "session_id"].astype(str).to_numpy()
        outer_sessions = outer.loc[outer_mask, "session_id"].astype(str).to_numpy()
        train_no_contact = training[training["scientific_role"] == "no_contact"]
        by_window: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for window in sorted({candidate.window_frames for candidate in choices}):
            train_signed, train_active = _normalised_arrays(
                training, train_no_contact, window
            )
            outer_signed, outer_active = _normalised_arrays(
                outer, train_no_contact, window
            )
            by_window[window] = (
                force_features(train_signed, train_active)[training_mask],
                force_features(outer_signed, outer_active)[outer_mask],
            )

        tuning: list[dict[str, Any]] = []
        for candidate in choices:
            x_training, _ = by_window[candidate.window_frames]
            group_scores: list[float] = []
            for validation_group in sorted(np.unique(training_groups)):
                fit = training_groups != validation_group
                validation = ~fit
                prediction = _fit_predict(
                    candidate,
                    x_training[fit],
                    training_force[training_mask][fit],
                    training_sessions[fit],
                    x_training[validation],
                )
                group_scores.append(
                    _session_balanced_mae(
                        training_force[training_mask][validation],
                        prediction,
                        training_sessions[validation],
                    )
                )
            tuning.append(
                {
                    "candidate": candidate,
                    "inner_mae_N": float(np.mean(group_scores)),
                    "inner_group_mae_N": group_scores,
                }
            )
        selected_row = min(
            tuning,
            key=lambda row: (
                float(row["inner_mae_N"]),
                row["candidate"].family not in {"isotonic", "ridge", "elastic_net"},
                row["candidate"].name,
            ),
        )
        selected: Candidate = selected_row["candidate"]
        x_training, x_outer = by_window[selected.window_frames]
        outer_prediction = _fit_predict(
            selected,
            x_training,
            training_force[training_mask],
            training_sessions,
            x_outer,
        )
        weights = _session_balanced_weights(
            training_sessions, training_force[training_mask]
        )
        constant = _weighted_median(training_force[training_mask], weights)
        truth = outer_force[outer_mask]
        mae = _session_balanced_mae(truth, outer_prediction, outer_sessions)
        constant_mae = _session_balanced_mae(
            truth, np.full(len(truth), constant), outer_sessions
        )
        outer_rows.append(
            {
                "outer_fold": outer_fold,
                "lag_ms": lag_ms,
                "selected_candidate": selected.name,
                "selected_family": selected.family,
                "selected_window_frames": selected.window_frames,
                "selected_parameters": dict(selected.parameters),
                "mean_inner_group_mae_N": float(selected_row["inner_mae_N"]),
                "conditional_force_mae_N": mae,
                "constant_force_mae_N": constant_mae,
                "relative_mae_gain_over_constant": (constant_mae - mae)
                / max(constant_mae, 1e-12),
                "force_spearman_rho": _session_balanced_spearman(
                    truth, outer_prediction, outer_sessions
                ),
                "prediction_sd_N": float(np.std(outer_prediction)),
                "truth_sd_N": float(np.std(truth)),
            }
        )
        prediction_rows.append(
            pd.DataFrame(
                {
                    "outer_fold": outer_fold,
                    "session_id": outer_sessions,
                    "truth_N": truth,
                    "prediction_N": outer_prediction,
                    "constant_N": constant,
                }
            )
        )

    predictions = pd.concat(prediction_rows, ignore_index=True)
    absolute_error = np.abs(predictions["prediction_N"] - predictions["truth_N"])
    family_counts = pd.Series(
        [row["selected_family"] for row in outer_rows]
    ).value_counts()
    aggregate = {
        "fold_count": 6,
        "frame_count": int(len(predictions)),
        "session_count": int(predictions["session_id"].nunique()),
        "mean_conditional_force_mae_N": float(
            np.mean([row["conditional_force_mae_N"] for row in outer_rows])
        ),
        "mean_constant_force_mae_N": float(
            np.mean([row["constant_force_mae_N"] for row in outer_rows])
        ),
        "relative_mae_gain_over_constant": float(
            (
                np.mean([row["constant_force_mae_N"] for row in outer_rows])
                - np.mean([row["conditional_force_mae_N"] for row in outer_rows])
            )
            / max(
                np.mean([row["constant_force_mae_N"] for row in outer_rows]),
                1e-12,
            )
        ),
        "mean_session_spearman_rho": float(
            np.mean([row["force_spearman_rho"] for row in outer_rows])
        ),
        "cross_validated_p95_absolute_error_N": float(
            np.quantile(absolute_error, 0.95)
        ),
        "selected_family_counts": {
            str(key): int(value) for key, value in family_counts.items()
        },
    }
    return {
        "schema_version": "1.0.0",
        "status": "posthoc_experimental_model_selection",
        "disclosure": (
            "The six manual-only outer TEST groups were already inspected before this "
            "benchmark. Hyperparameters are selected within each outer training partition, "
            "but the aggregate is recovery evidence and not untouched validation."
        ),
        "data_quality": {
            "source_manifest_hash": feature_report["source_manifest_hash"],
            "split_hash": split_report["split_hash"],
            "operating_range_N": [FORCE_MIN_N, FORCE_MAX_N],
            "feature_names": _feature_names(),
            "target_is_never_an_input": True,
            "session_grouped": True,
            "outer_group_field": "test_group",
        },
        "aggregate": aggregate,
        "folds": outer_rows,
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
    print(json.dumps(report["aggregate"], indent=2, sort_keys=True))
    print(str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
