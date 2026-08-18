"""M0 tests for the frozen live-sensor scientific/runtime contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.live_sensor_contracts import (
    ContractError,
    LIVE_LAYOUT_ID,
    LiveSensorState,
    exact_outputs_allowed,
    load_canonical_live_layout,
    require_transition,
    validate_json_schema,
)
from processing.roi_manager import load_roi_layout


PROJECT = Path(__file__).resolve().parents[1]


def test_study_and_gate_configs_validate_against_frozen_schemas() -> None:
    study_hash = validate_json_schema(
        PROJECT / "config" / "live_sensor_study.json",
        PROJECT / "contracts" / "live_sensor_contract.schema.json",
    )
    gate_hash = validate_json_schema(
        PROJECT / "config" / "live_sensor_release_gates.json",
        PROJECT / "contracts" / "live_sensor_release_gates.schema.json",
    )
    assert len(study_hash) == len(gate_hash) == 64
    assert study_hash != gate_hash


def test_every_json_schema_is_draft_2020_12_valid() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    for schema_path in sorted((PROJECT / "contracts").glob("*.schema.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)


def test_live_layout_matches_dataset_and_legacy_layout_stays_incompatible() -> None:
    live = load_canonical_live_layout(PROJECT / "assets" / "live_sensor" / "roi_layout.json")
    legacy = load_roi_layout(PROJECT / "roi_layout.json")

    assert live.roi_layout_id == LIVE_LAYOUT_ID
    assert (live.frame_width, live.frame_height) == (480, 640)
    assert live.rotation_degrees == 90
    assert live.mirror_horizontal is True
    assert legacy.roi_layout_id == "roi-4691aef42361b7430d3b"
    assert legacy.roi_layout_id != live.roi_layout_id


def test_runtime_state_machine_allows_only_frozen_transitions() -> None:
    require_transition(LiveSensorState.STARTUP, LiveSensorState.SETUP_BLOCKED)
    require_transition(
        LiveSensorState.SETUP_BLOCKED, LiveSensorState.BASELINE_CAPTURING
    )
    require_transition(LiveSensorState.BASELINE_CAPTURING, LiveSensorState.READY)
    require_transition(LiveSensorState.READY, LiveSensorState.LIVE_NO_CONTACT)
    require_transition(LiveSensorState.LIVE_NO_CONTACT, LiveSensorState.LIVE_CONTACT)
    require_transition(LiveSensorState.LIVE_CONTACT, LiveSensorState.LIVE_LIMIT)

    with pytest.raises(ContractError, match="invalid live-sensor transition"):
        require_transition(LiveSensorState.STARTUP, LiveSensorState.LIVE_CONTACT)


@pytest.mark.parametrize(
    ("state", "force_allowed", "localization_allowed"),
    [
        (LiveSensorState.LIVE_NO_CONTACT, True, False),
        (LiveSensorState.LIVE_CONTACT, True, True),
        (LiveSensorState.LIVE_UNCERTAIN, False, False),
        (LiveSensorState.LIVE_LIMIT, False, False),
        (LiveSensorState.RECONNECTING, False, False),
        (LiveSensorState.SETUP_BLOCKED, False, False),
    ],
)
def test_exact_outputs_fail_closed_by_state(
    state: LiveSensorState, force_allowed: bool, localization_allowed: bool
) -> None:
    assert exact_outputs_allowed(state) == (force_allowed, localization_allowed)
