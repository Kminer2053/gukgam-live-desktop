"""수집 관리자: 세션별 수집원 태스크를 띄우고 감독하며 상태 전이(idle|live|paused|ended)를 맡는다.

- 모든 상태 전이는 repo.audit() + bus 'session.status'(serialize.session + stats).
- 수집원이 예외로 죽으면 source_state.phase='error', 세션은 'paused'.
- 수집원이 정상 종료(파일·리플레이 끝, 산회 후 자동 종료)하면 stop() 과 같은 처리(status='ended').
  수집원이 end_action 을 남기면(예: 'session.auto_stop') 그 이름으로 감사 기록을 남긴다.
- pause/stop 은 수집원을 멈춘 뒤 pipeline.flush(): 미확정 발언 확정·분석. 열린 블록·대기 호명은
  stop 때만 닫는다(일시정지·오류로 인한 자동 일시정지는 유지 — 이어서 수집하면 같은 질의 블록에 붙는다).
- 정지 대기 시간은 남은 음성인식 대기열에 맞춰 늘린다(기본 15초, 최대 120초).
- 보존 기간 정리(gukgam.maintenance.RetentionWorker)를 recover() 에서 시작하고 shutdown() 에서 멈춘다.
- manual 수집원은 태스크 없이 status 만 live.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .. import serialize
from ..db import now_ms
from ..models import SegmentEvent
from .base import Source, SourceContext, SourceError

logger = logging.getLogger(__name__)

SOURCE_CLASSES: dict[str, tuple[str, str]] = {
    "assembly_caption": ("assembly", "AssemblyCaptionSource"),
    "assembly_hls": ("hls", "AssemblyHlsSource"),
    "browser": ("browser", "BrowserAudioSource"),
    "file": ("file", "FileSource"),
    "replay": ("replay", "ReplaySource"),
}


def make_source(ctx: SourceContext) -> Source | None:
    """source_type 에 맞는 수집원(manual 은 None). 모듈은 필요할 때만 import 한다."""
    st = ctx.session.get("source_type")
    if st == "manual":
        return None
    spec = SOURCE_CLASSES.get(str(st))
    if spec is None:
        raise SourceError(f"알 수 없는 수집원 종류입니다: {st}")
    mod = importlib.import_module(f".{spec[0]}", __package__)
    return getattr(mod, spec[1])(ctx)


@dataclass
class _Run:
    sid: str
    ctx: SourceContext
    source: Source | None
    stop: asyncio.Event
    task: asyncio.Task | None = None
    emit_errors: int = 0


class IngestManager:
    HALT_TIMEOUT = 15.0       # pause/stop 때 수집원이 스스로 끝나길 기다리는 기본 시간
    HALT_MAX = 120.0          # 음성인식 대기열이 남아 있을 때 늘려 기다리는 최대 시간
    STT_ITEM_SEC = 4.0        # 대기 구간 1건 전사 예상 시간(실측 지연이 없을 때)

    def __init__(self, rt: Any):
        self.rt = rt
        self._runs: dict[str, _Run] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.retention: Any = None

    def audio_input_running(self) -> bool:
        """음성 수신·분리 도중 모델 준비가 시작되어 전사를 막지 않게 한다."""
        return any(run.ctx.session.get("source_type") in {"file", "assembly_hls", "browser"}
                   for run in self._runs.values())

    # ── 조회 ──────────────────────────────────────────────────
    def is_running(self, sid: str) -> bool:
        return sid in self._runs

    def browser_feed(self, sid: str) -> Callable[[bytes], None] | None:
        """WS 수신 PCM 을 넘길 콜백(브라우저 수집원이 수집 중일 때만)."""
        run = self._runs.get(sid)
        if run is None or run.source is None or run.source.kind != "browser":
            return None
        return getattr(run.source, "feed", None)

    def source(self, sid: str) -> Source | None:
        run = self._runs.get(sid)
        return run.source if run else None

    # ── 상태 전이 ─────────────────────────────────────────────
    def _lock(self, sid: str) -> asyncio.Lock:
        lk = self._locks.get(sid)
        if lk is None:
            lk = self._locks[sid] = asyncio.Lock()
        return lk

    def _publish(self, sid: str) -> dict:
        s = self.rt.repo.get_session(sid)
        if s is not None:
            self.rt.bus.publish(sid, "session.status", serialize.session(s, self.rt.repo.session_stats(sid)))
        return s  # type: ignore[return-value]

    async def recover(self) -> None:
        """기동 시: 직전 실행에서 수집 중(live)이던 세션을 일시정지로 표시(자동 재개 안 함). 보존 기간 정리 시작."""
        for s in self.rt.repo.list_sessions(limit=10000, status="live"):
            st = dict(s.get("source_state") or {})
            st.update(phase="stopped", detail="서버 재시작으로 일시정지됨 — 다시 시작하세요")
            self.rt.repo.update_session(s["id"], status="paused", source_state=st)
            self.rt.repo.audit("system", "session.recover", s["id"], "live → paused")
            logger.info("세션 %s: 서버 재시작으로 일시정지", s["id"])
        if self.retention is None:
            from ..maintenance import RetentionWorker

            self.retention = RetentionWorker(self.rt)
        self.retention.start()

    async def start(self, sid: str, actor: str = "") -> dict:
        async with self._lock(sid):
            s = self.rt.repo.get_session(sid)
            if s is None:
                raise KeyError(sid)
            if sid in self._runs:
                return self._publish(sid)
            fields: dict[str, Any] = {"status": "live"}
            if not s.get("started_at"):
                fields["started_at"] = now_ms()
            if s.get("status") == "ended":
                fields["ended_at"] = None
            s = self.rt.repo.update_session(sid, **fields)
            self.rt.repo.audit(actor, "session.start", sid, {"source_type": s["source_type"]})
            try:
                self.rt.pipeline(sid).reload_settings()     # 명단·감시 키워드 최신화
            except Exception:  # pragma: no cover - 방어
                logger.exception("파이프라인 설정 갱신 실패: %s", sid)
            ctx = SourceContext(self.rt, s)
            try:
                source = make_source(ctx)
            except Exception as e:
                msg = str(e) if isinstance(e, SourceError) else f"{type(e).__name__}: {e}"
                ctx.log(f"수집 시작 실패: {msg}", "error")
                ctx.set_state(phase="error", error=msg)
                self.rt.repo.update_session(sid, status="paused")
                self.rt.repo.audit("system", "session.error", sid, msg)
                ctx.close()
                return self._publish(sid)
            run = _Run(sid=sid, ctx=ctx, source=source, stop=asyncio.Event())
            self._runs[sid] = run
            if source is None:
                ctx.set_state(phase="connected", error=None, detail="수기 입력 대기")
            else:
                ctx.set_state(phase="connecting", error=None, detail="수집 시작")
                run.task = asyncio.create_task(self._supervise(run), name=f"ingest-{sid}")
            ctx.flush_state()
            # 자막 블록 보정에는 별도 음성 연결이나 PCM 수집을 시작하지 않는다.
            return self._publish(sid)

    async def pause(self, sid: str, actor: str = "") -> dict:
        async with self._lock(sid):
            if self.rt.repo.get_session(sid) is None:
                raise KeyError(sid)
            run = self._runs.pop(sid, None)
            await self._halt(run)
            return await self._transition(sid, "paused", actor, "session.pause", run)

    async def stop(self, sid: str, actor: str = "") -> dict:
        async with self._lock(sid):
            if self.rt.repo.get_session(sid) is None:
                raise KeyError(sid)
            run = self._runs.pop(sid, None)
            await self._halt(run)
            return await self._transition(sid, "ended", actor, "session.stop", run)

    async def shutdown(self) -> None:
        """서버 종료: 모든 수집원 정지(ffmpeg 등 서브프로세스 kill 포함). 세션 상태는 recover() 가 다음 기동 때 정리."""
        if self.retention is not None:
            await self.retention.stop()
        runs = list(self._runs.values())
        self._runs.clear()
        for r in runs:
            r.stop.set()
        tasks = [r.task for r in runs if r.task is not None]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=5)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for r in runs:
            if r.source is not None:
                try:
                    await r.source.close()
                except Exception:  # pragma: no cover - 방어
                    logger.exception("수집원 정리 실패: %s", r.sid)
            try:
                if r.ctx.state.get("phase") not in ("error", "done"):
                    r.ctx.state.update(phase="stopped", detail="서버 종료")
                r.ctx.close()
            except Exception:  # pragma: no cover - DB 가 먼저 닫힌 경우
                pass

    # ── 내부 ──────────────────────────────────────────────────
    def halt_timeout(self, run: _Run) -> float:
        """정지 대기 시간: 기본 + 남은 음성인식 구간 수 × 구간당 예상 시간(최대 HALT_MAX)."""
        backlog = int(getattr(run.ctx, "stt_backlog", 0) or 0)
        if backlog <= 0:
            return self.HALT_TIMEOUT
        last = getattr(getattr(self.rt, "stt", None), "last_latency_ms", None)
        per = max(1.0, min(30.0, (last / 1000 * 1.5) if isinstance(last, (int, float)) and last else self.STT_ITEM_SEC))
        return min(self.HALT_MAX, self.HALT_TIMEOUT + (backlog + 1) * per)

    async def _halt(self, run: _Run | None) -> None:
        if run is None:
            return
        audio = getattr(self.rt, "audio_review", None)
        if audio is not None:
            await audio.stop(run.sid)
        run.stop.set()
        if run.task is not None and not run.task.done():
            timeout = self.halt_timeout(run)
            try:
                await asyncio.wait_for(asyncio.shield(run.task), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("수집원이 %.0f초 안에 멈추지 않아 취소합니다: %s", timeout, run.sid)
                run.ctx.log(f"수집원이 {timeout:.0f}초 안에 멈추지 않아 강제로 정지했습니다", "warning")
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)
            except Exception:
                pass  # 수집원 오류는 _supervise 에서 기록
        if run.source is not None:
            try:
                await run.source.close()
            except Exception:  # pragma: no cover - 방어
                logger.exception("수집원 정리 실패: %s", run.sid)

    async def _transition(self, sid: str, status: str, actor: str, action: str, run: _Run | None,
                          detail: Any = "") -> dict:
        try:
            # 종료만 열린 블록·대기 호명을 닫는다. 일시정지(오류로 인한 자동 일시정지 포함)는 이어 받을 수 있게 둔다.
            await self.rt.pipeline(sid).flush(close_blocks=(status == "ended"))
        except Exception:  # pragma: no cover - 방어
            logger.exception("pipeline.flush 실패: %s", sid)
        fields: dict[str, Any] = {"status": status}
        if status == "ended":
            fields["ended_at"] = now_ms()
        self.rt.repo.update_session(sid, **fields)
        ctx = run.ctx if run is not None else SourceContext(self.rt, self.rt.repo.get_session(sid))
        if run is None:
            ctx.state.update({k: v for k, v in ctx.prev_state.items() if k != "log"})
        if ctx.state.get("phase") not in ("error", "done"):
            ctx.state["phase"] = "stopped"
            ctx.state["detail"] = "일시정지" if status == "paused" else "수집 종료"
        ctx.close()
        self.rt.repo.audit(actor, action, sid, detail)
        return self._publish(sid)

    async def _finish(self, run: _Run, status: str, action: str, detail: Any = "") -> None:
        """수집원 태스크 안에서 스스로 끝났을 때(오류·입력 끝). 이미 다른 쪽이 정지 중이면 아무것도 하지 않는다."""
        if self._runs.get(run.sid) is not run:
            return
        self._runs.pop(run.sid, None)
        async with self._lock(run.sid):
            audio = getattr(self.rt, "audio_review", None)
            if audio is not None:
                await audio.stop(run.sid)
            if run.source is not None:
                try:
                    await run.source.close()
                except Exception:  # pragma: no cover - 방어
                    logger.exception("수집원 정리 실패: %s", run.sid)
            await self._transition(run.sid, status, "system", action, run, detail)

    def _emit(self, run: _Run) -> Callable[[SegmentEvent], Any]:
        async def emit(ev: SegmentEvent) -> None:
            try:
                await self.rt.pipeline(run.sid).handle(ev)
            except Exception as e:
                run.emit_errors += 1
                logger.exception("발언 처리 실패(%s): %s", run.sid, ev.key)
                if run.emit_errors <= 3 or run.emit_errors % 100 == 0:
                    run.ctx.log(f"발언 처리 오류({run.emit_errors}회): {type(e).__name__} {e}", "error")
        return emit

    async def _supervise(self, run: _Run) -> None:
        assert run.source is not None
        try:
            await run.source.run(self._emit(run), run.stop)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = str(e) if isinstance(e, SourceError) else f"{type(e).__name__}: {e}"
            if not isinstance(e, SourceError):
                logger.exception("수집원 오류(%s)", run.sid)
            run.ctx.log(f"수집 중단: {msg}", "error")
            run.ctx.set_state(phase="error", error=msg, detail="오류로 수집이 멈췄습니다")
            await self._finish(run, "paused", "session.error", msg)
            return
        if run.stop.is_set():
            return          # 정지 요청 — 전이는 요청한 쪽(pause/stop/shutdown)이 처리
        action = str(getattr(run.source, "end_action", "") or "session.stop")
        detail = str(getattr(run.source, "end_detail", "") or "입력 끝")
        run.ctx.log("산회 후 자동 종료합니다" if action == "session.auto_stop" else "수집원 입력이 끝나 세션을 종료합니다")
        if run.ctx.state.get("phase") != "done":
            run.ctx.set_state(phase="done")
        await self._finish(run, "ended", action, detail)
