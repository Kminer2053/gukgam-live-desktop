"""세션별 실시간 이벤트 버스(SSE 구독자에게 팬아웃).

이벤트 타입:
  session.status     세션 상태·수집원 상태 변경
  utterance.upsert   발언 추가/갱신(부분→확정, 화자 교정 포함)
  block.upsert       질의 블록 추가/갱신(질의자·상태·요약)
  item.upsert        요구자료·약속사항 등 추출 항목
  alert              우리 기관 언급 등 감시 키워드 적중
  summary.update     롤링/전체 요약 갱신
  stats              발언 수·지연 등 통계
  utterance.delete   미확정 턴 정리로 지워진 발언({id})
  items.reload       발언 수정 등으로 항목을 다시 불러야 함({utterance_id, block_id})
  alert.delete       본문 교정으로 사라진 감시 키워드의 미확인 알림({id, utterance_id})
  analysis.status    AI 분석 작업 큐 상태

이벤트 id 는 전 채널 공통 증가값이고, 프로세스 기동 시각(ms)×1000 에서 시작한다.
그래서 재기동 뒤 id 는 늘 이전 프로세스의 id 보다 크고, 이전 프로세스의 Last-Event-ID 는 start_id 보다 작다
(JS Number 안전 범위 안: 약 1.8e15 < 9e15).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from .db import now_ms

log = logging.getLogger(__name__)


@dataclass
class Event:
    id: int
    type: str
    data: dict[str, Any]

    def sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n"


class EventBus:
    """채널(세션 id 또는 '*')별 구독. 느린 구독자는 큐가 차면 끊어 서버를 보호한다."""

    def __init__(self, history: int = 500, queue_size: int = 1000, start_id: int | None = None):
        self._subs: dict[str, set[asyncio.Queue[Event | None]]] = defaultdict(set)
        self._history: dict[str, deque[Event]] = defaultdict(lambda: deque(maxlen=history))
        # 재기동해도 id 가 되감기지 않게 기동 시각 기반으로 시작한다(routes_stream 의 재기동 판정 근거)
        self._start = start_id if start_id is not None else now_ms() * 1000
        self._seq = self._start
        self._queue_size = queue_size
        self._taps: list[Callable[[str, Event], None]] = []

    def add_tap(self, fn: Callable[[str, Event], None]) -> None:
        """채널 정보가 필요한 동기 수신자를 등록한다(중복 등록 방지)."""
        if fn not in self._taps:
            self._taps.append(fn)

    def remove_tap(self, fn: Callable[[str, Event], None]) -> None:
        """등록한 동기 수신자를 해제한다."""
        if fn in self._taps:
            self._taps.remove(fn)

    def publish(self, channel: str, type_: str, data: dict[str, Any]) -> Event:
        self._seq += 1
        ev = Event(self._seq, type_, data)
        self._history[channel].append(ev)
        for fn in tuple(self._taps):
            try:
                fn(channel, ev)
            except Exception:
                log.exception("이벤트 탭 처리 실패(channel=%s)", channel)
        for ch in (channel, "*"):
            for q in list(self._subs.get(ch, ())):
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    log.warning("구독자 큐 초과로 연결 종료(channel=%s)", ch)
                    self._subs[ch].discard(q)
                    # 큐가 가득 차 있으니 하나를 비워 종료 신호(None) 자리를 만든다. 빠진 이벤트는
                    # 클라이언트가 재연결하면서 Last-Event-ID 재전송(또는 history_overflow resync)으로 메운다.
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:  # pragma: no cover - 위에서 자리를 만들었으므로 오지 않는다
                        pass
        return ev

    @property
    def last_id(self) -> int:
        """지금까지 발행한 마지막 이벤트 id(전 채널 공통 증가값). 아직 발행 전이면 start_id."""
        return self._seq

    @property
    def start_id(self) -> int:
        """이 프로세스의 id 시작점. 이 프로세스가 발행한 이벤트 id 는 모두 이보다 크다."""
        return self._start

    def lost_since(self, channel: str, last_id: int) -> bool:
        """last_id 이후 이 채널 이벤트 일부가 기록 한도를 넘어 버려졌는지(재전송으로 못 메움)."""
        hist = self._history.get(channel)
        if not hist or hist.maxlen is None or len(hist) < hist.maxlen:
            return False
        # 기록이 가득 찼는데 가장 오래된 보관 이벤트가 last_id 보다 뒤 → 사이 이벤트가 버려졌을 수 있다
        return hist[0].id > last_id + 1

    def replay_since(self, channel: str, last_id: int) -> list[Event]:
        return [e for e in self._history.get(channel, ()) if e.id > last_id]

    async def subscribe(self, channel: str, last_id: int | None = None,
                        heartbeat: float = 15.0) -> AsyncIterator[Event | None]:
        """이벤트를 내보낸다. heartbeat 초마다 None(keep-alive)을 낸다."""
        q: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=self._queue_size)
        self._subs[channel].add(q)
        try:
            if last_id is not None:
                for ev in self.replay_since(channel, last_id):
                    yield ev
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    if q not in self._subs.get(channel, ()):
                        return  # 느린 구독자로 끊긴 연결: ping 만 보내는 '좀비' 연결로 남지 않게 끝낸다
                    yield None
                    continue
                if ev is None:
                    if q not in self._subs.get(channel, ()):
                        return
                    continue
                yield ev
        finally:
            self._subs[channel].discard(q)

    def subscriber_count(self, channel: str) -> int:
        return len(self._subs.get(channel, ()))
