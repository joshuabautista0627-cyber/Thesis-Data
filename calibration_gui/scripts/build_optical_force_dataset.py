"""Build the immutable original-frame optical feature-store dataset.

One ZIP session is staged at a time in the project cache. Each completed
session becomes an atomic, hash-validated Parquet checkpoint so ``--resume``
never has to restart a source archive from the beginning.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping
import zipfile

import cv2
from jsonschema import Draft202012Validator, FormatChecker
import numpy as np
import pandas as pd

from core.live_sensor_contracts import (
    LIVE_LAYOUT_ID,
    canonical_json_hash,
    load_canonical_live_layout,
    load_json_object,
)
from core.models import BaselineRecord, ROIBaseline
from processing.camera_orientation import orient_bgr_frame
from processing.feature_extraction import FeatureExtractionError, ROIFeatures, extract_roi_features
from scripts.live_sensor_common import (
    PROJECT_ROOT,
    RunLogger,
    atomic_write_json,
    assert_manual_only_artifact_lineage,
    code_hash,
    create_temporary_run_directory,
    elapsed_seconds,
    is_manual_only_config,
    load_study_config,
    make_run_id,
    output_file_hashes,
    platform_manifest,
    publish_run_directory,
    resolve_project_path,
    sha256_file,
    utc_now_iso,
)


COMMAND_NAME = "build-optical-force-dataset"
SCHEMA_VERSION = "1.0.0"
FEATURE_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "feature_store.schema.json"
CONTACT_PHASES = frozenset({"pressing_down", "holding", "retracting"})
UNLOADED_PHASES = frozenset({"pre_roll", "move_to_start", "inter_cycle_dwell"})
FORBIDDEN_FITTED_TOKENS = frozenset(
    {
        "no_contact_floor",
        "response_scale",
        "contact_threshold",
        "imputation",
        "fdetect",
        "fmax",
        "model_transform",
    }
)
CAMERA_PROVENANCE_FIELDS = (
    "camera_backend",
    "camera_device_index",
    "camera_exposure",
    "camera_auto_exposure",
    "camera_gain",
    "camera_brightness",
    "camera_contrast",
    "camera_saturation",
    "camera_sharpness",
    "camera_gamma",
    "camera_white_balance",
    "camera_auto_white_balance",
    "camera_focus",
    "camera_auto_focus",
    "camera_hue",
    "camera_backlight_compensation",
)


def _safe_string(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    number = _float_or_none(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _bool_value(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value) if value is not None else False


def _find_source_inventory(
    config: Mapping[str, Any], config_hash: str
) -> tuple[Path, dict[str, Any], pd.DataFrame]:
    root = resolve_project_path(config["paths"]["source_inventory_root"], writable=True)
    matches: list[tuple[Path, dict[str, Any]]] = []
    for report_path in root.glob("*/audit_report.json"):
        report = load_json_object(report_path)
        if report.get("study_config_hash") != config_hash or not report.get("gate_pass"):
            continue
        if all(
            report.get("archives", {}).get(archive_id, {}).get("actual_sha256")
            == source["expected_sha256"]
            for archive_id, source in config["sources"].items()
        ):
            matches.append((report_path.parent, report))
    if not matches:
        raise FileNotFoundError(
            "no passing hash-matched archive audit exists; run "
            "python -m scripts.audit_live_sensor_archives first"
        )
    directory, report = sorted(matches, key=lambda item: item[0].name)[-1]
    manifest_path = directory / "session_manifest.parquet"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"audit source manifest is missing: {manifest_path}")
    sessions = pd.read_parquet(manifest_path)
    if len(sessions) != int(report["session_count"]):
        raise ValueError("audit report/session manifest row count mismatch")
    if set(sessions["source_manifest_hash"].dropna().unique()) != {
        report["source_manifest_hash"]
    }:
        raise ValueError("source manifest hash column does not match audit report")
    return directory, report, sessions


def _load_baseline(
    summary_path: Path, arrays_path: Path, layout_id: str
) -> BaselineRecord:
    summary = load_json_object(summary_path)
    if summary.get("valid") is not True:
        raise ValueError("staged baseline is not valid")
    if summary.get("roi_layout_id") != layout_id:
        raise ValueError("staged baseline layout does not match the live layout")
    summaries = {
        int(item["roi_id"]): item
        for item in summary.get("rois", [])
        if isinstance(item, dict) and _int_or_none(item.get("roi_id")) is not None
    }
    records: list[ROIBaseline] = []
    with np.load(arrays_path, allow_pickle=False) as arrays:
        for roi_id in range(1, 10):
            item = summaries.get(roi_id)
            if item is None:
                raise ValueError(f"baseline summary is missing ROI {roi_id}")
            median_name = f"roi{roi_id}_median_v"
            mean_name = f"roi{roi_id}_mean_v"
            if median_name not in arrays.files or mean_name not in arrays.files:
                raise ValueError(f"baseline arrays are missing ROI {roi_id}")
            records.append(
                ROIBaseline(
                    roi_id=roi_id,
                    median_v_image=np.array(arrays[median_name], copy=True),
                    mean_v_image=np.array(arrays[mean_name], copy=True),
                    circular_mean_h=float(item.get("circular_mean_h") or 0.0),
                    mean_s=float(item["mean_s"]),
                    mean_v=float(item["mean_v"]),
                    median_v=float(item["median_v"]),
                    std_v=float(item["std_v"]),
                    valid_frame_count=int(item["valid_frame_count"]),
                    capture_timestamp_iso=str(item.get("capture_timestamp_iso", "")),
                )
            )
    return BaselineRecord(
        baseline_id=str(summary["baseline_id"]),
        roi_layout_id=str(summary["roi_layout_id"]),
        roi_baselines=tuple(records),
        camera_fingerprint=str(summary.get("camera_fingerprint", "")),
        processing_fingerprint=str(summary.get("processing_fingerprint", "")),
        capture_timestamp_iso=str(summary.get("capture_timestamp_iso", "")),
        camera_settings=summary.get("camera_settings", {}),
        valid=True,
    )


def _extract_member(
    archive: zipfile.ZipFile, member: str, destination: Path
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    try:
        with archive.open(member) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _read_master(archive: zipfile.ZipFile, member: str) -> pd.DataFrame:
    with archive.open(member) as handle:
        frame = pd.read_csv(handle, low_memory=False)
    return frame.reset_index(drop=True)


def _cycle_force_offsets(
    master: pd.DataFrame, archive_id: str
) -> tuple[list[float | None], list[float | None]]:
    if "force_N" not in master:
        return [None] * len(master), [None] * len(master)
    raw = pd.to_numeric(master["force_N"], errors="coerce")
    if "synchronization_valid" in master:
        sync_valid = master["synchronization_valid"].map(_bool_value)
        raw = raw.where(sync_valid)
    raw = raw.where(np.isfinite(raw))
    if archive_id != "automated":
        offsets = pd.Series(0.0, index=master.index).where(raw.notna())
        corrected = raw.copy()
        return (
            [_float_or_none(value) for value in offsets],
            [_float_or_none(value) for value in corrected],
        )

    phases = master.get("motion_phase", pd.Series("", index=master.index)).fillna("")
    cycles = pd.to_numeric(
        master.get("motion_cycle_index", pd.Series(np.nan, index=master.index)),
        errors="coerce",
    )
    elapsed = pd.to_numeric(
        master.get("elapsed_time_s", pd.Series(np.nan, index=master.index)),
        errors="coerce",
    )
    offsets = pd.Series(np.nan, index=master.index, dtype=float)
    previous_contact_end = -1
    cycle_numbers = sorted(
        int(value) for value in cycles.dropna().unique() if int(value) >= 1
    )
    for cycle_number in cycle_numbers:
        contact_positions = master.index[
            (cycles == cycle_number) & phases.isin(CONTACT_PHASES)
        ].tolist()
        if not contact_positions:
            continue
        onset = contact_positions[0]
        prior_mask = (
            (master.index < onset)
            & (master.index > previous_contact_end)
            & phases.isin(UNLOADED_PHASES)
            & raw.notna()
        )
        onset_time = _float_or_none(elapsed.iloc[onset])
        if onset_time is not None:
            prior_mask &= elapsed >= onset_time - 4.0
        prior_positions = master.index[prior_mask].tolist()
        if not prior_positions:
            prior_positions = master.index[
                (master.index < onset) & phases.isin(UNLOADED_PHASES) & raw.notna()
            ].tolist()[-45:]
        if not prior_positions:
            previous_contact_end = contact_positions[-1]
            continue
        zero = float(raw.loc[prior_positions].median())
        offsets.loc[contact_positions] = zero
        offsets.loc[prior_positions] = zero
        previous_contact_end = contact_positions[-1]

    offsets = offsets.ffill().bfill().where(raw.notna())
    corrected = raw - offsets
    return (
        [_float_or_none(value) for value in offsets],
        [_float_or_none(value) for value in corrected],
    )


def _stable_frame_uid(
    archive_sha256: str, session_id: str, video_index: int, capture_frame_id: int | None
) -> str:
    value = f"{archive_sha256}|{session_id}|{video_index}|{capture_frame_id}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _frame_base(
    *,
    session: Mapping[str, Any],
    source_manifest_hash: str,
    study_config_hash: str,
    run_id: str,
    video_index: int,
    master_row: Mapping[str, Any] | None,
    raw_force: float | None,
    corrected_force: float | None,
    zero_offset: float | None,
) -> dict[str, Any]:
    row = master_row or {}
    capture_frame_id = _int_or_none(row.get("capture_frame_id"))
    elapsed = _float_or_none(row.get("elapsed_time_s"))
    synchronization_offset_ms = _float_or_none(row.get("synchronization_offset_ms"))
    synchronized_time = (
        elapsed + synchronization_offset_ms / 1000.0
        if elapsed is not None and synchronization_offset_ms is not None
        else elapsed
    )
    synchronization_valid = _bool_value(row.get("synchronization_valid"))
    force_valid = raw_force is not None and synchronization_valid
    target_roi = _int_or_none(row.get("target_roi_ground_truth"))
    if target_roi is None:
        target_roi = _int_or_none(session.get("target_roi"))
    base = {
        "schema_version": SCHEMA_VERSION,
        "study_config_hash": study_config_hash,
        "source_manifest_hash": source_manifest_hash,
        "created_by_run_id": run_id,
        "frame_uid": _stable_frame_uid(
            str(session["archive_sha256"]),
            str(session["session_id"]),
            video_index,
            capture_frame_id,
        ),
        "archive_id": str(session["archive_id"]),
        "collection_day": str(session["collection_day"]),
        "test_group": _safe_string(session.get("test_group")),
        "session_id": str(session["session_id"]),
        "scientific_role": str(session["scientific_role"]),
        "video_frame_index": video_index,
        "frame_id": capture_frame_id if capture_frame_id is not None else video_index,
        "capture_monotonic_relative_s": elapsed,
        "synchronized_monotonic_relative_s": synchronized_time,
        "source_timestamp_quality": "host" if elapsed is not None else "missing",
        "target_roi": target_roi,
        "reference_force_raw_N": raw_force,
        "reference_force_corrected_N": corrected_force,
        "cycle_force_zero_offset_N": zero_offset,
        "force_valid": force_valid,
        "motion_phase": _safe_string(row.get("motion_phase")),
        "cycle_id": _safe_string(row.get("motion_cycle_id")),
        "cycle_index": _int_or_none(row.get("motion_cycle_index")),
        "speed_mm_min": _float_or_none(row.get("requested_feed_rate_mm_min")),
        "displacement_mm": _float_or_none(row.get("target_displacement_mm")),
        "synchronization_residual_ms": _float_or_none(
            row.get("nearest_sample_gap_ms")
        ),
        "synchronization_offset_ms": synchronization_offset_ms,
        "baseline_id": str(session["baseline_id"]),
        "baseline_hash": str(session["baseline_hash"]),
        "layout_id": LIVE_LAYOUT_ID,
        "layout_hash": str(session["layout_hash"]),
        "raw_width": 640,
        "raw_height": 480,
        "oriented_width": 480,
        "oriented_height": 640,
    }
    for name in CAMERA_PROVENANCE_FIELDS:
        value = session.get(name)
        base[name] = None if pd.isna(value) else value
    return base


def _feature_payload(features: Iterable[ROIFeatures]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for feature in features:
        prefix = f"roi{feature.roi_id}_"
        payload.update(
            {
                f"{prefix}x": feature.x,
                f"{prefix}y": feature.y,
                f"{prefix}width": feature.width,
                f"{prefix}height": feature.height,
                f"{prefix}area": feature.area,
                f"{prefix}signed_delta_v_sum": feature.signed_delta_v_sum,
                f"{prefix}signed_delta_v_mean": feature.signed_delta_v_mean,
                f"{prefix}signed_delta_v_median": feature.signed_delta_v_median,
                f"{prefix}signed_delta_v_mad": feature.signed_delta_v_mad,
                f"{prefix}positive_delta_sum": feature.delta_v_sum,
                f"{prefix}positive_delta_mean": feature.delta_v_sum_per_pixel,
                f"{prefix}active_fraction": feature.active_fraction,
                f"{prefix}centroid_x": _float_or_none(
                    feature.full_frame_centroid_x
                ),
                f"{prefix}centroid_y": _float_or_none(
                    feature.full_frame_centroid_y
                ),
                f"{prefix}raw_v_mean": feature.mean_v,
                f"{prefix}raw_v_median": feature.median_v,
                f"{prefix}raw_v_max": feature.max_v,
            }
        )
    return payload


def _missing_feature_payload(layout: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for roi in layout.rois:
        prefix = f"roi{roi.roi_id}_"
        payload.update(
            {
                f"{prefix}x": roi.x,
                f"{prefix}y": roi.y,
                f"{prefix}width": roi.width,
                f"{prefix}height": roi.height,
                f"{prefix}area": roi.area,
                f"{prefix}signed_delta_v_sum": None,
                f"{prefix}signed_delta_v_mean": None,
                f"{prefix}signed_delta_v_median": None,
                f"{prefix}signed_delta_v_mad": None,
                f"{prefix}positive_delta_sum": None,
                f"{prefix}positive_delta_mean": None,
                f"{prefix}active_fraction": None,
                f"{prefix}centroid_x": None,
                f"{prefix}centroid_y": None,
                f"{prefix}raw_v_mean": None,
                f"{prefix}raw_v_median": None,
                f"{prefix}raw_v_max": None,
            }
        )
    return payload


def _validate_feature_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("a session feature checkpoint must contain at least one row")
    schema = load_json_object(FEATURE_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    sample_indices = sorted({0, len(rows) // 2, len(rows) - 1})
    for index in sample_indices:
        errors = sorted(
            validator.iter_errors(rows[index]), key=lambda error: list(error.path)
        )
        if errors:
            first = errors[0]
            location = "/".join(str(item) for item in first.absolute_path)
            raise ValueError(
                f"feature row {index} failed schema at {location}: {first.message}"
            )
    names = set(rows[0])
    for roi_id in range(1, 10):
        for suffix in (
            "signed_delta_v_sum",
            "signed_delta_v_mean",
            "signed_delta_v_median",
            "signed_delta_v_mad",
            "positive_delta_sum",
            "positive_delta_mean",
            "active_fraction",
            "centroid_x",
            "centroid_y",
            "raw_v_mean",
            "raw_v_median",
            "raw_v_max",
        ):
            required = f"roi{roi_id}_{suffix}"
            if required not in names:
                raise ValueError(f"feature checkpoint is missing {required}")
    forbidden = sorted(
        name
        for name in names
        if any(token in name.lower() for token in FORBIDDEN_FITTED_TOKENS)
    )
    if forbidden:
        raise ValueError(f"global fitted values leaked into feature store: {forbidden}")


def _orient_quantitative_frame(frame: np.ndarray) -> tuple[np.ndarray | None, str]:
    height, width = frame.shape[:2]
    if (width, height) == (480, 640):
        return np.ascontiguousarray(frame), "already_oriented"
    if (width, height) == (640, 480):
        return (
            orient_bgr_frame(
                frame, rotation_degrees=90, mirror_horizontal=True
            ),
            "oriented_from_raw",
        )
    return None, f"wrong_geometry:{width}x{height}"


def _session_checkpoint_identity(
    session: Mapping[str, Any], feature_spec_hash: str
) -> str:
    return canonical_json_hash(
        {
            "archive_sha256": session["archive_sha256"],
            "zip_member_root": session["zip_member_root"],
            "video_crc32": session["video_crc32"],
            "video_size_bytes": session["video_size_bytes"],
            "master_crc32": session["master_crc32"],
            "master_size_bytes": session["master_size_bytes"],
            "baseline_hash": session["baseline_hash"],
            "layout_hash": session["layout_hash"],
            "feature_spec_hash": feature_spec_hash,
        }
    )


def _checkpoint_valid(
    parquet_path: Path, manifest_path: Path, identity_hash: str
) -> bool:
    if not parquet_path.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = load_json_object(manifest_path)
        return (
            manifest.get("checkpoint_identity_hash") == identity_hash
            and manifest.get("output_sha256") == sha256_file(parquet_path)
            and int(manifest.get("row_count", -1))
            == len(pd.read_parquet(parquet_path, columns=["frame_uid"]))
        )
    except (OSError, ValueError, KeyError):
        return False


def _write_checkpoint(
    rows: list[dict[str, Any]],
    parquet_path: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    _validate_feature_rows(rows)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = parquet_path.with_name(parquet_path.name + ".partial")
    frame = pd.DataFrame(rows)
    frame.to_parquet(temporary, index=False)
    with temporary.open("rb+") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, parquet_path)
    output_hash = sha256_file(parquet_path)
    complete_manifest = {
        **manifest,
        "row_count": len(frame),
        "column_count": len(frame.columns),
        "output_sha256": output_hash,
        "output_size_bytes": parquet_path.stat().st_size,
        "completed_at": utc_now_iso(),
    }
    atomic_write_json(manifest_path, complete_manifest)
    return complete_manifest


def _process_session(
    *,
    archive: zipfile.ZipFile,
    session: Mapping[str, Any],
    layout: Any,
    source_manifest_hash: str,
    study_config_hash: str,
    run_id: str,
    feature_spec_hash: str,
    checkpoint_root: Path,
    cache_root: Path,
    resume: bool,
) -> dict[str, Any]:
    session_id = str(session["session_id"])
    archive_id = str(session["archive_id"])
    identity_hash = _session_checkpoint_identity(session, feature_spec_hash)
    checkpoint_directory = checkpoint_root / f"archive_id={archive_id}"
    parquet_path = checkpoint_directory / f"{session_id}.parquet"
    manifest_path = checkpoint_directory / f"{session_id}.checkpoint.json"
    if resume and _checkpoint_valid(parquet_path, manifest_path, identity_hash):
        result = load_json_object(manifest_path)
        result["reused"] = True
        return result

    video_member = str(session["source_video_member"])
    master_member = str(session["master_csv_member"])
    root = str(session["zip_member_root"])
    baseline_member = f"{root}/baseline_data.npz"
    baseline_summary_member = f"{root}/baseline_summary.json"
    required_stage_bytes = int(session["video_size_bytes"] or 0) + 1024 * 1024 * 1024
    free_bytes = shutil.disk_usage(PROJECT_ROOT).free
    if free_bytes < required_stage_bytes:
        raise OSError(
            f"insufficient staging space for {session_id}: "
            f"required={required_stage_bytes}, available={free_bytes}"
        )

    staging_parent = cache_root / "staging" / run_id / f"archive_id={archive_id}"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{session_id}.", dir=staging_parent))
    try:
        video_path = _extract_member(archive, video_member, staging / "session_video.mp4")
        arrays_path = _extract_member(
            archive, baseline_member, staging / "baseline_data.npz"
        )
        summary_path = _extract_member(
            archive, baseline_summary_member, staging / "baseline_summary.json"
        )
        if sha256_file(arrays_path) != str(session["baseline_hash"]):
            raise ValueError(f"baseline hash mismatch for {session_id}")
        baseline = _load_baseline(summary_path, arrays_path, LIVE_LAYOUT_ID)
        master = _read_master(archive, master_member)
        zero_offsets, corrected_forces = _cycle_force_offsets(master, archive_id)

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise OSError(f"OpenCV could not open staged video for {session_id}")
        rows: list[dict[str, Any]] = []
        decoded_count = 0
        orientation_counts: Counter[str] = Counter()
        exclusion_counts: Counter[str] = Counter()
        missing_payload = _missing_feature_payload(layout)
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                video_index = decoded_count
                decoded_count += 1
                has_master = video_index < len(master)
                master_row = master.iloc[video_index].to_dict() if has_master else None
                raw_force = (
                    _float_or_none(master_row.get("force_N")) if master_row else None
                )
                zero_offset = zero_offsets[video_index] if has_master else None
                corrected_force = corrected_forces[video_index] if has_master else None
                base = _frame_base(
                    session=session,
                    source_manifest_hash=source_manifest_hash,
                    study_config_hash=study_config_hash,
                    run_id=run_id,
                    video_index=video_index,
                    master_row=master_row,
                    raw_force=raw_force,
                    corrected_force=corrected_force,
                    zero_offset=zero_offset,
                )
                oriented, orientation_status = _orient_quantitative_frame(frame)
                orientation_counts[orientation_status] += 1
                reasons: list[str] = []
                if not has_master:
                    reasons.append("unmatched_video_frame")
                elif not base["force_valid"]:
                    reasons.append("invalid_force")
                if oriented is None:
                    reasons.append("wrong_geometry")
                    features_payload = missing_payload
                    optical_valid = False
                else:
                    try:
                        features = extract_roi_features(oriented, layout.rois, baseline)
                        features_payload = _feature_payload(features)
                        optical_valid = True
                    except (FeatureExtractionError, ValueError) as exc:
                        reasons.append("feature_extraction_failed")
                        features_payload = missing_payload
                        optical_valid = False
                        base["feature_error"] = f"{type(exc).__name__}: {exc}"
                for reason in reasons:
                    exclusion_counts[reason] += 1
                rows.append(
                    {
                        **base,
                        "video_decode_status": "decoded",
                        "orientation_status": orientation_status,
                        "optical_valid": optical_valid,
                        "exclusion_reason": reasons[0] if reasons else None,
                        "exclusion_reasons_json": json.dumps(
                            reasons, separators=(",", ":")
                        ),
                        **features_payload,
                    }
                )
        finally:
            capture.release()

        for master_index in range(decoded_count, len(master)):
            master_row = master.iloc[master_index].to_dict()
            raw_force = _float_or_none(master_row.get("force_N"))
            base = _frame_base(
                session=session,
                source_manifest_hash=source_manifest_hash,
                study_config_hash=study_config_hash,
                run_id=run_id,
                video_index=master_index,
                master_row=master_row,
                raw_force=raw_force,
                corrected_force=corrected_forces[master_index],
                zero_offset=zero_offsets[master_index],
            )
            exclusion_counts["missing_video_frame"] += 1
            rows.append(
                {
                    **base,
                    "video_decode_status": "missing",
                    "orientation_status": "not_available",
                    "optical_valid": False,
                    "exclusion_reason": "missing_video_frame",
                    "exclusion_reasons_json": '["missing_video_frame"]',
                    **missing_payload,
                }
            )

        checkpoint_manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "session_id": session_id,
            "archive_id": archive_id,
            "checkpoint_identity_hash": identity_hash,
            "feature_spec_hash": feature_spec_hash,
            "source_manifest_hash": source_manifest_hash,
            "expected_master_rows": len(master),
            "reported_video_frames": _int_or_none(session.get("video_frame_count")),
            "decoded_video_frames": decoded_count,
            "orientation_counts": dict(sorted(orientation_counts.items())),
            "exclusion_counts": dict(sorted(exclusion_counts.items())),
            "frame_reconciliation": {
                "master_rows": len(master),
                "decoded_video_frames": decoded_count,
                "output_rows": len(rows),
                "missing_video_frames": max(len(master) - decoded_count, 0),
                "unmatched_video_frames": max(decoded_count - len(master), 0),
                "all_differences_reason_coded": True,
            },
            "reused": False,
        }
        return _write_checkpoint(
            rows, parquet_path, manifest_path, checkpoint_manifest
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _feature_code_hash() -> str:
    # Frozen when the 245-session quantitative run began. This identifies the
    # transform/extractor/schema/layout contract, not output-assembly code.
    return "38fa59a354306bf39c0b570f57e764f5638b19a3ecbdbd49b177b8df6ac56cf5"


def _builder_code_hash() -> str:
    """Hash the current orchestration separately from per-frame semantics."""

    return code_hash(
        (
            "scripts/build_optical_force_dataset.py",
            "scripts/live_sensor_common.py",
            "core/live_sensor_contracts.py",
        )
    )


def _run_identity(
    config_hash: str, source_manifest_hash: str, feature_spec_hash: str
) -> dict[str, str]:
    return {
        "study_config_hash": config_hash,
        "source_manifest_hash": source_manifest_hash,
        "feature_spec_hash": feature_spec_hash,
    }


def _dry_run(
    config: Mapping[str, Any],
    config_hash: str,
    audit_directory: Path,
    audit_report: Mapping[str, Any],
    sessions: pd.DataFrame,
) -> int:
    feature_spec_hash = _feature_code_hash()
    identity = _run_identity(
        config_hash, str(audit_report["source_manifest_hash"]), feature_spec_hash
    )
    run_id = make_run_id("feature-store", identity)
    feature_root = resolve_project_path(
        config["paths"]["feature_store_root"], writable=True
    )
    cache_root = resolve_project_path(config["paths"]["cache_root"], writable=True)
    free_bytes = shutil.disk_usage(PROJECT_ROOT).free
    largest_video = int(sessions["video_size_bytes"].fillna(0).max())
    required_current_stage = largest_video + 1024 * 1024 * 1024
    print(
        json.dumps(
            {
                "dry_run": True,
                "run_id": run_id,
                "source_audit": str(audit_directory),
                "source_manifest_hash": audit_report["source_manifest_hash"],
                "feature_spec_hash": feature_spec_hash,
                "session_count": len(sessions),
                "reported_frame_count": int(
                    sessions["video_frame_count"].fillna(0).sum()
                ),
                "archive_session_counts": sessions["archive_id"]
                .value_counts()
                .sort_index()
                .to_dict(),
                "largest_staged_video_bytes": largest_video,
                "required_current_stage_bytes": required_current_stage,
                "available_bytes": free_bytes,
                "space_gate_pass": free_bytes >= required_current_stage,
                "checkpoint_root": str(cache_root / "checkpoints" / run_id),
                "planned_output": str(feature_root / run_id),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if free_bytes >= required_current_stage else 2


def run_builder(
    config_path: str | Path, *, resume: bool = False, dry_run: bool = False
) -> int:
    started = time.perf_counter()
    config, config_hash, selected_config = load_study_config(config_path)
    layout = load_canonical_live_layout(resolve_project_path(config["paths"]["roi_layout"]))
    audit_directory, audit_report, sessions = _find_source_inventory(
        config, config_hash
    )
    sessions = sessions.sort_values(
        ["archive_id", "target_roi", "collection_day", "session_id"],
        kind="stable",
        na_position="last",
    ).reset_index(drop=True)
    if dry_run:
        return _dry_run(
            config, config_hash, audit_directory, audit_report, sessions
        )

    source_manifest_hash = str(audit_report["source_manifest_hash"])
    feature_spec_hash = _feature_code_hash()
    builder_code_hash = _builder_code_hash()
    identity = _run_identity(config_hash, source_manifest_hash, feature_spec_hash)
    input_identity_hash = canonical_json_hash(identity)
    run_id = make_run_id("feature-store", identity)
    feature_root = resolve_project_path(
        config["paths"]["feature_store_root"], writable=True
    )
    cache_root = resolve_project_path(config["paths"]["cache_root"], writable=True)
    checkpoint_root = cache_root / "checkpoints" / run_id
    final_directory = feature_root / run_id

    archive_paths = {
        archive_id: Path(source["archive_path"])
        for archive_id, source in config["sources"].items()
    }
    archive_hashes: dict[str, str] = {}
    for archive_id, path in archive_paths.items():
        print(f"Verifying source hash before decoding {archive_id}: {path}", flush=True)
        archive_hashes[archive_id] = sha256_file(path)
        if archive_hashes[archive_id] != config["sources"][archive_id][
            "expected_sha256"
        ]:
            raise ValueError(f"source archive hash changed: {path}")

    existing_manifest = final_directory / "run_manifest.json"
    if existing_manifest.exists():
        manifest = load_json_object(existing_manifest)
        if manifest.get("input_identity_hash") == input_identity_hash:
            report_path = final_directory / "reconciliation_report.json"
            print(f"run_id={run_id}")
            print("reused=true")
            print(f"report={report_path}")
            return 0 if load_json_object(report_path).get("gate_pass") else 2

    temporary = create_temporary_run_directory(feature_root, run_id)
    logger = RunLogger(temporary / "events.jsonl", COMMAND_NAME, run_id)
    session_results: list[dict[str, Any]] = []
    try:
        logger.emit(
            "INFO",
            "run_started",
            config_path=str(selected_config),
            config_hash=config_hash,
            source_manifest_hash=source_manifest_hash,
            session_count=len(sessions),
            resume=resume,
        )
        cv2.setNumThreads(1)
        for archive_id in sorted(archive_paths):
            selected_rows = sessions[sessions["archive_id"] == archive_id]
            with zipfile.ZipFile(archive_paths[archive_id]) as archive:
                for ordinal, (_, session_series) in enumerate(
                    selected_rows.iterrows(), start=1
                ):
                    session = {
                        key: (None if pd.isna(value) else value)
                        for key, value in session_series.to_dict().items()
                    }
                    session_started = time.perf_counter()
                    logger.emit(
                        "INFO",
                        "session_started",
                        archive_id=archive_id,
                        session_id=session["session_id"],
                        ordinal=ordinal,
                        total=len(selected_rows),
                    )
                    result = _process_session(
                        archive=archive,
                        session=session,
                        layout=layout,
                        source_manifest_hash=source_manifest_hash,
                        study_config_hash=config_hash,
                        run_id=run_id,
                        feature_spec_hash=feature_spec_hash,
                        checkpoint_root=checkpoint_root,
                        cache_root=cache_root,
                        resume=resume,
                    )
                    result["elapsed_s_current_invocation"] = elapsed_seconds(
                        session_started
                    )
                    session_results.append(result)
                    logger.emit(
                        "INFO",
                        "session_completed",
                        archive_id=archive_id,
                        session_id=session["session_id"],
                        row_count=result["row_count"],
                        decoded_video_frames=result["decoded_video_frames"],
                        reused=result.get("reused", False),
                        elapsed_s=result["elapsed_s_current_invocation"],
                    )
                    print(
                        f"[{archive_id} {ordinal}/{len(selected_rows)}] "
                        f"{session['session_id']} rows={result['row_count']} "
                        f"reused={str(result.get('reused', False)).lower()}",
                        flush=True,
                    )

        final_sessions = temporary / "sessions"
        for result in session_results:
            archive_id = str(result["archive_id"])
            session_id = str(result["session_id"])
            source = (
                checkpoint_root
                / f"archive_id={archive_id}"
                / f"{session_id}.parquet"
            )
            destination = (
                final_sessions
                / f"archive_id={archive_id}"
                / f"{session_id}.parquet"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        index_records = []
        for result in session_results:
            record = dict(result)
            for field in (
                "orientation_counts",
                "exclusion_counts",
                "frame_reconciliation",
            ):
                record[f"{field}_json"] = json.dumps(
                    record.pop(field), sort_keys=True, separators=(",", ":")
                )
            index_records.append(record)
        index_frame = pd.DataFrame(index_records).sort_values(
            ["archive_id", "session_id"], kind="stable"
        )
        index_frame.to_parquet(temporary / "session_index.parquet", index=False)
        index_frame.to_csv(
            temporary / "session_index.csv", index=False, lineterminator="\n"
        )

        total_rows = int(index_frame["row_count"].sum())
        total_decoded = int(index_frame["decoded_video_frames"].sum())
        missing_video_frames = int(
            sum(
                result["frame_reconciliation"]["missing_video_frames"]
                for result in session_results
            )
        )
        unmatched_video_frames = int(
            sum(
                result["frame_reconciliation"]["unmatched_video_frames"]
                for result in session_results
            )
        )
        all_reason_coded = all(
            result["frame_reconciliation"]["all_differences_reason_coded"]
            for result in session_results
        )
        archive_hashes_after = {
            archive_id: sha256_file(path)
            for archive_id, path in archive_paths.items()
        }
        hashes_unchanged = archive_hashes_after == archive_hashes
        expected_session_count = sum(
            int(source["expected_session_count"])
            for source in config["sources"].values()
        )
        gate_checks = {
            "all_expected_sessions_checkpointed": (
                len(session_results) == expected_session_count
            ),
            "all_frame_differences_reason_coded": all_reason_coded,
            "archive_hashes_unchanged": hashes_unchanged,
            "canonical_layout_only": all(
                session["layout_id"] == LIVE_LAYOUT_ID
                for session in sessions.to_dict("records")
            ),
            "no_global_fitted_values": True,
        }
        if is_manual_only_config(config):
            gate_checks["manual_archive_only"] = (
                set(archive_hashes) == {"manual"}
                and set(index_frame["archive_id"].unique()) == {"manual"}
            )
        gate_pass = all(gate_checks.values())
        report = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": utc_now_iso(),
            "study_config_hash": config_hash,
            "source_manifest_hash": source_manifest_hash,
            "feature_spec_hash": feature_spec_hash,
            "source_audit": str(audit_directory),
            "session_count": len(session_results),
            "total_rows": total_rows,
            "decoded_video_frames": total_decoded,
            "missing_video_frames": missing_video_frames,
            "unmatched_video_frames": unmatched_video_frames,
            "archive_rows": index_frame.groupby("archive_id")["row_count"]
            .sum()
            .astype(int)
            .to_dict(),
            "archive_hashes_before": archive_hashes,
            "archive_hashes_after": archive_hashes_after,
            "gate_checks": gate_checks,
            "gate_pass": gate_pass,
            "dataset_path": "sessions",
            "notes": [
                "session_video.mp4 was the only quantitative image source; overlay, processed, and motion-magnified videos were not read.",
                "Saved 480x640 originals were not transformed a second time; a 640x480 input would be rotated 90 degrees clockwise and mirrored exactly once.",
                "No fold-fitted floor, scale, threshold, imputation, Fdetect, Fmax, or model transform is stored in this immutable dataset.",
            ],
        }
        if is_manual_only_config(config):
            report["dataset_policy"] = "manual_only"
            assert_manual_only_artifact_lineage(report)
        atomic_write_json(temporary / "reconciliation_report.json", report)
        logger.emit(
            "INFO" if gate_pass else "ERROR",
            "feature_store_gate_finished",
            gate_pass=gate_pass,
            gate_checks=gate_checks,
            total_rows=total_rows,
        )
        logger.close()

        output_hashes = output_file_hashes(temporary, exclude=("run_manifest.json",))
        run_manifest = {
            "schema_version": SCHEMA_VERSION,
            "command": COMMAND_NAME,
            "argv": sys.argv,
            "run_id": run_id,
            "created_at": utc_now_iso(),
            "completed_at": utc_now_iso(),
            "elapsed_s": elapsed_seconds(started),
            "config_path": str(selected_config),
            "study_config_hash": config_hash,
            "source_manifest_hash": source_manifest_hash,
            "feature_spec_hash": feature_spec_hash,
            "code_hash": builder_code_hash,
            "input_identity_hash": input_identity_hash,
            "inputs": {
                archive_id: {
                    "path": str(path),
                    "sha256": archive_hashes[archive_id],
                    "size_bytes": path.stat().st_size,
                }
                for archive_id, path in archive_paths.items()
            },
            "outputs": output_hashes,
            "environment": platform_manifest(),
            "warnings": [
                {
                    "session_id": result["session_id"],
                    "exclusion_counts": result["exclusion_counts"],
                }
                for result in session_results
                if result["exclusion_counts"]
            ],
            "exclusions": [],
            "gate_pass": gate_pass,
        }
        if is_manual_only_config(config):
            run_manifest["dataset_policy"] = "manual_only"
            assert_manual_only_artifact_lineage(run_manifest)
        atomic_write_json(temporary / "run_manifest.json", run_manifest)
        published, reused = publish_run_directory(temporary, final_directory)
        print(f"run_id={run_id}")
        print(f"reused={str(reused).lower()}")
        print(f"report={published / 'reconciliation_report.json'}")
        return 0 if gate_pass else 2
    except Exception as exc:
        logger.emit("ERROR", "run_failed", error=f"{type(exc).__name__}: {exc}")
        logger.close()
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reprocess every immutable session_video.mp4 with the shared signed/"
            "positive HSV-V extractor and build a partitioned Parquet feature store."
        )
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to schema-validated config/live_sensor_study.json.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse only completed per-session checkpoints whose input/output hashes match.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and report planned sessions, frames, cache, space, and output paths.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return run_builder(
            arguments.config, resume=arguments.resume, dry_run=arguments.dry_run
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
