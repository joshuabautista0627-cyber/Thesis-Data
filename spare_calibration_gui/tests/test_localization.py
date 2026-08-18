"""Deterministic tests for the specified nine-ROI localization formulas."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import pytest

from core.models import ROI
from processing.localization import calculate_localization, localize_roi_features


def grid_rois() -> tuple[ROI, ...]:
    return tuple(
        ROI(
            roi_id=index + 1,
            x=(index % 3) * 10,
            y=(index // 3) * 10,
            width=4,
            height=4,
        )
        for index in range(9)
    )


def test_dominant_roi_confidence_ratios_normalization_and_weighted_position() -> None:
    rois = grid_rois()
    intensities = tuple(float(value) for value in range(1, 10))
    result = calculate_localization(
        intensities,
        rois,
        global_active_pixel_count=37,
        localization_min_mean_delta_v=5,
        target_roi_ground_truth=9,
    )
    total = sum(intensities)
    assert result.predicted_dominant_roi == 9
    assert result.dominant_intensity == 9.0
    assert result.second_highest_intensity == 8.0
    assert result.top_one_to_top_two_ratio == pytest.approx(9 / 8)
    assert result.dominant_to_total_ratio == pytest.approx(9 / total)
    assert result.localization_confidence == pytest.approx(9 / total)
    assert result.total_corrected_intensity == 45.0
    assert result.global_active_pixel_count == 37
    assert result.predicted_roi_matches_target is True
    assert result.normalized_roi_intensities == pytest.approx(
        tuple(value / total for value in intensities)
    )
    expected_x = sum(value * roi.center_x for value, roi in zip(intensities, rois)) / total
    expected_y = sum(value * roi.center_y for value, roi in zip(intensities, rois)) / total
    assert result.weighted_full_frame_x == pytest.approx(expected_x)
    assert result.weighted_full_frame_y == pytest.approx(expected_y)


def test_zero_total_returns_nan_normalization_confidence_and_coordinates() -> None:
    result = calculate_localization([0.0] * 9, grid_rois())
    assert result.predicted_dominant_roi is None
    assert result.dominant_intensity == 0.0
    assert result.second_highest_intensity == 0.0
    assert result.total_corrected_intensity == 0.0
    assert all(math.isnan(value) for value in result.normalized_roi_intensities)
    assert math.isnan(result.localization_confidence)
    assert math.isnan(result.dominant_to_total_ratio)
    assert math.isnan(result.top_one_to_top_two_ratio)
    assert math.isnan(result.weighted_full_frame_x)
    assert math.isnan(result.weighted_full_frame_y)
    assert math.isnan(result.predicted_roi_matches_target)


def test_prediction_is_blank_below_threshold_but_other_derivatives_remain_valid() -> None:
    result = calculate_localization(
        [4.9, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        grid_rois(),
        localization_min_mean_delta_v=5.0,
        target_roi_ground_truth=1,
    )
    assert result.predicted_dominant_roi is None
    assert result.total_corrected_intensity == pytest.approx(5.9)
    assert result.localization_confidence == pytest.approx(4.9 / 5.9)
    assert math.isnan(result.predicted_roi_matches_target)


def test_prediction_threshold_is_inclusive() -> None:
    result = calculate_localization(
        [0, 0, 0, 0, 5, 0, 0, 0, 0],
        grid_rois(),
        localization_min_mean_delta_v=5,
        target_roi_ground_truth=4,
    )
    assert result.predicted_dominant_roi == 5
    assert result.predicted_roi_matches_target is False


def test_top_one_to_top_two_ratio_is_nan_when_second_is_zero() -> None:
    result = calculate_localization(
        [10, 0, 0, 0, 0, 0, 0, 0, 0], grid_rois()
    )
    assert result.predicted_dominant_roi == 1
    assert math.isnan(result.top_one_to_top_two_ratio)
    assert result.localization_confidence == 1.0


def test_dominant_tie_breaks_to_lowest_ordered_roi() -> None:
    result = calculate_localization(
        [7, 7, 1, 1, 1, 1, 1, 1, 1], grid_rois()
    )
    assert result.predicted_dominant_roi == 1
    assert result.dominant_intensity == 7
    assert result.second_highest_intensity == 7
    assert result.top_one_to_top_two_ratio == 1


def test_unavailable_intensity_propagates_nan_instead_of_becoming_zero() -> None:
    result = calculate_localization(
        [1, 2, float("nan"), 4, 5, 6, 7, 8, 9], grid_rois()
    )
    assert result.predicted_dominant_roi is None
    assert math.isnan(result.total_corrected_intensity)
    assert math.isnan(result.dominant_intensity)
    assert all(math.isnan(value) for value in result.normalized_roi_intensities)


def test_negative_or_wrong_length_intensities_are_rejected() -> None:
    with pytest.raises(ValueError, match="exactly 9"):
        calculate_localization([1] * 8, grid_rois())
    with pytest.raises(ValueError, match="cannot be negative"):
        calculate_localization([1, 1, 1, 1, -1, 1, 1, 1, 1], grid_rois())


@dataclass(frozen=True)
class _Feature:
    roi_id: int
    delta_v_mean: float
    active_pixel_count: int


def test_localize_roi_features_preserves_roi_order_and_sums_active_pixels() -> None:
    features = tuple(
        _Feature(roi_id=index, delta_v_mean=float(index), active_pixel_count=index)
        for index in reversed(range(1, 10))
    )
    result = localize_roi_features(features, grid_rois())
    assert result.roi_mean_delta_v == tuple(float(index) for index in range(1, 10))
    assert result.global_active_pixel_count == sum(range(1, 10))
    assert result.predicted_dominant_roi == 9


def test_weighted_center_uses_full_frame_roi_centers() -> None:
    rois = grid_rois()
    result = calculate_localization(
        [0, 0, 0, 0, 10, 0, 0, 0, 0], rois
    )
    assert result.weighted_full_frame_x == rois[4].center_x
    assert result.weighted_full_frame_y == rois[4].center_y

