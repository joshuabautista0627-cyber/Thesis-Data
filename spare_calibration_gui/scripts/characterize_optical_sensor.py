"""Characterize the original-frame optical sensor without fitting live models."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Mapping

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

from core.live_sensor_contracts import canonical_json_hash, load_json_object
from scripts.live_sensor_common import (
    PROJECT_ROOT,
    RunLogger,
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


COMMAND_NAME = "characterize-optical-sensor"
SCHEMA_VERSION = "1.0.0"


def _mad(values: pd.Series | np.ndarray) -> float:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if not len(array):
        return float("nan")
    median = float(np.median(array))
    return float(np.median(np.abs(array - median)))


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _find_passed_artifact(
    root: Path, report_name: str, config_hash: str
) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in root.glob(f"*/{report_name}"):
        report = load_json_object(path)
        if report.get("study_config_hash") == config_hash and report.get("gate_pass"):
            matches.append((path.parent, report))
    if not matches:
        raise FileNotFoundError(f"no passing {report_name} exists under {root}")
    return sorted(matches, key=lambda item: item[0].name)[-1]


def _discover_inputs(
    config: Mapping[str, Any], config_hash: str
) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
    feature_root = resolve_project_path(
        config["paths"]["feature_store_root"], writable=True
    )
    split_root = resolve_project_path(config["paths"]["split_root"], writable=True)
    feature_directory, feature_report = _find_passed_artifact(
        feature_root, "reconciliation_report.json", config_hash
    )
    split_directory, leakage_report = _find_passed_artifact(
        split_root, "leakage_audit.json", config_hash
    )
    if leakage_report["split_hash"] is None:
        raise ValueError("split audit has no split hash")
    return feature_directory, feature_report, split_directory, leakage_report


def _frame_columns() -> list[str]:
    columns = [
        "archive_id",
        "collection_day",
        "session_id",
        "scientific_role",
        "target_roi",
        "video_frame_index",
        "capture_monotonic_relative_s",
        "reference_force_raw_N",
        "reference_force_corrected_N",
        "force_valid",
        "motion_phase",
        "cycle_id",
        "cycle_index",
        "speed_mm_min",
        "displacement_mm",
        "optical_valid",
    ]
    for roi in range(1, 10):
        columns.extend(
            [
                f"roi{roi}_positive_delta_sum",
                f"roi{roi}_signed_delta_v_sum",
                f"roi{roi}_signed_delta_v_mad",
                f"roi{roi}_area",
            ]
        )
    return columns


def _load_frames(feature_directory: Path) -> pd.DataFrame:
    paths = sorted((feature_directory / "sessions").glob("archive_id=*/*.parquet"))
    if len(paths) != 245:
        raise ValueError(f"expected 245 session Parquet files; found {len(paths)}")
    columns = _frame_columns()
    frames = [pd.read_parquet(path, columns=columns) for path in paths]
    result = pd.concat(frames, ignore_index=True)
    if result["session_id"].nunique() != 245:
        raise ValueError("feature dataset does not contain 245 unique sessions")
    return result


def _no_contact_noise(
    frames: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[int, float], dict[int, float]]:
    no_contact = frames[
        (frames["scientific_role"] == "no_contact") & frames["optical_valid"]
    ]
    session_rows: list[dict[str, Any]] = []
    for (session_id, roi), group in (
        (
            (session_id, roi),
            session_group,
        )
        for session_id, session_group in no_contact.groupby("session_id", sort=True)
        for roi in range(1, 10)
    ):
        positive = group[f"roi{roi}_positive_delta_sum"]
        signed = group[f"roi{roi}_signed_delta_v_sum"]
        session_rows.append(
            {
                "session_id": session_id,
                "roi": roi,
                "frame_count": len(group),
                "positive_floor_median": float(positive.median()),
                "signed_median": float(signed.median()),
                "signed_mad": _mad(signed),
                "signed_p95_absolute": float(np.nanpercentile(np.abs(signed), 95)),
            }
        )
    session = pd.DataFrame(session_rows)
    aggregate = (
        session.groupby("roi", as_index=False)
        .agg(
            session_count=("session_id", "nunique"),
            positive_floor=("positive_floor_median", "median"),
            signed_noise_mad=("signed_mad", "median"),
            signed_noise_mad_min=("signed_mad", "min"),
            signed_noise_mad_max=("signed_mad", "max"),
        )
        .sort_values("roi")
    )
    floors = {
        int(row.roi): float(row.positive_floor)
        for row in aggregate.itertuples(index=False)
    }
    noise = {
        int(row.roi): float(row.signed_noise_mad)
        for row in aggregate.itertuples(index=False)
    }
    return session, aggregate, floors, noise


def _add_corrected_lights(
    frames: pd.DataFrame, floors: Mapping[int, float]
) -> pd.DataFrame:
    result = frames.copy()
    for roi in range(1, 10):
        result[f"light_{roi}"] = np.maximum(
            pd.to_numeric(result[f"roi{roi}_positive_delta_sum"], errors="coerce")
            - floors[roi],
            0.0,
        )
    roi_values = result["target_roi"].fillna(0).astype(int).to_numpy()
    target_light = np.full(len(result), np.nan, dtype=float)
    target_light_per_pixel = np.full(len(result), np.nan, dtype=float)
    for roi in range(1, 10):
        mask = roi_values == roi
        target_light[mask] = result.loc[mask, f"light_{roi}"]
        target_light_per_pixel[mask] = (
            result.loc[mask, f"light_{roi}"] / result.loc[mask, f"roi{roi}_area"]
        )
    result["target_light"] = target_light
    result["target_light_per_pixel"] = target_light_per_pixel
    result["total_light"] = result[[f"light_{roi}" for roi in range(1, 10)]].sum(
        axis=1
    )
    return result


def _force_bins(values: pd.Series, width: float) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return np.floor(numeric / width) * width + width / 2.0


def _session_binned_response(
    frames: pd.DataFrame, bin_width: float
) -> pd.DataFrame:
    usable = frames[
        frames["optical_valid"]
        & frames["force_valid"]
        & frames["reference_force_corrected_N"].notna()
    ].copy()
    usable["force_bin_N"] = _force_bins(
        usable["reference_force_corrected_N"], bin_width
    )
    usable = usable[
        (usable["force_bin_N"] >= bin_width / 2)
        & (usable["force_bin_N"] <= 3.0 - bin_width / 2 + 1e-9)
    ]
    return (
        usable.groupby(
            [
                "archive_id",
                "collection_day",
                "session_id",
                "target_roi",
                "motion_phase",
                "speed_mm_min",
                "displacement_mm",
                "force_bin_N",
            ],
            dropna=False,
            as_index=False,
        )
        .agg(
            frame_count=("video_frame_index", "count"),
            median_force_N=("reference_force_corrected_N", "median"),
            median_light=("target_light", "median"),
            median_light_per_pixel=("target_light_per_pixel", "median"),
        )
    )


def _bootstrap_initial_slope(
    per_session: pd.DataFrame, seed: int, replicates: int = 500
) -> tuple[float | None, float | None]:
    sessions = sorted(per_session["session_id"].unique())
    if len(sessions) < 3:
        return None, None
    rng = np.random.default_rng(seed)
    slopes: list[float] = []
    for _ in range(replicates):
        selected = rng.choice(sessions, size=len(sessions), replace=True)
        sampled = pd.concat(
            [per_session[per_session["session_id"] == session] for session in selected],
            ignore_index=True,
        )
        response = sampled.groupby("force_bin_N", as_index=False)["median_light"].median()
        initial = response[response["force_bin_N"] <= 1.0]
        if len(initial) >= 3:
            slopes.append(float(np.polyfit(initial["force_bin_N"], initial["median_light"], 1)[0]))
    if not slopes:
        return None, None
    return float(np.percentile(slopes, 2.5)), float(np.percentile(slopes, 97.5))


def _plateau_and_sensitivity(
    session_bins: pd.DataFrame,
    noise: Mapping[int, float],
    *,
    margin_fraction: float,
    seed: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, float | None]:
    source = session_bins[
        (session_bins["archive_id"] == "automated")
        & (session_bins["motion_phase"] == "pressing_down")
        & np.isclose(session_bins["speed_mm_min"], 200.0, atol=1.0)
    ]
    response_rows: list[dict[str, Any]] = []
    slope_rows: list[dict[str, Any]] = []
    plateau_rows: list[dict[str, Any]] = []
    safe_limits: list[float] = []
    for roi in range(1, 10):
        roi_sessions = source[source["target_roi"] == roi]
        aggregate = (
            roi_sessions.groupby("force_bin_N", as_index=False)
            .agg(
                session_count=("session_id", "nunique"),
                light=("median_light", "median"),
                light_per_pixel=("median_light_per_pixel", "median"),
                q25=("median_light", lambda values: float(np.percentile(values, 25))),
                q75=("median_light", lambda values: float(np.percentile(values, 75))),
            )
            .sort_values("force_bin_N")
        )
        aggregate = aggregate[aggregate["session_count"] >= 3].copy()
        for row in aggregate.itertuples(index=False):
            response_rows.append(
                {
                    "roi": roi,
                    "force_bin_N": float(row.force_bin_N),
                    "session_count": int(row.session_count),
                    "median_light": float(row.light),
                    "median_light_per_pixel": float(row.light_per_pixel),
                    "q25_light": float(row.q25),
                    "q75_light": float(row.q75),
                }
            )
        if len(aggregate) < 4:
            plateau_rows.append(
                {
                    "roi": roi,
                    "qualification": "exploratory-insufficient",
                    "reason": "fewer_than_four_supported_bins",
                    "initial_sensitivity_light_per_N": None,
                    "initial_sensitivity_ci_low": None,
                    "initial_sensitivity_ci_high": None,
                    "knee_N": None,
                    "safe_limit_N": None,
                    "monotonic_gate_pass": False,
                }
            )
            continue
        x = aggregate["force_bin_N"].to_numpy(dtype=float)
        y = aggregate["light"].to_numpy(dtype=float)
        monotonic_y = IsotonicRegression(increasing=True).fit_transform(x, y)
        initial_mask = x <= 1.0
        initial_slope = (
            float(np.polyfit(x[initial_mask], monotonic_y[initial_mask], 1)[0])
            if initial_mask.sum() >= 3
            else float((monotonic_y[1] - monotonic_y[0]) / (x[1] - x[0]))
        )
        ci_low, ci_high = _bootstrap_initial_slope(
            roi_sessions, seed=seed + roi, replicates=500
        )
        slopes = np.diff(monotonic_y) / np.diff(x)
        for index, slope in enumerate(slopes):
            slope_rows.append(
                {
                    "roi": roi,
                    "force_from_N": float(x[index]),
                    "force_to_N": float(x[index + 1]),
                    "local_sensitivity_light_per_N": float(slope),
                    "fraction_initial_sensitivity": float(slope / initial_slope)
                    if initial_slope > 0
                    else None,
                }
            )
        low_sensitivity = slopes < 0.20 * initial_slope if initial_slope > 0 else np.ones_like(slopes, dtype=bool)
        knee: float | None = None
        for index in range(max(0, len(low_sensitivity) - 2)):
            if bool(np.all(low_sensitivity[index : index + 3])):
                knee = float(x[index + 1])
                break
        covers_nominal = float(x.max()) >= 3.0 - 0.125 - 1e-9
        safe_limit = (
            max(0.0, knee * (1.0 - margin_fraction))
            if knee is not None
            else 3.0 if covers_nominal else None
        )
        raw_decreases = np.diff(y)
        three_bin_reversal = any(
            bool(np.all(raw_decreases[index : index + 3] < -3.0 * noise[roi]))
            for index in range(max(0, len(raw_decreases) - 2))
        )
        bootstrap_gate = ci_low is not None and ci_low > -0.05 * max(initial_slope, 0.0)
        monotonic_gate = initial_slope > 0 and bootstrap_gate and not three_bin_reversal
        if safe_limit is not None:
            safe_limits.append(float(safe_limit))
        fitted_initial = np.polyval(
            np.polyfit(x[initial_mask], y[initial_mask], 1), x
        ) if initial_mask.sum() >= 2 else np.full_like(y, np.nan)
        full_span = float(np.max(y) - np.min(y))
        nonlinearity = (
            float(np.max(np.abs(y - fitted_initial)) / full_span * 100.0)
            if full_span > 0 and np.isfinite(fitted_initial).all()
            else None
        )
        plateau_rows.append(
            {
                "roi": roi,
                "qualification": "primary" if safe_limit is not None else "exploratory-insufficient",
                "reason": "knee_detected" if knee is not None else "response_supported_through_3N" if covers_nominal else "coverage_below_3N_without_knee",
                "initial_sensitivity_light_per_N": initial_slope,
                "initial_sensitivity_ci_low": ci_low,
                "initial_sensitivity_ci_high": ci_high,
                "knee_N": knee,
                "safe_limit_N": safe_limit,
                "nonlinearity_percent_full_scale": nonlinearity,
                "three_consecutive_bin_reversal": three_bin_reversal,
                "monotonic_gate_pass": monotonic_gate,
            }
        )
    common_fmax = min([3.0, *safe_limits]) if len(safe_limits) == 9 else None
    return (
        pd.DataFrame(response_rows),
        pd.DataFrame(slope_rows),
        pd.DataFrame(plateau_rows),
        common_fmax,
    )


def _repeatability(session_bins: pd.DataFrame) -> pd.DataFrame:
    source = session_bins[
        (session_bins["archive_id"] == "automated")
        & session_bins["motion_phase"].isin(["pressing_down", "retracting"])
    ]
    return (
        source.groupby(
            ["target_roi", "speed_mm_min", "motion_phase", "force_bin_N"],
            as_index=False,
        )
        .agg(
            session_count=("session_id", "nunique"),
            median_light=("median_light", "median"),
            between_session_sd=("median_light", "std"),
            between_session_mad=("median_light", _mad),
        )
        .rename(columns={"target_roi": "roi"})
    )


def _hysteresis(session_bins: pd.DataFrame) -> pd.DataFrame:
    source = session_bins[
        (session_bins["archive_id"] == "automated")
        & session_bins["motion_phase"].isin(["pressing_down", "retracting"])
    ]
    pivot = source.pivot_table(
        index=[
            "session_id",
            "target_roi",
            "speed_mm_min",
            "displacement_mm",
            "force_bin_N",
        ],
        columns="motion_phase",
        values="median_light",
        aggfunc="median",
    ).reset_index()
    if "pressing_down" not in pivot or "retracting" not in pivot:
        return pd.DataFrame()
    paired = pivot.dropna(subset=["pressing_down", "retracting"]).copy()
    paired["loading_minus_unloading_light"] = (
        paired["pressing_down"] - paired["retracting"]
    )
    return (
        paired.groupby(["target_roi", "speed_mm_min", "force_bin_N"], as_index=False)
        .agg(
            session_count=("session_id", "nunique"),
            median_loading_light=("pressing_down", "median"),
            median_unloading_light=("retracting", "median"),
            median_hysteresis_light=("loading_minus_unloading_light", "median"),
            max_abs_hysteresis_light=(
                "loading_minus_unloading_light",
                lambda values: float(np.max(np.abs(values))),
            ),
        )
        .rename(columns={"target_roi": "roi"})
    )


def _cross_talk(frames: pd.DataFrame, bin_width: float) -> pd.DataFrame:
    source = frames[
        (frames["scientific_role"] == "model_primary")
        & frames["optical_valid"]
        & frames["force_valid"]
        & frames["reference_force_corrected_N"].between(0.0, 3.0)
    ].copy()
    source["force_bin_N"] = _force_bins(source["reference_force_corrected_N"], bin_width)
    rows: list[dict[str, Any]] = []
    for measured_roi in range(1, 10):
        piece = source[
            ["session_id", "target_roi", "force_bin_N", "target_light", "total_light", f"light_{measured_roi}"]
        ].copy()
        piece["measured_roi"] = measured_roi
        piece["off_to_target_ratio"] = np.where(
            piece["target_light"] > 0,
            piece[f"light_{measured_roi}"] / piece["target_light"],
            np.nan,
        )
        piece["share_total"] = np.where(
            piece["total_light"] > 0,
            piece[f"light_{measured_roi}"] / piece["total_light"],
            np.nan,
        )
        session = piece.groupby(
            ["session_id", "target_roi", "measured_roi", "force_bin_N"], as_index=False
        ).agg(
            response=(f"light_{measured_roi}", "median"),
            off_to_target_ratio=("off_to_target_ratio", "median"),
            share_total=("share_total", "median"),
        )
        rows.extend(session.to_dict("records"))
    session_long = pd.DataFrame(rows)
    return session_long.groupby(
        ["target_roi", "measured_roi", "force_bin_N"], as_index=False
    ).agg(
        session_count=("session_id", "nunique"),
        median_response=("response", "median"),
        median_off_to_target_ratio=("off_to_target_ratio", "median"),
        median_share_total=("share_total", "median"),
    )


def _drift(frames: pd.DataFrame) -> pd.DataFrame:
    source = frames[
        (frames["scientific_role"] == "no_contact")
        & frames["optical_valid"]
        & frames["capture_monotonic_relative_s"].notna()
    ]
    rows: list[dict[str, Any]] = []
    for session_id, group in source.groupby("session_id", sort=True):
        time_s = group["capture_monotonic_relative_s"].to_numpy(dtype=float)
        for roi in range(1, 10):
            light = group[f"light_{roi}"].to_numpy(dtype=float)
            valid = np.isfinite(time_s) & np.isfinite(light)
            if valid.sum() < 10 or np.ptp(time_s[valid]) <= 0:
                continue
            slope_per_s = float(np.polyfit(time_s[valid], light[valid], 1)[0])
            rows.append(
                {
                    "session_id": session_id,
                    "roi": roi,
                    "frame_count": int(valid.sum()),
                    "duration_s": float(np.ptp(time_s[valid])),
                    "drift_light_per_min": slope_per_s * 60.0,
                }
            )
    return pd.DataFrame(rows)


def _relaxation_recovery(frames: pd.DataFrame) -> pd.DataFrame:
    source = frames[
        (frames["archive_id"] == "automated")
        & frames["optical_valid"]
        & frames["motion_phase"].isin(["holding", "inter_cycle_dwell"])
    ].sort_values(["session_id", "cycle_index", "capture_monotonic_relative_s"])
    rows: list[dict[str, Any]] = []
    for keys, group in source.groupby(
        ["session_id", "target_roi", "cycle_index", "motion_phase"], dropna=False
    ):
        if len(group) < 2:
            continue
        first = group.iloc[0]
        last = group.iloc[-1]
        rows.append(
            {
                "session_id": keys[0],
                "roi": int(keys[1]),
                "cycle_index": _finite(keys[2]),
                "phase": keys[3],
                "frame_count": len(group),
                "duration_s": float(
                    last["capture_monotonic_relative_s"]
                    - first["capture_monotonic_relative_s"]
                ),
                "light_change": float(last["target_light"] - first["target_light"]),
                "force_change_N": (
                    float(
                        last["reference_force_corrected_N"]
                        - first["reference_force_corrected_N"]
                    )
                    if pd.notna(last["reference_force_corrected_N"])
                    and pd.notna(first["reference_force_corrected_N"])
                    else None
                ),
            }
        )
    return pd.DataFrame(rows)


def _approximate_lag(frames: pd.DataFrame) -> pd.DataFrame:
    source = frames[
        (frames["archive_id"] == "automated")
        & frames["optical_valid"]
        & frames["force_valid"]
        & frames["capture_monotonic_relative_s"].notna()
    ]
    rows: list[dict[str, Any]] = []
    lag_grid_ms = np.arange(0, 301, 10)
    for session_id, group in source.groupby("session_id", sort=True):
        group = group.sort_values("capture_monotonic_relative_s")
        t = group["capture_monotonic_relative_s"].to_numpy(dtype=float)
        force = group["reference_force_corrected_N"].to_numpy(dtype=float)
        light = group["total_light"].to_numpy(dtype=float)
        valid = np.isfinite(t) & np.isfinite(force) & np.isfinite(light)
        t, force, light = t[valid], force[valid], light[valid]
        if len(t) < 30 or np.ptp(t) <= 1.0:
            continue
        scores: list[float] = []
        for lag_ms in lag_grid_ms:
            query = t - lag_ms / 1000.0
            inside = (query >= t[0]) & (query <= t[-1])
            aligned = np.interp(query[inside], t, force)
            optical = light[inside]
            score = (
                float(np.corrcoef(optical, aligned)[0, 1])
                if len(aligned) >= 20 and np.std(aligned) > 0 and np.std(optical) > 0
                else float("nan")
            )
            scores.append(score)
        if not np.isfinite(scores).any():
            continue
        best = int(np.nanargmax(scores))
        rows.append(
            {
                "session_id": session_id,
                "target_roi": int(group["target_roi"].iloc[0]),
                "best_lag_ms": int(lag_grid_ms[best]),
                "best_correlation": float(scores[best]),
                "qualification": "exploratory-training-fold-rule-pending",
            }
        )
    return pd.DataFrame(rows)


def _write_table(directory: Path, name: str, frame: pd.DataFrame) -> None:
    frame.to_parquet(directory / f"{name}.parquet", index=False)
    frame.to_csv(directory / f"{name}.csv", index=False, lineterminator="\n")


def _script_hash() -> str:
    return code_hash(
        (
            "scripts/characterize_optical_sensor.py",
            "scripts/live_sensor_common.py",
            "config/live_sensor_study.json",
        )
    )


def run_characterization(config_path: str | Path, *, dry_run: bool = False) -> int:
    started = time.perf_counter()
    config, config_hash, selected_config = load_study_config(config_path)
    feature_directory, feature_report, split_directory, leakage_report = _discover_inputs(
        config, config_hash
    )
    script_hash = _script_hash()
    identity = {
        "study_config_hash": config_hash,
        "source_manifest_hash": feature_report["source_manifest_hash"],
        "feature_spec_hash": feature_report["feature_spec_hash"],
        "split_hash": leakage_report["split_hash"],
        "code_hash": script_hash,
    }
    run_id = make_run_id("characterization", identity)
    output_root = resolve_project_path(
        "analysis_outputs/live_sensor_study/characterization", writable=True
    )
    final_directory = output_root / run_id
    if dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "run_id": run_id,
                    "feature_store": str(feature_directory),
                    "split_manifest": str(split_directory),
                    "reported_rows": feature_report["total_rows"],
                    "planned_outputs": [
                        "no_contact_session_noise",
                        "no_contact_summary",
                        "session_binned_response",
                        "plateau_summary",
                        "local_sensitivity",
                        "repeatability",
                        "hysteresis",
                        "cross_talk",
                        "no_contact_drift",
                        "relaxation_recovery",
                        "approximate_lag",
                        "range_decision.json",
                    ],
                    "planned_output": str(final_directory),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    input_identity_hash = canonical_json_hash(identity)
    existing = final_directory / "run_manifest.json"
    if existing.exists() and load_json_object(existing).get("input_identity_hash") == input_identity_hash:
        report_path = final_directory / "characterization_summary.json"
        print(f"run_id={run_id}")
        print("reused=true")
        print(f"report={report_path}")
        return 0 if load_json_object(report_path).get("gate_pass") else 2

    temporary = create_temporary_run_directory(output_root, run_id)
    logger = RunLogger(temporary / "events.jsonl", COMMAND_NAME, run_id)
    try:
        logger.emit("INFO", "run_started", feature_store=str(feature_directory))
        frames = _load_frames(feature_directory)
        no_contact_session, no_contact_summary, floors, noise = _no_contact_noise(frames)
        frames = _add_corrected_lights(frames, floors)
        bin_width = float(config["force_study"]["force_bin_width_N"])
        session_bins = _session_binned_response(frames, bin_width)
        # The master plan defines this numeric value as a 10% fraction; the
        # historical config key retains its `_N` suffix for hash stability.
        margin_fraction = float(config["force_study"]["plateau_safety_margin_N"])
        response, local_sensitivity, plateau, common_fmax = _plateau_and_sensitivity(
            session_bins,
            noise,
            margin_fraction=margin_fraction,
            seed=int(config["seeds"][0]),
        )
        repeatability = _repeatability(session_bins)
        hysteresis = _hysteresis(session_bins)
        cross_talk = _cross_talk(frames, bin_width)
        drift = _drift(frames)
        relaxation = _relaxation_recovery(frames)
        lag = _approximate_lag(frames)

        for name, table in (
            ("no_contact_session_noise", no_contact_session),
            ("no_contact_summary", no_contact_summary),
            ("session_binned_response", session_bins),
            ("session_balanced_response", response),
            ("plateau_summary", plateau),
            ("local_sensitivity", local_sensitivity),
            ("repeatability", repeatability),
            ("hysteresis", hysteresis),
            ("cross_talk", cross_talk),
            ("no_contact_drift", drift),
            ("relaxation_recovery", relaxation),
            ("approximate_lag", lag),
        ):
            _write_table(temporary, name, table)

        sensitivity_proxy = plateau[["roi", "initial_sensitivity_light_per_N"]].copy()
        sensitivity_proxy["signed_noise_mad"] = sensitivity_proxy["roi"].map(noise)
        sensitivity_proxy["noise_equivalent_force_N"] = (
            sensitivity_proxy["signed_noise_mad"]
            / sensitivity_proxy["initial_sensitivity_light_per_N"]
        )
        sensitivity_proxy["conservative_3x_noise_force_N"] = (
            3.0 * sensitivity_proxy["noise_equivalent_force_N"]
        )
        sensitivity_proxy["qualification"] = "noise-limited-proxy-not-true-resolution"
        _write_table(temporary, "noise_equivalent_force_proxy", sensitivity_proxy)

        plateau_complete = len(plateau) == 9 and plateau["safe_limit_N"].notna().all()
        monotonic_complete = len(plateau) == 9 and plateau["monotonic_gate_pass"].all()
        gates = {
            "all_nine_roi_no_contact_noise": len(no_contact_summary) == 9,
            "all_nine_roi_plateau_decisions": bool(plateau_complete),
            "all_nine_roi_data_monotonicity": bool(monotonic_complete),
            "complete_cross_talk_matrix": cross_talk[
                ["target_roi", "measured_roi"]
            ].drop_duplicates().shape[0]
            == 81,
            "automated_hysteresis_reported": not hysteresis.empty,
            "relaxation_recovery_reported": not relaxation.empty,
            "true_resolution_not_claimed": True,
            "creep_not_claimed": True,
        }
        gate_pass = all(gates.values())
        plateau_records = (
            plateau.astype(object).where(pd.notna(plateau), None).to_dict("records")
        )
        range_decision = {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now_iso(),
            "study_config_hash": config_hash,
            "source_manifest_hash": feature_report["source_manifest_hash"],
            "feature_spec_hash": feature_report["feature_spec_hash"],
            "split_hash": leakage_report["split_hash"],
            "force_bin_width_N": bin_width,
            "plateau_initial_sensitivity_fraction": 0.20,
            "plateau_consecutive_bins": 3,
            "minimum_sessions_per_bin": 3,
            "safety_margin_fraction": margin_fraction,
            "per_roi": plateau_records,
            "provisional_common_fmax_N": _finite(common_fmax),
            "frozen_before_outer_predictions": True,
            "gate_pass": bool(plateau_complete and monotonic_complete),
        }
        atomic_write_json(temporary / "range_decision.json", range_decision)
        summary = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": utc_now_iso(),
            "study_config_hash": config_hash,
            "source_manifest_hash": feature_report["source_manifest_hash"],
            "feature_store_report_hash": sha256_file(
                feature_directory / "reconciliation_report.json"
            ),
            "split_hash": leakage_report["split_hash"],
            "frame_count": len(frames),
            "session_count": frames["session_id"].nunique(),
            "no_contact_session_count": no_contact_session["session_id"].nunique(),
            "automated_session_count": frames.loc[
                frames["archive_id"] == "automated", "session_id"
            ].nunique(),
            "provisional_common_fmax_N": _finite(common_fmax),
            "gate_checks": gates,
            "gate_pass": gate_pass,
            "qualification": {
                "true_force_resolution": "not established",
                "creep": "not estimable from current protocol",
                "loaded_hold": "short-term fixed-displacement relaxation only",
                "approximate_lag": "exploratory; final lag must be selected inside training folds",
            },
        }
        atomic_write_json(temporary / "characterization_summary.json", summary)
        logger.emit(
            "INFO" if gate_pass else "ERROR",
            "characterization_gate_finished",
            gate_pass=gate_pass,
            gates=gates,
            provisional_common_fmax_N=_finite(common_fmax),
        )
        logger.close()
        output_hashes = output_file_hashes(temporary, exclude=("run_manifest.json",))
        run_manifest = {
            "schema_version": SCHEMA_VERSION,
            "command": COMMAND_NAME,
            "argv": sys.argv,
            "run_id": run_id,
            "created_at": utc_now_iso(),
            "completed_at": utc_now_iso(),
            "elapsed_s": elapsed_seconds(started),
            "config_path": str(selected_config),
            "study_config_hash": config_hash,
            "source_manifest_hash": feature_report["source_manifest_hash"],
            "split_hash": leakage_report["split_hash"],
            "code_hash": script_hash,
            "input_identity_hash": input_identity_hash,
            "inputs": {
                "feature_store": str(feature_directory),
                "split_manifest": str(split_directory / "split_manifest.parquet"),
            },
            "outputs": output_hashes,
            "environment": platform_manifest(),
            "warnings": [
                "True force resolution is not established by continuous ramps.",
                "Constant-force creep is not estimable from 0.5 s fixed-displacement holds.",
            ],
            "exclusions": [],
            "gate_pass": gate_pass,
        }
        atomic_write_json(temporary / "run_manifest.json", run_manifest)
        published, reused = publish_run_directory(temporary, final_directory)
        print(f"run_id={run_id}")
        print(f"reused={str(reused).lower()}")
        print(f"report={published / 'characterization_summary.json'}")
        return 0 if gate_pass else 2
    except Exception as exc:
        logger.emit("ERROR", "run_failed", error=f"{type(exc).__name__}: {exc}")
        logger.close()
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compute source-backed range, sensitivity, repeatability, cross-talk, "
            "noise, hysteresis, short-term drift/relaxation/recovery, and lag evidence."
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return run_characterization(arguments.config, dry_run=arguments.dry_run)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
