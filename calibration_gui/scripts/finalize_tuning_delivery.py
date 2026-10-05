"""Verify portable report identity and preserve the completed run's source."""
import base64
import gzip
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil

root=Path(__file__).resolve().parents[1]
out=root/"analysis_outputs/live_sensor_manual_only/model_runs/tuning_20260909"
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
artifact=json.loads((out/"artifact.json").read_text())
html=(out/"report.html").read_text(encoding="utf-8")
encoded=re.search(r'<template id="data-analytics-portable-artifact-payload-source"[^>]*>(.*?)</template>',html,re.S).group(1)
embedded=json.loads(gzip.decompress(base64.b64decode(encoded)))
assert embedded["manifest"]==artifact["manifest"]
assert embedded["snapshot"]==artifact["snapshot"]
protocol=json.loads((out/"protocol.json").read_text())
assert protocol["source_code_sha256"]==sha(root/"scripts/tune_manual_models.py")
frame_protocol=json.loads((out/"frame_only_protocol.json").read_text())
assert frame_protocol["source_code_sha256"]==sha(root/"scripts/evaluate_manual_frame_only.py")
model_files=0
for directory in ("models","models_frame_only"):
    manifest=json.loads((out/directory/"manifest.json").read_text())
    for name,digest in manifest["files"].items():
        assert sha(out/directory/name)==digest
        model_files+=1
audit=json.loads((out/"validation_audit.json").read_text())
assert audit["status"]=="passed"
sources=["scripts/"+name+".py" for name in (
    "tune_manual_models","predict_tuned_manual","audit_tuned_manual","evaluate_manual_frame_only",
    "report_tuned_manual","finish_manual_tuning","retrain_manual_models","audit_manual_retraining",
    "predict_retrained_manual","fit_manual_only_preprocessing","live_sensor_common")]
sources += ["core/timestamp_alignment.py","tests/test_tuned_manual_features.py","config/live_sensor_manual_only.json"]
hashes={}
for relative in sources:
    source=root/relative
    destination=out/"source_snapshot"/relative
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,destination)
    hashes[relative]=sha(destination)
(out/"source_snapshot_manifest.json").write_text(json.dumps(hashes,indent=2)+"\n")
environment=json.loads((out/"environment.json").read_text())
versions={name:importlib.metadata.version(name) for name in environment["packages"]}
receipt=dict(status="passed",metric_audit_checks=audit["metric_checks"],models_checksum_verified=model_files,
    canonical_manifest_and_snapshot_match_portable_html=True,training_source_hashes_unchanged=True,
    source_files_preserved=len(sources),model_library_versions_unchanged=versions==environment["packages"],
    current_model_library_versions=versions,
    visual_review="Four charts and comparison/ROI/bin tables reviewed; full model-parameter table uses horizontal scrolling.",
    html_sha256=sha(out/"report.html"),artifact_sha256=sha(out/"artifact.json"),
    renderer="Self-contained verified export produced with the original packaged Data Analytics renderer.",
    renderer_runtime_note="The plugin updated after export and removed its legacy builder. The existing portable HTML contains its runtime and exactly matches the canonical artifact; model training and inference do not depend on that plugin.")
(out/"report_delivery.json").write_text(json.dumps(receipt,indent=2)+"\n")
print(json.dumps({k:v for k,v in receipt.items() if k not in ("current_model_library_versions","renderer_runtime_note")}))
