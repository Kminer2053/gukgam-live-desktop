import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm,readdir,stat} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {DesktopDiagnostics,diagnosticEntry} from '../src/main/diagnostics';

test('진단 허용 목록은 본문·토큰·이메일·URL·오류 메시지를 저장하지 않는다',()=>{
  const row=diagnosticEntry('chatgpt.request',{
    request_id:'request-1',state:'complete',model:'gpt-test',startedAt:1,updatedAt:9,receivedChars:30,durationMs:8,
    token:'SECRET_TOKEN',email:'person@example.invalid',url:'https://secret.invalid',message:'SECRET_ERROR',
    prompt:'SECRET_PROMPT',response:'SECRET_RESPONSE',input:'SECRET_INPUT',
  } as any,10)!;
  assert.deepEqual(Object.keys(row).sort(),['durationMs','event','model','receivedChars','request_id','startedAt','state','time','updatedAt'].sort());
  assert.equal(JSON.stringify(row).includes('SECRET'),false);
  for(const model of ['person@example.invalid','https://secret.invalid','sk-secret-token','Bearer token','eyJsecret']){
    assert.equal('model' in diagnosticEntry('chatgpt.request',{model})!,false);
  }
  assert.equal(diagnosticEntry('SECRET_EVENT' as any),undefined);
});

test('비동기 기록 순서를 보존하며 완료 이벤트를 flush 뒤 읽을 수 있다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-diagnostics-'));
  try{
    const log=new DesktopDiagnostics(dir);
    for(const state of ['preparing','connecting','complete'] as const)log.request({id:'request-1',state,model:'gpt-test',startedAt:10,updatedAt:state==='complete'?40:10,receivedChars:state==='complete'?5:0});
    assert.equal(await log.flush(),true);
    const rows=(await readFile(log.path,'utf8')).trim().split('\n').map(line=>JSON.parse(line));
    assert.deepEqual(rows.map(row=>row.state),['preparing','connecting','complete']);
    assert.equal(rows[2].durationMs,30);assert.equal(rows[2].receivedChars,5);
    log.request({id:'request-1',state:'receiving',startedAt:10,updatedAt:45,receivedChars:9});
    await log.flush();assert.equal((await readFile(log.path,'utf8')).trim().split('\n').length,3);
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('파일 크기 상한과 백업 한 개를 지키며 최신 완료 기록을 보존한다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-diagnostics-rotation-'));
  try{
    const log=new DesktopDiagnostics(dir,{maxBytes:650});
    for(let index=0;index<18;index++){log.log('chatgpt.request',{request_id:`request-${index}`,state:'complete',model:'gpt-test',durationMs:index});await log.flush();}
    assert.ok((await stat(log.path)).size<=650);assert.ok((await stat(log.path+'.1')).size<=650);
    assert.deepEqual((await readdir(join(dir,'logs'))).sort(),['desktop.jsonl','desktop.jsonl.1']);
    const rows=(await readFile(log.path,'utf8')).trim().split('\n').map(line=>JSON.parse(line));
    assert.equal(rows.at(-1).request_id,'request-17');
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('진단 폴더 쓰기 실패를 호출자에게 전파하지 않는다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-diagnostics-failure-'));
  try{
    await writeFile(join(dir,'logs'),'진단 폴더 충돌');
    const log=new DesktopDiagnostics(dir);assert.doesNotThrow(()=>log.log('app.start',{version:'0.1.0-preview.3'}));
    assert.equal(await log.flush(),true);log.log('app.quit');assert.equal(await log.flush(),true);
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('느린 디스크에서도 대기열과 종료 flush가 유한하며 누락 표지를 남긴다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-diagnostics-queue-'));
  try{
    const log=new DesktopDiagnostics(dir,{maxPending:4});const owned=log as any;
    const originalWrite=owned.write.bind(log);let release!:()=>void;
    const gate=new Promise<void>(done=>{release=done;});
    owned.write=async(line:string)=>{await gate;await originalWrite(line);};
    for(let index=0;index<100;index++)log.log('engine.state',{engineState:'ready'});
    assert.equal(owned.pending,4);assert.equal(await log.flush(5),false);
    release();assert.equal(await log.flush(),true);
    const rows=(await readFile(log.path,'utf8')).trim().split('\n').map(line=>JSON.parse(line));
    assert.equal(rows.length,5);assert.equal(rows.at(-1).event,'diagnostics.dropped');
  }finally{await rm(dir,{recursive:true,force:true});}
});
