"""Deterministic tests for the mandatory load-cell calibration behavior."""

from __future__ import annotations

import math

import pytest

from processing.loadcell_calibration import (
    CalibrationRejectedError,
    DEFAULT_MAXIMUM_TARE_STD_COUNTS,
    assess_calibration_windows,
    calculate_calibration,
    calculate_counts_per_gram,
    calculate_tare,
    force_N_from_raw,
    force_gf_from_raw,
    grams_force_to_newtons,
    retare,
    verify_calibration,
    build_saved_calibration,
)


def _constant_window(value: float, count: int = 20) -> list[float]:
    return [value] * count


def test_calibration_formula_preserves_positive_scale() -> None:
    calibration = calculate_calibration(
        _constant_window(1_000), _constant_window(11_000), 200
    )

    assert calibration.counts_per_gram == pytest.approx(50.0)
    assert calibration.tare_raw == pytest.approx(1_000.0)
    assert calibration.force_gf(11_000) == pytest.approx(200.0)
    assert calibration.assessment.passed
    assert math.isinf(calibration.assessment.calibration_snr)


def test_negative_calibration_factor_is_not_replaced_by_absolute_value() -> None:
    calibration = calculate_calibration(
        _constant_window(50_000), _constant_window(30_000), 200
    )

    assert calibration.counts_per_gram == pytest.approx(-100.0)
    assert force_gf_from_raw(30_000, 50_000, calibration.counts_per_gram) == pytest.approx(
        200.0
    )
    assert force_gf_from_raw(60_000, 50_000, calibration.counts_per_gram) == pytest.approx(
        -100.0
    )


def test_grams_force_and_raw_force_convert_to_newtons() -> None:
    assert grams_force_to_newtons(200.0) == pytest.approx(1.96133)
    assert force_N_from_raw(3_000, 1_000, 10.0) == pytest.approx(1.96133)


def test_retare_updates_zero_without_changing_signed_scale() -> None:
    calibration = calculate_calibration(
        _constant_window(50_000), _constant_window(30_000), 200
    )
    updated = retare(calibration, [49_998, 50_002] * 5, maximum_std_counts=3)

    assert updated.tare_raw == pytest.approx(50_000.0)
    assert updated.counts_per_gram == calibration.counts_per_gram
    assert updated.force_gf(updated.tare_raw) == pytest.approx(0.0)


def test_tare_rejects_too_few_or_unstable_samples() -> None:
    with pytest.raises(ValueError, match="at least 10"):
        calculate_tare([1_000] * 9)
    with pytest.raises(ValueError, match="not stable"):
        calculate_tare([900, 1_100] * 5, maximum_std_counts=10)


def test_tare_enforces_stability_threshold_by_default() -> None:
    noisy_window = [
        10_000 - DEFAULT_MAXIMUM_TARE_STD_COUNTS - 1,
        10_000 + DEFAULT_MAXIMUM_TARE_STD_COUNTS + 1,
    ] * 5

    with pytest.raises(ValueError, match="not stable"):
        calculate_tare(noisy_window)


def test_verification_passes_at_inclusive_five_percent_boundary() -> None:
    verification = verify_calibration(
        _constant_window(2_900, 10),
        known_mass_g=200,
        tare_raw=1_000,
        counts_per_gram=10,
    )

    assert verification.measured_mean_gf == pytest.approx(190.0)
    assert verification.percentage_error == pytest.approx(5.0)
    assert verification.passed


def test_failed_verification_blocks_when_error_exceeds_five_percent() -> None:
    verification = verify_calibration(
        _constant_window(2_800, 10),
        known_mass_g=200,
        tare_raw=1_000,
        counts_per_gram=10,
    )

    assert verification.percentage_error == pytest.approx(10.0)
    assert not verification.passed
    assert verification.rejection_reasons == (
        "verification_error_above_tolerance",
    )


def test_calibration_rejects_low_snr_and_records_quality_evidence() -> None:
    unloaded = [-50.0, 50.0] * 10
    loaded = [50.0, 150.0] * 10
    assessment = assess_calibration_windows(unloaded, loaded, 200)

    assert assessment.calibration_snr == pytest.approx(2.0)
    assert not assessment.passed
    assert "calibration_snr_below_minimum" in assessment.rejection_reasons
    with pytest.raises(CalibrationRejectedError) as error:
        calculate_calibration(unloaded, loaded, 200)
    assert error.value.assessment == assessment


def test_calibration_rejects_loaded_window_cv_even_when_snr_passes() -> None:
    unloaded = _constant_window(0)
    loaded = [970.0, 1_030.0] * 10
    assessment = assess_calibration_windows(unloaded, loaded, 200)

    assert assessment.calibration_snr > 10
    assert assessment.loaded_window_cv_percent == pytest.approx(3.0)
    assert assessment.rejection_reasons == (
        "loaded_window_cv_above_maximum",
    )


def test_calibration_rejects_each_undersampled_window_explicitly() -> None:
    assessment = assess_calibration_windows(
        _constant_window(0, 19), _constant_window(2_000, 18), 200
    )

    assert not assessment.passed
    assert "insufficient_unloaded_samples" in assessment.rejection_reasons
    assert "insufficient_loaded_samples" in assessment.rejection_reasons


def test_calibration_rejects_zero_or_near_zero_span() -> None:
    assessment = assess_calibration_windows(
        _constant_window(1_000), _constant_window(1_000), 200
    )

    assert not assessment.passed
    assert assessment.calibration_snr == 0.0
    assert "calibration_span_too_small" in assessment.rejection_reasons
    with pytest.raises(ValueError, match="zero or numerically too small"):
        calculate_counts_per_gram(1_000, 1_000, 200)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_calibration_rejects_nonfinite_physical_input(invalid: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        assess_calibration_windows([invalid], [100.0], 200, minimum_sample_count=1)


def test_verification_rejects_too_few_valid_samples_even_when_mean_is_correct() -> None:
    verification = verify_calibration(
        [3_000] * 9,
        known_mass_g=200,
        tare_raw=1_000,
        counts_per_gram=10,
    )

    assert not verification.passed
    assert verification.rejection_reasons == (
        "insufficient_verification_samples",
    )


def test_build_saved_calibration_preserves_all_quality_and_verification_evidence() -> None:
    computation = calculate_calibration(
        _constant_window(1_000), _constant_window(11_000), 200
    )
    verification = verify_calibration(
        _constant_window(11_000),
        known_mass_g=200,
        tare_raw=computation.tare_raw,
        counts_per_gram=computation.counts_per_gram,
    )

    saved = build_saved_calibration(
        "cal-sim-001",
        computation,
        verification,
        serial_port="SIMULATED",
        firmware_identity="HX711_NANO_SIM",
        protocol_version="1.0",
        firmware_version="sim-1.0",
        calibration_timestamp_iso="2026-08-03T00:00:00+00:00",
    )

    assert saved.counts_per_gram == 50.0
    assert saved.calibration_snr == math.inf
    assert saved.loaded_window_mean_gf == pytest.approx(200.0)
    assert saved.minimum_sample_count == 20
    assert saved.quality_passed
    assert saved.verification is not None and saved.verification.passed
    assert saved.verification.minimum_sample_count == 10
