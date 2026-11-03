import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import ts from 'typescript';
import {controls} from '../src/renderer/state';
import type {AppStatus,EngineState} from '../src/shared/contracts';

class Element {
  textContent='';hidden=false;disabled=false;value='';className='';dataset:Record<string,string>={};
  children:Element[]=[];
  classList={toggle:()=>{}};
  handlers=new Map<string,Array<(event:unknown)=>void>>();
  addEventListener(name:string,action:(event:unknown)=>void){this.handlers.set(name,[...(this.handlers.get(name)??[]),action]);}
  emit(name:string){for(const action of this.handlers.get(name)??[])action({preventDefault(){}});}
  click(){if(!this.disabled)this.emit('click');}
  append(...children:Element[]){this.children.push(...children);}
  replaceChildren(){this.children=[];}
  focus(){}
  setAttribute(){}
  removeAttribute(){}
}

const engine=(state:EngineState['state']):EngineState=>({state,message:({stopped:'기록 엔진을 종료했습니다.',
  starting:'이 앱의 기록 엔진을 준비하고 있습니다.',ready:'기록 엔진이 준비됐습니다.',
  stopping:'기록 엔진을 종료하고 있습니다.',error:'기록 엔진을 확인하지 못했습니다.'})[state]});
const status=(value:EngineState):AppStatus=>({version:'모의 버전',setup:{organization:'합성기관',hasRelayToken:false},
  chatgpt:{connected:false,verified:false,message:'모의 미연결',models:[]},engine:value});
async function flush(){for(let index=0;index<20;index++)await Promise.resolve();}
function deferred<T>(){let resolve!:(value:T)=>void,reject!:(error:Error)=>void;const promise=new Promise<T>((done,fail)=>{resolve=done;reject=fail;});return {promise,resolve,reject};}

async function fixture(initial:EngineState['state']='stopped',initialRead?:Promise<AppStatus>) {
  const source=await readFile(new URL('../src/renderer/index.ts',import.meta.url),'utf8');
  const html=await readFile(new URL('../src/renderer/index.html',import.meta.url),'utf8');
  const nodes=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],new Element()]));
  const get=(id:string)=>{const node=nodes.get(id);assert.ok(node,`실제 화면에 없는 요소: ${id}`);return node;};
  const documentEvents=new Element(),windowEvents=new Element();
  const timers=new Map<number,()=>void>();let timerId=0,reads=0,starts=0,stops=0,opens=0;
  let current=engine(initial),organization='합성기관',rejectReads=false,nextRead:Promise<AppStatus>|undefined=initialRead,start:Promise<EngineState>|undefined;
  let readFactory:(()=>Promise<AppStatus>)|undefined;
  const document={visibilityState:'visible',getElementById:get,querySelectorAll:()=>[],createElement:()=>new Element(),
    addEventListener:documentEvents.addEventListener.bind(documentEvents)};
  const api={
    status:async()=>{reads++;if(nextRead){const saved=nextRead;nextRead=undefined;return saved;}if(readFactory)return readFactory();if(rejectReads)throw Error('모의 상태 조회 실패');const value=status({...current});return {...value,setup:{...value.setup,organization}};},
    saveSetup:async(value:{organization:string})=>{organization=value.organization;return {organization,hasRelayToken:false};},
    startEngine:async()=>{starts++;if(start)return start;current=engine('ready');return {...current};},
    stopEngine:async()=>{stops++;current=engine('stopped');return {...current};},
    openWorkspace:async()=>{opens++;},
  };
  const context=vm.createContext({console,window:{gukgam:api,addEventListener:windowEvents.addEventListener.bind(windowEvents)},document,
    setInterval:(callback:()=>void)=>{const id=++timerId;timers.set(id,callback);return id;},clearInterval:(id:number)=>timers.delete(id),
    exports:{},require:(name:string)=>{
      if(name==='./state')return {controls};
      if(name==='./relay-wizard')return {initializeRelayWizard:()=>({updateAvailability(){}})};
      if(name==='./role-settings')return {initializeRoleSettings:()=>({updateAvailability(){},refresh:async()=>{}})};
      throw Error(`허용하지 않은 모듈: ${name}`);
    }});
  const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  new vm.Script(compiled,{filename:'실제 시작창 renderer/index.ts'}).runInContext(context);
  await flush();
  return {get,document,reads:()=>reads,starts:()=>starts,stops:()=>stops,opens:()=>opens,
    failReads:()=>{rejectReads=true;},setEngine:(value:EngineState['state'])=>{current=engine(value);},
    nextRead:(value:Promise<AppStatus>)=>{nextRead=value;},deferStart:(value:Promise<EngineState>)=>{start=value;},
    readEachWith:(factory:()=>Promise<AppStatus>)=>{readFactory=factory;},
    tick:()=>{for(const callback of timers.values())callback();},
    focus:()=>windowEvents.emit('focus'),visible:()=>documentEvents.emit('visibilitychange'),
    close:()=>windowEvents.emit('beforeunload')};
}

test('구성 준비 진행률은 상태 값만 표시하며 화면 조회로 실행이나 다운로드를 요청하지 않는다',async()=>{
  const value=status(engine('stopped'));
  value.components={engine:{id:'engine',state:'downloading',message:'DO_NOT_DISPLAY_INTERNAL_DETAILS',receivedBytes:25,totalBytes:100,progress:25},relayTools:{id:'relay-tools',state:'missing',message:'DO_NOT_DISPLAY_INTERNAL_DETAILS',receivedBytes:0,totalBytes:200,progress:0}};
  const f=await fixture('stopped',Promise.resolve(value));try{
    assert.match(f.get('engine-component-message').textContent,/기록 기능.*25%/);assert.equal(f.get('engine-component-progress').hidden,false);assert.equal(Number(f.get('engine-component-progress').value),0.25);
    assert.match(f.get('relay-component-message').textContent,/처음 사용할 때/);assert.equal(f.get('relay-component-progress').hidden,true);
    assert.equal(f.get('engine-component-message').textContent.includes('DO_NOT_DISPLAY'),false);assert.equal(f.starts(),0);assert.equal(f.stops(),0);
  }finally{f.close();}
});

test('시작 최종 ready 응답은 후속 상태 조회가 실패해도 준비 완료와 다시 열기에 반영한다',async()=>{
  const f=await fixture();try{
    f.failReads();f.get('start').click();await flush();
    assert.equal(f.starts(),1);assert.equal(f.get('engine-state').textContent,engine('ready').message);
    assert.equal(f.get('reopen').disabled,false);assert.equal(f.get('start').disabled,true);assert.equal(f.get('stop').disabled,false);
    f.get('reopen').click();await flush();assert.equal(f.opens(),1);
  }finally{f.close();}
});

test('종료 최종 응답도 후속 상태 조회 실패와 관계없이 종료와 다시 열기 비활성에 반영한다',async()=>{
  const f=await fixture('ready');try{
    f.failReads();f.get('stop').click();await flush();
    assert.equal(f.stops(),1);assert.equal(f.get('engine-state').textContent,engine('stopped').message);
    assert.equal(f.get('reopen').disabled,true);assert.equal(f.get('start').disabled,false);assert.equal(f.get('stop').disabled,true);
  }finally{f.close();}
});

test('종료 뒤 늦은 시작 ready 응답으로 시작창을 되돌리지 않는다',async()=>{
  const f=await fixture();const start=deferred<EngineState>();try{
    f.deferStart(start.promise);f.failReads();f.get('start').click();await flush();
    assert.equal(f.get('stop').disabled,false);f.get('stop').click();await flush();
    start.resolve(engine('ready'));await flush();
    assert.equal(f.starts(),1);assert.equal(f.stops(),1);assert.equal(f.get('engine-state').textContent,engine('stopped').message);
    assert.equal(f.get('reopen').disabled,true);
  }finally{start.resolve(engine('ready'));await flush();f.close();}
});

test('시작 전 진행 중이던 상태 조회는 최종 ready 응답보다 늦게 도착해도 덮지 않는다',async()=>{
  const f=await fixture();const read=deferred<AppStatus>();try{
    f.nextRead(read.promise);f.tick();f.failReads();f.get('start').click();await flush();
    read.resolve(status(engine('starting')));await flush();
    assert.equal(f.get('engine-state').textContent,engine('ready').message);assert.equal(f.get('reopen').disabled,false);
  }finally{read.resolve(status(engine('starting')));await flush();f.close();}
});

test('숨겨진 시작창은 조회하지 않고 다시 보이거나 초점이 돌아오면 현재 상태를 즉시 읽는다',async()=>{
  const f=await fixture('starting');try{
    const before=f.reads();f.document.visibilityState='hidden';f.setEngine('ready');f.tick();f.focus();f.visible();await flush();
    assert.equal(f.reads(),before);assert.equal(f.get('reopen').disabled,true);
    f.document.visibilityState='visible';f.visible();await flush();
    assert.equal(f.reads(),before+1);assert.equal(f.get('engine-state').textContent,engine('ready').message);
    assert.equal(f.get('reopen').disabled,false);
    f.setEngine('stopped');f.focus();await flush();
    assert.equal(f.reads(),before+2);assert.equal(f.get('engine-state').textContent,engine('stopped').message);
    assert.equal(f.get('reopen').disabled,true);assert.equal(f.starts(),0);assert.equal(f.stops(),0);
  }finally{f.close();}
});

for(const outcome of ['resolve','reject'] as const)test(`종료 후 늦은 시작 ${outcome} 응답은 실제 종료 성공 안내를 덮지 않는다`,async()=>{
  const f=await fixture();const start=deferred<EngineState>();try{
    f.deferStart(start.promise);f.get('start').click();await flush();f.get('stop').click();await flush();
    const stopped=f.get('notice').textContent;assert.match(stopped,/기록 엔진을 종료했습니다/);
    if(outcome==='resolve')start.resolve(engine('ready'));else start.reject(Error('모의 이전 시작 취소'));
    await flush();assert.equal(f.get('notice').textContent,stopped);
    assert.equal(f.get('notice').className,'');assert.equal(f.get('engine-state').textContent,engine('stopped').message);
  }finally{start.resolve(engine('ready'));await flush();f.close();}
});

test('느린 자동 조회 중 연속 주기·초점 이벤트가 새 요청을 쌓거나 앞선 정상 응답을 버리지 않는다',async()=>{
  const f=await fixture('starting'),reads:Array<ReturnType<typeof deferred<AppStatus>>>=[];try{
    const before=f.reads();f.readEachWith(()=>{const next=deferred<AppStatus>();reads.push(next);return next.promise;});
    f.tick();f.tick();f.focus();f.visible();await flush();
    assert.equal(f.reads(),before+1);assert.equal(reads.length,1);
    reads[0].resolve(status(engine('ready')));await flush();
    assert.equal(f.get('engine-state').textContent,engine('ready').message);assert.equal(f.get('reopen').disabled,false);
    f.tick();await flush();assert.equal(reads.length,2);
  }finally{for(const read of reads)read.resolve(status(engine('ready')));await flush();f.close();}
});

test('자동 조회 대기 중에도 기관 저장의 명시 fill 조회를 생략하지 않는다',async()=>{
  const f=await fixture(),read=deferred<AppStatus>();try{
    const before=f.reads();f.nextRead(read.promise);f.tick();
    f.get('organization').value='변경 합성기관';f.get('organization-form').emit('submit');await flush();
    assert.equal(f.get('organization').value,'변경 합성기관');assert.ok(f.reads()>=before+2);
    read.resolve(status(engine('stopped')));await flush();assert.equal(f.get('organization').value,'변경 합성기관');
  }finally{read.resolve(status(engine('stopped')));await flush();f.close();}
});

test('최초 fill 조회가 느려도 자동 틱이 저장 기관을 덮거나 처음 단계 선택을 앞지르지 않는다',async()=>{
  const read=deferred<AppStatus>(),saved=status(engine('ready'));
  saved.setup.organization='저장 합성기관';
  const f=await fixture('starting',read.promise);try{
    f.tick();f.focus();f.visible();await flush();assert.equal(f.reads(),1);
    read.resolve(saved);await flush();
    assert.equal(f.get('organization').value,'저장 합성기관');
    assert.equal(f.get('step-title').textContent,'이 PC에서 기록을 시작하세요');
    assert.equal(f.get('engine-state').textContent,engine('ready').message);
  }finally{read.resolve(saved);await flush();f.close();}
});
