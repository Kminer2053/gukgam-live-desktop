import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {SetupStore} from '../src/storage/settings';
import type {ChatGPTVault} from '../src/auth/chatgpt';
test('계정 보안 저장소 오류는 로컬 기관 설정과 기록 엔진 준비를 막지 않는다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-vault-recovery-'));
  const vault:ChatGPTVault={get:async()=>{throw Error('보안 저장소 잠김');},set:async()=>{throw Error('보안 저장소 잠김');},delete:async()=>{}};
  try {
    const store=new SetupStore(dir,vault);
    const saved=await store.save({organization:'모의 기관',relayUrl:'https://mock.example.com'});
    assert.equal(saved.organization,'모의 기관');assert.match(saved.secureStorageWarning!,/회의 기록과 실황 수집/);
    assert.deepEqual(await store.engineConfig(),{organization:'모의 기관'});
    assert.equal((await readFile(join(dir,'setup.json'),'utf8')).includes('secureStorageWarning'),false);
    await assert.rejects(store.save({organization:'모의 기관',relayToken:'MOCK_SECRET'}),/보안 저장소/);
    assert.equal((await store.read()).organization,'모의 기관');
  } finally {await rm(dir,{recursive:true,force:true});}
});

test('주소만 저장되고 연결 토큰이 없으면 릴레이를 제외해 로컬 엔진은 시작할 수 있다',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'gukgam-relay-missing-token-'));
 try{const vault={get:async()=>undefined,set:async()=>{},delete:async()=>{}};const store=new SetupStore(dir,vault);await store.save({organization:'시험 기관',relayUrl:'https://relay.example.invalid'});
 assert.equal((await store.read()).relayUrl,'https://relay.example.invalid');assert.deepEqual(await store.engineConfig(),{organization:'시험 기관'});
 }finally{await rm(dir,{recursive:true,force:true});}
});
