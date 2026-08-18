"""Narrow structural interfaces for the sole camera and serial owners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

import numpy as np

from core.models import CameraConfig, LoadCellConfig, LoadCellSample


@dataclass(frozen=True, slots=True)
class CameraPropertyReadback:
    """Evidence for one attempted camera-property write and readback."""

    property_name: str
    requested_value: float | bool | None
    actual_value: float | bool | None
    write_succeeded: bool
    read_succeeded: bool
    confirmed: bool
    supported: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class CameraPropertyCapability:
    """Best available driver evidence; unavailable ranges remain explicit."""

    property_name: str
    supported: bool
    readable: bool
    writable: bool | None
    current_value: float | bool | None
    minimum_value: float | None = None
    maximum_value: float | None = None
    step_size: float | None = None
    default_value: float | bool | None = None
    automatic_supported: bool | None = None
    requires_stream_restart: bool = False
    status: str = ""


@dataclass(frozen=True, slots=True)
class CameraConnectionInfo:
    """Actual mode and provenance returned only after a successful connection."""

    device_index: int
    backend: str
    requested_width: int
    requested_height: int
    requested_fps: float
    actual_width: int
    actual_height: int
    actual_fps: float
    simulation_mode: bool
    source_label: str


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """One original, unannotated frame and its timestamps from the camera owner."""

    original_bgr: np.ndarray
    source_frame_id: int
    host_monotonic_ns: int
    wall_clock_iso: str

    def __post_init__(self) -> None:
        if self.source_frame_id < 0 or self.host_monotonic_ns < 0:
            raise ValueError("frame IDs and timestamps must be nonnegative")
        if not isinstance(self.original_bgr, np.ndarray):
            raise TypeError("original_bgr must be a NumPy array")
        if self.original_bgr.ndim != 3 or self.original_bgr.shape[2] != 3:
            raise ValueError("original_bgr must have shape (height, width, 3)")


@dataclass(frozen=True, slots=True)
class SerialConnectionInfo:
    """Validated HELLO identity for one physical or simulated device session."""

    port: str
    baud_rate: int
    device_name: str
    protocol_version: str
    firmware_version: str
    device_session_id: str
    simulation_mode: bool


@runtime_checkable
class CameraServiceInterface(Protocol):
    """Contract implemented by both OpenCV and video-file camera sources.

    Exactly one instance owns the underlying capture object.  Consumers receive
    the same :class:`CapturedFrame`; preview annotations must be made on a copy.
    """

    @property
    def is_connected(self) -> bool:
        """Whether this owner currently has a usable capture source."""

    @property
    def simulation_mode(self) -> bool:
        """Whether the source is a clearly labeled video-file simulation."""

    def connect(
        self, config: CameraConfig, *, source_path: str | None = None
    ) -> CameraConnectionInfo:
        """Open the sole capture source and return its actual read-back mode."""

    def disconnect(self) -> None:
        """Release the capture source idempotently."""

    def read_frame(self) -> CapturedFrame | None:
        """Read one frame outside the GUI thread; return None at source end."""

    def apply_settings(
        self, requested: Mapping[str, float | bool]
    ) -> tuple[CameraPropertyReadback, ...]:
        """Attempt property writes and return requested-versus-actual evidence."""

    def warmup_complete(self, host_monotonic_ns: int) -> bool:
        """Report whether the configured discard-only warm-up has elapsed."""


@runtime_checkable
class SerialServiceInterface(Protocol):
    """Contract implemented by physical serial and deterministic load sources.

    Exactly one instance owns the serial stream.  Parsed samples are fanned out
    to plotting, calibration, and recording consumers by the application layer.
    """

    @property
    def is_connected(self) -> bool:
        """Whether HELLO (or an explicit simulation identity) was validated."""

    @property
    def simulation_mode(self) -> bool:
        """Whether samples come from a clearly labeled synthetic/playback source."""

    def connect(
        self, config: LoadCellConfig, *, port: str | None = None
    ) -> SerialConnectionInfo:
        """Open the sole stream and establish a fresh device-session identity."""

    def disconnect(self) -> None:
        """Stop streaming and release the source idempotently."""

    def send_command(self, command: str) -> str:
        """Send PING, START, STOP, or STATUS and return its validated response."""

    def read_sample(self) -> LoadCellSample | None:
        """Return the next valid new conversion, outside the GUI thread."""

    def diagnostics(self) -> Mapping[str, Any]:
        """Return counts, rates, intervals, malformed lines, and timeout evidence."""


__all__ = [
    "CameraConnectionInfo",
    "CameraPropertyReadback",
    "CameraPropertyCapability",
    "CameraServiceInterface",
    "CapturedFrame",
    "SerialConnectionInfo",
    "SerialServiceInterface",
]
