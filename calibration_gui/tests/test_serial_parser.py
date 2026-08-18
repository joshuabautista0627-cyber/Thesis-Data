from __future__ import annotations

import math

import pytest

from services.serial_parser import (
    SensorInputFormat,
    SensorLineParseError,
    parse_sensor_line,
)


@pytest.mark.parametrize(
    ("line", "expected", "source"),
    (
        ("123\n", 123.0, "plain"),
        ("+42.5\r\n", 42.5, "plain"),
        (" -83421 ", -83421.0, "plain"),
        ("RAW:-83421\n", -83421.0, "raw_prefix"),
        (" raw = +18.25 \r\n", 18.25, "raw_prefix"),
        ("12543,-83421\n", -83421.0, "timestamp_csv"),
        ("DATA,9,12543,-83421", -83421.0, "data4"),
        ("DATA,12543,-83421.5", -83421.5, "data3"),
        ("1.25e6", 1_250_000.0, "plain"),
    ),
)
def test_supported_sensor_lines(line: str, expected: float, source: str) -> None:
    parsed = parse_sensor_line(line)
    assert parsed.raw_value == expected
    assert parsed.source_format == source
    assert math.isfinite(parsed.raw_value)


def test_timestamp_csv_preserves_device_timestamp() -> None:
    parsed = parse_sensor_line(" 12543 , -83421 \r\n")
    assert parsed.arduino_timestamp == 12543
    assert parsed.sample_id is None


@pytest.mark.parametrize(
    "line",
    (
        "",
        "\r\n",
        "Arduino Nano starting",
        "RAW:",
        "12,",
        "not,a,measurement",
        "NaN",
        "Infinity",
        "-inf",
        b"\xff\xfe\n",
    ),
)
def test_invalid_startup_malformed_partial_and_nonfinite_lines_are_rejected(
    line: str | bytes,
) -> None:
    with pytest.raises(SensorLineParseError):
        parse_sensor_line(line)


def test_explicit_parser_format_does_not_silently_accept_another_format() -> None:
    with pytest.raises(SensorLineParseError):
        parse_sensor_line("RAW:17", SensorInputFormat.PLAIN)
    assert parse_sensor_line("RAW:17", SensorInputFormat.RAW_PREFIX).raw_value == 17

