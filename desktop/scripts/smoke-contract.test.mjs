import test from 'node:test';
import assert from 'node:assert/strict';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {archivePayload,argumentsMap,assertInside,assertJuliaPrepareIntent,assertUnavailableJulia,assertWindowsCi,ORIGIN,parseMissingJuliaWorker} from './smoke-contract.mjs';

test('로컬 macOS와 일반 Windows에서는 실포트 시험을 시작하지 않는다',()=>{
  assert.throws(()=>assertWindowsCi('darwin',{GITHUB_ACTIONS:'true',RUNNER_OS:'Windows',RUNNER_TEMP:tmpdir()}));
  assert.throws(()=>assertWindowsCi('win32',{RUNNER_OS:'Windows',RUNNER_TEMP:tmpdir()}));
  assertWindowsCi('win32',{GITHUB_ACTIONS:'true',RUNNER_OS:'Windows',RUNNER_TEMP:tmpdir()});
});
test('시험 정리는 소유한 임시 하위 폴더만 허용한다',()=>{
  assertInside(tmpdir(),join(tmpdir(),'gukgam-smoke-owned'));
  assert.throws(()=>assertInside(tmpdir(),tmpdir()));
  assert.throws(()=>assertInside(join(tmpdir(),'owned'),join(tmpdir(),'other')));
});
test('중복·홀수 명령 인자는 거절한다',()=>{
  assert.deepEqual(argumentsMap(['--app','installed.exe']),{app:'installed.exe'});
  assert.throws(()=>argumentsMap(['--app']));assert.throws(()=>argumentsMap(['--app','a','--app','b']));
});
test('실제 HTML 압축 자료에서 원문과 사람 수정을 대조한다',()=>{
  const data={originals:[{id:1,original_text:'원문',text:'사람 수정',speaker_source:'manual'}]};
  assert.deepEqual(archivePayload('<script id="offline-data" type="application/octet-stream" data-compression="gzip">H4sIAAAAAAACCqtWyi/KTM/MS8wpVrKKrlbKTFGyMtSBC8aXpFaUKFkpvZk94fWaHUo6SjB+05rX83oU3nTMeLNgKlC4uCA1MTu1KL44v7QoORWoIDcxrzQxR6k2thYAE1aT0mIAAAA=</script>'),data);
  assert.throws(()=>archivePayload('<html></html>'));
});
test('모델 없는 설치판을 실제 CPU 준비 성공으로 보고하지 않는다',()=>{
  const missing={selected_provider:'julia',model:{model_present:false,available:false,runtime_present:true},
    worker:{available:false},preparing:false,cpu_threads:1,probability_notice:'보정된 정확도가 아닙니다'};
  assertUnavailableJulia(missing);
  for(const changed of [
    {...missing,model:{...missing.model,model_present:true}},
    {...missing,worker:{available:true}}, {...missing,preparing:true},
    {...missing,model:{...missing.model,runtime_present:false}},
  ])assert.throws(()=>assertUnavailableJulia(changed));
});
test('모델 준비 UI는 고정 루프백 POST와 명시 확인만 허용한다',()=>{
  const request={url:ORIGIN+'/api/settings/role-decision/julia/prepare',method:'POST',body:{confirm:true}};
  assertJuliaPrepareIntent(request);
  for(const changed of [{...request,method:'GET'},{...request,body:{}},
    {...request,body:{confirm:'true'}},{...request,body:{confirm:true,path:'arbitrary'}},
    {...request,url:'https://example.invalid/api/settings/role-decision/julia/prepare'},
  ])assert.throws(()=>assertJuliaPrepareIntent(changed));
});
test('모델누락 작업자는 생성 결과 없이 오류 JSON 한 줄만 반환한다',()=>{
  const event={event:'fatal',status:'invalid_model',error:'Julia CPU 모델을 준비하지 못했습니다'};
  assert.deepEqual(parseMissingJuliaWorker(JSON.stringify(event)+'\n'),event);
  assert.throws(()=>parseMissingJuliaWorker('진단 출력\n'+JSON.stringify(event)));
  assert.throws(()=>parseMissingJuliaWorker(JSON.stringify({...event,event:'ready'})));
  assert.throws(()=>parseMissingJuliaWorker(JSON.stringify({...event,answers:{role_s1:{}}})));
});
