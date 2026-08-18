"""Dual video display and presentation-only processing control panels."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from processing.motion_magnification import MotionMagnificationConfig
from processing.preview_processing import PROCESSED_FRAME_MODES


class AspectRatioImageLabel(QLabel):
    """Scale a stored image with letterboxing and no geometric distortion."""

    def __init__(self, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(placeholder, parent)
        self._source_pixmap: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(240, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            "background: #111418; color: #c9d1d9; border: 1px solid #4d5660;"
        )

    def set_image(self, image: QImage | QPixmap | None) -> None:
        if image is None:
            self._source_pixmap = None
            self.clear()
            return
        self._source_pixmap = QPixmap.fromImage(image) if isinstance(image, QImage) else QPixmap(image)
        self._rescale()

    def _rescale(self) -> None:
        if self._source_pixmap is None or self._source_pixmap.isNull():
            return
        self.setPixmap(
            self._source_pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()


class VideoDisplayArea(QWidget):
    """Vertically aligned original and selected processed previews."""

    processed_mode_changed = Signal(str)
    overlays_changed = Signal(bool)
    save_original_requested = Signal()
    save_processed_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("video_display_area")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        original_group = QGroupBox("Original Frame")
        original_group.setObjectName("original_frame_panel")
        original_layout = QVBoxLayout(original_group)
        original_actions = QHBoxLayout()
        self.overlay_check = QCheckBox("Show ROI overlays")
        self.overlay_check.setChecked(False)
        self.overlay_check.setVisible(False)
        self.save_original_button = QPushButton("Save Original Frame")
        original_actions.addWidget(self.overlay_check)
        original_actions.addStretch(1)
        original_actions.addWidget(self.save_original_button)
        self.original_label = AspectRatioImageLabel("Original camera frame unavailable")
        self.original_label.setObjectName("original_frame_display")
        self.original_status = QLabel(
            "Unannotated camera view after rotation/mirroring; no overlays or analysis"
        )
        original_layout.addLayout(original_actions)
        original_layout.addWidget(self.original_label, 1)
        original_layout.addWidget(self.original_status)

        processed_group = QGroupBox("Processed Frame")
        processed_group.setObjectName("processed_frame_panel")
        processed_layout = QVBoxLayout(processed_group)
        processed_actions = QHBoxLayout()
        processed_actions.addWidget(QLabel("Mode:"))
        self.processed_mode_combo = QComboBox()
        self.processed_mode_combo.setObjectName("processed_frame_mode")
        self.processed_mode_combo.addItems(PROCESSED_FRAME_MODES)
        self.save_processed_button = QPushButton("Save Processed Frame")
        processed_actions.addWidget(self.processed_mode_combo, 1)
        processed_actions.addWidget(self.save_processed_button)
        self.processed_label = AspectRatioImageLabel("Processed frame unavailable")
        self.processed_label.setObjectName("processed_frame_display")
        self.processed_status = QLabel(PROCESSED_FRAME_MODES[0])
        processed_layout.addLayout(processed_actions)
        processed_layout.addWidget(self.processed_label, 1)
        processed_layout.addWidget(self.processed_status)

        root.addWidget(original_group, 1)
        root.addWidget(processed_group, 1)
        self.processed_mode_combo.currentTextChanged.connect(
            self._processed_mode_changed
        )
        self.overlay_check.toggled.connect(self.overlays_changed)
        self.save_original_button.clicked.connect(self.save_original_requested)
        self.save_processed_button.clicked.connect(self.save_processed_requested)

    def _processed_mode_changed(self, mode: str) -> None:
        self.processed_status.setText(str(mode))
        self.processed_mode_changed.emit(str(mode))


class ImageProcessingTab(QWidget):
    """Display mode and force-light HSV source controls."""

    processed_mode_changed = Signal(str)
    analysis_source_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.processed_mode_combo = QComboBox()
        self.processed_mode_combo.addItems(PROCESSED_FRAME_MODES)
        self.analysis_source_combo = QComboBox()
        self.analysis_source_combo.addItems(
            (
                "Background-subtracted frame",
                "Original frame",
                "Motion-magnified frame",
            )
        )
        form.addRow("Processed preview:", self.processed_mode_combo)
        form.addRow("Force-light HSV source:", self.analysis_source_combo)
        root.addLayout(form)
        self.analysis_warning = QLabel(
            "The ROI HSV H/S/V and baseline-corrected delta-V values now come from "
            "the motion-magnified output. Calibrate force against this same source; "
            "a calibration made from original pixels is not interchangeable."
        )
        self.analysis_warning.setWordWrap(True)
        self.analysis_warning.setStyleSheet(
            "padding: 8px; background: #fff3cd; color: #664d03; border: 1px solid #ffecb5;"
        )
        self.analysis_warning.hide()
        root.addWidget(self.analysis_warning)
        self.current_source_label = QLabel(
            "Current force-light HSV source: Background-subtracted frame"
        )
        self.current_source_label.setWordWrap(True)
        root.addWidget(self.current_source_label)
        root.addStretch(1)
        self.processed_mode_combo.currentTextChanged.connect(
            self.processed_mode_changed
        )
        self.analysis_source_combo.currentTextChanged.connect(self._source_changed)

    def _source_changed(self, source: str) -> None:
        self.analysis_warning.setVisible(source == "Motion-magnified frame")
        self.current_source_label.setText(
            f"Current force-light HSV source: {source}"
        )
        self.analysis_source_changed.emit(source)


class MotionMagnificationTab(QWidget):
    """Validated profile editor; processing is owned by a worker thread."""

    apply_requested = Signal(object)
    reset_filter_requested = Signal()
    save_profile_requested = Signal(object)
    load_profile_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        self.enabled_check = QCheckBox("Enable Motion Magnification")
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(("Color magnification", "Intensity-only magnification"))
        self.amplification_spin = QDoubleSpinBox()
        self.amplification_spin.setRange(0.0, 300.0)
        self.amplification_spin.setValue(30.0)
        self.lower_spin = QDoubleSpinBox()
        self.lower_spin.setRange(0.001, 100.0)
        self.lower_spin.setDecimals(3)
        self.lower_spin.setValue(0.4)
        self.lower_spin.setSuffix(" Hz")
        self.upper_spin = QDoubleSpinBox()
        self.upper_spin.setRange(0.002, 200.0)
        self.upper_spin.setDecimals(3)
        self.upper_spin.setValue(3.0)
        self.upper_spin.setSuffix(" Hz")
        self.chrominance_spin = QDoubleSpinBox()
        self.chrominance_spin.setRange(0.0, 2.0)
        self.chrominance_spin.setValue(0.5)
        self.levels_spin = QSpinBox()
        self.levels_spin.setRange(1, 8)
        self.levels_spin.setValue(3)
        self.lambda_spin = QDoubleSpinBox()
        self.lambda_spin.setRange(0.01, 100000.0)
        self.lambda_spin.setValue(16.0)
        self.lambda_spin.setSuffix(" px")
        self.width_spin = QSpinBox()
        self.width_spin.setRange(0, 8192)
        self.width_spin.setSpecialValueText("Auto")
        self.height_spin = QSpinBox()
        self.height_spin.setRange(0, 8192)
        self.height_spin.setSpecialValueText("Auto")
        self.downscale_spin = QDoubleSpinBox()
        self.downscale_spin.setRange(0.1, 1.0)
        self.downscale_spin.setSingleStep(0.1)
        self.downscale_spin.setValue(0.5)
        self.target_fps_spin = QDoubleSpinBox()
        self.target_fps_spin.setRange(1.0, 240.0)
        self.target_fps_spin.setValue(20.0)
        self.target_fps_spin.setSuffix(" FPS")
        self.roi_only_check = QCheckBox("Process only the union of configured ROIs")
        form.addRow(self.enabled_check)
        form.addRow("Processing mode:", self.mode_combo)
        form.addRow("Amplification factor:", self.amplification_spin)
        form.addRow("Lower cutoff frequency:", self.lower_spin)
        form.addRow("Upper cutoff frequency:", self.upper_spin)
        form.addRow("Chrominance gain:", self.chrominance_spin)
        form.addRow("Pyramid levels:", self.levels_spin)
        form.addRow("Spatial wavelength (lambda_c):", self.lambda_spin)
        form.addRow("Processing width:", self.width_spin)
        form.addRow("Processing height:", self.height_spin)
        form.addRow("Downscale factor:", self.downscale_spin)
        form.addRow("Target processing rate:", self.target_fps_spin)
        form.addRow(self.roi_only_check)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("Apply Parameters")
        self.reset_filter_button = QPushButton("Reset Temporal Filter")
        self.restore_button = QPushButton("Restore Defaults")
        self.save_button = QPushButton("Save Profile")
        self.load_button = QPushButton("Load Profile")
        for button in (
            self.apply_button,
            self.reset_filter_button,
            self.restore_button,
            self.save_button,
            self.load_button,
        ):
            buttons.addWidget(button)
        root.addLayout(buttons)
        self.status_label = QLabel("Motion magnification disabled.")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self.apply_button.clicked.connect(self._emit_apply)
        self.reset_filter_button.clicked.connect(self.reset_filter_requested)
        self.restore_button.clicked.connect(
            lambda: self.set_configuration(MotionMagnificationConfig())
        )
        self.save_button.clicked.connect(
            lambda: self.save_profile_requested.emit(self.configuration())
        )
        self.load_button.clicked.connect(self.load_profile_requested)

    def configuration(self) -> MotionMagnificationConfig:
        return MotionMagnificationConfig(
            enabled=self.enabled_check.isChecked(),
            mode=self.mode_combo.currentText(),
            amplification=self.amplification_spin.value(),
            lower_cutoff_hz=self.lower_spin.value(),
            upper_cutoff_hz=self.upper_spin.value(),
            chrominance_gain=self.chrominance_spin.value(),
            pyramid_levels=self.levels_spin.value(),
            lambda_c=self.lambda_spin.value(),
            processing_width=self.width_spin.value(),
            processing_height=self.height_spin.value(),
            downscale_factor=self.downscale_spin.value(),
            target_fps=self.target_fps_spin.value(),
            roi_only=self.roi_only_check.isChecked(),
        )

    def set_configuration(self, config: MotionMagnificationConfig) -> None:
        self.enabled_check.setChecked(config.enabled)
        self.mode_combo.setCurrentText(config.mode)
        self.amplification_spin.setValue(config.amplification)
        self.lower_spin.setValue(config.lower_cutoff_hz)
        self.upper_spin.setValue(config.upper_cutoff_hz)
        self.chrominance_spin.setValue(config.chrominance_gain)
        self.levels_spin.setValue(config.pyramid_levels)
        self.lambda_spin.setValue(config.lambda_c)
        self.width_spin.setValue(config.processing_width)
        self.height_spin.setValue(config.processing_height)
        self.downscale_spin.setValue(config.downscale_factor)
        self.target_fps_spin.setValue(config.target_fps)
        self.roi_only_check.setChecked(config.roi_only)

    def _emit_apply(self) -> None:
        try:
            config = self.configuration()
        except Exception as exc:
            self.status_label.setText(str(exc))
            return
        self.apply_requested.emit(config)

    def set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(str(message))
        self.status_label.setStyleSheet(
            "color: #9b2525;" if error else "color: #236b35;"
        )


class GraphExportSettingsTab(QWidget):
    save_spatial_requested = Signal()
    save_lines_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        note = QLabel(
            "Graph exports use the exact current ROI values and authoritative elapsed "
            "timestamps. Configure the graph view in ROI/Baseline or Recording, then save here."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.save_spatial_button = QPushButton("Save Current Spatial Heatmap")
        self.save_lines_button = QPushButton("Save Current Line Graph")
        layout.addWidget(self.save_spatial_button)
        layout.addWidget(self.save_lines_button)
        layout.addStretch(1)
        self.save_spatial_button.clicked.connect(self.save_spatial_requested)
        self.save_lines_button.clicked.connect(self.save_lines_requested)


class SystemStatusTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setPlaceholderText("Runtime status and actionable errors appear here.")
        self.performance_label = QLabel("Performance data unavailable")
        self.performance_label.setWordWrap(True)
        layout.addWidget(self.performance_label)
        layout.addWidget(self.status_text, 1)

    def append_status(self, component: str, message: str) -> None:
        self.status_text.append(f"[{component}] {message}")

    def update_performance(self, values: Mapping[str, object]) -> None:
        self.performance_label.setText(
            " | ".join(f"{key}: {value}" for key, value in values.items())
        )


__all__ = [
    "GraphExportSettingsTab",
    "ImageProcessingTab",
    "MotionMagnificationTab",
    "SystemStatusTab",
    "VideoDisplayArea",
]
