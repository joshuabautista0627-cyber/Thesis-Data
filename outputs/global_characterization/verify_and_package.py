"""Verify every exported XLSX cell and package only final deliverables."""
from pathlib import Path
import json,math,hashlib,zipfile,sys,os
import openpyxl
from datetime import datetime
from openpyxl.utils.datetime import to_excel
HERE=Path(__file__).resolve().parent; OUT=HERE.parent; ROOT=OUT.parent
payload=json.loads((HERE/'workbook_data.json').read_text())
wb=openpyxl.load_workbook(HERE/'global_characterization_statistics.xlsx',read_only=True,data_only=True)
checked=0;bad=[]
for spec in payload['sheets']:
    sheet=wb[spec['name']]
    actual=list(sheet.iter_rows(values_only=True))
    for i,row in enumerate(spec['rows']):
        for j,want in enumerate(row):
            got=actual[i][j] if i<len(actual) and j<len(actual[i]) else None
            checked+=1
            if isinstance(got,datetime) and isinstance(want,(int,float)):ok=abs(to_excel(got)-want)<1.2e-8
            elif isinstance(want,(int,float)) and not isinstance(want,bool):ok=isinstance(got,(int,float)) and math.isclose(want,got,rel_tol=1e-12,abs_tol=1e-12)
            else:ok=got==want
            if not ok:bad.append([spec['name'],i+1,j+1,want,got])
wb.close()
assert not bad,bad[:10]
source=json.loads((HERE/'validation_results.json').read_text())
assert source['status']=='PASS' and source['n_events_checked']==5357
result=dict(status='PASS',sheets=len(payload['sheets']),cells_compared=checked,mismatches=len(bad),
    source_event_check=source['status'],verified_events=source['n_events_checked'],
    figure_visual_review='All nine primary figures and spatial diagnostics inspected; labels, visible marks and units reviewed')
(HERE/'delivery_validation.json').write_text(json.dumps(result,indent=2))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

files=[ROOT/'global_sensor_characterization.py']
model_integration=HERE/'model_report_validation.json'
if model_integration.exists():
    model_check=json.loads(model_integration.read_text())
    assert model_check['status']=='PASS'
    result['model_metrics_recomputed']=model_check['metrics_recomputed']
    result['model_maximum_absolute_difference']=model_check['maximum_absolute_difference']
    result['figure_visual_review']='Original nine figures and spatial diagnostics reviewed; updated gate labels and additional model comparison visually checked'
    (HERE/'delivery_validation.json').write_text(json.dumps(result,indent=2))
    files += [ROOT/'integrate_model_characterization.py',HERE/'model_assisted_results.json',model_integration]
    files += [p for p in (HERE/'model_report_review/evidence').rglob('*') if p.is_file()]
for d in [OUT/'tables',OUT/'spatial_diagnostics',OUT/'figures']:
    files += [p for p in d.rglob('*') if p.is_file() and p.suffix in {'.csv','.png','.pdf','.svg'}]
for name in ['Global_Sensor_Characterization_Report.md','Thesis_Results_and_Discussion.md','README.md',
    'global_characterization_summary.csv','global_characterization_statistics.xlsx','analysis_results.json',
    'validation_results.json','delivery_validation.json','prepare_workbook.py','build_statistics_workbook.mjs','verify_and_package.py']:
    files.append(HERE/name)
files += list((HERE/'audit').glob('segmentation_atlas_*.png'))
old=json.loads((HERE/'run_manifest.json').read_text())
manifest=dict(source_archive_sha256=old['source_archive_sha256'],code_sha256=sha(ROOT/'global_sensor_characterization.py'),
    python_version=old.get('python_version'),packages=old.get('packages'),
    output_scope='Final deliverables only; source ZIP and optional original-video feature store remain external',
    outputs={str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for p in sorted(files)})
(HERE/'run_manifest.json').write_text(json.dumps(manifest,indent=2))
files.append(HERE/'run_manifest.json')
dest=ROOT/'BAURods_Global_Characterization.zip'
with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in files:z.write(p,str(p.relative_to(ROOT)))
with zipfile.ZipFile(dest) as z:
    assert z.testzip() is None
    assert len(z.namelist())==len(files)
print(json.dumps(dict(**result,package=str(dest),package_files=len(files),package_bytes=dest.stat().st_size),indent=2))
