# Calibration datasets

This repository includes the complete processed automatic and manual calibration datasets used by the thesis analysis.

## Dataset location

`calibration_gui/analysis_outputs/live_sensor_study/feature_store/feature-store-c0ec762f1dc888a7/`

The feature store contains:

- 162 automatic calibration sessions with 149,858 frame-level rows.
- 83 manual calibration sessions with 29,318 frame-level rows.
- 245 sessions and 179,176 rows in total.
- One Parquet file per session, plus CSV/Parquet session indexes, an event log, a reconciliation report, and a hash manifest.

The automatic session files occupy 122,741,880 bytes. The manual session files occupy 28,868,879 bytes. No processed dataset file exceeds 1.34 MiB.

## Source provenance

The processed datasets were derived from these immutable source archives:

| Dataset | Source archive | Size | SHA-256 |
| --- | --- | ---: | --- |
| Automatic | `Auto Calibration.zip` | 31,171,632,642 bytes | `f81a41b635aa2b893e9fa1faff0bda2db41ad0717d8c134fb6d907e1471e344d` |
| Manual | `Manual Calibration (2).zip` | 1,283,430,583 bytes | `e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a` |

The original automatic ZIP is 29.03 GiB, which exceeds GitHub's per-file Git LFS limit and the included LFS storage allowance. The raw ZIP archives are therefore not committed. The repository includes the complete normalized frame-level datasets and the source hashes needed to verify provenance.

## Integrity

`reconciliation_report.json` records a passing dataset gate:

- All 245 sessions were checkpointed.
- All 179,176 source video frames were decoded with zero missing or unmatched frames.
- Source archive hashes were unchanged before and after processing.
- No fold-fitted thresholds, scales, imputation values, or model transforms were stored in the feature dataset.

`run_manifest.json` contains SHA-256 hashes for every output file and the exact feature, configuration, and source-manifest identities used to create the dataset.
