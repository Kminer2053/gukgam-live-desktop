import {JevKeyStore} from '../storage/jev-key';
import type {RoleChoice,RoleSelection,RoleSettingsStatus} from '../role/contracts';
import type {EngineState} from '../shared/contracts';
import {ENGINE_ORIGIN} from './security';
type Vault=ConstructorParameters<typeof JevKeyStore>[0];
type Engine={status():EngineState;sendSensitive(value:unknown,secret:string):void};
const choices=new Set(['none','jev','julia']);
const text=(value:unknown,fallback='')=>typeof value==='string'?value.replace(/[\x00-\x1f]/g,' ').slice(0,400):fallback;
export function validateRoleSelection(value:unknown):RoleSelection{
  if(!value||typeof value!=='object'||Array.isArray(value))throw Error('사전 판정 방식을 선택하세요.');
  const row=value as Record<string,unknown>;
  if(Object.keys(row).some(key=>!['provider','threads'].includes(key))||typeof row.provider!=='string'||!choices.has(row.provider)||(row.threads!==1&&row.threads!==2))throw Error('사전 판정 방식과 CPU 스레드 1개 또는 2개를 선택하세요.');
  return {provider:row.provider as RoleChoice,threads:row.threads};
}
/** 소유한 준비 완료 엔진의 고정 설정 경로만 호출한다. 키는 HTTP로 보내지 않는다. */
export class RoleSettingsService{
  private keys:JevKeyStore;
  private syncWarning='';
  constructor(vault:Vault,private engine:Engine,private fetcher:typeof fetch=fetch){this.keys=new JevKeyStore(vault);}
  private assertStopped(){if(['starting','ready','stopping'].includes(this.engine.status().state))throw Error('Jev 키를 바꾸려면 수집을 마친 뒤 기록 엔진을 종료하세요. 현재 키와 수집은 유지합니다.');}
  private async request(path:string,method='GET',body?:unknown):Promise<Record<string,any>>{
    if(this.engine.status().state!=='ready')throw Error('기록 엔진을 먼저 켜세요. 키 저장은 엔진을 종료한 상태에서 할 수 있습니다.');
    try{
      const response=await this.fetcher(`${ENGINE_ORIGIN}/api/${path}`,{method,redirect:'error',signal:AbortSignal.timeout(5000),
        headers:{'X-Requested-With':'gukgam',...(body===undefined?{}:{'Content-Type':'application/json'})},
        ...(body===undefined?{}:{body:JSON.stringify(body)})});
      if(!response.ok)throw Error('http');
      const value=await response.json();if(!value||typeof value!=='object'||Array.isArray(value))throw Error('shape');
      return value;
    }catch{throw Error('사전 판정 설정을 읽거나 저장하지 못했습니다. 기록 엔진 상태를 확인하세요.');}
  }
  async syncKey():Promise<void>{
    try{const value=await this.keys.configurationForEngine();this.engine.sendSensitive({type:'jev.configure',...value},value.key);this.syncWarning='';}
    catch{this.syncWarning='보호된 Jev 키를 적용하지 못했습니다. 기존 ChatGPT 처리로 계속합니다.';try{this.engine.sendSensitive({type:'jev.configure',key:'',revision:'unconfigured'},'');}catch{/* 선택 키 오류가 로컬 기록 시작을 막지 않는다. */}}
  }
  async saveKey(value:unknown){this.assertStopped();const result=await this.keys.save(value);this.syncWarning='';return result;}
  async deleteKey(){this.assertStopped();const result=await this.keys.delete();this.syncWarning='';return result;}
  async status():Promise<RoleSettingsStatus>{
    const key=await this.keys.status();
    const base:RoleSettingsStatus={...key,engineReady:this.engine.status().state==='ready',message:this.syncWarning||key.storageWarning||'',
      selectedProvider:null,threads:2,enabled:false,autoBlock:false,preparing:false,
      model:{state:'unknown',message:'기록 엔진을 켠 뒤 준비 상태를 확인하세요.',available:false},worker:{state:'idle',message:'미기동',available:false}};
    if(!base.engineReady)return {...base,message:base.message||'Jev 키는 여기서 저장할 수 있습니다. 판정 방식 선택과 Julia 준비는 기록 엔진을 켠 뒤 사용하세요.'};
    try{
      const value=await this.request('settings/role-decision/status'),model=value.model||{},worker=value.worker||{};
      const selected=choices.has(value.selected_provider)?value.selected_provider as RoleChoice:null;
      return {...base,selectedProvider:selected,threads:value.cpu_threads===1?1:2,enabled:value.enabled===true,autoBlock:value.auto_block===true,
        preparing:value.preparing===true,message:base.message||text(value.prepare_error),
        model:{state:text(model.state,'unknown'),message:text(model.message),available:model.available===true,
          ...(typeof model.progress==='number'&&Number.isFinite(model.progress)?{progress:Math.max(0,Math.min(1,model.progress))}:{})},
        worker:{state:text(worker.state,'idle'),message:text(worker.message),available:worker.available===true}};
    }catch{return {...base,message:'사전 판정 상태를 확인하지 못했습니다. 기록과 수집은 유지합니다.'};}
  }
  async saveSelection(value:unknown){const input=validateRoleSelection(value);await this.request('settings/transcript_review','PUT',{role_decision_provider:input.provider,julia_threads:input.threads});return this.status();}
  async prepareJulia(){await this.request('settings/role-decision/julia/prepare','POST',{confirm:true});return this.status();}
  async cancelJulia(){await this.request('settings/role-decision/julia/cancel','POST');return this.status();}
}
