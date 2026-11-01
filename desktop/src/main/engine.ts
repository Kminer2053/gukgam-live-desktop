import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {Socket} from 'node:net';
import {mkdir,rm,writeFile} from 'node:fs/promises';
import {dirname,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {StringDecoder} from 'node:string_decoder';
import type {BackendEvent,EngineState,SetupInput} from '../shared/contracts';
import {ENGINE_ORIGIN,explicitPython,parseBackendEvent,parseRelayConnectionResult,redactLog,validateEngineSetup} from './security';
type Launch = {executable:string;args:string[];cwd:string;env:NodeJS.ProcessEnv};
type Options = {userData:string;resourcesPath:string;projectRoot:string;packaged:boolean;env?:NodeJS.ProcessEnv;spawn?:typeof spawn;checkPort?:()=>Promise<void>;readyTimeoutMs?:number;shutdownTimeoutMs?:number;forceKillTimeoutMs?:number;connectionTimeoutMs?:number;onEvent?:(event:BackendEvent)=>void;onStopped?:()=>void};
export function buildLaunch(options:Pick<Options,'userData'|'resourcesPath'|'projectRoot'|'packaged'|'env'>,configPath:string):Launch {
  const original=options.env??process.env,env:NodeJS.ProcessEnv={};
  for(const [key,value] of Object.entries(original))if(!key.startsWith('GUKGAM_')&&!['PYTHONPATH','PYTHONHOME'].includes(key))env[key]=value;
  // 외부 환경은 네트워크를 끄는 값만 전달할 수 있다. 켜는 값·다른 GUKGAM 설정은 격리한다.
  if(original.GUKGAM_ALLOW_ASSEMBLY_NETWORK==='0')env.GUKGAM_ALLOW_ASSEMBLY_NETWORK='0';
  env.GUKGAM_DATA_DIR=join(options.userData,'records');
  if(options.packaged) {
    const executable=join(options.resourcesPath,'engine','gukgam-engine.exe');
    return {executable,args:['--config',configPath],cwd:dirname(executable),env};
  }
  return {executable:explicitPython(original.GUKGAM_DESKTOP_PYTHON),args:['-m','gukgam.desktop_runtime','--config',configPath],cwd:options.projectRoot,env};
}
/** 접속 여부만 점검하며 기존 서비스에는 요청·종료 명령을 보내지 않는다. */
export function ensurePortAvailable():Promise<void> {
  return new Promise((resolve,reject)=>{
    const socket=new Socket();let finished=false;
    const finish=(error?:Error)=>{if(finished)return;finished=true;socket.destroy();error?reject(error):resolve();};
    socket.setTimeout(1000,()=>finish(Error('8765 포트 상태를 확인하지 못했습니다. 잠시 후 다시 시도하세요.')));
    socket.once('connect',()=>finish(Error('8765 포트를 다른 앱이 사용 중입니다. 기존 앱에는 접속하지 않았습니다. 기존 앱을 직접 닫은 뒤 다시 시도하세요.')));
    socket.once('error',(error:NodeJS.ErrnoException)=>finish(error.code==='ECONNREFUSED'?undefined:Error('8765 포트 상태를 확인하지 못했습니다.')));
    socket.connect(8765,'127.0.0.1');
  });
}
export class EngineController {
  private child?:ChildProcessWithoutNullStreams;
  private current:EngineState={state:'stopped',message:'기록 화면을 열 준비가 됐습니다.'};
  private configPath?:string;
  private shutdown?:Promise<EngineState>;
  private logs='';
  private sensitiveValues=new Set<string>();
  private starting=false;
  private generation=0;
  private connectionReads=new Map<string,{child:ChildProcessWithoutNullStreams;finish:(value?:string)=>void}>();
  constructor(private options:Options) {}
  status():EngineState {return {...this.current};}
  /** 로컬 포트 대신 이 앱이 생성한 자식의 익명 파이프로 현재 중계 세대를 읽는다. */
  readRelayConnectionId():Promise<string|undefined> {
    const child=this.child;
    if(!child||this.current.state!=='ready'||child.exitCode!==null||child.signalCode!==null||child.stdin.destroyed||!child.stdin.writable)return Promise.resolve(undefined);
    const id=randomUUID().replace(/-/g,'');
    return new Promise(resolve=>{
      const finish=(value?:string)=>{
        if(this.connectionReads.get(id)?.child!==child)return;
        clearTimeout(timer);this.connectionReads.delete(id);
        resolve(this.child===child&&this.current.state==='ready'&&child.exitCode===null&&child.signalCode===null?value:undefined);
      };
      const timer=setTimeout(()=>finish(),this.options.connectionTimeoutMs??2000);
      this.connectionReads.set(id,{child,finish});
      try {child.stdin.write(JSON.stringify({type:'relay.connection.request',id})+'\n',error=>{if(error)finish();});}
      catch {finish();}
    });
  }
  private clearConnectionReads(child?:ChildProcessWithoutNullStreams) {
    for(const pending of this.connectionReads.values())if(!child||pending.child===child)pending.finish();
  }
  send(value:unknown) {
    const child=this.child;
    if(!child||child.stdin.destroyed||!child.stdin.writable)return;
    const data=JSON.stringify(value)+'\n';if(Buffer.byteLength(data)>1_048_576)throw Error('AI 응답이 기록 엔진의 전송 한도를 넘었습니다.');
    child.stdin.write(data,()=>{});
  }
  /** 키는 디스크·환경·진단에 넣지 않고 소유한 자식의 익명 파이프로만 보낸다. */
  sendSensitive(value:unknown,secret:string){if(secret)this.sensitiveValues.add(secret);this.send(value);}
  async start(raw:SetupInput & {relayPolicyRevision?:string}):Promise<EngineState> {
    if(this.child||this.starting||this.shutdown)throw Error('기록 엔진이 이미 실행 중이거나 종료 중입니다.');
    const config=validateEngineSetup(raw);
    this.starting=true;const generation=++this.generation;
    const check=()=>{if(generation!==this.generation)throw Error('기록 엔진 시작을 취소했습니다.');};
    this.current={state:'starting',message:'이 앱의 기록 엔진을 준비하고 있습니다.'};
    try {
      await (this.options.checkPort??ensurePortAvailable)();
      check();
      const configDir=join(this.options.userData,'private');
      await mkdir(configDir,{recursive:true,mode:0o700});
      check();
      this.configPath=join(configDir,`engine-${randomUUID()}.json`);
      const ownedConfigPath=this.configPath;
      await writeFile(this.configPath,JSON.stringify(config),{mode:0o600,flag:'wx'});
      check();
      const launch=buildLaunch(this.options,this.configPath);
      const child=(this.options.spawn??spawn)(launch.executable,launch.args,{cwd:launch.cwd,env:launch.env,windowsHide:true,shell:false,stdio:['pipe','pipe','pipe']}) as ChildProcessWithoutNullStreams;
      this.child=child;this.logs='';
      let buffer='',stderr='';const stdoutDecoder=new StringDecoder('utf8'),stderrDecoder=new StringDecoder('utf8');
      child.stdin.on('error',()=>{});
      const cleanConfig=async()=>{if(this.configPath===ownedConfigPath)this.configPath=undefined;await rm(ownedConfigPath,{force:true}).catch(()=>{});};
      const started=new Promise<EngineState>((resolve,reject)=>{
        let settled=false;
        const timer=setTimeout(()=>fail(Error('기록 엔진 준비 시간이 지났습니다. 앱을 종료한 뒤 다시 시도하세요.')),this.options.readyTimeoutMs??30_000);
        const fail=(error:Error)=>{if(settled)return;settled=true;clearTimeout(timer);reject(error);};
        child.once('error',()=>{this.clearConnectionReads(child);fail(Error('기록 엔진 파일을 실행하지 못했습니다. 설치 파일을 다시 확인하세요.'));});
        child.once('exit',()=>{
          this.clearConnectionReads(child);
          const owned=this.child===child;
          if(owned)this.child=undefined;
          void cleanConfig();
          if(owned) {
            if(this.current.state!=='stopping')this.current={state:'error',message:'기록 엔진이 종료됐습니다. 앱에서 다시 시작할 수 있습니다.'};
            this.options.onStopped?.();
          }
          fail(Error('기록 엔진이 준비되기 전에 종료됐습니다.'));
        });
        child.stdout.on('data',(data:Buffer)=>{
          if(this.child!==child)return;
          buffer+=stdoutDecoder.write(data);
          if(Buffer.byteLength(buffer)>1_048_576){fail(Error('기록 엔진의 응답이 너무 큽니다.'));void this.stop();return;}
          let newline:number;
          while((newline=buffer.indexOf('\n'))>=0) {
            const line=buffer.slice(0,newline);buffer=buffer.slice(newline+1);if(!line.trim())continue;
            let event:BackendEvent;
            try {
              const raw=JSON.parse(line);
              if(raw&&typeof raw==='object'&&!Array.isArray(raw)&&raw.type==='relay.connection.result') {
                const result=parseRelayConnectionResult(raw);
                const pending=result?this.connectionReads.get(result.id):undefined;
                if(result&&pending?.child===child)pending.finish(result.connection_id??undefined);
                continue;
              }
              event=parseBackendEvent(raw);
            } catch {fail(Error('기록 엔진의 응답 형식이 올바르지 않습니다.'));void this.stop();return;}
            if(event.type==='error'){fail(Error(event.message));void this.stop();return;}
            if(event.type==='ready') {
              if(settled||this.current.state!=='starting')continue;
              settled=true;clearTimeout(timer);void cleanConfig();this.current={state:'ready',message:'기록 엔진이 준비됐습니다.'};resolve(this.status());
            } else if(this.current.state==='ready')this.options.onEvent?.(event);
          }
        });
        child.stderr.on('data',(data:Buffer)=>{
          if(this.child!==child)return;
          stderr+=stderrDecoder.write(data);
          if(stderr.length>8192){stderr='';this.logs=(this.logs+'\n[긴 진단 생략]').slice(-10_000);return;}
          let newline:number;
          while((newline=stderr.indexOf('\n'))>=0) {
            let line=stderr.slice(0,newline);stderr=stderr.slice(newline+1);
            if(config.relayToken)line=line.split(config.relayToken).join('[숨김]');
            for(const secret of this.sensitiveValues)line=line.split(secret).join('[숨김]');
            this.logs=(this.logs+'\n'+redactLog(line)).slice(-10_000);
          }
        });
      });
      return await started;
    } catch(error) {
      await this.stop();
      const message=error instanceof Error&&/[가-힣]/.test(error.message)?error.message:'기록 엔진을 시작하지 못했습니다. 설치 파일을 확인하세요.';
      this.current={state:'error',message};throw Error(message);
    } finally {this.starting=false;}
  }
  async stop():Promise<EngineState> {
    if(this.shutdown)return this.shutdown;
    ++this.generation;
    this.clearConnectionReads();
    this.shutdown=(async()=>{
      const child=this.child;this.current={state:'stopping',message:'이 앱의 기록 엔진을 종료하고 있습니다.'};
      if(child&&child.exitCode===null&&child.signalCode===null) {
        await new Promise<void>(resolve=>{
          let finished=false;let force:ReturnType<typeof setTimeout>|undefined;
          const finish=()=>{if(finished)return;finished=true;clearTimeout(timer);if(force)clearTimeout(force);resolve();};
          const timer=setTimeout(()=>{
            if(this.child===child&&child.exitCode===null&&child.signalCode===null)child.kill('SIGTERM');
            force=setTimeout(()=>{if(this.child===child&&child.exitCode===null&&child.signalCode===null)child.kill('SIGKILL');finish();},this.options.forceKillTimeoutMs??2000);
          },this.options.shutdownTimeoutMs??10_000);
          child.once('exit',finish);child.once('error',finish);
          this.send({type:'shutdown'});
        });
      }
      if(this.child===child)this.child=undefined;
      this.sensitiveValues.clear();
      if(this.configPath){await rm(this.configPath,{force:true}).catch(()=>{});this.configPath=undefined;}
      this.current={state:'stopped',message:'기록 엔진을 종료했습니다. 저장한 회의 기록은 남아 있습니다.'};
      return this.status();
    })();
    try {return await this.shutdown;} finally {this.shutdown=undefined;}
  }
}
export {ENGINE_ORIGIN};
