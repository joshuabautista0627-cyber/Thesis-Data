"""Thread-safe motion provenance shared by printer, camera, and load-cell paths."""

from __future__ import annotations

from dataclasses import dataclass, asdict, replace
import math
from threading import RLock
import time
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class MotionSnapshot:
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

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


class MotionContext:
    """Atomically publish the exact motion state sampled by acquisition sinks."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshot = MotionSnapshot(motion_phase_host_monotonic_ns=time.perf_counter_ns())

    def snapshot(self) -> MotionSnapshot:
        with self._lock:
            return self._snapshot

    def update(self, **changes: Any) -> MotionSnapshot:
        with self._lock:
            if "motion_phase_host_monotonic_ns" not in changes:
                changes["motion_phase_host_monotonic_ns"] = time.perf_counter_ns()
            self._snapshot = replace(self._snapshot, **changes)
            return self._snapshot

    def replace(self, values: MotionSnapshot | Mapping[str, Any]) -> MotionSnapshot:
        snapshot = values if isinstance(values, MotionSnapshot) else MotionSnapshot(**dict(values))
        with self._lock:
            self._snapshot = snapshot
            return snapshot

    def reset(self, *, aborted: bool = False, status: str = "idle") -> MotionSnapshot:
        snapshot = MotionSnapshot(
            sequence_status=status,
            motion_phase=status,
            motion_phase_host_monotonic_ns=time.perf_counter_ns(),
            sequence_aborted=bool(aborted),
        )
        return self.replace(snapshot)


MOTION_FIELD_NAMES = tuple(MotionSnapshot.__dataclass_fields__)


__all__ = ["MOTION_FIELD_NAMES", "MotionContext", "MotionSnapshot"]
