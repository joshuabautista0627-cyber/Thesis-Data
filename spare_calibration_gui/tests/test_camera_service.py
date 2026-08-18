"""Hardware-free tests for the single-owner physical OpenCV camera service."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
import pytest

from core.models import CameraBackend, CameraConfig
from services.camera_service import (
    CameraConnectionError,
    CameraNotConnectedError,
    CameraServiceError,
    OpenCVCameraService,
    load_camera_settings,
    save_camera_settings,
)
from services.interfaces import CameraServiceInterface


class CaptureTracker:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0

    def opened(self) -> None:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)

    def closed(self) -> None:
        self.active -= 1


class FakeCapture:
    def __init__(
        self,
        *,
        opened: bool,
        tracker: CaptureTracker,
        frames: Iterable[np.ndarray] = (),
        unsupported_properties: Iterable[int] = (),
        readback_overrides: dict[int, float] | None = None,
        initial_properties: dict[int, float] | None = None,
    ) -> None:
        self._opened = bool(opened)
        self._tracker = tracker
        self._frames = [frame.copy() for frame in frames]
        self._frame_index = 0
        self._unsupported = set(unsupported_properties)
        self._readback_overrides = dict(readback_overrides or {})
        self._properties = {
            cv2.CAP_PROP_FRAME_WIDTH: 640.0,
            cv2.CAP_PROP_FRAME_HEIGHT: 480.0,
            cv2.CAP_PROP_FPS: 30.0,
            **(initial_properties or {}),
        }
        self.set_calls: list[tuple[int, float]] = []
        self.release_count = 0
        if self._opened:
            self._tracker.opened()

    def isOpened(self) -> bool:
        return self._opened

    def set(self, property_id: int, value: float) -> bool:
        self.set_calls.append((property_id, value))
        if not self._opened or property_id in self._unsupported:
            return False
        self._properties[property_id] = float(value)
        return True

    def get(self, property_id: int) -> float:
        if property_id in self._readback_overrides:
            return self._readback_overrides[property_id]
        return self._properties.get(property_id, 0.0)

    def read(self) -> tuple[bool, np.ndarray | None]:
        if not self._opened or self._frame_index >= len(self._frames):
            return False, None
        frame = self._frames[self._frame_index].copy()
        self._frame_index += 1
        return True, frame

    def release(self) -> None:
        self.release_count += 1
        if self._opened:
            self._opened = False
            self._tracker.closed()


class FakeCaptureFactory:
    def __init__(
        self,
        *,
        open_pairs: Iterable[tuple[int, int]],
        frames: Iterable[np.ndarray] = (),
        unsupported_properties: Iterable[int] = (),
        readback_overrides: dict[int, float] | None = None,
        initial_properties: dict[int, float] | None = None,
    ) -> None:
        self.open_pairs = set(open_pairs)
        self.frames = list(frames)
        self.unsupported_properties = set(unsupported_properties)
        self.readback_overrides = dict(readback_overrides or {})
        self.initial_properties = dict(initial_properties or {})
        self.tracker = CaptureTracker()
        self.calls: list[tuple[int, int]] = []
        self.instances: list[FakeCapture] = []

    def __call__(self, device_index: int, backend_code: int) -> FakeCapture:
        self.calls.append((device_index, backend_code))
        capture = FakeCapture(
            opened=(device_index, backend_code) in self.open_pairs,
            tracker=self.tracker,
            frames=self.frames,
            unsupported_properties=self.unsupported_properties,
            readback_overrides=self.readback_overrides,
            initial_properties=self.initial_properties,
        )
        self.instances.append(capture)
        return capture


def _config(
    *,
    index: int = 0,
    warmup_s: float = 0.0,
    tolerance: float = 0.05,
) -> CameraConfig:
    return CameraConfig(
        device_index=index,
        requested_width=320,
        requested_height=240,
        requested_fps=20.0,
        warmup_seconds=warmup_s,
        property_readback_tolerance=tolerance,
    )


def test_connect_tries_directshow_then_msmf_and_keeps_one_owner() -> None:
    factory = FakeCaptureFactory(open_pairs={(0, cv2.CAP_MSMF)})
    service = OpenCVCameraService(
        capture_factory=factory, monotonic_clock=lambda: 100
    )
    assert isinstance(service, CameraServiceInterface)

    info = service.connect(_config())
    assert factory.calls[:2] == [(0, cv2.CAP_DSHOW), (0, cv2.CAP_MSMF)]
    assert factory.instances[0].release_count == 1
    assert info.backend == CameraBackend.MEDIA_FOUNDATION.value
    assert info.simulation_mode is False
    assert info.actual_width == 320
    assert info.actual_height == 240
    assert info.actual_fps == 20.0
    assert service.mode_confirmed
    assert factory.tracker.active == 1
    assert factory.tracker.maximum_active == 1

    service.connect(_config())
    assert factory.tracker.active == 1
    assert factory.tracker.maximum_active == 1
    service.disconnect()
    service.disconnect()
    assert factory.tracker.active == 0


def test_all_backend_failures_release_attempts_and_report_evidence() -> None:
    factory = FakeCaptureFactory(open_pairs=set())
    service = OpenCVCameraService(capture_factory=factory)
    with pytest.raises(CameraConnectionError, match="could not be opened"):
        service.connect(_config(index=2))
    assert factory.calls == [(2, cv2.CAP_DSHOW), (2, cv2.CAP_MSMF)]
    assert all(instance.release_count == 1 for instance in factory.instances)
    assert not service.is_connected
    assert [attempt.backend for attempt in service.backend_attempts] == [
        CameraBackend.DIRECTSHOW.value,
        CameraBackend.MEDIA_FOUNDATION.value,
    ]


def test_refresh_probes_sequentially_and_releases_every_temporary_capture() -> None:
    factory = FakeCaptureFactory(open_pairs={(1, cv2.CAP_MSMF)})
    service = OpenCVCameraService(capture_factory=factory)
    devices = service.refresh_devices(max_devices=3)
    assert [(device.device_index, device.backend) for device in devices] == [
        (1, CameraBackend.MEDIA_FOUNDATION.value)
    ]
    assert "identify Arducam IMX179 Camera Module by preview" in devices[0].display_name
    assert factory.tracker.active == 0
    assert factory.tracker.maximum_active == 1
    assert all(instance.release_count == 1 for instance in factory.instances)

    service.connect(_config(index=1))
    call_count = len(factory.calls)
    with pytest.raises(CameraServiceError, match="disconnect before refreshing"):
        service.refresh_devices(max_devices=2)
    assert len(factory.calls) == call_count
    assert factory.tracker.active == 1
    service.disconnect()


def test_read_returns_original_immutable_bgr_and_failure_disconnects() -> None:
    first = np.full((12, 16, 3), (10, 20, 30), dtype=np.uint8)
    second = np.full((12, 16, 3), (40, 50, 60), dtype=np.uint8)
    factory = FakeCaptureFactory(
        open_pairs={(0, cv2.CAP_DSHOW)}, frames=[first, second]
    )
    clock_values = iter([1_000, 2_000, 3_000])
    service = OpenCVCameraService(
        capture_factory=factory,
        monotonic_clock=lambda: next(clock_values),
        wall_clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    service.connect(_config())
    frame0 = service.read_frame()
    frame1 = service.read_frame()
    assert frame0 is not None and frame1 is not None
    assert [frame0.source_frame_id, frame1.source_frame_id] == [0, 1]
    assert [frame0.host_monotonic_ns, frame1.host_monotonic_ns] == [2_000, 3_000]
    assert frame0.wall_clock_iso == "2026-08-03T00:00:00.000000+00:00"
    assert np.array_equal(frame0.original_bgr, first)
    assert np.array_equal(frame1.original_bgr, second)
    assert not frame0.original_bgr.flags.writeable

    preview = frame0.original_bgr.copy()
    preview[0, 0] = (255, 255, 255)
    assert tuple(frame0.original_bgr[0, 0]) == (10, 20, 30)
    assert service.read_frame() is None
    assert not service.is_connected
    assert factory.tracker.active == 0
    assert "read failed" in service.last_error
    with pytest.raises(CameraNotConnectedError):
        service.read_frame()


def test_manual_controls_confirm_readback_and_mark_unsupported_safely() -> None:
    factory = FakeCaptureFactory(
        open_pairs={(0, cv2.CAP_DSHOW)},
        unsupported_properties={cv2.CAP_PROP_GAIN},
    )
    clock_values = iter([100, 500])
    service = OpenCVCameraService(
        capture_factory=factory, monotonic_clock=lambda: next(clock_values)
    )
    service.connect(_config(warmup_s=1.0))
    results = service.apply_settings(
        {
            "exposure": -5.0,
            "automatic exposure": False,
            "gain": 12.0,
            "not_a_camera_control": 1.0,
        }
    )
    by_name = {result.property_name: result for result in results}
    assert by_name["exposure"].confirmed
    assert by_name["auto_exposure"].confirmed
    assert by_name["auto_exposure"].actual_value is False
    assert not by_name["gain"].supported
    assert "unsupported" in by_name["gain"].message
    assert not by_name["not_a_camera_control"].supported
    assert "no driver call" in by_name["not_a_camera_control"].message
    active_capture = factory.instances[-1]
    assert (cv2.CAP_PROP_AUTO_EXPOSURE, 0.25) in active_capture.set_calls

    assert not service.warmup_complete(500 + 999_999_999)
    assert service.warmup_complete(500 + 1_000_000_000)
    diagnostics = service.diagnostics()
    assert diagnostics["mode_confirmed"] is True
    assert len(diagnostics["property_readbacks"]) == 3


def test_mode_mismatch_is_reported_not_claimed_as_applied() -> None:
    factory = FakeCaptureFactory(
        open_pairs={(0, cv2.CAP_DSHOW)},
        readback_overrides={cv2.CAP_PROP_FPS: 15.0},
    )
    service = OpenCVCameraService(
        capture_factory=factory, monotonic_clock=lambda: 1_000
    )
    info = service.connect(_config())
    assert info.actual_fps == 15.0
    assert not service.mode_confirmed
    fps_result = {item.property_name: item for item in service.mode_readbacks}["fps"]
    assert fps_result.write_succeeded
    assert fps_result.read_succeeded
    assert not fps_result.confirmed
    assert "differ" in fps_result.message


def test_settings_free_gui_connection_reads_native_mode_without_writes() -> None:
    factory = FakeCaptureFactory(
        open_pairs={(0, cv2.CAP_DSHOW)},
        initial_properties={
            cv2.CAP_PROP_FRAME_WIDTH: 800.0,
            cv2.CAP_PROP_FRAME_HEIGHT: 600.0,
            cv2.CAP_PROP_FPS: 15.0,
        },
    )
    service = OpenCVCameraService(
        capture_factory=factory, monotonic_clock=lambda: 1_000
    )
    info = service.connect(_config(), apply_mode=False)

    assert (info.actual_width, info.actual_height, info.actual_fps) == (800, 600, 15.0)
    assert factory.instances[-1].set_calls == []
    assert service.mode_confirmed
    assert all(row.requested_value is None for row in service.mode_readbacks)
    assert all(not row.write_succeeded for row in service.mode_readbacks)
    assert all("without a GUI mode write" in row.message for row in service.mode_readbacks)


def test_settings_json_round_trip_is_explicit_and_loading_does_not_apply(
    tmp_path: Path,
) -> None:
    path = tmp_path / "camera_settings.json"
    saved = save_camera_settings(
        path,
        {
            "automatic exposure": False,
            "white balance temperature": 4_500,
            "auto focus": True,
        },
        metadata={"operator": "test"},
    )
    assert saved == path
    loaded = load_camera_settings(path)
    assert loaded == {
        "auto_exposure": False,
        "white_balance": 4_500.0,
        "auto_focus": True,
    }
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["camera_model"] == "Arducam IMX179 Camera Module"

    factory = FakeCaptureFactory(open_pairs={(0, cv2.CAP_DSHOW)})
    service = OpenCVCameraService(
        capture_factory=factory, monotonic_clock=iter(range(100, 1000)).__next__
    )
    service.connect(_config())
    capture = factory.instances[-1]
    mode_write_count = len(capture.set_calls)
    assert service.load_settings(path, apply=False) == loaded
    assert len(capture.set_calls) == mode_write_count
    applied = service.load_settings(path, apply=True)
    assert isinstance(applied, tuple) and all(item.confirmed for item in applied)
    assert len(capture.set_calls) == mode_write_count + 3

    with pytest.raises(ValueError, match="unsupported camera setting"):
        save_camera_settings(tmp_path / "bad.json", {"invented": 1.0})
    with pytest.raises(ValueError, match="finite"):
        save_camera_settings(tmp_path / "nan.json", {"gain": math.nan})


def test_source_path_and_naive_wall_clock_fail_without_false_capture_data() -> None:
    factory = FakeCaptureFactory(
        open_pairs={(0, cv2.CAP_DSHOW)},
        frames=[np.zeros((4, 4, 3), dtype=np.uint8)],
    )
    service = OpenCVCameraService(
        capture_factory=factory,
        monotonic_clock=iter([1, 2]).__next__,
        wall_clock=lambda: datetime(2026, 8, 3),
    )
    with pytest.raises(ValueError, match="does not accept source_path"):
        service.connect(_config(), source_path="input.avi")
    service.connect(_config())
    with pytest.raises(ValueError, match="timezone-aware"):
        service.read_frame()


def test_capability_detection_is_read_only_and_keeps_unknown_ranges_explicit() -> None:
    factory = FakeCaptureFactory(
        open_pairs={(0, cv2.CAP_DSHOW)},
        readback_overrides={cv2.CAP_PROP_GAIN: math.nan},
        initial_properties={cv2.CAP_PROP_BRIGHTNESS: 42.0},
    )
    service = OpenCVCameraService(
        capture_factory=factory, monotonic_clock=iter(range(100, 1000)).__next__
    )
    service.connect(_config())
    capture = factory.instances[-1]
    writes_before = tuple(capture.set_calls)

    by_name = {item.property_name: item for item in service.detect_capabilities()}

    assert tuple(capture.set_calls) == writes_before
    assert by_name["brightness"].supported
    assert by_name["brightness"].current_value == 42.0
    assert by_name["brightness"].minimum_value is None
    assert by_name["brightness"].maximum_value is None
    assert by_name["brightness"].step_size is None
    assert by_name["brightness"].default_value is None
    assert by_name["brightness"].writable is None
    assert not by_name["gain"].supported
    assert not by_name["gain"].readable


def test_native_properties_uses_directshow_settings_and_rejects_other_backend() -> None:
    factory = FakeCaptureFactory(
        open_pairs={(0, cv2.CAP_DSHOW), (1, cv2.CAP_MSMF)}
    )
    service = OpenCVCameraService(
        capture_factory=factory, monotonic_clock=iter(range(100, 1000)).__next__
    )
    service.connect(_config(index=0))
    assert service.open_native_properties()
    assert (cv2.CAP_PROP_SETTINGS, 1.0) in factory.instances[-1].set_calls
    service.disconnect()

    media_config = CameraConfig(
        device_index=1,
        requested_width=320,
        requested_height=240,
        requested_fps=20.0,
        backend_preference=(CameraBackend.MEDIA_FOUNDATION.value,),
        warmup_seconds=0.0,
    )
    service.connect(media_config)
    with pytest.raises(CameraServiceError, match="DirectShow"):
        service.open_native_properties()
