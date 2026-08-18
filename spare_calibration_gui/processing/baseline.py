"""Unloaded optical baseline capture, provenance validity, warm-up, and drift."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Iterable, Mapping, Sequence
import uuid

import cv2
import numpy as np
from numpy.typing import NDArray

from core.models import BaselineRecord, ROI, ROIBaseline
from processing.feature_extraction import (
    FeatureExtractionError,
    circular_mean_opencv_h,
    positive_delta_v,
)
from processing.roi_manager import ROI_COUNT, order_rois, roi_layout_fingerprint, validate_rois


DEFAULT_MINIMUM_BASELINE_FRAMES = 20
DEFAULT_CAMERA_WARMUP_S = 3.0
DEFAULT_BASELINE_DRIFT_MEAN_V = 5.0
BASELINE_SUMMARY_SCHEMA_VERSION = "1.0"


class BaselineError(ValueError):
    """Raised when an unloaded baseline cannot be captured or applied safely."""


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    return str(value)


def settings_fingerprint(settings: Mapping[str, object]) -> str:
    """Return a stable SHA-256 fingerprint for nested analytical settings."""

    canonical = json.dumps(
        _json_safe(settings), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BaselineContext:
    """Every condition for which a baseline remains scientifically valid."""

    camera_device: str
    backend: str
    frame_width: int
    frame_height: int
    camera_settings: Mapping[str, object] = field(default_factory=dict)
    roi_layout_id: str = ""
    processing_settings: Mapping[str, object] = field(default_factory=dict)

    @property
    def camera_fingerprint(self) -> str:
        """Fingerprint device, backend, resolution, and fixed camera controls."""

        return settings_fingerprint(
            {
                "camera_device": self.camera_device,
                "backend": self.backend,
                "frame_width": self.frame_width,
                "frame_height": self.frame_height,
                "camera_settings": self.camera_settings,
            }
        )

    @property
    def processing_fingerprint(self) -> str:
        """Fingerprint the analytical settings that influence baseline use."""

        return settings_fingerprint(self.processing_settings)


@dataclass(frozen=True, slots=True)
class BaselineDriftResult:
    """Pre-recording unloaded mean-V drift check across all nine ROIs."""

    live_roi_mean_v: tuple[float, ...]
    baseline_roi_mean_v: tuple[float, ...]
    absolute_drift_by_roi: tuple[float, ...]
    mean_absolute_drift: float
    threshold: float
    accepted: bool


class WarmupGate:
    """Exclude camera frames until the configured monotonic warm-up has elapsed."""

    def __init__(self, duration_s: float = DEFAULT_CAMERA_WARMUP_S) -> None:
        if not math.isfinite(duration_s) or duration_s < 0:
            raise BaselineError("warm-up duration must be finite and nonnegative")
        self.duration_s = float(duration_s)
        self.started_at_ns: int | None = None
        self.excluded_frame_count = 0

    def start(self, now_ns: int | None = None) -> None:
        """Start or restart warm-up and clear the exclusion counter."""

        self.started_at_ns = time.perf_counter_ns() if now_ns is None else int(now_ns)
        self.excluded_frame_count = 0

    def is_complete(self, now_ns: int | None = None) -> bool:
        """Return whether baseline/recording calculations may accept frames."""

        if self.started_at_ns is None:
            return False
        current = time.perf_counter_ns() if now_ns is None else int(now_ns)
        return current - self.started_at_ns >= round(self.duration_s * 1_000_000_000)

    def accept_frame(self, timestamp_ns: int | None = None) -> bool:
        """Return true after warm-up; count every excluded incoming frame."""

        accepted = self.is_complete(timestamp_ns)
        if not accepted:
            self.excluded_frame_count += 1
        return accepted

    def remaining_s(self, now_ns: int | None = None) -> float:
        """Return nonnegative remaining warm-up duration in seconds."""

        if self.started_at_ns is None:
            return self.duration_s
        current = time.perf_counter_ns() if now_ns is None else int(now_ns)
        elapsed = max(0, current - self.started_at_ns) / 1_000_000_000.0
        return max(0.0, self.duration_s - elapsed)


def is_frame_after_warmup(
    warmup_started_ns: int, frame_timestamp_ns: int, warmup_duration_s: float = 3.0
) -> bool:
    """Pure helper used by acquisition and deterministic warm-up tests."""

    if not math.isfinite(warmup_duration_s) or warmup_duration_s < 0:
        raise BaselineError("warm-up duration must be finite and nonnegative")
    return frame_timestamp_ns - warmup_started_ns >= round(
        warmup_duration_s * 1_000_000_000
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_frames(
    frames: Sequence[NDArray[np.generic]], rois: tuple[ROI, ...], minimum_frames: int
) -> tuple[int, int]:
    if len(frames) < minimum_frames:
        raise BaselineError(
            f"baseline requires at least {minimum_frames} valid frames; "
            f"received {len(frames)}"
        )
    first = np.asarray(frames[0])
    if first.ndim != 3 or first.shape[2] != 3 or first.dtype != np.uint8:
        raise BaselineError("baseline frames must be uint8 BGR images")
    height, width = first.shape[:2]
    validation = validate_rois(rois, width, height)
    if not validation.valid:
        raise BaselineError("; ".join(validation.errors))
    for index, frame in enumerate(frames):
        array = np.asarray(frame)
        if array.shape != first.shape or array.dtype != np.uint8:
            raise BaselineError(
                f"baseline frame {index} does not match shape {first.shape} and uint8 dtype"
            )
    return width, height


def capture_baseline(
    frames_bgr: Sequence[NDArray[np.generic]] | Iterable[NDArray[np.generic]],
    rois: Sequence[ROI] | Iterable[ROI],
    *,
    context: BaselineContext | None = None,
    minimum_saturation_for_h: float = 10.0,
    minimum_valid_frames: int = DEFAULT_MINIMUM_BASELINE_FRAMES,
    capture_timestamp_iso: str | None = None,
    baseline_id: str | None = None,
) -> BaselineRecord:
    """Capture per-pixel V baselines and required per-ROI summaries.

    Every BGR baseline frame is converted to HSV exactly once.  The source
    frames are only read; no annotation or mutation occurs.
    """

    if isinstance(minimum_valid_frames, bool) or minimum_valid_frames <= 0:
        raise BaselineError("minimum_valid_frames must be a positive integer")
    frames = tuple(np.asarray(frame) for frame in frames_bgr)
    ordered = order_rois(rois)
    frame_width, frame_height = _validate_frames(
        frames, ordered, int(minimum_valid_frames)
    )
    if context is None:
        layout_id = roi_layout_fingerprint(ordered, frame_width, frame_height)
        context = BaselineContext(
            camera_device="unspecified",
            backend="unspecified",
            frame_width=frame_width,
            frame_height=frame_height,
            roi_layout_id=layout_id,
            processing_settings={
                "minimum_saturation_for_h": minimum_saturation_for_h
            },
        )
    if context.frame_width != frame_width or context.frame_height != frame_height:
        raise BaselineError("baseline context resolution does not match captured frames")
    expected_layout_id = roi_layout_fingerprint(ordered, frame_width, frame_height)
    if context.roi_layout_id != expected_layout_id:
        raise BaselineError("baseline context ROI-layout identifier does not match geometry")

    hsv_frames = tuple(cv2.cvtColor(frame, cv2.COLOR_BGR2HSV) for frame in frames)
    captured_at = capture_timestamp_iso or _iso_now()
    roi_baselines: list[ROIBaseline] = []
    for roi in ordered:
        stack = np.stack(
            [
                hsv[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width]
                for hsv in hsv_frames
            ],
            axis=0,
        )
        hue = stack[..., 0]
        saturation = stack[..., 1]
        value = stack[..., 2]
        roi_baselines.append(
            ROIBaseline(
                roi_id=roi.roi_id,
                median_v_image=np.median(value, axis=0).astype(np.float32),
                mean_v_image=np.mean(value, axis=0, dtype=np.float64).astype(
                    np.float32
                ),
                circular_mean_h=circular_mean_opencv_h(
                    hue, saturation, minimum_saturation_for_h
                ),
                mean_s=float(np.mean(saturation, dtype=np.float64)),
                mean_v=float(np.mean(value, dtype=np.float64)),
                median_v=float(np.median(value)),
                std_v=float(np.std(value, dtype=np.float64)),
                valid_frame_count=len(frames),
                capture_timestamp_iso=captured_at,
            )
        )

    return BaselineRecord(
        baseline_id=baseline_id or f"baseline-{uuid.uuid4().hex}",
        roi_layout_id=context.roi_layout_id,
        roi_baselines=tuple(roi_baselines),
        camera_fingerprint=context.camera_fingerprint,
        processing_fingerprint=context.processing_fingerprint,
        capture_timestamp_iso=captured_at,
        camera_settings={
            "camera_device": context.camera_device,
            "backend": context.backend,
            "frame_width": frame_width,
            "frame_height": frame_height,
            "controls": dict(context.camera_settings),
            "processing_settings": dict(context.processing_settings),
        },
        valid=True,
        invalid_reason="",
    )


def validate_baseline_context(
    baseline: BaselineRecord, context: BaselineContext
) -> bool:
    """Invalidate and reject a baseline when any bound acquisition input changed."""

    reasons: list[str] = []
    if baseline.roi_layout_id != context.roi_layout_id:
        reasons.append("ROI layout changed")
    if baseline.camera_fingerprint != context.camera_fingerprint:
        reasons.append("camera device, backend, resolution, or settings changed")
    if baseline.processing_fingerprint != context.processing_fingerprint:
        reasons.append("analytical processing settings changed")
    if reasons:
        baseline.invalidate("; ".join(reasons))
        return False
    return baseline.valid


def invalidate_for_camera_change(baseline: BaselineRecord) -> None:
    """Explicit GUI/service hook for a camera-setting or mode change."""

    baseline.invalidate("camera device, backend, resolution, or settings changed")


def invalidate_for_roi_change(baseline: BaselineRecord) -> None:
    """Explicit ROI-manager hook for any rectangle change."""

    baseline.invalidate("ROI layout changed")


def evaluate_baseline_drift(
    live_roi_mean_v: Sequence[float] | Iterable[float],
    baseline_roi_mean_v: Sequence[float] | Iterable[float],
    threshold: float = DEFAULT_BASELINE_DRIFT_MEAN_V,
) -> BaselineDriftResult:
    """Evaluate the mean absolute unloaded ROI-mean-V drift criterion."""

    live = np.asarray(tuple(live_roi_mean_v), dtype=np.float64)
    saved = np.asarray(tuple(baseline_roi_mean_v), dtype=np.float64)
    if live.shape != (ROI_COUNT,) or saved.shape != (ROI_COUNT,):
        raise BaselineError("drift evaluation requires exactly nine live and baseline means")
    if not np.all(np.isfinite(live)) or not np.all(np.isfinite(saved)):
        raise BaselineError("drift inputs must all be finite")
    if not math.isfinite(threshold) or threshold < 0:
        raise BaselineError("drift threshold must be finite and nonnegative")
    differences = np.abs(live - saved)
    mean_drift = float(np.mean(differences, dtype=np.float64))
    return BaselineDriftResult(
        live_roi_mean_v=tuple(float(value) for value in live),
        baseline_roi_mean_v=tuple(float(value) for value in saved),
        absolute_drift_by_roi=tuple(float(value) for value in differences),
        mean_absolute_drift=mean_drift,
        threshold=float(threshold),
        accepted=mean_drift <= threshold,
    )


def check_frame_baseline_drift(
    unloaded_frame_bgr: NDArray[np.generic],
    rois: Sequence[ROI] | Iterable[ROI],
    baseline: BaselineRecord,
    threshold: float = DEFAULT_BASELINE_DRIFT_MEAN_V,
) -> BaselineDriftResult:
    """Convert one live unloaded frame once and compare ROI mean V to baseline."""

    if not baseline.valid:
        raise BaselineError(f"cannot check drift with invalid baseline: {baseline.invalid_reason}")
    frame = np.asarray(unloaded_frame_bgr)
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise BaselineError("drift frame must be a uint8 BGR image")
    ordered = order_rois(rois)
    validation = validate_rois(ordered, frame.shape[1], frame.shape[0])
    if not validation.valid:
        raise BaselineError("; ".join(validation.errors))
    current_layout_id = roi_layout_fingerprint(
        ordered, frame.shape[1], frame.shape[0]
    )
    if baseline.roi_layout_id != current_layout_id:
        baseline.invalidate("ROI layout changed")
        raise BaselineError(
            "cannot check drift: current ROI layout does not match baseline"
        )
    saved_by_id = {record.roi_id: record for record in baseline.roi_baselines}
    if set(saved_by_id) != set(range(1, ROI_COUNT + 1)):
        raise BaselineError("baseline does not contain exactly ROI 1 through ROI 9")
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    live_means = tuple(
        float(
            np.mean(
                hsv[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width, 2],
                dtype=np.float64,
            )
        )
        for roi in ordered
    )
    baseline_means = tuple(saved_by_id[roi.roi_id].mean_v for roi in ordered)
    return evaluate_baseline_drift(live_means, baseline_means, threshold)


def subtract_baseline_v(
    current_v: NDArray[np.generic], roi_baseline: ROIBaseline
) -> NDArray[np.int16]:
    """Public baseline-subtraction helper using per-pixel median V."""

    try:
        return positive_delta_v(current_v, roi_baseline.median_v_image)
    except FeatureExtractionError as exc:
        raise BaselineError(str(exc)) from exc


def baseline_summary_dict(baseline: BaselineRecord) -> dict[str, object]:
    """Return JSON-safe baseline metadata without embedding per-pixel arrays."""

    payload = {
        "schema_version": BASELINE_SUMMARY_SCHEMA_VERSION,
        "baseline_id": baseline.baseline_id,
        "roi_layout_id": baseline.roi_layout_id,
        "camera_fingerprint": baseline.camera_fingerprint,
        "processing_fingerprint": baseline.processing_fingerprint,
        "capture_timestamp_iso": baseline.capture_timestamp_iso,
        "camera_device": baseline.camera_settings.get("camera_device", ""),
        "backend": baseline.camera_settings.get("backend", ""),
        "frame_width": baseline.camera_settings.get("frame_width"),
        "frame_height": baseline.camera_settings.get("frame_height"),
        "camera_settings": _json_safe(baseline.camera_settings),
        "processing_settings": _json_safe(
            baseline.camera_settings.get("processing_settings", {})
        ),
        "valid": baseline.valid,
        "invalid_reason": baseline.invalid_reason,
        "rois": [
            {
                "roi_id": record.roi_id,
                "circular_mean_h": record.circular_mean_h,
                "mean_s": record.mean_s,
                "mean_v": record.mean_v,
                "median_v": record.median_v,
                "std_v": record.std_v,
                "valid_frame_count": record.valid_frame_count,
                "capture_timestamp_iso": record.capture_timestamp_iso,
                "array_shape": list(record.median_v_image.shape),
            }
            for record in baseline.roi_baselines
        ],
    }
    safe_payload = _json_safe(payload)
    if not isinstance(safe_payload, dict):  # defensive: payload root is fixed above
        raise BaselineError("baseline summary root must be a JSON object")
    return safe_payload


def save_baseline(
    baseline: BaselineRecord, summary_path: str | Path, data_path: str | Path
) -> tuple[Path, Path]:
    """Atomically save baseline summary JSON and compressed per-pixel arrays."""

    summary_destination = Path(summary_path)
    data_destination = Path(data_path)
    summary_destination.parent.mkdir(parents=True, exist_ok=True)
    data_destination.parent.mkdir(parents=True, exist_ok=True)
    summary_temporary = summary_destination.with_name(summary_destination.name + ".tmp")
    data_temporary = data_destination.with_name(data_destination.name + ".tmp.npz")
    summary_temporary.write_text(
        json.dumps(
            baseline_summary_dict(baseline),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    arrays: dict[str, NDArray[np.generic]] = {}
    for record in baseline.roi_baselines:
        arrays[f"roi{record.roi_id}_median_v"] = record.median_v_image
        arrays[f"roi{record.roi_id}_mean_v"] = record.mean_v_image
    np.savez_compressed(data_temporary, **arrays)
    os.replace(summary_temporary, summary_destination)
    os.replace(data_temporary, data_destination)
    return summary_destination, data_destination


__all__ = [
    "BASELINE_SUMMARY_SCHEMA_VERSION",
    "BaselineContext",
    "BaselineDriftResult",
    "BaselineError",
    "DEFAULT_BASELINE_DRIFT_MEAN_V",
    "DEFAULT_CAMERA_WARMUP_S",
    "DEFAULT_MINIMUM_BASELINE_FRAMES",
    "WarmupGate",
    "baseline_summary_dict",
    "capture_baseline",
    "check_frame_baseline_drift",
    "evaluate_baseline_drift",
    "invalidate_for_camera_change",
    "invalidate_for_roi_change",
    "is_frame_after_warmup",
    "save_baseline",
    "settings_fingerprint",
    "subtract_baseline_v",
    "validate_baseline_context",
]
