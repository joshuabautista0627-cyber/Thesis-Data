# Manual-Only Application Gate Failure Report

Date: 2026-08-13  
Controlling run: `manual-preprocessing-502142bacd6c35bb`  
Status: failed scientific preprocessing gate; model and GUI promotion stopped

## Source and lineage

- Allowed source only: `Manual Calibration (2).zip`
- Archive SHA-256: `e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a`
- Source manifest: `d5f12dff622e6a7dc8dab85ad2c48858b5422975b67138c91ede303e08c6574b`
- Feature specification: `38fa59a354306bf39c0b570f57e764f5638b19a3ecbdbd49b177b8df6ac56cf5`
- Split hash: `452064a320d05b6e8e8a6a2ba69f2ac6223aa4385db47dd5506eb9ec7ba14cc1`
- Session roles: 54 primary press, 11 dedicated no-contact, 18 replay-only
- Reconciled data: 83 sessions and 29,318 original-video frames, with zero missing or unmatched frames
- Forbidden archive and derived-lineage matches in the authoritative run: zero

## Controlling failure

Every outer-training fold and the all-development refit fails to derive an eligible `Fdetect`. Each contact threshold satisfies the frozen no-contact frame false-positive target, but no supported force range reaches the required 90% session-balanced contact recall.

| Fit | Selected lag | Training no-contact FPR | Best supported-bin recall |
|---|---:|---:|---:|
| Outer fold 1 | 260 ms | 4.96% | 76.56% |
| Outer fold 2 | 220 ms | 4.96% | 82.29% |
| Outer fold 3 | 240 ms | 4.91% | 84.62% |
| Outer fold 4 | 240 ms | 4.96% | 83.87% |
| Outer fold 5 | 240 ms | 4.99% | 85.71% |
| Outer fold 6 | 240 ms | 4.95% | 83.33% |
| Development refit | 240 ms | 4.96% | 85.16% |

The development refit's best recall occurs in the 2.75–3.0 N bin. Because it is below 90%, `Fdetect` and the common `Fmax` remain null.

## Independent range and support failures

The loading-only development range procedure is also ineligible:

- ROIs 1 and 3 lack three-session support in the 0.75–1.0 N bin.
- ROI 2 lacks three-session support in the 0.5–0.75 N bin.
- ROI 5 has a nonpositive initial low-force slope.
- ROI 7 has a plateau knee at 0.625 N and a training-only safe limit of 0.5625 N.
- The frozen inner-fold optical-support retention target is not met.

These failures are reported independently; none is rescued by the automatic archive or historical characterization outputs.

## Required disposition

Per the authoritative implementation plan, force/localization candidate comparison, bundle promotion, replay acceptance, and live GUI integration stop here. No threshold, range, support gate, or ROI may be weakened or omitted after seeing these results.

Progress requires new prospectively governed manual recordings that improve contact/no-contact separation and provide at least three independent training sessions in every required ROI/force bin. Any new evidence run must receive new config, source-manifest, feature-store, split, and preprocessing identities.
