import {mkdir,mkdtemp,readFile,writeFile,rename,rm,cp,stat} from 'node:fs/promises';
import {join,resolve,dirname,basename,relative} from 'node:path';
import {randomBytes,randomUUID} from 'node:crypto';
import {validateVercelDeviceUrl,vercelCommandFailure,vercelFailureMetadata} from './vercel-cli';
import type {SetupStore} from '../storage/settings';
import type {ChatGPTVault} from '../auth/chatgpt';
import {relayUrl} from '../main/security';
import {siteUpdateStatus,siteUpdateVersion,type SiteUpdateStatus,type ManagedRelayStatus,type RelayCatalog,type RelayField,type RelayPlan,type RelayProduct,type RelayProvisionInput,type RelayTeam} from './contracts';

export type Cli={requestApi(path:string,options:{cwd:string;teamId?:string;body?:unknown;method?:string;signal?:AbortSignal}):Promise<{status:number;body:unknown}>;run(args:string[],options:{cwd:string;input?:string;signal?:AbortSignal;onDeviceUrl?:(url:string)=>void}):Promise<{code:number;stdout:string;stderr:string}>};
type Job={version:1;id:string;teamId:string;teamSlug:string;projectName:string;productId:string;productSlug:string;integrationId:string;planId:string;metadata:Record<string,unknown>;installationMetadata:Record<string,unknown>;resourceName:string;phase:'prepared'|'creating-project'|'project'|'creating-database'|'database'|'configured'|'deployed';projectId?:string;resourceId?:string;url?:string;pendingApprovalUrl?:string;siteUpdate?:SiteUpdateStatus};
type Secrets={operatorPasscode:string;viewerPasscode:string;token:string;key:string};
type Options={userData:string;templateDir:string;cli:Cli;settings:Pick<SetupStore,'read'|'save'>;vault:ChatGPTVault;initializeDatabase:(dsn:string,signal?:AbortSignal)=>Promise<void>;openExternal:(url:string)=>Promise<unknown>;fetcher?:typeof fetch;expectedConnection?:()=>Promise<string|undefined>;version?:string;onSiteUpdate?:(value:SiteUpdateStatus)=>void};
const identifier=/^[a-zA-Z0-9_-]{1,128}$/;
const excludedTemplateParts=new Set(['.vercel','.git','__pycache__','node_modules','docs','outputs','records','raw','audio','vault','account']);
class ApiFailure extends Error {constructor(readonly status:number,readonly body:any){super(status===401?'사이트 계정을 다시 연결하세요.':status===403?'이 작업 공간의 기관 SSO 재인증 또는 관리 권한이 필요합니다. 공식 사이트에서 기관 관리자와 접근 권한을 확인하거나 사용 권한이 있는 작업 공간을 선택하세요.':'외부 사이트 요청을 완료하지 못했습니다.');}}
class UpdateFailure extends Error {constructor(readonly updateCode:NonNullable<SiteUpdateStatus['knownCode']>){super('사이트 업데이트 단계를 확인하지 못했습니다.');}}
const genericError='릴레이 구성을 완료하지 못했습니다. 계정 권한·선택한 요금제·외부 서비스 상태를 확인한 뒤 ‘이어서 준비’를 누르세요. 이미 만든 자료는 삭제하거나 중복 생성하지 않습니다.';
function parse(text:string):any {try{return JSON.parse(text.trim());}catch{throw Error('배포 도구의 응답 형식을 확인하지 못했습니다.');}}
function safeId(value:unknown):string {if(typeof value!=='string'||!identifier.test(value))throw Error('외부 서비스의 식별자 형식이 바뀌었습니다.');return value;}
function jsonObject(value:unknown):Record<string,any> {if(!value||typeof value!=='object'||Array.isArray(value))throw Error('외부 서비스의 응답 형식을 확인하세요.');return value as Record<string,any>;}
export function officialApproval(value:string):string {const url=new URL(value);if(url.protocol!=='https:'||url.hostname!=='vercel.com'||url.port||url.username||url.password||url.hash)throw Error('공식 Vercel 승인 주소가 아닙니다.');return url.href;}
function fields(schema:any):RelayField[] {
 const props=jsonObject(schema?.properties??{}),required=new Set<string>(schema?.required??[]);
 return Object.entries(props).filter(([,p])=>p&&typeof p==='object'&&!p['ui:control']?.includes?.('hidden')&&p['ui:hidden']!==true).map(([key,p]:[string,any])=>{
  if(!/^[a-zA-Z0-9_-]{1,100}$/.test(key)||!['string','number','integer','boolean','array'].includes(p.type))throw Error('현재 앱이 지원하지 않는 DB 설정 항목입니다.');
  const uiOptions=p['ui:options'];
  const opts=p.enum??p.items?.enum??(Array.isArray(uiOptions)?uiOptions.map((x:any)=>typeof x==='string'?x:x?.value):undefined);
  return {key,label:String(p.title??key),type:p.type==='integer'?'number':p.type,required:required.has(key),...(Array.isArray(opts)?{options:opts.filter((x:any)=>typeof x==='string'&&x.length<=300)}:{}),...(p.default!==undefined?{default:p.default}:{}),description:typeof p.description==='string'?p.description.slice(0,2000):undefined};
 });
}
function normalizedMetadata(product:RelayProduct,raw:unknown):Record<string,unknown> {
 const input=jsonObject(raw),result:Record<string,unknown>={};
 if(Object.keys(input).some(k=>!product.fields.some(f=>f.key===k)))throw Error('허용되지 않은 DB 설정 항목입니다.');
 for(const field of product.fields) {
  // 이 앱은 자체 암호와 SSE를 사용한다. 신규 견적·생성에서 불필요한 부가 서비스를 켜지 않는다.
  if(['auth','realtime'].includes(field.key)&&field.type!=='boolean')throw Error('DB 부가 기능의 설정 형식이 바뀌었습니다. 구성을 중단했습니다.');
  const value=['auth','realtime'].includes(field.key)?false:input[field.key]??field.default;
  if(value===undefined||value===''){if(field.required)throw Error(`${field.label} 항목을 선택하세요.`);continue;}
  if(field.type==='array') {if(!Array.isArray(value)||value.length>20||value.some(x=>typeof x!=='string'||!field.options?.includes(x)))throw Error('DB 복수 선택 항목을 확인하세요.');}
  else if(typeof value!==field.type||(field.type==='number'&&!Number.isFinite(value)))throw Error('DB 설정 값의 형식을 확인하세요.');
  if(typeof value==='string'&&(value.length>300||/[\x00-\x1f]/.test(value)||field.options&&!field.options.includes(value)))throw Error('DB 설정 값의 범위를 확인하세요.');
  result[field.key]=value;
 }
 return result;
}
function planText(value:unknown,max=500):string|undefined {
 if(typeof value!=='string'&&!(typeof value==='number'&&Number.isFinite(value)))return undefined;
 return String(value).replace(/[\x00-\x1f\x7f]+/g,' ').trim().slice(0,max)||undefined;
}
function describePlan(plan:Record<string,any>):string {
 const lines:string[]=[],description=planText(plan.description),cost=planText(plan.cost);
 lines.push(cost?`가격·주기(제공사 표기): ${cost}`:'가격·청구 주기: 응답에 명확한 금액·주기가 없습니다. 공식 승인 화면에서 확인하세요.');
 for(const [key,label] of [['initialCharge','가입 시 청구'],['minimumAmount','최소 선불 구매액'],['maximumAmount','최대 선불 구매액'],['maximumAmountAutoPurchasePerPeriod','기간별 자동 구매 최대액'],['preauthorizationAmount','결제 수단 사전 승인액'],['effectiveDate','적용 시점']] as const) {
  const value=planText(plan[key],150);if(value)lines.push(`${label}: ${value}`);
 }
 if(plan.paymentMethodRequired===true)lines.push('결제 수단 등록 필요');
 else if(plan.paymentMethodRequired===false)lines.push('응답상 결제 수단 등록 불필요');
 if(description)lines.push(description);
 const details=new Set<string>();
 for(const rows of [plan.highlightedDetails,plan.details])if(Array.isArray(rows))for(const item of rows.slice(0,20)) {
  if(!item||typeof item!=='object'||Array.isArray(item))continue;
  const label=planText(item.label,100),value=planText(item.value,250);if(label&&value)details.add(`${label}: ${value}`);
 }
 if(details.size)lines.push(`제공량·조건: ${[...details].join(' / ')}`);
 if(Array.isArray(plan.quote)) {
  const quote=plan.quote.slice(0,20).flatMap((row:any)=>{const line=planText(row?.line,150),amount=planText(row?.amount,100);return line&&amount?[`${line}: ${amount}`]:[];});
  if(quote.length)lines.push(`견적 내역: ${quote.join(' / ')}`);
 }
 return lines.join('\n').slice(0,1700)+'\n표기되지 않은 통화·청구 주기·초과 사용료는 미확인입니다. 최종 금액·이용 조건은 공식 승인 화면에서 확인하세요.';
}
type EnvironmentReference={id:string;key:string;type:'plain'|'encrypted';target:string|string[];visibility?:unknown};
const environmentReadError='보호된 사이트 설정 값을 확인하지 못했습니다. 공식 Vercel 화면에서 추가 인증·설정 조회 권한과 Production 연결을 확인한 뒤 ‘이어서 준비’를 누르세요. 기존 사이트·DB는 보존했습니다.';
function productionEnvironment(row:Record<string,any>):boolean {
 return (row.target==='production'||Array.isArray(row.target)&&row.target.every((value:unknown)=>typeof value==='string')&&row.target.includes('production'))&&(row.gitBranch===undefined||row.gitBranch===null||row.gitBranch==='');
}
function returnedEnvironmentScope(row:Record<string,any>,reference:EnvironmentReference):boolean {
 // 개별 응답의 선택 메타데이터는 생략될 수 있다. 조회 주소는 목록에서 검증한 프로젝트·ID에 고정한다.
 return (row.id===undefined||row.id===null||row.id===reference.id)
  &&(row.target===undefined||row.target===null||productionEnvironment({...row,gitBranch:undefined}))
  &&(row.gitBranch===undefined||row.gitBranch===null||row.gitBranch==='');
}
function readableEnvironment(row:Record<string,any>):boolean {
 // 회수 불가능한 민감 Secret은 다른 API로 우회하지 않는다.
 return ['plain','encrypted'].includes(row.type)&&(row.visibility===undefined||row.visibility===null||row.visibility==='config');
}
function databaseUrl(value:unknown):string {
 if(typeof value!=='string'||!value||value.length>16_384||/[\x00-\x20\x7f]/.test(value))throw Error('DB 연결 값이 없거나 지원하는 형식이 아닙니다. 기존 DB 연결과 설정 조회 권한을 확인하세요.');
 let url:URL;try{url=new URL(value);}catch{throw Error('DB를 사이트에 연결한 결과를 확인하지 못했습니다.');}
 let password:string;try{password=decodeURIComponent(url.password);}catch{throw Error('DB 연결 정보의 형식을 확인하지 못했습니다.');}
 if(!['postgres:','postgresql:'].includes(url.protocol)||!url.hostname||!url.username||!password||!url.pathname||url.pathname==='/'||url.hash||/^(?:\*{3,}|[•●]{3,}|\[?(?:redacted|masked|hidden)\]?)$/i.test(password))throw Error('DB 연결 정보를 확인하지 못했습니다. 마스킹된 값은 사용할 수 없습니다.');
 return value;
}
export function databaseFromEnvironment(text:string):string {
 if(Buffer.byteLength(text)>1_000_000)throw Error('배포 환경 파일이 너무 큽니다.');
 const found:Record<string,string>={};
 for(const line of text.split(/\r?\n/)) {
  const match=/^(DATABASE_URL_UNPOOLED|DATABASE_URL)=(.*)$/.exec(line.trim());if(!match)continue;
  if(match[1] in found)throw Error('DB 연결 값이 중복되었습니다.');
  let value=match[2];
  if(value.startsWith('"')){try{value=JSON.parse(value);}catch{throw Error('DB 연결 값의 형식을 확인하세요.');}}
  else if(value.startsWith("'")){if(!value.endsWith("'"))throw Error('DB 연결 값의 형식을 확인하세요.');value=value.slice(1,-1);}
  found[match[1]]=value;
 }
 return databaseUrl(found.DATABASE_URL_UNPOOLED||found.DATABASE_URL);
}
export class RelayProvisioner {
 private current:ManagedRelayStatus={state:'idle',message:'릴레이를 만들면 동료가 웹에서 기록을 볼 수 있습니다.',teams:[],canResume:false,verified:false,sourceOnline:false};
 private active?:AbortController;
 private activeDone?:Promise<void>;
 private job?:Job;
 private integration:any;
 private products:RelayProduct[]=[];
 private loaded=false;
 private cwd:string;
 constructor(private options:Options){this.cwd=join(options.userData,'relay','site');}
 private async load() {
  if(this.loaded)return;
  try {
   const raw=jsonObject(parse(await readFile(join(this.options.userData,'relay','job.json'),'utf8')));
   if(raw.version!==1||typeof raw.id!=='string'||!identifier.test(raw.id)||!identifier.test(raw.teamId)||!identifier.test(raw.projectName)||!['prepared','creating-project','project','creating-database','database','configured','deployed'].includes(raw.phase))throw Error();
   for(const key of ['teamSlug','productId','productSlug','integrationId','planId','resourceName'])safeId(raw[key]);
   if(!/^gukgam-[a-z0-9][a-z0-9-]{2,45}$/.test(raw.projectName))throw Error();
   jsonObject(raw.metadata);jsonObject(raw.installationMetadata);
   if(!['prepared','creating-project'].includes(raw.phase))safeId(raw.projectId);
   if(['database','configured','deployed'].includes(raw.phase))safeId(raw.resourceId);
   if(raw.phase==='deployed')relayUrl(raw.url);
   if(raw.pendingApprovalUrl){const url=new URL(raw.pendingApprovalUrl);if(url.protocol!=='https:'||url.username||url.password||url.port||url.hash||!['vercel.com','console.neon.tech','neon.tech'].includes(url.hostname))throw Error();}
   const journal=siteUpdateStatus(raw.siteUpdate);
   const interrupted=raw.siteUpdate&&typeof raw.siteUpdate==='object'&&!Array.isArray(raw.siteUpdate)&&raw.siteUpdate.state==='updating';
   const restored:SiteUpdateStatus=journal.state==='updating'?{...journal,state:journal.deployAttempted===false?'failed':'unknown',knownCode:'INTERRUPTED'}
    :interrupted&&journal.state==='unverified'?{state:'unknown',stage:'unverified',knownCode:'INTERRUPTED'}:journal;
   const {siteUpdate:unused,...saved}=raw;
   this.job={...saved,...(restored.state!=='unverified'?{siteUpdate:restored}:{})} as Job;this.loaded=true;
   this.current={...this.current,state:raw.pendingApprovalUrl?'approval':raw.phase==='deployed'?'ready':'connected',message:raw.phase==='deployed'?'저장된 릴레이가 있습니다. 현재 PC 연결을 확인하세요.':'이전 릴레이 준비를 이어갈 수 있습니다.',projectName:raw.projectName,teamId:raw.teamId,url:raw.url,approvalUrl:raw.pendingApprovalUrl,canResume:raw.phase!=='deployed',siteUpdate:restored};
  }catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw Error('릴레이 준비 기록을 읽지 못했습니다. 기존 사이트를 중복 생성하지 않습니다.');this.loaded=true;}
 }
 async status():Promise<ManagedRelayStatus>{await this.load();return {...this.current,teams:this.current.teams.map(t=>({...t})),siteUpdate:siteUpdateStatus(this.current.siteUpdate)};}
 private async persist(job=this.job){if(!job)return;const dir=join(this.options.userData,'relay');await mkdir(dir,{recursive:true,mode:0o700});const file=join(dir,'job.json'),temp=`${file}.${randomUUID()}.tmp`;try{await writeFile(temp,JSON.stringify(job),{mode:0o600,flag:'wx'});await rename(temp,file);}finally{await rm(temp,{force:true});}}
 private async command(args:string[],input?:unknown,cwd=this.cwd) {
  await mkdir(cwd,{recursive:true,mode:0o700});
  const owned=this.active;let opened=false;
  const result=await this.options.cli.run(args,{cwd,input:input===undefined?undefined:JSON.stringify(input),signal:owned?.signal,onDeviceUrl:value=>{
   if(args[0]!=='login'||!owned||this.active!==owned||owned.signal.aborted||this.current.state!=='login'||opened)return;
   const url=validateVercelDeviceUrl(value);if(!url)return;
   opened=true;this.current.approvalUrl=url;this.current.message='공식 브라우저에서 계정 연결을 승인하세요. 페이지가 열리지 않으면 승인 페이지 열기를 누르세요.';
   void Promise.resolve().then(()=>{
    if(this.active===owned&&!owned.signal.aborted&&this.current.state==='login'&&this.current.approvalUrl===url)return this.options.openExternal(url);
   }).catch(()=>{
    if(this.active===owned&&!owned.signal.aborted&&this.current.state==='login'&&this.current.approvalUrl===url)
     this.current.message='승인 페이지를 자동으로 열지 못했습니다. 승인 페이지 열기를 눌러 공식 브라우저에서 로그인하세요.';
   });
  }});
  if(result.code!==0){if(args[0]==='deploy')throw vercelCommandFailure(result.code);throw Error(genericError);}
  return result.stdout;
 }
 private async api(path:string,teamId?:string,body?:unknown,method?:string):Promise<any> {
  if(!path.startsWith('/v')||/[\r\n]/.test(path))throw Error('허용되지 않은 외부 작업입니다.');
  await mkdir(this.cwd,{recursive:true,mode:0o700});
  const response=await this.options.cli.requestApi(path,{cwd:this.cwd,teamId:teamId?safeId(teamId):undefined,body,method:method??(body===undefined?'GET':'POST'),signal:this.active?.signal});
  if(response.status<200||response.status>=300)throw new ApiFailure(response.status,response.body&&typeof response.body==='object'&&(response.body as any).error?(response.body as any).error:response.body);
  return response.body;
 }
 private async environmentApi(path:string,teamId:string):Promise<any> {
  try{return await this.api(path,teamId);}catch{throw Error(environmentReadError);}
 }
 private async environmentValues(projectId:string,teamId:string,keys:string[]):Promise<Record<string,string>> {
  const project=safeId(projectId),listing=jsonObject(await this.environmentApi(`/v10/projects/${project}/env`,teamId));
  if(!Array.isArray(listing.envs)||listing.envs.length>1000)throw Error('사이트 설정 목록의 형식을 확인하지 못했습니다. 기존 사이트·DB는 보존했습니다.');
  const references:EnvironmentReference[]=[];
  for(const key of keys) {
   const matches=listing.envs.filter((row:any)=>row&&typeof row==='object'&&!Array.isArray(row)&&row.key===key&&productionEnvironment(row));
   if(matches.length>1)throw Error('같은 Production 설정 항목이 중복되어 값을 임의로 선택하지 않았습니다. 공식 Vercel 설정을 확인하세요.');
   if(!matches.length)continue;
   const row=matches[0];
   if(!readableEnvironment(row))throw Error('이 사이트 설정은 민감 Secret이거나 지원하지 않는 보호 형식입니다. 값을 우회 조회하지 않았습니다. 공식 Vercel 설정과 조회 권한을 확인하세요.');
   references.push({id:safeId(row.id),key,type:row.type,target:row.target,visibility:row.visibility});
  }
  if(!references.length)throw Error('사이트의 필요한 Production 설정 항목이 아직 없습니다. DB와 사이트 연결 및 설정 반영을 확인한 뒤 이어서 준비하세요.');
  if(new Set(references.map(row=>row.id)).size!==references.length)throw Error('서로 다른 사이트 설정에 같은 식별자가 있어 조회를 중단했습니다.');
  const values:Record<string,string>={};
  for(const reference of references) {
   // 목록의 값은 사용하지 않고, 검증한 대상의 공식 단일 조회만 수행한다.
   const row=jsonObject(await this.environmentApi(`/v1/projects/${project}/env/${reference.id}`,teamId));
   if(!returnedEnvironmentScope(row,reference)||row.key!==reference.key||!readableEnvironment(row)||row.type!==reference.type||typeof row.decrypted!=='boolean'||(row.type==='encrypted'&&row.decrypted!==true))throw Error('요청한 Production 설정과 보호 상태가 일치하지 않아 값을 사용하지 않았습니다. 공식 Vercel 설정을 확인하세요.');
   if(typeof row.value!=='string'||!row.value||row.value.length>16_384||row.value.trim()!==row.value||/[\x00-\x1f\x7f]/.test(row.value))throw Error('사이트 설정 항목은 있지만 읽을 수 있는 값이 없습니다. 공식 Vercel에서 추가 인증·조회 권한과 DB 연결을 확인한 뒤 이어서 준비하세요.');
   values[reference.key]=row.value;
  }
  return values;
 }
 private async query<T>(action:()=>Promise<T>):Promise<T> {
  await this.load();if(this.active)throw Error('릴레이 작업이 진행 중입니다.');
  const owned=new AbortController();this.active=owned;let done!:()=>void;
  const completed=new Promise<void>(resolve=>{done=resolve;});this.activeDone=completed;
  try{return await action();}finally{if(this.active===owned)this.active=undefined;if(this.activeDone===completed)this.activeDone=undefined;done();}
 }
 private async operate(action:()=>Promise<void>,state:ManagedRelayStatus['state']='configuring',cancelMessage?:string) {
  await this.load();if(this.active)throw Error('릴레이 준비가 진행 중입니다.');
  const owned=new AbortController();this.active=owned;let done!:()=>void;const completed=new Promise<void>(resolve=>{done=resolve;});this.activeDone=completed;this.current={...this.current,state,verified:false,sourceOnline:false,approvalUrl:undefined};
  try{await action();}catch(error){
   const stopUnconfirmed=error instanceof Error&&(error as Error&{code?:string}).code==='VERCEL_STOP_UNCONFIRMED';
   const restoreUnconfirmed=error instanceof Error&&(error as Error&{code?:string}).code==='RELAY_UPDATE_RESTORE_UNCONFIRMED';
   this.current={...this.current,state:'error',message:stopUnconfirmed?'Vercel 작업 종료를 확인하지 못했습니다. 앱을 닫은 뒤 다시 실행하세요.':restoreUnconfirmed?(error as Error).message:owned.signal.aborted?(cancelMessage??(state==='login'?'Vercel 로그인을 취소했습니다. 다시 로그인할 수 있습니다.':'릴레이 준비를 취소했습니다. 이미 생성한 사이트·DB는 보존하며 이어서 준비할 수 있습니다.')):error instanceof Error&&/[가-힣]/.test(error.message)?error.message:genericError,approvalUrl:state==='login'?undefined:this.current.approvalUrl,canResume:Boolean(this.job&&this.job.phase!=='deployed')};
  }
  finally{if(this.active===owned)this.active=undefined;if(this.activeDone===completed)this.activeDone=undefined;done();}
  return this.status();
 }
 private async teams():Promise<RelayTeam[]> {
  const raw=await this.api('/v2/teams?limit=100');
  if(!Array.isArray(raw.teams))throw Error('Vercel 작업 공간을 확인하지 못했습니다.');
  return raw.teams.map((t:any)=>({id:safeId(t.id),slug:safeId(t.slug),name:String(t.name??t.slug).slice(0,150)}));
 }
 async connect(){return this.operate(async()=>{
  this.current.message='브라우저에서 Vercel에 로그인하세요. 가입이 필요하면 공식 화면에서 진행합니다.';
  await this.command(['login']);this.current.teams=await this.teams();
  if(!this.current.teams.length)throw Error('Vercel 작업 공간이 없습니다. 공식 브라우저에서 계정 설정을 마친 뒤 다시 연결하세요.');
  this.current={...this.current,state:'connected',message:'Vercel 계정을 연결했습니다. 우리 기관에서 사용할 작업 공간을 선택하세요.',approvalUrl:undefined};
 },'login');}
 private async ensureTeam(id:string) {
  safeId(id);const teams=await this.teams();this.current.teams=teams;
  const team=teams.find(t=>t.id===id);if(!team)throw Error('선택한 Vercel 작업 공간에 접근할 수 없습니다.');return team;
 }
 async catalog(teamId:string):Promise<RelayCatalog> {return this.query(()=>this.catalogData(teamId));}
 private async catalogData(teamId:string):Promise<RelayCatalog> {
  await this.ensureTeam(teamId);this.integration=await this.api('/v2/integrations/integration/neon',teamId);
  if(!Array.isArray(this.integration.products)||!this.integration.id)throw Error('DB 제품 정보를 가져오지 못했습니다.');
  safeId(this.integration.id);
  this.products=this.integration.products.map((p:any)=>({id:safeId(p.id),slug:safeId(p.slug),name:String(p.name).slice(0,150),fields:fields({properties:{...this.integration.metadataSchema?.properties,...p.metadataSchema?.properties},required:[...(this.integration.metadataSchema?.required??[]),...(p.metadataSchema?.required??[])]})}));
  return {products:this.products};
 }
 async plans(input:{teamId:string;productId:string;metadata:Record<string,unknown>}):Promise<RelayPlan[]> {return this.query(()=>this.plansData(input));}
 private async plansData(input:{teamId:string;productId:string;metadata:Record<string,unknown>}):Promise<RelayPlan[]> {
  await this.catalogData(input.teamId);const product=this.products.find(p=>p.id===input.productId);if(!product)throw Error('DB 제품을 선택하세요.');
  const metadata=normalizedMetadata(product,input.metadata);
  const data=await this.api(`/v1/integrations/integration/${this.integration.id}/products/${product.id}/plans?metadata=${encodeURIComponent(JSON.stringify(metadata))}`,input.teamId);
  if(!Array.isArray(data.plans)||!data.plans.length)throw Error('선택 가능한 요금제를 확인하지 못했습니다. 임의로 유료 요금제를 고르지 않습니다.');
  return data.plans.filter((p:any)=>p.disabled!==true).map((p:any)=>({id:safeId(p.id),name:planText(p.name,150)??'이름 미확인 요금제',description:describePlan(jsonObject(p)),type:planText(p.type,100)??''}));
 }
 async provision(input:RelayProvisionInput) {
  return this.operate(async()=>{
   if(this.job)throw Error('이미 준비 중이거나 배포한 릴레이가 있습니다. ‘이어서 준비’ 또는 연결 확인을 사용하세요.');
   if(!input||input.consent!==true||!/^gukgam-[a-z0-9][a-z0-9-]{2,45}$/.test(input.projectName))throw Error('사이트 이름과 외부 사이트·선택 요금제 생성 동의를 확인하세요. 이름은 gukgam-으로 시작하는 영문 소문자·숫자·하이픈을 사용합니다.');
   if(typeof input.operatorPasscode!=='string'||Array.from(input.operatorPasscode).length<16||input.operatorPasscode.length>200||typeof input.viewerPasscode!=='string'||Array.from(input.viewerPasscode).length<12||input.viewerPasscode.length>200||input.operatorPasscode===input.viewerPasscode||/[\x00-\x1f]/.test(input.operatorPasscode+input.viewerPasscode))throw Error('운영자 암호는 16자, 열람 암호는 12자 이상이며 서로 달라야 합니다.');
   if(!(await this.options.settings.read()).organization)throw Error('먼저 기관명을 저장하세요.');
   const plans=await this.plansData(input);if(!plans.some(p=>p.id===input.planId))throw Error('현재 제공되는 DB 요금제를 다시 선택하세요.');
   const product=this.products.find(p=>p.id===input.productId)!;
   const metadata=normalizedMetadata(product,input.metadata),installationMetadata:Record<string,unknown>={};
   const productKeys=new Set(Object.keys(this.integration.products.find((p:any)=>p.id===product.id).metadataSchema?.properties??{}));
   for(const key of Object.keys(metadata))if(!productKeys.has(key)){installationMetadata[key]=metadata[key];delete metadata[key];}
   const team=this.current.teams.find(t=>t.id===input.teamId)!;
   const job:Job={version:1,id:randomUUID(),teamId:team.id,teamSlug:team.slug,projectName:input.projectName,productId:product.id,productSlug:product.slug,integrationId:this.integration.id,planId:input.planId,metadata,installationMetadata,resourceName:`${input.projectName}-${randomBytes(4).toString('hex')}`,phase:'prepared'};
   const secrets:Secrets={operatorPasscode:input.operatorPasscode,viewerPasscode:input.viewerPasscode,token:randomBytes(32).toString('base64url'),key:randomBytes(48).toString('base64url')};
   await this.options.vault.set('relay-provision',secrets);this.job=job;await this.persist();
   this.current={...this.current,projectName:job.projectName,teamId:job.teamId,canResume:true};await this.advance(false);
  });
 }
 async resume(){return this.operate(async()=>{if(!this.job)throw Error('이어서 준비할 릴레이가 없습니다.');await this.ensureTeam(this.job.teamId);await this.advance(true);});}
 async updateExistingSite(){return this.operate(async()=>{
  const job=this.job;
  if(!job||job.phase!=='deployed')throw Error('이미 배포가 완료된 관리 사이트만 업데이트할 수 있습니다. 먼저 기존 구성을 마치세요.');
  const projectId=safeId(job.projectId),teamId=safeId(job.teamId),oldUrl=relayUrl(job.url!);
  if(projectId.startsWith('-')||teamId.startsWith('-'))throw Error('기존 프로젝트·팀 식별자가 실행 옵션으로 해석될 수 있어 업데이트를 중단했습니다.');
  const version=siteUpdateVersion(this.options.version);
  if(!version)throw Error('동봉 앱의 버전을 확인하지 못해 사이트 업데이트를 시작하지 않았습니다. 설치 파일을 확인하세요.');
  const owned=this.active,check=()=>{if(!owned||this.active!==owned||owned.signal.aborted)throw Error('사이트 업데이트를 취소했습니다.');};
  const started=Date.now(),duration=()=>Math.min(86_400_000,Math.max(0,Date.now()-started));
  let outcome:SiteUpdateStatus={state:'updating',stage:'preparing',deployAttempted:false,targetVersion:version};
  const publish=(value:SiteUpdateStatus)=>{
   outcome=siteUpdateStatus(value);this.current={...this.current,siteUpdate:{...outcome},canResume:false};
   try{this.options.onSiteUpdate?.({...outcome});}catch{}
  };
  const record=async(stage:SiteUpdateStatus['stage'],deployAttempted=outcome.deployAttempted)=>{
   publish({...outcome,state:'updating',stage,deployAttempted,durationMs:duration()});
   const next={...job,siteUpdate:{...outcome}};
   try{await this.persist(next);this.job=next;}catch{throw new UpdateFailure('JOURNAL_FAILED');}
  };
  let directory:string|undefined,adopting=false,settingsBefore:Record<string,unknown>|undefined,token:string|undefined;
  try {
   await record('preparing');check();
   await this.ensureTeam(teamId);check();
   const {hasRelayToken,secureStorageWarning,...saved}=await this.options.settings.read();check();
   if(secureStorageWarning||!hasRelayToken||saved.relayUrl!==oldUrl)throw Error('기존 사이트와 현재 앱의 연결 설정을 먼저 확인하세요.');
   token=await this.options.vault.get<string>('relay');check();
   if(typeof token!=='string'||!token)throw Error('기존 사이트의 보호된 연결 정보를 확인하지 못했습니다.');
   settingsBefore=saved;
   const parent=join(this.options.userData,'relay');await mkdir(parent,{recursive:true,mode:0o700});check();
   directory=await mkdtemp(join(parent,'site-update-'));check();
   await this.prepareTemplate(directory);check();
   await mkdir(join(directory,'.vercel'),{mode:0o700});
   await writeFile(join(directory,'.vercel','project.json'),JSON.stringify({projectId,orgId:teamId,projectName:job.projectName}),{mode:0o600,flag:'wx'});check();
   this.current.message='기존 프로젝트의 사이트 코드만 업데이트하고 있습니다. DB·요금제·암호·환경 설정과 이 PC의 기록은 변경하거나 업로드하지 않습니다.';
   // pinned CLI의 명시 --project는 failIfNotFound를 켜므로 삭제·권한 상실 시 신규 생성으로 전환하지 않는다.
   // 호출 직전의 보수적 표지다. 실제 서버 배포 생성이나 CLI 시작 성공을 뜻하지 않는다.
   await record('deploying',true);check();
   const result=await this.command(['deploy','--prod','--yes','--scope',teamId,'--project',projectId],undefined,directory);check();
   outcome={...outcome,exitCode:0};
   const urls=result.trim().split(/\s+/).filter(value=>/^https:\/\/[^\s]+\.vercel\.app\/?$/.test(value));
   if(urls.length!==1)throw new UpdateFailure('DEPLOY_URL_INVALID');
   const nextUrl=relayUrl(urls[0]);
   // 공식 CLI와 같은 deployment 조회로 기존 프로젝트 바인딩을 확인한 뒤에만 암호를 보낸다.
   await record('binding');check();
   const deployment=jsonObject(await this.api(`/v13/deployments/${encodeURIComponent(new URL(nextUrl).hostname)}`,teamId));check();
   if(deployment.projectId!==projectId||deployment.url!==new URL(nextUrl).hostname)throw new UpdateFailure('BINDING_FAILED');
   await record('verifying');check();
   const verified=await this.checkSite(false,nextUrl);check();
   await record('saving');check();
   const next={...job,url:nextUrl,siteUpdate:{...outcome}};
   adopting=true;await this.persist(next);check();
   await this.options.settings.save({...saved,relayUrl:nextUrl,relayToken:token});check();
   const succeeded:SiteUpdateStatus={state:'succeeded',stage:'completed',deployAttempted:true,targetVersion:version,version,exitCode:0,durationMs:duration()};
   const completed={...next,siteUpdate:succeeded};await this.persist(completed);check();
   this.job=completed;adopting=false;publish(succeeded);
   this.current={...verified,siteUpdate:{...outcome},url:nextUrl,approvalUrl:undefined,canResume:false,sourceOnline:false,message:`기존 사이트 코드를 업데이트하고 로그인·DB를 확인했습니다. ${nextUrl!==oldUrl?'사이트 주소가 새 배포 주소로 바뀌었습니다. ':''}운영자·열람 암호는 그대로입니다. 기록 엔진을 시작한 뒤 현재 PC 연결을 다시 확인하세요.`};
  }catch(error){
   const cancelled=owned?.signal.aborted===true;
   const fallback:Record<string,NonNullable<SiteUpdateStatus['knownCode']>>={preparing:'PREPARATION_FAILED',deploying:'DEPLOY_FAILED',binding:'BINDING_FAILED',verifying:'SITE_VERIFICATION_FAILED',saving:'SAVE_FAILED'};
   const details=vercelFailureMetadata(error);
   let failed:SiteUpdateStatus={...outcome,state:outcome.deployAttempted?'unknown':cancelled?'cancelled':'failed',
    knownCode:cancelled?'VERCEL_CANCELLED':error instanceof UpdateFailure?error.updateCode:error instanceof ApiFailure?'API_HTTP_ERROR':details.knownCode??fallback[outcome.stage]??'PREPARATION_FAILED',
    ...(error instanceof ApiFailure&&Number.isSafeInteger(error.status)&&error.status>=100&&error.status<=599?{httpStatus:error.status}:{}),
    ...(details.exitCode!==undefined?{exitCode:details.exitCode}:{}),durationMs:duration()};
   publish(failed);
   if(adopting){
    try{await this.persist({...job,siteUpdate:failed});await this.options.settings.save({...settingsBefore,relayUrl:oldUrl,relayToken:token});}
    catch{
     const uncertain={...failed,knownCode:'RESTORE_UNCONFIRMED' as const};publish(uncertain);
     try{const saved={...job,siteUpdate:uncertain};await this.persist(saved);this.job=saved;}catch{}
     throw Object.assign(Error('사이트 주소 저장의 복원을 확인하지 못했습니다. 앱을 닫고 기존 사이트 주소와 연결 설정을 확인하세요. 자동 재배포하지 않았습니다.'),{code:'RELAY_UPDATE_RESTORE_UNCONFIRMED'});
    }
   }
   const failedJob={...job,siteUpdate:failed};
   try{await this.persist(failedJob);this.job=failedJob;}
   catch{failed={...failed,knownCode:'JOURNAL_FAILED'};publish(failed);}
   this.current={...this.current,url:oldUrl,verified:false,sourceOnline:false};
   if(error instanceof Error&&(error as Error&{code?:string}).code==='VERCEL_STOP_UNCONFIRMED')throw error;
   throw Error('기존 사이트 업데이트 결과를 확인하지 못했습니다. 이전 주소와 DB·암호는 유지했습니다. 기존 프로젝트에 새 배포가 생성됐을 수 있으므로 공식 배포 내역을 먼저 확인하세요. 자동 재배포하지 않았습니다.');
  }finally{
   // 이 호출이 만든 전용 형제 폴더만 정리한다. 성공한 배포를 로컬 정리 오류로 실패 처리하지 않는다.
   if(directory&&dirname(resolve(directory))===resolve(this.options.userData,'relay')&&basename(directory).startsWith('site-update-'))
    await rm(directory,{recursive:true,force:true}).catch(()=>{});
  }
 },'configuring','사이트 업데이트를 취소했습니다. 이전 주소와 DB·암호는 유지했습니다. 배포가 생성됐을 수 있으므로 공식 배포 내역을 확인하세요. 자동 재배포하지 않습니다.');}
 async cancel(){this.active?.abort();await this.activeDone;return this.status();}
 async openApproval(){await this.load();if(this.active?.signal.aborted)throw Error('취소 중인 작업의 승인 페이지는 열 수 없습니다. 취소가 끝난 뒤 현재 상태를 확인하세요.');if(!this.current.approvalUrl)throw Error('현재 열어야 할 공식 승인 화면이 없습니다.');const url=new URL(this.current.approvalUrl);if(url.protocol!=='https:'||url.username||url.password||url.port||!['vercel.com','console.neon.tech','neon.tech'].includes(url.hostname))throw Error('공식 승인 주소가 아닙니다.');await this.options.openExternal(url.href);}
 private async prepareTemplate(destination=this.cwd) {
  const info=await stat(this.options.templateDir);if(!info.isDirectory())throw Error('동봉한 릴레이 소스를 찾지 못했습니다. 설치 파일을 확인하세요.');
  // 설치 자산만 복사한다. 직원 PC의 저장 기록·계정·환경 파일은 배포 대상이 아니다.
  await cp(this.options.templateDir,destination,{recursive:true,force:true,filter:source=>!relative(this.options.templateDir,source).split(/[\\/]/).some(p=>excludedTemplateParts.has(p)||p.startsWith('.env'))});
 }
 private async advance(resuming:boolean) {
  const job=this.job!,secrets=await this.options.vault.get<Secrets>('relay-provision');if(!secrets)throw Error('보호된 릴레이 연결 정보를 읽지 못했습니다.');
  await this.prepareTemplate();
  if(job.phase==='prepared') {
   this.current.message='우리 기관 전용 릴레이 사이트를 준비하고 있습니다.';
   job.phase='creating-project';await this.persist();
   let project:any;
   try{project=await this.api('/v11/projects',job.teamId,{name:job.projectName,framework:'fastapi',ssoProtection:null,environmentVariables:[{key:'GUKGAM_DESKTOP_SETUP_ID',value:job.id,type:'plain',target:['production']}]});}
   catch(error){if(error instanceof ApiFailure&&[400,401,403,409,422].includes(error.status)){
    this.job=undefined;await rm(join(this.options.userData,'relay','job.json'),{force:true});await this.options.vault.delete('relay-provision');this.current={...this.current,projectName:undefined,canResume:false};
    if(error.status===409)throw Error('이미 사용 중인 사이트 이름입니다. 다른 이름을 입력해 다시 구성하세요. 기존 사이트는 변경하지 않았습니다.');
   }throw error;}
   job.projectId=safeId(project.id);job.phase='project';await this.persist();
  } else if(job.phase==='creating-project') {
   const project=await this.api(`/v9/projects/${job.projectName}`,job.teamId);
   const values=await this.environmentValues(safeId(project.id),job.teamId,['GUKGAM_DESKTOP_SETUP_ID']);
   if(values.GUKGAM_DESKTOP_SETUP_ID!==job.id)throw Error('이 앱이 요청한 사이트인지 확인하지 못했습니다. 새 사이트를 중복 생성하지 않습니다.');
   job.projectId=safeId(project.id);job.phase='project';await this.persist();
  }
  await mkdir(join(this.cwd,'.vercel'),{recursive:true,mode:0o700});
  await writeFile(join(this.cwd,'.vercel','project.json'),JSON.stringify({projectId:job.projectId,orgId:job.teamId,projectName:job.projectName}),{mode:0o600});
  if(job.phase==='project') {
   this.current.message='공식 브라우저의 DB 이용 동의를 확인하고 있습니다.';
   const installations=await this.api(`/v2/integrations/configurations?view=account&installationType=marketplace&integrationIdOrSlug=${job.integrationId}`,job.teamId);
   if(!Array.isArray(installations))throw Error('DB 이용 동의 상태를 확인하지 못했습니다.');
   const matching=installations.filter((i:any)=>i.ownerId===job.teamId&&i.installationType==='marketplace');
   if(matching.length>1)throw Error('이 작업 공간에 DB 연결 계정이 여러 개 있습니다. 공식 Vercel 관리 화면에서 사용할 연결 계정을 정리하거나 담당 관리자에게 확인하세요. 청구 계정을 임의로 선택하지 않았습니다.');
   const installation=matching[0];
   if(!installation) {
    this.current={...this.current,state:'approval',message:'공식 브라우저에서 Neon DB 이용 약관·요금제를 확인하세요. 승인 후 이 앱의 ‘이어서 준비’를 누르면 계속합니다.',approvalUrl:officialApproval(`https://vercel.com/${encodeURIComponent(job.teamSlug)}/~/integrations/accept-terms/neon?source=cli`),canResume:true};return;
   }
   job.phase='creating-database';await this.persist();this.current.message='선택한 DB를 만들고 있습니다. 이 단계는 중복 실행하지 않습니다.';
   // 공식 CLI와 같은 API를 호출하여 불필요한 npx·에이전트 스킬 설치를 피한다.
   let resource:any;
   try{resource=await this.api(`/v1/integrations/integration/neon/marketplace/auto-provision/${job.productSlug}`,job.teamId,{name:job.resourceName,metadata:job.metadata,installationMetadata:job.installationMetadata,acceptedPolicies:{},billingPlanId:job.planId,installationId:safeId(installation.id),source:'cli'});}
   catch(error){
    const fallback=error instanceof ApiFailure&&error.status===422?error.body:undefined;
    if(fallback&&fallback.kind!=='provisioned'&&typeof fallback.url==='string'&&fallback.integration&&fallback.product){
     let approval:string;try{const u=new URL(fallback.url);if(u.protocol!=='https:'||u.username||u.password||u.port||u.hash||!['vercel.com','console.neon.tech','neon.tech'].includes(u.hostname))throw Error();u.searchParams.set('defaultResourceName',job.resourceName);u.searchParams.set('source','cli');u.searchParams.set('metadata',JSON.stringify(job.metadata));u.searchParams.set('installationMetadata',JSON.stringify(job.installationMetadata));u.searchParams.set('projectSlug',job.projectName);u.searchParams.set('planId',job.planId);u.searchParams.set('environment','production');approval=u.href;}catch{throw Error('외부 서비스의 승인 주소를 확인하지 못했습니다. 중복 생성을 중단했습니다.');}
     job.pendingApprovalUrl=approval;await this.persist();this.current={...this.current,state:'approval',approvalUrl:approval,canResume:true,message:'공식 브라우저에서 선택한 DB의 추가 구성을 완료하세요. 이어서 준비할 때 이미 생성한 DB만 확인하며 새 DB 생성 요청은 반복하지 않습니다.'};return;
    }throw error;
   }
   if(resource.kind!=='provisioned'||!resource.resource?.id)throw Error('DB 생성 결과를 확인하지 못했습니다. 중복 생성 없이 기존 결과를 먼저 확인합니다.');
   job.resourceId=safeId(resource.resource.id);job.phase='database';await this.persist();
  } else if(job.phase==='creating-database') {
   const response=await this.command(['integration','list','--all','--json','--scope',job.teamId]);const listing=parse(response);
   const rows=Array.isArray(listing)?listing:listing.resources??listing.stores;
   const matches=Array.isArray(rows)?rows.filter((r:any)=>r.name===job.resourceName):[];
   if(matches.length!==1){if(job.pendingApprovalUrl){this.current={...this.current,state:'approval',approvalUrl:job.pendingApprovalUrl,canResume:true,message:'아직 이 요청 이름의 DB가 확인되지 않았습니다. 공식 브라우저에서 구성을 마친 뒤 다시 확인하세요. 새 DB 생성 요청은 보내지 않았습니다.'};return;}throw Error('이전 DB 생성 결과가 아직 확인되지 않았습니다. 중복 DB를 만들지 않았습니다. 잠시 뒤 다시 이어서 준비하세요.');}
   job.resourceId=safeId(matches[0].id);delete job.pendingApprovalUrl;job.phase='database';await this.persist();
  }
  if(job.phase==='database') {
   this.current.message='DB와 사이트를 연결하고 접속 암호를 보호된 환경에 등록하고 있습니다.';
   // 연결이 이미 있어도 credentials 조회로 최종 연결을 검증한다.
   try{await this.api(`/v1/storage/stores/${job.resourceId}/connections`,job.teamId,{envVarEnvironments:['production'],projectId:job.projectId,type:'integration'});}catch(error){if(!resuming)throw error;}
   const connections=await this.environmentValues(job.projectId!,job.teamId,['DATABASE_URL_UNPOOLED','DATABASE_URL']);
   const dsn=databaseUrl(connections.DATABASE_URL_UNPOOLED??connections.DATABASE_URL);
   await this.options.initializeDatabase(dsn,this.active?.signal);
   const values:Record<string,string>={GUKGAM_OPERATOR_PASSCODE:secrets.operatorPasscode,GUKGAM_VIEWER_PASSCODE:secrets.viewerPasscode,GUKGAM_SECRET_KEY:secrets.key,GUKGAM_AGENT_TOKEN:secrets.token,GUKGAM_CLOUD_READ_MODE:'source',GUKGAM_CLOUD_MIRROR_SENSITIVE:'0'};
   await this.api(`/v10/projects/${job.projectId}/env?upsert=true`,job.teamId,Object.entries(values).map(([key,value])=>({key,value,type:'encrypted',target:['production']})));
   job.phase='configured';await this.persist();
  }
  if(job.phase==='configured') {
   this.current.message='동봉한 릴레이 페이지를 배포하고 있습니다. 이 PC의 회의 기록은 업로드하지 않습니다.';
   const result=await this.command(['deploy','--prod','--yes','--scope',job.teamId]);
   const urls=result.trim().split(/\s+/).filter(v=>/^https:\/\/[^\s]+\.vercel\.app\/?$/.test(v));
   if(urls.length!==1)throw Error('배포 주소를 확인하지 못했습니다. 이미 생성한 프로젝트는 보존했습니다.');
   job.url=relayUrl(urls[0]);job.phase='deployed';await this.persist();
  }
  if(job.phase==='deployed') {
   const current=await this.options.settings.read();const {hasRelayToken,secureStorageWarning,...settings}=current;
   await this.options.settings.save({...settings,relayUrl:job.url,relayToken:secrets.token});
   this.current={...this.current,state:'ready',message:'릴레이 주소를 저장했습니다. 기록 엔진을 시작한 뒤 실제 연결 확인을 실행하세요.',url:job.url,approvalUrl:undefined,canResume:false,verified:false,sourceOnline:false};
   await this.verifySite(false);
  }
 }
 private async response(url:string,init:RequestInit):Promise<Response> {return (this.options.fetcher??fetch)(url,{...init,redirect:'error',signal:this.active?AbortSignal.any([this.active.signal,AbortSignal.timeout(35_000)]):AbortSignal.timeout(35_000)});}
 private async body(response:Response):Promise<any> {
  if(!response.ok||!response.headers.get('content-type')?.includes('application/json')||!response.body)throw Error('배포된 릴레이가 준비되지 않았습니다. 공식 배포 결과와 접근 권한을 확인하세요.');
  let size=0;const chunks:Uint8Array[]=[];const reader=response.body.getReader();try{while(true){const part=await reader.read();if(part.done)break;size+=part.value.length;if(size>1_000_000)throw Error('릴레이 응답이 너무 큽니다.');chunks.push(part.value);}}finally{void reader.cancel().catch(()=>{});reader.releaseLock();}return parse(Buffer.concat(chunks).toString('utf8'));
 }
 private async checkSite(requireSource:boolean,targetUrl?:string):Promise<ManagedRelayStatus> {
  const job=this.job!,secrets=await this.options.vault.get<Secrets>('relay-provision');if(!job?.url||!secrets)throw Error('먼저 릴레이 구성을 마치세요.');
  const origin=relayUrl(targetUrl??job.url),headers={'Content-Type':'application/json','X-Requested-With':'gukgam','Origin':origin};
  const login=await this.response(`${origin}/api/auth/login`,{method:'POST',headers,body:JSON.stringify({passcode:secrets.operatorPasscode,name:'설치 앱 연결 확인'})});await this.body(login);
  const cookie=login.headers.get('set-cookie')?.split(';')[0];if(!cookie?.startsWith('__Host-gukgam_auth='))throw Error('릴레이 로그인 결과를 확인하지 못했습니다.');
  const health=await this.body(await this.response(`${origin}/api/health`,{headers:{Cookie:cookie}}));
  if(health.ok!==true||health.cloud!==true||health.read_mode!=='source')throw Error('배포된 릴레이가 원본 PC 열람 방식이 아닙니다.');
  const online=health.agent?.online===true&&health.agent?.read_mode==='source';
  const result:ManagedRelayStatus={...this.current,state:'ready',verified:true,sourceOnline:false,canResume:false,message:'릴레이 로그인·DB·원본 열람 모드를 확인했습니다. 기록 엔진을 시작하면 현재 PC의 자료를 중계합니다.'};
  if(requireSource) {
   if(!online){result.message='릴레이는 정상이며 현재 PC의 연결을 기다리고 있습니다. 기록 엔진이 켜져 있는지 확인하세요.';return result;}
   const expected=await this.options.expectedConnection?.();
   if(!expected||health.agent?.connection_id!==expected){result.message='릴레이는 정상이며 현재 PC의 연결 식별자를 기다리고 있습니다. 이 앱의 기록 엔진을 켠 뒤 다시 확인하세요.';return result;}
   const data=await this.body(await this.response(`${origin}/api/sessions`,{headers:{Cookie:cookie}}));
   if(!Array.isArray(data)&&!Array.isArray(data.items))throw Error('원본 PC의 회의 목록 응답을 확인하지 못했습니다.');
   result.sourceOnline=true;result.message='이 PC 연결과 릴레이를 통한 실제 회의 목록 조회를 확인했습니다. 동료에게 릴레이 주소와 열람 암호를 안내할 수 있습니다.';
  }
  return result;
 }
 private async verifySite(requireSource:boolean){this.current=await this.checkSite(requireSource);}
 async verify(){return this.operate(()=>this.verifySite(true));}
}
