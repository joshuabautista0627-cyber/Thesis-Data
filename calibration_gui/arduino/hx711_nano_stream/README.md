# Arduino Nano + HX711 stream

The supplied sketch uses the Bogdan Necula/Bogde `HX711` Arduino library, DOUT on Nano D4, SCK on D5, and 115200 baud. These pins match the existing physical calibration sketch for the COM3 assembly; incorrect D2/D3 assignments produced an invalid all-zero, hundreds-of-hertz stream. It streams immediately after reset. Close Arduino Serial Monitor and Serial Plotter before connecting the desktop GUI because only one process can own the COM port.

Each fresh conversion is one ASCII line:

```text
DATA,<sample_id>,<arduino_micros>,<signed_raw_adc>
```

Example:

```text
DATA,42,12543000,-83421
```

The startup line `HELLO,HX711_NANO,1.0,1.0.0` is optional identity information. The GUI does not require it and validates the sensor from finite numeric measurement lines. `START`, `STOP`, `PING`, and `STATUS` remain optional diagnostic commands for this supplied protocol; generic numeric firmware is not sent these commands.

The GUI also accepts these simpler firmware outputs in Auto-detect mode:

```text
-83421
RAW:-83421
raw=-83421
12543,-83421
DATA,12543,-83421
```

Plain and prefixed values may be signed integers, decimals, or scientific notation. The timestamp forms require an unsigned 32-bit integer device timestamp. One complete reading per line is required. `NaN`, infinity, incomplete lines, verbose text, and invalid UTF-8 are rejected and shown in the Raw Serial Monitor.

The sketch calls `scale.read()` only after `scale.is_ready()`. It reports `ERROR,HX711_NOT_READY` during startup when appropriate and periodically reports `ERROR,HX711_TIMEOUT` if fresh conversions stop. It does not convert counts to mass: host calibration retains the signed raw values and calculates tared counts, grams, and newtons.

Physical upload, electrical wiring, real sample cadence, and 200 g verification must be completed with the lab hardware using `HARDWARE_TEST_CHECKLIST.md`.
