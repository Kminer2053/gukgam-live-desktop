import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {spawn} from 'node:child_process';
import {mkdir,mkdtemp,readFile,readdir,rm,symlink,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {VercelCliRunner,VERCEL_AUTH_VAULT_KEY,validateVercelDeviceUrl,vercelChildEnvironment,type VercelAuthVault} from '../src/relay/vercel-cli';

class Vault implements VercelAuthVault {
  values=new Map<string,unknown>();
  async get<T>(key:string){return this.values.get(key) as T|undefined;}
  async set(key:string,value:unknown){this.values.set(key,value);}
  async delete(key:string){this.values.delete(key);}
}
class Child extends EventEmitter {
  stdin=new PassThrough();stdout=new PassThrough();stderr=new PassThrough();
  kills:string[]=[];
  kill(signal='SIGTERM'){this.kills.push(signal);queueMicrotask(()=>this.emit('close',null));return true;}
}
type Invocation={executable:string;args:readonly string[];options:Record<string,unknown>;child:Child;dir:string};
async function harness(action:(call:Invocation)=>Promise<void>|void,settings:{timeoutMs?:number;maxOutputBytes?:number;vault?:Vault;fetch?:typeof fetch;apiTimeoutMs?:number;maxApiBytes?:number}={}) {
  const root=await mkdtemp(join(tmpdir(),'gukgam-cli-test-'));
  const calls:Invocation[]=[],vault=settings.vault??new Vault();
  const fake=((executable:string,args:readonly string[],options:Record<string,unknown>)=>{
    const child=new Child(),dir=args[args.indexOf('--global-config')+1];
    const call={executable,args,options,child,dir};calls.push(call);
    queueMicrotask(()=>{Promise.resolve(action(call)).catch(error=>child.emit('error',error));});
    return child;
  }) as unknown as typeof spawn;
  const runner=new VercelCliRunner({cliPath:join(root,'bundled','vercel','dist','vc.js'),tempRoot:join(root,'private'),vault,spawn:fake,
    env:{PATH:'/mock/path',HOME:root,VERCEL_TOKEN:'PARENT_VERCEL',GH_TOKEN:'PARENT_GH',GITHUB_TOKEN:'PARENT_GITHUB',GUKGAM_RELAY_TOKEN:'PARENT_RELAY',PYTHONPATH:'PARENT_PYTHON',OPENAI_API_KEY:'PARENT_OPENAI',NODE_OPTIONS:'PARENT_OPTIONS'},
    timeoutMs:settings.timeoutMs??1000,killWaitMs:20,maxOutputBytes:settings.maxOutputBytes,fetch:settings.fetch,apiTimeoutMs:settings.apiTimeoutMs,maxApiBytes:settings.maxApiBytes});
  return {root,runner,calls,vault,cleanup:()=>rm(root,{recursive:true,force:true})};
}

test('동봉 CLI만 shell 없이 실행하고 전용 config와 자식 환경을 고정한다',async()=>{
  const h=await harness(async c=>{
    const env=c.options.env as NodeJS.ProcessEnv;
    assert.equal(c.executable,process.execPath);assert.equal(c.options.shell,false);assert.equal(c.options.windowsHide,true);
    assert.equal(env.ELECTRON_RUN_AS_NODE,'1');assert.equal(env.CI,'1');assert.equal(env.VERCEL_TOKEN_STORAGE,'file');
    for(const key of ['VERCEL_TOKEN','GH_TOKEN','GITHUB_TOKEN','GUKGAM_RELAY_TOKEN','PYTHONPATH','OPENAI_API_KEY','NODE_OPTIONS'])assert.equal(env[key],undefined);
    assert.deepEqual(JSON.parse(await readFile(join(c.dir,'config.json'),'utf8')),{credStorage:'file',autoUpdate:false});
    c.child.stdout.end('{"teams":[]}\n');c.child.emit('close',0);
  });
  try {const r=await h.runner.run(['api','/v2/teams','--raw'],{cwd:h.root});assert.deepEqual(JSON.parse(r.stdout),{teams:[]});assert.equal(r.code,0);assert.deepEqual(await readdir(join(h.root,'private')),[]);}
  finally {await h.cleanup();}
});

test('인증 JSON 전체를 복원하고 회전한 인증·확장필드를 vault에 저장한 뒤 평문을 지운다',async()=>{
  const vault=new Vault(),old={token:'OLD_TOKEN',refreshToken:'OLD_REFRESH',expiresAt:42,extension:{account:'mock'}};
  vault.values.set(VERCEL_AUTH_VAULT_KEY,{version:1,authJson:JSON.stringify(old)});
  const next={...old,token:'NEW_TOKEN',refreshToken:'NEW_REFRESH',additional:'preserved'};
  const h=await harness(async c=>{
    assert.deepEqual(JSON.parse(await readFile(join(c.dir,'auth.json'),'utf8')),old);
    await writeFile(join(c.dir,'auth.json'),JSON.stringify(next));
    c.child.stderr.end('NEW_TOKEN OLD_REFRESH\n');c.child.emit('close',0);
  },{vault});
  try {const r=await h.runner.run(['api','/v2/teams','--raw'],{cwd:h.root});const stored=await vault.get<{version:1;authJson:string}>(VERCEL_AUTH_VAULT_KEY);assert.deepEqual(JSON.parse(stored!.authJson),next);assert.equal(r.stderr.includes('NEW_TOKEN'),false);assert.equal(r.stderr.includes('OLD_REFRESH'),false);assert.deepEqual(await readdir(join(h.root,'private')),[]);}
  finally {await h.cleanup();}
});

test('분할된 한국어·공식 device URI를 읽고 외부 주소와 유사 도메인은 무시한다',async()=>{
  const urls:string[]=[];
  const h=await harness(async c=>{
    const b=Buffer.from('로그인 안내\nVisit https://vercel.com/oauth/device?user_code=MOCK-CODE\n');
    c.child.stderr.write(b.subarray(0,2));c.child.stderr.write(b.subarray(2,31));c.child.stderr.write(b.subarray(31));
    c.child.stderr.write('https://vercel.com.evil.example/oauth/device?user_code=NO\nhttps://evil.example/device\n');
    await writeFile(join(c.dir,'auth.json'),JSON.stringify({token:'MOCK_LOGIN_TOKEN'}));
    c.child.emit('close',0);
  });
  try {const r=await h.runner.run(['login'],{cwd:h.root,onDeviceUrl:url=>urls.push(url)});assert.ok(r.stderr.includes('로그인 안내'));assert.deepEqual(urls,['https://vercel.com/oauth/device?user_code=MOCK-CODE']);}
  finally {await h.cleanup();}
});

test('device URI는 HTTPS 공식 도메인·로그인 경로만 허용한다',()=>{
  assert.ok(validateVercelDeviceUrl('https://vercel.com/device?user_code=MOCK'));
  for(const url of ['http://vercel.com/device','https://user:pass@vercel.com/device','https://vercel.com:444/device','https://vercel.com/device#x','https://vercel.com/other','https://vercel.com.evil/device','javascript:alert(1)'])assert.equal(validateVercelDeviceUrl(url),undefined);
});

for(const part of ['stdout','stderr'] as const)test(`로그인 ${part}의 완결 URI는 개행·종료 전 공백 구분으로 전달하고 부분 query는 열지 않는다`,async()=>{
  const urls:string[]=[],full='https://vercel.com/oauth/device?user_code=MOCK-CODE&team_id=team_mock';
  let closed=false;
  const h=await harness(async c=>{
    const stream=c.child[part],other=c.child[part==='stdout'?'stderr':'stdout'];
    const prefix=Buffer.from('\n  Visit https://vercel.com/oauth/device');
    stream.write(prefix.subarray(0,11));stream.write(prefix.subarray(11));
    assert.deepEqual(urls,[]);
    stream.write('?user_code=MOCK-');other.write('서로 섞이지 않는 진행 안내\r\n');
    assert.deepEqual(urls,[]);
    stream.write('CODE&team_');assert.deepEqual(urls,[]);
    stream.write('id=team_mock');assert.deepEqual(urls,[]);
    stream.write(' ');assert.deepEqual(urls,[full]);assert.equal(closed,false);
    stream.write(`\u001b[1m${full}\u001b[0m\r\n`);assert.deepEqual(urls,[full]);
    await writeFile(join(c.dir,'auth.json'),'{"token":"MOCK_LOGIN_TOKEN"}');
    closed=true;c.child.emit('close',0);
  });
  try{await h.runner.run(['login'],{cwd:h.root,onDeviceUrl:url=>urls.push(url)});assert.deepEqual(urls,[full]);}
  finally{await h.cleanup();}
});

test('stdout와 stderr의 조각을 이어 가짜 device URI를 만들지 않는다',async()=>{
  const urls:string[]=[];
  const h=await harness(async c=>{
    c.child.stdout.write('Visit https://vercel.com/oauth/dev');
    c.child.stderr.write('ice?user_code=WRONG-CODE\n');assert.deepEqual(urls,[]);
    c.child.stdout.write('ice?user_code=MOCK-CODE\r');
    assert.deepEqual(urls,['https://vercel.com/oauth/device?user_code=MOCK-CODE']);
    await writeFile(join(c.dir,'auth.json'),'{"token":"MOCK_LOGIN_TOKEN"}');c.child.emit('close',0);
  });
  try{await h.runner.run(['login'],{cwd:h.root,onDeviceUrl:url=>urls.push(url)});}
  finally{await h.cleanup();}
});

test('로그인 취소 뒤 남은 URI 구분자와 종료 출력은 승인 페이지를 열지 않는다',async()=>{
  const urls:string[]=[],abort=new AbortController();
  const h=await harness(c=>{
    c.child.stderr.write('Visit https://vercel.com/oauth/device?user_code=MOCK-CODE');
    abort.abort();c.child.stderr.end('\n');c.child.stdout.end('https://vercel.com/device?user_code=LATE-CODE\n');
  });
  try{await assert.rejects(h.runner.run(['login'],{cwd:h.root,signal:abort.signal,onDeviceUrl:url=>urls.push(url)}),{code:'VERCEL_CANCELLED'});assert.deepEqual(urls,[]);}
  finally{await h.cleanup();}
});

test('stdin 본문은 인자에 넣지 않으며 main용 JSON은 보존하고 진단의 비밀만 숨긴다',async()=>{
  const input='{"password":"MOCK_PASSCODE"}';
  const h=await harness(c=>{
    assert.equal(c.args.some(arg=>arg.includes('MOCK_PASSCODE')),false);
    let text='';c.child.stdin.on('data',chunk=>text+=chunk);c.child.stdin.on('end',()=>{assert.equal(text,input);c.child.stdout.end(input);c.child.stderr.end(input);c.child.emit('close',0);});c.child.stdin.resume();
  });
  try {const r=await h.runner.run(['api','/v10/projects/mock/env','--input','-'],{cwd:h.root,input});assert.deepEqual(JSON.parse(r.stdout),JSON.parse(input));assert.equal(r.stderr.includes('MOCK_PASSCODE'),false);}
  finally {await h.cleanup();}
});

test('시간 제한·출력 상한·취소는 소유한 자식만 종료하고 인증 임시폴더를 정리한다',async()=>{
  for(const mode of ['timeout','limit','abort'] as const) {
    const abort=new AbortController();
    const h=await harness(c=>{if(mode==='limit')c.child.stdout.write(Buffer.alloc(200));if(mode==='abort')abort.abort();},{timeoutMs:20,maxOutputBytes:100});
    try {await assert.rejects(h.runner.run(['login'],{cwd:h.root,signal:abort.signal}),{code:mode==='timeout'?'VERCEL_TIMEOUT':mode==='limit'?'VERCEL_OUTPUT_LIMIT':'VERCEL_CANCELLED'});assert.deepEqual(h.calls[0].child.kills,['SIGTERM']);assert.deepEqual(await readdir(join(h.root,'private')),[]);}
    finally {await h.cleanup();}
  }
});

test('API 비0 종료도 결과로 반환하며 연결 실패 뒤 인증 갱신을 보존한다',async()=>{
  const h=await harness(async c=>{await writeFile(join(c.dir,'auth.json'),JSON.stringify({token:'UPDATED_TOKEN',refreshToken:'UPDATED_REFRESH'}));c.child.stdout.end('{"status":"action_required"}');c.child.emit('close',1);});
  try {const r=await h.runner.run(['integration','add','neon','--json','--non-interactive'],{cwd:h.root});assert.equal(r.code,1);assert.equal(JSON.parse(r.stdout).status,'action_required');assert.ok(await h.vault.get(VERCEL_AUTH_VAULT_KEY));}
  finally {await h.cleanup();}
});

test('직렬 실행으로 같은 refresh token의 동시 갱신을 막는다',async()=>{
  let finish:()=>void=()=>{};
  const h=await harness(c=>new Promise<void>(resolve=>{finish=()=>{c.child.emit('close',0);resolve();};}));
  try {
    const first=h.runner.run(['api','/v2/teams','--raw'],{cwd:h.root});
    while(h.calls.length===0)await new Promise(resolve=>setTimeout(resolve,1));
    const second=h.runner.run(['api','/v2/teams','--raw'],{cwd:h.root});
    await new Promise(resolve=>setTimeout(resolve,5));assert.equal(h.calls.length,1);finish();await first;
    while(h.calls.length<2)await new Promise(resolve=>setTimeout(resolve,1));finish();await second;
  } finally {await h.cleanup();}
});

test('인증 경로·토큰·debug 우회 옵션과 실행 전 취소는 자식을 만들지 않는다',async()=>{
  const h=await harness(c=>{c.child.emit('close',0);});
  try {
    for(const option of ['--global-config=/outside','-Q','--token','-t','--cwd','--local-config','--api','--debug','-d'])await assert.rejects(h.runner.run(['api',option],{cwd:h.root}),{code:'VERCEL_ARGS_INVALID'});
    const abort=new AbortController();abort.abort();await assert.rejects(h.runner.run(['login'],{cwd:h.root,signal:abort.signal}),{code:'VERCEL_CANCELLED'});assert.equal(h.calls.length,0);
  } finally {await h.cleanup();}
});

test('인증파일 symlink는 vault로 읽지 않고 전용폴더만 삭제한다',async t=>{
  const h=await harness(async c=>{
    const outside=join(h.root,'outside.json');await writeFile(outside,'{"token":"OUTSIDE"}');
    try {await symlink(outside,join(c.dir,'auth.json'));}
    catch(error) {
      if(process.platform!=='win32'||(error as NodeJS.ErrnoException).code!=='EPERM')throw error;
      // 일반 직원 Windows의 파일 링크 권한을 높이지 않고 외부 디렉터리 링크도 거부하는지 확인한다.
      const directory=join(h.root,'outside-directory');await mkdir(directory);await writeFile(join(directory,'auth.json'),'{"token":"OUTSIDE"}');
      await symlink(directory,join(c.dir,'auth.json'),'junction');
      t.diagnostic('파일 symlink 생성 권한이 없어 디렉터리 junction의 동일 거부·외부 보존 경계를 검사했습니다.');
    }
    c.child.emit('close',0);
  });
  try {await assert.rejects(h.runner.run(['login'],{cwd:h.root}),{code:'VERCEL_AUTH_SAVE_FAILED'});assert.equal(await readFile(join(h.root,'outside.json'),'utf8'),'{"token":"OUTSIDE"}');assert.equal(await h.vault.get(VERCEL_AUTH_VAULT_KEY),undefined);}
  finally {await h.cleanup();}
});

test('환경변수는 대소문자가 다른 Windows 자격증명도 제외한다',()=>{
  const env=vercelChildEnvironment({Path:'safe',SystemRoot:'C:\\Windows',vercel_token:'SECRET',Github_Token:'SECRET',PYTHONSTARTUP:'SECRET',NODE_PATH:'SECRET',SSH_AUTH_SOCK:'SECRET'});
  assert.equal(env.Path,'safe');assert.equal(env.SystemRoot,'C:\\Windows');assert.equal(env.vercel_token,undefined);assert.equal(env.Github_Token,undefined);assert.equal(env.PYTHONSTARTUP,undefined);assert.equal(env.NODE_PATH,undefined);assert.equal(env.SSH_AUTH_SOCK,undefined);
});

test('성공 종료라도 로그인 파일이 없으면 연결 성공으로 처리하지 않는다',async()=>{
  const h=await harness(c=>{c.child.emit('close',0);});
  try {await assert.rejects(h.runner.run(['login'],{cwd:h.root}),{code:'VERCEL_AUTH_SAVE_FAILED'});assert.deepEqual(await readdir(join(h.root,'private')),[]);}
  finally {await h.cleanup();}
});

test('로그아웃이 파일을 지우면 앱 vault에서도 인증 전체를 지운다',async()=>{
  const vault=new Vault();vault.values.set(VERCEL_AUTH_VAULT_KEY,{version:1,authJson:'{"token":"MOCK_OLD"}'});
  const h=await harness(async c=>{await rm(join(c.dir,'auth.json'));c.child.emit('close',0);},{vault});
  try {await h.runner.run(['logout'],{cwd:h.root});assert.equal(await vault.get(VERCEL_AUTH_VAULT_KEY),undefined);}
  finally {await h.cleanup();}
});

test('환경변수 조회의 DB URL을 main용 JSON에서 보존하고 진단에서는 숨긴다',async()=>{
  const dsn='postgresql://mock:MOCK_DB_PASSWORD@db.example.invalid/audit?sslmode=require';
  const h=await harness(c=>{const value=JSON.stringify({envs:[{key:'DATABASE_URL',value:dsn}]});c.child.stdout.end(value);c.child.stderr.end(value);c.child.emit('close',0);});
  try {const r=await h.runner.run(['api','/v10/projects/mock/env?decrypt=true','--raw'],{cwd:h.root});assert.equal(JSON.parse(r.stdout).envs[0].value,dsn);assert.equal(r.stderr.includes('MOCK_DB_PASSWORD'),false);}
  finally {await h.cleanup();}
});

const MOCK_API_TOKEN='MOCK_VERCEL_API_TOKEN',MOCK_REFRESH_TOKEN='MOCK_VERCEL_REFRESH_TOKEN';
async function authenticated(c:Invocation) {
  assert.equal(c.args[1],'whoami');assert.equal(c.args.some(arg=>arg.includes(MOCK_API_TOKEN)),false);
  await writeFile(join(c.dir,'auth.json'),JSON.stringify({token:MOCK_API_TOKEN,refreshToken:MOCK_REFRESH_TOKEN,expiresAt:123456}));
  c.child.stdout.end('모의 Vercel 사용자');c.child.emit('close',0);
}

test('직접 API는 공식 whoami 갱신 뒤 고정 origin·teamId·redirect:error로 한 번만 요청한다',async()=>{
  const dsn='postgresql://mock:MOCK_PASS@db.example.invalid/audit';let requests=0;
  const h=await harness(authenticated,{fetch:async (url,options)=>{
    requests++;const address=new URL(String(url));assert.equal(address.origin,'https://api.vercel.com');assert.equal(address.pathname,'/v11/projects');
    assert.deepEqual(address.searchParams.getAll('teamId'),['team_selected']);assert.equal(address.searchParams.has('slug'),false);
    assert.equal(options!.method,'POST');assert.equal(options!.redirect,'error');assert.equal(options!.headers&&new Headers(options!.headers).get('authorization'),`Bearer ${MOCK_API_TOKEN}`);
    assert.deepEqual(JSON.parse(options!.body as string),{name:'모의 프로젝트'});
    return Response.json({id:'prj_mock',dsn,echo:MOCK_API_TOKEN,refresh:MOCK_REFRESH_TOKEN},{status:201});
  }});
  try {
    const result=await h.runner.requestApi('/v11/projects?teamId=wrong&teamId=other&slug=wrong',{cwd:h.root,teamId:'team_selected',method:'POST',body:{name:'모의 프로젝트'}});
    assert.equal(result.status,201);assert.equal((result.body as {dsn:string}).dsn,dsn);assert.equal(requests,1);assert.equal(h.calls.length,1);
    assert.equal(JSON.stringify(result).includes(MOCK_API_TOKEN),false);assert.equal(JSON.stringify(result).includes(MOCK_REFRESH_TOKEN),false);
    assert.deepEqual(await readdir(join(h.root,'private')),[]);
  } finally {await h.cleanup();}
});

test('401과422는 본문·상태를 보존하며 POST를 자동 재시도하지 않는다',async()=>{
  for(const status of [401,422]) {
    let requests=0;const body=status===422?{kind:'requires_browser',url:'https://vercel.com/team/~/integrations/accept-terms/neon',installations:[{id:'icfg_mock'}]}:{error:{code:'not_authorized',message:'다시 로그인하세요'}};
    const h=await harness(authenticated,{fetch:async()=>{requests++;return Response.json(body,{status});}});
    try {const result=await h.runner.requestApi('/v1/integrations/integration/neon/marketplace/auto-provision/mock',{cwd:h.root,teamId:'team_mock',method:'POST',body:{name:'mock'}});assert.deepEqual(result,{status,body});assert.equal(requests,1);assert.equal(h.calls.length,1);}
    finally {await h.cleanup();}
  }
});

test('API의 외부 origin·경로 우회·DELETE는 인증 갱신이나 HTTP 전에 차단한다',async()=>{
  let requests=0;const h=await harness(authenticated,{fetch:async()=>{requests++;return Response.json({});}});
  try {
    for(const path of ['https://evil.example/v1/teams','//evil.example/v1/teams','/v2/../teams','/v2/%2e%2e/teams','/v2/%252e%252e/teams','/v2/%2f%2fevil.example/x','/v2/teams\\x','/v2/teams#secret','/v2/%00teams','/v2/%0ateams','/v2/…/teams'])
      await assert.rejects(h.runner.requestApi(path,{cwd:h.root}),{code:'VERCEL_API_PATH_INVALID'});
    await assert.rejects(h.runner.requestApi('/v2/teams?teamId=hidden',{cwd:h.root}),{code:'VERCEL_API_TEAM_INVALID'});
    await assert.rejects(h.runner.requestApi('/v2/teams',{cwd:h.root,method:'DELETE' as 'GET'}),{code:'VERCEL_API_METHOD_INVALID'});
    assert.equal(requests,0);assert.equal(h.calls.length,0);
  } finally {await h.cleanup();}
});

test('API 취소·시간 제한은 토큰이나 fetch 내부 오류를 반환하지 않는다',async()=>{
  for(const timeout of [false,true]) {
    const abort=new AbortController();
    const h=await harness(authenticated,{apiTimeoutMs:timeout?20:1000,fetch:async(_url,options)=>new Promise((_resolve,reject)=>{
      options!.signal!.addEventListener('abort',()=>reject(Error(MOCK_API_TOKEN)),{once:true});if(!timeout)abort.abort();
    })});
    try {await assert.rejects(h.runner.requestApi('/v2/teams',{cwd:h.root,signal:abort.signal}),error=>{assert.equal((error as {code:string}).code,timeout?'VERCEL_API_TIMEOUT':'VERCEL_API_CANCELLED');assert.equal(String(error).includes(MOCK_API_TOKEN),false);return true;});assert.deepEqual(await readdir(join(h.root,'private')),[]);}
    finally {await h.cleanup();}
  }
});

test('직접 API의 본문 크기·비JSON·실패 오류는 안전하게 제한한다',async()=>{
  for(const mode of ['size','json','transport'] as const) {
    const h=await harness(authenticated,{maxApiBytes:64,fetch:async()=>{
      if(mode==='transport')throw Object.assign(Error(MOCK_API_TOKEN),{code:'VERCEL_FAKE_EXTERNAL'});
      return new Response(mode==='size'?JSON.stringify({message:'x'.repeat(100)}):MOCK_API_TOKEN,{status:502});
    }});
    try {await assert.rejects(h.runner.requestApi('/v2/teams',{cwd:h.root}),error=>{assert.equal((error as {code:string}).code,mode==='size'?'VERCEL_API_BODY_LIMIT':mode==='json'?'VERCEL_API_BODY_INVALID':'VERCEL_API_FAILED');assert.equal(String(error).includes(MOCK_API_TOKEN),false);return true;});}
    finally {await h.cleanup();}
  }
});

test('직접 API와 CLI 실행은 같은 tail을 공유하고 대기 중 취소도 지킨다',async()=>{
  let finish:()=>void=()=>{},requests=0;
  const h=await harness(c=>{if(c.args[1]==='whoami')return authenticated(c);return new Promise<void>(resolve=>{finish=()=>{c.child.emit('close',0);resolve();};});},{fetch:async()=>{requests++;return Response.json({teams:[]});}});
  try {
    const first=h.runner.run(['api','/v2/teams','--raw'],{cwd:h.root});while(!h.calls.length)await new Promise(resolve=>setTimeout(resolve,1));
    const abort=new AbortController(),second=h.runner.requestApi('/v2/teams',{cwd:h.root,signal:abort.signal});abort.abort();
    await new Promise(resolve=>setTimeout(resolve,5));assert.equal(h.calls.length,1);finish();await first;
    await assert.rejects(second,{code:'VERCEL_API_CANCELLED'});assert.equal(requests,0);assert.equal(h.calls.length,1);
  } finally {await h.cleanup();}
});
