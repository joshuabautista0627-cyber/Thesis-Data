import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const here=path.dirname(fileURLToPath(import.meta.url));
const input=JSON.parse(await fs.readFile(path.join(here,'workbook_data.json'),'utf8'));
const wb=Workbook.create();
const letter=n=>{let s='';for(n++;n;n=Math.floor((n-1)/26))s=String.fromCharCode(65+(n-1)%26)+s;return s;};
for(const spec of input.sheets){
  const sheet=wb.worksheets.add(spec.name);sheet.showGridLines=false;
  const values=spec.rows;
  const range=sheet.getRangeByIndexes(0,0,values.length,values[0].length);
  range.values=values;
  range.format.font={name:'Arial',size:10,color:'#222222'};
  range.format.rowHeight=17;
  range.format.columnWidth=17;
  range.format.verticalAlignment='center';
  sheet.getRangeByIndexes(0,0,1,values[0].length).format={fill:'#234C61',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},rowHeight:32,wrapText:true};
  sheet.freezePanes.freezeRows(1);
  for(let j=0;j<values[0].length;j++){
    const col=sheet.getRangeByIndexes(1,j,values.length-1,1);
    if(values.slice(1).some(row=>typeof row[j]==='number'))col.setNumberFormat(/count|events|frames|row|taxel|target|n_|support|recordings/.test(values[0][j])&&!/mean|sd|ci|duration|rate|fraction/.test(values[0][j])?'#,##0':'0.0000');
    if(values[0][j].endsWith('_UTC'))col.setNumberFormat('yyyy-mm-dd hh:mm:ss.000 "UTC"');
    const maxLen=Math.max(...values.slice(0,150).map(row=>String(row[j]??'').length));
    sheet.getRange(`${letter(j)}1:${letter(j)}${values.length}`).format.columnWidth=Math.min(78,Math.max(14,maxLen*.95));
    if(values[0][j].endsWith('_UTC'))sheet.getRange(`${letter(j)}1:${letter(j)}${values.length}`).format.columnWidth=32;
  }
  if(spec.name==='Summary'){
    sheet.tabColor='#234C61';sheet.getRange(`A1:A${values.length}`).format.columnWidth=42;
    sheet.getRange(`B1:B${values.length}`).format.columnWidth=32;
    values.slice(1).forEach((row,i)=>{if(row[2]==='events'||row[2]==='recordings')sheet.getRange(`B${i+2}`).setNumberFormat('#,##0');});
    sheet.getRange(`C1:C${values.length}`).format.columnWidth=24;
    sheet.getRange(`D1:D${values.length}`).format.columnWidth=83;
    sheet.getRange(`D2:D${values.length}`).format.wrapText=true;
    sheet.getRange(`A2:A${values.length}`).format.wrapText=true;
    sheet.getRange(`A2:D${values.length}`).format.rowHeight=32;
  }
  if(spec.name==='Model-assisted evaluation'){
    sheet.getRange(`A1:A${values.length}`).format.columnWidth=40;
    sheet.getRange(`B1:C${values.length}`).format.columnWidth=21;
    sheet.getRange(`D1:F${values.length}`).format.columnWidth=16;
    sheet.getRange(`G1:G${values.length}`).format.columnWidth=85;
    sheet.getRange(`G2:G${values.length}`).format.wrapText=true;
    sheet.getRange(`A2:G${values.length}`).format.rowHeight=34;
    sheet.getRange(`E2:F${values.length}`).setNumberFormat('#,##0');
    values.slice(1).forEach((row,i)=>sheet.getRange(`B${i+2}:C${i+2}`).setNumberFormat(row[3]==='proportion'&&!/F1|squared/.test(row[0])?'0.00%':'0.0000'));
  }
  if(spec.name==='Press events')sheet.freezePanes.freezeColumns(2);
  if(spec.name==='Methods'){
    sheet.getRange(`A1:A${values.length}`).format.columnWidth=29;
    sheet.getRange(`B1:B${values.length}`).format.columnWidth=115;
    sheet.getRange(`B2:B${values.length}`).format.wrapText=true;
    sheet.getRange(`A2:B${values.length}`).format.rowHeight=44;
  }
}
wb.recalculate();
const inspected=await wb.inspect({kind:'table',range:'Summary!A1:D14',include:'values,formulas',tableMaxRows:14,tableMaxCols:4,maxChars:4000});
await fs.writeFile(path.join(here,'workbook_inspection.ndjson'),inspected.ndjson);
const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:50},summary:'Final workbook error scan'});
await fs.writeFile(path.join(here,'workbook_errors.ndjson'),errors.ndjson);
const workbookFile=await SpreadsheetFile.exportXlsx(wb);
await workbookFile.save(path.join(here,'global_characterization_statistics.xlsx'));
try {
  const preview=await wb.render({sheetName:'Summary',range:'A1:D14',scale:1,format:'png'});
  await fs.writeFile(path.join(here,'workbook_preview.png'),new Uint8Array(await preview.arrayBuffer()));
}catch(err){ console.log('PREVIEW_UNAVAILABLE',err.message); }
if(input.sheets.some(s=>s.name==='Model-assisted evaluation')){
  for(const [sheetName,range,file] of [['Model-assisted evaluation','A1:G13','model_workbook_preview.png'],['Methods','A18:B23','model_methods_preview.png']]){
    const preview=await wb.render({sheetName,range,scale:1,format:'png'});
    await fs.writeFile(path.join(here,file),new Uint8Array(await preview.arrayBuffer()));
  }
}
console.log('Workbook exported:',input.sheets.length,'sheets');
