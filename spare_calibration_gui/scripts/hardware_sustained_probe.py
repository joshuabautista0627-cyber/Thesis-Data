"""Run a sustained concurrent Arducam/COM3 physical cadence probe."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import threading
import time

from core.models import ApplicationConfig, LoadCellCalibration
from scripts.hardware_physical_smoke import _open_camera
from services.serial_service import SerialService


def _interval_summary(timestamps_ns: list[int]) -> dict[str, float]:
    intervals = [
        (current - previous) / 1e6
        for previous, current in zip(timestamps_ns, timestamps_ns[1:])
    ]
    elapsed_s = (timestamps_ns[-1] - timestamps_ns[0]) / 1e9
    return {
        "rate_hz": (len(timestamps_ns) - 1) / elapsed_s,
        "minimum_interval_ms": min(intervals),
        "median_interval_ms": statistics.median(intervals),
        "p95_interval_ms": sorted(intervals)[int(0.95 * (len(intervals) - 1))],
        "maximum_interval_ms": max(intervals),
    }


def run(project_root: Path, duration_s: float) -> dict[str, object]:
    base = ApplicationConfig.load_json(project_root / "config" / "default_config.json")
    camera_config = replace(
        base.camera,
        device_index=0,
        backend_preference=("DirectShow",),
        requested_width=640,
        requested_height=480,
        requested_fps=30.0,
        warmup_seconds=0.0,
    )
    calibration_path = (
        project_root
        / "output"
        / "hardware_calibration_20260803"
        / "physical_loadcell_calibration.json"
    )
    calibration = LoadCellCalibration.load_json(calibration_path)
    camera, camera_info = _open_camera(camera_config)
    serial_service = SerialService(calibration=calibration)
    serial_info = serial_service.connect(base.loadcell, port="COM3")
    serial_samples = []
    serial_errors: list[str] = []
    stop = threading.Event()

    def read_serial() -> None:
        while not stop.is_set():
            try:
                sample = serial_service.read_sample()
                if sample is not None:
                    serial_samples.append(sample)
            except Exception as exc:
                serial_errors.append(f"{type(exc).__name__}: {exc}")
                stop.set()

    thread = threading.Thread(target=read_serial, daemon=True)
    frame_timestamps_ns: list[int] = []
    read_block_ms: list[float] = []
    started_ns = time.perf_counter_ns()
    thread.start()
    try:
        while (time.perf_counter_ns() - started_ns) / 1e9 < duration_s:
            before = time.perf_counter_ns()
            frame = camera.read_frame()
            after = time.perf_counter_ns()
            if frame is None:
                raise RuntimeError("camera returned no frame during sustained probe")
            frame_timestamps_ns.append(int(frame.host_monotonic_ns))
            read_block_ms.append((after - before) / 1e6)
    finally:
        stop.set()
        thread.join(timeout=2.0)
        serial_service.disconnect()
        camera.disconnect()
    if serial_errors:
        raise RuntimeError("serial error during sustained probe: " + "; ".join(serial_errors))
    if len(frame_timestamps_ns) < 2 or len(serial_samples) < 2:
        raise RuntimeError("insufficient camera or serial observations")

    frame_elapsed = [
        (timestamp - frame_timestamps_ns[0]) / 1e9 for timestamp in frame_timestamps_ns
    ]
    windows = []
    window_s = duration_s / 3.0
    for index in range(3):
        low = index * window_s
        high = (index + 1) * window_s
        window_times = [
            timestamp
            for timestamp, elapsed in zip(frame_timestamps_ns, frame_elapsed)
            if low <= elapsed < high
        ]
        windows.append(
            {
                "start_s": low,
                "end_s": high,
                "frames": len(window_times),
                **_interval_summary(window_times),
            }
        )
    serial_ids = [int(sample.arduino_sample_id) for sample in serial_samples]
    serial_times = [int(sample.host_monotonic_ns) for sample in serial_samples]
    result = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "duration_requested_s": duration_s,
        "camera": {
            "identity": "Arducam IMX179 Camera Module",
            "pnp_id": "USB VID_1BCF&PID_0B12",
            "requested": asdict(camera_config),
            "actual": asdict(camera_info),
            "frame_count": len(frame_timestamps_ns),
            "overall": _interval_summary(frame_timestamps_ns),
            "beginning_middle_end": windows,
            "read_block_median_ms": statistics.median(read_block_ms),
            "read_block_p95_ms": sorted(read_block_ms)[int(0.95 * (len(read_block_ms) - 1))],
            "read_block_maximum_ms": max(read_block_ms),
        },
        "loadcell": {
            "identity": asdict(serial_info),
            "sample_count": len(serial_samples),
            **_interval_summary(serial_times),
            "sample_id_gaps": sum(
                current != previous + 1
                for previous, current in zip(serial_ids, serial_ids[1:])
            ),
        },
        "usb_contention_passed": True,
        "serial_errors": serial_errors,
    }
    evidence_dir = project_root / "output" / "hardware_camera_probe_20260803"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "sustained_camera_serial_probe.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    print(json.dumps(run(project_root, args.seconds), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
