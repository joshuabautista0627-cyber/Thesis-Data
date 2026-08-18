"""Build thesis-ready sensor-characterization tables, figures, and report data.

This script is intentionally downstream of the immutable original-frame feature
store and the preserved characterization run.  It never modifies either input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd


SCHEMA_VERSION = "2.0.0"
DEFAULT_SEED = 20260813
FORCE_BIN_WIDTH_N = 0.25
LOW_FORCE_MAX_N = 1.0
SIGNAL_FORCE_MIN_N = 0.75
SIGNAL_FORCE_MAX_N = 1.25
DETECTION_FPR = 0.01
DETECTION_RECALL = 0.95
PLOT_DPI = 220
MANUAL_LAG_MIN_MS = -500
MANUAL_LAG_MAX_MS = 500
MANUAL_LAG_STEP_MS = 25
PLATEAU_TAIL_FRACTION = 0.95
PLATEAU_TAIL_SPAN_FRACTION = 0.10


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mad(values: Iterable[float]) -> float:
    array = np.asarray(list(values), dtype=float)
    array = array[np.isfinite(array)]
    if not len(array):
        return float("nan")
    centre = np.median(array)
    return float(np.median(np.abs(array - centre)))


def q25(values: Iterable[float]) -> float:
    return float(np.nanpercentile(np.asarray(list(values), dtype=float), 25))


def q75(values: Iterable[float]) -> float:
    return float(np.nanpercentile(np.asarray(list(values), dtype=float), 75))


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            value,
            handle,
            indent=2,
            sort_keys=True,
            allow_nan=False,
            default=lambda item: item.item() if isinstance(item, np.generic) else str(item),
        )
        handle.write("\n")


def write_table(directory: Path, name: str, frame: pd.DataFrame) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{name}.csv"
    parquet_path = directory / f"{name}.parquet"
    frame.to_csv(csv_path, index=False, lineterminator="\n")
    frame.to_parquet(parquet_path, index=False)
    return {csv_path.name: sha256_file(csv_path), parquet_path.name: sha256_file(parquet_path)}


def force_bins(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return np.floor(numeric / FORCE_BIN_WIDTH_N) * FORCE_BIN_WIDTH_N + FORCE_BIN_WIDTH_N / 2


def feature_columns() -> list[str]:
    columns = [
        "archive_id",
        "collection_day",
        "test_group",
        "session_id",
        "scientific_role",
        "video_frame_index",
        "capture_monotonic_relative_s",
        "target_roi",
        "reference_force_raw_N",
        "reference_force_corrected_N",
        "cycle_force_zero_offset_N",
        "force_valid",
        "motion_phase",
        "cycle_id",
        "cycle_index",
        "speed_mm_min",
        "displacement_mm",
        "synchronization_offset_ms",
        "optical_valid",
        "exclusion_reason",
        "baseline_id",
        "baseline_hash",
        "camera_exposure",
        "camera_gain",
        "camera_brightness",
        "camera_contrast",
        "camera_saturation",
        "camera_sharpness",
        "camera_focus",
    ]
    for roi in range(1, 10):
        columns.extend(
            [
                f"roi{roi}_area",
                f"roi{roi}_signed_delta_v_sum",
                f"roi{roi}_signed_delta_v_mad",
                f"roi{roi}_positive_delta_sum",
                f"roi{roi}_positive_delta_mean",
            ]
        )
    return columns


def load_features(feature_store: Path) -> pd.DataFrame:
    paths = sorted((feature_store / "sessions").glob("archive_id=*/*.parquet"))
    if len(paths) != 245:
        raise ValueError(f"expected 245 feature-store session files, found {len(paths)}")
    columns = feature_columns()
    frames = pd.concat([pd.read_parquet(path, columns=columns) for path in paths], ignore_index=True)
    if len(frames) != 179176 or frames["session_id"].nunique() != 245:
        raise ValueError("feature-store frame/session reconciliation differs from the master plan")
    if not frames["optical_valid"].all():
        raise ValueError("feature store contains optical-invalid rows; stop condition reached")
    return frames


def add_lights(frames: pd.DataFrame, floors: dict[int, float]) -> pd.DataFrame:
    result = frames.copy()
    for roi in range(1, 10):
        raw = pd.to_numeric(result[f"roi{roi}_positive_delta_sum"], errors="coerce")
        result[f"light_{roi}"] = np.maximum(raw - floors[roi], 0.0)
        result[f"light_per_pixel_{roi}"] = result[f"light_{roi}"] / result[f"roi{roi}_area"]
    roi_values = result["target_roi"].fillna(0).astype(int).to_numpy()
    target = np.full(len(result), np.nan)
    target_per_pixel = np.full(len(result), np.nan)
    target_signed = np.full(len(result), np.nan)
    for roi in range(1, 10):
        mask = roi_values == roi
        target[mask] = result.loc[mask, f"light_{roi}"]
        target_per_pixel[mask] = result.loc[mask, f"light_per_pixel_{roi}"]
        target_signed[mask] = result.loc[mask, f"roi{roi}_signed_delta_v_sum"]
    result["target_light"] = target
    result["target_light_per_pixel"] = target_per_pixel
    result["target_signed_light"] = target_signed
    result["total_light"] = result[[f"light_{roi}" for roi in range(1, 10)]].sum(axis=1)
    return result


def coverage_tables(frames: pd.DataFrame) -> dict[str, pd.DataFrame]:
    source = frames.copy()
    # Some automatic pre-roll rows expose a planned displacement before the
    # matching speed is populated.  Preserve only paired condition values so a
    # session cannot be assigned by independently taking two different rows.
    source["condition_speed_mm_min"] = source["speed_mm_min"].where(
        source["speed_mm_min"].notna() & source["displacement_mm"].notna()
    )
    source["condition_displacement_mm"] = source["displacement_mm"].where(
        source["speed_mm_min"].notna() & source["displacement_mm"].notna()
    )
    session = (
        source.groupby(
            ["archive_id", "collection_day", "scientific_role", "session_id", "test_group", "target_roi"],
            dropna=False,
            as_index=False,
        )
        .agg(
            frame_count=("video_frame_index", "size"),
            force_valid_frames=("force_valid", "sum"),
            min_force_N=("reference_force_corrected_N", "min"),
            max_force_N=("reference_force_corrected_N", "max"),
            duration_s=("capture_monotonic_relative_s", lambda x: float(x.max() - x.min())),
            speed_mm_min=("condition_speed_mm_min", "first"),
            displacement_mm=("condition_displacement_mm", "first"),
            planned_cycles=("cycle_index", lambda x: int(pd.to_numeric(x, errors="coerce").loc[lambda s: s.gt(0)].nunique())),
            baseline_id=("baseline_id", "first"),
            baseline_hash=("baseline_hash", "first"),
            camera_exposure=("camera_exposure", "first"),
            camera_gain=("camera_gain", "first"),
            camera_brightness=("camera_brightness", "first"),
            camera_contrast=("camera_contrast", "first"),
            camera_saturation=("camera_saturation", "first"),
            camera_sharpness=("camera_sharpness", "first"),
            camera_focus=("camera_focus", "first"),
        )
    )
    manual = session[session["archive_id"].eq("manual")].copy()
    automatic = session[session["archive_id"].eq("automated")].copy()
    automatic["displacement_mm_abs"] = automatic["displacement_mm"].abs()
    automatic_condition = (
        automatic.groupby(
            ["target_roi", "speed_mm_min", "displacement_mm_abs"], as_index=False
        )
        .agg(
            independent_sessions=("session_id", "nunique"),
            frames=("frame_count", "sum"),
            collection_days=("collection_day", "nunique"),
            ten_cycle_sessions=("planned_cycles", lambda x: int(pd.Series(x).eq(10).sum())),
            twenty_cycle_sessions=("planned_cycles", lambda x: int(pd.Series(x).eq(20).sum())),
        )
        .sort_values(["target_roi", "speed_mm_min", "displacement_mm_abs"])
    )
    return {
        "coverage_sessions": session,
        "coverage_manual_sessions": manual,
        "coverage_automatic_sessions": automatic,
        "coverage_automatic_conditions": automatic_condition,
    }


def no_contact_tables(frames: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], dict[int, float]]:
    source = frames[frames["scientific_role"].eq("no_contact")].copy()
    rows: list[dict[str, Any]] = []
    for session_id, group in source.groupby("session_id", sort=True):
        duration = float(group["capture_monotonic_relative_s"].max() - group["capture_monotonic_relative_s"].min())
        for roi in range(1, 10):
            signed = group[f"roi{roi}_signed_delta_v_sum"].to_numpy(dtype=float)
            positive = group[f"roi{roi}_positive_delta_sum"].to_numpy(dtype=float)
            time_s = group["capture_monotonic_relative_s"].to_numpy(dtype=float)
            valid = np.isfinite(time_s) & np.isfinite(signed)
            signed_slope_per_min = (
                float(np.polyfit(time_s[valid], signed[valid], 1)[0] * 60.0)
                if valid.sum() >= 10 and np.ptp(time_s[valid]) > 0
                else float("nan")
            )
            rows.append(
                {
                    "session_id": session_id,
                    "roi": roi,
                    "baseline_id": group["baseline_id"].iloc[0],
                    "camera_sharpness": finite(group["camera_sharpness"].iloc[0]),
                    "frame_count": len(group),
                    "duration_s": duration,
                    "positive_floor_median": float(np.median(positive)),
                    "signed_median": float(np.median(signed)),
                    "signed_mad": mad(signed),
                    "signed_p95_absolute": float(np.percentile(np.abs(signed), 95)),
                    "signed_drift_light_per_min": signed_slope_per_min,
                    "signed_total_change": signed_slope_per_min * duration / 60.0,
                }
            )
    session = pd.DataFrame(rows)
    summary = (
        session.groupby("roi", as_index=False)
        .agg(
            independent_sessions=("session_id", "nunique"),
            no_contact_frames=("frame_count", "sum"),
            positive_floor=("positive_floor_median", "median"),
            signed_noise_mad=("signed_mad", "median"),
            signed_noise_mad_min=("signed_mad", "min"),
            signed_noise_mad_max=("signed_mad", "max"),
            median_abs_drift_light_per_min=("signed_drift_light_per_min", lambda x: float(np.median(np.abs(x)))),
            max_abs_drift_light_per_min=("signed_drift_light_per_min", lambda x: float(np.max(np.abs(x)))),
        )
        .sort_values("roi")
    )
    floors = {int(row.roi): float(row.positive_floor) for row in summary.itertuples(index=False)}
    return {"no_contact_session": session, "no_contact_summary": summary}, floors


def manual_response_tables(frames: pd.DataFrame, noise_summary: pd.DataFrame) -> dict[str, pd.DataFrame]:
    source = frames[
        frames["scientific_role"].eq("model_primary")
        & frames["force_valid"]
        & frames["reference_force_corrected_N"].notna()
    ].copy()
    source["force_bin_N"] = force_bins(source["reference_force_corrected_N"])
    source = source[source["force_bin_N"].between(FORCE_BIN_WIDTH_N / 2, 15.0)]
    session_bins = (
        source.groupby(
            ["session_id", "test_group", "target_roi", "motion_phase", "force_bin_N"], as_index=False
        )
        .agg(
            frames=("video_frame_index", "size"),
            median_raw_force_N=("reference_force_raw_N", "median"),
            median_force_N=("reference_force_corrected_N", "median"),
            median_light=("target_light", "median"),
            median_light_per_pixel=("target_light_per_pixel", "median"),
            baseline_id=("baseline_id", "first"),
            camera_exposure=("camera_exposure", "first"),
            camera_gain=("camera_gain", "first"),
            camera_brightness=("camera_brightness", "first"),
            camera_contrast=("camera_contrast", "first"),
            camera_saturation=("camera_saturation", "first"),
            camera_sharpness=("camera_sharpness", "first"),
            camera_focus=("camera_focus", "first"),
        )
        .rename(columns={"target_roi": "roi"})
    )
    response = (
        session_bins.groupby(["roi", "force_bin_N"], as_index=False)
        .agg(
            independent_sessions=("session_id", "nunique"),
            median_force_N=("median_force_N", "median"),
            median_light=("median_light", "median"),
            q25_light=("median_light", q25),
            q75_light=("median_light", q75),
            median_light_per_pixel=("median_light_per_pixel", "median"),
            q25_light_per_pixel=("median_light_per_pixel", q25),
            q75_light_per_pixel=("median_light_per_pixel", q75),
        )
        .sort_values(["roi", "force_bin_N"])
    )
    sensitivity_rows: list[dict[str, Any]] = []
    repeatability_rows: list[dict[str, Any]] = []
    local_sensitivity_rows: list[dict[str, Any]] = []
    plateau_rows: list[dict[str, Any]] = []
    for roi in range(1, 10):
        roi_bins = session_bins[session_bins["roi"].eq(roi)]
        supported = response[
            response["roi"].eq(roi)
            & response["force_bin_N"].le(LOW_FORCE_MAX_N)
            & response["independent_sessions"].ge(3)
        ]
        if len(supported) >= 3:
            raw_slope, raw_intercept = np.polyfit(supported["median_force_N"], supported["median_light"], 1)
            norm_slope, norm_intercept = np.polyfit(
                supported["median_force_N"], supported["median_light_per_pixel"], 1
            )
            fit = raw_intercept + raw_slope * supported["median_force_N"].to_numpy()
            full_span = float(supported["median_light"].max() - supported["median_light"].min())
            nonlinearity = (
                float(np.max(np.abs(supported["median_light"].to_numpy() - fit)) / full_span * 100)
                if full_span > 0
                else float("nan")
            )
            qualification = "primary"
        else:
            raw_slope = raw_intercept = norm_slope = norm_intercept = nonlinearity = float("nan")
            qualification = "exploratory-insufficient"
        sensitivity_rows.append(
            {
                "roi": roi,
                "force_interval_N": f"0-{LOW_FORCE_MAX_N:g}",
                "supported_bins": len(supported),
                "minimum_independent_sessions_per_bin": int(supported["independent_sessions"].min()) if len(supported) else 0,
                "low_force_sensitivity_light_per_N": raw_slope,
                "low_force_intercept_light": raw_intercept,
                "low_force_sensitivity_light_per_pixel_per_N": norm_slope,
                "low_force_intercept_light_per_pixel": norm_intercept,
                "nonlinearity_percent_supported_low_force_span": nonlinearity,
                "qualification": qualification,
            }
        )
        all_supported = response[
            response["roi"].eq(roi) & response["independent_sessions"].ge(3)
        ].sort_values("force_bin_N")
        for left, right in zip(
            all_supported.iloc[:-1].itertuples(index=False),
            all_supported.iloc[1:].itertuples(index=False),
            strict=True,
        ):
            bin_gap = float(right.force_bin_N - left.force_bin_N)
            force_gap = float(right.median_force_N - left.median_force_N)
            if not math.isclose(bin_gap, FORCE_BIN_WIDTH_N, rel_tol=0, abs_tol=1e-9) or force_gap <= 0:
                continue
            local_sensitivity_rows.append(
                {
                    "roi": roi,
                    "left_force_bin_N": float(left.force_bin_N),
                    "right_force_bin_N": float(right.force_bin_N),
                    "force_midpoint_N": float((left.force_bin_N + right.force_bin_N) / 2),
                    "minimum_independent_sessions": int(min(left.independent_sessions, right.independent_sessions)),
                    "local_sensitivity_light_per_N": float((right.median_light - left.median_light) / force_gap),
                    "local_sensitivity_light_per_pixel_per_N": float(
                        (right.median_light_per_pixel - left.median_light_per_pixel) / force_gap
                    ),
                    "qualification": "primary-supported-adjacent-bins",
                }
            )
        supported_force_min = finite(all_supported["force_bin_N"].min())
        supported_force_max = finite(all_supported["force_bin_N"].max())
        peak_light = finite(all_supported["median_light"].max())
        plateau_onset = float("nan")
        plateau_bins = 0
        if len(all_supported) >= 3 and peak_light is not None and peak_light > 0:
            threshold = PLATEAU_TAIL_FRACTION * peak_light
            supported_span = float(all_supported["force_bin_N"].max() - all_supported["force_bin_N"].min())
            minimum_span = PLATEAU_TAIL_SPAN_FRACTION * supported_span
            above = all_supported["median_light"].ge(threshold).to_numpy()
            bins = all_supported["force_bin_N"].to_numpy(dtype=float)
            for start in range(len(above) - 2):
                stop = start
                while stop + 1 < len(above) and above[stop + 1] and math.isclose(
                    bins[stop + 1] - bins[stop], FORCE_BIN_WIDTH_N, rel_tol=0, abs_tol=1e-9
                ):
                    stop += 1
                if above[start] and stop - start + 1 >= 3 and bins[stop] - bins[start] >= minimum_span:
                    plateau_onset = float(bins[start])
                    plateau_bins = int(stop - start + 1)
                    break
        plateau_rows.append(
            {
                "roi": roi,
                "supported_force_min_N": supported_force_min,
                "supported_force_max_N": supported_force_max,
                "supported_bins": int(len(all_supported)),
                "peak_session_balanced_light": peak_light,
                "plateau_threshold_fraction_of_peak": PLATEAU_TAIL_FRACTION,
                "minimum_plateau_force_span_fraction": PLATEAU_TAIL_SPAN_FRACTION,
                "plateau_onset_N": plateau_onset,
                "plateau_consecutive_bins": plateau_bins,
                "plateau_observation": "observed-under-frozen-rule" if np.isfinite(plateau_onset) else "not-observed-under-frozen-rule",
                "claim_boundary": "manual characterization observation; not a deployed usable range",
            }
        )
        for force_bin, group in roi_bins.groupby("force_bin_N", sort=True):
            if group["session_id"].nunique() < 3:
                continue
            spread = float(group["median_light"].std(ddof=1))
            span = float(roi_bins["median_light"].max() - roi_bins["median_light"].min())
            repeatability_rows.append(
                {
                    "roi": roi,
                    "force_bin_N": float(force_bin),
                    "independent_sessions": int(group["session_id"].nunique()),
                    "median_light": float(group["median_light"].median()),
                    "between_session_sd_light": spread,
                    "between_session_mad_light": mad(group["median_light"]),
                    "between_session_sd_percent_observed_span": spread / span * 100 if span > 0 else float("nan"),
                }
            )
    sensitivity = pd.DataFrame(sensitivity_rows)
    repeatability = pd.DataFrame(repeatability_rows)
    noise_map = dict(zip(noise_summary["roi"], noise_summary["signed_noise_mad"], strict=True))
    proxy = sensitivity[["roi", "low_force_sensitivity_light_per_N", "qualification"]].copy()
    proxy["signed_noise_mad_light"] = proxy["roi"].map(noise_map)
    positive_sensitivity = proxy["low_force_sensitivity_light_per_N"] > 0
    proxy["noise_equivalent_force_N"] = np.where(
        positive_sensitivity,
        proxy["signed_noise_mad_light"] / proxy["low_force_sensitivity_light_per_N"],
        np.nan,
    )
    proxy["conservative_3x_noise_force_N"] = 3 * proxy["noise_equivalent_force_N"]
    proxy["qualification"] = np.where(
        proxy["qualification"].eq("primary") & positive_sensitivity,
        "conditional-qualified-noise-limited-proxy",
        "exploratory-insufficient",
    )
    proxy["claim_boundary"] = "not true force resolution"
    uniformity = sensitivity.merge(
        noise_summary[["roi", "signed_noise_mad", "median_abs_drift_light_per_min"]],
        on="roi",
        how="left",
    )
    for column in ["low_force_sensitivity_light_per_N", "low_force_sensitivity_light_per_pixel_per_N", "signed_noise_mad"]:
        median = float(uniformity[column].median())
        uniformity[f"{column}_relative_to_roi_median"] = uniformity[column] / median if median else np.nan
    return {
        "manual_session_force_bins": session_bins,
        "manual_static_response": response,
        "manual_sensitivity_nonlinearity": sensitivity,
        "manual_local_sensitivity": pd.DataFrame(local_sensitivity_rows),
        "manual_plateau_observations": pd.DataFrame(plateau_rows),
        "manual_repeatability": repeatability,
        "spatial_uniformity": uniformity,
        "noise_equivalent_force_proxy": proxy,
    }


def detection_and_snr(frames: pd.DataFrame, sensitivity: pd.DataFrame) -> dict[str, pd.DataFrame]:
    no_contact = frames[frames["scientific_role"].eq("no_contact")]
    press = frames[
        frames["scientific_role"].eq("model_primary")
        & frames["force_valid"]
        & frames["reference_force_corrected_N"].between(0, 3)
    ].copy()
    press["force_bin_N"] = force_bins(press["reference_force_corrected_N"])
    threshold_rows: list[dict[str, Any]] = []
    snr_rows: list[dict[str, Any]] = []
    for roi in range(1, 10):
        null_session = (
            no_contact.groupby("session_id")[f"roi{roi}_signed_delta_v_sum"].median().dropna()
        )
        threshold = float(np.quantile(null_session, 1 - DETECTION_FPR, method="higher"))
        roi_press = press[press["target_roi"].eq(roi)]
        for force_bin, group in roi_press.groupby("force_bin_N", sort=True):
            session = group.groupby("session_id").agg(
                signed_signal=(f"roi{roi}_signed_delta_v_sum", "median"),
                positive_signal=("target_light", "median"),
            )
            if session.empty:
                continue
            recalls = (session["signed_signal"] > threshold).astype(float)
            threshold_rows.append(
                {
                    "roi": roi,
                    "force_bin_N": float(force_bin),
                    "independent_press_sessions": int(len(session)),
                    "independent_no_contact_sessions": int(len(null_session)),
                    "signed_light_threshold": threshold,
                    "empirical_recall": float(recalls.mean()),
                    "frozen_fpr_rule": DETECTION_FPR,
                    "required_recall": DETECTION_RECALL,
                    "supported": bool(len(session) >= 3),
                }
            )
        signal = roi_press[roi_press["reference_force_corrected_N"].between(SIGNAL_FORCE_MIN_N, SIGNAL_FORCE_MAX_N)]
        signal_session = signal.groupby("session_id")["target_light"].median().dropna()
        noise_mad = mad(no_contact.groupby("session_id")[f"roi{roi}_signed_delta_v_sum"].median())
        median_signal = float(signal_session.median()) if len(signal_session) else float("nan")
        snr_linear = median_signal / noise_mad if noise_mad > 0 else float("nan")
        snr_rows.append(
            {
                "roi": roi,
                "force_region_N": f"{SIGNAL_FORCE_MIN_N:g}-{SIGNAL_FORCE_MAX_N:g}",
                "independent_press_sessions": int(len(signal_session)),
                "independent_no_contact_sessions": int(no_contact["session_id"].nunique()),
                "median_contact_positive_light": median_signal,
                "dedicated_no_contact_signed_light_mad": noise_mad,
                "snr_linear": snr_linear,
                "snr_dB_amplitude_convention": 20 * math.log10(snr_linear) if snr_linear > 0 else float("nan"),
                "qualification": "primary" if len(signal_session) >= 3 else "exploratory-insufficient",
            }
        )
    detail = pd.DataFrame(threshold_rows)
    summary_rows: list[dict[str, Any]] = []
    for roi in range(1, 10):
        supported = detail[
            detail["roi"].eq(roi)
            & detail["supported"]
            & detail["empirical_recall"].ge(DETECTION_RECALL)
        ]
        minimum = float(supported["force_bin_N"].min()) if len(supported) else float("nan")
        summary_rows.append(
            {
                "roi": roi,
                "optical_detection_region_N": minimum,
                "frozen_empirical_fpr": DETECTION_FPR,
                "required_recall": DETECTION_RECALL,
                "qualification": "conditional-qualified-characterization-only" if np.isfinite(minimum) else "not-established",
                "application_threshold": False,
            }
        )
    return {
        "manual_detection_detail": detail,
        "manual_detection_summary": pd.DataFrame(summary_rows),
        "manual_snr": pd.DataFrame(snr_rows),
    }


def cross_talk(frames: pd.DataFrame) -> dict[str, pd.DataFrame]:
    source = frames[
        frames["scientific_role"].eq("model_primary")
        & frames["force_valid"]
        & frames["reference_force_corrected_N"].between(0, 5)
    ].copy()
    source["force_bin_N"] = force_bins(source["reference_force_corrected_N"])
    rows: list[dict[str, Any]] = []
    for measured_roi in range(1, 10):
        piece = source[
            ["session_id", "target_roi", "force_bin_N", "target_light", "total_light", f"light_{measured_roi}"]
        ].copy()
        piece["measured_roi"] = measured_roi
        piece["response"] = piece[f"light_{measured_roi}"]
        piece["response_share_total"] = np.where(
            piece["total_light"] > 0, piece["response"] / piece["total_light"], np.nan
        )
        piece["response_relative_target"] = np.where(
            piece["target_light"] > 0, piece["response"] / piece["target_light"], np.nan
        )
        session = piece.groupby(
            ["session_id", "target_roi", "measured_roi", "force_bin_N"], as_index=False
        ).agg(
            median_response=("response", "median"),
            median_share_total=("response_share_total", "median"),
            median_relative_target=("response_relative_target", "median"),
        )
        rows.extend(session.to_dict("records"))
    detail = (
        pd.DataFrame(rows)
        .groupby(["target_roi", "measured_roi", "force_bin_N"], as_index=False)
        .agg(
            independent_sessions=("session_id", "nunique"),
            median_response=("median_response", "median"),
            median_share_total=("median_share_total", "median"),
            median_relative_target=("median_relative_target", "median"),
        )
        .sort_values(["target_roi", "measured_roi", "force_bin_N"])
    )
    matrix = (
        detail[detail["force_bin_N"].between(SIGNAL_FORCE_MIN_N, SIGNAL_FORCE_MAX_N)]
        .groupby(["target_roi", "measured_roi"], as_index=False)
        .agg(
            independent_sessions=("independent_sessions", "max"),
            median_response=("median_response", "median"),
            median_share_total=("median_share_total", "median"),
            median_relative_target=("median_relative_target", "median"),
        )
        .sort_values(["target_roi", "measured_roi"])
    )
    return {"cross_talk_force_bins": detail, "cross_talk": matrix}


def automatic_cycle_tables(frames: pd.DataFrame) -> dict[str, pd.DataFrame]:
    source = frames[
        frames["archive_id"].eq("automated")
        & frames["force_valid"]
        & frames["cycle_id"].notna()
        & frames["cycle_index"].notna()
    ].copy()
    source["displacement_mm_abs"] = source["displacement_mm"].abs()
    source["force_bin_N"] = force_bins(source["reference_force_corrected_N"])
    movement = source[
        source["motion_phase"].isin(["pressing_down", "retracting"])
        & source["force_bin_N"].between(FORCE_BIN_WIDTH_N / 2, 6.0)
    ]
    cycle_bins = (
        movement.groupby(
            [
                "collection_day",
                "session_id",
                "target_roi",
                "speed_mm_min",
                "displacement_mm_abs",
                "cycle_index",
                "motion_phase",
                "force_bin_N",
            ],
            as_index=False,
        )
        .agg(
            frames=("video_frame_index", "size"),
            median_raw_force_N=("reference_force_raw_N", "median"),
            median_force_N=("reference_force_corrected_N", "median"),
            median_cycle_force_zero_offset_N=("cycle_force_zero_offset_N", "median"),
            median_internal_timing_correction_ms=("synchronization_offset_ms", "median"),
            median_light=("target_light", "median"),
            median_light_per_pixel=("target_light_per_pixel", "median"),
        )
        .rename(columns={"target_roi": "roi"})
    )
    session_bins = (
        cycle_bins.groupby(
            [
                "collection_day",
                "session_id",
                "roi",
                "speed_mm_min",
                "displacement_mm_abs",
                "motion_phase",
                "force_bin_N",
            ],
            as_index=False,
        )
        .agg(
            cycles=("cycle_index", "nunique"),
            median_raw_force_N=("median_raw_force_N", "median"),
            median_force_N=("median_force_N", "median"),
            median_cycle_force_zero_offset_N=("median_cycle_force_zero_offset_N", "median"),
            median_internal_timing_correction_ms=("median_internal_timing_correction_ms", "median"),
            mean_cycle_light=("median_light", "mean"),
            sd_cycle_light=("median_light", "std"),
            cv_cycle_light=("median_light", lambda x: float(np.std(x, ddof=1) / np.mean(x)) if len(x) > 1 and np.mean(x) > 0 else float("nan")),
            mean_cycle_light_per_pixel=("median_light_per_pixel", "mean"),
        )
    )
    response = (
        session_bins.groupby(
            ["roi", "speed_mm_min", "displacement_mm_abs", "motion_phase", "force_bin_N"],
            as_index=False,
        )
        .agg(
            independent_sessions=("session_id", "nunique"),
            collection_days=("collection_day", "nunique"),
            median_force_N=("median_force_N", "median"),
            median_light=("mean_cycle_light", "median"),
            min_light=("mean_cycle_light", "min"),
            max_light=("mean_cycle_light", "max"),
            median_light_per_pixel=("mean_cycle_light_per_pixel", "median"),
        )
    )
    pivot = session_bins.pivot_table(
        index=["collection_day", "session_id", "roi", "speed_mm_min", "displacement_mm_abs", "force_bin_N"],
        columns="motion_phase",
        values="mean_cycle_light",
        aggfunc="first",
    ).reset_index()
    hysteresis_session = pivot.dropna(subset=["pressing_down", "retracting"]).copy()
    hysteresis_session["loading_minus_unloading_light"] = (
        hysteresis_session["pressing_down"] - hysteresis_session["retracting"]
    )
    full_scale = (
        session_bins.groupby("roi")["mean_cycle_light"].agg(lambda x: float(x.max() - x.min())).to_dict()
    )
    hysteresis_session["hysteresis_percent_observed_span"] = hysteresis_session.apply(
        lambda row: row["loading_minus_unloading_light"] / full_scale[int(row["roi"])] * 100
        if full_scale[int(row["roi"])] > 0
        else np.nan,
        axis=1,
    )
    hysteresis = (
        hysteresis_session.groupby(
            ["roi", "speed_mm_min", "displacement_mm_abs", "force_bin_N"], as_index=False
        )
        .agg(
            independent_sessions=("session_id", "nunique"),
            collection_days=("collection_day", "nunique"),
            median_loading_light=("pressing_down", "median"),
            median_unloading_light=("retracting", "median"),
            median_hysteresis_light=("loading_minus_unloading_light", "median"),
            median_hysteresis_percent_observed_span=("hysteresis_percent_observed_span", "median"),
        )
    )
    force_separation_rows: list[dict[str, Any]] = []
    for keys, group in session_bins.groupby(
        ["collection_day", "session_id", "roi", "speed_mm_min", "displacement_mm_abs"],
        sort=True,
    ):
        load = group[group["motion_phase"].eq("pressing_down")]
        unload = group[group["motion_phase"].eq("retracting")]
        load_curve = load.groupby("mean_cycle_light", as_index=False)["median_force_N"].median().sort_values("mean_cycle_light")
        unload_curve = unload.groupby("mean_cycle_light", as_index=False)["median_force_N"].median().sort_values("mean_cycle_light")
        if len(load_curve) < 3 or len(unload_curve) < 3:
            continue
        lower = max(float(load_curve["mean_cycle_light"].min()), float(unload_curve["mean_cycle_light"].min()))
        upper = min(float(load_curve["mean_cycle_light"].max()), float(unload_curve["mean_cycle_light"].max()))
        if not np.isfinite(lower) or not np.isfinite(upper) or upper <= lower:
            continue
        light_levels = np.linspace(lower, upper, 21)
        load_force = np.interp(light_levels, load_curve["mean_cycle_light"], load_curve["median_force_N"])
        unload_force = np.interp(light_levels, unload_curve["mean_cycle_light"], unload_curve["median_force_N"])
        force_span = max(float(group["median_force_N"].max() - group["median_force_N"].min()), 0.0)
        for light_level, loading_force, unloading_force in zip(light_levels, load_force, unload_force, strict=True):
            difference = float(loading_force - unloading_force)
            force_separation_rows.append(
                {
                    "collection_day": keys[0],
                    "session_id": keys[1],
                    "roi": int(keys[2]),
                    "speed_mm_min": float(keys[3]),
                    "displacement_mm_abs": float(keys[4]),
                    "matched_light": float(light_level),
                    "loading_force_N": float(loading_force),
                    "unloading_force_N": float(unloading_force),
                    "loading_minus_unloading_force_N": difference,
                    "force_separation_percent_observed_span": difference / force_span * 100 if force_span > 0 else float("nan"),
                }
            )
    force_separation_session = pd.DataFrame(force_separation_rows)
    force_separation = (
        force_separation_session.groupby(
            ["roi", "speed_mm_min", "displacement_mm_abs", "matched_light"], as_index=False
        )
        .agg(
            independent_sessions=("session_id", "nunique"),
            collection_days=("collection_day", "nunique"),
            median_loading_force_N=("loading_force_N", "median"),
            median_unloading_force_N=("unloading_force_N", "median"),
            median_force_separation_N=("loading_minus_unloading_force_N", "median"),
            median_force_separation_percent_observed_span=("force_separation_percent_observed_span", "median"),
        )
        if not force_separation_session.empty
        else pd.DataFrame()
    )
    repeatability = (
        session_bins.groupby(["session_id", "roi", "speed_mm_min", "displacement_mm_abs"], as_index=False)
        .agg(
            contributing_cycles=("cycles", "max"),
            median_within_cycle_cv=("cv_cycle_light", "median"),
            max_within_cycle_cv=("cv_cycle_light", "max"),
        )
    )
    expected_cycles = (
        frames[
            frames["archive_id"].eq("automated")
            & frames["cycle_index"].notna()
            & frames["cycle_index"].gt(0)
        ]
        .groupby("session_id", as_index=False)
        .agg(expected_cycles=("cycle_index", "nunique"))
    )
    repeatability = repeatability.merge(expected_cycles, on="session_id", how="left")
    repeatability["coverage_qualification"] = np.where(
        repeatability["contributing_cycles"].eq(repeatability["expected_cycles"]),
        "complete-cycle-bin-coverage",
        "partial-cycle-bin-coverage",
    )
    rate = (
        response[
            response["motion_phase"].eq("pressing_down")
            & response["independent_sessions"].eq(2)
        ]
        .groupby(["roi", "speed_mm_min", "displacement_mm_abs"], as_index=False)
        .agg(
            independent_sessions=("independent_sessions", "min"),
            peak_median_light=("median_light", "max"),
            median_light_across_supported_bins=("median_light", "median"),
        )
    )

    condition_rows = frames[
        frames["archive_id"].eq("automated")
        & frames["speed_mm_min"].notna()
        & frames["displacement_mm"].notna()
    ]
    condition_by_session = condition_rows.groupby("session_id").agg(
        condition_speed_mm_min=("speed_mm_min", "first"),
        condition_displacement_mm=("displacement_mm", "first"),
    )
    phase_source = frames[
        frames["archive_id"].eq("automated")
        & frames["cycle_index"].notna()
        & frames["motion_phase"].isin(["holding", "inter_cycle_dwell"])
    ].copy()
    phase_source["speed_mm_min"] = phase_source["session_id"].map(
        condition_by_session["condition_speed_mm_min"]
    )
    phase_source["displacement_mm"] = phase_source["session_id"].map(
        condition_by_session["condition_displacement_mm"]
    )
    phase_source = phase_source.sort_values(
        ["session_id", "cycle_index", "capture_monotonic_relative_s"]
    )
    phase_source = phase_source.assign(displacement_mm_abs=phase_source["displacement_mm"].abs())
    phase_rows: list[dict[str, Any]] = []
    trace_rows: list[dict[str, Any]] = []
    for keys, group in phase_source.groupby(
        ["collection_day", "session_id", "target_roi", "speed_mm_min", "displacement_mm_abs", "cycle_index", "motion_phase"],
        dropna=False,
        sort=True,
    ):
        if len(group) < 2:
            continue
        t = group["capture_monotonic_relative_s"].to_numpy(dtype=float)
        t = t - t[0]
        light = group["target_light"].to_numpy(dtype=float)
        raw_force = group["reference_force_raw_N"].to_numpy(dtype=float)
        force = group["reference_force_corrected_N"].to_numpy(dtype=float)
        phase_rows.append(
            {
                "collection_day": keys[0],
                "session_id": keys[1],
                "roi": int(keys[2]),
                "speed_mm_min": float(keys[3]),
                "displacement_mm_abs": float(keys[4]),
                "cycle_index": int(keys[5]),
                "phase": keys[6],
                "frames": len(group),
                "duration_s": float(t[-1]),
                "light_change": float(light[-1] - light[0]),
                "force_change_N": float(force[-1] - force[0]),
                "raw_force_change_N": float(raw_force[-1] - raw_force[0]),
                "residual_light_end": float(light[-1]),
                "residual_force_end_N": float(force[-1]),
                "median_cycle_force_zero_offset_N": float(group["cycle_force_zero_offset_N"].median()),
                "median_internal_timing_correction_ms": float(group["synchronization_offset_ms"].median()),
            }
        )
        for time_s, light_value, raw_force_value, force_value in zip(t, light, raw_force, force, strict=True):
            trace_rows.append(
                {
                    "session_id": keys[1],
                    "roi": int(keys[2]),
                    "speed_mm_min": float(keys[3]),
                    "displacement_mm_abs": float(keys[4]),
                    "cycle_index": int(keys[5]),
                    "phase": keys[6],
                    "elapsed_phase_s": float(time_s),
                    "light": float(light_value),
                    "raw_force_N": float(raw_force_value),
                    "force_N": float(force_value),
                    "cycle_force_zero_offset_N": float(group["cycle_force_zero_offset_N"].median()),
                    "internal_timing_correction_ms": float(group["synchronization_offset_ms"].median()),
                }
            )
    phase_cycle = pd.DataFrame(phase_rows)
    phase_trace = pd.DataFrame(trace_rows)
    phase_session = (
        phase_cycle.groupby(
            ["collection_day", "session_id", "roi", "speed_mm_min", "displacement_mm_abs", "phase"],
            as_index=False,
        )
        .agg(
            cycles=("cycle_index", "nunique"),
            median_frames=("frames", "median"),
            median_duration_s=("duration_s", "median"),
            mean_cycle_light_change=("light_change", "mean"),
            mean_cycle_force_change_N=("force_change_N", "mean"),
            mean_cycle_raw_force_change_N=("raw_force_change_N", "mean"),
            mean_cycle_residual_light_end=("residual_light_end", "mean"),
            mean_cycle_residual_force_end_N=("residual_force_end_N", "mean"),
            median_cycle_force_zero_offset_N=("median_cycle_force_zero_offset_N", "median"),
            median_internal_timing_correction_ms=("median_internal_timing_correction_ms", "median"),
        )
    )
    for frame in (phase_cycle, phase_session, phase_trace):
        frame["evidence_role"] = "creep-related exploratory evidence"
        frame["claim_boundary"] = "not constant-force creep and not independent no-contact evidence"
    for frame in (cycle_bins, session_bins, response, hysteresis_session, hysteresis, repeatability, rate):
        frame["evidence_role"] = "hysteresis supporting component"
    if not force_separation_session.empty:
        force_separation_session["evidence_role"] = "hysteresis supporting component"
        force_separation["evidence_role"] = "hysteresis supporting component"
    return {
        "automatic_cycle_force_bins": cycle_bins,
        "automatic_session_force_bins": session_bins,
        "automatic_response": response,
        "automatic_hysteresis_session": hysteresis_session,
        "automatic_hysteresis": hysteresis,
        "automatic_hysteresis_force_separation_session": force_separation_session,
        "automatic_hysteresis_force_separation": force_separation,
        "automatic_within_session_repeatability": repeatability,
        "automatic_speed_dependence": rate,
        "automatic_phase_cycle": phase_cycle,
        "automatic_phase_session": phase_session,
        "automatic_phase_trace": phase_trace,
    }


def manual_lag_tables(frames: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Estimate a manual-only system lag from derivative cross-correlation.

    Positive lag means the optical response follows the reference response: the
    reference derivative at time t is compared with optical derivative at
    t + lag.  Absolute correlation selects the lag so inverse optical responses
    remain visible instead of being silently excluded.
    """
    source = frames[
        frames["scientific_role"].eq("model_primary")
        & frames["force_valid"]
        & frames["reference_force_corrected_N"].notna()
        & frames["target_signed_light"].notna()
    ].copy()
    lag_grid_ms = np.arange(MANUAL_LAG_MIN_MS, MANUAL_LAG_MAX_MS + MANUAL_LAG_STEP_MS, MANUAL_LAG_STEP_MS)
    curve_rows: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    for session_id, group in source.groupby("session_id", sort=True):
        group = group.sort_values("capture_monotonic_relative_s").drop_duplicates("capture_monotonic_relative_s")
        t = group["capture_monotonic_relative_s"].to_numpy(dtype=float)
        force = group["reference_force_corrected_N"].to_numpy(dtype=float)
        optical = group["target_signed_light"].to_numpy(dtype=float)
        if len(group) < 20 or np.ptp(t) <= 0:
            continue
        force_smooth = pd.Series(force).rolling(3, center=True, min_periods=1).median().to_numpy()
        optical_smooth = pd.Series(optical).rolling(3, center=True, min_periods=1).median().to_numpy()
        force_rate = np.gradient(force_smooth, t)
        optical_rate = np.gradient(optical_smooth, t)
        for values in (force_rate, optical_rate):
            lower, upper = np.nanpercentile(values, [1, 99])
            np.clip(values, lower, upper, out=values)
        session_curves: list[tuple[float, float, int]] = []
        for lag_ms in lag_grid_ms:
            lag_s = lag_ms / 1000.0
            shifted_t = t + lag_s
            valid = (shifted_t >= t[0]) & (shifted_t <= t[-1]) & np.isfinite(force_rate)
            shifted_optical = np.interp(shifted_t[valid], t, optical_rate)
            reference = force_rate[valid]
            finite_pair = np.isfinite(reference) & np.isfinite(shifted_optical)
            if finite_pair.sum() < 15 or np.std(reference[finite_pair]) <= 0 or np.std(shifted_optical[finite_pair]) <= 0:
                correlation = float("nan")
            else:
                correlation = float(np.corrcoef(reference[finite_pair], shifted_optical[finite_pair])[0, 1])
            session_curves.append((float(lag_ms), correlation, int(finite_pair.sum())))
            curve_rows.append(
                {
                    "archive_id": "manual",
                    "session_id": session_id,
                    "test_group": group["test_group"].iloc[0],
                    "roi": int(group["target_roi"].iloc[0]),
                    "lag_ms": float(lag_ms),
                    "correlation": correlation,
                    "absolute_correlation": abs(correlation) if np.isfinite(correlation) else float("nan"),
                    "overlap_frames": int(finite_pair.sum()),
                }
            )
        usable = [row for row in session_curves if np.isfinite(row[1])]
        if not usable:
            continue
        best_lag_ms, best_correlation, overlap_frames = max(usable, key=lambda row: abs(row[1]))
        session_rows.append(
            {
                "archive_id": "manual",
                "session_id": session_id,
                "test_group": group["test_group"].iloc[0],
                "roi": int(group["target_roi"].iloc[0]),
                "frames": int(len(group)),
                "duration_s": float(t[-1] - t[0]),
                "median_frame_interval_ms": float(np.median(np.diff(t)) * 1000),
                "best_lag_ms": best_lag_ms,
                "best_correlation": best_correlation,
                "best_absolute_correlation": abs(best_correlation),
                "overlap_frames": overlap_frames,
                "lag_at_search_boundary": bool(best_lag_ms in {MANUAL_LAG_MIN_MS, MANUAL_LAG_MAX_MS}),
                "sign_convention": "positive lag means optical follows reference",
                "lag_label": "camera+acquisition+synchronization+material system lag",
                "qualification": "exploratory-manual-only",
            }
        )
    curve = pd.DataFrame(curve_rows)
    session = pd.DataFrame(session_rows)
    summary = (
        session.groupby("roi", as_index=False)
        .agg(
            independent_sessions=("session_id", "nunique"),
            median_best_lag_ms=("best_lag_ms", "median"),
            minimum_best_lag_ms=("best_lag_ms", "min"),
            maximum_best_lag_ms=("best_lag_ms", "max"),
            median_best_correlation=("best_correlation", "median"),
            median_best_absolute_correlation=("best_absolute_correlation", "median"),
            boundary_session_fraction=("lag_at_search_boundary", "mean"),
        )
        .sort_values("roi")
    )
    summary["archive_id"] = "manual"
    summary["sign_convention"] = "positive lag means optical follows reference"
    summary["claim_boundary"] = "system-level estimate; not intrinsic material response time"
    return {
        "manual_system_lag_curve": curve,
        "manual_system_lag_session": session,
        "manual_system_lag_summary": summary,
    }


def metric_status_table(tables: dict[str, pd.DataFrame], prior_plateau: pd.DataFrame) -> pd.DataFrame:
    sensitivity = tables["manual_sensitivity_nonlinearity"]
    detection = tables["manual_detection_summary"]
    auto_conditions = tables["coverage_automatic_conditions"]
    rows = [
        {
            "metric": "Archive integrity and original-frame reconciliation",
            "source": "Both archives / immutable feature store",
            "status": "primary",
            "independent_evidence": "245 complete sessions; 179,176 reconciled frames",
            "claim_boundary": "Source and pipeline evidence only",
        },
        {
            "metric": "Low-force sensitivity",
            "source": "54 manual TEST1-TEST6 sessions",
            "status": "primary" if sensitivity["qualification"].eq("primary").all() else "exploratory-insufficient",
            "independent_evidence": "Six TEST sessions per ROI; supported-bin rule >=3 sessions",
            "claim_boundary": "Characterization only; no deployed force range",
        },
        {
            "metric": "Local sensitivity",
            "source": "54 manual TEST1-TEST6 sessions",
            "status": "primary",
            "independent_evidence": "Adjacent supported 0.25 N bins; >=3 independent sessions per endpoint",
            "claim_boundary": "No interpolation across force-bin gaps",
        },
        {
            "metric": "Linearity / nonlinearity",
            "source": "54 manual TEST1-TEST6 sessions",
            "status": "primary",
            "independent_evidence": "Manual-only session-balanced 0-1 N response",
            "claim_boundary": "Observed light-span deviation; separate from model error",
        },
        {
            "metric": "Plateau and usable-range observations",
            "source": "54 manual TEST1-TEST6 sessions",
            "status": "conditional-qualified",
            "independent_evidence": "Per-ROI supported manual bins and frozen sustained-plateau rule",
            "claim_boundary": "Observation only; no automatic evidence and no deployed/common range",
        },
        {
            "metric": "General between-session repeatability",
            "source": "54 manual TEST1-TEST6 sessions",
            "status": "primary",
            "independent_evidence": "Six independent sessions per ROI",
            "claim_boundary": "Session-level spread; frames are repeated observations",
        },
        {
            "metric": "Spatial uniformity",
            "source": "54 manual TEST1-TEST6 sessions plus 11 manual no-contact sessions",
            "status": "primary",
            "independent_evidence": "All ROI 1-9 with integrated and area-normalized response",
            "claim_boundary": "Manual-only comparison; sharpness retained and non-blocking",
        },
        {
            "metric": "Cross-talk",
            "source": "54 manual TEST1-TEST6 sessions",
            "status": "primary",
            "independent_evidence": "Complete 9x9 target/measured ROI matrix",
            "claim_boundary": "Single-ROI presses only",
        },
        {
            "metric": "No-contact noise and drift",
            "source": "11 dedicated manual no-contact sessions",
            "status": "primary",
            "independent_evidence": "All nine ROIs observed in each session",
            "claim_boundary": "Short-term recorded durations only",
        },
        {
            "metric": "SNR and optical detection threshold",
            "source": "Manual press vs dedicated no-contact sessions",
            "status": "conditional-qualified" if detection["qualification"].ne("not-established").any() else "not-established",
            "independent_evidence": "Frozen 1% empirical FPR / 95% recall rule; >=3 press sessions",
            "claim_boundary": "Not an application threshold",
        },
        {
            "metric": "Hysteresis",
            "source": "162 automatic sessions",
            "status": "conditional-qualified",
            "independent_evidence": f"{int(auto_conditions['independent_sessions'].min())} sessions per exact ROI x speed x displacement condition",
            "claim_boundary": "Speed is a hysteresis stratum; condition intervals descriptive",
        },
        {
            "metric": "Hysteresis internal cycle and correction QA",
            "source": "162 automatic sessions",
            "status": "conditional-qualified",
            "independent_evidence": "Cycles summarized within session; cycle-local force zero and timing provenance retained",
            "claim_boundary": "Supporting component only; not general repeatability or standalone lag",
        },
        {
            "metric": "Creep-related fixed-displacement settling and unloaded recovery",
            "source": "Automatic holding and inter-cycle dwell phases",
            "status": "exploratory-insufficient",
            "independent_evidence": "Cycle estimates averaged within complete session",
            "claim_boundary": "Not constant-force creep",
        },
        {
            "metric": "Approximate standalone system lag",
            "source": "54 manual TEST1-TEST6 synchronized optical/reference traces",
            "status": "exploratory-insufficient",
            "independent_evidence": "Manual session-level derivative cross-correlation with complete lag curves",
            "claim_boundary": "Camera + acquisition + synchronization + material system lag; not intrinsic response time",
        },
        {
            "metric": "Noise-equivalent force",
            "source": "Dedicated no-contact noise / manual low-force sensitivity",
            "status": "conditional-qualified",
            "independent_evidence": "Per-ROI proxy from independent no-contact and press sessions",
            "claim_boundary": "Noise-limited stability proxy, not true resolution",
        },
        {
            "metric": "Historical former combined common-range / monotonicity gate",
            "source": "Preserved characterization-dade466f019e6d15",
            "status": "not-established",
            "independent_evidence": f"All nine ROI monotonic gate pass = {bool(prior_plateau['monotonic_gate_pass'].all())}",
            "claim_boundary": "Historical automatic-derived procedure only; not controlling under the revised manual-first plan",
        },
        {
            "metric": "True creep",
            "source": "No qualifying constant-force protocol",
            "status": "not-established",
            "independent_evidence": "Required >=5 s constant-force holds were not collected",
            "claim_boundary": "No creep number reported",
        },
        {
            "metric": "True force resolution",
            "source": "No qualifying randomized settled-step protocol",
            "status": "not-established",
            "independent_evidence": "Continuous ramps cannot establish resolution",
            "claim_boundary": "No resolution claim reported",
        },
    ]
    return pd.DataFrame(rows)


def configure_plot() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "axes.titlesize": 11,
            "axes.labelsize": 9,
            "axes.edgecolor": "#374151",
            "axes.labelcolor": "#111827",
            "xtick.color": "#374151",
            "ytick.color": "#374151",
            "text.color": "#111827",
            "axes.grid": True,
            "grid.color": "#E5E7EB",
            "grid.linewidth": 0.7,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )


def save_figure(
    fig: plt.Figure,
    figure_dir: Path,
    name: str,
    plotted: pd.DataFrame,
    metadata: dict[str, Any],
    layout_rect: tuple[float, float, float, float] | None = None,
    tight_layout: bool = True,
) -> dict[str, str]:
    figure_dir.mkdir(parents=True, exist_ok=True)
    if tight_layout:
        fig.tight_layout(rect=layout_rect)
    paths = {
        "png": figure_dir / f"{name}.png",
        "svg": figure_dir / f"{name}.svg",
        "pdf": figure_dir / f"{name}.pdf",
        "csv": figure_dir / f"{name}.csv",
        "json": figure_dir / f"{name}.json",
    }
    fig.savefig(paths["png"], dpi=PLOT_DPI, bbox_inches="tight")
    fig.savefig(paths["svg"], bbox_inches="tight")
    fig.savefig(paths["pdf"], bbox_inches="tight")
    plt.close(fig)
    plotted.to_csv(paths["csv"], index=False, lineterminator="\n")
    clean = plotted.replace([np.inf, -np.inf], np.nan).astype(object)
    clean = clean.where(pd.notna(clean), None)
    meta = dict(metadata)
    meta.update(
        {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "seed": DEFAULT_SEED,
            "plotted_rows": len(plotted),
            "plotted_columns": list(plotted.columns),
            "plotted_data_hash": json_hash(clean.to_dict("records")),
            "exports": {key: path.name for key, path in paths.items()},
        }
    )
    write_json(paths["json"], meta)
    return {path.name: sha256_file(path) for path in paths.values()}


def build_figures(
    tables: dict[str, pd.DataFrame],
    figure_dir: Path,
    source_hash: str,
    code_hash: str,
) -> tuple[dict[str, str], pd.DataFrame]:
    configure_plot()
    outputs: dict[str, str] = {}
    chart_map: list[dict[str, Any]] = []

    def meta(title: str, source_table: str, units: str, n: str, method: str, filters: list[str]) -> dict[str, Any]:
        return {
            "title": title,
            "source_manifest_hash": source_hash,
            "source_table": source_table,
            "source_table_hash": sha256_file(figure_dir.parent / "tables" / f"{source_table}.csv"),
            "code_hash": code_hash,
            "filters": filters,
            "units": units,
            "independent_session_count": n,
            "uncertainty_method": method,
        }

    # Coverage heatmaps.
    auto = tables["coverage_automatic_conditions"]
    values = auto.pivot_table(index="target_roi", columns=["speed_mm_min", "displacement_mm_abs"], values="independent_sessions")
    fig, ax = plt.subplots(figsize=(11, 4.8))
    image = ax.imshow(
        values.to_numpy(),
        cmap="Blues",
        vmin=0,
        vmax=max(3, float(values.to_numpy(dtype=float).max())),
    )
    ax.set_title("Automatic hysteresis/creep-related condition coverage")
    ax.set_xlabel("Speed (mm/min) x absolute displacement (mm)")
    ax.set_ylabel("Target ROI")
    ax.set_xticks(range(len(values.columns)), [f"{int(s)} x {d:g}" for s, d in values.columns], rotation=45, ha="right")
    ax.set_yticks(range(len(values.index)), values.index.astype(int))
    for y in range(values.shape[0]):
        for x in range(values.shape[1]):
            ax.text(x, y, int(values.iloc[y, x]), ha="center", va="center", color="#111827")
    fig.colorbar(image, ax=ax, label="Independent sessions")
    outputs.update(save_figure(fig, figure_dir, "fig01_automatic_condition_coverage", auto, meta(
        "Automatic hysteresis/creep-related condition coverage", "coverage_automatic_conditions", "sessions", "162", "exact counts including one 10-cycle and one 20-cycle session per cell", ["automatic archive limited to hysteresis and creep-related sufficiency"]
    )))
    chart_map.append({"figure": "fig01_automatic_condition_coverage", "family": "heatmap", "claim": "Each exact permitted automatic condition has two independent sessions: one 10-cycle and one 20-cycle run."})

    response = tables["manual_static_response"]
    fig, axes = plt.subplots(3, 3, figsize=(13, 12), sharex=True)
    for roi, ax in enumerate(axes.flat, 1):
        data = response[(response["roi"].eq(roi)) & response["independent_sessions"].ge(3) & response["force_bin_N"].le(10)]
        ax.plot(data["median_force_N"], data["median_light"], color="#2563EB", marker="o", ms=3, lw=1.4)
        ax.fill_between(data["median_force_N"], data["q25_light"], data["q75_light"], color="#DBEAFE", alpha=0.8)
        ax.set_title(f"ROI {roi}")
        ax.set_xlabel("Force (N)")
        ax.set_ylabel("Positive light sum")
    fig.suptitle("Manual session-balanced static response", y=1.01, fontsize=13)
    plotted = response[(response["independent_sessions"].ge(3)) & response["force_bin_N"].le(10)].copy()
    outputs.update(save_figure(fig, figure_dir, "fig02_manual_static_response", plotted, meta(
        "Manual session-balanced static response", "manual_static_response", "positive V-sum and N", "54 (six per ROI)", "median and interquartile range across sessions", ["manual TEST1-TEST6", ">=3 sessions/bin", "force <=10 N"]
    )))
    chart_map.append({"figure": "fig02_manual_static_response", "family": "faceted line", "claim": "All ROI are shown with equal session influence."})

    sensitivity = tables["spatial_uniformity"].copy()
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.8))
    x = np.arange(1, 10)
    axes[0].bar(x, sensitivity["low_force_sensitivity_light_per_N"], color="#2563EB", edgecolor="#374151")
    axes[0].axhline(0, color="#111827", lw=1)
    axes[0].set_title("Integrated low-force sensitivity")
    axes[0].set_xlabel("ROI")
    axes[0].set_ylabel("Positive light sum per N")
    axes[0].set_xticks(x)
    axes[1].bar(x, sensitivity["low_force_sensitivity_light_per_pixel_per_N"], color="#F59E0B", edgecolor="#374151")
    axes[1].axhline(0, color="#111827", lw=1)
    axes[1].set_title("Area-normalized low-force sensitivity")
    axes[1].set_xlabel("ROI")
    axes[1].set_ylabel("Positive light per pixel per N")
    axes[1].set_xticks(x)
    axes[2].bar(x, sensitivity["nonlinearity_percent_supported_low_force_span"], color="#6B7280", edgecolor="#374151")
    axes[2].set_title("Low-force nonlinearity")
    axes[2].set_xlabel("ROI")
    axes[2].set_ylabel("Maximum deviation (% observed light span)")
    axes[2].set_xticks(x)
    outputs.update(save_figure(fig, figure_dir, "fig03_spatial_sensitivity_uniformity", sensitivity, meta(
        "Low-force sensitivity and nonlinearity across ROI", "spatial_uniformity", "positive V-sum/N, positive V/pixel/N, and % span", "54 (six per ROI)", "session-balanced low-force linear slope and maximum residual", ["manual only", "0-1 N", ">=3 sessions/bin"]
    )))
    chart_map.append({"figure": "fig03_spatial_sensitivity_uniformity", "family": "three-panel bar", "claim": "Integrated and area-normalized sensitivity plus manual-only low-force nonlinearity are shown for every ROI."})

    repeatability = tables["manual_repeatability"]
    rep_summary = repeatability[repeatability["force_bin_N"].between(0.375, 2.875)].groupby("roi", as_index=False).agg(
        median_sd_percent_span=("between_session_sd_percent_observed_span", "median"),
        max_sd_percent_span=("between_session_sd_percent_observed_span", "max"),
        supported_bins=("force_bin_N", "nunique"),
    )
    fig, ax = plt.subplots(figsize=(8.5, 4.6))
    ax.bar(rep_summary["roi"], rep_summary["median_sd_percent_span"], color="#2563EB", edgecolor="#374151")
    ax.scatter(rep_summary["roi"], rep_summary["max_sd_percent_span"], marker="D", color="#F59E0B", label="Maximum supported-bin SD")
    ax.set_title("Manual between-session repeatability")
    ax.set_xlabel("ROI")
    ax.set_ylabel("Session SD (% observed ROI light span)")
    ax.set_xticks(range(1, 10))
    ax.legend(frameon=False)
    outputs.update(save_figure(fig, figure_dir, "fig04_manual_repeatability", rep_summary, meta(
        "Manual between-session repeatability", "manual_repeatability", "% observed per-ROI light span", "54 (six per ROI)", "session SD; median/max across supported force bins", ["0.25-3 N", ">=3 sessions/bin"]
    )))
    chart_map.append({"figure": "fig04_manual_repeatability", "family": "bar + point", "claim": "Session-level, not frame-level, spread is reported."})

    ct = tables["cross_talk"]
    matrix = ct.pivot(index="target_roi", columns="measured_roi", values="median_share_total").sort_index().sort_index(axis=1)
    fig, ax = plt.subplots(figsize=(7.5, 6.2))
    image = ax.imshow(matrix.to_numpy(), cmap="Blues", vmin=0, vmax=1)
    ax.set_title("Single-ROI press response distribution")
    ax.set_xlabel("Measured ROI")
    ax.set_ylabel("Target ROI")
    ax.set_xticks(range(9), range(1, 10))
    ax.set_yticks(range(9), range(1, 10))
    for y in range(9):
        for x in range(9):
            ax.text(x, y, f"{matrix.iloc[y, x]:.2f}", ha="center", va="center", fontsize=7, color="#111827")
    fig.colorbar(image, ax=ax, label="Median share of total positive response")
    outputs.update(save_figure(fig, figure_dir, "fig05_cross_talk_matrix", ct, meta(
        "Single-ROI press response distribution", "cross_talk", "share of total response", "54 (six per target ROI)", "session median then across-session median", ["0.75-1.25 N"]
    )))
    chart_map.append({"figure": "fig05_cross_talk_matrix", "family": "heatmap", "claim": "The complete 9x9 target/measured ROI matrix is retained."})

    noise = tables["no_contact_session"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))
    data = [noise[noise["roi"].eq(roi)]["signed_mad"] for roi in range(1, 10)]
    axes[0].boxplot(data, tick_labels=range(1, 10), patch_artist=True, boxprops={"facecolor": "#DBEAFE", "edgecolor": "#2563EB"}, medianprops={"color": "#111827"})
    axes[0].set_title("Dedicated no-contact signed-light noise")
    axes[0].set_xlabel("ROI")
    axes[0].set_ylabel("Session signed-light MAD")
    axes[1].axhline(0, color="#111827", lw=1)
    for roi in range(1, 10):
        part = noise[noise["roi"].eq(roi)]
        axes[1].scatter(np.full(len(part), roi), part["signed_drift_light_per_min"], s=20, alpha=0.7, color="#2563EB")
    axes[1].set_title("Short-term no-contact drift")
    axes[1].set_xlabel("ROI")
    axes[1].set_ylabel("Signed-light slope per minute")
    axes[1].set_xticks(range(1, 10))
    outputs.update(save_figure(fig, figure_dir, "fig06_no_contact_noise_drift", noise, meta(
        "Dedicated no-contact noise and drift", "no_contact_session", "signed V-sum and signed V-sum/min", "11 sessions observing all nine ROI", "session MAD and least-squares slope", ["dedicated no-contact only"]
    )))
    chart_map.append({"figure": "fig06_no_contact_noise_drift", "family": "box plot + scatter", "claim": "Noise and drift use dedicated no-contact evidence."})

    snr = tables["manual_snr"]
    detection = tables["manual_detection_summary"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))
    axes[0].bar(snr["roi"], snr["snr_dB_amplitude_convention"], color="#2563EB", edgecolor="#374151")
    axes[0].set_title("Manual contact SNR")
    axes[0].set_xlabel("ROI")
    axes[0].set_ylabel("SNR (dB; 20 log10 amplitude ratio)")
    axes[0].set_xticks(range(1, 10))
    axes[1].bar(detection["roi"], detection["optical_detection_region_N"], color="#F59E0B", edgecolor="#374151")
    axes[1].set_title("Characterization-only optical detection region")
    axes[1].set_xlabel("ROI")
    axes[1].set_ylabel("Smallest supported force-bin centre (N)")
    axes[1].set_xticks(range(1, 10))
    plotted = snr.merge(detection, on="roi", suffixes=("_snr", "_detection"))
    outputs.update(save_figure(fig, figure_dir, "fig07_snr_detection", plotted, meta(
        "Manual SNR and optical detection region", "manual_snr", "dB and N", "54 press + 11 no-contact sessions", "session-balanced signal/noise; frozen 1% FPR and 95% recall", ["signal 0.75-1.25 N", ">=3 press sessions/bin"]
    )))
    chart_map.append({"figure": "fig07_snr_detection", "family": "paired bars", "claim": "Detection is explicitly characterization-only."})

    hysteresis = tables["automatic_hysteresis"]
    plot_h = hysteresis[
        hysteresis["displacement_mm_abs"].eq(3.5)
    ]
    fig, axes = plt.subplots(3, 3, figsize=(12, 10), sharex=True)
    colors = {200.0: "#2563EB", 400.0: "#F59E0B", 600.0: "#6B7280"}
    for roi, ax in enumerate(axes.flat, 1):
        for speed, data in plot_h[plot_h["roi"].eq(roi)].groupby("speed_mm_min"):
            data = data.sort_values("force_bin_N")
            ax.plot(data["force_bin_N"], data["median_loading_light"], color=colors[speed], lw=1.4, label=f"{int(speed)} load")
            ax.plot(data["force_bin_N"], data["median_unloading_light"], color=colors[speed], lw=1.2, ls="--", label=f"{int(speed)} unload")
        ax.set_title(f"ROI {roi}")
        ax.set_xlabel("Corrected force (N)")
        ax.set_ylabel("Cycle-then-session light")
    present_speeds = sorted(plot_h["speed_mm_min"].dropna().unique())
    handles = [Line2D([0], [0], color=colors[speed], ls=phase_style) for speed in present_speeds for phase_style in ("-", "--")]
    labels = [f"{int(speed)} {phase_label}" for speed in present_speeds for phase_label in ("load", "unload")]
    fig.subplots_adjust(left=0.07, right=0.99, bottom=0.06, top=0.82, wspace=0.30, hspace=0.38)
    fig.legend(handles, labels, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 0.91))
    fig.suptitle("Automatic hysteresis at 3.5 mm, separated by speed", y=0.97, fontsize=13)
    outputs.update(save_figure(fig, figure_dir, "fig08_automatic_hysteresis", plot_h, meta(
        "Automatic hysteresis at 3.5 mm", "automatic_hysteresis", "positive V-sum and corrected N", "two sessions per exact condition", "cycle estimates averaged within session; across-session median", ["automatic only", "absolute displacement=3.5 mm", "speeds shown separately", "cycle-local force zero and internal timing provenance retained"]
    ), tight_layout=False))
    chart_map.append({"figure": "fig08_automatic_hysteresis", "family": "faceted loop", "claim": "Loading/unloading and speeds remain separate."})

    rate = tables["automatic_speed_dependence"]
    rate_plot = rate[rate["displacement_mm_abs"].eq(3.5)]
    fig, ax = plt.subplots(figsize=(9.5, 5.0))
    qa_colors = plt.cm.Blues(np.linspace(0.35, 0.95, 9))
    qa_styles = ["-", "--", "-."]
    qa_markers = ["o", "s", "D"]
    for roi, data in rate_plot.groupby("roi"):
        index = int(roi) - 1
        ax.plot(
            data["speed_mm_min"], data["peak_median_light"],
            color=qa_colors[index], ls=qa_styles[index % len(qa_styles)],
            marker=qa_markers[index % len(qa_markers)], lw=1.2, label=f"ROI {roi}"
        )
    ax.set_title("Hysteresis speed-stratum internal QA at 3.5 mm")
    ax.set_xlabel("Speed (mm/min)")
    ax.set_ylabel("Peak session-balanced loading light")
    ax.set_xticks([200, 400, 600])
    ax.legend(ncol=3, frameon=False)
    outputs.update(save_figure(fig, figure_dir, "fig09_speed_dependence", rate_plot, meta(
        "Hysteresis speed-stratum internal QA at 3.5 mm", "automatic_speed_dependence", "positive V-sum", "two sessions per exact condition", "cycle estimates averaged within session; descriptive hysteresis support check", ["not a general sensor speed metric", "absolute displacement=3.5 mm", "loading only"]
    )))
    chart_map.append({"figure": "fig09_speed_dependence", "family": "multi-series line", "claim": "Speed remains a hysteresis stratum and is not promoted to a general sensor metric."})

    cycle_rep = tables["automatic_within_session_repeatability"]
    cycle_summary = cycle_rep.groupby(["roi", "speed_mm_min"], as_index=False).agg(
        sessions=("session_id", "nunique"),
        median_session_cycle_cv=("median_within_cycle_cv", "median"),
        q75_session_cycle_cv=("median_within_cycle_cv", q75),
    )
    fig, ax = plt.subplots(figsize=(9.5, 5.0))
    cycle_colors = {200.0: "#2563EB", 400.0: "#F59E0B", 600.0: "#6B7280"}
    cycle_styles = {200.0: "-", 400.0: "--", 600.0: ":"}
    for speed, data in cycle_summary.groupby("speed_mm_min"):
        ax.plot(data["roi"], data["median_session_cycle_cv"] * 100, color=cycle_colors[speed], ls=cycle_styles[speed], marker="o", label=f"{int(speed)} mm/min")
    ax.set_title("Hysteresis internal QA: within-session cycle variability")
    ax.set_xlabel("ROI")
    ax.set_ylabel("Median within-session cycle CV (%)")
    ax.set_xticks(range(1, 10))
    ax.legend(frameon=False)
    outputs.update(save_figure(fig, figure_dir, "fig10_cycle_repeatability", cycle_summary, meta(
        "Hysteresis internal QA: within-session cycle variability", "automatic_within_session_repeatability", "%", "162 sessions", "cycle CV calculated within force bins, summarized within session, then across sessions", ["not standalone general repeatability", "speeds separated"]
    )))
    chart_map.append({"figure": "fig10_cycle_repeatability", "family": "multi-series line", "claim": "Cycles never become independent replicates."})

    phase_trace = tables["automatic_phase_trace"].copy()
    trace_keys = ["session_id", "roi", "speed_mm_min", "displacement_mm_abs", "cycle_index", "phase"]
    phase_trace["phase_duration_s"] = phase_trace.groupby(trace_keys)["elapsed_phase_s"].transform("max")
    phase_trace["phase_fraction"] = np.where(
        phase_trace["phase_duration_s"] > 0,
        phase_trace["elapsed_phase_s"] / phase_trace["phase_duration_s"],
        0,
    )
    phase_trace["phase_fraction_bin"] = (phase_trace["phase_fraction"] * 10).round().clip(0, 10) / 10
    phase_trace["light_change_from_phase_start"] = phase_trace["light"] - phase_trace.groupby(trace_keys)["light"].transform("first")
    phase_trace["force_change_from_phase_start_N"] = phase_trace["force_N"] - phase_trace.groupby(trace_keys)["force_N"].transform("first")
    trace_cycle = phase_trace.groupby(trace_keys + ["phase_fraction_bin"], as_index=False).agg(
        light_change=("light_change_from_phase_start", "median"),
        force_change_N=("force_change_from_phase_start_N", "median"),
    )
    trace_session = trace_cycle.groupby(
        ["session_id", "roi", "speed_mm_min", "displacement_mm_abs", "phase", "phase_fraction_bin"], as_index=False
    ).agg(light_change=("light_change", "mean"), force_change_N=("force_change_N", "mean"))
    trace_summary = trace_session.groupby(["roi", "phase", "phase_fraction_bin"], as_index=False).agg(
        independent_sessions=("session_id", "nunique"),
        median_light_change=("light_change", "median"),
        q25_light_change=("light_change", q25),
        q75_light_change=("light_change", q75),
        median_force_change_N=("force_change_N", "median"),
    )
    fig, axes = plt.subplots(3, 3, figsize=(13, 10), sharex=True)
    phase_style = {"holding": ("#2563EB", "-", "0.5 s fixed-displacement hold"), "inter_cycle_dwell": ("#F59E0B", "--", "2-3 s unloaded dwell")}
    legend_handles = []
    legend_labels = []
    for roi, ax in enumerate(axes.flat, 1):
        force_ax = ax.twinx()
        for phase, (color, linestyle, label) in phase_style.items():
            data = trace_summary[trace_summary["roi"].eq(roi) & trace_summary["phase"].eq(phase)].sort_values("phase_fraction_bin")
            line = ax.plot(data["phase_fraction_bin"], data["median_light_change"], color=color, ls=linestyle, lw=1.5)[0]
            force_ax.plot(data["phase_fraction_bin"], data["median_force_change_N"], color=color, ls=":", lw=1.0, alpha=0.8)
            if roi == 1:
                legend_handles.append(line)
                legend_labels.append(label)
        ax.axhline(0, color="#111827", lw=0.7)
        force_ax.axhline(0, color="#6B7280", lw=0.5, alpha=0.5)
        ax.set_title(f"ROI {roi}")
        ax.set_xlabel("Fraction of recorded phase")
        ax.set_ylabel("Optical change")
        force_ax.set_ylabel("Force change (N)", color="#6B7280")
        force_ax.tick_params(axis="y", colors="#6B7280", labelsize=7)
    legend_handles.append(Line2D([0], [0], color="#6B7280", ls=":", lw=1.2))
    legend_labels.append("Force change on right axis")
    fig.legend(legend_handles, legend_labels, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.02))
    fig.suptitle("Creep-related exploratory evidence: fixed-displacement settling and unloaded recovery", y=1.06, fontsize=13)
    outputs.update(save_figure(fig, figure_dir, "fig11_relaxation_recovery", trace_summary, meta(
        "Creep-related fixed-displacement settling and unloaded recovery", "automatic_phase_trace", "positive V-sum change and N", "162 sessions", "cycle traces averaged within session then median across sessions", ["automatic only", "not constant-force creep", "holding and unloaded dwell shown separately", "internal correction provenance retained"]
    )))
    chart_map.append({"figure": "fig11_relaxation_recovery", "family": "faceted paired-axis traces", "claim": "Only short-term creep-related exploratory trajectories are shown; no numerical true-creep value is claimed."})

    proxy = tables["noise_equivalent_force_proxy"]
    fig, ax = plt.subplots(figsize=(8.8, 4.6))
    ax.bar(proxy["roi"] - 0.18, proxy["noise_equivalent_force_N"], 0.36, color="#2563EB", edgecolor="#374151", label="1x noise/sensitivity")
    ax.bar(proxy["roi"] + 0.18, proxy["conservative_3x_noise_force_N"], 0.36, color="#F59E0B", edgecolor="#374151", label="3x noise/sensitivity")
    ax.set_title("Noise-equivalent-force stability proxy")
    ax.set_xlabel("ROI")
    ax.set_ylabel("Equivalent force (N)")
    ax.set_xticks(range(1, 10))
    ax.legend(frameon=False)
    outputs.update(save_figure(fig, figure_dir, "fig12_noise_equivalent_force", proxy, meta(
        "Noise-equivalent-force stability proxy", "noise_equivalent_force_proxy", "N", "54 press + 11 no-contact sessions", "dedicated no-contact signed-light MAD divided by qualified manual low-force sensitivity", ["not true resolution"]
    )))
    chart_map.append({"figure": "fig12_noise_equivalent_force", "family": "grouped bar", "claim": "The result is reported only as a stability proxy."})

    manual_bins = tables["manual_session_force_bins"]
    manual_coverage = (
        manual_bins[manual_bins["force_bin_N"].le(10)]
        .groupby(["roi", "force_bin_N"], as_index=False)
        .agg(independent_sessions=("session_id", "nunique"), frames=("frames", "sum"))
    )
    coverage_matrix = manual_coverage.pivot(index="roi", columns="force_bin_N", values="independent_sessions").sort_index().sort_index(axis=1)
    fig, ax = plt.subplots(figsize=(14, 4.8))
    image = ax.imshow(coverage_matrix.to_numpy(), cmap="Blues", vmin=0, vmax=6, aspect="auto")
    ax.set_title("Manual general-metric force-bin coverage")
    ax.set_xlabel("Force-bin centre (N)")
    ax.set_ylabel("Target ROI")
    tick_positions = np.arange(0, len(coverage_matrix.columns), 4)
    ax.set_xticks(tick_positions, [f"{coverage_matrix.columns[i]:g}" for i in tick_positions], rotation=45, ha="right")
    ax.set_yticks(range(9), range(1, 10))
    fig.colorbar(image, ax=ax, label="Independent TEST sessions")
    outputs.update(save_figure(fig, figure_dir, "fig13_manual_force_bin_coverage", manual_coverage, meta(
        "Manual general-metric force-bin coverage", "manual_session_force_bins", "independent sessions", "54 (six per ROI)", "exact complete-session counts", ["manual TEST1-TEST6 only", "force <=10 N"]
    )))
    chart_map.append({"figure": "fig13_manual_force_bin_coverage", "family": "heatmap", "claim": "General-metric support is determined entirely from manual complete sessions."})

    local = tables["manual_local_sensitivity"].copy()
    local_plot = local[local["force_midpoint_N"].le(5)].copy()
    local_matrix = local_plot.pivot(index="roi", columns="force_midpoint_N", values="local_sensitivity_light_per_N").sort_index().sort_index(axis=1)
    scale = float(np.nanpercentile(np.abs(local_matrix.to_numpy(dtype=float)), 95))
    fig, ax = plt.subplots(figsize=(14, 4.8))
    image = ax.imshow(local_matrix.to_numpy(), cmap="coolwarm", vmin=-scale, vmax=scale, aspect="auto")
    ax.set_title("Manual adjacent-bin local sensitivity")
    ax.set_xlabel("Force midpoint (N)")
    ax.set_ylabel("Target ROI")
    tick_positions = np.arange(0, len(local_matrix.columns), 4)
    ax.set_xticks(tick_positions, [f"{local_matrix.columns[i]:.2f}" for i in tick_positions], rotation=45, ha="right")
    ax.set_yticks(range(9), range(1, 10))
    fig.colorbar(image, ax=ax, label="Local sensitivity (positive light sum/N)")
    outputs.update(save_figure(fig, figure_dir, "fig14_manual_local_sensitivity", local_plot, meta(
        "Manual adjacent-bin local sensitivity", "manual_local_sensitivity", "positive V-sum/N", "54 (six per ROI)", "adjacent supported-bin slope", ["manual only", "no interpolation across gaps", ">=3 sessions at both endpoints", "force midpoint <=5 N"]
    )))
    chart_map.append({"figure": "fig14_manual_local_sensitivity", "family": "diverging heatmap", "claim": "Local slopes retain negative and positive supported-bin behavior without filling force gaps."})

    plateau = tables["manual_plateau_observations"].copy()
    fig, ax = plt.subplots(figsize=(9.5, 4.8))
    ax.bar(plateau["roi"], plateau["supported_force_max_N"], color="#DBEAFE", edgecolor="#2563EB", label="Maximum supported manual bin")
    observed = plateau[plateau["plateau_onset_N"].notna()]
    if not observed.empty:
        ax.scatter(observed["roi"], observed["plateau_onset_N"], marker="D", color="#F59E0B", s=55, label="Frozen-rule plateau onset")
    else:
        ax.text(0.02, 0.96, "No sustained plateau observed under the frozen rule", transform=ax.transAxes, ha="left", va="top", color="#374151")
    ax.set_title("Manual supported range and plateau observations")
    ax.set_xlabel("ROI")
    ax.set_ylabel("Force-bin centre (N)")
    ax.set_xticks(range(1, 10))
    ax.legend(frameon=False)
    outputs.update(save_figure(fig, figure_dir, "fig15_manual_plateau_observations", plateau, meta(
        "Manual supported range and plateau observations", "manual_plateau_observations", "N", "54 (six per ROI)", "supported-bin maximum plus frozen sustained-plateau rule", ["manual only", ">=3 sessions/bin", "observation only; not a deployed or common range"]
    )))
    chart_map.append({"figure": "fig15_manual_plateau_observations", "family": "bar + point", "claim": "Per-ROI manual support and plateau observations replace the former automatic-derived common-range procedure."})

    lag_curve = tables["manual_system_lag_curve"]
    lag_plot = lag_curve.groupby(["roi", "lag_ms"], as_index=False).agg(
        independent_sessions=("session_id", "nunique"),
        median_absolute_correlation=("absolute_correlation", "median"),
        q25_absolute_correlation=("absolute_correlation", q25),
        q75_absolute_correlation=("absolute_correlation", q75),
    )
    lag_summary = tables["manual_system_lag_summary"].set_index("roi")
    fig, axes = plt.subplots(3, 3, figsize=(12, 10), sharex=True, sharey=True)
    for roi, ax in enumerate(axes.flat, 1):
        data = lag_plot[lag_plot["roi"].eq(roi)].sort_values("lag_ms")
        ax.plot(data["lag_ms"], data["median_absolute_correlation"], color="#2563EB", lw=1.4)
        ax.fill_between(data["lag_ms"], data["q25_absolute_correlation"], data["q75_absolute_correlation"], color="#DBEAFE", alpha=0.8)
        ax.axvline(float(lag_summary.loc[roi, "median_best_lag_ms"]), color="#F59E0B", ls="--", lw=1)
        ax.set_title(f"ROI {roi}")
        ax.set_xlabel("Lag (ms; + means optical follows)")
        ax.set_ylabel("|derivative correlation|")
    fig.suptitle("Manual-only camera/acquisition/load-cell/material system-lag curves", y=1.01, fontsize=13)
    outputs.update(save_figure(fig, figure_dir, "fig16_manual_system_lag", lag_plot, meta(
        "Manual-only system-lag curves", "manual_system_lag_curve", "ms and absolute correlation", "54 (six per ROI)", "session derivative cross-correlation; median and interquartile range across sessions", ["manual TEST1-TEST6 only", "positive lag means optical follows reference", f"search {MANUAL_LAG_MIN_MS} to {MANUAL_LAG_MAX_MS} ms", "not intrinsic material response time"]
    )))
    chart_map.append({"figure": "fig16_manual_system_lag", "family": "faceted lag curves", "claim": "The published standalone lag is manual-only and explicitly system-level."})

    for figure_number, displacement in [(17, 1.75), (18, 2.625)]:
        plot_h_extra = hysteresis[
            hysteresis["displacement_mm_abs"].eq(displacement)
        ]
        fig, axes = plt.subplots(3, 3, figsize=(13, 12), sharex=True)
        for roi, ax in enumerate(axes.flat, 1):
            for speed, data in plot_h_extra[plot_h_extra["roi"].eq(roi)].groupby("speed_mm_min"):
                data = data.sort_values("force_bin_N")
                ax.plot(data["force_bin_N"], data["median_loading_light"], color=colors[speed], lw=1.4, label=f"{int(speed)} load")
                ax.plot(data["force_bin_N"], data["median_unloading_light"], color=colors[speed], lw=1.2, ls="--", label=f"{int(speed)} unload")
            ax.set_title(f"ROI {roi}")
            ax.set_xlabel("Corrected force (N)")
            ax.set_ylabel("Cycle-then-session light")
        present_speeds = sorted(plot_h_extra["speed_mm_min"].dropna().unique())
        handles = [Line2D([0], [0], color=colors[speed], ls=phase_style) for speed in present_speeds for phase_style in ("-", "--")]
        labels = [f"{int(speed)} {phase_label}" for speed in present_speeds for phase_label in ("load", "unload")]
        fig.subplots_adjust(left=0.07, right=0.99, bottom=0.06, top=0.82, wspace=0.30, hspace=0.38)
        fig.legend(handles, labels, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 0.91))
        fig.suptitle(f"Automatic hysteresis at {displacement:g} mm, separated by speed", y=0.97, fontsize=13)
        figure_name = f"fig{figure_number:02d}_automatic_hysteresis_{str(displacement).replace('.', '_')}mm"
        outputs.update(save_figure(fig, figure_dir, figure_name, plot_h_extra, meta(
            f"Automatic hysteresis at {displacement:g} mm", "automatic_hysteresis", "positive V-sum and corrected N", "two sessions per exact condition", "cycle estimates averaged within session; across-session median", ["automatic only", f"absolute displacement={displacement:g} mm", "speeds shown separately", "cycle-local force zero and internal timing provenance retained"]
        ), tight_layout=False))
        chart_map.append({"figure": figure_name, "family": "faceted loop", "claim": "Loading/unloading loops are retained for every speed and displacement stratum."})

    return outputs, pd.DataFrame(chart_map)


def build_report_data(tables: dict[str, pd.DataFrame]) -> dict[str, Any]:
    status = tables["metric_status"]
    sensitivity = tables["manual_sensitivity_nonlinearity"]
    noise = tables["no_contact_summary"]
    detection = tables["manual_detection_summary"]
    proxy = tables["noise_equivalent_force_proxy"]
    lag = tables["manual_system_lag_summary"]
    return {
        "headline": {
            "sessions": 245,
            "frames": 179176,
            "manual_primary_sessions": 54,
            "automatic_sessions": 162,
            "no_contact_sessions": 11,
            "replay_only_sessions": 18,
            "established_or_qualified_metrics": int(status["status"].isin(["primary", "conditional-qualified"]).sum()),
            "not_established_metrics": int(status["status"].eq("not-established").sum()),
        },
        "ranges": {
            "manual_low_force_sensitivity_light_per_N": [finite(sensitivity["low_force_sensitivity_light_per_N"].min()), finite(sensitivity["low_force_sensitivity_light_per_N"].max())],
            "no_contact_signed_noise_mad": [finite(noise["signed_noise_mad"].min()), finite(noise["signed_noise_mad"].max())],
            "detection_region_N": [finite(detection["optical_detection_region_N"].min()), finite(detection["optical_detection_region_N"].max())],
            "noise_equivalent_force_proxy_N": [finite(proxy["noise_equivalent_force_N"].min()), finite(proxy["noise_equivalent_force_N"].max())],
            "manual_system_lag_median_by_roi_ms": [finite(lag["median_best_lag_ms"].min()), finite(lag["median_best_lag_ms"].max())],
        },
        "metric_status": status.astype(object).where(pd.notna(status), None).to_dict("records"),
    }


def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    feature_store = (project_root / args.feature_store).resolve()
    prior_run = (project_root / args.prior_run).resolve()
    master_plan = Path(args.master_plan).resolve()
    source_audit = (project_root / args.source_audit).resolve()
    output = (project_root / args.output).resolve()
    table_dir = output / "tables"
    figure_dir = output / "figures"
    output.mkdir(parents=True, exist_ok=True)
    if not master_plan.exists():
        raise FileNotFoundError(f"revised master plan not found: {master_plan}")

    reconciliation = read_json(feature_store / "reconciliation_report.json")
    audit = read_json(source_audit / "audit_report.json")
    prior_summary = read_json(prior_run / "characterization_summary.json")
    if not reconciliation.get("gate_pass") or not audit.get("gate_pass"):
        raise ValueError("upstream archive or feature-store gate did not pass")
    if prior_summary.get("run_id") != "characterization-dade466f019e6d15":
        raise ValueError("authoritative preserved characterization run not found")
    if prior_summary.get("source_manifest_hash") != reconciliation.get("source_manifest_hash"):
        raise ValueError("source manifest conflict between upstream artifacts")
    source_hash = str(reconciliation["source_manifest_hash"])
    code_hash = sha256_file(Path(__file__))
    master_plan_hash = sha256_file(master_plan)

    frames = load_features(feature_store)
    tables = coverage_tables(frames)
    no_contact, floors = no_contact_tables(frames)
    tables.update(no_contact)
    frames = add_lights(frames, floors)
    tables.update(manual_response_tables(frames, tables["no_contact_summary"]))
    tables.update(detection_and_snr(frames, tables["manual_sensitivity_nonlinearity"]))
    tables.update(cross_talk(frames))
    tables.update(automatic_cycle_tables(frames))
    tables.update(manual_lag_tables(frames))
    prior_plateau = pd.read_parquet(prior_run / "plateau_summary.parquet")
    tables["metric_status"] = metric_status_table(tables, prior_plateau)

    table_hashes: dict[str, str] = {}
    for name, table in sorted(tables.items()):
        table_hashes.update(write_table(table_dir, name, table))

    figure_hashes, chart_map = build_figures(tables, figure_dir, source_hash, code_hash)
    table_hashes.update(write_table(table_dir, "chart_map", chart_map))
    report_data = build_report_data(tables)
    write_json(output / "report_data.json", report_data)

    archive_inventory = read_json(source_audit / "archive_inventory.json")
    quality_checks = {
        "archive_hashes_match": bool(audit["gate_checks"]["all_archive_hashes_match"]),
        "archive_member_crc_pass": bool(audit["gate_checks"]["all_archive_member_crcs_pass"]),
        "all_245_sessions_present": frames["session_id"].nunique() == 245,
        "all_179176_frames_present": len(frames) == 179176,
        "manual_primary_sessions_54": frames.loc[frames["scientific_role"].eq("model_primary"), "session_id"].nunique() == 54,
        "manual_no_contact_sessions_11": frames.loc[frames["scientific_role"].eq("no_contact"), "session_id"].nunique() == 11,
        "manual_replay_sessions_18": frames.loc[frames["scientific_role"].eq("replay_only"), "session_id"].nunique() == 18,
        "automatic_sessions_162": frames.loc[frames["archive_id"].eq("automated"), "session_id"].nunique() == 162,
        "automatic_exact_conditions_two_sessions": bool(tables["coverage_automatic_conditions"]["independent_sessions"].eq(2).all()),
        "automatic_conditions_have_one_10_and_one_20_cycle_session": bool(
            tables["coverage_automatic_conditions"]["ten_cycle_sessions"].eq(1).all()
            and tables["coverage_automatic_conditions"]["twenty_cycle_sessions"].eq(1).all()
        ),
        "manual_six_sessions_per_roi": bool(tables["coverage_manual_sessions"].query("scientific_role == 'model_primary'").groupby("target_roi")["session_id"].nunique().eq(6).all()),
        "cross_talk_complete_9x9": tables["cross_talk"][["target_roi", "measured_roi"]].drop_duplicates().shape[0] == 81,
        "cycles_nested_within_session": not tables["automatic_cycle_force_bins"].duplicated(["session_id", "cycle_index", "motion_phase", "force_bin_N"]).any(),
        "general_metrics_manual_only": bool(
            tables["metric_status"].loc[
                ~tables["metric_status"]["metric"].isin(
                    [
                        "Archive integrity and original-frame reconciliation",
                        "Hysteresis",
                        "Hysteresis internal cycle and correction QA",
                        "Creep-related fixed-displacement settling and unloaded recovery",
                        "Historical former combined common-range / monotonicity gate",
                        "True creep",
                        "True force resolution",
                    ]
                ),
                "source",
            ].str.contains("manual", case=False).all()
        ),
        "standalone_lag_manual_only": tables["manual_system_lag_session"]["archive_id"].eq("manual").all(),
        "automatic_outputs_role_limited": all(
            table["evidence_role"].isin(["hysteresis supporting component", "creep-related exploratory evidence"]).all()
            for name, table in tables.items()
            if name.startswith("automatic_") and "evidence_role" in table.columns
        ),
        "creep_not_claimed": not tables["metric_status"].query("metric == 'True creep'")["status"].eq("primary").any(),
        "true_resolution_not_claimed": not tables["metric_status"].query("metric == 'True force resolution'")["status"].eq("primary").any(),
        "failed_prior_gate_preserved": prior_summary.get("gate_pass") is False,
        "all_figure_companions_present": len(figure_hashes) == 18 * 5,
    }
    quality = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "gate_pass": all(quality_checks.values()),
        "checks": quality_checks,
        "source_manifest_hash": source_hash,
        "feature_store_hash": sha256_file(feature_store / "reconciliation_report.json"),
        "prior_characterization_summary_hash": sha256_file(prior_run / "characterization_summary.json"),
        "code_hash": code_hash,
        "master_plan_hash": master_plan_hash,
    }
    write_json(output / "quality_report.json", quality)
    if not quality["gate_pass"]:
        raise ValueError(f"evidence-product quality gate failed: {quality_checks}")

    run_manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"characterization-evidence-{json_hash({'source': source_hash, 'code': code_hash})[:16]}",
        "created_at": utc_now(),
        "command": "build-characterization-evidence",
        "argv": sys.argv,
        "source_manifest_hash": source_hash,
        "code_hash": code_hash,
        "inputs": {
            "archive_inventory": archive_inventory,
            "feature_store": str(feature_store),
            "preserved_characterization_run": str(prior_run),
            "source_audit": str(source_audit),
            "revised_master_plan": str(master_plan),
            "revised_master_plan_sha256": master_plan_hash,
        },
        "outputs": {
            "tables": table_hashes,
            "figures": figure_hashes,
            "report_data.json": sha256_file(output / "report_data.json"),
            "quality_report.json": sha256_file(output / "quality_report.json"),
        },
        "qualification": {
            "automatic_condition_intervals": "descriptive; exactly two independent sessions per condition",
            "collection_day_intervals": "descriptive; two automatic collection days",
            "true_creep": "not established",
            "true_force_resolution": "not established",
            "noise_equivalent_force": "stability proxy only",
            "approximate_lag": "manual-only system-level exploratory estimate",
            "automatic_archive_role": "hysteresis and creep-related analysis only",
            "manual_archive_role": "all general characterization metrics",
            "historical_common_supported_force_range": "former combined-procedure failure preserved as historical only",
        },
        "gate_pass": True,
    }
    write_json(output / "run_manifest.json", run_manifest)
    print(json.dumps({"output": str(output), "run_id": run_manifest["run_id"], "figures": 18, "gate_pass": True}, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--feature-store",
        default="analysis_outputs/live_sensor_study/feature_store/feature-store-c0ec762f1dc888a7",
    )
    value.add_argument(
        "--prior-run",
        default="analysis_outputs/live_sensor_study/characterization/characterization-dade466f019e6d15",
    )
    value.add_argument(
        "--source-audit",
        default="analysis_outputs/live_sensor_study/source_inventory/archive-audit-a3f75ebef480d214",
    )
    value.add_argument(
        "--master-plan",
        default=r"C:\Users\DLSU\Downloads\THESIS DATA\Data Collection\SENSOR_CHARACTERIZATION_MASTER_PLAN.md",
    )
    value.add_argument(
        "--output",
        default="analysis_outputs/live_sensor_study/characterization/evidence-products-manual-first-authoritative",
    )
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
