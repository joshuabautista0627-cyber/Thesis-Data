## 6. Applied-Force Variability

Mean contact force per recording ranges from **2.149 to 7.459 N**. Within-taxel CV of these means ranges from **9.40% (T5) to 36.23% (T1)**. Peak-force means and full descriptive distributions are included separately; synchronized forces reach **22.699 N**.

Manual force variability is an experimental input variation, not by itself evidence of corrupted measurements. It provides an alternative explanation for differences in optical response, together with loading history, duration and baseline changes. No force normalization or model has been used to erase that variation.

The calibration records document a **200 g reference mass (1.96133 N under the recorded conversion)** and passing stored verification checks. The maximum observed force is about 11.6 times that reference force. The supplied records do not provide a multi-point calibration or uncertainty budget covering the full observed range; reference-force accuracy over that range is therefore not established by this audit. Small negative tared forces occur in **2,154 frames**, reaching **−0.190 N**. They are retained as measured offsets/noise, not automatically declared impossible or clamped to zero.

## 7. Force–Optical Response Relationship

The question is whether paired force and optical recordings exhibit a coherent directional association. Spearman's rank correlation describes monotonic association without imposing a linear calibration model. Across **72 recording pairs**, the correlation of mean contact force and mean target optical response is **ρ = 0.520**. Within-taxel values range from **0.238 to 0.905** (eight recordings each).

The positive pooled trend is compatible with stronger loading often accompanying larger optical responses. It is neither a quality score nor causal evidence. Group-specific results differ: TEST7 has **ρ = −0.433** and TEST1_v2 **ρ = −0.150**, each across nine taxels. Pooling can therefore conceal acquisition-group and taxel effects.

The scatterplots and saved group-specific coefficients are descriptive. No Pearson model is imposed because a stable linear relationship is not established. No p-values, ANOVA or normality-screening tests are reported; independence across recordings is not established, there is only one source session, and each within-taxel sample contains eight recording summaries. An independent-session confidence interval is consequently unavailable. No multiple-comparison claims are made. [SciPy's Spearman documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html) describes the coefficient used here.