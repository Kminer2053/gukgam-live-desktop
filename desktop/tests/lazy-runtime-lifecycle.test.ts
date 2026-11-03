import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import type {spawn} from 'node:child_process';
import {mkdtemp,readFile,readdir,rm,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {EngineController} from '../src/main/engine';
import {VercelCliRunner,type VercelAuthVault} from '../src/relay/vercel-cli';
import {initializeRelayDatabase} from '../src/relay';

function deferred<T>() {let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};}
class Child extends EventEmitter {
  stdin=new PassThrough();stdout=new PassThrough();stderr=new PassThrough();exitCode:number|null=null;signalCode:NodeJS.Signals|null=null;
  constructor(){super();this.stdin.on('data',value=>{if(String(value).includes('"shutdown"')){this.exitCode=0;queueMicrotask(()=>this.emit('exit',0));}});}
  kill(){this.exitCode=0;queueMicrotask(()=>{this.emit('exit',0);this.emit('close',0);});return true;}
}
function vault(counter:{reads:number}):VercelAuthVault {return {get:async()=>{counter.reads++;return undefined;},set:async()=>{},delete:async()=>{}};}
async function fixture(){const root=await mkdtemp(join(tmpdir(),'gukgam-lazy-lifecycle-'));return {root,cleanup:()=>rm(root,{recursive:true,force:true})};}

test('엔진 포트 점검 실패는 준비 다운로드와 비밀 설정·프로세스 생성을 시작하지 않는다',async()=>{
  const h=await fixture();let prepared=0,spawned=0;
  try {
    const engine=new EngineController({userData:h.root,resourcesPath:h.root,projectRoot:h.root,packaged:true,env:{},checkPort:async()=>{throw Error('포트 점검 실패');},resolveEngine:async()=>{prepared++;return h.root;},spawn:(()=>{spawned++;throw Error('금지');}) as typeof spawn});
    engine.status();assert.equal(prepared,0);
    await assert.rejects(engine.start({organization:'합성 기관',relayToken:'fixture-secret'}),/포트/);
    assert.equal(prepared,0);assert.equal(spawned,0);assert.deepEqual(await readdir(h.root),[]);
  }finally{await h.cleanup();}
});

test('준비 중 엔진 종료는 신호를 취소하며 늦게 돌아온 준비 결과로 설정이나 자식을 만들지 않는다',async()=>{
  const h=await fixture(),entered=deferred<void>(),release=deferred<string>();let signal:AbortSignal|undefined,spawned=0;
  try {
    const engine=new EngineController({userData:h.root,resourcesPath:h.root,projectRoot:h.root,packaged:true,env:{},checkPort:async()=>{},resolveEngine:async value=>{signal=value;entered.resolve();return release.promise;},spawn:(()=>{spawned++;throw Error('금지');}) as typeof spawn});
    const start=engine.start({organization:'합성 기관',relayToken:'fixture-secret'}),rejected=assert.rejects(start,/취소/);
    await entered.promise;assert.deepEqual(await readdir(h.root),[]);assert.equal((await engine.stop()).state,'stopped');assert.equal(signal?.aborted,true);
    release.resolve(h.root);await rejected;
    assert.equal(engine.status().state,'stopped');assert.equal(spawned,0);assert.deepEqual(await readdir(h.root),[]);
  }finally{await h.cleanup();}
});

test('준비 실패 후 다시 누르면 검증된 엔진 root에서 한 번만 실행한다',async()=>{
  const h=await fixture(),child=new Child(),events:string[]=[];let attempts=0,spawned=0;
  try {
    const engine=new EngineController({userData:h.root,resourcesPath:join(h.root,'unused-resources'),projectRoot:h.root,packaged:true,env:{},checkPort:async()=>{events.push('port');},resolveEngine:async()=>{events.push('prepare');if(++attempts===1)throw Error('준비 실패');assert.deepEqual(await readdir(h.root),[]);return join(h.root,'verified-pack');},spawn:((exe:string,_args:string[],options:any)=>{
      events.push('spawn');spawned++;assert.equal(exe,join(h.root,'verified-pack','gukgam-engine.exe'));assert.equal(options.cwd,join(h.root,'verified-pack'));assert.equal(options.shell,false);
      queueMicrotask(()=>child.stdout.write('{"type":"ready","url":"http://127.0.0.1:8765"}\n'));return child;
    }) as unknown as typeof spawn});
    await assert.rejects(engine.start({organization:'합성 기관'}),/준비 실패/);assert.deepEqual(await readdir(h.root),[]);
    assert.equal((await engine.start({organization:'합성 기관'})).state,'ready');assert.equal(spawned,1);assert.deepEqual(events,['port','prepare','port','prepare','spawn']);await engine.stop();
  }finally{await h.cleanup();}
});

test('설정 쓰기 중 종료가 먼저 끝나도 늦게 생성된 이번 비밀 설정은 남기지 않는다',async()=>{
  const h=await fixture(),entered=deferred<void>(),release=deferred<void>();let path='',spawned=0,writes=0;
  const sentinel=join(h.root,'preserved.json');await writeFile(sentinel,'원래 자료');
  try {
    const engine=new EngineController({userData:h.root,resourcesPath:h.root,projectRoot:h.root,packaged:true,env:{},checkPort:async()=>{},resolveEngine:async()=>h.root,
      writeConfig:async(file,data,options)=>{path=String(file);writes++;entered.resolve();await release.promise;await writeFile(file,data,options);},
      spawn:(()=>{spawned++;throw Error('금지');}) as typeof spawn});
    const start=engine.start({organization:'합성 기관',relayToken:'SYNTHETIC_LATE_CONFIG_SECRET'}),rejected=assert.rejects(start,/취소/);
    await entered.promise;assert.ok(path.startsWith(join(h.root,'private','engine-')));assert.equal((await engine.stop()).state,'stopped');
    assert.deepEqual(await readdir(join(h.root,'private')),[]);
    release.resolve();await rejected;
    assert.equal(writes,1);assert.equal(spawned,0);assert.equal(engine.status().state,'stopped');
    assert.deepEqual(await readdir(join(h.root,'private')),[]);assert.equal(await readFile(sentinel,'utf8'),'원래 자료');
  }finally{release.resolve();await h.cleanup();}
});

test('CLI 생성과 대기는 도구를 준비하지 않고 실제 요청에서 준비 후에만 vault와 임시 파일을 사용한다',async()=>{
  const h=await fixture(),entered=deferred<void>(),release=deferred<string>(),counter={reads:0};let prepared=0,spawned=0;
  try {
    const runner=new VercelCliRunner({cliPath:join(h.root,'unused.js'),tempRoot:join(h.root,'private'),env:{},vault:vault(counter),resolveCliPath:async()=>{prepared++;entered.resolve();return release.promise;},spawn:((_exe:string,args:string[],options:any)=>{
      spawned++;assert.equal(args[0],join(h.root,'verified','vc.js'));assert.equal(options.shell,false);const child=new Child();queueMicrotask(()=>child.emit('close',0));return child;
    }) as unknown as typeof spawn});
    assert.equal(prepared,0);assert.equal(counter.reads,0);
    const result=runner.run(['whoami'],{cwd:h.root});await entered.promise;
    assert.equal(counter.reads,0);assert.equal(spawned,0);assert.deepEqual(await readdir(h.root),[]);
    release.resolve(join(h.root,'verified','vc.js'));assert.equal((await result).code,0);assert.equal(spawned,1);assert.ok(counter.reads>0);assert.deepEqual(await readdir(join(h.root,'private')),[]);
  }finally{await h.cleanup();}
});

test('CLI 도구 준비 중 취소된 요청은 늦게 완료돼도 vault·파일·자식을 만들지 않는다',async()=>{
  const h=await fixture(),entered=deferred<void>(),release=deferred<string>(),counter={reads:0},controller=new AbortController();let seen:AbortSignal|undefined,spawned=0;
  try {
    const runner=new VercelCliRunner({cliPath:join(h.root,'unused.js'),tempRoot:join(h.root,'private'),env:{},vault:vault(counter),resolveCliPath:async signal=>{seen=signal;entered.resolve();return release.promise;},spawn:(()=>{spawned++;throw Error('금지');}) as typeof spawn});
    const result=runner.run(['whoami'],{cwd:h.root,signal:controller.signal}),rejected=assert.rejects(result,/취소/);
    await entered.promise;controller.abort();assert.equal(seen,controller.signal);release.resolve(join(h.root,'verified.js'));await rejected;
    assert.equal(counter.reads,0);assert.equal(spawned,0);assert.deepEqual(await readdir(h.root),[]);
  }finally{await h.cleanup();}
});

test('CLI 도구 준비 실패는 고정 오류로 반환하고 다음 명시 요청은 다시 준비할 수 있다',async()=>{
  const h=await fixture(),counter={reads:0};let attempts=0,spawned=0;
  try {
    const runner=new VercelCliRunner({cliPath:join(h.root,'unused.js'),tempRoot:join(h.root,'private'),env:{},vault:vault(counter),resolveCliPath:async()=>{if(++attempts===1)throw Error('fixture-secret-path');return join(h.root,'verified.js');},spawn:(()=>{spawned++;const child=new Child();queueMicrotask(()=>child.emit('close',0));return child;}) as unknown as typeof spawn});
    await assert.rejects(runner.run(['whoami'],{cwd:h.root}),error=>error instanceof Error&&/준비 도구/.test(error.message)&&!error.message.includes('fixture-secret-path'));
    assert.equal(counter.reads,0);assert.deepEqual(await readdir(h.root),[]);assert.equal((await runner.run(['whoami'],{cwd:h.root})).code,0);assert.equal(attempts,2);assert.equal(spawned,1);
  }finally{await h.cleanup();}
});

test('릴레이 DB 준비도 같은 엔진 resolver를 사용하고 취소된 늦은 결과는 실행하지 않는다',async()=>{
  const h=await fixture(),entered=deferred<void>(),release=deferred<string>(),controller=new AbortController();let seen:AbortSignal|undefined;
  try {
    const result=initializeRelayDatabase({userData:h.root,resourcesPath:join(h.root,'not-present'),projectRoot:h.root,packaged:true,resolveEngine:async signal=>{seen=signal;entered.resolve();return release.promise;}},'postgresql://synthetic.invalid/fixture',controller.signal),rejected=assert.rejects(result,/취소/);
    await entered.promise;controller.abort();assert.equal(seen,controller.signal);release.resolve(join(h.root,'not-present'));await rejected;assert.deepEqual(await readdir(h.root),[]);
  }finally{await h.cleanup();}
});

test('최초 도구 준비는 REST 타이머를 소비하지 않고 준비 뒤 whoami와 API에 같은 제한을 적용한다',async t=>{
  const h=await fixture(),entered=deferred<void>(),release=deferred<string>(),counter={reads:0};let preparationSignal:AbortSignal|undefined,fetches=0;
  t.mock.timers.enable({apis:['setTimeout']});
  const auth={get:async<T>()=>{counter.reads++;return {version:1,authJson:JSON.stringify({token:'SYNTHETIC_TOKEN'})} as T;},set:async()=>{},delete:async()=>{}};
  try {
    const runner=new VercelCliRunner({cliPath:join(h.root,'unused.js'),tempRoot:join(h.root,'private'),env:{},vault:auth,apiTimeoutMs:100,
      resolveCliPath:async signal=>{preparationSignal=signal;entered.resolve();return release.promise;},
      spawn:(()=>{const child=new Child();queueMicrotask(()=>child.emit('close',0));return child;}) as unknown as typeof spawn,
      fetch:async()=>{fetches++;return new Response('{"teams":[]}',{status:200});}});
    const result=runner.requestApi('/v2/teams',{cwd:h.root});await entered.promise;t.mock.timers.tick(1000);
    assert.equal(preparationSignal?.aborted,false);assert.equal(fetches,0);assert.equal(counter.reads,0);assert.deepEqual(await readdir(h.root),[]);
    release.resolve(join(h.root,'verified.js'));assert.equal((await result).status,200);assert.equal(fetches,1);
  }finally{t.mock.timers.reset();await h.cleanup();}
});
