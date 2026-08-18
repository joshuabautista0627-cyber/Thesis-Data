from __future__ import annotations

import json
from pathlib import Path

from scripts.performance_profile import run_performance_profile


def test_longer_simulation_profile_preserves_integrity_without_backlog(
    tmp_path: Path,
) -> None:
    # Five times the ordinary 24-frame smoke slice, but at a compact frame size
    # so this endurance/integrity check remains suitable for the full test suite.
    result = run_performance_profile(
        tmp_path,
        session_directory_name="performance-profile",
        frame_count=120,
        width=96,
        height=72,
        fps=30.0,
        loadcell_rate_hz=80.0,
        seed=43,
    )

    assert result.simulated_duration_s > 3.9
    assert result.frame_count == 120
    assert result.video_frame_count == 120
    assert result.frame_feature_row_count == 120
    assert result.master_row_count == 120
    assert result.frame_ids_contiguous
    assert result.one_to_one_identity_passed
    assert result.integrity_requirement_passed
    assert result.backlog_requirement_passed
    assert not result.projected_backlog_increasing
    assert (
        result.projected_max_pipeline_backlog_events
        < result.recording_queue_capacity_events
    )
    assert result.projected_final_pipeline_backlog_events <= 1
    assert result.automatic_graph_artifact_count == 15
    assert result.automatic_graphs_complete
    assert result.partial_artifact_count == 0
    assert result.overall_passed


def test_performance_profile_reports_every_required_stage_and_strict_json(
    tmp_path: Path,
) -> None:
    result = run_performance_profile(
        tmp_path,
        session_directory_name="performance-stage-profile",
        frame_count=24,
        width=72,
        height=54,
        fps=24.0,
        loadcell_rate_hz=80.0,
        seed=47,
    )

    assert set(result.stage_timings_s) == {
        "workflow_setup",
        "recorder_start",
        "synthetic_camera_generation",
        "synthetic_loadcell_generation",
        "loadcell_incremental_write",
        "optical_processing",
        "video_and_frame_csv_write",
        "finalization_and_synchronization",
        "artifact_validation",
    }
    assert all(duration >= 0.0 for duration in result.stage_timings_s.values())
    assert result.slowest_stage in result.stage_timings_s
    assert result.effective_recording_pipeline_fps > 0.0
    assert result.realtime_capacity_ratio > 0.0
    payload = json.dumps(result.to_dict(), allow_nan=False)
    assert json.loads(payload)["overall_passed"] is True

