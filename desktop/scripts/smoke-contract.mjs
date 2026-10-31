import assert from 'node:assert/strict';
import {request as httpRequest} from 'node:http';
import {Socket} from 'node:net';
import {access,realpath,writeFile} from 'node:fs/promises';
import {isAbsolute,join,relative,resolve,sep} from 'node:path';
import {gunzipSync} from 'node:zlib';

export const ORIGIN='http://127.0.0.1:8765';
export const fixtureLines=[
  ['10:00:00','시험위원','시험기관은 자료 열두 건을 제출하십시오.'],
  ['10:00:01','답변자','예 자료를 확인하겠습니다.'],
  ['10:00:02','시험위원','원문 보존과 사람 수정을 확인합니다.'],
];
export function assertWindowsCi(platform=process.platform,env=process.env){
  assert.equal(platform,'win32','실제 설치 시험은 Windows CI에서만 실행합니다.');
  assert.equal(env.GITHUB_ACTIONS,'true','일회용 GitHub Actions VM이 필요합니다.');
  assert.equal(env.RUNNER_OS,'Windows','Windows runner가 필요합니다.');
  assert.ok(env.RUNNER_TEMP&&isAbsolute(env.RUNNER_TEMP),'CI 임시 폴더가 필요합니다.');
}
export function assertInside(parent,child){
  assert.ok(isAbsolute(parent)&&isAbsolute(child),'절대 경로만 사용합니다.');
  const part=relative(resolve(parent),resolve(child));
  assert.ok(part&&part!=='..'&&!part.startsWith('..'+sep)&&!isAbsolute(part),'소유한 하위 임시 경로만 사용합니다.');
}
export function argumentsMap(args=process.argv.slice(2)){
  assert.equal(args.length%2,0,'이름과 값으로 인자를 전달하세요.');
  const values={};
  for(let index=0;index<args.length;index+=2){
    assert.match(args[index],/^--[a-z-]+$/);
    assert.ok(!(args[index].slice(2) in values),'중복 인자는 허용하지 않습니다.');
    values[args[index].slice(2)]=args[index+1];
  }
  return values;
}
export function isolatedEnvironment(){
  const env={};
  for(const [key,value] of Object.entries(process.env)){
    if(/^(SystemRoot|WINDIR|PATH|TEMP|TMP|APPDATA|LOCALAPPDATA|COMSPEC|PATHEXT|NUMBER_OF_PROCESSORS|PROCESSOR_ARCHITECTURE|USERPROFILE|HOMEDRIVE|HOMEPATH|CI|GITHUB_ACTIONS|RUNNER_OS|RUNNER_TEMP)$/i.test(key)&&value!==undefined)env[key]=value;
  }
  return {...env,PYTHONUTF8:'1',GUKGAM_ALLOW_ASSEMBLY_NETWORK:'0',GUKGAM_DISABLE_HEAVY:'1',
    GUKGAM_TEST_NETWORK:'0',GUKGAM_TEST_MLX:'0',GUKGAM_TEST_VAD:'0'};
}
export async function installedPaths(installRoot,appExecutable){
  const root=await realpath(installRoot),executable=await realpath(appExecutable);
  assertInside(root,executable);
  assert.equal(relative(root,executable).includes(sep),false,'설치 루트의 앱 EXE만 실행합니다.');
  const engine=join(root,'resources','engine','gukgam-engine.exe');
  await access(join(root,'resources','app.asar'));await access(engine);
  return {root,executable,engine};
}
export const pause=milliseconds=>new Promise(done=>setTimeout(done,milliseconds));
export async function within(promise,milliseconds,message){
  let timer;
  try{return await Promise.race([promise,new Promise((_,fail)=>{timer=setTimeout(()=>fail(Error(message)),milliseconds);})]);}
  finally{clearTimeout(timer);}
}
export async function until(check,timeout=15_000){
  const deadline=Date.now()+timeout;
  do{const value=await check();if(value)return value;await pause(100);}while(Date.now()<deadline);
  throw Error('시험 상태를 제한 시간 안에 확인하지 못했습니다.');
}
export function localRequest(path,{method='GET',body,expected=200,maxBytes=16*1024*1024}={}){
  assert.ok(path.startsWith('/')&&!path.startsWith('//'),'고정 루프백 경로만 호출합니다.');
  return new Promise((done,fail)=>{
    const payload=body===undefined?undefined:Buffer.from(JSON.stringify(body));
    const req=httpRequest(new URL(path,ORIGIN),{method,headers:{'X-Requested-With':'gukgam',...(payload?{'Content-Type':'application/json','Content-Length':String(payload.length)}:{})}},response=>{
      const chunks=[];let size=0;
      response.on('data',chunk=>{size+=chunk.length;if(size>maxBytes)response.destroy(Error('응답 크기 한도를 넘었습니다.'));else chunks.push(chunk);});
      response.on('error',fail);
      response.on('end',()=>{
        if(response.statusCode!==expected){fail(Error(`${method} ${path}: HTTP ${response.statusCode}, 기대값 ${expected}`));return;}
        const buffer=Buffer.concat(chunks),text=buffer.toString('utf8');
        let data;try{data=JSON.parse(text);}catch{}
        done({data,text,buffer,status:response.statusCode});
      });
    });
    req.setTimeout(10_000,()=>req.destroy(Error('루프백 응답 시간이 지났습니다.')));
    const deadline=setTimeout(()=>req.destroy(Error('루프백 전체 응답 시간이 지났습니다.')),15_000);
    req.once('close',()=>clearTimeout(deadline));
    req.on('error',fail);if(payload)req.write(payload);req.end();
  });
}
export function assertPortClosed(){
  return new Promise((done,fail)=>{
    const socket=new Socket();let settled=false;
    const finish=error=>{if(settled)return;settled=true;socket.destroy();error?fail(error):done();};
    socket.setTimeout(1000,()=>finish(Error('종료 뒤 포트 상태를 확인하지 못했습니다.')));
    socket.once('connect',()=>finish(Error('이 시험의 엔진이 8765에 남아 있습니다.')));
    socket.once('error',error=>finish(error.code==='ECONNREFUSED'?undefined:error));
    socket.connect(8765,'127.0.0.1');
  });
}
export function archivePayload(html){
  const found=html.match(/<script id="offline-data"[^>]*>([^<]+)<\/script>/);
  assert.ok(found,'설치된 내보내기의 실제 자료가 필요합니다.');
  return JSON.parse(gunzipSync(Buffer.from(found[1].trim(),'base64'),{maxOutputLength:4*1024*1024}).toString('utf8'));
}
export function assertUnavailableJulia(status){
  assert.equal(status.selected_provider,'julia','설치판에 저장한 Julia 선택을 읽어야 합니다.');
  assert.equal(status.model?.model_present,false,'CI에는 실제 Julia 모델을 준비하지 않습니다.');
  assert.equal(status.model?.available,false,'모델이 없는 상태를 준비 완료로 표시하면 안 됩니다.');
  assert.equal(status.model?.runtime_present,true,'CPU 도구는 별도 설치 없이 설치판에 동봉되어야 합니다.');
  assert.equal(status.worker?.available,false,'모델이 없는 작업자를 실행 가능으로 표시하면 안 됩니다.');
  assert.equal(status.preparing,false,'설정 선택만으로 모델 준비를 시작하면 안 됩니다.');
  assert.equal(status.cpu_threads,1);
  assert.match(status.probability_notice,/보정된 정확도/);
}
export function assertJuliaPrepareIntent(request){
  const url=new URL(request.url);
  assert.equal(url.origin,ORIGIN);assert.equal(url.pathname,'/api/settings/role-decision/julia/prepare');
  assert.equal(url.search,'');assert.equal(request.method,'POST');
  assert.deepEqual(request.body,{confirm:true},'명시적 준비 버튼만 정확한 확인 값을 보냅니다.');
}
export function parseMissingJuliaWorker(stdout){
  assert.ok(Buffer.byteLength(stdout)<=96_000,'설치된 작업자 오류 응답은 유한해야 합니다.');
  const lines=stdout.trim().split(/\r?\n/);assert.equal(lines.length,1,'작업자는 오류 JSON 한 줄만 출력합니다.');
  const event=JSON.parse(lines[0]);
  assert.equal(event.event,'fatal');assert.equal(event.status,'invalid_model');
  assert.match(event.error,/Julia CPU 모델을 준비하지 못했습니다/);
  assert.equal('answers' in event,false,'모델이 없으면 실제 판정 결과를 만들지 않습니다.');
  return event;
}
export async function exerciseRecords(recordsDir,outputDir){
  const health=(await localRequest('/api/health')).data;
  assert.equal(health.ok,true);assert.equal(health.stt.loaded,false);assert.equal(health.llm.loaded,false);
  assert.equal(health.llm.provider,'desktop_chatgpt');
  const assembly=(await localRequest('/api/assembly/live')).data;
  assert.match(assembly.error??'',/GUKGAM_ALLOW_ASSEMBLY_NETWORK=0/);
  // 설치된 엔진의 공개 API에서 임시 시험만 규칙 모드로 바꾼다. AI 요청은 보내지 않는다.
  for(const [key,value] of Object.entries({llm:{provider:'rules',auto_block_summary:false,rolling_interval_sec:0},transcript_review:{enabled:false,use_jev:false},assembly:{auto_detect:false},audio_review:{enabled:false}})){
    await localRequest(`/api/settings/${key}`,{method:'PUT',body:value});
  }
  const fixture=join(recordsDir,'windows-smoke-fixture.txt');
  await writeFile(fixture,fixtureLines.map(line=>line.join('|')).join('\n')+'\n','utf8');
  const created=(await localRequest('/api/sessions',{method:'POST',body:{title:'Windows 설치 검증 · 가상 회의',source_type:'replay',
    source_config:{fixture,speed:1000,with_hints:true,turn_marks:true},roster_text:'시험위원 위원 무소속\n답변자 기관 시험기관',start:true}})).data;
  assert.ok(created.id&&!created.start_error,'설치된 엔진의 파일 수집이 시작되어야 합니다.');
  const sid=created.id;
  const snapshot=await until(async()=>{
    const data=(await localRequest(`/api/sessions/${sid}/snapshot`)).data;
    return fixtureLines.every(line=>data.utterances?.some(row=>row.is_final&&row.text===line[2]))?data:undefined;
  });
  const first=snapshot.utterances.find(row=>row.text===fixtureLines[0][2]);
  const edited=fixtureLines[0][2]+' 검수 확인.';
  await localRequest(`/api/utterances/${first.id}`,{method:'PATCH',body:{text:edited,speaker:'검수위원',speaker_role:'member'}});
  await localRequest(`/api/sessions/${sid}`,{method:'PATCH',body:{notes:'설치된 실행물의 임시 CRUD 검증'}});
  await localRequest(`/api/sessions/${sid}/stop`,{method:'POST'});
  await until(async()=>{
    const data=(await localRequest(`/api/sessions/${sid}/snapshot`)).data;
    return data.utterances?.some(row=>row.id===first.id&&row.text===edited&&row.speaker_source==='manual')?data:undefined;
  });
  const download=await localRequest(`/api/sessions/${sid}/offline.html`);
  const payload=archivePayload(download.text);
  const original=payload.originals.find(row=>row.id===first.id);
  assert.equal(original.original_text,fixtureLines[0][2]);assert.equal(original.text,edited);assert.equal(original.speaker_source,'manual');
  await writeFile(join(outputDir,'synthetic-meeting-offline.html'),download.buffer);
  const report=(await localRequest(`/api/sessions/${sid}/report`)).data;
  assert.ok(report&&typeof report==='object','실제 보고서 라우트가 동작해야 합니다.');
  return {sid,edited,checks:{health:true,assembly_network_disabled:true,original_fixture_lines:fixtureLines.length,
    saved_fixture_rows:snapshot.utterances.filter(row=>fixtureLines.some(line=>line[2]===row.text)).length,
    human_edit_saved:true,original_text_preserved:true,offline_export:true,report_route:true},
    async cleanup(){await localRequest(`/api/sessions/${sid}`,{method:'DELETE'});await localRequest(`/api/sessions/${sid}`,{expected:404});}};
}
