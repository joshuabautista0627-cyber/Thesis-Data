"""Gate 5 reliability tests for bounded queues, faults, and clean shutdown."""

from __future__ import annotations

import csv
from concurrent.futures import Future
import json
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np
import pytest

from core.models import ErrorCode, RecordingLifecycle
from gui.main_window import MainWindow
from services.interfaces import CapturedFrame
from services.qt_workers import RecordingThread
from services.session_recorder import SessionRecorder, preflight_recording_output
from tests.test_gui_smoke import _calibration, _fast_config
from tests.test_recording_pipeline import (
    BASE_NS,
    calibration,
    original_frame,
    processed_frame,
    recording_config,
    roi_layout_and_baseline,
)


def _partial_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _start_gui_simulation_recording(qtbot, tmp_path: Path) -> MainWindow:
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
    qtbot.waitUntil(lambda: window.readiness.baseline_valid, timeout=3_000)

    window.loadcell_tab.connect_button.click()
    qtbot.waitUntil(lambda: window.readiness.serial_connected, timeout=4_000)
    saved_calibration = _calibration()
    window._calibration = saved_calibration
    window.readiness.loadcell_calibrated = True
    window.readiness.calibration_verified = True
    window.serial_calibration_command.emit(saved_calibration)

    window.recording_tab.session_id_edit.setText("gate5-simulation")
    window.recording_tab.trial_id_edit.setText("trial-reliability")
    window.recording_tab.sensing_skin_id_edit.setText("skin-reliability")
    window.recording_tab.interaction_class_combo.setCurrentText("Press")
    window._output_directory = tmp_path
    window.recording_tab.set_output_directory(str(tmp_path))
    result = preflight_recording_output(
        config.recording,
        frame_size=(160, 120),
        output_fps=30.0,
    )
    window._preflight_succeeded(result)
    window._refresh_readiness()
    assert window.readiness.ready_to_record

    window.recording_tab.start_button.click()
    qtbot.waitUntil(
        lambda: window.lifecycle is RecordingLifecycle.RECORDING,
        timeout=7_000,
    )
    qtbot.waitUntil(lambda: window._recording_frame_count >= 3, timeout=5_000)
    assert window._active_session_directory is not None
    return window


def test_recording_queue_saturation_drains_accepted_frame_to_recoverable_partial(
    qtbot, tmp_path: Path
) -> None:
    layout, baseline = roi_layout_and_baseline()
    recorder = SessionRecorder(recording_config(tmp_path))

    def process(_captured, *, capture_frame_id, recording_start_monotonic_ns):
        assert recording_start_monotonic_ns == BASE_NS
        return processed_frame(capture_frame_id)

    worker = RecordingThread(
        pipeline=SimpleNamespace(process=process),
        recorder=recorder,
        recorder_start_kwargs={
            "session_directory_name": "queue-saturation",
            "frame_size": (layout.frame_width, layout.frame_height),
            "output_fps": 10.0,
            "calibration": calibration(),
            "session_config": {"session_id": "queue-saturation"},
            "camera_settings": {"simulation_mode": True},
            "roi_layout": layout,
            "baseline": baseline,
        },
        recording_start_monotonic_ns=BASE_NS,
        queue_size=1,
    )
    first = CapturedFrame(
        original_frame(0),
        source_frame_id=0,
        host_monotonic_ns=BASE_NS,
        wall_clock_iso="2026-08-03T00:00:00+00:00",
    )
    second = CapturedFrame(
        original_frame(1),
        source_frame_id=1,
        host_monotonic_ns=BASE_NS + 100_000_000,
        wall_clock_iso="2026-08-03T00:00:00.100000+00:00",
    )
    assert worker.submit_frame(first)
    assert not worker.submit_frame(second)

    with qtbot.waitSignal(worker.failed, timeout=10_000):
        worker.start()
    assert worker.wait(5_000)

    session = recorder.session_directory
    assert session is not None
    status = json.loads((session / "session_status.json").read_text("utf-8"))
    assert status["partial"] is True
    assert status["errors"][-1]["error_code"] == (
        ErrorCode.RECORDING_QUEUE_SATURATED.value
    )
    assert status["accepted_frame_count"] == 1
    assert status["feature_row_count"] == 1
    assert status["video_frame_count"] == 1
    assert len(_partial_rows(session / "frame_features.csv.partial")) == 1
    assert (session / "loadcell_raw.csv.partial").is_file()


def test_preview_burst_displays_newest_without_an_increasing_backlog(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    pixels = np.zeros((120, 160, 3), dtype=np.uint8)
    last_timestamp = time.perf_counter_ns()
    for frame_id in range(500):
        window.preview_buffer.publish(
            CapturedFrame(
                pixels,
                source_frame_id=frame_id,
                host_monotonic_ns=last_timestamp - (499 - frame_id) * 1_000_000,
                wall_clock_iso="2026-08-03T00:00:00+00:00",
            )
        )

    window._refresh_preview()

    assert window._latest_captured.source_frame_id == 499
    assert window.preview_buffer.take_latest() is None
    assert window.preview_buffer.dropped_count == 499
    latency_text = window.camera_tab.performance_labels[
        "capture_to_display_ms"
    ].text()
    latency_ms = float(latency_text.removesuffix(" ms"))
    assert latency_ms < 150.0
    window.close()
    assert not window.camera_thread.isRunning()
    assert not window.serial_thread.isRunning()


@pytest.mark.parametrize(
    ("component", "error_code"),
    (
        ("camera", ErrorCode.CAMERA_DISCONNECTED),
        ("serial", ErrorCode.SERIAL_DISCONNECTED),
    ),
)
def test_gui_device_disconnect_saves_partial_and_clears_readiness(
    qtbot,
    tmp_path: Path,
    component: str,
    error_code: ErrorCode,
) -> None:
    window = _start_gui_simulation_recording(qtbot, tmp_path)
    session = window._active_session_directory
    assert session is not None

    if component == "camera":
        window.camera_disconnect_command.emit()
    else:
        window.serial_disconnect_command.emit()

    qtbot.waitUntil(
        lambda: window.lifecycle is RecordingLifecycle.ERROR,
        timeout=15_000,
    )
    if component == "camera":
        assert not window.readiness.camera_connected
        assert not window.readiness.baseline_valid
    else:
        assert not window.readiness.serial_connected

    status = json.loads((session / "session_status.json").read_text("utf-8"))
    assert status["partial"] is True
    assert status["errors"][-1]["error_code"] == error_code.value
    assert status["accepted_frame_count"] == status["feature_row_count"]
    assert status["accepted_frame_count"] == status["video_frame_count"]
    rows = _partial_rows(session / "frame_features.csv.partial")
    assert rows
    # Accepted pre-fault frames retain the baseline snapshot locked at Start.
    assert all(row["baseline_valid"].lower() == "true" for row in rows)
    qtbot.waitUntil(
        lambda: window.recording_tab.review_labels["session_status"].text()
        == "partial",
        timeout=5_000,
    )
    assert all(
        label.text() != "—" for label in window.recording_tab.review_labels.values()
    )
    assert window.recording_tab.open_output_button.isEnabled()
    assert (
        window.recording_tab.review_labels["final_output_directory"].text()
        == str(session.resolve())
    )
    window.close()
    assert not window.camera_thread.isRunning()
    assert not window.serial_thread.isRunning()


def test_main_window_close_during_recording_flushes_shutdown_partial(
    qtbot, tmp_path: Path
) -> None:
    window = _start_gui_simulation_recording(qtbot, tmp_path)
    session = window._active_session_directory
    assert session is not None and window._recording_thread is not None

    assert window.close()

    assert window._recording_thread is None
    assert not window.camera_thread.isRunning()
    assert not window.serial_thread.isRunning()
    assert window.camera_worker._service is None
    assert window.serial_worker._service is None
    status = json.loads((session / "session_status.json").read_text("utf-8"))
    assert status["partial"] is True
    assert status["errors"][-1]["error_code"] == ErrorCode.SHUTDOWN_REQUESTED.value
    assert status["accepted_frame_count"] == status["feature_row_count"]
    assert status["accepted_frame_count"] == status["video_frame_count"]
    assert (session / "frame_features.csv.partial").is_file()
    assert (session / "loadcell_raw.csv.partial").is_file()


class _StuckRecordingWorker:
    def __init__(self) -> None:
        self.abort_request = None

    def isRunning(self) -> bool:  # noqa: N802 - Qt-shaped test double
        return True

    def request_abort(self, message: str, error_code: ErrorCode) -> None:
        self.abort_request = (message, error_code)

    def wait(self, _milliseconds: int) -> bool:
        return False


def test_close_is_ignored_while_critical_recording_worker_is_alive(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    stuck = _StuckRecordingWorker()
    window._recording_thread = stuck
    window._shutdown_worker_wait_ms = 1
    window._set_lifecycle(RecordingLifecycle.RECORDING)

    assert not window.close()

    assert window.isVisible()
    assert window.camera_thread.isRunning()
    assert window.serial_thread.isRunning()
    assert stuck.abort_request == (
        "Application shutdown requested",
        ErrorCode.SHUTDOWN_REQUESTED,
    )
    assert window._closing is False

    window._recording_thread = None
    window._set_lifecycle(RecordingLifecycle.IDLE)
    assert window.close()
    assert not window.camera_thread.isRunning()
    assert not window.serial_thread.isRunning()


def test_close_refuses_pending_graph_export_without_unbounded_shutdown_wait(
    qtbot, tmp_path: Path
) -> None:
    window = MainWindow(_fast_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window.show()
    future = Future()
    window._track_graph_future(future, category="heatmap")

    assert not window.close()
    assert window.isVisible()
    assert window.camera_thread.isRunning()
    assert "Graph exports are still finishing" in window.recording_tab.status_label.text()

    future.set_exception(RuntimeError("intentional graph test completion"))
    qtbot.waitUntil(lambda: not window._graph_futures, timeout=2_000)
    assert window.close()
    assert not window.camera_thread.isRunning()


def test_sustained_gui_simulation_finalizes_one_to_one_without_queue_growth(
    qtbot, tmp_path: Path
) -> None:
    window = _start_gui_simulation_recording(qtbot, tmp_path)
    worker = window._recording_thread
    assert worker is not None
    qtbot.waitUntil(lambda: window._recording_frame_count >= 60, timeout=8_000)
    assert worker.queue_depth <= window.config.recording.recording_queue_size

    window.recording_tab.stop_button.click()
    qtbot.waitUntil(
        lambda: window.lifecycle in {
            RecordingLifecycle.COMPLETE,
            RecordingLifecycle.ERROR,
        },
        timeout=20_000,
    )
    assert window.lifecycle is RecordingLifecycle.COMPLETE
    session = window._active_session_directory
    assert session is not None
    status = json.loads((session / "session_status.json").read_text("utf-8"))
    assert status["accepted_frame_count"] >= 60
    assert status["accepted_frame_count"] == status["feature_row_count"]
    assert status["accepted_frame_count"] == status["video_frame_count"]
    assert status["complete"] is True
    window.close()


def test_active_trial_hx711_timeout_aborts_with_specific_health_error(
    qtbot, tmp_path: Path
) -> None:
    window = _start_gui_simulation_recording(qtbot, tmp_path)
    session = window._active_session_directory
    assert session is not None

    window._serial_health_changed(
        False,
        ErrorCode.HX711_TIMEOUT.value,
        "deterministic HX711 conversion timeout",
    )
    qtbot.waitUntil(
        lambda: window.lifecycle is RecordingLifecycle.ERROR, timeout=15_000
    )
    assert not window.readiness.serial_connected
    status = json.loads((session / "session_status.json").read_text("utf-8"))
    assert status["errors"][-1]["error_code"] == ErrorCode.HX711_TIMEOUT.value
    window.close()
