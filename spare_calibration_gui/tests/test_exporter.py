"""Focused tests for incremental safety and validation-first finalization."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pytest

from core.models import ErrorCode, LoadCellSample
from data.exporter import (
    CsvValidationError,
    ExistingExportError,
    IncrementalCsvWriter,
    atomic_write_json,
    build_feature_failure_row,
    complete_schema_row,
    finalize_master_from_partials,
    inspect_partial_session,
    loadcell_sample_to_row,
    strict_json_dumps,
    validate_csv_file,
    validate_frame_master_identity,
    write_data_dictionary,
)
from data.schemas import (
    DATA_DICTIONARY_ARTIFACT_SCHEMAS,
    DATA_DICTIONARY_COLUMNS,
    FRAME_FEATURE_COLUMNS,
    LOADCELL_RAW_COLUMNS,
    MASTER_COLUMNS,
)


def _frame_values(frame_id: int, timestamp_ns: int) -> dict[str, object]:
    values: dict[str, object] = {
        "session_id": "session-1",
        "trial_id": "trial-1",
        "sensing_skin_id": "skin-1",
        "specimen_or_participant_id": "",
        "target_roi_ground_truth": 5,
        "trial_interaction_class": "Press",
        "trial_force_class": "",
        "press_number": 1,
        "notes": "one intentional press",
        "trial_label_scope": "trial",
        "contact_threshold_N": 0.05,
        "capture_frame_id": frame_id,
        "host_monotonic_ns": timestamp_ns,
        "elapsed_time_s": (timestamp_ns - 1_000_000_000) / 1e9,
        "wall_clock_iso": f"2026-08-03T00:00:0{frame_id}+00:00",
        "requested_fps": 30.0,
        "actual_fps": 29.9,
        "width": 90,
        "height": 90,
        "camera_backend": "Video File (Simulation)",
        "camera_device_index": 0,
        "applied_exposure": math.nan,
        "applied_gain": math.nan,
        "applied_white_balance": math.nan,
        "predicted_dominant_roi": 5,
        "dominant_intensity": 15.0,
        "second_highest_intensity": 8.0,
        "top_one_to_top_two_ratio": 1.875,
        "dominant_to_total_ratio": 0.25,
        "weighted_full_frame_x": 45.0,
        "weighted_full_frame_y": 45.0,
        "total_corrected_intensity": 60.0,
        "global_active_pixel_count": 81,
        "localization_confidence": 0.25,
        "predicted_roi_matches_target": True,
        "frame_valid": True,
        "baseline_valid": True,
        "saturation_warning": False,
        "error_code": ErrorCode.NONE,
        "schema_version": "1.0.0",
        "application_version": "1.0.0",
        "arduino_protocol_version": "SIM-1",
        "arduino_firmware_version": "SIM-1",
        "python_version": "3.13",
        "opencv_version": "4.12",
        "operating_system": "test",
        "calibration_id": "cal-1",
        "baseline_id": "base-1",
        "roi_layout_id": "layout-1",
    }
    for roi_id in range(1, 10):
        x = ((roi_id - 1) % 3) * 30
        y = ((roi_id - 1) // 3) * 30
        prefix = f"roi{roi_id}_"
        values.update(
            {
                f"{prefix}x": x,
                f"{prefix}y": y,
                f"{prefix}width": 20,
                f"{prefix}height": 20,
                f"{prefix}area": 400,
                f"{prefix}center_x": x + 10.0,
                f"{prefix}center_y": y + 10.0,
                f"{prefix}mean_h": 20.0,
                f"{prefix}mean_s": 30.0,
                f"{prefix}mean_v": 40.0,
                f"{prefix}median_v": 39.0,
                f"{prefix}max_v": 45.0,
                f"{prefix}p95_v": 44.0,
                f"{prefix}v_std": 2.0,
                f"{prefix}baseline_mean_v": 35.0,
                f"{prefix}delta_v_mean": float(roi_id),
                f"{prefix}delta_v_max": float(roi_id + 2),
                f"{prefix}delta_v_p95": float(roi_id + 1),
                f"{prefix}delta_v_sum": float(roi_id * 400),
                f"{prefix}delta_v_sum_per_pixel": float(roi_id),
                f"{prefix}delta_v_std": 1.0,
                f"{prefix}active_pixel_count": 9,
                f"{prefix}active_fraction": 9 / 400,
                f"{prefix}roi_local_centroid_x": 10.0,
                f"{prefix}roi_local_centroid_y": 10.0,
                f"{prefix}full_frame_centroid_x": x + 10.0,
                f"{prefix}full_frame_centroid_y": y + 10.0,
                f"{prefix}normalized_intensity": roi_id / 45.0,
            }
        )
    return complete_schema_row(FRAME_FEATURE_COLUMNS, values)


def _failure_values(frame_id: int, timestamp_ns: int) -> dict[str, object]:
    success = _frame_values(frame_id, timestamp_ns)
    base = {
        name: value
        for name, value in success.items()
        if not name.startswith("roi")
        and name
        not in {
            "predicted_dominant_roi",
            "dominant_intensity",
            "second_highest_intensity",
            "top_one_to_top_two_ratio",
            "dominant_to_total_ratio",
            "weighted_full_frame_x",
            "weighted_full_frame_y",
            "total_corrected_intensity",
            "global_active_pixel_count",
            "localization_confidence",
            "predicted_roi_matches_target",
        }
    }
    return build_feature_failure_row(base)


def _sample_values(sample_id: int, timestamp_ns: int, raw_adc: int) -> dict[str, object]:
    return loadcell_sample_to_row(
        LoadCellSample(
            host_monotonic_ns=timestamp_ns,
            arduino_sample_id=sample_id,
            arduino_micros=sample_id * 1_000,
            arduino_micros_unwrapped=sample_id * 1_000,
            raw_adc=raw_adc,
            force_gf=(raw_adc - 1_000) / 100.0,
            force_N=(raw_adc - 1_000) / 100.0 * 0.00980665,
            device_session_id="device-1",
            elapsed_time_s=(timestamp_ns - 1_000_000_000) / 1e9,
            wall_clock_iso="2026-08-03T00:00:00+00:00",
        )
    )


def _write_raw_partials(session_dir: Path) -> None:
    with IncrementalCsvWriter(
        session_dir / "frame_features.csv.partial",
        FRAME_FEATURE_COLUMNS,
        flush_interval_s=60,
        flush_row_count=2,
    ) as writer:
        writer.write_row(_frame_values(0, 1_000_000_000))
        writer.write_row(_failure_values(1, 1_050_000_000))
        writer.write_row(_frame_values(2, 1_400_000_000))
    with IncrementalCsvWriter(
        session_dir / "loadcell_raw.csv.partial",
        LOADCELL_RAW_COLUMNS,
    ) as writer:
        writer.write_row(_sample_values(10, 1_000_000_000, 1_000))
        writer.write_row(_sample_values(11, 1_100_000_000, 2_000))


def test_strict_portable_json_replaces_nonfinite_and_records_status(
    tmp_path: Path,
) -> None:
    payload = {"finite": 1.25, "missing": math.nan, "nested": [math.inf, -math.inf]}
    text = strict_json_dumps(payload, include_nonfinite_status=True)
    assert "NaN" not in text
    assert "Infinity" not in text
    restored = json.loads(text)
    assert restored["missing"] is None
    assert restored["nested"] == [None, None]
    assert {item["status"] for item in restored["nonfinite_value_status"]} == {
        "nan",
        "positive_infinity",
        "negative_infinity",
    }

    path = atomic_write_json(tmp_path / "metadata.json", payload)
    assert json.loads(path.read_text(encoding="utf-8"))["missing"] is None
    with pytest.raises(ExistingExportError):
        atomic_write_json(path, payload)


def test_incremental_writer_flushes_by_row_count_and_never_overwrites(
    tmp_path: Path,
) -> None:
    path = tmp_path / "frame_features.csv.partial"
    writer = IncrementalCsvWriter(
        path,
        FRAME_FEATURE_COLUMNS,
        flush_interval_s=60,
        flush_row_count=2,
    )
    writer.write_row(_frame_values(0, 1_000_000_000))
    writer.write_row(_frame_values(1, 1_010_000_000))
    # Row-count policy made the two rows visible without closing the writer.
    assert validate_csv_file(path, FRAME_FEATURE_COLUMNS).row_count == 2
    with pytest.raises(ExistingExportError):
        IncrementalCsvWriter(path, FRAME_FEATURE_COLUMNS)
    with pytest.raises(CsvValidationError, match="expected 2"):
        writer.write_row(_frame_values(3, 1_030_000_000))
    writer.close()
    writer.close()


def test_feature_failure_helper_preserves_identity_and_uses_nan_features() -> None:
    row = _failure_values(7, 1_070_000_000)
    assert tuple(row) == FRAME_FEATURE_COLUMNS
    assert row["capture_frame_id"] == 7
    assert row["frame_valid"] is True
    assert row["error_code"] == ErrorCode.FEATURE_EXTRACTION_FAILED.value
    assert math.isnan(float(row["roi1_mean_v"]))
    assert math.isnan(float(row["roi9_delta_v_sum"]))
    assert math.isnan(float(row["localization_confidence"]))


def test_master_finalization_reads_partials_preserves_every_frame_and_nan(
    tmp_path: Path,
) -> None:
    _write_raw_partials(tmp_path)
    summary = finalize_master_from_partials(
        session_dir=tmp_path,
        calibration={
            "calibration_id": "cal-1",
            "counts_per_gram": 100.0,
            "tare_raw": 1_000.0,
        },
        max_sync_gap_ms=200,
        contact_threshold_N=0.05,
    )

    assert summary.frame_count == summary.master_row_count == 3
    assert summary.loadcell_sample_count == 2
    assert summary.missing_force_count == 1
    assert summary.one_row_per_frame
    assert not tuple(tmp_path.glob("*.csv.partial"))
    assert all(path.is_file() for path in summary.final_paths)

    frame_summary, master_summary = validate_frame_master_identity(
        tmp_path / "frame_features.csv", tmp_path / "master_synchronized.csv",
        video_frame_count=3,
    )
    assert frame_summary.capture_frame_ids == master_summary.capture_frame_ids == (0, 1, 2)
    with (tmp_path / "master_synchronized.csv").open(
        "r", encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert rows[1]["synchronization_method"] == "linear_interpolation"
    assert float(rows[1]["force_gf"]) == pytest.approx(5.0)
    assert rows[1]["error_code"] == ErrorCode.FEATURE_EXTRACTION_FAILED.value
    assert math.isnan(float(rows[1]["roi1_mean_v"]))
    assert rows[2]["synchronization_method"] == "invalid"
    assert rows[2]["synchronization_valid"] == "False"
    assert math.isnan(float(rows[2]["force_N"]))
    assert math.isnan(float(rows[2]["contact_state_derived"]))


def test_partial_recovery_inspection_is_read_only_and_detects_truncation(
    tmp_path: Path,
) -> None:
    _write_raw_partials(tmp_path)
    before = (tmp_path / "frame_features.csv.partial").read_bytes()
    inspection = inspect_partial_session(tmp_path)
    assert inspection.recoverable
    assert inspection.frame_features.row_count == 3
    assert inspection.loadcell_raw.row_count == 2
    assert (tmp_path / "frame_features.csv.partial").read_bytes() == before

    path = tmp_path / "frame_features.csv.partial"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], "only,three,cells"]), encoding="utf-8")
    broken = inspect_partial_session(tmp_path)
    assert not broken.recoverable
    assert "truncated" in broken.frame_features.error


def test_data_dictionary_has_complete_unique_export_coverage(tmp_path: Path) -> None:
    destination = write_data_dictionary(tmp_path / "data_dictionary.csv")
    with destination.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert tuple(reader.fieldnames or ()) == DATA_DICTIONARY_COLUMNS
    keys = tuple((row["artifact_scope"], row["column_name"]) for row in rows)
    expected = tuple(
        (scope, definition.column_name)
        for scope, schema in DATA_DICTIONARY_ARTIFACT_SCHEMAS
        for definition in schema
    )
    assert keys == expected
    assert len(keys) == len(set(keys))
    assert all(
        row["description"] and row["data_type"] and row["missing_value_behavior"]
        for row in rows
    )
    with pytest.raises(ExistingExportError):
        write_data_dictionary(destination)


def test_reordered_header_and_existing_final_block_mutation(tmp_path: Path) -> None:
    _write_raw_partials(tmp_path)
    load_path = tmp_path / "loadcell_raw.csv.partial"
    lines = load_path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    header[0], header[1] = header[1], header[0]
    load_path.write_text(",".join(header) + "\n" + "\n".join(lines[1:]) + "\n", encoding="utf-8")
    with pytest.raises(CsvValidationError, match="ordering_only=True"):
        validate_csv_file(load_path, LOADCELL_RAW_COLUMNS)

    # Recreate a valid load partial, then prove collision checks happen before
    # any raw partial is renamed.
    load_path.unlink()
    with IncrementalCsvWriter(load_path, LOADCELL_RAW_COLUMNS) as writer:
        writer.write_row(_sample_values(10, 1_000_000_000, 1_000))
    (tmp_path / "master_synchronized.csv").write_text("existing", encoding="utf-8")
    frame_before = (tmp_path / "frame_features.csv.partial").read_bytes()
    with pytest.raises(ExistingExportError):
        finalize_master_from_partials(
            tmp_path,
            {"calibration_id": "cal-1", "counts_per_gram": 100, "tare_raw": 1_000},
        )
    assert (tmp_path / "frame_features.csv.partial").read_bytes() == frame_before
    assert (tmp_path / "loadcell_raw.csv.partial").exists()


def test_loadcell_sample_row_schema_and_enum_serialization(tmp_path: Path) -> None:
    row = _sample_values(1, 1_000_000_000, -123)
    assert tuple(row) == LOADCELL_RAW_COLUMNS
    assert row["raw_adc"] == -123
    assert row["error_code"] is ErrorCode.NONE
    with IncrementalCsvWriter(
        tmp_path / "loadcell_raw.csv.partial", LOADCELL_RAW_COLUMNS
    ) as writer:
        writer.write_row(row)
    validate_csv_file(tmp_path / "loadcell_raw.csv.partial", LOADCELL_RAW_COLUMNS)


def test_master_schema_is_exact_after_finalization(tmp_path: Path) -> None:
    _write_raw_partials(tmp_path)
    finalize_master_from_partials(
        tmp_path,
        {"calibration_id": "cal-1", "counts_per_gram": 100, "tare_raw": 1_000},
    )
    with (tmp_path / "master_synchronized.csv").open(
        "r", encoding="utf-8", newline=""
    ) as handle:
        assert tuple(next(csv.reader(handle))) == MASTER_COLUMNS
