# Strongest supporting evidence

- All 29,318 accepted camera frames reconcile with synchronized records and decoded primary video.
- All 42,568 raw load-cell samples reconcile; required measurements and identifiers are complete.
- No duplicate recording/frame keys, missing within-recording IDs, or non-increasing host clocks were found.
- Force interpolation and synchronization offsets reproduce directly from raw load-cell timestamps.
- Each taxel has eight press recordings; four unique baseline identities and calibration metadata are traceable.
- The later six-fold design has no recording/frame overlap between training and held-out groups.

# Limitations to disclose

- One collection day, one source session S3 and one skin identifier do not support independent-session generalization.
- Physical press boundaries are unannotated; repeated force excursions cannot be equated with single trials or threshold runs.
- Optical measurements are configured as motion-magnified; raw optical agreement and intrinsic latency remain unverified.
- Manual force CV and baseline shifts confound a pure sensor-repeatability interpretation.
- Only a 200 g calibration/verification point is documented, while forces reach 22.699 N.
- Eight no-contact trials retain the contradictory interaction label Press.
- Legacy model results use within-recording blocks and carry dependence/leakage risk.
- The archived predictor covers only 0.127% of contact frames; conditional 100% accuracy must be reported with that coverage.
- Completeness is relative to accepted counters, not the unavailable planned experiment or pre-acceptance capture losses.
