# Live Sensor Planning Index

**Status:** Routing index only — no longer an authoritative or controlling plan  
**Superseded:** 2026-08-13

The previous mixed plan combined sensor characterization with live GUI/model development and caused the two calibration archives to be treated as one dependency chain. That mixed plan has been replaced by two independent authoritative documents:

1. [LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md](LIVE_SENSOR_GUI_IMPLEMENTATION_PLAN.md) controls contact detection, force estimation, fixed-ROI localization, model comparison, bundle creation, GUI implementation, replay/live hardware integration, physical testing, and usability validation. Its model/data boundary is strictly manual-only.
2. [SENSOR_CHARACTERIZATION_MASTER_PLAN.md](SENSOR_CHARACTERIZATION_MASTER_PLAN.md) controls sensor-property analysis such as sensitivity, nonlinearity, repeatability, cross-talk, SNR, drift, hysteresis, speed effects, cyclic repeatability, short-term relaxation/settling, recovery, and qualified resolution/creep reporting.

## Routing rule for Codex and contributors

- For any force/localization model or live application task, read and follow only the GUI implementation plan.
- For any sensor-metric or thesis characterization task, read and follow only the characterization plan.
- Neither plan is a prerequisite for the other, and a failed gate in one track does not stop or authorize the other.
- Do not reconstruct the removed mixed dependency by sharing thresholds, force ranges, fitted preprocessing, model artifacts, or release decisions across the two tracks.

Historical artifacts retain their original provenance and conclusions. Consult [LIVE_SENSOR_DECISION_LOG.md](LIVE_SENSOR_DECISION_LOG.md) and [LIVE_SENSOR_IMPLEMENTATION_STATUS.md](LIVE_SENSOR_IMPLEMENTATION_STATUS.md) for the split decision and current track status.
