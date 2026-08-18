"""OpenCV HSV conversion and baseline-corrected optical feature extraction.

The public extraction path converts each accepted BGR frame exactly once and
uses ROI array views.  It never draws on or otherwise mutates the input frame.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Iterable, Mapping, Sequence

import cv2
import numpy as np
from numpy.typing import NDArray

from core.models import BaselineRecord, ROI, ROIBaseline
from processing.localization import LocalizationResult, localize_roi_features
from processing.roi_manager import (
    ROI_COUNT,
    order_rois,
    roi_layout_fingerprint,
    validate_rois,
)


FloatArray = NDArray[np.floating]
UInt8Array = NDArray[np.uint8]


class FeatureExtractionError(ValueError):
    """Raised when frame, ROI, or baseline inputs cannot produce valid features."""


@dataclass(frozen=True, slots=True)
class ROIFeatures:
    """Required raw and positive-delta-V measurements for one ROI."""

    roi_id: int
    x: int
    y: int
    width: int
    height: int
    area: int
    center_x: float
    center_y: float
    mean_h: float
    mean_s: float
    mean_v: float
    median_v: float
    max_v: float
    p95_v: float
    v_std: float
    baseline_mean_v: float
    signed_delta_v_sum: float
    signed_delta_v_mean: float
    signed_delta_v_median: float
    signed_delta_v_mad: float
    delta_v_mean: float
    delta_v_max: float
    delta_v_p95: float
    delta_v_sum: float
    delta_v_sum_per_pixel: float
    delta_v_std: float
    active_pixel_count: int
    active_fraction: float
    roi_local_centroid_x: float
    roi_local_centroid_y: float
    full_frame_centroid_x: float
    full_frame_centroid_y: float
    normalized_intensity: float = float("nan")

    @property
    def integrated_delta_v(self) -> float:
        """Descriptive alias for the required integrated positive delta V."""

        return self.delta_v_sum

    @property
    def integrated_delta_v_per_pixel(self) -> float:
        """Descriptive alias for integrated delta V divided by ROI area."""

        return self.delta_v_sum_per_pixel

    @property
    def std_v(self) -> float:
        """Descriptive alias for the schema field ``v_std``."""

        return self.v_std

    @property
    def centroid_x_local(self) -> float:
        """Compatibility alias for ``roi_local_centroid_x``."""

        return self.roi_local_centroid_x

    @property
    def centroid_y_local(self) -> float:
        """Compatibility alias for ``roi_local_centroid_y``."""

        return self.roi_local_centroid_y

    @property
    def centroid_x_full_frame(self) -> float:
        """Compatibility alias for ``full_frame_centroid_x``."""

        return self.full_frame_centroid_x

    @property
    def centroid_y_full_frame(self) -> float:
        """Compatibility alias for ``full_frame_centroid_y``."""

        return self.full_frame_centroid_y

    def with_normalized_intensity(self, value: float) -> "ROIFeatures":
        """Return a copy populated with the localization normalization value."""

        return replace(self, normalized_intensity=float(value))

    def to_export_dict(self, prefix: str | None = None) -> dict[str, int | float]:
        """Return predictable flat names for frame-feature/master CSV rows."""

        base = prefix if prefix is not None else f"roi{self.roi_id}_"
        if base and not base.endswith("_"):
            base += "_"
        return {
            f"{base}x": self.x,
            f"{base}y": self.y,
            f"{base}width": self.width,
            f"{base}height": self.height,
            f"{base}area": self.area,
            f"{base}center_x": self.center_x,
            f"{base}center_y": self.center_y,
            f"{base}mean_h": self.mean_h,
            f"{base}mean_s": self.mean_s,
            f"{base}mean_v": self.mean_v,
            f"{base}median_v": self.median_v,
            f"{base}max_v": self.max_v,
            f"{base}p95_v": self.p95_v,
            f"{base}v_std": self.v_std,
            f"{base}baseline_mean_v": self.baseline_mean_v,
            f"{base}signed_delta_v_sum": self.signed_delta_v_sum,
            f"{base}signed_delta_v_mean": self.signed_delta_v_mean,
            f"{base}signed_delta_v_median": self.signed_delta_v_median,
            f"{base}signed_delta_v_mad": self.signed_delta_v_mad,
            f"{base}delta_v_mean": self.delta_v_mean,
            f"{base}delta_v_max": self.delta_v_max,
            f"{base}delta_v_p95": self.delta_v_p95,
            f"{base}delta_v_sum": self.delta_v_sum,
            f"{base}delta_v_sum_per_pixel": self.delta_v_sum_per_pixel,
            f"{base}delta_v_std": self.delta_v_std,
            f"{base}active_pixel_count": self.active_pixel_count,
            f"{base}active_fraction": self.active_fraction,
            f"{base}roi_local_centroid_x": self.roi_local_centroid_x,
            f"{base}roi_local_centroid_y": self.roi_local_centroid_y,
            f"{base}full_frame_centroid_x": self.full_frame_centroid_x,
            f"{base}full_frame_centroid_y": self.full_frame_centroid_y,
            f"{base}normalized_intensity": self.normalized_intensity,
        }


@dataclass(frozen=True, slots=True)
class OpticalFrameResult:
    """Complete optical-processing result for one accepted analytical frame."""

    roi_features: tuple[ROIFeatures, ...]
    localization: LocalizationResult
    frame_valid: bool
    baseline_valid: bool
    saturation_warning: bool
    error_code: str = "NONE"

    def to_export_dict(self) -> dict[str, object]:
        """Flatten per-ROI and localization values for an incremental CSV row."""

        row: dict[str, object] = {}
        for feature in self.roi_features:
            row.update(feature.to_export_dict())
        result = self.localization
        row.update(
            {
                "predicted_dominant_roi": result.predicted_dominant_roi,
                "dominant_intensity": result.dominant_intensity,
                "second_highest_intensity": result.second_highest_intensity,
                "top_one_to_top_two_ratio": result.top_one_to_top_two_ratio,
                "dominant_to_total_ratio": result.dominant_to_total_ratio,
                "weighted_full_frame_x": result.weighted_full_frame_x,
                "weighted_full_frame_y": result.weighted_full_frame_y,
                "total_corrected_intensity": result.total_corrected_intensity,
                "global_active_pixel_count": result.global_active_pixel_count,
                "localization_confidence": result.localization_confidence,
                "predicted_roi_matches_target": result.predicted_roi_matches_target,
                "frame_valid": self.frame_valid,
                "baseline_valid": self.baseline_valid,
                "saturation_warning": self.saturation_warning,
                "error_code": self.error_code,
            }
        )
        return row


def circular_mean_opencv_h(
    hue: NDArray[np.generic],
    saturation: NDArray[np.generic],
    minimum_saturation_for_h: float = 10.0,
) -> float:
    """Return circular mean hue in OpenCV's [0, 179] convention.

    Pixels below ``minimum_saturation_for_h`` are excluded.  NaN is returned
    when none qualify or when the circular resultant is mathematically
    undefined (equal opposing directions).
    """

    hue_array = np.asarray(hue)
    saturation_array = np.asarray(saturation)
    if hue_array.shape != saturation_array.shape:
        raise FeatureExtractionError("hue and saturation arrays must have equal shape")
    if not math.isfinite(minimum_saturation_for_h) or not (
        0.0 <= minimum_saturation_for_h <= 255.0
    ):
        raise FeatureExtractionError(
            "minimum_saturation_for_h must be finite and between 0 and 255"
        )
    valid = saturation_array >= minimum_saturation_for_h
    if not np.any(valid):
        return float("nan")
    selected = hue_array[valid].astype(np.float64, copy=False)
    angles = selected * (2.0 * np.pi / 180.0)
    sine = float(np.sin(angles).sum(dtype=np.float64))
    cosine = float(np.cos(angles).sum(dtype=np.float64))
    if math.hypot(sine, cosine) <= np.finfo(np.float64).eps * selected.size:
        return float("nan")
    angle = math.atan2(sine, cosine) % (2.0 * math.pi)
    return float(angle * (180.0 / (2.0 * math.pi)))


def bgr_to_hsv(frame_bgr: NDArray[np.generic]) -> UInt8Array:
    """Validate and convert an unannotated BGR frame once using OpenCV."""

    frame = np.asarray(frame_bgr)
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise FeatureExtractionError("frame must have shape (height, width, 3)")
    if frame.dtype != np.uint8:
        raise FeatureExtractionError("frame must use uint8 BGR values")
    if frame.shape[0] <= 0 or frame.shape[1] <= 0:
        raise FeatureExtractionError("frame dimensions must be positive")
    # cvtColor allocates its output and does not modify ``frame``.
    return cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)


def signed_delta_v(
    current_v: NDArray[np.generic], baseline_v: NDArray[np.generic]
) -> NDArray[np.int16]:
    """Subtract per-pixel baseline in signed arithmetic without clipping."""

    current = np.asarray(current_v)
    baseline = np.asarray(baseline_v)
    if current.shape != baseline.shape:
        raise FeatureExtractionError(
            f"current/baseline V shapes differ: {current.shape} versus {baseline.shape}"
        )
    return (
        current.astype(np.int16, copy=False)
        - baseline.astype(np.int16, copy=False)
    )


def positive_delta_v(
    current_v: NDArray[np.generic], baseline_v: NDArray[np.generic]
) -> NDArray[np.int16]:
    """Apply the contract's exact positive clipped subtraction formula."""

    return np.maximum(signed_delta_v(current_v, baseline_v), 0)


def _baseline_by_id(baseline: BaselineRecord) -> Mapping[int, ROIBaseline]:
    if not baseline.valid:
        reason = baseline.invalid_reason or "unspecified invalidation"
        raise FeatureExtractionError(f"baseline is invalid: {reason}")
    records = {record.roi_id: record for record in baseline.roi_baselines}
    expected = set(range(1, ROI_COUNT + 1))
    if set(records) != expected or len(baseline.roi_baselines) != ROI_COUNT:
        raise FeatureExtractionError("baseline must contain exactly ROI 1 through ROI 9")
    return records


def _extract_single_roi(
    hsv: UInt8Array,
    roi: ROI,
    baseline: ROIBaseline,
    *,
    minimum_saturation_for_h: float,
    active_delta_v_threshold: float,
) -> ROIFeatures:
    roi_hsv = hsv[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width]
    hue = roi_hsv[..., 0]
    saturation = roi_hsv[..., 1]
    value = roi_hsv[..., 2]
    if baseline.median_v_image.shape != value.shape:
        raise FeatureExtractionError(
            f"ROI {roi.roi_id} baseline shape {baseline.median_v_image.shape} "
            f"does not match current ROI shape {value.shape}"
        )
    signed_delta = signed_delta_v(value, baseline.median_v_image)
    signed_median = float(np.median(signed_delta))
    delta = np.maximum(signed_delta, 0)
    delta_sum = float(delta.sum(dtype=np.float64))
    if delta_sum > 0.0:
        column_weights = delta.sum(axis=0, dtype=np.float64)
        row_weights = delta.sum(axis=1, dtype=np.float64)
        local_x = float(
            np.dot(column_weights, np.arange(roi.width, dtype=np.float64))
            / delta_sum
        )
        local_y = float(
            np.dot(row_weights, np.arange(roi.height, dtype=np.float64))
            / delta_sum
        )
        full_x = roi.x + local_x
        full_y = roi.y + local_y
    else:
        local_x = local_y = full_x = full_y = float("nan")

    active = delta >= active_delta_v_threshold
    active_count = int(np.count_nonzero(active))
    return ROIFeatures(
        roi_id=roi.roi_id,
        x=roi.x,
        y=roi.y,
        width=roi.width,
        height=roi.height,
        area=roi.area,
        center_x=roi.center_x,
        center_y=roi.center_y,
        mean_h=circular_mean_opencv_h(
            hue, saturation, minimum_saturation_for_h
        ),
        mean_s=float(np.mean(saturation, dtype=np.float64)),
        mean_v=float(np.mean(value, dtype=np.float64)),
        median_v=float(np.median(value)),
        max_v=float(np.max(value)),
        p95_v=float(np.percentile(value, 95)),
        v_std=float(np.std(value, dtype=np.float64)),
        baseline_mean_v=float(baseline.mean_v),
        signed_delta_v_sum=float(signed_delta.sum(dtype=np.float64)),
        signed_delta_v_mean=float(np.mean(signed_delta, dtype=np.float64)),
        signed_delta_v_median=signed_median,
        signed_delta_v_mad=float(
            np.median(np.abs(signed_delta.astype(np.float64) - signed_median))
        ),
        delta_v_mean=float(np.mean(delta, dtype=np.float64)),
        delta_v_max=float(np.max(delta)),
        delta_v_p95=float(np.percentile(delta, 95)),
        delta_v_sum=delta_sum,
        delta_v_sum_per_pixel=delta_sum / roi.area,
        delta_v_std=float(np.std(delta, dtype=np.float64)),
        active_pixel_count=active_count,
        active_fraction=active_count / roi.area,
        roi_local_centroid_x=local_x,
        roi_local_centroid_y=local_y,
        full_frame_centroid_x=full_x,
        full_frame_centroid_y=full_y,
    )


def extract_roi_features_from_hsv(
    hsv: UInt8Array,
    rois: Sequence[ROI] | Iterable[ROI],
    baseline: BaselineRecord,
    *,
    minimum_saturation_for_h: float = 10.0,
    active_delta_v_threshold: float = 10.0,
) -> tuple[ROIFeatures, ...]:
    """Extract all required features from an already-converted HSV frame."""

    hsv_array = np.asarray(hsv)
    if hsv_array.ndim != 3 or hsv_array.shape[2] != 3 or hsv_array.dtype != np.uint8:
        raise FeatureExtractionError("HSV frame must be a uint8 (height, width, 3) array")
    if not math.isfinite(active_delta_v_threshold) or not (
        0.0 <= active_delta_v_threshold <= 255.0
    ):
        raise FeatureExtractionError(
            "active_delta_v_threshold must be finite and between 0 and 255"
        )
    ordered = order_rois(rois)
    validation = validate_rois(ordered, hsv_array.shape[1], hsv_array.shape[0])
    if not validation.valid:
        raise FeatureExtractionError("; ".join(validation.errors))
    current_layout_id = roi_layout_fingerprint(
        ordered, hsv_array.shape[1], hsv_array.shape[0]
    )
    if baseline.roi_layout_id != current_layout_id:
        baseline.invalidate("ROI layout changed")
        raise FeatureExtractionError(
            "baseline is invalid: current ROI layout does not match baseline"
        )
    baselines = _baseline_by_id(baseline)
    return tuple(
        _extract_single_roi(
            hsv_array,
            roi,
            baselines[roi.roi_id],
            minimum_saturation_for_h=minimum_saturation_for_h,
            active_delta_v_threshold=active_delta_v_threshold,
        )
        for roi in ordered
    )


def extract_roi_features(
    frame_bgr: NDArray[np.generic],
    rois: Sequence[ROI] | Iterable[ROI],
    baseline: BaselineRecord,
    *,
    minimum_saturation_for_h: float = 10.0,
    active_delta_v_threshold: float = 10.0,
) -> tuple[ROIFeatures, ...]:
    """Convert BGR to HSV exactly once, then extract all nine ROI features."""

    hsv = bgr_to_hsv(frame_bgr)
    return extract_roi_features_from_hsv(
        hsv,
        rois,
        baseline,
        minimum_saturation_for_h=minimum_saturation_for_h,
        active_delta_v_threshold=active_delta_v_threshold,
    )


def process_optical_frame(
    frame_bgr: NDArray[np.generic],
    rois: Sequence[ROI] | Iterable[ROI],
    baseline: BaselineRecord,
    *,
    minimum_saturation_for_h: float = 10.0,
    active_delta_v_threshold: float = 10.0,
    localization_min_mean_delta_v: float = 5.0,
    target_roi_ground_truth: int | None = None,
) -> OpticalFrameResult:
    """Run shared production/simulation feature extraction and localization."""

    ordered_rois = order_rois(rois)
    features = extract_roi_features(
        frame_bgr,
        ordered_rois,
        baseline,
        minimum_saturation_for_h=minimum_saturation_for_h,
        active_delta_v_threshold=active_delta_v_threshold,
    )
    localization = localize_roi_features(
        features,
        ordered_rois,
        localization_min_mean_delta_v=localization_min_mean_delta_v,
        target_roi_ground_truth=target_roi_ground_truth,
    )
    normalized_features = tuple(
        feature.with_normalized_intensity(normalized)
        for feature, normalized in zip(
            features, localization.normalized_roi_intensities, strict=True
        )
    )
    return OpticalFrameResult(
        roi_features=normalized_features,
        localization=localization,
        frame_valid=True,
        baseline_valid=baseline.valid,
        saturation_warning=any(feature.max_v >= 255.0 for feature in features),
    )


def annotate_preview(
    frame_bgr: NDArray[np.generic],
    rois: Sequence[ROI] | Iterable[ROI],
    features: Sequence[ROIFeatures] | Iterable[ROIFeatures] | None = None,
) -> UInt8Array:
    """Return an annotated copy while preserving the original frame byte-for-byte."""

    frame = np.asarray(frame_bgr)
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise FeatureExtractionError("frame must be a uint8 BGR image")
    ordered = order_rois(rois)
    validation = validate_rois(ordered, frame.shape[1], frame.shape[0])
    if not validation.valid:
        raise FeatureExtractionError("; ".join(validation.errors))
    by_id = (
        {feature.roi_id: feature for feature in features}
        if features is not None
        else {}
    )
    preview = frame.copy()
    for roi in ordered:
        cv2.rectangle(
            preview,
            (roi.x, roi.y),
            (roi.x + roi.width - 1, roi.y + roi.height - 1),
            (0, 255, 0),
            1,
        )
        label_y = max(12, roi.y - 3)
        cv2.putText(
            preview,
            f"ROI {roi.roi_id}",
            (roi.x, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (0, 255, 0),
            1,
            cv2.LINE_AA,
        )
        feature = by_id.get(roi.roi_id)
        if feature is not None and math.isfinite(feature.full_frame_centroid_x) and math.isfinite(
            feature.full_frame_centroid_y
        ):
            cv2.circle(
                preview,
                (
                    int(round(feature.full_frame_centroid_x)),
                    int(round(feature.full_frame_centroid_y)),
                ),
                3,
                (0, 0, 255),
                -1,
            )
    return preview


# Backward-readable pipeline alias.
extract_frame_features = extract_roi_features


__all__ = [
    "FeatureExtractionError",
    "OpticalFrameResult",
    "ROIFeatures",
    "annotate_preview",
    "bgr_to_hsv",
    "circular_mean_opencv_h",
    "extract_frame_features",
    "extract_roi_features",
    "extract_roi_features_from_hsv",
    "positive_delta_v",
    "process_optical_frame",
    "signed_delta_v",
]
