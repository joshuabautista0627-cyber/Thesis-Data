"""Reproducibility and worker-boundary tests for graph export/finalization."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import threading

import cv2
import numpy as np
import pytest

import data.graph_exporter as graph_exporter
from data.graph_exporter import (
    HEATMAP_HEIGHT,
    HEATMAP_WIDTH,
    LINE_GRAPH_HEIGHT,
    LINE_GRAPH_WIDTH,
    ExistingGraphError,
    GraphContext,
    GraphDataError,
    GraphExportService,
    SpatialGraphSnapshot,
    TemporalGraphSnapshot,
)
from data.schemas import (
    GRAPH_SPATIAL_COLUMNS,
    GRAPH_TEMPORAL_COLUMNS,
    data_dictionary_rows,
)


CONTEXT = GraphContext(
    session_id="session-graph",
    trial_id="trial-007",
    baseline_id="baseline-3",
    roi_layout_id="layout-9",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


def _read_png(path: Path, *, width: int, height: int) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    assert image is not None
    assert image.shape[1] >= width
    assert image.shape[0] >= height
    assert np.any(image != 255)
    return image


def _spatial_values() -> tuple[float, ...]:
    return (0.0, 1.25, 2.5, 5.0, 10.0, 20.0, 40.0, 80.0, 100.0)


def _trial_rows(count: int = 4, *, contacts: bool = True) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    means = [
        tuple(float(value) for value in range(1, 10)),
        (2.0,) * 9,
        tuple(float(value) for value in range(9)),
        (10.0,) * 9,
    ]
    for frame_id in range(count):
        if frame_id < len(means):
            values = means[frame_id]
        else:
            # Preserve one clear peak at the middle of longer trials.
            level = 30.0 if frame_id == count // 2 else float((frame_id % 7) + 1)
            values = (level,) * 9
        row: dict[str, object] = {
            "session_id": CONTEXT.session_id,
            "trial_id": CONTEXT.trial_id,
            "baseline_id": CONTEXT.baseline_id,
            "roi_layout_id": CONTEXT.roi_layout_id,
            "capture_frame_id": frame_id,
            "host_monotonic_ns": 9_000_000_000_000_000_000 + frame_id * 10_000_000,
            "elapsed_time_s": frame_id * 0.125,
            "frame_valid": True,
            "baseline_valid": True,
            "contact_state_derived": contacts and frame_id in {1, 2},
        }
        for roi_id, value in enumerate(values, start=1):
            row[f"roi{roi_id}_delta_v_mean"] = value
            row[f"roi{roi_id}_delta_v_sum"] = value * 10.0 + roi_id
        rows.append(row)
    return rows


def test_manual_spatial_snapshot_preserves_exact_values_order_scale_and_metadata(
    tmp_path: Path,
) -> None:
    caller_thread = threading.get_ident()
    snapshot = SpatialGraphSnapshot(
        values=_spatial_values(),
        metric="mean_delta_v",
        scale_min=0.0,
        scale_max=100.0,
        context=CONTEXT,
        frame_id=42,
        host_monotonic_ns=9_123_456_789_012_345,
        elapsed_time_s=1.75,
        title="Current 3 by 3 spatial intensity",
        timestamp_token="20260803T010203Z",
    )
    with GraphExportService(tmp_path / "spatial_graphs") as service:
        artifact = service.save_spatial_snapshot(snapshot).result(timeout=10)
    assert artifact.worker_thread_id != caller_thread
    assert artifact.png_path is not None and artifact.csv_path is not None
    assert artifact.png_path.name == (
        "snapshot_20260803T010203Z_42_mean_delta_v.png"
    )
    image = _read_png(
        artifact.png_path, width=HEATMAP_WIDTH, height=HEATMAP_HEIGHT
    )
    assert len(np.unique(image.reshape(-1, 3), axis=0)) > 20

    rows = _read_csv(artifact.csv_path)
    assert [int(row["roi"]) for row in rows] == list(range(1, 10))
    assert [(int(row["row"]), int(row["column"])) for row in rows] == [
        (1, 1),
        (1, 2),
        (1, 3),
        (2, 1),
        (2, 2),
        (2, 3),
        (3, 1),
        (3, 2),
        (3, 3),
    ]
    assert [float(row["value"]) for row in rows] == list(_spatial_values())

    metadata = _read_json(artifact.json_path)
    assert metadata["plotted_values"] == list(_spatial_values())
    assert metadata["roi_order"] == list(range(1, 10))
    assert metadata["color_scale_min"] == 0.0
    assert metadata["color_scale_max"] == 100.0
    assert metadata["session_id"] == CONTEXT.session_id
    assert metadata["trial_id"] == CONTEXT.trial_id
    assert metadata["frame_id"] == 42
    assert metadata["host_monotonic_ns"] == 9_123_456_789_012_345
    assert metadata["elapsed_time_s"] == 1.75
    rendered = metadata["render_elements"]
    assert "ROI 1-9 labels" in rendered
    assert "numeric values" in rendered
    assert "color bar" in rendered


def test_spatial_profile_uses_exact_same_nine_values_and_scale_as_heatmap(
    tmp_path: Path,
) -> None:
    snapshot = SpatialGraphSnapshot(
        values=_spatial_values(),
        metric="maximum_delta_v",
        scale_min=0.0,
        scale_max=120.0,
        context=CONTEXT,
        frame_id=8,
        elapsed_time_s=0.8,
        timestamp_token="same-frame",
    )
    with GraphExportService(tmp_path / "graphs") as service:
        heatmap = service.save_spatial_snapshot(snapshot).result(timeout=10)
        profile = service.save_spatial_profile(snapshot).result(timeout=10)
    assert profile.png_path is not None and profile.csv_path is not None
    _read_png(
        profile.png_path, width=LINE_GRAPH_WIDTH, height=LINE_GRAPH_HEIGHT
    )
    assert _read_csv(heatmap.csv_path) == _read_csv(profile.csv_path)  # type: ignore[arg-type]
    heatmap_json = _read_json(heatmap.json_path)
    profile_json = _read_json(profile.json_path)
    assert profile_json["plotted_values"] == heatmap_json["plotted_values"]
    assert profile_json["y_axis_min"] == heatmap_json["color_scale_min"]
    assert profile_json["y_axis_max"] == heatmap_json["color_scale_max"]
    assert "spatial-profile legend" in profile_json["render_elements"]
    assert profile.png_path.name == (
        "spatial_profile_same-frame_8_maximum_delta_v.png"
    )


def test_spatial_profile_renders_roi_ticks_once_and_an_explicit_legend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels: list[tuple[str, tuple[int, int]]] = []
    original_draw = graph_exporter._draw_centered_text

    def capture_label(image, text, center, **kwargs):
        labels.append((str(text), center))
        return original_draw(image, text, center, **kwargs)

    monkeypatch.setattr(graph_exporter, "_draw_centered_text", capture_label)
    snapshot = SpatialGraphSnapshot(
        values=_spatial_values(),
        metric="mean_delta_v",
        scale_min=0.0,
        scale_max=100.0,
        context=CONTEXT,
        frame_id=11,
        elapsed_time_s=0.5,
        timestamp_token="tick-audit",
    )
    image = graph_exporter._render_spatial_profile(snapshot)
    assert image.shape == (LINE_GRAPH_HEIGHT, LINE_GRAPH_WIDTH, 3)
    x_tick_labels = [text for text, center in labels if center[1] == 735]
    assert x_tick_labels == [str(roi_id) for roi_id in range(1, 10)]
    assert not any("." in label for label in x_tick_labels)
    legend_region = image[108:139, 1035:1466]
    assert np.any(legend_region != 255)


def test_manual_temporal_export_is_exact_current_range_and_visible_lines(
    tmp_path: Path,
) -> None:
    frame_ids = (10, 11, 12)
    elapsed = (5.0, 5.1, 5.4)
    values = tuple(
        tuple(float(frame_index * 10 + roi_id) for roi_id in range(1, 10))
        for frame_index in range(3)
    )
    snapshot = TemporalGraphSnapshot(
        capture_frame_ids=frame_ids,
        elapsed_time_s=elapsed,
        roi_values=values,
        metric="integrated_delta_v",
        visible_roi_ids=(1, 3, 9),
        x_axis_limits=(4.9, 5.5),
        y_axis_limits=(0.0, 40.0),
        display_history_s=12.0,
        context=CONTEXT,
        timestamp_token="visible-window",
    )
    caller_thread = threading.get_ident()
    with GraphExportService(tmp_path / "graphs") as service:
        artifact = service.save_temporal_lines(snapshot).result(timeout=10)
    assert artifact.worker_thread_id != caller_thread
    assert artifact.png_path is not None and artifact.csv_path is not None
    _read_png(
        artifact.png_path, width=LINE_GRAPH_WIDTH, height=LINE_GRAPH_HEIGHT
    )
    rows = _read_csv(artifact.csv_path)
    assert [int(row["capture_frame_id"]) for row in rows] == list(frame_ids)
    assert [float(row["elapsed_time_s"]) for row in rows] == list(elapsed)
    for row_index, row in enumerate(rows):
        assert [float(row[f"roi{roi_id}_value"]) for roi_id in range(1, 10)] == list(
            values[row_index]
        )
    metadata = _read_json(artifact.json_path)
    assert metadata["start_elapsed_time_s"] == 5.0
    assert metadata["end_elapsed_time_s"] == 5.4
    assert metadata["included_frame_ids"] == list(frame_ids)
    assert metadata["visible_roi_lines"] == [1, 3, 9]
    assert metadata["x_axis_limits"] == [4.9, 5.5]
    assert metadata["y_axis_limits"] == [0.0, 40.0]
    assert metadata["display_history_s"] == 12.0
    assert metadata["visual_downsampling_method"] == "none"
    assert metadata["render_elements"] == [
        "title",
        "labeled x-axis",
        "labeled y-axis",
        "ROI legend",
        "grid lines",
        "session and trial provenance",
    ]


def test_manual_temporal_png_downsampling_never_changes_companion_csv(
    tmp_path: Path,
) -> None:
    count = 101
    snapshot = TemporalGraphSnapshot(
        capture_frame_ids=tuple(range(count)),
        elapsed_time_s=tuple(index * 0.01 for index in range(count)),
        roi_values=tuple(
            tuple(float(index + roi_id) for roi_id in range(9))
            for index in range(count)
        ),
        y_axis_limits=(0.0, 120.0),
        context=CONTEXT,
        timestamp_token="downsampled",
    )
    with GraphExportService(
        tmp_path / "graphs", maximum_plot_points=11
    ) as service:
        artifact = service.save_temporal_lines(snapshot).result(timeout=10)
    assert artifact.csv_path is not None
    assert len(_read_csv(artifact.csv_path)) == count
    metadata = _read_json(artifact.json_path)
    assert metadata["visual_downsampling_method"] == "uniform_index"
    assert metadata["full_resolution_point_count"] == count
    assert int(metadata["png_point_count"]) <= 13


def test_automatic_summaries_use_exact_peak_contact_mean_integral_and_timestamps(
    tmp_path: Path,
) -> None:
    rows = _trial_rows()
    caller_thread = threading.get_ident()
    with GraphExportService(tmp_path / "spatial_graphs") as service:
        summary = service.finalize_trial(rows, context=CONTEXT).result(timeout=20)
    assert summary.worker_thread_id != caller_thread
    assert summary.peak_frame_id == 3
    assert summary.mean_contact_available
    assert len(summary.artifacts) == 5

    peak = summary.artifact("trial_peak_heatmap")
    peak_profile = summary.artifact("trial_peak_spatial_profile")
    assert peak.png_path is not None and peak.csv_path is not None
    assert peak_profile.png_path is not None and peak_profile.csv_path is not None
    assert peak.png_path.name == "trial_peak_mean_delta_v.png"
    assert peak_profile.png_path.name == "trial_peak_spatial_profile.png"
    peak_json = _read_json(peak.json_path)
    profile_json = _read_json(peak_profile.json_path)
    assert peak_json["frame_id"] == profile_json["frame_id"] == 3
    assert peak_json["plotted_values"] == profile_json["plotted_values"] == [10.0] * 9
    assert _read_csv(peak.csv_path) == _read_csv(peak_profile.csv_path)

    mean_contact = summary.artifact("trial_mean_contact_heatmap")
    assert mean_contact.csv_path is not None
    expected_mean = [
        (rows[1][f"roi{roi_id}_delta_v_mean"] + rows[2][f"roi{roi_id}_delta_v_mean"]) / 2
        for roi_id in range(1, 10)
    ]
    assert [float(row["value"]) for row in _read_csv(mean_contact.csv_path)] == expected_mean

    integrated = summary.artifact("trial_integrated_heatmap")
    assert integrated.csv_path is not None
    expected_integrated = [
        sum(float(row[f"roi{roi_id}_delta_v_sum"]) for row in rows)
        for roi_id in range(1, 10)
    ]
    assert [float(row["value"]) for row in _read_csv(integrated.csv_path)] == expected_integrated

    temporal = summary.artifact("trial_temporal_lines")
    assert temporal.csv_path is not None and temporal.png_path is not None
    temporal_rows = _read_csv(temporal.csv_path)
    assert [int(row["capture_frame_id"]) for row in temporal_rows] == [0, 1, 2, 3]
    assert [float(row["elapsed_time_s"]) for row in temporal_rows] == [
        0.0,
        0.125,
        0.25,
        0.375,
    ]
    temporal_json = _read_json(temporal.json_path)
    assert temporal_json["peak_frame_id"] == 3
    assert temporal_json["optical_intensity_plot"] is True
    assert temporal_json["force_plot"] is False
    for artifact in summary.artifacts:
        if artifact.png_path is not None:
            expected_width = (
                HEATMAP_WIDTH
                if "heatmap" in artifact.kind
                else LINE_GRAPH_WIDTH
            )
            expected_height = (
                HEATMAP_HEIGHT
                if "heatmap" in artifact.kind
                else LINE_GRAPH_HEIGHT
            )
            _read_png(
                artifact.png_path, width=expected_width, height=expected_height
            )


def test_no_contact_summary_is_marked_unavailable_and_never_fabricated(
    tmp_path: Path,
) -> None:
    rows = _trial_rows(contacts=False)
    graph_directory = tmp_path / "graphs"
    with GraphExportService(graph_directory) as service:
        summary = service.finalize_trial(rows, context=CONTEXT).result(timeout=20)
    assert not summary.mean_contact_available
    artifact = summary.artifact("trial_mean_contact_heatmap")
    assert not artifact.available
    assert artifact.png_path is None and artifact.csv_path is None
    assert not (graph_directory / "trial_mean_contact_mean_delta_v.png").exists()
    assert not (graph_directory / "trial_mean_contact_mean_delta_v.csv").exists()
    metadata = _read_json(artifact.json_path)
    assert metadata["available"] is False
    assert "No valid frames" in str(metadata["reason"])


def test_complete_trial_png_downsample_metadata_preserves_all_csv_rows_and_peak(
    tmp_path: Path,
) -> None:
    rows = _trial_rows(75, contacts=True)
    with GraphExportService(
        tmp_path / "graphs", maximum_plot_points=9
    ) as service:
        summary = service.finalize_trial(rows, context=CONTEXT).result(timeout=20)
    temporal = summary.artifact("trial_temporal_lines")
    assert temporal.csv_path is not None
    assert len(_read_csv(temporal.csv_path)) == 75
    metadata = _read_json(temporal.json_path)
    assert metadata["visual_downsampling_method"] == "uniform_index"
    assert metadata["full_resolution_point_count"] == 75
    assert int(metadata["png_point_count"]) < 75
    assert metadata["peak_index_forced_into_png"] is True


def test_nan_snapshot_uses_blank_csv_null_json_and_strict_status(tmp_path: Path) -> None:
    values = list(_spatial_values())
    values[4] = math.nan
    snapshot = SpatialGraphSnapshot(
        values=values,
        context=CONTEXT,
        frame_id=1,
        timestamp_token="missing-value",
    )
    with GraphExportService(tmp_path / "graphs") as service:
        artifact = service.save_spatial_snapshot(snapshot).result(timeout=10)
    assert artifact.csv_path is not None
    rows = _read_csv(artifact.csv_path)
    assert rows[4]["value"] == ""
    metadata = _read_json(artifact.json_path)
    assert metadata["plotted_values"][4] is None
    statuses = metadata["nonfinite_value_status"]
    assert any(item["status"] == "nan" for item in statuses)


def test_existing_graph_is_not_overwritten_and_service_rejects_after_shutdown(
    tmp_path: Path,
) -> None:
    snapshot = SpatialGraphSnapshot(
        values=_spatial_values(), frame_id=1, timestamp_token="collision"
    )
    service = GraphExportService(tmp_path / "graphs")
    artifact = service.save_spatial_snapshot(snapshot).result(timeout=10)
    assert artifact.png_path is not None
    original = artifact.png_path.read_bytes()
    with pytest.raises(ExistingGraphError):
        service.save_spatial_snapshot(snapshot).result(timeout=10)
    assert artifact.png_path.read_bytes() == original
    service.shutdown()
    with pytest.raises(Exception, match="closed"):
        service.save_spatial_snapshot(snapshot)


def test_automatic_finalization_rejects_missing_optical_data_without_fake_success(
    tmp_path: Path,
) -> None:
    rows = _trial_rows()
    for row in rows:
        for roi_id in range(1, 10):
            row[f"roi{roi_id}_delta_v_mean"] = math.nan
    with GraphExportService(tmp_path / "graphs") as service:
        future = service.finalize_trial(rows, context=CONTEXT)
        with pytest.raises(GraphDataError, match="no frame has nine valid"):
            future.result(timeout=10)


def test_every_generated_graph_csv_header_is_in_the_data_dictionary(
    tmp_path: Path,
) -> None:
    graph_directory = tmp_path / "all-graph-types"
    spatial = SpatialGraphSnapshot(
        values=_spatial_values(),
        context=CONTEXT,
        frame_id=5,
        elapsed_time_s=0.5,
        timestamp_token="schema-audit",
    )
    temporal = TemporalGraphSnapshot(
        capture_frame_ids=(1, 2, 3),
        elapsed_time_s=(0.1, 0.2, 0.3),
        roi_values=(_spatial_values(), _spatial_values(), _spatial_values()),
        context=CONTEXT,
        timestamp_token="schema-audit",
    )
    with GraphExportService(graph_directory) as service:
        service.save_spatial_snapshot(spatial).result(timeout=10)
        service.save_spatial_profile(spatial).result(timeout=10)
        service.save_temporal_lines(temporal).result(timeout=10)
        service.finalize_trial(_trial_rows(), context=CONTEXT).result(timeout=20)

    dictionary_columns = {
        scope: tuple(
            row["column_name"]
            for row in data_dictionary_rows()
            if row["artifact_scope"] == scope
        )
        for scope in (
            "spatial_graph_companion.csv",
            "temporal_graph_companion.csv",
        )
    }
    graph_csv_paths = sorted(graph_directory.glob("*.csv"))
    assert len(graph_csv_paths) == 8
    for path in graph_csv_paths:
        with path.open("r", encoding="utf-8", newline="") as handle:
            header = tuple(next(csv.reader(handle)))
        scope = (
            "spatial_graph_companion.csv"
            if header == GRAPH_SPATIAL_COLUMNS
            else "temporal_graph_companion.csv"
        )
        assert header in {GRAPH_SPATIAL_COLUMNS, GRAPH_TEMPORAL_COLUMNS}, path.name
        assert header == dictionary_columns[scope], path.name
