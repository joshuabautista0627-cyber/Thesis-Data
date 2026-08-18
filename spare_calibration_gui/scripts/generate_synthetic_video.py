"""Generate a short synthetic, unannotated camera video on demand.

No generated video is part of the repository.  Use this module from the project
root, for example::

    python -m scripts.generate_synthetic_video output/synthetic_input.avi
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import cv2

if __package__ in (None, ""):
    # Keep direct ``python scripts/generate_synthetic_video.py`` invocation
    # useful without requiring the project to be installed as a package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import CameraConfig
from services.simulation_service import SyntheticCameraSource


def _codec_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".avi":
        return "MJPG"
    if suffix == ".mp4":
        return "mp4v"
    raise ValueError("synthetic video filename must end in .avi or .mp4")


def generate_synthetic_video(
    output_path: str | Path,
    *,
    width: int = 320,
    height: int = 240,
    fps: float = 20.0,
    duration_s: float = 3.0,
    seed: int = 0,
    target_roi: int = 5,
    overwrite: bool = False,
) -> Path:
    """Write and verify a finite synthetic video, returning its absolute path."""

    destination = Path(output_path).expanduser().resolve()
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing video: {destination}")
    if width < 6 or height < 6:
        raise ValueError("width and height must each be at least 6 pixels")
    if fps <= 0.0 or duration_s <= 0.0:
        raise ValueError("fps and duration_s must be greater than zero")
    total_frames = max(1, round(float(fps) * float(duration_s)))
    codec = _codec_for_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    source = SyntheticCameraSource(
        seed=seed,
        total_frames=total_frames,
        target_roi=target_roi,
        base_monotonic_ns=1_000_000_000,
        base_wall_clock="2026-01-01T00:00:00+00:00",
    )
    config = CameraConfig(
        requested_width=int(width),
        requested_height=int(height),
        requested_fps=float(fps),
        warmup_seconds=0.0,
    )
    source.connect(config)
    writer = cv2.VideoWriter(
        str(destination),
        cv2.VideoWriter_fourcc(*codec),
        float(fps),
        (int(width), int(height)),
        True,
    )
    if not writer.isOpened():
        source.disconnect()
        writer.release()
        raise RuntimeError(
            f"OpenCV could not initialize {codec} writer for {destination}"
        )

    written = 0
    try:
        while True:
            captured = source.read_frame()
            if captured is None:
                break
            writer.write(captured.original_bgr)
            written += 1
    except Exception:
        writer.release()
        source.disconnect()
        if destination.exists():
            destination.unlink()
        raise
    writer.release()
    source.disconnect()
    if written != total_frames:
        if destination.exists():
            destination.unlink()
        raise RuntimeError(
            f"synthetic source produced {written} frames; expected {total_frames}"
        )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("synthetic video writer produced no usable file")

    verification = cv2.VideoCapture(str(destination))
    if not verification.isOpened():
        verification.release()
        raise RuntimeError("generated video could not be reopened by OpenCV")
    decoded_count = 0
    while True:
        ok, frame = verification.read()
        if not ok:
            break
        if frame is None or frame.shape[:2] != (int(height), int(width)):
            verification.release()
            raise RuntimeError("generated video contains an invalid frame")
        decoded_count += 1
    verification.release()
    if decoded_count != total_frames:
        raise RuntimeError(
            f"generated video decoded {decoded_count} frames; expected {total_frames}"
        )
    return destination


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a short unannotated 3x3-sensor simulation video."
    )
    parser.add_argument("output_path", type=Path)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--duration", type=float, default=3.0, dest="duration_s")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--target-roi", type=int, default=5)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; return zero only after generation and decode checks."""

    args = _argument_parser().parse_args(argv)
    generated = generate_synthetic_video(
        args.output_path,
        width=args.width,
        height=args.height,
        fps=args.fps,
        duration_s=args.duration_s,
        seed=args.seed,
        target_roi=args.target_roi,
        overwrite=args.overwrite,
    )
    print(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
