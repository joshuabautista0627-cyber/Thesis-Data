"""Session-safe timestamp alignment for optical features and reference labels.

This module is used only while preparing/training calibration data.  The live
camera-only sensor does not receive reference-force values at inference time.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


ALIGNMENT_METHOD = "per_session_linear_timestamp_interpolation"
SESSION_COLUMN = "session_id"
OPTICAL_TIMESTAMP_COLUMN = "capture_monotonic_relative_s"
REFERENCE_TIMESTAMP_COLUMN = "synchronized_monotonic_relative_s"
REFERENCE_VALUE_COLUMN = "reference_force_corrected_N"
DEFAULT_MAXIMUM_INTERPOLATION_GAP_MS = 300.0


def alignment_metadata(
    maximum_interpolation_gap_ms: float = DEFAULT_MAXIMUM_INTERPOLATION_GAP_MS,
) -> dict[str, Any]:
    """Return the machine-readable alignment contract used by model fitting."""

    maximum_gap = _validated_maximum_gap(maximum_interpolation_gap_ms)
    return {
        "method": ALIGNMENT_METHOD,
        "session_column": SESSION_COLUMN,
        "optical_timestamp_column": OPTICAL_TIMESTAMP_COLUMN,
        "reference_timestamp_column": REFERENCE_TIMESTAMP_COLUMN,
        "reference_value_column": REFERENCE_VALUE_COLUMN,
        "lag_convention": "positive_pairs_optical_t_with_reference_t_minus_lag",
        "maximum_interpolation_gap_ms": maximum_gap,
        "allow_cross_session_alignment": False,
        "allow_extrapolation": False,
        "duplicate_reference_timestamp_reducer": "median",
    }


def _validated_maximum_gap(value: float) -> float:
    maximum_gap = float(value)
    if not math.isfinite(maximum_gap) or maximum_gap <= 0.0:
        raise ValueError("maximum_interpolation_gap_ms must be finite and positive")
    return maximum_gap


def align_reference_force_by_timestamp(
    frames: pd.DataFrame,
    lag_ms: float,
    *,
    maximum_interpolation_gap_ms: float = DEFAULT_MAXIMUM_INTERPOLATION_GAP_MS,
) -> pd.Series:
    """Align reference force to each optical capture timestamp within a session.

    Positive ``lag_ms`` pairs an optical feature captured at ``t`` with the
    linearly interpolated reference force at ``t - lag``.  A result is missing
    when the requested time lies outside the session's reference range or its
    two bracketing reference timestamps are farther apart than the configured
    gap.  Alignment never uses row/frame offsets and never crosses sessions.
    """

    required = {
        SESSION_COLUMN,
        OPTICAL_TIMESTAMP_COLUMN,
        REFERENCE_TIMESTAMP_COLUMN,
        REFERENCE_VALUE_COLUMN,
    }
    missing = sorted(required.difference(frames.columns))
    if missing:
        raise ValueError(f"timestamp alignment is missing columns: {missing}")
    if not frames.index.is_unique:
        raise ValueError("timestamp alignment requires a unique frame index")
    lag = float(lag_ms)
    if not math.isfinite(lag):
        raise ValueError("lag_ms must be finite")
    maximum_gap_s = _validated_maximum_gap(maximum_interpolation_gap_ms) / 1000.0

    result = pd.Series(np.nan, index=frames.index, dtype=float)
    for _session_id, group in frames.groupby(SESSION_COLUMN, sort=False, dropna=False):
        source = pd.DataFrame(
            {
                "time": pd.to_numeric(
                    group[REFERENCE_TIMESTAMP_COLUMN], errors="coerce"
                ),
                "force": pd.to_numeric(group[REFERENCE_VALUE_COLUMN], errors="coerce"),
            }
        ).dropna()
        source = source.groupby("time", sort=True, as_index=False)["force"].median()
        if len(source) < 2:
            continue

        source_time = source["time"].to_numpy(float)
        source_force = source["force"].to_numpy(float)
        if not np.isfinite(source_time).all() or not np.isfinite(source_force).all():
            raise ValueError("non-finite reference values survived timestamp validation")
        if np.any(np.diff(source_time) <= 0.0):
            raise ValueError("reference timestamps must increase after duplicate reduction")

        capture_time = pd.to_numeric(
            group[OPTICAL_TIMESTAMP_COLUMN], errors="coerce"
        ).to_numpy(float)
        query_time = capture_time - lag / 1000.0
        finite_query = np.isfinite(query_time)
        right = np.searchsorted(source_time, query_time, side="left")
        clipped_right = np.clip(right, 0, len(source_time) - 1)
        exact = (
            finite_query
            & (right < len(source_time))
            & (source_time[clipped_right] == query_time)
        )
        bracketed = finite_query & (right > 0) & (right < len(source_time))
        bracket_gap = np.full(len(group), np.inf, dtype=float)
        bracket_gap[bracketed] = (
            source_time[right[bracketed]] - source_time[right[bracketed] - 1]
        )
        valid = exact | (bracketed & (bracket_gap <= maximum_gap_s))

        values = np.interp(
            query_time,
            source_time,
            source_force,
            left=np.nan,
            right=np.nan,
        )
        values[~valid] = np.nan
        result.loc[group.index] = values
    return result
