"""Fail-closed tests for the manual-only application dataset firewall."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from core.live_sensor_contracts import ContractError
from scripts.freeze_live_sensor_splits import _assign_no_contact_folds
from scripts.live_sensor_common import (
    assert_manual_only_artifact_lineage,
    assert_manual_only_source_firewall,
    load_study_config,
)


PROJECT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT / "config" / "live_sensor_manual_only.json"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_manual_only_config_is_schema_valid_and_firewalled() -> None:
    config, config_hash, selected = load_study_config(CONFIG_PATH)
    assert selected == CONFIG_PATH.resolve()
    assert len(config_hash) == 64
    assert set(config["sources"]) == {"manual"}


def test_firewall_rejects_any_second_source_before_loading() -> None:
    config = _config()
    config["sources"]["automated"] = copy.deepcopy(config["sources"]["manual"])
    with pytest.raises(ContractError, match="exactly the manual source"):
        assert_manual_only_source_firewall(config)


def test_firewall_rejects_non_whitelisted_archive_name() -> None:
    config = _config()
    config["sources"]["manual"]["archive_path"] = "C:/data/Auto Calibration.zip"
    with pytest.raises(ContractError, match="non-whitelisted"):
        assert_manual_only_source_firewall(config)


def test_firewall_requires_dedicated_output_namespace() -> None:
    config = _config()
    config["paths"]["feature_store_root"] = "analysis_outputs/live_sensor_study/feature_store"
    with pytest.raises(ContractError, match="dedicated"):
        assert_manual_only_source_firewall(config)


@pytest.mark.parametrize(
    "artifact",
    [
        {"inputs": {"automated": {"sha256": "a" * 64}}},
        {"source": "C:/data/Auto Calibration.zip"},
    ],
)
def test_artifact_lineage_rejects_forbidden_sources(artifact: dict) -> None:
    with pytest.raises(ContractError, match="forbidden"):
        assert_manual_only_artifact_lineage(artifact)


def test_no_contact_assignment_is_date_stratified_and_deterministic() -> None:
    sessions = pd.DataFrame(
        [
            {
                "session_id": f"D1-{index}",
                "collection_day": "2026-08-08",
                "scientific_role": "no_contact",
            }
            for index in range(7)
        ]
        + [
            {
                "session_id": f"D2-{index}",
                "collection_day": "2026-08-09",
                "scientific_role": "no_contact",
            }
            for index in range(4)
        ]
    )
    first = _assign_no_contact_folds(sessions, 6)
    second = _assign_no_contact_folds(sessions.sample(frac=1.0, random_state=1), 6)
    assert first == second
    counts = pd.Series(first).value_counts()
    assert int(counts.max() - counts.min()) <= 1
