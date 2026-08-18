"""Fit manual-only lag, contact, safe-range, and optical-support artifacts.

Every fitted value is derived inside a complete-session training fold. The
command never evaluates an outer held-out TEST group and never reads outside
the dedicated manual-only inventory, feature-store, and split namespaces.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from scipy.optimize import nnls

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")

from sklearn.isotonic import IsotonicRegression
from sklearn.neighbors import NearestNeighbors

from core.live_sensor_contracts import canonical_json_hash, load_json_object, validate_json_schema
from core.timestamp_alignment import (
    DEFAULT_MAXIMUM_INTERPOLATION_GAP_MS,
    align_reference_force_by_timestamp,
    alignment_metadata,
)
from scripts.live_sensor_common import (
    PROJECT_ROOT,
    RunLogger,
    assert_manual_only_artifact_lineage,
    atomic_write_json,
    code_hash,
    create_temporary_run_directory,
    elapsed_seconds,
    load_study_config,
    make_run_id,
    output_file_hashes,
    platform_manifest,
    publish_run_directory,
    resolve_project_path,
    sha256_file,
    utc_now_iso,
)


COMMAND_NAME = "fit-manual-only-preprocessing"
SCHEMA_VERSION = "1.0.0"
SPEC_SCHEMA = PROJECT_ROOT / "contracts" / "manual_only_preprocessing.schema.json"
ALIGNMENT_SPEC = PROJECT_ROOT / "config" / "manual_timestamp_alignment.json"
ALIGNMENT_SPEC_SCHEMA = (
    PROJECT_ROOT / "contracts" / "manual_timestamp_alignment.schema.json"
)
RAW_LIGHT_COLUMNS = [f"roi{roi}_positive_delta_sum" for roi in range(1, 10)]
AREA_COLUMNS = [f"roi{roi}_area" for roi in range(1, 10)]
ACTIVE_COLUMNS = [f"roi{roi}_active_fraction" for roi in range(1, 10)]
RAW_V_COLUMNS = [f"roi{roi}_raw_v_mean" for roi in range(1, 10)]
BASE_COLUMNS = [
    "frame_uid", "archive_id", "session_id", "scientific_role", "test_group",
    "target_roi", "video_frame_index", "capture_monotonic_relative_s",
    "synchronized_monotonic_relative_s", "reference_force_corrected_N",
    "force_valid", "optical_valid", "layout_id",
]


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return _finite_float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def _find_passing_run(root: Path, report_name: str, config_hash: str) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in root.glob(f"*/{report_name}"):
        report = load_json_object(path)
        if report.get("study_config_hash") == config_hash and report.get("gate_pass"):
            matches.append((path.parent, report))
    if not matches:
        raise FileNotFoundError(f"no passing {report_name} exists under {root}")
    return sorted(matches, key=lambda item: item[0].name)[-1]


def _load_inputs(
    config: Mapping[str, Any], config_hash: str
) -> tuple[pd.DataFrame, Path, dict[str, Any], Path, dict[str, Any]]:
    feature_root = resolve_project_path(config["paths"]["feature_store_root"], writable=True)
    split_root = resolve_project_path(config["paths"]["split_root"], writable=True)
    feature_dir, feature_report = _find_passing_run(
        feature_root, "reconciliation_report.json", config_hash
    )
    split_dir, split_report = _find_passing_run(
        split_root, "leakage_audit.json", config_hash
    )
    if feature_report.get("dataset_policy") != "manual_only":
        raise ValueError("feature store is not a manual-only artifact")
    if split_report.get("dataset_policy") != "manual_only":
        raise ValueError("split manifest is not a manual-only artifact")
    if feature_report.get("source_manifest_hash") != split_report.get("source_manifest_hash"):
        raise ValueError("feature and split source-manifest hashes differ")
    split = pd.read_parquet(split_dir / "split_manifest.parquet")
    if set(split["session_id"]) != set(
        pd.read_parquet(feature_dir / "session_index.parquet")["session_id"]
    ):
        raise ValueError("feature and split session sets differ")
    columns = BASE_COLUMNS + RAW_LIGHT_COLUMNS + AREA_COLUMNS + ACTIVE_COLUMNS + RAW_V_COLUMNS
    paths = sorted((feature_dir / "sessions" / "archive_id=manual").glob("*.parquet"))
    if len(paths) != 83:
        raise ValueError(f"expected 83 manual session partitions; found {len(paths)}")
    frames = pd.concat(
        (pd.read_parquet(path, columns=columns) for path in paths), ignore_index=True
    )
    assignments = split[["session_id", "outer_fold", "no_contact_fold"]]
    frames = frames.merge(assignments, on="session_id", how="left", validate="many_to_one")
    if set(frames["archive_id"].astype(str)) != {"manual"}:
        raise ValueError("feature rows contain forbidden source lineage")
    if frames["frame_uid"].duplicated().any():
        raise ValueError("feature store contains duplicate frame_uid values")
    if not frames["force_valid"].all() or not frames["optical_valid"].all():
        raise ValueError("invalid force or optical rows reached preprocessing")
    return frames, feature_dir, feature_report, split_dir, split_report


def _training_frames(frames: pd.DataFrame, outer_fold: int | None) -> pd.DataFrame:
    eligible = frames["scientific_role"].isin(["model_primary", "no_contact"])
    if outer_fold is None:
        return frames.loc[eligible].copy()
    primary = (frames["scientific_role"] == "model_primary") & (
        frames["outer_fold"] != outer_fold
    )
    no_contact = (frames["scientific_role"] == "no_contact") & (
        frames["no_contact_fold"] != outer_fold
    )
    return frames.loc[primary | no_contact].copy()


def _inner_partitions(
    training: pd.DataFrame, validation_group: str, validation_fold: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    primary = training["scientific_role"] == "model_primary"
    no_contact = training["scientific_role"] == "no_contact"
    fit = training.loc[
        (primary & (training["test_group"] != validation_group))
        | (no_contact & (training["no_contact_fold"] != validation_fold))
    ].copy()
    validation = training.loc[
        (primary & (training["test_group"] == validation_group))
        | (no_contact & (training["no_contact_fold"] == validation_fold))
    ].copy()
    return fit, validation


def pair_lagged_force(
    frames: pd.DataFrame,
    lag_ms: int,
    *,
    maximum_interpolation_gap_ms: float = DEFAULT_MAXIMUM_INTERPOLATION_GAP_MS,
) -> pd.Series:
    """Compatibility wrapper for session-safe timestamp alignment.

    This function intentionally does not shift DataFrame rows or video-frame
    indices.  See :func:`core.timestamp_alignment.align_reference_force_by_timestamp`.
    """

    return align_reference_force_by_timestamp(
        frames,
        lag_ms,
        maximum_interpolation_gap_ms=maximum_interpolation_gap_ms,
    )


def _fit_floors(training: pd.DataFrame) -> np.ndarray:
    no_contact = training[training["scientific_role"] == "no_contact"]
    if no_contact["session_id"].nunique() < 1:
        raise ValueError("no no-contact training session is available")
    per_session = no_contact.groupby("session_id")[RAW_LIGHT_COLUMNS].median()
    floors = per_session.median(axis=0).to_numpy(float)
    if not np.isfinite(floors).all() or (floors < 0).any():
        raise ValueError("invalid no-contact floor")
    return floors


def _corrected_light(frames: pd.DataFrame, floors: np.ndarray) -> np.ndarray:
    raw = frames[RAW_LIGHT_COLUMNS].to_numpy(float)
    return np.maximum(raw - floors.reshape(1, -1), 0.0)


def _balanced_weights(session_ids: np.ndarray, forces: np.ndarray) -> np.ndarray:
    bins = np.floor(np.clip(forces, 0.0, 2.999999) / 0.25).astype(int)
    table = pd.DataFrame({"session": session_ids.astype(str), "bin": bins})
    counts = table.groupby(["session", "bin"]).size().rename("count")
    bins_per_session = counts.reset_index().groupby("session")["bin"].nunique()
    keys = pd.MultiIndex.from_arrays([table["session"], table["bin"]])
    within = counts.reindex(keys).to_numpy(float)
    per_session = bins_per_session.reindex(table["session"]).to_numpy(float)
    weights = 1.0 / np.maximum(within * per_session, 1.0)
    return weights / weights.mean()


def _nnls_mae(
    fit: pd.DataFrame,
    validation: pd.DataFrame,
    fit_force: pd.Series,
    validation_force: pd.Series,
    floors: np.ndarray,
    minimum: float,
    maximum: float,
) -> float | None:
    y_fit = fit_force.loc[fit.index].to_numpy(float)
    y_validation = validation_force.loc[validation.index].to_numpy(float)
    fit_mask = np.isfinite(y_fit) & (y_fit >= minimum) & (y_fit <= maximum)
    validation_mask = (
        np.isfinite(y_validation)
        & (y_validation >= minimum)
        & (y_validation <= maximum)
    )
    if fit_mask.sum() < 20 or validation_mask.sum() < 5:
        return None
    x_fit = _corrected_light(fit, floors)[fit_mask]
    x_validation = _corrected_light(validation, floors)[validation_mask]
    weights = _balanced_weights(
        fit.loc[fit_mask, "session_id"].to_numpy(), y_fit[fit_mask]
    )
    root_weights = np.sqrt(weights)
    coefficients, _ = nnls(x_fit * root_weights[:, None], y_fit[fit_mask] * root_weights)
    prediction = x_validation @ coefficients
    session_ids = validation.loc[validation_mask, "session_id"].astype(str).to_numpy()
    errors = np.abs(prediction - y_validation[validation_mask])
    return float(pd.Series(errors).groupby(session_ids).mean().mean())


def _select_lag(
    training: pd.DataFrame,
    lagged: Mapping[int, pd.Series],
    spec: Mapping[str, Any],
    group_to_fold: Mapping[str, int],
) -> tuple[int, list[dict[str, Any]]]:
    groups = sorted(training.loc[
        training["scientific_role"] == "model_primary", "test_group"
    ].dropna().astype(str).unique())
    records: list[dict[str, Any]] = []
    for group in groups:
        fit, validation = _inner_partitions(training, group, group_to_fold[group])
        floors = _fit_floors(fit)
        for lag_ms, force in lagged.items():
            score = _nnls_mae(
                fit[fit["scientific_role"] == "model_primary"],
                validation[validation["scientific_role"] == "model_primary"],
                force,
                force,
                floors,
                float(spec["fit_force_min_N"]),
                float(spec["fit_force_max_N"]),
            )
            records.append(
                {"inner_validation_group": group, "lag_ms": lag_ms, "session_balanced_mae_N": score}
            )
    scores = pd.DataFrame(records).dropna(subset=["session_balanced_mae_N"])
    aggregate = scores.groupby("lag_ms")["session_balanced_mae_N"].mean()
    if aggregate.empty:
        raise ValueError("lag search produced no valid inner-fold score")
    best = float(aggregate.min())
    tolerance = float(spec["tie_relative_tolerance"])
    eligible = aggregate[aggregate <= best * (1.0 + tolerance)]
    selected = int(eligible.index.min())
    means = aggregate.to_dict()
    for record in records:
        record["mean_inner_mae_N"] = _finite_float(means.get(record["lag_ms"]))
        record["selected"] = record["lag_ms"] == selected
    return selected, records


def _fit_scales(
    training: pd.DataFrame,
    lagged_force: pd.Series,
    floors: np.ndarray,
    spec: Mapping[str, Any],
) -> np.ndarray:
    corrected = _corrected_light(training, floors)
    force = lagged_force.loc[training.index].to_numpy(float)
    primary = training["scientific_role"].to_numpy(str) == "model_primary"
    no_contact = training["scientific_role"].to_numpy(str) == "no_contact"
    scales: list[float] = []
    for roi in range(1, 10):
        mask = (
            primary
            & np.isfinite(force)
            & (force >= float(spec["scale_force_min_N"]))
            & (force <= float(spec["scale_force_max_N"]))
        )
        values = pd.DataFrame(
            {
                "session_id": training.loc[mask, "session_id"].astype(str),
                "light": corrected[mask, roi - 1],
            }
        )
        per_session = values.groupby("session_id")["light"].quantile(
            float(spec["scale_session_quantile"])
        )
        press_scale = float(per_session.median()) if len(per_session) else math.nan
        noise_values = pd.DataFrame(
            {
                "session_id": training.loc[no_contact, "session_id"].astype(str),
                "light": corrected[no_contact, roi - 1],
            }
        )
        noise_per_session = noise_values.groupby("session_id")["light"].quantile(
            float(spec["noise_scale_session_quantile"])
        )
        noise_scale = (
            float(noise_per_session.median())
            if len(noise_per_session)
            else math.nan
        )
        if not math.isfinite(press_scale) or not math.isfinite(noise_scale):
            raise ValueError(f"ROI {roi} has no supported response scale")
        scales.append(
            max(press_scale, noise_scale, float(spec["minimum_scale"]))
        )
    return np.asarray(scales, dtype=float)


def _contact_scores(frames: pd.DataFrame, floors: np.ndarray, scales: np.ndarray) -> np.ndarray:
    return (_corrected_light(frames, floors) / scales.reshape(1, -1)).max(axis=1)


def _smallest_fpr_threshold(scores: np.ndarray, target: float) -> tuple[float, float]:
    values = np.asarray(scores, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        raise ValueError("no no-contact scores are available")
    candidates = np.append(np.unique(values), np.nextafter(values.max(), np.inf))
    for threshold in candidates:
        fpr = float(np.mean(values >= threshold))
        if fpr <= target:
            return float(threshold), fpr
    raise AssertionError("finite threshold candidates must include zero-FPR sentinel")


def _derive_fdetect(
    primary: pd.DataFrame,
    lagged_force: pd.Series,
    detected: np.ndarray,
    range_spec: Mapping[str, Any],
    recall_target: float,
) -> tuple[float | None, list[dict[str, Any]]]:
    width = float(range_spec["force_bin_width_N"])
    maximum = float(range_spec["nominal_max_N"])
    force = lagged_force.loc[primary.index].to_numpy(float)
    mask = np.isfinite(force) & (force >= 0.0) & (force < maximum)
    bins = np.floor(force[mask] / width).astype(int)
    table = pd.DataFrame(
        {
            "session_id": primary.loc[mask, "session_id"].astype(str).to_numpy(),
            "bin": bins,
            "detected": detected[mask].astype(float),
        }
    )
    per_session = table.groupby(["session_id", "bin"])["detected"].mean().reset_index()
    summary = per_session.groupby("bin")["detected"].agg(["median", "count"])
    records: list[dict[str, Any]] = []
    supported: list[int] = []
    for bin_index in range(int(maximum / width)):
        if bin_index in summary.index:
            recall = float(summary.loc[bin_index, "median"])
            sessions = int(summary.loc[bin_index, "count"])
        else:
            recall, sessions = math.nan, 0
        is_supported = sessions >= int(range_spec["minimum_sessions_per_bin"])
        if is_supported:
            supported.append(bin_index)
        records.append(
            {
                "force_bin_lower_N": bin_index * width,
                "force_bin_upper_N": (bin_index + 1) * width,
                "session_balanced_recall": _finite_float(recall),
                "session_count": sessions,
                "supported": is_supported,
            }
        )
    fdetect: float | None = None
    for candidate in supported:
        higher = [index for index in supported if index >= candidate]
        if higher and all(float(summary.loc[index, "median"]) >= recall_target for index in higher):
            fdetect = candidate * width
            break
    return fdetect, records


def _loading_mask(group: pd.DataFrame, force: np.ndarray, spec: Mapping[str, Any]) -> np.ndarray:
    order = np.argsort(group["capture_monotonic_relative_s"].to_numpy(float))
    sorted_force = pd.Series(force[order]).rolling(
        int(spec["loading_smoothing_frames"]), center=True, min_periods=1
    ).median().to_numpy(float)
    sorted_time = group.iloc[order]["capture_monotonic_relative_s"].to_numpy(float)
    derivative = np.gradient(sorted_force, sorted_time)
    sorted_mask = derivative > float(spec["loading_derivative_threshold_N_s"])
    result = np.zeros(len(group), dtype=bool)
    result[order] = sorted_mask
    return result


def _range_decision(
    primary: pd.DataFrame,
    lagged_force: pd.Series,
    floors: np.ndarray,
    fdetect: float | None,
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    width = float(spec["force_bin_width_N"])
    maximum = float(spec["nominal_max_N"])
    minimum_sessions = int(spec["minimum_sessions_per_bin"])
    corrected = _corrected_light(primary, floors)
    force_all = lagged_force.loc[primary.index].to_numpy(float)
    session_rows: list[dict[str, Any]] = []
    for session_id, group in primary.groupby("session_id", sort=True):
        positions = primary.index.get_indexer(group.index)
        force = force_all[positions]
        loading = _loading_mask(group, force, spec)
        roi = int(group["target_roi"].iloc[0])
        valid = loading & np.isfinite(force) & (force >= 0.0) & (force < maximum)
        bins = np.floor(force[valid] / width).astype(int)
        lights = corrected[positions[valid], roi - 1]
        local = pd.DataFrame({"bin": bins, "light": lights})
        for bin_index, values in local.groupby("bin")["light"]:
            session_rows.append(
                {"session_id": str(session_id), "target_roi": roi, "bin": int(bin_index), "session_median_light": float(values.median())}
            )
    session_table = pd.DataFrame(session_rows)
    response_records: list[dict[str, Any]] = []
    roi_decisions: dict[str, Any] = {}
    all_eligible = True
    safe_limits: list[float] = []
    required_first = int(math.floor(max(float(fdetect or 0.0), width) / width))
    last_bin = int(maximum / width) - 1
    for roi in range(1, 10):
        selected = session_table[session_table["target_roi"] == roi]
        grouped = selected.groupby("bin")["session_median_light"].agg(["median", "count"])
        supported = [int(index) for index in grouped.index if int(grouped.loc[index, "count"]) >= minimum_sessions]
        required = list(range(required_first, last_bin + 1))
        missing = sorted(set(required) - set(supported))
        x = np.asarray([(index + 0.5) * width for index in supported], dtype=float)
        y = np.asarray([float(grouped.loc[index, "median"]) for index in supported], dtype=float)
        fitted = np.asarray([], dtype=float)
        initial_slope: float | None = None
        knee: float | None = None
        safe: float | None = None
        reason: str | None = None
        if missing:
            reason = "insufficient_session_support"
        elif len(x) < 4:
            reason = "insufficient_supported_bins"
        else:
            fitted = IsotonicRegression(increasing=True, out_of_bounds="clip").fit_transform(x, y)
            initial = (x <= float(spec["initial_force_max_N"])) & (x >= max(width / 2.0, float(fdetect or 0.0)))
            if initial.sum() < 3:
                reason = "insufficient_initial_slope_support"
            else:
                slope, _ = np.polyfit(x[initial], fitted[initial], 1)
                initial_slope = max(float(slope), 0.0)
                if initial_slope <= 0.0:
                    reason = "nonpositive_initial_slope"
                else:
                    increments = np.diff(fitted) / np.diff(x)
                    low = increments < float(spec["sensitivity_fraction"]) * initial_slope
                    run = int(spec["consecutive_bins"])
                    for start in range(0, max(len(low) - run + 1, 0)):
                        if bool(low[start : start + run].all()):
                            knee = float(x[start])
                            break
                    safe = maximum if knee is None else float(spec["safe_limit_fraction_below_knee"]) * knee
                    if fdetect is not None and safe <= fdetect:
                        reason = "safe_limit_not_above_fdetect"
        eligible = reason is None and safe is not None
        all_eligible = all_eligible and eligible
        if eligible:
            safe_limits.append(float(safe))
        fitted_lookup = {supported[index]: float(fitted[index]) for index in range(len(fitted))}
        for bin_index in range(int(maximum / width)):
            response_records.append(
                {
                    "target_roi": roi,
                    "force_bin_lower_N": bin_index * width,
                    "force_bin_upper_N": (bin_index + 1) * width,
                    "session_count": int(grouped.loc[bin_index, "count"]) if bin_index in grouped.index else 0,
                    "session_balanced_median_light": _finite_float(grouped.loc[bin_index, "median"]) if bin_index in grouped.index else None,
                    "monotonic_fitted_light": fitted_lookup.get(bin_index),
                    "supported": bin_index in supported,
                }
            )
        roi_decisions[str(roi)] = {
            "eligible": eligible,
            "reason": reason,
            "missing_supported_bins": missing,
            "initial_low_force_slope": initial_slope,
            "plateau_knee_N": knee,
            "safe_limit_N": safe,
        }
    common = min([maximum, *safe_limits]) if all_eligible and len(safe_limits) == 9 else None
    return {
        "eligible": bool(common is not None),
        "fdetect_N": fdetect,
        "common_fmax_N": common,
        "roi_decisions": roi_decisions,
    }, response_records


def _support_matrix(frames: pd.DataFrame, floors: np.ndarray) -> np.ndarray:
    corrected = _corrected_light(frames, floors)
    area = frames[AREA_COLUMNS].to_numpy(float)
    active = frames[ACTIVE_COLUMNS].to_numpy(float)
    raw_v = frames[RAW_V_COLUMNS].to_numpy(float)
    result = np.empty((len(frames), 27), dtype=float)
    for roi in range(9):
        result[:, roi * 3] = corrected[:, roi] / area[:, roi]
        result[:, roi * 3 + 1] = active[:, roi]
        result[:, roi * 3 + 2] = raw_v[:, roi]
    return result


def _stable_hash(value: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}|{value}".encode("utf-8")).hexdigest()


def _stratified_indices(
    frames: pd.DataFrame,
    force: pd.Series,
    detected: np.ndarray,
    per_stratum: int,
    maximum: int,
    seed: int,
) -> np.ndarray:
    values = force.loc[frames.index].to_numpy(float)
    force_bins = np.full(len(frames), "invalid", dtype=object)
    finite = np.isfinite(values)
    force_bins[finite] = np.floor(
        np.clip(values[finite], 0.0, 2.999999) / 0.25
    ).astype(int).astype(str)
    force_bins[
        frames["scientific_role"].to_numpy(str) == "no_contact"
    ] = "no_contact"
    table = pd.DataFrame(
        {
            "position": np.arange(len(frames)),
            "session_id": frames["session_id"].astype(str).to_numpy(),
            "target_roi": frames["target_roi"].astype(int).to_numpy(),
            "contact_state": np.where(detected, "contact", "no_contact"),
            "force_bin": force_bins,
            "hash": [_stable_hash(value, seed) for value in frames["frame_uid"].astype(str)],
        }
    )
    groups: list[list[int]] = []
    for _, group in table.groupby(
        ["session_id", "target_roi", "contact_state", "force_bin"], sort=True
    ):
        groups.append(
            group.sort_values("hash", kind="stable")["position"].head(per_stratum).astype(int).tolist()
        )
    selected: list[int] = []
    offset = 0
    while len(selected) < maximum:
        added = False
        for group in groups:
            if offset < len(group):
                selected.append(group[offset])
                added = True
                if len(selected) == maximum:
                    break
        if not added:
            break
        offset += 1
    return np.asarray(selected, dtype=int)


def _fit_guard_base(
    frames: pd.DataFrame,
    force: pd.Series,
    floors: np.ndarray,
    scales: np.ndarray,
    threshold: float,
    spec: Mapping[str, Any],
    seed: int,
) -> dict[str, Any]:
    scores = _contact_scores(frames, floors, scales)
    detected = scores >= threshold
    indices = _stratified_indices(
        frames, force, detected,
        int(spec["max_samples_per_stratum"]), int(spec["max_reference_samples"]), seed,
    )
    matrix = _support_matrix(frames, floors)[indices]
    if not np.isfinite(matrix).all() or (matrix[:, 0::3] < 0).any():
        raise ValueError("invalid optical-support training vector")
    low_q, high_q = (float(value) for value in spec["envelope_quantiles"])
    lower = np.quantile(matrix, low_q, axis=0)
    upper = np.quantile(matrix, high_q, axis=0)
    center = np.median(matrix, axis=0)
    scale = 1.4826 * np.median(np.abs(matrix - center), axis=0)
    scale = np.maximum(scale, float(spec["robust_scale_floor"]))
    standardized = (matrix - center) / scale
    neighbors = NearestNeighbors(
        n_neighbors=min(2, len(standardized)), algorithm="brute", n_jobs=1
    ).fit(standardized)
    distances = neighbors.kneighbors(standardized, return_distance=True)[0]
    self_excluded = distances[:, -1]
    distance_thresholds = {
        str(value): float(np.quantile(self_excluded, float(value)))
        for value in spec["distance_quantile_options"]
    }
    return {
        "lower": lower, "upper": upper, "center": center, "scale": scale,
        "references": standardized, "distance_thresholds": distance_thresholds,
        "reference_count": len(standardized),
    }


def _guard_validation_metrics(
    base: Mapping[str, Any],
    frames: pd.DataFrame,
    force: pd.Series,
    floors: np.ndarray,
    scales: np.ndarray,
    threshold: float,
    spec: Mapping[str, Any],
    seed: int,
) -> list[dict[str, Any]]:
    detected = _contact_scores(frames, floors, scales) >= threshold
    indices = _stratified_indices(
        frames, force, detected,
        int(spec["max_samples_per_stratum"]), int(spec["max_reference_samples"]), seed,
    )
    matrix = _support_matrix(frames, floors)[indices]
    standardized = (matrix - base["center"]) / base["scale"]
    model = NearestNeighbors(n_neighbors=1, algorithm="brute", n_jobs=1).fit(
        base["references"]
    )
    distances = model.kneighbors(standardized, return_distance=True)[0][:, 0]
    span = np.maximum(base["upper"] - base["lower"], base["scale"])
    records: list[dict[str, Any]] = []
    for margin in spec["margin_options"]:
        envelope = (
            (matrix >= base["lower"] - float(margin) * span)
            & (matrix <= base["upper"] + float(margin) * span)
        ).all(axis=1)
        for quantile in spec["distance_quantile_options"]:
            distance = distances <= base["distance_thresholds"][str(quantile)]
            retained = envelope & distance
            records.append(
                {"margin": float(margin), "distance_quantile": float(quantile), "retained": int(retained.sum()), "total": int(len(retained))}
            )
    return records


def _fit_and_tune_support(
    training: pd.DataFrame,
    selected_force: pd.Series,
    selected_lag: int,
    contact_spec: Mapping[str, Any],
    support_spec: Mapping[str, Any],
    group_to_fold: Mapping[str, int],
    seed: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    groups = sorted(training.loc[
        training["scientific_role"] == "model_primary", "test_group"
    ].dropna().astype(str).unique())
    tuning_rows: list[dict[str, Any]] = []
    for inner_index, group in enumerate(groups):
        fit, validation = _inner_partitions(training, group, group_to_fold[group])
        floors = _fit_floors(fit)
        scales = _fit_scales(fit, selected_force, floors, contact_spec)
        no_contact_fit = fit[fit["scientific_role"] == "no_contact"]
        threshold, _ = _smallest_fpr_threshold(
            _contact_scores(no_contact_fit, floors, scales),
            float(contact_spec["no_contact_frame_fpr_target"]),
        )
        base = _fit_guard_base(
            fit, selected_force, floors, scales, threshold, support_spec, seed + inner_index
        )
        rows = _guard_validation_metrics(
            base, validation, selected_force, floors, scales, threshold, support_spec,
            seed + 100 + inner_index,
        )
        for row in rows:
            row["inner_validation_group"] = group
            tuning_rows.append(row)
    table = pd.DataFrame(tuning_rows)
    aggregate = table.groupby(["margin", "distance_quantile"])[["retained", "total"]].sum().reset_index()
    aggregate["retention"] = aggregate["retained"] / aggregate["total"]
    target = float(support_spec["inner_retention_target"])
    eligible = aggregate[aggregate["retention"] >= target].sort_values(
        ["margin", "distance_quantile"], kind="stable"
    )
    tuning_pass = not eligible.empty
    selected = (
        eligible.iloc[0]
        if tuning_pass
        else aggregate.sort_values(["retention", "margin", "distance_quantile"], ascending=[False, True, True], kind="stable").iloc[0]
    )
    floors = _fit_floors(training)
    scales = _fit_scales(training, selected_force, floors, contact_spec)
    no_contact = training[training["scientific_role"] == "no_contact"]
    threshold, fpr = _smallest_fpr_threshold(
        _contact_scores(no_contact, floors, scales),
        float(contact_spec["no_contact_frame_fpr_target"]),
    )
    final_base = _fit_guard_base(
        training, selected_force, floors, scales, threshold, support_spec, seed + 1000
    )
    params = {
        "selected_margin": float(selected["margin"]),
        "selected_distance_quantile": float(selected["distance_quantile"]),
        "selected_distance_threshold": final_base["distance_thresholds"][str(float(selected["distance_quantile"]))],
        "inner_retention": float(selected["retention"]),
        "inner_retention_target": target,
        "tuning_pass": tuning_pass,
        "reference_count": int(final_base["reference_count"]),
        "lag_ms": selected_lag,
        "contact_threshold": threshold,
        "training_no_contact_fpr": fpr,
    }
    return params, final_base, tuning_rows


def _write_npz(path: Path, *, base: Mapping[str, Any], floors: np.ndarray, scales: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("wb") as handle:
        np.savez_compressed(
            handle,
            lower=np.asarray(base["lower"], dtype=np.float64),
            upper=np.asarray(base["upper"], dtype=np.float64),
            center=np.asarray(base["center"], dtype=np.float64),
            scale=np.asarray(base["scale"], dtype=np.float64),
            references=np.asarray(base["references"], dtype=np.float64),
            no_contact_floors=np.asarray(floors, dtype=np.float64),
            response_scales=np.asarray(scales, dtype=np.float64),
        )
    temporary.replace(path)


def run_fit(
    config_path: str | Path,
    spec_path: str | Path,
    *,
    alignment_spec_path: str | Path = ALIGNMENT_SPEC,
    dry_run: bool = False,
) -> int:
    started = time.perf_counter()
    config, config_hash, selected_config = load_study_config(config_path)
    selected_spec = Path(spec_path).resolve()
    spec_hash = validate_json_schema(selected_spec, SPEC_SCHEMA)
    spec = load_json_object(selected_spec)
    selected_alignment_spec = Path(alignment_spec_path).resolve()
    alignment_spec_hash = validate_json_schema(
        selected_alignment_spec, ALIGNMENT_SPEC_SCHEMA
    )
    alignment_spec = load_json_object(selected_alignment_spec)
    expected_alignment = alignment_metadata(
        float(alignment_spec["maximum_interpolation_gap_ms"])
    )
    for key, expected in expected_alignment.items():
        if alignment_spec.get(key) != expected:
            raise ValueError(f"timestamp-alignment contract mismatch for {key}")
    expected_feature_order = [
        item
        for roi in range(1, 10)
        for item in (
            f"roi{roi}_corrected_light_per_pixel",
            f"roi{roi}_active_fraction",
            f"roi{roi}_raw_v_mean",
        )
    ]
    if spec["optical_support"]["feature_order"] != expected_feature_order:
        raise ValueError("optical-support feature order does not match implementation")
    frames, feature_dir, feature_report, split_dir, split_report = _load_inputs(config, config_hash)
    script_hash = code_hash(
        (
            "scripts/fit_manual_only_preprocessing.py",
            "scripts/live_sensor_common.py",
            "core/timestamp_alignment.py",
            "contracts/manual_only_preprocessing.schema.json",
            "contracts/manual_timestamp_alignment.schema.json",
        )
    )
    identity = {
        "study_config_hash": config_hash,
        "preprocessing_spec_hash": spec_hash,
        "timestamp_alignment_spec_hash": alignment_spec_hash,
        "feature_report_hash": sha256_file(feature_dir / "reconciliation_report.json"),
        "split_hash": split_report["split_hash"],
        "code_hash": script_hash,
    }
    run_id = make_run_id("manual-preprocessing", identity)
    output_root = resolve_project_path(config["paths"]["model_run_root"], writable=True) / "preprocessing"
    final_dir = output_root / run_id
    if dry_run:
        print(json.dumps({
            "dry_run": True, "run_id": run_id, "frame_count": len(frames),
            "eligible_session_count": int(frames.loc[frames.scientific_role.isin(["model_primary", "no_contact"]), "session_id"].nunique()),
            "lag_candidates": list(range(int(spec["lag"]["minimum_ms"]), int(spec["lag"]["maximum_ms"]) + 1, int(spec["lag"]["step_ms"]))),
            "timestamp_alignment": alignment_spec,
            "outer_folds": [1, 2, 3, 4, 5, 6], "includes_development_refit": True,
            "planned_output": str(final_dir), "dataset_policy": "manual_only",
        }, indent=2, sort_keys=True))
        return 0
    existing = final_dir / "run_manifest.json"
    input_identity_hash = canonical_json_hash(identity)
    if existing.exists() and load_json_object(existing).get("input_identity_hash") == input_identity_hash:
        report_path = final_dir / "preprocessing_summary.json"
        print(f"run_id={run_id}")
        print("reused=true")
        print(f"report={report_path}")
        return 0 if load_json_object(report_path).get("gate_pass") else 2
    temporary = create_temporary_run_directory(output_root, run_id)
    logger = RunLogger(temporary / "events.jsonl", COMMAND_NAME, run_id)
    try:
        logger.emit("INFO", "run_started", frame_count=len(frames), config_hash=config_hash, spec_hash=spec_hash)
        lag_values = list(range(int(spec["lag"]["minimum_ms"]), int(spec["lag"]["maximum_ms"]) + 1, int(spec["lag"]["step_ms"])))
        lagged = {
            lag: pair_lagged_force(
                frames,
                lag,
                maximum_interpolation_gap_ms=float(
                    alignment_spec["maximum_interpolation_gap_ms"]
                ),
            )
            for lag in lag_values
        }
        outer_groups = list(config["force_study"]["outer_groups"])
        group_to_fold = {group: index + 1 for index, group in enumerate(outer_groups)}
        fold_summaries: dict[str, Any] = {}
        lag_records: list[dict[str, Any]] = []
        contact_records: list[dict[str, Any]] = []
        range_records: list[dict[str, Any]] = []
        support_records: list[dict[str, Any]] = []
        fold_keys: list[int | None] = [1, 2, 3, 4, 5, 6, None]
        for ordinal, outer_fold in enumerate(fold_keys, start=1):
            key = "development" if outer_fold is None else str(outer_fold)
            training = _training_frames(frames, outer_fold)
            selected_lag, current_lag_records = _select_lag(
                training, lagged, spec["lag"], group_to_fold
            )
            for record in current_lag_records:
                record["outer_fold"] = key
            lag_records.extend(current_lag_records)
            selected_force = lagged[selected_lag]
            floors = _fit_floors(training)
            primary = training[training["scientific_role"] == "model_primary"]
            no_contact = training[training["scientific_role"] == "no_contact"]
            scales = _fit_scales(training, selected_force, floors, spec["contact"])
            threshold, no_contact_fpr = _smallest_fpr_threshold(
                _contact_scores(no_contact, floors, scales),
                float(spec["contact"]["no_contact_frame_fpr_target"]),
            )
            primary_detected = _contact_scores(primary, floors, scales) >= threshold
            fdetect, current_contact = _derive_fdetect(
                primary, selected_force, primary_detected, spec["range"],
                float(spec["contact"]["recall_target"]),
            )
            for record in current_contact:
                record["outer_fold"] = key
            contact_records.extend(current_contact)
            range_decision, current_range = _range_decision(
                primary, selected_force, floors, fdetect, spec["range"]
            )
            for record in current_range:
                record["outer_fold"] = key
            range_records.extend(current_range)
            support_params, support_base, current_support = _fit_and_tune_support(
                training, selected_force, selected_lag, spec["contact"],
                spec["optical_support"], group_to_fold, int(spec["seed"]) + ordinal * 10000,
            )
            for record in current_support:
                record["outer_fold"] = key
            support_records.extend(current_support)
            fold_dir = temporary / f"fold_{key}"
            _write_npz(fold_dir / "optical_support_arrays.npz", base=support_base, floors=floors, scales=scales)
            eligibility_reasons: list[str] = []
            if fdetect is None:
                eligibility_reasons.append("contact_recall_below_target")
            if not range_decision["eligible"]:
                eligibility_reasons.append("common_safe_range_unsupported")
            if not support_params["tuning_pass"]:
                eligibility_reasons.append(
                    "optical_support_inner_retention_below_target"
                )
            fold_summary = {
                "outer_fold": key,
                "training_test_groups": sorted(primary["test_group"].astype(str).unique()),
                "training_primary_session_count": int(primary["session_id"].nunique()),
                "training_no_contact_session_count": int(no_contact["session_id"].nunique()),
                "selected_lag_ms": selected_lag,
                "timestamp_alignment": alignment_spec,
                "no_contact_floors": floors.tolist(),
                "response_scales": scales.tolist(),
                "contact_threshold": threshold,
                "training_no_contact_frame_fpr": no_contact_fpr,
                "fdetect_N": fdetect,
                "range": range_decision,
                "optical_support": {
                    **support_params,
                    "feature_order": spec["optical_support"]["feature_order"],
                    "arrays_file": "optical_support_arrays.npz",
                },
                "eligible": not eligibility_reasons,
                "eligibility_reasons": eligibility_reasons,
            }
            atomic_write_json(fold_dir / "fold_summary.json", _json_safe(fold_summary))
            fold_summaries[key] = fold_summary
            logger.emit("INFO", "fold_finished", outer_fold=key, selected_lag_ms=selected_lag, fdetect_N=fdetect, common_fmax_N=range_decision["common_fmax_N"], eligible=fold_summary["eligible"])
            print(f"[{ordinal}/7] fold={key} lag_ms={selected_lag} fdetect_N={fdetect} fmax_N={range_decision['common_fmax_N']} eligible={fold_summary['eligible']}", flush=True)
        pd.DataFrame(lag_records).to_csv(temporary / "lag_inner_scores.csv", index=False, lineterminator="\n")
        pd.DataFrame(contact_records).to_csv(temporary / "contact_training_bins.csv", index=False, lineterminator="\n")
        pd.DataFrame(range_records).to_csv(temporary / "range_loading_response.csv", index=False, lineterminator="\n")
        pd.DataFrame(support_records).to_csv(temporary / "optical_support_inner_tuning.csv", index=False, lineterminator="\n")
        outer_eligible = all(fold_summaries[str(fold)]["eligible"] for fold in range(1, 7))
        development_eligible = bool(fold_summaries["development"]["eligible"])
        stop_reasons = sorted(
            {
                reason
                for summary in fold_summaries.values()
                for reason in summary["eligibility_reasons"]
            }
        )
        gate_checks = {
            "manual_only_lineage": True,
            "six_outer_folds_present": set(fold_summaries) >= {str(value) for value in range(1, 7)},
            "all_outer_fold_preprocessing_eligible": outer_eligible,
            "development_refit_eligible": development_eligible,
            "held_out_groups_not_used_for_fitting": all(
                f"TEST{fold}" not in fold_summaries[str(fold)]["training_test_groups"]
                for fold in range(1, 7)
            ),
        }
        gate_pass = all(gate_checks.values())
        report = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "created_at": utc_now_iso(), "dataset_policy": "manual_only",
            "study_config_hash": config_hash, "preprocessing_spec_hash": spec_hash,
            "timestamp_alignment_spec_hash": alignment_spec_hash,
            "timestamp_alignment": alignment_spec,
            "source_manifest_hash": feature_report["source_manifest_hash"],
            "feature_spec_hash": feature_report["feature_spec_hash"],
            "split_hash": split_report["split_hash"], "folds": fold_summaries,
            "gate_checks": gate_checks, "gate_pass": gate_pass,
            "stop_condition": None if gate_pass else "manual_only_preprocessing_ineligible",
            "stop_reasons": stop_reasons,
            "notes": [
                "Replay-only sessions were not used for fitting.",
                "Reference force was aligned by per-session monotonic timestamps and linear interpolation; frame-row shifting was not used.",
                "Positive lag pairs an optical feature at t with reference force at t-L.",
                "Timestamp extrapolation, cross-session pairing, and interpolation across gaps above 300 ms were prohibited.",
                "All floors, scales, thresholds, Fdetect, range, and support parameters were fit inside their training fold.",
                "Outer held-out TEST groups were not evaluated by this command.",
            ],
        }
        assert_manual_only_artifact_lineage(report)
        atomic_write_json(temporary / "preprocessing_summary.json", _json_safe(report))
        logger.emit("INFO" if gate_pass else "ERROR", "preprocessing_gate_finished", gate_pass=gate_pass, checks=gate_checks)
        logger.close()
        output_hashes = output_file_hashes(temporary, exclude=("run_manifest.json",))
        manifest = {
            "schema_version": SCHEMA_VERSION, "command": COMMAND_NAME, "argv": sys.argv,
            "run_id": run_id, "created_at": utc_now_iso(), "completed_at": utc_now_iso(),
            "elapsed_s": elapsed_seconds(started), "dataset_policy": "manual_only",
            "config_path": str(selected_config), "preprocessing_spec_path": str(selected_spec),
            "timestamp_alignment_spec_path": str(selected_alignment_spec),
            "study_config_hash": config_hash, "preprocessing_spec_hash": spec_hash,
            "timestamp_alignment_spec_hash": alignment_spec_hash,
            "timestamp_alignment": alignment_spec,
            "source_manifest_hash": feature_report["source_manifest_hash"], "split_hash": split_report["split_hash"],
            "code_hash": script_hash, "input_identity_hash": input_identity_hash,
            "inputs": {
                "manual_feature_store": str(feature_dir), "manual_split_manifest": str(split_dir),
                "manual_archive_sha256": config["sources"]["manual"]["expected_sha256"],
            },
            "outputs": output_hashes, "environment": platform_manifest(), "warnings": [],
            "exclusions": ["replay_only"], "gate_pass": gate_pass,
        }
        assert_manual_only_artifact_lineage(manifest)
        atomic_write_json(temporary / "run_manifest.json", manifest)
        published, reused = publish_run_directory(temporary, final_dir)
        print(f"run_id={run_id}")
        print(f"reused={str(reused).lower()}")
        print(f"report={published / 'preprocessing_summary.json'}")
        return 0 if gate_pass else 2
    except Exception as exc:
        logger.emit("ERROR", "run_failed", error=f"{type(exc).__name__}: {exc}")
        logger.close()
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fit frozen manual-only lag/contact/range/support preprocessing without opening outer outcomes.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument(
        "--alignment-spec", default=str(ALIGNMENT_SPEC),
        help="Validated timestamp-alignment contract for optical/reference pairing.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_fit(
            args.config,
            args.spec,
            alignment_spec_path=args.alignment_spec,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
