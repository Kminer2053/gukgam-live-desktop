import {constants} from 'node:fs';
import {lstat,mkdir,mkdtemp,open,readdir,realpath,rename,rm} from 'node:fs/promises';
import {createHash,randomUUID} from 'node:crypto';
import {dirname,isAbsolute,join,relative,resolve,sep} from 'node:path';
import {list as listTar,extract as extractTar} from 'tar';
import type {RuntimePackId,RuntimePackSpec,RuntimePackStatus} from './contracts';

const IDS=new Set(['engine','relay-tools']);
const MAX_DOWNLOAD=512*1024*1024,MAX_UNPACKED=1024*1024*1024,MAX_FILES=20_000;
const MARKER='.gukgam-component.json';
const sha=(value:Buffer|string)=>createHash('sha256').update(value).digest('hex');
const samePath=(a:string,b:string)=>process.platform==='win32'?a.toLowerCase()===b.toLowerCase():a===b;
class PackError extends Error {constructor(readonly code:string){super(code);}}
function demand(ok:unknown,code:string):asserts ok {if(!ok)throw new PackError(code);}
function check(signal?:AbortSignal){if(signal?.aborted)throw new PackError('cancelled');}
export function safePackPath(value:string):boolean {
  if(typeof value!=='string'||!value||value.length>1024||value!==value.normalize('NFC')||value.startsWith('/')||/[\\:\u0000-\u001f\u007f]/.test(value))return false;
  return value.split('/').every(p=>p&&p!=='.'&&p!=='..'&&!/[. ]$/.test(p)&&!/^\s/.test(p)&&!/[<>"|?*]/.test(p)
    &&!/^(?:con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/i.test(p));
}
export function validateRuntimePackSpec(raw:RuntimePackSpec):RuntimePackSpec {
  const spec=structuredClone(raw);
  demand(spec&&IDS.has(spec.id)&&/^[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}$/.test(spec.version),'catalog_identity');
  demand(Number.isSafeInteger(spec.bytes)&&spec.bytes>0&&spec.bytes<=MAX_DOWNLOAD&&/^[a-f0-9]{64}$/.test(spec.sha256),'catalog_archive');
  demand(Number.isSafeInteger(spec.unpackedBytes)&&spec.unpackedBytes>0&&spec.unpackedBytes<=MAX_UNPACKED,'catalog_size');
  let url:URL;try{url=new URL(spec.url);}catch{throw new PackError('catalog_url');}
  demand(url.protocol==='https:'&&url.hostname==='github.com'&&!url.port&&!url.username&&!url.password&&!url.search&&!url.hash
    &&/^\/Kminer2053\/gukgam-live-desktop\/releases\/download\/v[0-9][A-Za-z0-9.+_-]*\/Gukgam-(?:Engine|Relay-Tools)-win-x64-[A-Za-z0-9._+-]+\.tar\.gz$/.test(url.pathname),'catalog_url');
  demand(Array.isArray(spec.files)&&spec.files.length>0&&spec.files.length<=MAX_FILES,'catalog_files');
  const names=new Set<string>();let total=0;
  for(const file of spec.files){
    demand(safePackPath(file.path)&&file.path!==MARKER&&!names.has(file.path.toLowerCase()),'catalog_file_path');
    names.add(file.path.toLowerCase());
    demand(Number.isSafeInteger(file.bytes)&&file.bytes>=0&&file.bytes<=MAX_UNPACKED&&/^[a-f0-9]{64}$/.test(file.sha256),'catalog_file_hash');total+=file.bytes;
  }
  demand(total===spec.unpackedBytes&&safePackPath(spec.entryPoint)&&spec.files.some(f=>f.path===spec.entryPoint),'catalog_entry');
  demand(spec.entryPoint===(spec.id==='engine'?'gukgam-engine.exe':'node_modules/vercel/dist/vc.js'),'catalog_fixed_entry');
  return spec;
}
async function unlinked(file:string){
  for(let current=resolve(file);;current=dirname(current)){
    try{const s=await lstat(current);demand(!s.isSymbolicLink(),'linked_path');demand(samePath(await realpath(current),current),'path_identity');}
    catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
    if(current===dirname(current))break;
  }
}
async function directory(file:string,walkAncestors=true){if(walkAncestors)await unlinked(file);const s=await lstat(file,{bigint:true});demand(s.isDirectory()&&!s.isSymbolicLink()&&samePath(await realpath(file),file),'not_directory');return s;}
function child(root:string,path:string){demand(safePackPath(path),'unsafe_relative_path');const p=resolve(root,...path.split('/'));demand(relative(root,p)!==''&&!relative(root,p).startsWith('..'+sep)&&!isAbsolute(relative(root,p)),'path_escape');return p;}
async function digestRegular(file:string,expectedBytes:number,signal?:AbortSignal,readBuffer?:Buffer,checkedParent?:string):Promise<string>{
  check(signal);if(checkedParent)demand(samePath(dirname(file),checkedParent),'unchecked_parent');else await unlinked(file);
  const before=await lstat(file,{bigint:true});
  demand(before.isFile()&&!before.isSymbolicLink()&&before.nlink===1n&&before.size===BigInt(expectedBytes),'file_identity');
  const handle=await open(file,constants.O_RDONLY|(constants.O_NOFOLLOW??0));
  const same=(s:typeof before)=>s.isFile()&&s.nlink===1n&&s.dev===before.dev&&s.ino===before.ino&&s.size===before.size&&s.mtimeNs===before.mtimeNs&&s.ctimeNs===before.ctimeNs;
  try {
    demand(same(await handle.stat({bigint:true})),'open_identity');const h=createHash('sha256'),buffer=readBuffer??Buffer.alloc(1024*1024);let read=0;
    for(;;){check(signal);const r=await handle.read(buffer,0,buffer.length,null);if(!r.bytesRead)break;read+=r.bytesRead;demand(read<=expectedBytes,'file_size');h.update(buffer.subarray(0,r.bytesRead));}
    demand(read===expectedBytes&&same(await handle.stat({bigint:true}))&&same(await lstat(file,{bigint:true})),'read_identity');return h.digest('hex');
  } finally {await handle.close();}
}
function marker(spec:RuntimePackSpec){return JSON.stringify({schema:1,id:spec.id,version:spec.version,sha256:spec.sha256,catalog:sha(JSON.stringify(spec))})+'\n';}
const LABELS:Record<RuntimePackId,string>={engine:'기록 화면', 'relay-tools':'중계 도구'};
type Options={root:string;catalog:readonly RuntimePackSpec[];fetcher?:typeof fetch;timeoutMs?:number};
type Operation={controller:AbortController;subscribers:Set<symbol>;promise:Promise<string>};

/** URL·판본·해시·실행 파일은 앱에 고정한다. 화면은 구성 이름과 진행 상태만 다룬다. */
export class RuntimePackManager {
  private specs=new Map<RuntimePackId,RuntimePackSpec>();
  private states=new Map<RuntimePackId,RuntimePackStatus>();
  private operations=new Map<RuntimePackId,Operation>();
  constructor(private options:Options){
    demand(isAbsolute(options.root),'absolute_root_required');
    for(const raw of options.catalog){const spec=validateRuntimePackSpec(raw);demand(!this.specs.has(spec.id),'duplicate_component');this.specs.set(spec.id,spec);}
  }
  private spec(id:RuntimePackId){const s=this.specs.get(id);demand(IDS.has(id)&&s,'component_not_configured');return s;}
  private target(spec:RuntimePackSpec){return join(this.options.root,`${spec.id}-${spec.version}-${spec.sha256.slice(0,16)}`);}
  private state(id:RuntimePackId,state:RuntimePackStatus['state'],message:string,receivedBytes=0){
    const totalBytes=this.specs.get(id)?.bytes??0;
    const value={id,state,message,receivedBytes,totalBytes,progress:state==='ready'?100:totalBytes?Math.min(99,Math.floor(receivedBytes/totalBytes*100)):0};
    this.states.set(id,value);return {...value};
  }
  async status(id:RuntimePackId):Promise<RuntimePackStatus>{
    const active=this.states.get(id);if(active)return {...active};
    let spec:RuntimePackSpec;try{spec=this.spec(id);}catch{return this.state(id,'missing',`${LABELS[id]}는 새 설치파일에서 준비할 수 있습니다.`);}
    const file=join(this.target(spec),MARKER),data=Buffer.from(marker(spec));
    try{if(await digestRegular(file,data.length)===sha(data))return this.state(id,'ready',`${LABELS[id]}가 설치되어 있습니다. 실행 전에 파일을 확인합니다.`,spec.bytes);}
    catch{}
    return this.state(id,'missing',`${LABELS[id]}는 처음 사용할 때 한 번 내려받습니다.`);
  }
  cancel(id:RuntimePackId){this.operations.get(id)?.controller.abort();}
  ensure(id:RuntimePackId,signal?:AbortSignal,onProgress?:(s:RuntimePackStatus)=>void):Promise<string>{
    if(signal?.aborted)return Promise.reject(Error('필요한 파일 준비를 취소했습니다.'));
    let spec:RuntimePackSpec;try{spec=this.spec(id);}catch{return Promise.reject(Error('구성 파일의 판본을 확인하지 못했습니다. 최신 설치파일을 확인하세요.'));}
    let operation=this.operations.get(id);
    // 취소된 호출의 정리가 끝나기 전 재시도해도 같은 취소 Promise에 다시 묶지 않는다.
    if(operation?.controller.signal.aborted)return operation.promise.catch(()=>undefined).then(()=>this.ensure(id,signal,onProgress));
    if(!operation){
      const controller=new AbortController(),subscribers=new Set<symbol>();
      const promise=Promise.resolve().then(()=>this.install(spec,controller.signal)).finally(()=>{if(this.operations.get(id)?.controller===controller)this.operations.delete(id);});
      operation={controller,subscribers,promise};this.operations.set(id,operation);
    }
    const owned=operation,key=Symbol();owned.subscribers.add(key);
    return new Promise((resolve,reject)=>{
      let finished=false;const progress=()=>{const s=this.states.get(id);if(s)try{onProgress?.({...s});}catch{}};
      const timer=onProgress?setInterval(progress,250):undefined;
      const finish=(error?:Error,value?:string)=>{if(finished)return;finished=true;clearInterval(timer);signal?.removeEventListener('abort',abort);owned.subscribers.delete(key);progress();error?reject(error):resolve(value!);};
      const abort=()=>{finish(Error('필요한 파일 준비를 취소했습니다.'));if(owned.subscribers.size===0)owned.controller.abort();};
      signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted){abort();return;}
      owned.promise.then(path=>finish(undefined,path),error=>finish(error instanceof Error?error:Error('필요한 파일 준비에 실패했습니다.')));
    });
  }
  private async verify(root:string,spec:RuntimePackSpec,signal:AbortSignal,withMarker:boolean){
    const rootBefore=await directory(root);const allowed=new Map(spec.files.map(f=>[f.path,f]));
    const dirs=new Set<string>();for(const file of spec.files){const parts=file.path.split('/');for(let i=1;i<parts.length;i++)dirs.add(parts.slice(0,i).join('/'));}
    const found=new Set<string>(),stack=[''],buffer=Buffer.alloc(1024*1024);
    // 각 폴더는 읽기 전후 identity를 대조한다. 같은 상위 폴더를 파일마다 다시 걷지 않되,
    // 모든 파일의 열린 핸들·링크 수·전체 해시와 루트 경로 검증은 그대로 수행한다.
    while(stack.length){check(signal);const dir=stack.pop()!;const base=dir?child(root,dir):root;const parentBefore=await directory(base,false);
      for(const row of await readdir(base,{withFileTypes:true})){
        const name=dir?dir+'/'+row.name:row.name;demand(safePackPath(name),'installed_path');
        if(row.isDirectory()){demand(dirs.has(name),'unexpected_directory');stack.push(name);}
        else{demand(row.isFile()&&!row.isSymbolicLink(),'installed_file_type');
          if(name===MARKER&&withMarker){const bytes=Buffer.from(marker(spec));demand(await digestRegular(child(root,name),bytes.length,signal,buffer,base)===sha(bytes),'marker_hash');continue;}
          const file=allowed.get(name);demand(file&&!found.has(name),'unexpected_file');
          demand(await digestRegular(child(root,name),file.bytes,signal,buffer,base)===file.sha256,'installed_hash');found.add(name);
        }
      }
      const parentAfter=await directory(base,false);demand(parentBefore.dev===parentAfter.dev&&parentBefore.ino===parentAfter.ino,'parent_changed');
    }
    const rootAfter=await directory(root);demand(rootBefore.dev===rootAfter.dev&&rootBefore.ino===rootAfter.ino,'root_changed');
    demand(found.size===spec.files.length,'installed_file_missing');
    if(withMarker){const bytes=Buffer.from(marker(spec));demand(await digestRegular(child(root,MARKER),bytes.length,signal,buffer,root)===sha(bytes),'marker_missing');}
  }
  private async download(spec:RuntimePackSpec,file:string,signal:AbortSignal){
    let url=new URL(spec.url),response:Response|undefined;
    const deadline=AbortSignal.timeout(this.options.timeoutMs??20*60_000),combined=AbortSignal.any([signal,deadline]);
    for(let redirects=0;redirects<=4;redirects++){
      check(combined);response=await (this.options.fetcher??fetch)(url,{method:'GET',redirect:'manual',credentials:'omit',signal:combined,headers:{Accept:'application/octet-stream'}});
      if([301,302,303,307,308].includes(response.status)){
        const location=response.headers.get('location');await response.body?.cancel();demand(location&&redirects<4,'download_redirect');
        url=new URL(location,url);demand(url.protocol==='https:'&&!url.port&&!url.username&&!url.password&&!url.hash
          &&['release-assets.githubusercontent.com','objects.githubusercontent.com'].includes(url.hostname),'download_host');continue;
      }
      break;
    }
    try {
      demand(response?.status===200&&response.body,'download_status');
      const length=response.headers.get('content-length');demand(length===null||/^\d+$/.test(length)&&Number(length)===spec.bytes,'download_length');
      const encoding=response.headers.get('content-encoding');demand(!encoding||encoding==='identity','download_encoding');
    } catch(error){await response?.body?.cancel().catch(()=>{});throw error;}
    let handle:Awaited<ReturnType<typeof open>>;
    try{handle=await open(file,constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);}
    catch(error){await response.body.cancel().catch(()=>{});throw error;}
    const reader=response.body.getReader(),hash=createHash('sha256');let size=0;
    const abort=()=>{void reader.cancel().catch(()=>{});};combined.addEventListener('abort',abort,{once:true});
    try {
      for(;;){check(combined);const item=await reader.read();if(item.done)break;check(combined);size+=item.value.length;demand(size<=spec.bytes,'download_limit');hash.update(item.value);let offset=0;while(offset<item.value.length){const wrote=await handle.write(item.value,offset,item.value.length-offset,null);demand(wrote.bytesWritten>0,'download_write');offset+=wrote.bytesWritten;}this.state(spec.id,'downloading',`${LABELS[spec.id]}에 필요한 파일을 내려받고 있습니다.`,size);}
      check(combined);demand(size===spec.bytes&&hash.digest('hex')===spec.sha256,'download_hash');await handle.sync();
    } finally {combined.removeEventListener('abort',abort);await reader.cancel().catch(()=>{});reader.releaseLock();await handle.close();}
  }
  private async extract(spec:RuntimePackSpec,archive:string,staging:string,signal:AbortSignal){
    const expected=new Map(spec.files.map(f=>[f.path,f])),seen=new Set<string>();let total=0,invalid:Error|undefined;
    // 추출 전에 모든 유효 경로·형식·크기·목록을 확인한다. 다운로드 해시도 먼저 확인한다.
    await listTar({file:archive,strict:true,maxMetaEntrySize:8192,onReadEntry:entry=>{
      try{check(signal);const file=expected.get(entry.path);demand(entry.type==='File'&&safePackPath(entry.path)&&file&&!seen.has(entry.path.toLowerCase()),'archive_entry');
        demand(entry.size===file.bytes,'archive_entry_size');seen.add(entry.path.toLowerCase());total+=entry.size;demand(total<=spec.unpackedBytes&&seen.size<=MAX_FILES,'archive_budget');}
      catch(error){invalid=invalid??(error instanceof Error?error:new PackError('archive_invalid'));}
    }});
    check(signal);if(invalid)throw invalid;demand(total===spec.unpackedBytes&&seen.size===expected.size,'archive_missing');
    demand(await digestRegular(archive,spec.bytes,signal)===spec.sha256,'archive_changed');
    const extracting=new Set<string>();let extractedBytes=0;
    await extractTar({file:archive,cwd:staging,strict:true,preservePaths:false,preserveOwner:false,noMtime:true,umask:0o077,maxMetaEntrySize:8192,
      filter:(name,entry)=>{check(signal);const file=expected.get(name);demand(file&&safePackPath(name)&&'type'in entry&&entry.type==='File'&&!extracting.has(name.toLowerCase())&&entry.size===file.bytes,'extract_entry');
        extracting.add(name.toLowerCase());extractedBytes+=entry.size;demand(extractedBytes<=spec.unpackedBytes&&extracting.size<=expected.size,'extract_budget');return true;}});
    check(signal);
  }
  private async install(spec:RuntimePackSpec,signal:AbortSignal){
    let temporary:string|undefined;
    let temporaryIdentity:{dev:bigint;ino:bigint}|undefined;
    try {
      this.state(spec.id,'checking',`${LABELS[spec.id]}의 설치 상태를 확인하고 있습니다.`);check(signal);
      await unlinked(this.options.root);await mkdir(this.options.root,{recursive:true,mode:0o700});await directory(this.options.root);
      const target=this.target(spec);
      let exists=false;try{await lstat(target);exists=true;}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error;}
      if(exists){
        try{await this.verify(target,spec,signal,true);check(signal);this.state(spec.id,'ready',`${LABELS[spec.id]}가 준비됐습니다.`,spec.bytes);return target;}
        catch{check(signal);await directory(target);await rename(target,join(this.options.root,`${spec.id}-invalid-${randomUUID()}`));}
      }
      temporary=await mkdtemp(join(this.options.root,`.${spec.id}-prepare-`));await directory(temporary);
      const temporaryStat=await lstat(temporary,{bigint:true});temporaryIdentity={dev:temporaryStat.dev,ino:temporaryStat.ino};
      const archive=join(temporary,'component.tar.gz'),stage=join(temporary,'content');await mkdir(stage,{mode:0o700});
      this.state(spec.id,'downloading',`${LABELS[spec.id]}에 필요한 파일을 처음 한 번 내려받습니다.`);
      await this.download(spec,archive,signal);check(signal);
      this.state(spec.id,'extracting',`${LABELS[spec.id]}의 파일을 안전하게 확인하고 있습니다.`,spec.bytes);
      await this.extract(spec,archive,stage,signal);await this.verify(stage,spec,signal,false);check(signal);
      const h=await open(join(stage,MARKER),constants.O_CREAT|constants.O_EXCL|constants.O_WRONLY,0o600);try{await h.writeFile(marker(spec));await h.sync();}finally{await h.close();}
      await this.verify(stage,spec,signal,true);check(signal);await directory(this.options.root);await rename(stage,target);
      this.state(spec.id,'ready',`${LABELS[spec.id]}가 준비됐습니다.`,spec.bytes);return target;
    } catch(error) {
      const cancelled=signal.aborted;this.state(spec.id,cancelled?'cancelled':'error',cancelled?'필요한 파일 준비를 취소했습니다.':`${LABELS[spec.id]} 파일 준비를 마치지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도하세요.`);
      throw Error(this.states.get(spec.id)!.message);
    } finally {
      // 이 호출이 만든 새 폴더만 정리한다. 사용 중인 다른 판본과 사용자 자료는 건드리지 않는다.
      if(temporary){const absolute=resolve(temporary);if(dirname(absolute)===resolve(this.options.root)&&absolute.startsWith(resolve(this.options.root)+sep+'.'+spec.id+'-prepare-')){
        await directory(absolute).then(async()=>{const current=await lstat(absolute,{bigint:true});if(temporaryIdentity&&current.dev===temporaryIdentity.dev&&current.ino===temporaryIdentity.ino)await rm(absolute,{recursive:true,force:true});}).catch(()=>{});
      }}
    }
  }
}
