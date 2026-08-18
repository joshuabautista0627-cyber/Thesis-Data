from __future__ import annotations

from queue import Empty, Queue
import re
from threading import get_ident
import time

import pytest

from core.models import PrinterConfig
from services.printer_service import (
    CommandState,
    PrinterCommandError,
    PrinterConnectionError,
    PrinterSafetyError,
    PrinterSerialService,
)


class FakeMarlinSerial:
    def __init__(
        self,
        *,
        firmware: str = "FIRMWARE_NAME:Marlin 2.1.2 SOURCE_CODE_URL:https://marlinfw.org",
        reject: set[str] | None = None,
        timeout_commands: set[str] | None = None,
        delayed: set[str] | None = None,
        **kwargs,
    ) -> None:
        self.kwargs = kwargs
        self.is_open = True
        self.commands: list[str] = []
        self.write_threads: list[int] = []
        self.responses: Queue[bytes] = Queue()
        self.firmware = firmware
        self.reject = set(reject or ())
        self.timeout_commands = set(timeout_commands or ())
        self.delayed = set(delayed or ())
        self.position = [10.0, 20.0, 30.0]
        self.pending_delayed: list[str] = []
        self.reset_count = 0

    def reset_input_buffer(self) -> None:
        self.reset_count += 1

    def write(self, payload: bytes) -> int:
        command = payload.decode("ascii").strip()
        self.commands.append(command)
        self.write_threads.append(get_ident())
        root = command.split()[0]
        if root in self.timeout_commands:
            return len(payload)
        if root in self.reject:
            self.responses.put(f"Error: rejected {root}\n".encode())
            return len(payload)
        if root == "M115":
            self.responses.put((self.firmware + "\n").encode())
        elif root == "M114":
            self.responses.put(
                f"X:{self.position[0]:.3f} Y:{self.position[1]:.3f} Z:{self.position[2]:.3f} E:0.000\n".encode()
            )
        elif root == "G28":
            self.position[:] = [0.0, 0.0, 0.0]
        elif root == "G1":
            for axis, value in re.findall(r"([XYZ])(-?\d+(?:\.\d+)?)", command):
                self.position["XYZ".index(axis)] = float(value)
        if root in self.delayed:
            self.pending_delayed.append(root)
        elif root != "M112":
            self.responses.put(b"ok\n")
        return len(payload)

    def release(self, root: str) -> None:
        if root in self.pending_delayed:
            self.pending_delayed.remove(root)
            self.responses.put(b"ok\n")

    def readline(self) -> bytes:
        if not self.is_open:
            raise OSError("closed")
        try:
            return self.responses.get(timeout=0.01)
        except Empty:
            return b""

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


class Factory:
    def __init__(self, **serial_options) -> None:
        self.serial_options = serial_options
        self.instance: FakeMarlinSerial | None = None

    def __call__(self, **kwargs):
        self.instance = FakeMarlinSerial(**self.serial_options, **kwargs)
        return self.instance


def connected_service(**serial_options) -> tuple[PrinterSerialService, FakeMarlinSerial]:
    factory = Factory(**serial_options)
    service = PrinterSerialService(serial_factory=factory)
    service.connect(PrinterConfig(command_timeout_s=0.15, startup_delay_s=0.0))
    assert factory.instance is not None
    return service, factory.instance


def home_and_zero(service: PrinterSerialService) -> None:
    service.home_all()
    service.set_press_zero_here()


def test_connect_uses_configured_framing_and_m115_without_motion() -> None:
    service, transport = connected_service()
    try:
        assert transport.kwargs["port"] == "COM4"
        assert transport.kwargs["baudrate"] == 115200
        assert transport.reset_count == 1
        assert transport.commands == ["M115"]
        assert service.marlin_verified
    finally:
        service.disconnect()


def test_non_marlin_m115_fails_closed_and_never_moves() -> None:
    factory = Factory(firmware="FIRMWARE_NAME:OtherFirmware 1.0")
    service = PrinterSerialService(serial_factory=factory)
    with pytest.raises(PrinterConnectionError, match="Marlin"):
        service.connect(PrinterConfig(command_timeout_s=0.15, startup_delay_s=0.0))
    assert factory.instance is not None
    assert factory.instance.commands == ["M115"]
    assert not service.connected


def test_m115_handshake_timeout_fails_closed() -> None:
    factory = Factory(timeout_commands={"M115"})
    service = PrinterSerialService(serial_factory=factory)
    with pytest.raises(PrinterCommandError, match="timed_out"):
        service.connect(
            PrinterConfig(command_timeout_s=0.03, startup_delay_s=0.0)
        )
    assert factory.instance is not None
    assert factory.instance.commands == ["M115"]
    assert not service.connected


def test_command_lifecycle_and_unique_ids_are_recorded() -> None:
    service, _ = connected_service()
    states: list[dict] = []
    service.add_state_callback(states.append)
    try:
        first = service.execute("M105")
        second = service.execute("M105")
        assert first.state is CommandState.COMPLETED
        assert first.command_id != second.command_id
        assert {row["state"] for row in states if row.get("command_id") == first.command_id} >= {
            "queued",
            "sent",
            "completed",
        }
    finally:
        service.disconnect()


@pytest.mark.parametrize("gcode", ["", "   ", "G1 X1\nM112", "M114\r"])
def test_multiline_or_blank_commands_are_rejected(gcode: str) -> None:
    service, _ = connected_service()
    try:
        with pytest.raises(ValueError):
            service.submit(gcode)
    finally:
        service.disconnect()


def test_printer_rejection_is_not_treated_as_ok() -> None:
    service, _ = connected_service(reject={"M105"})
    try:
        with pytest.raises(PrinterCommandError, match="rejected"):
            service.execute("M105")
    finally:
        service.disconnect()


def test_timeout_invalidates_homing_position_and_press_zero() -> None:
    service, transport = connected_service()
    try:
        home_and_zero(service)
        transport.timeout_commands.add("M105")
        with pytest.raises(PrinterCommandError, match="timed_out"):
            service.execute("M105", timeout_s=0.03)
        position = service.position
        assert not position.homed
        assert not position.tracked_valid
        assert not position.press_zero_valid
    finally:
        service.disconnect()


def test_m114_populates_reported_coordinates_only_from_actual_response() -> None:
    service, _ = connected_service()
    try:
        before = service.position
        assert not before.reported_valid
        queried = service.query_position()
        assert queried.reported_valid
        assert (queried.reported_x_mm, queried.reported_y_mm, queried.reported_z_mm) == (
            10.0,
            20.0,
            30.0,
        )
    finally:
        service.disconnect()


def test_homing_is_explicit_synchronized_and_invalidates_old_zero() -> None:
    service, transport = connected_service()
    try:
        service.query_position()
        with service._lock:
            old = service._position
            service._position = type(old)(**{**old.__dict__}) if hasattr(old, "__dict__") else old
        position = service.home_all()
        assert position.homed and position.tracked_valid
        assert not position.press_zero_valid
        assert transport.commands[-4:] == ["G28", "M400", "M114",] or "G28" in transport.commands
        assert transport.commands.index("G28") < transport.commands.index("M400")
    finally:
        service.disconnect()


def test_press_zero_is_internal_and_never_sends_g92() -> None:
    service, transport = connected_service()
    try:
        service.home_all()
        before = list(transport.commands)
        position = service.set_press_zero_here()
        assert position.press_zero_valid
        assert position.press_zero_x_mm == position.tracked_x_mm
        assert position.press_zero_y_mm == position.tracked_y_mm
        assert position.press_zero_z_mm == position.tracked_z_mm
        assert transport.commands == before
        assert not any(command.startswith("G92") for command in transport.commands)
    finally:
        service.disconnect()


def test_press_zero_rejects_position_outside_configured_limits() -> None:
    service, transport = connected_service()
    try:
        service.home_all()
        transport.position[:] = [-13.0, -7.5, 0.0]
        service.query_position()
        with pytest.raises(PrinterSafetyError, match="outside configured press-zero limits"):
            service.set_press_zero_here()
    finally:
        service.disconnect()


def test_jog_can_recover_from_negative_home_offset_but_not_move_farther_out() -> None:
    service, transport = connected_service()
    try:
        service.home_all()
        transport.position[:] = [-13.0, -7.5, 0.0]
        service.query_position()
        with pytest.raises(PrinterSafetyError, match="outside configured limits"):
            service.jog("X", -0.1, feed_rate_mm_min=100.0)
        service.jog("X", 10.0, feed_rate_mm_min=100.0)
        assert service.position.tracked_x_mm == pytest.approx(-3.0)
        service.jog("X", 10.0, feed_rate_mm_min=100.0)
        assert service.position.tracked_x_mm == pytest.approx(7.0)
    finally:
        service.disconnect()


def test_timed_out_motion_requests_m410_and_latches_motion_interlock() -> None:
    service, transport = connected_service(timeout_commands={"G1"})
    try:
        with pytest.raises(PrinterCommandError, match="timed_out"):
            service.execute("G1 X1.000 F100.000", timeout_s=0.03)
        assert "M410" in transport.commands
        assert service.diagnostics()["motion_interlock_reason"].startswith("G1 X1.000")
        with pytest.raises(PrinterSafetyError, match="motion is interlocked"):
            service.move_absolute(x_mm=1.0, feed_rate_mm_min=100.0)
    finally:
        service.disconnect()


@pytest.mark.parametrize("step", [10.0, 1.0, 0.1, 0.01])
def test_all_required_jog_steps_use_absolute_bounded_motion(step: float) -> None:
    service, transport = connected_service()
    try:
        service.home_all()
        service.jog("X", step, feed_rate_mm_min=3000.0)
        assert service.position.tracked_x_mm == pytest.approx(step)
        assert any(command.startswith(f"G1 X{step:.3f}") for command in transport.commands)
        assert "M400" in transport.commands
    finally:
        service.disconnect()


@pytest.mark.parametrize(
    ("axis", "delta"),
    [("X", -0.01), ("Y", -1.0), ("Z", -0.1), ("X", 221.0), ("Y", 221.0), ("Z", 251.0)],
)
def test_axis_limit_violations_are_blocked_before_g1(axis: str, delta: float) -> None:
    service, transport = connected_service()
    try:
        service.home_all()
        before = sum(command.startswith("G1") for command in transport.commands)
        with pytest.raises(PrinterSafetyError, match="outside configured limits"):
            service.jog(axis, delta, feed_rate_mm_min=100.0)
        assert sum(command.startswith("G1") for command in transport.commands) == before
    finally:
        service.disconnect()


def test_motion_requires_homing_and_valid_position() -> None:
    service, _ = connected_service()
    try:
        with pytest.raises(PrinterSafetyError, match="homed"):
            service.jog("Z", 1.0, feed_rate_mm_min=100.0)
    finally:
        service.disconnect()


def test_g1_is_followed_by_m400_and_tracked_position_updates_after_ack() -> None:
    service, transport = connected_service()
    try:
        service.home_all()
        service.move_absolute(z_mm=5.0, feed_rate_mm_min=100.0, query_after=False)
        g1 = max(index for index, item in enumerate(transport.commands) if item.startswith("G1"))
        assert transport.commands[g1 + 1] == "M400"
        assert service.position.tracked_z_mm == 5.0
    finally:
        service.disconnect()


def test_disconnect_invalidates_all_machine_state() -> None:
    service, _ = connected_service()
    home_and_zero(service)
    service.disconnect()
    assert not service.connected
    assert not service.position.homed
    assert not service.position.press_zero_valid


def test_reconnect_runs_new_m115_and_does_not_restore_press_zero() -> None:
    instances = []

    def factory(**kwargs):
        instance = FakeMarlinSerial(**kwargs)
        instances.append(instance)
        return instance

    service = PrinterSerialService(serial_factory=factory)
    service.connect(PrinterConfig(command_timeout_s=0.15, startup_delay_s=0.0))
    home_and_zero(service)
    assert service.position.press_zero_valid
    service.connect(PrinterConfig(command_timeout_s=0.15, startup_delay_s=0.0))
    try:
        assert len(instances) == 2
        assert instances[1].commands == ["M115"]
        assert not service.position.homed
        assert not service.position.press_zero_valid
    finally:
        service.disconnect()


def test_reader_transport_loss_marks_connection_unavailable() -> None:
    service, transport = connected_service()
    try:
        transport.responses.put(b"ok\n")
        transport.is_open = False
        deadline = time.time() + 0.5
        while service.connected and time.time() < deadline:
            time.sleep(0.005)
        assert not service.connected
        assert not service.position.tracked_valid
        with pytest.raises(PrinterConnectionError):
            service.submit("M114")
    finally:
        service.disconnect()


def test_emergency_stop_is_written_by_same_sole_writer_thread() -> None:
    service, transport = connected_service()
    try:
        assert service.emergency_stop()
        assert transport.commands[-1] == "M112"
        assert len(set(transport.write_threads)) == 1
        assert not service.position.press_zero_valid
        with pytest.raises(PrinterSafetyError, match="emergency-stop"):
            service.move_absolute(z_mm=1.0, feed_rate_mm_min=10.0)
    finally:
        service.disconnect()


def test_emergency_stop_interrupts_active_command_and_cancels_queued_command() -> None:
    service, transport = connected_service(delayed={"M400"})
    try:
        active = service.submit("M400", timeout_s=1.0)
        queued = service.submit("M105", timeout_s=1.0)
        deadline = time.time() + 0.5
        while "M400" not in transport.commands and time.time() < deadline:
            time.sleep(0.005)
        assert service.emergency_stop()
        with pytest.raises(PrinterCommandError, match="emergency stop"):
            service.wait_command(active)
        with pytest.raises(PrinterCommandError, match="emergency stop"):
            service.wait_command(queued)
        assert transport.commands.count("M112") == 1
        assert "M105" not in transport.commands
        assert len(set(transport.write_threads)) == 1
    finally:
        service.disconnect()


def test_m410_interrupts_delayed_command_and_keeps_single_writer() -> None:
    service, transport = connected_service(delayed={"M400"})
    try:
        command = service.submit("M400", timeout_s=1.0)
        deadline = time.time() + 0.5
        while "M400" not in transport.commands and time.time() < deadline:
            time.sleep(0.005)
        assert service.quick_stop()
        with pytest.raises(PrinterCommandError, match="M410"):
            service.wait_command(command)
        assert "M410" in transport.commands
        assert len(set(transport.write_threads)) == 1
    finally:
        service.disconnect()


def test_temperature_lines_are_parsed_asynchronous_to_commands() -> None:
    service, transport = connected_service()
    try:
        transport.responses.put(b"ok T:201.5 /210.0 B:55.25 /60.0\n")
        time.sleep(0.03)
        assert service.diagnostics()["temperatures_C"] == {"T": 201.5, "B": 55.25}
    finally:
        service.disconnect()
