"""Safe NumPy-only runtime for the disclosed manual-only recovery bundle."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np


FORCE_EXPECTED_FILES = {
    "force_model.json",
    "localization_model.json",
    "metrics.json",
    "preprocessing.json",
    "release_decision.json",
    "replay_demo.jsonl",
}
EVENT_EXPECTED_FILES = {
    "event_model.json",
    "localization_model.json",
    "metrics.json",
    "preprocessing.json",
    "release_decision.json",
    "replay_demo.jsonl",
}
HYBRID_EXPECTED_FILES = FORCE_EXPECTED_FILES | {"event_model.json"}

# ``active_scale`` is the per-ROI 99th-percentile unloaded response stored in
# the reviewed bundle.  Requiring four times that response, and also exceeding
# the current unloaded warm-up by one normalized unit, gives every rod an
# independent, conservative activation gate.  This is deliberately separate
# from the legacy argmax path: argmax can only ever return one rod.
ROI_ACTIVATION_NORMALIZED_FLOOR = 4.0
ROI_ACTIVATION_WARMUP_MARGIN = 1.0
ROI_ACTIVATION_WARMUP_QUANTILE = 0.999
ROI_ACTIVATION_RELEASE_RATIO = 0.60


class ExperimentalBundleError(ValueError):
    """Raised when an experimental model bundle is incomplete or unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ExperimentalBundleError(f"expected a JSON object: {path.name}")
    return value


def _finite_vector(value: object, *, length: int | None = None, positive: bool = False) -> np.ndarray:
    result = np.asarray(value, dtype=float)
    if result.ndim != 1 or (length is not None and len(result) != length):
        raise ExperimentalBundleError("bundle vector has an invalid shape")
    if not np.isfinite(result).all() or (positive and np.any(result <= 0.0)):
        raise ExperimentalBundleError("bundle vector contains an invalid value")
    return result


@dataclass(frozen=True, slots=True)
class ExperimentalBundle:
    directory: Path
    bundle_id: str
    status: str
    sensor_mode: str
    signed_columns: tuple[str, ...]
    active_columns: tuple[str, ...]
    signed_center: np.ndarray
    signed_scale: np.ndarray
    active_center: np.ndarray
    active_scale: np.ndarray
    temporal_window: int
    fallback_threshold: float
    warmup_quantile: float
    minimum_warmup_frames: int
    acquire_frames: int
    clear_frames: int
    roi_switch_frames: int
    localization_confidence_threshold: float
    selective_localization: bool
    force_x: np.ndarray | None
    force_y: np.ndarray | None
    force_min_N: float | None
    force_max_N: float | None
    p95_error_N: float | None
    event_signal_unit: str | None
    required_roi_layout_id: str | None
    required_frame_width: int | None
    required_frame_height: int | None
    required_rotation_degrees: int | None
    required_mirror_horizontal: bool | None
    replay_path: Path
    metrics: Mapping[str, Any]
    release_decision: Mapping[str, Any]


def load_experimental_bundle(directory: Path) -> ExperimentalBundle:
    """Load a reviewed JSON bundle after path, size, and hash verification."""

    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ExperimentalBundleError("bundle path is not a directory")
    manifest_path = root / "manifest.json"
    sums_path = root / "SHA256SUMS"
    if not manifest_path.is_file() or not sums_path.is_file():
        raise ExperimentalBundleError("bundle manifest or checksum list is missing")
    manifest = _json_object(manifest_path)
    files = manifest.get("files")
    sensor_mode = str(manifest.get("sensor_mode", "frame_force"))
    if sensor_mode == "frame_force":
        expected_files = FORCE_EXPECTED_FILES
    elif sensor_mode == "event_signal":
        expected_files = EVENT_EXPECTED_FILES
    elif sensor_mode == "hybrid_force_event":
        expected_files = HYBRID_EXPECTED_FILES
    else:
        raise ExperimentalBundleError("unsupported sensor mode")
    if not isinstance(files, dict) or set(files) != expected_files:
        raise ExperimentalBundleError("bundle file allowlist does not match the manifest")
    if manifest.get("status") != "experimental" or manifest.get("manual_only") is not True:
        raise ExperimentalBundleError("only a disclosed manual-only experimental bundle is accepted")
    if sensor_mode == "hybrid_force_event":
        inference_inputs = manifest.get("inference_inputs")
        expected_inputs = {
            "camera_roi_features": ["signed_delta_v_sum", "active_fraction"],
            "load_cell": "forbidden",
            "printer": "forbidden",
        }
        if inference_inputs != expected_inputs:
            raise ExperimentalBundleError(
                "hybrid bundle must declare camera-only inference inputs"
            )
    for name, record in files.items():
        if Path(name).name != name or name.startswith("."):
            raise ExperimentalBundleError("unsafe path in the bundle manifest")
        path = (root / name).resolve(strict=True)
        if path.parent != root or not path.is_file() or path.is_symlink():
            raise ExperimentalBundleError("bundle file escapes its directory or is not regular")
        if not isinstance(record, dict):
            raise ExperimentalBundleError("invalid bundle file record")
        size = int(record.get("size_bytes", -1))
        if size < 0 or size > 50 * 1024 * 1024 or path.stat().st_size != size:
            raise ExperimentalBundleError(f"bundle file size check failed: {name}")
        if _sha256(path) != str(record.get("sha256", "")):
            raise ExperimentalBundleError(f"bundle file hash check failed: {name}")
    listed = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or Path(name).name != name:
            raise ExperimentalBundleError("invalid SHA256SUMS entry")
        listed[name] = digest
    expected_sum_names = expected_files | {"manifest.json"}
    if set(listed) != expected_sum_names:
        raise ExperimentalBundleError("SHA256SUMS file set is incomplete")
    for name, digest in listed.items():
        if _sha256(root / name) != digest:
            raise ExperimentalBundleError(f"SHA256SUMS verification failed: {name}")

    preprocessing = _json_object(root / "preprocessing.json")
    force = (
        _json_object(root / "force_model.json")
        if sensor_mode in {"frame_force", "hybrid_force_event"}
        else None
    )
    event = (
        _json_object(root / "event_model.json")
        if sensor_mode in {"event_signal", "hybrid_force_event"}
        else None
    )
    localization = _json_object(root / "localization_model.json")
    release = _json_object(root / "release_decision.json")
    metrics = _json_object(root / "metrics.json")
    if release.get("validated_claim_allowed") is not False or release.get("status") != "experimental":
        raise ExperimentalBundleError("release decision does not preserve experimental status")
    if force is not None and force.get("model_type") != "isotonic_piecewise_linear":
        raise ExperimentalBundleError("unsupported force-model type")
    if event is not None and event.get("model_type") != "threshold_normalized_peak":
        raise ExperimentalBundleError("unsupported event-model type")
    localization_type = localization.get("model_type")
    if sensor_mode == "frame_force" and localization_type not in {
        "normalized_active_fraction_argmax",
        "normalized_active_fraction_argmax_switch_debounce",
    }:
        raise ExperimentalBundleError("unsupported force-mode localization model")
    if (
        sensor_mode in {"event_signal", "hybrid_force_event"}
        and localization_type != "accumulated_positive_active_fraction_argmax"
    ):
        raise ExperimentalBundleError("unsupported event-mode localization model")
    feature_order = preprocessing.get("feature_order", {})
    signed_columns = tuple(str(value) for value in feature_order.get("signed", ()))
    active_columns = tuple(str(value) for value in feature_order.get("active", ()))
    expected_signed = tuple(f"roi{roi}_signed_delta_v_sum" for roi in range(1, 10))
    expected_active = tuple(f"roi{roi}_active_fraction" for roi in range(1, 10))
    if signed_columns != expected_signed or active_columns != expected_active:
        raise ExperimentalBundleError("feature order does not match the live extractor")
    signed_center = _finite_vector(preprocessing.get("signed_center"), length=9)
    signed_scale = _finite_vector(preprocessing.get("signed_scale"), length=9, positive=True)
    active_center = _finite_vector(preprocessing.get("active_center"), length=9)
    active_scale = _finite_vector(preprocessing.get("active_scale"), length=9, positive=True)
    temporal = preprocessing.get("temporal_filter", {})
    contact = preprocessing.get("contact", {})
    window = int(temporal.get("window_frames", 0))
    warmup_frames = int(contact.get("minimum_warmup_frames", 0))
    acquire = int(contact.get("acquire_frames", 0))
    clear = int(contact.get("clear_frames", 0))
    if not (1 <= window <= 120 and 30 <= warmup_frames <= 10_000 and 1 <= acquire <= 2 and 1 <= clear <= 2):
        raise ExperimentalBundleError("invalid temporal/contact settings")
    fallback = float(contact.get("fallback_threshold", math.nan))
    warmup_quantile = float(contact.get("unloaded_warmup_quantile", math.nan))
    if not math.isfinite(fallback) or fallback <= 0.0 or not 0.9 <= warmup_quantile < 1.0:
        raise ExperimentalBundleError("invalid contact threshold settings")
    force_x: np.ndarray | None = None
    force_y: np.ndarray | None = None
    force_min: float | None = None
    force_max: float | None = None
    p95_error: float | None = None
    event_signal_unit: str | None = None
    required_roi_layout_id: str | None = None
    required_frame_width: int | None = None
    required_frame_height: int | None = None
    required_rotation_degrees: int | None = None
    required_mirror_horizontal: bool | None = None
    if force is not None:
        force_x = _finite_vector(force.get("x_thresholds"))
        force_y = _finite_vector(force.get("y_thresholds_N"))
        if len(force_x) < 2 or len(force_x) != len(force_y):
            raise ExperimentalBundleError("force model knots are incomplete")
        if np.any(np.diff(force_x) <= 0.0) or np.any(np.diff(force_y) < -1e-9):
            raise ExperimentalBundleError("force model is not monotonic")
        operating = force.get("operating_range_N", {})
        force_min = float(operating.get("minimum", math.nan))
        force_max = float(operating.get("maximum", math.nan))
        p95_error = float(force.get("cross_validated_p95_absolute_error_N", math.nan))
        if not (math.isfinite(force_min) and math.isfinite(force_max) and 0.0 < force_min < force_max <= 3.0):
            raise ExperimentalBundleError("invalid operating range")
        if np.any(force_y < force_min - 1e-9) or np.any(force_y > force_max + 1e-9):
            raise ExperimentalBundleError("force knots leave the operating range")
        if not math.isfinite(p95_error) or p95_error <= 0.0:
            raise ExperimentalBundleError("invalid uncertainty summary")
    if event is not None:
        assert event is not None
        event_signal_unit = str(event.get("output_unit", ""))
        if event_signal_unit != "threshold_ratio":
            raise ExperimentalBundleError("event signal must use threshold_ratio units")
        expected_force_output = (
            "unavailable"
            if sensor_mode == "event_signal"
            else "approximate_isotonic_frame_and_event_peak"
        )
        if event.get("force_output") != expected_force_output:
            raise ExperimentalBundleError("event force-output contract is invalid")
        if (
            event.get("include_acquisition_buffer") is not True
            or event.get("accumulate_only_raw_contact_frames") is not True
        ):
            raise ExperimentalBundleError("event segmentation contract is incomplete")
        required_layout = preprocessing.get("required_layout")
        if not isinstance(required_layout, dict):
            raise ExperimentalBundleError("event bundle is missing its required ROI layout")
        required_roi_layout_id = str(required_layout.get("roi_layout_id", ""))
        required_frame_width = int(required_layout.get("frame_width", 0))
        required_frame_height = int(required_layout.get("frame_height", 0))
        required_rotation_degrees = int(
            required_layout.get("rotation_degrees", -1)
        )
        required_mirror = required_layout.get("mirror_horizontal")
        if (
            not required_roi_layout_id.startswith("roi-")
            or required_frame_width <= 0
            or required_frame_height <= 0
            or required_rotation_degrees not in {0, 90, 180, 270}
            or not isinstance(required_mirror, bool)
        ):
            raise ExperimentalBundleError("event bundle has an invalid required ROI layout")
        required_mirror_horizontal = required_mirror
    roi_switch_frames = int(localization.get("switch_frames", 1))
    localization_threshold = float(
        localization.get("confidence_margin_threshold", 0.5)
    )
    selective_localization = bool(
        localization.get("selective_output_enabled", False)
    )
    if not 1 <= roi_switch_frames <= 2:
        raise ExperimentalBundleError("invalid ROI switch debounce")
    if not math.isfinite(localization_threshold) or localization_threshold < 0.0:
        raise ExperimentalBundleError("invalid localization confidence threshold")
    if sensor_mode in {"event_signal", "hybrid_force_event"} and (
        localization_threshold > 1.0 or not selective_localization
    ):
        raise ExperimentalBundleError("invalid event localization withholding contract")
    return ExperimentalBundle(
        directory=root,
        bundle_id=str(manifest.get("bundle_id", "")),
        status="experimental",
        sensor_mode=sensor_mode,
        signed_columns=signed_columns,
        active_columns=active_columns,
        signed_center=signed_center,
        signed_scale=signed_scale,
        active_center=active_center,
        active_scale=active_scale,
        temporal_window=window,
        fallback_threshold=fallback,
        warmup_quantile=warmup_quantile,
        minimum_warmup_frames=warmup_frames,
        acquire_frames=acquire,
        clear_frames=clear,
        roi_switch_frames=roi_switch_frames,
        localization_confidence_threshold=localization_threshold,
        selective_localization=selective_localization,
        force_x=force_x,
        force_y=force_y,
        force_min_N=force_min,
        force_max_N=force_max,
        p95_error_N=p95_error,
        event_signal_unit=event_signal_unit,
        required_roi_layout_id=required_roi_layout_id,
        required_frame_width=required_frame_width,
        required_frame_height=required_frame_height,
        required_rotation_degrees=required_rotation_degrees,
        required_mirror_horizontal=required_mirror_horizontal,
        replay_path=root / "replay_demo.jsonl",
        metrics=metrics,
        release_decision=release,
    )


@dataclass(frozen=True, slots=True)
class LiveSensorResult:
    frame_id: int
    state: str
    message: str
    contact: bool
    force_N: float | None
    roi: int | None
    contact_score: float | None
    threshold: float | None
    confidence: str
    forced_roi: int | None = None
    experimental: bool = True
    sensor_mode: str = "frame_force"
    event_id: int | None = None
    event_phase: str = "idle"
    current_signal_ratio: float | None = None
    peak_signal_ratio: float | None = None
    peak_force_N: float | None = None
    force_status: str = "unavailable"
    peak_contact_score: float | None = None
    peak_hsv_v: float | None = None
    force_at_peak_hsv_v_N: float | None = None
    peak_delta_v: float | None = None
    force_at_peak_delta_v_N: float | None = None
    event_start_frame_id: int | None = None
    event_end_frame_id: int | None = None
    event_completion_reason: str | None = None
    rois: tuple[int, ...] = ()
    forced_rois: tuple[int, ...] = ()

    @property
    def displayed_rois(self) -> tuple[int, ...]:
        """Return every displayed ROI while preserving old single-ROI results."""

        if self.rois:
            return self.rois
        return (int(self.roi),) if self.roi is not None else ()

    @property
    def all_forced_rois(self) -> tuple[int, ...]:
        """Return every detected ROI while preserving old report compatibility."""

        if self.forced_rois:
            return self.forced_rois
        return (int(self.forced_roi),) if self.forced_roi is not None else ()


class ExperimentalLiveSensorEngine:
    """Stateful event-or-force/ROI inference with mandatory unloaded warm-up."""

    def __init__(self, bundle: ExperimentalBundle) -> None:
        self.bundle = bundle
        self._signed_history: deque[np.ndarray] = deque(maxlen=bundle.temporal_window)
        self._active_history: deque[np.ndarray] = deque(maxlen=bundle.temporal_window)
        self._warmup_scores: list[float] = []
        self._warmup_active: list[np.ndarray] = []
        self._threshold: float | None = None
        self._roi_activation_thresholds = np.full(
            9, ROI_ACTIVATION_NORMALIZED_FLOOR, dtype=float
        )
        self._context_ready = False
        self._context_id = ""
        self._warming_up = False
        self._contact_active = False
        self._positive_count = 0
        self._negative_count = 0
        self._roi_current: int | None = None
        self._roi_candidate: int | None = None
        self._roi_candidate_count = 0
        self._active_roi_mask = np.zeros(9, dtype=bool)
        self._active_roi_acquire_counts = np.zeros(9, dtype=np.int16)
        self._active_roi_clear_counts = np.zeros(9, dtype=np.int16)
        self._pretrigger: deque[
            tuple[int, float, np.ndarray, np.ndarray, np.ndarray, bool]
        ] = deque(
            maxlen=bundle.acquire_frames
        )
        self._event_counter = 0
        self._event_active = False
        self._event_accumulator = np.zeros(9, dtype=float)
        self._event_peak_active = np.zeros(9, dtype=float)
        self._event_peak_signal_ratio: float | None = None
        self._event_peak_force_N: float | None = None
        self._event_force_status = "unavailable"
        self._event_peak_contact_score: float | None = None
        self._event_peak_hsv_v = np.full(9, np.nan, dtype=float)
        self._event_force_at_peak_hsv_v = np.full(9, np.nan, dtype=float)
        self._event_peak_delta_v = np.full(9, np.nan, dtype=float)
        self._event_force_at_peak_delta_v = np.full(9, np.nan, dtype=float)
        self._event_start_frame_id: int | None = None
        self._event_end_frame_id: int | None = None

    @property
    def threshold(self) -> float | None:
        return self._threshold

    @property
    def context_ready(self) -> bool:
        return self._context_ready

    @property
    def roi_activation_thresholds(self) -> tuple[float, ...]:
        """Return the nine independent thresholds learned during warm-up."""

        return tuple(float(value) for value in self._roi_activation_thresholds)

    def set_context_ready(self, ready: bool, context_id: str = "") -> None:
        changed = bool(ready) != self._context_ready or str(context_id) != self._context_id
        self._context_ready = bool(ready)
        self._context_id = str(context_id)
        if changed:
            self.reset(keep_context=True)

    def reset(self, *, keep_context: bool = True) -> None:
        self._signed_history.clear()
        self._active_history.clear()
        self._warmup_scores.clear()
        self._warmup_active.clear()
        self._threshold = None
        self._roi_activation_thresholds.fill(ROI_ACTIVATION_NORMALIZED_FLOOR)
        self._warming_up = False
        self._contact_active = False
        self._positive_count = 0
        self._negative_count = 0
        self._reset_roi_state()
        self._pretrigger.clear()
        self._event_counter = 0
        self._clear_event_state()
        if not keep_context:
            self._context_ready = False
            self._context_id = ""

    def start_unloaded_warmup(self) -> None:
        if not self._context_ready:
            raise RuntimeError("an accepted matching baseline is required before warm-up")
        self.reset(keep_context=True)
        self._warming_up = True

    def _unavailable(self, frame_id: int, state: str, message: str, score: float | None = None) -> LiveSensorResult:
        return LiveSensorResult(
            frame_id=frame_id,
            state=state,
            message=message,
            contact=False,
            force_N=None,
            roi=None,
            contact_score=score,
            threshold=self._threshold,
            confidence="unavailable",
            sensor_mode=self.bundle.sensor_mode,
        )

    def _clear_event_state(self) -> None:
        self._event_active = False
        self._event_accumulator = np.zeros(9, dtype=float)
        self._event_peak_active = np.zeros(9, dtype=float)
        self._event_peak_signal_ratio = None
        self._event_peak_force_N = None
        self._event_force_status = "unavailable"
        self._event_peak_contact_score = None
        self._event_peak_hsv_v = np.full(9, np.nan, dtype=float)
        self._event_force_at_peak_hsv_v = np.full(9, np.nan, dtype=float)
        self._event_peak_delta_v = np.full(9, np.nan, dtype=float)
        self._event_force_at_peak_delta_v = np.full(9, np.nan, dtype=float)
        self._event_start_frame_id = None
        self._event_end_frame_id = None

    def _add_event_sample(
        self,
        frame_id: int,
        score: float,
        active: np.ndarray,
        hsv_v_max: np.ndarray,
        delta_v_max: np.ndarray,
    ) -> None:
        assert self._threshold is not None and self._threshold > 0.0
        positive_active = np.maximum(np.asarray(active, dtype=float), 0.0)
        self._event_accumulator += positive_active
        self._event_peak_active = np.maximum(
            self._event_peak_active, positive_active
        )
        active_ratio = float(
            np.max(positive_active / self._roi_activation_thresholds)
        )
        ratio = max(float(score / self._threshold), active_ratio)
        if self._event_peak_signal_ratio is None:
            self._event_peak_signal_ratio = ratio
        else:
            self._event_peak_signal_ratio = max(
                self._event_peak_signal_ratio, ratio
            )
        if self._event_peak_contact_score is None:
            self._event_peak_contact_score = float(score)
        else:
            self._event_peak_contact_score = max(
                self._event_peak_contact_score, float(score)
            )
        frame_force, frame_force_status = self._estimate_force(score)

        def update_camera_peak(
            values: np.ndarray,
            peak_values: np.ndarray,
            force_values: np.ndarray,
        ) -> None:
            finite = np.isfinite(values)
            replace = finite & (~np.isfinite(peak_values) | (values > peak_values))
            peak_values[replace] = values[replace]
            force_values[replace] = (
                frame_force if frame_force is not None else np.nan
            )

        update_camera_peak(
            np.asarray(hsv_v_max, dtype=float),
            self._event_peak_hsv_v,
            self._event_force_at_peak_hsv_v,
        )
        update_camera_peak(
            np.asarray(delta_v_max, dtype=float),
            self._event_peak_delta_v,
            self._event_force_at_peak_delta_v,
        )
        if self.bundle.sensor_mode == "hybrid_force_event":
            if frame_force_status == "above_range":
                self._event_force_status = "above_range"
                self._event_peak_force_N = None
            elif frame_force is not None and self._event_force_status != "above_range":
                self._event_force_status = "available"
                if self._event_peak_force_N is None:
                    self._event_peak_force_N = frame_force
                else:
                    self._event_peak_force_N = max(
                        self._event_peak_force_N, frame_force
                    )
        if self._event_start_frame_id is None:
            self._event_start_frame_id = int(frame_id)
        self._event_end_frame_id = int(frame_id)

    def _estimate_force(self, score: float) -> tuple[float | None, str]:
        if self.bundle.force_x is None or self.bundle.force_y is None:
            return None, "unavailable"
        if score < self.bundle.force_x[0]:
            return None, "below_range"
        if score > self.bundle.force_x[-1]:
            return None, "above_range"
        return (
            float(np.interp(score, self.bundle.force_x, self.bundle.force_y)),
            "available",
        )

    def _event_roi(
        self, current_rois: tuple[int, ...] | None = None
    ) -> tuple[int | None, int | None, str, tuple[int, ...], tuple[int, ...]]:
        total = float(np.sum(self._event_accumulator))
        if not math.isfinite(total) or total <= 0.0:
            return None, None, "unavailable", (), ()
        ordered = np.sort(self._event_accumulator)
        margin = float((ordered[-1] - ordered[-2]) / total)
        forced_roi = int(np.argmax(self._event_accumulator)) + 1
        confident = margin >= self.bundle.localization_confidence_threshold
        threshold_rois = self._thresholded_rois(self._event_peak_active)
        detected_rois = threshold_rois if current_rois is None else current_rois
        if detected_rois:
            display_rois = detected_rois
            forced_rois = detected_rois
            display_roi = (
                forced_roi if forced_roi in detected_rois else detected_rois[0]
            )
            confidence = "multi" if len(detected_rois) > 1 else "higher"
        else:
            display_roi = (
                forced_roi
                if confident or not self.bundle.selective_localization
                else None
            )
            display_rois = (display_roi,) if display_roi is not None else ()
            forced_rois = (forced_roi,)
            confidence = "higher" if confident else "tentative"
        return display_roi, forced_roi, confidence, display_rois, forced_rois

    def _thresholded_rois(self, active: np.ndarray) -> tuple[int, ...]:
        """Classify rods independently so simultaneous presses are representable."""

        values = np.asarray(active, dtype=float)
        if values.shape != (9,) or not np.isfinite(values).all():
            return ()
        return tuple(
            int(index + 1)
            for index in np.flatnonzero(values >= self._roi_activation_thresholds)
        )

    def _update_active_roi_state(self, active: np.ndarray) -> tuple[int, ...]:
        """Apply per-rod acquisition/release hysteresis to normalized activity."""

        values = np.maximum(np.asarray(active, dtype=float), 0.0)
        acquire = values >= self._roi_activation_thresholds
        release = values <= (
            self._roi_activation_thresholds * ROI_ACTIVATION_RELEASE_RATIO
        )
        for index in range(9):
            if self._active_roi_mask[index]:
                self._active_roi_acquire_counts[index] = 0
                if release[index]:
                    self._active_roi_clear_counts[index] += 1
                    if self._active_roi_clear_counts[index] >= self.bundle.clear_frames:
                        self._active_roi_mask[index] = False
                        self._active_roi_clear_counts[index] = 0
                else:
                    self._active_roi_clear_counts[index] = 0
            else:
                self._active_roi_clear_counts[index] = 0
                if acquire[index]:
                    self._active_roi_acquire_counts[index] += 1
                    if self._active_roi_acquire_counts[index] >= self.bundle.acquire_frames:
                        self._active_roi_mask[index] = True
                        self._active_roi_acquire_counts[index] = 0
                else:
                    self._active_roi_acquire_counts[index] = 0
        return tuple(int(index + 1) for index in np.flatnonzero(self._active_roi_mask))

    def _active_contact_ratio(self, active: np.ndarray) -> float:
        values = np.maximum(np.asarray(active, dtype=float), 0.0)
        return float(np.max(values / self._roi_activation_thresholds))

    @staticmethod
    def _selected_optional(values: np.ndarray, roi: int | None) -> float | None:
        if roi is None:
            return None
        value = float(values[int(roi) - 1])
        return value if math.isfinite(value) else None

    def _process_event_signal(
        self,
        frame_id: int,
        score: float,
        filtered_active: np.ndarray,
        hsv_v_max: np.ndarray,
        delta_v_max: np.ndarray,
    ) -> LiveSensorResult:
        assert self._threshold is not None and self._threshold > 0.0
        hybrid = self.bundle.sensor_mode == "hybrid_force_event"
        active_contact_ratio = self._active_contact_ratio(filtered_active)
        raw_contact = (
            score >= self._threshold or active_contact_ratio >= 1.0
        )
        current_rois = (
            self._update_active_roi_state(filtered_active)
            if raw_contact or self._contact_active
            else ()
        )
        self._pretrigger.append(
            (
                frame_id,
                score,
                np.asarray(filtered_active, dtype=float).copy(),
                np.asarray(hsv_v_max, dtype=float).copy(),
                np.asarray(delta_v_max, dtype=float).copy(),
                raw_contact,
            )
        )
        was_active = self._contact_active
        if raw_contact:
            self._positive_count += 1
            self._negative_count = 0
            if not self._contact_active and self._positive_count >= self.bundle.acquire_frames:
                self._contact_active = True
        else:
            self._negative_count += 1
            self._positive_count = 0
            if self._contact_active and self._negative_count >= self.bundle.clear_frames:
                self._contact_active = False

        entered = not was_active and self._contact_active
        exited = was_active and not self._contact_active
        if entered:
            self._event_counter += 1
            self._clear_event_state()
            self._event_active = True
            trigger_frames: list[
                tuple[int, float, np.ndarray, np.ndarray, np.ndarray, bool]
            ] = []
            for sample in reversed(self._pretrigger):
                if not sample[5]:
                    break
                trigger_frames.append(sample)
            for (
                trigger_frame,
                trigger_score,
                trigger_active,
                trigger_hsv_v,
                trigger_delta_v,
                _,
            ) in reversed(trigger_frames):
                self._add_event_sample(
                    trigger_frame,
                    trigger_score,
                    trigger_active,
                    trigger_hsv_v,
                    trigger_delta_v,
                )
        elif self._contact_active and raw_contact:
            self._add_event_sample(
                frame_id,
                score,
                filtered_active,
                hsv_v_max,
                delta_v_max,
            )

        if exited:
            (
                display_roi,
                forced_roi,
                confidence,
                display_rois,
                forced_rois,
            ) = self._event_roi()
            multi_press = len(display_rois) > 1
            peak_force = (
                self._event_peak_force_N if hybrid and not multi_press else None
            )
            force_status = (
                "multi_press_unavailable"
                if hybrid and multi_press
                else (self._event_force_status if hybrid else "unavailable")
            )
            if hybrid:
                force_message = (
                    "Force is withheld because the single-press model cannot separate simultaneous rods."
                    if multi_press
                    else "Approximate peak force shown; it is not physically validated."
                    if peak_force is not None
                    else "Approximate force is unavailable because the optical response left its calibrated support."
                )
            else:
                force_message = (
                    "Newton-valued force is unavailable because no event-level calibration exists."
                )
            result = LiveSensorResult(
                frame_id=frame_id,
                state=(
                    "EVENT_COMPLETE"
                    if display_roi is not None
                    else "EVENT_COMPLETE_UNCERTAIN_ROI"
                ),
                message=(
                    f"Optical event complete; {force_message}"
                    if display_roi is not None
                    else f"Optical event complete; ROI withheld. {force_message}"
                ),
                contact=False,
                force_N=None,
                roi=display_roi,
                contact_score=score,
                threshold=self._threshold,
                confidence=confidence,
                forced_roi=forced_roi,
                rois=display_rois,
                forced_rois=forced_rois,
                sensor_mode=self.bundle.sensor_mode,
                event_id=self._event_counter,
                event_phase="completed",
                current_signal_ratio=float(score / self._threshold),
                peak_signal_ratio=self._event_peak_signal_ratio,
                peak_force_N=peak_force,
                force_status=force_status,
                peak_contact_score=self._event_peak_contact_score,
                peak_hsv_v=self._selected_optional(
                    self._event_peak_hsv_v, forced_roi
                ),
                force_at_peak_hsv_v_N=(
                    None
                    if multi_press
                    else self._selected_optional(
                        self._event_force_at_peak_hsv_v, forced_roi
                    )
                ),
                peak_delta_v=self._selected_optional(
                    self._event_peak_delta_v, forced_roi
                ),
                force_at_peak_delta_v_N=(
                    None
                    if multi_press
                    else self._selected_optional(
                        self._event_force_at_peak_delta_v, forced_roi
                    )
                ),
                event_start_frame_id=self._event_start_frame_id,
                event_end_frame_id=self._event_end_frame_id,
                event_completion_reason="contact_cleared",
            )
            self._clear_event_state()
            self._reset_roi_state()
            return result

        if not self._contact_active:
            if not raw_contact:
                self._reset_roi_state()
            return LiveSensorResult(
                frame_id=frame_id,
                state="NO_CONTACT",
                message=(
                    "No contact detected; approximate force is 0 N."
                    if hybrid
                    else "No contact detected; Newton-valued force is unavailable."
                ),
                contact=False,
                force_N=0.0 if hybrid else None,
                roi=None,
                contact_score=score,
                threshold=self._threshold,
                confidence="no_contact",
                sensor_mode=self.bundle.sensor_mode,
                force_status="no_contact" if hybrid else "unavailable",
                event_phase="idle",
            )

        (
            display_roi,
            forced_roi,
            confidence,
            display_rois,
            forced_rois,
        ) = self._event_roi(current_rois)
        multi_press = len(display_rois) > 1
        current_force, force_status = (
            self._estimate_force(score) if hybrid else (None, "unavailable")
        )
        if hybrid and multi_press:
            current_force = None
            force_status = "multi_press_unavailable"
        if hybrid:
            force_message = (
                "force is withheld because simultaneous rods are active."
                if multi_press
                else "showing an approximate force inside the narrow experimental support."
                if current_force is not None
                else "approximate force is unavailable outside the narrow experimental support."
            )
        else:
            force_message = (
                "showing a unitless signal relative to the unloaded threshold."
            )
        return LiveSensorResult(
            frame_id=frame_id,
            state=(
                "EVENT_ACTIVE"
                if display_roi is not None
                else "EVENT_ACTIVE_UNCERTAIN_ROI"
            ),
            message=(
                f"Optical event active; {force_message}"
                if display_roi is not None
                else f"Optical event active; ROI is withheld while accumulated evidence is uncertain; {force_message}"
            ),
            contact=True,
            force_N=current_force,
            roi=display_roi,
            contact_score=score,
            threshold=self._threshold,
            confidence=confidence,
            forced_roi=forced_roi,
            rois=display_rois,
            forced_rois=forced_rois,
            sensor_mode=self.bundle.sensor_mode,
            event_id=self._event_counter,
            event_phase="active",
            current_signal_ratio=max(
                float(score / self._threshold), active_contact_ratio
            ),
            peak_signal_ratio=self._event_peak_signal_ratio,
            peak_force_N=(
                self._event_peak_force_N
                if hybrid and not multi_press
                else None
            ),
            force_status=force_status,
            peak_contact_score=self._event_peak_contact_score,
            peak_hsv_v=self._selected_optional(
                self._event_peak_hsv_v, forced_roi
            ),
            force_at_peak_hsv_v_N=(
                None
                if multi_press
                else self._selected_optional(
                    self._event_force_at_peak_hsv_v, forced_roi
                )
            ),
            peak_delta_v=self._selected_optional(
                self._event_peak_delta_v, forced_roi
            ),
            force_at_peak_delta_v_N=(
                None
                if multi_press
                else self._selected_optional(
                    self._event_force_at_peak_delta_v, forced_roi
                )
            ),
            event_start_frame_id=self._event_start_frame_id,
            event_end_frame_id=self._event_end_frame_id,
        )

    def _reset_roi_state(self) -> None:
        self._roi_current = None
        self._roi_candidate = None
        self._roi_candidate_count = 0
        self._reset_active_roi_state()

    def _reset_active_roi_state(self) -> None:
        self._active_roi_mask.fill(False)
        self._active_roi_acquire_counts.fill(0)
        self._active_roi_clear_counts.fill(0)

    def _update_roi_state(self, candidate: int) -> int:
        if self._roi_current is None:
            self._roi_current = int(candidate)
        elif candidate == self._roi_current:
            self._roi_candidate = None
            self._roi_candidate_count = 0
        elif candidate == self._roi_candidate:
            self._roi_candidate_count += 1
            if self._roi_candidate_count >= self.bundle.roi_switch_frames:
                self._roi_current = int(candidate)
                self._roi_candidate = None
                self._roi_candidate_count = 0
        else:
            self._roi_candidate = int(candidate)
            self._roi_candidate_count = 1
        return int(self._roi_current)

    def process(self, row: Mapping[str, object]) -> LiveSensorResult:
        frame_id = int(row.get("capture_frame_id", -1))
        if not self._context_ready:
            return self._unavailable(frame_id, "SETUP_BLOCKED", "Accept an unloaded baseline first.")
        try:
            signed = np.asarray([float(row[column]) for column in self.bundle.signed_columns])
            active = np.asarray([float(row[column]) for column in self.bundle.active_columns])
        except (KeyError, TypeError, ValueError):
            return self._unavailable(frame_id, "ERROR", "Required optical features are unavailable.")
        if not np.isfinite(signed).all() or not np.isfinite(active).all():
            return self._unavailable(frame_id, "ERROR", "Optical features are non-finite.")
        signed = (signed - self.bundle.signed_center) / self.bundle.signed_scale
        active = (active - self.bundle.active_center) / self.bundle.active_scale
        self._signed_history.append(signed)
        self._active_history.append(active)
        filtered_signed = np.mean(np.vstack(self._signed_history), axis=0)
        filtered_active = np.mean(np.vstack(self._active_history), axis=0)
        score = float(np.ptp(filtered_signed))
        def optional_float(key: str) -> float:
            try:
                value = float(row.get(key, math.nan))
            except (TypeError, ValueError):
                return math.nan
            return value if math.isfinite(value) else math.nan

        hsv_v_max = np.asarray(
            [optional_float(f"roi{roi}_max_v") for roi in range(1, 10)],
            dtype=float,
        )
        delta_v_max = np.asarray(
            [optional_float(f"roi{roi}_delta_v_max") for roi in range(1, 10)],
            dtype=float,
        )
        if self._warming_up:
            self._warmup_scores.append(score)
            self._warmup_active.append(filtered_active.copy())
            count = len(self._warmup_scores)
            if count >= self.bundle.minimum_warmup_frames:
                calibrated = float(
                    np.quantile(
                        np.asarray(self._warmup_scores),
                        self.bundle.warmup_quantile,
                        method="higher",
                    )
                )
                self._threshold = max(self.bundle.fallback_threshold, calibrated)
                warmup_active = np.vstack(self._warmup_active)
                warmup_high = np.quantile(
                    warmup_active,
                    ROI_ACTIVATION_WARMUP_QUANTILE,
                    axis=0,
                    method="higher",
                )
                self._roi_activation_thresholds = np.maximum(
                    ROI_ACTIVATION_NORMALIZED_FLOOR,
                    warmup_high + ROI_ACTIVATION_WARMUP_MARGIN,
                )
                self._warming_up = False
                return self._unavailable(
                    frame_id,
                    "READY",
                    f"Unloaded warm-up complete ({count} frames).",
                    score,
                )
            return self._unavailable(
                frame_id,
                "WARMING_UP",
                f"Keep the sensor unloaded: {count}/{self.bundle.minimum_warmup_frames} frames.",
                score,
            )
        if self._threshold is None:
            return self._unavailable(frame_id, "SETUP_BLOCKED", "Run unloaded threshold warm-up.", score)
        if self.bundle.sensor_mode in {"event_signal", "hybrid_force_event"}:
            return self._process_event_signal(
                frame_id,
                score,
                filtered_active,
                hsv_v_max,
                delta_v_max,
            )
        active_contact_ratio = self._active_contact_ratio(filtered_active)
        raw_contact = (
            score >= self._threshold or active_contact_ratio >= 1.0
        )
        current_rois = (
            self._update_active_roi_state(filtered_active)
            if raw_contact or self._contact_active
            else ()
        )
        if raw_contact:
            self._positive_count += 1
            self._negative_count = 0
            if not self._contact_active and self._positive_count >= self.bundle.acquire_frames:
                self._contact_active = True
        else:
            self._negative_count += 1
            self._positive_count = 0
            if self._contact_active and self._negative_count >= self.bundle.clear_frames:
                self._contact_active = False
        if not self._contact_active:
            if not raw_contact:
                self._reset_roi_state()
            return LiveSensorResult(
                frame_id=frame_id,
                state="NO_CONTACT",
                message="No contact detected.",
                contact=False,
                force_N=0.0,
                roi=None,
                contact_score=score,
                threshold=self._threshold,
                confidence="no_contact",
                sensor_mode="frame_force",
            )
        assert self.bundle.force_x is not None
        assert self.bundle.force_y is not None
        assert self.bundle.force_min_N is not None
        assert self.bundle.force_max_N is not None
        assert self.bundle.p95_error_N is not None
        raw_roi = int(np.argmax(filtered_active)) + 1
        forced_roi = self._update_roi_state(raw_roi)
        if current_rois:
            display_rois = current_rois
            forced_rois = current_rois
            display_roi = (
                forced_roi if forced_roi in current_rois else current_rois[0]
            )
            confidence = "multi" if len(current_rois) > 1 else "higher"
        else:
            ordered = np.sort(filtered_active)
            margin = float(ordered[-1] - ordered[-2])
            confident = margin >= self.bundle.localization_confidence_threshold
            confidence = "higher" if confident else "tentative"
            display_roi = (
                forced_roi
                if confident or not self.bundle.selective_localization
                else None
            )
            display_rois = (display_roi,) if display_roi is not None else ()
            forced_rois = (forced_roi,)
        multi_press = len(display_rois) > 1
        if multi_press:
            force = None
            force_status = "multi_press_unavailable"
            message = (
                "Simultaneous rods detected; localization is shown, but the "
                "single-press force model cannot separate their forces."
            )
        elif score < self.bundle.force_x[0]:
            force = None
            force_status = "below_range"
            message = (
                f"Contact detected below the {self.bundle.force_min_N:.1f} N "
                "experimental range."
            )
        elif score > self.bundle.force_x[-1]:
            force = None
            force_status = "above_range"
            message = (
                f"Optical response exceeds the {self.bundle.force_max_N:.1f} N "
                "experimental range."
            )
        else:
            force = float(np.interp(score, self.bundle.force_x, self.bundle.force_y))
            force_status = "available"
            message = (
                "Approximate force; cross-validated p95 error was about "
                f"±{self.bundle.p95_error_N:.2f} N."
                if display_roi is not None
                else "Approximate force available; ROI withheld because "
                "localization confidence is low."
            )
        state = (
            "EXPERIMENTAL_CONTACT"
            if display_roi is not None
            else "EXPERIMENTAL_CONTACT_UNCERTAIN_ROI"
        )
        return LiveSensorResult(
            frame_id=frame_id,
            state=state,
            message=message,
            contact=True,
            force_N=force,
            roi=display_roi,
            contact_score=score,
            threshold=self._threshold,
            confidence=confidence,
            forced_roi=forced_roi,
            rois=display_rois,
            forced_rois=forced_rois,
            sensor_mode="frame_force",
            force_status=force_status,
        )


__all__ = [
    "ExperimentalBundle",
    "ExperimentalBundleError",
    "ExperimentalLiveSensorEngine",
    "LiveSensorResult",
    "ROI_ACTIVATION_NORMALIZED_FLOOR",
    "ROI_ACTIVATION_RELEASE_RATIO",
    "ROI_ACTIVATION_WARMUP_MARGIN",
    "ROI_ACTIVATION_WARMUP_QUANTILE",
    "load_experimental_bundle",
]
