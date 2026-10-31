"""브라우저 마이크·탭 음성 수집원.

웹 모듈의 WS `/ws/ingest/{sid}` 가 받은 바이너리 프레임(16kHz mono s16le, 보통 100ms)을
`rt.ingest.browser_feed(sid)` 로 얻은 콜백에 넘기면 여기 큐로 들어와 VAD→STT 로 간다.
첫 텍스트 메시지 {"type":"hello","sample_rate":16000} 처리는 웹 모듈 몫이다(16kHz 만 받는다).
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from ..db import now_ms
from .audio import BYTES_PER_MS, run_audio_pipeline
from .base import ClockMap, Emit, GapMarker, Source, SourceContext

logger = logging.getLogger(__name__)

QUEUE_FRAMES = 600            # 100ms 프레임 기준 약 60초
DRIFT_MS = 2000               # 도착 시각이 예상과 이만큼 어긋나면(끊겼다 재개) 기준점을 다시 잡는다


class BrowserAudioSource(Source):
    kind = "browser"

    def __init__(self, ctx: SourceContext):
        super().__init__(ctx)
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=QUEUE_FRAMES)
        self.clock = ClockMap()
        self.gap = GapMarker(ctx)
        self.dropped = 0
        self._bytes = 0
        self._odd = b""

    def feed(self, pcm: bytes) -> None:
        """WS 수신 PCM 을 넣는다(동기, 이벤트 루프 스레드에서 호출). 큐가 차면 가장 오래된 프레임을 버린다."""
        if not pcm:
            return
        try:
            self.queue.put_nowait(bytes(pcm))
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            self.dropped += 1
            self.queue.put_nowait(bytes(pcm))

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        self.ctx.set_state(phase="connecting", detail="브라우저 음성 연결 대기 — 수집 화면에서 캡처를 시작하세요")
        hk = asyncio.create_task(self._housekeeping(stop), name=f"browser-hk-{self.ctx.sid}")
        try:
            await run_audio_pipeline(self.ctx, self._pcm(stop), emit, self.clock,
                                     key_prefix=f"b{now_ms()}-", partials=True, live=True, stop=stop)
        finally:
            hk.cancel()

    async def _pcm(self, stop: asyncio.Event) -> AsyncIterator[bytes]:
        stop_w = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                get = asyncio.ensure_future(self.queue.get())
                done, _ = await asyncio.wait({get, stop_w}, return_when=asyncio.FIRST_COMPLETED)
                if get not in done:
                    get.cancel()
                    break
                async for chunk in self._accept(get.result(), live=True):
                    yield chunk
            # 중지 시점에 큐에 남은 프레임도 처리한다(종료 직전 마지막 발화가 버려지지 않게)
            while True:
                try:
                    data = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                async for chunk in self._accept(data, live=False):
                    yield chunk
        finally:
            stop_w.cancel()

    async def _accept(self, data: bytes, *, live: bool) -> AsyncIterator[bytes]:
        """수신 프레임 하나를 시각 기준점에 맞춰 내보낸다. live=False(중지 후 잔여분)는 연속 오디오로 본다."""
        data = self._odd + data
        if len(data) % 2:
            self._odd, data = data[-1:], data[:-1]
        else:
            self._odd = b""
        now = now_ms()
        off = self._bytes // BYTES_PER_MS
        dur = len(data) // BYTES_PER_MS
        # 첫 프레임이거나 예상보다 크게 늦게 도착하면(끊겼다 재개) 기준점 갱신.
        # 일찍 도착(밀린 프레임 몰아 받기)은 연속 오디오이므로 그대로 둔다.
        if self._bytes == 0 or (live and now - (self.clock(off) + dur) > DRIFT_MS):
            if self._bytes:
                yield b""        # 불연속: 진행 중인 발화 구간을 확정
            self.clock.mark(off, now - dur)
        self._bytes += len(data)
        if live:
            await self.gap.on_rx(now)
        self.ctx.mark_rx(position_ms=self._bytes // BYTES_PER_MS, dropped=self.dropped,
                         detail="브라우저 음성 수신 중")
        yield data

    async def _housekeeping(self, stop: asyncio.Event) -> None:
        from .base import sleep_or_stop

        while not stop.is_set():
            try:
                if await self.gap.check():
                    self.ctx.set_state(detail="브라우저 음성이 들어오지 않습니다 — 캡처 화면을 확인하세요")
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 방어
                logger.exception("브라우저 수집 주기 점검 실패")
            if await sleep_or_stop(stop, 5):
                return
