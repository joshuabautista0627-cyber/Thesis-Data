from __future__ import annotations

import json
from pathlib import Path

import cv2
import pandas as pd

from scripts.simulation_smoke import run_simulation_smoke


def test_complete_simulation_vertical_slice(tmp_path: Path) -> None:
    result = run_simulation_smoke(
        tmp_path,
        session_directory_name="simulation-smoke",
        frame_count=24,
        width=96,
        height=72,
        fps=24.0,
        loadcell_rate_hz=80.0,
        seed=17,
    )
    session = Path(result.session_directory)

    assert result.frame_count == result.video_frame_count == result.master_row_count == 24
    assert result.missing_force_count == 0
    assert result.baseline_drift_passed
    assert result.calibration_snr > 10.0
    assert result.calibration_loaded_cv_percent < 2.0
    assert result.calibration_verification_error_percent < 5.0
    assert (session / result.video_filename).is_file()
    assert not list(session.glob("*.csv.partial"))
    assert not list(session.glob("session_video.partial.*"))

    frame_rows = pd.read_csv(session / "frame_features.csv")
    master_rows = pd.read_csv(session / "master_synchronized.csv")
    assert frame_rows["capture_frame_id"].tolist() == list(range(24))
    assert master_rows["capture_frame_id"].tolist() == list(range(24))
    assert master_rows["synchronization_valid"].all()

    status = json.loads((session / "session_status.json").read_text(encoding="utf-8"))
    config = json.loads((session / "session_config.json").read_text(encoding="utf-8"))
    assert status["complete"] is True and status["partial"] is False
    assert status["accepted_frame_count"] == 24
    assert config["simulation_mode"] is True
    assert config["video_filename"] == result.video_filename
    assert config["video_codec"] == result.video_codec
    assert status["automatic_graph_summary"]["status"] == "complete"
    assert status["automatic_heatmap_summary_status"] == "complete"
    assert status["automatic_temporal_line_status"] == "complete"
    assert status["automatic_spatial_profile_status"] == "complete"
    assert status["trial_mean_contact_mean_delta_v_status"] == "available"
    graph_directory = session / "spatial_graphs"
    for stem in (
        "trial_peak_mean_delta_v",
        "trial_mean_contact_mean_delta_v",
        "trial_integrated_delta_v",
        "trial_roi_intensity_over_time",
        "trial_peak_spatial_profile",
    ):
        for suffix in (".png", ".csv", ".json"):
            assert (graph_directory / f"{stem}{suffix}").is_file()

    capture = cv2.VideoCapture(str(session / result.video_filename))
    decoded = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        assert frame.shape == (72, 96, 3)
        decoded += 1
    capture.release()
    assert decoded == 24
