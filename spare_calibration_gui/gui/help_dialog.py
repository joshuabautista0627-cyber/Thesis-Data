"""Always-available, beginner-oriented in-application workflow help."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


HELP_TOPICS: tuple[tuple[str, str], ...] = (
    (
        "1. Identify and connect the Arducam IMX179",
        "Plug the camera directly into the Windows computer. Open Camera View, "
        "choose Refresh Cameras, select a device index, and connect. If Windows "
        "does not expose a camera name, identify the Arducam IMX179 from its live "
        "preview. Simulation is labeled explicitly and never means physical hardware is connected.",
    ),
    (
        "2. Orient and view the camera",
        "Try DirectShow first. If it cannot open the device, disconnect and use "
        "Media Foundation. Use Rotate clockwise and Mirror horizontally when needed, "
        "then start Preview. Rotation is applied before mirroring, and the same oriented "
        "image is used for preview, ROIs, analysis, baseline, and recording. Changing "
        "orientation invalidates the baseline; 90 or 270 degrees also swaps the ROI frame "
        "width and height. "
        "On connection, the GUI reads current values exposed by the camera driver. For "
        "a guaranteed match, connect with DirectShow and choose Open Windows Camera "
        "Properties; adjustments in that dialog affect the GUI's own live camera handle. "
        "The baseline is invalidated when the dialog closes. Windows Camera app filters "
        "or Studio Effects may be app-only and cannot be imported through OpenCV.",
    ),
    (
        "3. Use color motion magnification when needed",
        "The Original Frame remains the unannotated, software-oriented camera view. Open Motion "
        "Magnification and enable Color magnification. Apply automatically selects the "
        "motion-magnified color preview and uses HSV values from those magnified pixels "
        "for the force-light ROI graphs. The temporal graph defaults to Mean HSV V. "
        "Motion processing remains optional and never blocks camera connection or "
        "unloaded-baseline capture.",
    ),
    (
        "4. Define the nine ROIs",
        "In ROI and Baseline, draw or numerically edit exactly nine rectangles. "
        "They are ordered row-major as ROI 1–3, ROI 4–6, and ROI 7–9. Keep every "
        "rectangle inside the frame and resolve overlap warnings. Boxes and labels "
        "appear only on the preview copy; the experimental video stays unannotated. "
        "Changing an ROI invalidates the current baseline.",
    ),
    (
        "5. Capture the unloaded baseline",
        "Let the camera finish its warm-up, remove every load from the sensing skin, "
        "and capture the default three-second baseline with at least 20 valid frames. "
        "Accept it only when the live view is unloaded and the ROI layout is correct. Camera, "
        "orientation, ROI, stream resolution, or processing-setting changes invalidate it. Excessive "
        "pre-recording mean-V drift requires a new baseline.",
    ),
    (
        "6. Connect the Arduino Nano and HX711",
        "Upload the supplied hx711_nano_stream sketch, connect the Nano by USB, "
        "choose its COM port, and use the sketch's visible baud rate (115200 for the "
        "supplied firmware). Opening the port can reset the board, so a configurable "
        "startup delay is applied. HELLO is optional: the source becomes validated "
        "only after consecutive finite numeric sensor readings arrive. Plain numbers, "
        "RAW prefixes, timestamp CSV, and DATA lines are supported. Inspect the Raw "
        "Serial Monitor for rejected startup text or parser mismatch. A midstream "
        "board reset creates a new device session. Confirm fresh raw values, sample "
        "rate, and the absence of readiness or timeout errors before calibrating. "
        "Keep this load-cell connection on COM3 separate from the printer connection on COM4.",
    ),
    (
        "7. Calibrate with the 200 g mass",
        "First remove all load and capture the unloaded window. Then place the known "
        "200 g mass and capture the loaded window. The application checks sample "
        "count, signal-to-noise ratio, stability, and the signed counts-per-gram "
        "factor outside the GUI. Only after the calculation is accepted, remove the "
        "mass and run the stable unloaded tare. Tare invalidates any earlier "
        "verification. Replace the same known mass and verify it; the default ±5% "
        "verification must pass to record. Changing the entered mass invalidates "
        "the windows, tare, and verification, so restart the sequence.",
    ),
    (
        "8. Enter fixed trial labels",
        "In Recording and Synchronization, enter Session ID, Trial ID, sensing-skin ID, and "
        "target ROI ground truth (ROI 1–9). Optional specimen/participant ID, "
        "interaction class, force class, press number, and notes stay fixed for the "
        "whole trial. They are trial-level metadata, not automatic frame contact labels.",
    ),
    (
        "9. Record manually or run synchronized repeated printer presses",
        "Check that every readiness item is green, including valid baseline, verified "
        "load-cell calibration, writable output, video preflight, and disk space. "
        "Motion magnification is optional and changes pixels. If it is the force-light "
        "HSV source, create and use the calibration with that same source; original-frame "
        "and magnified-frame calibrations are not interchangeable. Its worker "
        "drops stale preview frames rather than blocking original capture. Choose any "
        "optional overlay, processed, or magnified video outputs before Start. "
        "For a manual trial, press Start Recording once, perform exactly one intentional press, "
        "then press Stop Recording. For automated repeated pressing, enter the trial labels but "
        "do not press Start Recording first: Start Repeated Press creates and owns its synchronized "
        "recording transaction. If a manual recording is active, stop it and wait for finalization. "
        "Then open Printer Motion, "
        "connect COM4 at 115200, verify the Marlin M115 identity, explicitly home only after "
        "switching on the Ender 3 main PSU and checking clearance, query M114, jog to the safe "
        "starting point, and set the in-memory XYZ press zero. USB serial activity alone does not "
        "prove that the motors have main power. Press zero never sends G92 and is invalidated by reconnect, homing, "
        "communication loss, or emergency stop. Confirm that negative machine Z moves down, "
        "preview the computed press-zero-plus-displacement target, and use Test One Cycle before "
        "a full run. Start Repeated Press automatically starts recording, captures pre-roll, "
        "performs continuous down/hold/up cycles with M400 synchronization, and captures post-roll. "
        "Pause finishes a safe retract before waiting; Stop and Abort attempt return to press zero. "
        "The red M112 action is immediate. Keep hands clear and never rely on software alone to "
        "prevent collision or specimen overload.",
    ),
    (
        "10. Find and understand the outputs",
        "The final output directory is shown in Recording and Synchronization and can be "
        "opened from the review screen. It contains one unannotated session_video.mp4 "
        "or AVI fallback, frame_features.csv, loadcell_raw.csv, "
        "master_synchronized.csv, session_config.json, camera_settings.json, "
        "roi_layout.json, loadcell_calibration.json, baseline_summary.json, "
        "baseline_data.npz, data_dictionary.csv, application.log, "
        "session_status.json, and spatial_graphs. Automated sessions also contain "
        "printer_sequence.json, while every frame/load-cell row includes sequence, phase, cycle, "
        "command, press-zero, computed target, commanded coordinate/feed, actual M114-reported "
        "coordinate when available, force-limit, pause, and abort provenance. Printer profiles "
        "never persist an active press zero or a prior direction confirmation. Partial files are preserved after "
        "an interruption; never overwrite or rename them before recovery inspection.",
    ),
)


class HelpDialog(QDialog):
    """Non-modal scrollable guide containing the complete essential workflow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("help_dialog")
        self.setWindowTitle("Camera, Load-Cell, and Printer Calibration Help")
        self.setModal(False)
        screen = self.screen() or QApplication.primaryScreen()
        scale = max(1.0, float(screen.devicePixelRatio())) if screen is not None else 1.0
        available = screen.availableGeometry() if screen is not None else None
        maximum_width = min(
            available.width() if available is not None else 1366,
            int(1366 // scale),
        )
        maximum_height = min(
            available.height() if available is not None else 768,
            int(768 // scale),
        )
        self.setMinimumSize(min(560, maximum_width), min(440, maximum_height))
        self.resize(min(760, maximum_width), min(650, maximum_height))

        layout = QVBoxLayout(self)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("help_scroll_area")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        layout.addWidget(self.scroll_area, 1)

        content = QWidget()
        content.setObjectName("help_scroll_content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(10)
        self.scroll_area.setWidget(content)

        title = QLabel("Camera, Load-Cell, and Ender 3 Calibration Workflow")
        title.setObjectName("help_title_label")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content_layout.addWidget(title)

        introduction = QLabel(
            "Follow these steps in order for either one manual sensing-skin press or a "
            "synchronized repeated Ender 3 press sequence. Camera, load-cell, and printer "
            "serial ownership remain independent, and hardware actions run in workers so "
            "this help remains available."
        )
        introduction.setObjectName("help_introduction_label")
        introduction.setWordWrap(True)
        introduction.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content_layout.addWidget(introduction)

        self.topic_labels: list[QLabel] = []
        for index, (heading, body) in enumerate(HELP_TOPICS, start=1):
            group = QGroupBox(heading)
            group.setObjectName(f"help_topic_{index}")
            group_layout = QVBoxLayout(group)
            label = QLabel(body)
            label.setObjectName(f"help_topic_{index}_text")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setToolTip(body)
            group_layout.addWidget(label)
            content_layout.addWidget(group)
            self.topic_labels.append(label)

        graphs_group = QGroupBox("Live views and saved graphs")
        graphs_layout = QVBoxLayout(graphs_group)
        graphs_label = QLabel(
            "Use the graph selector to switch among Heatmap, Temporal ROI Lines, "
            "and Current Spatial Profile. Manual Save Spatial Graph or Save Line "
            "Graph actions create PNG plus CSV and JSON companions from the exact "
            "displayed values. Finalization also creates peak, mean-contact, and "
            "integrated heatmaps, the complete-trial ROI temporal graph, and the "
            "peak-frame spatial profile. CSV timestamps remain authoritative."
        )
        graphs_label.setObjectName("help_graphs_text")
        graphs_label.setWordWrap(True)
        graphs_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        graphs_layout.addWidget(graphs_label)
        content_layout.addWidget(graphs_group)
        content_layout.addStretch(1)

        self.button_box = QDialogButtonBox(QDialogButtonBox.Close)
        self.button_box.rejected.connect(self.close)
        layout.addWidget(self.button_box)

    @property
    def help_text(self) -> str:
        """Return all required workflow text for search/accessibility tests."""

        return "\n".join(
            f"{heading}\n{body}" for heading, body in HELP_TOPICS
        )


__all__ = ["HELP_TOPICS", "HelpDialog"]
