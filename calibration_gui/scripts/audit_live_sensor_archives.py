"""Audit immutable manual/automated archives and build source manifests.

The command never extracts in place or writes to ``Downloads``.  It verifies
archive SHA-256 and every member CRC, profiles session metadata directly from
the ZIP streams, and atomically publishes schema-validated Parquet/CSV evidence.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Iterable
import zipfile

import pandas as pd
from jsonschema import Draft202012Validator, FormatChecker

from core.live_sensor_contracts import (
    LIVE_LAYOUT_ID,
    canonical_json_hash,
    load_canonical_live_layout,
    load_json_object,
)
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
    sha256_bytes,
    sha256_file,
    utc_now_iso,
)


COMMAND_NAME = "audit-live-sensor-archives"
SCHEMA_VERSION = "1.0.0"
SESSION_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "session_manifest.schema.json"
CONTROL_FIELDS = (
    "exposure",
    "auto_exposure",
    "gain",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
    "gamma",
    "white_balance",
    "auto_white_balance",
    "focus",
    "auto_focus",
    "hue",
    "backlight_compensation",
)
BASE_REQUIRED_MEMBERS = frozenset(
    {
        "baseline_data.npz",
        "baseline_summary.json",
        "camera_settings.json",
        "master_synchronized.csv",
        "roi_layout.json",
        "session_config.json",
        "session_status.json",
        "session_video.mp4",
    }
)


def _strict_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_json_member(archive: zipfile.ZipFile, member: str) -> dict[str, Any]:
    value = json.loads(archive.read(member).decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{member} does not contain one JSON object")
    return value


def _member_sha256(archive: zipfile.ZipFile, member: str) -> str:
    digest_chunks: list[bytes] = []
    with archive.open(member) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest_chunks.append(chunk)
    return sha256_bytes(b"".join(digest_chunks))


def _profile_master_member(
    archive: zipfile.ZipFile, member: str
) -> dict[str, int | float | None]:
    row_count = 0
    valid_force_count = 0
    force_min: float | None = None
    force_max: float | None = None
    with archive.open(member) as binary:
        with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as text:
            reader = csv.DictReader(text)
            for row in reader:
                row_count += 1
                force = _strict_float(row.get("force_N"))
                if force is None:
                    continue
                valid_force_count += 1
                force_min = force if force_min is None else min(force_min, force)
                force_max = force if force_max is None else max(force_max, force)
    return {
        "master_row_count": row_count,
        "valid_force_row_count": valid_force_count,
        "force_min_N": force_min,
        "force_max_N": force_max,
    }


def _session_identity(root: str) -> tuple[str, str, int | None, str | None]:
    session_id = root.rstrip("/").rsplit("/", 1)[-1]
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", session_id)
    roi_match = re.search(r"_R([1-9])(?:_|$)", session_id)
    test_match = re.search(r"_(TEST\d+)(?:_|$)", session_id)
    return (
        session_id,
        date_match.group(1) if date_match else "1970-01-01",
        int(roi_match.group(1)) if roi_match else None,
        test_match.group(1) if test_match else None,
    )


def _scientific_assignment(
    archive_id: str, session_id: str
) -> tuple[str, str, str | None, str]:
    if archive_id == "automated":
        return "characterization", "included", None, "mixed_protocol"
    if "NOCONTACT" in session_id.upper():
        return "no_contact", "included", None, "no_contact"
    if re.search(r"_TEST[1-6]$", session_id):
        return "model_primary", "included", None, "contact"
    return "replay_only", "included", "outside_primary_test1_6", "contact"


def _camera_controls(camera_settings: dict[str, Any]) -> dict[str, Any]:
    controls = camera_settings.get("confirmed_actual_controls")
    if not isinstance(controls, dict):
        controls = camera_settings.get("controls")
    if not isinstance(controls, dict):
        controls = {}
    return {f"camera_{name}": controls.get(name) for name in CONTROL_FIELDS}


def _crc32(info: zipfile.ZipInfo | None) -> str | None:
    return f"{info.CRC:08x}" if info is not None else None


def _audit_session(
    archive: zipfile.ZipFile,
    *,
    archive_id: str,
    archive_role: str,
    archive_sha256: str,
    root: str,
    study_config_hash: str,
    run_id: str,
) -> dict[str, Any]:
    names = set(archive.namelist())
    session_id, collection_day, target_roi, test_group = _session_identity(root)
    scientific_role, inclusion_status, reason_code, contact_role = (
        _scientific_assignment(archive_id, session_id)
    )
    required = set(BASE_REQUIRED_MEMBERS)
    if archive_id == "automated":
        required.add("printer_sequence.json")
    missing = sorted(name for name in required if f"{root}/{name}" not in names)
    problems = [f"missing_required_member:{name}" for name in missing]

    def read_optional(name: str) -> dict[str, Any]:
        member = f"{root}/{name}"
        if member not in names:
            return {}
        try:
            return _read_json_member(archive, member)
        except (KeyError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            problems.append(f"invalid_json:{name}:{type(exc).__name__}")
            return {}

    session_config = read_optional("session_config.json")
    camera_settings = read_optional("camera_settings.json")
    layout = read_optional("roi_layout.json")
    baseline = read_optional("baseline_summary.json")
    status = read_optional("session_status.json")

    layout_id = layout.get("roi_layout_id")
    if layout_id != LIVE_LAYOUT_ID:
        problems.append(f"wrong_layout:{layout_id}")
    oriented_width = layout.get("frame_width")
    oriented_height = layout.get("frame_height")
    if (oriented_width, oriented_height) != (480, 640):
        problems.append(
            f"wrong_oriented_geometry:{oriented_width}x{oriented_height}"
        )

    camera_actual = camera_settings.get("actual")
    if not isinstance(camera_actual, dict):
        camera_actual = session_config.get("camera_actual", {})
    if not isinstance(camera_actual, dict):
        camera_actual = {}
    raw_width = camera_actual.get("actual_width")
    raw_height = camera_actual.get("actual_height")
    if (raw_width, raw_height) != (640, 480):
        problems.append(f"wrong_raw_geometry:{raw_width}x{raw_height}")

    if status.get("complete") is not True or status.get("partial") is True:
        problems.append("session_not_complete")
    if baseline.get("valid") is not True:
        problems.append("baseline_not_valid")

    video_member = f"{root}/session_video.mp4"
    master_member = f"{root}/master_synchronized.csv"
    video_info = archive.getinfo(video_member) if video_member in names else None
    master_info = archive.getinfo(master_member) if master_member in names else None
    profile: dict[str, int | float | None] = {
        "master_row_count": None,
        "valid_force_row_count": None,
        "force_min_N": None,
        "force_max_N": None,
    }
    if master_info is not None:
        try:
            profile = _profile_master_member(archive, master_member)
        except (OSError, UnicodeError, csv.Error) as exc:
            problems.append(f"master_profile_failed:{type(exc).__name__}")

    reported_video_frames = status.get("video_frame_count")
    reported_feature_rows = status.get("feature_row_count")
    if (
        isinstance(profile["master_row_count"], int)
        and isinstance(reported_feature_rows, int)
        and profile["master_row_count"] != reported_feature_rows
    ):
        problems.append(
            "master_feature_count_mismatch:"
            f"{profile['master_row_count']}!={reported_feature_rows}"
        )
    if (
        isinstance(reported_video_frames, int)
        and isinstance(reported_feature_rows, int)
        and reported_video_frames != reported_feature_rows
    ):
        problems.append(
            f"video_feature_count_mismatch:{reported_video_frames}!={reported_feature_rows}"
        )

    baseline_member = f"{root}/baseline_data.npz"
    baseline_hash = (
        _member_sha256(archive, baseline_member) if baseline_member in names else None
    )
    layout_hash = canonical_json_hash(layout) if layout else None

    settings: dict[str, Any] = {}
    printer_motion = session_config.get("printer_motion")
    if isinstance(printer_motion, dict):
        candidate = printer_motion.get("settings")
        if isinstance(candidate, dict):
            settings = candidate

    if problems:
        scientific_role = "excluded"
        inclusion_status = "excluded"
        reason_code = problems[0].split(":", 1)[0]

    return {
        "schema_version": SCHEMA_VERSION,
        "study_config_hash": study_config_hash,
        "source_manifest_hash": "pending-self-hash",
        "created_by_run_id": run_id,
        "archive_id": archive_id,
        "archive_role": archive_role,
        "archive_sha256": archive_sha256,
        "zip_member_root": root,
        "collection_day": collection_day,
        "session_id": session_id,
        "test_group": test_group,
        "target_roi": target_roi,
        "contact_role": contact_role,
        "speed_mm_min": _strict_float(settings.get("down_feed_mm_min")),
        "up_speed_mm_min": _strict_float(settings.get("up_feed_mm_min")),
        "displacement_mm": _strict_float(settings.get("displacement_mm")),
        "cycle_plan": settings.get("cycles")
        if isinstance(settings.get("cycles"), int)
        else None,
        "bottom_hold_s": _strict_float(settings.get("bottom_hold_s")),
        "top_dwell_s": _strict_float(settings.get("top_dwell_s")),
        "layout_id": layout_id,
        "layout_hash": layout_hash,
        "baseline_id": baseline.get("baseline_id"),
        "baseline_hash": baseline_hash,
        "source_video_member": video_member if video_info else None,
        "master_csv_member": master_member if master_info else None,
        "video_size_bytes": video_info.file_size if video_info else None,
        "video_compressed_bytes": video_info.compress_size if video_info else None,
        "video_crc32": _crc32(video_info),
        "master_size_bytes": master_info.file_size if master_info else None,
        "master_crc32": _crc32(master_info),
        "video_frame_count": reported_video_frames
        if isinstance(reported_video_frames, int)
        else None,
        "video_frame_count_source": "session_status.json",
        "feature_row_count_reported": reported_feature_rows
        if isinstance(reported_feature_rows, int)
        else None,
        **profile,
        "raw_width": raw_width if isinstance(raw_width, int) else None,
        "raw_height": raw_height if isinstance(raw_height, int) else None,
        "oriented_width": oriented_width
        if isinstance(oriented_width, int)
        else None,
        "oriented_height": oriented_height
        if isinstance(oriented_height, int)
        else None,
        "camera_backend": camera_actual.get("backend"),
        "camera_device_index": camera_actual.get("device_index"),
        **_camera_controls(camera_settings),
        "inclusion_status": inclusion_status,
        "reason_code": reason_code,
        "reason_codes_json": json.dumps(problems, separators=(",", ":")),
        "scientific_role": scientific_role,
        "integrity_result": "fail" if problems else "pass",
        "notes": "; ".join(problems),
    }


def _member_inventory(
    archive: zipfile.ZipFile,
    *,
    archive_id: str,
    archive_sha256: str,
) -> list[dict[str, Any]]:
    return [
        {
            "archive_id": archive_id,
            "archive_sha256": archive_sha256,
            "member_path": info.filename,
            "is_directory": info.is_dir(),
            "uncompressed_bytes": info.file_size,
            "compressed_bytes": info.compress_size,
            "crc32": f"{info.CRC:08x}",
            "compression_type": info.compress_type,
        }
        for info in archive.infolist()
    ]


def _validate_session_rows(rows: Iterable[dict[str, Any]]) -> None:
    schema = load_json_object(SESSION_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for index, row in enumerate(rows):
        errors = sorted(validator.iter_errors(row), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            location = "/".join(str(item) for item in first.absolute_path)
            raise ValueError(
                f"session manifest row {index} failed schema at {location}: {first.message}"
            )


def _role_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row["scientific_role"])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _primary_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cell_counts = {
        f"ROI{roi}_{group}": sum(
            1
            for row in rows
            if row["archive_id"] == "manual"
            and row["scientific_role"] == "model_primary"
            and row["target_roi"] == roi
            and row["test_group"] == group
        )
        for roi in range(1, 10)
        for group in ("TEST1", "TEST2", "TEST3", "TEST4", "TEST5", "TEST6")
    }
    return {
        "cell_counts": cell_counts,
        "all_54_cells_exactly_one": all(value == 1 for value in cell_counts.values()),
    }


def _central_directory_summary(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        roots = sorted(
            info.filename.rsplit("/", 1)[0]
            for info in infos
            if info.filename.endswith("/session_config.json")
        )
        return {
            "archive_path": str(path),
            "archive_size_bytes": path.stat().st_size,
            "entry_count": len(infos),
            "file_entry_count": sum(not info.is_dir() for info in infos),
            "expanded_bytes": sum(info.file_size for info in infos),
            "compressed_member_bytes": sum(info.compress_size for info in infos),
            "session_count": len(roots),
        }


def _dry_run(config: dict[str, Any], config_hash: str) -> int:
    summaries: dict[str, Any] = {}
    total_expanded = 0
    for archive_id, source in config["sources"].items():
        path = Path(source["archive_path"])
        summary = _central_directory_summary(path)
        summaries[archive_id] = summary
        total_expanded += int(summary["expanded_bytes"])
    output_root = resolve_project_path(
        config["paths"]["source_inventory_root"], writable=True
    )
    free_bytes = shutil.disk_usage(PROJECT_ROOT).free
    required_with_headroom = math.ceil(total_expanded * 1.5)
    script_hash = code_hash(
        (
            "scripts/audit_live_sensor_archives.py",
            "scripts/live_sensor_common.py",
            "core/live_sensor_contracts.py",
            "contracts/session_manifest.schema.json",
        )
    )
    expected_hashes = {
        key: value["expected_sha256"] for key, value in config["sources"].items()
    }
    run_id = make_run_id(
        "archive-audit",
        {
            "study_config_hash": config_hash,
            "archive_hashes": expected_hashes,
            "code_hash": script_hash,
        },
    )
    print(
        json.dumps(
            {
                "dry_run": True,
                "run_id": run_id,
                "archives": summaries,
                "expanded_bytes": total_expanded,
                "required_with_50_percent_headroom_bytes": required_with_headroom,
                "available_bytes": free_bytes,
                "space_gate_pass": free_bytes >= required_with_headroom,
                "planned_output": str(output_root / run_id),
                "planned_session_count": sum(
                    int(item["session_count"]) for item in summaries.values()
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if free_bytes >= required_with_headroom else 2


def run_audit(config_path: str | Path, *, dry_run: bool = False) -> int:
    started = time.perf_counter()
    config, config_hash, selected_config = load_study_config(config_path)
    load_canonical_live_layout(resolve_project_path(config["paths"]["roi_layout"]))
    if dry_run:
        return _dry_run(config, config_hash)

    archive_hashes: dict[str, str] = {}
    archive_paths: dict[str, Path] = {}
    for archive_id, source in config["sources"].items():
        path = Path(source["archive_path"])
        if not path.is_file():
            raise FileNotFoundError(f"source archive is missing: {path}")
        archive_paths[archive_id] = path
        print(f"Hashing read-only {archive_id} archive: {path}", flush=True)
        archive_hashes[archive_id] = sha256_file(path)

    script_hash = code_hash(
        (
            "scripts/audit_live_sensor_archives.py",
            "scripts/live_sensor_common.py",
            "core/live_sensor_contracts.py",
            "contracts/session_manifest.schema.json",
        )
    )
    identity = {
        "study_config_hash": config_hash,
        "archive_hashes": archive_hashes,
        "code_hash": script_hash,
    }
    input_identity_hash = canonical_json_hash(identity)
    run_id = make_run_id("archive-audit", identity)
    output_root = resolve_project_path(
        config["paths"]["source_inventory_root"], writable=True
    )
    final_directory = output_root / run_id
    existing_manifest = final_directory / "run_manifest.json"
    if existing_manifest.exists():
        manifest = load_json_object(existing_manifest)
        if manifest.get("input_identity_hash") == input_identity_hash:
            report = final_directory / "audit_report.json"
            print(f"run_id={run_id}")
            print(f"reused=true")
            print(f"report={report}")
            return 0 if load_json_object(report).get("gate_pass") else 2

    temporary = create_temporary_run_directory(output_root, run_id)
    logger = RunLogger(temporary / "events.jsonl", COMMAND_NAME, run_id)
    rows: list[dict[str, Any]] = []
    member_rows: list[dict[str, Any]] = []
    archive_reports: dict[str, dict[str, Any]] = {}
    try:
        logger.emit(
            "INFO",
            "run_started",
            config_path=str(selected_config),
            config_hash=config_hash,
        )
        for archive_id, source in config["sources"].items():
            path = archive_paths[archive_id]
            actual_hash = archive_hashes[archive_id]
            hash_ok = actual_hash == source["expected_sha256"]
            logger.emit(
                "INFO",
                "archive_crc_started",
                archive_id=archive_id,
                archive_path=str(path),
            )
            crc_started = time.perf_counter()
            crc_error: str | None = None
            bad_member: str | None = None
            with zipfile.ZipFile(path) as archive:
                try:
                    bad_member = archive.testzip()
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    crc_error = f"{type(exc).__name__}: {exc}"
                crc_ok = bad_member is None and crc_error is None
                logger.emit(
                    "INFO" if crc_ok else "ERROR",
                    "archive_crc_finished",
                    archive_id=archive_id,
                    crc_ok=crc_ok,
                    bad_member=bad_member,
                    error=crc_error,
                    elapsed_s=elapsed_seconds(crc_started),
                )
                roots = sorted(
                    info.filename.rsplit("/", 1)[0]
                    for info in archive.infolist()
                    if info.filename.endswith("/session_config.json")
                )
                member_rows.extend(
                    _member_inventory(
                        archive,
                        archive_id=archive_id,
                        archive_sha256=actual_hash,
                    )
                )
                for index, root in enumerate(roots, start=1):
                    rows.append(
                        _audit_session(
                            archive,
                            archive_id=archive_id,
                            archive_role=source["archive_role"],
                            archive_sha256=actual_hash,
                            root=root,
                            study_config_hash=config_hash,
                            run_id=run_id,
                        )
                    )
                    if index % 25 == 0 or index == len(roots):
                        logger.emit(
                            "INFO",
                            "session_inventory_progress",
                            archive_id=archive_id,
                            completed=index,
                            total=len(roots),
                        )
                infos = archive.infolist()
                archive_reports[archive_id] = {
                    "archive_path": str(path),
                    "archive_role": source["archive_role"],
                    "expected_sha256": source["expected_sha256"],
                    "actual_sha256": actual_hash,
                    "hash_ok": hash_ok,
                    "crc_ok": crc_ok,
                    "bad_member": bad_member,
                    "crc_error": crc_error,
                    "entry_count": len(infos),
                    "file_entry_count": sum(not info.is_dir() for info in infos),
                    "archive_size_bytes": path.stat().st_size,
                    "expanded_bytes": sum(info.file_size for info in infos),
                    "session_count": len(roots),
                    "expected_session_count": source["expected_session_count"],
                    "session_count_ok": len(roots) == source["expected_session_count"],
                }

        manifest_identity = [
            {
                key: value
                for key, value in row.items()
                if key != "source_manifest_hash"
            }
            for row in sorted(rows, key=lambda item: (item["archive_id"], item["session_id"]))
        ]
        source_manifest_hash = canonical_json_hash(manifest_identity)
        for row in rows:
            row["source_manifest_hash"] = source_manifest_hash
        for member in member_rows:
            member.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "study_config_hash": config_hash,
                    "source_manifest_hash": source_manifest_hash,
                    "created_by_run_id": run_id,
                }
            )
        _validate_session_rows(rows)

        total_expanded = sum(
            int(report["expanded_bytes"]) for report in archive_reports.values()
        )
        free_bytes = shutil.disk_usage(PROJECT_ROOT).free
        required_with_headroom = math.ceil(total_expanded * 1.5)
        primary_coverage = _primary_coverage(rows)
        integrity_failure_count = sum(
            row["integrity_result"] == "fail" for row in rows
        )
        gate_checks = {
            "all_archive_hashes_match": all(
                report["hash_ok"] for report in archive_reports.values()
            ),
            "all_archive_member_crcs_pass": all(
                report["crc_ok"] for report in archive_reports.values()
            ),
            "all_archive_session_counts_match": all(
                report["session_count_ok"] for report in archive_reports.values()
            ),
            "all_sessions_integrity_pass": integrity_failure_count == 0,
            "manual_primary_54_cells_exactly_one": primary_coverage[
                "all_54_cells_exactly_one"
            ],
            "disk_space_with_50_percent_headroom": free_bytes
            >= required_with_headroom,
        }
        if is_manual_only_config(config):
            gate_checks["manual_archive_only"] = (
                set(archive_reports) == {"manual"}
                and {row["archive_id"] for row in rows} == {"manual"}
                and {row["archive_id"] for row in member_rows} == {"manual"}
            )
        gate_pass = all(gate_checks.values())
        report = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": utc_now_iso(),
            "study_config_hash": config_hash,
            "source_manifest_hash": source_manifest_hash,
            "code_hash": script_hash,
            "archives": archive_reports,
            "session_count": len(rows),
            "role_counts": _role_counts(rows),
            "integrity_failure_count": integrity_failure_count,
            "primary_manual_coverage": primary_coverage,
            "expanded_bytes": total_expanded,
            "required_with_50_percent_headroom_bytes": required_with_headroom,
            "available_bytes_at_audit": free_bytes,
            "gate_checks": gate_checks,
            "gate_pass": gate_pass,
            "legacy_layout_conflict": {
                "legacy_layout_id": "roi-4691aef42361b7430d3b",
                "live_layout_id": LIVE_LAYOUT_ID,
                "compatible": False,
            },
            "notes": [
                "Original ZIP archives were opened read-only and were not extracted or modified.",
                "video_frame_count is acquisition-reported evidence from session_status.json; actual decode reconciliation belongs to the feature-store command.",
                "TEST7 and TEST1_v2 contact sessions are retained as replay_only rather than pooled with the frozen TEST1-TEST6 primary groups.",
            ],
        }
        if is_manual_only_config(config):
            report["dataset_policy"] = "manual_only"
            assert_manual_only_artifact_lineage(report)

        session_frame = pd.DataFrame(rows).sort_values(
            ["archive_id", "target_roi", "collection_day", "session_id"],
            kind="stable",
            na_position="last",
        )
        member_frame = pd.DataFrame(member_rows).sort_values(
            ["archive_id", "member_path"], kind="stable"
        )
        session_frame.to_parquet(temporary / "session_manifest.parquet", index=False)
        session_frame.to_csv(
            temporary / "session_manifest.csv", index=False, lineterminator="\n"
        )
        member_frame.to_parquet(temporary / "archive_members.parquet", index=False)
        member_frame.to_csv(
            temporary / "archive_members.csv", index=False, lineterminator="\n"
        )
        atomic_write_json(temporary / "archive_inventory.json", archive_reports)
        atomic_write_json(temporary / "audit_report.json", report)
        logger.emit(
            "INFO" if gate_pass else "ERROR",
            "audit_gate_finished",
            gate_pass=gate_pass,
            gate_checks=gate_checks,
            session_count=len(rows),
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
            "code_hash": script_hash,
            "input_identity_hash": input_identity_hash,
            "inputs": {
                archive_id: {
                    "path": str(archive_paths[archive_id]),
                    "sha256": archive_hashes[archive_id],
                    "size_bytes": archive_paths[archive_id].stat().st_size,
                }
                for archive_id in sorted(archive_paths)
            },
            "outputs": output_hashes,
            "environment": platform_manifest(),
            "warnings": [
                row["session_id"] + ":" + str(row["reason_code"])
                for row in rows
                if row["reason_code"] is not None
            ],
            "exclusions": [
                row["session_id"]
                for row in rows
                if row["scientific_role"] == "excluded"
            ],
            "gate_pass": gate_pass,
        }
        if is_manual_only_config(config):
            run_manifest["dataset_policy"] = "manual_only"
            assert_manual_only_artifact_lineage(run_manifest)
        atomic_write_json(temporary / "run_manifest.json", run_manifest)
        published, reused = publish_run_directory(temporary, final_directory)
        print(f"run_id={run_id}")
        print(f"reused={str(reused).lower()}")
        print(f"report={published / 'audit_report.json'}")
        return 0 if gate_pass else 2
    except Exception as exc:
        logger.emit("ERROR", "run_failed", error=f"{type(exc).__name__}: {exc}")
        logger.close()
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify immutable calibration ZIPs and atomically build separate "
            "manual/automated source manifests."
        )
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to schema-validated config/live_sensor_study.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read only archive central directories and report session counts, "
            "space, hashes expected by the config, and planned outputs."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return run_audit(arguments.config, dry_run=arguments.dry_run)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
