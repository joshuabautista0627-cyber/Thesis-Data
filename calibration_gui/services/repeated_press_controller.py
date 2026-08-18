"""Generation-safe repeated-press state machine with force-triggered recovery."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
import math
from threading import Event, RLock, Thread
import time
from typing import Any
from uuid import uuid4

from core.models import LoadCellSample, PrinterConfig
from core.motion import MotionContext, MotionSnapshot
from services.printer_service import (
    PrinterCommand,
    PrinterCommandError,
    PrinterSafetyError,
    PrinterSerialService,
)


class RepeatedPressState(str, Enum):
    IDLE = "idle"
    VERIFYING_READINESS = "verifying_readiness"
    PRE_ROLL = "pre_roll"
    OPTIONAL_TARE = "optional_tare"
    OPTIONAL_BASELINE = "optional_baseline"
    MOVE_TO_START = "move_to_start"
    PRESSING_DOWN = "pressing_down"
    HOLDING = "holding"
    RETRACTING = "retracting"
    INTER_CYCLE_DWELL = "inter_cycle_dwell"
    PAUSED = "paused"
    STOPPING = "stopping"
    ABORTING = "aborting"
    POST_ROLL = "post_roll"
    COMPLETE = "complete"
    ERROR = "error"


TERMINAL_SEQUENCE_STATES = {
    RepeatedPressState.IDLE,
    RepeatedPressState.COMPLETE,
    RepeatedPressState.ERROR,
}


@dataclass(frozen=True, slots=True)
class RepeatedPressConfig:
    cycles: int = 3
    displacement_mm: float = -1.0
    down_feed_mm_min: float = 60.0
    up_feed_mm_min: float = 180.0
    bottom_hold_s: float = 0.5
    top_dwell_s: float = 1.0
    pre_roll_s: float = 1.0
    post_roll_s: float = 1.0
    force_limit_N: float = 20.0
    tare_before_sequence: bool = False
    fresh_baseline_before_sequence: bool = False
    return_to_press_zero_on_finish: bool = True
    negative_z_direction_confirmed: bool = False

    def validate(self, printer: PrinterConfig, *, press_zero_z_mm: float) -> float:
        if isinstance(self.cycles, bool) or self.cycles < 1 or self.cycles > 100_000:
            raise ValueError("cycles must be an integer from 1 through 100000")
        for name in (
            "displacement_mm",
            "down_feed_mm_min",
            "up_feed_mm_min",
            "bottom_hold_s",
            "top_dwell_s",
            "pre_roll_s",
            "post_roll_s",
            "force_limit_N",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.displacement_mm >= 0:
            raise ValueError("press displacement must be negative for the validated Ender 3 setup")
        if not self.negative_z_direction_confirmed:
            raise ValueError("explicit confirmation that negative Z moves downward is required")
        if not printer.minimum_displacement_mm <= self.displacement_mm <= printer.maximum_displacement_mm:
            raise ValueError("displacement is outside configured safety limits")
        for name in ("down_feed_mm_min", "up_feed_mm_min"):
            value = float(getattr(self, name))
            if not printer.minimum_z_feed_mm_min <= value <= printer.maximum_z_feed_mm_min:
                raise ValueError(f"{name} is outside configured Z feed limits")
        for name in ("bottom_hold_s", "top_dwell_s", "pre_roll_s", "post_roll_s"):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not printer.minimum_force_limit_N <= self.force_limit_N <= printer.maximum_force_limit_N:
            raise ValueError("force limit is outside configured safety limits")
        target_z = float(press_zero_z_mm) + float(self.displacement_mm)
        if not printer.z_min_mm <= target_z <= printer.z_max_mm:
            raise ValueError(
                f"computed press target Z={target_z:g} mm is outside configured machine limits"
            )
        return target_z

    def estimate_duration_s(self) -> float:
        travel_mm = abs(float(self.displacement_mm))
        down_s = travel_mm / float(self.down_feed_mm_min) * 60.0
        up_s = travel_mm / float(self.up_feed_mm_min) * 60.0
        per_cycle = down_s + up_s + self.bottom_hold_s + self.top_dwell_s
        return self.pre_roll_s + self.post_roll_s + self.cycles * per_cycle


@dataclass(frozen=True, slots=True)
class SequenceResult:
    sequence_id: str
    generation: int
    status: str
    configured_cycles: int
    completed_cycles: int
    force_limit_exceeded: bool
    maximum_abs_force_N: float
    error: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    events: tuple[Mapping[str, Any], ...]
    commands: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["events"] = [dict(item) for item in self.events]
        values["commands"] = [dict(item) for item in self.commands]
        return values


class RepeatedPressController:
    """Run one explicit sequence while keeping the caller and GUI responsive."""

    def __init__(
        self,
        printer: PrinterSerialService,
        motion_context: MotionContext,
        *,
        readiness_callback: Callable[[], tuple[bool, str]] | None = None,
        tare_callback: Callable[[], None] | None = None,
        baseline_callback: Callable[[], None] | None = None,
        state_callback: Callable[[Mapping[str, Any]], None] | None = None,
        completed_callback: Callable[[SequenceResult], None] | None = None,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.printer = printer
        self.motion_context = motion_context
        self.readiness_callback = readiness_callback
        self.tare_callback = tare_callback
        self.baseline_callback = baseline_callback
        self.state_callback = state_callback
        self.completed_callback = completed_callback
        self._monotonic_ns = monotonic_ns
        self._lock = RLock()
        self._thread: Thread | None = None
        self._generation = 0
        self._state = RepeatedPressState.IDLE
        self._config: RepeatedPressConfig | None = None
        self._stop_requested = Event()
        self._abort_requested = Event()
        self._pause_requested = Event()
        self._resume_requested = Event()
        self._force_limit_event = Event()
        self._sequence_id = ""
        self._completed_cycles = 0
        self._maximum_abs_force_N = 0.0
        self._force_limit_exceeded = False
        self._started_ns = 0
        self._events: list[dict[str, Any]] = []

    @property
    def state(self) -> RepeatedPressState:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self, config: RepeatedPressConfig) -> int:
        with self._lock:
            if self.running:
                raise RuntimeError("a repeated-press sequence is already running")
            position = self.printer.position
            if not position.press_zero_valid or not math.isfinite(position.press_zero_z_mm):
                raise PrinterSafetyError("a valid in-memory press zero is required")
            config.validate(self.printer._require_config(), press_zero_z_mm=position.press_zero_z_mm)
            self._generation += 1
            generation = self._generation
            self._config = config
            self._sequence_id = str(uuid4())
            self._completed_cycles = 0
            self._maximum_abs_force_N = 0.0
            self._force_limit_exceeded = False
            self._started_ns = self._monotonic_ns()
            self._events = []
            for event in (
                self._stop_requested,
                self._abort_requested,
                self._pause_requested,
                self._resume_requested,
                self._force_limit_event,
            ):
                event.clear()
            self._thread = Thread(
                target=self._run,
                args=(generation, config, position.press_zero_z_mm),
                name=f"repeated-press-{generation}",
                daemon=True,
            )
            self._thread.start()
            return generation

    def pause(self) -> None:
        if self.running:
            self._pause_requested.set()
            self._emit_event("pause_requested")

    def resume(self) -> None:
        if self.running:
            self._pause_requested.clear()
            self._resume_requested.set()
            self._emit_event("resume_requested")

    def stop(self) -> None:
        """Stop the sequence and request a safe return to press zero."""

        if self.running:
            self._stop_requested.set()
            self._emit_event("stop_requested")
            self.printer.quick_stop(wait_timeout_s=0.25)

    def abort(self) -> None:
        """Cancel queued work, interrupt motion, and attempt a bounded retract."""

        if self.running:
            self._abort_requested.set()
            self._emit_event("abort_requested")
            self.printer.cancel_pending("repeated-press abort")
            self.printer.quick_stop(wait_timeout_s=0.25)

    def emergency_stop(self) -> bool:
        self._abort_requested.set()
        self._emit_event("emergency_stop_requested")
        return self.printer.emergency_stop()

    def wait(self, timeout_s: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=None if timeout_s is None else max(0.0, float(timeout_s)))
        return not thread.is_alive()

    def observe_force(self, sample: LoadCellSample | object) -> None:
        """Consume calibrated samples live; invalid/unscaled samples never trip a limit."""

        if not self.running:
            return
        valid = bool(getattr(sample, "loadcell_valid", False))
        force = float(getattr(sample, "force_N", math.nan))
        if not valid or not math.isfinite(force):
            return
        magnitude = abs(force)
        with self._lock:
            self._maximum_abs_force_N = max(self._maximum_abs_force_N, magnitude)
            config = self._config
            exceeded = bool(config is not None and magnitude >= config.force_limit_N)
            if exceeded and not self._force_limit_exceeded:
                self._force_limit_exceeded = True
                self._force_limit_event.set()
                self.motion_context.update(force_limit_exceeded=True, sequence_aborted=True)
                self._emit_event("force_limit_exceeded", force_N=force)
        if exceeded:
            # This only sets a request and wakes the sole writer. The load-cell
            # serial owner never writes to the printer port.
            request_started_ns = self._monotonic_ns()
            requested = self.printer.request_quick_stop()
            self._emit_event(
                "force_quick_stop_requested",
                request_accepted=requested,
                observer_request_latency_ms=(
                    self._monotonic_ns() - request_started_ns
                )
                / 1_000_000.0,
            )

    def preview(self, config: RepeatedPressConfig) -> dict[str, Any]:
        position = self.printer.position
        target = config.validate(
            self.printer._require_config(), press_zero_z_mm=position.press_zero_z_mm
        )
        return {
            "press_zero_x_mm": position.press_zero_x_mm,
            "press_zero_y_mm": position.press_zero_y_mm,
            "press_zero_z_mm": position.press_zero_z_mm,
            "target_machine_z_mm": target,
            "cycles": config.cycles,
            "estimated_duration_s": config.estimate_duration_s(),
            "force_limit_N": config.force_limit_N,
        }

    def _run(self, generation: int, config: RepeatedPressConfig, press_zero_z: float) -> None:
        error = ""
        status = "complete"
        try:
            self._transition(generation, RepeatedPressState.VERIFYING_READINESS, config, press_zero_z)
            if self.readiness_callback is not None:
                ready, reason = self.readiness_callback()
                if not ready:
                    raise PrinterSafetyError(reason or "recording prerequisites are not ready")
            target_z = config.validate(
                self.printer._require_config(), press_zero_z_mm=press_zero_z
            )
            self._transition(generation, RepeatedPressState.PRE_ROLL, config, press_zero_z, target_z=target_z)
            self._interruptible_wait(config.pre_roll_s)
            if config.tare_before_sequence:
                self._transition(generation, RepeatedPressState.OPTIONAL_TARE, config, press_zero_z, target_z=target_z)
                if self.tare_callback is None:
                    raise PrinterSafetyError("tare-before-sequence was selected but no tare callback is available")
                self.tare_callback()
            if config.fresh_baseline_before_sequence:
                self._transition(generation, RepeatedPressState.OPTIONAL_BASELINE, config, press_zero_z, target_z=target_z)
                if self.baseline_callback is None:
                    raise PrinterSafetyError("fresh-baseline option was selected but no baseline callback is available")
                self.baseline_callback()
            self._transition(generation, RepeatedPressState.MOVE_TO_START, config, press_zero_z, target_z=target_z)
            self._move_z(press_zero_z, config.up_feed_mm_min)
            for cycle in range(1, config.cycles + 1):
                self._check_terminal_request()
                cycle_id = str(uuid4())
                self._transition(
                    generation,
                    RepeatedPressState.PRESSING_DOWN,
                    config,
                    press_zero_z,
                    target_z=target_z,
                    cycle=cycle,
                    cycle_id=cycle_id,
                )
                try:
                    self._move_z(target_z, config.down_feed_mm_min)
                except PrinterCommandError:
                    if not (self._stop_requested.is_set() or self._abort_requested.is_set() or self._force_limit_event.is_set()):
                        raise
                self._check_force_limit()
                self._check_terminal_request()
                self._transition(
                    generation,
                    RepeatedPressState.HOLDING,
                    config,
                    press_zero_z,
                    target_z=target_z,
                    cycle=cycle,
                    cycle_id=cycle_id,
                )
                self._interruptible_wait(config.bottom_hold_s)
                self._transition(
                    generation,
                    RepeatedPressState.RETRACTING,
                    config,
                    press_zero_z,
                    target_z=target_z,
                    cycle=cycle,
                    cycle_id=cycle_id,
                )
                try:
                    self._move_z(press_zero_z, config.up_feed_mm_min)
                except PrinterCommandError:
                    if not (
                        self._stop_requested.is_set()
                        or self._abort_requested.is_set()
                        or self._force_limit_event.is_set()
                    ):
                        raise
                self._check_force_limit()
                self._check_terminal_request()
                self._completed_cycles = cycle
                if self._pause_requested.is_set() and cycle < config.cycles:
                    self._transition(generation, RepeatedPressState.PAUSED, config, press_zero_z, target_z=target_z, cycle=cycle)
                    while self._pause_requested.is_set():
                        self._check_terminal_request()
                        self._resume_requested.wait(0.05)
                    self._resume_requested.clear()
                if cycle < config.cycles:
                    self._transition(generation, RepeatedPressState.INTER_CYCLE_DWELL, config, press_zero_z, target_z=target_z, cycle=cycle)
                    self._interruptible_wait(config.top_dwell_s)
            self._transition(generation, RepeatedPressState.POST_ROLL, config, press_zero_z, target_z=target_z)
            self._interruptible_wait(config.post_roll_s)
            self._transition(generation, RepeatedPressState.COMPLETE, config, press_zero_z, target_z=target_z)
        except _StopRequested:
            status = "stopped"
            self._transition(generation, RepeatedPressState.STOPPING, config, press_zero_z, aborted=True)
            self._safe_retract(press_zero_z, config.up_feed_mm_min)
            self._transition(generation, RepeatedPressState.POST_ROLL, config, press_zero_z, aborted=True)
            self._noninterruptible_wait(config.post_roll_s)
        except _AbortRequested as exc:
            status = "aborted"
            error = str(exc)
            self._transition(generation, RepeatedPressState.ABORTING, config, press_zero_z, aborted=True)
            self._safe_retract(press_zero_z, config.up_feed_mm_min)
            self._transition(generation, RepeatedPressState.POST_ROLL, config, press_zero_z, aborted=True)
            self._noninterruptible_wait(config.post_roll_s)
        except Exception as exc:
            status = "error"
            error = str(exc)
            self._transition(generation, RepeatedPressState.ERROR, config, press_zero_z, aborted=True)
            self._safe_retract(press_zero_z, config.up_feed_mm_min)
        finally:
            result = SequenceResult(
                sequence_id=self._sequence_id,
                generation=generation,
                status=status,
                configured_cycles=config.cycles,
                completed_cycles=self._completed_cycles,
                force_limit_exceeded=self._force_limit_exceeded,
                maximum_abs_force_N=self._maximum_abs_force_N,
                error=error,
                started_monotonic_ns=self._started_ns,
                finished_monotonic_ns=self._monotonic_ns(),
                events=tuple(dict(item) for item in self._events),
                commands=tuple(self.printer.diagnostics()["command_history"]),
            )
            self.motion_context.update(
                sequence_status=status,
                motion_phase=status,
                command_id="",
                sequence_paused=False,
                sequence_aborted=status != "complete",
                force_limit_exceeded=self._force_limit_exceeded,
            )
            callback = self.completed_callback
            if callback is not None and generation == self.generation:
                callback(result)

    def _move_z(self, z_mm: float, feed: float) -> None:
        def submitted(command: PrinterCommand) -> None:
            self.motion_context.update(
                command_id=command.command_id,
                commanded_z_mm=float(z_mm),
                requested_feed_rate_mm_min=float(feed),
            )
            self._emit_event("command_submitted", command_id=command.command_id, gcode=command.gcode)

        self.printer.move_absolute(
            z_mm=float(z_mm),
            feed_rate_mm_min=float(feed),
            query_after=False,
            motion_submitted_callback=submitted,
        )

    def _safe_retract(self, press_zero_z: float, feed: float) -> None:
        if not self.printer.connected or not self.printer.marlin_verified:
            self._emit_event("safe_retract_unavailable", reason="printer connection is unavailable")
            return
        position = self.printer.position
        if not position.press_zero_valid:
            self._emit_event("safe_retract_unavailable", reason="press zero was invalidated")
            return
        if (
            position.tracked_valid
            and math.isfinite(position.tracked_z_mm)
            and math.isclose(position.tracked_z_mm, press_zero_z, rel_tol=0.0, abs_tol=1.0e-6)
        ):
            self._emit_event("safe_retract_already_at_zero")
            return
        try:
            self._move_z(press_zero_z, feed)
            self._emit_event("safe_retract_completed")
        except Exception as exc:
            self._emit_event("safe_retract_failed", reason=str(exc))

    def _transition(
        self,
        generation: int,
        state: RepeatedPressState,
        config: RepeatedPressConfig,
        press_zero_z: float,
        *,
        target_z: float = math.nan,
        cycle: int = 0,
        cycle_id: str = "",
        aborted: bool = False,
    ) -> None:
        if generation != self.generation:
            raise _AbortRequested("stale sequence generation")
        with self._lock:
            self._state = state
        position = self.printer.position
        snapshot = MotionSnapshot(
            sequence_id=self._sequence_id,
            sequence_generation=generation,
            sequence_status="running" if state not in {RepeatedPressState.COMPLETE, RepeatedPressState.ERROR} else state.value,
            motion_phase=state.value,
            motion_phase_host_monotonic_ns=self._monotonic_ns(),
            motion_cycle_id=cycle_id,
            motion_cycle_index=cycle,
            motion_total_cycles=config.cycles,
            command_id="",
            press_zero_x_mm=position.press_zero_x_mm,
            press_zero_y_mm=position.press_zero_y_mm,
            press_zero_z_mm=press_zero_z,
            target_displacement_mm=config.displacement_mm,
            target_machine_z_mm=target_z,
            commanded_x_mm=position.tracked_x_mm,
            commanded_y_mm=position.tracked_y_mm,
            commanded_z_mm=position.tracked_z_mm,
            requested_feed_rate_mm_min=math.nan,
            printer_reported_x_mm=position.reported_x_mm,
            printer_reported_y_mm=position.reported_y_mm,
            printer_reported_z_mm=position.reported_z_mm,
            printer_position_valid=position.reported_valid,
            press_zero_valid=position.press_zero_valid,
            force_limit_N=config.force_limit_N,
            force_limit_exceeded=self._force_limit_exceeded,
            sequence_paused=state is RepeatedPressState.PAUSED,
            sequence_aborted=aborted,
        )
        self.motion_context.replace(snapshot)
        self._emit_event("state", state=state.value, cycle=cycle, cycle_id=cycle_id)
        callback = self.state_callback
        if callback is not None:
            callback({"generation": generation, "state": state.value, "snapshot": snapshot.as_row()})

    def _interruptible_wait(self, seconds: float) -> None:
        deadline = time.perf_counter() + max(0.0, float(seconds))
        while time.perf_counter() < deadline:
            self._check_force_limit()
            self._check_terminal_request()
            time.sleep(min(0.02, max(0.0, deadline - time.perf_counter())))

    @staticmethod
    def _noninterruptible_wait(seconds: float) -> None:
        if seconds > 0:
            time.sleep(float(seconds))

    def _check_force_limit(self) -> None:
        if self._force_limit_event.is_set():
            raise _AbortRequested("calibrated force limit exceeded")

    def _check_terminal_request(self) -> None:
        if self._abort_requested.is_set():
            raise _AbortRequested("sequence aborted by operator")
        if self._stop_requested.is_set():
            raise _StopRequested()

    def _emit_event(self, event_type: str, **values: Any) -> None:
        event = {
            "host_monotonic_ns": self._monotonic_ns(),
            "event": str(event_type),
            "sequence_id": self._sequence_id,
            "generation": self.generation,
            **values,
        }
        with self._lock:
            self._events.append(event)


class _StopRequested(Exception):
    pass


class _AbortRequested(Exception):
    pass


__all__ = [
    "RepeatedPressConfig",
    "RepeatedPressController",
    "RepeatedPressState",
    "SequenceResult",
    "TERMINAL_SEQUENCE_STATES",
]
