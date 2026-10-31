import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {validateSetup} from '../src/main/security';
import {SetupStore} from '../src/storage/settings';
import {SecureVault} from '../src/storage/vault';

const period={relayEnabled:true,relayStartDate:'2026-11-01',relayEndDate:'2026-11-02'};
test('최초 설정은 무기한 연결을 만들지 않으며 날짜와 사용 여부를 엄격히 확인한다',()=>{
  assert.deepEqual(validateSetup({organization:'합성기관'}),{organization:'합성기관'});
  assert.deepEqual(validateSetup({organization:'합성기관',...period}),{organization:'합성기관',...period});
  for(const input of [{relayEnabled:true},{...period,relayEnabled:'true'},{...period,relayStartDate:'2026-02-30'},
    {...period,relayStartDate:'2026-11-03'},{...period,relayEndDate:''},{...period,relayStartDate:'0000-11-01'}]) {
    assert.throws(()=>validateSetup({organization:'합성기관',...input}));
  }
  assert.equal(validateSetup({organization:'합성기관',...period,relayEnabled:false}).relayEnabled,false);
});

test('기간은 공개 설정에 저장하고 키는 분리하며 엔진으로 같은 사용 여부를 전달한다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-relay-period-'));
  const protector={isEncryptionAvailable:()=>true,encryptString:(value:string)=>Buffer.from(value),decryptString:(value:Buffer)=>value.toString()};
  try {
    const store=new SetupStore(dir,new SecureVault(join(dir,'vault'),protector));
    await store.save({organization:'합성기관',relayUrl:'https://relay.example.com',relayToken:'SYNTHETIC_KEY_ONLY',...period,applyRelayPolicy:true});
    const stored=JSON.parse(await readFile(join(dir,'setup.json'),'utf8'));
    assert.equal(stored.relayEnabled,true);assert.equal(stored.relayEndDate,period.relayEndDate);
    assert.equal(JSON.stringify(stored).includes('SYNTHETIC_KEY_ONLY'),false);
    const reloaded=new SetupStore(dir,new SecureVault(join(dir,'vault'),protector));
    assert.equal((await reloaded.engineConfig()).relayStartDate,period.relayStartDate);
    await reloaded.save({organization:'합성기관',relayUrl:'https://relay.example.com',...period,relayEnabled:false,applyRelayPolicy:true});
    const next=await reloaded.engineConfig();
    assert.equal(next.relayEnabled,false);assert.equal(next.relayEndDate,period.relayEndDate);
    assert.equal(next.relayToken,'SYNTHETIC_KEY_ONLY');
    await reloaded.save({organization:'다른 합성기관',relayUrl:'https://relay.example.com'});
    assert.equal((await reloaded.read()).relayEnabled,false);
    assert.equal((await reloaded.read()).relayEndDate,period.relayEndDate);
  }finally {await rm(dir,{recursive:true,force:true});}
});

test('명시 중계 저장만 새 내부 판본을 만들고 일반 저장은 판본과 기간을 보존한다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-relay-intent-'));
  const protector={isEncryptionAvailable:()=>true,encryptString:(value:string)=>Buffer.from(value),decryptString:(value:Buffer)=>value.toString()};
  try {
    const store=new SetupStore(dir,new SecureVault(join(dir,'vault'),protector));
    const off={relayEnabled:false,relayStartDate:'',relayEndDate:''};
    await store.save({organization:'합성기관',...off,applyRelayPolicy:true});
    const first=await store.engineConfig();
    assert.match(first.relayPolicyRevision!,/^[0-9a-f-]{36}$/);
    const visible=await store.read();
    assert.equal('relayPolicyRevision' in visible,false);assert.equal('applyRelayPolicy' in visible,false);
    await store.save({organization:'새 합성기관',model:'synthetic-model',...period});
    const generic=await store.engineConfig();
    assert.equal(generic.relayPolicyRevision,first.relayPolicyRevision);
    assert.equal(generic.relayEnabled,false);assert.equal(generic.relayStartDate,'');
    await store.save({organization:'새 합성기관',...off,applyRelayPolicy:true});
    const explicitOff=await store.engineConfig();
    assert.notEqual(explicitOff.relayPolicyRevision,first.relayPolicyRevision);
    assert.equal(explicitOff.relayEnabled,false);
    await store.save({organization:'새 합성기관',...period,applyRelayPolicy:true});
    const explicitOn=await store.engineConfig();
    assert.notEqual(explicitOn.relayPolicyRevision,explicitOff.relayPolicyRevision);
    assert.equal(explicitOn.relayEnabled,true);
    assert.throws(()=>validateSetup({organization:'합성기관',...period,relayPolicyRevision:explicitOn.relayPolicyRevision}));
    assert.throws(()=>validateSetup({organization:'합성기관',applyRelayPolicy:true}));
    const stored=JSON.parse(await readFile(join(dir,'setup.json'),'utf8'));
    assert.equal(stored.applyRelayPolicy,undefined);
  }finally {await rm(dir,{recursive:true,force:true});}
});
