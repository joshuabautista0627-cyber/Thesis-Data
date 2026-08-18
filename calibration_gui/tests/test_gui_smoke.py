from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QScrollArea

from core.models import ApplicationConfig, RecordingLifecycle
from gui.main_window import MainWindow
from processing.baseline import check_frame_baseline_drift
from processing.motion_magnification import MotionMagnificationConfig
from processing.loadcell_calibration import (
    build_saved_calibration,
    calculate_calibration,
    verify_calibration,
)
from services.session_recorder import preflight_recording_output


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _fast_config(tmp_path: Path) -> ApplicationConfig:
    root = Path(__file__).resolve().parents[1]
    base = ApplicationConfig.load_json(root / "config" / "default_config.json")
    return replace(
        base,
        camera=replace(
            base.camera,
            requested_width=160,
            requested_height=120,
            requested_fps=30.0,
            warmup_seconds=0.0,
        ),
        processing=replace(
            base.processing,
            baseline_duration_s=0.15,
            minimum_baseline_frames=3,
        ),
        loadcell=replace(base.loadcell, calibration_window_s=0.15),
        recording=replace(
            base.recording,
            output_directory=str(tmp_path),
            minimum_preflight_free_mb=1,
            disk_safety_free_mb=1,
            csv_flush_row_count=2,
        ),
    )


def _calibration():
    unloaded = [100_000 + (index % 3) - 1 for index in range(30)]
    loaded = [120_000 + (index % 3) - 1 for index in range(30)]
    computation = calculate_calibration(unloaded, loaded, 200.0)
    verification = verify_calibration(
        loaded[:20],
        200.0,
        computation.tare_raw,
        computation.counts_per_gram,
    )
    assert verification.passed
    return build_saved_calibration(
        "calibration-gui-smoke",
        computation,
        verification,
        serial_port="SIMULATED",
        firmware_identity="SIMULATED_HX711/synthetic-1.0",
        protocol_version="SIM-1.0",
        firmware_version="synthetic-1.0",
        calibration_timestamp_iso=datetime.now(UTC).isoformat(),
    )


def _feed_sample_window(window: MainWindow, raw_adc: int) -> None:
    start_ns = max(
        time.perf_counter_ns(),
        int(window._sample_collector._start_ns or 0),
    ) + 1_000_000_000
    for index in range(25):
        window._sample_collector.accept(
            SimpleNamespace(
                host_monotonic_ns=start_ns + index * 10_000_000,
                raw_adc=raw_adc + (index % 3) - 1,
            )
        )
    window._refresh_status()


def _complete_user_loadcell_workflow(window: MainWindow, qtbot) -> None:
    """Use public buttons for unloaded, loaded, calculate, tare, and verify."""

    window.serial_worker.set_sample_observer(None)
    assert window.loadcell_tab.capture_unloaded_button.isEnabled()
    window.loadcell_tab.capture_unloaded_button.click()
    _feed_sample_window(window, 100_000)
    qtbot.waitUntil(
        lambda: window.loadcell_tab.workflow_stage == "unloaded", timeout=2_000
    )
    window.loadcell_tab.capture_loaded_button.click()
    _feed_sample_window(window, 120_000)
    qtbot.waitUntil(
        lambda: window.loadcell_tab.workflow_stage == "loaded", timeout=2_000
    )
    window.loadcell_tab.calculate_calibration_button.click()
    qtbot.waitUntil(
        lambda: window._calibration is not None
        and window.loadcell_tab.workflow_stage == "calculated",
        timeout=4_000,
    )
    assert window.readiness.loadcell_calibrated
    assert not window.readiness.calibration_verified
    window.loadcell_tab.tare_button.click()
    _feed_sample_window(window, 100_000)
    qtbot.waitUntil(
        lambda: window.loadcell_tab.workflow_stage == "tared", timeout=4_000
    )
    assert window._calibration is not None
    assert window._calibration.verification is None
    assert not window.readiness.calibration_verified
    window.loadcell_tab.verify_calibration_button.click()
    _feed_sample_window(window, 120_000)
    qtbot.waitUntil(
        lambda: window.loadcell_tab.workflow_stage == "verified"
        and window.readiness.calibration_verified,
        timeout=4_000,
    )


def test_main_window_starts_offscreen_and_remains_navigable(qtbot, tmp_path: Path) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == [
        "Camera View",
        "ROI and Baseline Settings",
        "Image and HSV Processing",
        "Motion Magnification",
        "Load Cell and Calibration",
            "Recording and Synchronization",
            "Printer Motion",
            "Graph and Export Settings",
        "System Status and Logs",
        "Live Sensor (Experimental)",
    ]
    assert window.help_button.isVisible()
    assert not window.video_display.overlay_check.isChecked()
    assert not window.video_display.overlay_check.isVisible()
    assert window.motion_tab.mode_combo.findText("Color magnification") >= 0
    assert window.live_sensor_tab.engine is not None
    assert window.live_sensor_tab.engine.bundle.sensor_mode == "hybrid_force_event"
    assert window.live_sensor_tab.engine.bundle.force_x is not None
    assert not window.recording_tab.start_button.isEnabled()
    assert not window.recording_tab.stop_button.isHidden()
    assert len(window.recording_tab.readiness_checkboxes) == 10
    for width, height in ((1366, 768), (1920, 1080), (960, 640)):
        window.resize(width, height)
        qtbot.wait(20)
        assert not window.recording_tab.stop_button.isHidden()
        assert window.recording_tab.findChildren(QScrollArea)
    window.close()
    assert not window.camera_thread.isRunning()
    assert not window.serial_thread.isRunning()


def test_interactive_simulation_records_and_finalizes_one_press(
    qtbot, tmp_path: Path
) -> None:
    config = _fast_config(tmp_path)
    window = MainWindow(config, default_simulation=True)
    qtbot.addWidget(window)
    window.show()

    window.camera_tab.connect_button.click()
    qtbot.waitUntil(lambda: window.readiness.camera_connected, timeout=4000)
    window.camera_tab.start_preview_button.click()
    qtbot.waitUntil(lambda: window._latest_captured is not None, timeout=4000)

    window.roi_tab.capture_baseline_button.click()
    qtbot.waitUntil(lambda: window._pending_baseline is not None, timeout=5000)
    assert window._baseline_capture_frames == []
    window.roi_tab.accept_baseline_button.click()
    qtbot.waitUntil(lambda: window.readiness.baseline_valid, timeout=3000)

    window.loadcell_tab.connect_button.click()
    qtbot.waitUntil(lambda: window.readiness.serial_connected, timeout=4000)
    _complete_user_loadcell_workflow(window, qtbot)

    window.recording_tab.session_id_edit.setText("gui-simulation")
    window.recording_tab.trial_id_edit.setText("trial-001")
    window.recording_tab.sensing_skin_id_edit.setText("skin-001")
    window.recording_tab.interaction_class_combo.setCurrentText("Press")
    window._output_directory = tmp_path
    window.recording_tab.set_output_directory(str(tmp_path))
    preflight = preflight_recording_output(
        config.recording,
        frame_size=(160, 120),
        output_fps=30.0,
    )
    window._preflight_succeeded(preflight)
    window._refresh_readiness()
    assert window.readiness.ready_to_record
    assert window.recording_tab.start_button.isEnabled()

    window.recording_tab.start_button.click()
    try:
        qtbot.waitUntil(
            lambda: window.lifecycle is RecordingLifecycle.RECORDING,
            timeout=7000,
        )
    except Exception:
        pytest.fail(
            f"recording did not start: lifecycle={window.lifecycle.value}; "
            f"status={window.recording_tab.status_label.text()}; "
            f"next={window.recording_tab.next_action_label.text()}"
        )
    qtbot.waitUntil(lambda: window._recording_frame_count >= 6, timeout=5000)
    window.recording_tab.stop_button.click()
    qtbot.waitUntil(
        lambda: window.lifecycle in {RecordingLifecycle.COMPLETE, RecordingLifecycle.ERROR},
        timeout=15000,
    )
    assert window.lifecycle is RecordingLifecycle.COMPLETE
    session_dirs = [path for path in tmp_path.iterdir() if path.is_dir()]
    assert len(session_dirs) == 1
    session = session_dirs[0]
    assert (session / "master_synchronized.csv").is_file()
    assert (session / "session_status.json").is_file()
    assert (session / "spatial_graphs" / "trial_peak_mean_delta_v.png").is_file()
    qtbot.waitUntil(
        lambda: window.recording_tab.review_labels["session_status"].text() == "complete",
        timeout=5000,
    )
    window.close()


def test_unloaded_baseline_does_not_require_camera_mode_or_property_readback(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    window.camera_tab.connect_button.click()
    qtbot.waitUntil(lambda: window.readiness.camera_connected, timeout=4_000)
    window.camera_tab.start_preview_button.click()
    qtbot.waitUntil(lambda: window._latest_captured is not None, timeout=4_000)

    # Driver mode/property readback is deliberately non-authoritative for the
    # baseline workflow. Fresh frames and nine valid ROIs are sufficient.
    window._camera_mode_confirmed = False
    assert window.roi_tab.validation.valid
    assert "camera_settings_confirmed" not in window.readiness.REQUIRED_FIELDS
    window.roi_tab.capture_baseline_button.click()
    qtbot.waitUntil(lambda: window._pending_baseline is not None, timeout=5_000)
    assert "Baseline calculated" in window.roi_tab.baseline_status_label.text()
    window.close()


def _ready_recording_window(qtbot, tmp_path: Path) -> MainWindow:
    config = _fast_config(tmp_path)
    window = MainWindow(config, default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    window.camera_tab.connect_button.click()
    qtbot.waitUntil(lambda: window.readiness.camera_connected, timeout=4_000)
    window.camera_tab.start_preview_button.click()
    qtbot.waitUntil(lambda: window._latest_captured is not None, timeout=4_000)
    window.roi_tab.capture_baseline_button.click()
    qtbot.waitUntil(lambda: window._pending_baseline is not None, timeout=5_000)
    window.roi_tab.accept_baseline_button.click()
    window.loadcell_tab.connect_button.click()
    qtbot.waitUntil(lambda: window.readiness.serial_connected, timeout=4_000)
    saved = _calibration()
    window._calibration = saved
    window.readiness.loadcell_calibrated = True
    window.readiness.calibration_verified = True
    window.serial_calibration_command.emit(saved)
    window.recording_tab.session_id_edit.setText("atomic-preparation")
    window.recording_tab.trial_id_edit.setText("trial-atomic")
    window.recording_tab.sensing_skin_id_edit.setText("skin-atomic")
    window.recording_tab.interaction_class_combo.setCurrentText("Press")
    window._output_directory = tmp_path
    window.recording_tab.set_output_directory(str(tmp_path))
    window._preflight_succeeded(
        preflight_recording_output(
            config.recording, frame_size=(160, 120), output_fps=30.0
        )
    )
    window._refresh_readiness()
    assert window.readiness.ready_to_record
    return window


@pytest.mark.parametrize("mutation", ("roi", "serial_disconnect"))
def test_delayed_recording_preparation_is_locked_and_generation_cancelled(
    qtbot, tmp_path: Path, mutation: str
) -> None:
    window = _ready_recording_window(qtbot, tmp_path)
    original_run_job = window._run_job
    pending: dict[str, object] = {}

    def delay_drift(function, succeeded, failed, *args, **kwargs):
        if function is check_frame_baseline_drift:
            pending.update(succeeded=succeeded, failed=failed)
            return
        original_run_job(function, succeeded, failed, *args, **kwargs)

    window._run_job = delay_drift
    window.recording_tab.start_button.click()
    qtbot.waitUntil(lambda: "succeeded" in pending, timeout=4_000)
    assert window._preparing_recording
    assert not window.camera_tab.disconnect_button.isEnabled()
    assert not window.camera_tab.stop_preview_button.isEnabled()
    assert not window.roi_tab.roi_edit_group.isEnabled()
    assert not window.loadcell_tab.disconnect_button.isEnabled()
    assert not window.recording_tab.session_id_edit.isEnabled()

    if mutation == "roi":
        window.roi_tab.set_roi_layout(window.roi_tab.rois, emit_change=True)
    else:
        window.serial_disconnect_command.emit()
        qtbot.waitUntil(lambda: window._serial_info is None, timeout=3_000)
    qtbot.waitUntil(lambda: not window._preparing_recording, timeout=2_000)
    pending["succeeded"](
        SimpleNamespace(accepted=True, mean_absolute_drift=0.0, threshold=5.0)
    )
    qtbot.wait(50)
    assert window.lifecycle is RecordingLifecycle.IDLE
    assert window._recording_thread is None
    assert window.recording_tab.session_id_edit.isEnabled()
    window.close()


def test_stale_baseline_success_cannot_clear_new_generation_frames(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    marker = object()
    window._baseline_generation = 22
    window._baseline_capture_frames = [marker]

    window._baseline_calculated(21, object(), object())

    assert window._baseline_capture_frames == [marker]
    window.close()


def test_baseline_transaction_blocks_recording_in_ui_and_controller(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    window.recording_tab.session_id_edit.setText("baseline-lock")
    window.recording_tab.trial_id_edit.setText("trial-lock")
    window.recording_tab.sensing_skin_id_edit.setText("skin-lock")
    for field_name in window.readiness.REQUIRED_FIELDS:
        setattr(window.readiness, field_name, True)
    window._refresh_readiness()
    assert window.recording_tab.start_button.isEnabled()

    window._baseline_locked = True
    window._update_configuration_locks()
    assert not window.recording_tab.start_button.isEnabled()
    assert not window.recording_tab.trial_labels_group.isEnabled()
    assert not window.recording_tab.output_group.isEnabled()

    window._request_recording(window.recording_tab.trial_labels())
    assert not window._preparing_recording
    assert "baseline capture or calculation" in window.recording_tab.status_label.text()
    window._baseline_locked = False
    window._update_configuration_locks()
    window.close()


def test_camera_settings_are_absent_from_gui_and_readiness(qtbot, tmp_path: Path) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    assert "camera_settings_confirmed" not in window.readiness.REQUIRED_FIELDS
    assert "camera_settings_confirmed" not in window.recording_tab.readiness_checkboxes
    assert not hasattr(window.camera_tab, "apply_settings_button")
    assert not hasattr(window.camera_tab, "property_value_spins")
    assert window.camera_tab.requested_properties() == {}
    window.close()


def test_color_magnification_selects_live_magnified_hsv_force_light_source(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    window.camera_tab.connect_button.click()
    qtbot.waitUntil(lambda: window.readiness.camera_connected, timeout=4_000)
    window.camera_tab.start_preview_button.click()
    qtbot.waitUntil(lambda: window._latest_captured is not None, timeout=4_000)
    window.roi_tab.capture_baseline_button.click()
    qtbot.waitUntil(lambda: window._pending_baseline is not None, timeout=5_000)
    window.roi_tab.accept_baseline_button.click()
    qtbot.waitUntil(lambda: window.readiness.baseline_valid, timeout=3_000)

    # Model a physical UVC driver that reports no nominal FPS. Applying color
    # magnification must still use the target rate until measured capture FPS
    # arrives, and it must route live HSV measurements through magnified pixels.
    window._camera_info = replace(window._camera_info, actual_fps=0.0)
    window._measured_capture_fps = float("nan")
    config = MotionMagnificationConfig(
        enabled=True,
        mode="Color magnification",
        target_fps=20.0,
        upper_cutoff_hz=3.0,
        processing_width=160,
        processing_height=120,
        pyramid_levels=3,
    )
    window._apply_motion_config(config)

    assert "processing FPS is unavailable" not in window.motion_tab.status_label.text()
    assert (
        window.image_processing_tab.analysis_source_combo.currentText()
        == "Motion-magnified frame"
    )
    assert (
        window.video_display.processed_mode_combo.currentText()
        == "Motion-magnified color preview"
    )
    assert window.roi_tab.graph_panel.metric_selector.currentText() == "Mean HSV V (0-255)"
    assert window.roi_tab.graph_panel.view_selector.currentText() == "Temporal ROI Lines"
    assert "motion-magnified" in window.roi_tab.graph_panel.data_source_label.text()
    magnified_rows: list[object] = []

    def take_magnified_row() -> bool:
        row = window.live_optical_buffer.take_latest()
        if row is not None:
            magnified_rows.append(row)
        return bool(magnified_rows)

    qtbot.waitUntil(take_magnified_row, timeout=5_000)
    row = magnified_rows[-1]
    assert row["quantitative_analysis_source"] == "Motion-magnified frame"
    assert row["hsv_frame_source"] == "motion-magnified color frame"
    assert all(f"roi{roi_id}_mean_v" in row for roi_id in range(1, 10))
    window.close()


def test_camera_warmup_timeout_releases_baseline_lock(qtbot, tmp_path: Path) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    window._baseline_generation = 3
    window._baseline_locked = True
    window.readiness.camera_connected = True
    window.camera_tab.set_connection_state(True)
    window.camera_tab.set_preview_state(True)
    window._update_configuration_locks()

    window._await_authoritative_camera_warmup("baseline", 3, 0)

    assert not window._baseline_locked
    assert "timeout" in window.roi_tab.baseline_status_label.text().lower()
    assert window.recording_tab.trial_labels_group.isEnabled()
    window.close()


def test_preview_and_trial_performance_metrics_reset_without_cross_contamination(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.preview_buffer.publish("old-frame-1")
    window.preview_buffer.publish("old-frame-2")
    window.recording_tab.update_performance_metrics(
        {
            "preview_fps": "77.00",
            "feature_processing_fps": "88.00",
            "video_writing_fps": "99.00",
            "recording_pipeline_errors": 5,
        }
    )

    window._start_preview()
    assert window.preview_buffer.dropped_count == 0
    assert window.recording_tab.performance_labels["preview_fps"].text() == "0.00"
    assert window.recording_tab.performance_labels["dropped_preview_frames"].text() == "0"

    window.preview_buffer.publish("pre-trial-1")
    window.preview_buffer.publish("pre-trial-2")
    window._reset_recording_performance_metrics()
    assert window.recording_tab.performance_labels["feature_processing_fps"].text() == "0.00"
    assert window.recording_tab.performance_labels["video_writing_fps"].text() == "0.00"
    assert window.recording_tab.performance_labels["recording_pipeline_errors"].text() == "0"
    window.preview_buffer.publish("trial-1")
    window.preview_buffer.publish("trial-2")
    total_drops = window.preview_buffer.dropped_count
    window._preparing_recording = True
    window._camera_metrics(
        {"capture_fps": 30.0, "dropped_preview_frames": total_drops}
    )
    assert window.camera_tab.performance_labels["dropped_preview_frames"].text() == str(
        total_drops
    )
    assert window.recording_tab.performance_labels["dropped_preview_frames"].text() == "2"

    window._recording_error_count = 2
    window._recording_failed("deterministic terminal failure", None)
    assert window.recording_tab.performance_labels["recording_pipeline_errors"].text() == "3"
    window.close()


def test_terminal_recording_update_is_drained_before_review_summary(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    launched: dict[str, object] = {}

    def capture_job(function, succeeded, failed, *args, **kwargs):
        launched.update(function=function, args=args, kwargs=kwargs)

    window._run_job = capture_job
    window._start_live_analysis = lambda: None
    window.recording_display_buffer.publish(
        {
            "feature_row": {"capture_frame_id": 4},
            "processed_frame_count": 5,
            "pipeline_error_count": 3,
            "feature_processing_ns": 5_000_000,
            "file_writing_ns": 2_000_000,
        }
    )
    stale_live_row = {
        "analysis_generation": window._live_analysis_generation - 1,
        "capture_frame_id": 999,
    }
    # This arrives later in wall-clock order, modeling a nonblocking retiring
    # live worker.  It must not overwrite the recording terminal slot.
    window.live_optical_buffer.publish(stale_live_row)
    completion = SimpleNamespace(session_directory=tmp_path)

    window._recording_completed(completion)

    assert window._recording_error_count == 3
    assert launched["kwargs"]["recording_errors"] == 3
    assert window.recording_tab.performance_labels["recording_pipeline_errors"].text() == "3"
    assert window.recording_display_buffer.take_latest() is None
    assert window.live_optical_buffer.take_latest() == stale_live_row
    window.close()


def test_delayed_partial_review_cannot_overwrite_newer_complete_session(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    pending: list[tuple[object, object]] = []

    def delay_review(_function, succeeded, failed, *_args, **_kwargs):
        pending.append((succeeded, failed))

    window._run_job = delay_review
    window._start_live_analysis = lambda: None
    window._replace_graph_service = lambda *_args, **_kwargs: None
    old_directory = tmp_path / "old-partial"
    new_directory = tmp_path / "new-complete"
    old_directory.mkdir()
    new_directory.mkdir()

    window._recording_failed("old deterministic failure", old_directory)
    assert len(pending) == 1
    old_succeeded, old_failed = pending[0]
    window._recording_completed(SimpleNamespace(session_directory=new_directory))
    assert len(pending) == 2
    new_succeeded, _new_failed = pending[1]
    status_before_stale_failure = window.recording_tab.status_label.text()

    old_succeeded(
        {
            "final_output_directory": str(old_directory),
            "session_status": "partial",
        }
    )
    old_failed("late old review failure")

    assert window.recording_tab.review_labels["session_status"].text() == "—"
    assert not window.recording_tab.open_output_button.isEnabled()
    assert window.recording_tab.status_label.text() == status_before_stale_failure

    new_succeeded(
        {
            "final_output_directory": str(new_directory),
            "session_status": "complete",
        }
    )
    assert window.recording_tab.review_labels["session_status"].text() == "complete"
    assert (
        window.recording_tab.review_labels["final_output_directory"].text()
        == str(new_directory)
    )
    assert window.recording_tab.open_output_button.isEnabled()
    window.close()


@pytest.mark.parametrize("scale", (1.0, 1.25, 1.5))
def test_required_display_scale_fits_1366_by_768_and_keeps_actions_in_bounds(
    scale: float,
) -> None:
    project = Path(__file__).resolve().parents[1]
    code = r'''
import json
from PySide6.QtWidgets import QApplication
from gui.main_window import MainWindow
app = QApplication.instance() or QApplication([])
window = MainWindow(default_simulation=True)
window.show()
app.processEvents()
window.help_button.click()
app.processEvents()
help_button = window.help_dialog.button_box.button(
    window.help_dialog.button_box.StandardButton.Close
)
page_bounds = []
for index in range(window.tabs.count()):
    window.tabs.setCurrentIndex(index)
    app.processEvents()
    page = window.tabs.widget(index)
    page_bottom_central = page.mapTo(
        window.centralWidget(), page.rect().bottomRight()
    ).y()
    page_bottom_tabs = page.mapTo(window.tabs, page.rect().bottomRight()).y()
    page_bounds.append({
        "name": window.tabs.tabText(index),
        "bottom_central": page_bottom_central,
        "bottom_tabs": page_bottom_tabs,
        "page_height": page.height(),
    })
window.tabs.setCurrentWidget(window.recording_tab)
app.processEvents()
payload = {
    "width": window.width(),
    "height": window.height(),
    "minimum_width": window.minimumWidth(),
    "minimum_height": window.minimumHeight(),
    "dpr": window.devicePixelRatioF(),
    "help_right": window.help_button.geometry().right(),
    "central_width": window.centralWidget().width(),
    "tabbar_bottom": window.tabs.tabBar().geometry().bottom(),
    "central_height": window.centralWidget().height(),
    "tabs_height": window.tabs.height(),
    "page_bounds": page_bounds,
    "stop_width": window.recording_tab.stop_button.width(),
    "stop_height": window.recording_tab.stop_button.height(),
    "help_dialog_width": window.help_dialog.width(),
    "help_dialog_height": window.help_dialog.height(),
    "help_close_right": help_button.mapTo(window.help_dialog, help_button.rect().bottomRight()).x(),
    "help_close_bottom": help_button.mapTo(window.help_dialog, help_button.rect().bottomRight()).y(),
}
print(json.dumps(payload))
window.close()
'''
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["QT_SCALE_FACTOR"] = str(scale)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["minimum_width"] * payload["dpr"] <= 1366
    assert payload["minimum_height"] * payload["dpr"] <= 768
    assert payload["width"] * payload["dpr"] <= 1366
    assert payload["height"] * payload["dpr"] <= 768
    assert payload["help_right"] <= payload["central_width"]
    assert payload["tabbar_bottom"] <= payload["central_height"]
    assert all(
        item["page_height"] > 0
        and item["bottom_central"] <= payload["central_height"]
        and item["bottom_tabs"] <= payload["tabs_height"]
        for item in payload["page_bounds"]
    )
    assert payload["stop_width"] > 0 and payload["stop_height"] > 0
    assert payload["help_dialog_width"] * payload["dpr"] <= 1366
    assert payload["help_dialog_height"] * payload["dpr"] <= 768
    assert payload["help_close_right"] <= payload["help_dialog_width"]
    assert payload["help_close_bottom"] <= payload["help_dialog_height"]
