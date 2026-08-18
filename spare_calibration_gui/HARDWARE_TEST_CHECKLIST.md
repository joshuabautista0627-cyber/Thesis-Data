# Physical Hardware Test Checklist

## Ender 3 repeated-press safety boundary

Complete this section before any automated motion. The read-only checks may be run without moving the printer; every homing/jog/cycle item requires an operator at the machine with a clear build volume and access to physical power/reset controls.

### A. Separate COM identity and read-only handshake

- [ ] Disconnect software that may own either port, including slicers, printer terminals, Arduino Serial Monitor, and Serial Plotter.
- [ ] Enumerate Windows ports and record the Nano/HX711 as COM3 and the Ender 3 as COM4; do not infer identity from a COM number alone.
- [ ] Run `scripts/printer_hardware_smoke.py --printer-port COM4 --loadcell-port COM3 --output <new-json-path>`.
- [ ] Confirm its command list is exactly `M115`, `M114`, its firmware response identifies Marlin, and `motion_commands_sent` is false.
- [ ] Confirm COM3 was reserved/not opened by that script and remains independently available to the load-cell GUI.
- [ ] Disconnect/reconnect COM4 and confirm homing, position-valid, press-zero, and direction-confirmation state are cleared.

### B. Homing, reported coordinates, and axis direction

- [ ] Remove the specimen or install a nonvaluable dummy; clear the complete machine envelope.
- [ ] Confirm the Ender 3 main PSU is ON; do not infer motor power from USB serial/LCD activity.
- [ ] Use the explicit Home action and observe `G28`, `M400`, then `M114` in the printer log. Connection itself must not move.
- [ ] Record the reported X/Y/Z after homing: ______________________________.
- [ ] Jog X and Y by +1 and -1 mm, then Z by +1 and -1 mm at a conservative feed. Confirm physical direction and parsed M114 coordinates.
- [ ] Test 10, 1, 0.1, and 0.01 mm step selection without exceeding configured bounds.
- [ ] Explicitly determine whether negative machine Z lowers the press toward the specimen. Record observation and fixture orientation: ______________________________.
- [ ] Attempt one out-of-bounds target and confirm it is rejected before `G1` appears in the log.
- [ ] If using the opt-in helper, run `scripts/printer_motion_smoke.py --output <new-json-path> --confirm-clear` only with an operator at the power switch, then attach the JSON and record observed directions.

### C. Software press zero and target calculation

- [ ] Jog to a safe clearance above the unloaded dummy/specimen and query M114.
- [ ] Set Press Zero Here; confirm no `G92` appears in the command log.
- [ ] Preview a small negative displacement and independently calculate `press_zero_z + displacement`; confirm the displayed absolute target matches.
- [ ] Clear press zero and confirm Preview/Test/Start disable.
- [ ] Re-establish zero, then home or reconnect and confirm it is invalidated.
- [ ] Save/load a printer profile and confirm press zero and negative-Z confirmation are not restored.

### D. One-cycle motion and synchronization

- [ ] Use the smallest mechanically visible safe displacement and conservative feeds.
- [ ] Confirm camera preview, verified/calibrated COM3 stream, baseline, labels, disk, and recorder preflight are all ready.
- [ ] Click Test One Cycle and inspect the pre-start summary before approval.
- [ ] Confirm recording starts before the pre-roll, followed by one downward `G1`, `M400`, bottom hold, one upward `G1`, `M400`, post-roll, then clean finalization.
- [ ] Decode the video and inspect CSV rows across pre-roll/down/hold/up/post-roll phases.
- [ ] Confirm reported coordinates exist only after M114 and do not simply echo commanded coordinates.

### E. Repetition, pause, stop, abort, and application recovery

- [ ] Run at least three safe dummy cycles and compare configured/completed cycle counts.
- [ ] Pause during a cycle; confirm it retracts before entering Paused and resumes at the next cycle boundary.
- [ ] Use Stop + Return and confirm a bounded retract to press zero, post-roll, and clean finalization with stopped status.
- [ ] Use Abort during a disposable cycle and confirm normal queued work cancels, M410 is logged, retract is attempted, and the session records aborted status.
- [ ] Close/reopen the GUI with the printer stationary and confirm no press zero or sequence is resumed automatically.

### F. Force limit and emergency behavior

- [ ] Establish a conservative limit below specimen damage but above unloaded noise; document rationale: ______________________________.
- [ ] With a compliant dummy and lab authorization, produce a force just above the threshold and confirm the triggering COM3 sample, force event timestamp, M410 request, abort, and retract are recorded.
- [ ] Measure sample receipt to M410-write latency from `printer_sequence.json`: __________ ms. Define lab acceptance: __________ ms.
- [ ] Verify the HX711 conversion cadence plus mechanics makes that latency acceptable; software force limiting is not a certified interlock.
- [ ] Test the red M112 action only under a controlled disposable setup. Confirm it uses the printer writer, invalidates position/zero, aborts recording truthfully, and requires deliberate printer recovery/reset.
- [ ] Never use a successful M112 software test as evidence of physical guarding or fail-safe electrical protection.
- [ ] Record `M115` emergency-parser capability. If `Cap:EMERGENCY_PARSER:0`, treat M410/M112 as potentially delayed during a blocking command and keep a physical power cutoff immediately accessible.

### G. Final artifact audit

- [ ] `frame_features.csv`, `loadcell_raw.csv`, and `master_synchronized.csv` include every documented motion field.
- [ ] `printer_sequence.json` contains settings-independent results, events, commands, completed cycles, force maxima/trips, and terminal status.
- [ ] `session_config.json` records printer connection/settings and states that active press zero is not persisted.
- [ ] Frame/video/master identity remains one-to-one and partial files are absent after clean completion.
- [ ] Document every item actually tested, plus all untested collision, force, latency, disconnect, and emergency limitations. Do not convert simulation/mocked evidence into a hardware claim.

## Verification boundary from the development environment

Do not treat simulation or injected-service tests as physical-hardware verification.

| Item | Development-environment observation | Physical status |
|---|---|---|
| Arducam IMX179 Camera Module | Physically identified as USB VID:PID `1BCF:0B12`; DirectShow/native-control/concurrent-cadence evidence is retained under `output/hardware_camera_probe_20260803`. | **Verified 2026-08-03** |
| Arduino Nano | CH340 COM3; compiled and uploaded with the Nano old-bootloader FQBN; protocol command/reset behavior passed. | **Verified 2026-08-03** |
| HX711 | Physical DOUT D4/SCK D5 stream ran at a 91.847 ms median interval with no sample-ID gaps during the 60-second concurrent test. | **Verified 2026-08-03** |
| Load cell and 200 g reference mass | Physical calibration, fixture retare, independent 200 g verification, and single-press synchronization artifacts were captured. | **Verified 2026-08-03** |
| Native monitor layout | Qt offscreen tests passed at 1366×768 and 1920×1080 at 100%, 125%, and 150%, but no target monitor was physically inspected. | **Unverified** |

Complete every section below on the experiment computer. Record actual values, save screenshots/logs, and retain one output session as evidence. A checked box means the physical action was actually performed—not merely simulated.

The executed 2026-08-03 values, passes, and explicit remaining limitations are recorded in [PHYSICAL_HARDWARE_VALIDATION_20260803.md](PHYSICAL_HARDWARE_VALIDATION_20260803.md). Blank boxes below remain a reusable procedure for future hardware/driver/fixture changes and are not retroactively checked when the exact GUI or destructive action was not performed.

## Test record

- Tester: ______________________________
- Date/time/time zone: ______________________________
- Computer and Windows version: ______________________________
- Application commit/version: ______________________________
- Camera serial/asset ID: ______________________________
- Arduino Nano USB/asset ID: ______________________________
- HX711 board/rate selection: ______________________________
- Load-cell model, capacity, and serial: ______________________________
- Traceable 200 g mass ID/tolerance: ______________________________
- Output evidence directory: ______________________________

## 1. Camera detection and backend

- [ ] Disconnect other cameras if practical and close Windows Camera, browsers, conferencing tools, and capture software.
- [ ] Connect the Arducam IMX179 directly to the target PC with its intended cable/USB port.
- [ ] Confirm the device/driver in Windows Device Manager and record the displayed device name and driver version.
- [ ] Launch `run_windows.bat`, open **Camera View**, choose **Physical Arducam IMX179**, and click **Refresh Cameras**.
- [ ] Record every discovered device index. Identify the Arducam IMX179 from Device Manager and a safe preview; do not assume index 0 is the correct model.
- [ ] Select **DirectShow**, request the intended width, height, and FPS, and connect.
- [ ] Record requested and actual width/height/FPS, selected index, and reported backend.
- [ ] Disconnect fully, select **Media Foundation**, reconnect, and record the same evidence.
- [ ] Confirm that every unsuccessful backend attempt reports an actionable error and releases the camera so the other backend can open it.
- [ ] Choose the backend that streams fresh frames stably; reported dimensions/FPS are informational because the simplified GUI does not expose mode/property settings.

Recorded result:

| Backend | Index | Requested mode | Actual mode | Opened/stable? | Notes |
|---|---:|---|---|---|---|
| DirectShow | | | | | |
| Media Foundation | | | | | |

Acceptance: the identified Arducam IMX179 opens on at least one backend, streams fresh frames, reports an acceptable actual mode, and can be disconnected/reconnected without leaving a second owner.

## 2. Raw camera view and settings-free baseline

With the chosen physical backend:

- [ ] Confirm the Camera View page contains source, device, backend, connect/disconnect, preview, performance, and status controls only.
- [ ] Confirm there are no exposure, gain, white-balance, focus, brightness, contrast, resolution/FPS, capability, native-properties, defaults, or camera-profile controls.
- [ ] Start Preview and confirm Original Frame matches the camera view as received, without ROI boxes, annotations, false color, background subtraction, or motion magnification.
- [ ] Define exactly nine valid ROIs, leave the fixture unloaded, and click Capture Unloaded Baseline.
- [ ] Confirm baseline collection begins after fresh post-connect warm-up frames without requesting or waiting for camera-property readback.
- [ ] Enable Color magnification in the separate Motion Magnification tab and confirm it appears only in the processed view while Original Frame remains unchanged.
- [ ] Save a session and confirm camera property request/readback/capability collections are empty and marked as GUI-disabled provenance.

Acceptance: the raw camera is usable without camera-setting reads/writes, unloaded baseline capture succeeds from live frames and valid ROIs, and color motion magnification remains isolated to processed output.

## 3. Preview latency and sustained camera behavior

- [ ] Start the physical preview at the intended experiment mode.
- [ ] Observe it continuously for at least 60 seconds without recording.
- [ ] Record camera capture FPS, preview FPS, dropped preview copies, and capture-to-display time at the beginning, middle, and end.
- [ ] Confirm the preview remains responsive while switching among Heatmap, Temporal ROI Lines, and Current Spatial Profile.
- [ ] Confirm Help, tab scrolling, and Stop remain accessible on the actual target monitor/scaling.
- [ ] Cover/uncover or move a non-sensitive test target and independently estimate visible latency; do not record people or private content for this check.
- [ ] Start a short trial and confirm capture continues while plots refresh and graph export runs.
- [ ] Enable color magnification, switch the lower-left selector to the magnified preview, and confirm the top-left original remains responsive and unaltered.
- [ ] Confirm Apply automatically selects **Motion-magnified frame** as the force-light HSV source, **Temporal ROI Lines**, and **Mean HSV V (0-255)**.
- [ ] Confirm both graph panels say **HSV measurement source: motion-magnified color frame** and respond to the magnified color changes rather than the raw spatial-intensity preview.
- [ ] If the driver-reported FPS is zero/unavailable, confirm Apply succeeds using measured/configured fallback FPS; then verify the live measured rate keeps the upper cutoff below Nyquist.
- [ ] Repeat with intensity-only magnification, ROI-only mode, reduced downscale, temporal-filter reset, and a runtime parameter change.
- [ ] Record motion FPS, processing latency, queue depth, and dropped-processing count. Confirm any overload drops stale processing frames without growing memory or interrupting original camera/load-cell capture.
- [ ] Select an upper cutoff above the measured Nyquist limit and confirm Apply is rejected with an actionable message; restore a valid value afterward.

| Time | Capture FPS | Preview FPS | Preview drops | Capture-to-display ms | Notes |
|---|---:|---:|---:|---:|---|
| Start | | | | | |
| 30 s | | | | | |
| 60 s | | | | | |

Acceptance: no increasing preview/acquisition backlog, no UI freeze, and latency/cadence are acceptable for the intended experiment. Record the lab's numeric acceptance threshold here: ______________________________.

## 4. Arduino serial connection and reset/numeric-stream behavior

- [ ] Install the HX711 Arduino library documented in `README.md`.
- [ ] Verify/upload `arduino/hx711_nano_stream/hx711_nano_stream.ino` to the physical Nano.
- [ ] Confirm DOUT→D4, SCK→D5, supply, common ground, and load-cell terminal wiring with power removed.
- [ ] Close Arduino Serial Monitor and every other COM-port owner.
- [ ] In the GUI Refresh Ports, select the Nano COM port and `115200` baud, then Connect.
- [ ] Confirm the selected port first shows opened/waiting and becomes validated only after at least the configured number of finite readings. `HELLO` must not be required.
- [ ] With the supplied firmware, confirm optional `HELLO,HX711_NANO,1.0,1.0.0` identity triggers compatible `START`/`OK,START` handling while fresh `DATA` remains the validation evidence.
- [ ] If available, upload a temporary plain `RAW:<value>` or numeric-line sketch and confirm it validates without `HELLO` and receives no protocol command writes.
- [ ] Open Raw Serial Monitor and confirm startup text/malformed rows are rejected, valid/rejected counters update, Pause Display does not stop acquisition, and Save Serial Log works.
- [ ] Record COM port, protocol version, firmware version, and device-session ID from the GUI.
- [ ] Press Nano reset during non-recording streaming. Confirm a new HELLO/sample reset produces a new device-session ID and streaming restarts.
- [ ] During a disposable physical trial, reset/disconnect the Nano and confirm the session safely becomes partial with the correct error/session evidence; do not use this fault-injection trial as experimental data.
- [ ] Confirm reconnecting does not allow an old calibration/window callback to become current.

Acceptance: finite readings—not identity text—gate readiness; supplied-protocol identity/commands remain compatible; resets create a new device session; and disconnects preserve a truthful partial session.

## 5. HX711 sample rate and readiness/timeout behavior

- [ ] With the sensor stable, stream at least 60 seconds.
- [ ] Record the GUI measured sample rate and minimum/median/maximum sample interval.
- [ ] Confirm `sample_id` is strictly increasing within one device session and no stale conversion is repeated.
- [ ] Compare the measured rate with the HX711 board's selected 10 SPS or 80 SPS hardware mode; record the board configuration.
- [ ] Safely induce `NOT_READY`/timeout only if the lab procedure permits (for example with a disposable wiring test), then confirm actions disable and a specific error appears.
- [ ] Restore wiring and confirm only a fresh DATA sample recovers readiness.

Measured sample rate: __________ Hz  
Minimum / median / maximum interval: __________ / __________ / __________ ms  
Configured HX711 hardware rate: __________ SPS

Acceptance: measured cadence is stable and appropriate, only new conversions are emitted, readiness loss blocks actions, and fresh DATA recovers them.

## 6. Physical 200 g calibration

- [ ] Warm up/stabilize the load cell and fixture according to the sensor procedure.
- [ ] Remove all load and capture the complete unloaded window without touching the fixture.
- [ ] Place the verified 200 g mass consistently and capture the complete loaded window.
- [ ] Calculate calibration.
- [ ] Record unloaded/loaded means, standard deviations, sample counts, signed counts per gram, span, SNR, loaded mean/std/CV, and quality result.
- [ ] Confirm sample count meets the configured minimum, SNR is at least 10, loaded-window CV is at most 2%, and the factor magnitude is nonzero.
- [ ] Confirm a negative factor is accepted when ADC counts decrease under positive load and still produces positive force for the known mass.
- [ ] Save and reload the calibration and confirm serial/firmware/protocol provenance remains exact.

| Result | Value |
|---|---:|
| Unloaded mean ± SD (counts) | |
| Loaded mean ± SD (counts) | |
| Unloaded / loaded sample count | |
| Span (counts) | |
| Signed counts per gram | |
| Calibration SNR | |
| Loaded-window CV (%) | |
| Quality passed | |
| Calibration ID | |

Acceptance: every configured sample/SNR/stability/factor rule passes without bypassing or weakening a threshold.

## 7. Tare and calibration verification

- [ ] Remove the 200 g mass, allow settling, and run Tare.
- [ ] Confirm tare stability passes and counts per gram is unchanged.
- [ ] Confirm any prior verification is immediately invalidated and Recording Start is disabled during tare.
- [ ] Replace the same 200 g mass in the same position and run Verify.
- [ ] Record expected gf, measured mean/std gf, percentage error, sample count, and pass/fail.
- [ ] Confirm absolute percentage error is no more than 5%.
- [ ] Remove/reapply the mass at least three times and record repeatability; recalibrate if the lab's repeatability rule is not met.

| Verification | Expected gf | Measured mean ± SD gf | Error % | Passed |
|---|---:|---:|---:|---|
| 1 | 200 | | | |
| 2 | 200 | | | |
| 3 | 200 | | | |

Acceptance: tare changes zero but not scale, retare invalidates verification, and the current physical 200 g verification passes ±5%.

## 8. Physical single-press recording

- [ ] Select the verified physical camera and load-cell source.
- [ ] Define/save/load exactly nine valid ROIs and confirm row-major order.
- [ ] With no load and stable lighting, capture/accept the baseline after warm-up.
- [ ] Confirm the pre-recording baseline-drift check passes.
- [ ] Enter unique fixed trial labels and select a writable local output folder with sufficient disk space.
- [ ] Confirm all ten readiness items are green; camera-property confirmation is not a readiness item.
- [ ] Click Start once, perform exactly one intentional press, then click Stop once.
- [ ] Wait until finalization says complete; do not terminate the app while files/graphs are finalizing.
- [ ] Confirm Review shows all required counts/rates/errors/force/synchronization/graph statuses and Open Output works.
- [ ] Open the saved video and confirm it has no ROI boxes, labels, heatmap, lines, timestamps, or other annotations.
- [ ] In a disposable trial, enable processed and motion-magnified recording. Confirm the requested auxiliary videos exist, decode, and contain exactly the accepted original frame count.
- [ ] For force-light calibration with color magnification, keep **Motion-magnified frame** selected for the entire baseline/recording workflow and confirm exported `roi*_mean_v` / delta-V columns change consistently with the live magnified HSV graph.
- [ ] Confirm `session_config.json` contains all motion parameters, quantitative-analysis source, auxiliary stream list, and whether magnified pixels were used for exported measurements.
- [ ] Confirm all five automatic graph bundles exist and visually inspect unclipped labels, ROI 1–9 ordering, color bar/legend, axes, and peak-frame agreement.

Acceptance: one complete directory represents one fixed-label single-press trial, video is unannotated, review is populated, and every automatic graph bundle is complete.

## 9. Video/CSV frame consistency and output audit

In the completed physical session directory:

- [ ] Confirm `session_status.json` says `session_status: complete`, `complete: true`, and `partial: false`.
- [ ] Record `accepted_frame_count`, `feature_row_count`, and `video_frame_count`; all must be equal.
- [ ] Count data rows (excluding headers) in `frame_features.csv` and `master_synchronized.csv`; both must equal the accepted-frame count.
- [ ] Confirm `capture_frame_id` starts at 0, is contiguous, unique, and identical in both CSVs.
- [ ] Decode the saved video with a trusted player/OpenCV and confirm its physical frame count equals the same count.
- [ ] Confirm `loadcell_raw.csv` sample IDs/timestamps are ordered within device sessions.
- [ ] Confirm `loadcell_raw.csv` preserves raw/tared counts, mass, force, signed factor, zero offset, serial port/baud, and whether the Arduino timestamp was actually device-provided.
- [ ] Confirm `master_synchronized.csv` preserves the camera host timestamp, nearest sample identity/raw value, signed `synchronization_offset_ms`, and calibration/device provenance.
- [ ] Confirm synchronized rows outside `max_sync_gap_ms` use missing force/contact rather than fabricated values.
- [ ] Confirm `data_dictionary.csv` has unique `(artifact_scope, column_name)` keys and covers the exact headers of the three core CSVs and every graph companion CSV.
- [ ] Confirm MP4/AVI filename and codec in `session_status.json`, `session_config.json`, and the real file agree.
- [ ] Confirm there are no `.partial` files in a complete session.
- [ ] Back up the complete physical evidence directory without modifying its contents.

PowerShell row-count aid:

```powershell
$session = "C:\path\to\the\completed\session"
(Import-Csv "$session\frame_features.csv").Count
(Import-Csv "$session\master_synchronized.csv").Count
(Import-Csv "$session\loadcell_raw.csv").Count
Get-Content "$session\session_status.json"
Get-ChildItem $session -Recurse -Filter "*.partial*"
```

Acceptance: accepted video frames, feature rows, and master rows are one-to-one; IDs are contiguous; metadata matches the actual container; complete sessions contain no partial artifacts.

## Final physical sign-off

- [ ] All nine sections passed.
- [ ] Any failure is documented with exact error text and retained evidence.
- [ ] The selected camera backend/property matrix and physical latency are acceptable.
- [ ] The Arduino/HX711 identity, cadence, calibration, tare, and verification passed.
- [ ] The physical recording is complete, unannotated, synchronized within the configured limitation, and one-to-one.
- [ ] The tester has not used simulation evidence as a claim of physical verification.

Tester signature: ______________________________  Date: ______________________________
