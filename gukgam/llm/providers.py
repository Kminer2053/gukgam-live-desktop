"""LLM 제공자(local_mlx · openai_compat · anthropic · rules)와 JSON 파싱, 규칙 기반 요약.

모든 제공자는 complete(system, user) → 문자열(JSON)만 책임진다. 실패하면 LLMError 를 던지고,
대체(rules) 처리는 AnalysisService 가 한다. 규칙 기반 요약(rules_block_summary / rules_session_summary)은
LLM 없이도 같은 형태의 결과를 만드는 '항상 동작하는' 경로이며 보고서 모듈도 쓴다.
"""
from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from ..config import KST, ROOT
from ..db import now_ms
from ..nlp import rules
from ..nlp.text import split_sentences
from ..serialize import hms
from . import prompts

log = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
ANTHROPIC_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
# 서버측 거절 대체(fallbacks="default")를 지원하는 모델
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# output_config.effort 를 받는 모델(접두어)
_EFFORT_PREFIXES = ("claude-opus-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-5",
                    "claude-sonnet-4-6", "claude-fable-", "claude-mythos-")


class LLMError(RuntimeError):
    """제공자 호출 실패(연결·시간초과·인증·거절 등)."""


class LLMParseError(LLMError):
    """응답을 JSON 으로 읽지 못함."""


class LLMRequestStale(LLMError):
    """로컬 모델 대기 중 입력·승인 상태가 바뀌어 요청을 보내지 않음."""


# ── JSON 파싱 ─────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def _scan(s: str) -> tuple[int | None, list[tuple[int, list[str]]], list[str], bool]:
    """s(첫 글자 '{')를 훑어 (짝 맞는 끝 인덱스, 최상위 밖 쉼표 위치·스택, 남은 스택, 문자열 안 여부)."""
    stack: list[str] = []
    commas: list[tuple[int, list[str]]] = []
    in_str = esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                return i + 1, commas, [], False
        elif ch == ",":
            commas.append((i, list(stack)))
    return None, commas, stack, in_str


def _repair_truncated(s: str, commas: list[tuple[int, list[str]]], stack: list[str], in_str: bool) -> Any:
    """max_tokens 로 잘린 JSON 을 마지막 완결 요소까지 살려 닫는다."""
    tail = s + ('"' if in_str else "")
    cands = [tail + "".join(reversed(stack))]
    for pos, st in reversed(commas[-60:]):
        cands.append(s[:pos] + "".join(reversed(st)))
    for c in cands:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise LLMParseError("잘린 JSON 복구 실패")


_THINK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_THINK_OPEN = re.compile(r"<think(?:ing)?>", re.I)
_THINK_CLOSE = re.compile(r"</think(?:ing)?>", re.I)
# 요약 결과로 볼 수 있는 최상위 키(생각 중의 초안·스키마 예시보다 이것이 있는 마지막 객체를 고른다)
_RESULT_KEYS = ("one_line", "overview", "question_points")


def _strip_think(s: str) -> str:
    """추론형 모델의 <think>…</think> 를 지운다. 닫는 태그만 있으면 그 뒤, 여는 태그만 있으면 그 앞만 남긴다."""
    s = _THINK.sub("", s)
    closes = list(_THINK_CLOSE.finditer(s))
    if closes:                       # 템플릿이 <think> 를 프롬프트에 넣은 경우: 마지막 </think> 뒤가 답
        s = s[closes[-1].end():]
    m = _THINK_OPEN.search(s)
    if m:                            # 생각이 끝나지 않고 잘림: 그 앞까지만
        s = s[:m.start()]
    return s


def _loads_dict(cand: str) -> dict[str, Any] | None:
    for c in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_json(text: str) -> dict[str, Any]:
    """<think> 제거 → 코드펜스 안쪽 → 최상위 '{…}' 후보를 모두 시도해 요약 키가 있는 마지막 객체를 고른다.

    서두 문장에 '{one_line, …}' 같은 중괄호가 있어도 다음 후보로 넘어가고, 끝 쉼표·잘림(max_tokens)은 보정한다.
    """
    if not text or not text.strip():
        raise LLMParseError("빈 응답")
    s = _strip_think(text).strip()
    if not s:
        raise LLMParseError("빈 응답(생각 과정만 있음)")
    fences = [m.group(1) for m in _FENCE.finditer(s) if "{" in m.group(1)]
    if fences:
        s = "\n".join(fences)
    found: list[dict[str, Any]] = []
    repaired: dict[str, Any] | None = None
    syntax_error = False
    pos = s.find("{")
    if pos < 0:
        raise LLMParseError("JSON 객체를 찾지 못함")
    while pos >= 0:
        sub = s[pos:]
        end, commas, stack, in_str = _scan(sub)
        if end is None:
            # 닫히지 않은 객체: max_tokens 로 잘린 답이면 복구하고 끝낸다. 복구가 안 되면(서두의 짝 없는 '{' 등)
            # 다음 후보로 넘어간다
            try:
                repaired = _repair_truncated(sub, commas, stack, in_str)
                break
            except LLMParseError:
                syntax_error = True
                pos = s.find("{", pos + 1)
                continue
        obj = _loads_dict(sub[:end])
        if obj is None:
            syntax_error = True
            pos = s.find("{", pos + 1)          # 서두의 '{설명}' 등 → 다음 후보
            continue
        found.append(obj)
        pos = s.find("{", pos + end)            # 안쪽 객체는 건너뛴다
    cands = found + ([repaired] if repaired is not None else [])
    for obj in reversed(cands):
        if any(k in obj for k in _RESULT_KEYS):
            return obj
    if repaired is not None:
        return repaired
    if found:
        return found[-1]
    raise LLMParseError("JSON 문법 오류" if syntax_error else "JSON 객체를 찾지 못함")


# ── 제공자 ────────────────────────────────────────────────────────

class LLMProvider(ABC):
    name: str = ""
    model: str = ""

    @abstractmethod
    async def complete(self, system: str, user: str, max_tokens: int = 1200, json_mode: bool = True) -> str:
        """시스템·사용자 프롬프트 → 모델 출력 문자열. 실패 시 LLMError."""

    async def close(self) -> None:
        return None

    def info(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model}


def _hf_cached(repo: str) -> bool:
    if os.path.isdir(repo):
        return True
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"), "hub")
    snap = Path(hub) / ("models--" + repo.replace("/", "--")) / "snapshots"
    return snap.is_dir() and any(snap.iterdir())


async def _request_allowed(guard: Callable | None) -> bool:
    """보정 전용 실행기의 판본 검사를 수신 루프를 막지 않고 기다린다."""
    if guard is None:
        return True
    value = guard()
    return bool(await value) if inspect.isawaitable(value) else bool(value)


async def _guarded_remote_call(call: Callable, guard: Callable | None, timeout: float):
    """대기·재요청 중에도 원문 판본 취소를 확인하고 소유 HTTP 작업만 취소한다."""
    if not await _request_allowed(guard):
        raise LLMRequestStale("원문 또는 설정이 바뀌어 AI 요청을 취소했습니다")
    task = asyncio.create_task(call())
    try:
        async with asyncio.timeout(timeout):
            while not task.done():
                await asyncio.wait({task}, timeout=.05)
                if not await _request_allowed(guard):
                    raise LLMRequestStale("원문 또는 설정이 바뀌어 AI 요청을 취소했습니다")
            result = task.result()
            if not await _request_allowed(guard):
                raise LLMRequestStale("원문 또는 설정이 바뀌어 AI 요청을 취소했습니다")
            return result
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class LocalMLXProvider(LLMProvider):
    """`python -m gukgam.llm.mlx_worker` 서브프로세스(JSON lines). 첫 요청 때 띄우고, 죽으면 다음 요청에 다시 띄운다."""

    name = "local_mlx"

    def __init__(self, model: str = DEFAULT_LOCAL_MODEL, *, load_timeout: float = 600.0,
                 request_timeout: float | None = None, python: str | None = None,
                 progress_callback=None):
        self.model = model or DEFAULT_LOCAL_MODEL
        self.load_timeout = load_timeout
        self.request_timeout = request_timeout
        self.python = python or sys.executable
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self._stderr: deque[str] = deque(maxlen=40)
        self._stderr_task: asyncio.Task | None = None
        self.load_ms: int | None = None
        self.last_stats: dict[str, Any] = {}
        self.last_used = 0.0
        self.progress_callback = progress_callback
        self.last_progress: list[dict[str, Any]] = []

    def _progress(self, event: str, **values) -> None:
        """본문·토큰·키 없이 단계별 시간과 생성량만 관찰한다."""
        record = {"event": event, "monotonic": time.monotonic(),
                  **{k: v for k, v in values.items() if k in
                     {"load_ms", "elapsed_ms", "gen_tokens", "prompt_tokens", "first_token_ms"}
                     and type(v) in (int, float) and math.isfinite(v) and v >= 0}}
        self.last_progress.append(record)
        self.last_progress = self.last_progress[-128:]
        if self.progress_callback:
            try:
                self.progress_callback(dict(record))
            except Exception:
                log.warning("로컬 모델 단계 기록에 실패했습니다")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def info(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "loaded": self.running, "load_ms": self.load_ms,
                **({"last": self.last_stats} if self.last_stats else {})}

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self._stderr.append(line.decode("utf-8", "replace").rstrip()[:300])

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover - 방어
                pass
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None

    async def _spawn(self) -> None:
        self._progress("worker_start")
        env = os.environ.copy()
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        if _hf_cached(self.model):
            env["HF_HUB_OFFLINE"] = "1"   # 망분리 대비: 캐시에 있으면 네트워크를 쓰지 않는다
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            self.python, "-m", "gukgam.llm.mlx_worker", "--model", self.model,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(ROOT), env=env, limit=16 * 1024 * 1024)
        self._proc = proc
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc), name="mlx-llm-stderr")
        assert proc.stdout is not None
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.load_timeout)
                if not line:
                    raise LLMError("로컬 모델 프로세스가 기동 중 종료됨: " + " / ".join(list(self._stderr)[-3:]))
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("event") == "ready":
                    self.load_ms = msg.get("load_ms") or int((time.perf_counter() - t0) * 1000)
                    self._progress("load_ready", load_ms=self.load_ms)
                    log.info("로컬 LLM 기동: %s (%sms)", self.model, self.load_ms)
                    return
                if msg.get("event") == "fatal":
                    raise LLMError(msg.get("error") or "로컬 모델 기동 실패")
        except asyncio.TimeoutError:
            await self._kill()
            raise LLMError(f"로컬 모델 기동 시간 초과({int(self.load_timeout)}초)") from None
        except BaseException:
            await self._kill()
            raise

    async def _ensure(self) -> asyncio.subprocess.Process:
        if not self.running:
            await self._kill()
            await self._spawn()
        assert self._proc is not None
        return self._proc

    async def complete(self, system: str, user: str, max_tokens: int = 1200, json_mode: bool = True,
                       *, execution_timeout: float | None = None,
                       request_guard: Callable[[], bool | Awaitable[bool]] | None = None,
                       direct_output: bool = False, response_prefix: str = "", stop_line: str = "") -> str:
        if not isinstance(stop_line,str) or len(stop_line)>32 or (stop_line and not re.fullmatch(r"[A-Z_]+",stop_line)):
            raise ValueError("모델 출력 종료 행이 잘못되었습니다")
        if stop_line and (json_mode or not direct_output):
            raise ValueError("종료 행은 직접 응답 요청에서만 사용할 수 있습니다")
        if not isinstance(response_prefix,str) or len(response_prefix)>100 or "\n" in response_prefix:
            raise ValueError("모델 출력 시작 형식이 잘못되었습니다")
        if response_prefix and (json_mode or not direct_output):
            raise ValueError("출력 시작 형식은 직접 응답 요청에서만 사용할 수 있습니다")
        if execution_timeout is not None and (isinstance(execution_timeout, bool)
                or not isinstance(execution_timeout, (int, float)) or not math.isfinite(execution_timeout)
                or execution_timeout <= 0):
            raise ValueError("로컬 모델 실행 제한 시간은 유한한 양수여야 합니다")
        async with self._lock:
            if not await _request_allowed(request_guard):
                raise LLMRequestStale("로컬 모델 대기 중 입력·자동 검토 상태가 바뀌었습니다")
            proc = await self._ensure()
            if not await _request_allowed(request_guard):
                raise LLMRequestStale("로컬 모델 준비 중 입력·자동 검토 상태가 바뀌었습니다")
            assert proc.stdin is not None and proc.stdout is not None
            self._seq += 1
            rid = str(self._seq)
            req = {"id": rid, "system": system, "user": user, "max_tokens": max_tokens, "json_mode": json_mode}
            if direct_output:
                req["direct_output"] = True
            if response_prefix:
                req["response_prefix"] = response_prefix
            if stop_line:
                req["stop_line"] = stop_line
            if self.progress_callback:
                req["progress"] = True
            timeout = execution_timeout or self.request_timeout or (60.0 + max_tokens * 0.15 + len(user) / 400)
            deadline = time.monotonic() + timeout
            try:
                proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
                await proc.stdin.drain()
                self._progress("request_sent")
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=max(0, deadline - time.monotonic()))
                    if not line:
                        raise LLMError("로컬 모델 프로세스가 종료됨: " + " / ".join(list(self._stderr)[-3:]))
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("id") == rid:
                        if msg.get("event") in {"first_token", "generation_progress"}:
                            self._progress(msg["event"], **{k: v for k, v in msg.items() if k not in {"id", "event"}})
                            continue
                        break
            except asyncio.CancelledError:
                await self._kill()
                raise
            except asyncio.TimeoutError:
                await self._kill()   # 다음 요청 때 새로 띄운다
                raise LLMError(f"로컬 모델 응답 시간 초과({int(timeout)}초)") from None
            except (BrokenPipeError, ConnectionResetError) as e:
                await self._kill()
                raise LLMError(f"로컬 모델 프로세스 통신 실패: {e}") from None
            except LLMError:
                await self._kill()
                raise
            self.last_used = time.monotonic()
            if "error" in msg:
                raise LLMError("로컬 모델 오류: " + str(msg["error"]))
            self.last_stats = {k: msg.get(k) for k in ("prompt_tokens", "gen_tokens", "gen_tps", "elapsed_ms")}
            self._progress("response_complete", **self.last_stats)
            return str(msg.get("text") or "")

    async def close(self) -> None:
        async with self._lock:
            proc = self._proc
            if proc is not None and proc.returncode is None and proc.stdin is not None:
                try:
                    proc.stdin.write(b'{"op": "shutdown"}\n')
                    await proc.stdin.drain()
                    proc.stdin.close()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                    pass
            await self._kill()


class OpenAICompatProvider(LLMProvider):
    """사내 vLLM·Ollama 등 OpenAI 호환 `/chat/completions`."""

    name = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str = "", *, timeout: float = 180.0,
                 transport: httpx.AsyncBaseTransport | None = None, connection: str = "custom"):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.api_key = api_key or ""
        self.timeout = timeout
        self._transport = transport
        self.connection = connection
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0),
                                             transport=self._transport, follow_redirects=False)
        return self._client

    async def complete(self, system: str, user: str, max_tokens: int = 1200, json_mode: bool = True, *,
                       execution_timeout: float | None = None, request_guard: Callable | None = None,
                       direct_output: bool = False, response_prefix: str = "", stop_line: str = "") -> str:
        if not self.base_url:
            raise LLMError("OpenAI 호환 서버 주소(openai_base_url)가 설정되지 않았습니다")
        from .compat_config import normalize_base_url, credential_for_endpoint
        try:
            self.base_url = normalize_base_url(self.base_url)
            credential_for_endpoint({"openai_api_key": self.api_key}, endpoint=self.base_url)
        except ValueError:
            raise LLMError("AI 서버 주소와 API 키 입력 칸을 확인하세요.") from None
        if not self.model.strip():
            raise LLMError("AI 서버에서 사용할 모델 이름을 입력하세요.")
        if response_prefix or stop_line:
            raise LLMError("이 연결은 완성된 JSON 응답을 사용합니다.")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict[str, Any] = {"model": self.model,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.connection == "openai" or httpx.URL(self.base_url).host == "api.openai.com":
            body["max_completion_tokens"] = max_tokens
        else:
            body.update(max_tokens=max_tokens, temperature=0.2)
        if self.connection == "ollama":
            # 구조화 정리는 사고 출력 대신 최종 답변에 기존 생성 예산을 사용한다.
            body["reasoning_effort"] = "none"
        url = self.base_url + "/chat/completions"
        async def send():
            if json_mode:
                r = await self._http().post(url, json={**body, "response_format": {"type": "json_object"}},
                                            headers=headers, follow_redirects=False)
                if r.status_code in (400, 404, 415, 422, 500, 501):
                    # response_format 미지원 서버 → 없이 재시도
                    if not await _request_allowed(request_guard):
                        raise LLMRequestStale("원문 또는 설정이 바뀌어 AI 요청을 취소했습니다")
                    r = await self._http().post(url, json=body, headers=headers, follow_redirects=False)
            else:
                r = await self._http().post(url, json=body, headers=headers, follow_redirects=False)
            return r
        try:
            r = await _guarded_remote_call(send, request_guard, execution_timeout or self.timeout)
        except (httpx.HTTPError, TimeoutError):
            raise LLMError("AI 서버에 연결하지 못했습니다. 주소·실행 상태·응답 시간을 확인하세요.") from None
        if 300 <= r.status_code < 400:
            raise LLMError("AI 서버가 다른 주소로 이동을 요청했습니다. 서버 주소를 직접 확인하세요.")
        if r.status_code in (401, 403):
            raise LLMError("AI 서버 인증을 확인하세요. API 키와 사용 권한을 확인하세요.")
        if r.status_code == 429:
            raise LLMError("AI 서버의 이용 한도를 확인하세요.")
        if r.status_code >= 400:
            raise LLMError("AI 서버가 요청을 처리하지 못했습니다. 모델과 서버 상태를 확인하세요.")
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("OpenAI 호환 서버 응답 형식 오류") from None
        if isinstance(content, list):  # 일부 서버는 content 를 블록 목록으로 준다
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict) and isinstance(c.get("text", ""), str))
        if not isinstance(content, str) or not content.strip() or self.api_key and self.api_key in content:
            raise LLMError("AI 서버 응답 형식을 확인하세요.")
        return content

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class DesktopChatGPTProvider(LLMProvider):
    """계정·토큰·HTTP 클라이언트 없이 Electron에 텍스트 요청만 전달한다."""

    name = "desktop_chatgpt"

    def __init__(self, model: str = "", bridge: Any = None):
        self.model = model or ""
        self.bridge = bridge
        self._closed = False

    @property
    def available(self) -> bool:
        return self.bridge is not None and not self._closed and not self.bridge.closed

    async def complete(self, system: str, user: str, max_tokens: int = 1200, json_mode: bool = True,
                       *, execution_timeout: float | None = None, request_guard: Callable | None = None,
                       direct_output: bool = False, response_prefix: str = "", stop_line: str = "") -> str:
        if not self.available:
            raise LLMError("데스크톱 ChatGPT 연결이 준비되지 않았습니다")
        if response_prefix or stop_line:
            raise LLMError("데스크톱 연결은 직접 JSON 응답만 지원합니다")
        return await self.bridge.request(system, user, max_tokens,
                                         timeout=execution_timeout if execution_timeout is not None else 180,
                                         guard=request_guard)

    async def close(self) -> None:
        # 제공자 교체는 연결 전체를 닫지 않는다. 진행 중 요청은 상위 작업 취소로 정리한다.
        self._closed = True


class AnthropicProvider(LLMProvider):
    """Claude Messages API. anthropic SDK 가 설치돼 있으면 SDK, 없으면 httpx 로 직접 호출한다."""

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_ANTHROPIC_MODEL, api_key: str = "", *, base_url: str = ANTHROPIC_URL,
                 timeout: float = 180.0, effort: str = "medium", use_sdk: bool | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.effort = effort
        self._transport = transport
        if use_sdk is None:
            try:
                import anthropic  # noqa: F401
                use_sdk = transport is None
            except ImportError:
                use_sdk = False
        self.use_sdk = use_sdk
        self._sdk: Any = None
        self._client: httpx.AsyncClient | None = None
        self._fallbacks = self.model in _FALLBACK_MODELS

    def _params(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        # 생각(thinking) 토큰도 max_tokens 에 포함되므로 넉넉히 잡는다
        p: dict[str, Any] = {"model": self.model, "max_tokens": max(max_tokens, 8000), "system": system,
                             "messages": [{"role": "user", "content": user}]}
        if self.effort and self.model.startswith(_EFFORT_PREFIXES):
            p["output_config"] = {"effort": self.effort}
        return p

    async def complete(self, system: str, user: str, max_tokens: int = 1200, json_mode: bool = True, *,
                       execution_timeout: float | None = None, request_guard: Callable | None = None,
                       direct_output: bool = False, response_prefix: str = "", stop_line: str = "",
                       connection_probe: bool = False) -> str:
        if not self.api_key:
            raise LLMError("Anthropic API 키가 없습니다(설정 > AI 엔진의 anthropic_api_key 또는 ANTHROPIC_API_KEY)")
        if response_prefix or stop_line or any(c in self.api_key for c in "\r\n\x00"):
            raise LLMError("AI 연결 설정을 확인하세요.")
        params = self._params(system, user, max_tokens)
        if connection_probe:
            params["max_tokens"] = max_tokens
            params.pop("output_config", None)
        async def send():
            if connection_probe:
                return await self._complete_http(params, request_guard=request_guard, allow_fallbacks=False)
            return await (self._complete_sdk(params, request_guard=request_guard) if self.use_sdk
                          else self._complete_http(params, request_guard=request_guard))
        try:
            content = await _guarded_remote_call(send, request_guard, execution_timeout or self.timeout)
        except (httpx.HTTPError, TimeoutError):
            raise LLMError("Anthropic API 연결 상태와 응답 시간을 확인하세요.") from None
        if self.api_key in content:
            raise LLMError("AI 서버 응답 형식을 확인하세요.")
        return content

    async def _complete_sdk(self, params: dict[str, Any], *, request_guard: Callable | None = None) -> str:
        import anthropic

        if self._sdk is None:
            self._sdk = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=self.timeout, max_retries=1,
                                                 http_client=self._http())
        try:
            try:
                if self._fallbacks:
                    resp = await self._sdk.beta.messages.create(**params, betas=[_FALLBACK_BETA], fallbacks="default")
                else:
                    resp = await self._sdk.messages.create(**params)
            except anthropic.BadRequestError:
                if not self._fallbacks:
                    raise
                if not await _request_allowed(request_guard):
                    raise LLMRequestStale("원문 또는 설정이 바뀌어 AI 요청을 취소했습니다")
                self._fallbacks = False   # 대체 옵션을 받지 않는 환경이면 끄고 한 번 더
                resp = await self._sdk.messages.create(**params)
        except anthropic.AuthenticationError:
            raise LLMError("Anthropic API 키가 올바르지 않습니다") from None
        except anthropic.RateLimitError:
            raise LLMError("Anthropic API 사용량 한도 초과(429)") from None
        except anthropic.APIStatusError:
            raise LLMError("Anthropic API가 요청을 처리하지 못했습니다. 연결과 모델 설정을 확인하세요.") from None
        except anthropic.APIConnectionError:
            raise LLMError("Anthropic API 연결 상태를 확인하세요.") from None
        if resp.stop_reason == "refusal":
            raise LLMError("모델이 요청을 거절했습니다(refusal)")
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0),
                                             transport=self._transport, follow_redirects=False)
        return self._client

    async def _complete_http(self, params: dict[str, Any], *, request_guard: Callable | None = None,
                             allow_fallbacks: bool = True) -> str:
        headers = {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        url = self.base_url + "/v1/messages"

        async def post(with_fallback: bool) -> httpx.Response:
            if not await _request_allowed(request_guard):
                raise LLMRequestStale("원문 또는 설정이 바뀌어 AI 요청을 취소했습니다")
            body, h = dict(params), dict(headers)
            if with_fallback:
                body["fallbacks"] = "default"
                h["anthropic-beta"] = _FALLBACK_BETA
            return await self._http().post(url, json=body, headers=h, follow_redirects=False)

        try:
            r = await post(self._fallbacks and allow_fallbacks)
            if r.status_code == 400 and self._fallbacks and allow_fallbacks:
                self._fallbacks = False
                r = await post(False)
        except httpx.HTTPError:
            raise LLMError("Anthropic API 연결 상태를 확인하세요.") from None
        if 300 <= r.status_code < 400:
            raise LLMError("Anthropic API 주소 이동을 따르지 않았습니다. 서버 주소를 확인하세요.")
        if r.status_code == 401:
            raise LLMError("Anthropic API 키가 올바르지 않습니다")
        if r.status_code >= 400:
            raise LLMError("Anthropic API가 요청을 처리하지 못했습니다. 모델과 이용 한도를 확인하세요.")
        try:
            data = r.json()
        except ValueError:
            raise LLMError("Anthropic API 응답 형식 오류") from None
        if data.get("stop_reason") == "refusal":
            raise LLMError("모델이 요청을 거절했습니다(refusal)")
        return "".join(b.get("text", "") for b in data.get("content") or []
                       if isinstance(b, dict) and b.get("type") == "text")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._sdk is not None:
            try:
                await self._sdk.close()
            except Exception:  # pragma: no cover - 방어
                pass
            self._sdk = None


class RulesProvider(LLMProvider):
    """LLM 없이 규칙으로 같은 JSON 을 만든다(항상 동작하는 대체 경로)."""

    name = "rules"
    model = "rules"

    def __init__(self, terms_fn: Callable[[], list[tuple[str, str]]] | None = None, note: str = ""):
        self.terms_fn = terms_fn
        self.note = note

    def info(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, **({"note": self.note} if self.note else {})}

    async def complete(self, system: str, user: str, max_tokens: int = 1200, json_mode: bool = True) -> str:
        terms = self.terms_fn() if self.terms_fn else []
        if prompts.TASK_SESSION in user:
            return json.dumps(_rules_session_from_digest(user), ensure_ascii=False)
        utts = prompts.parse_utterance_lines(user)
        block = None
        m = re.search(r"\[질의 블록 b(\d+)\] (\S+) 위원", user)
        if m and m.group(2) != "성명":
            block = {"id": int(m.group(1)), "kind": "question", "questioner": m.group(2), "questioner_party": "",
                     "ts_start": 0, "ts_end": 0}
        return json.dumps(rules_block_summary(block, utts, terms), ensure_ascii=False)


def provider_signature(cfg: dict[str, Any]) -> str:
    keys = ("provider", "local_model", "desktop_model", "openai_base_url", "openai_connection", "openai_key_base_url", "openai_model", "openai_api_key", "anthropic_model",
            "anthropic_api_key")
    return hashlib.sha256(json.dumps([cfg.get(k) or "" for k in keys], ensure_ascii=False).encode("utf-8")).hexdigest()


def make_provider(cfg: dict[str, Any], rt: Any = None) -> LLMProvider:
    """settings['llm'] → 제공자. disable_heavy_engines 이면 local_mlx 대신 rules."""
    cfg = cfg or {}
    for secret in ("openai_api_key", "anthropic_api_key"):
        key = cfg.get(secret)
        if isinstance(key, str) and key and any(key in str(cfg.get(model) or "") for model in
                                               ("local_model", "desktop_model", "openai_model", "anthropic_model")):
            raise LLMError("API 키는 모델 이름이 아닌 API 키 칸에만 입력하세요.")
    kind = (cfg.get("provider") or "rules").strip()
    terms_fn = None
    if rt is not None and getattr(rt, "repo", None) is not None:
        from .. import settings as _settings

        terms_fn = lambda: _settings.watch_terms(rt.repo)  # noqa: E731
    heavy_off = bool(rt is not None and getattr(getattr(rt, "config", None), "disable_heavy_engines", False))
    if kind == "desktop_chatgpt":
        return DesktopChatGPTProvider(cfg.get("desktop_model") or "", getattr(rt, "desktop_bridge", None))
    if kind == "local_mlx":
        if heavy_off:
            return RulesProvider(terms_fn, note="무거운 엔진 비활성화 설정으로 규칙 기반 사용")
        return LocalMLXProvider(cfg.get("local_model") or DEFAULT_LOCAL_MODEL)
    if kind == "openai_compat":
        from .compat_config import connection_url, credential_for_endpoint
        try:
            endpoint = connection_url(cfg)
            key = credential_for_endpoint(cfg, endpoint=endpoint)
        except ValueError as error:
            raise LLMError(str(error)) from None
        return OpenAICompatProvider(endpoint, cfg.get("openai_model") or "", key,
                                    connection=cfg.get("openai_connection") or "custom")
    if kind == "anthropic":
        return AnthropicProvider(cfg.get("anthropic_model") or DEFAULT_ANTHROPIC_MODEL,
                                 cfg.get("anthropic_api_key") or "")
    return RulesProvider(terms_fn)


# ── 규칙 기반 요약 ────────────────────────────────────────────────

_LABEL_PREFIX = re.compile(r"^\[[^\]]{1,20}\]\s*")
_ORG_CATS = {"agency", "person"}
_RT_PATTERNS = [
    ("예산", re.compile(r"예산|국비|재정|사업비|지방비|출연금|보조금")),
    ("책임추궁", re.compile(r"책임|징계|감사원|수사|고발|문책|은폐|사찰")),
    ("제도개선 요구", re.compile(r"제도|법\s|법령|규정|기준|시행규칙|고시|개선|규제|보완")),
    ("사실확인 필요", re.compile(r"확인|사실|맞습니까|있습니까|아닙니까")),
]


_LEVEL = {"none": 0, "low": 1, "medium": 2, "high": 3}
# rules.keywords 가 거르지 못하는 구어·용언 조각(규칙 요약 해시태그 품질용)
_KW_STOP = {"하나", "입장", "확인", "어떤", "이야기", "가지고", "갖다", "그렇지", "관련해", "있는데", "생각하는데",
            "그거", "이것", "저것", "부분", "얘기", "여러분", "위원님들", "장관님께서", "그러면", "하여튼", "어쨌거나",
            # '때문에'·'가지고' 에서 조사를 떼면 남는 기능어, 뜻 없는 부사
            "때문", "가지", "실제", "실제로", "자체", "그동안", "거의", "아마", "결국", "역시", "물론", "저쪽", "이쪽",
            "지켜", "만들", "그렇", "어떻"}
_KW_TAIL = re.compile(r"(?:는데|해서|하고|가지고|갖다|이고|지만|면서|려고|해야|하게|하면|한다|된다|지요|거든|잖아|니까|"
                      r"는지|던데|래요|대요|해요|어서|아서|으면|이며|에서도)$")


def _clean_item_text(text: str) -> str:
    return _LABEL_PREFIX.sub("", text or "").strip()


def _keywords(texts: list[str], top: int = 5) -> list[str]:
    return [k for k in rules.keywords(texts, top=top * 3) if k not in _KW_STOP and not _KW_TAIL.search(k)][:top]


def ground_org_relevance(summary: dict[str, Any], utts: list[dict[str, Any]],
                         terms: list[tuple[str, str]]) -> dict[str, Any]:
    """LLM 의 우리 기관 관련도를 실제 언급(기관명·별칭·기관장·감시 키워드)에 맞춘다(소관 추측 방지)."""
    if not terms:
        return summary
    org, watch, uids = _org_hits(utts, terms)
    lvl = summary.get("org_relevance") or "none"
    reason = summary.get("org_reason") or ""
    if org:
        if _LEVEL.get(lvl, 0) < 2:
            summary["org_relevance"] = "medium"
            mention = ", ".join(f"'{k}' {n}회" for k, n in org.most_common(3))
            summary["org_reason"] = (reason + " / " if reason else "") + f"{mention} 언급"
    elif watch:
        if _LEVEL.get(lvl, 0) > 2:
            summary["org_relevance"] = "medium"
    elif _LEVEL.get(lvl, 0) > 1:
        summary["org_relevance"] = "low"
        summary["org_reason"] = (reason + " " if reason else "") + "(기관명·감시 키워드 직접 언급 없음 — AI 추정)"
    return summary


def _member_like(u: dict[str, Any]) -> bool:
    return (u.get("speaker_role") or "unknown") in ("member", "unknown", "other")


def _org_hits(utts: list[dict[str, Any]], terms: list[tuple[str, str]]) -> tuple[Counter, Counter, list[int]]:
    org, watch, uids = Counter(), Counter(), []
    for u in utts:
        hit = False
        for m in rules.find_mentions(u.get("text") or "", terms):
            if m.category in _ORG_CATS:
                org[m.keyword] += 1
                hit = True
            else:
                watch[m.keyword] += 1
        if hit:
            uids.append(u["id"])
    return org, watch, uids


# 위원 줄에 섞여 들어온 답변(국회 AI자막은 질의·답변이 한 줄로 합쳐지는 일이 잦다)
_MIXED_ANSWER = re.compile(r"(?:^|\s)(?:예|네)\s+(?:알겠습니다|그렇습니다|맞습니다)|하도록\s*하겠습니다|"
                           r"검토(?:해\s*보|하)(?:도록\s*하)?겠습니다|확인(?:해\s*보|하)(?:도록\s*하)?겠습니다|"
                           r"드리(?:도록\s*하)?겠습니다")


def _mixed_answer_points(utts: list[dict[str, Any]], commitments: list[dict[str, Any]],
                         limit: int = 2, max_len: int = 100) -> list[str]:
    """답변(answer) 발언이 없을 때 위원 줄 속 답변 문장(없으면 약속 문구)을 답변 요지 후보로 쓴다."""
    out: list[str] = []
    for u in utts:
        if not _member_like(u):
            continue
        for s in split_sentences(u.get("text") or ""):
            m = _MIXED_ANSWER.search(s)
            if not m:
                continue
            if re.match(r"\s*(?:예|네)\s", s[m.start():]):
                s = s[m.start():].strip()        # '…그렇지 않나요 예 그렇습니다' → '예 그렇습니다'(질의 부분 제외)
            if len(s) < 6:
                continue
            s = s if len(s) <= max_len else s[: max_len - 1].rstrip() + "…"
            if s not in out:
                out.append(s)
            if len(out) >= limit:
                return out
    if not out:
        for c in commitments:
            t = c.get("text") or ""
            if t and t not in out:
                out.append(t if len(t) <= max_len else t[: max_len - 1].rstrip() + "…")
            if len(out) >= limit:
                break
    return out


def rules_block_summary(block: dict[str, Any] | None, utts: list[dict[str, Any]],
                        terms: list[tuple[str, str]] | None = None, *, provider: str = "rules") -> dict[str, Any]:
    """질의 블록 발언 → BlockSummary(규칙 기반). 수치·사실은 원문 발췌만 쓴다."""
    terms = terms or []
    utts = [u for u in utts if (u.get("text") or "").strip() and u.get("speaker_role") != "system"]
    questioner = (block or {}).get("questioner") or ""
    requester = questioner if (block or {}).get("kind", "question") == "question" else ""
    texts = [u["text"] for u in utts]
    member_texts = [u["text"] for u in utts if _member_like(u)]
    answer_texts = [u["text"] for u in utts if u.get("speaker_role") == "answer"]

    lists: dict[str, list[dict[str, Any]]] = {"issue": [], "data_request": [], "commitment": []}
    seen: set[tuple[str, str]] = set()
    dues: list[str] = []
    for u in utts:
        # 기한('일주일 내' 등)은 그 발언이 나온 날짜 기준
        base = datetime.fromtimestamp(u["ts_start"] / 1000, KST).date() if u.get("ts_start") else None
        for it in rules.extract_items(u["text"], u.get("speaker_role") or "unknown", speaker=u.get("speaker") or "",
                                      requester=requester, base_date=base):
            if it.kind not in lists:
                continue
            text = _clean_item_text(it.text)
            if (it.kind, text) in seen:
                continue
            seen.add((it.kind, text))
            entry: dict[str, Any] = {"text": text, "utterance_id": u["id"]}
            if it.kind != "issue" and it.strength:
                entry["strength"] = it.strength
            lists[it.kind].append(entry)
            if it.due_date:
                dues.append(it.due_date)

    kws = _keywords(texts, top=5)
    q_points = rules.extractive_points(member_texts or texts, limit=3, max_len=100)
    a_points = rules.extractive_points(answer_texts, limit=2, max_len=100) if answer_texts else []
    if not a_points:
        a_points = _mixed_answer_points(utts, lists["commitment"])

    org, watch, org_uids = _org_hits(utts, terms)
    n_org = sum(org.values())
    if n_org >= 2 or (n_org and (lists["data_request"] or lists["issue"])):
        relevance = "high"
    elif n_org:
        relevance = "medium"
    elif watch:
        relevance = "low"
    else:
        relevance = "none"
    if org:
        reason = ", ".join(f"'{k}' {n}회" for k, n in org.most_common(3)) + " 언급"
        if org_uids:
            reason += "(" + ", ".join(f"u{i}" for i in org_uids[:3]) + ")"
    elif watch:
        reason = "감시 키워드 " + ", ".join(f"'{k}'" for k, _ in watch.most_common(3)) + " 언급"
    else:
        reason = ""

    importance = 1 + {"high": 2, "medium": 1}.get(relevance, 0)
    importance += 1 if lists["data_request"] else 0
    importance += 1 if (lists["issue"] or len(lists["commitment"]) >= 2) else 0
    joined = " ".join(texts)
    if lists["data_request"]:
        rtype = "자료요구"
    else:
        rtype = next((name for name, pat in _RT_PATTERNS if pat.search(joined)), "기타")
    if "종합감사 전" in dues or re.search(r"종합\s*(?:감사|국감)|종감", joined):
        urgency = "종합감사 전"
    elif re.search(r"오늘\s*중|즉시|당장|오후까지|내일\s*아침", joined):
        urgency = "당일"
    else:
        urgency = "결과보고서 대응"

    if block and block.get("kind") == "chair":
        one = "위원장 회의 진행"
    else:
        who = f"{questioner} 위원" if questioner else "질의 위원(성명 확인 필요)"
        one = f"{who}, {'·'.join(kws[:3])} 관련 질의" if kws else f"{who} 질의"
    return {
        "one_line": one,
        "question_points": q_points,
        "answer_points": a_points,
        "issues": lists["issue"][:6],
        "data_requests": lists["data_request"][:6],
        "commitments": lists["commitment"][:6],
        "keywords": kws[:5],
        "org_relevance": relevance,
        "org_reason": reason,
        "importance": min(5, importance),
        "response_type": rtype,
        "urgency": urgency,
        "provider": provider,
        "model": "rules",
        "generated_at": now_ms(),
    }


def _hm(ms: int | None) -> str:
    return hms(ms)[:5] if ms else ""


def rules_session_summary(session: dict[str, Any] | None, blocks: list[tuple[dict[str, Any], dict[str, Any]]],
                          items: list[dict[str, Any]], *, scope: str = "session", covered_until_ms: int = 0,
                          ts_first: int | None = None, provider: str = "rules") -> dict[str, Any]:
    """(질의 블록, BlockSummary) 목록 + 추출 항목 → SessionSummary(규칙 기반)."""
    s = session or {}
    name = s.get("title") or (f"{s.get('committee')} 국정감사" if s.get("committee") else "국정감사")
    qblocks = [(b, sm) for b, sm in blocks if b.get("kind") == "question"]
    names: list[str] = []
    for b, _ in qblocks:
        if b.get("questioner") and b["questioner"] not in names:
            names.append(b["questioner"])
    t0 = ts_first or (qblocks[0][0]["ts_start"] if qblocks else None)
    t1 = covered_until_ms or (qblocks[-1][0]["ts_end"] if qblocks else None)
    live = [i for i in items if i.get("status") not in ("dismissed", "superseded")]
    kinds = Counter(i["kind"] for i in live)
    # 약속 건수는 상황보고·후속조치와 같이 의례적 표현('노력하겠습니다')을 뺀다
    commits = sum(1 for i in live if i["kind"] == "commitment" and i.get("strength") != "의례")
    org_blocks = [(b, sm) for b, sm in qblocks if sm.get("org_relevance") in ("high", "medium")]

    sents = [f"{name}{' 진행 중' if scope == 'rolling' else ''} — {_hm(t0)}~{_hm(t1)} 기준 위원 질의 {len(qblocks)}건"
             + (f"({', '.join(names[:6])}{' 등' if len(names) > 6 else ''})" if names else "") + "이 기록됨."]
    if kinds:
        sents.append(f"요구자료 {kinds.get('data_request', 0)}건, 약속·검토사항 {commits}건, "
                     f"지적사항 {kinds.get('issue', 0)}건이 추출됨.")
    sents.append(f"우리 기관 관련 질의는 {len(org_blocks)}건임." if org_blocks else "우리 기관을 직접 다룬 질의는 확인되지 않음.")
    kw_count: Counter[str] = Counter()
    for _, sm in qblocks:
        for k in sm.get("keywords") or []:
            kw_count[k] += 1
    top = [k for k, _ in kw_count.most_common(8)]
    if top:
        sents.append("주요 쟁점어: " + ", ".join(top[:5]) + ".")

    ranked = sorted(qblocks, key=lambda x: (-int(x[1].get("importance") or 0), x[0]["ts_start"]))
    issues = [{"title": sm.get("one_line") or "", "utterance_id": block_ref_utterance(sm), "block_id": b["id"]}
              for b, sm in ranked[:6]]
    return {
        "overview": " ".join(sents),
        "issues": issues,
        "hashtags": ["#" + k for k in top],
        "org_points": session_org_points(qblocks),
        "next_actions": session_actions(items, len(org_blocks)),
        "provider": provider,
        "model": "rules",
        "generated_at": now_ms(),
        "covered_until_ms": covered_until_ms or (t1 or 0),
    }


def block_ref_utterance(sm: dict[str, Any]) -> int | None:
    """블록 요약에서 대표 근거 발언 id(요구자료 > 지적 > 약속 순)."""
    for key in ("data_requests", "issues", "commitments"):
        for r in sm.get(key) or []:
            if r.get("utterance_id"):
                return r["utterance_id"]
    return None


def session_org_points(qblocks: list[tuple[dict[str, Any], dict[str, Any]]], limit: int = 6) -> list[str]:
    """우리 기관 관련도 high·medium 블록 → '[HH:MM] 김남근 위원: 질의요지 (요구: …)'."""
    out = []
    for b, sm in qblocks:
        if sm.get("org_relevance") not in ("high", "medium"):
            continue
        who = f"{b['questioner']} 위원" if b.get("questioner") else "성명 미확인 위원"
        pt = (sm.get("question_points") or [sm.get("one_line") or ""])[0]
        reqs = [r.get("text", "") for r in sm.get("data_requests") or []][:1]
        out.append(f"[{_hm(b['ts_start'])}] {who}: {pt}" + (f" (요구: {reqs[0]})" if reqs else ""))
        if len(out) >= limit:
            break
    return out


def session_actions(items: list[dict[str, Any]], org_block_count: int = 0) -> list[str]:
    """추출 항목 → 우리 기관 후속조치 목록(규칙)."""
    live = [i for i in items if i.get("status") not in ("dismissed", "superseded")]
    kinds = Counter(i["kind"] for i in live)
    actions = []
    if kinds.get("data_request"):
        actions.append(f"요구자료 {kinds['data_request']}건 담당부서 지정 및 제출기한 관리")
    commits = [i for i in live if i["kind"] == "commitment" and i.get("strength") != "의례"]
    if commits:
        actions.append(f"약속·검토사항 {len(commits)}건 이행계획 수립")
    if kinds.get("written_question"):
        due = next((i.get("due_date") for i in live if i["kind"] == "written_question" and i.get("due_date")), "")
        actions.append("서면질의 답변서 제출 준비" + (f"(기한: {due})" if due else ""))
    if kinds.get("witness_request"):
        actions.append("증인·참고인 채택 요구 동향 확인")
    if org_block_count:
        actions.append(f"우리 기관 관련 질의 {org_block_count}건 사실관계 확인 및 보완자료 준비")
    if not actions:
        actions.append("질의 요지 검수 후 상황보고 배포")
    return actions


_PAST_ANSWER = re.compile(r"(?:답변함|약속함|요구함|지적함|촉구함|주장함|제기함|표명함|약속|답변)\W*$")


def merge_actions(llm_actions: list[str], rule_actions: list[str], limit: int = 6) -> list[str]:
    """LLM 후속조치 중 '이미 끝난 답변·요구'를 옮겨 적은 줄은 버리고 규칙 후속조치로 채운다."""
    out = [a for a in llm_actions if not _PAST_ANSWER.search(a)]
    for a in rule_actions:
        if len(out) >= limit:
            break
        if a not in out:
            out.append(a)
    return out[:limit]


# ── 문구 겹침(같은 요구·약속인지 판단) ─────────────────────────────
# 요구·약속 문구에 늘 붙는 일반어는 빼고 내용어의 글자 2-gram 으로 비교한다(음성인식 원문 ↔ AI 개조식 문구).
_SIM_GENERIC = re.compile(
    r"자료|제출|요구|요청|보고|약속|검토|확인|답변|지적|촉구|필요|하겠|겠습니다|습니다|주십시오|주시기|바랍니다|"
    r"드리|드립니다|해\s*주|위원님?|장관님?|사장님?|관련|대해서?|부분|한번|이번|다시|좀|함$")
SIM_SAME_UTTERANCE = 0.2    # 같은 발언·종류 안에서 같은 항목으로 볼 겹침(1:1 대응)
SIM_ANY_UTTERANCE = 0.5     # 근거 발언이 없거나 다를 때 같은 항목으로 볼 겹침


def _bigrams(text: str, generic: bool = True) -> set[str]:
    t = _SIM_GENERIC.sub(" ", text or "") if generic else (text or "")
    out: set[str] = set()
    for w in re.findall(r"[가-힣A-Za-z0-9]+", t):
        out.update(w[i:i + 2] for i in range(len(w) - 1))
    return out


def text_overlap(a: str, b: str) -> float:
    """a(짧은 요지)의 내용어 2-gram 중 b(원문 인용·요지)에 들어 있는 비율(0~1)."""
    A = _bigrams(a) or _bigrams(a, generic=False)
    if not A:
        return 0.0
    B = _bigrams(b, generic=False)
    return len(A & B) / len(A)


def ground_ref_uids(summary: dict[str, Any], utts: list[dict[str, Any]],
                    threshold: float = SIM_ANY_UTTERANCE) -> dict[str, Any]:
    """근거 발언(utterance_id)이 없는 지적·요구·약속에, 문구가 가장 많이 겹치는 블록 발언 id 를 붙인다."""
    cands = [u for u in utts if (u.get("text") or "").strip() and u.get("speaker_role") != "system"]
    if not cands:
        return summary
    for key in ("issues", "data_requests", "commitments"):
        for r in summary.get(key) or []:
            if r.get("utterance_id") is not None or not r.get("text"):
                continue
            best, score = None, 0.0
            for u in cands:
                sc = text_overlap(r["text"], u["text"])
                if sc > score:
                    best, score = u, sc
            if best is not None and score >= threshold:
                r["utterance_id"] = best["id"]
    return summary


def merge_rule_refs(summary: dict[str, Any], rule_summary: dict[str, Any]) -> dict[str, Any]:
    """LLM 이 놓친 요구자료·약속을 규칙 추출로 보충한다(origin='rule' 표시).

    같은 발언을 근거로 든 LLM 항목이 있거나, 근거 발언이 없더라도 문구가 겹치는 LLM 항목이 있으면 보충하지 않는다.
    """
    for key in ("data_requests", "commitments"):
        mine = list(summary.get(key) or [])
        have = {r.get("utterance_id") for r in mine if r.get("utterance_id") is not None}
        for r in rule_summary.get(key) or []:
            uid = r.get("utterance_id")
            if uid in have:
                continue
            if any(text_overlap(m.get("text") or "", r.get("text") or "") >= SIM_ANY_UTTERANCE for m in mine):
                continue
            summary.setdefault(key, []).append({**r, "origin": "rule"})
            if uid is not None:
                have.add(uid)
    return summary


def _rules_session_from_digest(user: str) -> dict[str, Any]:
    """RulesProvider 가 회의 요약 프롬프트만 받았을 때(설정 시험 등)의 간이 요약."""
    issues, texts = [], []
    for line in user.splitlines():
        m = re.match(r"^\[b(\d+)[^\]]*\]\s*([^:]+):\s*([^|]*)", line)
        if m:
            issues.append({"title": m.group(3).strip() or m.group(2).strip(), "utterance_id": None,
                           "block_id": int(m.group(1))})
            texts.append(line)
    kws = _keywords(texts, top=6)
    return {"overview": f"질의 블록 {len(issues)}건이 기록됨(규칙 기반 요약).", "issues": issues[:6],
            "hashtags": ["#" + k for k in kws], "org_points": [], "next_actions": ["질의 요지 검수 후 상황보고 배포"],
            "provider": "rules", "model": "rules", "generated_at": now_ms(), "covered_until_ms": 0}
