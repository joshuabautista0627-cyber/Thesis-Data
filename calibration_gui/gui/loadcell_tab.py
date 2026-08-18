"""Presentation-only Arduino/HX711 connection and calibration tab."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
import math
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.interfaces import SerialConnectionInfo


def _wrapped_label(text: str, *, selectable: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    if selectable:
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


class LoadCellTab(QWidget):
    """Scrollable load-cell UI whose actions are handled outside the widget."""

    WORKFLOW_STAGES = (
        "disconnected",
        "connected",
        "unloaded",
        "loaded",
        "calculated",
        "tared",
        "verified",
    )

    refresh_ports_requested = Signal()
    connect_requested = Signal(object)
    disconnect_requested = Signal()
    tare_requested = Signal()
    calibration_unloaded_requested = Signal()
    calibration_loaded_requested = Signal(float)
    calibration_calculate_requested = Signal(float)
    verification_requested = Signal(float)
    known_mass_changed = Signal(float)
    calibration_step_requested = Signal(str, float)
    save_calibration_requested = Signal()
    load_calibration_requested = Signal()
    reset_calibration_requested = Signal()
    cancel_calibration_requested = Signal()
    start_calibration_requested = Signal()
    save_serial_log_requested = Signal(object)
    playback_file_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        plot_history_s: float = 30.0,
        max_plot_points: int = 3000,
    ) -> None:
        super().__init__(parent)
        if not math.isfinite(plot_history_s) or plot_history_s <= 0:
            raise ValueError("plot_history_s must be finite and positive")
        if isinstance(max_plot_points, bool) or max_plot_points < 2:
            raise ValueError("max_plot_points must be at least two")
        self.setObjectName("loadcell_tab")
        # The page itself must shrink to the tab viewport; its scroll area
        # exposes content on physically small or scaled displays.
        self.setMinimumSize(0, 0)
        self.plot_history_s = float(plot_history_s)
        self.max_plot_points = int(max_plot_points)
        self._force_times_s: deque[float] = deque(maxlen=self.max_plot_points)
        self._force_values_N: deque[float] = deque(maxlen=self.max_plot_points)
        self._connected = False
        self._stream_ready = False
        self._sample_window_active = False
        self._workflow_job_active = False
        self._controls_locked = False
        self._workflow_stage = "disconnected"
        self._calibration_available = False
        self._verification_passed = False
        self._raw_serial_entries: list[dict[str, object]] = []
        self._last_raw_sequence = 0
        self._lockable_widgets: list[QWidget] = []

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("loadcell_scroll_area")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer_layout.addWidget(self.scroll_area)

        content = QWidget()
        content.setObjectName("loadcell_scroll_content")
        self.scroll_area.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.instruction_label = _wrapped_label(
            "Connect the Arduino Nano and confirm fresh HX711 samples. Then follow "
            "the gated sequence: unloaded window, known-mass window, calculate, "
            "remove the mass and tare, replace the mass, and verify. "
            "Calibration calculations and serial reads run outside this tab."
        )
        self.instruction_label.setObjectName("loadcell_instruction_label")
        self.instruction_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.instruction_label)

        connection_group = QGroupBox("1. Connect the Arduino Nano")
        connection_layout = QGridLayout(connection_group)
        self.source_mode_combo = QComboBox()
        self.source_mode_combo.setObjectName("loadcell_source_mode_combo")
        self.source_mode_combo.addItem("Physical Arduino Nano", "Physical")
        self.source_mode_combo.addItem("Synthetic HX711 (SIMULATION)", "Synthetic (simulation)")
        self.source_mode_combo.addItem("CSV/text playback (SIMULATION)", "Playback (simulation)")
        self.source_mode_combo.setToolTip(
            "Simulation modes reuse the serial parsing/consumer path without claiming physical hardware."
        )
        self.playback_path_edit = QLineEdit()
        self.playback_path_edit.setObjectName("loadcell_playback_path")
        self.playback_path_edit.setReadOnly(True)
        self.playback_path_edit.setPlaceholderText("Choose canonical CSV or protocol text")
        self.playback_file_button = QPushButton("Choose Playback File")
        self.playback_file_button.clicked.connect(self.playback_file_requested.emit)
        self.port_combo = QComboBox()
        self.port_combo.setObjectName("loadcell_port_combo")
        self.port_combo.setToolTip(
            "Select the Arduino serial port. Opening it may reset the Nano."
        )
        self.port_combo.addItem("No ports scanned", None)
        self.refresh_ports_button = QPushButton("Refresh Ports")
        self.refresh_ports_button.setToolTip(
            "Ask the serial worker to enumerate ports without opening them."
        )
        self.baud_combo = QComboBox()
        self.baud_combo.setObjectName("loadcell_baud_combo")
        for baud in (9600, 57600, 115200, 230400):
            self.baud_combo.addItem(str(baud), baud)
        self.baud_combo.setCurrentIndex(self.baud_combo.findData(115200))
        self.baud_combo.setEditable(True)
        self.baud_combo.setToolTip("The supplied firmware uses 115200 baud.")
        self.data_bits_combo = QComboBox()
        for value in (8, 7, 6, 5):
            self.data_bits_combo.addItem(str(value), value)
        self.parity_combo = QComboBox()
        for label, value in (("None", "N"), ("Even", "E"), ("Odd", "O")):
            self.parity_combo.addItem(label, value)
        self.stop_bits_combo = QComboBox()
        for value in (1.0, 1.5, 2.0):
            self.stop_bits_combo.addItem(f"{value:g}", value)
        self.read_timeout_spin = QDoubleSpinBox()
        self.read_timeout_spin.setRange(0.001, 10.0)
        self.read_timeout_spin.setDecimals(3)
        self.read_timeout_spin.setValue(0.05)
        self.read_timeout_spin.setSuffix(" s")
        self.startup_delay_spin = QDoubleSpinBox()
        self.startup_delay_spin.setRange(0.0, 30.0)
        self.startup_delay_spin.setDecimals(2)
        self.startup_delay_spin.setValue(2.0)
        self.startup_delay_spin.setSuffix(" s")
        self.validation_timeout_spin = QDoubleSpinBox()
        self.validation_timeout_spin.setRange(0.1, 120.0)
        self.validation_timeout_spin.setDecimals(1)
        self.validation_timeout_spin.setValue(3.0)
        self.validation_timeout_spin.setSuffix(" s")
        self.stream_timeout_spin = QDoubleSpinBox()
        self.stream_timeout_spin.setRange(0.1, 120.0)
        self.stream_timeout_spin.setDecimals(1)
        self.stream_timeout_spin.setValue(2.0)
        self.stream_timeout_spin.setSuffix(" s")
        self.input_format_combo = QComboBox()
        self.input_format_combo.addItems(
            (
                "Auto-detect",
                "DATA,<sample_id>,<timestamp>,<raw>",
                "Plain numeric",
                "RAW:<value> or raw=<value>",
                "<timestamp>,<raw>",
            )
        )
        self.validation_readings_spin = QSpinBox()
        self.validation_readings_spin.setRange(1, 100)
        self.validation_readings_spin.setValue(3)
        self.connect_button = QPushButton("Connect and Validate Readings")
        self.reconnect_button = QPushButton("Reconnect")
        self.reconnect_button.setEnabled(False)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setEnabled(False)
        connection_layout.addWidget(QLabel("Input source:"), 0, 0)
        connection_layout.addWidget(self.source_mode_combo, 0, 1, 1, 2)
        connection_layout.addWidget(QLabel("Playback file:"), 1, 0)
        connection_layout.addWidget(self.playback_path_edit, 1, 1)
        connection_layout.addWidget(self.playback_file_button, 1, 2)
        connection_layout.addWidget(QLabel("Serial port:"), 2, 0)
        connection_layout.addWidget(self.port_combo, 2, 1)
        connection_layout.addWidget(self.refresh_ports_button, 2, 2)
        connection_layout.addWidget(QLabel("Baud rate:"), 3, 0)
        connection_layout.addWidget(self.baud_combo, 3, 1, 1, 2)
        connection_layout.addWidget(QLabel("Data bits / parity / stop bits:"), 4, 0)
        serial_format_row = QHBoxLayout()
        serial_format_row.addWidget(self.data_bits_combo)
        serial_format_row.addWidget(self.parity_combo)
        serial_format_row.addWidget(self.stop_bits_combo)
        connection_layout.addLayout(serial_format_row, 4, 1, 1, 2)
        connection_layout.addWidget(QLabel("Read timeout / startup delay:"), 5, 0)
        timing_row = QHBoxLayout()
        timing_row.addWidget(self.read_timeout_spin)
        timing_row.addWidget(self.startup_delay_spin)
        connection_layout.addLayout(timing_row, 5, 1, 1, 2)
        connection_layout.addWidget(QLabel("Validation / stream timeout:"), 6, 0)
        validation_row = QHBoxLayout()
        validation_row.addWidget(self.validation_timeout_spin)
        validation_row.addWidget(self.stream_timeout_spin)
        connection_layout.addLayout(validation_row, 6, 1, 1, 2)
        connection_layout.addWidget(QLabel("Expected input format:"), 7, 0)
        connection_layout.addWidget(self.input_format_combo, 7, 1, 1, 2)
        connection_layout.addWidget(QLabel("Readings required to validate:"), 8, 0)
        connection_layout.addWidget(self.validation_readings_spin, 8, 1, 1, 2)
        connection_layout.addWidget(self.connect_button, 9, 0)
        connection_layout.addWidget(self.reconnect_button, 9, 1)
        connection_layout.addWidget(self.disconnect_button, 9, 2)
        connection_layout.setColumnStretch(1, 1)
        layout.addWidget(connection_group)

        reading_group = QGroupBox("2. Confirm live raw and force readings")
        reading_layout = QGridLayout(reading_group)
        self.raw_adc_label = QLabel("—")
        self.raw_adc_label.setObjectName("loadcell_raw_adc_label")
        self.force_gf_label = QLabel("— g")
        self.force_gf_label.setObjectName("loadcell_force_gf_label")
        self.force_N_label = QLabel("— N")
        self.force_N_label.setObjectName("loadcell_force_N_label")
        self.tared_raw_label = QLabel("not calibrated")
        self.reading_timestamp_label = QLabel("—")
        self.reading_frequency_label = QLabel("— Hz")
        self.connection_state_label = QLabel("Port closed")
        self.calibration_state_label = QLabel("Not calibrated")
        for label in (self.raw_adc_label, self.force_gf_label, self.force_N_label):
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setStyleSheet("font-size: 18px; font-weight: 600;")
        reading_layout.addWidget(QLabel("Raw ADC:"), 0, 0)
        reading_layout.addWidget(self.raw_adc_label, 0, 1)
        reading_layout.addWidget(QLabel("Tared raw:"), 1, 0)
        reading_layout.addWidget(self.tared_raw_label, 1, 1)
        reading_layout.addWidget(QLabel("Mass:"), 2, 0)
        reading_layout.addWidget(self.force_gf_label, 2, 1)
        reading_layout.addWidget(QLabel("Force:"), 3, 0)
        reading_layout.addWidget(self.force_N_label, 3, 1)
        reading_layout.addWidget(QLabel("Received:"), 4, 0)
        reading_layout.addWidget(self.reading_timestamp_label, 4, 1)
        reading_layout.addWidget(QLabel("Reading frequency:"), 5, 0)
        reading_layout.addWidget(self.reading_frequency_label, 5, 1)
        reading_layout.addWidget(QLabel("Connection status:"), 6, 0)
        reading_layout.addWidget(self.connection_state_label, 6, 1)
        reading_layout.addWidget(QLabel("Calibration status:"), 7, 0)
        reading_layout.addWidget(self.calibration_state_label, 7, 1)
        self.tare_button = QPushButton("Step 4: Remove Mass and Tare")
        self.tare_button.setEnabled(False)
        self.tare_button.setToolTip(
            "Requests a stable unloaded averaging window; it does not change counts per gram."
        )
        reading_layout.addWidget(self.tare_button, 8, 0, 1, 2)
        reading_layout.setColumnStretch(1, 1)
        layout.addWidget(reading_group)

        plot_group = QGroupBox("Live force plot (display-only bounded history)")
        plot_layout = QVBoxLayout(plot_group)
        self.force_plot = pg.PlotWidget()
        self.force_plot.setObjectName("loadcell_force_plot")
        self.force_plot.setLabel("bottom", "Elapsed time", units="s")
        self.force_plot.setLabel("left", "Force", units="N")
        self.force_plot.showGrid(x=True, y=True, alpha=0.25)
        self.force_plot.setMouseEnabled(x=True, y=True)
        self.force_plot.setMinimumHeight(220)
        self.force_curve = self.force_plot.plot(
            [], [], pen=pg.mkPen("#2f78c4", width=2), name="Force (N)"
        )
        self.force_curve.setDownsampling(auto=True, method="peak")
        self.force_curve.setClipToView(True)
        plot_layout.addWidget(self.force_plot)
        self.plot_history_label = _wrapped_label(
            f"Showing at most {self.plot_history_s:g} s and {self.max_plot_points} points. "
            "The complete raw stream is preserved by the recorder."
        )
        self.plot_history_label.setObjectName("loadcell_plot_history_label")
        plot_layout.addWidget(self.plot_history_label)
        layout.addWidget(plot_group)

        diagnostics_group = QGroupBox("Sample and device diagnostics")
        diagnostics_layout = QGridLayout(diagnostics_group)
        diagnostic_items = (
            ("valid_sample_count", "Valid physical samples"),
            ("measured_sample_rate_hz", "Measured sample rate (Hz)"),
            ("minimum_sample_interval_ms", "Minimum interval (ms)"),
            ("median_sample_interval_ms", "Median interval (ms)"),
            ("maximum_sample_interval_ms", "Maximum interval (ms)"),
            ("malformed_line_count", "Malformed lines"),
            ("readiness_error_count", "HX711 not-ready reports"),
            ("timeout_error_count", "HX711 timeout reports"),
            ("duplicate_sample_count", "Duplicate/stale samples rejected"),
            ("device_session_id", "Device-session identifier"),
            ("protocol_version", "Protocol version"),
            ("firmware_version", "Firmware version"),
            ("rejected_line_count", "Rejected serial lines"),
            ("most_recent_valid_reading", "Most recent valid raw value"),
        )
        self.diagnostic_labels: dict[str, QLabel] = {}
        for index, (key, title) in enumerate(diagnostic_items):
            row, column = divmod(index, 2)
            title_label = QLabel(title + ":")
            title_label.setWordWrap(True)
            value_label = QLabel("—")
            value_label.setObjectName(f"loadcell_{key}_label")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.diagnostic_labels[key] = value_label
            diagnostics_layout.addWidget(title_label, row, column * 2)
            diagnostics_layout.addWidget(value_label, row, column * 2 + 1)
        diagnostics_layout.setColumnStretch(1, 1)
        diagnostics_layout.setColumnStretch(3, 1)
        layout.addWidget(diagnostics_group)

        self.raw_monitor_group = QGroupBox("Raw Serial Monitor")
        self.raw_monitor_group.setCheckable(True)
        self.raw_monitor_group.setChecked(False)
        raw_layout = QVBoxLayout(self.raw_monitor_group)
        self.raw_monitor_text = QTextEdit()
        self.raw_monitor_text.setReadOnly(True)
        self.raw_monitor_text.setMaximumHeight(220)
        raw_buttons = QHBoxLayout()
        self.pause_raw_display_check = QCheckBox("Pause Display")
        self.clear_raw_log_button = QPushButton("Clear Log")
        self.save_raw_log_button = QPushButton("Save Serial Log")
        raw_buttons.addWidget(self.pause_raw_display_check)
        raw_buttons.addWidget(self.clear_raw_log_button)
        raw_buttons.addWidget(self.save_raw_log_button)
        raw_buttons.addStretch(1)
        raw_layout.addWidget(self.raw_monitor_text)
        raw_layout.addLayout(raw_buttons)
        layout.addWidget(self.raw_monitor_group)

        calibration_group = QGroupBox("3. Calibrate, tare, and verify in order")
        calibration_layout = QVBoxLayout(calibration_group)
        mass_row = QHBoxLayout()
        mass_row.addWidget(QLabel("Known calibration mass:"))
        self.known_mass_spin = QDoubleSpinBox()
        self.known_mass_spin.setObjectName("loadcell_known_mass_spin")
        self.known_mass_spin.setRange(0.001, 1_000_000.0)
        self.known_mass_spin.setDecimals(3)
        self.known_mass_spin.setValue(200.0)
        self.known_mass_spin.setSuffix(" g")
        self.known_mass_spin.setToolTip(
            "Enter the measured mass in grams; 200 g is the required default."
        )
        mass_row.addWidget(self.known_mass_spin)
        mass_row.addStretch(1)
        calibration_layout.addLayout(mass_row)

        workflow_buttons = QHBoxLayout()
        self.start_calibration_button = QPushButton("Start Calibration")
        self.cancel_calibration_button = QPushButton("Cancel Calibration")
        self.reset_calibration_button = QPushButton("Reset Calibration")
        workflow_buttons.addWidget(self.start_calibration_button)
        workflow_buttons.addWidget(self.cancel_calibration_button)
        workflow_buttons.addWidget(self.reset_calibration_button)
        calibration_layout.addLayout(workflow_buttons)

        steps_grid = QGridLayout()
        self.capture_unloaded_button = QPushButton("Step 1: Capture Unloaded Window")
        self.capture_loaded_button = QPushButton("Step 2: Capture Known-Mass Window")
        self.calculate_calibration_button = QPushButton("Step 3: Calculate Calibration")
        self.verify_calibration_button = QPushButton(
            "Step 5: Replace Mass and Verify (±5%)"
        )
        step_descriptions = (
            "Remove every load, wait for stability, then capture the unloaded samples.",
            "Place the known mass without touching the sensor and capture the loaded samples.",
            "Calculate the signed counts-per-gram factor only after SNR and stability pass.",
            "After the unloaded tare succeeds, replace the same known mass; failed verification blocks recording.",
        )
        for row, (button, description) in enumerate(
            zip(
                (
                    self.capture_unloaded_button,
                    self.capture_loaded_button,
                    self.calculate_calibration_button,
                    self.verify_calibration_button,
                ),
                step_descriptions,
                strict=True,
            )
        ):
            button.setEnabled(False)
            description_label = _wrapped_label(description)
            steps_grid.addWidget(button, row, 0)
            steps_grid.addWidget(description_label, row, 1)
        steps_grid.setColumnStretch(1, 1)
        calibration_layout.addLayout(steps_grid)

        result_grid = QGridLayout()
        result_items = (
            ("counts_per_gram", "Signed counts per gram"),
            ("tare_raw", "Tare raw ADC"),
            ("calibration_snr", "Calibration SNR"),
            ("loaded_window_cv_percent", "Loaded-window CV (%)"),
            ("quality_passed", "SNR/stability result"),
            ("verification_result", "Known-mass verification"),
        )
        self.calibration_labels: dict[str, QLabel] = {}
        for row, (key, title) in enumerate(result_items):
            result_grid.addWidget(QLabel(title + ":"), row, 0)
            value_label = QLabel("—")
            value_label.setObjectName(f"loadcell_{key}_label")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            result_grid.addWidget(value_label, row, 1)
            self.calibration_labels[key] = value_label
        result_grid.setColumnStretch(1, 1)
        calibration_layout.addLayout(result_grid)

        file_buttons = QHBoxLayout()
        self.save_calibration_button = QPushButton("Save Calibration")
        self.load_calibration_button = QPushButton("Load Calibration")
        file_buttons.addWidget(self.save_calibration_button)
        file_buttons.addWidget(self.load_calibration_button)
        file_buttons.addStretch(1)
        calibration_layout.addLayout(file_buttons)
        layout.addWidget(calibration_group)

        status_group = QGroupBox("Status and next action")
        status_layout = QVBoxLayout(status_group)
        self.status_label = _wrapped_label(
            "Arduino not connected. Refresh ports and select the Nano.",
            selectable=True,
        )
        self.status_label.setObjectName("loadcell_status_label")
        self.next_action_label = _wrapped_label(
            "Next: connect and wait for multiple valid numeric HX711 readings."
        )
        self.next_action_label.setObjectName("loadcell_next_action_label")
        self.workflow_progress_label = _wrapped_label(
            "Calibration progress: 0 of 5 steps complete."
        )
        self.workflow_progress_label.setObjectName("loadcell_workflow_progress_label")
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.workflow_progress_label)
        status_layout.addWidget(self.next_action_label)
        layout.addWidget(status_group)
        layout.addStretch(1)

        self._lockable_widgets.extend(
            (
                self.port_combo,
                self.source_mode_combo,
                self.playback_file_button,
                self.refresh_ports_button,
                self.baud_combo,
                self.data_bits_combo,
                self.parity_combo,
                self.stop_bits_combo,
                self.read_timeout_spin,
                self.startup_delay_spin,
                self.validation_timeout_spin,
                self.stream_timeout_spin,
                self.input_format_combo,
                self.validation_readings_spin,
                self.connect_button,
                self.reconnect_button,
                self.disconnect_button,
                self.tare_button,
                self.known_mass_spin,
                self.capture_unloaded_button,
                self.capture_loaded_button,
                self.calculate_calibration_button,
                self.verify_calibration_button,
                self.save_calibration_button,
                self.load_calibration_button,
                self.start_calibration_button,
                self.cancel_calibration_button,
                self.reset_calibration_button,
            )
        )
        self._connect_ui_signals()

    def _connect_ui_signals(self) -> None:
        self.refresh_ports_button.clicked.connect(self.refresh_ports_requested)
        self.connect_button.clicked.connect(self._emit_connect_request)
        self.reconnect_button.clicked.connect(self._emit_connect_request)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.tare_button.clicked.connect(self.tare_requested)
        self.capture_unloaded_button.clicked.connect(self._emit_unloaded_request)
        self.capture_loaded_button.clicked.connect(self._emit_loaded_request)
        self.calculate_calibration_button.clicked.connect(self._emit_calculate_request)
        self.verify_calibration_button.clicked.connect(self._emit_verification_request)
        self.save_calibration_button.clicked.connect(self.save_calibration_requested)
        self.load_calibration_button.clicked.connect(self.load_calibration_requested)
        self.start_calibration_button.clicked.connect(self.start_calibration_requested)
        self.cancel_calibration_button.clicked.connect(self.cancel_calibration_requested)
        self.reset_calibration_button.clicked.connect(self.reset_calibration_requested)
        self.clear_raw_log_button.clicked.connect(self.clear_raw_serial_log)
        self.save_raw_log_button.clicked.connect(
            lambda: self.save_serial_log_requested.emit(tuple(self._raw_serial_entries))
        )
        self.source_mode_combo.currentIndexChanged.connect(self._update_action_states)
        self.known_mass_spin.valueChanged.connect(self._known_mass_value_changed)
        self._set_workflow_stage("disconnected")

    def requested_connection(self) -> dict[str, object]:
        port = self.port_combo.currentData()
        baud_data = self.baud_combo.currentData()
        if self.baud_combo.isEditable():
            try:
                baud = int(self.baud_combo.currentText())
            except ValueError:
                baud = int(baud_data or 115200)
        else:
            baud = int(baud_data or 115200)
        request: dict[str, object] = {
            "port": None if port is None else str(port),
            "baud_rate": baud,
        }
        optional = {
            "data_bits": (int(self.data_bits_combo.currentData()), 8),
            "parity": (str(self.parity_combo.currentData()), "N"),
            "stop_bits": (float(self.stop_bits_combo.currentData()), 1.0),
            "read_timeout_s": (float(self.read_timeout_spin.value()), 0.05),
            "startup_delay_s": (float(self.startup_delay_spin.value()), 2.0),
            "validation_timeout_s": (
                float(self.validation_timeout_spin.value()), 3.0
            ),
            "stream_timeout_s": (float(self.stream_timeout_spin.value()), 2.0),
            "input_format": (self.input_format_combo.currentText(), "Auto-detect"),
            "validation_min_readings": (
                int(self.validation_readings_spin.value()), 3
            ),
        }
        request.update(
            {name: value for name, (value, default) in optional.items() if value != default}
        )
        return request

    def _emit_connect_request(self) -> None:
        self.connect_requested.emit(self.requested_connection())

    def _emit_unloaded_request(self) -> None:
        mass = self.known_mass_spin.value()
        self.calibration_unloaded_requested.emit()
        self.calibration_step_requested.emit("capture_unloaded", mass)

    def _emit_loaded_request(self) -> None:
        mass = self.known_mass_spin.value()
        self.calibration_loaded_requested.emit(mass)
        self.calibration_step_requested.emit("capture_loaded", mass)

    def _emit_calculate_request(self) -> None:
        mass = self.known_mass_spin.value()
        self.calibration_calculate_requested.emit(mass)
        self.calibration_step_requested.emit("calculate", mass)

    def _emit_verification_request(self) -> None:
        mass = self.known_mass_spin.value()
        self.verification_requested.emit(mass)
        self.calibration_step_requested.emit("verify", mass)

    def _known_mass_value_changed(self, value: float) -> None:
        mass = float(value)
        if self._workflow_stage not in {"disconnected", "connected"}:
            self.reset_calibration_progress(
                "Known mass changed. Previous windows, tare, and verification were invalidated."
            )
        self.known_mass_changed.emit(mass)

    def set_known_mass_g(self, value: float) -> None:
        """Set a loaded calibration's mass without emitting a user-change event."""

        mass = float(value)
        if not math.isfinite(mass) or mass <= 0.0:
            raise ValueError("known mass must be finite and positive")
        blocked = self.known_mass_spin.blockSignals(True)
        try:
            self.known_mass_spin.setValue(mass)
        finally:
            self.known_mass_spin.blockSignals(blocked)

    @property
    def workflow_stage(self) -> str:
        return self._workflow_stage

    def _set_workflow_stage(self, stage: str) -> None:
        normalized = str(stage).strip().lower()
        if normalized not in self.WORKFLOW_STAGES:
            raise ValueError(f"unknown load-cell workflow stage: {stage}")
        self._workflow_stage = normalized
        progress = {
            "disconnected": 0,
            "connected": 0,
            "unloaded": 1,
            "loaded": 2,
            "calculated": 3,
            "tared": 4,
            "verified": 5,
        }[normalized]
        if hasattr(self, "workflow_progress_label"):
            self.workflow_progress_label.setText(
                f"Calibration progress: {progress} of 5 steps complete."
            )
        next_actions = {
            "disconnected": (
                "Next: connect and wait for multiple valid numeric HX711 readings."
            ),
            "connected": "Next: remove every load and capture the unloaded window.",
            "unloaded": "Next: place the known mass and capture the loaded window.",
            "loaded": "Next: calculate and accept the signed calibration factor.",
            "calculated": "Next: remove the known mass, let the sensor settle, then tare.",
            "tared": "Next: replace the same known mass and run verification.",
            "verified": "Next: continue to Recording and Export.",
        }
        if hasattr(self, "next_action_label"):
            self.next_action_label.setText(next_actions[normalized])
        if hasattr(self, "capture_unloaded_button"):
            self._update_action_states()

    def reset_calibration_progress(self, message: str | None = None) -> None:
        """Invalidate the displayed sequence without claiming domain-state ownership."""

        self._calibration_available = False
        self._verification_passed = False
        self._set_workflow_stage("connected" if self._connected else "disconnected")
        self._clear_calibration_labels()
        if message:
            self.set_status(message, "warning")

    def mark_sample_window_captured(self, stage: str) -> None:
        """Advance only after the controller confirms a complete sample window."""

        normalized = str(stage).strip().lower()
        if normalized == "unloaded":
            self._calibration_available = False
            self._verification_passed = False
            self._clear_calibration_labels()
            self._set_workflow_stage("unloaded")
        elif normalized == "loaded":
            if self._workflow_stage not in {"unloaded", "loaded"}:
                raise ValueError("capture the unloaded window before the loaded window")
            self._set_workflow_stage("loaded")
        else:
            raise ValueError("sample-window stage must be 'unloaded' or 'loaded'")

    def _clear_calibration_labels(self) -> None:
        for label in self.calibration_labels.values():
            label.setText("—")

    def set_calibration_available(self, available: bool) -> None:
        """Expose an accepted but not-yet-retared calibration to the gated UI."""

        self._calibration_available = bool(available)
        self._verification_passed = False
        if available:
            self._set_workflow_stage("calculated")
        else:
            self._clear_calibration_labels()
            self._set_workflow_stage("connected" if self._connected else "disconnected")

    def mark_tare_complete(self) -> None:
        if not self._calibration_available:
            raise ValueError("an accepted calibration is required before tare")
        self._verification_passed = False
        self._set_workflow_stage("tared")

    def set_verification_state(self, passed: bool) -> None:
        if not self._calibration_available:
            raise ValueError("an accepted calibration is required before verification")
        self._verification_passed = bool(passed)
        self._set_workflow_stage("verified" if passed else "tared")

    def set_serial_ports(self, ports: Iterable[Any]) -> None:
        previous = self.port_combo.currentData()
        self.port_combo.clear()
        for item in ports:
            if isinstance(item, Mapping):
                device = str(item.get("device", item.get("port", ""))).strip()
                description = str(item.get("description", "")).strip()
            elif hasattr(item, "device"):
                device = str(getattr(item, "device")).strip()
                description = str(getattr(item, "description", "")).strip()
            elif isinstance(item, tuple) and len(item) >= 2:
                device, description = str(item[0]).strip(), str(item[1]).strip()
            else:
                device, description = str(item).strip(), ""
            if device:
                label = f"{device} — {description}" if description else device
                self.port_combo.addItem(label, device)
        if self.port_combo.count() == 0:
            self.port_combo.addItem("No serial port detected", None)
        elif previous is not None:
            index = self.port_combo.findData(previous)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        self._update_action_states()

    def set_connection_state(
        self,
        connected: bool,
        info: SerialConnectionInfo | Mapping[str, Any] | None = None,
        *,
        message: str | None = None,
    ) -> None:
        self._connected = bool(connected)
        self._stream_ready = False
        self._sample_window_active = False
        self._workflow_job_active = False
        self.connection_state_label.setText(
            "Port opened; waiting for sensor readings" if connected else "Port closed"
        )
        if info is not None:
            if isinstance(info, Mapping):
                get_value = info.get
            else:
                get_value = lambda name, default=None: getattr(info, name, default)
            for key in ("device_session_id", "protocol_version", "firmware_version"):
                self.diagnostic_labels[key].setText(str(get_value(key, "—")))
            simulation = bool(get_value("simulation_mode", False))
            if simulation:
                self.set_status(
                    message or "Serial playback/synthetic simulation connected; no physical hardware is claimed.",
                    "warning",
                )
            elif message is not None:
                self.set_status(message, "success")
        elif message is not None:
            self.set_status(message, "warning")
        self.reset_calibration_progress()

    def set_stream_ready(self, ready: bool) -> None:
        """Gate acquisition actions on fresh DATA, independently of transport state."""

        self._stream_ready = bool(ready) and self._connected
        self.connection_state_label.setText(
            "Receiving valid raw readings"
            if self._stream_ready
            else "Waiting for sensor readings"
            if self._connected
            else "Port closed"
        )
        self._update_action_states()

    def set_sample_window_active(self, active: bool) -> None:
        """Make one averaging window an atomic wizard operation in the GUI."""

        self._sample_window_active = bool(active)
        self._update_action_states()

    def set_workflow_job_active(self, active: bool) -> None:
        """Lock wizard inputs while calculation, tare, or verification runs."""

        self._workflow_job_active = bool(active)
        self._update_action_states()

    @property
    def workflow_operation_active(self) -> bool:
        """Return whether a sample window or background workflow job owns the wizard."""

        return self._sample_window_active or self._workflow_job_active

    def invalidate_verification_for_retare(self) -> None:
        """Return a verified calibration to the calculated stage before retaring."""

        if not self._calibration_available:
            raise ValueError("an accepted calibration is required before tare")
        self._verification_passed = False
        self.calibration_labels["verification_result"].setText("—")
        self._set_workflow_stage("calculated")

    def _update_action_states(self) -> None:
        unlocked = not self._controls_locked
        mode = str(self.source_mode_combo.currentData())
        source_ready = (
            self.port_combo.currentData() is not None
            if mode == "Physical"
            else bool(self.playback_path_edit.text().strip())
            if mode == "Playback (simulation)"
            else True
        )
        self.connect_button.setEnabled(unlocked and not self._connected and source_ready)
        self.reconnect_button.setEnabled(
            unlocked and self._connected and not self._sample_window_active
        )
        self.disconnect_button.setEnabled(
            unlocked and self._connected and not self._sample_window_active
        )
        active = (
            unlocked
            and self._connected
            and self._stream_ready
            and not self._sample_window_active
            and not self._workflow_job_active
        )
        self.capture_unloaded_button.setEnabled(active)
        self.start_calibration_button.setEnabled(active)
        self.cancel_calibration_button.setEnabled(
            unlocked and (self._sample_window_active or self._workflow_job_active)
        )
        self.reset_calibration_button.setEnabled(
            unlocked
            and not self._sample_window_active
            and not self._workflow_job_active
            and self._workflow_stage not in {"disconnected", "connected"}
        )
        self.capture_loaded_button.setEnabled(
            active and self._workflow_stage in {"unloaded", "loaded"}
        )
        self.calculate_calibration_button.setEnabled(
            active and self._workflow_stage == "loaded"
        )
        self.tare_button.setEnabled(
            active
            and self._calibration_available
            and self._workflow_stage in {"calculated", "verified"}
        )
        self.verify_calibration_button.setEnabled(
            active
            and self._calibration_available
            and self._workflow_stage == "tared"
        )
        self.known_mass_spin.setEnabled(active)
        self.save_calibration_button.setEnabled(
            unlocked
            and not self._sample_window_active
            and not self._workflow_job_active
            and self._calibration_available
        )
        self.load_calibration_button.setEnabled(
            unlocked and not self._sample_window_active and not self._workflow_job_active
        )
        self.playback_path_edit.setEnabled(False)
        self.playback_file_button.setEnabled(
            unlocked and not self._connected and mode == "Playback (simulation)"
        )

    def set_playback_path(self, path: str) -> None:
        self.playback_path_edit.setText(str(path))
        self.playback_path_edit.setToolTip(str(path))
        self._update_action_states()

    def set_controls_locked(self, locked: bool) -> None:
        self._controls_locked = bool(locked)
        for widget in self._lockable_widgets:
            widget.setEnabled(not locked)
        self._update_action_states()

    def update_live_reading(
        self,
        raw_adc: float | None,
        force_gf: float | None,
        force_N: float | None,
        *,
        elapsed_time_s: float | None = None,
        wall_clock_iso: str | None = None,
        reading_frequency_hz: float | None = None,
        tare_raw: float | None = None,
        update_plot: bool = True,
    ) -> None:
        if raw_adc is None:
            self.raw_adc_label.setText("—")
        else:
            numeric_raw = float(raw_adc)
            self.raw_adc_label.setText(
                str(int(numeric_raw)) if numeric_raw.is_integer() else f"{numeric_raw:.9g}"
            )
        calibrated = force_gf is not None and math.isfinite(float(force_gf))
        self.force_gf_label.setText(
            self._formatted_force(force_gf, "g") if calibrated else "not calibrated"
        )
        self.force_N_label.setText(
            self._formatted_force(force_N, "N") if calibrated else "not calibrated"
        )
        self.calibration_state_label.setText(
            "Calibrated" if calibrated else "Not calibrated"
        )
        if raw_adc is not None and tare_raw is not None:
            self.tared_raw_label.setText(f"{float(raw_adc) - float(tare_raw):.9g}")
        else:
            self.tared_raw_label.setText("not calibrated")
        if wall_clock_iso:
            self.reading_timestamp_label.setText(str(wall_clock_iso))
        if reading_frequency_hz is not None and math.isfinite(float(reading_frequency_hz)):
            self.reading_frequency_label.setText(f"{float(reading_frequency_hz):.2f} Hz")
        if elapsed_time_s is not None:
            self.append_force_sample(
                elapsed_time_s, force_N, update_plot=update_plot
            )

    @staticmethod
    def _formatted_force(value: float | None, unit: str) -> str:
        if value is None or not math.isfinite(float(value)):
            return f"— {unit}"
        return f"{float(value):.6g} {unit}"

    def append_force_sample(
        self,
        elapsed_time_s: float,
        force_N: float | None,
        *,
        update_plot: bool = True,
    ) -> None:
        timestamp = float(elapsed_time_s)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("elapsed_time_s must be finite and nonnegative")
        force = math.nan if force_N is None else float(force_N)
        if math.isinf(force):
            raise ValueError("force_N must be finite or NaN")
        self._force_times_s.append(timestamp)
        self._force_values_N.append(force)
        cutoff = timestamp - self.plot_history_s
        while self._force_times_s and self._force_times_s[0] < cutoff:
            self._force_times_s.popleft()
            self._force_values_N.popleft()
        if update_plot:
            self.refresh_force_plot()

    def refresh_force_plot(self) -> None:
        """Reuse the existing curve and update it with bounded display data."""

        self.force_curve.setData(
            list(self._force_times_s), list(self._force_values_N)
        )

    @property
    def force_history_size(self) -> int:
        return len(self._force_times_s)

    def clear_force_plot(self) -> None:
        self._force_times_s.clear()
        self._force_values_N.clear()
        self.refresh_force_plot()

    def update_diagnostics(self, values: Mapping[str, Any]) -> None:
        for name, label in self.diagnostic_labels.items():
            value = values.get(name)
            if value is None or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                label.setText("—")
            else:
                label.setText(str(value))
        entries = values.get("raw_log_entries", ())
        if isinstance(entries, (list, tuple)):
            new_entries = [
                dict(item)
                for item in entries
                if isinstance(item, Mapping)
                and int(item.get("sequence", 0)) > self._last_raw_sequence
            ]
            if new_entries:
                self._raw_serial_entries.extend(new_entries)
                self._raw_serial_entries = self._raw_serial_entries[-10_000:]
                self._last_raw_sequence = max(
                    int(item.get("sequence", 0)) for item in new_entries
                )
                if not self.pause_raw_display_check.isChecked():
                    for item in new_entries:
                        parsed = item.get("parsed_value")
                        suffix = "" if parsed is None else f" -> {parsed}"
                        message = str(item.get("message", "")).strip()
                        detail = "" if not message else f" ({message})"
                        self.raw_monitor_text.append(
                            f"{item.get('wall_clock_iso', '')} "
                            f"[{item.get('status', '')}] {item.get('raw_line', '')}"
                            f"{suffix}{detail}"
                        )
                    document = self.raw_monitor_text.document()
                    while document.blockCount() > 1000:
                        cursor = self.raw_monitor_text.textCursor()
                        cursor.movePosition(QTextCursor.MoveOperation.Start)
                        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                        cursor.removeSelectedText()
                        cursor.deleteChar()

    def clear_raw_serial_log(self) -> None:
        """Clear the diagnostic display without pausing serial acquisition."""

        self._raw_serial_entries.clear()
        self.raw_monitor_text.clear()

    def update_calibration_result(self, values: Mapping[str, Any]) -> None:
        for name, label in self.calibration_labels.items():
            if name == "verification_result":
                continue
            value = values.get(name)
            if value is None or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                label.setText("—")
            elif name == "quality_passed":
                label.setText("Passed" if bool(value) else "Failed")
            else:
                label.setText(str(value))
        passed = values.get("quality_passed")
        if passed is True:
            self.calibration_state_label.setText("Calibrated; validation pending")
            self.set_calibration_available(True)
            self.set_status(
                "Calibration SNR and loaded-window stability passed. Remove the mass and tare.",
                "success",
            )
        elif passed is False:
            self.calibration_state_label.setText("Calibration failed")
            self.set_calibration_available(False)
            reasons = values.get("rejection_reasons", ())
            detail = "; ".join(str(item) for item in reasons) or "quality limits failed"
            self.set_status(f"Calibration rejected: {detail}", "error")

    def update_verification_result(self, values: Mapping[str, Any]) -> None:
        passed = bool(values.get("passed", False))
        expected = values.get("expected_gf", "—")
        measured = values.get("measured_mean_gf", "—")
        error = values.get("percentage_error", "—")
        measured_std = values.get("measured_std_gf", "unavailable")
        try:
            expected_newtons = f"{float(expected) * 0.00980665:.6g} N"
        except (TypeError, ValueError):
            expected_newtons = "unavailable"
        try:
            measured_newtons = f"{float(measured) * 0.00980665:.6g} N"
        except (TypeError, ValueError):
            measured_newtons = "unavailable"
        text = (
            f"{'Passed' if passed else 'Failed'}: expected {expected} g "
            f"({expected_newtons}); measured {measured} g ({measured_newtons}); "
            f"stability SD {measured_std} g; error {error}%."
        )
        self.calibration_labels["verification_result"].setText(text)
        self.calibration_state_label.setText(
            "Calibration completed" if passed else "Calibration validation failed"
        )
        self.set_status(text, "success" if passed else "error")
        self.set_verification_state(passed)

    def set_status(self, message: str, severity: str = "info") -> None:
        colors = {
            "info": "#245c8a",
            "success": "#236b35",
            "warning": "#8a5a00",
            "error": "#9b2525",
        }
        normalized = severity if severity in colors else "info"
        self.status_label.setText(str(message))
        self.status_label.setStyleSheet(
            f"padding: 6px; border-left: 4px solid {colors[normalized]};"
        )

    def set_next_action(self, message: str) -> None:
        self.next_action_label.setText(str(message))


__all__ = ["LoadCellTab"]
