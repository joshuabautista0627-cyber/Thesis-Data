"""Run one short, operator-assisted Arducam + COM3 physical trial.

The fixture must be unloaded and untouched during baseline capture.  Recording
starts after one 700 Hz beep.  Two 1000 Hz beeps three seconds later mark the
operator's one-second center-ROI press window.  A low beep marks its end.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from queue import Empty, Queue
import statistics
import threading
import time
import winsound

import cv2
import pandas as pd

from core.models import (
    ApplicationConfig,
    CameraConfig,
    ErrorCode,
    LoadCellCalibration,
    ROI,
    TrialLabels,
)
from processing.baseline import BaselineContext, capture_baseline
from processing.motion_magnification import MotionMagnificationConfig
from processing.pipeline import ProcessingPipeline
from processing.roi_manager import ROILayout
from services.camera_service import OpenCVCameraService
from services.serial_service import SerialService
from services.session_recorder import SessionRecorder


UNLOADED_FORCE_LIMIT_N = 0.20
UNLOADED_STABILITY_LIMIT_N = 0.05
PRESS_PROMPT_START_S = 3.0
PRESS_PROMPT_END_S = 4.5


def _layout() -> ROILayout:
    return ROILayout.create(
        tuple(
            ROI(
                roi_id=row * 3 + column + 1,
                x=(160, 280, 380)[column],
                y=(105, 215, 315)[row],
                width=80,
                height=80,
            )
            for row in range(3)
            for column in range(3)
        ),
        640,
        480,
    )


def _open_camera(config: CameraConfig) -> tuple[OpenCVCameraService, object]:
    errors: list[str] = []
    for attempt in range(1, 4):
        if attempt > 1:
            time.sleep(2.0)
        service = OpenCVCameraService()
        try:
            return service, service.connect(config)
        except Exception as exc:
            service.disconnect()
            errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
    raise RuntimeError("camera failed after delayed retries: " + "; ".join(errors))


def _collect_unloaded_preflight(
    serial_service: SerialService,
    *,
    sample_count: int = 25,
    timeout_s: float = 6.0,
) -> dict[str, object]:
    """Require a stable, calibrated near-zero load before recording."""

    samples = []
    deadline = time.perf_counter() + timeout_s
    while len(samples) < sample_count and time.perf_counter() < deadline:
        sample = serial_service.read_sample()
        if sample is not None:
            samples.append(sample)
    if len(samples) < sample_count:
        raise RuntimeError(
            f"unloaded preflight received {len(samples)}/{sample_count} load-cell samples"
        )

    forces = [float(sample.force_N) for sample in samples]
    raw_values = [float(sample.raw_adc) for sample in samples]
    sample_ids = [int(sample.arduino_sample_id) for sample in samples]
    median_force = statistics.median(forces)
    force_std = statistics.stdev(forces) if len(forces) > 1 else 0.0
    sample_id_gaps = sum(
        current != previous + 1
        for previous, current in zip(sample_ids, sample_ids[1:])
    )
    passed = (
        abs(median_force) <= UNLOADED_FORCE_LIMIT_N
        and force_std <= UNLOADED_STABILITY_LIMIT_N
        and sample_id_gaps == 0
    )
    result = {
        "sample_count": len(samples),
        "raw_mean": statistics.mean(raw_values),
        "raw_std": statistics.stdev(raw_values),
        "raw_min": min(raw_values),
        "raw_max": max(raw_values),
        "force_mean_N": statistics.mean(forces),
        "force_median_N": median_force,
        "force_std_N": force_std,
        "force_min_N": min(forces),
        "force_max_N": max(forces),
        "force_first_N": forces[0],
        "force_last_N": forces[-1],
        "sample_id_gaps": sample_id_gaps,
        "absolute_force_limit_N": UNLOADED_FORCE_LIMIT_N,
        "stability_limit_N": UNLOADED_STABILITY_LIMIT_N,
        "passed": passed,
    }
    return result


def _require_unloaded_preflight(serial_service: SerialService) -> dict[str, object]:
    result = _collect_unloaded_preflight(serial_service)
    if not result["passed"]:
        raise RuntimeError(
            "unloaded preflight failed: "
            f"median={result['force_median_N']:.3f} N, "
            f"std={result['force_std_N']:.3f} N, "
            f"sample_id_gaps={result['sample_id_gaps']}; "
            "remove all load and do not touch the fixture"
        )
    return result


def run_physical_trial(
    project_root: Path,
    calibration_path: Path,
    *,
    record_seconds: float = 11.0,
) -> dict[str, object]:
    base = ApplicationConfig.load_json(project_root / "config" / "default_config.json")
    output_root = project_root / "output"
    camera_config = replace(
        base.camera,
        device_index=0,
        backend_preference=("DirectShow",),
        requested_width=640,
        requested_height=480,
        requested_fps=30.0,
        warmup_seconds=0.0,
    )
    recording_config = replace(
        base.recording,
        output_directory=str(output_root),
        minimum_preflight_free_mb=1,
        disk_safety_free_mb=1,
        csv_flush_row_count=10,
    )
    calibration = LoadCellCalibration.load_json(calibration_path)
    if calibration.serial_port.upper() != "COM3" or calibration.baud_rate != 115200:
        raise RuntimeError("physical calibration is not bound to COM3 at 115200 baud")

    camera, camera_info = _open_camera(camera_config)
    serial_service = SerialService(calibration=calibration)
    recorder = SessionRecorder(recording_config)
    sample_queue: Queue[object] = Queue()
    sample_stop = threading.Event()
    sample_errors: list[str] = []
    serial_info = None
    started = False
    baseline_capture_ms: list[float] = []
    frame_processing_ms: list[float] = []
    try:
        camera_settings = {
            "simulation_mode": False,
            "identified_device": "Arducam IMX179 Camera Module",
            "windows_pnp_identity": "USB VID_1BCF&PID_0B12",
            "requested": asdict(camera_config),
            "actual": asdict(camera_info),
            "mode_readbacks": [asdict(item) for item in camera.mode_readbacks],
            "controls": {},
        }
        layout = _layout()
        context = BaselineContext(
            camera_device="Arducam IMX179 Camera Module, index 0",
            backend=camera_info.backend,
            frame_width=640,
            frame_height=480,
            camera_settings=camera_settings,
            roi_layout_id=layout.roi_layout_id,
            processing_settings={
                "minimum_saturation_for_h": base.processing.minimum_saturation_for_h,
                "active_delta_v_threshold": base.processing.active_delta_v_threshold,
                "localization_min_mean_delta_v": base.processing.localization_min_mean_delta_v,
            },
        )

        baseline_frames = []
        for _ in range(30):
            before = time.perf_counter()
            captured = camera.read_frame()
            baseline_capture_ms.append((time.perf_counter() - before) * 1000.0)
            if captured is None:
                raise RuntimeError("camera failed during physical baseline capture")
            baseline_frames.append(captured.original_bgr)
        effective_fps = 1000.0 / statistics.median(baseline_capture_ms[5:])
        baseline = capture_baseline(
            baseline_frames,
            layout.rois,
            context=context,
            minimum_saturation_for_h=base.processing.minimum_saturation_for_h,
            minimum_valid_frames=20,
            capture_timestamp_iso=datetime.now(timezone.utc).isoformat(),
            baseline_id=f"baseline-physical-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        )

        serial_info = serial_service.connect(base.loadcell, port="COM3")
        unloaded_preflight = _require_unloaded_preflight(serial_service)
        labels = TrialLabels(
            session_id="physical-arducam-com3",
            trial_id="hardware-press-001",
            sensing_skin_id="physical-sensing-skin",
            target_roi_ground_truth=5,
            specimen_or_participant_id="hardware-validation",
            trial_interaction_class="Press",
            trial_force_class="physical-calibrated",
            press_number=1,
            notes="Operator-assisted physical hardware validation; center ROI single press.",
        )
        motion = MotionMagnificationConfig(
            enabled=True,
            mode="Color magnification",
            amplification=20.0,
            lower_cutoff_hz=0.4,
            upper_cutoff_hz=3.0,
            chrominance_gain=0.5,
            pyramid_levels=3,
            lambda_c=16.0,
            downscale_factor=0.5,
            target_fps=effective_fps,
            roi_only=True,
        )
        pipeline = ProcessingPipeline(
            config=replace(base, camera=camera_config, recording=recording_config),
            roi_layout=layout,
            baseline=baseline,
            baseline_context=context,
            trial_labels=labels,
            requested_fps=30.0,
            actual_fps=effective_fps,
            camera_backend=camera_info.backend,
            camera_device_index=0,
            calibration_id=calibration.calibration_id,
            arduino_protocol_version=serial_info.protocol_version,
            arduino_firmware_version=serial_info.firmware_version,
            motion_config=motion,
            quantitative_analysis_source="Background-subtracted frame",
            processed_preview_mode="Motion-magnified color preview",
            auxiliary_video_streams=(
                "original_overlays",
                "processed",
                "motion_magnified",
            ),
        )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        session_name = f"physical-arducam-com3-{stamp}"
        session_config = replace(
            base, camera=camera_config, recording=recording_config
        ).to_dict()
        session_config.update(
            {
                "simulation_mode": False,
                "physical_hardware": True,
                "trial_labels": labels.as_export_dict(),
                "camera_actual": asdict(camera_info),
                "camera_measured_capture_fps": effective_fps,
                "serial_identity": asdict(serial_info),
                "unloaded_preflight": unloaded_preflight,
                "press_prompt_start_s": PRESS_PROMPT_START_S,
                "press_prompt_end_s": PRESS_PROMPT_END_S,
                "calibration_id": calibration.calibration_id,
                "baseline_id": baseline.baseline_id,
                "roi_layout_id": layout.roi_layout_id,
                "motion_magnification": motion.to_dict(),
                "quantitative_analysis_source": "Background-subtracted frame",
                "auxiliary_video_streams": [
                    "original_overlays",
                    "processed",
                    "motion_magnified",
                ],
            }
        )
        recorder.start(
            session_name,
            frame_size=(640, 480),
            output_fps=effective_fps,
            calibration=calibration,
            session_config=session_config,
            camera_settings=camera_settings,
            roi_layout=layout,
            baseline=baseline,
            max_sync_gap_ms=base.loadcell.max_sync_gap_ms,
            contact_threshold_N=base.loadcell.contact_threshold_N,
            auxiliary_video_streams=(
                "original_overlays",
                "processed",
                "motion_magnified",
            ),
        )
        started = True

        def read_samples() -> None:
            while not sample_stop.is_set():
                try:
                    sample = serial_service.read_sample()
                    if sample is not None:
                        sample_queue.put(sample)
                except Exception as exc:
                    sample_errors.append(f"{type(exc).__name__}: {exc}")
                    sample_stop.set()

        sample_thread = threading.Thread(target=read_samples, daemon=True)
        sample_thread.start()
        winsound.Beep(700, 250)
        print("RECORDING_STARTED; press center ROI after the double beep", flush=True)
        recording_start_ns = time.perf_counter_ns()
        frame_id = 0
        prompt_sent = False
        prompt_end_sent = False
        while (time.perf_counter_ns() - recording_start_ns) / 1e9 < record_seconds:
            elapsed = (time.perf_counter_ns() - recording_start_ns) / 1e9
            if not prompt_sent and elapsed >= PRESS_PROMPT_START_S:
                winsound.Beep(1000, 180)
                winsound.Beep(1000, 180)
                prompt_sent = True
                print("PRESS_NOW", flush=True)
            if not prompt_end_sent and elapsed >= PRESS_PROMPT_END_S:
                winsound.Beep(500, 250)
                prompt_end_sent = True
                print("PRESS_WINDOW_ENDED", flush=True)
            captured = camera.read_frame()
            if captured is None:
                raise RuntimeError("camera failed during physical recording")
            before = time.perf_counter()
            processed = pipeline.process(
                captured,
                capture_frame_id=frame_id,
                recording_start_monotonic_ns=recording_start_ns,
            )
            recorder.record_frame(processed)
            frame_processing_ms.append((time.perf_counter() - before) * 1000.0)
            frame_id += 1
            while True:
                try:
                    recorder.record_loadcell_sample(sample_queue.get_nowait())
                except Empty:
                    break

        sample_stop.set()
        sample_thread.join(timeout=2.0)
        while True:
            try:
                recorder.record_loadcell_sample(sample_queue.get_nowait())
            except Empty:
                break
        if sample_errors:
            raise RuntimeError("load-cell reader failed: " + "; ".join(sample_errors))
        completion = recorder.finalize()
        started = False
        master = pd.read_csv(completion.session_directory / "master_synchronized.csv")
        force = pd.to_numeric(master["force_N"], errors="coerce")
        result = {
            "session_directory": str(completion.session_directory.resolve()),
            "accepted_frames": completion.accepted_frame_count,
            "video_frames": completion.video_frame_count,
            "loadcell_samples": completion.loadcell_sample_count,
            "master_rows": len(master),
            "missing_force_rows": int(force.isna().sum()),
            "peak_force_N": float(force.max()),
            "median_force_N": float(force.median()),
            "camera_measured_fps_before_recording": effective_fps,
            "median_processing_and_write_ms": statistics.median(frame_processing_ms),
            "video_codec": completion.video_codec,
            "auxiliary_streams": [
                "original_overlays",
                "processed",
                "motion_magnified",
            ],
            "calibration_id": calibration.calibration_id,
            "serial_identity": asdict(serial_info),
            "unloaded_preflight": unloaded_preflight,
        }
        (completion.session_directory / "hardware_validation_summary.json").write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        winsound.Beep(800, 350)
        return result
    except Exception:
        if started:
            recorder.abort(ErrorCode.INTERNAL_ERROR, "physical smoke test failed")
        raise
    finally:
        sample_stop.set()
        serial_service.disconnect()
        camera.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--calibration",
        type=Path,
        default=Path("output/hardware_calibration_20260803/physical_loadcell_calibration.json"),
    )
    parser.add_argument("--seconds", type=float, default=11.0)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    if args.preflight_only:
        base = ApplicationConfig.load_json(project_root / "config" / "default_config.json")
        calibration = LoadCellCalibration.load_json(args.calibration.expanduser().resolve())
        serial_service = SerialService(calibration=calibration)
        try:
            serial_service.connect(base.loadcell, port="COM3")
            preflight = _collect_unloaded_preflight(serial_service)
            print(json.dumps(preflight, indent=2))
            return 0 if preflight["passed"] else 2
        finally:
            serial_service.disconnect()
    result = run_physical_trial(
        project_root,
        args.calibration.expanduser().resolve(),
        record_seconds=float(args.seconds),
    )
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
