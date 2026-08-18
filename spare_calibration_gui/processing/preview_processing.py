"""Display-only frame transformations with no acquisition or recording side effects."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math

import cv2
import numpy as np

from core.models import BaselineRecord, ROI


PROCESSED_FRAME_MODES = (
    "Background-subtracted frame",
    "HSV / intensity visualization",
    "Spatial intensity visualization",
    "Motion-magnified color preview",
    "Motion-magnified intensity preview",
    "Raw frame for comparison",
)


def roi_overlay(frame_bgr: np.ndarray, rois: Sequence[ROI]) -> np.ndarray:
    result = np.array(frame_bgr, copy=True)
    for roi in rois:
        cv2.rectangle(
            result,
            (int(roi.x), int(roi.y)),
            (int(roi.x + roi.width - 1), int(roi.y + roi.height - 1)),
            (0, 255, 255),
            1,
        )
        cv2.putText(
            result,
            f"ROI {roi.roi_id}",
            (int(roi.x) + 2, max(12, int(roi.y) + 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            (0, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return result


def _background_subtracted(
    frame_bgr: np.ndarray,
    baseline: BaselineRecord | None,
    rois: Sequence[ROI],
) -> np.ndarray:
    height, width = frame_bgr.shape[:2]
    result = np.zeros((height, width), dtype=np.uint8)
    if baseline is None or not baseline.valid:
        return cv2.cvtColor(result, cv2.COLOR_GRAY2BGR)
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    by_id = {item.roi_id: item for item in baseline.roi_baselines}
    for roi in rois:
        saved = by_id.get(roi.roi_id)
        if saved is None:
            continue
        current = hsv[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width, 2]
        if current.shape != saved.median_v_image.shape:
            continue
        delta = np.maximum(
            current.astype(np.float32) - saved.median_v_image.astype(np.float32), 0.0
        )
        result[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width] = np.clip(
            delta, 0, 255
        ).astype(np.uint8)
    return cv2.applyColorMap(result, cv2.COLORMAP_TURBO)


def _spatial_intensity(
    frame_bgr: np.ndarray, optical_row: Mapping[str, object] | None
) -> np.ndarray:
    height, width = frame_bgr.shape[:2]
    values = []
    for index in range(1, 10):
        try:
            value = float((optical_row or {}).get(f"roi{index}_delta_v_mean", 0.0))
        except (TypeError, ValueError):
            value = 0.0
        values.append(value if math.isfinite(value) else 0.0)
    grid = np.asarray(values, dtype=np.float32).reshape(3, 3)
    normalized = np.clip(grid, 0.0, 255.0).astype(np.uint8)
    image = cv2.resize(normalized, (width, height), interpolation=cv2.INTER_NEAREST)
    return cv2.applyColorMap(image, cv2.COLORMAP_TURBO)


def build_processed_preview(
    frame_bgr: np.ndarray,
    mode: str,
    *,
    baseline: BaselineRecord | None = None,
    rois: Sequence[ROI] = (),
    optical_row: Mapping[str, object] | None = None,
    motion_color_bgr: np.ndarray | None = None,
    motion_intensity_bgr: np.ndarray | None = None,
) -> np.ndarray:
    frame = np.asarray(frame_bgr)
    if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("processed preview source must be a uint8 BGR frame")
    if mode == "Raw frame for comparison":
        return np.array(frame, copy=True)
    if mode == "Background-subtracted frame":
        return _background_subtracted(frame, baseline, rois)
    if mode == "HSV / intensity visualization":
        value = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[..., 2]
        return cv2.applyColorMap(value, cv2.COLORMAP_VIRIDIS)
    if mode == "Spatial intensity visualization":
        return _spatial_intensity(frame, optical_row)
    if mode == "Motion-magnified color preview":
        return (
            np.array(motion_color_bgr, copy=True)
            if motion_color_bgr is not None
            else np.array(frame, copy=True)
        )
    if mode == "Motion-magnified intensity preview":
        return (
            np.array(motion_intensity_bgr, copy=True)
            if motion_intensity_bgr is not None
            else cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        )
    raise ValueError(f"unsupported processed preview mode: {mode}")


__all__ = [
    "PROCESSED_FRAME_MODES",
    "build_processed_preview",
    "roi_overlay",
]
