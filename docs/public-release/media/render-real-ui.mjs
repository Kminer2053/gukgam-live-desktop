import {readFile,writeFile,mkdir,copyFile} from 'node:fs/promises';
import {constants} from 'node:fs';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {dirname,resolve,relative,isAbsolute} from 'node:path';
import {fileURLToPath} from 'node:url';
const media=dirname(fileURLToPath(import.meta.url)),repo=resolve(media,'../../..'),args=process.argv.slice(2);
function option(name,fallback){const i=args.indexOf(name);if(i<0)return fallback;if(!args[i+1]||args[i+1].startsWith('--'))throw new Error(`${name} 값이 필요합니다.`);return args[i+1];}
const dependencyDir=option('--node-modules',null),require=createRequire(import.meta.url);
const playwright=dependencyDir?require(resolve(dependencyDir,'playwright')):require('playwright');
const inputDir=resolve(option('--input-dir',resolve(media,'screenshots')));
const outputDir=resolve(option('--output-dir',media)),configPath=resolve(option('--config',resolve(media,'real-ui-shots.json')));
const framesOnly=args.includes('--frames-only'),config=JSON.parse(await readFile(configPath,'utf8'));
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const images={},captures=[];for(const[id,name]of Object.entries(config.captures)){const path=resolve(inputDir,name),rel=relative(inputDir,path);if(isAbsolute(rel)||rel==='..'||rel.startsWith('../')||rel.startsWith('..\\'))throw new Error('캡처 경로가 입력 폴더 밖입니다.');const bytes=await readFile(path),mime=bytes[0]===0xff&&bytes[1]===0xd8&&bytes[2]===0xff?'image/jpeg':bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))?'image/png':null;if(!mime)throw new Error('지원되지 않는 캡처 형식입니다.');images[id]={data:bytes.toString('base64'),mime};captures.push({id,file:name,sha256:sha(bytes),bytes:bytes.length,mime_type:mime});}
const canonicalPath=resolve(media,'gukgam-live-threads-30s.mp4'),audioPath=resolve(option('--source-audio',canonicalPath));
const originalBytes=await readFile(audioPath),sourceHash=sha(originalBytes);
const backupDir=resolve(repo,'outputs/real-ui-video-original/by-sha256');await mkdir(backupDir,{recursive:true});const backupPath=resolve(backupDir,`${sourceHash}.mp4`);try{await copyFile(audioPath,backupPath,constants.COPYFILE_EXCL);}catch(error){if(error.code!=='EEXIST')throw error;}if(sha(await readFile(backupPath))!==sourceHash)throw new Error('기존 영상 보존 사본의 해시가 다릅니다.');
await mkdir(outputDir,{recursive:true});const reviewDir=resolve(outputDir,'gukgam-live-threads-30s-real-ui-review');await mkdir(reviewDir,{recursive:true});
const browser=await playwright.chromium.launch({headless:true,channel:'msedge',args:['--disable-background-timer-throttling','--autoplay-policy=no-user-gesture-required']});
try{const page=await browser.newPage({viewport:{width:1080,height:1920},deviceScaleFactor:1});await page.route('**/*',route=>route.abort());await page.setContent(await readFile(resolve(media,'promo-real-ui.html'),'utf8'));const prepared=await page.evaluate(payload=>window.prepare(payload),{config,images,brand:(await readFile(resolve(repo,'desktop/assets/brand/gukgam-live-1024.png'))).toString('base64'),audio:framesOnly?null:originalBytes.toString('base64')});
 const times=[2.4,4.9,8.1,11.1,13.0,15.4,18.3,21.5,27.9];for(const time of times){await page.evaluate(time=>window.drawFrame(time),time);await page.screenshot({path:resolve(reviewDir,`frame-${String(time).replace('.','_')}.png`)});}await page.evaluate(()=>window.drawFrame(2.4));await page.screenshot({path:resolve(outputDir,'poster-real-ui.png')});
 const manifest={version:1,status:config.status,scope:'artifact_renderer_only; approved screenshots supplied separately',assets_kind:'real_installed_app_screenshots_with_synthetic_demo_data',width:1080,height:1920,target_seconds:30,paid_credits:0,actual_app_automation:false,model_calls:0,http_requests_allowed:false,speaker_playback:false,disclosure:config.disclosure,source_audio:{file:relative(repo,audioPath).split('\\').join('/'),sha256:sourceHash,bytes:originalBytes.length,reused_existing_final_mix:true},captures,crops:config.crops,sampled_seconds:times,original_canonical_unchanged:true,pending_captures:['summary','corrected'].filter(id=>!images[id])};
 if(!framesOnly){const result=await page.evaluate(()=>window.recordVideo()),video=Buffer.from(result.encoded,'base64');const file='gukgam-live-threads-30s-real-ui.mp4';await writeFile(resolve(outputDir,file),video);manifest.output={file,bytes:video.length,sha256:sha(video),mime:result.mime,audio_seconds:result.audio_seconds};}
 if(sha(await readFile(audioPath))!==sourceHash)throw new Error('기존 영상이 변경됐습니다.');await writeFile(resolve(outputDir,framesOnly?'real-ui-design.manifest.json':'real-ui-inputs.manifest.json'),JSON.stringify(manifest,null,2)+'\n');console.log(JSON.stringify({frames:times.length,frames_only:framesOnly,pending_captures:manifest.pending_captures,output:manifest.output??null,prepared,original_preserved:true}));
}finally{await browser.close();}
