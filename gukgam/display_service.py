"""자료 변경 때만 공통 표시본을 갱신하는 단일 작업자. 조회 요청은 작업을 만들지 않는다."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging

log = logging.getLogger(__name__)


class DisplayService:
    def __init__(self, rt, *, interval: float = .25):
        self.rt = rt
        self.interval = interval
        self._wake = asyncio.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcript-display")
        self._task = None
        self._closed = False

    def _changed(self, channel, event):
        # 본문은 조립하지 않는다. 늦은 조회가 수정 전 값을 복구하지 않도록 입력 세대만 붙인다.
        if channel and channel != "*" and event.type in {
            "caption.source", "utterance.upsert", "utterance.delete", "block.upsert",
            "transcript.stage", "summary.update", "roster.updated", "session.status",
        }:
            state = self.rt.repo.display_state(channel)
            if state:
                event.data["display_input_generation"] = state["input_generation"]
        if event.type != "transcript.display":
            self._wake.set()

    async def _refresh(self):
        from .display_materializer import refresh
        loop = asyncio.get_running_loop()
        sessions = await loop.run_in_executor(self._pool, self.rt.repo.display_dirty_sessions)
        for sid in sessions:
            if self._closed:
                return
            try:
                result = await loop.run_in_executor(self._pool, refresh, self.rt.repo, sid)
            except Exception as exc:
                # 한 회의의 손상된 옛 자료가 다른 회의의 실황 표시를 막지 않는다.
                log.warning("공통 표시본 단건 갱신 대기(%s)", type(exc).__name__)
                continue
            if result.get("committed"):
                self.rt.bus.publish(sid, "transcript.display", {"generation": result["generation"]})

    async def start(self):
        self.rt.bus.add_tap(self._changed)
        # 기존 기록의 최초 전환도 조회가 아닌 기동 작업이다. 모델·수집은 실행하지 않는다.
        try:
            await self._refresh()
            # 이미 저장된 회의도 수집 전에 최근 두 개만 준비해 첫 자막의 전체 재읽기를 피한다.
            from .display_materializer import refresh
            loop = asyncio.get_running_loop()
            sessions = await loop.run_in_executor(self._pool, self.rt.repo.display_prime_sessions)
            for sid in reversed(sessions):
                try:
                    result = await loop.run_in_executor(self._pool, refresh, self.rt.repo, sid)
                except Exception as exc:
                    log.warning("공통 표시본 읽기 준비 대기(%s)", type(exc).__name__)
                    continue
                if result.get("committed"):
                    self.rt.bus.publish(sid, "transcript.display", {"generation": result["generation"]})
        except BaseException:
            self._closed = True
            self.rt.bus.remove_tap(self._changed)
            self._pool.shutdown(wait=True, cancel_futures=True)
            raise
        self._task = asyncio.create_task(self._run(), name="transcript-display")

    async def _run(self):
        while not self._closed:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._closed:
                break
            # 한 수신 묶음의 원조각·발언·블록 변경을 합치되 연속 수신에도 기아가 없게 한다.
            await asyncio.sleep(.025)
            try:
                await self._refresh()
            except Exception as exc:
                # 이전 세대는 저장하되 dirty 상태로 공개하지 않는다. 다음 변경/주기에 재검사한다.
                log.warning("공통 표시본 갱신 대기(%s)", type(exc).__name__)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self.rt.bus.remove_tap(self._changed)
        self._wake.set()
        if self._task:
            await self._task
        self._pool.shutdown(wait=True, cancel_futures=True)
