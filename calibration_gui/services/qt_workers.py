"""Qt worker boundaries that keep acquisition and file work off the GUI thread."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
import math
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock
import time
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from core.models import (
    BaselineRecord,
    CameraConfig,
    ErrorCode,
    LoadCellCalibration,
    LoadCellConfig,
    ProcessingConfig,
    ROI,
)
from processing.feature_extraction import process_optical_frame
from processing.camera_orientation import orient_bgr_frame, validate_camera_orientation
from processing.motion_magnification import (
    EulerianMotionMagnifier,
    MotionMagnificationConfig,
)
from processing.pipeline import ProcessingPipeline
from services.camera_service import OpenCVCameraService
from services.interfaces import CapturedFrame
from services.serial_service import (
    SerialPlaybackSource,
    SerialService,
    enumerate_serial_ports,
)
from services.session_recorder import SessionRecorder
from services.simulation_service import (
    SyntheticCameraSource,
    SyntheticLoadCellSource,
    VideoFileCameraSource,
)


class LatestValueBuffer:
    """Thread-safe one-item buffer implementing the newest-value display policy."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._value: object | None = None
        self._dropped = 0

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def publish(self, value: object) -> None:
        with self._lock:
            if self._value is not None:
                self._dropped += 1
            self._value = value

    def take_latest(self) -> object | None:
        with self._lock:
            value = self._value
            self._value = None
            return value

    def clear(self) -> None:
        with self._lock:
            self._value = None

    def reset(self) -> None:
        """Atomically begin a new display interval with no retained value or drops."""

        with self._lock:
            self._value = None
            self._dropped = 0


class SampleWindowCollector:
    """Collect every raw sample for a monotonic calibration/tare window."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stage = ""
        self._duration_ns = 0
        self._start_ns: int | None = None
        self._last_timestamp_ns: int | None = None
        self._values: list[float] = []
        self._complete = False

    def begin(self, stage: str, duration_s: float) -> None:
        if not stage.strip() or not math.isfinite(duration_s) or duration_s <= 0:
            raise ValueError("sample-window stage and positive duration are required")
        with self._lock:
            if self._stage:
                raise RuntimeError("a sample window is already active")
            self._stage = stage.strip()
            self._duration_ns = round(duration_s * 1_000_000_000)
            self._start_ns = None
            self._last_timestamp_ns = None
            self._values = []
            self._complete = False

    def cancel(self) -> bool:
        """Atomically discard an in-progress window and return whether one existed."""

        with self._lock:
            was_active = bool(self._stage)
            self._stage = ""
            self._duration_ns = 0
            self._start_ns = None
            self._last_timestamp_ns = None
            self._values = []
            self._complete = False
            return was_active

    def accept(self, sample: object) -> None:
        timestamp = int(getattr(sample, "host_monotonic_ns"))
        raw_adc = float(getattr(sample, "raw_adc"))
        if not math.isfinite(raw_adc):
            return
        with self._lock:
            if not self._stage or self._complete:
                return
            if self._start_ns is None:
                self._start_ns = timestamp
            self._last_timestamp_ns = timestamp
            self._values.append(raw_adc)
            if timestamp - self._start_ns >= self._duration_ns:
                self._complete = True

    def take_completed(self) -> tuple[str, tuple[float, ...]] | None:
        with self._lock:
            if not self._complete:
                return None
            result = (self._stage, tuple(self._values))
            self._stage = ""
            self._values = []
            self._start_ns = None
            self._last_timestamp_ns = None
            self._complete = False
            return result

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._stage)

    @property
    def progress(self) -> tuple[str, int, float, float] | None:
        """Return stage, count, elapsed seconds, and duration for GUI guidance."""

        with self._lock:
            if not self._stage:
                return None
            elapsed_ns = (
                0
                if self._start_ns is None or not self._values
                else max(0, int(self._last_timestamp_ns or self._start_ns) - self._start_ns)
            )
            return (
                self._stage,
                len(self._values),
                elapsed_ns / 1_000_000_000.0,
                self._duration_ns / 1_000_000_000.0,
            )

    @property
    def statistics(self) -> tuple[int, float, float] | None:
        """Return count, mean, and population standard deviation for guidance."""

        with self._lock:
            if not self._stage or not self._values:
                return None
            values = np.asarray(self._values, dtype=np.float64)
            return (
                int(values.size),
                float(np.mean(values, dtype=np.float64)),
                float(np.std(values, dtype=np.float64)),
            )


class CameraWorker(QObject):
    """Create and own exactly one active camera service inside one Qt thread."""

    connected = Signal(object)
    disconnected = Signal()
    property_results = Signal(int, object)
    property_failed = Signal(int, str)
    metrics = Signal(object)
    error = Signal(str)
    source_exhausted = Signal()
    devices_found = Signal(object)
    mode_confirmation = Signal(bool, object)
    capabilities_found = Signal(object)
    native_properties_result = Signal(bool, str)

    def __init__(self, preview_buffer: LatestValueBuffer) -> None:
        super().__init__()
        self.preview_buffer = preview_buffer
        self._service: object | None = None
        self._timer: QTimer | None = None
        self._recording_sink: Callable[[CapturedFrame], bool] | None = None
        self._analysis_sink: Callable[[CapturedFrame], bool] | None = None
        self._sink_lock = Lock()
        self._capture_count = 0
        self._capture_started_ns = 0
        self._last_metrics_ns = 0
        self._rotation_degrees = 0
        self._mirror_horizontal = False

    def set_recording_sink(
        self, sink: Callable[[CapturedFrame], bool] | None
    ) -> None:
        with self._sink_lock:
            self._recording_sink = sink

    def set_analysis_sink(
        self, sink: Callable[[CapturedFrame], bool] | None
    ) -> None:
        with self._sink_lock:
            self._analysis_sink = sink

    @Slot()
    def refresh_devices(self) -> None:
        """Probe physical indices in the camera-owner thread and release each probe."""

        if self._service is not None:
            self.error.emit("Disconnect the active camera before refreshing devices.")
            return
        probe = OpenCVCameraService()
        try:
            devices = probe.refresh_devices()
            self.devices_found.emit(devices)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            probe.disconnect()

    @Slot(str, object, str)
    def connect_camera(
        self, mode: str, config: CameraConfig, source_path: str = ""
    ) -> None:
        self.disconnect_camera()
        try:
            self.set_orientation(
                int(config.rotation_degrees),
                bool(config.mirror_horizontal),
            )
            normalized = mode.strip().lower()
            if normalized == "video file (simulation)":
                service = VideoFileCameraSource()
                info = service.connect(config, source_path=source_path)
            elif normalized == "synthetic (simulation)":
                # Keep the interactive source long enough for warm-up, baseline,
                # calibration, and a deliberate single recording.  The scripted
                # smoke test continues to use its shorter deterministic source.
                service = SyntheticCameraSource(total_frames=3_600)
                info = service.connect(config)
            else:
                service = OpenCVCameraService()
                info = service.connect(config, apply_mode=False)
            self._service = service
            self._capture_count = 0
            self._capture_started_ns = time.perf_counter_ns()
            self._last_metrics_ns = self._capture_started_ns
            self.connected.emit(info)
            if bool(getattr(service, "simulation_mode", False)):
                mode_confirmed = (
                    int(getattr(info, "actual_width", 0)) > 0
                    and int(getattr(info, "actual_height", 0)) > 0
                    and float(getattr(info, "actual_fps", 0.0)) > 0.0
                )
                mode_rows: object = ()
            else:
                mode_confirmed = bool(getattr(service, "mode_confirmed", False))
                mode_rows = tuple(getattr(service, "mode_readbacks", ()))
            self.mode_confirmation.emit(mode_confirmed, mode_rows)
        except Exception as exc:
            self._service = None
            self.error.emit(str(exc))

    @Slot(int, bool)
    def set_orientation(self, rotation_degrees: int, mirror_horizontal: bool) -> None:
        rotation, mirror = validate_camera_orientation(
            rotation_degrees,
            mirror_horizontal,
        )
        self._rotation_degrees = rotation
        self._mirror_horizontal = mirror

    @Slot()
    def start_preview(self) -> None:
        if self._service is None or not bool(getattr(self._service, "is_connected", False)):
            self.error.emit("Connect a camera before starting preview.")
            return
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._capture_once)
        info = getattr(self._service, "connection_info", None)
        simulation = bool(getattr(self._service, "simulation_mode", False))
        fps = float(getattr(info, "actual_fps", 30.0)) if info is not None else 30.0
        interval_ms = max(1, round(1000.0 / fps)) if simulation else 0
        self._capture_count = 0
        self._capture_started_ns = time.perf_counter_ns()
        self._last_metrics_ns = self._capture_started_ns
        self._timer.start(interval_ms)

    @Slot()
    def stop_preview(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    @Slot(int, object)
    def apply_settings(
        self, generation: int, requested: Mapping[str, float | bool]
    ) -> None:
        if self._service is None:
            self.property_failed.emit(int(generation), "Camera is not connected.")
            return
        try:
            results = self._service.apply_settings(requested)
            self.property_results.emit(int(generation), results)
        except Exception as exc:
            self.property_failed.emit(int(generation), str(exc))

    @Slot()
    def refresh_capabilities(self) -> None:
        if self._service is None:
            self.error.emit("Connect a camera before refreshing available controls.")
            return
        try:
            detector = getattr(self._service, "detect_capabilities", None)
            self.capabilities_found.emit(detector() if callable(detector) else ())
        except Exception as exc:
            self.error.emit(str(exc))

    @Slot()
    def open_native_properties(self) -> None:
        if self._service is None:
            self.native_properties_result.emit(False, "Connect a camera first.")
            return
        try:
            opener = getattr(self._service, "open_native_properties", None)
            if not callable(opener):
                raise RuntimeError("Native camera properties are unavailable for this source.")
            opener()
            self.native_properties_result.emit(
                True, "Native camera properties closed; refresh controls to read accepted values."
            )
        except Exception as exc:
            self.native_properties_result.emit(False, str(exc))

    @Slot()
    def disconnect_camera(self) -> None:
        self.stop_preview()
        service = self._service
        self._service = None
        self.preview_buffer.clear()
        if service is not None:
            try:
                service.disconnect()
            except Exception as exc:
                self.error.emit(str(exc))
        self.disconnected.emit()

    @Slot()
    def _capture_once(self) -> None:
        service = self._service
        if service is None:
            return
        try:
            frame = service.read_frame()
            if frame is None:
                self.stop_preview()
                # A finite simulation source ending is not a device fault.  A
                # physical service, however, releases its capture on a failed
                # read and consequently reports ``is_connected == False``.
                if bool(getattr(service, "simulation_mode", False)):
                    self.source_exhausted.emit()
                elif not bool(getattr(service, "is_connected", False)):
                    self._release_failed_service(
                        "Camera read failed and the physical device disconnected."
                    )
                else:
                    self.source_exhausted.emit()
                return
            oriented_bgr = orient_bgr_frame(
                frame.original_bgr,
                rotation_degrees=self._rotation_degrees,
                mirror_horizontal=self._mirror_horizontal,
            )
            if oriented_bgr is not frame.original_bgr:
                frame = replace(frame, original_bgr=oriented_bgr)
            self.preview_buffer.publish(frame)
            with self._sink_lock:
                sink = self._recording_sink
                analysis_sink = self._analysis_sink
            if analysis_sink is not None:
                analysis_sink(frame)
            if sink is not None and not sink(frame):
                self.stop_preview()
                self.error.emit(
                    "Recording queue saturated; acquisition stopped to preserve frame identity."
                )
                return
            self._capture_count += 1
            now = time.perf_counter_ns()
            if now - self._last_metrics_ns >= 500_000_000:
                elapsed = max((now - self._capture_started_ns) / 1e9, 1e-9)
                self.metrics.emit(
                    {
                        "capture_fps": self._capture_count / elapsed,
                        "captured_frames": self._capture_count,
                        "dropped_preview_frames": self.preview_buffer.dropped_count,
                    }
                )
                self._last_metrics_ns = now
        except Exception as exc:
            self.stop_preview()
            self._release_failed_service(str(exc))

    def _release_failed_service(self, message: str) -> None:
        """Release a failed owner and make connection state unambiguous."""

        service = self._service
        self._service = None
        self.preview_buffer.clear()
        release_error = ""
        if service is not None:
            try:
                service.disconnect()
            except Exception as exc:
                release_error = f"; disconnect cleanup failed: {exc}"
        self.error.emit(f"{message}{release_error}")
        self.disconnected.emit()


class SerialWorker(QObject):
    """Create and own exactly one active serial or playback service."""

    connected = Signal(object)
    disconnected = Signal()
    sample_available = Signal()
    diagnostics_available = Signal(object)
    stream_health_changed = Signal(bool, str, str)
    command_response = Signal(str)
    error = Signal(str)
    ports_found = Signal(object)

    def __init__(self, sample_buffer: LatestValueBuffer) -> None:
        super().__init__()
        self.sample_buffer = sample_buffer
        self._service: object | None = None
        self._timer: QTimer | None = None
        self._recording_sink: Callable[[object], bool] | None = None
        self._sample_observer: Callable[[object], None] | None = None
        self._recording_origin_ns: int | None = None
        self._sink_lock = Lock()
        self._last_diagnostics_ns = 0
        self._stream_healthy = False
        self._last_readiness_error_count = 0
        self._last_timeout_error_count = 0
        self._validation_required = 1
        self._consecutive_valid_samples = 0
        self._validation_started_ns = 0
        self._validation_timeout_s = 3.0
        self._validation_timeout_reported = False
        self._stream_timeout_s = 2.0

    def set_recording_sink(self, sink: Callable[[object], bool] | None) -> None:
        with self._sink_lock:
            self._recording_sink = sink

    def set_recording_origin(self, host_monotonic_ns: int | None) -> None:
        """Set the authoritative recording origin without touching the port."""

        with self._sink_lock:
            self._recording_origin_ns = (
                None if host_monotonic_ns is None else int(host_monotonic_ns)
            )

    def set_sample_observer(self, observer: Callable[[object], None] | None) -> None:
        with self._sink_lock:
            self._sample_observer = observer

    @Slot()
    def refresh_ports(self) -> None:
        """Enumerate ports without opening a second serial connection."""

        if self._service is not None:
            self.error.emit("Disconnect the active serial source before refreshing ports.")
            return
        try:
            self.ports_found.emit(enumerate_serial_ports())
        except Exception as exc:
            self.error.emit(str(exc))

    @Slot(object)
    def set_calibration(self, calibration: LoadCellCalibration | None) -> None:
        if self._service is not None and hasattr(self._service, "set_calibration"):
            self._service.set_calibration(calibration)

    @Slot(str, object, str, object)
    def connect_serial(
        self,
        mode: str,
        config: LoadCellConfig,
        source_or_port: str = "",
        calibration: LoadCellCalibration | None = None,
    ) -> None:
        self.disconnect_serial()
        try:
            normalized = mode.strip().lower()
            if normalized == "playback (simulation)":
                service = SerialPlaybackSource(source_or_port, calibration=calibration)
                info = service.connect(config)
            elif normalized == "synthetic (simulation)":
                service = SyntheticLoadCellSource(
                    calibration=calibration,
                    duration_s=60.0,
                )
                info = service.connect(config)
            else:
                service = SerialService(
                    calibration=calibration,
                    read_timeout_s=float(getattr(config, "read_timeout_s", 0.05)),
                )
                info = service.connect(config, port=source_or_port)
            self._service = service
            self._last_diagnostics_ns = time.perf_counter_ns()
            diagnostics = dict(service.diagnostics())
            self._last_readiness_error_count = int(
                diagnostics.get("readiness_error_count", 0)
            )
            self._last_timeout_error_count = int(
                diagnostics.get("timeout_error_count", 0)
            )
            self._stream_healthy = False
            self._validation_required = max(
                1, int(getattr(config, "validation_min_readings", 1))
            )
            self._consecutive_valid_samples = 0
            self._validation_started_ns = time.perf_counter_ns()
            self._validation_timeout_s = float(
                getattr(config, "validation_timeout_s", 3.0)
            )
            self._validation_timeout_reported = False
            self._stream_timeout_s = float(getattr(config, "stream_timeout_s", 2.0))
            self.connected.emit(info)
            self.stream_health_changed.emit(
                False,
                ErrorCode.HX711_NOT_READY.value,
                f"Port opened. Waiting for {self._validation_required} valid HX711 readings.",
            )
            if self._timer is None:
                self._timer = QTimer(self)
                self._timer.timeout.connect(self._read_once)
            # Synthetic samples carry scheduled monotonic timestamps.  Pace their
            # delivery at the configured source rate so the GUI, calibration
            # windows, and diagnostics see the same realistic stream cadence.
            if isinstance(service, SyntheticLoadCellSource):
                interval_ms = max(1, round(1000.0 / service.sample_rate_hz))
            else:
                interval_ms = 1
            self._timer.start(interval_ms)
        except Exception as exc:
            self._service = None
            self.error.emit(str(exc))

    @Slot(str)
    def send_command(self, command: str) -> None:
        if self._service is None:
            self.error.emit("Serial service is not connected.")
            return
        try:
            self.command_response.emit(self._service.send_command(command))
        except Exception as exc:
            self.error.emit(str(exc))

    @Slot()
    def disconnect_serial(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        service = self._service
        self._service = None
        self._stream_healthy = False
        self._consecutive_valid_samples = 0
        self._validation_started_ns = 0
        self._validation_timeout_reported = False
        self.sample_buffer.clear()
        if service is not None:
            try:
                service.disconnect()
            except Exception as exc:
                self.error.emit(str(exc))
        self.disconnected.emit()
        self.stream_health_changed.emit(
            False,
            ErrorCode.SERIAL_DISCONNECTED.value,
            "Load-cell source disconnected.",
        )

    @Slot()
    def _read_once(self) -> None:
        service = self._service
        if service is None:
            return
        try:
            sample = service.read_sample()
            diagnostics = dict(service.diagnostics())
            readiness_errors = int(diagnostics.get("readiness_error_count", 0))
            timeout_errors = int(diagnostics.get("timeout_error_count", 0))
            if readiness_errors > self._last_readiness_error_count:
                self._set_stream_health(
                    False,
                    ErrorCode.HX711_NOT_READY,
                    "HX711 is not ready; recording remains blocked while recovery is attempted.",
                )
            if timeout_errors > self._last_timeout_error_count:
                self._set_stream_health(
                    False,
                    ErrorCode.HX711_TIMEOUT,
                    "HX711 produced no fresh conversion within the firmware timeout.",
                )
            self._last_readiness_error_count = readiness_errors
            self._last_timeout_error_count = timeout_errors
            if sample is not None:
                self._consecutive_valid_samples += 1
                validated = self._consecutive_valid_samples >= self._validation_required
                if validated:
                    self._validation_timeout_reported = False
                    self._validation_started_ns = 0
                self._set_stream_health(
                    validated,
                    ErrorCode.NONE if validated else ErrorCode.HX711_NOT_READY,
                    (
                        "Receiving valid raw HX711 readings; calibration is ready."
                        if validated
                        else f"Receiving valid raw readings ({self._consecutive_valid_samples}/"
                        f"{self._validation_required}); continuing validation."
                    ),
                )
                self.sample_buffer.publish(sample)
                self.sample_available.emit()
                with self._sink_lock:
                    sink = self._recording_sink
                    observer = self._sample_observer
                    recording_origin_ns = self._recording_origin_ns
                if observer is not None:
                    observer(sample)
                recording_sample = sample
                if recording_origin_ns is not None:
                    if int(sample.host_monotonic_ns) < recording_origin_ns:
                        recording_sample = None
                    else:
                        recording_sample = replace(
                            sample,
                            elapsed_time_s=(
                                int(sample.host_monotonic_ns) - recording_origin_ns
                            )
                            / 1_000_000_000.0,
                        )
                if sink is not None and recording_sample is not None and not sink(recording_sample):
                    if self._timer is not None:
                        self._timer.stop()
                    self.error.emit(
                        "Recording queue saturated; serial acquisition stopped safely."
                    )
                    return
            elif not self._stream_healthy and self._validation_started_ns:
                validation_elapsed_s = (
                    time.perf_counter_ns() - self._validation_started_ns
                ) / 1e9
                if (
                    validation_elapsed_s >= self._validation_timeout_s
                    and not self._validation_timeout_reported
                ):
                    self._validation_timeout_reported = True
                    self.stream_health_changed.emit(
                        False,
                        ErrorCode.HX711_TIMEOUT.value,
                        "Serial port opened, but the required consecutive valid "
                        f"readings did not arrive within {self._validation_timeout_s:g} s. "
                        "Validation is still retrying; inspect Raw Serial Monitor, "
                        "baud rate, parser format, and HX711 wiring.",
                    )
            elif self._stream_healthy:
                since_valid = diagnostics.get("seconds_since_last_valid")
                try:
                    stopped = float(since_valid) >= self._stream_timeout_s
                except (TypeError, ValueError):
                    stopped = False
                if stopped:
                    self._consecutive_valid_samples = 0
                    self._set_stream_health(
                        False,
                        ErrorCode.HX711_TIMEOUT,
                        "Sensor timeout: the serial port is open but the reading stream stopped.",
                    )
            now = time.perf_counter_ns()
            if now - self._last_diagnostics_ns >= 500_000_000:
                self.diagnostics_available.emit(diagnostics)
                self._last_diagnostics_ns = now
        except Exception as exc:
            if self._timer is not None:
                self._timer.stop()
            self._release_failed_service(str(exc))

    def _set_stream_health(
        self, healthy: bool, error_code: ErrorCode, message: str
    ) -> None:
        changed = bool(healthy) != self._stream_healthy
        self._stream_healthy = bool(healthy)
        if changed or not healthy:
            self.stream_health_changed.emit(
                bool(healthy), error_code.value, str(message)
            )

    def _release_failed_service(self, message: str) -> None:
        """Release a failed serial owner and emit a definitive disconnect."""

        service = self._service
        self._service = None
        self._stream_healthy = False
        self._consecutive_valid_samples = 0
        self._validation_started_ns = 0
        self._validation_timeout_reported = False
        self.sample_buffer.clear()
        release_error = ""
        if service is not None:
            try:
                service.disconnect()
            except Exception as exc:
                release_error = f"; disconnect cleanup failed: {exc}"
        self.error.emit(f"{message}{release_error}")
        self.disconnected.emit()
        self.stream_health_changed.emit(
            False,
            ErrorCode.SERIAL_DISCONNECTED.value,
            f"{message}{release_error}",
        )


@dataclass(frozen=True, slots=True)
class _FrameEvent:
    capture_frame_id: int
    frame: CapturedFrame
    motion_context: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _LoadEvent:
    sample: object


class RecordingThread(QThread):
    """Bounded Qt worker that owns the processing pipeline and recorder calls."""

    session_started = Signal(object)
    completed = Signal(object)
    failed = Signal(str, object)
    queue_depth_changed = Signal(int)

    def __init__(
        self,
        *,
        pipeline: ProcessingPipeline,
        recorder: SessionRecorder,
        recorder_start_kwargs: Mapping[str, object],
        recording_start_monotonic_ns: int,
        queue_size: int,
        display_buffer: LatestValueBuffer | None = None,
        motion_context_provider: Callable[[], object] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.pipeline = pipeline
        self.recorder = recorder
        self.recorder_start_kwargs = dict(recorder_start_kwargs)
        self.recording_start_monotonic_ns = int(recording_start_monotonic_ns)
        self.display_buffer = display_buffer
        self.motion_context_provider = motion_context_provider
        self._queue: Queue[_FrameEvent | _LoadEvent] = Queue(maxsize=queue_size)
        self._next_frame_id = 0
        self._state_lock = Lock()
        self._accepting = True
        self._finalize_requested = Event()
        self._abort_requested = Event()
        self._abort_message = ""
        self._abort_error_code = ErrorCode.SHUTDOWN_REQUESTED
        self._ingress_frame_count = 0
        self._ingress_loadcell_count = 0
        self._processed_frame_count = 0
        self._pipeline_error_count = 0
        self._feature_processing_ns = 0
        self._file_writing_ns = 0

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def submit_frame(self, frame: CapturedFrame) -> bool:
        # A callback already in flight when Stop is clicked is simply no longer
        # accepted.  Returning success prevents the acquisition owner from
        # falsely reporting a saturated queue during normal finalization.
        with self._state_lock:
            if not self._accepting:
                return self._finalize_requested.is_set() and not self._abort_requested.is_set()
            event = _FrameEvent(
                self._next_frame_id,
                frame,
                self._motion_snapshot(),
            )
            try:
                self._queue.put_nowait(event)
            except Full:
                self._request_abort_locked(
                    "Recording queue saturated while accepting a frame",
                    ErrorCode.RECORDING_QUEUE_SATURATED,
                )
                return False
            self._next_frame_id += 1
            self._ingress_frame_count += 1
        self.queue_depth_changed.emit(self._queue.qsize())
        return True

    def submit_load_sample(self, sample: object) -> bool:
        with self._state_lock:
            if not self._accepting:
                return self._finalize_requested.is_set() and not self._abort_requested.is_set()
            try:
                motion = self._motion_snapshot()
                if motion and hasattr(sample, "__dataclass_fields__"):
                    supported = {
                        key: value
                        for key, value in motion.items()
                        if key in getattr(sample, "__dataclass_fields__", {})
                    }
                    if supported:
                        sample = replace(sample, **supported)
                self._queue.put_nowait(_LoadEvent(sample))
            except Full:
                self._request_abort_locked(
                    "Recording queue saturated while accepting a load sample",
                    ErrorCode.RECORDING_QUEUE_SATURATED,
                )
                return False
            self._ingress_loadcell_count += 1
        self.queue_depth_changed.emit(self._queue.qsize())
        return True

    def _motion_snapshot(self) -> dict[str, object]:
        provider = self.motion_context_provider
        if provider is None:
            return {}
        value = provider()
        if hasattr(value, "as_row"):
            return dict(value.as_row())
        if isinstance(value, Mapping):
            return dict(value)
        raise TypeError("motion context provider must return a mapping or MotionSnapshot")

    def request_finalize(self) -> None:
        with self._state_lock:
            if self._abort_requested.is_set():
                return
            self._accepting = False
            self._finalize_requested.set()

    def request_abort(
        self,
        message: str,
        error_code: ErrorCode = ErrorCode.SHUTDOWN_REQUESTED,
    ) -> None:
        """Request one safe partial stop, preserving the first causal fault."""

        with self._state_lock:
            self._request_abort_locked(message, error_code)

    def _request_abort_locked(self, message: str, error_code: ErrorCode) -> None:
        """Set the first terminal fault while the single state lock is held."""

        if self._abort_requested.is_set():
            return
        self._accepting = False
        self._abort_message = str(message)
        self._abort_error_code = error_code
        self._abort_requested.set()

    def _terminal_and_empty(self) -> bool:
        """Atomically decide whether no producer can enqueue another event."""

        with self._state_lock:
            return (
                not self._accepting
                and (self._finalize_requested.is_set() or self._abort_requested.is_set())
                and self._queue.empty()
            )

    def _publish_ingress_counts(self) -> None:
        setter = getattr(self.recorder, "set_ingress_counts", None)
        if callable(setter):
            setter(
                accepted_frame_count=self._ingress_frame_count,
                accepted_loadcell_sample_count=self._ingress_loadcell_count,
            )

    def run(self) -> None:
        try:
            session_dir = self.recorder.start(**self.recorder_start_kwargs)
            self.session_started.emit(session_dir)
            while True:
                if self._terminal_and_empty():
                    break
                try:
                    event = self._queue.get(timeout=0.05)
                except Empty:
                    continue
                try:
                    if isinstance(event, _FrameEvent):
                        processing_started_ns = time.perf_counter_ns()
                        process_kwargs = {
                            "capture_frame_id": event.capture_frame_id,
                            "recording_start_monotonic_ns": (
                                self.recording_start_monotonic_ns
                            ),
                        }
                        if event.motion_context:
                            process_kwargs["motion_context"] = event.motion_context
                        processed = self.pipeline.process(
                            event.frame,
                            **process_kwargs,
                        )
                        processing_finished_ns = time.perf_counter_ns()
                        self.recorder.record_frame(processed)
                        writing_finished_ns = time.perf_counter_ns()
                        self._processed_frame_count += 1
                        row_error_code = str(
                            processed.feature_row.get("error_code", "")
                        )
                        if row_error_code not in {"", ErrorCode.NONE.value}:
                            self._pipeline_error_count += 1
                        self._feature_processing_ns += (
                            processing_finished_ns - processing_started_ns
                        )
                        self._file_writing_ns += (
                            writing_finished_ns - processing_finished_ns
                        )
                        if self.display_buffer is not None:
                            self.display_buffer.publish(
                                {
                                    "feature_row": dict(processed.feature_row),
                                    "processed_frame_count": self._processed_frame_count,
                                    "pipeline_error_count": self._pipeline_error_count,
                                    "feature_processing_ns": self._feature_processing_ns,
                                    "file_writing_ns": self._file_writing_ns,
                                }
                            )
                    else:
                        self.recorder.record_loadcell_sample(event.sample)
                finally:
                    self._queue.task_done()
                    self.queue_depth_changed.emit(self._queue.qsize())
            self._publish_ingress_counts()
            if self._abort_requested.is_set():
                with self._state_lock:
                    abort_code = self._abort_error_code
                    abort_message = self._abort_message or "Recording aborted"
                self.recorder.abort(
                    abort_code,
                    abort_message,
                )
                self.failed.emit(
                    abort_message,
                    self.recorder.session_directory,
                )
            else:
                self.completed.emit(self.recorder.finalize())
        except Exception as exc:
            with self._state_lock:
                self._request_abort_locked(str(exc), ErrorCode.INTERNAL_ERROR)
            self._publish_ingress_counts()
            cleanup_error = ""
            try:
                self.recorder.abort(ErrorCode.INTERNAL_ERROR, str(exc))
            except Exception as abort_exc:
                cleanup_error = f"; recorder cleanup failed: {abort_exc}"
            self.failed.emit(
                f"{exc}{cleanup_error}", getattr(self.recorder, "session_directory", None)
            )


class FunctionThread(QThread):
    """Run one bounded callable and return its value through Qt signals."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        function: Callable[..., object],
        *args: object,
        parent: QObject | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(parent)
        self._function = function
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            self.succeeded.emit(self._function(*self._args, **self._kwargs))
        except Exception as exc:
            self.failed.emit(str(exc))


class LiveOpticalThread(QThread):
    """Drop-stale live optical analysis that never owns acquisition or files."""

    failed = Signal(str)

    def __init__(
        self,
        *,
        processing_config: ProcessingConfig,
        rois: tuple[ROI, ...],
        baseline: BaselineRecord,
        result_buffer: LatestValueBuffer,
        generation: int = 0,
        target_roi_ground_truth: int = 1,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.processing_config = processing_config
        self.rois = tuple(rois)
        self.baseline = baseline
        self.result_buffer = result_buffer
        self.generation = int(generation)
        self.target_roi_ground_truth = int(target_roi_ground_truth)
        self._queue: Queue[CapturedFrame] = Queue(maxsize=1)
        self._stop_requested = Event()
        self._origin_ns: int | None = None
        self.dropped_frames = 0

    def submit_frame(self, frame: CapturedFrame) -> bool:
        """Keep only the newest live-analysis frame; recording is unaffected."""

        if self._stop_requested.is_set():
            return True
        try:
            self._queue.put_nowait(frame)
        except Full:
            with suppress(Empty):
                self._queue.get_nowait()
                self._queue.task_done()
                self.dropped_frames += 1
            try:
                self._queue.put_nowait(frame)
            except Full:
                self.dropped_frames += 1
        return True

    def request_stop(self) -> None:
        self._stop_requested.set()

    def run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                captured = self._queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                if self._origin_ns is None:
                    self._origin_ns = captured.host_monotonic_ns
                optical = process_optical_frame(
                    captured.original_bgr,
                    self.rois,
                    self.baseline,
                    minimum_saturation_for_h=(
                        self.processing_config.minimum_saturation_for_h
                    ),
                    active_delta_v_threshold=(
                        self.processing_config.active_delta_v_threshold
                    ),
                    localization_min_mean_delta_v=(
                        self.processing_config.localization_min_mean_delta_v
                    ),
                    target_roi_ground_truth=self.target_roi_ground_truth,
                )
                payload = dict(optical.to_export_dict())
                payload.update(
                    {
                        "capture_frame_id": captured.source_frame_id,
                        "host_monotonic_ns": captured.host_monotonic_ns,
                        "elapsed_time_s": (
                            captured.host_monotonic_ns - self._origin_ns
                        )
                        / 1_000_000_000.0,
                        "analysis_dropped_frames": self.dropped_frames,
                        "analysis_generation": self.generation,
                        "quantitative_analysis_source": "Original frame",
                        "hsv_frame_source": "Original camera frame",
                    }
                )
                self.result_buffer.publish(payload)
            except Exception as exc:
                self.failed.emit(str(exc))
            finally:
                self._queue.task_done()


class MotionMagnificationThread(QThread):
    """Drop-stale motion processing and optional HSV analysis.

    When a quantitative-analysis buffer is supplied, ROI features are extracted
    from the magnified output in this same worker.  This keeps magnified pixels,
    source frame identity, and HSV measurements together without doing image
    processing in the GUI thread.
    """

    failed = Signal(str)

    def __init__(
        self,
        *,
        config: MotionMagnificationConfig,
        result_buffer: LatestValueBuffer,
        rois: tuple[ROI, ...] = (),
        processing_config: ProcessingConfig | None = None,
        baseline: BaselineRecord | None = None,
        analysis_result_buffer: LatestValueBuffer | None = None,
        analysis_generation: int = 0,
        target_roi_ground_truth: int = 1,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.result_buffer = result_buffer
        self.rois = tuple(rois)
        self.processing_config = processing_config
        self.baseline = baseline
        self.analysis_result_buffer = analysis_result_buffer
        self.analysis_generation = int(analysis_generation)
        self.target_roi_ground_truth = int(target_roi_ground_truth)
        self._queue: Queue[CapturedFrame] = Queue(maxsize=1)
        self._stop_requested = Event()
        self._reset_requested = Event()
        self._origin_ns: int | None = None
        self.dropped_frames = 0
        self.processed_frames = 0

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def submit_frame(self, frame: CapturedFrame) -> bool:
        if self._stop_requested.is_set():
            return True
        try:
            self._queue.put_nowait(frame)
        except Full:
            with suppress(Empty):
                self._queue.get_nowait()
                self._queue.task_done()
                self.dropped_frames += 1
            with suppress(Full):
                self._queue.put_nowait(frame)
        return True

    def request_stop(self) -> None:
        self._stop_requested.set()

    def request_reset(self) -> None:
        self._reset_requested.set()

    def run(self) -> None:
        magnifier = EulerianMotionMagnifier(self.config)
        last_processed_ns: int | None = None
        minimum_interval_ns = round(1e9 / self.config.target_fps)
        while not self._stop_requested.is_set():
            try:
                captured = self._queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                if self._reset_requested.is_set():
                    magnifier.reset()
                    self._reset_requested.clear()
                if (
                    last_processed_ns is not None
                    and captured.host_monotonic_ns - last_processed_ns
                    < minimum_interval_ns
                ):
                    self.dropped_frames += 1
                    continue
                result = magnifier.process(
                    captured.original_bgr,
                    host_monotonic_ns=captured.host_monotonic_ns,
                    source_frame_id=captured.source_frame_id,
                    rois=self.rois,
                )
                if self._origin_ns is None:
                    self._origin_ns = result.host_monotonic_ns
                last_processed_ns = captured.host_monotonic_ns
                self.processed_frames += 1
                if (
                    self.analysis_result_buffer is not None
                    and self.processing_config is not None
                    and self.baseline is not None
                ):
                    analysis_frame = (
                        result.color_bgr
                        if self.config.mode == "Color magnification"
                        else result.intensity_bgr
                    )
                    optical = process_optical_frame(
                        analysis_frame,
                        self.rois,
                        self.baseline,
                        minimum_saturation_for_h=(
                            self.processing_config.minimum_saturation_for_h
                        ),
                        active_delta_v_threshold=(
                            self.processing_config.active_delta_v_threshold
                        ),
                        localization_min_mean_delta_v=(
                            self.processing_config.localization_min_mean_delta_v
                        ),
                        target_roi_ground_truth=self.target_roi_ground_truth,
                    )
                    payload = dict(optical.to_export_dict())
                    payload.update(
                        {
                            "capture_frame_id": result.source_frame_id,
                            "host_monotonic_ns": result.host_monotonic_ns,
                            "elapsed_time_s": (
                                result.host_monotonic_ns - self._origin_ns
                            )
                            / 1_000_000_000.0,
                            "analysis_dropped_frames": self.dropped_frames,
                            "analysis_generation": self.analysis_generation,
                            "quantitative_analysis_source": "Motion-magnified frame",
                            "hsv_frame_source": (
                                "motion-magnified color frame"
                                if self.config.mode == "Color magnification"
                                else "motion-magnified intensity frame"
                            ),
                        }
                    )
                    self.analysis_result_buffer.publish(payload)
                self.result_buffer.publish(
                    {
                        "result": result,
                        "processed_frames": self.processed_frames,
                        "dropped_frames": self.dropped_frames,
                    }
                )
            except Exception as exc:
                self.failed.emit(str(exc))
            finally:
                self._queue.task_done()


__all__ = [
    "CameraWorker",
    "FunctionThread",
    "LatestValueBuffer",
    "LiveOpticalThread",
    "MotionMagnificationThread",
    "RecordingThread",
    "SampleWindowCollector",
    "SerialWorker",
]
