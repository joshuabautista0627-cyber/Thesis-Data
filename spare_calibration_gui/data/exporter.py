"""Incremental, collision-safe export and validated session finalization.

The recording loop uses :class:`IncrementalCsvWriter`, which is deliberately
implemented with the standard-library ``csv`` module rather than pandas.  The
master file is generated only after acquisition has stopped so that a physical
load-cell sample on both sides of a frame can be used for interpolation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import csv
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime, time as datetime_time
from enum import Enum
import json
import math
import os
from pathlib import Path
import time
from typing import Any, TextIO
import uuid

import numpy as np

from core.models import ErrorCode, LoadCellCalibration, LoadCellSample
from data.schemas import (
    ALL_EXPORT_SCHEMA,
    DATA_DICTIONARY_COLUMNS,
    FRAME_FEATURE_COLUMNS,
    FRAME_FEATURE_SCHEMA,
    LOADCELL_RAW_COLUMNS,
    LOADCELL_RAW_SCHEMA,
    MASTER_COLUMNS,
    MASTER_SCHEMA,
    ColumnDefinition,
    data_dictionary_rows,
    validate_exact_columns,
)
from processing.synchronization import synchronize_frames_to_force


FRAME_FEATURE_PARTIAL = "frame_features.csv.partial"
LOADCELL_RAW_PARTIAL = "loadcell_raw.csv.partial"
MASTER_PARTIAL = "master_synchronized.csv.partial"
FRAME_FEATURE_FINAL = "frame_features.csv"
LOADCELL_RAW_FINAL = "loadcell_raw.csv"
MASTER_FINAL = "master_synchronized.csv"
DATA_DICTIONARY_FILENAME = "data_dictionary.csv"


class ExportError(RuntimeError):
    """Base error for an export that cannot be trusted."""


class ExistingExportError(FileExistsError, ExportError):
    """Raised before an existing final or partial artifact could be changed."""


class CsvValidationError(ValueError, ExportError):
    """Raised when a CSV does not exactly satisfy its canonical contract."""


class FinalizationError(ExportError):
    """Raised when validated partial files cannot be finalized together."""


@dataclass(frozen=True, slots=True)
class CsvValidationSummary:
    """Evidence returned after a complete CSV validation pass."""

    path: Path
    columns: tuple[str, ...]
    row_count: int
    id_column: str | None = None
    first_id: int | None = None
    last_id: int | None = None
    capture_frame_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PartialFileInspection:
    """Read-only recovery status for one expected incremental file."""

    path: Path
    exists: bool
    valid: bool
    row_count: int
    error: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryInspection:
    """Read-only assessment of whether raw partials can be finalized."""

    session_directory: Path
    frame_features: PartialFileInspection
    loadcell_raw: PartialFileInspection
    master_synchronized: PartialFileInspection
    conflicting_final_files: tuple[Path, ...]
    unexpected_partial_files: tuple[Path, ...]

    @property
    def recoverable(self) -> bool:
        """Both raw sources are valid and no final-file collision exists."""

        return (
            self.frame_features.valid
            and self.loadcell_raw.valid
            and not self.conflicting_final_files
        )


@dataclass(frozen=True, slots=True)
class FinalizationSummary:
    """Counts and final paths proving one-to-one raw/master preservation."""

    session_directory: Path
    frame_count: int
    loadcell_sample_count: int
    master_row_count: int
    missing_force_count: int
    max_sync_gap_ms_observed: float
    final_paths: tuple[Path, Path, Path]

    @property
    def one_row_per_frame(self) -> bool:
        return self.frame_count == self.master_row_count


def _json_path(parent: str, child: str) -> str:
    if child.startswith("["):
        return f"{parent}{child}"
    return f"{parent}.{child}"


def _portable_json_value(
    value: Any,
    *,
    path: str,
    nonfinite: list[dict[str, str]],
) -> Any:
    """Recursively convert common scientific Python values to strict JSON."""

    if isinstance(value, Enum):
        return _portable_json_value(value.value, path=path, nonfinite=nonfinite)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return _portable_json_value(value.tolist(), path=path, nonfinite=nonfinite)
    if isinstance(value, np.generic):
        return _portable_json_value(value.item(), path=path, nonfinite=nonfinite)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        status = (
            "nan"
            if math.isnan(value)
            else "positive_infinity"
            if value > 0
            else "negative_infinity"
        )
        nonfinite.append({"path": path, "status": status})
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be strings; got {type(key).__name__}")
            result[key] = _portable_json_value(
                item, path=_json_path(path, key), nonfinite=nonfinite
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _portable_json_value(
                item, path=_json_path(path, f"[{index}]"), nonfinite=nonfinite
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        ordered = sorted(value, key=lambda item: repr(item))
        return _portable_json_value(ordered, path=path, nonfinite=nonfinite)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _portable_json_value(to_dict(), path=path, nonfinite=nonfinite)
    if is_dataclass(value) and not isinstance(value, type):
        # Avoid dataclasses.asdict's deep-copy behavior for arrays and Paths.
        payload = {field.name: getattr(value, field.name) for field in fields(value)}
        return _portable_json_value(payload, path=path, nonfinite=nonfinite)
    raise TypeError(f"value of type {type(value).__name__} is not JSON portable")


def to_portable_json(
    value: Any, *, include_nonfinite_status: bool = False
) -> Any:
    """Return a JSON-compatible value, encoding NaN/infinity as ``null``.

    When status evidence is scientifically useful, ``include_nonfinite_status``
    adds a ``nonfinite_value_status`` list to a root object.  Models such as
    :class:`LoadCellCalibration` retain their own more specific status field via
    ``to_dict()`` before this generic conversion is applied.
    """

    nonfinite: list[dict[str, str]] = []
    portable = _portable_json_value(value, path="$", nonfinite=nonfinite)
    if include_nonfinite_status and nonfinite:
        if not isinstance(portable, dict):
            portable = {"value": portable}
        if "nonfinite_value_status" in portable:
            raise ValueError("payload already defines nonfinite_value_status")
        portable["nonfinite_value_status"] = nonfinite
    return portable


def strict_json_dumps(
    value: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    include_nonfinite_status: bool = False,
) -> str:
    """Serialize portable JSON while forbidding nonstandard NaN tokens."""

    portable = to_portable_json(
        value, include_nonfinite_status=include_nonfinite_status
    )
    return json.dumps(
        portable,
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        sort_keys=sort_keys,
    )


def _temporary_sibling(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")


def _move_no_overwrite(source: Path, destination: Path) -> None:
    """Publish a same-directory file atomically without replacing a target."""

    if destination.exists():
        raise ExistingExportError(f"refusing to overwrite existing file: {destination}")
    try:
        if os.name == "nt":
            # Windows rename fails when the destination already exists.
            os.rename(source, destination)
        else:
            # link(2) provides atomic no-replace semantics on POSIX.
            os.link(source, destination)
            source.unlink()
    except FileExistsError as exc:
        raise ExistingExportError(
            f"refusing to overwrite existing file: {destination}"
        ) from exc


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    include_nonfinite_status: bool = False,
) -> Path:
    """Write strict UTF-8 JSON through a sibling temp file, without overwrite."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ExistingExportError(f"refusing to overwrite existing file: {destination}")
    temporary = _temporary_sibling(destination)
    try:
        payload = strict_json_dumps(
            value,
            include_nonfinite_status=include_nonfinite_status,
        )
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _move_no_overwrite(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _column_names(
    columns: Sequence[str] | Sequence[ColumnDefinition],
) -> tuple[str, ...]:
    names = tuple(
        item.column_name if isinstance(item, ColumnDefinition) else str(item)
        for item in columns
    )
    if not names:
        raise ValueError("CSV columns must not be empty")
    if len(set(names)) != len(names):
        raise ValueError("CSV columns must be unique")
    return names


_DEFINITION_BY_NAME = {
    definition.column_name: definition for definition in ALL_EXPORT_SCHEMA
}


def _csv_scalar(value: Any) -> str | int | float | bool:
    if isinstance(value, Enum):
        return _csv_scalar(value.value)
    if isinstance(value, np.generic):
        return _csv_scalar(value.item())
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            raise ValueError("CSV export does not permit infinite values")
    if isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported CSV scalar type: {type(value).__name__}")


def _missing_value_for(column: str) -> object:
    definition = _DEFINITION_BY_NAME.get(column)
    if definition is None:
        return ""
    data_type = definition.data_type.lower()
    if "string" in data_type:
        return ""
    return math.nan


def complete_schema_row(
    columns: Sequence[str] | Sequence[ColumnDefinition],
    values: Mapping[str, Any],
) -> dict[str, object]:
    """Return one exactly ordered row, filling unavailable fields explicitly.

    Unknown keys are rejected.  Numeric omissions become IEEE NaN (written as
    ``NaN``); string omissions become blank.  Callers must still set scientific
    validity flags and error codes deliberately.
    """

    names = _column_names(columns)
    unexpected = tuple(key for key in values if key not in names)
    if unexpected:
        raise ValueError(f"row contains unexpected columns: {unexpected}")
    return {
        column: values[column] if column in values else _missing_value_for(column)
        for column in names
    }


def build_feature_failure_row(
    base_values: Mapping[str, Any],
    *,
    error_code: ErrorCode | str = ErrorCode.FEATURE_EXTRACTION_FAILED,
) -> dict[str, object]:
    """Preserve an accepted frame when feature extraction raises an error."""

    required = (
        "session_id",
        "trial_id",
        "sensing_skin_id",
        "target_roi_ground_truth",
        "contact_threshold_N",
        "capture_frame_id",
        "host_monotonic_ns",
        "elapsed_time_s",
        "wall_clock_iso",
        "requested_fps",
        "width",
        "height",
        "camera_backend",
        "camera_device_index",
    )
    missing = tuple(name for name in required if name not in base_values)
    if missing:
        raise ValueError(f"feature-failure frame is missing identity fields: {missing}")
    row = complete_schema_row(FRAME_FEATURE_SCHEMA, base_values)
    row["frame_valid"] = bool(base_values.get("frame_valid", True))
    row["baseline_valid"] = bool(base_values.get("baseline_valid", False))
    row["saturation_warning"] = bool(
        base_values.get("saturation_warning", False)
    )
    row["error_code"] = error_code.value if isinstance(error_code, Enum) else str(error_code)
    return row


def loadcell_sample_to_row(
    sample: LoadCellSample | Mapping[str, Any],
) -> dict[str, object]:
    """Flatten one physical sample to the exact incremental raw schema."""

    if isinstance(sample, LoadCellSample):
        values: Mapping[str, Any] = {
            field.name: getattr(sample, field.name) for field in fields(sample)
        }
    elif isinstance(sample, Mapping):
        values = sample
    else:
        raise TypeError("sample must be LoadCellSample or a mapping")
    return complete_schema_row(LOADCELL_RAW_SCHEMA, values)


def _validate_exact_row(row: Mapping[str, Any], columns: tuple[str, ...]) -> None:
    actual = tuple(row.keys())
    if set(actual) != set(columns):
        missing = tuple(column for column in columns if column not in row)
        unexpected = tuple(column for column in actual if column not in columns)
        raise ValueError(
            f"CSV row does not match schema; missing={missing}, unexpected={unexpected}"
        )


class IncrementalCsvWriter:
    """Buffered incremental writer with an immutable, precomputed schema."""

    def __init__(
        self,
        path: str | Path,
        columns: Sequence[str] | Sequence[ColumnDefinition],
        flush_interval_s: float = 1.0,
        flush_row_count: int = 30,
        allow_existing: bool = False,
    ) -> None:
        if not math.isfinite(float(flush_interval_s)) or flush_interval_s <= 0:
            raise ValueError("flush_interval_s must be finite and positive")
        if isinstance(flush_row_count, bool) or flush_row_count < 1:
            raise ValueError("flush_row_count must be a positive integer")
        self.path = Path(path)
        self.columns = _column_names(columns)
        self.flush_interval_s = float(flush_interval_s)
        self.flush_row_count = int(flush_row_count)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        final_counterpart = (
            self.path.with_suffix("") if self.path.suffix == ".partial" else None
        )
        if final_counterpart is not None and final_counterpart.exists():
            raise ExistingExportError(
                f"final export already exists: {final_counterpart}"
            )
        self.rows_written = 0
        self._rows_since_flush = 0
        self._last_flush_monotonic = time.monotonic()
        self._closed = False
        self._handle: TextIO

        if self.path.exists():
            if not allow_existing:
                raise ExistingExportError(
                    f"refusing to overwrite existing incremental file: {self.path}"
                )
            summary = validate_csv_file(self.path, self.columns)
            self.rows_written = summary.row_count
            self._handle = self.path.open("a", encoding="utf-8", newline="")
        else:
            self._handle = self.path.open("x", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(
            self._handle,
            fieldnames=self.columns,
            extrasaction="raise",
            lineterminator="\n",
        )
        if self.rows_written == 0 and self.path.stat().st_size == 0:
            self._writer.writeheader()
            self._durable_flush()

    @property
    def closed(self) -> bool:
        return self._closed

    def _durable_flush(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._rows_since_flush = 0
        self._last_flush_monotonic = time.monotonic()

    def write_row(self, row: Mapping[str, Any]) -> None:
        """Write exactly one row and flush on the configured time/count policy."""

        if self._closed:
            raise ValueError("cannot write to a closed incremental CSV writer")
        _validate_exact_row(row, self.columns)
        if "capture_frame_id" in self.columns:
            frame_id = _parse_int(row["capture_frame_id"], "capture_frame_id")
            if frame_id != self.rows_written:
                raise CsvValidationError(
                    "capture_frame_id must be contiguous and zero-based; "
                    f"expected {self.rows_written}, received {frame_id}"
                )
        serialized = {column: _csv_scalar(row[column]) for column in self.columns}
        self._writer.writerow(serialized)
        self.rows_written += 1
        self._rows_since_flush += 1
        elapsed = time.monotonic() - self._last_flush_monotonic
        if (
            self._rows_since_flush >= self.flush_row_count
            or elapsed >= self.flush_interval_s
        ):
            self._durable_flush()

    def flush(self) -> None:
        if self._closed:
            return
        self._durable_flush()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._durable_flush()
        finally:
            self._handle.close()
            self._closed = True

    def __enter__(self) -> "IncrementalCsvWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


# Readable compatibility spelling for callers that use CSV as an initialism.
IncrementalCSVWriter = IncrementalCsvWriter
BufferedIncrementalCsvWriter = IncrementalCsvWriter


def atomic_write_csv(
    path: str | Path,
    columns: Sequence[str] | Sequence[ColumnDefinition],
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    """Write and durably publish a complete CSV without overwriting a file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ExistingExportError(f"refusing to overwrite existing file: {destination}")
    names = _column_names(columns)
    temporary = _temporary_sibling(destination)
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=names, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                _validate_exact_row(row, names)
                writer.writerow({name: _csv_scalar(row[name]) for name in names})
            handle.flush()
            os.fsync(handle.fileno())
        _move_no_overwrite(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def write_data_dictionary(path: str | Path) -> Path:
    """Write complete metadata for the union of all exported CSV columns."""

    return atomic_write_csv(
        path,
        DATA_DICTIONARY_COLUMNS,
        data_dictionary_rows(),
    )


def _parse_int(value: Any, column: str) -> int:
    if value is None or isinstance(value, bool):
        raise CsvValidationError(f"{column} must be an integer")
    text = str(value).strip()
    try:
        numeric = float(text)
    except ValueError as exc:
        raise CsvValidationError(f"{column} must be an integer: {value!r}") from exc
    if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 0:
        raise CsvValidationError(f"{column} must be a nonnegative integer: {value!r}")
    return int(numeric)


def _parse_float(value: Any, column: str, *, finite: bool = False) -> float:
    text = "" if value is None else str(value).strip()
    if not text:
        return math.nan
    try:
        result = float(text)
    except ValueError as exc:
        raise CsvValidationError(f"{column} must be numeric: {value!r}") from exc
    if math.isinf(result) or (finite and not math.isfinite(result)):
        raise CsvValidationError(f"{column} must be finite: {value!r}")
    return result


def _parse_bool(value: Any, column: str, *, nullable: bool = False) -> bool | float:
    text = "" if value is None else str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    if nullable and (not text or text == "nan"):
        return math.nan
    raise CsvValidationError(f"{column} must be boolean{' or NaN' if nullable else ''}")


def _read_csv_rows(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise CsvValidationError(f"CSV has no header: {path}")
            rows: list[dict[str, str]] = []
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    raise CsvValidationError(
                        f"CSV line {line_number} has fields beyond the header: {path}"
                    )
                missing_cells = tuple(key for key, value in row.items() if value is None)
                if missing_cells:
                    raise CsvValidationError(
                        f"CSV line {line_number} is truncated; missing={missing_cells}: {path}"
                    )
                rows.append({key: str(value) for key, value in row.items()})
            return tuple(reader.fieldnames), rows
    except UnicodeDecodeError as exc:
        raise CsvValidationError(f"CSV is not valid UTF-8: {path}") from exc


def validate_csv_file(
    path: str | Path,
    columns: Sequence[str] | Sequence[ColumnDefinition],
    *,
    expected_capture_frame_ids: Sequence[int] | None = None,
) -> CsvValidationSummary:
    """Validate exact header order, structural rows, clocks, and identities."""

    source = Path(path)
    if not source.is_file():
        raise CsvValidationError(f"CSV file does not exist: {source}")
    expected_columns = _column_names(columns)
    actual_columns, rows = _read_csv_rows(source)
    try:
        schema = tuple(
            _DEFINITION_BY_NAME[name] for name in expected_columns
        )
    except KeyError:
        if actual_columns != expected_columns:
            raise CsvValidationError(
                f"CSV header mismatch; expected={expected_columns}, actual={actual_columns}"
            )
    else:
        try:
            validate_exact_columns(actual_columns, schema)
        except ValueError as exc:
            raise CsvValidationError(str(exc)) from exc

    frame_ids: tuple[int, ...] = ()
    id_column: str | None = None
    if "capture_frame_id" in expected_columns:
        id_column = "capture_frame_id"
        frame_ids = tuple(_parse_int(row[id_column], id_column) for row in rows)
        expected_ids = tuple(range(len(rows)))
        if frame_ids != expected_ids:
            raise CsvValidationError(
                "capture_frame_id values must be unique, contiguous, zero-based, "
                f"and ordered; expected={expected_ids}, actual={frame_ids}"
            )
        if expected_capture_frame_ids is not None and frame_ids != tuple(
            expected_capture_frame_ids
        ):
            raise CsvValidationError(
                "capture_frame_id order does not match frame_features.csv"
            )
    elif "arduino_sample_id" in expected_columns:
        id_column = "arduino_sample_id"
        last_by_session: dict[str, int] = {}
        for row in rows:
            sample_id = _parse_int(row[id_column], id_column)
            session_id = row.get("device_session_id", "").strip()
            if not session_id:
                raise CsvValidationError("device_session_id must not be blank")
            previous = last_by_session.get(session_id)
            if previous is not None and sample_id <= previous:
                raise CsvValidationError(
                    "arduino_sample_id must increase within each device session"
                )
            last_by_session[session_id] = sample_id

    if "host_monotonic_ns" in expected_columns:
        timestamps = tuple(
            _parse_int(row["host_monotonic_ns"], "host_monotonic_ns") for row in rows
        )
        if any(current < previous for previous, current in zip(timestamps, timestamps[1:])):
            raise CsvValidationError("host_monotonic_ns values must be nondecreasing")

    if tuple(expected_columns) == tuple(LOADCELL_RAW_COLUMNS):
        for row in rows:
            _parse_int(row["arduino_micros"], "arduino_micros")
            raw = _parse_float(row["raw_adc"], "raw_adc", finite=True)
            if not raw.is_integer():
                raise CsvValidationError("raw_adc must contain signed integer counts")
            _parse_bool(row["loadcell_valid"], "loadcell_valid")

    if tuple(expected_columns) == tuple(MASTER_COLUMNS):
        for row in rows:
            method = row["synchronization_method"].strip()
            if method not in {"linear_interpolation", "nearest", "invalid"}:
                raise CsvValidationError(f"unknown synchronization_method: {method!r}")
            valid = _parse_bool(row["synchronization_valid"], "synchronization_valid")
            force = _parse_float(row["force_N"], "force_N")
            interpolated = _parse_float(
                row["interpolated_raw_adc"], "interpolated_raw_adc"
            )
            contact = _parse_bool(
                row["contact_state_derived"],
                "contact_state_derived",
                nullable=True,
            )
            if valid:
                if method == "invalid" or not math.isfinite(force) or not math.isfinite(interpolated):
                    raise CsvValidationError(
                        "valid synchronization requires finite force/raw estimate and a valid method"
                    )
                if isinstance(contact, float) and math.isnan(contact):
                    raise CsvValidationError(
                        "valid synchronization requires a derived contact state"
                    )
            elif method != "invalid" or math.isfinite(force) or math.isfinite(interpolated):
                raise CsvValidationError(
                    "invalid synchronization must use method=invalid and NaN force/raw estimate"
                )
            elif not (isinstance(contact, float) and math.isnan(contact)):
                raise CsvValidationError(
                    "contact_state_derived must be NaN when force is unavailable"
                )

    first_id = frame_ids[0] if frame_ids else None
    last_id = frame_ids[-1] if frame_ids else None
    return CsvValidationSummary(
        path=source,
        columns=actual_columns,
        row_count=len(rows),
        id_column=id_column,
        first_id=first_id,
        last_id=last_id,
        capture_frame_ids=frame_ids,
    )


def validate_frame_master_identity(
    frame_features_path: str | Path,
    master_path: str | Path,
    *,
    video_frame_count: int | None = None,
) -> tuple[CsvValidationSummary, CsvValidationSummary]:
    """Require identical frame IDs/order and, when known, video frame count."""

    frame_summary = validate_csv_file(frame_features_path, FRAME_FEATURE_SCHEMA)
    master_summary = validate_csv_file(
        master_path,
        MASTER_SCHEMA,
        expected_capture_frame_ids=frame_summary.capture_frame_ids,
    )
    if frame_summary.row_count != master_summary.row_count:
        raise CsvValidationError("frame_features and master row counts differ")
    if video_frame_count is not None:
        if video_frame_count < 0:
            raise ValueError("video_frame_count must be nonnegative")
        if frame_summary.row_count != video_frame_count:
            raise CsvValidationError(
                "video/frame_features/master counts are not one-to-one; "
                f"video={video_frame_count}, CSV={frame_summary.row_count}"
            )
    return frame_summary, master_summary


def _inspect_one(path: Path, schema: Sequence[ColumnDefinition]) -> PartialFileInspection:
    if not path.exists():
        return PartialFileInspection(path, False, False, 0, "file is absent")
    try:
        summary = validate_csv_file(path, schema)
    except (OSError, CsvValidationError) as exc:
        return PartialFileInspection(path, True, False, 0, str(exc))
    return PartialFileInspection(path, True, True, summary.row_count)


def inspect_partial_session(session_dir: str | Path) -> RecoveryInspection:
    """Inspect partial artifacts without changing, renaming, or truncating them."""

    directory = Path(session_dir)
    expected = {
        FRAME_FEATURE_PARTIAL,
        LOADCELL_RAW_PARTIAL,
        MASTER_PARTIAL,
    }
    unexpected = tuple(
        sorted(
            (
                path
                for path in directory.glob("*.partial")
                if path.name not in expected
            ),
            key=lambda path: path.name,
        )
    ) if directory.is_dir() else ()
    conflicts = tuple(
        path
        for path in (
            directory / FRAME_FEATURE_FINAL,
            directory / LOADCELL_RAW_FINAL,
            directory / MASTER_FINAL,
        )
        if path.exists()
    )
    return RecoveryInspection(
        session_directory=directory,
        frame_features=_inspect_one(
            directory / FRAME_FEATURE_PARTIAL, FRAME_FEATURE_SCHEMA
        ),
        loadcell_raw=_inspect_one(directory / LOADCELL_RAW_PARTIAL, LOADCELL_RAW_SCHEMA),
        master_synchronized=_inspect_one(directory / MASTER_PARTIAL, MASTER_SCHEMA),
        conflicting_final_files=conflicts,
        unexpected_partial_files=unexpected,
    )


def _calibration_values(
    calibration: LoadCellCalibration | Mapping[str, Any],
) -> tuple[float, float, str, str, int]:
    if isinstance(calibration, LoadCellCalibration):
        return (
            float(calibration.counts_per_gram),
            float(calibration.tare_raw),
            calibration.calibration_id,
            calibration.serial_port,
            int(calibration.baud_rate),
        )
    try:
        counts = float(calibration["counts_per_gram"])
        tare = float(calibration["tare_raw"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("calibration requires counts_per_gram and tare_raw") from exc
    calibration_id = str(calibration.get("calibration_id", ""))
    serial_port = str(calibration.get("serial_port", ""))
    baud_rate = int(calibration.get("baud_rate", 0))
    if not math.isfinite(counts) or counts == 0.0 or not math.isfinite(tare):
        raise ValueError("calibration scale/tare must be finite and scale nonzero")
    if baud_rate < 0:
        raise ValueError("calibration baud_rate must be nonnegative")
    return counts, tare, calibration_id, serial_port, baud_rate


def _sync_load_rows(rows: Sequence[Mapping[str, str]]) -> list[dict[str, object]]:
    prepared: list[dict[str, object]] = []
    for row in rows:
        valid = _parse_bool(row["loadcell_valid"], "loadcell_valid")
        prepared.append(
            {
                "host_monotonic_ns": _parse_int(
                    row["host_monotonic_ns"], "host_monotonic_ns"
                ),
                "arduino_sample_id": _parse_int(
                    row["arduino_sample_id"], "arduino_sample_id"
                ),
                "arduino_micros": _parse_int(
                    row["arduino_micros"], "arduino_micros"
                ),
                "raw_adc": _parse_float(row["raw_adc"], "raw_adc", finite=True),
                "force_gf": _parse_float(row["force_gf"], "force_gf"),
                "force_N": _parse_float(row["force_N"], "force_N"),
                "device_session_id": row["device_session_id"],
                "loadcell_valid": valid,
            }
        )
    return prepared


def _master_row(
    frame_row: Mapping[str, str],
    sync: Any,
    *,
    counts_per_gram: float,
    tare_raw: float,
    serial_port: str,
    baud_rate: int,
) -> dict[str, object]:
    values: dict[str, object] = dict(frame_row)
    values.update(
        {
            "closest_arduino_sample_id": sync.closest_arduino_sample_id,
            "arduino_micros": sync.closest_arduino_micros,
            "closest_raw_adc": sync.closest_raw_adc,
            "interpolated_raw_adc": sync.interpolated_raw_adc,
            "tared_raw": (
                sync.interpolated_raw_adc - tare_raw
                if sync.synchronization_valid
                else math.nan
            ),
            "mass_g": sync.force_gf,
            "force_gf": sync.force_gf,
            "force_N": sync.force_N,
            "synchronization_method": sync.synchronization_method,
            "nearest_sample_gap_ms": sync.nearest_sample_gap_ms,
            "synchronization_valid": sync.synchronization_valid,
            "counts_per_gram": counts_per_gram,
            "tare_raw": tare_raw,
            "serial_port": serial_port,
            "baud_rate": baud_rate,
            "synchronization_offset_ms": sync.synchronization_offset_ms,
            "contact_state_derived": sync.contact_state_derived,
            "device_session_id": sync.closest_device_session_id or "",
            "loadcell_valid": sync.synchronization_valid,
        }
    )
    original_error = str(frame_row.get("error_code", "")).strip()
    if not sync.synchronization_valid and original_error in {"", ErrorCode.NONE.value}:
        values["error_code"] = ErrorCode.SYNCHRONIZATION_GAP_EXCEEDED.value
    return complete_schema_row(MASTER_SCHEMA, values)


def _finalize_validated_files(
    pairs: Sequence[tuple[Path, Path]],
) -> None:
    for source, destination in pairs:
        if not source.is_file():
            raise FinalizationError(f"partial file disappeared: {source}")
        if destination.exists():
            raise ExistingExportError(
                f"refusing to overwrite existing final file: {destination}"
            )
    completed: list[tuple[Path, Path]] = []
    try:
        for source, destination in pairs:
            _move_no_overwrite(source, destination)
            completed.append((source, destination))
    except Exception as exc:
        rollback_errors: list[str] = []
        for source, destination in reversed(completed):
            try:
                _move_no_overwrite(destination, source)
            except Exception as rollback_exc:  # pragma: no cover - catastrophic FS path
                rollback_errors.append(str(rollback_exc))
        detail = f"; rollback errors={rollback_errors}" if rollback_errors else ""
        raise FinalizationError(f"atomic finalization failed: {exc}{detail}") from exc


def finalize_master_from_partials(
    session_dir: str | Path,
    calibration: LoadCellCalibration | Mapping[str, Any],
    max_sync_gap_ms: float = 200.0,
    contact_threshold_N: float = 0.05,
) -> FinalizationSummary:
    """Validate raw partials, build master partial, then publish all three.

    Every frame row is copied exactly once, including feature-failure rows.  The
    shared host-monotonic synchronization implementation supplies force fields;
    unavailable force remains NaN with explicit invalid/error flags.
    """

    directory = Path(session_dir)
    frame_partial = directory / FRAME_FEATURE_PARTIAL
    load_partial = directory / LOADCELL_RAW_PARTIAL
    master_partial = directory / MASTER_PARTIAL
    finals = (
        directory / FRAME_FEATURE_FINAL,
        directory / LOADCELL_RAW_FINAL,
        directory / MASTER_FINAL,
    )
    if master_partial.exists():
        raise ExistingExportError(
            f"refusing to overwrite existing master partial: {master_partial}"
        )
    if any(path.exists() for path in finals):
        existing = tuple(path for path in finals if path.exists())
        raise ExistingExportError(f"final export collision: {existing}")

    frame_summary = validate_csv_file(frame_partial, FRAME_FEATURE_SCHEMA)
    load_summary = validate_csv_file(load_partial, LOADCELL_RAW_SCHEMA)
    _, frame_rows = _read_csv_rows(frame_partial)
    _, load_rows = _read_csv_rows(load_partial)
    (
        counts_per_gram,
        tare_raw,
        calibration_id,
        calibration_serial_port,
        calibration_baud_rate,
    ) = _calibration_values(calibration)
    if calibration_id:
        conflicting = {
            row["calibration_id"]
            for row in frame_rows
            if row["calibration_id"].strip()
            and row["calibration_id"].strip() != calibration_id
        }
        if conflicting:
            raise CsvValidationError(
                f"frame calibration IDs conflict with finalization calibration: {conflicting}"
            )

    frame_timestamps = [
        _parse_int(row["host_monotonic_ns"], "host_monotonic_ns")
        for row in frame_rows
    ]
    synchronization = synchronize_frames_to_force(
        frame_timestamps,
        _sync_load_rows(load_rows),
        max_sync_gap_ms=max_sync_gap_ms,
        contact_threshold_N=contact_threshold_N,
    )
    master_rows = [
        _master_row(
            frame_row,
            sync,
            counts_per_gram=counts_per_gram,
            tare_raw=tare_raw,
            serial_port=calibration_serial_port,
            baud_rate=calibration_baud_rate,
        )
        for frame_row, sync in zip(frame_rows, synchronization, strict=True)
    ]
    atomic_write_csv(master_partial, MASTER_SCHEMA, master_rows)
    try:
        master_summary = validate_csv_file(
            master_partial,
            MASTER_SCHEMA,
            expected_capture_frame_ids=frame_summary.capture_frame_ids,
        )
        if master_summary.row_count != frame_summary.row_count:
            raise CsvValidationError(
                "master finalization did not preserve exactly one row per frame"
            )
        # Revalidate raw inputs immediately before publication.
        validate_csv_file(frame_partial, FRAME_FEATURE_SCHEMA)
        validate_csv_file(load_partial, LOADCELL_RAW_SCHEMA)
        _finalize_validated_files(
            (
                (frame_partial, finals[0]),
                (load_partial, finals[1]),
                (master_partial, finals[2]),
            )
        )
    except Exception:
        # Keep a valid master partial for recovery/audit; never silently delete it.
        raise

    missing_force = sum(not item.synchronization_valid for item in synchronization)
    finite_gaps = [
        item.nearest_sample_gap_ms
        for item in synchronization
        if math.isfinite(item.nearest_sample_gap_ms)
    ]
    return FinalizationSummary(
        session_directory=directory,
        frame_count=frame_summary.row_count,
        loadcell_sample_count=load_summary.row_count,
        master_row_count=master_summary.row_count,
        missing_force_count=missing_force,
        max_sync_gap_ms_observed=max(finite_gaps, default=math.nan),
        final_paths=finals,
    )


__all__ = [
    "BufferedIncrementalCsvWriter",
    "CsvValidationError",
    "CsvValidationSummary",
    "DATA_DICTIONARY_FILENAME",
    "ExistingExportError",
    "ExportError",
    "FRAME_FEATURE_FINAL",
    "FRAME_FEATURE_PARTIAL",
    "FinalizationError",
    "FinalizationSummary",
    "IncrementalCSVWriter",
    "IncrementalCsvWriter",
    "LOADCELL_RAW_FINAL",
    "LOADCELL_RAW_PARTIAL",
    "MASTER_FINAL",
    "MASTER_PARTIAL",
    "PartialFileInspection",
    "RecoveryInspection",
    "atomic_write_csv",
    "atomic_write_json",
    "build_feature_failure_row",
    "complete_schema_row",
    "finalize_master_from_partials",
    "inspect_partial_session",
    "loadcell_sample_to_row",
    "strict_json_dumps",
    "to_portable_json",
    "validate_csv_file",
    "validate_frame_master_identity",
    "write_data_dictionary",
]
