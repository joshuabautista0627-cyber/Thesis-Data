"""Safety, state-machine, replay, and GUI tests for the recovery bundle."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from core.experimental_live_sensor import (
    ExperimentalBundleError,
    ExperimentalLiveSensorEngine,
    LiveSensorResult,
    load_experimental_bundle,
)
from gui.live_sensor_tab import LiveSensorTab


PROJECT = Path(__file__).resolve().parents[1]
BUNDLE = PROJECT / "models" / "live_sensor_experimental_manual_recovery_v2"
LEGACY_BUNDLE = PROJECT / "models" / "live_sensor_experimental_manual_recovery_v1"
EVENT_BUNDLE = PROJECT / "models" / "live_sensor_experimental_event_signal_v3"
HYBRID_BUNDLE = PROJECT / "models" / "live_sensor_experimental_hybrid_v4"


def _replay_rows() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (BUNDLE / "replay_demo.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event_replay_rows() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (EVENT_BUNDLE / "replay_demo.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]


def _hybrid_replay_rows() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (HYBRID_BUNDLE / "replay_demo.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]


def test_bundle_is_hash_verified_and_cannot_claim_validation() -> None:
    bundle = load_experimental_bundle(BUNDLE)
    assert bundle.status == "experimental"
    assert bundle.release_decision["validated_claim_allowed"] is False
    assert bundle.release_decision["model_eligible_under_authoritative_plan"] is False
    assert bundle.metrics["aggregate"]["max_no_contact_frame_fpr"] <= 0.05
    assert bundle.metrics["aggregate"]["min_contact_recall"] >= 0.90
    assert bundle.metrics["aggregate"]["forced_localization_macro_f1"] >= 0.80
    assert bundle.metrics["aggregate"]["force_resolution_established"] is False
    assert bundle.release_decision["numerical_recovery_checks"][
        "force_resolution_established"
    ] is False


def test_v2_metrics_improve_runtime_equivalent_results_without_hiding_coverage() -> None:
    v2 = load_experimental_bundle(BUNDLE).metrics["aggregate"]
    v1 = load_experimental_bundle(LEGACY_BUNDLE).metrics["aggregate"]
    assert v2["mean_displayed_force_mae_N"] < v1["mean_displayed_force_mae_N"]
    assert v2["forced_localization_macro_f1"] > v1["forced_localization_macro_f1"]
    assert 0.0 < v2["selective_localization_coverage"] < 1.0
    assert (
        v2["selective_localization_macro_f1"]
        > v2["forced_localization_macro_f1"]
    )


def test_legacy_v1_bundle_remains_loadable() -> None:
    bundle = load_experimental_bundle(LEGACY_BUNDLE)
    assert bundle.roi_switch_frames == 1
    assert bundle.selective_localization is False


def test_bundle_loader_rejects_tampered_model(tmp_path: Path) -> None:
    copied = tmp_path / "bundle"
    shutil.copytree(BUNDLE, copied)
    force_path = copied / "force_model.json"
    force_path.write_text(force_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ExperimentalBundleError, match="size check|hash check"):
        load_experimental_bundle(copied)


def test_engine_requires_context_and_unloaded_warmup() -> None:
    bundle = load_experimental_bundle(BUNDLE)
    engine = ExperimentalLiveSensorEngine(bundle)
    row = _replay_rows()[0]
    assert engine.process(row).state == "SETUP_BLOCKED"
    engine.set_context_ready(True, "test-baseline")
    assert engine.process(row).state == "SETUP_BLOCKED"
    engine.start_unloaded_warmup()
    states = [engine.process(item).state for item in _replay_rows()]
    assert states.count("WARMING_UP") == bundle.minimum_warmup_frames - 1
    assert "READY" in states
    assert "NO_CONTACT" in states
    assert "EXPERIMENTAL_CONTACT" in states
    assert "EXPERIMENTAL_CONTACT_UNCERTAIN_ROI" in states


def test_roi_switch_requires_two_consecutive_frames() -> None:
    engine = ExperimentalLiveSensorEngine(load_experimental_bundle(BUNDLE))
    observed = [engine._update_roi_state(value) for value in [1, 2, 1, 2, 2]]
    assert observed == [1, 1, 1, 1, 2]


def test_replay_is_deterministic_and_never_uses_loadcell_input() -> None:
    bundle = load_experimental_bundle(BUNDLE)

    def run() -> list[tuple[str, float | None, int | None, int | None]]:
        engine = ExperimentalLiveSensorEngine(bundle)
        engine.set_context_ready(True, "replay")
        engine.start_unloaded_warmup()
        output = []
        for row in _replay_rows():
            row["force_N"] = 9999.0
            result = engine.process(row)
            output.append((result.state, result.force_N, result.roi, result.forced_roi))
        return output

    first = run()
    second = run()
    assert first == second
    forces = [
        value for state, value, _, _ in first if state.startswith("EXPERIMENTAL_CONTACT")
    ]
    assert forces
    assert all(value is not None and bundle.force_min_N <= value <= bundle.force_max_N for value in forces)


def test_live_sensor_tab_runs_bundled_replay_and_keeps_warning_visible(qtbot) -> None:
    tab = LiveSensorTab(BUNDLE)
    qtbot.addWidget(tab)
    tab.show()
    assert tab.warning.isVisible()
    assert "NOT VALIDATED" in tab.warning.text()
    tab.start_demo()
    tab._demo_timer.stop()
    while tab._demo_mode:
        tab._advance_demo()
    assert tab._last_result is not None
    assert tab._last_result.state.startswith("EXPERIMENTAL_CONTACT")
    assert tab.force_label.text().startswith("~")
    if tab._last_result.roi is None:
        assert "withheld" in tab.roi_label.text()
    assert "Replay" in tab.message_label.text()


def test_event_bundle_has_no_force_model_and_meets_disclosed_recovery_gates() -> None:
    bundle = load_experimental_bundle(EVENT_BUNDLE)
    aggregate = bundle.metrics["aggregate"]
    assert bundle.sensor_mode == "event_signal"
    assert bundle.event_signal_unit == "threshold_ratio"
    assert bundle.force_x is None
    assert bundle.force_y is None
    assert bundle.force_min_N is None
    assert bundle.force_max_N is None
    assert bundle.p95_error_N is None
    assert bundle.required_roi_layout_id == "roi-9fbc67c50ee3bc7145fa"
    assert (bundle.required_frame_width, bundle.required_frame_height) == (480, 640)
    assert bundle.required_rotation_degrees == 90
    assert bundle.required_mirror_horizontal is True
    assert not (EVENT_BUNDLE / "force_model.json").exists()
    assert (EVENT_BUNDLE / "event_model.json").is_file()
    assert aggregate["force_output_available"] is False
    assert aggregate["max_no_contact_frame_fpr"] <= 0.05
    assert aggregate["min_contact_recall"] >= 0.90
    assert aggregate["event_localization_macro_f1"] >= 0.891
    assert 0.80 <= aggregate["selective_localization_coverage"] < 1.0
    assert aggregate["selective_localization_macro_f1"] >= aggregate[
        "event_localization_macro_f1"
    ]
    assert bundle.release_decision["validated_claim_allowed"] is False
    assert bundle.release_decision["force_output_available"] is False


def test_event_bundle_loader_rejects_tampered_event_model(tmp_path: Path) -> None:
    copied = tmp_path / "event_bundle"
    shutil.copytree(EVENT_BUNDLE, copied)
    event_path = copied / "event_model.json"
    event_path.write_text(
        event_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
    )
    with pytest.raises(ExperimentalBundleError, match="size check|hash check"):
        load_experimental_bundle(copied)


def test_event_replay_is_deterministic_event_level_and_never_outputs_newtons() -> None:
    bundle = load_experimental_bundle(EVENT_BUNDLE)

    def run() -> list:
        engine = ExperimentalLiveSensorEngine(bundle)
        engine.set_context_ready(True, "event-replay")
        engine.start_unloaded_warmup()
        output = []
        for original in _event_replay_rows():
            row = dict(original)
            row["force_N"] = 9999.0
            output.append(engine.process(row))
        return output

    first = run()
    second = run()
    first_signature = [
        (
            item.state,
            item.event_id,
            item.roi,
            item.forced_roi,
            item.peak_signal_ratio,
            item.event_start_frame_id,
            item.event_end_frame_id,
            item.force_N,
        )
        for item in first
    ]
    second_signature = [
        (
            item.state,
            item.event_id,
            item.roi,
            item.forced_roi,
            item.peak_signal_ratio,
            item.event_start_frame_id,
            item.event_end_frame_id,
            item.force_N,
        )
        for item in second
    ]
    assert first_signature == second_signature
    assert sum(item.state == "WARMING_UP" for item in first) == (
        bundle.minimum_warmup_frames - 1
    )
    assert any(item.state == "EVENT_ACTIVE" for item in first)
    completed = [item for item in first if item.event_phase == "completed"]
    assert len(completed) == 1
    event = completed[0]
    assert event.state == "EVENT_COMPLETE"
    assert event.contact is False
    assert event.roi == event.forced_roi
    assert event.peak_signal_ratio is not None and event.peak_signal_ratio > 1.0
    assert event.event_start_frame_id is not None
    assert event.event_end_frame_id is not None
    assert event.event_start_frame_id < event.event_end_frame_id < event.frame_id
    first_active = next(item for item in first if item.event_phase == "active")
    assert first_active.event_start_frame_id is not None
    assert first_active.event_start_frame_id < first_active.frame_id
    assert all(item.force_N is None for item in first)


def test_event_gui_persists_last_completed_event_without_newton_formatting(qtbot) -> None:
    tab = LiveSensorTab(EVENT_BUNDLE)
    qtbot.addWidget(tab)
    tab.show()
    assert "Newton-valued force is unavailable" in tab.warning.text()
    assert tab.result_group.title() == "Current / last optical event"
    assert tab.roi_group.title() == "Detected rod ROI(s)"
    tab.start_demo()
    tab._demo_timer.stop()
    while tab._demo_mode:
        tab._advance_demo()
    assert tab._last_result is not None
    assert tab._last_result.state == "NO_CONTACT"
    assert tab._last_completed_result is not None
    assert tab._last_completed_result.state == "EVENT_COMPLETE"
    assert tab._last_completed_result.force_N is None
    assert "× threshold" in tab.force_label.text()
    assert " N" not in tab.force_label.text()
    assert tab._last_completed_result.roi is not None
    assert str(tab._last_completed_result.roi) in tab.roi_label.text()
    assert "Replay" in tab.message_label.text()


def test_event_gui_blocks_noncanonical_live_baseline(qtbot) -> None:
    tab = LiveSensorTab(EVENT_BUNDLE)
    qtbot.addWidget(tab)
    tab.set_baseline_ready(True, "baseline-wrong", "roi-wrong", 0, False)
    assert not tab.warmup_button.isEnabled()
    assert tab.engine is not None and not tab.engine.context_ready
    assert "Baseline rejected for v3" in tab.message_label.text()
    tab.set_baseline_ready(
        True,
        "baseline-canonical",
        "roi-9fbc67c50ee3bc7145fa",
        90,
        True,
    )
    assert tab.warmup_button.isEnabled()
    assert tab.engine.context_ready


def test_hybrid_bundle_combines_v2_force_with_v3_event_localization() -> None:
    bundle = load_experimental_bundle(HYBRID_BUNDLE)
    aggregate = bundle.metrics["aggregate"]
    assert bundle.sensor_mode == "hybrid_force_event"
    assert bundle.force_x is not None and bundle.force_y is not None
    assert bundle.event_signal_unit == "threshold_ratio"
    assert bundle.required_roi_layout_id == "roi-9fbc67c50ee3bc7145fa"
    assert (HYBRID_BUNDLE / "force_model.json").is_file()
    assert (HYBRID_BUNDLE / "event_model.json").is_file()
    assert aggregate["force_output_available"] is True
    assert aggregate["force_output_status"] == "approximate_unvalidated"
    assert aggregate["force_resolution_established"] is False
    assert aggregate["event_localization_macro_f1"] >= 0.93
    assert aggregate["mean_conditional_force_mae_N"] < 0.33
    assert bundle.release_decision["validated_claim_allowed"] is False
    assert bundle.release_decision["physical_validation_complete"] is False
    manifest = json.loads((HYBRID_BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["inference_inputs"] == {
        "camera_roi_features": ["signed_delta_v_sum", "active_fraction"],
        "load_cell": "forbidden",
        "printer": "forbidden",
    }


def test_independent_roi_gate_represents_every_possible_press_set() -> None:
    engine = ExperimentalLiveSensorEngine(load_experimental_bundle(HYBRID_BUNDLE))
    thresholds = np.asarray(engine.roi_activation_thresholds)
    for bitmask in range(1 << 9):
        expected = tuple(
            roi for roi in range(1, 10) if bitmask & (1 << (roi - 1))
        )
        values = thresholds - 0.25
        values = values.copy()
        for roi in expected:
            values[roi - 1] = thresholds[roi - 1] + 0.25
        assert engine._thresholded_rois(values) == expected


def test_hybrid_engine_detects_simultaneous_rods_without_signed_argmax() -> None:
    bundle = load_experimental_bundle(HYBRID_BUNDLE)
    engine = ExperimentalLiveSensorEngine(bundle)
    engine.set_context_ready(True, "multi-press-test")
    engine.start_unloaded_warmup()
    warmup = _hybrid_replay_rows()[: bundle.minimum_warmup_frames]
    for row in warmup:
        ready = engine.process(row)
    assert ready.state == "READY"
    assert min(engine.roi_activation_thresholds) >= 4.0

    def synthetic_row(frame_id: int, active_rois: set[int]) -> dict[str, float | int]:
        row: dict[str, float | int] = {"capture_frame_id": frame_id}
        # Identical signed responses have zero spatial range, so this verifies
        # that independent rod activity—not the old single-label argmax—starts
        # and localizes the event.
        for index, column in enumerate(bundle.signed_columns):
            row[column] = float(bundle.signed_center[index])
        for index, column in enumerate(bundle.active_columns):
            normalized = 12.0 if index + 1 in active_rois else 0.0
            row[column] = float(
                bundle.active_center[index]
                + bundle.active_scale[index] * normalized
            )
        return row

    active_results = [
        engine.process(synthetic_row(frame_id, {2, 5, 9}))
        for frame_id in range(200, 215)
    ]
    localized = [result for result in active_results if result.contact]
    assert localized
    assert all(result.displayed_rois == (2, 5, 9) for result in localized)
    assert all(result.force_N is None for result in localized)
    assert all(result.force_status == "multi_press_unavailable" for result in localized)

    clear_results = [
        engine.process(synthetic_row(frame_id, set()))
        for frame_id in range(215, 235)
    ]
    completed = [result for result in clear_results if result.event_phase == "completed"]
    assert len(completed) == 1
    assert completed[0].displayed_rois == (2, 5, 9)
    assert completed[0].all_forced_rois == (2, 5, 9)
    assert completed[0].peak_force_N is None
    assert completed[0].force_status == "multi_press_unavailable"


def test_hybrid_replay_is_deterministic_and_reports_event_peak_force() -> None:
    bundle = load_experimental_bundle(HYBRID_BUNDLE)

    def run() -> list:
        engine = ExperimentalLiveSensorEngine(bundle)
        engine.set_context_ready(True, "hybrid-replay")
        engine.start_unloaded_warmup()
        output = []
        for original in _hybrid_replay_rows():
            row = dict(original)
            row["force_N"] = 9999.0
            frame_id = int(row["capture_frame_id"])
            for roi in range(1, 10):
                row[f"roi{roi}_max_v"] = 100.0 + roi + 0.1 * frame_id
                row[f"roi{roi}_delta_v_max"] = 5.0 + roi + 0.01 * frame_id
            output.append(engine.process(row))
        return output

    first = run()
    second = run()
    signature = lambda values: [
        (
            item.state,
            item.force_N,
            item.peak_force_N,
            item.force_status,
            item.roi,
            item.forced_roi,
            item.peak_hsv_v,
            item.force_at_peak_hsv_v_N,
        )
        for item in values
    ]
    assert signature(first) == signature(second)
    active = [item for item in first if item.event_phase == "active"]
    assert active and any(item.force_N is not None for item in active)
    assert all(
        item.force_N is None
        or bundle.force_min_N <= item.force_N <= bundle.force_max_N
        for item in active
    )
    completed = [item for item in first if item.event_phase == "completed"]
    assert len(completed) == 1
    assert completed[0].state == "EVENT_COMPLETE"
    assert completed[0].peak_force_N is not None
    assert bundle.force_min_N <= completed[0].peak_force_N <= bundle.force_max_N
    assert completed[0].force_status == "available"
    assert completed[0].roi == completed[0].forced_roi
    assert completed[0].peak_hsv_v is not None
    assert completed[0].peak_delta_v is not None
    assert completed[0].force_at_peak_hsv_v_N is not None
    assert bundle.force_min_N <= completed[0].force_at_peak_hsv_v_N <= bundle.force_max_N


def test_hybrid_inference_is_independent_of_load_cell_and_printer_fields() -> None:
    bundle = load_experimental_bundle(HYBRID_BUNDLE)

    def run(extra: dict[str, object]) -> list[tuple[object, ...]]:
        engine = ExperimentalLiveSensorEngine(bundle)
        engine.set_context_ready(True, "camera-only-replay")
        engine.start_unloaded_warmup()
        signature: list[tuple[object, ...]] = []
        for original in _hybrid_replay_rows():
            row = {**original, **extra}
            result = engine.process(row)
            signature.append(
                (
                    result.state,
                    result.force_N,
                    result.peak_force_N,
                    result.roi,
                    result.forced_roi,
                    result.contact_score,
                )
            )
        return signature

    camera_only = run({})
    unrelated_hardware = run(
        {
            "force_N": 999999.0,
            "reference_force_corrected_N": -999999.0,
            "loadcell_calibrated": True,
            "printer_connected": True,
            "printer_z_mm": 12345.0,
        }
    )
    assert camera_only == unrelated_hardware


def test_hybrid_gui_holds_completed_peak_and_discloses_limits(qtbot) -> None:
    tab = LiveSensorTab(HYBRID_BUNDLE)
    qtbot.addWidget(tab)
    tab.show()
    assert "Force is an approximate optical estimate" in tab.warning.text()
    assert "force resolution was not established" in tab.warning.text()
    assert tab.result_group.title() == "Current / last force event"
    tab.start_demo()
    tab._demo_timer.stop()
    while tab._demo_mode:
        tab._advance_demo()
    assert tab._last_result is not None and tab._last_result.state == "NO_CONTACT"
    assert tab._last_completed_result is not None
    assert tab._last_completed_result.peak_force_N is not None
    assert tab.force_label.text().startswith("~")
    assert "N peak" in tab.force_label.text()
    assert tab._last_completed_result.roi is not None


def test_live_gui_highlights_every_simultaneously_detected_rod(qtbot) -> None:
    tab = LiveSensorTab(HYBRID_BUNDLE)
    qtbot.addWidget(tab)
    result = LiveSensorResult(
        frame_id=42,
        state="EVENT_ACTIVE",
        message="multi",
        contact=True,
        force_N=None,
        roi=1,
        contact_score=1.0,
        threshold=0.5,
        confidence="multi",
        forced_roi=1,
        sensor_mode="hybrid_force_event",
        event_id=1,
        event_phase="active",
        force_status="multi_press_unavailable",
        rois=(1, 4, 8),
        forced_rois=(1, 4, 8),
    )
    tab._render(result)
    assert tab.roi_label.text() == "ROIs: 1, 4, 8 (multi)"
    highlighted = {
        index
        for index, cell in enumerate(tab.roi_cells, start=1)
        if "#198754" in cell.styleSheet()
    }
    assert highlighted == {1, 4, 8}
