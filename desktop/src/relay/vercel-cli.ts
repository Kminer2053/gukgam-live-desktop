import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {lstat,mkdir,mkdtemp,readFile,rm,writeFile} from 'node:fs/promises';
import {isAbsolute,join} from 'node:path';
import {StringDecoder} from 'node:string_decoder';
import {SITE_UPDATE_CODES,type SiteUpdateStatus} from './contracts';

/** 전용 CLI의 auth.json 전체를 저장한다. 이 값은 renderer·로그에 전달하지 않는다. */
export interface VercelAuthVault {
  get<T>(key:string):Promise<T|undefined>;
  set(key:string,value:unknown):Promise<void>;
  delete(key:string):Promise<void>;
}
export type VercelCliResult={code:number;stdout:string;stderr:string};
export type VercelCliRunOptions={cwd:string;input?:string;signal?:AbortSignal;onDeviceUrl?:(url:string)=>void};
export type VercelApiOptions={cwd:string;teamId?:string;body?:unknown;method?:'GET'|'POST'|'PATCH';signal?:AbortSignal};
export type VercelApiResult={status:number;body:unknown};
type AuthEnvelope={version:1;authJson:string};
export type VercelCliOptions={
  cliPath:string;tempRoot:string;vault:VercelAuthVault;
  executable?:string;env?:NodeJS.ProcessEnv;spawn?:typeof spawn;
  timeoutMs?:number;killWaitMs?:number;maxOutputBytes?:number;
  fetch?:typeof fetch;apiTimeoutMs?:number;maxApiBytes?:number;
};
export const VERCEL_AUTH_VAULT_KEY='vercel-cli-auth';
const MAX_AUTH_BYTES=1_048_576;
const MAX_INPUT_BYTES=1_048_576;
const SAFE_ENV=/^(PATH|PATHEXT|SYSTEMROOT|WINDIR|COMSPEC|TEMP|TMP|TMPDIR|HOME|USERPROFILE|HOMEDRIVE|HOMEPATH|APPDATA|LOCALAPPDATA|LANG|LC_[A-Z_]+|TZ)$/i;

/** 기존 셸 계정·토큰·Node 옵션·에이전트 문맥을 자식 프로세스에 넘기지 않는다. */
export function vercelChildEnvironment(parent:NodeJS.ProcessEnv):NodeJS.ProcessEnv {
  const env:NodeJS.ProcessEnv={};
  for(const [key,value] of Object.entries(parent))if(SAFE_ENV.test(key))env[key]=value;
  return {...env,ELECTRON_RUN_AS_NODE:'1',CI:'1',NO_COLOR:'1',FORCE_COLOR:'0',FORCE_HYPERLINK:'0',TERM:'dumb',
    NO_UPDATE_NOTIFIER:'1',VERCEL_TELEMETRY_DISABLED:'1',VERCEL_CLI_USE_NATIVE_BINARY:'0',VERCEL_TOKEN_STORAGE:'file',
    npm_config_update_notifier:'false'};
}

/** 로그인용 공식 주소만 허용한다. 약관 URL은 코어에서 별도 검증한다. */
export function validateVercelDeviceUrl(value:string):string|undefined {
  if(value.length>2048)return undefined;
  try {
    const url=new URL(value);
    if(url.protocol!=='https:'||url.hostname!=='vercel.com'||url.port||url.username||url.password||url.hash)return undefined;
    if(!/^\/(?:oauth\/)?device\/?$/.test(url.pathname))return undefined;
    return url.href;
  } catch {return undefined;}
}

const internalErrors=new WeakSet<Error>();
function failure(code:string,message:string):Error&{code:string} {const error=Object.assign(Error(message),{code});internalErrors.add(error);return error;}
/** 호출자가 명령 출력 대신 고정 코드·종료 수치만 보관할 수 있게 한다. */
export function vercelCommandFailure(exitCode:number):Error {
 const error=failure('VERCEL_EXIT_NONZERO','배포 도구가 정상 종료하지 않았습니다. 기존 사이트의 배포 결과를 먼저 확인하세요.');
 if(Number.isSafeInteger(exitCode)&&exitCode>=0&&exitCode<=255)Object.assign(error,{exitCode});
 return error;
}
export function vercelFailureMetadata(error:unknown):Pick<SiteUpdateStatus,'knownCode'|'exitCode'> {
 if(!(error instanceof Error)||!internalErrors.has(error))return {};
 const raw=error as Error&{code?:unknown;exitCode?:unknown};
 const out:Pick<SiteUpdateStatus,'knownCode'|'exitCode'>={};
 if(SITE_UPDATE_CODES.includes(raw.code as any))out.knownCode=raw.code as SiteUpdateStatus['knownCode'];
 if(Number.isSafeInteger(raw.exitCode)&&(raw.exitCode as number)>=0&&(raw.exitCode as number)<=255)out.exitCode=raw.exitCode as number;
 return out;
}
function authEnvelope(value:unknown):AuthEnvelope|undefined {
  if(value===undefined)return undefined;
  if(!value||typeof value!=='object'||(value as AuthEnvelope).version!==1||typeof (value as AuthEnvelope).authJson!=='string')
    throw failure('VERCEL_AUTH_INVALID','보호된 Vercel 로그인 정보를 읽지 못했습니다. 다시 로그인하세요.');
  const auth=(value as AuthEnvelope).authJson;
  if(Buffer.byteLength(auth)>MAX_AUTH_BYTES)throw failure('VERCEL_AUTH_INVALID','Vercel 로그인 파일이 허용 크기를 넘었습니다.');
  let parsed:unknown;
  try {parsed=JSON.parse(auth);}catch {throw failure('VERCEL_AUTH_INVALID','Vercel 로그인 파일 형식이 올바르지 않습니다.');}
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw failure('VERCEL_AUTH_INVALID','Vercel 로그인 파일 형식이 올바르지 않습니다.');
  return {version:1,authJson:auth};
}
function sensitiveStrings(auth:string|undefined,input:string|undefined):string[] {
  const values:string[]=[];
  const visit=(value:unknown,key='')=>{
    if(typeof value==='string'&&/token|secret|password|passcode|database.?url|private.?key/i.test(key)&&value.length>3)values.push(value);
    else if(value&&typeof value==='object')for(const [k,v]of Object.entries(value))visit(v,k);
  };
  for(const raw of [auth,input])if(raw)try{visit(JSON.parse(raw));}catch{}
  if(input&&input.trim().length>3)values.push(input.trim());
  return values;
}
function redact(value:string,secrets:string[],diagnostic=false):string {
  let text=value;
  for(const secret of [...new Set(secrets)].sort((a,b)=>b.length-a.length))text=text.split(secret).join('[숨김]');
  text=text.replace(/Bearer\s+[^\s"']+/gi,'Bearer [숨김]');
  return diagnostic?text.replace(/postgres(?:ql)?:\/\/[^\s"']+/gi,'[DB 주소 숨김]'):text;
}
function checkArguments(args:string[]) {
  if(!args.length||args.length>150||args.some(a=>typeof a!=='string'||a.length>16_384||/[\0\r\n]/.test(a)))
    throw failure('VERCEL_ARGS_INVALID','Vercel 실행 요청 형식이 올바르지 않습니다.');
  const blocked=/^(--(?:global-config|token|cwd|local-config|api|debug|verbose|spec-url|generate|dangerously-skip-permissions)(?:=|$)|-[QAtd])/;
  if(args.some(a=>blocked.test(a)))throw failure('VERCEL_ARGS_INVALID','앱의 Vercel 인증·실행 경로를 바꾸는 옵션은 사용할 수 없습니다.');
}

function apiUrl(path:string,teamId:string|undefined):URL {
  if(typeof path!=='string'||path.length>8192||!path.startsWith('/')||path.startsWith('//')||/[\s\u0000-\u001f\u007f\\#]/.test(path))
    throw failure('VERCEL_API_PATH_INVALID','Vercel API 경로가 올바르지 않습니다.');
  let pathname=path.split('?')[0];
  try {
    for(let step=0;step<5;step++) {
      if(/(?:^|\/)\.+(?:\/|$)|[\s\u0000-\u001f\u007f\\?#\u2025\u2026\uff0e\u2215\u2044]/.test(pathname)||pathname.includes('//'))throw Error();
      const decoded=decodeURIComponent(pathname);
      if(decoded===pathname)break;
      if(step===4)throw Error();
      pathname=decoded;
    }
  } catch {throw failure('VERCEL_API_PATH_INVALID','Vercel API 경로가 올바르지 않습니다.');}
  if(!/^\/v[0-9]+\//.test(pathname))throw failure('VERCEL_API_PATH_INVALID','지원하는 Vercel API 버전 경로가 필요합니다.');
  const url=new URL(path,'https://api.vercel.com');
  if(url.origin!=='https://api.vercel.com'||url.username||url.password||url.hash)throw failure('VERCEL_API_PATH_INVALID','Vercel API 주소가 올바르지 않습니다.');
  if(teamId!==undefined) {
    if(!/^[A-Za-z0-9_-]{1,128}$/.test(teamId))throw failure('VERCEL_API_TEAM_INVALID','선택한 Vercel 팀 식별자가 올바르지 않습니다.');
    url.searchParams.delete('teamId');url.searchParams.delete('slug');url.searchParams.set('teamId',teamId);
  } else if(url.searchParams.has('teamId')||url.searchParams.has('slug')) {
    throw failure('VERCEL_API_TEAM_INVALID','Vercel 팀은 API 경로와 분리해서 지정해야 합니다.');
  }
  return url;
}

async function boundedApiJson(response:Response,maxBytes:number,signal:AbortSignal,secrets:string[]):Promise<unknown> {
  if(Number(response.headers.get('content-length')??0)>maxBytes) {
    void response.body?.cancel().catch(()=>{});
    throw failure('VERCEL_API_BODY_LIMIT','Vercel API 응답이 허용 크기를 넘었습니다.');
  }
  const reader=response.body?.getReader();
  if(!reader)return null;
  const chunks:Uint8Array[]=[];let bytes=0;
  const abort=()=>{void reader.cancel().catch(()=>{});};
  signal.addEventListener('abort',abort,{once:true});
  try {
    while(true) {
      if(signal.aborted)throw failure('VERCEL_API_CANCELLED','Vercel API 요청을 취소했습니다.');
      const {done,value}=await reader.read();if(done)break;
      bytes+=value.byteLength;
      if(bytes>maxBytes){void reader.cancel().catch(()=>{});throw failure('VERCEL_API_BODY_LIMIT','Vercel API 응답이 허용 크기를 넘었습니다.');}
      chunks.push(value);
    }
    if(signal.aborted)throw failure('VERCEL_API_CANCELLED','Vercel API 요청을 취소했습니다.');
    if(!bytes)return null;
    try {
      const text=new TextDecoder('utf8',{fatal:true}).decode(Buffer.concat(chunks));
      // HTTP 오류가 토큰을 반사해도 자격증명을 반환하지 않는다. DB URL 등 업무 데이터는 보존한다.
      const body=JSON.parse(text);
      return JSON.parse(redact(JSON.stringify(body),secrets));
    } catch {throw failure('VERCEL_API_BODY_INVALID','Vercel API의 JSON 응답을 읽지 못했습니다.');}
  } finally {signal.removeEventListener('abort',abort);reader.releaseLock();}
}

/** 동봉 CLI만 실행한다. 회전하는 refreshToken 충돌을 막기 위해 호출을 직렬화한다. */
export class VercelCliRunner {
  private tail:Promise<unknown>=Promise.resolve();
  private unsafeChild=false;
  constructor(private options:VercelCliOptions) {
    if(!isAbsolute(options.cliPath)||!isAbsolute(options.tempRoot)||options.executable&&!isAbsolute(options.executable))
      throw failure('VERCEL_PATH_INVALID','Vercel 동봉 파일과 전용 임시 폴더에는 절대 경로가 필요합니다.');
  }
  run(args:string[],options:VercelCliRunOptions):Promise<VercelCliResult> {
    const copiedArgs=[...args],copiedOptions={...options};
    const operation=this.tail.then(()=>this.execute(copiedArgs,copiedOptions));
    this.tail=operation.catch(()=>{});return operation;
  }
  requestApi(path:string,options:VercelApiOptions):Promise<VercelApiResult> {
    const copied={...options};
    // 본문도 대기 전에 복사한다. 실행 중 호출자가 바꾼 값을 보내지 않는다.
    let input:string|undefined;
    try {input=copied.body===undefined?undefined:JSON.stringify(copied.body);}catch{return Promise.reject(failure('VERCEL_API_INPUT_INVALID','Vercel API 입력 형식이 올바르지 않습니다.'));}
    const operation=this.tail.then(()=>this.executeApi(path,copied,input));
    this.tail=operation.catch(()=>{});return operation;
  }
  private async executeApi(path:string,options:VercelApiOptions,input:string|undefined):Promise<VercelApiResult> {
    const url=apiUrl(path,options.teamId),method=options.method??'GET';
    if(!['GET','POST','PATCH'].includes(method)||method==='GET'&&input!==undefined)
      throw failure('VERCEL_API_METHOD_INVALID','이 Vercel API 요청 방식은 허용되지 않습니다.');
    if(input!==undefined&&Buffer.byteLength(input)>MAX_INPUT_BYTES)throw failure('VERCEL_API_INPUT_LIMIT','Vercel API 입력이 허용 크기를 넘었습니다.');
    if(options.signal?.aborted)throw failure('VERCEL_API_CANCELLED','Vercel API 요청을 취소했습니다.');
    const controller=new AbortController();let timedOut=false;
    const abort=()=>controller.abort();
    options.signal?.addEventListener('abort',abort,{once:true});
    const timer=setTimeout(()=>{timedOut=true;controller.abort();},this.options.apiTimeoutMs??30_000);
    try {
      // run()을 다시 호출하면 tail이 자신을 기다리므로 직렬 구간 안에서 private execute를 쓴다.
      const refresh=await this.execute(['whoami'],{cwd:options.cwd,signal:controller.signal});
      if(refresh.code!==0)throw failure('VERCEL_API_AUTH_FAILED','Vercel 로그인을 확인하지 못했습니다. 다시 로그인하세요.');
      if(controller.signal.aborted)throw failure('VERCEL_API_CANCELLED','Vercel API 요청을 취소했습니다.');
      const envelope=authEnvelope(await this.options.vault.get<unknown>(VERCEL_AUTH_VAULT_KEY));
      const auth=envelope?JSON.parse(envelope.authJson) as Record<string,unknown>:{};
      const token=auth.token;
      if(typeof token!=='string'||!token||token.length>16_384||!/^[-A-Za-z0-9._~+/]+=*$/.test(token))
        throw failure('VERCEL_API_AUTH_FAILED','Vercel 로그인 정보를 확인하지 못했습니다. 다시 로그인하세요.');
      const response=await (this.options.fetch??fetch)(url,{method,redirect:'error',signal:controller.signal,
        headers:{Authorization:`Bearer ${token}`,Accept:'application/json',...(input!==undefined?{'Content-Type':'application/json'}:{})},
        ...(input!==undefined?{body:input}:{})});
      const body=await boundedApiJson(response,this.options.maxApiBytes??1_048_576,controller.signal,sensitiveStrings(envelope?.authJson,undefined));
      return {status:response.status,body};
    } catch(error) {
      if(controller.signal.aborted)throw failure(timedOut?'VERCEL_API_TIMEOUT':'VERCEL_API_CANCELLED',timedOut?'Vercel API 요청 시간이 지났습니다. 결과를 확인한 뒤 다시 시도하세요.':'Vercel API 요청을 취소했습니다.');
      if(error instanceof Error&&internalErrors.has(error))throw error;
      throw failure('VERCEL_API_FAILED','Vercel API 요청을 마치지 못했습니다. 연결 상태를 확인하세요.');
    } finally {clearTimeout(timer);options.signal?.removeEventListener('abort',abort);}
  }
  private async execute(args:string[],options:VercelCliRunOptions):Promise<VercelCliResult> {
    checkArguments(args);
    if(!isAbsolute(options.cwd))throw failure('VERCEL_PATH_INVALID','Vercel 작업 폴더에는 절대 경로가 필요합니다.');
    if(this.unsafeChild)throw failure('VERCEL_STOP_UNCONFIRMED','이전 Vercel 작업의 종료를 확인하지 못했습니다. 앱을 다시 실행하세요.');
    if(options.signal?.aborted)throw failure('VERCEL_CANCELLED','Vercel 작업을 취소했습니다.');
    if(options.input&&Buffer.byteLength(options.input)>MAX_INPUT_BYTES)throw failure('VERCEL_INPUT_LIMIT','Vercel 입력이 허용 크기를 넘었습니다.');
    // vault가 잠겼으면 임시 평문 파일을 만들기 전에 중단한다.
    const saved=authEnvelope(await this.options.vault.get<unknown>(VERCEL_AUTH_VAULT_KEY));
    await mkdir(this.options.tempRoot,{recursive:true,mode:0o700});
    const dir=await mkdtemp(join(this.options.tempRoot,'vercel-'));
    const authPath=join(dir,'auth.json');
    let child:ChildProcessWithoutNullStreams|undefined;
    let stopped=true;
    let stdout='',stderr='';
    let resultCode=1;
    let issue:Error|undefined;
    const authSecrets=sensitiveStrings(saved?.authJson,undefined);
    const diagnosticSecrets=[...authSecrets,...sensitiveStrings(undefined,options.input)];
    try {
      await writeFile(join(dir,'config.json'),JSON.stringify({credStorage:'file',autoUpdate:false}),{mode:0o600,flag:'wx'});
      if(saved)await writeFile(authPath,saved.authJson,{mode:0o600,flag:'wx'});
      if(options.signal?.aborted)throw failure('VERCEL_CANCELLED','Vercel 작업을 취소했습니다.');
      resultCode=await new Promise<number>((resolve,reject)=>{
        let settled=false,total=0;
        let timer:ReturnType<typeof setTimeout>|undefined;
        let force:ReturnType<typeof setTimeout>|undefined;
        let abandon:ReturnType<typeof setTimeout>|undefined;
        const outDecoder=new StringDecoder('utf8'),errDecoder=new StringDecoder('utf8');
        const scans={stdout:'',stderr:''},seen=new Set<string>();
        const onUrls=(part:'stdout'|'stderr',text:string,flush=false)=>{
          if(settled||issue||options.signal?.aborted||args[0]!=='login'||!options.onDeviceUrl)return;
          const scan=(scans[part]+text).slice(-8192);
          // 두 스트림을 섞지 않는다. 공백·제어문자까지 도착한 URI만 읽어 분할 URL을 미리 열지 않는다.
          const delimiters=[...scan.matchAll(/[\s<>"'\u001b]/g)];
          const end=flush?scan.length:delimiters.length?(delimiters.at(-1)!.index??0)+1:0;
          const complete=scan.slice(0,end);scans[part]=scan.slice(end);
          for(const match of complete.matchAll(/https:\/\/[^\s<>"'\u001b]+/g)) {
            const url=validateVercelDeviceUrl(match[0].replace(/[),.;]+$/,''));
            if(url&&!seen.has(url)){seen.add(url);try{options.onDeviceUrl(url);}catch{}}
          }
        };
        const cleanup=()=>{clearTimeout(timer);clearTimeout(force);clearTimeout(abandon);options.signal?.removeEventListener('abort',abort);};
        const finish=(error?:Error,exitCode?:number)=>{
          if(settled)return;
          if(!error&&exitCode===0){onUrls('stdout','',true);onUrls('stderr','',true);}
          settled=true;cleanup();
          error?reject(error):resolve(exitCode??1);
        };
        const terminate=(error:Error)=>{
          if(issue)return;issue=error;
          if(!child||stopped){finish(error);return;}
          try {child.kill('SIGTERM');}catch{}
          if(stopped)return;
          force=setTimeout(()=>{
            if(stopped)return;try {child?.kill('SIGKILL');}catch{}
            if(stopped)return;
            abandon=setTimeout(()=>{
              if(stopped)return;
              // 종료가 확인되지 않으면 새 작업을 막고, close 이후에만 평문을 지운다.
              this.unsafeChild=true;
              finish(failure('VERCEL_STOP_UNCONFIRMED','Vercel 작업 종료를 확인하지 못했습니다. 앱을 닫은 뒤 다시 실행하세요.'));
            },this.options.killWaitMs??2000);
          },this.options.killWaitMs??2000);
        };
        const abort=()=>terminate(failure('VERCEL_CANCELLED','Vercel 작업을 취소했습니다.'));
        const output=(part:'stdout'|'stderr',chunk:Buffer)=>{
          if(settled||issue)return;
          total+=chunk.length;
          if(total>(this.options.maxOutputBytes??1_048_576)){terminate(failure('VERCEL_OUTPUT_LIMIT','Vercel 응답이 허용 크기를 넘었습니다.'));return;}
          const value=(part==='stdout'?outDecoder:errDecoder).write(chunk);
          if(part==='stdout')stdout+=value;else stderr+=value;
          onUrls(part,value);
        };
        try {
          child=(this.options.spawn??spawn)(this.options.executable??process.execPath,
            [this.options.cliPath,...args,'--global-config',dir],
            {cwd:options.cwd,env:vercelChildEnvironment(this.options.env??process.env),shell:false,windowsHide:true,stdio:['pipe','pipe','pipe']}) as ChildProcessWithoutNullStreams;
          stopped=false;
          child.stdin.on('error',()=>{});
          child.stdout.on('data',(chunk:Buffer)=>output('stdout',chunk));
          child.stderr.on('data',(chunk:Buffer)=>output('stderr',chunk));
          child.stdout.once('end',()=>{const value=outDecoder.end();stdout+=value;onUrls('stdout',value,true);});
          child.stderr.once('end',()=>{const value=errDecoder.end();stderr+=value;onUrls('stderr',value,true);});
          child.once('error',()=>{stopped=true;finish(failure('VERCEL_START_FAILED','동봉된 Vercel 실행 파일을 시작하지 못했습니다.'));});
          child.once('close',(exitCode:number|null)=>{
            stopped=true;
            if(settled){void rm(dir,{recursive:true,force:true}).catch(()=>{});return;}
            stdout+=outDecoder.end();stderr+=errDecoder.end();
            finish(issue,exitCode??1);
          });
          timer=setTimeout(()=>terminate(failure('VERCEL_TIMEOUT','Vercel 작업 시간이 지났습니다. 연결 상태를 확인한 뒤 다시 시도하세요.')),this.options.timeoutMs??600_000);
          options.signal?.addEventListener('abort',abort,{once:true});
          if(options.signal?.aborted)abort();
          if(options.input!==undefined)child.stdin.end(options.input);
        } catch {stopped=true;finish(failure('VERCEL_START_FAILED','동봉된 Vercel 실행 파일을 시작하지 못했습니다.'));}
      });
    } catch(error) {issue=error instanceof Error?error:failure('VERCEL_FAILED','Vercel 작업을 마치지 못했습니다.');throw issue;
    } finally {
      if(stopped) {
        try {
          const stat=await lstat(authPath).catch(error=>{if((error as NodeJS.ErrnoException).code==='ENOENT')return undefined;throw error;});
          if(stat) {
            if(!stat.isFile()||stat.isSymbolicLink()||stat.size>MAX_AUTH_BYTES)throw failure('VERCEL_AUTH_INVALID','Vercel 로그인 파일 형식이 올바르지 않습니다.');
            const auth=authEnvelope({version:1,authJson:await readFile(authPath,'utf8')})!;
            const refreshed=sensitiveStrings(auth.authJson,undefined);
            authSecrets.push(...refreshed);diagnosticSecrets.push(...refreshed);
            await this.options.vault.set(VERCEL_AUTH_VAULT_KEY,auth);
          } else if(!issue&&resultCode===0&&args[0]==='login') {
            throw failure('VERCEL_AUTH_INVALID','완료된 Vercel 로그인 파일을 찾지 못했습니다.');
          } else if(!issue&&resultCode===0&&args[0]==='logout') {
            await this.options.vault.delete(VERCEL_AUTH_VAULT_KEY);
          }
        } catch {throw failure('VERCEL_AUTH_SAVE_FAILED','Vercel 로그인 정보를 안전하게 보관하지 못했습니다. 다시 로그인하세요.');
        } finally {await rm(dir,{recursive:true,force:true});}
      }
    }
    // stdout은 main 내부의 API JSON이다. DB URL·환경변수 값을 고치면 후속 초기화가 깨진다.
    return {code:resultCode,stdout:redact(stdout,authSecrets),stderr:redact(stderr,diagnosticSecrets,true)};
  }
}
