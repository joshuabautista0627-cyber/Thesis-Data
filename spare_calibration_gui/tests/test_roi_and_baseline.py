"""Deterministic tests for nine-ROI rules and unloaded optical baselines."""

from __future__ import annotations

from dataclasses import dataclass
import json

import cv2
import numpy as np
import pytest

from core.models import ROI
from processing.baseline import (
    BaselineContext,
    BaselineError,
    WarmupGate,
    capture_baseline,
    check_frame_baseline_drift,
    evaluate_baseline_drift,
    invalidate_for_camera_change,
    save_baseline,
    subtract_baseline_v,
    validate_baseline_context,
)
from processing.roi_manager import (
    ROILayout,
    ROIManager,
    ROIValidationError,
    copy_roi1_size,
    find_roi_overlaps,
    load_roi_layout,
    order_rois,
    roi_layout_fingerprint,
    save_roi_layout,
    validate_rois,
)


FRAME_WIDTH = 18
FRAME_HEIGHT = 18


def grid_rois(*, width: int = 4, height: int = 4) -> tuple[ROI, ...]:
    return tuple(
        ROI(
            roi_id=index + 1,
            x=(index % 3) * 6,
            y=(index // 3) * 6,
            width=width,
            height=height,
        )
        for index in range(9)
    )


def hsv_frame(h: int = 30, s: int = 100, v: int = 50) -> np.ndarray:
    hsv = np.empty((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    hsv[..., 0] = h
    hsv[..., 1] = s
    hsv[..., 2] = v
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def baseline_context(
    rois: tuple[ROI, ...], *, exposure: int = -6
) -> BaselineContext:
    return BaselineContext(
        camera_device="simulation-video",
        backend="SIMULATION",
        frame_width=FRAME_WIDTH,
        frame_height=FRAME_HEIGHT,
        camera_settings={"exposure": exposure, "gain": 2},
        roi_layout_id=roi_layout_fingerprint(rois, FRAME_WIDTH, FRAME_HEIGHT),
        processing_settings={
            "minimum_saturation_for_h": 10,
            "active_delta_v_threshold": 10,
        },
    )


def captured_baseline(rois: tuple[ROI, ...] | None = None):
    selected = rois or grid_rois()
    frames = [hsv_frame(v=40 if index % 2 == 0 else 60) for index in range(20)]
    return capture_baseline(
        frames,
        selected,
        context=baseline_context(selected),
        capture_timestamp_iso="2026-08-03T00:00:00+00:00",
        baseline_id="baseline-test",
    )


def test_nine_roi_ordering_is_numeric_and_stable() -> None:
    ordered = grid_rois()
    reversed_input = tuple(reversed(ordered))
    assert [roi.roi_id for roi in order_rois(reversed_input)] == list(range(1, 10))
    with pytest.raises(ROIValidationError, match="exactly 9"):
        order_rois(ordered[:-1])
    with pytest.raises(ROIValidationError, match="identities"):
        order_rois(ordered[:-1] + (ordered[0],))


def test_roi_bounds_accept_exact_edges_and_reject_overflow() -> None:
    valid = grid_rois(width=6, height=6)
    result = validate_rois(valid, FRAME_WIDTH, FRAME_HEIGHT)
    assert result.valid
    overflow = list(grid_rois())
    overflow[-1] = ROI(roi_id=9, x=16, y=12, width=4, height=4)
    result = validate_rois(overflow, FRAME_WIDTH, FRAME_HEIGHT)
    assert not result.valid
    assert "extends outside" in result.errors[0]


def test_roi_overlap_is_a_warning_not_a_hard_bounds_failure() -> None:
    rois = list(grid_rois())
    rois[1] = ROI(roi_id=2, x=2, y=0, width=4, height=4)
    result = validate_rois(rois, FRAME_WIDTH, FRAME_HEIGHT)
    assert result.valid
    assert result.overlap_pairs == ((1, 2),)
    assert result.warnings == ("ROI 1 overlaps ROI 2",)
    assert find_roi_overlaps(rois) == ((1, 2),)


def test_copy_roi1_size_preserves_every_other_origin() -> None:
    rois = list(grid_rois())
    rois[0] = ROI(roi_id=1, x=0, y=0, width=5, height=3)
    copied = copy_roi1_size(rois)
    assert [(roi.width, roi.height) for roi in copied] == [(5, 3)] * 9
    assert [(roi.x, roi.y) for roi in copied] == [
        (roi.x, roi.y) for roi in order_rois(rois)
    ]


def test_roi_layout_json_round_trip_preserves_order_and_derived_geometry(
    tmp_path,
) -> None:
    layout = ROILayout.create(tuple(reversed(grid_rois())), FRAME_WIDTH, FRAME_HEIGHT)
    path = save_roi_layout(tmp_path / "roi_layout.json", layout)
    loaded = load_roi_layout(path)
    assert loaded == layout
    assert loaded.roi_layout_id.startswith("roi-")
    payload = path.read_text(encoding="utf-8")
    assert '"area": 16' in payload
    assert '"center_x"' in payload


def test_roi_layout_json_round_trip_preserves_camera_orientation(tmp_path) -> None:
    layout = ROILayout.create(
        grid_rois(),
        FRAME_WIDTH,
        FRAME_HEIGHT,
        rotation_degrees=270,
        mirror_horizontal=True,
    )

    loaded = load_roi_layout(save_roi_layout(tmp_path / "oriented_roi.json", layout))

    assert loaded == layout
    assert loaded.rotation_degrees == 270
    assert loaded.mirror_horizontal is True
    payload = json.loads((tmp_path / "oriented_roi.json").read_text(encoding="utf-8"))
    assert payload["camera_orientation"] == {
        "rotation_degrees": 270,
        "mirror_horizontal": True,
    }


@dataclass
class _DummyBaseline:
    valid: bool = True
    invalid_reason: str = ""

    def invalidate(self, reason: str) -> None:
        self.valid = False
        self.invalid_reason = reason


def test_roi_manager_change_invalidates_bound_baseline() -> None:
    manager = ROIManager(ROILayout.create(grid_rois(), FRAME_WIDTH, FRAME_HEIGHT))
    baseline = _DummyBaseline()
    manager.bind_baseline(baseline)
    manager.update_roi(1, x=1, y=1, width=4, height=4)
    assert not baseline.valid
    assert baseline.invalid_reason == "ROI layout changed"


def test_baseline_capture_saves_per_pixel_median_mean_and_summaries() -> None:
    baseline = captured_baseline()
    assert baseline.valid
    assert len(baseline.roi_baselines) == 9
    roi1 = baseline.roi_baselines[0]
    np.testing.assert_array_equal(roi1.median_v_image, np.full((4, 4), 50))
    np.testing.assert_array_equal(roi1.mean_v_image, np.full((4, 4), 50))
    assert roi1.mean_v == pytest.approx(50.0)
    assert roi1.median_v == pytest.approx(50.0)
    assert roi1.std_v == pytest.approx(10.0)
    assert roi1.valid_frame_count == 20
    assert roi1.circular_mean_h == pytest.approx(30.0, abs=1.0)


def test_baseline_capture_requires_twenty_valid_frames() -> None:
    rois = grid_rois()
    with pytest.raises(BaselineError, match="at least 20"):
        capture_baseline(
            [hsv_frame()] * 19, rois, context=baseline_context(rois)
        )


def test_baseline_subtraction_is_positive_only_and_per_pixel() -> None:
    baseline = captured_baseline()
    roi1 = baseline.roi_baselines[0]
    current = np.array(
        [[45, 50, 55, 60], [40, 49, 51, 70], [0, 50, 50, 50], [255, 1, 2, 3]],
        dtype=np.uint8,
    )
    expected = np.maximum(current.astype(np.int16) - 50, 0)
    np.testing.assert_array_equal(subtract_baseline_v(current, roi1), expected)


def test_camera_and_roi_context_changes_invalidate_baseline() -> None:
    rois = grid_rois()
    baseline = captured_baseline(rois)
    changed_camera = baseline_context(rois, exposure=-5)
    assert not validate_baseline_context(baseline, changed_camera)
    assert not baseline.valid
    assert "camera" in baseline.invalid_reason

    baseline = captured_baseline(rois)
    changed_rois = list(rois)
    changed_rois[0] = ROI(roi_id=1, x=1, y=0, width=4, height=4)
    changed_context = baseline_context(tuple(changed_rois))
    assert not validate_baseline_context(baseline, changed_context)
    assert "ROI layout" in baseline.invalid_reason

    baseline = captured_baseline(rois)
    invalidate_for_camera_change(baseline)
    assert not baseline.valid


def test_camera_warmup_excludes_frames_until_duration_elapsed() -> None:
    gate = WarmupGate(duration_s=3.0)
    gate.start(now_ns=1_000_000_000)
    assert not gate.accept_frame(1_000_000_000)
    assert not gate.accept_frame(3_999_999_999)
    assert gate.accept_frame(4_000_000_000)
    assert gate.excluded_frame_count == 2
    assert gate.remaining_s(2_500_000_000) == pytest.approx(1.5)


def test_pre_recording_baseline_drift_accepts_five_and_rejects_above_five() -> None:
    accepted = evaluate_baseline_drift([55.0] * 9, [50.0] * 9, threshold=5.0)
    rejected = evaluate_baseline_drift([55.1] * 9, [50.0] * 9, threshold=5.0)
    assert accepted.accepted
    assert accepted.mean_absolute_drift == pytest.approx(5.0)
    assert not rejected.accepted


def test_frame_drift_uses_live_unloaded_roi_means() -> None:
    rois = grid_rois()
    baseline = capture_baseline(
        [hsv_frame(v=50)] * 20,
        rois,
        context=baseline_context(rois),
        baseline_id="drift-baseline",
    )
    result = check_frame_baseline_drift(hsv_frame(v=56), rois, baseline, threshold=5)
    assert result.live_roi_mean_v == pytest.approx((56.0,) * 9)
    assert result.mean_absolute_drift == pytest.approx(6.0)
    assert not result.accepted


def test_frame_drift_rejects_same_size_shifted_roi_layout() -> None:
    rois = grid_rois()
    baseline = captured_baseline(rois)
    shifted = list(rois)
    shifted[0] = ROI(roi_id=1, x=1, y=0, width=4, height=4)
    with pytest.raises(BaselineError, match="layout does not match"):
        check_frame_baseline_drift(hsv_frame(v=50), shifted, baseline)
    assert not baseline.valid
    assert baseline.invalid_reason == "ROI layout changed"


def test_baseline_arrays_and_summary_are_saved(tmp_path) -> None:
    baseline = captured_baseline()
    summary, data = save_baseline(
        baseline, tmp_path / "baseline_summary.json", tmp_path / "baseline_data.npz"
    )
    assert summary.exists() and data.exists()
    with np.load(data) as arrays:
        assert set(arrays.files) == {
            *(f"roi{index}_median_v" for index in range(1, 10)),
            *(f"roi{index}_mean_v" for index in range(1, 10)),
        }
        np.testing.assert_array_equal(arrays["roi1_median_v"], np.full((4, 4), 50))


def test_low_saturation_hue_is_saved_as_strict_json_null(tmp_path) -> None:
    rois = grid_rois()
    baseline = capture_baseline(
        [hsv_frame(h=90, s=0, v=50)] * 20,
        rois,
        context=baseline_context(rois),
        baseline_id="low-saturation-baseline",
    )
    summary, _ = save_baseline(
        baseline, tmp_path / "baseline_summary.json", tmp_path / "baseline_data.npz"
    )
    text = summary.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    payload = json.loads(
        text,
        parse_constant=lambda value: (_ for _ in ()).throw(
            AssertionError(f"non-standard JSON constant: {value}")
        ),
    )
    assert payload["rois"][0]["circular_mean_h"] is None
