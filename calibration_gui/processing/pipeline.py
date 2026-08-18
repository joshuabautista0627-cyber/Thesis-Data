"""Shared frame-processing boundary used by simulation and physical acquisition."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import platform
from typing import Any, Mapping

import cv2
import numpy as np

from core.models import (
    APPLICATION_VERSION,
    SCHEMA_VERSION,
    ApplicationConfig,
    BaselineRecord,
    ErrorCode,
    TrialLabels,
)
from core.motion import MotionSnapshot
from data.schemas import FRAME_FEATURE_COLUMNS
from processing.baseline import BaselineContext, validate_baseline_context
from processing.feature_extraction import OpticalFrameResult, process_optical_frame
from processing.motion_magnification import (
    EulerianMotionMagnifier,
    MotionMagnificationConfig,
)
from processing.preview_processing import build_processed_preview, roi_overlay
from processing.roi_manager import ROILayout
from services.interfaces import CapturedFrame


@dataclass(frozen=True, slots=True)
class ProcessedFrame:
    """One accepted frame, its immutable identity, and its complete CSV row."""

    capture_frame_id: int
    original_bgr: np.ndarray
    feature_row: Mapping[str, Any]
    optical_result: OpticalFrameResult | None
    video_streams: Mapping[str, np.ndarray] = field(default_factory=dict)


class ProcessingPipeline:
    """Own optical feature/localization work without owning a camera or files."""

    def __init__(
        self,
        *,
        config: ApplicationConfig,
        roi_layout: ROILayout,
        baseline: BaselineRecord,
        baseline_context: BaselineContext,
        trial_labels: TrialLabels,
        requested_fps: float,
        requested_width: int | None = None,
        requested_height: int | None = None,
        actual_fps: float,
        camera_backend: str,
        camera_device_index: int,
        applied_camera_properties: Mapping[str, float] | None = None,
        calibration_id: str = "",
        arduino_protocol_version: str = "",
        arduino_firmware_version: str = "",
        motion_config: MotionMagnificationConfig | None = None,
        quantitative_analysis_source: str = "Background-subtracted frame",
        processed_preview_mode: str = "Background-subtracted frame",
        auxiliary_video_streams: tuple[str, ...] = (),
    ) -> None:
        if not validate_baseline_context(baseline, baseline_context):
            raise ValueError(f"baseline is invalid: {baseline.invalid_reason}")
        if roi_layout.roi_layout_id != baseline.roi_layout_id:
            raise ValueError("ROI layout does not match the saved baseline")
        if requested_fps <= 0.0 or actual_fps <= 0.0:
            raise ValueError("requested and actual FPS must be positive")
        resolved_requested_width = (
            config.camera.requested_width
            if requested_width is None
            else requested_width
        )
        resolved_requested_height = (
            config.camera.requested_height
            if requested_height is None
            else requested_height
        )
        if (
            isinstance(resolved_requested_width, bool)
            or isinstance(resolved_requested_height, bool)
            or not isinstance(resolved_requested_width, (int, np.integer))
            or not isinstance(resolved_requested_height, (int, np.integer))
            or int(resolved_requested_width) <= 0
            or int(resolved_requested_height) <= 0
        ):
            raise ValueError("requested camera dimensions must be positive integers")
        self.config = config
        self.roi_layout = roi_layout
        self.baseline = baseline
        self.baseline_context = baseline_context
        self.trial_labels = trial_labels
        self.requested_fps = float(requested_fps)
        self.requested_width = int(resolved_requested_width)
        self.requested_height = int(resolved_requested_height)
        self.actual_fps = float(actual_fps)
        self.camera_backend = str(camera_backend)
        self.camera_device_index = int(camera_device_index)
        self.applied_camera_properties = dict(applied_camera_properties or {})
        self.calibration_id = calibration_id
        self.arduino_protocol_version = arduino_protocol_version
        self.arduino_firmware_version = arduino_firmware_version
        self.motion_config = motion_config or MotionMagnificationConfig()
        self.quantitative_analysis_source = str(quantitative_analysis_source)
        if self.quantitative_analysis_source not in {
            "Original frame",
            "Background-subtracted frame",
            "Motion-magnified frame",
        }:
            raise ValueError("unsupported quantitative analysis source")
        self.processed_preview_mode = str(processed_preview_mode)
        self.auxiliary_video_streams = tuple(auxiliary_video_streams)
        needs_motion = (
            self.motion_config.enabled
            and (
                self.quantitative_analysis_source == "Motion-magnified frame"
                or "motion_magnified" in self.auxiliary_video_streams
                or self.processed_preview_mode.startswith("Motion-magnified")
            )
        )
        self._motion_magnifier = (
            EulerianMotionMagnifier(self.motion_config) if needs_motion else None
        )
        if needs_motion:
            self.motion_config.validate_for_fps(self.actual_fps)

    def process(
        self,
        captured: CapturedFrame,
        *,
        capture_frame_id: int,
        recording_start_monotonic_ns: int,
        motion_context: Mapping[str, Any] | None = None,
    ) -> ProcessedFrame:
        """Process one accepted original frame and preserve a row on failure."""

        if capture_frame_id < 0:
            raise ValueError("capture_frame_id must be nonnegative")
        if captured.host_monotonic_ns < recording_start_monotonic_ns:
            raise ValueError("captured frame predates the recording start")
        height, width = captured.original_bgr.shape[:2]
        if (width, height) != (
            self.roi_layout.frame_width,
            self.roi_layout.frame_height,
        ):
            raise ValueError("captured frame dimensions do not match the ROI layout")

        original_pixels = captured.original_bgr
        motion_result = None
        if self._motion_magnifier is not None:
            motion_result = self._motion_magnifier.process(
                original_pixels,
                host_monotonic_ns=captured.host_monotonic_ns,
                source_frame_id=captured.source_frame_id,
                rois=self.roi_layout.rois,
            )
        analysis_pixels = original_pixels
        if self.quantitative_analysis_source == "Motion-magnified frame":
            if motion_result is None:
                raise ValueError(
                    "motion-magnified analysis was selected while magnification is disabled"
                )
            analysis_pixels = (
                motion_result.intensity_bgr
                if self.motion_config.mode == "Intensity-only magnification"
                else motion_result.color_bgr
            )
        row = self._base_row(
            captured,
            capture_frame_id=capture_frame_id,
            recording_start_monotonic_ns=recording_start_monotonic_ns,
        )
        if motion_context:
            row.update(dict(motion_context))
        optical: OpticalFrameResult | None
        try:
            optical = process_optical_frame(
                analysis_pixels,
                self.roi_layout.rois,
                self.baseline,
                minimum_saturation_for_h=(
                    self.config.processing.minimum_saturation_for_h
                ),
                active_delta_v_threshold=(
                    self.config.processing.active_delta_v_threshold
                ),
                localization_min_mean_delta_v=(
                    self.config.processing.localization_min_mean_delta_v
                ),
                target_roi_ground_truth=(
                    self.trial_labels.target_roi_ground_truth
                ),
            )
            row.update(optical.to_export_dict())
        except Exception:
            optical = None
            self._preserve_roi_geometry(row)
            row.update(
                {
                    "frame_valid": True,
                    "baseline_valid": bool(self.baseline.valid),
                    "saturation_warning": False,
                    "error_code": ErrorCode.FEATURE_EXTRACTION_FAILED.value,
                }
            )

        ordered = {column: row[column] for column in FRAME_FEATURE_COLUMNS}
        video_streams: dict[str, np.ndarray] = {}
        if "original_overlays" in self.auxiliary_video_streams:
            video_streams["original_overlays"] = roi_overlay(
                original_pixels, self.roi_layout.rois
            )
        if "processed" in self.auxiliary_video_streams:
            video_streams["processed"] = build_processed_preview(
                original_pixels,
                self.processed_preview_mode,
                baseline=self.baseline,
                rois=self.roi_layout.rois,
                optical_row=ordered,
                motion_color_bgr=(
                    None if motion_result is None else motion_result.color_bgr
                ),
                motion_intensity_bgr=(
                    None if motion_result is None else motion_result.intensity_bgr
                ),
            )
        if "motion_magnified" in self.auxiliary_video_streams and motion_result is not None:
            video_streams["motion_magnified"] = (
                motion_result.intensity_bgr
                if self.motion_config.mode == "Intensity-only magnification"
                else motion_result.color_bgr
            )
        return ProcessedFrame(
            capture_frame_id=capture_frame_id,
            original_bgr=original_pixels,
            feature_row=ordered,
            optical_result=optical,
            video_streams=video_streams,
        )

    def _base_row(
        self,
        captured: CapturedFrame,
        *,
        capture_frame_id: int,
        recording_start_monotonic_ns: int,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {column: math.nan for column in FRAME_FEATURE_COLUMNS}
        row.update(self.trial_labels.as_export_dict())
        row.update(MotionSnapshot().as_row())
        row["contact_threshold_N"] = self.config.loadcell.contact_threshold_N
        row.update(
            {
                "capture_frame_id": capture_frame_id,
                "host_monotonic_ns": captured.host_monotonic_ns,
                "elapsed_time_s": (
                    captured.host_monotonic_ns - recording_start_monotonic_ns
                )
                / 1_000_000_000.0,
                "wall_clock_iso": captured.wall_clock_iso,
                "requested_width": self.requested_width,
                "requested_height": self.requested_height,
                "requested_fps": self.requested_fps,
                "actual_fps": self.actual_fps,
                "width": self.roi_layout.frame_width,
                "height": self.roi_layout.frame_height,
                "camera_backend": self.camera_backend,
                "camera_device_index": self.camera_device_index,
                "applied_exposure": self.applied_camera_properties.get(
                    "exposure", math.nan
                ),
                "applied_gain": self.applied_camera_properties.get("gain", math.nan),
                "applied_white_balance": self.applied_camera_properties.get(
                    "white_balance", math.nan
                ),
                "frame_valid": True,
                "baseline_valid": bool(self.baseline.valid),
                "saturation_warning": False,
                "error_code": ErrorCode.NONE.value,
                "schema_version": SCHEMA_VERSION,
                "application_version": APPLICATION_VERSION,
                "arduino_protocol_version": self.arduino_protocol_version,
                "arduino_firmware_version": self.arduino_firmware_version,
                "python_version": platform.python_version(),
                "opencv_version": cv2.__version__,
                "operating_system": platform.platform(),
                "calibration_id": self.calibration_id,
                "baseline_id": self.baseline.baseline_id,
                "roi_layout_id": self.roi_layout.roi_layout_id,
            }
        )
        return row

    def _preserve_roi_geometry(self, row: dict[str, Any]) -> None:
        baselines = {item.roi_id: item for item in self.baseline.roi_baselines}
        for roi in self.roi_layout.rois:
            prefix = f"roi{roi.roi_id}_"
            row.update(
                {
                    f"{prefix}x": roi.x,
                    f"{prefix}y": roi.y,
                    f"{prefix}width": roi.width,
                    f"{prefix}height": roi.height,
                    f"{prefix}area": roi.area,
                    f"{prefix}center_x": roi.center_x,
                    f"{prefix}center_y": roi.center_y,
                    f"{prefix}baseline_mean_v": baselines[roi.roi_id].mean_v,
                }
            )


__all__ = ["ProcessedFrame", "ProcessingPipeline"]
