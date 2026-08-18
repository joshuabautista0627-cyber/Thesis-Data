from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
from docx import Document
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
DOCX = ROOT / "SPARE_Calibration_Program_Full_Technical_Report_2026-08-18.docx"
MARKDOWN = ROOT / "SPARE_Calibration_Program_Full_Technical_Report_2026-08-18.md"
NOTEBOOK = ROOT / "SPARE_Report_Validation_Companion.ipynb"
PDF = ROOT / "SPARE_Calibration_Program_Full_Technical_Report_2026-08-18.pdf"
VALIDATION = ROOT / "validation_checks.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


document = Document(DOCX)
pdf = PdfReader(PDF)
notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
markdown = MARKDOWN.read_text(encoding="utf-8")
schema = pd.read_csv(ROOT / "current_schema_dictionary.csv")
evidence = pd.read_csv(ROOT / "evidence_manifest.csv")

assert len(pdf.pages) >= 40
assert notebook["nbformat"] == 4
assert len(notebook["cells"]) == 11
assert not any(
    output.get("output_type") == "error"
    for cell in notebook["cells"]
    if cell["cell_type"] == "code"
    for output in cell["outputs"]
)
assert len(schema) == 812
assert len(evidence) == 38
for required_text in [
    "Force resolution not measured",
    "validated_claim_allowed=false",
    "93.43%",
    "0.328 N",
    "245 sessions",
    "179,176",
    "Live Sensor GUI",
    "35 passed in 2.07 s",
]:
    assert required_text in markdown
assert len(document.inline_shapes) == 9

payload = json.loads(VALIDATION.read_text(encoding="utf-8"))
payload["outputs"] = {
    path.name: {"sha256": sha256(path), "bytes": path.stat().st_size}
    for path in [
        DOCX,
        PDF,
        MARKDOWN,
        NOTEBOOK,
        ROOT / "evidence_manifest.csv",
        ROOT / "current_schema_dictionary.csv",
        ROOT / "chart_map.csv",
    ]
}
payload["final_structural_checks"] = {
    "docx_opened": True,
    "inline_figures": len(document.inline_shapes),
    "pdf_pages": len(pdf.pages),
    "notebook_cells": len(notebook["cells"]),
    "notebook_has_errors": False,
    "schema_dictionary_rows": len(schema),
    "evidence_items": len(evidence),
    "required_claim_boundaries_present": True,
}
payload["visual_qa"] = {
    "method": "Microsoft Word field update and PDF export; Poppler rasterization at 120 DPI; complete contact-sheet review plus targeted full-page inspection",
    "rendered_pages": len(pdf.pages),
    "all_pages_inspected": True,
    "result": "pass",
    "pagination_adjustment": "Table rows marked not to split across pages; appendix evidence and release-gate rows rechecked.",
}
payload["all_checks_passed"] = True
VALIDATION.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

print("Final validation passed")
print(f"DOCX SHA-256: {sha256(DOCX)}")
print(f"PDF pages: {len(pdf.pages)}")
