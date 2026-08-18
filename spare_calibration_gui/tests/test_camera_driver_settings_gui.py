from __future__ import annotations

from PySide6.QtTest import QSignalSpy

from core.models import ApplicationConfig
from gui.main_window import MainWindow
from services.interfaces import CameraConnectionInfo, CameraPropertyCapability


def _physical_camera_info() -> CameraConnectionInfo:
    return CameraConnectionInfo(
        device_index=0,
        backend="DirectShow",
        requested_width=640,
        requested_height=480,
        requested_fps=30.0,
        actual_width=640,
        actual_height=480,
        actual_fps=30.0,
        simulation_mode=False,
        source_label="physical camera test",
    )


def test_current_driver_settings_are_displayed_and_stored_as_provenance(qtbot) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    window._camera_info = _physical_camera_info()
    capabilities = (
        CameraPropertyCapability(
            property_name="exposure",
            supported=True,
            readable=True,
            writable=None,
            current_value=-6.0,
        ),
        CameraPropertyCapability(
            property_name="auto_exposure",
            supported=True,
            readable=True,
            writable=None,
            current_value=False,
        ),
        CameraPropertyCapability(
            property_name="auto_focus",
            supported=True,
            readable=True,
            writable=None,
            current_value=True,
        ),
    )

    window._camera_capabilities_received(capabilities)

    assert window._camera_controls == {
        "exposure": -6.0,
        "auto_exposure": False,
        "auto_focus": True,
    }
    assert len(window._camera_property_readbacks) == 3
    assert "auto_focus" in window._automatic_control_warning
    assert "exposure=-6" in window.camera_tab.driver_settings_values.text()
    context = window._baseline_context_for_current_layout()
    assert context.camera_settings["current_driver_values"] == window._camera_controls
    window.close()


def test_native_driver_dialog_completion_invalidates_baseline_and_refreshes(qtbot) -> None:
    window = MainWindow(
        ApplicationConfig.load_json("config/default_config.json"),
        default_simulation=True,
    )
    qtbot.addWidget(window)
    window._camera_info = _physical_camera_info()
    window.readiness.baseline_valid = True
    refresh_spy = QSignalSpy(window.camera_capabilities_command)

    window._native_camera_properties_result(True, "closed")

    assert not window.readiness.baseline_valid
    assert refresh_spy.count() == 1
    assert "reading the values" in window.camera_tab.driver_settings_status.text()
    window.close()
