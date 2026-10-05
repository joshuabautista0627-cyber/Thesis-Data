import React from 'react';
import { DataComponent, EvidenceChart, ReportSection, RichNarrative, useDataApp } from '../../data-app-public.jsx';
import content from './report-content.json';
const previews={
 'https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/coefvari.htm':{title:'NIST: Coefficient of variation',summary:'Defines CV as sample standard deviation divided by the mean and explains ratio-scale and near-zero-mean limitations.',source:'NIST Dataplot',approvedForReport:true},
 'https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html':{title:'SciPy: Spearman rank correlation',summary:'Documents the monotonic association coefficient and cautions about small-sample p-value approximation.',source:'SciPy',approvedForReport:true}
};
export function ReportContent(){
 const {reviewedRows,appTitle,canEdit,mode,setAppTitle,visible}=useDataApp();
 const summaryIds=['integrity','recordings','repeatability','force','performance'];
 const sources=Object.fromEntries(summaryIds.map(id=>[id,reviewedRows(id)]));
 return <article className="report-content baurods-report" aria-label="BAURods data quality assessment">
  <header className="report-hero">
   <RichNarrative id="report:kicker" value="BAURODS / MANUAL CALIBRATION STUDY" className="baurods-kicker"/>
   <h1 data-data-app-title contentEditable={canEdit&&mode==='edit'} suppressContentEditableWarning onBlur={canEdit&&mode==='edit'?(e)=>setAppTitle(e.currentTarget.textContent):undefined}>{appTitle}</h1>
   <RichNarrative id="report:intro" value="Data quality assessment · 3 × 3 taxel array · Acquired 8 August 2026" className="report-deck"/>
  </header>
  {visible('executive-summary')&&<ReportSection id="executive-summary" title="Executive summary" queryId="integrity" queryIds={summaryIds} sourceRowsByQuery={sources} showHeading={false} className="report-summary">
   <RichNarrative id="executive-summary:body" value={content.summary} sourcePreviews={previews}/>
  </ReportSection>}
  {content.sections.map(section=><section className="baurods-section" key={section.id}>
   {visible(section.id)&&<ReportSection id={section.id} title={section.title} queryId={section.query} sourceRows={reviewedRows(section.query)} showHeading={false}>
    {section.text.split(/(\n\n\|[^]*?\|(?=\n\n))/g).filter(Boolean).map((block,index)=>{
      if(!block.trim().startsWith('|'))return <RichNarrative key={index} id={`${section.id}:body:${index}`} value={block.trim()} sourcePreviews={previews}/>;
      const lines=block.trim().split('\n').map(line=>line.split('|').slice(1,-1).map(cell=>cell.trim()));
      const headers=lines[0];const values=lines.slice(2);const query=section.id==='repeatability'?'joint_summary':section.query;
      return <DataComponent key={index} id={`${section.id}-table`} title={`${section.title} · summary table`} kind="table" queryId={query} sourceRows={reviewedRows(query)}>
       <div className="baurods-table-scroll" data-reviewed-rows><table aria-label={`${section.title} summary`}><thead><tr>{headers.map(h=><th key={h}>{h}</th>)}</tr></thead><tbody>{values.map((cells,i)=><tr key={i}>{cells.map((cell,j)=><td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>
      </DataComponent>;
    })}
   </ReportSection>}
   {section.chart&&visible(section.chart.id)&&<EvidenceChart id={section.chart.id} queryId={section.chart.query} title={section.chart.title} spec={section.chart.spec} rows={reviewedRows(section.chart.query)} sourceRows={reviewedRows(section.chart.query)} height={360}/>}
  </section>)}
 </article>;
}
