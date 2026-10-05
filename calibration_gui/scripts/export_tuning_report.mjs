// Reuse the shipped Data Analytics renderer; verify a local static review copy.
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {resolve,dirname} from 'node:path';
import {pathToFileURL} from 'node:url';
import {chromium} from 'file:///C:/Users/DLSU/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
import {buildPortableArtifact} from 'file:///C:/Users/DLSU/.codex/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts/build_portable_artifact.mjs';
import {extractPortableChartSvgs} from 'file:///C:/Users/DLSU/.codex/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts/extract_portable_chart_svgs.mjs';
import {verifyPortableArtifactStructure} from 'file:///C:/Users/DLSU/.codex/plugins/cache/openai-curated-remote/data-analytics/0.2.10-13ceeea1f599/skills/build-report/scripts/verify_portable_artifact.mjs';

const input=resolve(process.argv[2]);
const htmlPath=resolve(process.argv[3]);
const output=dirname(htmlPath);
mkdirSync(output,{recursive:true});
const artifact=JSON.parse(readFileSync(input,'utf8'));
writeFileSync(htmlPath,buildPortableArtifact(artifact));
const executablePath='C:/Program Files/Google/Chrome/Application/chrome.exe';
const browser=await chromium.launch({executablePath,headless:true,args:['--disable-background-networking','--no-first-run']});
try {
  async function runDump({arguments: args}) {
    const size=args.find(a=>a.startsWith('--window-size=')).split('=')[1].split(',').map(Number);
    const context=await browser.newContext({viewport:{width:size[0],height:size[1]},
      colorScheme:args.includes('--blink-settings=preferredColorScheme=0')?'dark':'light',reducedMotion:'reduce',deviceScaleFactor:1});
    try {
      await context.route(/^https?:/,route=>route.abort());
      const page=await context.newPage();
      await page.goto(args.at(-1),{waitUntil:'load'});
      await page.waitForSelector('meta[data-portable-chart-extraction]',{state:'attached',timeout:20000});
      return {stdout:await page.content(),stderr:''};
    } finally {await context.close();}
  }
  const staticCharts=await extractPortableChartSvgs({htmlPath,browserExecutable:executablePath,runDump,readyTimeoutMs:15000});
  if(Object.keys(staticCharts).length!==artifact.manifest.charts.length)throw new Error('Missing native SVG charts');
  writeFileSync(htmlPath,buildPortableArtifact(artifact,{staticCharts}));
  const structure=verifyPortableArtifactStructure({artifactPath:input,htmlPath});
  const qa={structure,source:input,chartCount:Object.keys(staticCharts).length,
    conversion:'Packaged shared renderer and native SVG extractor, with exact-viewport Chrome adapter',views:[]};
  for(const [label,width,colorScheme] of [['desktop',1200,'light'],['mobile',390,'light'],['dark',1200,'dark']]) {
    const context=await browser.newContext({viewport:{width,height:900},colorScheme,reducedMotion:'reduce',javaScriptEnabled:false});
    try {
      await context.route(/^https?:/,route=>route.abort());
      const page=await context.newPage();
      await page.goto(pathToFileURL(htmlPath).href,{waitUntil:'load'});
      await page.screenshot({path:output+`/report-${label}.png`,fullPage:label!=='desktop'});
      const state=await page.evaluate(()=>({title:document.querySelector('.portable-fallback h1')?.textContent,
        charts:document.querySelectorAll('.portable-static-chart-light svg').length,
        tables:document.querySelectorAll('.portable-fallback table').length,
        bodyWidth:document.body.scrollWidth,viewport:innerWidth,
        text:document.querySelector('.portable-fallback')?.innerText}));
      if(!state.title||state.charts!==artifact.manifest.charts.length)throw new Error('Incomplete static report');
      if(label==='desktop') {
        for(const block of artifact.manifest.blocks.filter(b=>b.type==='chart'||b.type==='table')) {
          const loc=page.locator(`[data-artifact-block-id="${block.id}"]`).first();
          if(await loc.count())await loc.screenshot({path:output+`/review-${block.id}.png`});
        }
      }
      writeFileSync(output+`/text-${label}.txt`,state.text??'');
      delete state.text;qa.views.push({label,...state});
    } finally {await context.close();}
  }
  writeFileSync(output+'/report_qa.json',JSON.stringify(qa,null,2)+'\n');
  console.log(JSON.stringify(qa));
} finally {await browser.close();}
