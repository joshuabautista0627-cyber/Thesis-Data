"""Validate the transparent global characterization sensitivity analysis."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import build_characterization_evidence as base


REQUIRED_TABLES = {
    "global_manual_session_force_bins",
    "global_manual_target_response",
    "global_manual_force_response",
    "global_manual_sensitivity_bootstrap",
    "global_target_signal_heterogeneity",
    "global_metric_summary",
    "global_no_contact_session",
    "global_no_contact_summary",
    "global_detection_detail",
    "global_detection_summary",
    "global_system_lag_curve",
    "global_system_lag_curve_summary",
    "global_system_lag_session",
    "global_system_lag_summary",
    "global_model_cv_predictions",
    "global_model_cv_session_metrics",
    "global_model_cv_target_metrics",
    "global_model_cv_summary",
    "global_model_cv_predictions_comparator_0_to_5_N",
    "global_model_cv_session_metrics_comparator_0_to_5_N",
    "global_model_cv_target_metrics_comparator_0_to_5_N",
    "global_model_cv_summary_comparator_0_to_5_N",
    "live_sensor_operating_range_r2",
    "global_model_range_comparison",
    "global_automatic_hysteresis",
    "global_improvement_options",
    "global_chart_map",
}


def load_tables(directory: Path) -> dict[str, pd.DataFrame]:
    return {name: pd.read_parquet(directory / f"{name}.parquet") for name in REQUIRED_TABLES}


def no_infinity(tables: dict[str, pd.DataFrame]) -> tuple[bool, list[str]]:
    failures = []
    for name, table in tables.items():
        numeric = table.select_dtypes(include=[np.number])
        if not numeric.empty and np.isinf(numeric.to_numpy(dtype=float)).any():
            failures.append(name)
    return not failures, failures


def no_synthetic_strings(tables: dict[str, pd.DataFrame]) -> bool:
    for table in tables.values():
        for column in table.columns:
            if not pd.api.types.is_string_dtype(table[column].dtype):
                continue
            if table[column].dropna().astype(str).str.contains("synthetic", case=False).any():
                return False
    return True


def r2(observed: pd.Series, predicted: pd.Series) -> float:
    y = observed.to_numpy(dtype=float)
    estimate = predicted.to_numpy(dtype=float)
    denominator = float(np.sum((y - y.mean()) ** 2))
    return 1.0 - float(np.sum((y - estimate) ** 2)) / denominator if denominator > 0 else float("nan")


def summary_recomputes(predictions: pd.DataFrame, summary: pd.DataFrame) -> bool:
    for model, group in predictions.groupby("model"):
        session = group.groupby("session_id")["absolute_error_N"].mean()
        row = summary[summary["model"].eq(model)]
        if len(row) != 1:
            return False
        if not math.isclose(
            float(session.median()),
            float(row["median_session_mae_N"].iloc[0]),
            rel_tol=1e-10,
            abs_tol=1e-10,
        ):
            return False
        if not math.isclose(
            r2(group["observed_force_N"], group["predicted_force_N"]),
            float(row["r2"].iloc[0]),
            rel_tol=1e-10,
            abs_tol=1e-10,
        ):
            return False
    return True


def figure_bundles(output: Path) -> tuple[bool, list[str]]:
    issues: list[str] = []
    figure_dir = output / "figures"
    pngs = sorted(figure_dir.glob("*.png"))
    if len(pngs) != 6:
        issues.append(f"expected 6 PNG figures, found {len(pngs)}")
    for png in pngs:
        stem = png.stem
        for suffix in ("png", "svg", "pdf", "csv", "json"):
            path = figure_dir / f"{stem}.{suffix}"
            if not path.exists() or path.stat().st_size == 0:
                issues.append(f"missing or empty {path.name}")
        metadata_path = figure_dir / f"{stem}.json"
        csv_path = figure_dir / f"{stem}.csv"
        if metadata_path.exists() and csv_path.exists():
            metadata = base.read_json(metadata_path)
            plotted = pd.read_csv(csv_path)
            if metadata.get("plotted_rows") != len(plotted):
                issues.append(f"row-count mismatch for {stem}")
            # CSV round-tripping changes some binary float representations, so
            # validate the source-table hash and creation-time provenance rather
            # than recomputing the in-memory plotted-data hash from text.
            source_table = output / "tables" / f"{metadata.get('source_table')}.csv"
            if not source_table.exists() or base.sha256_file(source_table) != metadata.get("source_table_hash"):
                issues.append(f"source-table hash mismatch for {stem}")
            if not metadata.get("source_manifest_hash") or not metadata.get("code_hash"):
                issues.append(f"missing provenance for {stem}")
    return not issues, issues


def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    output = (project_root / args.output).resolve()
    table_dir = output / "tables"
    quality = base.read_json(output / "quality_report.json")
    manifest = base.read_json(output / "run_manifest.json")
    tables = load_tables(table_dir)

    hash_mismatches: list[str] = []
    for name, expected in manifest["outputs"]["tables"].items():
        path = table_dir / name
        if not path.exists() or base.sha256_file(path) != expected:
            hash_mismatches.append(f"tables/{name}")
    for name, expected in manifest["outputs"]["figures"].items():
        path = output / "figures" / name
        if not path.exists() or base.sha256_file(path) != expected:
            hash_mismatches.append(f"figures/{name}")
    if base.sha256_file(output / "quality_report.json") != manifest["outputs"]["quality_report.json"]:
        hash_mismatches.append("quality_report.json")

    parent_path = Path(manifest["inputs"]["manual_first_authoritative_evidence"])
    if not parent_path.is_absolute():
        parent_path = (project_root / parent_path).resolve()
    parent_hash_ok = (
        parent_path.exists()
        and base.sha256_file(parent_path / "run_manifest.json")
        == manifest["inputs"]["manual_first_run_manifest_sha256"]
    )
    parent_quality = base.read_json(parent_path / "quality_report.json") if parent_path.exists() else {}
    parent_validation = base.read_json(parent_path / "validation_report.json") if parent_path.exists() else {}

    response = tables["global_manual_force_response"]
    summary = tables["global_metric_summary"].iloc[0]
    low = response[response["all_nine_targets"] & response["force_bin_N"].le(1.0)]
    recomputed_slope = float(np.polyfit(low["median_force_N"], low["global_median_total_light"], 1)[0])
    slope_ok = math.isclose(
        recomputed_slope,
        float(summary["global_low_force_sensitivity_light_per_N"]),
        rel_tol=1e-10,
        abs_tol=1e-10,
    )
    bootstrap = tables["global_manual_sensitivity_bootstrap"]
    bootstrap_ok = len(bootstrap) == 1000 and np.isfinite(bootstrap["low_force_sensitivity_light_per_N"]).all()

    detection = tables["global_detection_detail"]
    detection_summary = tables["global_detection_summary"].iloc[0]
    average = detection[detection["passes_average_only"]]
    guarded = detection[detection["passes_device_wide_guardrail"]]
    expected_average = float(average["force_bin_N"].min()) if len(average) else float("nan")
    expected_guarded = float(guarded["force_bin_N"].min()) if len(guarded) else float("nan")
    reported_average = float(detection_summary["average_global_detection_region_N"])
    reported_guarded = float(detection_summary["device_wide_guarded_detection_region_N"])
    detection_ok = (
        (math.isnan(expected_average) and math.isnan(reported_average) or math.isclose(expected_average, reported_average))
        and (math.isnan(expected_guarded) and math.isnan(reported_guarded) or math.isclose(expected_guarded, reported_guarded))
        and "minimum_target_recall" in detection.columns
    )

    predictions = tables["global_model_cv_predictions"]
    session_bins = tables["global_manual_session_force_bins"]
    group_map = session_bins[["session_id", "test_group"]].drop_duplicates()
    prediction_groups = predictions.merge(group_map, on="session_id", how="left", validate="many_to_one")
    no_outer_leakage = prediction_groups["outer_test_group"].eq(prediction_groups["test_group"]).all()
    prediction_unique = not predictions.duplicated(["model", "session_id", "force_bin_N"]).any()
    model_summary = tables["global_model_cv_summary"]
    live_min, live_max = (float(value) for value in manifest["inputs"]["live_sensor_operating_range_N"])
    model_coverage = (
        model_summary["held_out_test_groups"].eq(6).all()
        and model_summary["independent_sessions"].eq(54).all()
        and model_summary["target_roi"].eq(9).all()
        and model_summary["force_interval_N"].eq(f"{live_min:g}-{live_max:g}").all()
        and predictions["observed_force_N"].between(live_min, live_max, inclusive="both").all()
    )
    model_summary_ok = summary_recomputes(predictions, model_summary)

    comparator_predictions = tables["global_model_cv_predictions_comparator_0_to_5_N"]
    comparator_summary = tables["global_model_cv_summary_comparator_0_to_5_N"]
    comparator_groups = comparator_predictions.merge(group_map, on="session_id", how="left", validate="many_to_one")
    comparator_ok = (
        comparator_groups["outer_test_group"].eq(comparator_groups["test_group"]).all()
        and not comparator_predictions.duplicated(["model", "session_id", "force_bin_N"]).any()
        and comparator_summary["held_out_test_groups"].eq(6).all()
        and comparator_summary["independent_sessions"].eq(54).all()
        and comparator_summary["target_roi"].eq(9).all()
        and comparator_summary["force_interval_N"].eq("0-5").all()
        and summary_recomputes(comparator_predictions, comparator_summary)
    )

    live_run_path = Path(manifest["inputs"]["live_sensor_model_run"])
    if not live_run_path.is_absolute():
        live_run_path = (project_root / live_run_path).resolve()
    live_predictions_path = live_run_path / "outer_predictions.parquet"
    live_input_hash_ok = (
        live_predictions_path.exists()
        and base.sha256_file(live_predictions_path) == manifest["inputs"]["live_sensor_outer_predictions_sha256"]
        and base.sha256_file(live_run_path / "run_manifest.json") == manifest["inputs"]["live_sensor_run_manifest_sha256"]
    )
    live_predictions = pd.read_parquet(live_predictions_path) if live_predictions_path.exists() else pd.DataFrame()
    live_metrics = tables["live_sensor_operating_range_r2"]
    if live_predictions.empty:
        live_metrics_ok = False
        recomputed_live_binned_r2 = float("nan")
    else:
        live_predictions = live_predictions.copy()
        live_predictions["force_bin_N"] = base.force_bins(live_predictions["force_N"])
        live_binned = live_predictions.groupby(
            ["outer_fold", "session_id", "target_roi", "force_bin_N"], as_index=False
        ).agg(
            observed_force_N=("force_N", "median"),
            predicted_force_N=("conditional_force_N", "median"),
        )
        recomputed_live_binned_r2 = r2(
            live_binned["observed_force_N"], live_binned["predicted_force_N"]
        )
        reported_live = live_metrics[
            live_metrics["estimator"].eq("live_sensor_conditional_isotonic")
        ].iloc[0]
        live_metrics_ok = (
            live_predictions["force_N"].between(live_min, live_max, inclusive="both").all()
            and live_metrics["held_out_test_groups"].eq(6).all()
            and live_metrics["independent_sessions"].eq(54).all()
            and live_metrics["target_roi"].eq(9).all()
            and math.isclose(
                recomputed_live_binned_r2,
                float(reported_live["session_force_bin_r2"]),
                rel_tol=1e-10,
                abs_tol=1e-10,
            )
        )

    auto = tables["global_automatic_hysteresis"]
    automatic_ok = (
        auto["evidence_role"].eq("global normalized hysteresis shape only").all()
        and auto.loc[auto["all_nine_roi"], "contributing_roi"].eq(9).all()
        and auto.loc[auto["all_nine_roi"], "minimum_independent_sessions_per_roi"].eq(2).all()
    )
    figure_ok, figure_issues = figure_bundles(output)
    finite_ok, infinity_tables = no_infinity(tables)

    checks = {
        "upstream_manual_first_quality_gate_pass": bool(parent_quality.get("gate_pass")),
        "upstream_manual_first_validation_gate_pass": bool(parent_validation.get("gate_pass")),
        "upstream_run_manifest_hash_preserved": parent_hash_ok,
        "manifest_output_hashes_match": not hash_mismatches,
        "required_global_tables_present": set(tables) == REQUIRED_TABLES,
        "no_synthetic_rows_or_labels": no_synthetic_strings(tables),
        "manual_global_response_has_54_sessions_and_9_targets": (
            session_bins["session_id"].nunique() == 54 and session_bins["target_roi"].nunique() == 9
        ),
        "global_claimed_bins_require_all_nine_targets": response.loc[
            response["all_nine_targets"], "contributing_target_roi"
        ].eq(9).all(),
        "global_low_force_sensitivity_recomputes": slope_ok,
        "hierarchical_bootstrap_has_1000_finite_replicates": bootstrap_ok,
        "global_detection_average_and_guardrail_recompute": detection_ok,
        "global_lag_is_manual_only_54_sessions": (
            tables["global_system_lag_session"]["archive_id"].eq("manual").all()
            and tables["global_system_lag_session"]["session_id"].nunique() == 54
        ),
        "global_model_outer_holdout_has_no_session_leakage": bool(no_outer_leakage),
        "global_model_predictions_unique_at_declared_grain": bool(prediction_unique),
        "global_model_covers_six_groups_54_sessions_9_targets": bool(model_coverage),
        "global_model_summary_recomputes": bool(model_summary_ok),
        "zero_to_five_model_comparator_recomputes": bool(comparator_ok),
        "live_sensor_outer_prediction_hashes_match": bool(live_input_hash_ok),
        "live_sensor_operating_range_r2_recomputes": bool(live_metrics_ok),
        "automatic_global_evidence_is_normalized_hysteresis_only": bool(automatic_ok),
        "six_figure_bundles_and_metadata_valid": figure_ok,
        "no_infinite_numeric_outputs": finite_ok,
        "upstream_quality_gate_pass": bool(quality.get("gate_pass")),
    }
    report = {
        "schema_version": "1.0.0",
        "created_at": base.utc_now(),
        "global_run_id": manifest["run_id"],
        "gate_pass": all(checks.values()),
        "checks": checks,
        "overall_assessment": "share-with-caveats" if all(checks.values()) else "needs-revision",
        "methodology_review": (
            "The global estimand sums all nine floor-corrected positive optical channels, balances within session and target ROI, "
            "requires all nine targets for device-wide claims, and retains worst-target detection as a guardrail. Model comparisons "
            "use held-out TEST groups with nested ridge selection. The primary model rerun uses the 1.7-3.0 N range configured in "
            "the experimental live-sensor implementation, while the original 0-5 N benchmark is retained as a comparator."
        ),
        "calculation_spot_checks": {
            "reported_global_sensitivity_light_per_N": float(summary["global_low_force_sensitivity_light_per_N"]),
            "recomputed_global_sensitivity_light_per_N": recomputed_slope,
            "bootstrap_replicates": int(len(bootstrap)),
            "best_model": str(model_summary.iloc[0]["model"]),
            "best_model_median_session_mae_N": float(model_summary.iloc[0]["median_session_mae_N"]),
            "best_operating_range_r2_model": str(model_summary.sort_values("r2", ascending=False).iloc[0]["model"]),
            "best_operating_range_r2": float(model_summary["r2"].max()),
            "live_sensor_conditional_session_force_bin_r2": recomputed_live_binned_r2,
            "total_light_linear_median_session_mae_N": float(
                model_summary.loc[model_summary["model"].eq("total_light_linear"), "median_session_mae_N"].iloc[0]
            ),
        },
        "required_caveats": [
            "The global positive sensitivity estimate has a wide hierarchical bootstrap interval; a positive point estimate is not a device-wide sensitivity guarantee.",
            "Global optical detection is not established under either the average-only or worst-target guarded rule.",
            "A global output sacrifices localization and cannot replace per-ROI heterogeneity or cross-talk evidence.",
            "The best held-out calibration benchmark remains weak in absolute terms and must not be presented as deployment-ready.",
            "Restricting evaluation to the experimental 1.7-3.0 N implementation range lowers absolute error but does not improve held-out R-squared; the narrower outcome variance makes R-squared harder to improve.",
            "The 1.7-3.0 N interval is configured in a post-hoc experimental bundle and is not the authoritative common safe range, whose gate failed.",
            "Automatic global curves are normalized hysteresis shapes only; normalization removes absolute amplitude.",
            "No synthetic data were added and no empirical observations were modified.",
        ],
        "details": {
            "hash_mismatches": hash_mismatches,
            "figure_issues": figure_issues,
            "tables_with_infinity": infinity_tables,
        },
    }
    base.write_json(output / "validation_report.json", report)
    print(json.dumps({"gate_pass": report["gate_pass"], "checks": len(checks), "report": str(output / "validation_report.json")}, indent=2))
    return 0 if report["gate_pass"] else 1


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--output", default="analysis_outputs/live_sensor_study/characterization/evidence-products-global-characterization")
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
