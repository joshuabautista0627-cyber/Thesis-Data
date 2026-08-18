"""Build the post-hoc event-signal and discrete-ROI experimental bundle.

The artifact deliberately reports a unitless optical peak relative to the
unloaded threshold.  It never fits, stores, or exposes a Newton-valued force
model.  Existing manual-only folds remain grouped and are disclosed as reused.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, recall_score

from scripts.build_experimental_manual_recovery import (
    ACTIVE_COLUMNS,
    CONFIG_PATH,
    SIGNED_COLUMNS,
    _calibration_and_test_indices,
    _canonical_hash,
    _debounce,
    _episode_count,
    _fallback_threshold,
    _load_inputs,
    _normalizer,
    _normalized_smoothed,
    _outer_frames,
    _publish,
    _training_frames,
    _write_json,
)
from scripts.fit_manual_only_preprocessing import pair_lagged_force
from scripts.live_sensor_common import PROJECT_ROOT, sha256_file


SCHEMA_VERSION = "2.0.0"
SPEC_PATH = (
    PROJECT_ROOT / "config" / "manual_only_experimental_event_signal_v3.json"
)
RUN_ROOT = (
    PROJECT_ROOT
    / "analysis_outputs"
    / "live_sensor_manual_only"
    / "model_runs"
    / "experimental_event_signal"
)
DEFAULT_BUNDLE = PROJECT_ROOT / "models" / "live_sensor_experimental_event_signal_v3"
LIVE_LAYOUT_PATH = PROJECT_ROOT / "assets" / "live_sensor" / "roi_layout.json"


def _event_rows(
    frames: pd.DataFrame,
    scores: np.ndarray,
    active: np.ndarray,
    threshold: float,
    *,
    acquire_frames: int,
    clear_frames: int,
) -> pd.DataFrame:
    """Segment complete sequences exactly like the event runtime."""

    events: list[dict[str, Any]] = []
    for session_id, indices in frames.groupby("session_id", sort=False).indices.items():
        ordered = np.asarray(indices, dtype=int)
        pretrigger: deque[tuple[int, float, np.ndarray, bool]] = deque(
            maxlen=acquire_frames
        )
        contact_active = False
        positive_count = 0
        negative_count = 0
        accumulator = np.zeros(9, dtype=float)
        peak_ratio = math.nan
        start_frame: int | None = None
        end_frame: int | None = None
        included_frames = 0
        event_index = 0

        def add_sample(index: int, score: float, values: np.ndarray) -> None:
            nonlocal accumulator, peak_ratio, start_frame, end_frame, included_frames
            accumulator += np.maximum(values, 0.0)
            ratio = float(score / threshold)
            peak_ratio = ratio if not math.isfinite(peak_ratio) else max(peak_ratio, ratio)
            frame_id = int(frames.iloc[index]["video_frame_index"])
            start_frame = frame_id if start_frame is None else start_frame
            end_frame = frame_id
            included_frames += 1

        def finish(reason: str) -> None:
            nonlocal accumulator, peak_ratio, start_frame, end_frame, included_frames, event_index
            total = float(np.sum(accumulator))
            if start_frame is not None and end_frame is not None and total > 0.0:
                ordered_values = np.sort(accumulator)
                event_index += 1
                first = frames.iloc[ordered[0]]
                events.append(
                    {
                        "session_id": str(session_id),
                        "target_roi": (
                            int(first["target_roi"])
                            if pd.notna(first["target_roi"])
                            else None
                        ),
                        "event_index": event_index,
                        "start_frame_id": start_frame,
                        "end_frame_id": end_frame,
                        "included_frames": included_frames,
                        "peak_signal_ratio": float(peak_ratio),
                        "predicted_roi": int(np.argmax(accumulator)) + 1,
                        "localization_confidence": float(
                            (ordered_values[-1] - ordered_values[-2]) / total
                        ),
                        "completion_reason": reason,
                    }
                )
            accumulator = np.zeros(9, dtype=float)
            peak_ratio = math.nan
            start_frame = None
            end_frame = None
            included_frames = 0

        for index in ordered:
            score = float(scores[index])
            values = np.asarray(active[index], dtype=float)
            raw_contact = score >= threshold
            pretrigger.append((index, score, values, raw_contact))
            was_active = contact_active
            if raw_contact:
                positive_count += 1
                negative_count = 0
                if not contact_active and positive_count >= acquire_frames:
                    contact_active = True
            else:
                negative_count += 1
                positive_count = 0
                if contact_active and negative_count >= clear_frames:
                    contact_active = False
            entered = not was_active and contact_active
            exited = was_active and not contact_active
            if entered:
                trigger: list[tuple[int, float, np.ndarray, bool]] = []
                for sample in reversed(pretrigger):
                    if not sample[3]:
                        break
                    trigger.append(sample)
                for trigger_index, trigger_score, trigger_active, _ in reversed(trigger):
                    add_sample(trigger_index, trigger_score, trigger_active)
            elif contact_active and raw_contact:
                add_sample(index, score, values)
            if exited:
                finish("contact_cleared")
        if contact_active:
            finish("session_end")
    return pd.DataFrame(events)


def _session_weights(events: pd.DataFrame) -> np.ndarray:
    counts = events.groupby("session_id").size()
    weights = 1.0 / events["session_id"].map(counts).to_numpy(float)
    return weights / max(float(np.mean(weights)), 1e-12)


def _select_confidence_threshold(
    events: pd.DataFrame, minimum_coverage: float
) -> float:
    if events.empty:
        raise ValueError("event confidence requires training events")
    margins = events["localization_confidence"].to_numpy(float)
    truth = events["target_roi"].astype(int).to_numpy()
    prediction = events["predicted_roi"].astype(int).to_numpy()
    weights = _session_weights(events)
    candidates = np.unique(
        np.quantile(margins, np.linspace(0.0, 1.0 - minimum_coverage, 41))
    )
    best_score = -math.inf
    best_threshold = float(candidates[0])
    for threshold in candidates:
        retained = margins >= threshold
        coverage = float(np.sum(weights[retained]) / np.sum(weights))
        if coverage + 1e-12 < minimum_coverage:
            continue
        score = float(
            f1_score(
                truth[retained],
                prediction[retained],
                labels=list(range(1, 10)),
                average="macro",
                sample_weight=weights[retained],
                zero_division=0,
            )
        )
        if score > best_score + 1e-12 or (
            abs(score - best_score) <= 1e-12 and threshold < best_threshold
        ):
            best_score = score
            best_threshold = float(threshold)
    return best_threshold


def _fold_data(
    frames: pd.DataFrame,
    fold: int,
    lag_ms: int,
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], pd.DataFrame]:
    training = _training_frames(frames, fold).sort_values(
        ["session_id", "video_frame_index"]
    ).reset_index(drop=True)
    outer = _outer_frames(frames, fold).sort_values(
        ["session_id", "video_frame_index"]
    ).reset_index(drop=True)
    scale_floor = float(spec["normalization"]["scale_floor"])
    window = int(spec["temporal_filter"]["window_frames"])
    train_no_contact = training[training["scientific_role"] == "no_contact"]
    outer_no_contact = outer[outer["scientific_role"] == "no_contact"]
    signed_center, signed_scale = _normalizer(
        train_no_contact, SIGNED_COLUMNS, scale_floor
    )
    active_center, active_scale = _normalizer(
        train_no_contact, ACTIVE_COLUMNS, scale_floor
    )
    train_signed = _normalized_smoothed(
        training, SIGNED_COLUMNS, signed_center, signed_scale, window
    )
    outer_signed = _normalized_smoothed(
        outer, SIGNED_COLUMNS, signed_center, signed_scale, window
    )
    train_active = _normalized_smoothed(
        training, ACTIVE_COLUMNS, active_center, active_scale, window
    )
    outer_active = _normalized_smoothed(
        outer, ACTIVE_COLUMNS, active_center, active_scale, window
    )
    train_scores = np.ptp(train_signed, axis=1)
    outer_scores = np.ptp(outer_signed, axis=1)
    train_nc_mask = training["scientific_role"].eq("no_contact").to_numpy()
    fallback = _fallback_threshold(
        train_scores[train_nc_mask],
        training.loc[train_nc_mask].reset_index(drop=True),
        0.95,
    )
    calibration_index, test_index = _calibration_and_test_indices(
        outer_no_contact, float(spec["contact"]["warmup_fraction_for_replay"])
    )
    warmup_threshold = float(
        np.quantile(
            outer_scores[calibration_index],
            float(spec["contact"]["unloaded_warmup_quantile"]),
            method="higher",
        )
    )
    threshold = max(fallback, warmup_threshold)
    acquire = int(spec["contact"]["acquire_frames"])
    clear = int(spec["contact"]["clear_frames"])

    training_events = _event_rows(
        training[training["scientific_role"] == "model_primary"].reset_index(drop=True),
        train_scores[training["scientific_role"].eq("model_primary").to_numpy()],
        train_active[training["scientific_role"].eq("model_primary").to_numpy()],
        fallback,
        acquire_frames=acquire,
        clear_frames=clear,
    )
    outer_primary_mask = outer["scientific_role"].eq("model_primary").to_numpy()
    outer_primary = outer.loc[outer_primary_mask].reset_index(drop=True)
    outer_events = _event_rows(
        outer_primary,
        outer_scores[outer_primary_mask],
        outer_active[outer_primary_mask],
        threshold,
        acquire_frames=acquire,
        clear_frames=clear,
    )
    confidence_threshold = _select_confidence_threshold(
        training_events,
        float(spec["localization"]["selective_output"]["minimum_training_coverage"]),
    )
    outer_events["outer_fold"] = fold
    outer_events["confidence_threshold"] = confidence_threshold
    outer_events["localization_retained"] = outer_events[
        "localization_confidence"
    ].ge(confidence_threshold)
    event_weights = _session_weights(outer_events)
    forced_f1 = float(
        f1_score(
            outer_events["target_roi"].astype(int),
            outer_events["predicted_roi"].astype(int),
            labels=list(range(1, 10)),
            average="macro",
            sample_weight=event_weights,
            zero_division=0,
        )
    )
    retained = outer_events["localization_retained"].to_numpy(bool)
    selective_f1 = float(
        f1_score(
            outer_events.loc[retained, "target_roi"].astype(int),
            outer_events.loc[retained, "predicted_roi"].astype(int),
            labels=list(range(1, 10)),
            average="macro",
            sample_weight=event_weights[retained],
            zero_division=0,
        )
    )

    raw_contact = outer_scores >= threshold
    debounced = _debounce(raw_contact, outer["session_id"], acquire, clear)
    force = pair_lagged_force(outer, lag_ms).to_numpy(float)
    operating = spec["evaluation"]["operating_range_N_for_contact_recall_only"]
    eval_mask = (
        outer_primary_mask
        & np.isfinite(force)
        & (force >= float(operating[0]))
        & (force <= float(operating[1]))
    )
    no_contact_test_raw = outer_scores[test_index] >= threshold
    no_contact_test_debounced = _debounce(
        no_contact_test_raw,
        outer.loc[test_index, "session_id"],
        acquire,
        clear,
    )
    duration_s = float(
        sum(
            max(
                float(group["synchronized_monotonic_relative_s"].max())
                - float(group["synchronized_monotonic_relative_s"].min()),
                0.0,
            )
            for _, group in outer.loc[test_index].groupby("session_id", sort=False)
        )
    )
    return (
        {
            "outer_fold": fold,
            "lag_ms": lag_ms,
            "fallback_threshold": float(fallback),
            "warmup_threshold": float(warmup_threshold),
            "threshold": float(threshold),
            "no_contact_test_frames": int(len(test_index)),
            "no_contact_frame_fpr": float(np.mean(no_contact_test_raw)),
            "no_contact_debounced_frame_fpr": float(
                np.mean(no_contact_test_debounced)
            ),
            "false_contact_episodes": _episode_count(no_contact_test_debounced),
            "false_contact_episodes_per_minute": float(
                _episode_count(no_contact_test_debounced)
                / max(duration_s / 60.0, 1e-9)
            ),
            "contact_recall": float(np.mean(debounced[eval_mask])),
            "raw_contact_recall": float(np.mean(raw_contact[eval_mask])),
            "event_count": int(len(outer_events)),
            "session_count": int(outer_events["session_id"].nunique()),
            "event_localization_accuracy": float(
                accuracy_score(
                    outer_events["target_roi"],
                    outer_events["predicted_roi"],
                    sample_weight=event_weights,
                )
            ),
            "event_localization_macro_f1": forced_f1,
            "selective_localization_coverage": float(
                np.sum(event_weights[retained]) / np.sum(event_weights)
            ),
            "selective_localization_macro_f1": selective_f1,
            "selective_localization_threshold": confidence_threshold,
            "mean_peak_signal_ratio": float(
                np.average(outer_events["peak_signal_ratio"], weights=event_weights)
            ),
            "median_detection_delay_frames": float(acquire - 1),
        },
        outer_events,
    )


def _aggregate(folds: list[dict[str, Any]], events: pd.DataFrame) -> dict[str, Any]:
    weights = _session_weights(events)
    retained = events["localization_retained"].to_numpy(bool)
    truth = events["target_roi"].astype(int)
    prediction = events["predicted_roi"].astype(int)
    per_roi = recall_score(
        truth,
        prediction,
        labels=list(range(1, 10)),
        average=None,
        sample_weight=weights,
        zero_division=0,
    )
    return {
        "fold_count": 6,
        "force_output_available": False,
        "signal_unit": "threshold_ratio",
        "mean_no_contact_frame_fpr": float(
            np.mean([row["no_contact_frame_fpr"] for row in folds])
        ),
        "max_no_contact_frame_fpr": float(
            np.max([row["no_contact_frame_fpr"] for row in folds])
        ),
        "mean_contact_recall": float(
            np.mean([row["contact_recall"] for row in folds])
        ),
        "min_contact_recall": float(
            np.min([row["contact_recall"] for row in folds])
        ),
        "event_count": int(len(events)),
        "independent_session_count": int(events["session_id"].nunique()),
        "event_localization_accuracy": float(
            accuracy_score(truth, prediction, sample_weight=weights)
        ),
        "event_localization_macro_f1": float(
            f1_score(
                truth,
                prediction,
                labels=list(range(1, 10)),
                average="macro",
                sample_weight=weights,
                zero_division=0,
            )
        ),
        "selective_localization_coverage": float(
            np.sum(weights[retained]) / np.sum(weights)
        ),
        "selective_localization_macro_f1": float(
            f1_score(
                truth[retained],
                prediction[retained],
                labels=list(range(1, 10)),
                average="macro",
                sample_weight=weights[retained],
                zero_division=0,
            )
        ),
        "per_roi_event_localization_recall": {
            str(roi): float(per_roi[roi - 1]) for roi in range(1, 10)
        },
        "max_false_contact_episodes_per_minute": float(
            np.max([row["false_contact_episodes_per_minute"] for row in folds])
        ),
        "peak_signal_ratio": {
            "minimum": float(events["peak_signal_ratio"].min()),
            "median": float(events["peak_signal_ratio"].median()),
            "maximum": float(events["peak_signal_ratio"].max()),
        },
    }


def _fit_final(frames: pd.DataFrame, spec: Mapping[str, Any]) -> dict[str, Any]:
    eligible = frames[frames["scientific_role"].isin(["model_primary", "no_contact"])].copy()
    eligible = eligible.sort_values(["session_id", "video_frame_index"]).reset_index(drop=True)
    no_contact = eligible[eligible["scientific_role"] == "no_contact"]
    scale_floor = float(spec["normalization"]["scale_floor"])
    window = int(spec["temporal_filter"]["window_frames"])
    signed_center, signed_scale = _normalizer(no_contact, SIGNED_COLUMNS, scale_floor)
    active_center, active_scale = _normalizer(no_contact, ACTIVE_COLUMNS, scale_floor)
    signed = _normalized_smoothed(
        eligible, SIGNED_COLUMNS, signed_center, signed_scale, window
    )
    active = _normalized_smoothed(
        eligible, ACTIVE_COLUMNS, active_center, active_scale, window
    )
    scores = np.ptp(signed, axis=1)
    no_contact_mask = eligible["scientific_role"].eq("no_contact").to_numpy()
    fallback = _fallback_threshold(
        scores[no_contact_mask],
        eligible.loc[no_contact_mask].reset_index(drop=True),
        0.95,
    )
    primary_mask = eligible["scientific_role"].eq("model_primary").to_numpy()
    events = _event_rows(
        eligible.loc[primary_mask].reset_index(drop=True),
        scores[primary_mask],
        active[primary_mask],
        fallback,
        acquire_frames=int(spec["contact"]["acquire_frames"]),
        clear_frames=int(spec["contact"]["clear_frames"]),
    )
    confidence_threshold = _select_confidence_threshold(
        events,
        float(spec["localization"]["selective_output"]["minimum_training_coverage"]),
    )
    return {
        "signed_center": signed_center,
        "signed_scale": signed_scale,
        "active_center": active_center,
        "active_scale": active_scale,
        "fallback_threshold": fallback,
        "localization_confidence_threshold": confidence_threshold,
    }


def _write_demo(
    path: Path,
    frames: pd.DataFrame,
    final: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    no_contact = frames[frames["scientific_role"] == "no_contact"].sort_values(
        ["session_id", "video_frame_index"]
    )
    calibration_session = str(no_contact["session_id"].iloc[0])
    calibration = no_contact[no_contact["session_id"] == calibration_session].head(160)
    replay = (
        frames[frames["scientific_role"] == "replay_only"]
        .sort_values(["session_id", "video_frame_index"])
        .reset_index(drop=True)
    )
    window = int(spec["temporal_filter"]["window_frames"])
    calibration_signed = _normalized_smoothed(
        calibration.reset_index(drop=True),
        SIGNED_COLUMNS,
        np.asarray(final["signed_center"]),
        np.asarray(final["signed_scale"]),
        window,
    )
    replay_signed = _normalized_smoothed(
        replay,
        SIGNED_COLUMNS,
        np.asarray(final["signed_center"]),
        np.asarray(final["signed_scale"]),
        window,
    )
    replay_active = _normalized_smoothed(
        replay,
        ACTIVE_COLUMNS,
        np.asarray(final["active_center"]),
        np.asarray(final["active_scale"]),
        window,
    )
    calibrated_threshold = max(
        float(final["fallback_threshold"]),
        float(
            np.quantile(
                np.ptp(calibration_signed, axis=1),
                float(spec["contact"]["unloaded_warmup_quantile"]),
                method="higher",
            )
        ),
    )
    replay_events = _event_rows(
        replay,
        np.ptp(replay_signed, axis=1),
        replay_active,
        calibrated_threshold,
        acquire_frames=int(spec["contact"]["acquire_frames"]),
        clear_frames=int(spec["contact"]["clear_frames"]),
    )
    confident = replay_events[
        replay_events["localization_confidence"].ge(
            float(final["localization_confidence_threshold"])
        )
    ]
    candidates = confident if not confident.empty else replay_events
    if candidates.empty:
        raise ValueError("replay-only sessions contain no detectable optical event")
    chosen = str(
        candidates.sort_values(
            ["localization_confidence", "peak_signal_ratio"], ascending=False
        )["session_id"].iloc[0]
    )
    contact = replay[replay["session_id"] == chosen].sort_values(
        "video_frame_index"
    ).head(360)
    tail = calibration.tail(10)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        frame_id = 0
        for phase, source in (
            ("unloaded_warmup", calibration),
            ("replay", contact),
            ("replay_clear", tail),
        ):
            for _, row in source.iterrows():
                payload: dict[str, Any] = {
                    "phase": phase,
                    "capture_frame_id": frame_id,
                }
                for column in SIGNED_COLUMNS + ACTIVE_COLUMNS:
                    payload[column] = float(row[column])
                handle.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")
                frame_id += 1
    return {
        "calibration_session_id": calibration_session,
        "replay_session_id": chosen,
        "frame_count": frame_id,
        "model_fit_role": False,
        "trailing_clear_frames": int(len(tail)),
    }


def _write_bundle(
    directory: Path,
    final: Mapping[str, Any],
    metrics: Mapping[str, Any],
    provenance: Mapping[str, Any],
    spec: Mapping[str, Any],
    frames: pd.DataFrame,
) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    live_layout = json.loads(LIVE_LAYOUT_PATH.read_text(encoding="utf-8"))
    orientation = live_layout["camera_orientation"]
    preprocessing = {
        "schema_version": SCHEMA_VERSION,
        "feature_order": {"signed": SIGNED_COLUMNS, "active": ACTIVE_COLUMNS},
        "signed_center": final["signed_center"].tolist(),
        "signed_scale": final["signed_scale"].tolist(),
        "active_center": final["active_center"].tolist(),
        "active_scale": final["active_scale"].tolist(),
        "temporal_filter": spec["temporal_filter"],
        "contact": {
            "score": spec["contact"]["score"],
            "fallback_threshold": float(final["fallback_threshold"]),
            "unloaded_warmup_quantile": spec["contact"]["unloaded_warmup_quantile"],
            "minimum_warmup_frames": spec["contact"]["minimum_warmup_frames"],
            "acquire_frames": spec["contact"]["acquire_frames"],
            "clear_frames": spec["contact"]["clear_frames"],
        },
        "required_layout": {
            "roi_layout_id": live_layout["roi_layout_id"],
            "frame_width": live_layout["frame_width"],
            "frame_height": live_layout["frame_height"],
            "raw_frame_width": live_layout["raw_frame_width"],
            "raw_frame_height": live_layout["raw_frame_height"],
            "rotation_degrees": orientation["rotation_degrees"],
            "mirror_horizontal": orientation["mirror_horizontal"],
        },
    }
    event_model = {
        "schema_version": SCHEMA_VERSION,
        "model_type": "threshold_normalized_peak",
        "input": "filtered_normalized_signed_spatial_range",
        "output": "peak_optical_signal",
        "output_unit": "threshold_ratio",
        "force_output": "unavailable",
        "include_acquisition_buffer": True,
        "accumulate_only_raw_contact_frames": True,
    }
    localization = {
        "schema_version": SCHEMA_VERSION,
        "model_type": "accumulated_positive_active_fraction_argmax",
        "class_order": list(range(1, 10)),
        "input": "sum_max_filtered_normalized_active_fraction_zero",
        "claim": "tentative_event_roi",
        "switch_frames": 2,
        "selective_output_enabled": True,
        "confidence": "top_two_gap_divided_by_total_activity",
        "confidence_margin_threshold": float(
            final["localization_confidence_threshold"]
        ),
        "minimum_training_coverage": float(
            spec["localization"]["selective_output"]["minimum_training_coverage"]
        ),
    }
    aggregate = metrics["aggregate"]
    release = {
        "schema_version": SCHEMA_VERSION,
        "status": "experimental",
        "model_eligible_under_authoritative_plan": False,
        "validated_claim_allowed": False,
        "posthoc_outer_fold_reuse": True,
        "physical_validation_complete": False,
        "usability_validation_complete": False,
        "force_output_available": False,
        "numerical_recovery_checks": {
            "max_no_contact_frame_fpr_at_most_0_05": aggregate[
                "max_no_contact_frame_fpr"
            ]
            <= 0.05,
            "min_contact_recall_at_least_0_90": aggregate["min_contact_recall"]
            >= 0.90,
            "event_localization_macro_f1_at_least_0_891": aggregate[
                "event_localization_macro_f1"
            ]
            >= float(spec["evaluation"]["minimum_event_localization_macro_f1"]),
            "force_output_absent": True,
        },
        "blocking_reasons": [
            "Existing outer folds were reused post-hoc; no untouched confirmation data remain.",
            "No event-level calibration peaks exist in the declared 1.7-3.0 N band.",
            "Known-force physical, soak, and representative-user validation were not performed.",
        ],
    }
    _write_json(directory / "preprocessing.json", preprocessing)
    _write_json(directory / "event_model.json", event_model)
    _write_json(directory / "localization_model.json", localization)
    _write_json(directory / "metrics.json", metrics)
    _write_json(directory / "release_decision.json", release)
    demo = _write_demo(directory / "replay_demo.jsonl", frames, final, spec)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": spec["bundle_id"],
        "sensor_mode": "event_signal",
        "status": "experimental",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": provenance,
        "spec_hash": _canonical_hash(spec),
        "runtime": "reviewed_numpy_json",
        "manual_only": True,
        "replay_demo": demo,
        "files": {},
    }
    for path in sorted(directory.iterdir()):
        if path.name in {"manifest.json", "SHA256SUMS"}:
            continue
        manifest["files"][path.name] = {
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    _write_json(directory / "manifest.json", manifest)
    hashes = [
        f"{sha256_file(path)}  {path.name}"
        for path in sorted(directory.iterdir())
        if path.name != "SHA256SUMS"
    ]
    (directory / "SHA256SUMS").write_text(
        "\n".join(hashes) + "\n", encoding="utf-8"
    )


def build(
    bundle_path: Path = DEFAULT_BUNDLE, spec_path: Path = SPEC_PATH
) -> tuple[Path, Path]:
    study = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    manual_path = Path(study["sources"]["manual"]["archive_path"])
    expected_hash = str(study["sources"]["manual"]["expected_sha256"])
    before_hash = sha256_file(manual_path)
    if before_hash != expected_hash:
        raise ValueError("manual archive hash does not match the frozen allowlist")
    frames, _split, feature_report, split_report, lag_by_fold = _load_inputs()
    fold_metrics: list[dict[str, Any]] = []
    event_frames: list[pd.DataFrame] = []
    for fold in range(1, 7):
        fold_result, events = _fold_data(
            frames, fold, lag_by_fold[fold], spec
        )
        fold_metrics.append(fold_result)
        event_frames.append(events)
    events = pd.concat(event_frames, ignore_index=True)
    aggregate = _aggregate(fold_metrics, events)
    metrics = {
        "schema_version": SCHEMA_VERSION,
        "status": "posthoc_experimental",
        "disclosure": spec["disclosure"],
        "aggregate": aggregate,
        "folds": fold_metrics,
    }
    final = _fit_final(frames, spec)
    provenance = {
        "dataset_policy": "manual_only",
        "manual_archive_sha256": before_hash,
        "source_manifest_hash": feature_report["source_manifest_hash"],
        "split_hash": split_report["split_hash"],
        "feature_spec_hash": feature_report["feature_spec_hash"],
        "roi_layout_id": json.loads(
            LIVE_LAYOUT_PATH.read_text(encoding="utf-8")
        )["roi_layout_id"],
        "roi_layout_sha256": sha256_file(LIVE_LAYOUT_PATH),
        "force_output": "unavailable",
    }
    run_hash = hashlib.sha256(
        json.dumps(
            {"spec": _canonical_hash(spec), **provenance}, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()[:16]
    run_id = f"experimental-event-signal-{run_hash}"
    run_directory = RUN_ROOT / run_id
    if run_directory.exists():
        existing = json.loads(
            (run_directory / "run_manifest.json").read_text(encoding="utf-8")
        )
        if (
            existing.get("run_id") != run_id
            or existing.get("source") != provenance
            or existing.get("spec_hash") != _canonical_hash(spec)
        ):
            raise FileExistsError(f"conflicting immutable run already exists: {run_directory}")
    else:
        RUN_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=run_id + "-", dir=RUN_ROOT) as temporary:
            temp_run = Path(temporary)
            events.to_parquet(temp_run / "outer_event_predictions.parquet", index=False)
            events.to_csv(temp_run / "outer_event_predictions.csv", index=False)
            _write_json(temp_run / "metrics.json", metrics)
            _write_json(
                temp_run / "run_manifest.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "status": "posthoc_experimental",
                    "source": provenance,
                    "spec_hash": _canonical_hash(spec),
                    "manual_archive_hash_before": before_hash,
                    "manual_archive_hash_after": sha256_file(manual_path),
                },
            )
            os.replace(temp_run, run_directory)
    if sha256_file(manual_path) != before_hash:
        raise RuntimeError("manual archive changed during model construction")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=bundle_path.name + "-", dir=bundle_path.parent
    ) as temporary:
        temp_bundle = Path(temporary) / "bundle"
        _write_bundle(temp_bundle, final, metrics, provenance, spec, frames)
        _publish(temp_bundle, bundle_path)
    return run_directory, bundle_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run, bundle = build(args.bundle.resolve(), args.spec.resolve())
    print(json.dumps({"run": str(run), "bundle": str(bundle)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
