"""Audit a completed physical camera/load-cell recording session."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import pandas as pd


def audit_session(session_dir: Path) -> dict[str, object]:
    master = pd.read_csv(session_dir / "master_synchronized.csv")
    features = pd.read_csv(session_dir / "frame_features.csv")
    loadcell = pd.read_csv(session_dir / "loadcell_raw.csv")
    dictionary = pd.read_csv(session_dir / "data_dictionary.csv")
    session_config = json.loads(
        (session_dir / "session_config.json").read_text(encoding="utf-8")
    )
    prompt_start = float(session_config.get("press_prompt_start_s", 3.0))
    prompt_end = float(session_config.get("press_prompt_end_s", 6.0))
    result: dict[str, object] = {
        "master_rows": len(master),
        "raw_rows": len(loadcell),
        "elapsed_s": float(master["elapsed_time_s"].max()),
        "missing_force": int(master["force_N"].isna().sum()),
        "sync_invalid": int((~master["synchronization_valid"].astype(bool)).sum()),
        "feature_rows": len(features),
    }
    accepted_ids = list(range(len(master)))
    result["frame_ids_contiguous"] = (
        master["capture_frame_id"].tolist() == accepted_ids
        and features["capture_frame_id"].tolist() == accepted_ids
    )
    synchronization_gaps = master["nearest_sample_gap_ms"].dropna().astype(float)
    result["synchronization_gap_ms"] = {
        "minimum": float(synchronization_gaps.min()),
        "median": float(synchronization_gaps.median()),
        "p95": float(synchronization_gaps.quantile(0.95)),
        "maximum": float(synchronization_gaps.max()),
    }
    core_headers = {
        filename: set(pd.read_csv(session_dir / filename, nrows=0).columns)
        for filename in ("frame_features.csv", "loadcell_raw.csv", "master_synchronized.csv")
    }
    result["data_dictionary_core_headers_exact"] = all(
        set(dictionary.loc[dictionary["artifact_scope"] == filename, "column_name"])
        == headers
        for filename, headers in core_headers.items()
    )
    result["data_dictionary_keys_unique"] = not dictionary.duplicated(
        ["artifact_scope", "column_name"]
    ).any()

    phases: dict[str, object] = {}
    for label, start, end in (
        ("pre", 0.0, prompt_start),
        ("press", prompt_start, prompt_end),
        ("post", prompt_end, float("inf")),
    ):
        phase = master[
            (master["elapsed_time_s"] >= start) & (master["elapsed_time_s"] < end)
        ]
        phases[label] = {
            "n": len(phase),
            "force_mean_N": float(phase["force_N"].mean()),
            "force_median_N": float(phase["force_N"].median()),
            "force_min_N": float(phase["force_N"].min()),
            "force_max_N": float(phase["force_N"].max()),
            "raw_mean": float(phase["interpolated_raw_adc"].mean()),
            "roi5_delta_mean_peak": float(phase["roi5_delta_v_mean"].max()),
            "dominant_rois": {
                str(key): int(value)
                for key, value in phase["predicted_dominant_roi"].value_counts().head(3).items()
            },
        }
    result["phases"] = phases
    result["press_prompt_start_s"] = prompt_start
    result["press_prompt_end_s"] = prompt_end
    roi_peaks = {
        str(roi_id): float(master[f"roi{roi_id}_delta_v_mean"].max())
        for roi_id in range(1, 10)
    }
    result["roi_delta_v_mean_peaks"] = roi_peaks
    force_peak_index = master["force_N"].idxmax()
    result["force_peak_elapsed_s"] = float(master.loc[force_peak_index, "elapsed_time_s"])
    result["force_peak_N"] = float(master.loc[force_peak_index, "force_N"])
    result["final_two_seconds_force_median_N"] = float(
        master.loc[
            master["elapsed_time_s"] >= master["elapsed_time_s"].max() - 2.0,
            "force_N",
        ].median()
    )
    result["raw_sample_id_gaps"] = int(
        (loadcell["arduino_sample_id"].diff().dropna() != 1).sum()
    )

    videos: dict[str, object] = {}
    for video_path in sorted(session_dir.glob("*.mp4")):
        capture = cv2.VideoCapture(str(video_path))
        metadata_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        decoded_frames = 0
        while True:
            ok, _frame = capture.read()
            if not ok:
                break
            decoded_frames += 1
        capture.release()
        videos[video_path.name] = {
            "metadata_frames": metadata_frames,
            "decoded_frames": decoded_frames,
            "fps": fps,
            "size": [width, height],
            "bytes": video_path.stat().st_size,
            "sha256": hashlib.sha256(video_path.read_bytes()).hexdigest(),
        }
    result["videos"] = videos

    status = json.loads((session_dir / "session_status.json").read_text(encoding="utf-8"))
    result["status"] = {
        key: status.get(key)
        for key in (
            "session_status",
            "lifecycle",
            "complete",
            "partial",
            "accepted_frame_count",
            "feature_row_count",
            "video_frame_count",
            "loadcell_sample_count",
            "required_artifacts_written",
            "automatic_heatmap_summary_status",
            "automatic_spatial_profile_status",
            "automatic_temporal_line_status",
        )
    }
    result["graph_companion_files"] = len(list((session_dir / "spatial_graphs").glob("*")))
    result["partial_files"] = [
        path.name
        for path in session_dir.rglob("*")
        if path.is_file() and (".partial" in path.name or path.suffix == ".tmp")
    ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_session(args.session_dir)
    rendered = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
