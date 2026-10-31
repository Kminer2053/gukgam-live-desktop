import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,readdir,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {SecureVault} from '../src/storage/vault';
import {JevKeyStore} from '../src/storage/jev-key';
import {RoleSettingsService,validateRoleSelection} from '../src/main/role-settings';
const KEY='SYNTHETIC_JEV_PRIVATE_KEY';
const protector={isEncryptionAvailable:()=>true,encryptString:(value:string)=>Buffer.from(Buffer.from(value).toString('base64')),decryptString:(value:Buffer)=>Buffer.from(value.toString(),'base64').toString()};

test('Jev 키는 전용 암호화 슬롯에 저장하며 화면 응답은 hasKey만 반환한다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-jev-vault-'));
  try{
    const store=new JevKeyStore(new SecureVault(dir,protector));
    assert.deepEqual(await store.status(),{hasKey:false});
    assert.deepEqual(await store.save(KEY),{hasKey:true});
    assert.equal((await readFile(join(dir,'jev-api-key.enc'),'utf8')).includes(KEY),false);
    assert.deepEqual(await readdir(dir),['jev-api-key.enc']);
    assert.equal(await store.readForEngine(),KEY);
    assert.equal(JSON.stringify(await store.status()).includes(KEY),false);
    const initial=await store.configurationForEngine();
    const restarted=new JevKeyStore(new SecureVault(dir,protector));assert.deepEqual(await restarted.configurationForEngine(),initial);
    await restarted.save(KEY);assert.notEqual((await restarted.configurationForEngine()).revision,initial.revision);
    await store.delete();assert.deepEqual(await store.status(),{hasKey:false});
    const deleted=await store.configurationForEngine();assert.equal(deleted.key,'');assert.notEqual(deleted.revision,initial.revision);
    assert.equal((await readFile(join(dir,'jev-api-key.enc'),'utf8')).includes(KEY),false);
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('OS 보안 저장소 실패와 잘못된 키는 평문 저장이나 비밀 포함 오류로 대체하지 않는다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-jev-locked-'));
  try{
    const store=new JevKeyStore(new SecureVault(dir,{...protector,isEncryptionAvailable:()=>false}));
    await assert.rejects(store.save(KEY),error=>error instanceof Error&&!error.message.includes(KEY));
    for(const value of ['',true,'bad key','bad\nkey','a'.repeat(4097)])await assert.rejects(store.save(value));
    assert.deepEqual(await readdir(dir),[]);
    const status=await store.status();assert.equal(status.hasKey,false);assert.ok(status.storageWarning);
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('엔진 실행 중 키 변경은 막고 시작 시 키는 익명 파이프에만 적용한다',async()=>{
  const values=new Map<string,unknown>(),sent:unknown[]=[];let state='stopped';
  const vault={get:async<T>(key:string)=>values.get(key) as T|undefined,set:async(key:string,value:unknown)=>{values.set(key,value);},delete:async(key:string)=>{values.delete(key);}};
  const engine={status:()=>({state:state as any,message:''}),sendSensitive:(value:unknown,secret:string)=>{assert.equal(secret,KEY);sent.push(value);}};
  const service=new RoleSettingsService(vault,engine,async()=>{throw Error('실제 HTTP 금지');});
  await service.saveKey(KEY);assert.equal(sent.length,0);
  state='ready';await assert.rejects(service.saveKey('OTHER'),/기록 엔진/);await assert.rejects(service.deleteKey(),/기록 엔진/);
  await service.syncKey();assert.equal(sent.length,1);assert.equal((sent[0] as any).type,'jev.configure');assert.equal((sent[0] as any).key,KEY);assert.match((sent[0] as any).revision,/^[0-9a-f-]{36}$/);
  const result=await service.status();assert.equal(result.hasKey,true);assert.equal(JSON.stringify(result).includes(KEY),false);
  state='stopped';await service.deleteKey();assert.equal((values.get('jev-api-key') as any).key,undefined);assert.equal((await service.status()).hasKey,false);
});

test('보안 저장소와 파이프 오류가 기록 시작을 막지 않는다',async()=>{
  const vault={get:async<T>():Promise<T|undefined>=>{throw Error(KEY);},set:async()=>{},delete:async()=>{}};
  const service=new RoleSettingsService(vault,{status:()=>({state:'ready',message:''}),sendSensitive:()=>{throw Error(KEY);}},async()=>{throw Error('HTTP 금지');});
  await service.syncKey();const result=await service.status();assert.equal(JSON.stringify(result).includes(KEY),false);assert.ok(result.message);
});

test('선택과 Julia 준비는 고정 로컬 경로만 사용하며 자동 검토 설정을 바꾸지 않는다',async()=>{
  const calls:Array<{url:string;method:string;body:any}>=[];
  const vault={get:async<T>():Promise<T|undefined>=>undefined,set:async()=>{},delete:async()=>{}};
  const fetcher=(async(url:any,options:RequestInit={})=>{
    calls.push({url:String(url),method:options.method||'GET',body:options.body?JSON.parse(String(options.body)):undefined});
    assert.equal((options.headers as any)['X-Requested-With'],'gukgam');assert.equal(options.redirect,'error');
    return Response.json({selected_provider:'julia',cpu_threads:1,enabled:false,auto_block:false,model:{state:'idle',available:false,message:'준비 전'},worker:{available:false},key:KEY});
  }) as typeof fetch;
  const service=new RoleSettingsService(vault,{status:()=>({state:'ready',message:''}),sendSensitive:()=>{}},fetcher);
  const initial=await service.status();assert.equal(calls.length,1);assert.equal(JSON.stringify(initial).includes(KEY),false);
  await service.saveSelection({provider:'julia',threads:1});
  assert.deepEqual(calls[1].body,{role_decision_provider:'julia',julia_threads:1});
  assert.equal(calls.some(call=>call.url.includes('/prepare')),false);
  await service.prepareJulia();const prepare=calls.find(call=>call.url.endsWith('/julia/prepare'))!;
  assert.deepEqual(prepare.body,{confirm:true});assert.equal(prepare.method,'POST');
  await service.cancelJulia();assert.ok(calls.find(call=>call.url.endsWith('/julia/cancel')));
  assert.ok(calls.every(call=>call.url.startsWith('http://127.0.0.1:8765/api/settings/')));
  for(const value of [{provider:'other',threads:1},{provider:'none',threads:true},{provider:'jev',threads:3},{provider:'julia',threads:1,key:KEY}])assert.throws(()=>validateRoleSelection(value));
});
