import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';

const require=createRequire(import.meta.url);
const {apply,transform,sha,TARGETS,CACHE}=require('../scripts/apply-web-installer-security.cjs');
const desktop=fileURLToPath(new URL('../',import.meta.url));
const installed=path.join(desktop,'node_modules/app-builder-lib');
const prefix='node_modules/app-builder-lib/';
const original=(target:any)=>{
  const raw=fs.readFileSync(path.join(installed,target.file));
  if(sha(raw)===target.old)return raw;
  // 이전 개발 사본에서는 고정 원본 백업만 읽는다. 실제 패처의 허용 SHA는 넓히지 않는다.
  const previous=target.file===TARGETS[0].file?'54bdaa8c1848e08c8df41edfe1bcb4a284fbd5f9882d01c50e1917c226afa863':target.patched;
  assert([target.patched,previous].includes(sha(raw)));
  const backup=fs.readFileSync(path.join(desktop,CACHE,'originals',target.file));
  assert.equal(sha(backup),target.old);return backup;
};
function fixture(t:any){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'gukgam-web-security-'));
  t.after(()=>{
    // 이번 시험이 만든 단일 임시 폴더만 정리한다.
    assert.equal(path.dirname(path.resolve(root)),path.resolve(os.tmpdir()));
    assert(path.basename(root).startsWith('gukgam-web-security-'));
    fs.rmSync(root,{recursive:true,force:true});
  });
  const write=(relative:string,data:string|Buffer)=>{
    const file=path.join(root,relative);fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,data,{flag:'wx'});
  };
  for(const file of ['package.json','package-lock.json'])write(file,fs.readFileSync(path.join(desktop,file)));
  write(prefix+'package.json',fs.readFileSync(path.join(installed,'package.json')));
  for(const name of ['LICENSE','LICENSE.md','LICENSE.txt']){
    const source=path.join(installed,name);if(fs.existsSync(source))write(prefix+name,fs.readFileSync(source));
  }
  for(const target of TARGETS)write(prefix+target.file,original(target));
  return root;
}
function snapshot(root:string){
  const result:Record<string,string>={};
  function visit(dir:string){
    for(const entry of fs.readdirSync(dir,{withFileTypes:true})){
      assert(!entry.isSymbolicLink());const file=path.join(dir,entry.name);
      if(entry.isDirectory())visit(file);else{assert(entry.isFile());result[path.relative(root,file)]=sha(fs.readFileSync(file));}
    }
  }
  visit(root);return result;
}
function failureUnchanged(root:string,expected:RegExp){
  const before=snapshot(root);assert.throws(()=>apply(root),expected);assert.deepEqual(snapshot(root),before);
}
test('고정 공식 템플릿 두 개만 패치하고 원본·메타데이터·라이선스를 보존한다',t=>{
  const root=fixture(t),before=snapshot(root),result=apply(root);
  assert.equal(result.patched_this_run,2);assert.equal(result.already_applied,false);
  for(const target of TARGETS){
    assert.equal(sha(fs.readFileSync(path.join(root,prefix,target.file))),target.patched);
    assert.equal(sha(fs.readFileSync(path.join(root,CACHE,'originals',target.file))),target.old);
  }
  const after=snapshot(root);
  assert.equal(JSON.parse(fs.readFileSync(path.join(root,CACHE,'applied.json'),'utf8')).keep_install_archive,false);
  for(const [file,digest]of Object.entries(before)){
    if(!TARGETS.some((target:any)=>file===path.join(prefix,target.file)))assert.equal(after[file],digest);
  }
});
test('재실행은 이미 적용된 상태를 확인하며 어떤 파일도 다시 쓰지 않는다',t=>{
  const root=fixture(t);apply(root);const before=snapshot(root),result=apply(root);
  assert.equal(result.already_applied,true);assert.equal(result.patched_this_run,0);assert.deepEqual(snapshot(root),before);
});
for(const target of TARGETS)test('변조된 템플릿은 다른 템플릿·백업 쓰기 전에 거부한다: '+target.file,t=>{
  const root=fixture(t);fs.appendFileSync(path.join(root,prefix,target.file),'\n# 합성 변조\n');
  failureUnchanged(root,/ERR_WEB_TEMPLATE_HASH/);assert(!fs.existsSync(path.join(root,CACHE)));
});
test('검증하지 않은 builder 버전과 lock은 변경 없이 거부한다',t=>{
  const root=fixture(t),file=path.join(root,'package-lock.json'),lock=JSON.parse(fs.readFileSync(file,'utf8'));
  lock.packages['node_modules/app-builder-lib'].version='26.18.1';fs.writeFileSync(file,JSON.stringify(lock));
  failureUnchanged(root,/ERR_WEB_BUILDER_LOCK/);
});
test('다른 설치 스크립트로 검증 경계를 건너뛰는 구성을 거부한다',t=>{
  const root=fixture(t),file=path.join(root,'package.json'),pkg=JSON.parse(fs.readFileSync(file,'utf8'));
  pkg.build??={};pkg.build.nsisWeb={script:'different.nsi'};fs.writeFileSync(file,JSON.stringify(pkg));
  failureUnchanged(root,/ERR_WEB_CUSTOM_SCRIPT/);
});
test('백업 원본 변조와 다른 영수증을 이미 적용된 것으로 받아들이지 않는다',t=>{
  const root=fixture(t);apply(root);fs.appendFileSync(path.join(root,CACHE,'originals',TARGETS[0].file),' ');
  failureUnchanged(root,/ERR_WEB_ORIGINAL_HASH/);
  const second=fixture(t);apply(second);fs.appendFileSync(path.join(second,CACHE,'applied.json'),' ');
  failureUnchanged(second,/ERR_WEB_RECEIPT_DRIFT/);
});
test('다운로드·복사·해시 검증이 기존 앱 종료와 제거보다 앞선다',()=>{
  const section=transform(TARGETS[1].file,original(TARGETS[1])).toString('utf8').replaceAll('\r\n','\n');
  const preflight=section.indexOf('!insertmacro gukgamPrepareWebPackage');
  assert(preflight>section.indexOf('InitPluginsDir'));
  assert(preflight<section.indexOf('!insertmacro CHECK_APP_RUNNING'));
  assert(preflight<section.indexOf('!insertmacro uninstallOldVersion'));
  assert(section.includes('!ifdef APP_PACKAGE_URL\n  !insertmacro gukgamPrepareWebPackage\n!endif'));
});
test('인접 파일·명시 파일·원격 파일 모두 소유 사본 검증 후에만 추출한다',()=>{
  const installer=transform(TARGETS[0].file,original(TARGETS[0])).toString('utf8').replaceAll('\r\n','\n');
  const prep=installer.slice(installer.indexOf('!macro gukgamPrepareWebPackage'),installer.indexOf('!macro installApplicationFiles'));
  assert(prep.includes('${StdUtils.GetParameter} $packageFile "package-file" ""'));
  assert(prep.includes('StrCpy $packageFile "$EXEDIR\\${APP_64_NAME}"'));
  assert(prep.includes('!insertmacro downloadApplicationFiles'));
  const copy=prep.indexOf('CopyFiles /SILENT /FILESONLY "$packageFile" "$PLUGINSDIR\\gukgam-verified-package.7z"');
  const verify=prep.indexOf('!insertmacro gukgamVerifyWebPackage');
  const ready=prep.indexOf('StrCpy $gukgamWebPackagePrepared "1"');
  assert(copy>0&&copy<verify&&verify<ready);
  const install=installer.slice(installer.indexOf('!macro installApplicationFiles'));
  assert(install.indexOf('$gukgamWebPackagePrepared != "1"')<install.indexOf('!insertmacro gukgamVerifyWebPackage'));
  assert(install.indexOf('!insertmacro gukgamVerifyWebPackage')<install.indexOf('!insertmacro extractUsing7za "$packageFile"'));
  assert(!installer.includes('Goto fun_extract'));assert(!installer.includes('isPackageFileExplicitlySpecified'));
  assert(installer.includes('${StdUtils.HashFile} $3 "SHA2-512" "$packageFile"'));
  assert(installer.includes('$3 != $gukgamExpectedPackageHash'));
  assert(installer.includes('SetErrorLevel 2\n    Quit'));
});
test('고정 x64 SHA가 없는 구성은 NSIS 컴파일 단계부터 실패하도록 한다',()=>{
  const installer=transform(TARGETS[0].file,original(TARGETS[0])).toString('utf8').replaceAll('\r\n','\n');
  for(const name of ['APP_64_NAME','APP_64_HASH'])assert(installer.includes('!ifndef '+name+'\n    !error'));
  for(const name of ['APP_32_NAME','APP_ARM64_NAME'])assert(installer.includes('!ifdef '+name+'\n    !error'));
  assert(installer.includes('StrCpy $gukgamExpectedPackageHash "${APP_64_HASH}"'));
  assert(!installer.includes('/NOCHECK'));
});
test('인터넷 설치 뒤 압축 자료를 영구 캐시에 옮기지 않는다',()=>{
  const installer=transform(TARGETS[0].file,original(TARGETS[0])).toString('utf8').replaceAll('\r\n','\n');
  assert(!installer.includes('!insertmacro moveFile'));
  assert(!installer.includes('APP_PACKAGE_STORE_FILE'));
  const branch=installer.slice(installer.indexOf('!macro installApplicationFiles'),installer.indexOf('    !else\n      !insertmacro extractEmbeddedAppPackage'));
  assert(!branch.includes('SetShellVarContext'));
  assert(branch.includes('!insertmacro gukgamVerifyWebPackage\n      !insertmacro extractUsing7za "$packageFile"'));
});
