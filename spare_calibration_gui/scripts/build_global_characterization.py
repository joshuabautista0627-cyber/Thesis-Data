"""Build a transparent global sensor-characterization sensitivity analysis.

The global optical output is the sum of all nine no-contact-floor-corrected
positive ROI signals.  Manual sessions control every general metric.  Results
are balanced within session, then within target ROI, then equally across all
nine targets.  No ROI is dropped or sign-flipped, no synthetic rows are added,
and the per-ROI authoritative evidence package remains unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

import build_characterization_evidence as base


SCHEMA_VERSION = "1.0.0"
SEED = 20260814
BOOTSTRAP_REPLICATES = 1000
COMPARATOR_FORCE_MIN_N = 0.0
COMPARATOR_FORCE_MAX_N = 5.0
ALPHA_GRID = (0.01, 0.1, 1.0, 10.0, 100.0)


def global_manual_tables(frames: pd.DataFrame) -> dict[str, pd.DataFrame]:
    source = frames[
        frames["scientific_role"].eq("model_primary")
        & frames["force_valid"]
        & frames["reference_force_corrected_N"].notna()
    ].copy()
    source["force_bin_N"] = base.force_bins(source["reference_force_corrected_N"])
    source = source[source["force_bin_N"].between(base.FORCE_BIN_WIDTH_N / 2, 15.0)]
    source["global_signed_light"] = source[
        [f"roi{roi}_signed_delta_v_sum" for roi in range(1, 10)]
    ].sum(axis=1)

    aggregations: dict[str, tuple[str, str]] = {
        "frames": ("video_frame_index", "size"),
        "median_force_N": ("reference_force_corrected_N", "median"),
        "median_total_light": ("total_light", "median"),
        "median_global_signed_light": ("global_signed_light", "median"),
    }
    for roi in range(1, 10):
        aggregations[f"median_light_{roi}"] = (f"light_{roi}", "median")
    session_phase_bins = source.groupby(
        ["session_id", "test_group", "target_roi", "motion_phase", "force_bin_N"], as_index=False
    ).agg(**aggregations)
    session_bins = session_phase_bins.groupby(
        ["session_id", "test_group", "target_roi", "force_bin_N"], as_index=False
    ).agg(
        frames=("frames", "sum"),
        median_force_N=("median_force_N", "median"),
        median_total_light=("median_total_light", "median"),
        median_global_signed_light=("median_global_signed_light", "median"),
        **{f"median_light_{roi}": (f"median_light_{roi}", "median") for roi in range(1, 10)},
    )
    session_bins["data_origin"] = "empirical-manual"

    target_response = session_bins.groupby(["target_roi", "force_bin_N"], as_index=False).agg(
        independent_sessions=("session_id", "nunique"),
        median_force_N=("median_force_N", "median"),
        median_total_light=("median_total_light", "median"),
        q25_total_light=("median_total_light", base.q25),
        q75_total_light=("median_total_light", base.q75),
    )
    target_response["supported"] = target_response["independent_sessions"].ge(3)
    supported = target_response[target_response["supported"]].copy()
    response = supported.groupby("force_bin_N", as_index=False).agg(
        contributing_target_roi=("target_roi", "nunique"),
        minimum_sessions_per_target=("independent_sessions", "min"),
        median_force_N=("median_force_N", "median"),
        global_median_total_light=("median_total_light", "median"),
        q25_target_total_light=("median_total_light", base.q25),
        q75_target_total_light=("median_total_light", base.q75),
        minimum_target_total_light=("median_total_light", "min"),
        maximum_target_total_light=("median_total_light", "max"),
    )
    response["all_nine_targets"] = response["contributing_target_roi"].eq(9)
    response["aggregation_order"] = "frame median -> session -> target ROI median -> equal-ROI median"

    low = response[response["all_nine_targets"] & response["force_bin_N"].le(base.LOW_FORCE_MAX_N)]
    if len(low) < 3:
        raise ValueError("global low-force response lacks three all-target supported bins")
    slope, intercept = np.polyfit(low["median_force_N"], low["global_median_total_light"], 1)
    fitted = intercept + slope * low["median_force_N"].to_numpy(dtype=float)
    span = float(low["global_median_total_light"].max() - low["global_median_total_light"].min())
    nonlinearity = (
        float(np.max(np.abs(low["global_median_total_light"].to_numpy(dtype=float) - fitted)) / span * 100)
        if span > 0 else float("nan")
    )

    rng = np.random.default_rng(SEED)
    bootstrap_rows: list[dict[str, float]] = []
    low_bins = sorted(low["force_bin_N"].unique())
    low_force_axis = (
        low.set_index("force_bin_N").loc[low_bins, "median_force_N"].to_numpy(dtype=float)
    )
    target_matrices: dict[int, np.ndarray] = {}
    for target_roi in range(1, 10):
        matrix = (
            session_bins[
                session_bins["target_roi"].eq(target_roi)
                & session_bins["force_bin_N"].isin(low_bins)
            ]
            .pivot(index="session_id", columns="force_bin_N", values="median_total_light")
            .reindex(columns=low_bins)
            .dropna(how="all")
            .to_numpy(dtype=float)
        )
        if len(matrix) < 3 or np.any(np.sum(np.isfinite(matrix), axis=0) < 3):
            raise ValueError(f"target ROI {target_roi} lacks three supported sessions in a low-force bin")
        target_matrices[target_roi] = matrix
    for replicate in range(BOOTSTRAP_REPLICATES):
        target_curves = []
        for target_roi in rng.choice(np.arange(1, 10), size=9, replace=True):
            matrix = target_matrices[int(target_roi)]
            sampled = None
            for _ in range(50):
                row_indices = rng.integers(0, len(matrix), size=len(matrix))
                candidate = matrix[row_indices]
                if np.all(np.sum(np.isfinite(candidate), axis=0) >= 3):
                    sampled = candidate
                    break
            if sampled is None:
                sampled = matrix
            target_curves.append(np.nanmedian(sampled, axis=0))
        global_curve = np.median(np.vstack(target_curves), axis=0)
        boot_slope, _ = np.polyfit(low_force_axis, global_curve, 1)
        bootstrap_rows.append({"replicate": replicate, "low_force_sensitivity_light_per_N": float(boot_slope)})
    bootstrap = pd.DataFrame(bootstrap_rows)
    ci_low, ci_high = np.percentile(bootstrap["low_force_sensitivity_light_per_N"], [2.5, 97.5])

    all_supported = response[response["all_nine_targets"]].sort_values("force_bin_N")
    peak = float(all_supported["global_median_total_light"].max())
    plateau_onset = float("nan")
    plateau_bins = 0
    if len(all_supported) >= 3 and peak > 0:
        threshold = base.PLATEAU_TAIL_FRACTION * peak
        minimum_span = base.PLATEAU_TAIL_SPAN_FRACTION * float(
            all_supported["force_bin_N"].max() - all_supported["force_bin_N"].min()
        )
        above = all_supported["global_median_total_light"].ge(threshold).to_numpy()
        bins = all_supported["force_bin_N"].to_numpy(dtype=float)
        for start in range(len(above) - 2):
            if not above[start]:
                continue
            stop = start
            while stop + 1 < len(above) and above[stop + 1] and math.isclose(
                bins[stop + 1] - bins[stop], base.FORCE_BIN_WIDTH_N, abs_tol=1e-9
            ):
                stop += 1
            if stop - start + 1 >= 3 and bins[stop] - bins[start] >= minimum_span:
                plateau_onset = float(bins[start])
                plateau_bins = int(stop - start + 1)
                break

    signal = session_bins[session_bins["force_bin_N"].between(0.75, 1.25)]
    signal_session = signal.groupby(["session_id", "target_roi"], as_index=False).agg(
        median_force_N=("median_force_N", "median"),
        median_total_light=("median_total_light", "median"),
    )
    target_signal = signal_session.groupby("target_roi", as_index=False).agg(
        independent_sessions=("session_id", "nunique"),
        median_total_light=("median_total_light", "median"),
        q25_total_light=("median_total_light", base.q25),
        q75_total_light=("median_total_light", base.q75),
    )
    target_signal["global_equal_target_median"] = float(target_signal["median_total_light"].median())

    summary = pd.DataFrame([{
        "metric_scope": "global-total-light",
        "global_output_definition": "sum of all nine no-contact-floor-corrected positive ROI light sums",
        "independent_manual_sessions": int(session_bins["session_id"].nunique()),
        "target_roi": int(session_bins["target_roi"].nunique()),
        "low_force_interval_N": "0-1",
        "supported_low_force_bins": int(len(low)),
        "global_low_force_sensitivity_light_per_N": float(slope),
        "bootstrap_95_ci_low_light_per_N": float(ci_low),
        "bootstrap_95_ci_high_light_per_N": float(ci_high),
        "nonlinearity_percent_supported_low_force_span": nonlinearity,
        "common_supported_force_max_N": float(all_supported["force_bin_N"].max()),
        "plateau_onset_N": plateau_onset,
        "plateau_consecutive_bins": plateau_bins,
        "plateau_observation": "observed-under-frozen-rule" if np.isfinite(plateau_onset) else "not-observed-under-frozen-rule",
        "claim_boundary": "global contact/force proxy only; does not establish per-ROI or localization performance",
        "data_origin": "empirical-manual",
    }])
    return {
        "global_manual_session_force_bins": session_bins,
        "global_manual_target_response": target_response,
        "global_manual_force_response": response,
        "global_manual_sensitivity_bootstrap": bootstrap,
        "global_target_signal_heterogeneity": target_signal,
        "global_metric_summary": summary,
    }


def global_noise_detection_lag(
    frames: pd.DataFrame,
    session_bins: pd.DataFrame,
    metric_summary: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    work = frames.copy()
    work["global_signed_light"] = work[[f"roi{roi}_signed_delta_v_sum" for roi in range(1, 10)]].sum(axis=1)
    no_contact = work[work["scientific_role"].eq("no_contact")].copy()
    noise_rows: list[dict[str, Any]] = []
    for session_id, group in no_contact.groupby("session_id", sort=True):
        time_s = group["capture_monotonic_relative_s"].to_numpy(dtype=float)
        signed = group["global_signed_light"].to_numpy(dtype=float)
        valid = np.isfinite(time_s) & np.isfinite(signed)
        slope_per_min = (
            float(np.polyfit(time_s[valid], signed[valid], 1)[0] * 60)
            if valid.sum() >= 10 and np.ptp(time_s[valid]) > 0 else float("nan")
        )
        noise_rows.append({
            "session_id": session_id,
            "frames": int(len(group)),
            "median_global_signed_light": float(np.median(signed)),
            "global_signed_light_mad": base.mad(signed),
            "median_global_total_light": float(group["total_light"].median()),
            "global_total_light_mad": base.mad(group["total_light"]),
            "global_signed_drift_light_per_min": slope_per_min,
            "data_origin": "empirical-manual-no-contact",
        })
    noise_session = pd.DataFrame(noise_rows)
    noise_summary = pd.DataFrame([{
        "independent_sessions": int(noise_session["session_id"].nunique()),
        "median_global_signed_light_mad": float(noise_session["global_signed_light_mad"].median()),
        "median_global_total_light_mad": float(noise_session["global_total_light_mad"].median()),
        "median_abs_global_signed_drift_light_per_min": float(noise_session["global_signed_drift_light_per_min"].abs().median()),
        "maximum_abs_global_signed_drift_light_per_min": float(noise_session["global_signed_drift_light_per_min"].abs().max()),
        "data_origin": "empirical-manual-no-contact",
    }])

    no_contact_threshold = float(np.quantile(
        noise_session["median_global_total_light"], 1 - base.DETECTION_FPR, method="higher"
    ))
    press = session_bins[session_bins["force_bin_N"].le(3.0)].copy()
    press["detected"] = press["median_total_light"].gt(no_contact_threshold)
    detail_rows: list[dict[str, Any]] = []
    for force_bin, group in press.groupby("force_bin_N", sort=True):
        by_target = group.groupby("target_roi").agg(
            sessions=("session_id", "nunique"), recall=("detected", "mean")
        )
        all_nine = len(by_target) == 9 and bool(by_target["sessions"].ge(3).all())
        overall = float(group["detected"].mean())
        worst = float(by_target["recall"].min()) if len(by_target) else float("nan")
        detail_rows.append({
            "force_bin_N": float(force_bin),
            "independent_sessions": int(group["session_id"].nunique()),
            "contributing_target_roi": int(len(by_target)),
            "minimum_sessions_per_target": int(by_target["sessions"].min()) if len(by_target) else 0,
            "global_total_light_threshold": no_contact_threshold,
            "overall_recall": overall,
            "median_target_recall": float(by_target["recall"].median()) if len(by_target) else float("nan"),
            "minimum_target_recall": worst,
            "all_nine_targets_supported": all_nine,
            "passes_average_only": bool(all_nine and overall >= base.DETECTION_RECALL),
            "passes_device_wide_guardrail": bool(all_nine and overall >= base.DETECTION_RECALL and worst >= base.DETECTION_RECALL),
        })
    detection_detail = pd.DataFrame(detail_rows)
    average_supported = detection_detail[detection_detail["passes_average_only"]]
    guarded_supported = detection_detail[detection_detail["passes_device_wide_guardrail"]]
    average_region = float(average_supported["force_bin_N"].min()) if len(average_supported) else float("nan")
    guarded_region = float(guarded_supported["force_bin_N"].min()) if len(guarded_supported) else float("nan")

    signal = press[press["force_bin_N"].between(0.75, 1.25)]
    median_signal = float(signal.groupby("session_id")["median_total_light"].median().median())
    noise_mad = float(noise_summary["median_global_signed_light_mad"].iloc[0])
    snr_linear = median_signal / noise_mad if noise_mad > 0 else float("nan")
    sensitivity = float(metric_summary["global_low_force_sensitivity_light_per_N"].iloc[0])
    detection_summary = pd.DataFrame([{
        "average_global_detection_region_N": average_region,
        "device_wide_guarded_detection_region_N": guarded_region,
        "frozen_empirical_fpr": base.DETECTION_FPR,
        "required_recall": base.DETECTION_RECALL,
        "independent_no_contact_sessions": int(len(noise_session)),
        "median_contact_total_light_0_75_to_1_25_N": median_signal,
        "global_signed_noise_mad": noise_mad,
        "global_snr_linear": snr_linear,
        "global_snr_dB_amplitude_convention": 20 * math.log10(snr_linear) if snr_linear > 0 else float("nan"),
        "global_noise_equivalent_force_proxy_N": noise_mad / sensitivity if sensitivity > 0 else float("nan"),
        "claim_boundary": "average-only detection cannot be promoted to device-wide detection unless worst-target recall also passes",
        "data_origin": "empirical-manual",
    }])

    lag_input = work.copy()
    lag_input["target_signed_light"] = lag_input["total_light"]
    lag_tables = base.manual_lag_tables(lag_input)
    lag_session = lag_tables["manual_system_lag_session"].copy()
    lag_curve = lag_tables["manual_system_lag_curve"].copy()
    lag_summary = pd.DataFrame([{
        "independent_sessions": int(lag_session["session_id"].nunique()),
        "target_roi": int(lag_session["roi"].nunique()),
        "median_best_lag_ms": float(lag_session["best_lag_ms"].median()),
        "minimum_best_lag_ms": float(lag_session["best_lag_ms"].min()),
        "maximum_best_lag_ms": float(lag_session["best_lag_ms"].max()),
        "median_best_absolute_correlation": float(lag_session["best_absolute_correlation"].median()),
        "boundary_session_fraction": float(lag_session["lag_at_search_boundary"].mean()),
        "sign_convention": "positive lag means global optical output follows reference",
        "claim_boundary": "global camera+acquisition+synchronization+material system lag; not intrinsic response time",
        "data_origin": "empirical-manual",
    }])
    lag_curve_summary = lag_curve.groupby("lag_ms", as_index=False).agg(
        independent_sessions=("session_id", "nunique"),
        median_absolute_correlation=("absolute_correlation", "median"),
        q25_absolute_correlation=("absolute_correlation", base.q25),
        q75_absolute_correlation=("absolute_correlation", base.q75),
    )
    return {
        "global_no_contact_session": noise_session,
        "global_no_contact_summary": noise_summary,
        "global_detection_detail": detection_detail,
        "global_detection_summary": detection_summary,
        "global_system_lag_curve": lag_curve,
        "global_system_lag_curve_summary": lag_curve_summary,
        "global_system_lag_session": lag_session,
        "global_system_lag_summary": lag_summary,
    }


def ridge_predict(
    train_x: np.ndarray,
    train_y: np.ndarray,
    test_x: np.ndarray,
    alpha: float,
) -> np.ndarray:
    mean = np.nanmean(train_x, axis=0)
    scale = np.nanstd(train_x, axis=0)
    scale = np.where(scale > 0, scale, 1.0)
    x_train = (train_x - mean) / scale
    x_test = (test_x - mean) / scale
    y_mean = float(np.mean(train_y))
    centred_y = train_y - y_mean
    identity = np.eye(x_train.shape[1])
    coefficients = np.linalg.solve(x_train.T @ x_train + alpha * identity, x_train.T @ centred_y)
    return y_mean + x_test @ coefficients


def choose_alpha(data: pd.DataFrame, features: list[str], outer_group: str) -> float:
    train = data[data["test_group"].ne(outer_group)]
    inner_groups = sorted(train["test_group"].unique())
    scores: list[tuple[float, float]] = []
    for alpha in ALPHA_GRID:
        errors = []
        for inner_group in inner_groups:
            inner_train = train[train["test_group"].ne(inner_group)]
            inner_test = train[train["test_group"].eq(inner_group)]
            prediction = ridge_predict(
                inner_train[features].to_numpy(dtype=float),
                inner_train["median_force_N"].to_numpy(dtype=float),
                inner_test[features].to_numpy(dtype=float),
                alpha,
            )
            errors.append(float(np.mean(np.abs(prediction - inner_test["median_force_N"].to_numpy(dtype=float)))))
        scores.append((float(np.mean(errors)), alpha))
    return min(scores)[1]


def global_model_benchmark(
    session_bins: pd.DataFrame,
    force_min_N: float,
    force_max_N: float,
    evaluation_scope: str,
) -> dict[str, pd.DataFrame]:
    raw_features = [f"median_light_{roi}" for roi in range(1, 10)]
    log_features = [f"log_light_{roi}" for roi in range(1, 10)]
    zeroed_features = [f"zeroed_light_{roi}" for roi in range(1, 10)]
    baseline_columns = ["median_total_light", *raw_features]
    # Baseline initialization is computed before the evaluation-range filter so
    # the live-range rerun still uses each session's lowest observed load rather
    # than treating the 1.7 N lower operating limit as a zero-load baseline.
    baseline = (
        session_bins.sort_values("force_bin_N")
        .groupby("session_id", as_index=False)
        .first()[["session_id", *baseline_columns]]
        .rename(columns={column: f"baseline_{column}" for column in baseline_columns})
    )
    data = session_bins[
        session_bins["median_force_N"].between(force_min_N, force_max_N, inclusive="both")
    ].copy()
    if data.empty:
        raise ValueError(f"no global model rows inside {force_min_N:g}-{force_max_N:g} N")
    data = data.merge(baseline, on="session_id", how="left", validate="many_to_one")
    data["zeroed_total_light"] = data["median_total_light"] - data["baseline_median_total_light"]
    for raw, logged in zip(raw_features, log_features, strict=True):
        data[logged] = np.log1p(data[raw].clip(lower=0))
    for raw, zeroed in zip(raw_features, zeroed_features, strict=True):
        data[zeroed] = data[raw] - data[f"baseline_{raw}"]
    data["total_light_squared"] = data["median_total_light"] ** 2
    predictions: list[dict[str, Any]] = []
    for outer_group in sorted(data["test_group"].unique()):
        train = data[data["test_group"].ne(outer_group)]
        test = data[data["test_group"].eq(outer_group)]
        model_specs = [
            ("constant_baseline", [], None),
            ("total_light_linear", ["median_total_light"], 0.01),
            ("total_light_quadratic", ["median_total_light", "total_light_squared"], choose_alpha(data, ["median_total_light", "total_light_squared"], outer_group)),
            ("session_zeroed_total_linear", ["zeroed_total_light"], 0.01),
            ("nine_channel_ridge", raw_features, choose_alpha(data, raw_features, outer_group)),
            ("nine_channel_log_ridge", log_features, choose_alpha(data, log_features, outer_group)),
            ("session_zeroed_nine_channel_ridge", zeroed_features, choose_alpha(data, zeroed_features, outer_group)),
        ]
        for model, features, alpha in model_specs:
            if model == "constant_baseline":
                predicted = np.full(len(test), float(train["median_force_N"].median()))
            else:
                predicted = ridge_predict(
                    train[features].to_numpy(dtype=float),
                    train["median_force_N"].to_numpy(dtype=float),
                    test[features].to_numpy(dtype=float),
                    float(alpha),
                )
            for row, estimate in zip(test.itertuples(index=False), predicted, strict=True):
                predictions.append({
                    "model": model,
                    "outer_test_group": outer_group,
                    "evaluation_scope": evaluation_scope,
                    "force_min_N": force_min_N,
                    "force_max_N": force_max_N,
                    "selected_alpha": alpha,
                    "session_id": row.session_id,
                    "target_roi": int(row.target_roi),
                    "force_bin_N": float(row.force_bin_N),
                    "observed_force_N": float(row.median_force_N),
                    "predicted_force_N": float(estimate),
                    "absolute_error_N": float(abs(estimate - row.median_force_N)),
                    "squared_error_N2": float((estimate - row.median_force_N) ** 2),
                    "within_0_5_N": bool(abs(estimate - row.median_force_N) <= 0.5),
                    "data_origin": "empirical-manual-held-out-test-group",
                })
    prediction = pd.DataFrame(predictions)
    session_metric = prediction.groupby(["model", "session_id", "target_roi"], as_index=False).agg(
        bins=("force_bin_N", "nunique"),
        session_mae_N=("absolute_error_N", "mean"),
        session_rmse_N=("squared_error_N2", lambda x: float(np.sqrt(np.mean(x)))),
        session_within_0_5_N=("within_0_5_N", "mean"),
    )
    target_metric = session_metric.groupby(["model", "target_roi"], as_index=False).agg(
        sessions=("session_id", "nunique"),
        median_session_mae_N=("session_mae_N", "median"),
        median_session_rmse_N=("session_rmse_N", "median"),
        median_session_within_0_5_N=("session_within_0_5_N", "median"),
    )
    summary_rows = []
    for model, group in prediction.groupby("model", sort=True):
        session = session_metric[session_metric["model"].eq(model)]
        target = target_metric[target_metric["model"].eq(model)]
        observed = group["observed_force_N"].to_numpy(dtype=float)
        predicted = group["predicted_force_N"].to_numpy(dtype=float)
        denominator = float(np.sum((observed - observed.mean()) ** 2))
        summary_rows.append({
            "model": model,
            "held_out_test_groups": int(group["outer_test_group"].nunique()),
            "independent_sessions": int(group["session_id"].nunique()),
            "target_roi": int(group["target_roi"].nunique()),
            "force_interval_N": f"{force_min_N:g}-{force_max_N:g}",
            "evaluation_scope": evaluation_scope,
            "prediction_rows": int(len(group)),
            "mae_N": float(group["absolute_error_N"].mean()),
            "rmse_N": float(np.sqrt(group["squared_error_N2"].mean())),
            "r2": 1 - float(np.sum((observed - predicted) ** 2)) / denominator if denominator > 0 else float("nan"),
            "median_session_mae_N": float(session["session_mae_N"].median()),
            "q25_session_mae_N": base.q25(session["session_mae_N"]),
            "q75_session_mae_N": base.q75(session["session_mae_N"]),
            "worst_target_median_session_mae_N": float(target["median_session_mae_N"].max()),
            "within_0_5_N_fraction": float(group["within_0_5_N"].mean()),
            "validation_design": "outer holdout by TEST1-TEST6; ridge alpha selected only within outer training data",
            "claim_boundary": "operational global force benchmark; does not replace characterization evidence",
        })
    summary = pd.DataFrame(summary_rows).sort_values("median_session_mae_N")
    return {
        "global_model_cv_predictions": prediction,
        "global_model_cv_session_metrics": session_metric,
        "global_model_cv_target_metrics": target_metric,
        "global_model_cv_summary": summary,
    }


def _r2(observed: np.ndarray, predicted: np.ndarray, weights: np.ndarray | None = None) -> float:
    observed = np.asarray(observed, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    if weights is None:
        mean = float(np.mean(observed))
        numerator = float(np.sum((observed - predicted) ** 2))
        denominator = float(np.sum((observed - mean) ** 2))
    else:
        weights = np.asarray(weights, dtype=float)
        mean = float(np.average(observed, weights=weights))
        numerator = float(np.sum(weights * (observed - predicted) ** 2))
        denominator = float(np.sum(weights * (observed - mean) ** 2))
    return 1.0 - numerator / denominator if denominator > 0 else float("nan")


def live_sensor_operating_range_metrics(
    predictions: pd.DataFrame,
    force_min_N: float,
    force_max_N: float,
) -> pd.DataFrame:
    required = {
        "outer_fold", "session_id", "target_roi", "force_N",
        "conditional_force_N", "displayed_force_N", "constant_force_N",
    }
    if not required.issubset(predictions.columns):
        raise ValueError(f"live-sensor predictions missing columns: {sorted(required - set(predictions.columns))}")
    work = predictions[predictions["force_N"].between(force_min_N, force_max_N, inclusive="both")].copy()
    if len(work) != len(predictions):
        raise ValueError("live-sensor outer predictions leave the configured operating range")
    work["force_bin_N"] = base.force_bins(work["force_N"])
    rows: list[dict[str, Any]] = []
    estimators = {
        "live_sensor_conditional_isotonic": "conditional_force_N",
        "live_sensor_displayed_state": "displayed_force_N",
        "live_sensor_fold_constant": "constant_force_N",
    }
    for estimator, prediction_column in estimators.items():
        observed = work["force_N"].to_numpy(dtype=float)
        predicted = work[prediction_column].to_numpy(dtype=float)
        session_sizes = work.groupby("session_id")["session_id"].transform("size").to_numpy(dtype=float)
        folds = work.groupby("outer_fold", sort=True).apply(
            lambda group: _r2(
                group["force_N"].to_numpy(dtype=float),
                group[prediction_column].to_numpy(dtype=float),
            ),
            include_groups=False,
        )
        binned = work.groupby(
            ["outer_fold", "session_id", "target_roi", "force_bin_N"], as_index=False
        ).agg(
            observed_force_N=("force_N", "median"),
            predicted_force_N=(prediction_column, "median"),
        )
        binned["absolute_error_N"] = (
            binned["predicted_force_N"] - binned["observed_force_N"]
        ).abs()
        binned["within_0_5_N"] = binned["absolute_error_N"].le(0.5)
        session_metric = binned.groupby(["session_id", "target_roi"], as_index=False).agg(
            session_mae_N=("absolute_error_N", "mean")
        )
        target_metric = session_metric.groupby("target_roi", as_index=False).agg(
            median_session_mae_N=("session_mae_N", "median")
        )
        rows.append({
            "estimator": estimator,
            "force_interval_N": f"{force_min_N:g}-{force_max_N:g}",
            "evaluation_scope": "exact_live_sensor_outer_predictions",
            "frame_rows": int(len(work)),
            "session_force_bin_rows": int(len(binned)),
            "held_out_test_groups": int(work["outer_fold"].nunique()),
            "independent_sessions": int(work["session_id"].nunique()),
            "target_roi": int(work["target_roi"].nunique()),
            "pooled_frame_r2": _r2(observed, predicted),
            "session_balanced_frame_r2": _r2(observed, predicted, 1.0 / session_sizes),
            "mean_outer_fold_frame_r2": float(folds.mean()),
            "minimum_outer_fold_frame_r2": float(folds.min()),
            "maximum_outer_fold_frame_r2": float(folds.max()),
            "session_force_bin_r2": _r2(
                binned["observed_force_N"].to_numpy(dtype=float),
                binned["predicted_force_N"].to_numpy(dtype=float),
            ),
            "frame_mae_N": float(np.mean(np.abs(predicted - observed))),
            "frame_rmse_N": float(np.sqrt(np.mean((predicted - observed) ** 2))),
            "median_session_force_bin_mae_N": float(session_metric["session_mae_N"].median()),
            "worst_target_median_session_force_bin_mae_N": float(target_metric["median_session_mae_N"].max()),
            "session_force_bin_within_0_5_N_fraction": float(binned["within_0_5_N"].mean()),
            "claim_boundary": "post-hoc experimental live-sensor pipeline; configured range is not a validated common safe range",
            "data_origin": "empirical-manual-held-out-live-sensor",
        })
    return pd.DataFrame(rows)


def model_range_comparison(
    comparator: pd.DataFrame,
    operating: pd.DataFrame,
    live_sensor: pd.DataFrame,
) -> pd.DataFrame:
    combined = pd.concat([comparator, operating], ignore_index=True, sort=False)
    combined["model_family"] = "global_session_bin"
    combined["metric_grain"] = "session and 0.25 N force-bin medians"
    conditional = live_sensor[live_sensor["estimator"].eq("live_sensor_conditional_isotonic")].iloc[0]
    live_row = {
        "model": conditional["estimator"],
        "held_out_test_groups": conditional["held_out_test_groups"],
        "independent_sessions": conditional["independent_sessions"],
        "target_roi": conditional["target_roi"],
        "force_interval_N": conditional["force_interval_N"],
        "evaluation_scope": conditional["evaluation_scope"],
        "prediction_rows": conditional["session_force_bin_rows"],
        "mae_N": conditional["frame_mae_N"],
        "rmse_N": conditional["frame_rmse_N"],
        "r2": conditional["session_force_bin_r2"],
        "median_session_mae_N": conditional["median_session_force_bin_mae_N"],
        "q25_session_mae_N": float("nan"),
        "q75_session_mae_N": float("nan"),
        "worst_target_median_session_mae_N": conditional["worst_target_median_session_force_bin_mae_N"],
        "within_0_5_N_fraction": conditional["session_force_bin_within_0_5_N_fraction"],
        "validation_design": "exact six-fold live-sensor outer predictions; summarized at session-force-bin grain",
        "claim_boundary": conditional["claim_boundary"],
        "model_family": "live_sensor_frame_pipeline",
        "metric_grain": "outer predictions collapsed to session and 0.25 N force-bin medians",
    }
    return pd.concat([combined, pd.DataFrame([live_row])], ignore_index=True, sort=False)


def global_automatic_hysteresis(authoritative_tables: Path) -> pd.DataFrame:
    source = pd.read_csv(authoritative_tables / "automatic_hysteresis.csv")
    source = source[source["independent_sessions"].eq(2)].copy()
    normalized_parts = []
    for _, group in source.groupby(["roi", "speed_mm_min", "displacement_mm_abs"], sort=True):
        values = np.r_[group["median_loading_light"].to_numpy(), group["median_unloading_light"].to_numpy()]
        low = float(np.nanmin(values))
        high = float(np.nanmax(values))
        span = high - low
        if span <= 0:
            continue
        piece = group.copy()
        piece["normalized_loading_light"] = (piece["median_loading_light"] - low) / span
        piece["normalized_unloading_light"] = (piece["median_unloading_light"] - low) / span
        piece["normalization_span_light"] = span
        normalized_parts.append(piece)
    normalized = pd.concat(normalized_parts, ignore_index=True)
    result = normalized.groupby(["speed_mm_min", "displacement_mm_abs", "force_bin_N"], as_index=False).agg(
        contributing_roi=("roi", "nunique"),
        minimum_independent_sessions_per_roi=("independent_sessions", "min"),
        median_normalized_loading_light=("normalized_loading_light", "median"),
        q25_normalized_loading_light=("normalized_loading_light", base.q25),
        q75_normalized_loading_light=("normalized_loading_light", base.q75),
        median_normalized_unloading_light=("normalized_unloading_light", "median"),
        q25_normalized_unloading_light=("normalized_unloading_light", base.q25),
        q75_normalized_unloading_light=("normalized_unloading_light", base.q75),
    )
    result["all_nine_roi"] = result["contributing_roi"].eq(9)
    result["evidence_role"] = "global normalized hysteresis shape only"
    result["claim_boundary"] = "automatic evidence only; normalization removes absolute amplitude and cannot support general sensitivity"
    result["data_origin"] = "empirical-automatic"
    return result


def improvement_options(
    model_summary: pd.DataFrame,
    comparator_summary: pd.DataFrame,
    live_sensor_summary: pd.DataFrame,
    detection_summary: pd.DataFrame,
) -> pd.DataFrame:
    best_r2 = model_summary.sort_values("r2", ascending=False).iloc[0]
    comparator_same = comparator_summary[comparator_summary["model"].eq(best_r2["model"])].iloc[0]
    live_r2 = float(
        live_sensor_summary.loc[
            live_sensor_summary["estimator"].eq("live_sensor_conditional_isotonic"),
            "session_force_bin_r2",
        ].iloc[0]
    )
    guarded = base.finite(detection_summary["device_wide_guarded_detection_region_N"].iloc[0])
    return pd.DataFrame([
        {
            "priority": 1,
            "action": "Use the 1.7-3.0 N implementation range as a validity boundary, not as an R-squared optimization",
            "evidence": (
                f"Best global operating-range R2={best_r2['r2']:.3f} ({best_r2['model']}); "
                f"the same model is {comparator_same['r2']:.3f} over 0-5 N; exact live isotonic binned R2={live_r2:.3f}"
            ),
            "expected_effect": "Prevents a narrower target variance from being misreported as improved explained variance",
            "scientific_boundary": "The configured range is post-hoc experimental and is not the failed authoritative common safe range",
        },
        {
            "priority": 2,
            "action": "Standardize illumination, alignment, focus, and baseline acquisition before each session",
            "evidence": "Manual target responses remain heterogeneous after equal-target aggregation",
            "expected_effect": "Reduces between-session drift and target-to-target scale differences",
            "scientific_boundary": "Requires new empirical confirmation; no retrospective correction is claimed",
        },
        {
            "priority": 3,
            "action": "Collect randomized settled force steps with balanced repetitions at every target ROI",
            "evidence": f"Device-wide guarded detection region={'established at '+str(guarded)+' N' if guarded is not None else 'not established'}",
            "expected_effect": "Supports true resolution, application thresholds, and stronger global calibration validation",
            "scientific_boundary": "Continuous ramps cannot establish true resolution",
        },
        {
            "priority": 4,
            "action": "Collect longer constant-force holds and more independent days",
            "evidence": "Current automatic holds are fixed-displacement and short; exact conditions have two sessions",
            "expected_effect": "Enables true creep estimates and inferential hysteresis uncertainty",
            "scientific_boundary": "Existing data remain creep-related exploratory evidence only",
        },
    ])


def figure_metadata(
    title: str,
    source_table: str,
    table_dir: Path,
    source_hash: str,
    code_hash: str,
    units: str,
    independent_units: str,
    method: str,
    filters: list[str],
) -> dict[str, Any]:
    return {
        "title": title,
        "source_manifest_hash": source_hash,
        "source_table": source_table,
        "source_table_hash": base.sha256_file(table_dir / f"{source_table}.csv"),
        "code_hash": code_hash,
        "filters": filters,
        "units": units,
        "independent_session_count": independent_units,
        "uncertainty_method": method,
        "global_estimand": "equal-target global total-light response; no ROI excluded",
    }


def build_figures(
    tables: dict[str, pd.DataFrame],
    output: Path,
    source_hash: str,
    code_hash: str,
) -> tuple[dict[str, str], pd.DataFrame]:
    base.configure_plot()
    figure_dir = output / "figures"
    table_dir = output / "tables"
    hashes: dict[str, str] = {}
    chart_map: list[dict[str, str]] = []

    response = tables["global_manual_force_response"]
    shown = response[response["all_nine_targets"] & response["force_bin_N"].le(5)].copy()
    summary = tables["global_metric_summary"].iloc[0]
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    ax.fill_between(shown["median_force_N"], shown["q25_target_total_light"], shown["q75_target_total_light"], color="#DBEAFE", alpha=0.9, label="Target-ROI IQR")
    ax.plot(shown["median_force_N"], shown["global_median_total_light"], color="#2563EB", marker="o", lw=1.6, label="Equal-target median")
    low = shown[shown["force_bin_N"].le(1)]
    x = low["median_force_N"].to_numpy(dtype=float)
    y = summary["global_low_force_sensitivity_light_per_N"] * x + (
        low["global_median_total_light"].mean() - summary["global_low_force_sensitivity_light_per_N"] * low["median_force_N"].mean()
    )
    ax.plot(x, y, color="#F59E0B", ls="--", lw=1.5, label="0-1 N fit")
    ax.set_title("Global manual total-light response")
    ax.set_xlabel("Force (N)")
    ax.set_ylabel("Sum of nine floor-corrected light signals")
    ax.legend(frameon=False)
    hashes.update(base.save_figure(fig, figure_dir, "fig01_global_manual_response", shown, figure_metadata(
        "Global manual total-light response", "global_manual_force_response", table_dir, source_hash, code_hash,
        "positive light sum", "54 sessions; all nine targets", "session median -> target median -> equal-target median and IQR",
        ["manual TEST1-TEST6 only", "all nine target ROI required", "force <=5 N"],
    )))
    chart_map.append({"figure": "fig01_global_manual_response", "family": "line with interval", "claim": "Global total-light response is reported with target heterogeneity."})

    target = tables["global_target_signal_heterogeneity"]
    fig, ax = plt.subplots(figsize=(9.2, 4.8))
    error = np.vstack([target["median_total_light"] - target["q25_total_light"], target["q75_total_light"] - target["median_total_light"]])
    ax.bar(target["target_roi"], target["median_total_light"], color="#2563EB", edgecolor="#1F2937", yerr=error, capsize=3)
    ax.axhline(float(target["global_equal_target_median"].iloc[0]), color="#F59E0B", ls="--", lw=1.5, label="Equal-target median")
    ax.set_title("Target heterogeneity inside the global 0.75-1.25 N response")
    ax.set_xlabel("Pressed target ROI")
    ax.set_ylabel("Global total light")
    ax.set_xticks(range(1, 10))
    ax.legend(frameon=False)
    hashes.update(base.save_figure(fig, figure_dir, "fig02_global_target_heterogeneity", target, figure_metadata(
        "Target heterogeneity inside the global response", "global_target_signal_heterogeneity", table_dir, source_hash, code_hash,
        "positive light sum", "six sessions per target ROI", "session median and interquartile interval",
        ["manual TEST1-TEST6 only", "0.75-1.25 N"],
    )))
    chart_map.append({"figure": "fig02_global_target_heterogeneity", "family": "bar with interval", "claim": "Pooling does not erase target-to-target variation."})

    detection = tables["global_detection_detail"]
    fig, ax = plt.subplots(figsize=(9.2, 4.8))
    ax.plot(detection["force_bin_N"], detection["overall_recall"], color="#2563EB", marker="o", label="All-session recall")
    ax.plot(detection["force_bin_N"], detection["minimum_target_recall"], color="#F59E0B", marker="s", ls="--", label="Worst-target recall")
    ax.axhline(base.DETECTION_RECALL, color="#374151", ls=":", lw=1.3, label="Required recall")
    ax.set_ylim(-0.03, 1.05)
    ax.set_title("Global optical detection recall with worst-target guardrail")
    ax.set_xlabel("Force-bin centre (N)")
    ax.set_ylabel("Empirical recall")
    ax.legend(frameon=False)
    hashes.update(base.save_figure(fig, figure_dir, "fig03_global_detection_guardrail", detection, figure_metadata(
        "Global optical detection recall", "global_detection_detail", table_dir, source_hash, code_hash,
        "recall", "54 press + 11 no-contact sessions", "frozen 1% FPR and 95% recall; worst target retained",
        ["manual only", "all nine targets required for device-wide claim"],
    )))
    chart_map.append({"figure": "fig03_global_detection_guardrail", "family": "two-series line", "claim": "Average performance is not promoted when a target fails."})

    lag = tables["global_system_lag_curve_summary"]
    lag_summary = tables["global_system_lag_summary"].iloc[0]
    fig, ax = plt.subplots(figsize=(9.2, 4.8))
    ax.fill_between(lag["lag_ms"], lag["q25_absolute_correlation"], lag["q75_absolute_correlation"], color="#DBEAFE", alpha=0.9)
    ax.plot(lag["lag_ms"], lag["median_absolute_correlation"], color="#2563EB", lw=1.6)
    ax.axvline(float(lag_summary["median_best_lag_ms"]), color="#F59E0B", ls="--", label="Median session best lag")
    ax.set_title("Global manual system-lag curve")
    ax.set_xlabel("Lag (ms; positive means optical follows reference)")
    ax.set_ylabel("Absolute derivative correlation")
    ax.legend(frameon=False)
    hashes.update(base.save_figure(fig, figure_dir, "fig04_global_system_lag", lag, figure_metadata(
        "Global manual system-lag curve", "global_system_lag_curve_summary", table_dir, source_hash, code_hash,
        "ms and absolute correlation", "54 manual sessions", "session derivative cross-correlation; median and IQR",
        ["manual TEST1-TEST6 only", "-500 to +500 ms", "not intrinsic material response time"],
    )))
    chart_map.append({"figure": "fig04_global_system_lag", "family": "line with interval", "claim": "Global lag remains a system-level exploratory estimate."})

    comparison = tables["global_model_range_comparison"]
    global_only = comparison[comparison["model_family"].eq("global_session_bin")].copy()
    comparator = global_only[global_only["evaluation_scope"].eq("characterization_comparator_0_to_5_N")].set_index("model")
    operating = global_only[global_only["evaluation_scope"].eq("live_implementation_range_1.7_to_3.0_N")].set_index("model")
    order = operating.sort_values("r2", ascending=True).index.tolist()
    labels = [name.replace("_", " ") for name in order]
    y = np.arange(len(order), dtype=float)
    live_r2 = float(
        comparison.loc[
            comparison["model"].eq("live_sensor_conditional_isotonic"), "r2"
        ].iloc[0]
    )
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), sharey=True)
    axes[0].barh(y - 0.19, comparator.loc[order, "r2"], height=0.36, color="#9CA3AF", edgecolor="#374151", label="0-5 N comparator")
    axes[0].barh(y + 0.19, operating.loc[order, "r2"], height=0.36, color="#2563EB", edgecolor="#1F2937", label="1.7-3.0 N rerun")
    axes[0].axvline(0.0, color="#111827", lw=1.0)
    axes[0].axvline(live_r2, color="#F59E0B", lw=1.5, ls="--", label=f"Exact live isotonic, binned R²={live_r2:.3f}")
    axes[0].set_yticks(y, labels)
    axes[0].set_title("Held-out R² by evaluation range")
    axes[0].set_xlabel("R²; higher is better")
    axes[0].set_ylabel("")
    axes[0].legend(frameon=False, fontsize=8)
    error = np.vstack([
        operating.loc[order, "median_session_mae_N"] - operating.loc[order, "q25_session_mae_N"],
        operating.loc[order, "q75_session_mae_N"] - operating.loc[order, "median_session_mae_N"],
    ])
    colors = ["#9CA3AF" if name == "constant_baseline" else "#2563EB" for name in order]
    axes[1].barh(
        y,
        operating.loc[order, "median_session_mae_N"],
        color=colors,
        edgecolor="#374151",
        xerr=error,
        capsize=3,
    )
    axes[1].set_title("Error inside 1.7-3.0 N")
    axes[1].set_xlabel("Median session MAE (N); lower is better")
    axes[1].set_ylabel("")
    fig.suptitle("Global model benchmark within the live implementation range", fontsize=13)
    hashes.update(base.save_figure(fig, figure_dir, "fig05_global_model_benchmark", comparison, figure_metadata(
        "Global model benchmark within the live implementation range", "global_model_range_comparison", table_dir, source_hash, code_hash,
        "R-squared and N", "54 sessions; six held-out TEST-group folds", "same outer held-out TEST groups; nested ridge alpha selection; exact live outer predictions retained separately",
        ["manual empirical data only", "live implementation range 1.7-3.0 N", "0-5 N comparator retained", "all nine target ROI retained"],
    )))
    chart_map.append({"figure": "fig05_global_model_benchmark", "family": "paired horizontal bars with benchmark", "claim": "The implementation range lowers absolute error but does not improve held-out R-squared."})

    hysteresis = tables["global_automatic_hysteresis"]
    complete = hysteresis[hysteresis["all_nine_roi"]].copy()
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.8), sharey=True)
    colors = {200.0: "#2563EB", 400.0: "#F59E0B", 600.0: "#6B7280"}
    displacements = sorted(hysteresis["displacement_mm_abs"].unique())
    for ax, displacement in zip(axes, displacements, strict=True):
        panel = complete[complete["displacement_mm_abs"].eq(displacement)]
        for speed, group in panel.groupby("speed_mm_min"):
            group = group.sort_values("force_bin_N")
            ax.plot(group["force_bin_N"], group["median_normalized_loading_light"], color=colors[speed], lw=1.4, marker="o", ms=4)
            ax.plot(group["force_bin_N"], group["median_normalized_unloading_light"], color=colors[speed], lw=1.2, ls="--", marker="o", ms=4)
        ax.set_title(f"{displacement:g} mm")
        ax.set_xlabel("Corrected force (N)")
        ax.set_ylabel("Median normalized optical level")
        if panel.empty:
            support_note = "No force bin has all-nine-ROI support"
        else:
            supported_speeds = ", ".join(f"{int(value)}" for value in sorted(panel["speed_mm_min"].unique()))
            support_note = f"All-nine support: {supported_speeds} mm/min"
            if len(panel) == 1:
                support_note += "\n(single common force bin)"
        ax.text(0.03, 0.04, support_note, transform=ax.transAxes, ha="left", va="bottom", fontsize=8, color="#4B5563")
    present_speeds = sorted(complete["speed_mm_min"].unique())
    handles = [Line2D([0], [0], color=colors[speed], lw=1.4, marker="o", ms=4, label=f"{int(speed)} mm/min") for speed in present_speeds]
    handles += [Line2D([0], [0], color="#111827", ls="-", marker="o", ms=4, label="loading"), Line2D([0], [0], color="#111827", ls="--", marker="o", ms=4, label="unloading")]
    fig.legend(handles=handles, loc="upper center", ncol=len(handles), frameon=False, bbox_to_anchor=(0.5, 0.98))
    fig.suptitle("Global normalized hysteresis shape", y=1.04, fontsize=13)
    fig.subplots_adjust(top=0.80)
    hashes.update(base.save_figure(fig, figure_dir, "fig06_global_normalized_hysteresis", complete, figure_metadata(
        "Global normalized hysteresis shape", "global_automatic_hysteresis", table_dir, source_hash, code_hash,
        "within-condition normalized optical level", "162 sessions; two per exact condition", "session-nested ROI loops normalized before equal-ROI median",
        ["automatic evidence only", "all nine ROI required per plotted bin", "shape only; absolute amplitude removed"],
    ), tight_layout=False))
    chart_map.append({"figure": "fig06_global_normalized_hysteresis", "family": "faceted multi-series line", "claim": "Global automatic evidence is restricted to normalized hysteresis shape."})
    return hashes, pd.DataFrame(chart_map)


def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    feature_store = (project_root / args.feature_store).resolve()
    authoritative = (project_root / args.authoritative).resolve()
    live_spec_path = (project_root / args.live_spec).resolve()
    live_bundle = (project_root / args.live_bundle).resolve()
    live_model_run = (project_root / args.live_model_run).resolve()
    output = (project_root / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    table_dir = output / "tables"

    parent_quality = base.read_json(authoritative / "quality_report.json")
    parent_validation = base.read_json(authoritative / "validation_report.json")
    if not parent_quality.get("gate_pass") or not parent_validation.get("gate_pass"):
        raise ValueError("authoritative manual-first evidence package must pass before global aggregation")

    live_spec = base.read_json(live_spec_path)
    live_manifest = base.read_json(live_bundle / "manifest.json")
    live_force_model = base.read_json(live_bundle / "force_model.json")
    live_run_manifest = base.read_json(live_model_run / "run_manifest.json")
    spec_range = live_spec["operating_range_N"]
    force_range = live_force_model["operating_range_N"]
    manifest_range = live_manifest["operating_range_N"]
    live_force_min_N = float(spec_range["minimum"])
    live_force_max_N = float(spec_range["maximum"])
    if not (
        spec_range == force_range == manifest_range
        and live_run_manifest.get("spec_hash") == base.json_hash(live_spec)
        and live_spec.get("status") == "posthoc_experimental"
    ):
        raise ValueError("live-sensor operating range or experimental provenance is inconsistent")
    live_predictions = pd.read_parquet(live_model_run / "outer_predictions.parquet")

    frames = base.load_features(feature_store)
    no_contact_tables, floors = base.no_contact_tables(frames)
    del no_contact_tables
    frames = base.add_lights(frames, floors)

    tables = global_manual_tables(frames)
    tables.update(global_noise_detection_lag(frames, tables["global_manual_session_force_bins"], tables["global_metric_summary"]))
    operating_models = global_model_benchmark(
        tables["global_manual_session_force_bins"],
        live_force_min_N,
        live_force_max_N,
        "live_implementation_range_1.7_to_3.0_N",
    )
    comparator_models = global_model_benchmark(
        tables["global_manual_session_force_bins"],
        COMPARATOR_FORCE_MIN_N,
        COMPARATOR_FORCE_MAX_N,
        "characterization_comparator_0_to_5_N",
    )
    tables.update(operating_models)
    for name, table in comparator_models.items():
        tables[f"{name}_comparator_0_to_5_N"] = table
    tables["live_sensor_operating_range_r2"] = live_sensor_operating_range_metrics(
        live_predictions, live_force_min_N, live_force_max_N
    )
    tables["global_model_range_comparison"] = model_range_comparison(
        tables["global_model_cv_summary_comparator_0_to_5_N"],
        tables["global_model_cv_summary"],
        tables["live_sensor_operating_range_r2"],
    )
    tables["global_automatic_hysteresis"] = global_automatic_hysteresis(authoritative / "tables")
    tables["global_improvement_options"] = improvement_options(
        tables["global_model_cv_summary"],
        tables["global_model_cv_summary_comparator_0_to_5_N"],
        tables["live_sensor_operating_range_r2"],
        tables["global_detection_summary"],
    )

    table_hashes: dict[str, str] = {}
    for name, table in tables.items():
        table_hashes.update(base.write_table(table_dir, name, table))

    source_hash = base.json_hash({
        "parent_run_manifest": base.sha256_file(authoritative / "run_manifest.json"),
        "feature_reconciliation": base.sha256_file(feature_store / "reconciliation_report.json"),
        "live_sensor_spec": base.sha256_file(live_spec_path),
        "live_sensor_bundle_manifest": base.sha256_file(live_bundle / "manifest.json"),
        "live_sensor_force_model": base.sha256_file(live_bundle / "force_model.json"),
        "live_sensor_run_manifest": base.sha256_file(live_model_run / "run_manifest.json"),
        "live_sensor_outer_predictions": base.sha256_file(live_model_run / "outer_predictions.parquet"),
    })
    code_hash = base.sha256_file(Path(__file__).resolve())
    figure_hashes, chart_map = build_figures(tables, output, source_hash, code_hash)
    table_hashes.update(base.write_table(table_dir, "global_chart_map", chart_map))

    checks = {
        "parent_authoritative_quality_gate_pass": bool(parent_quality["gate_pass"]),
        "parent_authoritative_validation_gate_pass": bool(parent_validation["gate_pass"]),
        "all_245_framesource_sessions_retained": frames["session_id"].nunique() == 245,
        "all_179176_frames_retained": len(frames) == 179176,
        "manual_general_metrics_only": tables["global_metric_summary"]["data_origin"].eq("empirical-manual").all(),
        "no_synthetic_rows": all(
            not any(
                "synthetic" in str(value).lower()
                for column in table.columns
                if pd.api.types.is_string_dtype(table[column].dtype)
                for value in table[column].dropna().to_numpy()
            )
            for table in tables.values()
        ),
        "global_response_requires_all_nine_targets": tables["global_manual_force_response"].loc[
            tables["global_manual_force_response"]["all_nine_targets"], "contributing_target_roi"
        ].eq(9).all(),
        "global_detection_retains_worst_target_guardrail": "minimum_target_recall" in tables["global_detection_detail"].columns,
        "global_model_has_six_outer_holdouts": tables["global_model_cv_summary"]["held_out_test_groups"].eq(6).all(),
        "global_model_retains_all_nine_targets": tables["global_model_cv_summary"]["target_roi"].eq(9).all(),
        "global_model_uses_live_implementation_range": (
            tables["global_model_cv_summary"]["force_interval_N"].eq(f"{live_force_min_N:g}-{live_force_max_N:g}").all()
            and tables["global_model_cv_predictions"]["observed_force_N"].between(
                live_force_min_N, live_force_max_N, inclusive="both"
            ).all()
        ),
        "zero_to_five_comparator_is_retained": tables[
            "global_model_cv_summary_comparator_0_to_5_N"
        ]["force_interval_N"].eq("0-5").all(),
        "live_sensor_exact_outer_predictions_are_summarized": (
            tables["live_sensor_operating_range_r2"]["held_out_test_groups"].eq(6).all()
            and tables["live_sensor_operating_range_r2"]["independent_sessions"].eq(54).all()
            and tables["live_sensor_operating_range_r2"]["target_roi"].eq(9).all()
        ),
        "automatic_global_is_hysteresis_only": tables["global_automatic_hysteresis"]["evidence_role"].eq("global normalized hysteresis shape only").all(),
        "automatic_plotted_bins_require_all_nine_roi": tables["global_automatic_hysteresis"].loc[
            tables["global_automatic_hysteresis"]["all_nine_roi"], "contributing_roi"
        ].eq(9).all(),
        "six_figure_bundles": len(list((output / "figures").glob("*.png"))) == 6,
        "all_figure_companions_present": all(
            all((output / "figures" / f"{stem}.{suffix}").exists() for suffix in ("png", "svg", "pdf", "csv", "json"))
            for stem in [f"fig{number:02d}_{name}" for number, name in [
                (1, "global_manual_response"), (2, "global_target_heterogeneity"),
                (3, "global_detection_guardrail"), (4, "global_system_lag"),
                (5, "global_model_benchmark"), (6, "global_normalized_hysteresis"),
            ]]
        ),
    }
    quality = {
        "schema_version": SCHEMA_VERSION,
        "created_at": base.utc_now(),
        "gate_pass": all(checks.values()),
        "checks": checks,
        "source_manifest_hash": source_hash,
        "code_hash": code_hash,
        "parent_evidence_run_id": parent_validation["evidence_run_id"],
        "global_estimand": "equal-target median of all-nine-ROI total light after session balancing",
        "scope_change": "post-plan global sensitivity analysis; per-ROI authoritative evidence remains controlling for localization and heterogeneity",
    }
    base.write_json(output / "quality_report.json", quality)
    if not quality["gate_pass"]:
        raise ValueError(f"global characterization quality gate failed: {checks}")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"global-characterization-{base.json_hash({'source': source_hash, 'code': code_hash})[:16]}",
        "created_at": base.utc_now(),
        "command": "build-global-characterization",
        "argv": sys.argv,
        "source_manifest_hash": source_hash,
        "code_hash": code_hash,
        "inputs": {
            "feature_store": str(feature_store),
            "manual_first_authoritative_evidence": str(authoritative),
            "manual_first_run_manifest_sha256": base.sha256_file(authoritative / "run_manifest.json"),
            "live_sensor_spec": str(live_spec_path),
            "live_sensor_spec_sha256": base.sha256_file(live_spec_path),
            "live_sensor_bundle": str(live_bundle),
            "live_sensor_bundle_manifest_sha256": base.sha256_file(live_bundle / "manifest.json"),
            "live_sensor_force_model_sha256": base.sha256_file(live_bundle / "force_model.json"),
            "live_sensor_model_run": str(live_model_run),
            "live_sensor_run_manifest_sha256": base.sha256_file(live_model_run / "run_manifest.json"),
            "live_sensor_outer_predictions_sha256": base.sha256_file(live_model_run / "outer_predictions.parquet"),
            "live_sensor_operating_range_N": [live_force_min_N, live_force_max_N],
        },
        "outputs": {
            "tables": table_hashes,
            "figures": figure_hashes,
            "quality_report.json": base.sha256_file(output / "quality_report.json"),
        },
        "scientific_guardrails": {
            "synthetic_data": "none",
            "global_weighting": "session balanced, then equal target ROI; no ROI excluded",
            "device_wide_detection": "requires overall and minimum target recall >=95%",
            "model_validation": "outer holdout by TEST1-TEST6 with nested ridge selection",
            "model_evaluation_range": f"{live_force_min_N:g}-{live_force_max_N:g} N from the experimental live-sensor implementation; 0-5 N comparator retained",
            "range_claim_boundary": "configured post-hoc experimental operating range; not a validated common safe range",
            "localization": "not supported by global output; per-ROI diagnostics retained",
            "automatic_role": "normalized hysteresis shape only",
        },
        "gate_pass": True,
    }
    base.write_json(output / "run_manifest.json", manifest)
    print(json.dumps({"output": str(output), "run_id": manifest["run_id"], "figures": 6, "gate_pass": True}, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--feature-store", default="analysis_outputs/live_sensor_study/feature_store/feature-store-c0ec762f1dc888a7")
    value.add_argument("--authoritative", default="analysis_outputs/live_sensor_study/characterization/evidence-products-manual-first-authoritative")
    value.add_argument("--live-spec", default="config/manual_only_experimental_recovery_v2.json")
    value.add_argument("--live-bundle", default="models/live_sensor_experimental_manual_recovery_v2")
    value.add_argument("--live-model-run", default="analysis_outputs/live_sensor_manual_only/model_runs/experimental_recovery/experimental-recovery-5949deea3c881f09")
    value.add_argument("--output", default="analysis_outputs/live_sensor_study/characterization/evidence-products-global-characterization")
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
