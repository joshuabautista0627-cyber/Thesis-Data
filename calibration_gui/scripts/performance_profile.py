"""Deterministic long-session simulation profiler.

This command runs the same production processing, incremental recording,
synchronization, video validation, and automatic graph finalization path as the
simulation smoke test.  It then audits permanent frame identity and reports a
real-time backlog projection from the measured per-frame service durations.

The default 900 frames represent about 30 seconds at 30 FPS.  Tests may select
smaller dimensions and counts while retaining the same integrity checks.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from core.models import ApplicationConfig
from scripts.simulation_smoke import SimulationSmokeResult, run_simulation_smoke


AUTOMATIC_GRAPH_STEMS = (
    "trial_peak_mean_delta_v",
    "trial_mean_contact_mean_delta_v",
    "trial_integrated_delta_v",
    "trial_roi_intensity_over_time",
    "trial_peak_spatial_profile",
)
GRAPH_SUFFIXES = (".png", ".csv", ".json")


@dataclass(frozen=True, slots=True)
class PerformanceProfileResult:
    """Machine-readable evidence from one deterministic endurance run."""

    session_directory: str
    simulated_duration_s: float
    requested_fps: float
    frame_count: int
    video_frame_count: int
    frame_feature_row_count: int
    master_row_count: int
    loadcell_sample_count: int
    frame_ids_contiguous: bool
    one_to_one_identity_passed: bool
    automatic_graph_artifact_count: int
    automatic_graphs_complete: bool
    partial_artifact_count: int
    stage_timings_s: dict[str, float]
    slowest_stage: str
    effective_recording_pipeline_fps: float
    realtime_capacity_ratio: float
    projected_max_pipeline_backlog_events: int
    projected_final_pipeline_backlog_events: int
    projected_backlog_growth_events: float
    projected_backlog_increasing: bool
    recording_queue_capacity_events: int
    projected_peak_capture_to_completion_ms: float
    backlog_measurement_method: str
    backlog_requirement_passed: bool
    integrity_requirement_passed: bool
    realtime_capacity_goal_met: bool
    overall_passed: bool
    generated_graph_files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_capture_ids(path: Path) -> list[int]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        try:
            return [int(row["capture_frame_id"]) for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid capture_frame_id data in {path}") from exc


def _loadcell_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return sum(1 for _row in csv.DictReader(handle))


def _stage_timings(result: SimulationSmokeResult) -> dict[str, float]:
    return {
        "workflow_setup": result.workflow_setup_s,
        "recorder_start": result.recorder_start_s,
        "synthetic_camera_generation": result.synthetic_camera_generation_s,
        "synthetic_loadcell_generation": result.synthetic_loadcell_generation_s,
        "loadcell_incremental_write": result.loadcell_incremental_write_s,
        "optical_processing": result.optical_processing_s,
        "video_and_frame_csv_write": result.video_and_frame_csv_write_s,
        "finalization_and_synchronization": result.finalization_s,
        "artifact_validation": result.artifact_validation_s,
    }


def run_performance_profile(
    output_root: str | Path,
    *,
    session_directory_name: str | None = None,
    frame_count: int = 900,
    width: int = 320,
    height: int = 240,
    fps: float = 30.0,
    loadcell_rate_hz: float = 80.0,
    seed: int = 20260803,
) -> PerformanceProfileResult:
    """Run and audit one longer deterministic session.

    The acquisition queue projection uses measured optical, video, CSV, and
    load-cell write service durations and replays them against the requested
    camera schedule.  It does not count camera generation time because camera
    acquisition has a separate owner in the application architecture.
    """

    if frame_count < 20:
        raise ValueError("performance profile requires at least 20 frames")
    if fps <= 0.0:
        raise ValueError("fps must be positive")

    smoke = run_simulation_smoke(
        output_root,
        session_directory_name=session_directory_name,
        frame_count=frame_count,
        width=width,
        height=height,
        fps=fps,
        loadcell_rate_hz=loadcell_rate_hz,
        seed=seed,
    )
    session = Path(smoke.session_directory)
    frame_ids = _read_capture_ids(session / "frame_features.csv")
    master_ids = _read_capture_ids(session / "master_synchronized.csv")
    loadcell_rows = _loadcell_row_count(session / "loadcell_raw.csv")
    expected_ids = list(range(frame_count))
    frame_ids_contiguous = frame_ids == expected_ids and master_ids == expected_ids
    one_to_one = (
        frame_ids_contiguous
        and smoke.frame_count == frame_count
        and smoke.video_frame_count == frame_count
        and smoke.master_row_count == frame_count
        and loadcell_rows == smoke.loadcell_sample_count
    )

    graph_directory = session / "spatial_graphs"
    expected_graph_files = tuple(
        f"{stem}{suffix}"
        for stem in AUTOMATIC_GRAPH_STEMS
        for suffix in GRAPH_SUFFIXES
    )
    generated_graph_files = tuple(
        sorted(
            path.name
            for path in graph_directory.iterdir()
            if path.is_file() and path.name in expected_graph_files
        )
    )
    automatic_graphs_complete = set(generated_graph_files) == set(
        expected_graph_files
    )
    partial_artifacts = tuple(session.rglob("*.partial")) + tuple(
        session.rglob("*.partial.*")
    )

    project_root = Path(__file__).resolve().parents[1]
    config = ApplicationConfig.load_json(
        project_root / "config" / "default_config.json"
    )
    queue_capacity = config.recording.recording_queue_size
    backlog_passed = (
        not smoke.projected_backlog_increasing
        and smoke.projected_max_pipeline_backlog_events < queue_capacity
    )
    integrity_passed = (
        one_to_one
        and automatic_graphs_complete
        and not partial_artifacts
    )
    capacity_ratio = smoke.effective_recording_pipeline_fps / fps
    realtime_capacity_goal_met = capacity_ratio >= 1.0

    return PerformanceProfileResult(
        session_directory=str(session.resolve()),
        simulated_duration_s=(frame_count - 1) / fps,
        requested_fps=fps,
        frame_count=smoke.frame_count,
        video_frame_count=smoke.video_frame_count,
        frame_feature_row_count=len(frame_ids),
        master_row_count=len(master_ids),
        loadcell_sample_count=loadcell_rows,
        frame_ids_contiguous=frame_ids_contiguous,
        one_to_one_identity_passed=one_to_one,
        automatic_graph_artifact_count=len(generated_graph_files),
        automatic_graphs_complete=automatic_graphs_complete,
        partial_artifact_count=len(partial_artifacts),
        stage_timings_s=_stage_timings(smoke),
        slowest_stage=smoke.slowest_stage,
        effective_recording_pipeline_fps=(
            smoke.effective_recording_pipeline_fps
        ),
        realtime_capacity_ratio=capacity_ratio,
        projected_max_pipeline_backlog_events=(
            smoke.projected_max_pipeline_backlog_events
        ),
        projected_final_pipeline_backlog_events=(
            smoke.projected_final_pipeline_backlog_events
        ),
        projected_backlog_growth_events=(
            smoke.projected_backlog_growth_events
        ),
        projected_backlog_increasing=smoke.projected_backlog_increasing,
        recording_queue_capacity_events=queue_capacity,
        projected_peak_capture_to_completion_ms=(
            smoke.projected_peak_capture_to_completion_ms
        ),
        backlog_measurement_method=(
            "Measured production optical/video/CSV/load-cell service durations "
            "replayed against the requested camera schedule; separate camera "
            "generation time is excluded from the single recording-owner queue."
        ),
        backlog_requirement_passed=backlog_passed,
        integrity_requirement_passed=integrity_passed,
        realtime_capacity_goal_met=realtime_capacity_goal_met,
        overall_passed=integrity_passed and backlog_passed,
        generated_graph_files=generated_graph_files,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Profile a deterministic long simulation recording and audit "
            "frame identity, backlog, and final artifacts."
        )
    )
    parser.add_argument("--output-root", type=Path, default=Path("output"))
    parser.add_argument("--session-name", default=None)
    parser.add_argument("--frames", type=int, default=900)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--loadcell-rate", type=float, default=80.0)
    parser.add_argument("--seed", type=int, default=20260803)
    parser.add_argument(
        "--require-realtime-capacity",
        action="store_true",
        help=(
            "also fail if measured processing throughput is below requested FPS; "
            "integrity and sustained-backlog failures always fail"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    result = run_performance_profile(
        args.output_root,
        session_directory_name=args.session_name,
        frame_count=args.frames,
        width=args.width,
        height=args.height,
        fps=args.fps,
        loadcell_rate_hz=args.loadcell_rate,
        seed=args.seed,
    )
    print(json.dumps(result.to_dict(), indent=2, allow_nan=False))
    if not result.overall_passed:
        return 1
    if args.require_realtime_capacity and not result.realtime_capacity_goal_met:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

