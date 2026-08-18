"""Hardware-independent load-cell calibration, verification, and tare workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math
from typing import Iterable
import uuid

from core.models import LoadCellCalibration, LoadCellConfig
from processing.loadcell_calibration import (
    CalibrationAssessment,
    CalibrationComputation,
    TareResult,
    VerificationResult,
    build_saved_calibration,
    calculate_calibration,
    calculate_tare,
    verify_calibration,
)


@dataclass(frozen=True, slots=True)
class CalibrationDeviceIdentity:
    port: str
    device_name: str
    protocol_version: str
    firmware_version: str

    @property
    def firmware_identity(self) -> str:
        return f"{self.device_name}/{self.firmware_version}"


@dataclass(frozen=True, slots=True)
class CalibrationWorkflowResult:
    calibration: LoadCellCalibration
    assessment: CalibrationAssessment
    verification: VerificationResult
    minimum_sample_count_used: int


def minimum_calibration_samples_for_rate(
    measured_sample_rate_hz: float,
    capture_window_s: float,
    *,
    normal_minimum: int = 20,
    fallback_minimum: int = 10,
) -> int:
    """Use ten samples only when the measured rate cannot yield twenty."""

    if not math.isfinite(measured_sample_rate_hz) or measured_sample_rate_hz <= 0:
        raise ValueError("measured_sample_rate_hz must be finite and positive")
    if not math.isfinite(capture_window_s) or capture_window_s <= 0:
        raise ValueError("capture_window_s must be finite and positive")
    if normal_minimum < 1 or not 1 <= fallback_minimum <= normal_minimum:
        raise ValueError("calibration sample minima are inconsistent")
    possible_samples = math.floor(measured_sample_rate_hz * capture_window_s)
    return normal_minimum if possible_samples >= normal_minimum else fallback_minimum


class LoadCellCalibrationWorkflow:
    """Apply the mandated window-quality and known-mass verification gates."""

    def __init__(
        self,
        config: LoadCellConfig,
        device_identity: CalibrationDeviceIdentity,
    ) -> None:
        self.config = config
        self.device_identity = device_identity

    def calibrate_and_verify(
        self,
        unloaded_samples: Iterable[float],
        loaded_samples: Iterable[float],
        verification_samples: Iterable[float],
        *,
        measured_sample_rate_hz: float,
        timestamp_iso: str | None = None,
        calibration_id: str | None = None,
    ) -> CalibrationWorkflowResult:
        minimum_samples = minimum_calibration_samples_for_rate(
            measured_sample_rate_hz,
            self.config.calibration_window_s,
            normal_minimum=self.config.minimum_calibration_samples,
            fallback_minimum=self.config.fallback_minimum_calibration_samples,
        )
        computation: CalibrationComputation = calculate_calibration(
            unloaded_samples,
            loaded_samples,
            self.config.known_mass_g,
            minimum_sample_count=minimum_samples,
            minimum_calibration_snr=self.config.minimum_calibration_snr,
            maximum_loaded_window_cv_percent=(
                self.config.maximum_loaded_window_cv_percent
            ),
            minimum_abs_counts_per_gram=(
                self.config.minimum_abs_counts_per_gram
            ),
        )
        verification = verify_calibration(
            verification_samples,
            self.config.known_mass_g,
            computation.tare_raw,
            computation.counts_per_gram,
            tolerance_percent=self.config.verification_tolerance_percent,
            minimum_sample_count=minimum_samples,
        )
        if not verification.passed:
            reasons = ", ".join(verification.rejection_reasons)
            raise ValueError(f"calibration verification failed: {reasons}")
        captured_at = timestamp_iso or datetime.now(UTC).isoformat()
        saved = build_saved_calibration(
            calibration_id or f"calibration-{uuid.uuid4().hex}",
            computation,
            verification,
            serial_port=self.device_identity.port,
            firmware_identity=self.device_identity.firmware_identity,
            protocol_version=self.device_identity.protocol_version,
            firmware_version=self.device_identity.firmware_version,
            calibration_timestamp_iso=captured_at,
            baud_rate=self.config.baud_rate,
        )
        return CalibrationWorkflowResult(
            calibration=saved,
            assessment=computation.assessment,
            verification=verification,
            minimum_sample_count_used=minimum_samples,
        )

    def tare(
        self,
        calibration: LoadCellCalibration,
        unloaded_samples: Iterable[float],
        *,
        timestamp_iso: str | None = None,
    ) -> tuple[LoadCellCalibration, TareResult]:
        """Update only the unloaded zero after enforcing the configured stability."""

        result = calculate_tare(
            unloaded_samples,
            minimum_sample_count=self.config.fallback_minimum_calibration_samples,
            maximum_std_counts=self.config.maximum_tare_std_counts,
        )
        captured_at = timestamp_iso or datetime.now(UTC).isoformat()
        return calibration.with_tare(result.tare_raw, captured_at), result


__all__ = [
    "CalibrationDeviceIdentity",
    "CalibrationWorkflowResult",
    "LoadCellCalibrationWorkflow",
    "minimum_calibration_samples_for_rate",
]
