import type {EngineState} from '../shared/contracts';
import type {ManagedRelayStatus,RelayCatalog,RelayDesktopApi,RelayField,RelayPlan,RelayProvisionInput} from '../relay/contracts';
import {validateRelayForm} from './relay-form';
type Action='login'|'catalog'|'plans'|'provision'|'resume'|'cancel'|'approval'|'verify'|'pricing'|'site'|'update';
const byId=<T extends HTMLElement>(id:string)=>document.getElementById(id) as T;
const select=(id:string)=>byId<HTMLSelectElement>(id);
const input=(id:string)=>byId<HTMLInputElement>(id);
// 현재 앱은 자체 로그인과 실시간 전달을 사용한다. Neon 부가 기능은 선택받지 않는다.
const unusedNeonAddon=(field:RelayField)=>field.type==='boolean'&&['auth','realtime'].includes(field.key);
/** 실제 메인 상태만 표시하며 계정·배포·현재 PC 연결을 별도로 구분한다. */
export function initializeRelayWizard(api:RelayDesktopApi,options:{engine:()=>EngineState|undefined;settingsChanged:()=>Promise<void>}) {
  let status:ManagedRelayStatus|undefined,catalog:RelayCatalog={products:[]},plans:RelayPlan[]=[],catalogTeam='',teamSignature='',poll:ReturnType<typeof setTimeout>|undefined,generation=0;
  let connectionResult:{text:string;error:boolean}|undefined;
  const pending=new Set<Action>(),fields=new Map<string,{field:RelayField;control:HTMLInputElement|HTMLSelectElement}>();
  function message(text:string,error=false) {const node=byId('managed-relay-message');node.textContent=text;node.classList.toggle('error',error);}
  /** 업데이트 결과와 원본 연결은 서로의 검증 결과를 대신하지 않는다. */
  function siteUpdateNotice() {
    const update=status?.siteUpdate;
    if(pending.has('update')||update?.state==='updating') {
      const stages={unverified:'사이트 업데이트 진행 중',preparing:'사이트 파일 준비 중',deploying:'사이트 배포 중',binding:'사이트 주소 연결 중',verifying:'사이트 로그인·저장공간 확인 중',saving:'확인된 사이트 주소 저장 중',completed:'사이트 업데이트 진행 중'};
      const stage=update?.stage??'preparing';
      return {title:Object.hasOwn(stages,stage)?stages[stage]:'사이트 업데이트 진행 중',help:'기존 프로젝트의 사이트 화면을 업데이트하고 있습니다. 작업이 끝날 때까지 기다리세요. 현재 PC의 원본 연결은 완료 후 별도로 확인합니다.',attention:false};
    }
    if(update?.state==='unknown')return {title:'사이트 업데이트 결과 미확인',help:'새 배포가 이미 생성됐을 수 있습니다. Vercel 공식 배포 내역을 직접 확인한 뒤 필요한 경우 다시 업데이트하세요. 앱은 자동으로 재배포하지 않습니다. 현재 PC의 원본 연결 성공과 사이트 코드 업데이트 확인은 별개입니다.',attention:true};
    if(update?.state==='failed')return {title:'사이트 업데이트 완료되지 않음',help:'사이트 업데이트를 완료하지 못했습니다. 기존 사이트 주소를 확인하고, 다시 시도하려면 기록 엔진을 종료한 뒤 업데이트 버튼을 누르세요. 현재 PC의 원본 연결은 별도로 확인할 수 있습니다.',attention:true};
    if(update?.state==='cancelled')return {title:'사이트 업데이트 취소됨',help:'업데이트 작업이 취소됐습니다. 배포를 시작한 뒤 취소했다면 새 배포가 생성됐을 수 있으므로 공식 배포 내역을 먼저 확인하세요. 현재 PC의 원본 연결은 별도로 확인할 수 있습니다.',attention:true};
    // 공개 상태의 고정 버전 형식만 표시한다. 임의 문자열·진단 코드·명령 출력은 사용하지 않는다.
    const version=update?.version;
    if(update?.state==='succeeded'&&update.stage==='completed'&&typeof version==='string'&&/^\d{1,4}\.\d{1,4}\.\d{1,4}(?:-preview\.\d{1,6})?$/.test(version)&&update.targetVersion===version)return {title:`사이트 코드 업데이트 확인 완료 · ${version}`,help:'업데이트된 사이트의 로그인·저장공간과 저장된 주소를 확인했습니다. 현재 PC의 원본 연결은 위 연결 버튼으로 별도로 확인하세요.',attention:false};
    return {title:'사이트 코드 버전 미확인',help:'이 주소에서 확인된 사이트 코드 버전이 없습니다. 기존 사이트에 버전 표시가 없어 확인하지 못할 수도 있습니다. 현재 PC의 원본 연결이 성공해도 사이트 코드가 최신이라는 뜻은 아닙니다.',attention:false};
  }
  function resumableSetup() {
    // 배포 완료 사이트의 업데이트는 새 사이트 구성의 약관 승인·재개와 구분한다.
    const update=status?.siteUpdate;
    return Boolean(status?.canResume&&!(status.url&&update&&update.state!=='unverified'));
  }
  function requiresSetupApproval() {return resumableSetup()&&Boolean(status?.approvalUrl);}
  function renderSiteUpdate() {
    const notice=siteUpdateNotice();
    byId('relay-site-update-state').textContent=notice.title;
    byId('relay-site-update-help').textContent=notice.help;
    byId('relay-site-update-state').classList.toggle('warning',notice.attention);
  }
  function renderConnection() {
    const engineState=options.engine()?.state??'stopped',checking=pending.has('verify'),updating=pending.has('update')||status?.siteUpdate?.state==='updating';
    const button=byId('relay-verify'),progress=byId('relay-connection-progress');
    byId('relay-ready-actions').hidden=!status?.url;
    if(updating)byId('managed-relay-state').textContent='기존 사이트 업데이트 중';
    else if(checking)byId('managed-relay-state').textContent='현재 PC 연결 확인 중';
    else if(status?.state==='ready')byId('managed-relay-state').textContent=status.verified&&status.sourceOnline&&engineState==='ready'?'현재 PC의 원본 연결 검증 완료':status.verified?'사이트 검증 완료 · 현재 PC 연결 대기':'사이트 준비됨 · 현재 PC 연결 미검증';
    else if(status)byId('managed-relay-state').textContent=({idle:'사이트 계정 미설정',login:'Vercel 로그인 진행 중',connected:'사이트 계정 연결 · 구성 대기',configuring:'사이트 구성 진행 중',approval:requiresSetupApproval()?'외부 서비스 승인 대기':resumableSetup()?'사이트 구성 이어가기':'사이트 구성 확인 필요',error:status.siteUpdate?.state==='unknown'?'사이트 업데이트 결과 미확인':'사이트 구성 확인 필요'})[status.state];
    button.textContent=engineState==='starting'?'기록 엔진 시작 중…':engineState==='stopping'?'기록 엔진 종료 중…':checking?(engineState==='ready'?'현재 PC 연결 확인 중…':'기록 엔진 시작·연결 확인 중…'):engineState==='ready'?'현재 PC 연결 확인':'기록 엔진 시작 후 연결 확인';
    let text='',error=false;
    if(updating)text='기존 프로젝트의 사이트 코드를 업데이트하고 있습니다. DB와 암호는 유지하며 완료 후 새 주소와 현재 PC 연결을 확인하세요.';
    else if(checking)text=engineState==='starting'?'기록 엔진을 시작하고 있습니다. 시작이 끝나면 이 PC의 연결을 확인합니다.':engineState==='ready'?'사이트 로그인과 현재 PC의 연결, 실제 회의 목록 조회를 확인하고 있습니다.':'사이트 준비 상태를 확인한 뒤 기록 엔진을 시작하고 이 PC의 연결을 확인합니다.';
    else if(engineState==='starting')text='기록 엔진이 시작되고 있습니다. 시작이 끝나면 연결 버튼을 사용할 수 있습니다.';
    else if(engineState==='stopping')text='기록 엔진이 종료되고 있습니다. 종료가 끝난 뒤 연결 버튼을 누르세요.';
    else if(connectionResult){text=connectionResult.text;error=connectionResult.error;}
    else if(status?.state==='error'&&status.url){text=siteUpdateNotice().attention?'현재 PC의 원본 연결은 별도로 확인하세요. 위 연결 버튼으로 기록 엔진을 시작하고 저장 기록 조회를 확인할 수 있습니다.':status.message;error=true;}
    else if(status?.url)text=status.sourceOnline&&engineState==='ready'?'현재 PC의 연결이 확인됐습니다. 사이트를 열어 저장 기록을 확인하세요.':engineState==='ready'?'기록 엔진이 켜져 있습니다. 연결 버튼을 눌러 이 PC의 기록이 사이트에서 조회되는지 확인하세요.':'사이트가 준비됐습니다. 연결 버튼을 누르면 기록 엔진 시작과 현재 PC 연결 확인을 차례로 진행합니다.';
    if(!checking&&!updating&&status?.sourceOnline&&engineState!=='ready'&&!['starting','stopping'].includes(engineState)){text='기록 엔진이 꺼져 있어 현재 PC 연결을 다시 확인해야 합니다. 연결 버튼을 누르면 기록 엔진을 시작하고 확인합니다.';error=false;}
    progress.textContent=text;progress.classList.toggle('error',error);
    byId('relay-connection-help').textContent='연결 버튼은 이 PC의 기록 엔진을 필요한 경우 시작한 뒤, 저장 기록을 사이트에서 조회할 수 있는지 확인합니다. 기록 화면으로 따로 이동할 필요는 없습니다.';
  }
  function refreshButtons() {
    const changing=pending.has('provision')||pending.has('resume')||pending.has('update')||status?.state==='configuring'||status?.siteUpdate?.state==='updating';
    const loggingIn=pending.has('login')||status?.state==='login';
    const querying=changing||loggingIn||pending.has('verify')||pending.has('catalog')||pending.has('plans')||pending.has('cancel');
    const engineState=options.engine()?.state??'stopped',running=['starting','ready','stopping'].includes(engineState);
    const canResume=resumableSetup(),needsApproval=requiresSetupApproval(),canOpenApproval=Boolean(status?.approvalUrl&&(loggingIn||needsApproval));
    const approvalPanel=byId('relay-approval-panel'),approvalSlot=byId(loggingIn?'relay-login-approval-slot':'relay-configuration-approval-slot');
    if(approvalPanel.parentElement!==approvalSlot) {
      const focused=!approvalPanel.hidden&&approvalPanel.contains(document.activeElement)?document.activeElement as HTMLElement|null:null;
      approvalSlot.append(approvalPanel);focused?.focus({preventScroll:true});
    }
    byId<HTMLButtonElement>('relay-login').disabled=querying;
    byId('relay-login').textContent=loggingIn?'로그인 진행 중':status?.state==='error'&&!status.canResume&&!status.teams.length?'Vercel 로그인 재시도':'Vercel 로그인';
    byId('relay-login-help').hidden=!loggingIn;
    byId('relay-login-help').textContent=status?.approvalUrl?'브라우저가 열리지 않으면 아래 ‘승인 페이지 열기’를 누르세요. 다시 시작하려면 ‘로그인 취소’를 누른 뒤 로그인하세요.':'승인 페이지를 준비하고 있습니다. 기다려도 열리지 않으면 ‘로그인 취소’를 누른 뒤 다시 로그인하세요.';
    byId<HTMLButtonElement>('relay-load-catalog').disabled=!select('relay-team').value||querying;
    byId<HTMLButtonElement>('relay-load-plans').disabled=!select('relay-product').value||querying;
    byId<HTMLButtonElement>('relay-neon-pricing').disabled=pending.has('pricing');
    byId<HTMLButtonElement>('relay-vercel-pricing').disabled=pending.has('pricing');
    byId<HTMLButtonElement>('relay-provision').disabled=running||changing||querying||!plans.length||!select('relay-plan').value||!status?.teams.length||Boolean(status?.projectName&&(status?.canResume||status?.url));
    byId<HTMLButtonElement>('relay-resume').disabled=running||querying||!canResume;
    byId<HTMLButtonElement>('relay-open-approval').disabled=!canOpenApproval||pending.has('approval')||pending.has('cancel');
    byId('relay-open-approval').hidden=!canOpenApproval;
    byId('relay-approval-panel').hidden=!(canOpenApproval||canResume);
    byId('relay-approval-title').textContent=loggingIn?'Vercel 로그인 승인':needsApproval?'승인 또는 이어서 구성':'사이트 구성 이어가기';
    byId('relay-approval-help').textContent=loggingIn?'Vercel 공식 페이지에서 로그인을 완료하세요. 페이지가 열리지 않으면 아래 버튼으로 다시 열 수 있습니다.':needsApproval?'브라우저에서 약관·요금제 승인을 확인하세요. 필요한 승인을 마친 뒤 확인란을 선택하고 구성을 이어가세요.':'완료되지 않은 사이트 구성을 기존 설정으로 이어갑니다. 새 사이트·저장공간을 중복으로 만들지 않으며, 외부 승인이 필요한 경우 승인 페이지를 안내합니다.';
    byId('relay-approval-confirm-row').hidden=loggingIn||!needsApproval;
    byId('relay-resume').hidden=loggingIn||!canResume;
    byId('relay-resume').textContent=needsApproval?'승인 후 이어서 구성':'이어서 구성';
    byId<HTMLButtonElement>('relay-cancel').disabled=pending.has('cancel')||(!changing&&!pending.has('login')&&!pending.has('catalog')&&!pending.has('plans')&&status?.state!=='login'&&status?.state!=='approval');
    byId('relay-cancel').textContent=loggingIn?'로그인 취소':'사이트 준비 작업 취소';
    byId<HTMLButtonElement>('relay-verify').disabled=querying||status?.state==='approval'||!status?.url||['starting','stopping'].includes(engineState);
    byId<HTMLButtonElement>('relay-open-site').disabled=!status?.url||pending.has('site');
    byId<HTMLButtonElement>('relay-open-site-address').disabled=!status?.url||pending.has('site');
    byId<HTMLButtonElement>('relay-update-site').disabled=running||querying||status?.state==='approval'||!status?.url;
    byId('relay-update-site').textContent=pending.has('update')||status?.siteUpdate?.state==='updating'?'기존 사이트 업데이트 중…':status?.siteUpdate?.state==='unknown'?'배포 내역 확인 후 다시 업데이트':'기존 사이트 업데이트';
    byId('relay-engine-warning').hidden=!running||Boolean(status?.url);
    select('relay-team').disabled=querying;select('relay-product').disabled=querying;select('relay-plan').disabled=querying||!plans.length;
    for(const {control} of fields.values())control.disabled=querying;
    renderConnection();renderSiteUpdate();
  }
  function renderTeams() {
    const signature=JSON.stringify(status?.teams??[]);if(signature===teamSignature)return;teamSignature=signature;
    const node=select('relay-team'),previous=node.value;node.replaceChildren();
    const empty=document.createElement('option');empty.value='';empty.textContent='Vercel 로그인 뒤 팀을 선택하세요';node.append(empty);
    for(const team of status?.teams??[]) {const option=document.createElement('option');option.value=team.id;option.textContent=team.name||team.slug;node.append(option);}
    node.value=(status?.teams??[]).some(team=>team.id===previous)?previous:(status?.teamId??(status?.teams.length===1?status.teams[0].id:''));
  }
  function applyStatus(next:ManagedRelayStatus) {
    if(next.url!==status?.url||connectionResult&&!connectionResult.error&&(!next.sourceOnline||next.state==='error'))connectionResult=undefined;
    if(next.state==='approval'&&(status?.state!=='approval'||next.approvalUrl!==status.approvalUrl))input('relay-approval-confirm').checked=false;
    status=next;renderTeams();
    const labels={idle:'사이트 계정 미설정',login:'Vercel 로그인 진행 중',connected:'사이트 계정 연결 · 구성 대기',configuring:'사이트 구성 진행 중',approval:requiresSetupApproval()?'외부 서비스 승인 대기':resumableSetup()?'사이트 구성 이어가기':'사이트 구성 확인 필요',ready:next.verified?(next.sourceOnline?'현재 PC의 원본 연결 검증 완료':'사이트 검증 완료 · 현재 PC 연결 대기'):'사이트 준비됨 · 현재 PC 연결 미검증',error:'사이트 구성 확인 필요'};
    const notice=siteUpdateNotice();
    let text=next.message,error=next.state==='error';
    // 지난 업데이트 결과는 별도 영역에 남기며 현재 계정 승인·연결의 다음 행동을 가리지 않는다.
    if(next.siteUpdate&&next.siteUpdate.state!=='unverified'){
      if(pending.has('update')||next.state==='configuring'){text=`${notice.title}. ${notice.help}`;error=error||notice.attention;}
      else if(next.state==='login')text=next.approvalUrl?'Vercel 공식 페이지에서 계정 연결을 승인하세요. 브라우저가 열리지 않으면 아래 ‘승인 페이지 열기’를 누르세요.':'Vercel 로그인 승인 페이지를 준비하고 있습니다. 아래 로그인 안내를 확인하세요.';
      else if(next.state==='connected')text=resumableSetup()?'완료되지 않은 사이트 구성이 있습니다. 아래 ‘이어서 구성’ 버튼으로 기존 설정을 이어가세요.':'Vercel 계정을 연결했습니다. 기관에서 사용할 팀과 사이트 설정을 확인하세요.';
      else if(next.state==='approval')text=requiresSetupApproval()?'브라우저에서 필요한 약관·요금제 승인을 마친 뒤 아래 확인란을 선택하고 구성을 이어가세요.':resumableSetup()?'완료되지 않은 사이트 구성을 아래 ‘이어서 구성’ 버튼으로 이어가세요.':'현재 승인할 외부 서비스 화면이 확인되지 않았습니다. 사이트 업데이트 상태를 별도로 확인하세요.';
      else if(next.state==='ready')text=next.verified&&next.sourceOnline&&options.engine()?.state==='ready'?'현재 PC의 원본 연결과 실제 회의 목록 조회를 확인했습니다. 사이트 화면 업데이트 상태는 별도 영역에서 확인하세요.':'사이트 주소가 준비됐습니다. 위 연결 버튼으로 현재 PC의 기록 엔진과 저장 기록 조회를 확인하세요.';
      else if(next.state==='idle')text='사이트 계정에 로그인하면 기관의 사이트 설정을 확인할 수 있습니다. 이전 사이트 업데이트 상태는 별도 영역에서 확인하세요.';
      else{text=`${notice.title}. ${notice.help}`;error=error||notice.attention;}
    }
    byId('managed-relay-state').textContent=labels[next.state];message(text,error);
    byId('relay-created-url-row').hidden=!next.url;input('relay-created-url').value=next.url??'';
    if(next.projectName)input('relay-project-name').value=next.projectName;
    refreshButtons();
    if(poll)clearTimeout(poll);poll=undefined;
    if(['login','configuring'].includes(next.state)||['login','provision','resume','verify','update'].some(name=>pending.has(name as Action)))poll=setTimeout(()=>void refresh().catch(()=>message('사이트 구성 상태를 읽지 못했습니다. 잠시 뒤 다시 확인하세요.',true)),1500);
    if(next.state==='connected'&&select('relay-team').value&&catalogTeam!==select('relay-team').value&&!pending.has('catalog'))void loadCatalog();
  }
  async function refresh() {const current=++generation;const next=await api.relayStatus();if(current===generation)applyStatus(next);}
  async function action(name:Action,work:()=>Promise<unknown>) {
    if(pending.has(name))return;pending.add(name);refreshButtons();
    let completed=false;
    try {const workPromise=work();void workPromise.catch(()=>{});if(['login','provision','resume','verify','update'].includes(name))void refresh().catch(()=>{});await workPromise;await refresh();completed=true;} catch(error) {const notice=siteUpdateNotice();message(name==='update'?(notice.attention?`${notice.title}. ${notice.help}`:'사이트 업데이트를 완료하지 못했습니다. 상태를 확인한 뒤 다시 시도하세요.'):name==='verify'&&notice.attention?'현재 PC의 원본 연결을 확인하지 못했습니다. 기록 엔진 상태를 확인한 뒤 다시 시도하세요.':error instanceof Error?error.message:'사이트 구성을 완료하지 못했습니다.',true);}
    finally {pending.delete(name);refreshButtons();if(name==='update'&&completed&&status?.siteUpdate){const notice=siteUpdateNotice();message(`${notice.title}. ${notice.help}`,status.state==='error'||notice.attention);}}
  }
  function clearPlans() {plans=[];select('relay-plan').replaceChildren();input('relay-consent').checked=false;byId('relay-plan-description').textContent='종류와 조건을 선택한 뒤 이용 가능한 요금제를 확인하세요.';refreshButtons();}
  function renderFields() {
    fields.clear();byId('relay-product-fields').replaceChildren();clearPlans();
    const product=catalog.products.find(item=>item.id===select('relay-product').value);if(!product)return;
    for(const field of product.fields) {
      if(unusedNeonAddon(field))continue;
      const wrap=document.createElement('div'),label=document.createElement('label');label.textContent=field.label+(field.required?' (필수)':'');
      const control=field.type!=='boolean'&&field.options?.length?document.createElement('select'):document.createElement('input');
      control.id=`relay-field-${fields.size}`;label.htmlFor=control.id;control.required=field.required&&field.type!=='boolean';
      if(control instanceof HTMLSelectElement) {
        control.multiple=field.type==='array';
        for(const value of field.options??[]) {const option=document.createElement('option');option.value=value;option.textContent=value;option.selected=Array.isArray(field.default)?field.default.includes(value):field.default===value;control.append(option);}
      } else {
        control.type=field.type==='boolean'?'checkbox':field.type==='number'?'number':'text';
        if(field.type==='boolean')control.checked=field.default===true;
        else control.value=Array.isArray(field.default)?field.default.join(', '):field.default===undefined?'':String(field.default);
        if(field.type==='number')control.step='any';
        if(field.type==='array')control.placeholder='여러 값은 쉼표로 구분하세요';
      }
      wrap.append(label,control);
      if(field.description){const hint=document.createElement('p');hint.className='hint';hint.textContent=field.description;wrap.append(hint);}
      byId('relay-product-fields').append(wrap);fields.set(field.key,{field,control});
      control.addEventListener('change',clearPlans);control.addEventListener('input',clearPlans);
    }
  }
  function metadata():Record<string,unknown> {
    const result:Record<string,unknown>={};
    const product=catalog.products.find(item=>item.id===select('relay-product').value);
    for(const field of product?.fields??[])if(unusedNeonAddon(field))result[field.key]=false;
    for(const [key,{field,control}] of fields) {
      if(field.type==='boolean'){result[key]=(control as HTMLInputElement).checked;continue;}
      const raw=control.value.trim();
      if(!raw){if(field.required)throw Error(`${field.label} 항목을 입력하거나 선택하세요.`);continue;}
      if(field.type==='number'){const value=Number(raw);if(!Number.isFinite(value))throw Error(`${field.label} 항목은 숫자로 입력하세요.`);result[key]=value;}
      else if(field.type==='array')result[key]=control instanceof HTMLSelectElement?Array.from(control.selectedOptions,option=>option.value):raw.split(',').map(value=>value.trim()).filter(Boolean);
      else result[key]=raw;
    }
    return result;
  }
  async function loadCatalog() {
    const teamId=select('relay-team').value;if(!teamId)return;
    await action('catalog',async()=>{
      const result=await api.relayCatalog(teamId);if(select('relay-team').value!==teamId)return;
      catalog=result;catalogTeam=teamId;select('relay-product').replaceChildren();
      for(const product of catalog.products) {const option=document.createElement('option');option.value=product.id;option.textContent=product.name;select('relay-product').append(option);}
      renderFields();if(!catalog.products.length)message('이 팀에서 구성할 수 있는 저장공간이 없습니다. 계정 권한과 기관 정책을 확인하세요.',true);
    });
  }
  byId('relay-login').addEventListener('click',()=>void action('login',async()=>{await api.connectRelayAccount();}));
  byId('relay-neon-pricing').addEventListener('click',()=>void action('pricing',()=>api.openRelayPricing('neon')));
  byId('relay-vercel-pricing').addEventListener('click',()=>void action('pricing',()=>api.openRelayPricing('vercel')));
  byId('relay-load-catalog').addEventListener('click',()=>void loadCatalog());
  select('relay-team').addEventListener('change',()=>{catalogTeam='';catalog={products:[]};select('relay-product').replaceChildren();renderFields();void loadCatalog();});
  select('relay-product').addEventListener('change',renderFields);
  byId('relay-load-plans').addEventListener('click',()=>void action('plans',async()=>{
    plans=await api.relayPlans({teamId:select('relay-team').value,productId:select('relay-product').value,metadata:metadata()});select('relay-plan').replaceChildren();
    const empty=document.createElement('option');empty.value='';empty.textContent='요금제를 직접 선택하세요';select('relay-plan').append(empty);
    for(const plan of plans){const option=document.createElement('option');option.value=plan.id;option.textContent=plan.name;select('relay-plan').append(option);}
    describePlan();if(!plans.length)message('선택한 조건에서 이용 가능한 요금제가 없습니다. 다른 조건을 선택하세요.',true);
  }));
  function describePlan() {const plan=plans.find(item=>item.id===select('relay-plan').value);byId('relay-plan-description').textContent=plan?[plan.name,plan.description||'요금제 상세 조건을 확인하지 못했습니다. 공식 가격 안내와 승인 화면에서 확인하세요.'].join('\n'):'사용할 Neon DB 요금제를 직접 선택하세요.';}
  select('relay-plan').addEventListener('change',()=>{input('relay-consent').checked=false;describePlan();refreshButtons();});
  input('relay-project-name').addEventListener('input',()=>{input('relay-consent').checked=false;});
  byId('managed-relay-form').addEventListener('submit',event=>{
    event.preventDefault();void action('provision',async()=>{
      const value:RelayProvisionInput=validateRelayForm({teamId:select('relay-team').value,projectName:input('relay-project-name').value.trim(),productId:select('relay-product').value,planId:select('relay-plan').value,metadata:metadata(),operatorPasscode:input('relay-operator-passcode').value,viewerPasscode:input('relay-viewer-passcode').value,consent:input('relay-consent').checked});
      const request=api.provisionRelay(value);input('relay-operator-passcode').value='';input('relay-viewer-passcode').value='';await request;await options.settingsChanged();
    });
  });
  byId('relay-open-approval').addEventListener('click',()=>void action('approval',()=>api.openRelayApproval()));
  byId('relay-resume').addEventListener('click',()=>{if(!resumableSetup())return;void action('resume',async()=>{if(requiresSetupApproval()&&!input('relay-approval-confirm').checked)throw Error('브라우저에서 약관·요금제 승인을 마친 뒤 확인란을 선택하세요.');await api.resumeRelay();await options.settingsChanged();});});
  byId('relay-cancel').addEventListener('click',()=>void action('cancel',()=>api.cancelRelay()));
  byId('relay-update-site').addEventListener('click',()=>void action('update',async()=>{
    connectionResult=undefined;await api.updateManagedRelaySite();await options.settingsChanged();
  }));
  byId('relay-verify').addEventListener('click',()=>{
    if(pending.has('verify'))return;
    connectionResult=undefined;
    void action('verify',async()=>{
      try{
        const next=await api.connectManagedRelay();applyStatus(next);await options.settingsChanged();
        connectionResult={text:next.verified&&next.sourceOnline?'현재 PC 연결 확인 완료. 이 PC의 연결과 사이트를 통한 실제 회의 목록 조회를 확인했습니다.':siteUpdateNotice().attention?'현재 PC의 원본 연결을 확인하지 못했습니다. 기록 엔진 상태를 확인한 뒤 연결 버튼으로 다시 시도하세요.':next.message,error:next.state==='error'||!next.sourceOnline};
      }catch(error){connectionResult={text:siteUpdateNotice().attention?'현재 PC의 원본 연결을 확인하지 못했습니다. 기록 엔진 상태를 확인한 뒤 다시 시도하세요.':error instanceof Error?error.message:'현재 PC 연결을 확인하지 못했습니다. 잠시 뒤 다시 시도하세요.',error:true};throw error;}
      finally{if(document.activeElement===byId('relay-verify'))byId('relay-connection-progress').scrollIntoView({block:'nearest'});}
    });
  });
  for(const id of ['relay-open-site','relay-open-site-address'])byId(id).addEventListener('click',()=>void action('site',()=>api.openManagedRelaySite()));
  input('relay-project-name').value=`gukgam-relay-${Date.now().toString(36).slice(-6)}`;
  window.addEventListener('beforeunload',()=>{if(poll)clearTimeout(poll);});
  void refresh().catch(error=>message(error instanceof Error?error.message:'사이트 구성 상태를 읽지 못했습니다.',true));
  return {updateAvailability:refreshButtons,refresh};
}
