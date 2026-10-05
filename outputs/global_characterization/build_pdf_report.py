"""Create the standalone, paginated technical report from reviewed Markdown."""
from pathlib import Path
import re, json, hashlib
from html import escape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether, PageBreak, CondPageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image as PILImage
from pypdf import PdfReader
import pdfplumber

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
DEST=ROOT/'output/pdf/BAURods_Global_Sensor_Characterization_Technical_Report.pdf'
QA=ROOT/'tmp/pdfs/global_characterization'
DEST.parent.mkdir(parents=True,exist_ok=True);QA.mkdir(parents=True,exist_ok=True)
for name,file in [('Arial','arial.ttf'),('Arial-Bold','arialbd.ttf'),('Arial-Italic','ariali.ttf'),('Arial-BoldItalic','arialbi.ttf')]:
    pdfmetrics.registerFont(TTFont(name,str(Path('C:/Windows/Fonts')/file)))
pdfmetrics.registerFontFamily('Arial',normal='Arial',bold='Arial-Bold',italic='Arial-Italic',boldItalic='Arial-BoldItalic')
BLUE=colors.HexColor('#234C61');GRAY=colors.HexColor('#53616B')
styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='Body',fontName='Arial',fontSize=9.5,leading=14,spaceAfter=8,textColor=colors.HexColor('#20272D'),splitLongWords=True,allowWidows=0,allowOrphans=0))
styles.add(ParagraphStyle(name='Section',parent=styles['Body'],fontName='Arial-Bold',fontSize=16,leading=20,spaceBefore=15,spaceAfter=10,keepWithNext=True,textColor=BLUE))
styles.add(ParagraphStyle(name='Sub',parent=styles['Body'],fontName='Arial-Bold',fontSize=11,leading=15,spaceBefore=9,spaceAfter=7,keepWithNext=True,textColor=BLUE))
styles.add(ParagraphStyle(name='Cell',parent=styles['Body'],fontSize=8,leading=11,spaceAfter=0))
styles.add(ParagraphStyle(name='HeadCell',parent=styles['Cell'],fontName='Arial-Bold',textColor=colors.white))
styles.add(ParagraphStyle(name='Caption',parent=styles['Body'],fontSize=8,leading=11,textColor=GRAY,spaceAfter=12))
styles.add(ParagraphStyle(name='TitleCustom',parent=styles['Body'],fontName='Arial-Bold',fontSize=26,leading=31,spaceAfter=15,textColor=BLUE))
WIDTH=A4[0]-96
story=[];table_count=0;figures=[];headings=[]

def inline(text):
    text=text.replace('–','-').replace('—','-').replace('−','-').replace('\u2011','-')
    text=escape(text).replace('&lt;br/&gt;','<br/>')
    def link(m):
        label,url=m.groups()
        if url.startswith('http'):return f'<link href="{url}" color="#285A77">{label}</link>'
        return label+' [S1]' if 'Technical_Report.pdf' in url else label
    text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,text)
    text=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',text)
    text=re.sub(r'`([^`]+)`',r'<font size="8.5">\1</font>',text)
    return text

def p(text,style='Body'):return Paragraph(inline(text),styles[style])
def heading(text,level=2):
    story.append(CondPageBreak(150 if level==2 else 90))
    para=p(text,'Section' if level==2 else 'Sub')
    para.bookmark='section_'+str(len(headings));para.outline_title=text
    headings.append(text);story.append(para)

class Report(SimpleDocTemplate):
    def afterFlowable(self,flow):
        if hasattr(flow,'bookmark'):
            self.canv.bookmarkPage(flow.bookmark)
            self.canv.addOutlineEntry(flow.outline_title,flow.bookmark,0,False)

def page(canvas,doc):
    canvas.setFont('Arial',8);canvas.setFillColor(GRAY)
    canvas.drawString(48,A4[1]-29,'BAURods | Global sensor characterization and model-assisted sensing')
    canvas.setStrokeColor(colors.HexColor('#CBD5DC'));canvas.line(48,A4[1]-36,A4[0]-48,A4[1]-36)
    canvas.drawString(48,27,'Technical report | 6 October 2026')
    canvas.drawRightString(A4[0]-48,27,str(doc.page))

headers={'variable':'Measure','unit':'Unit','n':'Count','sd':'SD','minimum':'Minimum','maximum':'Maximum',
 'mean_ci95_low':'Mean CI lower','mean_ci95_high':'Mean CI upper','cv_percent':'CV (%)','r2':'R²',
 'rmse':'RMSE (V units)','mae':'MAE (V units)','recording_cv_rmse':'Held-out RMSE (V units)',
 'recording_cv_r2':'Held-out R²','force_coefficient_V_per_N':'Force coefficient (V units/N)',
 'mean_recording_rmse':'Mean recording RMSE','se_recording_rmse':'SE recording RMSE',
 'min_force_N':'Min force (N)','max_force_N':'Max force (N)','response_mean_V':'Mean response (V units)',
 'response_sd_V':'Response SD (V units)','response_cv_percent':'Response CV (%)',
 'residual_sd_V':'Residual SD (V units)','residual_rmse_V':'Residual RMSE (V units)','residual_iqr_V':'Residual IQR (V units)',
 'force_band':'Force band (N)'}

def table(rows):
    global table_count
    table_count+=1
    n=len(rows[0]);groups=[list(range(n))] if n<=5 else [[0]+list(range(k,min(k+4,n))) for k in range(1,n,4)]
    for part,columns in enumerate(groups):
        title=f'Table {table_count}'+(f', part {part+1}' if len(groups)>1 else '')
        cap=p(title,'Caption');cap.keepWithNext=True;story.append(cap)
        if n==3:widths=[WIDTH*.29,WIDTH*.33,WIDTH*.38]
        else:widths=[WIDTH*.31]+[(WIDTH*.69)/(len(columns)-1)]*(len(columns)-1)
        data=[]
        for ri,row in enumerate(rows):
            values=[]
            for j in columns:
                value=row[j].strip()
                if ri==0:value=headers.get(value,value.replace('_',' ').capitalize())
                value=value.replace('(1.1500000000000001, 6.121]','(1.150, 6.121]')
                if ri and j==0 and '_' in value:value=value.replace('_',' ')
                values.append(p(value,'HeadCell' if ri==0 else 'Cell'))
            data.append(values)
        t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),BLUE),('VALIGN',(0,0),(-1,-1),'TOP'),
          ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F1F5F7')]),
          ('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),
          ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
          ('LINEBELOW',(0,-1),(-1,-1),.4,colors.HexColor('#B9C7CF'))]))
        story.extend([t,Spacer(1,10)])

captions={
 'fig10':'Figure 10. Model-assisted performance on matched held-out frames. Six outer TEST groups; equal recording weights; eligible press force 0.05-3 N. Fixed-family comparators were identified retrospectively; the tuned result evaluates the nested selector.',
 'fig01':'Figure 1. Global applied peak force and optical response of the complete BAURods skin. Each point represents one retained force-segmented press; observations remain clustered in recordings.',
 'fig02':'Figure 2. Global fitted force-response characteristic. The model describes archived optical response in V digital units; its slope is not an intrinsic material sensitivity.',
 'fig03':'Figure 3. Residual diagnostics for the global force-response model.',
 'fig04':'Figure 4. Distribution of global optical response across retained presses.',
 'fig05':'Figure 5. Force-conditioned response dispersion under manual pressing. This is a repeatability proxy, not controlled fixed-force metrological repeatability.',
 'fig06':'Figure 6. Recorded baseline stability and unloaded optical traces.',
 'fig07':'Figure 7. Sampled force and optical behavior during repeated manual presses. Temporal filtering and sparse samples prevent inference of intrinsic response or recovery time.',
 'fig08':'Figure 8. Archived 5-V acquisition gate: 5,357 retained presses and 11 no-contact recording windows. These event/window metrics are separate from the learned frame-level detector.',
 'fig09':'Figure 9. Archived-gate press-level localization. No-detection outcomes remain in the denominator; this matrix does not describe the later learned localization model.',
 'spatial_residuals':'Supplementary Figure S1. Spatial consistency of recording-mean residuals. Error bars are 95% t intervals across eight recording means per location.'}

def figure(label,relative):
    source=(HERE/relative).resolve();assert source.exists(),source
    with PILImage.open(source) as im:w,h=im.size
    scale=min(WIDTH/w,355/h)
    pic=Image(str(source),width=w*scale,height=h*scale)
    pic.hAlign='CENTER'
    key=next((k for k in captions if source.stem.startswith(k)),None)
    caption=captions.get(key,label)
    story.append(KeepTogether([Spacer(1,6),pic,Spacer(1,7),p(caption,'Caption')]))
    figures.append(str(source))

text=(HERE/'Global_Sensor_Characterization_Report.md').read_text(encoding='utf-8')
text=re.sub(r'<!--.*?-->','',text,flags=re.S)
lines=text.splitlines()
story.extend([Spacer(1,20),p('BAURods<br/>Global Sensor Characterization','TitleCustom'),
              p('Technical report on the integrated 3 × 3 visuotactile sensing skin','Sub'),
              p('Prepared 6 October 2026 | Manual acquisition: 8 August 2026','Caption')])
i=0
while i<len(lines):
    line=lines[i].strip()
    if not line or line.startswith('# ') or line.startswith('Physical analysis:'):i+=1;continue
    if line.startswith('### '):
        title=line[4:]
        if title=='Model-assisted sensing performance':story.append(PageBreak())
        heading(title,3);i+=1;continue
    if line.startswith('## '):
        title=line[3:]
        if title=='Global Characterization of the BAURods Visuotactile Sensing Skin':story.append(PageBreak())
        heading(title);i+=1;continue
    match=re.match(r'!\[([^\]]*)\]\(([^)]+)\)',line)
    if match:figure(*match.groups());i+=1;continue
    if line.startswith('|'):
        rows=[]
        while i<len(lines) and lines[i].strip().startswith('|'):
            cells=[c.strip() for c in lines[i].strip().strip('|').split('|')]
            if not all(re.fullmatch(r'[-: ]+',c) for c in cells):rows.append(cells)
            i+=1
        table(rows);continue
    paragraph=[line];i+=1
    while i<len(lines) and lines[i].strip() and not lines[i].startswith(('#','|','![')):
        paragraph.append(lines[i].strip());i+=1
    story.append(p(' '.join(paragraph)))

story.append(PageBreak());heading('Appendix A. Thesis-ready Results and Discussion')
thesis=(HERE/'Thesis_Results_and_Discussion.md').read_text(encoding='utf-8')
for para in thesis.split('\n\n'):
    if para.strip() and not para.startswith('#'):story.append(p(para.strip().replace('\n',' ')))
story.append(PageBreak());heading('Appendix B. Source register and validation')
sources=[
 '[S1] Manual Calibration Models Technical Report for Thesis Writing, prepared 5 October 2026. Tables 9-11 document the principal nested model results; Table 19 the combined pipeline; Table 23 offline inference timing. This report is supplied by the researcher and traces to the same manual archive.',
 '[S2] Manual Calibration (2).zip. SHA-256: e039dcd49f3390d34dc979490dcf46a6ecc43d9e6a2aec08e4952acc14b5006a. The primary physical analysis uses the archived synchronized measurements and raw reference force.',
 '[S3] Saved fixed and tuned model runs: retraining_20260909 and tuning_20260909. The model extension independently recomputes selected outer-prediction metrics, checks matching held-out frame identities and truth, and retains the original evaluation weights.',
 '[S4] Global analysis outputs: global_force_response_data.csv, fitted_model_comparison.csv, baseline_statistics.csv, contact_detection_metrics.csv, localization_per_class.csv, and the spatial diagnostics. Complete numerical evidence and reproducible scripts accompany the analysis package.',
 'V means the OpenCV HSV brightness channel in image digital units, not volts. N denotes newtons; MAE, mean absolute error; RMSE, root mean squared error; ROI, region of interest; FPR, false-positive rate; CI, confidence interval; SD, standard deviation.',
 'Verification: all 5,357 retained event maxima were checked against source measurements. The model extension reproduced 36 principal and combined metrics, with a maximum absolute difference of 2.34 × 10^-15. The 17-sheet numerical workbook was checked across 212,817 cells with zero mismatches. These checks establish numerical consistency, not independent physical or prospective validation.',
 'Figure numbering follows the reproducible analysis package. Figure 10 appears first to introduce the model-assisted findings; Figures 1-9 retain their original physical-characterization numbering. Wide numerical tables are split into labelled parts to preserve legibility and all reported values.'
]
for s in sources:story.append(p(s))
doc=Report(str(DEST),pagesize=A4,rightMargin=48,leftMargin=48,topMargin=51,bottomMargin=47,
 title='BAURods Global Sensor Characterization Technical Report',author='',subject='Global physical characterization and retrospective model-assisted sensing')
doc.build(story,onFirstPage=page,onLaterPages=page)
r=PdfReader(DEST);full='\n'.join(page.extract_text() for page in r.pages)
required=['98.50%','84.40%','0.499','83.61%','5,357','0.597%','0.12106','0.2229','41.10','1.241','42.71%',
          '1. Global Response Definition','12. Characterization Summary','Appendix A.','Appendix B.']
assert all(s in full for s in required),[s for s in required if s not in full]
assert len(figures)==11,len(figures)
issues=[]
with pdfplumber.open(DEST) as pdf:
    for i,page in enumerate(pdf.pages,1):
        for c in page.chars:
            if c['x0']<44 or c['x1']>page.width-43 or c['top']<15 or c['bottom']>page.height-17:
                issues.append([i,c['text'],c['x0'],c['top']])
assert not issues,issues[:20]
result={'status':'PASS','pages':len(r.pages),'figures':len(figures),'tables':table_count,
        'required_claims_present':True,'text_bounds_passed':True,'pdf_sha256':hashlib.sha256(DEST.read_bytes()).hexdigest(),
        'source_markdown_sha256':hashlib.sha256((HERE/'Global_Sensor_Characterization_Report.md').read_bytes()).hexdigest(),
        'pdf':str(DEST),'bytes':DEST.stat().st_size}
(QA/'build_validation.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
