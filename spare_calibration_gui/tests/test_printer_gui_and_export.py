from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path

import numpy as np
import pytest

from core.models import (
    ApplicationConfig,
    ErrorCode,
    LoadCellSample,
    PrinterConfig,
    RecordingLifecycle,
    TrialLabels,
)
from core.motion import MotionSnapshot
from data.exporter import loadcell_sample_to_row
from data.schemas import FRAME_FEATURE_COLUMNS, LOADCELL_RAW_COLUMNS, MASTER_COLUMNS, MOTION_PROVENANCE_SCHEMA
from gui.printer_motion_tab import PrinterMotionTab
from gui.main_window import MainWindow
from PySide6.QtWidgets import QMessageBox
from processing.baseline import BaselineContext, capture_baseline
from processing.pipeline import ProcessingPipeline
from services.interfaces import CapturedFrame
from services.repeated_press_controller import RepeatedPressConfig, SequenceResult
from services.simulation_service import create_default_roi_layout


def test_printer_tab_defaults_to_com4_115200_and_has_safety_controls(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    assert tab.port_combo.currentText() == "COM4"
    assert tab.baud_spin.value() == 115200
    assert tab.emergency_button.objectName() == "printer_emergency_stop"
    assert "M112" in tab.emergency_button.text()
    assert tab.direction_checkbox.objectName() == "negative_z_direction_confirmation"


def test_printer_tab_exposes_all_required_jog_steps(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    assert [tab.step_combo.itemData(index) for index in range(tab.step_combo.count())] == [
        10.0,
        1.0,
        0.1,
        0.01,
    ]


def test_jog_signal_uses_independent_xy_and_z_feeds(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    tab.xy_feed_spin.setValue(2222.0)
    tab.z_jog_feed_spin.setValue(111.0)
    tab.step_combo.setCurrentIndex(2)
    with qtbot.waitSignal(tab.jog_requested) as x_signal:
        tab._emit_jog("X", 1)
    with qtbot.waitSignal(tab.jog_requested) as z_signal:
        tab._emit_jog("Z", -1)
    assert x_signal.args == ["X", 0.1, 2222.0]
    assert z_signal.args == ["Z", -0.1, 111.0]


def test_motion_actions_require_verified_homing_position_and_zero(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    tab.set_connected(
        {
            "port": "COM4",
            "baud_rate": 115200,
            "firmware_name": "Marlin 2.1.2",
            "firmware_version": "2.1.2",
            "marlin_verified": True,
        }
    )
    assert tab.home_button.isEnabled()
    assert not tab.start_button.isEnabled()
    tab.update_printer_state(
        {
            "position": {
                "tracked_x_mm": 1.0,
                "tracked_y_mm": 2.0,
                "tracked_z_mm": 10.0,
                "reported_x_mm": 1.0,
                "reported_y_mm": 2.0,
                "reported_z_mm": 10.0,
                "tracked_valid": True,
                "reported_valid": True,
                "homed": True,
                "press_zero_z_mm": 10.0,
                "press_zero_valid": True,
            }
        }
    )
    assert tab.start_button.isEnabled()
    assert "Z=10.000" in tab.zero_label.text()
    assert "no G92" in tab.zero_label.text()


def test_manual_printer_operation_disables_overlapping_actions(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    tab.set_connected(
        {
            "port": "COM4",
            "baud_rate": 115200,
            "firmware_name": "Marlin",
            "firmware_version": "1.1.6",
            "marlin_verified": True,
        }
    )
    tab.update_operation({"busy": True, "operation": "homing all axes", "error": ""})
    assert "waiting for Marlin" in tab.operation_label.text()
    assert not tab.home_button.isEnabled()
    assert not tab.query_button.isEnabled()
    assert not tab.disconnect_button.isEnabled()
    assert tab.emergency_button.isEnabled()
    tab.update_operation({"busy": False, "operation": "homing all axes", "error": "timeout"})
    assert "failed" in tab.operation_label.text()
    assert tab.home_button.isEnabled()


def test_home_confirmation_dispatches_when_qt_returns_integer_button(
    qtbot, monkeypatch
) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    window.printer_home_command.disconnect(window.printer_worker.home_all)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: int(QMessageBox.StandardButton.Yes),
    )
    with qtbot.waitSignal(window.printer_home_command):
        window._confirm_and_home_printer()
    assert window.printer_tab._operation_busy
    assert "homing request queued" in window.printer_tab.operation_label.text()
    assert "queued G28" in window.printer_tab.log.toPlainText()


def test_home_confirmation_no_does_not_dispatch(qtbot, monkeypatch) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: int(QMessageBox.StandardButton.No),
    )
    emissions: list[bool] = []
    window.printer_home_command.connect(lambda: emissions.append(True))
    window._confirm_and_home_printer()
    assert emissions == []


def test_profile_never_persists_press_zero_or_direction_confirmation(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    tab.direction_checkbox.setChecked(True)
    profile = tab.profile_values()
    assert profile["press_zero_persisted"] is False
    assert "press_zero_z_mm" not in profile
    tab.apply_profile(profile)
    assert not tab.direction_checkbox.isChecked()
    assert "Press zero was not loaded" in tab.preview_label.text()


def test_test_one_cycle_preserves_settings_but_forces_cycle_count(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    tab.cycles_spin.setValue(12)
    tab.direction_checkbox.setChecked(True)
    with qtbot.waitSignal(tab.test_cycle_requested) as signal:
        tab._test_cycle()
    emitted = signal.args[0]
    assert emitted.cycles == 1
    assert emitted.negative_z_direction_confirmed


def test_emergency_button_emits_without_confirmation_delay(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    tab.set_connected(
        {
            "port": "COM4",
            "baud_rate": 115200,
            "firmware_name": "Marlin",
            "firmware_version": "2.1",
            "marlin_verified": True,
        }
    )
    with qtbot.waitSignal(tab.emergency_stop_requested):
        tab.emergency_button.click()


def test_sequence_result_surfaces_cycles_force_and_error(qtbot) -> None:
    tab = PrinterMotionTab(PrinterConfig())
    qtbot.addWidget(tab)
    result = SequenceResult(
        sequence_id="s",
        generation=1,
        status="aborted",
        configured_cycles=5,
        completed_cycles=2,
        force_limit_exceeded=True,
        maximum_abs_force_N=3.2,
        error="force limit",
        started_monotonic_ns=1,
        finished_monotonic_ns=2,
        events=(),
        commands=(),
    )
    tab.sequence_finished(result)
    assert "2/5" in tab.sequence_label.text()
    assert "3.2" in tab.safety_label.text()
    assert "force limit" in tab.safety_label.text()


def test_repeated_press_starts_new_transaction_from_terminal_recording_state(
    qtbot, monkeypatch
) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    for field_name in window.readiness.REQUIRED_FIELDS:
        setattr(window.readiness, field_name, True)
    # Automated pressing requires a quality-passed calibration but deliberately
    # does not require a separate verification that an optional pre-run tare
    # would immediately invalidate.
    window.readiness.calibration_verified = False
    window._calibration = type("Calibration", (), {"quality_passed": True})()
    window._printer_connection_info = {"port": "COM4"}
    window._set_lifecycle(RecordingLifecycle.COMPLETE)
    config = RepeatedPressConfig(negative_z_direction_confirmed=True)
    monkeypatch.setattr(
        window.printer_worker.controller,
        "preview",
        lambda _config: {
            "press_zero_x_mm": 1.0,
            "press_zero_y_mm": 2.0,
            "press_zero_z_mm": 14.0,
            "target_machine_z_mm": 13.0,
            "estimated_duration_s": 4.0,
        },
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    continued: list[bool] = []
    monkeypatch.setattr(
        window,
        "_continue_printer_sequence_preparation",
        lambda: continued.append(True),
    )

    window._printer_sequence_requested(config)

    assert window.lifecycle is RecordingLifecycle.IDLE
    assert (
        window._pending_printer_sequence == config
    ), window.printer_tab.safety_label.text()
    assert continued == [True]
    assert "Preparing synchronized recording" in window.printer_tab.sequence_label.text()
    window._pending_printer_sequence = None
    window.close()


def test_repeated_press_explains_that_manual_recording_must_not_be_started_first(
    qtbot,
) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    window._set_lifecycle(RecordingLifecycle.RECORDING)

    window._printer_sequence_requested(
        RepeatedPressConfig(negative_z_direction_confirmed=True)
    )

    assert "without pressing Start Recording first" in window.printer_tab.safety_label.text()
    assert window._pending_printer_sequence is None
    window._set_lifecycle(RecordingLifecycle.IDLE)
    window.close()


def test_recorder_started_dispatches_repeated_press_with_prerequisites_first(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    window.printer_prerequisites_command.disconnect(
        window.printer_worker.set_prerequisites
    )
    window.printer_start_command.disconnect(window.printer_worker.start_sequence)
    prerequisite_events: list[tuple[bool, str]] = []
    start_events: list[RepeatedPressConfig] = []
    window.printer_prerequisites_command.connect(
        lambda ready, reason: prerequisite_events.append((ready, reason))
    )
    window.printer_start_command.connect(start_events.append)
    original = RepeatedPressConfig(
        cycles=2,
        pre_roll_s=0.25,
        tare_before_sequence=True,
        fresh_baseline_before_sequence=True,
        negative_z_direction_confirmed=True,
    )
    window._pending_printer_sequence = original
    window._printer_automated_recording = True

    window._recording_started(tmp_path)

    assert prerequisite_events and prerequisite_events[0][0] is True
    assert len(start_events) == 1
    assert start_events[0].cycles == 2
    assert not start_events[0].tare_before_sequence
    assert not start_events[0].fresh_baseline_before_sequence
    assert "Queuing repeated press" in window.printer_tab.log.toPlainText()
    window._pending_printer_sequence = None
    window._printer_automated_recording = False
    window._set_lifecycle(RecordingLifecycle.IDLE)
    window.close()


def test_printer_start_error_clears_pending_sequence_transaction(
    qtbot, monkeypatch
) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    window._pending_printer_sequence = RepeatedPressConfig(
        negative_z_direction_confirmed=True
    )
    window._printer_automated_recording = True
    window._printer_sequence_preparation_stage = "running"
    window._set_lifecycle(RecordingLifecycle.RECORDING)
    stopped: list[bool] = []
    monkeypatch.setattr(window, "_stop_recording", lambda: stopped.append(True))

    window._printer_error("controller start failed")

    assert window._pending_printer_sequence is None
    assert not window._printer_automated_recording
    assert window._printer_sequence_preparation_stage == ""
    assert stopped == [True]
    window._set_lifecycle(RecordingLifecycle.IDLE)
    window.close()


@pytest.mark.parametrize(("component", "message"), [("Camera", "camera failed"), ("Load cell", "COM3 failed")])
def test_camera_or_loadcell_failure_requests_active_printer_sequence_abort(
    qtbot, component: str, message: str
) -> None:
    window = MainWindow(ApplicationConfig.load_json("config/default_config.json"), default_simulation=True)
    qtbot.addWidget(window)
    window._pending_printer_sequence = RepeatedPressConfig(
        negative_z_direction_confirmed=True
    )
    with qtbot.waitSignal(window.printer_abort_command):
        window._worker_error(component, message)
    window._pending_printer_sequence = None


def test_motion_schema_is_present_in_frame_master_and_raw_loadcell() -> None:
    names = tuple(item.column_name for item in MOTION_PROVENANCE_SCHEMA)
    assert names
    assert all(name in FRAME_FEATURE_COLUMNS for name in names)
    assert all(name in MASTER_COLUMNS for name in names)
    assert all(name in LOADCELL_RAW_COLUMNS for name in names)
    assert "printer_reported_z_mm" in names
    assert "force_limit_exceeded" in names


def test_loadcell_row_captures_motion_snapshot_without_fabricating_reported_position() -> None:
    sample = LoadCellSample(
        host_monotonic_ns=10,
        arduino_sample_id=1,
        arduino_micros=2,
        raw_adc=3.0,
        force_gf=4.0,
        force_N=5.0,
        device_session_id="d",
        sequence_id="sequence",
        motion_phase="pressing_down",
        commanded_z_mm=9.0,
        printer_reported_z_mm=math.nan,
        printer_position_valid=False,
    )
    row = loadcell_sample_to_row(sample)
    assert tuple(row) == LOADCELL_RAW_COLUMNS
    assert row["sequence_id"] == "sequence"
    assert row["commanded_z_mm"] == 9.0
    assert math.isnan(row["printer_reported_z_mm"])
    assert row["printer_position_valid"] is False


def _pipeline() -> tuple[ProcessingPipeline, CapturedFrame]:
    app_config = ApplicationConfig.load_json("config/default_config.json")
    layout = create_default_roi_layout(90, 90)
    frame = np.full((90, 90, 3), 20, dtype=np.uint8)
    context = BaselineContext(
        camera_device="test",
        backend="test",
        frame_width=90,
        frame_height=90,
        camera_settings={},
        roi_layout_id=layout.roi_layout_id,
        processing_settings={
            "minimum_saturation_for_h": 10,
            "active_delta_v_threshold": 10.0,
            "localization_min_mean_delta_v": 5.0,
        },
    )
    baseline = capture_baseline(
        [frame] * 20,
        layout.rois,
        context=context,
        minimum_valid_frames=20,
    )
    pipeline = ProcessingPipeline(
        config=app_config,
        roi_layout=layout,
        baseline=baseline,
        baseline_context=context,
        trial_labels=TrialLabels(
            session_id="s",
            trial_id="t",
            sensing_skin_id="skin",
            target_roi_ground_truth=1,
        ),
        requested_fps=30.0,
        actual_fps=30.0,
        camera_backend="test",
        camera_device_index=0,
    )
    captured = CapturedFrame(
        source_frame_id=1,
        host_monotonic_ns=100,
        wall_clock_iso="2026-01-01T00:00:00Z",
        original_bgr=frame,
    )
    return pipeline, captured


def test_frame_row_uses_acquisition_motion_snapshot_exactly() -> None:
    pipeline, captured = _pipeline()
    snapshot = MotionSnapshot(
        sequence_id="seq",
        sequence_generation=4,
        sequence_status="running",
        motion_phase="holding",
        motion_phase_host_monotonic_ns=90,
        motion_cycle_id="cycle",
        motion_cycle_index=2,
        motion_total_cycles=8,
        command_id="command",
        press_zero_z_mm=10.0,
        target_displacement_mm=-1.0,
        target_machine_z_mm=9.0,
        commanded_z_mm=9.0,
        requested_feed_rate_mm_min=60.0,
        printer_reported_z_mm=9.0,
        printer_position_valid=True,
        press_zero_valid=True,
        force_limit_N=3.0,
    )
    processed = pipeline.process(
        captured,
        capture_frame_id=0,
        recording_start_monotonic_ns=100,
        motion_context=snapshot.as_row(),
    )
    row = processed.feature_row
    assert tuple(row) == FRAME_FEATURE_COLUMNS
    assert row["sequence_id"] == "seq"
    assert row["motion_phase"] == "holding"
    assert row["motion_cycle_index"] == 2
    assert row["printer_reported_z_mm"] == 9.0


def test_frame_outside_sequence_has_explicit_idle_false_and_nan_defaults() -> None:
    pipeline, captured = _pipeline()
    row = pipeline.process(
        captured,
        capture_frame_id=0,
        recording_start_monotonic_ns=100,
    ).feature_row
    assert row["sequence_status"] == "idle"
    assert row["motion_phase"] == "idle"
    assert row["press_zero_valid"] is False
    assert row["sequence_aborted"] is False
    assert math.isnan(row["printer_reported_z_mm"])
