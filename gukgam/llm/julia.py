"""선택한 Julia-1 CPU 판정을 앱 소유 작업자와 파이프로 실행한다.

공식 원본: https://huggingface.co/SupersonicLabs/Julia-1
공식 ONNX: https://huggingface.co/SupersonicLabs/Julia-1-ONNX
모델 설치는 명시적인 prepare_model 호출만 수행하며 설정 선택은 내려받지 않는다.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
import uuid

import httpx

from .decision import (DecisionError, MAX_RESPONSE_BYTES, _number, validate_answer_set,
                       validate_questions)
from ..assets_download import download_file, verified_file
from ..secure_files import make_private_directory

JULIA_MODEL = "SupersonicLabs/Julia-1"
JULIA_REVISION = "a85b127321d580d65176c89ced8273f305745d85"
JULIA_ONNX_REPO = "SupersonicLabs/Julia-1-ONNX"
JULIA_ONNX_REVISION = "82a2fadf8fccfccdc5fd4e1009ba8f1a265eb7a8"
JULIA_RUNTIME = {"onnxruntime": "1.31.0", "tokenizers": "0.23.3", "numpy": "2.5.4"}
JULIA_FILES = {
    "model.onnx": {"size": 2_988_923, "sha256": "97141d0cfb1da6204e9f8f24d581af72eaeb82cda21149d83eaa6df7160fbcd9"},
    "model.onnx.data": {"size": 576_782_336, "sha256": "fd915be810d7ebfb80fb05a48dd33c9484d17ae1b6bcb9e1f544cbaaa913ded1"},
    "tokenizer.json": {"size": 34_363_188, "sha256": "609d8f4c067cd3950f88594c5a802616cea245823836ef5848ee4fc40aab5b6f"},
}
MAX_PIPE_BYTES = 128_000
PREPARE_TIMEOUT_SECONDS = 1800
_PREPARATIONS: dict[str, asyncio.Task] = {}
_PREPARE_STATUS: dict[str, dict[str, Any]] = {}


def _worker_creation_options() -> dict[str, int]:
    """Julia가 실제로 실행되는 동안만 일반 CPU 음성 작업보다 먼저 배정받는다."""
    if os.name != "nt":
        return {}
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    above_normal = getattr(subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0x00008000)
    return {"creationflags": no_window | above_normal}


def protocol_json(value: str | bytes) -> Any:
    """동일 ID가 중복된 JSON 객체를 마지막 값으로 조용히 채택하지 않는다."""
    def unique(pairs):
        output = {}
        for key, item in pairs:
            if key in output:
                raise ValueError("중복 프로토콜 필드")
            output[key] = item
        return output
    return json.loads(value, object_pairs_hook=unique)


def model_directory(data_dir: Path) -> Path:
    return Path(data_dir).absolute() / "models" / "julia-1"


def model_fingerprint(data_dir: Path) -> str:
    """같은 크기 파일의 재준비도 캐시 세대에 반영한다. 경로·PC 식별자는 포함하지 않는다."""
    root = model_directory(data_dir)
    versions = []
    for name in JULIA_FILES:
        try:
            stat = (root / name).stat()
            versions.append((name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            versions.append((name, None, None))
    return hashlib.sha256(json.dumps(versions, separators=(",", ":")).encode()).hexdigest()


def runtime_fingerprint() -> str:
    """C 런타임을 불러오지 않고 설치 교체 세대를 구분한다. 경로는 해시에도 넣지 않는다."""
    versions = []
    for name in JULIA_RUNTIME:
        try:
            spec = importlib.util.find_spec(name)
        except (ValueError, ImportError):
            spec = None
        version, stamp = None, None
        if spec is not None:
            try:
                version = importlib.metadata.version(name)
            except (importlib.metadata.PackageNotFoundError, OSError, ValueError, UnicodeError):
                pass
            try:
                stat = Path(spec.origin).stat() if spec.origin else None
                stamp = (stat.st_size, stat.st_mtime_ns) if stat else None
            except (OSError, ValueError, TypeError):
                pass
        versions.append((name, spec is not None, version, stamp))
    if getattr(sys, "frozen", False):
        try:
            stat = Path(sys.executable).stat()
            versions.append(("installed-engine", stat.st_size, stat.st_mtime_ns))
        except OSError:
            versions.append(("installed-engine", None, None))
    return hashlib.sha256(json.dumps(versions, separators=(",", ":")).encode()).hexdigest()


def _bounded(settings: dict, key: str, default: float, low: float, high: float, *, integer=False):
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DecisionError("invalid_request", "Julia CPU 실행 제한 설정을 확인하세요")
    if not low <= value <= high or integer and int(value) != value:
        raise DecisionError("invalid_request", "Julia CPU 실행 제한 설정을 확인하세요")
    return int(value) if integer else float(value)


def julia_options(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    return {
        "threads": _bounded(settings, "julia_threads", 2, 1, 2, integer=True),
        "max_tokens": _bounded(settings, "julia_max_tokens", 2048, 512, 8192, integer=True),
        "load_timeout_seconds": _bounded(settings, "julia_load_timeout_seconds", 60, 10, 180),
        "request_timeout_seconds": _bounded(settings, "julia_request_timeout_seconds", 20, 1, 60),
    }


def julia_provenance(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"provider": "julia", "model": JULIA_MODEL, "source_revision": JULIA_REVISION,
            "revision": JULIA_ONNX_REVISION, "model_sha256": JULIA_FILES["model.onnx"]["sha256"],
            "weights_sha256": JULIA_FILES["model.onnx.data"]["sha256"],
            "tokenizer_sha256": JULIA_FILES["tokenizer.json"]["sha256"],
            "runtime": dict(JULIA_RUNTIME), "execution_provider": "CPUExecutionProvider",
            "input_projection": "target_and_adjacent_v2", "role_prompt_version": "actual_speaker_v2", "context_radius": 1,
            "strict_encoding": True, "confidence_kind": "uncalibrated_softmax", **julia_options(settings)}


def model_status(data_dir: Path) -> dict[str, Any]:
    """비용 큰 해시·모델 import 없이 준비 여부만 알린다. 파일 존재는 실행 성공이 아니다."""
    root = model_directory(data_dir)
    present = []
    for name, spec in JULIA_FILES.items():
        try:
            if (root / name).is_file() and (root / name).stat().st_size == spec["size"]:
                present.append(name)
        except OSError:
            pass
    try:
        runtime_present = all(importlib.util.find_spec(name) is not None for name in JULIA_RUNTIME)
    except (ValueError, ImportError):
        runtime_present = False
    available = len(present) == len(JULIA_FILES) and runtime_present
    result = {"state": "idle" if available else "unavailable", "available": available,
              "model_present": len(present) == len(JULIA_FILES), "runtime_present": runtime_present,
              "message": "Julia 파일 준비됨 · CPU 실행 전" if available else
              ("Julia CPU 실행 도구가 설치판에 없습니다" if len(present) == len(JULIA_FILES) else
               "Julia 모델을 준비해야 합니다. 기존 ChatGPT 처리로 계속할 수 있습니다"),
              "provenance": julia_provenance()}
    return {**result, **_PREPARE_STATUS.get(str(root), {})}


def validate_julia_answers(response: Any, questions: dict[str, Any]) -> dict[str, Any]:
    answers = validate_answer_set(response, questions, model=JULIA_MODEL)
    provenance = response.get("provenance")
    if (list(answers) != list(questions) or not isinstance(provenance, dict)
            or provenance.get("revision") != JULIA_ONNX_REVISION):
        raise DecisionError("invalid_response", "Julia 응답의 판본·문장 순서가 일치하지 않습니다")
    expected = julia_provenance()
    for key in ("provider", "model", "source_revision", "revision", "model_sha256", "weights_sha256",
                "tokenizer_sha256", "runtime", "execution_provider", "input_projection", "confidence_kind"):
        if provenance.get(key) != expected[key]:
            raise DecisionError("invalid_response", "Julia 응답의 모델 출처가 검증값과 다릅니다")
    for answer in answers.values():
        if answer.get("type") != "choice" or answer.get("confidence_kind") != "uncalibrated_softmax":
            raise DecisionError("invalid_response", "Julia 역할 응답의 출처가 잘못되었습니다")
        confidence = _number(answer.get("confidence"))
        if abs(confidence - max(answer["probabilities"].values())) > 1e-6:
            raise DecisionError("invalid_response", "Julia 선택 확률 통계가 일치하지 않습니다")
        answer.update(provider="julia", model=JULIA_MODEL, provenance=dict(response["provenance"]))
    return answers


async def prepare_model(data_dir: Path) -> dict[str, Any]:
    """명시적으로 승인한 준비 요청. 고정 공식 파일만 해시 확인 후 원자 교체한다."""
    root = model_directory(data_dir)
    key = str(root)
    if key in _PREPARATIONS and not _PREPARATIONS[key].done():
        raise DecisionError("busy", "Julia 모델 준비가 이미 진행 중입니다")
    task = asyncio.create_task(_prepare(root), name="julia-model-prepare")
    _PREPARATIONS[key] = task
    try:
        await asyncio.wait_for(task, PREPARE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _PREPARE_STATUS[key] = {"state": "error", "message": "Julia 모델 준비 시간 한도를 넘었습니다"}
        raise DecisionError("timeout", "Julia 모델 준비 시간 한도를 넘었습니다") from None
    except asyncio.CancelledError:
        _PREPARE_STATUS[key] = {"state": "cancelled", "message": "Julia 모델 준비를 취소했습니다"}
        raise
    finally:
        if _PREPARATIONS.get(key) is task:
            _PREPARATIONS.pop(key, None)
    return model_status(data_dir)


async def cancel_prepare_model(data_dir: Path) -> dict[str, Any]:
    key = str(model_directory(data_dir))
    task = _PREPARATIONS.get(key)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return model_status(data_dir)


async def _prepare(root: Path) -> None:
    key = str(root)
    total = sum(spec["size"] for spec in JULIA_FILES.values())
    received = 0
    try:
        await asyncio.to_thread(make_private_directory, root, parents=True, exist_ok=True)
        free = await asyncio.to_thread(lambda: shutil.disk_usage(root).free)
        if free < total + 128 * 1024 * 1024:
            raise DecisionError("disk_space", "Julia 모델 준비에 약 750MB 이상의 여유 공간이 필요합니다")
        _PREPARE_STATUS[key] = {"state": "preparing", "message": "공식 Julia 모델을 확인하고 있습니다",
                                "bytes": 0, "total_bytes": total, "progress": 0}
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(30, connect=10)) as client:
            for name, spec in JULIA_FILES.items():
                target = root / name
                if await asyncio.to_thread(_verified_file, target, spec):
                    received += spec["size"]
                    continue
                url = f"https://huggingface.co/{JULIA_ONNX_REPO}/resolve/{JULIA_ONNX_REVISION}/{name}"
                def progress(size):
                    _PREPARE_STATUS[key].update(bytes=received + size,
                        progress=round((received + size) / total, 4))
                await download_file(client, url, target, spec["size"], spec["sha256"], progress=progress)
                received += spec["size"]
        _PREPARE_STATUS[key] = {"state": "idle", "message": "Julia 모델 해시 확인 완료 · CPU 실행 전",
                                "bytes": total, "total_bytes": total, "progress": 1}
    except asyncio.CancelledError:
        _PREPARE_STATUS[key] = {"state": "cancelled", "message": "Julia 모델 준비를 취소했습니다"}
        raise
    except Exception as exc:
        safe = exc if isinstance(exc, DecisionError) else DecisionError("download_failed", "Julia 모델 준비에 실패했습니다. 기존 처리로 계속할 수 있습니다")
        _PREPARE_STATUS[key] = {"state": "error", "message": str(safe)}
        raise safe from None


def _verified_file(path: Path, spec: dict[str, Any]) -> bool:
    return verified_file(path, spec["size"], spec["sha256"])


class JuliaDecisionProvider:
    """메인 루프에서는 파이프만 기다린다. 로딩·토큰화·추론은 단일 자식에서 수행한다."""

    name = "julia"
    model = JULIA_MODEL
    max_batch_sentences = 4

    def __init__(self, data_dir: Path, *, settings: dict[str, Any] | None = None):
        self.data_dir = Path(data_dir)
        self.root = model_directory(data_dir)
        self.options = julia_options(settings)
        self.call_timeout_seconds = self.options["load_timeout_seconds"] + self.options["request_timeout_seconds"] + 3
        self._provenance = julia_provenance(settings)
        self._child: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._state = "idle"
        self._message = "Julia CPU 실행 전"

    @property
    def provenance(self) -> dict[str, Any]:
        return {**self._provenance, "runtime": dict(self._provenance["runtime"])}

    @property
    def decision_metadata(self) -> dict[str, Any]:
        return self.provenance

    @property
    def available(self) -> bool:
        return not self._closed and bool(model_status(self.data_dir)["available"])

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "message": self._message, "available": self.available,
                "provenance": self.provenance}

    async def _read(self) -> dict[str, Any]:
        assert self._child is not None and self._child.stdout is not None
        try:
            line = await self._child.stdout.readline()
            if not line or len(line) > MAX_RESPONSE_BYTES:
                raise ValueError
            result = protocol_json(line)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise DecisionError("invalid_response", "Julia 작업자 응답을 확인하지 못했습니다") from None

    async def _start(self) -> None:
        if self._child is not None and self._child.returncode is None:
            return
        self._state, self._message = "loading", "Julia 모델을 CPU 작업자에 불러오고 있습니다"
        args = ([sys.executable, "--julia-worker"] if getattr(sys, "frozen", False) else
                [sys.executable, "-m", "gukgam.llm.julia_worker"])
        args += ["--model-dir", str(self.root), "--threads", str(self.options["threads"]),
                 "--max-tokens", str(self.options["max_tokens"]),
                 "--load-timeout-seconds", str(self.options["load_timeout_seconds"]),
                 "--request-timeout-seconds", str(self.options["request_timeout_seconds"])]
        env = {**os.environ, "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": str(self.options["threads"]),
               "MKL_NUM_THREADS": str(self.options["threads"]), "TOKENIZERS_PARALLELISM": "false",
               "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        self._child = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
            limit=MAX_PIPE_BYTES, **_worker_creation_options())
        result = await asyncio.wait_for(self._read(), self.options["load_timeout_seconds"])
        if result.get("event") != "ready" or result.get("model") != self.model or result.get("provenance") != self.provenance:
            status = result.get("status") if result.get("status") in {"invalid_model", "runtime_unavailable"} else "unavailable"
            raise DecisionError(status, "Julia CPU 모델을 준비하지 못했습니다. 기존 처리로 계속합니다")
        self._state, self._message = "ready", "Julia CPU 작업자 준비 완료"

    async def evaluate(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        validate_questions(state, questions)
        for question in questions.values():
            if question["type"] != "choice" or set(question["criteria"]) != {"member", "answer", "chair", "other"}:
                raise DecisionError("unsupported_scope", "Julia는 문장별 네 화자 역할만 판정합니다")
        if not self.available:
            raise DecisionError("unavailable", "Julia 모델 또는 CPU 실행 도구가 준비되지 않았습니다. 기존 처리로 계속합니다")
        if self._lock.locked():
            raise DecisionError("busy", "Julia 역할 판정이 진행 중입니다. 추가 대기열은 만들지 않습니다")
        async with self._lock:
            rid = uuid.uuid4().hex
            request = json.dumps({"id": rid, "state": state, "questions": questions}, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            if len(request) > MAX_PIPE_BYTES:
                raise DecisionError("input_limit", "Julia 검토 전송 한도를 넘었습니다. 입력을 자르지 않았습니다")
            try:
                await self._start()
                self._state, self._message = "running", "Julia CPU 역할 판정 중"
                assert self._child is not None and self._child.stdin is not None
                async def exchange():
                    assert self._child is not None and self._child.stdin is not None
                    self._child.stdin.write(request)
                    await self._child.stdin.drain()
                    return await self._read()
                result = await asyncio.wait_for(exchange(), self.options["request_timeout_seconds"])
                if self._closed:
                    raise asyncio.CancelledError
                if result.get("id") != rid:
                    raise DecisionError("invalid_response", "Julia 응답의 요청 번호가 일치하지 않습니다")
                if result.get("error"):
                    status = result.get("status") if result.get("status") in {"input_limit", "invalid_request"} else "failed"
                    raise DecisionError(status, "Julia 역할 판정을 완료하지 못했습니다. 기존 처리로 계속합니다")
                if result.get("provenance") != self.provenance:
                    raise DecisionError("invalid_response", "Julia 응답의 실행 설정 판본이 일치하지 않습니다")
                answers = validate_julia_answers(result, questions)
                self._state, self._message = "ready", "Julia CPU 역할 판정 완료"
                return answers
            except asyncio.CancelledError:
                await self._stop_child()
                self._state, self._message = "closed" if self._closed else "idle", "Julia 역할 판정을 취소했습니다"
                raise
            except Exception as exc:
                await self._stop_child()
                if self._closed:
                    self._state, self._message = "closed", "Julia CPU 작업자를 해제했습니다"
                    raise asyncio.CancelledError from None
                safe = exc if isinstance(exc, DecisionError) else DecisionError("timeout" if isinstance(exc, TimeoutError) else "failed", "Julia CPU 처리에 실패했습니다. 기존 처리로 계속합니다")
                self._state, self._message = "error", str(safe)
                raise safe from None

    async def _stop_child(self) -> None:
        child, self._child = self._child, None
        if child is None or child.returncode is not None:
            return
        try:
            if child.stdin is not None:
                child.stdin.close()
            child.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(child.wait(), 2)
        except asyncio.TimeoutError:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            await child.wait()

    async def close(self) -> None:
        self._closed = True
        await self._stop_child()
        self._state, self._message = "closed", "Julia CPU 작업자를 해제했습니다"
