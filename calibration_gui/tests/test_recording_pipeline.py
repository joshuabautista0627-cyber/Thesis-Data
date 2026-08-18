"""Real-video and failure-path tests for the single-owner session recorder."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import platform

import cv2
import numpy as np
import pytest

from core.models import (
    BaselineRecord,
    ErrorCode,
    LoadCellCalibration,
    LoadCellSample,
    ROI,
    ROIBaseline,
    RecordingConfig,
    RecordingLifecycle,
)
from data.schemas import FRAME_FEATURE_COLUMNS
from processing.pipeline import ProcessedFrame
from processing.roi_manager import ROILayout
from services.session_recorder import (
    DiskSpaceLowError,
    RecorderIntegrityError,
    RecorderPreflightError,
    SessionRecorder,
    preflight_recording_output,
)


WIDTH = 64
HEIGHT = 48
BASE_NS = 1_000_000_000


def recording_config(tmp_path: Path) -> RecordingConfig:
    return RecordingConfig(
        output_directory=str(tmp_path / "output"),
        preview_queue_size=1,
        recording_queue_size=8,
        csv_flush_interval_s=0.01,
        csv_flush_row_count=1,
        primary_video_codec="mp4v",
        fallback_video_codec="MJPG",
        minimum_preflight_free_mb=1,
        disk_safety_free_mb=1,
    )


def calibration() -> LoadCellCalibration:
    return LoadCellCalibration(
        calibration_id="cal-test",
        counts_per_gram=100.0,
        tare_raw=1_000.0,
        known_mass_g=200.0,
        unloaded_mean_raw=1_000.0,
        unloaded_std_raw=2.0,
        unloaded_sample_count=20,
        loaded_mean_raw=21_000.0,
        loaded_std_raw=3.0,
        loaded_sample_count=20,
        calibration_span_counts=20_000.0,
        calibration_noise_counts=3.0,
        calibration_snr=20_000.0 / 3.0,
        loaded_window_mean_gf=200.0,
        loaded_window_std_gf=0.03,
        loaded_window_cv_percent=0.015,
        minimum_sample_count=20,
        minimum_calibration_snr=10.0,
        maximum_loaded_window_cv_percent=2.0,
        minimum_abs_counts_per_gram=1.0e-9,
        quality_passed=True,
        serial_port="SIMULATED",
        firmware_identity="HX711_NANO_SIM/1.0.0",
        protocol_version="1.0",
        firmware_version="sim-1.0.0",
        calibration_timestamp_iso="2026-08-03T00:00:00+00:00",
        tare_timestamp_iso="2026-08-03T00:00:00+00:00",
    )


def roi_layout_and_baseline() -> tuple[ROILayout, BaselineRecord]:
    rois = tuple(
        ROI(
            roi_id=roi_id,
            x=((roi_id - 1) % 3) * 20,
            y=((roi_id - 1) // 3) * 14,
            width=10,
            height=10,
        )
        for roi_id in range(1, 10)
    )
    layout = ROILayout.create(rois, WIDTH, HEIGHT)
    roi_baselines = tuple(
        ROIBaseline(
            roi_id=roi_id,
            median_v_image=np.full((10, 10), 50, dtype=np.float32),
            mean_v_image=np.full((10, 10), 50, dtype=np.float32),
            circular_mean_h=30.0,
            mean_s=100.0,
            mean_v=50.0,
            median_v=50.0,
            std_v=0.0,
            valid_frame_count=20,
            capture_timestamp_iso="2026-08-03T00:00:00+00:00",
        )
        for roi_id in range(1, 10)
    )
    baseline = BaselineRecord(
        baseline_id="baseline-test",
        roi_layout_id=layout.roi_layout_id,
        roi_baselines=roi_baselines,
        capture_timestamp_iso="2026-08-03T00:00:00+00:00",
        camera_settings={"simulation_mode": True},
    )
    return layout, baseline


def original_frame(frame_id: int) -> np.ndarray:
    """Create deterministic unannotated content with no overlays or text."""

    frame = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
    frame[...] = (25 + frame_id * 20, 70 + frame_id * 15, 130 - frame_id * 10)
    frame[:, : WIDTH // 2, 0] += 20
    frame[HEIGHT // 2 :, :, 1] += 15
    return frame


def feature_row(frame_id: int, *, feature_failure: bool = False) -> dict[str, object]:
    row: dict[str, object] = {column: math.nan for column in FRAME_FEATURE_COLUMNS}
    row.update(
        {
            "session_id": "session-test",
            "trial_id": "trial-001",
            "sensing_skin_id": "skin-01",
            "specimen_or_participant_id": "",
            "target_roi_ground_truth": 1,
            "trial_interaction_class": "Press",
            "trial_force_class": "",
            "press_number": 1,
            "notes": "",
            "trial_label_scope": "trial",
            "contact_threshold_N": 0.05,
            "capture_frame_id": frame_id,
            "host_monotonic_ns": BASE_NS + frame_id * 100_000_000,
            "elapsed_time_s": frame_id * 0.1,
            "wall_clock_iso": f"2026-08-03T00:00:0{frame_id}+00:00",
            "requested_fps": 10.0,
            "actual_fps": 10.0,
            "width": WIDTH,
            "height": HEIGHT,
            "camera_backend": "Video File (Simulation)",
            "camera_device_index": -1,
            "frame_valid": True,
            "baseline_valid": True,
            "saturation_warning": False,
            "error_code": (
                ErrorCode.FEATURE_EXTRACTION_FAILED.value
                if feature_failure
                else ErrorCode.NONE.value
            ),
            "schema_version": "1.0.0",
            "application_version": "1.0.0",
            "arduino_protocol_version": "sim-1",
            "arduino_firmware_version": "sim-1",
            "python_version": "test",
            "opencv_version": cv2.__version__,
            "operating_system": "test",
            "calibration_id": "cal-test",
            "baseline_id": "baseline-test",
            "roi_layout_id": "roi-layout-test",
        }
    )
    for roi_id in range(1, 10):
        prefix = f"roi{roi_id}_"
        row.update(
            {
                f"{prefix}x": ((roi_id - 1) % 3) * 20,
                f"{prefix}y": ((roi_id - 1) // 3) * 14,
                f"{prefix}width": 10,
                f"{prefix}height": 10,
                f"{prefix}area": 100,
                f"{prefix}center_x": ((roi_id - 1) % 3) * 20 + 5,
                f"{prefix}center_y": ((roi_id - 1) // 3) * 14 + 5,
                f"{prefix}baseline_mean_v": 50.0,
            }
        )
        if not feature_failure:
            delta_mean = float((frame_id + 1) * roi_id)
            row.update(
                {
                    f"{prefix}mean_h": 30.0,
                    f"{prefix}mean_s": 100.0,
                    f"{prefix}mean_v": 50.0 + delta_mean,
                    f"{prefix}delta_v_mean": delta_mean,
                    f"{prefix}delta_v_max": delta_mean + 1.0,
                    f"{prefix}delta_v_sum": delta_mean * 100.0,
                    f"{prefix}delta_v_sum_per_pixel": delta_mean,
                    f"{prefix}active_fraction": 1.0,
                }
            )
    return row


def processed_frame(frame_id: int, *, feature_failure: bool = False) -> ProcessedFrame:
    return ProcessedFrame(
        capture_frame_id=frame_id,
        original_bgr=original_frame(frame_id),
        feature_row=feature_row(frame_id, feature_failure=feature_failure),
        optical_result=None,
    )


def load_sample(sample_id: int, host_ns: int) -> LoadCellSample:
    raw_adc = 1_000 + sample_id * 100
    force_gf = (raw_adc - 1_000) / 100.0
    return LoadCellSample(
        host_monotonic_ns=host_ns,
        arduino_sample_id=sample_id,
        arduino_micros=sample_id * 10_000,
        arduino_micros_unwrapped=sample_id * 10_000,
        raw_adc=raw_adc,
        force_gf=force_gf,
        force_N=force_gf * 0.00980665,
        device_session_id="sim-device-1",
        elapsed_time_s=(host_ns - BASE_NS) / 1_000_000_000.0,
        wall_clock_iso="2026-08-03T00:00:00+00:00",
    )


def start_recorder(
    tmp_path: Path,
    name: str = "session-test",
    *,
    auxiliary_video_streams: tuple[str, ...] = (),
    **kwargs,
) -> SessionRecorder:
    recorder = SessionRecorder(recording_config(tmp_path), **kwargs)
    layout, saved_baseline = roi_layout_and_baseline()
    recorder.start(
        name,
        frame_size=(WIDTH, HEIGHT),
        output_fps=10.0,
        calibration=calibration(),
        session_config={
            "session_id": "session-test",
            "trial_id": "trial-001",
            "simulation_mode": True,
        },
        camera_settings={
            "backend": "Video File (Simulation)",
            "width": WIDTH,
            "height": HEIGHT,
            "fps": 10.0,
        },
        roi_layout=layout,
        baseline=saved_baseline,
        max_sync_gap_ms=200.0,
        contact_threshold_N=0.05,
        auxiliary_video_streams=auxiliary_video_streams,
    )
    return recorder


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def decode_video(path: Path) -> list[np.ndarray]:
    capture = cv2.VideoCapture(str(path))
    assert capture.isOpened(), f"could not open test video {path}"
    frames: list[np.ndarray] = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    return frames


def test_clean_recording_is_one_to_one_and_video_is_unannotated_with_tolerance(
    tmp_path: Path,
) -> None:
    recorder = start_recorder(tmp_path)
    for sample_id, timestamp in enumerate(
        [BASE_NS - 50_000_000, BASE_NS + 50_000_000, BASE_NS + 150_000_000, BASE_NS + 250_000_000]
    ):
        recorder.record_loadcell_sample(load_sample(sample_id, timestamp))
    originals = [original_frame(index).copy() for index in range(3)]
    for index in range(3):
        recorder.record_frame(processed_frame(index))

    completion = recorder.finalize()
    recorder.close()  # clean close is idempotent and preserves final files
    session_dir = completion.session_directory
    decoded = decode_video(completion.video_path)
    feature_rows = read_csv_rows(session_dir / "frame_features.csv")
    master_rows = read_csv_rows(session_dir / "master_synchronized.csv")
    assert len(decoded) == len(feature_rows) == len(master_rows) == 3
    assert [int(row["capture_frame_id"]) for row in feature_rows] == [0, 1, 2]
    assert [int(row["capture_frame_id"]) for row in master_rows] == [0, 1, 2]
    for encoded, original in zip(decoded, originals, strict=True):
        # Lossy video codecs may shift values slightly, but overlays/boxes would
        # create a much larger localized error than this whole-frame tolerance.
        mean_absolute_error = np.mean(
            np.abs(encoded.astype(np.int16) - original.astype(np.int16))
        )
        assert mean_absolute_error < 8.0
    status = json.loads((session_dir / "session_status.json").read_text("utf-8"))
    config = json.loads((session_dir / "session_config.json").read_text("utf-8"))
    assert status["complete"] is True and status["partial"] is False
    assert status["accepted_frame_count"] == status["video_frame_count"] == 3
    assert config["video_filename"] == completion.video_path.name
    assert config["video_codec"] == completion.video_codec
    assert config["python_version"] == platform.python_version()
    assert config["opencv_version"] == cv2.__version__
    assert config["operating_system"] == platform.platform()
    assert not (session_dir / "frame_features.csv.partial").exists()
    assert not (session_dir / "loadcell_raw.csv.partial").exists()
    assert not any(session_dir.glob("session_video.partial.*"))
    required_artifacts = {
        "session_config.json",
        "camera_settings.json",
        "roi_layout.json",
        "loadcell_calibration.json",
        "baseline_summary.json",
        "baseline_data.npz",
        "data_dictionary.csv",
        "spatial_graphs",
    }
    assert required_artifacts <= {path.name for path in session_dir.iterdir()}
    assert (session_dir / "spatial_graphs").is_dir()
    expected_automatic_graphs = {
        "trial_peak_mean_delta_v.png",
        "trial_peak_mean_delta_v.csv",
        "trial_peak_mean_delta_v.json",
        "trial_mean_contact_mean_delta_v.json",
        "trial_integrated_delta_v.png",
        "trial_integrated_delta_v.csv",
        "trial_integrated_delta_v.json",
        "trial_roi_intensity_over_time.png",
        "trial_roi_intensity_over_time.csv",
        "trial_roi_intensity_over_time.json",
        "trial_peak_spatial_profile.png",
        "trial_peak_spatial_profile.csv",
        "trial_peak_spatial_profile.json",
    }
    graph_names = {
        path.name for path in (session_dir / "spatial_graphs").iterdir()
    }
    assert expected_automatic_graphs <= graph_names
    assert "trial_mean_contact_mean_delta_v.png" not in graph_names
    assert "trial_mean_contact_mean_delta_v.csv" not in graph_names
    mean_contact = json.loads(
        (session_dir / "spatial_graphs" / "trial_mean_contact_mean_delta_v.json")
        .read_text("utf-8")
    )
    assert mean_contact["available"] is False
    assert status["automatic_heatmap_summary_status"] == (
        "complete_with_mean_contact_unavailable"
    )
    assert status["automatic_temporal_line_status"] == "complete"
    assert status["automatic_spatial_profile_status"] == "complete"
    assert status["trial_mean_contact_mean_delta_v_status"] == (
        "unavailable_no_valid_contact_frames"
    )
    assert config["automatic_graph_summary"] == status["automatic_graph_summary"]
    assert completion.graph_summary.mean_contact_available is False


def test_recorder_runtime_provenance_is_authoritative_and_log_uses_nested_session_id(
    tmp_path: Path,
) -> None:
    recorder = SessionRecorder(recording_config(tmp_path))
    session_dir = recorder.start(
        "nested-session-id",
        frame_size=(WIDTH, HEIGHT),
        output_fps=10.0,
        calibration=calibration(),
        session_config={
            "trial_labels": {"session_id": "session-from-trial-labels"},
            "python_version": "stale-caller-value",
            "opencv_version": "stale-caller-value",
            "operating_system": "stale-caller-value",
        },
    )
    recorder.abort(ErrorCode.SHUTDOWN_REQUESTED, "intentional provenance test")

    config = json.loads((session_dir / "session_config.json").read_text("utf-8"))
    assert config["python_version"] == platform.python_version()
    assert config["opencv_version"] == cv2.__version__
    assert config["operating_system"] == platform.platform()
    entries = [
        json.loads(line)
        for line in (session_dir / "application.log").read_text("utf-8").splitlines()
    ]
    assert entries
    assert {entry["session_id"] for entry in entries} == {
        "session-from-trial-labels"
    }


def test_session_start_csv_failure_is_not_misclassified_as_video_failure(
    tmp_path: Path,
) -> None:
    def failing_csv_factory(*_args, **_kwargs):
        raise OSError("feature CSV path became unwritable")

    recorder = SessionRecorder(
        recording_config(tmp_path), csv_writer_factory=failing_csv_factory
    )
    with pytest.raises(RecorderPreflightError):
        recorder.start(
            "csv-start-failure",
            frame_size=(WIDTH, HEIGHT),
            output_fps=10.0,
            calibration=calibration(),
        )
    assert recorder.session_directory is not None
    status = json.loads(
        (recorder.session_directory / "session_status.json").read_text("utf-8")
    )
    assert status["errors"][-1]["error_code"] == ErrorCode.DISK_WRITE_FAILED.value
    assert "frame_feature_csv" in status["errors"][-1]["message"]


def test_session_artifact_validation_failure_reports_its_actual_stage(
    tmp_path: Path,
) -> None:
    recorder = SessionRecorder(recording_config(tmp_path))
    with pytest.raises(RecorderPreflightError):
        recorder.start(
            "artifact-validation-failure",
            frame_size=(WIDTH, HEIGHT),
            output_fps=10.0,
            calibration=calibration(),
            camera_settings={"simulation_mode": True},
        )
    assert recorder.session_directory is not None
    status = json.loads(
        (recorder.session_directory / "session_status.json").read_text("utf-8")
    )
    assert status["errors"][-1]["error_code"] == ErrorCode.DISK_WRITE_FAILED.value
    assert "session_artifact_validation" in status["errors"][-1]["message"]


def test_status_distinguishes_ingress_acceptance_from_persisted_counts(
    tmp_path: Path,
) -> None:
    recorder = start_recorder(tmp_path, "ingress-counts")
    recorder.set_ingress_counts(
        accepted_frame_count=3, accepted_loadcell_sample_count=4
    )
    recorder.record_frame(processed_frame(0))
    recorder.record_loadcell_sample(load_sample(0, BASE_NS))
    recorder.abort(ErrorCode.SHUTDOWN_REQUESTED, "intentional partial")
    status = json.loads(
        (recorder.session_directory / "session_status.json").read_text("utf-8")
    )
    assert status["ingress_accepted_frame_count"] == 3
    assert status["accepted_frame_count"] == 1
    assert status["ingress_accepted_loadcell_sample_count"] == 4
    assert status["loadcell_sample_count"] == 1


def test_feature_failure_still_writes_video_feature_and_master_rows(tmp_path: Path) -> None:
    recorder = start_recorder(tmp_path, "feature-failure")
    recorder.record_loadcell_sample(load_sample(0, BASE_NS - 10_000_000))
    recorder.record_loadcell_sample(load_sample(1, BASE_NS + 10_000_000))
    recorder.record_frame(processed_frame(0, feature_failure=True))
    recorder.record_frame(processed_frame(1))
    completion = recorder.finalize()
    frame_rows = read_csv_rows(completion.session_directory / "frame_features.csv")
    master_rows = read_csv_rows(completion.session_directory / "master_synchronized.csv")
    assert len(decode_video(completion.video_path)) == len(frame_rows) == len(master_rows) == 2
    assert frame_rows[0]["error_code"] == ErrorCode.FEATURE_EXTRACTION_FAILED.value
    assert math.isnan(float(frame_rows[0]["roi1_mean_v"]))


class _ClosedVideoWriter:
    def isOpened(self) -> bool:  # noqa: N802
        return False

    def write(self, frame: np.ndarray) -> None:
        raise AssertionError("closed writer must never receive a frame")

    def release(self) -> None:
        return None


class _RejectingVideoWriter:
    def isOpened(self) -> bool:  # noqa: N802
        return True

    def write(self, frame: np.ndarray) -> bool:
        return False

    def release(self) -> None:
        return None


def test_runtime_video_writer_rejection_stops_with_specific_partial_status(
    tmp_path: Path,
) -> None:
    recorder = start_recorder(
        tmp_path,
        "runtime-codec-failure",
        video_writer_factory=lambda *_args: _RejectingVideoWriter(),
    )

    with pytest.raises(RecorderIntegrityError, match="video writer"):
        recorder.record_frame(processed_frame(0))

    session_dir = recorder.session_directory
    assert session_dir is not None
    status = json.loads((session_dir / "session_status.json").read_text("utf-8"))
    assert status["partial"] is True
    assert status["accepted_frame_count"] == 0
    assert status["feature_row_count"] == 0
    assert status["video_frame_count"] == 0
    assert status["errors"][-1]["error_code"] == ErrorCode.VIDEO_WRITER_FAILED.value
    assert (session_dir / "frame_features.csv.partial").is_file()
    assert (session_dir / "loadcell_raw.csv.partial").is_file()


def test_mp4_failure_falls_back_to_mjpg_avi_and_metadata_matches(tmp_path: Path) -> None:
    def fallback_factory(path: str, fourcc: int, fps: float, size: tuple[int, int]):
        if path.endswith(".mp4"):
            return _ClosedVideoWriter()
        return cv2.VideoWriter(path, fourcc, fps, size)

    recorder = start_recorder(
        tmp_path, "fallback-session", video_writer_factory=fallback_factory
    )
    recorder.record_loadcell_sample(load_sample(0, BASE_NS))
    recorder.record_frame(processed_frame(0))
    completion = recorder.finalize()
    assert completion.video_codec == "MJPG"
    assert completion.video_path.name == "session_video.avi"
    status = json.loads(
        (completion.session_directory / "session_status.json").read_text("utf-8")
    )
    config = json.loads(
        (completion.session_directory / "session_config.json").read_text("utf-8")
    )
    assert status["video_codec"] == config["video_codec"] == "MJPG"
    assert status["video_filename"] == config["video_filename"] == "session_video.avi"
    assert not (completion.session_directory / "session_video.mp4").exists()


def test_periodic_low_space_stops_safely_and_preserves_partials(tmp_path: Path) -> None:
    calls = 0

    def disk_usage(_path):
        nonlocal calls
        calls += 1
        free = 10 * 1024 * 1024 if calls == 1 else 0
        return (20 * 1024 * 1024, 10 * 1024 * 1024, free)

    recorder = start_recorder(
        tmp_path,
        "low-space",
        disk_usage_provider=disk_usage,
        disk_check_interval_frames=1,
    )
    with pytest.raises(DiskSpaceLowError):
        recorder.record_frame(processed_frame(0))
    session_dir = recorder.session_directory
    assert session_dir is not None
    assert recorder.lifecycle is RecordingLifecycle.ERROR
    assert (session_dir / "frame_features.csv.partial").exists()
    assert (session_dir / "loadcell_raw.csv.partial").exists()
    assert any(session_dir.glob("session_video.partial.*"))
    assert not (session_dir / "master_synchronized.csv").exists()
    assert len(read_csv_rows(session_dir / "frame_features.csv.partial")) == 1
    status = json.loads((session_dir / "session_status.json").read_text("utf-8"))
    assert status["partial"] is True
    assert status["errors"][-1]["error_code"] == ErrorCode.DISK_SPACE_LOW.value


def test_existing_partial_session_is_never_overwritten_and_remains_recoverable(
    tmp_path: Path,
) -> None:
    first = start_recorder(tmp_path, "same-name")
    first.record_frame(processed_frame(0))
    first.abort(ErrorCode.CAMERA_DISCONNECTED, "synthetic interruption")
    session_dir = first.session_directory
    assert session_dir is not None
    existing_feature_bytes = (session_dir / "frame_features.csv.partial").read_bytes()

    second = SessionRecorder(recording_config(tmp_path))
    with pytest.raises(RecorderPreflightError, match="will not be overwritten"):
        second.start(
            "same-name",
            frame_size=(WIDTH, HEIGHT),
            output_fps=10,
            calibration=calibration(),
        )
    assert (session_dir / "frame_features.csv.partial").read_bytes() == existing_feature_bytes
    status = json.loads((session_dir / "session_status.json").read_text("utf-8"))
    assert status["partial"] is True
    assert status["accepted_frame_count"] == 1


class _EmptyCapture:
    def isOpened(self) -> bool:  # noqa: N802
        return True

    def read(self):
        return False, None

    def release(self) -> None:
        return None


def test_physical_video_count_mismatch_blocks_finalization_and_keeps_partials(
    tmp_path: Path,
) -> None:
    recorder = start_recorder(
        tmp_path,
        "bad-video-count",
        video_capture_factory=lambda _path: _EmptyCapture(),
    )
    recorder.record_frame(processed_frame(0))
    with pytest.raises(RecorderIntegrityError, match="video contains 0 frames"):
        recorder.finalize()
    session_dir = recorder.session_directory
    assert session_dir is not None
    assert recorder.lifecycle is RecordingLifecycle.ERROR
    assert (session_dir / "frame_features.csv.partial").exists()
    assert any(session_dir.glob("session_video.partial.*"))
    assert not any(session_dir.glob("session_video.mp4"))


def test_nonsequential_frame_id_stops_partial_before_silent_identity_loss(
    tmp_path: Path,
) -> None:
    recorder = start_recorder(tmp_path, "bad-frame-id")
    with pytest.raises(RecorderIntegrityError, match="sequential"):
        recorder.record_frame(processed_frame(1))
    assert recorder.lifecycle is RecordingLifecycle.ERROR
    session_dir = recorder.session_directory
    assert session_dir is not None
    assert len(read_csv_rows(session_dir / "frame_features.csv.partial")) == 0


def test_missing_required_artifact_bundle_blocks_complete_status(tmp_path: Path) -> None:
    recorder = SessionRecorder(recording_config(tmp_path))
    recorder.start(
        "missing-artifacts",
        frame_size=(WIDTH, HEIGHT),
        output_fps=10.0,
        calibration=calibration(),
    )
    recorder.record_frame(processed_frame(0))
    with pytest.raises(RecorderIntegrityError, match="provenance artifacts"):
        recorder.finalize()
    session_dir = recorder.session_directory
    assert session_dir is not None
    status = json.loads((session_dir / "session_status.json").read_text("utf-8"))
    assert status["partial"] is True
    assert status["required_artifacts_written"] is False
    assert (session_dir / "frame_features.csv.partial").exists()


def test_contact_trial_generates_all_automatic_graph_companions(tmp_path: Path) -> None:
    recorder = start_recorder(tmp_path, "contact-graphs")
    recorder.record_loadcell_sample(load_sample(10, BASE_NS - 10_000_000))
    recorder.record_loadcell_sample(load_sample(11, BASE_NS + 10_000_000))
    recorder.record_frame(processed_frame(0))
    completion = recorder.finalize()
    graph_directory = completion.session_directory / "spatial_graphs"
    for stem in (
        "trial_peak_mean_delta_v",
        "trial_mean_contact_mean_delta_v",
        "trial_integrated_delta_v",
        "trial_roi_intensity_over_time",
        "trial_peak_spatial_profile",
    ):
        for suffix in (".png", ".csv", ".json"):
            assert (graph_directory / f"{stem}{suffix}").is_file()
    status = json.loads(
        (completion.session_directory / "session_status.json").read_text("utf-8")
    )
    assert status["automatic_heatmap_summary_status"] == "complete"
    assert status["trial_mean_contact_mean_delta_v_status"] == "available"
    assert len(status["automatic_graph_summary"]["generated_files"]) == 15


def test_all_invalid_optical_rows_make_finalization_partial_not_fake_success(
    tmp_path: Path,
) -> None:
    recorder = start_recorder(tmp_path, "invalid-optical-graphs")
    recorder.record_frame(processed_frame(0, feature_failure=True))
    with pytest.raises(RecorderIntegrityError, match="automatic trial graph"):
        recorder.finalize()
    session_dir = recorder.session_directory
    assert session_dir is not None
    status = json.loads((session_dir / "session_status.json").read_text("utf-8"))
    config = json.loads((session_dir / "session_config.json").read_text("utf-8"))
    assert status["partial"] is True
    assert status["automatic_graph_summary"]["status"] == "failed"
    assert config["automatic_graph_summary"] == status["automatic_graph_summary"]
    assert status["automatic_heatmap_summary_status"] == "failed"
    assert status["errors"][-1]["error_code"] == ErrorCode.FINALIZATION_FAILED.value


def test_routine_status_count_writes_are_throttled_but_abort_is_immediate(
    tmp_path: Path,
) -> None:
    recorder = SessionRecorder(
        recording_config(tmp_path),
        status_clock_ns=lambda: 123,
    )
    layout, saved_baseline = roi_layout_and_baseline()
    recorder.start(
        "status-throttle",
        frame_size=(WIDTH, HEIGHT),
        output_fps=10.0,
        calibration=calibration(),
        session_config={"session_id": "status", "trial_id": "throttle"},
        camera_settings={"simulation_mode": True},
        roi_layout=layout,
        baseline=saved_baseline,
    )
    status_writes: list[Path] = []
    original_atomic_json = recorder._atomic_json

    def tracked_atomic_json(path: Path, payload: dict[str, object]) -> None:
        if path.name == "session_status.json":
            status_writes.append(path)
        original_atomic_json(path, payload)

    recorder._atomic_json = tracked_atomic_json  # type: ignore[method-assign]
    for sample_id in range(20):
        recorder.record_loadcell_sample(load_sample(sample_id, BASE_NS + sample_id))
    assert status_writes == []
    recorder.abort(ErrorCode.SHUTDOWN_REQUESTED, "test abort")
    assert len(status_writes) == 1
    assert recorder.session_directory is not None
    status = json.loads(
        (recorder.session_directory / "session_status.json").read_text("utf-8")
    )
    assert status["loadcell_sample_count"] == 20
    assert status["errors"][-1]["error_code"] == ErrorCode.SHUTDOWN_REQUESTED.value


def test_output_video_disk_preflight_is_reusable_and_leaves_no_probe_files(
    tmp_path: Path,
) -> None:
    config = recording_config(tmp_path)
    result = preflight_recording_output(
        config,
        frame_size=(WIDTH, HEIGHT),
        output_fps=10.0,
    )
    assert result.output_directory_valid
    assert result.disk_space_sufficient
    assert result.video_writer_preflight_passed
    assert result.selected_codec in {"mp4v", "MJPG"}
    assert result.selected_container in {".mp4", ".avi"}
    assert result.free_bytes >= result.required_free_bytes
    assert result.attempted_codecs == ("mp4v", "MJPG")
    assert result.output_directory.is_dir()
    assert list(result.output_directory.iterdir()) == []


def test_output_preflight_uses_documented_avi_fallback_without_residue(
    tmp_path: Path,
) -> None:
    config = recording_config(tmp_path)

    def fallback_factory(path: str, fourcc: int, fps: float, size: tuple[int, int]):
        if path.endswith(".mp4"):
            return _ClosedVideoWriter()
        return cv2.VideoWriter(path, fourcc, fps, size)

    result = preflight_recording_output(
        config,
        frame_size=(WIDTH, HEIGHT),
        output_fps=10.0,
        video_writer_factory=fallback_factory,
    )
    assert result.selected_codec == "MJPG"
    assert result.selected_container == ".avi"
    assert list(result.output_directory.iterdir()) == []


def test_output_preflight_rejects_low_disk_before_video_and_leaves_no_residue(
    tmp_path: Path,
) -> None:
    config = recording_config(tmp_path)
    with pytest.raises(RecorderPreflightError, match="below required"):
        preflight_recording_output(
            config,
            frame_size=(WIDTH, HEIGHT),
            output_fps=10.0,
            disk_usage_provider=lambda _path: (
                2 * 1024 * 1024,
                2 * 1024 * 1024,
                0,
            ),
        )
    output_directory = Path(config.output_directory)
    assert output_directory.is_dir()
    assert list(output_directory.iterdir()) == []


def test_auxiliary_processed_and_motion_videos_are_frame_aligned(tmp_path: Path) -> None:
    streams = ("original_overlays", "processed", "motion_magnified")
    recorder = start_recorder(
        tmp_path,
        name="auxiliary-streams",
        auxiliary_video_streams=streams,
    )
    recorder.record_loadcell_sample(load_sample(0, BASE_NS))
    for frame_id in range(3):
        stream_frames = {
            "original_overlays": np.full(
                (HEIGHT, WIDTH, 3), (15, 200, 220), dtype=np.uint8
            ),
            "processed": np.full(
                (HEIGHT, WIDTH, 3), (200, 35, 75), dtype=np.uint8
            ),
            "motion_magnified": np.full(
                (HEIGHT, WIDTH, 3), (60, 90, 230), dtype=np.uint8
            ),
        }
        recorder.record_frame(
            ProcessedFrame(
                capture_frame_id=frame_id,
                original_bgr=original_frame(frame_id),
                feature_row=feature_row(frame_id),
                optical_result=None,
                video_streams=stream_frames,
            )
        )

    completion = recorder.finalize()
    for stream_name in streams:
        path = completion.session_directory / f"session_{stream_name}{completion.video_path.suffix}"
        assert path.is_file()
        assert len(decode_video(path)) == completion.accepted_frame_count == 3
    status = json.loads(
        (completion.session_directory / "session_status.json").read_text("utf-8")
    )
    assert status["auxiliary_video_frame_counts"] == {
        stream_name: 3 for stream_name in streams
    }
