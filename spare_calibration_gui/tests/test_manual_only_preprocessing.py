"""Deterministic unit tests for manual-only fold preprocessing."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from core.live_sensor_contracts import validate_json_schema
from core.timestamp_alignment import align_reference_force_by_timestamp
from scripts.fit_manual_only_preprocessing import (
    _fit_scales,
    _smallest_fpr_threshold,
    _stratified_indices,
    _training_frames,
    pair_lagged_force,
)


PROJECT = Path(__file__).resolve().parents[1]


def test_preprocessing_spec_is_frozen_and_schema_valid() -> None:
    digest = validate_json_schema(
        PROJECT / "config" / "manual_only_preprocessing.json",
        PROJECT / "contracts" / "manual_only_preprocessing.schema.json",
    )
    assert len(digest) == 64

    alignment_digest = validate_json_schema(
        PROJECT / "config" / "manual_timestamp_alignment.json",
        PROJECT / "contracts" / "manual_timestamp_alignment.schema.json",
    )
    assert len(alignment_digest) == 64


def test_positive_lag_pairs_optical_t_with_force_at_t_minus_l() -> None:
    frames = pd.DataFrame(
        {
            "session_id": ["s"] * 4,
            "synchronized_monotonic_relative_s": [0.0, 0.1, 0.2, 0.3],
            "capture_monotonic_relative_s": [0.0, 0.1, 0.2, 0.3],
            "reference_force_corrected_N": [0.0, 1.0, 2.0, 3.0],
        }
    )
    actual = pair_lagged_force(frames, 50)
    assert np.isnan(actual.iloc[0])
    np.testing.assert_allclose(actual.iloc[1:].to_numpy(), [0.5, 1.5, 2.5])


def test_timestamp_alignment_handles_irregular_cadence_without_row_shifting() -> None:
    frames = pd.DataFrame(
        {
            "session_id": ["s"] * 4,
            "synchronized_monotonic_relative_s": [0.0, 0.1, 0.4, 1.0],
            "capture_monotonic_relative_s": [0.05, 0.25, 0.70, 1.10],
            "reference_force_corrected_N": [0.0, 10.0, 40.0, 100.0],
        }
    )
    actual = align_reference_force_by_timestamp(
        frames, 0, maximum_interpolation_gap_ms=310
    )
    np.testing.assert_allclose(actual.iloc[:2].to_numpy(), [5.0, 25.0])
    assert np.isnan(actual.iloc[2])  # 600 ms source gap is not bridged.
    assert np.isnan(actual.iloc[3])  # Extrapolation past the session is forbidden.


def test_timestamp_alignment_never_crosses_session_boundaries() -> None:
    frames = pd.DataFrame(
        {
            "session_id": ["a", "a", "b", "b"],
            "synchronized_monotonic_relative_s": [0.0, 0.1, 0.2, 0.3],
            "capture_monotonic_relative_s": [0.0, 0.1, 0.2, 0.3],
            "reference_force_corrected_N": [1.0, 1.0, 9.0, 9.0],
        }
    )
    actual = align_reference_force_by_timestamp(frames, 50)
    assert np.isnan(actual.iloc[0])
    assert actual.iloc[1] == 1.0
    assert np.isnan(actual.iloc[2])
    assert actual.iloc[3] == 9.0


def test_contact_threshold_is_smallest_candidate_meeting_fpr() -> None:
    threshold, fpr = _smallest_fpr_threshold(
        np.asarray([0.0, 1.0, 2.0, 3.0]), 0.25
    )
    assert threshold == 3.0
    assert fpr == 0.25


def test_outer_training_excludes_held_out_and_replay_sessions() -> None:
    frames = pd.DataFrame(
        {
            "scientific_role": ["model_primary", "model_primary", "no_contact", "no_contact", "replay_only"],
            "outer_fold": [1.0, 2.0, np.nan, np.nan, np.nan],
            "no_contact_fold": [np.nan, np.nan, 1.0, 2.0, np.nan],
            "session_id": ["p1", "p2", "n1", "n2", "replay"],
        }
    )
    assert set(_training_frames(frames, 1)["session_id"]) == {"p2", "n2"}


def test_response_scales_use_all_press_sessions_per_optical_channel() -> None:
    training = pd.DataFrame(
        {
            "session_id": ["roi1", "roi2", "noise1", "noise2"],
            "target_roi": [1, 2, 1, 2],
            **{
                f"roi{roi}_positive_delta_sum": [
                    100.0 + roi,
                    200.0 + roi,
                    10.0 + roi,
                    20.0 + roi,
                ]
                for roi in range(1, 10)
            },
            "scientific_role": [
                "model_primary",
                "model_primary",
                "no_contact",
                "no_contact",
            ],
        }
    )
    force = pd.Series([0.5, 0.5, 0.0, 0.0], index=training.index)
    scales = _fit_scales(
        training,
        force,
        np.zeros(9),
        {
            "scale_force_min_N": 0.25,
            "scale_force_max_N": 1.0,
            "scale_session_quantile": 0.9,
            "noise_scale_session_quantile": 0.99,
            "minimum_scale": 1.0,
        },
    )
    assert scales[1] > 100.0


def test_support_sampling_is_deterministic_and_bounded() -> None:
    frames = pd.DataFrame(
        {
            "frame_uid": [f"f{index}" for index in range(20)],
            "session_id": ["s1"] * 10 + ["s2"] * 10,
            "scientific_role": ["model_primary"] * 20,
            "target_roi": [1] * 20,
        },
        index=np.arange(100, 120),
    )
    force = pd.Series(np.linspace(0.0, 2.0, 20), index=frames.index)
    detected = np.arange(20) >= 5
    first = _stratified_indices(frames, force, detected, 3, 8, 1729)
    detected_by_index = pd.Series(detected, index=frames.index)
    shuffled = frames.sample(frac=1.0, random_state=4)
    second = _stratified_indices(
        shuffled,
        force,
        detected_by_index.loc[shuffled.index].to_numpy(),
        3,
        8,
        1729,
    )
    first_ids = set(frames.iloc[first]["frame_uid"])
    second_ids = set(shuffled.iloc[second]["frame_uid"])
    assert first_ids == second_ids
    assert len(first) <= 8
