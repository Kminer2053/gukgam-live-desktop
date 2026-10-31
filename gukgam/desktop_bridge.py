"""Electron 부모와 익명 파이프로만 통신하는 제한된 ChatGPT 요청 연결."""
from __future__ import annotations

import asyncio
import json
import math
import threading
from typing import Any, Callable, TextIO
from uuid import uuid4

from .llm.providers import LLMError, LLMRequestStale, _request_allowed

MAX_LINE_CHARS = 1_048_576
MAX_RESPONSE_CHARS = 256_000


class JsonlOutput:
    """파이프의 역압력이 수집 루프를 멈추지 않도록 쓰기를 짧게 기다린다."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self._lock = threading.Lock()

    async def emit(self, message: dict, *, timeout: float = 2) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(line) > MAX_LINE_CHARS:
            raise LLMError("앱 연결 요청이 크기 한도를 넘었습니다")
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def settle(error: bool):
            if not done.done():
                if error:
                    done.set_exception(LLMError("앱 연결 파이프에 쓸 수 없습니다"))
                else:
                    done.set_result(None)

        def write():
            error = False
            try:
                with self._lock:
                    self.stream.write(line)
                    self.stream.flush()
            except Exception:
                error = True
            try:
                loop.call_soon_threadsafe(settle, error)
            except RuntimeError:
                pass

        # 부모가 파이프를 읽지 않아도 앱 종료가 이 스레드를 무한 대기하지 않는다.
        threading.Thread(target=write, name="desktop-jsonl-write", daemon=True).start()
        try:
            await asyncio.wait_for(done, timeout)
        except TimeoutError:
            raise LLMError("앱 연결 파이프의 쓰기 시간이 지났습니다") from None


class DesktopBridge:
    """동시에 한 요청만 보내며 취소·만료 뒤 도착한 응답은 폐기한다."""

    def __init__(self, output: JsonlOutput):
        self.output = output
        self.closed = False
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._status_pending: dict[str, asyncio.Future] = {}
        self._status_lock = asyncio.Lock()

    def receive(self, message: Any) -> None:
        """엔진 루프에서만 호출한다. 부모 오류 본문은 저장·로그·예외에 복사하지 않는다."""
        if not isinstance(message, dict):
            return
        if message.get("type") == "account.status.result":
            rid = message.get("id")
            future = self._status_pending.get(rid) if isinstance(rid, str) else None
            if future is not None and not future.done():
                state = message.get("state")
                future.set_result({"state": state if isinstance(state, str) and state in {
                    "verified", "refreshable", "unverified", "expired", "disconnected", "checking", "error", "unavailable"
                } else "unavailable"})
            return
        if message.get("type") not in {"llm.result", "llm.error"}:
            return
        rid = message.get("id")
        future = self._pending.get(rid) if isinstance(rid, str) else None
        if future is None or future.done():
            return
        text = message.get("text")
        if message["type"] == "llm.result" and isinstance(text, str) and len(text) <= MAX_RESPONSE_CHARS:
            future.set_result(text)
        else:
            future.set_exception(LLMError("ChatGPT 요청을 완료하지 못했습니다. 앱의 연결 상태를 확인하세요"))

    def close(self) -> None:
        self.closed = True
        for future in (*self._pending.values(), *self._status_pending.values()):
            if not future.done():
                future.set_exception(LLMError("ChatGPT 앱 연결이 종료되었습니다"))

    async def connection_health(self, *, timeout: float = 1.5) -> dict:
        """앱의 현재 연결 요약만 조회한다. AI 요청·계정 갱신은 실행하지 않는다."""
        rid = None
        try:
            async with asyncio.timeout(timeout):
                async with self._status_lock:
                    if self.closed:
                        return {"state": "unavailable"}
                    rid = uuid4().hex
                    future = asyncio.get_running_loop().create_future()
                    self._status_pending[rid] = future
                    await self.output.emit({"type": "account.status.request", "id": rid})
                    return await future
        except (TimeoutError, LLMError):
            return {"state": "unavailable"}
        finally:
            future = self._status_pending.pop(rid, None)
            if future is not None and not future.done():
                future.cancel()

    async def request(self, system: str, user: str, max_tokens: int, *, timeout: float = 180,
                      guard: Callable | None = None) -> str:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("앱 요청 제한 시간은 유한한 양수여야 합니다")
        if not isinstance(system, str) or not isinstance(user, str) or type(max_tokens) is not int or not 1 <= max_tokens <= 32768:
            raise ValueError("앱 요청 형식이 잘못되었습니다")
        rid, future, sent = None, None, False
        try:
            async with asyncio.timeout(min(timeout, 180)):
                async with self._lock:
                    if self.closed:
                        raise LLMError("ChatGPT 앱 연결이 종료되었습니다")
                    if not await _request_allowed(guard):
                        raise LLMRequestStale("모델 대기 중 입력·자동 검토 상태가 바뀌었습니다")
                    rid = uuid4().hex
                    future = asyncio.get_running_loop().create_future()
                    self._pending[rid] = future
                    sent = True
                    await self.output.emit({"type": "llm.request", "id": rid, "system": system,
                                            "user": user, "max_tokens": max_tokens})
                    while not future.done():
                        await asyncio.wait({future}, timeout=0.5)
                        if not await _request_allowed(guard):
                            raise LLMRequestStale("모델 처리 중 입력·자동 검토 상태가 바뀌었습니다")
                    return future.result()
        except TimeoutError:
            raise LLMError("ChatGPT 앱 응답 제한 시간을 넘었습니다") from None
        finally:
            if rid is not None:
                self._pending.pop(rid, None)
            if future is not None and not future.done():
                future.cancel()
                if sent:
                    try:
                        await self.output.emit({"type": "llm.cancel", "id": rid}, timeout=0.25)
                    except (Exception, asyncio.CancelledError):
                        pass
