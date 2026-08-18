"""Post-processing report aggregation, export, and GUI-capture tests."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from core.experimental_live_sensor import LiveSensorResult
from core.live_sensor_report import (
    EventReportRecord,
    summarize_by_roi,
    write_post_processing_report,
)
from gui.live_sensor_tab import LiveSensorTab


PROJECT = Path(__file__).resolve().parents[1]
HYBRID_BUNDLE = PROJECT / "models" / "live_sensor_experimental_hybrid_v4"


def _result(
    event_id: int,
    roi: int,
    *,
    accepted: bool = True,
    peak_v: float | None = 210.0,
    force_at_v: float | None = 2.25,
    peak_force: float | None = 2.60,
    rois: tuple[int, ...] | None = None,
) -> LiveSensorResult:
    detected_rois = rois or (roi,)
    return LiveSensorResult(
        frame_id=event_id * 100 + 20,
        state="EVENT_COMPLETE" if accepted else "EVENT_COMPLETE_UNCERTAIN_ROI",
        message="complete",
        contact=False,
        force_N=None,
        roi=roi if accepted else None,
        contact_score=0.4,
        threshold=0.1,
        confidence="higher" if accepted else "tentative",
        forced_roi=roi,
        sensor_mode="hybrid_force_event",
        event_id=event_id,
        event_phase="completed",
        peak_signal_ratio=4.0,
        peak_force_N=peak_force,
        force_status="available" if peak_force is not None else "above_range",
        peak_contact_score=0.4,
        peak_hsv_v=peak_v,
        force_at_peak_hsv_v_N=force_at_v,
        peak_delta_v=42.0,
        force_at_peak_delta_v_N=2.4,
        event_start_frame_id=event_id * 100,
        event_end_frame_id=event_id * 100 + 19,
        event_completion_reason="contact_cleared",
        rois=detected_rois if accepted else (),
        forced_rois=detected_rois,
    )


def _record(event_id: int, roi: int, **kwargs: object) -> EventReportRecord:
    return EventReportRecord.from_result(
        _result(event_id, roi, **kwargs),
        completed_at=datetime(2026, 8, 17, 1, event_id, tzinfo=UTC),
    )


def test_roi_summary_counts_every_press_and_preserves_withheld_status() -> None:
    events = [
        _record(1, 2, peak_v=201.0, force_at_v=2.0, peak_force=2.2),
        _record(2, 2, accepted=False, peak_v=240.0, force_at_v=2.4, peak_force=2.8),
        _record(3, 7, peak_v=180.0, force_at_v=None, peak_force=None),
    ]
    summary = summarize_by_roi(events)
    assert len(summary) == 9
    roi2 = summary[1]
    assert roi2["press_count"] == 2
    assert roi2["accepted_count"] == 1
    assert roi2["withheld_count"] == 1
    assert roi2["max_peak_hsv_v"] == 240.0
    assert roi2["mean_force_at_peak_hsv_v_N"] == pytest.approx(2.2)
    assert roi2["max_event_peak_force_N"] == 2.8
    assert summary[6]["press_count"] == 1
    assert sum(int(row["press_count"]) for row in summary) == 3


def test_roi_summary_counts_each_rod_in_one_simultaneous_event() -> None:
    event = _record(
        1,
        2,
        rois=(2, 5, 9),
        peak_force=None,
        force_at_v=None,
    )
    assert event.forced_rois == (2, 5, 9)
    assert event.reported_rois == (2, 5, 9)
    summary = summarize_by_roi([event])
    assert [summary[index - 1]["press_count"] for index in (2, 5, 9)] == [1, 1, 1]
    assert sum(int(row["press_count"]) for row in summary) == 3
    assert summary[1]["press_share_percent"] == pytest.approx(100 / 3)


def test_report_export_is_self_contained_machine_readable_and_explicit(tmp_path: Path) -> None:
    events = [
        _record(1, 1),
        _record(2, 4, accepted=False, peak_v=None, force_at_v=None, peak_force=None),
    ]
    paths = write_post_processing_report(
        tmp_path / "session_report",
        events,
        bundle_id="experimental-hybrid-test",
    )
    assert paths.html.is_file()
    assert paths.event_csv.is_file()
    assert paths.roi_csv.is_file()
    assert paths.json.is_file()

    with paths.event_csv.open(encoding="utf-8", newline="") as handle:
        event_rows = list(csv.DictReader(handle))
    assert len(event_rows) == 2
    assert event_rows[0]["peak_hsv_v"] == "210.000000"
    assert event_rows[0]["force_at_peak_hsv_v_N"] == "2.250000"
    assert event_rows[1]["localization_status"] == "withheld"
    assert event_rows[1]["peak_hsv_v"] == ""

    payload = json.loads(paths.json.read_text(encoding="utf-8"))
    assert payload["inference_inputs"]["load_cell"] == "not_used"
    assert payload["inference_inputs"]["printer"] == "not_used"
    assert payload["roi_summary"][0]["press_count"] == 1
    html = paths.html.read_text(encoding="utf-8")
    assert "Press count by ROI" in html
    assert "Force @ peak V" in html
    assert "Experimental and not physically validated" in html
    assert "No load-cell or printer input" in html
    assert "nan" not in html.lower()


def test_report_rejects_empty_and_duplicate_event_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        write_post_processing_report(tmp_path / "empty", [], bundle_id="test")
    duplicate = [_record(1, 1), _record(1, 2)]
    with pytest.raises(ValueError, match="unique"):
        write_post_processing_report(tmp_path / "duplicate", duplicate, bundle_id="test")


def test_live_tab_collects_completed_events_and_exports_without_hardware(qtbot, tmp_path: Path) -> None:
    tab = LiveSensorTab(HYBRID_BUNDLE)
    qtbot.addWidget(tab)
    tab.start_demo()
    tab._demo_timer.stop()
    while tab._demo_mode:
        tab._advance_demo()
    assert len(tab._report_events) == 1
    assert tab.export_report_button.isEnabled()
    assert "1 completed press" in tab.report_status_label.text()
    assert tab._last_completed_result is not None
    tab._render(tab._last_completed_result)
    assert len(tab._report_events) == 1
    paths = tab.export_post_processing_report(tmp_path, notify=False)
    assert paths is not None and paths.html.is_file()
    tab.clear_report_events(confirm=False)
    assert not tab._report_events
    assert not tab.export_report_button.isEnabled()
