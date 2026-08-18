"""Deterministic localization derived from nine mean-positive-delta-V values."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Protocol, Sequence

import numpy as np

from core.models import ROI
from processing.roi_manager import ROI_COUNT, order_rois


class FeatureLike(Protocol):
    """Structural type accepted by :func:`localize_roi_features`."""

    roi_id: int
    delta_v_mean: float
    active_pixel_count: int


@dataclass(frozen=True, slots=True)
class LocalizationResult:
    """All localization fields required for one analytical frame."""

    roi_mean_delta_v: tuple[float, ...]
    normalized_roi_intensities: tuple[float, ...]
    predicted_dominant_roi: int | None
    dominant_intensity: float
    second_highest_intensity: float
    top_one_to_top_two_ratio: float
    dominant_to_total_ratio: float
    weighted_full_frame_x: float
    weighted_full_frame_y: float
    total_corrected_intensity: float
    global_active_pixel_count: int
    localization_confidence: float
    predicted_roi_matches_target: bool | float

    @property
    def prediction_available(self) -> bool:
        """Return whether the threshold produced a dominant ROI prediction."""

        return self.predicted_dominant_roi is not None


def _unavailable_result(
    intensities: tuple[float, ...], global_active_pixel_count: int
) -> LocalizationResult:
    nan = float("nan")
    return LocalizationResult(
        roi_mean_delta_v=intensities,
        normalized_roi_intensities=(nan,) * ROI_COUNT,
        predicted_dominant_roi=None,
        dominant_intensity=nan,
        second_highest_intensity=nan,
        top_one_to_top_two_ratio=nan,
        dominant_to_total_ratio=nan,
        weighted_full_frame_x=nan,
        weighted_full_frame_y=nan,
        total_corrected_intensity=nan,
        global_active_pixel_count=global_active_pixel_count,
        localization_confidence=nan,
        predicted_roi_matches_target=nan,
    )


def calculate_localization(
    roi_mean_delta_v: Sequence[float] | Iterable[float],
    rois: Sequence[ROI] | Iterable[ROI],
    *,
    global_active_pixel_count: int = 0,
    localization_min_mean_delta_v: float = 5.0,
    target_roi_ground_truth: int | None = None,
) -> LocalizationResult:
    """Calculate dominant-ROI, confidence, ratios, and weighted coordinates.

    ``roi_mean_delta_v`` must be in ROI 1-through-9 order.  Any unavailable
    (NaN or infinite) input makes derived localization unavailable rather than
    silently replacing invalid measurements with zero.  Non-finite output is
    represented with IEEE NaN, while a below-threshold prediction is ``None``
    (exported as a blank field).
    """

    intensities = tuple(float(value) for value in roi_mean_delta_v)
    if len(intensities) != ROI_COUNT:
        raise ValueError(
            f"exactly {ROI_COUNT} localization intensities are required; "
            f"received {len(intensities)}"
        )
    ordered_rois = order_rois(rois)
    if not math.isfinite(localization_min_mean_delta_v) or (
        localization_min_mean_delta_v < 0
    ):
        raise ValueError("localization_min_mean_delta_v must be finite and nonnegative")
    if isinstance(global_active_pixel_count, bool) or global_active_pixel_count < 0:
        raise ValueError("global_active_pixel_count must be a nonnegative integer")
    if target_roi_ground_truth is not None and target_roi_ground_truth not in range(
        1, ROI_COUNT + 1
    ):
        raise ValueError("target_roi_ground_truth must be ROI 1 through ROI 9")

    values = np.asarray(intensities, dtype=np.float64)
    if not np.all(np.isfinite(values)):
        return _unavailable_result(intensities, int(global_active_pixel_count))
    if np.any(values < 0):
        raise ValueError("mean positive delta-V localization intensities cannot be negative")

    total = float(values.sum(dtype=np.float64))
    dominant_index = int(np.argmax(values))
    dominant = float(values[dominant_index])
    sorted_values = np.sort(values)
    second = float(sorted_values[-2])
    top_ratio = dominant / second if second > 0.0 else float("nan")

    if total > 0.0:
        normalized_array = values / total
        normalized = tuple(float(value) for value in normalized_array)
        confidence = dominant / total
        centers_x = np.fromiter(
            (roi.center_x for roi in ordered_rois), dtype=np.float64, count=ROI_COUNT
        )
        centers_y = np.fromiter(
            (roi.center_y for roi in ordered_rois), dtype=np.float64, count=ROI_COUNT
        )
        weighted_x = float(np.dot(values, centers_x) / total)
        weighted_y = float(np.dot(values, centers_y) / total)
    else:
        normalized = (float("nan"),) * ROI_COUNT
        confidence = float("nan")
        weighted_x = float("nan")
        weighted_y = float("nan")

    prediction = (
        ordered_rois[dominant_index].roi_id
        if dominant >= localization_min_mean_delta_v
        else None
    )
    match: bool | float
    if prediction is None or target_roi_ground_truth is None:
        match = float("nan")
    else:
        match = prediction == target_roi_ground_truth

    return LocalizationResult(
        roi_mean_delta_v=intensities,
        normalized_roi_intensities=normalized,
        predicted_dominant_roi=prediction,
        dominant_intensity=dominant,
        second_highest_intensity=second,
        top_one_to_top_two_ratio=float(top_ratio),
        dominant_to_total_ratio=float(confidence),
        weighted_full_frame_x=weighted_x,
        weighted_full_frame_y=weighted_y,
        total_corrected_intensity=total,
        global_active_pixel_count=int(global_active_pixel_count),
        localization_confidence=float(confidence),
        predicted_roi_matches_target=match,
    )


def localize_roi_features(
    features: Sequence[FeatureLike] | Iterable[FeatureLike],
    rois: Sequence[ROI] | Iterable[ROI],
    *,
    localization_min_mean_delta_v: float = 5.0,
    target_roi_ground_truth: int | None = None,
) -> LocalizationResult:
    """Calculate localization directly from ordered per-ROI feature records."""

    ordered_features = tuple(sorted(features, key=lambda feature: feature.roi_id))
    ids = [feature.roi_id for feature in ordered_features]
    expected = list(range(1, ROI_COUNT + 1))
    if ids != expected:
        raise ValueError(f"feature identities must be exactly {expected}; received {ids}")
    return calculate_localization(
        (feature.delta_v_mean for feature in ordered_features),
        rois,
        global_active_pixel_count=sum(
            feature.active_pixel_count for feature in ordered_features
        ),
        localization_min_mean_delta_v=localization_min_mean_delta_v,
        target_roi_ground_truth=target_roi_ground_truth,
    )


# Concise alias for callers that treat localization as a pipeline stage.
localize = calculate_localization


__all__ = [
    "LocalizationResult",
    "calculate_localization",
    "localize",
    "localize_roi_features",
]
