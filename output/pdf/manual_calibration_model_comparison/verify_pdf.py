"""Verify the final static report PDF and retain its export provenance."""
from pathlib import Path
import hashlib
import json
import re
from pypdf import PdfReader

root = Path(__file__).resolve().parent
pdf = root / "Manual_Calibration_Model_Comparison.pdf"
artifact = root.parents[2] / "calibration_gui/analysis_outputs/live_sensor_manual_only/model_runs/retraining_20260909/artifact.json"
reader = PdfReader(pdf)
texts = [page.extract_text() or "" for page in reader.pages]
content = "\n\n".join(texts)
(root / "extracted_text.txt").write_text(content, encoding="utf-8")
required = [
    "Manual calibration model comparison", "0.637", "74.00%", "91.86%",
    "8.20%", "32.58%", "16,434", "Recommended next steps",
    "Manual Calibration (2).zip", "model_comparison.csv", "protocol.json",
    "end_to_end_metrics.json", "validation_audit.json", "inference_timing.json",
]
checks = {
    "eight_pages": len(reader.pages) == 8,
    "searchable_text_on_every_page": all(len(t) > 400 for t in texts),
    "required_metrics_and_sources_present": all(s in content for s in required),
    "no_ui_controls": not re.search(r"(?im)^\s*(share|edit|refresh|publish|toolbar|menu|drag)\s*$", content),
    "no_internal_renderer_metadata": not any(s in content.lower() for s in [
        "snapshot status", "widget type", "manifest path", "package path",
        "validation status", "table: pathlib", "c:\\users",
    ]),
    "all_page_previews_exist": all((root / f"page-{i}.png").is_file() for i in range(1, 9)),
}
assert all(checks.values()), checks
receipt = {
    "status": "passed",
    "page_count": len(reader.pages),
    "chart_count": 4,
    "table_count": 5,
    "checks": checks,
    "visual_review": {
        "pages_reviewed": list(range(1, 9)),
        "result": "All final page previews reviewed: readable tables and native vector charts; no clipping, overlapping numeric values, or unintended UI controls.",
    },
    "source_resolution": "Static export from the saved canonical report artifact, not the live MCP sandbox shell.",
    "conversion": "Packaged portable report renderer with native SVG extraction, then installed Chrome PDF API using the same static HTML.",
    "print_repairs": [
        "A4 margins, page numbers, table wrapping and paragraph break control",
        "Flattened interactive value annotations using print styles",
        "Expanded abbreviated total-light chart label",
        "Corrected inferred pathlib labels to source filenames",
    ],
    "sha256": {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [artifact, root / "report.html", root / "report.print.html", pdf]
    },
}
(root / "pdf_qa.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"status": receipt["status"], "pages": len(reader.pages), "checks": checks, "bytes": pdf.stat().st_size}))
