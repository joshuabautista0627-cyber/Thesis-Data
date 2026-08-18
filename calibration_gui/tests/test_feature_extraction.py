"""Deterministic OpenCV HSV and optical feature-extraction tests."""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from core.models import ROI
from processing.baseline import BaselineContext, capture_baseline
from processing.feature_extraction import (
    FeatureExtractionError,
    annotate_preview,
    bgr_to_hsv,
    circular_mean_opencv_h,
    extract_roi_features,
    positive_delta_v,
    process_optical_frame,
    signed_delta_v,
)
from processing.roi_manager import roi_layout_fingerprint


FRAME_SIZE = 18


def grid_rois() -> tuple[ROI, ...]:
    return tuple(
        ROI(
            roi_id=index + 1,
            x=(index % 3) * 6,
            y=(index // 3) * 6,
            width=4,
            height=4,
        )
        for index in range(9)
    )


def from_hsv(hue: np.ndarray, saturation: np.ndarray, value: np.ndarray) -> np.ndarray:
    hsv = np.stack((hue, saturation, value), axis=-1).astype(np.uint8)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def uniform_frame(h: int = 30, s: int = 120, v: int = 50) -> np.ndarray:
    channel = np.full((FRAME_SIZE, FRAME_SIZE), h, dtype=np.uint8)
    saturation = np.full_like(channel, s)
    value = np.full_like(channel, v)
    return from_hsv(channel, saturation, value)


def make_baseline():
    rois = grid_rois()
    context = BaselineContext(
        camera_device="simulation",
        backend="SIMULATION",
        frame_width=FRAME_SIZE,
        frame_height=FRAME_SIZE,
        camera_settings={"exposure": -6},
        roi_layout_id=roi_layout_fingerprint(rois, FRAME_SIZE, FRAME_SIZE),
        processing_settings={"minimum_saturation_for_h": 10},
    )
    return capture_baseline(
        [uniform_frame()] * 20,
        rois,
        context=context,
        baseline_id="feature-baseline",
        capture_timestamp_iso="2026-08-03T00:00:00+00:00",
    )


def test_opencv_hsv_ranges_for_primary_colors_and_white() -> None:
    bgr = np.array([[[0, 0, 255], [0, 255, 0], [255, 0, 0], [255, 255, 255]]], dtype=np.uint8)
    hsv = bgr_to_hsv(bgr)
    np.testing.assert_array_equal(hsv[0, :3, 0], [0, 60, 120])
    np.testing.assert_array_equal(hsv[0, :3, 1], [255, 255, 255])
    np.testing.assert_array_equal(hsv[0, :, 2], [255, 255, 255, 255])
    assert np.all((0 <= hsv[..., 0]) & (hsv[..., 0] <= 179))
    assert hsv[0, 3, 1] == 0


def test_circular_hue_wraps_and_low_saturation_pixels_are_excluded() -> None:
    wrapped = circular_mean_opencv_h(
        np.array([179, 1], dtype=np.uint8),
        np.array([255, 255], dtype=np.uint8),
        minimum_saturation_for_h=10,
    )
    assert min(abs(wrapped), abs(180.0 - wrapped)) == pytest.approx(0.0, abs=1e-6)
    assert math.isnan(
        circular_mean_opencv_h(
            np.array([20, 100], dtype=np.uint8),
            np.array([9, 0], dtype=np.uint8),
            minimum_saturation_for_h=10,
        )
    )


def test_positive_delta_uses_signed_math_and_never_underflows() -> None:
    current = np.array([[0, 40, 50, 60, 255]], dtype=np.uint8)
    saved = np.array([[50, 50, 50, 50, 250]], dtype=np.uint8)
    np.testing.assert_array_equal(
        positive_delta_v(current, saved), np.array([[0, 0, 0, 10, 5]], dtype=np.int16)
    )
    np.testing.assert_array_equal(
        signed_delta_v(current, saved),
        np.array([[-50, -10, 0, 10, 5]], dtype=np.int16),
    )


def test_feature_extraction_calculates_all_raw_and_delta_features() -> None:
    rois = grid_rois()
    baseline = make_baseline()
    hsv = cv2.cvtColor(uniform_frame(), cv2.COLOR_BGR2HSV)
    roi1 = rois[0]
    roi_values = np.full((4, 4), 50, dtype=np.uint8)
    roi_values[1, 1] = 70
    hsv[roi1.y : roi1.y + roi1.height, roi1.x : roi1.x + roi1.width, 2] = roi_values
    current = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    features = extract_roi_features(current, rois, baseline)
    assert len(features) == 9
    first = features[0]
    assert first.mean_h == pytest.approx(30.0, abs=1.0)
    assert first.mean_s == pytest.approx(120.0, abs=2.0)
    assert first.mean_v == pytest.approx(51.25)
    assert first.median_v == pytest.approx(50.0)
    assert first.max_v == 70.0
    assert first.p95_v == pytest.approx(np.percentile(roi_values, 95))
    assert first.v_std == pytest.approx(np.std(roi_values))
    assert first.baseline_mean_v == pytest.approx(50.0)
    assert first.signed_delta_v_sum == 20.0
    assert first.signed_delta_v_mean == pytest.approx(1.25)
    assert first.signed_delta_v_median == 0.0
    assert first.signed_delta_v_mad == 0.0
    assert first.delta_v_mean == pytest.approx(1.25)
    assert first.delta_v_max == 20.0
    assert first.delta_v_p95 == pytest.approx(np.percentile(np.maximum(roi_values - 50, 0), 95))
    assert first.delta_v_sum == 20.0
    assert first.delta_v_sum_per_pixel == pytest.approx(1.25)
    assert first.delta_v_std == pytest.approx(np.std(np.where(roi_values == 70, 20, 0)))
    assert first.active_pixel_count == 1
    assert first.active_fraction == pytest.approx(1 / 16)
    assert first.roi_local_centroid_x == pytest.approx(1.0)
    assert first.roi_local_centroid_y == pytest.approx(1.0)
    assert first.full_frame_centroid_x == pytest.approx(roi1.x + 1.0)
    assert first.full_frame_centroid_y == pytest.approx(roi1.y + 1.0)
    assert features[1].delta_v_sum == 0.0


def test_signed_summaries_retain_darkening_that_positive_delta_clips() -> None:
    rois = grid_rois()
    baseline = make_baseline()
    hsv = cv2.cvtColor(uniform_frame(), cv2.COLOR_BGR2HSV)
    roi1 = rois[0]
    hsv[roi1.y, roi1.x, 2] = 30

    first = extract_roi_features(
        cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), rois, baseline
    )[0]

    assert first.signed_delta_v_sum == -20.0
    assert first.signed_delta_v_mean == pytest.approx(-1.25)
    assert first.delta_v_sum == 0.0


def test_active_threshold_is_inclusive() -> None:
    rois = grid_rois()
    baseline = make_baseline()
    hsv = cv2.cvtColor(uniform_frame(), cv2.COLOR_BGR2HSV)
    hsv[0, 0, 2] = 60
    features = extract_roi_features(
        cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR),
        rois,
        baseline,
        active_delta_v_threshold=10,
    )
    assert features[0].active_pixel_count == 1


def test_zero_delta_centroids_are_nan_not_misleading_zero() -> None:
    features = extract_roi_features(uniform_frame(), grid_rois(), make_baseline())
    first = features[0]
    assert first.delta_v_sum == 0.0
    assert math.isnan(first.roi_local_centroid_x)
    assert math.isnan(first.roi_local_centroid_y)
    assert math.isnan(first.full_frame_centroid_x)
    assert math.isnan(first.full_frame_centroid_y)


def test_roi_local_centroid_converts_to_full_frame_coordinates() -> None:
    rois = grid_rois()
    roi5 = rois[4]
    hsv = cv2.cvtColor(uniform_frame(), cv2.COLOR_BGR2HSV)
    hsv[roi5.y + 2, roi5.x + 3, 2] = 90
    features = extract_roi_features(
        cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), rois, make_baseline()
    )
    feature = features[4]
    assert (feature.roi_local_centroid_x, feature.roi_local_centroid_y) == pytest.approx((3, 2))
    assert (feature.full_frame_centroid_x, feature.full_frame_centroid_y) == pytest.approx(
        (roi5.x + 3, roi5.y + 2)
    )


def test_low_saturation_roi_returns_nan_hue_but_keeps_other_features() -> None:
    gray = uniform_frame(h=100, s=0, v=60)
    features = extract_roi_features(gray, grid_rois(), make_baseline())
    assert math.isnan(features[0].mean_h)
    assert features[0].mean_s == 0.0
    assert features[0].mean_v == 60.0


def test_each_analytical_frame_is_converted_to_hsv_exactly_once(monkeypatch) -> None:
    import processing.feature_extraction as module

    baseline = make_baseline()
    frame = uniform_frame()
    real_cvt_color = module.cv2.cvtColor
    calls = 0

    def counted(frame, code):
        nonlocal calls
        calls += 1
        return real_cvt_color(frame, code)

    monkeypatch.setattr(module.cv2, "cvtColor", counted)
    extract_roi_features(frame, grid_rois(), baseline)
    assert calls == 1


def test_extraction_and_preview_annotation_never_mutate_original_frame() -> None:
    original = uniform_frame(v=70)
    snapshot = original.copy()
    features = extract_roi_features(original, grid_rois(), make_baseline())
    preview = annotate_preview(original, grid_rois(), features)
    np.testing.assert_array_equal(original, snapshot)
    assert not np.shares_memory(preview, original)
    assert np.any(preview != original)


def test_invalidated_baseline_blocks_feature_extraction() -> None:
    baseline = make_baseline()
    baseline.invalidate("ROI layout changed")
    with pytest.raises(FeatureExtractionError, match="baseline is invalid"):
        extract_roi_features(uniform_frame(), grid_rois(), baseline)


def test_same_size_shifted_roi_cannot_use_stale_per_pixel_baseline() -> None:
    baseline = make_baseline()
    shifted = list(grid_rois())
    shifted[0] = ROI(roi_id=1, x=1, y=0, width=4, height=4)
    with pytest.raises(FeatureExtractionError, match="layout does not match"):
        extract_roi_features(uniform_frame(), shifted, baseline)
    assert not baseline.valid
    assert baseline.invalid_reason == "ROI layout changed"


def test_complete_optical_result_populates_normalized_intensities() -> None:
    frame = uniform_frame(v=60)
    result = process_optical_frame(
        frame,
        grid_rois(),
        make_baseline(),
        localization_min_mean_delta_v=5,
        target_roi_ground_truth=1,
    )
    assert result.frame_valid and result.baseline_valid
    assert result.localization.predicted_dominant_roi == 1  # deterministic tie break
    assert [feature.normalized_intensity for feature in result.roi_features] == pytest.approx(
        [1 / 9] * 9
    )


def test_roi_feature_export_names_match_the_declared_schema() -> None:
    from data.schemas import ROI_FEATURE_SCHEMA

    result = process_optical_frame(uniform_frame(v=60), grid_rois(), make_baseline())
    exported = result.roi_features[0].to_export_dict()
    expected = {
        definition.column_name
        for definition in ROI_FEATURE_SCHEMA
        if definition.column_name.startswith("roi1_")
    }
    assert set(exported) == expected
