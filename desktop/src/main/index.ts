import {app,BrowserWindow,dialog,ipcMain,safeStorage,session,shell,type IpcMainInvokeEvent} from 'electron';
import {join,resolve} from 'node:path';
import {existsSync,mkdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {pathToFileURL} from 'node:url';
import {ChatGPTService} from '../auth/chatgpt';
import {SecureVault} from '../storage/vault';
import {SetupStore} from '../storage/settings';
import {EngineController,ENGINE_ORIGIN} from './engine';
import {LlmBridge} from './llm-bridge';
import {isEngineUrl,isEngineDownloadUrl,relayUrl} from './security';
import {checkRelayHealth} from './relay-health';
import {applicationPaths,isolatedWebPreferences} from './window-policy';
import {createManagedRelay} from '../relay';
import type {ManagedRelayStatus,RelayProvisionInput} from '../relay/contracts';
import type {AppStatus,EngineState} from '../shared/contracts';
import {DesktopDiagnostics} from './diagnostics';
import {RoleSettingsService} from './role-settings';

let userData=join(app.getPath('appData'),'GukgamLiveDesktop'),ciSmoke=false,startupFailure='';
try {
  const paths=applicationPaths({platform:process.platform,appData:app.getPath('appData'),tempDir:process.env.CI==='true'&&process.env.RUNNER_TEMP?process.env.RUNNER_TEMP:tmpdir(),ci:process.env.CI,smokeSwitch:app.commandLine.hasSwitch('gukgam-ci-smoke'),smokeDir:process.env.GUKGAM_DESKTOP_SMOKE_DIR});
  userData=paths.userData;ciSmoke=paths.ciSmoke;
  mkdirSync(userData,{recursive:true,mode:0o700});mkdirSync(paths.sessionData,{recursive:true,mode:0o700});
  app.setPath('userData',userData);app.setPath('sessionData',paths.sessionData);
} catch {startupFailure='앱 전용 기록 폴더를 준비하지 못했습니다. Windows 사용자 폴더의 쓰기 권한과 저장 공간을 확인한 뒤 다시 실행하세요.';}
app.setAppUserModelId('kr.gukgam.live.desktop');
let setupWindow:BrowserWindow|undefined,workspace:BrowserWindow|undefined,manualWindow:BrowserWindow|undefined;
const workspaceChildren=new Set<BrowserWindow>();
let engine:EngineController,chat:ChatGPTService,settings:SetupStore,bridge:LlmBridge|undefined,roles:RoleSettingsService;
let quitting=false;
let loginPending=false,startingEngine=false,connectingManagedRelay=false,preparingRelay=false,startGeneration=0;
let managedSourceGeneration:number|undefined;
let cancelRelayOnQuit:(()=>Promise<unknown>)|undefined;
let diagnostics:DesktopDiagnostics|undefined;
const setupFile=join(__dirname,'../renderer/index.html');
const setupUrl=pathToFileURL(setupFile).href;
const lockedPreferences=isolatedWebPreferences;

function brandIcon():string|undefined {
  const base=app.isPackaged?process.resourcesPath:resolve(__dirname,'../../assets');
  if(typeof base!=='string')return;
  const file=join(base,'brand/gukgam-live-1024.png');
  return existsSync(file)?file:undefined;
}

function secureWindow(window:BrowserWindow,allowed:(url:string)=>boolean,allowInternalChildren=false) {
  window.webContents.setWindowOpenHandler(({url})=>{
    // 부모의 preload나 팝업 옵션을 상속하지 않고 소유한 창을 직접 만든다.
    if(allowInternalChildren&&isEngineUrl(url)&&engine.status().state==='ready'&&!quitting) {
      const child=new BrowserWindow({width:1100,height:850,icon:brandIcon(),webPreferences:{...lockedPreferences,partition:'persist:gukgam-workspace'}});
      workspaceChildren.add(child);secureWindow(child,isEngineUrl,true);child.on('closed',()=>workspaceChildren.delete(child));
      void child.loadURL(url).catch(()=>{if(!child.isDestroyed())child.destroy();});
    }
    return {action:'deny'};
  });
  window.webContents.on('will-navigate',(event,url)=>{if(!allowed(url))event.preventDefault();});
  window.webContents.on('will-redirect',(event,url)=>{if(!allowed(url))event.preventDefault();});
  window.webContents.on('will-attach-webview',event=>event.preventDefault());
}
function showSetup() {
  if(setupWindow&&!setupWindow.isDestroyed()){setupWindow.show();setupWindow.focus();return;}
  setupWindow=new BrowserWindow({width:1060,height:800,minWidth:760,minHeight:650,title:'국감 라이브 · 시작하기',icon:brandIcon(),webPreferences:{...lockedPreferences,preload:join(__dirname,'../preload/index.cjs')}});
  secureWindow(setupWindow,url=>url===setupUrl);
  const ownedWindow=setupWindow;
  ownedWindow.on('closed',()=>{if(setupWindow===ownedWindow)setupWindow=undefined;});
  void ownedWindow.loadFile(setupFile).catch(()=>dialog.showErrorBox('시작 화면을 열지 못했습니다','설치된 시작 화면 파일을 읽지 못했습니다. 앱을 다시 설치한 뒤 실행하세요. 저장한 기록은 앱을 제거해도 보관됩니다.'));
}
async function showWorkspace() {
  if(engine.status().state!=='ready')throw Error('먼저 기록 엔진을 시작하세요.');
  if(workspace&&!workspace.isDestroyed()){workspace.show();workspace.focus();return;}
  workspace=new BrowserWindow({width:1360,height:900,minWidth:850,minHeight:650,title:'국감 라이브 · 회의 기록',icon:brandIcon(),webPreferences:{...lockedPreferences,partition:'persist:gukgam-workspace'}});
  secureWindow(workspace,isEngineUrl,true);
  const ownedWindow=workspace;
  ownedWindow.on('closed',()=>{if(workspace===ownedWindow)workspace=undefined;});
  try {await ownedWindow.loadURL(ENGINE_ORIGIN);} catch {if(!ownedWindow.isDestroyed())ownedWindow.destroy();throw Error('기록 엔진은 켜져 있지만 화면을 열지 못했습니다. 시작 화면에서 ‘열린 기록 화면 보기’를 다시 누르세요.');}
}
async function showUserManual() {
  if(manualWindow&&!manualWindow.isDestroyed()){manualWindow.show();manualWindow.focus();return;}
  const file=app.isPackaged?join(process.resourcesPath,'manual/사용설명서.html'):resolve(__dirname,'../../../docs/public-release/manual.html');
  const url=pathToFileURL(file).href;
  const owned=new BrowserWindow({width:1080,height:880,minWidth:720,minHeight:620,title:'국감 라이브 · 설치·활용 매뉴얼',icon:brandIcon(),webPreferences:{...lockedPreferences}});
  manualWindow=owned;
  secureWindow(owned,value=>value.split('#')[0]===url);
  owned.on('closed',()=>{if(manualWindow===owned)manualWindow=undefined;});
  try {await owned.loadFile(file);}catch {if(!owned.isDestroyed())owned.close();throw Error('설치·활용 매뉴얼을 읽지 못했습니다. 최신 설치 파일의 매뉴얼 동봉 여부를 확인하세요.');}
}
function assertSender(event:IpcMainInvokeEvent) {
  if(!setupWindow||event.sender!==setupWindow.webContents||event.senderFrame!==setupWindow.webContents.mainFrame||event.senderFrame.url!==setupUrl)throw Error('이 화면에서는 앱 설정을 제어할 수 없습니다.');
}
function assertIdle() {
  if(startingEngine||connectingManagedRelay||preparingRelay)throw Error('기록 엔진 시작 또는 릴레이 연결·구성이 진행 중입니다. 완료한 뒤 설정을 바꾸세요.');
  if(['starting','ready','stopping'].includes(engine.status().state))throw Error('기관명이나 릴레이 설정을 바꾸려면 먼저 기록 엔진을 종료하세요. 계정 연결은 수집 중에도 바꿀 수 있습니다.');
}
function handle(name:string,action:(...args:unknown[])=>unknown) {
  ipcMain.handle(`gukgam:${name}`,async(event,...args)=>{
    try {assertSender(event);return {ok:true,data:await action(...args)};}
    catch(error) {return {ok:false,message:error instanceof Error&&/[가-힣]/.test(error.message)?error.message:'작업을 완료하지 못했습니다. 앱을 다시 열고 설정을 확인하세요.'};}
  });
}
async function status():Promise<AppStatus> {return {version:app.getVersion(),setup:await settings.read(),chatgpt:await chat.status(),engine:engine.status()};}
/** 화면 이동 여부만 다르게 하며 같은 소유 엔진·브릿지·키 준비 경로를 사용한다. */
async function startOwnedEngine(openWorkspace:boolean,reuseReady=false):Promise<{result:EngineState;generation:number}> {
  const current=engine.status();
  if(quitting||startingEngine||['starting','stopping'].includes(current.state)||current.state==='ready'&&!reuseReady)throw Error('기록 엔진이 이미 실행 중이거나 시작·종료 중입니다.');
  startingEngine=true;const generation=++startGeneration;managedSourceGeneration=undefined;
  const check=()=>{if(quitting||generation!==startGeneration)throw Error('기록 엔진 시작을 취소했습니다.');};
  try {
    let result=current;
    if(current.state!=='ready') {
      const config=await settings.engineConfig();check();
      bridge?.close();const ownedBridge=new LlmBridge(chat,()=>config.model,value=>engine.send(value));bridge=ownedBridge;
      diagnostics?.log('engine.start',{engineState:'starting'});
      try {result=await engine.start(config);diagnostics?.log('engine.state',{engineState:result.state});}
      catch(error) {diagnostics?.log('engine.state',{engineState:'error'});ownedBridge.close();if(bridge===ownedBridge)bridge=undefined;throw error;}
    }
    check();if(engine.status().state!=='ready')throw Error('기록 엔진 준비를 확인하지 못했습니다. 시작 화면에서 상태를 확인하세요.');
    await roles.syncKey();check();
    if(engine.status().state!=='ready')throw Error('기록 엔진이 종료됐습니다. 다시 시작한 뒤 연결을 확인하세요.');
    if(openWorkspace)await showWorkspace();
    return {result,generation};
  } finally {startingEngine=false;}
}
/** 다른 엔진 세대의 늦은 확인 결과를 현재 PC 연결로 표시하지 않는다. */
function managedStatusForEngine(value:ManagedRelayStatus):ManagedRelayStatus {
  const sourceOnline=value.sourceOnline&&engine.status().state==='ready'&&managedSourceGeneration===startGeneration;
  return {...value,sourceOnline,...(value.sourceOnline&&!sourceOnline&&value.state==='ready'?{
    message:engine.status().state==='ready'?'릴레이는 정상이며 현재 PC의 연결 확인을 기다리고 있습니다. ‘현재 PC 연결’을 눌러 확인하세요.':'릴레이는 정상이며 현재 PC의 연결을 기다리고 있습니다. ‘현재 PC 연결’을 눌러 기록 엔진을 켜세요.'
  }:{})};
}

if(startupFailure){dialog.showErrorBox('앱을 시작하지 못했습니다',startupFailure);app.exit(1);}
else if(!app.requestSingleInstanceLock())app.quit();
else {
  app.on('second-instance',()=>{void app.whenReady().then(showSetup);});
  app.whenReady().then(async()=>{
    diagnostics=new DesktopDiagnostics(userData);
    diagnostics.log('app.start',{version:app.getVersion()});
    session.defaultSession.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false));
    const workspaceSession=session.fromPartition('persist:gukgam-workspace');
    workspaceSession.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false));
    // 기존 서버 내보내기와 그 화면의 Blob은 운영체제 저장 대화상자로 내려받는다.
    workspaceSession.on('will-download',(event,item)=>{if(!item.getURLChain().length||!item.getURLChain().every(isEngineDownloadUrl))event.preventDefault();});
    const vault=new SecureVault(join(userData,'vault'),safeStorage);
    settings=new SetupStore(userData,vault);
    const projectRoot=resolve(__dirname,'../../..');
    const managedRelayReady=Promise.resolve().then(()=>createManagedRelay({userData,templateDir:app.isPackaged?join(process.resourcesPath,'relay-template'):join(projectRoot,'cloud'),resourcesPath:process.resourcesPath,projectRoot,packaged:app.isPackaged,engineIsReady:()=>engine.status().state==='ready',version:app.getVersion(),onSiteUpdate:value=>diagnostics?.log('relay.site-update',{siteUpdate:value}),settings,vault,openExternal:async value=>{
      const url=new URL(value);
      if(url.protocol!=='https:'||url.username||url.password||url.port||!['vercel.com','neon.tech'].some(host=>url.hostname===host||url.hostname.endsWith(`.${host}`)))throw Error('공식 사이트 준비 주소가 아닙니다. 기관 담당자에게 확인하세요.');
      await shell.openExternal(value);
    }}));
    void managedRelayReady.catch(()=>{});
    const managedRelay=async()=>{try{return await managedRelayReady;}catch{throw Error('사이트 준비 도구를 읽지 못했습니다. 로컬 기록은 계속 사용할 수 있습니다. 앱 설치 파일을 확인하세요.');}};
    const verifyManagedRelay=async(relay:Awaited<typeof managedRelayReady>,generation=startGeneration)=>{
      managedSourceGeneration=undefined;
      const result=await relay.verify();
      if(!quitting&&generation===startGeneration&&engine.status().state==='ready'&&result.sourceOnline)managedSourceGeneration=generation;
      return managedStatusForEngine(result);
    };
    cancelRelayOnQuit=async()=>{const relay=await managedRelayReady.catch(()=>undefined);return relay?.cancel();};
    chat=new ChatGPTService(vault,join(userData,'account'),async value=>{
      const url=new URL(value);
      if(url.protocol!=='https:'||url.hostname!=='auth.openai.com'||url.pathname!=='/api/accounts/authorize'||url.username||url.password)throw Error('공식 ChatGPT 로그인 주소가 아닙니다.');
      await shell.openExternal(value);
    },{onRequestStatus:value=>diagnostics?.request(value)});
    engine=new EngineController({userData,resourcesPath:process.resourcesPath,projectRoot:resolve(__dirname,'../../..'),packaged:app.isPackaged,env:{...process.env,...(ciSmoke?{GUKGAM_ALLOW_ASSEMBLY_NETWORK:'0'}:{})},
      onEvent:event=>{
        if(event.type==='llm.request')bridge?.accept(event);
        else if(event.type==='llm.cancel')bridge?.cancel(event.id);
        else if(event.type==='account.status.request') {
          const owner=bridge;
          void chat.connectionHealth().catch(()=>({state:'unavailable' as const})).then(health=>{
            if(owner&&bridge===owner&&engine.status().state==='ready')engine.send({type:'account.status.result',id:event.id,state:health.state});
          });
        }
      },
      onStopped:()=>{diagnostics?.log('engine.stopped',{engineState:engine.status().state});bridge?.close();bridge=undefined;for(const child of workspaceChildren)if(!child.isDestroyed())child.close();if(workspace&&!workspace.isDestroyed())workspace.close();}});
    handle('status',status);
    roles=new RoleSettingsService(vault,engine);
    handle('role-settings-status',()=>roles.status());
    handle('save-jev-key',value=>roles.saveKey(value));
    handle('delete-jev-key',()=>roles.deleteKey());
    handle('save-role-settings',value=>roles.saveSelection(value));
    handle('prepare-julia',()=>roles.prepareJulia());
    handle('cancel-julia',()=>roles.cancelJulia());
    handle('open-role-settings-page',async()=>{await showWorkspace();if(workspace&&!workspace.isDestroyed())await workspace.loadURL(`${ENGINE_ORIGIN}/admin#sec-speaker-review`);});
    handle('save-setup',value=>{assertIdle();return settings.save(value);});
    handle('connect-chatgpt',async()=>{if(loginPending)throw Error('로그인이 이미 진행 중입니다.');loginPending=true;try{return await chat.connect();}finally{loginPending=false;}});
    handle('cancel-login',()=>{if(loginPending)chat.cancel();});
    handle('test-chatgpt',()=>chat.test());
    handle('disconnect-chatgpt',()=>chat.disconnect());
    handle('start-engine',async()=>{
      if(connectingManagedRelay||preparingRelay)throw Error('릴레이 연결·구성이 진행 중입니다. 완료한 뒤 기록 화면을 여세요.');
      return (await startOwnedEngine(true)).result;
    });
    handle('stop-engine',async()=>{
      ++startGeneration;managedSourceGeneration=undefined;bridge?.close();bridge=undefined;
      if(connectingManagedRelay)void managedRelayReady.then(relay=>relay.cancel()).catch(()=>{});
      diagnostics?.log('engine.stop',{engineState:'stopping'});const result=await engine.stop();diagnostics?.log('engine.state',{engineState:result.state});return result;
    });
    handle('open-workspace',showWorkspace);
    handle('open-llm-settings-page',async()=>{
      if(connectingManagedRelay||preparingRelay)throw Error('릴레이 연결·구성이 진행 중입니다. 완료한 뒤 AI 연결 설정을 여세요.');
      const generation=engine.status().state==='ready'?startGeneration:(await startOwnedEngine(false)).generation;
      if(generation!==startGeneration||engine.status().state!=='ready')throw Error('기록 엔진 상태가 바뀌었습니다. 다시 AI 연결 설정을 여세요.');
      await showWorkspace();
      if(workspace&&!workspace.isDestroyed())await workspace.loadURL(`${ENGINE_ORIGIN}/admin#sec-ai`);
    });
    handle('open-user-manual',showUserManual);
    handle('open-help',()=>shell.openExternal('https://vercel.com/new'));
    handle('check-relay',value=>{if(typeof value!=='string')throw Error('릴레이 주소를 입력하세요.');return checkRelayHealth(value);});
    handle('relay-status',async()=>{try{return managedStatusForEngine(await (await managedRelay()).status());}catch(error){return {state:'error',message:error instanceof Error?error.message:'사이트 구성 상태를 읽지 못했습니다.',teams:[],canResume:false,verified:false,sourceOnline:false} satisfies ManagedRelayStatus;}});
    handle('connect-relay-account',async()=>(await managedRelay()).connect());
    handle('relay-catalog',async teamId=>{if(typeof teamId!=='string'||!teamId||teamId.length>200)throw Error('Vercel 팀을 선택하세요.');return (await managedRelay()).catalog(teamId);});
    handle('relay-plans',async value=>(await managedRelay()).plans(value as {teamId:string;productId:string;metadata:Record<string,unknown>}));
    handle('provision-relay',async value=>{assertIdle();preparingRelay=true;try{return await (await managedRelay()).provision(value as RelayProvisionInput);}finally{preparingRelay=false;}});
    handle('resume-relay',async()=>{assertIdle();preparingRelay=true;try{return await (await managedRelay()).resume();}finally{preparingRelay=false;}});
    handle('update-managed-relay-site',async(...args)=>{
      if(args.length)throw Error('사이트 업데이트는 저장된 관리 사이트만 사용하며 주소나 식별자를 입력받지 않습니다.');
      assertIdle();preparingRelay=true;managedSourceGeneration=undefined;
      try{return managedStatusForEngine(await (await managedRelay()).updateExistingSite());}finally{preparingRelay=false;}
    });
    handle('cancel-relay',async()=>(await managedRelay()).cancel());
    handle('open-relay-approval',async()=>(await managedRelay()).openApproval());
    handle('open-relay-pricing',async provider=>{
      // 화면에서는 공급자 이름만 받는다. 임의 주소·인증값을 외부로 전달하지 않는다.
      if(provider!=='neon'&&provider!=='vercel')throw Error('공식 요금 안내를 선택하세요.');
      await shell.openExternal(provider==='neon'?'https://neon.com/pricing':'https://vercel.com/pricing');
    });
    handle('verify-managed-relay',async()=>{
      if(connectingManagedRelay||preparingRelay||startingEngine)throw Error('릴레이 연결·구성이 진행 중입니다. 완료한 뒤 다시 확인하세요.');
      return verifyManagedRelay(await managedRelay());
    });
    handle('connect-managed-relay',async()=>{
      if(quitting||connectingManagedRelay||preparingRelay||startingEngine||['starting','stopping'].includes(engine.status().state))throw Error('기록 엔진 또는 릴레이 연결 준비가 진행 중입니다. 완료한 뒤 다시 확인하세요.');
      connectingManagedRelay=true;const initialGeneration=startGeneration;
      try {
        const relay=await managedRelay(),current=await relay.status();
        if(quitting||initialGeneration!==startGeneration)throw Error('현재 PC 연결을 취소했습니다.');
        if(['login','configuring','approval'].includes(current.state))throw Error('사이트 로그인·승인 또는 구성이 진행 중입니다. 먼저 사이트 준비를 마치세요.');
        if(typeof current.url!=='string'||!current.url)throw Error('먼저 릴레이 사이트 구성을 마치세요. 준비된 사이트 주소가 필요합니다.');
        relayUrl(current.url);
        const {generation}=await startOwnedEngine(false,true);
        if(quitting||generation!==startGeneration||engine.status().state!=='ready')throw Error('현재 PC 연결을 취소했습니다. 기록 엔진 상태를 확인하세요.');
        const result=await verifyManagedRelay(relay,generation);
        if(quitting||generation!==startGeneration||engine.status().state!=='ready')throw Error('현재 PC 연결 확인 중 기록 엔진이 종료됐습니다. 다시 시작한 뒤 확인하세요.');
        return result;
      } finally {connectingManagedRelay=false;}
    });
    handle('open-managed-relay-site',async()=>{
      const current=await (await managedRelay()).status();
      if(typeof current.url!=='string'||!current.url)throw Error('아직 준비된 릴레이 사이트가 없습니다. 사이트 구성을 먼저 마치세요.');
      await shell.openExternal(relayUrl(current.url));
    });
    showSetup();
    app.on('activate',showSetup);
  }).catch(()=>{dialog.showErrorBox('앱을 준비하지 못했습니다','시작 화면과 기록 엔진을 준비하지 못했습니다. 앱을 다시 실행하세요. 같은 문제가 반복되면 설치 파일을 다시 확인하세요.');app.quit();});
  app.on('window-all-closed',()=>app.quit());
  app.on('before-quit',event=>{
    if(quitting)return;
    event.preventDefault();quitting=true;++startGeneration;
    diagnostics?.log('app.quit',{version:app.getVersion(),engineState:engine?.status().state});
    bridge?.close();chat?.cancel();
    if(engine)diagnostics?.log('engine.stop',{engineState:'stopping'});
    const stopped=engine?.stop().then(result=>diagnostics?.log('engine.state',{engineState:result.state}))??Promise.resolve();
    void Promise.allSettled([stopped,cancelRelayOnQuit?.()??Promise.resolve()]).finally(async()=>{await diagnostics?.flush();app.quit();});
  });
}
