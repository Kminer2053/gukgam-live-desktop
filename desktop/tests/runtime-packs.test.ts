import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {gzipSync} from 'node:zlib';
import {mkdtemp,mkdir,readFile,readdir,rm,writeFile,symlink,link} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {RuntimePackManager,safePackPath,validateRuntimePackSpec} from '../src/runtime-packs/manager';
import type {RuntimePackSpec} from '../src/runtime-packs/contracts';

const hash=(x:Buffer|string)=>createHash('sha256').update(x).digest('hex');
// 시험 전용 USTAR: 운영 압축 생성·해제 코드를 복제하지 않고 악성 헤더를 독립 구성한다.
function archive(rows:Array<{name:string;body?:string;type?:string;link?:string;size?:number}>){
  const blocks:Buffer[]=[];
  for(const row of rows){const data=Buffer.from(row.body??''),head=Buffer.alloc(512);head.write(row.name,0,100,'utf8');
    for(const [at,length,value]of [[100,8,0o600],[108,8,0],[116,8,0],[124,12,row.size??data.length],[136,12,0]] as const)head.write(value.toString(8).padStart(length-1,'0')+'\0',at,length,'ascii');
    head.fill(32,148,156);head.write(row.type??'0',156,1);head.write(row.link??'',157,100);head.write('ustar\0',257,6);head.write('00',263,2);
    const sum=head.reduce((a,b)=>a+b,0);head.write(sum.toString(8).padStart(6,'0')+'\0 ',148,8);
    blocks.push(head,data,Buffer.alloc((512-data.length%512)%512));
  }
  return gzipSync(Buffer.concat([...blocks,Buffer.alloc(1024)]));
}
function fixture(data=archive([{name:'gukgam-engine.exe',body:'synthetic engine only'}])){
  const file=Buffer.from('synthetic engine only');
  const spec:RuntimePackSpec={id:'engine',version:'0.2.0',url:'https://github.com/Kminer2053/gukgam-live-desktop/releases/download/v0.2.0/Gukgam-Engine-win-x64-0.2.0.tar.gz',bytes:data.length,sha256:hash(data),unpackedBytes:file.length,entryPoint:'gukgam-engine.exe',files:[{path:'gukgam-engine.exe',bytes:file.length,sha256:hash(file)}]};
  return {data,spec};
}
async function owned(t:test.TestContext){const p=await mkdtemp(join(tmpdir(),'gukgam-pack-test-'));t.after(()=>rm(p,{recursive:true,force:true}));return p;}
const response=(bytes:Buffer)=>new Response(new Uint8Array(bytes).buffer,{status:200,headers:{'content-length':String(bytes.length)}});

test('화면 상태 조회는 다운로드하지 않고 실제 사용 때 검증·설치·재사용한다',async t=>{
  const root=await owned(t),{data,spec}=fixture();let calls=0;
  const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>{calls++;return response(data);}});
  assert.equal((await manager.status('engine')).state,'missing');assert.equal(calls,0);
  const installed=await manager.ensure('engine');assert.equal(calls,1);assert.equal(await readFile(join(installed,spec.entryPoint),'utf8'),'synthetic engine only');
  assert.equal((await manager.status('engine')).state,'ready');assert.equal(await manager.ensure('engine'),installed);assert.equal(calls,1);
  assert.deepEqual((await readdir(root)).filter(x=>x.startsWith('.engine-prepare-')),[]);
});
test('해시가 다른 다운로드는 설치하지 않고 같은 준비 요청을 다시 시도할 수 있다',async t=>{
  const root=await owned(t),{data,spec}=fixture();let calls=0;
  const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>response(++calls===1?Buffer.alloc(data.length,1):data)});
  await assert.rejects(manager.ensure('engine'));assert.equal((await manager.status('engine')).state,'error');assert.deepEqual(await readdir(root),[]);
  const path=await manager.ensure('engine');assert.equal(calls,2);assert.equal(await readFile(join(path,spec.entryPoint),'utf8'),'synthetic engine only');
});
test('고정 길이보다 큰 응답은 본문을 설치하지 않는다',async t=>{
  const root=await owned(t),{spec}=fixture();let cancelled=false;
  const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>new Response(new ReadableStream({cancel(){cancelled=true;}}),{status:200,headers:{'content-length':String(spec.bytes+1)}})});
  await assert.rejects(manager.ensure('engine'));assert.equal(cancelled,true);assert.deepEqual(await readdir(root),[]);
});
test('GitHub 외부·평문·자격증명이 있는 리다이렉트는 따라가지 않는다',async t=>{
  for(const location of ['https://evil.test/component','http://release-assets.githubusercontent.com/file','https://a:b@release-assets.githubusercontent.com/file']){
    const root=await owned(t),{spec}=fixture();let calls=0;const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>{calls++;return new Response(null,{status:302,headers:{location}});}});
    await assert.rejects(manager.ensure('engine'));assert.equal(calls,1);assert.deepEqual(await readdir(root),[]);
  }
});
test('공식 릴리스 CDN으로만 이동하며 인증 헤더나 쿠키를 보내지 않는다',async t=>{
  const root=await owned(t),{data,spec}=fixture();const urls:string[]=[];
  const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async(url,options)=>{
    urls.push(String(url));assert.equal(options?.redirect,'manual');assert.equal(options?.credentials,'omit');assert.equal(new Headers(options?.headers).get('authorization'),null);
    return urls.length===1?new Response(null,{status:302,headers:{location:'https://release-assets.githubusercontent.com/synthetic?signature=test'}}):response(data);
  }});await manager.ensure('engine');assert.equal(urls.length,2);
});
test('Windows 장치명·ADS·부모경로·중복 경로 카탈로그를 거부한다',()=>{
  for(const path of ['../escape','/absolute','a\\b','a:b','aux.txt','folder/NUL','folder/CONIN$','LPT¹.txt','COM²','folder/name.','folder/name ','a//b','a/./b'])assert.equal(safePackPath(path),false,path);
  const {spec}=fixture();assert.throws(()=>validateRuntimePackSpec({...spec,files:[...spec.files,{...spec.files[0],path:'GUKGAM-ENGINE.EXE'}]}));
});
test('해시가 맞는 압축도 경로 우회·링크·중복·잘못된 크기를 추출 전에 거부한다',async t=>{
  const malicious=[
    archive([{name:'../escape.exe',body:'synthetic engine only'}]),
    archive([{name:'gukgam-engine.exe',type:'2',link:'../outside'}]),
    archive([{name:'gukgam-engine.exe',type:'1',link:'../outside'}]),
    archive([{name:'gukgam-engine.exe',body:'synthetic engine only'},{name:'gukgam-engine.exe',body:'synthetic engine only'}]),
    archive([{name:'gukgam-engine.exe',body:'short'}]),
  ];
  for(const data of malicious){const root=await owned(t),{spec}=fixture(data);const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>response(data)});
    await assert.rejects(manager.ensure('engine'));assert.deepEqual(await readdir(root),[]);
  }
});
test('캐시 파일의 같은 크기 변조와 파일 누락은 실행 전에 발견해 다시 준비한다',async t=>{
  for(const missing of [false,true]){
    const root=await owned(t),{data,spec}=fixture();let calls=0;const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>{calls++;return response(data);}});
    const installed=await manager.ensure('engine'),file=join(installed,spec.entryPoint);
    if(missing)await rm(file);else await writeFile(file,'synthetic engine evil');
    assert.equal(await manager.ensure('engine'),installed);assert.equal(calls,2);assert.equal(await readFile(file,'utf8'),'synthetic engine only');
  }
});
test('준비 전 취소는 네트워크를 사용하지 않는다',async t=>{
  const root=await owned(t),{spec}=fixture();let calls=0;const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>{calls++;throw Error('unused');}});const c=new AbortController();c.abort();
  await assert.rejects(manager.ensure('engine',c.signal));assert.equal(calls,0);assert.deepEqual(await readdir(root),[]);
});
test('다운로드 중 취소는 대기 중인 본문을 닫고 새 실행 파일을 남기지 않는다',async t=>{
  const root=await owned(t),{data,spec}=fixture();let started!:()=>void;const ready=new Promise<void>(r=>started=r);let cancelled=false;
  const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>new Response(new ReadableStream({start(c){c.enqueue(data.subarray(0,10));started();},cancel(){cancelled=true;}}))});
  const c=new AbortController(),pending=manager.ensure('engine',c.signal);await ready;c.abort();await assert.rejects(pending);
  // 개별 호출의 취소 응답 후 소유 다운로드의 안전한 정리가 끝날 때까지 기다린다.
  for(let n=0;n<100&&(await readdir(root)).some(x=>x.startsWith('.engine-prepare-'));n++)await new Promise(r=>setTimeout(r,10));
  assert.equal(cancelled,true);assert.deepEqual(await readdir(root),[]);
});
test('하나의 준비를 공유하는 두 호출 중 하나만 취소해도 다른 호출은 완료된다',async t=>{
  const root=await owned(t),{data,spec}=fixture();let release!:()=>void;const gate=new Promise<void>(r=>release=r);let calls=0;
  const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>{calls++;await gate;return response(data);}});
  const a=new AbortController(),first=manager.ensure('engine',a.signal),second=manager.ensure('engine');a.abort();await assert.rejects(first);release();
  const path=await second;assert.equal(calls,1);assert.equal(await readFile(join(path,spec.entryPoint),'utf8'),'synthetic engine only');
});
test('상위 폴더 junction을 통해 다른 폴더에 설치하지 않는다',async t=>{
  const ownedRoot=await owned(t),outside=join(ownedRoot,'outside'),root=join(ownedRoot,'components');await mkdir(outside);await symlink(outside,root,process.platform==='win32'?'junction':'dir');
  const {data,spec}=fixture();let calls=0;const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>{calls++;return response(data);}});
  await assert.rejects(manager.ensure('engine'));assert.equal(calls,0);assert.deepEqual(await readdir(outside),[]);
});
test('캐시 hardlink를 실행하지 않고 외부 원본은 변경하지 않는다',async t=>{
  const ownedRoot=await owned(t),root=join(ownedRoot,'components'),outside=join(ownedRoot,'outside.exe'),{data,spec}=fixture();
  const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>response(data)}),installed=await manager.ensure('engine'),entry=join(installed,spec.entryPoint);
  await writeFile(outside,'synthetic engine only');await rm(entry);await link(outside,entry);
  await manager.ensure('engine');assert.equal(await readFile(outside,'utf8'),'synthetic engine only');assert.equal(await readFile(entry,'utf8'),'synthetic engine only');
});
test('모든 호출을 취소한 직후 다시 준비하면 이전 정리 뒤 새로 완료한다',async t=>{
  const root=await owned(t),{data,spec}=fixture();let started!:()=>void;const began=new Promise<void>(r=>started=r);let calls=0;
  const manager=new RuntimePackManager({root,catalog:[spec],fetcher:async()=>{
    calls++;if(calls>1)return response(data);
    return new Response(new ReadableStream({start(c){c.enqueue(data.subarray(0,8));started();}}));
  }});
  const controller=new AbortController(),first=manager.ensure('engine',controller.signal);await began;controller.abort();await assert.rejects(first);
  const installed=await manager.ensure('engine');assert.equal(calls,2);assert.equal(await readFile(join(installed,spec.entryPoint),'utf8'),'synthetic engine only');
  assert.deepEqual((await readdir(root)).filter(x=>x.startsWith('.engine-prepare-')),[]);
});
