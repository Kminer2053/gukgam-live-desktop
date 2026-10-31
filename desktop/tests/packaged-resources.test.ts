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

test('실제 electron-builder 필터가 CLI의 부모 폴더·진입점·의존성을 모두 동봉한다',()=>{
  for(const path of ['relay-cli/package.json','relay-cli/node_modules/vercel/package.json',
    'relay-cli/node_modules/vercel/dist/vc.js','relay-cli/node_modules/vercel/dist/version.mjs',
    'relay-cli/node_modules/@vercel/cli-config/package.json','relay-cli/node_modules/@vercel/cli-config/dist/paths.js']){
    assert.equal(copied(path),true,`설치물에 복사할 수 있어야 함: ${path}`);
  }
  assert.equal(copied('relay-cli/node_modules/.cache/private-value'),false);
});

test('고정 CPU 화자 분리 모델·실행 도구만 실제 설치 경로에 복사한다',()=>{
  for(const name of ['models/nemotron3-diarization/Nemotron-3-Diarization.q8_0.gguf',
    'runtimes/nemo-speech-0.2.0-windows-x86_64-cpu.zip',
    'runtimes/nemo-speech-0.2.0-windows-x86_64-cpu/bin/nemo_speech_asr_c.dll',
    'runtimes/nemo-speech-0.2.0-windows-x86_64-cpu/share/licenses/LICENSE']){
    assert.equal(copied(`../dist/nemotron-assets/${name}`,`nemotron-assets/${name}`),true,name);
  }
  for(const name of ['records/gukgam.sqlite3','models/other.gguf','runtimes/other/private.dll']){
    assert.equal(copied(`../dist/nemotron-assets/${name}`,`nemotron-assets/${name}`),false,name);
  }
});

test('설치·활용 매뉴얼과 독립 앱 아이콘이 사용자용 설치 경로에 포함된다',()=>{
  assert.equal(copied('../docs/public-release/manual.html','manual/사용설명서.html'),true);
  assert.equal(copied('assets/brand/gukgam-live-1024.png','brand/gukgam-live-1024.png'),true);
  assert.equal(copied('assets/brand/gukgam-live.svg','brand/gukgam-live.svg'),true);
  assert.equal(config.win.icon,'assets/brand/gukgam-live.ico');
});
