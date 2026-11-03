"""직원이 준비를 누른 경우만 고정된 공식 CPU 런타임·모델을 받는다."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

import httpx

from ..assets_download import AssetDownloadError, asset_response, atomic_download, download_file, verified_file
from ..secure_files import make_private_directory, open_regular_file, write_all

from .nemotron_cpu import (MODEL_REPO, MODEL_REVISION, MODEL_FILE, MODEL_BYTES,
    MODEL_SHA256, MODEL_LICENSE, RUNTIME_TAG, RUNTIME_ARCHIVE, RUNTIME_BYTES,
    RUNTIME_SHA256)

FILES = (
    (Path("runtimes") / RUNTIME_ARCHIVE, RUNTIME_BYTES, RUNTIME_SHA256,
     f"https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/{RUNTIME_TAG}/{RUNTIME_ARCHIVE}"),
    (Path("models/nemotron35-asr") / MODEL_FILE, MODEL_BYTES, MODEL_SHA256,
     f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"),
)
TOTAL_BYTES = RUNTIME_BYTES + MODEL_BYTES
MODEL_CHUNK_BYTES = 32 * 1024 * 1024
MAX_MODEL_CHUNKS = 23
MODEL_CHUNK_ATTEMPTS = 2
_STATUS: dict[str, dict] = {}


class PreparationError(RuntimeError):
    """다운로드 주소·응답 본문·사용자 경로를 포함하지 않는 안내."""

    def __init__(self, message, *, code="preparation_failed", transport_class=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.transport_class = transport_class
        self.http_status = http_status


class _IncompleteRange(Exception):
    pass


def _range_response(response, start: int, end: int, total: int) -> None:
    """검증된 범위의 원시 바이트만 받는다. 헤더 원문은 오류에 복사하지 않는다."""
    if response.status_code != 206:
        raise PreparationError("공식 음성 모델 서버의 구간 응답을 확인하지 못했습니다",
            code="range_not_supported" if response.status_code in (200, 416) else "range_http_status",
            http_status=response.status_code if type(response.status_code) is int else None)
    match = re.fullmatch(r"bytes ([0-9]{1,12})-([0-9]{1,12})/([0-9]{1,12})",
                         response.headers.get("Content-Range", ""))
    if match is None or tuple(map(int, match.groups())) != (start, end, total):
        raise PreparationError("음성 모델 구간의 위치와 전체 크기가 일치하지 않습니다",
                               code="range_metadata_mismatch", http_status=206)
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise PreparationError("음성 모델 구간의 전송 형식을 확인하지 못했습니다",
                               code="range_encoding_rejected", http_status=206)
    length = response.headers.get("Content-Length")
    if length is not None and (re.fullmatch(r"[0-9]{1,12}", length) is None or int(length) != end - start + 1):
        raise PreparationError("음성 모델 구간의 크기가 일치하지 않습니다",
                               code="range_length_mismatch", http_status=206)


async def _download_model_ranges(client, url: str, size: int, output, digest, progress) -> int:
    """최대32MiB 메모리에서 구간 전체를 검증한 뒤 파일·최종 해시에 한 번만 반영한다."""
    if not 0 < size <= MODEL_CHUNK_BYTES * MAX_MODEL_CHUNKS:
        raise PreparationError("음성 모델의 고정 크기를 확인하지 못했습니다", code="model_size_bound")
    count = 0
    for start in range(0, size, MODEL_CHUNK_BYTES):
        end = min(start + MODEL_CHUNK_BYTES, size) - 1
        expected = end - start + 1
        for attempt in range(MODEL_CHUNK_ATTEMPTS):
            payload = bytearray()
            try:
                async with asset_response(client, url, headers={"Range": f"bytes={start}-{end}",
                        "Accept-Encoding": "identity"}) as response:
                    _range_response(response, start, end, size)
                    payload = bytearray(expected)
                    offset = 0
                    async for chunk in response.aiter_raw(64 * 1024):
                        next_offset = offset + len(chunk)
                        if next_offset > expected:
                            raise PreparationError("음성 모델 구간의 크기가 검증값을 넘었습니다",
                                                   code="range_body_oversize", http_status=206)
                        payload[offset:next_offset] = chunk
                        offset = next_offset
                    if offset != expected:
                        raise _IncompleteRange()
                if count + len(payload) > size:
                    raise PreparationError("음성 모델 파일 크기가 검증값을 넘었습니다", code="model_size_bound")
                write_all(output, payload)
                digest.update(payload)
                count += len(payload)
                progress(count)
                break
            except (httpx.TransportError, httpx.StreamError, _IncompleteRange, AssetDownloadError) as error:
                if isinstance(error, AssetDownloadError) and error.code != "download_body_incomplete":
                    raise
                if attempt + 1 == MODEL_CHUNK_ATTEMPTS:
                    name = "RemoteProtocolError" if isinstance(error, AssetDownloadError) else type(error).__name__
                    safe_class = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "TransportError"
                    raise PreparationError("음성 모델 구간을 내려받지 못했습니다. 인터넷 연결을 확인하고 다시 준비하세요",
                        code="range_transfer_incomplete", transport_class=safe_class) from None
            finally:
                payload.clear()
    return count


def reserve(data_dir: Path) -> None:
    """요청 루프에서 첫 작업 예약과 동시에 신규 전사를 막는다. 파일·모델을 열지 않는다."""
    _STATUS[str(Path(data_dir).resolve())] = {"state": "preparing", "message": "음성 모델 준비를 시작합니다",
                                             "bytes": 0, "progress": 0}


def mark_cancelled(data_dir: Path) -> None:
    _STATUS[str(Path(data_dir).resolve())] = {"state": "cancelled", "message": "음성 모델 준비를 취소했습니다"}


def _inside(root: Path, path: Path) -> Path:
    absolute = path.absolute()
    if ".." in path.parts or not absolute.is_relative_to(root.absolute()):
        raise PreparationError("음성 모델 준비 경로를 확인하지 못했습니다")
    return absolute


def status(data_dir: Path) -> dict:
    root = Path(data_dir)
    present = all(_size_ok(root / path, size) for path, size, _, _ in FILES)
    dll = root / "runtimes" / RUNTIME_ARCHIVE.removesuffix(".zip") / "bin/nemo_speech_asr_c.dll"
    present = present and dll.is_file()
    result = {"files_present": present, "state": "idle" if present else "missing",
              "message": "파일 준비됨 · CPU 전사 실행 전" if present else "CPU 런타임·한국어 음성 모델 준비가 필요합니다",
              "total_bytes": TOTAL_BYTES, "license": MODEL_LICENSE,
              "model_revision": MODEL_REVISION, "runtime_tag": RUNTIME_TAG,
              "cpu_limit_kind": "owned_child_logical_cpu_affinity",
              "installed_inference_verified": False}
    previous = _STATUS.get(str(root.resolve()), {})
    # 과거의 준비 성공 안내가 현재 누락·크기 손상을 가리지 않도록 한다.
    if previous.get("state") == "idle" and not present:
        previous = {}
    return {**result, **previous}


def _size_ok(path: Path, size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def _verified(path: Path, size: int, sha: str) -> bool:
    return verified_file(path, size, sha)


def _extract(root: Path) -> None:
    """공식 해시를 확인한 ZIP만 읽으며 링크·경로 이탈·과다 압축을 거부한다."""
    from .nemotron_worker import _open_read_locked

    archive = _inside(root, root / FILES[0][0])
    prefix = RUNTIME_ARCHIVE.removesuffix(".zip")
    seen, total = set(), 0
    with open_regular_file(archive) as guard, _open_read_locked(archive) as archive_stream:
        first, second = os.fstat(guard.fileno()), os.fstat(archive_stream.fileno())
        if (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino):
            raise PreparationError("CPU 런타임의 파일이 검사 중 변경되었습니다")
        archive_stream.seek(0)
        if (os.fstat(archive_stream.fileno()).st_size != RUNTIME_BYTES
                or hashlib.file_digest(archive_stream, "sha256").hexdigest() != RUNTIME_SHA256):
            raise PreparationError("CPU 런타임의 검증 해시가 일치하지 않습니다")
        # 검증 전에는 ZIP 생성자의 중앙 디렉터리 파싱도 실행하지 않는다.
        archive_stream.seek(0)
        with zipfile.ZipFile(archive_stream) as bundle:
            entries = bundle.infolist()
            if len(entries) > 2000:
                raise PreparationError("CPU 런타임의 압축 구성을 확인하지 못했습니다")
            for entry in entries:
                parts = PurePosixPath(entry.filename).parts
                raw_name = entry.orig_filename
                canonical_name = "/".join(parts)
                mode = entry.external_attr >> 16
                total += entry.file_size
                if (not parts or parts[0] != prefix or ".." in parts or "\\" in raw_name
                        or "\x00" in raw_name or ":" in raw_name or raw_name.startswith("/")
                        or raw_name.rstrip("/") != canonical_name
                        or any(part.endswith((".", " ")) for part in parts)
                        or canonical_name.casefold() in seen or stat.S_ISLNK(mode)
                        or total > 128 * 1024 * 1024):
                    raise PreparationError("CPU 런타임의 압축 구성을 확인하지 못했습니다")
                seen.add(canonical_name.casefold())
            for entry in entries:
                if entry.is_dir():
                    continue
                target = _inside(root, root / "runtimes" / entry.filename)
                make_private_directory(target.parent, parents=True, exist_ok=True)
                with bundle.open(entry) as source:
                    expected = hashlib.file_digest(source, "sha256").hexdigest()
                if verified_file(target, entry.file_size, expected):
                    # 검증된 구형 캐시는 권한·파일 판본을 바꾸지 않고 그대로 재사용한다.
                    continue
                with bundle.open(entry) as source, atomic_download(target) as output:
                    shutil.copyfileobj(source, output, 64 * 1024)


async def prepare(data_dir: Path) -> dict:
    """HTTP 엔드포인트의 명시적 라이선스·준비 확인 뒤에만 호출한다. 추론은 하지 않는다."""
    root = Path(data_dir).absolute()
    key = str(root.resolve())
    try:
        make_private_directory(root, parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < TOTAL_BYTES + 192 * 1024 * 1024:
            raise PreparationError("음성 모델 준비에 약 950MB 이상의 여유 공간이 필요합니다")
        _STATUS[key] = {"state": "preparing", "message": "공식 CPU 런타임·모델을 확인하고 있습니다",
                        "bytes": 0, "progress": 0}
        received = 0
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(30, connect=10)) as client:
            for relative, size, sha, url in FILES:
                target = _inside(root, root / relative)
                make_private_directory(target.parent, parents=True, exist_ok=True)
                if await asyncio.to_thread(_verified, target, size, sha):
                    received += size
                    continue
                def progress(count):
                    _STATUS[key].update(bytes=received + count, progress=round((received + count) / TOTAL_BYTES, 4))
                if relative == Path("models/nemotron35-asr") / MODEL_FILE:
                    digest = hashlib.sha256()
                    with atomic_download(target) as output:
                        count = await _download_model_ranges(client, url, size, output, digest, progress)
                        if count != size or digest.hexdigest() != sha:
                            raise PreparationError("음성 모델 파일의 검증 해시가 일치하지 않습니다")
                else:
                    await download_file(client, url, target, size, sha, progress=progress)
                received += size
        # 작은 CPU ZIP을 쓰는 동안 취소되면 추출 종료까지 기다려 종료 후 쓰기를 남기지 않는다.
        extraction = asyncio.create_task(asyncio.to_thread(_extract, root))
        try:
            await asyncio.shield(extraction)
        except asyncio.CancelledError:
            await asyncio.gather(extraction, return_exceptions=True)
            raise
        _STATUS[key] = {"state": "idle", "message": "공식 파일 해시 확인 완료 · CPU 전사 실행 전",
                        "bytes": TOTAL_BYTES, "progress": 1}
        return status(root)
    except asyncio.CancelledError:
        mark_cancelled(root)
        raise
    except Exception as exc:
        safe = exc if isinstance(exc, PreparationError) else PreparationError("음성 모델 준비에 실패했습니다. 저장공간과 인터넷 연결을 확인하세요")
        _STATUS[key] = {"state": "error", "message": str(safe), "error_code": safe.code}
        if safe.transport_class is not None:
            _STATUS[key]["transport_exception_class"] = safe.transport_class
        if safe.http_status is not None:
            _STATUS[key]["http_status"] = safe.http_status
        raise safe from None
