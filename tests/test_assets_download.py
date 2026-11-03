"""실제 인터넷·모델 없이 공식 자산의 전송 및 파일 채택 경계를 검증한다."""
import asyncio
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import threading
import zipfile

import httpx
import pytest

from gukgam import assets_download as assets
from gukgam.secure_files import create_private_file, make_private_directory, open_regular_file
from gukgam.stt import nemotron_assets as asr
from gukgam.stt import nemotron_diarization_assets as diar


URL = "https://huggingface.co/test/model/resolve/" + "a" * 40 + "/model.bin"
PAYLOAD = b"synthetic model bytes only"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)


def response(status=200, *, headers=None, payload=b""):
    # 원시 전송 계약이므로 MockTransport도 미리 읽거나 압축 해제하지 않는다.
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(payload))


async def test_fixed_asset_is_private_verified_and_reused_without_network(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["Accept-Encoding"] == "identity"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return response(payload=PAYLOAD)
    target = tmp_path / "prepared" / "model.bin"
    async with client(handler) as connection:
        await assets.download_file(connection, URL, target, len(PAYLOAD), SHA)
    assert assets.verified_file(target, len(PAYLOAD), SHA)
    with open_regular_file(target, private=True) as stream:
        assert stream.read() == PAYLOAD
    assert len(calls) == 1 and not list(target.parent.glob("*.partial"))


@pytest.mark.parametrize("url", [URL.replace("https:", "http:"), URL.replace("huggingface.co", "localhost"),
    URL.replace("https://", "https://user:secret@"), URL.replace("a" * 40, "main"), URL + "?token=synthetic-secret",
    URL + "#fragment", URL.replace("huggingface.co", "huggingface.co.attacker.invalid"),
    URL.replace("huggingface.co", "huggingface.co:444"), None])
async def test_untrusted_initial_origin_is_rejected_before_request(tmp_path, url):
    calls = []
    async with client(lambda request: calls.append(request)) as connection:
        with pytest.raises(assets.AssetDownloadError):
            await assets.download_file(connection, url, tmp_path / "model", len(PAYLOAD), SHA)
    assert calls == [] and list(tmp_path.iterdir()) == []


async def test_official_cdn_redirect_is_followed_after_validation(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "huggingface.co":
            return httpx.Response(302, headers={"Location": "https://cas-bridge.xethub.hf.co/signed?public-signature=test"})
        return response(payload=PAYLOAD)
    async with client(handler) as connection:
        await assets.download_file(connection, URL, tmp_path / "model", len(PAYLOAD), SHA)
    assert calls == ["huggingface.co", "cas-bridge.xethub.hf.co"]


@pytest.mark.parametrize("location", ["http://huggingface.co/file", "https://attacker.invalid/file",
    "https://secret:token@release-assets.githubusercontent.com/file", "file:///tmp/model", "\n" + URL])
async def test_bad_redirect_is_never_contacted_or_adopted(tmp_path, location):
    calls = []
    def handler(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"Location": location})
    async with client(handler) as connection:
        with pytest.raises(assets.AssetDownloadError) as error:
            await assets.download_file(connection, URL, tmp_path / "model", len(PAYLOAD), SHA)
    assert len(calls) == 1
    assert "secret" not in str(error.value) and "attacker" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


async def test_redirect_loop_has_exact_finite_bound(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": URL})
    async with client(handler) as connection:
        with pytest.raises(assets.AssetDownloadError, match="공식"):
            await assets.download_file(connection, URL, tmp_path / "model", len(PAYLOAD), SHA)
    assert len(calls) == assets.MAX_REDIRECTS + 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status,headers,payload", [(403, {}, b"synthetic-secret"),
    (200, {"Content-Encoding": "gzip"}, PAYLOAD), (200, {"Content-Length": "999"}, PAYLOAD),
    (200, {}, PAYLOAD + b"x"), (200, {}, PAYLOAD[:-1]), (200, {}, b"x" * len(PAYLOAD))])
async def test_invalid_transfer_preserves_previous_file_and_cleans_owned_partial(tmp_path, status, headers, payload):
    target = tmp_path / "model"
    with create_private_file(target) as stream:
        stream.write(b"previous verified generation")
    async with client(lambda _: response(status, headers=headers, payload=payload)) as connection:
        with pytest.raises(assets.AssetDownloadError) as error:
            await assets.download_file(connection, URL, target, len(PAYLOAD), SHA)
    assert target.read_bytes() == b"previous verified generation"
    assert "synthetic-secret" not in str(error.value)
    assert not list(tmp_path.glob("*.partial"))


class WaitingBody(httpx.AsyncByteStream):
    def __init__(self):
        self.entered = asyncio.Event()
    async def __aiter__(self):
        yield PAYLOAD[:4]
        self.entered.set()
        await asyncio.Event().wait()
    async def aclose(self):
        pass


async def test_cancel_removes_only_owned_partial_and_preserves_existing_cache(tmp_path):
    target = tmp_path / "model"
    with create_private_file(target) as stream:
        stream.write(b"previous")
    other = tmp_path / "unrelated.partial"
    other.write_bytes(b"other request")
    body = WaitingBody()
    async with client(lambda _: httpx.Response(200, stream=body)) as connection:
        task = asyncio.create_task(assets.download_file(connection, URL, target, len(PAYLOAD), SHA))
        await asyncio.wait_for(body.entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert target.read_bytes() == b"previous" and other.read_bytes() == b"other request"
    assert list(tmp_path.glob("*.partial")) == [other]


def test_failed_atomic_adoption_keeps_original_and_uses_guarded_parent(monkeypatch, tmp_path):
    target = tmp_path / "model"
    target.write_bytes(b"previous")
    calls = []
    real_open = assets.create_private_file
    @contextmanager
    def guarded(path):
        calls.append(path)
        with real_open(path) as stream:
            yield stream
    monkeypatch.setattr(assets, "create_private_file", guarded)
    monkeypatch.setattr(assets, "atomic_replace", lambda *_: (_ for _ in ()).throw(ValueError("guard rejected")))
    with pytest.raises(ValueError):
        with assets.atomic_download(target) as stream:
            stream.write(PAYLOAD)
    assert len(calls) == 1 and calls[0].parent == target.parent
    assert target.read_bytes() == b"previous" and not list(tmp_path.glob("*.partial"))


def test_same_size_corrupt_cache_is_not_trusted(tmp_path):
    target = tmp_path / "model"
    target.write_bytes(b"x" * len(PAYLOAD))
    assert not assets.verified_file(target, len(PAYLOAD), SHA)


async def test_asr_range_contract_and_atomic_hash_are_preserved(tmp_path, monkeypatch):
    calls, output = [], io.BytesIO()
    monkeypatch.setattr(asr, "MODEL_CHUNK_BYTES", 8)
    def handler(request):
        calls.append(request.headers["Range"])
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        return response(206, headers={"Content-Range": f"bytes {start}-{end}/{len(PAYLOAD)}"},
                        payload=PAYLOAD[start:end + 1])
    digest = hashlib.sha256()
    async with client(handler) as connection:
        count = await asr._download_model_ranges(connection, URL, len(PAYLOAD), output, digest, lambda _: None)
    assert count == len(PAYLOAD) and output.getvalue() == PAYLOAD and digest.hexdigest() == SHA
    assert len(calls) == (len(PAYLOAD) + 7) // 8


async def test_bad_asr_range_is_not_written_or_retried(tmp_path):
    calls, output = [], io.BytesIO()
    def handler(request):
        calls.append(request)
        return httpx.Response(206, headers={"Content-Range": "bytes 1-2/3"}, content=b"bad")
    async with client(handler) as connection:
        with pytest.raises(asr.PreparationError):
            await asr._download_model_ranges(connection, URL, len(PAYLOAD), output, hashlib.sha256(), lambda _: None)
    assert len(calls) == 1 and output.getvalue() == b""


async def test_asr_transient_range_retry_remains_exactly_two_attempts():
    calls, output = [], io.BytesIO()
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic-private-marker", request=request)
    async with client(handler) as connection:
        with pytest.raises(asr.PreparationError) as error:
            await asr._download_model_ranges(connection, URL, len(PAYLOAD), output, hashlib.sha256(), lambda _: None)
    assert len(calls) == 2 and output.getvalue() == b""
    assert error.value.transport_class == "ReadTimeout" and "synthetic-private-marker" not in str(error.value)


@pytest.mark.parametrize("recover", [False, True])
async def test_asr_partial_body_disconnect_retries_without_mixing_failed_bytes(recover):
    calls, output, progress = [], io.BytesIO(), []
    complete = PAYLOAD * 8192
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (64 * 1024)  # 실제 aiter_raw 한 블록이 구간 버퍼에 들어간 뒤 단절
            raise httpx.RemoteProtocolError("synthetic-private-response-marker")
        async def aclose(self):
            pass
    def handler(request):
        calls.append(request)
        headers = {"Content-Range": f"bytes 0-{len(complete) - 1}/{len(complete)}"}
        if recover and len(calls) == 2:
            return response(206, headers=headers, payload=complete)
        return httpx.Response(206, headers=headers, stream=BrokenBody())
    digest = hashlib.sha256()
    async with client(handler) as connection:
        if recover:
            count = await asr._download_model_ranges(connection, URL, len(complete), output, digest, progress.append)
            assert count == len(complete) and output.getvalue() == complete and digest.hexdigest() == hashlib.sha256(complete).hexdigest()
            assert progress == [len(complete)]
        else:
            with pytest.raises(asr.PreparationError) as error:
                await asr._download_model_ranges(connection, URL, len(complete), output, digest, progress.append)
            assert error.value.transport_class == "RemoteProtocolError"
            assert "synthetic-private-response-marker" not in str(error.value)
            assert output.getvalue() == b"" and digest.hexdigest() == hashlib.sha256().hexdigest() and progress == []
    assert len(calls) == 2


async def test_unsafe_parent_guard_stops_before_request_or_partial(tmp_path, monkeypatch):
    calls = []
    def reject(*args, **kwargs):
        raise ValueError("synthetic parent rejected")
    monkeypatch.setattr(assets, "make_private_directory", reject)
    async with client(lambda request: calls.append(request)) as connection:
        with pytest.raises(ValueError):
            await assets.download_file(connection, URL, tmp_path / "unsafe" / "model", len(PAYLOAD), SHA)
    assert calls == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("size,sha", [(0, SHA), (True, SHA), (4 * 1024**3 + 1, SHA),
    (len(PAYLOAD), "0" * 63), (len(PAYLOAD), "G" * 64), (len(PAYLOAD), None)])
async def test_invalid_manifest_never_creates_files_or_requests(tmp_path, size, sha):
    calls = []
    async with client(lambda request: calls.append(request)) as connection:
        with pytest.raises(assets.AssetDownloadError):
            await assets.download_file(connection, URL, tmp_path / "model", size, sha)
    assert calls == [] and list(tmp_path.iterdir()) == []


def tiny_assets(monkeypatch):
    buf = io.BytesIO()
    prefix = asr.RUNTIME_ARCHIVE.removesuffix(".zip")
    with zipfile.ZipFile(buf, "w") as bundle:
        bundle.writestr(prefix + "/bin/nemo_speech_asr_c.dll", b"synthetic-dll-not-executable")
    archive = buf.getvalue()
    runtime = (Path("runtimes") / asr.RUNTIME_ARCHIVE, len(archive), hashlib.sha256(archive).hexdigest(),
               "https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/v0.2.0/" + asr.RUNTIME_ARCHIVE)
    asr_model = (Path("models/nemotron35-asr") / asr.MODEL_FILE, len(PAYLOAD), SHA, URL)
    diar_model = (Path("models/nemotron3-diarization") / diar.MODEL_FILE, len(PAYLOAD), SHA, URL)
    monkeypatch.setattr(asr, "FILES", (runtime, asr_model))
    monkeypatch.setattr(diar, "FILES", (runtime, diar_model))
    monkeypatch.setattr(asr, "RUNTIME_BYTES", len(archive))
    monkeypatch.setattr(asr, "RUNTIME_SHA256", runtime[2])
    monkeypatch.setattr(asr, "TOTAL_BYTES", len(archive) + len(PAYLOAD))
    monkeypatch.setattr(diar, "TOTAL_BYTES", len(archive) + len(PAYLOAD))
    return archive


async def test_both_cpu_preparations_share_verified_runtime_and_reuse_cache(tmp_path, monkeypatch):
    archive = tiny_assets(monkeypatch)
    calls, client_options = [], []
    original_client = httpx.AsyncClient
    def handler(request):
        calls.append(request)
        if request.url.path.endswith(".zip"):
            return response(payload=archive)
        if "Range" in request.headers:
            return response(206, headers={"Content-Range": f"bytes 0-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"}, payload=PAYLOAD)
        return response(payload=PAYLOAD)
    def factory(**kwargs):
        client_options.append(kwargs)
        return original_client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(asr.httpx, "AsyncClient", factory)
    await asr.prepare(tmp_path)
    await diar.prepare(tmp_path, confirm=True, accept_license=True, bundled_dir=tmp_path / "absent-bundle")
    assert len(calls) == 3  # 공통 런타임 1회, 전사·화자 모델 각 1회
    assert all(option["follow_redirects"] is False and option["trust_env"] is False for option in client_options)
    assert all((tmp_path / path).read_bytes() == PAYLOAD for path in (asr.FILES[1][0], diar.FILES[1][0]))
    before = len(calls)
    monkeypatch.setattr(asr, "atomic_download", lambda *_: pytest.fail("verified runtime cache must not be rewritten"))
    await asr.prepare(tmp_path)
    await diar.prepare(tmp_path, confirm=True, accept_license=True, bundled_dir=tmp_path / "absent-bundle")
    assert len(calls) == before and not list(tmp_path.rglob("*.partial"))


async def test_diarization_license_is_required_before_any_download_or_write(tmp_path, monkeypatch):
    monkeypatch.setattr(diar.httpx, "AsyncClient", lambda **_: pytest.fail("network client must not be created"))
    with pytest.raises(ValueError):
        await diar._prepare(tmp_path, confirm=True, accept_license=False)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_name", ["../outside", "wrong-root/bin/model.dll", "root/../outside",
    "root/dir\\model.dll", "root/ads:stream", "root/dir./model.dll"])
def test_verified_runtime_zip_rejects_unsafe_layout_before_any_extraction(tmp_path, monkeypatch, bad_name):
    buf = io.BytesIO()
    prefix = asr.RUNTIME_ARCHIVE.removesuffix(".zip")
    bad_name = bad_name.replace("root/", prefix + "/")
    with zipfile.ZipFile(buf, "w") as bundle:
        bundle.writestr(prefix + "/bin/nemo_speech_asr_c.dll", b"synthetic")
        bundle.writestr(bad_name, b"must-not-extract")
    payload = buf.getvalue()
    if "\\" in bad_name:
        # Windows ZipInfo 생성자는 역슬래시를 정규화하므로, 중앙/로컬 헤더를
        # 같은 길이의 실제 악성 이름으로 만든다. 모델·런타임 파일은 실행하지 않는다.
        payload = payload.replace(bad_name.replace("\\", "/").encode(), bad_name.encode())
    monkeypatch.setattr(asr, "RUNTIME_BYTES", len(payload))
    monkeypatch.setattr(asr, "RUNTIME_SHA256", hashlib.sha256(payload).hexdigest())
    archive = tmp_path / asr.FILES[0][0]
    make_private_directory(archive.parent, parents=True, exist_ok=True)
    with create_private_file(archive) as stream:
        stream.write(payload)
    with pytest.raises(asr.PreparationError):
        asr._extract(tmp_path)
    assert not (archive.parent / prefix).exists()
    assert not list(tmp_path.rglob("*.partial"))


async def test_diarization_cancel_waits_for_owned_extraction_before_return(tmp_path, monkeypatch):
    entered, released, completed = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(diar, "status", lambda *_, **kw: {"asset_origin": None})
    monkeypatch.setattr(diar, "FILES", ())
    def extraction(_root):
        entered.set()
        assert released.wait(2)
        completed.set()
    monkeypatch.setattr(asr, "_extract", extraction)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(diar.httpx, "AsyncClient", lambda **kw:
        original_client(**kw, transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))))
    task = asyncio.create_task(diar._prepare(tmp_path, confirm=True, accept_license=True))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not completed.is_set()
    finally:
        released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()


def test_model_page_explains_explicit_first_preparation_without_bundled_claim():
    source = (Path(__file__).parents[1] / "gukgam/web/templates/admin.html").read_text(encoding="utf-8")
    assert "화자 분리 모델은 설치파일에 포함" not in source
    assert "이미 준비한 파일은 검증 후 다시 사용" in source
    assert 'id="nemotron-license"' in source and 'id="nemotron-prepare"' in source
    assert 'id="julia-prepare"' in source
