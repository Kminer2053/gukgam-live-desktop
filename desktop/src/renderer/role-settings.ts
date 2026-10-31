import type {RoleDesktopApi,RoleSettingsStatus} from '../role/contracts';
import type {EngineState} from '../shared/contracts';
const node=<T extends HTMLElement>(id:string)=>document.getElementById(id) as T;
/** 계정 입력과 CPU 모델 준비를 같은 위치에서 찾되 실제 연결 시험으로 표시하지 않는다. */
export function initializeRoleSettings(api:RoleDesktopApi,options:{engine:()=>EngineState|undefined;visible:()=>boolean}){
  let current:RoleSettingsStatus|undefined,busy=false,generation=0,selectionDirty=false;
  const choice=node<HTMLSelectElement>('role-provider'),threads=node<HTMLSelectElement>('role-threads');
  function message(value:string,error=false){const el=node('role-message');el.textContent=value;el.classList.toggle('error',error);}
  function availability(){
    const engine=options.engine()?.state??'stopped',stopped=!['starting','ready','stopping'].includes(engine),ready=engine==='ready';
    for(const id of ['jev-key','jev-key-save','jev-key-delete'])node<HTMLInputElement|HTMLButtonElement>(id).disabled=busy||!stopped;
    node('jev-key-running').hidden=stopped;
    node<HTMLSelectElement>('role-provider').disabled=busy;
    for(const id of ['role-threads','role-save','role-advanced'])node<HTMLSelectElement|HTMLButtonElement>(id).disabled=busy||!ready;
    node<HTMLButtonElement>('role-refresh').disabled=busy;
    node<HTMLButtonElement>('role-julia-prepare').disabled=busy||!ready||current?.preparing===true;
    node<HTMLButtonElement>('role-julia-cancel').disabled=busy||!ready||current?.preparing!==true;
    node('role-jev-panel').hidden=choice.value!=='jev';node('role-julia-panel').hidden=choice.value!=='julia';
    node('role-key-state').textContent=current?.hasKey?'이 앱의 보호된 Jev 키가 저장돼 있습니다.':'Jev 키가 저장되지 않았습니다.';
  }
  function apply(value:RoleSettingsStatus,fill=false){
    current=value;
    if(fill||!selectionDirty){choice.value=value.selectedProvider??choice.value??'none';threads.value=String(value.threads);selectionDirty=false;}
    const labels={none:'추가 판정 모델 없음',jev:'Jev',julia:'Julia-1 CPU'};
    node('role-saved-state').textContent=value.selectedProvider?`저장된 선택: ${labels[value.selectedProvider]}`:'기록 엔진을 켜면 저장된 선택을 확인할 수 있습니다.';
    node('role-automatic-state').textContent=value.engineReady&&(!value.enabled||!value.autoBlock)?'종료 질의 자동 검토가 꺼져 있습니다. 자동 처리·고급 설정에서 기존 동작을 확인하세요.':'선택만으로 모델을 호출하지 않습니다. 실제 처리는 기존 종료 질의 설정을 따릅니다.';
    node('role-model-state').textContent=value.model.message||'Julia 준비 상태를 확인하세요.';
    const states:Record<string,string>={idle:'미기동',loading:'불러오는 중',ready:'준비됨',running:'판정 중',error:'실패',closed:'종료됨'};
    node('role-worker-state').textContent=`작업자: ${states[value.worker.state]||'상태 확인 필요'} · ${value.worker.message}`;
    const progress=node<HTMLProgressElement>('role-model-progress');progress.hidden=!value.preparing;
    progress.value=value.model.progress??0;
    message(value.storageWarning||value.message||'키 저장과 파일 준비는 실제 판정 요청 성공을 뜻하지 않습니다.');availability();
  }
  async function refresh(fill=false){const own=++generation;const value=await api.roleSettingsStatus();if(own===generation)apply(value,fill);}
  async function action(work:()=>Promise<unknown>,success:string){
    if(busy)return;busy=true;availability();
    try{await work();await refresh();message(success);}catch(error){message(error instanceof Error?error.message:'설정을 완료하지 못했습니다.',true);}
    finally{busy=false;availability();}
  }
  choice.addEventListener('change',()=>{selectionDirty=true;availability();});
  threads.addEventListener('change',()=>{selectionDirty=true;});
  node('jev-key-save').addEventListener('click',()=>{
    const input=node<HTMLInputElement>('jev-key'),value=input.value;input.value='';
    void action(()=>api.saveJevKey(value),'Jev 키를 운영체제 보안 저장소에 저장했습니다. 다음 기록 엔진 시작 때 적용하며 실제 연결 시험은 하지 않았습니다.');
  });
  node('jev-key-delete').addEventListener('click',()=>void action(()=>api.deleteJevKey(),'이 앱에 저장한 Jev 키를 삭제했습니다. 다른 서비스의 키는 변경하지 않습니다.'));
  node('role-save').addEventListener('click',()=>void action(async()=>{await api.saveRoleSettings({provider:choice.value as 'none'|'jev'|'julia',threads:Number(threads.value) as 1|2});selectionDirty=false;},'사전 판정 선택을 저장했습니다. 기존 ChatGPT 후속 보정·요약과 수집은 유지합니다.'));
  node('role-julia-prepare').addEventListener('click',()=>void action(()=>api.prepareJulia(),'명시적으로 요청한 Julia 파일 준비를 시작했습니다. 진행 상태를 확인하세요.'));
  node('role-julia-cancel').addEventListener('click',()=>void action(()=>api.cancelJulia(),'Julia 파일 준비를 취소했습니다.'));
  node('role-advanced').addEventListener('click',()=>void action(()=>api.openRoleSettingsPage(),'기록 화면의 사전 판정 설정을 열었습니다.'));
  node('role-refresh').addEventListener('click',()=>void refresh(true).catch(()=>message('준비 상태를 확인하지 못했습니다.',true)));
  const timer=setInterval(()=>{if(options.visible()&&document.visibilityState!=='hidden'&&!busy)void refresh().catch(()=>{});},2000);
  window.addEventListener('beforeunload',()=>{clearInterval(timer);node<HTMLInputElement>('jev-key').value='';});
  return {refresh,updateAvailability:availability};
}
