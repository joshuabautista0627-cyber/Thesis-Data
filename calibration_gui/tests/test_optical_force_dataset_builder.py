"""Deterministic tests for original-frame feature-store helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from processing.camera_orientation import orient_bgr_frame
from scripts.build_optical_force_dataset import (
    _cycle_force_offsets,
    _orient_quantitative_frame,
)


def test_saved_oriented_frames_are_not_transformed_twice() -> None:
    oriented = np.zeros((640, 480, 3), dtype=np.uint8)
    oriented[10, 20] = (1, 2, 3)

    actual, status = _orient_quantitative_frame(oriented)

    assert status == "already_oriented"
    np.testing.assert_array_equal(actual, oriented)


def test_raw_frames_receive_rotate_then_mirror_exactly_once() -> None:
    raw = np.arange(480 * 640 * 3, dtype=np.uint32).reshape(480, 640, 3)
    raw = (raw % 256).astype(np.uint8)

    actual, status = _orient_quantitative_frame(raw)

    assert status == "oriented_from_raw"
    np.testing.assert_array_equal(
        actual,
        orient_bgr_frame(raw, rotation_degrees=90, mirror_horizontal=True),
    )
    assert actual.shape == (640, 480, 3)


def test_wrong_frame_geometry_fails_closed() -> None:
    actual, status = _orient_quantitative_frame(
        np.zeros((720, 1280, 3), dtype=np.uint8)
    )
    assert actual is None
    assert status == "wrong_geometry:1280x720"


def test_automated_cycle_force_uses_preceding_unloaded_median() -> None:
    master = pd.DataFrame(
        {
            "motion_phase": [
                "pre_roll",
                "pre_roll",
                "pressing_down",
                "holding",
                "retracting",
                "inter_cycle_dwell",
                "inter_cycle_dwell",
                "pressing_down",
                "holding",
                "retracting",
            ],
            "motion_cycle_index": [0, 0, 1, 1, 1, 1, 1, 2, 2, 2],
            "force_N": [0.08, 0.12, 1.10, 1.20, 0.30, 0.18, 0.22, 1.20, 1.30, 0.40],
            "synchronization_valid": [True] * 10,
            "elapsed_time_s": [index * 0.1 for index in range(10)],
        }
    )

    offsets, corrected = _cycle_force_offsets(master, "automated")

    assert offsets[2] == 0.10
    assert corrected[2] == 1.0
    assert offsets[7] == 0.20
    assert corrected[7] == 1.0


def test_manual_force_is_preserved_without_cycle_correction() -> None:
    master = pd.DataFrame(
        {"force_N": [0.1, 1.5], "synchronization_valid": [True, True]}
    )
    offsets, corrected = _cycle_force_offsets(master, "manual")
    assert offsets == [0.0, 0.0]
    assert corrected == [0.1, 1.5]
