"""Deterministic, hardware-free camera and HX711 service implementations.

The simulation services implement the same narrow contracts used by physical
camera and serial owners.  They deliberately stop at the acquisition boundary:
feature extraction, synchronization, recording, and export remain production
pipeline responsibilities.

Synthetic sources use a simulated acquisition schedule anchored to
``time.perf_counter_ns()``.  Supplying ``base_monotonic_ns`` and
``base_wall_clock`` makes absolute timestamps reproducible and lets independent
camera and load-cell sources share one time origin.  Video-file playback uses
the host receipt clock by default; supplying a base makes its timestamps follow
the file's reported FPS, which is useful for fast deterministic smoke tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import statistics
import time
from typing import Callable, Mapping

import cv2
import numpy as np

from core.models import (
    CameraBackend,
    CameraConfig,
    ErrorCode,
    LoadCellCalibration,
    LoadCellConfig,
    LoadCellSample,
    ROI,
)
from processing.roi_manager import ROILayout
from services.interfaces import (
    CameraConnectionInfo,
    CameraPropertyReadback,
    CapturedFrame,
    SerialConnectionInfo,
)


_NS_PER_SECOND = 1_000_000_000
_US_PER_SECOND = 1_000_000
_ARDUINO_MICROS_MODULUS = 1 << 32
_SIMULATION_BACKEND = "Synthetic Camera (Simulation)"
_SIMULATION_DEVICE_NAME = "SIMULATED_HX711"
_SIMULATION_PROTOCOL_VERSION = "SIM-1.0"
_SIMULATION_FIRMWARE_VERSION = "synthetic-1.0"


def _positive_finite(value: float, name: str) -> float:
    result = float(value)
    if isinstance(value, bool) or not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return result


def _nonnegative_finite(value: float, name: str) -> float:
    result = float(value)
    if isinstance(value, bool) or not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("base_wall_clock must be a valid ISO 8601 timestamp") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("base_wall_clock must be a datetime, ISO string, or None")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("base_wall_clock must include a timezone")
    return parsed.astimezone(timezone.utc)


def _wall_clock_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(slots=True)
class _TimestampSequence:
    """Provide either scheduled simulation timestamps or live receipt times."""

    rate_hz: float
    scheduled: bool
    requested_base_monotonic_ns: int | None = None
    requested_base_wall_clock: datetime | str | None = None
    monotonic_clock: Callable[[], int] = time.perf_counter_ns
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    _base_monotonic_ns: int = field(init=False, default=0)
    _base_wall_clock: datetime = field(
        init=False, default_factory=lambda: datetime.now(timezone.utc)
    )
    _last_monotonic_ns: int = field(init=False, default=-1)

    def __post_init__(self) -> None:
        self.rate_hz = _positive_finite(self.rate_hz, "rate_hz")
        if self.requested_base_monotonic_ns is not None:
            if isinstance(self.requested_base_monotonic_ns, bool):
                raise TypeError("base_monotonic_ns must be an integer")
            self.requested_base_monotonic_ns = int(self.requested_base_monotonic_ns)
            if self.requested_base_monotonic_ns < 0:
                raise ValueError("base_monotonic_ns must be nonnegative")
        if self.requested_base_wall_clock is not None:
            _utc_datetime(self.requested_base_wall_clock)
    def start(self) -> None:
        actual_monotonic_ns = int(self.monotonic_clock())
        if actual_monotonic_ns < 0:
            raise ValueError("monotonic clock returned a negative timestamp")
        self._base_monotonic_ns = (
            actual_monotonic_ns
            if self.requested_base_monotonic_ns is None
            else self.requested_base_monotonic_ns
        )
        if self.requested_base_wall_clock is None:
            captured_wall = self.wall_clock()
            if not isinstance(captured_wall, datetime):
                raise TypeError("wall clock must return a datetime")
            self._base_wall_clock = _utc_datetime(captured_wall)
        else:
            self._base_wall_clock = _utc_datetime(self.requested_base_wall_clock)
        self._last_monotonic_ns = self._base_monotonic_ns - 1

    @property
    def base_monotonic_ns(self) -> int:
        return self._base_monotonic_ns

    def capture(self, item_index: int) -> tuple[int, str]:
        if item_index < 0:
            raise ValueError("item_index must be nonnegative")
        if self.scheduled:
            offset_ns = round(item_index * _NS_PER_SECOND / self.rate_hz)
            monotonic_ns = self._base_monotonic_ns + offset_ns
            wall_value = self._base_wall_clock + timedelta(
                microseconds=offset_ns / 1_000
            )
        else:
            monotonic_ns = int(self.monotonic_clock())
            if monotonic_ns < 0:
                raise ValueError("monotonic clock returned a negative timestamp")
            wall_value = self.wall_clock()
            if not isinstance(wall_value, datetime):
                raise TypeError("wall clock must return a datetime")
            wall_value = _utc_datetime(wall_value)
        # Some platform clocks can repeat at very high call rates.  Preserve a
        # strictly increasing acquisition identity without fabricating a frame.
        if monotonic_ns <= self._last_monotonic_ns:
            monotonic_ns = self._last_monotonic_ns + 1
        self._last_monotonic_ns = monotonic_ns
        return monotonic_ns, _wall_clock_iso(wall_value)


def single_press_curve(
    normalized_time: float,
    *,
    start_fraction: float = 0.25,
    peak_fraction: float = 0.55,
    end_fraction: float = 0.85,
) -> float:
    """Return a smooth, unit-height curve for exactly one intentional press.

    The curve is zero before ``start_fraction`` and after ``end_fraction``.  A
    raised-cosine loading branch reaches one at ``peak_fraction`` and a matching
    unloading branch returns to zero.  This avoids derivative discontinuities
    that would make the synthetic optical and force streams unrealistically
    abrupt.
    """

    start = float(start_fraction)
    peak = float(peak_fraction)
    end = float(end_fraction)
    if not 0.0 <= start < peak < end <= 1.0:
        raise ValueError(
            "press fractions must satisfy 0 <= start < peak < end <= 1"
        )
    position = float(normalized_time)
    if not math.isfinite(position):
        raise ValueError("normalized_time must be finite")
    if position <= start or position >= end:
        return 0.0
    if position <= peak:
        phase = (position - start) / (peak - start)
        return 0.5 - 0.5 * math.cos(math.pi * phase)
    phase = (position - peak) / (end - peak)
    return 0.5 + 0.5 * math.cos(math.pi * phase)


def create_default_roi_layout(frame_width: int, frame_height: int) -> ROILayout:
    """Create a centered, non-overlapping 3-by-3 layout in ROI 1..9 order."""

    if isinstance(frame_width, bool) or isinstance(frame_height, bool):
        raise TypeError("frame dimensions must be integers")
    width = int(frame_width)
    height = int(frame_height)
    if width < 6 or height < 6:
        raise ValueError("frame dimensions must each be at least 6 pixels")

    x_edges = [round(index * width / 3) for index in range(4)]
    y_edges = [round(index * height / 3) for index in range(4)]
    rois: list[ROI] = []
    for row in range(3):
        for column in range(3):
            cell_width = x_edges[column + 1] - x_edges[column]
            cell_height = y_edges[row + 1] - y_edges[row]
            inset_x = min(max(1, cell_width // 10), (cell_width - 1) // 2)
            inset_y = min(max(1, cell_height // 10), (cell_height - 1) // 2)
            rois.append(
                ROI(
                    roi_id=row * 3 + column + 1,
                    x=x_edges[column] + inset_x,
                    y=y_edges[row] + inset_y,
                    width=cell_width - 2 * inset_x,
                    height=cell_height - 2 * inset_y,
                )
            )
    return ROILayout.create(rois, frame_width=width, frame_height=height)


def _property_value(value: float | bool) -> float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if math.isfinite(numeric):
            return numeric
    raise TypeError("camera property values must be finite numbers or booleans")


class SyntheticCameraSource:
    """Finite deterministic camera source with one localized optical press.

    Frames are C-contiguous ``uint8`` BGR arrays and are marked read-only before
    they cross the service boundary.  The generated scene contains no text,
    rectangles, heatmap, force value, centroid, or other preview annotation.
    """

    def __init__(
        self,
        *,
        seed: int = 0,
        total_frames: int = 120,
        target_roi: int = 5,
        roi_layout: ROILayout | None = None,
        peak_optical_delta_v: float = 160.0,
        press_start_fraction: float = 0.25,
        press_peak_fraction: float = 0.55,
        press_end_fraction: float = 0.85,
        background_level: int = 24,
        static_texture_std: float = 1.5,
        temporal_noise_std: float = 0.0,
        base_monotonic_ns: int | None = None,
        base_wall_clock: datetime | str | None = None,
        monotonic_clock: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if isinstance(total_frames, bool) or int(total_frames) < 1:
            raise ValueError("total_frames must be a positive integer")
        if target_roi not in range(1, 10):
            raise ValueError("target_roi must be between 1 and 9")
        peak = _nonnegative_finite(peak_optical_delta_v, "peak_optical_delta_v")
        if peak > 255.0:
            raise ValueError("peak_optical_delta_v must not exceed 255")
        if not 0 <= int(background_level) <= 255:
            raise ValueError("background_level must be in 0..255")
        _nonnegative_finite(static_texture_std, "static_texture_std")
        _nonnegative_finite(temporal_noise_std, "temporal_noise_std")
        # Validate the complete curve contract now rather than during capture.
        single_press_curve(
            press_peak_fraction,
            start_fraction=press_start_fraction,
            peak_fraction=press_peak_fraction,
            end_fraction=press_end_fraction,
        )

        self.seed = int(seed)
        self.total_frames = int(total_frames)
        self.target_roi = int(target_roi)
        self._requested_layout = roi_layout
        self.peak_optical_delta_v = peak
        self.press_start_fraction = float(press_start_fraction)
        self.press_peak_fraction = float(press_peak_fraction)
        self.press_end_fraction = float(press_end_fraction)
        self.background_level = int(background_level)
        self.static_texture_std = float(static_texture_std)
        self.temporal_noise_std = float(temporal_noise_std)
        self._base_monotonic_ns = base_monotonic_ns
        self._base_wall_clock = base_wall_clock
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock

        self._connected = False
        self._config: CameraConfig | None = None
        self._connection_info: CameraConnectionInfo | None = None
        self._timestamp_sequence: _TimestampSequence | None = None
        self._layout: ROILayout | None = None
        self._frame_index = 0
        self._rng = np.random.default_rng(self.seed)
        self._background: np.ndarray | None = None
        self._signal_unit_bgr: np.ndarray | None = None
        self._settings: dict[str, float | bool] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def simulation_mode(self) -> bool:
        return True

    @property
    def roi_layout(self) -> ROILayout:
        if self._layout is None:
            raise RuntimeError("synthetic camera is not connected")
        return self._layout

    @property
    def connection_info(self) -> CameraConnectionInfo:
        if self._connection_info is None:
            raise RuntimeError("synthetic camera is not connected")
        return self._connection_info

    def connect(
        self, config: CameraConfig, *, source_path: str | None = None
    ) -> CameraConnectionInfo:
        if source_path is not None and str(source_path).strip():
            raise ValueError("SyntheticCameraSource generates frames and takes no source_path")
        self.disconnect()
        width = config.requested_width
        height = config.requested_height
        if self._requested_layout is None:
            layout = create_default_roi_layout(width, height)
        else:
            layout = self._requested_layout
            if layout.frame_width != width or layout.frame_height != height:
                raise ValueError(
                    "roi_layout frame dimensions must match the requested camera mode"
                )

        self._config = config
        self._layout = layout
        self._frame_index = 0
        self._rng = np.random.default_rng(self.seed)
        self._build_scene(width, height)
        self._timestamp_sequence = _TimestampSequence(
            rate_hz=config.requested_fps,
            scheduled=True,
            requested_base_monotonic_ns=self._base_monotonic_ns,
            requested_base_wall_clock=self._base_wall_clock,
            monotonic_clock=self._monotonic_clock,
            wall_clock=self._wall_clock,
        )
        self._timestamp_sequence.start()
        self._connection_info = CameraConnectionInfo(
            device_index=config.device_index,
            backend=_SIMULATION_BACKEND,
            requested_width=width,
            requested_height=height,
            requested_fps=config.requested_fps,
            actual_width=width,
            actual_height=height,
            actual_fps=config.requested_fps,
            simulation_mode=True,
            source_label=f"Synthetic single press at ROI {self.target_roi}",
        )
        self._connected = True
        return self._connection_info

    def disconnect(self) -> None:
        self._connected = False
        self._config = None
        self._connection_info = None
        self._timestamp_sequence = None
        self._layout = None
        self._frame_index = 0
        self._background = None
        self._signal_unit_bgr = None
        self._settings.clear()

    def _build_scene(self, width: int, height: int) -> None:
        texture = self._rng.normal(
            0.0, self.static_texture_std, size=(height, width, 1)
        )
        channel_offsets = np.array([0.0, 1.0, 2.0], dtype=np.float32)
        background = self.background_level + texture + channel_offsets
        self._background = np.clip(np.rint(background), 0, 255).astype(np.uint8)

        target = self.roi_layout.rois[self.target_roi - 1]
        local_y, local_x = np.mgrid[0 : target.height, 0 : target.width]
        center_x = (target.width - 1) / 2.0
        center_y = (target.height - 1) / 2.0
        sigma_x = max(target.width * 0.24, 1.0)
        sigma_y = max(target.height * 0.24, 1.0)
        gaussian = np.exp(
            -0.5
            * (
                ((local_x - center_x) / sigma_x) ** 2
                + ((local_y - center_y) / sigma_y) ** 2
            )
        ).astype(np.float32)
        signal = np.zeros((height, width, 3), dtype=np.float32)
        # A blue-dominant optical response raises V while retaining meaningful
        # OpenCV hue/saturation in the pressed region.
        bgr_weights = np.array([1.0, 0.62, 0.24], dtype=np.float32)
        signal[
            target.y : target.y + target.height,
            target.x : target.x + target.width,
            :,
        ] = gaussian[:, :, None] * bgr_weights
        self._signal_unit_bgr = signal

    def press_level_for_frame(self, source_frame_id: int) -> float:
        if source_frame_id < 0 or source_frame_id >= self.total_frames:
            raise ValueError("source_frame_id is outside this finite simulation")
        normalized = (
            0.0
            if self.total_frames == 1
            else source_frame_id / (self.total_frames - 1)
        )
        return single_press_curve(
            normalized,
            start_fraction=self.press_start_fraction,
            peak_fraction=self.press_peak_fraction,
            end_fraction=self.press_end_fraction,
        )

    def read_frame(self) -> CapturedFrame | None:
        if not self._connected:
            raise RuntimeError("synthetic camera is not connected")
        if self._frame_index >= self.total_frames:
            return None
        if (
            self._background is None
            or self._signal_unit_bgr is None
            or self._timestamp_sequence is None
        ):
            raise RuntimeError("synthetic camera scene was not initialized")

        source_frame_id = self._frame_index
        press_level = self.press_level_for_frame(source_frame_id)
        frame_values = self._background.astype(np.float32, copy=True)
        frame_values += (
            self._signal_unit_bgr * self.peak_optical_delta_v * press_level
        )
        if self.temporal_noise_std > 0.0:
            frame_values += self._rng.normal(
                0.0, self.temporal_noise_std, size=frame_values.shape[:2] + (1,)
            )
        frame = np.ascontiguousarray(
            np.clip(np.rint(frame_values), 0, 255).astype(np.uint8)
        )
        frame.setflags(write=False)
        host_monotonic_ns, wall_clock_iso = self._timestamp_sequence.capture(
            source_frame_id
        )
        self._frame_index += 1
        return CapturedFrame(
            original_bgr=frame,
            source_frame_id=source_frame_id,
            host_monotonic_ns=host_monotonic_ns,
            wall_clock_iso=wall_clock_iso,
        )

    def apply_settings(
        self, requested: Mapping[str, float | bool]
    ) -> tuple[CameraPropertyReadback, ...]:
        if not self._connected:
            raise RuntimeError("synthetic camera is not connected")
        results: list[CameraPropertyReadback] = []
        for name, raw_value in requested.items():
            value = _property_value(raw_value)
            self._settings[str(name)] = value
            results.append(
                CameraPropertyReadback(
                    property_name=str(name),
                    requested_value=value,
                    actual_value=value,
                    write_succeeded=True,
                    read_succeeded=True,
                    confirmed=True,
                    supported=True,
                    message="Accepted by synthetic simulation; no physical control was changed.",
                )
            )
        return tuple(results)

    def warmup_complete(self, host_monotonic_ns: int) -> bool:
        if not self._connected or self._config is None or self._timestamp_sequence is None:
            return False
        warmup_ns = round(self._config.warmup_seconds * _NS_PER_SECOND)
        return int(host_monotonic_ns) >= (
            self._timestamp_sequence.base_monotonic_ns + warmup_ns
        )


class VideoFileCameraSource:
    """Single-owner OpenCV video-file camera simulation.

    With no fixed base timestamp, each decoded frame is timestamped at host
    receipt using ``time.perf_counter_ns``.  With a fixed base, timestamps use a
    deterministic acquisition schedule at the video's reported FPS.
    """

    def __init__(
        self,
        *,
        loop: bool = False,
        base_monotonic_ns: int | None = None,
        base_wall_clock: datetime | str | None = None,
        monotonic_clock: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.loop = bool(loop)
        self._base_monotonic_ns = base_monotonic_ns
        self._base_wall_clock = base_wall_clock
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._capture: cv2.VideoCapture | None = None
        self._config: CameraConfig | None = None
        self._connection_info: CameraConnectionInfo | None = None
        self._timestamp_sequence: _TimestampSequence | None = None
        self._source_frame_id = 0

    @property
    def is_connected(self) -> bool:
        return self._capture is not None and bool(self._capture.isOpened())

    @property
    def simulation_mode(self) -> bool:
        return True

    @property
    def connection_info(self) -> CameraConnectionInfo:
        if self._connection_info is None:
            raise RuntimeError("video-file camera is not connected")
        return self._connection_info

    def connect(
        self, config: CameraConfig, *, source_path: str | None = None
    ) -> CameraConnectionInfo:
        if source_path is None or not str(source_path).strip():
            raise ValueError("source_path is required for video-file simulation")
        source = Path(source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"video source does not exist: {source}")
        self.disconnect()
        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"OpenCV could not open video source: {source}")

        width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
        height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        reported_fps = float(capture.get(cv2.CAP_PROP_FPS))
        if width <= 0 or height <= 0:
            capture.release()
            raise RuntimeError("video source reported invalid frame dimensions")
        actual_fps = (
            reported_fps
            if math.isfinite(reported_fps) and reported_fps > 0.0
            else config.requested_fps
        )
        scheduled = self._base_monotonic_ns is not None
        sequence = _TimestampSequence(
            rate_hz=actual_fps,
            scheduled=scheduled,
            requested_base_monotonic_ns=self._base_monotonic_ns,
            requested_base_wall_clock=self._base_wall_clock,
            monotonic_clock=self._monotonic_clock,
            wall_clock=self._wall_clock,
        )
        sequence.start()
        self._capture = capture
        self._config = config
        self._timestamp_sequence = sequence
        self._source_frame_id = 0
        self._connection_info = CameraConnectionInfo(
            device_index=config.device_index,
            backend=CameraBackend.VIDEO_FILE_SIMULATION.value,
            requested_width=config.requested_width,
            requested_height=config.requested_height,
            requested_fps=config.requested_fps,
            actual_width=width,
            actual_height=height,
            actual_fps=actual_fps,
            simulation_mode=True,
            source_label=str(source),
        )
        return self._connection_info

    def disconnect(self) -> None:
        if self._capture is not None:
            self._capture.release()
        self._capture = None
        self._config = None
        self._connection_info = None
        self._timestamp_sequence = None
        self._source_frame_id = 0

    def read_frame(self) -> CapturedFrame | None:
        if not self.is_connected or self._capture is None:
            raise RuntimeError("video-file camera is not connected")
        ok, decoded = self._capture.read()
        if not ok and self.loop:
            self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0.0)
            ok, decoded = self._capture.read()
        if not ok or decoded is None:
            return None
        if decoded.ndim != 3 or decoded.shape[2] != 3:
            raise RuntimeError("decoded video frame is not three-channel BGR")
        frame = np.ascontiguousarray(decoded, dtype=np.uint8)
        frame.setflags(write=False)
        if self._timestamp_sequence is None:
            raise RuntimeError("video timestamp sequence was not initialized")
        source_frame_id = self._source_frame_id
        host_monotonic_ns, wall_clock_iso = self._timestamp_sequence.capture(
            source_frame_id
        )
        self._source_frame_id += 1
        return CapturedFrame(
            original_bgr=frame,
            source_frame_id=source_frame_id,
            host_monotonic_ns=host_monotonic_ns,
            wall_clock_iso=wall_clock_iso,
        )

    def apply_settings(
        self, requested: Mapping[str, float | bool]
    ) -> tuple[CameraPropertyReadback, ...]:
        if not self.is_connected:
            raise RuntimeError("video-file camera is not connected")
        results: list[CameraPropertyReadback] = []
        for name, raw_value in requested.items():
            value = _property_value(raw_value)
            results.append(
                CameraPropertyReadback(
                    property_name=str(name),
                    requested_value=value,
                    actual_value=None,
                    write_succeeded=False,
                    read_succeeded=False,
                    confirmed=False,
                    supported=False,
                    message="Video-file playback has no physical camera controls.",
                )
            )
        return tuple(results)

    def warmup_complete(self, host_monotonic_ns: int) -> bool:
        if not self.is_connected or self._config is None or self._timestamp_sequence is None:
            return False
        warmup_ns = round(self._config.warmup_seconds * _NS_PER_SECOND)
        return int(host_monotonic_ns) >= (
            self._timestamp_sequence.base_monotonic_ns + warmup_ns
        )


class SyntheticLoadCellSource:
    """Finite deterministic stream of new, calibrated HX711 conversions."""

    def __init__(
        self,
        *,
        seed: int = 0,
        sample_rate_hz: float = 80.0,
        duration_s: float = 4.0,
        total_samples: int | None = None,
        calibration: LoadCellCalibration | None = None,
        counts_per_gram: float = 100.0,
        tare_raw: float = 100_000.0,
        peak_force_gf: float = 200.0,
        noise_std_counts: float = 2.0,
        press_start_fraction: float = 0.25,
        press_peak_fraction: float = 0.55,
        press_end_fraction: float = 0.85,
        base_monotonic_ns: int | None = None,
        base_wall_clock: datetime | str | None = None,
        arduino_start_micros: int = 0,
        monotonic_clock: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.seed = int(seed)
        self.sample_rate_hz = _positive_finite(sample_rate_hz, "sample_rate_hz")
        self.duration_s = _positive_finite(duration_s, "duration_s")
        if total_samples is not None and (
            isinstance(total_samples, bool) or int(total_samples) < 1
        ):
            raise ValueError("total_samples must be a positive integer or None")
        self.total_samples = (
            max(1, round(self.duration_s * self.sample_rate_hz) + 1)
            if total_samples is None
            else int(total_samples)
        )
        if calibration is None:
            factor = float(counts_per_gram)
            if not math.isfinite(factor) or abs(factor) < 1.0e-12:
                raise ValueError("counts_per_gram must be finite and nonzero")
            self.counts_per_gram = factor
            self.tare_raw = float(tare_raw)
        else:
            self.counts_per_gram = float(calibration.counts_per_gram)
            self.tare_raw = float(calibration.tare_raw)
        if not math.isfinite(self.tare_raw):
            raise ValueError("tare_raw must be finite")
        self.calibration = calibration
        self.peak_force_gf = _nonnegative_finite(peak_force_gf, "peak_force_gf")
        self.noise_std_counts = _nonnegative_finite(
            noise_std_counts, "noise_std_counts"
        )
        single_press_curve(
            press_peak_fraction,
            start_fraction=press_start_fraction,
            peak_fraction=press_peak_fraction,
            end_fraction=press_end_fraction,
        )
        self.press_start_fraction = float(press_start_fraction)
        self.press_peak_fraction = float(press_peak_fraction)
        self.press_end_fraction = float(press_end_fraction)
        if isinstance(arduino_start_micros, bool):
            raise TypeError("arduino_start_micros must be an integer")
        self.arduino_start_micros = int(arduino_start_micros)
        if not 0 <= self.arduino_start_micros < _ARDUINO_MICROS_MODULUS:
            raise ValueError("arduino_start_micros must be an unsigned 32-bit value")
        self._base_monotonic_ns = base_monotonic_ns
        self._base_wall_clock = base_wall_clock
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock

        self._connected = False
        self._streaming = False
        self._config: LoadCellConfig | None = None
        self._connection_info: SerialConnectionInfo | None = None
        self._timestamp_sequence: _TimestampSequence | None = None
        self._sample_index = 0
        self._connection_count = 0
        self._rng = np.random.default_rng(self.seed)
        self._emitted_monotonic_ns: list[int] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def simulation_mode(self) -> bool:
        return True

    @property
    def connection_info(self) -> SerialConnectionInfo:
        if self._connection_info is None:
            raise RuntimeError("synthetic load cell is not connected")
        return self._connection_info

    def connect(
        self, config: LoadCellConfig, *, port: str | None = None
    ) -> SerialConnectionInfo:
        self.disconnect()
        self._connection_count += 1
        self._config = config
        self._sample_index = 0
        self._rng = np.random.default_rng(self.seed)
        self._emitted_monotonic_ns = []
        self._timestamp_sequence = _TimestampSequence(
            rate_hz=self.sample_rate_hz,
            scheduled=True,
            requested_base_monotonic_ns=self._base_monotonic_ns,
            requested_base_wall_clock=self._base_wall_clock,
            monotonic_clock=self._monotonic_clock,
            wall_clock=self._wall_clock,
        )
        self._timestamp_sequence.start()
        device_session_id = f"sim-hx711-{self.seed}-{self._connection_count}"
        self._connection_info = SerialConnectionInfo(
            port=(str(port).strip() if port is not None and str(port).strip() else "SIMULATED"),
            baud_rate=config.baud_rate,
            device_name=_SIMULATION_DEVICE_NAME,
            protocol_version=_SIMULATION_PROTOCOL_VERSION,
            firmware_version=_SIMULATION_FIRMWARE_VERSION,
            device_session_id=device_session_id,
            simulation_mode=True,
        )
        self._connected = True
        self._streaming = True
        return self._connection_info

    def disconnect(self) -> None:
        self._connected = False
        self._streaming = False
        self._config = None
        self._connection_info = None
        self._timestamp_sequence = None
        self._sample_index = 0
        self._emitted_monotonic_ns = []

    def send_command(self, command: str) -> str:
        if not self._connected:
            raise RuntimeError("synthetic load cell is not connected")
        normalized = command.strip().upper()
        if normalized == "PING":
            return "PONG,SIMULATED_HX711"
        if normalized == "START":
            self._streaming = True
            return "OK,START"
        if normalized == "STOP":
            self._streaming = False
            return "OK,STOP"
        if normalized == "STATUS":
            state = "STREAMING" if self._streaming else "STOPPED"
            return f"STATUS,{state},{self._sample_index},{self.total_samples}"
        raise ValueError("command must be PING, START, STOP, or STATUS")

    def press_level_for_sample(self, sample_id: int) -> float:
        if sample_id < 0 or sample_id >= self.total_samples:
            raise ValueError("sample_id is outside this finite simulation")
        normalized = (
            0.0
            if self.total_samples == 1
            else sample_id / (self.total_samples - 1)
        )
        return single_press_curve(
            normalized,
            start_fraction=self.press_start_fraction,
            peak_fraction=self.press_peak_fraction,
            end_fraction=self.press_end_fraction,
        )

    def read_sample(self) -> LoadCellSample | None:
        if not self._connected:
            raise RuntimeError("synthetic load cell is not connected")
        if not self._streaming or self._sample_index >= self.total_samples:
            return None
        if self._timestamp_sequence is None or self._connection_info is None:
            raise RuntimeError("synthetic load-cell timing was not initialized")

        sample_id = self._sample_index
        level = self.press_level_for_sample(sample_id)
        intended_force_gf = self.peak_force_gf * level
        ideal_raw = self.tare_raw + intended_force_gf * self.counts_per_gram
        noise = (
            0.0
            if self.noise_std_counts == 0.0
            else float(self._rng.normal(0.0, self.noise_std_counts))
        )
        raw_adc = int(round(ideal_raw + noise))
        force_gf = (raw_adc - self.tare_raw) / self.counts_per_gram
        force_N = force_gf * 0.00980665
        host_monotonic_ns, wall_clock_iso = self._timestamp_sequence.capture(sample_id)
        elapsed_time_s = sample_id / self.sample_rate_hz
        arduino_unwrapped = self.arduino_start_micros + round(
            sample_id * _US_PER_SECOND / self.sample_rate_hz
        )
        arduino_micros = arduino_unwrapped % _ARDUINO_MICROS_MODULUS
        sample = LoadCellSample(
            host_monotonic_ns=host_monotonic_ns,
            arduino_sample_id=sample_id,
            arduino_micros=arduino_micros,
            raw_adc=raw_adc,
            force_gf=force_gf,
            force_N=force_N,
            device_session_id=self._connection_info.device_session_id,
            elapsed_time_s=elapsed_time_s,
            wall_clock_iso=wall_clock_iso,
            arduino_micros_unwrapped=arduino_unwrapped,
            loadcell_valid=True,
            error_code=ErrorCode.NONE,
            tared_raw=raw_adc - self.tare_raw,
            mass_g=force_gf,
            calibration_factor_counts_per_gram=self.counts_per_gram,
            zero_offset_raw=self.tare_raw,
            serial_port=self._connection_info.port,
            baud_rate=(0 if self._config is None else self._config.baud_rate),
            arduino_timestamp_available=True,
        )
        self._sample_index += 1
        self._emitted_monotonic_ns.append(host_monotonic_ns)
        return sample

    def diagnostics(self) -> Mapping[str, object]:
        intervals_ms = [
            (current - previous) / 1_000_000.0
            for previous, current in zip(
                self._emitted_monotonic_ns, self._emitted_monotonic_ns[1:]
            )
        ]
        if intervals_ms:
            elapsed_s = (
                self._emitted_monotonic_ns[-1] - self._emitted_monotonic_ns[0]
            ) / _NS_PER_SECOND
            measured_rate = (
                (len(self._emitted_monotonic_ns) - 1) / elapsed_s
                if elapsed_s > 0.0
                else math.nan
            )
            minimum_interval = min(intervals_ms)
            median_interval = statistics.median(intervals_ms)
            maximum_interval = max(intervals_ms)
        else:
            measured_rate = math.nan
            minimum_interval = math.nan
            median_interval = math.nan
            maximum_interval = math.nan
        return {
            "simulation_mode": True,
            "configured_sample_rate_hz": self.sample_rate_hz,
            "measured_sample_rate_hz": measured_rate,
            "valid_sample_count": len(self._emitted_monotonic_ns),
            "malformed_line_count": 0,
            "readiness_error_count": 0,
            "timeout_error_count": 0,
            "minimum_sample_interval_ms": minimum_interval,
            "median_sample_interval_ms": median_interval,
            "maximum_sample_interval_ms": maximum_interval,
            "streaming": self._streaming,
            "exhausted": self._sample_index >= self.total_samples,
        }


__all__ = [
    "SyntheticCameraSource",
    "SyntheticLoadCellSource",
    "VideoFileCameraSource",
    "create_default_roi_layout",
    "single_press_curve",
]
