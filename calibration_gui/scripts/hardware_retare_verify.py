"""Retare the physical fixture or verify it with the known 200 g mass."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time

from core.models import (
    ApplicationConfig,
    CalibrationVerification,
    LoadCellCalibration,
)
from services.serial_service import SerialService


SETTLING_SAMPLES = 10
USED_SAMPLES = 85


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _capture(service: SerialService) -> tuple[list[object], dict[str, object]]:
    required = SETTLING_SAMPLES + USED_SAMPLES
    captured = []
    deadline = time.perf_counter() + 15.0
    while len(captured) < required and time.perf_counter() < deadline:
        sample = service.read_sample()
        if sample is not None:
            captured.append(sample)
    if len(captured) != required:
        raise RuntimeError(f"received {len(captured)}/{required} load-cell samples")
    samples = captured[SETTLING_SAMPLES:]
    raw = [float(sample.raw_adc) for sample in samples]
    ids = [int(sample.arduino_sample_id) for sample in samples]
    host_ns = [int(sample.host_monotonic_ns) for sample in samples]
    intervals_ms = [
        (current - previous) / 1e6
        for previous, current in zip(host_ns, host_ns[1:])
    ]
    summary = {
        "captured_samples": len(captured),
        "discarded_settling_samples": SETTLING_SAMPLES,
        "used_samples": len(samples),
        "mean_raw": statistics.mean(raw),
        "std_raw": statistics.stdev(raw),
        "span_raw": max(raw) - min(raw),
        "min_raw": min(raw),
        "max_raw": max(raw),
        "median_interval_ms": statistics.median(intervals_ms),
        "measured_rate_hz": 1000.0 / statistics.median(intervals_ms),
        "first_sample_id": ids[0],
        "last_sample_id": ids[-1],
        "id_gaps": sum(
            current != previous + 1 for previous, current in zip(ids, ids[1:])
        ),
    }
    return samples, summary


def _open(project_root: Path, calibration: LoadCellCalibration) -> SerialService:
    config = ApplicationConfig.load_json(project_root / "config" / "default_config.json")
    service = SerialService(calibration=calibration)
    service.connect(config.loadcell, port="COM3")
    return service


def retare(project_root: Path, calibration_path: Path, evidence_dir: Path) -> dict[str, object]:
    calibration = LoadCellCalibration.load_json(calibration_path)
    service = _open(project_root, calibration)
    try:
        _samples, summary = _capture(service)
    finally:
        service.disconnect()
    if summary["id_gaps"] != 0:
        raise RuntimeError("sample-ID gap during fixture retare")
    if summary["std_raw"] > 100.0:
        raise RuntimeError(f"fixture retare is unstable: std={summary['std_raw']:.3f} counts")

    timestamp = _timestamp()
    previous_tare = calibration.tare_raw
    previous_verification = asdict(calibration.verification) if calibration.verification else None
    retared = calibration.with_tare(summary["mean_raw"], timestamp)
    backup_path = evidence_dir / "physical_loadcell_calibration_before_fixture_retare.json"
    if not backup_path.exists():
        calibration.save_json(backup_path)
    retared.save_json(calibration_path)
    evidence = {
        "port": "COM3",
        "baud_rate": 115200,
        "phase": "fixture_installed_retare",
        **summary,
        "previous_tare_raw": previous_tare,
        "new_tare_raw": retared.tare_raw,
        "tare_shift_counts": retared.tare_raw - previous_tare,
        "tare_shift_g": (retared.tare_raw - previous_tare) / retared.counts_per_gram,
        "previous_verification_invalidated": previous_verification is not None,
        "timestamp_utc": timestamp,
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "fixture_retare_window.json").write_text(
        json.dumps(evidence, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return evidence


def verify(project_root: Path, calibration_path: Path, evidence_dir: Path) -> dict[str, object]:
    calibration = LoadCellCalibration.load_json(calibration_path)
    service = _open(project_root, calibration)
    try:
        samples, summary = _capture(service)
    finally:
        service.disconnect()
    mass_g = [calibration.force_gf(float(sample.raw_adc)) for sample in samples]
    measured_mean = statistics.mean(mass_g)
    measured_std = statistics.stdev(mass_g)
    expected = calibration.known_mass_g
    absolute_error = abs(measured_mean - expected)
    percentage_error = absolute_error / expected * 100.0
    reasons = () if percentage_error <= 5.0 else ("verification_error_above_tolerance",)
    verification = CalibrationVerification(
        expected_gf=expected,
        measured_mean_gf=measured_mean,
        absolute_error_gf=absolute_error,
        percentage_error=percentage_error,
        measured_std_gf=measured_std,
        tolerance_percent=5.0,
        sample_count=len(samples),
        minimum_sample_count=20,
        passed=not reasons,
        rejection_reasons=reasons,
    )
    verified = replace(calibration, verification=verification)
    verified.save_json(calibration_path)
    evidence = {
        "port": "COM3",
        "baud_rate": 115200,
        "phase": "fixture_retare_independent_200g_verification",
        "expected_mass_g": expected,
        **summary,
        "zero_offset_raw": calibration.tare_raw,
        "counts_per_gram": calibration.counts_per_gram,
        "measured_mass_g": measured_mean,
        "mass_stability_sd_g": measured_std,
        "measured_force_N": measured_mean * 0.00980665,
        "expected_force_N": expected * 0.00980665,
        "absolute_mass_error_g": absolute_error,
        "verification_error_percent": percentage_error,
        "verification_tolerance_percent": 5.0,
        "verification_passed": verification.passed,
        "timestamp_utc": _timestamp(),
    }
    (evidence_dir / "fixture_retare_200g_verification.json").write_text(
        json.dumps(evidence, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    if not verification.passed:
        raise RuntimeError(
            f"200 g verification failed: measured={measured_mean:.3f} g, "
            f"error={percentage_error:.3f}%"
        )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("retare", "verify"))
    parser.add_argument(
        "--calibration",
        type=Path,
        default=Path("output/hardware_calibration_20260803/physical_loadcell_calibration.json"),
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    calibration_path = args.calibration.resolve()
    evidence_dir = calibration_path.parent
    action = retare if args.mode == "retare" else verify
    print(json.dumps(action(project_root, calibration_path, evidence_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
