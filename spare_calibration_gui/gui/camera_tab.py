"""Presentation-only camera connection, orientation, and preview tab.

The GUI intentionally exposes no camera-property or mode controls.  It selects a
source/backend, connects it, and applies only the selected lossless orientation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.interfaces import CameraConnectionInfo


def _wrapped_label(text: str, *, selectable: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    if selectable:
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


class CameraTab(QWidget):
    """Simple source selection, connection, and software-oriented preview."""

    refresh_devices_requested = Signal()
    connect_requested = Signal(object)
    disconnect_requested = Signal()
    start_preview_requested = Signal()
    stop_preview_requested = Signal()
    source_file_requested = Signal()
    orientation_changed = Signal(int, bool)
    driver_settings_refresh_requested = Signal()
    native_properties_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        rotation_degrees: int = 0,
        mirror_horizontal: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("camera_tab")
        self.setMinimumSize(0, 0)
        self._connected = False
        self._previewing = False
        self._controls_locked = False
        self._driver_settings_available = False
        self._native_properties_available = False
        self._preview_pixmap: QPixmap | None = None

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("camera_scroll_area")
        self.scroll_area.setWidgetResizable(True)
        outer_layout.addWidget(self.scroll_area)

        content = QWidget()
        self.scroll_area.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.instruction_label = _wrapped_label(
            "Select the Arducam source, connect, and start preview. The application "
            "reads the current camera-driver values without silently overwriting them. "
            "With DirectShow, Open Windows Camera Properties adjusts the same live "
            "camera handle used by this GUI. Rotation and mirroring remain lossless "
            "software steps applied consistently to preview, analysis, and recording."
        )
        self.instruction_label.setObjectName("camera_instruction_label")
        self.instruction_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.instruction_label)

        connection_group = QGroupBox("Camera source and connection")
        connection_layout = QGridLayout(connection_group)
        self.source_mode_combo = QComboBox()
        self.source_mode_combo.setObjectName("camera_source_mode_combo")
        self.source_mode_combo.addItem("Physical Arducam IMX179", "Physical")
        self.source_mode_combo.addItem(
            "Synthetic single press (SIMULATION)", "Synthetic (simulation)"
        )
        self.source_mode_combo.addItem(
            "Video file (SIMULATION)", "Video file (simulation)"
        )
        self.source_path_edit = QLineEdit()
        self.source_path_edit.setObjectName("camera_simulation_video_path")
        self.source_path_edit.setReadOnly(True)
        self.source_path_edit.setPlaceholderText("Choose a simulation video file")
        self.source_file_button = QPushButton("Choose Video File")
        self.refresh_devices_button = QPushButton("Refresh Cameras")
        self.device_combo = QComboBox()
        self.device_combo.setObjectName("camera_device_combo")
        self.device_combo.addItem("Camera 0", 0)
        self.backend_combo = QComboBox()
        self.backend_combo.setObjectName("camera_backend_combo")
        self.backend_combo.addItem("DirectShow (recommended)", "DirectShow")
        self.backend_combo.addItem("Media Foundation fallback", "Media Foundation")
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setEnabled(False)
        connection_layout.addWidget(QLabel("Input source:"), 0, 0)
        connection_layout.addWidget(self.source_mode_combo, 0, 1, 1, 2)
        connection_layout.addWidget(QLabel("Simulation video:"), 1, 0)
        connection_layout.addWidget(self.source_path_edit, 1, 1)
        connection_layout.addWidget(self.source_file_button, 1, 2)
        connection_layout.addWidget(QLabel("Device index:"), 2, 0)
        connection_layout.addWidget(self.device_combo, 2, 1)
        connection_layout.addWidget(self.refresh_devices_button, 2, 2)
        connection_layout.addWidget(QLabel("Windows backend:"), 3, 0)
        connection_layout.addWidget(self.backend_combo, 3, 1, 1, 2)
        connection_layout.addWidget(self.connect_button, 4, 1)
        connection_layout.addWidget(self.disconnect_button, 4, 2)
        connection_layout.setColumnStretch(1, 1)
        layout.addWidget(connection_group)

        driver_group = QGroupBox("Windows / driver camera settings")
        driver_layout = QVBoxLayout(driver_group)
        self.driver_settings_note = _wrapped_label(
            "Settings saved by the hardware driver are used when the camera opens. "
            "Windows Camera app filters or Studio Effects may be app-only and cannot "
            "be imported through OpenCV. For a guaranteed match, connect with "
            "DirectShow and adjust the driver dialog below; preview may pause while it is open."
        )
        driver_layout.addWidget(self.driver_settings_note)
        driver_buttons = QHBoxLayout()
        self.open_native_properties_button = QPushButton(
            "Open Windows Camera Properties"
        )
        self.open_native_properties_button.setObjectName(
            "camera_open_native_properties_button"
        )
        self.refresh_driver_settings_button = QPushButton(
            "Read Current Driver Values"
        )
        self.refresh_driver_settings_button.setObjectName(
            "camera_refresh_driver_settings_button"
        )
        driver_buttons.addWidget(self.open_native_properties_button)
        driver_buttons.addWidget(self.refresh_driver_settings_button)
        driver_buttons.addStretch(1)
        driver_layout.addLayout(driver_buttons)
        self.driver_settings_status = _wrapped_label(
            "Connect a physical camera to read its current driver values."
        )
        self.driver_settings_status.setObjectName("camera_driver_settings_status")
        driver_layout.addWidget(self.driver_settings_status)
        self.driver_settings_values = _wrapped_label(
            "Current values: unavailable", selectable=True
        )
        self.driver_settings_values.setObjectName("camera_driver_settings_values")
        driver_layout.addWidget(self.driver_settings_values)
        layout.addWidget(driver_group)

        preview_group = QGroupBox("Camera view and orientation")
        preview_layout = QVBoxLayout(preview_group)
        buttons = QHBoxLayout()
        self.start_preview_button = QPushButton("Start Preview")
        self.stop_preview_button = QPushButton("Stop Preview")
        self.start_preview_button.setEnabled(False)
        self.stop_preview_button.setEnabled(False)
        buttons.addWidget(self.start_preview_button)
        buttons.addWidget(self.stop_preview_button)
        buttons.addStretch(1)
        preview_layout.addLayout(buttons)
        orientation_row = QHBoxLayout()
        orientation_row.addWidget(QLabel("Rotate clockwise:"))
        self.rotation_combo = QComboBox()
        self.rotation_combo.setObjectName("camera_rotation_combo")
        for degrees in (0, 90, 180, 270):
            self.rotation_combo.addItem(f"{degrees}°", degrees)
        rotation_index = self.rotation_combo.findData(int(rotation_degrees))
        if rotation_index < 0:
            raise ValueError("rotation_degrees must be 0, 90, 180, or 270")
        self.rotation_combo.setCurrentIndex(rotation_index)
        self.mirror_checkbox = QCheckBox("Mirror horizontally")
        self.mirror_checkbox.setObjectName("camera_mirror_horizontal_checkbox")
        self.mirror_checkbox.setChecked(bool(mirror_horizontal))
        orientation_row.addWidget(self.rotation_combo)
        orientation_row.addWidget(self.mirror_checkbox)
        orientation_row.addStretch(1)
        preview_layout.addLayout(orientation_row)
        self.orientation_note = _wrapped_label(
            "Changing orientation invalidates the optical baseline and may reset the ROI layout."
        )
        preview_layout.addWidget(self.orientation_note)
        self.preview_label = QLabel("Camera view unavailable")
        self.preview_label.setObjectName("camera_preview_label")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(320, 200)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_label.setStyleSheet(
            "background: #17191c; color: #d6d6d6; border: 1px solid #555;"
        )
        preview_layout.addWidget(self.preview_label, 1)
        self.actual_mode_label = _wrapped_label(
            "Camera stream: unavailable until connected.", selectable=True
        )
        preview_layout.addWidget(self.actual_mode_label)
        layout.addWidget(preview_group, 1)

        performance_group = QGroupBox("Live performance")
        performance_layout = QGridLayout(performance_group)
        performance_items = (
            ("capture_fps", "Camera capture", " FPS"),
            ("preview_fps", "Preview refresh", " FPS"),
            ("feature_processing_fps", "Feature processing", " FPS"),
            ("video_writing_fps", "Video writing", " FPS"),
            ("dropped_preview_frames", "Dropped preview copies", ""),
            ("capture_to_display_ms", "Capture-to-display", " ms"),
        )
        self.performance_labels: dict[str, QLabel] = {}
        for index, (key, title, suffix) in enumerate(performance_items):
            title_label = QLabel(title + ":")
            value_label = QLabel("—")
            value_label.setObjectName(f"camera_{key}_label")
            value_label.setProperty("unit_suffix", suffix)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.performance_labels[key] = value_label
            row, column = divmod(index, 2)
            performance_layout.addWidget(title_label, row, column * 2)
            performance_layout.addWidget(value_label, row, column * 2 + 1)
        layout.addWidget(performance_group)

        status_group = QGroupBox("Status and next action")
        status_layout = QVBoxLayout(status_group)
        self.status_label = _wrapped_label(
            "Camera not connected. Refresh cameras and identify the correct device."
        )
        self.next_action_label = _wrapped_label(
            "Next: select a device and connect using DirectShow."
        )
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.next_action_label)
        layout.addWidget(status_group)
        layout.addStretch(1)

        self._lockable_widgets = (
            self.refresh_devices_button,
            self.source_mode_combo,
            self.source_file_button,
            self.device_combo,
            self.backend_combo,
            self.connect_button,
            self.rotation_combo,
            self.mirror_checkbox,
            self.open_native_properties_button,
            self.refresh_driver_settings_button,
        )
        self.refresh_devices_button.clicked.connect(self.refresh_devices_requested)
        self.connect_button.clicked.connect(self._emit_connect_request)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.start_preview_button.clicked.connect(self.start_preview_requested)
        self.stop_preview_button.clicked.connect(self.stop_preview_requested)
        self.source_file_button.clicked.connect(self.source_file_requested)
        self.source_mode_combo.currentIndexChanged.connect(self._update_action_states)
        self.rotation_combo.currentIndexChanged.connect(self._emit_orientation_changed)
        self.mirror_checkbox.toggled.connect(self._emit_orientation_changed)
        self.open_native_properties_button.clicked.connect(
            self.native_properties_requested
        )
        self.refresh_driver_settings_button.clicked.connect(
            self.driver_settings_refresh_requested
        )
        self._update_action_states()

    def requested_connection(self) -> dict[str, object]:
        device_data = self.device_combo.currentData()
        return {
            "device_index": int(device_data if device_data is not None else 0),
            "backend": str(self.backend_combo.currentData()),
        }

    def requested_properties(self) -> dict[str, float | bool]:
        """Compatibility method: camera property requests are intentionally absent."""

        return {}

    def requested_orientation(self) -> tuple[int, bool]:
        return int(self.rotation_combo.currentData()), self.mirror_checkbox.isChecked()

    def set_orientation(
        self,
        rotation_degrees: int,
        mirror_horizontal: bool,
        *,
        emit_change: bool = True,
    ) -> None:
        """Set both orientation controls atomically and emit at most one change."""

        rotation = int(rotation_degrees)
        mirror = bool(mirror_horizontal)
        index = self.rotation_combo.findData(rotation)
        if index < 0:
            raise ValueError("rotation_degrees must be 0, 90, 180, or 270")
        changed = self.requested_orientation() != (rotation, mirror)
        rotation_was_blocked = self.rotation_combo.blockSignals(True)
        mirror_was_blocked = self.mirror_checkbox.blockSignals(True)
        try:
            self.rotation_combo.setCurrentIndex(index)
            self.mirror_checkbox.setChecked(mirror)
        finally:
            self.rotation_combo.blockSignals(rotation_was_blocked)
            self.mirror_checkbox.blockSignals(mirror_was_blocked)
        if changed and emit_change:
            self.orientation_changed.emit(rotation, mirror)

    def orientation_description(self) -> str:
        rotation, mirror = self.requested_orientation()
        return f"{rotation}° clockwise; horizontal mirror {'on' if mirror else 'off'}"

    def _emit_orientation_changed(self, *_args: object) -> None:
        rotation, mirror = self.requested_orientation()
        self.orientation_changed.emit(rotation, mirror)

    def _emit_connect_request(self) -> None:
        self.connect_requested.emit(self.requested_connection())

    def set_camera_devices(self, devices: Iterable[Any]) -> None:
        previous = self.device_combo.currentData()
        self.device_combo.clear()
        for item in devices:
            if isinstance(item, Mapping):
                index = int(item.get("device_index", item.get("index", 0)))
                label = str(item.get("label", item.get("name", f"Camera {index}")))
            elif isinstance(item, tuple) and len(item) >= 2:
                index, label = int(item[0]), str(item[1])
            elif hasattr(item, "device_index"):
                index = int(getattr(item, "device_index"))
                label = str(
                    getattr(item, "display_name", None) or f"Camera {index}"
                )
            else:
                index = int(item)
                label = f"Camera {index}"
            self.device_combo.addItem(label, index)
        if self.device_combo.count() == 0:
            self.device_combo.addItem("No camera detected", None)
        elif previous is not None:
            match = self.device_combo.findData(previous)
            if match >= 0:
                self.device_combo.setCurrentIndex(match)
        self._update_action_states()

    def set_connection_state(
        self,
        connected: bool,
        info: CameraConnectionInfo | Mapping[str, Any] | None = None,
        *,
        message: str | None = None,
    ) -> None:
        self._connected = bool(connected)
        if info is not None:
            get_value = info.get if isinstance(info, Mapping) else (
                lambda name, default=None: getattr(info, name, default)
            )
            backend = str(get_value("backend", ""))
            simulation = bool(get_value("simulation_mode", False))
            self._driver_settings_available = self._connected and not simulation
            self._native_properties_available = (
                self._driver_settings_available and backend == "DirectShow"
            )
            self.actual_mode_label.setText(
                "Camera stream: "
                f"{get_value('actual_width', '—')} × {get_value('actual_height', '—')}; "
                f"reported {get_value('actual_fps', '—')} FPS; "
                f"{get_value('backend', '—')} device {get_value('device_index', '—')}. "
                f"Orientation: {self.orientation_description()}. "
                "Current driver properties are read for provenance."
            )
        elif not connected:
            self._driver_settings_available = False
            self._native_properties_available = False
            self.actual_mode_label.setText("Camera stream: unavailable until connected.")
            self.driver_settings_status.setText(
                "Connect a physical camera to read its current driver values."
            )
            self.driver_settings_values.setText("Current values: unavailable")
        self._update_action_states()
        if message is not None:
            self.set_status(message, "success" if connected else "warning")
        self.next_action_label.setText(
            "Next: start preview, define nine ROIs, and capture the unloaded baseline."
            if connected
            else "Next: select a device and connect using DirectShow."
        )

    def set_preview_state(self, previewing: bool) -> None:
        self._previewing = bool(previewing) and self._connected
        self._update_action_states()

    @property
    def previewing(self) -> bool:
        return self._previewing

    def _update_action_states(self, *_args: object) -> None:
        unlocked = not self._controls_locked
        mode = str(self.source_mode_combo.currentData())
        source_ready = (
            self.device_combo.currentData() is not None
            if mode == "Physical"
            else bool(self.source_path_edit.text().strip())
            if mode == "Video file (simulation)"
            else True
        )
        self.connect_button.setEnabled(unlocked and not self._connected and source_ready)
        self.disconnect_button.setEnabled(unlocked and self._connected)
        self.start_preview_button.setEnabled(
            unlocked and self._connected and not self._previewing
        )
        self.stop_preview_button.setEnabled(
            unlocked and self._connected and self._previewing
        )
        self.source_file_button.setEnabled(
            unlocked and not self._connected and mode == "Video file (simulation)"
        )
        self.refresh_driver_settings_button.setEnabled(
            unlocked and self._driver_settings_available
        )
        self.open_native_properties_button.setEnabled(
            unlocked and self._native_properties_available
        )
        self.open_native_properties_button.setToolTip(
            "Adjust the active GUI camera through its Windows DirectShow driver dialog."
            if self._native_properties_available
            else "Connect a physical camera with the DirectShow backend to use this dialog."
        )

    def set_source_path(self, path: str) -> None:
        self.source_path_edit.setText(str(path))
        self.source_path_edit.setToolTip(str(path))
        self._update_action_states()

    def set_controls_locked(self, locked: bool) -> None:
        self._controls_locked = bool(locked)
        for widget in self._lockable_widgets:
            widget.setEnabled(not locked)
        self._update_action_states()

    def set_driver_settings_status(self, message: str, severity: str = "info") -> None:
        colors = {
            "info": "#245c8a",
            "success": "#236b35",
            "warning": "#8a5a00",
            "error": "#9b2525",
        }
        self.driver_settings_status.setText(str(message))
        self.driver_settings_status.setStyleSheet(
            f"padding: 4px; border-left: 4px solid {colors.get(severity, colors['info'])};"
        )

    def update_property_diagnostics(self, readbacks: Iterable[object]) -> None:
        """Display the latest explicit camera-property write/readback evidence."""

        rows = tuple(readbacks)
        if rows:
            self.update_capabilities(rows)

    def update_capabilities(self, capabilities: Iterable[object]) -> None:
        """Show readable values currently reported by the active camera driver."""

        preferred_order = (
            "exposure",
            "auto_exposure",
            "gain",
            "brightness",
            "contrast",
            "saturation",
            "sharpness",
            "gamma",
            "white_balance",
            "auto_white_balance",
            "focus",
            "auto_focus",
            "hue",
            "backlight_compensation",
            "zoom",
            "pan",
            "tilt",
            "roll",
            "iris",
            "temperature",
        )
        values: dict[str, object] = {}
        for item in capabilities:
            get_value = item.get if isinstance(item, Mapping) else (
                lambda name, default=None: getattr(item, name, default)
            )
            name = str(get_value("property_name", ""))
            readable = bool(
                get_value("readable", get_value("read_succeeded", False))
            )
            current = get_value("current_value", get_value("actual_value", None))
            if name in preferred_order and readable and current is not None:
                values[name] = current
        if not values:
            self.driver_settings_values.setText(
                "Current values: the selected backend/driver exposed no readable controls."
            )
            self.set_driver_settings_status(
                "No transferable driver values were reported. Use DirectShow and its native dialog.",
                "warning",
            )
            return

        def format_value(value: object) -> str:
            if isinstance(value, bool):
                return "on" if value else "off"
            try:
                return f"{float(value):.6g}"
            except (TypeError, ValueError):
                return str(value)

        summary = "; ".join(
            f"{name.replace('_', ' ')}={format_value(values[name])}"
            for name in preferred_order
            if name in values
        )
        self.driver_settings_values.setText(f"Current values used by GUI: {summary}")
        self.set_driver_settings_status(
            f"Read {len(values)} current value(s) from the active camera driver.",
            "success",
        )

    def update_performance(self, values: Mapping[str, Any]) -> None:
        for name, label in self.performance_labels.items():
            value = values.get(name)
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                label.setText("—")
            else:
                label.setText(f"{value}{str(label.property('unit_suffix') or '')}")

    def set_preview_image(self, image: QImage | QPixmap | None) -> None:
        if image is None:
            self._preview_pixmap = None
            self.preview_label.clear()
            self.preview_label.setText("Camera view unavailable")
            return
        if isinstance(image, QImage):
            self._preview_pixmap = QPixmap.fromImage(image)
        elif isinstance(image, QPixmap):
            self._preview_pixmap = QPixmap(image)
        else:
            raise TypeError("preview image must be QImage, QPixmap, or None")
        self._scale_preview_pixmap()

    def _scale_preview_pixmap(self) -> None:
        if self._preview_pixmap is None or self._preview_pixmap.isNull():
            return
        self.preview_label.setPixmap(
            self._preview_pixmap.scaled(
                self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._scale_preview_pixmap()

    def set_status(self, message: str, severity: str = "info") -> None:
        colors = {
            "info": "#245c8a",
            "success": "#236b35",
            "warning": "#8a5a00",
            "error": "#9b2525",
        }
        color = colors.get(severity, colors["info"])
        self.status_label.setText(str(message))
        self.status_label.setStyleSheet(
            f"padding: 6px; border-left: 4px solid {color};"
        )

    def set_next_action(self, message: str) -> None:
        self.next_action_label.setText(str(message))


__all__ = ["CameraTab"]
