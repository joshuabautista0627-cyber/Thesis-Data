"""Gate 2 tests for deterministic camera and load-cell acquisition sources."""

from __future__ import annotations

from datetime import datetime
import math

import numpy as np
import pytest

from core.models import CameraConfig, LoadCellConfig
from scripts.generate_synthetic_video import generate_synthetic_video
from services.interfaces import CameraServiceInterface, SerialServiceInterface
from services.simulation_service import (
    SyntheticCameraSource,
    SyntheticLoadCellSource,
    VideoFileCameraSource,
    create_default_roi_layout,
    single_press_curve,
)


BASE_NS = 9_000_000_000
BASE_ISO = "2026-08-03T00:00:00+00:00"


def _camera_config(*, fps: float = 10.0, warmup_s: float = 0.0) -> CameraConfig:
    return CameraConfig(
        requested_width=96,
        requested_height=72,
        requested_fps=fps,
        warmup_seconds=warmup_s,
    )


def test_single_press_curve_has_one_smooth_peak_and_zero_endpoints() -> None:
    positions = np.linspace(0.0, 1.0, 101)
    values = np.array(
        [
            single_press_curve(
                position,
                start_fraction=0.2,
                peak_fraction=0.5,
                end_fraction=0.8,
            )
            for position in positions
        ]
    )
    assert values[0] == values[-1] == 0.0
    assert values[20] == values[80] == 0.0
    assert values[50] == pytest.approx(1.0)
    assert np.all(np.diff(values[20:51]) >= 0.0)
    assert np.all(np.diff(values[50:81]) <= 0.0)
    with pytest.raises(ValueError, match="start < peak < end"):
        single_press_curve(0.5, start_fraction=0.5, peak_fraction=0.5)


def test_default_layout_is_exact_ordered_nonoverlapping_three_by_three() -> None:
    layout = create_default_roi_layout(96, 72)
    assert [roi.roi_id for roi in layout.rois] == list(range(1, 10))
    assert layout.validation.valid
    assert layout.validation.overlap_pairs == ()
    assert [(roi.roi_id - 1) // 3 for roi in layout.rois] == [
        0,
        0,
        0,
        1,
        1,
        1,
        2,
        2,
        2,
    ]


def test_synthetic_camera_is_deterministic_read_only_and_targets_one_roi() -> None:
    kwargs = dict(
        seed=41,
        total_frames=21,
        target_roi=7,
        press_start_fraction=0.2,
        press_peak_fraction=0.5,
        press_end_fraction=0.8,
        temporal_noise_std=0.0,
        base_monotonic_ns=BASE_NS,
        base_wall_clock=BASE_ISO,
    )
    first_source = SyntheticCameraSource(**kwargs)
    second_source = SyntheticCameraSource(**kwargs)
    config = _camera_config(fps=10.0)
    first_info = first_source.connect(config)
    second_source.connect(config)
    assert isinstance(first_source, CameraServiceInterface)
    assert first_info.simulation_mode
    assert "Simulation" in first_info.backend

    first_frames = [first_source.read_frame() for _ in range(11)]
    second_frames = [second_source.read_frame() for _ in range(11)]
    assert all(frame is not None for frame in first_frames + second_frames)
    captured_first = [frame for frame in first_frames if frame is not None]
    captured_second = [frame for frame in second_frames if frame is not None]
    for left, right in zip(captured_first, captured_second):
        assert left.source_frame_id == right.source_frame_id
        assert left.host_monotonic_ns == right.host_monotonic_ns
        assert left.wall_clock_iso == right.wall_clock_iso
        assert np.array_equal(left.original_bgr, right.original_bgr)
        assert left.original_bgr.dtype == np.uint8
        assert left.original_bgr.flags.c_contiguous
        assert not left.original_bgr.flags.writeable

    baseline = captured_first[0].original_bgr
    baseline_snapshot = baseline.copy()
    peak = captured_first[10].original_bgr
    changed_roi_ids = []
    for roi in first_source.roi_layout.rois:
        difference = np.abs(
            peak[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width].astype(
                np.int16
            )
            - baseline[
                roi.y : roi.y + roi.height, roi.x : roi.x + roi.width
            ].astype(np.int16)
        )
        if np.any(difference):
            changed_roi_ids.append(roi.roi_id)
    assert changed_roi_ids == [7]
    assert peak.max() > baseline.max()

    preview_copy = baseline.copy()
    preview_copy[0, 0] = (255, 0, 0)
    assert np.array_equal(baseline, baseline_snapshot)
    with pytest.raises(ValueError):
        baseline[0, 0] = (0, 0, 0)


def test_synthetic_camera_timestamps_ids_warmup_and_finite_end() -> None:
    source = SyntheticCameraSource(
        total_frames=4,
        base_monotonic_ns=BASE_NS,
        base_wall_clock=BASE_ISO,
    )
    source.connect(_camera_config(fps=10.0, warmup_s=0.2))
    frames = [source.read_frame() for _ in range(4)]
    assert [frame.source_frame_id for frame in frames if frame is not None] == [
        0,
        1,
        2,
        3,
    ]
    timestamps = [frame.host_monotonic_ns for frame in frames if frame is not None]
    assert np.diff(timestamps).tolist() == [100_000_000] * 3
    assert not source.warmup_complete(timestamps[1])
    assert source.warmup_complete(timestamps[2])
    assert source.read_frame() is None
    assert source.read_frame() is None
    parsed = datetime.fromisoformat(frames[0].wall_clock_iso)  # type: ignore[union-attr]
    assert parsed.utcoffset() is not None


def test_synthetic_loadcell_is_deterministic_calibrated_and_never_stale() -> None:
    kwargs = dict(
        seed=7,
        sample_rate_hz=20.0,
        total_samples=11,
        counts_per_gram=-50.0,
        tare_raw=10_000.0,
        peak_force_gf=200.0,
        noise_std_counts=1.5,
        press_start_fraction=0.2,
        press_peak_fraction=0.5,
        press_end_fraction=0.8,
        base_monotonic_ns=BASE_NS,
        base_wall_clock=BASE_ISO,
    )
    first = SyntheticLoadCellSource(**kwargs)
    second = SyntheticLoadCellSource(**kwargs)
    config = LoadCellConfig()
    info = first.connect(config)
    second.connect(config)
    assert isinstance(first, SerialServiceInterface)
    assert info.simulation_mode
    assert info.port == "SIMULATED"

    left_samples = [first.read_sample() for _ in range(11)]
    right_samples = [second.read_sample() for _ in range(11)]
    assert left_samples == right_samples
    samples = [sample for sample in left_samples if sample is not None]
    assert [sample.arduino_sample_id for sample in samples] == list(range(11))
    assert all(sample.loadcell_valid for sample in samples)
    assert all(sample.device_session_id == info.device_session_id for sample in samples)
    assert np.diff([sample.host_monotonic_ns for sample in samples]).tolist() == [
        50_000_000
    ] * 10
    assert np.diff([sample.arduino_micros_unwrapped for sample in samples]).tolist() == [
        50_000
    ] * 10
    assert samples[5].force_gf == pytest.approx(200.0, abs=0.05)
    assert samples[5].force_N == pytest.approx(samples[5].force_gf * 0.00980665)
    assert first.read_sample() is None
    assert first.read_sample() is None

    diagnostics = first.diagnostics()
    assert diagnostics["valid_sample_count"] == 11
    assert diagnostics["measured_sample_rate_hz"] == pytest.approx(20.0)
    assert diagnostics["malformed_line_count"] == 0
    assert diagnostics["exhausted"] is True


def test_synthetic_loadcell_commands_do_not_consume_or_repeat_conversions() -> None:
    source = SyntheticLoadCellSource(
        total_samples=3,
        sample_rate_hz=10.0,
        noise_std_counts=0.0,
        base_monotonic_ns=BASE_NS,
        base_wall_clock=BASE_ISO,
        arduino_start_micros=(1 << 32) - 50_000,
    )
    first_session = source.connect(LoadCellConfig(), port="SIM-COM")
    assert source.send_command("STOP") == "OK,STOP"
    assert source.read_sample() is None
    assert "STOPPED" in source.send_command("STATUS")
    assert source.send_command("START") == "OK,START"
    samples = [source.read_sample() for _ in range(3)]
    assert [sample.arduino_sample_id for sample in samples if sample is not None] == [
        0,
        1,
        2,
    ]
    assert [sample.arduino_micros for sample in samples if sample is not None] == [
        (1 << 32) - 50_000,
        50_000,
        150_000,
    ]
    assert source.read_sample() is None

    source.disconnect()
    second_session = source.connect(LoadCellConfig())
    assert second_session.device_session_id != first_session.device_session_id
    restarted = source.read_sample()
    assert restarted is not None and restarted.arduino_sample_id == 0


def test_generated_video_plays_through_same_captured_frame_contract(tmp_path) -> None:
    path = generate_synthetic_video(
        tmp_path / "short_input.avi",
        width=96,
        height=72,
        fps=12.0,
        duration_s=0.5,
        seed=19,
        target_roi=2,
    )
    assert path.is_file() and path.stat().st_size > 0

    source = VideoFileCameraSource(
        base_monotonic_ns=BASE_NS,
        base_wall_clock=BASE_ISO,
    )
    config = _camera_config(fps=12.0)
    info = source.connect(config, source_path=str(path))
    assert isinstance(source, CameraServiceInterface)
    assert info.simulation_mode
    assert info.actual_width == 96
    assert info.actual_height == 72
    frames = []
    while True:
        captured = source.read_frame()
        if captured is None:
            break
        frames.append(captured)
    assert [frame.source_frame_id for frame in frames] == list(range(6))
    assert all(frame.original_bgr.shape == (72, 96, 3) for frame in frames)
    assert all(not frame.original_bgr.flags.writeable for frame in frames)
    expected_step = round(1_000_000_000 / info.actual_fps)
    actual_steps = np.diff([frame.host_monotonic_ns for frame in frames])
    assert np.all(np.abs(actual_steps - expected_step) <= 1)
    readbacks = source.apply_settings({"exposure": -5.0})
    assert len(readbacks) == 1
    assert not readbacks[0].supported
    source.disconnect()
    assert not source.is_connected


def test_video_generator_refuses_silent_overwrite(tmp_path) -> None:
    path = tmp_path / "existing.avi"
    generate_synthetic_video(path, width=48, height=36, fps=5.0, duration_s=0.4)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        generate_synthetic_video(path, width=48, height=36, fps=5.0, duration_s=0.4)
    assert math.isfinite(path.stat().st_size)
