import assert from 'node:assert/strict';
import {access,mkdir,writeFile} from 'node:fs/promises';
import {join,resolve} from 'node:path';
import {argumentsMap,assertInside,assertJuliaPrepareIntent,assertPortClosed,assertUnavailableJulia,assertWindowsCi,exerciseRecords,installedPaths,isolatedEnvironment,localRequest,ORIGIN,within} from './smoke-contract.mjs';

assertWindowsCi();
const {_electron}=await import('playwright');
const args=argumentsMap(),outputDir=resolve(args.output),stateDir=resolve(args.state);
assertInside(process.env.RUNNER_TEMP,stateDir);
const paths=await installedPaths(args.install,args.app);
await mkdir(outputDir,{recursive:true});await mkdir(stateDir,{recursive:true});
const report={kind:'installed-electron-ui',passed:false,source_commit:process.env.GITHUB_SHA,prepared_installed_app:true,
  actual_oauth:false,actual_model:false,actual_model_download:false,actual_assembly:false,checks:{},page_errors:[],unexpected_requests:[]};
let application,setup,workspace,fixture,appStdout='',appStderr='';
const watchdog=setTimeout(()=>{console.error('설치된 앱의 GUI 검증 전체 제한 시간을 넘었습니다.');process.exit(1);},150_000);
function observe(page){
  page.on('pageerror',error=>report.page_errors.push(String(error.message).slice(0,500)));
  page.on('request',request=>{const url=request.url();if(!url.startsWith('file:')&&!url.startsWith(ORIGIN+'/')&&!url.startsWith('data:')&&!url.startsWith('blob:'+ORIGIN+'/'))report.unexpected_requests.push(new URL(url).origin);});
}
try{
  await assertPortClosed();
  application=await _electron.launch({executablePath:paths.executable,args:['--gukgam-ci-smoke'],cwd:paths.root,
    env:{...isolatedEnvironment(),CI:'true',GUKGAM_DESKTOP_SMOKE_DIR:stateDir},timeout:30_000});
  const ownedProcess=application.process();
  ownedProcess.stdout?.on('data',chunk=>{appStdout=(appStdout+chunk.toString('utf8')).slice(-64_000);});
  ownedProcess.stderr?.on('data',chunk=>{appStderr=(appStderr+chunk.toString('utf8')).slice(-64_000);});
  application.on('window',observe);
  await application.context().route('**/*',route=>{
    const url=route.request().url();
    return url.startsWith('file:')||url.startsWith(ORIGIN+'/')?route.continue():route.abort('blockedbyclient');
  });
  setup=await application.firstWindow();observe(setup);
  const identity=await application.evaluate(({app})=>({packaged:app.isPackaged,appPath:app.getAppPath(),userData:app.getPath('userData'),sessionData:app.getPath('sessionData'),version:app.getVersion()}));
  assert.equal(identity.packaged,true);assert.ok(identity.appPath.endsWith('app.asar'));
  assert.equal(resolve(identity.userData).toLowerCase(),stateDir.toLowerCase());assertInside(stateDir,identity.sessionData);
  report.checks.packaged_main_and_temp_appdata=true;report.version=identity.version;
  await setup.locator('#organization').waitFor({state:'visible'});
  await setup.locator('#organization').fill('Windows 설치 검증 기관');
  await setup.locator('#save-org').click();
  await setup.locator('#start-no-ai').waitFor({state:'visible'});
  await setup.screenshot({path:join(outputDir,'01-installed-wizard.png'),fullPage:true});
  report.checks.real_wizard_saved=true;
  const opened=application.waitForEvent('window',{predicate:page=>page!==setup,timeout:35_000});
  await setup.locator('#start-no-ai').click();
  workspace=await opened;
  await workspace.waitForURL(ORIGIN+'/',{timeout:35_000});
  await workspace.locator('h1').filter({hasText:'대시보드'}).waitFor({state:'visible'});
  await setup.waitForFunction(()=>document.querySelector('#reopen')?.disabled===false);
  await workspace.screenshot({path:join(outputDir,'02-installed-workspace.png'),fullPage:true});
  report.checks.real_workspace_ready=true;
  // 실제 설치된 설정 화면의 저장 경로를 확인한다. 준비 POST는 브라우저에서 가로채
  // 명시적인 확인 값만 검사하며 모델·계정·외부 네트워크는 실행하지 않는다.
  const prepareRequests=[];
  await workspace.route(`${ORIGIN}/api/settings/role-decision/julia/prepare`,async route=>{
    const request=route.request();let body;try{body=request.postDataJSON();}catch{}
    prepareRequests.push({url:request.url(),method:request.method(),body});
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({preparing:false})});
  });
  const openInstalledAdmin=async()=>{
    await workspace.goto(`${ORIGIN}/admin`);
    await workspace.locator('#jev-status').filter({hasText:'저장된 선택:'}).waitFor({state:'visible'});
  };
  await openInstalledAdmin();
  assert.deepEqual(await workspace.locator('#role-decision-provider option').evaluateAll(options=>options.map(option=>option.value)),['none','jev','julia']);
  assert.equal(await workspace.locator('#role-decision-provider').inputValue(),'none');
  assert.deepEqual(await workspace.locator('#julia-threads option').evaluateAll(options=>options.map(option=>option.value)),['1','2']);
  assert.equal(await workspace.locator('#julia-threads').inputValue(),'2');
  assert.equal((await localRequest('/api/settings')).data.transcript_review.role_decision_provider,'none');
  await workspace.screenshot({path:join(outputDir,'06-installed-role-default.png'),fullPage:true});
  report.checks.installed_role_options_and_default=true;
  const saveInstalledRole=async(provider,threads)=>{
    await workspace.locator('#role-decision-provider').selectOption(provider);
    await workspace.locator('#review-enabled').uncheck();
    if(provider==='julia')await workspace.locator('#julia-threads').selectOption(String(threads));
    const saved=workspace.waitForResponse(response=>response.url()===`${ORIGIN}/api/settings/transcript_review`&&response.request().method()==='PUT');
    await workspace.locator('#f-speaker-review button[type="submit"]').click();
    assert.equal((await saved).status(),200);
    await openInstalledAdmin();
    assert.equal(await workspace.locator('#role-decision-provider').inputValue(),provider);
    const persisted=(await localRequest('/api/settings')).data.transcript_review;
    assert.equal(persisted.role_decision_provider,provider);
    if(provider==='julia')assert.equal(persisted.julia_threads,threads);
  };
  await saveInstalledRole('jev');
  await saveInstalledRole('julia',1);
  assert.equal(await workspace.locator('#julia-threads').inputValue(),'1');
  assert.equal(await workspace.locator('#julia-prepare-panel').isVisible(),true);
  assert.equal(prepareRequests.length,0,'역할 선택·설정 저장만으로 모델을 내려받으면 안 됩니다.');
  const missingModel=(await localRequest('/api/settings/role-decision/status')).data;
  assertUnavailableJulia(missingModel);
  assert.match(await workspace.locator('#jev-status').textContent(),/사전 판정 준비 필요 · 기존 ChatGPT 처리 유지/);
  assert.match(await workspace.locator('#julia-model-status').textContent(),/모델을 준비해야/);
  assert.equal(await workspace.locator('#julia-prepare-cancel').isDisabled(),true);
  await localRequest('/api/settings/transcript_review',{method:'PUT',body:{julia_threads:3},expected:422});
  await localRequest('/api/settings/role-decision/julia/prepare',{method:'POST',body:{},expected:422});
  await workspace.screenshot({path:join(outputDir,'07-installed-julia-missing-model.png'),fullPage:true});
  report.checks.installed_role_selection_persisted=true;
  report.checks.installed_cpu_threads_bounded=true;
  report.checks.installed_missing_model_fallback_visible=true;
  report.checks.installed_prepare_requires_confirm=true;
  const preparedIntent=workspace.waitForResponse(response=>response.url()===`${ORIGIN}/api/settings/role-decision/julia/prepare`);
  await workspace.locator('#julia-prepare').click();await preparedIntent;
  assert.equal(prepareRequests.length,1);assertJuliaPrepareIntent(prepareRequests[0]);
  assert.equal(await access(join(stateDir,'records','models','julia-1')).then(()=>true,()=>false),false);
  report.checks.installed_prepare_button_intent_intercepted=true;
  report.julia_prepare={intercepted_ui_requests:prepareRequests.length,actual_model_download:false,actual_inference:false};
  await saveInstalledRole('none');
  report.checks.installed_none_path_restored=true;
  await workspace.unroute(`${ORIGIN}/api/settings/role-decision/julia/prepare`);
  fixture=await exerciseRecords(join(stateDir,'records'),outputDir);
  Object.assign(report.checks,fixture.checks);
  await workspace.goto(`${ORIGIN}/s/${fixture.sid}`);
  await workspace.getByText(fixture.edited,{exact:false}).first().waitFor({state:'visible',timeout:15_000});
  await workspace.screenshot({path:join(outputDir,'03-installed-record-and-human-edit.png'),fullPage:true});
  report.checks.real_record_screen=true;
  await fixture.cleanup();fixture=undefined;
  await setup.locator('#stop').click();
  await setup.waitForFunction(()=>document.querySelector('#engine-state')?.textContent?.includes('종료했습니다'),undefined,{timeout:20_000});
  await setup.screenshot({path:join(outputDir,'04-installed-engine-stopped.png'),fullPage:true});
  await assertPortClosed();report.checks.gui_normal_shutdown=true;
  // 계정 연결 버튼은 누르지 않고 설치된 릴레이 구성 화면과 명시적 선택 경계를 확인한다.
  await setup.locator('#relay-next').click();
  await setup.locator('#managed-relay-form').waitFor({state:'visible'});
  const relayUi={login_enabled:await setup.locator('#relay-login').isEnabled(),
    project_name_editable:await setup.locator('#relay-project-name').isEditable(),
    team_unselected:(await setup.locator('#relay-team').inputValue())==='',
    catalog_disabled:await setup.locator('#relay-load-catalog').isDisabled(),
    plans_disabled:await setup.locator('#relay-load-plans').isDisabled(),
    plan_disabled:await setup.locator('#relay-plan').isDisabled(),
    creation_disabled:await setup.locator('#relay-provision').isDisabled(),
    resume_disabled:await setup.locator('#relay-resume').isDisabled(),
    verify_disabled:await setup.locator('#relay-verify').isDisabled(),
    consent_unchecked:!(await setup.locator('#relay-consent').isChecked())};
  for(const [name,value] of Object.entries(relayUi))assert.equal(value,true,`릴레이 초기 입력 제어: ${name}`);
  await setup.locator('#relay-project-name').fill('gukgam-ci-preview');
  assert.equal(await setup.locator('#relay-project-name').inputValue(),'gukgam-ci-preview');
  assert.equal(await setup.locator('#relay-operator-passcode').inputValue(),'');
  assert.equal(await setup.locator('#relay-viewer-passcode').inputValue(),'');
  await setup.screenshot({path:join(outputDir,'05-installed-relay-setup.png'),fullPage:true});
  report.relay_ui=relayUi;report.checks.installed_relay_setup_visible=true;
  report.checks.relay_requires_explicit_account_and_plan=true;
  assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.unexpected_requests,[]);
  await application.close();application=undefined;
  await assertPortClosed();report.checks.no_leftover_listener=true;report.passed=true;
}catch(error){report.failure=error instanceof Error?error.message:'설치된 앱 GUI 검증 실패';process.exitCode=1;
  if(setup&&!setup.isClosed())await setup.screenshot({path:join(outputDir,'failure-setup.png'),fullPage:true}).catch(()=>{});
  if(workspace&&!workspace.isClosed())await workspace.screenshot({path:join(outputDir,'failure-workspace.png'),fullPage:true}).catch(()=>{});
}finally{
  if(fixture)await fixture.cleanup().catch(()=>{});
  if(application){
    const owned=application.process();
    await within(application.evaluate(({app})=>app.quit()),2000,'앱 종료 요청 대기 만료').catch(()=>{});
    try{await within(application.close(),12_000,'앱 종료 대기 만료');}
    catch{if(owned.exitCode===null){owned.kill();report.forced_owned_app_cleanup=true;process.exitCode=1;}}
  }
  clearTimeout(watchdog);
  await writeFile(join(outputDir,'electron.stdout.log'),appStdout);
  await writeFile(join(outputDir,'electron.stderr.log'),appStderr);
  await writeFile(join(outputDir,'gui-report.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({kind:report.kind,passed:report.passed,checks:report.checks,failure:report.failure}));
}
