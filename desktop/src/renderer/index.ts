import type {AppStatus,EngineState,SetupInput} from '../shared/contracts';
import {controls,type Task} from './state';
import {initializeRelayWizard} from './relay-wizard';
import {initializeRoleSettings} from './role-settings';
const api=window.gukgam;
const element=<T extends HTMLElement>(id:string)=>document.getElementById(id) as T;
const input=(id:string)=>element<HTMLInputElement>(id);
let step=0,state:AppStatus|undefined,initial=true,loginPending=false,refreshGeneration=0,refreshInFlight=0,engineGeneration=0;
const pending=new Set<Task>();
const supersededEngineResult=Symbol('이전 엔진 작업');
let relayWizard:ReturnType<typeof initializeRelayWizard>|undefined;
let roleSettings:ReturnType<typeof initializeRoleSettings>|undefined;
const titles=['기관명을 알려주세요','사용할 AI 연결 방식을 고르세요','이 PC에서 기록을 시작하세요','동료의 웹 열람을 준비하세요','추가 판정 모델을 준비하세요'];
function notice(message:string,error=false) {const box=element('notice');box.hidden=false;box.textContent=message;box.className=error?'error':'';}
function showStep(next:number,focus=true) {
  if(!Number.isInteger(next)||next<0||next>=titles.length)return;
  if(next===2&&!state?.setup.organization){notice('기관명을 먼저 저장한 뒤 기록 화면을 여세요.',true);input('organization').focus();return;}
  step=next;
  document.querySelectorAll<HTMLElement>('section[data-step]').forEach(section=>{section.hidden=Number(section.dataset.step)!==step;});
  document.querySelectorAll<HTMLElement>('#steps [data-step-target]').forEach(item=>{
    const selected=Number(item.dataset.stepTarget)===step;item.closest('li')?.classList.toggle('selected',selected);
    selected?item.setAttribute('aria-current','step'):item.removeAttribute('aria-current');
  });
  const heading=element('step-title');heading.textContent=titles[step];
  element('notice').hidden=true;updateButtons();if(focus)heading.focus();
  if(step===4)void roleSettings?.refresh(true).catch(()=>notice('추가 판정 설정을 확인하지 못했습니다.',true));
}
function updateButtons() {
  const availability=controls(state,pending);
  for(const id of ['save-org','account-next','save-relay'])element<HTMLButtonElement>(id).disabled=availability.settingsDisabled;
  for(const id of ['connect','test-account','disconnect'])element<HTMLButtonElement>(id).disabled=availability.accountDisabled;
  element<HTMLButtonElement>('start').disabled=availability.startDisabled;
  element<HTMLButtonElement>('start-no-ai').disabled=availability.startWithoutAiDisabled;
  for(const id of ['open-llm-settings','workspace-llm-settings'])element<HTMLButtonElement>(id).disabled=availability.startWithoutAiDisabled;
  element<HTMLButtonElement>('stop').disabled=availability.stopDisabled;
  element<HTMLButtonElement>('reopen').disabled=availability.reopenDisabled;
  element<HTMLButtonElement>('cancel-login').disabled=!loginPending;
  element<HTMLButtonElement>('back').disabled=step===0;
  element<HTMLButtonElement>('check-relay').disabled=availability.checkRelayDisabled;
  element<HTMLButtonElement>('deploy').disabled=availability.deployDisabled;
  for(const id of ['organization','relay-url','relay-token'])input(id).disabled=availability.settingsDisabled;
  element<HTMLSelectElement>('model').disabled=availability.settingsDisabled;
  element('engine-state').textContent=state?.engine.message??'기록 엔진 상태를 확인하고 있습니다.';
  element('workspace-account-note').textContent=state?.chatgpt.connected?'ChatGPT 계정이 연결돼 있습니다. 실제 사용할 AI는 기록 화면의 설정에서 선택하세요.':'ChatGPT 미연결 상태입니다. Ollama·API를 연결하거나 ChatGPT에 로그인해 AI 보정과 요약을 사용할 수 있습니다. 회의 기록과 수집은 먼저 시작할 수 있습니다.';
  relayWizard?.updateAvailability();
  roleSettings?.updateAvailability();
}
async function refresh(fill=false) {
  const generation=++refreshGeneration;++refreshInFlight;
  try {
    const next=await api.status();if(generation!==refreshGeneration)return;
    state=next;
    element('version').textContent=`버전 ${state.version}`;
    element('account-state').textContent=state.chatgpt.connected?(state.chatgpt.verified?'계정 연결 · 시험 완료':'계정 연결 · 요청 시험 전'):'ChatGPT 미연결 · 기록 사용 가능';
    element('account-detail').textContent=[state.chatgpt.account,state.chatgpt.message].filter(Boolean).join(' · ');
    element('relay-token-state').textContent=state.setup.hasRelayToken?'보호된 연결 토큰이 저장돼 있습니다.':'저장된 연결 토큰이 없습니다. 관리자 안내가 있을 때만 입력하세요.';
    const warning=element('storage-warning');warning.hidden=!state.setup.secureStorageWarning;warning.textContent=state.setup.secureStorageWarning??'';
    const model=element<HTMLSelectElement>('model'),selected=fill?(state.setup.model??''):model.value;
    model.replaceChildren();
    const defaultOption=document.createElement('option');defaultOption.value='';defaultOption.textContent='계정에서 제공하는 기본 모델';model.append(defaultOption);
    for(const name of state.chatgpt.models) {const option=document.createElement('option');option.value=name;option.textContent=name;model.append(option);}
    if(selected&&!state.chatgpt.models.includes(selected)){const option=document.createElement('option');option.value=selected;option.textContent=`${selected} · 계정 목록 재확인 필요`;model.append(option);}
    model.value=selected;
    if(initial||fill) {input('organization').value=state.setup.organization;input('relay-url').value=state.setup.relayUrl??'';initial=false;}
    updateButtons();
  }finally{--refreshInFlight;}
}
async function run(task:Task,action:()=>Promise<unknown>,success?:string) {
  if(pending.has(task))return;pending.add(task);updateButtons();let failed=false,superseded=false;
  try {superseded=await action()===supersededEngineResult;if(success&&!superseded)notice(success);} catch(error) {failed=true;notice(error instanceof Error?error.message:'작업을 완료하지 못했습니다.',true);}
  finally {pending.delete(task);try {await refresh();}catch(error){if(!failed&&!superseded)notice(error instanceof Error?error.message:'앱 상태를 확인하지 못했습니다.',true);}updateButtons();}
}
function settings(extra:Partial<SetupInput>={}):SetupInput {
  return {organization:input('organization').value,model:element<HTMLSelectElement>('model').value,relayUrl:input('relay-url').value,...extra};
}
function applyEngineResult(next:EngineState,generation:number) {
  if(generation!==engineGeneration)return;
  // 시작·종료 전 조회의 늦은 응답이 최종 상태를 덮지 않게 한다.
  ++refreshGeneration;
  if(state)state={...state,engine:next};
  updateButtons();
}
async function engineAction(action:()=>Promise<EngineState>,generation:number) {
  try {
    const next=await action();
    if(generation!==engineGeneration)return supersededEngineResult;
    applyEngineResult(next,generation);
  }catch(error){if(generation!==engineGeneration)return supersededEngineResult;throw error;}
}
async function startWorkspace() {
  showStep(2);
  if(state?.engine.state==='ready'){await api.openWorkspace();return;}
  const generation=++engineGeneration;++refreshGeneration;
  if(state)state={...state,engine:{state:'starting',message:'기록 엔진을 시작하고 있습니다. 계정 연결은 기다리지 않습니다.'}};
  updateButtons();return engineAction(()=>api.startEngine(),generation);
}
async function stopWorkspace() {
  const generation=++engineGeneration;++refreshGeneration;
  return engineAction(()=>api.stopEngine(),generation);
}
element('organization-form').addEventListener('submit',event=>{event.preventDefault();void run('settings',async()=>{await api.saveSetup(settings());await refresh(true);showStep(1);});});
element('connect').addEventListener('click',()=>void run('account',async()=>{loginPending=true;updateButtons();try{return await api.connectChatGPT();}finally{loginPending=false;}},'계정 정보를 이 PC의 보안 저장소에 보관했습니다. 연결 시험은 별도 버튼으로 실행합니다.'));
element('cancel-login').addEventListener('click',()=>void api.cancelLogin().catch(error=>notice(error.message,true)));
element('test-account').addEventListener('click',()=>void run('account',()=>api.testChatGPT(),'짧은 AI 요청 시험을 완료했습니다.'));
element('disconnect').addEventListener('click',()=>void run('account',()=>api.disconnectChatGPT(),'이 앱의 계정 연결을 해제했습니다. 회의 기록과 실황 수집은 계속 사용할 수 있습니다.'));
element('account-next').addEventListener('click',()=>void run('settings',async()=>{await api.saveSetup(settings());showStep(2);}));
element('start').addEventListener('click',()=>void run('engine-start',startWorkspace,'별도 창에 회의 기록 화면을 열었습니다.'));
element('start-no-ai').addEventListener('click',()=>void run('engine-start',startWorkspace,'AI 연결과 관계없이 회의 기록 화면을 열었습니다.'));
for(const id of ['open-llm-settings','workspace-llm-settings'])element(id).addEventListener('click',()=>void run('engine-start',async()=>{showStep(2);await api.openLlmSettingsPage();},'AI 연결 설정을 열었습니다. 연결 방식·모델을 고르고 필요한 경우 API 키를 입력하세요.'));
element('open-user-manual').addEventListener('click',()=>void run('relay-help',()=>api.openUserManual()));
element('account-return').addEventListener('click',()=>showStep(1));
element('stop').addEventListener('click',()=>void run('engine-stop',stopWorkspace,'이 앱의 기록 엔진을 종료했습니다. 저장 기록은 유지합니다.'));
element('reopen').addEventListener('click',()=>void run('engine-start',()=>api.openWorkspace()));
element('relay-next').addEventListener('click',()=>showStep(3));
for(const id of ['role-from-account','role-from-workspace'])element(id).addEventListener('click',()=>showStep(4));
element('role-records').addEventListener('click',()=>showStep(2));
element('deploy').addEventListener('click',()=>void run('relay-help',()=>api.openHelp(),'기본 브라우저에 사이트 준비 화면을 열었습니다. 표시된 절차를 계속 진행하세요.'));
element('save-relay').addEventListener('click',()=>void run('settings',async()=>{await api.saveSetup(settings({relayToken:input('relay-token').value}));input('relay-token').value='';},'릴레이 설정을 저장했습니다. 다음 기록 엔진 시작 때 적용하며 현재 PC 연결은 아직 검증하지 않았습니다.'));
element('check-relay').addEventListener('click',()=>void run('relay-check',async()=>{const result=await api.checkRelay(input('relay-url').value);element('relay-status').textContent=result.message;}));
element('back').addEventListener('click',()=>showStep(Math.max(0,step-1)));
element('refresh').addEventListener('click',()=>void run('refresh',()=>refresh(),'연결 상태를 새로 확인했습니다.'));
document.querySelectorAll<HTMLElement>('[data-step-target]').forEach(button=>button.addEventListener('click',()=>showStep(Number(button.dataset.stepTarget))));
relayWizard=initializeRelayWizard(api,{engine:()=>state?.engine,settingsChanged:()=>refresh(true)});
roleSettings=initializeRoleSettings(api,{engine:()=>state?.engine,visible:()=>step===4});
// 최초·명시 저장 조회를 포함해 진행 중인 조회를 자동 조회가 앞지르지 않게 한다.
const refreshVisible=()=>{if(document.visibilityState!=='hidden'&&!refreshInFlight)void refresh().catch(()=>{});};
const timer=setInterval(refreshVisible,2000);
document.addEventListener('visibilitychange',refreshVisible);
window.addEventListener('focus',refreshVisible);
window.addEventListener('beforeunload',()=>clearInterval(timer));
void refresh(true).then(()=>showStep(state?.setup.organization?2:0,false)).catch(error=>notice(error.message,true));
