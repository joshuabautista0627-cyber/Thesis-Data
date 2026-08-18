"""Run a non-actuating physical camera probe for a v3/v4 event bundle.

Preview-only mode verifies camera acquisition and canonical ROI geometry without
claiming an unloaded baseline.  The stability mode is gated by an explicit
``--fixture-unloaded`` acknowledgement and then captures a fresh optical
baseline, completes the bundle's live warm-up, and monitors no-contact behavior.
The script never opens a serial port, reads a load cell, or commands a printer.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import statistics
import time

import cv2
import numpy as np

from core.experimental_live_sensor import (
    ExperimentalLiveSensorEngine,
    load_experimental_bundle,
)
from core.live_sensor_contracts import load_canonical_live_layout
from core.models import ApplicationConfig
from processing.baseline import BaselineContext, capture_baseline
from processing.camera_orientation import orient_bgr_frame
from processing.feature_extraction import annotate_preview, process_optical_frame
from scripts.hardware_physical_smoke import _open_camera


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = PROJECT_ROOT / "models" / "live_sensor_experimental_hybrid_v4"
DEFAULT_LAYOUT = PROJECT_ROOT / "assets" / "live_sensor" / "roi_layout.json"


def _capture_oriented(camera, *, rotation: int, mirror: bool):
    before = time.perf_counter_ns()
    captured = camera.read_frame()
    after = time.perf_counter_ns()
    if captured is None:
        raise RuntimeError("camera returned no frame")
    return (
        orient_bgr_frame(
            captured.original_bgr,
            rotation_degrees=rotation,
            mirror_horizontal=mirror,
        ),
        (after - before) / 1_000_000.0,
    )


def run(
    output_directory: Path,
    *,
    bundle_path: Path = DEFAULT_BUNDLE,
    layout_path: Path = DEFAULT_LAYOUT,
    fixture_unloaded: bool = False,
    preview_only: bool = False,
    baseline_frames: int = 30,
    monitor_frames: int = 60,
) -> dict[str, object]:
    if not preview_only and not fixture_unloaded:
        raise ValueError(
            "stability mode requires --fixture-unloaded; use --preview-only otherwise"
        )
    if baseline_frames < 20 or monitor_frames < 1:
        raise ValueError("baseline_frames must be at least 20 and monitor_frames positive")

    base = ApplicationConfig.load_json(PROJECT_ROOT / "config" / "default_config.json")
    layout = load_canonical_live_layout(layout_path)
    if layout.rotation_degrees is None or layout.mirror_horizontal is None:
        raise ValueError("canonical live layout is missing camera orientation")
    bundle = load_experimental_bundle(bundle_path)
    if bundle.sensor_mode not in {"event_signal", "hybrid_force_event"}:
        raise ValueError("hardware event probe requires an event-capable bundle")
    camera_config = replace(
        base.camera,
        device_index=0,
        backend_preference=("DirectShow",),
        requested_width=640,
        requested_height=480,
        requested_fps=30.0,
        rotation_degrees=layout.rotation_degrees,
        mirror_horizontal=layout.mirror_horizontal,
        warmup_seconds=0.0,
    )
    output_directory.mkdir(parents=True, exist_ok=False)
    camera, camera_info = _open_camera(camera_config)
    read_ms: list[float] = []
    try:
        for _ in range(10):
            _frame, elapsed_ms = _capture_oriented(
                camera,
                rotation=layout.rotation_degrees,
                mirror=layout.mirror_horizontal,
            )
            read_ms.append(elapsed_ms)
        preview, elapsed_ms = _capture_oriented(
            camera,
            rotation=layout.rotation_degrees,
            mirror=layout.mirror_horizontal,
        )
        read_ms.append(elapsed_ms)
        preview_path = output_directory / "canonical_roi_preview.png"
        if not cv2.imwrite(str(preview_path), annotate_preview(preview, layout.rois)):
            raise RuntimeError("could not write canonical ROI preview")
        result: dict[str, object] = {
            "schema_version": "1.0.0",
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "mode": "preview_only" if preview_only else "unloaded_stability",
            "scientific_claim_allowed": False,
            "bundle_id": bundle.bundle_id,
            "sensor_mode": bundle.sensor_mode,
            "force_output_available": bundle.force_x is not None,
            "canonical_layout": {
                "roi_layout_id": layout.roi_layout_id,
                "frame_size": [layout.frame_width, layout.frame_height],
                "rotation_degrees": layout.rotation_degrees,
                "mirror_horizontal": layout.mirror_horizontal,
            },
            "camera": {
                "requested": asdict(camera_config),
                "actual": asdict(camera_info),
                "oriented_frame_shape": list(preview.shape),
                "preview_mean_bgr": [
                    float(value) for value in np.mean(preview, axis=(0, 1))
                ],
                "preview_std_bgr": [
                    float(value) for value in np.std(preview, axis=(0, 1))
                ],
                "median_blocking_read_ms": statistics.median(read_ms),
            },
            "preview_path": str(preview_path.resolve()),
        }
        if preview_only:
            result["interpretation"] = (
                "Acquisition and ROI geometry only; no unloaded-state or event-performance claim."
            )
        else:
            baseline_images = []
            for _ in range(baseline_frames):
                frame, elapsed_ms = _capture_oriented(
                    camera,
                    rotation=layout.rotation_degrees,
                    mirror=layout.mirror_horizontal,
                )
                read_ms.append(elapsed_ms)
                baseline_images.append(frame)
            context = BaselineContext(
                camera_device="Arducam IMX179 Camera Module, DirectShow index 0",
                backend=str(camera_info.backend),
                frame_width=layout.frame_width,
                frame_height=layout.frame_height,
                camera_settings={
                    "rotation_degrees_clockwise": layout.rotation_degrees,
                    "mirror_horizontal": layout.mirror_horizontal,
                    "probe": "non_actuating_event_bundle_stability",
                },
                roi_layout_id=layout.roi_layout_id,
                processing_settings={
                    "minimum_saturation_for_h": base.processing.minimum_saturation_for_h,
                    "active_delta_v_threshold": base.processing.active_delta_v_threshold,
                    "localization_min_mean_delta_v": base.processing.localization_min_mean_delta_v,
                },
            )
            baseline = capture_baseline(
                baseline_images,
                layout.rois,
                context=context,
                minimum_saturation_for_h=base.processing.minimum_saturation_for_h,
                minimum_valid_frames=20,
                capture_timestamp_iso=datetime.now(UTC).isoformat(),
                baseline_id=f"event-probe-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
            )
            engine = ExperimentalLiveSensorEngine(bundle)
            engine.set_context_ready(True, baseline.baseline_id)
            engine.start_unloaded_warmup()
            outputs = []
            total_runtime_frames = bundle.minimum_warmup_frames + monitor_frames
            for frame_id in range(total_runtime_frames):
                frame, elapsed_ms = _capture_oriented(
                    camera,
                    rotation=layout.rotation_degrees,
                    mirror=layout.mirror_horizontal,
                )
                read_ms.append(elapsed_ms)
                optical = process_optical_frame(
                    frame,
                    layout.rois,
                    baseline,
                    minimum_saturation_for_h=base.processing.minimum_saturation_for_h,
                    active_delta_v_threshold=base.processing.active_delta_v_threshold,
                    localization_min_mean_delta_v=(
                        base.processing.localization_min_mean_delta_v
                    ),
                )
                row = dict(optical.to_export_dict())
                row["capture_frame_id"] = frame_id
                outputs.append(engine.process(row))
            monitor = outputs[bundle.minimum_warmup_frames :]
            raw_exceedances = sum(
                item.contact_score is not None
                and item.threshold is not None
                and item.contact_score >= item.threshold
                for item in monitor
            )
            result["unloaded_acknowledged_by_operator"] = True
            result["baseline"] = {
                "baseline_id": baseline.baseline_id,
                "frame_count": baseline_frames,
                "roi_layout_id": baseline.roi_layout_id,
                "mean_v_by_roi": [
                    float(record.mean_v) for record in baseline.roi_baselines
                ],
            }
            result["runtime"] = {
                "warmup_frames": bundle.minimum_warmup_frames,
                "warmup_completed": any(item.state == "READY" for item in outputs),
                "threshold": engine.threshold,
                "monitor_frames": len(monitor),
                "state_counts": dict(Counter(item.state for item in monitor)),
                "raw_threshold_exceedance_fraction": raw_exceedances / len(monitor),
                "debounced_contact_fraction": sum(item.contact for item in monitor)
                / len(monitor),
                "completed_events": sum(
                    item.event_phase == "completed" for item in monitor
                ),
                "no_nonzero_force_outputs_while_unloaded": all(
                    item.force_N is None or abs(item.force_N) <= 1e-12
                    for item in outputs
                ),
                "unloaded_stability_passed": (
                    raw_exceedances / len(monitor) <= 0.05
                    and not any(item.contact for item in monitor)
                    and not any(item.event_phase == "completed" for item in monitor)
                ),
            }
            result["camera"]["median_blocking_read_ms"] = statistics.median(read_ms)
            result["camera"]["p95_blocking_read_ms"] = float(
                np.quantile(np.asarray(read_ms), 0.95)
            )
            result["interpretation"] = (
                "One operator-acknowledged unloaded stability check; not known-force, "
                "localization, soak, usability, or confirmatory validation."
            )
        summary_path = output_directory / "hardware_event_signal_probe.json"
        summary_path.write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        result["summary_path"] = str(summary_path.resolve())
        return result
    finally:
        camera.disconnect()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--layout", type=Path, default=DEFAULT_LAYOUT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--fixture-unloaded", action="store_true")
    parser.add_argument("--baseline-frames", type=int, default=30)
    parser.add_argument("--monitor-frames", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir or (
        PROJECT_ROOT / "output" / f"hardware_event_signal_probe_{stamp}"
    )
    result = run(
        output.resolve(),
        bundle_path=args.bundle.resolve(),
        layout_path=args.layout.resolve(),
        fixture_unloaded=args.fixture_unloaded,
        preview_only=args.preview_only,
        baseline_frames=args.baseline_frames,
        monitor_frames=args.monitor_frames,
    )
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
