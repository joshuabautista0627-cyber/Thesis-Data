## 8. Temporal Synchronization

The camera and load-cell records contain a shared host-monotonic time base. Recomputing the nearest physical load-cell sample reproduces every stored synchronization offset exactly. Independently interpolating raw force on that time base reproduces synchronized force with maximum absolute numerical difference **1.46 × 10⁻¹² N**.

For **29,318 frames**, signed camera-minus-nearest-load-cell timestamp offset is **mean 1.298 ms**, **median −0.323 ms**, **SD 26.799 ms**, **IQR 43.623 ms**, and **range −80.268 to 83.430 ms**. Median absolute nearest-sample gap is **22.059 ms**, with maximum **83.430 ms**, below the recorded **200 ms** matching gate. There are no non-increasing host timestamps within either stream.

These values assess **timestamp matching**, not physical sensor response time. A deterministic exploratory cross-correlation searches ±1 s after resampling at each recording's median frame interval; its selected lag has median **0.129 s** and range **−0.262 to 0.917 s**. This is approximately frame-scale and depends on waveform shape, repeated loading, interpolation and optical magnification. It cannot identify intrinsic mechanoluminescent latency, and the ±1 s search bound is an analysis setting rather than a validated acceptance threshold. Detailed estimates and boundary flags are saved for inspection.

Representative overlays use R1_TEST1, R5_TEST4 and R9_TEST1_v2, selected by fixed trial identifiers. Optical and force peaks are not automatically paired as if each recording were one press.