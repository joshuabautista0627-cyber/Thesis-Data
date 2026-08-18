"""Bounded-state live Eulerian motion/color magnification.

This modern implementation uses OpenCV pyramids and timestamp-aware one-pole
temporal filters.  It intentionally has no capture, GUI, or file-writing loop.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import time
from typing import Sequence

import cv2
import numpy as np

from core.models import ROI


class MotionMagnificationError(ValueError):
    """Parameters or frame timing cannot produce a valid magnified frame."""


@dataclass(frozen=True, slots=True)
class MotionMagnificationConfig:
    enabled: bool = False
    mode: str = "Color magnification"
    amplification: float = 30.0
    lower_cutoff_hz: float = 0.4
    upper_cutoff_hz: float = 3.0
    chrominance_gain: float = 0.5
    pyramid_levels: int = 3
    lambda_c: float = 16.0
    downscale_factor: float = 0.5
    target_fps: float = 20.0
    roi_only: bool = False
    processing_width: int = 0
    processing_height: int = 0

    def __post_init__(self) -> None:
        if self.mode not in {"Color magnification", "Intensity-only magnification"}:
            raise MotionMagnificationError("unsupported motion-magnification mode")
        numeric = {
            "amplification": self.amplification,
            "lower_cutoff_hz": self.lower_cutoff_hz,
            "upper_cutoff_hz": self.upper_cutoff_hz,
            "chrominance_gain": self.chrominance_gain,
            "lambda_c": self.lambda_c,
            "downscale_factor": self.downscale_factor,
            "target_fps": self.target_fps,
        }
        if any(not math.isfinite(float(value)) for value in numeric.values()):
            raise MotionMagnificationError("motion parameters must be finite")
        if not 0.0 <= self.amplification <= 300.0:
            raise MotionMagnificationError("amplification must be between 0 and 300")
        if self.lower_cutoff_hz <= 0.0:
            raise MotionMagnificationError("lower cutoff frequency must be greater than zero")
        if self.upper_cutoff_hz <= self.lower_cutoff_hz:
            raise MotionMagnificationError("upper cutoff must exceed lower cutoff")
        if not 0.0 <= self.chrominance_gain <= 2.0:
            raise MotionMagnificationError("chrominance gain must be between 0 and 2")
        if not 1 <= int(self.pyramid_levels) <= 8:
            raise MotionMagnificationError("pyramid levels must be in 1..8")
        if self.lambda_c <= 0.0:
            raise MotionMagnificationError("lambda_c must be greater than zero")
        if not 0.1 <= self.downscale_factor <= 1.0:
            raise MotionMagnificationError("downscale factor must be in 0.1..1.0")
        if self.target_fps <= 0.0:
            raise MotionMagnificationError("target processing FPS must be positive")
        if self.processing_width < 0 or self.processing_height < 0:
            raise MotionMagnificationError("processing dimensions must be nonnegative")
        if (self.processing_width == 0) != (self.processing_height == 0):
            raise MotionMagnificationError(
                "processing width and height must both be zero or both be positive"
            )

    def validate_for_fps(self, measured_fps: float) -> None:
        fps = float(measured_fps)
        if not math.isfinite(fps) or fps <= 0.0:
            raise MotionMagnificationError("measured processing FPS is unavailable")
        if self.upper_cutoff_hz >= fps / 2.0:
            raise MotionMagnificationError(
                f"upper cutoff {self.upper_cutoff_hz:g} Hz exceeds the Nyquist limit "
                f"for measured processing FPS {fps:.3f}"
            )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def save_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
        return destination

    @classmethod
    def load_json(cls, path: str | Path) -> "MotionMagnificationConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise MotionMagnificationError("motion profile root must be an object")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class MotionMagnificationResult:
    color_bgr: np.ndarray
    intensity_bgr: np.ndarray
    source_frame_id: int
    host_monotonic_ns: int
    measured_fps: float
    processing_latency_ms: float
    processing_size: tuple[int, int]


def maximum_pyramid_levels(width: int, height: int) -> int:
    if width < 2 or height < 2:
        return 0
    return max(0, int(math.floor(math.log2(min(width, height)))) - 2)


def _laplacian_pyramid(image: np.ndarray, levels: int) -> list[np.ndarray]:
    gaussian = [image]
    for _ in range(levels):
        gaussian.append(cv2.pyrDown(gaussian[-1]))
    result: list[np.ndarray] = []
    for level in range(levels):
        current = gaussian[level]
        expanded = cv2.pyrUp(
            gaussian[level + 1], dstsize=(current.shape[1], current.shape[0])
        )
        result.append(current - expanded)
    result.append(gaussian[-1])
    return result


def _reconstruct_pyramid(pyramid: Sequence[np.ndarray]) -> np.ndarray:
    result = np.array(pyramid[-1], copy=True)
    for level in range(len(pyramid) - 2, -1, -1):
        target = pyramid[level]
        result = cv2.pyrUp(result, dstsize=(target.shape[1], target.shape[0]))
        result = result + target
    return result


class EulerianMotionMagnifier:
    """Timestamp-aware temporal filtering with bounded per-level state."""

    def __init__(self, config: MotionMagnificationConfig) -> None:
        self.config = config
        self.reset()

    def reset(self) -> None:
        self._low_cutoff_state: list[np.ndarray] | None = None
        self._high_cutoff_state: list[np.ndarray] | None = None
        self._last_timestamp_ns: int | None = None
        self._measured_fps = float(self.config.target_fps)
        self._state_shape: tuple[int, ...] | None = None

    @property
    def measured_fps(self) -> float:
        return self._measured_fps

    def _processing_rectangle(
        self, frame: np.ndarray, rois: Sequence[ROI] | None
    ) -> tuple[int, int, int, int]:
        height, width = frame.shape[:2]
        if not self.config.roi_only or not rois:
            return 0, 0, width, height
        x0 = max(0, min(roi.x for roi in rois))
        y0 = max(0, min(roi.y for roi in rois))
        x1 = min(width, max(roi.x + roi.width for roi in rois))
        y1 = min(height, max(roi.y + roi.height for roi in rois))
        if x1 - x0 < 8 or y1 - y0 < 8:
            raise MotionMagnificationError("ROI-only processing rectangle is too small")
        return x0, y0, x1, y1

    def process(
        self,
        frame_bgr: np.ndarray,
        *,
        host_monotonic_ns: int,
        source_frame_id: int = 0,
        rois: Sequence[ROI] | None = None,
    ) -> MotionMagnificationResult:
        started_ns = time.perf_counter_ns()
        frame = np.asarray(frame_bgr)
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise MotionMagnificationError("motion input must be a uint8 BGR frame")
        timestamp_ns = int(host_monotonic_ns)
        if timestamp_ns < 0:
            raise MotionMagnificationError("frame timestamp must be nonnegative")
        x0, y0, x1, y1 = self._processing_rectangle(frame, rois)
        source = np.ascontiguousarray(frame[y0:y1, x0:x1])
        if self.config.processing_width > 0:
            target_size = (
                int(self.config.processing_width), int(self.config.processing_height)
            )
        else:
            target_size = (
                max(8, round(source.shape[1] * self.config.downscale_factor)),
                max(8, round(source.shape[0] * self.config.downscale_factor)),
            )
        working_bgr = cv2.resize(source, target_size, interpolation=cv2.INTER_AREA)
        maximum = maximum_pyramid_levels(*target_size)
        if self.config.pyramid_levels > maximum:
            raise MotionMagnificationError(
                f"pyramid depth {self.config.pyramid_levels} is incompatible with "
                f"processing size {target_size[0]}x{target_size[1]}; maximum is {maximum}"
            )
        ycrcb = cv2.cvtColor(working_bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32) / 255.0
        pyramid = _laplacian_pyramid(ycrcb, self.config.pyramid_levels)
        state_shape = tuple(working_bgr.shape)
        if self._state_shape != state_shape:
            self.reset()
            self._state_shape = state_shape

        if self._last_timestamp_ns is None:
            self._low_cutoff_state = [np.array(level, copy=True) for level in pyramid]
            self._high_cutoff_state = [np.array(level, copy=True) for level in pyramid]
            filtered = [np.zeros_like(level) for level in pyramid]
        else:
            delta_s = (timestamp_ns - self._last_timestamp_ns) / 1e9
            if not math.isfinite(delta_s) or delta_s <= 0.0:
                self.reset()
                self._state_shape = state_shape
                self._low_cutoff_state = [np.array(level, copy=True) for level in pyramid]
                self._high_cutoff_state = [np.array(level, copy=True) for level in pyramid]
                filtered = [np.zeros_like(level) for level in pyramid]
            else:
                instantaneous_fps = 1.0 / delta_s
                self._measured_fps = 0.9 * self._measured_fps + 0.1 * instantaneous_fps
                self.config.validate_for_fps(self._measured_fps)
                low_alpha = 1.0 - math.exp(
                    -2.0 * math.pi * self.config.lower_cutoff_hz * delta_s
                )
                high_alpha = 1.0 - math.exp(
                    -2.0 * math.pi * self.config.upper_cutoff_hz * delta_s
                )
                assert self._low_cutoff_state is not None
                assert self._high_cutoff_state is not None
                filtered = []
                for index, level in enumerate(pyramid):
                    self._low_cutoff_state[index] = (
                        self._low_cutoff_state[index]
                        + low_alpha * (level - self._low_cutoff_state[index])
                    )
                    self._high_cutoff_state[index] = (
                        self._high_cutoff_state[index]
                        + high_alpha * (level - self._high_cutoff_state[index])
                    )
                    band = self._high_cutoff_state[index] - self._low_cutoff_state[index]
                    if index in {0, len(pyramid) - 1}:
                        gain = 0.0
                    else:
                        wavelength = math.sqrt(
                            float(target_size[0] ** 2 + target_size[1] ** 2)
                        ) / (3.0 * (2.0**index))
                        delta = self.config.lambda_c / (
                            8.0 * (1.0 + self.config.amplification)
                        )
                        gain = min(
                            self.config.amplification,
                            max(0.0, (wavelength / (8.0 * delta) - 1.0) * 2.0),
                        )
                    amplified = band * gain
                    amplified[..., 1:] *= self.config.chrominance_gain
                    filtered.append(amplified)
        self._last_timestamp_ns = timestamp_ns

        reconstructed = _reconstruct_pyramid(filtered)
        color_ycrcb = np.clip(ycrcb + reconstructed, 0.0, 1.0)
        intensity_ycrcb = np.array(ycrcb, copy=True)
        intensity_ycrcb[..., 0] = np.clip(
            ycrcb[..., 0] + reconstructed[..., 0], 0.0, 1.0
        )
        color_small = cv2.cvtColor(
            np.rint(color_ycrcb * 255.0).astype(np.uint8), cv2.COLOR_YCrCb2BGR
        )
        intensity_gray = np.rint(intensity_ycrcb[..., 0] * 255.0).astype(np.uint8)
        intensity_small = cv2.cvtColor(intensity_gray, cv2.COLOR_GRAY2BGR)
        color_region = cv2.resize(
            color_small, (source.shape[1], source.shape[0]), interpolation=cv2.INTER_LINEAR
        )
        intensity_region = cv2.resize(
            intensity_small,
            (source.shape[1], source.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
        color_full = np.array(frame, copy=True)
        intensity_full = cv2.cvtColor(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR
        )
        color_full[y0:y1, x0:x1] = color_region
        intensity_full[y0:y1, x0:x1] = intensity_region
        latency_ms = (time.perf_counter_ns() - started_ns) / 1e6
        return MotionMagnificationResult(
            color_bgr=np.ascontiguousarray(color_full),
            intensity_bgr=np.ascontiguousarray(intensity_full),
            source_frame_id=int(source_frame_id),
            host_monotonic_ns=timestamp_ns,
            measured_fps=float(self._measured_fps),
            processing_latency_ms=float(latency_ms),
            processing_size=target_size,
        )


__all__ = [
    "EulerianMotionMagnifier",
    "MotionMagnificationConfig",
    "MotionMagnificationError",
    "MotionMagnificationResult",
    "maximum_pyramid_levels",
]
