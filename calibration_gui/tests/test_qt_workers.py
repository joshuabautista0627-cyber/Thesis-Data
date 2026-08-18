from __future__ import annotations

from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtTest import QSignalSpy

from core.models import ErrorCode
from services.interfaces import CapturedFrame
from services.qt_workers import (
    CameraWorker,
    LatestValueBuffer,
    RecordingThread,
    SampleWindowCollector,
    SerialWorker,
)


def test_latest_value_buffer_keeps_only_newest_and_counts_preview_drops() -> None:
    buffer = LatestValueBuffer()
    buffer.publish("frame-1")
    buffer.publish("frame-2")
    buffer.publish("frame-3")
    assert buffer.take_latest() == "frame-3"
    assert buffer.take_latest() is None
    assert buffer.dropped_count == 2


def test_latest_value_buffer_reset_starts_a_new_drop_interval() -> None:
    buffer = LatestValueBuffer()
    buffer.publish("old-1")
    buffer.publish("old-2")
    assert buffer.dropped_count == 1

    buffer.reset()

    assert buffer.take_latest() is None
    assert buffer.dropped_count == 0
    buffer.publish("new")
    assert buffer.take_latest() == "new"
    assert buffer.dropped_count == 0


def test_sample_window_collector_keeps_every_raw_value_until_duration() -> None:
    collector = SampleWindowCollector()
    collector.begin("unloaded", 0.1)
    for index in range(6):
        collector.accept(
            SimpleNamespace(
                host_monotonic_ns=1_000_000_000 + index * 20_000_000,
                raw_adc=10_000 + index,
            )
        )
    assert collector.take_completed() == (
        "unloaded",
        (10_000, 10_001, 10_002, 10_003, 10_004, 10_005),
    )
    assert not collector.active


def test_sample_window_progress_uses_fresh_source_timestamps() -> None:
    collector = SampleWindowCollector()
    collector.begin("verify", 0.5)
    collector.accept(SimpleNamespace(host_monotonic_ns=1_000_000_000, raw_adc=1))
    collector.accept(SimpleNamespace(host_monotonic_ns=1_125_000_000, raw_adc=2))
    assert collector.progress == ("verify", 2, 0.125, 0.5)


def test_sample_window_cancel_discards_values_and_allows_clean_restart() -> None:
    collector = SampleWindowCollector()
    collector.begin("loaded", 0.5)
    collector.accept(SimpleNamespace(host_monotonic_ns=1_000, raw_adc=10))
    assert collector.cancel()
    assert not collector.active
    assert collector.take_completed() is None
    collector.begin("unloaded", 0.1)
    assert collector.progress == ("unloaded", 0, 0.0, 0.1)


class _FailingCameraService:
    simulation_mode = False
    is_connected = True

    def __init__(self) -> None:
        self.disconnect_called = False

    def read_frame(self):
        raise OSError("camera cable removed")

    def disconnect(self) -> None:
        self.disconnect_called = True
        self.is_connected = False


class _ExhaustedSimulationCamera:
    simulation_mode = True
    is_connected = True

    def __init__(self) -> None:
        self.disconnect_called = False

    def read_frame(self):
        return None

    def disconnect(self) -> None:
        self.disconnect_called = True


class _FailingSerialService:
    is_connected = True

    def __init__(self) -> None:
        self.disconnect_called = False

    def read_sample(self):
        raise OSError("serial cable removed")

    def disconnect(self) -> None:
        self.disconnect_called = True
        self.is_connected = False


def test_camera_read_exception_releases_owner_and_emits_disconnect() -> None:
    worker = CameraWorker(LatestValueBuffer())
    service = _FailingCameraService()
    worker._service = service
    errors = QSignalSpy(worker.error)
    disconnected = QSignalSpy(worker.disconnected)

    worker._capture_once()

    assert worker._service is None
    assert service.disconnect_called
    assert errors.count() == 1
    assert "camera cable removed" in errors.at(0)[0]
    assert disconnected.count() == 1


def test_normal_finite_simulation_exhaustion_is_not_a_device_disconnect() -> None:
    worker = CameraWorker(LatestValueBuffer())
    service = _ExhaustedSimulationCamera()
    worker._service = service
    exhausted = QSignalSpy(worker.source_exhausted)
    disconnected = QSignalSpy(worker.disconnected)

    worker._capture_once()

    assert worker._service is service
    assert not service.disconnect_called
    assert exhausted.count() == 1
    assert disconnected.count() == 0


def test_camera_setting_failure_is_generation_tagged() -> None:
    worker = CameraWorker(LatestValueBuffer())
    worker._service = object()
    failed = QSignalSpy(worker.property_failed)
    generic_errors = QSignalSpy(worker.error)

    worker.apply_settings(42, {"auto_focus": False})

    assert failed.count() == 1
    assert failed.at(0)[0] == 42
    assert "apply_settings" in failed.at(0)[1]
    assert generic_errors.count() == 0


def test_serial_read_exception_releases_owner_and_emits_disconnect() -> None:
    worker = SerialWorker(LatestValueBuffer())
    service = _FailingSerialService()
    worker._service = service
    errors = QSignalSpy(worker.error)
    disconnected = QSignalSpy(worker.disconnected)

    worker._read_once()

    assert worker._service is None
    assert service.disconnect_called
    assert errors.count() == 1
    assert "serial cable removed" in errors.at(0)[0]
    assert disconnected.count() == 1


def test_serial_health_blocks_on_timeout_and_recovers_only_on_fresh_data() -> None:
    sample = SimpleNamespace(
        host_monotonic_ns=10,
        elapsed_time_s=0.0,
        raw_adc=123,
    )

    class HealthService:
        is_connected = True

        def __init__(self) -> None:
            self.calls = 0
            self.timeout_errors = 0

        def read_sample(self):
            self.calls += 1
            if self.calls == 1:
                self.timeout_errors = 1
                return None
            return sample

        def diagnostics(self):
            return {
                "readiness_error_count": 0,
                "timeout_error_count": self.timeout_errors,
            }

    worker = SerialWorker(LatestValueBuffer())
    worker._service = HealthService()
    health = QSignalSpy(worker.stream_health_changed)
    worker._read_once()
    worker._read_once()
    assert health.count() == 2
    assert health.at(0)[0] is False
    assert health.at(0)[1] == ErrorCode.HX711_TIMEOUT.value
    assert health.at(1)[0] is True
    assert health.at(1)[1] == ErrorCode.NONE.value


def test_serial_validation_deadline_reports_but_keeps_retrying(monkeypatch) -> None:
    class WaitingService:
        is_connected = True

        def read_sample(self):
            return None

        def diagnostics(self):
            return {"readiness_error_count": 0, "timeout_error_count": 0}

    worker = SerialWorker(LatestValueBuffer())
    worker._service = WaitingService()
    worker._validation_started_ns = 1_000_000_000
    worker._validation_timeout_s = 3.0
    monkeypatch.setattr(
        "services.qt_workers.time.perf_counter_ns", lambda: 4_100_000_000
    )
    health = QSignalSpy(worker.stream_health_changed)

    worker._read_once()
    worker._read_once()

    assert health.count() == 1
    assert health.at(0)[0] is False
    assert health.at(0)[1] == ErrorCode.HX711_TIMEOUT.value
    assert "still retrying" in health.at(0)[2]
    assert worker._service is not None


def test_late_submit_after_normal_finalize_is_benign_not_saturation() -> None:
    worker = RecordingThread(
        pipeline=SimpleNamespace(),
        recorder=SimpleNamespace(),
        recorder_start_kwargs={},
        recording_start_monotonic_ns=1,
        queue_size=1,
    )
    worker.request_finalize()
    frame = CapturedFrame(
        np.zeros((2, 2, 3), dtype=np.uint8),
        source_frame_id=1,
        host_monotonic_ns=2,
        wall_clock_iso="2026-08-03T00:00:00+00:00",
    )

    assert worker.submit_frame(frame)
    assert worker.queue_depth == 0
    assert not worker._abort_requested.is_set()


def test_recording_abort_preserves_first_causal_fault() -> None:
    worker = RecordingThread(
        pipeline=SimpleNamespace(),
        recorder=SimpleNamespace(),
        recorder_start_kwargs={},
        recording_start_monotonic_ns=1,
        queue_size=1,
    )
    worker.request_abort("camera disconnected", ErrorCode.CAMERA_DISCONNECTED)
    worker.request_abort("application shutdown", ErrorCode.SHUTDOWN_REQUESTED)

    assert worker._abort_message == "camera disconnected"
    assert worker._abort_error_code is ErrorCode.CAMERA_DISCONNECTED


class _MemoryRecorder:
    def __init__(self) -> None:
        self.session_directory = "memory-session"
        self.frames = []
        self.loads = []
        self.ingress = None
        self.abort_call = None

    def start(self, **_kwargs):
        return self.session_directory

    def record_frame(self, frame) -> None:
        self.frames.append(frame)

    def record_loadcell_sample(self, sample) -> None:
        self.loads.append(sample)

    def set_ingress_counts(self, **values) -> None:
        self.ingress = values

    def finalize(self):
        return SimpleNamespace(session_directory=self.session_directory)

    def abort(self, code, message) -> None:
        self.abort_call = (code, message)


def _captured(frame_id: int) -> CapturedFrame:
    return CapturedFrame(
        np.full((2, 2, 3), frame_id, dtype=np.uint8),
        source_frame_id=frame_id,
        host_monotonic_ns=1_000_000_000 + frame_id,
        wall_clock_iso="2026-08-03T00:00:00+00:00",
    )


def test_finalize_and_submit_interleaving_cannot_orphan_an_accepted_frame(
    qtbot,
) -> None:
    entered_put = Event()
    release_put = Event()

    class BlockingQueue(Queue):
        def put_nowait(self, item) -> None:
            entered_put.set()
            assert release_put.wait(2.0)
            super().put_nowait(item)

    recorder = _MemoryRecorder()
    worker = RecordingThread(
        pipeline=SimpleNamespace(
            process=lambda _frame, **values: SimpleNamespace(
                feature_row={
                    "capture_frame_id": values["capture_frame_id"],
                    "error_code": (
                        ErrorCode.FEATURE_EXTRACTION_FAILED.value
                        if values["capture_frame_id"] in {3, 7}
                        else ErrorCode.NONE.value
                    ),
                }
            )
        ),
        recorder=recorder,
        recorder_start_kwargs={},
        recording_start_monotonic_ns=1,
        queue_size=2,
    )
    worker._queue = BlockingQueue(maxsize=2)
    submit_result = []
    submitter = Thread(target=lambda: submit_result.append(worker.submit_frame(_captured(0))))
    finalizer = Thread(target=worker.request_finalize)
    submitter.start()
    assert entered_put.wait(2.0)
    finalizer.start()
    assert finalizer.is_alive()  # waits for the same state lock as submit
    release_put.set()
    submitter.join(2.0)
    finalizer.join(2.0)
    assert submit_result == [True]

    with qtbot.waitSignal(worker.completed, timeout=5_000):
        worker.start()
    assert worker.wait(2_000)
    assert len(recorder.frames) == 1
    assert recorder.ingress == {
        "accepted_frame_count": 1,
        "accepted_loadcell_sample_count": 0,
    }


def test_fatal_processing_error_closes_ingress_and_reports_unpersisted_count(
    qtbot,
) -> None:
    recorder = _MemoryRecorder()

    def fail(*_args, **_kwargs):
        raise RuntimeError("deterministic processing fault")

    worker = RecordingThread(
        pipeline=SimpleNamespace(process=fail),
        recorder=recorder,
        recorder_start_kwargs={},
        recording_start_monotonic_ns=1,
        queue_size=2,
    )
    assert worker.submit_frame(_captured(0))
    with qtbot.waitSignal(worker.failed, timeout=5_000):
        worker.start()
    assert worker.wait(2_000)
    assert not worker.submit_frame(_captured(1))
    assert recorder.ingress == {
        "accepted_frame_count": 1,
        "accepted_loadcell_sample_count": 0,
    }
    assert recorder.frames == []


def test_recording_display_updates_coalesce_to_newest_when_gui_is_stalled(
    qtbot,
) -> None:
    recorder = _MemoryRecorder()
    display = LatestValueBuffer()

    def process(_frame, **values):
        frame_id = values["capture_frame_id"]
        return SimpleNamespace(
            feature_row={
                "capture_frame_id": frame_id,
                "error_code": (
                    ErrorCode.FEATURE_EXTRACTION_FAILED.value
                    if frame_id in {3, 7}
                    else ErrorCode.NONE.value
                ),
            }
        )

    worker = RecordingThread(
        pipeline=SimpleNamespace(process=process),
        recorder=recorder,
        recorder_start_kwargs={},
        recording_start_monotonic_ns=1,
        queue_size=20,
        display_buffer=display,
    )
    for frame_id in range(10):
        assert worker.submit_frame(_captured(frame_id))
    worker.request_finalize()
    with qtbot.waitSignal(worker.completed, timeout=5_000):
        worker.start()
    latest = display.take_latest()
    assert latest["processed_frame_count"] == 10
    assert latest["pipeline_error_count"] == 2
    assert latest["feature_row"]["capture_frame_id"] == 9
    assert display.dropped_count == 9
    assert not hasattr(worker, "frame_processed")
