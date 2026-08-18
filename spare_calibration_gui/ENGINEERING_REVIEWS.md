# Independent Engineering Reviews

Five independent reviews were performed after the implementation, automated suite, and simulation path existed. Every finding records its severity, affected files, required correction, final resolution, and verification evidence. The final frozen review state is **0 blockers, 0 majors, and 0 minors remaining**.

The original engineering review was completed without physical hardware. A later operator-assisted physical pass on 2026-08-03 identified the intended camera as Arducam IMX179 and added Nano/HX711/calibration/session evidence in `PHYSICAL_HARDWARE_VALIDATION_20260803.md`; review claims below remain scoped to the evidence stated at their original review time.

## 1. Software architecture and concurrency

Reviewer scope: service/thread ownership, bounded queues/buffers, Qt handoffs, state transactions, partial recovery, and shutdown.

| Finding | Severity | Affected files | Required correction | Resolution status | Evidence after correction |
|---|---|---|---|---|---|
| Recorder ingress could race finalize/abort, and persisted counts did not distinguish accepted ingress from written rows. | Major | `services/qt_workers.py`, `services/session_recorder.py` | Use one state lock for acceptance/queue/terminal decisions, close ingress on the first terminal fault, and persist ingress and written counts separately. | Resolved | Deterministic submit/finalize interleaving, post-fatal rejection, saturation recovery, and ingress-count tests pass. |
| Baseline/recording prerequisites were not one atomic immutable transaction across identities, labels, ROI, calibration, output, and readiness. | Major | `gui/main_window.py`, all four GUI tabs | Add generations, deep snapshots, revalidation immediately before sink attachment, complete configuration locks, and cancellation on faults/changes. | Resolved | Delayed ROI mutation, serial disconnect, camera-setting failure, stale baseline callback, and baseline/recording overlap regressions pass. |
| Per-frame Qt payloads could accumulate when the GUI stalled. | Major | `services/qt_workers.py`, `gui/main_window.py` | Replace unbounded per-frame signals with newest-only buffers containing lightweight rows/counters. | Resolved | Stalled-GUI tests retain only the newest recording/live result and count coalesced drops; no full image array crosses the per-frame GUI handoff. |
| Ordinary live-analysis stop waited on the worker. | Minor | `gui/main_window.py`, `services/qt_workers.py` | Detach/invalidate nonblocking during ordinary operation; reserve bounded waits for shutdown. | Resolved | Probe confirmed ordinary stop returns without `wait`; stale analysis generations are rejected. |
| Graph-export shutdown could wait without a visible bound. | Minor | `gui/main_window.py` | Track futures, refuse close while exports are pending, and use nonwaiting executor shutdown after acceptance. | Resolved | Pending-export close-refusal regression passes. |
| Recorder start errors could report an inaccurate initialization stage. | Minor | `services/session_recorder.py` | Classify video vs disk failures and set `session_artifact_validation` before the all-or-none provenance check. | Resolved | CSV/video classification and artifact-validation-stage regressions pass. |
| The worker's final cumulative recoverable-error counter could be cleared or overwritten by a retiring live-analysis row before Review. | Major | `services/qt_workers.py`, `gui/main_window.py` | Separate live and recording newest-only buffers and drain the authoritative recording slot before complete/partial review and live restart. | Resolved | Exact regression publishes `pipeline_error_count=3`, then a later stale live row; Review still receives 3. |
| A delayed old complete/partial Review FunctionThread could overwrite a newer session's fields/output directory. | Major | `gui/main_window.py`, `gui/recording_tab.py` | Guard success/failure callbacks by review generation and resolved session path; invalidate/clear at a new Start. | Resolved | Delayed old-partial versus newer-complete success/failure regression passes. |

Final sign-off: **0 blockers, 0 majors, 0 minors**. Frozen focused suite: **97 passed in 37.54 s**; compilation passed.

## 2. Camera and image-processing correctness

Reviewer scope: backend/index selection, requested/actual mode and properties, unannotated frames, baseline generations/context, warm-up, HSV/ROI/localization rules, and graph value provenance.

| Finding | Severity | Affected files | Required correction | Resolution status | Evidence after correction |
|---|---|---|---|---|---|
| Automatic property names and saved/loaded controls were not consistently canonical across UI and OpenCV boundaries. | Major | `gui/main_window.py`, `gui/camera_tab.py`, `services/camera_service.py` | Map UI aliases once, use canonical `auto_exposure`, `auto_white_balance`, and `auto_focus`, and preserve readback diagnostics. | Resolved | Canonical alias/save-load/readback tests pass. |
| Baseline capture could retain frames or let stale calculation success mutate a newer generation; its lock did not initially exclude every conflicting action. | Major | `gui/main_window.py`, `gui/camera_tab.py`, `gui/roi_baseline_tab.py`, `gui/recording_tab.py` | Check generation before any mutation, hand immutable frames to the worker and release the list, lock all conflicting tabs, and make baseline/recording mutually exclusive. | Resolved | Stale generation cannot clear new frames; storage release and overlap/lock regressions pass. |
| Prerequisite settings/warm-up could advance from an issue-time timestamp, fail without unlocking, or wait forever if frames stopped. | Major | `services/qt_workers.py`, `gui/main_window.py` | Generation-tag setting success/failure, use successful readback time plus captured-frame monotonic timestamps, reattempt automatic-off controls, and add a bounded timeout. | Resolved | Baseline and recording setting-failure unlock plus warm-up timeout/captured-frame boundary tests pass. |
| Requested camera values could be exported as applied after unsupported/unread controls, and auto-only reattempts replaced earlier numeric diagnostics. | Major | `gui/main_window.py`, `processing/pipeline.py`, session metadata | Separate requested controls from confirmed actual controls, remove unavailable actuals, merge diagnostics by canonical property, and revalidate/export both sets. | Resolved | Unsupported gain never appears applied; exposure/gain diagnostics survive later auto-only attempts; recording/export compatibility tests pass. |
| Manual graph snapshots lacked fully authoritative timestamps/ranges and the spatial profile needed explicit ROI ticks/legend. | Major | `gui/main_window.py`, `gui/roi_baseline_tab.py`, `data/graph_exporter.py` | Carry host monotonic identity, export the exact displayed temporal range/values, and render ROI 1–9 ticks/legend without clipping. | Resolved | Exact manual spatial/temporal/profile companion tests and visual artifact inspection pass. |

Final sign-off: **0 blockers, 0 majors, 0 minors**. Focused camera/GUI review: **58 passed**; recording/export compatibility: **52 passed**; worker isolation: **14 passed**. The final project suite supersedes these with 279 passing tests. The later Arducam driver/property pass is recorded separately in the physical validation report.

## 3. Load-cell and synchronization correctness

Reviewer scope: firmware protocol/new-conversion behavior, HELLO/START/reset sessions, parsing/health, signed calibration/tare/verification, host monotonic timing, interpolation/nearest/gap, and UI transaction integrity.

| Finding | Severity | Affected files | Required correction | Resolution status | Evidence after correction |
|---|---|---|---|---|---|
| A valid HELLO did not fully require START acknowledgement, and midstream reset/session handling needed to preserve trial elapsed origin. | Major | `services/serial_service.py`, `services/qt_workers.py`, firmware tests | Send START after HELLO, require `OK,START`, create a new device session on reset, restart streaming, and preserve the host recording origin. | Resolved | Handshake/reset/sample-ID/device-session regressions pass. |
| Stream readiness/timeout diagnostics were not fully correlated with fresh DATA; disconnect/error cleanup needed to fail closed and be idempotent. | Major | `services/serial_service.py`, `services/qt_workers.py`, `gui/main_window.py` | Emit structured health changes, block until fresh DATA, abort active timeout with `HX711_TIMEOUT`, release the single owner, and recover only on fresh data. | Resolved | HELLO→NOT_READY→DATA→TIMEOUT→recovery gating and disconnect/timeout partial-session tests pass. |
| Retare could retain verification, and loaded calibration/provenance validation permitted stale semantic state. | Major | `core/models.py`, `processing/loadcell_calibration.py`, `gui/main_window.py`, `gui/loadcell_tab.py` | Preserve signed scale while updating zero, immediately invalidate verification, require current identity/provenance and ±5% verification. | Resolved | Negative factor, stable tare/no-scale-change, failed/pass verification, retare invalidation, and JSON round-trip tests pass. |
| The GUI calibration wizard was not strictly sequential and mass changes could leave old windows/results usable. | Major | `gui/loadcell_tab.py`, `gui/main_window.py` | Enforce unloaded→loaded→calculate→tare→verify; invalidate windows/calibration/verification on mass edits and restore loaded mass without emitting a user edit. | Resolved | Full public-button GUI workflow and known-mass invalidation tests pass. |
| Sample windows and delayed calculate/tare/verify callbacks could cross mass, disconnect, health, or device-session changes; retare could leave Start enabled. | Major | `services/qt_workers.py`, `gui/main_window.py`, `gui/loadcell_tab.py` | Add cancellable atomic windows, workflow generations, immutable serial/mass/calibration contexts, pending-job locks, stale success/failure rejection, and recording interlocks. | Resolved | Five exact transaction regressions plus combined focused suite **91 passed**; delayed callbacks cannot restore readiness. |

Final sign-off: **0 blockers, 0 majors, 0 minors**. Independent load-cell/synchronization review reported **74 focused passes**; final transaction-focused rerun reported **91 passes**. Physical Arduino/HX711/load-cell behavior remains unverified.

## 4. Data integrity and scientific reproducibility

Reviewer scope: raw/master/video identity, partials/finalization, provenance, column meanings, missing values, graph calculations/companions, filenames, and reproducible visual metadata.

| Finding | Severity | Affected files | Required correction | Resolution status | Evidence after correction |
|---|---|---|---|---|---|
| Runtime provenance and several session/graph schemas did not cover every required measured/requested field and companion header. | Major | `data/schemas.py`, `data/graph_exporter.py`, `processing/pipeline.py`, `services/session_recorder.py` | Record runtime Python/OpenCV/OS, requested width/height, nested session identity fallback, graph schemas, and exact companion headers. | Resolved | Runtime keys, graph CSV header coverage, requested mode, status, and session-ID tests pass. |
| Temporal/profile graph companions needed exact full-resolution timestamps/values, fixed ROI order, and peak-frame consistency. | Major | `data/graph_exporter.py`, graph tests | Use authoritative elapsed/host timestamps, preserve ROI 1–9, make peak profile use the peak heatmap frame, and keep display downsampling out of raw exports. | Resolved | Value-level audit confirms peak frame 65, exact temporal/master rows, exact heatmap/profile/contact/integrated values, and correct PNG dimensions. |
| First-definition-wins data-dictionary deduplication collapsed nine same-named fields with different frame/load/master semantics. | Major | `data/schemas.py`, `data/__init__.py`, schema/export/graph tests | Add `artifact_scope` and emit one row for every column in frame, master, load, spatial-companion, and temporal-companion scopes. | Resolved | Final smoke contains **647/647 unique `(artifact_scope, column_name)` keys** and exact header coverage for every core/graph CSV. |

Final sign-off: **0 blockers, 0 majors, 0 minors**. Schema/export/graph/recording/smoke set: **62 passed**; reliability/pipeline/performance set: **14 passed**. Final 120-frame artifact audit: 120 video/frame/master, 318 load, zero missing force, five graph bundles, no partials.

## 5. GUI usability, performance, and QA

Reviewer scope: beginner workflow, action gating, layout/scaling, Help, performance counters, complete/partial Review, async generations, and error recovery.

| Finding | Severity | Affected files | Required correction | Resolution status | Evidence after correction |
|---|---|---|---|---|---|
| Help exceeded the 1366×768 physical target at 150% scaling. | Major | `gui/help_dialog.py`, scale tests | Cap logical geometry by available screen and DPR; verify Close remains in bounds. | Resolved | Offscreen 100/125/150% Help geometry tests pass. |
| Camera and Load Cell tab minima clipped their pages at 150%. | Major | `gui/camera_tab.py`, `gui/loadcell_tab.py`, scale tests | Let page widgets shrink to the tab viewport and put overflow in internal scroll areas; inspect all four mapped bounds. | Resolved | All pages remain inside central/tab bounds. |
| Camera prerequisite errors/warm-up could strand workflow locks. | Major | `services/qt_workers.py`, `gui/main_window.py` | Tag setting failures and add bounded authoritative-frame warm-up failure paths. | Resolved | Baseline/recording unlock and timeout regressions pass. |
| Trial performance fields retained old rates/application-lifetime drops and did not count recoverable feature failures. | Major | `services/qt_workers.py`, `gui/main_window.py` | Reset at Preview/Start, use a trial drop baseline, publish cumulative recoverable errors, add terminal failures, and isolate live/recording buffers. | Resolved | Second-interval reset, stalled-GUI, recoverable/terminal counter, and stale-live overwrite regressions pass. |
| Partial sessions did not populate every Review field or enable Open Output. | Major | `gui/main_window.py` | Build a partial-aware off-thread summary from closed partial artifacts and guard it by session generation/path. | Resolved | Forced camera/serial partial integrations populate every Review label and Open Output. |
| Calibration actions were enabled after HELLO but before fresh healthy DATA. | Major | `gui/loadcell_tab.py`, `gui/main_window.py` | Separate transport-connected from stream-ready state and recover only on fresh DATA. | Resolved | HELLO/NOT_READY/DATA/timeout/recovery gating tests pass. |
| Sample windows were not atomic across mass edits/disconnect/overlapping actions. | Major | `services/qt_workers.py`, `gui/loadcell_tab.py`, `gui/main_window.py` | Add cancellation, disable conflicting actions, bind completion to current generation, and discard retained values. | Resolved | Window interleaving/mass reset/disconnect/restart tests pass. |
| Delayed calculation/tare/verification callbacks could reinstate stale calibration. | Major | `gui/main_window.py`, `gui/loadcell_tab.py` | Bind callbacks to generation, serial identity, mass, operation, and calibration ID; ignore stale success/failure. | Resolved | Delayed calculate/new-unloaded and verify/disconnect regressions pass. |
| Recording Start could remain enabled during retare or a pending wizard job. | Major | `gui/main_window.py`, `gui/loadcell_tab.py`, `gui/recording_tab.py` | Invalidate verification immediately and interlock Start/preparation until the operation ends and reverification passes. | Resolved | Verified→retare→reverify Start-interlock test passes. |

Final sign-off: **0 blockers, 0 majors, 0 minors**. GUI focused suite: **68 passed in 29.92 s**; six display combinations (1366×768 and 1920×1080 at 100%, 125%, and 150%) passed. Final narrow separate-buffer/review-generation regressions also passed. Native physical monitor behavior remains unverified.

## Final review conclusion

- Blocker findings discovered: **0**
- Blocker findings remaining: **0**
- Major findings remaining: **0**
- Minor findings remaining: **0**
- Final complete automated suite at the original review checkpoint: **279 passed, 0 failed, 0 skipped**
- Physical-hardware claims: **none**; use `HARDWARE_TEST_CHECKLIST.md`.

## 2026-08-03 integration update addendum

The application now accepts finite numeric load-cell streams without requiring `HELLO`, validates startup and live-stream deadlines independently, exposes raw serial evidence, binds calibration profiles to serial identity, and exports raw/tared/mass/calibration/synchronization provenance. The GUI uses a dual original/processed video workspace with nine control tabs, including independent Ender 3 motion control, a settings-free raw Camera View, baseline capture that does not depend on property readback, live Eulerian color/intensity magnification, and optional frame-aligned overlay/processed/magnified recordings. Earlier camera-control findings above are retained as historical review evidence for service code, not as current GUI behavior.

## Ender 3 repeated-press safety review — 2026-08-04

| Finding | Severity | Resolution | Evidence boundary |
|---|---|---|---|
| The reference prototype wrote serial from multiple paths and optimistically claimed position. | Blocker | Replaced by one queued writer, asynchronous reader, command UUID/lifecycle tracking, M115 identity gate, M114-only reported fields, and invalidation on faults. | Mocked serial tests; physical claims require the dated validation report. |
| Connection-time homing/motion and `G92` could silently alter the machine coordinate system. | Blocker | Connect sends only M115; G28 requires an explicit warning/approval; press zero is in-memory only and no G92 path exists. | Command-history/no-G92 tests. |
| Repeated motion could race GUI callbacks or continue after acquisition failure. | Blocker | Explicit state machine with generation/cycle IDs, stale-result guard, acquisition failure→abort signal, queue cancellation, M410 interruption, and bounded safe retract. | Controller and GUI integration tests. |
| Force safety could be applied to raw/unscaled values or too late to interrupt M400. | Blocker | Only finite calibrated Newton samples trip an inclusive absolute limit; COM3 observer requests M410 through the sole COM4 writer while active commands poll at 10 ms; latency limitations are documented. | Force mocked tests; physical latency remains checklist work. |
| Motion labels could be attached after processing and misrepresent transition timing. | Major | Immutable motion snapshots are captured at recorder ingress for each frame/sample and carried through frame, raw load-cell, and master schemas. | Schema/pipeline/recording tests. |
| Active press zero or direction confirmation could survive reconnect/profile load. | Major | Zero invalidates on reconnect/home/fault/emergency and is excluded from config/profile; direction confirmation is cleared on profile load and required at preview/start. | Model/GUI/service tests. |

Post-update verification: **318 passed, 0 failed, 0 skipped**. This includes no-`HELLO` parser/service tests, serial lifecycle/recovery, signed calibration and GUI transactions, injected camera capability/native-dialog paths, offscreen layout tests, motion parameter/filter/state/Nyquist tests, auxiliary-video frame alignment, export-schema provenance, and full simulation recording/finalization. Later physical Arducam, Nano/HX711, native-control, measured-FPS, calibration, recording, and USB-contention evidence is recorded in `PHYSICAL_HARDWARE_VALIDATION_20260803.md`; true optical latency remains unmeasured.
