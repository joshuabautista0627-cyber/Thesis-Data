"""Deterministic, lossless camera orientation transforms."""

from __future__ import annotations

import numpy as np


VALID_ROTATIONS = (0, 90, 180, 270)


def validate_camera_orientation(
    rotation_degrees: int,
    mirror_horizontal: bool,
) -> tuple[int, bool]:
    rotation = int(rotation_degrees)
    if rotation not in VALID_ROTATIONS:
        raise ValueError("camera rotation must be 0, 90, 180, or 270 degrees clockwise")
    if not isinstance(mirror_horizontal, bool):
        raise ValueError("camera mirror_horizontal must be a boolean")
    return rotation, mirror_horizontal


def oriented_frame_size(
    width: int,
    height: int,
    rotation_degrees: int,
) -> tuple[int, int]:
    rotation, _ = validate_camera_orientation(rotation_degrees, False)
    return (int(height), int(width)) if rotation in {90, 270} else (int(width), int(height))


def orient_bgr_frame(
    frame_bgr: np.ndarray,
    *,
    rotation_degrees: int = 0,
    mirror_horizontal: bool = False,
) -> np.ndarray:
    """Rotate clockwise, then optionally mirror across the displayed vertical axis."""

    rotation, mirror = validate_camera_orientation(
        rotation_degrees,
        mirror_horizontal,
    )
    frame = np.asarray(frame_bgr)
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("camera frame must have shape (height, width, 3)")
    if rotation == 90:
        oriented = np.rot90(frame, k=3)
    elif rotation == 180:
        oriented = np.rot90(frame, k=2)
    elif rotation == 270:
        oriented = np.rot90(frame, k=1)
    else:
        oriented = frame
    if mirror:
        oriented = np.flip(oriented, axis=1)
    return np.ascontiguousarray(oriented)


__all__ = [
    "VALID_ROTATIONS",
    "orient_bgr_frame",
    "oriented_frame_size",
    "validate_camera_orientation",
]
