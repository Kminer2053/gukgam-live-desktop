import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {SecureVault} from '../src/storage/vault';
import {SetupStore} from '../src/storage/settings';
const protector={isEncryptionAvailable:()=>true,encryptString:(text:string)=>Buffer.from(Buffer.from(text).toString('base64')),decryptString:(bytes:Buffer)=>Buffer.from(bytes.toString(),'base64').toString()};
test('토큰은 보안저장소에만 저장하고 화면용 설정에서 제외한다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-desktop-test-'));
  try {
    const vault=new SecureVault(join(dir,'vault'),protector),settings=new SetupStore(dir,vault);
    const status=await settings.save({organization:'모의 기관',relayUrl:'https://audit.example.com',relayToken:'MOCK_RELAY_SECRET'});
    assert.equal(status.hasRelayToken,true);assert.equal(JSON.stringify(status).includes('MOCK_RELAY_SECRET'),false);
    assert.equal((await readFile(join(dir,'setup.json'),'utf8')).includes('MOCK_RELAY_SECRET'),false);
    assert.equal((await settings.engineConfig()).relayToken,'MOCK_RELAY_SECRET');
    await settings.save({organization:'다른 모의기관',relayUrl:'https://audit.example.com'});assert.equal((await settings.engineConfig()).relayToken,'MOCK_RELAY_SECRET');
    await settings.save({organization:'모의기관',relayUrl:'https://other.example.com'});assert.equal((await settings.read()).hasRelayToken,false);
    await settings.save({organization:'모의기관'});assert.equal((await settings.read()).hasRelayToken,false);
  } finally {await rm(dir,{recursive:true,force:true});}
});
test('운영체제 보안저장소가 없으면 평문 저장으로 대체하지 않는다',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'gukgam-vault-test-'));
  try {const vault=new SecureVault(dir,{...protector,isEncryptionAvailable:()=>false});await assert.rejects(vault.set('chatgpt',{token:'MOCK'}),/보안 저장소/);await assert.rejects(readFile(join(dir,'chatgpt.enc')));await assert.rejects(vault.get('../outside'),/보안 저장소|잘못된/);} finally {await rm(dir,{recursive:true,force:true});}
});
