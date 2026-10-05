import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { chromium } from 'file:///C:/Users/DLSU/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
import { buildPortableArtifact } from 'file:///C:/Users/DLSU/.codex/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts/build_portable_artifact.mjs';
import { extractPortableChartSvgs } from 'file:///C:/Users/DLSU/.codex/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts/extract_portable_chart_svgs.mjs';
import { verifyPortableArtifactStructure, verifyPortableArtifact } from 'file:///C:/Users/DLSU/.codex/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts/verify_portable_artifact.mjs';

const dir='C:/Users/DLSU/OneDrive/Documents/SOFTWARE CALIBRATION/output/pdf/manual_calibration_model_comparison';
const input='C:/Users/DLSU/OneDrive/Documents/SOFTWARE CALIBRATION/calibration_gui/analysis_outputs/live_sensor_manual_only/model_runs/retraining_20260909/artifact.json';
const executablePath='C:/Program Files/Google/Chrome/Application/chrome.exe';
const htmlPath=dir+'/report.html';
const artifact=JSON.parse(readFileSync(input,'utf8'));
const browser=await chromium.launch({executablePath,headless:true,args:['--disable-background-networking','--no-first-run']});
try {
  // Use the shipped SVG extraction probe and sanitizer with a Windows-compatible
  // browser context. Full Chrome's CLI window includes non-content dimensions.
  async function runDump({arguments: args}) {
    const size=args.find(a=>a.startsWith('--window-size=')).split('=')[1].split(',').map(Number);
    const dark=args.includes('--blink-settings=preferredColorScheme=0');
    const context=await browser.newContext({viewport:{width:size[0],height:size[1]},colorScheme:dark?'dark':'light',reducedMotion:'reduce',deviceScaleFactor:1});
    try {
      await context.route(/^https?:/,route=>route.abort());
      const page=await context.newPage();
      await page.goto(args.at(-1),{waitUntil:'load'});
      await page.waitForSelector('meta[data-portable-chart-extraction]',{state:'attached',timeout:15000});
      return {stdout:await page.content(),stderr:''};
    } finally {await context.close();}
  }
  const charts=await extractPortableChartSvgs({htmlPath,browserExecutable:executablePath,runDump,readyTimeoutMs:10000});
  if(Object.keys(charts).length!==4) throw new Error(`Expected four static charts; received ${Object.keys(charts).length}`);
  const html=buildPortableArtifact(artifact,{staticCharts:charts});
  writeFileSync(htmlPath,html);
  const structure=verifyPortableArtifactStructure({artifactPath:input,htmlPath});
  const qa={source:input,chartCount:Object.keys(charts).length,structure,conversion:'Saved canonical artifact -> packaged shared renderer -> extracted native SVGs -> Chrome PDF',browserAdaptation:'Playwright context used to set exact viewport and media for shipped extractor'};
  try {qa.packagedBrowserQA=await verifyPortableArtifact({artifactPath:input,htmlPath,browserExecutable:executablePath,timeoutMs:20000,screenshotPath:dir+'/verification_failure.png'});}
  catch(error) {qa.packagedBrowserQAFailure=String(error.message);}
  const context=await browser.newContext({viewport:{width:1200,height:900},colorScheme:'light',reducedMotion:'reduce'});
  const page=await context.newPage();
  await page.goto(pathToFileURL(htmlPath).href,{waitUntil:'load'});
  await page.emulateMedia({media:'print'});
  qa.printDom=await page.evaluate(()=>({charts:document.querySelectorAll('.portable-static-chart-light svg').length,
    text:document.querySelector('.portable-fallback')?.innerText,
    headers:[...document.querySelectorAll('.portable-fallback h1,.portable-fallback h2')].map(n=>n.textContent)}));
  writeFileSync(dir+'/static_export_qa.json',JSON.stringify(qa,null,2));
  console.log(JSON.stringify({chartCount:Object.keys(charts).length,structure,packagedBrowserQA:qa.packagedBrowserQA,packagedBrowserQAFailure:qa.packagedBrowserQAFailure,printChartCount:qa.printDom.charts}));
  await context.close();
} finally {await browser.close();}


