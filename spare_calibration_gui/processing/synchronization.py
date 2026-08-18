"""Host-clock frame/load-cell synchronization and Arduino clock tracking.

Frame-to-force alignment uses only host monotonic receipt timestamps.  Arduino
``micros()`` values are preserved for provenance and unwrapped within each device
session, but are never substituted for the authoritative host clock.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import math
from numbers import Integral, Real
from typing import Any


DEFAULT_MAX_SYNC_GAP_MS = 200.0
DEFAULT_CONTACT_THRESHOLD_N = 0.05
ARDUINO_MICROS_MODULUS = 2**32


class ArduinoClockEvent(str, Enum):
    """Classification of a transition between two Arduino data rows."""

    INITIAL = "initial"
    NORMAL = "normal"
    ROLLOVER = "rollover"
    RESET = "reset"
    DUPLICATE_SAMPLE_ID = "duplicate_sample_id"
    SAMPLE_ID_COLLISION = "sample_id_collision"


@dataclass(frozen=True, slots=True)
class ArduinoTimingUpdate:
    """Result of advancing :class:`ArduinoMicrosTracker` by one sample."""

    sample_id: int
    raw_micros: int
    unwrapped_micros: int
    event: ArduinoClockEvent
    device_session_index: int
    new_device_session: bool


@dataclass(frozen=True, slots=True)
class _NormalizedSample:
    host_monotonic_ns: int
    arduino_sample_id: int
    arduino_micros: int
    raw_adc: float
    force_gf: float
    force_N: float
    device_session_id: str


@dataclass(frozen=True, slots=True)
class SynchronizationResult:
    """Force fields and provenance aligned to one accepted frame."""

    frame_host_monotonic_ns: int | float
    closest_arduino_sample_id: int | None
    closest_arduino_micros: int | None
    closest_raw_adc: float
    interpolated_raw_adc: float
    force_gf: float
    force_N: float
    synchronization_method: str
    nearest_sample_gap_ms: float
    synchronization_valid: bool
    closest_device_session_id: str | None
    contact_state_derived: bool | float
    synchronization_offset_ms: float = math.nan

    @property
    def method(self) -> str:
        """Concise alias used by plotting and diagnostic code."""

        return self.synchronization_method

    @classmethod
    def invalid(
        cls,
        frame_host_monotonic_ns: int | float,
        *,
        closest_sample: _NormalizedSample | None = None,
        nearest_sample_gap_ms: float = math.nan,
    ) -> "SynchronizationResult":
        """Create a NaN-force result, retaining nearest provenance when known."""

        return cls(
            frame_host_monotonic_ns=frame_host_monotonic_ns,
            closest_arduino_sample_id=(
                None if closest_sample is None else closest_sample.arduino_sample_id
            ),
            closest_arduino_micros=(
                None if closest_sample is None else closest_sample.arduino_micros
            ),
            closest_raw_adc=(
                math.nan if closest_sample is None else closest_sample.raw_adc
            ),
            interpolated_raw_adc=math.nan,
            force_gf=math.nan,
            force_N=math.nan,
            synchronization_method="invalid",
            nearest_sample_gap_ms=nearest_sample_gap_ms,
            synchronization_valid=False,
            closest_device_session_id=(
                None
                if closest_sample is None or not closest_sample.device_session_id
                else closest_sample.device_session_id
            ),
            contact_state_derived=math.nan,
            synchronization_offset_ms=(
                math.nan
                if closest_sample is None
                or not isinstance(frame_host_monotonic_ns, (int, float))
                or not math.isfinite(float(frame_host_monotonic_ns))
                else (
                    float(frame_host_monotonic_ns)
                    - closest_sample.host_monotonic_ns
                )
                / 1_000_000.0
            ),
        )


def _as_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be an integer")
    if isinstance(value, Integral):
        result = int(value)
    else:
        numeric = float(value)
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise TypeError(f"{name} must be an integer")
        result = int(numeric)
    if result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return result


def _as_finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a real number, not bool")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a real number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def classify_arduino_clock_transition(
    previous_sample_id: int,
    previous_micros: int,
    current_sample_id: int,
    current_micros: int,
    *,
    micros_modulus: int = ARDUINO_MICROS_MODULUS,
    rollover_guard_fraction: float = 0.25,
) -> ArduinoClockEvent:
    """Distinguish normal progress, 32-bit rollover, duplicate, and reset.

    A decreasing sample ID is a reset.  A decreasing ``micros()`` value counts as
    rollover only when it crosses from the top guard region to the bottom guard
    region while sample IDs continue increasing; other decreases are resets.
    This prevents an Arduino reboot from being silently interpreted as rollover.
    """

    previous_id = _as_int(previous_sample_id, "previous_sample_id")
    current_id = _as_int(current_sample_id, "current_sample_id")
    modulus = _as_int(micros_modulus, "micros_modulus", minimum=2)
    previous_us = _as_int(previous_micros, "previous_micros")
    current_us = _as_int(current_micros, "current_micros")
    if previous_us >= modulus or current_us >= modulus:
        raise ValueError("Arduino micros value must be below micros_modulus")
    guard = _as_finite_float(rollover_guard_fraction, "rollover_guard_fraction")
    if not 0.0 < guard < 0.5:
        raise ValueError("rollover_guard_fraction must be between 0 and 0.5")

    if current_id < previous_id:
        return ArduinoClockEvent.RESET
    if current_id == previous_id:
        if current_us < previous_us:
            return ArduinoClockEvent.RESET
        if current_us == previous_us:
            return ArduinoClockEvent.DUPLICATE_SAMPLE_ID
        # A repeated ID with a later clock is neither a byte-for-byte duplicate
        # nor a credible reset.  Report it without advancing trusted clock state.
        return ArduinoClockEvent.SAMPLE_ID_COLLISION
    if current_us >= previous_us:
        return ArduinoClockEvent.NORMAL
    high_watermark = modulus * (1.0 - guard)
    low_watermark = modulus * guard
    if previous_us >= high_watermark and current_us <= low_watermark:
        return ArduinoClockEvent.ROLLOVER
    return ArduinoClockEvent.RESET


def detect_micros_rollover_or_reset(
    previous_sample_id: int,
    previous_micros: int,
    current_sample_id: int,
    current_micros: int,
) -> ArduinoClockEvent:
    """Convenience alias with the standard 32-bit Arduino clock settings."""

    return classify_arduino_clock_transition(
        previous_sample_id,
        previous_micros,
        current_sample_id,
        current_micros,
    )


class ArduinoMicrosTracker:
    """Unwrap ``micros()`` while assigning reset-separated device sessions."""

    def __init__(self, *, micros_modulus: int = ARDUINO_MICROS_MODULUS) -> None:
        self._modulus = _as_int(micros_modulus, "micros_modulus", minimum=2)
        self.reset()

    @property
    def device_session_index(self) -> int:
        """Zero-based host device-session counter."""

        return self._device_session_index

    def reset(self) -> None:
        """Clear tracking for a newly opened serial connection."""

        self._last_sample_id: int | None = None
        self._last_raw_micros: int | None = None
        self._rollover_count = 0
        self._device_session_index = 0

    def update(self, sample_id: int, arduino_micros: int) -> ArduinoTimingUpdate:
        """Advance with one valid DATA row and return timing/session provenance."""

        current_id = _as_int(sample_id, "sample_id")
        current_us = _as_int(arduino_micros, "arduino_micros")
        if current_us >= self._modulus:
            raise ValueError("arduino_micros must be below micros_modulus")

        if self._last_sample_id is None or self._last_raw_micros is None:
            event = ArduinoClockEvent.INITIAL
            new_session = True
        else:
            event = classify_arduino_clock_transition(
                self._last_sample_id,
                self._last_raw_micros,
                current_id,
                current_us,
                micros_modulus=self._modulus,
            )
            new_session = event is ArduinoClockEvent.RESET
            if event is ArduinoClockEvent.ROLLOVER:
                self._rollover_count += 1
            elif event is ArduinoClockEvent.RESET:
                self._device_session_index += 1
                self._rollover_count = 0

        unwrapped = current_us + self._rollover_count * self._modulus
        if event not in (
            ArduinoClockEvent.DUPLICATE_SAMPLE_ID,
            ArduinoClockEvent.SAMPLE_ID_COLLISION,
        ):
            self._last_sample_id = current_id
            self._last_raw_micros = current_us
        return ArduinoTimingUpdate(
            sample_id=current_id,
            raw_micros=current_us,
            unwrapped_micros=unwrapped,
            event=event,
            device_session_index=self._device_session_index,
            new_device_session=new_session,
        )


def unwrap_arduino_micros(
    micros_values: Iterable[int],
    sample_ids: Iterable[int] | None = None,
    *,
    micros_modulus: int = ARDUINO_MICROS_MODULUS,
) -> list[ArduinoTimingUpdate]:
    """Return deterministic rollover/reset updates for a finite sample sequence."""

    raw_values = list(micros_values)
    ids = list(range(len(raw_values))) if sample_ids is None else list(sample_ids)
    if len(ids) != len(raw_values):
        raise ValueError("sample_ids and micros_values must have equal lengths")
    tracker = ArduinoMicrosTracker(micros_modulus=micros_modulus)
    return [tracker.update(sample_id, micros) for sample_id, micros in zip(ids, raw_values)]


def _value(sample: Any, names: tuple[str, ...]) -> Any:
    if isinstance(sample, Mapping):
        for name in names:
            if name in sample:
                return sample[name]
    else:
        for name in names:
            if hasattr(sample, name):
                return getattr(sample, name)
    joined = ", ".join(names)
    raise ValueError(f"load-cell sample is missing required field ({joined})")


def _optional_value(sample: Any, names: tuple[str, ...], default: Any) -> Any:
    if isinstance(sample, Mapping):
        for name in names:
            if name in sample:
                return sample[name]
    else:
        for name in names:
            if hasattr(sample, name):
                return getattr(sample, name)
    return default


def _normalize_sample(sample: Any) -> _NormalizedSample | None:
    valid = _optional_value(
        sample, ("loadcell_valid", "load_cell_valid", "valid"), True
    )
    if not bool(valid):
        return None
    timestamp = _as_int(
        _value(sample, ("host_monotonic_ns", "receipt_monotonic_ns")),
        "host_monotonic_ns",
    )
    sample_id = _as_int(
        _value(sample, ("arduino_sample_id", "sample_id")),
        "arduino_sample_id",
    )
    micros = _as_int(_value(sample, ("arduino_micros",)), "arduino_micros")
    raw = _as_finite_float(_value(sample, ("raw_adc",)), "raw_adc")
    try:
        force_gf = _as_finite_float(_value(sample, ("force_gf",)), "force_gf")
        force_n = _as_finite_float(_value(sample, ("force_N", "force_n")), "force_N")
    except ValueError as exc:
        # A NaN calibrated force is expected before calibration and cannot be used
        # for synchronization.  Preserve its raw source row elsewhere and skip it.
        if "must be finite" in str(exc):
            return None
        raise
    device_session_id = str(
        _optional_value(sample, ("device_session_id", "device_session_identifier"), "")
    )
    return _NormalizedSample(
        host_monotonic_ns=timestamp,
        arduino_sample_id=sample_id,
        arduino_micros=micros,
        raw_adc=raw,
        force_gf=force_gf,
        force_N=force_n,
        device_session_id=device_session_id,
    )


@dataclass(frozen=True, slots=True)
class _PreparedTimeline:
    samples: tuple[_NormalizedSample, ...]
    timestamps_ns: tuple[int, ...]


def _prepare_timeline(loadcell_samples: Iterable[Any]) -> _PreparedTimeline:
    normalized = tuple(
        sample
        for raw_sample in loadcell_samples
        if (sample := _normalize_sample(raw_sample)) is not None
    )
    ordered = tuple(
        sorted(
            normalized,
            key=lambda sample: (
                sample.host_monotonic_ns,
                sample.arduino_sample_id,
            ),
        )
    )
    return _PreparedTimeline(
        samples=ordered,
        timestamps_ns=tuple(sample.host_monotonic_ns for sample in ordered),
    )


def _contact_state(force_N: float, threshold_N: float) -> bool:
    return force_N >= threshold_N


def _nearest(
    frame_timestamp_ns: int,
    candidates: tuple[_NormalizedSample, ...],
) -> tuple[_NormalizedSample, int]:
    # Earlier physical samples win an exact gap tie, making finalization stable.
    return min(
        ((sample, abs(frame_timestamp_ns - sample.host_monotonic_ns)) for sample in candidates),
        key=lambda item: (item[1], item[0].host_monotonic_ns),
    )


def _validated_sync_parameters(
    max_sync_gap_ms: float, contact_threshold_N: float
) -> tuple[float, float]:
    max_gap = _as_finite_float(max_sync_gap_ms, "max_sync_gap_ms")
    if max_gap < 0.0:
        raise ValueError("max_sync_gap_ms must be non-negative")
    threshold = _as_finite_float(contact_threshold_N, "contact_threshold_N")
    if threshold < 0.0:
        raise ValueError("contact_threshold_N must be non-negative")
    return max_gap * 1_000_000.0, threshold


def _synchronize_prepared(
    frame_timestamp: int,
    timeline: _PreparedTimeline,
    max_gap_ns: float,
    contact_threshold_N: float,
) -> SynchronizationResult:
    if not timeline.samples:
        return SynchronizationResult.invalid(frame_timestamp)
    ordered = timeline.samples
    insertion = bisect_left(timeline.timestamps_ns, frame_timestamp)

    # An exact physical sample is more authoritative than an interpolated estimate.
    if (
        insertion < len(ordered)
        and timeline.timestamps_ns[insertion] == frame_timestamp
    ):
        exact = ordered[insertion]
        return SynchronizationResult(
            frame_host_monotonic_ns=frame_timestamp,
            closest_arduino_sample_id=exact.arduino_sample_id,
            closest_arduino_micros=exact.arduino_micros,
            closest_raw_adc=exact.raw_adc,
            interpolated_raw_adc=exact.raw_adc,
            force_gf=exact.force_gf,
            force_N=exact.force_N,
            synchronization_method="nearest",
            nearest_sample_gap_ms=0.0,
            synchronization_valid=True,
            closest_device_session_id=exact.device_session_id or None,
            contact_state_derived=_contact_state(
                exact.force_N, contact_threshold_N
            ),
            synchronization_offset_ms=0.0,
        )

    if 0 < insertion < len(ordered):
        before = ordered[insertion - 1]
        after = ordered[insertion]
        before_gap_ns = frame_timestamp - before.host_monotonic_ns
        after_gap_ns = after.host_monotonic_ns - frame_timestamp
        same_device_session = before.device_session_id == after.device_session_id
        if (
            same_device_session
            and before_gap_ns <= max_gap_ns
            and after_gap_ns <= max_gap_ns
            and after.host_monotonic_ns > before.host_monotonic_ns
        ):
            ratio = before_gap_ns / (
                after.host_monotonic_ns - before.host_monotonic_ns
            )
            interpolated_raw = before.raw_adc + ratio * (after.raw_adc - before.raw_adc)
            force_gf = before.force_gf + ratio * (after.force_gf - before.force_gf)
            force_n = before.force_N + ratio * (after.force_N - before.force_N)
            closest, nearest_gap_ns = _nearest(frame_timestamp, (before, after))
            return SynchronizationResult(
                frame_host_monotonic_ns=frame_timestamp,
                closest_arduino_sample_id=closest.arduino_sample_id,
                closest_arduino_micros=closest.arduino_micros,
                closest_raw_adc=closest.raw_adc,
                interpolated_raw_adc=interpolated_raw,
                force_gf=force_gf,
                force_N=force_n,
                synchronization_method="linear_interpolation",
                nearest_sample_gap_ms=nearest_gap_ns / 1_000_000.0,
                synchronization_valid=True,
                closest_device_session_id=closest.device_session_id or None,
                contact_state_derived=_contact_state(
                    force_n, contact_threshold_N
                ),
                synchronization_offset_ms=(
                    frame_timestamp - closest.host_monotonic_ns
                )
                / 1_000_000.0,
            )

    nearest, nearest_gap_ns = _nearest(frame_timestamp, ordered)
    if nearest_gap_ns <= max_gap_ns:
        return SynchronizationResult(
            frame_host_monotonic_ns=frame_timestamp,
            closest_arduino_sample_id=nearest.arduino_sample_id,
            closest_arduino_micros=nearest.arduino_micros,
            closest_raw_adc=nearest.raw_adc,
            interpolated_raw_adc=nearest.raw_adc,
            force_gf=nearest.force_gf,
            force_N=nearest.force_N,
            synchronization_method="nearest",
            nearest_sample_gap_ms=nearest_gap_ns / 1_000_000.0,
            synchronization_valid=True,
            closest_device_session_id=nearest.device_session_id or None,
            contact_state_derived=_contact_state(
                nearest.force_N, contact_threshold_N
            ),
            synchronization_offset_ms=(
                frame_timestamp - nearest.host_monotonic_ns
            )
            / 1_000_000.0,
        )
    return SynchronizationResult.invalid(
        frame_timestamp,
        closest_sample=nearest,
        nearest_sample_gap_ms=nearest_gap_ns / 1_000_000.0,
    )


def synchronize_frame_to_force(
    frame_host_monotonic_ns: int | float,
    loadcell_samples: Iterable[Any],
    *,
    max_sync_gap_ms: float = DEFAULT_MAX_SYNC_GAP_MS,
    contact_threshold_N: float = DEFAULT_CONTACT_THRESHOLD_N,
) -> SynchronizationResult:
    """Synchronize one frame to calibrated physical samples.

    Linear interpolation is used only when samples bracket the frame and *both*
    sides are within ``max_sync_gap_ms``.  When interpolation is unavailable, the
    nearest sample is used only inside the same limit.  This conservative rule
    prevents interpolation across a long serial outage while satisfying the
    required nearest-sample fallback.  Invalid or unavailable force is represented
    by NaNs and a false validity flag; frame rows are never dropped.
    """

    max_gap_ns, threshold = _validated_sync_parameters(
        max_sync_gap_ms, contact_threshold_N
    )
    try:
        frame_timestamp = _as_int(frame_host_monotonic_ns, "frame_host_monotonic_ns")
    except (TypeError, ValueError):
        return SynchronizationResult.invalid(frame_host_monotonic_ns)
    return _synchronize_prepared(
        frame_timestamp,
        _prepare_timeline(loadcell_samples),
        max_gap_ns,
        threshold,
    )


def synchronize_frame(
    frame_host_monotonic_ns: int | float,
    loadcell_samples: Iterable[Any],
    *,
    max_sync_gap_ms: float = DEFAULT_MAX_SYNC_GAP_MS,
    contact_threshold_N: float = DEFAULT_CONTACT_THRESHOLD_N,
) -> SynchronizationResult:
    """Alias for :func:`synchronize_frame_to_force`."""

    return synchronize_frame_to_force(
        frame_host_monotonic_ns,
        loadcell_samples,
        max_sync_gap_ms=max_sync_gap_ms,
        contact_threshold_N=contact_threshold_N,
    )


def synchronize_frames_to_force(
    frame_host_monotonic_timestamps_ns: Iterable[int | float],
    loadcell_samples: Iterable[Any],
    *,
    max_sync_gap_ms: float = DEFAULT_MAX_SYNC_GAP_MS,
    contact_threshold_N: float = DEFAULT_CONTACT_THRESHOLD_N,
) -> list[SynchronizationResult]:
    """Synchronize many frames after normalizing/sorting samples exactly once."""

    max_gap_ns, threshold = _validated_sync_parameters(
        max_sync_gap_ms, contact_threshold_N
    )
    raw_timestamps = list(frame_host_monotonic_timestamps_ns)
    parsed_timestamps: list[int | None] = []
    for timestamp in raw_timestamps:
        try:
            parsed_timestamps.append(
                _as_int(timestamp, "frame_host_monotonic_ns")
            )
        except (TypeError, ValueError):
            parsed_timestamps.append(None)

    # Match scalar behavior for an all-invalid frame sequence: no load-cell row
    # needs to be consumed when there is no valid authoritative frame timestamp.
    if not any(timestamp is not None for timestamp in parsed_timestamps):
        return [SynchronizationResult.invalid(value) for value in raw_timestamps]

    timeline = _prepare_timeline(loadcell_samples)
    results: list[SynchronizationResult] = []
    for raw_timestamp, parsed_timestamp in zip(raw_timestamps, parsed_timestamps):
        if parsed_timestamp is None:
            results.append(SynchronizationResult.invalid(raw_timestamp))
        else:
            results.append(
                _synchronize_prepared(
                    parsed_timestamp,
                    timeline,
                    max_gap_ns,
                    threshold,
                )
            )
    return results


__all__ = [
    "ARDUINO_MICROS_MODULUS",
    "ArduinoClockEvent",
    "ArduinoMicrosTracker",
    "ArduinoTimingUpdate",
    "DEFAULT_CONTACT_THRESHOLD_N",
    "DEFAULT_MAX_SYNC_GAP_MS",
    "SynchronizationResult",
    "classify_arduino_clock_transition",
    "detect_micros_rollover_or_reset",
    "synchronize_frame",
    "synchronize_frame_to_force",
    "synchronize_frames_to_force",
    "unwrap_arduino_micros",
]
