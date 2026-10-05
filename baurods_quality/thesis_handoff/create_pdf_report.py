"""Typeset the complete reviewed technical handoff as a searchable PDF."""
from pathlib import Path
import html
import re
import textwrap
import json
from reportlab.pdfgen import canvas
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph,
    Spacer, PageBreak, Table, TableStyle, Image, KeepTogether, CondPageBreak)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image as PILImage
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parent
WORK=ROOT.parent.parent
OUTPUT=WORK/'output/pdf'
OUTPUT.mkdir(parents=True,exist_ok=True)
SOURCE=ROOT/'BAURods_Manual_Calibration_Technical_Report.md'
PDF=OUTPUT/'BAURods_Manual_Calibration_Technical_Report.pdf'
FONT=Path('C:/Windows/Fonts')
for name,file in [('Body','calibri.ttf'),('BodyBold','calibrib.ttf'),('BodyItalic','calibrii.ttf'),('BodyBI','calibriz.ttf'),('Mono','consola.ttf')]:
    pdfmetrics.registerFont(TTFont(name,str(FONT/file)))
pdfmetrics.registerFontFamily('Body',normal='Body',bold='BodyBold',italic='BodyItalic',boldItalic='BodyBI')

NAVY=colors.HexColor('#18354b');TEAL=colors.HexColor('#247477')
INK=colors.HexColor('#273846');MUTED=colors.HexColor('#596c78')
LIGHT=colors.HexColor('#f1f5f7');RULE=colors.HexColor('#ccd8df')
PW,PH=A4;MARGIN=46;WIDTH=PW-2*MARGIN
styles={
 'body':ParagraphStyle('BodyText',fontName='Body',fontSize=10.3,leading=14.2,textColor=INK,spaceAfter=6,splitLongWords=True,allowWidows=0,allowOrphans=0),
 'part':ParagraphStyle('Part',fontName='BodyBold',fontSize=21,leading=25,textColor=NAVY,spaceAfter=17,keepWithNext=True),
 'section':ParagraphStyle('Section',fontName='BodyBold',fontSize=13.3,leading=17,textColor=NAVY,spaceBefore=12,spaceAfter=7,keepWithNext=True),
 'minor':ParagraphStyle('Minor',fontName='BodyBold',fontSize=11,leading=14,textColor=TEAL,spaceBefore=10,spaceAfter=6,keepWithNext=True),
 'cell':ParagraphStyle('Cell',fontName='Body',fontSize=8.2,leading=10.7,textColor=INK,splitLongWords=True),
 'numcell':ParagraphStyle('NumCell',fontName='Body',fontSize=7.7,leading=10.3,textColor=INK,splitLongWords=True),
 'headcell':ParagraphStyle('HeadCell',fontName='BodyBold',fontSize=8.1,leading=10.5,textColor=colors.white,splitLongWords=True),
 'code':ParagraphStyle('Code',fontName='Mono',fontSize=7.5,leading=10.8,textColor=INK,spaceAfter=1,splitLongWords=True),
 'caption':ParagraphStyle('Caption',fontName='BodyItalic',fontSize=8.9,leading=12,textColor=MUTED,spaceBefore=5,spaceAfter=12),
 'quote':ParagraphStyle('Quote',fontName='Body',fontSize=10,leading=14,textColor=INK,leftIndent=12,rightIndent=12,spaceAfter=9,borderColor=TEAL,borderWidth=1,borderPadding=10),
 'small':ParagraphStyle('Small',fontName='Body',fontSize=9,leading=12.5,textColor=MUTED,spaceAfter=8),
}

def clean(text):
    return text.replace('\u2011','-').replace('\u2013','-').replace('\u2014',' - ').replace('\u2212','-').replace('\u00a0',' ')

def inline(text):
    text=clean(text)
    # Protect inline code and links before applying emphasis.
    blocks=[]
    def protect(s):blocks.append(s);return f'ZZZPROTECTED{len(blocks)-1}ZZZ'
    text=re.sub(r'`([^`]+)`',lambda m:protect('<font name="Mono" size="8">'+html.escape(m[1])+'</font>'),text)
    def link(m):
        label,url=m[1],m[2]
        if url.startswith(('https://','http://')):
            return protect('<link href="'+html.escape(url,quote=True)+'" color="#247477">'+html.escape(label)+'</link>')
        return protect(html.escape(label))
    text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,text)
    text=html.escape(text)
    text=re.sub(r'\*\*([^*]+)\*\*',r'<b>\1</b>',text)
    text=re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)',r'<i>\1</i>',text)
    for i,block in enumerate(blocks):text=text.replace(f'ZZZPROTECTED{i}ZZZ',block)
    return text

class ReportDoc(BaseDocTemplate):
    def afterFlowable(self,flowable):
        if isinstance(flowable,Paragraph) and getattr(flowable,'_toc_level',None) is not None:
            level=flowable._toc_level;title=flowable.getPlainText();key=flowable._bookmark
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title,key,level=level,closed=False)
            self.notify('TOCEntry',(level,title,self.page,key))

def furniture(c,doc):
    if doc.page==1:return
    c.saveState();c.setStrokeColor(RULE);c.setLineWidth(.5)
    c.line(MARGIN,PH-36,PW-MARGIN,PH-36)
    c.setFont('BodyBold',8);c.setFillColor(MUTED)
    c.drawString(MARGIN,PH-27,'BAURODS  /  MANUAL CALIBRATION')
    c.setFont('Body',8);c.drawRightString(PW-MARGIN,PH-27,'Technical assessment report')
    c.line(MARGIN,36,PW-MARGIN,36)
    c.drawString(MARGIN,23,'Evidence and methodology for thesis writing  |  5 October 2026')
    c.drawRightString(PW-MARGIN,23,str(doc.page));c.restoreState()

doc=ReportDoc(str(PDF),pagesize=A4,leftMargin=MARGIN,rightMargin=MARGIN,
    topMargin=51,bottomMargin=49,title='BAURods Manual Calibration Data Quality Technical Report',
    author='BAURods research project',subject='Completed manual-calibration data quality assessment and thesis-writing handoff')
doc.addPageTemplates(PageTemplate(id='Report',frames=[Frame(MARGIN,49,WIDTH,PH-100,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],onPage=furniture))
story=[]
cover_tag=ParagraphStyle('CoverTag',fontName='BodyBold',fontSize=10,leading=14,textColor=TEAL,spaceAfter=24)
cover_title=ParagraphStyle('CoverTitle',fontName='BodyBold',fontSize=32,leading=36,textColor=NAVY,spaceAfter=16)
cover_sub=ParagraphStyle('CoverSub',fontName='Body',fontSize=17,leading=23,textColor=MUTED,spaceAfter=32)
story += [Spacer(1,62),Paragraph('BAURODS RESEARCH DOCUMENTATION',cover_tag),
    Paragraph('Manual calibration<br/>data quality assessment',cover_title),
    Paragraph('Technical report and thesis-writing handoff',cover_sub)]
story.append(Paragraph('Process, methodology, verified findings, interpretation limits, and reproducibility',styles['section']))
story.append(Spacer(1,18))
for t in ['Acquisition: 8 August 2026  |  Source session: S3',
          'Assessment: 5 October 2026  |  Version 1.0',
          '83 recordings  /  29,318 camera frames  /  nine taxels']:
    story.append(Paragraph(t,styles['small']))
story.append(Spacer(1,30))
story.append(Paragraph('This report covers the completed manual-calibration data quality assessment. Later trained-model results belong to the separate model-training report.',styles['body']))
story.append(Paragraph('The complete technical narrative is preserved in this PDF. The original archive remains unchanged.',styles['small']))
story.append(PageBreak())
story.append(Paragraph('Contents',styles['part']))
toc=TableOfContents()
toc.levelStyles=[ParagraphStyle('TOC0',fontName='BodyBold',fontSize=11,leading=15,textColor=NAVY,spaceBefore=9,leftIndent=0,firstLineIndent=0),
                 ParagraphStyle('TOC1',fontName='Body',fontSize=8.5,leading=12,textColor=MUTED,leftIndent=13,firstLineIndent=0,spaceBefore=1)]
story += [toc,PageBreak()]

heading_count=0;part=None;images=0;tables=0;paragraphs=0
def heading(title,kind,toclevel=None):
    global heading_count
    heading_count+=1
    p=Paragraph(inline(title),styles[kind])
    if toclevel is not None:p._toc_level=toclevel;p._bookmark=f'section-{heading_count}'
    return p

def md_table(lines):
    global tables
    rows=[[c.strip().replace('\\|','|') for c in l.strip().strip('|').split('|')] for l in lines]
    rows=[r for r in rows if not all(re.fullmatch(r':?-+:?',c.replace(' ','')) for c in r)]
    n=len(rows[0]);assert all(len(r)==n for r in rows),rows
    if n>=8:
        widths=[37,28]+[(WIDTH-65)/(n-2)]*(n-2)
    elif n==4:widths=[WIDTH*.36,WIDTH*.18,WIDTH*.28,WIDTH*.18]
    elif n==3:widths=[WIDTH*.29,WIDTH*.35,WIDTH*.36]
    elif n==2:widths=[WIDTH*.44,WIDTH*.56]
    else:widths=[WIDTH/n]*n
    data=[[Paragraph(inline(c),styles['headcell'] if i==0 else styles['numcell'] if n>=8 else styles['cell']) for c in r] for i,r in enumerate(rows)]
    t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),NAVY),('VALIGN',(0,0),(-1,-1),'TOP'),
        ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
        ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,LIGHT]),
        ('LINEBELOW',(0,0),(-1,0),.6,NAVY),('LINEBELOW',(0,1),(-1,-1),.25,RULE)]))
    tables+=1;return [t,Spacer(1,12)]

lines=SOURCE.read_text(encoding='utf-8').splitlines();i=0
story.append(heading('Purpose and reading guide','part',0))
while i<len(lines):
    line=lines[i].strip()
    if not line:i+=1;continue
    if line.startswith('# '):i+=1;continue
    if line.startswith('```'):
        i+=1;code=[]
        while i<len(lines) and not lines[i].strip().startswith('```'):
            code.extend(textwrap.wrap(clean(lines[i]),width=92,break_long_words=True,replace_whitespace=False,drop_whitespace=False) or [' ']);i+=1
        rows=[[Paragraph(html.escape(s).replace(' ','&nbsp;'),styles['code'])] for s in code]
        t=Table(rows,colWidths=[WIDTH],hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),LIGHT),('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),2),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
        story.extend([Spacer(1,3),t,Spacer(1,10)]);i+=1;continue
    if line.startswith('|'):
        block=[]
        while i<len(lines) and lines[i].strip().startswith('|'):block.append(lines[i].strip());i+=1
        story.extend(md_table(block));continue
    match=re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)',line)
    if match:
        path=(ROOT/match[2]).resolve()
        with PILImage.open(path) as img:w,h=img.size
        factor=min(WIDTH/w,370/h)
        im=Image(str(path),width=w*factor,height=h*factor)
        story.append(KeepTogether([im,Paragraph(inline(match[1])+' - reviewed assessment figure.',styles['caption'])]));images+=1;i+=1;continue
    if line.startswith('## '):
        title=line[3:]
        if title.startswith('Part '):
            part=title.split('  ')[0];story.extend([PageBreak(),heading(title,'part',0)])
        else:story.append(heading(title,'section'))
        i+=1;continue
    if line.startswith('### '):
        title=line[4:]
        if title=='Archived localization and contact-proxy calculations':story.append(CondPageBreak(360))
        story.append(heading(title,'section',1 if part=='Part III' else None));i+=1;continue
    if line.startswith('#### '):story.append(heading(line[5:],'minor'));i+=1;continue
    if line.startswith('> '):
        story.extend([Spacer(1,10),Paragraph(inline(line[2:]),styles['quote']),Spacer(1,8)]);i+=1;paragraphs+=1;continue
    if re.match(r'^(\d+\. |[-*] )',line):
        marker=re.match(r'^(\d+\.|[-*])\s+',line).group(1)
        txt=re.sub(r'^(\d+\.|[-*])\s+','',line)
        st=ParagraphStyle('List',parent=styles['body'],leftIndent=16,firstLineIndent=-14,spaceAfter=5)
        story.append(Paragraph(inline(marker+' '+txt),st));i+=1;paragraphs+=1;continue
    body=[line];i+=1
    while i<len(lines) and lines[i].strip() and not re.match(r'^(#|\||```|!\[|> |\d+\. |[-*] )',lines[i].strip()):body.append(lines[i].strip());i+=1
    story.append(Paragraph(inline(' '.join(body)),styles['body']));paragraphs+=1

doc.multiBuild(story)
reader=PdfReader(str(PDF));text='\n'.join(p.extract_text() or '' for p in reader.pages)
assert len(reader.pages)>20
for expected in ['29,318','26,786','0.126932','0.520','Part VII','360','1.298','107'][:7]:
    assert expected in text,expected
assert '\u25a0' not in text
receipt={'pdf':str(PDF),'pages':len(reader.pages),'embedded_figures':images,'tables':tables,'narrative_paragraphs':paragraphs,'bytes':PDF.stat().st_size,'source':str(SOURCE),'searchable_text_chars':len(text)}
(OUTPUT/'pdf_build_verification.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps(receipt,indent=2))
