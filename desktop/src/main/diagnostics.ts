import {appendFile,mkdir,rename,rm,stat,truncate} from 'node:fs/promises';
import {join} from 'node:path';
import type {AiRequestStatus} from '../shared/contracts';
import {siteUpdateStatus} from '../relay/contracts';

type Event='app.start'|'app.quit'|'engine.start'|'engine.stop'|'engine.stopped'|'engine.state'|'chatgpt.request'|'diagnostics.dropped'|'relay.site-update';
type Metadata={request_id?:unknown;state?:unknown;model?:unknown;startedAt?:unknown;updatedAt?:unknown;receivedChars?:unknown;durationMs?:unknown;version?:unknown;engineState?:unknown;siteUpdate?:unknown};
export type RequestDiagnostic=Omit<AiRequestStatus,'message'>;
const EVENTS=new Set<Event>(['app.start','app.quit','engine.start','engine.stop','engine.stopped','engine.state','chatgpt.request','diagnostics.dropped','relay.site-update']);
const STATES=new Set(['preparing','connecting','complete','cancelled','failed']);
const ENGINE_STATES=new Set(['stopped','starting','ready','stopping','error']);
const MAX_BYTES=1_048_576,MAX_PENDING=64;

export function diagnosticEntry(event:Event,metadata:Metadata={},now=Date.now()):Record<string,unknown>|undefined {
  if(!EVENTS.has(event))return;
  const output:Record<string,unknown>={time:new Date(now).toISOString(),event};
  if(event==='relay.site-update')return {...output,...siteUpdateStatus(metadata.siteUpdate)};
  if(typeof metadata.request_id==='string'&&/^[A-Za-z0-9_-]{1,80}$/.test(metadata.request_id)&&!/^sk-|^eyJ/i.test(metadata.request_id))output.request_id=metadata.request_id;
  if(typeof metadata.state==='string'&&STATES.has(metadata.state))output.state=metadata.state;
  // 모델명에도 이메일·URL·자격증명처럼 보이는 값을 허용하지 않는다.
  if(typeof metadata.model==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/.test(metadata.model)&&!/^sk-|^eyJ|^Bearer/i.test(metadata.model))output.model=metadata.model;
  for(const key of ['startedAt','updatedAt','receivedChars','durationMs'] as const){
    const value=metadata[key];if(typeof value==='number'&&Number.isSafeInteger(value)&&value>=0)output[key]=value;
  }
  if(typeof metadata.version==='string'&&/^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]{1,32})?$/.test(metadata.version))output.version=metadata.version;
  if(typeof metadata.engineState==='string'&&ENGINE_STATES.has(metadata.engineState))output.engineState=metadata.engineState;
  return output;
}

/** 진단은 기록·인증 작업과 독립적이다. 실패·느린 디스크·대기열 포화는 호출자를 막지 않는다. */
export class DesktopDiagnostics {
  readonly path:string;
  private tail:Promise<void>=Promise.resolve();
  private pending=0;
  private dropped=false;
  constructor(userData:string,private options:{maxBytes?:number;maxPending?:number}={}){
    this.path=join(userData,'logs','desktop.jsonl');
  }
  log(event:Event,metadata:Metadata={}):void {
    const entry=diagnosticEntry(event,metadata);if(!entry)return;
    if(this.pending>=(this.options.maxPending??MAX_PENDING)){this.dropped=true;return;}
    const line=JSON.stringify(entry)+'\n';this.pending++;
    this.tail=this.tail.catch(()=>{}).then(()=>this.write(line)).catch(()=>{}).finally(async()=>{
      this.pending--;
      if(this.pending===0&&this.dropped){
        this.dropped=false;
        await this.write(JSON.stringify(diagnosticEntry('diagnostics.dropped'))+'\n').catch(()=>{});
      }
    });
  }
  request(status:RequestDiagnostic):void {
    if(!STATES.has(status.state))return;
    this.log('chatgpt.request',{request_id:status.id,state:status.state,model:status.model,
      startedAt:status.startedAt,updatedAt:status.updatedAt,receivedChars:status.receivedChars,
      durationMs:Math.max(0,status.updatedAt-status.startedAt)});
  }
  private async write(line:string):Promise<void>{
    const maxBytes=this.options.maxBytes??MAX_BYTES,bytes=Buffer.byteLength(line);
    if(bytes>maxBytes)return;
    await mkdir(join(this.path,'..'),{recursive:true,mode:0o700});
    let size=0;try{size=(await stat(this.path)).size;}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
    if(size>maxBytes){await truncate(this.path,0);size=0;}
    if(size+bytes>maxBytes){
      await rm(this.path+'.1',{force:true});await rename(this.path,this.path+'.1');
    }
    await appendFile(this.path,line,{encoding:'utf8',mode:0o600});
  }
  async flush(timeoutMs=750):Promise<boolean>{
    let timer:ReturnType<typeof setTimeout>|undefined;
    try{return await Promise.race([this.tail.then(()=>true),new Promise<boolean>(done=>{timer=setTimeout(()=>done(false),Math.max(1,timeoutMs));})]);}
    finally{if(timer)clearTimeout(timer);}
  }
}
