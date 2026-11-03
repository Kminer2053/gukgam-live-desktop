"""외부 모델 없이 CPU 작업자의 격리·판본·취소·무절단 계약을 시험한다."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from gukgam.llm import julia
from gukgam.llm.decision import (DecisionError, JEV_MODEL, NullDecisionProvider,
    create_decision_provider, decision_provider_metadata, selected_decision_provider)
from gukgam.llm.julia_worker import encode_row, project_role_rows


CRITERIA = {"member": "질의 의원", "answer": "피감기관 답변", "chair": "진행 위원장", "other": "기타"}
QUESTIONS = {"role_s2": {"type": "choice", "instructions": "역할", "criteria": CRITERIA}}
STATE = {"context": {"questioner": "가위원", "agency": "시험기관", "chair": "나위원장",
                      "roster": [{"name": "가위원", "role": "member"}]},
         "sentences": [{"id": f"s{i}", "text": text} for i, text in enumerate(
             ["첫 문장", "앞 문장", "대상 원문", "뒤 문장", "마지막 문장"])],
         "raw_sources": [{"id": 1, "text": "중복 전체 원문"}], "target_sentence_ids": ["s2"]}


def result(provenance, request_id="synthetic-request"):
    return {"id": request_id, "model": julia.JULIA_MODEL, "provenance": provenance, "answers": {
        "role_s2": {"type": "choice", "choice": "member", "confidence": 0.7,
                    "probabilities": {"member": 0.7, "answer": 0.1, "chair": 0.1, "other": 0.1},
                    "confidence_kind": "uncalibrated_softmax"}}}


def test_factory_default_and_legacy_explicit_choice(tmp_path):
    assert selected_decision_provider({}) == "none"
    assert selected_decision_provider({"use_jev": True}) == "jev"
    assert selected_decision_provider({"use_jev": True, "role_decision_provider": "none"}) == "none"
    assert isinstance(create_decision_provider(settings={}, data_dir=tmp_path), NullDecisionProvider)
    cfg = {"role_decision_provider": "julia", "julia_threads": 1, "julia_max_tokens": 1024}
    provider = create_decision_provider(settings=cfg, data_dir=tmp_path)
    assert provider.provenance == decision_provider_metadata(cfg)
    assert provider.root == tmp_path / "models" / "julia-1"
    assert not provider.available


def test_model_cache_generation_changes_on_equal_size_replacement(tmp_path):
    before = julia.model_fingerprint(tmp_path)
    root = julia.model_directory(tmp_path)
    root.mkdir(parents=True)
    target = root / "model.onnx"
    target.write_bytes(b"test")
    installed = julia.model_fingerprint(tmp_path)
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert before != installed != julia.model_fingerprint(tmp_path)
    assert str(tmp_path) not in julia.model_fingerprint(tmp_path)


def test_runtime_cache_generation_tracks_availability_and_version_without_native_import(tmp_path, monkeypatch):
    origin = tmp_path / "runtime_init.py"
    origin.write_text("synthetic-runtime", encoding="utf-8")
    monkeypatch.setattr(julia.importlib.util, "find_spec", lambda name: None)
    absent = julia.runtime_fingerprint()
    monkeypatch.setattr(julia.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(origin)))
    monkeypatch.setattr(julia.importlib.metadata, "version", lambda name: "synthetic-1")
    present = julia.runtime_fingerprint()
    monkeypatch.setattr(julia.importlib.metadata, "version", lambda name: "synthetic-2")
    assert absent != present != julia.runtime_fingerprint()


@pytest.mark.parametrize("cfg", [{"julia_threads": 3}, {"julia_threads": True},
    {"julia_max_tokens": 8193}, {"julia_request_timeout_seconds": float("nan")},
    {"julia_load_timeout_seconds": 181}])
def test_cpu_and_resource_limits_cannot_be_disabled(tmp_path, cfg):
    with pytest.raises(DecisionError):
        julia.JuliaDecisionProvider(tmp_path, settings=cfg)


def test_julia_softmax_is_not_jev_normalized_confidence():
    response = result(julia.julia_provenance())
    answers = julia.validate_julia_answers(response, QUESTIONS)
    assert answers["role_s2"]["confidence"] == 0.7
    assert answers["role_s2"]["confidence_kind"] == "uncalibrated_softmax"
    assert answers["role_s2"]["provenance"]["weights_sha256"] == julia.JULIA_FILES["model.onnx.data"]["sha256"]


def test_duplicate_protocol_fields_are_rejected():
    with pytest.raises(ValueError):
        julia.protocol_json('{"id":"old","id":"new"}')


@pytest.mark.parametrize("change", [lambda r: r.update(model=JEV_MODEL),
    lambda r: r.update(provenance=None), lambda r: r["provenance"].update(revision="changed"),
    lambda r: r["answers"]["role_s2"].update(confidence=0.6),
    lambda r: r["answers"]["role_s2"].update(confidence_kind="jev_normalized"),
    lambda r: r["answers"]["role_s2"]["probabilities"].update(member=True),
    lambda r: r["answers"].update(role_unknown={})])
def test_changed_model_ids_or_confidence_are_rejected(change):
    response = result(julia.julia_provenance())
    change(response)
    with pytest.raises(DecisionError):
        julia.validate_julia_answers(response, QUESTIONS)


def test_target_projection_keeps_whole_sentences_and_context():
    rows = project_role_rows(STATE, QUESTIONS)
    assert rows[0]["state"]["sentences"] == STATE["sentences"][1:4]
    assert rows[0]["state"]["context"] == STATE["context"]
    assert rows[0]["state"]["target_sentence_id"] == "s2"
    assert "raw_sources" not in rows[0]["state"]
    assert len(rows[0]["options"]) == 4
    assert "인용·전언을 해도 실제 발언자의 역할을 유지" in rows[0]["question"]
    assert "인용" not in rows[0]["options"][-1] and "실제 발언자" in rows[0]["options"][-1]
    assert julia.julia_provenance()["role_prompt_version"] == "actual_speaker_v2"


@pytest.mark.parametrize("change", [lambda s: s["sentences"].append(s["sentences"][0]),
    lambda s: s.update(target_sentence_ids=["s1"]), lambda s: s.update(target_sentence_ids=["missing"]),
    lambda s: s.update(context=None)])
def test_duplicate_or_mismatched_sentence_ids_never_run(change):
    value = deepcopy(STATE)
    change(value)
    with pytest.raises(DecisionError):
        project_role_rows(value, QUESTIONS)


class Tokenizer:
    def encode(self, text, **_):
        return SimpleNamespace(ids=list(range(len(text))))
    def token_to_id(self, name):
        return {"<mask>": 99, "<bos>": 1, "<eos>": 2, "<pad>": 0}[name]


def test_encoding_markers_order_and_no_silent_truncation():
    row = {"state": "전체 원문", "question": "역할?", "options": ["위원", "답변"], "type": "choice"}
    encoded = encode_row(Tokenizer(), row, max_tokens=1024)
    assert encoded["ids"][0] == 1 and encoded["ids"][-1] == 2
    assert [encoded["ids"][position] for position in encoded["markers"]] == [99, 99]
    for bad in ({**row, "state": "가" * 600}, {**row, "options": ["가" * 49, "답변"]},
                {**row, "state": "<mask>"}, {**row, "question": "가" * 600}):
        with pytest.raises(DecisionError):
            encode_row(Tokenizer(), bad, max_tokens=512, head_tokens=256)


class FakeProcess:
    def __init__(self, provider, *, hold=False, bad_id=False, broken=False):
        self.returncode = None
        self.messages = asyncio.Queue()
        self.messages.put_nowait((json.dumps({"event": "ready", "model": julia.JULIA_MODEL,
            "provenance": provider.provenance}) + "\n").encode())
        self.stdout = self
        self.stdin = self
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.sent = asyncio.Event()
        self.provider, self.bad_id, self.broken = provider, bad_id, broken
        self.terminated = False
    async def readline(self):
        message = await self.messages.get()
        if self.sent.is_set():
            await self.release.wait()
        return message
    def write(self, data):
        request = json.loads(data)
        self.sent.set()
        answer = result(self.provider.provenance, "wrong" if self.bad_id else request["id"])
        self.messages.put_nowait(b"sensitive-secret-invalid-json\n" if self.broken else (json.dumps(answer) + "\n").encode())
    async def drain(self):
        await asyncio.sleep(0)
    def close(self):
        pass
    def terminate(self):
        self.returncode = 0
        self.terminated = True
        self.release.set()
        self.messages.put_nowait(b"")
    def kill(self):
        self.terminate()
    async def wait(self):
        return self.returncode


def fake_spawn(monkeypatch, provider, **kwargs):
    child = FakeProcess(provider, **kwargs)
    seen = []
    async def spawn(*args, **options):
        seen.append((args, options))
        return child
    monkeypatch.setattr(julia.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(julia, "model_status", lambda _: {"available": True})
    return child, seen


async def test_cpu_child_reused_and_no_global_python_or_new_port(tmp_path, monkeypatch):
    provider = julia.JuliaDecisionProvider(tmp_path)
    child, seen = fake_spawn(monkeypatch, provider)
    for _ in range(2):
        answer = await provider.evaluate(STATE, QUESTIONS)
        assert answer["role_s2"]["choice"] == "member"
    assert len(seen) == 1
    args, options = seen[0]
    assert "gukgam.llm.julia_worker" in args
    assert options["env"]["CUDA_VISIBLE_DEVICES"] == ""
    assert options["env"]["OMP_NUM_THREADS"] == "2"
    assert options["env"]["HF_HUB_OFFLINE"] == "1"
    if os.name == "nt":
        assert options["creationflags"] & julia.subprocess.CREATE_NO_WINDOW
        assert options["creationflags"] & julia.subprocess.ABOVE_NORMAL_PRIORITY_CLASS
    assert provider.status()["state"] == "ready"
    await provider.close()
    assert child.terminated and provider.status()["state"] == "closed"


async def test_single_concurrency_and_event_loop_keeps_running(tmp_path, monkeypatch):
    provider = julia.JuliaDecisionProvider(tmp_path)
    child, _ = fake_spawn(monkeypatch, provider, hold=True)
    task = asyncio.create_task(provider.evaluate(STATE, QUESTIONS))
    await child.sent.wait()
    for _ in range(5):
        await asyncio.sleep(0)
    with pytest.raises(DecisionError) as exc:
        await provider.evaluate(STATE, QUESTIONS)
    assert exc.value.status == "busy"
    child.release.set()
    await task
    await provider.close()


async def test_cancel_terminates_owned_child_and_next_call_can_restart(tmp_path, monkeypatch):
    provider = julia.JuliaDecisionProvider(tmp_path)
    child, _ = fake_spawn(monkeypatch, provider, hold=True)
    task = asyncio.create_task(provider.evaluate(STATE, QUESTIONS))
    await child.sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.terminated
    second, _ = fake_spawn(monkeypatch, provider)
    assert (await provider.evaluate(STATE, QUESTIONS))["role_s2"]["provider"] == "julia"
    await provider.close()
    assert second.terminated


async def test_close_during_request_does_not_accept_late_response(tmp_path, monkeypatch):
    provider = julia.JuliaDecisionProvider(tmp_path)
    child, _ = fake_spawn(monkeypatch, provider, hold=True)
    task = asyncio.create_task(provider.evaluate(STATE, QUESTIONS))
    await child.sent.wait()
    await provider.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.terminated and provider.status()["state"] == "closed"


@pytest.mark.parametrize("kwargs", [{"bad_id": True}, {"broken": True}])
async def test_stale_or_invalid_worker_response_is_safe(tmp_path, monkeypatch, kwargs):
    provider = julia.JuliaDecisionProvider(tmp_path)
    child, _ = fake_spawn(monkeypatch, provider, **kwargs)
    with pytest.raises(DecisionError) as exc:
        await provider.evaluate(STATE, QUESTIONS)
    assert "sensitive-secret" not in str(exc.value)
    assert child.terminated


async def test_timeout_terminates_child(tmp_path, monkeypatch):
    provider = julia.JuliaDecisionProvider(tmp_path)
    provider.options["request_timeout_seconds"] = 0.01
    child, _ = fake_spawn(monkeypatch, provider, hold=True)
    with pytest.raises(DecisionError) as exc:
        await provider.evaluate(STATE, QUESTIONS)
    assert exc.value.status == "timeout" and child.terminated


async def test_pipe_backpressure_is_inside_request_deadline(tmp_path, monkeypatch):
    provider = julia.JuliaDecisionProvider(tmp_path)
    provider.options["request_timeout_seconds"] = 0.01
    child, _ = fake_spawn(monkeypatch, provider)
    async def blocked_drain():
        await asyncio.Event().wait()
    child.drain = blocked_drain
    with pytest.raises(DecisionError) as exc:
        await provider.evaluate(STATE, QUESTIONS)
    assert exc.value.status == "timeout" and child.terminated


async def test_absent_model_never_starts_worker(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("모델 없이 작업자를 실행하면 안 됩니다")
    monkeypatch.setattr(julia.asyncio, "create_subprocess_exec", forbidden)
    provider = julia.JuliaDecisionProvider(tmp_path)
    with pytest.raises(DecisionError) as exc:
        await provider.evaluate(STATE, QUESTIONS)
    assert exc.value.status == "unavailable"


def small_manifest(monkeypatch):
    payloads = {name: (name + "-검증").encode() for name in julia.JULIA_FILES}
    monkeypatch.setattr(julia, "JULIA_FILES", {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                            for name, data in payloads.items()})
    return payloads


class Download:
    def __init__(self, payloads, *, bad=False, hold=None):
        self.payloads, self.bad, self.hold = payloads, bad, hold
        self.urls = []
        self.status_code = 200
        self.headers = {}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass
    def stream(self, method, url, **kwargs):
        self.urls.append(url)
        self.current = self.payloads[url.rsplit("/", 1)[-1]]
        return self
    async def aiter_raw(self, _size):
        if self.hold:
            await self.hold.wait()
        yield b"bad" if self.bad else self.current


async def test_explicit_prepare_pins_official_files_and_hashes(tmp_path, monkeypatch):
    payloads = small_manifest(monkeypatch)
    download = Download(payloads)
    monkeypatch.setattr(julia.httpx, "AsyncClient", lambda **_: download)
    await julia.prepare_model(tmp_path)
    root = julia.model_directory(tmp_path)
    assert all((root / name).read_bytes() == value for name, value in payloads.items())
    assert all(julia.JULIA_ONNX_REVISION in url for url in download.urls)
    assert not list(root.glob("*.partial"))
    assert julia.model_status(tmp_path)["progress"] == 1
    count = len(download.urls)
    await julia.prepare_model(tmp_path)
    assert len(download.urls) == count


async def test_bad_download_keeps_existing_file_and_cleans_partial(tmp_path, monkeypatch):
    payloads = small_manifest(monkeypatch)
    root = julia.model_directory(tmp_path)
    root.mkdir(parents=True)
    (root / "model.onnx").write_bytes("기존 기록".encode())
    monkeypatch.setattr(julia.httpx, "AsyncClient", lambda **_: Download(payloads, bad=True))
    with pytest.raises(DecisionError):
        await julia.prepare_model(tmp_path)
    assert (root / "model.onnx").read_bytes() == "기존 기록".encode()
    assert not list(root.glob("*.partial"))


async def test_prepare_cancel_does_not_leave_partial_or_unbounded_queue(tmp_path, monkeypatch):
    payloads = small_manifest(monkeypatch)
    hold = asyncio.Event()
    download = Download(payloads, hold=hold)
    monkeypatch.setattr(julia.httpx, "AsyncClient", lambda **_: download)
    task = asyncio.create_task(julia.prepare_model(tmp_path))
    while not download.urls:
        await asyncio.sleep(0)
    with pytest.raises(DecisionError) as exc:
        await julia.prepare_model(tmp_path)
    assert exc.value.status == "busy"
    await julia.cancel_prepare_model(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(julia.model_directory(tmp_path).glob("*.partial"))
    assert julia.model_status(tmp_path)["state"] == "cancelled"
