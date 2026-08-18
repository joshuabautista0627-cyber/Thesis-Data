"""Fast tests for M1 archive classification and provenance helpers."""

from __future__ import annotations

import pytest

from core.live_sensor_contracts import ContractError
from scripts.audit_live_sensor_archives import (
    _primary_coverage,
    _scientific_assignment,
    _session_identity,
)
from scripts.live_sensor_common import make_run_id, resolve_project_path


@pytest.mark.parametrize(
    ("archive_id", "session_id", "role", "reason", "contact_role"),
    [
        (
            "manual",
            "2026-08-08_142130_319119_1_R1_TEST1",
            "model_primary",
            None,
            "contact",
        ),
        (
            "manual",
            "2026-08-08_174349_809996_1_R1_TEST1_v2",
            "replay_only",
            "outside_primary_test1_6",
            "contact",
        ),
        (
            "manual",
            "2026-08-08_173847_719544_1_R1_NOCONTACT_v2",
            "no_contact",
            None,
            "no_contact",
        ),
        (
            "automated",
            "2026-08-06_144254_451441_1_R1_C1",
            "characterization",
            None,
            "mixed_protocol",
        ),
    ],
)
def test_scientific_roles_keep_primary_and_repeated_evidence_separate(
    archive_id: str,
    session_id: str,
    role: str,
    reason: str | None,
    contact_role: str,
) -> None:
    actual_role, inclusion, actual_reason, actual_contact = _scientific_assignment(
        archive_id, session_id
    )
    assert (actual_role, inclusion, actual_reason, actual_contact) == (
        role,
        "included",
        reason,
        contact_role,
    )


def test_session_identity_is_derived_without_relabeling() -> None:
    assert _session_identity(
        "Manual Calibration/ROI 9/2026-08-08_165709_643597_1_R9_TEST6"
    ) == (
        "2026-08-08_165709_643597_1_R9_TEST6",
        "2026-08-08",
        9,
        "TEST6",
    )


def test_primary_coverage_requires_one_session_in_every_roi_test_cell() -> None:
    rows = [
        {
            "archive_id": "manual",
            "scientific_role": "model_primary",
            "target_roi": roi,
            "test_group": f"TEST{test}",
        }
        for roi in range(1, 10)
        for test in range(1, 7)
    ]
    assert _primary_coverage(rows)["all_54_cells_exactly_one"]
    rows.append(rows[0].copy())
    assert not _primary_coverage(rows)["all_54_cells_exactly_one"]


def test_run_ids_are_deterministic_and_writes_cannot_leave_project() -> None:
    identity = {"study_config_hash": "a" * 64, "archive_hashes": {"manual": "b" * 64}}
    assert make_run_id("archive-audit", identity) == make_run_id(
        "archive-audit", identity
    )
    with pytest.raises(ContractError, match="leaves the project"):
        resolve_project_path("C:/Windows/Temp/live-sensor", writable=True)
