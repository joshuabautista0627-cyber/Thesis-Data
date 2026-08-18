from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import statistics
import sys
import textwrap
from typing import Any, Iterable, Sequence

import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.enum.style import WD_STYLE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


WORKSPACE = Path(__file__).resolve().parents[2]
PROJECT = WORKSPACE / "spare_calibration_gui"
OUT = Path(__file__).resolve().parent
CHARTS = OUT / "charts"
CHARTS.mkdir(parents=True, exist_ok=True)

CHAR_ROOT = (
    PROJECT
    / "analysis_outputs"
    / "live_sensor_study"
    / "characterization"
    / "evidence-products-manual-first-authoritative"
)
CHAR_TABLES = CHAR_ROOT / "tables"
FEATURE_STORE = (
    PROJECT
    / "analysis_outputs"
    / "live_sensor_study"
    / "feature_store"
    / "feature-store-c0ec762f1dc888a7"
)
MODEL_ROOT = PROJECT / "models" / "live_sensor_experimental_hybrid_v4"

DOCX_PATH = OUT / "SPARE_Calibration_Program_Full_Technical_Report_2026-08-18.docx"
MD_PATH = OUT / "SPARE_Calibration_Program_Full_Technical_Report_2026-08-18.md"
NOTEBOOK_PATH = OUT / "SPARE_Report_Validation_Companion.ipynb"
VALIDATION_JSON_PATH = OUT / "validation_checks.json"
EVIDENCE_CSV_PATH = OUT / "evidence_manifest.csv"
SCHEMA_CSV_PATH = OUT / "current_schema_dictionary.csv"
CHART_MAP_PATH = OUT / "chart_map.csv"

INK = "#182230"
MUTED = "#5B6573"
BLUE = "#2E74B5"
BLUE_DARK = "#1F4D78"
BLUE_LIGHT = "#DCEAF7"
GOLD = "#F59E0B"
GOLD_LIGHT = "#FEF3C7"
GRAY = "#F2F4F7"
RED_LIGHT = "#FDECEC"
WHITE = "#FFFFFF"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


metrics = load_json(MODEL_ROOT / "metrics.json")
model_manifest = load_json(MODEL_ROOT / "manifest.json")
release_decision = load_json(MODEL_ROOT / "release_decision.json")
force_model = load_json(MODEL_ROOT / "force_model.json")
localization_model = load_json(MODEL_ROOT / "localization_model.json")
event_model = load_json(MODEL_ROOT / "event_model.json")
preprocessing_model = load_json(MODEL_ROOT / "preprocessing.json")
char_quality = load_json(CHAR_ROOT / "quality_report.json")
char_validation = load_json(CHAR_ROOT / "validation_report.json")
char_run = load_json(CHAR_ROOT / "run_manifest.json")
feature_reconciliation = load_json(FEATURE_STORE / "reconciliation_report.json")
feature_run = load_json(FEATURE_STORE / "run_manifest.json")

metric_status = pd.read_csv(CHAR_TABLES / "metric_status.csv")
sensitivity = pd.read_csv(CHAR_TABLES / "manual_sensitivity_nonlinearity.csv")
no_contact = pd.read_csv(CHAR_TABLES / "no_contact_summary.csv")
snr = pd.read_csv(CHAR_TABLES / "manual_snr.csv")
detection = pd.read_csv(CHAR_TABLES / "manual_detection_summary.csv")
lag_summary = pd.read_csv(CHAR_TABLES / "manual_system_lag_summary.csv")
nef = pd.read_csv(CHAR_TABLES / "noise_equivalent_force_proxy.csv")
plateau = pd.read_csv(CHAR_TABLES / "manual_plateau_observations.csv")
repeatability = pd.read_csv(CHAR_TABLES / "manual_repeatability.csv")
cross_talk = pd.read_csv(CHAR_TABLES / "cross_talk.csv")
hysteresis = pd.read_csv(CHAR_TABLES / "automatic_hysteresis.csv")
hysteresis_session = pd.read_csv(CHAR_TABLES / "automatic_hysteresis_session.csv")
phase_session = pd.read_csv(CHAR_TABLES / "automatic_phase_session.csv")
coverage_manual = pd.read_csv(CHAR_TABLES / "coverage_manual_sessions.csv")
coverage_auto = pd.read_csv(CHAR_TABLES / "coverage_automatic_sessions.csv")

agg = metrics["aggregate"]
force_agg = metrics["force_source_metrics"]["aggregate"]
force_bench = metrics["force_candidate_benchmark"]
loc_bench = metrics["localization_candidate_benchmark"]


def check(condition: bool, name: str, evidence: Any) -> dict[str, Any]:
    return {"check": name, "passed": bool(condition), "evidence": evidence}


validation_checks: list[dict[str, Any]] = [
    check(
        feature_reconciliation["session_count"] == 245,
        "feature_store_session_count",
        feature_reconciliation["session_count"],
    ),
    check(
        feature_reconciliation["total_rows"] == 179176,
        "feature_store_frame_count",
        feature_reconciliation["total_rows"],
    ),
    check(
        feature_reconciliation["missing_video_frames"] == 0
        and feature_reconciliation["unmatched_video_frames"] == 0,
        "feature_store_video_reconciliation",
        {
            "missing": feature_reconciliation["missing_video_frames"],
            "unmatched": feature_reconciliation["unmatched_video_frames"],
        },
    ),
    check(
        char_quality["gate_pass"] is True and char_validation["gate_pass"] is True,
        "characterization_quality_and_validation_gates",
        {
            "quality": char_quality["gate_pass"],
            "validation": char_validation["gate_pass"],
        },
    ),
    check(len(metric_status) == 18, "characterization_metric_inventory", len(metric_status)),
    check(agg["event_count"] == 102, "v4_event_count", agg["event_count"]),
    check(
        agg["independent_session_count"] == 54,
        "v4_independent_session_count",
        agg["independent_session_count"],
    ),
    check(
        abs(agg["event_localization_macro_f1"] - 0.9342785558588881) < 1e-12,
        "v4_event_localization_macro_f1",
        agg["event_localization_macro_f1"],
    ),
    check(
        abs(agg["mean_conditional_force_mae_N"] - 0.32798681555113973) < 1e-12,
        "v4_force_conditional_mae",
        agg["mean_conditional_force_mae_N"],
    ),
    check(
        release_decision["validated_claim_allowed"] is False,
        "v4_release_claim_is_withheld",
        release_decision["validated_claim_allowed"],
    ),
    check(
        release_decision["force_resolution_established"] is False,
        "v4_force_resolution_is_not_established",
        release_decision["force_resolution_established"],
    ),
    check(
        set(char_validation["details"]["conditionally_qualified_detection_rois"])
        == {3, 9},
        "characterization_detection_is_sparse",
        char_validation["details"]["conditionally_qualified_detection_rois"],
    ),
    check(
        len(force_model["x_thresholds"]) == len(force_model["y_thresholds_N"]) == 32,
        "force_model_knot_count",
        len(force_model["x_thresholds"]),
    ),
    check(
        (CHARTS / "chart_live_sensor_gui.png").is_file(),
        "live_sensor_gui_screenshot_exists",
        str(CHARTS / "chart_live_sensor_gui.png"),
    ),
]

failed = [item for item in validation_checks if not item["passed"]]
if failed:
    raise RuntimeError(f"Validation checks failed: {failed}")

VALIDATION_JSON_PATH.write_text(
    json.dumps(
        {
            "created_for": DOCX_PATH.name,
            "overall_assessment": "share_with_caveats",
            "checks": validation_checks,
            "current_test_result": {
                "passed": 490,
                "method": "isolated test-file groups",
                "live_sensor_followup": "35 dedicated live-sensor/runtime/report/contract cases passed on 18 August 2026",
                "pip_check": "No broken requirements found.",
                "monolithic_windows_limitation": (
                    "A mixed Qt run reproduced the documented pyqtgraph garbage-collection "
                    "access violation; all files passed when isolated."
                ),
            },
        },
        indent=2,
        ensure_ascii=False,
    ),
    encoding="utf-8",
)


# Export the current authoritative schema dictionary from project code.
sys.path.insert(0, str(PROJECT))
from data.schemas import (  # noqa: E402
    DATA_DICTIONARY_COLUMNS,
    FRAME_FEATURE_COLUMNS,
    LOADCELL_RAW_COLUMNS,
    MASTER_COLUMNS,
    data_dictionary_rows,
)

with SCHEMA_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=DATA_DICTIONARY_COLUMNS)
    writer.writeheader()
    writer.writerows(data_dictionary_rows())


EVIDENCE = [
    ("E01", "spare_calibration_gui/README.md", "Current program purpose, installation, UI, operating workflow, exports, and use restrictions", "current implementation documentation"),
    ("E02", "spare_calibration_gui/LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md", "Authoritative manual-only application-model plan, gates, splits, and deployment contract", "authoritative plan"),
    ("E03", "spare_calibration_gui/SENSOR_CHARACTERIZATION_MASTER_PLAN.md", "Manual-first characterization plan and permitted automatic-archive role", "authoritative plan"),
    ("E04", "spare_calibration_gui/LIVE_SENSOR_DECISION_LOG.md", "Dated model-governance and v1-v4 recovery decisions", "decision record"),
    ("E05", "spare_calibration_gui/LIVE_SENSOR_IMPLEMENTATION_STATUS.md", "Current implementation, metric, test, and release-gate status", "status record"),
    ("E06", "spare_calibration_gui/PHYSICAL_HARDWARE_VALIDATION_20260803.md", "Camera, load-cell, calibration, synchronization, and physical-session measurements", "physical validation"),
    ("E07", "spare_calibration_gui/ENDER3_REPEATED_PRESS_IMPLEMENTATION_REPORT_20260804.md", "Printer implementation, protocol tests, and remaining physical limitations", "implementation validation"),
    ("E08", "spare_calibration_gui/ENGINEERING_REVIEWS.md", "Architecture, acquisition, integrity, and GUI review findings", "engineering review"),
    ("E09", "spare_calibration_gui/config/default_config.json", "Runtime defaults for camera, processing, load cell, printer, recording, and display", "configuration"),
    ("E10", "spare_calibration_gui/config/live_sensor_release_gates.json", "Frozen acceptance gates", "configuration"),
    ("E11", "spare_calibration_gui/config/manual_timestamp_alignment.json", "Offline per-session timestamp-alignment contract", "configuration"),
    ("E12", "spare_calibration_gui/config/manual_only_experimental_hybrid_v4.json", "v4 hybrid model disclosure and operating contract", "configuration"),
    ("E13", "spare_calibration_gui/processing/feature_extraction.py", "HSV conversion and per-ROI optical feature formulas", "source code"),
    ("E14", "spare_calibration_gui/processing/baseline.py", "Baseline capture, provenance invalidation, and drift gate", "source code"),
    ("E15", "spare_calibration_gui/processing/loadcell_calibration.py", "Signed counts/g calibration, tare, verification, and unit conversion", "source code"),
    ("E16", "spare_calibration_gui/processing/synchronization.py", "Host-monotonic frame/load-cell synchronization", "source code"),
    ("E17", "spare_calibration_gui/core/timestamp_alignment.py", "Offline training-label timestamp interpolation", "source code"),
    ("E18", "spare_calibration_gui/core/experimental_live_sensor.py", "Hash-checked NumPy-only v4 runtime, state machine, event and withholding logic", "source code"),
    ("E19", "spare_calibration_gui/core/live_sensor_contracts.py", "Fixed layout/orientation and fail-closed scientific-state contract", "source code"),
    ("E20", "spare_calibration_gui/data/schemas.py", "Canonical CSV schemas and data-dictionary definitions", "source code"),
    ("E21", "spare_calibration_gui/services/session_recorder.py", "Incremental recording, partial artifacts, integrity validation, and finalization", "source code"),
    ("E22", "spare_calibration_gui/arduino/hx711_nano_stream/hx711_nano_stream.ino", "Nano/HX711 firmware and ASCII serial protocol", "firmware"),
    ("E23", "spare_calibration_gui/analysis_outputs/live_sensor_study/feature_store/feature-store-c0ec762f1dc888a7/reconciliation_report.json", "245-session/179,176-frame immutable feature-store reconciliation", "derived evidence"),
    ("E24", "spare_calibration_gui/analysis_outputs/live_sensor_study/characterization/evidence-products-manual-first-authoritative/quality_report.json", "Characterization data-quality checks", "validated evidence"),
    ("E25", "spare_calibration_gui/analysis_outputs/live_sensor_study/characterization/evidence-products-manual-first-authoritative/validation_report.json", "Characterization methodology validation and required caveats", "validated evidence"),
    ("E26", "spare_calibration_gui/analysis_outputs/live_sensor_study/characterization/evidence-products-manual-first-authoritative/tables", "Authoritative characterization tables", "validated evidence"),
    ("E27", "spare_calibration_gui/models/live_sensor_experimental_hybrid_v4/metrics.json", "v4 grouped retrospective model metrics", "post-hoc experimental evidence"),
    ("E28", "spare_calibration_gui/models/live_sensor_experimental_hybrid_v4/release_decision.json", "Explicit release blockers and prohibited validated claim", "release decision"),
    ("E29", "spare_calibration_gui/models/live_sensor_experimental_hybrid_v4/force_model.json", "32-knot monotonic isotonic approximation", "experimental model bundle"),
    ("E30", "spare_calibration_gui/models/live_sensor_experimental_hybrid_v4/localization_model.json", "Accumulated active-fraction argmax and selective confidence rule", "experimental model bundle"),
    ("E31", "analysis_outputs/linear_models/validation_metrics.json", "Historical automatic-archive linear model metrics", "superseded exploratory evidence"),
    ("E32", "analysis_outputs/manual_linear_models/validation_metrics.json", "Historical nine-session manual linear model metrics", "superseded exploratory evidence"),
    ("E33", "analysis_outputs/manual_global_models_clean/validation_metrics.json", "Historical TEST2-TEST6 clean global-force model metrics", "superseded diagnostic evidence"),
    ("E34", "reports/software_calibration_metrics_2026-08-17/validation_summary.json", "Previous metrics-report QA and share-with-caveats classification", "report QA"),
    ("E35", "spare_calibration_gui/tests", "Current automated test suite", "verification"),
    ("E36", "spare_calibration_gui/gui/live_sensor_tab.py", "Live Sensor tab controls, state-colored presentation, replay, completed-event capture, and report export", "current GUI source code"),
    ("E37", "spare_calibration_gui/core/live_sensor_report.py", "Atomic HTML/CSV/JSON completed-event post-processing report generation", "current reporting source code"),
    ("E38", "spare_calibration_gui/tests/test_experimental_live_sensor.py; spare_calibration_gui/tests/test_live_sensor_report.py; spare_calibration_gui/tests/test_live_sensor_contracts.py", "Dedicated bundle, engine, GUI, reporting, contract, replay, abstention, and multi-contact tests", "35 current passing test cases"),
]

with EVIDENCE_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["evidence_id", "workspace_relative_path", "use", "status"])
    writer.writerows(EVIDENCE)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    filename = "calibrib.ttf" if bold else "calibri.ttf"
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
    return ImageFont.truetype(str(path), size=size)


def _canvas(title: str, subtitle: str, width: int = 1600, height: int = 900) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(image)
    draw.text((80, 45), title, font=_font(46, True), fill=INK)
    draw.text((80, 105), subtitle, font=_font(27), fill=MUTED)
    return image, draw


def _save(image: Image.Image, name: str) -> Path:
    path = CHARTS / name
    image.save(path, format="PNG", optimize=True)
    return path


def horizontal_bar_chart(
    name: str,
    title: str,
    subtitle: str,
    labels: Sequence[str],
    values: Sequence[float],
    *,
    maximum: float | None = None,
    benchmark: float | None = None,
    percent: bool = False,
    value_decimals: int = 1,
) -> Path:
    image, draw = _canvas(title, subtitle)
    left, right, top, bottom = 420, 1510, 190, 820
    maximum = float(maximum if maximum is not None else max(values) * 1.12)
    row_h = (bottom - top) / len(labels)
    if benchmark is not None:
        x = left + (right - left) * benchmark / maximum
        draw.line((x, top - 15, x, bottom), fill=GOLD, width=5)
        draw.text((x + 8, top - 42), f"benchmark {benchmark * 100:.0f}%" if percent else f"benchmark {benchmark:g}", font=_font(22, True), fill=GOLD)
    for i, (label, value) in enumerate(zip(labels, values)):
        y0 = top + i * row_h + row_h * 0.20
        y1 = top + (i + 1) * row_h - row_h * 0.20
        x1 = left + (right - left) * max(0.0, value) / maximum
        draw.text((70, y0 + 4), label, font=_font(28), fill=INK)
        draw.rectangle((left, y0, right, y1), fill=GRAY)
        draw.rectangle((left, y0, x1, y1), fill=BLUE, outline=BLUE_DARK, width=2)
        value_text = f"{value * 100:.{value_decimals}f}%" if percent else f"{value:.{value_decimals}f}"
        draw.text((min(x1 + 12, right - 100), y0 + 5), value_text, font=_font(26, True), fill=INK)
    draw.line((left, bottom, right, bottom), fill=INK, width=2)
    return _save(image, name)


def grouped_fold_chart() -> Path:
    folds = force_bench["folds"]
    image, draw = _canvas(
        "Force approximation versus a session-balanced constant",
        "Outer-fold conditional MAE; lower is better. Paired within each held-out TEST group (post-hoc).",
    )
    left, right, top, bottom = 120, 1510, 200, 700
    max_y = 0.40
    for tick in [0, 0.1, 0.2, 0.3, 0.4]:
        y = bottom - (bottom - top) * tick / max_y
        draw.line((left, y, right, y), fill="#D8DDE5", width=2)
        draw.text((50, y - 15), f"{tick:.1f}", font=_font(24), fill=MUTED)
    group_w = (right - left) / len(folds)
    bar_w = 58
    for i, fold in enumerate(folds):
        center = left + group_w * (i + 0.5)
        values = [fold["conditional_force_mae_N"], fold["constant_force_mae_N"]]
        for j, (value, color) in enumerate(zip(values, [BLUE, GOLD])):
            x0 = center + (j - 1) * bar_w
            x1 = x0 + bar_w
            y0 = bottom - (bottom - top) * value / max_y
            draw.rectangle((x0, y0, x1, bottom), fill=color, outline=INK, width=2)
            draw.text((x0 - 3, y0 - 34), f"{value:.3f}", font=_font(20, True), fill=INK)
        draw.text((center - 30, bottom + 18), f"Fold {i + 1}", font=_font(24), fill=INK)
    draw.rectangle((1120, 760, 1160, 795), fill=BLUE, outline=INK)
    draw.text((1175, 760), "selected per-fold model", font=_font(24), fill=INK)
    draw.rectangle((1120, 810, 1160, 845), fill=GOLD, outline=INK)
    draw.text((1175, 810), "constant baseline", font=_font(24), fill=INK)
    return _save(image, "chart_force_mae_by_fold.png")


def force_spread_chart() -> Path:
    folds = force_bench["folds"]
    ratios = [f["prediction_sd_N"] / f["truth_sd_N"] for f in folds]
    rhos = [f["force_spearman_rho"] for f in folds]
    image, draw = _canvas(
        "Why the reported force MAE is not force resolution",
        "Prediction spread is only 13-32% of true spread and rank correlation is weak across outer folds.",
    )
    left, right, top, bottom = 120, 1510, 210, 730
    group_w = (right - left) / len(folds)
    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        y = bottom - (bottom - top) * tick
        draw.line((left, y, right, y), fill="#D8DDE5", width=2)
        draw.text((55, y - 15), f"{tick:.2f}", font=_font(23), fill=MUTED)
    for i, (ratio, rho) in enumerate(zip(ratios, rhos)):
        center = left + group_w * (i + 0.5)
        for j, (value, color) in enumerate(zip([ratio, rho], [BLUE, GOLD])):
            x0 = center + (j - 1) * 60
            y0 = bottom - (bottom - top) * max(0.0, value)
            draw.rectangle((x0, y0, x0 + 60, bottom), fill=color, outline=INK, width=2)
            draw.text((x0, y0 - 32), f"{value:.2f}", font=_font(20, True), fill=INK)
        draw.text((center - 30, bottom + 18), f"Fold {i + 1}", font=_font(24), fill=INK)
    draw.rectangle((980, 790, 1020, 825), fill=BLUE, outline=INK)
    draw.text((1035, 790), "predicted SD / true SD", font=_font(24), fill=INK)
    draw.rectangle((1280, 790, 1320, 825), fill=GOLD, outline=INK)
    draw.text((1335, 790), "Spearman rho", font=_font(24), fill=INK)
    return _save(image, "chart_force_resolution_limits.png")


def sensitivity_chart() -> Path:
    values = sensitivity.sort_values("roi")["low_force_sensitivity_light_per_N"].fillna(0).tolist()
    image, draw = _canvas(
        "Manual low-force sensitivity is strongly position-dependent",
        "Session-balanced slope of positive integrated light versus force over 0-1 N; zero indicates no positive fitted slope.",
    )
    left, right, top, bottom = 140, 1510, 210, 720
    min_v, max_v = -130, 340
    zero_y = bottom - (bottom - top) * (0 - min_v) / (max_v - min_v)
    draw.line((left, zero_y, right, zero_y), fill=INK, width=3)
    group_w = (right - left) / 9
    for i, value in enumerate(values):
        center = left + group_w * (i + 0.5)
        y = bottom - (bottom - top) * (value - min_v) / (max_v - min_v)
        color = BLUE if value >= 0 else GOLD
        y0, y1 = sorted([zero_y, y])
        draw.rectangle((center - 42, y0, center + 42, y1), fill=color, outline=INK, width=2)
        draw.text((center - 36, y - 34 if value >= 0 else y + 8), f"{value:.1f}", font=_font(21, True), fill=INK)
        draw.text((center - 28, bottom + 18), f"ROI {i + 1}", font=_font(23), fill=INK)
    draw.text((55, top - 5), "340", font=_font(22), fill=MUTED)
    draw.text((55, zero_y - 12), "0", font=_font(22), fill=MUTED)
    draw.text((35, bottom - 10), "-130", font=_font(22), fill=MUTED)
    return _save(image, "chart_manual_sensitivity.png")


def cross_talk_chart() -> Path:
    matrix = cross_talk.pivot(index="target_roi", columns="measured_roi", values="median_share_total").reindex(index=range(1, 10), columns=range(1, 10))
    image, draw = _canvas(
        "Single-ROI presses distribute optical response across the array",
        "Median share of total positive response; row = labeled target, column = measured ROI. Medians are session-balanced.",
        width=1500,
        height=1080,
    )
    left, top, cell = 290, 220, 78
    for row in range(9):
        draw.text((170, top + row * cell + 20), f"Target {row + 1}", font=_font(23), fill=INK)
        for col in range(9):
            value = float(matrix.iloc[row, col])
            value = 0.0 if not math.isfinite(value) else value
            level = min(max(value, 0.0), 1.0)
            start = (238, 244, 249)
            end = (46, 116, 181)
            rgb = tuple(round(start[k] + (end[k] - start[k]) * level) for k in range(3))
            x0, y0 = left + col * cell, top + row * cell
            draw.rectangle((x0, y0, x0 + cell, y0 + cell), fill=rgb, outline=WHITE, width=2)
            if row == col:
                draw.rectangle((x0 + 3, y0 + 3, x0 + cell - 3, y0 + cell - 3), outline=GOLD, width=5)
            draw.text((x0 + 17, y0 + 24), f"{value:.2f}", font=_font(20, True), fill=INK if level < 0.5 else WHITE)
    for col in range(9):
        draw.text((left + col * cell + 18, top - 42), f"ROI {col + 1}", font=_font(22), fill=INK)
    draw.text((left + 200, top + 9 * cell + 35), "Measured ROI", font=_font(28, True), fill=INK)
    draw.text((1050, 325), "Gold outline = target channel", font=_font(24), fill=GOLD)
    draw.text((1050, 375), "Darker blue = larger response share", font=_font(24), fill=BLUE_DARK)
    return _save(image, "chart_cross_talk_matrix.png")


def noise_drift_chart() -> Path:
    ordered = no_contact.sort_values("roi")
    noise_vals = ordered["signed_noise_mad"].tolist()
    drift_vals = ordered["median_abs_drift_light_per_min"].tolist()
    image, draw = _canvas(
        "Dedicated no-contact recordings show substantial noise and short-term drift",
        "Eleven independent no-contact sessions; signed-light MAD and median absolute drift are reported separately.",
    )
    left, mid, right, top, bottom = 120, 790, 1510, 220, 730
    for panel_left, panel_right, values, color, label in [
        (left, mid - 40, noise_vals, BLUE, "Signed-light MAD"),
        (mid + 40, right, drift_vals, GOLD, "Absolute drift (light/min)"),
    ]:
        max_v = max(values) * 1.12
        group_w = (panel_right - panel_left) / 9
        draw.text((panel_left, 165), label, font=_font(28, True), fill=INK)
        for i, value in enumerate(values):
            x0 = panel_left + group_w * i + group_w * 0.18
            x1 = panel_left + group_w * (i + 1) - group_w * 0.18
            y0 = bottom - (bottom - top) * value / max_v
            draw.rectangle((x0, y0, x1, bottom), fill=color, outline=INK, width=2)
            draw.text((x0, y0 - 30), f"{value:.0f}", font=_font(18, True), fill=INK)
            draw.text((x0, bottom + 16), str(i + 1), font=_font(22), fill=INK)
        draw.text((panel_left + (panel_right - panel_left) / 2 - 35, bottom + 55), "ROI", font=_font(24), fill=MUTED)
    return _save(image, "chart_no_contact_noise_drift.png")


def hysteresis_chart() -> Path:
    frame = hysteresis.assign(abs_pct=hysteresis["median_hysteresis_percent_observed_span"].abs())
    per_roi = frame.groupby("roi", as_index=True)["abs_pct"].median().reindex(range(1, 10))
    return horizontal_bar_chart(
        "chart_hysteresis_by_roi.png",
        "Automatic cyclic evidence indicates ROI-dependent hysteresis",
        "Median absolute loading-unloading separation across automatic speed/displacement/force-bin summaries; two sessions per exact condition.",
        [f"ROI {i}" for i in range(1, 10)],
        per_roi.tolist(),
        maximum=14,
        percent=False,
        value_decimals=2,
    )


charts = {
    "localization_recall": horizontal_bar_chart(
        "chart_localization_recall.png",
        "Event-level localization recall by ROI",
        "Six outer TEST-group folds; 102 single-press events from 54 independent sessions. Post-hoc experimental evidence.",
        [f"ROI {i}" for i in range(1, 10)],
        [float(agg["per_roi_event_localization_recall"][str(i)]) for i in range(1, 10)],
        maximum=1.05,
        benchmark=0.80,
        percent=True,
    ),
    "localization_models": horizontal_bar_chart(
        "chart_localization_model_comparison.png",
        "Event-localization model comparison",
        "Macro F1 from grouped outer-fold recovery evaluation; the simple accumulated argmax was retained.",
        ["Accumulated argmax", "Extra Trees", "Shrinkage LDA", "Logistic regression"],
        [
            loc_bench["aggregate"]["argmax"]["macro_f1"],
            loc_bench["aggregate"]["extra_trees"]["macro_f1"],
            loc_bench["aggregate"]["shrinkage_lda"]["macro_f1"],
            loc_bench["aggregate"]["logistic"]["macro_f1"],
        ],
        maximum=1.0,
        benchmark=0.80,
        percent=True,
    ),
    "force_mae": grouped_fold_chart(),
    "force_resolution": force_spread_chart(),
    "sensitivity": sensitivity_chart(),
    "cross_talk": cross_talk_chart(),
    "noise_drift": noise_drift_chart(),
    "hysteresis": hysteresis_chart(),
    "live_sensor_gui": CHARTS / "chart_live_sensor_gui.png",
}


with CHART_MAP_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["chart", "analytical_question", "takeaway", "source", "output"])
    writer.writerows(
        [
            ("localization_recall", "Does every ROI meet the 80% recall benchmark?", "All reported per-ROI event recalls are 90.28-100%, but evidence is post-hoc.", "v4 metrics.json", charts["localization_recall"].name),
            ("localization_models", "Which event-localization family performed best?", "Accumulated argmax had the highest aggregate macro F1.", "v4 metrics.json", charts["localization_models"].name),
            ("force_mae", "Does the force model materially outperform a constant?", "The average improvement was only 4.49% and varied by fold.", "v4 metrics.json", charts["force_mae"].name),
            ("force_resolution", "Does the estimate track within-session force variation?", "Spread and rank tracking were weak, so resolution is not established.", "v4 metrics.json", charts["force_resolution"].name),
            ("sensitivity", "Is optical sensitivity spatially uniform?", "Slopes are heterogeneous, zero, or negative in several ROIs.", "manual_sensitivity_nonlinearity.csv", charts["sensitivity"].name),
            ("cross_talk", "How concentrated is a single-ROI press response?", "Off-target response is substantial for multiple target ROIs.", "cross_talk.csv", charts["cross_talk"].name),
            ("noise_drift", "What is the unloaded stability?", "Noise and short-term drift are material and ROI-dependent.", "no_contact_summary.csv", charts["noise_drift"].name),
            ("hysteresis", "How large is automatic loading-unloading separation?", "Absolute hysteresis is ROI-dependent and condition estimates are descriptive.", "automatic_hysteresis.csv", charts["hysteresis"].name),
            ("live_sensor_gui", "How does the operator see and control the experimental live sensor?", "The tab keeps validation warnings, force/ROI state, setup controls, report export, and archive evidence visible together.", "LiveSensorTab bundled replay screenshot [E36]", charts["live_sensor_gui"].name),
        ]
    )


def shade_cell(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill.lstrip("#"))


def set_cell_width(cell, width_dxa: int) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(width_dxa))
    tc_w.set(qn("w:type"), "dxa")


def set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def set_row_cant_split(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    cant_split.set(qn("w:val"), "true")
    tr_pr.append(cant_split)


def set_table_width(table, width_dxa: int = 9360) -> None:
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(width_dxa))
    tbl_w.set(qn("w:type"), "dxa")


def add_field(paragraph, instruction: str) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instr, separate, end])


class Report:
    def __init__(self) -> None:
        self.doc = Document()
        self.md: list[str] = []
        self.figure_number = 0
        self.table_number = 0
        self._configure()

    def _configure(self) -> None:
        section = self.doc.sections[0]
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
        section.top_margin = Inches(0.85)
        section.bottom_margin = Inches(0.85)
        section.left_margin = Inches(0.9)
        section.right_margin = Inches(0.9)
        section.header_distance = Inches(0.35)
        section.footer_distance = Inches(0.35)

        styles = self.doc.styles
        normal = styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(10.5)
        normal.font.color.rgb = RGBColor.from_string(INK.lstrip("#"))
        normal.paragraph_format.space_after = Pt(6)
        normal.paragraph_format.line_spacing = 1.08

        for name, size, color, before, after in [
            ("Title", 28, INK, 0, 12),
            ("Subtitle", 14, MUTED, 0, 10),
            ("Heading 1", 16, BLUE, 16, 8),
            ("Heading 2", 13, BLUE_DARK, 12, 6),
            ("Heading 3", 11.5, BLUE_DARK, 8, 4),
        ]:
            style = styles[name]
            style.font.name = "Calibri"
            style.font.size = Pt(size)
            style.font.color.rgb = RGBColor.from_string(color.lstrip("#"))
            style.font.bold = name != "Subtitle"
            style.paragraph_format.space_before = Pt(before)
            style.paragraph_format.space_after = Pt(after)
            style.paragraph_format.keep_with_next = True

        for style_name in ["List Bullet", "List Number"]:
            style = styles[style_name]
            style.font.name = "Calibri"
            style.font.size = Pt(10.5)
            style.paragraph_format.left_indent = Inches(0.25)
            style.paragraph_format.first_line_indent = Inches(-0.18)
            style.paragraph_format.space_after = Pt(4)

        if "Table Caption" not in styles:
            caption = styles.add_style("Table Caption", WD_STYLE_TYPE.PARAGRAPH)
        else:
            caption = styles["Table Caption"]
        caption.font.name = "Calibri"
        caption.font.size = Pt(9)
        caption.font.bold = True
        caption.font.color.rgb = RGBColor.from_string(BLUE_DARK.lstrip("#"))
        caption.paragraph_format.space_before = Pt(6)
        caption.paragraph_format.space_after = Pt(3)
        caption.paragraph_format.keep_with_next = True

        if "Source Note" not in styles:
            source = styles.add_style("Source Note", WD_STYLE_TYPE.PARAGRAPH)
        else:
            source = styles["Source Note"]
        source.font.name = "Calibri"
        source.font.size = Pt(8)
        source.font.italic = True
        source.font.color.rgb = RGBColor.from_string(MUTED.lstrip("#"))
        source.paragraph_format.space_before = Pt(2)
        source.paragraph_format.space_after = Pt(6)

        header = section.header
        p = header.paragraphs[0]
        p.text = "SPARE CALIBRATION PROGRAM  |  TECHNICAL EVIDENCE REPORT"
        p.style = styles["Source Note"]
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer = section.footer
        p = footer.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("De La Salle University  •  18 August 2026  •  Page ")
        add_field(p, "PAGE")

        settings = self.doc.settings._element
        update = OxmlElement("w:updateFields")
        update.set(qn("w:val"), "true")
        settings.append(update)

    def cover(self) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(60)
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        r = p.add_run("SPARE")
        r.bold = True
        r.font.size = Pt(18)
        r.font.color.rgb = RGBColor.from_string(BLUE.lstrip("#"))
        self.doc.add_paragraph(
            "Calibration Program and Camera-Only Force Sensing System",
            style="Title",
        )
        self.doc.add_paragraph(
            "Comprehensive Technical Development, Data, Modeling, Validation, Use, and Limitations Report",
            style="Subtitle",
        )
        callout = self.doc.add_table(rows=1, cols=1)
        set_table_width(callout)
        shade_cell(callout.cell(0, 0), BLUE_LIGHT)
        cell_p = callout.cell(0, 0).paragraphs[0]
        cell_p.add_run(
            "Purpose: thesis-source dossier for transfer into ChatGPT Work. "
            "This report consolidates project-owned code, data products, dated decisions, "
            "model evidence, physical validation, and claim boundaries as of 18 August 2026."
        )
        cell_p.paragraph_format.space_after = Pt(0)
        self.doc.add_paragraph("")
        metadata = [
            ("Document status", "Technical evidence synthesis — share with explicit caveats"),
            ("Evidence cutoff", "18 August 2026 (Asia/Taipei)"),
            ("Primary software", "SPARE Camera and Load-Cell Calibration GUI, application/config schema 1.1.0"),
            ("Experimental live model", "live-sensor-experimental-hybrid-v4; validated claim not allowed"),
            ("Authoring basis", "Local workspace evidence; no external web sources used"),
        ]
        self._table_doc(["Field", "Value"], metadata, [2100, 7260], compact=False)
        self.doc.add_paragraph("")
        p = self.doc.add_paragraph()
        p.add_run("Prepared for thesis development and evidence traceability").bold = True
        p.add_run("\nDe La Salle University")
        p.add_run("\n18 August 2026")
        self.doc.add_page_break()
        self.md.extend(
            [
                "# SPARE Calibration Program and Camera-Only Force Sensing System",
                "",
                "## Comprehensive Technical Development, Data, Modeling, Validation, Use, and Limitations Report",
                "",
                "**Evidence cutoff:** 18 August 2026 (Asia/Taipei)",
                "",
                "**Document status:** Technical evidence synthesis — share with explicit caveats.",
                "",
            ]
        )

    def toc(self) -> None:
        self.doc.add_heading("Contents", level=1)
        p = self.doc.add_paragraph()
        add_field(p, 'TOC \\o "1-3" \\h \\z \\u')
        self.doc.add_paragraph(
            "Word should update this field automatically when the document opens. If it does not, select the table and choose Update Field.",
            style="Source Note",
        )
        self.doc.add_page_break()

    def heading(self, text: str, level: int = 1) -> None:
        self.doc.add_heading(text, level=level)
        self.md.extend(["#" * (level + 1) + " " + text, ""])

    def paragraph(self, text: str, *, bold_lead: str | None = None) -> None:
        p = self.doc.add_paragraph()
        if bold_lead and text.startswith(bold_lead):
            p.add_run(bold_lead).bold = True
            p.add_run(text[len(bold_lead):])
        else:
            p.add_run(text)
        self.md.extend([text, ""])

    def bullets(self, items: Sequence[str]) -> None:
        for item in items:
            self.doc.add_paragraph(item, style="List Bullet")
            self.md.append(f"- {item}")
        self.md.append("")

    def numbered(self, items: Sequence[str]) -> None:
        for item in items:
            self.doc.add_paragraph(item, style="List Number")
        for i, item in enumerate(items, 1):
            self.md.append(f"{i}. {item}")
        self.md.append("")

    def callout(self, title: str, text: str, kind: str = "info") -> None:
        fill = BLUE_LIGHT if kind == "info" else GOLD_LIGHT if kind == "caution" else RED_LIGHT
        table = self.doc.add_table(rows=1, cols=1)
        set_table_width(table)
        shade_cell(table.cell(0, 0), fill)
        p = table.cell(0, 0).paragraphs[0]
        r = p.add_run(title + ": ")
        r.bold = True
        p.add_run(text)
        p.paragraph_format.space_after = Pt(0)
        self.doc.add_paragraph("")
        self.md.extend([f"> **{title}:** {text}", ""])

    def _table_doc(self, headers: Sequence[str], rows: Sequence[Sequence[Any]], widths: Sequence[int] | None = None, compact: bool = True) -> None:
        table = self.doc.add_table(rows=1, cols=len(headers))
        table.style = "Table Grid"
        table.autofit = False
        set_table_width(table)
        set_repeat_table_header(table.rows[0])
        set_row_cant_split(table.rows[0])
        if widths is None:
            base = 9360 // len(headers)
            widths = [base] * len(headers)
        for idx, header in enumerate(headers):
            cell = table.cell(0, idx)
            set_cell_width(cell, widths[idx])
            shade_cell(cell, GRAY)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            run = p.add_run(str(header))
            run.bold = True
            run.font.size = Pt(8.5 if compact else 9.5)
        for row_values in rows:
            row = table.add_row()
            set_row_cant_split(row)
            for idx, value in enumerate(row_values):
                cell = row.cells[idx]
                set_cell_width(cell, widths[idx])
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
                p = cell.paragraphs[0]
                p.paragraph_format.space_after = Pt(0)
                run = p.add_run("" if value is None else str(value))
                run.font.size = Pt(7.8 if compact else 9)
        self.doc.add_paragraph("")

    def table(self, title: str, headers: Sequence[str], rows: Sequence[Sequence[Any]], *, widths: Sequence[int] | None = None, source: str | None = None, compact: bool = True) -> None:
        self.table_number += 1
        caption = f"Table {self.table_number}. {title}"
        self.doc.add_paragraph(caption, style="Table Caption")
        self._table_doc(headers, rows, widths, compact)
        if source:
            self.doc.add_paragraph("Source: " + source, style="Source Note")
        self.md.extend([f"**{caption}**", ""])
        self.md.append("| " + " | ".join(str(h) for h in headers) + " |")
        self.md.append("| " + " | ".join("---" for _ in headers) + " |")
        for row in rows:
            self.md.append("| " + " | ".join(str(v).replace("|", "\\|") for v in row) + " |")
        if source:
            self.md.extend(["", f"*Source: {source}*"])
        self.md.append("")

    def figure(self, path: Path, title: str, source: str, width: float = 6.55) -> None:
        self.figure_number += 1
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.keep_with_next = True
        p.add_run().add_picture(str(path), width=Inches(width))
        caption = f"Figure {self.figure_number}. {title}"
        cp = self.doc.add_paragraph(caption, style="Table Caption")
        cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sp = self.doc.add_paragraph("Source: " + source, style="Source Note")
        sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.md.extend([f"![{title}](charts/{path.name})", "", f"**{caption}**", "", f"*Source: {source}*", ""])

    def page_break(self) -> None:
        self.doc.add_page_break()

    def save(self) -> None:
        self.doc.core_properties.title = "SPARE Calibration Program and Camera-Only Force Sensing System — Full Technical Report"
        self.doc.core_properties.subject = "Thesis evidence dossier: development, use, data, localization, force estimation, validation, and limitations"
        self.doc.core_properties.author = "SPARE project technical evidence synthesis"
        self.doc.core_properties.keywords = "calibration, load cell, visuotactile, force estimation, localization, thesis, SPARE"
        self.doc.save(DOCX_PATH)
        MD_PATH.write_text("\n".join(self.md), encoding="utf-8")


report = Report()
report.cover()
report.toc()


# ---------------------------------------------------------------------------
# Technical report body
# ---------------------------------------------------------------------------

report.heading("Technical Summary", 1)
report.callout(
    "Bottom line",
    "The project has a physically exercised, integrity-oriented Windows acquisition and calibration program; a strong retrospective camera-only event-localization result; and a deliberately limited approximate force output. The software and synchronized physical acquisition are verified more strongly than the force-estimation claim. The v4 model is experimental, post-hoc, device-specific, and not eligible for a validated or deployment-grade force-sensor claim.",
    "caution",
)
report.paragraph(
    "The SPARE Camera and Load-Cell Calibration GUI is a standalone PySide6 application for collecting manual fixed-label presses or Ender 3-driven repeated presses from a nine-region sensing skin. It acquires unannotated camera frames, Arduino Nano/HX711 load-cell samples, and—when selected—independent Marlin printer motion. It performs per-pixel optical baseline correction, signed load-cell calibration, host-monotonic synchronization, structured export, graph generation, partial-file recovery, and optional live camera-only inference. [E01, E13-E22]"
)
report.paragraph(
    "The strongest present model result is discrete event-level localization: 93.43% macro F1 and 93.41% accuracy on 102 single-press events from 54 independent manual sessions, using six complete TEST-group outer folds. Every ROI's reported event recall is at least 90.28%. A selective output reaches 100% macro F1 at 82.93% coverage. These are retrospective post-hoc recovery results because the same six outer TEST groups had already been inspected during prior model development. [E04, E27-E30]"
)
report.paragraph(
    "The current force output is a 32-knot monotonic isotonic mapping from a filtered, normalized signed optical spatial-range score to an approximate force in a narrow nominal band of 1.7-3.0 N. The conditional MAE is 0.328 N and the cross-validated 95th-percentile absolute error is 0.643 N. Those headline errors do not establish force resolution: the model improves MAE over a session-balanced constant by only 0.013 N (3.81%) in the retained v2 mapping, the expanded per-fold search improves by 4.49%, mean Spearman rank correlation is only 0.207, and predicted spread is about 20.7% of true spread. Force is withheld outside fitted score support and when multiple rods are active. [E27-E30]"
)
report.paragraph(
    "The immutable feature-store reconciliation is internally strong: 245 sessions and 179,176 decoded frames were recovered exactly, with zero missing or unmatched video frames and unchanged archive hashes. The manual archive contributes 83 sessions/29,318 frames; the automatic archive contributes 162 sessions/149,858 frames. Governance is asymmetric: the manual archive controls application modeling and all general characterization metrics; the automatic archive is prohibited from application-model preprocessing, fitting, thresholds, ranges, selection, and evaluation, and is allowed only for hysteresis and creep-related exploratory characterization. [E02-E05, E23-E26]"
)
report.table(
    "Present system and evidence status",
    ["Area", "What is presently supported", "Evidence status", "Thesis-safe interpretation"],
    [
        ("Program acquisition/export", "Camera, load cell, synchronized rows, optional printer state, videos, CSV/JSON/PNG", "Implemented; physically exercised", "A research acquisition/calibration platform"),
        ("Load-cell calibration", "Signed 200 g factor, tare, SNR/CV gates, ±5% verification", "Physically verified on one fixture", "Reference-force acquisition was demonstrated on the tested setup"),
        ("Camera-only contact/event detection", "Warm-up-calibrated event state with two-frame debounce", "Post-hoc retrospective", "Promising experimental detector; not prospectively confirmed"),
        ("Discrete localization", "Nine ROI classes; simultaneous-rod representation in runtime", "Single-press post-hoc evidence", "Strong discrete localization within this dataset; multi-press accuracy unknown"),
        ("Force estimation", "Approximate isotonic force inside optical support", "Experimental; release blocked", "Error-bounded approximation, not validated force measurement or resolution"),
        ("Automatic repeated press", "State machine, safety checks, exports, simulation and read-only printer checks", "Software verified; full physical sequence pending", "Automation infrastructure exists, but physical repeated-press safety/performance is incomplete"),
        ("Sensor characterization", "Sensitivity, nonlinearity, repeatability, cross-talk, noise/drift, SNR, hysteresis, lag proxies", "Qualified by metric-specific boundaries", "Characterization evidence must be reported with its individual protocol limitations"),
    ],
    widths=[1500, 2600, 1900, 3360],
    source="Consolidated from [E01-E08, E23-E30].",
)
report.table(
    "Headline quantitative evidence",
    ["Metric", "Result", "Population/grain", "Qualification"],
    [
        ("Reconciled feature store", "245 sessions; 179,176 frames", "Independent session; decoded frame", "0 missing and 0 unmatched video frames"),
        ("Manual application corpus", "83 sessions; 29,318 frames", "54 primary + 11 no-contact + 18 replay", "Only 54 primary sessions enter six TEST-group model folds"),
        ("Event localization", "Macro F1 93.43%; accuracy 93.41%", "102 events; 54 independent sessions", "Post-hoc grouped outer-fold evidence"),
        ("Selective localization", "Macro F1 100%; coverage 82.93%", "Eligible single-press events", "Abstention trades coverage for accuracy"),
        ("Contact detection", "Minimum fold recall 94.20%; maximum no-contact frame FPR 2.44%", "Held-out TEST group and dedicated no-contact frames", "Post-hoc; zero false-contact episodes in this replay"),
        ("Approximate force", "Conditional MAE 0.328 N; p95 absolute error 0.643 N", "1.7-3.0 N; 4,622 frames/54 sessions in expanded benchmark", "Not resolution; not untouched validation"),
        ("Force tracking", "Spearman rho 0.207; predicted/true SD ratio 0.207", "Six outer folds", "Fails resolution/tracking gate"),
        ("Physical camera", "14.985 frames/s over 60 s", "901 DirectShow frames", "Nominal 30 FPS readback was not authoritative"),
        ("Physical load cell", "~10.89 samples/s; 0.493% verification error", "661 samples/60 s; one 200 g verification", "One tested fixture and placement"),
        ("Current automated tests", "490 passed in isolated file groups", "Current workspace on 18 Aug 2026", "Monolithic Windows Qt run can trigger pyqtgraph GC access violation"),
    ],
    widths=[1800, 1900, 2500, 3160],
    source="Recomputed/reconciled from [E05-E07, E23-E30, E35] and validation_checks.json.",
)


report.heading("1. Scope, Intended Use, and Evidence Hierarchy", 1)
report.heading("1.1 Scope of this dossier", 2)
report.paragraph(
    "This report covers the complete project-owned calibration program and its evidence chain: intended purpose; hardware and software architecture; development chronology; operator workflow; camera, load-cell, and printer handling; raw and derived data; feature extraction; synchronization; immutable feature-store construction; characterization metrics; historical and current modeling; validation; limitations; thesis-safe claims; and reproducibility. Installed Arduino tooling, the local virtual environment, caches, and other third-party runtime files are not treated as project evidence except where a measured environment version affects reproducibility."
)
report.paragraph(
    "The report is descriptive and diagnostic. It reports what was implemented and measured, why certain model and governance decisions were made, and which claims remain unsupported. It does not infer causal material behavior from observational calibration archives and does not treat post-hoc cross-validation as prospective confirmation."
)
report.heading("1.2 Evidence hierarchy", 2)
report.table(
    "Evidence hierarchy used throughout this report",
    ["Level", "Evidence type", "Examples", "Permitted use"],
    [
        ("A", "Physical verification", "Camera cadence, load-cell cadence/calibration, accepted synchronized session", "Support statements about the tested hardware setup and acquisition path"),
        ("B", "Immutable reconciled data", "Archive hashes/CRC, 245-session feature store, session-indexed tables", "Support corpus identity, completeness, and auditable derivation"),
        ("C", "Frozen/qualified characterization", "Manual-first metric tables and validation report", "Support metric-specific sensor observations with stated protocol boundaries"),
        ("D", "Post-hoc experimental model evidence", "v1-v4 recovery metrics and grouped folds", "Support feasibility and retrospective performance only"),
        ("E", "Historical/superseded analyses", "Earlier automatic/manual/global linear models", "Explain development evolution; not current application performance"),
        ("F", "Planned/unperformed gates", "Known-force live test, 60-minute soak, representative usability, physical multi-press", "Identify remaining work; no positive claim"),
    ],
    widths=[500, 1900, 3100, 3860],
    source="Evidence-policy synthesis of [E02-E08, E23-E34].",
)
report.callout(
    "Controlling rule",
    "When records disagree because the software evolved, the most recent dated evidence governs current state; earlier counts and schemas are retained as historical checkpoints. When a model metric conflicts with a release decision, the release decision controls the claim.",
)
report.heading("1.3 Repository condition and traceability limitation", 2)
report.paragraph(
    "The local Git repository has no committed history and the project directories are presently untracked. Consequently, this dossier reconstructs chronology from dated plans, decision logs, validation reports, run manifests, hashes, and artifacts—not from commit history. File modification times are not treated as authoritative development dates. This is a material traceability limitation for a thesis methods audit and should be corrected before future development by committing code, configuration, and decision records with tagged releases."
)


report.heading("2. System Objective, Boundaries, and Terminology", 1)
report.heading("2.1 Research objective", 2)
report.paragraph(
    "The program was built to acquire paired optical and mechanical-reference observations from a nine-location sensing skin, first to calibrate and characterize the device and then to explore whether a camera-only runtime could detect contact, localize the active region, and approximate force. The calibration GUI is therefore both an acquisition instrument and an experimental application host. Those roles must remain distinct: the load cell supplies offline reference labels and calibration evidence, whereas the deployed v4 inference path accepts camera-derived ROI features only."
)
report.heading("2.2 System boundary", 2)
report.table(
    "System inputs, internal products, and outputs",
    ["Boundary element", "Content", "Role"],
    [
        ("Physical inputs", "Arducam IMX179 frames; Nano/HX711 counts; optional Ender 3 position/commands; operator trial labels", "Acquisition"),
        ("Calibration state", "Nine-ROI layout, unloaded per-pixel V baseline, signed counts/g factor, tare, verification record", "Required context"),
        ("Frame-level derived data", "HSV statistics, signed/positive delta-V features, active fractions, centroids, legacy localization, synchronized force", "Analysis/export"),
        ("Characterization products", "Sensitivity, nonlinearity, repeatability, cross-talk, noise/drift, SNR/detection, hysteresis, lag and NEF proxies", "Sensor evidence"),
        ("Experimental live inference", "Contact/event state, one or more ROI labels, optical threshold ratio, optional approximate force", "Camera-only runtime"),
        ("Final artifacts", "Original/auxiliary videos; CSV/JSON; PNG graphs; status/logs; model reports", "Reproducibility and review"),
    ],
    widths=[1800, 4800, 2760],
    source="[E01, E09, E13-E22].",
)
report.heading("2.3 Key terminology", 2)
report.bullets(
    [
        "ROI means one of nine fixed rectangular regions in a 3×3 row-major layout. Localization is classification to these discrete regions, not continuous millimetre localization.",
        "Reference force means load-cell-derived force after signed calibration, tare, and synchronization. It is not a live input to the camera-only v4 model.",
        "Positive light means the sum or mean of max(current V − baseline median V, 0). Signed light retains darkening as negative values.",
        "Contact detection is the binary/state decision that an optical event is active. Event localization assigns the event to one or more ROI identities.",
        "Approximate force is the v4 isotonic output inside fitted optical-score support. It is not equivalent to validated measurement, sensor resolution, or certified force.",
        "Session is the independent acquisition unit. Frames and repeated cycles inside a session are repeated observations, not independent replicates.",
    ]
)


report.heading("3. Development History and Decision Record", 1)
report.paragraph(
    "Because the repository lacks commit history, the following chronology is reconstructed from dated project records and immutable run manifests. It explains how an acquisition program expanded into printer automation, formal characterization, and a disclosed experimental live sensor."
)
report.table(
    "Reconstructed development chronology",
    ["Date", "Development or decision", "Technical consequence"],
    [
        ("Before 3 Aug 2026", "Core GUI, simulation, baseline, load-cell calibration, synchronization, recorder, graph export, and engineering reviews implemented", "Architecture emphasized single hardware owners, bounded queues, exact schemas, partial artifacts, and fail-closed readiness"),
        ("3 Aug 2026", "Arducam/Nano/HX711 physical validation", "DirectShow retained at measured 14.985 FPS; ~10.89 Hz load-cell stream; 200 g calibration and one accepted synchronized session demonstrated"),
        ("4 Aug 2026", "Ender 3 repeated-press software integration", "Independent printer serial owner, motion state machine, press-zero semantics, bounds, force limit, logging, and simulation exports added"),
        ("6-8 Aug 2026", "Automatic and manual calibration archives collected", "162 automatic sessions plus 83 manual sessions; later governed as different evidence roles"),
        ("13 Aug 2026", "Application modeling and characterization formally separated", "Automatic archive forbidden for application model; manual-first characterization and immutable feature store frozen"),
        ("13 Aug 2026", "Authoritative preprocessing/range gate failed", "No confirmatory candidate comparison was eligible under the original plan"),
        ("13-16 Aug 2026", "Disclosed recovery v1-v3", "v1 restored retrospective metrics; v2 fixed runtime parity and exposed failed force-resolution behavior; v3 emphasized event signal and withheld Newton force"),
        ("17 Aug 2026", "Hybrid v4 assembled", "v3 event detection/localization combined with v2 isotonic approximate force; expanded family comparison did not justify replacement"),
        ("18 Aug 2026", "Current audit", "490 tests pass in isolated file groups; exact schema now contains 812 scoped data-dictionary rows; validated release remains blocked"),
    ],
    widths=[1150, 3600, 4610],
    source="[E04-E08, E23-E30, E35].",
)
report.heading("3.1 Why the original application plan stopped", 2)
report.paragraph(
    "The original plan required a training-only contact threshold that simultaneously achieved at least 90% contact recall and no more than 5% no-contact false-positive rate, plus a common all-ROI force interval supported by session counts and monotonic-response rules. The frozen manual-only gate failed before confirmatory model comparison: the best development recall under the no-contact constraint was 85.16%; several required low-force bins were absent; ROI 5 had a nonpositive initial slope; and ROI 7's supported interval ended near 0.5625 N. The correct governance response was to stop the confirmatory path rather than optimize through the held-out data. [E02, E04]"
)
report.heading("3.2 Recovery models and permanent disclosure", 2)
report.paragraph(
    "Because additional data could not be collected, later work was explicitly labeled post-hoc recovery. v1 used a nine-frame signed-light spatial-range signal, unloaded quantiles, isotonic force, and active-fraction argmax. v2 replayed complete sequences with contact debounce, two-frame ROI switching, and a selective confidence threshold; its strong MAE coexisted with weak force variation tracking. v3 therefore made a unitless event signal the primary output and removed Newton force. v4 restored the v2 approximate force alongside the stronger v3 event/localization path, while retaining the release blockers in machine-readable form. No recovery version can become untouched confirmation evidence without a new prospectively frozen dataset. [E04, E12, E27-E30]"
)


report.heading("4. Hardware and Software Architecture", 1)
report.heading("4.1 Tested hardware configuration", 2)
report.table(
    "Hardware components and tested identities",
    ["Component", "Tested identity/configuration", "Interface", "Status"],
    [
        ("Camera", "Arducam IMX179 Camera Module; VID:PID 1BCF:0B12", "USB/OpenCV DirectShow, index 0", "Physically verified"),
        ("Load-cell controller", "Arduino Nano / CH340; VID:PID 1A86:7523", "COM3, 115200 baud", "Physically verified"),
        ("ADC/load cell", "HX711 on Nano D4/D5; fixture-aware 200 g calibration", "Fresh-conversion ASCII DATA rows", "Physically verified on one fixture"),
        ("Motion platform", "Creality Ender 3 V2 Neo; Marlin 1.1.6; EMERGENCY_PARSER:0", "Independent COM4, 115200 baud", "Read-only and small manual moves verified; repeated press not physically validated"),
        ("Host", "Windows 10.0.26200 / Windows 11 build string in later analysis manifests", "PySide6 application", "Project environment"),
    ],
    widths=[1500, 3300, 2200, 2360],
    source="[E06, E07, E23].",
)
report.heading("4.2 Layered software design", 2)
report.table(
    "Project-owned software layers",
    ["Layer/path", "Primary responsibilities", "Important design properties"],
    [
        ("app.py", "Application entry point and simulation switch", "Launches one desktop process"),
        ("core/", "Configuration models, lifecycle/readiness, logging, model-bundle contracts, live inference", "Dataclasses, explicit validity, state transitions, hash/schema checks"),
        ("services/", "Camera, load-cell serial, printer serial, repeated-press controller, Qt workers, simulation, recorder", "One hardware owner per device; bounded queues; generation IDs reject stale callbacks"),
        ("processing/", "Orientation, ROI, baseline, HSV features, motion magnification, legacy localization, load-cell calibration, synchronization", "Deterministic non-Qt functions shared by physical and simulation paths"),
        ("data/", "Canonical schemas, incremental CSV writers, synchronized finalization, graph export", "Exact column order, missing-value rules, atomic publication"),
        ("gui/", "Nine workflow tabs, dual video, controls, plots, readiness, logs", "Newest-only preview buffer is independent of analytical/recording queues"),
        ("arduino/", "HX711 firmware", "Reads only when ready; emits one ID/timestamp/raw value per fresh conversion"),
        ("scripts/ and analysis_outputs/", "Dataset construction, characterization, modeling, replay, performance and audit", "Run manifests, hashes, grouped validation, immutable outputs"),
        ("tests/", "Hardware-free unit, integration, simulation, GUI-offscreen, contract and reliability tests", "490 pass when isolated in current audit"),
    ],
    widths=[1700, 4200, 3460],
    source="[E01, E08, E13-E22, E35].",
)
report.heading("4.3 Concurrency and ownership", 2)
report.paragraph(
    "Camera acquisition, optical analysis, motion magnification, serial load-cell acquisition, printer control, recording, and graph export operate through separate owners/workers. Preview uses a newest-only buffer so a slow UI does not back up the camera. Recording uses a bounded loss-detecting queue, while graph writes are asynchronous. The load-cell COM port and printer COM port are intentionally independent. Hardware callbacks carry transaction/generation identities so callbacks from a cancelled or superseded operation cannot mutate current state. [E08, E21]"
)
report.heading("4.4 Fail-closed scientific states", 2)
report.paragraph(
    "The live path defines STARTUP, SETUP_BLOCKED, BASELINE_CAPTURING, READY, LIVE_NO_CONTACT, LIVE_CONTACT, LIVE_UNCERTAIN, LIVE_LIMIT, PAUSED, RECONNECTING, and ERROR transitions. Exact localization is permitted only in contact state; incompatible layout, missing baseline, stale/non-finite features, out-of-support force scores, or model-bundle integrity errors withhold output. The experimental bundle loader enforces an allowlist, regular-file and size constraints, per-file SHA-256, a complete SHA256SUMS list, monotonic force knots, the exact feature order, and the explicit experimental release decision. [E18, E19, E28-E30]"
)


report.heading("5. Installation, Setup, and Operator Use Process", 1)
report.heading("5.1 Software installation and launch", 2)
report.numbered(
    [
        "Install 64-bit Python 3.11 or later on Windows and create a virtual environment with `py -3.11 -m venv .venv`.",
        "Install dependencies with `.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt` and verify them with `python -m pip check`.",
        "Launch hardware mode with `.\\.venv\\Scripts\\python.exe app.py`, or launch clearly labeled simulation mode with `.\\.venv\\Scripts\\python.exe app.py --simulation`.",
        "Use the Windows helpers `setup_windows.bat` and `run_windows.bat` when a guided batch workflow is preferred.",
    ]
)
report.paragraph(
    "The current GUI exposes Camera View; ROI and Baseline Settings; Image and HSV Processing; Motion Magnification; Load Cell and Calibration; Recording and Synchronization; Printer Motion; Graph and Export Settings; and System Status and Logs. The original display is an unannotated copy. ROI overlays and processed views are separate display/output copies and do not alter the authoritative original frame. [E01]"
)
report.heading("5.2 Arduino/HX711 setup", 2)
report.numbered(
    [
        "Install an HX711 Arduino library exposing `begin()`, `is_ready()`, and `read()`; open `arduino/hx711_nano_stream/hx711_nano_stream.ino`.",
        "With USB power disconnected, wire HX711 DOUT to Nano D4, SCK to D5, module-rated VCC, and common GND. Connect the load cell to E+/E− and A+/A− according to its datasheet; wire color is not standardized.",
        "Upload for Arduino Nano, select the correct processor/bootloader and COM port, then close Arduino Serial Monitor before GUI connection.",
        "Select the same port at 115200 baud in the GUI. A physical connection is accepted only after finite numeric readings arrive; HELLO identity alone is insufficient.",
    ]
)
report.paragraph(
    "The supplied firmware streams immediately. The canonical line is `DATA,sample_id,arduino_micros,raw_adc`; optional commands are PING, START, STOP, and STATUS. The host also supports plain numeric and several compatibility formats. The firmware calls `read()` only after `is_ready()`, preventing stale cached conversions from being re-emitted. Midstream HELLO/sample-ID/micros resets create a new device-session identity without resetting the host trial clock. [E01, E22]"
)
report.heading("5.3 Camera setup and orientation", 2)
report.numbered(
    [
        "Connect the Arducam, refresh camera indices, and confirm the device through preview and Windows Device Manager; an index alone is not identity evidence.",
        "Use DirectShow first on the verified unit. Connect, confirm dimensions and cadence, and open the native Windows camera properties dialog on the active capture handle when exposure/focus/white balance/gain need adjustment.",
        "Apply software rotation first and optional horizontal mirroring second. Any change invalidates the optical baseline and may swap the ROI frame dimensions.",
        "Treat measured capture cadence as authoritative. The tested DirectShow stream was about 14.985 FPS despite a nominal 30 FPS property.",
    ]
)
report.heading("5.4 Nine-ROI definition and unloaded baseline", 2)
report.numbered(
    [
        "Define exactly nine non-overlapping, in-bounds rectangles in row-major order and save the layout if it will be reused.",
        "Ensure the sensing skin is unloaded and camera settings are stable. Allow the normal camera warm-up.",
        "Capture a baseline for the configured duration (default 3 s) with at least 20 valid frames.",
        "Review and accept the baseline. The record binds camera device/backend/resolution/settings, processing settings, orientation, and ROI-layout identity.",
        "Before recording, perform the unloaded drift check. The default acceptance criterion is mean absolute difference across the nine ROI mean-V values no greater than 5 V levels.",
    ]
)
report.paragraph(
    "For each ROI, baseline capture converts every BGR frame to HSV and stores a per-pixel median V image plus a per-pixel mean V image and ROI-level H/S/V summaries. Any camera, orientation, processing, or ROI-layout change invalidates the record. [E09, E14]"
)
report.heading("5.5 Signed 200 g load-cell calibration", 2)
report.numbered(
    [
        "Collect an unloaded raw-count window and a loaded window using the configured known mass (default 200 g). Each normally requires at least 20 samples; 10 is a documented fallback only when measured cadence cannot supply 20 in the window.",
        "Compute the signed factor `(loaded mean − unloaded mean) / known mass`. The sign is retained so either electrical polarity produces a positive force when the same loading direction is applied.",
        "Accept only if calibration SNR is at least 10, the loaded-window force CV is at most 2%, and the span is numerically nonzero.",
        "Tare with a stable unloaded window; the default maximum raw standard deviation is 100 counts.",
        "Reapply the known mass and require verification error no greater than ±5%. Persist calibration identity, device/firmware/port provenance, windows, tare, and verification.",
    ]
)
report.heading("5.6 Manual recording workflow", 2)
report.numbered(
    [
        "Enter trial ID, sensing-skin ID, target ROI ground truth, optional specimen/participant ID, optional interaction/force class, press number, and notes. These are fixed trial labels, not frame-level contact labels.",
        "Choose a new writable output root. The preflight checks path identity, writeability, free space, and non-overwrite behavior.",
        "Confirm camera, exact ROI layout, accepted baseline, current drift, load-cell connection/calibration/verification, labels, output destination, and recorder/worker readiness.",
        "Select the original video (required) and any optional overlay, processed, or motion-magnified streams. The authoritative quantitative source remains unannotated oriented pixels for the live application model.",
        "Start recording, perform the single intended press, stop recording, and review the generated integrity/status summary and plots.",
    ]
)
report.heading("5.7 Repeated-press workflow", 2)
report.paragraph(
    "Printer setup begins with M115 identity, explicit G28 homing, M114 position readback, and bounds checks. Press Zero is stored in host memory and must never be implemented by G92. The target Z is `press_zero_z_mm + signed_displacement_mm`; on the tested machine, negative displacement moves downward toward the specimen. A sequence proceeds through pre-roll, down motion, hold, up motion, inter-cycle dwell, repeated cycles, and post-roll. Command lifecycle, reported position, phase, cycle identity, and force-safety state are exported with frame and load-cell rows. [E01, E07]"
)
report.callout(
    "Safety boundary",
    "The force limit is a secondary software safeguard that can issue M410 when calibrated force exceeds a threshold. The tested Marlin firmware reports EMERGENCY_PARSER:0, and physical abort latency and a full repeated-press sequence were not validated. The program is not a certified machine-safety interlock; operator supervision, conservative motion bounds, mechanical clearance, and an accessible power-off path remain mandatory.",
    "danger",
)


report.heading("6. Acquisition, Recording, and Exported Data", 1)
report.heading("6.1 Authoritative clocks and identities", 2)
report.paragraph(
    "Every accepted frame receives a permanent zero-based capture_frame_id, a host_monotonic_ns timestamp captured at acquisition, elapsed time relative to the recording start, and a human-readable wall clock. Every fresh HX711 conversion carries an Arduino sample ID, raw 32-bit micros timestamp, unwrapped micros value, host receipt monotonic timestamp, and a device-session identity. Host monotonic time is the authoritative cross-device clock because the camera and Arduino do not share a trigger. [E16, E20-E22]"
)
report.heading("6.2 Incremental recording and integrity", 2)
report.paragraph(
    "The recorder writes `.partial` CSV/video artifacts incrementally and flushes by time and row count. Finalization is transactional: the recorder closes streams, validates exact schemas and row identities, synchronizes frames to load-cell samples, confirms video/frame/feature counts, writes status and metadata, and atomically publishes final names. If interrupted, partial recovery is fail-closed and reports why a session cannot be accepted. Original frames are never dropped silently; analytical failure produces a row with preserved identity/geometry, NaN derived values, and an error code. [E20, E21]"
)
report.heading("6.3 Standard session artifact set", 2)
report.table(
    "Session files and scientific purpose",
    ["Artifact", "Content", "Authority/notes"],
    [
        ("session_video.*", "Unannotated oriented original frames", "Only quantitative image source used by the immutable feature-store builder"),
        ("auxiliary videos", "Optional ROI overlay, processed view, motion-magnified stream", "Visualization/review only unless an explicitly separate analysis declares otherwise"),
        ("frame_features.csv", "One row per accepted frame; camera/ROI/feature/localization/provenance fields", "Current exact width: 366 columns"),
        ("loadcell_raw.csv", "One row per parsed physical sample; raw, calibrated, device-session, and motion fields", "Current exact width: 46 columns"),
        ("master_synchronized.csv", "Frame row plus synchronized load-cell fields", "Current exact width: 385 columns"),
        ("data_dictionary.csv", "Artifact-scoped column definitions, units, types, value roles, missing behavior", "Current generated width: 812 dictionary rows across five schema scopes"),
        ("session_config / camera / ROI / calibration / baseline files", "Acquisition context and immutable identifiers", "Required for provenance and replay"),
        ("session_status and logs", "Completion/abort, counts, errors, performance, identity checks", "Acceptance evidence"),
        ("spatial_graphs and graph companions", "Heatmap, temporal line, and spatial-profile PNG/CSV/JSON bundles", "Graph JSON controls metric meaning and units"),
        ("printer_sequence files", "Commands, responses, cycle/phase events, position, safety status", "Present only for automated sequences"),
    ],
    widths=[1900, 4000, 3460],
    source="[E01, E20, E21] and current_schema_dictionary.csv.",
)
report.heading("6.4 Current schema groups", 2)
report.table(
    "Current canonical schema composition",
    ["Group", "Representative fields", "Scientific role"],
    [
        ("Trial/session", "session_id, trial_id, skin/specimen ID, target ROI, optional classes/notes", "Ground truth and metadata fixed at trial grain"),
        ("Frame identity", "capture_frame_id, monotonic/wall clocks, dimensions/FPS/backend, driver readbacks", "Raw identity and camera provenance"),
        ("Motion provenance", "sequence/cycle/phase/command, zero/targets/positions, force-limit flags", "Automated-sequence context"),
        ("Load-cell synchronization", "nearest sample, interpolated raw, force, method/gap/offset/validity", "Frame-aligned mechanical reference"),
        ("Per-ROI optical", "33 fields × 9 ROI: geometry, raw HSV, signed and positive delta, activity, centroids, normalized intensity", "Raw-derived optical features"),
        ("Legacy localization", "dominant ROI, top ratios, weighted coordinates, confidence", "Frame-level deterministic diagnostic"),
        ("Validity/provenance", "frame/baseline/load-cell validity, error, versions, calibration/baseline/layout IDs", "Fail-closed interpretation and reproducibility"),
    ],
    widths=[1800, 4500, 3060],
    source="[E20] and current_schema_dictionary.csv.",
)
report.paragraph(
    "Earlier implementation reports cite 647, 734, or other data-dictionary row counts. Those values were correct at their dated checkpoints. The current code generates 812 artifact-scoped rows because later versions added printer-motion fields, richer optical signed features, graph-companion scopes, and artifact-specific definitions for same-named fields. The complete current dictionary is delivered beside this report rather than reproduced as an 812-row appendix."
)


report.heading("7. Optical, Mechanical, and Synchronization Processing", 1)
report.heading("7.1 Canonical image transform and ROI geometry", 2)
report.paragraph(
    "The authoritative live layout expects raw 640×480 input, then a 90° clockwise rotation followed by horizontal mirroring, producing a 480×640 analytical frame. Its fixed identity is `roi-9fbc67c50ee3bc7145fa`. Saved 480×640 originals are not transformed a second time. A raw 640×480 input is transformed exactly once. Any orientation/layout mismatch blocks the live bundle. [E02, E19, E23]"
)
report.table(
    "Canonical live ROI rectangles on the 480×640 oriented frame",
    ["ROI", "x", "y", "width", "height", "area (px²)"],
    [
        (1, 98, 161, 82, 89, 82 * 89),
        (2, 197, 156, 76, 99, 76 * 99),
        (3, 312, 154, 58, 97, 58 * 97),
        (4, 100, 268, 73, 89, 73 * 89),
        (5, 204, 264, 76, 96, 76 * 96),
        (6, 314, 260, 66, 94, 66 * 94),
        (7, 104, 383, 73, 77, 73 * 77),
        (8, 194, 394, 87, 66, 87 * 66),
        (9, 312, 379, 61, 77, 61 * 77),
    ],
    widths=[1000, 1200, 1200, 1500, 1500, 2960],
    source="[E02, E19].",
)
report.heading("7.2 Per-pixel optical baseline and feature formulas", 2)
report.paragraph(
    "For every frame, unannotated BGR is converted to OpenCV HSV exactly once. Within each ROI, the saved per-pixel baseline median V is subtracted in signed 16-bit arithmetic: `signed_delta(x,y,t) = V(x,y,t) − median_baseline_V(x,y)`. The positive response is `positive_delta = max(signed_delta, 0)`. The implementation preserves both signed and positive summaries because darkening contains information even though legacy light-only features clip it. [E13, E14]"
)
report.table(
    "Per-ROI optical feature families",
    ["Family", "Fields/formula", "Use"],
    [
        ("Raw HSV", "circular mean H above saturation gate; mean S; mean/median/max/p95/std V", "Camera/material state and saturation review"),
        ("Signed response", "sum, mean, median, MAD of current V − baseline median V", "Primary v4 contact/force input uses signed sum"),
        ("Positive response", "mean, max, p95, sum, sum/area, std of max(delta,0)", "Characterization and legacy localization"),
        ("Active pixels", "count and fraction where positive delta ≥ configured threshold (default 10 V)", "v4 localization and independent rod gates"),
        ("Spatial centroid", "positive-delta weighted local and full-frame x/y", "Spatial diagnostic"),
        ("Normalized intensity", "ROI mean positive delta divided by sum over nine ROIs", "Legacy deterministic localization"),
    ],
    widths=[1700, 4700, 2960],
    source="[E09, E13, E20].",
)
report.heading("7.3 Legacy frame-level deterministic localization", 2)
report.paragraph(
    "The general processing pipeline computes a legacy diagnostic localization from the nine ROI mean positive delta-V values. The ROI with the largest mean is returned only if it reaches the configured minimum (default 5 V). Confidence is dominant/total, with additional top-one/top-two ratio and intensity-weighted full-frame coordinates. Non-finite features make the result unavailable; invalid values are not silently replaced by zero. This legacy diagnostic is not the v4 event-localization model. [E13]"
)
report.heading("7.4 Load-cell equations", 2)
report.paragraph(
    "Let U be the unloaded raw mean, L the loaded raw mean, and M the known mass in grams. The signed factor is `c = (L − U)/M` counts/g. With tare T and raw sample R, `force_gf = (R − T)/c` and `force_N = force_gf × 0.00980665`. Calibration SNR is `|L − U| / max(population_std_unloaded, population_std_loaded)`. Loaded-window CV is the population standard deviation of converted loaded grams-force divided by its absolute mean, times 100%. Verification is the absolute mean-force error divided by expected grams-force, times 100%. [E15]"
)
report.heading("7.5 Online frame/load-cell synchronization", 2)
report.paragraph(
    "A frame is first matched to an exact host-monotonic sample if available. Otherwise, linear interpolation is used only when two samples bracket the frame, both gaps are within the configured maximum (default 200 ms), the device-session identity is unchanged, and timestamps increase. If interpolation is unavailable, the nearest sample is used only within the same maximum gap; ties prefer the earlier physical sample. When no valid match exists, force fields are NaN and synchronization_valid is false, but the frame row is retained. Contact state is derived from synchronized force using the configured 0.05 N threshold, never from trial labels. [E09, E16]"
)
report.heading("7.6 Offline model-label alignment", 2)
report.paragraph(
    "Model fitting uses a separate session-safe alignment contract. Positive lag pairs an optical observation at time t with reference force at `t − lag`. Reference timestamps are reduced by median when duplicated, interpolation never crosses sessions, no extrapolation is allowed, and a bracketing gap above 300 ms produces a missing label. The six outer-fold lags were 260, 220, 240, 240, 240, and 240 ms; the all-development value was 240 ms. These lags are preprocessing hyperparameters fitted within training partitions, not intrinsic material response times. [E11, E17, E27]"
)
report.heading("7.7 Motion magnification", 2)
report.paragraph(
    "Eulerian color/intensity motion magnification is optional and off by default. It executes in an independent drop-stale worker and may feed visualization or explicitly requested auxiliary streams. For the authoritative live application model, quantitative features come from the original unannotated oriented frame; the load cell and printer are forbidden runtime inputs, and motion-magnified output is not substituted into the v4 camera feature contract. [E01, E02]"
)


report.heading("8. Data Sources, Extraction, Reconciliation, and Governance", 1)
report.heading("8.1 Source archives and permitted roles", 2)
manual_inventory = char_run["inputs"]["archive_inventory"]["manual"]
auto_inventory = char_run["inputs"]["archive_inventory"]["automated"]
report.table(
    "Immutable archive inventory",
    ["Archive", "Size / identity", "Sessions / frames", "Permitted role"],
    [
        (
            "Manual Calibration (2).zip",
            f"{manual_inventory['archive_size_bytes'] / (1024**3):.2f} GiB; SHA-256 {manual_inventory['actual_sha256']}",
            "83 sessions; 29,318 frames",
            "Only application-model archive; controlling source for all general characterization metrics",
        ),
        (
            "Auto Calibration.zip",
            f"{auto_inventory['archive_size_bytes'] / (1024**3):.2f} GiB; SHA-256 {auto_inventory['actual_sha256']}",
            "162 sessions; 149,858 frames",
            "Characterization only: hysteresis and creep-related exploratory analysis",
        ),
    ],
    widths=[2100, 3000, 1700, 2560],
    source="[E03, E23-E25]. Full hashes are retained to prevent archive substitution.",
)
report.heading("8.2 Manual archive design", 2)
report.table(
    "Manual archive roles",
    ["Role", "Sessions", "Design", "Use"],
    [
        ("Primary model/characterization", 54, "Six complete TEST sessions for each ROI 1-9", "Outer-fold application modeling and general press characterization"),
        ("Dedicated no-contact", 11, "Short unloaded sessions; all nine ROIs observed", "No-contact false positives, noise, drift, threshold evidence"),
        ("Replay-only", 18, "TEST1_v2 and other recovery/replay sessions", "Runtime behavior and combined independent ROI gate; not primary fitting"),
        ("Total", 83, "29,318 frames", "Immutable manual corpus"),
    ],
    widths=[2100, 1000, 3400, 2860],
    source="[E02-E05, E23-E26].",
)
report.paragraph(
    "The confirmatory-style split is six outer folds, each holding out one complete TEST number across all nine ROI. Every preprocessing choice—including no-contact floor, scale, lag, contact threshold, support/range, selective threshold, and model hyperparameters—must be fitted within the outer training partition. Session is the independence grain. This structure prevents frame leakage across folds, but the subsequent recovery work remains post-hoc because the outer-fold results had already been inspected. [E02, E04, E27]"
)
report.heading("8.3 Automatic archive design", 2)
report.paragraph(
    "The automatic archive contains 18 sessions per ROI: three absolute displacements (1.75, 2.625, and 3.5 mm) × three speeds (200, 400, and 600 mm/min) × one nominal 20-cycle session and one nominal 10-cycle session. Thus each exact ROI × displacement × speed condition has two independent sessions, collected across two days. Cycles are nested repeated observations inside session and must not be counted as independent replicates. [E03, E24-E26]"
)
report.callout(
    "Automatic-data firewall",
    "Automatic sessions cannot supply application-model preprocessing, training labels, thresholds, support/range, model selection, or evaluation. They may support hysteresis, speed-stratified hysteresis, internal cycle QA, short fixed-displacement settling, and unloaded-recovery observations only. This firewall is enforced in configuration/tests and is a central thesis-methods requirement.",
    "caution",
)
report.heading("8.4 Feature-store extraction", 2)
report.numbered(
    [
        "Audit each ZIP archive by expected SHA-256, entry count, session count, and full member CRC.",
        "For each session, read only the authoritative session_video original stream for quantitative pixels; do not read overlays, processed video, or motion-magnified video.",
        "Reconcile every decoded frame to saved frame/master rows and exact session identity. Preserve reason-coded differences instead of silently dropping rows.",
        "Apply the canonical orientation exactly once; saved 480×640 originals are not reoriented again.",
        "Extract the fixed manual-only feature specification and retain raw/reference/corrected values, validity, phase, labels, layout, calibration, baseline, and source hashes.",
        "For automatic sessions only, calculate a cycle-local force correction from the median synchronized unloaded dwell immediately before each cycle while preserving raw force.",
        "Write one Parquet file per session, a session index, event log, reconciliation report, and run manifest. Do not store global fitted floors, scales, thresholds, Fdetect, Fmax, imputation, or model transforms in the immutable store.",
    ]
)
report.table(
    "Feature-store reconciliation checks",
    ["Check", "Result"],
    [
        ("Archive hashes before vs. after", "Unchanged for manual and automatic archives"),
        ("Sessions checkpointed", "245 of 245"),
        ("Rows/decoded frames", "179,176 of 179,176"),
        ("Missing video frames", "0"),
        ("Unmatched video frames", "0"),
        ("Canonical layout only", "Passed"),
        ("Frame differences reason-coded", "Passed"),
        ("Global fitted values absent", "Passed"),
        ("Build time/environment", "82.51 s; CPython 3.12.13; NumPy 2.5.1; pandas 3.0.5; OpenCV 4.14.0.94"),
    ],
    widths=[3300, 6060],
    source="[E23].",
)
report.heading("8.5 Data-quality findings", 2)
report.paragraph(
    "The authoritative feature-store and characterization products pass their own integrity/validation gates, but data quality is not equivalent to claim validity. The main risks are design limitations: only six primary sessions per ROI; exactly two independent automatic sessions per exact cyclic condition; two automatic collection days; short no-contact durations; zero labeled simultaneous-press events; narrow force recovery support; and no untouched confirmation set after model recovery. Four automatic sessions contribute one fewer usable cycle than nominal to cycle-bin summaries; this is disclosed and does not invalidate the session-level archive. [E24, E25]"
)


report.heading("9. Sensor Characterization Results", 1)
report.paragraph(
    "Characterization is governed metric by metric. Manual sessions control every general sensor-property claim. Automatic sessions contribute only conditional hysteresis and creep-related exploratory evidence. The validated characterization report classifies the package as ready to share with stated qualification boundaries, not as a universal device specification. [E03, E24-E26]"
)
report.table(
    "Characterization metric-status inventory",
    ["Status", "Metrics"],
    [
        ("Primary", "Archive integrity/reconciliation; low-force and local sensitivity; nonlinearity; between-session repeatability; spatial uniformity; cross-talk; no-contact noise/drift"),
        ("Conditional-qualified", "Plateau observations; SNR/detection; hysteresis; hysteresis cycle/correction QA; noise-equivalent-force proxy"),
        ("Exploratory-insufficient", "Short fixed-displacement settling/recovery; approximate standalone system lag"),
        ("Not established", "Historical combined common range/monotonicity; true creep; true force resolution"),
    ],
    widths=[2000, 7360],
    source="metric_status.csv [E26].",
)
report.heading("9.1 Static response, sensitivity, and nonlinearity", 2)
report.figure(
    charts["sensitivity"],
    "Manual session-balanced low-force sensitivity by ROI",
    "manual_sensitivity_nonlinearity.csv [E26]. Values are characterization slopes, not the v4 force-model coefficient.",
)
sensitivity_rows = []
for _, row in sensitivity.sort_values("roi").iterrows():
    nonlin = row.get("nonlinearity_percent_observed_span")
    sensitivity_rows.append(
        (
            int(row["roi"]),
            f"{row['low_force_sensitivity_light_per_N']:.3f}",
            f"{row['low_force_sensitivity_light_per_pixel_per_N']:.5f}",
            "—" if pd.isna(nonlin) else f"{nonlin:.2f}%",
            "positive" if row["low_force_sensitivity_light_per_N"] > 0 else "zero/negative",
        )
    )
report.table(
    "Manual low-force sensitivity and observed nonlinearity",
    ["ROI", "Integrated light/N", "Light/pixel/N", "Nonlinearity", "Slope interpretation"],
    sensitivity_rows,
    widths=[700, 2000, 2000, 1700, 2960],
    source="manual_sensitivity_nonlinearity.csv [E26]. Nonlinearity is reported only where the fitted slope supports the calculation.",
)
report.paragraph(
    "Only ROIs 1, 4, 8, and 9 show positive low-force slopes under the frozen rule; ROI 3 is negative and ROIs 2, 5, 6, and 7 are zero after the positive-slope qualification logic. Where nonlinearity is calculable, maximum deviation spans roughly 37.7-70.1% of the observed light range. This strong spatial heterogeneity blocks a single uniform optical-force calibration and helps explain why global force models perform weakly. [E26]"
)
report.heading("9.2 Between-session repeatability and plateau observations", 2)
report.paragraph(
    f"Manual repeatability is summarized at ROI × 0.25 N force-bin grain using independent sessions. Across {len(repeatability)} supported bin summaries, the median between-session standard deviation is {repeatability['between_session_sd_percent_observed_span'].median():.2f}% of observed light span and the 95th percentile is {repeatability['between_session_sd_percent_observed_span'].quantile(.95):.2f}%. This is a descriptive stability measure, not an inference interval. The frozen sustained-plateau rule observed no plateau in any ROI. The reported supported-force extents are observation ranges only, not a common deployment range. [E26]"
)
report.heading("9.3 Spatial cross-talk", 2)
report.figure(
    charts["cross_talk"],
    "Session-balanced single-ROI press response distribution",
    "cross_talk.csv [E26]. Cell medians are calculated independently and therefore a row of medians need not sum exactly to one.",
    width=6.2,
)
diag = cross_talk[cross_talk["target_roi"] == cross_talk["measured_roi"]]
off = cross_talk[cross_talk["target_roi"] != cross_talk["measured_roi"]]
report.paragraph(
    f"The median target-channel share across the nine diagonal cells is {diag['median_share_total'].median():.3f}; diagonal values range from {diag['median_share_total'].min():.3f} to {diag['median_share_total'].max():.3f}. The largest off-target median share is {off['median_share_total'].max():.3f}. Several targets distribute response across neighboring or non-target ROIs, so localization is not simply equivalent to the largest raw positive-light channel. The strong v3/v4 event result depends on normalized activity, temporal accumulation, and the labeled corpus rather than perfect optical isolation."
)
report.heading("9.4 No-contact noise and drift", 2)
report.figure(
    charts["noise_drift"],
    "Dedicated no-contact signed-light noise and short-term drift",
    "no_contact_summary.csv [E26]; 11 independent dedicated no-contact sessions.",
)
noise_rows = []
for _, row in no_contact.sort_values("roi").iterrows():
    noise_rows.append(
        (
            int(row["roi"]),
            f"{row['signed_noise_mad']:.1f}",
            f"{row['median_abs_drift_light_per_min']:.1f}",
            f"{row['max_abs_drift_light_per_min']:.1f}",
        )
    )
report.table(
    "No-contact stability by ROI",
    ["ROI", "Signed-light MAD", "Median |drift|/min", "Maximum |drift|/min"],
    noise_rows,
    widths=[900, 2200, 2900, 3360],
    source="no_contact_summary.csv [E26].",
)
report.paragraph(
    "Signed-light MAD ranges from 352.0 to 538.5. Median absolute drift ranges from 590.3 to 1,459.4 light units/min, and the largest observed session-level drift is about 4,184.5 light units/min at ROI 5. These are short-term recorded-session properties. They motivate the mandatory unloaded baseline, drift gate, live warm-up quantile, per-ROI normalization, and out-of-support withholding."
)
report.heading("9.5 SNR, detection threshold, and noise-equivalent-force proxy", 2)
snr_rows = []
for _, row in snr.sort_values("roi").iterrows():
    db_text = f"{row['snr_dB_amplitude_convention']:.2f} dB" if math.isfinite(row["snr_dB_amplitude_convention"]) else "not defined"
    snr_rows.append((int(row["roi"]), f"{row['snr_linear']:.3f}", db_text))
report.table(
    "Manual optical SNR in the 0.75-1.25 N band",
    ["ROI", "Linear SNR", "SNR (dB)"],
    snr_rows,
    widths=[1200, 3000, 5160],
    source="manual_snr.csv [E26]. Zero indicates no qualified positive signal above the defined noise comparison.",
)
report.paragraph(
    "Only ROI 3 at approximately 1.875 N and ROI 9 at approximately 0.875 N satisfy the frozen characterization detection rule of 1% empirical no-contact FPR and 95% recall with at least three press sessions. These are characterization observations, not application thresholds. The NEF calculation is also deliberately limited: ROIs 1, 4, 8, and 9 yield one-noise proxy values of 17.51, 1.43, 6.68, and 2.32 N respectively; other ROIs are unavailable because sensitivity is zero/negative. The proxy is noise divided by low-force slope and cannot establish true force resolution. [E25, E26]"
)
report.heading("9.6 Automatic hysteresis and cycle evidence", 2)
report.figure(
    charts["hysteresis"],
    "Median absolute hysteresis percentage by ROI",
    "automatic_hysteresis.csv [E26]. Two independent sessions per exact ROI × speed × displacement condition; estimates are descriptive.",
)
h_abs = hysteresis["median_hysteresis_percent_observed_span"].abs()
report.paragraph(
    f"Across {len(hysteresis)} condition/force-bin summaries, the median signed hysteresis is {hysteresis['median_hysteresis_percent_observed_span'].median():.2f}% of observed span, the median absolute value is {h_abs.median():.2f}%, and the 95th percentile absolute value is {h_abs.quantile(.95):.2f}%. The observed signed range is {hysteresis['median_hysteresis_percent_observed_span'].min():.2f}% to {hysteresis['median_hysteresis_percent_observed_span'].max():.2f}%. ROI-level median absolute hysteresis ranges from about 0.80% (ROI 6) to 12.49% (ROI 9). Speed is a hysteresis stratum, not a standalone application-model feature."
)
holding = phase_session[phase_session["phase"] == "holding"]
dwell = phase_session[phase_session["phase"] == "inter_cycle_dwell"]
report.paragraph(
    f"The automatic hold phase is short: median session-level cycle duration is {holding['median_duration_s'].median():.3f} s. The inter-cycle dwell median is {dwell['median_duration_s'].median():.3f} s. These durations support short fixed-displacement settling and unloaded-recovery observations only. They do not satisfy a true creep protocol, which would require a constant-force hold of at least 5 s and preferably 30-60 s. Four sessions have one fewer usable cycle in cycle-bin summaries; the validation report identifies them explicitly. [E25, E26]"
)
report.heading("9.7 Approximate system lag", 2)
lag_rows = []
for _, row in lag_summary.sort_values("roi").iterrows():
    lag_rows.append((int(row["roi"]), f"{row['median_best_lag_ms']:.1f}", f"{row['minimum_best_lag_ms']:.1f}", f"{row['maximum_best_lag_ms']:.1f}"))
report.table(
    "Manual-only derivative-correlation system-lag summary",
    ["ROI", "Median lag (ms)", "Minimum (ms)", "Maximum (ms)"],
    lag_rows,
    widths=[900, 2600, 2600, 3260],
    source="manual_system_lag_summary.csv [E26]. Positive means optical follows reference under the stated convention.",
)
report.paragraph(
    "Median lags vary from −37.5 to 137.5 ms and several session extrema reach the search boundaries. The estimate combines camera exposure/readout, USB, host acquisition, software timestamping, reference interpolation, material response, and the selected derivative-correlation method. It is therefore an exploratory system-level lag, not intrinsic material response time or measured end-to-end display latency."
)


report.heading("10. Force Localization Model", 1)
report.heading("10.1 Current v4 event/localization pipeline", 2)
report.numbered(
    [
        "Read only the nine `signed_delta_v_sum` and nine `active_fraction` camera features in the exact frozen order. Load cell and printer are rejected as runtime inputs.",
        "Normalize each signed sum and active fraction using manual-only per-ROI centers/scales saved in the reviewed bundle.",
        "Apply a causal nine-frame moving mean to signed and active channels.",
        "Calculate the contact score as the spatial peak-to-peak range of filtered normalized signed sums.",
        "During an unloaded warm-up of at least 120 frames, set the global contact threshold to the larger of the bundle fallback (0.596388) and the empirical 99th percentile of warm-up scores.",
        "Set nine independent rod activation thresholds to the larger of 4.0 normalized units and the warm-up 99.9th percentile plus 1.0; release occurs below 60% of the activation threshold.",
        "Declare raw contact if either the global signed-range score crosses threshold or any independent active-fraction ratio reaches one. Apply two-frame acquire and clear debounce.",
        "For an event, accumulate positive normalized active-fraction evidence over raw-contact frames, including the acquisition buffer. Forced ROI is the accumulated argmax; confidence is `(top1 − top2)/total`.",
        "Withhold the selective single-ROI label when confidence is below 0.374959 unless independent rod gates provide active ROI identities. Require two frames before switching a maintained single-ROI candidate.",
        "Represent simultaneous rods as a set when multiple independent gates are active. In that state, force is withheld because the single-press force model cannot decompose contributions.",
    ]
)
report.heading("10.2 Validation design", 2)
report.paragraph(
    "The reported event metrics aggregate frame evidence to events before classification. Each of six outer folds holds out the complete TEST number across all nine ROI, preserving session independence and target coverage. Model comparisons and thresholds are fitted within the outer training portion. However, the outer groups were already inspected during prior recovery, so the design is grouped retrospective evaluation rather than untouched confirmation. [E02, E04, E27]"
)
report.figure(
    charts["localization_models"],
    "Grouped event-localization model comparison",
    "v4 metrics.json [E27]. All families use 102 events/54 independent sessions; outer TEST groups had been inspected post-hoc.",
)
report.paragraph(
    "Accumulated argmax was retained because it had the highest aggregate macro F1 (93.43%) compared with Extra Trees (91.14%), shrinkage LDA (89.25%), and logistic regression (85.27%). Its simplicity also matches the event physics and avoids adding a more fragile learned classifier without performance benefit."
)
report.figure(
    charts["localization_recall"],
    "Event-localization recall across all nine ROI",
    "v4 metrics.json [E27]. The 80% line is the frozen primary benchmark; post-hoc status remains controlling.",
)
report.table(
    "v4 event detection and localization metrics",
    ["Metric", "Value", "Interpretation"],
    [
        ("Events / independent sessions / folds", "102 / 54 / 6", "Single-press manual TEST1-TEST6 events"),
        ("Forced event accuracy", "93.41%", "A ROI is always selected for eligible events"),
        ("Forced event macro F1", "93.43%", "Primary localization metric"),
        ("Per-ROI event recall", "90.28-100%", "All nine exceed the 80% recall benchmark used for presentation; minimum is ROI 4"),
        ("Selective macro F1 / coverage", "100% / 82.93%", "Higher precision by withholding uncertain ROI labels"),
        ("Mean / minimum contact recall", "97.79% / 94.20%", "Across outer folds"),
        ("Mean / maximum no-contact frame FPR", "0.41% / 2.44%", "Dedicated no-contact frames"),
        ("Maximum false-contact episodes/min", "0", "No debounced false episode in the evaluated replay"),
        ("Median detection delay", "1 frame in every fold", "Approximately 67 ms at 15 FPS, excluding unmeasured exposure/display latency"),
    ],
    widths=[2500, 1900, 4960],
    source="v4 metrics.json [E27].",
)
report.heading("10.3 Simultaneous-rod handling", 2)
report.paragraph(
    "The runtime can display a set of independently thresholded rods and withhold force in a multi-press state. A combined replay of independent single-ROI evidence reported 100% exact-set accuracy at 83.08% displayed coverage. This is a synthetic combination/fallback check, not validation on true simultaneous physical presses: the archive contains zero labeled multi-press events. Consequently, the thesis may describe multi-ROI representability and logic coverage, but not measured multi-contact localization accuracy. [E05, E18]"
)


report.heading("11. Force Estimation Model", 1)
report.callout(
    "Required thesis wording",
    "The model produces an approximate, post-hoc, camera-only force estimate inside a narrow optical-score support. Its cross-validated error is reportable, but force resolution, general accuracy, common safe range, and physical deployment validity are not established.",
    "caution",
)
report.heading("11.1 Input, mapping, and runtime behavior", 2)
report.paragraph(
    "The v4 force component retains the v2 monotonic isotonic mapping. Its scalar input is the filtered normalized signed spatial range: after per-ROI centering/scaling and the causal nine-frame mean, the score is `max(filtered_signed) − min(filtered_signed)`. The model contains 32 strictly ordered x knots and nondecreasing y knots. Linear interpolation between knots produces approximately 1.859-2.693 N; declared force-operation labels are 1.7-3.0 N. A score below the first knot returns `below_range`, above the last knot returns `above_range`, and neither produces a numeric force. During an event, the completed-event force is the maximum in-support frame estimate. Force is set to 0 only in the hybrid NO_CONTACT state. [E18, E29]"
)
report.paragraph(
    "The runtime exposes current approximate force while a single-rod event is active and peak approximate force at completion. It also records the approximate force associated with the event peak raw V and peak positive delta-V for the selected ROI. If multiple rods are active, all force outputs are withheld because the training and mapping are single-press. If the optical score leaves support, force is withheld rather than clipped to an endpoint. [E18, E29]"
)
report.heading("11.2 Candidate model search", 2)
report.paragraph(
    "The original plan listed zero-intercept total light, nonnegative least squares, positive ridge, elastic net, isotonic, monotonic piecewise/spline, histogram gradient boosting, XGBoost, and exploratory SVR/tree/MLP candidates. The post-hoc expanded benchmark compared grouped isotonic, ridge, elastic net, Extra Trees, and histogram gradient boosting over 30 normalized signed/active summary features and causal windows. Outer-fold selection chose Extra Trees in four folds, isotonic in one, and ridge in one. The family instability and negligible aggregate difference from v2 did not justify replacing the reviewed monotonic mapping. [E02, E04, E27]"
)
report.figure(
    charts["force_mae"],
    "Conditional force MAE versus fold-fitted constant baseline",
    "v4 metrics.json [E27]. The selected family is chosen within each outer training partition, but the outer TEST groups are post-hoc.",
)
report.table(
    "Force-estimation headline metrics and gates",
    ["Metric", "Observed", "Gate/benchmark", "Status"],
    [
        ("Conditional MAE, retained isotonic", "0.328 N", "≤0.75 N", "Numerically passes, but post-hoc"),
        ("Displayed/end-to-end MAE, v2 replay", "0.380 N", "≤0.75 N", "Numerically passes, but post-hoc"),
        ("RMSE", "0.384 N", "≤1.0 N", "Numerically passes, but post-hoc"),
        ("Absolute force bias", "0.115 N", "≤0.20 N", "Numerically passes, but post-hoc"),
        ("Per-ROI conditional MAE", "0.305-0.408 N", "≤1.0 N", "Numerically passes, but post-hoc"),
        ("Cross-validated p95 absolute error", "0.643 N", "No frozen release gate", "Uncertainty summary only"),
        ("MAE gain vs. constant, retained", "0.013 N / 3.81%", "Combined resolution gate expected ≥5% plus tracking", "Fails material-improvement component"),
        ("Expanded-search gain vs. constant", "4.49%", "≥5% target used in recovery audit", "Fails"),
        ("Mean Spearman rho", "0.207 retained; 0.136 expanded", "≥0.30 recovery criterion", "Fails"),
        ("Predicted/true SD ratio", "0.207", "≥0.25 recovery criterion", "Fails"),
        ("Force resolution", "Not established", "Required for resolution claim", "Blocked"),
        ("Common all-ROI safe range", "Not established", "Required by authoritative plan", "Blocked"),
    ],
    widths=[2600, 1800, 3000, 1960],
    source="[E02, E04, E27-E29].",
)
report.figure(
    charts["force_resolution"],
    "Fold-level force variation-tracking limitations",
    "v4 expanded candidate benchmark [E27]. Low spread and weak rank tracking show why MAE alone is insufficient.",
)
report.heading("11.3 Why low MAE does not establish force resolution", 2)
report.paragraph(
    "The evaluated force window is narrow and centered near roughly 2.3 N. A constant predictor can therefore achieve a mean MAE of 0.341 N without resolving meaningful within-session force changes. The isotonic model reduces that error only slightly and compresses predicted variation. Mean predicted standard deviation is about one-fifth of true standard deviation, and rank correlation is weak. In practical terms, the output often behaves like a mildly varying central estimate rather than a sensor that reliably orders or separates small force steps."
)
report.paragraph(
    "True resolution requires a dedicated randomized, settled small-step protocol with known increments, repeated across sessions and positions, plus a declared detection/discrimination rule. The collected manual presses and continuous automatic ramps do not provide that design. The NEF characterization metric is also only a noise-to-slope stability proxy and cannot substitute for a step-resolution experiment. [E03, E25-E27]"
)
report.heading("11.4 Release decision", 2)
report.bullets(
    [
        "The six outer TEST folds were reused after inspection; no untouched confirmation data remain.",
        "The force estimator fails the combined improvement/rank/response-resolution gate.",
        "The authoritative all-ROI common safe-range procedure remains failed.",
        "Known-force physical validation of the live optical estimate is absent.",
        "End-to-end latency, 60-minute soak, and representative-user validation are absent.",
        "The model bundle and release_decision.json explicitly set validated_claim_allowed to false.",
    ]
)


report.heading("12. Historical Models and What They Contributed", 1)
report.paragraph(
    "Several earlier analyses remain in the workspace. They are useful for explaining technical evolution but are not directly comparable because they use different archives, force ranges, features, validation grains, and governance rules. Their numbers must not be blended into the v4 headline."
)
report.table(
    "Historical model record",
    ["Model/run", "Data and validation", "Headline result", "Current interpretation"],
    [
        ("Automatic-archive linear", "162 automatic sessions; grouped full sessions", "Force all-frame MAE 0.526 N, R² 0.0165; session localization 54.94%; non-contact predicted-contact rate 99.58%", "Automatic archive is now forbidden for application modeling; diagnostic failure"),
        ("Early manual ROI-conditioned linear", "Older 9-session Manual Calibration.zip; within-session blocked fifths", "Localization 79.68%; end-to-end force MAE 2.562 N, R² 0.087", "One session/ROI and frame-block validation; exploratory only"),
        ("Early global manual families", "Within-session validation; ridge/nonlinear/RFF", "Best delayed nonlinear MAE 2.380 N, R² 0.217", "Diagnostic evidence that temporal context helped but generalization was weak"),
        ("Clean global TEST2-TEST6", "Leave-one-complete-TEST-out; TEST1/TEST7/no-contact excluded", "Delayed nonlinear MAE 1.646 N, R² 0.362; low-force MAE 2.391 N; false-contact rate 100%", "Historical force-shape diagnostic; exclusions and low-force failure prevent deployment"),
        ("Current characterization global summary", "Session-zeroed total linear over 1.7-3.0 N", "R² about 0.0136; constant often best median session MAE", "Global aggregation sacrifices position information and does not support a universal force map"),
        ("Recovery v1", "Manual-only post-hoc six folds", "Displayed MAE 0.479 N; localization macro F1 88.04%", "First disclosed recovery; later runtime parity fixes supersede it"),
        ("Recovery v2", "Complete-sequence replay with debounce", "Displayed MAE 0.380 N; forced localization F1 89.13%; force-resolution gate failed", "Source of retained isotonic force mapping"),
        ("Recovery v3", "Event-centric camera-only output", "Event localization F1 93.43%; no Newton output", "Source of v4 detector/localizer"),
        ("Hybrid v4", "v3 event/localization + v2 isotonic force", "Event F1 93.43%; conditional force MAE 0.328 N", "Current experimental bundle; validated claim withheld"),
    ],
    widths=[1600, 2700, 2600, 2460],
    source="[E04, E27, E31-E34] and historical analysis outputs. Metrics are intentionally not pooled.",
)
report.heading("12.1 Main lessons from the model history", 2)
report.bullets(
    [
        "Frame-level or within-session splits can overstate generalization; independent session/TEST-group folds are necessary.",
        "Automatic cyclic data are not interchangeable with manual application presses; archive role and collection protocol matter.",
        "Total positive light is spatially heterogeneous and non-monotonic, so a single global force relationship is weak.",
        "Event aggregation and active-fraction spatial evidence are more reliable for discrete localization than global continuous force regression.",
        "A low error inside a narrow force band can coexist with weak dynamic tracking; constant-baseline and spread/rank checks are mandatory.",
        "Simple argmax/monotonic models can outperform or equal more complex families when the dataset is small and device-specific.",
    ]
)


report.heading("13. Physical, Software, and Engineering Validation", 1)
report.heading("13.1 Physical camera and load-cell validation", 2)
report.table(
    "Measured physical acquisition results on 3 August 2026",
    ["Test", "Measured result", "Interpretation"],
    [
        ("DirectShow camera cadence", "901 frames/60 s; 14.98485 FPS; interval median 64.105 ms, p95 79.980 ms, max 81.105 ms", "Stable tested stream; nominal 30 FPS readback not authoritative"),
        ("Nano/HX711 cadence", "661 samples/60 s; median interval 91.847 ms (~10.89 Hz), p95 92.128 ms", "No gaps or parse errors in tested run"),
        ("200 g calibration windows", "U=56,052.6±22.748 counts; L=178,197.388±25.410; factor=610.723941 counts/g; SNR=4,807; loaded CV=0.0208%", "Calibration quality gates passed"),
        ("Fixture preload and retare", "Preload 54.216 g; retare 89,201.518±28.025 counts", "Fixture-aware zero was necessary"),
        ("Known-mass verification", "199.0146±0.0450 g; error 0.9854 g / 0.4927%", "Passed ±5% criterion on one placement"),
        ("Accepted physical session", "161 frame/video/feature/master rows; 125 load samples; 0 missing/invalid sync; peak 5.0935 N", "Complete synchronized acquisition demonstrated"),
        ("Physical-session sync gaps", "Median 24.943 ms; p95 42.830 ms; max 45.940 ms", "Well within 200 ms limit for this run"),
        ("Physical optical response", "No dominant ROI; peak mean delta-V below localization threshold", "Session did not demonstrate specimen mechanoluminescence or live localization"),
    ],
    widths=[2200, 4300, 2860],
    source="[E06].",
)
report.paragraph(
    "This validation supports the tested acquisition/calibration path, not universal hardware performance. It did not measure true optical latency, destructive disconnect recovery, repeated placements, long-term drift, physical live-model accuracy, or specimen mechanoluminescence. The absence of a clear optical response in the accepted session is a negative result that must be preserved in the thesis."
)
report.heading("13.2 Printer validation", 2)
report.paragraph(
    "Software implementation covered connection ownership, Marlin response parsing, command lifecycle, bounds, press-zero validity, sequence phases, pause/abort, force-limit polling, synchronized motion provenance, and simulation. Physical checks identified Marlin 1.1.6, read positions with M114, homed explicitly, and exercised small X/Y/Z moves. Operator inspection confirmed positive Z moves away from the specimen. A full physical repeated-press sequence, loaded force abort, worst-case abort latency, long-run motion reliability, and mechanical repeatability were not tested. [E07]"
)
report.heading("13.3 Engineering reviews", 2)
report.paragraph(
    "Five structured reviews covered architecture, camera service, load-cell/synchronization, data integrity, and GUI/usability. Findings were resolved to zero recorded blockers/majors/minors at the dated checkpoint. Important resulting controls include newest-only preview buffers, bounded analytical/recording queues, exact schema validation, atomic writer finalization, one serial owner, explicit readiness flags, immutable frame IDs, generation-based transaction safety, and fail-closed partial recovery. [E08]"
)
report.heading("13.4 Current automated verification", 2)
report.table(
    "Current test audit on 18 August 2026",
    ["Group", "Passed", "Scope"],
    [
        ("Core/model/data-quality", 189, "Live bundle, contracts, schemas, features, baseline, calibration, synchronization, characterization, splits"),
        ("Exporter/firmware/graphs/logging/motion", 37, "Data and support services"),
        ("Performance/reliability/controller/serial/simulation sources", 91, "Reliability and repeated-press logic"),
        ("Camera/printer/Qt workers/recording/simulation smoke", 83, "Service and integration paths"),
        ("GUI-bearing files run individually", 90, "Camera settings/orientation, components, transactions, smoke, visualization, workflow, printer GUI"),
        ("Total", 490, "All current test files passed in isolated groups"),
    ],
    widths=[3600, 1200, 4560],
    source="Current local test execution; validation_checks.json.",
)
report.paragraph(
    "`pip check` reports no broken requirements. A monolithic mixed Qt run reproduced a Windows access violation during pyqtgraph ROI garbage collection; the same files pass when run individually. A separate documented recording-finalization timing flake can also appear only in a monolithic run. These are test-harness/process-lifetime reliability issues that should be fixed, but they do not invalidate the isolated assertions. Conversely, isolated tests cannot substitute for the missing physical live-model, soak, and user validation."
)


report.page_break()
report.heading("14. Live Sensor GUI: Implementation, Use, Outputs, and Safeguards", 1)
report.callout(
    "Application status",
    "The Live Sensor tab is an implemented camera-only experimental application surface. It is integrated into the main calibration GUI and can run live optical rows or a bundled replay, but every screen and export preserves the model's post-hoc, not-physically-validated status.",
    "caution",
)
report.heading("14.1 Role in the main application", 2)
report.paragraph(
    "The main window creates `LiveSensorTab` from `models/live_sensor_experimental_hybrid_v4` and exposes it as the tenth tab, titled `Live Sensor (Experimental)`. It sits beside the camera, ROI/baseline, image processing, motion magnification, load-cell, recording, printer, graph/export, and status tabs. This makes the live sensor an application view over the same authoritative camera acquisition and optical feature pipeline; it is not a separate executable or an alternate acquisition process. [E01, E18, E36]"
)
report.paragraph(
    "When an unloaded baseline is accepted, the main window passes its validity, baseline ID, ROI-layout ID, camera rotation, and mirror state into the live tab, then starts the normal live optical-analysis worker. Any camera or ROI-layout change invalidates the baseline, resets the tab to setup-blocked, and stops live analysis. The tab deliberately refuses the motion-magnified quantitative source: a baseline is not made live-sensor-ready when that source is selected, and magnified rows are not forwarded to inference. Consequently, current v4 inference is tied to baseline-corrected features from the original oriented camera frame. [E18, E19, E36]"
)
report.paragraph(
    "The load cell and printer remain available elsewhere in the calibration application, but the live inference engine never reads them. Its bundle contract accepts only the nine signed integrated delta-V values and nine active-fraction values from the camera/mechanoluminescent-skin path. This separation is visible in the permanent warning, setup instructions, report JSON, and automated tests. [E12, E18, E36-E38]"
)
report.heading("14.2 Actual Live Sensor tab presentation", 2)
report.figure(
    charts["live_sensor_gui"],
    "Live Sensor (Experimental) tab after the bundled replay completed one press",
    "Actual PySide6 `LiveSensorTab` rendered from the current v4 bundle and shipped replay trace [E36, E38]. The ~2.33 N/ROI 6 display is replay output, not a new physical measurement.",
    width=5.75,
)
report.paragraph(
    "The warning banner remains above every other control. The captured view shows an actual completed replay event, the selected ROI cell, completed 120-frame warm-up, event-report availability, and the controlling archive-recovery metrics. It is included to document software behavior and interface design only; it does not add independent model or hardware evidence."
)
report.heading("14.3 Visible regions, controls, and operator meaning", 2)
report.table(
    "Live Sensor tab interface inventory",
    ["Interface region/control", "Behavior and operator meaning"],
    [
        ("Permanent experimental warning", "States that the model is not validated, force is approximate and narrow-range, resolution is unestablished, inputs are camera/skin only, ROI is tentative, and output must not be used for safety, control, or physical-validation claims."),
        ("Current / last force event", "Large state banner, force or dash, ROI/confidence or withheld text, plus a detailed message containing current score and threshold when available."),
        ("Nine ROI cells", "A 3×3 grid labeled 1-9; active displayed ROI cells turn green. More than one cell can be active when independent rod gates cross."),
        ("Unloaded warm-up progress", "Progresses to the bundle minimum of 120 frames. Live estimates remain unavailable until a compatible baseline exists and warm-up completes."),
        ("Calibrate while unloaded", "Starts adaptive global and per-ROI unloaded threshold calibration; enabled only after compatible baseline/layout context is accepted."),
        ("Reset estimate", "Stops replay, clears temporal/event/threshold state, preserves current context, and requires a new unloaded warm-up."),
        ("Run bundled replay demo", "Processes the signed, hardware-free JSONL trace at a 25 ms timer interval, including replay warm-up and one example event."),
        ("Post-processing report status", "Counts completed presses captured during the current app session and separately reports how many ROI assignments were withheld."),
        ("Export post-processing report", "Creates a new timestamped directory containing HTML, event CSV, ROI-summary CSV, and JSON; disabled until at least one event completes."),
        ("Clear captured events", "Clears only the in-memory report history after confirmation; previously exported report directories are not deleted."),
        ("Archive recovery evidence", "Prints the controlling six-fold contact/localization/force metrics and repeats that they are reused-fold recovery results rather than untouched validation."),
    ],
    widths=[2500, 6860],
    source="gui/live_sensor_tab.py [E36].",
)
report.paragraph(
    "The main force label and warm-up control have explicit accessibility names, every ROI cell has an accessible `ROI n` name, and state is communicated with text as well as color. The content is placed inside a resizable scroll area. These are implementation-level accessibility provisions; the frozen representative-operator, scaling, keyboard-access, and state-recognition usability gates have not been completed."
)
report.heading("14.4 Live operator workflow", 2)
report.numbered(
    [
        "Connect and preview the camera with the mechanoluminescent skin. The model does not require a load-cell or printer connection.",
        "Use the canonical processed 480×640 view, 90° clockwise rotation, horizontal mirror enabled, and layout `roi-9fbc67c50ee3bc7145fa`. Load `assets/live_sensor/roi_layout.json` when necessary.",
        "Keep the sensing surface fully unloaded, capture the standard per-pixel optical baseline, review its stability result, and explicitly accept it.",
        "Open `Live Sensor (Experimental)`. A valid context changes the guidance from `Accept an unloaded baseline first` to `Baseline accepted. Keep unloaded and start warm-up`.",
        "Press `Calibrate while unloaded` and keep all nine ROI unloaded for at least 120 frames. The engine calculates the adaptive global contact threshold and nine independent rod thresholds from this warm-up.",
        "After READY, press the skin while preserving the same camera settings, geometry, lighting, and mechanical setup. Read the state banner before interpreting force or ROI.",
        "Treat `~x.xx N` as approximate. A dash means force is unavailable; an uncertain/withheld ROI is deliberately not converted into a confident label; multiple green ROI cells cause Newton force withholding.",
        "After one or more completed events, export the post-processing report. Record the output directory with the session evidence; the GUI report is descriptive model output and not ground truth.",
        "Repeat baseline capture and unloaded warm-up after a camera/orientation/ROI/baseline context change, after `Reset estimate`, or when lighting/drift makes the existing context questionable.",
    ]
)
report.heading("14.5 Runtime states and displayed behavior", 2)
report.table(
    "Live Sensor GUI/runtime state interpretation",
    ["Visible state/result", "Force presentation", "ROI presentation", "Meaning/action"],
    [
        ("MODEL UNAVAILABLE", "—", "—", "Bundle failed integrity/schema/content checks; live and replay controls are disabled and the rejection reason is shown."),
        ("SETUP BLOCKED", "—", "—", "No accepted matching baseline, or layout/rotation/mirror mismatch. Correct context and recapture baseline."),
        ("WARMING UP / REPLAY WARM-UP", "—", "—", "Unloaded global and per-ROI threshold evidence is accumulating; any contact contaminates calibration."),
        ("READY", "—", "—", "Minimum unloaded warm-up has completed and inference may begin."),
        ("NO CONTACT", "0.00 N in hybrid mode", "—", "No debounced contact. After a completed event, the GUI may keep the last event's peak/ROI in the large result fields while the state/message still indicate current no-contact."),
        ("EVENT ACTIVE", "Current `~x.xx N` only when single-contact score is inside support", "Accepted ROI(s) and qualitative confidence", "A debounced optical event is active. Peak quantities continue accumulating."),
        ("EVENT ACTIVE UNCERTAIN ROI", "Approximate force may remain available for an in-support single press", "`uncertain (withheld)`", "Event evidence has not cleared the selective localization threshold; abstention is intentional."),
        ("EVENT COMPLETE", "`~x.xx N peak` when an eligible event peak exists", "Completed ROI(s)", "Contact cleared; the completed event is captured once for the in-memory report history."),
        ("EVENT COMPLETE UNCERTAIN ROI", "Peak force only if otherwise eligible", "Withheld", "Completed event remains auditable but the reported ROI is blank/withheld."),
        ("Below/above support", "—", "ROI may still display", "The isotonic model is not extrapolated or clipped. An above-support sample can invalidate the event peak force."),
        ("Simultaneous rods", "—", "Multiple highlighted ROI cells", "Independent rod gates represent multi-contact, but single-press training cannot decompose Newton force."),
        ("ERROR", "—", "—", "Required optical fields are missing/non-numeric or contain non-finite values; output fails closed."),
    ],
    widths=[1900, 2500, 2000, 2960],
    source="[E18, E19, E36]. GUI result labels are event-engine results; the broader scientific state contract additionally defines STARTUP, BASELINE_CAPTURING, PAUSED, RECONNECTING, LIVE_UNCERTAIN, and LIVE_LIMIT controller states.",
)
report.heading("14.6 Per-frame inference and display contract", 2)
report.numbered(
    [
        "The main window selects the newest valid live optical row and rejects rows from retired analysis generations.",
        "Only original-camera quantitative rows are passed to the live tab. The engine reads the exact 18-feature camera contract and rejects missing, nonnumeric, or non-finite values.",
        "Per-ROI signed and active features are robustly normalized and averaged over the causal nine-frame history; the signed spatial peak-to-peak range becomes the global contact/force score.",
        "During warm-up, the global threshold and nine independent rod thresholds are learned. After warm-up, two-frame contact acquire/clear and rod acquire/release hysteresis stabilize the visible state.",
        "An event accumulates active-fraction evidence and optical peaks. Forced ROI is retained internally for audit; selective ROI can be blank when confidence is insufficient.",
        "The GUI shows current approximate force during an eligible active single press, event-peak force after completion, a unitless score/threshold message, and highlighted accepted ROI cells.",
        "Completed events are deduplicated by source epoch, engine event ID, start/end frame IDs, and completion-frame ID before report capture.",
    ]
)
report.callout(
    "Interpretation safeguard",
    "The confidence text is a rule-based evidence-gap label, not a calibrated probability. A green ROI cell is an experimental discrete classification, not a continuous location measurement. A displayed Newton value is model output, not the load-cell reference.",
    "caution",
)
report.heading("14.7 Completed-event report products", 2)
report.paragraph(
    "A report can be exported only when at least one valid completed optical event has been captured. The destination is a new UTC-stamped `live_sensor_report_YYYYMMDDTHHMMSSZ` directory; a numeric suffix avoids collision. Files are written into a temporary sibling directory and atomically published with `os.replace`, so an incomplete report is not presented as final. Existing report directories are never overwritten. [E36, E37]"
)
report.table(
    "Live Sensor post-processing report files",
    ["File", "Contents and use"],
    [
        ("post_processing_report.html", "Self-contained responsive report with permanent experimental notice, headline cards, press-count bars, nine-ROI summary, event audit table, definitions, validation boundary, follow-up questions, and print styling."),
        ("event_details.csv", "One completed-event row with exact event, localization, optical-peak, force, support-status, and frame-bound fields."),
        ("roi_summary.csv", "Nine rows, including zero-count ROI, with press distribution, accepted/withheld counts, force availability, and peak/mean/max summaries."),
        ("report.json", "Machine-readable schema version 2 record containing generation time, bundle ID, experimental validation status, explicit camera-only inference inputs, events, and ROI summary."),
    ],
    widths=[2550, 6810],
    source="core/live_sensor_report.py [E37].",
)
report.table(
    "Exact completed-event export fields",
    ["Field group", "Columns"],
    [
        ("Identity/time", "completed_at_utc; event_id; start_frame_id; end_frame_id; duration_frames"),
        ("Localization", "forced_roi; reported_roi; forced_rois; reported_rois; localization_status; confidence"),
        ("Optical peaks", "peak_hsv_v; peak_delta_v; peak_contact_score; peak_signal_ratio"),
        ("Force/status", "force_at_peak_hsv_v_N; force_at_peak_delta_v_N; event_peak_force_N; force_status"),
    ],
    widths=[2100, 7260],
    source="EVENT_COLUMNS in [E37]. Missing/non-finite values are exported as blank fields, never NaN strings.",
)
report.table(
    "Exact nine-ROI summary fields",
    ["Field group", "Columns"],
    [
        ("Distribution", "roi; press_count; press_share_percent"),
        ("Localization/availability", "accepted_count; withheld_count; force_available_count"),
        ("Optical/force summaries", "max_peak_hsv_v; max_peak_delta_v; mean_force_at_peak_hsv_v_N; mean_event_peak_force_N; max_event_peak_force_N"),
    ],
    widths=[2600, 6760],
    source="SUMMARY_COLUMNS in [E37]. Every independently detected rod is counted, so one simultaneous event can contribute to several ROI rows.",
)
report.paragraph(
    "Peak raw V is the largest HSV Value value (0-255) in the dominant assigned ROI during the event. Peak delta-V is the corresponding largest baseline-corrected increase. `Force at peak V` and `force at peak delta-V` are evaluated on the exact frames of those optical maxima and can differ from the maximum in-support event force. For simultaneous rods, force fields are withheld; for selective abstention, the forced ROI remains in the audit fields while the reported ROI is blank."
)
report.heading("14.8 Bundled replay and verification", 2)
report.paragraph(
    "`Run bundled replay demo` reads the hash-checked `replay_demo.jsonl`, creates a temporary ready context named `bundled-manual-replay`, performs the same unloaded warm-up, and feeds rows through the production engine on a 25 ms GUI timer. It disables live warm-up during replay, restores the real baseline readiness afterward, and can populate the same completed-event report controls. Replay is an operator demonstration and regression path; because it is derived from already-inspected manual evidence, it is not independent validation. [E18, E29-E30, E36]"
)
report.table(
    "Dedicated Live Sensor verification audit on 18 August 2026",
    ["Test files", "Result", "Covered behavior"],
    [
        ("test_experimental_live_sensor.py", "Included in 35 passes", "Bundle integrity, warm-up, thresholds, replay, force support, contact debounce, event state, localization abstention, multi-ROI and force withholding"),
        ("test_live_sensor_report.py", "Included in 35 passes", "Completed-event validation, same-frame peaks, nine-ROI summaries, simultaneous-rod counting, atomic HTML/CSV/JSON export, GUI capture and clear"),
        ("test_live_sensor_contracts.py", "Included in 35 passes", "Schema/layout validation, canonical geometry, allowed state transitions, exact-output state policy"),
        ("Combined execution", "35 passed in 2.07 s", "Current project virtual environment; hardware-free Qt/test path"),
    ],
    widths=[2800, 1700, 4860],
    source="Current follow-up pytest execution [E38]. These cases are included within the broader 490-test audit; they are not 35 additional unique tests beyond that total.",
)
report.heading("14.9 Live Sensor GUI limitations", 2)
report.bullets(
    [
        "The GUI has not been validated against known physical forces or positions; the accepted hardware session did not produce a clear optical localization response.",
        "The bundled replay demonstrates deterministic software behavior on reused manual evidence, not real-time hardware accuracy or prospective generalization.",
        "The post-processing report records model outputs and internal forced/reported labels. It contains no independent ground truth and cannot calculate new physical accuracy from a live session.",
        "Event history is held in memory until exported, cleared, or the app closes. Clearing does not delete prior exports, but an unexported history is not a durable scientific record.",
        "After event completion, the interface intentionally holds the last peak/ROI for visibility. Operators must read the current state/message so a held result is not mistaken for a current contact.",
        "Motion-magnified quantitative frames are intentionally excluded from v4 inference because the frozen model was built for original-camera features.",
        "The interface is fixed to one camera orientation, one nine-ROI layout, one model bundle, and one device-specific preprocessing context.",
        "Representative-user validation, first-time setup success, recovery success, scaling coverage, full keyboard audit, state-recognition accuracy, and optical-limit recognition remain untested release gates.",
        "No GUI output is authorized for safety interlocks, closed-loop printer control, clinical/industrial measurement, or a thesis claim of validated force sensing.",
    ]
)


report.heading("15. Limitations and Threats to Validity", 1)
report.callout(
    "Central limitation",
    "Localization and force do not have the same evidential status. The nine-class event localizer is the strongest model result, while the Newton-valued force output remains a narrow-range post-hoc approximation whose resolution, common safe range, and prospective physical accuracy have not been established.",
    "caution",
)
report.heading("15.1 Force-estimation limitations", 2)
report.table(
    "Force-estimation limitation register",
    ["Limitation", "Why it matters", "Required reporting/mitigation"],
    [
        ("No untouched confirmation set", "All six complete TEST groups were inspected during recovery; cross-validation is no longer prospective.", "Call all v4 metrics retrospective/post-hoc and collect a new frozen confirmation corpus."),
        ("Narrow force population", "The evaluated window is labeled 1.7-3.0 N and is concentrated near its center; low MAE is easier for a near-constant predictor.", "Always pair MAE with constant-baseline gain, rank correlation, prediction/truth spread, and support boundaries."),
        ("Force resolution not measured", "Continuous presses do not create randomized, settled, known small increments; NEF is only a proxy.", "Do not quote a minimum detectable force or resolution. Run a dedicated repeated step-discrimination study."),
        ("Weak variation tracking", "Mean Spearman rho is 0.207 and predicted/true SD is 0.207; the estimate compresses real variation.", "Describe the output as approximate event force, not a dynamically accurate force trace."),
        ("Marginal value over a constant", "Retained improvement is 3.81%; expanded search is 4.49%, below the recovery target and unstable by selected family.", "Do not use MAE alone to imply meaningful calibration or tracking."),
        ("No common all-ROI safe range", "The authoritative preprocessing/range procedure failed because response support and monotonicity differ by ROI.", "Do not claim one validated all-position transfer function."),
        ("Spatial heterogeneity and cross-talk", "Sensitivity is zero/negative for several ROI; diagonal response share has a median of only 0.376 and off-target response can be large.", "Keep the model device/layout-specific; investigate ROI-conditioned models and mechanics."),
        ("Noise, drift, hysteresis, and lag", "No-contact drift, automatic loading/unloading separation, and variable system lag can change the optical score independently of force.", "Rebaseline, control imaging/temperature/mechanics, and validate across time, speed, and loading direction."),
        ("Conditional support", "The 32-knot model only returns a number between fitted optical-score knots; declared Newton labels are not identical to score support.", "Withhold rather than extrapolate or clip; report `below_range`/`above_range` explicitly."),
        ("Single-contact model", "The mapping cannot decompose contributions from simultaneous rods.", "Withhold all force values in multi-ROI state until real multi-contact data and an identifiable model exist."),
        ("No live known-force validation", "The accepted physical session verified synchronized acquisition but produced no clear optical localization response.", "Run known-force live replay/fixture tests on the actual specimen before any deployment claim."),
        ("Device and geometry specificity", "Preprocessing assumes one orientation, mirror, ROI layout, optical baseline, camera behavior, and tested mechanics.", "Do not generalize to another camera, skin, mounting, ROI layout, or lighting without recalibration and new validation."),
    ],
    widths=[1900, 3800, 3660],
    source="Consolidated from [E02-E06, E25-E30].",
)
report.paragraph(
    "The report intentionally places these limitations beside the force metrics and repeats them in the thesis-safe claims section. This prevents a reader from extracting the 0.328 N MAE without seeing that the model has weak within-session ordering, limited spread, post-hoc fold reuse, and no established physical resolution."
)
report.heading("15.2 Construct validity", 2)
report.bullets(
    [
        "The load cell measures fixture/reference force, while optical features measure camera V-channel changes inside fixed rectangles. Their association is not direct proof of a material constitutive relationship.",
        "The term localization means selection among nine predefined regions. It does not quantify continuous position error in millimetres, sub-ROI location, or contact-shape reconstruction.",
        "The system-lag estimator combines material, camera, USB, host, interpolation, and algorithm effects. It is not intrinsic mechanoluminescent response time.",
        "Automatic loading-unloading separation is labeled hysteresis, but it contains only two sessions per exact condition and may include drift, rate dependence, fixture effects, and synchronization error.",
        "Automatic hold duration and dwell summarize the available cycle protocol; they are not a designed long-duration creep experiment.",
        "Positive delta-V discards darkening for several summary features, while the v4 detector preserves signed spatial range. Results therefore depend on the chosen optical construct.",
    ]
)
report.heading("15.3 Internal and statistical validity", 2)
report.bullets(
    [
        "Sessions, not frames, are the independent unit. Frame counts can describe coverage but must not be presented as independent sample size.",
        "Outer groups were complete TEST identities, which limits direct leakage, but repeated post-hoc inspection introduces researcher degrees of freedom and optimistic selection risk.",
        "Only 102 labeled single-press events support nine classes. Per-ROI estimates therefore have limited independent-event counts and should be accompanied by the fold/session design.",
        "Several metrics are medians of session-balanced bins or rows. A median cross-talk row need not sum to one, and aggregate hysteresis rows are not independent physical replications.",
        "Four automatic sessions contain one fewer usable cycle than expected; the validation artifact identifies them. Characterization passed its data-quality gate with that qualification.",
        "No formal confidence intervals or preregistered hypothesis tests were used for current model claims. The results are engineering performance estimates.",
    ]
)
report.heading("15.4 External validity and operational limitations", 2)
report.bullets(
    [
        "Evidence is from one sensing assembly, tested camera path, one fixture/load-cell calibration, and a narrow set of laboratory conditions.",
        "Camera auto-exposure, focus, white balance, gain, ambient illumination, material aging, temperature, mounting, and mechanical boundary conditions can change optical response.",
        "The full Ender 3 repeated-press sequence and force-triggered abort were not physically validated; worst-case abort latency is unknown, and Marlin EMERGENCY_PARSER was disabled on the tested firmware.",
        "The 60-minute inference soak, measured camera-to-display p95 latency, memory-growth gate, reconnect/failure-injection campaign, and representative-operator study are incomplete.",
        "The accepted physical synchronized session did not demonstrate mechanoluminescence or a live localization result. Physical acquisition readiness is therefore stronger than sensor-phenomenon validation.",
        "Windows/Qt process-lifetime behavior prevents treating one monolithic test invocation as stable even though all 490 tests pass in isolated groups.",
    ]
)
report.heading("15.5 Data and software traceability limitations", 2)
report.paragraph(
    "Archive hashes, feature-store manifests, model hashes, exact schemas, and dated decision records provide substantial artifact traceability. However, the absence of committed Git history prevents authoritative code provenance, review attribution, and reproducible checkout by tag. The current local source may therefore be described as the 18 August 2026 workspace state, not as a versioned release. Future results should be generated from a committed, tagged, dependency-locked release with captured hardware/firmware identities."
)


report.heading("16. Thesis-Safe Claims and Writing Guidance", 1)
report.heading("16.1 Claim matrix", 2)
report.table(
    "What the thesis may and may not claim",
    ["Topic", "Supported wording", "Unsupported wording"],
    [
        ("Program", "A standalone calibration/acquisition application was implemented with camera, load-cell, synchronization, recording, optional printer automation, and structured exports.", "The complete system is deployment-certified or physically validated under all operating modes."),
        ("Data integrity", "Two archives were immutably reconciled into 245 sessions and 179,176 decoded frames with zero missing/unmatched video frames.", "All frames are independent samples or all archives are eligible for application modeling."),
        ("Load-cell reference", "One 200 g calibration/verification on the tested fixture achieved 0.493% error and passed the project gates.", "The load cell is universally calibrated across time, mounting, temperature, or force range."),
        ("Localization", "In grouped retrospective evaluation, the camera-only event localizer achieved 93.43% macro F1 on 102 single-press events from 54 sessions.", "The model is prospectively validated, continuously localizes position, or has measured physical multi-press accuracy."),
        ("Selective localization", "Abstention produced 100% macro F1 at 82.93% coverage in the same post-hoc corpus.", "The model is 100% accurate without stating coverage and post-hoc status."),
        ("Force", "Inside fitted support, the experimental mapping had 0.328 N conditional MAE and 0.643 N p95 absolute error in retrospective grouped evaluation.", "The sensor has 0.328 N resolution, measures force accurately in general, or supports extrapolation beyond the trained score/range."),
        ("Characterization", "ROI-specific sensitivity, nonlinearity, noise/drift, repeatability, cross-talk, hysteresis, lag, and NEF-proxy metrics were computed under documented protocols.", "The metrics establish intrinsic material constants, causal mechanisms, or broad environmental robustness."),
        ("Automation", "Printer-control logic, safety states, simulation, protocol handling, and small manual moves were exercised.", "A loaded repeated-press sequence and emergency force abort were physically validated."),
    ],
    widths=[1300, 4210, 3850],
    source="Claim synthesis governed by [E02-E08, E23-E30].",
)
report.heading("16.2 Copy-ready limitations paragraph", 2)
report.callout(
    "Thesis-ready text",
    "Although the camera-only system produced strong retrospective event-localization performance, the force output must be interpreted as an experimental approximation rather than a validated force measurement. All six outer TEST groups had been inspected during post-hoc model recovery, leaving no untouched confirmation set. The retained isotonic mapping achieved a conditional MAE of 0.328 N within a narrow nominal 1.7-3.0 N band, but improved only marginally over a session-balanced constant, showed weak rank tracking, and compressed the observed force variation. A common all-ROI safe range and minimum resolvable force were not established, and no known-force physical validation of the live optical model was completed. The model therefore withholds force outside fitted optical-score support and during multi-contact states; its output should not be generalized to other devices, geometries, lighting conditions, or force ranges without prospective recalibration and validation.",
    "caution",
)
report.heading("16.3 Recommended results order", 2)
report.numbered(
    [
        "Establish the acquisition/calibration system and the immutable dataset before presenting model performance.",
        "Report the failed original confirmatory gate before the post-hoc recovery, so the governance decision is transparent.",
        "Present event detection and nine-class localization as the main successful modeling result.",
        "Present force MAE beside constant-baseline gain, Spearman rho, spread ratio, p95 error, and support/withholding behavior.",
        "Separate characterization findings by protocol: manual general metrics, automatic hysteresis/hold/dwell metrics, and physical hardware verification.",
        "End with unresolved physical, prospective, soak, usability, multi-contact, and force-resolution validation rather than labeling the system fully validated.",
    ]
)
report.heading("16.4 Numbers that must retain their denominator", 2)
report.bullets(
    [
        "93.43% macro F1: 102 single-press events, 54 independent sessions, six complete TEST-group outer folds, post-hoc.",
        "100% selective macro F1: only at 82.93% coverage and under the same post-hoc design.",
        "0.328 N conditional MAE: only in-support contact frames in the narrow operating population; not missed-contact end-to-end performance or resolution.",
        "0.493% verification error: one 200 g placement on one fixture after retare.",
        "179,176 frames: decoded observations across 245 sessions, not 179,176 independent experimental replicates.",
        "Automatic hysteresis condition values: only two sessions per exact condition and descriptive aggregation.",
    ]
)


report.heading("17. Reproducibility and Transfer to ChatGPT Work", 1)
report.heading("17.1 Artifact set delivered with this report", 2)
report.table(
    "Companion artifacts",
    ["Artifact", "Purpose"],
    [
        (DOCX_PATH.name, "Primary presentable technical report with figures, tables, caveats, and appendices"),
        (MD_PATH.name, "Machine-readable full-text version for direct ingestion into ChatGPT Work"),
        (NOTEBOOK_PATH.name, "Executable/embedded-output checks for headline data and model metrics"),
        (VALIDATION_JSON_PATH.name, "Machine-readable report QA and output hashes"),
        (EVIDENCE_CSV_PATH.name, "Evidence ID to workspace-path register"),
        (SCHEMA_CSV_PATH.name, "Current authoritative 812-row schema/data dictionary"),
        (CHART_MAP_PATH.name, "Chart question, takeaway, source, and filename map"),
        ("charts/", "Eight source-backed PNG figures used by the report"),
    ],
    widths=[3900, 5460],
    source="Generated together by build_report.py.",
)
report.heading("17.2 Rebuild procedure", 2)
report.numbered(
    [
        "Open a PowerShell prompt at the workspace root and confirm that the `spare_calibration_gui` evidence tree is present.",
        "Run `python reports/thesis_technical_report_2026-08-18/build_report.py` in an environment containing pandas, Pillow, and python-docx. The bundled Codex document runtime may also be used.",
        "Review `validation_checks.json`; every check must be true, the current test audit must remain explicitly qualified, and output hashes should be recorded.",
        "Open the DOCX in Word and update the table of contents if required. Preserve the PDF/PNG visual-render QA record when the document is submitted.",
        "When the underlying project changes, create a new dated report directory rather than overwriting this evidence cutoff.",
    ]
)
report.heading("17.3 Suggested ChatGPT Work ingestion prompt", 2)
report.paragraph(
    "Upload the DOCX and Markdown report together with `evidence_manifest.csv`, `current_schema_dictionary.csv`, `validation_checks.json`, and the validation notebook. Instruct ChatGPT Work to treat this report as the controlling technical evidence dossier; preserve all denominators and evidence-status labels; distinguish implementation, physical verification, characterization, post-hoc model performance, and missing validation; and never transform approximate force MAE into a resolution or general-accuracy claim. Ask it to cite evidence IDs in draft thesis sections so every assertion remains traceable to the local project artifact listed in Appendix D."
)
report.heading("17.4 Reproducibility boundary", 2)
report.paragraph(
    "The report generator validates current metric files and schemas, but it does not re-extract 31 GB of compressed automatic video, retrain every historical model, or physically operate the hardware. Full end-to-end reproduction requires the two original archives, the exact extraction/alignment scripts, the recorded configuration and run manifests, the tested hardware/firmware, and a versioned software environment. Artifact-level reproducibility is currently stronger than historical code-revision reproducibility because Git history is absent."
)


report.heading("18. Required Next Work and Priority Order", 1)
report.table(
    "Recommended validation and development backlog",
    ["Priority", "Work package", "Minimum completion evidence", "Claim unlocked"],
    [
        ("P0", "Version control and release freeze", "Commit all project-owned sources/configs/tests; lock dependencies; tag model/report releases; preserve hashes", "Auditable software provenance"),
        ("P0", "Prospective confirmation corpus", "Predeclare protocol and gates; collect new manual sessions untouched by selection; retain dedicated no-contact sessions", "Prospective detection/localization/force performance"),
        ("P0", "Known-force live optical validation", "Randomized known loads/placements on the actual specimen with complete live replay and synchronized truth", "Physical accuracy statement within a declared range"),
        ("P0", "Force-resolution experiment", "Settled randomized small increments, repeated sessions/ROI, explicit discrimination rule and uncertainty", "Minimum resolvable force—only if passed"),
        ("P1", "All-ROI operating range", "Training-only support and monotonicity procedure repeated with adequate sessions/ROI", "Common or ROI-specific validated force ranges"),
        ("P1", "Real simultaneous-contact study", "Labeled two-or-more-rod combinations with identifiable reference forces", "Measured multi-contact localization and possibly decomposed force"),
        ("P1", "Printer physical safety validation", "Loaded repeated cycles, bounds/interlock tests, force abort and worst-case stop latency", "Physical automated-press readiness"),
        ("P1", "Latency/soak/reconnect reliability", "Measured camera-to-display p95; 60-minute soak; memory-growth and failure-injection results", "Operational performance/reliability"),
        ("P2", "Environmental and longitudinal robustness", "Lighting, camera settings, temperature, aging, remounting, day/operator and device variation", "Defined generalization envelope"),
        ("P2", "Representative usability validation", "At least five representative operators against frozen setup/recovery/state-recognition gates", "Validated usability claim"),
        ("P2", "Qt monolithic-test stabilization", "Eliminate pyqtgraph teardown access violation and recording-finalization timing flake", "Stable all-in-one CI execution"),
    ],
    widths=[600, 2200, 4300, 2260],
    source="Priorities derived from [E02, E05-E08, E28].",
)


report.heading("19. Conclusion", 1)
report.paragraph(
    "The project has produced a substantive research platform: it can calibrate the load-cell reference, acquire and synchronize camera/load-cell/printer evidence, preserve raw and derived products, reconstruct an immutable 245-session feature store, compute a broad sensor-characterization suite, and present guarded camera-only event inference through a dedicated Live Sensor GUI with replay and auditable post-processing exports. Engineering controls—one device owner, monotonic host time, exact schemas, bounded queues, partial-file recovery, model hashing, and fail-closed output—make the software evidence more mature than a typical exploratory script collection."
)
report.paragraph(
    "The principal model contribution is event-centric camera-only localization among nine fixed regions. Its 93.43% retrospective macro F1 is scientifically useful when paired with the 102-event/54-session/six-fold denominator and permanent post-hoc disclosure. The project also demonstrates how selective abstention and independent rod gates can make uncertainty visible rather than forcing every output."
)
report.paragraph(
    "Force estimation remains the limiting claim. The present isotonic model offers a bounded approximate Newton output and defensible error summaries, but it neither tracks force variation strongly nor establishes resolution, a universal safe range, multi-contact force, prospective validity, or physical live accuracy. The thesis should therefore frame the project as a verified calibration/acquisition platform with promising discrete localization and an explicitly experimental force approximation. That framing is both technically accurate and stronger than overstating a force-sensor result the evidence does not yet support."
)


report.page_break()
report.heading("Appendix A. Metric and Formula Reference", 1)
report.table(
    "Core acquisition, preprocessing, and evaluation formulas",
    ["Quantity", "Definition", "Interpretation/caveat"],
    [
        ("Signed pixel delta", "d(x,y,t) = V(x,y,t) − median_baseline[V(x,y)]", "Preserves both brightening and darkening relative to the unloaded baseline"),
        ("Positive pixel delta", "d+(x,y,t) = max(d(x,y,t), 0)", "Ignores darkening; used by legacy light and active-area summaries"),
        ("Integrated signed light", "Sum of d over pixels in one ROI", "Size-dependent unless divided by ROI area"),
        ("Integrated positive light", "Sum of d+ over pixels in one ROI", "Cannot express negative optical response"),
        ("Active fraction", "count[d > threshold_V] / ROI pixel count; default threshold = 10 V", "Dimensionless active-area estimate; threshold depends on camera/baseline stability"),
        ("Centroid", "First spatial moment of positive delta divided by total positive delta", "Unavailable when positive mass is zero"),
        ("Counts per gram", "c = (L − U) / M", "Signed calibration; L/U are loaded/unloaded mean counts and M is known mass in grams"),
        ("Reference force", "F_gf = (raw − tare) / c; F_N = F_gf × 0.00980665", "Direction is preserved by signed c; gravity conversion is conventional"),
        ("Online synchronized force", "Exact timestamp; else bracket interpolation if both sides ≤200 ms; else nearest ≤200 ms; otherwise invalid", "Never crosses device-session identity"),
        ("Offline aligned force", "Per-session linear interpolation at frame_time − fitted_lag", "No extrapolation/cross-session interpolation; maximum bracket gap 300 ms"),
        ("v4 contact score", "peak-to-peak range of nine filtered normalized signed sums", "Unitless, device/model-specific"),
        ("Localization confidence", "(largest accumulated evidence − second-largest) / total accumulated evidence", "Used for selective abstention; not a calibrated probability"),
        ("Conditional MAE", "mean |estimated force − reference force| over eligible in-support contact observations", "Excludes out-of-support/withheld cases; state the eligible population"),
        ("Macro F1", "Unweighted mean of class-specific F1 across nine ROI", "Prevents large classes from dominating but remains sample-size sensitive"),
        ("Hysteresis percentage", "Loading-unloading separation divided by observed optical span for a matched force bin", "Descriptive system response, not an intrinsic material constant"),
        ("NEF proxy", "No-contact optical noise divided by fitted low-force sensitivity", "Stability proxy only; not force resolution"),
    ],
    widths=[1900, 4050, 3410],
    source="[E13-E17, E25-E30].",
)


report.heading("Appendix B. Frozen Release-Gate Status", 1)
report.table(
    "Selected release gates and current disposition",
    ["Gate", "Frozen criterion", "Current evidence", "Disposition"],
    [
        ("End-to-end force MAE", "≤0.75 N", "v2 displayed MAE 0.380 N", "Numerical check passes post-hoc; release still blocked"),
        ("Forced localization macro F1", "≥0.80", "v4 event macro F1 0.934", "Numerical check passes post-hoc"),
        ("No-contact frame FPR", "≤0.05", "maximum fold 0.0244", "Numerical check passes post-hoc"),
        ("Contact recall", "≥0.90", "minimum fold 0.942", "Numerical check passes post-hoc"),
        ("Per-ROI recall", "≥0.70", "minimum event recall 0.903", "Numerical check passes post-hoc"),
        ("Force resolution/tracking", "Material improvement plus response tracking", "Constant gain <5%; rho/spread checks fail", "Fails"),
        ("Common safe range", "Training-only all-ROI supported interval", "Authoritative procedure failed", "Fails"),
        ("Invalid/OOD exact output", "0", "Runtime fail-closed contracts and tests", "Implemented/tested; physical fault campaign pending"),
        ("Inference p95", "≤30 ms", "Performance tests exist", "Software evidence only; verify on final deployment host"),
        ("Camera-to-display p95", "≤300 ms", "Not measured end-to-end", "Pending"),
        ("Soak", "60 min; memory growth ≤100 MB after warm-up", "Not performed", "Pending"),
        ("Representative usability", "≥5 operators plus success/time/state gates", "Not performed", "Pending"),
    ],
    widths=[1900, 2400, 3000, 2060],
    source="config/live_sensor_release_gates.json [E10] and [E04-E05, E27-E28].",
)


report.heading("Appendix C. Data and Artifact Inventory", 1)
report.table(
    "Primary data populations",
    ["Population", "Sessions", "Frames", "Role", "Integrity/status"],
    [
        ("Manual Calibration (2).zip", "83", "29,318", "54 primary TEST sessions; 11 no-contact; 18 replay; only source for application modeling and general characterization", "SHA-256 e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a; 1,283,430,583 bytes"),
        ("Auto Calibration (2).zip", "162", "149,858", "Hysteresis and creep-related descriptive characterization only", "SHA-256 f81a41b635aa2b893e9fa1faff0bda2db41ad0717d8c134fb6d907e1471e344d; 31,171,632,642 bytes"),
        ("Immutable feature store", "245", "179,176", "Exact decoded-video optical feature source for downstream analysis", "0 missing; 0 unmatched; source archive hashes unchanged"),
        ("v4 modeling subset", "54", "102 events", "Six complete TEST-group outer folds", "Manual primary sessions only; post-hoc outer-fold reuse"),
        ("Dedicated no-contact", "11", "Frame replay", "Warm-up/no-contact FPR evidence and noise/drift characterization", "Manual archive; session-independent evidence"),
        ("Physical accepted session", "1", "161 camera/master frames; 125 load samples", "Acquisition/synchronization demonstration", "0 missing/invalid synchronized rows; no clear optical localization response"),
    ],
    widths=[1600, 850, 1000, 3600, 2310],
    source="[E06, E23-E27]. Archive hashes/bytes are from immutable audit manifests.",
)
report.table(
    "Current authoritative export schemas",
    ["Schema/product", "Current columns/rows", "Notes"],
    [
        ("frame_features.csv", len(FRAME_FEATURE_COLUMNS), "Frame metadata, reference/sync, processing state, nine-ROI raw/derived/live-model fields"),
        ("master.csv", len(MASTER_COLUMNS), "Frame feature schema plus synchronized/provenance fields used by final trial product"),
        ("loadcell_raw.csv", len(LOADCELL_RAW_COLUMNS), "Raw serial identity/timing/counts, calibration/tare/force, integrity and motion provenance"),
        ("All-export union", 404, "Union exposed by the current export/data-dictionary scope; sparse fields remain explicit"),
        ("current_schema_dictionary.csv", len(data_dictionary_rows()), "One row per scoped output/field definition; 812 current rows"),
        ("Historical schema checkpoint", "647 then 734 rows", "Dated intermediate counts preserved in earlier documentation; superseded by the current 812-row export"),
    ],
    widths=[2500, 1700, 5160],
    source="Current data/schemas.py [E20] and generated current_schema_dictionary.csv.",
)


report.heading("Appendix D. Evidence Register", 1)
report.paragraph(
    "Evidence IDs used in the body map to current workspace-relative paths below. The CSV companion contains the same register for machine ingestion. Historical evidence is intentionally labeled so that it cannot silently override current decisions."
)
report.table(
    "Evidence ID to artifact map",
    ["ID", "Workspace-relative path", "Use/status"],
    [(evidence_id, path, f"{use}; {status}") for evidence_id, path, use, status in EVIDENCE],
    widths=[650, 4350, 4360],
    source=EVIDENCE_CSV_PATH.name,
)


report.heading("Appendix E. Current v4 Bundle Details", 1)
report.table(
    "Current model contract",
    ["Component", "Frozen setting"],
    [
        ("Bundle status", "experimental; model_eligible_under_authoritative_plan=false; validated_claim_allowed=false"),
        ("Camera inputs", "Nine signed_delta_v_sum plus nine active_fraction features in exact row-major ROI order"),
        ("Forbidden live/model inputs", "Load-cell reference, printer state, automatic archive, or any substituted layout/feature order"),
        ("Temporal preprocessing", "Per-ROI robust normalization followed by causal nine-frame moving mean"),
        ("Warm-up", "At least 120 unloaded frames"),
        ("Global contact threshold", "max(bundle fallback 0.596388, warm-up q99 of contact score)"),
        ("Independent rod threshold", "max(4.0, warm-up q99.9 + 1.0); release ratio 0.60"),
        ("Debounce", "Two frames to acquire contact and two to clear"),
        ("Localization", "Event accumulation of positive normalized active-fraction evidence; forced argmax"),
        ("Selective confidence", "Withhold single-ROI label below 0.374959 unless independent rod gates identify active rods"),
        ("Switching", "Two-frame maintained single-ROI switch"),
        ("Force input", "Filtered normalized signed spatial range"),
        ("Force mapping", "32-knot nondecreasing isotonic piecewise-linear interpolation"),
        ("Out-of-support force", "Unavailable; no clipping or extrapolation"),
        ("Multi-contact force", "Withheld"),
    ],
    widths=[2850, 6510],
    source="[E12, E18, E27-E30].",
)
report.table(
    "Isotonic force-model knots",
    ["Knot", "Optical score x", "Approximate force y (N)"],
    [
        (index, f"{x_value:.9f}", f"{y_value:.9f}")
        for index, (x_value, y_value) in enumerate(
            zip(force_model["x_thresholds"], force_model["y_thresholds_N"]), 1
        )
    ],
    widths=[1100, 3900, 4360],
    source="force_model.json [E29]. Interpolation is allowed only within the first and last x knot.",
)


report.heading("Appendix F. ROI Geometry and Source Map", 1)
report.table(
    "Canonical nine-ROI layout after rotate-90°-clockwise and horizontal mirror",
    ["ROI", "x", "y", "width", "height"],
    [
        (1, 98, 161, 82, 89),
        (2, 197, 156, 76, 99),
        (3, 312, 154, 58, 97),
        (4, 100, 268, 73, 89),
        (5, 204, 264, 76, 96),
        (6, 314, 260, 66, 94),
        (7, 104, 383, 73, 77),
        (8, 194, 394, 87, 66),
        (9, 312, 379, 61, 77),
    ],
    widths=[1600, 1940, 1940, 1940, 1940],
    source="Frozen layout hash/ID roi-9fbc67c50ee3bc7145fa [E02, E12, E19]. Raw 640×480 becomes processed 480×640.",
)
report.table(
    "Key source-code map for methods writing",
    ["Method topic", "Primary source"],
    [
        ("Application entry and CLI", "spare_calibration_gui/app.py"),
        ("Default device/processing configuration", "spare_calibration_gui/config/default_config.json"),
        ("Optical feature extraction", "spare_calibration_gui/processing/feature_extraction.py"),
        ("Baseline capture/drift", "spare_calibration_gui/processing/baseline.py"),
        ("Load-cell calibration", "spare_calibration_gui/processing/loadcell_calibration.py"),
        ("Online synchronization", "spare_calibration_gui/processing/synchronization.py"),
        ("Offline timestamp alignment", "spare_calibration_gui/core/timestamp_alignment.py"),
        ("v4 live inference", "spare_calibration_gui/core/experimental_live_sensor.py"),
        ("Live Sensor GUI tab", "spare_calibration_gui/gui/live_sensor_tab.py"),
        ("Live Sensor event-report export", "spare_calibration_gui/core/live_sensor_report.py"),
        ("Scientific-state/layout contracts", "spare_calibration_gui/core/live_sensor_contracts.py"),
        ("Schemas/data dictionary", "spare_calibration_gui/data/schemas.py"),
        ("Incremental recorder/finalization", "spare_calibration_gui/services/session_recorder.py"),
        ("HX711 firmware", "spare_calibration_gui/arduino/hx711_nano_stream/hx711_nano_stream.ino"),
        ("Model bundle", "spare_calibration_gui/models/live_sensor_experimental_hybrid_v4/"),
        ("Characterization tables", "spare_calibration_gui/analysis_outputs/live_sensor_study/characterization/evidence-products-manual-first-authoritative/tables/"),
        ("Current tests", "spare_calibration_gui/tests/"),
    ],
    widths=[3500, 5860],
    source="Evidence map [E09-E22, E26, E35].",
)


def build_validation_notebook() -> None:
    """Create a small dependency-light notebook and embed verified outputs."""
    cells: list[dict[str, Any]] = []

    def markdown(source: str) -> None:
        cells.append(
            {
                "cell_type": "markdown",
                "id": f"cell-{len(cells) + 1:02d}",
                "metadata": {},
                "source": [line + "\n" for line in source.splitlines()],
            }
        )

    execution_namespace: dict[str, Any] = {}
    execution_count = 0

    def code(source: str) -> None:
        nonlocal execution_count
        execution_count += 1
        compile(source, f"notebook-cell-{execution_count}", "exec")
        stdout = io.StringIO()
        error: dict[str, Any] | None = None
        try:
            from contextlib import redirect_stdout

            with redirect_stdout(stdout):
                exec(source, execution_namespace)
        except Exception as exc:  # pragma: no cover - causes build failure below
            error = {
                "output_type": "error",
                "ename": type(exc).__name__,
                "evalue": str(exc),
                "traceback": [f"{type(exc).__name__}: {exc}"],
            }
        outputs: list[dict[str, Any]] = []
        if stdout.getvalue():
            outputs.append(
                {
                    "output_type": "stream",
                    "name": "stdout",
                    "text": stdout.getvalue().splitlines(keepends=True),
                }
            )
        if error is not None:
            outputs.append(error)
            raise RuntimeError(f"Notebook execution failed: {error}")
        cells.append(
            {
                "cell_type": "code",
                "execution_count": execution_count,
                "id": f"cell-{len(cells) + 1:02d}",
                "metadata": {},
                "outputs": outputs,
                "source": [line + "\n" for line in source.splitlines()],
            }
        )

    markdown(
        "# SPARE report validation companion\n\n"
        "## tl;dr\n\n"
        "This notebook reads the same local authoritative artifacts as the technical report and checks the central data, localization, force, and release-status claims. Outputs were executed and embedded when the report was built."
    )
    code(
        "from pathlib import Path\n"
        "import json\n"
        "import pandas as pd\n\n"
        f"WORKSPACE = Path({str(WORKSPACE)!r})\n"
        "PROJECT = WORKSPACE / 'spare_calibration_gui'\n"
        "MODEL = PROJECT / 'models' / 'live_sensor_experimental_hybrid_v4'\n"
        "CHAR = PROJECT / 'analysis_outputs' / 'live_sensor_study' / 'characterization' / 'evidence-products-manual-first-authoritative'\n"
        "FEATURE = PROJECT / 'analysis_outputs' / 'live_sensor_study' / 'feature_store' / 'feature-store-c0ec762f1dc888a7'\n\n"
        "def read_json(path):\n"
        "    return json.loads(path.read_text(encoding='utf-8'))\n\n"
        "metrics = read_json(MODEL / 'metrics.json')\n"
        "release = read_json(MODEL / 'release_decision.json')\n"
        "reconciliation = read_json(FEATURE / 'reconciliation_report.json')\n"
        "metric_status = pd.read_csv(CHAR / 'tables' / 'metric_status.csv')\n"
        "print('Workspace:', WORKSPACE)\n"
        "print('Artifacts loaded:', MODEL.name, CHAR.name, FEATURE.name)"
    )
    markdown(
        "## Context & Methods\n\n"
        "The application model is manual-only and camera-only. The automatic archive is allowed only for hysteresis/creep-related characterization. Independent sessions, not frames, define replication. The six TEST-group folds are post-hoc because they had been inspected during recovery."
    )
    code(
        "assert release['status'] == 'experimental'\n"
        "assert release['validated_claim_allowed'] is False\n"
        "assert release['force_resolution_established'] is False\n"
        "print('Release status:', release['status'])\n"
        "print('Validated claim allowed:', release['validated_claim_allowed'])\n"
        "print('Force resolution established:', release['force_resolution_established'])\n"
        "print('Blocking reasons:')\n"
        "for reason in release['blocking_reasons']:\n"
        "    print('-', reason)"
    )
    markdown("## Data\n\nThe immutable feature store reconciles the complete decoded video population.")
    code(
        "assert reconciliation['session_count'] == 245\n"
        "assert reconciliation['total_rows'] == 179176\n"
        "assert reconciliation['missing_video_frames'] == 0\n"
        "assert reconciliation['unmatched_video_frames'] == 0\n"
        "assert len(metric_status) == 18\n"
        "print('Sessions:', reconciliation['session_count'])\n"
        "print('Decoded frames:', reconciliation['total_rows'])\n"
        "print('Missing/unmatched:', reconciliation['missing_video_frames'], reconciliation['unmatched_video_frames'])\n"
        "print('Characterization metric-status rows:', len(metric_status))"
    )
    markdown("## Results\n\nHeadline results are printed with the claim-limiting force diagnostics.")
    code(
        "a = metrics['aggregate']\n"
        "print(f\"Localization macro F1: {a['event_localization_macro_f1']:.4%}\")\n"
        "print(f\"Localization accuracy: {a['event_localization_accuracy']:.4%}\")\n"
        "print('Events / sessions / folds:', a['event_count'], a['independent_session_count'], a['fold_count'])\n"
        "print(f\"Selective F1 / coverage: {a['selective_localization_macro_f1']:.4%} / {a['selective_localization_coverage']:.4%}\")\n"
        "print(f\"Conditional force MAE: {a['mean_conditional_force_mae_N']:.6f} N\")\n"
        "print(f\"p95 absolute error: {a['cross_validated_p95_absolute_error_N']:.6f} N\")\n"
        "print(f\"Mean force Spearman rho: {a['mean_force_spearman_rho']:.6f}\")\n"
        "print(f\"Expanded gain over constant: {a['expanded_force_search_relative_gain_over_constant']:.4%}\")\n"
        "assert a['event_count'] == 102 and a['independent_session_count'] == 54 and a['fold_count'] == 6\n"
        "assert abs(a['event_localization_macro_f1'] - 0.9342785558588881) < 1e-12\n"
        "assert abs(a['mean_conditional_force_mae_N'] - 0.32798681555113973) < 1e-12"
    )
    markdown(
        "## Live Sensor GUI contract\n\n"
        "The tenth application tab is a guarded camera-only presentation with an explicit warning, unloaded warm-up, replay, event capture, and post-processing export."
    )
    code(
        "ui_source = (PROJECT / 'gui' / 'live_sensor_tab.py').read_text(encoding='utf-8')\n"
        "report_source = (PROJECT / 'core' / 'live_sensor_report.py').read_text(encoding='utf-8')\n"
        "spec = read_json(PROJECT / 'config' / 'manual_only_experimental_hybrid_v4.json')\n"
        "assert 'EXPERIMENTAL — NOT VALIDATED' in ui_source\n"
        "assert 'Unloaded warm-up: %v/%m frames' in ui_source\n"
        "assert 'Export post-processing report' in ui_source\n"
        "assert 'post_processing_report.html' in report_source\n"
        "assert spec['deployment_inputs']['load_cell'] == 'forbidden'\n"
        "assert spec['deployment_inputs']['printer'] == 'forbidden'\n"
        "print('Sensor mode:', spec['sensor_mode'])\n"
        "print('GUI safeguards: warning + 120-frame warm-up + replay + HTML/CSV/JSON export')\n"
        "print('Runtime load cell/printer:', spec['deployment_inputs']['load_cell'], '/', spec['deployment_inputs']['printer'])"
    )
    markdown(
        "## Takeaways\n\n"
        "The artifact checks support a physically exercised acquisition/calibration program and strong retrospective discrete localization. They do not support a validated force sensor, force resolution, extrapolation, true multi-contact force, or prospective model claim."
    )
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": f"{sys.version_info.major}.{sys.version_info.minor}"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    NOTEBOOK_PATH.write_text(json.dumps(notebook, indent=2, ensure_ascii=False), encoding="utf-8")
    parsed = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))
    if parsed["nbformat"] != 4 or not parsed["cells"]:
        raise RuntimeError("Notebook structural validation failed")
    if any(output.get("output_type") == "error" for cell in parsed["cells"] if cell["cell_type"] == "code" for output in cell["outputs"]):
        raise RuntimeError("Notebook contains an execution error")


build_validation_notebook()
report.save()

# Complete machine-readable QA after all artifacts exist.
validation_payload = json.loads(VALIDATION_JSON_PATH.read_text(encoding="utf-8"))
validation_payload["report_structure"] = {
    "tables": report.table_number,
    "figures": report.figure_number,
    "evidence_items": len(EVIDENCE),
    "schema_dictionary_rows": len(data_dictionary_rows()),
    "notebook_cells": len(json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))["cells"]),
}
validation_payload["outputs"] = {
    path.name: {"sha256": sha256(path), "bytes": path.stat().st_size}
    for path in [DOCX_PATH, MD_PATH, NOTEBOOK_PATH, EVIDENCE_CSV_PATH, SCHEMA_CSV_PATH, CHART_MAP_PATH]
}
validation_payload["all_checks_passed"] = all(item["passed"] for item in validation_checks)
VALIDATION_JSON_PATH.write_text(json.dumps(validation_payload, indent=2, ensure_ascii=False), encoding="utf-8")

print(f"Created: {DOCX_PATH}")
print(f"Created: {MD_PATH}")
print(f"Created: {NOTEBOOK_PATH}")
print(f"Validation: {VALIDATION_JSON_PATH}")
print(f"Tables: {report.table_number}; Figures: {report.figure_number}; Checks: {len(validation_checks)} passed")
