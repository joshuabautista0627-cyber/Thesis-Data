"""Single-owner pyserial service for protocol and plain HX711 streams.

The class in this module is the only physical serial-port owner.  It performs a
The richer supplied firmware protocol remains supported, but a ``HELLO`` banner
is never required for a continuously transmitting numeric HX711 stream.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from collections import deque
import csv
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import statistics
import time
from typing import Any, Protocol
import uuid

import serial
from serial.tools import list_ports

from core.models import (
    ErrorCode,
    LoadCellCalibration,
    LoadCellConfig,
    LoadCellSample,
)
from data.schemas import LOADCELL_RAW_COLUMNS
from processing.synchronization import ArduinoClockEvent, ArduinoMicrosTracker
from services.interfaces import SerialConnectionInfo
from services.serial_parser import (
    ParsedSensorLine,
    SensorLineParseError,
    parse_sensor_line,
)


ARDUINO_UINT32_MAX = 0xFFFFFFFF
SIGNED_LONG_MIN = -(2**31)
SIGNED_LONG_MAX = 2**31 - 1
VALID_COMMANDS = ("PING", "START", "STOP", "STATUS")
DEVICE_NAME = "HX711_NANO"
DEFAULT_READ_TIMEOUT_S = 0.05
DEFAULT_COMMAND_TIMEOUT_S = 1.0
DEFAULT_POLL_INTERVAL_S = 0.005
DEFAULT_MAX_LINE_BYTES = 256
_INTEGER_PATTERN = re.compile(r"[+-]?\d+")


class SerialServiceError(RuntimeError):
    """Base error for the physical serial boundary."""


class SerialHandshakeError(SerialServiceError):
    """The port opened but did not provide a valid versioned HELLO."""


class SerialDisconnectedError(SerialServiceError):
    """The owned serial stream closed or failed during use."""


class SerialProtocolError(SerialServiceError, ValueError):
    """A protocol line is malformed or violates the firmware contract."""


class SerialCommandError(SerialServiceError):
    """A command could not be written or did not receive its response."""


@dataclass(frozen=True, slots=True)
class SerialPortDescriptor:
    """Stable, GUI-friendly description of one enumerated serial port."""

    device: str
    description: str
    hwid: str
    manufacturer: str = ""
    product: str = ""
    serial_number: str = ""


@dataclass(frozen=True, slots=True)
class HelloIdentity:
    """Validated ``HELLO,HX711_NANO,protocol,firmware`` response."""

    device_name: str
    protocol_version: str
    firmware_version: str


@dataclass(frozen=True, slots=True)
class ParsedDataLine:
    """Validated raw fields from one new physical HX711 conversion."""

    sample_id: int
    arduino_micros: int
    raw_adc: float


@dataclass(frozen=True, slots=True)
class _ReceivedLine:
    text: str
    receipt_monotonic_ns: int
    receipt_wall_clock_iso: str
    sequence: int = 0


class _SerialLike(Protocol):
    is_open: bool
    timeout: float | None

    def readline(self) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
    def reset_input_buffer(self) -> None: ...
    def reset_output_buffer(self) -> None: ...


def _protocol_text(line: str | bytes) -> str:
    if isinstance(line, bytes):
        try:
            text = line.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise SerialProtocolError("protocol line is not ASCII") from exc
    elif isinstance(line, str):
        text = line
    else:
        raise TypeError("protocol line must be str or bytes")
    normalized = text.strip("\r\n")
    if not normalized or "\x00" in normalized:
        raise SerialProtocolError("protocol line is empty or contains NUL")
    return normalized


def _decimal_integer(
    value: str,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    token = value.strip()
    if _INTEGER_PATTERN.fullmatch(token) is None:
        raise SerialProtocolError(f"{name} must be a decimal integer")
    result = int(token, 10)
    if not minimum <= result <= maximum:
        raise SerialProtocolError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return result


def parse_hello_line(line: str | bytes) -> HelloIdentity:
    """Parse and strictly validate the firmware identity line."""

    fields = _protocol_text(line).split(",")
    if len(fields) != 4 or fields[0] != "HELLO" or fields[1] != DEVICE_NAME:
        raise SerialProtocolError(
            "expected HELLO,HX711_NANO,protocol_version,firmware_version"
        )
    protocol_version = fields[2].strip()
    firmware_version = fields[3].strip()
    if not protocol_version or not firmware_version:
        raise SerialProtocolError("HELLO protocol and firmware versions must not be blank")
    return HelloIdentity(
        device_name=DEVICE_NAME,
        protocol_version=protocol_version,
        firmware_version=firmware_version,
    )


def parse_data_line(line: str | bytes) -> ParsedDataLine:
    """Parse ``DATA,sample_id,arduino_micros,raw_adc`` without coercion."""

    fields = _protocol_text(line).split(",")
    if len(fields) != 4 or fields[0] != "DATA":
        raise SerialProtocolError(
            "expected DATA,sample_id,arduino_micros,raw_adc"
        )
    return ParsedDataLine(
        sample_id=_decimal_integer(
            fields[1],
            "sample_id",
            minimum=0,
            maximum=ARDUINO_UINT32_MAX,
        ),
        arduino_micros=_decimal_integer(
            fields[2],
            "arduino_micros",
            minimum=0,
            maximum=ARDUINO_UINT32_MAX,
        ),
        raw_adc=_decimal_integer(
            fields[3],
            "raw_adc",
            minimum=SIGNED_LONG_MIN,
            maximum=SIGNED_LONG_MAX,
        ),
    )


def enumerate_serial_ports(
    port_enumerator: Callable[[], Iterable[Any]] | None = None,
) -> tuple[SerialPortDescriptor, ...]:
    """Enumerate ports without opening them, sorted by device name."""

    enumerate_now = list_ports.comports if port_enumerator is None else port_enumerator
    descriptors: list[SerialPortDescriptor] = []
    for item in enumerate_now():
        device = str(getattr(item, "device", "")).strip()
        if not device:
            continue
        descriptors.append(
            SerialPortDescriptor(
                device=device,
                description=str(getattr(item, "description", "") or ""),
                hwid=str(getattr(item, "hwid", "") or ""),
                manufacturer=str(getattr(item, "manufacturer", "") or ""),
                product=str(getattr(item, "product", "") or ""),
                serial_number=str(getattr(item, "serial_number", "") or ""),
            )
        )
    return tuple(sorted(descriptors, key=lambda item: item.device.casefold()))


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number, not bool")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _aware_wall_clock_iso(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("wall_clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("wall_clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class SerialService:
    """Sole pyserial owner for a physical Arduino Nano/HX711 stream."""

    def __init__(
        self,
        calibration: LoadCellCalibration | None = None,
        *,
        serial_factory: Callable[..., _SerialLike] | None = None,
        port_enumerator: Callable[[], Iterable[Any]] | None = None,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleeper: Callable[[float], None] = time.sleep,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        device_session_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._serial_factory = serial.Serial if serial_factory is None else serial_factory
        self._port_enumerator = (
            list_ports.comports if port_enumerator is None else port_enumerator
        )
        self._monotonic_ns = monotonic_ns
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._read_timeout_s = _positive_finite(read_timeout_s, "read_timeout_s")
        self._command_timeout_s = _positive_finite(
            command_timeout_s, "command_timeout_s"
        )
        self._poll_interval_s = _positive_finite(
            poll_interval_s, "poll_interval_s"
        )
        if isinstance(max_line_bytes, bool) or max_line_bytes < 32:
            raise ValueError("max_line_bytes must be an integer of at least 32")
        self._max_line_bytes = int(max_line_bytes)
        self._device_session_token_factory = (
            (lambda: uuid.uuid4().hex)
            if device_session_token_factory is None
            else device_session_token_factory
        )
        self._calibration = calibration
        self._serial: _SerialLike | None = None
        self._config: LoadCellConfig | None = None
        self._connection_info: SerialConnectionInfo | None = None
        self._connected = False
        self._streaming = False
        self._port = ""
        self._tracker = ArduinoMicrosTracker()
        self._device_session_sequence = 0
        self._current_device_session_id = ""
        self._elapsed_origin_ns = 0
        self._pending_data_lines: list[_ReceivedLine] = []
        self._protocol_mode = False
        self._generic_sample_id = 0
        self._input_format = "Auto-detect"
        self._last_valid_receipt_ns: int | None = None
        self._raw_log_entries: deque[dict[str, object]] = deque(maxlen=2000)
        self._raw_line_sequence = 0
        self._reset_diagnostics()

    @property
    def is_connected(self) -> bool:
        return self._connected and self._serial_is_open()

    @property
    def simulation_mode(self) -> bool:
        return False

    @property
    def connection_info(self) -> SerialConnectionInfo:
        if self._connection_info is None:
            raise SerialDisconnectedError("serial service has no opened sensor stream")
        return self._connection_info

    @property
    def calibration(self) -> LoadCellCalibration | None:
        return self._calibration

    def set_calibration(self, calibration: LoadCellCalibration | None) -> None:
        """Set or clear the signed calibration used for subsequent samples."""

        self._calibration = calibration

    def enumerate_ports(self) -> tuple[SerialPortDescriptor, ...]:
        return enumerate_serial_ports(self._port_enumerator)

    # GUI-friendly aliases.
    list_ports = enumerate_ports
    refresh_ports = enumerate_ports

    def connect(
        self, config: LoadCellConfig, *, port: str | None = None
    ) -> SerialConnectionInfo:
        """Open one port and recognize either protocol identity or sensor data."""

        if not isinstance(config, LoadCellConfig):
            raise TypeError("config must be a LoadCellConfig")
        selected_port = "" if port is None else str(port).strip()
        if not selected_port:
            raise ValueError("a serial port must be selected")
        self.disconnect()
        self._reset_diagnostics()
        self._config = config
        self._port = selected_port
        self._input_format = str(config.input_format)
        try:
            serial_kwargs: dict[str, object] = {
                "port": selected_port,
                "baudrate": config.baud_rate,
                "timeout": self._read_timeout_s,
                "write_timeout": self._command_timeout_s,
            }
            if (
                int(config.data_bits),
                str(config.parity).upper(),
                float(config.stop_bits),
            ) != (8, "N", 1.0):
                serial_kwargs.update(
                    {
                        "bytesize": int(config.data_bits),
                        "parity": str(config.parity).upper(),
                        "stopbits": float(config.stop_bits),
                    }
                )
            try:
                handle = self._serial_factory(**serial_kwargs)
            except TypeError:
                # Small hardware-free test doubles may implement only the core
                # pyserial keyword subset.
                handle = self._serial_factory(
                    port=selected_port,
                    baudrate=config.baud_rate,
                    timeout=self._read_timeout_s,
                    write_timeout=self._command_timeout_s,
                )
            if handle is None:
                raise SerialDisconnectedError("serial factory returned no port handle")
            self._serial = handle
            if hasattr(handle, "is_open") and not bool(handle.is_open):
                opener = getattr(handle, "open", None)
                if not callable(opener):
                    raise SerialDisconnectedError("serial port did not open")
                opener()
            reset_input = getattr(handle, "reset_input_buffer", None)
            if callable(reset_input):
                reset_input()
            reset_output = getattr(handle, "reset_output_buffer", None)
            if callable(reset_output):
                reset_output()
            if config.startup_delay_s > 0:
                self._sleeper(float(config.startup_delay_s))
            self._elapsed_origin_ns = self._clock_ns()
            identity, protocol_mode = self._wait_for_sensor_or_hello(
                config.startup_window_s
            )
            self._tracker.reset()
            self._device_session_sequence = 0
            self._current_device_session_id = self._make_device_session_id()
            self._connection_info = SerialConnectionInfo(
                port=selected_port,
                baud_rate=config.baud_rate,
                device_name=identity.device_name,
                protocol_version=identity.protocol_version,
                firmware_version=identity.firmware_version,
                device_session_id=self._current_device_session_id,
                simulation_mode=False,
            )
            self._connected = True
            self._protocol_mode = bool(protocol_mode)
            # Only the supplied command protocol needs START.  Continuously
            # transmitting numeric firmware is never required to implement it.
            if self._protocol_mode:
                self._send_command("START", reset_elapsed_origin=False)
            else:
                self._streaming = True
            return self._connection_info
        except Exception as exc:
            self._close_handle()
            self._connected = False
            self._streaming = False
            self._connection_info = None
            if isinstance(exc, (serial.SerialException, PermissionError, OSError)):
                raise SerialDisconnectedError(
                    f"Could not open {selected_port}. The port may be in Arduino IDE "
                    "Serial Monitor/Plotter, another application, or another GUI instance: "
                    f"{exc}"
                ) from exc
            raise

    def disconnect(self) -> None:
        """Release the sole port handle idempotently."""

        self._close_handle()
        self._connected = False
        self._streaming = False
        self._config = None
        self._connection_info = None
        self._pending_data_lines.clear()
        self._protocol_mode = False
        self._generic_sample_id = 0
        self._current_device_session_id = ""
        self._tracker.reset()

    def set_elapsed_origin_ns(self, host_monotonic_ns: int) -> None:
        """Set the recording origin used for exported physical-sample elapsed time."""

        if isinstance(host_monotonic_ns, bool):
            raise TypeError("host_monotonic_ns must be an integer")
        origin = int(host_monotonic_ns)
        if origin < 0:
            raise ValueError("host_monotonic_ns must be nonnegative")
        self._elapsed_origin_ns = origin

    def send_command(self, command: str) -> str:
        """Send one supported command and await its bounded protocol response."""

        return self._send_command(command, reset_elapsed_origin=True)

    def _send_command(
        self, command: str, *, reset_elapsed_origin: bool
    ) -> str:
        """Execute a command, optionally preserving an active trial time origin."""

        self._require_connected()
        normalized = str(command).strip().upper()
        if normalized not in VALID_COMMANDS:
            raise ValueError("command must be PING, START, STOP, or STATUS")
        handle = self._require_handle()
        payload = f"{normalized}\n".encode("ascii")
        command_sent_ns = self._clock_ns()
        try:
            written = handle.write(payload)
            if written is not None and int(written) != len(payload):
                raise OSError(
                    f"serial write accepted {written} of {len(payload)} bytes"
                )
            flush = getattr(handle, "flush", None)
            if callable(flush):
                flush()
        except (OSError, serial.SerialException) as exc:
            self._mark_disconnected(f"serial command write failed: {exc}")
            raise SerialDisconnectedError(str(exc)) from exc

        deadline = self._clock_ns() + round(self._command_timeout_s * 1e9)
        attempts = 0
        while self._clock_ns() <= deadline and attempts < 4096:
            attempts += 1
            received = self._read_line_once(count_empty_timeout=False)
            if received is None:
                self._wait_until_next_poll(deadline)
                continue
            text = received.text
            if text.startswith("DATA,"):
                self._pending_data_lines.append(received)
                continue
            if text.startswith("HELLO,"):
                identity = parse_hello_line(text)
                self._update_identity(identity)
                # STATUS deliberately emits HELLO before its STATUS row.
                continue
            if text.startswith("ERROR,"):
                self._record_firmware_error(text)
                # Readiness and conversion timeout rows are asynchronous health
                # reports, not responses rejecting the command currently in
                # flight.  Record them and continue waiting for that command's
                # own bounded acknowledgement.
                if self._is_hx711_health_error(text):
                    continue
                raise SerialCommandError(f"firmware rejected {normalized}: {text}")
            if self._is_command_response(normalized, text):
                self._command_count += 1
                if normalized == "START":
                    self._streaming = True
                    # DATA can arrive while the command response is being read;
                    # anchor elapsed time at command transmission so those fresh
                    # conversions never precede the recording origin.
                    if reset_elapsed_origin:
                        self._elapsed_origin_ns = command_sent_ns
                elif normalized == "STOP":
                    self._streaming = False
                elif normalized == "STATUS":
                    fields = text.split(",")
                    self._streaming = len(fields) > 1 and fields[1] == "STREAMING"
                    if len(fields) > 2 and fields[2] == "NOT_READY":
                        self._readiness_error_count += 1
                return text
            self._unexpected_response_count += 1
        self._command_timeout_count += 1
        raise SerialCommandError(f"timed out waiting for {normalized} response")

    def read_sample(self) -> LoadCellSample | None:
        """Return one fresh conversion, or ``None`` within the read timeout."""

        self._require_connected()
        for _ in range(256):
            if self._pending_data_lines:
                received = self._pending_data_lines.pop(0)
            else:
                received = self._read_line_once(count_empty_timeout=True)
                if received is None:
                    return None
            text = received.text
            if text.startswith("DATA,"):
                try:
                    parsed = parse_data_line(text)
                    sample = self._sample_from_data(parsed, received)
                except SerialProtocolError:
                    try:
                        generic = parse_sensor_line(text, self._input_format)
                    except SensorLineParseError:
                        self._reject_received(received, "DATA line could not be parsed")
                        continue
                    sample = self._sample_from_sensor(generic, received)
                self._mark_received(received, "parsed", getattr(sample, "raw_adc", None))
                if sample is not None:
                    return sample
                continue
            if text.startswith("HELLO,"):
                try:
                    identity = parse_hello_line(text)
                except SerialProtocolError:
                    self._malformed_line_count += 1
                    continue
                self._board_reset_count += 1
                self._tracker.reset()
                self._advance_device_session()
                self._update_identity(identity)
                self._streaming = False
                # A Nano reset returns the firmware to STOPPED.  Re-arm it before
                # accepting another DATA row, but preserve the recording origin so
                # elapsed time remains one continuous host-clock domain.
                try:
                    self._send_command("START", reset_elapsed_origin=False)
                except SerialServiceError as exc:
                    message = f"failed to restart stream after board reset: {exc}"
                    self._mark_disconnected(message)
                    raise SerialDisconnectedError(message) from exc
                continue
            if text.startswith("ERROR,"):
                self._record_firmware_error(text)
                continue
            if text.startswith(("STATUS,", "PONG,", "OK,")):
                self._asynchronous_status_count += 1
                self._mark_received(received, "status", None)
                continue
            try:
                generic = parse_sensor_line(text, self._input_format)
            except SensorLineParseError:
                self._reject_received(received, "not a measurement line")
                continue
            sample = self._sample_from_sensor(generic, received)
            self._mark_received(received, "parsed", generic.raw_value)
            if sample is not None:
                return sample
        self._last_error = "protocol line processing limit reached"
        return None

    def diagnostics(self) -> Mapping[str, Any]:
        """Return sample-rate, interval, protocol, and device-session evidence."""

        if self._accepted_timestamps_ns:
            first = self._accepted_timestamps_ns[0]
            last = self._accepted_timestamps_ns[-1]
            elapsed_s = (last - first) / 1e9
            measured_rate = (
                (len(self._accepted_timestamps_ns) - 1) / elapsed_s
                if len(self._accepted_timestamps_ns) > 1 and elapsed_s > 0
                else math.nan
            )
        else:
            measured_rate = math.nan
        return {
            "simulation_mode": False,
            "connected": self.is_connected,
            "streaming": self._streaming,
            "port": self._port,
            "protocol_version": (
                self._connection_info.protocol_version
                if self._connection_info is not None
                else ""
            ),
            "firmware_version": (
                self._connection_info.firmware_version
                if self._connection_info is not None
                else ""
            ),
            "device_session_id": self._current_device_session_id,
            "valid_sample_count": self._valid_sample_count,
            "uncalibrated_sample_count": self._uncalibrated_sample_count,
            "malformed_line_count": self._malformed_line_count,
            "duplicate_sample_count": self._duplicate_sample_count,
            "sample_id_collision_count": self._sample_id_collision_count,
            "stale_sample_count": (
                self._duplicate_sample_count + self._sample_id_collision_count
            ),
            "micros_rollover_count": self._micros_rollover_count,
            "device_session_reset_count": self._device_session_reset_count,
            "board_reset_count": self._board_reset_count,
            "readiness_error_count": self._readiness_error_count,
            "timeout_error_count": self._timeout_error_count,
            "read_timeout_count": self._read_timeout_count,
            "command_timeout_count": self._command_timeout_count,
            "command_count": self._command_count,
            "unexpected_response_count": self._unexpected_response_count,
            "asynchronous_status_count": self._asynchronous_status_count,
            "disconnect_error_count": self._disconnect_error_count,
            "measured_sample_rate_hz": measured_rate,
            "validation_min_readings": (
                self._config.validation_min_readings if self._config is not None else 0
            ),
            "seconds_since_last_valid": (
                math.nan
                if self._last_valid_receipt_ns is None
                else max(0.0, (self._clock_ns() - self._last_valid_receipt_ns) / 1e9)
            ),
            "most_recent_valid_reading": (
                next(
                    (
                        item.get("parsed_value")
                        for item in reversed(self._raw_log_entries)
                        if item.get("status") == "parsed"
                    ),
                    None,
                )
            ),
            "rejected_line_count": self._malformed_line_count,
            "raw_log_entries": tuple(dict(item) for item in self._raw_log_entries),
            "minimum_sample_interval_ms": min(self._sample_intervals_ms, default=math.nan),
            "median_sample_interval_ms": (
                statistics.median(self._sample_intervals_ms)
                if self._sample_intervals_ms
                else math.nan
            ),
            "maximum_sample_interval_ms": max(self._sample_intervals_ms, default=math.nan),
            "last_error": self._last_error,
        }

    def _clock_ns(self) -> int:
        value = self._monotonic_ns()
        if isinstance(value, bool):
            raise TypeError("monotonic_ns clock must return an integer")
        result = int(value)
        if result < 0:
            raise ValueError("monotonic_ns clock returned a negative value")
        return result

    def _serial_is_open(self) -> bool:
        if self._serial is None:
            return False
        return bool(getattr(self._serial, "is_open", True))

    def _require_handle(self) -> _SerialLike:
        if self._serial is None:
            raise SerialDisconnectedError("serial port is not open")
        return self._serial

    def _require_connected(self) -> None:
        if not self._connected or not self._serial_is_open():
            if self._connected:
                self._mark_disconnected("serial port closed unexpectedly")
            raise SerialDisconnectedError("Arduino/HX711 is not connected")

    def _close_handle(self) -> None:
        handle, self._serial = self._serial, None
        if handle is None:
            return
        try:
            handle.close()
        except (OSError, serial.SerialException):
            # The handle is already detached from the service; explicit close is
            # idempotent even if the OS reports that removal raced with us.
            return

    def _mark_disconnected(self, message: str) -> None:
        self._last_error = message
        if self._connected or self._serial is not None:
            self._disconnect_error_count += 1
        self._connected = False
        self._streaming = False
        self._close_handle()

    def _read_line_once(self, *, count_empty_timeout: bool) -> _ReceivedLine | None:
        handle = self._require_handle()
        if not self._serial_is_open():
            self._mark_disconnected("serial port closed unexpectedly")
            raise SerialDisconnectedError("serial port closed unexpectedly")
        # Skip a bounded run of malformed/noise lines in the same public read;
        # this prevents one bad firmware line from hiding the fresh DATA row
        # immediately behind it while keeping the call strictly time bounded.
        for _ in range(64):
            try:
                raw = handle.readline()
            except (OSError, serial.SerialException) as exc:
                self._mark_disconnected(f"serial read failed: {exc}")
                raise SerialDisconnectedError(str(exc)) from exc
            receipt_ns = self._clock_ns()
            if raw in (b"", "", None):
                if count_empty_timeout:
                    self._read_timeout_count += 1
                return None
            wall_clock_iso = _aware_wall_clock_iso(self._wall_clock)
            if isinstance(raw, bytes):
                if len(raw) > self._max_line_bytes:
                    self._malformed_line_count += 1
                    continue
                if not raw.endswith(b"\n"):
                    self._malformed_line_count += 1
                    self._append_raw_log(
                        repr(raw),
                        receipt_ns,
                        wall_clock_iso,
                        "rejected",
                        None,
                        "partial line timed out before newline",
                    )
                    reset_input = getattr(handle, "reset_input_buffer", None)
                    if callable(reset_input):
                        reset_input()
                    return None
            elif isinstance(raw, str):
                if len(raw.encode("utf-8")) > self._max_line_bytes:
                    self._malformed_line_count += 1
                    continue
            else:
                self._malformed_line_count += 1
                continue
            try:
                if isinstance(raw, bytes):
                    text = raw.decode("utf-8", errors="strict").strip("\r\n")
                else:
                    text = str(raw).strip("\r\n")
                if not text or "\x00" in text:
                    raise ValueError("empty line or NUL")
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                self._malformed_line_count += 1
                self._append_raw_log(
                    repr(raw), receipt_ns, wall_clock_iso, "rejected", None, str(exc)
                )
                continue
            sequence = self._append_raw_log(
                text, receipt_ns, wall_clock_iso, "pending", None, ""
            )
            return _ReceivedLine(text, receipt_ns, wall_clock_iso, sequence)
        self._last_error = "too many consecutive malformed serial lines"
        return None

    def _wait_for_sensor_or_hello(
        self, startup_window_s: float
    ) -> tuple[HelloIdentity, bool]:
        timeout = _positive_finite(startup_window_s, "startup_window_s")
        deadline = self._clock_ns() + round(timeout * 1e9)
        empty_budget = max(1, math.ceil(timeout / self._read_timeout_s) + 1)
        empty_count = 0
        iterations = 0
        while self._clock_ns() <= deadline and iterations < 4096:
            iterations += 1
            received = self._read_line_once(count_empty_timeout=False)
            if received is None:
                empty_count += 1
                if empty_count >= empty_budget:
                    break
                self._wait_until_next_poll(deadline)
                continue
            text = received.text
            if text.startswith("HELLO,"):
                try:
                    identity = parse_hello_line(text)
                    self._mark_received(received, "device_info", None)
                    return identity, True
                except SerialProtocolError:
                    self._reject_received(received, "malformed HELLO")
                    continue
            if text.startswith("ERROR,"):
                self._record_firmware_error(text)
                self._mark_received(received, "firmware_error", None)
                continue
            try:
                parse_sensor_line(text, self._input_format)
            except SensorLineParseError:
                self._reject_received(received, "startup text/non-measurement")
                continue
            self._pending_data_lines.append(received)
            return HelloIdentity("HX711_STREAM", "numeric-stream", "unknown"), False
        self._last_error = "no valid numeric HX711 reading received"
        if self._malformed_line_count:
            raise SerialHandshakeError(
                f"Data is arriving from {self._port}, but the selected parser "
                f"({self._input_format}) cannot interpret it. Open Raw Serial "
                f"Monitor to inspect the incoming format; no valid numeric HX711 "
                f"reading was received within {timeout:.3f} seconds."
            )
        raise SerialHandshakeError(
            f"{self._port} opened, but no valid numeric HX711 readings were "
            f"received within {timeout:.3f} seconds."
        )

    def _wait_until_next_poll(self, deadline_ns: int) -> None:
        remaining_s = max(0.0, (deadline_ns - self._clock_ns()) / 1e9)
        if remaining_s > 0.0:
            self._sleeper(min(self._poll_interval_s, remaining_s))

    def _make_device_session_id(self) -> str:
        token = str(self._device_session_token_factory()).strip()
        if not token:
            raise ValueError("device_session_token_factory returned a blank token")
        return f"hx711-{token}-{self._device_session_sequence}"

    def _advance_device_session(self) -> None:
        self._device_session_sequence += 1
        self._current_device_session_id = self._make_device_session_id()
        self._device_session_reset_count += 1
        if self._connection_info is not None:
            self._connection_info = replace(
                self._connection_info,
                device_session_id=self._current_device_session_id,
            )

    def _update_identity(self, identity: HelloIdentity) -> None:
        if self._connection_info is not None:
            self._connection_info = replace(
                self._connection_info,
                device_name=identity.device_name,
                protocol_version=identity.protocol_version,
                firmware_version=identity.firmware_version,
                device_session_id=self._current_device_session_id,
            )

    def _record_firmware_error(self, text: str) -> None:
        fields = text.split(",")
        code = fields[1].strip() if len(fields) > 1 else ""
        if code == "HX711_NOT_READY":
            self._readiness_error_count += 1
        elif code == "HX711_TIMEOUT":
            self._timeout_error_count += 1
        else:
            self._malformed_line_count += 1
        self._last_error = text

    @staticmethod
    def _is_hx711_health_error(text: str) -> bool:
        fields = text.split(",")
        return len(fields) >= 2 and fields[1].strip() in {
            "HX711_NOT_READY",
            "HX711_TIMEOUT",
        }

    @staticmethod
    def _is_command_response(command: str, text: str) -> bool:
        if command == "PING":
            return text == "PONG,HX711_NANO"
        if command == "START":
            return text == "OK,START"
        if command == "STOP":
            return text == "OK,STOP"
        if command == "STATUS":
            fields = text.split(",")
            return (
                len(fields) == 4
                and fields[0] == "STATUS"
                and fields[1] in {"STREAMING", "STOPPED"}
                and fields[2] in {"READY", "NOT_READY"}
                and _INTEGER_PATTERN.fullmatch(fields[3].strip()) is not None
            )
        return False

    def _sample_from_data(
        self, parsed: ParsedDataLine, received: _ReceivedLine
    ) -> LoadCellSample | None:
        update = self._tracker.update(parsed.sample_id, parsed.arduino_micros)
        if update.event is ArduinoClockEvent.DUPLICATE_SAMPLE_ID:
            self._duplicate_sample_count += 1
            return None
        if update.event is ArduinoClockEvent.SAMPLE_ID_COLLISION:
            self._sample_id_collision_count += 1
            return None
        if update.event is ArduinoClockEvent.RESET:
            self._advance_device_session()
        elif update.event is ArduinoClockEvent.ROLLOVER:
            self._micros_rollover_count += 1

        calibration = self._calibration
        calibrated = calibration is not None and calibration.quality_passed
        if calibrated and calibration is not None:
            tared_raw = float(parsed.raw_adc) - calibration.tare_raw
            force_gf = calibration.force_gf(parsed.raw_adc)
            force_N = calibration.force_N(parsed.raw_adc)
            calibration_factor = calibration.counts_per_gram
            zero_offset_raw = calibration.tare_raw
            loadcell_valid = True
            error_code = ErrorCode.NONE
        else:
            tared_raw = math.nan
            force_gf = math.nan
            force_N = math.nan
            calibration_factor = math.nan
            zero_offset_raw = math.nan
            loadcell_valid = False
            error_code = ErrorCode.LOAD_CELL_UNCALIBRATED
            self._uncalibrated_sample_count += 1

        elapsed_time_s = (
            received.receipt_monotonic_ns - self._elapsed_origin_ns
        ) / 1e9
        if elapsed_time_s < 0.0:
            raise SerialProtocolError(
                "sample receipt timestamp precedes the configured elapsed-time origin"
            )
        sample = LoadCellSample(
            host_monotonic_ns=received.receipt_monotonic_ns,
            arduino_sample_id=parsed.sample_id,
            arduino_micros=parsed.arduino_micros,
            arduino_micros_unwrapped=update.unwrapped_micros,
            raw_adc=parsed.raw_adc,
            force_gf=force_gf,
            force_N=force_N,
            device_session_id=self._current_device_session_id,
            elapsed_time_s=elapsed_time_s,
            wall_clock_iso=received.receipt_wall_clock_iso,
            loadcell_valid=loadcell_valid,
            error_code=error_code,
            tared_raw=tared_raw,
            mass_g=force_gf,
            calibration_factor_counts_per_gram=calibration_factor,
            zero_offset_raw=zero_offset_raw,
            serial_port=self._port,
            baud_rate=(0 if self._config is None else self._config.baud_rate),
            arduino_timestamp_available=True,
        )
        if self._accepted_timestamps_ns:
            interval_ns = (
                received.receipt_monotonic_ns - self._accepted_timestamps_ns[-1]
            )
            if interval_ns < 0:
                raise SerialProtocolError(
                    "host monotonic receipt timestamps moved backwards"
                )
            self._sample_intervals_ms.append(interval_ns / 1e6)
        self._accepted_timestamps_ns.append(received.receipt_monotonic_ns)
        self._valid_sample_count += 1
        self._last_valid_receipt_ns = received.receipt_monotonic_ns
        return sample

    def _sample_from_sensor(
        self, parsed: ParsedSensorLine, received: _ReceivedLine
    ) -> LoadCellSample | None:
        sample_id = (
            self._generic_sample_id if parsed.sample_id is None else parsed.sample_id
        )
        if parsed.sample_id is None:
            self._generic_sample_id += 1
        arduino_micros = (
            (received.receipt_monotonic_ns // 1000) & ARDUINO_UINT32_MAX
            if parsed.arduino_timestamp is None
            else parsed.arduino_timestamp
        )
        sample = self._sample_from_data(
            ParsedDataLine(sample_id, arduino_micros, parsed.raw_value), received
        )
        if sample is None:
            return None
        return replace(
            sample,
            arduino_timestamp_available=parsed.arduino_timestamp is not None,
        )

    def _append_raw_log(
        self,
        raw_line: str,
        receipt_ns: int,
        wall_clock_iso: str,
        status: str,
        parsed_value: float | None,
        message: str,
    ) -> int:
        self._raw_line_sequence += 1
        self._raw_log_entries.append(
            {
                "sequence": self._raw_line_sequence,
                "host_monotonic_ns": int(receipt_ns),
                "wall_clock_iso": str(wall_clock_iso),
                "raw_line": str(raw_line),
                "status": str(status),
                "parsed_value": parsed_value,
                "message": str(message),
            }
        )
        return self._raw_line_sequence

    def _mark_received(
        self, received: _ReceivedLine, status: str, parsed_value: float | None
    ) -> None:
        for entry in reversed(self._raw_log_entries):
            if int(entry.get("sequence", -1)) == received.sequence:
                entry["status"] = str(status)
                entry["parsed_value"] = parsed_value
                return

    def _reject_received(self, received: _ReceivedLine, message: str) -> None:
        self._malformed_line_count += 1
        self._mark_received(received, "rejected", None)
        for entry in reversed(self._raw_log_entries):
            if int(entry.get("sequence", -1)) == received.sequence:
                entry["message"] = str(message)
                return

    def _reset_diagnostics(self) -> None:
        self._last_valid_receipt_ns = None
        self._raw_log_entries.clear()
        self._raw_line_sequence = 0
        self._valid_sample_count = 0
        self._uncalibrated_sample_count = 0
        self._malformed_line_count = 0
        self._duplicate_sample_count = 0
        self._sample_id_collision_count = 0
        self._micros_rollover_count = 0
        self._device_session_reset_count = 0
        self._board_reset_count = 0
        self._readiness_error_count = 0
        self._timeout_error_count = 0
        self._read_timeout_count = 0
        self._command_timeout_count = 0
        self._command_count = 0
        self._unexpected_response_count = 0
        self._asynchronous_status_count = 0
        self._disconnect_error_count = 0
        self._sample_intervals_ms: list[float] = []
        self._accepted_timestamps_ns: list[int] = []
        self._last_error = ""


def _playback_float(value: str, name: str) -> float:
    text = value.strip()
    if not text:
        return math.nan
    if text.casefold() == "nan":
        return math.nan
    try:
        result = float(text)
    except ValueError as exc:
        raise SerialProtocolError(f"{name} must be numeric") from exc
    if math.isinf(result):
        raise SerialProtocolError(f"{name} must not be infinite")
    return result


def _playback_nonnegative_int(value: str, name: str) -> int:
    token = value.strip()
    if _INTEGER_PATTERN.fullmatch(token) is None:
        raise SerialProtocolError(f"{name} must be a decimal integer")
    result = int(token, 10)
    if result < 0:
        raise SerialProtocolError(f"{name} must be nonnegative")
    return result


def _playback_bool(value: str, name: str) -> bool:
    token = value.strip().lower()
    if token in {"true", "1"}:
        return True
    if token in {"false", "0"}:
        return False
    raise SerialProtocolError(f"{name} must be boolean")


class SerialPlaybackSource:
    """Clearly simulated playback of canonical raw CSV or firmware text.

    Canonical ``loadcell_raw.csv`` input retains every recorded timestamp,
    device-session ID, raw value, force value, validity flag, and error code.
    Plain-text input accepts the actual firmware lines and timestamps each DATA
    row with the injected host clocks when it is emitted.
    """

    def __init__(
        self,
        source_path: str | Path,
        calibration: LoadCellCalibration | None = None,
        *,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.source_path = Path(source_path)
        self._calibration = calibration
        self._monotonic_ns = monotonic_ns
        self._wall_clock = wall_clock
        self._connected = False
        self._streaming = False
        self._config: LoadCellConfig | None = None
        self._connection_info: SerialConnectionInfo | None = None
        self._entries: tuple[LoadCellSample | ParsedDataLine, ...] = ()
        self._csv_mode = False
        self._index = 0
        self._elapsed_origin_ns = 0
        self._tracker = ArduinoMicrosTracker()
        self._text_device_session_index = 0
        self._source_malformed_count = 0
        self._source_readiness_error_count = 0
        self._source_timeout_error_count = 0
        self._emitted_timestamps_ns: list[int] = []

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def simulation_mode(self) -> bool:
        return True

    @property
    def connection_info(self) -> SerialConnectionInfo:
        if self._connection_info is None:
            raise SerialDisconnectedError("serial playback is not connected")
        return self._connection_info

    def set_calibration(self, calibration: LoadCellCalibration | None) -> None:
        self._calibration = calibration

    def enumerate_ports(self) -> tuple[SerialPortDescriptor, ...]:
        """Playback never advertises a physical serial port."""

        return ()

    list_ports = enumerate_ports
    refresh_ports = enumerate_ports

    def connect(
        self, config: LoadCellConfig, *, port: str | None = None
    ) -> SerialConnectionInfo:
        if not isinstance(config, LoadCellConfig):
            raise TypeError("config must be a LoadCellConfig")
        self.disconnect()
        if not self.source_path.is_file():
            raise FileNotFoundError(f"serial playback source does not exist: {self.source_path}")
        self._config = config
        self._source_malformed_count = 0
        self._source_readiness_error_count = 0
        self._source_timeout_error_count = 0
        source_identity: HelloIdentity | None
        if self.source_path.suffix.lower() in {".csv", ".partial"}:
            self._entries = self._load_canonical_csv()
            self._csv_mode = True
            source_identity = None
        else:
            self._entries, source_identity = self._load_protocol_text()
            self._csv_mode = False
        self._index = 0
        self._tracker.reset()
        self._text_device_session_index = 0
        self._emitted_timestamps_ns = []
        self._elapsed_origin_ns = self._clock_ns()
        identity = source_identity or HelloIdentity(
            device_name="HX711_PLAYBACK",
            protocol_version="PLAYBACK-1.0",
            firmware_version="recorded-source",
        )
        self._connection_info = SerialConnectionInfo(
            port=f"PLAYBACK:{self.source_path.name}",
            baud_rate=config.baud_rate,
            device_name="HX711_PLAYBACK",
            protocol_version=identity.protocol_version,
            firmware_version=identity.firmware_version,
            device_session_id=f"playback-{self.source_path.stem}-0",
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
        self._entries = ()
        self._index = 0
        self._tracker.reset()
        self._emitted_timestamps_ns = []

    def set_elapsed_origin_ns(self, host_monotonic_ns: int) -> None:
        if isinstance(host_monotonic_ns, bool):
            raise TypeError("host_monotonic_ns must be an integer")
        origin = int(host_monotonic_ns)
        if origin < 0:
            raise ValueError("host_monotonic_ns must be nonnegative")
        self._elapsed_origin_ns = origin

    def send_command(self, command: str) -> str:
        if not self._connected:
            raise SerialDisconnectedError("serial playback is not connected")
        normalized = str(command).strip().upper()
        if normalized == "PING":
            return "PONG,HX711_PLAYBACK"
        if normalized == "START":
            self._streaming = True
            self._elapsed_origin_ns = self._clock_ns()
            return "OK,START"
        if normalized == "STOP":
            self._streaming = False
            return "OK,STOP"
        if normalized == "STATUS":
            state = "STREAMING" if self._streaming else "STOPPED"
            return f"STATUS,{state},PLAYBACK,{self._index},{len(self._entries)}"
        raise ValueError("command must be PING, START, STOP, or STATUS")

    def read_sample(self) -> LoadCellSample | None:
        if not self._connected:
            raise SerialDisconnectedError("serial playback is not connected")
        if not self._streaming:
            return None
        while self._index < len(self._entries):
            entry = self._entries[self._index]
            self._index += 1
            if isinstance(entry, LoadCellSample):
                sample = entry
            else:
                sample = self._text_sample(entry)
                if sample is None:
                    continue
            self._emitted_timestamps_ns.append(sample.host_monotonic_ns)
            return sample
        return None

    def diagnostics(self) -> Mapping[str, Any]:
        timestamps = self._emitted_timestamps_ns
        intervals_ms = [
            (current - previous) / 1e6
            for previous, current in zip(timestamps, timestamps[1:])
        ]
        if len(timestamps) > 1 and timestamps[-1] > timestamps[0]:
            rate = (len(timestamps) - 1) / ((timestamps[-1] - timestamps[0]) / 1e9)
        else:
            rate = math.nan
        return {
            "simulation_mode": True,
            "source_type": "canonical_csv" if self._csv_mode else "protocol_text",
            "source_path": str(self.source_path),
            "connected": self._connected,
            "streaming": self._streaming,
            "exhausted": self._index >= len(self._entries),
            "source_row_count": len(self._entries),
            "valid_sample_count": len(timestamps),
            "malformed_line_count": self._source_malformed_count,
            "readiness_error_count": self._source_readiness_error_count,
            "timeout_error_count": self._source_timeout_error_count,
            "measured_sample_rate_hz": rate,
            "minimum_sample_interval_ms": min(intervals_ms, default=math.nan),
            "median_sample_interval_ms": (
                statistics.median(intervals_ms) if intervals_ms else math.nan
            ),
            "maximum_sample_interval_ms": max(intervals_ms, default=math.nan),
        }

    def _clock_ns(self) -> int:
        value = self._monotonic_ns()
        if isinstance(value, bool):
            raise TypeError("monotonic_ns clock must return an integer")
        result = int(value)
        if result < 0:
            raise ValueError("monotonic_ns clock returned a negative value")
        return result

    def _load_canonical_csv(self) -> tuple[LoadCellSample, ...]:
        with self.source_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != tuple(LOADCELL_RAW_COLUMNS):
                raise SerialProtocolError(
                    "canonical playback CSV header must exactly match loadcell_raw schema"
                )
            samples: list[LoadCellSample] = []
            previous_host_ns = -1
            for line_number, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    raise SerialProtocolError(
                        f"canonical playback CSV row {line_number} is truncated"
                    )
                try:
                    host_ns = _playback_nonnegative_int(
                        row["host_monotonic_ns"], "host_monotonic_ns"
                    )
                    if host_ns < previous_host_ns:
                        raise SerialProtocolError(
                            "canonical playback timestamps must be nondecreasing"
                        )
                    previous_host_ns = host_ns
                    sample_id = _decimal_integer(
                        row["arduino_sample_id"],
                        "arduino_sample_id",
                        minimum=0,
                        maximum=ARDUINO_UINT32_MAX,
                    )
                    micros = _decimal_integer(
                        row["arduino_micros"],
                        "arduino_micros",
                        minimum=0,
                        maximum=ARDUINO_UINT32_MAX,
                    )
                    raw_adc = _playback_float(row["raw_adc"], "raw_adc")
                    if not math.isfinite(raw_adc):
                        raise SerialProtocolError("raw_adc must be finite")
                    unwrapped_text = row["arduino_micros_unwrapped"].strip()
                    unwrapped = (
                        None
                        if not unwrapped_text or unwrapped_text.lower() == "nan"
                        else _playback_nonnegative_int(
                            unwrapped_text, "arduino_micros_unwrapped"
                        )
                    )
                    error_text = row["error_code"].strip() or ErrorCode.NONE.value
                    error_code = ErrorCode(error_text)
                    session_id = row["device_session_id"].strip()
                    if not session_id:
                        raise SerialProtocolError("device_session_id must not be blank")
                    samples.append(
                        LoadCellSample(
                            host_monotonic_ns=host_ns,
                            arduino_sample_id=sample_id,
                            arduino_micros=micros,
                            arduino_micros_unwrapped=unwrapped,
                            raw_adc=raw_adc,
                            force_gf=_playback_float(row["force_gf"], "force_gf"),
                            force_N=_playback_float(row["force_N"], "force_N"),
                            device_session_id=session_id,
                            elapsed_time_s=_playback_float(
                                row["elapsed_time_s"], "elapsed_time_s"
                            ),
                            wall_clock_iso=row["wall_clock_iso"],
                            loadcell_valid=_playback_bool(
                                row["loadcell_valid"], "loadcell_valid"
                            ),
                            error_code=error_code,
                            tared_raw=_playback_float(
                                row["tared_raw"], "tared_raw"
                            ),
                            mass_g=_playback_float(row["mass_g"], "mass_g"),
                            calibration_factor_counts_per_gram=_playback_float(
                                row["calibration_factor_counts_per_gram"],
                                "calibration_factor_counts_per_gram",
                            ),
                            zero_offset_raw=_playback_float(
                                row["zero_offset_raw"], "zero_offset_raw"
                            ),
                            serial_port=row["serial_port"],
                            baud_rate=_playback_nonnegative_int(
                                row["baud_rate"], "baud_rate"
                            ),
                            arduino_timestamp_available=_playback_bool(
                                row["arduino_timestamp_available"],
                                "arduino_timestamp_available",
                            ),
                        )
                    )
                except (KeyError, ValueError) as exc:
                    if isinstance(exc, SerialProtocolError):
                        raise
                    raise SerialProtocolError(
                        f"invalid canonical playback CSV row {line_number}: {exc}"
                    ) from exc
        return tuple(samples)

    def _load_protocol_text(
        self,
    ) -> tuple[tuple[ParsedDataLine, ...], HelloIdentity | None]:
        entries: list[ParsedDataLine] = []
        identity: HelloIdentity | None = None
        with self.source_path.open("r", encoding="ascii", errors="strict") as handle:
            for raw_line in handle:
                text = raw_line.strip()
                if not text or text.startswith("#"):
                    continue
                if text.startswith("DATA,"):
                    try:
                        entries.append(parse_data_line(text))
                    except SerialProtocolError:
                        self._source_malformed_count += 1
                elif text.startswith("HELLO,"):
                    try:
                        identity = parse_hello_line(text)
                    except SerialProtocolError:
                        self._source_malformed_count += 1
                elif text == "ERROR,HX711_NOT_READY":
                    self._source_readiness_error_count += 1
                elif text == "ERROR,HX711_TIMEOUT":
                    self._source_timeout_error_count += 1
                else:
                    self._source_malformed_count += 1
        return tuple(entries), identity

    def _text_sample(self, parsed: ParsedDataLine) -> LoadCellSample | None:
        update = self._tracker.update(parsed.sample_id, parsed.arduino_micros)
        if update.event in {
            ArduinoClockEvent.DUPLICATE_SAMPLE_ID,
            ArduinoClockEvent.SAMPLE_ID_COLLISION,
        }:
            self._source_malformed_count += 1
            return None
        if update.event is ArduinoClockEvent.RESET:
            self._text_device_session_index += 1
        receipt_ns = self._clock_ns()
        elapsed_s = (receipt_ns - self._elapsed_origin_ns) / 1e9
        calibration = self._calibration
        if calibration is not None and calibration.quality_passed:
            tared_raw = float(parsed.raw_adc) - calibration.tare_raw
            force_gf = calibration.force_gf(parsed.raw_adc)
            force_N = calibration.force_N(parsed.raw_adc)
            calibration_factor = calibration.counts_per_gram
            zero_offset_raw = calibration.tare_raw
            valid = True
            error_code = ErrorCode.NONE
        else:
            tared_raw = math.nan
            force_gf = force_N = math.nan
            calibration_factor = zero_offset_raw = math.nan
            valid = False
            error_code = ErrorCode.LOAD_CELL_UNCALIBRATED
        return LoadCellSample(
            host_monotonic_ns=receipt_ns,
            arduino_sample_id=parsed.sample_id,
            arduino_micros=parsed.arduino_micros,
            arduino_micros_unwrapped=update.unwrapped_micros,
            raw_adc=parsed.raw_adc,
            force_gf=force_gf,
            force_N=force_N,
            device_session_id=(
                f"playback-{self.source_path.stem}-{self._text_device_session_index}"
            ),
            elapsed_time_s=elapsed_s,
            wall_clock_iso=_aware_wall_clock_iso(self._wall_clock),
            loadcell_valid=valid,
            error_code=error_code,
            tared_raw=tared_raw,
            mass_g=force_gf,
            calibration_factor_counts_per_gram=calibration_factor,
            zero_offset_raw=zero_offset_raw,
            serial_port=(
                "" if self._connection_info is None else self._connection_info.port
            ),
            baud_rate=(0 if self._config is None else self._config.baud_rate),
            arduino_timestamp_available=True,
        )


# Explicit hardware-oriented alias used by GUI/service composition code.
Hx711SerialService = SerialService


__all__ = [
    "ARDUINO_UINT32_MAX",
    "DEFAULT_COMMAND_TIMEOUT_S",
    "DEFAULT_READ_TIMEOUT_S",
    "DEVICE_NAME",
    "HelloIdentity",
    "Hx711SerialService",
    "ParsedDataLine",
    "SerialCommandError",
    "SerialDisconnectedError",
    "SerialHandshakeError",
    "SerialPortDescriptor",
    "SerialPlaybackSource",
    "SerialProtocolError",
    "SerialService",
    "SerialServiceError",
    "enumerate_serial_ports",
    "parse_data_line",
    "parse_hello_line",
]
