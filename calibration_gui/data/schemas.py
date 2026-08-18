"""Canonical CSV schemas and data-dictionary metadata.

Column order is built once at import time and is shared by incremental writers,
finalization, validation, and documentation.  Adding a master column therefore
requires adding its description and missing-value rule in the same definition.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

from core.models import ROI_COUNT, SCHEMA_VERSION


DATA_DICTIONARY_COLUMNS = (
    "artifact_scope",
    "column_name",
    "description",
    "unit",
    "data_type",
    "value_type",
    "missing_value_behavior",
)


@dataclass(frozen=True, slots=True)
class ColumnDefinition:
    """One exported column and all required data-dictionary attributes."""

    column_name: str
    description: str
    unit: str
    data_type: str
    value_type: str
    missing_value_behavior: str

    def __post_init__(self) -> None:
        if not self.column_name or not self.description:
            raise ValueError("schema columns require a name and description")
        if self.value_type not in {
            "raw",
            "calculated",
            "predicted",
            "ground truth",
            "metadata",
        }:
            raise ValueError(f"unsupported value_type: {self.value_type}")
        if not self.data_type or not self.missing_value_behavior:
            raise ValueError("data type and missing-value behavior are required")

    def as_dictionary_row(self, artifact_scope: str) -> dict[str, str]:
        """Return this definition with its exported-artifact scope.

        Several required files intentionally reuse predictable names such as
        ``host_monotonic_ns`` and ``force_N``.  Their scientific meaning is
        file-specific (frame capture versus load-cell receipt, for example),
        so scope is part of the dictionary key rather than silently applying
        one first-seen definition to every artifact.
        """

        if not artifact_scope.strip():
            raise ValueError("data-dictionary artifact scope must not be blank")
        return {"artifact_scope": artifact_scope, **asdict(self)}


def _col(
    name: str,
    description: str,
    unit: str,
    data_type: str,
    value_type: str,
    missing: str = "Never missing for a valid row.",
) -> ColumnDefinition:
    return ColumnDefinition(name, description, unit, data_type, value_type, missing)


SESSION_SCHEMA = (
    _col("session_id", "Unique acquisition session identifier.", "", "string", "metadata"),
    _col("trial_id", "Operator-assigned single-press trial identifier.", "", "string", "ground truth"),
    _col("sensing_skin_id", "Identifier of the sensing skin under test.", "", "string", "ground truth"),
    _col(
        "specimen_or_participant_id",
        "Optional specimen or participant identifier.",
        "",
        "string",
        "ground truth",
        "Blank when the operator does not provide this optional label.",
    ),
    _col("target_roi_ground_truth", "Operator-selected target ROI number (1-9).", "ROI number", "integer", "ground truth"),
    _col(
        "trial_interaction_class",
        "Optional fixed interaction class for the whole trial; not a frame contact label.",
        "",
        "string",
        "ground truth",
        "Blank when the optional trial label is not provided.",
    ),
    _col(
        "trial_force_class",
        "Optional fixed force class for the whole trial; not measured force.",
        "",
        "string",
        "ground truth",
        "Blank when the optional trial label is not provided.",
    ),
    _col(
        "press_number",
        "Optional operator press number within an external experimental plan.",
        "count",
        "nullable integer",
        "ground truth",
        "Blank when the optional press number is not provided.",
    ),
    _col("notes", "Optional operator notes fixed for the complete trial.", "", "string", "metadata", "Blank when no notes are provided."),
    _col("trial_label_scope", "Explicit scope of manual labels; always 'trial'.", "", "string", "metadata"),
    _col("contact_threshold_N", "Force threshold used to derive frame-level contact state.", "N", "float64", "metadata"),
)


FRAME_IDENTITY_SCHEMA = (
    _col("capture_frame_id", "Permanent zero-based identity of an accepted recording frame.", "count", "int64", "raw"),
    _col("host_monotonic_ns", "Authoritative host monotonic frame-capture timestamp.", "ns", "int64", "raw"),
    _col("elapsed_time_s", "Frame timestamp relative to the recording monotonic start.", "s", "float64", "calculated"),
    _col("wall_clock_iso", "ISO 8601 wall-clock timestamp for human traceability.", "", "string", "metadata"),
    _col("requested_width", "Requested camera capture width for this session.", "px", "int32", "metadata"),
    _col("requested_height", "Requested camera capture height for this session.", "px", "int32", "metadata"),
    _col("requested_fps", "Requested camera capture rate.", "frames/s", "float64", "metadata"),
    _col("actual_fps", "Measured or camera-reported actual capture rate.", "frames/s", "float64", "raw", "NaN when the backend cannot report or measure a rate."),
    _col("width", "Actual unannotated frame width.", "px", "int32", "raw"),
    _col("height", "Actual unannotated frame height.", "px", "int32", "raw"),
    _col("camera_backend", "Capture backend fixed for the complete session.", "", "string", "metadata"),
    _col("camera_device_index", "OpenCV camera device index, or simulation source index.", "index", "int32", "metadata"),
    _col("applied_exposure", "Exposure value read back immediately after application.", "driver units", "float64", "metadata", "NaN when unsupported or readback is unavailable."),
    _col("applied_gain", "Gain value read back immediately after application.", "driver units", "float64", "metadata", "NaN when unsupported or readback is unavailable."),
    _col("applied_white_balance", "White-balance value read back immediately after application.", "driver units", "float64", "metadata", "NaN when unsupported or readback is unavailable."),
)


MOTION_PROVENANCE_SCHEMA = (
    _col("sequence_id", "Unique repeated-press sequence identifier.", "", "string", "metadata", "Blank outside an automated printer sequence."),
    _col("sequence_generation", "Monotonic controller generation used to reject stale callbacks.", "count", "int64", "metadata"),
    _col("sequence_status", "Overall repeated-press sequence status at acquisition time.", "", "string", "metadata"),
    _col("motion_phase", "Explicit repeated-press state-machine phase at acquisition time.", "", "string", "metadata"),
    _col("motion_phase_host_monotonic_ns", "Host monotonic timestamp when the current motion phase began.", "ns", "int64", "raw"),
    _col("motion_cycle_id", "Unique identifier of the active cycle.", "", "string", "metadata", "Blank outside an active cycle."),
    _col("motion_cycle_index", "One-based active cycle number; zero outside a cycle.", "count", "int64", "metadata"),
    _col("motion_total_cycles", "Configured number of cycles for the sequence.", "count", "int64", "metadata"),
    _col("command_id", "Unique printer command associated with the current phase.", "", "string", "metadata", "Blank when no command is active."),
    _col("press_zero_x_mm", "Mechanical Press Zero machine X coordinate stored in memory; never implemented with G92.", "mm", "float64", "metadata", "NaN when press zero is invalid or unavailable."),
    _col("press_zero_y_mm", "Mechanical Press Zero machine Y coordinate stored in memory; never implemented with G92.", "mm", "float64", "metadata", "NaN when press zero is invalid or unavailable."),
    _col("press_zero_z_mm", "Mechanical Press Zero machine Z coordinate stored in memory; never implemented with G92.", "mm", "float64", "metadata", "NaN when press zero is invalid or unavailable."),
    _col("target_displacement_mm", "Configured signed displacement relative to press zero; negative is downward on the validated machine.", "mm", "float64", "metadata", "NaN outside a configured sequence."),
    _col("target_machine_z_mm", "Computed machine Z target equal to press_zero_z_mm plus displacement.", "mm", "float64", "calculated", "NaN when press zero is invalid."),
    _col("commanded_x_mm", "Most recent commanded absolute X coordinate.", "mm", "float64", "metadata", "NaN when X was not commanded."),
    _col("commanded_y_mm", "Most recent commanded absolute Y coordinate.", "mm", "float64", "metadata", "NaN when Y was not commanded."),
    _col("commanded_z_mm", "Most recent commanded absolute Z coordinate.", "mm", "float64", "metadata", "NaN when Z was not commanded."),
    _col("requested_feed_rate_mm_min", "Feed rate requested for the current motion command.", "mm/min", "float64", "metadata", "NaN when no motion is active."),
    _col("printer_reported_x_mm", "Latest X coordinate parsed from a printer M114 response.", "mm", "float64", "raw", "NaN until an actual printer position report is received."),
    _col("printer_reported_y_mm", "Latest Y coordinate parsed from a printer M114 response.", "mm", "float64", "raw", "NaN until an actual printer position report is received."),
    _col("printer_reported_z_mm", "Latest Z coordinate parsed from a printer M114 response.", "mm", "float64", "raw", "NaN until an actual printer position report is received."),
    _col("printer_position_valid", "Whether the reported printer coordinates are current and trusted.", "boolean", "boolean", "calculated"),
    _col("press_zero_valid", "Whether the in-memory software press zero remains valid.", "boolean", "boolean", "calculated"),
    _col("force_limit_N", "Configured calibrated-force safety threshold.", "N", "float64", "metadata", "NaN when no repeated sequence is configured."),
    _col("force_limit_exceeded", "Whether a calibrated sample exceeded the configured force threshold.", "boolean", "boolean", "calculated"),
    _col("sequence_paused", "Whether the sequence was paused at this acquisition time.", "boolean", "boolean", "metadata"),
    _col("sequence_aborted", "Whether the sequence had entered an aborted terminal path.", "boolean", "boolean", "metadata"),
)


LOADCELL_SYNC_SCHEMA = (
    _col("closest_arduino_sample_id", "Physical sample ID nearest to the frame timestamp.", "count", "nullable int64", "raw", "NaN only when no physical sample exists; retained when a known sample exceeds the synchronization limit."),
    _col("arduino_micros", "Raw 32-bit Arduino micros value of the closest sample.", "us", "nullable int64", "raw", "NaN only when no physical sample exists; retained when a known sample exceeds the synchronization limit."),
    _col("closest_raw_adc", "Signed HX711 reading from the closest physical sample.", "ADC count", "float64", "raw", "NaN only when no physical sample exists; retained when a known sample exceeds the synchronization limit."),
    _col("interpolated_raw_adc", "Raw ADC estimate at the frame timestamp.", "ADC count", "float64", "calculated", "NaN when synchronization is invalid."),
    _col("tared_raw", "Synchronized raw reading minus the saved zero offset.", "ADC count", "float64", "calculated", "NaN when synchronization or calibration is invalid."),
    _col("mass_g", "Signed synchronized mass calculated from the tared raw reading.", "g", "float64", "calculated", "NaN when synchronization or calibration is invalid."),
    _col("force_gf", "Signed synchronized force at the frame timestamp.", "gf", "float64", "calculated", "NaN when synchronization or calibration is invalid."),
    _col("force_N", "Signed synchronized force at the frame timestamp.", "N", "float64", "calculated", "NaN when synchronization or calibration is invalid."),
    _col("synchronization_method", "linear_interpolation, nearest, or invalid.", "", "string", "calculated"),
    _col("nearest_sample_gap_ms", "Absolute gap to the closest physical sample.", "ms", "float64", "calculated", "NaN when no physical samples exist."),
    _col("synchronization_valid", "Whether synchronized force satisfies the maximum-gap rule.", "boolean", "boolean", "calculated"),
    _col("counts_per_gram", "Signed calibration scale used for force conversion.", "ADC count/gf", "float64", "metadata", "NaN when no valid calibration exists."),
    _col("tare_raw", "Raw ADC zero used for force conversion.", "ADC count", "float64", "metadata", "NaN when no valid calibration exists."),
    _col("serial_port", "Serial port associated with the applied calibration and sample stream.", "", "string", "metadata", "Blank only when unavailable."),
    _col("baud_rate", "Serial baud rate associated with the applied calibration and sample stream.", "bit/s", "int64", "metadata", "NaN only when unavailable."),
    _col("synchronization_offset_ms", "Signed frame timestamp minus nearest physical sample host timestamp.", "ms", "float64", "calculated", "NaN when no physical sample exists."),
    _col("contact_state_derived", "Frame contact state calculated from synchronized force_N.", "boolean", "nullable boolean", "calculated", "NaN when synchronized force is unavailable; never inferred from trial labels."),
    _col("device_session_id", "Host identifier for the closest continuous Arduino firmware session.", "", "string", "metadata", "Blank only when no physical sample exists; retained when a known sample exceeds the synchronization limit."),
)


_ROI_FIELD_TEMPLATES = (
    ("x", "Full-frame upper-left x coordinate", "px", "int32", "metadata", "Never missing when the ROI layout is valid."),
    ("y", "Full-frame upper-left y coordinate", "px", "int32", "metadata", "Never missing when the ROI layout is valid."),
    ("width", "Rectangle width", "px", "int32", "metadata", "Never missing when the ROI layout is valid."),
    ("height", "Rectangle height", "px", "int32", "metadata", "Never missing when the ROI layout is valid."),
    ("area", "Rectangle pixel area", "px^2", "int32", "calculated", "Never missing when the ROI layout is valid."),
    ("center_x", "Rectangle center x coordinate in the full frame", "px", "float64", "calculated", "Never missing when the ROI layout is valid."),
    ("center_y", "Rectangle center y coordinate in the full frame", "px", "float64", "calculated", "Never missing when the ROI layout is valid."),
    ("mean_h", "Circular mean OpenCV hue over pixels meeting the saturation threshold", "OpenCV H (0-179)", "float64", "calculated", "NaN when no pixel meets the hue saturation threshold or feature extraction fails."),
    ("mean_s", "Mean OpenCV saturation", "OpenCV S (0-255)", "float64", "calculated", "NaN when feature extraction fails."),
    ("mean_v", "Mean raw OpenCV value channel", "OpenCV V (0-255)", "float64", "calculated", "NaN when feature extraction fails."),
    ("median_v", "Median raw OpenCV value channel", "OpenCV V (0-255)", "float64", "calculated", "NaN when feature extraction fails."),
    ("max_v", "Maximum raw OpenCV value channel", "OpenCV V (0-255)", "float64", "calculated", "NaN when feature extraction fails."),
    ("p95_v", "95th percentile raw OpenCV value channel", "OpenCV V (0-255)", "float64", "calculated", "NaN when feature extraction fails."),
    ("v_std", "Population standard deviation of raw OpenCV value", "OpenCV V (0-255)", "float64", "calculated", "NaN when feature extraction fails."),
    ("baseline_mean_v", "Saved unloaded baseline mean V for this ROI", "OpenCV V (0-255)", "float64", "metadata", "NaN when the baseline is invalid or unavailable."),
    ("signed_delta_v_sum", "Integrated signed per-pixel current V minus baseline median V", "delta V*px", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("signed_delta_v_mean", "Mean signed per-pixel current V minus baseline median V", "delta V", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("signed_delta_v_median", "Median signed per-pixel current V minus baseline median V", "delta V", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("signed_delta_v_mad", "Median absolute deviation of signed per-pixel current V minus baseline median V", "delta V", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("delta_v_mean", "Mean positive per-pixel baseline-corrected V", "delta V (0-255)", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("delta_v_max", "Maximum positive per-pixel baseline-corrected V", "delta V (0-255)", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("delta_v_p95", "95th percentile positive per-pixel baseline-corrected V", "delta V (0-255)", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("delta_v_sum", "Integrated positive per-pixel baseline-corrected V", "delta V*px", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("delta_v_sum_per_pixel", "Integrated positive delta V divided by ROI area", "delta V", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("delta_v_std", "Population standard deviation of positive delta V", "delta V (0-255)", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("active_pixel_count", "Pixels whose positive delta V meets the configured threshold", "count", "int32", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("active_fraction", "Active-pixel count divided by ROI area", "fraction", "float64", "calculated", "NaN when baseline or feature extraction is invalid."),
    ("roi_local_centroid_x", "Delta-V weighted x centroid in ROI-local coordinates", "px", "float64", "calculated", "NaN when integrated positive delta V is zero or features are invalid."),
    ("roi_local_centroid_y", "Delta-V weighted y centroid in ROI-local coordinates", "px", "float64", "calculated", "NaN when integrated positive delta V is zero or features are invalid."),
    ("full_frame_centroid_x", "Delta-V weighted x centroid in full-frame coordinates", "px", "float64", "calculated", "NaN when the ROI-local centroid is unavailable."),
    ("full_frame_centroid_y", "Delta-V weighted y centroid in full-frame coordinates", "px", "float64", "calculated", "NaN when the ROI-local centroid is unavailable."),
    ("normalized_intensity", "ROI mean positive delta V divided by total across all nine ROIs", "fraction", "float64", "calculated", "NaN when total mean delta V is zero or features are invalid."),
)


def _build_roi_schema() -> tuple[ColumnDefinition, ...]:
    definitions: list[ColumnDefinition] = []
    for roi_id in range(1, ROI_COUNT + 1):
        for suffix, description, unit, data_type, value_type, missing in _ROI_FIELD_TEMPLATES:
            definitions.append(
                _col(
                    f"roi{roi_id}_{suffix}",
                    f"ROI {roi_id}: {description}.",
                    unit,
                    data_type,
                    value_type,
                    missing,
                )
            )
    return tuple(definitions)


ROI_FEATURE_SCHEMA = _build_roi_schema()


LOCALIZATION_SCHEMA = (
    _col("predicted_dominant_roi", "ROI number with the largest mean positive delta V above the localization threshold.", "ROI number", "nullable integer", "predicted", "Blank when the dominant intensity is below threshold or features are invalid."),
    _col("dominant_intensity", "Largest ROI mean positive delta V.", "delta V (0-255)", "float64", "calculated", "NaN when features are invalid."),
    _col("second_highest_intensity", "Second-largest ROI mean positive delta V.", "delta V (0-255)", "float64", "calculated", "NaN when features are invalid."),
    _col("top_one_to_top_two_ratio", "Dominant divided by second-highest intensity.", "ratio", "float64", "calculated", "NaN when second-highest intensity is zero or features are invalid."),
    _col("dominant_to_total_ratio", "Dominant intensity divided by total mean positive delta V.", "fraction", "float64", "calculated", "NaN when total intensity is zero or features are invalid."),
    _col("weighted_full_frame_x", "Nine-ROI intensity-weighted full-frame x coordinate.", "px", "float64", "calculated", "NaN when total intensity is zero or features are invalid."),
    _col("weighted_full_frame_y", "Nine-ROI intensity-weighted full-frame y coordinate.", "px", "float64", "calculated", "NaN when total intensity is zero or features are invalid."),
    _col("total_corrected_intensity", "Sum of the nine ROI mean positive delta-V intensities.", "delta V", "float64", "calculated", "NaN when features are invalid."),
    _col("global_active_pixel_count", "Sum of active-pixel counts across the nine ROIs.", "count", "nullable int64", "calculated", "NaN when features are invalid."),
    _col("localization_confidence", "Dominant ROI mean delta V divided by total mean delta V.", "fraction", "float64", "predicted", "NaN when total intensity is zero or features are invalid."),
    _col("predicted_roi_matches_target", "Whether the predicted ROI equals target_roi_ground_truth.", "boolean", "nullable boolean", "calculated", "NaN when no ROI prediction exists."),
)


VALIDITY_PROVENANCE_SCHEMA = (
    _col("frame_valid", "Whether the accepted frame identity and video write are valid.", "boolean", "boolean", "calculated"),
    _col("baseline_valid", "Whether exact-provenance baseline correction was valid for this frame.", "boolean", "boolean", "calculated"),
    _col("loadcell_valid", "Whether calibrated physical load-cell data were available.", "boolean", "boolean", "calculated"),
    _col("saturation_warning", "Whether frame or ROI saturation requires scientific caution.", "boolean", "boolean", "calculated"),
    _col("error_code", "Machine-readable primary row error code; NONE on success.", "", "string", "metadata"),
    _col("schema_version", "Master CSV schema version.", "", "string", "metadata"),
    _col("application_version", "Application version that produced the row.", "", "string", "metadata"),
    _col("arduino_protocol_version", "HELLO-reported Arduino serial protocol version.", "", "string", "metadata", "Blank in simulation or when no valid handshake is available."),
    _col("arduino_firmware_version", "HELLO-reported Arduino firmware version.", "", "string", "metadata", "Blank in simulation or when no valid handshake is available."),
    _col("python_version", "Python runtime version used for acquisition.", "", "string", "metadata"),
    _col("opencv_version", "OpenCV runtime version used for acquisition and video writing.", "", "string", "metadata"),
    _col("operating_system", "Operating-system provenance string.", "", "string", "metadata"),
    _col("calibration_id", "Identifier of the load-cell calibration used.", "", "string", "metadata", "Blank when calibrated force is unavailable."),
    _col("baseline_id", "Identifier of the exact optical baseline used.", "", "string", "metadata", "Blank when baseline correction is unavailable."),
    _col("roi_layout_id", "Identifier of the exact nine-ROI layout used.", "", "string", "metadata"),
)


LOADCELL_RAW_SCHEMA = (
    _col("arduino_sample_id", "Sequential physical HX711 conversion ID emitted by firmware.", "count", "int64", "raw"),
    _col("arduino_micros", "Raw unsigned 32-bit Arduino micros timestamp.", "us", "int64", "raw"),
    _col("arduino_micros_unwrapped", "Host-unwrapped Arduino micros value within the device session.", "us", "int64", "calculated"),
    _col("raw_adc", "Signed raw HX711 sensor reading.", "ADC count", "float64", "raw"),
    _col("tared_raw", "Raw reading minus the saved zero offset.", "ADC count", "float64", "calculated", "NaN before a calibration/tare profile is applied."),
    _col("mass_g", "Signed mass calculated from the tared raw reading.", "g", "float64", "calculated", "NaN before calibration."),
    _col("host_monotonic_ns", "Authoritative host receipt timestamp captured at line receipt.", "ns", "int64", "raw"),
    _col("elapsed_time_s", "Sample host receipt time relative to recording start.", "s", "float64", "calculated"),
    _col("wall_clock_iso", "ISO 8601 host receipt wall-clock timestamp.", "", "string", "metadata"),
    _col("force_gf", "Force calculated from raw ADC, signed scale, and tare.", "gf", "float64", "calculated", "NaN when no valid calibration exists."),
    _col("force_N", "Force calculated from force_gf using standard gravity.", "N", "float64", "calculated", "NaN when no valid calibration exists."),
    _col("calibration_factor_counts_per_gram", "Signed calibration factor applied to this sample.", "ADC count/g", "float64", "metadata", "NaN before calibration."),
    _col("zero_offset_raw", "Raw zero/tare offset applied to this sample.", "ADC count", "float64", "metadata", "NaN before calibration."),
    _col("serial_port", "Serial port from which this sample was received.", "", "string", "metadata", "Blank only when unavailable."),
    _col("baud_rate", "Serial baud rate used to receive this sample.", "bit/s", "int64", "metadata", "Zero only when unavailable."),
    _col("arduino_timestamp_available", "Whether arduino_micros came from the device rather than a host-generated compatibility timestamp.", "boolean", "boolean", "metadata"),
    _col("device_session_id", "Identifier reset on HELLO, sample-ID reset, or micros reset.", "", "string", "metadata"),
    _col("loadcell_valid", "Whether parsing and calibration are valid for this physical sample.", "boolean", "boolean", "calculated"),
    _col("error_code", "Machine-readable sample error code; NONE for a valid sample.", "", "string", "metadata"),
) + MOTION_PROVENANCE_SCHEMA


# Graph companions intentionally use metric-neutral column names so the same
# stable CSV layout can represent each supported optical metric.  The selected
# metric, its scientific description, and its units are authoritative in the
# adjacent graph JSON file; the dictionary makes that dependency explicit.
GRAPH_SPATIAL_SCHEMA = (
    _col(
        "roi",
        "ROI number (1-9) for a spatial heatmap or spatial-profile value.",
        "ROI number",
        "int32",
        "metadata",
    ),
    _col(
        "row",
        "One-based row of the ROI in the fixed 3-by-3 spatial layout.",
        "grid row",
        "int32",
        "metadata",
    ),
    _col(
        "column",
        "One-based column of the ROI in the fixed 3-by-3 spatial layout.",
        "grid column",
        "int32",
        "metadata",
    ),
    _col(
        "value",
        "Plotted spatial ROI value; its metric and scientific meaning are selected_metric and metric_description in the companion graph JSON.",
        "Metric-dependent; see companion graph JSON metric_units.",
        "float64",
        "calculated",
        "Blank when the selected metric is unavailable or non-finite for this ROI.",
    ),
)


GRAPH_TEMPORAL_SCHEMA = (
    # These identities deliberately share the canonical frame definitions.
    FRAME_IDENTITY_SCHEMA[0],
    FRAME_IDENTITY_SCHEMA[2],
    *tuple(
        _col(
            f"roi{roi_id}_value",
            f"ROI {roi_id} plotted temporal value; its metric and scientific meaning are selected_metric and metric_description in the companion graph JSON.",
            "Metric-dependent; see companion graph JSON metric_units.",
            "float64",
            "calculated",
            "Blank when the selected metric is unavailable or non-finite at this frame.",
        )
        for roi_id in range(1, ROI_COUNT + 1)
    ),
)


FRAME_FEATURE_SCHEMA = (
    SESSION_SCHEMA
    + FRAME_IDENTITY_SCHEMA
    + MOTION_PROVENANCE_SCHEMA
    + ROI_FEATURE_SCHEMA
    + LOCALIZATION_SCHEMA
    + tuple(
        definition
        for definition in VALIDITY_PROVENANCE_SCHEMA
        if definition.column_name not in {"loadcell_valid"}
    )
)

MASTER_SCHEMA = (
    SESSION_SCHEMA
    + FRAME_IDENTITY_SCHEMA
    + MOTION_PROVENANCE_SCHEMA
    + LOADCELL_SYNC_SCHEMA
    + ROI_FEATURE_SCHEMA
    + LOCALIZATION_SCHEMA
    + VALIDITY_PROVENANCE_SCHEMA
)


def _ordered_schema_union(
    *schemas: Sequence[ColumnDefinition],
) -> tuple[ColumnDefinition, ...]:
    """Return the legacy unique-name union used by row-completion helpers.

    Scientific documentation must use ``DATA_DICTIONARY_ARTIFACT_SCHEMAS`` so
    same-named fields with different artifact meanings are never collapsed.
    """

    definitions: list[ColumnDefinition] = []
    seen: set[str] = set()
    for schema in schemas:
        for definition in schema:
            if definition.column_name not in seen:
                seen.add(definition.column_name)
                definitions.append(definition)
    return tuple(definitions)


ALL_EXPORT_SCHEMA = _ordered_schema_union(
    MASTER_SCHEMA,
    FRAME_FEATURE_SCHEMA,
    LOADCELL_RAW_SCHEMA,
    GRAPH_SPATIAL_SCHEMA,
    GRAPH_TEMPORAL_SCHEMA,
)

# One dictionary row is emitted for every column in every exported CSV scope.
# This deliberately retains same-named columns when their artifact semantics
# differ.  Graph filenames vary by snapshot/summary kind, so those scopes name
# the stable companion schema rather than one concrete filename.
DATA_DICTIONARY_ARTIFACT_SCHEMAS = (
    ("frame_features.csv", FRAME_FEATURE_SCHEMA),
    ("master_synchronized.csv", MASTER_SCHEMA),
    ("loadcell_raw.csv", LOADCELL_RAW_SCHEMA),
    ("spatial_graph_companion.csv", GRAPH_SPATIAL_SCHEMA),
    ("temporal_graph_companion.csv", GRAPH_TEMPORAL_SCHEMA),
)

LOADCELL_RAW_COLUMNS = tuple(item.column_name for item in LOADCELL_RAW_SCHEMA)
FRAME_FEATURE_COLUMNS = tuple(item.column_name for item in FRAME_FEATURE_SCHEMA)
MASTER_COLUMNS = tuple(item.column_name for item in MASTER_SCHEMA)
GRAPH_SPATIAL_COLUMNS = tuple(item.column_name for item in GRAPH_SPATIAL_SCHEMA)
GRAPH_TEMPORAL_COLUMNS = tuple(item.column_name for item in GRAPH_TEMPORAL_SCHEMA)
ALL_EXPORT_COLUMNS = tuple(item.column_name for item in ALL_EXPORT_SCHEMA)


def _assert_unique(schema: Sequence[ColumnDefinition], name: str) -> None:
    columns = [definition.column_name for definition in schema]
    duplicate_columns = sorted(
        {column for column in columns if columns.count(column) > 1}
    )
    if duplicate_columns:
        raise RuntimeError(f"{name} contains duplicate columns: {duplicate_columns}")


_assert_unique(LOADCELL_RAW_SCHEMA, "loadcell_raw schema")
_assert_unique(FRAME_FEATURE_SCHEMA, "frame_features schema")
_assert_unique(MASTER_SCHEMA, "master schema")
_assert_unique(GRAPH_SPATIAL_SCHEMA, "spatial-graph schema")
_assert_unique(GRAPH_TEMPORAL_SCHEMA, "temporal-graph schema")
_assert_unique(ALL_EXPORT_SCHEMA, "all-export unique-name helper schema")


def data_dictionary_rows(
    schema: Sequence[ColumnDefinition] | None = None,
    *,
    artifact_scope: str = "custom_csv",
) -> list[dict[str, str]]:
    """Return artifact-aware dictionary rows.

    With no selected schema, the result covers every required raw, derived,
    master, and graph-companion CSV.  A caller requesting one custom schema may
    provide its own ``artifact_scope`` label.
    """

    if schema is not None:
        return [
            definition.as_dictionary_row(artifact_scope) for definition in schema
        ]
    return [
        definition.as_dictionary_row(scope)
        for scope, scoped_schema in DATA_DICTIONARY_ARTIFACT_SCHEMAS
        for definition in scoped_schema
    ]


DATA_DICTIONARY_ROWS = tuple(data_dictionary_rows())


def schema_for(dataset: str) -> tuple[ColumnDefinition, ...]:
    """Resolve a required CSV filename or logical dataset to its schema."""

    normalized = dataset.strip().lower().replace(".csv.partial", "").replace(
        ".csv", ""
    )
    schemas = {
        "loadcell_raw": LOADCELL_RAW_SCHEMA,
        "frame_features": FRAME_FEATURE_SCHEMA,
        "master_synchronized": MASTER_SCHEMA,
        "master": MASTER_SCHEMA,
        "graph_spatial": GRAPH_SPATIAL_SCHEMA,
        "spatial_graph": GRAPH_SPATIAL_SCHEMA,
        "graph_temporal": GRAPH_TEMPORAL_SCHEMA,
        "temporal_graph": GRAPH_TEMPORAL_SCHEMA,
    }
    try:
        return schemas[normalized]
    except KeyError as exc:
        raise KeyError(f"unknown exported dataset: {dataset}") from exc


def validate_exact_columns(
    actual_columns: Iterable[str],
    expected_schema: Sequence[ColumnDefinition] = MASTER_SCHEMA,
) -> None:
    """Require exact column identity and order; raise with actionable evidence."""

    actual = tuple(actual_columns)
    expected = tuple(item.column_name for item in expected_schema)
    if actual == expected:
        return
    missing = tuple(column for column in expected if column not in actual)
    unexpected = tuple(column for column in actual if column not in expected)
    ordering_only = not missing and not unexpected
    raise ValueError(
        "export columns do not match schema; "
        f"missing={missing}, unexpected={unexpected}, ordering_only={ordering_only}"
    )


__all__ = [
    "ColumnDefinition",
    "ALL_EXPORT_COLUMNS",
    "ALL_EXPORT_SCHEMA",
    "DATA_DICTIONARY_COLUMNS",
    "DATA_DICTIONARY_ARTIFACT_SCHEMAS",
    "DATA_DICTIONARY_ROWS",
    "FRAME_FEATURE_COLUMNS",
    "FRAME_FEATURE_SCHEMA",
    "FRAME_IDENTITY_SCHEMA",
    "GRAPH_SPATIAL_COLUMNS",
    "GRAPH_SPATIAL_SCHEMA",
    "GRAPH_TEMPORAL_COLUMNS",
    "GRAPH_TEMPORAL_SCHEMA",
    "LOADCELL_RAW_COLUMNS",
    "LOADCELL_RAW_SCHEMA",
    "LOADCELL_SYNC_SCHEMA",
    "LOCALIZATION_SCHEMA",
    "MASTER_COLUMNS",
    "MASTER_SCHEMA",
    "MOTION_PROVENANCE_SCHEMA",
    "ROI_FEATURE_SCHEMA",
    "SCHEMA_VERSION",
    "SESSION_SCHEMA",
    "VALIDITY_PROVENANCE_SCHEMA",
    "data_dictionary_rows",
    "schema_for",
    "validate_exact_columns",
]
