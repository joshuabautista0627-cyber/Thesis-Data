"""Freeze session-grouped outer/no-contact folds before model evaluation."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
import pandas as pd

from core.live_sensor_contracts import canonical_json_hash, load_json_object
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


COMMAND_NAME = "freeze-live-sensor-splits"
SCHEMA_VERSION = "1.0.0"
SPLIT_VERSION = "split-v1-test-group-held-out"
SPLIT_SCHEMA_PATH = PROJECT_ROOT / "contracts" / "split_manifest.schema.json"


def _none_if_missing(value: Any) -> Any:
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else value


def _find_feature_store(
    config: Mapping[str, Any], config_hash: str
) -> tuple[Path, dict[str, Any], pd.DataFrame]:
    root = resolve_project_path(config["paths"]["feature_store_root"], writable=True)
    matches: list[tuple[Path, dict[str, Any]]] = []
    for report_path in root.glob("*/reconciliation_report.json"):
        report = load_json_object(report_path)
        if report.get("study_config_hash") == config_hash and report.get("gate_pass"):
            matches.append((report_path.parent, report))
    if not matches:
        raise FileNotFoundError(
            "no passing feature store exists; run "
            "python -m scripts.build_optical_force_dataset --resume first"
        )
    directory, report = sorted(matches, key=lambda item: item[0].name)[-1]
    index_path = directory / "session_index.parquet"
    if not index_path.is_file():
        raise FileNotFoundError(f"feature-store session index is missing: {index_path}")
    index = pd.read_parquet(index_path)
    if len(index) != int(report["session_count"]):
        raise ValueError("feature-store report/session index count mismatch")
    return directory, report, index


def _find_source_manifest(
    config: Mapping[str, Any], source_manifest_hash: str
) -> tuple[Path, pd.DataFrame]:
    root = resolve_project_path(config["paths"]["source_inventory_root"], writable=True)
    for report_path in sorted(root.glob("*/audit_report.json")):
        report = load_json_object(report_path)
        if report.get("source_manifest_hash") != source_manifest_hash:
            continue
        path = report_path.parent / "session_manifest.parquet"
        if path.is_file():
            return report_path.parent, pd.read_parquet(path)
    raise FileNotFoundError(
        f"source manifest {source_manifest_hash} is not available under {root}"
    )


def _build_split_rows(
    sessions: pd.DataFrame,
    *,
    config: Mapping[str, Any],
    config_hash: str,
    source_manifest_hash: str,
    run_id: str,
) -> list[dict[str, Any]]:
    outer_groups = list(config["force_study"]["outer_groups"])
    fold_by_group = {group: index + 1 for index, group in enumerate(outer_groups)}
    no_contact_fold = _assign_no_contact_folds(sessions, len(outer_groups))
    rows: list[dict[str, Any]] = []
    for raw in sessions.sort_values(["archive_id", "session_id"], kind="stable").to_dict(
        "records"
    ):
        role = str(raw["scientific_role"])
        group = _none_if_missing(raw.get("test_group"))
        session_id = str(raw["session_id"])
        outer_fold = fold_by_group.get(str(group)) if role == "model_primary" else None
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "study_config_hash": config_hash,
                "source_manifest_hash": source_manifest_hash,
                "created_by_run_id": run_id,
                "split_version": SPLIT_VERSION,
                "split_hash": "0" * 64,
                "session_id": session_id,
                "scientific_role": role,
                "test_group": group,
                "collection_day": str(raw["collection_day"]),
                "outer_fold": outer_fold,
                "inner_fold_eligible": role in {"model_primary", "no_contact"},
                "no_contact_fold": no_contact_fold.get(session_id),
            }
        )
    split_hash = canonical_json_hash(
        [{key: value for key, value in row.items() if key != "split_hash"} for row in rows]
    )
    for row in rows:
        row["split_hash"] = split_hash
    return rows


def _assign_no_contact_folds(
    sessions: pd.DataFrame, fold_count: int
) -> dict[str, int]:
    """Assign whole no-contact sessions deterministically, stratified by date."""

    selected = sessions.loc[
        sessions["scientific_role"] == "no_contact",
        ["session_id", "collection_day"],
    ].copy()
    selected["session_id"] = selected["session_id"].astype(str)
    selected["collection_day"] = selected["collection_day"].astype(str)
    selected = selected.sort_values(
        ["collection_day", "session_id"], kind="stable"
    )
    counts = {fold: 0 for fold in range(1, fold_count + 1)}
    result: dict[str, int] = {}
    for _, day_rows in selected.groupby("collection_day", sort=True):
        start = min(counts, key=lambda fold: (counts[fold], fold))
        ordered_folds = [
            ((start - 1 + offset) % fold_count) + 1
            for offset in range(fold_count)
        ]
        for index, session_id in enumerate(day_rows["session_id"].tolist()):
            fold = ordered_folds[index % fold_count]
            result[session_id] = fold
            counts[fold] += 1
    return result


def _validate_rows(rows: list[dict[str, Any]]) -> None:
    schema = load_json_object(SPLIT_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for index, row in enumerate(rows):
        errors = sorted(validator.iter_errors(row), key=lambda error: list(error.path))
        if errors:
            first = errors[0]
            location = "/".join(str(item) for item in first.absolute_path)
            raise ValueError(
                f"split row {index} failed schema at {location}: {first.message}"
            )


def _leakage_audit(
    split_rows: list[dict[str, Any]], source_sessions: pd.DataFrame
) -> dict[str, Any]:
    split = pd.DataFrame(split_rows)
    primary = split[split["scientific_role"] == "model_primary"]
    no_contact = split[split["scientific_role"] == "no_contact"]
    duplicates = int(split.duplicated(["session_id"]).sum())
    outer_counts = {
        int(key): int(value)
        for key, value in primary.groupby("outer_fold")["session_id"]
        .count()
        .items()
    }
    roi_lookup = source_sessions.set_index("session_id")["target_roi"].to_dict()
    missing_training_rois: dict[str, list[int]] = {}
    held_out_overlap: dict[str, list[str]] = {}
    no_contact_overlap: dict[str, list[str]] = {}
    for fold in range(1, 7):
        held_out = set(primary.loc[primary["outer_fold"] == fold, "session_id"])
        training = set(primary.loc[primary["outer_fold"] != fold, "session_id"])
        held_out_overlap[str(fold)] = sorted(held_out & training)
        held_out_no_contact = set(
            no_contact.loc[no_contact["no_contact_fold"] == fold, "session_id"]
        )
        training_no_contact = set(
            no_contact.loc[no_contact["no_contact_fold"] != fold, "session_id"]
        )
        no_contact_overlap[str(fold)] = sorted(
            held_out_no_contact & training_no_contact
        )
        training_rois = {int(roi_lookup[session_id]) for session_id in training}
        missing_training_rois[str(fold)] = sorted(set(range(1, 10)) - training_rois)
    checks = {
        "one_row_per_session": bool(duplicates == 0 and len(split) == len(source_sessions)),
        "primary_session_count_54": bool(len(primary) == 54),
        "nine_primary_sessions_per_outer_fold": bool(
            outer_counts == {fold: 9 for fold in range(1, 7)}
        ),
        "outer_train_holdout_disjoint": bool(all(
            not values for values in held_out_overlap.values()
        )),
        "all_roi_classes_in_every_outer_training_set": bool(all(
            not values for values in missing_training_rois.values()
        )),
        "all_no_contact_sessions_assigned_whole": bool(
            len(no_contact) > 0 and no_contact["no_contact_fold"].notna().all()
        ),
        "no_contact_train_holdout_disjoint": bool(
            all(not values for values in no_contact_overlap.values())
        ),
        "non_model_roles_have_no_outer_fold": bool(split.loc[
            split["scientific_role"] != "model_primary", "outer_fold"
        ].isna().all()),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "split_version": SPLIT_VERSION,
        "split_hash": split_rows[0]["split_hash"],
        "session_count": len(split),
        "primary_session_count": len(primary),
        "no_contact_session_count": len(no_contact),
        "outer_fold_counts": {str(key): value for key, value in outer_counts.items()},
        "no_contact_fold_counts": {
            str(key): int(value)
            for key, value in no_contact["no_contact_fold"].value_counts().sort_index().items()
        },
        "held_out_training_overlap": held_out_overlap,
        "no_contact_held_out_training_overlap": no_contact_overlap,
        "missing_training_rois": missing_training_rois,
        "checks": checks,
        "gate_pass": all(checks.values()),
    }


def _script_hash() -> str:
    return code_hash(
        (
            "scripts/freeze_live_sensor_splits.py",
            "scripts/live_sensor_common.py",
            "core/live_sensor_contracts.py",
            "contracts/split_manifest.schema.json",
        )
    )


def run_freeze(config_path: str | Path, *, dry_run: bool = False) -> int:
    started = time.perf_counter()
    config, config_hash, selected_config = load_study_config(config_path)
    feature_directory, feature_report, feature_index = _find_feature_store(
        config, config_hash
    )
    source_manifest_hash = str(feature_report["source_manifest_hash"])
    source_directory, source_sessions = _find_source_manifest(
        config, source_manifest_hash
    )
    if set(feature_index["session_id"]) != set(source_sessions["session_id"]):
        raise ValueError("feature store and source manifest session sets differ")
    if is_manual_only_config(config):
        if set(feature_index["archive_id"].astype(str).unique()) != {"manual"}:
            raise ValueError("manual-only feature store contains forbidden lineage")
        if set(source_sessions["archive_id"].astype(str).unique()) != {"manual"}:
            raise ValueError("manual-only source manifest contains forbidden lineage")
    script_hash = _script_hash()
    identity = {
        "study_config_hash": config_hash,
        "source_manifest_hash": source_manifest_hash,
        "feature_spec_hash": feature_report["feature_spec_hash"],
        "feature_report_hash": sha256_file(
            feature_directory / "reconciliation_report.json"
        ),
        "code_hash": script_hash,
    }
    run_id = make_run_id("splits", identity)
    split_root = resolve_project_path(config["paths"]["split_root"], writable=True)
    final_directory = split_root / run_id
    if dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "run_id": run_id,
                    "source_manifest": str(source_directory),
                    "feature_store": str(feature_directory),
                    "session_count": len(source_sessions),
                    "primary_session_count": int(
                        (source_sessions["scientific_role"] == "model_primary").sum()
                    ),
                    "no_contact_session_count": int(
                        (source_sessions["scientific_role"] == "no_contact").sum()
                    ),
                    "outer_groups": config["force_study"]["outer_groups"],
                    "planned_output": str(final_directory),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    input_identity_hash = canonical_json_hash(identity)
    existing = final_directory / "run_manifest.json"
    if existing.exists():
        manifest = load_json_object(existing)
        if manifest.get("input_identity_hash") == input_identity_hash:
            report_path = final_directory / "leakage_audit.json"
            print(f"run_id={run_id}")
            print("reused=true")
            print(f"report={report_path}")
            return 0 if load_json_object(report_path).get("gate_pass") else 2

    temporary = create_temporary_run_directory(split_root, run_id)
    logger = RunLogger(temporary / "events.jsonl", COMMAND_NAME, run_id)
    try:
        logger.emit("INFO", "run_started", session_count=len(source_sessions))
        rows = _build_split_rows(
            source_sessions,
            config=config,
            config_hash=config_hash,
            source_manifest_hash=source_manifest_hash,
            run_id=run_id,
        )
        _validate_rows(rows)
        leakage = _leakage_audit(rows, source_sessions)
        leakage["study_config_hash"] = config_hash
        leakage["source_manifest_hash"] = source_manifest_hash
        if is_manual_only_config(config):
            leakage["dataset_policy"] = "manual_only"
            assert_manual_only_artifact_lineage(leakage)
        frame = pd.DataFrame(rows).sort_values(
            ["scientific_role", "outer_fold", "session_id"],
            kind="stable",
            na_position="last",
        )
        frame.to_parquet(temporary / "split_manifest.parquet", index=False)
        frame.to_csv(
            temporary / "split_manifest.csv", index=False, lineterminator="\n"
        )
        atomic_write_json(temporary / "leakage_audit.json", leakage)
        logger.emit(
            "INFO" if leakage["gate_pass"] else "ERROR",
            "leakage_gate_finished",
            gate_pass=leakage["gate_pass"],
            checks=leakage["checks"],
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
            "split_hash": rows[0]["split_hash"],
            "code_hash": script_hash,
            "input_identity_hash": input_identity_hash,
            "inputs": {
                "source_manifest": str(source_directory / "session_manifest.parquet"),
                "feature_store_report": str(
                    feature_directory / "reconciliation_report.json"
                ),
            },
            "outputs": output_hashes,
            "environment": platform_manifest(),
            "warnings": [],
            "exclusions": [],
            "gate_pass": leakage["gate_pass"],
        }
        if is_manual_only_config(config):
            run_manifest["dataset_policy"] = "manual_only"
            assert_manual_only_artifact_lineage(run_manifest)
        atomic_write_json(temporary / "run_manifest.json", run_manifest)
        published, reused = publish_run_directory(temporary, final_directory)
        print(f"run_id={run_id}")
        print(f"reused={str(reused).lower()}")
        print(f"report={published / 'leakage_audit.json'}")
        return 0 if leakage["gate_pass"] else 2
    except Exception as exc:
        logger.emit("ERROR", "run_failed", error=f"{type(exc).__name__}: {exc}")
        logger.close()
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze six TEST-group outer folds and whole-session no-contact "
            "assignments, then publish an automated leakage audit."
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate upstream artifacts and print the planned fold/output contract.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return run_freeze(arguments.config, dry_run=arguments.dry_run)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
