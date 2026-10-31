"""수집원 공통 기반: SourceContext(상태·로그 발행), Source ABC, 공용 도우미(끊김 표지·시각 매핑)."""
from __future__ import annotations

import asyncio
import bisect
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import serialize, settings
from ..config import KST
from ..db import now_ms
from ..models import SegmentEvent

logger = logging.getLogger(__name__)

Emit = Callable[[SegmentEvent], Awaitable[None]]

LOG_LINES = 50
GAP_MS = 60_000          # 이 시간 이상 아무것도 못 받으면 '수신 없음' 표지

LEVEL_TAGS = {"warning": "[주의] ", "error": "[오류] "}


class SourceError(RuntimeError):
    """수집원이 더 진행할 수 없는 오류(설정·환경 문제). 메시지는 화면에 그대로 보인다."""


def hm(ms: int | None) -> str:
    """epoch ms → 'HH:MM'(KST)."""
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%H:%M")


def default_state() -> dict[str, Any]:
    return {"phase": "connecting", "detail": "", "last_rx_ms": None, "latency_ms": None, "rx_count": 0,
            "error": None, "log": []}


class SourceContext:
    """매니저가 세션마다 하나 만들어 수집원에 넘긴다. source_state 저장과 'session.status' 발행을 맡는다.

    발행은 1초에 한 번으로 묶고(phase·error 변경과 경고 로그는 즉시) 남은 변경은 타이머로 밀어낸다.
    """

    THROTTLE_SEC = 1.0

    def __init__(self, rt: Any, session: dict[str, Any]):
        self.rt = rt
        self.session = session
        self.sid: str = session["id"]
        # 이전 실행의 상태(리플레이 위치·파일 진행 위치 같은 재개 정보를 읽을 때 쓴다)
        self.prev_state: dict[str, Any] = dict(session.get("source_state") or {})
        self.state: dict[str, Any] = default_state()
        self.state["log"] = list(self.prev_state.get("log") or [])[-LOG_LINES:]
        self._last_pub = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False
        # 음성 경로: 아직 전사하지 못한 확정 구간 수(처리 중 포함). 매니저가 정지 대기 시간을 정할 때 본다.
        self.stt_backlog = 0

    # ── 접근자 ────────────────────────────────────────────────
    @property
    def config(self):
        return self.rt.config

    @property
    def repo(self):
        return self.rt.repo

    @property
    def pipeline(self):
        return self.rt.pipeline(self.sid)

    @property
    def source_config(self) -> dict[str, Any]:
        return self.session.get("source_config") or {}

    def settings(self, key: str) -> Any:
        return settings.get(self.repo, key)

    # ── 상태 ──────────────────────────────────────────────────
    def set_state(self, **state: Any) -> None:
        urgent = False
        if "phase" in state and state["phase"] != self.state.get("phase"):
            urgent = True
        if "error" in state and state["error"] != self.state.get("error"):
            urgent = True
        self.state.update(state)
        self._schedule(urgent)

    def log(self, msg: str, level: str = "info") -> None:
        line = f"{serialize.hms(now_ms())} {LEVEL_TAGS.get(level, '')}{msg}"
        logs = self.state.setdefault("log", [])
        logs.append(line)
        del logs[:-LOG_LINES]
        getattr(logger, level if level in ("debug", "info", "warning", "error") else "info")("[%s] %s", self.sid, msg)
        self._schedule(level in ("warning", "error"))

    def _schedule(self, urgent: bool) -> None:
        now = time.monotonic()
        if urgent or now - self._last_pub >= self.THROTTLE_SEC:
            self.flush_state()
            return
        if self._timer is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.flush_state()
                return
            self._timer = loop.call_later(self.THROTTLE_SEC - (now - self._last_pub), self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        self.flush_state()

    def flush_state(self) -> None:
        """source_state 를 DB 에 쓰고 'session.status' 를 발행한다."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._last_pub = time.monotonic()
        try:
            s = self.repo.update_session(self.sid, source_state=self.state)
            if s is not None:
                self.rt.bus.publish(self.sid, "session.status",
                                    serialize.session(s, self.repo.session_stats(self.sid)))
        except Exception:  # pragma: no cover - DB 종료 중 등
            logger.exception("source_state 저장 실패: %s", self.sid)

    def mark_rx(self, n: int = 1, **extra: Any) -> None:
        """수신 기록(rx_count·last_rx_ms) 갱신. 처음 받으면 phase='receiving'."""
        st: dict[str, Any] = {"rx_count": int(self.state.get("rx_count") or 0) + n, "last_rx_ms": now_ms()}
        if self.state.get("phase") not in ("receiving",):
            st["phase"] = "receiving"
            st["error"] = None
        st.update(extra)
        self.set_state(**st)

    def close(self) -> None:
        """남은 상태를 즉시 기록하고 타이머를 정리한다."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.flush_state()
        self._closed = True


class Source(ABC):
    """수집원. run() 은 stop 이 set 될 때까지(또는 입력이 끝날 때까지) 수집해 emit 으로 넘긴다.

    일시 오류는 내부에서 재시도하고, 계속할 수 없으면 SourceError 를 올린다(매니저가 error/paused 처리).
    정상 반환 + stop 미설정 = 입력 끝(파일·리플레이) → 매니저가 세션을 종료한다.
    """

    kind: str = ""

    def __init__(self, ctx: SourceContext):
        self.ctx = ctx
        self.config: dict[str, Any] = dict(ctx.source_config)

    @abstractmethod
    async def run(self, emit: Emit, stop: asyncio.Event) -> None: ...

    async def close(self) -> None:
        """강제 정리(서브프로세스 kill 등). 여러 번 불려도 안전해야 한다."""
        return None


# ── 공용 도우미 ─────────────────────────────────────────────────
async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """seconds 동안 기다린다. 그 사이 stop 이 set 되면 True."""
    if stop.is_set():
        return True
    if seconds <= 0:
        return stop.is_set()
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


class ClockMap:
    """스트림 오프셋(ms) → 실제 시각(epoch ms). 재시작·끊김마다 기준점을 추가한다."""

    def __init__(self, offset_ms: int = 0, wall_ms: int | None = None):
        self._offs: list[int] = []
        self._walls: list[int] = []
        if wall_ms is not None:
            self.mark(offset_ms, wall_ms)

    def mark(self, offset_ms: int, wall_ms: int) -> None:
        if self._offs and offset_ms <= self._offs[-1]:
            # 같은 위치(또는 되돌아감)면 마지막 기준점을 교체
            while self._offs and offset_ms <= self._offs[-1]:
                self._offs.pop()
                self._walls.pop()
        self._offs.append(int(offset_ms))
        self._walls.append(int(wall_ms))

    def __call__(self, offset_ms: int) -> int:
        if not self._offs:
            return int(offset_ms)
        i = bisect.bisect_right(self._offs, offset_ms) - 1
        i = max(i, 0)
        return self._walls[i] + int(offset_ms) - self._offs[i]


class GapMarker:
    """GAP_MS 이상 아무것도 못 받으면 '— 수신 없음(HH:MM~) —' 표지를 남기고, 재개 시 종료 시각을 채운다(같은 key upsert).

    한 번이라도 받은 뒤에만 표지를 남긴다(회의 시작 전 대기는 표지 대상이 아니다).
    """

    def __init__(self, ctx: SourceContext, threshold_ms: int = GAP_MS):
        self.ctx = ctx
        self.threshold_ms = threshold_ms
        self.last_rx_ms: int | None = None
        self.open_key: str | None = None
        self.gap_start: int | None = None

    async def on_rx(self, now: int | None = None) -> None:
        now = now or now_ms()
        if self.open_key is not None and self.gap_start is not None:
            text = f"— 수신 없음({hm(self.gap_start)}~{hm(now)}) —"
            try:
                await self.ctx.pipeline.add_marker(text, ts=self.gap_start, key=self.open_key)
            except Exception:  # pragma: no cover - 방어
                logger.exception("수신 재개 표지 갱신 실패")
            self.ctx.log(f"수신 재개({hm(self.gap_start)}~{hm(now)} 끊김)")
            self.open_key = None
            self.gap_start = None
        self.last_rx_ms = now

    async def check(self, now: int | None = None) -> bool:
        """주기 호출. 새로 끊김 표지를 남겼으면 True."""
        now = now or now_ms()
        if self.last_rx_ms is None or self.open_key is not None:
            return False
        if now - self.last_rx_ms < self.threshold_ms:
            return False
        self.gap_start = self.last_rx_ms
        self.open_key = f"gap{self.gap_start}"
        try:
            await self.ctx.pipeline.add_marker(f"— 수신 없음({hm(self.gap_start)}~) —", ts=self.gap_start,
                                               key=self.open_key)
        except Exception:  # pragma: no cover - 방어
            logger.exception("수신 없음 표지 기록 실패")
        self.ctx.log(f"{self.threshold_ms // 1000}초 이상 수신 없음({hm(self.gap_start)}~)", "warning")
        return True


def resolve_path(ctx: SourceContext, p: str) -> Path:
    """상대 경로는 업로드 폴더 → 데이터 폴더 → 프로젝트 루트 순으로 찾는다."""
    from ..config import ROOT

    path = Path(p).expanduser()
    if path.is_absolute():
        return path
    for base in (ctx.config.upload_dir, ctx.config.data_dir, ROOT):
        cand = Path(base) / path
        if cand.exists():
            return cand
    return ROOT / path
