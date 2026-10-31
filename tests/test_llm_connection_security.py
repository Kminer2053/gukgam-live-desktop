"""합성 키·MockTransport·임시 설정만으로 연결과 본문 보정 경계를 검증한다."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from gukgam import settings
from gukgam.llm import connection_test, credential_store
from gukgam.llm.compat_config import connection_url, normalize_base_url
from gukgam.llm.providers import (AnthropicProvider, LLMError, LLMRequestStale, OpenAICompatProvider,
                                  make_provider, provider_signature)
from gukgam.web import routes_api


class MemorySettings:
    def __init__(self):
        self.saved = {}

    def get_setting(self, key):
        return deepcopy(self.saved.get(key))

    def set_setting(self, key, value):
        self.saved[key] = deepcopy(value)


@pytest.fixture(autouse=True)
def fake_dpapi(monkeypatch):
    monkeypatch.setattr(credential_store, "_windows", lambda: True)
    def crypt(data, *, decrypt):
        if decrypt:
            if not data.startswith(b"sealed:"):
                raise OSError("PRIVATE_OS_ERROR")
            data = data[7:]
        value = bytes(value ^ 0xA5 for value in data)
        return value if decrypt else b"sealed:" + value
    monkeypatch.setattr(credential_store, "_crypt", crypt)


def test_windows_save_encrypts_and_migrates_plaintext_and_mask_keeps_endpoint_binding():
    repo = MemorySettings()
    key = "SYNTHETIC_PRIVATE_LONG_API_KEY"
    repo.saved["llm"] = {"provider": "openai_compat", "openai_base_url": "http://localhost:9999/v1", "openai_api_key": key}
    assert settings.get(repo, "llm")["openai_api_key"] == key
    settings.update(repo, "llm", {"max_tokens": 800})
    assert repo.saved["llm"]["openai_api_key"].startswith(credential_store.PREFIX)
    assert key not in json.dumps(repo.saved)
    assert settings.get(repo, "llm")["openai_api_key"] == key
    assert settings.public_view({"llm": settings.get(repo, "llm")})["llm"]["openai_api_key"] == "••••_KEY"
    assert "openai_key_base_url" not in settings.public_view({"llm": settings.get(repo, "llm")})["llm"]
    settings.update(repo, "llm", {"openai_base_url": "https://other.example/v1", "openai_api_key": "••••_KEY"})
    with pytest.raises(LLMError, match="서버 주소가 바뀌었습니다"):
        make_provider(settings.get(repo, "llm"))
    settings.update(repo, "llm", {"clear_openai_api_key": True})
    assert settings.get(repo, "llm")["openai_api_key"] == ""
    assert make_provider(settings.get(repo, "llm")).api_key == ""


def test_new_key_explicitly_rebinds_to_new_endpoint_and_public_urls_hide_legacy_auth():
    repo = MemorySettings()
    settings.update(repo, "llm", {"provider": "openai_compat", "openai_connection": "ollama",
                                  "openai_model": "installed-model", "openai_api_key": "first-private-key"})
    settings.update(repo, "llm", {"openai_connection": "openai", "openai_api_key": "second-private-key"})
    provider = make_provider(settings.get(repo, "llm"))
    assert provider.base_url == "https://api.openai.com/v1" and provider.api_key == "second-private-key"
    public = settings.public_view({"llm": {"openai_base_url": "https://user:PRIVATE@example/v1?api_key=PRIVATE",
                                            "openai_api_key": "PRIVATE"}})
    assert public["llm"]["openai_base_url"] == "" and "user:" not in str(public)


def test_encoded_key_in_endpoint_or_key_in_model_is_rejected_without_echo():
    cfg = {"provider": "openai_compat", "openai_api_key": "PRIVATE_KEY", "openai_model": "installed",
           "openai_base_url": "https://server.test/PRIVATE%5FKEY/v1"}
    with pytest.raises(LLMError) as caught:
        make_provider(cfg)
    assert "PRIVATE_KEY" not in str(caught.value)
    assert settings.public_view({"llm": cfg})["llm"]["openai_base_url"] == ""
    cfg.update(openai_base_url="https://server.test/v1", openai_model="PRIVATE_KEY")
    with pytest.raises(LLMError):
        make_provider(cfg)
    assert settings.public_view({"llm": cfg})["llm"]["openai_model"] == ""


def test_key_saved_before_endpoint_is_not_silently_sent_to_first_new_server():
    repo = MemorySettings()
    settings.update(repo, "llm", {"provider": "openai_compat", "openai_api_key": "unbound-private-key"})
    settings.update(repo, "llm", {"openai_connection": "ollama", "openai_model": "installed"})
    with pytest.raises(LLMError):
        make_provider(settings.get(repo, "llm"))
    settings.update(repo, "llm", {"openai_api_key": "intentionally-bound-key"})
    assert make_provider(settings.get(repo, "llm")).api_key == "intentionally-bound-key"


def test_credential_failure_is_fixed_and_nonwindows_legacy_is_not_falsely_encrypted(monkeypatch):
    secret = "PRIVATE_SECRET_MARKER"
    sealed = credential_store.protect_secret(secret)
    assert secret not in sealed and credential_store.unprotect_secret(sealed) == secret
    for value in ("dpapi:v1:!!!", "dpapi:v2:PRIVATE_SECRET_MARKER"):
        with pytest.raises(credential_store.CredentialError) as caught:
            credential_store.unprotect_secret(value)
        assert "PRIVATE_SECRET_MARKER" not in str(caught.value)
    monkeypatch.setattr(credential_store, "_windows", lambda: False)
    assert credential_store.protect_secret(secret) == secret
    with pytest.raises(credential_store.CredentialError):
        credential_store.unprotect_secret(sealed)


def test_unreadable_key_can_be_replaced_without_exposing_ciphertext_or_sending_it():
    repo = MemorySettings()
    repo.saved["llm"] = {"provider": "openai_compat", "openai_api_key": "dpapi:v1:!!!"}
    with pytest.raises(credential_store.CredentialError):
        settings.get(repo, "llm")
    public = settings.public_view(settings.get_all(repo))
    assert public["llm"]["openai_api_key"] == "••••" and "dpapi:" not in str(public)
    settings.update(repo, "llm", {"openai_connection": "ollama", "openai_api_key": "replacement-private-key"})
    assert settings.get(repo, "llm")["openai_api_key"] == "replacement-private-key"


@pytest.mark.parametrize("url", ["https://user:PRIVATE@example/v1", "https://example/v1?key=PRIVATE",
                                  "https://example/v1#PRIVATE", "file:///PRIVATE", "https://example/v1/chat/completions"])
def test_url_validation_never_echoes_user_credentials(url):
    with pytest.raises(ValueError) as caught:
        normalize_base_url(url)
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("preset,url", [("ollama", "http://127.0.0.1:11434/v1"),
                                       ("lmstudio", "http://127.0.0.1:1234/v1"),
                                       ("openai", "https://api.openai.com/v1")])
def test_presets_and_signature_do_not_contain_key(preset, url):
    assert connection_url({"openai_connection": preset}) == url
    signature = provider_signature({"provider": "openai_compat", "openai_api_key": "PRIVATE_API_KEY"})
    assert len(signature) == 64 and "PRIVATE" not in signature


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 307, 401, 403, 429, 500])
async def test_http_error_body_and_redirect_cannot_expose_or_forward_key(status, caplog):
    requests = []
    key = "PRIVATE_AUTH_MARKER"
    def handle(request):
        requests.append(request)
        return httpx.Response(status, text=key, headers={"Location": "https://elsewhere.example/leak"})
    provider = OpenAICompatProvider("http://127.0.0.1:11434/v1", "installed", key,
                                    transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(LLMError) as caught:
            await provider.complete("synthetic", "synthetic", json_mode=False)
        assert key not in str(caught.value) + caplog.text
        assert len(requests) == 1 and requests[0].url.host == "127.0.0.1"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_guard_prevents_send_retry_and_cancels_owned_inflight_request():
    calls, allowed, cancelled = [], True, asyncio.Event()
    async def handle(request):
        nonlocal allowed
        calls.append(request)
        allowed = False
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
    provider = OpenAICompatProvider("http://127.0.0.1:11434/v1", "installed", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(LLMRequestStale):
            await provider.complete("s", "u", request_guard=lambda: False)
        assert calls == []
        with pytest.raises(LLMRequestStale):
            await provider.complete("s", "u", request_guard=lambda: allowed, execution_timeout=1)
        assert len(calls) == 1 and cancelled.is_set()
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_response_format_fallback_is_one_retry_and_guarded():
    calls, allowed = [], True
    def handle(request):
        nonlocal allowed
        calls.append(request)
        allowed = False
        return httpx.Response(400, text="PRIVATE_BODY")
    provider = OpenAICompatProvider("http://server.test/v1", "installed", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(LLMRequestStale):
            await provider.complete("s", "u", request_guard=lambda: allowed)
        assert len(calls) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", ["custom", "ollama"])
async def test_short_connection_probe_is_single_synthetic_request_and_returns_no_server_body(connection):
    seen, instances = [], []
    def factory(cfg, rt):
        def handle(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
        provider = OpenAICompatProvider("http://server.test/v1", "installed", transport=httpx.MockTransport(handle),
                                        connection=connection)
        instances.append(provider)
        return provider
    result = await connection_test.test_connection({"provider": "openai_compat"}, None, factory=factory)
    assert result["ok"] and result["synthetic_only"] and "sample" not in result and "result" not in result
    assert len(seen) == 1 and 0 < seen[0]["max_tokens"] <= 128 and "response_format" not in seen[0]
    assert (seen[0].get("reasoning_effort") == "none") is (connection == "ollama")
    assert all("회의" not in m["content"] for m in seen[0]["messages"])
    assert instances[0]._client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", ["ollama", "lmstudio", "custom", "openai"])
@pytest.mark.parametrize("json_mode", [False, True])
async def test_only_ollama_requests_no_thinking_with_same_budget_and_never_returns_reasoning(connection, json_mode):
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"ok":true}', "reasoning": "PRIVATE_REASONING_MARKER"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 6}})
    provider = OpenAICompatProvider("http://server.test/v1", "installed", transport=httpx.MockTransport(handle),
                                    connection=connection)
    try:
        result = await provider.complete("synthetic", "synthetic", max_tokens=73, json_mode=json_mode)
        assert result == '{"ok":true}' and "PRIVATE_REASONING_MARKER" not in result
        assert len(seen) == 1
        body = seen[0]
        budget_field = "max_completion_tokens" if connection == "openai" else "max_tokens"
        assert body[budget_field] == 73
        assert ("response_format" in body) is json_mode
        if connection == "ollama":
            assert body["reasoning_effort"] == "none"
        else:
            assert "reasoning_effort" not in body
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_openai_preset_uses_current_completion_budget_without_forcing_sampling():
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    provider = OpenAICompatProvider("https://api.openai.com/v1", "selected-model", transport=httpx.MockTransport(handle), connection="openai")
    try:
        await provider.complete("s", "u", max_tokens=64, json_mode=False)
        assert seen[0]["max_completion_tokens"] == 64
        assert "max_tokens" not in seen[0] and "temperature" not in seen[0]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_anthropic_probe_keeps_small_budget_and_does_not_use_sdk_retry():
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"ok":true}'}]})
    def factory(cfg, rt):
        return AnthropicProvider(api_key="synthetic", use_sdk=True, transport=httpx.MockTransport(handle))
    result = await connection_test.test_connection({"provider": "anthropic"}, None, factory=factory)
    assert result["ok"] and len(seen) == 1
    assert 0 < seen[0]["max_tokens"] <= 128 and "fallbacks" not in seen[0]


@pytest.mark.asyncio
async def test_probe_raw_failure_is_not_exposed_and_unsaved_test_does_not_modify_settings(monkeypatch):
    repo = MemorySettings()
    settings.update(repo, "llm", {"provider": "openai_compat", "openai_connection": "ollama", "openai_model": "installed"})
    saved = deepcopy(repo.saved)
    class Broken:
        name, model = "openai_compat", "installed"
        async def complete(self, *args, **kwargs):
            raise RuntimeError("PRIVATE_PROVIDER_MARKER")
        async def close(self):
            pass
    monkeypatch.setattr(connection_test, "make_provider", lambda *_: Broken())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rt=SimpleNamespace(repo=repo))))
    result = await routes_api.test_llm(request, {"openai_model": "other"}, user=SimpleNamespace())
    assert not result["ok"] and "PRIVATE_PROVIDER_MARKER" not in str(result)
    assert repo.saved == saved


def test_admin_has_preset_inputs_alias_and_no_key_in_html(client):
    html = client.get("/admin").text
    assert 'id="sec-ai"' in html and 'id="sec-llm"' in html
    for item in ("llm-openai-connection", "llm-openai-clear", "Ollama", "LM Studio"):
        assert item in html
