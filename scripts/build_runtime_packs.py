"""검증된 엔진·고정 릴레이 의존성을 계정·기록 없는 선택 다운로드 묶음으로 만든다."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
RELEASE='https://github.com/Kminer2053/gukgam-live-desktop/releases/download/'

def guard(path:Path):
    for item in [path,*path.parents]:
        if item.is_symlink() or item.is_junction():raise ValueError('linked_build_input')

def safe_name(value:str)->bool:
    if not value or len(value)>1024 or value!=unicodedata.normalize('NFC',value) or value.startswith('/') or re.search(r'[\\:\x00-\x1f\x7f]',value):return False
    return all(p and p not in {'.','..'} and not p.endswith(('.',' ')) and not p[0].isspace() and not re.search(r'[<>"|?*]',p)
               and not re.match(r'^(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(\.|$)',p,re.I) for p in value.split('/'))

def public_build_file(name:str)->bool:
    parts=PurePosixPath(name).parts
    return (not any(x.casefold() in {'records','uploads','account','.vercel','.git'} for x in parts)
            and not any(x.casefold().startswith('.env') for x in parts)
            and parts[-1].casefold() not in {'vault.json','auth.json','tokens.json','settings.json'}
            and PurePosixPath(name).suffix.casefold() not in {'.db','.sqlite','.sqlite3','.log','.wav','.mp3'})

def owned_dist_path(raw:Path)->Path:
    assert '..' not in raw.parts,'parent_path_not_allowed'
    guard(raw.absolute());p=raw.resolve()
    assert p.is_relative_to((ROOT/'dist').resolve()),'build_paths_must_be_owned'
    return p

def sha(path:Path):
    guard(path)
    with path.open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()

def scan(folder:Path,exclude:set[str])->list[tuple[str,Path]]:
    guard(folder);assert folder.is_dir()
    result=[];seen=set()
    for path in sorted(folder.rglob('*')):
        guard(path)
        if not path.is_file():continue
        relative=path.relative_to(folder).as_posix()
        if relative in exclude or '.cache' in path.relative_to(folder).parts or path.name=='.gitkeep':continue
        assert safe_name(relative) and public_build_file(relative) and relative.casefold() not in seen,'unsafe_or_private_build_path'
        assert path.stat().st_nlink==1,'hardlinked_build_input'
        seen.add(relative.casefold());result.append((relative,path))
    return result

def create_pack(output:Path,name:str,files:list[tuple[str,Path]],identity:str,version:str,entry:str)->dict:
    archive=output/name;guard(archive);assert not archive.exists()
    rows=[{'path':relative,'bytes':p.stat().st_size,'sha256':sha(p)} for relative,p in files]
    assert rows and any(x['path']==entry for x in rows)
    with archive.open('xb') as destination,gzip.GzipFile(filename='',mode='wb',fileobj=destination,mtime=0,compresslevel=9) as zipped,tarfile.open(fileobj=zipped,mode='w',format=tarfile.PAX_FORMAT) as bundle:
        for row,(_,path) in zip(rows,files,strict=True):
            info=tarfile.TarInfo(row['path']);info.size=row['bytes'];info.mode=0o600;info.mtime=0;info.uid=info.gid=0
            with path.open('rb') as source:bundle.addfile(info,source)
            assert path.stat().st_size==row['bytes'] and sha(path)==row['sha256'],'build_input_changed'
    return {'id':identity,'version':version,'url':RELEASE+'v'+version+'/'+name,'bytes':archive.stat().st_size,'sha256':sha(archive),'unpackedBytes':sum(x['bytes'] for x in rows),'entryPoint':entry,'files':rows}

def reuse_engine(folder:Path,output:Path,version:str,files:list[tuple[str,Path]])->dict:
    folder=owned_dist_path(folder)
    report=folder/'runtime-packs.json';guard(report);assert report.stat().st_size<5_000_000
    specs=json.loads(report.read_bytes())['catalog'];matches=[x for x in specs if x['id']=='engine'];assert len(matches)==1
    spec=matches[0];name=f'Gukgam-Engine-win-x64-{version}.tar.gz'
    assert spec['version']==version and spec['entryPoint']=='gukgam-engine.exe' and spec['url']==RELEASE+'v'+version+'/'+name
    assert spec['files']==[{'path':name,'bytes':path.stat().st_size,'sha256':sha(path)} for name,path in files],'engine_input_changed'
    old=folder/name;new=output/name
    assert old.stat().st_size==spec['bytes'] and sha(old)==spec['sha256'] and not new.exists()
    shutil.copyfile(old,new);assert sha(new)==spec['sha256']
    return spec

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist/runtime-packs')
    parser.add_argument('--engine-root',type=Path,default=ROOT/'dist/engine/gukgam-engine')
    parser.add_argument('--reuse-engine-from',type=Path,help='같은 엔진 파일을 검증한 기존 소유 빌드 묶음')
    options=parser.parse_args()
    output=owned_dist_path(options.output);engine=owned_dist_path(options.engine_root)
    assert not output.exists(),'new_output_directory_required'
    package=json.loads((ROOT/'desktop/package.json').read_bytes());version=package['version'];assert re.fullmatch(r'\d+\.\d+\.\d+',version)
    relay=ROOT/'desktop/relay-cli'
    declared=json.loads((relay/'package.json').read_bytes());lock=json.loads((relay/'package-lock.json').read_bytes())
    assert declared['dependencies']['vercel']=='50.0.0' and lock['packages']['node_modules/vercel']['version']=='50.0.0'
    assert not any(re.search(r'(?:^|/)node_modules/(?:@vercel/)?sandbox$',x) for x in lock['packages'])
    applied=json.loads((relay/'node_modules/.cache/gukgam-relay-cli-50-compat/applied.json').read_bytes())
    expected={'node_modules/@ts-morph/common/dist/ts-morph-common.js':'d767c2b1f8162516aa8ad0f06ca0236615b2ad21d8feeccc4ce31653bb31577c',
              'node_modules/vercel/dist/index.js':'92836e47cb271b5e9e07056f469cf0b40ab22ae1b78b6bcf10f66fedfbdf4927'}
    assert applied.get('kind')=='vercel_50_minimatch_and_project_id_compat' and applied.get('versions')=={'vercel':'50.0.0','@ts-morph/common':'0.11.1','minimatch':'11.0.2'}
    assert len(applied['targets'])==len(expected) and {x['file']:x['patched_sha256'] for x in applied['targets']}==expected,'cli_compatibility_receipt'
    for name,digest in expected.items():assert sha(relay/name)==digest,'cli_compatibility_not_applied'
    for package,info in lock['packages'].items():
        if not package:continue
        meta=relay/package/'package.json'
        if meta.exists():assert json.loads(meta.read_bytes())['version']==info['version'],'installed_dependency_version_mismatch'
        else:assert info.get('optional'),'required_dependency_missing'
    exclusions=json.loads((relay/'distribution-exclusions.json').read_bytes())
    assert isinstance(exclusions,list) and len(exclusions)==len(set(exclusions))
    for name in exclusions:assert safe_name(name) and (relay/'node_modules'/name).is_file()
    output.mkdir(parents=True)
    engine_files=scan(engine,set())
    cli_files=[('package.json',relay/'package.json'),('package-lock.json',relay/'package-lock.json')]
    cli_files += [('node_modules/'+name,path) for name,path in scan(relay/'node_modules',set(exclusions)) if name!='.package-lock.json']
    # 릴레이 화면·서버 템플릿도 중계를 선택하기 전에는 받지 않는다.
    if __package__:
        from .build_cloud import _copies
    else:
        from build_cloud import _copies
    template_names=set(_copies())|{'index.py','requirements.txt','.python-version','vercel.json','.vercelignore'}
    for name in sorted(template_names):
        path=ROOT/'cloud'/name;guard(path);assert safe_name(name) and public_build_file(name) and path.is_file()
        cli_files.append(('template/'+name,path))
    cli_files.append(('template/LICENSE.txt',ROOT/'LICENSE'))
    specs=[reuse_engine(options.reuse_engine_from,output,version,engine_files) if options.reuse_engine_from else create_pack(output,f'Gukgam-Engine-win-x64-{version}.tar.gz',engine_files,'engine',version,'gukgam-engine.exe'),
           create_pack(output,f'Gukgam-Relay-Tools-win-x64-{version}.tar.gz',cli_files,'relay-tools',version,'node_modules/vercel/dist/vc.js')]
    # 카탈로그는 네트워크에서 갱신하지 않는다. 신뢰한 앱 빌드에 URL·판본·전체 목록을 고정한다.
    catalog=ROOT/'desktop/src/runtime-packs/catalog.generated.ts'
    catalog.write_text("// scripts/build_runtime_packs.py가 생성한 고정 다운로드 목록. 임의 URL을 받지 않는다.\nimport type {RuntimePackSpec} from './contracts';\nexport const PACK_CATALOG:readonly RuntimePackSpec[]="+json.dumps(specs,ensure_ascii=False,separators=(',',':'))+';\n',encoding='utf-8',newline='\n')
    report={'version':version,'catalog':specs,'operating_records_included':False,'credentials_included':False,'npm_executed_on_user_pc':False}
    (output/'runtime-packs.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'version':version,'packs':[{'id':s['id'],'bytes':s['bytes'],'files':len(s['files']),'sha256':s['sha256']} for s in specs]}))

if __name__=='__main__':main()
