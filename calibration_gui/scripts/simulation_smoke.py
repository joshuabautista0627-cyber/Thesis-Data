"""Run the complete hardware-free acquisition path through final CSV export."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import time
from typing import Any

import cv2
import pandas as pd

from core.models import ApplicationConfig, CameraConfig, RecordingConfig, TrialLabels
from processing.baseline import BaselineContext, capture_baseline, check_frame_baseline_drift
from processing.loadcell_calibration import (
    build_saved_calibration,
    calculate_calibration,
    verify_calibration,
)
from processing.pipeline import ProcessingPipeline
from services.session_recorder import SessionRecorder
from services.simulation_service import SyntheticCameraSource, SyntheticLoadCellSource


@dataclass(frozen=True, slots=True)
class SimulationSmokeResult:
    session_directory: str
    frame_count: int
    video_frame_count: int
    loadcell_sample_count: int
    master_row_count: int
    missing_force_count: int
    calibration_snr: float
    calibration_loaded_cv_percent: float
    calibration_verification_error_percent: float
    baseline_drift_mean_v: float
    baseline_drift_threshold: float
    baseline_drift_passed: bool
    workflow_setup_s: float
    recorder_start_s: float
    synthetic_camera_generation_s: float
    synthetic_loadcell_generation_s: float
    loadcell_incremental_write_s: float
    optical_processing_s: float
    video_and_frame_csv_write_s: float
    processing_fps: float
    video_and_csv_fps: float
    finalization_s: float
    artifact_validation_s: float
    total_s: float
    slowest_stage: str
    effective_recording_pipeline_fps: float
    projected_max_pipeline_backlog_events: int
    projected_final_pipeline_backlog_events: int
    projected_backlog_growth_events: float
    projected_backlog_increasing: bool
    projected_peak_capture_to_completion_ms: float
    video_filename: str
    video_codec: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _application_config(
    output_root: Path, *, width: int, height: int, fps: float
) -> ApplicationConfig:
    project_root = Path(__file__).resolve().parents[1]
    base = ApplicationConfig.load_json(project_root / "config" / "default_config.json")
    camera = replace(
        base.camera,
        requested_width=width,
        requested_height=height,
        requested_fps=fps,
        warmup_seconds=0.0,
    )
    recording = replace(
        base.recording,
        output_directory=str(output_root),
        minimum_preflight_free_mb=1,
        disk_safety_free_mb=1,
        csv_flush_row_count=20,
    )
    return replace(base, camera=camera, recording=recording)


def _calibration(timestamp_iso: str):
    unloaded = [100_000 + ((index % 5) - 2) for index in range(40)]
    loaded = [120_000 + ((index % 5) - 2) for index in range(40)]
    computation = calculate_calibration(unloaded, loaded, 200.0)
    verification = verify_calibration(
        [120_010 + ((index % 5) - 2) for index in range(20)],
        known_mass_g=200.0,
        tare_raw=computation.tare_raw,
        counts_per_gram=computation.counts_per_gram,
    )
    if not verification.passed:
        raise RuntimeError("deterministic simulation calibration verification failed")
    return build_saved_calibration(
        "calibration-simulation-001",
        computation,
        verification,
        serial_port="SIMULATED",
        firmware_identity="SIMULATED_HX711",
        protocol_version="SIM-1.0",
        firmware_version="synthetic-1.0",
        calibration_timestamp_iso=timestamp_iso,
    )


def _processing_settings(config: ApplicationConfig) -> dict[str, object]:
    return {
        "minimum_saturation_for_h": config.processing.minimum_saturation_for_h,
        "active_delta_v_threshold": config.processing.active_delta_v_threshold,
        "localization_min_mean_delta_v": (
            config.processing.localization_min_mean_delta_v
        ),
    }


def run_simulation_smoke(
    output_root: str | Path,
    *,
    session_directory_name: str | None = None,
    frame_count: int = 90,
    width: int = 320,
    height: int = 240,
    fps: float = 30.0,
    loadcell_rate_hz: float = 80.0,
    seed: int = 20260803,
) -> SimulationSmokeResult:
    """Produce and validate a complete simulation session using production paths."""

    workflow_started = time.perf_counter()
    if frame_count < 20:
        raise ValueError("simulation smoke test requires at least 20 recording frames")
    output_path = Path(output_root).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    config = _application_config(output_path, width=width, height=height, fps=fps)
    base_ns = 10_000_000_000
    base_wall = datetime(2026, 8, 3, 1, 55, 0, tzinfo=UTC)
    timestamp_iso = base_wall.isoformat()
    trial_labels = TrialLabels(
        session_id="simulation-session-001",
        trial_id="trial001",
        sensing_skin_id="skin01",
        target_roi_ground_truth=5,
        specimen_or_participant_id="simulated-specimen",
        trial_interaction_class="Press",
        trial_force_class="calibration",
        press_number=1,
        notes="Deterministic simulation smoke test; no physical hardware.",
    )
    calibration = _calibration(timestamp_iso)

    # A separate, identically seeded unloaded source avoids contaminating the
    # baseline with any part of the intentional synthetic press.
    baseline_source = SyntheticCameraSource(
        seed=seed,
        total_frames=config.processing.minimum_baseline_frames,
        target_roi=5,
        peak_optical_delta_v=0.0,
        base_monotonic_ns=base_ns - 2_000_000_000,
        base_wall_clock=base_wall - timedelta(seconds=2),
    )
    baseline_info = baseline_source.connect(config.camera)
    layout = baseline_source.roi_layout
    camera_settings = {
        "simulation_mode": True,
        "source": "deterministic synthetic camera",
        "device_index": baseline_info.device_index,
        "backend": baseline_info.backend,
        "requested_width": baseline_info.requested_width,
        "requested_height": baseline_info.requested_height,
        "requested_fps": baseline_info.requested_fps,
        "actual_width": baseline_info.actual_width,
        "actual_height": baseline_info.actual_height,
        "actual_fps": baseline_info.actual_fps,
        "controls": {
            "auto_exposure": False,
            "auto_white_balance": False,
            "auto_focus": False,
        },
    }
    context = BaselineContext(
        camera_device="deterministic synthetic camera",
        backend=baseline_info.backend,
        frame_width=width,
        frame_height=height,
        camera_settings=camera_settings,
        roi_layout_id=layout.roi_layout_id,
        processing_settings=_processing_settings(config),
    )
    baseline_frames = []
    while True:
        captured = baseline_source.read_frame()
        if captured is None:
            break
        baseline_frames.append(captured.original_bgr)
    baseline_source.disconnect()
    baseline = capture_baseline(
        baseline_frames,
        layout.rois,
        context=context,
        minimum_saturation_for_h=config.processing.minimum_saturation_for_h,
        minimum_valid_frames=config.processing.minimum_baseline_frames,
        capture_timestamp_iso=timestamp_iso,
        baseline_id="baseline-simulation-001",
    )

    camera = SyntheticCameraSource(
        seed=seed,
        total_frames=frame_count,
        target_roi=5,
        roi_layout=layout,
        base_monotonic_ns=base_ns,
        base_wall_clock=base_wall,
    )
    camera_info = camera.connect(config.camera)
    camera_generation_started = time.perf_counter()
    first_frame = camera.read_frame()
    synthetic_camera_generation_s = time.perf_counter() - camera_generation_started
    if first_frame is None:
        raise RuntimeError("synthetic camera produced no recording frames")
    drift = check_frame_baseline_drift(
        first_frame.original_bgr,
        layout.rois,
        baseline,
        config.camera.baseline_drift_mean_v,
    )
    if not drift.accepted:
        raise RuntimeError("deterministic unloaded drift check unexpectedly failed")

    duration_s = max((frame_count - 1) / fps, 1.0 / fps)
    loadcell = SyntheticLoadCellSource(
        seed=seed,
        sample_rate_hz=loadcell_rate_hz,
        duration_s=duration_s,
        calibration=calibration,
        base_monotonic_ns=base_ns,
        base_wall_clock=base_wall,
        noise_std_counts=2.0,
    )
    serial_info = loadcell.connect(config.loadcell)
    pipeline = ProcessingPipeline(
        config=config,
        roi_layout=layout,
        baseline=baseline,
        baseline_context=context,
        trial_labels=trial_labels,
        requested_fps=camera_info.requested_fps,
        actual_fps=camera_info.actual_fps,
        camera_backend=camera_info.backend,
        camera_device_index=camera_info.device_index,
        calibration_id=calibration.calibration_id,
        arduino_protocol_version=serial_info.protocol_version,
        arduino_firmware_version=serial_info.firmware_version,
    )

    if session_directory_name is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        session_directory_name = f"{stamp}_skin01_trial001"
    session_config = config.to_dict()
    session_config.update(
        {
            "simulation_mode": True,
            "simulation_notice": "No physical camera, Arduino, HX711, or load cell was connected.",
            "trial_labels": trial_labels.as_export_dict(),
            "camera_requested": camera_settings,
            "camera_actual": asdict(camera_info),
            "serial_identity": asdict(serial_info),
            "calibration_id": calibration.calibration_id,
            "baseline_id": baseline.baseline_id,
            "roi_layout_id": layout.roi_layout_id,
            "pre_recording_baseline_drift_mean_v": drift.mean_absolute_drift,
            "baseline_drift_threshold": drift.threshold,
            "baseline_drift_passed": drift.accepted,
        }
    )

    workflow_setup_s = time.perf_counter() - workflow_started
    recorder = SessionRecorder(
        config.recording,
        monotonic_ns=lambda: base_ns,
        disk_check_interval_frames=30,
    )
    total_started = time.perf_counter()
    recorder_start_started = time.perf_counter()
    recorder.start(
        session_directory_name,
        frame_size=(width, height),
        output_fps=fps,
        calibration=calibration,
        session_config=session_config,
        camera_settings=camera_settings,
        roi_layout=layout,
        baseline=baseline,
        max_sync_gap_ms=config.loadcell.max_sync_gap_ms,
        contact_threshold_N=config.loadcell.contact_threshold_N,
    )
    recorder_start_s = time.perf_counter() - recorder_start_started

    synthetic_loadcell_generation_s = 0.0
    load_write_s = 0.0
    load_service_durations_s: list[float] = []
    while True:
        stage_started = time.perf_counter()
        sample = loadcell.read_sample()
        synthetic_loadcell_generation_s += time.perf_counter() - stage_started
        if sample is None:
            break
        stage_started = time.perf_counter()
        recorder.record_loadcell_sample(sample)
        load_service_duration_s = time.perf_counter() - stage_started
        load_write_s += load_service_duration_s
        load_service_durations_s.append(load_service_duration_s)

    processing_s = 0.0
    video_csv_s = 0.0
    frame_service_durations_s: list[float] = []
    captured = first_frame
    for capture_frame_id in range(frame_count):
        if capture_frame_id > 0:
            stage_started = time.perf_counter()
            captured = camera.read_frame()
            synthetic_camera_generation_s += time.perf_counter() - stage_started
        if captured is None:
            raise RuntimeError(
                f"synthetic camera ended at frame {capture_frame_id} of {frame_count}"
            )
        stage_started = time.perf_counter()
        processed = pipeline.process(
            captured,
            capture_frame_id=capture_frame_id,
            recording_start_monotonic_ns=base_ns,
        )
        processing_duration_s = time.perf_counter() - stage_started
        processing_s += processing_duration_s
        stage_started = time.perf_counter()
        recorder.record_frame(processed)
        video_duration_s = time.perf_counter() - stage_started
        video_csv_s += video_duration_s
        frame_service_durations_s.append(processing_duration_s + video_duration_s)

    finalization_started = time.perf_counter()
    completion = recorder.finalize()
    finalization_s = time.perf_counter() - finalization_started
    recording_total_s = time.perf_counter() - total_started
    camera.disconnect()
    loadcell.disconnect()

    validation_started = time.perf_counter()
    frame_path = completion.session_directory / "frame_features.csv"
    master_path = completion.session_directory / "master_synchronized.csv"
    load_path = completion.session_directory / "loadcell_raw.csv"
    frames = pd.read_csv(frame_path)
    master = pd.read_csv(master_path)
    loads = pd.read_csv(load_path)
    expected_ids = list(range(frame_count))
    if frames["capture_frame_id"].tolist() != expected_ids:
        raise RuntimeError("frame_features.csv capture IDs are not one-to-one")
    if master["capture_frame_id"].tolist() != expected_ids:
        raise RuntimeError("master_synchronized.csv capture IDs are not one-to-one")
    if len(loads) != completion.loadcell_sample_count:
        raise RuntimeError("raw load-cell row count differs from recorder count")

    required_files = {
        "session_config.json",
        "camera_settings.json",
        "roi_layout.json",
        "loadcell_calibration.json",
        "baseline_summary.json",
        "baseline_data.npz",
        "data_dictionary.csv",
        "application.log",
        "session_status.json",
        "frame_features.csv",
        "loadcell_raw.csv",
        "master_synchronized.csv",
        completion.video_path.name,
    }
    missing = sorted(
        name
        for name in required_files
        if not (completion.session_directory / name).is_file()
    )
    if missing or not (completion.session_directory / "spatial_graphs").is_dir():
        raise RuntimeError(f"simulation session is missing required artifacts: {missing}")
    required_automatic_graphs = {
        f"{stem}{suffix}"
        for stem in (
            "trial_peak_mean_delta_v",
            "trial_mean_contact_mean_delta_v",
            "trial_integrated_delta_v",
            "trial_roi_intensity_over_time",
            "trial_peak_spatial_profile",
        )
        for suffix in (".png", ".csv", ".json")
    }
    graph_directory = completion.session_directory / "spatial_graphs"
    missing_graphs = sorted(
        name for name in required_automatic_graphs if not (graph_directory / name).is_file()
    )
    if missing_graphs:
        raise RuntimeError(
            "simulation session is missing automatic graph artifacts: "
            f"{missing_graphs}"
        )
    status_payload = json.loads(
        (completion.session_directory / "session_status.json").read_text(
            encoding="utf-8"
        )
    )
    automatic_graphs = status_payload.get("automatic_graph_summary", {})
    if not isinstance(automatic_graphs, dict) or automatic_graphs.get("status") != "complete":
        raise RuntimeError("automatic graph finalization did not report complete status")
    artifact_validation_s = time.perf_counter() - validation_started

    # Project the measured single-owner recording service times onto the
    # requested real-time acquisition schedule.  Camera/serial generation runs
    # in their own owners, so only optical processing and durable writes count
    # against this queue.  This makes sustained backlog growth visible without
    # throttling the deterministic smoke path to wall-clock camera time.
    scheduled_events = [
        (frame_index / fps, 1, "frame", frame_service_s)
        for frame_index, frame_service_s in enumerate(frame_service_durations_s)
    ]
    scheduled_events.extend(
        (sample_index / loadcell_rate_hz, 0, "loadcell", service_s)
        for sample_index, service_s in enumerate(load_service_durations_s)
    )
    scheduled_events.sort(key=lambda event: (event[0], event[1]))
    completion_times_s: list[float] = []
    completed_before_arrival = 0
    backlog_depths: list[int] = []
    last_completion_s = 0.0
    capture_to_completion_s: list[float] = []
    for event_index, (arrival_s, _priority, kind, service_s) in enumerate(
        scheduled_events
    ):
        while (
            completed_before_arrival < len(completion_times_s)
            and completion_times_s[completed_before_arrival] <= arrival_s
        ):
            completed_before_arrival += 1
        backlog_depths.append(event_index - completed_before_arrival)
        service_started_s = max(arrival_s, last_completion_s)
        last_completion_s = service_started_s + service_s
        completion_times_s.append(last_completion_s)
        if kind == "frame":
            capture_to_completion_s.append(last_completion_s - arrival_s)
    quartile_count = max(1, len(backlog_depths) // 4)
    first_quartile_mean = sum(backlog_depths[:quartile_count]) / quartile_count
    final_quartile_mean = sum(backlog_depths[-quartile_count:]) / quartile_count
    projected_backlog_growth_events = final_quartile_mean - first_quartile_mean
    projected_backlog_increasing = projected_backlog_growth_events > 0.5
    measured_recording_service_s = (
        processing_s + video_csv_s + load_write_s
    )

    stages = {
        "workflow_setup": workflow_setup_s,
        "recorder_start": recorder_start_s,
        "synthetic_camera_generation": synthetic_camera_generation_s,
        "synthetic_loadcell_generation": synthetic_loadcell_generation_s,
        "loadcell_incremental_write": load_write_s,
        "optical_processing": processing_s,
        "video_and_frame_csv_write": video_csv_s,
        "finalization_and_synchronization": finalization_s,
        "artifact_validation": artifact_validation_s,
    }
    exporter_summary = completion.exporter_summary
    return SimulationSmokeResult(
        session_directory=str(completion.session_directory.resolve()),
        frame_count=completion.accepted_frame_count,
        video_frame_count=completion.video_frame_count,
        loadcell_sample_count=completion.loadcell_sample_count,
        master_row_count=int(exporter_summary.master_row_count),
        missing_force_count=int(exporter_summary.missing_force_count),
        calibration_snr=calibration.calibration_snr,
        calibration_loaded_cv_percent=calibration.loaded_window_cv_percent,
        calibration_verification_error_percent=(
            calibration.verification.percentage_error
            if calibration.verification is not None
            else float("nan")
        ),
        baseline_drift_mean_v=drift.mean_absolute_drift,
        baseline_drift_threshold=drift.threshold,
        baseline_drift_passed=drift.accepted,
        workflow_setup_s=workflow_setup_s,
        recorder_start_s=recorder_start_s,
        synthetic_camera_generation_s=synthetic_camera_generation_s,
        synthetic_loadcell_generation_s=synthetic_loadcell_generation_s,
        loadcell_incremental_write_s=load_write_s,
        optical_processing_s=processing_s,
        video_and_frame_csv_write_s=video_csv_s,
        processing_fps=(frame_count / processing_s if processing_s else float("inf")),
        video_and_csv_fps=(
            frame_count / video_csv_s if video_csv_s else float("inf")
        ),
        finalization_s=finalization_s,
        artifact_validation_s=artifact_validation_s,
        total_s=recording_total_s + artifact_validation_s,
        slowest_stage=max(stages, key=stages.get),
        effective_recording_pipeline_fps=(
            frame_count / measured_recording_service_s
            if measured_recording_service_s
            else float("inf")
        ),
        projected_max_pipeline_backlog_events=max(backlog_depths, default=0),
        projected_final_pipeline_backlog_events=(
            backlog_depths[-1] if backlog_depths else 0
        ),
        projected_backlog_growth_events=projected_backlog_growth_events,
        projected_backlog_increasing=projected_backlog_increasing,
        projected_peak_capture_to_completion_ms=(
            max(capture_to_completion_s, default=0.0) * 1_000.0
        ),
        video_filename=completion.video_path.name,
        video_codec=completion.video_codec,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic full simulation calibration workflow."
    )
    parser.add_argument("--output-root", type=Path, default=Path("output"))
    parser.add_argument("--session-name", default=None)
    parser.add_argument("--frames", type=int, default=90)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--loadcell-rate", type=float, default=80.0)
    parser.add_argument("--seed", type=int, default=20260803)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    result = run_simulation_smoke(
        args.output_root,
        session_directory_name=args.session_name,
        frame_count=args.frames,
        width=args.width,
        height=args.height,
        fps=args.fps,
        loadcell_rate_hz=args.loadcell_rate,
        seed=args.seed,
    )
    print(json.dumps(result.to_dict(), indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
