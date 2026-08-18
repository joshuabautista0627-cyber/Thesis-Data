from __future__ import annotations

import math
import time

import pytest

from core.models import ErrorCode, LoadCellSample
from core.motion import MotionContext
from services.repeated_press_controller import (
    RepeatedPressConfig,
    RepeatedPressController,
    RepeatedPressState,
)
from services.printer_service import PrinterSafetyError
from test_printer_service import connected_service, home_and_zero


def config(**changes) -> RepeatedPressConfig:
    values = {
        "cycles": 2,
        "displacement_mm": -1.0,
        "down_feed_mm_min": 60.0,
        "up_feed_mm_min": 180.0,
        "bottom_hold_s": 0.0,
        "top_dwell_s": 0.0,
        "pre_roll_s": 0.0,
        "post_roll_s": 0.0,
        "force_limit_N": 10.0,
        "negative_z_direction_confirmed": True,
    }
    values.update(changes)
    return RepeatedPressConfig(**values)


def calibrated_sample(force_N: float, *, valid: bool = True) -> LoadCellSample:
    return LoadCellSample(
        host_monotonic_ns=time.perf_counter_ns(),
        arduino_sample_id=1,
        arduino_micros=1,
        raw_adc=100.0,
        force_gf=force_N / 0.00980665,
        force_N=force_N,
        device_session_id="device-session",
        loadcell_valid=valid,
        error_code=ErrorCode.NONE if valid else ErrorCode.LOAD_CELL_UNCALIBRATED,
    )


def prepared_controller(**callbacks):
    service, transport = connected_service()
    service.home_all()
    service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
    service.set_press_zero_here()
    context = MotionContext()
    results = []
    controller = RepeatedPressController(
        service,
        context,
        completed_callback=results.append,
        **callbacks,
    )
    return service, transport, context, controller, results


def test_normal_cycles_use_one_continuous_g1_down_and_up_each() -> None:
    service, transport, _, controller, results = prepared_controller()
    try:
        before = len(transport.commands)
        controller.start(config(cycles=3))
        assert controller.wait(2.0)
        result = results[0]
        assert result.status == "complete"
        assert result.completed_cycles == 3
        sequence_g1 = [command for command in transport.commands[before:] if command.startswith("G1")]
        assert len(sequence_g1) == 1 + 2 * 3  # one move-to-start plus down/up per cycle
        assert sum("Z9.000" in command for command in sequence_g1) == 3
        assert sum("Z10.000" in command for command in sequence_g1) == 4
    finally:
        service.disconnect()


def test_state_machine_emits_required_order_and_unique_cycle_ids() -> None:
    states = []
    service, _, _, controller, _ = prepared_controller(state_callback=states.append)
    try:
        controller.start(config(cycles=2))
        assert controller.wait(2.0)
        names = [item["state"] for item in states]
        assert names[:3] == ["verifying_readiness", "pre_roll", "move_to_start"]
        assert names.count("pressing_down") == 2
        assert names.count("holding") == 2
        assert names.count("retracting") == 2
        assert names[-2:] == ["post_roll", "complete"]
        cycle_ids = {
            item["snapshot"]["motion_cycle_id"]
            for item in states
            if item["state"] == "pressing_down"
        }
        assert len(cycle_ids) == 2 and "" not in cycle_ids
    finally:
        service.disconnect()


@pytest.mark.parametrize(
    "changes",
    [
        {"displacement_mm": 0.1},
        {"negative_z_direction_confirmed": False},
        {"down_feed_mm_min": 0.1},
        {"up_feed_mm_min": 9999.0},
        {"force_limit_N": 0.001},
    ],
)
def test_invalid_or_unconfirmed_sequences_are_blocked_before_thread_start(changes) -> None:
    service, transport, _, controller, _ = prepared_controller()
    try:
        before = len(transport.commands)
        with pytest.raises(ValueError):
            controller.start(config(**changes))
        assert not controller.running
        assert len(transport.commands) == before
    finally:
        service.disconnect()


def test_computed_machine_target_must_remain_inside_z_limits() -> None:
    service, _, _, controller, _ = prepared_controller()
    try:
        service.home_all()
        service.set_press_zero_here()
        # Homing sets press zero at machine Z=0; any negative displacement is unsafe.
        with pytest.raises(ValueError, match="outside configured machine limits"):
            controller.preview(config(displacement_mm=-0.1))
    finally:
        service.disconnect()


def test_preview_reports_relative_target_and_duration_without_moving() -> None:
    service, transport, _, controller, _ = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        before = len(transport.commands)
        values = controller.preview(config(cycles=4, displacement_mm=-2.0))
        assert values["press_zero_z_mm"] == 10.0
        assert values["target_machine_z_mm"] == 8.0
        assert values["estimated_duration_s"] > 0
        assert len(transport.commands) == before
    finally:
        service.disconnect()


def test_readiness_failure_produces_error_result_without_g1() -> None:
    service, transport, _, controller, results = prepared_controller(
        readiness_callback=lambda: (False, "camera unavailable")
    )
    try:
        before = sum(item.startswith("G1") for item in transport.commands)
        controller.start(config(displacement_mm=-0.0 - 0.001))
        assert controller.wait(2.0)
        assert results[0].status == "error"
        assert "camera unavailable" in results[0].error
        assert sum(item.startswith("G1") for item in transport.commands) == before
    finally:
        service.disconnect()


def test_optional_tare_and_baseline_callbacks_run_before_motion() -> None:
    calls = []
    service, transport, _, controller, results = prepared_controller(
        tare_callback=lambda: calls.append("tare"),
        baseline_callback=lambda: calls.append("baseline"),
    )
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        controller.start(
            config(
                cycles=1,
                tare_before_sequence=True,
                fresh_baseline_before_sequence=True,
            )
        )
        assert controller.wait(2.0)
        assert calls == ["tare", "baseline"]
        assert results[0].status == "complete"
        assert any(item["event"] == "state" and item["state"] == "optional_tare" for item in results[0].events)
    finally:
        service.disconnect()


def test_pause_finishes_safe_retract_then_waits_before_next_cycle() -> None:
    service, _, _, controller, results = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        controller.start(config(cycles=2, bottom_hold_s=0.12))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.HOLDING and time.time() < deadline:
            time.sleep(0.005)
        controller.pause()
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.PAUSED and time.time() < deadline:
            time.sleep(0.005)
        assert controller.state is RepeatedPressState.PAUSED
        assert service.position.tracked_z_mm == pytest.approx(10.0)
        controller.resume()
        assert controller.wait(2.0)
        assert results[0].completed_cycles == 2
    finally:
        service.disconnect()


def test_stop_interrupts_sequence_and_returns_to_press_zero() -> None:
    service, _, _, controller, results = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        controller.start(config(cycles=10, bottom_hold_s=0.2))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.HOLDING and time.time() < deadline:
            time.sleep(0.005)
        controller.stop()
        assert controller.wait(2.0)
        assert results[0].status == "stopped"
        assert service.position.tracked_z_mm == pytest.approx(10.0)
    finally:
        service.disconnect()


def test_stop_during_inter_cycle_dwell_returns_safely() -> None:
    service, _, _, controller, results = prepared_controller()
    try:
        controller.start(config(cycles=3, top_dwell_s=0.3))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.INTER_CYCLE_DWELL and time.time() < deadline:
            time.sleep(0.005)
        assert controller.state is RepeatedPressState.INTER_CYCLE_DWELL
        controller.stop()
        assert controller.wait(2.0)
        assert results[0].status == "stopped"
        assert service.position.tracked_z_mm == pytest.approx(10.0)
    finally:
        service.disconnect()


def test_stop_during_downward_move_interrupts_then_retracts() -> None:
    service, transport, _, controller, results = prepared_controller()
    try:
        controller.state_callback = lambda payload: (
            transport.delayed.add("G1")
            if payload["state"] == "pressing_down"
            else None
        )
        controller.start(config(cycles=3))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.PRESSING_DOWN and time.time() < deadline:
            time.sleep(0.005)
        assert controller.state is RepeatedPressState.PRESSING_DOWN
        transport.delayed.discard("G1")
        controller.stop()
        assert controller.wait(2.0)
        assert results[0].status == "stopped"
        assert any(command["gcode"] == "M410" for command in results[0].commands)
        assert service.position.tracked_z_mm == pytest.approx(10.0)
    finally:
        service.disconnect()


def test_stop_during_upward_move_interrupts_then_finishes_at_zero() -> None:
    service, transport, _, controller, results = prepared_controller()
    try:
        controller.start(config(cycles=3, bottom_hold_s=0.1))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.HOLDING and time.time() < deadline:
            time.sleep(0.005)
        transport.delayed.add("G1")
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.RETRACTING and time.time() < deadline:
            time.sleep(0.005)
        assert controller.state is RepeatedPressState.RETRACTING
        transport.delayed.discard("G1")
        controller.stop()
        assert controller.wait(2.0)
        assert results[0].status == "stopped"
        assert service.position.tracked_z_mm == pytest.approx(10.0)
    finally:
        service.disconnect()


def test_abort_cancels_and_attempts_safe_retract() -> None:
    service, _, _, controller, results = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        controller.start(config(cycles=10, bottom_hold_s=0.2))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.HOLDING and time.time() < deadline:
            time.sleep(0.005)
        controller.abort()
        assert controller.wait(2.0)
        assert results[0].status == "aborted"
        assert any(item["event"] == "safe_retract_completed" for item in results[0].events)
    finally:
        service.disconnect()


def test_printer_disconnect_mid_sequence_ends_error_without_stale_completion() -> None:
    service, _, _, controller, results = prepared_controller()
    try:
        controller.start(config(cycles=10, bottom_hold_s=0.2))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.HOLDING and time.time() < deadline:
            time.sleep(0.005)
        service.disconnect()
        assert controller.wait(2.0)
        assert results[0].status == "error"
        assert "connection" in results[0].error or "Marlin" in results[0].error
        assert not service.position.press_zero_valid
    finally:
        service.disconnect()


def test_calibrated_force_limit_aborts_live_and_records_maximum() -> None:
    service, _, context, controller, results = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        controller.start(config(cycles=10, bottom_hold_s=0.2, force_limit_N=2.0))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.HOLDING and time.time() < deadline:
            time.sleep(0.005)
        controller.observe_force(calibrated_sample(2.0))
        assert controller.wait(2.0)
        result = results[0]
        assert result.status == "aborted"
        assert result.force_limit_exceeded
        assert result.maximum_abs_force_N == pytest.approx(2.0)
        assert context.snapshot().force_limit_exceeded
        assert any(command["gcode"] == "M410" for command in result.commands)
        assert any(
            event["event"] == "force_quick_stop_requested"
            and event["observer_request_latency_ms"] >= 0.0
            for event in result.events
        )
    finally:
        service.disconnect()


def test_invalid_or_uncalibrated_force_never_trips_limit() -> None:
    service, _, _, controller, results = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        controller.start(config(cycles=1, bottom_hold_s=0.05, force_limit_N=1.0))
        controller.observe_force(calibrated_sample(100.0, valid=False))
        controller.observe_force(type("Sample", (), {"loadcell_valid": True, "force_N": math.nan})())
        assert controller.wait(2.0)
        assert results[0].status == "complete"
        assert not results[0].force_limit_exceeded
    finally:
        service.disconnect()


def test_sequence_generation_increments_and_results_do_not_cross_runs() -> None:
    service, _, _, controller, results = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        first = controller.start(config(cycles=1))
        assert controller.wait(2.0)
        second = controller.start(config(cycles=1))
        assert controller.wait(2.0)
        assert second == first + 1
        assert [item.generation for item in results] == [first, second]
        assert results[0].sequence_id != results[1].sequence_id
    finally:
        service.disconnect()


def test_motion_context_contains_only_actual_m114_reported_coordinates() -> None:
    service, _, context, controller, _ = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        controller.start(config(cycles=1, bottom_hold_s=0.05))
        deadline = time.time() + 1.0
        while controller.state is not RepeatedPressState.HOLDING and time.time() < deadline:
            time.sleep(0.005)
        snapshot = context.snapshot()
        assert snapshot.commanded_z_mm == pytest.approx(9.0)
        assert snapshot.printer_position_valid
        assert snapshot.printer_reported_z_mm == pytest.approx(10.0)
        assert controller.wait(2.0)
    finally:
        service.disconnect()


def test_hold_timing_and_result_metadata_are_monotonic_and_complete() -> None:
    service, _, _, controller, results = prepared_controller()
    try:
        controller.start(config(cycles=1, bottom_hold_s=0.06))
        assert controller.wait(2.0)
        result = results[0]
        states = [event for event in result.events if event["event"] == "state"]
        times = [event["host_monotonic_ns"] for event in states]
        assert times == sorted(times)
        holding = next(event for event in states if event["state"] == "holding")
        retracting = next(event for event in states if event["state"] == "retracting")
        assert retracting["host_monotonic_ns"] - holding["host_monotonic_ns"] >= 50_000_000
        exported = result.to_dict()
        assert {
            "sequence_id",
            "generation",
            "status",
            "configured_cycles",
            "completed_cycles",
            "force_limit_exceeded",
            "maximum_abs_force_N",
            "error",
            "started_monotonic_ns",
            "finished_monotonic_ns",
            "events",
            "commands",
        } == set(exported)
    finally:
        service.disconnect()


def test_no_sequence_command_uses_g92() -> None:
    service, transport, _, controller, _ = prepared_controller()
    try:
        service.move_absolute(z_mm=10.0, feed_rate_mm_min=100.0)
        service.set_press_zero_here()
        controller.start(config(cycles=2))
        assert controller.wait(2.0)
        assert not any(command.startswith("G92") for command in transport.commands)
    finally:
        service.disconnect()
