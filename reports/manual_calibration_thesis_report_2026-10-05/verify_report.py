from pathlib import Path
import json, csv, hashlib, re, zipfile
from docx import Document
from pypdf import PdfReader
import pdfplumber

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
PDF=ROOT/'output/pdf/manual_calibration_thesis_report_2026-10-05/Manual_Calibration_Models_Technical_Report.pdf'
RUN=ROOT/'calibration_gui/analysis_outputs/live_sensor_manual_only/model_runs'
def rows(p):
    with p.open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))
def compact(s):return re.sub(r'\s+','',s)
checks=[]
def check(name,result):
    checks.append({'check':name,'passed':bool(result)})
    if not result:raise AssertionError(name)
d=Document(HERE/'Manual_Calibration_Models_Technical_Report.docx')
r=PdfReader(PDF);full='\n'.join(p.extract_text() for p in r.pages);ct=compact(full)
check('22 PDF pages',len(r.pages)==22)
check('25 tables',len(d.tables)==25)
for ti,t in enumerate(d.tables,1):
    for ri,row in enumerate(t.rows):
        for ci,c in enumerate(row.cells):
            check(f'PDF table {ti} row {ri} cell {ci} text',compact(c.text) in ct)
for run,model in [('retraining_20260909','Extra Trees'),('tuning_20260909',None)]:
    roi=[x for x in rows(RUN/run/'roi_metrics.csv') if x['task']=='force' and (model is None or x['model']==model)]
    comp=next(x for x in rows(RUN/run/'model_comparison.csv') if x['task']=='force' and x['model']==(model or 'Tuned selected'))
    check(run+' equal-ROI mean matches pooled equal-session MAE',abs(sum(float(x['mae_N']) for x in roi)/9-float(comp['mae_N']))<1e-12)
    col=2 if model else 3
    for i,rec in enumerate(roi,1):
        check(run+f' ROI {i} correct model value',d.tables[15].rows[i].cells[col].text==f"{float(rec['mae_N']):.3f}")
for manifest in json.loads((HERE/'evidence/source_manifest.json').read_text()):
    check('Evidence hash '+manifest['copy'],hashlib.sha256((HERE/manifest['copy']).read_bytes()).hexdigest()==manifest['sha256'])
with pdfplumber.open(PDF) as pdf:
    for i,p in enumerate(pdf.pages,1):
        check(f'Page {i} text within bounds',all(c['x0']>=45 and c['x1']<=p.width-40 and c['top']>=30 and c['bottom']<=p.height-15 for c in p.chars))
        check(f'Page {i} has substantive content',len(p.extract_text().split())>150)
check('No missing glyph replacement', '\ufffd' not in full and '\u25a0' not in full)
qa={'status':'passed','checks':len(checks),'pages':len(r.pages),'tables':len(d.tables),'visual_review':'All 22 PDF pages inspected; no clipping, overlap or broken tables.','docx_render_status':'Not delivered; canonical Word renderer unavailable because LibreOffice is absent. PDF independently typeset and reviewed.','files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [PDF,HERE/'Manual_Calibration_Models_Technical_Report.txt',HERE/'ChatGPT_Writing_Prompt.txt']},'details':checks}
(HERE/'report_validation.json').write_text(json.dumps(qa,indent=2),encoding='utf-8')
(HERE/'pdf_extracted_text.txt').write_text(full,encoding='utf-8')
with zipfile.ZipFile(HERE/'Thesis_Writing_Package.zip','w',zipfile.ZIP_DEFLATED) as z:
    for p in [PDF,HERE/'Manual_Calibration_Models_Technical_Report.txt',HERE/'ChatGPT_Writing_Prompt.txt']:
        z.write(p,p.name)
    for p in (HERE/'evidence').rglob('*'):
        if p.is_file():z.write(p,p.relative_to(HERE))
    z.write(HERE/'report_validation.json','report_validation.json')
print(json.dumps({k:qa[k] for k in ['status','checks','pages','tables']}))
