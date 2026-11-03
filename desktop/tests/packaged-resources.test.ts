import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {dirname,isAbsolute,join,relative,resolve,sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import {tmpdir} from 'node:os';

type Matcher={from:string;to:string;createFilter():((path:string,stat:{isDirectory():boolean})=>boolean)};
const require=createRequire(import.meta.url);
const {getFileMatchers}=require('app-builder-lib/out/fileMatcher.js') as {getFileMatchers(...args:unknown[]):Matcher[]};
const desktopRoot=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const config=JSON.parse(readFileSync(join(desktopRoot,'package.json'),'utf8')).build;
const destination=join(tmpdir(),'gukgam-resource-filter-test','resources');
const matchers=getFileMatchers(config,'extraResources',destination,{macroExpander:(value:string)=>value,
  customBuildOptions:{},globalOutDir:join(desktopRoot,'release'),defaultSrc:desktopRoot});

/** 실제 복사 필터로 각 부모 폴더를 통과한 파일만 설치 대상 경로에 도달한다. */
function copied(sourceRelative:string,targetRelative=sourceRelative):boolean {
  const source=join(desktopRoot,sourceRelative),target=join(destination,targetRelative);
  return matchers.some(matcher=>{
    const part=relative(matcher.from,source);
    if(part==='..'||part.startsWith('..'+sep)||isAbsolute(part)||resolve(matcher.to,part)!==target)return false;
    const filter=matcher.createFilter(),parts=part?part.split(sep):[];
    let current=matcher.from;
    if(!filter(current,{isDirectory:()=>parts.length>0}))return false;
    return parts.every((name,index)=>{current=join(current,name);return filter(current,{isDirectory:()=>index<parts.length-1});});
  });
}

test('기본 설치 필터는 중계 CLI·의존성·개발 캐시를 복사하지 않는다',()=>{
  for(const path of ['relay-cli/package.json','relay-cli/node_modules/vercel/package.json',
    'relay-cli/node_modules/vercel/dist/vc.js','relay-cli/node_modules/vercel/dist/version.mjs',
    'relay-cli/node_modules/@vercel/cli-config/package.json','relay-cli/node_modules/@vercel/cli-config/dist/paths.js']){
    assert.equal(copied(path),false,`기본 설치물에 복사하지 않아야 함: ${path}`);
  }
  assert.equal(copied('relay-cli/node_modules/.cache/private-value'),false);
});

test('기록 엔진·음성 모델·CPU 실행기는 기본 설치물 대신 선택 준비로 분리한다',()=>{
  for(const name of ['models/nemotron3-diarization/Nemotron-3-Diarization.q8_0.gguf',
    'runtimes/nemo-speech-0.2.0-windows-x86_64-cpu.zip',
    'runtimes/nemo-speech-0.2.0-windows-x86_64-cpu/bin/nemo_speech_asr_c.dll',
    'runtimes/nemo-speech-0.2.0-windows-x86_64-cpu/share/licenses/LICENSE']){
    assert.equal(copied(`../dist/nemotron-assets/${name}`,`nemotron-assets/${name}`),false,name);
  }
  for(const name of ['records/gukgam.sqlite3','models/other.gguf','runtimes/other/private.dll']){
    assert.equal(copied(`../dist/nemotron-assets/${name}`,`nemotron-assets/${name}`),false,name);
  }
  assert.equal(copied('../dist/engine/gukgam-engine/gukgam-engine.exe','engine/gukgam-engine.exe'),false);
  assert.equal(config.win.target[0].target,'nsis-web');
  assert.match(config.nsisWeb.appPackageUrl,/^https:\/\/github\.com\/Kminer2053\/gukgam-live-desktop\/releases\/download\/v0\.2\.0\/gukgam-live-desktop-0\.2\.0-x64\.nsis\.7z$/);
});

test('설치·활용 매뉴얼과 독립 앱 아이콘이 사용자용 설치 경로에 포함된다',()=>{
  assert.equal(copied('../docs/public-release/manual.html','manual/사용설명서.html'),true);
  assert.equal(copied('assets/brand/gukgam-live-1024.png','brand/gukgam-live-1024.png'),true);
  assert.equal(copied('assets/brand/gukgam-live.svg','brand/gukgam-live.svg'),true);
  assert.equal(config.win.icon,'assets/brand/gukgam-live.ico');
});
