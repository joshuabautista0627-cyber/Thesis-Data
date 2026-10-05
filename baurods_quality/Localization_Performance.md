## Appendix A. Sensor performance, separate from data quality

This appendix evaluates the **existing thresholded dominant-ROI predictions stored in the supplied ZIP**, not subsequently trained workspace models. The source configurations set a localization threshold of **5 mean delta V**. The force-derived reference class uses **0.05 N**; target ROI is treated as localization truth only during contact.

Of **26,786 contact frames**, **34** have a prediction and all 34 match the selected taxel: conditional accuracy **100%**, coverage **0.126932%**. Counting withheld predictions as failures gives overall correct localization **0.126932%** and macro-F1 **0.002439**. There are no predictions for T2, T5, T7, T8 or T9. Undefined precision for a class with no predictions is set to zero for the explicitly reported macro-F1 convention; it must not be read as a measured false-positive fraction.

| Taxel | Contact frames | Predicted | Correct | Coverage (%) | Precision | Recall | F1 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | 3454 | 12 | 12 | 0.347 | 1.000 | 0.003 | 0.007 |
| T2 | 3095 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| T3 | 2798 | 13 | 13 | 0.465 | 1.000 | 0.005 | 0.009 |
| T4 | 3130 | 2 | 2 | 0.064 | 1.000 | 0.001 | 0.001 |
| T5 | 3218 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| T6 | 3102 | 7 | 7 | 0.226 | 1.000 | 0.002 | 0.005 |
| T7 | 2862 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| T8 | 2627 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |
| T9 | 2500 | 0 | 0 | 0.000 | 0.000 | 0.000 | 0.000 |

No independent optical contact-detector output is stored. Prediction availability can be reported only as a **detection proxy**: TP 34, FN 26,752, FP 0 and TN 2,532. This gives sensitivity **0.126932%** and specificity **100%**, relative to the load-cell threshold. It does not validate a dedicated contact detector. Comparing `contact_state_derived` against the same force threshold would be circular. Metrics here are frame-weighted descriptions of dependent observations, with no independent-frame confidence interval.