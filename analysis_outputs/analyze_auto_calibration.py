from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


ARCHIVE = Path(r"C:\Users\DLSU\Downloads\Data Collection\Auto Calibration.zip")
OUTPUT_DIR = Path(__file__).resolve().parent
SESSION_RE = re.compile(
    r"^Auto Calibration/ROI (?P<folder_roi>\d+)/(?P<session>[^/]+)/session_status\.json$"
)
NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{6})_(?P<micro>\d+)_1_"
    r"R(?P<target>\d+)_C(?P<condition>\d+)(?P<suffix>_10)?$"
)


def safe_float(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def percentile(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * p
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def stdev(values):
    values = [v for v in values if v is not None]
    return statistics.stdev(values) if len(values) > 1 else 0.0 if values else None


def read_json(zf, name):
    return json.loads(zf.read(name).decode("utf-8-sig"))


def read_spatial(zf, name):
    text = zf.read(name).decode("utf-8-sig")
    return {int(r["roi"]): float(r["value"]) for r in csv.DictReader(io.StringIO(text))}


def summarize_spatial(values, target):
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    target_value = values[target]
    rank = next(i for i, (roi, _) in enumerate(ordered, 1) if roi == target)
    non_target_max = max(v for roi, v in values.items() if roi != target)
    total = sum(values.values())
    return {
        "target_value": target_value,
        "winner_roi": ordered[0][0],
        "target_rank": rank,
        "target_to_best_other_ratio": target_value / non_target_max if non_target_max else None,
        "target_share": target_value / total if total else None,
    }


def summarize_loadcell(zf, name):
    with zf.open(name) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        forces = []
        contact_forces = []
        valid = 0
        exceeded = 0
        aborted = 0
        errors = Counter()
        rows = 0
        for row in reader:
            rows += 1
            force = safe_float(row.get("force_N"))
            if force is not None:
                forces.append(force)
                if force >= 0.05:
                    contact_forces.append(force)
            if row.get("loadcell_valid") == "True":
                valid += 1
            if row.get("force_limit_exceeded") == "True":
                exceeded += 1
            if row.get("sequence_aborted") == "True":
                aborted += 1
            errors[row.get("error_code") or ""] += 1
    return {
        "loadcell_rows_profiled": rows,
        "loadcell_valid_rate": valid / rows if rows else None,
        "force_N_max": max(forces) if forces else None,
        "force_N_p95": percentile(forces, 0.95),
        "force_N_contact_mean": mean(contact_forces),
        "force_limit_exceeded_rows": exceeded,
        "sequence_aborted_rows": aborted,
        "loadcell_error_rows": sum(v for k, v in errors.items() if k not in ("", "NONE")),
    }


def summarize_frames(zf, name):
    with zf.open(name) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        header = next(reader)
        wanted = [
            "frame_valid",
            "baseline_valid",
            "loadcell_valid",
            "saturation_warning",
            "error_code",
            "predicted_dominant_roi",
            "predicted_roi_matches_target",
            "localization_confidence",
        ]
        idx = {k: header.index(k) for k in wanted if k in header}
        counts = Counter()
        confidences = []
        predictions = Counter()
        rows = 0
        for row in reader:
            rows += 1
            for key in ("frame_valid", "baseline_valid", "loadcell_valid", "saturation_warning", "predicted_roi_matches_target"):
                if key in idx and row[idx[key]] == "True":
                    counts[key] += 1
            if "error_code" in idx and row[idx["error_code"]] not in ("", "NONE"):
                counts["error"] += 1
            if "localization_confidence" in idx:
                value = safe_float(row[idx["localization_confidence"]])
                if value is not None:
                    confidences.append(value)
            if "predicted_dominant_roi" in idx:
                value = row[idx["predicted_dominant_roi"]]
                if value:
                    predictions[value] += 1
    localized = sum(predictions.values())
    return {
        "frame_rows_profiled": rows,
        "frame_valid_rate": counts["frame_valid"] / rows if rows else None,
        "baseline_valid_rate": counts["baseline_valid"] / rows if rows else None,
        "frame_loadcell_valid_rate": (
            counts["loadcell_valid"] / rows if rows and "loadcell_valid" in idx else None
        ),
        "saturation_warning_rate": counts["saturation_warning"] / rows if rows else None,
        "frame_error_rate": counts["error"] / rows if rows else None,
        "localized_frame_rate": localized / rows if rows else None,
        "localization_accuracy": counts["predicted_roi_matches_target"] / localized if localized else None,
        "localization_confidence_mean": mean(confidences),
        "dominant_prediction_mode": predictions.most_common(1)[0][0] if predictions else "",
    }


def build_session_rows():
    rows = []
    with zipfile.ZipFile(ARCHIVE) as zf:
        status_entries = []
        for name in zf.namelist():
            match = SESSION_RE.match(name)
            if match:
                status_entries.append((int(match.group("folder_roi")), match.group("session"), name))

        for index, (folder_roi, session_name, status_name) in enumerate(sorted(status_entries), 1):
            match = NAME_RE.match(session_name)
            if not match:
                raise ValueError(f"Unexpected session name: {session_name}")
            base = status_name[: -len("session_status.json")]
            status = read_json(zf, status_name)
            config = read_json(zf, base + "session_config.json")
            calibration = read_json(zf, base + "loadcell_calibration.json")
            baseline = read_json(zf, base + "baseline_summary.json")
            labels = config["trial_labels"]
            motion = config.get("printer_motion", {}).get("settings", {})
            target = int(labels["target_roi_ground_truth"])
            baseline_roi = next(r for r in baseline.get("rois", []) if int(r["roi_id"]) == target)
            camera_controls = (
                baseline.get("camera_settings", {})
                .get("controls", {})
                .get("current_driver_values", {})
            )
            timestamp = datetime.strptime(
                f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H%M%S"
            ).isoformat(sep=" ")
            if target != folder_roi or target != int(match.group("target")):
                raise ValueError(f"Target ROI mismatch in {session_name}")

            row = {
                "session_folder": session_name,
                "timestamp_local_name": timestamp,
                "batch": labels["session_id"],
                "specimen_or_participant_id": labels.get("specimen_or_participant_id", ""),
                "trial_id": labels["trial_id"],
                "target_roi": target,
                "condition_index": int(match.group("condition")),
                "suffix_10": bool(match.group("suffix")),
                "complete": status.get("complete"),
                "partial": status.get("partial"),
                "status_error_count": len(status.get("errors", [])),
                "accepted_frame_count": status.get("accepted_frame_count"),
                "feature_row_count": status.get("feature_row_count"),
                "loadcell_sample_count": status.get("loadcell_sample_count"),
                "video_frame_count": status.get("video_frame_count"),
                "output_fps": status.get("output_fps"),
                "readiness_at_start": config.get("readiness_at_start", {}).get("ready_to_record"),
                "calibration_verified_at_start": config.get("readiness_at_start", {}).get("calibration_verified"),
                "baseline_drift_passed": config.get("baseline_drift_passed"),
                "baseline_drift_mean_v": config.get("pre_recording_baseline_drift_mean_v"),
                "baseline_valid": baseline.get("valid"),
                "baseline_id": baseline.get("baseline_id"),
                "camera_fingerprint": baseline.get("camera_fingerprint"),
                "processing_fingerprint": baseline.get("processing_fingerprint"),
                "roi_layout_id": baseline.get("roi_layout_id"),
                "baseline_target_mean_v": baseline_roi.get("mean_v"),
                "baseline_target_std_v": baseline_roi.get("std_v"),
                "camera_exposure": camera_controls.get("exposure"),
                "camera_gain": camera_controls.get("gain"),
                "camera_white_balance": camera_controls.get("white_balance"),
                "camera_brightness": camera_controls.get("brightness"),
                "camera_contrast": camera_controls.get("contrast"),
                "calibration_quality_passed": calibration.get("quality_passed"),
                "calibration_id": calibration.get("calibration_id"),
                "counts_per_gram": calibration.get("counts_per_gram"),
                "calibration_snr": calibration.get("calibration_snr"),
                "calibration_loaded_cv_percent": calibration.get("loaded_window_cv_percent"),
                "motion_cycles": motion.get("cycles"),
                "displacement_mm": motion.get("displacement_mm"),
                "down_feed_mm_min": motion.get("down_feed_mm_min"),
                "roi_only_magnification": config.get("motion_magnification", {}).get("roi_only"),
                "lower_cutoff_hz": config.get("motion_magnification", {}).get("lower_cutoff_hz"),
            }
            for key, filename in {
                "integrated": "trial_integrated_delta_v.csv",
                "mean_contact": "trial_mean_contact_mean_delta_v.csv",
                "peak": "trial_peak_mean_delta_v.csv",
                "peak_profile": "trial_peak_spatial_profile.csv",
            }.items():
                values = read_spatial(zf, base + "spatial_graphs/" + filename)
                for metric, value in summarize_spatial(values, target).items():
                    row[f"{key}_{metric}"] = value
            row.update(summarize_loadcell(zf, base + "loadcell_raw.csv"))
            row.update(summarize_frames(zf, base + "frame_features.csv"))
            rows.append(row)
            if index % 18 == 0:
                print(f"Profiled {index}/{len(status_entries)} sessions")
    return rows


def aggregate(rows):
    total_frames = sum(r["frame_rows_profiled"] for r in rows)
    total_loadcell = sum(r["loadcell_rows_profiled"] for r in rows)
    def weighted(key, weight):
        eligible = [r for r in rows if r[key] is not None]
        denominator = sum(r[weight] for r in eligible)
        return sum(r[key] * r[weight] for r in eligible) / denominator if denominator else None
    def weighted_localization(group):
        denominator = sum(r["localized_frame_rate"] * r["frame_rows_profiled"] for r in group)
        if not denominator:
            return None
        return sum(
            (r["localization_accuracy"] or 0) * r["localized_frame_rate"] * r["frame_rows_profiled"]
            for r in group
        ) / denominator
    summary = {
        "archive": str(ARCHIVE),
        "session_count": len(rows),
        "target_rois": sorted({r["target_roi"] for r in rows}),
        "batches": dict(Counter(r["batch"] for r in rows)),
        "first_session": min(r["timestamp_local_name"] for r in rows),
        "last_session": max(r["timestamp_local_name"] for r in rows),
        "complete_sessions": sum(bool(r["complete"]) for r in rows),
        "sessions_with_status_errors": sum(r["status_error_count"] > 0 for r in rows),
        "sessions_not_ready_at_start": sum(not bool(r["readiness_at_start"]) for r in rows),
        "sessions_calibration_unverified_at_start": sum(not bool(r["calibration_verified_at_start"]) for r in rows),
        "baseline_drift_passed_sessions": sum(bool(r["baseline_drift_passed"]) for r in rows),
        "baseline_valid_sessions": sum(bool(r["baseline_valid"]) for r in rows),
        "calibration_quality_passed_sessions": sum(bool(r["calibration_quality_passed"]) for r in rows),
        "distinct_calibration_ids": len({r["calibration_id"] for r in rows}),
        "distinct_camera_fingerprints": len({r["camera_fingerprint"] for r in rows}),
        "distinct_processing_fingerprints": len({r["processing_fingerprint"] for r in rows}),
        "distinct_roi_layout_ids": len({r["roi_layout_id"] for r in rows}),
        "total_frame_rows_profiled": total_frames,
        "total_loadcell_rows_profiled": total_loadcell,
        "frame_valid_rate_weighted": weighted("frame_valid_rate", "frame_rows_profiled"),
        "baseline_valid_rate_weighted": weighted("baseline_valid_rate", "frame_rows_profiled"),
        "frame_loadcell_valid_rate_weighted": weighted("frame_loadcell_valid_rate", "frame_rows_profiled"),
        "frame_error_rate_weighted": weighted("frame_error_rate", "frame_rows_profiled"),
        "saturation_warning_rate_weighted": weighted("saturation_warning_rate", "frame_rows_profiled"),
        "localized_frame_rate_weighted": weighted("localized_frame_rate", "frame_rows_profiled"),
        "localization_accuracy_weighted": weighted_localization(rows),
        "loadcell_valid_rate_weighted": weighted("loadcell_valid_rate", "loadcell_rows_profiled"),
        "force_limit_exceeded_rows": sum(r["force_limit_exceeded_rows"] for r in rows),
        "sequence_aborted_rows": sum(r["sequence_aborted_rows"] for r in rows),
        "loadcell_error_rows": sum(r["loadcell_error_rows"] for r in rows),
        "peak_target_winner_sessions": sum(r["peak_winner_roi"] == r["target_roi"] for r in rows),
        "integrated_target_winner_sessions": sum(r["integrated_winner_roi"] == r["target_roi"] for r in rows),
        "mean_contact_target_winner_sessions": sum(r["mean_contact_winner_roi"] == r["target_roi"] for r in rows),
        "peak_profile_exact_match_sessions": sum(
            abs(r["peak_target_value"] - r["peak_profile_target_value"]) < 1e-12
            and r["peak_winner_roi"] == r["peak_profile_winner_roi"] for r in rows
        ),
    }

    by_target = []
    for target in sorted({r["target_roi"] for r in rows}):
        group = [r for r in rows if r["target_roi"] == target]
        peak = [r["peak_target_value"] for r in group]
        integrated = [r["integrated_target_value"] for r in group]
        by_target.append({
            "target_roi": target,
            "sessions": len(group),
            "peak_target_mean": mean(peak),
            "peak_target_sd": stdev(peak),
            "peak_target_cv": stdev(peak) / mean(peak) if mean(peak) else None,
            "integrated_target_mean": mean(integrated),
            "integrated_target_sd": stdev(integrated),
            "integrated_target_cv": stdev(integrated) / mean(integrated) if mean(integrated) else None,
            "peak_winner_rate": mean([r["peak_winner_roi"] == target for r in group]),
            "integrated_winner_rate": mean([r["integrated_winner_roi"] == target for r in group]),
            "mean_contact_winner_rate": mean([r["mean_contact_winner_roi"] == target for r in group]),
            "localization_accuracy": weighted_localization(group),
            "force_N_max_median": percentile([r["force_N_max"] for r in group], 0.5),
            "baseline_drift_mean_v_mean": mean([r["baseline_drift_mean_v"] for r in group]),
        })

    by_batch = []
    for batch in sorted({r["batch"] for r in rows}):
        group = [r for r in rows if r["batch"] == batch]
        by_batch.append({
            "batch": batch,
            "sessions": len(group),
            "first_session": min(r["timestamp_local_name"] for r in group),
            "last_session": max(r["timestamp_local_name"] for r in group),
            "peak_winner_rate": mean([r["peak_winner_roi"] == r["target_roi"] for r in group]),
            "integrated_winner_rate": mean([r["integrated_winner_roi"] == r["target_roi"] for r in group]),
            "localization_accuracy": weighted_localization(group),
            "force_N_max_median": percentile([r["force_N_max"] for r in group], 0.5),
            "baseline_drift_mean_v_mean": mean([r["baseline_drift_mean_v"] for r in group]),
            "roi_only_sessions": sum(bool(r["roi_only_magnification"]) for r in group),
            "unverified_at_start": sum(not bool(r["calibration_verified_at_start"]) for r in group),
        })
    return summary, by_target, by_batch


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = build_session_rows()
    summary, by_target, by_batch = aggregate(rows)
    write_csv(OUTPUT_DIR / "session_summary.csv", rows)
    write_csv(OUTPUT_DIR / "summary_by_target_roi.csv", by_target)
    write_csv(OUTPUT_DIR / "summary_by_batch.csv", by_batch)
    (OUTPUT_DIR / "analysis_summary.json").write_text(
        json.dumps({"overall": summary, "by_target": by_target, "by_batch": by_batch}, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
