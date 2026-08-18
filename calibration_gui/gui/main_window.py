"""Qt application controller for the four-tab calibration workflow."""

from __future__ import annotations

import copy
from contextlib import suppress
import csv
from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
from threading import Event, Lock
import time
from typing import Any, Callable, Mapping
import uuid

import cv2
import numpy as np
from PySide6.QtCore import QEventLoop, QMetaObject, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QImage
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.models import (
    ApplicationConfig,
    CameraConfig,
    CalibrationVerification,
    ErrorCode,
    LoadCellCalibration,
    LoadCellConfig,
    ReadinessFlags,
    RecordingLifecycle,
    TrialLabels,
)
from core.motion import MotionContext
from data.exporter import atomic_write_json
from data.graph_exporter import (
    GraphContext,
    GraphExportService,
    SpatialGraphSnapshot,
    TemporalGraphSnapshot,
)
from gui.camera_tab import CameraTab
from gui.help_dialog import HelpDialog
from gui.loadcell_tab import LoadCellTab
from gui.live_sensor_tab import LiveSensorTab
from gui.printer_motion_tab import PrinterMotionTab
from gui.processing_tabs import (
    GraphExportSettingsTab,
    ImageProcessingTab,
    MotionMagnificationTab,
    SystemStatusTab,
    VideoDisplayArea,
)
from gui.recording_tab import RecordingTab
from gui.roi_baseline_tab import OpticalGraphPanel, ROIBaselineTab
from processing.baseline import (
    BaselineContext,
    capture_baseline,
    check_frame_baseline_drift,
    invalidate_for_camera_change,
    invalidate_for_roi_change,
    validate_baseline_context,
)
from processing.camera_orientation import oriented_frame_size
from processing.loadcell_calibration import (
    build_saved_calibration,
    calculate_calibration,
    calculate_tare,
    verify_calibration,
)
from processing.motion_magnification import MotionMagnificationConfig
from processing.pipeline import ProcessingPipeline
from processing.preview_processing import build_processed_preview
from processing.roi_manager import ROILayout, load_roi_layout, save_roi_layout
from scripts.simulation_smoke import run_simulation_smoke
from services.camera_service import SUPPORTED_CAMERA_PROPERTIES
from services.qt_workers import (
    CameraWorker,
    FunctionThread,
    LatestValueBuffer,
    LiveOpticalThread,
    MotionMagnificationThread,
    RecordingThread,
    SampleWindowCollector,
    SerialWorker,
)
from services.printer_worker import PrinterWorker
from services.repeated_press_controller import RepeatedPressConfig, SequenceResult
from services.session_recorder import (
    RecordingCompletion,
    SessionRecorder,
    preflight_recording_output,
)


_METRIC_COLUMNS = {
    "Mean delta V": "delta_v_mean",
    "Integrated delta V": "delta_v_sum",
    "Maximum delta V": "delta_v_max",
    "Mean HSV V (0-255)": "mean_v",
    "Raw mean V": "mean_v",
}
_METRIC_CODES = {
    "Mean delta V": "mean_delta_v",
    "Integrated delta V": "integrated_delta_v",
    "Maximum delta V": "maximum_delta_v",
    "Mean HSV V (0-255)": "raw_mean_v",
    "Raw mean V": "raw_mean_v",
}
_REQUIREMENT_TAB_INDEX = {
    "camera_connected": 0,
    "rois_valid": 1,
    "baseline_valid": 1,
    "serial_connected": 4,
    "loadcell_calibrated": 4,
    "calibration_verified": 4,
    "trial_labels_valid": 5,
    "output_directory_valid": 5,
    "video_writer_preflight_passed": 5,
    "disk_space_sufficient": 5,
}
def _processing_settings(config: ApplicationConfig) -> dict[str, object]:
    return {
        "minimum_saturation_for_h": config.processing.minimum_saturation_for_h,
        "active_delta_v_threshold": config.processing.active_delta_v_threshold,
        "localization_min_mean_delta_v": (
            config.processing.localization_min_mean_delta_v
        ),
    }


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_serial_log(path: Path, entries: tuple[Mapping[str, object], ...]) -> Path:
    """Persist raw serial diagnostics without touching the acquisition owner."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        for item in entries:
            handle.write(
                "\t".join(
                    (
                        str(item.get("wall_clock_iso", "")),
                        str(item.get("host_monotonic_ns", "")),
                        str(item.get("status", "")),
                        str(item.get("parsed_value", "")),
                        str(item.get("raw_line", "")).replace("\t", " "),
                        str(item.get("message", "")).replace("\t", " "),
                    )
                )
                + "\n"
            )
    return destination


def _write_frame_image(path: Path, frame_bgr: np.ndarray) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing image: {destination}")
    if not cv2.imwrite(str(destination), np.asarray(frame_bgr)):
        raise OSError(f"OpenCV could not write image: {destination}")
    return destination


def build_review_summary(
    completion: RecordingCompletion | Path,
    *,
    preview_drops: int,
    recording_errors: int,
    manual_heatmap_count: int = 0,
    manual_line_graph_count: int = 0,
) -> dict[str, object]:
    """Build every required review field for a complete or recoverable partial."""

    is_complete = isinstance(completion, RecordingCompletion)
    directory = (
        completion.session_directory if is_complete else Path(completion)
    )
    status_path = directory / "session_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    frame_path = directory / "master_synchronized.csv"
    if not frame_path.is_file():
        frame_path = directory / "frame_features.csv.partial"
    load_path = directory / "loadcell_raw.csv"
    if not load_path.is_file():
        load_path = directory / "loadcell_raw.csv.partial"
    master = _read_csv(frame_path) if frame_path.is_file() else []
    loads = _read_csv(load_path) if load_path.is_file() else []
    elapsed = [
        value
        for value in (_finite(row.get("elapsed_time_s")) for row in master)
        if value is not None
    ]
    synchronized_forces = [
        value
        for value in (_finite(row.get("force_N")) for row in master)
        if value is not None
    ]
    raw_forces = [
        value
        for value in (_finite(row.get("force_N")) for row in loads)
        if value is not None
    ]
    forces = synchronized_forces if is_complete else raw_forces
    gaps = [
        value
        for value in (_finite(row.get("nearest_sample_gap_ms")) for row in master)
        if value is not None
    ]
    optical_by_frame: list[tuple[float, int, tuple[float, ...]]] = []
    totals = np.zeros(9, dtype=np.float64)
    valid_feature_rows = 0
    for row_index, row in enumerate(master):
        values = tuple(_finite(row.get(f"roi{roi}_delta_v_mean")) for roi in range(1, 10))
        if all(value is not None for value in values):
            numeric = tuple(float(value) for value in values if value is not None)
            totals += np.asarray(numeric)
            optical_by_frame.append((max(numeric), row_index, numeric))
            valid_feature_rows += 1
    peak = max((item[0] for item in optical_by_frame), default=math.nan)
    highest_roi = int(np.argmax(totals)) + 1 if optical_by_frame else "unavailable"
    duration = max(elapsed) - min(elapsed) if len(elapsed) >= 2 else 0.0
    camera_fps = (len(master) - 1) / duration if duration > 0 and len(master) > 1 else 0.0
    load_elapsed = [
        value
        for value in (_finite(row.get("elapsed_time_s")) for row in loads)
        if value is not None
    ]
    load_duration = (
        max(load_elapsed) - min(load_elapsed) if len(load_elapsed) >= 2 else 0.0
    )
    load_rate = (
        (len(load_elapsed) - 1) / load_duration
        if load_duration > 0 and len(load_elapsed) > 1
        else 0.0
    )
    graph_files = tuple((directory / "spatial_graphs").glob("*"))
    manual_heatmaps = sum(path.name.startswith("snapshot_") for path in graph_files)
    manual_lines = sum(
        path.name.startswith(("temporal_lines_", "spatial_profile_"))
        for path in graph_files
    )
    row_count = max(len(master), 1)
    accepted_frame_count = (
        completion.accepted_frame_count
        if is_complete
        else int(status.get("feature_row_count", len(master)))
    )
    video_frame_count = (
        completion.video_frame_count
        if is_complete
        else int(status.get("video_frame_count", 0))
    )
    loadcell_sample_count = (
        completion.loadcell_sample_count
        if is_complete
        else int(status.get("loadcell_sample_count", len(loads)))
    )
    return {
        "duration_s": f"{duration:.3f}",
        "recorded_frame_count": accepted_frame_count,
        "video_frame_count": video_frame_count,
        "loadcell_sample_count": loadcell_sample_count,
        "actual_camera_fps": f"{camera_fps:.3f}",
        "loadcell_sample_rate_hz": f"{load_rate:.3f}",
        "preview_drops": int(preview_drops),
        "recording_errors": int(recording_errors),
        "minimum_force_N": f"{min(forces):.6g}" if forces else "unavailable",
        "maximum_force_N": f"{max(forces):.6g}" if forces else "unavailable",
        "mean_force_N": f"{float(np.mean(forces)):.6g}" if forces else "unavailable",
        "peak_optical_intensity": f"{peak:.6g}" if math.isfinite(peak) else "unavailable",
        "highest_total_response_roi": highest_roi,
        "missing_feature_percent": f"{100.0 * (len(master) - valid_feature_rows) / row_count:.3f}",
        "missing_force_percent": (
            f"{100.0 * (len(master) - len(synchronized_forces)) / row_count:.3f}"
            if is_complete
            else "unavailable (partial; synchronization not finalized)"
        ),
        "maximum_sync_gap_ms": f"{max(gaps):.3f}" if gaps else "unavailable",
        "manual_heatmap_count": max(manual_heatmaps // 3, int(manual_heatmap_count)),
        "manual_line_graph_count": max(manual_lines // 3, int(manual_line_graph_count)),
        "automatic_heatmap_summary_status": status.get(
            "automatic_heatmap_summary_status", "unavailable"
        ),
        "automatic_temporal_spatial_status": (
            f"temporal={status.get('automatic_temporal_line_status', 'unavailable')}; "
            f"profile={status.get('automatic_spatial_profile_status', 'unavailable')}"
        ),
        "final_output_directory": str(directory.resolve()),
        "session_status": status.get("session_status", "unknown"),
    }


def verify_and_build_calibration(
    calibration: LoadCellCalibration,
    samples: tuple[int, ...],
    *,
    config: LoadCellConfig,
    minimum_sample_count: int,
    serial_identity: Mapping[str, str],
) -> tuple[LoadCellCalibration | None, object]:
    """Verify the current retared calibration without replacing provenance."""

    verification = verify_calibration(
        samples,
        calibration.known_mass_g,
        calibration.tare_raw,
        calibration.counts_per_gram,
        tolerance_percent=config.verification_tolerance_percent,
        minimum_sample_count=minimum_sample_count,
    )
    if not verification.passed:
        return None, verification
    if serial_identity["protocol_version"] != calibration.protocol_version:
        raise ValueError("Serial protocol identity changed before verification completed.")
    verified = replace(
        calibration,
        verification=CalibrationVerification(
            expected_gf=verification.expected_gf,
            measured_mean_gf=verification.measured_mean_gf,
            absolute_error_gf=verification.absolute_error_gf,
            percentage_error=verification.percentage_error,
            measured_std_gf=verification.measured_std_gf,
            tolerance_percent=verification.tolerance_percent,
            sample_count=verification.sample_count,
            minimum_sample_count=verification.minimum_sample_count,
            passed=verification.passed,
            rejection_reasons=verification.rejection_reasons,
        ),
    )
    return verified, verification


class MainWindow(QMainWindow):
    """Coordinate presentation widgets and strict single-owner worker services."""

    camera_refresh_command = Signal()
    camera_connect_command = Signal(str, object, str)
    camera_disconnect_command = Signal()
    camera_start_command = Signal()
    camera_stop_command = Signal()
    camera_orientation_command = Signal(int, bool)
    camera_capabilities_command = Signal()
    camera_native_properties_command = Signal()
    serial_refresh_command = Signal()
    serial_connect_command = Signal(str, object, str, object)
    serial_disconnect_command = Signal()
    serial_calibration_command = Signal(object)
    printer_connect_command = Signal(str, int)
    printer_home_command = Signal()
    printer_start_command = Signal(object)
    printer_emergency_command = Signal()
    printer_abort_command = Signal()
    printer_prerequisites_command = Signal(bool, str)
    graph_message = Signal(str, bool)
    manual_graph_saved = Signal(str)

    def __init__(
        self,
        config: ApplicationConfig | None = None,
        *,
        default_simulation: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        project_root = Path(__file__).resolve().parents[1]
        self.config = config or ApplicationConfig.load_json(
            project_root / "config" / "default_config.json"
        )
        self.setObjectName("main_window")
        self.setWindowTitle("SPARE Camera, Load-Cell, and Ender 3 Calibration")
        self.setMinimumSize(800, 480)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        scale = max(1.0, float(screen.devicePixelRatio())) if screen is not None else 1.0
        available_width = min(
            available.width() if available is not None else 1366,
            math.floor(1366 / scale),
        )
        available_height = min(
            available.height() if available is not None else 768,
            math.floor(768 / scale),
        )
        self.resize(
            max(800, min(1366, available_width)),
            max(480, min(768, available_height)),
        )

        self.readiness = ReadinessFlags()
        self.lifecycle = RecordingLifecycle.IDLE
        self._active_camera_config = self.config.camera
        self._active_loadcell_config = self.config.loadcell
        self._camera_info: object | None = None
        self._serial_info: object | None = None
        self._serial_transport_connected = False
        # Current driver values are read for reproducibility. Property changes
        # are only operator-initiated through the active DirectShow native dialog.
        self._camera_requested_controls: dict[str, float | bool] = {}
        self._camera_controls: dict[str, float | bool] = {}
        self._camera_property_readbacks: list[dict[str, object]] = []
        self._camera_capabilities: list[dict[str, object]] = []
        self._camera_mode_readbacks: list[dict[str, object]] = []
        self._camera_mode_confirmed = False
        self._automatic_control_warning = ""
        self._camera_connected_at_ns = 0
        self._camera_warmup_origin_ns = 0
        self._latest_captured: object | None = None
        self._latest_optical_row: Mapping[str, object] | None = None
        self._baseline = None
        self._pending_baseline = None
        self._baseline_context: BaselineContext | None = None
        self._baseline_capture_frames: list[np.ndarray] = []
        self._baseline_capture_first_ns: int | None = None
        self._baseline_capture_last_source_id: int | None = None
        self._baseline_generation = 0
        self._baseline_locked = False
        self._calibration: LoadCellCalibration | None = None
        self._calibration_computation: object | None = None
        self._calibration_windows: dict[str, tuple[int, ...]] = {}
        self._loadcell_workflow_generation = 0
        self._loadcell_pending_job: tuple[int, str] | None = None
        self._sample_window_generation: int | None = None
        self._sample_collector = SampleWindowCollector()
        self._measured_sample_rate_hz = 80.0
        self._measured_capture_fps = float("nan")
        self._output_directory: Path | None = None
        self._active_session_directory: Path | None = None
        self._recording_thread: RecordingThread | None = None
        self._recording_started_perf_ns = 0
        self._recording_frame_count = 0
        self._recording_error_count = 0
        self._recording_preview_drop_baseline = 0
        self._review_generation = 0
        self._manual_heatmap_count = 0
        self._manual_line_graph_count = 0
        self._preparing_recording = False
        self._recording_prepare_generation = 0
        self._recording_prepare_label_values: dict[str, object] = {}
        self._live_analysis_thread: LiveOpticalThread | None = None
        self._retiring_live_analysis_threads: set[LiveOpticalThread] = set()
        self._live_analysis_generation = 0
        self._motion_config = MotionMagnificationConfig()
        self._motion_thread: MotionMagnificationThread | None = None
        self._retiring_motion_threads: set[MotionMagnificationThread] = set()
        self._latest_motion_result: object | None = None
        self._latest_processed_bgr: np.ndarray | None = None
        self._quantitative_analysis_source = "Background-subtracted frame"
        self._jobs: set[FunctionThread] = set()
        self._graph_service: GraphExportService | None = None
        self._graph_futures: set[object] = set()
        self._graph_futures_lock = Lock()
        self._preflight_generation = 0
        self._preview_display_count = 0
        self._preview_started_ns = time.perf_counter_ns()
        self._last_preview_refresh_ns = self._preview_started_ns
        self._closing = False
        self._shutdown_worker_wait_ms = 30_000
        self._shutdown_owner_wait_ms = 3_000
        self.motion_context = MotionContext()
        self._printer_connection_info: dict[str, object] | None = None
        self._pending_printer_sequence: RepeatedPressConfig | None = None
        self._printer_sequence_preparation_stage = ""
        self._printer_automated_recording = False
        self._printer_tare_bridge_event: Event | None = None
        self._printer_tare_bridge_error = ""
        self._printer_baseline_bridge_event: Event | None = None
        self._printer_baseline_bridge_error = ""

        self.camera_tab = CameraTab(
            rotation_degrees=self.config.camera.rotation_degrees,
            mirror_horizontal=self.config.camera.mirror_horizontal,
        )
        initial_frame_size = oriented_frame_size(
            self.config.camera.requested_width,
            self.config.camera.requested_height,
            self.config.camera.rotation_degrees,
        )
        self.roi_tab = ROIBaselineTab(
            frame_size=initial_frame_size
        )
        self.loadcell_tab = LoadCellTab(
            plot_history_s=self.config.visualization.temporal_history_s
        )
        self.recording_tab = RecordingTab()
        self.printer_tab = PrinterMotionTab(self.config.printer)
        self.image_processing_tab = ImageProcessingTab()
        self.motion_tab = MotionMagnificationTab()
        self.graph_export_tab = GraphExportSettingsTab()
        self.system_status_tab = SystemStatusTab()
        self.live_sensor_tab = LiveSensorTab(
            project_root / "models" / "live_sensor_experimental_hybrid_v4"
        )
        self.video_display = VideoDisplayArea()
        self.help_dialog = HelpDialog(self)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("main_tabs")
        self.tabs.addTab(self.camera_tab, "Camera View")
        self.tabs.addTab(self.roi_tab, "ROI and Baseline Settings")
        self.tabs.addTab(self.image_processing_tab, "Image and HSV Processing")
        self.tabs.addTab(self.motion_tab, "Motion Magnification")
        self.tabs.addTab(self.loadcell_tab, "Load Cell and Calibration")
        self.tabs.addTab(self.recording_tab, "Recording and Synchronization")
        self.tabs.addTab(self.printer_tab, "Printer Motion")
        self.tabs.addTab(self.graph_export_tab, "Graph and Export Settings")
        self.tabs.addTab(self.system_status_tab, "System Status and Logs")
        self.tabs.addTab(self.live_sensor_tab, "Live Sensor (Experimental)")

        central = QWidget()
        layout = QVBoxLayout(central)
        header = QHBoxLayout()
        title = QLabel("Camera + HX711 + Ender 3 calibration workflow")
        title.setStyleSheet("font-size: 17px; font-weight: 650;")
        self.hardware_notice = QLabel(
            "Physical hardware status is shown per connection; simulation is explicitly labeled."
        )
        self.hardware_notice.setWordWrap(True)
        self.help_button = QPushButton("Help")
        self.help_button.setObjectName("always_available_help_button")
        header.addWidget(title)
        header.addWidget(self.hardware_notice, 1)
        header.addWidget(self.help_button)
        layout.addLayout(header)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("main_video_control_splitter")
        self.main_splitter.addWidget(self.video_display)
        self.main_splitter.addWidget(self.tabs)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([820, 520])
        layout.addWidget(self.main_splitter, 1)
        self.setCentralWidget(central)

        self.preview_buffer = LatestValueBuffer()
        self.sample_buffer = LatestValueBuffer()
        # Live analysis can retire non-blockingly, so it must never share a
        # newest-only slot with authoritative recording completion counters.
        self.live_optical_buffer = LatestValueBuffer()
        self.motion_result_buffer = LatestValueBuffer()
        self.recording_display_buffer = LatestValueBuffer()
        self.camera_thread = QThread(self)
        self.camera_thread.setObjectName("camera_owner_thread")
        self.camera_worker = CameraWorker(self.preview_buffer)
        self.camera_worker.moveToThread(self.camera_thread)
        self.serial_thread = QThread(self)
        self.serial_thread.setObjectName("serial_owner_thread")
        self.serial_worker = SerialWorker(self.sample_buffer)
        self.serial_worker.set_sample_observer(self._observe_loadcell_sample)
        self.serial_worker.moveToThread(self.serial_thread)
        self.printer_thread = QThread(self)
        self.printer_thread.setObjectName("printer_owner_thread")
        self.printer_worker = PrinterWorker(
            self.config.printer,
            self.motion_context,
            tare_callback=self._printer_tare_bridge,
            baseline_callback=self._printer_baseline_bridge,
        )
        self.printer_worker.moveToThread(self.printer_thread)
        self._wire_workers()
        self._wire_ui()
        self.camera_thread.start()
        self.serial_thread.start()
        self.printer_thread.start()

        if default_simulation:
            self.camera_tab.source_mode_combo.setCurrentIndex(1)
            self.loadcell_tab.source_mode_combo.setCurrentIndex(1)
            self.recording_tab.set_simulation_mode(True)

        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._refresh_preview)
        self.preview_timer.start(
            max(1, round(1000.0 / self.config.visualization.preview_refresh_hz))
        )
        self.force_timer = QTimer(self)
        self.force_timer.timeout.connect(self._refresh_force)
        self.force_timer.start(
            max(1, round(1000.0 / self.config.visualization.force_plot_refresh_hz))
        )
        self.graph_timer = QTimer(self)
        self.graph_timer.timeout.connect(self._refresh_optical_graphs)
        self.graph_timer.start(
            max(1, round(1000.0 / self.config.visualization.heatmap_refresh_hz))
        )
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start(
            max(1, round(1000.0 / self.config.visualization.status_refresh_hz))
        )
        self.readiness.rois_valid = self.roi_tab.validation.valid
        self._refresh_readiness()

    def _wire_workers(self) -> None:
        self.camera_refresh_command.connect(self.camera_worker.refresh_devices)
        self.camera_connect_command.connect(self.camera_worker.connect_camera)
        self.camera_disconnect_command.connect(self.camera_worker.disconnect_camera)
        self.camera_start_command.connect(self.camera_worker.start_preview)
        self.camera_stop_command.connect(self.camera_worker.stop_preview)
        self.camera_orientation_command.connect(
            self.camera_worker.set_orientation,
            Qt.ConnectionType.QueuedConnection,
        )
        self.camera_capabilities_command.connect(
            self.camera_worker.refresh_capabilities,
            Qt.ConnectionType.QueuedConnection,
        )
        self.camera_native_properties_command.connect(
            self.camera_worker.open_native_properties,
            Qt.ConnectionType.QueuedConnection,
        )
        self.camera_worker.devices_found.connect(self.camera_tab.set_camera_devices)
        self.camera_worker.connected.connect(self._camera_connected)
        self.camera_worker.disconnected.connect(self._camera_disconnected)
        self.camera_worker.mode_confirmation.connect(self._camera_mode_confirmation)
        self.camera_worker.capabilities_found.connect(
            self._camera_capabilities_received
        )
        self.camera_worker.native_properties_result.connect(
            self._native_camera_properties_result
        )
        self.camera_worker.metrics.connect(self._camera_metrics)
        self.camera_worker.error.connect(lambda text: self._worker_error("Camera", text))
        self.camera_worker.source_exhausted.connect(self._camera_source_exhausted)

        self.serial_refresh_command.connect(self.serial_worker.refresh_ports)
        self.serial_connect_command.connect(self.serial_worker.connect_serial)
        self.serial_disconnect_command.connect(self.serial_worker.disconnect_serial)
        self.serial_calibration_command.connect(self.serial_worker.set_calibration)
        self.serial_worker.ports_found.connect(self.loadcell_tab.set_serial_ports)
        self.serial_worker.connected.connect(self._serial_connected)
        self.serial_worker.disconnected.connect(self._serial_disconnected)
        self.serial_worker.diagnostics_available.connect(self._serial_diagnostics)
        self.serial_worker.stream_health_changed.connect(self._serial_health_changed)
        self.serial_worker.error.connect(lambda text: self._worker_error("Load cell", text))

        self.printer_worker.ports_refreshed.connect(self.printer_tab.set_ports)
        self.printer_worker.connected.connect(self._printer_connected)
        self.printer_worker.disconnected.connect(self._printer_disconnected)
        self.printer_worker.log_received.connect(self.printer_tab.append_log)
        self.printer_worker.state_changed.connect(self.printer_tab.update_printer_state)
        self.printer_worker.command_changed.connect(self.printer_tab.update_command)
        self.printer_worker.operation_changed.connect(self.printer_tab.update_operation)
        self.printer_worker.sequence_changed.connect(self.printer_tab.update_sequence)
        self.printer_worker.sequence_completed.connect(self._printer_sequence_completed)
        self.printer_worker.error.connect(self._printer_error)
        self.printer_connect_command.connect(self.printer_worker.connect_printer)
        self.printer_home_command.connect(
            self.printer_worker.home_all,
            Qt.ConnectionType.QueuedConnection,
        )
        self.printer_start_command.connect(self.printer_worker.start_sequence)
        self.printer_emergency_command.connect(self.printer_worker.emergency_stop)
        self.printer_abort_command.connect(self.printer_worker.abort_sequence)
        self.printer_prerequisites_command.connect(
            self.printer_worker.set_prerequisites
        )

    def _wire_ui(self) -> None:
        self.help_button.clicked.connect(self.help_dialog.show)
        self.video_display.processed_mode_changed.connect(
            self.image_processing_tab.processed_mode_combo.setCurrentText
        )
        self.image_processing_tab.processed_mode_changed.connect(
            self.video_display.processed_mode_combo.setCurrentText
        )
        self.image_processing_tab.analysis_source_changed.connect(
            self._analysis_source_changed
        )
        self.video_display.save_original_requested.connect(
            lambda: self._save_frame_snapshot(original=True)
        )
        self.video_display.save_processed_requested.connect(
            lambda: self._save_frame_snapshot(original=False)
        )
        self.motion_tab.apply_requested.connect(self._apply_motion_config)
        self.motion_tab.reset_filter_requested.connect(self._reset_motion_filter)
        self.motion_tab.save_profile_requested.connect(self._save_motion_profile)
        self.motion_tab.load_profile_requested.connect(self._load_motion_profile)
        self.graph_export_tab.save_spatial_requested.connect(
            lambda: self._save_spatial_graph(
                self.recording_tab.graph_panel.current_snapshot()
            )
        )
        self.graph_export_tab.save_lines_requested.connect(
            lambda: self._save_line_graph(
                self.recording_tab.graph_panel.current_snapshot()
            )
        )
        self.camera_tab.refresh_devices_requested.connect(self.camera_refresh_command.emit)
        self.camera_tab.connect_requested.connect(self._connect_camera)
        self.camera_tab.disconnect_requested.connect(self.camera_disconnect_command.emit)
        self.camera_tab.start_preview_requested.connect(self._start_preview)
        self.camera_tab.stop_preview_requested.connect(self._stop_preview)
        self.camera_tab.orientation_changed.connect(self._camera_orientation_changed)
        self.camera_tab.source_file_requested.connect(self._choose_video_source)
        self.camera_tab.driver_settings_refresh_requested.connect(
            self._refresh_camera_driver_settings
        )
        self.camera_tab.native_properties_requested.connect(
            self._open_native_camera_properties
        )

        self.roi_tab.roi_layout_changed.connect(self._roi_changed)
        self.roi_tab.rois_valid_changed.connect(self._rois_valid_changed)
        self.roi_tab.baseline_capture_requested.connect(self._start_baseline_capture)
        self.roi_tab.baseline_accept_requested.connect(self._accept_baseline)
        self.roi_tab.baseline_repeat_requested.connect(self._discard_pending_baseline)
        self.roi_tab.save_roi_layout_requested.connect(self._save_roi_layout)
        self.roi_tab.load_roi_layout_requested.connect(self._load_roi_layout)
        self.roi_tab.save_spatial_graph_requested.connect(self._save_spatial_graph)
        self.roi_tab.save_line_graph_requested.connect(self._save_line_graph)

        self.loadcell_tab.refresh_ports_requested.connect(self.serial_refresh_command.emit)
        self.loadcell_tab.connect_requested.connect(self._connect_serial)
        self.loadcell_tab.disconnect_requested.connect(self.serial_disconnect_command.emit)
        self.loadcell_tab.playback_file_requested.connect(self._choose_playback_source)
        self.loadcell_tab.calibration_unloaded_requested.connect(
            lambda: self._start_sample_window("unloaded")
        )
        self.loadcell_tab.calibration_loaded_requested.connect(
            lambda _mass: self._start_sample_window("loaded")
        )
        self.loadcell_tab.calibration_calculate_requested.connect(
            self._calculate_calibration
        )
        self.loadcell_tab.verification_requested.connect(
            lambda _mass: self._start_sample_window("verify")
        )
        self.loadcell_tab.tare_requested.connect(lambda: self._start_sample_window("tare"))
        self.loadcell_tab.known_mass_changed.connect(self._known_mass_changed)
        self.loadcell_tab.save_calibration_requested.connect(self._save_calibration)
        self.loadcell_tab.load_calibration_requested.connect(self._load_calibration)
        self.loadcell_tab.start_calibration_requested.connect(
            lambda: self._start_sample_window("unloaded")
        )
        self.loadcell_tab.cancel_calibration_requested.connect(
            lambda: self._cancel_sample_window("cancelled by operator")
        )
        self.loadcell_tab.reset_calibration_requested.connect(self._reset_calibration)
        self.loadcell_tab.save_serial_log_requested.connect(self._save_serial_log)

        self.recording_tab.trial_labels_changed.connect(self._trial_labels_changed)
        self.recording_tab.output_directory_requested.connect(self._choose_output_directory)
        self.recording_tab.start_recording_requested.connect(self._request_recording)
        self.recording_tab.stop_recording_requested.connect(self._stop_recording)
        self.recording_tab.open_prerequisite_tab_requested.connect(
            lambda requirement: self.tabs.setCurrentIndex(
                _REQUIREMENT_TAB_INDEX.get(str(requirement), 3)
            )
        )
        self.recording_tab.run_simulation_smoke_requested.connect(self._run_smoke)
        self.recording_tab.open_output_directory_requested.connect(self._open_directory)
        self.recording_tab.save_spatial_graph_requested.connect(self._save_spatial_graph)
        self.recording_tab.save_line_graph_requested.connect(self._save_line_graph)
        self.graph_message.connect(self._graph_result_message)
        self.manual_graph_saved.connect(self._manual_graph_completed)

        self.printer_tab.refresh_ports_requested.connect(
            self.printer_worker.refresh_ports
        )
        self.printer_tab.connect_requested.connect(self._connect_printer)
        self.printer_tab.disconnect_requested.connect(
            self.printer_worker.disconnect_printer
        )
        self.printer_tab.home_requested.connect(self._confirm_and_home_printer)
        self.printer_tab.query_position_requested.connect(
            self.printer_worker.query_position
        )
        self.printer_tab.jog_requested.connect(self.printer_worker.jog)
        self.printer_tab.set_press_zero_requested.connect(
            self.printer_worker.set_press_zero
        )
        self.printer_tab.clear_press_zero_requested.connect(
            self.printer_worker.clear_press_zero
        )
        self.printer_tab.preview_requested.connect(
            self.printer_worker.preview_sequence
        )
        self.printer_tab.test_cycle_requested.connect(
            self._printer_sequence_requested
        )
        self.printer_tab.start_requested.connect(
            self._printer_sequence_requested
        )
        self.printer_tab.pause_requested.connect(
            self.printer_worker.pause_sequence
        )
        self.printer_tab.resume_requested.connect(
            self.printer_worker.resume_sequence
        )
        self.printer_tab.stop_requested.connect(
            self.printer_worker.stop_sequence
        )
        self.printer_tab.abort_requested.connect(
            self.printer_worker.abort_sequence
        )
        self.printer_tab.emergency_stop_requested.connect(
            self._confirm_emergency_stop
        )
        self.printer_tab.save_profile_requested.connect(
            self._save_printer_profile
        )
        self.printer_tab.load_profile_requested.connect(
            self._load_printer_profile
        )

    def _observe_loadcell_sample(self, sample: object) -> None:
        """Fan out COM3 samples without ever allowing that owner to write COM4."""

        self._sample_collector.accept(sample)
        self.printer_worker.observe_force_threadsafe(sample)

    def _connect_printer(self, port: str, baud_rate: int) -> None:
        selected = str(port).strip()
        load_port = ""
        if self._serial_info is not None and not self._serial_is_simulation():
            load_port = str(getattr(self._serial_info, "port", ""))
        elif str(self.loadcell_tab.source_mode_combo.currentData()) == "Physical":
            load_port = str(self.loadcell_tab.requested_connection().get("port") or "")
        if selected and load_port and selected.casefold() == load_port.casefold():
            self._printer_error(
                f"Printer port {selected} conflicts with the load-cell port. "
                "COM4 and COM3 must remain separate."
            )
            return
        self.printer_tab.append_log(
            {"level": "info", "message": f"Opening {selected} read-only first via M115; no motion or homing is automatic."}
        )
        self.printer_connect_command.emit(selected, int(baud_rate))

    def _printer_connected(self, info: object) -> None:
        values = dict(info) if isinstance(info, Mapping) else {}
        self._printer_connection_info = values
        self.printer_tab.set_connected(values)
        self.system_status_tab.append_status(
            "Printer",
            f"Verified Marlin on {values.get('port')} at {values.get('baud_rate')}; no motion was sent.",
        )

    def _printer_disconnected(self) -> None:
        self._printer_connection_info = None
        self._clear_printer_sequence_transaction(
            "Printer disconnected; synchronized sequence readiness was cleared."
        )
        self.motion_context.reset(status="idle")
        self.printer_tab.set_disconnected()

    def _printer_error(self, message: str) -> None:
        self.printer_tab.show_error(str(message))
        self.system_status_tab.append_status("Printer", str(message))
        if self._pending_printer_sequence is not None:
            was_recording = self.lifecycle is RecordingLifecycle.RECORDING
            self._clear_printer_sequence_transaction(
                f"Printer error before or during repeated pressing: {message}"
            )
            if was_recording:
                self._stop_recording()

    def _clear_printer_sequence_transaction(self, reason: str) -> None:
        self._pending_printer_sequence = None
        self._printer_sequence_preparation_stage = ""
        self._printer_automated_recording = False
        self.printer_prerequisites_command.emit(False, str(reason))

    def _confirm_and_home_printer(self) -> None:
        response = QMessageBox.warning(
            self,
            "Confirm Ender 3 homing",
            "Homing moves all axes. Confirm the build volume is clear, the press fixture cannot collide, "
            "the Ender 3 MAIN POWER/PSU switch is ON (USB alone cannot drive motors), and you are "
            "ready to send G28. Connection alone never homes the printer. If any axis fails to move, "
            "switch off printer power immediately.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        # PySide may return the integer value of StandardButton rather than
        # the exact Python enum singleton, so identity comparison can silently
        # drop an operator-approved Home request.
        if response == QMessageBox.StandardButton.Yes:
            self.printer_tab.update_operation(
                {"busy": True, "operation": "homing request queued", "error": ""}
            )
            self.printer_tab.append_log(
                {
                    "level": "info",
                    "message": "Operator confirmed Home All; queued G28 for the printer worker.",
                }
            )
            self.printer_home_command.emit()

    def _confirm_emergency_stop(self) -> None:
        # The large red button is itself the explicit action; do not insert a
        # confirmation dialog that delays a safety command.
        self.printer_emergency_command.emit()
        self._abort_active_recording(
            "Printer M112 emergency stop requested",
            ErrorCode.SHUTDOWN_REQUESTED,
        )

    def _printer_sequence_requested(self, values: object) -> None:
        if not isinstance(values, RepeatedPressConfig):
            self._printer_error("Repeated-press settings are invalid.")
            return
        if self._pending_printer_sequence is not None:
            self._printer_error("A repeated-press preparation or sequence is already active.")
            return
        if self.lifecycle in {RecordingLifecycle.COMPLETE, RecordingLifecycle.ERROR}:
            # A terminal prior recording is not an active transaction. Starting
            # repeated press owns a new synchronized recording, so normalize it
            # here instead of relying on an incidental trial-label edit signal.
            self._set_lifecycle(RecordingLifecycle.IDLE)
            self.recording_tab.set_status(
                "Preparing a new synchronized repeated-press trial."
            )
        elif self.lifecycle is RecordingLifecycle.RECORDING:
            self._printer_error(
                "A manual recording is already active. Stop it and wait for completion; "
                "then use Start Repeated Press without pressing Start Recording first."
            )
            return
        elif self.lifecycle is RecordingLifecycle.FINALIZING:
            self._printer_error(
                "The previous recording is still finalizing. Wait until completion, then "
                "use Start Repeated Press; it starts its own synchronized recording."
            )
            return
        elif self.lifecycle is not RecordingLifecycle.IDLE:
            self._printer_error(
                f"Recording state {self.lifecycle.value} cannot start repeated pressing."
            )
            return
        if not self._recording_prerequisites_ready(automated_sequence=True):
            self._printer_error(
                "Camera preview, baseline, quality-passed calibrated load cell, trial labels, "
                "output preflight, and disk readiness must all be valid before starting."
            )
            return
        if self._printer_connection_info is None:
            self._printer_error("Connect and verify the printer before starting.")
            return
        load_port = str(getattr(self._serial_info, "port", ""))
        printer_port = str(self._printer_connection_info.get("port", ""))
        if load_port and printer_port and load_port.casefold() == printer_port.casefold():
            self._printer_error("Printer and load-cell ports conflict; the sequence was blocked.")
            return
        try:
            preview = self.printer_worker.controller.preview(values)
        except Exception as exc:
            self._printer_error(str(exc))
            return
        response = QMessageBox.warning(
            self,
            "Confirm repeated printer motion",
            (
                f"Mechanical Press Zero X/Y/Z: {preview['press_zero_x_mm']:.3f}/"
                f"{preview['press_zero_y_mm']:.3f}/{preview['press_zero_z_mm']:.3f} mm\n"
                f"Target machine Z: {preview['target_machine_z_mm']:.3f} mm\n"
                f"Cycles: {values.cycles}\n"
                f"Estimated duration: {preview['estimated_duration_s']:.2f} s\n"
                f"Force limit: {values.force_limit_N:.3f} N\n\n"
                "Confirm the specimen, camera, load cell, fixture clearance, and negative-Z direction are correct."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        self._pending_printer_sequence = values
        self._printer_sequence_preparation_stage = ""
        self.printer_tab.set_sequence_preparation_status(
            "Preparing synchronized recording; printer motion has not started yet."
        )
        self.printer_tab.append_log(
            {
                "level": "info",
                "message": (
                    "Repeated press confirmed. Preparing recorder and pre-roll; "
                    "motion will be queued only after recording reports started."
                ),
            }
        )
        self._continue_printer_sequence_preparation()

    def _continue_printer_sequence_preparation(self) -> None:
        config = self._pending_printer_sequence
        if config is None:
            return
        if config.tare_before_sequence and self._printer_sequence_preparation_stage == "":
            self._printer_sequence_preparation_stage = "tare"
            self._start_sample_window("tare")
            if not self._sample_collector.active:
                self._printer_sequence_preparation_failed("Could not start the pre-sequence tare window.")
            else:
                self.printer_tab.append_log({"level": "info", "message": "Capturing the optional unloaded tare before recording."})
            return
        if config.fresh_baseline_before_sequence and self._printer_sequence_preparation_stage in {"", "tare_complete"}:
            self._printer_sequence_preparation_stage = "baseline"
            self._start_baseline_capture()
            if not self._baseline_locked:
                self._printer_sequence_preparation_failed("Could not start the fresh optical baseline capture.")
            else:
                self.printer_tab.append_log({"level": "info", "message": "Capturing the optional fresh unloaded optical baseline before recording."})
            return
        self._printer_sequence_preparation_stage = "recording"
        self._printer_automated_recording = True
        self._request_recording_internal(
            self.recording_tab.trial_labels(),
            automated_sequence=True,
        )
        if not self._preparing_recording:
            self._printer_sequence_preparation_failed("The synchronized recording transaction did not start.")

    def _printer_sequence_preparation_failed(self, message: str) -> None:
        self._clear_printer_sequence_transaction(message)
        self.printer_tab.set_sequence_preparation_status(message, error=True)
        self.printer_tab.show_error(message)

    def _printer_tare_bridge(self) -> None:
        raise RuntimeError("tare must be completed by the pre-recording GUI transaction")

    def _printer_baseline_bridge(self) -> None:
        raise RuntimeError("baseline must be completed by the pre-recording GUI transaction")

    def _printer_sequence_completed(self, result: object) -> None:
        if not isinstance(result, SequenceResult):
            self._printer_error("Repeated-press controller returned an invalid result.")
            return
        self.printer_tab.sequence_finished(result)
        directory = self._active_session_directory
        if directory is not None:
            try:
                atomic_write_json(directory / "printer_sequence.json", result.to_dict())
            except Exception as exc:
                self.printer_tab.show_error(f"Could not save printer_sequence.json: {exc}")
        self._clear_printer_sequence_transaction(
            f"Repeated-press sequence finished with status {result.status}."
        )
        if self.lifecycle is RecordingLifecycle.RECORDING:
            self._stop_recording()

    def _save_printer_profile(self, values: object) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save printer-motion profile",
            "printer_motion_profile.json",
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            atomic_write_json(Path(path), dict(values))
            self.printer_tab.append_log(
                {"level": "info", "message": f"Saved printer profile to {path}; active press zero was excluded."}
            )
        except Exception as exc:
            self._printer_error(str(exc))

    def _load_printer_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load printer-motion profile",
            "",
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                values = json.load(handle)
            if not isinstance(values, Mapping):
                raise ValueError("printer profile root must be a JSON object")
            if bool(values.get("press_zero_persisted", True)):
                raise ValueError("unsafe printer profile claims a persisted active press zero")
            self.printer_tab.apply_profile(values)
            self.printer_tab.append_log(
                {"level": "info", "message": f"Loaded printer profile from {path}; press zero and direction confirmation remain invalid."}
            )
        except Exception as exc:
            self._printer_error(str(exc))

    def _connect_camera(self, request: Mapping[str, object]) -> None:
        backend = str(request["backend"])
        other = "Media Foundation" if backend == "DirectShow" else "DirectShow"
        rotation_degrees, mirror_horizontal = self.camera_tab.requested_orientation()
        self._active_camera_config = replace(
            self.config.camera,
            device_index=int(request["device_index"]),
            backend_preference=(backend, other),
            rotation_degrees=rotation_degrees,
            mirror_horizontal=mirror_horizontal,
        )
        self.readiness.video_writer_preflight_passed = False
        self._invalidate_baseline("camera connection or mode changed", camera=True)
        mode = str(self.camera_tab.source_mode_combo.currentData())
        source_path = self.camera_tab.source_path_edit.text().strip()
        self.camera_tab.set_status("Connecting in the camera-owner thread…")
        self.camera_connect_command.emit(mode, self._active_camera_config, source_path)

    def _camera_orientation_changed(
        self,
        rotation_degrees: int,
        mirror_horizontal: bool,
    ) -> None:
        rotation = int(rotation_degrees)
        mirror = bool(mirror_horizontal)
        if (
            rotation == self._active_camera_config.rotation_degrees
            and mirror == self._active_camera_config.mirror_horizontal
        ):
            return
        self._active_camera_config = replace(
            self._active_camera_config,
            rotation_degrees=rotation,
            mirror_horizontal=mirror,
        )
        self.camera_orientation_command.emit(rotation, mirror)
        self.preview_buffer.reset()
        self._latest_captured = None
        self._latest_processed_bgr = None
        self._latest_motion_result = None
        self._camera_warmup_origin_ns = time.perf_counter_ns()
        self._invalidate_baseline("camera orientation changed", camera=True)
        self._stop_motion_processing()
        raw_width = int(
            getattr(
                self._camera_info,
                "actual_width",
                self._active_camera_config.requested_width,
            )
        )
        raw_height = int(
            getattr(
                self._camera_info,
                "actual_height",
                self._active_camera_config.requested_height,
            )
        )
        width, height = oriented_frame_size(raw_width, raw_height, rotation)
        self.roi_tab.set_frame_size(width, height)
        self.readiness.video_writer_preflight_passed = False
        if self._output_directory is not None and self._camera_info is not None:
            self._start_output_preflight()
        if self._motion_config.enabled and self.camera_tab.previewing:
            self._start_motion_processing()
        if self._camera_info is not None:
            self.camera_tab.set_connection_state(True, self._camera_info)
        self.camera_tab.set_status(
            f"Camera orientation applied: {self.camera_tab.orientation_description()}.",
            "success",
        )
        self.system_status_tab.append_status(
            "Camera",
            "Orientation changed; ROI geometry and optical baseline must be checked again.",
        )
        self._refresh_readiness()

    def _refresh_camera_driver_settings(self) -> None:
        if self._camera_info is None:
            self.camera_tab.set_driver_settings_status(
                "Connect a physical camera before reading driver settings.", "warning"
            )
            return
        if bool(getattr(self._camera_info, "simulation_mode", False)):
            self.camera_tab.set_driver_settings_status(
                "Simulation sources do not have Windows camera-driver settings.",
                "warning",
            )
            return
        self.camera_tab.set_driver_settings_status(
            "Reading current values from the active camera driver…"
        )
        self.camera_capabilities_command.emit()

    def _open_native_camera_properties(self) -> None:
        if self._camera_info is None:
            self.camera_tab.set_driver_settings_status(
                "Connect a physical camera before opening Windows camera properties.",
                "warning",
            )
            return
        backend = str(getattr(self._camera_info, "backend", ""))
        if backend != "DirectShow":
            self.camera_tab.set_driver_settings_status(
                "Reconnect with DirectShow to open the Windows camera-properties dialog.",
                "warning",
            )
            return
        self.camera_tab.set_driver_settings_status(
            "Windows camera properties are opening; preview may pause until the dialog closes."
        )
        self.camera_native_properties_command.emit()

    def _camera_capabilities_received(self, capabilities: object) -> None:
        rows = tuple(capabilities)
        serialized: list[dict[str, object]] = []
        current_controls: dict[str, float | bool] = {}
        readbacks: list[dict[str, object]] = []
        for item in rows:
            data = dict(item) if isinstance(item, Mapping) else asdict(item)
            serialized.append(data)
            name = str(data.get("property_name", ""))
            readable = bool(data.get("readable", False))
            current = data.get("current_value")
            if (
                name in SUPPORTED_CAMERA_PROPERTIES
                and readable
                and isinstance(current, (bool, int, float))
                and (isinstance(current, bool) or math.isfinite(float(current)))
            ):
                value: float | bool = (
                    current if isinstance(current, bool) else float(current)
                )
                current_controls[name] = value
                readbacks.append(
                    {
                        "property_name": name,
                        "requested_value": None,
                        "actual_value": value,
                        "write_succeeded": False,
                        "read_succeeded": True,
                        "confirmed": True,
                        "supported": bool(data.get("supported", True)),
                        "message": (
                            "Current value read from the active camera driver; "
                            "no automatic GUI write was made."
                        ),
                    }
                )
        self._camera_capabilities = serialized
        self._camera_controls = current_controls
        self._camera_property_readbacks = readbacks
        enabled_automatic = [
            name
            for name in ("auto_exposure", "auto_white_balance", "auto_focus")
            if current_controls.get(name) is True
        ]
        self._automatic_control_warning = (
            "Automatic camera controls are enabled ("
            + ", ".join(enabled_automatic)
            + "); optical response may change during calibration."
            if enabled_automatic
            else ""
        )
        self.camera_tab.update_capabilities(rows)
        self.system_status_tab.append_status(
            "Camera",
            (
                f"Read {len(current_controls)} current driver setting(s)."
                if current_controls
                else "The selected camera backend exposed no readable driver settings."
            ),
        )

    def _native_camera_properties_result(self, success: bool, message: str) -> None:
        if not success:
            self.camera_tab.set_driver_settings_status(message, "error")
            self.system_status_tab.append_status("Camera", message)
            return
        self.preview_buffer.reset()
        self._latest_captured = None
        self._latest_processed_bgr = None
        self._latest_motion_result = None
        self._camera_warmup_origin_ns = time.perf_counter_ns()
        self._invalidate_baseline("native camera properties may have changed", camera=True)
        self._stop_motion_processing()
        if self._motion_config.enabled and self.camera_tab.previewing:
            self._start_motion_processing()
        self.camera_tab.set_driver_settings_status(
            "Windows camera properties closed; reading the values now used by the GUI…",
            "success",
        )
        self.system_status_tab.append_status(
            "Camera",
            "Native driver settings may have changed; the optical baseline was invalidated.",
        )
        self.camera_capabilities_command.emit()
        self._refresh_readiness()

    def _camera_connected(self, info: object) -> None:
        self._camera_info = info
        self._camera_requested_controls.clear()
        self._camera_controls.clear()
        self._camera_property_readbacks.clear()
        self._camera_capabilities.clear()
        self._camera_connected_at_ns = time.perf_counter_ns()
        self._camera_warmup_origin_ns = self._camera_connected_at_ns
        self._measured_capture_fps = float("nan")
        self.readiness.camera_connected = True
        self._camera_mode_confirmed = (
            int(getattr(info, "actual_width", 0)) > 0
            and int(getattr(info, "actual_height", 0)) > 0
            and float(getattr(info, "actual_fps", 0.0)) > 0
        )
        simulation = bool(getattr(info, "simulation_mode", False))
        message = (
            "SIMULATION camera connected; no physical Arducam camera is claimed."
            if simulation
            else "Camera connected; reading its current driver settings."
        )
        self.camera_tab.set_connection_state(True, info, message=message)
        width, height = oriented_frame_size(
            int(getattr(info, "actual_width")),
            int(getattr(info, "actual_height")),
            self._active_camera_config.rotation_degrees,
        )
        self.roi_tab.set_frame_size(width, height)
        self.recording_tab.set_simulation_mode(simulation or self._serial_is_simulation())
        if not simulation:
            self._refresh_camera_driver_settings()
        if self._output_directory is not None:
            self._start_output_preflight()
        self._refresh_readiness()

    def _camera_disconnected(self) -> None:
        had_connection = self.readiness.camera_connected
        self._camera_info = None
        self._measured_capture_fps = float("nan")
        self.readiness.camera_connected = False
        self._camera_mode_confirmed = False
        self._automatic_control_warning = ""
        self._camera_requested_controls.clear()
        self._camera_controls.clear()
        self._camera_property_readbacks.clear()
        self._camera_capabilities.clear()
        self.camera_tab.update_property_diagnostics(())
        self.camera_tab.update_capabilities(())
        self.camera_tab.set_connection_state(False, message="Camera disconnected.")
        self.camera_tab.set_preview_state(False)
        self._cancel_recording_preparation("camera disconnected during preparation")
        self._stop_live_analysis()
        self._stop_motion_processing()
        if had_connection:
            self._invalidate_baseline("camera disconnected", camera=True)
        self._abort_active_recording(
            "Camera disconnected during recording",
            ErrorCode.CAMERA_DISCONNECTED,
        )
        self._refresh_readiness()

    def _start_preview(self) -> None:
        self.preview_buffer.reset()
        self._latest_captured = None
        self._preview_display_count = 0
        self._preview_started_ns = time.perf_counter_ns()
        self._measured_capture_fps = float("nan")
        initial_metrics = {
            "preview_fps": "0.00",
            "dropped_preview_frames": 0,
            "capture_to_display_ms": "—",
        }
        self.camera_tab.update_performance(initial_metrics)
        self.recording_tab.update_performance_metrics(initial_metrics)
        self.camera_start_command.emit()
        self.camera_tab.set_preview_state(True)
        if self._motion_config.enabled:
            self._start_motion_processing()

    def _stop_preview(self) -> None:
        self._cancel_baseline_capture("preview stopped")
        self._cancel_recording_preparation("preview stopped during preparation")
        self.camera_stop_command.emit()
        self.camera_tab.set_preview_state(False)
        self._stop_motion_processing()

    def _await_authoritative_camera_warmup(
        self, purpose: str, workflow_generation: int, deadline_ns: int
    ) -> None:
        """Advance only after a live frame proves the post-connect warm-up elapsed."""

        if purpose == "baseline" and workflow_generation != self._baseline_generation:
            return
        if (
            purpose == "recording"
            and workflow_generation != self._recording_prepare_generation
        ):
            return
        if not self.readiness.camera_connected or not self.camera_tab.previewing:
            if purpose == "baseline":
                self._baseline_capture_failed(
                    workflow_generation, "Camera preview stopped during baseline preparation."
                )
            else:
                self._recording_prepare_failed(
                    "Camera preview stopped during recording preparation.",
                    workflow_generation,
                )
            return
        if time.perf_counter_ns() >= int(deadline_ns):
            message = (
                "No captured frame reached the post-connect warm-up boundary before "
                "the bounded camera prerequisite timeout."
            )
            if purpose == "baseline":
                self._baseline_capture_failed(workflow_generation, message)
            else:
                self._recording_prepare_failed(message, workflow_generation)
            return
        latest_ns = int(
            getattr(self._latest_captured, "host_monotonic_ns", 0)
            if self._latest_captured is not None
            else 0
        )
        ready_ns = self._camera_warmup_origin_ns + round(
            self._active_camera_config.warmup_seconds * 1_000_000_000
        )
        if latest_ns < ready_ns:
            remaining = max(0.0, (ready_ns - latest_ns) / 1_000_000_000.0)
            self.camera_tab.set_next_action(
                f"Live camera warm-up: {remaining:.1f} s remaining."
            )
            QTimer.singleShot(
                25,
                lambda: self._await_authoritative_camera_warmup(
                    purpose, workflow_generation, deadline_ns
                ),
            )
            return
        if purpose == "baseline":
            self._begin_baseline_frame_collection(workflow_generation)
        else:
            self._begin_recording_drift_check(workflow_generation)

    def _camera_mode_confirmation(self, confirmed: bool, rows: object) -> None:
        self._camera_mode_confirmed = bool(confirmed)
        mode_rows = tuple(rows)
        self._camera_mode_readbacks = [asdict(item) for item in mode_rows]
        if not confirmed:
            self.camera_tab.set_status(
                "The driver did not confirm its reported stream mode; frames remain authoritative.",
                "warning",
            )
        self._refresh_readiness()

    def _camera_metrics(self, metrics: Mapping[str, object]) -> None:
        self.camera_tab.update_performance(metrics)
        mapped = dict(metrics)
        if "capture_fps" in mapped:
            capture_fps = float(mapped["capture_fps"])
            if math.isfinite(capture_fps) and capture_fps > 0.0:
                self._measured_capture_fps = capture_fps
            mapped["camera_capture_fps"] = f"{capture_fps:.2f}"
        if (
            "dropped_preview_frames" in mapped
            and (
                self._preparing_recording
                or self.lifecycle
                in {RecordingLifecycle.RECORDING, RecordingLifecycle.FINALIZING}
            )
        ):
            mapped["dropped_preview_frames"] = max(
                0,
                int(mapped["dropped_preview_frames"])
                - self._recording_preview_drop_baseline,
            )
        self.recording_tab.update_performance_metrics(mapped)

    def _effective_camera_fps(self) -> float:
        """Return measured capture FPS, then driver FPS, then configured fallback."""

        if math.isfinite(self._measured_capture_fps) and self._measured_capture_fps > 0.0:
            return float(self._measured_capture_fps)
        reported_fps = float(getattr(self._camera_info, "actual_fps", float("nan")))
        if math.isfinite(reported_fps) and reported_fps > 0.0:
            return reported_fps
        return float(self._active_camera_config.requested_fps)

    def _refresh_preview(self) -> None:
        captured = self.preview_buffer.take_latest()
        if captured is None:
            return
        self._latest_captured = captured
        frame = np.asarray(getattr(captured, "original_bgr"))
        height, width = frame.shape[:2]
        image = QImage(
            frame.data,
            width,
            height,
            int(frame.strides[0]),
            QImage.Format.Format_BGR888,
        ).copy()
        self.camera_tab.set_preview_image(image)
        display_original = frame
        original_qimage = QImage(
            display_original.data,
            width,
            height,
            int(display_original.strides[0]),
            QImage.Format.Format_BGR888,
        ).copy()
        self.video_display.original_label.set_image(original_qimage)
        self.video_display.original_status.setText(
            f"Unannotated camera frame {getattr(captured, 'source_frame_id', '—')} | "
            f"{self.camera_tab.orientation_description()}"
        )
        motion_update = self.motion_result_buffer.take_latest()
        motion_dropped = None
        if isinstance(motion_update, Mapping):
            self._latest_motion_result = motion_update.get("result")
            motion_dropped = int(motion_update.get("dropped_frames", 0))
        motion_color = getattr(self._latest_motion_result, "color_bgr", None)
        motion_intensity = getattr(self._latest_motion_result, "intensity_bgr", None)
        mode = self.video_display.processed_mode_combo.currentText()
        self._latest_processed_bgr = build_processed_preview(
            frame,
            mode,
            baseline=self._baseline,
            rois=self.roi_tab.rois,
            optical_row=self._latest_optical_row,
            motion_color_bgr=motion_color,
            motion_intensity_bgr=motion_intensity,
        )
        processed = self._latest_processed_bgr
        processed_qimage = QImage(
            processed.data,
            processed.shape[1],
            processed.shape[0],
            int(processed.strides[0]),
            QImage.Format.Format_BGR888,
        ).copy()
        self.video_display.processed_label.set_image(processed_qimage)
        motion_frame_id = getattr(self._latest_motion_result, "source_frame_id", "—")
        self.video_display.processed_status.setText(
            f"{mode} | source frame "
            f"{motion_frame_id if mode.startswith('Motion-') else getattr(captured, 'source_frame_id', '—')}"
        )
        # Pyqtgraph interprets the final axis as RGB.  This display-only view is
        # reversed without mutating or annotating the authoritative BGR frame.
        self.roi_tab.set_preview_image(np.ascontiguousarray(frame[..., ::-1]))
        now = time.perf_counter_ns()
        self._preview_display_count += 1
        elapsed = max((now - self._preview_started_ns) / 1e9, 1e-9)
        latency_ms = max(0.0, (now - int(getattr(captured, "host_monotonic_ns"))) / 1e6)
        metrics = {
            "preview_fps": f"{self._preview_display_count / elapsed:.2f}",
            "dropped_preview_frames": self.preview_buffer.dropped_count,
            "capture_to_display_ms": f"{latency_ms:.2f}",
        }
        self.camera_tab.update_performance(metrics)
        recording_metrics = dict(metrics)
        if self._preparing_recording or self.lifecycle in {
            RecordingLifecycle.RECORDING,
            RecordingLifecycle.FINALIZING,
        }:
            recording_metrics["dropped_preview_frames"] = max(
                0,
                self.preview_buffer.dropped_count
                - self._recording_preview_drop_baseline,
            )
        self.recording_tab.update_performance_metrics(recording_metrics)
        if self._latest_motion_result is not None:
            motion_metrics = {
                "motion_magnification_fps": f"{float(getattr(self._latest_motion_result, 'measured_fps', 0.0)):.2f}",
                "motion_latency_ms": f"{float(getattr(self._latest_motion_result, 'processing_latency_ms', 0.0)):.2f}",
                "motion_queue_depth": (
                    0 if self._motion_thread is None else self._motion_thread.queue_depth
                ),
                "dropped_processing_frames": (
                    self._motion_thread.dropped_frames
                    if motion_dropped is None and self._motion_thread is not None
                    else (motion_dropped or 0)
                ),
            }
            motion_metrics["processing_warning"] = (
                "Reduce pyramid depth/downscale/target FPS: stale motion frames are being dropped."
                if int(motion_metrics["dropped_processing_frames"]) > 0
                else "None"
            )
            self.system_status_tab.update_performance({**metrics, **motion_metrics})
        self._last_preview_refresh_ns = now
        self._collect_baseline_frame(captured)

    def _baseline_context_for_current_layout(self) -> BaselineContext:
        if self._camera_info is None:
            raise ValueError("connect a camera before capturing a baseline")
        layout = self.roi_tab.current_layout()
        return BaselineContext(
            camera_device=str(getattr(self._camera_info, "source_label", "camera")),
            backend=str(getattr(self._camera_info, "backend", "unknown")),
            frame_width=layout.frame_width,
            frame_height=layout.frame_height,
            camera_settings={
                "property_control": (
                    "current driver values read for provenance; changes are "
                    "operator-initiated through the DirectShow native dialog"
                ),
                "current_driver_values": copy.deepcopy(self._camera_controls),
                "frame_source": "unannotated camera image after configured orientation",
                "rotation_degrees_clockwise": self._active_camera_config.rotation_degrees,
                "mirror_horizontal": self._active_camera_config.mirror_horizontal,
            },
            roi_layout_id=layout.roi_layout_id,
            processing_settings=_processing_settings(self.config),
        )

    def _start_baseline_capture(self) -> None:
        if self._preparing_recording or self.lifecycle in {
            RecordingLifecycle.RECORDING,
            RecordingLifecycle.FINALIZING,
        }:
            self.roi_tab.set_baseline_result_available(
                False,
                "Finish or cancel the recording transaction before capturing a baseline.",
            )
            return
        if not self.readiness.camera_connected:
            self.roi_tab.set_baseline_result_available(False, "Connect and preview a camera first.")
            return
        if not self.camera_tab.previewing:
            self.roi_tab.set_baseline_result_available(False, "Start the camera preview first.")
            return
        if not self.roi_tab.validation.valid:
            self.roi_tab.set_baseline_result_available(False, "Define nine valid ROIs first.")
            return
        self._cancel_baseline_capture("")
        self._baseline_generation += 1
        generation = self._baseline_generation
        self._pending_baseline = None
        self._baseline_capture_frames = []
        self._baseline_capture_first_ns = None
        self._baseline_capture_last_source_id = None
        self._baseline_locked = True
        self._update_configuration_locks()
        self.roi_tab.set_baseline_result_available(
            False,
            "Waiting for the live camera warm-up, then capturing unloaded frames…",
        )
        timeout_s = max(2.0, self._active_camera_config.warmup_seconds + 2.0)
        self._await_authoritative_camera_warmup(
            "baseline",
            generation,
            time.perf_counter_ns() + round(timeout_s * 1_000_000_000),
        )

    def _begin_baseline_frame_collection(self, generation: int) -> None:
        if generation != self._baseline_generation:
            return
        self._baseline_capture_frames = []
        self._baseline_capture_first_ns = None
        self._baseline_capture_last_source_id = None
        self.roi_tab.set_baseline_capture_active(True)

    def _collect_baseline_frame(self, captured: object) -> None:
        if not getattr(self.roi_tab, "_baseline_capture_active", False):
            return
        source_id = int(getattr(captured, "source_frame_id"))
        if source_id == self._baseline_capture_last_source_id:
            return
        timestamp_ns = int(getattr(captured, "host_monotonic_ns"))
        warmup_ready_ns = self._camera_warmup_origin_ns + round(
            self._active_camera_config.warmup_seconds * 1_000_000_000
        )
        if timestamp_ns < warmup_ready_ns:
            return
        if self._baseline_capture_first_ns is None:
            self._baseline_capture_first_ns = timestamp_ns
        self._baseline_capture_last_source_id = source_id
        self._baseline_capture_frames.append(np.asarray(getattr(captured, "original_bgr")))
        elapsed_s = (timestamp_ns - self._baseline_capture_first_ns) / 1e9
        duration = self.config.processing.baseline_duration_s
        percent = min(100.0, elapsed_s / duration * 100.0)
        self.roi_tab.set_baseline_capture_progress(percent, len(self._baseline_capture_frames))
        if (
            elapsed_s < duration
            or len(self._baseline_capture_frames) < self.config.processing.minimum_baseline_frames
        ):
            return
        self.roi_tab.set_baseline_capture_active(False)
        try:
            context = self._baseline_context_for_current_layout()
            rois = self.roi_tab.rois
        except Exception as exc:
            self._baseline_capture_failed(self._baseline_generation, str(exc))
            return
        frames = tuple(self._baseline_capture_frames)
        self._baseline_capture_frames.clear()
        generation = self._baseline_generation
        self._run_job(
            capture_baseline,
            lambda baseline: self._baseline_calculated(generation, baseline, context),
            lambda text: self._baseline_capture_failed(generation, text),
            frames,
            rois,
            context=context,
            minimum_saturation_for_h=self.config.processing.minimum_saturation_for_h,
            minimum_valid_frames=self.config.processing.minimum_baseline_frames,
        )

    def _baseline_calculated(
        self, generation: int, baseline: object, capture_context: BaselineContext
    ) -> None:
        if generation != self._baseline_generation:
            return
        self._baseline_capture_frames.clear()
        try:
            current_context = self._baseline_context_for_current_layout()
            if not validate_baseline_context(baseline, current_context):
                raise ValueError(
                    "Camera, processing, or ROI context changed while calculating the baseline."
                )
            if current_context != capture_context:
                raise ValueError("Baseline context changed before the result was accepted.")
        except Exception as exc:
            self._baseline_capture_failed(generation, str(exc))
            return
        self._pending_baseline = baseline
        self._baseline_context = capture_context
        self._baseline_locked = False
        self._update_configuration_locks()
        self.roi_tab.set_baseline_result_available(
            True, "Baseline calculated. Inspect the unloaded preview, then Accept or Repeat."
        )
        if (
            self._pending_printer_sequence is not None
            and self._printer_sequence_preparation_stage == "baseline"
        ):
            self._accept_baseline()
            if self.readiness.baseline_valid:
                self._printer_sequence_preparation_stage = "baseline_complete"
                self._continue_printer_sequence_preparation()
            else:
                self._printer_sequence_preparation_failed(
                    "The fresh optical baseline could not be accepted."
                )

    def _baseline_capture_failed(self, generation: int, message: str) -> None:
        if generation != self._baseline_generation:
            return
        self._baseline_capture_frames.clear()
        self._baseline_capture_first_ns = None
        self._baseline_capture_last_source_id = None
        self._baseline_locked = False
        self._update_configuration_locks()
        self.roi_tab.set_baseline_result_available(False, message)
        if self._printer_sequence_preparation_stage == "baseline":
            self._printer_sequence_preparation_failed(
                f"Fresh pre-sequence baseline failed: {message}"
            )

    def _cancel_baseline_capture(self, reason: str) -> None:
        was_active = self._baseline_locked or bool(
            getattr(self.roi_tab, "_baseline_capture_active", False)
        )
        self._baseline_generation += 1
        self._baseline_capture_frames.clear()
        self._baseline_capture_first_ns = None
        self._baseline_capture_last_source_id = None
        self._baseline_locked = False
        self.roi_tab.set_baseline_capture_active(False)
        self._update_configuration_locks()
        if was_active and reason:
            self.roi_tab.set_baseline_result_available(False, reason)

    def _accept_baseline(self) -> None:
        if self._pending_baseline is None:
            self.roi_tab.set_baseline_status(False, "no calculated baseline is available")
            return
        try:
            current_context = self._baseline_context_for_current_layout()
            if not validate_baseline_context(self._pending_baseline, current_context):
                raise ValueError("Baseline context no longer matches the current setup.")
        except Exception as exc:
            self._pending_baseline = None
            self.roi_tab.set_baseline_status(False, str(exc))
            self._refresh_readiness()
            return
        self._baseline = self._pending_baseline
        self._pending_baseline = None
        self._baseline_context = current_context
        self.readiness.baseline_valid = bool(getattr(self._baseline, "valid", False))
        self.roi_tab.set_baseline_status(
            self.readiness.baseline_valid,
            captured_at=str(getattr(self._baseline, "capture_timestamp_iso", "")),
        )
        self.live_sensor_tab.set_baseline_ready(
            self.readiness.baseline_valid
            and self._quantitative_analysis_source != "Motion-magnified frame",
            str(getattr(self._baseline, "baseline_id", "")),
            current_context.roi_layout_id,
            self._active_camera_config.rotation_degrees,
            self._active_camera_config.mirror_horizontal,
        )
        self._start_live_analysis()
        if (
            self._quantitative_analysis_source == "Motion-magnified frame"
            and self._motion_config.enabled
            and self.camera_tab.previewing
        ):
            # Rebuild the motion worker so magnified HSV extraction receives
            # the newly accepted unloaded baseline.
            self._stop_motion_processing()
            self._start_motion_processing()
        self._refresh_readiness()

    def _discard_pending_baseline(self) -> None:
        self._pending_baseline = None
        self._baseline_capture_frames.clear()

    def _roi_changed(self, _rois: object) -> None:
        self._invalidate_baseline("ROI layout changed", camera=False)

    def _rois_valid_changed(self, valid: bool) -> None:
        self.readiness.rois_valid = bool(valid)
        self._refresh_readiness()

    def _invalidate_baseline(self, reason: str, *, camera: bool) -> None:
        self._cancel_recording_preparation(reason)
        self._cancel_baseline_capture(reason)
        self._pending_baseline = None
        if self._baseline is not None and bool(getattr(self._baseline, "valid", False)):
            if camera:
                invalidate_for_camera_change(self._baseline)
            else:
                invalidate_for_roi_change(self._baseline)
        self.readiness.baseline_valid = False
        self.roi_tab.set_baseline_status(False, reason)
        self.live_sensor_tab.set_baseline_ready(False, "")
        self._stop_live_analysis()
        self._refresh_readiness()

    def _start_live_analysis(self) -> None:
        self._stop_live_analysis()
        self.live_optical_buffer.clear()
        if self._baseline is None or not bool(getattr(self._baseline, "valid", False)):
            return
        self._live_analysis_generation += 1
        generation = self._live_analysis_generation
        if self._quantitative_analysis_source == "Motion-magnified frame":
            # The motion worker publishes HSV features from its own magnified
            # pixels, preserving exact frame identity and avoiding duplicate
            # analysis of the original camera frame.
            return
        target = self.recording_tab.target_roi_combo.currentIndex() + 1
        worker = LiveOpticalThread(
            processing_config=self.config.processing,
            rois=self.roi_tab.rois,
            baseline=self._baseline,
            result_buffer=self.live_optical_buffer,
            generation=generation,
            target_roi_ground_truth=target,
            parent=self,
        )
        worker.failed.connect(lambda text: self.roi_tab.status_label.setText(f"Live analysis warning: {text}"))
        worker.finished.connect(
            lambda current=worker: self._live_analysis_finished(current)
        )
        self._live_analysis_thread = worker
        self._update_camera_analysis_sink()
        worker.start()

    def _live_analysis_finished(self, worker: LiveOpticalThread) -> None:
        self._retiring_live_analysis_threads.discard(worker)
        if self._live_analysis_thread is worker:
            self._live_analysis_thread = None
        self._update_camera_analysis_sink()
        worker.deleteLater()

    def _stop_live_analysis(self, wait_ms: int = 0) -> bool:
        worker = self._live_analysis_thread
        if worker is not None:
            self._live_analysis_generation += 1
            self.live_optical_buffer.clear()
            self._live_analysis_thread = None
            self._retiring_live_analysis_threads.add(worker)
            worker.request_stop()
        self._update_camera_analysis_sink()
        if wait_ms <= 0:
            return True
        deadline_ns = time.perf_counter_ns() + int(wait_ms) * 1_000_000
        for retiring in tuple(self._retiring_live_analysis_threads):
            remaining_ms = max(
                0, (deadline_ns - time.perf_counter_ns()) // 1_000_000
            )
            if retiring.isRunning() and not retiring.wait(int(remaining_ms)):
                return False
        return True

    def _submit_analysis_frame(self, frame: object) -> bool:
        live = self._live_analysis_thread
        motion = self._motion_thread
        if live is not None:
            live.submit_frame(frame)  # type: ignore[arg-type]
        if motion is not None:
            motion.submit_frame(frame)  # type: ignore[arg-type]
        return True

    def _update_camera_analysis_sink(self) -> None:
        active = self._live_analysis_thread is not None or self._motion_thread is not None
        self.camera_worker.set_analysis_sink(
            self._submit_analysis_frame if active else None
        )

    def _start_motion_processing(self) -> None:
        if not self._motion_config.enabled or self._motion_thread is not None:
            return
        self.motion_result_buffer.clear()
        worker = MotionMagnificationThread(
            config=self._motion_config,
            result_buffer=self.motion_result_buffer,
            rois=self.roi_tab.rois,
            processing_config=(
                self.config.processing
                if self._quantitative_analysis_source == "Motion-magnified frame"
                else None
            ),
            baseline=(
                self._baseline
                if self._quantitative_analysis_source == "Motion-magnified frame"
                else None
            ),
            analysis_result_buffer=(
                self.live_optical_buffer
                if self._quantitative_analysis_source == "Motion-magnified frame"
                and self._baseline is not None
                and bool(getattr(self._baseline, "valid", False))
                else None
            ),
            analysis_generation=self._live_analysis_generation,
            target_roi_ground_truth=(
                self.recording_tab.target_roi_combo.currentIndex() + 1
            ),
            parent=self,
        )
        worker.failed.connect(
            lambda text: self.motion_tab.set_status(text, error=True)
        )
        worker.finished.connect(
            lambda current=worker: self._motion_processing_finished(current)
        )
        self._motion_thread = worker
        self._update_camera_analysis_sink()
        worker.start()

    def _motion_processing_finished(self, worker: MotionMagnificationThread) -> None:
        self._retiring_motion_threads.discard(worker)
        if self._motion_thread is worker:
            self._motion_thread = None
        self._update_camera_analysis_sink()
        worker.deleteLater()

    def _stop_motion_processing(self, wait_ms: int = 0) -> bool:
        worker = self._motion_thread
        if worker is not None:
            self._motion_thread = None
            self._retiring_motion_threads.add(worker)
            worker.request_stop()
        self._update_camera_analysis_sink()
        if wait_ms <= 0:
            return True
        deadline_ns = time.perf_counter_ns() + int(wait_ms) * 1_000_000
        for retiring in tuple(self._retiring_motion_threads):
            remaining_ms = max(0, (deadline_ns - time.perf_counter_ns()) // 1_000_000)
            if retiring.isRunning() and not retiring.wait(int(remaining_ms)):
                return False
        return True

    def _apply_motion_config(self, config: MotionMagnificationConfig) -> None:
        try:
            # Some UVC drivers report zero FPS even while frames are arriving.
            # Measured capture wins; configured camera FPS is the startup
            # fallback and timestamp-aware validation continues in the worker.
            source_fps = self._effective_camera_fps()
            validation_fps = min(source_fps, config.target_fps)
            if config.enabled:
                config.validate_for_fps(validation_fps)
        except Exception as exc:
            self.motion_tab.set_status(str(exc), error=True)
            return
        self._stop_motion_processing()
        self._motion_config = config
        self._latest_motion_result = None
        auto_selected_magnified_hsv = False
        if (
            config.enabled
            and config.mode == "Color magnification"
            and self.image_processing_tab.analysis_source_combo.currentText()
            != "Motion-magnified frame"
        ):
            auto_selected_magnified_hsv = True
            self.image_processing_tab.analysis_source_combo.setCurrentText(
                "Motion-magnified frame"
            )
        elif (
            not config.enabled
            and self.image_processing_tab.analysis_source_combo.currentText()
            == "Motion-magnified frame"
        ):
            self.image_processing_tab.analysis_source_combo.setCurrentText(
                "Background-subtracted frame"
            )
        elif (
            config.enabled
            and self._quantitative_analysis_source == "Motion-magnified frame"
        ):
            # A parameter change creates a new temporal filter and therefore a
            # new live-analysis generation. Late rows from the retiring worker
            # cannot enter the new graph history.
            self._analysis_source_changed("Motion-magnified frame")
        if config.enabled and self.camera_tab.previewing:
            self._start_motion_processing()
        self.motion_tab.set_status(
            (
                "Color magnification enabled; magnified-frame HSV V now feeds "
                "the force-light graphs and recording. Temporal filter reset."
                if config.enabled and config.mode == "Color magnification"
                else "Motion magnification enabled; temporal filter reset."
            )
            if config.enabled
            else "Motion magnification disabled."
        )
        if auto_selected_magnified_hsv:
            self.system_status_tab.append_status(
                "Analysis",
                "Color magnification automatically selected the motion-magnified "
                "frame as the force-light HSV source.",
            )

    def _reset_motion_filter(self) -> None:
        if self._motion_thread is not None:
            self._motion_thread.request_reset()
            self.motion_tab.set_status("Temporal filter reset requested.")
        else:
            self._latest_motion_result = None
            self.motion_tab.set_status("Temporal filter state is clear.")

    def _analysis_source_changed(self, source: str) -> None:
        self._quantitative_analysis_source = str(source)
        self._latest_optical_row = None
        self.live_sensor_tab.set_baseline_ready(
            self.readiness.baseline_valid and source != "Motion-magnified frame",
            (
                str(getattr(self._baseline, "baseline_id", ""))
                if self._baseline is not None
                else ""
            ),
            (
                self._baseline_context.roi_layout_id
                if self._baseline_context is not None
                else ""
            ),
            self._active_camera_config.rotation_degrees,
            self._active_camera_config.mirror_horizontal,
        )
        for panel in (self.roi_tab.graph_panel, self.recording_tab.graph_panel):
            panel.clear_temporal_history()
        if source == "Motion-magnified frame":
            self.video_display.processed_mode_combo.setCurrentText(
                (
                    "Motion-magnified intensity preview"
                    if self._motion_config.mode == "Intensity-only magnification"
                    else "Motion-magnified color preview"
                )
            )
            for panel in (self.roi_tab.graph_panel, self.recording_tab.graph_panel):
                panel.metric_selector.setCurrentText("Mean HSV V (0-255)")
                panel.view_selector.setCurrentText("Temporal ROI Lines")
                panel.set_data_source("motion-magnified color frame")
            self.system_status_tab.append_status(
                "Analysis",
                "Magnified color pixels are selected for force-light HSV analysis. "
                "Use a calibration created with this same source.",
            )
        else:
            for panel in (self.roi_tab.graph_panel, self.recording_tab.graph_panel):
                panel.set_data_source("original camera frame")
        self._stop_live_analysis()
        self._stop_motion_processing()
        if self.camera_tab.previewing:
            self._start_live_analysis()
            self._start_motion_processing()

    def _optical_result(self, row: Mapping[str, object]) -> None:
        self.live_optical_buffer.publish(dict(row))

    def _refresh_optical_graphs(self) -> None:
        # Recording updates take priority and live-analysis rows have a
        # physically separate slot, so a retiring live worker cannot overwrite
        # cumulative trial counters.
        update = self.recording_display_buffer.take_latest()
        if update is None:
            update = self.live_optical_buffer.take_latest()
        if update is None:
            return
        row = dict(update)
        if "feature_row" in row:
            recording_row = self._apply_recording_display_update(row)
            if recording_row is None:
                return
            row = recording_row
        elif int(row.get("analysis_generation", self._live_analysis_generation)) != (
            self._live_analysis_generation
        ):
            return
        self._latest_optical_row = row
        if "feature_row" in dict(update):
            hsv_source = (
                "motion-magnified color frame"
                if self._quantitative_analysis_source == "Motion-magnified frame"
                else "original camera frame"
            )
        else:
            hsv_source = str(row.get("hsv_frame_source", "original camera frame"))
        if self._quantitative_analysis_source != "Motion-magnified frame":
            self.live_sensor_tab.process_live_row(row)
        for panel in (self.roi_tab.graph_panel, self.recording_tab.graph_panel):
            panel.set_data_source(hsv_source)
            suffix = _METRIC_COLUMNS.get(panel.metric_selector.currentText(), "delta_v_mean")
            values = tuple(float(row.get(f"roi{roi}_{suffix}", math.nan)) for roi in range(1, 10))
            frame_id = int(row.get("capture_frame_id", 0))
            elapsed_s = float(row.get("elapsed_time_s", 0.0))
            panel.set_current_values(
                values,
                capture_frame_id=frame_id,
                elapsed_time_s=elapsed_s,
                host_monotonic_ns=(
                    int(row["host_monotonic_ns"])
                    if row.get("host_monotonic_ns") is not None
                    else None
                ),
            )
            try:
                panel.append_temporal_values(frame_id, elapsed_s, values)
            except ValueError:
                panel.clear_temporal_history()
                panel.append_temporal_values(frame_id, elapsed_s, values)

    def _apply_recording_display_update(
        self, update: Mapping[str, object]
    ) -> dict[str, object] | None:
        """Consume cumulative recording counters from one newest-only update."""

        feature_row = update.get("feature_row")
        if not isinstance(feature_row, Mapping):
            return None
        self._recording_frame_count = int(
            update.get("processed_frame_count", self._recording_frame_count)
        )
        self._recording_error_count = max(
            self._recording_error_count,
            int(update.get("pipeline_error_count", 0)),
        )
        processing_ns = max(1, int(update.get("feature_processing_ns", 0)))
        writing_ns = max(1, int(update.get("file_writing_ns", 0)))
        feature_rate = self._recording_frame_count * 1_000_000_000.0 / processing_ns
        writing_rate = self._recording_frame_count * 1_000_000_000.0 / writing_ns
        performance = {
            "feature_processing_fps": f"{feature_rate:.2f}",
            "video_writing_fps": f"{writing_rate:.2f}",
            "recording_pipeline_errors": self._recording_error_count,
        }
        self.camera_tab.update_performance(performance)
        self.recording_tab.update_performance_metrics(performance)
        return dict(feature_row)

    def _drain_terminal_recording_update(self) -> None:
        """Capture the worker's final cumulative counters before live restart clears them."""

        update = self.recording_display_buffer.take_latest()
        if isinstance(update, Mapping) and "feature_row" in update:
            self._apply_recording_display_update(update)

    def _serial_identity_snapshot(self) -> tuple[str, ...] | None:
        """Return immutable identity evidence for a load-cell workflow transaction."""

        info = self._serial_info
        if info is None:
            return None
        return (
            str(getattr(info, "port", "")),
            str(getattr(info, "device_session_id", "")),
            str(getattr(info, "device_name", "")),
            str(getattr(info, "protocol_version", "")),
            str(getattr(info, "firmware_version", "")),
            "true" if bool(getattr(info, "simulation_mode", False)) else "false",
        )

    @staticmethod
    def _serial_identity_mapping(identity: tuple[str, ...]) -> dict[str, str]:
        return {
            "port": identity[0],
            "device_session_id": identity[1],
            "device_name": identity[2],
            "protocol_version": identity[3],
            "firmware_version": identity[4],
            "simulation_mode": identity[5],
            "firmware_identity": f"{identity[2]}/{identity[4]}",
        }

    def _loadcell_workflow_busy(self) -> bool:
        return self._sample_collector.active or self._loadcell_pending_job is not None

    def _advance_loadcell_workflow_generation(self) -> int:
        """Invalidate every delayed callback belonging to the prior workflow."""

        self._loadcell_workflow_generation += 1
        self._loadcell_pending_job = None
        self.loadcell_tab.set_workflow_job_active(False)
        return self._loadcell_workflow_generation

    def _begin_loadcell_job(self, operation: str, generation: int) -> bool:
        if generation != self._loadcell_workflow_generation:
            return False
        if self._loadcell_pending_job is not None:
            self.loadcell_tab.set_status(
                "Wait for the active load-cell calculation to finish.", "warning"
            )
            return False
        self._loadcell_pending_job = (generation, str(operation))
        self.loadcell_tab.set_workflow_job_active(True)
        self._refresh_readiness()
        return True

    def _finish_loadcell_job(self, operation: str, generation: int) -> bool:
        if self._loadcell_pending_job != (generation, str(operation)):
            return False
        self._loadcell_pending_job = None
        self.loadcell_tab.set_workflow_job_active(False)
        return True

    def _loadcell_job_callback_is_current(
        self,
        operation: str,
        generation: int,
        serial_identity: tuple[str, ...],
        known_mass_g: float,
        calibration_id: str | None = None,
    ) -> bool:
        """Accept a worker callback only for its exact immutable transaction."""

        if (
            generation != self._loadcell_workflow_generation
            or self._loadcell_pending_job != (generation, str(operation))
        ):
            return False
        current_calibration_id = (
            None
            if self._calibration is None
            else str(self._calibration.calibration_id)
        )
        context_matches = (
            self._serial_identity_snapshot() == serial_identity
            and math.isclose(
                float(self.loadcell_tab.known_mass_spin.value()),
                float(known_mass_g),
                rel_tol=0.0,
                abs_tol=1.0e-9,
            )
            and (
                calibration_id is None
                or current_calibration_id == str(calibration_id)
            )
        )
        if context_matches:
            return True
        self._advance_loadcell_workflow_generation()
        self.loadcell_tab.set_status(
            f"Discarded stale {operation} result because the load-cell context changed.",
            "warning",
        )
        self._refresh_readiness()
        return False

    def _loadcell_job_failed(
        self,
        operation: str,
        generation: int,
        serial_identity: tuple[str, ...],
        known_mass_g: float,
        calibration_id: str | None,
        message: str,
    ) -> None:
        if not self._loadcell_job_callback_is_current(
            operation,
            generation,
            serial_identity,
            known_mass_g,
            calibration_id,
        ):
            return
        self._finish_loadcell_job(operation, generation)
        self.loadcell_tab.set_status(message, "error")
        self._refresh_readiness()
        if operation == "tare" and self._printer_sequence_preparation_stage == "tare":
            self._printer_sequence_preparation_failed(
                f"Pre-sequence tare failed: {message}"
            )

    def _connect_serial(self, request: Mapping[str, object]) -> None:
        self._active_loadcell_config = replace(
            self.config.loadcell,
            baud_rate=int(request["baud_rate"]),
            data_bits=int(request.get("data_bits", self.config.loadcell.data_bits)),
            parity=str(request.get("parity", self.config.loadcell.parity)),
            stop_bits=float(request.get("stop_bits", self.config.loadcell.stop_bits)),
            read_timeout_s=float(
                request.get("read_timeout_s", self.config.loadcell.read_timeout_s)
            ),
            startup_delay_s=float(
                request.get("startup_delay_s", self.config.loadcell.startup_delay_s)
            ),
            validation_timeout_s=float(
                request.get(
                    "validation_timeout_s",
                    self.config.loadcell.validation_timeout_s,
                )
            ),
            stream_timeout_s=float(
                request.get("stream_timeout_s", self.config.loadcell.stream_timeout_s)
            ),
            input_format=str(
                request.get("input_format", self.config.loadcell.input_format)
            ),
            validation_min_readings=int(
                request.get(
                    "validation_min_readings",
                    self.config.loadcell.validation_min_readings,
                )
            ),
        )
        mode = str(self.loadcell_tab.source_mode_combo.currentData())
        source = (
            self.loadcell_tab.playback_path_edit.text().strip()
            if mode == "Playback (simulation)"
            else str(request.get("port") or "")
        )
        printer_port = str(
            (self._printer_connection_info or {}).get("port", "")
        )
        if (
            mode == "Physical"
            and source
            and printer_port
            and source.casefold() == printer_port.casefold()
        ):
            self.loadcell_tab.set_status(
                f"Load-cell port {source} conflicts with the connected printer port. "
                "Keep the Nano/HX711 on COM3 and the printer on COM4.",
                "error",
            )
            return
        calibration_mismatch_message = ""
        if self._calibration is not None and not self._calibration_matches_device(
            self._calibration,
            port=("SIMULATED" if mode != "Physical" else source),
            baud_rate=self._active_loadcell_config.baud_rate,
        ):
            prior = self._calibration
            self._reset_calibration()
            calibration_mismatch_message = (
                "The loaded calibration was not applied because it belongs to "
                f"{prior.serial_port} at {prior.baud_rate} baud, not {source} at "
                f"{self._active_loadcell_config.baud_rate} baud. Recalibrate this device. "
            )
        self.loadcell_tab.set_status("Connecting in the serial-owner thread…")
        if calibration_mismatch_message:
            self.loadcell_tab.set_status(
                calibration_mismatch_message + "Connecting in the serial-owner thread.",
                "warning",
            )
        self.serial_connect_command.emit(mode, self._active_loadcell_config, source, self._calibration)

    def _serial_connected(self, info: object) -> None:
        self._serial_info = info
        self._serial_transport_connected = True
        self.readiness.serial_connected = False
        simulation = bool(getattr(info, "simulation_mode", False))
        message = (
            "SIMULATION load-cell source connected; no physical Arduino/HX711 is claimed."
            if simulation
            else "Serial port opened; waiting for valid numeric HX711 readings."
        )
        self.loadcell_tab.set_connection_state(True, info, message=message)
        self.loadcell_tab.set_stream_ready(False)
        if self._calibration is not None and self._calibration_matches_device(
            self._calibration,
            port=str(getattr(info, "port", "")),
            baud_rate=int(getattr(info, "baud_rate", 0)),
        ):
            self.serial_calibration_command.emit(self._calibration)
            self.loadcell_tab.set_known_mass_g(self._calibration.known_mass_g)
            self.loadcell_tab.update_calibration_result(self._calibration.to_dict())
            if self._calibration.verification is not None:
                self.loadcell_tab.update_verification_result(
                    asdict(self._calibration.verification)
                )
        elif self._calibration is not None:
            prior = self._calibration
            self._reset_calibration()
            self.loadcell_tab.set_status(
                "Serial readings are available, but the saved calibration was rejected: "
                f"profile={prior.serial_port}@{prior.baud_rate}, "
                f"connected={getattr(info, 'port', '')}@{getattr(info, 'baud_rate', 0)}.",
                "warning",
            )
        self.recording_tab.set_simulation_mode(simulation or self._camera_is_simulation())
        self._refresh_readiness()

    def _serial_disconnected(self) -> None:
        had_connection = self.readiness.serial_connected
        self._serial_info = None
        self._serial_transport_connected = False
        self.readiness.serial_connected = False
        self._cancel_sample_window("load-cell source disconnected")
        self._advance_loadcell_workflow_generation()
        self._cancel_recording_preparation(
            "load-cell source disconnected during recording preparation"
        )
        self.loadcell_tab.set_connection_state(False, message="Serial source disconnected.")
        if had_connection:
            self._abort_active_recording(
                "Load-cell source disconnected during recording",
                ErrorCode.SERIAL_DISCONNECTED,
            )
        self._refresh_readiness()

    def _serial_health_changed(
        self, healthy: bool, error_code: str, message: str
    ) -> None:
        if not self._serial_transport_connected:
            self.readiness.serial_connected = False
            self._refresh_readiness()
            return
        self.readiness.serial_connected = bool(healthy)
        self.loadcell_tab.set_stream_ready(bool(healthy))
        if healthy:
            self.loadcell_tab.set_status(message, "success")
        else:
            self._cancel_sample_window("load-cell stream is not healthy")
            self._advance_loadcell_workflow_generation()
            self._cancel_recording_preparation("load-cell stream became unhealthy")
            self.loadcell_tab.set_status(message, "warning")
            if error_code == ErrorCode.HX711_TIMEOUT.value:
                self._abort_active_recording(
                    message,
                    ErrorCode.HX711_TIMEOUT,
                )
        self._refresh_readiness()

    def _serial_diagnostics(self, values: Mapping[str, object]) -> None:
        self.loadcell_tab.update_diagnostics(values)
        measured = _finite(values.get("measured_sample_rate_hz"))
        if measured is not None and measured > 0:
            self._measured_sample_rate_hz = measured
        self.recording_tab.update_performance_metrics(
            {"loadcell_sample_rate_hz": f"{self._measured_sample_rate_hz:.2f}"}
        )

    def _refresh_force(self) -> None:
        sample = self.sample_buffer.take_latest()
        if sample is None:
            return
        self.loadcell_tab.update_live_reading(
            float(getattr(sample, "raw_adc")),
            float(getattr(sample, "force_gf")),
            float(getattr(sample, "force_N")),
            elapsed_time_s=max(0.0, float(getattr(sample, "elapsed_time_s", 0.0))),
            wall_clock_iso=str(getattr(sample, "wall_clock_iso", "")),
            reading_frequency_hz=self._measured_sample_rate_hz,
            tare_raw=(
                None if self._calibration is None else self._calibration.tare_raw
            ),
        )

    def _start_sample_window(self, stage: str) -> None:
        if not self.readiness.serial_connected:
            self.loadcell_tab.set_status("Connect a load-cell source first.", "error")
            return
        normalized = str(stage).strip().lower()
        if self._sample_collector.active:
            self.loadcell_tab.set_status(
                "Wait for the active sample window to finish before starting another.",
                "warning",
            )
            return
        if self._loadcell_pending_job is not None and normalized != "unloaded":
            self.loadcell_tab.set_status(
                "Wait for the active load-cell calculation to finish.", "warning"
            )
            return
        if normalized == "unloaded":
            self._advance_loadcell_workflow_generation()
            self._cancel_recording_preparation("a new unloaded calibration began")
            self._calibration_windows.clear()
            self._calibration_computation = None
            self._calibration = None
            self.readiness.loadcell_calibrated = False
            self.readiness.calibration_verified = False
            self.serial_calibration_command.emit(None)
            self.loadcell_tab.reset_calibration_progress()
        elif normalized == "loaded" and "unloaded" not in self._calibration_windows:
            self.loadcell_tab.set_status("Capture the unloaded window first.", "error")
            return
        elif normalized == "tare" and self._calibration is None:
            self.loadcell_tab.set_status("Calculate an accepted calibration first.", "error")
            return
        elif normalized == "verify" and (
            self._calibration is None
            or self._calibration.tare_timestamp_iso
            == self._calibration.calibration_timestamp_iso
        ):
            self.loadcell_tab.set_status("Complete the unloaded tare before verification.", "error")
            return
        if normalized == "tare":
            assert self._calibration is not None
            if self._calibration.verification is not None:
                self._calibration = replace(self._calibration, verification=None)
                self.serial_calibration_command.emit(self._calibration)
            self.readiness.calibration_verified = False
            self.loadcell_tab.invalidate_verification_for_retare()
        elif normalized == "verify":
            assert self._calibration is not None
            if self._calibration.verification is not None:
                self._calibration = replace(self._calibration, verification=None)
                self.serial_calibration_command.emit(self._calibration)
            self.readiness.calibration_verified = False
            if self.loadcell_tab.workflow_stage == "verified":
                self.loadcell_tab.set_verification_state(False)
        self._sample_collector.begin(
            normalized, self._active_loadcell_config.calibration_window_s
        )
        self._sample_window_generation = self._loadcell_workflow_generation
        self.loadcell_tab.set_sample_window_active(True)
        self.loadcell_tab.set_status(
            f"Capturing every fresh raw sample for the {normalized} window…", "info"
        )
        self._refresh_readiness()

    def _cancel_sample_window(self, reason: str) -> None:
        was_active = self._sample_collector.cancel()
        self._sample_window_generation = None
        self.loadcell_tab.set_sample_window_active(False)
        if was_active:
            self.loadcell_tab.set_status(
                f"Sample window cancelled: {reason}.", "warning"
            )
        self._refresh_readiness()

    def _refresh_status(self) -> None:
        if self.readiness.camera_connected:
            warmup_elapsed = (
                time.perf_counter_ns() - self._camera_connected_at_ns
            ) / 1e9
            remaining = max(
                0.0, self._active_camera_config.warmup_seconds - warmup_elapsed
            )
            if remaining > 0.0:
                self.camera_tab.set_next_action(
                    f"Camera warm-up: {remaining:.1f} s remaining; frames are excluded from baseline/recording."
                )
            elif not self.readiness.baseline_valid:
                self.camera_tab.set_next_action(
                    "Warm-up complete. Define nine ROIs and capture the unloaded baseline."
                )
        progress = self._sample_collector.progress
        if progress is not None:
            stage, sample_count, elapsed_s, duration_s = progress
            remaining_s = max(0.0, duration_s - elapsed_s)
            statistics = self._sample_collector.statistics
            stability = "waiting for samples"
            if statistics is not None:
                _, mean_raw, std_raw = statistics
                stability = (
                    f"mean {mean_raw:.3f}, standard deviation {std_raw:.3f} counts; "
                    + (
                        "stable"
                        if std_raw <= self._active_loadcell_config.maximum_tare_std_counts
                        else "still stabilizing"
                    )
                )
            self.loadcell_tab.set_next_action(
                f"Capturing {stage}: {sample_count} fresh samples; "
                f"{remaining_s:.1f} s remaining; {stability}."
            )
        completed = self._sample_collector.take_completed()
        if completed is not None:
            generation = self._sample_window_generation
            self._sample_window_generation = None
            self.loadcell_tab.set_sample_window_active(False)
            stage, samples = completed
            if generation != self._loadcell_workflow_generation:
                self._refresh_readiness()
                return
            if stage in {"unloaded", "loaded"}:
                self._calibration_windows[stage] = samples
                self.loadcell_tab.mark_sample_window_captured(stage)
                self.loadcell_tab.set_status(
                    f"Captured {len(samples)} fresh samples for {stage}.", "success"
                )
            elif stage == "verify":
                self._verify_calibration(samples)
            elif stage == "tare":
                self._apply_tare(samples)
            self._refresh_readiness()

    def _minimum_calibration_samples(self) -> int:
        possible = math.floor(
            self._measured_sample_rate_hz * self._active_loadcell_config.calibration_window_s
        )
        return (
            self._active_loadcell_config.minimum_calibration_samples
            if possible >= self._active_loadcell_config.minimum_calibration_samples
            else self._active_loadcell_config.fallback_minimum_calibration_samples
        )

    def _calculate_calibration(self, known_mass_g: float) -> None:
        if not {"unloaded", "loaded"} <= self._calibration_windows.keys():
            self.loadcell_tab.set_status("Capture unloaded and known-mass windows first.", "error")
            return
        identity = self._serial_identity_snapshot()
        if identity is None:
            self.loadcell_tab.set_status("The serial identity is unavailable.", "error")
            return
        generation = self._loadcell_workflow_generation
        mass = float(known_mass_g)
        if not self._begin_loadcell_job("calculation", generation):
            return
        config = self._active_loadcell_config
        self._run_job(
            calculate_calibration,
            lambda computation: self._calibration_calculated(
                generation, identity, mass, computation
            ),
            lambda text: self._loadcell_job_failed(
                "calculation", generation, identity, mass, None, text
            ),
            self._calibration_windows["unloaded"],
            self._calibration_windows["loaded"],
            mass,
            minimum_sample_count=self._minimum_calibration_samples(),
            minimum_calibration_snr=config.minimum_calibration_snr,
            maximum_loaded_window_cv_percent=config.maximum_loaded_window_cv_percent,
            minimum_abs_counts_per_gram=config.minimum_abs_counts_per_gram,
        )

    def _calibration_calculated(
        self,
        generation: int,
        identity: tuple[str, ...],
        known_mass_g: float,
        computation: object,
    ) -> None:
        if not self._loadcell_job_callback_is_current(
            "calculation", generation, identity, known_mass_g
        ):
            return
        try:
            identity_values = self._serial_identity_mapping(identity)
            calibration = build_saved_calibration(
                f"calibration-{uuid.uuid4().hex}",
                computation,
                None,
                serial_port=identity_values["port"],
                firmware_identity=identity_values["firmware_identity"],
                protocol_version=identity_values["protocol_version"],
                firmware_version=identity_values["firmware_version"],
                calibration_timestamp_iso=datetime.now(UTC).isoformat(),
                baud_rate=self._active_loadcell_config.baud_rate,
            )
            assessment = getattr(computation, "assessment")
            values = asdict(assessment)
            values.update(
                {
                    "counts_per_gram": getattr(computation, "counts_per_gram"),
                    "tare_raw": getattr(computation, "tare_raw"),
                    "quality_passed": getattr(assessment, "passed"),
                }
            )
        except Exception as exc:
            self._calibration = None
            self.loadcell_tab.set_calibration_available(False)
            self.loadcell_tab.set_status(str(exc), "error")
            self.readiness.loadcell_calibrated = False
            self.readiness.calibration_verified = False
            self._finish_loadcell_job("calculation", generation)
            self._refresh_readiness()
            return
        self._calibration_computation = computation
        self._calibration = calibration
        self.readiness.loadcell_calibrated = True
        self.readiness.calibration_verified = False
        self.serial_calibration_command.emit(self._calibration)
        self.loadcell_tab.update_calibration_result(values)
        self._finish_loadcell_job("calculation", generation)
        self._refresh_readiness()

    def _verify_calibration(self, samples: tuple[int, ...]) -> None:
        if self._calibration is None:
            self.loadcell_tab.set_status("Calculate an accepted calibration first.", "error")
            return
        identity = self._serial_identity_snapshot()
        if identity is None:
            self.loadcell_tab.set_status("The serial identity is unavailable.", "error")
            return
        generation = self._loadcell_workflow_generation
        calibration = self._calibration
        mass = float(calibration.known_mass_g)
        calibration_id = str(calibration.calibration_id)
        if not self._begin_loadcell_job("verification", generation):
            return
        self._run_job(
            verify_and_build_calibration,
            lambda result: self._verification_finished(
                generation, identity, mass, calibration_id, result
            ),
            lambda text: self._loadcell_job_failed(
                "verification",
                generation,
                identity,
                mass,
                calibration_id,
                text,
            ),
            calibration,
            samples,
            config=self._active_loadcell_config,
            minimum_sample_count=self._minimum_calibration_samples(),
            serial_identity=self._serial_identity_mapping(identity),
        )

    def _verification_finished(
        self,
        generation: int,
        identity: tuple[str, ...],
        known_mass_g: float,
        calibration_id: str,
        result: object,
    ) -> None:
        if not self._loadcell_job_callback_is_current(
            "verification",
            generation,
            identity,
            known_mass_g,
            calibration_id,
        ):
            return
        calibration, verification = result
        self.loadcell_tab.update_verification_result(asdict(verification))
        if not verification.passed:
            self.readiness.loadcell_calibrated = self._calibration is not None
            self.readiness.calibration_verified = False
            self._finish_loadcell_job("verification", generation)
            self._refresh_readiness()
            return
        self._calibration = calibration
        self.readiness.loadcell_calibrated = True
        self.readiness.calibration_verified = True
        self.serial_calibration_command.emit(self._calibration)
        self._finish_loadcell_job("verification", generation)
        self._refresh_readiness()

    def _apply_tare(self, samples: tuple[int, ...]) -> None:
        if self._calibration is None:
            self.loadcell_tab.set_status("Complete calibration before tare.", "error")
            return
        identity = self._serial_identity_snapshot()
        if identity is None:
            self.loadcell_tab.set_status("The serial identity is unavailable.", "error")
            return
        generation = self._loadcell_workflow_generation
        calibration = self._calibration
        mass = float(calibration.known_mass_g)
        calibration_id = str(calibration.calibration_id)
        if not self._begin_loadcell_job("tare", generation):
            return
        self._run_job(
            calculate_tare,
            lambda result: self._tare_finished(
                generation,
                identity,
                mass,
                calibration_id,
                calibration,
                result,
            ),
            lambda text: self._loadcell_job_failed(
                "tare", generation, identity, mass, calibration_id, text
            ),
            samples,
            minimum_sample_count=self._active_loadcell_config.fallback_minimum_calibration_samples,
            maximum_std_counts=self._active_loadcell_config.maximum_tare_std_counts,
        )

    def _tare_finished(
        self,
        generation: int,
        identity: tuple[str, ...],
        known_mass_g: float,
        calibration_id: str,
        calibration: LoadCellCalibration,
        result: object,
    ) -> None:
        if not self._loadcell_job_callback_is_current(
            "tare", generation, identity, known_mass_g, calibration_id
        ):
            return
        try:
            updated = calibration.with_tare(
                getattr(result, "tare_raw"), datetime.now(UTC).isoformat()
            )
            self._calibration = updated
            if self._calibration_computation is not None:
                self._calibration_computation = replace(
                    self._calibration_computation,
                    tare_raw=float(getattr(result, "tare_raw")),
                )
            self.readiness.loadcell_calibrated = True
            self.readiness.calibration_verified = False
            self.serial_calibration_command.emit(self._calibration)
            self.loadcell_tab.update_calibration_result(self._calibration.to_dict())
            self.loadcell_tab.mark_tare_complete()
            self.loadcell_tab.set_status(
                f"Tare updated from {getattr(result, 'sample_count')} stable samples; scale unchanged.",
                "success",
            )
            self._finish_loadcell_job("tare", generation)
            self._refresh_readiness()
            if (
                self._pending_printer_sequence is not None
                and self._printer_sequence_preparation_stage == "tare"
            ):
                self._printer_sequence_preparation_stage = "tare_complete"
                self._continue_printer_sequence_preparation()
        except Exception as exc:
            self._finish_loadcell_job("tare", generation)
            self.loadcell_tab.set_status(str(exc), "error")
            self._refresh_readiness()
            if self._printer_sequence_preparation_stage == "tare":
                self._printer_sequence_preparation_failed(
                    f"Pre-sequence tare failed: {exc}"
                )

    def _known_mass_changed(self, _mass_g: float) -> None:
        """Invalidate every mass-dependent result when the operator edits mass."""

        self._cancel_sample_window("known calibration mass changed")
        self._advance_loadcell_workflow_generation()
        self._cancel_recording_preparation("known calibration mass changed")
        self._calibration_windows.clear()
        self._calibration_computation = None
        self._calibration = None
        self.readiness.loadcell_calibrated = False
        self.readiness.calibration_verified = False
        self.serial_calibration_command.emit(None)
        self._refresh_readiness()

    def _trial_labels_changed(self, values: Mapping[str, object]) -> None:
        if self._preparing_recording:
            self._cancel_recording_preparation(
                "trial labels changed during recording preparation"
            )
        self.readiness.trial_labels_valid = all(
            bool(str(values.get(name, "")).strip())
            for name in ("session_id", "trial_id", "sensing_skin_id")
        )
        if self.lifecycle in {RecordingLifecycle.COMPLETE, RecordingLifecycle.ERROR}:
            self._set_lifecycle(RecordingLifecycle.IDLE)
        self._refresh_readiness()

    def _choose_output_directory(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose recording output directory")
        if not chosen:
            return
        self._cancel_recording_preparation(
            "output directory changed during recording preparation"
        )
        self._output_directory = Path(chosen).resolve()
        self._active_session_directory = None
        self.recording_tab.set_output_directory(str(self._output_directory))
        self.readiness.output_directory_valid = False
        self.readiness.video_writer_preflight_passed = False
        self.readiness.disk_space_sufficient = False
        self._start_output_preflight()
        self._refresh_readiness()

    def _start_output_preflight(self) -> None:
        if self._output_directory is None:
            return
        recording_config = replace(
            self.config.recording, output_directory=str(self._output_directory)
        )
        raw_frame_size = (
            int(getattr(self._camera_info, "actual_width", self._active_camera_config.requested_width)),
            int(getattr(self._camera_info, "actual_height", self._active_camera_config.requested_height)),
        )
        frame_size = oriented_frame_size(
            raw_frame_size[0],
            raw_frame_size[1],
            self._active_camera_config.rotation_degrees,
        )
        fps = self._effective_camera_fps()
        self._preflight_generation += 1
        generation = self._preflight_generation
        self.recording_tab.set_status("Testing directory, free space, and MP4/AVI writer off-thread…")
        self._run_job(
            preflight_recording_output,
            lambda result: self._preflight_result_for_generation(generation, result),
            lambda message: self._preflight_failure_for_generation(generation, message),
            recording_config,
            frame_size=frame_size,
            output_fps=fps,
        )

    def _preflight_result_for_generation(self, generation: int, result: object) -> None:
        if generation == self._preflight_generation:
            self._preflight_succeeded(result)

    def _preflight_failure_for_generation(self, generation: int, message: str) -> None:
        if generation == self._preflight_generation:
            self._preflight_failed(message)

    def _preflight_succeeded(self, result: object) -> None:
        self.readiness.output_directory_valid = bool(getattr(result, "output_directory_valid"))
        self.readiness.video_writer_preflight_passed = bool(
            getattr(result, "video_writer_preflight_passed")
        )
        self.readiness.disk_space_sufficient = bool(getattr(result, "disk_space_sufficient"))
        self.recording_tab.set_status(
            f"Output preflight passed with {getattr(result, 'selected_codec')} "
            f"({getattr(result, 'free_bytes') / (1024**3):.2f} GiB free).",
            "Next recommended action: complete any remaining readiness items.",
        )
        self._replace_graph_service()
        self._refresh_readiness()

    def _preflight_failed(self, message: str) -> None:
        self.readiness.output_directory_valid = False
        self.readiness.video_writer_preflight_passed = False
        self.readiness.disk_space_sufficient = False
        self.recording_tab.set_status(
            f"Output preflight failed: {message}",
            "Choose a writable location with sufficient free space and a working codec.",
        )
        self._refresh_readiness()

    def _request_recording(self, values: Mapping[str, object]) -> None:
        self._request_recording_internal(values, automated_sequence=False)

    def _recording_prerequisites_ready(self, *, automated_sequence: bool) -> bool:
        values = self.readiness.as_dict()
        # ``as_dict`` includes the derived conjunction itself. Automated
        # pressing intentionally substitutes a quality-passed calibration for
        # the separate verification flag, so retaining the precomputed false
        # value would make this path impossible to satisfy.
        values.pop("ready_to_record", None)
        if automated_sequence:
            values.pop("calibration_verified", None)
            return bool(
                all(values.values())
                and self._calibration is not None
                and self._calibration.quality_passed
            )
        return self.readiness.ready_to_record

    def _request_recording_internal(
        self,
        values: Mapping[str, object],
        *,
        automated_sequence: bool,
    ) -> None:
        if self._baseline_locked:
            self.recording_tab.set_status(
                "Recording remains blocked while baseline capture or calculation is active."
            )
            return
        if self._loadcell_workflow_busy():
            self.recording_tab.set_status(
                "Recording remains blocked while a load-cell sample window or "
                "calibration operation is active."
            )
            return
        if self._preparing_recording or self.lifecycle is not RecordingLifecycle.IDLE:
            return
        if not self._recording_prerequisites_ready(
            automated_sequence=automated_sequence
        ):
            self.recording_tab.set_status("Recording remains blocked by the readiness checklist.")
            return
        if self._latest_captured is None or self._baseline is None:
            self.recording_tab.set_status("A current unloaded preview frame and baseline are required.")
            return
        if not self.camera_tab.previewing:
            self.recording_tab.set_status("Start the camera preview before recording.")
            return
        self._recording_prepare_generation += 1
        generation = self._recording_prepare_generation
        self._recording_prepare_label_values = copy.deepcopy(dict(values))
        self._printer_automated_recording = bool(automated_sequence)
        self._preparing_recording = True
        # A new operator transaction owns all later review callbacks.  Any
        # still-running review from the prior session becomes stale here.
        self._review_generation += 1
        self.recording_tab.clear_review_summary()
        self._reset_recording_performance_metrics()
        self._update_configuration_locks()
        timeout_s = max(2.0, self._active_camera_config.warmup_seconds + 2.0)
        self._await_authoritative_camera_warmup(
            "recording",
            generation,
            time.perf_counter_ns() + round(timeout_s * 1_000_000_000),
        )

    def _begin_recording_drift_check(self, generation: int) -> None:
        if generation != self._recording_prepare_generation:
            return
        try:
            snapshot = self._recording_preparation_snapshot(generation)
        except Exception as exc:
            self._recording_prepare_failed(str(exc), generation)
            return
        self.recording_tab.set_status(
            "Checking unloaded baseline drift off-thread before recording."
        )
        self._run_job(
            check_frame_baseline_drift,
            lambda result: self._drift_checked(generation, result, snapshot),
            lambda message: self._recording_prepare_failed(message, generation),
            snapshot["frame_bgr"],
            snapshot["layout"].rois,
            snapshot["baseline"],
            self._active_camera_config.baseline_drift_mean_v,
        )

    def _recording_preparation_snapshot(self, generation: int) -> dict[str, object]:
        """Capture one immutable transaction after settings and warm-up."""

        if generation != self._recording_prepare_generation or not self._preparing_recording:
            raise ValueError("Recording preparation was cancelled.")
        if self._loadcell_workflow_busy():
            raise ValueError("A load-cell workflow operation is still active.")
        if not self._recording_prerequisites_ready(
            automated_sequence=self._printer_automated_recording
        ):
            raise ValueError("A readiness prerequisite changed during recording preparation.")
        if not self.camera_tab.previewing:
            raise ValueError("Camera preview is not running.")
        if any(
            value is None
            for value in (
                self._output_directory,
                self._calibration,
                self._baseline,
                self._baseline_context,
                self._camera_info,
                self._serial_info,
                self._latest_captured,
            )
        ):
            raise ValueError(
                "Output, device identity, calibration, frame, or baseline is unavailable."
            )
        assert self._calibration is not None
        assert self._baseline is not None
        assert self._camera_info is not None
        assert self._serial_info is not None
        assert self._latest_captured is not None
        assert self._output_directory is not None
        if (
            not self._printer_automated_recording
            and (
                self._calibration.verification is None
                or not self._calibration.verification.passed
            )
        ):
            raise ValueError("The load-cell calibration is not currently verified.")
        context = self._baseline_context_for_current_layout()
        if not validate_baseline_context(self._baseline, context):
            raise ValueError("The baseline no longer matches the camera and ROI context.")
        layout = self.roi_tab.current_layout()
        frame = np.asarray(getattr(self._latest_captured, "original_bgr"))
        if frame.shape[:2] != (layout.frame_height, layout.frame_width):
            raise ValueError("The current frame dimensions no longer match the ROI layout.")
        frame_ns = int(getattr(self._latest_captured, "host_monotonic_ns"))
        warmup_ready_ns = self._camera_warmup_origin_ns + round(
            self._active_camera_config.warmup_seconds * 1_000_000_000
        )
        if frame_ns < warmup_ready_ns:
            raise ValueError("The latest frame remains inside post-setting warm-up.")
        labels_payload = {
            key: value
            for key, value in self._recording_prepare_label_values.items()
            if key != "trial_label_scope"
        }
        labels = TrialLabels(**labels_payload)
        auxiliary_streams = self.recording_tab.recording_streams()
        if "motion_magnified" in auxiliary_streams and not self._motion_config.enabled:
            raise ValueError(
                "Motion-magnified recording was selected, but Motion Magnification is disabled."
            )
        if (
            self._quantitative_analysis_source == "Motion-magnified frame"
            and not self._motion_config.enabled
        ):
            raise ValueError(
                "Motion-magnified quantitative analysis requires Motion Magnification to be enabled."
            )
        return {
            "generation": generation,
            "labels": labels,
            "output_directory": Path(self._output_directory),
            "layout": copy.deepcopy(layout),
            "baseline": copy.deepcopy(self._baseline),
            "baseline_id": str(self._baseline.baseline_id),
            "baseline_context": copy.deepcopy(context),
            "calibration": copy.deepcopy(self._calibration),
            "calibration_id": str(self._calibration.calibration_id),
            "camera_info": copy.deepcopy(self._camera_info),
            "camera_identity": asdict(self._camera_info),
            "camera_orientation": {
                "rotation_degrees_clockwise": self._active_camera_config.rotation_degrees,
                "mirror_horizontal": self._active_camera_config.mirror_horizontal,
            },
            "serial_info": copy.deepcopy(self._serial_info),
            "serial_identity": asdict(self._serial_info),
            "camera_requested_controls": copy.deepcopy(
                self._camera_requested_controls
            ),
            "camera_controls": copy.deepcopy(self._camera_controls),
            "camera_mode_readbacks": copy.deepcopy(self._camera_mode_readbacks),
            "camera_property_readbacks": copy.deepcopy(self._camera_property_readbacks),
            "camera_capabilities": copy.deepcopy(self._camera_capabilities),
            "automatic_control_warning": self._automatic_control_warning,
            "motion_magnification": copy.deepcopy(self._motion_config.to_dict()),
            "quantitative_analysis_source": self._quantitative_analysis_source,
            "processed_preview_mode": self.video_display.processed_mode_combo.currentText(),
            "auxiliary_video_streams": auxiliary_streams,
            "frame_bgr": np.array(frame, copy=True),
            "recording_origin_ns": frame_ns,
            "readiness": copy.deepcopy(self.readiness.as_dict()),
            "automated_printer_sequence": self._printer_automated_recording,
            "printer_sequence_settings": (
                asdict(self._pending_printer_sequence)
                if self._pending_printer_sequence is not None
                else None
            ),
            "printer_connection": copy.deepcopy(self._printer_connection_info),
        }

    def _recording_snapshot_still_valid(self, snapshot: Mapping[str, object]) -> bool:
        if (
            int(snapshot["generation"]) != self._recording_prepare_generation
            or not self._preparing_recording
            or self._loadcell_workflow_busy()
            or not self._recording_prerequisites_ready(
                automated_sequence=bool(snapshot.get("automated_printer_sequence", False))
            )
            or not self.camera_tab.previewing
            or self._camera_info is None
            or self._serial_info is None
            or self._baseline is None
            or self._calibration is None
            or self._output_directory is None
        ):
            return False
        try:
            return bool(
                asdict(self._camera_info) == snapshot["camera_identity"]
                and {
                    "rotation_degrees_clockwise": self._active_camera_config.rotation_degrees,
                    "mirror_horizontal": self._active_camera_config.mirror_horizontal,
                }
                == snapshot["camera_orientation"]
                and asdict(self._serial_info) == snapshot["serial_identity"]
                and Path(self._output_directory) == snapshot["output_directory"]
                and self.roi_tab.current_layout() == snapshot["layout"]
                and dict(self._camera_requested_controls)
                == snapshot["camera_requested_controls"]
                and dict(self._camera_controls) == snapshot["camera_controls"]
                and self._camera_property_readbacks
                == snapshot["camera_property_readbacks"]
                and self._camera_capabilities == snapshot["camera_capabilities"]
                and self._motion_config.to_dict() == snapshot["motion_magnification"]
                and self._quantitative_analysis_source
                == snapshot["quantitative_analysis_source"]
                and self.video_display.processed_mode_combo.currentText()
                == snapshot["processed_preview_mode"]
                and self.recording_tab.recording_streams()
                == snapshot["auxiliary_video_streams"]
                and TrialLabels(
                    **{
                        key: value
                        for key, value in self.recording_tab.trial_labels().items()
                        if key != "trial_label_scope"
                    }
                )
                == snapshot["labels"]
                and str(self._baseline.baseline_id) == snapshot["baseline_id"]
                and str(self._calibration.calibration_id) == snapshot["calibration_id"]
                and (
                    bool(snapshot.get("automated_printer_sequence", False))
                    or (
                        self._calibration.verification is not None
                        and self._calibration.verification.passed
                    )
                )
                and validate_baseline_context(
                    self._baseline, self._baseline_context_for_current_layout()
                )
            )
        except Exception:
            return False

    def _drift_checked(
        self, generation: int, result: object, snapshot: Mapping[str, object]
    ) -> None:
        if generation != self._recording_prepare_generation:
            return
        if not bool(getattr(result, "accepted", False)):
            self._recording_prepare_failed(
                f"Baseline drift {getattr(result, 'mean_absolute_drift'):.3f} exceeds "
                f"{getattr(result, 'threshold'):.3f}; recapture the unloaded baseline.",
                generation,
            )
            self._invalidate_baseline("pre-recording drift check failed", camera=False)
            return
        if not self._recording_snapshot_still_valid(snapshot):
            self._recording_prepare_failed(
                "A readiness, identity, ROI, baseline, calibration, label, or output value changed during preparation.",
                generation,
            )
            return
        self._begin_recording(snapshot, result)

    def _begin_recording(
        self, snapshot: Mapping[str, object], drift: object
    ) -> None:
        generation = int(snapshot["generation"])
        if not self._recording_snapshot_still_valid(snapshot):
            self._recording_prepare_failed(
                "Recording prerequisites changed before acquisition sinks were attached.",
                generation,
            )
            return
        try:
            self._construct_and_start_recording(snapshot, drift)
        except Exception as exc:
            self._recording_prepare_failed(str(exc), generation)

    def _construct_and_start_recording(
        self, snapshot: Mapping[str, object], drift: object
    ) -> None:
        labels = snapshot["labels"]
        layout = snapshot["layout"]
        info = snapshot["camera_info"]
        serial = snapshot["serial_info"]
        calibration = snapshot["calibration"]
        session_baseline = snapshot["baseline"]
        baseline_context = snapshot["baseline_context"]
        output_directory = Path(snapshot["output_directory"])
        pipeline = ProcessingPipeline(
            config=self.config,
            roi_layout=layout,
            baseline=session_baseline,
            baseline_context=baseline_context,
            trial_labels=labels,
            requested_fps=float(getattr(info, "requested_fps")),
            requested_width=self._active_camera_config.requested_width,
            requested_height=self._active_camera_config.requested_height,
            actual_fps=self._effective_camera_fps(),
            camera_backend=str(getattr(info, "backend")),
            camera_device_index=int(getattr(info, "device_index")),
            applied_camera_properties=snapshot["camera_controls"],
            calibration_id=calibration.calibration_id,
            arduino_protocol_version=str(getattr(serial, "protocol_version", "")),
            arduino_firmware_version=str(getattr(serial, "firmware_version", "")),
            motion_config=MotionMagnificationConfig(
                **snapshot["motion_magnification"]
            ),
            quantitative_analysis_source=str(
                snapshot["quantitative_analysis_source"]
            ),
            processed_preview_mode=str(snapshot["processed_preview_mode"]),
            auxiliary_video_streams=tuple(snapshot["auxiliary_video_streams"]),
        )
        recording_config = replace(
            self.config.recording, output_directory=str(output_directory)
        )
        recorder = SessionRecorder(recording_config)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        token = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{labels.sensing_skin_id}_{labels.trial_id}")
        directory_name = f"{timestamp}_{token}"[:180]
        session_config = self.config.to_dict()
        orientation = dict(snapshot["camera_orientation"])
        session_config["camera"]["rotation_degrees"] = orientation[
            "rotation_degrees_clockwise"
        ]
        session_config["camera"]["mirror_horizontal"] = orientation[
            "mirror_horizontal"
        ]
        session_config.update(
            {
                "trial_labels": labels.as_export_dict(),
                "readiness_at_start": snapshot["readiness"],
                "simulation_mode": self._camera_is_simulation() or self._serial_is_simulation(),
                "simulation_notice": (
                    "No physical hardware is claimed for any source marked simulation."
                ),
                "camera_actual": asdict(info),
                "gui_camera_property_control": (
                    "read current driver values; changes only through the "
                    "operator-opened DirectShow native properties dialog"
                ),
                "gui_camera_mode_control": "disabled; driver-selected stream",
                "original_frame_display": (
                    "unannotated camera image after configured rotation/mirroring"
                ),
                "camera_orientation": orientation,
                "camera_requested_controls": snapshot[
                    "camera_requested_controls"
                ],
                "camera_confirmed_actual_controls": snapshot["camera_controls"],
                "camera_mode_readbacks": snapshot["camera_mode_readbacks"],
                "camera_property_readbacks": snapshot["camera_property_readbacks"],
                "camera_capabilities": snapshot["camera_capabilities"],
                "serial_identity": asdict(serial),
                "pre_recording_baseline_drift_mean_v": getattr(drift, "mean_absolute_drift"),
                "baseline_drift_threshold": getattr(drift, "threshold"),
                "baseline_drift_passed": getattr(drift, "accepted"),
                "motion_magnification": snapshot["motion_magnification"],
                "quantitative_analysis_source": snapshot[
                    "quantitative_analysis_source"
                ],
                "magnified_pixels_used_for_exported_measurements": (
                    snapshot["quantitative_analysis_source"]
                    == "Motion-magnified frame"
                ),
                "processed_preview_mode": snapshot["processed_preview_mode"],
                "auxiliary_video_streams": list(snapshot["auxiliary_video_streams"]),
                "printer_motion": {
                    "enabled_for_session": bool(
                        snapshot.get("automated_printer_sequence", False)
                    ),
                    "connection": snapshot.get("printer_connection"),
                    "settings": snapshot.get("printer_sequence_settings"),
                    "press_zero_persisted": False,
                    "coordinate_policy": (
                        "software press zero only; target_machine_z_mm = "
                        "press_zero_z_mm + target_displacement_mm; G92 prohibited"
                    ),
                    "reported_position_policy": (
                        "printer_reported_* fields are populated only from parsed M114 responses"
                    ),
                    "force_safety_latency_policy": (
                        "each calibrated COM3 sample is checked in the serial-owner callback; "
                        "M410 is requested through the sole COM4 writer, followed by bounded safe retract"
                    ),
                },
            }
        )
        camera_settings = {
            "camera_model": "Arducam IMX179 Camera Module",
            "gui_camera_property_control": (
                "read current driver values; changes only through the "
                "operator-opened DirectShow native properties dialog"
            ),
            "gui_camera_mode_control": "disabled; driver-selected stream",
            "original_frame_display": (
                "unannotated camera image after configured rotation/mirroring"
            ),
            "camera_orientation": orientation,
            "camera_name_status": (
                "operator-selected model; OpenCV does not expose a trustworthy USB product name"
            ),
            "camera_identifier": (
                f"{getattr(info, 'backend')}:{getattr(info, 'device_index')}"
            ),
            "driver_information": (
                "Backend recorded in actual mode; Windows driver name/version are not "
                "available through OpenCV and must be recorded from Device Manager."
            ),
            "simulation_mode": self._camera_is_simulation(),
            "actual": asdict(info),
            "requested_controls": dict(snapshot["camera_requested_controls"]),
            "confirmed_actual_controls": dict(snapshot["camera_controls"]),
            # Backward-compatible key; its value is now explicitly restricted
            # to confirmed actual controls rather than unverified requests.
            "controls": dict(snapshot["camera_controls"]),
            "mode_readbacks": snapshot["camera_mode_readbacks"],
            "property_readbacks": snapshot["camera_property_readbacks"],
            "capabilities": snapshot["camera_capabilities"],
            "automatic_control_warning": snapshot["automatic_control_warning"],
        }
        recording_origin_ns = int(snapshot["recording_origin_ns"])
        worker = RecordingThread(
            pipeline=pipeline,
            recorder=recorder,
            recorder_start_kwargs={
                "session_directory_name": directory_name,
                "frame_size": (layout.frame_width, layout.frame_height),
                "output_fps": self._effective_camera_fps(),
                "calibration": calibration,
                "session_config": session_config,
                "camera_settings": camera_settings,
                "roi_layout": layout,
                "baseline": session_baseline,
                "max_sync_gap_ms": self._active_loadcell_config.max_sync_gap_ms,
                "contact_threshold_N": self._active_loadcell_config.contact_threshold_N,
                "auxiliary_video_streams": tuple(
                    snapshot["auxiliary_video_streams"]
                ),
            },
            recording_start_monotonic_ns=recording_origin_ns,
            queue_size=recording_config.recording_queue_size,
            display_buffer=self.recording_display_buffer,
            motion_context_provider=self.motion_context.snapshot,
            parent=self,
        )
        if not self._recording_snapshot_still_valid(snapshot):
            raise ValueError(
                "Recording prerequisites changed while constructing the session."
            )
        worker.session_started.connect(self._recording_started)
        worker.completed.connect(self._recording_completed)
        worker.failed.connect(self._recording_failed)
        worker.finished.connect(worker.deleteLater)
        self._recording_thread = worker
        self._recording_started_perf_ns = time.perf_counter_ns()
        self._recording_frame_count = 0
        self._recording_error_count = 0
        self._manual_heatmap_count = 0
        self._manual_line_graph_count = 0
        for panel in (self.roi_tab.graph_panel, self.recording_tab.graph_panel):
            panel.clear_temporal_history()
        self._stop_live_analysis()
        if self.recording_tab.disable_live_motion_during_raw_check.isChecked():
            self._stop_motion_processing()
        self.serial_worker.set_recording_origin(recording_origin_ns)
        self.camera_worker.set_recording_sink(worker.submit_frame)
        self.serial_worker.set_recording_sink(worker.submit_load_sample)
        worker.start()

    def _recording_started(self, directory: object) -> None:
        self._preparing_recording = False
        self._recording_prepare_label_values = {}
        self._active_session_directory = Path(directory)
        self._replace_graph_service(self._active_session_directory / "spatial_graphs")
        self._set_lifecycle(RecordingLifecycle.RECORDING)
        if self._printer_automated_recording and self._pending_printer_sequence is not None:
            sequence_config = replace(
                self._pending_printer_sequence,
                tare_before_sequence=False,
                fresh_baseline_before_sequence=False,
            )
            self._printer_sequence_preparation_stage = "running"
            self.printer_tab.set_sequence_preparation_status(
                "Recorder started; repeated-press command is queued in the printer-owner thread."
            )
            self.printer_tab.append_log(
                {
                    "level": "info",
                    "message": (
                        "Synchronized recorder started. Queuing repeated press; "
                        f"controller pre-roll is {sequence_config.pre_roll_s:.3f} s."
                    ),
                }
            )
            self.printer_prerequisites_command.emit(
                True,
                "Camera, load cell, baseline, output, recorder, homing, position, and press zero are ready.",
            )
            self.printer_start_command.emit(sequence_config)

    def _stop_recording(self) -> None:
        worker = self._recording_thread
        if worker is None or self.lifecycle is not RecordingLifecycle.RECORDING:
            return
        self.camera_worker.set_recording_sink(None)
        self.serial_worker.set_recording_sink(None)
        self.serial_worker.set_recording_origin(None)
        self._set_lifecycle(RecordingLifecycle.FINALIZING)
        worker.request_finalize()

    def _recording_completed(self, completion: RecordingCompletion) -> None:
        self.camera_worker.set_recording_sink(None)
        self.serial_worker.set_recording_sink(None)
        self.serial_worker.set_recording_origin(None)
        self._drain_terminal_recording_update()
        self._recording_thread = None
        self._active_session_directory = Path(completion.session_directory)
        self._set_lifecycle(RecordingLifecycle.COMPLETE)
        self._launch_review_job(
            completion,
            failure_prefix="Review calculation failed",
            preview_drops=max(
                0,
                self.preview_buffer.dropped_count
                - self._recording_preview_drop_baseline,
            ),
            recording_errors=self._recording_error_count,
            manual_heatmap_count=self._manual_heatmap_count,
            manual_line_graph_count=self._manual_line_graph_count,
        )
        if not self._closing:
            self._start_live_analysis()
            self._start_motion_processing()

    def _recording_failed(self, message: str, directory: object) -> None:
        self.camera_worker.set_recording_sink(None)
        self.serial_worker.set_recording_sink(None)
        self.serial_worker.set_recording_origin(None)
        self._drain_terminal_recording_update()
        self._recording_thread = None
        self._preparing_recording = False
        self._recording_error_count += 1
        self.recording_tab.update_performance_metrics(
            {"recording_pipeline_errors": self._recording_error_count}
        )
        self._set_lifecycle(RecordingLifecycle.ERROR)
        self.recording_tab.set_status(
            f"Partial session saved after error: {message}",
            f"Recoverable output directory: {directory}",
        )
        if directory is not None:
            partial_directory = Path(directory)
            self._active_session_directory = partial_directory
            self._replace_graph_service(partial_directory / "spatial_graphs")
            self._launch_review_job(
                partial_directory,
                failure_prefix="Partial-session review calculation failed",
                preview_drops=max(
                    0,
                    self.preview_buffer.dropped_count
                    - self._recording_preview_drop_baseline,
                ),
                recording_errors=self._recording_error_count,
                manual_heatmap_count=self._manual_heatmap_count,
                manual_line_graph_count=self._manual_line_graph_count,
            )
        else:
            self._review_generation += 1
        if not self._closing:
            self._start_live_analysis()
            self._start_motion_processing()

    def _launch_review_job(
        self,
        source: RecordingCompletion | Path,
        *,
        failure_prefix: str,
        preview_drops: int,
        recording_errors: int,
        manual_heatmap_count: int,
        manual_line_graph_count: int,
    ) -> None:
        """Run one session-scoped review whose callbacks cannot cross trials."""

        directory = Path(getattr(source, "session_directory", source)).resolve()
        self._review_generation += 1
        generation = self._review_generation

        def is_current() -> bool:
            active = self._active_session_directory
            return bool(
                generation == self._review_generation
                and active is not None
                and Path(active).resolve() == directory
            )

        def succeeded(values: object) -> None:
            if is_current() and isinstance(values, Mapping):
                self.recording_tab.set_review_summary(values)

        def failed(text: str) -> None:
            if is_current():
                self.recording_tab.set_status(f"{failure_prefix}: {text}")

        self._run_job(
            build_review_summary,
            succeeded,
            failed,
            source,
            preview_drops=int(preview_drops),
            recording_errors=int(recording_errors),
            manual_heatmap_count=int(manual_heatmap_count),
            manual_line_graph_count=int(manual_line_graph_count),
        )

    def _reset_recording_performance_metrics(self) -> None:
        """Initialize trial-scoped counters when the operator presses Start."""

        self._recording_frame_count = 0
        self._recording_error_count = 0
        self._recording_preview_drop_baseline = self.preview_buffer.dropped_count
        self.recording_display_buffer.reset()
        metrics = {
            "feature_processing_fps": "0.00",
            "video_writing_fps": "0.00",
            "dropped_preview_frames": 0,
            "recording_pipeline_errors": 0,
        }
        self.camera_tab.update_performance(metrics)
        self.recording_tab.update_performance_metrics(metrics)

    def _recording_prepare_failed(
        self, message: str, generation: int | None = None
    ) -> None:
        if generation is not None and generation != self._recording_prepare_generation:
            return
        self._recording_prepare_generation += 1
        self._preparing_recording = False
        self._recording_prepare_label_values = {}
        self._update_configuration_locks()
        self.recording_tab.set_status(f"Recording did not start: {message}")
        if self._printer_automated_recording:
            self._printer_sequence_preparation_failed(
                f"Synchronized recording preparation failed: {message}"
            )
        self._refresh_readiness()

    def _cancel_recording_preparation(self, reason: str) -> None:
        if not self._preparing_recording:
            return
        self._recording_prepare_generation += 1
        self._preparing_recording = False
        self._recording_prepare_label_values = {}
        self._update_configuration_locks()
        if reason:
            self.recording_tab.set_status(f"Recording preparation cancelled: {reason}.")
        if self._printer_automated_recording:
            self._printer_sequence_preparation_failed(
                f"Synchronized recording preparation cancelled: {reason}"
            )

    def _abort_active_recording(
        self,
        message: str,
        error_code: ErrorCode = ErrorCode.SHUTDOWN_REQUESTED,
    ) -> None:
        if self._pending_printer_sequence is not None:
            self.printer_abort_command.emit()
        worker = self._recording_thread
        if worker is not None and worker.isRunning():
            self.camera_worker.set_recording_sink(None)
            self.serial_worker.set_recording_sink(None)
            self.serial_worker.set_recording_origin(None)
            worker.request_abort(message, error_code)

    def _worker_error(self, component: str, message: str) -> None:
        self.system_status_tab.append_status(component, message)
        if component == "Camera":
            self.camera_tab.set_status(message, "error")
            self._cancel_baseline_capture(f"camera prerequisite failed: {message}")
            self._cancel_recording_preparation(
                f"camera prerequisite failed: {message}"
            )
        else:
            self.loadcell_tab.set_status(message, "error")
            self._cancel_recording_preparation(
                f"load-cell prerequisite failed: {message}"
            )
        error_code = (
            ErrorCode.CAMERA_DISCONNECTED
            if component == "Camera"
            else ErrorCode.SERIAL_DISCONNECTED
        )
        self._abort_active_recording(
            f"{component} error: {message}",
            error_code,
        )

    def _camera_source_exhausted(self) -> None:
        self.camera_tab.set_preview_state(False)
        self._cancel_baseline_capture("camera source ended")
        self._cancel_recording_preparation("camera source ended")
        self._abort_active_recording(
            "Camera source ended before Stop Recording",
            ErrorCode.CAMERA_DISCONNECTED,
        )

    def _set_lifecycle(self, lifecycle: RecordingLifecycle) -> None:
        self.lifecycle = lifecycle
        self.recording_tab.set_lifecycle(lifecycle)
        self._update_configuration_locks()

    def _update_configuration_locks(self) -> None:
        """Apply the union of baseline, preparation, and recording locks."""

        locked = self.lifecycle in {
            RecordingLifecycle.RECORDING,
            RecordingLifecycle.FINALIZING,
        }
        full_lock = locked or self._preparing_recording
        self.camera_tab.set_controls_locked(full_lock or self._baseline_locked)
        self.roi_tab.set_recording_locked(full_lock or self._baseline_locked)
        self.loadcell_tab.set_controls_locked(full_lock)
        self.recording_tab.set_recording_locked(full_lock or self._baseline_locked)
        self.motion_tab.setEnabled(not full_lock)
        self.image_processing_tab.setEnabled(not full_lock)

    def _refresh_readiness(self) -> None:
        if self._loadcell_workflow_busy():
            displayed = self.readiness.as_dict()
            displayed["calibration_verified"] = False
            self.recording_tab.set_readiness(displayed)
        else:
            self.recording_tab.set_readiness(self.readiness)

    def _run_smoke(self) -> None:
        output = self._output_directory or (Path.cwd() / "output")
        self.recording_tab.run_smoke_button.setEnabled(False)
        self.recording_tab.set_status(
            "Running the full SIMULATION smoke test off-thread; no physical hardware is claimed."
        )
        self._run_job(
            run_simulation_smoke,
            self._smoke_complete,
            self._smoke_failed,
            output,
        )

    def _smoke_complete(self, result: object) -> None:
        self.recording_tab.run_smoke_button.setEnabled(True)
        self.recording_tab.set_status(
            f"Simulation smoke passed: {getattr(result, 'frame_count')} frames; "
            f"output {getattr(result, 'session_directory')}.",
            f"Measured bottleneck: {getattr(result, 'slowest_stage')}.",
        )

    def _smoke_failed(self, message: str) -> None:
        self.recording_tab.run_smoke_button.setEnabled(True)
        self.recording_tab.set_status(f"Simulation smoke failed: {message}")

    def _replace_graph_service(self, graph_directory: Path | None = None) -> None:
        if self._graph_service is not None:
            self._graph_service.shutdown(wait=False)
        if self._output_directory is not None:
            self._graph_service = GraphExportService(
                graph_directory or self._output_directory / "manual_graphs"
            )

    def _graph_context(self) -> GraphContext:
        labels = self.recording_tab.trial_labels()
        return GraphContext(
            session_id=str(labels.get("session_id", "")),
            trial_id=str(labels.get("trial_id", "")),
            baseline_id=str(getattr(self._baseline, "baseline_id", "")),
            roi_layout_id=(
                self.roi_tab.current_layout().roi_layout_id
                if self.roi_tab.validation.valid
                else ""
            ),
        )

    def _ensure_graph_service(self) -> GraphExportService | None:
        if self._graph_service is None:
            if self._output_directory is None:
                self.recording_tab.set_status("Choose an output directory before saving a graph.")
                return None
            self._replace_graph_service()
        return self._graph_service

    def _spatial_snapshot(self, snapshot: Mapping[str, object]) -> SpatialGraphSnapshot:
        return SpatialGraphSnapshot(
            values=tuple(snapshot["values"]),
            metric=_METRIC_CODES.get(str(snapshot["metric"]), "mean_delta_v"),
            scale_min=float(snapshot["scale_min"]),
            scale_max=float(snapshot["scale_max"]),
            context=self._graph_context(),
            frame_id=snapshot.get("capture_frame_id"),
            elapsed_time_s=snapshot.get("elapsed_time_s"),
            host_monotonic_ns=snapshot.get("host_monotonic_ns"),
        )

    def _save_spatial_graph(self, snapshot: Mapping[str, object]) -> None:
        service = self._ensure_graph_service()
        if service is None:
            return
        self._track_graph_future(
            service.save_spatial_snapshot(self._spatial_snapshot(snapshot)),
            category="heatmap",
        )

    def _save_line_graph(self, snapshot: Mapping[str, object]) -> None:
        service = self._ensure_graph_service()
        if service is None:
            return
        if str(snapshot.get("view")) == "Current Spatial Profile":
            future = service.save_spatial_profile(self._spatial_snapshot(snapshot))
        else:
            history = tuple(snapshot.get("temporal_history", ()))
            if not history:
                self.recording_tab.set_status("No displayed temporal samples are available to save.")
                return
            future = service.save_temporal_lines(
                TemporalGraphSnapshot(
                    capture_frame_ids=tuple(item.capture_frame_id for item in history),
                    elapsed_time_s=tuple(item.elapsed_time_s for item in history),
                    roi_values=tuple(item.values for item in history),
                    metric=_METRIC_CODES.get(str(snapshot["metric"]), "mean_delta_v"),
                    visible_roi_ids=tuple(snapshot["visible_roi_ids"]),
                    x_axis_limits=tuple(float(value) for value in snapshot["x_axis_limits"]),
                    y_axis_limits=tuple(float(value) for value in snapshot["y_axis_limits"]),
                    display_history_s=float(snapshot["history_seconds"]),
                    context=self._graph_context(),
                )
            )
        self._track_graph_future(future, category="line")

    def _track_graph_future(self, future: object, *, category: str) -> None:
        with self._graph_futures_lock:
            self._graph_futures.add(future)

        def complete(done: object) -> None:
            try:
                artifact = done.result()
                self.graph_message.emit(f"Saved graph companions: {artifact.json_path.parent}", True)
                self.manual_graph_saved.emit(category)
            except Exception as exc:
                self.graph_message.emit(str(exc), False)
            finally:
                with self._graph_futures_lock:
                    self._graph_futures.discard(done)

        future.add_done_callback(complete)

    def _manual_graph_completed(self, category: str) -> None:
        if category == "heatmap":
            self._manual_heatmap_count += 1
            key = "manual_heatmap_count"
            value = self._manual_heatmap_count
        else:
            self._manual_line_graph_count += 1
            key = "manual_line_graph_count"
            value = self._manual_line_graph_count
        if self.lifecycle is RecordingLifecycle.COMPLETE:
            self.recording_tab.set_review_summary({key: value})

    def _graph_result_message(self, message: str, succeeded: bool) -> None:
        self.recording_tab.set_status(
            message if succeeded else f"Graph export failed: {message}"
        )

    def _choose_video_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose simulation video", "", "Video files (*.mp4 *.avi *.mov *.mkv);;All files (*)"
        )
        if path:
            self.camera_tab.set_source_path(path)

    def _choose_playback_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose serial playback", "", "CSV or text (*.csv *.txt);;All files (*)"
        )
        if path:
            self.loadcell_tab.set_playback_path(path)

    def _save_frame_snapshot(self, *, original: bool) -> None:
        if original:
            if self._latest_captured is None:
                self.system_status_tab.append_status("Snapshot", "No original frame is available.")
                return
            frame = np.array(getattr(self._latest_captured, "original_bgr"), copy=True)
            suggested = "original_frame.png"
        else:
            if self._latest_processed_bgr is None:
                self.system_status_tab.append_status("Snapshot", "No processed frame is available.")
                return
            frame = np.array(self._latest_processed_bgr, copy=True)
            suggested = "processed_frame.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save frame image", suggested, "PNG (*.png);;JPEG (*.jpg *.jpeg)"
        )
        if not path:
            return
        self._run_job(
            _write_frame_image,
            lambda saved: self.system_status_tab.append_status(
                "Snapshot", f"Saved {saved}."
            ),
            lambda message: self.system_status_tab.append_status("Snapshot", message),
            Path(path),
            frame,
        )

    def _save_motion_profile(self, config: object) -> None:
        if not isinstance(config, MotionMagnificationConfig):
            self.motion_tab.set_status("Motion profile is invalid.", error=True)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save motion-magnification profile", "motion_profile.json", "JSON (*.json)"
        )
        if not path:
            return
        try:
            config.save_json(path)
            self.motion_tab.set_status(f"Saved motion profile to {path}.")
        except Exception as exc:
            self.motion_tab.set_status(str(exc), error=True)

    def _load_motion_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load motion-magnification profile", "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            config = MotionMagnificationConfig.load_json(path)
            self.motion_tab.set_configuration(config)
            self._apply_motion_config(config)
        except Exception as exc:
            self.motion_tab.set_status(str(exc), error=True)

    def _save_roi_layout(self, layout: ROILayout) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save ROI layout", "roi_layout.json", "JSON (*.json)")
        if path:
            try:
                rotation, mirror = self.camera_tab.requested_orientation()
                saved_layout = ROILayout.create(
                    layout.rois,
                    layout.frame_width,
                    layout.frame_height,
                    rotation_degrees=rotation,
                    mirror_horizontal=mirror,
                )
                save_roi_layout(path, saved_layout)
                self.roi_tab.status_label.setText(f"Saved ROI layout to {path}.")
            except Exception as exc:
                self.roi_tab.status_label.setText(f"Could not save ROI layout: {exc}")

    def _load_roi_layout(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load ROI layout", "", "JSON (*.json)")
        if not path:
            return
        try:
            layout = load_roi_layout(path)
            saved_size = (layout.frame_width, layout.frame_height)
            current_size = (self.roi_tab.frame_width, self.roi_tab.frame_height)
            orientation_note = ""
            if layout.rotation_degrees is not None:
                target_orientation = (
                    layout.rotation_degrees,
                    bool(layout.mirror_horizontal),
                )
                if self.camera_tab.requested_orientation() != target_orientation:
                    self.camera_tab.set_orientation(*target_orientation)
                    orientation_note = (
                        " Camera orientation was restored to "
                        f"{self.camera_tab.orientation_description()}."
                    )
            elif saved_size != current_size and saved_size == current_size[::-1]:
                current_rotation, current_mirror = self.camera_tab.requested_orientation()
                self.camera_tab.set_orientation(
                    (current_rotation + 90) % 360,
                    current_mirror,
                )
                orientation_note = (
                    " Legacy layout dimensions were rotated to match the saved "
                    f"{layout.frame_width} × {layout.frame_height} view."
                )
            current_size = (self.roi_tab.frame_width, self.roi_tab.frame_height)
            if saved_size != current_size:
                raise ValueError(
                    "saved ROI frame is "
                    f"{layout.frame_width} × {layout.frame_height}, but the current "
                    f"camera view is {current_size[0]} × {current_size[1]}"
                )
            self.roi_tab.set_roi_layout(layout.rois, emit_change=True)
            self.roi_tab.status_label.setText(
                f"Loaded ROI layout from {path}.{orientation_note} "
                "The unloaded baseline must be captured again."
            )
        except Exception as exc:
            self.roi_tab.status_label.setText(f"Could not load ROI layout: {exc}")

    def _save_calibration(self) -> None:
        if self._calibration is None:
            self.loadcell_tab.set_status("No verified calibration is available to save.", "error")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save load-cell calibration", "loadcell_calibration.json", "JSON (*.json)")
        if path:
            try:
                self._calibration.save_json(path)
                self.loadcell_tab.set_status(f"Saved calibration to {path}.", "success")
            except Exception as exc:
                self.loadcell_tab.set_status(str(exc), "error")

    def _reset_calibration(self) -> None:
        self._cancel_sample_window("calibration reset")
        self._advance_loadcell_workflow_generation()
        self._cancel_recording_preparation("calibration reset")
        self._calibration_windows.clear()
        self._calibration_computation = None
        self._calibration = None
        self.readiness.loadcell_calibrated = False
        self.readiness.calibration_verified = False
        self.serial_calibration_command.emit(None)
        self.loadcell_tab.reset_calibration_progress("Calibration reset by operator.")
        self.loadcell_tab.calibration_state_label.setText("Not calibrated")
        self._refresh_readiness()

    def _save_serial_log(self, entries: object) -> None:
        rows = tuple(dict(item) for item in entries if isinstance(item, Mapping))
        if not rows:
            self.loadcell_tab.set_status("No serial diagnostics are available to save.", "warning")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save raw serial log", "raw_serial_log.txt", "Text (*.txt);;All files (*)"
        )
        if not path:
            return
        destination = Path(path)
        if destination.exists():
            self.loadcell_tab.set_status(
                f"Refusing to overwrite existing serial log: {destination}", "error"
            )
            return
        self._run_job(
            _write_serial_log,
            lambda saved: self.loadcell_tab.set_status(
                f"Saved raw serial log to {saved}.", "success"
            ),
            lambda message: self.loadcell_tab.set_status(message, "error"),
            destination,
            rows,
        )

    def _load_calibration(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load load-cell calibration", "", "JSON (*.json)")
        if not path:
            return
        try:
            calibration = LoadCellCalibration.load_json(path)
            if self._serial_info is not None:
                expected_port = str(getattr(self._serial_info, "port", ""))
                expected_baud = int(getattr(self._serial_info, "baud_rate", 0))
            else:
                request = self.loadcell_tab.requested_connection()
                expected_port = str(request.get("port") or "")
                if str(self.loadcell_tab.source_mode_combo.currentData()) != "Physical":
                    expected_port = "SIMULATED"
                expected_baud = int(request["baud_rate"])
            if not expected_port:
                raise ValueError(
                    "Select or connect the intended serial device before loading a "
                    "calibration profile so device identity can be verified."
                )
            if not self._calibration_matches_device(
                calibration, port=expected_port, baud_rate=expected_baud
            ):
                raise ValueError(
                    "Calibration profile device mismatch: "
                    f"profile={calibration.serial_port}@{calibration.baud_rate}, "
                    f"selected={expected_port}@{expected_baud}. The profile was not applied."
                )
            self._cancel_sample_window("a saved calibration was loaded")
            self._advance_loadcell_workflow_generation()
            self._cancel_recording_preparation("a saved calibration was loaded")
            self._calibration_windows.clear()
            self._calibration_computation = None
            self._calibration = calibration
            self.loadcell_tab.set_known_mass_g(calibration.known_mass_g)
            self.readiness.loadcell_calibrated = bool(calibration.quality_passed)
            self.readiness.calibration_verified = bool(
                calibration.verification is not None and calibration.verification.passed
            )
            self.loadcell_tab.update_calibration_result(calibration.to_dict())
            if calibration.verification is not None:
                self.loadcell_tab.update_verification_result(asdict(calibration.verification))
            self.serial_calibration_command.emit(calibration)
            self._refresh_readiness()
        except Exception as exc:
            self.loadcell_tab.set_status(str(exc), "error")

    @staticmethod
    def _calibration_matches_device(
        calibration: LoadCellCalibration, *, port: str, baud_rate: int
    ) -> bool:
        """Require explicit serial identity before applying signed scale evidence."""

        same_port = (
            calibration.serial_port.strip().casefold()
            == str(port).strip().casefold()
        )
        same_baud = calibration.baud_rate in {0, int(baud_rate)}
        return bool(same_port and same_baud)

    def _open_directory(self, path: str) -> None:
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))

    def _camera_is_simulation(self) -> bool:
        return bool(self._camera_info is not None and getattr(self._camera_info, "simulation_mode", False))

    def _serial_is_simulation(self) -> bool:
        return bool(self._serial_info is not None and getattr(self._serial_info, "simulation_mode", False))

    def _run_job(
        self,
        function: Callable[..., object],
        succeeded: Callable[[object], None],
        failed: Callable[[str], None],
        *args: object,
        **kwargs: object,
    ) -> None:
        worker = FunctionThread(function, *args, parent=self, **kwargs)
        self._jobs.add(worker)
        worker.succeeded.connect(succeeded)
        worker.failed.connect(failed)
        worker.finished.connect(lambda: self._jobs.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._closing:
            event.ignore()
            return
        with self._graph_futures_lock:
            pending_graphs = tuple(
                future
                for future in self._graph_futures
                if not bool(getattr(future, "done", lambda: False)())
            )
        if pending_graphs:
            self.recording_tab.set_status(
                "Graph exports are still finishing; close again after they complete."
            )
            event.ignore()
            return
        self._closing = True
        self.preview_timer.stop()
        self.force_timer.stop()
        self.graph_timer.stop()
        self.status_timer.stop()

        self._abort_active_recording(
            "Application shutdown requested",
            ErrorCode.SHUTDOWN_REQUESTED,
        )
        recording_worker = self._recording_thread
        if (
            recording_worker is not None
            and recording_worker.isRunning()
            and not recording_worker.wait(self._shutdown_worker_wait_ms)
        ):
            self._refuse_close_while_worker_runs(
                event,
                "Recording is still flushing recoverable partial data; close again after it finishes.",
            )
            return

        for job in tuple(self._jobs):
            if job.isRunning() and not job.wait(self._shutdown_worker_wait_ms):
                self._refuse_close_while_worker_runs(
                    event,
                    "A background calculation is still finishing; close again after it stops.",
                )
                return

        if not self._stop_motion_processing(self._shutdown_owner_wait_ms):
            self._refuse_close_while_worker_runs(
                event,
                "Motion-magnification processing is still stopping; close again shortly.",
            )
            return

        if not self._stop_live_analysis(self._shutdown_owner_wait_ms):
            self._refuse_close_while_worker_runs(
                event,
                "Live optical analysis is still stopping; close again after it finishes.",
            )
            return

        if self.camera_thread.isRunning():
            if not self._disconnect_owner_thread(
                self.camera_worker,
                "disconnect_camera",
                self.camera_worker.disconnected,
                self.camera_thread,
            ):
                self._refuse_close_while_worker_runs(
                    event,
                    "The camera owner is still releasing the device; close again after it stops.",
                )
                return
        if self.serial_thread.isRunning():
            if not self._disconnect_owner_thread(
                self.serial_worker,
                "disconnect_serial",
                self.serial_worker.disconnected,
                self.serial_thread,
            ):
                self._refuse_close_while_worker_runs(
                    event,
                    "The serial owner is still releasing the port; close again after it stops.",
                )
                return
        if self.printer_thread.isRunning():
            if not self._disconnect_owner_thread(
                self.printer_worker,
                "disconnect_printer",
                self.printer_worker.disconnected,
                self.printer_thread,
            ):
                self._refuse_close_while_worker_runs(
                    event,
                    "The printer owner is still cancelling motion or releasing COM4; close again shortly.",
                )
                return
        if self._graph_service is not None:
            self._graph_service.shutdown(wait=False)
        event.accept()

    def _disconnect_owner_thread(
        self,
        worker: object,
        method_name: str,
        disconnected_signal: object,
        thread: QThread,
    ) -> bool:
        """Request owner-thread cleanup and wait through a bounded nested loop."""

        timeout_ms = max(1, int(self._shutdown_owner_wait_ms))
        started_ns = time.perf_counter_ns()
        loop = QEventLoop()
        timeout = QTimer(loop)
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        disconnected_signal.connect(
            loop.quit,
            Qt.ConnectionType.QueuedConnection,
        )
        try:
            invoked = bool(
                QMetaObject.invokeMethod(
                    worker,
                    method_name,
                    Qt.ConnectionType.QueuedConnection,
                )
            )
            if not invoked:
                return False
            timeout.start(timeout_ms)
            loop.exec()
        finally:
            timeout.stop()
            with suppress(RuntimeError, TypeError):
                disconnected_signal.disconnect(loop.quit)

        service_released = getattr(worker, "_service", None) is None
        elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
        remaining_ms = max(0, round(timeout_ms - elapsed_ms))
        thread.quit()
        return service_released and bool(thread.wait(remaining_ms))

    def _refuse_close_while_worker_runs(
        self, event: QCloseEvent, message: str
    ) -> None:
        """Keep QObject/QThread ownership alive until critical work has stopped."""

        self._closing = False
        for timer in (
            self.preview_timer,
            self.force_timer,
            self.graph_timer,
            self.status_timer,
        ):
            timer.start()
        self.recording_tab.set_status(message)
        event.ignore()


__all__ = ["MainWindow", "build_review_summary"]
