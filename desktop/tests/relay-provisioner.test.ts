import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,readdir,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {RelayProvisioner,databaseFromEnvironment,officialApproval,type Cli} from '../src/relay/provisioner';
async function fixture() {
 const dir=await mkdtemp(join(tmpdir(),'gukgam-relay-test-')),template=join(dir,'template');await mkdir(template);await writeFile(join(template,'index.py'),'# 동봉 소스');await writeFile(join(template,'.env'),'배포 제외');
 const values=new Map<string,unknown>(),calls:{args:string[];input?:any}[]=[],fetches:string[]=[];
 let approved=false,created=false,online=false,initialized=0,unknown=false,jobId='';let setup:any={organization:'시험기관',hasRelayToken:false};
 const vault={get:async<T>(k:string)=>values.get(k) as T|undefined,set:async(k:string,v:unknown)=>{values.set(k,v);},delete:async(k:string)=>{values.delete(k);}};
 const settings={read:async()=>setup,save:async(v:any)=>{setup={...v,hasRelayToken:Boolean(v.relayToken)};return setup;}};
 const cli:Cli={requestApi:async(path,o)=>{const result=await cli.run(['api',path,...(o.teamId?['--scope',o.teamId]:[])],{cwd:o.cwd,input:o.body===undefined?undefined:JSON.stringify(o.body),signal:o.signal});return {status:200,body:JSON.parse(result.stdout)};},run:async(args,options)=>{
  calls.push({args,input:options.input?JSON.parse(options.input):undefined});const path=args[1];let data:any;
  if(args[0]==='login')return {code:0,stdout:'로그인 완료',stderr:''};
  if(path==='/v2/teams?limit=100')data={teams:[{id:'team_test',slug:'test-workspace',name:'시험기관'}]};
  else if(path==='/v2/integrations/integration/neon')data={id:'integration_test',products:[{id:'product_test',slug:'actual-neon-product',name:'Neon',metadataSchema:{properties:{region:{type:'string',enum:['region-a','region-b'],title:'DB 위치'}},required:['region']}}]};
  else if(path?.includes('/plans?'))data={plans:[{id:'plan_test',name:'시험 요금제',type:'subscription'}]};
  else if(path==='/v11/projects'){created=true;jobId=JSON.parse(options.input!).environmentVariables[0].value;data={id:'project_test'};}
  else if(path?.startsWith('/v2/integrations/configurations?'))data=approved?[{id:'install_test',ownerId:'team_test',installationType:'marketplace'}]:[];
  else if(path?.includes('/auto-provision/')){if(unknown)throw Error('private-provider-message');data={kind:'provisioned',resource:{id:'store_test'}};}
  else if(path?.includes('/connections')||path?.includes('/env?upsert=true'))data={ok:true};
  else if(path?.endsWith('/env'))data={envs:[{id:'env_database',key:'DATABASE_URL_UNPOOLED',target:['production'],type:'encrypted'},{id:'env_marker',key:'GUKGAM_DESKTOP_SETUP_ID',target:['production'],type:'plain'}]};
  else if(path?.endsWith('/env/env_database'))data={id:'env_database',key:'DATABASE_URL_UNPOOLED',target:['production'],type:'encrypted',decrypted:true,value:'postgresql://test:placeholder@db.invalid/newdb?sslmode=require'};
  else if(path?.endsWith('/env/env_marker'))data={id:'env_marker',key:'GUKGAM_DESKTOP_SETUP_ID',target:['production'],type:'plain',decrypted:true,value:jobId};
  else if(path?.startsWith('/v9/projects/'))data={id:'project_test'};
  else if(args[0]==='integration'&&args[1]==='list')data={resources:[{id:'store_test',name:JSON.parse(await readFile(join(dir,'relay','job.json'),'utf8')).resourceName}]};
  else if(args[0]==='deploy')return {code:0,stdout:'https://gukgam-test-workspace.vercel.app\n',stderr:''};
  else throw Error('예상하지 않은 CLI 요청');return {code:0,stdout:JSON.stringify(data),stderr:''};
 }};
 const options={userData:dir,templateDir:template,cli,settings,vault,initializeDatabase:async(dsn:string)=>{initialized++;assert.equal(dsn,'postgresql://test:placeholder@db.invalid/newdb?sslmode=require');},openExternal:async(url:string)=>{assert.equal(new URL(url).hostname,'vercel.com');},expectedConnection:async()=> 'a'.repeat(32),fetcher:(async(url:any,init:RequestInit={})=>{
  fetches.push(String(url));assert.equal(init.redirect,'error');const headers={'content-type':'application/json'};
  if(String(url).endsWith('/api/auth/login'))return new Response('{"ok":true}',{headers:{...headers,'set-cookie':'__Host-gukgam_auth=placeholder; Secure; HttpOnly'}});
  if(String(url).endsWith('/api/health'))return new Response(JSON.stringify({ok:true,cloud:true,read_mode:'source',agent:{online,read_mode:'source',connection_id:'a'.repeat(32)}}),{headers});
  if(String(url).endsWith('/api/sessions'))return new Response('{"items":[]}',{headers});throw Error('허용되지 않은 요청');
 }) as typeof fetch};
 return {dir,options,provisioner:new RelayProvisioner(options),input:{teamId:'team_test',projectName:'gukgam-test-workspace',productId:'product_test',planId:'plan_test',metadata:{region:'region-a'},operatorPasscode:'operator-placeholder-long',viewerPasscode:'viewer-placeholder',consent:true},calls,fetches,values,setApproved:()=>{approved=true;},setOnline:()=>{online=true;},setUnknown:()=>{unknown=true;},initialized:()=>initialized,created:()=>created,setup:()=>setup,close:()=>rm(dir,{recursive:true,force:true})};
}
test('로그인·공식 약관 대기·재개·배포·현재 PC 조회를 안전한 계약으로 연결한다',async()=>{
 const f=await fixture();try{
  assert.equal((await f.provisioner.connect()).state,'connected');
  const catalog=await f.provisioner.catalog('team_test');assert.deepEqual(catalog.products[0].fields[0].options,['region-a','region-b']);
  const approval=await f.provisioner.provision(f.input);assert.equal(approval.state,'approval');assert.ok(approval.approvalUrl?.includes('/integrations/accept-terms/neon'));assert.equal(f.initialized(),0);
  await f.provisioner.openApproval();f.setApproved();const ready=await f.provisioner.resume();assert.equal(ready.state,'ready');assert.equal(ready.verified,true);assert.equal(ready.sourceOnline,false);assert.equal(f.initialized(),1);
  assert.equal(f.calls.filter(c=>c.args[1]==='/v11/projects').length,1);assert.ok(f.setup().relayToken);
  assert.deepEqual(f.calls.find(c=>c.args[1]?.includes('/connections'))!.input,{envVarEnvironments:['production'],projectId:'project_test',type:'integration'});
  assert.equal(f.calls.some(c=>c.args.includes('add')||c.args.includes('npx')||c.args[0]==='env'),false);
  assert.equal((await readdir(join(f.dir,'relay','site'))).includes('.env'),false);
  const saved=await readFile(join(f.dir,'relay','job.json'),'utf8');assert.equal(saved.includes(f.input.operatorPasscode),false);assert.equal(saved.includes(f.setup().relayToken),false);
  f.setOnline();assert.equal((await f.provisioner.verify()).sourceOnline,true);assert.ok(f.fetches.some(x=>x.endsWith('/api/sessions')));
  assert.equal((await new RelayProvisioner(f.options).status()).verified,false);
 }finally{await f.close();}
});
test('동의·암호·카탈로그 검증 실패 때 사이트와 DB를 생성하지 않는다',async()=>{
 for(const changed of [{consent:false},{operatorPasscode:'short'},{viewerPasscode:'operator-placeholder-long'},{metadata:{region:'invalid'}},{planId:'unknown'}]){const f=await fixture();try{assert.equal((await f.provisioner.provision({...f.input,...changed})).state,'error');assert.equal(f.created(),false);assert.equal(f.calls.some(c=>c.args[1]?.includes('/auto-provision/')),false);}finally{await f.close();}}
});
test('결과가 불명확한 DB는 재개 때 기존 자원만 조회하고 중복 생성하지 않는다',async()=>{
 const f=await fixture();try{f.setApproved();f.setUnknown();assert.equal((await f.provisioner.provision(f.input)).state,'error');assert.equal((await f.provisioner.resume()).state,'ready');assert.equal(f.calls.filter(c=>c.args[1]?.includes('/auto-provision/')).length,1);}finally{await f.close();}
});
test('손상된 준비 기록은 반복 조회에도 덮어쓰거나 신규 생성하지 않는다',async()=>{
 const f=await fixture();try{await mkdir(join(f.dir,'relay'));await writeFile(join(f.dir,'relay','job.json'),'{}');await assert.rejects(f.provisioner.status(),/중복/);await assert.rejects(f.provisioner.status(),/중복/);assert.equal(f.calls.length,0);}finally{await f.close();}
});
test('연결 값과 공식 승인 URI를 검증하고 외부 주소·중복 환경값을 거부한다',()=>{
 assert.equal(databaseFromEnvironment('DATABASE_URL="postgresql://a:placeholder@host.invalid/db"'),'postgresql://a:placeholder@host.invalid/db');
 assert.throws(()=>databaseFromEnvironment('DATABASE_URL="https://example.invalid"'),/연결/);
 assert.throws(()=>databaseFromEnvironment('DATABASE_URL="postgresql://a:p@host/db"\nDATABASE_URL="postgresql://a:p@host/db"'),/중복/);
 assert.throws(()=>officialApproval('https://vercel.com.attacker.invalid/path'),/공식/);
});
test('목록의 값은 무시하고 검증한 Production DB 항목만 단일 조회한다',async()=>{
 const f=await fixture();try{
  f.setApproved();const original=f.options.cli.requestApi,reads:string[]=[];
  f.options.cli.requestApi=async(path,o)=>{
   reads.push(path);
   if(path==='/v10/projects/project_test/env')return {status:200,body:{envs:[
    {id:'env_database',key:'DATABASE_URL_UNPOOLED',target:['production'],type:'encrypted',value:'MOCK_LIST_VALUE_MUST_NOT_BE_USED'},
    {id:'env_preview',key:'DATABASE_URL',target:['preview'],type:'encrypted'},
    {id:'env_other',key:'OTHER_PRIVATE_VALUE',target:['production'],type:'encrypted'},
   ]}};
   return original(path,o);
  };
  assert.equal((await f.provisioner.provision(f.input)).state,'ready');
  assert.deepEqual(reads.filter(path=>path.startsWith('/v1/projects/')),['/v1/projects/project_test/env/env_database']);
  assert.equal(reads.some(path=>path.includes('decrypt=')),false);assert.equal(f.initialized(),1);
  assert.equal(JSON.stringify(await f.provisioner.status()).includes('postgresql:'),false);
  assert.equal((await readFile(join(f.dir,'relay','job.json'),'utf8')).includes('postgresql:'),false);
 }finally{await f.close();}
});
test('개별 응답의 선택 메타데이터가 없거나 null이어도 검증한 조회 범위는 유지한다',async()=>{
 for(const optional of [{},{id:null,target:null,gitBranch:null}]){const f=await fixture();try{
  f.setApproved();const original=f.options.cli.requestApi;
  f.options.cli.requestApi=async(path,o)=>path==='/v1/projects/project_test/env/env_database'?{status:200,body:{key:'DATABASE_URL_UNPOOLED',type:'encrypted',decrypted:true,value:'postgresql://test:placeholder@db.invalid/newdb?sslmode=require',...optional}}:original(path,o);
  assert.equal((await f.provisioner.provision(f.input)).state,'ready');assert.equal(f.initialized(),1);
  assert.equal(f.calls.some(call=>call.args[1]?.includes('decrypt=')),false);
 }finally{await f.close();}}
});
test('DB 설정의 누락·범위·중복·식별자·보호 상태와 값을 안전하게 거부한다',async()=>{
 const base={id:'env_database',key:'DATABASE_URL_UNPOOLED',target:['production'],type:'encrypted'},detail={...base,decrypted:true,value:'postgresql://test:placeholder@db.invalid/newdb?sslmode=require'};
 const cases:{name:string;rows?:any[];detail?:any;status?:number;expect:string;reads?:number}[]=[
  {name:'필요한 키 없음',rows:[{...base,key:'OTHER_KEY'}],expect:'Production 설정 항목이 아직 없습니다',reads:0},
  {name:'preview 전용',rows:[{...base,target:['preview']}],expect:'Production 설정 항목이 아직 없습니다',reads:0},
  {name:'분기 지정 혼합',rows:[{...base,gitBranch:'branch'}],expect:'Production 설정 항목이 아직 없습니다',reads:0},
  {name:'중복 Production',rows:[base,{...base,id:'env_duplicate'}],expect:'중복',reads:0},
  {name:'잘못된 식별자',rows:[{...base,id:'../env_private'}],expect:'식별자',reads:0},
  {name:'민감값',rows:[{...base,type:'sensitive'}],expect:'우회 조회하지',reads:0},
  {name:'새 Secret 보호 방식',rows:[{...base,visibility:'secret'}],expect:'우회 조회하지',reads:0},
  {name:'개별 키 불일치',detail:{...detail,key:'OTHER_PRIVATE_KEY'},expect:'일치하지'},
  {name:'개별 식별자 불일치',detail:{...detail,id:'env_other'},expect:'일치하지'},
  {name:'개별 환경 불일치',detail:{...detail,target:['preview']},expect:'일치하지'},
  {name:'개별 분기 불일치',detail:{...detail,gitBranch:'preview-branch'},expect:'일치하지'},
  {name:'개별 식별자 잘못된 타입',detail:{...detail,id:123},expect:'일치하지'},
  {name:'개별 환경 잘못된 타입',detail:{...detail,target:123},expect:'일치하지'},
  {name:'개별 보호 방식 변경',detail:{...detail,visibility:'secret'},expect:'일치하지'},
  {name:'개별 복호화 상태 누락',detail:{...detail,decrypted:undefined},expect:'일치하지'},
  {name:'복호화 미확인',detail:{...detail,decrypted:false},expect:'일치하지'},
  {name:'값 누락',detail:{...detail,value:undefined},expect:'읽을 수 있는 값이 없습니다'},
  {name:'값 빈 문자열',detail:{...detail,value:''},expect:'읽을 수 있는 값이 없습니다'},
  {name:'마스킹된 암호',detail:{...detail,value:'postgresql://test:********@db.invalid/newdb'},expect:'마스킹'},
  {name:'해석 불가 값',detail:{...detail,value:'MOCK_PRIVATE_INVALID_URL'},expect:'연결한 결과'},
  {name:'추가 인증 필요',status:403,detail:{error:{code:'challenge_required',message:'비밀 MOCK_PRIVATE_ERROR'}},expect:'추가 인증'},
 ];
 for(const scenario of cases){const f=await fixture();try{
  f.setApproved();const original=f.options.cli.requestApi;let singleReads=0;
  f.options.cli.requestApi=async(path,o)=>{
   if(path==='/v10/projects/project_test/env')return {status:200,body:{envs:scenario.rows??[base]}};
   if(path==='/v1/projects/project_test/env/env_database'){singleReads++;return {status:scenario.status??200,body:scenario.detail??detail};}
   return original(path,o);
  };
  const state=await f.provisioner.provision(f.input);assert.equal(state.state,'error',scenario.name);assert.ok(state.message.includes(scenario.expect),`${scenario.name}: ${state.message}`);
  assert.equal(state.canResume,true);assert.equal(f.initialized(),0);if(scenario.reads!==undefined)assert.equal(singleReads,scenario.reads);
  assert.equal(JSON.stringify(state).includes('MOCK_PRIVATE'),false);assert.equal(JSON.stringify(state).includes('postgresql:'),false);
  const job=JSON.parse(await readFile(join(f.dir,'relay','job.json'),'utf8'));assert.equal(job.phase,'database');assert.equal(job.projectId,'project_test');assert.equal(job.resourceId,'store_test');
 }finally{await f.close();}}
});
test('기존 DB 단계 복구는 원래 ID와 부가 설정을 보존하고 사이트·DB 생성 요청을 반복하지 않는다',async()=>{
 const f=await fixture();try{
  f.setApproved();const original=f.options.cli.requestApi;let missing=true;
  f.options.cli.requestApi=async(path,o)=>path==='/v10/projects/project_test/env'&&missing?{status:200,body:{envs:[]}}:original(path,o);
  assert.equal((await f.provisioner.provision(f.input)).state,'error');
  const file=join(f.dir,'relay','job.json'),job=JSON.parse(await readFile(file,'utf8'));job.metadata.auth=true;job.installationMetadata.realtime=true;await writeFile(file,JSON.stringify(job));
  const boundary=f.calls.length;missing=false;assert.equal((await new RelayProvisioner(f.options).resume()).state,'ready');
  const later=f.calls.slice(boundary);assert.equal(later.some(call=>call.args[1]==='/v11/projects'||call.args[1]?.includes('/auto-provision/')),false);
  const saved=JSON.parse(await readFile(file,'utf8'));assert.equal(saved.id,job.id);assert.equal(saved.projectId,job.projectId);assert.equal(saved.resourceId,job.resourceId);assert.equal(saved.resourceName,job.resourceName);assert.equal(saved.metadata.auth,true);assert.equal(saved.installationMetadata.realtime,true);
 }finally{await f.close();}
});
test('프로젝트 생성 응답 유실 후 작업 표식도 검증된 개별 항목으로 확인한다',async()=>{
 const f=await fixture();try{
  f.setApproved();const original=f.options.cli.requestApi,reads:string[]=[];let lost=true;
  f.options.cli.requestApi=async(path,o)=>{reads.push(path);const response=await original(path,o);if(path==='/v11/projects'&&lost){lost=false;throw Error('MOCK_RESPONSE_LOST');}return response;};
  assert.equal((await f.provisioner.provision(f.input)).state,'error');assert.equal((await f.provisioner.resume()).state,'ready');
  assert.ok(reads.includes('/v1/projects/project_test/env/env_marker'));assert.equal(reads.some(path=>path.includes('decrypt=')),false);assert.equal(f.calls.filter(call=>call.args[1]==='/v11/projects').length,1);
 }finally{await f.close();}
});
test('유실된 프로젝트의 작업 표식이 다르면 기존 작업을 보존하고 DB를 생성하지 않는다',async()=>{
 const f=await fixture();try{
  f.setApproved();const original=f.options.cli.requestApi;
  f.options.cli.requestApi=async(path,o)=>{
   const response=await original(path,o);
   if(path==='/v11/projects')throw Error('MOCK_RESPONSE_LOST');
   if(path==='/v1/projects/project_test/env/env_marker')return {status:200,body:{...(response.body as any),value:'another_setup_id'}};
   return response;
  };
  assert.equal((await f.provisioner.provision(f.input)).state,'error');const state=await f.provisioner.resume();assert.equal(state.state,'error');assert.match(state.message,/이 앱이 요청한 사이트인지 확인하지 못/);
  assert.equal(f.calls.filter(call=>call.args[1]==='/v11/projects').length,1);assert.equal(f.calls.some(call=>call.args[1]?.includes('/auto-provision/')),false);assert.equal(f.initialized(),0);
  assert.equal(JSON.parse(await readFile(join(f.dir,'relay','job.json'),'utf8')).phase,'creating-project');
 }finally{await f.close();}
});
test('신규 견적과 생성은 원시 요청과 기본값에 관계없이 auth·realtime을 끈다',async()=>{
 const f=await fixture();try{
  f.setApproved();const original=f.options.cli.requestApi,metadata:any[]=[];
  f.options.cli.requestApi=async(path,o)=>{
   if(path==='/v2/integrations/integration/neon')return {status:200,body:{id:'integration_test',metadataSchema:{properties:{auth:{type:'boolean',default:true}}},products:[{id:'product_test',slug:'actual-neon-product',name:'Neon',metadataSchema:{properties:{region:{type:'string',enum:['region-a']},realtime:{type:'boolean',default:true}}}}]}};
   if(path.includes('/plans?'))metadata.push(JSON.parse(new URL('https://example.invalid'+path).searchParams.get('metadata')!));
   return original(path,o);
  };
  const input={...f.input,metadata:{region:'region-a',auth:true,realtime:true}};assert.equal((await f.provisioner.provision(input)).state,'ready');
  assert.ok(metadata.length);for(const value of metadata){assert.equal(value.auth,false);assert.equal(value.realtime,false);}
  const generated=f.calls.find(call=>call.args[1]?.includes('/auto-provision/'))!.input;assert.equal(generated.metadata.realtime,false);assert.equal(generated.installationMetadata.auth,false);
 }finally{await f.close();}
});
test('요금제 비용과 제공량은 실제 응답의 문구를 풀어 표시하고 미확인 조건을 명시한다',async()=>{
 const f=await fixture();try{
  const original=f.options.cli.requestApi;f.options.cli.requestApi=async(path,o)=>path.includes('/plans?')?{status:200,body:{plans:[
   {id:'plan_test',name:'시험',type:'subscription',description:'사용량에 따라 과금',cost:'$5/month + usage',details:[{label:'저장공간',value:'1 GB'},{label:'컴퓨팅',value:'20 CU-hours'}],highlightedDetails:[{label:'저장공간',value:'1 GB'}],initialCharge:'5 USD',quote:[{line:'추가 사용',amount:'별도'}],paymentMethodRequired:true},
   {id:'plan_unknown',name:'조건 미확인',type:'subscription',paymentMethodRequired:false,details:{invalid:true}},
  ]}}:original(path,o);
  const plans=await f.provisioner.plans({teamId:'team_test',productId:'product_test',metadata:{region:'region-a'}});
  assert.match(plans[0].description,/\$5\/month \+ usage/);assert.match(plans[0].description,/컴퓨팅: 20 CU-hours/);assert.equal(plans[0].description.match(/저장공간: 1 GB/g)?.length,1);assert.match(plans[0].description,/가입 시 청구: 5 USD/);assert.match(plans[0].description,/추가 사용: 별도/);assert.match(plans[0].description,/초과 사용료/);
  assert.match(plans[1].description,/명확한 금액·주기가 없습니다/);assert.doesNotMatch(plans[1].description,/무료|\[object Object\]/);
 }finally{await f.close();}
});
test('동명이름 409는 기존 사이트를 건드리지 않고 새 이름 입력으로 복구한다',async()=>{
 const f=await fixture();try{
  const original=f.options.cli.requestApi;let conflict=true;
  f.options.cli.requestApi=async(path,o)=>path==='/v11/projects'&&conflict?{status:409,body:{error:{code:'project_already_exists'}}}:original(path,o);
  const first=await f.provisioner.provision(f.input);assert.equal(first.state,'error');assert.match(first.message,/다른 이름/);assert.equal(first.projectName,undefined);assert.equal(first.canResume,false);
  conflict=false;f.setApproved();assert.equal((await f.provisioner.provision({...f.input,projectName:'gukgam-another-workspace'})).state,'ready');
 }finally{await f.close();}
});
test('명시 422 브라우저 구성 뒤 재개는 이미 생성한 DB만 조회한다',async()=>{
 const f=await fixture();try{
  f.setApproved();const original=f.options.cli.requestApi;let approvalRequired=true;
  f.options.cli.requestApi=async(path,o)=>path.includes('/auto-provision/')&&approvalRequired?{status:422,body:{error:{kind:'requires-billing',url:'https://console.neon.tech/app/approve',integration:{id:'integration_test'},product:{id:'product_test'}}}}:original(path,o);
  const approval=await f.provisioner.provision(f.input);assert.equal(approval.state,'approval');assert.equal(new URL(approval.approvalUrl!).hostname,'console.neon.tech');assert.ok(new URL(approval.approvalUrl!).searchParams.get('defaultResourceName')?.startsWith('gukgam-test-workspace-'));assert.equal(f.initialized(),0);
  assert.equal((await f.provisioner.resume()).state,'ready');assert.equal(f.calls.filter(c=>c.args[1]==='/v11/projects').length,1);
 }finally{await f.close();}
});
test('카탈로그 조회 중 취소도 자식 작업 종료를 기다린다',async()=>{
 const f=await fixture();try{
  let started!:()=>void,finished=false;const ready=new Promise<void>(r=>{started=r;});
  f.options.cli.requestApi=async(_path,o)=>{started();return new Promise((_resolve,reject)=>{o.signal!.addEventListener('abort',()=>{setTimeout(()=>{finished=true;reject(Error('취소'));},10);});});};
  const querying=f.provisioner.catalog('team_test');const rejected=assert.rejects(querying,/취소/);await ready;await f.provisioner.cancel();assert.equal(finished,true);await rejected;
 }finally{await f.close();}
});

test('로그인 완료 대기 중 공식 device 주소를 한 번 자동 열고 수동 승인 열기도 유지한다',async()=>{
 const f=await fixture();try{
  const opened:string[]=[],url='https://vercel.com/oauth/device?user_code=MOCK-CODE';
  f.options.openExternal=async value=>{opened.push(value);};
  const original=f.options.cli.run;let finish!:(value:{code:number;stdout:string;stderr:string})=>void,started!:()=>void;
  const began=new Promise<void>(resolve=>{started=resolve;});
  f.options.cli.run=async(args,options)=>{
   if(args[0]!=='login')return original(args,options);
   const pending=new Promise<{code:number;stdout:string;stderr:string}>(resolve=>{finish=resolve;});
   options.onDeviceUrl?.('https://vercel.com.evil.invalid/oauth/device?user_code=BAD');
   options.onDeviceUrl?.(url);options.onDeviceUrl?.(url);options.onDeviceUrl?.('https://vercel.com/device?user_code=OTHER-CODE');started();return pending;
  };
  const connecting=f.provisioner.connect();await began;await Promise.resolve();
  assert.deepEqual(opened,[url]);const pending=await f.provisioner.status();assert.equal(pending.state,'login');assert.equal(pending.approvalUrl,url);
  await assert.rejects(f.provisioner.connect(),/진행 중/);assert.equal(f.calls.length,0);
  await f.provisioner.openApproval();assert.deepEqual(opened,[url,url]);
  finish({code:0,stdout:'',stderr:''});const connected=await connecting;
  assert.equal(connected.state,'connected');assert.equal(connected.approvalUrl,undefined);
 }finally{await f.close();}
});

test('자동 브라우저 열기 실패는 로그인 URI와 수동 버튼을 보존하고 오류 원문을 숨긴다',async()=>{
 const f=await fixture();try{
  let attempts=0;f.options.openExternal=async()=>{if(++attempts===1)throw Error('MOCK_PRIVATE_BROWSER_ERROR');};
  const original=f.options.cli.run;let finish!:(value:{code:number;stdout:string;stderr:string})=>void,started!:()=>void;
  const began=new Promise<void>(resolve=>{started=resolve;});
  f.options.cli.run=async(args,options)=>{
   if(args[0]!=='login')return original(args,options);
   const pending=new Promise<{code:number;stdout:string;stderr:string}>(resolve=>{finish=resolve;});
   options.onDeviceUrl?.('https://vercel.com/oauth/device?user_code=MOCK-CODE');started();return pending;
  };
  const connecting=f.provisioner.connect();await began;await new Promise(resolve=>setTimeout(resolve,0));
  const status=await f.provisioner.status();assert.equal(status.state,'login');assert.ok(status.approvalUrl);assert.match(status.message,/자동으로 열지 못/);assert.equal(status.message.includes('MOCK_PRIVATE'),false);
  await f.provisioner.openApproval();assert.equal(attempts,2);
  finish({code:0,stdout:'',stderr:''});assert.equal((await connecting).state,'connected');
 }finally{await f.close();}
});

test('로그인 취소는 CLI 종료와 connect 완료를 기다리고 늦은 URL·브라우저 실패가 재시도를 바꾸지 않는다',async()=>{
 const f=await fixture();try{
  let failBrowser!:(error:Error)=>void,oldUrl:((url:string)=>void)|undefined,started!:()=>void,finished=false,opens=0;
  f.options.openExternal=()=>{opens++;return new Promise<void>((_resolve,reject)=>{failBrowser=reject;});};
  const original=f.options.cli.run,began=new Promise<void>(resolve=>{started=resolve;});
  f.options.cli.run=async(args,options)=>{
   if(args[0]!=='login')return original(args,options);
   oldUrl=options.onDeviceUrl;
   return new Promise((_resolve,reject)=>{
    options.signal!.addEventListener('abort',()=>{oldUrl?.('https://vercel.com/device?user_code=LATE-CODE');setTimeout(()=>{finished=true;reject(Error('mock_cancel'));},5);},{once:true});
    oldUrl?.('https://vercel.com/device?user_code=MOCK-CODE');started();
   });
  };
  const connecting=f.provisioner.connect();await began;await Promise.resolve();
  const cancelling=f.provisioner.cancel();
  await assert.rejects(f.provisioner.openApproval(),/취소 중/);assert.equal(opens,1);assert.equal(finished,false);
  const cancelled=await cancelling;const settled=await connecting;
  assert.equal(finished,true);assert.equal(settled.state,'error');assert.equal(cancelled.approvalUrl,undefined);assert.match(cancelled.message,/로그인을 취소/);assert.equal(opens,1);
  f.options.cli.run=original;assert.equal((await f.provisioner.connect()).state,'connected');
  oldUrl?.('https://vercel.com/device?user_code=LATE-CODE');failBrowser(Error('MOCK_LATE_BROWSER_FAILURE'));await new Promise(resolve=>setTimeout(resolve,0));
  const retried=await f.provisioner.status();assert.equal(retried.state,'connected');assert.equal(retried.approvalUrl,undefined);assert.equal(opens,1);
 }finally{await f.close();}
});

test('로그인 취소 때 자식 종료 미확인은 재로그인 가능 안내로 가리지 않는다',async()=>{
 const f=await fixture();try{
  let started!:()=>void;const began=new Promise<void>(resolve=>{started=resolve;});
  f.options.cli.run=async(_args,options)=>new Promise((_resolve,reject)=>{
   options.signal!.addEventListener('abort',()=>reject(Object.assign(Error('MOCK_PRIVATE_STOP_ERROR'),{code:'VERCEL_STOP_UNCONFIRMED'})),{once:true});started();
  });
  const connecting=f.provisioner.connect();await began;const cancelled=await f.provisioner.cancel();await connecting;
  assert.equal(cancelled.state,'error');assert.match(cancelled.message,/종료를 확인하지 못/);assert.match(cancelled.message,/앱을 닫은 뒤/);assert.equal(cancelled.message.includes('다시 로그인할 수'),false);assert.equal(cancelled.message.includes('MOCK_PRIVATE'),false);assert.equal(cancelled.approvalUrl,undefined);
 }finally{await f.close();}
});
