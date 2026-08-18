"""Load-cell calibration, tare, force conversion, and verification formulas.

This module is intentionally independent of Qt and serial I/O.  Acquisition code
passes already-validated physical HX711 samples into these deterministic helpers.
The signed calibration direction is preserved throughout: a load cell whose raw
count decreases under compression has a negative ``counts_per_gram`` and still
reports a positive force for a correctly applied calibration mass.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from statistics import fmean, pstdev
from typing import Iterable, Protocol, runtime_checkable

from core.models import CalibrationVerification, LoadCellCalibration


GRAM_FORCE_TO_NEWTON = 0.00980665
DEFAULT_KNOWN_MASS_G = 200.0
DEFAULT_MINIMUM_CALIBRATION_SNR = 10.0
DEFAULT_MAXIMUM_LOADED_WINDOW_CV_PERCENT = 2.0
DEFAULT_VERIFICATION_TOLERANCE_PERCENT = 5.0
DEFAULT_MINIMUM_CALIBRATION_SAMPLES = 20
FALLBACK_MINIMUM_CALIBRATION_SAMPLES = 10
DEFAULT_MINIMUM_ABS_COUNTS_PER_GRAM = 1.0e-9
# Raw HX711 noise is hardware dependent.  This conservative starting limit is
# deliberately exposed as a configuration-compatible constant; deployments may
# tune it from measured unloaded diagnostics, but production tare never accepts an
# arbitrarily noisy window merely because the caller omitted a threshold.
DEFAULT_MAXIMUM_TARE_STD_COUNTS = 100.0


class CalibrationRejectedError(ValueError):
    """Raised when calibration windows do not meet explicit quality criteria."""

    def __init__(self, assessment: "CalibrationAssessment") -> None:
        self.assessment = assessment
        reasons = ", ".join(assessment.rejection_reasons)
        super().__init__(f"calibration rejected: {reasons}")


@runtime_checkable
class CalibrationLike(Protocol):
    """Structural type needed by conversion and retare helpers."""

    counts_per_gram: float
    tare_raw: float


@dataclass(frozen=True, slots=True)
class CalibrationAssessment:
    """Complete, serializable quality evidence for two calibration windows."""

    unloaded_mean_raw: float
    unloaded_std_raw: float
    unloaded_sample_count: int
    loaded_mean_raw: float
    loaded_std_raw: float
    loaded_sample_count: int
    known_mass_g: float
    calibration_span_counts: float
    calibration_noise_counts: float
    calibration_snr: float
    preliminary_counts_per_gram: float
    loaded_window_mean_gf: float
    loaded_window_std_gf: float
    loaded_window_cv_percent: float
    minimum_sample_count: int
    minimum_calibration_snr: float
    maximum_loaded_window_cv_percent: float
    minimum_abs_counts_per_gram: float
    passed: bool
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CalibrationComputation:
    """Accepted signed scale and zero together with its quality evidence."""

    counts_per_gram: float
    tare_raw: float
    known_mass_g: float
    assessment: CalibrationAssessment

    def force_gf(self, raw_adc: float) -> float:
        """Convert a signed raw ADC value to grams-force."""

        return force_gf_from_raw(raw_adc, self.tare_raw, self.counts_per_gram)

    def force_N(self, raw_adc: float) -> float:
        """Convert a signed raw ADC value to Newtons."""

        return force_N_from_raw(raw_adc, self.tare_raw, self.counts_per_gram)


@dataclass(frozen=True, slots=True)
class TareResult:
    """Statistics for a stable unloaded tare window."""

    tare_raw: float
    std_raw: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Known-mass verification evidence used to gate recording readiness."""

    expected_gf: float
    measured_mean_gf: float
    absolute_error_gf: float
    percentage_error: float
    measured_std_gf: float
    tolerance_percent: float
    sample_count: int
    minimum_sample_count: int
    passed: bool
    rejection_reasons: tuple[str, ...]


def _coerce_finite(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a real number, not bool")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a real number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _sample_window(samples: Iterable[float], name: str) -> tuple[float, ...]:
    values = tuple(_coerce_finite(value, f"{name} sample") for value in samples)
    if not values:
        raise ValueError(f"{name} sample window must not be empty")
    return values


def _validate_positive(value: float, name: str) -> float:
    result = _coerce_finite(value, name)
    if result <= 0.0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def grams_force_to_newtons(force_gf: float) -> float:
    """Convert grams-force to Newtons using standard gravity."""

    return _coerce_finite(force_gf, "force_gf") * GRAM_FORCE_TO_NEWTON


def gf_to_newtons(force_gf: float) -> float:
    """Alias for :func:`grams_force_to_newtons`."""

    return grams_force_to_newtons(force_gf)


def calculate_counts_per_gram(
    unloaded_mean_raw: float,
    loaded_mean_raw: float,
    known_mass_g: float = DEFAULT_KNOWN_MASS_G,
    *,
    minimum_abs_counts_per_gram: float = DEFAULT_MINIMUM_ABS_COUNTS_PER_GRAM,
) -> float:
    """Calculate the signed HX711 scale factor and reject a negligible span."""

    unloaded = _coerce_finite(unloaded_mean_raw, "unloaded_mean_raw")
    loaded = _coerce_finite(loaded_mean_raw, "loaded_mean_raw")
    mass = _validate_positive(known_mass_g, "known_mass_g")
    minimum_factor = _validate_positive(
        minimum_abs_counts_per_gram, "minimum_abs_counts_per_gram"
    )
    counts_per_gram = (loaded - unloaded) / mass
    if abs(counts_per_gram) < minimum_factor:
        raise ValueError("calibration factor is zero or numerically too small")
    return counts_per_gram


def force_gf_from_raw(
    raw_adc: float,
    tare_raw: float,
    counts_per_gram: float,
    *,
    minimum_abs_counts_per_gram: float = DEFAULT_MINIMUM_ABS_COUNTS_PER_GRAM,
) -> float:
    """Convert a raw ADC count to grams-force without changing its sign."""

    raw = _coerce_finite(raw_adc, "raw_adc")
    tare = _coerce_finite(tare_raw, "tare_raw")
    factor = _coerce_finite(counts_per_gram, "counts_per_gram")
    minimum_factor = _validate_positive(
        minimum_abs_counts_per_gram, "minimum_abs_counts_per_gram"
    )
    if abs(factor) < minimum_factor:
        raise ValueError("counts_per_gram is zero or numerically too small")
    return (raw - tare) / factor


def raw_to_force_gf(
    raw_adc: float, tare_raw: float, counts_per_gram: float
) -> float:
    """Alias for :func:`force_gf_from_raw`."""

    return force_gf_from_raw(raw_adc, tare_raw, counts_per_gram)


def force_N_from_raw(
    raw_adc: float,
    tare_raw: float,
    counts_per_gram: float,
    *,
    minimum_abs_counts_per_gram: float = DEFAULT_MINIMUM_ABS_COUNTS_PER_GRAM,
) -> float:
    """Convert a raw ADC count directly to Newtons."""

    force_gf = force_gf_from_raw(
        raw_adc,
        tare_raw,
        counts_per_gram,
        minimum_abs_counts_per_gram=minimum_abs_counts_per_gram,
    )
    return grams_force_to_newtons(force_gf)


def raw_to_force_N(
    raw_adc: float, tare_raw: float, counts_per_gram: float
) -> float:
    """Alias for :func:`force_N_from_raw`."""

    return force_N_from_raw(raw_adc, tare_raw, counts_per_gram)


def assess_calibration_windows(
    unloaded_samples: Iterable[float],
    loaded_samples: Iterable[float],
    known_mass_g: float = DEFAULT_KNOWN_MASS_G,
    *,
    minimum_sample_count: int = DEFAULT_MINIMUM_CALIBRATION_SAMPLES,
    minimum_calibration_snr: float = DEFAULT_MINIMUM_CALIBRATION_SNR,
    maximum_loaded_window_cv_percent: float = (
        DEFAULT_MAXIMUM_LOADED_WINDOW_CV_PERCENT
    ),
    minimum_abs_counts_per_gram: float = DEFAULT_MINIMUM_ABS_COUNTS_PER_GRAM,
) -> CalibrationAssessment:
    """Evaluate sample count, span, SNR, and loaded-window stability.

    ``minimum_sample_count`` defaults to the specified 20 samples.  A caller may
    pass ``FALLBACK_MINIMUM_CALIBRATION_SAMPLES`` (10) only after determining
    that the measured HX711 rate cannot provide 20 samples in the capture window.
    All quality metrics are returned even when the windows are rejected.
    """

    unloaded = _sample_window(unloaded_samples, "unloaded")
    loaded = _sample_window(loaded_samples, "loaded")
    mass = _validate_positive(known_mass_g, "known_mass_g")
    if isinstance(minimum_sample_count, bool) or not isinstance(
        minimum_sample_count, int
    ):
        raise TypeError("minimum_sample_count must be an integer")
    if minimum_sample_count <= 0:
        raise ValueError("minimum_sample_count must be greater than zero")
    minimum_snr = _validate_positive(
        minimum_calibration_snr, "minimum_calibration_snr"
    )
    maximum_cv = _validate_positive(
        maximum_loaded_window_cv_percent,
        "maximum_loaded_window_cv_percent",
    )
    minimum_factor = _validate_positive(
        minimum_abs_counts_per_gram, "minimum_abs_counts_per_gram"
    )

    unloaded_mean = fmean(unloaded)
    loaded_mean = fmean(loaded)
    unloaded_std = pstdev(unloaded)
    loaded_std = pstdev(loaded)
    signed_span = loaded_mean - unloaded_mean
    span = abs(signed_span)
    noise = max(unloaded_std, loaded_std)
    if span == 0.0:
        snr = 0.0
    elif noise == 0.0:
        snr = math.inf
    else:
        snr = span / noise

    preliminary_factor = signed_span / mass
    if abs(preliminary_factor) >= minimum_factor:
        loaded_forces = tuple(
            (raw - unloaded_mean) / preliminary_factor for raw in loaded
        )
        loaded_mean_gf = fmean(loaded_forces)
        loaded_std_gf = pstdev(loaded_forces)
        loaded_cv = (
            math.inf
            if loaded_mean_gf == 0.0
            else abs(loaded_std_gf / loaded_mean_gf) * 100.0
        )
    else:
        loaded_mean_gf = math.nan
        loaded_std_gf = math.nan
        loaded_cv = math.inf

    rejection_reasons: list[str] = []
    if len(unloaded) < minimum_sample_count:
        rejection_reasons.append("insufficient_unloaded_samples")
    if len(loaded) < minimum_sample_count:
        rejection_reasons.append("insufficient_loaded_samples")
    if abs(preliminary_factor) < minimum_factor:
        rejection_reasons.append("calibration_span_too_small")
    if snr < minimum_snr:
        rejection_reasons.append("calibration_snr_below_minimum")
    if loaded_cv > maximum_cv:
        rejection_reasons.append("loaded_window_cv_above_maximum")

    return CalibrationAssessment(
        unloaded_mean_raw=unloaded_mean,
        unloaded_std_raw=unloaded_std,
        unloaded_sample_count=len(unloaded),
        loaded_mean_raw=loaded_mean,
        loaded_std_raw=loaded_std,
        loaded_sample_count=len(loaded),
        known_mass_g=mass,
        calibration_span_counts=span,
        calibration_noise_counts=noise,
        calibration_snr=snr,
        preliminary_counts_per_gram=preliminary_factor,
        loaded_window_mean_gf=loaded_mean_gf,
        loaded_window_std_gf=loaded_std_gf,
        loaded_window_cv_percent=loaded_cv,
        minimum_sample_count=minimum_sample_count,
        minimum_calibration_snr=minimum_snr,
        maximum_loaded_window_cv_percent=maximum_cv,
        minimum_abs_counts_per_gram=minimum_factor,
        passed=not rejection_reasons,
        rejection_reasons=tuple(rejection_reasons),
    )


def assess_calibration_quality(
    unloaded_samples: Iterable[float],
    loaded_samples: Iterable[float],
    known_mass_g: float = DEFAULT_KNOWN_MASS_G,
    **criteria: float | int,
) -> CalibrationAssessment:
    """Alias for :func:`assess_calibration_windows`."""

    return assess_calibration_windows(
        unloaded_samples, loaded_samples, known_mass_g, **criteria
    )


def calculate_calibration(
    unloaded_samples: Iterable[float],
    loaded_samples: Iterable[float],
    known_mass_g: float = DEFAULT_KNOWN_MASS_G,
    *,
    minimum_sample_count: int = DEFAULT_MINIMUM_CALIBRATION_SAMPLES,
    minimum_calibration_snr: float = DEFAULT_MINIMUM_CALIBRATION_SNR,
    maximum_loaded_window_cv_percent: float = (
        DEFAULT_MAXIMUM_LOADED_WINDOW_CV_PERCENT
    ),
    minimum_abs_counts_per_gram: float = DEFAULT_MINIMUM_ABS_COUNTS_PER_GRAM,
) -> CalibrationComputation:
    """Return an accepted calibration or raise :class:`CalibrationRejectedError`."""

    assessment = assess_calibration_windows(
        unloaded_samples,
        loaded_samples,
        known_mass_g,
        minimum_sample_count=minimum_sample_count,
        minimum_calibration_snr=minimum_calibration_snr,
        maximum_loaded_window_cv_percent=maximum_loaded_window_cv_percent,
        minimum_abs_counts_per_gram=minimum_abs_counts_per_gram,
    )
    if not assessment.passed:
        raise CalibrationRejectedError(assessment)
    return CalibrationComputation(
        counts_per_gram=assessment.preliminary_counts_per_gram,
        tare_raw=assessment.unloaded_mean_raw,
        known_mass_g=assessment.known_mass_g,
        assessment=assessment,
    )


def calculate_tare(
    unloaded_samples: Iterable[float],
    *,
    minimum_sample_count: int = FALLBACK_MINIMUM_CALIBRATION_SAMPLES,
    maximum_std_counts: float | None = DEFAULT_MAXIMUM_TARE_STD_COUNTS,
) -> TareResult:
    """Average an unloaded window and enforce an absolute raw-noise limit.

    The default limit is :data:`DEFAULT_MAXIMUM_TARE_STD_COUNTS`.  Passing a
    configured positive value is recommended after measuring a particular HX711;
    ``None`` is retained only for explicit diagnostic use where stability gating
    is intentionally performed by the caller.
    """

    samples = _sample_window(unloaded_samples, "tare")
    if isinstance(minimum_sample_count, bool) or not isinstance(
        minimum_sample_count, int
    ):
        raise TypeError("minimum_sample_count must be an integer")
    if minimum_sample_count <= 0:
        raise ValueError("minimum_sample_count must be greater than zero")
    if len(samples) < minimum_sample_count:
        raise ValueError(
            f"tare requires at least {minimum_sample_count} valid samples"
        )
    std_raw = pstdev(samples)
    if maximum_std_counts is not None:
        maximum_std = _validate_positive(maximum_std_counts, "maximum_std_counts")
        if std_raw > maximum_std:
            raise ValueError("tare window is not stable")
    return TareResult(
        tare_raw=fmean(samples), std_raw=std_raw, sample_count=len(samples)
    )


def retare(
    calibration: CalibrationComputation,
    unloaded_samples: Iterable[float],
    *,
    minimum_sample_count: int = FALLBACK_MINIMUM_CALIBRATION_SAMPLES,
    maximum_std_counts: float | None = DEFAULT_MAXIMUM_TARE_STD_COUNTS,
) -> CalibrationComputation:
    """Return a calibration with a new zero and an unchanged signed scale."""

    tare = calculate_tare(
        unloaded_samples,
        minimum_sample_count=minimum_sample_count,
        maximum_std_counts=maximum_std_counts,
    )
    return replace(calibration, tare_raw=tare.tare_raw)


def verify_calibration(
    raw_samples: Iterable[float],
    known_mass_g: float,
    tare_raw: float,
    counts_per_gram: float,
    *,
    tolerance_percent: float = DEFAULT_VERIFICATION_TOLERANCE_PERCENT,
    minimum_sample_count: int = FALLBACK_MINIMUM_CALIBRATION_SAMPLES,
) -> VerificationResult:
    """Verify a calibration with the known mass using an inclusive +/- tolerance."""

    samples = _sample_window(raw_samples, "verification")
    expected = _validate_positive(known_mass_g, "known_mass_g")
    tolerance = _validate_positive(tolerance_percent, "tolerance_percent")
    if isinstance(minimum_sample_count, bool) or not isinstance(
        minimum_sample_count, int
    ):
        raise TypeError("minimum_sample_count must be an integer")
    if minimum_sample_count <= 0:
        raise ValueError("minimum_sample_count must be greater than zero")

    forces_gf = tuple(
        force_gf_from_raw(raw, tare_raw, counts_per_gram) for raw in samples
    )
    measured = fmean(forces_gf)
    measured_std = pstdev(forces_gf)
    absolute_error = abs(measured - expected)
    percentage_error = absolute_error / expected * 100.0
    reasons: list[str] = []
    if len(samples) < minimum_sample_count:
        reasons.append("insufficient_verification_samples")
    if percentage_error > tolerance:
        reasons.append("verification_error_above_tolerance")
    return VerificationResult(
        expected_gf=expected,
        measured_mean_gf=measured,
        absolute_error_gf=absolute_error,
        percentage_error=percentage_error,
        measured_std_gf=measured_std,
        tolerance_percent=tolerance,
        sample_count=len(samples),
        minimum_sample_count=minimum_sample_count,
        passed=not reasons,
        rejection_reasons=tuple(reasons),
    )


def build_saved_calibration(
    calibration_id: str,
    computation: CalibrationComputation,
    verification: VerificationResult | None,
    *,
    serial_port: str,
    firmware_identity: str,
    protocol_version: str,
    firmware_version: str,
    calibration_timestamp_iso: str,
    tare_timestamp_iso: str | None = None,
    baud_rate: int = 0,
) -> LoadCellCalibration:
    """Convert accepted calculation evidence into the persisted contract.

    ``verification=None`` represents the required provisional state between an
    accepted factor calculation and the post-tare known-mass verification.
    """

    if not calibration_id.strip():
        raise ValueError("calibration_id must not be blank")
    assessment = computation.assessment
    return LoadCellCalibration(
        calibration_id=calibration_id.strip(),
        counts_per_gram=computation.counts_per_gram,
        tare_raw=computation.tare_raw,
        known_mass_g=computation.known_mass_g,
        unloaded_mean_raw=assessment.unloaded_mean_raw,
        unloaded_std_raw=assessment.unloaded_std_raw,
        unloaded_sample_count=assessment.unloaded_sample_count,
        loaded_mean_raw=assessment.loaded_mean_raw,
        loaded_std_raw=assessment.loaded_std_raw,
        loaded_sample_count=assessment.loaded_sample_count,
        calibration_span_counts=assessment.calibration_span_counts,
        calibration_noise_counts=assessment.calibration_noise_counts,
        calibration_snr=assessment.calibration_snr,
        loaded_window_mean_gf=assessment.loaded_window_mean_gf,
        loaded_window_std_gf=assessment.loaded_window_std_gf,
        loaded_window_cv_percent=assessment.loaded_window_cv_percent,
        minimum_sample_count=assessment.minimum_sample_count,
        minimum_calibration_snr=assessment.minimum_calibration_snr,
        maximum_loaded_window_cv_percent=(
            assessment.maximum_loaded_window_cv_percent
        ),
        minimum_abs_counts_per_gram=assessment.minimum_abs_counts_per_gram,
        quality_passed=assessment.passed,
        rejection_reasons=assessment.rejection_reasons,
        serial_port=serial_port,
        firmware_identity=firmware_identity,
        protocol_version=protocol_version,
        firmware_version=firmware_version,
        calibration_timestamp_iso=calibration_timestamp_iso,
        tare_timestamp_iso=tare_timestamp_iso or calibration_timestamp_iso,
        verification=(
            None
            if verification is None
            else CalibrationVerification(
                expected_gf=verification.expected_gf,
                measured_mean_gf=verification.measured_mean_gf,
                absolute_error_gf=verification.absolute_error_gf,
                percentage_error=verification.percentage_error,
                measured_std_gf=verification.measured_std_gf,
                tolerance_percent=verification.tolerance_percent,
                sample_count=verification.sample_count,
                minimum_sample_count=verification.minimum_sample_count,
                passed=verification.passed,
                rejection_reasons=verification.rejection_reasons,
            )
        ),
        baud_rate=int(baud_rate),
    )


__all__ = [
    "CalibrationAssessment",
    "CalibrationComputation",
    "CalibrationLike",
    "CalibrationRejectedError",
    "DEFAULT_KNOWN_MASS_G",
    "DEFAULT_MAXIMUM_LOADED_WINDOW_CV_PERCENT",
    "DEFAULT_MAXIMUM_TARE_STD_COUNTS",
    "DEFAULT_MINIMUM_ABS_COUNTS_PER_GRAM",
    "DEFAULT_MINIMUM_CALIBRATION_SAMPLES",
    "DEFAULT_MINIMUM_CALIBRATION_SNR",
    "DEFAULT_VERIFICATION_TOLERANCE_PERCENT",
    "FALLBACK_MINIMUM_CALIBRATION_SAMPLES",
    "GRAM_FORCE_TO_NEWTON",
    "TareResult",
    "VerificationResult",
    "assess_calibration_quality",
    "assess_calibration_windows",
    "build_saved_calibration",
    "calculate_calibration",
    "calculate_counts_per_gram",
    "calculate_tare",
    "force_N_from_raw",
    "force_gf_from_raw",
    "gf_to_newtons",
    "grams_force_to_newtons",
    "raw_to_force_N",
    "raw_to_force_gf",
    "retare",
    "verify_calibration",
]
