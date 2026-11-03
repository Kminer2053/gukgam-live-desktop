'use strict';

// 고정된 공식 NSIS 템플릿의 인터넷 설치 경로만 보완한다. 의존성과 라이선스는 그대로 둔다.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const VERSION = '26.18.0';
const CACHE = 'node_modules/.cache/gukgam-web-installer-security';
const TARGETS = [
  {file:'templates/nsis/include/installer.nsh',old:'0e319437dd01dcbf911f3f48f664fde0cefbaef704f1cdb1739f63d563f5d4a0',patched:'f6eb3bd790d4afda063185e2d24180dec2f98cd3951999775f45996f48b2d6a9'},
  {file:'templates/nsis/installSection.nsh',old:'74c4dbb4163cdd90ca4850b75d304e916235c882d6af7b5cdad63bffbc977072',patched:'6700f9707ee04bd8c59ea522936c6791832f995ba90c4a9e3b8cbf7a741cb48b'},
];
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const demand = (value,code) => {if(!value)throw new Error(code);};
const packagePrefix = 'node_modules/app-builder-lib/';

const macros = [
  '# 내려받기와 검증은 기존 앱 종료·제거보다 먼저 끝낸다.',
  '!macro gukgamVerifyWebPackage',
  '  ${if} $packageFile != "$PLUGINSDIR\\gukgam-verified-package.7z"',
  '    SetErrorLevel 2',
  '    Quit',
  '  ${endif}',
  '  ${StdUtils.HashFile} $3 "SHA2-512" "$packageFile"',
  '  ${if} $3 != $gukgamExpectedPackageHash',
  '    MessageBox MB_OK|MB_ICONSTOP "설치 자료의 무결성을 확인하지 못했습니다. 공식 설치 파일을 다시 받아 주세요." /SD IDOK',
  '    SetErrorLevel 2',
  '    Quit',
  '  ${endif}',
  '!macroend',
  '',
  '!macro gukgamPrepareWebPackage',
  '  # 현재 제품의 고정 x64 패키지만 허용한다. 다른 구성은 빌드 단계에서 중단한다.',
  '  !ifndef APP_64_NAME',
  '    !error "국감 웹 설치에는 고정 x64 패키지가 필요합니다."',
  '  !endif',
  '  !ifndef APP_64_HASH',
  '    !error "국감 웹 설치에는 SHA512가 필요합니다."',
  '  !endif',
  '  !ifdef APP_32_NAME',
  '    !error "국감 웹 설치는 현재 x64 패키지만 지원합니다."',
  '  !endif',
  '  !ifdef APP_ARM64_NAME',
  '    !error "국감 웹 설치는 현재 x64 패키지만 지원합니다."',
  '  !endif',
  '  Var /GLOBAL packageFile',
  '  Var /GLOBAL gukgamExpectedPackageHash',
  '  Var /GLOBAL gukgamWebPackagePrepared',
  '  StrCpy $gukgamWebPackagePrepared "0"',
  '  StrCpy $gukgamExpectedPackageHash "${APP_64_HASH}"',
  '  ${StdUtils.GetParameter} $packageFile "package-file" ""',
  '  ${if} $packageFile == ""',
  '    StrCpy $packageFile "$EXEDIR\\${APP_64_NAME}"',
  '    ${ifNot} ${FileExists} "$packageFile"',
  '      !insertmacro downloadApplicationFiles',
  '    ${endif}',
  '  ${endif}',
  '  # 외부에서 지정한 원본 파일을 옮기거나 직접 압축 해제하지 않는다.',
  '  ClearErrors',
  '  CopyFiles /SILENT /FILESONLY "$packageFile" "$PLUGINSDIR\\gukgam-verified-package.7z"',
  '  ${if} ${Errors}',
  '    MessageBox MB_OK|MB_ICONSTOP "설치 자료를 준비하지 못했습니다. 저장 공간과 파일 접근 권한을 확인해 주세요." /SD IDOK',
  '    SetErrorLevel 2',
  '    Quit',
  '  ${endif}',
  '  StrCpy $packageFile "$PLUGINSDIR\\gukgam-verified-package.7z"',
  '  !insertmacro gukgamVerifyWebPackage',
  '  StrCpy $gukgamWebPackagePrepared "1"',
  '!macroend',
  '',
].join('\n');
const webBranch = [
  '    !ifdef APP_PACKAGE_URL',
  '      ${if} $gukgamWebPackagePrepared != "1"',
  '        SetErrorLevel 2',
  '        Quit',
  '      ${endif}',
  '      # 설치 중에도 압축 해제 직전 같은 소유 파일의 해시를 다시 확인한다.',
  '      !insertmacro gukgamVerifyWebPackage',
  '      !insertmacro extractUsing7za "$packageFile"',
].join('\n')+'\n';

function transform(file,raw) {
  const original=raw.toString('utf8');
  demand(Buffer.from(original,'utf8').equals(raw),'ERR_WEB_UTF8');
  const lf=original.replaceAll('\r\n','\n');
  demand(!lf.includes('\r'),'ERR_WEB_NEWLINE');
  let updated;
  if(file===TARGETS[0].file) {
    const start='    !ifdef APP_PACKAGE_URL\n';
    const end='    !else\n      !insertmacro extractEmbeddedAppPackage';
    demand(lf.split(start).length===2&&lf.split(end).length===2,'ERR_WEB_INSTALLER_ANCHOR');
    const a=lf.indexOf(start),b=lf.indexOf(end,a);
    demand(b>a,'ERR_WEB_INSTALLER_ORDER');
    updated=lf.slice(0,a)+webBranch+lf.slice(b);
    const macro='!macro installApplicationFiles\n';
    demand(updated.split(macro).length===2,'ERR_WEB_MACRO_ANCHOR');
    updated=updated.replace(macro,macros+'\n'+macro);
  } else {
    const anchor='InitPluginsDir\n';
    demand(lf.split(anchor).length===2,'ERR_WEB_SECTION_ANCHOR');
    updated=lf.replace(anchor,anchor+'\n!ifdef APP_PACKAGE_URL\n  !insertmacro gukgamPrepareWebPackage\n!endif\n');
  }
  return Buffer.from(original.includes('\r\n')?updated.replaceAll('\n','\r\n'):updated,'utf8');
}
function same(a,b) {return process.platform==='win32'?a.toLowerCase()===b.toLowerCase():a===b;}
function unlinked(file) {
  for(let current=path.resolve(file);;current=path.dirname(current)) {
    try {demand(!fs.lstatSync(current).isSymbolicLink(),'ERR_WEB_LINKED_PATH');}
    catch(error) {if(error.code!=='ENOENT')throw error;}
    if(current===path.dirname(current))break;
  }
}
function apply(root) {
  demand(Number(process.versions.node.split('.')[0])>=24,'ERR_WEB_NODE_VERSION');
  root=path.resolve(root);unlinked(root);demand(same(fs.realpathSync(root),root),'ERR_WEB_ROOT_IDENTITY');
  const file=relative=>{demand(!path.isAbsolute(relative)&&!relative.split('/').includes('..'),'ERR_WEB_PATH');const full=path.join(root,...relative.split('/'));unlinked(full);return full;};
  const read=relative=>{const full=file(relative);demand(fs.lstatSync(full).isFile(),'ERR_WEB_NOT_FILE');return fs.readFileSync(full);};
  const manifest=JSON.parse(read('package.json'));
  demand(manifest.devDependencies?.['electron-builder']===VERSION,'ERR_WEB_BUILDER_DECLARATION');
  const lock=JSON.parse(read('package-lock.json'));
  demand(lock.packages?.['node_modules/electron-builder']?.version===VERSION
    &&lock.packages?.['node_modules/app-builder-lib']?.version===VERSION,'ERR_WEB_BUILDER_LOCK');
  demand(!manifest.build?.nsis?.script&&!manifest.build?.nsisWeb?.script,'ERR_WEB_CUSTOM_SCRIPT');
  const meta=JSON.parse(read(packagePrefix+'package.json'));
  demand(meta.name==='app-builder-lib'&&meta.version===VERSION,'ERR_WEB_BUILDER_VERSION');
  const immutable=['package.json','package-lock.json',packagePrefix+'package.json'];
  for(const leaf of ['LICENSE','LICENSE.md','LICENSE.txt'])if(fs.existsSync(file(packagePrefix+leaf)))immutable.push(packagePrefix+leaf);
  const unchanged=immutable.map(relative=>({file:relative,sha256:sha(read(relative))}));
  const plans=TARGETS.map(target=>{
    const current=read(packagePrefix+target.file),hash=sha(current);
    demand(hash===target.old||hash===target.patched,'ERR_WEB_TEMPLATE_HASH:'+target.file);
    const done=hash===target.patched;
    const original=done?read(CACHE+'/originals/'+target.file):current;
    demand(sha(original)===target.old,'ERR_WEB_ORIGINAL_HASH');
    const desired=transform(target.file,original);
    demand(sha(desired)===target.patched,'ERR_WEB_PATCH_HASH');
    return {...target,done,original,desired};
  });
  const receipt={schema_version:1,kind:'nsis_web_preflight_owned_sha512',app_builder_lib:VERSION,
    targets:TARGETS.map(t=>({file:t.file,original_sha256:t.old,patched_sha256:t.patched})),
    supported_architecture:'x64',download_before_old_app_stop:true,verify_before_extract:true,
    full_install_rollback_guaranteed:false,keep_install_archive:false,package_lock_or_license_changed:false};
  const receiptData=Buffer.from(JSON.stringify(receipt,null,2)+'\n'),receiptPath=CACHE+'/applied.json';
  const writeNew=(relative,data)=>{const full=file(relative);fs.mkdirSync(path.dirname(full),{recursive:true});if(fs.existsSync(full)){demand(read(relative).equals(data),'ERR_WEB_EVIDENCE_DRIFT');return;}fs.writeFileSync(full,data,{flag:'wx'});};
  if(fs.existsSync(file(receiptPath))) {
    demand(plans.every(p=>p.done)&&read(receiptPath).equals(receiptData),'ERR_WEB_RECEIPT_DRIFT');
  } else {
    // 모든 입력 검증을 끝낸 뒤에만 원본 보관과 파일 교체를 시작한다.
    for(const plan of plans)writeNew(CACHE+'/originals/'+plan.file,plan.original);
    for(const plan of plans)if(!plan.done) {
      const relative=packagePrefix+plan.file,tmp=relative+'.gukgam-web.tmp';
      demand(!fs.existsSync(file(tmp)),'ERR_WEB_TEMP_EXISTS');
      fs.writeFileSync(file(tmp),plan.desired,{flag:'wx'});fs.renameSync(file(tmp),file(relative));
    }
    writeNew(receiptPath,receiptData);
  }
  for(const plan of plans)demand(sha(read(packagePrefix+plan.file))===plan.patched,'ERR_WEB_FINAL_HASH');
  for(const item of unchanged)demand(sha(read(item.file))===item.sha256,'ERR_WEB_METADATA_CHANGED');
  return {ok:true,already_applied:plans.every(p=>p.done),patched_this_run:plans.filter(p=>!p.done).length,targets:receipt.targets};
}
module.exports={VERSION,TARGETS,CACHE,transform,apply,sha};
if(require.main===module) {
  try {demand(process.argv.length===2,'ERR_WEB_ARGUMENTS');console.log(JSON.stringify(apply(path.resolve(__dirname,'..'))));}
  catch(error) {console.error(error instanceof Error?error.message:'ERR_WEB_SECURITY');process.exitCode=1;}
}
