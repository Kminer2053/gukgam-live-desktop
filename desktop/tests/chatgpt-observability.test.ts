import test from 'node:test';
import assert from 'node:assert/strict';
import {ChatGPTService,type ChatGPTVault} from '../src/auth/chatgpt';
import type {RequestDiagnostic} from '../src/main/diagnostics';

class MemoryVault implements ChatGPTVault {
  data=new Map<string,unknown>();
  async get<T>(key:string):Promise<T|undefined>{return this.data.get(key) as T|undefined;}
  async set(key:string,value:unknown){this.data.set(key,value);}
  async delete(key:string){this.data.delete(key);}
}
async function vault(){
  const result=new MemoryVault();await result.set('chatgpt',{clientId:'mock-client',subject:'mock-user',email:'private@example.invalid',
    accessToken:'SECRET_ACCESS',idToken:'SECRET_ID',scopes:['chatgpt.tokens.use.direct'],expiresAt:Date.now()+3600000,verified:true,models:['gpt-test']});return result;
}
const response=()=>new Response('data: {"type":"response.output_text.delta","delta":"SECRET_RESPONSE"}\n\ndata: {"type":"response.completed"}\n\n');

test('실제 Responses 호출마다 준비·연결·완료 메타만 통지하고 chunk는 통지하지 않는다',async()=>{
  const rows:RequestDiagnostic[]=[];let calls=0;
  const service=new ChatGPTService(await vault(),'/unused',async()=>{}, {fetch:async()=>{calls++;return response();},onRequestStatus:status=>{rows.push(status);}});
  assert.equal(await service.request('SECRET_PROMPT','SECRET_INPUT',undefined,{requestId:'request-1'}),'SECRET_RESPONSE');
  assert.equal(calls,1);assert.deepEqual(rows.map(row=>row.state),['preparing','connecting','complete']);
  assert.equal(rows.at(-1)!.receivedChars,'SECRET_RESPONSE'.length);
  assert.ok(rows.at(-1)!.updatedAt>=rows[0].startedAt);
  assert.equal(JSON.stringify(rows).includes('SECRET'),false);assert.equal(JSON.stringify(rows).includes('private@'),false);
  assert.ok(rows.every(row=>!('message' in row)));service.cancel();
});

test('진단 callback의 동기·비동기 실패가 AI 결과를 바꾸지 않는다',async()=>{
  for(const callback of [()=>{throw Error('SECRET_LOG_FAILURE');},async()=>{throw Error('SECRET_LOG_FAILURE');}]){
    const service=new ChatGPTService(await vault(),'/unused',async()=>{}, {fetch:async()=>response(),onRequestStatus:callback});
    assert.equal(await service.request('SECRET_PROMPT','SECRET_INPUT'),'SECRET_RESPONSE');service.cancel();
  }
});

test('실패는 오류 본문 없이 terminal 한 건을 통지한다',async()=>{
  const rows:RequestDiagnostic[]=[];
  const service=new ChatGPTService(await vault(),'/unused',async()=>{}, {fetch:async()=>Response.json({error:{code:'SECRET_ERROR'}},{status:500}),onRequestStatus:status=>{rows.push(status);}});
  await assert.rejects(service.request('SECRET_PROMPT','SECRET_INPUT'));
  assert.deepEqual(rows.map(row=>row.state),['preparing','connecting','failed']);
  assert.equal(JSON.stringify(rows).includes('SECRET'),false);service.cancel();
});

test('사용자 취소는 늦은 실패와 중복되지 않는 terminal 한 건으로 남긴다',async()=>{
  const rows:RequestDiagnostic[]=[];let connected!:()=>void;
  const started=new Promise<void>(done=>{connected=done;});
  const service=new ChatGPTService(await vault(),'/unused',async()=>{}, {
    fetch:async(_url,options)=>new Promise<Response>((_done,fail)=>{options?.signal?.addEventListener('abort',()=>fail(Error('SECRET_NETWORK_ABORT')),{once:true});}),
    onRequestStatus:status=>{rows.push(status);if(status.state==='connecting')connected();},
  });
  const request=service.request('SECRET_PROMPT','SECRET_INPUT',undefined,{requestId:'request-cancel'});
  await started;service.cancelRequest('request-cancel');await assert.rejects(request);
  assert.deepEqual(rows.map(row=>row.state),['preparing','connecting','cancelled']);
  assert.equal(JSON.stringify(rows).includes('SECRET'),false);service.cancel();
});
