# Optical Sensor Characterization — Authoritative Master Plan

**Status:** Authoritative for sensor-property analysis  
**Reviewed:** 2026-08-13  
**Datasets:** `Manual Calibration (2).zip` for every characterization metric except hysteresis and creep-related analysis; `Auto Calibration.zip` only for hysteresis and creep-related analysis  
**Purpose:** Quantify the behavior and limitations of the nine-ROI optical sensing skin for the thesis. This plan is independent of the live application/model implementation plan and cannot block or authorize that work.

## 1. Scope and authority

This document controls sensor-characterization analysis only. It covers sensitivity, linearity/nonlinearity, repeatability, spatial uniformity, cross-talk, no-contact noise, SNR, optical detection threshold, drift, system lag, hysteresis, and creep-related behavior.

### Non-negotiable archive assignment

```text
Manual Calibration (2).zip:
All characterization metrics except hysteresis and creep-related analysis

Auto Calibration.zip:
Hysteresis and creep-related analysis only
```

Do not use automatic-calibration sessions to calculate or strengthen sensitivity, local sensitivity, linearity/nonlinearity, plateau, general repeatability, spatial uniformity, cross-talk, no-contact noise, SNR, optical detection threshold, noise-equivalent force, resolution, drift, or the standalone system-lag result. Those metrics use the manual archive only. Automatic cycle repeatability, speed effects, cycle-local force correction, hold behavior, and unloaded recovery may be calculated only as supporting components or quality checks inside the hysteresis and creep-related analyses; they are not separate general sensor metrics sourced from the automatic archive.

Characterization results may be reported as thesis evidence, including unfavorable or failed results. They must not supply application-model preprocessing, contact thresholds, model selection, training/validation samples, optical-support limits, or a deployed force range. The two development tracks are parallel: completion or failure here is not a prerequisite for the separate live sensor implementation.

Explicitly outside this plan:

- force/localization model comparison or promotion;
- application screens, runtime state handling, packaging, or usability gates;
- multi-contact, shear-force, or millimetre-accurate localization claims;
- a numerical true constant-force creep claim unless a separate qualifying automatic-calibration protocol is collected; and
- true force resolution unless a separate randomized settled-step protocol is collected.

## 2. Fixed image and ROI contract

All quantitative measurements use unannotated original `session_video.mp4` frames and matching baselines. Existing motion-magnified quantitative exports are exploratory only.

```text
raw camera frame 640×480
→ rotate 90° clockwise
→ mirror horizontally
→ final analysis frame 480×640
```

Canonical layout ID: `roi-9fbc67c50ee3bc7145fa`.

| ROI | x | y | width | height |
|---:|---:|---:|---:|---:|
| 1 | 98 | 161 | 82 | 89 |
| 2 | 197 | 156 | 76 | 99 |
| 3 | 312 | 154 | 58 | 97 |
| 4 | 100 | 268 | 73 | 89 |
| 5 | 204 | 264 | 76 | 96 |
| 6 | 314 | 260 | 66 | 94 |
| 7 | 104 | 383 | 73 | 77 |
| 8 | 194 | 394 | 87 | 66 |
| 9 | 312 | 379 | 61 | 77 |

The coordinates are already expressed after rotation and mirroring. Do not transform the boxes twice. Different rectangle dimensions are intentional; use both integrated and area-normalized optical response when comparing ROIs. ROI 1–9 have equal primary manual session coverage, and ROI 9 must not be singled out based on session count. Recorded sharpness is non-blocking provenance and is never an exclusion criterion by itself.

## 3. Dataset roles

### 3.1 Manual archive: static and independent-session evidence

`Manual Calibration (2).zip` contains 83 synchronized sessions and 29,318 frames. Its controlling characterization roles are:

- 54 primary press sessions: six complete TEST sessions for each ROI 1–9;
- 11 dedicated no-contact sessions that observe all nine ROIs simultaneously; and
- 18 replay-only sessions, usable for pipeline checks but not promoted to independent characterization replicates unless their protocol is separately qualified.

Use the manual press/no-contact sessions for:

- static loading response and per-ROI force-light curves;
- low-force and local sensitivity;
- linearity/nonlinearity and plateau observations;
- between-session repeatability;
- spatial uniformity and cross-talk;
- dedicated no-contact noise, SNR, and optical detection threshold;
- short-term no-contact drift; and
- approximate camera/acquisition/load-cell system lag.

The independent analysis grain is the complete session or TEST group, never an adjacent video frame. All six TEST-group results must be visible where relevant. Camera-setting strata remain in provenance, with sharpness non-blocking.

### 3.2 Automatic archive: hysteresis and creep-related evidence only

`Auto Calibration.zip` contains 162 complete sessions and 149,858 synchronized frames: 18 sessions for every ROI 1–9. For each ROI, the design crosses:

- displacement: `1.75`, `2.625`, and `3.5 mm`;
- speed: `200`, `400`, and `600 mm/min`; and
- repetition plan: one 10-cycle and one 20-cycle session per exact ROI × speed × displacement condition.

Each session contains explicit phases/cycle IDs, a matching baseline, a `0.5 s` fixed-displacement bottom hold, and a `2–3 s` unloaded top dwell.

The automatic archive is permitted only for:

- loading/unloading hysteresis and matched-light force separation;
- `200/400/600 mm/min` speed dependence as a stratification of hysteresis, not as a separate general sensor metric;
- within-session cycle repeatability as an internal quality check for the hysteresis calculation, not as standalone repeatability evidence;
- short-term fixed-displacement relaxation/optical settling as **creep-related exploratory evidence**;
- unloaded recovery and residual offset as **creep-related exploratory evidence**; and
- cycle-local reference-force zero and timing correction required only to align the hysteresis and creep-related trajectories.

Do not use this archive for sensitivity, local sensitivity, linearity/nonlinearity, plateau or usable range, general repeatability, spatial uniformity, cross-talk, no-contact noise, SNR, optical detection threshold, noise-equivalent force, true resolution, drift, or the published standalone system-lag metric. Even if an automatic output for one of those quantities already exists historically, it is not controlling evidence under this plan.

Use the hierarchy:

```text
archive → collection day → complete session/condition → cycle → frame
```

Frames and cycles are repeated observations. For the permitted hysteresis and creep-related analyses, estimate each cycle, average cycles within its complete session, and then compare sessions/days. A 10-cycle or 20-cycle session is one independent session, never 10 or 20 independent replicates. Each exact speed × displacement condition currently has only two sessions; condition-specific intervals therefore remain descriptive unless a third independent run is collected. Because there are only two collection days, day-level population intervals are also descriptive.

## 4. Quantitative measurement contract

For ROI `r`, frame `t`, current HSV V image `V`, and unloaded per-pixel median baseline `B`:

```text
signed_delta_r,t(x,y)   = V_t(x,y) - B_r(x,y)
positive_delta_r,t(x,y) = max(signed_delta_r,t(x,y), 0)
raw_light_r,t           = sum over ROI r of positive_delta_r,t(x,y)
light_r,t               = max(raw_light_r,t - no_contact_floor_r, 0)
```

Preserve signed delta summaries for noise/SNR and positive-delta summaries for response analysis. At minimum retain per ROI: signed sum/mean/median/MAD, positive sum/per-pixel mean, active fraction, centroid, raw V mean/median/max, ROI area, and baseline ID. The immutable feature store must not bake in globally fitted no-contact floors, scales, thresholds, lag, or range decisions.

Calculate the published standalone optical/load-cell system-lag metric from complete manual sessions only. Preserve raw timestamps, report the sign convention and lag curve, and label it camera + acquisition + synchronization + material system lag rather than intrinsic material response time.

For automatic cycles, estimate a cycle-local force offset from the immediately preceding unloaded dwell and preserve both raw and corrected force. An automatic timing correction may also be estimated, but only as an internal alignment step for hysteresis and creep-related trajectories; do not publish it as the standalone system-lag result. Use `200 mm/min` as the primary slower hysteresis condition and `400/600 mm/min` only to stratify hysteresis by speed; never pool speeds without showing the strata.

## 5. Required characterization metrics

| Metric | Permitted archive and calculation | Qualification |
|---|---|---|
| Low-force sensitivity | **Manual only** — loading-response slope `Δlight/ΔF` and area-normalized equivalent in the initial supported region | At least three independent manual sessions per supported bin/ROI |
| Local sensitivity | **Manual only** — derivative or adjacent-bin slope of session-balanced monotonic response | Supported manual bins only; no interpolation across gaps |
| Linearity/nonlinearity | **Manual only** — maximum deviation from the stated low-force straight line divided by observed full-scale light span | State ROI and force interval; separate from model error |
| Plateau and usable-range observations | **Manual only** — session-balanced per-ROI loading response and prespecified knee/support rule | Observation/characterization output only; state unsupported bins and do not import automatic range evidence |
| General repeatability | **Manual only** — between-session SD and robust spread by ROI/force/phase, with `%FS` | Equal manual-session influence; CV only where mean is safely above zero |
| Spatial uniformity | **Manual only** — across-ROI sensitivity, noise, repeatability, and observed plateau comparison | Show raw and area-normalized results for all ROI 1–9 |
| Cross-talk | **Manual only** — off-target response relative to target and total response during labelled single-ROI presses | Complete 9×9 matrices and force-bin summaries |
| SNR | **Manual only** — manual contact signal versus dedicated manual no-contact signed-light MAD | State linear/dB convention and independent-session counts |
| Optical detection threshold | **Manual only** — smallest supported force region separated from dedicated manual no-contact evidence under a frozen recall/FPR rule | Characterization result only; not an application threshold |
| Noise-equivalent force | **Manual only** — low-force noise divided by low-force sensitivity, plus conservative `3× noise/sensitivity` | Effective noise-limited proxy; never label true resolution |
| Drift | **Manual only** — robust unloaded light slope and total change versus time; optional equivalent N/min | State duration; convert only with qualified manual sensitivity |
| Approximate standalone system lag | **Manual only** — time offset between optical and reference response | Report in ms with system-level interpretation |
| Hysteresis | **Auto only** — loading minus unloading light at matched force, `%FS`, and force separation at matched light | Average cycles within session; speed is a hysteresis stratum; cycle repeatability is internal QA only |
| Short-term fixed-displacement relaxation/settling | **Auto only — creep-related exploratory evidence** — optical and force change during the `0.5 s` fixed-displacement hold | Not constant-force creep; note approximately 7–8 frames and internal timing correction |
| Unloaded recovery and residual offset | **Auto only — creep-related exploratory evidence** — response during the `2–3 s` top dwell | Not a creep value and not independent no-contact evidence |

### Metrics requiring new protocols

- **True creep — future automatic-calibration protocol only:** collect constant-force holds of at least 5 seconds and preferably 30–60 seconds, stable within ±0.10 N, with at least three independent sessions per ROI/force level. The present `0.5 s` fixed-displacement holds do not qualify, so report no numerical creep value.
- **True resolution — future manual-characterization protocol only:** collect randomized settled increasing/decreasing small-force steps, initially near 0.05 N and refined using the manual-only noise-equivalent-force proxy. Require reference uncertainty materially below the proposed resolution and at least three independent manual sessions per ROI. Continuous ramps do not establish this metric.

## 6. Workflow and evidence products

1. Verify immutable ZIP hashes, member CRC/size, source manifests, layout IDs, baselines, frame counts, and archive roles.
2. Reprocess selected original videos through the shared signed/positive feature extractor; reason-code every excluded/unmatched frame and preserve archive lineage.
3. Produce manual coverage audits for all general characterization metrics. Produce automatic condition/cycle coverage only to establish the sufficiency of hysteresis and creep-related trajectories.
4. Calculate every non-hysteresis/non-creep metric from manual sessions only. Calculate automatic hysteresis and creep-related exploratory outputs independently; never merge archive denominators or confidence intervals.
5. Save per-session values before aggregation. For manual metrics retain phase, force bin, ROI, TEST/session, and settings. For automatic hysteresis/creep-related work additionally retain speed, displacement, cycle, dwell/hold phase, collection day, and internal correction provenance.
6. Generate exact tables plus thesis-ready graphs with source-table/config/code hashes.
7. Assign every metric one status: `primary`, `conditional-qualified`, `exploratory-insufficient`, or `not-established`.
8. Preserve failures and insufficiencies without changing thresholds after viewing results.

Required figures include:

- manual coverage and force-bin heatmaps for all general metrics;
- manual per-ROI response, sensitivity, nonlinearity, plateau, repeatability, uniformity, cross-talk, no-contact, SNR, detection-threshold, drift, and standalone-lag plots;
- automatic condition/cycle coverage only for hysteresis and creep-related sufficiency;
- automatic hysteresis loops faceted by speed/displacement, with cycle repeatability shown only as internal QA; and
- automatic bottom-hold relaxation and top-dwell recovery traces labelled `creep-related exploratory evidence`; and
- qualification-aware metric summary tables.

Every final figure exports PNG plus SVG/PDF, plotted rows as CSV, and metadata JSON containing source hash, filters, units, independent-session count, uncertainty method, seed, and creation time.

## 7. Existing evidence and governing status

The completed historical run `characterization-dade466f019e6d15` remains unchanged and retains its original provenance. It processed 245 sessions and 179,176 source-aligned frames while retaining separate roles: 54 manual primary, 162 automatic characterization, 11 dedicated no-contact, and 18 replay-only sessions.

Its former common-range/monotonicity gate failed using automatic `200 mm/min` data. Preserve that result as a historical finding of the former combined procedure, but it is not controlling plateau, range, monotonicity, or other non-hysteresis/non-creep evidence under this revised manual-first characterization plan. It also does not block or set the range of the independently governed manual-only implementation track.

Within the revised boundary, historical manual-derived no-contact noise, cross-talk, drift, lag, and other general metrics remain candidates for use only when their exact manual lineage and calculation comply with this plan. Historical automatic-derived outputs remain controlling only for speed-stratified hysteresis and creep-related short-term fixed-displacement settling/recovery. Any automatic-derived plateau, range, monotonicity, general repeatability, drift, lag, noise-equivalent-force, or resolution-proxy result is historical only and must not be presented as a controlling characterization result. True force resolution is not established, and true creep is not estimable from the current fixed-displacement holds.

## 8. Validation and stop conditions

Before accepting characterization outputs:

- verify original frames, correct layout/transform, and unchanged archive hashes;
- reconcile every frame or attach a reason code;
- verify manual complete-session/TEST aggregation for general metrics and no frame-level confidence intervals;
- verify automatic cycle→session→day aggregation only for hysteresis and creep-related analysis; automatic cycle counts never become independent replicates;
- keep manual and automatic populations visibly separate and verify the source assignment of every reported metric;
- show all ROI 1–9 and retain all sharpness variants;
- verify automatic loading/unloading matching, cycle-local zero/timing correction, and speed strata are used only for hysteresis or creep-related analysis;
- verify figures against exact tables and render them for clipping/order/unit errors; and
- prevent creep/resolution or condition-level precision claims when their protocol gates fail.

Stop and report, rather than silently repair, if an archive changes, original-frame alignment fails, the layout differs, cycle/session identity is ambiguous, or an evidence denominator cannot be reconstructed. A failed or unfavorable metric is a valid outcome and is never permission to alter the separate application/model track.

## 9. Definition of done

Characterization is complete when the manual archive reproducibly supports every general metric, the automatic archive is used only for qualified hysteresis and creep-related exploratory evidence, every output has per-session/per-ROI lineage and qualification, automatic cycles are nested correctly, required tables/figures pass numerical and visual QA, and unsupported creep/resolution claims are explicitly withheld. Completion does not imply that any application force model is valid or invalid.
