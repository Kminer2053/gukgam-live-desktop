import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {spawn} from 'node:child_process';
import {link,mkdir,mkdtemp,readFile,readdir,rm,symlink,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {VercelCliRunner,vercelChildEnvironment} from '../src/relay/vercel-cli';

class Child extends EventEmitter {
  stdin=new PassThrough();stdout=new PassThrough();stderr=new PassThrough();
  kill(){queueMicrotask(()=>this.emit('close',null));return true;}
}
async function fixture(code=0) {
  const root=await mkdtemp(join(tmpdir(),'gukgam-deploy-binding-'));
  const calls:Array<{args:readonly string[];env:NodeJS.ProcessEnv;cwd:string}>=[];
  let vaultReads=0;
  const vault={get:async<T>(_key:string)=>{vaultReads++;return undefined as T|undefined;},set:async()=>{},delete:async()=>{}};
  const ambient={VERCEL_ORG_ID:'AMBIENT_ORG',VERCEL_PROJECT_ID:'AMBIENT_PROJECT',VERCEL_TOKEN:'AMBIENT_TOKEN',PATH:'SYNTHETIC_PATH'};
  const runner=new VercelCliRunner({cliPath:join(root,'bundle','vc.js'),tempRoot:join(root,'private'),vault,env:ambient,
    spawn:((_executable:string,args:readonly string[],options:any)=>{
      const child=new Child();calls.push({args:[...args],env:{...options.env},cwd:options.cwd});
      queueMicrotask(()=>{child.stdout.end('https://synthetic-deploy.vercel.app\n');child.emit('close',code);});return child;
    }) as unknown as typeof spawn});
  const saveLink=async(value:unknown={orgId:'team_fixture',projectId:'project_fixture',projectName:'synthetic-project'})=>{
    await mkdir(join(root,'.vercel'),{recursive:true});await writeFile(join(root,'.vercel','project.json'),typeof value==='string'?value:JSON.stringify(value));
  };
  return {root,runner,calls,ambient,saveLink,vaultReads:()=>vaultReads,cleanup:()=>rm(root,{recursive:true,force:true})};
}
const base=['deploy','--prod','--yes','--scope','team_fixture'];

for(const explicit of [false,true])test(`배포 ${explicit?'명시 project pair 제거':'저장 링크'}는 검증한 두 ID를 해당 자식에게만 전달한다`,async()=>{
  const f=await fixture();try{
    await f.saveLink();const original=await readFile(join(f.root,'.vercel','project.json'),'utf8');
    const args=explicit?[...base,'--project','project_fixture']:[...base];
    const result=await f.runner.run(args,{cwd:f.root});assert.equal(result.code,0);
    assert.equal(f.calls.length,1);assert.deepEqual(f.calls[0].args.slice(1,-2),base);
    assert.equal(f.calls[0].args.includes('--project'),false);
    assert.equal(f.calls[0].env.VERCEL_ORG_ID,'team_fixture');assert.equal(f.calls[0].env.VERCEL_PROJECT_ID,'project_fixture');
    assert.equal(f.calls[0].env.VERCEL_TOKEN,undefined);assert.equal(f.calls[0].cwd,f.root);
    assert.deepEqual(f.ambient,{VERCEL_ORG_ID:'AMBIENT_ORG',VERCEL_PROJECT_ID:'AMBIENT_PROJECT',VERCEL_TOKEN:'AMBIENT_TOKEN',PATH:'SYNTHETIC_PATH'});
    assert.equal(await readFile(join(f.root,'.vercel','project.json'),'utf8'),original);
    await f.runner.run(['whoami'],{cwd:f.root});
    assert.equal(f.calls[1].env.VERCEL_ORG_ID,undefined);assert.equal(f.calls[1].env.VERCEL_PROJECT_ID,undefined);
  }finally{await f.cleanup();}
});

test('기본 자식 환경은 대소문자가 다른 ambient project·org ID도 상속하지 않는다',()=>{
  assert.deepEqual(Object.keys(vercelChildEnvironment({vercel_org_id:'ambient',Vercel_Project_Id:'ambient'})).filter(key=>/org_id|project_id/i.test(key)),[]);
});

test('scope/project 누락·중복·옵션형식 우회는 vault나 자식 실행 전에 거부한다',async()=>{
  const cases=[['deploy','--prod','--yes'],[...base,'--scope','team_fixture'],[...base,'--project'],
    [...base,'--project','project_fixture','--project','project_fixture'],[...base,'--project=project_fixture'],
    ['deploy','--prod','--yes','--scope=team_fixture'],[...base,'--project','--prod'],[...base,'--env','SECRET=value'],
    [...base,'--prod'],[...base,'--yes']];
  for(const args of cases){const f=await fixture();try{
    await f.saveLink();await assert.rejects(f.runner.run(args,{cwd:f.root}),{code:'VERCEL_ARGS_INVALID'});
    assert.equal(f.calls.length,0);assert.equal(f.vaultReads(),0);
  }finally{await f.cleanup();}}
});

for(const value of [
  {orgId:'other_team',projectId:'project_fixture'},
  {orgId:'team_fixture',projectId:'other_project'},
])test(`명시 연결 불일치는 배포하지 않는다 ${value.orgId}/${value.projectId}`,async()=>{
  const f=await fixture();try{
    await f.saveLink(value);await assert.rejects(f.runner.run([...base,'--project','project_fixture'],{cwd:f.root}),{code:'VERCEL_ARGS_INVALID'});
    assert.equal(f.calls.length,0);assert.equal(f.vaultReads(),0);
  }finally{await f.cleanup();}
});

test('없거나 손상·비정규·과대한 링크는 외부 출력 없이 거부하고 신규 프로젝트를 만들지 않는다',async()=>{
  const values:unknown[]=['{invalid',[],{},null,{orgId:'team_fixture'},{orgId:'--scope',projectId:'project_fixture'},
    {orgId:'team_fixture',projectId:'../outside'},' '.repeat(4097)];
  for(const value of [undefined,...values]){const f=await fixture();try{
    if(value!==undefined)await f.saveLink(value);
    await assert.rejects(f.runner.run(base,{cwd:f.root}),{code:'VERCEL_PATH_INVALID'});
    assert.equal(f.calls.length,0);assert.equal(f.vaultReads(),0);
    assert.equal((await readdir(f.root)).includes('private'),false);
  }finally{await f.cleanup();}}
  const f=await fixture();try{
    await mkdir(join(f.root,'.vercel','project.json'),{recursive:true});
    await assert.rejects(f.runner.run(base,{cwd:f.root}),{code:'VERCEL_PATH_INVALID'});assert.equal(f.calls.length,0);
  }finally{await f.cleanup();}
});

test('실제 directory symlink/junction인 링크 부모와 leaf는 따라가지 않는다',async()=>{
  for(const leaf of [false,true]){const f=await fixture();try{
    const outside=join(f.root,'outside');await mkdir(outside);
    await writeFile(join(outside,'project.json'),JSON.stringify({orgId:'team_fixture',projectId:'project_fixture'}));
    const destination=leaf?join(f.root,'.vercel','project.json'):join(f.root,'.vercel');
    if(leaf)await mkdir(join(f.root,'.vercel'));
    await symlink(outside,destination,process.platform==='win32'?'junction':'dir');
    await assert.rejects(f.runner.run(base,{cwd:f.root}),{code:'VERCEL_PATH_INVALID'});
    assert.equal(f.calls.length,0);assert.equal(f.vaultReads(),0);
    assert.equal(JSON.parse(await readFile(join(outside,'project.json'),'utf8')).projectId,'project_fixture');
  }finally{await f.cleanup();}}
});

test('일반 파일 hardlink도 project 연결 파일로 채택하지 않는다',async()=>{
  const f=await fixture();try{
    await mkdir(join(f.root,'.vercel'));const outside=join(f.root,'outside.json');
    await writeFile(outside,JSON.stringify({orgId:'team_fixture',projectId:'project_fixture'}));
    await link(outside,join(f.root,'.vercel','project.json'));
    await assert.rejects(f.runner.run(base,{cwd:f.root}),{code:'VERCEL_PATH_INVALID'});assert.equal(f.calls.length,0);
  }finally{await f.cleanup();}
});

test('실행 전 취소와 공식 CLI의 비0 종료는 다른 프로젝트로 재시도하지 않는다',async()=>{
  const cancelled=await fixture();try{
    const signal=new AbortController();signal.abort();
    await assert.rejects(cancelled.runner.run(base,{cwd:cancelled.root,signal:signal.signal}),{code:'VERCEL_CANCELLED'});
    assert.equal(cancelled.calls.length,0);assert.equal(cancelled.vaultReads(),0);
  }finally{await cancelled.cleanup();}
  const failed=await fixture(1);try{
    await failed.saveLink();const result=await failed.runner.run([...base,'--project','project_fixture'],{cwd:failed.root});
    assert.equal(result.code,1);assert.equal(failed.calls.length,1);
    assert.equal(failed.calls[0].env.VERCEL_PROJECT_ID,'project_fixture');
  }finally{await failed.cleanup();}
});
