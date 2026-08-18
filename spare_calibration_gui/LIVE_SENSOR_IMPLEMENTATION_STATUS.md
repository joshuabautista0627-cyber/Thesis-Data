# Live Sensor Work Status

This ledger tracks two independent authoritative plans:

- [LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md](LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md) — manual-only application/model track.
- [SENSOR_CHARACTERIZATION_MASTER_PLAN.md](SENSOR_CHARACTERIZATION_MASTER_PLAN.md) — sensor-characterization track using both archives in distinct roles.

The former [LIVE_SENSOR_GUI_MASTER_PLAN.md](LIVE_SENSOR_GUI_MASTER_PLAN.md) is a routing index only. A failure in either track does not block or authorize the other. Physical and participant-dependent evidence is never inferred from software tests.

## Shared verified prerequisites

| Task | Owner | Status | Evidence | Result / limitation |
|---|---|---|---|---|
| Repository/environment preflight | Codex | Verified | `preflight-20260813`; legacy baseline 420 passed; current v4 plus reporting total 484 tests verified across split runs; `pip check` passed | Existing Git repository has no commits and project files are untracked; preserved unchanged. A monolithic Windows run can still expose an unrelated native pyqtgraph garbage-collection access violation or recording-finalization timing flake; each affected test passes in isolation. |
| Immutable source preflight | Codex | Verified | Manual SHA-256 `e039...006a`, 2,727 CRC-valid members; automatic SHA-256 `f81a...344d`, 5,545 CRC-valid members | Sources were streamed rather than fully extracted and remain immutable. |
| Canonical ROI/feature scaffold | Codex | Verified | `assets/live_sensor/roi_layout.json`; feature-spec hash `38fa59a3...56cf5`; signed/positive extractor tests | Canonical layout is `roi-9fbc67c50ee3bc7145fa`; the legacy calibration layout remains separate. |
| Read-only sensor discovery | Codex | Verified for discovery only | Arducam IMX179, `usbvideo.inf`, DirectShow index 0, YUY2 640×480 frame read | Operator has not yet confirmed current ROI alignment or unloaded state; no application baseline/physical validation claim. |

## Sensor-characterization track

| Milestone | Owner | Status | Command / run ID | Evidence | Result / next action |
|---|---|---|---|---|---|
| Source inventory and feature reconciliation | Codex | Complete | `archive-audit-a3f75ebef480d214`; `feature-store-c0ec762f1dc888a7` | `analysis_outputs/live_sensor_study/source_inventory/` and `feature_store/` | 245 sessions and 179,176 rows reconciled with no missing/unmatched frames. This combined feature store is characterization evidence, not a manual-only application artifact. |
| Historical split manifest | Codex | Complete | `splits-fc7de7cf0b88479e` | `analysis_outputs/live_sensor_study/splits/splits-fc7de7cf0b88479e/leakage_audit.json` | Complete-session exclusivity passed for the former combined study. Do not reuse its identity as the application split. |
| Characterization analysis and former range gate | Codex | Complete — failed scientific range gate | `characterization-dade466f019e6d15` | `analysis_outputs/live_sensor_study/characterization/characterization-dade466f019e6d15/` and `CHARACTERIZATION_GATE_FAILURE_REPORT.md` | The automatic 200 mm/min evidence did not establish the former common range/monotonicity gate. Preserve this as a characterization finding. It does not block or set the manual-only application range. |
| Characterization thesis outputs | Human + Codex | Partially complete | Existing summary/tables | Characterization output directory | Cross-talk, no-contact noise, hysteresis, short-term settling/recovery, drift, and exploratory lag remain usable after qualification. True creep and true resolution remain unestablished without new protocols. |

## Manual-only GUI/model track

| Milestone | Owner | Status | Evidence required | Gate / exact next action |
|---|---|---|---|---|
| G0 — Manual-only governance/source firewall | Codex | Complete | `config/live_sensor_manual_only.json`; `contracts/live_sensor_manual_only_contract.schema.json`; firewall/lineage tests | The allowed archive is the sole configured source and all new writable roots use `analysis_outputs/live_sensor_manual_only/`. Forbidden archive names, second sources, shared output roots, and forbidden artifact lineage fail closed before source reads. |
| G1 — Manual-only inventory, feature store, and splits | Codex | Complete | `archive-audit-dd1ed9cb68dab34b`; `feature-store-fa2500440adab6ca`; `splits-da608c2b8581adff`; split hash `452064a3...14cc1` | 83 sessions and 29,318 original-video frames reconciled exactly. The source roles are 54 primary, 11 no-contact, and 18 replay-only; six TEST folds and 2/2/2/2/2/1 whole-session no-contact assignments pass leakage checks. |
| G2 — Manual-only range, contact, force, and localization comparison | Codex | Stopped at failed preprocessing gate | `manual-preprocessing-502142bacd6c35bb`; `MANUAL_ONLY_APPLICATION_GATE_FAILURE_REPORT.md` | All six training folds and development fail to establish `Fdetect`; best force-bin contact recall is 76.56%–85.71% versus 90%. Development also lacks an eligible common range and the optical-support retention gate fails. Candidate comparison was not run. |
| G3 — Experimental recovery bundle and replay/live GUI | Codex | Implemented with explicit non-eligibility disclosure | `experimental-hybrid-bedb31cb1e7c1541`; `models/live_sensor_experimental_hybrid_v4`; v1/v2/v3 retained as rollback bundles; grouped force/localization candidate audits plus safe-loader/replay/state/UI/report tests | V4 is the default camera-only hybrid contract: v3 event detection/localization plus v2 monotonic approximate force. Runtime inputs are exclusively camera-derived signed-light and active-fraction features from the mechanoluminescent skin; load-cell and printer inputs are forbidden by the bundle contract. Each rod now has an independent unloaded-normalized gate and hysteresis, so a simultaneous event can highlight and export multiple ROIs; Newton force is withheld for multi-press events. Replaying the combined gate/fallback rule on the 102 reused single-press events gives 100% session-balanced exact-set accuracy when displayed at 83.08% coverage. The archive has no labeled simultaneous presses, so multi-press accuracy is not established. Across reused outer folds: max no-contact frame FPR 2.44%, minimum contact recall 94.20%, event ROI macro F1 93.43%, conditional force MAE 0.328 N, and p95 force error 0.643 N. Force resolution remains failed. |
| G4 — Connected-sensor physical and usability validation | Human + Codex | Awaiting operator-led camera/skin evidence | Confirm unloaded skin; baseline; every-ROI/reconnect/latency/soak evidence; representative-user results | Deployment requires only the camera and mechanoluminescent skin. A load cell/printer is neither a runtime prerequisite nor an inference input. Separate reference instrumentation could be used in a future research-validation protocol but is outside the deployed live sensor. |
| G5 — Validated application release | Human + Codex | Awaiting G4 | All manual-only scientific, software, physical, performance, accessibility, and usability gates; exact release hashes | Promote to `validated` only when every applicable gate has traceable evidence. |

## Current controlling next action

For best-effort use, connect only the camera and mechanoluminescent skin, open **Live Sensor (Experimental)**, accept an unloaded optical baseline, and complete the 120-frame camera warm-up. During contact v4 highlights every independently active rod. It shows approximate in-support force only for a single press; multi-press, out-of-support force, and low-confidence ROI are withheld as appropriate. No load-cell or printer calibration is required or consumed. Retain `manual-preprocessing-502142bacd6c35bb` as the controlling failed confirmatory result: v4 does not establish a common safe force range, force resolution, or multi-press accuracy and cannot be promoted to `validated` without a separate prospective research protocol.

Manual calibration preprocessing now has an explicit session-safe timestamp-alignment contract (`manual-preprocessing-952f8d4e26e4d95b`). Its numerical lag/contact/range/support tables are identical to the controlling historical run, and it retains the same 260/220/240/240/240/240 ms outer-fold lags plus 240 ms development lag. This implementation evidence does not replace the original frozen result or change v4 metrics.
