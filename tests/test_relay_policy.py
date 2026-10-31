"""중계 기간·수동 중단은 로컬 기록과 별개이며 외부 호출 없이 검증한다."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import json

import httpx
import pytest

from gukgam import settings
from gukgam.config import KST
from gukgam.relay_policy import normalize_policy, policy_status
from gukgam.relay_lifecycle import RelayLifecycle
from gukgam.runtime import Runtime
from tests.conftest import make_config

POLICY = {"enabled": True, "start_date": "2026-11-01", "end_date": "2026-11-02"}


@pytest.mark.parametrize("now,state", [
    ("2026-10-31T14:59:59+00:00", "scheduled"),
    ("2026-10-31T15:00:00+00:00", "active"),
    ("2026-11-02T14:59:59+00:00", "active"),
    ("2026-11-02T15:00:00+00:00", "expired"),
])
def test_period_includes_end_date_and_uses_korea_not_process_timezone(now, state):
    value = policy_status(POLICY, configured=True, now=datetime.fromisoformat(now))
    assert value["state"] == state and value["active"] == (state == "active")
    assert value["timezone"] == "Asia/Seoul"


@pytest.mark.parametrize("value", [
    {"enabled": "true"}, {"enabled": 1}, {"enabled": True},
    {"start_date": "2026-02-30", "end_date": "2026-03-01"},
    {"start_date": "2026-11-03", "end_date": "2026-11-02"},
    {"start_date": "20261101", "end_date": "2026-11-02"},
    {"start_date": "2026-11-01"}, {"start_date": 20261101},
])
def test_invalid_policy_fails_closed_with_safe_error(value):
    with pytest.raises(ValueError) as error:
        normalize_policy(value)
    assert "2026-02-30" not in str(error.value)
    assert not policy_status(value, configured=True)["active"]


def test_missing_policy_stays_off_and_toggle_keeps_dates(tmp_path):
    rt = Runtime(make_config(tmp_path, cloud_url="https://relay.invalid", agent_token="synthetic-only"))
    try:
        assert settings.get(rt.repo, "relay") == {"enabled": False, "start_date": "", "end_date": ""}
        assert rt.repo.get_setting("relay") is None
        settings.update(rt.repo, "relay", POLICY)
        before = rt.repo.get_setting("relay")
        with pytest.raises(ValueError):
            settings.update(rt.repo, "relay", {"end_date": "bad"})
        assert rt.repo.get_setting("relay") == before
        assert settings.update(rt.repo, "relay", {"enabled": False}) == {**POLICY, "enabled": False}
        assert not rt.relay_status()["active"]
    finally:
        rt.db.close()


async def test_credentials_and_live_collection_do_not_bypass_unconfigured_default(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("미설정 중계는 클라우드 연결을 만들면 안 됩니다")
    monkeypatch.setattr("gukgam.cloudsync.CloudSync", forbidden)
    rt = Runtime(make_config(tmp_path, cloud_url="https://relay.invalid", agent_token="synthetic-only", cloud_read_mode="source"))
    session = rt.repo.create_session(title="합성 수집 보존", source_type="manual")
    rt.repo.update_session(session["id"], status="live")
    try:
        await rt.attach_app(object())
        assert rt.cloud is None and rt.relay_status()["state"] == "unconfigured"
        assert rt.repo.get_session(session["id"])["status"] == "live"
    finally:
        await rt.relay.close()
        rt.db.close()


def test_bootstrap_restarts_preserve_admin_policy_until_wizard_changes(tmp_path):
    config = make_config(tmp_path, desktop_bridge=object(), desktop_relay_policy=POLICY)
    first = Runtime(config)
    settings.update(first.repo, "relay", {"enabled": False})
    first.db.close()
    second = Runtime(config)
    assert settings.get(second.repo, "relay")["enabled"] is False
    second.db.close()
    config.desktop_relay_policy = {**POLICY, "end_date": "2026-11-03"}
    third = Runtime(config)
    try:
        assert settings.get(third.repo, "relay") == config.desktop_relay_policy
    finally:
        third.db.close()


def test_desktop_initial_period_roundtrip_and_legacy_off(tmp_path, monkeypatch):
    from gukgam.desktop_runtime import read_initial_config, engine_config
    path = tmp_path / "initial.json"
    legacy = {"organization": "합성기관", "model": "", "relayUrl": "", "relayToken": ""}
    monkeypatch.setenv("GUKGAM_DATA_DIR", str(tmp_path / "isolated-records"))
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert engine_config(read_initial_config(path), object()).desktop_relay_policy["enabled"] is False
    revision = "e14265df-87cf-4b48-9546-364fa670ff10"
    value = {**legacy, "relayEnabled": True, "relayStartDate": "2026-11-01", "relayEndDate": "2026-11-02", "relayPolicyRevision": revision}
    path.write_text(json.dumps(value), encoding="utf-8")
    assert engine_config(read_initial_config(path), object()).desktop_relay_policy == POLICY
    assert engine_config(read_initial_config(path), object()).desktop_relay_revision == revision
    path.write_text(json.dumps({**value, "relayEnabled": "true"}), encoding="utf-8")
    with pytest.raises(ValueError):
        read_initial_config(path)


def test_explicit_same_policy_revision_is_applied_but_restart_and_general_setup_preserve_admin(tmp_path):
    off = {"enabled": False, "start_date": "", "end_date": ""}
    config = make_config(tmp_path, desktop_bridge=object(), desktop_relay_policy=off,
                         desktop_relay_revision="e14265df-87cf-4b48-9546-364fa670ff10")
    first = Runtime(config)
    settings.update(first.repo, "relay", POLICY)  # 관리 화면에서 켰다.
    first.db.close()
    restart = Runtime(config)
    assert settings.get(restart.repo, "relay") == POLICY  # 단순 재시작은 유지한다.
    restart.db.close()
    config.desktop_relay_revision = "a0dc25ec-70ba-4ef8-80ee-1c00244b3ec0"  # 같은 OFF를 명시 저장.
    explicit_off = Runtime(config)
    assert settings.get(explicit_off.repo, "relay") == off
    settings.update(explicit_off.repo, "relay", {**POLICY, "enabled": False, "end_date": "2026-11-09"})
    explicit_off.db.close()
    config.desktop_organization, config.desktop_model = "새 합성기관", "synthetic-model"
    general_save = Runtime(config)
    assert settings.get(general_save.repo, "relay") == {**POLICY, "enabled": False, "end_date": "2026-11-09"}
    general_save.db.close()
    # 명시 ON 저장은 앞서 관리 화면에서 껐어도 적용한다.
    config.desktop_relay_policy = POLICY
    config.desktop_relay_revision = "1732adf4-e8bf-4ec0-bd16-679753087ccc"
    explicit_on = Runtime(config)
    assert settings.get(explicit_on.repo, "relay") == POLICY
    settings.update(explicit_on.repo, "relay", {"enabled": False})
    explicit_on.db.close()
    same_restart = Runtime(config)
    assert settings.get(same_restart.repo, "relay")["enabled"] is False
    same_restart.db.close()
    config.desktop_relay_revision = "64d399d3-79cc-42fb-9724-31ed7347418f"  # 같은 기간의 ON 재저장.
    same_on_save = Runtime(config)
    try:
        assert settings.get(same_on_save.repo, "relay") == POLICY
    finally:
        same_on_save.db.close()


async def test_period_watcher_expires_without_settings_request(tmp_path, monkeypatch):
    stopped = asyncio.Event()
    class FakeCloud:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self):
            pass
        async def stop(self, *, flush_timeout):
            assert flush_timeout == 0
            stopped.set()
    monkeypatch.setattr("gukgam.cloudsync.CloudSync", FakeCloud)
    rt = Runtime(make_config(tmp_path, cloud_url="https://relay.invalid", agent_token="synthetic-only"))
    settings.update(rt.repo, "relay", POLICY)
    now = [datetime(2026, 11, 2, 23, 59, 59, tzinfo=KST)]
    relay = RelayLifecycle(rt, object(), clock=lambda: now[0], interval_s=.001)
    try:
        await relay.start()
        now[0] = datetime(2026, 11, 3, tzinfo=KST)
        await asyncio.wait_for(stopped.wait(), 1)
        assert rt.cloud is None
    finally:
        await relay.close()
        rt.db.close()


async def test_lifecycle_scheduled_start_manual_off_resume_and_midnight_expiry(tmp_path, monkeypatch):
    calls = []
    class FakeCloud:
        def __init__(self, rt, app, *, connection_allowed):
            self.allowed = connection_allowed
            calls.append("construct")
        async def start(self):
            assert self.allowed()
            calls.append("start")
        async def stop(self, *, flush_timeout):
            assert not self.allowed() and flush_timeout == 0
            calls.append("stop-without-flush")
    monkeypatch.setattr("gukgam.cloudsync.CloudSync", FakeCloud)
    rt = Runtime(make_config(tmp_path, cloud_url="https://relay.invalid", agent_token="synthetic-only"))
    settings.update(rt.repo, "relay", POLICY)
    now = [datetime(2026, 10, 31, 23, 59, 59, tzinfo=KST)]
    relay = RelayLifecycle(rt, object(), clock=lambda: now[0])
    try:
        await relay.start()
        assert rt.cloud is None
        now[0] = datetime(2026, 11, 1, tzinfo=KST)
        await relay.reconcile()
        first = rt.cloud
        await relay.reconcile()
        assert calls == ["construct", "start"]
        settings.update(rt.repo, "relay", {"enabled": False})
        await relay.reconcile(reload=True)
        assert rt.cloud is None
        settings.update(rt.repo, "relay", {"enabled": True})
        await relay.reconcile(reload=True)
        assert rt.cloud is not first
        assert not first.allowed()  # 다시 켜도 폐기한 연결이 요청을 다시 만들 수 없다.
        now[0] = datetime(2026, 11, 3, tzinfo=KST)
        assert not rt.cloud.allowed()  # 감시 tick 전에도 새 요청 경계는 닫힌다.
        await relay.reconcile()
        assert rt.cloud is None and calls.count("stop-without-flush") == 2
    finally:
        await relay.close()
        rt.db.close()


async def test_manual_off_cancels_owned_longpoll_closes_http_without_flush_or_touching_local(tmp_path, monkeypatch):
    from gukgam.cloudsync import CloudSync
    entered, cancelled = asyncio.Event(), asyncio.Event()
    requests = []
    async def transport(request):
        requests.append(request.url.path)
        if request.url.path == "/agent/commands":
            assert request.url.params["wait"] == "20"
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return httpx.Response(200, json={"ok": True, "wanted": False})
    network = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    rt = Runtime(make_config(tmp_path, cloud_url="https://relay.invalid", agent_token="synthetic-only", cloud_read_mode="source"))
    local_session = rt.repo.create_session(title="합성 로컬 보존", source_type="manual")
    settings.update(rt.repo, "relay", POLICY)
    rt.ingest = SimpleNamespace(shutdown=AsyncMock(), stop=AsyncMock())
    rt.analysis = SimpleNamespace(status=lambda: {})
    def factory(owner, app, **kwargs):
        value = CloudSync(owner, app, client=network, **kwargs)
        value._own_client = True
        return value
    monkeypatch.setattr("gukgam.cloudsync.CloudSync", factory)
    relay = RelayLifecycle(rt, SimpleNamespace(state=SimpleNamespace(auth=object())),
                           clock=lambda: datetime(2026, 11, 1, tzinfo=KST))
    try:
        await relay.start()
        await asyncio.wait_for(entered.wait(), 1)
        old = rt.cloud
        settings.update(rt.repo, "relay", {"enabled": False})
        await relay.reconcile(reload=True)
        assert cancelled.is_set() and network.is_closed and old._client is None
        assert old._tasks == [] and not old.command_runner._running and rt.cloud is None
        assert rt.repo.get_session(local_session["id"])["title"] == "합성 로컬 보존"
        rt.ingest.stop.assert_not_awaited()
        rt.ingest.shutdown.assert_not_awaited()
        count = len(requests)
        assert await old._http("/agent/push", {"ops": []}) is None
        assert await old.poll_commands() == []
        assert await old.command_runner._post_result(1, {}) is False
        assert len(requests) == count  # 종료 flush·재시도·새 결과 전송 모두 0.
    finally:
        await relay.close()
        await network.aclose()
        rt.db.close()


def test_settings_api_and_admin_expose_only_safe_local_policy(client, rt):
    result = client.put("/api/settings/relay", json=POLICY)
    assert result.status_code == 200 and result.json() == POLICY
    status = client.get("/api/settings/relay/status").json()
    assert status["configured"] is False
    assert not {"token", "agent_token", "url", "cloud_url"} & status.keys()
    assert client.put("/api/settings/relay", json={"enabled": "false"}).status_code == 422
    assert client.get("/api/settings").json()["relay"] == POLICY
    html = client.get("/admin").text
    assert 'id="sec-relay"' in html and 'id="relay-end-date"' in html


async def test_relay_saves_keep_running_text_summary_signatures_and_local_collection(client, rt, monkeypatch):
    """설정 PUT를 통과해도 실제 stage 허용서명과 요약 입력서명이 유효해야 한다."""
    from copy import deepcopy
    settings.update(rt.repo, "transcript_review", {"enabled": True, "auto_block": False,
                                                   "role_decision_provider": "none"})
    settings.update(rt.repo, "llm", {"provider": "rules", "rolling_interval_sec": 0})
    sid = rt.repo.create_session(title="합성 진행 작업 보존", source_type="manual")["id"]
    rt.repo.update_session(sid, status="live")
    block = rt.repo.create_block(sid, kind="question", ts_start=1000, questioner="합성위원")
    rt.repo.update_block(block["id"], is_open=0, ts_end=2000)
    row, _ = rt.repo.upsert_utterance(sid, "synthetic-progress#0", ts_start=1000, ts_end=2000,
                                     text="합성 자료를 제출해 주세요.", is_final=True,
                                     speaker="합성위원", speaker_role="member")
    rt.repo.update_utterance(row["id"], block_id=block["id"])
    before_session = rt.repo.get_session(sid)
    before_llm = deepcopy(settings.get(rt.repo, "llm"))
    epoch = rt.stages._settings_epoch
    original_reload = rt.reload_settings
    reload_calls = []
    def reload_spy():
        reload_calls.append(True)
        original_reload()
    monkeypatch.setattr(rt, "reload_settings", reload_spy)
    release = asyncio.Event()
    def text_signature():
        return rt.stages._execution_key()
    def summary_signature():
        return (rt.analysis._session_review_signature(sid),
                rt.analysis._session_prompt_context_signature(sid),
                rt.analysis._session_source_signature(rt.repo.list_utterances(sid)))
    initial_text, initial_summary = text_signature(), summary_signature()
    assert all(initial_summary)
    text_started, summary_started = asyncio.Event(), asyncio.Event()
    async def pending_job(signature, expected, started):
        started.set()
        await release.wait()
        return signature() == expected
    text_job = asyncio.create_task(pending_job(text_signature, initial_text, text_started))
    summary_job = asyncio.create_task(pending_job(summary_signature, initial_summary, summary_started))
    await asyncio.gather(text_started.wait(), summary_started.wait())
    try:
        for policy in (POLICY, {"enabled": False}, {"end_date": "2026-11-03"}, {"enabled": True}):
            response = client.put("/api/settings/relay", json=policy)
            assert response.status_code == 200
            assert rt.stages._settings_epoch == epoch
            assert text_signature() == initial_text and summary_signature() == initial_summary
            assert settings.get(rt.repo, "llm") == before_llm
            assert rt.repo.get_session(sid) == before_session
            assert not text_job.done() and not summary_job.done()
        saved = settings.get(rt.repo, "relay")
        response = client.put("/api/settings/relay", json={"start_date": "2026-11-04"})
        assert response.status_code == 422
        assert settings.get(rt.repo, "relay") == saved and rt.stages._settings_epoch == epoch
        assert text_signature() == initial_text and summary_signature() == initial_summary
        assert settings.get(rt.repo, "llm") == before_llm
        assert rt.repo.get_session(sid) == before_session and reload_calls == []
        release.set()
        assert await text_job and await summary_job
        # 중계 외 설정의 기존 공통 재적용은 유지한다.
        assert client.put("/api/settings/retention", json={"audio_days": 7}).status_code == 200
        assert reload_calls == [True] and rt.stages._settings_epoch == epoch + 1
    finally:
        release.set()
        await asyncio.gather(text_job, summary_job)
