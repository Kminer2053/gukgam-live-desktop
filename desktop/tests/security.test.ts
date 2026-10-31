import test from 'node:test';
import assert from 'node:assert/strict';
import {join} from 'node:path';
import {buildLaunch} from '../src/main/engine';
import {isEngineUrl,isEngineDownloadUrl,parseBackendEvent,redactLog,relayUrl,validateSetup} from '../src/main/security';
test('기관 설정은 정해진 네 필드와 공개 HTTPS 주소만 받는다',()=>{
  assert.deepEqual(validateSetup({organization:' 한국도로공사 ',relayUrl:'https://audit.example.com/'}),{organization:'한국도로공사',relayUrl:'https://audit.example.com'});
  for(const raw of [{organization:'가',dataDir:'/다른기록'},{organization:''},{organization:'가',relayUrl:'http://audit.example.com'},{organization:'가',relayUrl:'https://127.0.0.1'},{organization:'가',relayUrl:'https://audit.example.com?token=secret'},{organization:'가',model:'bad\nmodel'}])assert.throws(()=>validateSetup(raw));
  for(const url of ['javascript:alert(1)','file:///tmp/x','https://user:pass@audit.example.com','https://audit.example.com/path','https://audit.local','https://[::1]'])assert.throws(()=>relayUrl(url));
});
test('기록 창은 정확한 루프백 원점만 허용한다',()=>{
  assert.equal(isEngineUrl('http://127.0.0.1:8765/sessions/a'),true);
  for(const url of ['https://127.0.0.1:8765','http://localhost:8765','http://127.0.0.1:8766','http://127.0.0.1:8765.evil.example','http://u:p@127.0.0.1:8765','javascript:alert(1)'])assert.equal(isEngineUrl(url),false);
});
test('단독 HTML 내보내기와 앱 원점 Blob만 다운로드한다',()=>{
  assert.equal(isEngineDownloadUrl('http://127.0.0.1:8765/api/sessions/mock/offline.html'),true);
  assert.equal(isEngineDownloadUrl('blob:http://127.0.0.1:8765/mock-id'),true);
  for(const url of ['blob:https://outside.example.com/id','file:///tmp/x','https://outside.example.com/file'])assert.equal(isEngineDownloadUrl(url),false);
});
test('엔진 프로토콜은 원점과 요청 크기·토큰 수를 검증한다',()=>{
  assert.equal(parseBackendEvent({type:'ready',url:'http://127.0.0.1:8765'}).type,'ready');
  assert.equal(parseBackendEvent({type:'llm.cancel',id:'mine-1'}).type,'llm.cancel');
  assert.deepEqual(parseBackendEvent({type:'account.status.request',id:'status-1',secret:'discard'}),{type:'account.status.request',id:'status-1'});
  assert.throws(()=>parseBackendEvent({type:'account.status.request',id:'bad/id'}));
  for(const raw of [{type:'ready',url:'https://outside.example.com'},{type:'llm.request',id:'bad/id',system:'',user:'',max_tokens:10},{type:'llm.request',id:'valid',system:'',user:'',max_tokens:0},{type:'llm.request',id:'valid',system:'',user:'x'.repeat(500001),max_tokens:10}])assert.throws(()=>parseBackendEvent(raw));
});
test('진단에서 계정 토큰과 주소를 가리고 출력 길이를 제한한다',()=>{
  const result=redactLog('Bearer secret-access relayToken=secret-relay access_token="secret-json" https://example.com/?token=secret-url');
  for(const secret of ['secret-access','secret-relay','secret-json','secret-url'])assert.equal(result.includes(secret),false);
  assert.equal(redactLog('x'.repeat(5000)).length,2000);
});
test('개발 엔진은 명시 Python과 새 저장경로를 사용하고 이전 앱 환경을 제거한다',()=>{
  const options={userData:'/own/profile',resourcesPath:'/bundle',projectRoot:'/new/repo',packaged:false,env:{PATH:'/bin',GUKGAM_DESKTOP_PYTHON:'/python/explicit',GUKGAM_DATA_DIR:'/old/records',GUKGAM_API_KEY:'other-secret',PYTHONPATH:'/old/repo'}};
  const launch=buildLaunch(options,'/own/config.json');
  assert.equal(launch.executable,'/python/explicit');assert.equal(launch.cwd,'/new/repo');
  assert.deepEqual(launch.args,['-m','gukgam.desktop_runtime','--config','/own/config.json']);
  assert.equal(launch.env.GUKGAM_DATA_DIR,join(options.userData,'records'));assert.equal(launch.env.GUKGAM_API_KEY,undefined);assert.equal(launch.env.PYTHONPATH,undefined);
  assert.throws(()=>buildLaunch({...options,env:{}},'/config'));
  const packaged=buildLaunch({...options,packaged:true},'/config');assert.match(packaged.executable,/engine[/\\]gukgam-engine\.exe$/);assert.deepEqual(packaged.args,['--config','/config']);
});
