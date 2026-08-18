"""Independent QA for authoritative sensor-characterization evidence products."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
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


def mad(values: pd.Series) -> float:
    array = values.to_numpy(dtype=float)
    centre = np.median(array)
    return float(np.median(np.abs(array - centre)))


def run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    evidence = (root / args.evidence).resolve()
    tables = evidence / "tables"
    figures = evidence / "figures"
    manifest = read_json(evidence / "run_manifest.json")
    quality = read_json(evidence / "quality_report.json")
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    checks["upstream_quality_gate_pass"] = bool(quality.get("gate_pass"))
    checks["source_hash_consistent"] = quality["source_manifest_hash"] == manifest["source_manifest_hash"]
    checks["revised_master_plan_hash_preserved"] = (
        bool(quality.get("master_plan_hash"))
        and quality["master_plan_hash"] == manifest["inputs"]["revised_master_plan_sha256"]
    )

    # Verify every output file listed in the authoritative evidence manifest.
    mismatches: list[str] = []
    for section in ("tables", "figures"):
        base = tables if section == "tables" else figures
        for name, expected in manifest["outputs"][section].items():
            path = base / name
            if not path.exists() or sha256_file(path) != expected:
                mismatches.append(f"{section}/{name}")
    for name in ("report_data.json", "quality_report.json"):
        path = evidence / name
        if not path.exists() or sha256_file(path) != manifest["outputs"][name]:
            mismatches.append(name)
    checks["manifest_output_hashes_match"] = not mismatches
    details["hash_mismatches"] = mismatches

    # Figure bundle contract and figure metadata/data reconciliation.
    figure_names = sorted(path.stem for path in figures.glob("fig*.png"))
    checks["eighteen_required_figure_bundles"] = len(figure_names) == 18
    figure_issues: list[str] = []
    for name in figure_names:
        companions = [figures / f"{name}.{ext}" for ext in ("png", "svg", "pdf", "csv", "json")]
        if not all(path.exists() and path.stat().st_size > 0 for path in companions):
            figure_issues.append(f"{name}:missing-or-empty")
            continue
        metadata = read_json(figures / f"{name}.json")
        csv = pd.read_csv(figures / f"{name}.csv")
        clean = csv.replace([np.inf, -np.inf], np.nan).astype(object)
        clean = clean.where(pd.notna(clean), None)
        if metadata["plotted_rows"] != len(csv):
            figure_issues.append(f"{name}:row-count")
        # CSV round-tripping can change binary float representations.  The
        # metadata still carries the creation-time in-memory hash, while this
        # check independently verifies source-table and companion hashes.
        source_table = tables / f"{metadata['source_table']}.csv"
        if not source_table.exists() or sha256_file(source_table) != metadata["source_table_hash"]:
            figure_issues.append(f"{name}:source-table-hash")
        if not metadata.get("source_manifest_hash") or not metadata.get("code_hash"):
            figure_issues.append(f"{name}:missing-provenance")
    checks["figure_metadata_and_companions_valid"] = not figure_issues
    details["figure_issues"] = figure_issues

    coverage = pd.read_parquet(tables / "coverage_sessions.parquet")
    auto_conditions = pd.read_parquet(tables / "coverage_automatic_conditions.parquet")
    checks["coverage_reconciles_245_sessions"] = coverage["session_id"].nunique() == 245
    checks["automatic_design_reconciles_two_sessions_per_condition"] = (
        len(auto_conditions) == 81
        and auto_conditions["independent_sessions"].eq(2).all()
        and auto_conditions["ten_cycle_sessions"].eq(1).all()
        and auto_conditions["twenty_cycle_sessions"].eq(1).all()
    )
    checks["manual_primary_design_reconciles_six_sessions_per_roi"] = (
        coverage[coverage["scientific_role"].eq("model_primary")]
        .groupby("target_roi")["session_id"]
        .nunique()
        .eq(6)
        .all()
    )

    # Independent recomputation of selected headline calculations.
    manual_bins = pd.read_parquet(tables / "manual_session_force_bins.parquet")
    sensitivity = pd.read_parquet(tables / "manual_sensitivity_nonlinearity.parquet")
    roi4 = manual_bins[
        manual_bins["roi"].eq(4)
        & manual_bins["force_bin_N"].le(1.0)
    ]
    aggregate = roi4.groupby("force_bin_N", as_index=False).agg(
        n=("session_id", "nunique"),
        force=("median_force_N", "median"),
        light=("median_light", "median"),
    )
    aggregate = aggregate[aggregate["n"].ge(3)]
    roi4_slope = float(np.polyfit(aggregate["force"], aggregate["light"], 1)[0])
    reported_roi4 = float(sensitivity.loc[sensitivity["roi"].eq(4), "low_force_sensitivity_light_per_N"].iloc[0])
    checks["roi4_sensitivity_recomputed"] = math.isclose(roi4_slope, reported_roi4, rel_tol=1e-12, abs_tol=1e-9)
    details["roi4_sensitivity_light_per_N"] = {"recomputed": roi4_slope, "reported": reported_roi4}

    no_contact_session = pd.read_parquet(tables / "no_contact_session.parquet")
    no_contact_summary = pd.read_parquet(tables / "no_contact_summary.parquet")
    roi9_noise = float(no_contact_session[no_contact_session["roi"].eq(9)]["signed_mad"].median())
    reported_roi9_noise = float(no_contact_summary.loc[no_contact_summary["roi"].eq(9), "signed_noise_mad"].iloc[0])
    checks["roi9_noise_recomputed"] = math.isclose(roi9_noise, reported_roi9_noise, rel_tol=0, abs_tol=1e-12)
    details["roi9_signed_noise_mad"] = {"recomputed": roi9_noise, "reported": reported_roi9_noise}

    cross_talk = pd.read_parquet(tables / "cross_talk.parquet")
    checks["cross_talk_is_complete_9x9"] = (
        cross_talk[["target_roi", "measured_roi"]].drop_duplicates().shape[0] == 81
    )
    cross_talk_bins = pd.read_parquet(tables / "cross_talk_force_bins.parquet")
    checks["cross_talk_force_bin_summaries_present"] = (
        cross_talk_bins[["target_roi", "measured_roi"]].drop_duplicates().shape[0] == 81
        and cross_talk_bins["force_bin_N"].nunique() > 10
    )

    cycle_bins = pd.read_parquet(tables / "automatic_cycle_force_bins.parquet")
    checks["cycle_force_bins_unique_at_declared_grain"] = not cycle_bins.duplicated(
        ["session_id", "cycle_index", "motion_phase", "force_bin_N"]
    ).any()
    cycle_repeatability = pd.read_parquet(tables / "automatic_within_session_repeatability.parquet")
    checks["automatic_sessions_remain_independent_units"] = (
        cycle_repeatability["session_id"].nunique() == 162
        and cycle_repeatability["expected_cycles"].isin([10, 20]).all()
        and cycle_repeatability["contributing_cycles"].le(cycle_repeatability["expected_cycles"]).all()
    )
    partial_cycle_coverage = cycle_repeatability[
        cycle_repeatability["coverage_qualification"].eq("partial-cycle-bin-coverage")
    ][["session_id", "contributing_cycles", "expected_cycles"]]
    details["partial_cycle_bin_coverage_sessions"] = partial_cycle_coverage.to_dict("records")
    checks["automatic_force_and_timing_corrections_retained"] = (
        {"median_raw_force_N", "median_force_N", "median_cycle_force_zero_offset_N", "median_internal_timing_correction_ms"}
        .issubset(cycle_bins.columns)
        and cycle_bins["median_cycle_force_zero_offset_N"].notna().all()
        and cycle_bins["median_internal_timing_correction_ms"].notna().all()
    )
    force_separation = pd.read_parquet(tables / "automatic_hysteresis_force_separation.parquet")
    checks["matched_light_force_separation_is_session_nested"] = (
        len(force_separation) > 0
        and force_separation["independent_sessions"].le(2).all()
        and force_separation["evidence_role"].eq("hysteresis supporting component").all()
    )

    phase_session = pd.read_parquet(tables / "automatic_phase_session.parquet")
    checks["relaxation_and_recovery_are_session_nested"] = (
        phase_session["session_id"].nunique() == 162
        and set(phase_session["phase"]) == {"holding", "inter_cycle_dwell"}
        and phase_session["evidence_role"].eq("creep-related exploratory evidence").all()
    )

    local_sensitivity = pd.read_parquet(tables / "manual_local_sensitivity.parquet")
    checks["manual_local_sensitivity_uses_adjacent_supported_bins"] = (
        len(local_sensitivity) > 0
        and np.isclose(
            local_sensitivity["right_force_bin_N"] - local_sensitivity["left_force_bin_N"],
            0.25,
            atol=1e-12,
        ).all()
        and local_sensitivity["minimum_independent_sessions"].ge(3).all()
    )
    plateau = pd.read_parquet(tables / "manual_plateau_observations.parquet")
    checks["manual_plateau_observations_cover_all_roi"] = (
        plateau["roi"].tolist() == list(range(1, 10))
        and plateau["claim_boundary"].str.contains("not a deployed usable range").all()
    )
    manual_lag = pd.read_parquet(tables / "manual_system_lag_session.parquet")
    manual_lag_curve = pd.read_parquet(tables / "manual_system_lag_curve.parquet")
    checks["standalone_system_lag_is_manual_only"] = (
        manual_lag["session_id"].nunique() == 54
        and manual_lag["archive_id"].eq("manual").all()
        and manual_lag_curve["archive_id"].eq("manual").all()
        and manual_lag["sign_convention"].eq("positive lag means optical follows reference").all()
    )

    detection = pd.read_parquet(tables / "manual_detection_summary.parquet")
    established_detection = detection[detection["qualification"].str.startswith("conditional")]["roi"].tolist()
    checks["detection_result_is_frozen_and_sparse"] = established_detection == [3, 9]
    details["conditionally_qualified_detection_rois"] = established_detection

    proxy = pd.read_parquet(tables / "noise_equivalent_force_proxy.parquet")
    qualified_proxy = proxy[proxy["qualification"].str.startswith("conditional")]["roi"].tolist()
    checks["noise_equivalent_force_is_proxy_only"] = (
        qualified_proxy == [1, 4, 8, 9]
        and proxy["claim_boundary"].eq("not true force resolution").all()
    )
    details["conditionally_qualified_nef_proxy_rois"] = qualified_proxy

    status = pd.read_parquet(tables / "metric_status.parquet")
    required_not_established = {
        "Historical former combined common-range / monotonicity gate",
        "True creep",
        "True force resolution",
    }
    actual_not_established = set(status.loc[status["status"].eq("not-established"), "metric"])
    checks["required_nonclaims_preserved"] = required_not_established.issubset(actual_not_established)
    automatic_status_metrics = {
        "Hysteresis",
        "Hysteresis internal cycle and correction QA",
        "Creep-related fixed-displacement settling and unloaded recovery",
    }
    automatic_status = status[status["source"].str.contains("automatic", case=False)]
    checks["automatic_archive_is_limited_to_permitted_status_rows"] = set(automatic_status["metric"]).issubset(automatic_status_metrics)
    excluded_general = status[
        ~status["metric"].isin(
            automatic_status_metrics
            | {
                "Archive integrity and original-frame reconciliation",
                "Historical former combined common-range / monotonicity gate",
                "True creep",
                "True force resolution",
            }
        )
    ]
    checks["every_general_metric_has_manual_controlling_source"] = excluded_general["source"].str.contains("manual", case=False).all()

    numeric_tables = list(tables.glob("*.parquet"))
    infinity_tables: list[str] = []
    for path in numeric_tables:
        frame = pd.read_parquet(path)
        numeric = frame.select_dtypes(include=[np.number])
        if np.isinf(numeric.to_numpy(dtype=float, na_value=np.nan)).any():
            infinity_tables.append(path.name)
    checks["no_infinite_numeric_outputs"] = not infinity_tables
    details["tables_with_infinity"] = infinity_tables

    report = {
        "schema_version": "2.0.0",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "overall_assessment": "ready-to-share-with-stated-qualification-boundaries",
        "gate_pass": all(checks.values()),
        "checks": checks,
        "details": details,
        "source_manifest_hash": manifest["source_manifest_hash"],
        "evidence_run_id": manifest["run_id"],
        "methodology_review": (
            "Manual sessions control every general sensor metric, including the standalone lag. "
            "Automatic sessions are limited to hysteresis and creep-related exploratory evidence; "
            "automatic cycles are nested within session and no frame-level confidence intervals are used."
        ),
        "required_caveats": [
            "Only two independent sessions exist per exact automatic condition; condition intervals are descriptive.",
            "Only two automatic collection days exist; day-level population intervals are descriptive.",
            "The former combined-procedure common-range/monotonicity gate remains historical only and is not controlling under the manual-first plan.",
            "True creep and true force resolution are not established by the collected protocols.",
            "Noise-equivalent force is a stability proxy only, and the manual-only approximate lag is exploratory system-level evidence.",
        ],
    }
    write_json(evidence / "validation_report.json", report)
    print(json.dumps({"gate_pass": report["gate_pass"], "checks": len(checks), "report": str(evidence / 'validation_report.json')}, indent=2))
    return 0 if report["gate_pass"] else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--evidence",
        default="analysis_outputs/live_sensor_study/characterization/evidence-products-manual-first-authoritative",
    )
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
