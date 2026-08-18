"""Stable domain contracts shared by acquisition, processing, export, and GUI code.

The dataclasses in this module contain no Qt, OpenCV, serial-port, or file-writer
logic.  They make the scientifically important state explicit and keep hardware
implementations behind the service protocols in :mod:`services.interfaces`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum, IntFlag
import json
import math
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

import numpy as np


SCHEMA_VERSION = "1.1.0"
APPLICATION_VERSION = "1.1.0"
ROI_COUNT = 9
GRAM_FORCE_TO_NEWTON = 0.00980665


class RecordingLifecycle(str, Enum):
    """Lifecycle of one recording; readiness is modeled independently."""

    IDLE = "IDLE"
    RECORDING = "RECORDING"
    FINALIZING = "FINALIZING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


class ErrorCode(str, Enum):
    """Predictable machine-readable error codes retained in exported rows."""

    NONE = "NONE"
    CAMERA_UNAVAILABLE = "CAMERA_UNAVAILABLE"
    CAMERA_DISCONNECTED = "CAMERA_DISCONNECTED"
    CAMERA_SETTINGS_UNCONFIRMED = "CAMERA_SETTINGS_UNCONFIRMED"
    CAMERA_WARMUP_INCOMPLETE = "CAMERA_WARMUP_INCOMPLETE"
    ROI_INVALID = "ROI_INVALID"
    BASELINE_INVALID = "BASELINE_INVALID"
    BASELINE_DRIFT_EXCEEDED = "BASELINE_DRIFT_EXCEEDED"
    FEATURE_EXTRACTION_FAILED = "FEATURE_EXTRACTION_FAILED"
    SERIAL_UNAVAILABLE = "SERIAL_UNAVAILABLE"
    SERIAL_DISCONNECTED = "SERIAL_DISCONNECTED"
    SERIAL_HANDSHAKE_FAILED = "SERIAL_HANDSHAKE_FAILED"
    SERIAL_MALFORMED_LINE = "SERIAL_MALFORMED_LINE"
    HX711_NOT_READY = "HX711_NOT_READY"
    HX711_TIMEOUT = "HX711_TIMEOUT"
    LOAD_CELL_UNCALIBRATED = "LOAD_CELL_UNCALIBRATED"
    CALIBRATION_REJECTED = "CALIBRATION_REJECTED"
    CALIBRATION_VERIFICATION_FAILED = "CALIBRATION_VERIFICATION_FAILED"
    SYNCHRONIZATION_GAP_EXCEEDED = "SYNCHRONIZATION_GAP_EXCEEDED"
    VIDEO_WRITER_FAILED = "VIDEO_WRITER_FAILED"
    RECORDING_QUEUE_SATURATED = "RECORDING_QUEUE_SATURATED"
    DISK_SPACE_LOW = "DISK_SPACE_LOW"
    DISK_WRITE_FAILED = "DISK_WRITE_FAILED"
    FINALIZATION_FAILED = "FINALIZATION_FAILED"
    SHUTDOWN_REQUESTED = "SHUTDOWN_REQUESTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ValidityFlag(IntFlag):
    """Bit flags for compact internal transport; CSV uses named booleans."""

    NONE = 0
    FRAME_VALID = 1 << 0
    BASELINE_VALID = 1 << 1
    LOAD_CELL_VALID = 1 << 2
    SYNCHRONIZATION_VALID = 1 << 3
    SATURATION_WARNING = 1 << 4


class SynchronizationMethod(str, Enum):
    """How a physical load-cell sample was associated with a frame."""

    LINEAR_INTERPOLATION = "linear_interpolation"
    NEAREST_SAMPLE = "nearest"
    INVALID = "invalid"


class CameraBackend(str, Enum):
    """Supported capture backends, including the clearly labeled simulation."""

    DIRECTSHOW = "DirectShow"
    MEDIA_FOUNDATION = "Media Foundation"
    VIDEO_FILE_SIMULATION = "Video File (Simulation)"


@dataclass(frozen=True, slots=True)
class ValidityFlags:
    """Named validity and warning values written with every master row."""

    frame_valid: bool = True
    baseline_valid: bool = True
    loadcell_valid: bool = True
    synchronization_valid: bool = True
    saturation_warning: bool = False

    @property
    def bitmask(self) -> ValidityFlag:
        result = ValidityFlag.NONE
        if self.frame_valid:
            result |= ValidityFlag.FRAME_VALID
        if self.baseline_valid:
            result |= ValidityFlag.BASELINE_VALID
        if self.loadcell_valid:
            result |= ValidityFlag.LOAD_CELL_VALID
        if self.synchronization_valid:
            result |= ValidityFlag.SYNCHRONIZATION_VALID
        if self.saturation_warning:
            result |= ValidityFlag.SATURATION_WARNING
        return result


@dataclass(slots=True)
class ReadinessFlags:
    """Independent prerequisites whose conjunction gates recording."""

    camera_connected: bool = False
    rois_valid: bool = False
    baseline_valid: bool = False
    serial_connected: bool = False
    loadcell_calibrated: bool = False
    calibration_verified: bool = False
    trial_labels_valid: bool = False
    output_directory_valid: bool = False
    video_writer_preflight_passed: bool = False
    disk_space_sufficient: bool = False

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "camera_connected",
        "rois_valid",
        "baseline_valid",
        "serial_connected",
        "loadcell_calibrated",
        "calibration_verified",
        "trial_labels_valid",
        "output_directory_valid",
        "video_writer_preflight_passed",
        "disk_space_sufficient",
    )
    GUIDANCE: ClassVar[Mapping[str, str]] = {
        "camera_connected": "Connect the camera in Camera Setup.",
        "rois_valid": "Define nine valid ROIs in ROI and Baseline.",
        "baseline_valid": "Capture a new unloaded baseline.",
        "serial_connected": "Connect an Arduino that completes the HELLO handshake.",
        "loadcell_calibrated": "Complete an accepted load-cell calibration.",
        "calibration_verified": "Verify the known mass within tolerance.",
        "trial_labels_valid": "Enter all required trial labels.",
        "output_directory_valid": "Choose a writable output directory.",
        "video_writer_preflight_passed": "Run the video-writer preflight test.",
        "disk_space_sufficient": "Free disk space or choose another output drive.",
    }

    @property
    def ready_to_record(self) -> bool:
        """Return true only when every required, independent flag is true."""

        return all(bool(getattr(self, name)) for name in self.REQUIRED_FIELDS)

    @property
    def missing_requirements(self) -> tuple[str, ...]:
        """Return failed prerequisite names in stable workflow order."""

        return tuple(
            name for name in self.REQUIRED_FIELDS if not bool(getattr(self, name))
        )

    def missing_guidance(self) -> dict[str, str]:
        """Map every failed prerequisite to a direct operator action."""

        return {name: self.GUIDANCE[name] for name in self.missing_requirements}

    def as_dict(self) -> dict[str, bool]:
        """Return JSON-ready flags plus the derived readiness value."""

        values = {name: bool(getattr(self, name)) for name in self.REQUIRED_FIELDS}
        values["ready_to_record"] = self.ready_to_record
        return values


def _positive_number(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number, not bool")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return result


def _nonnegative_number(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number, not bool")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number, not bool")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _numerically_equal(left: float, right: float) -> bool:
    """Compare persisted derived values without hiding meaningful tampering."""

    return math.isclose(float(left), float(right), rel_tol=1.0e-9, abs_tol=1.0e-9)


def _aware_iso_timestamp(value: str, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} must not be blank")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return text


@dataclass(frozen=True, slots=True)
class CameraConfig:
    """Requested camera mode and baseline-stability settings."""

    device_index: int = 0
    backend_preference: tuple[str, ...] = ("DirectShow", "Media Foundation")
    requested_width: int = 640
    requested_height: int = 480
    requested_fps: float = 30.0
    rotation_degrees: int = 0
    mirror_horizontal: bool = False
    warmup_seconds: float = 3.0
    baseline_drift_mean_v: float = 5.0
    property_readback_tolerance: float = 0.05

    def __post_init__(self) -> None:
        if self.device_index < 0:
            raise ValueError("device_index must be nonnegative")
        if self.requested_width <= 0 or self.requested_height <= 0:
            raise ValueError("requested camera dimensions must be positive")
        if isinstance(self.rotation_degrees, bool) or self.rotation_degrees not in {
            0,
            90,
            180,
            270,
        }:
            raise ValueError("rotation_degrees must be 0, 90, 180, or 270")
        if not isinstance(self.mirror_horizontal, bool):
            raise ValueError("mirror_horizontal must be a boolean")
        _positive_number(self.requested_fps, "requested_fps")
        _nonnegative_number(self.warmup_seconds, "warmup_seconds")
        _nonnegative_number(self.baseline_drift_mean_v, "baseline_drift_mean_v")
        _nonnegative_number(
            self.property_readback_tolerance, "property_readback_tolerance"
        )
        if not self.backend_preference:
            raise ValueError("backend_preference must not be empty")


@dataclass(frozen=True, slots=True)
class ProcessingConfig:
    """Optical feature, baseline, and localization thresholds."""

    baseline_duration_s: float = 3.0
    minimum_baseline_frames: int = 20
    minimum_saturation_for_h: int = 10
    active_delta_v_threshold: float = 10.0
    localization_min_mean_delta_v: float = 5.0

    def __post_init__(self) -> None:
        _positive_number(self.baseline_duration_s, "baseline_duration_s")
        if self.minimum_baseline_frames < 1:
            raise ValueError("minimum_baseline_frames must be positive")
        if not 0 <= self.minimum_saturation_for_h <= 255:
            raise ValueError("minimum_saturation_for_h must be in 0..255")
        if not 0.0 <= self.active_delta_v_threshold <= 255.0:
            raise ValueError("active_delta_v_threshold must be in 0..255")
        if not 0.0 <= self.localization_min_mean_delta_v <= 255.0:
            raise ValueError("localization_min_mean_delta_v must be in 0..255")


@dataclass(frozen=True, slots=True)
class LoadCellConfig:
    """Serial, calibration, force, and synchronization settings."""

    baud_rate: int = 115200
    startup_window_s: float = 2.5
    startup_delay_s: float = 2.0
    validation_timeout_s: float = 3.0
    validation_min_readings: int = 3
    stream_timeout_s: float = 2.0
    read_timeout_s: float = 0.05
    data_bits: int = 8
    parity: str = "N"
    stop_bits: float = 1.0
    input_format: str = "Auto-detect"
    known_mass_g: float = 200.0
    calibration_window_s: float = 3.0
    minimum_calibration_samples: int = 20
    fallback_minimum_calibration_samples: int = 10
    minimum_calibration_snr: float = 10.0
    maximum_loaded_window_cv_percent: float = 2.0
    minimum_abs_counts_per_gram: float = 1.0e-9
    maximum_tare_std_counts: float = 100.0
    verification_tolerance_percent: float = 5.0
    contact_threshold_N: float = 0.05
    max_sync_gap_ms: float = 200.0

    def __post_init__(self) -> None:
        if self.baud_rate <= 0:
            raise ValueError("baud_rate must be positive")
        for name in (
            "startup_window_s",
            "validation_timeout_s",
            "stream_timeout_s",
            "read_timeout_s",
            "known_mass_g",
            "calibration_window_s",
            "minimum_calibration_snr",
            "maximum_loaded_window_cv_percent",
            "minimum_abs_counts_per_gram",
            "maximum_tare_std_counts",
            "verification_tolerance_percent",
            "max_sync_gap_ms",
        ):
            _positive_number(getattr(self, name), name)
        _nonnegative_number(self.startup_delay_s, "startup_delay_s")
        if self.validation_min_readings < 1:
            raise ValueError("validation_min_readings must be positive")
        if self.data_bits not in {5, 6, 7, 8}:
            raise ValueError("data_bits must be 5, 6, 7, or 8")
        if str(self.parity).upper() not in {"N", "E", "O", "M", "S"}:
            raise ValueError("parity must be N, E, O, M, or S")
        if float(self.stop_bits) not in {1.0, 1.5, 2.0}:
            raise ValueError("stop_bits must be 1, 1.5, or 2")
        if not str(self.input_format).strip():
            raise ValueError("input_format must not be blank")
        _nonnegative_number(self.contact_threshold_N, "contact_threshold_N")
        if self.minimum_calibration_samples < 1:
            raise ValueError("minimum_calibration_samples must be positive")
        if not 1 <= self.fallback_minimum_calibration_samples <= self.minimum_calibration_samples:
            raise ValueError(
                "fallback_minimum_calibration_samples must be between 1 and the normal minimum"
            )


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    """Bounded-queue, incremental-write, video, and disk settings."""

    output_directory: str = "output"
    preview_queue_size: int = 1
    recording_queue_size: int = 120
    csv_flush_interval_s: float = 1.0
    csv_flush_row_count: int = 30
    primary_video_codec: str = "mp4v"
    fallback_video_codec: str = "MJPG"
    minimum_preflight_free_mb: int = 1024
    disk_safety_free_mb: int = 512

    def __post_init__(self) -> None:
        if not self.output_directory.strip():
            raise ValueError("output_directory must not be blank")
        if self.preview_queue_size != 1:
            raise ValueError("preview_queue_size must be 1 for latest-frame policy")
        if self.recording_queue_size < 1:
            raise ValueError("recording_queue_size must be positive")
        _positive_number(self.csv_flush_interval_s, "csv_flush_interval_s")
        if self.csv_flush_row_count < 1:
            raise ValueError("csv_flush_row_count must be positive")
        if self.minimum_preflight_free_mb <= 0 or self.disk_safety_free_mb <= 0:
            raise ValueError("disk thresholds must be positive")
        if self.minimum_preflight_free_mb < self.disk_safety_free_mb:
            raise ValueError("preflight free-space threshold must cover safety threshold")


@dataclass(frozen=True, slots=True)
class PrinterConfig:
    """Independent Marlin printer connection and conservative motion limits."""

    port: str = "COM4"
    baud_rate: int = 115200
    read_timeout_s: float = 0.05
    startup_delay_s: float = 2.0
    command_timeout_s: float = 15.0
    x_min_mm: float = 0.0
    x_max_mm: float = 220.0
    y_min_mm: float = 0.0
    y_max_mm: float = 220.0
    z_min_mm: float = 0.0
    z_max_mm: float = 250.0
    default_xy_feed_mm_min: float = 3000.0
    default_z_feed_mm_min: float = 300.0
    minimum_z_feed_mm_min: float = 1.0
    maximum_z_feed_mm_min: float = 1200.0
    minimum_displacement_mm: float = -20.0
    maximum_displacement_mm: float = 20.0
    default_force_limit_N: float = 20.0
    minimum_force_limit_N: float = 0.01
    maximum_force_limit_N: float = 1000.0
    emergency_poll_interval_s: float = 0.01

    def __post_init__(self) -> None:
        if not self.port.strip():
            raise ValueError("printer port must not be blank")
        if self.baud_rate <= 0:
            raise ValueError("printer baud_rate must be positive")
        for name in (
            "read_timeout_s",
            "command_timeout_s",
            "default_xy_feed_mm_min",
            "default_z_feed_mm_min",
            "minimum_z_feed_mm_min",
            "maximum_z_feed_mm_min",
            "default_force_limit_N",
            "minimum_force_limit_N",
            "maximum_force_limit_N",
            "emergency_poll_interval_s",
        ):
            _positive_number(getattr(self, name), name)
        _nonnegative_number(self.startup_delay_s, "startup_delay_s")
        for low_name, high_name in (
            ("x_min_mm", "x_max_mm"),
            ("y_min_mm", "y_max_mm"),
            ("z_min_mm", "z_max_mm"),
            ("minimum_displacement_mm", "maximum_displacement_mm"),
            ("minimum_z_feed_mm_min", "maximum_z_feed_mm_min"),
            ("minimum_force_limit_N", "maximum_force_limit_N"),
        ):
            low = float(getattr(self, low_name))
            high = float(getattr(self, high_name))
            if not math.isfinite(low) or not math.isfinite(high) or low >= high:
                raise ValueError(f"{low_name} must be less than {high_name}")
        if not self.minimum_z_feed_mm_min <= self.default_z_feed_mm_min <= self.maximum_z_feed_mm_min:
            raise ValueError("default_z_feed_mm_min must be inside configured limits")
        if not self.minimum_force_limit_N <= self.default_force_limit_N <= self.maximum_force_limit_N:
            raise ValueError("default_force_limit_N must be inside configured limits")


@dataclass(frozen=True, slots=True)
class VisualizationConfig:
    """GUI refresh limits and fixed graph scales."""

    preview_refresh_hz: float = 30.0
    heatmap_refresh_hz: float = 10.0
    force_plot_refresh_hz: float = 10.0
    status_refresh_hz: float = 2.0
    temporal_history_s: float = 30.0
    default_graph_metric: str = "mean_delta_v"
    mean_intensity_scale_min: float = 0.0
    mean_intensity_scale_max: float = 255.0

    def __post_init__(self) -> None:
        for name in (
            "preview_refresh_hz",
            "heatmap_refresh_hz",
            "force_plot_refresh_hz",
            "status_refresh_hz",
            "temporal_history_s",
            "mean_intensity_scale_max",
        ):
            _positive_number(getattr(self, name), name)
        if self.heatmap_refresh_hz > 10.0:
            raise ValueError("default heatmap_refresh_hz must not exceed 10")
        if self.mean_intensity_scale_min < 0.0:
            raise ValueError("mean_intensity_scale_min must be nonnegative")
        if self.mean_intensity_scale_max <= self.mean_intensity_scale_min:
            raise ValueError("graph scale maximum must exceed minimum")


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    """Complete versioned application configuration with JSON round-trip."""

    schema_version: str = SCHEMA_VERSION
    application_version: str = APPLICATION_VERSION
    camera: CameraConfig = field(default_factory=CameraConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    loadcell: LoadCellConfig = field(default_factory=LoadCellConfig)
    printer: PrinterConfig = field(default_factory=PrinterConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    visualization: VisualizationConfig = field(default_factory=VisualizationConfig)

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "ApplicationConfig":
        """Construct and validate a configuration mapping."""

        camera_values = dict(values.get("camera", {}))
        if "backend_preference" in camera_values:
            camera_values["backend_preference"] = tuple(
                camera_values["backend_preference"]
            )
        return cls(
            schema_version=str(values.get("schema_version", SCHEMA_VERSION)),
            application_version=str(
                values.get("application_version", APPLICATION_VERSION)
            ),
            camera=CameraConfig(**camera_values),
            processing=ProcessingConfig(**dict(values.get("processing", {}))),
            loadcell=LoadCellConfig(**dict(values.get("loadcell", {}))),
            printer=PrinterConfig(**dict(values.get("printer", {}))),
            recording=RecordingConfig(**dict(values.get("recording", {}))),
            visualization=VisualizationConfig(
                **dict(values.get("visualization", {}))
            ),
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "ApplicationConfig":
        """Load and validate UTF-8 JSON configuration from disk."""

        with Path(path).open("r", encoding="utf-8") as handle:
            values = json.load(handle)
        if not isinstance(values, dict):
            raise ValueError("configuration root must be a JSON object")
        return cls.from_dict(values)

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-ready nested mapping."""

        values = asdict(self)
        values["camera"]["backend_preference"] = list(
            self.camera.backend_preference
        )
        return values

    def save_json(self, path: str | Path) -> None:
        """Save configuration without silently creating malformed JSON."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")


@dataclass(frozen=True, slots=True)
class TrialLabels:
    """Fixed trial-level metadata for one manual press or repeated sequence."""

    session_id: str
    trial_id: str
    sensing_skin_id: str
    target_roi_ground_truth: int
    specimen_or_participant_id: str = ""
    trial_interaction_class: str = ""
    trial_force_class: str = ""
    press_number: int | None = None
    notes: str = ""
    trial_label_scope: str = field(default="trial", init=False)

    def __post_init__(self) -> None:
        for name in ("session_id", "trial_id", "sensing_skin_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if not 1 <= self.target_roi_ground_truth <= ROI_COUNT:
            raise ValueError("target_roi_ground_truth must be an ROI number 1..9")
        if self.press_number is not None and self.press_number < 1:
            raise ValueError("press_number must be positive when provided")

    def as_export_dict(self) -> dict[str, Any]:
        """Return names that explicitly distinguish trial labels from predictions."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class ROI:
    """One rectangular taxel ROI in full-frame upper-left coordinates."""

    roi_id: int
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if not 1 <= self.roi_id <= ROI_COUNT:
            raise ValueError("roi_id must be in 1..9")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("ROI width and height must be positive")

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2.0

    def is_inside(self, frame_width: int, frame_height: int) -> bool:
        """Return whether the complete rectangle lies inside the frame."""

        return (
            frame_width > 0
            and frame_height > 0
            and self.x >= 0
            and self.y >= 0
            and self.x + self.width <= frame_width
            and self.y + self.height <= frame_height
        )

    def overlaps(self, other: "ROI") -> bool:
        """Return true for positive-area overlap; touching edges do not overlap."""

        return not (
            self.x + self.width <= other.x
            or other.x + other.width <= self.x
            or self.y + self.height <= other.y
            or other.y + other.height <= self.y
        )


@dataclass(eq=False, slots=True)
class ROIBaseline:
    """Per-pixel baseline arrays plus summary evidence for one ROI."""

    roi_id: int
    median_v_image: np.ndarray
    mean_v_image: np.ndarray
    circular_mean_h: float
    mean_s: float
    mean_v: float
    median_v: float
    std_v: float
    valid_frame_count: int
    capture_timestamp_iso: str

    def __post_init__(self) -> None:
        if not 1 <= self.roi_id <= ROI_COUNT:
            raise ValueError("roi_id must be in 1..9")
        self.median_v_image = np.asarray(self.median_v_image)
        self.mean_v_image = np.asarray(self.mean_v_image)
        if self.median_v_image.ndim != 2 or self.mean_v_image.ndim != 2:
            raise ValueError("baseline V images must be two-dimensional")
        if self.median_v_image.shape != self.mean_v_image.shape:
            raise ValueError("median and mean baseline images must have equal shapes")
        if self.median_v_image.size == 0:
            raise ValueError("baseline images must not be empty")
        if self.valid_frame_count < 1:
            raise ValueError("valid_frame_count must be positive")


@dataclass(slots=True)
class BaselineRecord:
    """Nine-ROI baseline bound to exact camera, ROI, and processing provenance."""

    baseline_id: str
    roi_layout_id: str
    roi_baselines: tuple[ROIBaseline, ...]
    camera_fingerprint: str = ""
    processing_fingerprint: str = ""
    capture_timestamp_iso: str = ""
    camera_settings: dict[str, object] = field(default_factory=dict)
    valid: bool = True
    invalid_reason: str = ""

    def __post_init__(self) -> None:
        self.roi_baselines = tuple(self.roi_baselines)
        roi_ids = tuple(item.roi_id for item in self.roi_baselines)
        if roi_ids != tuple(range(1, ROI_COUNT + 1)):
            raise ValueError("roi_baselines must contain ROI 1..9 in order")
        if not self.valid and not self.invalid_reason.strip():
            raise ValueError("an invalid baseline must include invalid_reason")

    def invalidate(self, reason: str) -> None:
        """Invalidate this baseline with a human-readable provenance reason."""

        if not reason.strip():
            raise ValueError("baseline invalidation reason must not be blank")
        self.valid = False
        self.invalid_reason = reason.strip()


@dataclass(frozen=True, slots=True)
class CalibrationVerification:
    """Known-mass verification evidence that independently gates readiness."""

    expected_gf: float
    measured_mean_gf: float
    absolute_error_gf: float
    percentage_error: float
    measured_std_gf: float
    tolerance_percent: float
    sample_count: int
    minimum_sample_count: int
    passed: bool
    rejection_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        expected = _positive_number(self.expected_gf, "expected_gf")
        measured = _finite_number(self.measured_mean_gf, "measured_mean_gf")
        absolute_error = _nonnegative_number(
            self.absolute_error_gf, "absolute_error_gf"
        )
        percentage_error = _nonnegative_number(
            self.percentage_error, "percentage_error"
        )
        _nonnegative_number(self.measured_std_gf, "measured_std_gf")
        tolerance = _positive_number(self.tolerance_percent, "tolerance_percent")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count < 0
        ):
            raise ValueError("sample_count must be a nonnegative integer")
        if (
            isinstance(self.minimum_sample_count, bool)
            or not isinstance(self.minimum_sample_count, int)
            or self.minimum_sample_count < 1
        ):
            raise ValueError("minimum_sample_count must be a positive integer")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be a bool")
        if not _numerically_equal(absolute_error, abs(measured - expected)):
            raise ValueError("absolute_error_gf is inconsistent with measured and expected force")
        expected_percentage = absolute_error / expected * 100.0
        if not _numerically_equal(percentage_error, expected_percentage):
            raise ValueError("percentage_error is inconsistent with absolute_error_gf")

        reasons: list[str] = []
        if self.sample_count < self.minimum_sample_count:
            reasons.append("insufficient_verification_samples")
        if percentage_error > tolerance:
            reasons.append("verification_error_above_tolerance")
        supplied_reasons = tuple(str(item).strip() for item in self.rejection_reasons)
        if any(not item for item in supplied_reasons) or len(set(supplied_reasons)) != len(
            supplied_reasons
        ):
            raise ValueError("verification rejection_reasons must be unique and nonblank")
        if supplied_reasons != tuple(reasons):
            raise ValueError("verification rejection_reasons are inconsistent with evidence")
        if bool(self.passed) != (not reasons):
            raise ValueError("verification passed flag is inconsistent with evidence")

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "CalibrationVerification":
        """Restore verification evidence from its portable JSON mapping."""

        payload = dict(values)
        payload["rejection_reasons"] = tuple(payload.get("rejection_reasons", ()))
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class LoadCellCalibration:
    """Saved signed HX711 scale, tare, quality evidence, and provenance."""

    calibration_id: str
    counts_per_gram: float
    tare_raw: float
    known_mass_g: float
    unloaded_mean_raw: float
    unloaded_std_raw: float
    unloaded_sample_count: int
    loaded_mean_raw: float
    loaded_std_raw: float
    loaded_sample_count: int
    calibration_span_counts: float
    calibration_noise_counts: float
    calibration_snr: float
    loaded_window_mean_gf: float
    loaded_window_std_gf: float
    loaded_window_cv_percent: float
    minimum_sample_count: int
    minimum_calibration_snr: float
    maximum_loaded_window_cv_percent: float
    minimum_abs_counts_per_gram: float
    quality_passed: bool
    rejection_reasons: tuple[str, ...] = ()
    serial_port: str = ""
    firmware_identity: str = ""
    protocol_version: str = ""
    firmware_version: str = ""
    calibration_timestamp_iso: str = ""
    tare_timestamp_iso: str = ""
    verification: CalibrationVerification | None = None
    baud_rate: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.counts_per_gram, bool):
            raise TypeError("counts_per_gram must be a number, not bool")
        factor = float(self.counts_per_gram)
        if isinstance(self.baud_rate, bool) or int(self.baud_rate) < 0:
            raise ValueError("baud_rate must be a nonnegative integer")
        if not str(self.calibration_id).strip():
            raise ValueError("calibration_id must not be blank")
        for value, name in (
            (self.serial_port, "serial_port"),
            (self.firmware_identity, "firmware_identity"),
            (self.protocol_version, "protocol_version"),
            (self.firmware_version, "firmware_version"),
        ):
            if not str(value).strip():
                raise ValueError(f"{name} must not be blank")
        _aware_iso_timestamp(
            self.calibration_timestamp_iso, "calibration_timestamp_iso"
        )
        _aware_iso_timestamp(self.tare_timestamp_iso, "tare_timestamp_iso")
        minimum_factor = _positive_number(
            self.minimum_abs_counts_per_gram, "minimum_abs_counts_per_gram"
        )
        if not math.isfinite(factor) or abs(factor) < minimum_factor:
            raise ValueError(
                "counts_per_gram must be finite and meet minimum_abs_counts_per_gram"
            )
        known_mass = _positive_number(self.known_mass_g, "known_mass_g")
        tare = _finite_number(self.tare_raw, "tare_raw")
        unloaded_mean = _finite_number(self.unloaded_mean_raw, "unloaded_mean_raw")
        loaded_mean = _finite_number(self.loaded_mean_raw, "loaded_mean_raw")
        unloaded_std = _nonnegative_number(self.unloaded_std_raw, "unloaded_std_raw")
        loaded_std = _nonnegative_number(self.loaded_std_raw, "loaded_std_raw")
        if (
            isinstance(self.unloaded_sample_count, bool)
            or not isinstance(self.unloaded_sample_count, int)
            or self.unloaded_sample_count < 0
        ):
            raise ValueError("unloaded_sample_count must be a nonnegative integer")
        if (
            isinstance(self.loaded_sample_count, bool)
            or not isinstance(self.loaded_sample_count, int)
            or self.loaded_sample_count < 0
        ):
            raise ValueError("loaded_sample_count must be a nonnegative integer")
        if (
            isinstance(self.minimum_sample_count, bool)
            or not isinstance(self.minimum_sample_count, int)
            or self.minimum_sample_count < 1
        ):
            raise ValueError("minimum_sample_count must be a positive integer")
        if not isinstance(self.quality_passed, bool):
            raise TypeError("quality_passed must be a bool")

        span = _nonnegative_number(
            self.calibration_span_counts, "calibration_span_counts"
        )
        noise = _nonnegative_number(
            self.calibration_noise_counts, "calibration_noise_counts"
        )
        if math.isnan(self.calibration_snr) or self.calibration_snr < 0.0:
            raise ValueError("calibration_snr must be nonnegative or positive infinity")
        if math.isinf(self.calibration_snr) and self.calibration_snr < 0.0:
            raise ValueError("calibration_snr cannot be negative infinity")
        loaded_window_mean = _finite_number(
            self.loaded_window_mean_gf, "loaded_window_mean_gf"
        )
        loaded_window_std = _nonnegative_number(
            self.loaded_window_std_gf, "loaded_window_std_gf"
        )
        loaded_cv = _nonnegative_number(
            self.loaded_window_cv_percent, "loaded_window_cv_percent"
        )
        minimum_snr = _positive_number(
            self.minimum_calibration_snr, "minimum_calibration_snr"
        )
        maximum_cv = _positive_number(
            self.maximum_loaded_window_cv_percent,
            "maximum_loaded_window_cv_percent",
        )
        if not _numerically_equal(span, abs(loaded_mean - unloaded_mean)):
            raise ValueError("calibration_span_counts is inconsistent with raw means")
        if not _numerically_equal(noise, max(unloaded_std, loaded_std)):
            raise ValueError("calibration_noise_counts is inconsistent with window noise")
        if not _numerically_equal(factor, (loaded_mean - unloaded_mean) / known_mass):
            raise ValueError("counts_per_gram is inconsistent with calibration windows")
        expected_loaded_mean = (loaded_mean - unloaded_mean) / factor
        expected_loaded_std = loaded_std / abs(factor)
        expected_loaded_cv = abs(expected_loaded_std / expected_loaded_mean) * 100.0
        if not _numerically_equal(loaded_window_mean, expected_loaded_mean):
            raise ValueError(
                "loaded_window_mean_gf is inconsistent with raw calibration evidence"
            )
        if not _numerically_equal(loaded_window_std, expected_loaded_std):
            raise ValueError(
                "loaded_window_std_gf is inconsistent with raw calibration evidence"
            )
        if not _numerically_equal(loaded_cv, expected_loaded_cv):
            raise ValueError(
                "loaded_window_cv_percent is inconsistent with loaded-window evidence"
            )
        expected_snr = math.inf if noise == 0.0 and span > 0.0 else (
            0.0 if span == 0.0 else span / noise
        )
        if math.isinf(expected_snr):
            if not math.isinf(self.calibration_snr):
                raise ValueError("calibration_snr is inconsistent with zero noise")
        elif not _numerically_equal(self.calibration_snr, expected_snr):
            raise ValueError("calibration_snr is inconsistent with span and noise")
        if not math.isfinite(tare):
            raise ValueError("tare_raw must be finite")

        reasons: list[str] = []
        if self.unloaded_sample_count < self.minimum_sample_count:
            reasons.append("insufficient_unloaded_samples")
        if self.loaded_sample_count < self.minimum_sample_count:
            reasons.append("insufficient_loaded_samples")
        if self.calibration_snr < minimum_snr:
            reasons.append("calibration_snr_below_minimum")
        if loaded_cv > maximum_cv:
            reasons.append("loaded_window_cv_above_maximum")
        supplied_reasons = tuple(str(item).strip() for item in self.rejection_reasons)
        if any(not item for item in supplied_reasons) or len(set(supplied_reasons)) != len(
            supplied_reasons
        ):
            raise ValueError("calibration rejection_reasons must be unique and nonblank")
        if supplied_reasons != tuple(reasons):
            raise ValueError("calibration rejection_reasons are inconsistent with evidence")
        if bool(self.quality_passed) != (not reasons):
            raise ValueError("quality_passed is inconsistent with calibration evidence")
        if self.verification is not None:
            if not _numerically_equal(self.verification.expected_gf, known_mass):
                raise ValueError("verification expected_gf must match known_mass_g")
            if self.verification.passed and not self.quality_passed:
                raise ValueError("verification cannot pass a rejected calibration")

    def force_gf(self, raw_adc: float) -> float:
        """Apply the saved signed scale without an undocumented absolute value."""

        return (float(raw_adc) - self.tare_raw) / self.counts_per_gram

    def force_N(self, raw_adc: float) -> float:
        """Convert raw ADC counts to Newtons using standard gravity."""

        return self.force_gf(raw_adc) * GRAM_FORCE_TO_NEWTON

    def with_tare(self, tare_raw: float, tare_timestamp_iso: str) -> "LoadCellCalibration":
        """Return a retared scale and invalidate prior known-mass verification."""

        return replace(
            self,
            tare_raw=_finite_number(tare_raw, "tare_raw"),
            tare_timestamp_iso=tare_timestamp_iso,
            verification=None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return complete JSON data without nonstandard Infinity tokens.

        Zero-noise windows legitimately produce positive-infinite SNR.  JSON has
        no portable infinity literal, so the numeric field becomes null and an
        explicit status preserves the scientific meaning for round-trip loading.
        """

        values = asdict(self)
        values["calibration_direction"] = (
            "increasing_raw_with_load" if self.counts_per_gram > 0
            else "decreasing_raw_with_load"
        )
        values["rejection_reasons"] = list(self.rejection_reasons)
        if self.verification is not None:
            values["verification"]["rejection_reasons"] = list(
                self.verification.rejection_reasons
            )
        if math.isinf(self.calibration_snr):
            values["calibration_snr"] = None
            values["calibration_snr_status"] = "positive_infinity_zero_noise"
        else:
            values["calibration_snr_status"] = "finite"
        return values

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "LoadCellCalibration":
        """Restore complete calibration and nested verification evidence."""

        payload = dict(values)
        payload.pop("calibration_direction", None)
        snr_status = str(payload.pop("calibration_snr_status", "finite"))
        if payload.get("calibration_snr") is None:
            if snr_status != "positive_infinity_zero_noise":
                raise ValueError("null calibration_snr requires an infinity status")
            payload["calibration_snr"] = math.inf
        elif snr_status != "finite":
            raise ValueError("finite calibration_snr requires status 'finite'")
        elif isinstance(payload["calibration_snr"], bool) or not math.isfinite(
            float(payload["calibration_snr"])
        ):
            raise ValueError(
                "nonfinite calibration_snr requires null plus the zero-noise infinity status"
            )
        else:
            payload["calibration_snr"] = float(payload["calibration_snr"])
        payload["rejection_reasons"] = tuple(payload.get("rejection_reasons", ()))
        verification = payload.get("verification")
        if isinstance(verification, Mapping):
            payload["verification"] = CalibrationVerification.from_dict(verification)
        return cls(**payload)

    def save_json(self, path: str | Path) -> None:
        """Save portable, strict JSON calibration evidence."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                self.to_dict(),
                handle,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            handle.write("\n")

    @classmethod
    def load_json(cls, path: str | Path) -> "LoadCellCalibration":
        """Load and validate a saved calibration JSON file."""

        with Path(path).open("r", encoding="utf-8") as handle:
            values = json.load(handle)
        if not isinstance(values, dict):
            raise ValueError("calibration root must be a JSON object")
        return cls.from_dict(values)


@dataclass(frozen=True, slots=True)
class LoadCellSample:
    """One valid physical HX711 conversion with authoritative host receipt time."""

    host_monotonic_ns: int
    arduino_sample_id: int
    arduino_micros: int
    raw_adc: float
    force_gf: float
    force_N: float
    device_session_id: str
    elapsed_time_s: float = 0.0
    wall_clock_iso: str = ""
    arduino_micros_unwrapped: int | None = None
    loadcell_valid: bool = True
    error_code: ErrorCode = ErrorCode.NONE
    tared_raw: float = math.nan
    mass_g: float = math.nan
    calibration_factor_counts_per_gram: float = math.nan
    zero_offset_raw: float = math.nan
    serial_port: str = ""
    baud_rate: int = 0
    arduino_timestamp_available: bool = True
    sequence_id: str = ""
    sequence_generation: int = 0
    sequence_status: str = "idle"
    motion_phase: str = "idle"
    motion_phase_host_monotonic_ns: int = 0
    motion_cycle_id: str = ""
    motion_cycle_index: int = 0
    motion_total_cycles: int = 0
    command_id: str = ""
    press_zero_x_mm: float = math.nan
    press_zero_y_mm: float = math.nan
    press_zero_z_mm: float = math.nan
    target_displacement_mm: float = math.nan
    target_machine_z_mm: float = math.nan
    commanded_x_mm: float = math.nan
    commanded_y_mm: float = math.nan
    commanded_z_mm: float = math.nan
    requested_feed_rate_mm_min: float = math.nan
    printer_reported_x_mm: float = math.nan
    printer_reported_y_mm: float = math.nan
    printer_reported_z_mm: float = math.nan
    printer_position_valid: bool = False
    press_zero_valid: bool = False
    force_limit_N: float = math.nan
    force_limit_exceeded: bool = False
    sequence_paused: bool = False
    sequence_aborted: bool = False

    def __post_init__(self) -> None:
        if self.host_monotonic_ns < 0 or self.arduino_sample_id < 0:
            raise ValueError("timestamps and sample IDs must be nonnegative")
        if not 0 <= self.arduino_micros <= 0xFFFFFFFF:
            raise ValueError("arduino_micros must be an unsigned 32-bit value")
        if not self.device_session_id.strip():
            raise ValueError("device_session_id must not be blank")
        if isinstance(self.raw_adc, bool) or not math.isfinite(float(self.raw_adc)):
            raise ValueError("raw_adc must be a finite number")
        if isinstance(self.baud_rate, bool) or int(self.baud_rate) < 0:
            raise ValueError("baud_rate must be a nonnegative integer")
        if self.sequence_generation < 0 or self.motion_phase_host_monotonic_ns < 0:
            raise ValueError("motion generations and timestamps must be nonnegative")
        if self.motion_cycle_index < 0 or self.motion_total_cycles < 0:
            raise ValueError("motion cycle counters must be nonnegative")
        for value, name in (
            (self.tared_raw, "tared_raw"),
            (self.mass_g, "mass_g"),
            (
                self.calibration_factor_counts_per_gram,
                "calibration_factor_counts_per_gram",
            ),
            (self.zero_offset_raw, "zero_offset_raw"),
        ):
            numeric = float(value)
            if math.isinf(numeric):
                raise ValueError(f"{name} must be finite or NaN")

    @property
    def sample_id(self) -> int:
        """Compatibility alias for parsers that use the firmware field name."""

        return self.arduino_sample_id

    @property
    def valid(self) -> bool:
        """Compatibility alias used by structural synchronization code."""

        return self.loadcell_valid


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One accepted frame identity and its schema-level feature payload.

    Processing modules own feature calculations.  This record merely binds their
    exported mappings to the permanent capture identity and authoritative clocks.
    """

    capture_frame_id: int
    host_monotonic_ns: int
    elapsed_time_s: float
    wall_clock_iso: str
    requested_fps: float
    actual_fps: float
    width: int
    height: int
    camera_backend: str
    camera_device_index: int
    applied_exposure: float = math.nan
    applied_gain: float = math.nan
    applied_white_balance: float = math.nan
    roi_features: tuple[Mapping[str, Any], ...] = ()
    localization: Mapping[str, Any] = field(default_factory=dict)
    validity: ValidityFlags = field(default_factory=ValidityFlags)
    error_code: ErrorCode = ErrorCode.NONE

    def __post_init__(self) -> None:
        if self.capture_frame_id < 0 or self.host_monotonic_ns < 0:
            raise ValueError("frame identity and timestamp must be nonnegative")
        if self.elapsed_time_s < 0.0:
            raise ValueError("elapsed_time_s must be nonnegative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("frame dimensions must be positive")
        if self.roi_features and len(self.roi_features) != ROI_COUNT:
            raise ValueError("roi_features must be empty or contain exactly nine rows")


__all__ = [
    "APPLICATION_VERSION",
    "ApplicationConfig",
    "BaselineRecord",
    "CalibrationVerification",
    "CameraBackend",
    "CameraConfig",
    "ErrorCode",
    "FrameRecord",
    "GRAM_FORCE_TO_NEWTON",
    "LoadCellCalibration",
    "LoadCellConfig",
    "LoadCellSample",
    "ProcessingConfig",
    "PrinterConfig",
    "ROI",
    "ROIBaseline",
    "ROI_COUNT",
    "ReadinessFlags",
    "RecordingConfig",
    "RecordingLifecycle",
    "SCHEMA_VERSION",
    "SynchronizationMethod",
    "TrialLabels",
    "ValidityFlag",
    "ValidityFlags",
    "VisualizationConfig",
]
