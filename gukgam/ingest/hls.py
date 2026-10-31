"""국회 중계영상(HLS) 음성 → STT 수집원.

ffmpeg 로 m3u8 의 오디오만 16kHz mono s16le 로 받아(32,000 B/s) VAD→STT 로 넘긴다.
스트림은 24시간 살아 있고 심야엔 무음이다(회의 진행 여부는 live_play 의 xstat 로 본다).
ffmpeg 가 끊기거나 20초간 데이터가 없으면 live_play 로 주소를 새로 받아 재시작한다(호스트가 바뀔 수 있음).
"""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import time
from collections import deque
from typing import AsyncIterator

from ..db import now_ms
from ..models import SegmentEvent
from .assembly import AssemblyClient, AssemblyError, AutoStop, MeetingWatcher, hls_urls, resolve_meeting_codes
from .audio import BYTES_PER_MS, run_audio_pipeline
from .base import ClockMap, Emit, GapMarker, Source, SourceContext, SourceError, sleep_or_stop

logger = logging.getLogger(__name__)

READ_BYTES = 6400            # 200ms
STALL_SEC = 20.0


def ffmpeg_hls_cmd(url: str) -> list[str]:
    """검증된 HLS 오디오 추출 명령(마지막 세그먼트부터, 15초 읽기 타임아웃)."""
    return ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-live_start_index", "-1",
            "-rw_timeout", "15000000", "-i", url, "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
            "-f", "s16le", "-"]


async def drain_stderr(proc: asyncio.subprocess.Process, tail: deque[str]) -> None:
    if proc.stderr is None:
        return
    try:
        async for raw in proc.stderr:
            s = raw.decode("utf-8", "replace").strip()
            if s:
                tail.append(s[:300])
    except Exception:  # pragma: no cover - 방어
        pass


async def kill_proc(proc: asyncio.subprocess.Process | None, drain: bool = True) -> None:
    """서브프로세스를 죽이고 파이프를 끝까지 비워 전송 객체가 닫히게 한다(다른 코루틴이 읽는 중이면 drain=False)."""
    if proc is None:
        return
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    if drain:
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                await asyncio.wait_for(stream.read(), timeout=2)
            except (asyncio.TimeoutError, RuntimeError, OSError):
                pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:  # pragma: no cover
        pass


async def stop_task(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class AssemblyHlsSource(Source):
    """source_config: xcode·xcgcd(권장 — xcgcd 가 없으면 오늘 live_list 에서 채움) 또는 hls_url(직접 지정),
    quality('240p' 기본 — 오디오만 쓰므로 최저 화질), auto_stop_min(선택: 이 세션만 자동 종료 분)."""

    kind = "assembly_hls"
    BACKOFF_START = 5.0
    BACKOFF_MAX = 60.0
    HOUSEKEEP_SEC = 5.0

    def __init__(self, ctx: SourceContext):
        super().__init__(ctx)
        self._proc: asyncio.subprocess.Process | None = None
        self._bytes = 0
        self.clock = ClockMap()
        self.gap = GapMarker(ctx)
        self.client: AssemblyClient | None = None
        self.watcher: MeetingWatcher | None = None
        self.autostop = AutoStop(ctx, None, self.config.get("auto_stop_min"))
        self._stderr: deque[str] = deque(maxlen=20)
        self._last_url: str | None = None
        self._halt = asyncio.Event()          # 정지 요청 또는 자동 종료
        self.end_action = ""
        self.end_detail = ""

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        if not self.ctx.config.allow_assembly_network:
            raise SourceError("외부망(국회 의사중계) 호출이 꺼져 있습니다(GUKGAM_ALLOW_ASSEMBLY_NETWORK=0)")
        if not shutil.which("ffmpeg"):
            raise SourceError("ffmpeg 를 찾을 수 없습니다(설치 필요)")
        self.client = AssemblyClient()
        hk: asyncio.Task | None = None
        relay: asyncio.Task | None = None
        try:
            xcode = str(self.config.get("xcode") or "").strip()
            if not self.config.get("hls_url") and not xcode:
                raise SourceError("수집원 설정에 xcode(·xcgcd) 또는 hls_url 이 필요합니다")
            xcgcd = str(self.config.get("xcgcd") or "").strip()
            if xcode and not xcgcd and not self.config.get("hls_url"):
                xcode, xcgcd = await resolve_meeting_codes(self.ctx, self.client, self.config)
            if xcode and xcgcd:
                self.watcher = MeetingWatcher(self.ctx, self.client, xcode, xcgcd)
            self.autostop.watcher = self.watcher
            # 정지 요청과 자동 종료를 하나의 이벤트로 묶는다(ffmpeg 읽기 루프가 이것만 본다)
            relay = asyncio.create_task(self._relay_stop(stop), name=f"hls-stop-{self.ctx.sid}")
            hk = asyncio.create_task(self._housekeeping(self._halt), name=f"hls-hk-{self.ctx.sid}")

            async def emit_note(ev: SegmentEvent) -> None:
                if ev.is_final and ev.text:
                    self.autostop.note_activity(ev.ts_end or ev.ts_start)
                await emit(ev)

            await run_audio_pipeline(self.ctx, self._pcm(self._halt, xcode, xcgcd), emit_note, self.clock,
                                     key_prefix=f"h{now_ms()}-", partials=True, live=True, stop=self._halt)
            if self.end_action and not stop.is_set():
                self.ctx.set_state(phase="done", detail=f"자동 종료: {self.end_detail}")
        finally:
            for t in (hk, relay):
                if t is not None:
                    t.cancel()
            await kill_proc(self._proc)
            self._proc = None
            await self.client.aclose()

    async def _relay_stop(self, stop: asyncio.Event) -> None:
        await stop.wait()
        self._halt.set()

    async def close(self) -> None:
        await kill_proc(self._proc, drain=False)

    def _offset_ms(self) -> int:
        return self._bytes // BYTES_PER_MS

    async def _resolve_url(self, xcode: str, xcgcd: str) -> str:
        """HLS 주소. live_play 가 실패·불완전해도 이전에 받은 주소가 있으면 그것으로 재시작한다."""
        assert self.client is not None
        if self.config.get("hls_url"):
            return str(self.config["hls_url"])
        try:
            info = await self.client.live_play(xcode, xcgcd, fresh=True)
        except AssemblyError as e:
            if self._last_url:
                self.ctx.log(f"중계 정보 조회 실패 — 이전 영상 주소로 재시작합니다: {e}", "warning")
                return self._last_url
            raise
        if self.watcher is not None:
            await self.watcher.apply(info)
        urls = hls_urls(info)
        q = str(self.config.get("quality") or "240p")
        url = urls.get(q) or urls.get("default")
        if not url:
            if self._last_url:
                self.ctx.log("중계 정보에 영상(HLS) 주소가 없어 이전 주소로 재시작합니다", "warning")
                return self._last_url
            raise SourceError("이 회의는 중계영상(HLS) 주소가 없습니다")
        if urls.get("default"):
            self.ctx.set_state(hls_url=urls["default"])
        self._last_url = url
        return url

    async def _pcm(self, stop: asyncio.Event, xcode: str, xcgcd: str) -> AsyncIterator[bytes]:
        backoff = self.BACKOFF_START
        while not stop.is_set():
            try:
                url = await self._resolve_url(xcode, xcgcd)
            except AssemblyError as e:
                if "유효하지 않은" in str(e) and not self._last_url:
                    raise SourceError(str(e)) from e
                self.ctx.set_state(phase="reconnecting", error=str(e), detail=f"{backoff:.0f}초 후 재시도")
                self.ctx.log(str(e), "warning")
                if await sleep_or_stop(stop, backoff):
                    return
                backoff = min(backoff * 2, self.BACKOFF_MAX)
                continue
            self.ctx.set_state(phase="connecting", detail="중계 음성 연결 중")
            self._stderr.clear()
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_hls_cmd(url), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            self._proc = proc
            err_task = asyncio.create_task(drain_stderr(proc, self._stderr))
            started = time.monotonic()
            first = True
            reason = ""
            try:
                while not stop.is_set():
                    try:
                        data = await asyncio.wait_for(proc.stdout.read(READ_BYTES), timeout=STALL_SEC)  # type: ignore[union-attr]
                    except asyncio.TimeoutError:
                        reason = f"{STALL_SEC:.0f}초간 음성 데이터 없음"
                        break
                    if not data:
                        reason = "ffmpeg 종료" + (f": {self._stderr[-1]}" if self._stderr else "")
                        break
                    if first:
                        # 이 지점부터의 스트림 오프셋을 지금 시각에 맞춘다(재시작 사이 공백 반영)
                        self.clock.mark(self._offset_ms(), now_ms())
                        self.ctx.set_state(phase="connected", error=None, detail="중계 음성 수신 중")
                        self.ctx.log(f"중계 음성 수신 시작({url.split('/')[-2] if '/' in url else url})")
                        first = False
                    self._bytes += len(data)
                    await self.gap.on_rx()
                    self.ctx.mark_rx(position_ms=self._offset_ms())
                    yield data
            finally:
                await stop_task(err_task)
                await kill_proc(proc)
                if self._proc is proc:
                    self._proc = None
            if stop.is_set():
                return
            yield b""            # 불연속: 진행 중인 발화 구간을 여기서 확정(재시작 뒤 오디오와 이어 붙이지 않음)
            if time.monotonic() - started > 60:
                backoff = self.BACKOFF_START
            wait = backoff + random.random()
            self.ctx.log(f"중계 음성 끊김({reason}) — {wait:.0f}초 후 재시작", "warning")
            self.ctx.set_state(phase="reconnecting", detail=f"{wait:.0f}초 후 재시작")
            if await sleep_or_stop(stop, wait):
                return
            backoff = min(backoff * 2, self.BACKOFF_MAX)

    async def _housekeeping(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                gap_opened = await self.gap.check()
                if self.watcher is not None:
                    await self.watcher.tick(force=gap_opened)     # 끊기면 회의 상태(정회 등)를 바로 확인
                reason = self.autostop.check()
                if reason:
                    self.end_action = "session.auto_stop"
                    self.end_detail = reason
                    self.ctx.log(f"자동 종료: {reason}")
                    self._halt.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 방어
                logger.exception("HLS 수집 주기 점검 실패")
            if await sleep_or_stop(stop, self.HOUSEKEEP_SEC):
                return
