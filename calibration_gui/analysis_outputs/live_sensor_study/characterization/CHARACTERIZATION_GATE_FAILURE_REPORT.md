# Live Sensor Characterization Gate Failure

Date: 2026-08-13  
Controlling plan at time of run: the former mixed `LIVE_SENSOR_GUI_MASTER_PLAN.md`  
Current controlling plan for this evidence: `SENSOR_CHARACTERIZATION_MASTER_PLAN.md`  
Characterization run: `characterization-dade466f019e6d15`

> **Governance annotation (2026-08-13):** This completed failure remains valid sensor-characterization evidence. After the planning split, it no longer blocks the independently governed manual-only force/localization GUI track. Its automatic-data-derived range/monotonicity result must not be used for application preprocessing, thresholds, `Fdetect`, `Fmax`, model selection, bundle promotion, or GUI validation. See `LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md` for the new manual-only application contract.

## Outcome

Under the former mixed plan, M2 stopped at the independent range and data-monotonicity gate. No safe common nine-ROI `Fmax` was established from that characterization procedure, so the former combined dependency did not permit force-model training, bundle promotion, or GUI integration.

This remains a failed characterization gate, not a missing analysis result. The run completed over 245 sessions and 179,176 source-aligned frames. Archive roles remained separate: 54 manual primary sessions, 162 automated characterization sessions, 11 dedicated no-contact sessions, and 18 replay-only sessions. It now governs characterization reporting only and does not decide whether a separately executed manual-only application analysis may proceed.

## Frozen range evidence

The range decision used automated loading data at 200 mm/min, fixed 0.25 N bins, session-balanced medians, and the preregistered requirement of at least three independent sessions in a supported bin.

| ROI | Supported bins | Highest supported bin (N) | Monotonic gate |
|---:|---:|---:|:---:|
| 1 | 4 | 0.875 | Fail |
| 2 | 4 | 0.875 | Fail |
| 3 | 5 | 1.125 | Fail |
| 4 | 4 | 0.875 | Fail |
| 5 | 3 | 0.625 | Fail |
| 6 | 6 | 1.375 | Fail |
| 7 | 6 | 1.375 | Fail |
| 8 | 7 | 1.625 | Fail |
| 9 | 4 | 0.875 | Fail |

The available higher-force bins were commonly supported by only the two paired 10-cycle/20-cycle sessions for a displacement condition. The plan explicitly forbids treating their cycles as additional independent sessions. All nine ROI also failed the frozen slope/bootstrap/reversal monotonic-evidence rule. Therefore `provisional_common_fmax_N` is null.

## Results that remain usable

- Archive integrity, source/session balance, layout identity, frame reconciliation, and split-leakage gates passed.
- No-contact noise was characterized from 11 independent recordings for every ROI.
- Complete 9×9 cross-talk, speed-stratified hysteresis, short-term fixed-displacement relaxation/settling, unloaded recovery, drift, and exploratory lag tables were generated.
- True force resolution remains **not established** because there is no randomized settled small-force-step protocol.
- Creep remains **not estimable** because the 0.5 s holds are fixed-displacement short-term relaxation, not qualified constant-force holds.

## Required next action

Do not relax or retune this failed characterization rule. Any new characterization claim over the intended range needs preregistered independent data that provide at least three sessions per ROI and supported force bin while directly resolving the failed increasing-light monotonicity premise. The separate application track must perform its own manual-only nested range/model procedure with new provenance; this run can neither block nor rescue that result.

Machine-readable evidence:

- `characterization-dade466f019e6d15/characterization_summary.json`
- `characterization-dade466f019e6d15/range_decision.json`
- `characterization-dade466f019e6d15/session_balanced_response.parquet`
- `characterization-dade466f019e6d15/plateau_summary.parquet`
