'use strict';

// 공개된 고정 CLI의 실제 API 호환 보완이다. 패키지 선언·lock·이름·버전은 바꾸지 않는다.
// 원본 Apache-2.0/MIT 라이선스와 변경 전 바이트를 보존한다.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');
const ROOT = path.resolve(__dirname, '..');
const VERSIONS = {vercel:'50.0.0','@ts-morph/common':'0.11.1',minimatch:'11.0.2'};
const TARGETS = [
  {file:'node_modules/@ts-morph/common/dist/ts-morph-common.js',
   old:'3184b1f03ea81d1ba8fafc5fb67663dc0e2dded6bc5d77dc185815e5900050fa',
   patched:'d767c2b1f8162516aa8ad0f06ca0236615b2ad21d8feeccc4ce31653bb31577c',
   before:'var minimatch__default = /*#__PURE__*/_interopDefaultLegacy(minimatch);',
   after:"var minimatch__default = /*#__PURE__*/_interopDefaultLegacy(typeof minimatch === 'function' ? minimatch : minimatch.minimatch);"},
  {file:'node_modules/vercel/dist/index.js',
   old:'826642e4444ecbecd85cc223144a579ebcaa2fab30509b27cbecfb289e7ad1e9',
   patched:'92836e47cb271b5e9e07056f469cf0b40ab22ae1b78b6bcf10f66fedfbdf4927',
   before:'        const createArgs = {\n          name,\n          env: deploymentEnv,',
   after:'        const createArgs = {\n          name,\n          project: project.id,\n          env: deploymentEnv,'},
];
const evidence='node_modules/.cache/gukgam-relay-cli-50-compat';
const demand=(value,code)=>{if(!value)throw new Error(code);};
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const same=(a,b)=>process.platform==='win32'?a.toLowerCase()===b.toLowerCase():a===b;
function unlinked(file){
  for(let current=path.resolve(file);;current=path.dirname(current)){
    try{demand(!fs.lstatSync(current).isSymbolicLink(),'ERR_RELAY_LINKED_PATH');}
    catch(error){if(error.code!=='ENOENT')throw error;}
    if(current===path.dirname(current))break;
  }
}
function under(relative){
  demand(relative&&!path.isAbsolute(relative)&&!relative.includes('\\')&&!relative.includes(':')
    &&!relative.split('/').includes('..'),'ERR_RELAY_RELATIVE_PATH');
  const file=path.resolve(ROOT,...relative.split('/'));
  demand(path.relative(ROOT,file)!==''&&!path.relative(ROOT,file).startsWith('..'+path.sep),'ERR_RELAY_PATH_ESCAPE');
  unlinked(file);return file;
}
function read(relative){const file=under(relative);demand(fs.lstatSync(file).isFile(),'ERR_RELAY_NOT_FILE');return fs.readFileSync(file);}
function writeNew(relative,data){
  const file=under(relative);fs.mkdirSync(path.dirname(file),{recursive:true});
  if(fs.existsSync(file)){demand(read(relative).equals(data),'ERR_RELAY_BACKUP_DRIFT');return;}
  fs.writeFileSync(file,data,{flag:'wx'});demand(read(relative).equals(data),'ERR_RELAY_WRITE_HASH');
}
function main(){
  const args=process.argv.slice(2);
  demand(args.length===0||args.length===2&&args[0]==='--root'&&same(path.resolve(args[1]),ROOT),'ERR_RELAY_SCRIPT_ROOT');
  demand(Number(process.versions.node.split('.')[0])>=24,'ERR_RELAY_NODE_VERSION');
  unlinked(ROOT);demand(same(fs.realpathSync(ROOT),ROOT),'ERR_RELAY_ROOT_IDENTITY');
  const manifest=JSON.parse(read('package.json')),lock=JSON.parse(read('package-lock.json'));
  demand(manifest.dependencies?.vercel===VERSIONS.vercel&&lock.packages?.['node_modules/vercel']?.version===VERSIONS.vercel,'ERR_RELAY_CLI_VERSION');
  demand(!Object.keys(lock.packages).some(name=>/(?:^|\/)node_modules\/(?:@vercel\/)?sandbox$/.test(name)),'ERR_RELAY_UNEXPECTED_SANDBOX_DEPENDENCY');
  const immutable=['package.json','package-lock.json'];
  for(const [name,version]of Object.entries(VERSIONS)){
    const relative='node_modules/'+name+'/package.json';
    const value=JSON.parse(read(relative));demand(value.name===name&&value.version===version,'ERR_RELAY_PACKAGE_VERSION:'+name);
    immutable.push(relative);
    for(const leaf of ['LICENSE','LICENSE.md','LICENSE.txt'])if(fs.existsSync(under('node_modules/'+name+'/'+leaf)))immutable.push('node_modules/'+name+'/'+leaf);
  }
  const minimatch=createRequire(under(TARGETS[0].file)).resolve('minimatch');
  demand(path.relative(ROOT,minimatch)!=='..'&&!path.relative(ROOT,minimatch).startsWith('..'+path.sep),'ERR_RELAY_MODULE_ESCAPE');unlinked(minimatch);
  const unchanged=immutable.map(file=>({file,sha256:sha(read(file))}));
  const plans=TARGETS.map(target=>{
    const current=read(target.file),hash=sha(current),text=current.toString('utf8');
    demand(hash===target.old||hash===target.patched,'ERR_RELAY_SOURCE_HASH:'+target.file);
    const done=hash===target.patched;
    demand(text.split(done?target.after:target.before).length===2,'ERR_RELAY_PATCH_ANCHOR:'+target.file);
    const original=done?Buffer.from(text.replace(target.after,target.before)):current;
    const desired=done?current:Buffer.from(text.replace(target.before,target.after));
    demand(sha(original)===target.old&&sha(desired)===target.patched,'ERR_RELAY_PATCH_RESULT:'+target.file);
    return {...target,done,original,desired};
  });
  const receipt={schema_version:2,kind:'vercel_50_minimatch_and_project_id_compat',versions:VERSIONS,
    targets:TARGETS.map(({file,old,patched})=>({file,original_sha256:old,patched_sha256:patched,changes:1})),
    dependency_names_or_versions_changed:false,dependency_lock_changed:false,licenses_changed:false,
    project_id_forwarded_to_deployment_api:true,sandbox_dependency_present:false};
  const receiptData=Buffer.from(JSON.stringify(receipt,null,2)+'\n'),receiptPath=evidence+'/applied.json';
  if(fs.existsSync(under(receiptPath))){
    demand(plans.every(p=>p.done)&&read(receiptPath).equals(receiptData),'ERR_RELAY_RECEIPT_DRIFT');
    for(const plan of plans)demand(read(evidence+'/originals/'+plan.file).equals(plan.original),'ERR_RELAY_ORIGINAL_DRIFT');
  }else{
    for(const plan of plans)writeNew(evidence+'/originals/'+plan.file,plan.original);
    for(const plan of plans)if(!plan.done){
      const file=under(plan.file),temporary=under(plan.file+'.gukgam-compat.tmp');
      demand(!fs.existsSync(temporary),'ERR_RELAY_TEMP_EXISTS');fs.writeFileSync(temporary,plan.desired,{flag:'wx'});fs.renameSync(temporary,file);
    }
    for(const plan of plans)demand(sha(read(plan.file))===plan.patched,'ERR_RELAY_FINAL_SOURCE_HASH');
    writeNew(receiptPath,receiptData);
  }
  for(const item of unchanged)demand(sha(read(item.file))===item.sha256,'ERR_RELAY_METADATA_OR_LICENSE_CHANGED');
  console.log(JSON.stringify({ok:true,already_applied:plans.every(p=>p.done),dependency_sources_patched_this_run:plans.filter(p=>!p.done).length,targets:receipt.targets}));
}
try{main();}catch(error){console.error(error instanceof Error?error.message:'ERR_RELAY_COMPAT');process.exitCode=1;}
