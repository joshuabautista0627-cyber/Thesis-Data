"""Reproducible OpenCV graph rendering and worker-backed export.

This module intentionally does not import Matplotlib or Qt.  OpenCV renders
report-ready PNG files on a dedicated executor while companion CSV and strict
JSON files preserve the exact plotted values, ordering, scales, and provenance.
The GUI can display the same values with pyqtgraph without sharing GUI objects
with this worker.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future, ThreadPoolExecutor
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Iterable, Mapping, Sequence
import uuid

import cv2
import numpy as np

from data.exporter import strict_json_dumps
from data.schemas import GRAPH_SPATIAL_COLUMNS, GRAPH_TEMPORAL_COLUMNS


HEATMAP_WIDTH = 1200
HEATMAP_HEIGHT = 900
LINE_GRAPH_WIDTH = 1600
LINE_GRAPH_HEIGHT = 900
ROI_ORDER = tuple(range(1, 10))
SPATIAL_CSV_COLUMNS = GRAPH_SPATIAL_COLUMNS
TEMPORAL_CSV_COLUMNS = GRAPH_TEMPORAL_COLUMNS


class GraphExportError(RuntimeError):
    """Base error for invalid graph data or failed artifact publication."""


class GraphDataError(GraphExportError, ValueError):
    """Raised when values cannot satisfy a reproducible graph contract."""


class ExistingGraphError(GraphExportError, FileExistsError):
    """Raised rather than silently overwriting any graph companion file."""


@dataclass(frozen=True, slots=True)
class GraphMetric:
    code: str
    description: str
    units: str
    column_suffix: str


METRICS: dict[str, GraphMetric] = {
    "mean_delta_v": GraphMetric(
        "mean_delta_v",
        "Mean positive baseline-corrected value-channel intensity",
        "delta V (0-255)",
        "delta_v_mean",
    ),
    "integrated_delta_v": GraphMetric(
        "integrated_delta_v",
        "Integrated positive baseline-corrected value-channel intensity",
        "delta V*px",
        "delta_v_sum",
    ),
    "maximum_delta_v": GraphMetric(
        "maximum_delta_v",
        "Maximum positive baseline-corrected value-channel intensity",
        "delta V (0-255)",
        "delta_v_max",
    ),
    "raw_mean_v": GraphMetric(
        "raw_mean_v",
        "Mean raw OpenCV value-channel intensity",
        "OpenCV V (0-255)",
        "mean_v",
    ),
}

_METRIC_ALIASES = {
    "delta_v_mean": "mean_delta_v",
    "mean positive delta v": "mean_delta_v",
    "delta_v_sum": "integrated_delta_v",
    "integrated delta v": "integrated_delta_v",
    "delta_v_max": "maximum_delta_v",
    "maximum delta v": "maximum_delta_v",
    "mean_v": "raw_mean_v",
    "raw mean v": "raw_mean_v",
}


def resolve_metric(metric: str | GraphMetric) -> GraphMetric:
    if isinstance(metric, GraphMetric):
        return metric
    normalized = str(metric).strip().lower().replace("-", "_")
    normalized = _METRIC_ALIASES.get(normalized, normalized)
    try:
        return METRICS[normalized]
    except KeyError as exc:
        raise GraphDataError(
            f"unsupported graph metric {metric!r}; choose {tuple(METRICS)}"
        ) from exc


@dataclass(frozen=True, slots=True)
class GraphContext:
    """Session provenance copied into every graph companion JSON."""

    session_id: str = ""
    trial_id: str = ""
    baseline_id: str = ""
    roi_layout_id: str = ""


def _values9(values: Sequence[float]) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 9:
        raise GraphDataError("exactly nine ROI values are required")
    if any(math.isinf(value) for value in result):
        raise GraphDataError("ROI graph values cannot be infinite")
    return result


def _axis_limits(values: Sequence[float], name: str) -> tuple[float, float]:
    if len(values) != 2:
        raise GraphDataError(f"{name} must contain minimum and maximum")
    minimum, maximum = (float(values[0]), float(values[1]))
    if not math.isfinite(minimum) or not math.isfinite(maximum) or maximum <= minimum:
        raise GraphDataError(f"{name} maximum must be finite and exceed minimum")
    return minimum, maximum


def _default_timestamp_token() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _safe_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip()).strip("_.")
    if not cleaned:
        raise GraphDataError("filename token must contain a safe character")
    return cleaned


@dataclass(frozen=True, slots=True)
class SpatialGraphSnapshot:
    """The exact nine values currently shown by a heatmap/profile view."""

    values: tuple[float, ...] | Sequence[float]
    metric: str | GraphMetric = "mean_delta_v"
    scale_min: float = 0.0
    scale_max: float = 255.0
    context: GraphContext = field(default_factory=GraphContext)
    frame_id: int | None = None
    host_monotonic_ns: int | None = None
    elapsed_time_s: float | None = None
    title: str = "Spatial intensity"
    timestamp_token: str = field(default_factory=_default_timestamp_token)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _values9(self.values))
        object.__setattr__(self, "metric", resolve_metric(self.metric))
        limits = _axis_limits((self.scale_min, self.scale_max), "spatial scale")
        object.__setattr__(self, "scale_min", limits[0])
        object.__setattr__(self, "scale_max", limits[1])
        if self.frame_id is not None and self.frame_id < 0:
            raise GraphDataError("frame_id must be nonnegative when available")
        if self.host_monotonic_ns is not None and self.host_monotonic_ns < 0:
            raise GraphDataError("host_monotonic_ns must be nonnegative")
        if self.elapsed_time_s is not None and (
            not math.isfinite(float(self.elapsed_time_s))
            or float(self.elapsed_time_s) < 0.0
        ):
            raise GraphDataError("elapsed_time_s must be finite and nonnegative")
        object.__setattr__(self, "timestamp_token", _safe_token(self.timestamp_token))


@dataclass(frozen=True, slots=True)
class TemporalGraphSnapshot:
    """Exact currently displayed temporal rows in authoritative time order."""

    capture_frame_ids: tuple[int, ...] | Sequence[int]
    elapsed_time_s: tuple[float, ...] | Sequence[float]
    roi_values: tuple[tuple[float, ...], ...] | Sequence[Sequence[float]]
    metric: str | GraphMetric = "mean_delta_v"
    visible_roi_ids: tuple[int, ...] | Sequence[int] = ROI_ORDER
    x_axis_limits: tuple[float, float] | Sequence[float] | None = None
    y_axis_limits: tuple[float, float] | Sequence[float] = (0.0, 255.0)
    display_history_s: float = 30.0
    context: GraphContext = field(default_factory=GraphContext)
    title: str = "Temporal ROI optical intensity"
    timestamp_token: str = field(default_factory=_default_timestamp_token)
    peak_frame_id: int | None = None

    def __post_init__(self) -> None:
        frame_ids = tuple(int(value) for value in self.capture_frame_ids)
        elapsed = tuple(float(value) for value in self.elapsed_time_s)
        rows = tuple(_values9(row) for row in self.roi_values)
        if not frame_ids:
            raise GraphDataError("temporal graph requires at least one frame")
        if len(frame_ids) != len(elapsed) or len(frame_ids) != len(rows):
            raise GraphDataError("temporal IDs, timestamps, and ROI rows must align")
        if any(value < 0 for value in frame_ids):
            raise GraphDataError("capture_frame_ids must be nonnegative")
        if any(
            not math.isfinite(value) or value < 0.0
            for value in elapsed
        ):
            raise GraphDataError("elapsed timestamps must be finite and nonnegative")
        if any(current < previous for previous, current in zip(elapsed, elapsed[1:])):
            raise GraphDataError("elapsed timestamps must be nondecreasing")
        visible = tuple(int(value) for value in self.visible_roi_ids)
        if len(set(visible)) != len(visible) or any(value not in ROI_ORDER for value in visible):
            raise GraphDataError("visible_roi_ids must be unique ROI numbers 1..9")
        history = float(self.display_history_s)
        if not math.isfinite(history) or history <= 0.0:
            raise GraphDataError("display_history_s must be finite and positive")
        x_limits = (
            _axis_limits(self.x_axis_limits, "x_axis_limits")
            if self.x_axis_limits is not None
            else _nondegenerate_range(elapsed[0], elapsed[-1])
        )
        y_limits = _axis_limits(self.y_axis_limits, "y_axis_limits")
        if self.peak_frame_id is not None and self.peak_frame_id not in frame_ids:
            raise GraphDataError("peak_frame_id must occur in capture_frame_ids")
        object.__setattr__(self, "capture_frame_ids", frame_ids)
        object.__setattr__(self, "elapsed_time_s", elapsed)
        object.__setattr__(self, "roi_values", rows)
        object.__setattr__(self, "visible_roi_ids", visible)
        object.__setattr__(self, "metric", resolve_metric(self.metric))
        object.__setattr__(self, "x_axis_limits", x_limits)
        object.__setattr__(self, "y_axis_limits", y_limits)
        object.__setattr__(self, "display_history_s", history)
        object.__setattr__(self, "timestamp_token", _safe_token(self.timestamp_token))


@dataclass(frozen=True, slots=True)
class GraphArtifactSet:
    kind: str
    png_path: Path | None
    csv_path: Path | None
    json_path: Path
    worker_thread_id: int
    frame_id: int | None = None
    available: bool = True


@dataclass(frozen=True, slots=True)
class TrialGraphSummary:
    artifacts: tuple[GraphArtifactSet, ...]
    peak_frame_id: int
    mean_contact_available: bool
    worker_thread_id: int

    def artifact(self, kind: str) -> GraphArtifactSet:
        for artifact in self.artifacts:
            if artifact.kind == kind:
                return artifact
        raise KeyError(kind)


def _nondegenerate_range(minimum: float, maximum: float) -> tuple[float, float]:
    if maximum > minimum:
        return float(minimum), float(maximum)
    padding = max(abs(float(minimum)) * 0.05, 0.5)
    return float(minimum) - padding, float(maximum) + padding


def _format_value(value: float) -> str:
    if math.isnan(value):
        return "N/A"
    magnitude = abs(value)
    if magnitude >= 100_000:
        return f"{value:.3e}"
    if magnitude >= 100:
        return f"{value:.1f}"
    if magnitude >= 10:
        return f"{value:.2f}"
    return f"{value:.3f}"


def _draw_centered_text(
    image: np.ndarray,
    text: str,
    center: tuple[int, int],
    *,
    scale: float,
    color: tuple[int, int, int],
    thickness: int = 1,
) -> None:
    size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    origin = (center[0] - size[0] // 2, center[1] + size[1] // 2)
    cv2.putText(
        image,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        thickness,
        cv2.LINE_AA,
    )


def _color_for_value(value: float, scale_min: float, scale_max: float) -> tuple[int, int, int]:
    if math.isnan(value):
        return 205, 205, 205
    normalized = int(
        round(255.0 * min(max((value - scale_min) / (scale_max - scale_min), 0.0), 1.0))
    )
    pixel = np.array([[[normalized]]], dtype=np.uint8)
    colored = cv2.applyColorMap(pixel, cv2.COLORMAP_VIRIDIS)[0, 0]
    return int(colored[0]), int(colored[1]), int(colored[2])


def _text_color(background_bgr: tuple[int, int, int]) -> tuple[int, int, int]:
    b, g, r = background_bgr
    luminance = 0.114 * b + 0.587 * g + 0.299 * r
    return (20, 20, 20) if luminance > 145 else (255, 255, 255)


def _render_heatmap(snapshot: SpatialGraphSnapshot) -> np.ndarray:
    image = np.full((HEATMAP_HEIGHT, HEATMAP_WIDTH, 3), 255, dtype=np.uint8)
    metric = resolve_metric(snapshot.metric)
    _draw_centered_text(
        image,
        snapshot.title,
        (HEATMAP_WIDTH // 2, 48),
        scale=1.05,
        color=(25, 25, 25),
        thickness=2,
    )
    _draw_centered_text(
        image,
        f"Metric: {metric.description} [{metric.units}]",
        (HEATMAP_WIDTH // 2, 90),
        scale=0.58,
        color=(55, 55, 55),
    )
    grid_left, grid_top = 115, 135
    cell_width, cell_height = 250, 205
    for index, value in enumerate(snapshot.values):
        row, column = divmod(index, 3)
        left = grid_left + column * cell_width
        top = grid_top + row * cell_height
        right = left + cell_width
        bottom = top + cell_height
        cell_color = _color_for_value(value, snapshot.scale_min, snapshot.scale_max)
        cv2.rectangle(image, (left, top), (right, bottom), cell_color, -1)
        cv2.rectangle(image, (left, top), (right, bottom), (35, 35, 35), 2)
        foreground = _text_color(cell_color)
        _draw_centered_text(
            image,
            f"ROI {index + 1}",
            ((left + right) // 2, (top + bottom) // 2 - 28),
            scale=0.76,
            color=foreground,
            thickness=2,
        )
        _draw_centered_text(
            image,
            _format_value(value),
            ((left + right) // 2, (top + bottom) // 2 + 30),
            scale=0.76,
            color=foreground,
            thickness=2,
        )

    bar_left, bar_right = 930, 990
    bar_top, bar_bottom = grid_top, grid_top + 3 * cell_height
    for y in range(bar_top, bar_bottom):
        fraction = 1.0 - (y - bar_top) / max(bar_bottom - bar_top - 1, 1)
        value = snapshot.scale_min + fraction * (snapshot.scale_max - snapshot.scale_min)
        color = _color_for_value(value, snapshot.scale_min, snapshot.scale_max)
        cv2.line(image, (bar_left, y), (bar_right, y), color, 1)
    cv2.rectangle(image, (bar_left, bar_top), (bar_right, bar_bottom), (30, 30, 30), 2)
    cv2.putText(
        image,
        _format_value(snapshot.scale_max),
        (bar_right + 15, bar_top + 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (30, 30, 30),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        _format_value(snapshot.scale_min),
        (bar_right + 15, bar_bottom),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (30, 30, 30),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        f"Scale: {metric.units}",
        (bar_left - 5, bar_bottom + 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (30, 30, 30),
        1,
        cv2.LINE_AA,
    )
    footer = (
        f"Session: {snapshot.context.session_id or 'N/A'}   "
        f"Trial: {snapshot.context.trial_id or 'N/A'}   "
        f"Frame: {snapshot.frame_id if snapshot.frame_id is not None else 'N/A'}   "
        f"Elapsed: {_format_value(float(snapshot.elapsed_time_s)) + ' s' if snapshot.elapsed_time_s is not None else 'N/A'}"
    )
    _draw_centered_text(
        image,
        footer,
        (HEATMAP_WIDTH // 2, 835),
        scale=0.5,
        color=(45, 45, 45),
    )
    return image


_ROI_COLORS: tuple[tuple[int, int, int], ...] = (
    (31, 119, 180),
    (255, 127, 14),
    (44, 160, 44),
    (214, 39, 40),
    (148, 103, 189),
    (140, 86, 75),
    (227, 119, 194),
    (127, 127, 127),
    (188, 189, 34),
)


def _plot_point(
    x: float,
    y: float,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
    rectangle: tuple[int, int, int, int],
) -> tuple[int, int]:
    left, top, right, bottom = rectangle
    x_fraction = (x - x_limits[0]) / (x_limits[1] - x_limits[0])
    y_fraction = (y - y_limits[0]) / (y_limits[1] - y_limits[0])
    return (
        int(round(left + x_fraction * (right - left))),
        int(round(bottom - y_fraction * (bottom - top))),
    )


def _draw_axes(
    image: np.ndarray,
    rectangle: tuple[int, int, int, int],
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
    *,
    x_label: str,
    y_label: str,
    x_ticks: Sequence[tuple[float, str]] | None = None,
) -> None:
    left, top, right, bottom = rectangle
    for index in range(6):
        fraction = index / 5
        y = int(round(bottom - fraction * (bottom - top)))
        cv2.line(image, (left, y), (right, y), (220, 220, 220), 1)
        y_value = y_limits[0] + fraction * (y_limits[1] - y_limits[0])
        cv2.putText(
            image,
            _format_value(y_value),
            (20, y + 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (50, 50, 50),
            1,
            cv2.LINE_AA,
        )
    resolved_x_ticks = (
        tuple(x_ticks)
        if x_ticks is not None
        else tuple(
            (
                x_limits[0] + index / 5 * (x_limits[1] - x_limits[0]),
                _format_value(
                    x_limits[0] + index / 5 * (x_limits[1] - x_limits[0])
                ),
            )
            for index in range(6)
        )
    )
    for x_value, label in resolved_x_ticks:
        if x_value < x_limits[0] or x_value > x_limits[1]:
            raise GraphDataError("x-axis tick lies outside x_axis_limits")
        fraction = (x_value - x_limits[0]) / (x_limits[1] - x_limits[0])
        x = int(round(left + fraction * (right - left)))
        cv2.line(image, (x, top), (x, bottom), (220, 220, 220), 1)
        _draw_centered_text(
            image,
            str(label),
            (x, bottom + 25),
            scale=0.42,
            color=(50, 50, 50),
        )
    cv2.rectangle(image, (left, top), (right, bottom), (30, 30, 30), 2)
    _draw_centered_text(
        image, x_label, ((left + right) // 2, bottom + 70), scale=0.62, color=(25, 25, 25), thickness=2
    )
    cv2.putText(
        image,
        f"Y: {y_label}",
        (left, top - 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (25, 25, 25),
        1,
        cv2.LINE_AA,
    )


def _downsample_indices(
    count: int,
    maximum_points: int,
    *,
    preserve_indices: Iterable[int] = (),
) -> np.ndarray:
    if count <= maximum_points:
        return np.arange(count, dtype=np.int64)
    base = np.linspace(0, count - 1, maximum_points, dtype=np.int64)
    preserved = np.fromiter(preserve_indices, dtype=np.int64)
    return np.unique(np.concatenate((base, preserved, np.array([0, count - 1]))))


def _render_temporal(
    snapshot: TemporalGraphSnapshot,
    *,
    maximum_plot_points: int,
) -> tuple[np.ndarray, dict[str, object]]:
    image = np.full((LINE_GRAPH_HEIGHT, LINE_GRAPH_WIDTH, 3), 255, dtype=np.uint8)
    metric = resolve_metric(snapshot.metric)
    plot_rectangle = (105, 140, 1245, 720)
    _draw_centered_text(
        image, snapshot.title, (LINE_GRAPH_WIDTH // 2, 48), scale=1.0, color=(25, 25, 25), thickness=2
    )
    _draw_centered_text(
        image,
        f"Optical intensity only - {metric.description} [{metric.units}]",
        (LINE_GRAPH_WIDTH // 2, 88),
        scale=0.55,
        color=(55, 55, 55),
    )
    _draw_axes(
        image,
        plot_rectangle,
        snapshot.x_axis_limits,
        snapshot.y_axis_limits,
        x_label="Elapsed recording time (s)",
        y_label=f"{metric.description} [{metric.units}]",
    )
    peak_indices: list[int] = []
    if snapshot.peak_frame_id is not None:
        peak_indices.append(snapshot.capture_frame_ids.index(snapshot.peak_frame_id))
    indices = _downsample_indices(
        len(snapshot.capture_frame_ids), maximum_plot_points, preserve_indices=peak_indices
    )
    times = np.asarray(snapshot.elapsed_time_s, dtype=np.float64)[indices]
    values = np.asarray(snapshot.roi_values, dtype=np.float64)[indices, :]
    for roi_id in snapshot.visible_roi_ids:
        color = _ROI_COLORS[roi_id - 1]
        previous: tuple[int, int] | None = None
        for time_value, intensity in zip(times, values[:, roi_id - 1], strict=True):
            if not math.isfinite(float(intensity)):
                previous = None
                continue
            point = _plot_point(
                float(time_value),
                float(intensity),
                snapshot.x_axis_limits,
                snapshot.y_axis_limits,
                plot_rectangle,
            )
            if previous is not None:
                cv2.line(image, previous, point, color, 2, cv2.LINE_AA)
            previous = point
        legend_y = 160 + (roi_id - 1) * 48
        cv2.line(image, (1300, legend_y), (1350, legend_y), color, 3, cv2.LINE_AA)
        cv2.putText(
            image,
            f"ROI {roi_id}",
            (1365, legend_y + 7),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (30, 30, 30),
            1,
            cv2.LINE_AA,
        )
    if snapshot.peak_frame_id is not None:
        peak_index = snapshot.capture_frame_ids.index(snapshot.peak_frame_id)
        peak_time = snapshot.elapsed_time_s[peak_index]
        x, _ = _plot_point(
            peak_time,
            snapshot.y_axis_limits[0],
            snapshot.x_axis_limits,
            snapshot.y_axis_limits,
            plot_rectangle,
        )
        cv2.line(
            image,
            (x, plot_rectangle[1]),
            (x, plot_rectangle[3]),
            (20, 20, 20),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            image,
            f"Peak frame {snapshot.peak_frame_id}",
            (min(x + 8, plot_rectangle[2] - 170), plot_rectangle[1] + 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (20, 20, 20),
            1,
            cv2.LINE_AA,
        )
    footer = (
        f"Session: {snapshot.context.session_id or 'N/A'}   "
        f"Trial: {snapshot.context.trial_id or 'N/A'}   "
        f"Displayed range: {snapshot.elapsed_time_s[0]:.3f} to {snapshot.elapsed_time_s[-1]:.3f} s"
    )
    _draw_centered_text(
        image, footer, (LINE_GRAPH_WIDTH // 2, 855), scale=0.5, color=(45, 45, 45)
    )
    method = "none" if len(indices) == len(snapshot.capture_frame_ids) else "uniform_index"
    return image, {
        "visual_downsampling_method": method,
        "full_resolution_point_count": len(snapshot.capture_frame_ids),
        "png_point_count": len(indices),
        "peak_index_forced_into_png": bool(peak_indices and peak_indices[0] in indices),
    }


def _render_spatial_profile(snapshot: SpatialGraphSnapshot) -> np.ndarray:
    image = np.full((LINE_GRAPH_HEIGHT, LINE_GRAPH_WIDTH, 3), 255, dtype=np.uint8)
    metric = resolve_metric(snapshot.metric)
    rectangle = (125, 145, 1470, 710)
    x_limits = (1.0, 9.0)
    y_limits = (snapshot.scale_min, snapshot.scale_max)
    _draw_centered_text(
        image, snapshot.title, (LINE_GRAPH_WIDTH // 2, 48), scale=1.0, color=(25, 25, 25), thickness=2
    )
    _draw_centered_text(
        image,
        f"Current-frame spatial profile - {metric.description} [{metric.units}]",
        (LINE_GRAPH_WIDTH // 2, 90),
        scale=0.55,
        color=(55, 55, 55),
    )
    _draw_axes(
        image,
        rectangle,
        x_limits,
        y_limits,
        x_label="ROI number (1-9)",
        y_label=f"{metric.description} [{metric.units}]",
        x_ticks=tuple((float(roi_id), str(roi_id)) for roi_id in ROI_ORDER),
    )
    # A single explicit legend distinguishes the plotted profile from the
    # numeric point annotations without duplicating the ROI tick labels.
    cv2.rectangle(image, (1035, 108), (1465, 138), (255, 255, 255), -1)
    cv2.rectangle(image, (1035, 108), (1465, 138), (95, 95, 95), 1)
    cv2.line(image, (1055, 123), (1100, 123), (180, 70, 30), 3, cv2.LINE_AA)
    cv2.circle(image, (1078, 123), 5, (180, 70, 30), -1, cv2.LINE_AA)
    cv2.putText(
        image,
        "Legend: measured ROI optical value",
        (1115, 130),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (30, 30, 30),
        1,
        cv2.LINE_AA,
    )
    previous: tuple[int, int] | None = None
    for roi_id, value in enumerate(snapshot.values, start=1):
        x = int(round(rectangle[0] + (roi_id - 1) / 8 * (rectangle[2] - rectangle[0])))
        if not math.isfinite(value):
            previous = None
            continue
        point = _plot_point(float(roi_id), value, x_limits, y_limits, rectangle)
        if previous is not None:
            cv2.line(image, previous, point, (180, 70, 30), 3, cv2.LINE_AA)
        cv2.circle(image, point, 7, (180, 70, 30), -1, cv2.LINE_AA)
        label_y = max(rectangle[1] + 20, point[1] - 16)
        _draw_centered_text(
            image, _format_value(value), (point[0], label_y), scale=0.42, color=(25, 25, 25)
        )
        previous = point
    footer = (
        f"Session: {snapshot.context.session_id or 'N/A'}   "
        f"Trial: {snapshot.context.trial_id or 'N/A'}   "
        f"Frame: {snapshot.frame_id if snapshot.frame_id is not None else 'N/A'}   "
        f"Elapsed: {_format_value(float(snapshot.elapsed_time_s)) + ' s' if snapshot.elapsed_time_s is not None else 'N/A'}"
    )
    _draw_centered_text(
        image, footer, (LINE_GRAPH_WIDTH // 2, 850), scale=0.5, color=(45, 45, 45)
    )
    return image


def _csv_number(value: float) -> str:
    return "" if not math.isfinite(value) else repr(float(value))


def _spatial_csv_rows(values: Sequence[float]) -> list[dict[str, object]]:
    return [
        {
            "roi": roi_id,
            "row": (roi_id - 1) // 3 + 1,
            "column": (roi_id - 1) % 3 + 1,
            "value": _csv_number(float(values[roi_id - 1])),
        }
        for roi_id in ROI_ORDER
    ]


def _temporal_csv_rows(snapshot: TemporalGraphSnapshot) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for frame_id, elapsed, values in zip(
        snapshot.capture_frame_ids,
        snapshot.elapsed_time_s,
        snapshot.roi_values,
        strict=True,
    ):
        row: dict[str, object] = {
            "capture_frame_id": frame_id,
            "elapsed_time_s": repr(float(elapsed)),
        }
        row.update(
            {
                f"roi{roi_id}_value": _csv_number(values[roi_id - 1])
                for roi_id in ROI_ORDER
            }
        )
        rows.append(row)
    return rows


def _context_json(context: GraphContext) -> dict[str, str]:
    return {
        "session_id": context.session_id,
        "trial_id": context.trial_id,
        "baseline_id": context.baseline_id,
        "roi_layout_id": context.roi_layout_id,
    }


def _spatial_json(
    snapshot: SpatialGraphSnapshot,
    *,
    graph_kind: str,
) -> dict[str, object]:
    metric = resolve_metric(snapshot.metric)
    return {
        "available": True,
        "graph_kind": graph_kind,
        "title": snapshot.title,
        "selected_metric": metric.code,
        "metric_description": metric.description,
        "metric_units": metric.units,
        "frame_id": snapshot.frame_id,
        "host_monotonic_ns": snapshot.host_monotonic_ns,
        "elapsed_time_s": snapshot.elapsed_time_s,
        "color_scale_min": snapshot.scale_min,
        "color_scale_max": snapshot.scale_max,
        "y_axis_min": snapshot.scale_min,
        "y_axis_max": snapshot.scale_max,
        "roi_order": list(ROI_ORDER),
        "roi_labels": [f"ROI {roi_id}" for roi_id in ROI_ORDER],
        "plotted_values": list(snapshot.values),
        "csv_columns": list(SPATIAL_CSV_COLUMNS),
        "render_elements": [
            "title",
            "metric",
            "ROI 1-9 labels",
            "numeric values",
            "fixed scale",
            "color bar" if graph_kind.endswith("heatmap") else "axes",
            *(
                ["spatial-profile legend"]
                if graph_kind.endswith("profile")
                else []
            ),
            "session and trial provenance",
        ],
        **_context_json(snapshot.context),
    }


def _temporal_json(
    snapshot: TemporalGraphSnapshot,
    *,
    downsampling: Mapping[str, object],
    graph_kind: str,
) -> dict[str, object]:
    metric = resolve_metric(snapshot.metric)
    return {
        "available": True,
        "graph_kind": graph_kind,
        "title": snapshot.title,
        "selected_metric": metric.code,
        "metric_description": metric.description,
        "metric_units": metric.units,
        "start_elapsed_time_s": snapshot.elapsed_time_s[0],
        "end_elapsed_time_s": snapshot.elapsed_time_s[-1],
        "included_frame_ids": list(snapshot.capture_frame_ids),
        "visible_roi_lines": list(snapshot.visible_roi_ids),
        "roi_order": list(ROI_ORDER),
        "x_axis_limits": list(snapshot.x_axis_limits),
        "y_axis_limits": list(snapshot.y_axis_limits),
        "display_history_s": snapshot.display_history_s,
        "peak_frame_id": snapshot.peak_frame_id,
        "csv_columns": list(TEMPORAL_CSV_COLUMNS),
        "render_elements": [
            "title",
            "labeled x-axis",
            "labeled y-axis",
            "ROI legend",
            "grid lines",
            "session and trial provenance",
        ],
        **dict(downsampling),
        **_context_json(snapshot.context),
    }


def _ensure_available_paths(paths: Sequence[Path]) -> None:
    existing = [path for path in paths if path.exists()]
    if existing:
        raise ExistingGraphError(
            "refusing to overwrite graph artifact(s): "
            + ", ".join(str(path) for path in existing)
        )


def _publish_no_overwrite(temporary: Path, destination: Path) -> None:
    try:
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
    except FileExistsError as exc:
        raise ExistingGraphError(
            f"refusing to overwrite graph artifact: {destination}"
        ) from exc


def _temporary_path(directory: Path, suffix: str) -> Path:
    return directory / f".graph-{uuid.uuid4().hex}{suffix}"


def _write_graph_bundle(
    directory: Path,
    basename: str,
    image: np.ndarray,
    *,
    csv_columns: Sequence[str],
    csv_rows: Sequence[Mapping[str, object]],
    json_payload: Mapping[str, object],
    graph_kind: str,
    frame_id: int | None,
) -> GraphArtifactSet:
    directory.mkdir(parents=True, exist_ok=True)
    png_path = directory / f"{basename}.png"
    csv_path = directory / f"{basename}.csv"
    json_path = directory / f"{basename}.json"
    final_paths = (png_path, csv_path, json_path)
    _ensure_available_paths(final_paths)
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise GraphExportError("renderer did not produce a uint8 BGR image")

    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise GraphExportError("OpenCV failed to encode graph PNG")
    png_temporary = _temporary_path(directory, ".png.tmp")
    csv_temporary = _temporary_path(directory, ".csv.tmp")
    json_temporary = _temporary_path(directory, ".json.tmp")
    temporaries = (png_temporary, csv_temporary, json_temporary)
    try:
        with png_temporary.open("xb") as handle:
            handle.write(encoded.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        with csv_temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(csv_columns), extrasaction="raise")
            writer.writeheader()
            writer.writerows(csv_rows)
            handle.flush()
            os.fsync(handle.fileno())
        with json_temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(
                strict_json_dumps(
                    dict(json_payload), include_nonfinite_status=True, sort_keys=True
                )
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _publish_no_overwrite(png_temporary, png_path)
        _publish_no_overwrite(csv_temporary, csv_path)
        _publish_no_overwrite(json_temporary, json_path)
    finally:
        for temporary in temporaries:
            if temporary.exists():
                temporary.unlink()
    return GraphArtifactSet(
        kind=graph_kind,
        png_path=png_path,
        csv_path=csv_path,
        json_path=json_path,
        worker_thread_id=threading.get_ident(),
        frame_id=frame_id,
        available=True,
    )


def _write_unavailable_status(
    directory: Path,
    basename: str,
    *,
    graph_kind: str,
    reason: str,
    context: GraphContext,
) -> GraphArtifactSet:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{basename}.json"
    _ensure_available_paths((json_path,))
    temporary = _temporary_path(directory, ".json.tmp")
    payload = {
        "available": False,
        "graph_kind": graph_kind,
        "reason": reason,
        "roi_order": list(ROI_ORDER),
        **_context_json(context),
    }
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(strict_json_dumps(payload, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _publish_no_overwrite(temporary, json_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return GraphArtifactSet(
        kind=graph_kind,
        png_path=None,
        csv_path=None,
        json_path=json_path,
        worker_thread_id=threading.get_ident(),
        available=False,
    )


def _manual_heatmap(directory: Path, snapshot: SpatialGraphSnapshot) -> GraphArtifactSet:
    metric = resolve_metric(snapshot.metric)
    frame_token = str(snapshot.frame_id) if snapshot.frame_id is not None else "preview"
    basename = (
        f"snapshot_{snapshot.timestamp_token}_{frame_token}_{_safe_token(metric.code)}"
    )
    return _write_graph_bundle(
        directory,
        basename,
        _render_heatmap(snapshot),
        csv_columns=SPATIAL_CSV_COLUMNS,
        csv_rows=_spatial_csv_rows(snapshot.values),
        json_payload=_spatial_json(snapshot, graph_kind="manual_spatial_heatmap"),
        graph_kind="manual_spatial_heatmap",
        frame_id=snapshot.frame_id,
    )


def _manual_profile(directory: Path, snapshot: SpatialGraphSnapshot) -> GraphArtifactSet:
    metric = resolve_metric(snapshot.metric)
    frame_token = str(snapshot.frame_id) if snapshot.frame_id is not None else "preview"
    basename = (
        f"spatial_profile_{snapshot.timestamp_token}_{frame_token}_{_safe_token(metric.code)}"
    )
    return _write_graph_bundle(
        directory,
        basename,
        _render_spatial_profile(snapshot),
        csv_columns=SPATIAL_CSV_COLUMNS,
        csv_rows=_spatial_csv_rows(snapshot.values),
        json_payload=_spatial_json(snapshot, graph_kind="manual_spatial_profile"),
        graph_kind="manual_spatial_profile",
        frame_id=snapshot.frame_id,
    )


def _manual_temporal(
    directory: Path,
    snapshot: TemporalGraphSnapshot,
    *,
    maximum_plot_points: int,
) -> GraphArtifactSet:
    metric = resolve_metric(snapshot.metric)
    basename = f"temporal_lines_{snapshot.timestamp_token}_{_safe_token(metric.code)}"
    image, downsampling = _render_temporal(
        snapshot, maximum_plot_points=maximum_plot_points
    )
    return _write_graph_bundle(
        directory,
        basename,
        image,
        csv_columns=TEMPORAL_CSV_COLUMNS,
        csv_rows=_temporal_csv_rows(snapshot),
        json_payload=_temporal_json(
            snapshot,
            downsampling=downsampling,
            graph_kind="manual_temporal_lines",
        ),
        graph_kind="manual_temporal_lines",
        frame_id=None,
    )


def _row_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        return math.isfinite(numeric) and numeric != 0.0
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no", "", "nan", "none"}:
        return False
    raise GraphDataError(f"cannot interpret boolean graph field value {value!r}")


def _row_float(row: Mapping[str, object], column: str) -> float:
    value = row.get(column, math.nan)
    if value is None or isinstance(value, str) and not value.strip():
        return math.nan
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise GraphDataError(f"column {column} must be numeric") from exc
    return result


def _row_int(row: Mapping[str, object], column: str) -> int:
    raw = row.get(column)
    if isinstance(raw, bool) or raw is None:
        raise GraphDataError(f"column {column} must be a nonnegative integer")
    if isinstance(raw, (int, np.integer)):
        value = int(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not re.fullmatch(r"[+]?[0-9]+", stripped):
            raise GraphDataError(f"column {column} must be a nonnegative integer")
        value = int(stripped)
    else:
        try:
            numeric = float(raw)
        except (TypeError, ValueError) as exc:
            raise GraphDataError(
                f"column {column} must be a nonnegative integer"
            ) from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise GraphDataError(f"column {column} must be a nonnegative integer")
        value = int(numeric)
    if value < 0:
        raise GraphDataError(f"column {column} must be a nonnegative integer")
    return value


def _roi_values_from_row(
    row: Mapping[str, object], metric: GraphMetric
) -> tuple[float, ...]:
    return _values9(
        [_row_float(row, f"roi{roi_id}_{metric.column_suffix}") for roi_id in ROI_ORDER]
    )


def _valid_optical_row(row: Mapping[str, object], values: Sequence[float]) -> bool:
    return (
        _row_bool(row.get("frame_valid"), default=True)
        and _row_bool(row.get("baseline_valid"), default=True)
        and all(math.isfinite(value) for value in values)
    )


def _automatic_scale_max(values: Iterable[float]) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    maximum = max(finite, default=0.0)
    return maximum if maximum > 0.0 else 1.0


def _context_with_row_defaults(
    context: GraphContext, rows: Sequence[Mapping[str, object]]
) -> GraphContext:
    first = rows[0] if rows else {}
    return GraphContext(
        session_id=context.session_id or str(first.get("session_id", "")),
        trial_id=context.trial_id or str(first.get("trial_id", "")),
        baseline_id=context.baseline_id or str(first.get("baseline_id", "")),
        roi_layout_id=context.roi_layout_id or str(first.get("roi_layout_id", "")),
    )


def _spatial_from_row(
    row: Mapping[str, object],
    values: Sequence[float],
    *,
    metric: GraphMetric,
    scale_max: float,
    context: GraphContext,
    title: str,
) -> SpatialGraphSnapshot:
    raw_host = row.get("host_monotonic_ns")
    host_value = None if raw_host in (None, "") else _row_int(row, "host_monotonic_ns")
    return SpatialGraphSnapshot(
        values=values,
        metric=metric,
        scale_min=0.0,
        scale_max=scale_max,
        context=context,
        frame_id=_row_int(row, "capture_frame_id"),
        host_monotonic_ns=host_value,
        elapsed_time_s=_row_float(row, "elapsed_time_s"),
        title=title,
        timestamp_token="automatic",
    )


def _write_automatic_heatmap(
    directory: Path,
    basename: str,
    snapshot: SpatialGraphSnapshot,
    *,
    graph_kind: str,
) -> GraphArtifactSet:
    return _write_graph_bundle(
        directory,
        basename,
        _render_heatmap(snapshot),
        csv_columns=SPATIAL_CSV_COLUMNS,
        csv_rows=_spatial_csv_rows(snapshot.values),
        json_payload={
            **_spatial_json(snapshot, graph_kind=graph_kind),
            "automatic_summary": True,
            "automatic_scale_rule": "zero_to_observed_maximum_or_one_when_all_zero",
        },
        graph_kind=graph_kind,
        frame_id=snapshot.frame_id,
    )


def _write_automatic_profile(
    directory: Path,
    basename: str,
    snapshot: SpatialGraphSnapshot,
    *,
    graph_kind: str,
) -> GraphArtifactSet:
    return _write_graph_bundle(
        directory,
        basename,
        _render_spatial_profile(snapshot),
        csv_columns=SPATIAL_CSV_COLUMNS,
        csv_rows=_spatial_csv_rows(snapshot.values),
        json_payload={
            **_spatial_json(snapshot, graph_kind=graph_kind),
            "automatic_summary": True,
        },
        graph_kind=graph_kind,
        frame_id=snapshot.frame_id,
    )


def _finalize_trial_graphs(
    directory: Path,
    rows: Sequence[Mapping[str, object]],
    context: GraphContext,
    *,
    maximum_plot_points: int,
) -> TrialGraphSummary:
    # Copy mutable recorder/GUI mappings on the worker, not on the caller thread.
    rows = tuple(dict(row) for row in rows)
    if not rows:
        raise GraphDataError("trial graph finalization requires at least one frame row")
    context = _context_with_row_defaults(context, rows)
    frame_ids = [_row_int(row, "capture_frame_id") for row in rows]
    if any(current <= previous for previous, current in zip(frame_ids, frame_ids[1:])):
        raise GraphDataError("trial rows must have strictly increasing capture_frame_id")
    elapsed = [_row_float(row, "elapsed_time_s") for row in rows]
    if any(not math.isfinite(value) or value < 0.0 for value in elapsed):
        raise GraphDataError("all trial rows require authoritative elapsed_time_s")
    if any(current < previous for previous, current in zip(elapsed, elapsed[1:])):
        raise GraphDataError("trial elapsed timestamps must be nondecreasing")

    mean_metric = METRICS["mean_delta_v"]
    integrated_metric = METRICS["integrated_delta_v"]
    mean_values = [_roi_values_from_row(row, mean_metric) for row in rows]
    integrated_values = [_roi_values_from_row(row, integrated_metric) for row in rows]
    peak_candidates = [
        (sum(values), index)
        for index, (row, values) in enumerate(zip(rows, mean_values, strict=True))
        if _valid_optical_row(row, values)
    ]
    if not peak_candidates:
        raise GraphDataError("no frame has nine valid mean-delta-V values")
    _, peak_index = max(peak_candidates, key=lambda item: (item[0], -item[1]))
    peak_row = rows[peak_index]
    peak_values = mean_values[peak_index]
    peak_frame_id = frame_ids[peak_index]
    peak_scale_max = _automatic_scale_max(peak_values)
    peak_snapshot = _spatial_from_row(
        peak_row,
        peak_values,
        metric=mean_metric,
        scale_max=peak_scale_max,
        context=context,
        title="Trial peak mean positive delta V",
    )

    artifacts: list[GraphArtifactSet] = []
    artifacts.append(
        _write_automatic_heatmap(
            directory,
            "trial_peak_mean_delta_v",
            peak_snapshot,
            graph_kind="trial_peak_heatmap",
        )
    )

    contact_values = [
        values
        for row, values in zip(rows, mean_values, strict=True)
        if _row_bool(row.get("contact_state_derived"), default=False)
        and _valid_optical_row(row, values)
    ]
    if contact_values:
        mean_contact_values = tuple(
            float(value)
            for value in np.mean(np.asarray(contact_values, dtype=np.float64), axis=0)
        )
        mean_contact_snapshot = SpatialGraphSnapshot(
            values=mean_contact_values,
            metric=mean_metric,
            scale_min=0.0,
            scale_max=_automatic_scale_max(mean_contact_values),
            context=context,
            title="Trial mean contact mean positive delta V",
            timestamp_token="automatic",
        )
        artifacts.append(
            _write_automatic_heatmap(
                directory,
                "trial_mean_contact_mean_delta_v",
                mean_contact_snapshot,
                graph_kind="trial_mean_contact_heatmap",
            )
        )
        mean_contact_available = True
    else:
        artifacts.append(
            _write_unavailable_status(
                directory,
                "trial_mean_contact_mean_delta_v",
                graph_kind="trial_mean_contact_heatmap",
                reason="No valid frames had contact_state_derived equal to true.",
                context=context,
            )
        )
        mean_contact_available = False

    valid_integrated = [
        values
        for row, values in zip(rows, integrated_values, strict=True)
        if _valid_optical_row(row, values)
    ]
    if not valid_integrated:
        raise GraphDataError("no frame has nine valid integrated-delta-V values")
    integrated_summary = tuple(
        float(value)
        for value in np.sum(np.asarray(valid_integrated, dtype=np.float64), axis=0)
    )
    integrated_snapshot = SpatialGraphSnapshot(
        values=integrated_summary,
        metric=integrated_metric,
        scale_min=0.0,
        scale_max=_automatic_scale_max(integrated_summary),
        context=context,
        title="Trial integrated positive delta V",
        timestamp_token="automatic",
    )
    artifacts.append(
        _write_automatic_heatmap(
            directory,
            "trial_integrated_delta_v",
            integrated_snapshot,
            graph_kind="trial_integrated_heatmap",
        )
    )

    temporal_rows = [
        (frame_id, time_value, values)
        for row, frame_id, time_value, values in zip(
            rows, frame_ids, elapsed, mean_values, strict=True
        )
        if _row_bool(row.get("frame_valid"), default=True)
    ]
    if not temporal_rows:
        raise GraphDataError("no frame-valid rows are available for temporal export")
    temporal_values_flat = [
        value for _, _, values in temporal_rows for value in values if math.isfinite(value)
    ]
    temporal_snapshot = TemporalGraphSnapshot(
        capture_frame_ids=tuple(item[0] for item in temporal_rows),
        elapsed_time_s=tuple(item[1] for item in temporal_rows),
        roi_values=tuple(item[2] for item in temporal_rows),
        metric=mean_metric,
        visible_roi_ids=ROI_ORDER,
        x_axis_limits=_nondegenerate_range(
            temporal_rows[0][1], temporal_rows[-1][1]
        ),
        y_axis_limits=(0.0, _automatic_scale_max(temporal_values_flat)),
        display_history_s=max(temporal_rows[-1][1] - temporal_rows[0][1], 1.0),
        context=context,
        title="Complete-trial ROI optical intensity over time",
        timestamp_token="automatic",
        peak_frame_id=peak_frame_id,
    )
    temporal_image, downsampling = _render_temporal(
        temporal_snapshot, maximum_plot_points=maximum_plot_points
    )
    artifacts.append(
        _write_graph_bundle(
            directory,
            "trial_roi_intensity_over_time",
            temporal_image,
            csv_columns=TEMPORAL_CSV_COLUMNS,
            csv_rows=_temporal_csv_rows(temporal_snapshot),
            json_payload={
                **_temporal_json(
                    temporal_snapshot,
                    downsampling=downsampling,
                    graph_kind="trial_temporal_lines",
                ),
                "automatic_summary": True,
                "force_plot": False,
                "optical_intensity_plot": True,
            },
            graph_kind="trial_temporal_lines",
            frame_id=None,
        )
    )

    peak_profile = SpatialGraphSnapshot(
        values=peak_snapshot.values,
        metric=mean_metric,
        scale_min=peak_snapshot.scale_min,
        scale_max=peak_snapshot.scale_max,
        context=context,
        frame_id=peak_snapshot.frame_id,
        host_monotonic_ns=peak_snapshot.host_monotonic_ns,
        elapsed_time_s=peak_snapshot.elapsed_time_s,
        title="Trial peak-frame spatial profile",
        timestamp_token="automatic",
    )
    artifacts.append(
        _write_automatic_profile(
            directory,
            "trial_peak_spatial_profile",
            peak_profile,
            graph_kind="trial_peak_spatial_profile",
        )
    )
    worker_thread_id = threading.get_ident()
    return TrialGraphSummary(
        artifacts=tuple(artifacts),
        peak_frame_id=peak_frame_id,
        mean_contact_available=mean_contact_available,
        worker_thread_id=worker_thread_id,
    )


class GraphExportService:
    """Submit all graph rendering and file I/O to one bounded worker."""

    def __init__(
        self,
        graph_directory: str | Path,
        *,
        executor: Executor | None = None,
        maximum_plot_points: int = 2_000,
        max_pending_exports: int = 8,
    ) -> None:
        if isinstance(maximum_plot_points, bool) or int(maximum_plot_points) < 2:
            raise ValueError("maximum_plot_points must be an integer of at least two")
        if isinstance(max_pending_exports, bool) or int(max_pending_exports) < 1:
            raise ValueError("max_pending_exports must be a positive integer")
        self.graph_directory = Path(graph_directory)
        self.maximum_plot_points = int(maximum_plot_points)
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="graph-export"
        )
        self._owns_executor = executor is None
        self._closed = False
        self._state_lock = threading.Lock()
        self._pending_slots = threading.BoundedSemaphore(int(max_pending_exports))

    def _submit(self, function: Any, *args: Any, **kwargs: Any) -> Future[Any]:
        with self._state_lock:
            if self._closed:
                raise GraphExportError("graph export service is closed")
            if not self._pending_slots.acquire(blocking=False):
                raise GraphExportError(
                    "graph export queue is full; wait for a pending export to finish"
                )
            try:
                future = self._executor.submit(function, *args, **kwargs)
            except Exception:
                self._pending_slots.release()
                raise
            future.add_done_callback(lambda _completed: self._pending_slots.release())
            return future

    def save_spatial_snapshot(
        self, snapshot: SpatialGraphSnapshot
    ) -> Future[GraphArtifactSet]:
        return self._submit(_manual_heatmap, self.graph_directory, snapshot)

    def save_temporal_lines(
        self, snapshot: TemporalGraphSnapshot
    ) -> Future[GraphArtifactSet]:
        return self._submit(
            _manual_temporal,
            self.graph_directory,
            snapshot,
            maximum_plot_points=self.maximum_plot_points,
        )

    def save_spatial_profile(
        self, snapshot: SpatialGraphSnapshot
    ) -> Future[GraphArtifactSet]:
        return self._submit(_manual_profile, self.graph_directory, snapshot)

    def finalize_trial(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        context: GraphContext | None = None,
    ) -> Future[TrialGraphSummary]:
        # Tuple creation is cheap; mapping copies and all analytical work occur
        # inside ``_finalize_trial_graphs`` on the export worker.
        row_references = tuple(rows)
        return self._submit(
            _finalize_trial_graphs,
            self.graph_directory,
            row_references,
            context or GraphContext(),
            maximum_plot_points=self.maximum_plot_points,
        )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=wait, cancel_futures=False)

    def __enter__(self) -> "GraphExportService":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.shutdown(wait=True)


__all__ = [
    "HEATMAP_HEIGHT",
    "HEATMAP_WIDTH",
    "LINE_GRAPH_HEIGHT",
    "LINE_GRAPH_WIDTH",
    "METRICS",
    "ROI_ORDER",
    "SPATIAL_CSV_COLUMNS",
    "TEMPORAL_CSV_COLUMNS",
    "ExistingGraphError",
    "GraphArtifactSet",
    "GraphContext",
    "GraphDataError",
    "GraphExportError",
    "GraphExportService",
    "GraphMetric",
    "SpatialGraphSnapshot",
    "TemporalGraphSnapshot",
    "TrialGraphSummary",
    "resolve_metric",
]
