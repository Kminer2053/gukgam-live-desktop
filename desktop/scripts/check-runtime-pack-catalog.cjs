'use strict';
// 앱에 고정된 카탈로그와 이번 빌드의 실제 압축 바이트가 같은 경우만 배포한다.
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const root=path.resolve(__dirname,'..');
const fail=code=>{throw Error(code);};
function unlinked(file){for(let p=path.resolve(file);;p=path.dirname(p)){try{if(fs.lstatSync(p).isSymbolicLink())fail('ERR_COMPONENT_BUILD_LINK');}catch(e){if(e.code!=='ENOENT')throw e;}if(p===path.dirname(p))break;}}
try{
  const pkg=JSON.parse(fs.readFileSync(path.join(root,'package.json'),'utf8'));
  const file=path.join(root,'src/runtime-packs/catalog.generated.ts');unlinked(file);
  const source=fs.readFileSync(file,'utf8'),match=/export const PACK_CATALOG:readonly RuntimePackSpec\[\]=(\[.*\]);\s*$/s.exec(source);
  if(!match)fail('ERR_COMPONENT_CATALOG_NOT_GENERATED');
  const rows=JSON.parse(match[1]);if(rows.length!==2||new Set(rows.map(x=>x.id)).size!==2||!rows.some(x=>x.id==='engine')||!rows.some(x=>x.id==='relay-tools'))fail('ERR_COMPONENT_CATALOG_INCOMPLETE');
  for(const row of rows){
    const url=new URL(row.url),name=path.posix.basename(url.pathname);
    if(row.version!==pkg.version||url.origin!=='https://github.com'||url.search||url.hash||url.pathname!==`/Kminer2053/gukgam-live-desktop/releases/download/v${pkg.version}/${name}`||!/^Gukgam-(Engine|Relay-Tools)-win-x64-[0-9.]+\.tar\.gz$/.test(name))fail('ERR_COMPONENT_RELEASE_URL');
    if(!Array.isArray(row.files)||!row.files.length||!row.files.some(f=>f.path===row.entryPoint)||row.files.reduce((n,f)=>n+f.bytes,0)!==row.unpackedBytes)fail('ERR_COMPONENT_FILE_CATALOG');
    const archive=path.resolve(root,'../dist/runtime-packs',name);unlinked(archive);
    if(!fs.lstatSync(archive).isFile()||fs.statSync(archive).size!==row.bytes||crypto.createHash('sha256').update(fs.readFileSync(archive)).digest('hex')!==row.sha256)fail('ERR_COMPONENT_ARCHIVE_DRIFT');
  }
  console.log(JSON.stringify({ok:true,version:pkg.version,verified_packs:rows.map(r=>({id:r.id,bytes:r.bytes,sha256:r.sha256}))}));
}catch(error){console.error(error instanceof Error?error.message:'ERR_COMPONENT_BUILD');process.exitCode=1;}
