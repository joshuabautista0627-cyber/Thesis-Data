"""Gate 1 tests for configuration and domain contracts."""

from __future__ import annotations

import json
import math
from dataclasses import replace

import numpy as np
import pytest

from core.models import (
    ApplicationConfig,
    BaselineRecord,
    CalibrationVerification,
    ErrorCode,
    FrameRecord,
    LoadCellCalibration,
    LoadCellSample,
    ROI,
    ROIBaseline,
    ReadinessFlags,
    RecordingLifecycle,
    TrialLabels,
    ValidityFlag,
    ValidityFlags,
)


def _roi_baselines() -> tuple[ROIBaseline, ...]:
    return tuple(
        ROIBaseline(
            roi_id=roi_id,
            median_v_image=np.full((2, 3), 10 + roi_id, dtype=np.uint8),
            mean_v_image=np.full((2, 3), 10.5 + roi_id, dtype=np.float32),
            circular_mean_h=20.0,
            mean_s=30.0,
            mean_v=40.0,
            median_v=39.0,
            std_v=1.0,
            valid_frame_count=20,
            capture_timestamp_iso="2026-08-03T00:00:00+00:00",
        )
        for roi_id in range(1, 10)
    )


def _calibration(counts_per_gram: float = 100.0) -> LoadCellCalibration:
    return LoadCellCalibration(
        calibration_id="cal-001",
        counts_per_gram=counts_per_gram,
        tare_raw=1000.0,
        known_mass_g=200.0,
        unloaded_mean_raw=1000.0,
        unloaded_std_raw=2.0,
        unloaded_sample_count=20,
        loaded_mean_raw=1000.0 + counts_per_gram * 200.0,
        loaded_std_raw=3.0,
        loaded_sample_count=20,
        calibration_span_counts=abs(counts_per_gram * 200.0),
        calibration_noise_counts=3.0,
        calibration_snr=abs(counts_per_gram * 200.0) / 3.0,
        loaded_window_mean_gf=200.0,
        loaded_window_std_gf=0.03,
        loaded_window_cv_percent=0.015,
        minimum_sample_count=20,
        minimum_calibration_snr=10.0,
        maximum_loaded_window_cv_percent=2.0,
        minimum_abs_counts_per_gram=1.0e-9,
        quality_passed=True,
        serial_port="COM7",
        firmware_identity="HX711_NANO/1.0.0",
        protocol_version="1.0",
        firmware_version="1.0.0",
        calibration_timestamp_iso="2026-08-03T00:00:00+00:00",
        tare_timestamp_iso="2026-08-03T00:00:00+00:00",
        verification=CalibrationVerification(
            expected_gf=200.0,
            measured_mean_gf=199.0,
            absolute_error_gf=1.0,
            percentage_error=0.5,
            measured_std_gf=0.4,
            tolerance_percent=5.0,
            sample_count=20,
            minimum_sample_count=10,
            passed=True,
        ),
    )


def test_recording_lifecycle_is_separate_five_state_contract() -> None:
    assert [state.value for state in RecordingLifecycle] == [
        "IDLE",
        "RECORDING",
        "FINALIZING",
        "COMPLETE",
        "ERROR",
    ]


def test_readiness_requires_every_independent_flag() -> None:
    readiness = ReadinessFlags()
    assert not readiness.ready_to_record
    assert readiness.missing_requirements == ReadinessFlags.REQUIRED_FIELDS

    for field_name in ReadinessFlags.REQUIRED_FIELDS:
        setattr(readiness, field_name, True)
    assert readiness.ready_to_record
    assert readiness.missing_requirements == ()
    assert readiness.as_dict()["ready_to_record"] is True

    readiness.baseline_valid = False
    assert not readiness.ready_to_record
    assert readiness.missing_guidance() == {
        "baseline_valid": "Capture a new unloaded baseline."
    }


def test_named_validity_flags_have_predictable_bitmask() -> None:
    flags = ValidityFlags(
        frame_valid=True,
        baseline_valid=False,
        loadcell_valid=True,
        synchronization_valid=False,
        saturation_warning=True,
    )
    assert flags.bitmask & ValidityFlag.FRAME_VALID
    assert not flags.bitmask & ValidityFlag.BASELINE_VALID
    assert flags.bitmask & ValidityFlag.LOAD_CELL_VALID
    assert flags.bitmask & ValidityFlag.SATURATION_WARNING


def test_roi_geometry_uses_full_frame_upper_left_convention() -> None:
    roi = ROI(roi_id=1, x=10, y=20, width=8, height=6)
    assert roi.area == 48
    assert roi.center_x == 14.0
    assert roi.center_y == 23.0
    assert roi.is_inside(18, 26)
    assert not roi.is_inside(17, 26)
    assert not ROI(2, -1, 0, 4, 4).is_inside(100, 100)
    assert roi.overlaps(ROI(2, 17, 25, 3, 3))
    assert not roi.overlaps(ROI(2, 18, 20, 3, 3))


def test_roi_rejects_nonpositive_size_but_not_out_of_frame_coordinates() -> None:
    with pytest.raises(ValueError, match="positive"):
        ROI(1, 0, 0, 0, 2)
    assert ROI(1, -5, -3, 2, 2).x == -5


def test_baseline_requires_nine_ordered_rois_and_records_invalidation_reason() -> None:
    baseline = BaselineRecord(
        baseline_id="base-001",
        roi_layout_id="layout-001",
        roi_baselines=_roi_baselines(),
        camera_fingerprint="camera-mode-settings-hash",
        processing_fingerprint="threshold-hash",
    )
    assert baseline.valid
    baseline.invalidate("ROI layout changed")
    assert not baseline.valid
    assert baseline.invalid_reason == "ROI layout changed"

    with pytest.raises(ValueError, match="ROI 1..9 in order"):
        BaselineRecord(
            baseline_id="bad",
            roi_layout_id="layout",
            roi_baselines=tuple(reversed(_roi_baselines())),
        )


def test_baseline_arrays_require_matching_nonempty_2d_shapes() -> None:
    kwargs = dict(
        roi_id=1,
        circular_mean_h=math.nan,
        mean_s=0.0,
        mean_v=0.0,
        median_v=0.0,
        std_v=0.0,
        valid_frame_count=20,
        capture_timestamp_iso="2026-08-03T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="equal shapes"):
        ROIBaseline(
            median_v_image=np.zeros((2, 2)),
            mean_v_image=np.zeros((3, 2)),
            **kwargs,
        )


def test_signed_negative_calibration_produces_positive_compressive_force() -> None:
    calibration = _calibration(-100.0)
    assert calibration.counts_per_gram == -100.0
    assert calibration.force_gf(-19_000) == pytest.approx(200.0)
    assert calibration.force_N(-19_000) == pytest.approx(1.96133)


def test_retare_changes_zero_without_changing_signed_scale() -> None:
    calibration = _calibration(-100.0)
    retared = calibration.with_tare(900.0, "2026-08-03T00:01:00+00:00")
    assert retared.tare_raw == 900.0
    assert retared.counts_per_gram == calibration.counts_per_gram
    assert retared.tare_timestamp_iso.endswith("+00:00")


def test_calibration_strict_json_preserves_complete_quality_evidence(tmp_path) -> None:
    calibration = _calibration(-100.0)
    destination = tmp_path / "loadcell_calibration.json"
    calibration.save_json(destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["loaded_window_mean_gf"] == 200.0
    assert payload["loaded_window_std_gf"] == 0.03
    assert payload["minimum_sample_count"] == 20
    assert payload["minimum_abs_counts_per_gram"] == 1.0e-9
    assert payload["verification"]["minimum_sample_count"] == 10
    assert payload["calibration_snr_status"] == "finite"
    assert LoadCellCalibration.load_json(destination) == calibration


def test_infinite_zero_noise_snr_uses_portable_null_and_status(tmp_path) -> None:
    calibration = _calibration(100.0)
    calibration = LoadCellCalibration.from_dict(
        {
            **calibration.to_dict(),
            "calibration_snr": None,
            "calibration_snr_status": "positive_infinity_zero_noise",
            "calibration_noise_counts": 0.0,
            "unloaded_std_raw": 0.0,
            "loaded_std_raw": 0.0,
            "loaded_window_std_gf": 0.0,
            "loaded_window_cv_percent": 0.0,
        }
    )
    destination = tmp_path / "infinite_snr.json"
    calibration.save_json(destination)
    text = destination.read_text(encoding="utf-8")
    assert "Infinity" not in text
    payload = json.loads(text)
    assert payload["calibration_snr"] is None
    assert payload["calibration_snr_status"] == "positive_infinity_zero_noise"
    assert math.isinf(LoadCellCalibration.load_json(destination).calibration_snr)


def test_saved_calibration_rejects_nonfinite_evidence_and_too_small_factor() -> None:
    original = _calibration(100.0)
    with pytest.raises(ValueError, match="tare_raw must be finite"):
        replace(original, tare_raw=math.nan)
    with pytest.raises(ValueError, match="loaded_std_raw must be finite"):
        replace(original, loaded_std_raw=math.inf)
    with pytest.raises(ValueError, match="meet minimum_abs_counts_per_gram"):
        replace(
            original,
            counts_per_gram=100.0,
            minimum_abs_counts_per_gram=101.0,
        )
    payload = original.to_dict()
    payload["calibration_snr"] = math.inf
    payload["calibration_snr_status"] = "finite"
    with pytest.raises(ValueError, match="nonfinite calibration_snr"):
        LoadCellCalibration.from_dict(payload)


def test_saved_calibration_requires_complete_device_and_timestamp_provenance() -> None:
    original = _calibration(100.0)
    for field_name in (
        "serial_port",
        "firmware_identity",
        "protocol_version",
        "firmware_version",
    ):
        with pytest.raises(ValueError, match=field_name):
            replace(original, **{field_name: " "})
    with pytest.raises(ValueError, match="calibration_timestamp_iso"):
        replace(original, calibration_timestamp_iso="2026-08-03T00:00:00")
    with pytest.raises(ValueError, match="tare_timestamp_iso"):
        replace(original, tare_timestamp_iso="not-a-timestamp")


def test_saved_calibration_rejects_inconsistent_quality_and_verification_flags() -> None:
    original = _calibration(100.0)
    with pytest.raises(ValueError, match="quality_passed is inconsistent"):
        replace(original, quality_passed=False)
    verification = original.verification
    assert verification is not None
    with pytest.raises(ValueError, match="passed flag is inconsistent"):
        replace(verification, passed=False)
    with pytest.raises(ValueError, match="rejection_reasons are inconsistent"):
        replace(
            verification,
            passed=False,
            rejection_reasons=("verification_error_above_tolerance",),
        )


def test_saved_calibration_rejects_tampered_loaded_window_statistics() -> None:
    original = _calibration(100.0)
    with pytest.raises(ValueError, match="loaded_window_mean_gf is inconsistent"):
        replace(original, loaded_window_mean_gf=1.0)
    with pytest.raises(ValueError, match="loaded_window_std_gf is inconsistent"):
        replace(original, loaded_window_std_gf=0.0)
    with pytest.raises(ValueError, match="loaded_window_cv_percent is inconsistent"):
        replace(original, loaded_window_cv_percent=0.0)


def test_retare_invalidates_prior_known_mass_verification() -> None:
    original = _calibration(-100.0)
    assert original.verification is not None and original.verification.passed
    retared = original.with_tare(900.0, "2026-08-03T00:01:00+00:00")
    assert retared.verification is None


def test_trial_labels_are_explicitly_trial_scoped() -> None:
    labels = TrialLabels("session-1", "trial-1", "skin-1", 9)
    exported = labels.as_export_dict()
    assert exported["trial_label_scope"] == "trial"
    assert "contact_state_derived" not in exported
    with pytest.raises(ValueError, match="1..9"):
        TrialLabels("session-1", "trial-1", "skin-1", 10)


def test_load_sample_preserves_physical_and_host_identities() -> None:
    sample = LoadCellSample(
        host_monotonic_ns=12_345,
        arduino_sample_id=7,
        arduino_micros=4_294_967_000,
        raw_adc=-123,
        force_gf=2.0,
        force_N=0.0196133,
        device_session_id="device-session-2",
    )
    assert sample.sample_id == 7
    assert sample.valid
    assert sample.error_code is ErrorCode.NONE


def test_frame_record_preserves_row_when_features_are_missing() -> None:
    record = FrameRecord(
        capture_frame_id=3,
        host_monotonic_ns=100,
        elapsed_time_s=0.1,
        wall_clock_iso="2026-08-03T00:00:00+00:00",
        requested_fps=30.0,
        actual_fps=29.8,
        width=640,
        height=480,
        camera_backend="DirectShow",
        camera_device_index=0,
        validity=ValidityFlags(frame_valid=True, baseline_valid=False),
        error_code=ErrorCode.FEATURE_EXTRACTION_FAILED,
    )
    assert record.capture_frame_id == 3
    assert record.roi_features == ()
    assert record.error_code is ErrorCode.FEATURE_EXTRACTION_FAILED


def test_default_configuration_json_loads_and_round_trips(tmp_path) -> None:
    config = ApplicationConfig.load_json("config/default_config.json")
    assert config.camera.warmup_seconds == 3.0
    assert config.camera.baseline_drift_mean_v == 5.0
    assert config.processing.minimum_saturation_for_h == 10
    assert config.loadcell.known_mass_g == 200.0
    assert config.loadcell.max_sync_gap_ms == 200.0
    assert config.loadcell.minimum_abs_counts_per_gram == 1.0e-9
    assert config.loadcell.maximum_tare_std_counts == 100.0
    assert config.recording.preview_queue_size == 1
    assert config.visualization.temporal_history_s == 30.0

    destination = tmp_path / "saved_config.json"
    config.save_json(destination)
    reloaded = ApplicationConfig.load_json(destination)
    assert reloaded == config
    assert json.loads(destination.read_text(encoding="utf-8"))["schema_version"] == "1.1.0"
    assert reloaded.printer.port == "COM4"
    assert reloaded.printer.baud_rate == 115200
