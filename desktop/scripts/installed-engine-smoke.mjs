import assert from 'node:assert/strict';
import {spawn,spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {dirname,join,resolve} from 'node:path';
import {StringDecoder} from 'node:string_decoder';
import {argumentsMap,assertInside,assertPortClosed,assertWindowsCi,exerciseRecords,installedPaths,isolatedEnvironment,ORIGIN,parseMissingJuliaWorker,within} from './smoke-contract.mjs';

assertWindowsCi();
const args=argumentsMap(),outputDir=resolve(args.output),stateDir=resolve(args.state);
assertInside(process.env.RUNNER_TEMP,stateDir);
const paths=await installedPaths(args.install,args.app);
await mkdir(outputDir,{recursive:true});await mkdir(join(stateDir,'records'),{recursive:true});
const report={kind:'installed-engine',passed:false,source_commit:process.env.GITHUB_SHA,prepared_installed_app:true,ai_requests:0,network_targets:[ORIGIN],checks:{}};
let child,run,stderr='',protocolError,events=[];
const failures=[];
try{
 try{
  // 정확히 --version 하나를 전달해 동봉 CLI의 외부 호출 없는 조기 종료 경로를 사용한다.
  const cliPath=join(paths.root,'resources','relay-cli','node_modules','vercel','dist','vc.js');
  const cliPackage=JSON.parse(await readFile(join(dirname(cliPath),'..','package.json'),'utf8'));
  const cli=spawnSync(paths.executable,[cliPath,'--version'],{cwd:stateDir,
    env:{...isolatedEnvironment(),ELECTRON_RUN_AS_NODE:'1',NODE_DISABLE_COMPILE_CACHE:'1',
      VERCEL_CLI_USE_NATIVE_BINARY:'0',VERCEL_TELEMETRY_DISABLED:'1',NO_UPDATE_NOTIFIER:'1'},
    encoding:'utf8',timeout:20_000,maxBuffer:1_048_576,windowsHide:true});
  await writeFile(join(outputDir,'installed-cli-version.json'),JSON.stringify({
    kind:'installed-electron-node-cli',command:['installed-app.exe','resources/relay-cli/node_modules/vercel/dist/vc.js','--version'],
    expected_version:cliPackage.version,exit_code:cli.status,signal:cli.signal,error:cli.error?.message,
    stdout:cli.stdout??'',stderr:cli.stderr??'',actual_login:false,actual_deploy:false,
  },null,2)+'\n');
  assert.equal(cli.error,undefined,'설치 앱의 Node 모드에서 동봉 CLI를 실행하지 못했습니다.');
  assert.equal(cli.status,0);assert.equal(cli.stdout.trim(),cliPackage.version);assert.match(cli.stderr,/Vercel CLI/);
  report.checks.installed_cli_version=true;report.cli_version=cli.stdout.trim();
 }catch(error){
   report.cli_failure=error instanceof Error?error.message:'동봉 CLI 검증 실패';
   failures.push('동봉 CLI: '+report.cli_failure);
   await writeFile(join(outputDir,'installed-cli-failure.json'),JSON.stringify({failure:report.cli_failure},null,2)+'\n');
 }
 try{
  // 고정 CLI의 정적 루트 도움말만 읽는다. 동적 integration 도움말·계정 명령은 실행하지 않는다.
  const cliPath=join(paths.root,'resources','relay-cli','node_modules','vercel','dist','vc.js');
  const help=spawnSync(paths.executable,[cliPath,'--help'],{cwd:stateDir,
    env:{...isolatedEnvironment(),ELECTRON_RUN_AS_NODE:'1',NODE_DISABLE_COMPILE_CACHE:'1',
      VERCEL_CLI_USE_NATIVE_BINARY:'0',VERCEL_TELEMETRY_DISABLED:'1',NO_UPDATE_NOTIFIER:'1'},
    encoding:'utf8',timeout:20_000,maxBuffer:1_048_576,windowsHide:true});
  await writeFile(join(outputDir,'installed-cli-help.json'),JSON.stringify({kind:'installed-cli-static-help',
    exit_code:help.status,signal:help.signal,error:help.error?.message,stdout:help.stdout??'',stderr:help.stderr??'',
    actual_login:false,actual_deploy:false,full_cli_dependency_load:false},null,2)+'\n');
  assert.equal(help.error,undefined,'설치된 정적 CLI 도움말을 실행하지 못했습니다.');assert.equal(help.status,0);
  const text=(help.stdout??'')+'\n'+(help.stderr??'');
  assert.match(text,/login\s+\[email\]/);assert.match(text,/deploy\s+\[path\]/);
  report.checks.installed_cli_static_help=true;
 }catch(error){
  report.cli_help_failure=error instanceof Error?error.message:'동봉 CLI 정적 도움말 검증 실패';failures.push('CLI 정적 도움말: '+report.cli_help_failure);
 }
 try{
  const help=spawnSync(paths.engine,['--help'],{cwd:dirname(paths.engine),env:isolatedEnvironment(),encoding:'utf8',timeout:20_000,maxBuffer:1_048_576,windowsHide:true});
  await writeFile(join(outputDir,'installed-engine-help.json'),JSON.stringify({exit_code:help.status,signal:help.signal,
    error:help.error?.message,stdout:help.stdout??'',stderr:help.stderr??''},null,2)+'\n');
  assert.equal(help.error,undefined,'설치된 엔진의 --help 실행에 실패했습니다.');assert.equal(help.status,0,'설치된 엔진 --help의 종료 코드는 0이어야 합니다.');assert.match(help.stdout,/--config/);
  report.checks.installed_help=true;
 }catch(error){
  report.help_failure=error instanceof Error?error.message:'설치된 엔진 도움말 검증 실패';failures.push('엔진 도움말: '+report.help_failure);
 }
 try{
  const help=spawnSync(paths.engine,['--julia-worker','--help'],{cwd:dirname(paths.engine),env:isolatedEnvironment(),encoding:'utf8',timeout:20_000,maxBuffer:1_048_576,windowsHide:true});
  assert.equal(help.error,undefined);assert.equal(help.status,0);
  assert.match(help.stdout,/--model-dir/);assert.match(help.stdout,/--threads/);assert.match(help.stdout,/--max-tokens/);
  await writeFile(join(outputDir,'installed-julia-worker-help.json'),JSON.stringify({exit_code:help.status,stdout:help.stdout,actual_model:false},null,2)+'\n');
  const missing=join(stateDir,'missing-julia-model');
  const result=spawnSync(paths.engine,['--julia-worker','--model-dir',missing,'--threads','2','--max-tokens','1024'],
    {cwd:dirname(paths.engine),env:isolatedEnvironment(),input:'',encoding:'utf8',timeout:20_000,maxBuffer:1_048_576,windowsHide:true});
  assert.equal(result.error,undefined);assert.equal(result.status,2);
  const event=parseMissingJuliaWorker(result.stdout);
  assert.ok(!result.stdout.includes(missing),'모델 오류에 사용자 경로를 출력하지 않습니다.');
  await writeFile(join(outputDir,'installed-julia-worker-missing-model.json'),JSON.stringify({exit_code:result.status,event,
    actual_model:false,actual_model_download:false,actual_inference:false},null,2)+'\n');
  await assertPortClosed();
  report.checks.installed_julia_worker_help=true;report.checks.installed_julia_missing_model_json=true;
 }catch(error){
  report.julia_worker_failure=error instanceof Error?error.message:'설치된 Julia 작업자 경계 검증 실패';failures.push('Julia 작업자: '+report.julia_worker_failure);
 }
  report.engine_sha256=createHash('sha256').update(await readFile(paths.engine)).digest('hex');
  await assertPortClosed();
  const config=join(stateDir,'engine-config.json');await writeFile(config,JSON.stringify({organization:'Windows 설치 검증 기관'}));
  child=spawn(paths.engine,['--config',config],{cwd:dirname(paths.engine),env:{...isolatedEnvironment(),GUKGAM_DATA_DIR:join(stateDir,'records')},windowsHide:true,stdio:['pipe','pipe','pipe']});
  child.stdin.on('error',()=>{});
  const exited=new Promise(done=>child.once('exit',(code,signal)=>done({code,signal})));run={exited};
  child.stderr.on('data',chunk=>{stderr=(stderr+chunk.toString('utf8')).slice(-32_000);});
  const decoder=new StringDecoder('utf8');let buffer='';
  const ready=new Promise((done,fail)=>{
    const timer=setTimeout(()=>fail(Error('설치된 엔진 ready 대기 시간을 넘었습니다.')),30_000);
    child.once('error',()=>{clearTimeout(timer);fail(Error('설치된 엔진 프로세스를 만들지 못했습니다.'));});
    child.once('exit',()=>{clearTimeout(timer);fail(Error('ready 전에 설치된 엔진이 종료됐습니다.'));});
    child.stdout.on('data',chunk=>{
      try{
        buffer+=decoder.write(chunk);assert.ok(Buffer.byteLength(buffer)<=1_048_576);
        let end;while((end=buffer.indexOf('\n'))>=0){
          const line=buffer.slice(0,end);buffer=buffer.slice(end+1);if(!line.trim())continue;
          const event=JSON.parse(line);events.push(event.type);
          if(event.type==='llm.request'){report.ai_requests++;child.stdin.write(JSON.stringify({type:'llm.error',id:event.id,message:'CI에서는 AI를 실행하지 않습니다.'})+'\n');throw Error('AI 요청 없이 실행해야 합니다.');}
          if(event.type==='ready'){assert.equal(event.url,ORIGIN);clearTimeout(timer);done();}
          else assert.equal(event.type,'llm.cancel');
        }
      }catch(error){protocolError=error;clearTimeout(timer);fail(error);}
    });
  });
  await ready;report.checks.ready_jsonl=true;
  const fixture=await exerciseRecords(join(stateDir,'records'),outputDir);
  Object.assign(report.checks,fixture.checks);
  await fixture.cleanup();report.checks.fixture_crud_cleanup=true;
  assert.equal(protocolError,undefined);assert.equal(report.ai_requests,0);
  child.stdin.write('{"type":"shutdown"}\n');
  const exit=await within(exited,15_000,'정상 종료 시간을 넘었습니다.');
  assert.equal(exit.code,0);assert.equal(exit.signal,null);await assertPortClosed();report.checks.normal_shutdown=true;
  report.events=events;report.checks.installed_python_engine_runtime=true;
}catch(error){failures.push(error instanceof Error?error.message:'설치된 엔진 검증 실패');}
finally{
  if(child&&child.exitCode===null&&child.signalCode===null){child.stdin.write('{"type":"shutdown"}\n');await within(run.exited,5000,'정리 대기 만료').catch(()=>{});if(child.exitCode===null){child.kill();await within(run.exited,2000,'자식 종료 대기 만료').catch(()=>{});}}
  await writeFile(join(outputDir,'engine.stderr.log'),stderr);
  report.passed=failures.length===0;if(!report.passed){report.failure=failures.join(' / ');process.exitCode=1;}
  report.failures=failures;
  await writeFile(join(outputDir,'engine-report.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({kind:report.kind,passed:report.passed,checks:report.checks,failure:report.failure}));
}
