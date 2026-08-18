from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtTest import QSignalSpy

from core.models import ApplicationConfig, CameraConfig, ROI
from gui.camera_tab import CameraTab
from gui.main_window import MainWindow
from processing.camera_orientation import orient_bgr_frame, oriented_frame_size
from processing.roi_manager import ROILayout, save_roi_layout
from services.interfaces import CapturedFrame
from services.qt_workers import CameraWorker, LatestValueBuffer


def _numbered_frame() -> np.ndarray:
    values = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.uint8)
    return np.repeat(values[..., None], 3, axis=2)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, [[1, 2, 3], [4, 5, 6]]),
        (90, [[4, 1], [5, 2], [6, 3]]),
        (180, [[6, 5, 4], [3, 2, 1]]),
        (270, [[3, 6], [2, 5], [1, 4]]),
    ],
)
def test_clockwise_camera_rotation_is_exact_and_contiguous(
    rotation: int,
    expected: list[list[int]],
) -> None:
    oriented = orient_bgr_frame(_numbered_frame(), rotation_degrees=rotation)
    assert oriented.flags.c_contiguous
    assert oriented[..., 0].tolist() == expected


def test_horizontal_mirror_is_applied_after_rotation() -> None:
    oriented = orient_bgr_frame(
        _numbered_frame(),
        rotation_degrees=90,
        mirror_horizontal=True,
    )
    assert oriented[..., 0].tolist() == [[1, 4], [2, 5], [3, 6]]


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [(0, (640, 480)), (90, (480, 640)), (180, (640, 480)), (270, (480, 640))],
)
def test_oriented_frame_size(rotation: int, expected: tuple[int, int]) -> None:
    assert oriented_frame_size(640, 480, rotation) == expected


def test_camera_config_orientation_round_trip_and_validation() -> None:
    config = ApplicationConfig(
        camera=replace(CameraConfig(), rotation_degrees=270, mirror_horizontal=True)
    )
    assert ApplicationConfig.from_dict(config.to_dict()) == config
    with pytest.raises(ValueError, match="rotation_degrees"):
        CameraConfig(rotation_degrees=45)
    with pytest.raises(ValueError, match="mirror_horizontal"):
        CameraConfig(mirror_horizontal=1)  # type: ignore[arg-type]


def test_camera_tab_emits_orientation_and_locks_controls(qtbot) -> None:
    tab = CameraTab()
    qtbot.addWidget(tab)
    spy = QSignalSpy(tab.orientation_changed)
    tab.rotation_combo.setCurrentIndex(tab.rotation_combo.findData(90))
    assert spy.at(0) == [90, False]
    tab.mirror_checkbox.setChecked(True)
    assert spy.at(1) == [90, True]
    assert tab.requested_orientation() == (90, True)
    tab.set_controls_locked(True)
    assert not tab.rotation_combo.isEnabled()
    assert not tab.mirror_checkbox.isEnabled()


def test_camera_tab_sets_rotation_and_mirror_with_one_signal(qtbot) -> None:
    tab = CameraTab()
    qtbot.addWidget(tab)
    spy = QSignalSpy(tab.orientation_changed)

    tab.set_orientation(270, True)

    assert tab.requested_orientation() == (270, True)
    assert spy.count() == 1
    assert spy.at(0) == [270, True]


def test_main_window_rotation_updates_roi_dimensions_before_connect(qtbot) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    window.camera_tab.rotation_combo.setCurrentIndex(
        window.camera_tab.rotation_combo.findData(90)
    )
    assert window._active_camera_config.rotation_degrees == 90
    assert (window.roi_tab.frame_width, window.roi_tab.frame_height) == (480, 640)
    assert not window.readiness.baseline_valid
    assert "orientation applied" in window.camera_tab.status_label.text().lower()


def _offset_first_roi(rois: tuple[ROI, ...]) -> tuple[ROI, ...]:
    first = rois[0]
    return (
        ROI(
            roi_id=first.roi_id,
            x=first.x + 1,
            y=first.y + 1,
            width=first.width,
            height=first.height,
        ),
        *rois[1:],
    )


def test_main_window_loads_saved_roi_values_at_same_frame_size(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    saved_rois = _offset_first_roi(window.roi_tab.rois)
    path = save_roi_layout(
        tmp_path / "same_size_roi.json",
        ROILayout.create(saved_rois, 640, 480),
    )
    assert window.roi_tab.rois != saved_rois
    monkeypatch.setattr(
        "gui.main_window.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(path), "JSON (*.json)"),
    )

    window._load_roi_layout()

    assert window.roi_tab.rois == saved_rois
    assert "loaded roi layout" in window.roi_tab.status_label.text().lower()
    window.close()


def test_main_window_loads_legacy_roi_after_restoring_swapped_orientation(
    qtbot, tmp_path, monkeypatch
) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    window.camera_tab.set_orientation(90, False)
    saved_rois = _offset_first_roi(window.roi_tab.rois)
    path = save_roi_layout(
        tmp_path / "legacy_portrait_roi.json",
        ROILayout.create(saved_rois, 480, 640),
    )
    window.camera_tab.set_orientation(0, False)
    assert (window.roi_tab.frame_width, window.roi_tab.frame_height) == (640, 480)
    monkeypatch.setattr(
        "gui.main_window.QFileDialog.getOpenFileName",
        lambda *_args, **_kwargs: (str(path), "JSON (*.json)"),
    )

    window._load_roi_layout()

    assert window.camera_tab.requested_orientation() == (90, False)
    assert (window.roi_tab.frame_width, window.roi_tab.frame_height) == (480, 640)
    assert window.roi_tab.rois == saved_rois
    assert "legacy layout dimensions were rotated" in window.roi_tab.status_label.text().lower()
    window.close()


def test_camera_worker_orients_same_frame_for_preview_analysis_and_recording() -> None:
    captured = CapturedFrame(
        original_bgr=_numbered_frame(),
        source_frame_id=7,
        host_monotonic_ns=100,
        wall_clock_iso="2026-08-04T00:00:00+00:00",
    )

    class OneFrameService:
        is_connected = True
        simulation_mode = False

        def read_frame(self):
            return captured

    preview = LatestValueBuffer()
    worker = CameraWorker(preview)
    worker._service = OneFrameService()
    worker.set_orientation(90, True)
    analysis: list[CapturedFrame] = []
    recording: list[CapturedFrame] = []
    worker.set_analysis_sink(lambda frame: analysis.append(frame) or True)
    worker.set_recording_sink(lambda frame: recording.append(frame) or True)

    worker._capture_once()

    displayed = preview.take_latest()
    assert displayed is analysis[0] is recording[0]
    assert displayed.original_bgr[..., 0].tolist() == [[1, 4], [2, 5], [3, 6]]
    assert displayed.source_frame_id == 7
    assert displayed.host_monotonic_ns == 100
