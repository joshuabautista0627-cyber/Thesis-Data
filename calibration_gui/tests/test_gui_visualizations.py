"""Offscreen contracts for ROI, baseline, recording, and optical graphs."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QSignalSpy

from core.models import ReadinessFlags, RecordingLifecycle
from gui.recording_tab import PERFORMANCE_FIELDS, REVIEW_FIELDS, RecordingTab
from gui.roi_baseline_tab import (
    GRAPH_VIEWS,
    OpticalGraphPanel,
    ROIBaselineTab,
)


def _show(qtbot, widget, *, width: int = 900, height: int = 650) -> None:
    qtbot.addWidget(widget)
    widget.resize(width, height)
    widget.show()
    qtbot.wait(10)


def _complete_required_labels(tab: RecordingTab) -> None:
    tab.session_id_edit.setText("session-001")
    tab.trial_id_edit.setText("trial-001")
    tab.sensing_skin_id_edit.setText("skin-001")


def _all_ready() -> dict[str, bool]:
    return {name: True for name in ReadinessFlags.REQUIRED_FIELDS}


def test_roi_tab_has_exactly_nine_ordered_editable_overlays(qtbot) -> None:
    tab = ROIBaselineTab(frame_size=(120, 90))
    _show(qtbot, tab)

    assert tab.instructions_label.wordWrap()
    assert len(tab.roi_items) == len(tab.roi_labels) == 9
    assert tuple(roi.roi_id for roi in tab.rois) == tuple(range(1, 10))
    assert tuple(label.toPlainText() for label in tab.roi_labels) == tuple(
        f"ROI {index}" for index in range(1, 10)
    )
    assert all(isinstance(item, pg.RectROI) for item in tab.roi_items)
    assert all(item.translatable for item in tab.roi_items)
    assert all(item.getHandles() for item in tab.roi_items)
    assert set(tab.roi_editors) == set(range(1, 10))
    assert all(
        set(editors) == {"x", "y", "width", "height"}
        for editors in tab.roi_editors.values()
    )
    assert tab.draw_roi_selector.count() == 9


def test_roi_draw_mode_replaces_selected_rectangle_without_adding_a_tenth(qtbot) -> None:
    tab = ROIBaselineTab(frame_size=(120, 90))
    _show(qtbot, tab)
    changed_spy = QSignalSpy(tab.roi_layout_changed)
    drawn_spy = QSignalSpy(tab.preview_view_box.rectangle_drawn)
    tab.draw_roi_selector.setCurrentIndex(4)

    qtbot.mouseClick(tab.draw_selected_roi_button, Qt.LeftButton)
    assert tab.preview_view_box.draw_enabled

    class FinishedDrag:
        accepted = False

        @staticmethod
        def button():
            return Qt.MouseButton.LeftButton

        @staticmethod
        def buttonDownPos():
            return QPointF(50.0, 40.0)

        @staticmethod
        def pos():
            return QPointF(150.0, 110.0)

        @staticmethod
        def isFinish():
            return True

        def accept(self):
            self.accepted = True

    drag = FinishedDrag()
    tab.preview_view_box.mouseDragEvent(drag)

    assert drag.accepted
    assert drawn_spy.count() == 1
    assert not tab.preview_view_box.draw_enabled
    assert len(tab.roi_items) == len(tab.rois) == 9
    assert tab.rois[4].roi_id == 5
    assert tab.rois[4].width > 0 and tab.rois[4].height > 0
    assert tab.rois[4].is_inside(120, 90)
    assert changed_spy.count() == 1


def test_roi_numeric_overlay_copy_validation_and_invalidation(qtbot) -> None:
    tab = ROIBaselineTab(frame_size=(120, 90))
    _show(qtbot, tab)
    changed_spy = QSignalSpy(tab.roi_layout_changed)
    invalidated_spy = QSignalSpy(tab.baseline_invalidated)
    valid_spy = QSignalSpy(tab.rois_valid_changed)

    tab.roi_editors[1]["width"].setValue(26)
    tab.roi_editors[1]["height"].setValue(20)
    qtbot.mouseClick(tab.copy_roi1_size_button, Qt.LeftButton)
    assert {(roi.width, roi.height) for roi in tab.rois} == {(26, 20)}

    first = tab.rois[0]
    tab.roi_editors[2]["x"].setValue(first.x)
    tab.roi_editors[2]["y"].setValue(first.y)
    assert tab.validation.valid
    assert tab.validation.overlap_pairs
    assert "warning" in tab.validation_label.text().lower()

    tab.roi_items[0].setPos((1, 1), finish=True)
    qtbot.waitUntil(
        lambda: tab.roi_editors[1]["x"].value() == 1
        and tab.roi_editors[1]["y"].value() == 1
    )
    assert changed_spy.count() >= 5
    assert invalidated_spy.count() == changed_spy.count()
    assert valid_spy.count() == changed_spy.count()
    assert invalidated_spy.at(invalidated_spy.count() - 1)[0] == "ROI layout changed"


def test_roi_layout_baseline_and_graph_buttons_emit_requests_only(qtbot) -> None:
    tab = ROIBaselineTab(frame_size=(120, 90))
    _show(qtbot, tab)
    save_layout_spy = QSignalSpy(tab.save_roi_layout_requested)
    load_layout_spy = QSignalSpy(tab.load_roi_layout_requested)
    capture_spy = QSignalSpy(tab.baseline_capture_requested)
    accept_spy = QSignalSpy(tab.baseline_accept_requested)
    repeat_spy = QSignalSpy(tab.baseline_repeat_requested)

    qtbot.mouseClick(tab.save_layout_button, Qt.LeftButton)
    qtbot.mouseClick(tab.load_layout_button, Qt.LeftButton)
    assert save_layout_spy.count() == load_layout_spy.count() == 1
    assert tuple(roi.roi_id for roi in save_layout_spy.at(0)[0].rois) == tuple(
        range(1, 10)
    )

    qtbot.mouseClick(tab.capture_baseline_button, Qt.LeftButton)
    assert capture_spy.count() == 1
    assert not tab.roi_edit_group.isEnabled()
    tab.set_baseline_capture_progress(42.4, 17)
    assert tab.baseline_progress.value() == 42
    assert "17 valid frames" in tab.baseline_progress.format()
    tab.set_baseline_result_available(True, "Stable baseline is ready for review.")
    assert tab.accept_baseline_button.isEnabled()
    assert tab.repeat_baseline_button.isEnabled()
    qtbot.mouseClick(tab.accept_baseline_button, Qt.LeftButton)
    qtbot.mouseClick(tab.repeat_baseline_button, Qt.LeftButton)
    assert accept_spy.count() == repeat_spy.count() == 1
    assert capture_spy.count() == 2


def test_roi_lock_disables_geometry_and_baseline_but_keeps_graphs_available(qtbot) -> None:
    tab = ROIBaselineTab(frame_size=(120, 90))
    _show(qtbot, tab)
    tab.set_baseline_result_available(True)
    tab.set_recording_locked(True)

    assert not tab.roi_edit_group.isEnabled()
    assert not tab.capture_baseline_button.isEnabled()
    assert not tab.accept_baseline_button.isEnabled()
    assert not tab.repeat_baseline_button.isEnabled()
    assert all(not item.translatable for item in tab.roi_items)
    assert not tab.draw_selected_roi_button.isEnabled()
    assert all(
        not handle.isVisible()
        for item in tab.roi_items
        for handle in item.getHandles()
    )
    assert tab.graph_panel.view_selector.isEnabled()


def test_graph_views_use_same_exact_values_and_fixed_scale(qtbot) -> None:
    panel = OpticalGraphPanel(scale_min=-10.0, scale_max=300.0)
    _show(qtbot, panel)
    values = tuple(float(index) + 0.125 for index in range(1, 10))
    panel.set_current_values(values, capture_frame_id=123, elapsed_time_s=4.25)

    assert tuple(
        panel.view_selector.itemText(index)
        for index in range(panel.view_selector.count())
    ) == GRAPH_VIEWS
    assert panel.current_values == panel.spatial_profile_values == values
    np.testing.assert_array_equal(panel.heatmap_matrix, np.asarray(values).reshape(3, 3))
    np.testing.assert_array_equal(panel.heatmap_image.image, np.asarray(values).reshape(3, 3))
    profile_x, profile_y = panel.spatial_profile_curve.getData()
    np.testing.assert_array_equal(profile_x, np.arange(1, 10))
    np.testing.assert_array_equal(profile_y, np.asarray(values))
    np.testing.assert_array_equal(panel.heatmap_image.getLevels(), (-10.0, 300.0))
    assert panel.temporal_plot.getViewBox().viewRange()[1] == pytest.approx(
        [-10.0, 300.0]
    )
    assert panel.profile_plot.getViewBox().viewRange()[1] == pytest.approx(
        [-10.0, 300.0]
    )
    assert "123" in panel.profile_context_label.text()
    assert "4.250 s" in panel.profile_context_label.text()


def test_temporal_graph_reuses_nine_curves_and_bounds_authoritative_history(qtbot) -> None:
    panel = OpticalGraphPanel(history_seconds=2.0, max_history_points=5)
    _show(qtbot, panel)
    curve_identities = tuple(id(curve) for curve in panel.temporal_curves)

    for index in range(20):
        panel.append_temporal_values(
            capture_frame_id=1000 + index,
            elapsed_time_s=index * 0.5,
            values=tuple(index * 10.0 + roi_id for roi_id in range(1, 10)),
        )

    assert len(panel.temporal_curves) == 9
    assert tuple(id(curve) for curve in panel.temporal_curves) == curve_identities
    assert len(panel.temporal_history) == 5
    assert [row.capture_frame_id for row in panel.temporal_history] == list(
        range(1015, 1020)
    )
    assert [row.elapsed_time_s for row in panel.temporal_history] == pytest.approx(
        [7.5, 8.0, 8.5, 9.0, 9.5]
    )
    for roi_index, curve in enumerate(panel.temporal_curves):
        x_values, y_values = curve.getData()
        assert x_values == pytest.approx([7.5, 8.0, 8.5, 9.0, 9.5])
        assert y_values == pytest.approx(
            [index * 10.0 + roi_index + 1 for index in range(15, 20)]
        )

    qtbot.mouseClick(panel.hide_all_button, Qt.LeftButton)
    assert not any(curve.isVisible() for curve in panel.temporal_curves)
    qtbot.mouseClick(panel.show_all_button, Qt.LeftButton)
    assert all(curve.isVisible() for curve in panel.temporal_curves)
    panel.roi_visibility_checkboxes[3].setChecked(False)
    assert not panel.temporal_curves[3].isVisible()
    with pytest.raises(ValueError, match="cannot move backward"):
        panel.append_temporal_values(1020, 9.0, (0.0,) * 9)


def test_graph_save_signals_include_exact_displayed_snapshot(qtbot) -> None:
    panel = OpticalGraphPanel()
    _show(qtbot, panel)
    values = tuple(float(index) for index in range(9))
    panel.set_current_values(values, capture_frame_id=5, elapsed_time_s=1.5)
    panel.append_temporal_values(5, 1.5, values)
    spatial_spy = QSignalSpy(panel.save_spatial_graph_requested)
    line_spy = QSignalSpy(panel.save_line_graph_requested)

    qtbot.mouseClick(panel.save_spatial_button, Qt.LeftButton)
    assert spatial_spy.count() == 1
    assert spatial_spy.at(0)[0]["values"] == values
    panel.view_selector.setCurrentText("Temporal ROI Lines")
    qtbot.mouseClick(panel.save_line_button, Qt.LeftButton)
    assert line_spy.count() == 1
    snapshot = line_spy.at(0)[0]
    assert snapshot["view"] == "Temporal ROI Lines"
    assert snapshot["temporal_history"] == panel.temporal_history
    assert tuple(snapshot["x_axis_limits"]) == pytest.approx((0.0, 30.0))
    assert tuple(snapshot["y_axis_limits"]) == pytest.approx((0.0, 255.0))


def test_recording_readiness_gates_start_and_stop_stays_visible(qtbot) -> None:
    tab = RecordingTab()
    _show(qtbot, tab, width=700, height=480)
    start_spy = QSignalSpy(tab.start_recording_requested)
    stop_spy = QSignalSpy(tab.stop_recording_requested)

    assert tab.scroll_area.widgetResizable()
    assert not tab.start_button.isEnabled()
    assert tab.stop_button.isVisibleTo(tab)
    stop_bottom = tab.stop_button.mapTo(tab, QPoint(0, 0)).y() + tab.stop_button.height()
    assert stop_bottom <= tab.height()

    _complete_required_labels(tab)
    tab.set_readiness(_all_ready())
    assert tab.start_button.isEnabled()
    qtbot.mouseClick(tab.start_button, Qt.LeftButton)
    assert start_spy.count() == 1
    payload = start_spy.at(0)[0]
    assert payload["trial_label_scope"] == "trial"
    assert payload["target_roi_ground_truth"] == 1

    tab.set_lifecycle(RecordingLifecycle.RECORDING)
    assert not tab.trial_labels_group.isEnabled()
    assert not tab.output_group.isEnabled()
    assert not tab.start_button.isEnabled()
    assert tab.stop_button.isEnabled()
    assert tab.stop_button.isVisibleTo(tab)
    qtbot.mouseClick(tab.stop_button, Qt.LeftButton)
    assert stop_spy.count() == 1


def test_recording_readiness_actions_simulation_and_lock_signals(qtbot) -> None:
    tab = RecordingTab()
    _show(qtbot, tab)
    prerequisite_spy = QSignalSpy(tab.open_prerequisite_tab_requested)
    output_spy = QSignalSpy(tab.output_directory_requested)
    smoke_spy = QSignalSpy(tab.run_simulation_smoke_requested)

    assert set(tab.readiness_checkboxes) == set(ReadinessFlags.REQUIRED_FIELDS)
    assert all(not checkbox.isEnabled() for checkbox in tab.readiness_checkboxes.values())
    assert all(label.wordWrap() for label in tab.readiness_explanations.values())
    qtbot.mouseClick(tab.readiness_buttons["camera_connected"], Qt.LeftButton)
    qtbot.mouseClick(tab.choose_output_button, Qt.LeftButton)
    qtbot.mouseClick(tab.run_smoke_button, Qt.LeftButton)
    assert prerequisite_spy.at(0)[0] == "camera_connected"
    assert output_spy.count() == smoke_spy.count() == 1
    assert "simulation mode" in tab.simulation_label.text().lower()
    assert "no physical" in tab.simulation_label.text().lower()

    tab.set_recording_locked(True)
    assert not tab.trial_labels_group.isEnabled()
    assert not tab.output_group.isEnabled()
    assert not tab.run_smoke_button.isEnabled()
    assert not any(button.isEnabled() for button in tab.readiness_buttons.values())


def test_recording_performance_review_and_reusable_graph_contract(qtbot) -> None:
    tab = RecordingTab()
    _show(qtbot, tab)
    performance = {
        key: f"metric-{index}" for index, (key, _title) in enumerate(PERFORMANCE_FIELDS)
    }
    tab.update_performance_metrics(performance)
    assert {
        key: label.text() for key, label in tab.performance_labels.items()
    } == performance

    review = {
        key: f"review-{index}" for index, (key, _title) in enumerate(REVIEW_FIELDS)
    }
    review["final_output_directory"] = r"C:\data\session-001"
    output_spy = QSignalSpy(tab.open_output_directory_requested)
    tab.set_review_summary(review)
    assert set(tab.review_labels) == {key for key, _title in REVIEW_FIELDS}
    assert all(
        tab.review_labels[key].text() == str(value) for key, value in review.items()
    )
    assert tab.open_output_button.isEnabled()
    qtbot.mouseClick(tab.open_output_button, Qt.LeftButton)
    assert output_spy.at(0)[0] == r"C:\data\session-001"

    graph_values = tuple(float(index) for index in range(1, 10))
    tab.graph_panel.set_current_values(graph_values)
    assert tab.graph_panel.heatmap_matrix.ravel().tolist() == list(graph_values)


def test_recording_required_labels_are_fixed_trial_scope(qtbot) -> None:
    tab = RecordingTab()
    _show(qtbot, tab)
    label_spy = QSignalSpy(tab.trial_labels_changed)
    _complete_required_labels(tab)
    tab.target_roi_combo.setCurrentIndex(8)
    tab.interaction_class_combo.setCurrentText("Press")
    tab.press_number_spin.setValue(3)

    labels = tab.trial_labels()
    assert labels == {
        "session_id": "session-001",
        "trial_id": "trial-001",
        "sensing_skin_id": "skin-001",
        "target_roi_ground_truth": 9,
        "specimen_or_participant_id": "",
        "trial_interaction_class": "Press",
        "trial_force_class": "",
        "press_number": 3,
        "notes": "",
        "trial_label_scope": "trial",
    }
    assert label_spy.count() >= 6
