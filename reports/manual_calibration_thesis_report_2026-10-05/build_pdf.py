"""Render the source report as a PDF without relying on a Word installation."""
from pathlib import Path
from html import escape
import json
from docx import Document
from docx.text.paragraph import Paragraph as WordParagraph
from docx.table import Table as WordTable
from docx.oxml.ns import qn
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
OUT=ROOT/'output/pdf/manual_calibration_thesis_report_2026-10-05'
OUT.mkdir(parents=True,exist_ok=True)
fonts=Path('C:/Windows/Fonts')
for name,file in [('Calibri','calibri.ttf'),('Calibri-Bold','calibrib.ttf'),('Calibri-Italic','calibrii.ttf')]:
    pdfmetrics.registerFont(TTFont(name,str(fonts/file)))
pdfmetrics.registerFontFamily('Calibri',normal='Calibri',bold='Calibri-Bold',italic='Calibri-Italic',boldItalic='Calibri-Bold')
ss={
 'Normal':ParagraphStyle('body',fontName='Calibri',fontSize=10.2,leading=13.0,spaceAfter=7,splitLongWords=True),
 'Title':ParagraphStyle('title',fontName='Calibri-Bold',fontSize=25,leading=28,spaceAfter=12),
 'Subtitle':ParagraphStyle('subtitle',fontName='Calibri',fontSize=12,leading=15,spaceAfter=10),
 'Heading 1':ParagraphStyle('h1',fontName='Calibri-Bold',fontSize=17,leading=21,spaceAfter=12,keepWithNext=True),
 'Heading 2':ParagraphStyle('h2',fontName='Calibri-Bold',fontSize=12,leading=15,spaceBefore=8,spaceAfter=6,keepWithNext=True),
 'Caption':ParagraphStyle('caption',fontName='Calibri-Italic',fontSize=9,leading=11,spaceAfter=5,keepWithNext=True),
}
cellstyle=ParagraphStyle('cell',fontName='Calibri',fontSize=8.7,leading=10.8)
cellnum=ParagraphStyle('num',parent=cellstyle,alignment=TA_CENTER)
header=ParagraphStyle('header',parent=cellstyle,fontName='Calibri-Bold',textColor=colors.white)
width=A4[0]-108
story=[]
doc=Document(HERE/'Manual_Calibration_Models_Technical_Report.docx')
def fmt(t): return escape(t).replace('\n','<br/>')
for elem in doc.element.body:
    if elem.tag==qn('w:p'):
        wp=WordParagraph(elem,doc)
        if elem.xpath('.//w:br[@w:type="page"]'):
            story.append(PageBreak());continue
        if not wp.text.strip():continue
        style=ss.get(wp.style.name,ss['Normal'])
        story.append(Paragraph(fmt(wp.text),style))
    elif elem.tag==qn('w:tbl'):
        wt=WordTable(elem,doc)
        data=[]
        for ri,r in enumerate(wt.rows):
            data.append([Paragraph(fmt(c.text),header if ri==0 else (cellstyle if ci==0 or len(c.text)>65 else cellnum)) for ci,c in enumerate(r.cells)])
        ws=[c.width for c in wt.columns];total=sum(ws)
        tab=Table(data,colWidths=[width*w/total for w in ws],repeatRows=1,hAlign='LEFT')
        tab.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#233747')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F1F4F6')]),('GRID',(0,0),(-1,-1),.4,colors.HexColor('#D9D9D9')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        story.extend([tab,Spacer(1,9)])
def footer(canvas,doc):
    canvas.saveState();canvas.setFont('Calibri',8);canvas.setFillColor(colors.HexColor('#555555'))
    canvas.drawString(54,27,'Manual calibration models | Technical report | 5 October 2026')
    canvas.drawRightString(A4[0]-54,27,str(doc.page));canvas.restoreState()
path=OUT/'Manual_Calibration_Models_Technical_Report.pdf'
pdf=SimpleDocTemplate(str(path),pagesize=A4,rightMargin=54,leftMargin=54,topMargin=44,bottomMargin=46,title='Manual Calibration Models Technical Report for Thesis Writing',author='',pageCompression=1)
pdf.build(story,onFirstPage=footer,onLaterPages=footer)
print(path)
