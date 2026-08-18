"""Single-owner OpenCV service for the Arducam IMX179 USB camera.

The service contains no Qt code and performs no preview annotation.  It tries
Windows DirectShow before Media Foundation, retains at most one live
``VideoCapture`` object, records requested-versus-actual mode information, and
returns immutable original BGR frames with authoritative host timestamps.

OpenCV generally cannot provide a reliable USB camera product name on Windows;
the operator therefore identifies the Arducam IMX179 by device-index preview.
The saved device index and backend remain explicit in :class:`CameraConnectionInfo`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Protocol

import cv2
import numpy as np

from core.models import CameraBackend, CameraConfig
from services.interfaces import (
    CameraConnectionInfo,
    CameraPropertyCapability,
    CameraPropertyReadback,
    CapturedFrame,
)


PRIMARY_CAMERA_MODEL = "Arducam IMX179 Camera Module"
CAMERA_SETTINGS_SCHEMA_VERSION = "1.0"
_NS_PER_SECOND = 1_000_000_000


class CameraServiceError(RuntimeError):
    """Base error for physical camera ownership and acquisition failures."""


class CameraConnectionError(CameraServiceError):
    """Raised when no requested OpenCV backend can open the device."""


class CameraNotConnectedError(CameraServiceError):
    """Raised when an operation requires the sole camera owner."""


class _Capture(Protocol):
    def isOpened(self) -> bool: ...

    def set(self, property_id: int, value: float) -> bool: ...

    def get(self, property_id: int) -> float: ...

    def read(self) -> tuple[bool, np.ndarray | None]: ...

    def release(self) -> None: ...


CaptureFactory = Callable[[int, int], _Capture]


@dataclass(frozen=True, slots=True)
class CameraBackendAttempt:
    """Diagnostic evidence for one attempted device-index/backend open."""

    device_index: int
    backend: str
    opened: bool
    message: str


@dataclass(frozen=True, slots=True)
class CameraDeviceInfo:
    """One device index that opened during an ephemeral refresh probe."""

    device_index: int
    backend: str
    actual_width: int
    actual_height: int
    actual_fps: float
    display_name: str


@dataclass(frozen=True, slots=True)
class _ControlSpec:
    property_id: int | None
    boolean: bool = False
    directshow_auto_exposure: bool = False


def _cv_property(name: str) -> int | None:
    value = getattr(cv2, name, None)
    return int(value) if value is not None else None


_CONTROL_SPECS: dict[str, _ControlSpec] = {
    "exposure": _ControlSpec(_cv_property("CAP_PROP_EXPOSURE")),
    "auto_exposure": _ControlSpec(
        _cv_property("CAP_PROP_AUTO_EXPOSURE"),
        boolean=True,
        directshow_auto_exposure=True,
    ),
    "gain": _ControlSpec(_cv_property("CAP_PROP_GAIN")),
    "brightness": _ControlSpec(_cv_property("CAP_PROP_BRIGHTNESS")),
    "contrast": _ControlSpec(_cv_property("CAP_PROP_CONTRAST")),
    "saturation": _ControlSpec(_cv_property("CAP_PROP_SATURATION")),
    "sharpness": _ControlSpec(_cv_property("CAP_PROP_SHARPNESS")),
    "gamma": _ControlSpec(_cv_property("CAP_PROP_GAMMA")),
    "white_balance": _ControlSpec(_cv_property("CAP_PROP_WB_TEMPERATURE")),
    "auto_white_balance": _ControlSpec(
        _cv_property("CAP_PROP_AUTO_WB"), boolean=True
    ),
    "focus": _ControlSpec(_cv_property("CAP_PROP_FOCUS")),
    "auto_focus": _ControlSpec(_cv_property("CAP_PROP_AUTOFOCUS"), boolean=True),
    "hue": _ControlSpec(_cv_property("CAP_PROP_HUE")),
    "backlight_compensation": _ControlSpec(_cv_property("CAP_PROP_BACKLIGHT")),
    "zoom": _ControlSpec(_cv_property("CAP_PROP_ZOOM")),
    "pan": _ControlSpec(_cv_property("CAP_PROP_PAN")),
    "tilt": _ControlSpec(_cv_property("CAP_PROP_TILT")),
    "roll": _ControlSpec(_cv_property("CAP_PROP_ROLL")),
    "iris": _ControlSpec(_cv_property("CAP_PROP_IRIS")),
    "temperature": _ControlSpec(_cv_property("CAP_PROP_TEMPERATURE")),
}

_NATIVE_ONLY_CONTROLS = (
    "power_line_frequency_anti_flicker",
    "low_light_compensation",
    "hdr",
    "exposure_compensation",
    "white_balance_preset",
    "focus_distance",
    "horizontal_mirroring",
    "vertical_mirroring",
)

SUPPORTED_CAMERA_PROPERTIES = tuple(_CONTROL_SPECS)

_CONTROL_ALIASES = {
    "automatic_exposure": "auto_exposure",
    "autoexposure": "auto_exposure",
    "white_balance_temperature": "white_balance",
    "white_balance_temperature_k": "white_balance",
    "automatic_white_balance": "auto_white_balance",
    "auto_wb": "auto_white_balance",
    "automatic_focus": "auto_focus",
    "autofocus": "auto_focus",
}

_BACKEND_CODES = {
    CameraBackend.DIRECTSHOW.value: int(cv2.CAP_DSHOW),
    CameraBackend.MEDIA_FOUNDATION.value: int(cv2.CAP_MSMF),
}

_BACKEND_ALIASES = {
    "directshow": CameraBackend.DIRECTSHOW.value,
    "dshow": CameraBackend.DIRECTSHOW.value,
    "cap_dshow": CameraBackend.DIRECTSHOW.value,
    "media foundation": CameraBackend.MEDIA_FOUNDATION.value,
    "media_foundation": CameraBackend.MEDIA_FOUNDATION.value,
    "msmf": CameraBackend.MEDIA_FOUNDATION.value,
    "cap_msmf": CameraBackend.MEDIA_FOUNDATION.value,
}


def _normalize_control_name(name: str) -> str:
    normalized = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    return _CONTROL_ALIASES.get(normalized, normalized)


def _normalize_backend_name(name: str) -> str:
    raw = str(name).strip()
    if raw in _BACKEND_CODES:
        return raw
    normalized = raw.lower().replace("-", " ")
    try:
        return _BACKEND_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"unsupported camera backend {name!r}; use DirectShow or Media Foundation"
        ) from exc


def _backend_order(preferences: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    requested = {_normalize_backend_name(name) for name in preferences}
    canonical = (
        CameraBackend.DIRECTSHOW.value,
        CameraBackend.MEDIA_FOUNDATION.value,
    )
    result = tuple(name for name in canonical if name in requested)
    if not result:
        raise ValueError("at least one supported camera backend is required")
    return result


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric, not bool")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _normalize_setting_value(
    name: str, value: float | bool
) -> float | bool:
    spec = _CONTROL_SPECS[name]
    if spec.boolean:
        if isinstance(value, bool):
            return value
        numeric = _finite_number(value, name)
        if numeric not in (0.0, 1.0):
            raise ValueError(f"{name} must be true/false or 0/1")
        return bool(numeric)
    return _finite_number(value, name)


def _canonical_settings(
    settings: Mapping[str, float | bool], *, reject_unknown: bool
) -> dict[str, float | bool]:
    normalized: dict[str, float | bool] = {}
    for raw_name, raw_value in settings.items():
        name = _normalize_control_name(raw_name)
        if name not in _CONTROL_SPECS:
            if reject_unknown:
                raise ValueError(f"unsupported camera setting: {raw_name}")
            continue
        if name in normalized:
            raise ValueError(f"duplicate camera setting after normalization: {name}")
        normalized[name] = _normalize_setting_value(name, raw_value)
    return {
        name: normalized[name]
        for name in SUPPORTED_CAMERA_PROPERTIES
        if name in normalized
    }


def save_camera_settings(
    path: str | Path,
    settings: Mapping[str, float | bool],
    *,
    metadata: Mapping[str, object] | None = None,
) -> Path:
    """Atomically save validated manual controls without applying them."""

    canonical = _canonical_settings(settings, reject_unknown=True)
    payload: dict[str, object] = {
        "schema_version": CAMERA_SETTINGS_SCHEMA_VERSION,
        "camera_model": PRIMARY_CAMERA_MODEL,
        "settings": canonical,
    }
    if metadata is not None:
        payload["metadata"] = dict(metadata)
    try:
        serialized = json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("camera settings metadata must be strict JSON data") from exc
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def load_camera_settings(path: str | Path) -> dict[str, float | bool]:
    """Load and validate manual controls; loading alone never changes hardware."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not load camera settings {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("camera settings JSON root must be an object")
    if payload.get("schema_version") != CAMERA_SETTINGS_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported camera settings schema: {payload.get('schema_version')!r}"
        )
    raw_settings = payload.get("settings")
    if not isinstance(raw_settings, dict):
        raise ValueError("camera settings JSON must contain a settings object")
    return _canonical_settings(raw_settings, reject_unknown=True)


def _safe_opened(capture: _Capture) -> bool:
    try:
        return bool(capture.isOpened())
    except Exception:
        return False


def _safe_release(capture: _Capture | None) -> None:
    if capture is None:
        return
    try:
        capture.release()
    except Exception:
        # Ownership is still cleared even if a faulty driver raises on release.
        return


def _safe_mode_value(capture: _Capture, property_id: int) -> float | None:
    try:
        value = float(capture.get(property_id))
    except Exception:
        return None
    return value if math.isfinite(value) else None


def _dimension_or_zero(value: float | bool | None) -> int:
    if isinstance(value, bool) or value is None or not math.isfinite(float(value)):
        return 0
    rounded = int(round(float(value)))
    return rounded if rounded > 0 else 0


def _fps_or_nan(value: float | bool | None) -> float:
    if isinstance(value, bool) or value is None:
        return math.nan
    result = float(value)
    return result if math.isfinite(result) and result > 0.0 else math.nan


class OpenCVCameraService:
    """Own exactly one physical OpenCV capture for its complete connection."""

    def __init__(
        self,
        *,
        capture_factory: CaptureFactory = cv2.VideoCapture,
        monotonic_clock: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._capture_factory = capture_factory
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._capture: _Capture | None = None
        self._config: CameraConfig | None = None
        self._connection_info: CameraConnectionInfo | None = None
        self._selected_backend: str | None = None
        self._source_frame_id = 0
        self._connected_monotonic_ns = 0
        self._warmup_started_monotonic_ns = 0
        self._last_frame_monotonic_ns = -1
        self._mode_readbacks: dict[str, CameraPropertyReadback] = {}
        self._property_readbacks: dict[str, CameraPropertyReadback] = {}
        self._confirmed_settings: dict[str, float | bool] = {}
        self._backend_attempts: tuple[CameraBackendAttempt, ...] = ()
        self._probe_attempts: tuple[CameraBackendAttempt, ...] = ()
        self._last_error = ""

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._capture is not None and _safe_opened(self._capture)

    @property
    def simulation_mode(self) -> bool:
        return False

    @property
    def connection_info(self) -> CameraConnectionInfo:
        with self._lock:
            if self._connection_info is None or not self.is_connected:
                raise CameraNotConnectedError("physical camera is not connected")
            return self._connection_info

    @property
    def mode_readbacks(self) -> tuple[CameraPropertyReadback, ...]:
        with self._lock:
            return tuple(self._mode_readbacks.values())

    @property
    def property_readbacks(self) -> tuple[CameraPropertyReadback, ...]:
        with self._lock:
            return tuple(self._property_readbacks.values())

    @property
    def backend_attempts(self) -> tuple[CameraBackendAttempt, ...]:
        return self._backend_attempts

    @property
    def probe_attempts(self) -> tuple[CameraBackendAttempt, ...]:
        return self._probe_attempts

    @property
    def mode_confirmed(self) -> bool:
        with self._lock:
            return len(self._mode_readbacks) == 3 and all(
                result.confirmed for result in self._mode_readbacks.values()
            )

    @property
    def last_error(self) -> str:
        return self._last_error

    def _clock_now_ns(self) -> int:
        value = int(self._monotonic_clock())
        if value < 0:
            raise ValueError("monotonic clock returned a negative timestamp")
        return value

    def _wall_clock_iso(self) -> str:
        value = self._wall_clock()
        if not isinstance(value, datetime):
            raise TypeError("wall clock must return a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("wall clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def _release_owner(self) -> None:
        capture = self._capture
        self._capture = None
        _safe_release(capture)
        self._config = None
        self._connection_info = None
        self._selected_backend = None
        self._source_frame_id = 0
        self._connected_monotonic_ns = 0
        self._warmup_started_monotonic_ns = 0
        self._last_frame_monotonic_ns = -1

    def disconnect(self) -> None:
        """Release the sole capture idempotently."""

        with self._lock:
            self._release_owner()

    def _new_capture(self, device_index: int, backend: str) -> _Capture:
        return self._capture_factory(device_index, _BACKEND_CODES[backend])

    def refresh_devices(
        self,
        *,
        max_devices: int = 10,
        backend_preference: tuple[str, ...] = (
            CameraBackend.DIRECTSHOW.value,
            CameraBackend.MEDIA_FOUNDATION.value,
        ),
    ) -> tuple[CameraDeviceInfo, ...]:
        """Probe indices sequentially and release every ephemeral capture.

        Refresh is rejected while connected so probing never opens a second
        stream alongside the service-owned camera.
        """

        if isinstance(max_devices, bool) or int(max_devices) < 1:
            raise ValueError("max_devices must be a positive integer")
        order = _backend_order(backend_preference)
        with self._lock:
            if self.is_connected:
                raise CameraServiceError("disconnect before refreshing camera devices")
            devices: list[CameraDeviceInfo] = []
            attempts: list[CameraBackendAttempt] = []
            for device_index in range(int(max_devices)):
                for backend in order:
                    capture: _Capture | None = None
                    try:
                        capture = self._new_capture(device_index, backend)
                        opened = _safe_opened(capture)
                        if opened:
                            width = _dimension_or_zero(
                                _safe_mode_value(capture, cv2.CAP_PROP_FRAME_WIDTH)
                            )
                            height = _dimension_or_zero(
                                _safe_mode_value(capture, cv2.CAP_PROP_FRAME_HEIGHT)
                            )
                            fps = _fps_or_nan(
                                _safe_mode_value(capture, cv2.CAP_PROP_FPS)
                            )
                            devices.append(
                                CameraDeviceInfo(
                                    device_index=device_index,
                                    backend=backend,
                                    actual_width=width,
                                    actual_height=height,
                                    actual_fps=fps,
                                    display_name=(
                                        f"Camera index {device_index} ({backend}); "
                                        f"identify {PRIMARY_CAMERA_MODEL} by preview"
                                    ),
                                )
                            )
                            attempts.append(
                                CameraBackendAttempt(
                                    device_index=device_index,
                                    backend=backend,
                                    opened=True,
                                    message="Opened for probe and released immediately.",
                                )
                            )
                            break
                        attempts.append(
                            CameraBackendAttempt(
                                device_index=device_index,
                                backend=backend,
                                opened=False,
                                message="Backend did not open this device index.",
                            )
                        )
                    except Exception as exc:
                        attempts.append(
                            CameraBackendAttempt(
                                device_index=device_index,
                                backend=backend,
                                opened=False,
                                message=f"Open attempt raised {type(exc).__name__}: {exc}",
                            )
                        )
                    finally:
                        _safe_release(capture)
            self._probe_attempts = tuple(attempts)
            return tuple(devices)

    def _mode_property(
        self, name: str, property_id: int, requested: float
    ) -> CameraPropertyReadback:
        if self._capture is None or self._config is None:
            raise CameraNotConnectedError("physical camera is not connected")
        try:
            write_succeeded = bool(self._capture.set(property_id, float(requested)))
            write_message = ""
        except Exception as exc:
            write_succeeded = False
            write_message = f"write raised {type(exc).__name__}: {exc}"
        actual = _safe_mode_value(self._capture, property_id)
        read_succeeded = actual is not None
        tolerance = self._config.property_readback_tolerance
        confirmed = bool(
            write_succeeded
            and read_succeeded
            and math.isclose(
                float(actual), float(requested), rel_tol=tolerance, abs_tol=tolerance
            )
        )
        if confirmed:
            message = "Requested mode value confirmed by OpenCV readback."
        elif write_message:
            message = write_message
        elif not write_succeeded:
            message = "Driver rejected the requested mode value."
        elif not read_succeeded:
            message = "Driver did not provide a finite mode readback."
        else:
            message = "Requested and actual mode values differ beyond tolerance."
        return CameraPropertyReadback(
            property_name=name,
            requested_value=float(requested),
            actual_value=actual,
            write_succeeded=write_succeeded,
            read_succeeded=read_succeeded,
            confirmed=confirmed,
            supported=write_succeeded and read_succeeded,
            message=message,
        )

    def _native_mode_property(
        self, name: str, property_id: int
    ) -> CameraPropertyReadback:
        """Read one driver-selected stream value without issuing a write."""

        if self._capture is None:
            raise CameraNotConnectedError("physical camera is not connected")
        actual = _safe_mode_value(self._capture, property_id)
        read_succeeded = actual is not None
        return CameraPropertyReadback(
            property_name=name,
            requested_value=None,
            actual_value=actual,
            write_succeeded=False,
            read_succeeded=read_succeeded,
            confirmed=read_succeeded,
            supported=read_succeeded,
            message=(
                "Driver-selected stream value read without a GUI mode write."
                if read_succeeded
                else "Driver did not provide a finite stream value."
            ),
        )

    def connect(
        self,
        config: CameraConfig,
        *,
        source_path: str | None = None,
        apply_mode: bool = True,
    ) -> CameraConnectionInfo:
        """Open the selected index, optionally leaving its stream mode untouched."""

        if source_path is not None and str(source_path).strip():
            raise ValueError("physical camera service does not accept source_path")
        order = _backend_order(config.backend_preference)
        with self._lock:
            self._release_owner()
            attempts: list[CameraBackendAttempt] = []
            selected_capture: _Capture | None = None
            selected_backend: str | None = None
            for backend in order:
                candidate: _Capture | None = None
                try:
                    candidate = self._new_capture(config.device_index, backend)
                    if _safe_opened(candidate):
                        selected_capture = candidate
                        selected_backend = backend
                        attempts.append(
                            CameraBackendAttempt(
                                device_index=config.device_index,
                                backend=backend,
                                opened=True,
                                message="Backend opened and became the sole camera owner.",
                            )
                        )
                        break
                    attempts.append(
                        CameraBackendAttempt(
                            device_index=config.device_index,
                            backend=backend,
                            opened=False,
                            message="Backend did not open the device index.",
                        )
                    )
                except Exception as exc:
                    attempts.append(
                        CameraBackendAttempt(
                            device_index=config.device_index,
                            backend=backend,
                            opened=False,
                            message=f"Open attempt raised {type(exc).__name__}: {exc}",
                        )
                    )
                if candidate is not selected_capture:
                    _safe_release(candidate)
            self._backend_attempts = tuple(attempts)
            if selected_capture is None or selected_backend is None:
                self._last_error = (
                    f"Camera index {config.device_index} could not be opened with "
                    + ", ".join(order)
                )
                raise CameraConnectionError(self._last_error)

            self._capture = selected_capture
            self._selected_backend = selected_backend
            self._config = config
            self._source_frame_id = 0
            self._last_frame_monotonic_ns = -1
            self._property_readbacks.clear()
            self._confirmed_settings.clear()
            try:
                if apply_mode:
                    self._mode_readbacks = {
                        "width": self._mode_property(
                            "width", cv2.CAP_PROP_FRAME_WIDTH, config.requested_width
                        ),
                        "height": self._mode_property(
                            "height", cv2.CAP_PROP_FRAME_HEIGHT, config.requested_height
                        ),
                        "fps": self._mode_property(
                            "fps", cv2.CAP_PROP_FPS, config.requested_fps
                        ),
                    }
                else:
                    self._mode_readbacks = {
                        "width": self._native_mode_property(
                            "width", cv2.CAP_PROP_FRAME_WIDTH
                        ),
                        "height": self._native_mode_property(
                            "height", cv2.CAP_PROP_FRAME_HEIGHT
                        ),
                        "fps": self._native_mode_property("fps", cv2.CAP_PROP_FPS),
                    }
                actual_width = _dimension_or_zero(
                    self._mode_readbacks["width"].actual_value
                )
                actual_height = _dimension_or_zero(
                    self._mode_readbacks["height"].actual_value
                )
                actual_fps = _fps_or_nan(self._mode_readbacks["fps"].actual_value)
                connected_ns = self._clock_now_ns()
                self._connected_monotonic_ns = connected_ns
                self._warmup_started_monotonic_ns = connected_ns
                self._connection_info = CameraConnectionInfo(
                    device_index=config.device_index,
                    backend=selected_backend,
                    requested_width=config.requested_width,
                    requested_height=config.requested_height,
                    requested_fps=config.requested_fps,
                    actual_width=actual_width,
                    actual_height=actual_height,
                    actual_fps=actual_fps,
                    simulation_mode=False,
                    source_label=(
                        f"{PRIMARY_CAMERA_MODEL} candidate at camera index "
                        f"{config.device_index}; operator confirmation required"
                    ),
                )
                self._last_error = ""
                return self._connection_info
            except Exception:
                self._release_owner()
                raise

    def _encode_control_value(
        self, name: str, value: float | bool
    ) -> float:
        spec = _CONTROL_SPECS[name]
        if not spec.boolean:
            return float(value)
        enabled = bool(value)
        if (
            spec.directshow_auto_exposure
            and self._selected_backend == CameraBackend.DIRECTSHOW.value
        ):
            return 0.75 if enabled else 0.25
        return 1.0 if enabled else 0.0

    def _decode_control_value(self, name: str, raw_value: float) -> float | bool:
        spec = _CONTROL_SPECS[name]
        if not spec.boolean:
            return raw_value
        if (
            spec.directshow_auto_exposure
            and self._selected_backend == CameraBackend.DIRECTSHOW.value
        ):
            return bool(raw_value >= 0.5)
        return bool(raw_value >= 0.5)

    def _apply_one_control(
        self, name: str, requested_value: float | bool
    ) -> CameraPropertyReadback:
        if self._capture is None or self._config is None:
            raise CameraNotConnectedError("physical camera is not connected")
        spec = _CONTROL_SPECS[name]
        if spec.property_id is None:
            return CameraPropertyReadback(
                property_name=name,
                requested_value=requested_value,
                actual_value=None,
                write_succeeded=False,
                read_succeeded=False,
                confirmed=False,
                supported=False,
                message="This OpenCV build does not expose the property constant.",
            )
        encoded = self._encode_control_value(name, requested_value)
        try:
            write_succeeded = bool(self._capture.set(spec.property_id, encoded))
            write_error = ""
        except Exception as exc:
            write_succeeded = False
            write_error = f"write raised {type(exc).__name__}: {exc}"
        try:
            raw_actual = float(self._capture.get(spec.property_id))
            read_succeeded = math.isfinite(raw_actual)
        except Exception as exc:
            raw_actual = math.nan
            read_succeeded = False
            read_error = f"readback raised {type(exc).__name__}: {exc}"
        else:
            read_error = ""
        actual_value: float | bool | None = (
            self._decode_control_value(name, raw_actual) if read_succeeded else None
        )
        if spec.boolean:
            confirmed = bool(
                write_succeeded
                and read_succeeded
                and actual_value is bool(requested_value)
            )
        else:
            tolerance = self._config.property_readback_tolerance
            confirmed = bool(
                write_succeeded
                and read_succeeded
                and math.isclose(
                    float(actual_value),
                    float(requested_value),
                    rel_tol=tolerance,
                    abs_tol=tolerance,
                )
            )
        supported = write_succeeded and read_succeeded
        if confirmed:
            message = "Requested property value confirmed by driver readback."
        elif write_error:
            message = write_error
        elif read_error:
            message = read_error
        elif not write_succeeded:
            message = "Driver rejected the property write; property is unsupported."
        elif not read_succeeded:
            message = "Driver returned no finite property readback."
        else:
            message = "Driver readback differs from the requested value."
        return CameraPropertyReadback(
            property_name=name,
            requested_value=requested_value,
            actual_value=actual_value,
            write_succeeded=write_succeeded,
            read_succeeded=read_succeeded,
            confirmed=confirmed,
            supported=supported,
            message=message,
        )

    def apply_settings(
        self, requested: Mapping[str, float | bool]
    ) -> tuple[CameraPropertyReadback, ...]:
        """Attempt every requested control and retain explicit readback evidence."""

        with self._lock:
            if not self.is_connected:
                raise CameraNotConnectedError("physical camera is not connected")
            results: list[CameraPropertyReadback] = []
            any_write_succeeded = False
            seen: set[str] = set()
            for raw_name, raw_value in requested.items():
                name = _normalize_control_name(raw_name)
                if name in seen:
                    raise ValueError(
                        f"duplicate camera property after normalization: {name}"
                    )
                seen.add(name)
                if name not in _CONTROL_SPECS:
                    result = CameraPropertyReadback(
                        property_name=str(raw_name),
                        requested_value=(
                            raw_value
                            if isinstance(raw_value, bool)
                            else _finite_number(raw_value, str(raw_name))
                        ),
                        actual_value=None,
                        write_succeeded=False,
                        read_succeeded=False,
                        confirmed=False,
                        supported=False,
                        message="Unknown camera property; no driver call was made.",
                    )
                else:
                    normalized_value = _normalize_setting_value(name, raw_value)
                    result = self._apply_one_control(name, normalized_value)
                    self._property_readbacks[name] = result
                    if result.confirmed:
                        self._confirmed_settings[name] = normalized_value
                    else:
                        self._confirmed_settings.pop(name, None)
                    any_write_succeeded |= result.write_succeeded
                results.append(result)
            if any_write_succeeded:
                # A write can change the image even when readback is unavailable;
                # restart warm-up conservatively in either case.
                self._warmup_started_monotonic_ns = self._clock_now_ns()
            return tuple(results)

    def detect_capabilities(self) -> tuple[CameraPropertyCapability, ...]:
        """Return conservative runtime evidence without inventing driver ranges.

        OpenCV exposes current-value reads but not IAMVideoProcAmp/IAMCameraControl
        min/max/step/default metadata.  Those unavailable fields remain ``None``
        and the native driver dialog is offered for full manufacturer controls.
        """

        with self._lock:
            if not self.is_connected or self._capture is None:
                raise CameraNotConnectedError("physical camera is not connected")
            capabilities: list[CameraPropertyCapability] = []
            for name, spec in _CONTROL_SPECS.items():
                if spec.property_id is None:
                    capabilities.append(
                        CameraPropertyCapability(
                            property_name=name,
                            supported=False,
                            readable=False,
                            writable=False,
                            current_value=None,
                            status="OpenCV does not expose this property constant.",
                        )
                    )
                    continue
                try:
                    raw = float(self._capture.get(spec.property_id))
                    readable = math.isfinite(raw)
                except Exception:
                    raw = math.nan
                    readable = False
                current = self._decode_control_value(name, raw) if readable else None
                capabilities.append(
                    CameraPropertyCapability(
                        property_name=name,
                        supported=readable,
                        readable=readable,
                        writable=None if readable else False,
                        current_value=current,
                        automatic_supported=(True if spec.boolean and readable else None),
                        status=(
                            "Current value read. Driver range/default metadata are unavailable "
                            "through OpenCV; use Native Camera Properties for manufacturer controls."
                            if readable
                            else "Driver supplied no finite readback; control is disabled."
                        ),
                    )
                )
            width = _safe_mode_value(self._capture, cv2.CAP_PROP_FRAME_WIDTH)
            height = _safe_mode_value(self._capture, cv2.CAP_PROP_FRAME_HEIGHT)
            fps = _safe_mode_value(self._capture, cv2.CAP_PROP_FPS)
            mode_values = (
                ("capture_width", width, "Current stream width; changing it requires a safe reconnect."),
                ("capture_height", height, "Current stream height; changing it requires a safe reconnect."),
                ("frame_rate", fps, "Current driver FPS readback; changing it requires a safe reconnect."),
                (
                    "aspect_ratio",
                    (
                        None
                        if width is None or height is None or height <= 0
                        else width / height
                    ),
                    "Derived from the current driver width and height.",
                ),
            )
            for name, current, status in mode_values:
                readable = current is not None and math.isfinite(float(current)) and float(current) > 0
                capabilities.append(
                    CameraPropertyCapability(
                        property_name=name,
                        supported=readable,
                        readable=readable,
                        writable=(name != "aspect_ratio") if readable else False,
                        current_value=(float(current) if readable else None),
                        requires_stream_restart=name != "aspect_ratio",
                        status=status if readable else "Driver supplied no usable mode readback.",
                    )
                )
            format_specs = (
                ("pixel_format_fourcc", _cv_property("CAP_PROP_FOURCC")),
                ("codec_pixel_format", _cv_property("CAP_PROP_CODEC_PIXEL_FORMAT")),
                ("rotation_degrees", _cv_property("CAP_PROP_ORIENTATION_META")),
            )
            for name, property_id in format_specs:
                raw = (
                    None
                    if property_id is None
                    else _safe_mode_value(self._capture, property_id)
                )
                # OpenCV commonly returns zero for an unsupported property.  Be
                # conservative instead of presenting that sentinel as support.
                readable = raw is not None and math.isfinite(raw) and raw != 0.0
                status = (
                    "Read-only backend metadata. Change format/rotation in Native Camera Properties."
                    if readable
                    else "Unavailable or ambiguous through OpenCV; inspect Native Camera Properties."
                )
                if readable and name == "pixel_format_fourcc":
                    code = int(raw)
                    decoded = "".join(chr((code >> (8 * index)) & 0xFF) for index in range(4))
                    status = f"Driver FOURCC is {decoded!r}. " + status
                capabilities.append(
                    CameraPropertyCapability(
                        property_name=name,
                        supported=readable,
                        readable=readable,
                        writable=False,
                        current_value=(float(raw) if readable else None),
                        requires_stream_restart=True,
                        status=status,
                    )
                )
            capabilities.extend(
                CameraPropertyCapability(
                    property_name=name,
                    supported=False,
                    readable=False,
                    writable=False,
                    current_value=None,
                    status=(
                        "No standardized OpenCV control is available. Use the "
                        "DirectShow Native Camera Properties dialog when supported."
                    ),
                )
                for name in _NATIVE_ONLY_CONTROLS
            )
            return tuple(capabilities)

    def open_native_properties(self) -> bool:
        """Open the Windows DirectShow property page when the backend supports it."""

        with self._lock:
            if not self.is_connected or self._capture is None:
                raise CameraNotConnectedError("physical camera is not connected")
            if self._selected_backend != CameraBackend.DIRECTSHOW.value:
                raise CameraServiceError(
                    "Native camera properties require the DirectShow backend on Windows."
                )
            property_id = _cv_property("CAP_PROP_SETTINGS")
            if property_id is None:
                raise CameraServiceError(
                    "This OpenCV build does not expose the native camera-properties dialog."
                )
            try:
                opened = bool(self._capture.set(property_id, 1.0))
            except Exception as exc:
                raise CameraServiceError(
                    f"Native camera-properties dialog failed: {exc}"
                ) from exc
            if not opened:
                raise CameraServiceError(
                    "The selected camera driver rejected the native properties dialog request."
                )
            self._warmup_started_monotonic_ns = self._clock_now_ns()
            return True

    def read_frame(self) -> CapturedFrame | None:
        """Read one unannotated frame; a failed read releases the failed owner."""

        with self._lock:
            if not self.is_connected or self._capture is None:
                if self._capture is not None:
                    self._release_owner()
                raise CameraNotConnectedError("physical camera is not connected")
            try:
                ok, decoded = self._capture.read()
            except Exception as exc:
                self._last_error = f"camera read raised {type(exc).__name__}: {exc}"
                self._release_owner()
                return None
            if not ok or decoded is None:
                self._last_error = "camera read failed; the capture was released"
                self._release_owner()
                return None
            if (
                not isinstance(decoded, np.ndarray)
                or decoded.dtype != np.uint8
                or decoded.ndim != 3
                or decoded.shape[2] != 3
            ):
                self._last_error = "camera returned a non-uint8 or non-BGR frame"
                self._release_owner()
                return None
            frame = np.ascontiguousarray(decoded)
            frame.setflags(write=False)
            host_monotonic_ns = self._clock_now_ns()
            if host_monotonic_ns <= self._last_frame_monotonic_ns:
                host_monotonic_ns = self._last_frame_monotonic_ns + 1
            self._last_frame_monotonic_ns = host_monotonic_ns
            wall_clock_iso = self._wall_clock_iso()
            source_frame_id = self._source_frame_id
            self._source_frame_id += 1
            return CapturedFrame(
                original_bgr=frame,
                source_frame_id=source_frame_id,
                host_monotonic_ns=host_monotonic_ns,
                wall_clock_iso=wall_clock_iso,
            )

    def warmup_complete(self, host_monotonic_ns: int) -> bool:
        with self._lock:
            if not self.is_connected or self._config is None:
                return False
            warmup_ns = round(self._config.warmup_seconds * _NS_PER_SECOND)
            return int(host_monotonic_ns) >= (
                self._warmup_started_monotonic_ns + warmup_ns
            )

    def save_settings(
        self,
        path: str | Path,
        settings: Mapping[str, float | bool] | None = None,
    ) -> Path:
        """Save explicit settings, or only values confirmed by driver readback."""

        with self._lock:
            selected = dict(self._confirmed_settings) if settings is None else dict(settings)
            metadata: dict[str, object] = {
                "saved_at_iso": datetime.now(timezone.utc).isoformat(
                    timespec="microseconds"
                ),
                "simulation_mode": False,
            }
            if self._connection_info is not None:
                info = asdict(self._connection_info)
                if isinstance(info.get("actual_fps"), float) and not math.isfinite(
                    info["actual_fps"]
                ):
                    info["actual_fps"] = None
                metadata["connection_info"] = info
            return save_camera_settings(path, selected, metadata=metadata)

    def load_settings(
        self, path: str | Path, *, apply: bool = False
    ) -> dict[str, float | bool] | tuple[CameraPropertyReadback, ...]:
        """Load controls, applying them only when the caller explicitly asks."""

        settings = load_camera_settings(path)
        if apply:
            return self.apply_settings(settings)
        return settings

    def diagnostics(self) -> Mapping[str, object]:
        """Return compact requested/readback/error evidence for the GUI."""

        with self._lock:
            return {
                "camera_model": PRIMARY_CAMERA_MODEL,
                "connected": self.is_connected,
                "simulation_mode": False,
                "selected_backend": self._selected_backend,
                "mode_confirmed": self.mode_confirmed,
                "mode_readbacks": [asdict(item) for item in self.mode_readbacks],
                "property_readbacks": [
                    asdict(item) for item in self.property_readbacks
                ],
                "backend_attempts": [asdict(item) for item in self._backend_attempts],
                "last_error": self._last_error,
            }


# Concise compatibility name for application wiring.
CameraService = OpenCVCameraService


__all__ = [
    "CAMERA_SETTINGS_SCHEMA_VERSION",
    "PRIMARY_CAMERA_MODEL",
    "SUPPORTED_CAMERA_PROPERTIES",
    "CameraBackendAttempt",
    "CameraConnectionError",
    "CameraDeviceInfo",
    "CameraNotConnectedError",
    "CameraService",
    "CameraServiceError",
    "OpenCVCameraService",
    "load_camera_settings",
    "save_camera_settings",
]
