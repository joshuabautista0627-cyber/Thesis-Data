"""Guarded experimental live optical-sensor presentation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.experimental_live_sensor import (
    ExperimentalBundleError,
    ExperimentalLiveSensorEngine,
    LiveSensorResult,
    load_experimental_bundle,
)
from core.live_sensor_report import (
    EventReportRecord,
    ReportPaths,
    write_post_processing_report,
)


class LiveSensorTab(QWidget):
    """Live/replay UI that never hides the recovery model's limitations."""

    def __init__(self, bundle_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("experimental_live_sensor_tab")
        self._bundle_path = Path(bundle_path)
        self._engine: ExperimentalLiveSensorEngine | None = None
        self._live_context_ready = False
        self._live_context_id = ""
        self._live_layout_id = ""
        self._demo_rows: list[dict[str, object]] = []
        self._demo_index = 0
        self._demo_mode = False
        self._last_result: LiveSensorResult | None = None
        self._last_completed_result: LiveSensorResult | None = None
        self._report_events: list[EventReportRecord] = []
        self._report_event_counter = 0
        self._event_source_epoch = 0
        self._recorded_completion_keys: set[tuple[int, int, int, int, int]] = set()

        root = QVBoxLayout(self)
        self.warning = QLabel(
            "EXPERIMENTAL — NOT VALIDATED. Best-effort manual-data recovery only; "
            "do not use for safety, control, or thesis claims of physical validation."
        )
        self.warning.setObjectName("experimental_model_warning")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(
            "padding: 9px; background: #fff3cd; color: #5f4700; "
            "border: 2px solid #d39e00; font-weight: 700;"
        )
        root.addWidget(self.warning)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)

        self.result_group = QGroupBox("Current optical estimate")
        result_layout = QVBoxLayout(self.result_group)
        self.state_label = QLabel("MODEL UNAVAILABLE")
        self.state_label.setObjectName("live_sensor_state")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setStyleSheet(
            "padding: 7px; background: #5c6770; color: white; font-size: 15px; font-weight: 700;"
        )
        self.force_label = QLabel("—")
        self.force_label.setObjectName("live_sensor_force")
        self.force_label.setAccessibleName("Experimental optical force estimate")
        self.force_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.force_label.setStyleSheet("font-size: 38px; font-weight: 750; padding: 8px;")
        self.roi_label = QLabel("ROI: —")
        self.roi_label.setObjectName("live_sensor_roi")
        self.roi_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.roi_label.setStyleSheet("font-size: 19px; font-weight: 650;")
        self.message_label = QLabel("Loading the experimental bundle…")
        self.message_label.setWordWrap(True)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_layout.addWidget(self.state_label)
        result_layout.addWidget(self.force_label)
        result_layout.addWidget(self.roi_label)
        result_layout.addWidget(self.message_label)
        content_layout.addWidget(self.result_group)

        self.roi_group = QGroupBox("Detected rod ROI(s)")
        roi_layout = QGridLayout(self.roi_group)
        self.roi_cells: list[QLabel] = []
        for index in range(9):
            cell = QLabel(str(index + 1))
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setMinimumSize(45, 34)
            cell.setAccessibleName(f"ROI {index + 1}")
            cell.setStyleSheet(
                "background: #e9ecef; color: #495057; border: 1px solid #adb5bd; font-weight: 650;"
            )
            roi_layout.addWidget(cell, index // 3, index % 3)
            self.roi_cells.append(cell)
        content_layout.addWidget(self.roi_group)

        controls = QGroupBox("Required setup and replay")
        controls_layout = QVBoxLayout(controls)
        self.warmup_progress = QProgressBar()
        self.warmup_progress.setRange(0, 120)
        self.warmup_progress.setValue(0)
        self.warmup_progress.setFormat("Unloaded warm-up: %v/%m frames")
        button_row = QHBoxLayout()
        self.warmup_button = QPushButton("Calibrate while unloaded")
        self.warmup_button.setObjectName("live_sensor_warmup_button")
        self.warmup_button.setAccessibleName("Start unloaded optical threshold calibration")
        self.reset_button = QPushButton("Reset estimate")
        self.demo_button = QPushButton("Run bundled replay demo")
        self.demo_button.setObjectName("live_sensor_replay_button")
        button_row.addWidget(self.warmup_button)
        button_row.addWidget(self.reset_button)
        button_row.addWidget(self.demo_button)
        controls_layout.addWidget(self.warmup_progress)
        controls_layout.addLayout(button_row)
        self.setup_help = QLabel(
            "Camera-only live use: connect the camera and mechanoluminescent skin, define the stored "
            "nine-ROI layout, capture and accept an unloaded optical baseline, then keep the skin "
            "unloaded while calibration collects 120 frames. No load cell, printer, or their "
            "calibration is required or read by force/localization inference."
        )
        self.setup_help.setWordWrap(True)
        controls_layout.addWidget(self.setup_help)
        content_layout.addWidget(controls)

        report_group = QGroupBox("Post-processing report")
        report_layout = QVBoxLayout(report_group)
        self.report_status_label = QLabel("No completed presses captured in this app session.")
        self.report_status_label.setWordWrap(True)
        report_layout.addWidget(self.report_status_label)
        report_help = QLabel(
            "Exports a self-contained HTML report plus event and ROI-summary CSV files and JSON. "
            "It includes press counts, peak raw HSV V (0–255), peak baseline-corrected ΔV, force "
            "at the exact peak-V frame, event peak force, and localization confidence."
        )
        report_help.setWordWrap(True)
        report_layout.addWidget(report_help)
        report_buttons = QHBoxLayout()
        self.export_report_button = QPushButton("Export post-processing report")
        self.export_report_button.setObjectName("live_sensor_export_report_button")
        self.export_report_button.setEnabled(False)
        self.clear_report_button = QPushButton("Clear captured events")
        self.clear_report_button.setObjectName("live_sensor_clear_report_button")
        self.clear_report_button.setEnabled(False)
        report_buttons.addWidget(self.export_report_button)
        report_buttons.addWidget(self.clear_report_button)
        report_layout.addLayout(report_buttons)
        content_layout.addWidget(report_group)

        evidence_group = QGroupBox("Archive recovery evidence")
        evidence_layout = QVBoxLayout(evidence_group)
        self.evidence_label = QLabel("Metrics unavailable.")
        self.evidence_label.setWordWrap(True)
        evidence_layout.addWidget(self.evidence_label)
        self.limits_label = QLabel(
            "Exact values are shown only for detected single-contact responses inside the model's "
            "optical support. The usable band is intentionally narrow (1.7–3.0 N), ROI is tentative, "
            "and above/below-range responses show —. A new independent dataset would be required to "
            "convert these recovery results into confirmatory evidence."
        )
        self.limits_label.setWordWrap(True)
        evidence_layout.addWidget(self.limits_label)
        content_layout.addWidget(evidence_group)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        self._demo_timer = QTimer(self)
        self._demo_timer.setInterval(25)
        self._demo_timer.timeout.connect(self._advance_demo)
        self.warmup_button.clicked.connect(self.start_live_warmup)
        self.reset_button.clicked.connect(self.reset_estimate)
        self.demo_button.clicked.connect(self.start_demo)
        self.export_report_button.clicked.connect(
            lambda: self.export_post_processing_report()
        )
        self.clear_report_button.clicked.connect(lambda: self.clear_report_events())
        self._load_bundle()

    @property
    def engine(self) -> ExperimentalLiveSensorEngine | None:
        return self._engine

    def _load_bundle(self) -> None:
        try:
            bundle = load_experimental_bundle(self._bundle_path)
            self._engine = ExperimentalLiveSensorEngine(bundle)
        except (OSError, ValueError, ExperimentalBundleError) as exc:
            self._engine = None
            self.warmup_button.setEnabled(False)
            self.demo_button.setEnabled(False)
            self.message_label.setText(f"Experimental bundle rejected: {exc}")
            self.state_label.setText("MODEL UNAVAILABLE")
            return
        aggregate = dict(bundle.metrics.get("aggregate", {}))
        if bundle.sensor_mode in {"event_signal", "hybrid_force_event"}:
            hybrid = bundle.sensor_mode == "hybrid_force_event"
            self.result_group.setTitle(
                "Current / last force event" if hybrid else "Current / last optical event"
            )
            self.roi_group.setTitle("Detected rod ROI(s)")
            self.force_label.setAccessibleName(
                "Approximate optical force estimate"
                if hybrid
                else "Peak optical signal relative to threshold"
            )
            if hybrid:
                self.warning.setText(
                    "EXPERIMENTAL — NOT VALIDATED. Force is an approximate optical estimate "
                    "within a narrow 1.7–3.0 N archive band; force resolution was not established. "
                    "Inference uses only the camera and mechanoluminescent skin—no load cell or printer. "
                    "ROI is tentative. Do not use this output for safety, control, or physical-validation claims."
                )
            else:
                self.warning.setText(
                    "EXPERIMENTAL — NOT VALIDATED. This view reports a unitless optical event signal "
                    "and a tentative discrete ROI. Newton-valued force is unavailable; do not use "
                    "this output for safety, control, or physical-validation claims."
                )
            self.evidence_label.setText(
                "Six-fold post-hoc event replay: max no-contact FPR "
                f"{100.0 * float(aggregate.get('max_no_contact_frame_fpr', 0.0)):.2f}%; "
                f"minimum fold contact recall {100.0 * float(aggregate.get('min_contact_recall', 0.0)):.2f}%; "
                f"single-press event ROI macro F1 {100.0 * float(aggregate.get('event_localization_macro_f1', 0.0)):.2f}%; "
                f"selective single-press ROI F1 {100.0 * float(aggregate.get('selective_localization_macro_f1', 0.0)):.2f}% "
                f"at {100.0 * float(aggregate.get('selective_localization_coverage', 1.0)):.2f}% coverage. "
                "These are reused-fold recovery figures, not untouched validation."
                + (
                    " The retained monotonic force model has conditional MAE "
                    f"{float(aggregate.get('mean_conditional_force_mae_N', 0.0)):.3f} N and "
                    f"cross-validated p95 absolute error {float(aggregate.get('cross_validated_p95_absolute_error_N', 0.0)):.3f} N; "
                    "it did not establish force resolution."
                    if hybrid
                    else ""
                )
            )
            if hybrid:
                self.limits_label.setText(
                    "During contact, the display shows the v2 monotonic frame estimate; after an event, "
                    "it holds the maximum in-support estimate. Values outside the fitted optical support "
                    "show —. Each rod now has an independent unloaded-normalized activation gate, so "
                    "multiple ROI boxes may be shown together. Newton force is withheld for multi-press "
                    "events because the archived force model contains only single presses."
                )
            else:
                self.limits_label.setText(
                    "The displayed peak is the largest optical contact score divided by the unloaded "
                    "threshold; it is unitless and is not force. Each rod has an independent activation "
                    "gate, so multiple ROI boxes may be shown together; the archived accuracy evidence "
                    "still covers only single-press events. The "
                    "1.7–3.0 newton band is used only to evaluate contact recall in the archived data."
                )
            self.setup_help.setText(
                "Camera-only live use requires the mechanoluminescent skin and bundled canonical nine-ROI layout "
                f"({bundle.required_roi_layout_id}), a {bundle.required_frame_width} × "
                f"{bundle.required_frame_height} view, {bundle.required_rotation_degrees}° clockwise "
                f"rotation, and horizontal mirror={bundle.required_mirror_horizontal}. Load the "
                "canonical layout, capture and accept an unloaded baseline, then keep the sensor "
                "unloaded while calibration collects 120 frames. No load cell or printer is required or read."
            )
        else:
            resolution = (
                "established"
                if bool(aggregate.get("force_resolution_established", False))
                else "not established versus the constant baseline"
            )
            self.evidence_label.setText(
                "Six-fold post-hoc replay: displayed-force MAE "
                f"{float(aggregate.get('mean_displayed_force_mae_N', 0.0)):.3f} N; "
                f"conditional MAE {float(aggregate.get('mean_conditional_force_mae_N', 0.0)):.3f} N; "
                f"max no-contact FPR {100.0 * float(aggregate.get('max_no_contact_frame_fpr', 0.0)):.2f}%; "
                f"minimum fold recall {100.0 * float(aggregate.get('min_contact_recall', 0.0)):.2f}%; "
                f"forced macro ROI F1 {100.0 * float(aggregate.get('forced_localization_macro_f1', 0.0)):.2f}%; "
                f"selective ROI F1 {100.0 * float(aggregate.get('selective_localization_macro_f1', 0.0)):.2f}% "
                f"at {100.0 * float(aggregate.get('selective_localization_coverage', 1.0)):.2f}% coverage. "
                f"Force response resolution is {resolution}. "
                "These are recovery/CV figures, not an untouched validation result."
            )
        self.warmup_progress.setRange(0, bundle.minimum_warmup_frames)
        self.message_label.setText("Accept an unloaded camera baseline, then run warm-up—or use replay demo.")
        self.state_label.setText("SETUP BLOCKED")
        self.warmup_button.setEnabled(False)
        self.demo_button.setEnabled(True)

    def set_baseline_ready(
        self,
        ready: bool,
        baseline_id: str = "",
        layout_id: str = "",
        rotation_degrees: int | None = None,
        mirror_horizontal: bool | None = None,
    ) -> None:
        self._last_completed_result = None
        compatible = bool(ready)
        mismatch = ""
        if (
            compatible
            and self._engine is not None
            and self._engine.bundle.required_roi_layout_id is not None
        ):
            bundle = self._engine.bundle
            checks = (
                (str(layout_id) == bundle.required_roi_layout_id, "ROI geometry"),
                (
                    rotation_degrees == bundle.required_rotation_degrees,
                    "camera rotation",
                ),
                (
                    mirror_horizontal == bundle.required_mirror_horizontal,
                    "camera mirror",
                ),
            )
            failed = [label for passed, label in checks if not passed]
            compatible = not failed
            if failed:
                mismatch = ", ".join(failed)
        self._live_context_ready = compatible
        self._live_context_id = str(baseline_id)
        self._live_layout_id = str(layout_id)
        if self._engine is None or self._demo_mode:
            return
        self._engine.set_context_ready(compatible, baseline_id)
        self.warmup_button.setEnabled(compatible)
        self.warmup_progress.setValue(0)
        if ready and not compatible:
            model_label = (
                "v3"
                if self._engine.bundle.sensor_mode == "event_signal"
                else self._engine.bundle.bundle_id
            )
            self._show_unavailable(
                "SETUP BLOCKED",
                f"Baseline rejected for {model_label}: {mismatch} does not match the bundled canonical layout. "
                "Load assets/live_sensor/roi_layout.json and recapture the unloaded baseline.",
            )
        elif compatible:
            self._show_unavailable("SETUP BLOCKED", "Baseline accepted. Keep unloaded and start warm-up.")
        else:
            self._show_unavailable("SETUP BLOCKED", "Accept an unloaded baseline first.")

    def start_live_warmup(self) -> None:
        if self._engine is None:
            return
        if self._demo_mode:
            self._stop_demo()
        self._event_source_epoch += 1
        self._engine.set_context_ready(self._live_context_ready, self._live_context_id)
        self._last_completed_result = None
        try:
            self._engine.start_unloaded_warmup()
        except RuntimeError as exc:
            QMessageBox.warning(self, "Warm-up blocked", str(exc))
            return
        self.warmup_progress.setValue(0)
        self._show_unavailable("WARMING UP", "Keep every ROI completely unloaded.")

    def reset_estimate(self) -> None:
        self._stop_demo()
        if self._engine is None:
            return
        self._event_source_epoch += 1
        self._engine.set_context_ready(self._live_context_ready, self._live_context_id)
        self._engine.reset(keep_context=True)
        self._last_completed_result = None
        self.warmup_progress.setValue(0)
        self._show_unavailable(
            "SETUP BLOCKED",
            "Estimate reset. Run unloaded warm-up again." if self._live_context_ready else "Accept an unloaded baseline first.",
        )

    def process_live_row(self, row: Mapping[str, object]) -> None:
        if self._engine is None or self._demo_mode:
            return
        result = self._engine.process(row)
        self._render(result)

    def start_demo(self) -> None:
        if self._engine is None:
            return
        try:
            rows = [
                json.loads(line)
                for line in self._engine.bundle.replay_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Replay unavailable", str(exc))
            return
        if not rows:
            QMessageBox.warning(self, "Replay unavailable", "The bundled replay trace is empty.")
            return
        self._demo_rows = rows
        self._demo_index = 0
        self._demo_mode = True
        self._event_source_epoch += 1
        self._last_completed_result = None
        self._engine.set_context_ready(True, "bundled-manual-replay")
        self._engine.start_unloaded_warmup()
        self.demo_button.setEnabled(False)
        self.warmup_button.setEnabled(False)
        self.warmup_progress.setValue(0)
        self._show_unavailable("REPLAY WARM-UP", "Bundled no-contact frames are calibrating the replay threshold.")
        self._demo_timer.start()

    def _advance_demo(self) -> None:
        if self._engine is None or self._demo_index >= len(self._demo_rows):
            self._stop_demo(preserve_result=True)
            return
        row = self._demo_rows[self._demo_index]
        self._demo_index += 1
        result = self._engine.process(row)
        self._render(result)

    def _stop_demo(self, *, preserve_result: bool = False) -> None:
        self._demo_timer.stop()
        self._demo_mode = False
        self._demo_rows = []
        self._demo_index = 0
        self.demo_button.setEnabled(self._engine is not None)
        self.warmup_button.setEnabled(self._engine is not None and self._live_context_ready)
        if not preserve_result and self._engine is not None:
            self._engine.set_context_ready(self._live_context_ready, self._live_context_id)

    def _show_unavailable(self, state: str, message: str) -> None:
        self.state_label.setText(state)
        self.state_label.setStyleSheet(
            "padding: 7px; background: #6c757d; color: white; font-size: 15px; font-weight: 700;"
        )
        self.force_label.setText("—")
        self.roi_label.setText("ROI: —")
        self.message_label.setText(message)
        self._highlight_rois(())

    def _highlight_rois(self, rois: tuple[int, ...]) -> None:
        active = frozenset(int(roi) for roi in rois)
        for index, cell in enumerate(self.roi_cells, start=1):
            if index in active:
                cell.setStyleSheet(
                    "background: #198754; color: white; border: 2px solid #0f5132; font-weight: 750;"
                )
            else:
                cell.setStyleSheet(
                    "background: #e9ecef; color: #495057; border: 1px solid #adb5bd; font-weight: 650;"
                )

    def _capture_completed_event(self, result: LiveSensorResult) -> None:
        if (
            result.event_id is None
            or result.event_start_frame_id is None
            or result.event_end_frame_id is None
        ):
            return
        key = (
            self._event_source_epoch,
            int(result.event_id),
            int(result.event_start_frame_id),
            int(result.event_end_frame_id),
            int(result.frame_id),
        )
        if key in self._recorded_completion_keys:
            return
        self._report_event_counter += 1
        try:
            record = EventReportRecord.from_result(
                result,
                report_event_id=self._report_event_counter,
            )
        except ValueError:
            self._report_event_counter -= 1
            return
        self._recorded_completion_keys.add(key)
        self._report_events.append(record)
        self._update_report_controls()

    def _update_report_controls(self) -> None:
        count = len(self._report_events)
        self.export_report_button.setEnabled(count > 0)
        self.clear_report_button.setEnabled(count > 0)
        if count:
            withheld = sum(
                event.localization_status == "withheld" for event in self._report_events
            )
            self.report_status_label.setText(
                f"{count} completed press{'es' if count != 1 else ''} captured in this app session; "
                f"{withheld} ROI assignment{'s' if withheld != 1 else ''} withheld."
            )
        else:
            self.report_status_label.setText(
                "No completed presses captured in this app session."
            )

    def export_post_processing_report(
        self,
        parent_directory: Path | None = None,
        *,
        notify: bool = True,
    ) -> ReportPaths | None:
        if self._engine is None or not self._report_events:
            if notify:
                QMessageBox.information(
                    self,
                    "No completed presses",
                    "Complete at least one optical press before exporting a report.",
                )
            return None
        if parent_directory is None:
            default = Path.cwd() / "output"
            default.mkdir(parents=True, exist_ok=True)
            selected = QFileDialog.getExistingDirectory(
                self,
                "Choose parent folder for the post-processing report",
                str(default),
            )
            if not selected:
                return None
            parent_directory = Path(selected)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = Path(parent_directory) / f"live_sensor_report_{stamp}"
        suffix = 2
        while destination.exists():
            destination = Path(parent_directory) / f"live_sensor_report_{stamp}_{suffix}"
            suffix += 1
        try:
            paths = write_post_processing_report(
                destination,
                tuple(self._report_events),
                bundle_id=self._engine.bundle.bundle_id,
            )
        except (OSError, ValueError) as exc:
            if notify:
                QMessageBox.warning(self, "Report export failed", str(exc))
            return None
        if notify:
            QMessageBox.information(
                self,
                "Post-processing report exported",
                "The HTML report, event CSV, ROI summary CSV, and JSON audit file were saved to:\n"
                f"{paths.directory}",
            )
        return paths

    def clear_report_events(self, *, confirm: bool = True) -> None:
        if not self._report_events:
            return
        if confirm:
            response = QMessageBox.question(
                self,
                "Clear captured events?",
                "This clears the in-memory report history. Previously exported reports are not deleted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        self._report_events.clear()
        self._recorded_completion_keys.clear()
        self._report_event_counter = 0
        self._update_report_controls()

    def _render(self, result: LiveSensorResult) -> None:
        self._last_result = result
        if result.state == "WARMING_UP":
            count = min(self.warmup_progress.maximum(), self.warmup_progress.value() + 1)
            self.warmup_progress.setValue(count)
        elif result.state == "READY":
            self.warmup_progress.setValue(self.warmup_progress.maximum())
        displayed = result
        if result.sensor_mode in {"event_signal", "hybrid_force_event"}:
            if result.event_phase == "completed":
                self._last_completed_result = result
                self._capture_completed_event(result)
            elif result.state == "NO_CONTACT" and self._last_completed_result is not None:
                displayed = self._last_completed_result
            if result.sensor_mode == "hybrid_force_event":
                if displayed.event_phase == "completed":
                    if displayed.peak_force_N is None:
                        self.force_label.setText("—")
                    else:
                        self.force_label.setText(f"~{displayed.peak_force_N:.2f} N peak")
                elif displayed.force_N is None:
                    self.force_label.setText("—")
                elif displayed.contact:
                    self.force_label.setText(f"~{displayed.force_N:.2f} N")
                else:
                    self.force_label.setText(f"{displayed.force_N:.2f} N")
            elif displayed.peak_signal_ratio is None:
                self.force_label.setText("—")
            else:
                self.force_label.setText(f"{displayed.peak_signal_ratio:.2f}× threshold")
        elif result.force_N is None:
            self.force_label.setText("—")
        elif result.contact:
            self.force_label.setText(f"~{result.force_N:.2f} N")
        else:
            self.force_label.setText(f"{result.force_N:.2f} N")
        displayed_rois = displayed.displayed_rois
        if displayed_rois:
            joined = ", ".join(str(roi) for roi in displayed_rois)
            prefix = "ROIs" if len(displayed_rois) > 1 else "ROI"
            self.roi_label.setText(
                f"{prefix}: {joined} ({displayed.confidence})"
            )
        elif displayed.all_forced_rois:
            self.roi_label.setText("ROIs: uncertain (withheld)")
        else:
            self.roi_label.setText("ROI: —")
        self._highlight_rois(displayed_rois)
        label = result.state.replace("_", " ")
        self.state_label.setText(label)
        color = (
            "#b26a00"
            if result.state in {
                "EXPERIMENTAL_CONTACT_UNCERTAIN_ROI",
                "EVENT_ACTIVE_UNCERTAIN_ROI",
                "EVENT_COMPLETE_UNCERTAIN_ROI",
            }
            else (
                "#198754"
                if result.contact
                else ("#0d6efd" if result.state == "NO_CONTACT" else "#6c757d")
            )
        )
        self.state_label.setStyleSheet(
            f"padding: 7px; background: {color}; color: white; font-size: 15px; font-weight: 700;"
        )
        score_text = ""
        if result.contact_score is not None and result.threshold is not None:
            score_text = f" Score {result.contact_score:.3f}; threshold {result.threshold:.3f}."
        prefix = "Replay: " if self._demo_mode else ""
        self.message_label.setText(prefix + result.message + score_text)


__all__ = ["LiveSensorTab"]
