"""Leakage-gate tests for frozen TEST-group splits."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts.freeze_live_sensor_splits import _build_split_rows, _leakage_audit


PROJECT = Path(__file__).resolve().parents[1]


def _sessions() -> pd.DataFrame:
    rows = [
        {
            "archive_id": "manual",
            "session_id": f"R{roi}_TEST{group}",
            "scientific_role": "model_primary",
            "test_group": f"TEST{group}",
            "collection_day": "2026-08-08",
            "target_roi": roi,
        }
        for roi in range(1, 10)
        for group in range(1, 7)
    ]
    rows.extend(
        {
            "archive_id": "manual",
            "session_id": f"NC{index}",
            "scientific_role": "no_contact",
            "test_group": None,
            "collection_day": "2026-08-08",
            "target_roi": (index % 9) + 1,
        }
        for index in range(11)
    )
    rows.append(
        {
            "archive_id": "automated",
            "session_id": "AUTO1",
            "scientific_role": "characterization",
            "test_group": None,
            "collection_day": "2026-08-06",
            "target_roi": 1,
        }
    )
    return pd.DataFrame(rows)


def test_grouped_split_has_disjoint_complete_sessions_and_all_rois() -> None:
    config = json.loads(
        (PROJECT / "config" / "live_sensor_study.json").read_text(encoding="utf-8")
    )
    sessions = _sessions()
    rows = _build_split_rows(
        sessions,
        config=config,
        config_hash="a" * 64,
        source_manifest_hash="b" * 64,
        run_id="test-run",
    )

    audit = _leakage_audit(rows, sessions)

    assert audit["gate_pass"]
    assert audit["outer_fold_counts"] == {str(fold): 9 for fold in range(1, 7)}
    assert sum(audit["no_contact_fold_counts"].values()) == 11
    assert len({row["split_hash"] for row in rows}) == 1


def test_duplicate_session_fails_leakage_gate() -> None:
    config = json.loads(
        (PROJECT / "config" / "live_sensor_study.json").read_text(encoding="utf-8")
    )
    sessions = _sessions()
    sessions = pd.concat((sessions, sessions.iloc[[0]]), ignore_index=True)
    rows = _build_split_rows(
        sessions,
        config=config,
        config_hash="a" * 64,
        source_manifest_hash="b" * 64,
        run_id="test-run",
    )
    assert not _leakage_audit(rows, sessions)["gate_pass"]
