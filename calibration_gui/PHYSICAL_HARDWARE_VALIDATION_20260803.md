# Physical Hardware Validation — 2026-08-03

This report records tests actually executed on the experiment computer. It does not convert simulation evidence into a physical claim. The operator confirmed that the intended experimental camera is the **Arducam IMX179 Camera Module**, so former GXIVISION labels in the application were corrected.

> Subsequent workflow update: the operator-facing camera mode/property settings, native-properties action, profiles, capability tables, and setting-readback readiness gate were removed. The driver-control results below remain historical physical evidence only. The current GUI opens the driver-selected stream without camera mode/property writes, shows the raw frame as received, and allows baseline capture from live frames plus valid ROIs.

## Test record

| Item | Recorded value |
|---|---|
| Date and time zone | 2026-08-03, Asia/Taipei (UTC+08:00) |
| Computer OS | Microsoft Windows NT 10.0.26200.0 |
| Application version | 1.0.0 |
| Camera | Arducam IMX179 Camera Module, index 0, USB VID:PID `1BCF:0B12` |
| Camera driver | Microsoft provider, version `10.0.26100.8875`, driver date 2006-06-21 as reported by Device Manager |
| Serial device | CH340 USB serial, COM3, USB VID:PID `1A86:7523`, 115200 baud |
| Firmware identity | `HX711_NANO`, protocol 1.0, firmware 1.0.0 |
| HX711 wiring/rate | DOUT D4, SCK D5, approximately 10 SPS hardware mode |
| Calibration ID | `c87fc15d-2a7e-49fb-b07c-acf4c43f440e` |
| Accepted physical session | `output/physical-arducam-com3-20260803T105013Z` |

The camera serial/asset ID, Nano asset ID, load-cell model/capacity/serial, and traceable-mass certificate ID were not available from software and remain operator/lab-record fields.

## Camera identity, backend, and native controls

Windows PnP and safe frame probes identified index 0 as the Arducam. Index 1 was the laptop webcam and was not used. Both OpenCV backends initially opened at 640×480. DirectShow was retained because it repeatedly recovered and completed the sustained test; Media Foundation initially measured near 30 FPS but later produced grab/open hangs after native-dialog/reconnect testing.

DirectShow’s nominal FPS property read back as 30.00003 FPS, but the authoritative 60-second measured cadence was 14.98485 FPS. This discrepancy is preserved rather than presenting the driver’s nominal value as achieved throughput.

| DirectShow sustained result | Value |
|---|---:|
| Requested mode | 640×480 at 30 FPS |
| Confirmed dimensions | 640×480 |
| Frames over 60 seconds | 901 |
| Measured overall FPS | 14.98485 |
| Start / middle / end FPS | 14.98822 / 14.97409 / 14.98524 |
| Frame interval min / median / p95 / max | 60.514 / 64.105 / 79.980 / 81.105 ms |
| Blocking camera-read median / p95 / max | 64.100 / 79.983 / 81.093 ms |

The DirectShow native property page opened as **Arducam IMX179 Camera Module Properties** and the stream delivered fresh frames after the dialog closed. The page exposed brightness 128, contrast 102, hue 128, saturation 153, sharpness 255, gamma 110, white balance 4600, backlight compensation 4, gain 0, 60 Hz anti-flicker, exposure −6, zoom/focus/pan/tilt/roll 0, and low-light compensation. Aperture and ColorEnable were disabled. No separate Arducam SDK or additional vendor-only control panel was installed.

DirectShow same-value writes and readbacks were confirmed for exposure, automatic exposure, gain, brightness, contrast, saturation, sharpness, gamma, white balance, automatic white balance, focus, automatic focus, hue, backlight compensation, zoom, pan, tilt, and roll. Iris and temperature writes were rejected and retained as unsupported/unconfirmed. Media Foundation rejected white-balance, focus, zoom, pan, tilt, roll, and iris writes in the captured matrix; it did not falsely confirm them.

Evidence: `output/hardware_camera_probe_20260803/camera_controls.json`, `camera_index_0_directshow.png`, native-dialog probe outputs, and `sustained_camera_serial_probe.json`.

## Nano upload, protocol, reset, and HX711 cadence

The firmware compiled and uploaded using:

- Arduino CLI 1.5.2-rc.1
- Arduino AVR core 1.8.8
- HX711 Arduino Library 0.7.5
- FQBN `arduino:avr:nano:cpu=atmega328old`
- COM3 at 115200 baud

The hardware wiring was resolved as DOUT D4 and SCK D5. Other attempted pin pairs produced invalid zero readings and were not retained. The final firmware produced live nonzero `DATA` rows and passed HELLO identity, PING/PONG, STATUS, STOP (no DATA after acknowledgment), START, and reset/sample-ID restart checks.

During the simultaneous 60-second Arducam/COM3 run, 661 serial samples were received with zero ID gaps and no serial errors. The steady median interval was 91.847 ms (approximately 10.89 Hz), p95 was 92.128 ms, and maximum was 93.691 ms. A 0.190 ms minimum came from startup-buffered protocol data and is not a physical HX711 conversion interval. No stale sample ID was repeated.

Destructive wiring-based `NOT_READY`/timeout fault injection and unplugging COM3 during a disposable recording were not performed, because they require explicit lab authorization and could disturb the verified wiring/session.

## Physical calibration, fixture retare, and verification

The initial 200 g calibration produced:

| Calibration result | Value |
|---|---:|
| Unloaded mean ± SD | 56052.600 ± 22.748 counts |
| Loaded mean ± SD | 178197.388 ± 25.410 counts |
| Used samples | 85 unloaded / 85 loaded |
| Span | 122144.788 counts |
| Signed factor | +610.723941 counts/g |
| Calibration SNR | 4807.043 |
| Loaded-window CV | 0.020803% |
| Quality | Passed |

Installing/settling the normal sensing fixture introduced a stable 54.216 g preload relative to the earlier tare. The recording preflight correctly rejected that nonzero state. The fixture was retared at 89201.518 ± 28.025 counts without changing the scale factor, and the prior verification was invalidated as required.

The independent post-retare 200 g verification measured **199.0146 ± 0.0450 g**, an absolute error of 0.9854 g or **0.4927%**, passing the configured ±5% limit. The application profile was saved and round-trip loaded with COM3, baud, protocol, firmware, calibration ID, signed factor, new tare, and verification evidence intact.

Evidence: `output/hardware_calibration_20260803`. The pre-fixture-retare profile is preserved as `physical_loadcell_calibration_before_fixture_retare.json`.

## Concurrent USB acquisition and physical recording

The 60-second concurrent test completed with 901 Arducam frames, continuous COM3 sampling, zero sample-ID gaps, and no camera/serial ownership exception. This verifies sustained simultaneous operation on the tested USB configuration.

The accepted single-press session is `output/physical-arducam-com3-20260803T105013Z`:

| Session audit | Result |
|---|---:|
| Unloaded preflight median | −0.0106 N (passed ±0.20 N gate) |
| Accepted/video/feature/master rows | 161 / 161 / 161 / 161 |
| Load-cell samples | 125 |
| Missing force / invalid synchronization rows | 0 / 0 |
| Synchronization gap median / p95 / maximum | 24.943 / 42.830 / 45.940 ms |
| Peak force | 5.0935 N at 4.8298 s |
| Final two-second median force | −0.0087 N |
| Measured recording FPS | 15.595 |
| Median processing and write time | 13.247 ms/frame |
| Video streams | Original, original+overlays, processed, motion-magnified |
| Decoded frames per stream | 161 each |
| Automatic graph companion files | 15 |
| Partial files | 0 |
| Session status | Complete; all required artifacts written |

Frame IDs are contiguous and identical between feature and master CSVs. The data dictionary exactly covers all three core CSV headers with unique scoped keys. All four videos decode at 640×480, and the unannotated original is separate from the overlay stream.

The measured optical delta-V response was below the localization threshold: ROI 5 peaked at 0.004375 V levels, ROI 1 had the largest peak at only 0.019375, and no dominant ROI was emitted. The original, processed, and motion-magnified files therefore encoded identically in this trial. This verifies that the requested streams and motion pipeline completed, but it does **not** demonstrate a measurable mechanoluminescent response from the specimen.

Evidence audit: `output/hardware_physical_validation_20260803/physical_session_audit.json`.

## Acceptance and remaining limitations

| Requested physical item | Result |
|---|---|
| Arducam driver/backend behavior | Verified for DirectShow 640×480; Media Foundation instability documented |
| Native/vendor controls | DirectShow property page and truthful readback matrix verified; no separate vendor SDK controls available |
| Nano compile/upload/reset behavior | Verified |
| Real HX711 cadence | Verified at approximately 10.89 Hz with zero ID gaps |
| Physical calibration accuracy | Verified; final 200 g error 0.4927% |
| Camera/load-cell USB contention | Verified for 60 seconds |
| Actual camera FPS | Verified at 14.98485 FPS sustained and 15.595 FPS in the physical recording |
| Software capture/processing timing | Measured as above |
| True scene-to-sensor-to-display optical latency | **Not measured**; requires a synchronized light/timer target or other external reference |
| Native target-monitor layout and 60-second interactive GUI preview | **Not physically inspected**; automated offscreen layout evidence only |
| Destructive COM disconnect / HX711 NOT_READY recovery | **Not performed** without explicit lab authorization |
| Three-placement 200 g repeatability series | **Not completed**; one independent post-retare verification passed |

The accepted physical data session is suitable as integration evidence for the tested configuration. Scientific experiments should not begin until the lab accepts a numeric latency threshold, measures true optical latency if required, records hardware asset/certificate identifiers, and demonstrates an optical response with the final specimen/lighting/ROI alignment.

## Evidence archive and regression result

The unchanged physical session, camera probes, calibration/retare/verification evidence, session audit, and a report snapshot were packaged as `output/physical-hardware-evidence-20260803.zip`. The archive contains 53 entries, is 1,712,398 bytes, and has SHA-256:

`054A1807554128F8BF5A0108CAAE6DDE3971D5B1D53E380D40BBDFEFCF56398D`

After the physical helper scripts and Arducam identity corrections, the complete automated suite passed: **318 passed, 0 failed**.
