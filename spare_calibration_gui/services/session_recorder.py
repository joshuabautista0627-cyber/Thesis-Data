"""Single-owner incremental session recorder and video lifetime boundary.

The recorder is deliberately synchronous: callers run it in the dedicated
recorder worker, never the GUI or acquisition thread.  It is the sole owner of
the two incremental CSV writers, the experimental ``cv2.VideoWriter``, and the
application-log handle for a session.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import shutil
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

import cv2
import numpy as np

from core.models import (
    BaselineRecord,
    ErrorCode,
    LoadCellCalibration,
    LoadCellSample,
    RecordingConfig,
    RecordingLifecycle,
)
from data.exporter import (
    IncrementalCsvWriter,
    atomic_write_json,
    finalize_master_from_partials,
    loadcell_sample_to_row,
    write_data_dictionary,
)
from data.graph_exporter import GraphContext, GraphExportService, TrialGraphSummary
from data.schemas import FRAME_FEATURE_COLUMNS, LOADCELL_RAW_COLUMNS
from processing.pipeline import ProcessedFrame
from processing.baseline import save_baseline
from processing.roi_manager import ROILayout, save_roi_layout


MEBIBYTE = 1024 * 1024


class CsvWriterLike(Protocol):
    """Runtime surface the recorder exclusively owns."""

    rows_written: int

    def write_row(self, row: Mapping[str, Any]) -> None:
        """Validate and append one complete schema row."""

    def flush(self) -> None:
        """Flush buffered CSV data."""

    def close(self) -> None:
        """Flush and close the handle."""


class VideoWriterLike(Protocol):
    """Subset shared by OpenCV and deterministic test doubles."""

    def isOpened(self) -> bool:  # noqa: N802 - OpenCV API spelling
        """Return whether codec/container initialization succeeded."""

    def write(self, frame: np.ndarray) -> object:
        """Encode one BGR frame."""

    def release(self) -> None:
        """Flush and close the video container."""


class SessionRecorderError(RuntimeError):
    """Base class for recorder failures that leave recoverable evidence."""


class RecorderStateError(SessionRecorderError):
    """Raised when an operation is invalid for the recording lifecycle."""


class RecorderPreflightError(SessionRecorderError):
    """Raised before recording when disk or codec preflight fails."""


class RecorderIntegrityError(SessionRecorderError):
    """Raised when frame/video/CSV one-to-one identity cannot be guaranteed."""


class DiskSpaceLowError(SessionRecorderError):
    """Raised after a safe partial stop caused by the disk safety threshold."""


@dataclass(frozen=True, slots=True)
class RecorderSnapshot:
    """Immutable status suitable for GUI signals and tests."""

    lifecycle: RecordingLifecycle
    session_directory: Path | None
    session_status: str
    accepted_frame_count: int
    feature_row_count: int
    video_frame_count: int
    loadcell_sample_count: int
    video_filename: str
    video_codec: str
    errors: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class RecordingCompletion:
    """Evidence returned only after clean validated finalization."""

    session_directory: Path
    video_path: Path
    video_codec: str
    accepted_frame_count: int
    video_frame_count: int
    feature_row_count: int
    loadcell_sample_count: int
    exporter_summary: object
    graph_summary: TrialGraphSummary


@dataclass(frozen=True, slots=True)
class RecordingPreflightResult:
    """Successful, disposable output/disk/video readiness evidence."""

    output_directory: Path
    output_directory_valid: bool
    disk_space_sufficient: bool
    video_writer_preflight_passed: bool
    selected_codec: str
    selected_container: str
    free_bytes: int
    required_free_bytes: int
    attempted_codecs: tuple[str, ...]


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return str(getattr(value, "value"))
    return str(value)


def _free_bytes(
    provider: Callable[[str | Path], object], path: str | Path
) -> int:
    """Return free bytes from either ``shutil.disk_usage`` or a test double."""

    usage = provider(path)
    free = getattr(usage, "free", None)
    if free is None:
        try:
            free = usage[2]  # type: ignore[index]
        except (TypeError, IndexError) as exc:
            raise RecorderPreflightError(
                "disk usage provider did not return a free-byte value"
            ) from exc
    try:
        result = int(free)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecorderPreflightError(
            "disk usage provider returned an invalid free-byte value"
        ) from exc
    if result < 0:
        raise RecorderPreflightError(
            "disk usage provider returned a negative free-byte value"
        )
    return result


def preflight_recording_output(
    config: RecordingConfig,
    *,
    frame_size: tuple[int, int],
    output_fps: float,
    video_writer_factory: Callable[..., VideoWriterLike] = cv2.VideoWriter,
    video_capture_factory: Callable[[str], object] = cv2.VideoCapture,
    disk_usage_provider: Callable[[str | Path], object] = shutil.disk_usage,
) -> RecordingPreflightResult:
    """Test output writability, disk space, and video encoding without residue.

    The helper creates one uniquely named temporary directory beneath the
    selected output directory, writes and decodes one unannotated black frame,
    and removes only that temporary directory on exit.  It is synchronous by
    design so GUI callers can run it in a ``FunctionThread`` before setting the
    independent readiness flags.
    """

    width, height = frame_size
    if width <= 0 or height <= 0:
        raise RecorderPreflightError("video frame dimensions must be positive")
    if not math.isfinite(output_fps) or output_fps <= 0:
        raise RecorderPreflightError("output_fps must be finite and positive")

    output_root = Path(config.output_directory).expanduser().resolve()
    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RecorderPreflightError(
            f"output directory could not be created: {output_root}: {exc}"
        ) from exc
    if not output_root.is_dir():
        raise RecorderPreflightError(f"output path is not a directory: {output_root}")

    try:
        free = _free_bytes(disk_usage_provider, output_root)
    except RecorderPreflightError:
        raise
    except Exception as exc:
        raise RecorderPreflightError(
            f"disk-space preflight failed for {output_root}: {exc}"
        ) from exc
    required = int(config.minimum_preflight_free_mb) * MEBIBYTE
    if free < required:
        raise RecorderPreflightError(
            f"free disk space {free / MEBIBYTE:.1f} MiB is below required "
            f"{config.minimum_preflight_free_mb} MiB"
        )

    attempts = (
        (config.primary_video_codec, ".mp4"),
        (config.fallback_video_codec, ".avi"),
    )
    failures: list[str] = []
    selected: tuple[str, str] | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix=".recording_preflight_", dir=output_root
        ) as temporary_directory:
            temporary_root = Path(temporary_directory).resolve()
            # Verify the selected directory can create, flush, and close a file.
            probe = temporary_root / "output_write_probe.tmp"
            with probe.open("xb") as handle:
                handle.write(b"SPARE recording output preflight\n")
                handle.flush()
                os.fsync(handle.fileno())
            probe.unlink()

            black_frame = np.zeros((height, width, 3), dtype=np.uint8)
            for codec, suffix in attempts:
                video_path = temporary_root / f"video_writer_probe{suffix}"
                writer: VideoWriterLike | None = None
                capture: object | None = None
                try:
                    fourcc = cv2.VideoWriter_fourcc(*codec)
                    writer = video_writer_factory(
                        str(video_path), fourcc, float(output_fps), (width, height)
                    )
                    if writer is None or not writer.isOpened():
                        raise RecorderPreflightError("writer did not open")
                    write_result = writer.write(black_frame)
                    if write_result is False:
                        raise RecorderPreflightError("writer rejected the probe frame")
                    writer.release()
                    writer = None
                    if not video_path.is_file() or video_path.stat().st_size <= 0:
                        raise RecorderPreflightError(
                            "writer did not produce a non-empty probe video"
                        )
                    capture = video_capture_factory(str(video_path))
                    if capture is None or not capture.isOpened():  # type: ignore[attr-defined]
                        raise RecorderPreflightError("probe video could not be reopened")
                    ok, decoded = capture.read()  # type: ignore[attr-defined]
                    if not ok or decoded is None:
                        raise RecorderPreflightError("probe video frame could not be decoded")
                    if tuple(decoded.shape[:2]) != (height, width):
                        raise RecorderPreflightError(
                            "decoded probe frame dimensions do not match requested mode"
                        )
                    selected = (codec, suffix)
                    break
                except Exception as exc:
                    failures.append(f"{codec}: {exc}")
                finally:
                    if capture is not None:
                        try:
                            capture.release()  # type: ignore[attr-defined]
                        except Exception as exc:
                            failures.append(f"{codec} capture release: {exc}")
                    if writer is not None:
                        try:
                            writer.release()
                        except Exception as exc:
                            failures.append(f"{codec} writer release: {exc}")
                    if video_path.exists():
                        try:
                            video_path.unlink()
                        except OSError as exc:
                            failures.append(f"{codec} probe cleanup: {exc}")
    except RecorderPreflightError:
        raise
    except OSError as exc:
        raise RecorderPreflightError(
            f"output directory write preflight failed: {output_root}: {exc}"
        ) from exc

    if selected is None:
        raise RecorderPreflightError(
            "video writer preflight failed for MP4 and AVI: " + "; ".join(failures)
        )
    return RecordingPreflightResult(
        output_directory=output_root,
        output_directory_valid=True,
        disk_space_sufficient=True,
        video_writer_preflight_passed=True,
        selected_codec=selected[0],
        selected_container=selected[1],
        free_bytes=free,
        required_free_bytes=required,
        attempted_codecs=tuple(codec for codec, _suffix in attempts),
    )


class SessionRecorder:
    """Own incremental raw outputs and enforce one video/frame-feature identity."""

    def __init__(
        self,
        config: RecordingConfig,
        *,
        csv_writer_factory: Callable[..., CsvWriterLike] = IncrementalCsvWriter,
        master_finalizer: Callable[..., object] = finalize_master_from_partials,
        graph_export_service_factory: Callable[[str | Path], GraphExportService] = (
            GraphExportService
        ),
        video_writer_factory: Callable[..., VideoWriterLike] = cv2.VideoWriter,
        video_capture_factory: Callable[[str], object] = cv2.VideoCapture,
        disk_usage_provider: Callable[[str | Path], object] = shutil.disk_usage,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
        disk_check_interval_frames: int = 30,
        disk_check_interval_s: float = 1.0,
        status_write_interval_s: float = 0.5,
        status_clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        if disk_check_interval_frames < 1:
            raise ValueError("disk_check_interval_frames must be positive")
        if not math.isfinite(disk_check_interval_s) or disk_check_interval_s <= 0:
            raise ValueError("disk_check_interval_s must be finite and positive")
        if not math.isfinite(status_write_interval_s) or status_write_interval_s <= 0:
            raise ValueError("status_write_interval_s must be finite and positive")
        self.config = config
        self._csv_writer_factory = csv_writer_factory
        self._master_finalizer = master_finalizer
        self._graph_export_service_factory = graph_export_service_factory
        self._video_writer_factory = video_writer_factory
        self._video_capture_factory = video_capture_factory
        self._disk_usage_provider = disk_usage_provider
        self._monotonic_ns = monotonic_ns
        self._disk_check_interval_frames = int(disk_check_interval_frames)
        self._disk_check_interval_ns = round(disk_check_interval_s * 1_000_000_000)
        self._status_write_interval_ns = round(
            status_write_interval_s * 1_000_000_000
        )
        self._status_clock_ns = status_clock_ns

        self.lifecycle = RecordingLifecycle.IDLE
        self.session_directory: Path | None = None
        self._session_status = "idle"
        self._session_metadata: dict[str, object] = {}
        self._calibration: LoadCellCalibration | Mapping[str, object] | None = None
        self._max_sync_gap_ms = 200.0
        self._contact_threshold_N = 0.05
        self._frame_size = (0, 0)
        self._output_fps = 0.0
        self._video_writer: VideoWriterLike | None = None
        self._frame_writer: CsvWriterLike | None = None
        self._loadcell_writer: CsvWriterLike | None = None
        self._log_handle: Any | None = None
        self._partial_video_path: Path | None = None
        self._final_video_path: Path | None = None
        self._video_codec = ""
        self._auxiliary_streams: tuple[str, ...] = ()
        self._auxiliary_video_writers: dict[str, VideoWriterLike] = {}
        self._auxiliary_partial_paths: dict[str, Path] = {}
        self._auxiliary_final_paths: dict[str, Path] = {}
        self._auxiliary_write_counts: dict[str, int] = {}
        self._accepted_frame_count = 0
        self._ingress_accepted_frame_count = 0
        self._ingress_accepted_loadcell_sample_count = 0
        self._video_write_count = 0
        self._feature_row_count = 0
        self._loadcell_sample_count = 0
        self._errors: list[dict[str, object]] = []
        self._recording_started_ns: int | None = None
        self._last_disk_check_ns = 0
        self._completion: RecordingCompletion | None = None
        self._required_artifacts_written = False
        self._artifact_paths: dict[str, Path] = {}
        self._last_status_write_ns: int | None = None
        self._automatic_graph_summary: dict[str, object] = {
            "status": "not_started",
            "automatic_heatmap_summary_status": "not_started",
            "automatic_temporal_line_status": "not_started",
            "automatic_spatial_profile_status": "not_started",
            "trial_mean_contact_mean_delta_v_status": "not_evaluated",
            "generated_files": [],
        }

    @property
    def snapshot(self) -> RecorderSnapshot:
        """Return current counts, selected codec, and accumulated errors."""

        return RecorderSnapshot(
            lifecycle=self.lifecycle,
            session_directory=self.session_directory,
            session_status=self._session_status,
            accepted_frame_count=self._accepted_frame_count,
            feature_row_count=self._feature_row_count,
            video_frame_count=self._video_write_count,
            loadcell_sample_count=self._loadcell_sample_count,
            video_filename=(
                self._final_video_path.name if self._final_video_path else ""
            ),
            video_codec=self._video_codec,
            errors=tuple(dict(error) for error in self._errors),
        )

    def set_ingress_counts(
        self,
        *,
        accepted_frame_count: int,
        accepted_loadcell_sample_count: int,
    ) -> None:
        """Record producer-ingress counts separately from persisted row counts."""

        if (
            isinstance(accepted_frame_count, bool)
            or isinstance(accepted_loadcell_sample_count, bool)
            or int(accepted_frame_count) < 0
            or int(accepted_loadcell_sample_count) < 0
        ):
            raise ValueError("ingress counts must be nonnegative integers")
        self._ingress_accepted_frame_count = int(accepted_frame_count)
        self._ingress_accepted_loadcell_sample_count = int(
            accepted_loadcell_sample_count
        )
        if (
            self.session_directory is not None
            and self.lifecycle is RecordingLifecycle.ERROR
        ):
            self._write_status_best_effort()

    def start(
        self,
        session_directory_name: str | None,
        *,
        frame_size: tuple[int, int],
        output_fps: float,
        calibration: LoadCellCalibration | Mapping[str, object],
        session_config: Mapping[str, object] | None = None,
        camera_settings: Mapping[str, object] | None = None,
        roi_layout: ROILayout | None = None,
        baseline: BaselineRecord | None = None,
        max_sync_gap_ms: float = 200.0,
        contact_threshold_N: float = 0.05,
        auxiliary_video_streams: tuple[str, ...] = (),
    ) -> Path:
        """Create a non-overwriting session and open all incremental outputs."""

        if self.lifecycle is not RecordingLifecycle.IDLE:
            raise RecorderStateError("a SessionRecorder instance can start only once")
        width, height = frame_size
        if width <= 0 or height <= 0:
            raise RecorderPreflightError("video frame dimensions must be positive")
        if not math.isfinite(output_fps) or output_fps <= 0:
            raise RecorderPreflightError("output_fps must be finite and positive")
        if not math.isfinite(max_sync_gap_ms) or max_sync_gap_ms <= 0:
            raise RecorderPreflightError("max_sync_gap_ms must be finite and positive")
        if not math.isfinite(contact_threshold_N) or contact_threshold_N < 0:
            raise RecorderPreflightError(
                "contact_threshold_N must be finite and nonnegative"
            )

        output_root = Path(self.config.output_directory)
        output_root.mkdir(parents=True, exist_ok=True)
        self._require_free_space(
            output_root,
            self.config.minimum_preflight_free_mb,
            preflight=True,
        )
        directory_name = self._validated_directory_name(session_directory_name)
        session_directory = output_root / directory_name
        try:
            session_directory.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise RecorderPreflightError(
                f"session directory already exists and will not be overwritten: "
                f"{session_directory}"
            ) from exc

        self.session_directory = session_directory
        self._frame_size = (int(width), int(height))
        self._output_fps = float(output_fps)
        self._calibration = calibration
        self._max_sync_gap_ms = float(max_sync_gap_ms)
        self._contact_threshold_N = float(contact_threshold_N)
        allowed_streams = {"original_overlays", "processed", "motion_magnified"}
        requested_streams = tuple(str(item) for item in auxiliary_video_streams)
        if len(set(requested_streams)) != len(requested_streams):
            raise RecorderPreflightError("auxiliary video streams must be unique")
        if not set(requested_streams) <= allowed_streams:
            raise RecorderPreflightError("unknown auxiliary video stream requested")
        self._auxiliary_streams = requested_streams
        self._session_metadata = dict(session_config or {})
        # Runtime provenance is measured by the recorder process itself and is
        # therefore authoritative even when a caller supplied stale or empty
        # placeholders in its configuration snapshot.
        self._session_metadata.update(
            {
                "python_version": platform.python_version(),
                "opencv_version": cv2.__version__,
                "operating_system": platform.platform(),
            }
        )
        self._automatic_graph_summary = {
            "status": "pending",
            "automatic_heatmap_summary_status": "pending",
            "automatic_temporal_line_status": "pending",
            "automatic_spatial_profile_status": "pending",
            "trial_mean_contact_mean_delta_v_status": "pending",
            "generated_files": [],
        }
        self._recording_started_ns = self._monotonic_ns()
        self._last_disk_check_ns = self._recording_started_ns

        initialization_stage = "application_log"
        try:
            self._log_handle = (session_directory / "application.log").open(
                "x", encoding="utf-8", newline="\n", buffering=1
            )
            initialization_stage = "video_writer"
            self._open_video_writer()
            self._open_auxiliary_video_writers()
            initialization_stage = "frame_feature_csv"
            self._frame_writer = self._csv_writer_factory(
                session_directory / "frame_features.csv.partial",
                FRAME_FEATURE_COLUMNS,
                flush_interval_s=self.config.csv_flush_interval_s,
                flush_row_count=self.config.csv_flush_row_count,
                allow_existing=False,
            )
            initialization_stage = "loadcell_csv"
            self._loadcell_writer = self._csv_writer_factory(
                session_directory / "loadcell_raw.csv.partial",
                LOADCELL_RAW_COLUMNS,
                flush_interval_s=self.config.csv_flush_interval_s,
                flush_row_count=self.config.csv_flush_row_count,
                allow_existing=False,
            )
            initialization_stage = "session_artifact_validation"
            supplied_artifacts = (
                camera_settings is not None,
                roi_layout is not None,
                baseline is not None,
            )
            if any(supplied_artifacts) and not all(supplied_artifacts):
                raise RecorderPreflightError(
                    "camera_settings, roi_layout, and baseline must be supplied together"
                )
            if all(supplied_artifacts):
                initialization_stage = "immutable_session_artifacts"
                self.write_session_artifacts(
                    camera_settings=camera_settings,
                    roi_layout=roi_layout,
                    baseline=baseline,
                )
            self.lifecycle = RecordingLifecycle.RECORDING
            self._session_status = "recording"
            initialization_stage = "session_metadata"
            self._write_session_config()
            self._write_status()
            self._log("INFO", "recording_started", "Recording outputs opened")
        except Exception as exc:
            self.lifecycle = RecordingLifecycle.ERROR
            self._session_status = "partial"
            error_code = (
                ErrorCode.VIDEO_WRITER_FAILED
                if initialization_stage == "video_writer"
                else ErrorCode.DISK_WRITE_FAILED
            )
            self._append_error(
                error_code, f"{initialization_stage}: {exc}"
            )
            self._close_owned_handles()
            self._write_status_best_effort()
            raise RecorderPreflightError(
                f"could not initialize session recorder: {exc}"
            ) from exc
        return session_directory

    def write_session_artifacts(
        self,
        *,
        camera_settings: Mapping[str, object],
        roi_layout: ROILayout,
        baseline: BaselineRecord,
    ) -> Mapping[str, Path]:
        """Write the required immutable provenance bundle under recorder ownership.

        This method may be called by ``start`` or immediately afterward, before
        any frame is accepted.  Acquisition/simulation runners pass values to
        this method but never open competing handles in the session directory.
        """

        if self.session_directory is None or self._calibration is None:
            raise RecorderStateError("session paths and calibration must exist first")
        if self._accepted_frame_count or self._loadcell_sample_count:
            raise RecorderStateError(
                "required session artifacts must be written before measurements"
            )
        if self._required_artifacts_written:
            raise RecorderStateError("required session artifacts were already written")
        if roi_layout.roi_layout_id != baseline.roi_layout_id:
            raise RecorderPreflightError(
                "ROI layout identifier does not match the saved baseline"
            )

        directory = self.session_directory
        paths = {
            "camera_settings": directory / "camera_settings.json",
            "roi_layout": directory / "roi_layout.json",
            "loadcell_calibration": directory / "loadcell_calibration.json",
            "baseline_summary": directory / "baseline_summary.json",
            "baseline_data": directory / "baseline_data.npz",
            "data_dictionary": directory / "data_dictionary.csv",
            "spatial_graphs": directory / "spatial_graphs",
        }
        collisions = tuple(path for path in paths.values() if path.exists())
        if collisions:
            raise RecorderPreflightError(
                f"refusing to overwrite existing session artifacts: {collisions}"
            )

        atomic_write_json(paths["camera_settings"], dict(camera_settings))
        save_roi_layout(paths["roi_layout"], roi_layout)
        if isinstance(self._calibration, LoadCellCalibration):
            atomic_write_json(
                paths["loadcell_calibration"], self._calibration.to_dict()
            )
        else:
            atomic_write_json(
                paths["loadcell_calibration"], dict(self._calibration)
            )
        save_baseline(
            baseline, paths["baseline_summary"], paths["baseline_data"]
        )
        write_data_dictionary(paths["data_dictionary"])
        paths["spatial_graphs"].mkdir(parents=False, exist_ok=False)
        self._artifact_paths = paths
        self._required_artifacts_written = True
        return dict(paths)

    def record_frame(self, processed: ProcessedFrame) -> None:
        """Write the exact original BGR frame and its one complete feature row."""

        self._require_recording()
        expected_id = self._accepted_frame_count
        if processed.capture_frame_id != expected_id:
            self._fatal_partial(
                ErrorCode.INTERNAL_ERROR,
                f"non-sequential capture_frame_id: expected {expected_id}, "
                f"received {processed.capture_frame_id}",
            )
            raise RecorderIntegrityError("recording frame IDs must be sequential from zero")
        row_id = processed.feature_row.get("capture_frame_id")
        if row_id != processed.capture_frame_id:
            self._fatal_partial(
                ErrorCode.INTERNAL_ERROR,
                "feature-row capture_frame_id does not match ProcessedFrame identity",
            )
            raise RecorderIntegrityError("feature row and frame identity differ")
        frame = np.asarray(processed.original_bgr)
        width, height = self._frame_size
        if (
            frame.dtype != np.uint8
            or frame.ndim != 3
            or frame.shape != (height, width, 3)
        ):
            self._fatal_partial(
                ErrorCode.INTERNAL_ERROR,
                f"invalid original frame shape/dtype: {frame.shape}/{frame.dtype}",
            )
            raise RecorderIntegrityError("original frame does not match video mode")
        if self._video_writer is None or self._frame_writer is None:
            raise RecorderStateError("recording handles are not open")

        try:
            write_result = self._video_writer.write(
                frame if frame.flags.c_contiguous else np.ascontiguousarray(frame)
            )
            if write_result is False:
                raise OSError("video writer rejected the frame")
            self._video_write_count += 1
            for stream_name in self._auxiliary_streams:
                auxiliary = processed.video_streams.get(stream_name)
                if auxiliary is None:
                    raise OSError(
                        f"requested auxiliary stream {stream_name} has no frame"
                    )
                auxiliary_frame = np.asarray(auxiliary)
                if (
                    auxiliary_frame.dtype != np.uint8
                    or auxiliary_frame.shape != (height, width, 3)
                ):
                    raise OSError(
                        f"auxiliary stream {stream_name} has invalid frame shape/dtype"
                    )
                writer = self._auxiliary_video_writers[stream_name]
                result = writer.write(
                    auxiliary_frame
                    if auxiliary_frame.flags.c_contiguous
                    else np.ascontiguousarray(auxiliary_frame)
                )
                if result is False:
                    raise OSError(f"{stream_name} video writer rejected the frame")
                self._auxiliary_write_counts[stream_name] += 1
        except Exception as exc:
            self._fatal_partial(ErrorCode.VIDEO_WRITER_FAILED, str(exc))
            raise RecorderIntegrityError(
                f"video writer could not preserve the accepted frame: {exc}"
            ) from exc

        try:
            self._frame_writer.write_row(processed.feature_row)
            self._feature_row_count += 1
            self._accepted_frame_count += 1
            self._ingress_accepted_frame_count = max(
                self._ingress_accepted_frame_count, self._accepted_frame_count
            )
            self._maybe_write_status()
            self._periodic_disk_check()
        except DiskSpaceLowError:
            raise
        except Exception as exc:
            self._fatal_partial(ErrorCode.DISK_WRITE_FAILED, str(exc))
            raise RecorderIntegrityError(
                f"could not preserve frame/video/CSV identity: {exc}"
            ) from exc

    def record_loadcell_sample(self, sample: LoadCellSample) -> None:
        """Incrementally persist one original physical/simulated HX711 sample."""

        self._require_recording()
        if self._loadcell_writer is None:
            raise RecorderStateError("load-cell writer is not open")
        row = loadcell_sample_to_row(sample)
        try:
            self._loadcell_writer.write_row(row)
            self._loadcell_sample_count += 1
            self._ingress_accepted_loadcell_sample_count = max(
                self._ingress_accepted_loadcell_sample_count,
                self._loadcell_sample_count,
            )
            self._maybe_write_status()
            self._periodic_disk_check()
        except DiskSpaceLowError:
            raise
        except Exception as exc:
            self._fatal_partial(ErrorCode.DISK_WRITE_FAILED, str(exc))
            raise SessionRecorderError(f"could not save load-cell sample: {exc}") from exc

    def finalize(self) -> RecordingCompletion:
        """Close raw handles, validate video, build master CSV, and finalize atomically."""

        if self.lifecycle is RecordingLifecycle.COMPLETE and self._completion is not None:
            return self._completion
        self._require_recording()
        if self.session_directory is None or self._partial_video_path is None:
            raise RecorderStateError("session paths are unavailable")
        if self._calibration is None:
            raise RecorderStateError("load-cell calibration is unavailable")
        if not self._required_artifacts_written:
            self._fatal_partial(
                ErrorCode.FINALIZATION_FAILED,
                "required immutable session artifacts were not written",
            )
            raise RecorderIntegrityError(
                "cannot complete a session without all required provenance artifacts"
            )

        self.lifecycle = RecordingLifecycle.FINALIZING
        self._session_status = "finalizing"
        self._write_status()
        self._log("INFO", "finalizing", "Closing raw files and validating outputs")
        close_errors = self._close_data_and_video()
        if close_errors:
            message = "; ".join(close_errors)
            self._fatal_after_close(ErrorCode.DISK_WRITE_FAILED, message)
            raise SessionRecorderError(message)

        try:
            physical_video_count = self._count_video_frames(self._partial_video_path)
            if physical_video_count != self._accepted_frame_count:
                raise RecorderIntegrityError(
                    f"video contains {physical_video_count} frames but "
                    f"{self._accepted_frame_count} feature rows were accepted"
                )
            if self._feature_row_count != self._accepted_frame_count:
                raise RecorderIntegrityError(
                    "feature-row count does not equal accepted-frame count"
                )
            for stream_name, partial_path in self._auxiliary_partial_paths.items():
                count = self._count_video_frames(partial_path)
                if count != self._accepted_frame_count:
                    raise RecorderIntegrityError(
                        f"{stream_name} video contains {count} frames but "
                        f"{self._accepted_frame_count} were accepted"
                    )

            exporter_summary = self._master_finalizer(
                session_dir=self.session_directory,
                calibration=self._calibration,
                max_sync_gap_ms=self._max_sync_gap_ms,
                contact_threshold_N=self._contact_threshold_N,
            )
            self._validate_exporter_summary(exporter_summary)
            graph_summary = self._finalize_automatic_graphs()
            if self._final_video_path is None:
                raise RecorderStateError("final video path was not selected")
            if self._final_video_path.exists():
                raise RecorderIntegrityError(
                    f"refusing to overwrite final video: {self._final_video_path}"
                )
            os.replace(self._partial_video_path, self._final_video_path)
            for stream_name, partial_path in self._auxiliary_partial_paths.items():
                final_path = self._auxiliary_final_paths[stream_name]
                if final_path.exists():
                    raise RecorderIntegrityError(
                        f"refusing to overwrite auxiliary video: {final_path}"
                    )
                os.replace(partial_path, final_path)
            self._video_write_count = physical_video_count
            self.lifecycle = RecordingLifecycle.COMPLETE
            self._session_status = "complete"
            self._completion = RecordingCompletion(
                session_directory=self.session_directory,
                video_path=self._final_video_path,
                video_codec=self._video_codec,
                accepted_frame_count=self._accepted_frame_count,
                video_frame_count=physical_video_count,
                feature_row_count=self._feature_row_count,
                loadcell_sample_count=self._loadcell_sample_count,
                exporter_summary=exporter_summary,
                graph_summary=graph_summary,
            )
            self._write_session_config()
            self._write_status()
            self._log("INFO", "recording_complete", "Session finalized successfully")
            self._close_log()
            return self._completion
        except Exception as exc:
            self._fatal_after_close(ErrorCode.FINALIZATION_FAILED, str(exc))
            if isinstance(exc, SessionRecorderError):
                raise
            raise SessionRecorderError(f"session finalization failed: {exc}") from exc

    def abort(
        self,
        error_code: ErrorCode = ErrorCode.SHUTDOWN_REQUESTED,
        message: str = "Recording stopped before successful finalization",
    ) -> None:
        """Safely close and retain every available partial output."""

        if self.lifecycle in {RecordingLifecycle.COMPLETE, RecordingLifecycle.ERROR}:
            return
        if self.lifecycle is RecordingLifecycle.IDLE:
            self.lifecycle = RecordingLifecycle.ERROR
            self._session_status = "partial"
            return
        self._fatal_partial(error_code, message)

    def close(self) -> None:
        """Idempotently preserve a partial session unless already complete."""

        if self.lifecycle in {RecordingLifecycle.IDLE, RecordingLifecycle.COMPLETE}:
            self._close_log()
            return
        if self.lifecycle is not RecordingLifecycle.ERROR:
            self.abort()

    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc is not None and self.lifecycle is RecordingLifecycle.RECORDING:
            self.abort(ErrorCode.INTERNAL_ERROR, str(exc))
        else:
            self.close()

    def _validated_directory_name(self, requested: str | None) -> str:
        if requested is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            return f"{timestamp}_{uuid.uuid4().hex[:8]}"
        cleaned = requested.strip()
        candidate = Path(cleaned)
        if (
            not cleaned
            or candidate.is_absolute()
            or candidate.name != cleaned
            or cleaned in {".", ".."}
        ):
            raise RecorderPreflightError(
                "session_directory_name must be one non-empty path component"
            )
        return cleaned

    def _open_video_writer(self) -> None:
        if self.session_directory is None:
            raise RecorderStateError("session directory is unavailable")
        width, height = self._frame_size
        attempts = (
            (
                self.config.primary_video_codec,
                self.session_directory / "session_video.partial.mp4",
                self.session_directory / "session_video.mp4",
            ),
            (
                self.config.fallback_video_codec,
                self.session_directory / "session_video.partial.avi",
                self.session_directory / "session_video.avi",
            ),
        )
        failures: list[str] = []
        for codec, partial_path, final_path in attempts:
            writer: VideoWriterLike | None = None
            try:
                fourcc = cv2.VideoWriter_fourcc(*codec)
                writer = self._video_writer_factory(
                    str(partial_path), fourcc, self._output_fps, (width, height)
                )
                if writer is not None and writer.isOpened():
                    self._video_writer = writer
                    self._partial_video_path = partial_path
                    self._final_video_path = final_path
                    self._video_codec = codec
                    return
                failures.append(f"{codec} writer did not open")
            except Exception as exc:
                failures.append(f"{codec}: {exc}")
            finally:
                if writer is not None and writer is not self._video_writer:
                    try:
                        writer.release()
                    except Exception as exc:
                        failures.append(f"{codec} failed-writer release: {exc}")
                if writer is not self._video_writer and partial_path.exists():
                    try:
                        partial_path.unlink()
                    except OSError as exc:
                        failures.append(f"{codec} failed-artifact cleanup: {exc}")
        raise RecorderPreflightError(
            "video writer preflight failed for MP4 and AVI: " + "; ".join(failures)
        )

    def _open_auxiliary_video_writers(self) -> None:
        if not self._auxiliary_streams:
            return
        if self.session_directory is None or not self._video_codec or self._partial_video_path is None:
            raise RecorderStateError("primary video writer must open before auxiliary streams")
        width, height = self._frame_size
        suffix = self._partial_video_path.suffix
        fourcc = cv2.VideoWriter_fourcc(*self._video_codec)
        for stream_name in self._auxiliary_streams:
            partial = self.session_directory / f"session_{stream_name}.partial{suffix}"
            final = self.session_directory / f"session_{stream_name}{suffix}"
            writer = self._video_writer_factory(
                str(partial), fourcc, self._output_fps, (width, height)
            )
            if writer is None or not writer.isOpened():
                if writer is not None:
                    writer.release()
                raise RecorderPreflightError(
                    f"video writer did not open for requested {stream_name} stream"
                )
            self._auxiliary_video_writers[stream_name] = writer
            self._auxiliary_partial_paths[stream_name] = partial
            self._auxiliary_final_paths[stream_name] = final
            self._auxiliary_write_counts[stream_name] = 0

    def _write_session_config(self) -> None:
        if self.session_directory is None:
            return
        payload = dict(self._session_metadata)
        payload.update(
            {
                "video_filename": (
                    self._final_video_path.name if self._final_video_path else ""
                ),
                "video_partial_filename": (
                    self._partial_video_path.name if self._partial_video_path else ""
                ),
                "video_codec": self._video_codec,
                "output_fps": self._output_fps,
                "width": self._frame_size[0],
                "height": self._frame_size[1],
                "max_sync_gap_ms": self._max_sync_gap_ms,
                "contact_threshold_N": self._contact_threshold_N,
                "automatic_graph_summary": self._automatic_graph_summary,
                "auxiliary_video_files": {
                    name: path.name for name, path in self._auxiliary_final_paths.items()
                },
            }
        )
        self._atomic_json(self.session_directory / "session_config.json", payload)

    def _status_payload(self) -> dict[str, object]:
        return {
            "lifecycle": self.lifecycle.value,
            "session_status": self._session_status,
            "complete": self.lifecycle is RecordingLifecycle.COMPLETE,
            "partial": self._session_status == "partial",
            "session_directory": (
                str(self.session_directory) if self.session_directory else ""
            ),
            "video_filename": (
                self._final_video_path.name if self._final_video_path else ""
            ),
            "video_partial_filename": (
                self._partial_video_path.name if self._partial_video_path else ""
            ),
            "video_codec": self._video_codec,
            "output_fps": self._output_fps,
            "accepted_frame_count": self._accepted_frame_count,
            "ingress_accepted_frame_count": self._ingress_accepted_frame_count,
            "ingress_accepted_loadcell_sample_count": (
                self._ingress_accepted_loadcell_sample_count
            ),
            "feature_row_count": self._feature_row_count,
            "video_frame_count": self._video_write_count,
            "auxiliary_video_frame_counts": dict(self._auxiliary_write_counts),
            "loadcell_sample_count": self._loadcell_sample_count,
            "recording_started_monotonic_ns": self._recording_started_ns,
            "updated_wall_clock_iso": datetime.now(timezone.utc).isoformat(),
            "errors": self._errors,
            "required_artifacts_written": self._required_artifacts_written,
            "artifact_paths": {
                name: str(path) for name, path in self._artifact_paths.items()
            },
            "manual_spatial_graph_count": self._manual_spatial_graph_count(),
            "manual_line_graph_count": self._manual_line_graph_count(),
            "automatic_graph_summary": self._automatic_graph_summary,
            "automatic_heatmap_summary_status": self._automatic_graph_summary.get(
                "automatic_heatmap_summary_status", "not_started"
            ),
            "automatic_temporal_line_status": self._automatic_graph_summary.get(
                "automatic_temporal_line_status", "not_started"
            ),
            "automatic_spatial_profile_status": self._automatic_graph_summary.get(
                "automatic_spatial_profile_status", "not_started"
            ),
            "trial_mean_contact_mean_delta_v_status": (
                self._automatic_graph_summary.get(
                    "trial_mean_contact_mean_delta_v_status", "not_evaluated"
                )
            ),
        }

    def _write_status(self, *, status_clock_ns: int | None = None) -> None:
        if self.session_directory is None:
            return
        self._atomic_json(
            self.session_directory / "session_status.json", self._status_payload()
        )
        self._last_status_write_ns = (
            self._status_clock_ns()
            if status_clock_ns is None
            else int(status_clock_ns)
        )

    def _maybe_write_status(self) -> None:
        """Persist routine count progress at the configured 2 Hz default ceiling."""

        now_ns = self._status_clock_ns()
        if (
            self._last_status_write_ns is None
            or now_ns < self._last_status_write_ns
            or now_ns - self._last_status_write_ns >= self._status_write_interval_ns
        ):
            self._write_status(status_clock_ns=now_ns)

    def _write_status_best_effort(self) -> None:
        try:
            self._write_status()
        except Exception:
            return

    def _write_session_config_best_effort(self) -> None:
        try:
            self._write_session_config()
        except Exception:
            return

    def _finalize_automatic_graphs(self) -> TrialGraphSummary:
        """Render all required trial graphs from the finalized master CSV."""

        if self.session_directory is None:
            raise RecorderStateError("session directory is unavailable")
        graph_directory = self._artifact_paths.get(
            "spatial_graphs", self.session_directory / "spatial_graphs"
        )
        master_path = self.session_directory / "master_synchronized.csv"
        try:
            with master_path.open("r", encoding="utf-8", newline="") as handle:
                rows = tuple(dict(row) for row in csv.DictReader(handle))
        except OSError as exc:
            self._automatic_graph_summary = {
                "status": "failed",
                "automatic_heatmap_summary_status": "failed",
                "automatic_temporal_line_status": "failed",
                "automatic_spatial_profile_status": "failed",
                "trial_mean_contact_mean_delta_v_status": "not_evaluated",
                "generated_files": [],
                "error": f"master CSV could not be read: {exc}",
            }
            raise RecorderIntegrityError(
                f"automatic graph source could not be read: {master_path}: {exc}"
            ) from exc

        labels = self._session_metadata.get("trial_labels", {})
        label_mapping = labels if isinstance(labels, Mapping) else {}
        context = GraphContext(
            session_id=str(
                self._session_metadata.get("session_id")
                or label_mapping.get("session_id", "")
            ),
            trial_id=str(
                self._session_metadata.get("trial_id")
                or label_mapping.get("trial_id", "")
            ),
            baseline_id=str(self._session_metadata.get("baseline_id", "")),
            roi_layout_id=str(self._session_metadata.get("roi_layout_id", "")),
        )
        service: GraphExportService | None = None
        try:
            service = self._graph_export_service_factory(graph_directory)
            graph_summary = service.finalize_trial(rows, context=context).result()
            self._automatic_graph_summary = self._graph_status_payload(graph_summary)
            return graph_summary
        except Exception as exc:
            generated = self._generated_graph_files(graph_directory)
            self._automatic_graph_summary = {
                "status": "failed",
                "automatic_heatmap_summary_status": "failed",
                "automatic_temporal_line_status": "failed",
                "automatic_spatial_profile_status": "failed",
                "trial_mean_contact_mean_delta_v_status": "not_evaluated",
                "generated_files": generated,
                "error": str(exc),
            }
            raise RecorderIntegrityError(
                f"automatic trial graph finalization failed: {exc}"
            ) from exc
        finally:
            if service is not None:
                service.shutdown(wait=True)

    def _graph_status_payload(
        self, summary: TrialGraphSummary
    ) -> dict[str, object]:
        artifacts: dict[str, dict[str, object]] = {}
        generated_files: list[str] = []
        for artifact in summary.artifacts:
            paths = {
                "png": artifact.png_path,
                "csv": artifact.csv_path,
                "json": artifact.json_path,
            }
            serialized_paths: dict[str, str] = {}
            for label, path in paths.items():
                if path is None:
                    serialized_paths[label] = ""
                    continue
                relative = self._relative_session_path(path)
                serialized_paths[label] = relative
                generated_files.append(relative)
            artifacts[artifact.kind] = {
                "available": bool(artifact.available),
                "frame_id": artifact.frame_id,
                **serialized_paths,
            }

        mean_contact_status = (
            "available"
            if summary.mean_contact_available
            else "unavailable_no_valid_contact_frames"
        )
        return {
            "status": "complete",
            "automatic_heatmap_summary_status": (
                "complete"
                if summary.mean_contact_available
                else "complete_with_mean_contact_unavailable"
            ),
            "automatic_temporal_line_status": "complete",
            "automatic_spatial_profile_status": "complete",
            "trial_mean_contact_mean_delta_v_status": mean_contact_status,
            "peak_frame_id": summary.peak_frame_id,
            "generated_files": sorted(generated_files),
            "artifacts": artifacts,
        }

    def _relative_session_path(self, path: Path) -> str:
        if self.session_directory is None:
            return str(path)
        try:
            return path.resolve().relative_to(self.session_directory.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    def _generated_graph_files(self, graph_directory: Path) -> list[str]:
        if not graph_directory.is_dir():
            return []
        return sorted(
            self._relative_session_path(path)
            for path in graph_directory.iterdir()
            if path.is_file()
        )

    def _manual_spatial_graph_count(self) -> int:
        graph_directory = self._artifact_paths.get("spatial_graphs")
        if graph_directory is None or not graph_directory.is_dir():
            return 0
        return sum(1 for _path in graph_directory.glob("snapshot_*.json"))

    def _manual_line_graph_count(self) -> int:
        graph_directory = self._artifact_paths.get("spatial_graphs")
        if graph_directory is None or not graph_directory.is_dir():
            return 0
        return sum(1 for _path in graph_directory.glob("temporal_lines_*.json")) + sum(
            1 for _path in graph_directory.glob("spatial_profile_*.json")
        )

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                _json_safe(payload),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _log(self, severity: str, event: str, message: str) -> None:
        if self._log_handle is None:
            return
        session_id = self._session_metadata.get("session_id", "")
        if not session_id:
            trial_labels = self._session_metadata.get("trial_labels", {})
            if isinstance(trial_labels, Mapping):
                session_id = trial_labels.get("session_id", "")
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": severity,
            "component": "session_recorder",
            "session_id": str(session_id),
            "event": event,
            "message": message,
        }
        self._log_handle.write(
            json.dumps(_json_safe(entry), sort_keys=True, allow_nan=False) + "\n"
        )
        self._log_handle.flush()

    def _append_error(self, code: ErrorCode, message: str) -> None:
        entry: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error_code": code.value,
            "message": message,
        }
        self._errors.append(entry)
        self._log("ERROR", code.value, message)

    def _require_recording(self) -> None:
        if self.lifecycle is not RecordingLifecycle.RECORDING:
            raise RecorderStateError(
                f"operation requires RECORDING; current lifecycle is {self.lifecycle.value}"
            )

    def _periodic_disk_check(self) -> None:
        now_ns = self._monotonic_ns()
        frame_due = (
            self._accepted_frame_count > 0
            and self._accepted_frame_count % self._disk_check_interval_frames == 0
        )
        time_due = now_ns - self._last_disk_check_ns >= self._disk_check_interval_ns
        if not frame_due and not time_due:
            return
        self._last_disk_check_ns = now_ns
        if self.session_directory is None:
            return
        try:
            self._require_free_space(
                self.session_directory,
                self.config.disk_safety_free_mb,
                preflight=False,
            )
        except DiskSpaceLowError as exc:
            self._fatal_partial(ErrorCode.DISK_SPACE_LOW, str(exc))
            raise

    def _require_free_space(
        self, path: Path, required_mb: int, *, preflight: bool
    ) -> None:
        free = _free_bytes(self._disk_usage_provider, path)
        required_bytes = int(required_mb) * MEBIBYTE
        if free < required_bytes:
            message = (
                f"free disk space {free / MEBIBYTE:.1f} MiB is below "
                f"required {required_mb} MiB"
            )
            if preflight:
                raise RecorderPreflightError(message)
            raise DiskSpaceLowError(message)

    def _close_data_and_video(self) -> list[str]:
        errors: list[str] = []
        for label, writer in (
            ("frame_features", self._frame_writer),
            ("loadcell_raw", self._loadcell_writer),
        ):
            if writer is None:
                continue
            try:
                writer.flush()
                writer.close()
            except Exception as exc:
                errors.append(f"{label} close failed: {exc}")
        self._frame_writer = None
        self._loadcell_writer = None
        if self._video_writer is not None:
            try:
                self._video_writer.release()
            except Exception as exc:
                errors.append(f"video close failed: {exc}")
        self._video_writer = None
        for stream_name, writer in tuple(self._auxiliary_video_writers.items()):
            try:
                writer.release()
            except Exception as exc:
                errors.append(f"{stream_name} video close failed: {exc}")
        self._auxiliary_video_writers.clear()
        return errors

    def _close_owned_handles(self) -> None:
        self._close_data_and_video()
        self._close_log()

    def _close_log(self) -> None:
        if self._log_handle is None:
            return
        try:
            self._log_handle.flush()
            self._log_handle.close()
        finally:
            self._log_handle = None

    def _fatal_partial(self, code: ErrorCode, message: str) -> None:
        self._append_error(code, message)
        self._close_data_and_video()
        self.lifecycle = RecordingLifecycle.ERROR
        self._session_status = "partial"
        self._write_session_config_best_effort()
        self._write_status_best_effort()
        self._close_log()

    def _fatal_after_close(self, code: ErrorCode, message: str) -> None:
        self._append_error(code, message)
        self.lifecycle = RecordingLifecycle.ERROR
        self._session_status = "partial"
        self._write_session_config_best_effort()
        self._write_status_best_effort()
        self._close_log()

    def _count_video_frames(self, path: Path) -> int:
        capture = self._video_capture_factory(str(path))
        try:
            if capture is None or not capture.isOpened():
                raise RecorderIntegrityError(f"could not reopen recorded video: {path}")
            count = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                if frame is None:
                    raise RecorderIntegrityError("video decoder returned an empty frame")
                count += 1
            return count
        finally:
            if capture is not None:
                capture.release()

    def _validate_exporter_summary(self, summary: object) -> None:
        for field_name, expected in (
            ("frame_count", self._accepted_frame_count),
            ("master_row_count", self._accepted_frame_count),
            ("loadcell_sample_count", self._loadcell_sample_count),
        ):
            if isinstance(summary, Mapping):
                actual = summary.get(field_name)
            else:
                actual = getattr(summary, field_name, None)
            if actual is not None and int(actual) != expected:
                raise RecorderIntegrityError(
                    f"exporter {field_name}={actual} does not match recorder count {expected}"
                )


__all__ = [
    "DiskSpaceLowError",
    "RecorderIntegrityError",
    "RecorderPreflightError",
    "RecorderSnapshot",
    "RecorderStateError",
    "RecordingCompletion",
    "RecordingPreflightResult",
    "SessionRecorder",
    "SessionRecorderError",
    "preflight_recording_output",
]
