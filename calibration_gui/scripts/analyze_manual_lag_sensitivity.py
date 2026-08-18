"""Diagnose bidirectional camera/reference-force lag in the manual-only archive.

Positive lag pairs optical features at time t with reference force at t-L.
All model selection remains inside complete-session training folds.  Outer
groups are used only for the final zero-lag versus selected-lag diagnostic.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import tempfile
from typing import Any

import numpy as np
import pandas as pd

from core.live_sensor_contracts import canonical_json_hash, load_json_object
from scripts.build_experimental_manual_recovery import (
    _evaluate_fold as evaluate_v2_fold,
    _load_inputs as load_v2_inputs,
)
from scripts.fit_manual_only_preprocessing import (
    _fit_floors,
    _load_inputs,
    _nnls_mae,
    _select_lag,
    _training_frames,
    pair_lagged_force,
)
from scripts.live_sensor_common import (
    PROJECT_ROOT,
    atomic_write_json,
    load_study_config,
    output_file_hashes,
    publish_run_directory,
    sha256_file,
)


CONFIG_PATH = PROJECT_ROOT / "config" / "live_sensor_manual_only.json"
PREPROCESSING_SPEC_PATH = PROJECT_ROOT / "config" / "manual_only_preprocessing.json"
V2_SPEC_PATH = PROJECT_ROOT / "config" / "manual_only_experimental_recovery.json"
OUTPUT_ROOT = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "diagnostics"
    / "lag_sensitivity"
)


def _session_fps(frames: pd.DataFrame) -> pd.Series:
    values: dict[str, float] = {}
    primary = frames[frames["scientific_role"] == "model_primary"]
    for session_id, group in primary.groupby("session_id", sort=True):
        times = np.sort(
            pd.to_numeric(
                group["capture_monotonic_relative_s"], errors="coerce"
            ).dropna().to_numpy(float)
        )
        differences = np.diff(times)
        differences = differences[np.isfinite(differences) & (differences > 0)]
        if len(differences):
            values[str(session_id)] = 1.0 / float(np.median(differences))
    return pd.Series(values, dtype=float)


def _relative_gain(zero: float, shifted: float) -> float:
    return (zero - shifted) / zero if zero > 0 else math.nan


def run(minimum_ms: int, maximum_ms: int, step_ms: int) -> Path:
    if minimum_ms >= 0 or maximum_ms <= 0:
        raise ValueError("bidirectional lag search must include negative and positive values")
    if step_ms <= 0 or minimum_ms % step_ms or maximum_ms % step_ms:
        raise ValueError("lag bounds must be exact multiples of the positive step")
    lags = list(range(minimum_ms, maximum_ms + 1, step_ms))
    if 0 not in lags:
        raise ValueError("lag search must include zero")

    config, config_hash, _ = load_study_config(CONFIG_PATH)
    preprocessing_spec = load_json_object(PREPROCESSING_SPEC_PATH)
    frames, feature_dir, feature_report, split_dir, split_report = _load_inputs(
        config, config_hash
    )
    lagged = {lag: pair_lagged_force(frames, lag) for lag in lags}
    group_to_fold = {
        group: index + 1
        for index, group in enumerate(config["force_study"]["outer_groups"])
    }
    lag_spec = {
        **preprocessing_spec["lag"],
        "minimum_ms": minimum_ms,
        "maximum_ms": maximum_ms,
        "step_ms": step_ms,
    }

    lag_rows: list[dict[str, Any]] = []
    outer_rows: list[dict[str, Any]] = []
    selected_by_fold: dict[int, int] = {}
    for outer_fold in [1, 2, 3, 4, 5, 6, None]:
        scope = "development" if outer_fold is None else str(outer_fold)
        training = _training_frames(frames, outer_fold)
        selected, records = _select_lag(
            training, lagged, lag_spec, group_to_fold
        )
        if outer_fold is not None:
            selected_by_fold[int(outer_fold)] = selected
        means: dict[int, float] = {}
        for row in records:
            mean_value = row.get("mean_inner_mae_N")
            if mean_value is not None:
                means[int(row["lag_ms"])] = float(mean_value)
        for lag in lags:
            lag_rows.append(
                {
                    "scope": scope,
                    "lag_ms": lag,
                    "mean_inner_session_balanced_mae_N": means.get(lag),
                    "selected": lag == selected,
                }
            )
        if outer_fold is None:
            continue
        outer = frames[
            (frames["scientific_role"] == "model_primary")
            & (frames["outer_fold"] == outer_fold)
        ]
        fit = training[training["scientific_role"] == "model_primary"]
        floors = _fit_floors(training)
        scores: dict[int, float] = {}
        for lag in lags:
            score = _nnls_mae(
                fit,
                outer,
                lagged[lag],
                lagged[lag],
                floors,
                float(lag_spec["fit_force_min_N"]),
                float(lag_spec["fit_force_max_N"]),
            )
            if score is not None:
                scores[lag] = float(score)
        if 0 not in scores or selected not in scores:
            raise ValueError(f"outer fold {outer_fold} produced incomplete lag scores")
        oracle_lag = min(scores, key=lambda lag: (scores[lag], abs(lag), lag))
        outer_rows.append(
            {
                "outer_fold": outer_fold,
                "held_out_group": config["force_study"]["outer_groups"][outer_fold - 1],
                "held_out_sessions": int(outer["session_id"].nunique()),
                "selected_lag_ms": selected,
                "zero_lag_mae_N": scores[0],
                "selected_lag_mae_N": scores[selected],
                "relative_mae_reduction": _relative_gain(scores[0], scores[selected]),
                "held_out_oracle_lag_ms_descriptive_only": oracle_lag,
                "held_out_oracle_mae_N_descriptive_only": scores[oracle_lag],
            }
        )

    v2_frames, _, v2_feature_report, v2_split_report, existing_lags = load_v2_inputs()
    if feature_report["source_manifest_hash"] != v2_feature_report["source_manifest_hash"]:
        raise ValueError("preprocessing and v2 source manifests differ")
    v2_spec = load_json_object(V2_SPEC_PATH)
    v2_rows: list[dict[str, Any]] = []
    for fold in range(1, 7):
        for condition, lag in (
            ("zero_lag", 0),
            ("existing_positive_only_selected", existing_lags[fold]),
            ("bidirectional_inner_selected", selected_by_fold[fold]),
        ):
            metrics, _ = evaluate_v2_fold(v2_frames, fold, lag, v2_spec)
            v2_rows.append(
                {
                    "outer_fold": fold,
                    "condition": condition,
                    "lag_ms": lag,
                    "conditional_force_mae_N": metrics["conditional_force_mae_N"],
                    "displayed_force_mae_N": metrics["displayed_force_mae_N"],
                    "contact_recall": metrics["contact_recall"],
                    "forced_localization_macro_f1": metrics[
                        "forced_localization_macro_f1"
                    ],
                    "force_spearman_rho": metrics["force_spearman_rho"],
                    "predicted_to_true_sd_ratio": metrics[
                        "predicted_to_true_sd_ratio"
                    ],
                }
            )
    v2_table = pd.DataFrame(v2_rows)
    v2_aggregate = (
        v2_table.groupby("condition")
        .agg(
            mean_conditional_force_mae_N=("conditional_force_mae_N", "mean"),
            mean_displayed_force_mae_N=("displayed_force_mae_N", "mean"),
            mean_contact_recall=("contact_recall", "mean"),
            mean_forced_localization_macro_f1=(
                "forced_localization_macro_f1",
                "mean",
            ),
            mean_force_spearman_rho=("force_spearman_rho", "mean"),
            mean_predicted_to_true_sd_ratio=(
                "predicted_to_true_sd_ratio",
                "mean",
            ),
        )
        .reset_index()
    )
    aggregate_lookup = v2_aggregate.set_index("condition")
    zero_force_mae = float(
        aggregate_lookup.loc["zero_lag", "mean_conditional_force_mae_N"]
    )
    shifted_force_mae = float(
        aggregate_lookup.loc[
            "bidirectional_inner_selected", "mean_conditional_force_mae_N"
        ]
    )
    existing_force_mae = float(
        aggregate_lookup.loc[
            "existing_positive_only_selected", "mean_conditional_force_mae_N"
        ]
    )

    fps = _session_fps(frames)
    development_selected = int(
        next(
            row["lag_ms"]
            for row in lag_rows
            if row["scope"] == "development" and row["selected"]
        )
    )
    identity = {
        "source_manifest_hash": feature_report["source_manifest_hash"],
        "split_hash": split_report["split_hash"],
        "minimum_ms": minimum_ms,
        "maximum_ms": maximum_ms,
        "step_ms": step_ms,
        "script_sha256": sha256_file(Path(__file__)),
    }
    run_id = f"lag-sensitivity-{canonical_json_hash(identity)[:16]}"
    final = OUTPUT_ROOT / run_id
    if final.exists():
        return final
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".tmp", dir=OUTPUT_ROOT))
    try:
        lag_table = pd.DataFrame(lag_rows)
        outer_table = pd.DataFrame(outer_rows)
        lag_table.to_csv(temporary / "bidirectional_lag_curve.csv", index=False)
        outer_table.to_csv(temporary / "outer_validation_nnls.csv", index=False)
        v2_table.to_csv(temporary / "v2_zero_vs_selected_folds.csv", index=False)
        v2_aggregate.to_csv(temporary / "v2_zero_vs_selected_aggregate.csv", index=False)
        summary = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "created_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "dataset_policy": "manual_only",
            "source_manifest_hash": feature_report["source_manifest_hash"],
            "split_hash": split_report["split_hash"],
            "feature_store": feature_dir.relative_to(PROJECT_ROOT).as_posix(),
            "split_artifact": split_dir.relative_to(PROJECT_ROOT).as_posix(),
            "lag_convention": "positive lag pairs optical time t with reference force at t-L",
            "search": {
                "minimum_ms": minimum_ms,
                "maximum_ms": maximum_ms,
                "step_ms": step_ms,
                "candidate_count": len(lags),
            },
            "capture_rate": {
                "session_count": int(len(fps)),
                "median_session_fps": float(fps.median()),
                "minimum_session_fps": float(fps.min()),
                "maximum_session_fps": float(fps.max()),
            },
            "selected_lag_ms_by_outer_training_fold": {
                str(key): value for key, value in selected_by_fold.items()
            },
            "existing_v2_lag_ms_by_fold": {
                str(key): value for key, value in existing_lags.items()
            },
            "bidirectional_matches_existing_v2": selected_by_fold == existing_lags,
            "development_selected_lag_ms": development_selected,
            "development_selected_lag_frames_at_median_fps": (
                development_selected * float(fps.median()) / 1000.0
            ),
            "outer_nnls": {
                "mean_zero_lag_mae_N": float(outer_table["zero_lag_mae_N"].mean()),
                "mean_selected_lag_mae_N": float(
                    outer_table["selected_lag_mae_N"].mean()
                ),
                "relative_mae_reduction": _relative_gain(
                    float(outer_table["zero_lag_mae_N"].mean()),
                    float(outer_table["selected_lag_mae_N"].mean()),
                ),
                "folds_improved": int(
                    (outer_table["selected_lag_mae_N"] < outer_table["zero_lag_mae_N"]).sum()
                ),
                "fold_count": 6,
            },
            "v2_isotonic_outer_validation": {
                "zero_lag_mean_conditional_force_mae_N": zero_force_mae,
                "selected_lag_mean_conditional_force_mae_N": shifted_force_mae,
                "existing_positive_lag_mean_conditional_force_mae_N": existing_force_mae,
                "relative_force_mae_reduction": _relative_gain(
                    zero_force_mae, shifted_force_mae
                ),
                "bidirectional_absolute_mae_gain_over_existing_N": (
                    existing_force_mae - shifted_force_mae
                ),
                "aggregate_by_condition": v2_aggregate.to_dict(orient="records"),
            },
            "localization_interpretation": (
                "The deployed v3 event-localization rule uses only accumulated camera ROI activity; "
                "shifting reference-force labels does not change live ROI inference."
            ),
            "limitations": [
                "All six outer groups were previously inspected; this is diagnostic reuse, not untouched confirmation.",
                "The held-out oracle lag is descriptive only and was not used for model selection.",
                "A constant global lag cannot correct frame jitter or session-specific clock drift.",
                "Force resolution and physical validation remain unestablished.",
            ],
        }
        atomic_write_json(temporary / "summary.json", summary)
        manifest = {
            "run_id": run_id,
            "identity": identity,
            "inputs": {
                "config": CONFIG_PATH.relative_to(PROJECT_ROOT).as_posix(),
                "preprocessing_spec": PREPROCESSING_SPEC_PATH.relative_to(
                    PROJECT_ROOT
                ).as_posix(),
                "v2_spec": V2_SPEC_PATH.relative_to(PROJECT_ROOT).as_posix(),
                "feature_report_sha256": sha256_file(
                    feature_dir / "reconciliation_report.json"
                ),
                "split_report_sha256": sha256_file(split_dir / "leakage_audit.json"),
                "v2_feature_source_manifest_hash": v2_feature_report[
                    "source_manifest_hash"
                ],
                "v2_split_hash": v2_split_report["split_hash"],
            },
            "outputs": output_file_hashes(temporary),
        }
        atomic_write_json(temporary / "run_manifest.json", manifest)
        publish_run_directory(temporary, final)
    finally:
        if temporary.exists():
            # A failed temporary run contains no source data and is safe to leave for audit.
            pass
    return final


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum-ms", type=int, default=-500)
    parser.add_argument("--maximum-ms", type=int, default=500)
    parser.add_argument("--step-ms", type=int, default=10)
    args = parser.parse_args()
    output = run(args.minimum_ms, args.maximum_ms, args.step_ms)
    print(json.dumps({"output": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
