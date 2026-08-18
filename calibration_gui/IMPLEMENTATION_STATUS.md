# Implementation Status

This is the final evidence ledger. Status values are limited to **Not started**, **Implemented**, **Tested**, **Verified**, and **Blocked by unavailable hardware**. “Verified” refers to automated, simulation, artifact, or documented evidence; it does not silently imply physical hardware.

## Build-gate status

| Gate / core requirement | Status | Evidence |
|---|---|---|
| Gate 1: project structure, validated configuration, schemas, interfaces, error codes, status models | Verified | `core/models.py`, `data/schemas.py`, `services/interfaces.py`, `config/default_config.json`; model/schema tests in final suite. |
| Gate 1: load-cell formulas, calibration quality, tare, verification, synchronization | Verified | Calibration/workflow/firmware/serial/synchronization focused command: 67 passed. |
| Gate 1: ROI rules, baseline, HSV features, localization, export schemas | Verified | ROI/feature/pipeline/schema tests and final smoke artifact audit. |
| Gate 2: deterministic camera/video/load-cell simulation sources | Verified | `services/simulation_service.py`, `scripts/generate_synthetic_video.py`; simulation-source and smoke tests. |
| Gate 2: simulation vertical slice through unannotated video/raw/master/graphs | Verified | `output/final-simulation-smoke-completion-20260803`: 120 video/frame/master, 318 load, five graph bundles. |
| Gate 2: incremental writes, one-to-one identity, partial recovery | Verified | Exporter/recorder/reliability tests; forced camera/serial partial integrations and complete smoke. |
| Gate 3: Arduino Nano/HX711 firmware and protocol | Verified | AVR compile and old-bootloader Nano upload passed on COM3; physical HELLO/DATA/PING/STATUS/STOP/START/reset behavior and fresh sample IDs were exercised. |
| Gate 3: single-owner OpenCV camera and serial services | Verified | Arducam IMX179 index 0 and COM3 ran concurrently for 60 seconds with no ownership error, serial exception, or sample-ID gap. |
| Gate 3: raw camera view, post-connect warm-up, and drift | Verified | GUI camera-property/mode controls and readback gates were removed; Original Frame is always raw, and baseline/recording use fresh post-connect frames. DirectShow sustained 14.985 measured FPS physically. |
| Gate 3: load-cell reading validation/sessions/calibration/tare/verification/live health | Verified | Consecutive finite-reading validation, startup delay, stream timeout, raw monitor, signed calibration, device-profile matching, and GUI transaction tests pass. |
| Gate 4: dual video layout, nine scrollable control tabs, Help, readiness/review workflow | Verified | Offscreen GUI/full workflow/partial review/layout tests pass, including Printer Motion. |
| Gate 4: independent Ender 3/Marlin serial owner and safe manual controls | Tested | Mocked coverage plus physical COM4 M115/M114, G28/M400, and bounded jog evidence. `output/printer_z10_to_z14_operator_step_20260804.json` records Z=10→14 mm and the operator confirmed positive Z moved away from the sensor; X/Y direction remains separately bounded. |
| Gate 4: generation-safe synchronized repeated-press controller | Tested | Explicit states, continuous down/up commands, pre/post-roll recorder integration, pause/stop/abort/retract, calibrated-force trip, motion schema, and generation tests pass. |
| Gate 4: live Eulerian color/intensity motion magnification | Verified | Timestamp-aware OpenCV pyramid implementation, bounded worker, parameter/profile/Nyquist/state tests, processed preview integration, and scientific-source warning pass. |
| Gate 4: independent original/overlay/processed/motion video outputs | Verified | Recorder validates equal physical frame counts for every requested stream; original remains unannotated; auxiliary stream integration test passes. |
| Gate 4: heatmap, force plot, temporal ROI lines, spatial profile | Verified | Graph/GUI focused command: 24 passed; visual inspection of final PNGs. |
| Gate 4: manual and automatic PNG/CSV/JSON graph exports | Verified | Final smoke has all 15 automatic companions; exact manual/automatic companion tests. |
| Gate 5: disconnect, shutdown, queue, disk, codec, partial/review behavior | Verified | Recorder focused command: 19 passed; reliability/architecture sign-off. |
| Gate 5: performance profile and bounded newest-only display | Verified | Final 900-frame profile: 180.952 FPS effective, 6.032× real-time, max backlog 1, final backlog 0. |
| Gate 6: final suite and offscreen launch | Verified | `.\.venv\Scripts\python.exe -m pytest -q` → 395 passed after Home All confirmation dispatch repair; app and GUI paths execute through Qt offscreen tests. |
| Gate 6: full smoke and generated-artifact audit | Verified | Final smoke exited 0; 647/647 scoped dictionary keys, 120/120/120 identity, no partial files. |
| Gate 6: five independent reviews and major-finding remediation | Verified | `ENGINEERING_REVIEWS.md`: 0 blockers, 0 majors, 0 minors remaining. |
| Gate 6: README, physical checklist, final acceptance audit | Verified | `README.md`, `HARDWARE_TEST_CHECKLIST.md`, and this 43-row ledger. |

## Acceptance-criteria audit

| # | Acceptance criterion | Status | Evidence |
|---:|---|---|---|
| 1 | Installs from `requirements.txt` | Verified | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` exited 0; `pip check` reported no broken requirements. |
| 2 | GUI launches on Windows or Qt offscreen | Verified | `QT_QPA_PLATFORM=offscreen` simulation launch exited 0; GUI smoke tests pass. |
| 3 | Simulation completes the full core workflow | Verified | Final 120-frame smoke completed calibration, baseline, recording, synchronization, finalization, and graphs. |
| 4 | Arducam IMX179 selection by index with DirectShow/Media Foundation | Verified | Windows PnP `USB VID_1BCF&PID_0B12` and safe previews identified index 0; DirectShow was retained after both backends were probed. |
| 5 | Camera properties do not block acquisition or baseline | Verified | No camera-property controls are exposed or written by the GUI; baseline needs only live preview, nine valid ROIs, fresh warm-up frames, and unloaded capture. |
| 6 | Exactly nine ROIs can be created, saved, and loaded | Verified | ROI validation/order/JSON round-trip and GUI tests. |
| 7 | ROI or camera changes invalidate the baseline | Verified | Context fingerprint and GUI invalidation regressions. |
| 8 | HSV and delta-V use unannotated frames | Verified | OpenCV range/low-saturation/delta/unchanged-original tests. |
| 9 | Live heatmap and force plot do not block acquisition | Verified | Newest-only buffers, timer-driven plots, stalled-GUI/coalescing tests, 900-frame no-growth profile. |
| 10 | Firmware emits only new HX711 conversions | Verified | Static contract plus physical 60-second concurrent stream: 91.847 ms median interval and zero sample-ID gaps. |
| 11 | Calibration, tare, and ±5% verification are implemented | Verified | Formula/sign/tare/verification tests and full GUI wizard test; simulation verification error 0.05%. |
| 12 | One recording is one fixed-label manual press or repeated-printer sequence | Verified | `TrialLabels`, locked start snapshot, sequence IDs/cycle provenance, and GUI workflows. |
| 13 | One video frame, feature row, and master row per accepted frame | Verified | Final smoke 120=120=120; performance profile 900=900=900 with contiguous IDs. |
| 14 | Monotonic timestamps are authoritative | Verified | Camera/serial/pipeline/sync/graph tests and exported host/elapsed timestamps. |
| 15 | Force synchronization respects maximum gap | Verified | Linear, nearest, invalid-gap, missing force/contact tests. |
| 16 | Video is unannotated | Verified | Recorder decodes/compares saved frames within codec tolerance; display annotations use preview copies only. |
| 17 | Raw and derived data are written incrementally | Verified | `.partial` writer/flush/fault tests and partial-session artifacts. |
| 18 | Interrupted sessions are recoverable and partial | Verified | Forced camera/serial/HX711/disk/shutdown paths preserve partials/status and populate partial Review. |
| 19 | All required files and CSV fields are produced | Verified | Final smoke files/status plus 647-row scoped dictionary/header audit. |
| 20 | Automated tests pass | Verified | 395 passed, 0 failed, 0 skipped after Ender 3 Home All dispatch repair. |
| 21 | GUI includes instructions and readiness checklist | Verified | Always-available Help topics, dual preview, nine right-side tabs, ten recording-readiness flags, and printer-specific interlocks. |
| 22 | Text/controls do not overlap at supported sizes/scales | Verified | All tabs/Help checked offscreen at 1366×768 and 1920×1080, 100/125/150%; native monitor remains blocked below. |
| 23 | README and hardware checklist are complete | Verified | Required topics and nine exact physical test sections are present. |
| 24 | Printer control is independent, bounded, and synchronized | Tested | COM4 is separate from COM3; Marlin handshake, single writer, homing/jog/zero, state machine, force safety, and recorder provenance are implemented. No machine-learning dependency exists. |
| 25 | Hardware testing is reported honestly | Verified | `PHYSICAL_HARDWARE_VALIDATION_20260803.md` separates verified Arducam/Nano/HX711/calibration/session evidence from unmeasured optical latency and unperformed destructive fault injection. |
| 26 | Independent readiness flags derive recording readiness | Verified | Model truth table plus GUI gating/revalidation/transaction tests. |
| 27 | Warm-up frames excluded and baseline drift checked | Verified | Readback-time/captured-timestamp/timeout tests; final drift 0.0 ≤ 5.0. |
| 28 | Arduino reset/numeric validation and device-session reset handled | Verified | Numeric lines validate without HELLO; optional HELLO→START/ACK, sample reset, rollover, new session, and elapsed-origin tests pass. |
| 29 | Calibration windows enforce SNR/stability rules | Verified | Sample/SNR/CV/span/tare-stability rejection tests; simulation SNR 14142.136, CV 0.007071%. |
| 30 | Trial labels differ from derived frame contact state | Verified | Schema/pipeline tests keep trial metadata fixed and contact NaN without valid force. |
| 31 | Raw files written during recording; master generated/validated at finalization | Verified | Recorder tests inspect partials before and finals/master after; final smoke has no remaining partials. |
| 32 | MP4/AVI fallback names and metadata stay consistent | Verified | Injected MP4 failure produces MJPG/AVI with matching actual path/config/status; final smoke used mp4/mp4v. |
| 33 | Disk checked before and during recording | Tested | Preflight, low-space safe partial stop, disk-write classification tests; physical disk exhaustion not induced. |
| 34 | Manual current 3×3 graph saves PNG/CSV/JSON | Verified | Exact displayed-value/order/metadata/manual worker tests. |
| 35 | Finalization saves peak, mean-contact, integrated heatmaps | Verified | All three bundles complete in final smoke; no-contact unavailable regression. |
| 36 | Graph values/order/labels/scale/metadata reproducible | Verified | Exact companion/audit tests; final value audit/visual inspection. |
| 37 | Spatial export does not block acquisition/GUI | Verified | Graph executor/future tracking and worker-thread tests. |
| 38 | Live ROI 1–9 temporal graph uses elapsed timestamps | Verified | Authoritative timestamp/order/display tests. |
| 39 | Current spatial profile uses heatmap values | Verified | Exact shared snapshot test and profile companion audit. |
| 40 | Both line types save PNG/CSV/JSON | Verified | Manual temporal/profile export tests and companion schemas. |
| 41 | Finalization saves complete temporal and peak profile graphs | Verified | Final smoke has both bundles; peak frame 65 matches peak heatmap/profile. |
| 42 | Live history bounded without altering saved raw data | Verified | Bounded/downsampling tests plus full 120/900 raw exports. |
| 43 | Line-graph export does not block acquisition/GUI | Verified | Off-thread graph service/future/close tests. |
| 44 | Serial validation does not require HELLO | Verified | Plain signed, float, RAW prefix, timestamp CSV, DATA, startup text, invalid UTF-8, malformed, NaN/infinity, timeout, and no-HELLO service tests pass. |
| 45 | Raw readings remain visible before calibration | Verified | Worker publishes every finite parsed sample; UI renders raw/timestamp/rate while mass/force remain explicitly not calibrated. |
| 46 | Calibration profiles are device-associated | Verified | Saved port/baud/direction/statistics are validated and a selected/connected-device mismatch is rejected before application. |
| 47 | Camera GUI is settings-free and raw | Verified | Capability tables, property editors, native-properties/profile actions, and setting-readback readiness were removed; service-level diagnostics remain outside the operator workflow. |
| 48 | Original and processed frames occupy the required left layout | Verified | Named top/bottom panels, keep-aspect-ratio labels, splitter/right control tabs, independent screenshots, overlay toggle, and resize tests pass. |
| 49 | Motion magnification is optional, bounded, and scientifically explicit | Verified | Config/filter/ROI/pyramid/Nyquist/profile tests; drop-stale worker; source warning; complete parameter metadata. |
| 50 | Raw/master exports retain calibration and synchronization provenance | Verified | Raw/tared/mass/force/factor/zero/port/baud/timestamp-availability fields and signed nearest-sample offset are schema-tested and dictionary-backed. |

## Hardware verification boundary

| Physical item | Status | Reason/evidence boundary |
|---|---|---|
| Ender 3 COM4 Marlin identity and read-only position query | Verified | `output/printer_read_only_smoke_20260804.json`: COM3/COM4 enumerated separately; COM4 at 115200 identified `Marlin V1.1.6`; M114 parsed X=-13.0, Y=-7.5, Z=0.0; command list was exactly M115/M114. No motion claim follows. |
| Ender 3 homing, axis direction, repeated motion, force abort, and M112 recovery | Not yet physically verified in this implementation | Requires explicit operator clearance/action and the Ender 3 sections in `HARDWARE_TEST_CHECKLIST.md`. Mocked tests are not a hardware claim. |
| Arducam IMX179 identity, modes, real FPS, backend stability, property matrix | Verified | Device Manager identity/driver recorded; DirectShow sustained 14.985 FPS for 60 seconds; property/native-dialog evidence retained. True sensor-to-display optical latency was not measured and is stated separately. |
| Arduino sketch compile/upload and real numeric/reset behavior | Verified | `arduino-cli` 1.5.2-rc.1, AVR core 1.8.8, HX711 library 0.7.5, Nano old-bootloader upload on COM3, command/reset checks passed. |
| HX711 electrical readiness and conversion cadence | Verified | DOUT D4/SCK D5 produced live data; median cadence 91.847 ms (~10.89 Hz) with zero ID gaps during concurrent acquisition. Destructive NOT_READY wiring fault injection was not performed. |
| Load-cell wiring, real 200 g SNR/CV/factor/tare/±5% verification | Verified | Factor 610.723941 counts/g; initial SNR 4807.0 and CV 0.0208%; fixture retare followed by independent 199.015 g result, 0.493% error, 0.045 g SD. |
| Physical single-press recording and artifact consistency | Verified | `output/physical-arducam-com3-20260803T105013Z`: 161 video/feature/master rows, 125 serial samples, zero invalid sync rows, four decodable 161-frame videos, 15 graph companions, no partials. |
| Native target monitor/taskbar/DPI behavior | Blocked by unavailable hardware | Six Qt-offscreen size/scale combinations pass; no physical target display inspected. |
