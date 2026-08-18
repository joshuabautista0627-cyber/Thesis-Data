"""Frozen configuration, layout, and runtime-state contracts for live sensing.

This module is deliberately independent of model libraries.  Analysis commands
and the GUI use the same fail-closed validation path before touching source
archives or publishing inference output.
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from processing.roi_manager import ROILayout, load_roi_layout


LIVE_LAYOUT_ID = "roi-9fbc67c50ee3bc7145fa"
RAW_FRAME_SIZE = (640, 480)
ORIENTED_FRAME_SIZE = (480, 640)
LIVE_ROTATION_DEGREES = 90
LIVE_MIRROR_HORIZONTAL = True


class ContractError(ValueError):
    """Raised when a frozen live-sensor input is missing or incompatible."""


class LiveSensorState(StrEnum):
    """Scientific runtime states; recording remains an orthogonal flag."""

    STARTUP = "STARTUP"
    SETUP_BLOCKED = "SETUP_BLOCKED"
    BASELINE_CAPTURING = "BASELINE_CAPTURING"
    READY = "READY"
    LIVE_NO_CONTACT = "LIVE_NO_CONTACT"
    LIVE_CONTACT = "LIVE_CONTACT"
    LIVE_UNCERTAIN = "LIVE_UNCERTAIN"
    LIVE_LIMIT = "LIVE_LIMIT"
    PAUSED = "PAUSED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


_TRANSITIONS: Mapping[LiveSensorState, frozenset[LiveSensorState]] = MappingProxyType(
    {
        LiveSensorState.STARTUP: frozenset(
            {LiveSensorState.SETUP_BLOCKED, LiveSensorState.ERROR}
        ),
        LiveSensorState.SETUP_BLOCKED: frozenset(
            {
                LiveSensorState.BASELINE_CAPTURING,
                LiveSensorState.RECONNECTING,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.BASELINE_CAPTURING: frozenset(
            {
                LiveSensorState.READY,
                LiveSensorState.SETUP_BLOCKED,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.READY: frozenset(
            {
                LiveSensorState.LIVE_NO_CONTACT,
                LiveSensorState.LIVE_CONTACT,
                LiveSensorState.PAUSED,
                LiveSensorState.RECONNECTING,
                LiveSensorState.SETUP_BLOCKED,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.LIVE_NO_CONTACT: frozenset(
            {
                LiveSensorState.LIVE_CONTACT,
                LiveSensorState.PAUSED,
                LiveSensorState.RECONNECTING,
                LiveSensorState.SETUP_BLOCKED,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.LIVE_CONTACT: frozenset(
            {
                LiveSensorState.LIVE_NO_CONTACT,
                LiveSensorState.LIVE_UNCERTAIN,
                LiveSensorState.LIVE_LIMIT,
                LiveSensorState.PAUSED,
                LiveSensorState.RECONNECTING,
                LiveSensorState.SETUP_BLOCKED,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.LIVE_UNCERTAIN: frozenset(
            {
                LiveSensorState.LIVE_CONTACT,
                LiveSensorState.LIVE_NO_CONTACT,
                LiveSensorState.LIVE_LIMIT,
                LiveSensorState.PAUSED,
                LiveSensorState.RECONNECTING,
                LiveSensorState.SETUP_BLOCKED,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.LIVE_LIMIT: frozenset(
            {
                LiveSensorState.LIVE_CONTACT,
                LiveSensorState.LIVE_NO_CONTACT,
                LiveSensorState.LIVE_UNCERTAIN,
                LiveSensorState.PAUSED,
                LiveSensorState.RECONNECTING,
                LiveSensorState.SETUP_BLOCKED,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.PAUSED: frozenset(
            {
                LiveSensorState.READY,
                LiveSensorState.RECONNECTING,
                LiveSensorState.SETUP_BLOCKED,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.RECONNECTING: frozenset(
            {
                LiveSensorState.SETUP_BLOCKED,
                LiveSensorState.BASELINE_CAPTURING,
                LiveSensorState.ERROR,
            }
        ),
        LiveSensorState.ERROR: frozenset(
            {LiveSensorState.SETUP_BLOCKED, LiveSensorState.RECONNECTING}
        ),
    }
)


EXACT_FORCE_STATES = frozenset(
    {LiveSensorState.LIVE_NO_CONTACT, LiveSensorState.LIVE_CONTACT}
)
EXACT_LOCALIZATION_STATES = frozenset({LiveSensorState.LIVE_CONTACT})


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically for provenance hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_hash(value: Any) -> str:
    """Return SHA-256 for a canonical JSON value."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def load_json_object(path: str | Path) -> dict[str, Any]:
    """Load one strict JSON object with an actionable error."""

    selected = Path(path)
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read valid JSON from {selected}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{selected} must contain one JSON object")
    return value


def validate_json_schema(instance_path: str | Path, schema_path: str | Path) -> str:
    """Validate a JSON object and return its canonical SHA-256."""

    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from jsonschema.exceptions import SchemaError, ValidationError
    except ImportError as exc:
        raise ContractError(
            "jsonschema is required; install requirements-analysis.txt or "
            "requirements-live.txt"
        ) from exc

    instance = load_json_object(instance_path)
    schema = load_json_object(schema_path)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)
    except (SchemaError, ValidationError) as exc:
        location = "/".join(str(item) for item in getattr(exc, "absolute_path", ()))
        where = f" at {location}" if location else ""
        raise ContractError(f"schema validation failed for {instance_path}{where}: {exc.message}") from exc
    return canonical_json_hash(instance)


def load_canonical_live_layout(path: str | Path) -> ROILayout:
    """Load the fixed layout and reject every geometry/orientation mismatch."""

    layout = load_roi_layout(path)
    problems: list[str] = []
    if layout.roi_layout_id != LIVE_LAYOUT_ID:
        problems.append(f"layout ID {layout.roi_layout_id!r} != {LIVE_LAYOUT_ID!r}")
    if (layout.frame_width, layout.frame_height) != ORIENTED_FRAME_SIZE:
        problems.append(
            "oriented frame size "
            f"{(layout.frame_width, layout.frame_height)!r} != {ORIENTED_FRAME_SIZE!r}"
        )
    if layout.rotation_degrees != LIVE_ROTATION_DEGREES:
        problems.append(
            f"rotation {layout.rotation_degrees!r} != {LIVE_ROTATION_DEGREES!r}"
        )
    if layout.mirror_horizontal is not LIVE_MIRROR_HORIZONTAL:
        problems.append(
            f"mirror_horizontal {layout.mirror_horizontal!r} is not True"
        )
    if problems:
        raise ContractError("incompatible live ROI layout: " + "; ".join(problems))
    return layout


def transition_allowed(
    current: LiveSensorState, requested: LiveSensorState
) -> bool:
    """Return whether the frozen state machine permits this transition."""

    return requested in _TRANSITIONS[current]


def require_transition(
    current: LiveSensorState, requested: LiveSensorState
) -> None:
    """Fail closed when a widget or controller requests an invalid transition."""

    if not transition_allowed(current, requested):
        raise ContractError(
            f"invalid live-sensor transition: {current.value} -> {requested.value}"
        )


def exact_outputs_allowed(state: LiveSensorState) -> tuple[bool, bool]:
    """Return `(force_allowed, localization_allowed)` for one state."""

    return state in EXACT_FORCE_STATES, state in EXACT_LOCALIZATION_STATES


__all__ = [
    "ContractError",
    "EXACT_FORCE_STATES",
    "EXACT_LOCALIZATION_STATES",
    "LIVE_LAYOUT_ID",
    "LIVE_MIRROR_HORIZONTAL",
    "LIVE_ROTATION_DEGREES",
    "LiveSensorState",
    "ORIENTED_FRAME_SIZE",
    "RAW_FRAME_SIZE",
    "canonical_json_bytes",
    "canonical_json_hash",
    "exact_outputs_allowed",
    "load_canonical_live_layout",
    "load_json_object",
    "require_transition",
    "transition_allowed",
    "validate_json_schema",
]
