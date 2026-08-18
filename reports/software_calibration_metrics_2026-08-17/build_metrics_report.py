from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[2]
OUTPUT_DIR = Path(__file__).resolve().parent

MANUAL_BASE = Path(
    "calibration_gui/analysis_outputs/live_sensor_study/characterization/"
    "evidence-products-manual-first-authoritative"
)
GLOBAL_BASE = Path(
    "calibration_gui/analysis_outputs/live_sensor_study/characterization/"
    "evidence-products-global-characterization"
)
LIVE_BASE = Path("calibration_gui/analysis_outputs/live_sensor_manual_only")
LEGACY_BASE = Path("analysis_outputs")


def load_json(relative_path: Path | str) -> dict[str, Any]:
    with (WORKSPACE / relative_path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_csv(relative_path: Path | str) -> list[dict[str, str]]:
    with (WORKSPACE / relative_path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def integer(value: Any) -> int | None:
    parsed = number(value)
    return int(parsed) if parsed is not None else None


def rounded(value: Any, digits: int = 4) -> float | None:
    parsed = number(value)
    return round(parsed, digits) if parsed is not None else None


def safe_median(values: list[float]) -> float | None:
    clean = [v for v in values if math.isfinite(v)]
    return statistics.median(clean) if clean else None


def source(
    source_id: str,
    label: str,
    relative_path: Path | str,
    *,
    sql: str | None = None,
    description: str | None = None,
    tables_used: list[str] | None = None,
    metric_definitions: list[str] | None = None,
) -> dict[str, Any]:
    normalized_path = str(relative_path).replace("\\", "/")
    suffix = Path(normalized_path).suffix.lower()
    if sql is None:
        if suffix == ".csv":
            sql = f"SELECT * FROM read_csv_auto('{normalized_path}', header = true)"
        elif suffix == ".json":
            sql = f"SELECT * FROM read_json_auto('{normalized_path}')"
        elif suffix in {".md", ".txt"}:
            sql = f"SELECT * FROM read_text('{normalized_path}')"
        else:
            sql = f"SELECT '{normalized_path}' AS source_path"
    return {
        "id": source_id,
        "label": label,
        "path": normalized_path,
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": sql,
            "description": description or f"Reads the reviewed source file for {label.lower()}.",
            "tables_used": tables_used or [normalized_path],
            "metric_definitions": metric_definitions or [],
        },
    }


def web_source(
    source_id: str,
    label: str,
    url: str,
    *,
    description: str,
    metric_definitions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": source_id,
        "label": label,
        "href": url,
        "query": {
            "engine": "primary-literature",
            "language": "text",
            "url": url,
            "description": description,
            "tables_used": [url],
            "metric_definitions": metric_definitions or [],
        },
    }


def markdown(block_id: str, body: str, source_id: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"id": block_id, "type": "markdown", "body": body, "layout": "full"}
    if source_id:
        block["sourceId"] = source_id
    return block


def chart_block(block_id: str, chart_id: str) -> dict[str, Any]:
    return {"id": block_id, "type": "chart", "chartId": chart_id, "layout": "full"}


def table_block(block_id: str, table_id: str) -> dict[str, Any]:
    return {"id": block_id, "type": "table", "tableId": table_id, "layout": "full"}


def metric_card(
    card_id: str,
    dataset: str,
    source_id: str,
    description: str,
    metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": card_id,
        "dataset": dataset,
        "sourceId": source_id,
        "description": description,
        "metrics": metrics,
    }


def native_chart(
    chart_id: str,
    title: str,
    subtitle: str,
    chart_type: str,
    dataset: str,
    source_id: str,
    x: dict[str, Any],
    y: dict[str, Any],
    *,
    color: dict[str, Any] | None = None,
    label: dict[str, Any] | None = None,
    tooltip: list[dict[str, Any]] | None = None,
    palette: dict[str, Any] | None = None,
    reference_lines: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    value_format: str = "number",
    unit: str | None = None,
) -> dict[str, Any]:
    encodings: dict[str, Any] = {"x": x, "y": y}
    if color:
        encodings["color"] = color
    if label:
        encodings["label"] = label
    if tooltip:
        encodings["tooltip"] = tooltip
    chart: dict[str, Any] = {
        "id": chart_id,
        "title": title,
        "subtitle": subtitle,
        "showDescription": True,
        "intent": "comparison" if chart_type != "scatter" else "relationship",
        "type": chart_type,
        "dataset": dataset,
        "sourceId": source_id,
        "encodings": encodings,
        "valueFormat": value_format,
        "layout": "full",
        "surface": {"surface": "card", "viewMode": "visualization"},
    }
    if palette:
        chart["palette"] = palette
    if reference_lines:
        chart["referenceLines"] = reference_lines
    if settings:
        chart["settings"] = settings
    if unit:
        chart["unit"] = unit
    return chart


def native_table(
    table_id: str,
    title: str,
    subtitle: str,
    dataset: str,
    source_id: str,
    columns: list[dict[str, Any]],
    sort_field: str,
    sort_direction: str = "asc",
    density: str = "spacious",
) -> dict[str, Any]:
    return {
        "id": table_id,
        "title": title,
        "subtitle": subtitle,
        "showDescription": True,
        "dataset": dataset,
        "sourceId": source_id,
        "defaultSort": {"field": sort_field, "direction": sort_direction},
        "density": density,
        "layout": "full",
        "columns": columns,
    }


manual_report_path = MANUAL_BASE / "report_data.json"
metric_status_path = MANUAL_BASE / "tables/metric_status.csv"
sensitivity_path = MANUAL_BASE / "tables/manual_sensitivity_nonlinearity.csv"
plateau_path = MANUAL_BASE / "tables/manual_plateau_observations.csv"
cross_talk_path = MANUAL_BASE / "tables/cross_talk.csv"
no_contact_path = MANUAL_BASE / "tables/no_contact_summary.csv"
detection_path = MANUAL_BASE / "tables/manual_detection_summary.csv"
nef_path = MANUAL_BASE / "tables/noise_equivalent_force_proxy.csv"
lag_path = MANUAL_BASE / "tables/manual_system_lag_summary.csv"
hysteresis_path = MANUAL_BASE / "tables/automatic_hysteresis.csv"
cycle_path = MANUAL_BASE / "tables/automatic_within_session_repeatability.csv"

global_model_path = GLOBAL_BASE / "tables/global_model_cv_summary.csv"
global_range_path = GLOBAL_BASE / "tables/live_sensor_operating_range_r2.csv"
global_validation_path = GLOBAL_BASE / "validation_report.json"
global_quality_path = GLOBAL_BASE / "quality_report.json"

hybrid_path = LIVE_BASE / (
    "model_runs/experimental_hybrid/experimental-hybrid-bedb31cb1e7c1541/metrics.json"
)
event_benchmark_path = LIVE_BASE / "model_runs/event_localization_candidate_benchmark.json"
force_benchmark_path = LIVE_BASE / "model_runs/hybrid_candidate_benchmark.json"
application_gate_path = LIVE_BASE / "MANUAL_ONLY_APPLICATION_GATE_FAILURE_REPORT.md"
recovery_report_path = LIVE_BASE / "MANUAL_ONLY_EXPERIMENTAL_RECOVERY_REPORT.md"

legacy_auto_path = LEGACY_BASE / "linear_models/validation_metrics.json"
legacy_manual_path = LEGACY_BASE / "manual_linear_models/validation_metrics.json"
legacy_clean_path = LEGACY_BASE / "clean_global_force_model/validation_metrics.json"
legacy_global_path = LEGACY_BASE / "global_force_model/model_comparison.csv"
legacy_archive_summary_path = LEGACY_BASE / "analysis_summary.json"


manual_report = load_json(manual_report_path)
metric_status_raw = load_csv(metric_status_path)
sensitivity_raw = load_csv(sensitivity_path)
plateau_raw = load_csv(plateau_path)
cross_talk_raw = load_csv(cross_talk_path)
no_contact_raw = load_csv(no_contact_path)
detection_raw = load_csv(detection_path)
nef_raw = load_csv(nef_path)
lag_raw = load_csv(lag_path)
hysteresis_raw = load_csv(hysteresis_path)
cycle_raw = load_csv(cycle_path)

global_model_raw = load_csv(global_model_path)
global_range_raw = load_csv(global_range_path)
global_validation = load_json(global_validation_path)
global_quality = load_json(global_quality_path)

hybrid = load_json(hybrid_path)
event_benchmark = load_json(event_benchmark_path)
force_benchmark = load_json(force_benchmark_path)

legacy_auto = load_json(legacy_auto_path)
legacy_manual = load_json(legacy_manual_path)
legacy_clean = load_json(legacy_clean_path)
legacy_global_raw = load_csv(legacy_global_path)
legacy_archive_summary = load_json(legacy_archive_summary_path)

headline = manual_report["headline"]
hybrid_agg = hybrid["aggregate"]


# Coverage and evidence-status datasets.
data_coverage = [
    {"evidence_group": "All complete sessions", "sessions": headline["sessions"], "role": "Total"},
    {"evidence_group": "Automatic characterization", "sessions": headline["automatic_sessions"], "role": "Characterization"},
    {"evidence_group": "Manual primary", "sessions": headline["manual_primary_sessions"], "role": "Calibration"},
    {"evidence_group": "Dedicated no-contact", "sessions": headline["no_contact_sessions"], "role": "Noise baseline"},
    {"evidence_group": "Replay only", "sessions": headline["replay_only_sessions"], "role": "Replay"},
]

status_label_map = {
    "primary": "Primary",
    "conditional-qualified": "Conditionally qualified",
    "exploratory-insufficient": "Exploratory / insufficient",
    "not-established": "Not established",
}
status_counts = Counter(row["status"] for row in metric_status_raw)
evidence_status = [
    {
        "status": status_label_map.get(status, status.replace("-", " ").title()),
        "metric_count": count,
        "status_key": status,
        "share": count / len(metric_status_raw),
    }
    for status, count in status_counts.items()
]
evidence_status.sort(key=lambda row: (-row["metric_count"], row["status"]))

metric_status = [
    {
        "metric": row["metric"],
        "status": status_label_map.get(row["status"], row["status"]),
        "independent_evidence": row["independent_evidence"],
        "claim_boundary": row["claim_boundary"],
        "source_scope": row["source"],
    }
    for row in metric_status_raw
]

validation_gates = [
    {
        "gate": "Archive and original-frame reconciliation",
        "status": "Passed",
        "evidence": "245 complete sessions; 179,176 source-aligned frames",
        "implication": "The retained characterization dataset is internally reconciled.",
    },
    {
        "gate": "Manual-first characterization quality",
        "status": "Passed",
        "evidence": "Authoritative quality and validation reports passed",
        "implication": "Characterization results may be shared with their stated boundaries.",
    },
    {
        "gate": "Global characterization validation",
        "status": "Share with caveats",
        "evidence": "All calculation and lineage checks passed",
        "implication": "Global summaries are valid but do not establish deployment readiness.",
    },
    {
        "gate": "Common supported all-ROI force range",
        "status": "Failed",
        "evidence": "All nine ROI failed the frozen monotonic gate",
        "implication": "No authoritative common safe force range exists.",
    },
    {
        "gate": "Manual-only application preprocessing",
        "status": "Failed",
        "evidence": "Best supported-bin recall remained below the required 90%",
        "implication": "The frozen application track did not authorize model promotion.",
    },
    {
        "gate": "Force resolution",
        "status": "Failed / not established",
        "evidence": "4.49% MAE gain, Spearman 0.136, low response spread",
        "implication": "Newton output remains approximate and experimental.",
    },
    {
        "gate": "True creep",
        "status": "Not established",
        "evidence": "No qualifying constant-force holds of at least 5 s",
        "implication": "Settling/recovery observations cannot be labeled creep.",
    },
]


# Per-ROI characterization dataset.
sensitivity_by_roi = {integer(row["roi"]): row for row in sensitivity_raw}
plateau_by_roi = {integer(row["roi"]): row for row in plateau_raw}
no_contact_by_roi = {integer(row["roi"]): row for row in no_contact_raw}
detection_by_roi = {integer(row["roi"]): row for row in detection_raw}
nef_by_roi = {integer(row["roi"]): row for row in nef_raw}
lag_by_roi = {integer(row["roi"]): row for row in lag_raw}

cross_talk_by_target: dict[int, list[dict[str, str]]] = defaultdict(list)
for row in cross_talk_raw:
    target = integer(row["target_roi"])
    if target is not None:
        cross_talk_by_target[target].append(row)

v4_force_mae = hybrid["force_source_metrics"]["aggregate"]["per_roi_conditional_force_mae_N"]
v4_event_recall = hybrid_agg["per_roi_event_localization_recall"]

roi_characterization: list[dict[str, Any]] = []
cross_talk_summary: list[dict[str, Any]] = []
noise_drift: list[dict[str, Any]] = []
lag_summary: list[dict[str, Any]] = []
v4_roi_metrics: list[dict[str, Any]] = []

for roi in range(1, 10):
    sensitivity = sensitivity_by_roi[roi]
    plateau = plateau_by_roi[roi]
    no_contact = no_contact_by_roi[roi]
    detection = detection_by_roi[roi]
    nef = nef_by_roi[roi]
    lag = lag_by_roi[roi]
    ct_rows = cross_talk_by_target[roi]
    target_share = next(
        (number(row["median_share_total"]) for row in ct_rows if integer(row["measured_roi"]) == roi),
        None,
    )
    max_off_target_share = max(
        [number(row["median_share_total"]) or 0.0 for row in ct_rows if integer(row["measured_roi"]) != roi],
        default=0.0,
    )

    row = {
        "roi": roi,
        "roi_label": f"ROI {roi}",
        "low_force_sensitivity_light_per_N": rounded(sensitivity["low_force_sensitivity_light_per_N"], 3),
        "nonlinearity_fraction": (
            (number(sensitivity["nonlinearity_percent_supported_low_force_span"]) or 0.0) / 100.0
            if number(sensitivity["nonlinearity_percent_supported_low_force_span"]) is not None
            else None
        ),
        "supported_force_max_N": rounded(plateau["supported_force_max_N"], 3),
        "plateau_observation": plateau["plateau_observation"],
        "noise_mad_light": rounded(no_contact["signed_noise_mad"], 2),
        "median_abs_drift_light_per_min": rounded(no_contact["median_abs_drift_light_per_min"], 2),
        "optical_detection_region_N": rounded(detection["optical_detection_region_N"], 3),
        "noise_equivalent_force_proxy_N": rounded(nef["noise_equivalent_force_N"], 3),
        "median_system_lag_ms": rounded(lag["median_best_lag_ms"], 1),
        "target_channel_share": rounded(target_share, 4),
        "max_off_target_share": rounded(max_off_target_share, 4),
        "v4_conditional_force_mae_N": rounded(v4_force_mae[str(roi)], 3),
        "v4_event_localization_recall": rounded(v4_event_recall[str(roi)], 4),
    }
    roi_characterization.append(row)
    cross_talk_summary.extend(
        [
            {"roi_label": f"ROI {roi}", "component": "Target channel share", "share": rounded(target_share, 4), "roi": roi},
            {"roi_label": f"ROI {roi}", "component": "Largest off-target share", "share": rounded(max_off_target_share, 4), "roi": roi},
        ]
    )
    noise_drift.append(
        {
            "roi": roi,
            "roi_label": f"ROI {roi}",
            "noise_mad_light": rounded(no_contact["signed_noise_mad"], 2),
            "drift_light_per_min": rounded(no_contact["median_abs_drift_light_per_min"], 2),
            "sessions": integer(no_contact["independent_sessions"]),
        }
    )
    lag_summary.append(
        {
            "roi": roi,
            "roi_label": f"ROI {roi}",
            "median_lag_ms": rounded(lag["median_best_lag_ms"], 1),
            "minimum_lag_ms": rounded(lag["minimum_best_lag_ms"], 1),
            "maximum_lag_ms": rounded(lag["maximum_best_lag_ms"], 1),
            "median_abs_correlation": rounded(lag["median_best_absolute_correlation"], 3),
            "sessions": integer(lag["independent_sessions"]),
        }
    )
    v4_roi_metrics.append(
        {
            "roi": roi,
            "roi_label": f"ROI {roi}",
            "event_localization_recall": rounded(v4_event_recall[str(roi)], 4),
            "conditional_force_mae_N": rounded(v4_force_mae[str(roi)], 4),
        }
    )


# Automatic-cycle effects, aggregated to an honest presentation grain.
hysteresis_groups: dict[tuple[int, int], list[float]] = defaultdict(list)
for row in hysteresis_raw:
    roi = integer(row["roi"])
    speed = integer(row["speed_mm_min"])
    value = number(row["median_hysteresis_percent_observed_span"])
    if roi is not None and speed is not None and value is not None:
        hysteresis_groups[(roi, speed)].append(abs(value) / 100.0)

hysteresis_summary = [
    {
        "roi": roi,
        "roi_label": f"ROI {roi}",
        "speed_mm_min": speed,
        "speed_label": f"{speed} mm/min",
        "median_abs_hysteresis_fraction": rounded(safe_median(values), 5),
        "force_bin_observations": len(values),
    }
    for (roi, speed), values in sorted(hysteresis_groups.items())
]

cycle_groups: dict[int, list[float]] = defaultdict(list)
cycle_session_counts: Counter[int] = Counter()
for row in cycle_raw:
    roi = integer(row["roi"])
    value = number(row["median_within_cycle_cv"])
    if roi is not None and value is not None:
        cycle_groups[roi].append(value)
        cycle_session_counts[roi] += 1

cycle_summary = [
    {
        "roi": roi,
        "roi_label": f"ROI {roi}",
        "median_within_cycle_cv": rounded(safe_median(values), 5),
        "independent_sessions": cycle_session_counts[roi],
    }
    for roi, values in sorted(cycle_groups.items())
]

auto_effects_table: list[dict[str, Any]] = []
cycle_lookup = {row["roi"]: row for row in cycle_summary}
for row in hysteresis_summary:
    auto_effects_table.append(
        {
            **row,
            "median_within_cycle_cv": cycle_lookup[row["roi"]]["median_within_cycle_cv"],
            "cycle_sessions": cycle_lookup[row["roi"]]["independent_sessions"],
        }
    )


# Experimental event/localization and force-model datasets.
localization_model_rows = []
for model_name, values in event_benchmark["aggregate"].items():
    display = {
        "argmax": "Accumulated argmax",
        "extra_trees": "Extra Trees",
        "logistic": "Multinomial logistic",
        "shrinkage_lda": "Shrinkage LDA",
    }.get(model_name, model_name)
    localization_model_rows.append(
        {
            "model": display,
            "model_key": model_name,
            "accuracy": rounded(values["accuracy"], 5),
            "macro_f1": rounded(values["macro_f1"], 5),
            "minimum_fold_macro_f1": rounded(values["minimum_fold_macro_f1"], 5),
            "events": values["event_count"],
            "sessions": values["session_count"],
        }
    )
localization_model_rows.sort(key=lambda row: row["macro_f1"], reverse=True)

force_fold_rows: list[dict[str, Any]] = []
force_fold_table: list[dict[str, Any]] = []
for fold in force_benchmark["folds"]:
    fold_label = f"Fold {fold['outer_fold']}"
    force_fold_rows.extend(
        [
            {
                "fold": fold_label,
                "fold_number": fold["outer_fold"],
                "series": "Selected candidate",
                "mae_N": rounded(fold["conditional_force_mae_N"], 4),
            },
            {
                "fold": fold_label,
                "fold_number": fold["outer_fold"],
                "series": "Fold constant",
                "mae_N": rounded(fold["constant_force_mae_N"], 4),
            },
        ]
    )
    force_fold_table.append(
        {
            "fold": fold_label,
            "selected_family": fold["selected_family"].replace("_", " ").title(),
            "selected_candidate": fold["selected_candidate"],
            "mae_N": rounded(fold["conditional_force_mae_N"], 4),
            "constant_mae_N": rounded(fold["constant_force_mae_N"], 4),
            "relative_gain": rounded(fold["relative_mae_gain_over_constant"], 5),
            "spearman_rho": rounded(fold["force_spearman_rho"], 4),
            "lag_ms": fold["lag_ms"],
        }
    )

hybrid_metrics_table = [
    {"metric": "Independent primary sessions", "value": str(hybrid_agg["independent_session_count"]), "status": "Recovery evidence"},
    {"metric": "Outer-fold events", "value": str(hybrid_agg["event_count"]), "status": "Recovery evidence"},
    {"metric": "Minimum fold contact recall", "value": f"{hybrid_agg['min_contact_recall']:.2%}", "status": "Post-hoc"},
    {"metric": "Maximum no-contact frame FPR", "value": f"{hybrid_agg['max_no_contact_frame_fpr']:.2%}", "status": "Post-hoc"},
    {"metric": "Event localization macro F1", "value": f"{hybrid_agg['event_localization_macro_f1']:.2%}", "status": "Post-hoc"},
    {"metric": "Selective localization macro F1", "value": f"{hybrid_agg['selective_localization_macro_f1']:.2%}", "status": "Post-hoc"},
    {"metric": "Selective localization coverage", "value": f"{hybrid_agg['selective_localization_coverage']:.2%}", "status": "Post-hoc"},
    {"metric": "Conditional force MAE", "value": f"{hybrid_agg['mean_conditional_force_mae_N']:.3f} N", "status": "Approximate / unvalidated"},
    {"metric": "Cross-validated p95 absolute force error", "value": f"{hybrid_agg['cross_validated_p95_absolute_error_N']:.3f} N", "status": "Approximate / unvalidated"},
    {"metric": "Mean force Spearman rho", "value": f"{hybrid_agg['mean_force_spearman_rho']:.3f}", "status": "Below gate"},
    {"metric": "Force resolution", "value": "Not established", "status": "Failed"},
]

localization_operating_modes = [
    {
        "mode": "All events (forced output)",
        "mode_order": 1,
        "metric": "Macro F1",
        "value": rounded(hybrid_agg["event_localization_macro_f1"], 6),
    },
    {
        "mode": "All events (forced output)",
        "mode_order": 1,
        "metric": "Coverage",
        "value": 1.0,
    },
    {
        "mode": "Confidence-filtered",
        "mode_order": 2,
        "metric": "Macro F1",
        "value": rounded(hybrid_agg["selective_localization_macro_f1"], 6),
    },
    {
        "mode": "Confidence-filtered",
        "mode_order": 2,
        "metric": "Coverage",
        "value": rounded(hybrid_agg["selective_localization_coverage"], 6),
    },
]


# Representative primary-literature comparison. The score is deliberately narrow:
# it measures published evidence alignment to camera-only contact localization, not
# overall sensor quality. A point is awarded for each of five disclosed criteria:
# camera-only runtime inference; explicit contact location/state output; a quantitative
# localization/state result; a meaningful held-out unit; and uncertainty-aware
# abstention with both coverage and performance reported.
peer_comparison = [
    {
        "rank": 1,
        "sensor": "Our nine-region optical skin",
        "year": 2026,
        "focus_group": "Our system",
        "alignment_score": 5,
        "camera_only_inference": "Yes",
        "explicit_localization": "Yes — nine ROI",
        "quantitative_localization": "Yes — event macro F1",
        "held_out_unit": "Yes — grouped sessions (post-hoc recovery)",
        "abstention_reported": "Yes — F1 and coverage",
        "reported_ml_output": "Nine-class event localization; approximate normal force",
        "localization_result": "93.43% macro F1; all ROI recall >=90.28%",
        "force_result": "0.328 N conditional MAE in 1.7-3.0 N; resolution not established",
        "evaluation_scope": "102 events; 54 independent sessions; six reused outer TEST groups",
        "citation": "Internal experimental v4 metrics (2026)",
        "source_url": "local:experimental-v4-metrics",
        "comparison_caveat": "Experimental, post-hoc recovery evidence; not prospectively validated.",
    },
    {
        "rank": 2,
        "sensor": "Insight",
        "year": 2022,
        "focus_group": "Published peer",
        "alignment_score": 4,
        "camera_only_inference": "Yes",
        "explicit_localization": "Yes — continuous 3D contact",
        "quantitative_localization": "Yes — position error",
        "held_out_unit": "Yes — unseen test contact points",
        "abstention_reported": "Not reported",
        "reported_ml_output": "Contact position plus 3D force vector / force map",
        "localization_result": "~0.4 mm overall localization precision",
        "force_result": "~0.03 N force-magnitude precision; ~5 deg direction precision; up to 2 N",
        "evaluation_scope": "Test contact points absent from training; curved thumb-sized surface",
        "citation": "Sun et al., Nature Machine Intelligence (2022)",
        "source_url": "https://www.nature.com/articles/s42256-021-00439-3",
        "comparison_caveat": "Continuous position/force regression is not the same task as nine-class event localization.",
    },
    {
        "rank": 2,
        "sensor": "Minsight",
        "year": 2023,
        "focus_group": "Published peer",
        "alignment_score": 4,
        "camera_only_inference": "Yes",
        "explicit_localization": "Yes — continuous contact",
        "quantitative_localization": "Yes — position error",
        "held_out_unit": "Yes — held-out test data / trajectories",
        "abstention_reported": "Not reported",
        "reported_ml_output": "High-resolution maps of 3D contact-force vectors at 60 Hz",
        "localization_result": "0.6 mm mean contact-location error",
        "force_result": "0.07 N mean absolute force error",
        "evaluation_scope": "Fingertip-sized 360-degree surface; test trajectories separated for the classification task",
        "citation": "Andrussow et al., Advanced Intelligent Systems (2023)",
        "source_url": "https://arxiv.org/abs/2304.10990",
        "comparison_caveat": "Continuous location and force-map errors are not event-classification metrics.",
    },
    {
        "rank": 4,
        "sensor": "ELTac",
        "year": 2024,
        "focus_group": "Published peer",
        "alignment_score": 3,
        "camera_only_inference": "Yes",
        "explicit_localization": "Yes — single/multipoint",
        "quantitative_localization": "Yes — position error",
        "held_out_unit": "Not reported as a generalization split",
        "abstention_reported": "Not reported",
        "reported_ml_output": "Image processing and statistical fitting, not an ML model",
        "localization_result": "6.63 mm localization accuracy",
        "force_result": "9.3%-11.7% error over 3.1-9.4 N",
        "evaluation_scope": "Five indenters on a large-area cylindrical electroluminescent skin",
        "citation": "Fu et al., IEEE Sensors Journal (2024)",
        "source_url": "https://eprints.soton.ac.uk/499673/",
        "comparison_caveat": "Included as the closest electroluminescent hardware peer; its inference is not ML-based.",
    },
    {
        "rank": 4,
        "sensor": "OmniTact",
        "year": 2020,
        "focus_group": "Published peer",
        "alignment_score": 3,
        "camera_only_inference": "Yes — five cameras",
        "explicit_localization": "Yes — contact-angle state",
        "quantitative_localization": "Yes — angular MAE",
        "held_out_unit": "No split disclosed in the paper",
        "abstention_reported": "Not reported",
        "reported_ml_output": "Contact-angle regression and tactile-only insertion control",
        "localization_result": "Median angle MAE 1.142 deg, 1.986 deg, and 1.248 deg across three ranges",
        "force_result": "Not reported in the sensor paper",
        "evaluation_scope": "3,000 angle samples; 80% connector insertion success with top+side cameras",
        "citation": "Padmanabha et al., ICRA (2020)",
        "source_url": "https://arxiv.org/pdf/2003.06965",
        "comparison_caveat": "Contact angle and manipulation success are not spatial event-localization F1.",
    },
    {
        "rank": 6,
        "sensor": "9DTact",
        "year": 2023,
        "focus_group": "Published peer",
        "alignment_score": 2,
        "camera_only_inference": "Yes",
        "explicit_localization": "No discrete contact-location output",
        "quantitative_localization": "No comparable localization metric",
        "held_out_unit": "Yes — 18 unseen objects",
        "abstention_reported": "Not reported",
        "reported_ml_output": "3D shape reconstruction and generalizable 6D force",
        "localization_result": "No contact-location metric; shape reconstruction MAE 0.0462 mm",
        "force_result": "0.370 N mean force MAE and 0.0077 Nm torque MAE on unseen objects",
        "evaluation_scope": "100,417 image-force pairs from 175 objects; object-based holdout",
        "citation": "Lin et al., IEEE RA-L / ICRA (2023-2024)",
        "source_url": "https://arxiv.org/pdf/2308.14277",
        "comparison_caveat": "Its primary claims are shape and 6D-force generalization, not discrete event localization.",
    },
    {
        "rank": 7,
        "sensor": "DIGIT",
        "year": 2020,
        "focus_group": "Published peer",
        "alignment_score": 1,
        "camera_only_inference": "Yes",
        "explicit_localization": "Not reported as a contact output",
        "quantitative_localization": "Not reported",
        "held_out_unit": "Not reported for contact localization",
        "abstention_reported": "Not reported",
        "reported_ml_output": "Tactile keypoints and predictive dynamics for in-hand manipulation",
        "localization_result": "No contact-localization accuracy in the sensor paper",
        "force_result": "No force-estimation accuracy in the sensor paper",
        "evaluation_scope": "Compact 19 x 16 mm sensing field; 640 x 480 at 60 fps",
        "citation": "Lambeta et al., IEEE RA-L (2020)",
        "source_url": "https://www.seas.upenn.edu/~dineshj/publication/lambeta-2020-digit/lambeta-2020-digit.pdf",
        "comparison_caveat": "A hardware/manipulation platform paper, not a contact-localization benchmark.",
    },
]

peer_alignment_metrics = [
    {
        "alignment_score": peer_comparison[0]["alignment_score"],
        "rank": peer_comparison[0]["rank"],
        "systems_compared": len(peer_comparison),
    }
]


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


peer_comparison_columns = list(peer_comparison[0].keys())
peer_comparison_sql = (
    "SELECT * FROM (VALUES "
    + ", ".join(
        "(" + ", ".join(sql_literal(row[column]) for column in peer_comparison_columns) + ")"
        for row in peer_comparison
    )
    + ") AS peer_comparison("
    + ", ".join('"' + column + '"' for column in peer_comparison_columns)
    + ") ORDER BY rank, sensor"
)


# Current global model benchmark and application-gate datasets.
global_models = []
for row in global_model_raw:
    global_models.append(
        {
            "model": row["model"].replace("_", " ").title(),
            "model_key": row["model"],
            "mae_N": rounded(row["mae_N"], 4),
            "rmse_N": rounded(row["rmse_N"], 4),
            "r2": rounded(row["r2"], 5),
            "median_session_mae_N": rounded(row["median_session_mae_N"], 4),
            "worst_target_median_session_mae_N": rounded(row["worst_target_median_session_mae_N"], 4),
            "within_0_5_N_fraction": rounded(row["within_0_5_N_fraction"], 5),
            "sessions": integer(row["independent_sessions"]),
            "force_interval_N": row["force_interval_N"],
            "prediction_rows": integer(row["prediction_rows"]),
        }
    )
global_models.sort(key=lambda row: row["r2"], reverse=True)

global_range = [
    {
        "estimator": row["estimator"].replace("_", " ").title(),
        "session_force_bin_r2": rounded(row["session_force_bin_r2"], 5),
        "frame_mae_N": rounded(row["frame_mae_N"], 4),
        "frame_rmse_N": rounded(row["frame_rmse_N"], 4),
        "median_session_force_bin_mae_N": rounded(row["median_session_force_bin_mae_N"], 4),
        "within_0_5_N_fraction": rounded(row["session_force_bin_within_0_5_N_fraction"], 5),
        "frames": integer(row["frame_rows"]),
        "sessions": integer(row["independent_sessions"]),
    }
    for row in global_range_raw
]

application_gate = [
    {"fit": "Outer fold 1", "fit_order": 1, "selected_lag_ms": 260, "no_contact_fpr": 0.0496, "best_recall": 0.7656, "required_recall": 0.90},
    {"fit": "Outer fold 2", "fit_order": 2, "selected_lag_ms": 220, "no_contact_fpr": 0.0496, "best_recall": 0.8229, "required_recall": 0.90},
    {"fit": "Outer fold 3", "fit_order": 3, "selected_lag_ms": 240, "no_contact_fpr": 0.0491, "best_recall": 0.8462, "required_recall": 0.90},
    {"fit": "Outer fold 4", "fit_order": 4, "selected_lag_ms": 240, "no_contact_fpr": 0.0496, "best_recall": 0.8387, "required_recall": 0.90},
    {"fit": "Outer fold 5", "fit_order": 5, "selected_lag_ms": 240, "no_contact_fpr": 0.0499, "best_recall": 0.8571, "required_recall": 0.90},
    {"fit": "Outer fold 6", "fit_order": 6, "selected_lag_ms": 240, "no_contact_fpr": 0.0495, "best_recall": 0.8333, "required_recall": 0.90},
    {"fit": "Development refit", "fit_order": 7, "selected_lag_ms": 240, "no_contact_fpr": 0.0496, "best_recall": 0.8516, "required_recall": 0.90},
]


# Historical model summary. These rows deliberately retain the validation design so readers
# do not compare numbers from incompatible populations as if they were one leaderboard.
legacy_global_selected = next(
    row for row in legacy_global_raw if row.get("") == "global_nonlinear_basis_1frame_delayed"
)
current_best_global = max(global_models, key=lambda row: row["r2"])

historical_models = [
    {
        "system": "Automatic archive linear models",
        "validation": "5-fold grouped CV; complete sessions held out",
        "sessions": 162,
        "force_scope": "All automatic frames",
        "force_mae_N": rounded(legacy_auto["force_all_frames"]["mae_N"], 3),
        "force_rmse_N": rounded(legacy_auto["force_all_frames"]["rmse_N"], 3),
        "r2": rounded(legacy_auto["force_all_frames"]["r2"], 4),
        "localization_result": f"{legacy_auto['localization_session_accuracy']:.1%} session accuracy",
        "status": "Historical; not deployment-ready",
    },
    {
        "system": "Manual per-ROI linear models",
        "validation": "Blocked within-session CV; one recording per ROI",
        "sessions": 9,
        "force_scope": "End-to-end all frames",
        "force_mae_N": rounded(legacy_manual["force_all_frames_end_to_end"]["mae_N"], 3),
        "force_rmse_N": rounded(legacy_manual["force_all_frames_end_to_end"]["rmse_N"], 3),
        "r2": rounded(legacy_manual["force_all_frames_end_to_end"]["r2"], 4),
        "localization_result": f"{legacy_manual['localization_contact_frame_accuracy']:.1%} contact-frame accuracy",
        "status": "Historical; no repeat-session validation",
    },
    {
        "system": "Legacy global nonlinear delayed model",
        "validation": "Within-recording comparison on nine manual sessions",
        "sessions": 9,
        "force_scope": "Instantaneous frames",
        "force_mae_N": rounded(legacy_global_selected["instantaneous_mae_N"], 3),
        "force_rmse_N": rounded(legacy_global_selected["instantaneous_rmse_N"], 3),
        "r2": rounded(legacy_global_selected["instantaneous_r2"], 4),
        "localization_result": "Not applicable",
        "status": "Historical",
    },
    {
        "system": "Clean global nonlinear delayed model",
        "validation": "Leave-one-complete-TEST-number-out; TEST2-TEST6",
        "sessions": 45,
        "force_scope": "15,849 eligible frames",
        "force_mae_N": rounded(legacy_clean["candidate_results"]["global_nonlinear_basis_1frame_delayed"]["mae_N"], 3),
        "force_rmse_N": rounded(legacy_clean["candidate_results"]["global_nonlinear_basis_1frame_delayed"]["rmse_N"], 3),
        "r2": rounded(legacy_clean["candidate_results"]["global_nonlinear_basis_1frame_delayed"]["r2"], 4),
        "localization_result": "Not applicable",
        "status": "Historical; low-force false-contact gate failed",
    },
    {
        "system": "Current global session-zeroed total-light model",
        "validation": "Outer TEST-group holdout; session/force-bin medians",
        "sessions": current_best_global["sessions"],
        "force_scope": "1.7-3.0 N live implementation interval",
        "force_mae_N": current_best_global["mae_N"],
        "force_rmse_N": current_best_global["rmse_N"],
        "r2": current_best_global["r2"],
        "localization_result": "Global output; localization intentionally removed",
        "status": "Current benchmark; weak absolute utility",
    },
    {
        "system": "Experimental v4 hybrid",
        "validation": "Six reused outer TEST groups; post-hoc recovery",
        "sessions": hybrid_agg["independent_session_count"],
        "force_scope": "Conditional contact frames, 1.7-3.0 N",
        "force_mae_N": rounded(hybrid_agg["mean_conditional_force_mae_N"], 3),
        "force_rmse_N": rounded(hybrid["force_source_metrics"]["aggregate"]["mean_force_rmse_N"], 3),
        "r2": None,
        "localization_result": f"{hybrid_agg['event_localization_macro_f1']:.1%} event macro F1",
        "status": "Experimental — not validated",
    },
]


headline_metrics = [
    {
        "sessions": headline["sessions"],
        "manual_sessions": headline["manual_primary_sessions"],
        "automatic_sessions": headline["automatic_sessions"],
        "frames": headline["frames"],
        "established_qualified_metrics": headline["established_or_qualified_metrics"],
        "not_established_metrics": headline["not_established_metrics"],
        "event_macro_f1": rounded(hybrid_agg["event_localization_macro_f1"], 6),
        "selective_event_macro_f1": rounded(hybrid_agg["selective_localization_macro_f1"], 6),
        "selective_coverage": rounded(hybrid_agg["selective_localization_coverage"], 6),
        "conditional_force_mae_N": rounded(hybrid_agg["mean_conditional_force_mae_N"], 4),
        "p95_force_error_N": rounded(hybrid_agg["cross_validated_p95_absolute_error_N"], 4),
        "force_spearman": rounded(hybrid_agg["mean_force_spearman_rho"], 4),
        "best_live_range_r2": current_best_global["r2"],
    }
]


sources = [
    source("src_manual_report", "Authoritative manual-first characterization summary", manual_report_path),
    source("src_metric_status", "Authoritative characterization metric status", metric_status_path),
    source("src_sensitivity", "Manual sensitivity and nonlinearity table", sensitivity_path),
    source("src_plateau", "Manual plateau and supported-force observations", plateau_path),
    source("src_cross_talk", "Manual 9x9 cross-talk table", cross_talk_path),
    source("src_no_contact", "Dedicated no-contact noise and drift table", no_contact_path),
    source("src_detection", "Manual optical detection summary", detection_path),
    source("src_nef", "Noise-equivalent-force proxy table", nef_path),
    source("src_lag", "Manual system-lag summary", lag_path),
    source("src_hysteresis", "Automatic hysteresis table", hysteresis_path),
    source("src_cycle", "Automatic within-session repeatability table", cycle_path),
    source("src_global_models", "Global held-out model benchmark", global_model_path),
    source("src_global_range", "Exact live-sensor operating-range summary", global_range_path),
    source("src_global_validation", "Global characterization validation report", global_validation_path),
    source("src_global_quality", "Global characterization quality report", global_quality_path),
    source("src_hybrid", "Experimental v4 hybrid metrics", hybrid_path),
    source("src_event_benchmark", "Event-localization candidate benchmark", event_benchmark_path),
    source("src_force_benchmark", "Force-candidate benchmark", force_benchmark_path),
    source("src_application_gate", "Manual-only application gate failure report", application_gate_path),
    source("src_recovery_report", "Manual-only experimental recovery report", recovery_report_path),
    source("src_legacy_auto", "Historical automatic linear-model validation", legacy_auto_path),
    source("src_legacy_manual", "Historical manual per-ROI model validation", legacy_manual_path),
    source("src_legacy_clean", "Historical clean global model validation", legacy_clean_path),
    source("src_legacy_global", "Historical global model comparison", legacy_global_path),
    source("src_legacy_archive", "Historical automatic archive summary", legacy_archive_summary_path),
    source(
        "src_validation_gates",
        "Consolidated validation and scientific-gate evidence",
        global_validation_path,
        sql=(
            "WITH global_validation AS (SELECT * FROM read_json_auto('"
            + str(global_validation_path).replace("\\", "/")
            + "')), global_quality AS (SELECT * FROM read_json_auto('"
            + str(global_quality_path).replace("\\", "/")
            + "')), application_report AS (SELECT * FROM read_text('"
            + str(application_gate_path).replace("\\", "/")
            + "')), recovery_report AS (SELECT * FROM read_text('"
            + str(recovery_report_path).replace("\\", "/")
            + "')) SELECT * FROM global_validation CROSS JOIN global_quality "
            "CROSS JOIN application_report CROSS JOIN recovery_report"
        ),
        description="Combines the validated global checks with the frozen application-gate and experimental-recovery dispositions.",
        tables_used=[
            str(global_validation_path).replace("\\", "/"),
            str(global_quality_path).replace("\\", "/"),
            str(application_gate_path).replace("\\", "/"),
            str(recovery_report_path).replace("\\", "/"),
        ],
        metric_definitions=[
            "A gate is Passed only when its controlling machine-readable report records gate_pass=true.",
            "Failed and Not established dispositions are preserved from the controlling reports and are not inferred from presentation choices.",
        ],
    ),
    source(
        "src_roi_consolidated",
        "Consolidated per-ROI characterization and recovery metrics",
        sensitivity_path,
        sql=(
            "WITH sensitivity AS (SELECT * FROM read_csv_auto('"
            + str(sensitivity_path).replace("\\", "/")
            + "', header=true)), plateau AS (SELECT * FROM read_csv_auto('"
            + str(plateau_path).replace("\\", "/")
            + "', header=true)), noise AS (SELECT * FROM read_csv_auto('"
            + str(no_contact_path).replace("\\", "/")
            + "', header=true)), detection AS (SELECT * FROM read_csv_auto('"
            + str(detection_path).replace("\\", "/")
            + "', header=true)), nef AS (SELECT * FROM read_csv_auto('"
            + str(nef_path).replace("\\", "/")
            + "', header=true)), lag AS (SELECT * FROM read_csv_auto('"
            + str(lag_path).replace("\\", "/")
            + "', header=true)) SELECT * FROM sensitivity LEFT JOIN plateau USING (roi) "
            "LEFT JOIN noise USING (roi) LEFT JOIN detection USING (roi) LEFT JOIN nef USING (roi) LEFT JOIN lag USING (roi)"
        ),
        description="Joins the reviewed ROI-level characterization tables; experimental v4 ROI metrics are added by the reproducible report-building script.",
        tables_used=[
            str(sensitivity_path).replace("\\", "/"),
            str(plateau_path).replace("\\", "/"),
            str(cross_talk_path).replace("\\", "/"),
            str(no_contact_path).replace("\\", "/"),
            str(detection_path).replace("\\", "/"),
            str(nef_path).replace("\\", "/"),
            str(lag_path).replace("\\", "/"),
            str(hybrid_path).replace("\\", "/"),
        ],
        metric_definitions=[
            "Low-force sensitivity is the frozen session-balanced 0-1 N slope in light per N.",
            "Noise-equivalent force is signed no-contact noise MAD divided by positive low-force sensitivity; it is a proxy, not true resolution.",
            "Target share is the median target-channel share of total response; max off-target share is the largest other-channel median share for the same pressed ROI.",
        ],
    ),
    source(
        "src_auto_effects",
        "Consolidated automatic hysteresis and cycle-repeatability metrics",
        hysteresis_path,
        sql=(
            "WITH hysteresis AS (SELECT roi, speed_mm_min, median(abs(median_hysteresis_percent_observed_span))/100.0 "
            "AS median_abs_hysteresis_fraction FROM read_csv_auto('"
            + str(hysteresis_path).replace("\\", "/")
            + "', header=true) GROUP BY roi, speed_mm_min), cycles AS (SELECT roi, median(median_within_cycle_cv) AS median_cycle_cv "
            "FROM read_csv_auto('"
            + str(cycle_path).replace("\\", "/")
            + "', header=true) GROUP BY roi) SELECT * FROM hysteresis LEFT JOIN cycles USING (roi)"
        ),
        description="Aggregates automatic hysteresis by ROI and speed and joins the session-level cycle-CV summary.",
        tables_used=[
            str(hysteresis_path).replace("\\", "/"),
            str(cycle_path).replace("\\", "/"),
        ],
        metric_definitions=[
            "Median absolute hysteresis is the median absolute condition-level percent observed-span value, stored as a fractional rate for rendering.",
            "Cycle CV is summarized within sessions; cycles are never treated as independent replicates.",
        ],
    ),
    source(
        "src_historical_models",
        "Consolidated historical and current model inventory",
        legacy_auto_path,
        sql=(
            "WITH legacy_auto AS (SELECT * FROM read_json_auto('"
            + str(legacy_auto_path).replace("\\", "/")
            + "')), legacy_manual AS (SELECT * FROM read_json_auto('"
            + str(legacy_manual_path).replace("\\", "/")
            + "')), legacy_clean AS (SELECT * FROM read_json_auto('"
            + str(legacy_clean_path).replace("\\", "/")
            + "')), current_global AS (SELECT * FROM read_csv_auto('"
            + str(global_model_path).replace("\\", "/")
            + "', header=true)), hybrid AS (SELECT * FROM read_json_auto('"
            + str(hybrid_path).replace("\\", "/")
            + "')) SELECT * FROM legacy_auto CROSS JOIN legacy_manual CROSS JOIN legacy_clean CROSS JOIN current_global CROSS JOIN hybrid"
        ),
        description="Collects the underlying result objects used to build the non-comparable historical/current model inventory.",
        tables_used=[
            str(legacy_auto_path).replace("\\", "/"),
            str(legacy_manual_path).replace("\\", "/"),
            str(legacy_global_path).replace("\\", "/"),
            str(legacy_clean_path).replace("\\", "/"),
            str(global_model_path).replace("\\", "/"),
            str(hybrid_path).replace("\\", "/"),
        ],
        metric_definitions=[
            "Rows retain their original validation population, force interval, grain, and session structure and must not be treated as a single leaderboard.",
        ],
    ),
    web_source(
        "src_insight_paper",
        "Insight: dynamic vision-based tactile sensing",
        "https://www.nature.com/articles/s42256-021-00439-3",
        description="Primary paper used for Insight contact-position and force results.",
    ),
    web_source(
        "src_minsight_paper",
        "Minsight: fingertip-sized vision-based tactile sensing",
        "https://arxiv.org/abs/2304.10990",
        description="Primary paper used for Minsight contact-location and force results.",
    ),
    web_source(
        "src_eltac_paper",
        "ELTac: large-area electroluminescent tactile skin",
        "https://eprints.soton.ac.uk/499673/",
        description="Primary paper used for the closest electroluminescent hardware comparison; ELTac is not ML-based.",
    ),
    web_source(
        "src_omnitact_paper",
        "OmniTact: multi-directional high-resolution touch sensing",
        "https://arxiv.org/pdf/2003.06965",
        description="Primary paper used for OmniTact contact-angle and manipulation results.",
    ),
    web_source(
        "src_9dtact_paper",
        "9DTact: shape reconstruction and generalizable 6D force",
        "https://arxiv.org/pdf/2308.14277",
        description="Primary paper used for 9DTact unseen-object shape and force results.",
    ),
    web_source(
        "src_digit_paper",
        "DIGIT: compact high-resolution vision-based tactile sensing",
        "https://www.seas.upenn.edu/~dineshj/publication/lambeta-2020-digit/lambeta-2020-digit.pdf",
        description="Primary paper used for DIGIT hardware and ML application scope.",
    ),
    {
        "id": "src_peer_comparison",
        "label": "Representative primary-literature visuotactile comparison",
        "href": "https://www.nature.com/articles/s42256-021-00439-3",
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": peer_comparison_sql,
            "description": "Source-backed manual extraction from six representative primary papers plus the local experimental v4 metrics.",
            "tables_used": [
                str(hybrid_path).replace("\\", "/"),
                "https://www.nature.com/articles/s42256-021-00439-3",
                "https://arxiv.org/abs/2304.10990",
                "https://eprints.soton.ac.uk/499673/",
                "https://arxiv.org/pdf/2003.06965",
                "https://arxiv.org/pdf/2308.14277",
                "https://www.seas.upenn.edu/~dineshj/publication/lambeta-2020-digit/lambeta-2020-digit.pdf",
            ],
            "metric_definitions": [
                "Use-case evidence alignment awards one point for each disclosed item: camera-only runtime inference; explicit contact location or state output; a quantitative localization or state result; a meaningful held-out unit; and uncertainty-aware abstention with both coverage and performance reported.",
                "The score measures evidence alignment to camera-only contact localization. It is not an overall sensor-performance ranking.",
                "Not reported means the cited sensor paper did not disclose the item; it is not evidence that the broader platform lacks the capability.",
                "Cross-paper localization, force, angle, shape, and manipulation metrics are task-specific and are retained in their native units rather than normalized into a false common benchmark.",
            ],
        },
    },
]


cards = [
    metric_card(
        "card_sessions",
        "headline_metrics",
        "src_manual_report",
        "Complete sessions retained across characterization, calibration, baseline, and replay roles.",
        [
            {"label": "Complete sessions", "field": "sessions", "format": "number"},
            {"label": "Manual primary", "field": "manual_sessions", "format": "number"},
            {"label": "Automatic", "field": "automatic_sessions", "format": "number"},
        ],
    ),
    metric_card(
        "card_frames",
        "headline_metrics",
        "src_manual_report",
        "Original-video frames reconciled to the immutable feature-store evidence.",
        [{"label": "Reconciled frames", "field": "frames", "format": "compact"}],
    ),
    metric_card(
        "card_metric_status",
        "headline_metrics",
        "src_manual_report",
        "Characterization metrics currently established or qualified within explicit claim boundaries.",
        [
            {"label": "Established / qualified", "field": "established_qualified_metrics", "format": "number"},
            {"label": "Not established", "field": "not_established_metrics", "format": "number"},
        ],
    ),
    metric_card(
        "card_event_f1",
        "headline_metrics",
        "src_hybrid",
        "Post-hoc event-level localization across 102 events and 54 independent primary sessions.",
        [
            {"label": "Event macro F1", "field": "event_macro_f1", "format": "percent"},
            {"label": "Selective F1", "field": "selective_event_macro_f1", "format": "percent"},
            {"label": "Selective coverage", "field": "selective_coverage", "format": "percent"},
        ],
    ),
    metric_card(
        "card_force_mae",
        "headline_metrics",
        "src_hybrid",
        "Approximate conditional force error in the experimental 1.7-3.0 N interval.",
        [
            {"label": "Conditional force MAE (N)", "field": "conditional_force_mae_N", "format": "number"},
            {"label": "p95 abs. error (N)", "field": "p95_force_error_N", "format": "number"},
            {"label": "Spearman rho", "field": "force_spearman", "format": "number"},
        ],
    ),
    metric_card(
        "card_global_r2",
        "headline_metrics",
        "src_global_models",
        "Best held-out R-squared among current global session/force-bin models in the live interval.",
        [{"label": "Best live-range R²", "field": "best_live_range_r2", "format": "number"}],
    ),
    metric_card(
        "card_peer_alignment",
        "peer_alignment_metrics",
        "src_peer_comparison",
        "Evidence alignment to the declared camera-only contact-localization use case; this is not an overall sensor-quality rank.",
        [
            {"label": "Evidence points (of 5)", "field": "alignment_score", "format": "number"},
            {"label": "Use-case rank", "field": "rank", "format": "number"},
            {"label": "Systems compared", "field": "systems_compared", "format": "number"},
        ],
    ),
]


charts = [
    native_chart(
        "chart_metric_status",
        "Characterization metrics by evidence status",
        "Thirteen metrics are established or conditionally qualified; three remain explicitly not established.",
        "horizontalBar",
        "evidence_status",
        "src_metric_status",
        {"field": "status", "type": "nominal", "label": "Evidence status"},
        {"field": "metric_count", "type": "quantitative", "label": "Metric count"},
        palette={"kind": "single-root", "name": "blue"},
        settings={"sort": "descending", "showValues": True, "orientation": "horizontal"},
    ),
    native_chart(
        "chart_sensitivity",
        "Low-force sensitivity by ROI",
        "Session-balanced 0-1 N slopes; zero and negative estimates show the response is not uniformly increasing.",
        "bar",
        "roi_characterization",
        "src_sensitivity",
        {"field": "roi_label", "type": "ordinal", "label": "ROI"},
        {"field": "low_force_sensitivity_light_per_N", "type": "quantitative", "label": "Light per N", "unit": "light/N"},
        palette={"kind": "diverging", "name": "blue-orange", "midpoint": 0},
        reference_lines=[{"axis": "y", "value": 0, "label": "Zero sensitivity", "color": "neutral", "lineStyle": "dashed"}],
        settings={"sort": "none", "showValues": True},
        unit="light/N",
    ),
    native_chart(
        "chart_cross_talk",
        "Target-channel and largest off-target response share",
        "Single-ROI presses do not consistently concentrate the optical response in the intended channel.",
        "bar",
        "cross_talk_summary",
        "src_cross_talk",
        {"field": "roi_label", "type": "ordinal", "label": "Pressed ROI"},
        {"field": "share", "type": "quantitative", "label": "Median share", "format": "percent"},
        color={"field": "component", "type": "nominal", "label": "Response component"},
        tooltip=[{"field": "roi", "type": "quantitative", "label": "ROI"}],
        palette={"kind": "categorical", "name": "blue-orange"},
        settings={"groupMode": "grouped", "showValues": False},
        value_format="percent",
    ),
    native_chart(
        "chart_noise_drift",
        "No-contact noise and drift by ROI",
        "Nine labeled ROI summarize 11 dedicated no-contact sessions each; higher drift is not explained by noise alone.",
        "scatter",
        "noise_drift",
        "src_no_contact",
        {"field": "noise_mad_light", "type": "quantitative", "label": "Signed noise MAD", "unit": "light"},
        {"field": "drift_light_per_min", "type": "quantitative", "label": "Median absolute drift", "unit": "light/min"},
        label={"field": "roi_label", "type": "text", "label": "ROI"},
        tooltip=[
            {"field": "roi_label", "type": "text", "label": "ROI"},
            {"field": "sessions", "type": "quantitative", "label": "Sessions"},
        ],
        palette={"kind": "single-root", "name": "blue"},
        unit="light/min",
    ),
    native_chart(
        "chart_lag",
        "Median system-level lag by ROI",
        "Positive values mean optical response follows the reference; these are system estimates, not intrinsic material response times.",
        "bar",
        "lag_summary",
        "src_lag",
        {"field": "roi_label", "type": "ordinal", "label": "ROI"},
        {"field": "median_lag_ms", "type": "quantitative", "label": "Median lag", "unit": "ms"},
        tooltip=[
            {"field": "minimum_lag_ms", "type": "quantitative", "label": "Minimum lag", "unit": "ms"},
            {"field": "maximum_lag_ms", "type": "quantitative", "label": "Maximum lag", "unit": "ms"},
            {"field": "median_abs_correlation", "type": "quantitative", "label": "Median |correlation|"},
        ],
        palette={"kind": "diverging", "name": "blue-orange", "midpoint": 0},
        reference_lines=[{"axis": "y", "value": 0, "label": "No lag", "color": "neutral", "lineStyle": "dashed"}],
        settings={"sort": "none", "showValues": True},
        unit="ms",
    ),
    native_chart(
        "chart_hysteresis",
        "Median absolute hysteresis by ROI and speed",
        "Automatic data are condition-specific supporting evidence; speed strata remain separate and are not pooled as independent replication.",
        "bar",
        "hysteresis_summary",
        "src_hysteresis",
        {"field": "roi_label", "type": "ordinal", "label": "ROI"},
        {"field": "median_abs_hysteresis_fraction", "type": "quantitative", "label": "Median absolute hysteresis", "format": "percent"},
        color={"field": "speed_label", "type": "nominal", "label": "Speed"},
        tooltip=[{"field": "force_bin_observations", "type": "quantitative", "label": "Force-bin observations"}],
        palette={"kind": "categorical", "name": "blue-orange-gold"},
        settings={"groupMode": "grouped", "showValues": False},
        value_format="percent",
    ),
    native_chart(
        "chart_cycle_repeatability",
        "Within-session cycle variability by ROI",
        "Median cycle coefficient of variation across complete automatic sessions; cycles are repeated observations, not independent replicates.",
        "bar",
        "cycle_summary",
        "src_cycle",
        {"field": "roi_label", "type": "ordinal", "label": "ROI"},
        {"field": "median_within_cycle_cv", "type": "quantitative", "label": "Median cycle CV", "format": "percent"},
        tooltip=[{"field": "independent_sessions", "type": "quantitative", "label": "Independent sessions"}],
        palette={"kind": "single-root", "name": "blue"},
        settings={"sort": "none", "showValues": True},
        value_format="percent",
    ),
    native_chart(
        "chart_localization_models",
        "Event-localization candidate comparison",
        "Accumulated argmax leads the tested classifiers on macro F1 across six reused outer TEST groups.",
        "horizontalBar",
        "localization_model_rows",
        "src_event_benchmark",
        {"field": "model", "type": "nominal", "label": "Candidate"},
        {"field": "macro_f1", "type": "quantitative", "label": "Macro F1", "format": "percent"},
        tooltip=[
            {"field": "accuracy", "type": "quantitative", "label": "Accuracy", "format": "percent"},
            {"field": "minimum_fold_macro_f1", "type": "quantitative", "label": "Minimum fold macro F1", "format": "percent"},
            {"field": "events", "type": "quantitative", "label": "Events"},
        ],
        palette={"kind": "single-root", "name": "blue"},
        settings={"sort": "descending", "showValues": True, "orientation": "horizontal"},
        value_format="percent",
    ),
    native_chart(
        "chart_v4_roi_recall",
        "Experimental v4 event-localization recall by ROI",
        "All nine ROI exceed 90% event-level recall in the post-hoc grouped recovery evaluation.",
        "bar",
        "v4_roi_metrics",
        "src_hybrid",
        {"field": "roi_label", "type": "ordinal", "label": "ROI"},
        {"field": "event_localization_recall", "type": "quantitative", "label": "Event recall", "format": "percent"},
        tooltip=[{"field": "conditional_force_mae_N", "type": "quantitative", "label": "Conditional force MAE", "unit": "N"}],
        palette={"kind": "single-root", "name": "blue"},
        reference_lines=[{"axis": "y", "value": 0.90, "label": "90%", "color": "neutral", "lineStyle": "dashed"}],
        settings={"sort": "none", "showValues": True},
        value_format="percent",
    ),
    native_chart(
        "chart_localization_operating_modes",
        "Localization performance and retained-event coverage",
        "Forced output reaches 93.43% macro F1 on all events; confidence filtering reaches 100% on 82.93%.",
        "bar",
        "localization_operating_modes",
        "src_hybrid",
        {"field": "mode", "type": "ordinal", "label": "Operating mode"},
        {"field": "value", "type": "quantitative", "label": "Rate", "format": "percent"},
        color={"field": "metric", "type": "nominal", "label": "Metric"},
        tooltip=[
            {"field": "metric", "type": "text", "label": "Metric"},
            {"field": "value", "type": "quantitative", "label": "Result", "format": "percent"},
        ],
        palette={"kind": "categorical", "name": "blue-orange"},
        settings={"groupMode": "grouped", "sort": "none", "showValues": True},
        value_format="percent",
    ),
    native_chart(
        "chart_peer_alignment",
        "Evidence alignment for camera-only contact localization",
        "Five disclosed criteria; this is a use-case fit score, not an overall sensor-quality ranking.",
        "horizontalBar",
        "peer_comparison",
        "src_peer_comparison",
        {"field": "sensor", "type": "nominal", "label": "Sensor"},
        {"field": "alignment_score", "type": "quantitative", "label": "Evidence points (of 5)"},
        color={"field": "focus_group", "type": "nominal", "label": "System group"},
        tooltip=[
            {"field": "rank", "type": "quantitative", "label": "Use-case rank"},
            {"field": "year", "type": "quantitative", "label": "Year"},
            {"field": "citation", "type": "text", "label": "Citation"},
        ],
        palette={"kind": "categorical", "name": "blue-gold"},
        reference_lines=[{"axis": "y", "value": 5, "label": "All five criteria", "color": "neutral", "lineStyle": "dashed"}],
        settings={"sort": "descending", "showValues": True, "orientation": "horizontal"},
    ),
    native_chart(
        "chart_force_folds",
        "Selected force candidate versus fold constant",
        "The selected search improves MAE only marginally and inconsistently across folds, supporting the failed resolution gate.",
        "bar",
        "force_fold_rows",
        "src_force_benchmark",
        {"field": "fold", "type": "ordinal", "label": "Outer fold"},
        {"field": "mae_N", "type": "quantitative", "label": "Conditional MAE", "unit": "N"},
        color={"field": "series", "type": "nominal", "label": "Benchmark"},
        tooltip=[{"field": "fold_number", "type": "quantitative", "label": "Fold"}],
        palette={"kind": "categorical", "name": "blue-orange"},
        settings={"groupMode": "grouped", "showValues": True},
        unit="N",
    ),
    native_chart(
        "chart_global_r2",
        "Held-out global-model R² in the 1.7-3.0 N interval",
        "Only the session-zeroed total-light linear model is positive, and its R² is just 0.014.",
        "horizontalBar",
        "global_models",
        "src_global_models",
        {"field": "model", "type": "nominal", "label": "Model"},
        {"field": "r2", "type": "quantitative", "label": "Held-out R²"},
        tooltip=[
            {"field": "mae_N", "type": "quantitative", "label": "MAE", "unit": "N"},
            {"field": "median_session_mae_N", "type": "quantitative", "label": "Median session MAE", "unit": "N"},
            {"field": "within_0_5_N_fraction", "type": "quantitative", "label": "Within 0.5 N", "format": "percent"},
        ],
        palette={"kind": "diverging", "name": "blue-orange", "midpoint": 0},
        reference_lines=[{"axis": "y", "value": 0, "label": "Constant-baseline level", "color": "neutral", "lineStyle": "dashed"}],
        settings={"sort": "descending", "showValues": True, "orientation": "horizontal"},
    ),
    native_chart(
        "chart_application_gate",
        "Best supported-bin contact recall by application fit",
        "Every outer fit and the development refit remain below the frozen 90% recall requirement.",
        "bar",
        "application_gate",
        "src_application_gate",
        {"field": "fit", "type": "ordinal", "label": "Fit"},
        {"field": "best_recall", "type": "quantitative", "label": "Best supported-bin recall", "format": "percent"},
        tooltip=[
            {"field": "no_contact_fpr", "type": "quantitative", "label": "Training no-contact FPR", "format": "percent"},
            {"field": "selected_lag_ms", "type": "quantitative", "label": "Selected lag", "unit": "ms"},
        ],
        palette={"kind": "single-root", "name": "orange"},
        reference_lines=[{"axis": "y", "value": 0.90, "label": "Required recall", "color": "neutral", "lineStyle": "dashed"}],
        settings={"sort": "none", "showValues": True},
        value_format="percent",
    ),
]


tables = [
    native_table(
        "table_coverage",
        "Evidence coverage",
        "Session roles are overlapping views of the full study scope only where explicitly stated.",
        "data_coverage",
        "src_manual_report",
        [
            {"field": "evidence_group", "label": "Evidence group", "type": "text"},
            {"field": "sessions", "label": "Sessions", "format": "number"},
            {"field": "role", "label": "Role", "type": "text"},
        ],
        "sessions",
        "desc",
    ),
    native_table(
        "table_validation_gates",
        "Validation and scientific gates",
        "Current disposition of the principal data, characterization, application, and physical-validation gates.",
        "validation_gates",
        "src_validation_gates",
        [
            {"field": "gate", "label": "Gate", "type": "text"},
            {"field": "status", "label": "Status", "type": "text"},
            {"field": "evidence", "label": "Evidence", "type": "text"},
            {"field": "implication", "label": "Implication", "type": "text"},
        ],
        "gate",
        "asc",
        "dense",
    ),
    native_table(
        "table_metric_status",
        "Complete characterization metric inventory",
        "All 18 tracked metrics with evidence level, independent evidence, and claim boundary.",
        "metric_status",
        "src_metric_status",
        [
            {"field": "metric", "label": "Metric", "type": "text"},
            {"field": "status", "label": "Status", "type": "text"},
            {"field": "independent_evidence", "label": "Independent evidence", "type": "text"},
            {"field": "claim_boundary", "label": "Claim boundary", "type": "text"},
            {"field": "source_scope", "label": "Source scope", "type": "text"},
        ],
        "status",
        "asc",
        "dense",
    ),
    native_table(
        "table_roi_characterization",
        "Per-ROI characterization and experimental performance",
        "Exact ROI-level sensitivity, support, noise, drift, detection, lag, cross-talk, and v4 recovery metrics.",
        "roi_characterization",
        "src_roi_consolidated",
        [
            {"field": "roi_label", "label": "ROI", "type": "text"},
            {"field": "low_force_sensitivity_light_per_N", "label": "Sensitivity (light/N)", "format": "number"},
            {"field": "nonlinearity_fraction", "label": "Nonlinearity", "format": "percent"},
            {"field": "supported_force_max_N", "label": "Observed support max (N)", "format": "number"},
            {"field": "noise_mad_light", "label": "Noise MAD (light)", "format": "number"},
            {"field": "median_abs_drift_light_per_min", "label": "Median |drift| (light/min)", "format": "number"},
            {"field": "optical_detection_region_N", "label": "Detection region (N)", "format": "number"},
            {"field": "noise_equivalent_force_proxy_N", "label": "NEF proxy (N)", "format": "number"},
            {"field": "median_system_lag_ms", "label": "Median lag (ms)", "format": "number"},
            {"field": "target_channel_share", "label": "Target share", "format": "percent"},
            {"field": "max_off_target_share", "label": "Max off-target share", "format": "percent"},
            {"field": "v4_conditional_force_mae_N", "label": "v4 force MAE (N)", "format": "number"},
            {"field": "v4_event_localization_recall", "label": "v4 event recall", "format": "percent"},
        ],
        "roi_label",
        "asc",
        "dense",
    ),
    native_table(
        "table_auto_effects",
        "Automatic-cycle effects",
        "Median absolute hysteresis is stratified by ROI and speed; cycle CV remains at the independent-session summary level.",
        "auto_effects_table",
        "src_auto_effects",
        [
            {"field": "roi_label", "label": "ROI", "type": "text"},
            {"field": "speed_label", "label": "Speed", "type": "text"},
            {"field": "median_abs_hysteresis_fraction", "label": "Median |hysteresis|", "format": "percent"},
            {"field": "force_bin_observations", "label": "Force-bin rows", "format": "number"},
            {"field": "median_within_cycle_cv", "label": "Median cycle CV", "format": "percent"},
            {"field": "cycle_sessions", "label": "Cycle sessions", "format": "number"},
        ],
        "roi_label",
        "asc",
        "dense",
    ),
    native_table(
        "table_hybrid_metrics",
        "Experimental v4 hybrid metrics",
        "Current recovery metrics and their validation disposition.",
        "hybrid_metrics_table",
        "src_hybrid",
        [
            {"field": "metric", "label": "Metric", "type": "text"},
            {"field": "value", "label": "Result", "type": "text"},
            {"field": "status", "label": "Status", "type": "text"},
        ],
        "metric",
        "asc",
    ),
    native_table(
        "table_peer_alignment",
        "Representative visuotactile sensor metric comparison",
        "Native reported metrics are preserved because event F1, spatial error, angular error, shape error, and force error are not one common benchmark.",
        "peer_comparison",
        "src_peer_comparison",
        [
            {"field": "rank", "label": "Use-case rank", "format": "number"},
            {"field": "sensor", "label": "Sensor", "type": "text"},
            {"field": "year", "label": "Year", "format": "number"},
            {"field": "alignment_score", "label": "Evidence points (of 5)", "format": "number"},
            {"field": "reported_ml_output", "label": "Reported output", "type": "text"},
            {"field": "localization_result", "label": "Localization / state metric", "type": "text"},
            {"field": "force_result", "label": "Force metric", "type": "text"},
            {"field": "evaluation_scope", "label": "Evaluation scope / sample", "type": "text"},
            {"field": "citation", "label": "Primary citation", "type": "text"},
        ],
        "rank",
        "asc",
        "dense",
    ),
    native_table(
        "table_peer_metrics",
        "Use-case evidence scoring audit",
        "The five disclosed criteria explain the rank; not reported describes the cited paper and is not evidence that a platform lacks the capability.",
        "peer_comparison",
        "src_peer_comparison",
        [
            {"field": "rank", "label": "Use-case rank", "format": "number"},
            {"field": "sensor", "label": "Sensor", "type": "text"},
            {"field": "alignment_score", "label": "Evidence points (of 5)", "format": "number"},
            {"field": "camera_only_inference", "label": "Camera-only inference", "type": "text"},
            {"field": "explicit_localization", "label": "Explicit location/state", "type": "text"},
            {"field": "quantitative_localization", "label": "Quantitative result", "type": "text"},
            {"field": "held_out_unit", "label": "Held-out unit", "type": "text"},
            {"field": "abstention_reported", "label": "Abstention + coverage", "type": "text"},
            {"field": "comparison_caveat", "label": "Comparison boundary", "type": "text"},
        ],
        "rank",
        "asc",
        "dense",
    ),
    native_table(
        "table_force_folds",
        "Force-candidate selection by outer fold",
        "Nested candidate selection results; aggregate recovery evidence is post-hoc, not untouched confirmation.",
        "force_fold_table",
        "src_force_benchmark",
        [
            {"field": "fold", "label": "Fold", "type": "text"},
            {"field": "selected_family", "label": "Selected family", "type": "text"},
            {"field": "selected_candidate", "label": "Candidate", "type": "text"},
            {"field": "mae_N", "label": "Selected MAE (N)", "format": "number"},
            {"field": "constant_mae_N", "label": "Constant MAE (N)", "format": "number"},
            {"field": "relative_gain", "label": "Relative gain", "format": "percent"},
            {"field": "spearman_rho", "label": "Spearman rho", "format": "number"},
            {"field": "lag_ms", "label": "Lag (ms)", "format": "number"},
        ],
        "fold",
        "asc",
        "dense",
    ),
    native_table(
        "table_global_models",
        "Current global held-out model benchmark",
        "All models use 54 sessions and 263 session/force-bin prediction rows in the 1.7-3.0 N interval.",
        "global_models",
        "src_global_models",
        [
            {"field": "model", "label": "Model", "type": "text"},
            {"field": "mae_N", "label": "MAE (N)", "format": "number"},
            {"field": "rmse_N", "label": "RMSE (N)", "format": "number"},
            {"field": "r2", "label": "R²", "format": "number"},
            {"field": "median_session_mae_N", "label": "Median session MAE (N)", "format": "number"},
            {"field": "worst_target_median_session_mae_N", "label": "Worst-target median MAE (N)", "format": "number"},
            {"field": "within_0_5_N_fraction", "label": "Within 0.5 N", "format": "percent"},
        ],
        "r2",
        "desc",
        "dense",
    ),
    native_table(
        "table_global_range",
        "Exact live-sensor operating-range summaries",
        "Frame and session/force-bin summaries for the exact experimental live-sensor predictions.",
        "global_range",
        "src_global_range",
        [
            {"field": "estimator", "label": "Estimator", "type": "text"},
            {"field": "frame_mae_N", "label": "Frame MAE (N)", "format": "number"},
            {"field": "frame_rmse_N", "label": "Frame RMSE (N)", "format": "number"},
            {"field": "session_force_bin_r2", "label": "Session-bin R²", "format": "number"},
            {"field": "median_session_force_bin_mae_N", "label": "Median session-bin MAE (N)", "format": "number"},
            {"field": "within_0_5_N_fraction", "label": "Within 0.5 N", "format": "percent"},
            {"field": "frames", "label": "Frames", "format": "compact"},
        ],
        "session_force_bin_r2",
        "desc",
        "dense",
    ),
    native_table(
        "table_historical_models",
        "Historical and current model results",
        "Validation designs and scopes differ; rows are an inventory, not a single comparable leaderboard.",
        "historical_models",
        "src_historical_models",
        [
            {"field": "system", "label": "System", "type": "text"},
            {"field": "validation", "label": "Validation design", "type": "text"},
            {"field": "sessions", "label": "Sessions", "format": "number"},
            {"field": "force_scope", "label": "Force scope", "type": "text"},
            {"field": "force_mae_N", "label": "Force MAE (N)", "format": "number"},
            {"field": "force_rmse_N", "label": "Force RMSE (N)", "format": "number"},
            {"field": "r2", "label": "R²", "format": "number"},
            {"field": "localization_result", "label": "Localization result", "type": "text"},
            {"field": "status", "label": "Disposition", "type": "text"},
        ],
        "system",
        "asc",
        "dense",
    ),
    native_table(
        "table_application_gate",
        "Frozen application-preprocessing gate results",
        "Every fit satisfied the reported no-contact FPR target but missed the required 90% supported-bin recall.",
        "application_gate",
        "src_application_gate",
        [
            {"field": "fit", "label": "Fit", "type": "text"},
            {"field": "selected_lag_ms", "label": "Lag (ms)", "format": "number"},
            {"field": "no_contact_fpr", "label": "No-contact FPR", "format": "percent"},
            {"field": "best_recall", "label": "Best supported-bin recall", "format": "percent"},
            {"field": "required_recall", "label": "Required recall", "format": "percent"},
        ],
        "fit",
        "asc",
    ),
]


legacy_blocks = [
    markdown("report_title", "# Software Calibration Metrics Report"),
    markdown(
        "executive_summary",
        "## Executive Summary\n\n"
        "- **The evidence base is large and internally reconciled.** The current characterization retains **245 complete sessions** and **179,176 source-aligned frames**, with the authoritative quality and validation checks passing.\n"
        "- **Event-level localization is the strongest application result.** Experimental v4 reaches **93.43% macro F1** across 102 events; confidence filtering reaches **100% macro F1 at 82.93% coverage**. These are post-hoc grouped recovery results, not untouched confirmation.\n"
        "- **Force estimation remains approximate.** Experimental v4 conditional MAE is **0.328 N** with **0.643 N p95 absolute error**, but the resolution gate fails because gain over a constant is small and rank/response-spread metrics are weak.\n"
        "- **Deployment is not authorized.** No authoritative common all-ROI safe force range was established; true force resolution and true creep remain not established. The correct disposition is **experimental — not validated**.",
    ),
    {"id": "headline_strip", "type": "metric-strip", "cardIds": [card["id"] for card in cards], "layout": "full"},
    markdown(
        "coverage_section",
        "## The dataset is complete, but evidence strength varies by metric\n\n"
        "**Archive and frame reconciliation passed, so the principal limitation is scientific support rather than missing data.** The 18-metric characterization inventory separates primary results, conditionally qualified findings, exploratory observations, and explicitly unestablished claims. This distinction should remain visible in every presentation of the results.",
        "src_manual_report",
    ),
    chart_block("metric_status_chart_block", "chart_metric_status"),
    table_block("coverage_table_block", "table_coverage"),
    table_block("validation_gate_table_block", "table_validation_gates"),
    markdown(
        "inventory_note",
        "**The complete metric inventory below is the controlling checklist.** It prevents a polished summary from silently dropping failed or unestablished outcomes, especially the common force range, true creep, and true force resolution.",
        "src_metric_status",
    ),
    table_block("metric_status_table_block", "table_metric_status"),
    markdown(
        "sensitivity_section",
        "## Sensor response varies sharply across the nine ROI\n\n"
        "**Low-force sensitivity is neither uniform nor consistently positive.** In the session-balanced 0-1 N analysis, ROI 4 has the largest positive slope, ROI 3 is negative, ROI 1 is small positive, and the remaining ROI are estimated at zero under the frozen calculation. This undermines any single monotonic device-wide calibration claim.",
        "src_sensitivity",
    ),
    chart_block("sensitivity_chart_block", "chart_sensitivity"),
    markdown(
        "cross_talk_note",
        "**Spatial separation is also imperfect.** The intended ROI channel is not always the dominant share during single-ROI presses; the largest off-target share can match or exceed the target-channel share. The implication is that localization should rely on the validated event-level evidence contract, not a naive per-frame maximum alone.",
        "src_cross_talk",
    ),
    chart_block("cross_talk_chart_block", "chart_cross_talk"),
    table_block("roi_characterization_table_block", "table_roi_characterization"),
    markdown(
        "noise_lag_section",
        "## Noise, drift and timing limit interpretation\n\n"
        "**Dedicated no-contact recordings show material ROI-to-ROI drift, while noise varies over a narrower range.** The noise-equivalent-force values are therefore stability proxies only; they are not force-resolution measurements.",
        "src_no_contact",
    ),
    chart_block("noise_drift_chart_block", "chart_noise_drift"),
    markdown(
        "lag_note",
        "**Lag estimates are system-level and exploratory.** Median ROI lag spans negative to positive values, and the reported bounds are broad. These numbers include camera, acquisition, synchronization, and material effects and should not be presented as intrinsic sensor response time.",
        "src_lag",
    ),
    chart_block("lag_chart_block", "chart_lag"),
    markdown(
        "automatic_section",
        "## Automatic-cycle results are useful supporting evidence, not independent replication\n\n"
        "**Hysteresis depends on ROI, speed, displacement and force bin.** The chart keeps the three speed strata separate and aggregates only the absolute condition-specific hysteresis values needed for a readable comparison. Cycles are summarized within each session and never counted as independent replicates.",
        "src_hysteresis",
    ),
    chart_block("hysteresis_chart_block", "chart_hysteresis"),
    markdown(
        "cycle_note",
        "**Within-session cycle variability remains non-trivial across ROI.** These CV values describe short-run repeatability under the automatic protocol; they do not establish between-session repeatability, true resolution, or long-term stability.",
        "src_cycle",
    ),
    chart_block("cycle_chart_block", "chart_cycle_repeatability"),
    table_block("auto_effects_table_block", "table_auto_effects"),
    markdown(
        "localization_section",
        "## Event localization is the strongest recovered capability\n\n"
        "**The simple accumulated-argmax event rule outperforms the tested learned classifiers.** It reaches 93.43% macro F1 overall, while the per-ROI recall stays above 90% in the v4 hybrid. Confidence filtering can achieve perfect macro F1 on the retained 82.93% of events, so the system should continue to withhold uncertain ROI outputs.",
        "src_event_benchmark",
    ),
    chart_block("localization_models_chart_block", "chart_localization_models"),
    markdown(
        "roi_recall_note",
        "**Performance is broad across ROI but still post-hoc.** The nine ROI recall values come from six already-inspected outer TEST groups; they support continued experimental replay/live work but not a validated product claim.",
        "src_hybrid",
    ),
    chart_block("v4_roi_recall_chart_block", "chart_v4_roi_recall"),
    table_block("hybrid_metrics_table_block", "table_hybrid_metrics"),
    markdown(
        "force_section",
        "## Force models do not beat simple baselines by enough to establish resolution\n\n"
        "**The expanded candidate search improves mean MAE over a fold-fitted constant by only 4.49%.** Four folds selected Extra Trees, one isotonic regression and one ridge model, showing selection instability. Mean within-session Spearman correlation is only 0.136 in that search, so the more auditable isotonic mapping was retained for v4 despite its approximate output.",
        "src_force_benchmark",
    ),
    chart_block("force_fold_chart_block", "chart_force_folds"),
    table_block("force_fold_table_block", "table_force_folds"),
    markdown(
        "global_force_note",
        "**The current global benchmarks confirm weak absolute predictive utility.** In the 1.7-3.0 N live interval, the best held-out R² is 0.0136 and the constant baseline has the best median session MAE. Narrower-range MAE is lower than the 0-5 N comparator, but the reduced outcome variance does not improve explained variance.",
        "src_global_models",
    ),
    chart_block("global_r2_chart_block", "chart_global_r2"),
    table_block("global_models_table_block", "table_global_models"),
    table_block("global_range_table_block", "table_global_range"),
    markdown(
        "historical_note",
        "**Earlier calibration models are retained for traceability, not as one leaderboard.** Their data sources, force ranges, validation grains and session structures differ materially. The table preserves those differences so apparent metric improvements are not mistaken for like-for-like progress.",
    ),
    table_block("historical_models_table_block", "table_historical_models"),
    markdown(
        "application_gate_section",
        "## Frozen gates still block a validated application claim\n\n"
        "**Every frozen application-preprocessing fit misses the required 90% supported-bin contact recall.** The best development result is 85.16%, so the authoritative application path correctly left `Fdetect` and the common `Fmax` undefined. The later v4 recovery work is explicitly experimental and does not retroactively pass this gate.",
        "src_application_gate",
    ),
    chart_block("application_gate_chart_block", "chart_application_gate"),
    table_block("application_gate_table_block", "table_application_gate"),
    markdown(
        "next_steps",
        "## Recommended Next Steps\n\n"
        "1. **Keep the v4 interface labeled experimental — not validated.** Preserve fail-closed force and ROI outputs outside optical support or below localization confidence.\n"
        "2. **Collect prospectively governed manual recordings.** Add at least three independent sessions in every required ROI/force bin and improve contact/no-contact separation before rerunning the frozen gate.\n"
        "3. **Run a true resolution protocol.** Use randomized, settled small-force steps with independent repetitions; continuous ramps and MAE alone cannot establish resolution.\n"
        "4. **Run qualified physical validation.** Add known-force confirmation, latency/soak testing, and representative-user testing before any validated deployment claim.\n"
        "5. **Use event-level localization as the main development path.** It is currently the only capability with consistently strong held-out recovery metrics, while force should remain approximate.",
    ),
    markdown(
        "further_questions",
        "## Further Questions\n\n"
        "- Can better illumination, ROI geometry, or optical normalization reduce the observed zero/negative sensitivity and off-target response?\n"
        "- Will prospective data reproduce the 93.43% event-localization macro F1 without post-hoc fold reuse?\n"
        "- Does a randomized settled-step protocol show useful resolution inside any narrower, prospectively chosen force interval?\n"
        "- Which component dominates the broad lag estimates: camera exposure, acquisition timing, synchronization, or material response?",
    ),
    markdown(
        "caveats",
        "## Caveats and Assumptions\n\n"
        "- The v3/v4 application results and model-selection benchmarks reuse six already-inspected outer TEST groups. They are recovery evidence, not untouched confirmation.\n"
        "- The 1.7-3.0 N interval comes from a post-hoc experimental implementation bundle and is not an authoritative common safe operating range.\n"
        "- Global-output metrics sacrifice localization and cannot replace per-ROI heterogeneity or cross-talk evidence.\n"
        "- Automatic cycles are repeated observations within sessions, not independent replicates.\n"
        "- Fixed-displacement settling/recovery is not true constant-force creep. Noise-equivalent force is a stability proxy, not true force resolution.\n"
        "- Historical model metrics use different populations, ranges, grains and validation designs; they should not be compared as a single progression curve.\n"
        "- No synthetic rows or labels were added to the source analyses summarized here.",
    ),
]


# Answer-first presentation order. The complete prior block inventory is retained,
# while event localization and the criteria-specific literature comparison lead.
blocks = [
    markdown("report_title", "# Camera-Only Visuotactile Contact Localization: Performance and Comparative Positioning"),
    markdown(
        "executive_summary",
        "## Technical Summary\n\n"
        "- **The clearest result is camera-only event localization.** The nine-region system reaches **93.43% macro F1 across all 102 events**, with every ROI at **90.28% recall or higher** across 54 independent primary sessions.\n"
        "- **Confidence-aware operation creates a strong accuracy/coverage story.** With uncertain events withheld, macro F1 reaches **100% on 82.93% of events**; forced output retains full coverage at 93.43% macro F1.\n"
        "- **Our system ranks first on a declared five-point use-case evidence score.** It is the only representative system in this comparison that reports all five items: camera-only inference, explicit localization, a quantitative localization result, a held-out unit, and abstention with both performance and coverage. This is not an overall sensor-quality ranking.\n"
        "- **Peers still lead on other dimensions.** Insight and Minsight report sub-millimetre contact localization and substantially stronger force results. Our force output remains approximate, with **0.328 N conditional MAE**, and true force resolution is not established.\n"
        "- **The correct disposition remains experimental — not validated.** The localization results are post-hoc recovery evidence from reused outer groups and need prospective confirmation.",
    ),
    {
        "id": "headline_strip",
        "type": "metric-strip",
        "cardIds": [
            "card_event_f1",
            "card_sessions",
            "card_frames",
            "card_metric_status",
            "card_force_mae",
            "card_global_r2",
        ],
        "layout": "full",
    },
    markdown(
        "localization_section",
        "## Camera-only event localization is the primary contribution\n\n"
        "**The simple accumulated-argmax event rule outperforms the tested learned classifiers.** It reaches 93.43% macro F1 overall, while per-ROI recall stays above 90% in the v4 hybrid. The output is a direct nine-region contact decision from optical data rather than a global force estimate.",
        "src_event_benchmark",
    ),
    chart_block("localization_models_chart_block", "chart_localization_models"),
    markdown(
        "roi_recall_note",
        "**Performance is broad across all nine ROI, but the evidence is still post-hoc.** Recall ranges from 90.28% to 100% across six already-inspected outer TEST groups. This supports continued experimental replay and live work, not a validated product claim.",
        "src_hybrid",
    ),
    chart_block("v4_roi_recall_chart_block", "chart_v4_roi_recall"),
    markdown(
        "localization_modes_note",
        "**The strongest operating angle is the explicit accuracy/coverage trade-off.** Forced output produces a location for every event at 93.43% macro F1. A confidence threshold can instead withhold ambiguous events and deliver 100% macro F1 on the retained 82.93%. Both numbers belong together; reporting only the filtered accuracy would overstate field performance.",
        "src_hybrid",
    ),
    chart_block("localization_modes_chart_block", "chart_localization_operating_modes"),
    table_block("hybrid_metrics_table_block", "table_hybrid_metrics"),
    markdown(
        "peer_comparison_section",
        "## Our system ranks first for the target localization use case\n\n"
        "**A transparent five-point evidence-alignment score puts our system first among six representative published peers plus our prototype.** One point is awarded for each disclosed item: (1) camera-only runtime inference, (2) explicit contact location or state output, (3) a quantitative localization or state result, (4) a meaningful held-out unit, and (5) uncertainty-aware abstention with both performance and coverage. Our system scores **5/5**; Insight and Minsight score **4/5** because their sensor papers do not report abstention with coverage.\n\n"
        "This is a **use-case fit ranking, not an overall field leaderboard**. The accompanying native-metrics table keeps stronger peer force and continuous spatial results visible and does not convert unlike tasks into a single synthetic accuracy number.",
        "src_peer_comparison",
    ),
    {"id": "peer_alignment_strip", "type": "metric-strip", "cardIds": ["card_peer_alignment"], "layout": "full"},
    chart_block("peer_alignment_chart_block", "chart_peer_alignment"),
    table_block("peer_alignment_table_block", "table_peer_alignment"),
    markdown(
        "peer_metrics_note",
        "**The papers are not one benchmark.** Insight and Minsight regress continuous position and force; OmniTact estimates contact angle and reports insertion success; 9DTact emphasizes shape and 6D-force generalization; DIGIT is primarily a compact hardware/manipulation platform paper. ELTac is included as the closest electroluminescent hardware comparator even though its reported inference uses image processing and statistical fitting rather than ML. ‘Not reported’ describes the cited paper, not necessarily the full capability of the platform.",
        "src_peer_comparison",
    ),
    table_block("peer_metrics_table_block", "table_peer_metrics"),
    markdown(
        "coverage_section",
        "## The dataset is complete, but evidence strength varies by metric\n\n"
        "**Archive and frame reconciliation passed, so the principal limitation is scientific support rather than missing data.** The current characterization retains 245 complete sessions and 179,176 source-aligned frames. The 18-metric inventory separates primary, conditionally qualified, exploratory, and explicitly unestablished claims.",
        "src_manual_report",
    ),
    chart_block("metric_status_chart_block", "chart_metric_status"),
    table_block("coverage_table_block", "table_coverage"),
    table_block("validation_gate_table_block", "table_validation_gates"),
    markdown(
        "inventory_note",
        "**The complete metric inventory is the controlling checklist.** It prevents the localization-forward narrative from dropping failed or unestablished outcomes, especially the common force range, true creep, and true force resolution.",
        "src_metric_status",
    ),
    table_block("metric_status_table_block", "table_metric_status"),
    markdown(
        "sensitivity_section",
        "## Sensor response varies sharply across the nine ROI\n\n"
        "**Low-force sensitivity is neither uniform nor consistently positive.** In the session-balanced 0-1 N analysis, ROI 4 has the largest positive slope, ROI 3 is negative, ROI 1 is small positive, and the remaining ROI are estimated at zero under the frozen calculation. This blocks a single monotonic device-wide calibration claim.",
        "src_sensitivity",
    ),
    chart_block("sensitivity_chart_block", "chart_sensitivity"),
    markdown(
        "cross_talk_note",
        "**Spatial separation is also imperfect.** During single-ROI presses, the largest off-target share can match or exceed the intended channel. The stronger event-level localization result therefore comes from temporal/event evidence, not a claim of perfectly isolated optical channels.",
        "src_cross_talk",
    ),
    chart_block("cross_talk_chart_block", "chart_cross_talk"),
    table_block("roi_characterization_table_block", "table_roi_characterization"),
    markdown(
        "noise_lag_section",
        "## Noise, drift and timing limit interpretation\n\n"
        "**Dedicated no-contact recordings show material ROI-to-ROI drift, while noise varies over a narrower range.** Noise-equivalent-force values are stability proxies only; they are not force-resolution measurements.",
        "src_no_contact",
    ),
    chart_block("noise_drift_chart_block", "chart_noise_drift"),
    markdown(
        "lag_note",
        "**Lag estimates are system-level and exploratory.** They include camera, acquisition, synchronization, and material effects and should not be presented as intrinsic sensor response time.",
        "src_lag",
    ),
    chart_block("lag_chart_block", "chart_lag"),
    markdown(
        "automatic_section",
        "## Automatic-cycle results are supporting evidence, not independent replication\n\n"
        "**Hysteresis depends on ROI, speed, displacement and force bin.** Speed strata remain separate, and cycles are summarized within sessions rather than counted as independent replicates.",
        "src_hysteresis",
    ),
    chart_block("hysteresis_chart_block", "chart_hysteresis"),
    markdown(
        "cycle_note",
        "**Within-session cycle variability remains non-trivial across ROI.** These CV values describe short-run repeatability under the automatic protocol; they do not establish between-session repeatability, resolution, or long-term stability.",
        "src_cycle",
    ),
    chart_block("cycle_chart_block", "chart_cycle_repeatability"),
    table_block("auto_effects_table_block", "table_auto_effects"),
    markdown(
        "force_section",
        "## Force estimation remains a secondary, approximate output\n\n"
        "**The expanded candidate search improves mean MAE over a fold-fitted constant by only 4.49%.** Selected model families vary across folds, and rank correlation remains weak. The auditable isotonic mapping is retained for experimental use, but the evidence does not establish force resolution.",
        "src_force_benchmark",
    ),
    chart_block("force_fold_chart_block", "chart_force_folds"),
    table_block("force_fold_table_block", "table_force_folds"),
    markdown(
        "global_force_note",
        "**Global aggregation makes the force story simpler, not stronger.** In the 1.7-3.0 N live interval, the best held-out R² is only 0.0136 and the constant baseline has the best median session MAE. Global output also removes localization, so it should remain a diagnostic benchmark rather than the headline result.",
        "src_global_models",
    ),
    chart_block("global_r2_chart_block", "chart_global_r2"),
    table_block("global_models_table_block", "table_global_models"),
    table_block("global_range_table_block", "table_global_range"),
    markdown(
        "historical_note",
        "**Earlier calibration models are retained for traceability, not as one leaderboard.** Their data sources, force ranges, validation grains and session structures differ materially.",
    ),
    table_block("historical_models_table_block", "table_historical_models"),
    markdown(
        "application_gate_section",
        "## Frozen gates still block a validated application claim\n\n"
        "**Every frozen application-preprocessing fit misses the required 90% supported-bin contact recall.** The best development result is 85.16%; later v4 recovery work is explicitly experimental and does not retroactively pass this gate.",
        "src_application_gate",
    ),
    chart_block("application_gate_chart_block", "chart_application_gate"),
    table_block("application_gate_table_block", "table_application_gate"),
    markdown(
        "next_steps",
        "## Recommended Next Steps\n\n"
        "1. **Prospectively confirm the localization result.** Freeze preprocessing, thresholds and scoring before collecting new grouped sessions.\n"
        "2. **Keep the v4 interface labeled experimental — not validated.** Preserve fail-closed force and ROI outputs outside optical support or below localization confidence.\n"
        "3. **Collect at least three independent sessions in every required ROI/force bin** before rerunning the frozen application gate.\n"
        "4. **Run a true resolution protocol** with randomized, settled small-force steps and independent repetitions.\n"
        "5. **Position the contribution around camera-only event localization and uncertainty handling.** Treat force as approximate until physical validation and resolution gates pass.",
    ),
    markdown(
        "further_questions",
        "## Further Questions\n\n"
        "- Will prospectively frozen data reproduce 93.43% macro F1 and the 100%/82.93% selective operating point?\n"
        "- How much of the localization gain comes from temporal accumulation versus ROI geometry or illumination normalization?\n"
        "- Can a common protocol support direct comparison with continuous-location peers without disguising task differences?\n"
        "- Does a randomized settled-step protocol show useful force resolution in any prospectively selected interval?",
    ),
    markdown(
        "caveats",
        "## Caveats and Assumptions\n\n"
        "- The v3/v4 application results and model-selection benchmarks reuse six already-inspected outer TEST groups. They are recovery evidence, not untouched confirmation.\n"
        "- The literature set is representative rather than exhaustive, and the five-point score is specific to camera-only contact-localization evidence. It is not an overall sensor-performance ranking.\n"
        "- ‘Not reported’ means the cited sensor paper did not disclose the item; it does not prove the broader platform lacks it.\n"
        "- Cross-paper outputs and metrics are not directly comparable. Continuous position error, contact-angle MAE, shape error, force error, manipulation success and event macro F1 remain in their native task definitions.\n"
        "- ELTac is included as the closest electroluminescent hardware comparator, but its reported inference is not ML-based.\n"
        "- The 1.7-3.0 N interval is post-hoc experimental and is not an authoritative all-ROI safe operating range.\n"
        "- Global-output metrics sacrifice localization and cannot replace per-ROI heterogeneity or cross-talk evidence.\n"
        "- Automatic cycles are repeated observations within sessions, not independent replicates. Fixed-displacement settling/recovery is not true constant-force creep.\n"
        "- Noise-equivalent force is a stability proxy, not true force resolution. No synthetic experimental rows or labels were added.",
    ),
]


datasets: dict[str, list[dict[str, Any]]] = {
    "headline_metrics": headline_metrics,
    "data_coverage": data_coverage,
    "evidence_status": evidence_status,
    "metric_status": metric_status,
    "validation_gates": validation_gates,
    "roi_characterization": roi_characterization,
    "cross_talk_summary": cross_talk_summary,
    "noise_drift": noise_drift,
    "lag_summary": lag_summary,
    "hysteresis_summary": hysteresis_summary,
    "cycle_summary": cycle_summary,
    "auto_effects_table": auto_effects_table,
    "localization_model_rows": localization_model_rows,
    "v4_roi_metrics": v4_roi_metrics,
    "force_fold_rows": force_fold_rows,
    "force_fold_table": force_fold_table,
    "hybrid_metrics_table": hybrid_metrics_table,
    "localization_operating_modes": localization_operating_modes,
    "peer_comparison": peer_comparison,
    "peer_alignment_metrics": peer_alignment_metrics,
    "global_models": global_models,
    "global_range": global_range,
    "historical_models": historical_models,
    "application_gate": application_gate,
}


generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
manifest = {
    "version": 1,
    "surface": "report",
    "title": "Camera-Only Visuotactile Contact Localization: Performance and Comparative Positioning",
    "description": "Answer-first technical report leading with camera-only event localization, a transparent primary-literature use-case comparison, and the complete characterization, force, validation-gate, and historical metric inventory available as of August 17, 2026.",
    "generatedAt": generated_at,
    "cards": cards,
    "charts": charts,
    "tables": tables,
    "sources": sources,
    "blocks": blocks,
}

artifact = {
    "surface": "report",
    "manifest": manifest,
    "snapshot": {
        "version": 1,
        "generatedAt": generated_at,
        "status": "ready",
        "datasets": datasets,
    },
    "sources": sources,
}


# Independent high-impact checks before artifact validation.
force_fold_mean = statistics.mean(row["conditional_force_mae_N"] for row in force_benchmark["folds"])
constant_fold_mean = statistics.mean(row["constant_force_mae_N"] for row in force_benchmark["folds"])
expected_status_total = sum(status_counts.values())
established_or_qualified = status_counts["primary"] + status_counts["conditional-qualified"]
best_localization = max(localization_model_rows, key=lambda row: row["macro_f1"])
peer_recomputed_scores = {
    row["sensor"]: sum(
        str(row[field]).startswith("Yes")
        for field in [
            "camera_only_inference",
            "explicit_localization",
            "quantitative_localization",
            "held_out_unit",
            "abstention_reported",
        ]
    )
    for row in peer_comparison
}
our_peer_row = next(row for row in peer_comparison if row["focus_group"] == "Our system")

assert expected_status_total == 18
assert established_or_qualified == headline["established_or_qualified_metrics"]
assert abs(force_fold_mean - force_benchmark["aggregate"]["mean_conditional_force_mae_N"]) < 1e-12
assert abs(constant_fold_mean - force_benchmark["aggregate"]["mean_constant_force_mae_N"]) < 1e-12
assert best_localization["model_key"] == "argmax"
assert global_quality["gate_pass"] is True
assert global_validation["gate_pass"] is True
assert global_validation["overall_assessment"] == "share-with-caveats"
assert len(peer_comparison) == 7
assert all(row["alignment_score"] == peer_recomputed_scores[row["sensor"]] for row in peer_comparison)
assert our_peer_row["alignment_score"] == max(row["alignment_score"] for row in peer_comparison)
assert our_peer_row["rank"] == 1
assert all(row["citation"] and row["source_url"] for row in peer_comparison)
assert all(len(rows) <= 2000 for rows in datasets.values())

serialized_snapshot = json.dumps(datasets, allow_nan=False)
assert len(serialized_snapshot.encode("utf-8")) < 3_000_000

validation_summary = {
    "generated_at": generated_at,
    "overall_assessment": "share-with-caveats",
    "checks": {
        "all_local_source_files_present": all(
            "path" not in item or (WORKSPACE / Path(item["path"])).is_file() for item in sources
        ),
        "metric_inventory_has_18_rows": expected_status_total == 18,
        "established_or_qualified_count_recomputes": established_or_qualified == headline["established_or_qualified_metrics"],
        "force_candidate_mean_mae_recomputes": abs(force_fold_mean - force_benchmark["aggregate"]["mean_conditional_force_mae_N"]) < 1e-12,
        "force_constant_mean_mae_recomputes": abs(constant_fold_mean - force_benchmark["aggregate"]["mean_constant_force_mae_N"]) < 1e-12,
        "argmax_is_best_localization_candidate": best_localization["model_key"] == "argmax",
        "global_quality_gate_pass": global_quality["gate_pass"] is True,
        "global_validation_gate_pass": global_validation["gate_pass"] is True,
        "representative_peer_count_is_7": len(peer_comparison) == 7,
        "peer_scores_recompute_from_disclosed_criteria": all(
            row["alignment_score"] == peer_recomputed_scores[row["sensor"]] for row in peer_comparison
        ),
        "our_system_is_top_use_case_score": our_peer_row["alignment_score"]
        == max(row["alignment_score"] for row in peer_comparison),
        "peer_rows_have_citations_and_source_urls": all(
            row["citation"] and row["source_url"] for row in peer_comparison
        ),
        "artifact_datasets_within_row_limit": all(len(rows) <= 2000 for rows in datasets.values()),
        "artifact_snapshot_under_3mb": len(serialized_snapshot.encode("utf-8")) < 3_000_000,
    },
    "spot_checks": {
        "metric_status_rows": expected_status_total,
        "established_or_qualified_metrics": established_or_qualified,
        "force_candidate_mean_mae_N": force_fold_mean,
        "force_constant_mean_mae_N": constant_fold_mean,
        "best_localization_candidate": best_localization["model"],
        "best_localization_macro_f1": best_localization["macro_f1"],
        "best_current_global_r2_model": current_best_global["model"],
        "best_current_global_r2": current_best_global["r2"],
        "our_use_case_alignment_score": our_peer_row["alignment_score"],
        "our_use_case_rank": our_peer_row["rank"],
        "representative_systems_compared": len(peer_comparison),
    },
    "required_caveats": global_validation["required_caveats"]
    + [
        "The literature comparison is representative, not exhaustive.",
        "The five-point score ranks evidence alignment to camera-only contact localization, not overall sensor quality.",
        "Cross-paper metrics retain their native task definitions and are not normalized into a common benchmark.",
    ],
}

chart_map = [
    {
        "section": "Evidence strength",
        "chart_id": "chart_metric_status",
        "family": "Comparison & ranking",
        "type": "horizontalBar",
        "question": "How many metrics sit at each evidence level?",
        "takeaway": "Thirteen are established/qualified and three are not established.",
        "source_id": "src_metric_status",
    },
    {
        "section": "ROI response",
        "chart_id": "chart_sensitivity",
        "family": "Comparison & ranking",
        "type": "bar",
        "question": "Is low-force sensitivity consistent across ROI?",
        "takeaway": "Sensitivity is highly heterogeneous and not uniformly positive.",
        "source_id": "src_sensitivity",
    },
    {
        "section": "ROI response",
        "chart_id": "chart_cross_talk",
        "family": "Composition",
        "type": "grouped bar",
        "question": "Does each pressed ROI dominate the optical response?",
        "takeaway": "The largest off-target share can rival or exceed target share.",
        "source_id": "src_cross_talk",
    },
    {
        "section": "Noise and timing",
        "chart_id": "chart_noise_drift",
        "family": "Relationship",
        "type": "scatter",
        "question": "Do ROI with higher noise also drift more?",
        "takeaway": "Drift varies more than noise and is not explained by noise alone.",
        "source_id": "src_no_contact",
    },
    {
        "section": "Noise and timing",
        "chart_id": "chart_lag",
        "family": "Comparison & ranking",
        "type": "bar",
        "question": "How does system-level lag vary by ROI?",
        "takeaway": "Median lag spans negative and positive values with broad bounds.",
        "source_id": "src_lag",
    },
    {
        "section": "Automatic cycles",
        "chart_id": "chart_hysteresis",
        "family": "Comparison & ranking",
        "type": "grouped bar",
        "question": "How does absolute hysteresis vary by ROI and speed?",
        "takeaway": "Hysteresis is condition-specific and speed strata should remain separate.",
        "source_id": "src_hysteresis",
    },
    {
        "section": "Automatic cycles",
        "chart_id": "chart_cycle_repeatability",
        "family": "Comparison & ranking",
        "type": "bar",
        "question": "How variable are repeated cycles within sessions?",
        "takeaway": "Cycle CV remains non-trivial and is not independent replication.",
        "source_id": "src_cycle",
    },
    {
        "section": "Experimental localization",
        "chart_id": "chart_localization_models",
        "family": "Comparison & ranking",
        "type": "horizontalBar",
        "question": "Which event-localization candidate performs best?",
        "takeaway": "Accumulated argmax has the highest macro F1.",
        "source_id": "src_event_benchmark",
    },
    {
        "section": "Experimental localization",
        "chart_id": "chart_v4_roi_recall",
        "family": "Comparison & ranking",
        "type": "bar",
        "question": "Is event recall broad across ROI?",
        "takeaway": "All ROI exceed 90% in the post-hoc recovery evaluation.",
        "source_id": "src_hybrid",
    },
    {
        "section": "Experimental localization",
        "chart_id": "chart_localization_operating_modes",
        "family": "Accuracy and coverage trade-off",
        "type": "grouped bar",
        "question": "What is gained and lost when uncertain events are withheld?",
        "takeaway": "Selective output reaches 100% macro F1 while retaining 82.93% of events.",
        "source_id": "src_hybrid",
    },
    {
        "section": "Primary-literature comparison",
        "chart_id": "chart_peer_alignment",
        "family": "Criteria-specific ranking",
        "type": "horizontalBar",
        "question": "Which representative systems disclose the complete evidence package for camera-only contact localization?",
        "takeaway": "Our system is the only compared row with all five disclosed evidence items; this is not an overall performance rank.",
        "source_id": "src_peer_comparison",
    },
    {
        "section": "Force models",
        "chart_id": "chart_force_folds",
        "family": "Uncertainty & benchmark",
        "type": "grouped bar",
        "question": "Do selected force models beat fold constants?",
        "takeaway": "Gains are marginal and inconsistent.",
        "source_id": "src_force_benchmark",
    },
    {
        "section": "Force models",
        "chart_id": "chart_global_r2",
        "family": "Uncertainty & benchmark",
        "type": "horizontalBar",
        "question": "Do current global models explain held-out variation?",
        "takeaway": "The best R-squared is only 0.014.",
        "source_id": "src_global_models",
    },
    {
        "section": "Frozen gates",
        "chart_id": "chart_application_gate",
        "family": "Uncertainty & benchmark",
        "type": "bar with reference",
        "question": "Did any application fit reach the 90% recall gate?",
        "takeaway": "No fit reached the frozen requirement.",
        "source_id": "src_application_gate",
    },
]


with (OUTPUT_DIR / "artifact.json").open("w", encoding="utf-8", newline="\n") as handle:
    json.dump(artifact, handle, indent=2, ensure_ascii=False, allow_nan=False)
    handle.write("\n")

with (OUTPUT_DIR / "validation_summary.json").open("w", encoding="utf-8", newline="\n") as handle:
    json.dump(validation_summary, handle, indent=2, ensure_ascii=False, allow_nan=False)
    handle.write("\n")

with (OUTPUT_DIR / "chart_map.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(chart_map[0].keys()))
    writer.writeheader()
    writer.writerows(chart_map)

with (OUTPUT_DIR / "literature_comparison.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(peer_comparison[0].keys()))
    writer.writeheader()
    writer.writerows(peer_comparison)

print(
    json.dumps(
        {
            "artifact": str(OUTPUT_DIR / "artifact.json"),
            "validation_summary": str(OUTPUT_DIR / "validation_summary.json"),
            "chart_map": str(OUTPUT_DIR / "chart_map.csv"),
            "literature_comparison": str(OUTPUT_DIR / "literature_comparison.csv"),
            "datasets": len(datasets),
            "rows": sum(len(rows) for rows in datasets.values()),
            "charts": len(charts),
            "tables": len(tables),
            "blocks": len(blocks),
        },
        indent=2,
    )
)
