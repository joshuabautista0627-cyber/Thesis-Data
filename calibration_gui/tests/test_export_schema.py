"""Gate 1 audit tests for master, raw, and frame-feature schemas."""

from __future__ import annotations

import math
import pytest

from data.schemas import (
    ALL_EXPORT_COLUMNS,
    DATA_DICTIONARY_COLUMNS,
    DATA_DICTIONARY_ARTIFACT_SCHEMAS,
    DATA_DICTIONARY_ROWS,
    FRAME_FEATURE_COLUMNS,
    GRAPH_SPATIAL_COLUMNS,
    GRAPH_TEMPORAL_COLUMNS,
    LOADCELL_RAW_COLUMNS,
    LOADCELL_SYNC_SCHEMA,
    MASTER_COLUMNS,
    MASTER_SCHEMA,
    data_dictionary_rows,
    schema_for,
    validate_exact_columns,
)
from core.models import ROI, SynchronizationMethod
from processing.feature_extraction import OpticalFrameResult, ROIFeatures
from processing.localization import calculate_localization
from processing.synchronization import synchronize_frame_to_force


def test_master_schema_has_unique_predictable_columns() -> None:
    assert len(MASTER_COLUMNS) == len(set(MASTER_COLUMNS))
    assert MASTER_COLUMNS[:4] == (
        "session_id",
        "trial_id",
        "sensing_skin_id",
        "specimen_or_participant_id",
    )
    required = {
        "capture_frame_id",
        "host_monotonic_ns",
        "requested_width",
        "requested_height",
        "force_N",
        "contact_state_derived",
        "predicted_dominant_roi",
        "predicted_roi_matches_target",
        "frame_valid",
        "baseline_valid",
        "loadcell_valid",
        "error_code",
        "calibration_id",
        "baseline_id",
        "roi_layout_id",
    }
    assert required <= set(MASTER_COLUMNS)


@pytest.mark.parametrize("roi_id", range(1, 10))
def test_each_roi_has_every_required_feature_group(roi_id: int) -> None:
    prefix = f"roi{roi_id}_"
    required_suffixes = {
        "x",
        "y",
        "width",
        "height",
        "area",
        "center_x",
        "center_y",
        "mean_h",
        "mean_s",
        "mean_v",
        "median_v",
        "max_v",
        "p95_v",
            "v_std",
            "baseline_mean_v",
            "signed_delta_v_sum",
            "signed_delta_v_mean",
            "signed_delta_v_median",
            "signed_delta_v_mad",
            "delta_v_mean",
        "delta_v_max",
        "delta_v_p95",
        "delta_v_sum",
        "delta_v_sum_per_pixel",
        "delta_v_std",
        "active_pixel_count",
        "active_fraction",
        "roi_local_centroid_x",
        "roi_local_centroid_y",
        "full_frame_centroid_x",
        "full_frame_centroid_y",
        "normalized_intensity",
    }
    actual_suffixes = {
        column[len(prefix) :] for column in MASTER_COLUMNS if column.startswith(prefix)
    }
    assert actual_suffixes == required_suffixes


def test_roi_columns_preserve_row_major_roi_one_through_nine_order() -> None:
    starts = [MASTER_COLUMNS.index(f"roi{roi_id}_x") for roi_id in range(1, 10)]
    assert starts == sorted(starts)
    for earlier, later in zip(starts[:-1], starts[1:], strict=True):
        assert later - earlier == 32


def test_data_dictionary_covers_every_column_once_per_artifact_scope() -> None:
    rows = data_dictionary_rows()
    expected_pairs = tuple(
        (scope, definition.column_name)
        for scope, schema in DATA_DICTIONARY_ARTIFACT_SCHEMAS
        for definition in schema
    )
    actual_pairs = tuple(
        (row["artifact_scope"], row["column_name"]) for row in rows
    )
    assert actual_pairs == expected_pairs
    assert len(actual_pairs) == len(set(actual_pairs))
    assert len(DATA_DICTIONARY_ROWS) == sum(
        len(schema) for _scope, schema in DATA_DICTIONARY_ARTIFACT_SCHEMAS
    )
    dictionary_names = {row["column_name"] for row in rows}
    assert set(MASTER_COLUMNS) <= dictionary_names
    assert set(FRAME_FEATURE_COLUMNS) <= dictionary_names
    assert set(LOADCELL_RAW_COLUMNS) <= dictionary_names
    assert set(GRAPH_SPATIAL_COLUMNS) <= dictionary_names
    assert set(GRAPH_TEMPORAL_COLUMNS) <= dictionary_names
    assert all(tuple(row) == DATA_DICTIONARY_COLUMNS for row in rows)
    assert all(row["description"].strip() for row in rows)
    assert all(row["data_type"].strip() for row in rows)
    assert all(row["missing_value_behavior"].strip() for row in rows)
    assert {row["value_type"] for row in rows} == {
        "raw",
        "calculated",
        "predicted",
        "ground truth",
        "metadata",
    }


def test_shared_column_names_retain_artifact_specific_scientific_meaning() -> None:
    rows = data_dictionary_rows()
    by_key = {
        (row["artifact_scope"], row["column_name"]): row for row in rows
    }
    assert "frame-capture" in by_key[
        ("frame_features.csv", "host_monotonic_ns")
    ]["description"]
    assert "host receipt" in by_key[
        ("loadcell_raw.csv", "host_monotonic_ns")
    ]["description"]
    assert "frame timestamp" in by_key[
        ("master_synchronized.csv", "force_N")
    ]["description"]
    assert "standard gravity" in by_key[("loadcell_raw.csv", "force_N")][
        "description"
    ]


def test_scientific_missing_values_are_explicit_not_misleading_zeroes() -> None:
    by_name = {item.column_name: item for item in MASTER_SCHEMA}
    assert "NaN" in by_name["force_N"].missing_value_behavior
    assert "NaN" in by_name["contact_state_derived"].missing_value_behavior
    assert "Blank" in by_name["predicted_dominant_roi"].missing_value_behavior
    assert "NaN" in by_name["roi1_roi_local_centroid_x"].missing_value_behavior
    assert "NaN" in by_name["roi9_mean_h"].missing_value_behavior


def test_raw_loadcell_schema_preserves_unsynchronized_physical_samples() -> None:
    assert LOADCELL_RAW_COLUMNS[:19] == (
        "arduino_sample_id",
        "arduino_micros",
        "arduino_micros_unwrapped",
        "raw_adc",
        "tared_raw",
        "mass_g",
        "host_monotonic_ns",
        "elapsed_time_s",
        "wall_clock_iso",
        "force_gf",
        "force_N",
        "calibration_factor_counts_per_gram",
        "zero_offset_raw",
        "serial_port",
        "baud_rate",
        "arduino_timestamp_available",
        "device_session_id",
        "loadcell_valid",
        "error_code",
    )
    assert {
        "sequence_id",
        "motion_phase",
        "motion_cycle_index",
        "command_id",
        "press_zero_z_mm",
        "target_machine_z_mm",
        "commanded_z_mm",
        "printer_reported_z_mm",
        "force_limit_exceeded",
        "sequence_paused",
        "sequence_aborted",
    } <= set(LOADCELL_RAW_COLUMNS[19:])
    assert "capture_frame_id" not in LOADCELL_RAW_COLUMNS


def test_frame_features_precedes_force_synchronization() -> None:
    assert "capture_frame_id" in FRAME_FEATURE_COLUMNS
    assert "roi9_delta_v_sum" in FRAME_FEATURE_COLUMNS
    assert "force_N" not in FRAME_FEATURE_COLUMNS
    assert "synchronization_method" not in FRAME_FEATURE_COLUMNS
    assert "loadcell_valid" not in FRAME_FEATURE_COLUMNS


def test_schema_resolver_accepts_final_and_partial_filenames() -> None:
    assert schema_for("master_synchronized.csv") is schema_for("master")
    assert schema_for("frame_features.csv.partial")[0].column_name == "session_id"
    assert schema_for("loadcell_raw.csv.partial")[0].column_name == "arduino_sample_id"
    assert tuple(item.column_name for item in schema_for("graph_spatial")) == (
        "roi",
        "row",
        "column",
        "value",
    )
    assert tuple(item.column_name for item in schema_for("graph_temporal")) == (
        "capture_frame_id",
        "elapsed_time_s",
        *(f"roi{roi_id}_value" for roi_id in range(1, 10)),
    )
    with pytest.raises(KeyError, match="unknown exported dataset"):
        schema_for("unrelated.csv")


def test_graph_dictionary_columns_explain_metric_dependent_values() -> None:
    definitions = {
        row["column_name"]: row
        for row in data_dictionary_rows()
        if row["artifact_scope"] in {
            "spatial_graph_companion.csv",
            "temporal_graph_companion.csv",
        }
    }
    for column in ("value", *(f"roi{roi_id}_value" for roi_id in range(1, 10))):
        assert "companion graph JSON" in definitions[column]["description"]
        assert "Metric-dependent" in definitions[column]["unit"]
        assert definitions[column]["data_type"] == "float64"
        assert "Blank" in definitions[column]["missing_value_behavior"]


def test_exact_column_validator_rejects_missing_extra_and_reordered_columns() -> None:
    validate_exact_columns(MASTER_COLUMNS)
    with pytest.raises(ValueError, match="missing="):
        validate_exact_columns(MASTER_COLUMNS[:-1])
    with pytest.raises(ValueError, match="unexpected="):
        validate_exact_columns((*MASTER_COLUMNS, "unexpected"))
    with pytest.raises(ValueError, match="ordering_only=True"):
        validate_exact_columns((MASTER_COLUMNS[1], MASTER_COLUMNS[0], *MASTER_COLUMNS[2:]))


def _feature(roi: ROI, intensity: float) -> ROIFeatures:
    return ROIFeatures(
        roi_id=roi.roi_id,
        x=roi.x,
        y=roi.y,
        width=roi.width,
        height=roi.height,
        area=roi.area,
        center_x=roi.center_x,
        center_y=roi.center_y,
        mean_h=20.0,
        mean_s=30.0,
        mean_v=40.0,
        median_v=39.0,
        max_v=45.0,
        p95_v=44.0,
        v_std=2.0,
        baseline_mean_v=35.0,
        signed_delta_v_sum=intensity * roi.area,
        signed_delta_v_mean=intensity,
        signed_delta_v_median=intensity,
        signed_delta_v_mad=0.0,
        delta_v_mean=intensity,
        delta_v_max=intensity + 2.0,
        delta_v_p95=intensity + 1.0,
        delta_v_sum=intensity * roi.area,
        delta_v_sum_per_pixel=intensity,
        delta_v_std=1.0,
        active_pixel_count=roi.area,
        active_fraction=1.0,
        roi_local_centroid_x=roi.width / 2.0,
        roi_local_centroid_y=roi.height / 2.0,
        full_frame_centroid_x=roi.center_x,
        full_frame_centroid_y=roi.center_y,
        normalized_intensity=intensity / 45.0,
    )


def test_optical_runtime_flattened_names_match_frame_schema() -> None:
    rois = tuple(
        ROI(
            roi_id=roi_id,
            x=((roi_id - 1) % 3) * 10,
            y=((roi_id - 1) // 3) * 10,
            width=8,
            height=8,
        )
        for roi_id in range(1, 10)
    )
    features = tuple(_feature(roi, float(roi.roi_id)) for roi in rois)
    localization = calculate_localization(
        [feature.delta_v_mean for feature in features],
        rois,
        global_active_pixel_count=sum(item.active_pixel_count for item in features),
        target_roi_ground_truth=9,
    )
    flattened = OpticalFrameResult(
        roi_features=features,
        localization=localization,
        frame_valid=True,
        baseline_valid=True,
        saturation_warning=False,
    ).to_export_dict()

    assert set(flattened) <= set(FRAME_FEATURE_COLUMNS)
    runtime_roi_columns = {
        name
        for name in flattened
        if any(name.startswith(f"roi{roi_id}_") for roi_id in range(1, 10))
    }
    schema_roi_columns = {
        name
        for name in FRAME_FEATURE_COLUMNS
        if any(name.startswith(f"roi{roi_id}_") for roi_id in range(1, 10))
    }
    assert runtime_roi_columns == schema_roi_columns
    assert "predicted_roi_matches_target" in flattened


def _sample(timestamp_ns: int, sample_id: int, raw_adc: int) -> dict[str, object]:
    return {
        "host_monotonic_ns": timestamp_ns,
        "arduino_sample_id": sample_id,
        "arduino_micros": sample_id * 1000,
        "raw_adc": raw_adc,
        "force_gf": raw_adc / 100.0,
        "force_N": raw_adc / 100.0 * 0.00980665,
        "device_session_id": "device-1",
        "loadcell_valid": True,
    }


def test_sync_runtime_methods_and_flattened_names_match_schema() -> None:
    samples = (
        _sample(1_000_000_000, 1, 100),
        _sample(1_100_000_000, 2, 200),
    )
    nearest = synchronize_frame_to_force(1_000_000_000, samples)
    interpolated = synchronize_frame_to_force(1_050_000_000, samples)
    invalid = synchronize_frame_to_force(
        2_000_000_000, samples, max_sync_gap_ms=100.0
    )
    assert nearest.synchronization_method == "nearest"
    assert interpolated.synchronization_method == "linear_interpolation"
    assert invalid.synchronization_method == "invalid"
    assert {
        nearest.synchronization_method,
        interpolated.synchronization_method,
        invalid.synchronization_method,
    } <= {method.value for method in SynchronizationMethod}

    result = invalid
    flattened = {
        "closest_arduino_sample_id": result.closest_arduino_sample_id,
        "arduino_micros": result.closest_arduino_micros,
        "closest_raw_adc": result.closest_raw_adc,
        "interpolated_raw_adc": result.interpolated_raw_adc,
        "tared_raw": math.nan,
        "mass_g": result.force_gf,
        "force_gf": result.force_gf,
        "force_N": result.force_N,
        "synchronization_method": result.synchronization_method,
        "nearest_sample_gap_ms": result.nearest_sample_gap_ms,
        "synchronization_valid": result.synchronization_valid,
        "counts_per_gram": 100.0,
        "tare_raw": 0.0,
        "serial_port": "COM1",
        "baud_rate": 115200,
        "synchronization_offset_ms": result.synchronization_offset_ms,
        "contact_state_derived": result.contact_state_derived,
        "device_session_id": result.closest_device_session_id,
    }
    assert tuple(flattened) == tuple(
        definition.column_name for definition in LOADCELL_SYNC_SCHEMA
    )
    assert result.closest_arduino_sample_id == 2
    assert result.closest_raw_adc == 200
    assert result.closest_device_session_id == "device-1"

    definitions = {item.column_name: item for item in LOADCELL_SYNC_SCHEMA}
    assert "retained" in definitions["closest_raw_adc"].missing_value_behavior
    assert "retained" in definitions["device_session_id"].missing_value_behavior
