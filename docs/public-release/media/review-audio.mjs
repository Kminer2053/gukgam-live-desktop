// 생성한 최종 MP4의 AAC를 Web Audio로 디코딩한다. 재생·네트워크·외부 모델 없음.
import {readFile,writeFile} from 'node:fs/promises';
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
const file='gukgam-live-threads-30s.mp4';
const bytes=await readFile(resolve(media,file));
const browser=await chromium.launch({headless:true,channel:'msedge'});
try{const page=await browser.newPage();await page.route('**/*',route=>route.abort());const report=await page.evaluate(async encoded=>{const binary=atob(encoded),raw=new Uint8Array(binary.length);for(let i=0;i<binary.length;i++)raw[i]=binary.charCodeAt(i);const context=new OfflineAudioContext(2,1,48000);const audio=await context.decodeAudioData(raw.buffer);let peak=0,power=0,clipped=0;const count=audio.length*audio.numberOfChannels;const window=(start,end)=>{let energy=0,n=0,max=0;for(let ch=0;ch<audio.numberOfChannels;ch++){const data=audio.getChannelData(ch);for(let i=Math.round(start*audio.sampleRate);i<Math.min(data.length,Math.round(end*audio.sampleRate));i++){const value=Math.abs(data[i]);energy+=value*value;max=Math.max(max,value);n++;}}return{rms:n?Math.sqrt(energy/n):0,peak:max};};for(let ch=0;ch<audio.numberOfChannels;ch++)for(const sample of audio.getChannelData(ch)){const abs=Math.abs(sample);peak=Math.max(peak,abs);power+=sample*sample;if(abs>=1)clipped++;}return{seconds:audio.duration,sample_rate:audio.sampleRate,channels:audio.numberOfChannels,sample_peak:peak,sample_peak_dbfs:20*Math.log10(peak),rms:Math.sqrt(power/count),rms_dbfs:10*Math.log10(power/count),samples_at_or_above_full_scale:clipped,voice_window:window(1,22.5),music_outro_window:window(24,28),last_150ms:window(Math.max(0,audio.duration-.15),audio.duration),measurement:'decoded_PCM_sample_peak_and_RMS_not_oversampled_true_peak_or_LUFS',playback:false,http_requests_allowed:false};},bytes.toString('base64'));await writeFile(resolve(media,'audio-review.json'),JSON.stringify({file,...report},null,2)+'\n');console.log(JSON.stringify(report));}finally{await browser.close();}
