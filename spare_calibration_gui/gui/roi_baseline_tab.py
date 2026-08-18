"""ROI/baseline presentation widgets and reusable optical graph panel.

This module owns no camera, baseline calculation, recording, or export I/O.
Buttons emit requests for worker/controller code, and public setters only render
already-calculated state.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Mapping, Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QGraphicsRectItem,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.models import ROI
from processing.roi_manager import (
    ROI_COUNT,
    ROILayout,
    ROIValidationResult,
    order_rois,
    validate_rois,
)


GRAPH_VIEWS = ("Heatmap", "Temporal ROI Lines", "Current Spatial Profile")
GRAPH_METRICS = (
    "Mean delta V",
    "Integrated delta V",
    "Maximum delta V",
    "Mean HSV V (0-255)",
)


class _ROIDrawViewBox(pg.ViewBox):
    """View box that turns one explicit left-button drag into a rectangle."""

    rectangle_drawn = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._draw_enabled = False
        self._draw_preview: QGraphicsRectItem | None = None

    @property
    def draw_enabled(self) -> bool:
        return self._draw_enabled

    def set_draw_enabled(self, enabled: bool) -> None:
        self._draw_enabled = bool(enabled)
        if not self._draw_enabled and self._draw_preview is not None:
            self.removeItem(self._draw_preview)
            self._draw_preview = None

    def mouseDragEvent(self, event, axis=None) -> None:  # noqa: N802 - Qt API
        if not self._draw_enabled or event.button() != Qt.MouseButton.LeftButton:
            super().mouseDragEvent(event, axis=axis)
            return
        event.accept()
        start = self.mapToView(event.buttonDownPos())
        current = self.mapToView(event.pos())
        rectangle = QRectF(start, current).normalized()
        if self._draw_preview is None:
            self._draw_preview = QGraphicsRectItem()
            self._draw_preview.setPen(pg.mkPen("y", width=2, style=Qt.PenStyle.DashLine))
            self._draw_preview.setZValue(20)
            self.addItem(self._draw_preview, ignoreBounds=True)
        self._draw_preview.setRect(rectangle)
        if event.isFinish():
            self.set_draw_enabled(False)
            self.rectangle_drawn.emit(rectangle)


@dataclass(frozen=True, slots=True)
class TemporalDisplayRow:
    """One authoritative displayed timestamp and its ordered nine ROI values."""

    capture_frame_id: int
    elapsed_time_s: float
    values: tuple[float, ...]


class OpticalGraphPanel(QWidget):
    """Reusable fixed-scale heatmap, temporal-line, and spatial-profile view."""

    save_spatial_graph_requested = Signal(object)
    save_line_graph_requested = Signal(object)
    metric_requested = Signal(str)
    view_changed = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        history_seconds: float = 30.0,
        max_history_points: int = 3000,
        scale_min: float = 0.0,
        scale_max: float = 255.0,
    ) -> None:
        super().__init__(parent)
        if history_seconds <= 0 or max_history_points < 1:
            raise ValueError("temporal history limits must be positive")
        if not scale_max > scale_min:
            raise ValueError("graph scale maximum must exceed minimum")
        self.history_seconds = float(history_seconds)
        self.max_history_points = int(max_history_points)
        self.scale_min = float(scale_min)
        self.scale_max = float(scale_max)
        self._current_values = (0.0,) * ROI_COUNT
        self._current_frame_id: int | None = None
        self._current_elapsed_time_s: float | None = None
        self._current_host_monotonic_ns: int | None = None
        self._temporal_history: list[TemporalDisplayRow] = []

        root = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Graph view:"))
        self.view_selector = QComboBox()
        self.view_selector.setObjectName("graph_view_selector")
        self.view_selector.addItems(GRAPH_VIEWS)
        controls.addWidget(self.view_selector)
        controls.addWidget(QLabel("Metric:"))
        self.metric_selector = QComboBox()
        self.metric_selector.setObjectName("graph_metric_selector")
        self.metric_selector.addItems(GRAPH_METRICS)
        controls.addWidget(self.metric_selector)
        controls.addWidget(QLabel("Display maximum:"))
        self.scale_max_spin = QDoubleSpinBox()
        self.scale_max_spin.setRange(0.001, 1_000_000_000.0)
        self.scale_max_spin.setDecimals(3)
        self.scale_max_spin.setValue(self.scale_max)
        self.scale_max_spin.setToolTip(
            "Fixed heatmap/profile maximum; the graph never auto-scales per frame."
        )
        controls.addWidget(self.scale_max_spin)
        controls.addStretch(1)
        root.addLayout(controls)
        self.data_source_label = QLabel(
            "HSV measurement source: original camera frame"
        )
        self.data_source_label.setWordWrap(True)
        self.data_source_label.setObjectName("optical_graph_data_source")
        root.addWidget(self.data_source_label)

        self.stack = QStackedWidget()
        self.stack.setObjectName("optical_graph_stack")
        self.stack.addWidget(self._create_heatmap_page())
        self.stack.addWidget(self._create_temporal_page())
        self.stack.addWidget(self._create_profile_page())
        root.addWidget(self.stack, 1)

        save_row = QHBoxLayout()
        self.save_spatial_button = QPushButton("Save Spatial Graph")
        self.save_spatial_button.setObjectName("save_spatial_graph_button")
        self.save_line_button = QPushButton("Save Line Graph")
        self.save_line_button.setObjectName("save_line_graph_button")
        save_row.addWidget(self.save_spatial_button)
        save_row.addWidget(self.save_line_button)
        save_row.addStretch(1)
        root.addLayout(save_row)

        self.view_selector.currentIndexChanged.connect(self._change_view)
        self.metric_selector.currentTextChanged.connect(self.metric_requested.emit)
        self.scale_max_spin.valueChanged.connect(
            lambda value: self.set_graph_scale(self.scale_min, value)
        )
        self.save_spatial_button.clicked.connect(
            lambda: self.save_spatial_graph_requested.emit(self.current_snapshot())
        )
        self.save_line_button.clicked.connect(
            lambda: self.save_line_graph_requested.emit(self.current_snapshot())
        )
        self._change_view(0)
        self.set_current_values(self._current_values)
        self.set_graph_scale(self.scale_min, self.scale_max)

    def _create_heatmap_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.heatmap_plot = pg.PlotWidget()
        self.heatmap_plot.setObjectName("roi_heatmap_plot")
        self.heatmap_plot.setAspectLocked(True)
        self.heatmap_plot.setMouseEnabled(x=False, y=False)
        self.heatmap_plot.setXRange(-0.5, 2.5, padding=0)
        self.heatmap_plot.setYRange(-0.5, 2.5, padding=0)
        self.heatmap_plot.getViewBox().invertY(True)
        self.heatmap_plot.getAxis("bottom").setTicks(
            [[(0, "Column 1"), (1, "Column 2"), (2, "Column 3")]]
        )
        self.heatmap_plot.getAxis("left").setTicks(
            [[(0, "Row 1"), (1, "Row 2"), (2, "Row 3")]]
        )
        self.heatmap_image = pg.ImageItem(axisOrder="row-major")
        self.heatmap_image.setRect(QRectF(-0.5, -0.5, 3.0, 3.0))
        self.heatmap_plot.addItem(self.heatmap_image)
        self.heatmap_value_labels: tuple[pg.TextItem, ...] = tuple(
            pg.TextItem("0.000", color="w", anchor=(0.5, 0.5))
            for _ in range(ROI_COUNT)
        )
        for index, label in enumerate(self.heatmap_value_labels):
            label.setPos(index % 3, index // 3)
            self.heatmap_plot.addItem(label)
        layout.addWidget(self.heatmap_plot)
        return page

    def _create_temporal_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        visibility = QHBoxLayout()
        self.roi_visibility_checkboxes: tuple[QCheckBox, ...] = tuple(
            QCheckBox(f"ROI {roi_id}") for roi_id in range(1, ROI_COUNT + 1)
        )
        for checkbox in self.roi_visibility_checkboxes:
            checkbox.setChecked(True)
            visibility.addWidget(checkbox)
        self.show_all_button = QPushButton("Show All")
        self.hide_all_button = QPushButton("Hide All")
        visibility.addWidget(self.show_all_button)
        visibility.addWidget(self.hide_all_button)
        layout.addLayout(visibility)

        history_row = QHBoxLayout()
        history_row.addWidget(QLabel("Displayed history (s):"))
        self.history_seconds_spin = QDoubleSpinBox()
        self.history_seconds_spin.setRange(0.1, 3600.0)
        self.history_seconds_spin.setValue(self.history_seconds)
        self.history_seconds_spin.setToolTip(
            "Only the live display is bounded; raw per-frame data remain external and complete."
        )
        history_row.addWidget(self.history_seconds_spin)
        history_row.addStretch(1)
        layout.addLayout(history_row)

        self.temporal_plot = pg.PlotWidget()
        self.temporal_plot.setObjectName("temporal_roi_lines_plot")
        self.temporal_plot.setLabel("bottom", "Authoritative elapsed time", units="s")
        self.temporal_plot.setLabel("left", "HSV V / baseline-corrected delta V")
        self.temporal_plot.showGrid(x=True, y=True, alpha=0.25)
        self.temporal_plot.addLegend()
        self.temporal_curves: tuple[pg.PlotDataItem, ...] = tuple(
            self.temporal_plot.plot(
                [],
                [],
                pen=pg.mkPen(pg.intColor(index, ROI_COUNT), width=2),
                name=f"ROI {index + 1}",
            )
            for index in range(ROI_COUNT)
        )
        for checkbox, curve in zip(
            self.roi_visibility_checkboxes, self.temporal_curves, strict=True
        ):
            checkbox.toggled.connect(curve.setVisible)
        self.show_all_button.clicked.connect(lambda: self.set_all_lines_visible(True))
        self.hide_all_button.clicked.connect(lambda: self.set_all_lines_visible(False))
        self.history_seconds_spin.valueChanged.connect(self.set_history_seconds)
        layout.addWidget(self.temporal_plot)
        return page

    def _create_profile_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.profile_context_label = QLabel("Current frame: unavailable")
        layout.addWidget(self.profile_context_label)
        self.profile_plot = pg.PlotWidget()
        self.profile_plot.setObjectName("current_spatial_profile_plot")
        self.profile_plot.setLabel("bottom", "ROI number")
        self.profile_plot.setLabel("left", "HSV V / baseline-corrected delta V")
        self.profile_plot.showGrid(x=True, y=True, alpha=0.25)
        self.profile_plot.setXRange(1, 9, padding=0.05)
        self.profile_plot.getAxis("bottom").setTicks(
            [[(index, f"ROI {index}") for index in range(1, 10)]]
        )
        self.spatial_profile_curve = self.profile_plot.plot(
            np.arange(1, 10),
            np.zeros(9),
            pen=pg.mkPen("c", width=2),
            symbol="o",
            symbolBrush="c",
        )
        self.profile_value_labels: tuple[pg.TextItem, ...] = tuple(
            pg.TextItem("0.000", color="w", anchor=(0.5, 1.0))
            for _ in range(ROI_COUNT)
        )
        for index, label in enumerate(self.profile_value_labels, start=1):
            label.setPos(index, 0)
            self.profile_plot.addItem(label)
        layout.addWidget(self.profile_plot)
        return page

    @property
    def current_values(self) -> tuple[float, ...]:
        """Exact nine values shared by the heatmap and spatial profile."""

        return self._current_values

    @property
    def heatmap_matrix(self) -> np.ndarray:
        """Return a copy in required ROI 1-3 / 4-6 / 7-9 row-major order."""

        return np.asarray(self._current_values, dtype=float).reshape(3, 3).copy()

    @property
    def spatial_profile_values(self) -> tuple[float, ...]:
        return self._current_values

    @property
    def temporal_history(self) -> tuple[TemporalDisplayRow, ...]:
        return tuple(self._temporal_history)

    def clear_temporal_history(self) -> None:
        """Clear display-only history without touching any recorded raw data."""

        self._temporal_history.clear()
        self._update_temporal_curves()

    def set_data_source(self, source: str) -> None:
        """Show which pixels supplied the displayed HSV measurements."""

        self.data_source_label.setText(f"HSV measurement source: {str(source)}")

    def set_current_values(
        self,
        values: Sequence[float] | Iterable[float],
        *,
        capture_frame_id: int | None = None,
        elapsed_time_s: float | None = None,
        host_monotonic_ns: int | None = None,
    ) -> None:
        """Render one controller-supplied vector without recomputing it."""

        ordered = tuple(float(value) for value in values)
        if len(ordered) != ROI_COUNT:
            raise ValueError("exactly nine ordered ROI graph values are required")
        self._current_values = ordered
        self._current_frame_id = capture_frame_id
        self._current_elapsed_time_s = elapsed_time_s
        self._current_host_monotonic_ns = host_monotonic_ns
        matrix = np.asarray(ordered, dtype=float).reshape(3, 3)
        self.heatmap_image.setImage(
            matrix, autoLevels=False, levels=(self.scale_min, self.scale_max)
        )
        x_values = np.arange(1, ROI_COUNT + 1, dtype=float)
        y_values = np.asarray(ordered, dtype=float)
        self.spatial_profile_curve.setData(x_values, y_values)
        for index, (heat_label, profile_label, value) in enumerate(
            zip(
                self.heatmap_value_labels,
                self.profile_value_labels,
                ordered,
                strict=True,
            )
        ):
            text = self._format_value(value)
            heat_label.setText(text)
            profile_label.setText(text)
            profile_label.setPos(index + 1, value if math.isfinite(value) else 0.0)
        frame_text = "unavailable" if capture_frame_id is None else str(capture_frame_id)
        elapsed_text = (
            "unavailable" if elapsed_time_s is None else f"{elapsed_time_s:.3f} s"
        )
        self.profile_context_label.setText(
            f"Current frame: {frame_text} | Elapsed: {elapsed_text}"
        )

    def append_temporal_values(
        self,
        capture_frame_id: int,
        elapsed_time_s: float,
        values: Sequence[float] | Iterable[float],
    ) -> None:
        """Append authoritative elapsed time and bound only the live display."""

        ordered = tuple(float(value) for value in values)
        if len(ordered) != ROI_COUNT:
            raise ValueError("exactly nine ordered ROI values are required")
        if capture_frame_id < 0 or not math.isfinite(elapsed_time_s) or elapsed_time_s < 0:
            raise ValueError("frame ID and elapsed time must be nonnegative")
        if self._temporal_history and (
            elapsed_time_s < self._temporal_history[-1].elapsed_time_s
        ):
            raise ValueError("authoritative elapsed timestamps cannot move backward")
        self._temporal_history.append(
            TemporalDisplayRow(capture_frame_id, float(elapsed_time_s), ordered)
        )
        self._trim_history()
        self._update_temporal_curves()

    def set_history_seconds(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("history seconds must be finite and positive")
        self.history_seconds = float(seconds)
        if not math.isclose(self.history_seconds_spin.value(), seconds):
            self.history_seconds_spin.setValue(seconds)
        self._trim_history()
        self._update_temporal_curves()

    def set_graph_scale(self, minimum: float, maximum: float) -> None:
        if not math.isfinite(minimum) or not math.isfinite(maximum) or maximum <= minimum:
            raise ValueError("fixed graph scale requires finite minimum < maximum")
        self.scale_min = float(minimum)
        self.scale_max = float(maximum)
        if not math.isclose(self.scale_max_spin.value(), maximum):
            self.scale_max_spin.setValue(maximum)
        self.heatmap_image.setLevels((minimum, maximum))
        self.temporal_plot.setYRange(minimum, maximum, padding=0)
        self.profile_plot.setYRange(minimum, maximum, padding=0)

    def set_all_lines_visible(self, visible: bool) -> None:
        for checkbox in self.roi_visibility_checkboxes:
            checkbox.setChecked(visible)

    def current_snapshot(self) -> dict[str, object]:
        """Return exact displayed data for a worker-side export request."""

        current_view = self.view_selector.currentText()
        active_plot = {
            "Heatmap": self.heatmap_plot,
            "Temporal ROI Lines": self.temporal_plot,
            "Current Spatial Profile": self.profile_plot,
        }[current_view]
        x_range, y_range = active_plot.getViewBox().viewRange()
        return {
            "view": current_view,
            "metric": self.metric_selector.currentText(),
            "values": self._current_values,
            "capture_frame_id": self._current_frame_id,
            "elapsed_time_s": self._current_elapsed_time_s,
            "host_monotonic_ns": self._current_host_monotonic_ns,
            "scale_min": self.scale_min,
            "scale_max": self.scale_max,
            "visible_roi_ids": tuple(
                index + 1
                for index, checkbox in enumerate(self.roi_visibility_checkboxes)
                if checkbox.isChecked()
            ),
            "temporal_history": self.temporal_history,
            "history_seconds": self.history_seconds,
            "x_axis_limits": tuple(float(value) for value in x_range),
            "y_axis_limits": tuple(float(value) for value in y_range),
        }

    def _change_view(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        view = self.view_selector.itemText(index)
        self.save_spatial_button.setEnabled(view == "Heatmap")
        self.save_line_button.setEnabled(view != "Heatmap")
        self.view_changed.emit(view)

    def _trim_history(self) -> None:
        if not self._temporal_history:
            return
        cutoff = self._temporal_history[-1].elapsed_time_s - self.history_seconds
        while self._temporal_history and (
            self._temporal_history[0].elapsed_time_s < cutoff
        ):
            self._temporal_history.pop(0)
        if len(self._temporal_history) > self.max_history_points:
            del self._temporal_history[: -self.max_history_points]

    def _update_temporal_curves(self) -> None:
        timestamps = np.asarray(
            [row.elapsed_time_s for row in self._temporal_history], dtype=float
        )
        for roi_index, curve in enumerate(self.temporal_curves):
            values = np.asarray(
                [row.values[roi_index] for row in self._temporal_history], dtype=float
            )
            curve.setData(timestamps, values)
        if timestamps.size:
            self.temporal_plot.setXRange(
                max(0.0, timestamps[-1] - self.history_seconds),
                max(self.history_seconds, timestamps[-1]),
                padding=0,
            )

    @staticmethod
    def _format_value(value: float) -> str:
        return f"{value:.3f}" if math.isfinite(value) else "NaN"


class ROIBaselineTab(QWidget):
    """Manual nine-ROI editing and unloaded-baseline workflow presentation."""

    roi_layout_changed = Signal(object)
    baseline_invalidated = Signal(str)
    rois_valid_changed = Signal(bool)
    save_roi_layout_requested = Signal(object)
    load_roi_layout_requested = Signal()
    baseline_capture_requested = Signal()
    baseline_accept_requested = Signal()
    baseline_repeat_requested = Signal()
    save_spatial_graph_requested = Signal(object)
    save_line_graph_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        frame_size: tuple[int, int] = (640, 480),
    ) -> None:
        super().__init__(parent)
        self.frame_width, self.frame_height = frame_size
        if self.frame_width <= 0 or self.frame_height <= 0:
            raise ValueError("frame dimensions must be positive")
        self._updating_rois = False
        self._locked = False
        self._baseline_capture_active = False

        root = QVBoxLayout(self)
        self.instructions_label = QLabel(
            "Define exactly nine taxel rectangles on the unloaded live preview, "
            "then capture and accept a baseline. ROI edits invalidate the baseline."
        )
        self.instructions_label.setWordWrap(True)
        self.instructions_label.setObjectName("roi_instructions")
        root.addWidget(self.instructions_label)
        self.status_label = QLabel("Status: define and validate all nine ROIs.")
        self.status_label.setWordWrap(True)
        self.next_action_label = QLabel("Next recommended action: adjust ROI 1 through ROI 9.")
        self.next_action_label.setWordWrap(True)
        root.addWidget(self.status_label)
        root.addWidget(self.next_action_label)

        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        upper_splitter = QSplitter(Qt.Orientation.Horizontal)
        preview_widget = self._create_preview_widget()
        controls_scroll = self._create_controls_scroll()
        preview_widget.setMinimumWidth(0)
        controls_scroll.setMinimumWidth(0)
        upper_splitter.setChildrenCollapsible(True)
        upper_splitter.addWidget(preview_widget)
        upper_splitter.addWidget(controls_scroll)
        upper_splitter.setStretchFactor(0, 3)
        upper_splitter.setStretchFactor(1, 2)
        vertical_splitter.addWidget(upper_splitter)
        self.graph_panel = OpticalGraphPanel()
        vertical_splitter.addWidget(self.graph_panel)
        vertical_splitter.setStretchFactor(0, 3)
        vertical_splitter.setStretchFactor(1, 2)
        root.addWidget(vertical_splitter, 1)

        self.graph_panel.save_spatial_graph_requested.connect(
            self.save_spatial_graph_requested.emit
        )
        self.graph_panel.save_line_graph_requested.connect(
            self.save_line_graph_requested.emit
        )
        self._create_roi_overlays(self._default_rois())
        self._refresh_validation(emit=False)

    def _create_preview_widget(self) -> QWidget:
        group = QGroupBox("Live Preview — annotations are display-only")
        layout = QVBoxLayout(group)
        self.preview_widget = pg.GraphicsLayoutWidget()
        self.preview_view_box = _ROIDrawViewBox()
        self.preview_plot = self.preview_widget.addPlot(viewBox=self.preview_view_box)
        self.preview_plot.hideAxis("left")
        self.preview_plot.hideAxis("bottom")
        self.preview_plot.setAspectLocked(True)
        self.preview_plot.getViewBox().invertY(True)
        self.preview_plot.setXRange(0, self.frame_width, padding=0)
        self.preview_plot.setYRange(0, self.frame_height, padding=0)
        self.preview_image = pg.ImageItem(axisOrder="row-major")
        self.preview_plot.addItem(self.preview_image)
        self.roi_items: tuple[pg.RectROI, ...] = ()
        self.roi_labels: tuple[pg.TextItem, ...] = ()
        layout.addWidget(self.preview_widget)
        return group

    def _create_controls_scroll(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)

        self.roi_edit_group = QGroupBox("ROI geometry (full-frame pixels)")
        grid = QGridLayout(self.roi_edit_group)
        for column, header in enumerate(("ROI", "x", "y", "width", "height")):
            grid.addWidget(QLabel(header), 0, column)
        self.roi_editors: dict[int, dict[str, QSpinBox]] = {}
        defaults = self._default_rois()
        for row_index, roi in enumerate(defaults, start=1):
            grid.addWidget(QLabel(f"ROI {roi.roi_id}"), row_index, 0)
            editors: dict[str, QSpinBox] = {}
            for column, (field, maximum) in enumerate(
                (
                    ("x", self.frame_width - 1),
                    ("y", self.frame_height - 1),
                    ("width", self.frame_width),
                    ("height", self.frame_height),
                ),
                start=1,
            ):
                editor = QSpinBox()
                editor.setObjectName(f"roi{roi.roi_id}_{field}")
                editor.setRange(1 if field in {"width", "height"} else 0, maximum)
                editor.setValue(int(getattr(roi, field)))
                editor.valueChanged.connect(
                    lambda _value, roi_id=roi.roi_id: self._numeric_roi_changed(roi_id)
                )
                editors[field] = editor
                grid.addWidget(editor, row_index, column)
            self.roi_editors[roi.roi_id] = editors
        content_layout.addWidget(self.roi_edit_group)

        roi_buttons = QHBoxLayout()
        self.draw_roi_selector = QComboBox()
        self.draw_roi_selector.addItems(
            [f"ROI {roi_id}" for roi_id in range(1, ROI_COUNT + 1)]
        )
        self.draw_selected_roi_button = QPushButton("Draw Selected ROI")
        self.copy_roi1_size_button = QPushButton("Copy ROI 1 Size to ROI 2–9")
        self.reset_rois_button = QPushButton("Reset Nine ROIs")
        roi_buttons.addWidget(self.draw_roi_selector)
        roi_buttons.addWidget(self.draw_selected_roi_button)
        roi_buttons.addWidget(self.copy_roi1_size_button)
        roi_buttons.addWidget(self.reset_rois_button)
        content_layout.addLayout(roi_buttons)
        layout_buttons = QHBoxLayout()
        self.save_layout_button = QPushButton("Save ROI Layout JSON")
        self.load_layout_button = QPushButton("Load ROI Layout JSON")
        layout_buttons.addWidget(self.save_layout_button)
        layout_buttons.addWidget(self.load_layout_button)
        content_layout.addLayout(layout_buttons)
        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        self.validation_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        content_layout.addWidget(self.validation_label)

        self.baseline_group = QGroupBox("Unloaded Baseline")
        baseline_layout = QVBoxLayout(self.baseline_group)
        self.baseline_status_label = QLabel("Baseline invalid: capture an unloaded baseline.")
        self.baseline_status_label.setWordWrap(True)
        baseline_layout.addWidget(self.baseline_status_label)
        self.baseline_progress = QProgressBar()
        self.baseline_progress.setRange(0, 100)
        self.baseline_progress.setValue(0)
        baseline_layout.addWidget(self.baseline_progress)
        baseline_buttons = QHBoxLayout()
        self.capture_baseline_button = QPushButton("Capture Unloaded Baseline")
        self.accept_baseline_button = QPushButton("Accept Baseline")
        self.repeat_baseline_button = QPushButton("Repeat Baseline")
        self.accept_baseline_button.setEnabled(False)
        self.repeat_baseline_button.setEnabled(False)
        baseline_buttons.addWidget(self.capture_baseline_button)
        baseline_buttons.addWidget(self.accept_baseline_button)
        baseline_buttons.addWidget(self.repeat_baseline_button)
        baseline_layout.addLayout(baseline_buttons)
        content_layout.addWidget(self.baseline_group)
        content_layout.addStretch(1)
        scroll.setWidget(content)

        self.draw_selected_roi_button.clicked.connect(self._begin_draw_selected_roi)
        self.preview_view_box.rectangle_drawn.connect(self._finish_drawn_roi)
        self.copy_roi1_size_button.clicked.connect(self.copy_roi1_size)
        self.reset_rois_button.clicked.connect(
            lambda: self.set_roi_layout(self._default_rois(), emit_change=True)
        )
        self.save_layout_button.clicked.connect(
            lambda: self.save_roi_layout_requested.emit(self.current_layout())
        )
        self.load_layout_button.clicked.connect(self.load_roi_layout_requested.emit)
        self.capture_baseline_button.clicked.connect(self._request_baseline_capture)
        self.accept_baseline_button.clicked.connect(self.baseline_accept_requested.emit)
        self.repeat_baseline_button.clicked.connect(self._request_baseline_repeat)
        return scroll

    @property
    def rois(self) -> tuple[ROI, ...]:
        return tuple(
            ROI(
                roi_id=roi_id,
                x=editors["x"].value(),
                y=editors["y"].value(),
                width=editors["width"].value(),
                height=editors["height"].value(),
            )
            for roi_id, editors in sorted(self.roi_editors.items())
        )

    @property
    def validation(self) -> ROIValidationResult:
        return validate_rois(self.rois, self.frame_width, self.frame_height)

    def current_layout(self) -> ROILayout:
        return ROILayout.create(self.rois, self.frame_width, self.frame_height)

    def set_preview_image(self, image: np.ndarray) -> None:
        array = np.asarray(image)
        if array.ndim not in {2, 3}:
            raise ValueError("preview image must be grayscale or color")
        self.preview_image.setImage(array, autoLevels=False, levels=(0, 255))

    def set_frame_size(self, width: int, height: int) -> None:
        """Reset ROI geometry when the authoritative camera mode changes."""

        if width <= 0 or height <= 0:
            raise ValueError("frame dimensions must be positive")
        if (int(width), int(height)) == (self.frame_width, self.frame_height):
            return
        for item in self.roi_items:
            self.preview_plot.removeItem(item)
        for label in self.roi_labels:
            self.preview_plot.removeItem(label)
        self.roi_items = ()
        self.roi_labels = ()
        self.frame_width, self.frame_height = int(width), int(height)
        self.preview_plot.setXRange(0, self.frame_width, padding=0)
        self.preview_plot.setYRange(0, self.frame_height, padding=0)
        self._updating_rois = True
        try:
            for editors in self.roi_editors.values():
                for editor in editors.values():
                    editor.blockSignals(True)
                editors["x"].setMaximum(self.frame_width - 1)
                editors["y"].setMaximum(self.frame_height - 1)
                editors["width"].setMaximum(self.frame_width)
                editors["height"].setMaximum(self.frame_height)
                for editor in editors.values():
                    editor.blockSignals(False)
        finally:
            self._updating_rois = False
        defaults = self._default_rois()
        self._create_roi_overlays(defaults)
        self.set_roi_layout(defaults, emit_change=True)

    def set_roi_layout(
        self, rois: Sequence[ROI] | Iterable[ROI], *, emit_change: bool = True
    ) -> None:
        ordered = order_rois(rois)
        result = validate_rois(ordered, self.frame_width, self.frame_height)
        result.raise_for_errors()
        self._updating_rois = True
        try:
            for roi in ordered:
                editors = self.roi_editors[roi.roi_id]
                for field in ("x", "y", "width", "height"):
                    editor = editors[field]
                    editor.blockSignals(True)
                    editor.setValue(int(getattr(roi, field)))
                    editor.blockSignals(False)
                self._set_overlay_geometry(roi)
        finally:
            self._updating_rois = False
        self._refresh_validation(emit=emit_change)

    def copy_roi1_size(self) -> None:
        first = self.rois[0]
        copied = tuple(
            roi
            if roi.roi_id == 1
            else ROI(
                roi.roi_id,
                min(roi.x, self.frame_width - first.width),
                min(roi.y, self.frame_height - first.height),
                first.width,
                first.height,
            )
            for roi in self.rois
        )
        self.set_roi_layout(copied, emit_change=True)

    def set_baseline_capture_active(self, active: bool) -> None:
        self._baseline_capture_active = bool(active)
        self._apply_locked_state()
        if active:
            self.baseline_progress.setValue(0)
            self.baseline_status_label.setText(
                "Capturing unloaded frames; ROI editing is temporarily locked."
            )

    def set_baseline_capture_progress(self, percent: float, valid_frames: int) -> None:
        bounded = max(0, min(100, int(round(percent))))
        self.baseline_progress.setValue(bounded)
        self.baseline_progress.setFormat(f"{bounded}% — {valid_frames} valid frames")

    def set_baseline_result_available(self, available: bool, message: str = "") -> None:
        self.set_baseline_capture_active(False)
        self.accept_baseline_button.setEnabled(bool(available) and not self._locked)
        self.repeat_baseline_button.setEnabled(not self._locked)
        if message:
            self.baseline_status_label.setText(message)

    def set_baseline_status(
        self, valid: bool, reason: str = "", captured_at: str = ""
    ) -> None:
        if valid:
            detail = f" Captured {captured_at}." if captured_at else ""
            self.baseline_status_label.setText(f"Baseline valid.{detail}")
            self.status_label.setText("Status: nine ROIs and unloaded baseline are valid.")
            self.next_action_label.setText(
                "Next recommended action: continue to Load Cell calibration."
            )
        else:
            detail = reason or "capture an unloaded baseline"
            self.baseline_status_label.setText(f"Baseline invalid: {detail}.")

    def set_recording_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self._apply_locked_state()

    def _apply_locked_state(self) -> None:
        editing_enabled = not self._locked and not self._baseline_capture_active
        self.roi_edit_group.setEnabled(editing_enabled)
        for item in self.roi_items:
            item.translatable = editing_enabled
            for handle in item.getHandles():
                handle.setVisible(editing_enabled)
        for button in (
            self.draw_roi_selector,
            self.draw_selected_roi_button,
            self.copy_roi1_size_button,
            self.reset_rois_button,
            self.save_layout_button,
            self.load_layout_button,
        ):
            button.setEnabled(editing_enabled)
        if not editing_enabled:
            self.preview_view_box.set_draw_enabled(False)
            self.draw_selected_roi_button.setText("Draw Selected ROI")
        self.capture_baseline_button.setEnabled(not self._locked and not self._baseline_capture_active)
        if self._locked:
            self.accept_baseline_button.setEnabled(False)
            self.repeat_baseline_button.setEnabled(False)

    def _default_rois(self) -> tuple[ROI, ...]:
        cell_width = self.frame_width / 3.0
        cell_height = self.frame_height / 3.0
        width = max(1, int(cell_width * 0.6))
        height = max(1, int(cell_height * 0.6))
        return tuple(
            ROI(
                roi_id=index + 1,
                x=int((index % 3) * cell_width + (cell_width - width) / 2),
                y=int((index // 3) * cell_height + (cell_height - height) / 2),
                width=width,
                height=height,
            )
            for index in range(ROI_COUNT)
        )

    def _create_roi_overlays(self, rois: Sequence[ROI]) -> None:
        items: list[pg.RectROI] = []
        labels: list[pg.TextItem] = []
        bounds = QRectF(0, 0, self.frame_width, self.frame_height)
        for roi in rois:
            color = pg.intColor(roi.roi_id - 1, ROI_COUNT)
            item = pg.RectROI(
                (roi.x, roi.y),
                (roi.width, roi.height),
                pen=pg.mkPen(color, width=2),
                movable=True,
                removable=False,
                maxBounds=bounds,
            )
            item.setZValue(10)
            item.sigRegionChangeFinished.connect(
                lambda *_args, roi_id=roi.roi_id: self._overlay_changed(roi_id)
            )
            self.preview_plot.addItem(item)
            label = pg.TextItem(f"ROI {roi.roi_id}", color=color, anchor=(0, 1))
            label.setPos(roi.x, roi.y)
            label.setZValue(11)
            self.preview_plot.addItem(label)
            items.append(item)
            labels.append(label)
        self.roi_items = tuple(items)
        self.roi_labels = tuple(labels)

    def _numeric_roi_changed(self, roi_id: int) -> None:
        if self._updating_rois:
            return
        editors = self.roi_editors[roi_id]
        width = min(editors["width"].value(), self.frame_width)
        height = min(editors["height"].value(), self.frame_height)
        x = min(editors["x"].value(), self.frame_width - width)
        y = min(editors["y"].value(), self.frame_height - height)
        self.set_roi_layout(
            tuple(
                ROI(roi.roi_id, x, y, width, height)
                if roi.roi_id == roi_id
                else roi
                for roi in self.rois
            ),
            emit_change=True,
        )

    def _begin_draw_selected_roi(self) -> None:
        if self._locked or self._baseline_capture_active:
            return
        roi_id = self.draw_roi_selector.currentIndex() + 1
        self.preview_view_box.set_draw_enabled(True)
        self.draw_selected_roi_button.setText(f"Drag ROI {roi_id} on Preview…")
        self.status_label.setText(
            f"Status: drag a rectangle on the live preview to replace ROI {roi_id}."
        )

    def _finish_drawn_roi(self, rectangle: QRectF) -> None:
        self.preview_view_box.set_draw_enabled(False)
        if self._locked or self._baseline_capture_active:
            return
        roi_id = self.draw_roi_selector.currentIndex() + 1
        left = max(0.0, min(float(self.frame_width - 1), rectangle.left()))
        top = max(0.0, min(float(self.frame_height - 1), rectangle.top()))
        right = max(left + 1.0, min(float(self.frame_width), rectangle.right()))
        bottom = max(top + 1.0, min(float(self.frame_height), rectangle.bottom()))
        x = int(math.floor(left))
        y = int(math.floor(top))
        width = max(1, min(self.frame_width - x, int(math.ceil(right)) - x))
        height = max(1, min(self.frame_height - y, int(math.ceil(bottom)) - y))
        self.set_roi_layout(
            tuple(
                ROI(roi.roi_id, x, y, width, height)
                if roi.roi_id == roi_id
                else roi
                for roi in self.rois
            ),
            emit_change=True,
        )
        self.draw_selected_roi_button.setText("Draw Selected ROI")
        self.status_label.setText(f"Status: ROI {roi_id} was redrawn and validated.")

    def _overlay_changed(self, roi_id: int) -> None:
        if self._updating_rois or self._locked or self._baseline_capture_active:
            return
        item = self.roi_items[roi_id - 1]
        size = item.size()
        width = max(1, min(self.frame_width, int(round(size.x()))))
        height = max(1, min(self.frame_height, int(round(size.y()))))
        pos = item.pos()
        x = max(0, min(self.frame_width - width, int(round(pos.x()))))
        y = max(0, min(self.frame_height - height, int(round(pos.y()))))
        current = self.rois
        self.set_roi_layout(
            tuple(
                ROI(roi.roi_id, x, y, width, height)
                if roi.roi_id == roi_id
                else roi
                for roi in current
            ),
            emit_change=True,
        )

    def _set_overlay_geometry(self, roi: ROI) -> None:
        item = self.roi_items[roi.roi_id - 1]
        item.setPos((roi.x, roi.y), update=False, finish=False)
        item.setSize((roi.width, roi.height), update=False, finish=False)
        item.stateChanged(finish=False)
        self.roi_labels[roi.roi_id - 1].setPos(roi.x, roi.y)

    def _refresh_validation(self, *, emit: bool) -> None:
        result = self.validation
        if result.valid and result.overlap_pairs:
            self.validation_label.setText(
                "Valid bounds. Warning: " + "; ".join(result.warnings)
            )
        elif result.valid:
            self.validation_label.setText(
                "All nine ROIs are inside the frame with no overlaps."
            )
        else:
            self.validation_label.setText("Invalid: " + "; ".join(result.errors))
        if emit:
            self.roi_layout_changed.emit(self.rois)
            self.rois_valid_changed.emit(result.valid)
            self.baseline_invalidated.emit("ROI layout changed")
            self.set_baseline_status(False, "ROI layout changed")

    def _request_baseline_capture(self) -> None:
        if not self.validation.valid:
            self.status_label.setText("Status: correct invalid ROI geometry first.")
            return
        self.set_baseline_capture_active(True)
        self.baseline_capture_requested.emit()

    def _request_baseline_repeat(self) -> None:
        self.baseline_repeat_requested.emit()
        self._request_baseline_capture()


__all__ = [
    "GRAPH_METRICS",
    "GRAPH_VIEWS",
    "OpticalGraphPanel",
    "ROIBaselineTab",
    "TemporalDisplayRow",
]
