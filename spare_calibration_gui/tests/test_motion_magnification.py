from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from core.models import ROI
from processing.motion_magnification import (
    EulerianMotionMagnifier,
    MotionMagnificationConfig,
    MotionMagnificationError,
    maximum_pyramid_levels,
)


def _frame(offset: int = 0) -> np.ndarray:
    image = np.zeros((96, 128, 3), dtype=np.uint8)
    image[24:72, 38 + offset : 78 + offset] = (70, 115, 180)
    return image


def test_config_validation_and_measured_fps_nyquist_guard() -> None:
    with pytest.raises(MotionMagnificationError, match="upper cutoff"):
        MotionMagnificationConfig(lower_cutoff_hz=2.0, upper_cutoff_hz=1.0)
    config = MotionMagnificationConfig(upper_cutoff_hz=3.0)
    config.validate_for_fps(20.0)
    with pytest.raises(MotionMagnificationError, match="Nyquist"):
        config.validate_for_fps(5.0)


def test_profile_round_trip_preserves_all_parameters(tmp_path) -> None:
    config = MotionMagnificationConfig(
        enabled=True,
        mode="Intensity-only magnification",
        amplification=18.0,
        lower_cutoff_hz=0.25,
        upper_cutoff_hz=2.5,
        chrominance_gain=0.2,
        pyramid_levels=2,
        lambda_c=12.0,
        downscale_factor=0.75,
        target_fps=24.0,
        roi_only=True,
    )
    path = tmp_path / "motion_profile.json"
    config.save_json(path)
    assert MotionMagnificationConfig.load_json(path) == config


def test_temporal_filter_uses_distinct_output_and_safe_state_copies() -> None:
    config = MotionMagnificationConfig(
        enabled=True,
        amplification=25.0,
        lower_cutoff_hz=0.4,
        upper_cutoff_hz=3.0,
        pyramid_levels=2,
        downscale_factor=0.5,
        target_fps=30.0,
    )
    magnifier = EulerianMotionMagnifier(config)
    first_source = _frame(0)
    first = magnifier.process(
        first_source, host_monotonic_ns=1_000_000_000, source_frame_id=1
    )
    first_snapshot = first.color_bgr.copy()
    second = magnifier.process(
        _frame(2), host_monotonic_ns=1_033_333_333, source_frame_id=2
    )
    assert first.source_frame_id == 1 and second.source_frame_id == 2
    assert first.color_bgr is not first_source
    assert np.array_equal(first.color_bgr, first_snapshot)
    assert second.color_bgr.shape == first_source.shape
    assert second.intensity_bgr.shape == first_source.shape
    assert second.processing_size == (64, 48)
    assert 20.0 < second.measured_fps < 40.0


def test_roi_only_magnification_preserves_pixels_outside_roi() -> None:
    config = MotionMagnificationConfig(
        enabled=True,
        amplification=15.0,
        pyramid_levels=1,
        roi_only=True,
        downscale_factor=1.0,
        target_fps=30.0,
    )
    magnifier = EulerianMotionMagnifier(config)
    roi = ROI(1, 32, 20, 64, 56)
    source = _frame()
    result = magnifier.process(
        source, host_monotonic_ns=1_000_000_000, rois=(roi,)
    )
    outside = np.ones(source.shape[:2], dtype=bool)
    outside[20:76, 32:96] = False
    assert np.array_equal(result.color_bgr[outside], source[outside])


def test_pyramid_depth_is_checked_against_effective_processing_resolution() -> None:
    config = MotionMagnificationConfig(
        enabled=True,
        pyramid_levels=4,
        processing_width=16,
        processing_height=16,
    )
    assert maximum_pyramid_levels(16, 16) == 2
    with pytest.raises(MotionMagnificationError, match="maximum is 2"):
        EulerianMotionMagnifier(config).process(
            _frame(), host_monotonic_ns=1_000_000_000
        )


def test_timestamp_regression_resets_filter_without_mutating_input() -> None:
    config = replace(MotionMagnificationConfig(), pyramid_levels=2)
    magnifier = EulerianMotionMagnifier(config)
    source = _frame()
    original = source.copy()
    magnifier.process(source, host_monotonic_ns=2_000_000_000)
    result = magnifier.process(source, host_monotonic_ns=1_000_000_000)
    assert np.array_equal(source, original)
    assert result.measured_fps == config.target_fps

