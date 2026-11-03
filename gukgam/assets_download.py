"""고정 공식 자산의 전송·캐시·원자 저장. 모델 실행과 임의 URL 입력은 받지 않는다."""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from .secure_files import (atomic_replace, create_private_file, make_private_directory,
                          open_regular_file, unlink_owned_file, write_all)

MAX_REDIRECTS = 4
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


class AssetDownloadError(RuntimeError):
    """주소·헤더·응답·개인 경로를 포함하지 않는 유한한 준비 오류."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("공식 준비 파일을 안전하게 확인하지 못했습니다. 인터넷 연결과 저장공간을 확인하세요.")


def _url(url: str, *, initial: bool) -> str:
    try:
        if not isinstance(url, str) or len(url) > 16384:
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError
        host = parsed.hostname.lower()
        if initial:
            # 최초 경로는 고정 revision 또는 고정 공식 릴리스의 파일만 허용한다.
            allowed = ((host == "huggingface.co" and re.fullmatch(
                r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/resolve/[0-9a-f]{40}/[A-Za-z0-9_.-]+", parsed.path))
                or (host == "github.com" and re.fullmatch(
                    r"/NVIDIA/NeMo-Speech\.cpp/releases/download/v[0-9.]+/[A-Za-z0-9_.-]+", parsed.path)))
            if not allowed or parsed.query:
                raise ValueError
        elif not (host in {"huggingface.co", "github.com", "release-assets.githubusercontent.com",
                           "objects.githubusercontent.com", "cdn-lfs.huggingface.co"}
                  or host.endswith(".hf.co") or host.endswith(".xethub.hf.co")):
            raise ValueError
        return url
    except (ValueError, TypeError):
        raise AssetDownloadError("download_origin_rejected") from None


@asynccontextmanager
async def asset_response(client, url: str, *, headers: dict[str, str] | None = None):
    """TLS 검증을 유지하고, 응답을 열기 전 매 redirect 목적지를 검사한다."""
    current = _url(url, initial=True)
    safe_headers = {"Accept-Encoding": "identity"}
    if headers:
        if set(headers) - {"Range", "Accept-Encoding"}:
            raise AssetDownloadError("download_headers_rejected")
        safe_headers.update(headers)
    for hop in range(MAX_REDIRECTS + 1):
        body_opened = False
        try:
            async with client.stream("GET", current, headers=safe_headers) as response:
                if response.status_code in _REDIRECT_STATUS:
                    location = response.headers.get("Location", "")
                    if (hop == MAX_REDIRECTS or not location or len(location) > 16384
                            or any(c.isspace() or ord(c) < 32 for c in location)):
                        raise AssetDownloadError("download_redirect_limit")
                    current = _url(urljoin(current, location), initial=False)
                    continue
                body_opened = True
                yield response
                return
        except httpx.RemoteProtocolError:
            # 헤더/redirect 거부와 실제 본문 단절을 구분한다. 구간 전송만 후자를
            # 고정된 2회 한도 안에서 다시 받을 수 있으며 원문 예외는 전달하지 않는다.
            raise AssetDownloadError("download_body_incomplete" if body_opened else "download_protocol_rejected") from None
    raise AssetDownloadError("download_redirect_limit")


def checked_spec(size: int, sha256: str, *, allow_empty=False) -> None:
    if (type(size) is not int or not (0 if allow_empty else 1) <= size <= 4 * 1024**3 or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
        raise AssetDownloadError("download_manifest_invalid")


def verified_file(path: Path, size: int, sha256: str) -> bool:
    """기존 캐시도 같은 일반 파일 핸들에서 크기·전문 해시를 확인한다."""
    checked_spec(size, sha256, allow_empty=True)
    try:
        with open_regular_file(path) as stream:
            return (os.fstat(stream.fileno()).st_size == size
                    and hashlib.file_digest(stream, "sha256").hexdigest() == sha256)
    except (OSError, ValueError):
        return False


@contextmanager
def atomic_download(target: Path):
    """이번 요청의 비공개 파일만 정리하며 검증 성공 뒤 같은 부모에서 교체한다."""
    target = Path(target).absolute()
    make_private_directory(target.parent, parents=True, exist_ok=True)
    partial = target.with_name(target.name + "." + uuid4().hex + ".partial")
    identity = None
    adopted = False
    try:
        with create_private_file(partial) as output:
            info = os.fstat(output.fileno())
            identity = info.st_dev, info.st_ino
            yield output
            output.flush()
            os.fsync(output.fileno())
        atomic_replace(partial, target)
        adopted = True
    finally:
        if identity is not None and not adopted:
            try:
                unlink_owned_file(partial, identity)
            except (OSError, ValueError):
                # 바뀐 파일·부모는 삭제하지 않는다. 원래 준비 실패를 유지한다.
                pass


async def download_file(client, url: str, target: Path, size: int, sha256: str, *, progress=None) -> None:
    """완전한 고정 크기·해시가 일치한 원시 바이트만 캐시로 채택한다."""
    checked_spec(size, sha256)
    _url(url, initial=True)
    count, digest = 0, hashlib.sha256()
    with atomic_download(target) as output:
        async with asset_response(client, url) as response:
            if response.status_code != 200:
                raise AssetDownloadError("download_http_status")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise AssetDownloadError("download_encoding_rejected")
            length = response.headers.get("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,12}", length) or int(length) != size):
                raise AssetDownloadError("download_length_mismatch")
            async for block in response.aiter_raw(64 * 1024):
                count += len(block)
                if count > size:
                    raise AssetDownloadError("download_size_exceeded")
                write_all(output, block)
                digest.update(block)
                if progress:
                    progress(count)
        if count != size or digest.hexdigest() != sha256:
            raise AssetDownloadError("download_hash_mismatch")
