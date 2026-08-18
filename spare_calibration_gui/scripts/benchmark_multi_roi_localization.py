"""Replay the independent ROI gate against grouped single-press evidence.

The frozen archive contains no simultaneous-press labels.  This benchmark can
therefore verify selective exact-set accuracy and false extra boxes on known
single presses, but it deliberately refuses to claim multi-press accuracy.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.experimental_live_sensor import (
    ROI_ACTIVATION_NORMALIZED_FLOOR,
    ROI_ACTIVATION_WARMUP_MARGIN,
    ROI_ACTIVATION_WARMUP_QUANTILE,
)
from scripts.build_experimental_event_signal import _fold_data, _session_weights
from scripts.build_experimental_manual_recovery import (
    ACTIVE_COLUMNS,
    SIGNED_COLUMNS,
    _calibration_and_test_indices,
    _load_inputs,
    _normalizer,
    _normalized_smoothed,
    _outer_frames,
    _training_frames,
)
from scripts.live_sensor_common import PROJECT_ROOT


SPEC_PATH = PROJECT_ROOT / "config" / "manual_only_experimental_event_signal_v3.json"


def _fold_predictions(
    frames: pd.DataFrame,
    fold: int,
    lag_ms: int,
    spec: dict[str, Any],
) -> list[dict[str, object]]:
    fold_metrics, events = _fold_data(frames, fold, lag_ms, spec)
    training = _training_frames(frames, fold).sort_values(
        ["session_id", "video_frame_index"]
    ).reset_index(drop=True)
    outer = _outer_frames(frames, fold).sort_values(
        ["session_id", "video_frame_index"]
    ).reset_index(drop=True)
    no_contact = training[training["scientific_role"] == "no_contact"]
    scale_floor = float(spec["normalization"]["scale_floor"])
    window = int(spec["temporal_filter"]["window_frames"])
    active_center, active_scale = _normalizer(
        no_contact, ACTIVE_COLUMNS, scale_floor
    )
    signed_center, signed_scale = _normalizer(
        no_contact, SIGNED_COLUMNS, scale_floor
    )
    active = _normalized_smoothed(
        outer, ACTIVE_COLUMNS, active_center, active_scale, window
    )
    signed = _normalized_smoothed(
        outer, SIGNED_COLUMNS, signed_center, signed_scale, window
    )
    contact_scores = np.ptp(signed, axis=1)

    outer_no_contact = outer[outer["scientific_role"] == "no_contact"]
    warmup_indices, _ = _calibration_and_test_indices(
        outer_no_contact,
        float(spec["contact"]["warmup_fraction_for_replay"]),
    )
    warmup_high = np.quantile(
        active[warmup_indices],
        ROI_ACTIVATION_WARMUP_QUANTILE,
        axis=0,
        method="higher",
    )
    activation_thresholds = np.maximum(
        ROI_ACTIVATION_NORMALIZED_FLOOR,
        warmup_high + ROI_ACTIVATION_WARMUP_MARGIN,
    )

    primary_mask = outer["scientific_role"].eq("model_primary").to_numpy()
    primary_frames = outer.loc[primary_mask].reset_index(drop=True)
    primary_active = active[primary_mask]
    primary_scores = contact_scores[primary_mask]
    predictions: list[dict[str, object]] = []
    for event in events.itertuples(index=False):
        included = (
            primary_frames["session_id"].astype(str).eq(str(event.session_id)).to_numpy()
            & primary_frames["video_frame_index"].ge(int(event.start_frame_id)).to_numpy()
            & primary_frames["video_frame_index"].le(int(event.end_frame_id)).to_numpy()
            & (primary_scores >= float(fold_metrics["threshold"]))
        )
        if not np.any(included):
            raise RuntimeError("an archived event has no included contact frames")
        event_peak = np.maximum(primary_active[included], 0.0).max(axis=0)
        active_rois = tuple(
            int(index + 1)
            for index in np.flatnonzero(event_peak >= activation_thresholds)
        )
        legacy_rois = (
            (int(event.predicted_roi),)
            if bool(event.localization_retained)
            else ()
        )
        displayed_rois = active_rois or legacy_rois
        predictions.append(
            {
                "session_id": str(event.session_id),
                "target_roi": int(event.target_roi),
                "displayed_rois": displayed_rois,
                "source": (
                    "independent_gate"
                    if active_rois
                    else ("legacy_selective" if legacy_rois else "withheld")
                ),
            }
        )
    return predictions


def evaluate(spec_path: Path = SPEC_PATH) -> dict[str, object]:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    frames, _split, _feature_report, _split_report, lag_by_fold = _load_inputs()
    rows = [
        row
        for fold in range(1, 7)
        for row in _fold_predictions(frames, fold, lag_by_fold[fold], spec)
    ]
    table = pd.DataFrame(rows)
    weights = _session_weights(table)
    shown = table["displayed_rois"].map(bool).to_numpy()
    correct = np.asarray(
        [
            tuple(prediction) == (int(target),)
            for prediction, target in zip(
                table["displayed_rois"], table["target_roi"], strict=True
            )
        ],
        dtype=bool,
    )
    source_counts = table["source"].value_counts().to_dict()
    displayed_weight = float(np.sum(weights[shown]))
    return {
        "schema_version": "1.0.0",
        "evidence_scope": "archived_grouped_single_press_only",
        "event_count": int(len(table)),
        "session_count": int(table["session_id"].nunique()),
        "multi_press_ground_truth_event_count": 0,
        "display_source_counts": {
            str(key): int(value) for key, value in source_counts.items()
        },
        "unweighted_display_coverage": float(np.mean(shown)),
        "unweighted_exact_set_accuracy_when_displayed": float(
            np.mean(correct[shown])
        ),
        "session_balanced_display_coverage": float(
            displayed_weight / np.sum(weights)
        ),
        "session_balanced_exact_set_accuracy_when_displayed": float(
            np.sum(weights[shown & correct]) / displayed_weight
        ),
        "wrong_displayed_event_count": int(np.sum(shown & ~correct)),
        "validation_boundary": (
            "No labeled simultaneous-press events exist; collect independent "
            "multi-press trials before claiming multi-press accuracy."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--minimum-displayed-accuracy", type=float, default=0.99)
    args = parser.parse_args(argv)
    if not 0.0 <= args.minimum_displayed_accuracy <= 1.0:
        parser.error("--minimum-displayed-accuracy must be between zero and one")
    metrics = evaluate(args.spec.resolve())
    print(json.dumps(metrics, indent=2, sort_keys=True))
    accuracy = float(metrics["session_balanced_exact_set_accuracy_when_displayed"])
    return 0 if accuracy >= args.minimum_displayed_accuracy else 1


if __name__ == "__main__":
    raise SystemExit(main())
