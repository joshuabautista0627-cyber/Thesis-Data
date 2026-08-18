"""Recording/readiness/review presentation without acquisition or file I/O."""

from __future__ import annotations

from typing import Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.models import ReadinessFlags, RecordingLifecycle
from gui.roi_baseline_tab import OpticalGraphPanel


PERFORMANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("camera_capture_fps", "Camera capture FPS"),
    ("preview_fps", "Preview FPS"),
    ("feature_processing_fps", "Feature-processing FPS"),
    ("video_writing_fps", "Video-writing FPS"),
    ("loadcell_sample_rate_hz", "Load-cell sample rate"),
    ("dropped_preview_frames", "Dropped preview frames"),
    ("recording_pipeline_errors", "Recording pipeline errors"),
    ("capture_to_display_ms", "Capture-to-display time (ms)"),
)


REVIEW_FIELDS: tuple[tuple[str, str], ...] = (
    ("duration_s", "Duration (s)"),
    ("recorded_frame_count", "Recorded frame count"),
    ("video_frame_count", "Video frame count"),
    ("loadcell_sample_count", "Load-cell sample count"),
    ("actual_camera_fps", "Actual camera FPS"),
    ("loadcell_sample_rate_hz", "Load-cell sample rate"),
    ("preview_drops", "Preview drops"),
    ("recording_errors", "Recording errors"),
    ("minimum_force_N", "Minimum force (N)"),
    ("maximum_force_N", "Maximum force (N)"),
    ("mean_force_N", "Mean force (N)"),
    ("peak_optical_intensity", "Peak optical intensity"),
    ("highest_total_response_roi", "ROI with highest total response"),
    ("missing_feature_percent", "Missing-feature percentage"),
    ("missing_force_percent", "Missing-force percentage"),
    ("maximum_sync_gap_ms", "Maximum synchronization gap (ms)"),
    ("manual_heatmap_count", "Manually saved heatmaps"),
    ("manual_line_graph_count", "Manually saved line graphs"),
    ("automatic_heatmap_summary_status", "Automatic heatmap-summary status"),
    (
        "automatic_temporal_spatial_status",
        "Automatic temporal-line and spatial-profile status",
    ),
    ("final_output_directory", "Final output directory"),
    ("session_status", "Complete or partial session status"),
)


_READINESS_TAB = {
    "camera_connected": "Camera Setup",
    "rois_valid": "ROI and Baseline",
    "baseline_valid": "ROI and Baseline",
    "serial_connected": "Load Cell",
    "loadcell_calibrated": "Load Cell",
    "calibration_verified": "Load Cell",
    "trial_labels_valid": "Recording and Export",
    "output_directory_valid": "Recording and Export",
    "video_writer_preflight_passed": "Recording and Export",
    "disk_space_sufficient": "Recording and Export",
}


class RecordingTab(QWidget):
    """Fixed-label single-press workflow, readiness gating, and review screen."""

    trial_labels_changed = Signal(object)
    output_directory_requested = Signal()
    start_recording_requested = Signal(object)
    stop_recording_requested = Signal()
    open_prerequisite_tab_requested = Signal(str)
    run_simulation_smoke_requested = Signal()
    open_output_directory_requested = Signal(str)
    save_spatial_graph_requested = Signal(object)
    save_line_graph_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lifecycle = RecordingLifecycle.IDLE
        self._locked = False
        self._readiness = {
            name: False for name in ReadinessFlags.REQUIRED_FIELDS
        }
        self._review_output_directory = ""

        root = QVBoxLayout(self)
        self.instructions_label = QLabel(
            "Enter labels for one intentional press, satisfy every readiness item, "
            "then use Start Recording once and Stop Recording when the press is complete."
        )
        self.instructions_label.setObjectName("recording_instructions")
        self.instructions_label.setWordWrap(True)
        self.status_label = QLabel("Status: recording prerequisites are incomplete.")
        self.status_label.setWordWrap(True)
        self.next_action_label = QLabel(
            "Next recommended action: complete the first failed readiness item."
        )
        self.next_action_label.setWordWrap(True)
        root.addWidget(self.instructions_label)
        root.addWidget(self.status_label)
        root.addWidget(self.next_action_label)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        self.content_layout.addWidget(self._create_trial_labels_group())
        self.content_layout.addWidget(self._create_output_group())
        self.content_layout.addWidget(self._create_readiness_group())
        self.content_layout.addWidget(self._create_simulation_group())
        self.content_layout.addWidget(self._create_performance_group())
        self.graph_panel = OpticalGraphPanel()
        graph_group = QGroupBox("Live Optical Visualization")
        graph_layout = QVBoxLayout(graph_group)
        graph_layout.addWidget(self.graph_panel)
        self.content_layout.addWidget(graph_group)
        self.content_layout.addWidget(self._create_review_group())
        self.content_layout.addStretch(1)
        self.scroll_area.setWidget(content)
        root.addWidget(self.scroll_area, 1)

        # The action bar is deliberately outside the scroll area, so Stop never
        # scrolls off-screen at 1366x768 or under Windows display scaling.
        action_bar = QHBoxLayout()
        self.start_button = QPushButton("Start Recording")
        self.start_button.setObjectName("start_recording_button")
        self.stop_button = QPushButton("Stop Recording")
        self.stop_button.setObjectName("stop_recording_button")
        self.stop_button.setEnabled(False)
        self.stop_button.setVisible(True)
        action_bar.addStretch(1)
        action_bar.addWidget(self.start_button)
        action_bar.addWidget(self.stop_button)
        root.addLayout(action_bar)

        self.start_button.clicked.connect(
            lambda: self.start_recording_requested.emit(self.trial_labels())
        )
        self.stop_button.clicked.connect(self.stop_recording_requested.emit)
        self.graph_panel.save_spatial_graph_requested.connect(
            self.save_spatial_graph_requested.emit
        )
        self.graph_panel.save_line_graph_requested.connect(
            self.save_line_graph_requested.emit
        )
        self._refresh_start_gating()

    def _create_trial_labels_group(self) -> QGroupBox:
        self.trial_labels_group = QGroupBox("Fixed Trial Labels — one recording = one press")
        form = QFormLayout(self.trial_labels_group)
        self.session_id_edit = QLineEdit()
        self.trial_id_edit = QLineEdit()
        self.sensing_skin_id_edit = QLineEdit()
        self.target_roi_combo = QComboBox()
        self.target_roi_combo.addItems([f"ROI {index}" for index in range(1, 10)])
        self.specimen_id_edit = QLineEdit()
        self.interaction_class_combo = QComboBox()
        self.interaction_class_combo.setEditable(True)
        self.interaction_class_combo.addItems(["", "Press", "Tap", "Hold", "Other"])
        self.force_class_edit = QLineEdit()
        self.press_number_spin = QSpinBox()
        self.press_number_spin.setRange(0, 1_000_000)
        self.press_number_spin.setSpecialValueText("Blank")
        self.notes_edit = QLineEdit()
        fields = (
            ("Session ID *", self.session_id_edit),
            ("Trial ID *", self.trial_id_edit),
            ("Sensing-skin ID *", self.sensing_skin_id_edit),
            ("Target ROI ground truth *", self.target_roi_combo),
            ("Specimen / participant ID", self.specimen_id_edit),
            ("Interaction class", self.interaction_class_combo),
            ("Force class", self.force_class_edit),
            ("Press number", self.press_number_spin),
            ("Notes", self.notes_edit),
        )
        for label, widget in fields:
            form.addRow(label, widget)
        for line_edit in (
            self.session_id_edit,
            self.trial_id_edit,
            self.sensing_skin_id_edit,
            self.specimen_id_edit,
            self.force_class_edit,
            self.notes_edit,
        ):
            line_edit.textChanged.connect(self._emit_trial_labels)
        self.target_roi_combo.currentIndexChanged.connect(self._emit_trial_labels)
        self.interaction_class_combo.currentTextChanged.connect(self._emit_trial_labels)
        self.press_number_spin.valueChanged.connect(self._emit_trial_labels)
        return self.trial_labels_group

    def _create_output_group(self) -> QGroupBox:
        self.output_group = QGroupBox("Output Location")
        layout = QVBoxLayout(self.output_group)
        path_row = QHBoxLayout()
        self.output_directory_edit = QLineEdit()
        self.output_directory_edit.setReadOnly(True)
        self.output_directory_edit.setPlaceholderText("Choose a writable output directory")
        self.choose_output_button = QPushButton("Choose Output Directory…")
        self.choose_output_button.clicked.connect(self.output_directory_requested.emit)
        path_row.addWidget(self.output_directory_edit, 1)
        path_row.addWidget(self.choose_output_button)
        layout.addLayout(path_row)
        self.record_original_check = QCheckBox("Original unannotated video (required)")
        self.record_original_check.setChecked(True)
        self.record_original_check.setEnabled(False)
        self.record_overlay_check = QCheckBox("Original video with ROI overlays")
        self.record_processed_check = QCheckBox("Selected processed video")
        self.record_motion_check = QCheckBox("Motion-magnified video")
        self.disable_live_motion_during_raw_check = QCheckBox(
            "Disable live magnification while recording raw-only video"
        )
        for checkbox in (
            self.record_original_check,
            self.record_overlay_check,
            self.record_processed_check,
            self.record_motion_check,
            self.disable_live_motion_during_raw_check,
        ):
            layout.addWidget(checkbox)
        return self.output_group

    def recording_streams(self) -> tuple[str, ...]:
        streams = []
        if self.record_overlay_check.isChecked():
            streams.append("original_overlays")
        if self.record_processed_check.isChecked():
            streams.append("processed")
        if self.record_motion_check.isChecked():
            streams.append("motion_magnified")
        return tuple(streams)

    def _create_readiness_group(self) -> QGroupBox:
        group = QGroupBox("Recording Readiness Checklist")
        grid = QGridLayout(group)
        grid.addWidget(QLabel("Ready"), 0, 0)
        grid.addWidget(QLabel("Requirement and corrective action"), 0, 1)
        grid.addWidget(QLabel("Open"), 0, 2)
        self.readiness_checkboxes: dict[str, QCheckBox] = {}
        self.readiness_explanations: dict[str, QLabel] = {}
        self.readiness_buttons: dict[str, QPushButton] = {}
        for row, name in enumerate(ReadinessFlags.REQUIRED_FIELDS, start=1):
            checkbox = QCheckBox(name.replace("_", " ").title())
            checkbox.setEnabled(False)
            explanation = QLabel(ReadinessFlags.GUIDANCE[name])
            explanation.setWordWrap(True)
            button = QPushButton(f"Open {_READINESS_TAB[name]}")
            button.setObjectName(f"open_{name}_button")
            button.clicked.connect(
                lambda _checked=False, requirement=name: (
                    self.open_prerequisite_tab_requested.emit(requirement)
                )
            )
            grid.addWidget(checkbox, row, 0)
            grid.addWidget(explanation, row, 1)
            grid.addWidget(button, row, 2)
            self.readiness_checkboxes[name] = checkbox
            self.readiness_explanations[name] = explanation
            self.readiness_buttons[name] = button
        return group

    def _create_simulation_group(self) -> QGroupBox:
        group = QGroupBox("Simulation")
        layout = QHBoxLayout(group)
        self.simulation_label = QLabel(
            "SIMULATION MODE — no physical Arducam camera or Arduino is connected"
        )
        self.simulation_label.setObjectName("simulation_mode_label")
        self.simulation_label.setWordWrap(True)
        self.run_smoke_button = QPushButton("Run Simulation Smoke Test")
        self.run_smoke_button.clicked.connect(self.run_simulation_smoke_requested.emit)
        layout.addWidget(self.simulation_label, 1)
        layout.addWidget(self.run_smoke_button)
        return group

    def _create_performance_group(self) -> QGroupBox:
        group = QGroupBox("Live Performance")
        grid = QGridLayout(group)
        self.performance_labels: dict[str, QLabel] = {}
        for index, (key, title) in enumerate(PERFORMANCE_FIELDS):
            row, column = divmod(index, 2)
            container = QWidget()
            form = QFormLayout(container)
            value_label = QLabel("—")
            value_label.setObjectName(f"performance_{key}")
            form.addRow(f"{title}:", value_label)
            grid.addWidget(container, row, column)
            self.performance_labels[key] = value_label
        return group

    def _create_review_group(self) -> QGroupBox:
        group = QGroupBox("Completed / Partial Session Review")
        layout = QVBoxLayout(group)
        grid = QGridLayout()
        self.review_labels: dict[str, QLabel] = {}
        for index, (key, title) in enumerate(REVIEW_FIELDS):
            row, column_pair = divmod(index, 2)
            title_label = QLabel(f"{title}:")
            value_label = QLabel("—")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            grid.addWidget(title_label, row, column_pair * 2)
            grid.addWidget(value_label, row, column_pair * 2 + 1)
            self.review_labels[key] = value_label
        layout.addLayout(grid)
        self.open_output_button = QPushButton("Open Final Output Directory")
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(
            lambda: self.open_output_directory_requested.emit(
                self._review_output_directory
            )
        )
        layout.addWidget(self.open_output_button)
        return group

    def trial_labels(self) -> dict[str, object]:
        """Return fixed trial-scope values; no frame contact label is inferred."""

        press_number = self.press_number_spin.value()
        return {
            "session_id": self.session_id_edit.text().strip(),
            "trial_id": self.trial_id_edit.text().strip(),
            "sensing_skin_id": self.sensing_skin_id_edit.text().strip(),
            "target_roi_ground_truth": self.target_roi_combo.currentIndex() + 1,
            "specimen_or_participant_id": self.specimen_id_edit.text().strip(),
            "trial_interaction_class": self.interaction_class_combo.currentText().strip(),
            "trial_force_class": self.force_class_edit.text().strip(),
            "press_number": press_number if press_number > 0 else None,
            "notes": self.notes_edit.text(),
            "trial_label_scope": "trial",
        }

    def set_output_directory(self, path: str) -> None:
        self.output_directory_edit.setText(path)
        self.output_directory_edit.setToolTip(path)

    def set_readiness(self, readiness: ReadinessFlags | Mapping[str, bool]) -> None:
        values = readiness.as_dict() if isinstance(readiness, ReadinessFlags) else readiness
        for name in ReadinessFlags.REQUIRED_FIELDS:
            ready = bool(values.get(name, False))
            self._readiness[name] = ready
            self.readiness_checkboxes[name].setChecked(ready)
            self.readiness_explanations[name].setText(
                "Ready." if ready else ReadinessFlags.GUIDANCE[name]
            )
            self.readiness_buttons[name].setEnabled(not ready and not self._locked)
        self._refresh_start_gating()

    def set_lifecycle(self, lifecycle: RecordingLifecycle | str) -> None:
        self._lifecycle = (
            lifecycle
            if isinstance(lifecycle, RecordingLifecycle)
            else RecordingLifecycle(str(lifecycle))
        )
        self.set_recording_locked(
            self._lifecycle
            in {RecordingLifecycle.RECORDING, RecordingLifecycle.FINALIZING}
        )
        if self._lifecycle is RecordingLifecycle.RECORDING:
            self.status_label.setText("Status: recording one fixed-label press.")
            self.next_action_label.setText(
                "Next recommended action: press Stop Recording when the press is complete."
            )
        elif self._lifecycle is RecordingLifecycle.FINALIZING:
            self.status_label.setText("Status: finalizing and validating output files.")
        elif self._lifecycle is RecordingLifecycle.COMPLETE:
            self.status_label.setText("Status: recording completed and validated.")
        elif self._lifecycle is RecordingLifecycle.ERROR:
            self.status_label.setText("Status: partial session saved after an error.")
        self._refresh_start_gating()

    def set_recording_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self.trial_labels_group.setEnabled(not self._locked)
        self.output_group.setEnabled(not self._locked)
        self.run_smoke_button.setEnabled(not self._locked)
        for name, button in self.readiness_buttons.items():
            button.setEnabled(not self._locked and not self._readiness[name])
        self._refresh_start_gating()

    def set_simulation_mode(self, enabled: bool) -> None:
        self.simulation_label.setVisible(bool(enabled))
        self.run_smoke_button.setVisible(bool(enabled))

    def update_performance_metrics(self, values: Mapping[str, object]) -> None:
        for key, value in values.items():
            if key in self.performance_labels:
                self.performance_labels[key].setText(str(value))

    def set_review_summary(self, values: Mapping[str, object]) -> None:
        for key, label in self.review_labels.items():
            if key in values:
                label.setText(str(values[key]))
                label.setToolTip(str(values[key]))
        if "final_output_directory" in values:
            self._review_output_directory = str(values["final_output_directory"])
            self.open_output_button.setEnabled(bool(self._review_output_directory))

    def clear_review_summary(self) -> None:
        """Remove prior-trial review values when a new trial transaction starts."""

        for label in self.review_labels.values():
            label.setText("—")
            label.setToolTip("")
        self._review_output_directory = ""
        self.open_output_button.setEnabled(False)

    def set_status(self, status: str, next_action: str = "") -> None:
        self.status_label.setText(status)
        if next_action:
            self.next_action_label.setText(next_action)

    def _emit_trial_labels(self, *_args: object) -> None:
        self.trial_labels_changed.emit(self.trial_labels())
        self._refresh_start_gating()

    def _refresh_start_gating(self) -> None:
        labels = self.trial_labels()
        labels_valid = all(
            bool(str(labels[name]).strip())
            for name in ("session_id", "trial_id", "sensing_skin_id")
        )
        ready = all(self._readiness.values()) and labels_valid
        can_start = (
            ready
            and not self._locked
            and self._lifecycle is RecordingLifecycle.IDLE
        )
        self.start_button.setEnabled(can_start)
        self.stop_button.setEnabled(self._lifecycle is RecordingLifecycle.RECORDING)
        self.stop_button.setVisible(True)
        if not ready and self._lifecycle is RecordingLifecycle.IDLE:
            if not labels_valid:
                self.next_action_label.setText(
                    "Next recommended action: enter Session ID, Trial ID, and "
                    "Sensing-skin ID."
                )
            else:
                failed = next(
                    (
                        name
                        for name in ReadinessFlags.REQUIRED_FIELDS
                        if not self._readiness[name]
                    ),
                    "",
                )
                if not failed:
                    return
                self.next_action_label.setText(
                    f"Next recommended action: {ReadinessFlags.GUIDANCE[failed]}"
                )


__all__ = [
    "PERFORMANCE_FIELDS",
    "REVIEW_FIELDS",
    "RecordingTab",
]
