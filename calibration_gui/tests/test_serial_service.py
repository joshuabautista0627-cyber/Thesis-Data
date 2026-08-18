"""Hardware-free tests for the sole physical Arduino/HX711 serial owner."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
import serial

from core.models import ErrorCode, LoadCellCalibration, LoadCellConfig
from data.exporter import atomic_write_csv, loadcell_sample_to_row
from data.schemas import LOADCELL_RAW_COLUMNS
from services.interfaces import SerialServiceInterface
from services.serial_service import (
    ARDUINO_UINT32_MAX,
    SerialCommandError,
    SerialDisconnectedError,
    SerialHandshakeError,
    SerialProtocolError,
    SerialPlaybackSource,
    SerialService,
    enumerate_serial_ports,
    parse_data_line,
    parse_hello_line,
)


class FakeClock:
    def __init__(self, now_ns: int = 1_000_000_000) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, seconds: float) -> None:
        self.now_ns += round(seconds * 1_000_000_000)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class FakeSerial:
    def __init__(
        self,
        clock: FakeClock,
        *,
        startup_lines: tuple[bytes, ...] = (
            b"HELLO,HX711_NANO,1.0,1.0.0\r\n",
        ),
        **kwargs,
    ) -> None:
        self.clock = clock
        self.startup_lines = startup_lines
        self.kwargs = kwargs
        self.inbound: deque[bytes] = deque()
        self.writes: list[bytes] = []
        self.is_open = True
        self.timeout = kwargs.get("timeout")
        self.reset_input_calls = 0
        self.reset_output_calls = 0
        self.flush_calls = 0
        self.close_calls = 0
        self.fail_on_read = False
        self.fail_on_write = False
        self.command_responses: dict[bytes, tuple[bytes, ...]] = {
            b"PING\n": (b"PONG,HX711_NANO\r\n",),
            b"START\n": (b"OK,START\r\n",),
            b"STOP\n": (b"OK,STOP\r\n",),
            b"STATUS\n": (
                b"HELLO,HX711_NANO,1.0,1.0.0\r\n",
                b"STATUS,STREAMING,READY,7\r\n",
            ),
        }

    def queue(self, *lines: bytes) -> None:
        self.inbound.extend(lines)

    def reset_input_buffer(self) -> None:
        self.reset_input_calls += 1
        self.inbound.clear()
        if self.reset_input_calls == 1:
            self.inbound.extend(self.startup_lines)

    def reset_output_buffer(self) -> None:
        self.reset_output_calls += 1

    def readline(self) -> bytes:
        if self.fail_on_read:
            raise serial.SerialException("device removed")
        self.clock.advance(float(self.timeout or 0.01))
        return self.inbound.popleft() if self.inbound else b""

    def write(self, data: bytes) -> int:
        if self.fail_on_write:
            raise serial.SerialException("device removed during write")
        self.writes.append(data)
        self.inbound.extend(self.command_responses.get(data, ()))
        return len(data)

    def flush(self) -> None:
        self.flush_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False


class SerialFactory:
    def __init__(
        self,
        clock: FakeClock,
        *,
        startup_lines: tuple[bytes, ...] = (
            b"HELLO,HX711_NANO,1.0,1.0.0\r\n",
        ),
    ) -> None:
        self.clock = clock
        self.startup_lines = startup_lines
        self.calls: list[dict[str, object]] = []
        self.handles: list[FakeSerial] = []

    def __call__(self, **kwargs) -> FakeSerial:
        self.calls.append(dict(kwargs))
        handle = FakeSerial(
            self.clock, startup_lines=self.startup_lines, **kwargs
        )
        self.handles.append(handle)
        return handle


def calibration(*, counts_per_gram: float = 100.0) -> LoadCellCalibration:
    loaded_mean = 1_000.0 + counts_per_gram * 200.0
    return LoadCellCalibration(
        calibration_id="cal-serial",
        counts_per_gram=counts_per_gram,
        tare_raw=1_000.0,
        known_mass_g=200.0,
        unloaded_mean_raw=1_000.0,
        unloaded_std_raw=2.0,
        unloaded_sample_count=20,
        loaded_mean_raw=loaded_mean,
        loaded_std_raw=3.0,
        loaded_sample_count=20,
        calibration_span_counts=abs(loaded_mean - 1_000.0),
        calibration_noise_counts=3.0,
        calibration_snr=abs(loaded_mean - 1_000.0) / 3.0,
        loaded_window_mean_gf=200.0,
        loaded_window_std_gf=0.03,
        loaded_window_cv_percent=0.015,
        minimum_sample_count=20,
        minimum_calibration_snr=10.0,
        maximum_loaded_window_cv_percent=2.0,
        minimum_abs_counts_per_gram=1e-9,
        quality_passed=True,
        serial_port="COM7",
        firmware_identity="HX711_NANO/1.0.0",
        protocol_version="1.0",
        firmware_version="1.0.0",
        calibration_timestamp_iso="2026-08-03T00:00:00+00:00",
        tare_timestamp_iso="2026-08-03T00:00:00+00:00",
    )


def load_config() -> LoadCellConfig:
    return LoadCellConfig(startup_window_s=0.1)


def connected_service(
    *,
    calibration_value: LoadCellCalibration | None = None,
    startup_lines: tuple[bytes, ...] = (
        b"HELLO,HX711_NANO,1.0,1.0.0\r\n",
    ),
) -> tuple[SerialService, FakeSerial, FakeClock, SerialFactory]:
    clock = FakeClock()
    factory = SerialFactory(clock, startup_lines=startup_lines)
    tokens = iter(("connection", "reset-one", "reset-two", "reset-three"))
    service = SerialService(
        calibration_value,
        serial_factory=factory,
        monotonic_ns=clock,
        wall_clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
        sleeper=clock.sleep,
        read_timeout_s=0.01,
        command_timeout_s=0.1,
        poll_interval_s=0.005,
        device_session_token_factory=lambda: next(tokens),
    )
    service.connect(load_config(), port="COM7")
    return service, factory.handles[-1], clock, factory


def test_protocol_parsers_accept_signed_counts_and_reject_malformed_fields() -> None:
    hello = parse_hello_line("HELLO,HX711_NANO,1.0,1.2.3\r\n")
    assert hello.protocol_version == "1.0"
    assert hello.firmware_version == "1.2.3"
    data = parse_data_line(b"DATA,42,4294967295,-8388608\r\n")
    assert data.sample_id == 42
    assert data.arduino_micros == ARDUINO_UINT32_MAX
    assert data.raw_adc == -8_388_608

    for malformed in (
        "DATA,1,2",
        "DATA,1,2,3,4",
        "DATA,one,2,3",
        "DATA,1,4294967296,3",
        "DATA,1,2,3.0",
        "HELLO,WRONG,1.0,1.0",
        "HELLO,HX711_NANO,,1.0",
    ):
        parser = parse_hello_line if malformed.startswith("HELLO") else parse_data_line
        with pytest.raises(SerialProtocolError):
            parser(malformed)


def test_port_enumeration_is_sorted_and_never_opens_a_port() -> None:
    ports = enumerate_serial_ports(
        lambda: (
            SimpleNamespace(device="COM10", description="Nano", hwid="USB:2"),
            SimpleNamespace(
                device="COM2",
                description="Arduino Nano",
                hwid="USB:1",
                manufacturer="Arduino",
                product="Nano",
                serial_number="ABC",
            ),
            SimpleNamespace(device="", description="ignored", hwid=""),
        )
    )
    assert [item.device for item in ports] == ["COM10", "COM2"]
    assert ports[1].manufacturer == "Arduino"


def test_startup_reset_flush_and_required_hello_create_physical_connection() -> None:
    service, handle, _, factory = connected_service(calibration_value=calibration())
    assert isinstance(service, SerialServiceInterface)
    assert service.is_connected
    assert not service.simulation_mode
    assert service.connection_info.device_name == "HX711_NANO"
    assert service.connection_info.device_session_id == "hx711-connection-0"
    assert service.diagnostics()["streaming"] is True
    assert handle.writes == [b"START\n"]
    assert handle.reset_input_calls == handle.reset_output_calls == 1
    assert factory.calls[0] == {
        "port": "COM7",
        "baudrate": 115200,
        "timeout": 0.01,
        "write_timeout": 0.1,
    }


def test_startup_not_ready_health_row_does_not_reject_start_handshake() -> None:
    service, handle, _, _ = connected_service(
        calibration_value=calibration(),
        startup_lines=(
            b"HELLO,HX711_NANO,1.0,1.0.0\r\n",
            b"ERROR,HX711_NOT_READY\r\n",
        ),
    )
    assert service.is_connected
    assert service.diagnostics()["streaming"] is True
    assert service.diagnostics()["readiness_error_count"] == 1
    assert handle.writes == [b"START\n"]


@pytest.mark.parametrize(
    "startup_lines",
    (
        (b"-83421\r\n",),
        (b"RAW:-83421\n",),
        (b"raw=-83421\r\n",),
        (b"12543,-83421\r\n",),
        (b"Arduino Nano reset\r\n", b"HX711 starting\r\n", b"-83421\r\n"),
    ),
)
def test_numeric_sensor_stream_validates_without_hello_and_keeps_first_sample(
    startup_lines: tuple[bytes, ...],
) -> None:
    service, handle, _, _ = connected_service(
        calibration_value=calibration(), startup_lines=startup_lines
    )
    assert service.connection_info.device_name == "HX711_STREAM"
    assert handle.writes == []
    sample = service.read_sample()
    assert sample is not None and sample.raw_adc == -83421
    assert not sample.arduino_timestamp_available or startup_lines[-1].startswith(
        b"12543,"
    )


def test_configured_startup_delay_and_serial_framing_are_applied() -> None:
    clock = FakeClock()
    factory = SerialFactory(clock, startup_lines=(b"RAW:100\r\n",))
    service = SerialService(
        serial_factory=factory,
        monotonic_ns=clock,
        wall_clock=lambda: datetime.now(timezone.utc),
        sleeper=clock.sleep,
        read_timeout_s=0.02,
    )
    config = LoadCellConfig(
        baud_rate=9600,
        data_bits=7,
        parity="E",
        stop_bits=2,
        read_timeout_s=0.02,
        startup_delay_s=2.0,
        startup_window_s=0.1,
    )
    before = clock.now_ns
    service.connect(config, port="COM12")
    assert clock.now_ns - before >= 2_000_000_000
    assert factory.calls[0] == {
        "port": "COM12",
        "baudrate": 9600,
        "timeout": 0.02,
        "write_timeout": 1.0,
        "bytesize": 7,
        "parity": "E",
        "stopbits": 2,
    }


def test_nonmeasurement_startup_text_fails_within_window_and_closes_owner() -> None:
    clock = FakeClock()
    factory = SerialFactory(
        clock,
        startup_lines=(b"HELLO,OTHER_DEVICE,1.0,1.0\r\n",),
    )
    service = SerialService(
        serial_factory=factory,
        monotonic_ns=clock,
        wall_clock=lambda: datetime.now(timezone.utc),
        sleeper=clock.sleep,
        read_timeout_s=0.01,
        command_timeout_s=0.05,
        poll_interval_s=0.005,
    )
    with pytest.raises(SerialHandshakeError, match="selected parser"):
        service.connect(load_config(), port="COM8")
    assert not service.is_connected
    assert factory.handles[0].close_calls == 1


def test_all_commands_are_newline_terminated_and_time_bounded() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    assert service.send_command("PING") == "PONG,HX711_NANO"
    assert service.send_command("START") == "OK,START"
    assert service.send_command("STATUS") == "STATUS,STREAMING,READY,7"
    assert service.send_command("STOP") == "OK,STOP"
    assert handle.writes == [
        b"START\n",
        b"PING\n",
        b"START\n",
        b"STATUS\n",
        b"STOP\n",
    ]
    assert service.diagnostics()["command_count"] == 5

    handle.command_responses[b"PING\n"] = ()
    with pytest.raises(SerialCommandError, match="timed out"):
        service.send_command("PING")
    assert service.diagnostics()["command_timeout_count"] == 1


def test_asynchronous_hx711_timeout_during_command_is_health_not_rejection() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    handle.command_responses[b"PING\n"] = (
        b"ERROR,HX711_TIMEOUT\r\n",
        b"PONG,HX711_NANO\r\n",
    )

    assert service.send_command("PING") == "PONG,HX711_NANO"
    assert service.diagnostics()["timeout_error_count"] == 1


def test_valid_data_captures_receipt_clock_and_applies_signed_calibration() -> None:
    service, handle, clock, _ = connected_service(
        calibration_value=calibration(counts_per_gram=-100.0)
    )
    service.set_elapsed_origin_ns(clock.now_ns)
    handle.queue(b"DATA,0,12345,900\r\n")
    sample = service.read_sample()
    assert sample is not None
    assert sample.host_monotonic_ns > 1_000_000_000
    assert sample.elapsed_time_s == pytest.approx(0.01)
    assert sample.wall_clock_iso.startswith("2026-08-03T00:00:00")
    assert sample.raw_adc == 900
    assert sample.force_gf == pytest.approx(1.0)
    assert sample.force_N == pytest.approx(0.00980665)
    assert sample.loadcell_valid
    assert sample.error_code is ErrorCode.NONE


def test_malformed_readiness_and_timeout_lines_do_not_hide_next_valid_sample() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    handle.queue(
        b"garbage\r\n",
        b"DATA,not-an-id,10,1000\r\n",
        b"ERROR,HX711_NOT_READY\r\n",
        b"ERROR,HX711_TIMEOUT\r\n",
        b"DATA,0,10,1000\r\n",
    )
    sample = service.read_sample()
    assert sample is not None and sample.arduino_sample_id == 0
    diagnostics = service.diagnostics()
    assert diagnostics["malformed_line_count"] == 2
    assert diagnostics["readiness_error_count"] == 1
    assert diagnostics["timeout_error_count"] == 1


def test_partial_timeout_line_is_rejected_and_visible_in_raw_monitor_log() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    handle.queue(b"RAW:12")
    assert service.read_sample() is None
    diagnostics = service.diagnostics()
    assert diagnostics["malformed_line_count"] == 1
    assert any(
        "partial line timed out" in str(item.get("message", ""))
        for item in diagnostics["raw_log_entries"]
    )


def test_duplicate_and_sample_id_collision_are_rejected_as_not_new_conversions() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    handle.queue(
        b"DATA,0,100,1000\r\n",
        b"DATA,0,100,1000\r\n",
        b"DATA,0,200,1001\r\n",
        b"DATA,1,300,1100\r\n",
    )
    first = service.read_sample()
    second = service.read_sample()
    assert first is not None and first.arduino_sample_id == 0
    assert second is not None and second.arduino_sample_id == 1
    diagnostics = service.diagnostics()
    assert diagnostics["valid_sample_count"] == 2
    assert diagnostics["duplicate_sample_count"] == 1
    assert diagnostics["sample_id_collision_count"] == 1
    assert diagnostics["stale_sample_count"] == 2


def test_sample_id_reset_creates_new_device_session_but_rollover_does_not() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    handle.queue(
        b"DATA,10,1000,1000\r\n",
        b"DATA,11,2000,1100\r\n",
        b"DATA,0,100,1200\r\n",
    )
    before = service.read_sample()
    middle = service.read_sample()
    after_reset = service.read_sample()
    assert before is not None and middle is not None and after_reset is not None
    assert before.device_session_id == middle.device_session_id
    assert after_reset.device_session_id != before.device_session_id
    assert after_reset.arduino_micros_unwrapped == 100
    assert service.diagnostics()["device_session_reset_count"] == 1

    rollover_service, rollover_handle, _, _ = connected_service(
        calibration_value=calibration()
    )
    rollover_handle.queue(
        f"DATA,0,{ARDUINO_UINT32_MAX - 5},1000\r\n".encode(),
        b"DATA,1,7,1100\r\n",
    )
    high = rollover_service.read_sample()
    rolled = rollover_service.read_sample()
    assert high is not None and rolled is not None
    assert high.device_session_id == rolled.device_session_id
    assert rolled.arduino_micros_unwrapped == 2**32 + 7
    assert rollover_service.diagnostics()["micros_rollover_count"] == 1


def test_unsolicited_hello_marks_board_reset_and_restarts_sample_tracking() -> None:
    service, handle, clock, _ = connected_service(calibration_value=calibration())
    origin_ns = clock.now_ns
    service.set_elapsed_origin_ns(origin_ns)
    handle.queue(
        b"DATA,8,1000,1000\r\n",
        b"HELLO,HX711_NANO,1.0,1.0.1\r\n",
        b"DATA,0,50,1100\r\n",
    )
    before = service.read_sample()
    after = service.read_sample()
    assert before is not None and after is not None
    assert after.device_session_id != before.device_session_id
    assert handle.writes == [b"START\n", b"START\n"]
    assert after.elapsed_time_s == pytest.approx(
        (after.host_monotonic_ns - origin_ns) / 1e9
    )
    assert service.connection_info.firmware_version == "1.0.1"
    diagnostics = service.diagnostics()
    assert diagnostics["board_reset_count"] == 1
    assert diagnostics["device_session_reset_count"] == 1
    assert diagnostics["command_count"] == 2
    assert diagnostics["streaming"] is True


def test_connection_fails_closed_when_start_acknowledgement_is_missing() -> None:
    clock = FakeClock()
    factory = SerialFactory(clock)
    service = SerialService(
        serial_factory=factory,
        monotonic_ns=clock,
        wall_clock=lambda: datetime.now(timezone.utc),
        sleeper=clock.sleep,
        read_timeout_s=0.01,
        command_timeout_s=0.05,
        poll_interval_s=0.005,
    )
    handle = factory(port="COM7", baudrate=115200, timeout=0.01, write_timeout=0.05)
    handle.command_responses[b"START\n"] = ()
    factory.handles.clear()

    def returns_configured_handle(**kwargs):
        handle.kwargs = kwargs
        handle.is_open = True
        factory.handles.append(handle)
        return handle

    service._serial_factory = returns_configured_handle
    with pytest.raises(SerialCommandError, match="timed out waiting for START"):
        service.connect(load_config(), port="COM7")
    assert not service.is_connected
    assert not service.diagnostics()["streaming"]
    assert handle.close_calls == 1


def test_board_reset_restart_failure_disconnects_instead_of_accepting_data() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    handle.command_responses[b"START\n"] = ()
    handle.queue(
        b"HELLO,HX711_NANO,1.0,1.0.1\r\n",
        b"DATA,0,50,1100\r\n",
    )

    with pytest.raises(SerialDisconnectedError, match="failed to restart stream"):
        service.read_sample()

    assert not service.is_connected
    assert handle.close_calls == 1


def test_board_reset_restart_write_failure_counts_one_disconnect_event() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    handle.fail_on_write = True
    handle.queue(b"HELLO,HX711_NANO,1.0,1.0.1\r\n")

    with pytest.raises(SerialDisconnectedError, match="failed to restart stream"):
        service.read_sample()

    assert not service.is_connected
    assert handle.close_calls == 1
    assert service.diagnostics()["disconnect_error_count"] == 1


def test_uncalibrated_raw_sample_is_preserved_with_nan_force_and_error_flag() -> None:
    service, handle, _, _ = connected_service(calibration_value=None)
    handle.queue(b"DATA,0,100,123456\r\n")
    sample = service.read_sample()
    assert sample is not None
    assert sample.raw_adc == 123456
    assert math.isnan(sample.force_gf) and math.isnan(sample.force_N)
    assert not sample.loadcell_valid
    assert sample.error_code is ErrorCode.LOAD_CELL_UNCALIBRATED
    assert service.diagnostics()["uncalibrated_sample_count"] == 1


def test_disconnect_during_read_closes_owner_and_diagnostics_report_intervals() -> None:
    service, handle, _, _ = connected_service(calibration_value=calibration())
    handle.queue(
        b"DATA,0,100,1000\r\n",
        b"DATA,1,200,1100\r\n",
        b"DATA,2,300,1200\r\n",
    )
    assert service.read_sample() is not None
    assert service.read_sample() is not None
    assert service.read_sample() is not None
    diagnostics = service.diagnostics()
    assert diagnostics["measured_sample_rate_hz"] == pytest.approx(100.0)
    assert diagnostics["minimum_sample_interval_ms"] == pytest.approx(10.0)
    assert diagnostics["median_sample_interval_ms"] == pytest.approx(10.0)
    assert diagnostics["maximum_sample_interval_ms"] == pytest.approx(10.0)

    handle.fail_on_read = True
    with pytest.raises(SerialDisconnectedError, match="device removed"):
        service.read_sample()
    assert not service.is_connected
    assert handle.close_calls == 1
    assert service.diagnostics()["disconnect_error_count"] == 1
    service.disconnect()
    service.disconnect()


def test_canonical_csv_playback_preserves_every_recorded_field_and_timestamp(
    tmp_path: Path,
) -> None:
    from core.models import LoadCellSample

    samples = (
        LoadCellSample(
            host_monotonic_ns=10_000_000_000,
            arduino_sample_id=7,
            arduino_micros=123,
            arduino_micros_unwrapped=123,
            raw_adc=-55,
            force_gf=1.25,
            force_N=0.0122583125,
            device_session_id="recorded-device-1",
            elapsed_time_s=0.25,
            wall_clock_iso="2026-08-03T01:02:03+00:00",
            loadcell_valid=True,
            error_code=ErrorCode.NONE,
        ),
        LoadCellSample(
            host_monotonic_ns=10_020_000_000,
            arduino_sample_id=8,
            arduino_micros=20_123,
            arduino_micros_unwrapped=20_123,
            raw_adc=145,
            force_gf=3.25,
            force_N=0.0318716125,
            device_session_id="recorded-device-1",
            elapsed_time_s=0.27,
            wall_clock_iso="2026-08-03T01:02:03.020000+00:00",
            loadcell_valid=True,
            error_code=ErrorCode.NONE,
        ),
    )
    source_path = tmp_path / "loadcell_raw.csv"
    atomic_write_csv(
        source_path,
        LOADCELL_RAW_COLUMNS,
        (loadcell_sample_to_row(sample) for sample in samples),
    )
    playback = SerialPlaybackSource(source_path)
    info = playback.connect(load_config(), port="COM99")
    assert isinstance(playback, SerialServiceInterface)
    assert playback.simulation_mode and info.simulation_mode
    assert info.device_name == "HX711_PLAYBACK"
    assert info.port == "PLAYBACK:loadcell_raw.csv"
    assert playback.enumerate_ports() == ()

    restored = (playback.read_sample(), playback.read_sample())
    assert restored == samples
    assert playback.read_sample() is None
    diagnostics = playback.diagnostics()
    assert diagnostics["source_type"] == "canonical_csv"
    assert diagnostics["source_row_count"] == diagnostics["valid_sample_count"] == 2
    assert diagnostics["measured_sample_rate_hz"] == pytest.approx(50.0)


def test_protocol_text_playback_is_simulated_calibrated_and_skips_stale_rows(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "hx711_capture.txt"
    source_path.write_text(
        "\n".join(
            (
                "# recorded Arduino stream",
                "HELLO,HX711_NANO,1.0,1.0.0",
                "ERROR,HX711_NOT_READY",
                "ERROR,HX711_TIMEOUT",
                "bad line",
                "DATA,0,100,1000",
                "DATA,0,100,1000",
                "DATA,1,200,1100",
            )
        )
        + "\n",
        encoding="ascii",
    )
    clock = FakeClock()
    playback = SerialPlaybackSource(
        source_path,
        calibration(),
        monotonic_ns=clock,
        wall_clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    info = playback.connect(load_config())
    assert info.simulation_mode
    assert info.device_name == "HX711_PLAYBACK"
    assert playback.send_command("STOP") == "OK,STOP"
    assert playback.read_sample() is None
    assert playback.send_command("START") == "OK,START"
    clock.advance(0.01)
    first = playback.read_sample()
    clock.advance(0.01)
    second = playback.read_sample()
    assert first is not None and second is not None
    assert (first.arduino_sample_id, second.arduino_sample_id) == (0, 1)
    assert first.force_gf == pytest.approx(0.0)
    assert second.force_gf == pytest.approx(1.0)
    assert first.host_monotonic_ns != second.host_monotonic_ns
    assert playback.read_sample() is None
    diagnostics = playback.diagnostics()
    assert diagnostics["source_type"] == "protocol_text"
    assert diagnostics["malformed_line_count"] == 2  # bad line + duplicate DATA
    assert diagnostics["readiness_error_count"] == 1
    assert diagnostics["timeout_error_count"] == 1
    assert diagnostics["valid_sample_count"] == 2


def test_playback_rejects_noncanonical_csv_and_missing_source(tmp_path: Path) -> None:
    missing = SerialPlaybackSource(tmp_path / "missing.csv")
    with pytest.raises(FileNotFoundError):
        missing.connect(load_config())

    invalid = tmp_path / "invalid.csv"
    invalid.write_text("sample_id,raw\n0,1\n", encoding="utf-8")
    with pytest.raises(SerialProtocolError, match="header"):
        SerialPlaybackSource(invalid).connect(load_config())
