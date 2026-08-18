"""Operator-facing Ender 3 connection, jog, press-zero, and cycle controls."""

from __future__ import annotations

from dataclasses import asdict
import math
from typing import Mapping

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.models import PrinterConfig
from services.repeated_press_controller import RepeatedPressConfig, SequenceResult


class PrinterMotionTab(QWidget):
    refresh_ports_requested = Signal()
    connect_requested = Signal(str, int)
    disconnect_requested = Signal()
    home_requested = Signal()
    query_position_requested = Signal()
    jog_requested = Signal(str, float, float)
    set_press_zero_requested = Signal()
    clear_press_zero_requested = Signal()
    preview_requested = Signal(object)
    test_cycle_requested = Signal(object)
    start_requested = Signal(object)
    pause_requested = Signal()
    resume_requested = Signal()
    stop_requested = Signal()
    abort_requested = Signal()
    emergency_stop_requested = Signal()
    save_profile_requested = Signal(object)
    load_profile_requested = Signal()

    def __init__(self, config: PrinterConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._connected = False
        self._verified = False
        self._sequence_running = False
        self._operation_busy = False
        self._position_valid = False
        self._homed = False
        self._press_zero_valid = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("printer_motion_scroll_area")
        self.scroll_area.setWidgetResizable(True)
        content = QWidget()
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)
        root = QVBoxLayout(content)

        connection = QGroupBox("Independent Printer Serial Connection")
        self.connection_group = connection
        grid = QGridLayout(connection)
        self.port_combo = QComboBox()
        self.port_combo.setObjectName("printer_port_combo")
        self.port_combo.setEditable(True)
        self.port_combo.addItem(config.port)
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(1200, 4_000_000)
        self.baud_spin.setValue(config.baud_rate)
        self.refresh_button = QPushButton("Refresh Ports")
        self.connect_button = QPushButton("Connect and Verify M115")
        self.disconnect_button = QPushButton("Disconnect")
        self.connection_label = QLabel("Disconnected")
        self.firmware_label = QLabel("Firmware: not verified")
        self.connection_label.setWordWrap(True)
        self.firmware_label.setWordWrap(True)
        grid.addWidget(QLabel("Printer port"), 0, 0)
        grid.addWidget(self.port_combo, 0, 1)
        grid.addWidget(QLabel("Baud"), 0, 2)
        grid.addWidget(self.baud_spin, 0, 3)
        connection_buttons = QHBoxLayout()
        connection_buttons.addWidget(self.refresh_button)
        connection_buttons.addWidget(self.connect_button)
        connection_buttons.addWidget(self.disconnect_button)
        grid.addLayout(connection_buttons, 1, 0, 1, 4)
        grid.addWidget(self.connection_label, 2, 0, 1, 4)
        grid.addWidget(self.firmware_label, 3, 0, 1, 4)
        root.addWidget(connection)

        position = QGroupBox("Homing, Position, Jogging, and Software Press Zero")
        self.position_group = position
        pos_grid = QGridLayout(position)
        self.position_label = QLabel("Tracked X/Y/Z: unavailable | Reported X/Y/Z: unavailable")
        self.homed_label = QLabel("Homed: no")
        self.operation_label = QLabel("Printer action: idle")
        self.operation_label.setObjectName("printer_operation_status")
        self.zero_label = QLabel("Press zero: invalid (never persisted; no G92)")
        self.position_label.setWordWrap(True)
        self.zero_label.setWordWrap(True)
        self.home_button = QPushButton("Home All Axes (explicit G28)")
        self.query_button = QPushButton("Query Position (M114)")
        self.set_zero_button = QPushButton("Set Press Zero Here")
        self.clear_zero_button = QPushButton("Clear Press Zero")
        self.step_combo = QComboBox()
        for step in (10.0, 1.0, 0.1, 0.01):
            self.step_combo.addItem(f"{step:g} mm", step)
        self.xy_feed_spin = self._double_spin(1.0, 12000.0, config.default_xy_feed_mm_min, 10.0, 1)
        self.z_jog_feed_spin = self._double_spin(
            config.minimum_z_feed_mm_min,
            config.maximum_z_feed_mm_min,
            config.default_z_feed_mm_min,
            1.0,
            1,
        )
        pos_grid.addWidget(self.position_label, 0, 0, 1, 5)
        pos_grid.addWidget(self.homed_label, 1, 0, 1, 2)
        pos_grid.addWidget(self.zero_label, 1, 2, 1, 3)
        pos_grid.addWidget(self.operation_label, 2, 0, 1, 5)
        pos_grid.addWidget(self.home_button, 3, 0, 1, 2)
        pos_grid.addWidget(self.query_button, 3, 2, 1, 2)
        pos_grid.addWidget(QLabel("Step"), 4, 0)
        pos_grid.addWidget(self.step_combo, 4, 1)
        pos_grid.addWidget(QLabel("XY feed (mm/min)"), 4, 2)
        pos_grid.addWidget(self.xy_feed_spin, 4, 3)
        pos_grid.addWidget(QLabel("Z feed"), 5, 2)
        pos_grid.addWidget(self.z_jog_feed_spin, 5, 3)
        jog_layout = QGridLayout()
        buttons = {
            (0, 1): ("Y+", "Y", 1),
            (1, 0): ("X-", "X", -1),
            (1, 2): ("X+", "X", 1),
            (2, 1): ("Y-", "Y", -1),
            (0, 4): ("Z+", "Z", 1),
            (1, 4): ("Z-", "Z", -1),
        }
        self.jog_buttons: list[QPushButton] = []
        for (row, column), (text, axis, direction) in buttons.items():
            button = QPushButton(text)
            button.clicked.connect(
                lambda _checked=False, a=axis, d=direction: self._emit_jog(a, d)
            )
            jog_layout.addWidget(button, row, column)
            self.jog_buttons.append(button)
        pos_grid.addLayout(jog_layout, 6, 0, 3, 5)
        pos_grid.addWidget(self.set_zero_button, 9, 0, 1, 2)
        pos_grid.addWidget(self.clear_zero_button, 9, 2, 1, 2)
        root.addWidget(position)

        settings = QGroupBox("Repeated Press Settings")
        self.settings_group = settings
        form = QFormLayout(settings)
        self.cycles_spin = QSpinBox()
        self.cycles_spin.setRange(1, 100_000)
        self.cycles_spin.setValue(3)
        self.displacement_spin = self._double_spin(
            config.minimum_displacement_mm,
            min(-0.001, config.maximum_displacement_mm),
            -1.0,
            0.1,
            3,
        )
        self.down_feed_spin = self._double_spin(config.minimum_z_feed_mm_min, config.maximum_z_feed_mm_min, 60.0, 1.0, 1)
        self.up_feed_spin = self._double_spin(config.minimum_z_feed_mm_min, config.maximum_z_feed_mm_min, 180.0, 1.0, 1)
        self.hold_spin = self._double_spin(0.0, 3600.0, 0.5, 0.1, 3)
        self.dwell_spin = self._double_spin(0.0, 3600.0, 1.0, 0.1, 3)
        self.pre_roll_spin = self._double_spin(0.0, 3600.0, 1.0, 0.1, 3)
        self.post_roll_spin = self._double_spin(0.0, 3600.0, 1.0, 0.1, 3)
        self.force_limit_spin = self._double_spin(
            config.minimum_force_limit_N,
            config.maximum_force_limit_N,
            config.default_force_limit_N,
            0.1,
            3,
        )
        self.tare_checkbox = QCheckBox("Tare load cell before the sequence")
        self.baseline_checkbox = QCheckBox("Capture and accept a fresh optical baseline")
        self.return_checkbox = QCheckBox("Return to press zero after finish/stop")
        self.return_checkbox.setChecked(True)
        self.direction_checkbox = QCheckBox(
            "Negative Z direction physically verified (required)"
        )
        self.direction_checkbox.setToolTip(
            "Confirm on this printer and fixture that a negative machine-Z move lowers the press toward the specimen."
        )
        self.direction_checkbox.setObjectName("negative_z_direction_confirmation")
        form.addRow("Cycles", self.cycles_spin)
        form.addRow("Signed displacement (mm; negative = down)", self.displacement_spin)
        form.addRow("Down feed (mm/min)", self.down_feed_spin)
        form.addRow("Up feed (mm/min)", self.up_feed_spin)
        form.addRow("Bottom hold (s)", self.hold_spin)
        form.addRow("Inter-cycle top dwell (s)", self.dwell_spin)
        form.addRow("Recording pre-roll (s)", self.pre_roll_spin)
        form.addRow("Recording post-roll (s)", self.post_roll_spin)
        form.addRow("Calibrated force limit (N)", self.force_limit_spin)
        form.addRow(self.tare_checkbox)
        form.addRow(self.baseline_checkbox)
        form.addRow(self.return_checkbox)
        form.addRow(self.direction_checkbox)
        profile_row = QHBoxLayout()
        self.save_profile_button = QPushButton("Save Printer Profile")
        self.load_profile_button = QPushButton("Load Printer Profile")
        profile_row.addWidget(self.save_profile_button)
        profile_row.addWidget(self.load_profile_button)
        form.addRow(profile_row)
        root.addWidget(settings)

        preview_group = QGroupBox("Preview, Controls, and Safety")
        preview_layout = QVBoxLayout(preview_group)
        self.preview_label = QLabel(
            "Preview unavailable until Marlin, homing, position, press zero, and direction are valid."
        )
        self.preview_label.setWordWrap(True)
        row = QGridLayout()
        self.preview_button = QPushButton("Preview Motion")
        self.test_button = QPushButton("Test One Cycle")
        self.start_button = QPushButton("Start Repeated Press")
        self.pause_button = QPushButton("Pause at Safe Retract")
        self.resume_button = QPushButton("Resume")
        self.stop_button = QPushButton("Stop + Return")
        self.abort_button = QPushButton("Abort + Retract")
        for index, button in enumerate((
            self.preview_button,
            self.test_button,
            self.start_button,
            self.pause_button,
            self.resume_button,
            self.stop_button,
            self.abort_button,
        )):
            row.addWidget(button, index // 2, index % 2)
        self.emergency_button = QPushButton("EMERGENCY STOP — SEND M112")
        self.emergency_button.setObjectName("printer_emergency_stop")
        self.emergency_button.setStyleSheet(
            "QPushButton { background: #b00020; color: white; font-weight: bold; min-height: 40px; }"
        )
        self.sequence_label = QLabel("Sequence: idle | Cycle: 0/0 | Phase: idle")
        self.safety_label = QLabel("Safety: printer disconnected")
        self.sequence_label.setWordWrap(True)
        self.safety_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_label)
        preview_layout.addLayout(row)
        preview_layout.addWidget(self.sequence_label)
        preview_layout.addWidget(self.safety_label)
        root.addWidget(preview_group)

        log_group = QGroupBox("Printer Command and Response Log")
        log_layout = QVBoxLayout(log_group)
        self.log = QPlainTextEdit()
        self.log.setObjectName("printer_log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)
        self.log.document().setMaximumBlockCount(2000)
        self.clear_log_button = QPushButton("Clear Printer Log")
        log_layout.addWidget(self.log)
        log_layout.addWidget(self.clear_log_button)
        root.addWidget(log_group)
        root.addStretch(1)
        outer.addWidget(self.emergency_button)

        self.refresh_button.clicked.connect(self.refresh_ports_requested)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.home_button.clicked.connect(self.home_requested)
        self.query_button.clicked.connect(self.query_position_requested)
        self.set_zero_button.clicked.connect(self.set_press_zero_requested)
        self.clear_zero_button.clicked.connect(self.clear_press_zero_requested)
        self.preview_button.clicked.connect(lambda: self.preview_requested.emit(self.sequence_config()))
        self.test_button.clicked.connect(self._test_cycle)
        self.start_button.clicked.connect(lambda: self.start_requested.emit(self.sequence_config()))
        self.pause_button.clicked.connect(self.pause_requested)
        self.resume_button.clicked.connect(self.resume_requested)
        self.stop_button.clicked.connect(self.stop_requested)
        self.abort_button.clicked.connect(self.abort_requested)
        self.emergency_button.clicked.connect(self.emergency_stop_requested)
        self.clear_log_button.clicked.connect(self.log.clear)
        self.save_profile_button.clicked.connect(
            lambda: self.save_profile_requested.emit(self.profile_values())
        )
        self.load_profile_button.clicked.connect(self.load_profile_requested)
        self._update_actions()

    @staticmethod
    def _double_spin(
        minimum: float,
        maximum: float,
        value: float,
        step: float,
        decimals: int,
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(float(minimum), float(maximum))
        spin.setDecimals(decimals)
        spin.setSingleStep(float(step))
        spin.setValue(float(value))
        return spin

    def sequence_config(self) -> RepeatedPressConfig:
        return RepeatedPressConfig(
            cycles=self.cycles_spin.value(),
            displacement_mm=self.displacement_spin.value(),
            down_feed_mm_min=self.down_feed_spin.value(),
            up_feed_mm_min=self.up_feed_spin.value(),
            bottom_hold_s=self.hold_spin.value(),
            top_dwell_s=self.dwell_spin.value(),
            pre_roll_s=self.pre_roll_spin.value(),
            post_roll_s=self.post_roll_spin.value(),
            force_limit_N=self.force_limit_spin.value(),
            tare_before_sequence=self.tare_checkbox.isChecked(),
            fresh_baseline_before_sequence=self.baseline_checkbox.isChecked(),
            return_to_press_zero_on_finish=self.return_checkbox.isChecked(),
            negative_z_direction_confirmed=self.direction_checkbox.isChecked(),
        )

    def profile_values(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "printer": asdict(self.config),
            "repeated_press": asdict(self.sequence_config()),
            "press_zero_persisted": False,
        }

    def apply_profile(self, values: Mapping[str, object]) -> None:
        repeated = values.get("repeated_press")
        if not isinstance(repeated, Mapping):
            raise ValueError("printer profile is missing repeated_press settings")
        config = RepeatedPressConfig(**dict(repeated))
        # Validate only finite/control ranges here. Machine/press-zero validation
        # remains a live preview/start interlock.
        self.cycles_spin.setValue(config.cycles)
        self.displacement_spin.setValue(config.displacement_mm)
        self.down_feed_spin.setValue(config.down_feed_mm_min)
        self.up_feed_spin.setValue(config.up_feed_mm_min)
        self.hold_spin.setValue(config.bottom_hold_s)
        self.dwell_spin.setValue(config.top_dwell_s)
        self.pre_roll_spin.setValue(config.pre_roll_s)
        self.post_roll_spin.setValue(config.post_roll_s)
        self.force_limit_spin.setValue(config.force_limit_N)
        self.tare_checkbox.setChecked(config.tare_before_sequence)
        self.baseline_checkbox.setChecked(config.fresh_baseline_before_sequence)
        self.return_checkbox.setChecked(config.return_to_press_zero_on_finish)
        # Direction is a per-run operator observation, never trusted from disk.
        self.direction_checkbox.setChecked(False)
        self.preview_label.setText(
            "Profile loaded. Press zero was not loaded; re-establish position/zero and reconfirm negative-Z direction."
        )

    def set_ports(self, rows: object) -> None:
        selected = self.port_combo.currentText().strip() or self.config.port
        devices = [str(row.get("device", "")) for row in rows if isinstance(row, Mapping)]
        self.port_combo.clear()
        self.port_combo.addItems([item for item in devices if item])
        if selected and self.port_combo.findText(selected) < 0:
            self.port_combo.addItem(selected)
        self.port_combo.setCurrentText(selected)

    def set_connected(self, info: Mapping[str, object]) -> None:
        self._connected = True
        self._verified = bool(info.get("marlin_verified", False))
        self.connection_label.setText(
            f"Connected: {info.get('port')} @ {info.get('baud_rate')} (independent printer port)"
        )
        self.firmware_label.setText(
            f"Firmware: {info.get('firmware_name') or 'unknown'} {info.get('firmware_version') or ''} | Marlin verified: {'yes' if self._verified else 'no'}"
        )
        self._update_actions()

    def set_disconnected(self) -> None:
        self._connected = self._verified = self._position_valid = self._homed = self._press_zero_valid = False
        self._sequence_running = False
        self._operation_busy = False
        self.connection_label.setText("Disconnected")
        self.firmware_label.setText("Firmware: not verified")
        self.position_label.setText("Tracked X/Y/Z: unavailable | Reported X/Y/Z: unavailable")
        self.homed_label.setText("Homed: no")
        self.operation_label.setText("Printer action: idle")
        self.zero_label.setText("Press zero: invalid (never persisted; no G92)")
        self._update_actions()

    def update_printer_state(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        position = payload.get("position")
        if isinstance(position, Mapping):
            self._position_valid = bool(position.get("tracked_valid", False))
            self._homed = bool(position.get("homed", False))
            self._press_zero_valid = bool(position.get("press_zero_valid", False))
            tracked = self._xyz_text(position, "tracked")
            reported = self._xyz_text(position, "reported")
            self.position_label.setText(f"Tracked X/Y/Z: {tracked} | Reported X/Y/Z: {reported}")
            self.homed_label.setText(f"Homed: {'yes' if self._homed else 'no'}")
            zero_x = position.get("press_zero_x_mm", position.get("tracked_x_mm", math.nan))
            zero_y = position.get("press_zero_y_mm", position.get("tracked_y_mm", math.nan))
            zero_z = position.get("press_zero_z_mm", math.nan)
            self.zero_label.setText(
                "Mechanical Press Zero: "
                f"X={float(zero_x):.3f}, Y={float(zero_y):.3f}, Z={float(zero_z):.3f} mm "
                "(software only; no G92)"
                if self._press_zero_valid
                else "Press zero: invalid (never persisted; no G92)"
            )
        self._update_actions()

    def update_operation(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        self._operation_busy = bool(payload.get("busy", False))
        operation = str(payload.get("operation") or "printer action")
        error = str(payload.get("error") or "")
        if self._operation_busy:
            self.operation_label.setText(f"Printer action: {operation} — waiting for Marlin completion…")
        elif error:
            self.operation_label.setText(f"Printer action failed: {operation}")
        else:
            self.operation_label.setText(f"Printer action complete: {operation}")
        self._update_actions()

    def update_command(self, payload: object) -> None:
        if isinstance(payload, Mapping):
            self.append_log(
                {
                    "level": "command",
                    "message": (
                        f"{payload.get('command_id')} {payload.get('state')}: "
                        f"{payload.get('gcode')} {payload.get('error') or ''}"
                    ),
                }
            )

    def update_sequence(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        event_type = str(payload.get("type", ""))
        if event_type == "preview":
            self.preview_label.setText(
                "Computed motion: Mechanical Zero X/Y/Z={press_zero_x_mm:.3f}/{press_zero_y_mm:.3f}/{press_zero_z_mm:.3f} mm → target Z={target_machine_z_mm:.3f} mm; "
                "{cycles} cycle(s), estimated {estimated_duration_s:.2f} s, force limit {force_limit_N:.3f} N.".format(
                    **{key: float(value) if key not in {"cycles"} else int(value) for key, value in payload.items() if key != "type"}
                )
            )
            return
        if event_type == "started":
            self._sequence_running = True
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, Mapping):
            self._sequence_running = str(payload.get("state")) not in {"complete", "error", "idle"}
            self.sequence_label.setText(
                f"Sequence: {snapshot.get('sequence_status')} | Cycle: {snapshot.get('motion_cycle_index')}/"
                f"{snapshot.get('motion_total_cycles')} | Phase: {snapshot.get('motion_phase')}"
            )
            self.safety_label.setText(
                f"Safety: force limit {snapshot.get('force_limit_N')} N | exceeded: "
                f"{snapshot.get('force_limit_exceeded')} | zero valid: {snapshot.get('press_zero_valid')}"
            )
        self._update_actions()

    def sequence_finished(self, result: SequenceResult | object) -> None:
        values = result.to_dict() if isinstance(result, SequenceResult) else dict(result)
        self._sequence_running = False
        self.sequence_label.setText(
            f"Sequence: {values.get('status')} | Completed {values.get('completed_cycles')}/"
            f"{values.get('configured_cycles')} cycles"
        )
        self.safety_label.setText(
            f"Safety: force limit exceeded={values.get('force_limit_exceeded')}; "
            f"max |force|={values.get('maximum_abs_force_N')} N; {values.get('error') or 'no error'}"
        )
        self._update_actions()

    def set_sequence_preparation_status(
        self, message: str, *, error: bool = False
    ) -> None:
        prefix = "Sequence preparation failed" if error else "Sequence preparation"
        self.sequence_label.setText(f"{prefix}: {message}")

    def append_log(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        level = str(payload.get("level", "info"))
        message = str(payload.get("message", ""))
        cursor = self.log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        colors = {
            "error": "#c62828",
            "warning": "#ef6c00",
            "emergency": "#b00020",
            "send": "#1565c0",
            "receive": "#2e7d32",
            "command": "#6a1b9a",
        }
        char_format = QTextCharFormat()
        char_format.setForeground(QColor(colors.get(level, "#444444")))
        cursor.insertText(f"[{level.upper()}] {message}\n", char_format)
        self.log.setTextCursor(cursor)

    def show_error(self, message: str) -> None:
        self.append_log({"level": "error", "message": message})
        self.safety_label.setText(f"Safety/error: {message}")

    def _connect(self) -> None:
        self.connect_requested.emit(self.port_combo.currentText().strip(), self.baud_spin.value())

    def _emit_jog(self, axis: str, direction: int) -> None:
        step = float(self.step_combo.currentData()) * int(direction)
        feed = self.xy_feed_spin.value() if axis in {"X", "Y"} else self.z_jog_feed_spin.value()
        self.jog_requested.emit(axis, step, feed)

    def _test_cycle(self) -> None:
        values = asdict(self.sequence_config())
        values["cycles"] = 1
        self.test_cycle_requested.emit(RepeatedPressConfig(**values))

    @staticmethod
    def _xyz_text(position: Mapping[str, object], prefix: str) -> str:
        values = [float(position.get(f"{prefix}_{axis}_mm", math.nan)) for axis in "xyz"]
        return "/".join(f"{value:.3f}" if math.isfinite(value) else "—" for value in values)

    def _update_actions(self) -> None:
        motion_ready = self._connected and self._verified and self._homed and self._position_valid
        zero_ready = motion_ready and self._press_zero_valid
        idle = not self._sequence_running and not self._operation_busy
        self.connect_button.setEnabled(not self._connected and idle)
        self.disconnect_button.setEnabled(self._connected and idle)
        self.home_button.setEnabled(self._connected and self._verified and idle)
        self.query_button.setEnabled(self._connected and self._verified and idle)
        for button in self.jog_buttons:
            button.setEnabled(motion_ready and idle)
        self.set_zero_button.setEnabled(motion_ready and idle)
        self.clear_zero_button.setEnabled(self._press_zero_valid and idle)
        self.preview_button.setEnabled(zero_ready and idle)
        self.test_button.setEnabled(zero_ready and idle)
        self.start_button.setEnabled(zero_ready and idle)
        self.pause_button.setEnabled(self._sequence_running)
        self.resume_button.setEnabled(self._sequence_running)
        self.stop_button.setEnabled(self._sequence_running)
        self.abort_button.setEnabled(self._sequence_running)
        self.emergency_button.setEnabled(self._connected)
        self.connection_group.setEnabled(idle)
        # Keep the operation label readable while its sibling controls are disabled.
        self.position_group.setEnabled(not self._sequence_running)
        self.home_button.setEnabled(self.home_button.isEnabled() and idle)
        self.query_button.setEnabled(self.query_button.isEnabled() and idle)
        self.settings_group.setEnabled(idle)


__all__ = ["PrinterMotionTab"]
