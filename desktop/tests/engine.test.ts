import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {access,mkdtemp,rm} from 'node:fs/promises';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import type {spawn} from 'node:child_process';
import {EngineController,buildLaunch} from '../src/main/engine';
class MockChild extends EventEmitter {
  stdin=new PassThrough();stdout=new PassThrough();stderr=new PassThrough();exitCode:number|null=null;signalCode:NodeJS.Signals|null=null;kills:string[]=[];
  constructor() {super();this.stdin.on('data',chunk=>{if(String(chunk).includes('"shutdown"'))queueMicrotask(()=>this.exit());});}
  exit() {if(this.exitCode!==null)return;this.exitCode=0;this.emit('exit',0,null);}
  kill(signal:NodeJS.Signals='SIGTERM') {this.kills.push(signal);this.signalCode=signal;this.exit();return true;}
}
test('엔진은 새 설정으로만 기동하고 자신의 JSONL shutdown으로 종료한다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-engine-test-'));let calls=0;const child=new MockChild();
  try {
    const spawnMock=((executable:string,args:string[],options:any)=>{
      calls++;assert.equal(executable,'/explicit/python');assert.equal(options.shell,false);assert.equal(options.windowsHide,true);assert.equal(options.env.GUKGAM_DATA_DIR,join(dir,'records'));
      const config=JSON.parse(readFileSync(args.at(-1)!,'utf8'));assert.deepEqual(config,{organization:'모의 기관'});
      queueMicrotask(()=>child.stdout.write('{"type":"ready","url":"http://127.0.0.1:8765"}\n'));return child;
    }) as unknown as typeof spawn;
    const engine=new EngineController({userData:dir,resourcesPath:'/resources',projectRoot:'/new/repo',packaged:false,env:{GUKGAM_DESKTOP_PYTHON:'/explicit/python'},checkPort:async()=>{},spawn:spawnMock});
    assert.equal((await engine.start({organization:'모의 기관'})).state,'ready');assert.equal(calls,1);
    assert.equal((await engine.stop()).state,'stopped');assert.deepEqual(child.kills,[]);
  } finally {await rm(dir,{recursive:true,force:true});}
});
test('8765가 점유되면 기존 서비스에 접속하거나 프로세스를 생성하지 않는다',async()=>{
  let spawnCalls=0;
  const engine=new EngineController({userData:'/not-used',resourcesPath:'/resources',projectRoot:'/new/repo',packaged:true,checkPort:async()=>{throw Error('8765 포트 사용 중');},spawn:(()=>{spawnCalls++;throw Error('호출 금지');}) as typeof spawn});
  await assert.rejects(engine.start({organization:'모의'}),/8765/);assert.equal(spawnCalls,0);assert.equal(engine.status().state,'error');
});

test('main의 중계 저장 판본은 임시 엔진 초기 파일까지 그대로 전달한다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-engine-relay-intent-')),child=new MockChild();
  const revision='e14265df-87cf-4b48-9546-364fa670ff10';
  try {
    const engine=new EngineController({userData:dir,resourcesPath:'/resources',projectRoot:'/new/repo',packaged:true,checkPort:async()=>{},spawn:((_exe:string,args:string[])=>{
      const initial=JSON.parse(readFileSync(args.at(-1)!,'utf8'));
      assert.equal(initial.relayPolicyRevision,revision);assert.equal(initial.relayEnabled,false);
      assert.equal(initial.applyRelayPolicy,undefined);
      queueMicrotask(()=>child.stdout.write('{"type":"ready","url":"http://127.0.0.1:8765"}\n'));return child;
    }) as unknown as typeof spawn});
    assert.equal((await engine.start({organization:'합성기관',relayEnabled:false,relayStartDate:'',relayEndDate:'',relayPolicyRevision:revision})).state,'ready');
    await engine.stop();assert.deepEqual(child.kills,[]);
  }finally {await rm(dir,{recursive:true,force:true});}
});
test('시작 준비 중 취소하면 늦은 포트 확인 뒤 프로세스를 만들지 않는다',async()=>{
  let release!:()=>void,spawnCalls=0;
  const engine=new EngineController({userData:'/not-used',resourcesPath:'/resources',projectRoot:'/new/repo',packaged:true,checkPort:()=>new Promise(resolve=>{release=resolve;}),spawn:(()=>{spawnCalls++;throw Error('호출 금지');}) as typeof spawn});
  const started=engine.start({organization:'모의'});const rejected=assert.rejects(started,/취소/);await engine.stop();release();await rejected;assert.equal(spawnCalls,0);
});
test('한국어 JSONL이 바이트 경계에서 나뉘어도 원문을 유지한다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-jsonl-test-'));const child=new MockChild();const users:string[]=[];
  try {
    const engine=new EngineController({userData:dir,resourcesPath:'/resources',projectRoot:'/new/repo',packaged:true,checkPort:async()=>{},onEvent:event=>{if(event.type==='llm.request')users.push(event.user);},spawn:(()=>{queueMicrotask(()=>child.stdout.write('{"type":"ready","url":"http://127.0.0.1:8765"}\n'));return child;}) as unknown as typeof spawn});
    await engine.start({organization:'모의'});
    const bytes=Buffer.from(JSON.stringify({type:'llm.request',id:'split',system:'',user:'한글 원문',max_tokens:20})+'\n');
    for(let i=0;i<bytes.length;i++)child.stdout.write(bytes.subarray(i,i+1));assert.deepEqual(users,['한글 원문']);await engine.stop();
  } finally {await rm(dir,{recursive:true,force:true});}
});
test('이전 자식의 늦은 종료와 출력은 새 엔진·설정·AI 요청을 건드리지 않는다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-old-child-test-'));const oldChild=new MockChild(),newChild=new MockChild();let calls=0,stopped=0,newConfig='';const events:unknown[]=[];
  oldChild.stdin.removeAllListeners('data');oldChild.kill=signal=>{oldChild.kills.push(signal??'SIGTERM');return true;};
  try {
    const engine=new EngineController({userData:dir,resourcesPath:'/resources',projectRoot:'/new/repo',packaged:true,checkPort:async()=>{},shutdownTimeoutMs:1,forceKillTimeoutMs:1,onStopped:()=>{stopped++;},onEvent:event=>events.push(event),spawn:((_exe:string,args:string[])=>{
      calls++;if(calls===1){queueMicrotask(()=>oldChild.stdout.write('{"type":"ready","url":"http://127.0.0.1:8765"}\n'));return oldChild;}
      newConfig=args.at(-1)!;return newChild;
    }) as unknown as typeof spawn});
    await engine.start({organization:'처음'});await engine.stop();
    const started=engine.start({organization:'새 엔진'});
    while(!newConfig)await new Promise(resolve=>setTimeout(resolve,1));
    oldChild.emit('exit',0,null);oldChild.stdout.write('{"type":"llm.request","id":"old","system":"","user":"이전","max_tokens":2}\n');
    await access(newConfig);assert.equal(stopped,0);assert.equal(engine.status().state,'starting');assert.deepEqual(events,[]);
    newChild.stdout.write('{"type":"ready","url":"http://127.0.0.1:8765"}\n');await started;await engine.stop();assert.equal(stopped,1);
  } finally {await rm(dir,{recursive:true,force:true});}
});

test('국회 네트워크 차단 값만 자식에 전달하고 켜는 값·비밀 설정은 격리한다',()=>{
  const common={userData:'/new/data',resourcesPath:'/resources',projectRoot:'/new/repo',packaged:true};
  const off=buildLaunch({...common,env:{GUKGAM_ALLOW_ASSEMBLY_NETWORK:'0',GUKGAM_TYPESAFE_API_KEY:'private',GUKGAM_AGENT_TOKEN:'private'}},'/config');
  assert.equal(off.env.GUKGAM_ALLOW_ASSEMBLY_NETWORK,'0');
  assert.equal(off.env.GUKGAM_TYPESAFE_API_KEY,undefined);assert.equal(off.env.GUKGAM_AGENT_TOKEN,undefined);
  const on=buildLaunch({...common,env:{GUKGAM_ALLOW_ASSEMBLY_NETWORK:'1'}},'/config');
  assert.equal(on.env.GUKGAM_ALLOW_ASSEMBLY_NETWORK,undefined);
});

test('Jev 키는 설정 파일·환경 없이 소유 파이프로 보내며 예외적 stderr에서도 숨긴다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-jev-pipe-test-')),child=new MockChild();
  const key='SYNTHETIC_JEV_SECRET',sent:string[]=[];
  child.stdin.on('data',chunk=>sent.push(String(chunk)));
  try{
    const engine=new EngineController({userData:dir,resourcesPath:'/resources',projectRoot:'/new/repo',packaged:true,checkPort:async()=>{},spawn:((_exe:string,args:string[],options:any)=>{
      assert.equal(readFileSync(args.at(-1)!,'utf8').includes(key),false);
      assert.equal(JSON.stringify(options.env).includes(key),false);
      queueMicrotask(()=>child.stdout.write('{"type":"ready","url":"http://127.0.0.1:8765"}\n'));return child;
    }) as unknown as typeof spawn});
    await engine.start({organization:'모의 기관'});
    engine.sendSensitive({type:'jev.configure',key,revision:'synthetic-revision'},key);
    child.stderr.write(`${key}\n`);
    assert.equal((engine as any).logs.includes(key),false);assert.equal((engine as any).logs.includes('[숨김]'),true);
    assert.ok(sent.some(value=>value.includes(key)));await engine.stop();
    assert.equal((engine as any).sensitiveValues.size,0);
  }finally{await rm(dir,{recursive:true,force:true});}
});

async function connectionFixture(children=[new MockChild()],timeout=30) {
  const dir=await mkdtemp(join(tmpdir(),'gukgam-owned-relay-'));
  const requests=new Map<MockChild,any[]>();let index=0;
  for(const child of children) {
    requests.set(child,[]);
    child.stdin.on('data',chunk=>{for(const line of String(chunk).trim().split('\n')){
      const value=JSON.parse(line);if(value.type==='relay.connection.request')requests.get(child)!.push(value);
    }});
  }
  const engine=new EngineController({userData:dir,resourcesPath:'/resources',projectRoot:'/new/repo',packaged:true,
    checkPort:async()=>{},connectionTimeoutMs:timeout,shutdownTimeoutMs:1,forceKillTimeoutMs:1,
    spawn:(()=>{const child=children[index++]!;queueMicrotask(()=>child.stdout.write('{"type":"ready","url":"http://127.0.0.1:8765"}\n'));return child;}) as unknown as typeof spawn});
  return {engine,requests,close:async()=>{await engine.stop();await rm(dir,{recursive:true,force:true});}};
}
function reply(child:MockChild,id:string,connection_id:unknown) {
  child.stdout.write(JSON.stringify({type:'relay.connection.result',id,connection_id})+'\n');
}

test('중계 식별자는 ready 캐시 없이 같은 소유 자식에 매번 새 요청으로 읽는다',async()=>{
  const child=new MockChild(),f=await connectionFixture([child]);
  try{
    assert.equal(await f.engine.readRelayConnectionId(),undefined);assert.equal(f.requests.get(child)!.length,0);
    await f.engine.start({organization:'합성기관'});
    for(const value of ['a'.repeat(32),'b'.repeat(32)]) {
      const pending=f.engine.readRelayConnectionId();const request=f.requests.get(child)!.at(-1);
      assert.match(request.id,/^[a-f0-9]{32}$/);reply(child,request.id,value);
      assert.equal(await pending,value);
    }
    assert.equal(new Set(f.requests.get(child)!.map(value=>value.id)).size,2);
    assert.equal(f.engine.status().state,'ready');assert.deepEqual(child.kills,[]);
  }finally{await f.close();}
});

test('잘못된 신원 응답과 다른 요청 ID는 엔진을 멈추거나 신원 조회를 완료하지 않는다',async()=>{
  const child=new MockChild(),f=await connectionFixture([child],1000);
  try{
    await f.engine.start({organization:'합성기관'});
    const pending=f.engine.readRelayConnectionId();let settled=false;void pending.then(()=>{settled=true;});
    const request=f.requests.get(child)!.at(-1);
    reply(child,'f'.repeat(32),'a'.repeat(32));reply(child,request.id,'not-an-id');
    child.stdout.write(JSON.stringify({type:'relay.connection.result',id:request.id,connection_id:'a'.repeat(32),extra:'ignored'})+'\n');
    await new Promise(resolve=>setImmediate(resolve));assert.equal(settled,false);
    assert.equal(f.engine.status().state,'ready');assert.deepEqual(child.kills,[]);
    reply(child,request.id,null);assert.equal(await pending,undefined);
  }finally{await f.close();}
});

test('신원 조회 시간 초과와 늦은 응답은 수집 엔진 상태와 다음 요청을 바꾸지 않는다',async()=>{
  const child=new MockChild(),f=await connectionFixture([child]);
  try{
    await f.engine.start({organization:'합성기관'});
    const expired=f.engine.readRelayConnectionId(),old=f.requests.get(child)!.at(-1);
    assert.equal(await expired,undefined);assert.equal(f.engine.status().state,'ready');assert.deepEqual(child.kills,[]);
    const pending=f.engine.readRelayConnectionId(),current=f.requests.get(child)!.at(-1);
    reply(child,old.id,'a'.repeat(32));reply(child,current.id,'b'.repeat(32));
    assert.equal(await pending,'b'.repeat(32));
  }finally{await f.close();}
});

test('종료·재시작은 대기 신원을 즉시 해제하고 이전 자식의 현재 ID 위조 응답도 거부한다',async()=>{
  const old=new MockChild(),current=new MockChild();
  old.stdin.removeAllListeners('data');old.kill=signal=>{old.kills.push(signal??'SIGTERM');return true;};
  const f=await connectionFixture([old,current],5000);
  try{
    await f.engine.start({organization:'이전 합성기관'});
    const previous=f.engine.readRelayConnectionId();await f.engine.stop();
    assert.deepEqual(await Promise.race([previous.then(value=>({value})),new Promise(resolve=>setImmediate(()=>resolve('unsettled')))]),{value:undefined});
    await f.engine.start({organization:'새 합성기관'});
    const pending=f.engine.readRelayConnectionId(),request=f.requests.get(current)!.at(-1);
    old.emit('exit',0,null);reply(old,request.id,'a'.repeat(32));
    reply(current,request.id,'b'.repeat(32));assert.equal(await pending,'b'.repeat(32));
    assert.equal(f.engine.status().state,'ready');assert.deepEqual(current.kills,[]);
  }finally{await f.close();}
});
