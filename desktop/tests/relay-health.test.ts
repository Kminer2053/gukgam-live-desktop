import test from 'node:test';
import assert from 'node:assert/strict';
import {checkRelayHealth} from '../src/main/relay-health';
const response=(body:unknown)=>new Response(JSON.stringify(body),{headers:{'content-type':'application/json'}});
test('릴레이 공개 상태 확인은 인증정보 없이 제한된 GET 한 번만 보낸다',async()=>{
  let calls=0;
  const fetcher:typeof fetch=async(url,options)=>{calls++;assert.equal(url,'https://audit.example.com/api/health');assert.equal(options?.method,'GET');assert.equal(options?.redirect,'error');assert.equal(options?.credentials,'omit');assert.equal((options?.headers as Record<string,string>).Authorization,undefined);return response({ok:true,cloud:true,read_mode:'source',agent:{online:true,read_mode:'source'}});};
  const result=await checkRelayHealth('https://audit.example.com',fetcher);assert.equal(calls,1);assert.equal(result.sourceReady,true);assert.match(result.message,/현재 PC/);
});
test('미러와 오프라인 응답은 현재 PC 연결 성공으로 표시하지 않는다',async()=>{
  const result=await checkRelayHealth('https://audit.example.com',async()=>response({ok:true,cloud:true,read_mode:'mirror',agent:{online:false,read_mode:'mirror'}}));
  assert.equal(result.sourceReady,false);assert.match(result.message,/아직 준비되지/);
});
test('리디렉션·HTML·큰 JSON·알 수 없는 상태 형식은 실패한다',async()=>{
  for(const fetcher of [async()=>{throw Error('redirect');},async()=>new Response('<html>login</html>',{headers:{'content-type':'text/html'}}),async()=>response({data:'x'.repeat(70000)}),async()=>response({ok:true,cloud:false}),async()=>response({ok:true,cloud:true,read_mode:'source',agent:{online:'true',read_mode:'source'}})])await assert.rejects(checkRelayHealth('https://audit.example.com',fetcher),/확인하지 못/);
});
test('인증필수 릴레이의 401은 공개 상태 확인 성공으로 표시하지 않는다',async()=>{
  await assert.rejects(checkRelayHealth('https://audit.example.com',async()=>new Response('',{status:401})),/웹 로그인/);
});
