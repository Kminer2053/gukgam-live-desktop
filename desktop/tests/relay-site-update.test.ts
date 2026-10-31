import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,readdir,rm} from 'node:fs/promises';
import {join,resolve,basename} from 'node:path';
import {tmpdir} from 'node:os';
import {RelayProvisioner,type Cli} from '../src/relay/provisioner';
import {siteUpdateStatus,type SiteUpdateStatus} from '../src/relay/contracts';

const oldUrl='https://existing-fixture.vercel.app',newUrl='https://updated-fixture.vercel.app';
function gate(){let release!:()=>void;return {promise:new Promise<void>(done=>{release=done;}),release:()=>release()};}
async function fixture(options:{phase?:string;projectId?:string;deploymentProject?:string;deploymentUrl?:string;stdout?:string;code?:number;missingProject?:boolean;deployGate?:Promise<void>;teamGate?:Promise<void>;unhealthy?:boolean;noTeam?:boolean;apiStatus?:number;settingsFailure?:boolean;cancelOnSave?:boolean;restoreFailure?:boolean;version?:string}={}) {
 const dir=await mkdtemp(join(tmpdir(),'gukgam-site-update-')),template=join(dir,'template'),relayDir=join(dir,'relay');
 await mkdir(template);await mkdir(relayDir);await writeFile(join(template,'index.py'),'# SYNTHETIC_BUNDLED_SITE_CODE');
 await writeFile(join(template,'.env'),'SYNTHETIC_ENV_NOT_FOR_DEPLOYMENT');await mkdir(join(template,'.vercel'));await writeFile(join(template,'.vercel','project.json'),'SYNTHETIC_LINK_NOT_FOR_DEPLOYMENT');
 for(const name of ['docs','outputs','records','raw','audio','vault','account','.git']){await mkdir(join(template,name));await writeFile(join(template,name,'private.txt'),'SYNTHETIC_PRIVATE_NOT_FOR_DEPLOYMENT');}
 const job={version:1,id:'job_fixture',teamId:'team_fixture',teamSlug:'fixture-team',projectName:'gukgam-update-fixture',productId:'product_fixture',productSlug:'neon-fixture',integrationId:'integration_fixture',planId:'plan_fixture',metadata:{region:'region-fixture',auth:false},installationMetadata:{realtime:false},resourceName:'store-fixture',phase:options.phase??'deployed',projectId:options.projectId??'project_fixture',resourceId:'resource_fixture',url:oldUrl};
 const jobFile=join(relayDir,'job.json');await writeFile(jobFile,JSON.stringify(job));
 const token='SYNTHETIC_EXISTING_RELAY_TOKEN',values=new Map<string,unknown>([['relay',token],['relay-provision',{operatorPasscode:'SYNTHETIC_EXISTING_OPERATOR',viewerPasscode:'SYNTHETIC_EXISTING_VIEWER',token,key:'SYNTHETIC_EXISTING_KEY'}],['vercel-cli-auth',{SYNTHETIC_AUTH:'UNCHANGED'}]]);
 const originalSecrets=structuredClone([...values]);let saved:any={organization:'합성기관',relayUrl:oldUrl,model:'fixture-model',hasRelayToken:true},saves=0;
 const calls:Array<{kind:'api'|'deploy';path?:string;args?:string[];teamId?:string;method?:string;body?:unknown;cwd:string}>=[],fetches:Array<{url:string;method:string;body?:BodyInit|null}>=[];
 let link:any,copiedSource='',copiedNames:string[]=[],provisioner:RelayProvisioner;
 const cli:Cli={requestApi:async(path,o)=>{
  calls.push({kind:'api',path,teamId:o.teamId,method:o.method,body:o.body,cwd:o.cwd});
  if(path==='/v2/teams?limit=100'){await options.teamGate;return options.apiStatus?{status:options.apiStatus,body:{error:{code:'SYNTHETIC_PRIVATE_CODE',message:'SYNTHETIC_PRIVATE_API'}}}:{status:200,body:{teams:options.noTeam?[]:[{id:'team_fixture',slug:'fixture-team',name:'합성기관'}]}};}
  if(path==='/v13/deployments/updated-fixture.vercel.app')return {status:200,body:{projectId:options.deploymentProject??'project_fixture',url:options.deploymentUrl??'updated-fixture.vercel.app'}};
  throw Error('UNEXPECTED_REMOTE_ACTION');
 },run:async(args,o)=>{
  calls.push({kind:'deploy',args:[...args],cwd:o.cwd});assert.deepEqual(args,['deploy','--prod','--yes','--scope','team_fixture','--project','project_fixture']);
  assert.equal(o.input,undefined);assert.equal(resolve(o.cwd).startsWith(resolve(relayDir)+requireSeparator()),true);assert.match(basename(o.cwd),/^site-update-/);
  link=JSON.parse(await readFile(join(o.cwd,'.vercel','project.json'),'utf8'));copiedSource=await readFile(join(o.cwd,'index.py'),'utf8');copiedNames=await readdir(o.cwd);
  await options.deployGate;return {code:options.missingProject?1:options.code??0,stdout:options.missingProject?'':options.stdout??newUrl+'\n',stderr:'SYNTHETIC_PRIVATE_CLI_STDERR'};
 }};
 const settings={read:async()=>({...saved}),save:async(raw:unknown)=>{
  const {relayToken,...publicSettings}=raw as any;saves++;assert.equal(relayToken,token);
  if(options.restoreFailure)throw Error('SYNTHETIC_PRIVATE_STORE_FAILURE');
  if(options.settingsFailure&&saves===1)throw Error('SYNTHETIC_PRIVATE_STORE_FAILURE');
  saved={...publicSettings,hasRelayToken:true};if(options.cancelOnSave&&saves===1)void provisioner.cancel();return {...saved};
 }};
 const events:SiteUpdateStatus[]=[];
 const config={userData:dir,templateDir:template,cli,settings,version:options.version??'0.1.0-preview.14',onSiteUpdate:(value:SiteUpdateStatus)=>events.push(structuredClone(value)),vault:{get:async<T>(key:string)=>values.get(key) as T|undefined,set:async()=>{throw Error('SECRET_MUTATION_FORBIDDEN');},delete:async()=>{throw Error('SECRET_DELETE_FORBIDDEN');}},initializeDatabase:async()=>{throw Error('DB_MUTATION_FORBIDDEN');},openExternal:async()=>{throw Error('BROWSER_FORBIDDEN');},fetcher:(async(url:any,init:RequestInit={})=>{
  const value=String(url);fetches.push({url:value,method:init.method??'GET',body:init.body});assert.equal(new URL(value).origin,newUrl);assert.equal(init.redirect,'error');
  if(value.endsWith('/api/auth/login'))return new Response('{"ok":true}',{headers:{'content-type':'application/json','set-cookie':'__Host-gukgam_auth=SYNTHETIC; Secure; HttpOnly'}});
  if(value.endsWith('/api/health'))return new Response(JSON.stringify({ok:!options.unhealthy,cloud:true,read_mode:'source',agent:{online:false}}),{headers:{'content-type':'application/json'}});
  throw Error('SOURCE_OR_OTHER_REQUEST_FORBIDDEN');
 }) as typeof fetch};
 provisioner=new RelayProvisioner(config);
 return {dir,relayDir,provisioner,job,calls,fetches,settings,values,originalSecrets,events,config,restart:()=>new RelayProvisioner(config),get link(){return link;},get copiedSource(){return copiedSource;},get copiedNames(){return copiedNames;},setup:()=>saved,jobNow:async()=>JSON.parse(await readFile(jobFile,'utf8')),close:()=>rm(dir,{recursive:true,force:true})};
}
function requireSeparator(){return process.platform==='win32'?'\\':'/';}
function expectedJob(job:Record<string,unknown>,outcome?:SiteUpdateStatus,url=job.url){return {...job,url,...(outcome&&outcome.state!=='unverified'?{siteUpdate:outcome}:{})};}

test('명시 업데이트 한 번은 기존 팀·프로젝트에 동봉 코드만 배포하고 검증 후 주소만 갱신한다',async()=>{
 const f=await fixture();try{
  assert.equal(f.calls.length,0);await f.provisioner.status();assert.equal(f.calls.length,0,'상태 조회는 자동 업데이트하지 않음');
  const state=await f.provisioner.updateExistingSite();assert.equal(state.state,'ready');assert.equal(state.url,newUrl);assert.equal(state.verified,true);assert.equal(state.sourceOnline,false);
  assert.match(state.message,/주소가 새 배포 주소로 바뀌/);assert.match(state.message,/암호는 그대로/);
  assert.deepEqual(f.link,{projectId:'project_fixture',orgId:'team_fixture',projectName:'gukgam-update-fixture'});assert.match(f.copiedSource,/BUNDLED_SITE_CODE/);assert.equal(f.copiedNames.includes('.env'),false);
  assert.equal(f.copiedNames.some(name=>['docs','outputs','records','raw','audio','vault','account','.git'].includes(name)),false);
  assert.deepEqual(f.calls.map(c=>c.kind==='deploy'?'deploy':c.path),['/v2/teams?limit=100','deploy','/v13/deployments/updated-fixture.vercel.app']);
  assert.ok(f.calls.filter(c=>c.kind==='api').every(c=>c.method==='GET'&&c.body===undefined));assert.equal(f.calls[2].teamId,'team_fixture');
  assert.deepEqual(await f.jobNow(),expectedJob(f.job,state.siteUpdate,newUrl));assert.equal(f.setup().relayUrl,newUrl);assert.equal(f.setup().organization,'합성기관');
  assert.deepEqual([...f.values],f.originalSecrets);assert.equal(f.fetches.length,2);assert.equal(f.fetches.some(v=>v.url.endsWith('/api/sessions')),false);
  assert.equal(JSON.stringify(state).includes('SYNTHETIC'),false);assert.equal((await readdir(f.relayDir)).some(name=>name.startsWith('site-update-')),false);
  assert.equal(state.siteUpdate?.state,'succeeded');assert.equal(state.siteUpdate?.stage,'completed');
  assert.equal(state.siteUpdate?.version,'0.1.0-preview.14');assert.equal(state.siteUpdate?.targetVersion,state.siteUpdate?.version);
  assert.deepEqual(f.events.map(value=>value.stage),['preparing','deploying','binding','verifying','saving','completed']);
  assert.equal((await f.restart().status()).siteUpdate?.version,'0.1.0-preview.14');
 }finally{await f.close();}
});

test('완료 전 단계와 손상된 프로젝트 ID는 업데이트·리소스 생성·비밀 전송을 하지 않는다',async()=>{
 for(const phase of ['prepared','creating-project','project','creating-database','database','configured']){
  const f=await fixture({phase});try{const state=await f.provisioner.updateExistingSite();assert.equal(state.state,'error');assert.equal(f.calls.length,0);assert.equal(f.fetches.length,0);assert.deepEqual(await f.jobNow(),f.job);}finally{await f.close();}
 }
 const f=await fixture({projectId:'../project'});try{await assert.rejects(f.provisioner.updateExistingSite(),/중복/);assert.equal(f.calls.length,0);assert.equal(f.fetches.length,0);}finally{await f.close();}
 for(const projectId of ['--yes','--prod','--scope','-p']){const bad=await fixture({projectId});try{const state=await bad.provisioner.updateExistingSite();assert.equal(state.state,'error');assert.equal(bad.calls.length,0);assert.equal(bad.fetches.length,0);assert.deepEqual(await bad.jobNow(),bad.job);}finally{await bad.close();}}
});

test('삭제·이전된 프로젝트는 명시 project ID의 failIfNotFound 종료를 보존하고 새 구성으로 재시도하지 않는다',async()=>{
 const f=await fixture({missingProject:true});try{
  const state=await f.provisioner.updateExistingSite();assert.equal(state.state,'error');assert.equal(state.url,oldUrl);
  const deployments=f.calls.filter(call=>call.kind==='deploy');assert.equal(deployments.length,1);assert.deepEqual(deployments[0].args?.slice(-2),['--project','project_fixture']);
  assert.equal(f.calls.some(call=>call.path!==undefined&&call.path!=='/v2/teams?limit=100'),false);assert.equal(f.fetches.length,0);
  assert.deepEqual(await f.jobNow(),expectedJob(f.job,state.siteUpdate));assert.equal(f.setup().relayUrl,oldUrl);assert.deepEqual([...f.values],f.originalSecrets);
 }finally{await f.close();}
});

test('팀 권한·CLI 실패·복수 또는 미확정 URL·프로젝트 바인딩 불일치는 이전 주소와 비밀을 유지한다',async()=>{
 const cases=[{noTeam:true},{code:1},{stdout:''},{stdout:newUrl+'\nhttps://second-fixture.vercel.app\n'},{stdout:'https://untrusted.invalid'},{stdout:'https://user:password@updated-fixture.vercel.app'},{deploymentProject:'other_project'},{deploymentUrl:'other-fixture.vercel.app'}];
 for(const options of cases){const f=await fixture(options);try{
  const state=await f.provisioner.updateExistingSite();assert.equal(state.state,'error');assert.equal(state.url,oldUrl);assert.equal(state.verified,false);assert.match(state.message,/이전 주소/);assert.match(state.message,/자동 재배포하지/);
  assert.equal(f.fetches.length,0,'프로젝트 바인딩 전에 암호를 전송하지 않음');assert.equal(f.calls.filter(c=>c.kind==='deploy').length,options.noTeam?0:1);
  assert.deepEqual(await f.jobNow(),expectedJob(f.job,state.siteUpdate));assert.equal(f.setup().relayUrl,oldUrl);assert.deepEqual([...f.values],f.originalSecrets);assert.equal(JSON.stringify(state).includes('SYNTHETIC_PRIVATE'),false);
 }finally{await f.close();}}
});

test('새 사이트 검증 실패와 주소 저장 실패도 이전 URL을 보존하고 배포를 다시 하지 않는다',async()=>{
 for(const options of [{unhealthy:true},{settingsFailure:true}]){const f=await fixture(options);try{
  const state=await f.provisioner.updateExistingSite();assert.equal(state.state,'error');assert.equal(state.url,oldUrl);assert.deepEqual(await f.jobNow(),expectedJob(f.job,state.siteUpdate));assert.equal(f.setup().relayUrl,oldUrl);assert.deepEqual([...f.values],f.originalSecrets);
  assert.equal(f.calls.filter(c=>c.kind==='deploy').length,1);assert.equal(f.fetches.length,2);assert.equal((await readdir(f.relayDir)).some(name=>name.startsWith('site-update-')),false);
 }finally{await f.close();}}
});

test('취소 중 늦은 배포 응답은 버리고 중복 작업·재시도·비밀 전송 없이 이전 URL을 유지한다',async()=>{
 const wait=gate(),f=await fixture({deployGate:wait.promise});let pending:Promise<any>|undefined;
 try{
  pending=f.provisioner.updateExistingSite();for(let i=0;i<100&&!f.calls.some(c=>c.kind==='deploy');i++)await new Promise(done=>setTimeout(done,5));
  assert.equal(f.calls.filter(c=>c.kind==='deploy').length,1);await assert.rejects(f.provisioner.updateExistingSite(),/진행/);
  const cancelled=f.provisioner.cancel();wait.release();const state=await pending;await cancelled;
  assert.equal(state.state,'error');assert.equal(state.url,oldUrl);assert.match(state.message,/취소/);assert.equal(f.fetches.length,0);assert.deepEqual(await f.jobNow(),expectedJob(f.job,state.siteUpdate));assert.equal(f.setup().relayUrl,oldUrl);assert.deepEqual([...f.values],f.originalSecrets);
  assert.equal(state.siteUpdate?.state,'unknown');assert.equal(state.siteUpdate?.deployAttempted,true);
  assert.equal(state.siteUpdate?.knownCode,'VERCEL_CANCELLED');assert.equal(state.canResume,false);
 }finally{wait.release();await pending;await f.close();}
});

test('주소 저장 중 취소는 이전 설정을 복원하고 복원 미확인은 성공이나 안전 취소로 숨기지 않는다',async()=>{
 const f=await fixture({cancelOnSave:true});try{const state=await f.provisioner.updateExistingSite();assert.equal(state.state,'error');assert.match(state.message,/취소/);assert.equal(state.url,oldUrl);assert.deepEqual(await f.jobNow(),expectedJob(f.job,state.siteUpdate));assert.equal(f.setup().relayUrl,oldUrl);}finally{await f.close();}
 const uncertain=await fixture({restoreFailure:true});try{const state=await uncertain.provisioner.updateExistingSite();assert.equal(state.state,'error');assert.match(state.message,/복원을 확인하지 못/);assert.equal(state.verified,false);assert.equal(uncertain.calls.filter(c=>c.kind==='deploy').length,1);}finally{await uncertain.close();}
});

test('완료된 구 사이트의 업데이트 실패는 구성재개·승인과 분리되고 재시작·연결 검증도 결과를 바꾸지 않는다',async()=>{
 const f=await fixture({code:17});try{
  const state=await f.provisioner.updateExistingSite(),beforeCalls=f.calls.length;
  assert.equal(state.canResume,false);assert.equal(state.approvalUrl,undefined);
  assert.deepEqual({...state.siteUpdate,durationMs:0},{state:'unknown',stage:'deploying',deployAttempted:true,
   targetVersion:'0.1.0-preview.14',knownCode:'VERCEL_EXIT_NONZERO',exitCode:17,durationMs:0});
  assert.equal(state.siteUpdate?.version,undefined);
  const restarted=f.restart(),restored=await restarted.status();
  assert.deepEqual(restored.siteUpdate,state.siteUpdate);assert.equal(f.calls.length,beforeCalls);
  f.config.fetcher=(async(url:any)=>{
   assert.equal(new URL(String(url)).origin,oldUrl);
   return String(url).endsWith('/api/auth/login')?new Response('{"ok":true}',{headers:{'content-type':'application/json','set-cookie':'__Host-gukgam_auth=SYNTHETIC; Secure'}})
    :new Response('{"ok":true,"cloud":true,"read_mode":"source","agent":{"online":false}}',{headers:{'content-type':'application/json'}});
  }) as typeof fetch;
  const verified=await restarted.verify();assert.equal(verified.verified,true);assert.equal(verified.state,'ready');
  assert.deepEqual(verified.siteUpdate,state.siteUpdate);assert.equal(verified.canResume,false);
  assert.deepEqual([...f.values],f.originalSecrets);assert.equal(f.calls.filter(call=>call.kind==='deploy').length,1);
 }finally{await f.close();}
});

test('배포 전 공식 API 실패는 HTTP 수치만 남기고 배포 미시도·초기 구성과 구분한다',async()=>{
 const f=await fixture({apiStatus:403});try{
  const state=await f.provisioner.updateExistingSite();
  assert.equal(state.siteUpdate?.state,'failed');assert.equal(state.siteUpdate?.stage,'preparing');
  assert.equal(state.siteUpdate?.deployAttempted,false);assert.equal(state.siteUpdate?.knownCode,'API_HTTP_ERROR');
  assert.equal(state.siteUpdate?.httpStatus,403);assert.equal(state.canResume,false);
  assert.equal(f.calls.filter(call=>call.kind==='deploy').length,0);
  assert.equal(JSON.stringify({state,events:f.events}).includes('SYNTHETIC_PRIVATE'),false);
  assert.deepEqual(await f.jobNow(),expectedJob(f.job,state.siteUpdate));
 }finally{await f.close();}
});

test('배포 호출 전 취소만 확정 취소로 남기고 재시작에 로컬 결과만 복원한다',async()=>{
 const wait=gate(),f=await fixture({teamGate:wait.promise});let work:Promise<any>|undefined;
 try{
  work=f.provisioner.updateExistingSite();for(let index=0;index<100&&!f.calls.length;index++)await new Promise(done=>setTimeout(done,2));
  assert.equal(f.calls.length,1);const cancel=f.provisioner.cancel();wait.release();const state=await work;await cancel;
  assert.equal(state.siteUpdate?.state,'cancelled');assert.equal(state.siteUpdate?.deployAttempted,false);
  assert.equal(f.calls.filter(call=>call.kind==='deploy').length,0);
  assert.deepEqual((await f.restart().status()).siteUpdate,state.siteUpdate);
 }finally{wait.release();await work;await f.close();}
});

test('과거 기록의 누락·손상된 선택 업데이트 journal은 조회나 버전 추정 없이 미확인으로 둔다',async()=>{
 const invalid=[undefined,{state:'succeeded',stage:'completed',deployAttempted:true,version:'PRIVATE_VERSION_URL'},
  {state:'succeeded',stage:'completed',deployAttempted:true,targetVersion:'0.1.0-preview.14',version:'0.1.0-preview.13'},
  {state:'unknown',stage:'deploying',deployAttempted:true,knownCode:'PRIVATE_ERROR'},
  {state:'unknown',stage:'deploying',deployAttempted:true,httpStatus:Infinity},
  {state:'cancelled',stage:'deploying',deployAttempted:true},'PRIVATE_JOURNAL'];
 for(const siteUpdate of invalid){const f=await fixture();try{
  const source=JSON.stringify({...f.job,...(siteUpdate===undefined?{}:{siteUpdate})});await writeFile(join(f.relayDir,'job.json'),source);
  const current=await f.provisioner.status();assert.deepEqual(current.siteUpdate,{state:'unverified',stage:'unverified'});
  assert.equal(current.canResume,false);assert.equal(f.calls.length,0);assert.equal(f.events.length,0);
  assert.equal(await readFile(join(f.relayDir,'job.json'),'utf8'),source);
 }finally{await f.close();}}
});

test('직전 버전의 진행 중 journal은 중단 미확인으로만 복원하고 자동 배포하지 않는다',async()=>{
 const f=await fixture();try{
  const siteUpdate={state:'updating',stage:'binding',deployAttempted:true,targetVersion:'0.1.0-preview.14',durationMs:7};
  const source=JSON.stringify({...f.job,siteUpdate});await writeFile(join(f.relayDir,'job.json'),source);
  const current=await f.provisioner.status();assert.deepEqual(current.siteUpdate,{...siteUpdate,state:'unknown',knownCode:'INTERRUPTED'});
  assert.equal(current.canResume,false);assert.equal(f.calls.length,0);assert.equal(f.events.length,0);
  assert.equal(await readFile(join(f.relayDir,'job.json'),'utf8'),source);
 }finally{await f.close();}
});

test('배포 전 중단은 실패로만 복원하고 배포시도가 불명확한 중단에는 값을 추정하지 않는다',async()=>{
 for(const [journal,expected] of [
  [{state:'updating',stage:'preparing',deployAttempted:false,targetVersion:'0.1.0-preview.14'},
   {state:'failed',stage:'preparing',deployAttempted:false,targetVersion:'0.1.0-preview.14',knownCode:'INTERRUPTED'}],
  [{state:'updating',stage:'PRIVATE_STAGE',targetVersion:'PRIVATE_VERSION'},
   {state:'unknown',stage:'unverified',knownCode:'INTERRUPTED'}],
 ] as const){const f=await fixture();try{
  const source=JSON.stringify({...f.job,siteUpdate:journal});await writeFile(join(f.relayDir,'job.json'),source);
  const current=await f.provisioner.status();assert.deepEqual(current.siteUpdate,expected);assert.equal(current.canResume,false);
  assert.equal(f.calls.length,0);assert.equal(await readFile(join(f.relayDir,'job.json'),'utf8'),source);
 }finally{await f.close();}}
});

test('안전 진단 callback 실패는 업데이트나 주소 저장을 실패로 바꾸지 않는다',async()=>{
 const f=await fixture();try{
  f.config.onSiteUpdate=()=>{throw Error('SYNTHETIC_PRIVATE_DIAGNOSTIC_FAILURE');};
  const current=await f.provisioner.updateExistingSite();assert.equal(current.siteUpdate?.state,'succeeded');
  assert.equal(f.setup().relayUrl,newUrl);assert.equal(f.calls.filter(call=>call.kind==='deploy').length,1);
  assert.equal(JSON.stringify(current).includes('SYNTHETIC_PRIVATE'),false);
 }finally{await f.close();}
});

test('배포 전·후 journal 쓰기 실패는 원인 본문 없이 이전 주소를 보존하고 재배포하지 않는다',async()=>{
 for(const stage of ['preparing','binding']){const f=await fixture();try{
  const owner=f.provisioner as any,original=owner.persist.bind(owner);let failed=false;
  owner.persist=async(job:any)=>{if(!failed&&job?.siteUpdate?.state==='updating'&&job.siteUpdate.stage===stage){failed=true;throw Error('SYNTHETIC_PRIVATE_DISK_FAILURE');}return original(job);};
  const current=await f.provisioner.updateExistingSite();assert.equal(current.siteUpdate?.knownCode,'JOURNAL_FAILED');
  assert.equal(current.siteUpdate?.state,stage==='preparing'?'failed':'unknown');assert.equal(current.siteUpdate?.deployAttempted,stage!=='preparing');
  assert.equal(f.setup().relayUrl,oldUrl);assert.equal(f.calls.filter(call=>call.kind==='deploy').length,stage==='preparing'?0:1);
  assert.equal(current.siteUpdate?.version,undefined);assert.deepEqual(await f.jobNow(),expectedJob(f.job,current.siteUpdate));
  assert.equal(JSON.stringify({current,events:f.events}).includes('SYNTHETIC_PRIVATE'),false);
 }finally{await f.close();}}
});

test('최종 완료 marker 저장 실패도 성공 버전을 만들지 않고 주소를 복원한다',async()=>{
 const f=await fixture();try{
  const owner=f.provisioner as any,original=owner.persist.bind(owner);
  owner.persist=async(job:any)=>{if(job?.siteUpdate?.state==='succeeded')throw Error('SYNTHETIC_PRIVATE_DISK_FAILURE');return original(job);};
  const current=await f.provisioner.updateExistingSite();assert.equal(current.siteUpdate?.state,'unknown');
  assert.equal(current.siteUpdate?.stage,'saving');assert.equal(current.siteUpdate?.version,undefined);
  assert.equal(f.events.some(value=>value.state==='succeeded'),false);assert.equal(f.setup().relayUrl,oldUrl);
  assert.deepEqual(await f.jobNow(),expectedJob(f.job,current.siteUpdate));assert.equal(f.calls.filter(call=>call.kind==='deploy').length,1);
 }finally{await f.close();}
});
