// Local artifact review only: no app, network, model, or speaker access.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {basename,dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
const media=dirname(fileURLToPath(import.meta.url)),args=process.argv.slice(2),require=createRequire(import.meta.url);
function option(name){const i=args.indexOf(name);if(i<0)return null;if(!args[i+1]||args[i+1].startsWith('--'))throw Error(`${name} value required`);return args[i+1];}
const dependencies=option('--node-modules'),load=name=>dependencies?require(resolve(dependencies,name)):require(name),{chromium}=load('playwright'),sharp=load('sharp');
const inputDir=resolve(option('--input-dir')??media),plan=JSON.parse(await readFile(resolve(inputDir,'product-tour-inputs.manifest.json'),'utf8')),name=plan.file;
if(typeof name!=='string'||basename(name)!==name||name.includes('\\')||!name.endsWith('.mp4'))throw Error('Manifest must name an MP4 inside the input directory.');
const bytes=await readFile(resolve(inputDir,name)),sha=b=>createHash('sha256').update(b).digest('hex');
if(sha(bytes)!==plan.sha256)throw Error('Rendered video changed.');
const qa=resolve(inputDir,'product-tour-review');await mkdir(qa,{recursive:true});
const browser=await chromium.launch({headless:true,channel:'msedge'});
try{
 const page=await browser.newPage({viewport:{width:1080,height:1920},deviceScaleFactor:1});await page.route('**/*',r=>r.abort());
 await page.setContent('<style>html,body{margin:0;background:#071329}video{width:1080px;height:1920px;display:block}</style><video muted preload="auto"></video>');
 await page.locator('video').evaluate((v,b)=>{v.src='data:video/mp4;base64,'+b;},bytes.toString('base64'));
 await page.waitForFunction(()=>document.querySelector('video').readyState>=2,{timeout:30000});
 const meta=await page.locator('video').evaluate(v=>({width:v.videoWidth,height:v.videoHeight,seconds:v.duration}));
 if(meta.width!==1080||meta.height!==1920||meta.seconds<35||meta.seconds>40.7)throw Error('Unexpected output dimensions or duration.');
 const times=[.05,...plan.audio.clips.map(x=>(x.scene_start+x.scene_end)/2),meta.seconds-.2],layers=[];
 for(let i=0;i<times.length;i++){await page.locator('video').evaluate((v,t)=>new Promise((ok,bad)=>{v.onseeked=ok;v.onerror=bad;v.currentTime=t;}),times[i]);const path=resolve(qa,`frame-${i+1}.png`);await page.screenshot({path});layers.push({input:await sharp(path).resize(360,640).toBuffer(),left:i%3*360,top:Math.floor(i/3)*640});}
 await sharp({create:{width:1080,height:1920,channels:3,background:'#071329'}}).composite(layers).png().toFile(resolve(qa,'contact-sheet.png'));
 let audio;const reuse=option('--reuse-reviewed-mix');if(reuse){const prior=JSON.parse(await readFile(resolve(reuse),'utf8'));if(!prior.complete||JSON.stringify(prior.clips)!==JSON.stringify(plan.clips)||JSON.stringify(prior.source_music)!==JSON.stringify(plan.source_music)||JSON.stringify(prior.audio.clips)!==JSON.stringify(plan.audio.clips)||Math.abs(prior.audio.sample_peak_dbfs-plan.audio.sample_peak_dbfs)>1e-6||Math.abs(prior.audio.rms_dbfs-plan.audio.rms_dbfs)>1e-6||prior.audio.samples_at_or_above_full_scale!==0)throw Error('Previously reviewed narration/music mix changed.');audio={...plan.audio,unchanged_mix_inputs_verified:true,latest_encoded_output_pcm_remeasured:false,measurement:'Same clip/music hashes, timing, rendered PCM peak/RMS as the previously reviewed output; latest MP4 PCM not measured again.'};}else{audio=await page.evaluate(async encoded=>{const binary=atob(encoded),raw=new Uint8Array(binary.length);for(let i=0;i<binary.length;i++)raw[i]=binary.charCodeAt(i);const decoded=await new OfflineAudioContext(2,1,48000).decodeAudioData(raw.buffer);let peak=0,sum=0,clipped=0;for(let c=0;c<decoded.numberOfChannels;c++)for(const x of decoded.getChannelData(c)){peak=Math.max(peak,Math.abs(x));sum+=x*x;if(Math.abs(x)>=1)clipped++;}return{seconds:decoded.duration,channels:decoded.numberOfChannels,sample_rate:decoded.sampleRate,sample_peak_dbfs:20*Math.log10(peak),rms_dbfs:10*Math.log10(sum/(decoded.length*decoded.numberOfChannels)),samples_at_or_above_full_scale:clipped,measurement:'Actual MP4 decoded PCM sample peak/RMS; not true peak or LUFS.',speaker_playback:false};},bytes.toString('base64'));}
 if(audio.samples_at_or_above_full_scale)throw Error('Clipped output PCM.');
 const report={scope:'local_artifact_review_only',file:name,bytes:bytes.length,sha256:sha(bytes),...meta,frame_seconds:times,audio,narration_slots:plan.audio.clips,source_music_seconds:[24,29.6],new_tts_generation:0,new_model_calls:0,paid_credits:0,http_requests_allowed:false};
 await writeFile(resolve(inputDir,'product-tour-review.json'),JSON.stringify(report,null,2)+'\n');plan.complete=true;plan.review_file='product-tour-review.json';await writeFile(resolve(inputDir,'product-tour-inputs.manifest.json'),JSON.stringify(plan,null,2)+'\n');console.log(JSON.stringify(report));
}finally{await browser.close();}
