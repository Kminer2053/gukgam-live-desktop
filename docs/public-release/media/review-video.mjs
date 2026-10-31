// 생성한 로컬 MP4만 격리 브라우저에서 디코딩한다. HTTP·운영앱·프로필 접근 없음.
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {dirname,resolve,basename} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
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
const sharp=dependency('sharp');
const media=dirname(fileURLToPath(import.meta.url)),name=cli[0]||'motion-graphics.mp4';
if(basename(name)!==name||!name.endsWith('.mp4'))throw Error('owned_media_filename_required');
const bytes=await readFile(resolve(media,name)),qa=resolve(media,name.replace('.mp4','-review'));
await mkdir(qa,{recursive:true});
const browser=await chromium.launch({headless:true,channel:'msedge'});
try{
 const page=await browser.newPage({viewport:{width:1080,height:1920},deviceScaleFactor:1});
 await page.route('**/*',route=>route.abort());
 await page.setContent('<style>html,body{margin:0;background:#071329}video{width:1080px;height:1920px;display:block}</style><video muted preload="auto"></video>');
 await page.locator('video').evaluate((video,data)=>{video.src='data:video/mp4;base64,'+data;},bytes.toString('base64'));
 await page.waitForFunction(()=>document.querySelector('video').readyState>=2,{timeout:30000});
 const meta=await page.locator('video').evaluate(video=>({width:video.videoWidth,height:video.videoHeight,duration:video.duration}));
 const times=[0.8,4,7.5,12.5,17.5,22.5,28];
 for(const [index,time] of times.entries()){
  await page.locator('video').evaluate((video,time)=>new Promise(resolve=>{video.onseeked=()=>resolve();video.currentTime=time;}),time);
  await page.screenshot({path:resolve(qa,`frame-${index+1}.png`)});
 }
 const layers=[];
 for(let index=0;index<times.length;index++)layers.push({input:await sharp(resolve(qa,`frame-${index+1}.png`)).resize(360,640).toBuffer(),left:(index%3)*360,top:Math.floor(index/3)*640});
 await sharp({create:{width:1080,height:1920,channels:3,background:'#071329'}}).composite(layers).png().toFile(resolve(qa,'contact-sheet.png'));
 const report={file:name,sha256:createHash('sha256').update(bytes).digest('hex'),...meta,frame_seconds:times,http_requests_allowed:false};
 await writeFile(resolve(qa,'metadata.json'),JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify(report));
}finally{await browser.close();}
