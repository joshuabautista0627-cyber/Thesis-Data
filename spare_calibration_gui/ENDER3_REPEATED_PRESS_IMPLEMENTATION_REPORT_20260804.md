# Ender 3 Repeated-Press Implementation Report — 2026-08-04

## Scope and source review

The complete `Ender3_Repeated_Press_Calibration_Master_Prompt.md` was read before modification. The current repository was audited across application configuration, domain models, camera/load-cell services, Qt worker ownership, calibration workflow, optical processing, synchronization, schemas/export/finalization, GUI tabs/coordinator, scripts, Arduino firmware, tests, and existing operator/validation documentation. The ZIP reference implementations `Hardware Characterization/printer_control.py` and `printer_panel.py` were also inspected.

The ZIP code was treated as behavioral reference only. It was not copied because it wrote M112 outside its command worker, tracked position optimistically, did not verify Marlin, had no bounded command lifecycle, did not invalidate zero/position state reliably, and had no recorder/force-state synchronization.

The unchanged pre-implementation baseline was **319 passed**.

## Implementation outcome

The existing PySide6 application now contains a ninth **Printer Motion** tab and a complete independent Ender 3 repeated-press path. Camera ownership, COM3 load-cell ownership, optical processing, calibration, recording, synchronization, and finalization remain in their established architecture.

### Independent printer serial subsystem

- Default **COM4**, editable port, default/editable **115200 baud**, configurable startup/read/command timeouts.
- Explicit same-port conflict rejection against the physical load-cell selection/connection.
- COM4 uses its own pyserial object; it does not share the COM3 `SerialService`.
- The serial object is created from the printer worker thread. A dedicated reader handles asynchronous lines and a single dedicated writer owns every COM4 write.
- Connection waits for startup, sends only `M115`, and enables motion only when Marlin is identified. It never homes or moves automatically.
- Normal commands have UUIDs and queued, sent, acknowledged, completed, rejected, timed-out, failed, or cancelled states with monotonic timestamps and response/error evidence.
- `M114` is parsed into separate tracked and reported coordinates. Reported fields are populated only from actual printer text.
- Temperature lines are parsed without being mistaken for command completion.
- `M400` is used after homing/motion so a cycle cannot advance merely because `G1` was accepted.
- M410 and M112 are priority control requests executed by the same sole writer, so the emergency paths do not create a competing serial write owner.

### Position, limits, and press zero

- Explicit operator warning precedes `G28`; connection never invokes it.
- X/Y/Z jogging supports 10, 1, 0.1, and 0.01 mm with independent XY/Z feeds.
- Configured X/Y/Z, displacement, Z-feed, and force limits are checked before motion.
- Press zero is a software-only machine-XYZ reference. No implementation path emits `G92`.
- Manual actions expose an explicit busy state and cannot overlap. A timed-out motion requests M410, latches further motion, and requires deliberate recovery/reconnect/rehoming.
- A post-home coordinate outside a configured printable bound may move only toward the safe range; movement farther outward remains blocked.
- Press zero is invalidated by disconnect/reconnect, homing, communication timeout/loss, invalid position, or emergency stop.
- Active zero and prior direction confirmation are not persisted in application or printer profiles.
- The press target is exactly `press_zero_z_mm + signed_displacement_mm`; negative displacement and a per-run physical direction confirmation are mandatory.

### Repeated-press controller

The generation-safe controller implements explicit states for idle, readiness, pre-roll, optional tare/baseline, move-to-start, pressing down, holding, retracting, inter-cycle dwell, paused, stopping, aborting, post-roll, complete, and error. Sequence generation, sequence UUID, cycle UUID/index, and command UUID prevent stale work from being treated as current.

Each normal cycle emits exactly one continuous absolute downward `G1` and one upward `G1`, each synchronized by `M400`. The controller supports preview, one-cycle test, full sequence, pause after safe retract, resume, stop/return, abort/retract, and M112 emergency behavior.

Optional tare and fresh baseline are performed by the existing transactional GUI workflows before recorder startup. An optional tare truthfully invalidates the prior known-mass verification while retaining a quality-passed signed scale; the automated-session snapshot records that start state rather than claiming verification remained current.

### Force safety

Every fresh COM3 sample is observed independently of COM4. Only a finite sample marked calibrated/valid is eligible to trip the inclusive absolute Newton threshold. A trip:

1. timestamps the force event;
2. marks the acquisition motion context aborted/force-exceeded;
3. non-blockingly requests M410 from the sole COM4 writer;
4. cancels the sequence; and
5. attempts a bounded return to a still-valid press zero.

The request latency is captured in the sequence event log. Total physical response also includes HX711 conversion and USB cadence, OS/Python scheduling, the writer's configured 10 ms priority poll, Marlin processing, and mechanics. This is documented as a secondary software limit, not a certified interlock.

### Recording and export integration

Automated Start creates the normal incremental recorder before pre-roll and stops it only after controller post-roll. Camera and load-cell acquisition do not wait on normal printer commands.

Motion context is snapshotted at bounded-recorder ingress for every frame and every load-cell sample. Schema version 1.1.0 adds these fields to `frame_features.csv`, `master_synchronized.csv`, and `loadcell_raw.csv`:

- sequence ID, generation, status;
- phase and phase-start monotonic timestamp;
- cycle ID/index/total;
- command ID;
- software press zero and validity;
- signed displacement and computed absolute target;
- commanded XYZ/feed;
- parsed M114-reported XYZ and validity;
- force limit/trip;
- pause and abort flags.

Outside an automated sequence, state is explicitly idle/false and reported coordinates remain NaN. The data dictionary covers every new field. `session_config.json` stores the immutable printer connection/settings and coordinate/latency policies; `printer_sequence.json` stores the result, state events, completed cycles, force evidence, and command/control history.

## Files changed

New production files:

- `core/motion.py`
- `services/printer_service.py`
- `services/printer_worker.py`
- `services/repeated_press_controller.py`
- `gui/printer_motion_tab.py`
- `scripts/printer_hardware_smoke.py`
- `scripts/printer_motion_smoke.py`

Core integration changes:

- `core/models.py`, `config/default_config.json`, `data/schemas.py`
- `processing/pipeline.py`, `services/qt_workers.py`
- `gui/main_window.py`, `gui/help_dialog.py`
- package/application version metadata

Tests and documentation:

- `tests/test_printer_service.py`
- `tests/test_repeated_press_controller.py`
- `tests/test_printer_gui_and_export.py`
- updated schema/config/tab expectations in existing tests
- `README.md`, `HARDWARE_TEST_CHECKLIST.md`, `IMPLEMENTATION_STATUS.md`, and `ENGINEERING_REVIEWS.md`

## Automated and simulation validation

Final evidence:

- Full pytest suite: **395 passed, 0 failed, 0 skipped**.
- New printer-focused coverage includes 76 passing GUI/service/controller cases, including integer-valued Qt Yes/No home-confirmation dispatch regressions.
- Dependency audit: `pip check` reported no broken requirements.
- Python compilation succeeded for all added/modified production modules.
- Qt offscreen application launch with simulation exited 0.
- 1366×768 Printer Motion visual QA showed a vertically scrollable panel, no horizontal overflow, and a fixed always-visible M112 button.
- Full 120-frame simulation smoke completed with 120 video frames, 120 feature rows, 318 load-cell rows, 120 master rows, zero missing force, no partials, and 734 scoped data-dictionary rows.
- Simulation motion fields were explicitly idle; reported printer Z remained NaN rather than fabricated.

Simulation artifact: `output/ender3-integration-simulation-20260804`.

## Physical validation actually performed

Read-only validation was repeated, followed by an operator-authorized cautious homing and small-jog sequence. The operator confirmed the main PSU was on, the build volume was clear, and the axes had moved during the earlier Home All attempt.

At 2026-08-04 05:51 UTC (13:51 Asia/Taipei):

- COM3 and COM4 enumerated as separate CH340 interfaces.
- Only COM4 was opened at 115200.
- `M115` identified **Marlin V1.1.6 (Sep 13 2022 15:13:53)**.
- `M114` parsed X = −13.0 mm, Y = −7.5 mm, Z = 0.0 mm.
- Software correctly retained `homed=false` and `press_zero_valid=false`.
- The exact command list was `M115`, `M114`.
- No `G28`, `G1`, `G92`, M400, M410, or M112 was sent.
- COM3 was enumerated but not opened; no load-cell samples were collected.
- The camera was not opened.

Evidence: `output/printer_read_only_smoke_20260804.json` and `output/printer_read_only_recheck_20260804.json`.

At 2026-08-04 06:36 UTC (14:36 Asia/Taipei), the connected printer then completed:

- explicit `G28` (about 20.0 s of firmware processing), `M400`, and parsed `M114` at X=149.2, Y=120.9, Z=10.0 mm;
- Z +1.0 mm at 120 mm/min, verified by M114 at Z=11.0 mm;
- X +1.0 mm and Y +1.0 mm at 600 mm/min, verified at X=150.2 and Y=121.9 mm;
- Z −0.1 mm at 60 mm/min, verified at Z=10.9 mm;
- Z +0.1 mm at 60 mm/min, verified back at Z=11.0 mm.

Every `G1` used `G90`, completed through `M400`, and was followed by a parsed `M114`. No timeout, Marlin error, motion interlock, `G92`, M410, or M112 occurred. COM3 was enumerated/reserved but not opened. Exact evidence is in `output/printer_motion_smoke_20260804.json`.

In a separately confirmed operator step, the printer was rehomed at Z=10.0 mm and sent one absolute `G1 Z14.000 F120.000` move. The final M114 reported Z=14.0 mm, and the operator visually confirmed that this +4.0 mm motion moved away from the sensor. No X/Y jog, press-zero, M410, or M112 command was sent. Evidence: `output/printer_z10_to_z14_operator_step_20260804.json`.

Firmware identified the machine as **Ender-3 V2 Neo** and reported `Cap:EMERGENCY_PARSER:0`. M410/M112 therefore remain software requests, not guaranteed mid-command physical interruption; the operator must retain access to printer power.

## Physical validation deliberately not claimed

The following remain operator/lab checklist items and are not represented as tested hardware behavior:

- operator-observed physical X/Y direction confirmation for the recorded jog commands (positive Z away from the sensor is physically confirmed);
- press-zero placement and specimen-safe displacement;
- one-cycle or repeated physical motion;
- camera/COM3/COM4 concurrent automated recording;
- physical pause/stop/abort retraction;
- real force-limit trip latency and mechanical stopping distance;
- M112 physical recovery/reset behavior;
- application restart recovery after a physical interrupted sequence.

The GUI blocks automated pressing until a human explicitly confirms direction and all live interlocks. Use `HARDWARE_TEST_CHECKLIST.md` with a compliant dummy specimen and conservative limits before experimental use.
