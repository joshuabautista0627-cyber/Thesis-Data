# SPARE Camera and Load-Cell Calibration GUI

## Purpose

This standalone Windows application records either one manual fixed-label sensing-skin press or a synchronized repeated-press sequence driven by a Creality Ender 3. It combines an unannotated camera stream, an Arduino Nano/HX711 load-cell stream, and an independent Marlin printer connection with nine fixed regions of interest (ROIs), an unloaded optical baseline, signed force calibration, monotonic-time synchronization, optional live Eulerian color/intensity magnification, live heatmaps/line graphs, force-limited motion, and reproducible video/PNG/CSV/JSON exports.

Printer motion is isolated from the load-cell serial owner: the default Ender 3 connection is COM4 at 115200 while the Nano/HX711 remains on COM3. Simulation sources are clearly labeled and exercise camera/load-cell processing, synchronization, recording, and export paths without claiming physical printer motion.

The target camera was physically confirmed as an **Arducam IMX179 Camera Module**, not a GXIVISION device. Arduino Nano/HX711 upload, cadence, fixture-aware 200 g calibration, concurrent USB acquisition, and a complete physical session were exercised on 2026-08-03. See [PHYSICAL_HARDWARE_VALIDATION_20260803.md](PHYSICAL_HARDWARE_VALIDATION_20260803.md) for measured results and remaining limitations, and reuse [HARDWARE_TEST_CHECKLIST.md](HARDWARE_TEST_CHECKLIST.md) after any hardware, driver, port, wiring, or fixture change.

The former mixed live-sensor plan is now a [routing index](LIVE_SENSOR_GUI_MASTER_PLAN.md). Two independent documents are authoritative:

- [LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md](LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md) governs the manual-only force/localization model, live GUI, connected USB sensor integration, and application validation.
- [SENSOR_CHARACTERIZATION_MASTER_PLAN.md](SENSOR_CHARACTERIZATION_MASTER_PLAN.md) governs static and cyclic sensor-property analysis using the manual and automatic calibration archives in separate evidence roles.

Neither track blocks or authorizes the other, and characterization artifacts must not supply application-model thresholds, preprocessing, selection, or force range.

## Folder structure

| Path | Purpose |
|---|---|
| `app.py` | Windows/Python GUI entry point |
| `config/default_config.json` | Validated default camera, processing, load-cell, recording, and display settings |
| `core/` | Configuration models, readiness flags, lifecycle/error codes, and logging helpers |
| `services/` | Single-owner camera, load-cell serial, independent printer serial, repeated-press, simulation, Qt worker, and session-recorder services |
| `processing/` | ROI rules, baseline, HSV features, motion magnification, localization, calibration, synchronization, and pipeline |
| `data/` | Canonical schemas, incremental CSV export, and graph export/finalization |
| `gui/` | Dual original/processed video area, nine control tabs including Printer Motion, Help, readiness/review UI, heatmap, line graphs, and force plot |
| `arduino/hx711_nano_stream/` | Arduino Nano firmware |
| `scripts/` | Synthetic-video generator, full smoke test, performance profile, and reusable physical calibration/session audit probes |
| `tests/` | Hardware-free automated tests, including Qt offscreen GUI tests |
| `output/` | Default session output root; each run creates a new non-overwriting directory |

## Install Python and create the virtual environment

Use 64-bit Python 3.11 or later on Windows. During Python installation, enable **Add Python to PATH** and install the `py` launcher if offered.

Open PowerShell in this project directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
```

If PowerShell blocks activation, the interpreter can still be called directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The equivalent guided setup is:

```bat
setup_windows.bat
```

## Launch the application

Using Python:

```powershell
.\.venv\Scripts\python.exe app.py
```

Start with the clearly labeled synthetic camera and HX711 sources preselected:

```powershell
.\.venv\Scripts\python.exe app.py --simulation
```

Using the Windows launcher:

```bat
run_windows.bat
run_windows.bat --simulation
```

The main splitter keeps the unannotated, software-oriented **Original Frame** above the **Processed Frame** on the left and gives the larger share of width to video. The scrollable control area on the right has nine tabs: **Camera View**, **ROI and Baseline Settings**, **Image and HSV Processing**, **Motion Magnification**, **Load Cell and Calibration**, **Recording and Synchronization**, **Printer Motion**, **Graph and Export Settings**, and **System Status and Logs**. The Help button remains available throughout the workflow.

The original display is refreshed from the camera's newest-only preview buffer independently of slower processing. ROI overlays are optional display copies and never modify the authoritative frame. Each display has its own image-save button. The processed selector includes background-subtracted, HSV/intensity, spatial-intensity, motion-color, motion-intensity, and raw comparison views.

## Arduino Nano, HX711, and load-cell setup

### Install the Arduino library

1. Install Arduino IDE 2.x.
2. Open **Tools > Manage Libraries**.
3. Search for `HX711` and install the HX711 library by Bogdan Necula/Bogde that provides `#include <HX711.h>`, `begin()`, `is_ready()`, and `read()`.
4. Open `arduino/hx711_nano_stream/hx711_nano_stream.ino`.

### Wiring

Disconnect USB power while wiring.

| HX711 module | Arduino Nano |
|---|---|
| `DT` or `DOUT` | Digital pin D4 |
| `SCK` or `CLK` | Digital pin D5 |
| `VCC` | Module-rated supply, normally Nano 5 V for common HX711 boards |
| `GND` | GND |

Connect the load cell to the HX711 excitation and signal terminals (`E+`, `E-`, `A+`, `A-`) according to the load-cell and module datasheets. Wire colors are not standardized; do not infer polarity from color alone. Channel B is not used.

### Upload the sketch

1. Connect the Nano by USB.
2. In Arduino IDE select **Tools > Board > Arduino Nano** and the correct processor/bootloader variant.
3. Select the Nano COM port.
4. Verify and upload `hx711_nano_stream.ino`.
5. Close Serial Monitor before connecting from the GUI; only one process may own the port.
6. In the GUI select the same COM port and `115200` baud.

### Serial protocol

The supplied sketch starts streaming immediately at **115200 baud** and emits one complete ASCII measurement line per fresh HX711 conversion:

```text
HELLO,HX711_NANO,1.0,1.0.0
DATA,sample_id,arduino_micros,raw_adc
OK,START
OK,STOP
PONG,HX711_NANO
STATUS,STREAMING|STOPPED,READY|NOT_READY,sample_id
ERROR,HX711_NOT_READY
ERROR,HX711_TIMEOUT
```

`HELLO` is optional identity information, not sensor validation. The GUI validates a connection only after the port opens and the configured number of finite numeric readings arrive. It supports the supplied four-field `DATA` format and generic streams containing plain signed integer/float values, `RAW:<value>`, `raw=<value>`, `DATA,<millis>,<raw>`, or `<millis>,<raw>`. Select a fixed parser in the GUI if auto-detection is inappropriate. Startup text and malformed lines are logged and rejected without being counted as readings.

Supported optional host commands are `PING`, `START`, `STOP`, and `STATUS`, each terminated by a newline. They are used only when a genuine `HELLO,HX711_NANO,...` protocol is detected. A generic numeric source is never sent protocol commands. Opening a Nano serial port normally resets the board, so the configurable startup delay defaults to two seconds; stale input is cleared before new lines are accepted. A midstream sample-ID reset/HELLO starts a new device session while the trial's host-monotonic elapsed-time origin remains unchanged. The firmware calls `read()` only after `is_ready()`, so it emits only new HX711 conversions.

The Raw Serial Monitor shows received lines, parsed values, failures, counts, most recent value, and rate. **Pause Display** does not pause acquisition. Do not leave Arduino Serial Monitor/Plotter or another application connected to the same COM port; the GUI deliberately has one serial owner and reports likely port conflicts.

## Arducam IMX179 camera setup

1. Connect the camera directly to the Windows computer and allow its driver to install.
2. Open **Camera View** and click **Refresh Cameras**.
3. Select a device index. OpenCV often cannot expose a USB model name, so identify the Arducam IMX179 from the preview and Windows Device Manager. The verified unit reported `Arducam IMX179 Camera Module`, USB VID:PID `1BCF:0B12`.
4. Try **DirectShow** first. Disconnect before changing backend. If it cannot open or stream fresh frames, try **Media Foundation**.
5. Connect and confirm the displayed stream width, height, reported FPS, device index, and backend.
6. The GUI automatically reads the current values exposed by the active camera driver. With DirectShow, use **Open Windows Camera Properties** to adjust exposure, focus, white balance, gain, and other controls on the exact camera handle used by the GUI. Close the dialog and review **Current values used by GUI**.
7. If needed, choose **Rotate clockwise** (0°, 90°, 180°, or 270°) and/or **Mirror horizontally**. Rotation is applied first, followed by mirroring.
8. Start Preview and inspect capture FPS, preview FPS, dropped preview copies, and capture-to-display latency.

### DirectShow and Media Foundation troubleshooting

- If no index opens, close Windows Camera, browsers, conferencing software, and any other process that may own the camera, then Refresh Cameras again.
- If one backend fails, disconnect and try the other backend.
- If the requested resolution/FPS is not confirmed, choose a mode supported by the driver. Recording remains blocked when the mode is unconfirmed.
- If FPS readback is missing or implausible, verify the actual cadence in the performance panel and test both backends.
- Avoid USB hubs when preview is unstable. Try another cable/port and disable USB selective suspend for a controlled hardware test.
- A generic camera index is not proof that the Arducam IMX179 was identified; confirm its PnP identity and a safe preview.

On the verified unit, DirectShow at 640×480 with a requested 30 FPS produced a stable measured cadence of about 14.985 FPS over 60 seconds even though the driver property readback reported 30 FPS. Media Foundation initially produced about 30 FPS but later showed recovery/open instability after native-dialog testing, so DirectShow is the retained experimental backend. Always treat measured cadence as authoritative over the nominal FPS property.

### Driver settings, camera orientation, and baseline behavior

The GUI does not silently overwrite camera properties during connection, baseline capture, or recording preparation. It reads supported current driver values and records them as provenance. The **Open Windows Camera Properties** button is available for a physical DirectShow connection and opens the native driver dialog on the GUI's active capture handle; changes there therefore affect this GUI stream. After the dialog closes, the GUI rereads the values, restarts camera warm-up, clears stale preview/processing data, and invalidates the optical baseline.

Windows Camera app filters and Windows Studio Effects can be application- or pipeline-specific and are not exposed through OpenCV, so they cannot be truthfully imported into a DirectShow stream. If an adjustment is not shown under **Current values used by GUI**, make it through the GUI's native DirectShow dialog instead. Media Foundation can read values exposed by its backend but cannot open the DirectShow native dialog.

**Original Frame** displays the unannotated camera image after the selected software rotation/mirroring, with no ROI overlay or analysis. The same oriented pixels feed ROI analysis, baseline capture, and saved video so the preview and recorded data stay aligned. Stream identity and reported dimensions/FPS remain informational provenance only.

Changing orientation clears stale preview data, invalidates the optical baseline, and updates the ROI frame dimensions. A 90° or 270° rotation swaps width and height, so review or redefine the nine ROIs before capturing a new baseline. **Save ROI Layout JSON** now records rotation and mirror state, and **Load ROI Layout JSON** restores that view before repopulating all nine ROI editors. Older layouts without orientation metadata remain supported; when their saved dimensions are swapped, the loader applies the matching quarter-turn automatically. Loading any layout invalidates the optical baseline, so capture it again before recording. The selected rotation and mirror state are also included in session metadata.

Unloaded-baseline capture requires only a connected live preview, exactly nine valid ROIs, fresh post-connect frames, and the normal camera warm-up. It never waits for or requires camera-property readback. Color motion magnification remains available in the separate **Motion Magnification** tab and affects only the processed preview/requested auxiliary output—not the raw Original Frame.

## Operating workflow

### 1. Define exactly nine ROIs

In **ROI and Baseline**, draw or numerically edit nine rectangles. Their fixed row-major order is:

```text
ROI 1  ROI 2  ROI 3
ROI 4  ROI 5  ROI 6
ROI 7  ROI 8  ROI 9
```

Every ROI must be inside the current frame. Overlap is reported as a warning. Save/load uses a validated JSON layout. Preview boxes and labels are display-only; they are never drawn into the recorded video.

### 2. Capture and accept the unloaded baseline

Remove all load, allow camera warm-up to complete, and capture the default three-second baseline with at least 20 valid frames. Inspect the result, then Accept or Repeat. The baseline stores its camera, confirmed control, processing, resolution, and ROI-layout context.

Changing the camera connection/mode/control request, orientation, resolution, processing settings, or ROI layout invalidates the baseline. Immediately before recording, the current unloaded frame is checked against the accepted baseline; mean absolute V-channel drift above the default threshold of 5 requires recapture.

### 3. Perform the 200 g calibration and verification

In **Load Cell and Calibration**:

1. Connect and wait for a healthy fresh `DATA` stream.
2. Remove all load and capture the unloaded averaging window.
3. Place the known 200 g mass and capture the loaded window.
4. Calculate the signed counts-per-gram factor. The window must satisfy sample-count, calibration SNR (default minimum 10), and loaded-window CV (default maximum 2%) rules.
5. Remove the mass and run Tare. Tare changes the zero only; it does not change counts per gram.
6. Replace the same 200 g mass and Verify. The default tolerance is ±5%.

Mass edits, a new unloaded capture, disconnect, unhealthy stream, device-session change, retare, or loaded-calibration change invalidate dependent work. Sample windows and background calculations are atomic and block Recording Start until a current verification passes.

Saved calibration JSON includes signed direction, window statistics, timestamps, COM port, baud rate, firmware/protocol identity, and verification evidence. A loaded profile is applied only when its saved port and baud rate match the selected/connected source; a mismatch is reported and the profile is not silently used.

### 4. Configure optional motion magnification

Motion magnification is off by default and runs in a separate drop-stale worker. Choose color or intensity-only output, amplification, temporal band in Hz, chrominance gain, pyramid depth, `lambda_c`, explicit/automatic processing resolution, downscale, target FPS, and ROI-only processing. **Apply Parameters** validates the current camera/processing FPS Nyquist limit and effective pyramid size; incompatible changes rebuild temporal state. **Reset Temporal Filter**, **Restore Defaults**, **Save Profile**, and **Load Profile** are functional.

The implementation modernizes the uploaded reference with OpenCV Laplacian pyramids, timestamp-aware one-pole temporal filters, safe state copies, `time.perf_counter_ns()`, bounded newest-only queues, validated `**` exponentiation, and no obsolete `time.clock()` or `pyrtools` dependency. If processing falls behind, stale magnification frames are dropped without blocking original preview, camera capture, serial acquisition, or original video recording.

The **Force-light HSV source** defaults to **Background-subtracted frame**. Enabling **Color magnification** automatically selects **Motion-magnified frame**, shows the magnified color preview, and routes the live ROI H/S/V and baseline-corrected delta-V measurements through those exact magnified pixels. The graph switches to **Temporal ROI Lines** with **Mean HSV V (0-255)** so the source used for force-light calibration is explicit. Magnification changes pixel values, so calibrate and record with the same source; a calibration made from original pixels is not interchangeable. The choice and every magnification parameter are saved in session metadata, including whether magnified pixels were used for exported measurements.

If a UVC driver reports nominal FPS as zero, Apply Parameters uses the measured live capture rate when available and otherwise starts with the configured motion-processing target. Timestamp-aware Nyquist validation continues while frames are processed, so a missing driver FPS value no longer produces a false **measured processing FPS is unavailable** error.

### 5. Record one manual single-press trial

1. Enter Session ID, Trial ID, sensing-skin ID, target ROI ground truth (1–9), and any optional fixed trial labels.
2. Choose an output directory and run preflight.
3. Optionally select independent overlay, processed, or motion-magnified video streams. Original unannotated video is always required. Motion video requires magnification to be enabled.
4. Confirm all ten readiness items are green. Camera-property confirmation is not a readiness item.
5. Click **Start Recording** once.
6. Perform exactly one intentional press.
7. Click the always-visible **Stop Recording** button.
8. Wait for finalization and review. Use **Open Output Directory** after the review is populated.

Trial interaction/force classes are fixed ground-truth metadata. They are not frame-level contact labels. Frame contact is derived only from synchronized force and is missing when valid synchronized force is unavailable.

## Heatmaps and line graphs

Use the graph selector on the ROI or Recording tab to switch among:

- **Heatmap**: the exact current nine values in a 3×3 layout.
- **Temporal ROI Lines**: bounded live history for ROI 1–9 plotted against authoritative elapsed recording time.
- **Current Spatial Profile**: the same exact nine current values plotted against ROI number 1–9.

**Save Spatial Graph** exports the currently displayed heatmap. **Save Line Graph** exports the exact current temporal range or current spatial profile. Each save runs outside the GUI/acquisition thread and produces a PNG plus CSV and JSON companions.

Finalization automatically produces:

- `trial_peak_mean_delta_v.*`: heatmap at the frame with the largest ROI mean delta-V response.
- `trial_mean_contact_mean_delta_v.*`: per-ROI mean over frames with valid derived contact; marked unavailable rather than fabricated when no contact exists.
- `trial_integrated_delta_v.*`: time-integrated per-ROI delta-V response.
- `trial_roi_intensity_over_time.*`: complete-trial, full-resolution ROI 1–9 temporal lines.
- `trial_peak_spatial_profile.*`: ROI 1–9 profile from the same peak frame as the peak heatmap.

The `*` above means `.png`, `.csv`, and `.json`. PNGs provide axes/labels/legend or color bar; CSVs preserve exact values and order; JSON stores graph type, metric, limits, timestamps/frame identity, labels, and provenance.

## Session output files

A clean session contains:

The non-overwriting parent directory carries the sanitized session identifier and UTC creation timestamp; each stream filename identifies its content type. Together, the full path provides session, stream, and timestamp identity.

| File | Meaning |
|---|---|
| `session_video.mp4` or `session_video.avi` | Unannotated experimental frames; AVI/MJPG is the fallback when MP4/mp4v cannot open |
| `session_original_overlays.*` | Optional frame-aligned original stream with display ROI overlays |
| `session_processed.*` | Optional frame-aligned selected processed stream |
| `session_motion_magnified.*` | Optional frame-aligned magnified stream |
| `frame_features.csv` | One optical feature row for every accepted frame, including explicit error rows |
| `loadcell_raw.csv` | Every accepted raw load-cell sample and calibrated force when available |
| `master_synchronized.csv` | Final one-row-per-frame dataset with synchronized force/contact fields |
| `session_config.json` | Validated configuration, labels, runtime versions, identities, drift result, and graph summary |
| `camera_settings.json` | Requested mode/control evidence and confirmed actual camera evidence |
| `roi_layout.json` | Exact nine-ROI geometry and layout ID |
| `loadcell_calibration.json` | Signed factor, tare, window quality, identities, timestamps, and verification |
| `baseline_summary.json` / `baseline_data.npz` | Baseline metadata/statistics and per-pixel arrays |
| `data_dictionary.csv` | Artifact-scoped description, unit, type, value role, and missing behavior for every CSV column |
| `application.log` | Structured session log |
| `session_status.json` | Lifecycle, counts, error codes, filenames, artifacts, and complete/partial state |
| `spatial_graphs/` | Automatic and manually saved PNG/CSV/JSON graph bundles |
| `printer_sequence.json` | Automated sequence result, state events, cycle counts, force trip, timestamps, and command lifecycle log; present for repeated-printer sessions |

Raw `.partial` CSV/video files are created incrementally during recording. Only after video/frame identity and the generated master CSV validate does finalization atomically publish clean filenames and mark the session `complete`.

## Ender 3 repeated-press workflow

### Independent connection and identity

1. Keep the Arduino Nano/HX711 on **COM3**. Close Arduino Serial Monitor and any other COM-port owner.
2. Connect the Ender 3 USB serial interface and open **Printer Motion**. The default is **COM4 at 115200**, but both fields are editable.
3. Refresh ports and select the printer port. The GUI refuses to use the same physical port for the printer and load cell.
4. Click **Connect and Verify M115**. Opening the port waits for the configured startup delay, sends only `M115`, and enables motion only when the response identifies Marlin. Connection never homes or moves an axis.
5. Switch on the Ender 3 main PSU, check fixture/build-volume clearance, then explicitly click **Home All Axes** and approve the warning. USB can power enough electronics for serial replies while still being unable to drive the motors. Homing uses `G28`, waits with `M400`, and queries `M114`.

The printer service owns one pyserial handle. One dedicated writer serializes commands from jogging, automation, recovery, and emergency requests. Each normal command has a UUID and queued/sent/acknowledged/completed, rejected, timed-out, failed, or cancelled lifecycle evidence. The reader handles `ok`, `error`, temperatures, position reports, and unsolicited lines without blocking the GUI.

### Position, jogging, and press zero

- Query `M114` before trusting the reported position. `printer_reported_x/y/z_mm` are populated only from an actual parsed response; the application never fabricates them from a request.
- Jog X/Y/Z with 10, 1, 0.1, or 0.01 mm steps. XY and Z feed rates are independent. Every target is checked against configured axis bounds before any `G1` is queued. If homing reports an X/Y offset just outside the configured printable range, only recovery jogs toward the safe range are allowed; movement farther outward remains blocked.
- **Set Press Zero Here** stores the current machine X, Y, and Z only in memory after validating all three coordinates. It never sends `G92`, is never saved in a profile, and is invalidated by disconnect/reconnect, homing, communication loss, invalid position, or emergency stop.
- A visible **Printer action** status remains busy until the complete command chain finishes. Home, query, jog, zero, connect, and disconnect cannot overlap. A timed-out motion requests `M410`, invalidates position/zero, and blocks further motion until deliberate printer recovery, reconnect, and rehoming.
- The press target is always `press_zero_z_mm + signed_displacement_mm`. In the validated physical setup a negative displacement is intended to move down; the operator must reconfirm that direction every run.

### Preview, test, and repeated acquisition

Configure cycle count, signed displacement, down/up feed rates, bottom hold, top dwell, pre/post-roll, and a calibrated force limit. Optional pre-sequence tare and fresh optical baseline are executed before recorder startup. A tare preserves the scale factor but truthfully invalidates the prior known-mass verification; automated session metadata records the verification state at start.

**Preview Computed Motion** shows press zero, absolute target, cycles, estimated duration, and force limit without moving. **Test One Cycle** uses the same interlocks and synchronized recording path as a full run. Start is blocked unless camera preview, valid nine-ROI baseline, healthy calibrated load cell, labels, output/codec/disk preflight, verified Marlin connection, homing, valid position, press zero, direction confirmation, axis limits, feed limits, and force limit are valid.

For an automated trial, enter the labels in **Recording and Synchronization**, but do **not** press **Start Recording** first. Press **Start Repeated Press** in **Printer Motion**; it creates a new synchronized recording transaction, waits for the recorder to report that it has started, captures the configured pre-roll, and only then queues printer motion. A completed or failed prior trial is normalized automatically. If a manual recording is active, stop it and wait for finalization before starting repeated press.

Each cycle uses exactly one continuous absolute `G1` downward move and one continuous `G1` upward move, with `M400` synchronization. Timing is:

1. recording pre-roll;
2. move/confirm safe start;
3. downward `G1`;
4. bottom hold;
5. upward `G1` to press zero;
6. inter-cycle top dwell;
7. repeat;
8. recording post-roll and finalization.

Pause is honored after the current cycle retracts safely. Stop and Abort interrupt normal motion with `M410` through the same sole writer and attempt a bounded return to the valid press zero. The large red Emergency Stop asks that writer to send `M112` ahead of normal work and invalidates all position/zero state; the printer may require a physical reset afterward.

### Force safety and latency

Every calibrated COM3 sample is checked live using the signed Newton value. The configured threshold is inclusive and uses absolute magnitude so a wiring/sign reversal cannot evade protection. A trip timestamps the event, requests `M410` through the COM4 writer, cancels the sequence, and attempts a retract. The response is bounded by HX711 conversion/USB receipt cadence, worker scheduling, the writer's 10 ms emergency poll, printer firmware processing, and mechanics. It is a secondary software limit—not a certified interlock. Use conservative displacement/feed/force settings and physical guarding.

### Motion provenance

`frame_features.csv`, `master_synchronized.csv`, and `loadcell_raw.csv` carry the acquisition-time sequence ID/generation/status, phase and phase timestamp, cycle ID/index/count, command ID, press zero, target displacement and absolute machine Z, commanded XYZ/feed, parsed reported XYZ and validity, force limit/trip, pause, and abort fields. Motion state is snapshotted when each camera frame or load-cell sample enters the bounded recorder queue, preventing later phase changes from rewriting history.

Printer profiles store safe limits/settings only. Active press zero and direction confirmation are deliberately excluded. `session_config.json` stores the immutable connection/settings snapshot and coordinate/latency policies; `printer_sequence.json` stores the final state/event/command audit.

### Cautious physical smoke test

The read-only helper enumerates both ports, opens only COM4, sends `M115` and `M114`, asserts that no motion command was emitted, and saves explicit claim scope:

```powershell
.\.venv\Scripts\python.exe -m scripts.printer_hardware_smoke `
  --printer-port COM4 --loadcell-port COM3 `
  --output output\printer_read_only_smoke.json
```

This does not validate homing, axis direction, displacement, fixture clearance, force abort, camera synchronization, or repeated motion. Perform those checks interactively with the checklist below and record actual evidence.

After an operator confirms main power, clearance, and physical attendance, the opt-in small-motion helper performs `G28`, Z +1.0 mm, X +1.0 mm, Y +1.0 mm, Z −0.1 mm, and Z +0.1 mm back, recording every response:

```powershell
.\.venv\Scripts\python.exe scripts\printer_motion_smoke.py `
  --printer-port COM4 --loadcell-port COM3 `
  --output output\printer_motion_smoke.json --confirm-clear
```

The operator must still confirm the observed directions. On the connected Ender-3 V2 Neo, `M115` reported `Cap:EMERGENCY_PARSER:0`; therefore software `M410`/`M112` cannot be treated as a guaranteed mid-command physical stop. Stay at the machine with access to the power switch.

## CSV schema, units, and conventions

Treat `data_dictionary.csv` as authoritative. Its key is `(artifact_scope, column_name)` because a shared name can have different meanings in `frame_features.csv`, `master_synchronized.csv`, `loadcell_raw.csv`, a spatial companion, or a temporal companion.

Important units and conventions:

- `host_monotonic_ns`: integer nanoseconds from the host monotonic clock; its artifact-specific capture/receipt meaning is in the dictionary.
- `elapsed_time_s`: seconds relative to the fixed recording origin.
- `wall_clock_iso`: ISO-8601 audit timestamp; never the synchronization authority.
- `raw_adc`: original signed finite HX711 reading, preserved even before calibration.
- `tared_raw`: raw reading minus the applied zero offset; missing before calibration.
- `mass_g`: signed mass in grams; numerically equal to gram-force under the standard-gravity conversion used here.
- `force_gf`: signed gram-force; `force_N = force_gf × 0.00980665`.
- `calibration_factor_counts_per_gram`, `zero_offset_raw`, `serial_port`, and `baud_rate`: exact sample-level calibration/device provenance.
- `synchronization_offset_ms`: signed camera-frame host timestamp minus nearest physical serial-sample host timestamp. `nearest_sample_gap_ms` is its absolute magnitude.
- `counts_per_gram = (loaded_mean_raw - unloaded_mean_raw) / known_mass_g`; it may be negative. Applying the known mass should yield positive force because both ADC difference and factor carry the same sign.
- `delta_v_mean`, `delta_v_sum`, and `active_fraction`: baseline-corrected optical-response measures.
- Missing numeric values are written as `NaN` according to the dictionary; missing values are not fabricated as zero.

OpenCV converts unannotated uint8 BGR frames with `cv2.COLOR_BGR2HSV`. OpenCV hue is 0–179; saturation and value are 0–255. Hue is treated as unavailable for pixels below the configured saturation threshold (default 10). Positive delta V is `max(current V - baseline V, 0)`.

Image coordinates use origin `(0, 0)` at the full-frame upper-left, x increasing right and y increasing down. ROI x/y are upper-left coordinates. ROI-local centroids are translated back to full-frame coordinates by adding the ROI origin.

## Synchronization limitations

This is software synchronization, not a hardware trigger. Camera capture and serial receipt are timestamped with the same host monotonic clock, but USB, driver, buffering, and scheduling latency remain. Force is linearly interpolated when valid samples bracket a frame, otherwise the nearest valid sample is used only within the configured maximum gap (default 200 ms). Beyond that gap, force/contact fields are `NaN`. Arduino `micros()` rollover and sample/device-session resets are tracked, but the Arduino clock is not substituted for the authoritative host monotonic timestamp. Measure end-to-end latency with the real hardware before scientific use.

## Partial-session recovery

On disconnect, queue saturation, low disk space, writer failure, HX711 timeout, or shutdown, the recorder stops accepting new data, closes recoverable handles, preserves `.partial` artifacts, logs a specific error code, and writes `session_status.json` with `partial: true`. The Review screen reads the closed partial feature/load CSVs and shows available counts/statistics plus the output-directory button.

Do not rename, overwrite, or delete a partial directory before inspection/back-up. A partial trial is not resumed or silently promoted to complete; correct the fault and record a new single-press trial. `master_synchronized.csv` and automatic summaries are complete-session finalization products and may be unavailable for a partial.

## Experimental live optical sensor recovery

The **Live Sensor (Experimental)** tab is available in the normal application. Deployment is pure camera plus mechanoluminescent skin: force estimation and ROI localization use only camera-derived signed-light and active-fraction features. A load cell, printer, and their calibrations are neither required nor read. The model uses only the allowlisted manual archive and a reviewed NumPy/JSON bundle; it never reads pickle/joblib.

Start the GUI:

```powershell
.\.venv\Scripts\python.exe app.py
```

For a hardware-free check, open **Live Sensor (Experimental)** and click **Run bundled replay demo**. For a live camera, use the canonical nine-ROI layout, capture and accept an unloaded baseline, keep all ROIs unloaded, and click **Calibrate while unloaded**. After the 120-frame warm-up, v4 combines v3 event detection/localization with v2's monotonic approximate force. Every rod is checked independently against a conservative unloaded-normalized activation gate with two-frame acquire/release hysteresis, so simultaneous rods light multiple ROI boxes. The dominant single-ROI path remains as a confidence-gated fallback. Newton force is withheld whenever multiple rods are active because the archived force model contains only single presses. Out-of-support force and low-confidence ROI show `—`. The estimate is limited to the archived 1.7–3.0 N band, force resolution was not established, and the value is not physically validated. The v1/v2/v3 bundles remain loadable rollback artifacts but are not the GUI default.

Completed optical presses are retained in memory for the current app session even if the estimate is reset or recalibrated. Use **Export post-processing report** to create a new self-contained report directory containing `post_processing_report.html`, `event_details.csv`, `roi_summary.csv`, and `report.json`. The report stores every detected/reported ROI and counts each rod in a simultaneous event. It records peak raw HSV V (0–255), peak baseline-corrected ΔV, the approximate force on the exact frame of each V peak, the maximum approximate force anywhere in a single-press event, event duration in frames, and force availability. Missing, out-of-support, and multi-press force is left blank rather than imputed. **Clear captured events** starts a new in-memory report history and does not delete reports already exported.

The recovery bundle is not a validated release. Its six folds were reused after the original frozen gate failed, the authoritative all-ROI common safe range remains unsupported, and no connected known-force, soak, or representative-user validation was performed. The expanded grouped force search (isotonic, ridge, elastic-net, Extra Trees, and histogram gradient boosting) did not produce a material stable improvement, while v3 accumulated argmax outperformed logistic regression, shrinkage LDA, and Extra Trees for event ROI. Replaying the combined independent-gate/fallback rule on 102 archived single-press events produced 100% session-balanced exact-set accuracy when displayed at 83.08% coverage, clearing a 99% selective-accuracy target for this reused single-press evidence. The archive has zero labeled simultaneous-press events, so multi-press accuracy remains unmeasured; run `python -m scripts.benchmark_multi_roi_localization` to reproduce this boundary. See `analysis_outputs/live_sensor_manual_only/model_runs/hybrid_candidate_benchmark.json`, `analysis_outputs/live_sensor_manual_only/model_runs/event_localization_candidate_benchmark.json`, `analysis_outputs/live_sensor_manual_only/LITERATURE_SOFTWARE_METHOD_AUDIT.csv`, and the v4 bundle's `release_decision.json`.

Offline manual-force labels are aligned to optical samples by per-session monotonic timestamps, never by moving CSV rows or video-frame indices. A positive lag pairs the optical feature captured at `t` with linearly interpolated reference force at `t - lag`. The alignment contract forbids extrapolation, cross-session pairing, and interpolation across reference gaps greater than 300 ms; see `config/manual_timestamp_alignment.json`. This affects calibration/training labels only—the live v4 runtime remains camera-and-skin only.

## Tests and simulation

Run all hardware-free tests:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -q
```

The current suite covers serial formats without `HELLO`, reset/startup delays, reconnection/fault paths, signed positive/negative calibration, raw camera-view/baseline behavior without property controls, camera-service boundaries, dual-layout resizing, motion parameter/filter behavior, original/processed/magnified recording, synchronization, master export, and interrupted-session recovery.

Run the full deterministic smoke path:

```powershell
.\.venv\Scripts\python.exe -m scripts.simulation_smoke --output-root .\output --session-name my-simulation-smoke --frames 120 --width 320 --height 240 --fps 30 --loadcell-rate 80 --seed 20260803
```

Run the sustained 900-frame profile:

```powershell
.\.venv\Scripts\python.exe -m scripts.performance_profile --output-root .\output --session-name my-performance-profile --frames 900 --width 320 --height 240 --fps 30 --loadcell-rate 80 --seed 20260803 --require-realtime-capacity
```

Generate a short video-file simulation source if needed:

```powershell
.\.venv\Scripts\python.exe -m scripts.generate_synthetic_video .\output\synthetic_press.mp4 --overwrite
```

## Common errors and fixes

| Symptom | Action |
|---|---|
| Camera index/backend will not open | Close other camera programs, disconnect, Refresh Cameras, then try DirectShow and Media Foundation separately. |
| Laptop Camera app appearance does not match the GUI | Close the Camera app, connect the GUI with DirectShow, open **Windows Camera Properties** inside the GUI, make the adjustment there, then confirm it appears under **Current values used by GUI** and recapture the optical baseline. App-only filters and Studio Effects cannot be imported through OpenCV. |
| Camera prerequisite timeout | Restore fresh preview frames, reconnect if needed, then repeat baseline/recording preparation. |
| Baseline invalid | Restore the intended camera/ROI configuration and capture a new unloaded baseline. |
| Baseline drift rejected | Remove load, stabilize lighting/camera, and recapture the baseline. |
| No COM ports | Install the Nano USB-serial driver, use a data-capable cable, close Serial Monitor, reconnect, and Refresh Ports. |
| COM port opens but no valid reading arrives | Confirm the selected baud/parser, allow the Nano reset delay, inspect Raw Serial Monitor, and verify the sketch continuously emits a supported numeric format. `HELLO` is not required. |
| Data arrives but cannot be parsed | Select the matching input format or compare the raw line with the documented formats; startup text does not count as a sensor reading. |
| COM port access denied/in use | Close Arduino Serial Monitor/Plotter, other applications, and other GUI instances, then reconnect. |
| `M115`/`M114` worked but Home never completes | Verify the Ender 3 main PSU is ON; USB alone may power serial electronics without driving steppers. If an axis is stuck or grinding, cut printer power. Power-cycle, clear the envelope, reconnect, and retry once while watching the printer log. |
| Home is busy and controls are disabled | Wait for `G28`, `M400`, and the final `M114`. The busy label prevents overlapping commands. If it ends in timeout, recover/power-cycle and reconnect; do not keep queueing Home commands. |
| Repeated press says a recording/trial is active | Do not press **Start Recording** for an automated run. Stop any active manual recording, wait for finalization, enter the next trial labels, then press **Start Repeated Press**; it starts its own synchronized recorder. |
| Repeated press is confirmed but motion has not started yet | Read the Printer sequence status/log. The GUI first prepares the recorder and configured pre-roll; motion is queued only after the recorder reports started. Any preparation failure now clears the pending transaction so it can be retried safely. |
| Motion preview reports Nyquist/pyramid error | Lower the upper cutoff below half the measured processing FPS, reduce pyramid depth, or increase processing resolution/rate. |
| `HX711_NOT_READY` or `HX711_TIMEOUT` | Check HX711 power, common ground, DOUT/SCK pins, sensor wiring, and that no other sketch owns those pins. Fresh DATA must recover before actions re-enable. |
| Calibration SNR/CV/sample-count failure | Remove vibration, use the correct stable 200 g mass, wait for settling, inspect wiring, and repeat from unloaded. |
| Verification outside ±5% | Remove/reseat the mass, retare unloaded, verify the same mass, and recalibrate if it still fails. |
| Output preflight or disk-space failure | Choose a writable local folder with sufficient free space; do not record to an unreliable/sync-conflicted destination. |
| MP4 unavailable | The recorder tries MJPG/AVI and records the actual filename/codec. If both fail, install a compatible OpenCV/video environment or choose another Windows system. |
| Recording becomes partial | Open the reported directory, preserve `session_status.json` and `.partial` files, correct the named error, then record a new trial. |
| GUI looks clipped at scaling | Use the scroll areas; the app is offscreen-tested at 1366×768 and 1920×1080 with 100%, 125%, and 150% scale. Complete the physical-display checklist on the target PC. |
