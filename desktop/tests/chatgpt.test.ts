import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {ChatGPTService,type ChatGPTVault} from '../src/auth/chatgpt';
import {oauthProof,stateMatches} from '../src/auth/loopback';
class MemoryVault implements ChatGPTVault {
  data=new Map<string,unknown>();
  async get<T>(key:string):Promise<T|undefined> {return this.data.get(key) as T|undefined;}
  async set(key:string,value:unknown) {this.data.set(key,value);}
  async delete(key:string) {this.data.delete(key);}
}
test('PKCE 확인값은 요청마다 달라지고 state는 정확히 일치해야 한다',()=>{
  const first=oauthProof(),second=oauthProof();assert.notEqual(first.state,second.state);assert.notEqual(first.verifier,second.verifier);assert.equal(stateMatches(first.state,first.state),true);assert.equal(stateMatches(first.state+'x',first.state),false);assert.equal(stateMatches(null,first.state),false);
});
test('로그인은 자동 AI 요청 없이 연결 정보만 보관하고 토큰을 화면에 반환하지 않는다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-auth-test-'));const vault=new MemoryVault();let nonce='',calls=0;
  try {
    const service=new ChatGPTService(vault,dir,async value=>{const url=new URL(value);assert.equal(url.hostname,'auth.openai.com');assert.equal(url.searchParams.get('agent_name_hint'),'국감 라이브 데스크톱');nonce=url.searchParams.get('nonce')!;},{
      callback:async()=>({redirectUri:'http://127.0.0.1:17777/auth/callback',result:Promise.resolve(new URLSearchParams({code:'MOCK_CODE',client_id:'mock-client'})),close:()=>{}}),
      fetch:async url=>{calls++;assert.equal(String(url),'https://auth.openai.com/api/accounts/oauth/token');return Response.json({access_token:'MOCK_ACCESS_SECRET',refresh_token:'MOCK_REFRESH_SECRET',id_token:'MOCK_ID_SECRET',scope:'chatgpt.tokens.use.direct',expires_in:3600});},
      verifyIdentity:async()=>({sub:'mock-user',nonce,email:'mock@example.invalid'})
    });
    const status=await service.connect();assert.equal(calls,1);assert.equal(status.connected,true);assert.equal(status.verified,false);assert.equal(JSON.stringify(status).includes('SECRET'),false);assert.equal(vault.data.has('chatgpt'),true);service.cancel();
  } finally {await rm(dir,{recursive:true,force:true});}
});
test('엔진의 요청 토큰 한도를 기존 Responses 호출에 전달한다',async()=>{
  const vault=new MemoryVault();await vault.set('chatgpt',{clientId:'mock-client',subject:'mock-user',accessToken:'MOCK_ACCESS',idToken:'MOCK_ID',scopes:['chatgpt.tokens.use.direct'],expiresAt:Date.now()+3600000,verified:true,models:['mock-model']});
  const service=new ChatGPTService(vault,'/unused',async()=>{}, {fetch:async(_url,options)=>{const body=JSON.parse(String(options?.body));assert.equal(body.max_output_tokens,123);assert.equal(body.store,false);return new Response('data: {"type":"response.output_text.delta","delta":"모의 결과"}\n\ndata: {"type":"response.completed"}\n\n');}});
  assert.equal(await service.request('모의 지침','모의 입력',undefined,{requestId:'mock-1',maxTokens:123}),'모의 결과');service.cancel();
});
