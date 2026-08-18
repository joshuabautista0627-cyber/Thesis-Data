from __future__ import annotations

import pytest

from core.models import LoadCellConfig
from services.loadcell_workflow import (
    CalibrationDeviceIdentity,
    LoadCellCalibrationWorkflow,
    minimum_calibration_samples_for_rate,
)


def _workflow() -> LoadCellCalibrationWorkflow:
    return LoadCellCalibrationWorkflow(
        LoadCellConfig(),
        CalibrationDeviceIdentity("COM7", "HX711_NANO", "1.0", "1.2.0"),
    )


def test_sample_minimum_falls_back_only_when_rate_cannot_supply_twenty() -> None:
    assert minimum_calibration_samples_for_rate(80.0, 3.0) == 20
    assert minimum_calibration_samples_for_rate(5.0, 3.0) == 10
    with pytest.raises(ValueError):
        minimum_calibration_samples_for_rate(0.0, 3.0)


def test_workflow_calibrates_verifies_and_persists_complete_identity() -> None:
    result = _workflow().calibrate_and_verify(
        [100_000 + (index % 3) for index in range(20)],
        [120_000 + (index % 3) for index in range(20)],
        [120_020 + (index % 3) for index in range(20)],
        measured_sample_rate_hz=80.0,
        timestamp_iso="2026-08-03T00:00:00+00:00",
        calibration_id="cal-001",
    )

    assert result.assessment.passed
    assert result.verification.passed
    assert result.calibration.calibration_id == "cal-001"
    assert result.calibration.serial_port == "COM7"
    assert result.calibration.counts_per_gram == pytest.approx(100.0)
    assert result.calibration.verification is not None


def test_failed_known_mass_verification_blocks_calibration() -> None:
    with pytest.raises(ValueError, match="verification failed"):
        _workflow().calibrate_and_verify(
            [100_000] * 20,
            [120_000] * 20,
            [110_000] * 20,
            measured_sample_rate_hz=80.0,
        )


def test_tare_requires_stability_and_preserves_signed_scale() -> None:
    workflow = _workflow()
    calibrated = workflow.calibrate_and_verify(
        [100_000] * 20,
        [80_000] * 20,
        [80_000] * 20,
        measured_sample_rate_hz=80.0,
    ).calibration
    retared, evidence = workflow.tare(
        calibrated,
        [100_010 + (index % 2) for index in range(10)],
        timestamp_iso="2026-08-03T01:00:00+00:00",
    )
    assert retared.counts_per_gram == calibrated.counts_per_gram < 0
    assert retared.tare_raw == pytest.approx(evidence.tare_raw)
    assert retared.verification is None
    with pytest.raises(ValueError, match="not stable"):
        workflow.tare(calibrated, [99_000, 101_000] * 5)
