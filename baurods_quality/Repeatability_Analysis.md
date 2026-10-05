## 5. Optical-Response Repeatability

Each taxel contributes **eight recording means** computed over valid force-derived contact frames. The existing target-ROI mean positive delta V is averaged per recording; a separate table summarizes the maximum of that same feature. Between-recording sample SD uses `ddof=1`; CV is 100 × SD / mean. Quartiles use linear interpolation. Outliers remain included.

| Taxel | Recordings | Optical mean ΔV | Optical SD | Optical CV (%) | Force mean (N) | Force SD (N) | Force CV (%) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | 8 | 1.161 | 0.277 | 23.846 | 5.422 | 1.964 | 36.230 |
| T2 | 8 | 1.131 | 0.139 | 12.302 | 5.020 | 1.510 | 30.074 |
| T3 | 8 | 1.682 | 0.482 | 28.655 | 4.588 | 1.215 | 26.486 |
| T4 | 8 | 0.947 | 0.200 | 21.112 | 3.744 | 0.823 | 21.991 |
| T5 | 8 | 0.856 | 0.081 | 9.453 | 3.310 | 0.311 | 9.405 |
| T6 | 8 | 1.163 | 0.287 | 24.709 | 3.781 | 1.154 | 30.518 |
| T7 | 8 | 1.087 | 0.186 | 17.134 | 3.241 | 0.700 | 21.592 |
| T8 | 8 | 1.356 | 0.297 | 21.898 | 3.529 | 0.892 | 25.277 |
| T9 | 8 | 1.467 | 0.248 | 16.931 | 3.709 | 1.087 | 29.299 |

Mean-response CV ranges from **9.45% (T5) to 28.66% (T3)**. Peak-response CV ranges from **29.56% to 102.12%**, with the largest value at T4. Thus means are less dispersed than maxima under this aggregation. Longer recordings provide more opportunities for high maxima, so the peak comparison is duration-sensitive. Lower dispersion here describes these recordings; it does not isolate sensor repeatability at fixed loading.

All optical measurements are identified by `quantitative_analysis_source` as **Motion-magnified frame**, and `magnified_pixels_used_for_exported_measurements` is true throughout. These results characterize the saved transformed signal. Video decoding verifies frame counts, not pixel-level reproduction of every feature. The magnitude and timing of unprocessed optical responses require a separate feature recomputation from original video.

Physical press boundaries, controlled force levels and independent experimental sessions are unavailable. Consequently, a pure same-force, press-to-press repeatability coefficient and inter-session reproducibility claim are **not supported**. The recording-level result is retained as an honest descriptive surrogate.