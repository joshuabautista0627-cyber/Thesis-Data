from __future__ import annotations

from datetime import UTC, datetime
import math

import numpy as np
import pytest

from core.models import ApplicationConfig, ROI, TrialLabels
from data.schemas import FRAME_FEATURE_COLUMNS
from processing.baseline import BaselineContext, capture_baseline
from processing.pipeline import ProcessingPipeline
from processing.roi_manager import ROILayout
from services.interfaces import CapturedFrame


def _fixture_pipeline() -> tuple[ProcessingPipeline, np.ndarray]:
    rois = tuple(
        ROI(i + 1, (i % 3) * 8, (i // 3) * 8, 6, 6) for i in range(9)
    )
    layout = ROILayout.create(rois, 24, 24)
    config = ApplicationConfig()
    context = BaselineContext(
        camera_device="synthetic",
        backend="Video File (Simulation)",
        frame_width=24,
        frame_height=24,
        camera_settings={"exposure": -6.0},
        roi_layout_id=layout.roi_layout_id,
        processing_settings={
            "minimum_saturation_for_h": config.processing.minimum_saturation_for_h,
            "active_delta_v_threshold": config.processing.active_delta_v_threshold,
        },
    )
    baseline_frame = np.full((24, 24, 3), (15, 20, 25), dtype=np.uint8)
    baseline = capture_baseline(
        [baseline_frame.copy() for _ in range(20)],
        layout.rois,
        context=context,
        minimum_valid_frames=20,
    )
    pipeline = ProcessingPipeline(
        config=config,
        roi_layout=layout,
        baseline=baseline,
        baseline_context=context,
        trial_labels=TrialLabels("session-a", "trial-1", "skin-1", 5),
        requested_fps=30.0,
        requested_width=800,
        requested_height=600,
        actual_fps=29.8,
        camera_backend="Video File (Simulation)",
        camera_device_index=-1,
        calibration_id="cal-sim",
    )
    return pipeline, baseline_frame


def test_pipeline_outputs_exact_schema_and_preserves_original() -> None:
    pipeline, frame = _fixture_pipeline()
    frame[9:15, 9:15] = (80, 120, 180)
    before = frame.copy()
    captured = CapturedFrame(
        original_bgr=frame,
        source_frame_id=7,
        host_monotonic_ns=1_100_000_000,
        wall_clock_iso=datetime.now(UTC).isoformat(),
    )

    result = pipeline.process(
        captured, capture_frame_id=0, recording_start_monotonic_ns=1_000_000_000
    )

    assert tuple(result.feature_row) == FRAME_FEATURE_COLUMNS
    assert result.feature_row["capture_frame_id"] == 0
    assert result.feature_row["elapsed_time_s"] == pytest.approx(0.1)
    assert result.feature_row["requested_width"] == 800
    assert result.feature_row["requested_height"] == 600
    assert result.feature_row["width"] == 24
    assert result.feature_row["height"] == 24
    assert result.feature_row["error_code"] == "NONE"
    assert result.feature_row["predicted_dominant_roi"] == 5
    assert np.array_equal(frame, before)
    assert result.original_bgr is frame


def test_pipeline_preserves_failed_feature_row(monkeypatch) -> None:
    pipeline, frame = _fixture_pipeline()

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr("processing.pipeline.process_optical_frame", fail)
    result = pipeline.process(
        CapturedFrame(frame, 0, 5_000, "2026-08-03T00:00:00+00:00"),
        capture_frame_id=3,
        recording_start_monotonic_ns=4_000,
    )

    assert tuple(result.feature_row) == FRAME_FEATURE_COLUMNS
    assert result.feature_row["capture_frame_id"] == 3
    assert result.feature_row["frame_valid"] is True
    assert result.feature_row["error_code"] == "FEATURE_EXTRACTION_FAILED"
    assert math.isnan(result.feature_row["roi1_delta_v_mean"])
    assert result.feature_row["roi1_x"] == 0
    assert result.optical_result is None


def test_pipeline_rejects_context_changed_after_baseline() -> None:
    pipeline, _ = _fixture_pipeline()
    wrong_context = BaselineContext(
        camera_device="different-camera",
        backend=pipeline.baseline_context.backend,
        frame_width=24,
        frame_height=24,
        camera_settings=pipeline.baseline_context.camera_settings,
        roi_layout_id=pipeline.roi_layout.roi_layout_id,
        processing_settings=pipeline.baseline_context.processing_settings,
    )

    with pytest.raises(ValueError, match="baseline is invalid"):
        ProcessingPipeline(
            config=pipeline.config,
            roi_layout=pipeline.roi_layout,
            baseline=pipeline.baseline,
            baseline_context=wrong_context,
            trial_labels=pipeline.trial_labels,
            requested_fps=30.0,
            actual_fps=30.0,
            camera_backend="Video File (Simulation)",
            camera_device_index=-1,
        )
