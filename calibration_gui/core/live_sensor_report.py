"""Auditable post-processing reports for completed optical press events."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html import escape
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterable, Sequence

from core.experimental_live_sensor import LiveSensorResult


EVENT_COLUMNS = (
    "completed_at_utc",
    "event_id",
    "forced_roi",
    "reported_roi",
    "forced_rois",
    "reported_rois",
    "localization_status",
    "confidence",
    "start_frame_id",
    "end_frame_id",
    "duration_frames",
    "peak_hsv_v",
    "force_at_peak_hsv_v_N",
    "peak_delta_v",
    "force_at_peak_delta_v_N",
    "peak_contact_score",
    "peak_signal_ratio",
    "event_peak_force_N",
    "force_status",
)

SUMMARY_COLUMNS = (
    "roi",
    "press_count",
    "press_share_percent",
    "accepted_count",
    "withheld_count",
    "force_available_count",
    "max_peak_hsv_v",
    "max_peak_delta_v",
    "mean_force_at_peak_hsv_v_N",
    "mean_event_peak_force_N",
    "max_event_peak_force_N",
)


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mean(values: Iterable[float | None]) -> float | None:
    finite = [item for value in values if (item := _finite(value)) is not None]
    return sum(finite) / len(finite) if finite else None


def _maximum(values: Iterable[float | None]) -> float | None:
    finite = [item for value in values if (item := _finite(value)) is not None]
    return max(finite) if finite else None


@dataclass(frozen=True, slots=True)
class EventReportRecord:
    """One completed press, including confidence and same-frame peak values."""

    completed_at_utc: str
    event_id: int
    forced_roi: int
    reported_roi: int | None
    localization_status: str
    confidence: str
    start_frame_id: int
    end_frame_id: int
    duration_frames: int
    peak_hsv_v: float | None
    force_at_peak_hsv_v_N: float | None
    peak_delta_v: float | None
    force_at_peak_delta_v_N: float | None
    peak_contact_score: float | None
    peak_signal_ratio: float | None
    event_peak_force_N: float | None
    force_status: str
    forced_rois: tuple[int, ...]
    reported_rois: tuple[int, ...]

    @classmethod
    def from_result(
        cls,
        result: LiveSensorResult,
        *,
        report_event_id: int | None = None,
        completed_at: datetime | None = None,
    ) -> "EventReportRecord":
        if result.event_phase != "completed":
            raise ValueError("only completed optical events can be reported")
        forced_rois = result.all_forced_rois
        if (
            result.forced_roi is None
            or not 1 <= int(result.forced_roi) <= 9
            or not forced_rois
            or any(roi not in range(1, 10) for roi in forced_rois)
        ):
            raise ValueError("completed event has no valid forced ROI")
        if result.event_start_frame_id is None or result.event_end_frame_id is None:
            raise ValueError("completed event has no valid frame bounds")
        start = int(result.event_start_frame_id)
        end = int(result.event_end_frame_id)
        if end < start:
            raise ValueError("completed event frame bounds are reversed")
        event_id = report_event_id if report_event_id is not None else result.event_id
        if event_id is None or int(event_id) < 1:
            raise ValueError("completed event has no valid event ID")
        timestamp = completed_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        reported_rois = result.displayed_rois
        if any(roi not in range(1, 10) for roi in reported_rois):
            raise ValueError("completed event has an invalid reported ROI")
        reported = int(result.roi) if result.roi is not None else None
        return cls(
            completed_at_utc=timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            event_id=int(event_id),
            forced_roi=int(result.forced_roi),
            reported_roi=reported,
            localization_status="accepted" if reported_rois else "withheld",
            confidence=str(result.confidence),
            start_frame_id=start,
            end_frame_id=end,
            duration_frames=end - start + 1,
            peak_hsv_v=_finite(result.peak_hsv_v),
            force_at_peak_hsv_v_N=_finite(result.force_at_peak_hsv_v_N),
            peak_delta_v=_finite(result.peak_delta_v),
            force_at_peak_delta_v_N=_finite(result.force_at_peak_delta_v_N),
            peak_contact_score=_finite(result.peak_contact_score),
            peak_signal_ratio=_finite(result.peak_signal_ratio),
            event_peak_force_N=_finite(result.peak_force_N),
            force_status=str(result.force_status),
            forced_rois=forced_rois,
            reported_rois=reported_rois,
        )


@dataclass(frozen=True, slots=True)
class ReportPaths:
    directory: Path
    html: Path
    event_csv: Path
    roi_csv: Path
    json: Path


def summarize_by_roi(events: Sequence[EventReportRecord]) -> list[dict[str, object]]:
    """Summarize all nine ROIs, counting each rod in a simultaneous event."""

    total = sum(len(event.forced_rois) for event in events)
    rows: list[dict[str, object]] = []
    for roi in range(1, 10):
        selected = [event for event in events if roi in event.forced_rois]
        dominant = [event for event in selected if event.forced_roi == roi]
        rows.append(
            {
                "roi": roi,
                "press_count": len(selected),
                "press_share_percent": 100.0 * len(selected) / total if total else 0.0,
                "accepted_count": sum(event.localization_status == "accepted" for event in selected),
                "withheld_count": sum(event.localization_status == "withheld" for event in selected),
                "force_available_count": sum(event.event_peak_force_N is not None for event in selected),
                "max_peak_hsv_v": _maximum(event.peak_hsv_v for event in dominant),
                "max_peak_delta_v": _maximum(event.peak_delta_v for event in dominant),
                "mean_force_at_peak_hsv_v_N": _mean(
                    event.force_at_peak_hsv_v_N for event in dominant
                ),
                "mean_event_peak_force_N": _mean(event.event_peak_force_N for event in selected),
                "max_event_peak_force_N": _maximum(event.event_peak_force_N for event in selected),
            }
        )
    return rows


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, (tuple, list)):
        return ";".join(str(item) for item in value)
    return value


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in columns})


def _fmt(value: object, digits: int = 2) -> str:
    number = _finite(value)
    return "—" if number is None else f"{number:.{digits}f}"


def _fmt_rois(rois: Sequence[int]) -> str:
    return ", ".join(str(roi) for roi in rois) if rois else "—"


def _render_html(
    events: Sequence[EventReportRecord],
    summary: Sequence[dict[str, object]],
    *,
    bundle_id: str,
    generated_at: str,
) -> str:
    accepted = sum(event.localization_status == "accepted" for event in events)
    force_available = sum(event.event_peak_force_N is not None for event in events)
    peak_event_force = _maximum(event.event_peak_force_N for event in events)
    most_pressed = max(summary, key=lambda row: int(row["press_count"]))
    max_count = max(1, int(most_pressed["press_count"]))

    bars = "".join(
        f'<div class="bar-row"><span>ROI {row["roi"]}</span>'
        f'<div class="track"><div class="bar" style="width:{100 * int(row["press_count"]) / max_count:.1f}%"></div></div>'
        f'<strong>{row["press_count"]}</strong></div>'
        for row in summary
    )
    summary_rows = "".join(
        "<tr>"
        f'<th scope="row">{row["roi"]}</th>'
        f'<td>{row["press_count"]}</td>'
        f'<td>{float(row["press_share_percent"]):.1f}%</td>'
        f'<td>{row["accepted_count"]}</td>'
        f'<td>{row["withheld_count"]}</td>'
        f'<td>{_fmt(row["max_peak_hsv_v"], 1)}</td>'
        f'<td>{_fmt(row["max_peak_delta_v"], 2)}</td>'
        f'<td>{_fmt(row["mean_force_at_peak_hsv_v_N"], 3)}</td>'
        f'<td>{_fmt(row["mean_event_peak_force_N"], 3)}</td>'
        f'<td>{_fmt(row["max_event_peak_force_N"], 3)}</td>'
        "</tr>"
        for row in summary
    )
    event_rows = "".join(
        "<tr>"
        f"<td>{event.event_id}</td><td>{escape(event.completed_at_utc)}</td>"
        f"<td>{escape(_fmt_rois(event.forced_rois))}</td>"
        f"<td>{escape(_fmt_rois(event.reported_rois))}</td>"
        f"<td>{escape(event.localization_status)}</td><td>{event.duration_frames}</td>"
        f"<td>{_fmt(event.peak_hsv_v, 1)}</td><td>{_fmt(event.force_at_peak_hsv_v_N, 3)}</td>"
        f"<td>{_fmt(event.peak_delta_v, 2)}</td><td>{_fmt(event.event_peak_force_N, 3)}</td>"
        f"<td>{escape(event.force_status)}</td></tr>"
        for event in events
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mechanoluminescent Skin Press Report</title>
<style>
:root {{ color-scheme: light dark; --ink:#18212b; --muted:#596773; --paper:#f7f9fb; --card:#fff; --line:#d8e0e7; --accent:#146c5b; --accent2:#7ed6c4; --warn:#925e00; }}
@media (prefers-color-scheme:dark) {{ :root {{ --ink:#eef4f7; --muted:#aebbc3; --paper:#11171c; --card:#192229; --line:#34434d; --accent:#60d4bb; --accent2:#287866; --warn:#ffd278; }} }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif }}
main {{ max-width:1180px; margin:auto; padding:32px 22px 64px }} h1 {{ margin:0 0 6px; font-size:clamp(27px,4vw,43px); line-height:1.08 }} h2 {{ margin:0 0 14px; font-size:21px }} h3 {{ margin:18px 0 6px }} p {{ max-width:86ch }} .eyebrow {{ color:var(--accent); font-weight:750; letter-spacing:.08em; text-transform:uppercase }} .muted {{ color:var(--muted) }}
.notice {{ border-left:5px solid var(--warn); padding:10px 14px; background:color-mix(in srgb,var(--warn) 11%,var(--card)); margin:22px 0 }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(185px,1fr)); gap:12px; margin:24px 0 }} .card,.section {{ background:var(--card); border:1px solid var(--line); border-radius:12px; box-shadow:0 5px 18px #0000000d }} .card {{ padding:17px }} .card strong {{ display:block; font-size:27px }} .card span {{ color:var(--muted) }} .section {{ padding:22px; margin:16px 0 }}
.bar-row {{ display:grid; grid-template-columns:55px 1fr 35px; gap:10px; align-items:center; margin:9px 0 }} .track {{ height:18px; background:color-mix(in srgb,var(--line) 70%,transparent); border-radius:4px; overflow:hidden }} .bar {{ min-width:0; height:100%; background:linear-gradient(90deg,var(--accent),var(--accent2)); border-radius:4px }}
.table-wrap {{ overflow:auto }} table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; white-space:nowrap }} th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:right }} thead th {{ position:sticky; top:0; background:var(--card); color:var(--muted); font-size:12px }} th:first-child,td:first-child, thead th:nth-child(2),tbody td:nth-child(2) {{ text-align:left }}
code {{ background:color-mix(in srgb,var(--line) 55%,transparent); padding:2px 5px; border-radius:4px }} ul {{ padding-left:21px }}
@media print {{ :root {{ color-scheme:light }} body {{ background:#fff }} main {{ max-width:none; padding:10mm }} .section,.card {{ box-shadow:none; break-inside:avoid }} thead th {{ position:static }} }}
</style></head><body><main>
<div class="eyebrow">Camera-only post-processing</div><h1>Mechanoluminescent Skin Press Report</h1>
<p class="muted">Generated {escape(generated_at)} · Bundle <code>{escape(bundle_id)}</code></p>
<div class="notice"><strong>Experimental and not physically validated.</strong> Force is an approximate camera-derived estimate within the model's narrow optical support. No load-cell or printer input is used during inference.</div>
<div class="cards"><div class="card"><strong>{len(events)}</strong><span>Total completed presses</span></div>
<div class="card"><strong>ROI {most_pressed['roi']}</strong><span>Most pressed ({most_pressed['press_count']} events)</span></div>
<div class="card"><strong>{100 * accepted / len(events):.1f}%</strong><span>ROI assignments accepted</span></div>
<div class="card"><strong>{_fmt(peak_event_force,3)} N</strong><span>Highest available event force</span></div></div>
<section class="section"><h2>Technical summary</h2><p>The session contains {len(events)} completed optical events. {accepted} localization results were accepted and {len(events)-accepted} were withheld. An event-level force was available for {force_available} events. Each independently detected rod is counted in its ROI, so one simultaneous event can contribute to multiple ROI counts. Newton force is withheld for multi-press events because the archived model was fitted only to single presses.</p></section>
<section class="section"><h2>Press count by ROI</h2>{bars}</section>
<section class="section"><h2>ROI summary</h2><div class="table-wrap"><table><thead><tr><th>ROI</th><th>Presses</th><th>Share</th><th>Accepted</th><th>Withheld</th><th>Peak raw V</th><th>Peak ΔV</th><th>Mean force @ peak V (N)</th><th>Mean event peak (N)</th><th>Max event peak (N)</th></tr></thead><tbody>{summary_rows}</tbody></table></div></section>
<section class="section"><h2>Event audit table</h2><div class="table-wrap"><table><thead><tr><th>Event</th><th>Completed (UTC)</th><th>Detected ROIs</th><th>Reported ROIs</th><th>Localization</th><th>Frames</th><th>Peak raw V</th><th>Force @ peak V (N)</th><th>Peak ΔV</th><th>Event peak force (N)</th><th>Force status</th></tr></thead><tbody>{event_rows}</tbody></table></div></section>
<section class="section"><h2>Definitions</h2><ul>
<li><strong>Peak raw V</strong>: highest HSV Value channel (0–255) observed inside the assigned ROI during the press.</li>
<li><strong>Peak ΔV</strong>: highest baseline-corrected V increase in the assigned ROI.</li>
<li><strong>Force @ peak V</strong>: force estimate on the exact video frame where raw V reached its maximum; it can differ from event peak force.</li>
<li><strong>Event peak force</strong>: largest in-support force estimate anywhere in the completed press.</li>
<li><strong>Detected ROIs</strong>: rods that crossed their independent unloaded-normalized activation gates. <strong>Reported ROIs</strong> is blank when localization is withheld.</li></ul></section>
<section class="section"><h2>Methodology and validation boundary</h2><p>Each press starts after the contact hysteresis criterion and ends after the clear criterion. Every ROI is thresholded independently with acquisition and release hysteresis, allowing multiple rods to be represented in one event. Raw V and ΔV maxima are tracked per ROI from camera features. Force is produced by the retained monotonic optical model only for single-press responses inside its fitted support. Missing values are left blank, not imputed.</p><p>The report is descriptive of model output, not ground-truth force or location. Archive cross-validation is reused single-press evidence; it is not independent physical or multi-press validation. Do not use this report for safety, closed-loop control, or claims that force resolution was established.</p></section>
<section class="section"><h2>Recommended follow-up</h2><ul><li>Review withheld-localization rate and camera saturation before interpreting ROI usage.</li><li>Compare raw V and ΔV stability across sessions to detect lighting or baseline drift.</li><li>Collect independent ground-truth trials later if absolute force accuracy or spatial accuracy must be claimed.</li><li>Use event duration and time-between-press distributions to identify missed presses, bouncing, or unintended repeated contact.</li></ul></section>
<section class="section"><h2>Further questions</h2><p>Are press counts balanced across ROIs? Does the withheld rate cluster in adjacent ROIs? Do high raw-V events saturate at 255? Does force availability decline over the session, suggesting optical drift?</p></section>
</main></body></html>"""


def write_post_processing_report(
    destination: Path,
    events: Sequence[EventReportRecord],
    *,
    bundle_id: str,
) -> ReportPaths:
    """Write HTML, CSV, and JSON as one new self-contained report directory."""
    records = list(events)
    if not records:
        raise ValueError("at least one completed event is required")
    event_ids = [record.event_id for record in records]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("report event IDs must be unique")
    target = Path(destination).resolve()
    if target.exists():
        raise FileExistsError(f"report destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    summary = summarize_by_roi(records)
    try:
        event_rows = [asdict(record) for record in records]
        _write_csv(staging / "event_details.csv", EVENT_COLUMNS, event_rows)
        _write_csv(staging / "roi_summary.csv", SUMMARY_COLUMNS, summary)
        payload = {
            "schema_version": 2,
            "generated_at_utc": generated_at,
            "bundle_id": str(bundle_id),
            "validation_status": "experimental_not_physically_validated",
            "inference_inputs": {
                "camera_and_mechanoluminescent_skin": "required",
                "load_cell": "not_used",
                "printer": "not_used",
            },
            "events": event_rows,
            "roi_summary": summary,
        }
        (staging / "report.json").write_text(
            json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        (staging / "post_processing_report.html").write_text(
            _render_html(records, summary, bundle_id=str(bundle_id), generated_at=generated_at),
            encoding="utf-8",
        )
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return ReportPaths(
        directory=target,
        html=target / "post_processing_report.html",
        event_csv=target / "event_details.csv",
        roi_csv=target / "roi_summary.csv",
        json=target / "report.json",
    )


__all__ = [
    "EVENT_COLUMNS",
    "SUMMARY_COLUMNS",
    "EventReportRecord",
    "ReportPaths",
    "summarize_by_roi",
    "write_post_processing_report",
]
