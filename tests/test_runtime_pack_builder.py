from pathlib import Path
import io
import json
import tarfile

import pytest

from scripts import build_runtime_packs as builder


def test_reject_parent_escape_and_linked_build_paths(tmp_path,monkeypatch):
    monkeypatch.setattr(builder,'ROOT',tmp_path)
    (tmp_path/'dist').mkdir()
    for value in [tmp_path/'dist/../../escape',tmp_path/'dist/../wrong',tmp_path/'outside']:
        with pytest.raises(AssertionError):builder.owned_dist_path(value)
    assert builder.owned_dist_path(tmp_path/'dist/new')==tmp_path/'dist/new'


def test_windows_names_and_operating_data_are_not_pack_inputs(tmp_path):
    for name in ['../escape','CONIN$','a/COM¹.txt','LPT²','a\\b','data:stream']:
        assert not builder.safe_name(name)
    for name in ['.env','a/.env.private','records/input.txt','auth.json','nested/vault.json','a/meeting.sqlite','uploads/note.txt']:
        assert not builder.public_build_file(name)
    for name in ['_internal/tzdata/zoneinfo/Asia/Seoul','node_modules/vercel/dist/vc.js','LICENSE']:
        assert builder.safe_name(name) and builder.public_build_file(name)
    (tmp_path/'auth.json').write_text('synthetic-secret')
    with pytest.raises(AssertionError):builder.scan(tmp_path,set())


def test_archive_and_catalog_pin_the_same_bytes_without_private_metadata(tmp_path):
    src=tmp_path/'src';src.mkdir();(src/'gukgam-engine.exe').write_bytes(b'no executable code')
    out=tmp_path/'packs';out.mkdir()
    spec=builder.create_pack(out,'Gukgam-Engine-win-x64-0.2.0.tar.gz',builder.scan(src,set()),'engine','0.2.0','gukgam-engine.exe')
    archive=out/'Gukgam-Engine-win-x64-0.2.0.tar.gz'
    assert spec['sha256']==builder.sha(archive) and spec['bytes']==archive.stat().st_size
    assert spec['unpackedBytes']==len(b'no executable code')
    with tarfile.open(archive,'r:gz') as bundle:
        members=bundle.getmembers();assert len(members)==1 and members[0].isfile()
        assert members[0].name=='gukgam-engine.exe' and members[0].mtime==0 and members[0].uid==0
        assert bundle.extractfile(members[0]).read()==b'no executable code'
    assert str(tmp_path) not in json.dumps(spec)


@pytest.mark.parametrize('changed',[False,True])
def test_reuse_checks_existing_archive_and_current_engine_input(tmp_path,monkeypatch,changed):
    monkeypatch.setattr(builder,'ROOT',tmp_path)
    root=tmp_path/'dist';root.mkdir();source=root/'engine';source.mkdir()
    exe=source/'gukgam-engine.exe';exe.write_bytes(b'original synthetic engine')
    old=root/'old';old.mkdir();new=root/'new';new.mkdir();files=builder.scan(source,set())
    spec=builder.create_pack(old,'Gukgam-Engine-win-x64-0.2.0.tar.gz',files,'engine','0.2.0','gukgam-engine.exe')
    (old/'runtime-packs.json').write_text(json.dumps({'catalog':[spec]}))
    if changed:
        exe.write_bytes(b'modified synthetic engine')
        with pytest.raises(AssertionError):builder.reuse_engine(old,new,'0.2.0',builder.scan(source,set()))
        assert list(new.iterdir())==[]
    else:
        result=builder.reuse_engine(old,new,'0.2.0',files)
        assert result==spec and builder.sha(new/'Gukgam-Engine-win-x64-0.2.0.tar.gz')==spec['sha256']
