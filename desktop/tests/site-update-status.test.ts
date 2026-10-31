import test from 'node:test';
import assert from 'node:assert/strict';
import {siteUpdateStatus,siteUpdateVersion} from '../src/relay/contracts';
import {diagnosticEntry} from '../src/main/diagnostics';
import {vercelCommandFailure,vercelFailureMetadata} from '../src/relay/vercel-cli';

const failure=()=>({state:'unknown',stage:'deploying',deployAttempted:true,targetVersion:'0.1.0-preview.14',
 knownCode:'VERCEL_EXIT_NONZERO',exitCode:1,durationMs:10});

test('공개 journal·진단은 고정 상태·코드·유한 수치만 남기고 원격 출력·식별자·비밀을 저장하지 않는다',()=>{
 const raw={...failure(),message:'PRIVATE_MESSAGE',cause:{message:'PRIVATE_CAUSE'},stdout:'PRIVATE_STDOUT',stderr:'PRIVATE_STDERR',
  args:['PRIVATE_ARG'],url:'https://PRIVATE_URL.invalid',headers:{Authorization:'PRIVATE_TOKEN'},env:{PRIVATE_ENV:'value'},
  projectId:'PRIVATE_PROJECT',teamId:'PRIVATE_TEAM',path:'PRIVATE_PATH',metadata:{text:'PRIVATE_BODY'}};
 assert.deepEqual(siteUpdateStatus(raw),failure());
 const row=diagnosticEntry('relay.site-update',{siteUpdate:raw},1)!;
 assert.deepEqual(row,{time:'1970-01-01T00:00:00.001Z',event:'relay.site-update',...failure()});
 assert.equal(JSON.stringify(row).includes('PRIVATE_'),false);
});

test('손상된 optional journal과 비정수·범위 밖 수치는 업데이트 완료로 승격하지 않는다',()=>{
 for(const raw of [null,[],true,{},
  {...failure(),httpStatus:'403'},{...failure(),httpStatus:99},{...failure(),httpStatus:600},
  {...failure(),exitCode:-1},{...failure(),exitCode:256},{...failure(),exitCode:NaN},
  {...failure(),durationMs:Infinity},{...failure(),durationMs:true},{...failure(),knownCode:'PRIVATE_CODE'},
  {...failure(),stage:'PRIVATE_STAGE'},{...failure(),state:'PRIVATE_STATE'},
  {...failure(),targetVersion:'https://PRIVATE_VERSION.invalid'},{...failure(),deployAttempted:'true'},
  {...failure(),state:'succeeded',stage:'completed',version:'0.1.0-preview.13'},
  {...failure(),state:'cancelled'},
  {...failure(),state:'failed'},{...failure(),stage:'preparing'},
  {...failure(),state:'updating'},{...failure(),state:'succeeded',stage:'completed',targetVersion:'0.1.0-preview.14',version:'0.1.0-preview.14'},
 ])assert.deepEqual(siteUpdateStatus(raw),{state:'unverified',stage:'unverified'});
});

test('완료 버전은 엄격한 제품 버전이면서 실제 시도한 버전과 일치해야 한다',()=>{
 for(const version of ['0.1.0-preview.14','1.2.3'])assert.equal(siteUpdateVersion(version),version);
 for(const version of ['01.2.3','1.2','1.2.3-preview.01','1.2.3-PRIVATE','1.2.3\n',true,14])assert.equal(siteUpdateVersion(version),undefined);
 const value={state:'succeeded',stage:'completed',deployAttempted:true,targetVersion:'0.1.0-preview.14',version:'0.1.0-preview.14',durationMs:10};
 assert.deepEqual(siteUpdateStatus(value),value);
 for(const change of [{deployAttempted:false},{stage:'saving'},{targetVersion:'0.1.0-preview.13'},{version:undefined}])
  assert.equal(siteUpdateStatus({...value,...change}).state,'unverified');
});

test('명령 실패는 종료 수치만 공개하고 임의로 흉내 낸 코드·원인은 채택하지 않는다',()=>{
 const error=vercelCommandFailure(17);
 assert.deepEqual(vercelFailureMetadata(error),{knownCode:'VERCEL_EXIT_NONZERO',exitCode:17});
 assert.deepEqual(vercelFailureMetadata(Object.assign(Error('PRIVATE_ERROR'),{code:'VERCEL_TIMEOUT',exitCode:5})),{});
 assert.deepEqual(vercelFailureMetadata(vercelCommandFailure(NaN)),{knownCode:'VERCEL_EXIT_NONZERO'});
});
