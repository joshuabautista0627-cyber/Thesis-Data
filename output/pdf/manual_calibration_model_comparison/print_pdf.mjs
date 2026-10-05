import {readFileSync,writeFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {chromium} from 'file:///C:/Users/DLSU/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
const dir='C:/Users/DLSU/OneDrive/Documents/SOFTWARE CALIBRATION/output/pdf/manual_calibration_model_comparison';
let html=readFileSync(dir+'/report.html','utf8');
html=html.replaceAll('>Total li…</tspan>','>Total light</tspan>');
// The shared reader inferred Python's pathlib import as a SQL table. Correct
// those visible source lines using the unchanged source file identities.
const replacements={
  'Frozen experiment protocol and source provenance':'protocol.json',
  'Contact-gated combination metrics':'end_to_end_metrics.json',
  'Independent metric and inference audit':'validation_audit.json',
  'Offline inference timing':'inference_timing.json'
};
for(const [label,file] of Object.entries(replacements)){
  const old=`<strong>Source: ${label}</strong><span class="portable-source-meta">Table: pathlib</span>`;
  html=html.replaceAll(old,`<strong>Source: ${label}</strong><span class="portable-source-meta">File: ${file}</span>`);
}
const style=`<style id="pdf-print-repairs">
@page{size:A4;margin:16mm 16mm 17mm;@bottom-right{content:counter(page) " / " counter(pages);font-size:8pt;color:#707070}}
@media print{
html,body{font-size:10pt!important;line-height:1.42!important}
.portable-fallback{padding:0!important;width:100%!important;max-width:none!important}
.portable-page-header h1{font-size:22pt!important}
.portable-surface-label{font-size:9pt!important}
.portable-page-meta,.portable-status{display:none!important}
.portable-block-stack{display:block!important;margin-top:0!important}
.portable-block-stack>div,.portable-block-stack>section,.portable-block-stack>figure{margin:0 0 17pt!important}
.portable-markdown{max-width:none!important;break-inside:auto!important}
[data-artifact-block-id="bins_section"],[data-artifact-block-id="coverage_section"]{break-inside:avoid!important}
.portable-markdown h2{font-size:13pt!important;break-after:avoid!important;margin:0 0 9pt!important}
.portable-markdown p{margin:0 0 8pt!important;orphans:3;widows:3}
.portable-markdown ul{margin:0 0 8pt}
.portable-table-scroll{overflow:visible!important;max-width:100%!important}
.portable-table-scroll table{width:100%!important;table-layout:fixed!important}
.portable-table-scroll th,.portable-table-scroll td{font-size:8.3pt!important;line-height:1.32!important;padding:5pt 6pt 5pt 0!important;white-space:normal!important;overflow-wrap:break-word!important}
.portable-table-scroll th{height:auto!important}
.portable-source-meta,.portable-source-summary-content,.portable-source-tooltip-content{font-size:7.5pt!important;line-height:1.25!important}
.portable-source-value,.portable-source-value-text{position:static!important;display:inline!important;width:auto!important;min-width:0!important;inset:auto!important;transform:none!important;font-size:inherit!important}
.portable-source-value::before,.portable-source-value::after,.portable-source-value>.portable-source-tooltip-content{display:none!important;content:none!important}
.portable-inline-source{margin-top:5pt!important;padding-top:3pt!important}
.portable-static-chart-light svg{width:100%!important;height:auto!important}
.portable-content-card,.portable-chart-summary{break-inside:avoid!important}
.portable-visual-header{margin-bottom:8pt!important}
}
</style>`;
html=html.replace('</head>',style+'</head>');
writeFileSync(dir+'/report.print.html',html);
const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
try{
 const context=await browser.newContext({viewport:{width:1200,height:900},colorScheme:'light',reducedMotion:'reduce',javaScriptEnabled:false});
 await context.route(/^https?:/,route=>route.abort());
 const page=await context.newPage();
 await page.goto(pathToFileURL(dir+'/report.print.html').href,{waitUntil:'load'});
 await page.emulateMedia({media:'print',colorScheme:'light'});
 await page.evaluate(()=>document.fonts.ready);
 const chartCount=await page.locator('.portable-static-chart-light svg').count();
 if(chartCount!==4)throw new Error(`Expected 4 charts, got ${chartCount}`);
 await page.pdf({path:dir+'/Manual_Calibration_Model_Comparison.pdf',format:'A4',printBackground:true,preferCSSPageSize:true,displayHeaderFooter:false});
 writeFileSync(dir+'/print_receipt.json',JSON.stringify({renderer:'Installed Chrome via Playwright PDF API',primaryCliResult:'Chrome CLI exited without producing a PDF; used the same Chrome engine through its supported PDF API.',source:'report.print.html',chartCount,repairs:['Print stylesheet for paper sizing and table wrapping','Corrected falsely inferred pathlib table labels to actual source files'],pdf:'Manual_Calibration_Model_Comparison.pdf'},null,2));
 console.log('PDF written with four native vector charts.');
}finally{await browser.close();}
