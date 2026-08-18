"""Permissive, finite-only HX711 sensor-line parsing.

The physical serial owner uses this module for measurement decoding.  Device
identification text is deliberately outside this parser: a startup banner is
not proof that an HX711 is producing samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re


class SensorLineParseError(ValueError):
    """A complete serial line does not match the selected measurement format."""


class SensorInputFormat(str, Enum):
    AUTO = "Auto-detect"
    DATA = "DATA,<sample_id>,<timestamp>,<raw>"
    PLAIN = "Plain numeric"
    RAW_PREFIX = "RAW:<value> or raw=<value>"
    TIMESTAMP_CSV = "<timestamp>,<raw>"


@dataclass(frozen=True, slots=True)
class ParsedSensorLine:
    """One finite raw reading plus optional firmware timing information."""

    raw_value: float
    source_format: str
    sample_id: int | None = None
    arduino_timestamp: int | None = None

    @property
    def raw_adc(self) -> float:
        return self.raw_value


_NUMBER = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
)
_RAW_PREFIX = re.compile(
    r"(?i)raw\s*(?::|=)\s*(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
)


def _text(line: str | bytes) -> str:
    if isinstance(line, bytes):
        try:
            value = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise SensorLineParseError("serial line is not valid UTF-8") from exc
    elif isinstance(line, str):
        value = line
    else:
        raise TypeError("sensor line must be str or bytes")
    value = value.strip()
    if not value:
        raise SensorLineParseError("sensor line is empty")
    if "\x00" in value:
        raise SensorLineParseError("sensor line contains NUL")
    return value


def _finite_number(token: str, name: str) -> float:
    stripped = token.strip()
    if _NUMBER.fullmatch(stripped) is None:
        raise SensorLineParseError(f"{name} is not a decimal number")
    value = float(stripped)
    if not math.isfinite(value):
        raise SensorLineParseError(f"{name} must be finite")
    return value


def _nonnegative_uint32(token: str, name: str) -> int:
    value = _finite_number(token, name)
    if not value.is_integer() or not 0 <= value <= 0xFFFFFFFF:
        raise SensorLineParseError(f"{name} must be an unsigned 32-bit integer")
    return int(value)


def _format(value: SensorInputFormat | str) -> SensorInputFormat:
    if isinstance(value, SensorInputFormat):
        return value
    normalized = str(value).strip().casefold()
    aliases = {
        "auto": SensorInputFormat.AUTO,
        "auto-detect": SensorInputFormat.AUTO,
        "data": SensorInputFormat.DATA,
        "plain": SensorInputFormat.PLAIN,
        "plain numeric": SensorInputFormat.PLAIN,
        "raw": SensorInputFormat.RAW_PREFIX,
        "raw prefix": SensorInputFormat.RAW_PREFIX,
        "csv": SensorInputFormat.TIMESTAMP_CSV,
        "timestamp csv": SensorInputFormat.TIMESTAMP_CSV,
    }
    for member in SensorInputFormat:
        aliases[member.value.casefold()] = member
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported sensor input format: {value!r}") from exc


def _parse_data(text: str) -> ParsedSensorLine:
    fields = tuple(field.strip() for field in text.split(","))
    if len(fields) == 4 and fields[0].upper() == "DATA":
        return ParsedSensorLine(
            raw_value=_finite_number(fields[3], "raw value"),
            sample_id=_nonnegative_uint32(fields[1], "sample id"),
            arduino_timestamp=_nonnegative_uint32(fields[2], "Arduino timestamp"),
            source_format="data4",
        )
    if len(fields) == 3 and fields[0].upper() == "DATA":
        return ParsedSensorLine(
            raw_value=_finite_number(fields[2], "raw value"),
            arduino_timestamp=_nonnegative_uint32(fields[1], "Arduino timestamp"),
            source_format="data3",
        )
    raise SensorLineParseError(
        "expected DATA,<sample_id>,<timestamp>,<raw> or DATA,<timestamp>,<raw>"
    )


def _parse_plain(text: str) -> ParsedSensorLine:
    return ParsedSensorLine(
        raw_value=_finite_number(text, "raw value"), source_format="plain"
    )


def _parse_raw_prefix(text: str) -> ParsedSensorLine:
    match = _RAW_PREFIX.fullmatch(text)
    if match is None:
        raise SensorLineParseError("expected RAW:<value> or raw=<value>")
    return ParsedSensorLine(
        raw_value=_finite_number(match.group("value"), "raw value"),
        source_format="raw_prefix",
    )


def _parse_csv(text: str) -> ParsedSensorLine:
    fields = tuple(field.strip() for field in text.split(","))
    if len(fields) != 2:
        raise SensorLineParseError("expected <timestamp>,<raw>")
    return ParsedSensorLine(
        raw_value=_finite_number(fields[1], "raw value"),
        arduino_timestamp=_nonnegative_uint32(fields[0], "Arduino timestamp"),
        source_format="timestamp_csv",
    )


def parse_sensor_line(
    line: str | bytes,
    expected_format: SensorInputFormat | str = SensorInputFormat.AUTO,
) -> ParsedSensorLine:
    """Parse one complete line without accepting NaN, infinity, or banners."""

    text = _text(line)
    selected = _format(expected_format)
    parsers = {
        SensorInputFormat.DATA: _parse_data,
        SensorInputFormat.PLAIN: _parse_plain,
        SensorInputFormat.RAW_PREFIX: _parse_raw_prefix,
        SensorInputFormat.TIMESTAMP_CSV: _parse_csv,
    }
    if selected is not SensorInputFormat.AUTO:
        return parsers[selected](text)
    errors: list[str] = []
    for parser in (_parse_data, _parse_raw_prefix, _parse_plain, _parse_csv):
        try:
            return parser(text)
        except SensorLineParseError as exc:
            errors.append(str(exc))
    raise SensorLineParseError("line is not a supported HX711 measurement format")


__all__ = [
    "ParsedSensorLine",
    "SensorInputFormat",
    "SensorLineParseError",
    "parse_sensor_line",
]
