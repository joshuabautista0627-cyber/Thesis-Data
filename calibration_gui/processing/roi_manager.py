"""Nine-ROI geometry, validation, change tracking, and JSON persistence.

Coordinates use the full-frame OpenCV convention: the origin is the upper-left
pixel, ``x`` increases rightward, and ``y`` increases downward.  A rectangle's
right and bottom coordinates are exclusive, matching NumPy slicing.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from core.models import ROI
from processing.camera_orientation import validate_camera_orientation


ROI_COUNT = 9
ROI_LAYOUT_SCHEMA_VERSION = "1.0"


class ROIValidationError(ValueError):
    """Raised when a layout cannot represent the required nine valid ROIs."""


class Invalidatable(Protocol):
    """Minimal protocol used to invalidate a baseline after a layout change."""

    def invalidate(self, reason: str) -> None:
        """Mark the object invalid and retain a human-readable reason."""


@dataclass(frozen=True, slots=True)
class ROIValidationResult:
    """Complete ROI validation outcome.

    Overlaps are warnings rather than boundary errors, as required by the
    specification.  ``valid`` therefore remains true when overlaps are the only
    finding.
    """

    errors: tuple[str, ...]
    overlap_pairs: tuple[tuple[int, int], ...]

    @property
    def valid(self) -> bool:
        """Return whether all nine rectangles have valid identities and bounds."""

        return not self.errors

    @property
    def warnings(self) -> tuple[str, ...]:
        """Return stable, display-ready overlap warnings."""

        return tuple(
            f"ROI {first} overlaps ROI {second}"
            for first, second in self.overlap_pairs
        )

    def raise_for_errors(self) -> None:
        """Raise :class:`ROIValidationError` when hard validation failed."""

        if self.errors:
            raise ROIValidationError("; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class ROILayout:
    """A validated, ordered nine-ROI layout tied to one frame size."""

    frame_width: int
    frame_height: int
    rois: tuple[ROI, ...]
    roi_layout_id: str
    rotation_degrees: int | None = None
    mirror_horizontal: bool | None = None

    @classmethod
    def create(
        cls,
        rois: Iterable[ROI],
        frame_width: int,
        frame_height: int,
        *,
        rotation_degrees: int | None = None,
        mirror_horizontal: bool | None = None,
    ) -> "ROILayout":
        """Validate and normalize a collection into ROI 1-through-9 order."""

        ordered = order_rois(rois)
        validation = validate_rois(ordered, frame_width, frame_height)
        validation.raise_for_errors()
        if (rotation_degrees is None) != (mirror_horizontal is None):
            raise ROIValidationError(
                "ROI camera orientation requires both rotation and mirror values"
            )
        if rotation_degrees is not None and mirror_horizontal is not None:
            try:
                rotation_degrees, mirror_horizontal = validate_camera_orientation(
                    rotation_degrees,
                    mirror_horizontal,
                )
            except (TypeError, ValueError) as exc:
                raise ROIValidationError(f"invalid ROI camera orientation: {exc}") from exc
        return cls(
            frame_width=int(frame_width),
            frame_height=int(frame_height),
            rois=ordered,
            roi_layout_id=roi_layout_fingerprint(
                ordered, frame_width=frame_width, frame_height=frame_height
            ),
            rotation_degrees=rotation_degrees,
            mirror_horizontal=mirror_horizontal,
        )

    @property
    def validation(self) -> ROIValidationResult:
        """Return current bounds and overlap results."""

        return validate_rois(self.rois, self.frame_width, self.frame_height)

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation, including derived geometry."""

        payload: dict[str, object] = {
            "schema_version": ROI_LAYOUT_SCHEMA_VERSION,
            "roi_layout_id": self.roi_layout_id,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "roi_order": [roi.roi_id for roi in self.rois],
            "rois": [roi_to_dict(roi) for roi in self.rois],
        }
        if self.rotation_degrees is not None and self.mirror_horizontal is not None:
            payload["camera_orientation"] = {
                "rotation_degrees": self.rotation_degrees,
                "mirror_horizontal": self.mirror_horizontal,
            }
        return payload


def order_rois(rois: Iterable[ROI]) -> tuple[ROI, ...]:
    """Return exactly nine ROIs in numeric ROI 1-through-9 order.

    The function rejects duplicates and missing identities instead of silently
    relabeling rectangles, preserving the scientific meaning of ROI columns.
    """

    materialized = tuple(rois)
    if len(materialized) != ROI_COUNT:
        raise ROIValidationError(
            f"exactly {ROI_COUNT} ROIs are required; received {len(materialized)}"
        )
    ids = [roi.roi_id for roi in materialized]
    expected = list(range(1, ROI_COUNT + 1))
    if sorted(ids) != expected:
        raise ROIValidationError(
            f"ROI identities must be exactly {expected}; received {ids}"
        )
    return tuple(sorted(materialized, key=lambda roi: roi.roi_id))


def validate_rois(
    rois: Sequence[ROI] | Iterable[ROI],
    frame_width: int,
    frame_height: int,
) -> ROIValidationResult:
    """Validate identities, positive dimensions, and full-frame bounds.

    Rectangles that merely touch at an edge or corner do not overlap.  Actual
    overlap is reported separately as a warning and does not make the layout
    invalid.
    """

    materialized = tuple(rois)
    errors: list[str] = []
    if isinstance(frame_width, bool) or not isinstance(frame_width, int):
        errors.append("frame_width must be an integer")
    elif frame_width <= 0:
        errors.append("frame_width must be positive")
    if isinstance(frame_height, bool) or not isinstance(frame_height, int):
        errors.append("frame_height must be an integer")
    elif frame_height <= 0:
        errors.append("frame_height must be positive")

    if len(materialized) != ROI_COUNT:
        errors.append(
            f"exactly {ROI_COUNT} ROIs are required; received {len(materialized)}"
        )

    ids = [roi.roi_id for roi in materialized]
    expected_ids = list(range(1, ROI_COUNT + 1))
    if len(materialized) == ROI_COUNT and sorted(ids) != expected_ids:
        errors.append(
            f"ROI identities must be exactly {expected_ids}; received {ids}"
        )

    dimensions_are_valid = (
        isinstance(frame_width, int)
        and not isinstance(frame_width, bool)
        and frame_width > 0
        and isinstance(frame_height, int)
        and not isinstance(frame_height, bool)
        and frame_height > 0
    )
    for roi in materialized:
        if roi.width <= 0 or roi.height <= 0:
            errors.append(f"ROI {roi.roi_id} width and height must be positive")
            continue
        if roi.x < 0 or roi.y < 0:
            errors.append(f"ROI {roi.roi_id} starts outside the frame")
        if dimensions_are_valid and (
            roi.x + roi.width > frame_width or roi.y + roi.height > frame_height
        ):
            errors.append(f"ROI {roi.roi_id} extends outside the frame")

    return ROIValidationResult(
        errors=tuple(errors), overlap_pairs=find_roi_overlaps(materialized)
    )


def rectangles_overlap(first: ROI, second: ROI) -> bool:
    """Return true only when two rectangles share a positive-area region."""

    return (
        max(first.x, second.x) < min(first.x + first.width, second.x + second.width)
        and max(first.y, second.y)
        < min(first.y + first.height, second.y + second.height)
    )


def find_roi_overlaps(rois: Iterable[ROI]) -> tuple[tuple[int, int], ...]:
    """Return every overlapping ROI-ID pair in deterministic numeric order."""

    ordered = tuple(sorted(rois, key=lambda roi: roi.roi_id))
    return tuple(
        (first.roi_id, second.roi_id)
        for index, first in enumerate(ordered)
        for second in ordered[index + 1 :]
        if rectangles_overlap(first, second)
    )


def copy_roi1_size(rois: Iterable[ROI]) -> tuple[ROI, ...]:
    """Copy ROI 1 width and height to ROI 2-through-9 without moving them."""

    ordered = order_rois(rois)
    first = ordered[0]
    return tuple(
        roi
        if roi.roi_id == 1
        else ROI(
            roi_id=roi.roi_id,
            x=roi.x,
            y=roi.y,
            width=first.width,
            height=first.height,
        )
        for roi in ordered
    )


def roi_layout_fingerprint(
    rois: Iterable[ROI], frame_width: int, frame_height: int
) -> str:
    """Return a deterministic identifier for geometry and frame dimensions."""

    ordered = order_rois(rois)
    canonical = {
        "frame_width": int(frame_width),
        "frame_height": int(frame_height),
        "rois": [
            [roi.roi_id, roi.x, roi.y, roi.width, roi.height] for roi in ordered
        ],
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return f"roi-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def roi_to_dict(roi: ROI) -> dict[str, int | float]:
    """Serialize required raw and derived ROI geometry."""

    return {
        "roi_id": roi.roi_id,
        "row": (roi.roi_id - 1) // 3 + 1,
        "column": (roi.roi_id - 1) % 3 + 1,
        "x": roi.x,
        "y": roi.y,
        "width": roi.width,
        "height": roi.height,
        "area": roi.area,
        "center_x": roi.center_x,
        "center_y": roi.center_y,
    }


def _roi_from_mapping(data: Mapping[str, object]) -> ROI:
    required = ("roi_id", "x", "y", "width", "height")
    missing = [name for name in required if name not in data]
    if missing:
        raise ROIValidationError(f"ROI JSON is missing fields: {', '.join(missing)}")
    try:
        values = {name: int(data[name]) for name in required}
    except (TypeError, ValueError) as exc:
        raise ROIValidationError("ROI coordinates and identities must be integers") from exc
    for name in required:
        raw = data[name]
        if isinstance(raw, bool) or isinstance(raw, float) and not raw.is_integer():
            raise ROIValidationError(f"ROI field {name} must be an integer")
    return ROI(**values)


def save_roi_layout(path: str | Path, layout: ROILayout) -> Path:
    """Atomically save a validated layout as UTF-8 JSON."""

    validation = layout.validation
    validation.raise_for_errors()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(layout.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def load_roi_layout(path: str | Path) -> ROILayout:
    """Load, validate, order, and integrity-check a saved ROI layout."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ROIValidationError(f"could not load ROI layout {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ROIValidationError("ROI layout JSON root must be an object")
    if payload.get("schema_version") != ROI_LAYOUT_SCHEMA_VERSION:
        raise ROIValidationError(
            f"unsupported ROI layout schema version: {payload.get('schema_version')!r}"
        )
    try:
        frame_width = int(payload["frame_width"])
        frame_height = int(payload["frame_height"])
        raw_rois = payload["rois"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ROIValidationError("ROI layout is missing valid frame dimensions") from exc
    if not isinstance(raw_rois, list):
        raise ROIValidationError("ROI layout 'rois' must be a list")
    rois = tuple(
        _roi_from_mapping(item)
        if isinstance(item, dict)
        else (_raise_invalid_roi_item())
        for item in raw_rois
    )
    rotation_degrees: int | None = None
    mirror_horizontal: bool | None = None
    raw_orientation = payload.get("camera_orientation")
    if raw_orientation is not None:
        if not isinstance(raw_orientation, dict):
            raise ROIValidationError("ROI layout camera_orientation must be an object")
        try:
            rotation_degrees = int(raw_orientation["rotation_degrees"])
            mirror_horizontal = raw_orientation["mirror_horizontal"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ROIValidationError(
                "ROI layout camera_orientation is missing valid rotation and mirror values"
            ) from exc
        if not isinstance(mirror_horizontal, bool):
            raise ROIValidationError(
                "ROI layout camera_orientation mirror_horizontal must be a boolean"
            )
    layout = ROILayout.create(
        rois,
        frame_width,
        frame_height,
        rotation_degrees=rotation_degrees,
        mirror_horizontal=mirror_horizontal,
    )
    saved_id = payload.get("roi_layout_id")
    if saved_id != layout.roi_layout_id:
        raise ROIValidationError(
            "ROI layout identifier does not match its frame dimensions and geometry"
        )
    return layout


def _raise_invalid_roi_item() -> ROI:
    raise ROIValidationError("every ROI layout item must be an object")


class ROIManager:
    """Own one editable layout and notify dependents after real geometry changes."""

    def __init__(self, layout: ROILayout) -> None:
        self._layout = layout
        self._listeners: list[Callable[[str], None]] = []

    @property
    def layout(self) -> ROILayout:
        """Return the current immutable layout value."""

        return self._layout

    @property
    def rois(self) -> tuple[ROI, ...]:
        """Return current rectangles in canonical ROI 1-through-9 order."""

        return self._layout.rois

    def add_change_listener(self, listener: Callable[[str], None]) -> None:
        """Register a callback invoked only after a successful layout change."""

        if listener not in self._listeners:
            self._listeners.append(listener)

    def bind_baseline(self, baseline: Invalidatable) -> None:
        """Invalidate ``baseline`` automatically whenever the ROI layout changes."""

        self.add_change_listener(baseline.invalidate)

    def set_layout(self, layout: ROILayout) -> None:
        """Replace the layout transactionally and notify on an actual change."""

        if layout == self._layout:
            return
        self._layout = layout
        self._notify("ROI layout changed")

    def update_roi(self, roi_id: int, *, x: int, y: int, width: int, height: int) -> None:
        """Replace one rectangle, rejecting an invalid resulting layout."""

        if roi_id not in range(1, ROI_COUNT + 1):
            raise ROIValidationError("roi_id must be between 1 and 9")
        candidate = tuple(
            ROI(roi_id=roi.roi_id, x=x, y=y, width=width, height=height)
            if roi.roi_id == roi_id
            else roi
            for roi in self.rois
        )
        self.set_layout(
            ROILayout.create(
                candidate, self._layout.frame_width, self._layout.frame_height
            )
        )

    def copy_roi1_size(self) -> None:
        """Apply ROI 1 dimensions to all rectangles transactionally."""

        candidate = copy_roi1_size(self.rois)
        self.set_layout(
            ROILayout.create(
                candidate, self._layout.frame_width, self._layout.frame_height
            )
        )

    def save(self, path: str | Path) -> Path:
        """Save the current layout."""

        return save_roi_layout(path, self._layout)

    def load(self, path: str | Path) -> None:
        """Load and apply a layout, notifying bound baselines when it changed."""

        self.set_layout(load_roi_layout(path))

    def _notify(self, reason: str) -> None:
        for listener in tuple(self._listeners):
            listener(reason)


__all__ = [
    "ROI",
    "ROI_COUNT",
    "ROILayout",
    "ROIManager",
    "ROIValidationError",
    "ROIValidationResult",
    "copy_roi1_size",
    "find_roi_overlaps",
    "load_roi_layout",
    "order_rois",
    "rectangles_overlap",
    "roi_layout_fingerprint",
    "roi_to_dict",
    "save_roi_layout",
    "validate_rois",
]
