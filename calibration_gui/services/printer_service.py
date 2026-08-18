"""Single-writer Marlin serial service for the independent Ender 3 port."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
import math
from queue import Empty, Queue
import re
from threading import Event, RLock, Thread
import time
from typing import Any
from uuid import uuid4

import serial
from serial.tools import list_ports

from core.models import PrinterConfig


class PrinterError(RuntimeError):
    """Base printer-control failure."""


class PrinterConnectionError(PrinterError):
    """The serial transport or Marlin identity check failed."""


class PrinterCommandError(PrinterError):
    """A queued command was rejected, timed out, cancelled, or interrupted."""


class PrinterSafetyError(PrinterError):
    """A requested motion violates an explicit safety interlock."""


class CommandState(str, Enum):
    QUEUED = "queued"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_COMMAND_STATES = {
    CommandState.COMPLETED,
    CommandState.TIMED_OUT,
    CommandState.REJECTED,
    CommandState.CANCELLED,
    CommandState.FAILED,
}


@dataclass(slots=True)
class PrinterCommand:
    gcode: str
    timeout_s: float
    command_id: str = field(default_factory=lambda: str(uuid4()))
    state: CommandState = CommandState.QUEUED
    queued_monotonic_ns: int = field(default_factory=time.perf_counter_ns)
    sent_monotonic_ns: int = 0
    acknowledged_monotonic_ns: int = 0
    completed_monotonic_ns: int = 0
    response_lines: list[str] = field(default_factory=list)
    error: str = ""
    _ack: Event = field(default_factory=Event, repr=False)
    _done: Event = field(default_factory=Event, repr=False)

    @property
    def succeeded(self) -> bool:
        return self.state is CommandState.COMPLETED

    def snapshot(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "gcode": self.gcode,
            "timeout_s": self.timeout_s,
            "state": self.state.value,
            "queued_monotonic_ns": self.queued_monotonic_ns,
            "sent_monotonic_ns": self.sent_monotonic_ns,
            "acknowledged_monotonic_ns": self.acknowledged_monotonic_ns,
            "completed_monotonic_ns": self.completed_monotonic_ns,
            "response_lines": list(self.response_lines),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PrinterPosition:
    tracked_x_mm: float = math.nan
    tracked_y_mm: float = math.nan
    tracked_z_mm: float = math.nan
    reported_x_mm: float = math.nan
    reported_y_mm: float = math.nan
    reported_z_mm: float = math.nan
    tracked_valid: bool = False
    reported_valid: bool = False
    homed: bool = False
    press_zero_x_mm: float = math.nan
    press_zero_y_mm: float = math.nan
    press_zero_z_mm: float = math.nan
    press_zero_valid: bool = False


@dataclass(frozen=True, slots=True)
class PrinterConnectionInfo:
    port: str
    baud_rate: int
    firmware_name: str
    firmware_version: str
    marlin_verified: bool
    connected_monotonic_ns: int


def enumerate_printer_ports() -> tuple[dict[str, object], ...]:
    """Enumerate serial ports without opening any device."""

    rows = []
    for item in list_ports.comports():
        rows.append(
            {
                "device": str(item.device),
                "description": str(item.description or ""),
                "manufacturer": str(item.manufacturer or ""),
                "vid": item.vid,
                "pid": item.pid,
                "serial_number": str(item.serial_number or ""),
            }
        )
    return tuple(sorted(rows, key=lambda row: str(row["device"]).casefold()))


_POSITION_RE = re.compile(
    r"(?:^|\s)X:\s*(-?\d+(?:\.\d+)?)\s+Y:\s*(-?\d+(?:\.\d+)?)\s+Z:\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_TEMP_RE = re.compile(r"(?:^|\s)(T\d*|B):\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


class PrinterSerialService:
    """Own one pyserial handle and route every write through one writer thread."""

    def __init__(
        self,
        *,
        serial_factory: Callable[..., object] | None = None,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._serial_factory = serial_factory or serial.Serial
        self._monotonic_ns = monotonic_ns
        self._serial: object | None = None
        self._config: PrinterConfig | None = None
        self._connection: PrinterConnectionInfo | None = None
        self._queue: Queue[PrinterCommand | None] = Queue()
        self._stop = Event()
        self._emergency_requested = Event()
        self._emergency_written = Event()
        self._emergency_latched = Event()
        self._quick_stop_requested = Event()
        self._quick_stop_written = Event()
        self._quick_stop_ack_pending = Event()
        self._quick_stop_acknowledged = Event()
        self._reader: Thread | None = None
        self._writer: Thread | None = None
        self._active: PrinterCommand | None = None
        self._lock = RLock()
        self._write_owner_ident: int | None = None
        self._motion_interlock_reason = ""
        self._position = PrinterPosition()
        self._temperatures: dict[str, float] = {}
        self._command_history: list[dict[str, Any]] = []
        self._control_history: list[dict[str, Any]] = []
        self._log_callbacks: list[Callable[[Mapping[str, Any]], None]] = []
        self._state_callbacks: list[Callable[[Mapping[str, Any]], None]] = []

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._serial is not None and self._connection is not None

    @property
    def marlin_verified(self) -> bool:
        with self._lock:
            return bool(self._connection and self._connection.marlin_verified)

    @property
    def connection_info(self) -> PrinterConnectionInfo | None:
        with self._lock:
            return self._connection

    @property
    def position(self) -> PrinterPosition:
        with self._lock:
            return self._position

    def add_log_callback(self, callback: Callable[[Mapping[str, Any]], None]) -> None:
        with self._lock:
            self._log_callbacks.append(callback)

    def add_state_callback(self, callback: Callable[[Mapping[str, Any]], None]) -> None:
        with self._lock:
            self._state_callbacks.append(callback)

    def connect(self, config: PrinterConfig, *, port: str | None = None) -> PrinterConnectionInfo:
        """Open only the selected printer port and verify it with read-only M115."""

        self.disconnect()
        selected_port = str(port or config.port).strip()
        if not selected_port:
            raise PrinterConnectionError("printer port must not be blank")
        try:
            transport = self._serial_factory(
                port=selected_port,
                baudrate=config.baud_rate,
                timeout=config.read_timeout_s,
                write_timeout=config.command_timeout_s,
            )
            reset = getattr(transport, "reset_input_buffer", None)
            if callable(reset):
                reset()
        except Exception as exc:
            raise PrinterConnectionError(
                f"could not open printer port {selected_port}: {exc}"
            ) from exc
        with self._lock:
            self._serial = transport
            self._config = config
            self._connection = PrinterConnectionInfo(
                port=selected_port,
                baud_rate=config.baud_rate,
                firmware_name="",
                firmware_version="",
                marlin_verified=False,
                connected_monotonic_ns=self._monotonic_ns(),
            )
            self._position = PrinterPosition()
            self._motion_interlock_reason = ""
            self._temperatures.clear()
            self._command_history.clear()
            self._control_history.clear()
            self._stop.clear()
            self._emergency_requested.clear()
            self._emergency_written.clear()
            self._emergency_latched.clear()
            self._quick_stop_requested.clear()
            self._quick_stop_written.clear()
            self._quick_stop_ack_pending.clear()
            self._quick_stop_acknowledged.clear()
        self._reader = Thread(target=self._reader_loop, name="printer-reader", daemon=True)
        self._writer = Thread(target=self._writer_loop, name="printer-writer", daemon=True)
        self._reader.start()
        self._writer.start()
        self._emit_log("info", f"Opened printer transport {selected_port}; verifying firmware with M115.")
        try:
            if config.startup_delay_s > 0:
                time.sleep(config.startup_delay_s)
            response = self.execute("M115", timeout_s=config.command_timeout_s)
            joined = "\n".join(response.response_lines)
            if "marlin" not in joined.casefold():
                raise PrinterConnectionError(
                    "M115 did not identify Marlin firmware; motion remains disabled"
                )
            firmware_name, firmware_version = self._parse_firmware(joined)
            with self._lock:
                assert self._connection is not None
                self._connection = PrinterConnectionInfo(
                    port=selected_port,
                    baud_rate=config.baud_rate,
                    firmware_name=firmware_name,
                    firmware_version=firmware_version,
                    marlin_verified=True,
                    connected_monotonic_ns=self._connection.connected_monotonic_ns,
                )
            self._emit_state()
            return self.connection_info  # type: ignore[return-value]
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        with self._lock:
            transport = self._serial
            had_connection = transport is not None
            self._connection = None
            self._position = PrinterPosition()
            self._motion_interlock_reason = ""
            self._stop.set()
            active = self._active
            if active is not None and active.state not in TERMINAL_COMMAND_STATES:
                active.error = "printer disconnected"
                active.state = CommandState.CANCELLED
                active._done.set()
        self._queue.put(None)
        for worker in (self._writer, self._reader):
            if worker is not None and worker.is_alive():
                worker.join(timeout=1.0)
        self._cancel_queued("printer disconnected")
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                self._emit_log("error", f"Printer serial close failed: {exc}")
        with self._lock:
            self._serial = None
            self._config = None
            self._reader = None
            self._writer = None
            self._active = None
        if had_connection:
            self._emit_log("info", "Printer disconnected; homing, position, and press zero invalidated.")
            self._emit_state()

    def submit(self, gcode: str, *, timeout_s: float | None = None) -> PrinterCommand:
        raw_command = str(gcode)
        command_text = raw_command.strip()
        if not command_text or "\n" in raw_command or "\r" in raw_command:
            raise ValueError("G-code must be one nonblank line")
        with self._lock:
            config = self._config
            connected = self._serial is not None and self._connection is not None
        if not connected or config is None:
            raise PrinterConnectionError("printer is not connected")
        if self._emergency_latched.is_set() or self._emergency_requested.is_set():
            raise PrinterSafetyError(
                "printer is in emergency-stop state; reconnect before sending commands"
            )
        timeout = config.command_timeout_s if timeout_s is None else float(timeout_s)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("command timeout must be finite and positive")
        command = PrinterCommand(command_text, timeout)
        self._emit_command_state(command)
        self._queue.put(command)
        return command

    def execute(self, gcode: str, *, timeout_s: float | None = None) -> PrinterCommand:
        command = self.submit(gcode, timeout_s=timeout_s)
        return self.wait_command(command)

    def wait_command(self, command: PrinterCommand) -> PrinterCommand:
        """Wait outside the GUI thread for one already-submitted command."""

        wait_timeout = command.timeout_s + 1.0
        if not command._done.wait(wait_timeout):
            raise PrinterCommandError(
                f"command completion signal timed out: {command.gcode}"
            )
        if not command.succeeded:
            raise PrinterCommandError(
                f"{command.gcode} {command.state.value}: {command.error or 'printer did not complete command'}"
            )
        return command

    def query_position(self) -> PrinterPosition:
        self._require_motion_ready(require_homed=False)
        command = self.execute("M114")
        position = self._position_from_lines(command.response_lines)
        if position is None:
            self._invalidate_position("M114 response contained no parseable XYZ coordinates")
            raise PrinterCommandError("M114 response contained no parseable XYZ coordinates")
        with self._lock:
            old = self._position
            self._position = PrinterPosition(
                tracked_x_mm=position[0],
                tracked_y_mm=position[1],
                tracked_z_mm=position[2],
                reported_x_mm=position[0],
                reported_y_mm=position[1],
                reported_z_mm=position[2],
                tracked_valid=True,
                reported_valid=True,
                homed=old.homed,
                press_zero_x_mm=old.press_zero_x_mm,
                press_zero_y_mm=old.press_zero_y_mm,
                press_zero_z_mm=old.press_zero_z_mm,
                press_zero_valid=old.press_zero_valid,
            )
        self._emit_state()
        return self.position

    def home_all(self) -> PrinterPosition:
        """Home only after an explicit operator action; never called by connect."""

        self._require_motion_ready(require_homed=False)
        self.invalidate_press_zero("homing started")
        with self._lock:
            self._position = PrinterPosition()
        self._emit_state()
        self._emit_log(
            "info",
            "Homing started. The Ender 3 main PSU must be on; USB power alone cannot drive the axes.",
        )
        try:
            self.execute("G28", timeout_s=max(120.0, self._require_config().command_timeout_s))
            self.execute("M400", timeout_s=max(120.0, self._require_config().command_timeout_s))
        except PrinterCommandError as exc:
            raise PrinterCommandError(
                f"{exc}. Homing did not complete; check main printer power and endstops, "
                "then power-cycle/reconnect before retrying."
            ) from exc
        with self._lock:
            old = self._position
            self._position = PrinterPosition(
                tracked_x_mm=old.tracked_x_mm,
                tracked_y_mm=old.tracked_y_mm,
                tracked_z_mm=old.tracked_z_mm,
                reported_x_mm=old.reported_x_mm,
                reported_y_mm=old.reported_y_mm,
                reported_z_mm=old.reported_z_mm,
                tracked_valid=old.tracked_valid,
                reported_valid=old.reported_valid,
                homed=True,
            )
        position = self.query_position()
        self._emit_log(
            "info",
            f"Homing complete; M114 reported X={position.reported_x_mm:.3f}, "
            f"Y={position.reported_y_mm:.3f}, Z={position.reported_z_mm:.3f} mm.",
        )
        return position

    def set_press_zero_here(self) -> PrinterPosition:
        self._require_motion_ready(require_homed=True)
        position = self.position
        coordinates = {
            "X": position.tracked_x_mm,
            "Y": position.tracked_y_mm,
            "Z": position.tracked_z_mm,
        }
        if not position.tracked_valid or not all(math.isfinite(value) for value in coordinates.values()):
            raise PrinterSafetyError("a valid M114/tracked XYZ position is required for press zero")
        config = self._require_config()
        limits = {
            "X": (config.x_min_mm, config.x_max_mm),
            "Y": (config.y_min_mm, config.y_max_mm),
            "Z": (config.z_min_mm, config.z_max_mm),
        }
        outside = [
            f"{axis}={coordinates[axis]:g} (allowed {low:g}..{high:g})"
            for axis, (low, high) in limits.items()
            if not low <= coordinates[axis] <= high
        ]
        if outside:
            raise PrinterSafetyError(
                "current position is outside configured press-zero limits: " + "; ".join(outside)
            )
        with self._lock:
            self._position = PrinterPosition(
                **{
                    **asdict(position),
                    "press_zero_x_mm": position.tracked_x_mm,
                    "press_zero_y_mm": position.tracked_y_mm,
                    "press_zero_z_mm": position.tracked_z_mm,
                    "press_zero_valid": True,
                }
            )
        self._emit_log(
            "info",
            "Mechanical Press Zero set at machine "
            f"X={position.tracked_x_mm:.3f}, Y={position.tracked_y_mm:.3f}, "
            f"Z={position.tracked_z_mm:.3f} mm; no G92 sent.",
        )
        self._emit_state()
        return self.position

    def invalidate_press_zero(self, reason: str) -> None:
        with self._lock:
            old = self._position
            self._position = PrinterPosition(
                **{
                    **asdict(old),
                    "press_zero_x_mm": math.nan,
                    "press_zero_y_mm": math.nan,
                    "press_zero_z_mm": math.nan,
                    "press_zero_valid": False,
                }
            )
        self._emit_log("warning", f"Press zero invalidated: {reason}.")
        self._emit_state()

    def move_absolute(
        self,
        *,
        x_mm: float | None = None,
        y_mm: float | None = None,
        z_mm: float | None = None,
        feed_rate_mm_min: float,
        require_homed: bool = True,
        query_after: bool = True,
        motion_submitted_callback: Callable[[PrinterCommand], None] | None = None,
    ) -> PrinterCommand:
        self._require_motion_ready(require_homed=require_homed)
        config = self._require_config()
        feed = float(feed_rate_mm_min)
        if not math.isfinite(feed) or feed <= 0:
            raise PrinterSafetyError("feed rate must be finite and positive")
        requested = {"X": x_mm, "Y": y_mm, "Z": z_mm}
        if all(value is None for value in requested.values()):
            raise ValueError("at least one axis target is required")
        limits = {
            "X": (config.x_min_mm, config.x_max_mm),
            "Y": (config.y_min_mm, config.y_max_mm),
            "Z": (config.z_min_mm, config.z_max_mm),
        }
        current = self.position
        current_values = {
            "X": current.tracked_x_mm,
            "Y": current.tracked_y_mm,
            "Z": current.tracked_z_mm,
        }
        for axis, value in requested.items():
            if value is None:
                continue
            numeric = float(value)
            low, high = limits[axis]
            current_value = current_values[axis]
            returning_from_below = (
                math.isfinite(current_value)
                and current_value < low
                and current_value < numeric <= high
            )
            returning_from_above = (
                math.isfinite(current_value)
                and current_value > high
                and low <= numeric < current_value
            )
            if not math.isfinite(numeric) or not (
                low <= numeric <= high or returning_from_below or returning_from_above
            ):
                raise PrinterSafetyError(
                    f"{axis} target {value!r} is outside configured limits {low:g}..{high:g} mm"
                )
            if returning_from_below or returning_from_above:
                self._emit_log(
                    "warning",
                    f"Allowing {axis} recovery jog from {current_value:.3f} toward configured range "
                    f"{low:g}..{high:g} mm; movement farther out remains blocked.",
                )
        self.execute("G90")
        axes = " ".join(
            f"{axis}{float(value):.3f}" for axis, value in requested.items() if value is not None
        )
        motion = self.submit(f"G1 {axes} F{feed:.3f}", timeout_s=max(60.0, config.command_timeout_s))
        if motion_submitted_callback is not None:
            motion_submitted_callback(motion)
        self.wait_command(motion)
        self.execute("M400", timeout_s=max(120.0, config.command_timeout_s))
        with self._lock:
            old = self._position
            self._position = PrinterPosition(
                tracked_x_mm=old.tracked_x_mm if x_mm is None else float(x_mm),
                tracked_y_mm=old.tracked_y_mm if y_mm is None else float(y_mm),
                tracked_z_mm=old.tracked_z_mm if z_mm is None else float(z_mm),
                reported_x_mm=old.reported_x_mm,
                reported_y_mm=old.reported_y_mm,
                reported_z_mm=old.reported_z_mm,
                tracked_valid=True,
                reported_valid=old.reported_valid,
                homed=old.homed,
                press_zero_x_mm=old.press_zero_x_mm,
                press_zero_y_mm=old.press_zero_y_mm,
                press_zero_z_mm=old.press_zero_z_mm,
                press_zero_valid=old.press_zero_valid,
            )
        self._emit_state()
        if query_after:
            self.query_position()
        return motion

    def jog(self, axis: str, delta_mm: float, *, feed_rate_mm_min: float) -> PrinterCommand:
        normalized = str(axis).upper()
        if normalized not in {"X", "Y", "Z"}:
            raise ValueError("jog axis must be X, Y, or Z")
        self._require_motion_ready(require_homed=True)
        position = self.position
        if not position.tracked_valid:
            raise PrinterSafetyError("valid tracked position is required before jogging")
        targets = {
            "X": position.tracked_x_mm,
            "Y": position.tracked_y_mm,
            "Z": position.tracked_z_mm,
        }
        targets[normalized] += float(delta_mm)
        return self.move_absolute(
            x_mm=targets["X"] if normalized == "X" else None,
            y_mm=targets["Y"] if normalized == "Y" else None,
            z_mm=targets["Z"] if normalized == "Z" else None,
            feed_rate_mm_min=feed_rate_mm_min,
        )

    def emergency_stop(self, *, wait_timeout_s: float = 1.0) -> bool:
        """Ask the single writer to send M112 ahead of any normal queued work."""

        if not self.connected:
            return False
        self._emergency_written.clear()
        self._emergency_requested.set()
        self._queue.put(None)
        sent = self._emergency_written.wait(max(0.01, float(wait_timeout_s)))
        self._invalidate_position("emergency stop")
        self._cancel_queued("cancelled by emergency stop")
        return sent

    def quick_stop(self, *, wait_timeout_s: float = 1.0) -> bool:
        """Request Marlin M410 through the sole writer to interrupt a normal move."""

        if not self.request_quick_stop():
            return False
        return self._quick_stop_written.wait(max(0.01, float(wait_timeout_s)))

    def request_quick_stop(self) -> bool:
        """Wake the sole writer for M410 without blocking an acquisition callback."""

        if (
            not self.connected
            or self._emergency_requested.is_set()
            or self._emergency_latched.is_set()
        ):
            return False
        self._quick_stop_written.clear()
        self._quick_stop_requested.set()
        self._queue.put(None)
        return True

    def cancel_pending(self, reason: str = "cancelled by operator") -> None:
        self._cancel_queued(reason)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            history = list(self._command_history) + list(self._control_history)
            history.sort(
                key=lambda row: int(
                    row.get("sent_monotonic_ns")
                    or row.get("queued_monotonic_ns")
                    or 0
                )
            )
            return {
                "connected": self.connected,
                "marlin_verified": self.marlin_verified,
                "connection": asdict(self._connection) if self._connection else None,
                "position": asdict(self._position),
                "temperatures_C": dict(self._temperatures),
                "queue_depth": self._queue.qsize(),
                "active_command_id": self._active.command_id if self._active else "",
                "motion_interlock_reason": self._motion_interlock_reason,
                "command_history": history,
                "writer_thread_ident": self._write_owner_ident,
            }

    def _require_config(self) -> PrinterConfig:
        with self._lock:
            if self._config is None:
                raise PrinterConnectionError("printer is not connected")
            return self._config

    def _require_motion_ready(self, *, require_homed: bool) -> None:
        if not self.connected or not self.marlin_verified:
            raise PrinterSafetyError("verified Marlin connection is required for motion")
        if self._emergency_requested.is_set() or self._emergency_latched.is_set():
            raise PrinterSafetyError("printer is in emergency-stop state; reconnect before motion")
        with self._lock:
            interlock_reason = self._motion_interlock_reason
        if interlock_reason:
            raise PrinterSafetyError(
                f"motion is interlocked after a timed-out move ({interlock_reason}); "
                "power-cycle/reconnect and rehome before motion"
            )
        if require_homed and not self.position.homed:
            raise PrinterSafetyError("all axes must be homed by explicit operator action")

    def _writer_loop(self) -> None:
        import threading

        self._write_owner_ident = threading.get_ident()
        while not self._stop.is_set():
            if self._emergency_requested.is_set():
                self._write_emergency()
                continue
            if self._quick_stop_requested.is_set():
                self._write_quick_stop()
                continue
            try:
                command = self._queue.get(timeout=0.05)
            except Empty:
                continue
            if command is None:
                self._queue.task_done()
                continue
            if self._emergency_latched.is_set():
                command.state = CommandState.CANCELLED
                command.error = "cancelled by emergency stop"
                command.completed_monotonic_ns = self._monotonic_ns()
                command._done.set()
                with self._lock:
                    self._command_history.append(command.snapshot())
                self._emit_command_state(command)
                self._queue.task_done()
                continue
            with self._lock:
                self._active = command
            try:
                command.state = CommandState.SENT
                command.sent_monotonic_ns = self._monotonic_ns()
                self._emit_command_state(command)
                self._write_line(command.gcode)
                deadline_ns = command.sent_monotonic_ns + round(command.timeout_s * 1e9)
                while not command._ack.is_set() and not self._stop.is_set():
                    if self._emergency_requested.is_set():
                        self._write_emergency()
                        command.state = CommandState.CANCELLED
                        command.error = "interrupted by emergency stop"
                        break
                    if self._quick_stop_requested.is_set():
                        self._write_quick_stop()
                        command.state = CommandState.CANCELLED
                        command.error = "interrupted by M410 quick stop"
                        break
                    if self._monotonic_ns() >= deadline_ns:
                        command.state = CommandState.TIMED_OUT
                        command.error = f"no ok response within {command.timeout_s:g} s"
                        self._invalidate_position(command.error)
                        if self._is_motion_command(command.gcode):
                            with self._lock:
                                self._motion_interlock_reason = f"{command.gcode}: {command.error}"
                            self._quick_stop_requested.set()
                            self._write_quick_stop()
                            self._emit_log(
                                "error",
                                "Motion command timed out; M410 quick stop was requested and further "
                                "motion is blocked until a deliberate power-cycle/reconnect and rehome.",
                            )
                        break
                    time.sleep(self._require_config().emergency_poll_interval_s)
                if command.state is CommandState.ACKNOWLEDGED:
                    command.state = CommandState.COMPLETED
                elif command.state not in TERMINAL_COMMAND_STATES:
                    command.state = CommandState.CANCELLED
                    command.error = command.error or "writer stopped"
            except Exception as exc:
                command.state = CommandState.FAILED
                command.error = str(exc)
                self._mark_transport_failed(f"printer communication failure: {exc}")
            finally:
                command.completed_monotonic_ns = self._monotonic_ns()
                command._done.set()
                with self._lock:
                    self._command_history.append(command.snapshot())
                    self._active = None
                self._emit_command_state(command)
                self._queue.task_done()

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                transport = self._serial
            if transport is None:
                return
            try:
                raw = transport.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else str(raw).strip()
                if not line:
                    continue
                self._emit_log("receive", line)
                self._parse_async_line(line)
                lowered = line.casefold()
                if self._quick_stop_ack_pending.is_set() and (
                    lowered == "ok"
                    or lowered.startswith("ok ")
                    or lowered.startswith("error")
                    or lowered.startswith("!!")
                ):
                    self._quick_stop_acknowledged.set()
                    continue
                with self._lock:
                    command = self._active
                if command is None:
                    continue
                command.response_lines.append(line)
                if (
                    command.state is CommandState.SENT
                    and (lowered.startswith("error") or lowered.startswith("!!"))
                ):
                    command.state = CommandState.REJECTED
                    command.error = line
                    command._ack.set()
                elif command.state is CommandState.SENT and (
                    lowered == "ok" or lowered.startswith("ok ")
                ):
                    command.state = CommandState.ACKNOWLEDGED
                    command.acknowledged_monotonic_ns = self._monotonic_ns()
                    command._ack.set()
            except Exception as exc:
                if not self._stop.is_set():
                    self._emit_log("error", f"Printer read failed: {exc}")
                    self._mark_transport_failed(f"printer read failure: {exc}")
                return

    def _write_line(self, line: str) -> None:
        with self._lock:
            transport = self._serial
        if transport is None:
            raise PrinterConnectionError("printer transport is closed")
        payload = f"{line}\n".encode("ascii", errors="strict")
        transport.write(payload)
        flush = getattr(transport, "flush", None)
        if callable(flush):
            flush()
        self._emit_log("send", line)

    def _write_emergency(self) -> None:
        if not self._emergency_requested.is_set() or self._emergency_written.is_set():
            return
        try:
            self._write_line("M112")
            with self._lock:
                self._control_history.append(
                    {
                        "command_id": f"emergency-{uuid4()}",
                        "gcode": "M112",
                        "state": "sent",
                        "sent_monotonic_ns": self._monotonic_ns(),
                        "control_path": "emergency",
                    }
                )
            self._emit_log("emergency", "M112 emergency stop sent by the sole printer writer.")
        finally:
            self._emergency_requested.clear()
            self._emergency_latched.set()
            self._emergency_written.set()

    def _write_quick_stop(self) -> None:
        if not self._quick_stop_requested.is_set():
            return
        try:
            self._quick_stop_acknowledged.clear()
            self._quick_stop_ack_pending.set()
            self._write_line("M410")
            with self._lock:
                self._control_history.append(
                    {
                        "command_id": f"quick-stop-{uuid4()}",
                        "gcode": "M410",
                        "state": "sent",
                        "sent_monotonic_ns": self._monotonic_ns(),
                        "control_path": "quick_stop",
                    }
                )
            self._emit_log("warning", "M410 quick stop sent by the sole printer writer.")
            if not self._quick_stop_acknowledged.wait(0.25):
                self._emit_log(
                    "warning",
                    "M410 was written but no dedicated acknowledgement arrived within 250 ms.",
                )
        finally:
            self._quick_stop_ack_pending.clear()
            self._quick_stop_requested.clear()
            self._quick_stop_written.set()

    def _cancel_queued(self, reason: str) -> None:
        while True:
            try:
                command = self._queue.get_nowait()
            except Empty:
                break
            try:
                if command is not None:
                    command.state = CommandState.CANCELLED
                    command.error = str(reason)
                    command.completed_monotonic_ns = self._monotonic_ns()
                    command._done.set()
                    with self._lock:
                        self._command_history.append(command.snapshot())
                    self._emit_command_state(command)
            finally:
                self._queue.task_done()

    def _parse_async_line(self, line: str) -> None:
        for name, value in _TEMP_RE.findall(line):
            with self._lock:
                self._temperatures[name.upper()] = float(value)

    @staticmethod
    def _is_motion_command(gcode: str) -> bool:
        root = str(gcode).strip().split(maxsplit=1)[0].upper()
        return root in {"G0", "G1", "G2", "G3", "G28", "G29", "G30"}

    def _invalidate_position(self, reason: str) -> None:
        with self._lock:
            old = self._position
            self._position = PrinterPosition(
                reported_x_mm=old.reported_x_mm,
                reported_y_mm=old.reported_y_mm,
                reported_z_mm=old.reported_z_mm,
                reported_valid=False,
            )
        self._emit_log("warning", f"Position, homing, and press zero invalidated: {reason}.")
        self._emit_state()

    def _mark_transport_failed(self, reason: str) -> None:
        with self._lock:
            transport = self._serial
            self._connection = None
            self._stop.set()
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        self._invalidate_position(reason)

    @staticmethod
    def _position_from_lines(lines: list[str]) -> tuple[float, float, float] | None:
        for line in reversed(lines):
            match = _POSITION_RE.search(line)
            if match:
                return tuple(float(value) for value in match.groups())  # type: ignore[return-value]
        return None

    @staticmethod
    def _parse_firmware(text: str) -> tuple[str, str]:
        name_match = re.search(r"FIRMWARE_NAME:([^\n]+?)(?:\s+SOURCE_CODE_URL:|$)", text, re.IGNORECASE)
        firmware_name = name_match.group(1).strip() if name_match else "Marlin"
        version_match = re.search(r"(?:Marlin\s+|FIRMWARE_VERSION:)([0-9][^\s]*)", text, re.IGNORECASE)
        return firmware_name, version_match.group(1) if version_match else ""

    def _emit_log(self, level: str, message: str) -> None:
        payload = {
            "host_monotonic_ns": self._monotonic_ns(),
            "level": str(level),
            "message": str(message),
        }
        with self._lock:
            callbacks = tuple(self._log_callbacks)
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:
                pass

    def _emit_command_state(self, command: PrinterCommand) -> None:
        payload = {"type": "command", **command.snapshot()}
        with self._lock:
            callbacks = tuple(self._state_callbacks)
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:
                pass

    def _emit_state(self) -> None:
        payload = {
            "type": "printer",
            "connected": self.connected,
            "marlin_verified": self.marlin_verified,
            "connection": asdict(self.connection_info) if self.connection_info else None,
            "position": asdict(self.position),
        }
        with self._lock:
            callbacks = tuple(self._state_callbacks)
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:
                pass


__all__ = [
    "CommandState",
    "PrinterCommand",
    "PrinterCommandError",
    "PrinterConnectionError",
    "PrinterConnectionInfo",
    "PrinterError",
    "PrinterPosition",
    "PrinterSafetyError",
    "PrinterSerialService",
    "enumerate_printer_ports",
]
