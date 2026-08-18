"""Static contract tests for the Arduino Nano + bogde HX711 firmware."""

from __future__ import annotations

from pathlib import Path
import re


FIRMWARE = (
    Path(__file__).resolve().parents[1]
    / "arduino"
    / "hx711_nano_stream"
    / "hx711_nano_stream.ino"
)


def source() -> str:
    return FIRMWARE.read_text(encoding="utf-8")


def without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def function_body(text: str, name: str) -> str:
    """Extract a balanced-brace Arduino function body for structural checks."""

    match = re.search(rf"\bvoid\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", text)
    assert match, f"missing function {name}"
    start = match.end()
    depth = 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise AssertionError(f"unbalanced braces in {name}")


def test_required_editable_constants_are_exact() -> None:
    text = source()
    assert "const byte HX711_DOUT_PIN = 4;" in text
    assert "const byte HX711_SCK_PIN = 5;" in text
    assert "const unsigned long SERIAL_BAUD = 115200;" in text


def test_bogde_hx711_library_and_required_api_are_used() -> None:
    text = without_comments(source())
    assert re.search(r"#include\s*<HX711\.h>", text)
    assert re.search(r"\bHX711\s+scale\s*;", text)
    assert "scale.begin(HX711_DOUT_PIN, HX711_SCK_PIN);" in text
    assert "scale.is_ready()" in text
    assert "scale.read()" in text


def test_read_occurs_once_and_only_inside_is_ready_guard() -> None:
    body = without_comments(function_body(source(), "serviceHx711"))
    assert body.count("scale.read()") == 1
    ready = re.search(
        r"if\s*\(\s*scale\.is_ready\(\)\s*\)\s*\{(?P<body>.*?)\n\s*\}",
        body,
        flags=re.DOTALL,
    )
    assert ready, "fresh-conversion branch must be guarded by is_ready()"
    ready_body = ready.group("body")
    assert "scale.read()" in ready_body
    assert 'Serial.print(F("DATA,"))' in ready_body
    assert "++sampleId;" in ready_body
    assert body.index("scale.read()") < body.index('Serial.print(F("DATA,"))')


def test_data_protocol_contains_fresh_id_micros_and_signed_raw_adc() -> None:
    text = without_comments(source())
    body = function_body(text, "serviceHx711")
    assert re.search(r"const\s+long\s+rawAdc\s*=\s*scale\.read\(\)\s*;", body)
    assert re.search(
        r"const\s+unsigned\s+long\s+sampleMicros\s*=\s*micros\(\)\s*;", body
    )
    assert 'Serial.print(F("DATA,"));' in body
    for token in ("Serial.print(sampleId);", "Serial.print(sampleMicros);", "Serial.println(rawAdc);"):
        assert token in body
    assert re.search(r"unsigned\s+long\s+sampleId\s*=\s*0\s*;", text)


def test_no_cached_raw_value_or_stale_repeat_path_exists() -> None:
    text = without_comments(source())
    assert text.count('Serial.print(F("DATA,"))') == 1
    assert text.count("scale.read()") == 1
    forbidden_cache_names = ("lastRaw", "cachedRaw", "previousRaw", "rawCache")
    assert not any(name in text for name in forbidden_cache_names)
    assert function_body(text, "loop").count("serviceHx711();") == 1


def test_serial_command_parser_is_nonblocking_and_has_every_command() -> None:
    text = without_comments(source())
    parser = function_body(text, "pollSerialCommands")
    assert "while (Serial.available() > 0)" in parser
    assert "Serial.read()" in parser
    for blocking_api in ("readString", "readStringUntil", "parseInt", "parseFloat"):
        assert blocking_api not in text
    handler = function_body(text, "handleCommand")
    for command in ("PING", "START", "STOP", "STATUS"):
        assert f'strcmp(command, "{command}") == 0' in handler


def test_startup_and_status_emit_versioned_hello_identity() -> None:
    text = without_comments(source())
    hello = function_body(text, "emitHello")
    assert 'Serial.print(F("HELLO,HX711_NANO,"));' in hello
    assert "PROTOCOL_VERSION" in hello
    assert "FIRMWARE_VERSION" in hello
    assert "emitHello();" in function_body(text, "setup")
    assert "emitHello();" in function_body(text, "emitStatus")
    assert 'Serial.println(F("PONG,HX711_NANO"));' in text


def test_readiness_and_timeout_errors_are_reported_without_long_delays() -> None:
    text = without_comments(source())
    assert 'F("ERROR,HX711_NOT_READY")' in text
    assert 'F("ERROR,HX711_TIMEOUT")' in text
    assert "HX711_READY_TIMEOUT_MS" in text
    assert not re.search(r"\bdelay(?:Microseconds)?\s*\(", text)
    assert "while (!Serial" not in text


def test_loop_services_commands_and_hx711_without_other_work() -> None:
    loop = without_comments(function_body(source(), "loop"))
    statements = [line.strip() for line in loop.splitlines() if line.strip()]
    assert statements == ["pollSerialCommands();", "serviceHx711();"]
