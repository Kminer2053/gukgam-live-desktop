"""고정된 Windows CPU 화자분리 자산만 준비하며 실제 모델은 작업자가 검증한다."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

import httpx

from ..assets_download import download_file, verified_file
from ..secure_files import make_private_directory, open_regular_file
from .nemotron_cpu import RUNTIME_ARCHIVE, RUNTIME_BYTES, RUNTIME_SHA256, RUNTIME_TAG, NemotronContractError
from .nemotron_worker import VerifiedAssets, _open_read_locked, _hash

# NVIDIA v0.2.0의 models/index.json 및 고정 HF Git LFS 포인터와 대조한 값이다.
MODEL_REPO = "nvidia/Nemotron-3-Diarization"
MODEL_REVISION = "f667ed73aee57d40cc39428eb768b4fd87a0a29e"
MODEL_FILE = "Nemotron-3-Diarization.q8_0.gguf"
MODEL_BYTES = 107_012_128
MODEL_SHA256 = "08456d9e22cd9a323c0364d98375f3746d6e68507ebb705cd46438c534c7a3a1"
MODEL_LICENSE = "OpenMDW-1.1"
FILES = (
    (Path("runtimes") / RUNTIME_ARCHIVE, RUNTIME_BYTES, RUNTIME_SHA256,
     f"https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/{RUNTIME_TAG}/{RUNTIME_ARCHIVE}"),
    (Path("models/nemotron3-diarization") / MODEL_FILE, MODEL_BYTES, MODEL_SHA256,
     f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"),
)
TOTAL_BYTES = RUNTIME_BYTES + MODEL_BYTES
_STATUS = {}


def bundled_directory():
    """동봉 engine 실행물의 형제 폴더만 사용한다. 사용자 설정을 검색하지 않는다."""
    return Path(sys.executable).absolute().parent.parent / "nemotron-assets" if getattr(sys, "frozen", False) else None


def reserve(data_dir):
    key = str(Path(data_dir).absolute())
    if _STATUS.get(key, {}).get("state") == "preparing":
        return False
    _STATUS[key] = {"state": "preparing", "preparing": True, "bytes": 0, "progress": 0}
    return True


def mark_cancelled(data_dir):
    _STATUS[str(Path(data_dir).absolute())] = {"state": "cancelled", "preparing": False,
                                             "message": "화자분리 모델 준비를 취소했습니다"}


def paths(root):
    root = Path(root).absolute()
    return root / FILES[0][0], root / "runtimes" / RUNTIME_ARCHIVE.removesuffix(".zip"), root / FILES[1][0]


def _file_status(data_dir, *, bundled_dir=None):
    """설정 화면에서는 크기만 확인한다. 추론 성공·화자 정확도를 뜻하지 않는다."""
    bundled_dir = bundled_directory() if bundled_dir is None else bundled_dir
    for root, origin in ((bundled_dir, "bundled"), (data_dir, "prepared")):
        if root is None:
            continue
        archive, runtime, model = paths(root)
        try:
            present = (archive.is_file() and archive.stat().st_size == RUNTIME_BYTES
                       and model.is_file() and model.stat().st_size == MODEL_BYTES
                       and (runtime / "bin/nemo_speech_asr_c.dll").is_file())
        except OSError:
            present = False
        if present:
            return {"state": "idle", "files_present": True, "asset_origin": origin,
                    "model": MODEL_REPO, "model_revision": MODEL_REVISION,
                    "license": MODEL_LICENSE, "total_bytes": TOTAL_BYTES,
                    "installed_inference_verified": False}
    return {"state": "missing", "files_present": False, "asset_origin": None,
            "model": MODEL_REPO, "model_revision": MODEL_REVISION,
            "license": MODEL_LICENSE, "total_bytes": TOTAL_BYTES,
            "installed_inference_verified": False}


def status(data_dir, *, bundled_dir=None):
    result = _file_status(data_dir, bundled_dir=bundled_dir)
    previous = _STATUS.get(str(Path(data_dir).absolute()), {})
    if previous.get("state") == "idle" and not result["files_present"]:
        previous = {}
    return {**result, "preparing": False, **previous}


def asset_root(data_dir, bundled_dir=None):
    bundled_dir = bundled_directory() if bundled_dir is None else bundled_dir
    state = status(data_dir, bundled_dir=bundled_dir)
    if not state["files_present"]:
        raise NemotronContractError("diarization_assets_missing")
    return Path(bundled_dir if state["asset_origin"] == "bundled" else data_dir)


def verify_assets(root, *, _runtime_bytes=RUNTIME_BYTES, _runtime_sha=RUNTIME_SHA256,
                  _model_bytes=MODEL_BYTES, _model_sha=MODEL_SHA256):
    """고정 ZIP·DLL·모델을 같은 보호 핸들로 검증하고 네이티브 해제까지 유지한다."""
    archive, runtime, model = paths(root)
    opened = []

    def locked(path):
        # 부모 교체와 leaf 쓰기/삭제를 각각 막고 두 핸들이 같은 파일인지 확인한다.
        guard = open_regular_file(path)
        opened.append(guard)
        stream = _open_read_locked(path)
        opened.append(stream)
        first, second = os.fstat(guard.fileno()), os.fstat(stream.fileno())
        if (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino):
            raise NemotronContractError("diarization_asset_identity_changed")
        return stream

    try:
        original = locked(archive)
        if os.fstat(original.fileno()).st_size != _runtime_bytes or _hash(original) != _runtime_sha:
            raise NemotronContractError("runtime_archive_hash_mismatch")
        seen, dlls, total = set(), set(), 0
        with zipfile.ZipFile(original) as bundle:
            if len(bundle.infolist()) > 2000:
                raise NemotronContractError("invalid_runtime_archive_layout")
            for entry in bundle.infolist():
                name, parts = entry.orig_filename, PurePosixPath(entry.orig_filename).parts
                total += entry.file_size
                if (not parts or parts[0] != runtime.name or ".." in parts or "\\" in name
                        or ":" in name or "\x00" in name or name.startswith("/")
                        or name.rstrip("/") != "/".join(parts) or "/".join(parts).casefold() in seen
                        or any(p.endswith((".", " ")) for p in parts)
                        or stat.S_ISLNK(entry.external_attr >> 16) or total > 128 * 1024 * 1024):
                    raise NemotronContractError("invalid_runtime_archive_layout")
                seen.add("/".join(parts).casefold())
                if entry.is_dir() or not name.lower().endswith((".dll", ".exe")):
                    continue
                installed = runtime.joinpath(*parts[1:])
                stream = locked(installed)
                with bundle.open(entry) as packaged:
                    if os.fstat(stream.fileno()).st_size != entry.file_size or _hash(stream) != _hash(packaged):
                        raise NemotronContractError("runtime_member_hash_mismatch")
                if installed.parent == runtime / "bin" and installed.suffix.lower() == ".dll":
                    dlls.add(installed.name.lower())
        if ({p.name.lower() for p in (runtime / "bin").iterdir() if p.suffix.lower() == ".dll"} != dlls
                or "nemo_speech_asr_c.dll" not in dlls):
            raise NemotronContractError("runtime_dll_set_mismatch")
        source = locked(model)
        if os.fstat(source.fileno()).st_size != _model_bytes or _hash(source) != _model_sha:
            raise NemotronContractError("diarization_model_hash_mismatch")
        return VerifiedAssets(runtime / "bin", model, opened)
    except BaseException:
        for stream in reversed(opened):
            stream.close()
        raise


async def _prepare(data_dir, *, confirm=False, accept_license=False, bundled_dir=None):
    """명시 준비만 다운로드한다. 동봉 자산은 읽기만 하며 추론하지 않는다."""
    if confirm is not True or accept_license is not True:
        raise ValueError("화자분리 모델의 준비와 라이선스를 확인하세요")
    if status(data_dir, bundled_dir=bundled_dir)["asset_origin"] == "bundled":
        # 이전 설치의 동봉 파일도 고정 해시·DLL 구성이 일치할 때만 재사용한다.
        def verify_bundled():
            assets = verify_assets(asset_root(data_dir, bundled_dir=bundled_dir))
            assets.close()
        verification = asyncio.create_task(asyncio.to_thread(verify_bundled))
        try:
            await asyncio.shield(verification)
        except asyncio.CancelledError:
            await asyncio.gather(verification, return_exceptions=True)
            raise
        return status(data_dir, bundled_dir=bundled_dir)
    root = Path(data_dir).absolute()
    make_private_directory(root, parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < TOTAL_BYTES * 2 + 32 * 1024 * 1024:
        raise ValueError("화자분리 자산을 준비할 저장 공간이 부족합니다")
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                timeout=httpx.Timeout(120, connect=15)) as client:
        for relative, size, expected, url in FILES:
            target = root / relative
            make_private_directory(target.parent, parents=True, exist_ok=True)
            if await asyncio.to_thread(verified_file, target, size, expected):
                continue
            await download_file(client, url, target, size, expected)
    # 같은 고정 CPU SDK의 기존 안전 ZIP 검증·추출 계약을 재사용한다.
    from .nemotron_assets import _extract
    extraction = asyncio.create_task(asyncio.to_thread(_extract, root))
    try:
        await asyncio.shield(extraction)
    except asyncio.CancelledError:
        await asyncio.gather(extraction, return_exceptions=True)
        raise
    return status(root)


async def prepare(data_dir, *, confirm=True, accept_license=True, bundled_dir=None):
    """HTTP의 명시 준비·라이선스 확인 뒤 호출한다. 자동 모델 실행은 없다."""
    key = str(Path(data_dir).absolute())
    if _STATUS.get(key, {}).get("state") != "preparing":
        reserve(data_dir)
    try:
        await _prepare(data_dir, confirm=confirm, accept_license=accept_license, bundled_dir=bundled_dir)
        _STATUS[key] = {"state": "idle", "preparing": False, "bytes": TOTAL_BYTES, "progress": 1}
        return status(data_dir, bundled_dir=bundled_dir)
    except asyncio.CancelledError:
        mark_cancelled(data_dir)
        raise
    except Exception:
        _STATUS[key] = {"state": "failed", "preparing": False,
                        "message": "화자분리 모델 준비를 완료하지 못했습니다"}
        raise
