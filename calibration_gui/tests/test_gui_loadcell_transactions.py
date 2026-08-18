"""Regression tests for atomic load-cell GUI workflow transactions."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from core.models import ApplicationConfig, ErrorCode
from gui.main_window import MainWindow
from processing.loadcell_calibration import (
    build_saved_calibration,
    calculate_calibration,
    verify_calibration,
)


UNLOADED = tuple(100_000 + (index % 3) - 1 for index in range(30))
LOADED = tuple(120_000 + (index % 3) - 1 for index in range(30))


def _config(tmp_path: Path) -> ApplicationConfig:
    root = Path(__file__).resolve().parents[1]
    base = ApplicationConfig.load_json(root / "config" / "default_config.json")
    return replace(
        base,
        loadcell=replace(base.loadcell, calibration_window_s=0.05),
        recording=replace(base.recording, output_directory=str(tmp_path)),
    )


def _serial_info(session_id: str = "serial-session-a") -> SimpleNamespace:
    return SimpleNamespace(
        port="SIMULATED",
        device_session_id=session_id,
        device_name="SIMULATED_HX711",
        protocol_version="SIM-1.0",
        firmware_version="synthetic-1.0",
        simulation_mode=True,
    )


def _connected_window(qtbot, tmp_path: Path, *, fresh_data: bool = True) -> MainWindow:
    window = MainWindow(_config(tmp_path), default_simulation=True)
    qtbot.addWidget(window)
    window._serial_connected(_serial_info())
    if fresh_data:
        window._serial_health_changed(True, "", "Fresh HX711 DATA stream is healthy.")
    return window


def _saved_calibration(*, verified: bool, retared: bool = False):
    computation = calculate_calibration(UNLOADED, LOADED, 200.0)
    verification = (
        verify_calibration(
            LOADED,
            200.0,
            computation.tare_raw,
            computation.counts_per_gram,
        )
        if verified
        else None
    )
    timestamp = datetime.now(UTC).isoformat()
    calibration = build_saved_calibration(
        "transaction-calibration",
        computation,
        verification,
        serial_port="SIMULATED",
        firmware_identity="SIMULATED_HX711/synthetic-1.0",
        protocol_version="SIM-1.0",
        firmware_version="synthetic-1.0",
        calibration_timestamp_iso=timestamp,
    )
    if retared:
        calibration = calibration.with_tare(
            computation.tare_raw, "2099-01-01T00:00:00+00:00"
        )
    return calibration


def _install_calibration(window: MainWindow, calibration) -> None:
    window._calibration = calibration
    window.loadcell_tab.set_known_mass_g(calibration.known_mass_g)
    window.loadcell_tab.update_calibration_result(calibration.to_dict())
    if calibration.tare_timestamp_iso != calibration.calibration_timestamp_iso:
        window.loadcell_tab.mark_tare_complete()
    if calibration.verification is not None:
        window.loadcell_tab.update_verification_result(
            asdict(calibration.verification)
        )
    window.readiness.loadcell_calibrated = True
    window.readiness.calibration_verified = bool(
        calibration.verification is not None and calibration.verification.passed
    )
    window._refresh_readiness()


def _complete_window(window: MainWindow, raw_values: tuple[int, ...]) -> None:
    base_ns = 1_000_000_000
    for index, raw_adc in enumerate(raw_values):
        window._sample_collector.accept(
            SimpleNamespace(
                host_monotonic_ns=base_ns + index * 2_000_000,
                raw_adc=raw_adc,
            )
        )
    window._refresh_status()


def test_delayed_calculation_cannot_commit_after_new_unloaded_generation(
    qtbot, tmp_path: Path
) -> None:
    window = _connected_window(qtbot, tmp_path)
    window._calibration_windows = {"unloaded": UNLOADED, "loaded": LOADED}
    window.loadcell_tab.mark_sample_window_captured("unloaded")
    window.loadcell_tab.mark_sample_window_captured("loaded")
    pending: dict[str, object] = {}

    def delay(function, succeeded, failed, *args, **kwargs):
        pending.update(
            function=function,
            succeeded=succeeded,
            failed=failed,
            args=args,
            kwargs=kwargs,
        )

    window._run_job = delay
    window._calculate_calibration(200.0)
    assert window._loadcell_pending_job == (
        window._loadcell_workflow_generation,
        "calculation",
    )
    for widget in (
        window.loadcell_tab.known_mass_spin,
        window.loadcell_tab.capture_unloaded_button,
        window.loadcell_tab.capture_loaded_button,
        window.loadcell_tab.calculate_calibration_button,
        window.loadcell_tab.tare_button,
        window.loadcell_tab.verify_calibration_button,
        window.loadcell_tab.save_calibration_button,
        window.loadcell_tab.load_calibration_button,
    ):
        assert not widget.isEnabled()

    old_generation = window._loadcell_workflow_generation
    result = pending["function"](*pending["args"], **pending["kwargs"])
    window._start_sample_window("unloaded")
    assert window._loadcell_workflow_generation == old_generation + 1
    assert window._loadcell_pending_job is None
    assert window._sample_collector.active

    pending["succeeded"](result)
    pending["failed"]("stale calculation failure")
    assert window._calibration is None
    assert not window.readiness.loadcell_calibrated
    assert "stale calculation failure" not in window.loadcell_tab.status_label.text()
    window._cancel_sample_window("test complete")
    window.close()


def test_delayed_verification_cannot_commit_or_report_after_disconnect(
    qtbot, tmp_path: Path
) -> None:
    window = _connected_window(qtbot, tmp_path)
    calibration = _saved_calibration(verified=False, retared=True)
    _install_calibration(window, calibration)
    pending: dict[str, object] = {}

    def delay(function, succeeded, failed, *args, **kwargs):
        pending.update(
            function=function,
            succeeded=succeeded,
            failed=failed,
            args=args,
            kwargs=kwargs,
        )

    window._run_job = delay
    window._verify_calibration(LOADED)
    assert window._loadcell_pending_job is not None
    result = pending["function"](*pending["args"], **pending["kwargs"])
    old_generation = window._loadcell_workflow_generation

    window._serial_disconnected()
    disconnected_status = window.loadcell_tab.status_label.text()
    assert window._loadcell_workflow_generation == old_generation + 1
    assert window._loadcell_pending_job is None

    pending["succeeded"](result)
    pending["failed"]("stale verification failure")
    assert window._calibration is calibration
    assert window._calibration.verification is None
    assert not window.readiness.calibration_verified
    assert window.loadcell_tab.status_label.text() == disconnected_status
    window.close()


def test_sample_window_interleaving_mass_reset_disconnect_and_clean_restart(
    qtbot, tmp_path: Path
) -> None:
    window = _connected_window(qtbot, tmp_path)
    window._start_sample_window("unloaded")
    generation = window._loadcell_workflow_generation
    assert window._sample_collector.progress == ("unloaded", 0, 0.0, 0.05)
    for widget in (
        window.loadcell_tab.disconnect_button,
        window.loadcell_tab.known_mass_spin,
        window.loadcell_tab.capture_unloaded_button,
        window.loadcell_tab.capture_loaded_button,
        window.loadcell_tab.calculate_calibration_button,
        window.loadcell_tab.tare_button,
        window.loadcell_tab.verify_calibration_button,
        window.loadcell_tab.save_calibration_button,
        window.loadcell_tab.load_calibration_button,
    ):
        assert not widget.isEnabled()

    window._start_sample_window("loaded")
    assert window._sample_collector.progress == ("unloaded", 0, 0.0, 0.05)
    assert window._loadcell_workflow_generation == generation

    window.loadcell_tab.set_known_mass_g(250.0)
    window._known_mass_changed(250.0)
    assert window._loadcell_workflow_generation == generation + 1
    assert not window._sample_collector.active
    assert window._sample_collector.progress is None
    assert window.loadcell_tab.capture_unloaded_button.isEnabled()

    window._start_sample_window("unloaded")
    window._sample_collector.accept(
        SimpleNamespace(host_monotonic_ns=5_000_000_000, raw_adc=100_000)
    )
    window._serial_disconnected()
    assert not window._sample_collector.active
    assert window._sample_collector.take_completed() is None
    assert not window.loadcell_tab.capture_unloaded_button.isEnabled()

    window._serial_connected(_serial_info("serial-session-b"))
    assert not window.loadcell_tab.capture_unloaded_button.isEnabled()
    window._serial_health_changed(True, "", "Fresh HX711 DATA stream is healthy.")
    assert window.loadcell_tab.capture_unloaded_button.isEnabled()
    window._start_sample_window("unloaded")
    assert window._sample_collector.progress == ("unloaded", 0, 0.0, 0.05)
    window._cancel_sample_window("test complete")
    window.close()


def test_hello_not_ready_recovery_and_timeout_gate_all_sample_actions(
    qtbot, tmp_path: Path
) -> None:
    window = _connected_window(qtbot, tmp_path, fresh_data=False)
    assert window._serial_transport_connected
    assert not window.readiness.serial_connected
    assert not window.loadcell_tab.capture_unloaded_button.isEnabled()

    generation = window._loadcell_workflow_generation
    window._serial_health_changed(
        False,
        ErrorCode.HX711_NOT_READY.value,
        "HX711 is not ready; waiting for DATA.",
    )
    assert window._loadcell_workflow_generation == generation + 1
    assert not window.loadcell_tab.capture_unloaded_button.isEnabled()

    window._serial_health_changed(True, "", "Fresh HX711 DATA stream is healthy.")
    assert window.readiness.serial_connected
    assert window.loadcell_tab.capture_unloaded_button.isEnabled()
    window._start_sample_window("unloaded")
    assert window._sample_collector.active

    generation = window._loadcell_workflow_generation
    window._serial_health_changed(
        False,
        ErrorCode.HX711_TIMEOUT.value,
        "HX711 produced no fresh conversion within the firmware timeout.",
    )
    assert window._loadcell_workflow_generation == generation + 1
    assert not window._sample_collector.active
    assert not window.readiness.serial_connected
    assert not window.loadcell_tab.capture_unloaded_button.isEnabled()

    window._serial_health_changed(True, "", "Fresh HX711 DATA stream is healthy.")
    assert window.loadcell_tab.capture_unloaded_button.isEnabled()
    window.close()


def test_retare_window_and_pending_jobs_interlock_recording_until_verified(
    qtbot, tmp_path: Path
) -> None:
    window = _connected_window(qtbot, tmp_path)
    calibration = _saved_calibration(verified=True)
    _install_calibration(window, calibration)
    window.recording_tab.session_id_edit.setText("transaction-session")
    window.recording_tab.trial_id_edit.setText("transaction-trial")
    window.recording_tab.sensing_skin_id_edit.setText("transaction-skin")
    for field in window.readiness.REQUIRED_FIELDS:
        setattr(window.readiness, field, True)
    window._refresh_readiness()
    assert window.recording_tab.start_button.isEnabled()

    delayed: list[dict[str, object]] = []

    def delay(function, succeeded, failed, *args, **kwargs):
        delayed.append(
            {
                "function": function,
                "succeeded": succeeded,
                "failed": failed,
                "args": args,
                "kwargs": kwargs,
            }
        )

    window._run_job = delay
    window._start_sample_window("tare")
    assert window._calibration is not None
    assert window._calibration.verification is None
    assert not window.readiness.calibration_verified
    assert not window.recording_tab.start_button.isEnabled()
    window._request_recording(window.recording_tab.trial_labels())
    assert not window._preparing_recording
    assert "load-cell sample window" in window.recording_tab.status_label.text()

    _complete_window(window, UNLOADED)
    assert window._loadcell_pending_job is not None
    window.readiness.calibration_verified = True
    window._refresh_readiness()
    assert not window.recording_tab.start_button.isEnabled()
    window._request_recording(window.recording_tab.trial_labels())
    assert "calibration operation" in window.recording_tab.status_label.text()

    tare_job = delayed.pop(0)
    tare_job["succeeded"](
        tare_job["function"](*tare_job["args"], **tare_job["kwargs"])
    )
    assert window.loadcell_tab.workflow_stage == "tared"
    assert not window.readiness.calibration_verified

    window._start_sample_window("verify")
    _complete_window(window, LOADED)
    assert window._loadcell_pending_job is not None
    window.readiness.calibration_verified = True
    window._refresh_readiness()
    assert not window.recording_tab.start_button.isEnabled()
    verify_job = delayed.pop(0)
    verify_job["succeeded"](
        verify_job["function"](*verify_job["args"], **verify_job["kwargs"])
    )
    assert window.readiness.calibration_verified
    assert window.loadcell_tab.workflow_stage == "verified"
    assert window.recording_tab.start_button.isEnabled()
    window.close()
