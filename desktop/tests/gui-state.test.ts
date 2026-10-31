import test from 'node:test';
import assert from 'node:assert/strict';
import {controls,type Task} from '../src/renderer/state';
import {applicationPaths,isolatedWebPreferences} from '../src/main/window-policy';
import {validateRelayForm} from '../src/renderer/relay-form';
import type {AppStatus} from '../src/shared/contracts';
const state:AppStatus={version:'mock',setup:{organization:'모의기관',hasRelayToken:false},chatgpt:{connected:false,verified:false,message:'연결 전',models:[]},engine:{state:'stopped',message:'대기'}};
test('로그인 대기·연결 오류·사이트 조회가 AI 없는 기록 시작을 막지 않는다',()=>{
  for(const task of ['account','relay-check','relay-help'] as Task[]){const result=controls(state,new Set([task]));assert.equal(result.startDisabled,false);assert.equal(result.startWithoutAiDisabled,false);}
  assert.equal(controls({...state,setup:{...state.setup,secureStorageWarning:'보안 저장소 미사용'}},new Set()).startDisabled,false);
  assert.equal(controls({...state,setup:{organization:'',hasRelayToken:false}},new Set()).startDisabled,true);
});
test('실황이 켜져도 계정 설정은 열리고 기관·릴레이 저장은 잠긴다',()=>{
  const result=controls({...state,engine:{state:'ready',message:'실행'}},new Set());
  assert.equal(result.accountDisabled,false);assert.equal(result.settingsDisabled,true);assert.equal(result.reopenDisabled,false);assert.equal(result.stopDisabled,false);
});
test('기록 시작 대기는 중지할 수 있고 계정 작업의 잠금과 분리된다',()=>{
  const result=controls(state,new Set(['engine-start','account']));assert.equal(result.stopDisabled,false);assert.equal(result.startDisabled,true);assert.equal(result.accountDisabled,true);
});
test('Windows 설치판 시험 경로는 명시 CI 스위치와 임시 하위 폴더에만 격리한다',()=>{
  const paths=applicationPaths({platform:'win32',appData:'C:\\Users\\test\\AppData\\Roaming',tempDir:'D:\\runner\\temp',ci:'true',smokeSwitch:true,smokeDir:'D:\\runner\\temp\\gukgam-test'});
  assert.equal(paths.userData,'D:\\runner\\temp\\gukgam-test');assert.equal(paths.sessionData,'D:\\runner\\temp\\gukgam-test\\browser-cache');assert.equal(paths.ciSmoke,true);
  for(const overrides of [{ci:'false'},{platform:'darwin'},{smokeSwitch:false},{smokeDir:'D:\\other-profile'},{smokeDir:'D:\\runner\\temp'},{smokeDir:'relative'}])assert.throws(()=>applicationPaths({platform:'win32',appData:'C:\\AppData',tempDir:'D:\\runner\\temp',ci:'true',smokeSwitch:true,smokeDir:'D:\\runner\\temp\\gukgam-test',...overrides}));
  assert.deepEqual(applicationPaths({platform:'win32',appData:'C:\\AppData',tempDir:'D:\\temp',smokeSwitch:false}),{userData:'C:\\AppData\\GukgamLiveDesktop',sessionData:'C:\\AppData\\GukgamLiveDesktop\\browser-cache',ciSmoke:false});
  assert.equal(isolatedWebPreferences.sandbox,true);assert.equal(isolatedWebPreferences.contextIsolation,true);assert.equal(isolatedWebPreferences.nodeIntegration,false);assert.equal('preload' in isolatedWebPreferences,false);
});
test('사이트 생성에는 직접 선택한 요금제와 서로 다른 패스코드·동의가 필요하다',()=>{
  const valid={teamId:'mock-team',projectName:'gukgam-relay-mock',productId:'mock-product',planId:'mock-plan',metadata:{region:'mock'},operatorPasscode:'MOCK 운영자 비밀 암호를 정함',viewerPasscode:'MOCK 열람자 비밀 암호를 정함',consent:true};
  assert.equal(validateRelayForm(valid),valid);
  for(const overrides of [{planId:''},{projectName:'기관 사이트'},{operatorPasscode:'short'},{viewerPasscode:'short'},{viewerPasscode:valid.operatorPasscode},{consent:false}])assert.throws(()=>validateRelayForm({...valid,...overrides}));
});
