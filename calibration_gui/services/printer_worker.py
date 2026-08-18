"""Qt boundary for printer commands and the repeated-press controller."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from core.models import PrinterConfig
from core.motion import MotionContext
from services.printer_service import PrinterSerialService, enumerate_printer_ports
from services.repeated_press_controller import (
    RepeatedPressConfig,
    RepeatedPressController,
    SequenceResult,
)


class PrinterWorker(QObject):
    """Keep all blocking printer actions outside the GUI thread."""

    ports_refreshed = Signal(object)
    connected = Signal(object)
    disconnected = Signal()
    log_received = Signal(object)
    state_changed = Signal(object)
    command_changed = Signal(object)
    operation_changed = Signal(object)
    sequence_changed = Signal(object)
    sequence_completed = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        config: PrinterConfig,
        motion_context: MotionContext,
        *,
        tare_callback: Callable[[], None] | None = None,
        baseline_callback: Callable[[], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.motion_context = motion_context
        self._prerequisite_ready = False
        self._prerequisite_reason = "Camera, load cell, baseline, and recording output are not ready."
        self.service = PrinterSerialService()
        self.service.add_log_callback(self.log_received.emit)
        self.service.add_state_callback(self._service_state)
        self.controller = RepeatedPressController(
            self.service,
            motion_context,
            readiness_callback=self._readiness,
            tare_callback=tare_callback,
            baseline_callback=baseline_callback,
            state_callback=self.sequence_changed.emit,
            completed_callback=self._sequence_finished,
        )

    def observe_force_threadsafe(self, sample: object) -> None:
        self.controller.observe_force(sample)

    @Slot()
    def refresh_ports(self) -> None:
        try:
            self.ports_refreshed.emit(enumerate_printer_ports())
        except Exception as exc:
            self.error.emit(str(exc))

    @Slot(str, int)
    def connect_printer(self, port: str, baud_rate: int) -> None:
        operation = f"connecting {str(port).strip()} and verifying M115"
        self.operation_changed.emit({"busy": True, "operation": operation, "error": ""})
        try:
            self.config = replace(self.config, port=str(port), baud_rate=int(baud_rate))
            info = self.service.connect(self.config, port=port)
            self.connected.emit(asdict(info))
        except Exception as exc:
            self.error.emit(str(exc))
            self.disconnected.emit()
            self.operation_changed.emit(
                {"busy": False, "operation": operation, "error": str(exc)}
            )
        else:
            self.operation_changed.emit(
                {"busy": False, "operation": operation, "error": ""}
            )

    @Slot()
    def disconnect_printer(self) -> None:
        operation = "disconnecting printer"
        self.operation_changed.emit({"busy": True, "operation": operation, "error": ""})
        error = ""
        try:
            if self.controller.running:
                self.controller.abort()
                self.controller.wait(2.0)
            self.service.disconnect()
        except Exception as exc:
            error = str(exc)
            self.error.emit(str(exc))
        self.disconnected.emit()
        self.operation_changed.emit(
            {"busy": False, "operation": operation, "error": error}
        )

    @Slot()
    def home_all(self) -> None:
        self._run(self.service.home_all, operation="homing all axes")

    @Slot()
    def query_position(self) -> None:
        self._run(self.service.query_position, operation="querying position")

    @Slot(str, float, float)
    def jog(self, axis: str, delta_mm: float, feed_rate_mm_min: float) -> None:
        self._run(
            self.service.jog,
            str(axis),
            float(delta_mm),
            feed_rate_mm_min=float(feed_rate_mm_min),
            operation=f"jogging {str(axis).upper()} {float(delta_mm):+g} mm",
        )

    @Slot()
    def set_press_zero(self) -> None:
        self._run(self.service.set_press_zero_here, operation="setting Mechanical Press Zero")

    @Slot()
    def clear_press_zero(self) -> None:
        try:
            self.service.invalidate_press_zero("cleared by operator")
        except Exception as exc:
            self.error.emit(str(exc))

    @Slot(object)
    def preview_sequence(self, values: object) -> None:
        try:
            config = values if isinstance(values, RepeatedPressConfig) else RepeatedPressConfig(**dict(values))
            self.sequence_changed.emit({"type": "preview", **self.controller.preview(config)})
        except Exception as exc:
            self.error.emit(str(exc))

    @Slot(object)
    def start_sequence(self, values: object) -> None:
        try:
            config = values if isinstance(values, RepeatedPressConfig) else RepeatedPressConfig(**dict(values))
            generation = self.controller.start(config)
            self.sequence_changed.emit({"type": "started", "generation": generation})
        except Exception as exc:
            self.error.emit(str(exc))

    @Slot()
    def pause_sequence(self) -> None:
        self.controller.pause()

    @Slot()
    def resume_sequence(self) -> None:
        self.controller.resume()

    @Slot()
    def stop_sequence(self) -> None:
        self.controller.stop()

    @Slot()
    def abort_sequence(self) -> None:
        self.controller.abort()

    @Slot()
    def emergency_stop(self) -> None:
        try:
            if not self.controller.emergency_stop():
                raise RuntimeError("M112 was not confirmed as written")
        except Exception as exc:
            self.error.emit(str(exc))

    @Slot(bool, str)
    def set_prerequisites(self, ready: bool, reason: str) -> None:
        self._prerequisite_ready = bool(ready)
        self._prerequisite_reason = str(reason)

    def _readiness(self) -> tuple[bool, str]:
        return self._prerequisite_ready, self._prerequisite_reason

    def _service_state(self, payload: object) -> None:
        if isinstance(payload, dict) and payload.get("type") == "command":
            self.command_changed.emit(payload)
        else:
            self.state_changed.emit(payload)

    def _sequence_finished(self, result: SequenceResult) -> None:
        self.sequence_completed.emit(result)

    def _run(
        self,
        function: Callable[..., object],
        *args: object,
        operation: str = "printer action",
        **kwargs: object,
    ) -> None:
        self.operation_changed.emit(
            {"busy": True, "operation": str(operation), "error": ""}
        )
        try:
            function(*args, **kwargs)
            self.state_changed.emit(
                {
                    "type": "printer",
                    "connected": self.service.connected,
                    "marlin_verified": self.service.marlin_verified,
                    "connection": (
                        asdict(self.service.connection_info)
                        if self.service.connection_info is not None
                        else None
                    ),
                    "position": asdict(self.service.position),
                }
            )
        except Exception as exc:
            self.error.emit(str(exc))
            self.operation_changed.emit(
                {"busy": False, "operation": str(operation), "error": str(exc)}
            )
        else:
            self.operation_changed.emit(
                {"busy": False, "operation": str(operation), "error": ""}
            )


__all__ = ["PrinterWorker"]
