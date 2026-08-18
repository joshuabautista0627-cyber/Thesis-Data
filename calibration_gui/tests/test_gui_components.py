"""Offscreen component tests for signal-only camera/load-cell/help widgets."""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QSignalSpy

from gui.camera_tab import CameraTab
from gui.help_dialog import HELP_TOPICS, HelpDialog
from gui.loadcell_tab import LoadCellTab
from services.camera_service import CameraDeviceInfo
from services.interfaces import CameraPropertyCapability, SerialConnectionInfo


def test_camera_tab_is_simple_raw_view_without_property_controls(qtbot) -> None:
    tab = CameraTab()
    qtbot.addWidget(tab)
    tab.resize(600, 430)
    tab.show()
    qtbot.wait(10)

    assert tab.scroll_area.widgetResizable()
    assert tab.instruction_label.wordWrap()
    assert tab.minimumWidth() <= 600 and tab.minimumHeight() <= 430
    assert tab.backend_combo.itemData(0) == "DirectShow"
    assert tab.backend_combo.itemData(1) == "Media Foundation"
    assert tab.requested_properties() == {}
    assert not hasattr(tab, "apply_settings_button")
    assert not hasattr(tab, "property_value_spins")
    assert "without silently overwriting" in tab.instruction_label.text()
    assert tab.scroll_area.verticalScrollBar().maximum() > 0


def test_camera_actions_emit_only_requested_payloads(qtbot) -> None:
    tab = CameraTab()
    qtbot.addWidget(tab)
    tab.set_camera_devices(((2, "Arducam candidate"), (4, "Other camera")))
    tab.device_combo.setCurrentIndex(0)
    tab.backend_combo.setCurrentIndex(1)

    refresh_spy = QSignalSpy(tab.refresh_devices_requested)
    connect_spy = QSignalSpy(tab.connect_requested)
    qtbot.mouseClick(tab.refresh_devices_button, Qt.LeftButton)
    qtbot.mouseClick(tab.connect_button, Qt.LeftButton)
    assert refresh_spy.count() == 1
    assert connect_spy.count() == 1
    payload = connect_spy.at(0)[0]
    assert payload == {
        "device_index": 2,
        "backend": "Media Foundation",
    }


def test_camera_device_info_discovery_result_populates_device_combo(qtbot) -> None:
    tab = CameraTab()
    qtbot.addWidget(tab)
    device = CameraDeviceInfo(
        device_index=3,
        backend="DirectShow",
        actual_width=640,
        actual_height=480,
        actual_fps=30.0,
        display_name="Camera index 3 (DirectShow); Arducam candidate",
    )

    tab.set_camera_devices((device,))

    assert tab.device_combo.count() == 1
    assert tab.device_combo.currentData() == 3
    assert tab.device_combo.currentText() == device.display_name


def test_camera_stream_performance_preview_and_lock_state(qtbot) -> None:
    tab = CameraTab()
    qtbot.addWidget(tab)
    tab.set_connection_state(
        True,
        {
            "actual_width": 1280,
            "actual_height": 720,
            "actual_fps": 29.8,
            "backend": "DirectShow",
            "device_index": 1,
        },
    )
    assert "1280 × 720" in tab.actual_mode_label.text()
    assert "Current driver properties are read" in tab.actual_mode_label.text()
    assert tab.open_native_properties_button.isEnabled()
    assert tab.refresh_driver_settings_button.isEnabled()

    tab.update_performance(
        {
            "capture_fps": 30.0,
            "preview_fps": 20.0,
            "capture_to_display_ms": 42.5,
            "feature_processing_fps": math.nan,
        }
    )
    assert tab.performance_labels["capture_fps"].text() == "30.0 FPS"
    assert tab.performance_labels["capture_to_display_ms"].text() == "42.5 ms"
    assert tab.performance_labels["feature_processing_fps"].text() == "—"
    image = QImage(32, 24, QImage.Format_RGB888)
    image.fill(Qt.red)
    tab.set_preview_image(image)
    assert tab.preview_label.pixmap() is not None

    tab.set_controls_locked(True)
    assert not tab.disconnect_button.isEnabled()
    assert not tab.open_native_properties_button.isEnabled()
    assert not tab.refresh_driver_settings_button.isEnabled()
    tab.set_controls_locked(False)
    assert tab.disconnect_button.isEnabled()


def test_camera_driver_settings_actions_and_readback_display(qtbot) -> None:
    tab = CameraTab()
    qtbot.addWidget(tab)
    native_spy = QSignalSpy(tab.native_properties_requested)
    refresh_spy = QSignalSpy(tab.driver_settings_refresh_requested)
    tab.set_connection_state(
        True,
        {
            "actual_width": 640,
            "actual_height": 480,
            "actual_fps": 30.0,
            "backend": "DirectShow",
            "device_index": 0,
            "simulation_mode": False,
        },
    )

    qtbot.mouseClick(tab.open_native_properties_button, Qt.LeftButton)
    qtbot.mouseClick(tab.refresh_driver_settings_button, Qt.LeftButton)
    assert native_spy.count() == 1
    assert refresh_spy.count() == 1

    tab.update_capabilities(
        (
            CameraPropertyCapability(
                property_name="exposure",
                supported=True,
                readable=True,
                writable=None,
                current_value=-5.0,
            ),
            CameraPropertyCapability(
                property_name="auto_focus",
                supported=True,
                readable=True,
                writable=None,
                current_value=False,
            ),
        )
    )
    assert "exposure=-5" in tab.driver_settings_values.text()
    assert "auto focus=off" in tab.driver_settings_values.text()
    assert "Read 2 current value(s)" in tab.driver_settings_status.text()

    tab.set_connection_state(
        True,
        {
            "actual_width": 640,
            "actual_height": 480,
            "actual_fps": 30.0,
            "backend": "Media Foundation",
            "device_index": 0,
            "simulation_mode": False,
        },
    )
    assert not tab.open_native_properties_button.isEnabled()
    assert tab.refresh_driver_settings_button.isEnabled()


def test_camera_preview_and_file_buttons_emit_without_widget_io(qtbot) -> None:
    tab = CameraTab()
    qtbot.addWidget(tab)
    tab.set_connection_state(True)
    start_spy = QSignalSpy(tab.start_preview_requested)
    stop_spy = QSignalSpy(tab.stop_preview_requested)
    disconnect_spy = QSignalSpy(tab.disconnect_requested)
    qtbot.mouseClick(tab.start_preview_button, Qt.LeftButton)
    tab.set_preview_state(True)
    qtbot.mouseClick(tab.stop_preview_button, Qt.LeftButton)
    qtbot.mouseClick(tab.disconnect_button, Qt.LeftButton)
    assert [spy.count() for spy in (start_spy, stop_spy, disconnect_spy)] == [1, 1, 1]


def test_loadcell_tab_controls_signals_and_default_200g_workflow(qtbot) -> None:
    tab = LoadCellTab()
    qtbot.addWidget(tab)
    tab.resize(600, 430)
    tab.show()
    tab.set_serial_ports(
        (
            {"device": "COM7", "description": "Arduino Nano"},
            {"device": "COM9", "description": "Other"},
        )
    )
    assert tab.scroll_area.widgetResizable()
    assert tab.instruction_label.wordWrap()
    assert tab.known_mass_spin.value() == 200.0
    assert tab.scroll_area.verticalScrollBar().maximum() > 0

    connect_spy = QSignalSpy(tab.connect_requested)
    qtbot.mouseClick(tab.connect_button, Qt.LeftButton)
    assert connect_spy.count() == 1
    assert connect_spy.at(0)[0] == {"port": "COM7", "baud_rate": 115200}

    tab.set_connection_state(
        True,
        SerialConnectionInfo(
            port="COM7",
            baud_rate=115200,
            device_name="HX711_NANO",
            protocol_version="1.0",
            firmware_version="1.0.0",
            device_session_id="device-1",
            simulation_mode=False,
        ),
    )
    tab.set_stream_ready(True)
    tare_spy = QSignalSpy(tab.tare_requested)
    unloaded_spy = QSignalSpy(tab.calibration_unloaded_requested)
    loaded_spy = QSignalSpy(tab.calibration_loaded_requested)
    calculate_spy = QSignalSpy(tab.calibration_calculate_requested)
    verify_spy = QSignalSpy(tab.verification_requested)
    assert tab.capture_unloaded_button.isEnabled()
    assert not tab.capture_loaded_button.isEnabled()
    assert not tab.tare_button.isEnabled()
    qtbot.mouseClick(tab.capture_unloaded_button, Qt.LeftButton)
    tab.mark_sample_window_captured("unloaded")
    qtbot.mouseClick(tab.capture_loaded_button, Qt.LeftButton)
    tab.mark_sample_window_captured("loaded")
    qtbot.mouseClick(tab.calculate_calibration_button, Qt.LeftButton)
    tab.update_calibration_result(
        {
            "counts_per_gram": 100.0,
            "tare_raw": 1_000.0,
            "calibration_snr": 100.0,
            "loaded_window_cv_percent": 0.1,
            "quality_passed": True,
        }
    )
    qtbot.mouseClick(tab.tare_button, Qt.LeftButton)
    tab.mark_tare_complete()
    qtbot.mouseClick(tab.verify_calibration_button, Qt.LeftButton)
    assert tare_spy.count() == unloaded_spy.count() == 1
    assert loaded_spy.at(0)[0] == calculate_spy.at(0)[0] == verify_spy.at(0)[0] == 200.0
    assert tab.workflow_progress_label.text().startswith("Calibration progress: 4")


def test_loadcell_force_history_is_bounded_and_reuses_setdata_curve(qtbot, monkeypatch) -> None:
    tab = LoadCellTab(plot_history_s=2.0, max_plot_points=10)
    qtbot.addWidget(tab)
    curve_identity = id(tab.force_curve)
    original_set_data = tab.force_curve.setData
    calls: list[tuple[list[float], list[float]]] = []

    def recording_set_data(x, y, *args, **kwargs):
        calls.append((list(x), list(y)))
        return original_set_data(x, y, *args, **kwargs)

    monkeypatch.setattr(tab.force_curve, "setData", recording_set_data)
    for index in range(100):
        tab.append_force_sample(index * 0.1, index * 0.01, update_plot=False)
    tab.refresh_force_plot()
    assert id(tab.force_curve) == curve_identity
    assert tab.force_history_size <= 10
    assert len(calls) == 1
    assert len(calls[0][0]) == tab.force_history_size
    assert calls[0][0][-1] == pytest.approx(9.9)

    tab.update_live_reading(1234, 2.5, 0.024516625, elapsed_time_s=10.0)
    assert tab.raw_adc_label.text() == "1234"
    assert tab.force_gf_label.text().endswith(" g")
    assert tab.force_N_label.text().endswith(" N")
    assert id(tab.force_curve) == curve_identity


def test_loadcell_shows_raw_reading_before_calibration(qtbot) -> None:
    tab = LoadCellTab()
    qtbot.addWidget(tab)

    tab.update_live_reading(
        123456.25,
        None,
        None,
        tare_raw=None,
        wall_clock_iso="2026-08-03T12:34:56+08:00",
        reading_frequency_hz=79.8,
    )

    assert tab.raw_adc_label.text() == "123456.25"
    assert tab.force_gf_label.text() == "not calibrated"
    assert tab.force_N_label.text() == "not calibrated"
    assert tab.tared_raw_label.text() == "not calibrated"
    assert tab.calibration_state_label.text() == "Not calibrated"
    assert tab.reading_frequency_label.text() == "79.80 Hz"


def test_loadcell_diagnostics_calibration_verification_and_locking(qtbot) -> None:
    tab = LoadCellTab()
    qtbot.addWidget(tab)
    tab.set_serial_ports(("COM7",))
    tab.set_connection_state(True)
    tab.set_stream_ready(True)
    tab.update_diagnostics(
        {
            "valid_sample_count": 100,
            "measured_sample_rate_hz": 80.1,
            "minimum_sample_interval_ms": 12.0,
            "median_sample_interval_ms": 12.5,
            "maximum_sample_interval_ms": 13.1,
            "malformed_line_count": 2,
            "readiness_error_count": 1,
            "timeout_error_count": 0,
            "device_session_id": "device-2",
        }
    )
    assert tab.diagnostic_labels["valid_sample_count"].text() == "100"
    assert tab.diagnostic_labels["device_session_id"].text() == "device-2"
    tab.update_calibration_result(
        {
            "counts_per_gram": -100.0,
            "tare_raw": 12345.0,
            "calibration_snr": 30.0,
            "loaded_window_cv_percent": 0.5,
            "quality_passed": True,
        }
    )
    assert tab.calibration_labels["counts_per_gram"].text() == "-100.0"
    assert tab.calibration_labels["quality_passed"].text() == "Passed"
    tab.update_verification_result(
        {
            "passed": False,
            "expected_gf": 200.0,
            "measured_mean_gf": 180.0,
            "percentage_error": 10.0,
        }
    )
    assert "Failed" in tab.calibration_labels["verification_result"].text()
    tab.set_controls_locked(True)
    assert not tab.tare_button.isEnabled()
    assert not tab.port_combo.isEnabled()
    tab.set_controls_locked(False)
    assert not tab.tare_button.isEnabled()
    assert tab.verify_calibration_button.isEnabled()


def test_loadcell_known_mass_change_invalidates_sequential_progress(qtbot) -> None:
    tab = LoadCellTab()
    qtbot.addWidget(tab)
    tab.set_serial_ports(("COM7",))
    tab.set_connection_state(True)
    tab.set_stream_ready(True)
    tab.mark_sample_window_captured("unloaded")
    tab.mark_sample_window_captured("loaded")
    tab.set_calibration_available(True)
    tab.mark_tare_complete()
    changed = QSignalSpy(tab.known_mass_changed)

    tab.known_mass_spin.setValue(201.0)

    assert changed.count() == 1
    assert changed.at(0)[0] == 201.0
    assert tab.workflow_stage == "connected"
    assert tab.capture_unloaded_button.isEnabled()
    assert not tab.tare_button.isEnabled()
    assert "invalidated" in tab.status_label.text().lower()


def test_loadcell_programmatic_known_mass_restore_does_not_emit_change(qtbot) -> None:
    tab = LoadCellTab()
    qtbot.addWidget(tab)
    changed = QSignalSpy(tab.known_mass_changed)
    tab.set_known_mass_g(250.0)
    assert tab.known_mass_spin.value() == 250.0
    assert changed.count() == 0


def test_simulation_connection_is_never_presented_as_physical(qtbot) -> None:
    tab = LoadCellTab()
    qtbot.addWidget(tab)
    tab.set_connection_state(
        True,
        {
            "device_session_id": "playback-1",
            "protocol_version": "PLAYBACK-1.0",
            "firmware_version": "recorded",
            "simulation_mode": True,
        },
    )
    assert "simulation" in tab.status_label.text().lower()
    assert "no physical hardware" in tab.status_label.text().lower()


def test_help_dialog_contains_all_ten_topics_in_scrollable_selectable_text(qtbot) -> None:
    dialog = HelpDialog()
    qtbot.addWidget(dialog)
    dialog.resize(600, 480)
    dialog.show()
    qtbot.wait(10)
    assert not dialog.isModal()
    assert dialog.scroll_area.widgetResizable()
    assert len(HELP_TOPICS) == len(dialog.topic_labels) == 10
    assert all(label.wordWrap() for label in dialog.topic_labels)
    assert all(
        label.textInteractionFlags() & Qt.TextSelectableByMouse
        for label in dialog.topic_labels
    )
    assert dialog.scroll_area.verticalScrollBar().maximum() > 0
    searchable = dialog.help_text.lower()
    for required in (
            "arducam imx179",
        "directshow",
        "nine rois",
        "unloaded baseline",
        "arduino nano",
        "200 g",
        "trial labels",
        "one intentional press",
        "master_synchronized.csv",
        "partial files",
    ):
        assert required in searchable
