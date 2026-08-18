from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent


def weighted_localization(group: pd.DataFrame):
    localized = group["localized_frame_rate"] * group["frame_rows_profiled"]
    if localized.sum() == 0:
        return None
    return float((group["localization_accuracy"].fillna(0) * localized).sum() / localized.sum())


def main():
    df = pd.read_csv(ROOT / "session_summary.csv")
    df["integrated_per_cycle_millions"] = (
        df["integrated_target_value"] / df["motion_cycles"] / 1_000_000
    )
    df["camera_config"] = df["camera_fingerprint"].map(
        {
            "5a40306c002688d3274d10fb1dd05f03f54dfee4b3201d22c8cd7cf21dca0478": "Config A: brighter",
            "4cde17769ad834a7ff4b578d714cb13c8e8f28b980762587caadaaa8e6808b25": "Config B: darker",
        }
    )

    target_rows = []
    accuracy_rows = []
    for target, group in df.groupby("target_roi", sort=True):
        peak_win = float((group["peak_winner_roi"] == target).mean())
        integrated_win = float((group["integrated_winner_roi"] == target).mean())
        mean_contact_win = float((group["mean_contact_winner_roi"] == target).mean())
        target_rows.append(
            {
                "target_roi": int(target),
                "sessions": int(len(group)),
                "peak_winner_rate": peak_win,
                "integrated_winner_rate": integrated_win,
                "mean_contact_winner_rate": mean_contact_win,
                "peak_target_mean_delta_v": float(group["peak_target_value"].mean()),
                "integrated_per_cycle_millions": float(group["integrated_per_cycle_millions"].mean()),
                "median_max_force_N": float(group["force_N_max"].median()),
                "localized_frame_accuracy": weighted_localization(group),
                "baseline_mean_v": float(group["baseline_target_mean_v"].mean()),
            }
        )
        accuracy_rows.extend(
            [
                {
                    "target_roi": int(target),
                    "target_label": f"ROI {int(target)}",
                    "series": "Observed peak winner rate",
                    "rate": peak_win,
                    "sessions": int(len(group)),
                    "integrated_winner_rate": integrated_win,
                    "peak_target_mean_delta_v": float(group["peak_target_value"].mean()),
                },
                {
                    "target_roi": int(target),
                    "target_label": f"ROI {int(target)}",
                    "series": "Uniform-chance reference",
                    "rate": 1 / 9,
                    "sessions": int(len(group)),
                    "integrated_winner_rate": integrated_win,
                    "peak_target_mean_delta_v": float(group["peak_target_value"].mean()),
                },
            ]
        )

    phase = df[df["target_roi"] <= 6].groupby(["target_roi", "batch"], sort=True).agg(
        peak=("peak_target_value", "mean"),
        integrated_per_cycle=("integrated_per_cycle_millions", "mean"),
        force=("force_N_max", "median"),
    )
    peak_ratio = phase["peak"].unstack()["S2"] / phase["peak"].unstack()["S1"]
    integrated_ratio = (
        phase["integrated_per_cycle"].unstack()["S2"]
        / phase["integrated_per_cycle"].unstack()["S1"]
    )

    headline = [
        {
            "sessions": int(len(df)),
            "complete_rate": float(df["complete"].mean()),
            "peak_winner_rate": float((df["peak_winner_roi"] == df["target_roi"]).mean()),
            "chance_rate": 1 / 9,
            "new_to_old_peak_ratio_median": float(peak_ratio.median()),
            "new_to_old_integrated_ratio_median": float(integrated_ratio.median()),
            "frame_rows": int(df["frame_rows_profiled"].sum()),
            "loadcell_rows": int(df["loadcell_rows_profiled"].sum()),
        }
    ]

    mechanical_rows = []
    for displacement, group in df.groupby("displacement_mm", sort=True):
        mechanical_rows.append(
            {
                "displacement_mm": abs(float(displacement)),
                "sessions": int(len(group)),
                "median_max_force_N": float(group["force_N_max"].median()),
                "mean_peak_target_delta_v": float(group["peak_target_value"].mean()),
                "mean_integrated_per_cycle_millions": float(
                    group["integrated_per_cycle_millions"].mean()
                ),
            }
        )
    mechanical_rows.sort(key=lambda r: r["displacement_mm"])

    scatter_rows = []
    for _, row in df.iterrows():
        scatter_rows.append(
            {
                "session": row["session_folder"],
                "batch": row["batch"],
                "target_roi": int(row["target_roi"]),
                "condition_index": int(row["condition_index"]),
                "displacement_mm": abs(float(row["displacement_mm"])),
                "motion_cycles": int(row["motion_cycles"]),
                "camera_config": row["camera_config"],
                "force_N_max": float(row["force_N_max"]),
                "integrated_per_cycle_millions": float(row["integrated_per_cycle_millions"]),
                "peak_target_delta_v": float(row["peak_target_value"]),
                "baseline_mean_v": float(row["baseline_target_mean_v"]),
                "peak_target_rank": int(row["peak_target_rank"]),
            }
        )

    report_data = {
        "headline_metrics": headline,
        "target_accuracy": accuracy_rows,
        "session_scatter": scatter_rows,
        "target_summary": target_rows,
        "mechanical_response": mechanical_rows,
    }
    (ROOT / "report_datasets.json").write_text(
        json.dumps(report_data, indent=2), encoding="utf-8"
    )

    with sqlite3.connect(ROOT / "auto_calibration_analysis.sqlite") as connection:
        df.to_sql("session_summary", connection, if_exists="replace", index=False)
        for table_name, rows in report_data.items():
            pd.DataFrame(rows).to_sql(
                f"report_{table_name}", connection, if_exists="replace", index=False
            )

    checks = {
        "unique_session_folders": int(df["session_folder"].nunique()),
        "all_frame_counts_reconcile": bool(
            (df["frame_rows_profiled"] == df["accepted_frame_count"]).all()
            and (df["frame_rows_profiled"] == df["feature_row_count"]).all()
            and (df["frame_rows_profiled"] == df["video_frame_count"]).all()
        ),
        "all_loadcell_counts_reconcile": bool(
            (df["loadcell_rows_profiled"] == df["loadcell_sample_count"]).all()
        ),
        "peak_winner_count": int((df["peak_winner_roi"] == df["target_roi"]).sum()),
        "integrated_winner_count": int(
            (df["integrated_winner_roi"] == df["target_roi"]).sum()
        ),
        "camera_config_counts": {
            str(k): int(v) for k, v in df["camera_config"].value_counts().items()
        },
        "peak_ratio_new_to_old_by_roi_1_to_6": {
            str(int(k)): float(v) for k, v in peak_ratio.items()
        },
        "integrated_per_cycle_ratio_new_to_old_by_roi_1_to_6": {
            str(int(k)): float(v) for k, v in integrated_ratio.items()
        },
    }
    (ROOT / "validation_checks.json").write_text(
        json.dumps(checks, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
