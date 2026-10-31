import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
const require=createRequire(import.meta.url);
// 신뢰할 수 있는 기존 Node 의존성만 사용한다. 자동 설치·다운로드는 하지 않는다.
const cli=process.argv.slice(2),moduleAt=cli.indexOf('--node-modules');
let nodeModules=null;
if(moduleAt>=0){
  const value=cli[moduleAt+1];
  if(!value||value.startsWith('--'))throw Error('--node-modules requires a directory');
  nodeModules=resolve(value);cli.splice(moduleAt,2);
}
const dependency=name=>require(nodeModules===null?name:resolve(nodeModules,name));
const {chromium}=dependency('playwright');
const media=dirname(fileURLToPath(import.meta.url));
const browser=await chromium.launch({headless:true,channel:'msedge',args:['--disable-background-timer-throttling']});
try{const page=await browser.newPage({viewport:{width:1080,height:1920},deviceScaleFactor:1});await page.route('**/*',route=>route.abort());await page.setContent(await readFile(resolve(media,'promo-canvas.html'),'utf8'));await page.evaluate(data=>window.prepare(data),(await readFile(resolve(media,'../../../desktop/assets/brand/gukgam-live-1024.png'))).toString('base64'));const frames=resolve(media,'canvas-design-review');await mkdir(frames,{recursive:true});for(const t of [1.4,7.8,12.8,17.5,22.5,27]){await page.evaluate(t=>window.drawFrame(t),t);await page.screenshot({path:resolve(frames,`frame-${t}.png`)});}console.log('design_frames_ready');const result=await page.evaluate(()=>window.recordVideo());const bytes=Buffer.from(result,'base64');await writeFile(resolve(media,'promo-motion-master.mp4'),bytes);console.log(JSON.stringify({file:'promo-motion-master.mp4',bytes:bytes.length,requested_seconds:30,canvas:[1080,1920],http_requests_allowed:false,recorder:'existing_edge_mediarecorder_avc'}));}finally{await browser.close();}
