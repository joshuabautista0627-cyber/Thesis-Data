"""Deterministic tests for host-clock synchronization and micros rollover."""

from __future__ import annotations

import math

import pytest

import processing.synchronization as synchronization_module
from processing.synchronization import (
    ARDUINO_MICROS_MODULUS,
    ArduinoClockEvent,
    ArduinoMicrosTracker,
    classify_arduino_clock_transition,
    synchronize_frame_to_force,
    synchronize_frames_to_force,
    unwrap_arduino_micros,
)


def _sample(
    timestamp_ns: int,
    sample_id: int,
    raw_adc: float,
    force_gf: float,
    force_N: float,
    *,
    micros: int | None = None,
    device_session_id: str = "device-1",
    valid: bool = True,
) -> dict[str, object]:
    return {
        "host_monotonic_ns": timestamp_ns,
        "arduino_sample_id": sample_id,
        "arduino_micros": sample_id * 10_000 if micros is None else micros,
        "raw_adc": raw_adc,
        "force_gf": force_gf,
        "force_N": force_N,
        "device_session_id": device_session_id,
        "loadcell_valid": valid,
    }


def test_linear_interpolation_uses_host_monotonic_timestamps() -> None:
    samples = [
        _sample(1_000_000_000, 10, 1_000, 0, 0),
        _sample(1_100_000_000, 11, 3_000, 200, 1.96133),
    ]
    result = synchronize_frame_to_force(1_025_000_000, samples)

    assert result.synchronization_valid
    assert result.synchronization_method == "linear_interpolation"
    assert result.interpolated_raw_adc == pytest.approx(1_500)
    assert result.force_gf == pytest.approx(50)
    assert result.force_N == pytest.approx(1.96133 * 0.25)
    assert result.closest_arduino_sample_id == 10
    assert result.closest_raw_adc == 1_000
    assert result.nearest_sample_gap_ms == pytest.approx(25)
    assert result.contact_state_derived is True


def test_nearest_sample_fallback_is_allowed_within_maximum_gap() -> None:
    samples = [_sample(1_000_000_000, 10, 2_000, 100, 0.980665)]
    result = synchronize_frame_to_force(
        1_150_000_000, samples, max_sync_gap_ms=200
    )

    assert result.synchronization_valid
    assert result.synchronization_method == "nearest"
    assert result.nearest_sample_gap_ms == pytest.approx(150)
    assert result.interpolated_raw_adc == result.closest_raw_adc == 2_000


def test_maximum_gap_rejection_preserves_frame_row_as_nan() -> None:
    samples = [_sample(1_000_000_000, 10, 2_000, 100, 0.980665)]
    result = synchronize_frame_to_force(
        1_201_000_000, samples, max_sync_gap_ms=200
    )

    assert not result.synchronization_valid
    assert result.synchronization_method == "invalid"
    assert result.closest_arduino_sample_id == 10
    assert result.closest_raw_adc == 2_000
    assert result.nearest_sample_gap_ms == pytest.approx(201)
    assert math.isnan(result.force_gf)
    assert math.isnan(result.force_N)
    assert math.isnan(result.interpolated_raw_adc)
    assert math.isnan(result.contact_state_derived)


def test_interpolation_is_rejected_across_long_sample_outage() -> None:
    samples = [
        _sample(0, 1, 0, 0, 0),
        _sample(1_000_000_000, 2, 1_000, 100, 0.980665),
    ]
    result = synchronize_frame_to_force(
        500_000_000, samples, max_sync_gap_ms=200
    )

    assert not result.synchronization_valid
    assert math.isnan(result.force_N)


def test_samples_from_different_device_sessions_are_not_interpolated() -> None:
    samples = [
        _sample(1_000_000_000, 99, 1_000, 0, 0, device_session_id="before"),
        _sample(1_100_000_000, 0, 3_000, 200, 1.96133, device_session_id="after"),
    ]
    result = synchronize_frame_to_force(1_025_000_000, samples)

    assert result.synchronization_valid
    assert result.synchronization_method == "nearest"
    assert result.closest_device_session_id == "before"


def test_exact_timestamp_uses_physical_sample_without_interpolation() -> None:
    samples = [
        _sample(1_000_000_000, 10, 1_000, 0, 0),
        _sample(1_100_000_000, 11, 3_000, 200, 1.96133),
    ]
    result = synchronize_frame_to_force(1_100_000_000, samples)

    assert result.synchronization_method == "nearest"
    assert result.nearest_sample_gap_ms == 0
    assert result.closest_arduino_sample_id == 11
    assert result.force_gf == 200


def test_nan_force_sample_is_ignored_and_unavailable_force_stays_nan() -> None:
    samples = [_sample(1_000, 1, 123, math.nan, math.nan)]
    result = synchronize_frame_to_force(1_000, samples)

    assert not result.synchronization_valid
    assert math.isnan(result.force_N)
    assert math.isnan(result.contact_state_derived)


def test_batch_synchronization_preserves_input_order_and_nan_frame() -> None:
    samples = [_sample(1_000, 1, 123, 1, 0.01)]
    results = synchronize_frames_to_force([1_000, math.nan, 1_001], samples)

    assert len(results) == 3
    assert results[0].synchronization_valid
    assert not results[1].synchronization_valid
    assert results[2].synchronization_valid
    assert results[0].frame_host_monotonic_ns == 1_000
    assert math.isnan(results[1].frame_host_monotonic_ns)


def test_large_batch_normalizes_samples_once_and_matches_scalar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = [
        _sample(
            1_000_000_000 + sample_id * 10_000_000,
            sample_id,
            10_000 + sample_id * 2,
            float(sample_id),
            sample_id * 0.01,
        )
        for sample_id in range(256)
    ]
    frame_timestamps = [
        1_000_000_000 + frame_id * 5_000_000 for frame_id in range(511)
    ]
    scalar_reference = [
        synchronize_frame_to_force(timestamp, samples)
        for timestamp in frame_timestamps[:9]
    ]

    original_normalize = synchronization_module._normalize_sample
    normalization_calls = 0

    def counting_normalize(sample: object):  # type: ignore[no-untyped-def]
        nonlocal normalization_calls
        normalization_calls += 1
        return original_normalize(sample)

    monkeypatch.setattr(
        synchronization_module, "_normalize_sample", counting_normalize
    )
    batch_results = synchronize_frames_to_force(frame_timestamps, samples)

    assert normalization_calls == len(samples)
    assert len(batch_results) == len(frame_timestamps)
    assert batch_results[:9] == scalar_reference
    assert all(result.synchronization_valid for result in batch_results)


def test_contact_state_is_force_derived_and_nan_when_unsynchronized() -> None:
    below = synchronize_frame_to_force(
        1_000, [_sample(1_000, 1, 0, 0, 0.049)], contact_threshold_N=0.05
    )
    at_threshold = synchronize_frame_to_force(
        1_000, [_sample(1_000, 1, 0, 0, 0.05)], contact_threshold_N=0.05
    )
    unavailable = synchronize_frame_to_force(1_000, [])

    assert below.contact_state_derived is False
    assert at_threshold.contact_state_derived is True
    assert math.isnan(unavailable.contact_state_derived)


def test_micros_rollover_is_distinguished_from_reset() -> None:
    event = classify_arduino_clock_transition(
        100,
        ARDUINO_MICROS_MODULUS - 5,
        101,
        7,
    )
    assert event is ArduinoClockEvent.ROLLOVER

    tracker = ArduinoMicrosTracker()
    first = tracker.update(100, ARDUINO_MICROS_MODULUS - 5)
    rolled = tracker.update(101, 7)
    assert first.event is ArduinoClockEvent.INITIAL
    assert rolled.event is ArduinoClockEvent.ROLLOVER
    assert rolled.unwrapped_micros == ARDUINO_MICROS_MODULUS + 7
    assert not rolled.new_device_session


def test_sample_id_or_nonrollover_micros_reset_starts_new_device_session() -> None:
    tracker = ArduinoMicrosTracker()
    tracker.update(10, 1_000_000)
    sample_id_reset = tracker.update(0, 100)
    assert sample_id_reset.event is ArduinoClockEvent.RESET
    assert sample_id_reset.new_device_session
    assert sample_id_reset.device_session_index == 1
    assert sample_id_reset.unwrapped_micros == 100

    micros_reset = tracker.update(1, 50)
    assert micros_reset.event is ArduinoClockEvent.RESET
    assert micros_reset.device_session_index == 2


def test_duplicate_sample_id_is_reported_without_advancing_tracker() -> None:
    tracker = ArduinoMicrosTracker()
    tracker.update(1, 100)
    duplicate = tracker.update(1, 100)
    following = tracker.update(2, 300)

    assert duplicate.event is ArduinoClockEvent.DUPLICATE_SAMPLE_ID
    assert following.event is ArduinoClockEvent.NORMAL


def test_equal_sample_id_with_decreased_micros_is_a_new_device_session() -> None:
    tracker = ArduinoMicrosTracker()
    tracker.update(7, 10_000)
    reset = tracker.update(7, 5)

    assert reset.event is ArduinoClockEvent.RESET
    assert reset.new_device_session
    assert reset.device_session_index == 1
    assert reset.unwrapped_micros == 5


def test_equal_sample_id_with_increased_micros_is_collision_not_trusted_progress() -> None:
    tracker = ArduinoMicrosTracker()
    tracker.update(7, 100)
    collision = tracker.update(7, 200)
    following = tracker.update(8, 150)

    assert collision.event is ArduinoClockEvent.SAMPLE_ID_COLLISION
    assert not collision.new_device_session
    # The collision did not replace the trusted (ID 7, micros 100) state, so the
    # subsequent legitimate sample remains normal rather than looking like reset.
    assert following.event is ArduinoClockEvent.NORMAL


def test_unwrap_sequence_validates_lengths_and_reports_sessions() -> None:
    updates = unwrap_arduino_micros(
        [ARDUINO_MICROS_MODULUS - 2, 3, 50], [5, 6, 0]
    )
    assert [update.event for update in updates] == [
        ArduinoClockEvent.INITIAL,
        ArduinoClockEvent.ROLLOVER,
        ArduinoClockEvent.RESET,
    ]
    assert updates[-1].device_session_index == 1

    with pytest.raises(ValueError, match="equal lengths"):
        unwrap_arduino_micros([1, 2], [1])


def test_malformed_sample_missing_required_timestamp_is_rejected_clearly() -> None:
    with pytest.raises(ValueError, match="missing required field"):
        synchronize_frame_to_force(
            1_000,
            [
                {
                    "arduino_sample_id": 1,
                    "arduino_micros": 1,
                    "raw_adc": 1,
                    "force_gf": 1,
                    "force_N": 1,
                }
            ],
        )
