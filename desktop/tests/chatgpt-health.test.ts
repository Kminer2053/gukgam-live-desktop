import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {ChatGPTService,type ChatGPTVault} from '../src/auth/chatgpt';

class Vault implements ChatGPTVault {
  reads=0;writes=0;deletes=0;
  value:any={clientId:'mock-client',subject:'mock-subject',email:'DO_NOT_COPY',accessToken:'DO_NOT_COPY',
    idToken:'mock-id',scopes:['chatgpt.tokens.use.direct'],expiresAt:Date.now()+3600000,verified:false,models:['mock-model']};
  async get<T>():Promise<T|undefined>{this.reads++;return this.value;}
  async set(_key:string,value:unknown){this.writes++;this.value=value;}
  async delete(){this.deletes++;this.value=undefined;}
}
const response=()=>new Response('data: {"type":"response.output_text.delta","delta":"모의 응답"}\n\ndata: {"type":"response.completed"}\n\n');

test('실제 응답 성공·실패·복구를 반영하고 상태 조회는 외부 호출을 추가하지 않는다',async()=>{
  const vault=new Vault();let calls=0,failed=false;
  const service=new ChatGPTService(vault,'/unused',async()=>{}, {fetch:async()=>{calls++;return failed?Response.json({error:{code:'rate_limit_exceeded'}},{status:429}):response();}});
  try {
    assert.deepEqual(await service.connectionHealth(),{state:'unverified'});
    await service.request('모의 지침','모의 입력');assert.deepEqual(await service.connectionHealth(),{state:'verified'});
    failed=true;await assert.rejects(service.request('모의 지침','모의 입력'));assert.deepEqual(await service.connectionHealth(),{state:'error'});
    failed=false;await service.request('모의 지침','모의 입력');assert.deepEqual(await service.connectionHealth(),{state:'verified'});
    assert.equal(calls,3);assert.equal(JSON.stringify(await service.connectionHealth()).includes('DO_NOT_COPY'),false);
  }finally{service.cancel();}
});

test('저장된 시험 성공도 만료·권한 누락·연결 해제 상태를 덮지 않는다',async()=>{
  const vault=new Vault();vault.value.verified=true;
  const service=new ChatGPTService(vault,'/unused',async()=>{}, {fetch:async()=>{throw Error('상태 조회의 외부 호출 금지');}});
  assert.deepEqual(await service.connectionHealth(),{state:'verified'});
  vault.value.expiresAt=Date.now()-1;assert.deepEqual(await service.connectionHealth(),{state:'expired'});
  vault.value.expiresAt=Date.now()+3600000;vault.value.scopes=[];assert.deepEqual(await service.connectionHealth(),{state:'unverified'});
  vault.value=undefined;assert.deepEqual(await service.connectionHealth(),{state:'disconnected'});service.cancel();
});

test('연결 시험 성공 뒤 모델 목록 시험 실패도 이전 성공으로 숨기지 않는다',async()=>{
  const vault=new Vault();let failModels=false;
  const service=new ChatGPTService(vault,'/unused',async()=>{}, {fetch:async url=>String(url).endsWith('/models')?
    (failModels?Response.json({},{status:403}):Response.json({data:[{id:'mock-model'}]})):response()});
  try {
    await service.test();assert.deepEqual(await service.connectionHealth(),{state:'verified'});
    failModels=true;await assert.rejects(service.test());assert.deepEqual(await service.connectionHealth(),{state:'error'});
  }finally{service.cancel();}
});

test('재연결 중과 새 인증 직후에는 이전 계정 시험 성공을 재사용하지 않는다',async()=>{
  const vault=new Vault();vault.value.verified=true;let nonce='',finish!:(value:URLSearchParams)=>void;
  const callback=new Promise<URLSearchParams>(done=>{finish=done;});
  const dir=await mkdtemp(join(tmpdir(),'gukgam-health-mock-'));
  const service=new ChatGPTService(vault,dir,async value=>{nonce=new URL(value).searchParams.get('nonce')!;},{
    callback:async()=>({redirectUri:'http://127.0.0.1:17777/auth/callback',result:callback,close:()=>{}}),
    verifyIdentity:async()=>({sub:'mock-subject',nonce}),
    fetch:async()=>Response.json({id_token:'mock-id',access_token:'mock-access',scope:'chatgpt.tokens.use.direct',expires_in:3600})});
  try {
    const connecting=service.connect();assert.deepEqual(await service.connectionHealth(),{state:'checking'});
    finish(new URLSearchParams({code:'mock-code',client_id:'mock-client'}));await connecting;
    assert.deepEqual(await service.connectionHealth(),{state:'unverified'});
  }finally{service.cancel();await rm(dir,{recursive:true,force:true});}
});

test('기존 계정이 남아도 재연결 실패는 정상 연결로 표시하지 않는다',async()=>{
  const vault=new Vault();vault.value.verified=true;
  const service=new ChatGPTService(vault,'/unused',async()=>{}, {
    callback:async()=>{throw Error('모의 로그인 오류');},fetch:async()=>{throw Error('외부 호출 금지');}});
  try {
    await assert.rejects(service.connect());
    assert.deepEqual(await service.connectionHealth(),{state:'error'});
    assert.equal(vault.value.verified,true);
  }finally{service.cancel();}
});

test('검증된 계정의 만료는 갱신 수단이 있으면 중립 상태이며 조회만으로 갱신·모델·AI·저장을 하지 않는다',async()=>{
  const vault=new Vault();Object.assign(vault.value,{verified:true,expiresAt:Date.now()-1000,refreshToken:'DO_NOT_COPY_REFRESH'});
  const before=structuredClone(vault.value);let externalCalls=0;
  const service=new ChatGPTService(vault,'/unused',async()=>{externalCalls++;},{fetch:async()=>{externalCalls++;throw Error('상태 조회의 외부 호출 금지');}});
  try {
    for(let i=0;i<3;i++)assert.deepEqual(await service.connectionHealth(),{state:'refreshable'});
    assert.deepEqual(vault.value,before);assert.equal(vault.reads,3);assert.equal(vault.writes,0);assert.equal(vault.deletes,0);assert.equal(externalCalls,0);
    assert.equal(JSON.stringify(await service.connectionHealth()).includes('DO_NOT_COPY'),false);
  }finally{service.cancel();}
});

test('미검증·유효하지 않은 만료·사용할 수 없는 갱신 수단은 refreshable로 표시하지 않는다',async()=>{
  const vault=new Vault();let externalCalls=0;
  const service=new ChatGPTService(vault,'/unused',async()=>{externalCalls++;},{fetch:async()=>{externalCalls++;throw Error('상태 조회의 외부 호출 금지');}});
  try {
    const base={...vault.value,verified:true,expiresAt:Date.now()-1000,refreshToken:'mock-refresh'};
    for(const expiresAt of [Number.NaN,Number.POSITIVE_INFINITY,Number.NEGATIVE_INFINITY,undefined,null,'0']) {
      vault.value={...base,expiresAt};assert.deepEqual(await service.connectionHealth(),{state:'expired'});
    }
    for(const refreshToken of [undefined,null,'',' ','\n','mock token','mock\u007ftoken',123,{},false]) {
      vault.value={...base,refreshToken};assert.deepEqual(await service.connectionHealth(),{state:'expired'});
    }
    for(const verified of [false,undefined,'true',1]) {
      vault.value={...base,verified};assert.deepEqual(await service.connectionHealth(),{state:'expired'});
    }
    for(const scopes of [[],undefined,null,{},'chatgpt.tokens.use.direct']) {
      vault.value={...base,scopes};assert.deepEqual(await service.connectionHealth(),{state:'unverified'});
    }
    assert.equal(vault.writes,0);assert.equal(vault.deletes,0);assert.equal(externalCalls,0);
  }finally{service.cancel();}
});

test('실제 요청 실패는 갱신 가능한 만료나 잘못된 만료값보다 우선 경고한다',async()=>{
  const vault=new Vault();Object.assign(vault.value,{verified:true,refreshToken:'mock-refresh'});let calls=0;
  const service=new ChatGPTService(vault,'/unused',async()=>{},{fetch:async()=>{calls++;return Response.json({error:{code:'rate_limit_exceeded'}},{status:429});}});
  try {
    await assert.rejects(service.request('모의 지침','모의 입력'));assert.equal(calls,1);
    for(const expiresAt of [Date.now()-1000,Number.NaN]) {
      vault.value.expiresAt=expiresAt;assert.deepEqual(await service.connectionHealth(),{state:'error'});
    }
    assert.equal(calls,1,'상태 재조회는 요청이나 갱신을 반복하지 않음');
  }finally{service.cancel();}
});

test('다음 명시적 요청의 갱신 실패는 경고하고 이후 실제 응답 성공 뒤에만 verified로 복구한다',async()=>{
  const vault=new Vault();Object.assign(vault.value,{verified:true,expiresAt:Date.now()-1000,refreshToken:'mock-refresh'});
  let refreshFailed=true;const calls:string[]=[];
  const service=new ChatGPTService(vault,'/unused',async()=>{},{fetch:async url=>{
    if(String(url).endsWith('/oauth/token')){calls.push('refresh');return refreshFailed?Response.json({error:'temporarily_unavailable'},{status:503}):Response.json({access_token:'mock-renewed',expires_in:3600});}
    assert.equal(String(url).endsWith('/responses'),true);calls.push('response');return response();
  }});
  try {
    assert.deepEqual(await service.connectionHealth(),{state:'refreshable'});assert.deepEqual(calls,[]);
    await assert.rejects(service.request('모의 지침','모의 입력'));assert.deepEqual(await service.connectionHealth(),{state:'error'});assert.deepEqual(calls,['refresh']);
    refreshFailed=false;await service.request('모의 지침','모의 입력');
    assert.deepEqual(await service.connectionHealth(),{state:'verified'});assert.deepEqual(calls,['refresh','refresh','response']);
  }finally{service.cancel();}
});

test('실제 응답을 받은 같은 계정도 만료 뒤에는 새 갱신 성공이 아니라 refreshable로만 표시한다',async()=>{
  const vault=new Vault();vault.value.refreshToken='mock-refresh';let calls=0;
  const service=new ChatGPTService(vault,'/unused',async()=>{},{fetch:async()=>{calls++;return response();}});
  try {
    await service.request('모의 지침','모의 입력');assert.equal(vault.value.verified,false);
    vault.value.expiresAt=Date.now()-1000;assert.deepEqual(await service.connectionHealth(),{state:'refreshable'});assert.equal(calls,1);
  }finally{service.cancel();}
});

test('보안 저장소를 읽지 못한 상태는 갱신 가능으로 추정하지 않는다',async()=>{
  const vault:ChatGPTVault={get:async()=>{throw Error('모의 보안 저장소 오류');},set:async()=>{assert.fail('조회에서 저장 금지');},delete:async()=>{assert.fail('조회에서 삭제 금지');}};
  const service=new ChatGPTService(vault,'/unused',async()=>{assert.fail('조회에서 브라우저 열기 금지');},{fetch:async()=>{assert.fail('조회에서 외부 호출 금지');}});
  try {assert.deepEqual(await service.connectionHealth(),{state:'unavailable'});}finally{service.cancel();}
});
