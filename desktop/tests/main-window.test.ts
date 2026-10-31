import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {mkdtemp,rm} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {runInNewContext} from 'node:vm';
import {setTimeout as delay} from 'node:timers/promises';
import {build} from 'esbuild';
import type {ManagedRelayStatus} from '../src/relay/contracts';
let bundled:Promise<string>|undefined;
function mainBundle() {
  bundled??=build({entryPoints:[resolve('src/main/index.ts')],bundle:true,write:false,platform:'node',format:'cjs',external:['electron','gukgam-test-engine','gukgam-test-auth','gukgam-test-relay'],plugins:[{
    name:'실행 없는 메인 경계',setup(builder){
      builder.onResolve({filter:/^(?:\.\/engine|\.\.\/auth\/chatgpt|\.\.\/relay)$/},args=>{
        if(!args.importer.replaceAll('\\','/').endsWith('/src/main/index.ts'))return;
        return {path:args.path,namespace:'모의-메인'};
      });
      builder.onLoad({filter:/.*/,namespace:'모의-메인'},args=>({contents:args.path==='./engine'?"export {EngineController,ENGINE_ORIGIN} from 'gukgam-test-engine';":args.path==='../relay'?"export {createManagedRelay} from 'gukgam-test-relay';":"export {ChatGPTService} from 'gukgam-test-auth';",loader:'js'}));
    }
  }]}).then(result=>result.outputFiles[0].text);
  return bundled;
}
async function harness(options:{failWorkspace?:boolean;failRelay?:boolean;relay?:Partial<ManagedRelayStatus>;startGate?:Promise<void>;statusGate?:Promise<void>;verifyGate?:Promise<void>;provisionGate?:Promise<void>;updateGate?:Promise<void>;failVerify?:boolean;sourceOnline?:boolean;failCancel?:boolean;failStopOnce?:boolean}={}) {
  const dir=await mkdtemp(join(tmpdir(),'gukgam-main-mock-'));
  let finished!:()=>void;const quit=new Promise<void>(done=>{finished=done;});
  const handlers=new Map<string,Function>(),windows:MockWindow[]=[],paths=new Map<string,string>();
  const stats={starts:0,stops:0,requests:0,healthReads:0,healthState:'verified',sent:[] as any[],opened:[] as string[],engineOptions:undefined as any,failWorkspace:Boolean(options.failWorkspace),relayReads:0,verifies:0,provisions:0,resumes:0,updates:0,relayCancels:0,events:[] as string[],configs:[] as any[],failStopOnce:Boolean(options.failStopOnce)};
  class MockContents extends EventEmitter {mainFrame={url:''};openHandler?:Function;setWindowOpenHandler(value:Function){this.openHandler=value;}}
  class MockWindow extends EventEmitter {
    webContents=new MockContents();destroyed=false;
    constructor(public options:any){super();windows.push(this);}
    async loadFile(file:string){this.webContents.mainFrame.url=pathToFileURL(file).href;}
    async loadURL(url:string){if(stats.failWorkspace){stats.failWorkspace=false;throw Error('MOCK 화면 오류');}this.webContents.mainFrame.url=url;}
    show(){}focus(){}isDestroyed(){return this.destroyed;}close(){this.destroy();}destroy(){this.destroyed=true;this.emit('closed');}
  }
  const app=Object.assign(new EventEmitter(),{getPath:(name:string)=>paths.get(name)??dir,setPath:(name:string,value:string)=>{assert.equal(existsSync(value),true,'setPath 이전 폴더 생성');paths.set(name,value);},setAppUserModelId:()=>{},commandLine:{hasSwitch:()=>false},requestSingleInstanceLock:()=>true,whenReady:()=>Promise.resolve(),getVersion:()=>'mock',isPackaged:true,quit:finished,exit:()=>{}});
  const sessionValue=Object.assign(new EventEmitter(),{setPermissionRequestHandler:()=>{}});
  const electron={app,BrowserWindow:MockWindow,dialog:{showErrorBox:()=>{}},ipcMain:{handle:(name:string,handler:Function)=>handlers.set(name,handler)},safeStorage:{isEncryptionAvailable:()=>false},session:{defaultSession:sessionValue,fromPartition:()=>sessionValue},shell:{openExternal:async(value:string)=>{stats.opened.push(value);}}};
  class EngineController {
    current={state:'stopped',message:'모의 대기'};
    generation=0;
    constructor(private options:any){stats.engineOptions=options;}
    status(){return {...this.current};}
    async start(config:unknown){stats.starts++;stats.configs.push(config);stats.events.push('engine.start');const generation=++this.generation;this.current={state:'starting',message:'모의 준비'};await options.startGate;if(generation!==this.generation)throw Error('모의 엔진 시작 취소');this.current={state:'ready',message:'모의 실행'};stats.events.push('engine.ready');return this.status();}
    async stop(){stats.stops++;++this.generation;this.current={state:'stopping',message:'모의 종료중'};if(stats.failStopOnce){stats.failStopOnce=false;throw Error('모의 엔진 종료 확인 실패');}this.current={state:'stopped',message:'모의 중지'};this.options.onStopped();return this.status();}
    send(value:any){stats.sent.push(value);}
    sendSensitive(value:any,_secret:string){stats.events.push('roles.sync');stats.sent.push({type:value.type,hasKey:Boolean(value.key)});}
  }
  class ChatGPTService {
    async connectionHealth(){stats.healthReads++;return {state:stats.healthState,account:'DO_NOT_FORWARD',accessToken:'DO_NOT_FORWARD'};}
    async status(){return {connected:false,verified:false,message:'모의 계정 연결 전',models:[]};}
    async connect(){throw Error('모의 계정 사용 한도 안내');}async test(){throw Error('모의 계정 사용 한도 안내');}async disconnect(){return this.status();}cancel(){}cancelRequest(){}
    async request(){stats.requests++;throw Error('모의 계정 연결 전');}
  }
  let relay:ManagedRelayStatus={state:'idle',message:'모의 미설정',teams:[],canResume:false,verified:false,sourceOnline:false,...options.relay};
  const createManagedRelay=()=>{if(options.failRelay)throw Error('MOCK 릴레이 도구 오류');return {
    status:async()=>{stats.relayReads++;await options.statusGate;return {...relay};},
    verify:async()=>{stats.verifies++;stats.events.push('relay.verify');await options.verifyGate;if(options.failVerify)throw Error('모의 릴레이 연결 확인 실패');relay={...relay,state:'ready',verified:true,sourceOnline:options.sourceOnline??true,message:options.sourceOnline===false?'릴레이는 정상이며 현재 PC의 연결을 기다리고 있습니다.':'이 PC 연결과 릴레이를 통한 실제 회의 목록 조회를 확인했습니다.'};return {...relay};},
    provision:async()=>{stats.provisions++;relay={...relay,state:'configuring'};await options.provisionGate;return {...relay};},
    resume:async()=>{stats.resumes++;relay={...relay,state:'configuring'};await options.provisionGate;return {...relay};},
    updateExistingSite:async()=>{stats.updates++;relay={...relay,state:'configuring'};await options.updateGate;relay={...relay,state:'ready',url:'https://updated-fixture.vercel.app',verified:true,sourceOnline:false,message:'기존 사이트 코드를 업데이트했습니다. 암호는 그대로입니다.'};return {...relay};},
    // 지연된 모의 verify는 취소 뒤에도 성공을 돌려줄 수 있게 해 메인 세대 보호를 검증한다.
    cancel:async()=>{stats.relayCancels++;if(options.failCancel)throw Error('모의 릴레이 취소 실패');return {...relay};}
  };};
  const nativeRequire=createRequire(resolve('tests/main-window.test.ts'));
  const requireMock=(name:string)=>name==='electron'?electron:name==='gukgam-test-engine'?{EngineController,ENGINE_ORIGIN:'http://127.0.0.1:8765'}:name==='gukgam-test-auth'?{ChatGPTService}:name==='gukgam-test-relay'?{createManagedRelay}:nativeRequire(name);
  const module={exports:{}};
  runInNewContext(await mainBundle(),{require:requireMock,module,exports:module.exports,__dirname:join(dir,'desktop','dist','main'),process:{platform:process.platform,env:{},resourcesPath:join(dir,'resources')},Buffer,URL,Error,setTimeout,clearTimeout,AbortController,AbortSignal,fetch:async()=>{throw Error('모의 메인 시험의 외부 호출 금지');},console});
  await delay(5);
  const invoke=async(name:string,...args:unknown[])=>handlers.get(`gukgam:${name}`)!({sender:windows[0].webContents,senderFrame:windows[0].webContents.mainFrame},...args);
  const invokeUntrusted=async(name:string,...args:unknown[])=>handlers.get(`gukgam:${name}`)!({sender:{},senderFrame:{url:windows[0].webContents.mainFrame.url}},...args);
  return {dir,windows,paths,stats,sessionValue,invoke,invokeUntrusted,setRelay:(value:Partial<ManagedRelayStatus>)=>{relay={...relay,...value};},rawRelay:()=>({...relay}),cleanup:async()=>{
    // 실제 앱처럼 소유 작업·진단 flush가 끝난 뒤 임시 자료를 지운다.
    app.emit('before-quit',{preventDefault:()=>{}});let timer:ReturnType<typeof setTimeout>|undefined;
    try{await Promise.race([quit,new Promise((_,fail)=>{timer=setTimeout(()=>fail(Error('모의 앱 정상 종료 대기 만료')),2000);})]);}
    finally{if(timer)clearTimeout(timer);}
    await rm(dir,{recursive:true,force:true});
  }};
}
test('새 설치 폴더를 만든 뒤 실제 메인 진입 코드가 초기 설정을 연다',async()=>{
  const h=await harness();try {assert.equal(h.windows.length,1);const result=await h.invoke('status');assert.equal(result.ok,true);assert.equal(result.data.chatgpt.connected,false);assert.match(result.data.setup.secureStorageWarning,/기록/);assert.equal(h.stats.starts,0);assert.equal(h.windows[0].options.webPreferences.sandbox,true);}finally{await h.cleanup();}
});

test('ChatGPT 미연결에서도 AI 설정을 열고 준비된 자기 엔진을 재사용한다',async()=>{
  const h=await harness();try {
    await h.invoke('save-setup',{organization:'합성기관'});
    assert.equal((await h.invoke('open-llm-settings-page')).ok,true);
    assert.equal(h.stats.starts,1);assert.equal(h.stats.requests,0);
    assert.equal(h.windows[1].webContents.mainFrame.url,'http://127.0.0.1:8765/admin#sec-ai');
    assert.equal((await h.invoke('open-llm-settings-page')).ok,true);
    assert.equal(h.stats.starts,1);assert.equal(h.windows.length,2);
    assert.equal(h.stats.events.filter(value=>value==='roles.sync').length,1);
    assert.equal((await h.invokeUntrusted('open-llm-settings-page')).ok,false);
  }finally{await h.cleanup();}
});

test('AI 설정을 여는 중 종료하면 늦은 시작 결과로 기록 창을 만들지 않는다',async()=>{
  const opening=gate(),h=await harness({startGate:opening.promise});try {
    await h.invoke('save-setup',{organization:'합성기관'});
    const pending=h.invoke('open-llm-settings-page');await until(()=>h.stats.starts===1);
    await h.invoke('stop-engine');opening.release();
    assert.equal((await pending).ok,false);assert.equal(h.windows.length,1);assert.equal(h.stats.requests,0);
  }finally{opening.release();await h.cleanup();}
});

test('동봉 매뉴얼은 엔진과 계정 없이 격리된 읽기 창에서 열고 같은 파일 목차만 허용한다',async()=>{
  const h=await harness();try {
    assert.equal((await h.invoke('open-user-manual')).ok,true);
    const manual=h.windows[1],url=manual.webContents.mainFrame.url;
    assert.match(url,/\/manual\//);assert.equal(manual.options.webPreferences.preload,undefined);
    assert.equal(manual.options.webPreferences.sandbox,true);assert.equal(h.stats.starts,0);assert.equal(h.stats.requests,0);
    let blocked=false;manual.webContents.emit('will-navigate',{preventDefault:()=>{blocked=true;}},url+'#install');assert.equal(blocked,false);
    manual.webContents.emit('will-navigate',{preventDefault:()=>{blocked=true;}},'https://example.invalid');assert.equal(blocked,true);
    assert.equal((await h.invokeUntrusted('open-user-manual')).ok,false);
    assert.equal((await h.invoke('open-user-manual')).ok,true);assert.equal(h.windows.length,2);
  }finally{await h.cleanup();}
});

test('요금 안내는 두 공식 고정 주소만 열고 임의 주소와 객체 입력을 거절한다',async()=>{
  const h=await harness();try {
    for(const input of ['https://example.invalid/pricing','neon?token=placeholder','__proto__',{},null]) {
      const result=await h.invoke('open-relay-pricing',input);assert.equal(result.ok,false);
    }
    assert.deepEqual(h.stats.opened,[]);
    assert.equal((await h.invoke('open-relay-pricing','neon')).ok,true);
    assert.equal((await h.invoke('open-relay-pricing','vercel')).ok,true);
    assert.deepEqual(h.stats.opened,['https://neon.com/pricing','https://vercel.com/pricing']);
  } finally {await h.cleanup();}
});

test('대시보드 연결 조회는 현재 안전 상태만 엔진에 보내고 AI 요청을 실행하지 않는다',async()=>{
  const h=await harness();try {
    await h.invoke('save-setup',{organization:'모의기관'});await h.invoke('start-engine');
    for(const state of ['verified','refreshable','expired','disconnected','checking','error','unverified']) {
      h.stats.healthState=state;
      h.stats.engineOptions.onEvent({type:'account.status.request',id:`health-${state}`});await delay(5);
      assert.deepEqual(JSON.parse(JSON.stringify(h.stats.sent.at(-1))),{type:'account.status.result',id:`health-${state}`,state});
    }
    assert.equal(h.stats.healthReads,7);assert.equal(h.stats.requests,0);
    assert.equal(JSON.stringify(h.stats.sent).includes('DO_NOT_FORWARD'),false);
    h.stats.engineOptions.onEvent({type:'account.status.request',id:'after-stop'});
    await h.invoke('stop-engine');await delay(5);
    assert.equal(h.stats.sent.some(value=>value.id==='after-stop'),false);
  }finally{await h.cleanup();}
});
test('화면 열기 실패 뒤에도 엔진·AI 브릿지는 유지되고 계정 오류가 실황을 중지하지 않는다',async()=>{
  const h=await harness({failWorkspace:true});try {
    assert.equal((await h.invoke('save-setup',{organization:'모의기관'})).ok,true);
    const start=await h.invoke('start-engine');assert.equal(start.ok,false);assert.match(start.message,/기록 엔진은 켜져/);
    h.stats.engineOptions.onEvent({type:'llm.request',id:'after-window-error',system:'',user:'모의',max_tokens:10});await delay(5);
    assert.equal(h.stats.requests,1);assert.equal(h.stats.sent.filter(value=>value.type==='llm.error').length,1);assert.equal((await h.invoke('open-workspace')).ok,true);
    const account=await h.invoke('connect-chatgpt');assert.equal(account.ok,false);assert.match(account.message,/한도/);assert.equal(h.stats.stops,0);assert.equal((await h.invoke('status')).data.engine.state,'ready');
    await h.invoke('stop-engine');
  }finally{await h.cleanup();}
});
test('내부 팝업은 부모 preload 없이 직접 만들고 외부 창·다운로드는 거절한다',async()=>{
  const h=await harness();try {
    await h.invoke('save-setup',{organization:'모의기관'});await h.invoke('start-engine');const workspace=h.windows[1],count=h.windows.length;
    assert.equal(workspace.webContents.openHandler!({url:'http://127.0.0.1:8765/s/mock/report'}).action,'deny');assert.equal(h.windows.length,count+1);
    assert.equal(h.windows.at(-1)!.options.webPreferences.preload,undefined);assert.equal(h.windows.at(-1)!.options.webPreferences.nodeIntegration,false);
    workspace.webContents.openHandler!({url:'https://outside.example.com'});assert.equal(h.windows.length,count+1);
    let blocked=false;h.sessionValue.emit('will-download',{preventDefault:()=>{blocked=true;}},{getURLChain:()=>['http://127.0.0.1:8765/api/sessions/mock/offline.html']});assert.equal(blocked,false);
    h.sessionValue.emit('will-download',{preventDefault:()=>{blocked=true;}},{getURLChain:()=>['https://outside.example.com/file']});assert.equal(blocked,true);await h.invoke('stop-engine');
  }finally{await h.cleanup();}
});
test('선택 릴레이 도구 초기화 오류는 로그인 없는 기록 기동을 막지 않는다',async()=>{
  const h=await harness({failRelay:true});try {await h.invoke('save-setup',{organization:'모의기관'});assert.equal((await h.invoke('relay-status')).data.state,'error');assert.equal((await h.invoke('start-engine')).ok,true);assert.equal(h.stats.starts,1);await h.invoke('stop-engine');}finally{await h.cleanup();}
});

const deployed=():Partial<ManagedRelayStatus>=>({state:'ready',url:'https://fixture-relay.vercel.app',canResume:false});
function gate(){let release!:()=>void;return {promise:new Promise<void>(resolve=>{release=resolve;}),release:()=>release()};}
async function until(check:()=>boolean){for(let i=0;i<100;i++){if(check())return;await delay(5);}assert.fail('모의 작업이 정해진 시간 안에 시작돼야 함');}

test('준비된 관리 사이트를 확인한 뒤 엔진·키·연결 확인을 순서대로 실행하고 릴레이 화면을 유지한다',async()=>{
  const h=await harness({relay:deployed()});try {
    await h.invoke('save-setup',{organization:'모의기관'});
    const result=await h.invoke('connect-managed-relay');
    assert.equal(result.ok,true);assert.equal(result.data.sourceOnline,true);
    assert.deepEqual(h.stats.events,['engine.start','engine.ready','roles.sync','relay.verify']);
    assert.equal(h.stats.relayReads,1);assert.equal(h.stats.starts,1);assert.equal(h.stats.verifies,1);
    assert.equal(h.windows.length,1);assert.equal(h.stats.requests,0);assert.equal(h.stats.provisions,0);assert.equal(h.stats.resumes,0);
    assert.equal((await h.invoke('relay-status')).data.sourceOnline,true);
    assert.equal((await h.invoke('connect-managed-relay')).ok,true);
    assert.equal(h.stats.starts,1,'준비된 자기 엔진은 다시 시작하지 않음');assert.equal(h.stats.verifies,2);assert.equal(h.windows.length,1);
  }finally{await h.cleanup();}
});

test('기존 기록 화면으로 시작한 엔진도 교체 없이 재사용하고 원본 연결 대기를 정상 결과로 반환한다',async()=>{
  const h=await harness({relay:deployed(),sourceOnline:false});try {
    await h.invoke('save-setup',{organization:'모의기관'});assert.equal((await h.invoke('start-engine')).ok,true);
    assert.equal(h.windows.length,2);
    const result=await h.invoke('connect-managed-relay');
    assert.equal(result.ok,true);assert.equal(result.data.verified,true);assert.equal(result.data.sourceOnline,false);assert.match(result.data.message,/연결을 기다/);
    assert.equal(h.stats.starts,1);assert.equal(h.stats.stops,0);assert.equal(h.windows.length,2);assert.equal(h.stats.requests,0);
  }finally{await h.cleanup();}
});

test('이전 연결 확인 실패가 canResume을 남겨도 배포 URL이 있으면 새 리소스 생성 없이 다시 연결한다',async()=>{
  const h=await harness({relay:{...deployed(),state:'error',canResume:true,message:'이전 확인 실패'}});try {
    await h.invoke('save-setup',{organization:'모의기관'});
    const result=await h.invoke('connect-managed-relay');assert.equal(result.ok,true);assert.equal(result.data.sourceOnline,true);
    assert.equal(h.stats.starts,1);assert.equal(h.stats.verifies,1);assert.equal(h.stats.provisions,0);assert.equal(h.stats.resumes,0);
  }finally{await h.cleanup();}
});

test('사이트 미준비·로그인·승인·구성 중에는 엔진 시작이나 연결 요청을 하지 않는다',async()=>{
  const h=await harness();try {
    await h.invoke('save-setup',{organization:'모의기관'});
    const cases:Array<Partial<ManagedRelayStatus>>=[{state:'idle',url:undefined},...(['login','approval','configuring'] as const).map(state=>({...deployed(),state})),{...deployed(),url:'http://fixture.example.com'},{...deployed(),url:'https://user:pass@fixture.example.com'},{...deployed(),url:'https://fixture.example.com?secret=mock'}];
    for(const value of cases){h.setRelay(value);const result=await h.invoke('connect-managed-relay');assert.equal(result.ok,false);}
    assert.equal(h.stats.starts,0);assert.equal(h.stats.verifies,0);assert.equal(h.windows.length,1);assert.equal(h.stats.requests,0);
  }finally{await h.cleanup();}
});

test('연결 확인 실패 후에도 이미 켜진 엔진과 기존 AI 브릿지는 유지한다',async()=>{
  const h=await harness({relay:deployed(),failVerify:true});try {
    await h.invoke('save-setup',{organization:'모의기관'});
    const result=await h.invoke('connect-managed-relay');assert.equal(result.ok,false);assert.match(result.message,/연결 확인 실패/);
    assert.equal((await h.invoke('status')).data.engine.state,'ready');assert.equal(h.stats.stops,0);assert.equal(h.windows.length,1);assert.equal(h.stats.requests,0);
    h.stats.engineOptions.onEvent({type:'llm.request',id:'after-relay-failure',system:'',user:'모의',max_tokens:10});await delay(5);
    assert.equal(h.stats.requests,1);assert.equal(h.stats.sent.filter(value=>value.type==='llm.error').length,1);
  }finally{await h.cleanup();}
});

test('관리 사이트를 읽는 동안에도 중복 연결·별도 시작·설정 변경·구성은 차단한다',async()=>{
  const read=gate(),h=await harness({relay:deployed(),statusGate:read.promise});let pending:Promise<any>|undefined;
  try {
    await h.invoke('save-setup',{organization:'모의기관'});pending=h.invoke('connect-managed-relay');
    await until(()=>h.stats.relayReads===1);
    for(const [name,...args] of [['connect-managed-relay'],['start-engine'],['save-setup',{organization:'변경기관'}],['provision-relay',{}],['resume-relay'],['verify-managed-relay']])assert.equal((await h.invoke(String(name),...args)).ok,false);
    assert.equal(h.stats.starts,0);assert.equal(h.stats.provisions,0);assert.equal(h.stats.resumes,0);
    read.release();assert.equal((await pending).ok,true);assert.equal(h.stats.starts,1);
  }finally{read.release();await pending;await h.cleanup();}
});

test('엔진 설정 읽기 전의 시작 선점도 기관 변경·릴레이 구성과 경합하지 않는다',async()=>{
  const h=await harness();try {
    await h.invoke('save-setup',{organization:'원래기관'});
    const pending=h.invoke('start-engine');
    assert.equal((await h.invoke('save-setup',{organization:'변경기관'})).ok,false);
    assert.equal((await h.invoke('resume-relay')).ok,false);assert.equal((await h.invoke('provision-relay',{})).ok,false);
    assert.equal((await pending).ok,true);assert.equal(h.stats.configs[0].organization,'원래기관');assert.equal(h.stats.provisions,0);assert.equal(h.stats.resumes,0);
  }finally{await h.cleanup();}
});

test('릴레이 구성 선점은 공통 엔진 시작과 관리 연결을 모두 막는다',async()=>{
  const provision=gate(),h=await harness({relay:deployed(),provisionGate:provision.promise});let pending:Promise<any>|undefined;
  try {
    await h.invoke('save-setup',{organization:'모의기관'});pending=h.invoke('resume-relay');
    assert.equal((await h.invoke('start-engine')).ok,false);assert.equal((await h.invoke('connect-managed-relay')).ok,false);
    assert.equal((await h.invoke('save-setup',{organization:'변경기관'})).ok,false);assert.equal(h.stats.starts,0);
    provision.release();assert.equal((await pending).ok,true);
  }finally{provision.release();await pending;await h.cleanup();}
});

test('관리 연결로 엔진 준비 중 종료하면 늦은 준비를 연결 확인에 사용하지 않는다',async()=>{
  const start=gate(),h=await harness({relay:deployed(),startGate:start.promise});let pending:Promise<any>|undefined;
  try {
    await h.invoke('save-setup',{organization:'모의기관'});pending=h.invoke('connect-managed-relay');await until(()=>h.stats.starts===1);
    assert.equal((await h.invoke('stop-engine')).ok,true);start.release();assert.equal((await pending).ok,false);
    assert.equal(h.stats.relayCancels,1);assert.equal(h.stats.verifies,0);assert.equal(h.windows.length,1);assert.equal((await h.invoke('status')).data.engine.state,'stopped');
  }finally{start.release();await pending;await h.cleanup();}
});

test('종료 중 늦은 연결 성공은 반환·상태 폴링·새 엔진 세대에 재사용하지 않는다',async()=>{
  const verify=gate(),h=await harness({relay:deployed(),verifyGate:verify.promise,failCancel:true});let pending:Promise<any>|undefined;
  try {
    await h.invoke('save-setup',{organization:'모의기관'});pending=h.invoke('connect-managed-relay');await until(()=>h.stats.verifies===1);
    assert.equal((await h.invoke('stop-engine')).ok,true,'릴레이 취소 오류가 자기 엔진 정지를 막지 않음');
    verify.release();assert.equal((await pending).ok,false);assert.equal(h.stats.relayCancels,1);assert.equal(h.rawRelay().sourceOnline,true,'늦게 도착한 모의 백엔드 성공');
    let value=(await h.invoke('relay-status')).data;assert.equal(value.sourceOnline,false);assert.match(value.message,/연결을 기다/);assert.doesNotMatch(value.message,/실제 회의 목록 조회를 확인/);
    assert.equal((await h.invoke('start-engine')).ok,true);value=(await h.invoke('relay-status')).data;assert.equal(value.sourceOnline,false);assert.match(value.message,/다시|기다/);
    assert.equal((await h.invoke('verify-managed-relay')).data.sourceOnline,true,'새 소유 엔진의 명시적 확인 뒤에만 현재 연결로 표시');
    assert.equal(h.stats.starts,2);assert.equal(h.stats.requests,0);
  }finally{verify.release();await pending;await h.cleanup();}
});

test('엔진 정지 실패는 릴레이 취소와 별도로 사용자에게 전달하고 늦은 성공은 차단한다',async()=>{
  const verify=gate(),h=await harness({relay:deployed(),verifyGate:verify.promise,failStopOnce:true});let pending:Promise<any>|undefined;
  try {
    await h.invoke('save-setup',{organization:'모의기관'});pending=h.invoke('connect-managed-relay');await until(()=>h.stats.verifies===1);
    const stopped=await h.invoke('stop-engine');assert.equal(stopped.ok,false);assert.match(stopped.message,/종료 확인 실패/);
    verify.release();assert.equal((await pending).ok,false);assert.equal(h.stats.relayCancels,1);assert.equal((await h.invoke('relay-status')).data.sourceOnline,false);
  }finally{verify.release();await pending;await h.cleanup();}
});

test('관리 사이트 열기는 main이 저장한 HTTPS 주소만 사용하고 연결·AI·엔진 시작을 하지 않는다',async()=>{
  const h=await harness({relay:deployed()});try {
    assert.equal((await h.invoke('open-managed-relay-site','https://injected.example.com')).ok,true);
    assert.deepEqual(h.stats.opened,['https://fixture-relay.vercel.app']);
    for(const url of [undefined,'javascript:alert(1)','file:///C:/mock','http://fixture.example.com','https://user:pass@fixture.example.com','https://fixture.example.com?token=mock','https://127.0.0.1']){h.setRelay({url});assert.equal((await h.invoke('open-managed-relay-site')).ok,false);}
    assert.equal(h.stats.opened.length,1);assert.equal(h.stats.starts,0);assert.equal(h.stats.verifies,0);assert.equal(h.stats.requests,0);
    h.setRelay({state:'error',url:'https://fixture-relay.vercel.app',sourceOnline:true,message:'모의 실제 오류 안내'});
    assert.equal((await h.invoke('relay-status')).data.message,'모의 실제 오류 안내');
  }finally{await h.cleanup();}
});

test('새 관리 연결·사이트 열기 IPC도 시작창의 정확한 송신 화면만 허용한다',async()=>{
  const h=await harness({relay:deployed()});try {
    for(const name of ['connect-managed-relay','open-managed-relay-site'])assert.equal((await h.invokeUntrusted(name)).ok,false);
    assert.equal(h.stats.relayReads,0);assert.equal(h.stats.starts,0);assert.equal(h.stats.verifies,0);assert.equal(h.stats.opened.length,0);
  }finally{await h.cleanup();}
});

test('기존 사이트 업데이트는 송신자·무인자·엔진 종료 조건을 지키고 별도 창이나 엔진을 시작하지 않는다',async()=>{
  const h=await harness({relay:deployed()});try {
    assert.equal((await h.invokeUntrusted('update-managed-relay-site')).ok,false);
    assert.equal((await h.invoke('update-managed-relay-site',{projectId:'injected',url:'https://injected.invalid'})).ok,false);
    await h.invoke('save-setup',{organization:'모의기관'});await h.invoke('start-engine');
    assert.equal((await h.invoke('update-managed-relay-site')).ok,false);assert.equal(h.stats.updates,0);
    await h.invoke('stop-engine');const windows=h.windows.length;
    const result=await h.invoke('update-managed-relay-site');assert.equal(result.ok,true);
    assert.equal(result.data.url,'https://updated-fixture.vercel.app');assert.equal(result.data.sourceOnline,false);
    assert.equal(h.stats.updates,1);assert.equal(h.stats.starts,1);assert.equal(h.stats.requests,0);assert.equal(h.stats.provisions,0);assert.equal(h.stats.resumes,0);assert.equal(h.windows.length,windows);
  }finally{await h.cleanup();}
});

test('업데이트의 첫 await 전 선점으로 중복 배포·기동·연결·설정 변경을 차단한다',async()=>{
  const update=gate(),h=await harness({relay:deployed(),updateGate:update.promise});let pending:Promise<any>|undefined;
  try {
    await h.invoke('save-setup',{organization:'모의기관'});pending=h.invoke('update-managed-relay-site');await until(()=>h.stats.updates===1);
    for(const [name,...args] of [['update-managed-relay-site'],['start-engine'],['connect-managed-relay'],['save-setup',{organization:'변경기관'}],['provision-relay',{}],['resume-relay'],['verify-managed-relay']])assert.equal((await h.invoke(String(name),...args)).ok,false);
    assert.equal(h.stats.updates,1);assert.equal(h.stats.starts,0);assert.equal(h.stats.provisions,0);assert.equal(h.stats.resumes,0);
    update.release();assert.equal((await pending).ok,true);
  }finally{update.release();await pending;await h.cleanup();}
});
