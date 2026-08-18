"""Compare global force estimators that do not use ROI labels."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from build_manual_linear_models import (
    ARCHIVE, OPTICAL_COLS, OUT as MANUAL_OUT, RIDGE, STATS,
    extract, fit_ridge, predict, regression_metrics, truthy, weights_by_session,
)


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "global_force_model"
N_FOLDS = 5


def aggregate_features(data: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    cube = data[OPTICAL_COLS].to_numpy(float).reshape(-1, 9, len(STATS))
    arrays, names = [], []
    for j, stat in enumerate(STATS):
        values = cube[:, :, j]
        summaries = {
            "sum": values.sum(axis=1), "mean": values.mean(axis=1),
            "max": values.max(axis=1), "min": values.min(axis=1),
            "std": values.std(axis=1), "positive_sum": np.maximum(values, 0).sum(axis=1),
        }
        for agg, result in summaries.items():
            arrays.append(result)
            names.append(f"global_{stat}_{agg}")
    return np.column_stack(arrays), names


def engineered_features(data: pd.DataFrame, mode: str) -> tuple[np.ndarray, list[str]]:
    raw = data[OPTICAL_COLS].to_numpy(float)
    agg, agg_names = aggregate_features(data)
    if mode == "total_light_linear":
        keep = [agg_names.index("global_delta_v_mean_positive_sum"), agg_names.index("global_active_fraction_sum")]
        return agg[:, keep], [agg_names[i] for i in keep]
    if mode == "global_multichannel_linear":
        return np.column_stack([raw, agg]), OPTICAL_COLS + agg_names
    if mode == "global_nonlinear_basis":
        signed_log = np.sign(agg) * np.log1p(np.abs(agg))
        signed_square = np.sign(agg) * agg ** 2
        return np.column_stack([raw, agg, signed_log, signed_square]), OPTICAL_COLS + agg_names + [f"signed_log_{n}" for n in agg_names] + [f"signed_square_{n}" for n in agg_names]
    raise ValueError(mode)


def fold_ids(data: pd.DataFrame) -> np.ndarray:
    folds = np.empty(len(data), int)
    for _, idx in data.groupby("session", sort=False).indices.items():
        idx = np.asarray(idx)
        folds[idx] = np.minimum(np.arange(len(idx)) * N_FOLDS // len(idx), N_FOLDS - 1)
    return folds


def validate_ridge(data: pd.DataFrame, folds: np.ndarray, mode: str) -> tuple[np.ndarray, list[str]]:
    x, names = engineered_features(data, mode)
    y = data.force_physical_N.to_numpy(float)
    sessions = data.session.to_numpy()
    oof = np.full(len(data), np.nan)
    for k in range(N_FOLDS):
        train, test = folds != k, folds == k
        model = fit_ridge(x[train], y[train], weights_by_session(sessions[train]), ridge=0.03)
        oof[test] = np.maximum(0, predict(model, x[test]))
    return oof, names


def validate_delayed_ridge(data: pd.DataFrame, folds: np.ndarray, mode: str, delay_frames: int = 1) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    x, names = engineered_features(data, mode)
    current_y = data.force_physical_N.to_numpy(float)
    delayed_y = np.full(len(data), np.nan)
    usable = np.zeros(len(data), bool)
    for _, idx in data.groupby("session", sort=False).indices.items():
        idx = np.asarray(idx)
        delayed_y[idx[delay_frames:]] = current_y[idx[:-delay_frames]]
        usable[idx[delay_frames:]] = True
    sessions = data.session.to_numpy()
    oof = np.full(len(data), np.nan)
    for k in range(N_FOLDS):
        train = (folds != k) & usable
        test = (folds == k) & usable
        model = fit_ridge(x[train], delayed_y[train], weights_by_session(sessions[train]), ridge=0.03)
        oof[test] = np.maximum(0, predict(model, x[test]))
    return oof, delayed_y, usable, names


def validate_rff(data: pd.DataFrame, folds: np.ndarray, n_components: int = 192) -> np.ndarray:
    x = data[OPTICAL_COLS].to_numpy(float)
    y = data.force_physical_N.to_numpy(float)
    sessions = data.session.to_numpy()
    oof = np.full(len(data), np.nan)
    rng = np.random.default_rng(20260808)
    base_w = rng.normal(0, 0.55, size=(x.shape[1], n_components))
    phase = rng.uniform(0, 2 * np.pi, size=n_components)
    for k in range(N_FOLDS):
        train, test = folds != k, folds == k
        w = weights_by_session(sessions[train])
        mean = (x[train] * w[:, None]).sum(axis=0) / w.sum()
        scale = np.sqrt(np.maximum((((x[train] - mean) ** 2) * w[:, None]).sum(axis=0) / w.sum(), 1e-12))
        z_train = np.sqrt(2 / n_components) * np.cos(((x[train] - mean) / scale) @ base_w + phase)
        z_test = np.sqrt(2 / n_components) * np.cos(((x[test] - mean) / scale) @ base_w + phase)
        model = fit_ridge(z_train, y[train], w, ridge=0.08)
        oof[test] = np.maximum(0, predict(model, z_test))
    return oof


def rolling_mean_by_session(values: np.ndarray, data: pd.DataFrame, window: int = 5) -> np.ndarray:
    result = np.empty_like(values)
    for _, idx in data.groupby("session", sort=False).indices.items():
        idx = np.asarray(idx)
        result[idx] = pd.Series(values[idx]).rolling(window, center=True, min_periods=1).mean().to_numpy()
    return result


def correlation_diagnostics(data: pd.DataFrame) -> dict:
    agg, names = aggregate_features(data)
    light = agg[:, names.index("global_delta_v_mean_positive_sum")]
    force = data.force_physical_N.to_numpy(float)
    per_roi = []
    best_lags = []
    for roi, idx in data.groupby("target_roi", sort=True).indices.items():
        idx = np.asarray(idx)
        f, l = force[idx], light[idx]
        corr = float(np.corrcoef(f, l)[0, 1])
        lag_scores = []
        for lag in range(-30, 31):
            if lag < 0:
                a, b = f[-lag:], l[:lag]
            elif lag > 0:
                a, b = f[:-lag], l[lag:]
            else:
                a, b = f, l
            lag_scores.append((lag, float(np.corrcoef(a, b)[0, 1])))
        best = max(lag_scores, key=lambda pair: abs(pair[1]))
        best_lags.append(best[0])
        local = pd.DataFrame({"force": f, "light": l})
        local["light_bin"] = pd.qcut(local.light, 5, duplicates="drop")
        spread = local.groupby("light_bin", observed=True).force.agg(["std", "min", "max"])
        per_roi.append({
            "roi": int(roi), "zero_lag_correlation": corr,
            "best_lag_frames": int(best[0]), "best_lag_correlation": best[1],
            "median_force_std_within_same_light_quintile_N": float(spread["std"].median()),
            "median_force_range_within_same_light_quintile_N": float((spread["max"] - spread["min"]).median()),
        })
    overall = pd.DataFrame({"force": force, "light": light})
    overall["light_bin"] = pd.qcut(overall.light, 10, duplicates="drop")
    overall_spread = overall.groupby("light_bin", observed=True).force.agg(["std", "min", "max"])
    return {
        "overall_zero_lag_correlation": float(np.corrcoef(force, light)[0, 1]),
        "median_force_std_within_same_global_light_decile_N": float(overall_spread["std"].median()),
        "median_force_range_within_same_global_light_decile_N": float((overall_spread["max"] - overall_spread["min"]).median()),
        "per_roi": per_roi,
        "median_best_lag_frames": float(np.median(best_lags)),
        "best_lag_range_frames": [int(min(best_lags)), int(max(best_lags))],
    }


def serialize(model: dict, names: list[str]) -> dict:
    return {
        "feature_names": names, "feature_mean": np.asarray(model["mean"]).tolist(),
        "feature_scale": np.asarray(model["scale"]).tolist(), "target_mean": np.asarray(model["target_mean"]).tolist(),
        "standardized_coefficients": np.asarray(model["coef"]).tolist(),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    raw = extract()
    valid = truthy(raw.frame_valid) & truthy(raw.baseline_valid) & truthy(raw.loadcell_valid) & truthy(raw.synchronization_valid)
    data = raw.loc[valid & raw[["force_N", *OPTICAL_COLS]].notna().all(axis=1)].reset_index(drop=True)
    data["target_roi"] = data.target_roi_ground_truth.astype(int)
    data["force_physical_N"] = np.maximum(data.force_N.to_numpy(float), 0)
    folds = fold_ids(data)
    y = data.force_physical_N.to_numpy(float)

    predictions = {}
    results = {}
    names_by_model = {}
    modes = ["total_light_linear", "global_multichannel_linear", "global_nonlinear_basis"]
    for mode in modes:
        predictions[mode], names_by_model[mode] = validate_ridge(data, folds, mode)
        results[mode] = {
            "instantaneous": regression_metrics(y, predictions[mode]),
            "five_frame_smoothed": regression_metrics(y, rolling_mean_by_session(predictions[mode], data)),
        }
    predictions["random_fourier_nonlinear"] = validate_rff(data, folds)
    results["random_fourier_nonlinear"] = {
        "instantaneous": regression_metrics(y, predictions["random_fourier_nonlinear"]),
        "five_frame_smoothed": regression_metrics(y, rolling_mean_by_session(predictions["random_fourier_nonlinear"], data)),
    }

    delayed_name = "global_nonlinear_basis_1frame_delayed"
    delayed_pred, delayed_y, delayed_usable, delayed_names = validate_delayed_ridge(data, folds, "global_nonlinear_basis", 1)
    predictions[delayed_name] = delayed_pred
    names_by_model[delayed_name] = delayed_names
    results[delayed_name] = {
        "instantaneous": regression_metrics(delayed_y[delayed_usable], delayed_pred[delayed_usable]),
        "five_frame_smoothed": regression_metrics(delayed_y[delayed_usable], rolling_mean_by_session(np.nan_to_num(delayed_pred, nan=0), data)[delayed_usable]),
    }

    best_name = min(results, key=lambda name: results[name]["instantaneous"]["rmse_N"])
    # Save the best deterministic, directly serializable ridge-basis model. RFF is
    # compared diagnostically but not selected unless a separate deployer is built.
    deployable_names = modes + [delayed_name]
    selected = min(deployable_names, key=lambda name: results[name]["instantaneous"]["rmse_N"])
    selected_mode = "global_nonlinear_basis" if selected == delayed_name else selected
    target_delay = 1 if selected == delayed_name else 0
    final_x, final_names = engineered_features(data, selected_mode)
    if target_delay:
        final_target, final_mask = delayed_y, delayed_usable
    else:
        final_target, final_mask = y, np.ones(len(data), bool)
    final_model = fit_ridge(final_x[final_mask], final_target[final_mask], weights_by_session(data.session.to_numpy()[final_mask]), ridge=0.03)
    artifact = {
        "model_version": "global-force-1.0", "uses_roi_label": False,
        "selected_feature_model": selected_mode, "target_delay_frames": target_delay,
        "model_family": "ridge-regularized global regression",
        "training_scope": {"archive": str(ARCHIVE), "sessions": int(data.session.nunique()), "frames": int(len(data)), "force_range_N": [float(y.min()), float(y.max())]},
        "model": {**serialize(final_model, final_names), "prediction_floor_N": 0, "recommended_smoothing_frames": 1},
        "deployment_ready": False,
    }
    diagnostic = {
        "validation_design": "five contiguous within-session folds; no ROI labels used as model inputs",
        "candidate_results": results, "best_candidate_by_instantaneous_rmse": best_name,
        "selected_serialized_model": selected, "light_force_diagnostics": correlation_diagnostics(data),
        "conclusion": "Nonlinearity alone does not create a strong force signal; repeat-session validation is still unavailable.",
    }
    (OUT / "global_force_model.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    (OUT / "model_comparison.json").write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
    table = pd.DataFrame({name: {f"{kind}_{metric}": value for kind, vals in result.items() for metric, value in vals.items()} for name, result in results.items()}).T
    table.to_csv(OUT / "model_comparison.csv")
    output = pd.DataFrame({"session": data.session, "fold": folds, "force_N": y})
    for name, pred in predictions.items():
        output[name] = pred
    output.to_csv(OUT / "global_force_oof_predictions.csv.gz", index=False, compression="gzip")
    print(json.dumps(diagnostic, indent=2))


if __name__ == "__main__":
    main()
