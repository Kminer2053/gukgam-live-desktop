import test from 'node:test';
import assert from 'node:assert/strict';
import {createManagedRelay} from '../src/relay';

test('릴레이 factory는 로컬 HTTP 대신 매 확인마다 소유 엔진 getter만 사용한다',async()=>{
  let ready=true,calls=0,fetches=0,value:unknown='a'.repeat(32),failure=false;
  const originalFetch=globalThis.fetch;
  globalThis.fetch=async()=>{fetches++;throw Error('이 시험은 네트워크를 사용하지 않습니다.');};
  try{
    const relay=createManagedRelay({userData:'/unused',templateDir:'/unused',resourcesPath:'/unused',projectRoot:'/unused',packaged:true,
      settings:{} as any,vault:{} as any,openExternal:async()=>{},engineIsReady:()=>ready,
      readRelayConnectionId:async()=>{calls++;if(failure)throw Error('합성 조회 실패');return value as string;}});
    const read=(relay as any).options.expectedConnection as ()=>Promise<string|undefined>;
    assert.equal(await read(),'a'.repeat(32));value='b'.repeat(32);assert.equal(await read(),value);
    ready=false;assert.equal(await read(),undefined);assert.equal(calls,2);
    ready=true;value='malformed';assert.equal(await read(),undefined);
    failure=true;assert.equal(await read(),undefined);assert.equal(fetches,0);
  }finally{globalThis.fetch=originalFetch;}
});
