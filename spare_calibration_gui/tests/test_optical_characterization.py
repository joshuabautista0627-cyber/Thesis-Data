"""Focused tests for characterization calculations and claim boundaries."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.characterize_optical_sensor import (
    _add_corrected_lights,
    _finite,
    _force_bins,
    _hysteresis,
    _no_contact_noise,
)


def test_nonfinite_values_are_removed_from_strict_json_outputs() -> None:
    assert _finite(float("nan")) is None
    assert _finite(float("inf")) is None
    assert _finite(1.25) == 1.25


def test_force_bins_are_fixed_quarter_newton_intervals() -> None:
    actual = _force_bins(pd.Series([0.0, 0.24, 0.25, 0.50, 2.99]), 0.25)
    np.testing.assert_allclose(actual, [0.125, 0.125, 0.375, 0.625, 2.875])


def test_no_contact_floor_is_session_balanced_and_signed_noise_is_retained() -> None:
    rows = []
    for session_id, offset in (("A", 0.0), ("B", 100.0)):
        for frame_index, signed in enumerate((-2.0, 0.0, 2.0)):
            row = {
                "session_id": session_id,
                "scientific_role": "no_contact",
                "optical_valid": True,
            }
            for roi in range(1, 10):
                row[f"roi{roi}_positive_delta_sum"] = 10.0 + offset
                row[f"roi{roi}_signed_delta_v_sum"] = signed
            rows.append(row)
    frames = pd.DataFrame(rows)

    session, summary, floors, noise = _no_contact_noise(frames)

    assert len(session) == 18
    assert len(summary) == 9
    assert floors[1] == 60.0
    assert noise[1] == 2.0


def test_corrected_light_never_becomes_negative() -> None:
    row = {"target_roi": 1}
    for roi in range(1, 10):
        row[f"roi{roi}_positive_delta_sum"] = float(roi)
        row[f"roi{roi}_area"] = 10
    corrected = _add_corrected_lights(pd.DataFrame([row]), {roi: 5.0 for roi in range(1, 10)})
    assert corrected.loc[0, "light_1"] == 0.0
    assert corrected.loc[0, "light_9"] == 4.0
    assert corrected.loc[0, "target_light"] == 0.0


def test_hysteresis_pairs_loading_and_unloading_inside_each_session() -> None:
    source = pd.DataFrame(
        {
            "archive_id": ["automated"] * 4,
            "session_id": ["A", "A", "B", "B"],
            "target_roi": [1] * 4,
            "speed_mm_min": [200.0] * 4,
            "displacement_mm": [-1.75] * 4,
            "force_bin_N": [1.125] * 4,
            "motion_phase": ["pressing_down", "retracting"] * 2,
            "median_light": [100.0, 80.0, 110.0, 90.0],
        }
    )
    result = _hysteresis(source)
    assert len(result) == 1
    assert result.loc[0, "session_count"] == 2
    assert result.loc[0, "median_hysteresis_light"] == 20.0
