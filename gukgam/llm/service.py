"""AI 분석 서비스(pipeline.AnalysisHooks 구현).

작업 큐 1개(동시 1건)로 블록 요약·회의 요약을 돌린다.
  우선순위: 수동 요청(0) > 블록 요약(1) > 롤링·전체 요약(2). 같은 대상의 중복 요청은 하나로 합친다.
  블록이 닫히면 3초 디바운스 후 요약하고, 수집 중에는 rolling_interval_sec 마다 롤링 요약을 만든다.
어떤 제공자가 실패해도 규칙 기반 요약으로 같은 형태의 결과를 남긴다(provider='rules(대체)').
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import itertools
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .. import serialize, settings
from ..config import KST
from ..db import now_ms
from ..log_privacy import exception_label, failure_reason, public_error
from ..models import STATUS_LABELS
from ..nlp import rules
from ..nlp.text import split_sentences
from ..repo import fingerprint
from . import prompts
from .providers import (SIM_ANY_UTTERANCE, SIM_SAME_UTTERANCE, LLMParseError, LLMProvider, LocalMLXProvider,
                        RulesProvider, block_ref_utterance, ground_org_relevance, ground_ref_uids, make_provider,
                        merge_actions, merge_rule_refs, parse_json, provider_signature, rules_block_summary,
                        rules_session_summary, session_actions, session_org_points, text_overlap)

log = logging.getLogger(__name__)


def _review_input_signature(rows: list[dict]) -> str:
    """요약에 실제 사용한 원문 범위·화자 표시 판본을 식별한다."""
    data = [(u["id"], u.get("text"), u.get("speaker_role"), u.get("speaker")) for u in rows]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode("utf-8")).hexdigest()


def _source_sentence(text: str, utterance_text: str) -> str:
    """LLM 요지(text)와 가장 많이 겹치는 원문 문장(기한·담당 추정용). 겹치는 문장이 없으면 ''."""
    best, score = "", 0.0
    for s in split_sentences(utterance_text or ""):
        sc = text_overlap(text, s)
        if sc > score:
            best, score = s, sc
    return best if score >= SIM_SAME_UTTERANCE else ""

# '우리 기관'을 주어로 한 문장만 본다(다른 기관·쟁점 문장의 '관련 규정이 없는' 등은 건드리지 않음)
_ORG_SUBJ = r"우리\s*기관(?:\s*\([^)]{0,30}\))?"
_NO_ORG = re.compile(_ORG_SUBJ + r".{0,30}?(?:관련|해당|다룬|지목|언급).{0,20}?"
                     r"(?:없었|없음|없다|없는|없으며|없고|확인되지\s*않|않았)")
# 건수·존재 주장은 '질의'에 대한 것만(‘우리 기관 관련 요구자료 3건’ 같은 다른 건수는 건드리지 않음)
_ORG_COUNT = re.compile("(" + _ORG_SUBJ + r"(?:과|와|에|을|를|이|의)?\s*(?:직접\s*)?"
                        r"(?:관련(?:된|한)?|해당(?:하는|된)?|다룬|지목한)\s*(?:위원(?:의)?\s*)?(?:질의|질문)"
                        r"[^.。\d]{0,8}?)(\d+)(\s*건)")
_ORG_CLAIM = re.compile(_ORG_SUBJ + r".{0,30}?(?:관련|해당|다룬|지목).{0,10}?(?:질의|질문).{0,15}?"
                        r"(?:있었|있음|있다|제기|나왔|이어짐|집중)")
NO_ORG_SENTENCE = "우리 기관을 직접 다룬 질의는 확인되지 않음."


def ground_overview(overview: str, org_pairs: list[tuple[dict, dict]]) -> str:
    """LLM 개요가 언급 근거로 확정된 '우리 기관 관련 질의' 사실과 어긋나지 않게 맞춘다.

    - 관련 블록이 있으면: '관련 질의 없음' 문장은 지우고, 'N건' 숫자는 실제 건수로 고친다. 건수 문장이 없으면
      사실 문장(건수·위원)을 넣는다(관련 질의가 있었다는 막연한 문장은 사실 문장으로 바꾼다).
    - 관련 블록이 없으면: '관련 질의 N건/있었음' 주장은 '확인되지 않음' 문장으로 바꾼다.
    """
    sents = [s for s in re.split(r"(?<=[.。])\s+", (overview or "").strip()) if s]
    n = len(org_pairs)
    out: list[str] = []
    stated = False
    if n:
        names = []
        for b, _sm in org_pairs:
            nm = b.get("questioner") or "성명 미확인"
            if nm not in names:
                names.append(nm)
        fact = f"우리 기관 관련 질의 {n}건({', '.join(f'{x} 위원' if x != '성명 미확인' else x for x in names)})이 있었음."
        claim_at = None
        for s in sents:
            if _ORG_COUNT.search(s):
                out.append(_ORG_COUNT.sub(lambda m: f"{m.group(1)}{n}{m.group(3)}", s))
                stated = True
            elif _NO_ORG.search(s):
                continue
            else:
                if claim_at is None and _ORG_CLAIM.search(s):
                    claim_at = len(out)
                out.append(s)
        if not stated:
            if claim_at is not None:
                out[claim_at] = fact
            else:
                out.append(fact)
        return " ".join(out)
    for s in sents:
        if _ORG_COUNT.search(s) or (_ORG_CLAIM.search(s) and not _NO_ORG.search(s)):
            if not stated:
                out.append(NO_ORG_SENTENCE)
                stated = True
            continue
        if _NO_ORG.search(s):
            if stated:
                continue
            stated = True
        out.append(s)
    return " ".join(out)

PRIO_MANUAL, PRIO_BLOCK, PRIO_SESSION = 0, 1, 2
BLOCK_DEBOUNCE_SEC = 3.0
SESSION_DEBOUNCE_SEC = 5.0
TICK_SEC = 5.0
IDLE_UNLOAD_SEC = 1800          # 로컬 모델이 30분간 놀면 프로세스를 내려 메모리를 돌려준다
SESSION_DIGEST_CHARS = 9000     # 회의 요약 입력(블록 요지) 최대 글자 수
RECENT_UTTERANCES = 30
SESSION_UTTERANCE_LIMIT = 300  # 질의 미분류 수동 회의 요약만 적용. 롤링 30행은 유지한다.

FALLBACK_LABEL = "rules(대체)"

# 설정 화면 '연결 시험'용 짧은 예문(가상의 질의·답변)
_SAMPLE_TS = 1791385063000
_SAMPLE_BLOCK = {"id": 0, "kind": "question", "questioner": "홍길동", "questioner_party": "", "ts_start": _SAMPLE_TS,
                 "ts_end": _SAMPLE_TS + 60000}
_SAMPLE_UTTS = [
    {"id": 1, "ts_start": _SAMPLE_TS, "speaker": "홍길동", "speaker_role": "member",
     "text": "공공발주 공사에서 하도급 관리 실태가 어떻게 되는지 관련 자료를 종합감사 전까지 제출해 주십시오"},
    {"id": 2, "ts_start": _SAMPLE_TS + 30000, "speaker": "답변", "speaker_role": "answer",
     "text": "예 확인해서 종합감사 전까지 제출하도록 하겠습니다"},
]


@dataclass
class Job:
    kind: str                      # block|session
    sid: str
    block_id: int | None = None
    scope: str = "session"         # session|rolling (kind='session')
    priority: int = PRIO_SESSION
    manual: bool = False
    futures: list[asyncio.Future] = field(default_factory=list)
    requested_ms: int = field(default_factory=now_ms)   # (수동 요청이면) 요청 시각 — 이후의 사람 수정은 덮어쓰지 않는다

    @property
    def key(self) -> tuple:
        return ("block", self.block_id) if self.kind == "block" else ("session", self.sid, self.scope)

    @property
    def label(self) -> str:
        if self.kind == "block":
            return f"블록 #{self.block_id} 요약"
        return "롤링 요약" if self.scope == "rolling" else "회의 전체 요약"


class AnalysisService:
    def __init__(self, rt: Any):
        self.rt = rt
        self._queue: asyncio.PriorityQueue[tuple[int, int, tuple]] = asyncio.PriorityQueue()
        self._pending: dict[tuple, Job] = {}
        self._timers: dict[tuple, tuple[asyncio.TimerHandle, Job]] = {}
        self._seq = itertools.count()
        self._running: Job | None = None
        self._worker: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None
        self._provider: LLMProvider | None = None
        self._provider_sig = ""
        self._provider_activities: dict[object, dict[str, Any]] = {}
        self.last_activity: dict[str, Any] | None = None
        self._rolling: dict[str, dict[str, Any]] = {}
        self._closed = False
        self.last_error: str | None = None
        self.last_latency_ms: int | None = None
        self.last_provider: str | None = None

    # ── 수명주기 ─────────────────────────────────────────────
    async def start(self) -> None:
        self._closed = False
        self._ensure_tasks()

    def _ensure_tasks(self) -> None:
        if self._closed:
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="analysis-worker")
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick(), name="analysis-ticker")

    async def shutdown(self) -> None:
        self._closed = True
        for handle, _job in self._timers.values():
            handle.cancel()
        self._timers.clear()
        for t in (self._worker, self._ticker):
            if t is not None:
                t.cancel()
        for t in (self._worker, self._ticker):
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._worker = self._ticker = None
        for job in list(self._pending.values()):
            self._fail(job, RuntimeError("AI 분석 서비스가 종료되었습니다"))
        self._pending.clear()
        if self._provider is not None:
            try:
                await self._provider.close()
            except Exception as e:  # pragma: no cover - 방어
                log.error("LLM 제공자 종료 실패(%s)", exception_label(e))
            self._provider = None

    # ── 설정·제공자 ──────────────────────────────────────────
    def _llm_cfg(self) -> dict[str, Any]:
        return settings.get(self.rt.repo, "llm") or {}

    def _heavy_off(self) -> bool:
        return bool(getattr(self.rt.config, "disable_heavy_engines", False))

    async def _get_provider(self) -> tuple[LLMProvider, dict[str, Any]]:
        cfg = self._llm_cfg()
        sig = provider_signature(cfg) + ("|light" if self._heavy_off() else "")
        if self._provider is None or sig != self._provider_sig:
            old = self._provider
            self._provider = make_provider(cfg, self.rt)
            self._provider_sig = sig
            if old is not None:
                try:
                    await old.close()
                except Exception as e:  # pragma: no cover - 방어
                    log.error("이전 LLM 제공자 종료 실패(%s)", exception_label(e))
        return self._provider, cfg

    # ── 파이프라인 훅 ────────────────────────────────────────
    def is_block_running(self, sid: str, block_id: int) -> bool:
        """선행 요약이 끝나기 전에 같은 질의의 음성 입력 판본을 확정하지 않는다."""
        return bool(self._running and self._running.kind == "block"
                    and self._running.sid == sid and self._running.block_id == block_id)

    def _question_audio_holds(self, sid: str, block_id: int, *, completing: bool = False) -> bool:
        holds = getattr(getattr(self.rt, "question_audio", None), "blocks_automatic_summary", None)
        return bool(holds and holds(sid, block_id, completing=completing))

    def on_block_closed(self, sid: str, block_id: int) -> None:
        stages = getattr(self.rt, "stages", None)
        if stages is not None and stages.owns(sid):
            stages.on_block_closed(sid, block_id)
            return  # 종료된 원수신 블록의 보정과 같은 본문 기반 요약을 한 번 수행한다.
        review = getattr(self.rt, "reviews", None)
        def flush_context():
            if not self._closed and review is not None and hasattr(review, "flush_context"):
                review.flush_context(sid, block_id)
        # 과거 수동 검토 객체의 종료 계약만 유지한다. 새 자막 블록 경로와 활성화 API에서는 호출하지 않는다.
        audio = getattr(self.rt, "question_audio", None)
        if not self._closed and audio is not None:
            if audio.on_block_closed(sid, block_id):
                flush_context()
                return
        review = getattr(self.rt, "reviews", None)
        if not self._closed and review is not None:
            after = lambda: self.on_review_accepted(sid, block_id)
            if review.schedule(sid, block_id, after=after):
                flush_context()
                return
        self.on_review_accepted(sid, block_id)
        flush_context()

    def on_review_accepted(self, sid: str, block_id: int) -> None:
        """화자 검토가 끝나거나 판본을 채택한 뒤 기존 보호 규칙으로 자동 요약한다."""
        if self._uses_stage_summary(sid):
            return  # 종료 보정에서 이미 같은 문답 판본의 의원별 요약을 만든다.
        if self._closed or not self._llm_cfg().get("auto_block_summary", True) \
                or self._question_audio_holds(sid, block_id):
            return
        block = self.rt.repo.get_block(block_id)
        if not block or block.get("is_open"):
            return  # 의심 창의 중간 채택은 질의 전체 자동 요약을 앞당기지 않는다.
        self._schedule(Job("block", sid, block_id=block_id, priority=PRIO_BLOCK), BLOCK_DEBOUNCE_SEC)

    def on_review_display_changed(self, sid: str, block_id: int) -> None:
        """채택·거절한 표시 판본을 전체·롤링 요약에도 다시 반영한다."""
        if self._closed or not settings.get(self.rt.repo, "transcript_review").get("enabled"):
            return
        session = self.rt.repo.get_session(sid)
        if session:
            scope = "rolling" if session.get("status") == "live" else "session"
            self._schedule(Job("session", sid, scope=scope, priority=PRIO_SESSION), SESSION_DEBOUNCE_SEC)

    def on_utterance_final(self, sid: str, u: dict) -> None:
        stages = getattr(self.rt, "stages", None)
        if stages is not None and stages.owns(sid):
            return
        if (u.get("speaker_role") or "") == "system":
            return
        review = getattr(self.rt, "reviews", None)
        if review is not None:
            review.note_utterance(sid, u)
        st = self._rolling.setdefault(sid, {"dirty": False, "last_run": time.monotonic()})
        st["dirty"] = True
        st["last_utt_ms"] = u.get("ts_end") or u.get("ts_start")

    def on_session_stopped(self, sid: str) -> None:
        if self._closed:
            return
        stages = getattr(self.rt, "stages", None)
        if stages is not None and stages.owns(sid):
            stages.flush(sid)
            return
        self._rolling.pop(sid, None)
        review = getattr(self.rt, "reviews", None)
        if review is not None and hasattr(review, "flush_context"):
            review.flush_context(sid)
        self._schedule(Job("session", sid, scope="session", priority=PRIO_SESSION), SESSION_DEBOUNCE_SEC)

    # ── 큐 ──────────────────────────────────────────────────
    def _schedule(self, job: Job, delay: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        prev = self._timers.pop(job.key, None)
        if prev:
            prev[0].cancel()
        self._timers[job.key] = (loop.call_later(delay, self._fire, job), job)
        self._publish_status(job.sid)

    def _fire(self, job: Job) -> None:
        cur = self._timers.get(job.key)
        if cur and cur[1] is job:
            self._timers.pop(job.key, None)
        self._enqueue(job)

    def _enqueue(self, job: Job) -> Job:
        self._ensure_tasks()
        ex = self._pending.get(job.key)
        if ex is not None:
            ex.futures.extend(job.futures)
            if job.manual:
                ex.requested_ms = max(ex.requested_ms, job.requested_ms)
            ex.manual = ex.manual or job.manual
            if job.priority < ex.priority:
                ex.priority = job.priority
                self._queue.put_nowait((ex.priority, next(self._seq), ex.key))
            self._publish_status(job.sid)
            return ex
        self._pending[job.key] = job
        self._queue.put_nowait((job.priority, next(self._seq), job.key))
        self._publish_status(job.sid)
        return job

    async def _submit(self, job: Job) -> dict:
        if self._closed:
            raise RuntimeError("AI 분석 서비스가 종료되었습니다")
        prev = self._timers.pop(job.key, None)   # 수동 요청이 디바운스 대기 작업을 대신한다
        if prev:
            prev[0].cancel()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        job.futures.append(fut)
        self._enqueue(job)
        return await fut

    @staticmethod
    def _fail(job: Job, exc: BaseException) -> None:
        for f in job.futures:
            if not f.done():
                f.set_exception(exc)

    async def _work(self) -> None:
        while True:
            _prio, _n, key = await self._queue.get()
            job = self._pending.pop(key, None)
            if job is None:      # 우선순위가 올라가 이미 처리된 항목
                continue
            self._running = job
            self._publish_status(job.sid)
            try:
                result = await self._run(job)
            except asyncio.CancelledError:
                self._fail(job, RuntimeError("AI 분석 서비스가 종료되었습니다"))
                raise
            except Exception as e:
                log.error("AI 분석 작업 실패(%s)", exception_label(e))
                self.last_error = f"{job.label}: {e}"
                self._fail(job, e)
            else:
                for f in job.futures:
                    if not f.done():
                        f.set_result(result)
            finally:
                self._running = None
                self._publish_status(job.sid)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(TICK_SEC)
            try:
                self._check_rolling()
                await self._maybe_unload()
            except Exception as e:  # pragma: no cover - 방어
                log.error("AI 분석 주기 작업 실패(%s)", exception_label(e))

    def _check_rolling(self) -> None:
        interval = int(self._llm_cfg().get("rolling_interval_sec") or 0)
        if interval <= 0:
            return
        now = time.monotonic()
        for sid, st in list(self._rolling.items()):
            if not st.get("dirty") or now - st.get("last_run", now) < interval:
                continue
            s = self.rt.repo.get_session(sid)
            if s is None:
                self._rolling.pop(sid, None)
                continue
            if s["status"] != "live":
                continue
            st["dirty"] = False
            st["last_run"] = now
            self._enqueue(Job("session", sid, scope="rolling", priority=PRIO_SESSION))

    async def _maybe_unload(self) -> None:
        p = self._provider
        if (isinstance(p, LocalMLXProvider) and p.running and self._running is None and not self._pending
                and not self._provider_activities
                and p.last_used and time.monotonic() - p.last_used > IDLE_UNLOAD_SEC):
            log.info("로컬 LLM 유휴 %d초 → 프로세스 종료", IDLE_UNLOAD_SEC)
            await p.close()

    async def wait_idle(self, timeout: float = 60.0) -> bool:
        """대기·진행 중인 작업이 없어질 때까지 기다린다(테스트·종료 전 정리용)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self._timers and not self._pending and self._running is None and not self._provider_activities:
                return True
            await asyncio.sleep(0.05)
        return False

    # ── 상태 ────────────────────────────────────────────────
    def status(self) -> dict:
        p = self._provider
        if p is not None:
            name, model = p.name, p.model
        else:
            cfg = self._llm_cfg()
            name = cfg.get("provider") or "rules"
            model = {"local_mlx": cfg.get("local_model"), "openai_compat": cfg.get("openai_model"),
                     "anthropic": cfg.get("anthropic_model"), "desktop_chatgpt": cfg.get("desktop_model") or "계정 기본 모델"}.get(name) or "rules"
            if name == "local_mlx" and self._heavy_off():
                name, model = "rules", "rules"
        activity = next(iter(self._provider_activities.values()), None)
        return {
            "queue": len(self._pending) + len(self._timers),
            "running": activity["label"] if activity else self._running.label if self._running else None,
            "running_session": activity["session_id"] if activity else self._running.sid if self._running else None,
            "provider": name,
            "model": model or ("계정 기본 모델" if name == "desktop_chatgpt" else ""),
            "loaded": bool(p.running) if isinstance(p, LocalMLXProvider) else p is not None,
            "last_provider": self.last_provider,
            "last_latency_ms": self.last_latency_ms,
            "last_error": public_error(self.last_error),
            "last_activity": dict(self.last_activity) if self.last_activity else None,
        }

    def health(self) -> dict:
        return {"ok": True, **self.status()}

    def _publish_status(self, sid: str | None) -> None:
        if sid:
            self.rt.bus.publish(sid, "analysis.status", self.status())

    async def complete_for_task(self, sid: str, label: str, provider: LLMProvider,
                                system: str, user: str, **kwargs) -> str:
        """요약 큐 밖의 보정 모델 호출도 시작·종료 상태로 알린다. 본문은 남기지 않는다."""
        token = object()
        activity = {"label": label, "session_id": sid, "provider": provider.name,
                    "model": provider.model, "started_at": now_ms()}
        self._provider_activities[token] = activity
        self._publish_status(sid)
        started = time.perf_counter()
        state = "completed"
        error = None
        try:
            return await provider.complete(system, user, **kwargs)
        except asyncio.CancelledError:
            state = "cancelled"
            raise
        except Exception as exc:
            state, error = "failed", exception_label(exc)
            raise
        finally:
            duration = int((time.perf_counter() - started) * 1000)
            self._provider_activities.pop(token, None)
            self.last_activity = {**activity, "finished_at": now_ms(), "status": state,
                                  "latency_ms": duration, **({"error_code": error} if error else {})}
            self._record(provider.name, duration, f"{label}: {error}" if error else None)
            self._publish_status(sid)

    # ── 공개 API(수동 요청) ──────────────────────────────────
    def _uses_stage_summary(self, sid: str) -> bool:
        stages = getattr(self.rt, "stages", None)
        session = self.rt.repo.get_session(sid)
        return bool(stages is not None and session and session.get("source_type") in {"assembly_caption", "file"}
                    and stages.owns(sid))

    def stage_summary_result(self, sid: str, block_id: int) -> dict | None:
        """새 자막 회의의 요약 요청은 조회만 하며 별도 보정·요약을 예약하지 않는다."""
        return self.rt.repo.stage_block_summary_result(sid, block_id) if self._uses_stage_summary(sid) else None

    async def read_stage_summary_result(self, sid: str, block_id: int) -> dict | None:
        """현재성 검사는 수집 DB 연결과 분리된 읽기 실행기에서 수행한다."""
        stages = getattr(self.rt, "stages", None)
        if stages is None:
            return None
        read_call = getattr(stages, "read_call", None)
        if not callable(read_call):
            # 독립 읽기 실행기가 없는 작은 기존 모의 객체의 계약만 유지한다.
            return await asyncio.to_thread(self.stage_summary_result, sid, block_id)
        def read(view):
            session = view.rt.repo.get_session(sid)
            if not session or session.get("source_type") not in {"assembly_caption", "file"} or not view.owns(sid):
                return None
            return view.rt.repo.stage_block_summary_result(sid, block_id)
        return await read_call(read)

    async def summarize_block(self, sid: str, block_id: int) -> dict:
        """국회 자막은 같은 판본의 저장 요약을 읽고 그 밖의 입력만 수동 요약한다."""
        saved = await self.read_stage_summary_result(sid, block_id)
        if saved is not None:
            return saved.get("summary") or {}
        return await self._submit(Job("block", sid, block_id=block_id, priority=PRIO_MANUAL, manual=True))

    async def summarize_session(self, sid: str, scope: str = "session") -> dict:
        """회의 요약(수동, 최우선). SessionSummary 를 돌려준다(summaries 저장 + 'summary.update')."""
        scope = scope if scope in ("session", "rolling") else "session"
        return await self._submit(Job("session", sid, scope=scope, priority=PRIO_MANUAL, manual=True))

    async def test_provider(self, llm_cfg: dict) -> dict:
        """설정값으로 짧은 예문을 요약해 본다. 규칙 대체 없이 실제 결과·오류를 보고한다."""
        current = self._llm_cfg()
        cfg = {**current, **(llm_cfg or {})}
        for k in ("openai_api_key", "anthropic_api_key"):
            v = str(cfg.get(k) or "")
            if not v or v.startswith("••"):
                cfg[k] = current.get(k, "")
        sig = provider_signature(cfg) + ("|light" if self._heavy_off() else "")
        reuse = self._provider is not None and sig == self._provider_sig
        provider = self._provider if reuse else make_provider(cfg, self.rt)
        assert provider is not None
        out: dict[str, Any] = {"provider": provider.name, "model": provider.model}
        if isinstance(provider, RulesProvider) and provider.note:
            out["note"] = provider.note
        t0 = time.perf_counter()
        try:
            ctx = prompts.PromptContext.from_settings(settings.get(self.rt.repo, "org") or {},
                                                      settings.get(self.rt.repo, "watch_keywords") or [], None)
            system, user = prompts.block_messages(ctx, _SAMPLE_BLOCK, _SAMPLE_UTTS)
            text = await asyncio.wait_for(
                provider.complete(system, user, max_tokens=int(cfg.get("max_tokens") or 1200), json_mode=True),
                timeout=900)
            data = prompts.normalize_block_summary(parse_json(text), {1, 2})
            out.update(ok=True, latency_ms=int((time.perf_counter() - t0) * 1000), sample=data["one_line"],
                       result=data)
        except Exception as e:
            out.update(ok=False, latency_ms=int((time.perf_counter() - t0) * 1000),
                       error=failure_reason(e))
        finally:
            if not reuse:
                try:
                    await provider.close()
                except Exception:  # pragma: no cover - 방어
                    pass
        return out

    # ── 실행 ────────────────────────────────────────────────
    async def _run(self, job: Job) -> dict:
        if job.kind == "block":
            return await self._run_block(job)
        return await self._run_session(job)

    def _context(self, sid: str, session: dict | None) -> tuple[prompts.PromptContext, list, list]:
        repo = self.rt.repo
        ctx = prompts.PromptContext.from_settings(settings.get(repo, "org") or {},
                                                  settings.get(repo, "watch_keywords") or [], session)
        return ctx, settings.watch_terms(repo), settings.get(repo, "departments") or []

    async def _ask(self, provider: LLMProvider, system: str, user: str, max_tokens: int,
                   normalize: Callable[[Any], dict], *, safe_errors: bool = False) -> tuple[dict, int]:
        """LLM 호출 → JSON 파싱 → 정규화. 형식 오류면 'JSON만 출력' 재요청 1회."""
        t0 = time.perf_counter()
        last: Exception | None = None
        for attempt in range(2):
            text = await provider.complete(system, user if attempt == 0 else user + prompts.JSON_RETRY_SUFFIX,
                                           max_tokens=max_tokens, json_mode=True)
            try:
                return normalize(parse_json(text)), int((time.perf_counter() - t0) * 1000)
            except (LLMParseError, ValueError) as e:
                last = e
                log.warning("LLM 응답 형식 오류(%d회차, %s)", attempt + 1,
                            "session_utterance_summary_invalid_response" if safe_errors else exception_label(e))
        raise LLMParseError(f"JSON 형식 오류: {last}")

    def _record(self, provider: str, ms: int | None, err: str | None) -> None:
        self.last_provider = provider
        self.last_latency_ms = ms
        if err:
            self.last_error = err

    @staticmethod
    def _protected(block: dict) -> str:
        """자동 요약이 덮어쓰면 안 되는 블록이면 그 이유(검수 완료·사람이 고친 요약), 아니면 ''."""
        if block.get("review_status") == "reviewed":
            return "검수 완료"
        sm = block.get("summary")
        if isinstance(sm, dict) and sm.get("edited_by"):
            return "사람이 고친 요약"
        return ""

    async def _run_block(self, job: Job) -> dict:
        repo = self.rt.repo
        saved = await self.read_stage_summary_result(job.sid, int(job.block_id or 0))
        if saved is not None:
            return saved.get("summary") or {}  # 이전 큐에 남아 있던 일반 요약도 새 경로를 중복 실행하지 않는다.
        block = repo.get_block(int(job.block_id or 0))
        if not block or block["session_id"] != job.sid:
            raise KeyError(f"블록을 찾을 수 없습니다: {job.block_id}")
        if not job.manual and self._question_audio_holds(job.sid, block["id"]):
            return block.get("summary") or {}  # 이미 예약된 일반 요약도 음성 초안·명시적 보류를 앞서지 않는다.
        if not job.manual and (self._protected(block) or block["kind"] != "question"):
            return block.get("summary") or {}   # 검수 완료·사람이 고친·비질의 블록은 자동으로 덮어쓰지 않는다
        from ..turn_processing import prepare_integrated_review
        prepared = await prepare_integrated_review(self, job, block)
        if prepared is not None:
            return prepared
        if not job.manual:
            from ..turn_processing import latest_block_review, requires_question_audio
            if requires_question_audio(latest_block_review(repo, job.sid, block["id"])):
                return block.get("summary") or {}  # 취소·설정 변경 시 음성 근거 없이 전체 요약하지 않는다.
        session = repo.get_session(job.sid)
        utts = [u for u in repo.review_utterances(job.sid, block["id"])
                if u.get("speaker_role") != "system" and (u.get("text") or "").strip()]
        input_signature = _review_input_signature(utts)
        ctx, terms, departments = self._context(job.sid, session)
        provider, cfg = await self._get_provider()
        summary: dict | None = None
        reason = ""
        if utts and not isinstance(provider, RulesProvider):
            system, user = prompts.block_messages(ctx, block, utts,
                                                  reviewed=any(u.get("speaker_source") == "reviewed" for u in utts))
            valid = {u["id"] for u in utts}
            try:
                summary, ms = await self._ask(provider, system, user, int(cfg.get("max_tokens") or 1200),
                                              lambda d: prompts.normalize_block_summary(d, valid))
                ground_org_relevance(summary, utts, terms)
                ground_ref_uids(summary, utts)     # 근거 발언을 빠뜨린 항목은 문구가 겹치는 발언으로 보정
                merge_rule_refs(summary, rules_block_summary(block, utts, terms))
                summary.update(provider=provider.name, model=provider.model, generated_at=now_ms(), latency_ms=ms)
                self._record(provider.name, ms, None)
            except Exception as e:   # 어떤 실패든 규칙 대체
                reason = failure_reason(e)
                log.warning("블록 요약 실패 → 규칙 대체(%s)", exception_label(e))
                self._record(FALLBACK_LABEL, None, reason)
        if summary is None:
            label = "rules" if isinstance(provider, RulesProvider) else FALLBACK_LABEL
            summary = rules_block_summary(block, utts, terms, provider=label)
            if reason:
                summary["fallback_reason"] = reason[:300]
        # 모델 대기 사이 새 자막 경로가 소유했다면 구형 요약을 저장하지 않는다.
        saved = await self.read_stage_summary_result(job.sid, int(job.block_id or 0))
        if saved is not None:
            return saved.get("summary") or {}  # 실행 사이 새 자막 경로가 소유한 질의에는 구형 결과를 저장하지 않는다.
        # 이하 사람 판본 재검사부터 저장까지는 await 없이 검수·수정 요청과 섞이지 않는다.
        cur = repo.get_block(block["id"])
        if cur is None or cur["session_id"] != job.sid:
            raise KeyError(f"블록을 찾을 수 없습니다: {job.block_id}")
        if not job.manual and self._question_audio_holds(job.sid, block["id"], completing=True):
            return cur.get("summary") or {}  # 선행 모델 대기 중 음성 대조를 보류·취소한 경우도 보존한다.
        current_utts = [u for u in repo.review_utterances(job.sid, block["id"])
                        if u.get("speaker_role") != "system" and (u.get("text") or "").strip()]
        if _review_input_signature(current_utts) != input_signature:
            self.last_error = "요약 중 원문·화자 판본이 바뀌어 이전 결과를 저장하지 않았습니다"
            if not job.manual:
                self.on_review_accepted(job.sid, block["id"])
            return cur.get("summary") or {}
        if job.manual:
            cur_sm = cur.get("summary") if isinstance(cur.get("summary"), dict) else {}
            if cur_sm.get("edited_by") and int(cur_sm.get("edited_at") or 0) >= job.requested_ms:
                log.info("블록 #%s: 다시 요약 요청 뒤 사람이 고친 요약이 있어 결과를 저장하지 않음", block["id"])
                return cur_sm
            if cur.get("review_status") != block.get("review_status"):
                log.info("블록 #%s: 요약 중 검수 상태가 바뀜(%s→%s) — 수동 재요약 결과로 초안 처리",
                         block["id"], block.get("review_status"), cur.get("review_status"))
        else:
            why = self._protected(cur) or ("" if cur["kind"] == "question" else "질의 블록 아님")
            if why:
                log.info("블록 #%s: 자동 요약 중 %s → 결과를 저장하지 않음", block["id"], why)
                return cur.get("summary") or {}
            block = cur
        fields = {"summary": summary, "summary_at": now_ms(), "review_status": "draft",
                  "reviewed_by": "", "reviewed_at": None}
        previous_preview = None
        if job.manual and settings.get(repo, "transcript_review").get("enabled"):
            from ..turn_processing import latest_block_review
            candidate = latest_block_review(repo, job.sid, block["id"])
            if candidate and candidate["status"] == "draft" \
                    and ((candidate.get("payload") or {}).get("presentation") or {}).get("ready"):
                previous_preview = candidate
        if previous_preview:
            # 원문 기준 수동 재요약 뒤에 이전 표시 초안이 다시 나타나지 않게 한다.
            with repo.db.transaction():
                repo.set_transcript_review_status(previous_preview["id"], "stale")
                b2 = repo.update_block(block["id"], **fields)
            reviews = getattr(self.rt, "reviews", None)
            if reviews is not None:
                reviews._publish_review(job.sid, repo.get_transcript_review(previous_preview["id"]))
        else:
            b2 = repo.update_block(block["id"], **fields)
        if not str(summary.get("provider", "")).startswith("rules"):
            self._sync_llm_items(b2 or block, summary, utts, departments)
        if b2:
            self.rt.bus.publish(job.sid, "block.upsert", serialize.block(b2))
        self.on_review_display_changed(job.sid, block["id"])
        return summary

    @staticmethod
    def _dupe_entries(entries: list[dict], pool: list[dict]) -> set[int]:
        """LLM 항목(entries) 중 이미 있는 항목(pool: 규칙·수동·사람이 손댄 항목)과 같은 것의 위치.

        1) 같은 발언·종류: 문구가 조금이라도 겹치는 쌍부터 1:1 로 짝짓는다(한 발언 안의 요구 여러 건은 각각 남김).
        2) 그 밖(근거 발언 없음·다른 발언·짝이 이미 찬 경우): 문구가 크게 겹치면 같은 항목의 재진술로 본다.
        """
        def ref(it: dict) -> str:
            return f"{it.get('text') or ''} {it.get('quote') or ''}"

        skip: set[int] = set()
        used: set[int] = set()
        pairs = []
        for idx, r in enumerate(entries):
            uid = r.get("utterance_id")
            if uid is None:
                continue
            for it in pool:
                if it.get("utterance_id") == uid:
                    sc = text_overlap(r.get("text") or "", ref(it))
                    if sc >= SIM_SAME_UTTERANCE:
                        pairs.append((sc, idx, it["id"]))
        for _sc, idx, iid in sorted(pairs, key=lambda x: (-x[0], x[1])):
            if idx in skip or iid in used:
                continue
            skip.add(idx)
            used.add(iid)
        for idx, r in enumerate(entries):
            if idx not in skip and any(text_overlap(r.get("text") or "", ref(it)) >= SIM_ANY_UTTERANCE for it in pool):
                skip.add(idx)
        return skip

    def _sync_llm_items(self, block: dict, summary: dict, utts: list[dict], departments: list[dict]) -> None:
        """LLM 추출 항목을 후속조치 항목에 반영한다.

        - 교체 대상은 사람이 손대지 않은(edited=0, 미처리, 메모 없음) llm 항목뿐이다. 다시 나온 같은 항목은 id 를
          그대로 두고, 나오지 않은 항목은 'superseded' 로 숨긴다(삭제하지 않음 → id 재사용 없음).
        - 규칙·수동·사람이 손댄 항목과 같은 내용이면 건너뛴다(_dupe_entries). 한 발언에서 여러 건이 나와도 모두 남긴다.
        """
        repo, bus = self.rt.repo, self.rt.bus
        sid, bid = block["session_id"], block["id"]
        existing = repo.list_items(sid, block_id=bid, include_dismissed=True)
        replaceable = [i for i in existing if i["origin"] == "llm" and i["status"] == "open"
                       and not i.get("edited") and not (i.get("note") or "")]
        by_fp = {i["fingerprint"]: i for i in replaceable if i.get("fingerprint")}
        repl_ids = {i["id"] for i in replaceable}
        pool = [i for i in existing if i["id"] not in repl_ids]
        by_id = {u["id"]: repo.get_utterance(u["id"]) or u for u in utts}
        by_parts: dict[int, list[dict]] = {}
        for part in utts:
            by_parts.setdefault(part["id"], []).append(part)
        requester = (block.get("questioner") or "") if block["kind"] == "question" else ""
        block_day = datetime.fromtimestamp(block["ts_start"] / 1000, KST).date() if block.get("ts_start") else None
        kept: set[int] = set()
        changed: list[int] = []
        for key, kind in (("data_requests", "data_request"), ("commitments", "commitment"), ("issues", "issue")):
            entries = [r for r in summary.get(key) or [] if r.get("origin") != "rule" and (r.get("text") or "").strip()]
            dupes = self._dupe_entries(entries, [i for i in pool if i["kind"] == kind])
            for idx, r in enumerate(entries):
                if idx in dupes:
                    continue
                uid = r.get("utterance_id")
                u = by_id.get(uid) if uid is not None else None
                text = r["text"].strip()
                if u and uid in by_parts:
                    best = max(by_parts[uid], key=lambda p: text_overlap(text, p.get("text") or ""))
                    if text_overlap(text, best.get("text") or "") > 0:
                        u = {**u, "speaker": best.get("speaker") or "", "speaker_role": best.get("speaker_role")}
                src = _source_sentence(text, u["text"]) if u else ""
                base = datetime.fromtimestamp(u["ts_start"] / 1000, KST).date() if u else block_day
                due = ""
                if kind != "issue":
                    due = rules.due_hint(text, base) or (rules.due_hint(src, base) if src else "")
                dept = (rules.assign_department(text, departments) or rules.assign_department(src, departments)
                        or (rules.assign_department(u["text"], departments) if u else ""))
                strength = r.get("strength") or ""
                prev = by_fp.get(fingerprint(kind, str(uid or ""), text))
                if prev is not None and prev["id"] not in kept:
                    # 같은 항목이 다시 나옴: 그대로 두고(id 유지) 자동으로 정해지는 값만 맞춘다
                    kept.add(prev["id"])
                    upd = {k: v for k, v in (("requester", requester), ("assignee_dept", dept), ("due_date", due),
                                             ("strength", strength), ("speaker", (u or {}).get("speaker") or ""))
                           if (prev.get(k) or "") != v}
                    if upd:
                        repo.update_item(prev["id"], **upd)
                        changed.append(prev["id"])
                    continue
                row, new = repo.add_item(sid, kind=kind, text=text, speaker=(u or {}).get("speaker") or "",
                                         requester=requester, block_id=bid, utterance_id=uid, origin="llm",
                                         confidence=0.7, assignee_dept=dept, strength=strength, due_date=due)
                if new:
                    changed.append(row["id"])
        gone = [i for i in replaceable if i["id"] not in kept]
        if gone:
            repo.supersede_items([i["id"] for i in gone])
            for uid in sorted({i["utterance_id"] for i in gone}, key=lambda x: x or 0):
                bus.publish(sid, "items.reload", {"utterance_id": uid, "block_id": bid})
        for iid in changed:
            row = repo.get_item_full(iid)
            if row is not None and row.get("status") != "superseded":
                bus.publish(sid, "item.upsert", serialize.item(row))

    # ── 사람이 고친 블록 요약(검수) ─────────────────────────────
    def edit_block_summary(self, sid: str, bid: int, patch: dict, actor: str) -> dict:
        """블록 요약을 사람이 고친다(계약 C5). 허용 키만 병합하고 edited_by·edited_at 을 남긴다.

        고친 요약은 자동 재요약이 덮어쓰지 않는다(수동 재요약만 덮어씀). 반환은 serialize.block.
        블록이 없으면 KeyError, 허용되지 않는 키·값이면 ValueError.
        """
        repo = self.rt.repo
        block = repo.get_block(int(bid))
        if not block or block["session_id"] != sid:
            raise KeyError(f"블록을 찾을 수 없습니다: {bid}")
        utts = [u for u in repo.list_utterances(sid, block_id=block["id"], final_only=True)
                if u.get("speaker_role") != "system" and (u.get("text") or "").strip()]
        cur = block.get("summary") if isinstance(block.get("summary"), dict) and block["summary"].get("one_line") \
            else None
        if cur is None:   # 요약이 없던 블록이면 규칙 요약을 바탕으로 고친다
            cur = rules_block_summary(block, utts, settings.watch_terms(repo))
        fields = prompts.normalize_summary_patch(patch, {u["id"] for u in utts}, cur)
        t = now_ms()
        new = {**cur, **fields, "edited_by": (actor or "").strip() or "operator", "edited_at": t}
        b2 = repo.update_block(block["id"], summary=new, summary_at=block.get("summary_at") or t)
        assert b2 is not None
        repo.audit(new["edited_by"], "block.summary_edit", str(block["id"]), {"fields": sorted(fields)})
        out = serialize.block(b2)
        self.rt.bus.publish(sid, "block.upsert", out)
        return out

    async def _run_session(self, job: Job) -> dict:
        repo = self.rt.repo
        session = repo.get_session(job.sid)
        if session is None:
            raise KeyError(f"세션을 찾을 수 없습니다: {job.sid}")
        reviews = getattr(self.rt, "reviews", None)
        pending = getattr(reviews, "has_pending_closed", None)
        block_pending = any(j.sid == job.sid and j.kind == "block" for j in self._pending.values()) or any(
            j.sid == job.sid and j.kind == "block" for _timer, j in self._timers.values())
        if not job.manual and settings.get(repo, "transcript_review").get("enabled") \
                and ((pending is not None and pending(job.sid)) or block_pending):
            # 마지막 질의의 통합 초안보다 먼저 낡은 회의 요약을 확정하지 않는다.
            self._schedule(Job("session", job.sid, scope=job.scope, priority=job.priority), SESSION_DEBOUNCE_SEC)
            return repo.latest_summary(job.sid, job.scope) or {}
        source_signature = self._session_review_signature(job.sid)
        ctx, terms, _departments = self._context(job.sid, session)
        blocks = repo.list_blocks(job.sid)
        pairs: list[tuple[dict, dict]] = []
        for b in blocks:
            if b["kind"] != "question":
                continue
            sm = b.get("summary") if isinstance(b.get("summary"), dict) and b["summary"].get("one_line") else None
            if settings.get(repo, "transcript_review").get("enabled"):
                from ..review_display import get_block_display
                display = get_block_display(repo, b)
                ai = display.get("ai_review") or {}
                sm = ai.get("summary") or display.get("summary")
                owner = ai.get("ownership") or display.get("question_ownership")
                if owner:
                    b = {**b, "questioner": owner.get("questioner") if owner.get("status") in {"draft", "accepted"} else "질의자 확인 필요",
                         "questioner_party": ""}
                elif display.get("questioner") != b.get("questioner"):
                    b = {**b, "questioner": display.get("questioner") or "", "questioner_party": ""}
                if sm is None and (b.get("summary") or {}).get("question_ownership"):
                    # 낡은 채택 범위를 일반 원문 전체 요약으로 되살리지 않는다.
                    sm = {"one_line": "질의 범위 근거가 바뀌어 요약 확인이 필요합니다", "org_relevance": "none"}
            if sm is None:
                sm = rules_block_summary(b, repo.list_utterances(job.sid, block_id=b["id"], final_only=True), terms)
            pairs.append((b, sm))
        stats = repo.session_stats(job.sid)
        if job.manual and job.scope == "session" and not pairs:
            return await self._run_unclassified_session(job, session, ctx)
        covered = int(stats.get("ts_last") or 0)
        recent: list[dict] | None = None
        if job.scope == "rolling":
            ob = next((b for b in reversed(blocks) if b.get("is_open")), None)
            if ob is not None:
                us = [u for u in repo.list_utterances(job.sid, block_id=ob["id"], final_only=True)
                      if u.get("speaker_role") != "system"]
                recent = us[-RECENT_UTTERANCES:] or None
        items = repo.list_items(job.sid)
        provider, cfg = await self._get_provider()
        content: dict | None = None
        reason = ""
        if (pairs or recent) and not isinstance(provider, RulesProvider):
            digests = self._digests(pairs)
            names = list(dict.fromkeys(b["questioner"] for b, _ in pairs if b.get("questioner")))
            status_label = STATUS_LABELS.get(session["status"], session["status"])
            progress = (f"{serialize.hms(stats.get('ts_first'))[:5]}~{serialize.hms(stats.get('ts_last'))[:5]}, "
                        f"위원 질의 {len(pairs)}건({', '.join(names) or '성명 미확인'}), 상태: {status_label}")
            system, user = prompts.session_messages(ctx, job.scope, digests, recent, progress=progress)
            valid_b = {b["id"] for b, _ in pairs}
            valid_u = {r["id"] for r in repo.db.query("SELECT id FROM utterances WHERE session_id=?", (job.sid,))}
            try:
                content, ms = await self._ask(provider, system, user, max(int(cfg.get("max_tokens") or 1200), 1500),
                                              lambda d: prompts.normalize_session_summary(d, valid_b, valid_u))
                self._ground_session(content, pairs, items)
                content.update(provider=provider.name, model=provider.model, generated_at=now_ms(),
                               covered_until_ms=covered, latency_ms=ms)
                self._record(provider.name, ms, None)
            except Exception as e:
                reason = failure_reason(e)
                log.warning("회의 요약 실패 → 규칙 대체(%s)", exception_label(e))
                self._record(FALLBACK_LABEL, None, reason)
        if content is None:
            label = "rules" if isinstance(provider, RulesProvider) else FALLBACK_LABEL
            content = rules_session_summary(session, pairs, items, scope=job.scope, covered_until_ms=covered,
                                            ts_first=stats.get("ts_first"), provider=label)
            if reason:
                content["fallback_reason"] = reason[:300]
            self._ground_session(content, pairs, items)
        if source_signature is not None and source_signature != self._session_review_signature(job.sid):
            self.last_error = "회의 요약 중 정리 초안·원문·설정이 바뀌어 이전 결과를 저장하지 않았습니다"
            if not job.manual:
                self._schedule(Job("session", job.sid, scope=job.scope, priority=job.priority), SESSION_DEBOUNCE_SEC)
            return repo.latest_summary(job.sid, job.scope) or {}
        row = repo.add_summary(job.sid, job.scope, content, provider=content["provider"], model=content["model"])
        self.rt.bus.publish(job.sid, "summary.update", {"scope": job.scope, "summary": serialize.summary(row)})
        return content

    @staticmethod
    def _session_source_signature(rows: list[dict]) -> str:
        """선택한 입력의 원문·수정 본문·화자만 비교하며 새 행 추가는 서명에 넣지 않는다."""
        fields = ("id", "session_id", "seq", "ts_start", "ts_end", "text", "original_text", "speaker",
                  "speaker_role", "speaker_source", "is_final")
        values = [[row.get(key) for key in fields] for row in rows]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _session_prompt_context_signature(self, sid: str, ctx: prompts.PromptContext | None = None) -> str | None:
        """질의 미분류 입력의 공개 회의·기관·제공자 맥락. 비밀 필드는 서명에 넣지 않는다."""
        if ctx is None:
            session = self.rt.repo.get_session(sid)
            if session is None:
                return None
            ctx = prompts.PromptContext.from_settings(settings.get(self.rt.repo, "org") or {},
                settings.get(self.rt.repo, "watch_keywords") or [], session)
        cfg = self._llm_cfg()
        public = {key: cfg.get(key) for key in ("provider", "desktop_model", "local_model", "openai_base_url",
                                               "openai_model", "anthropic_model", "max_tokens")}
        return hashlib.sha256(json.dumps([vars(ctx), public, self._heavy_off()], ensure_ascii=False,
                                         sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _final_session_count(repo, sid: str) -> int:
        row = repo.db.query_one("SELECT COUNT(*) AS n FROM utterances WHERE session_id=? AND is_final=1 "
                                "AND COALESCE(speaker_role,'')!='system' AND trim(text,?)!=''",
                                (sid, " \t\r\n\v\f"))
        return int(row["n"])

    @staticmethod
    def _session_coverage_note(coverage: dict) -> str:
        count, total = coverage["selected_utterance_count"], coverage["eligible_utterance_count"]
        start, end = coverage["selected_start_ms"], coverage["selected_end_ms"]
        span = f"{serialize.hms(start)}~{serialize.hms(end)}" if count else "선택 발언 없음"
        selection = "최근 일부 선택" if coverage["partial"] else "저장된 해당 확정 발언 전부 포함"
        cut = " · 긴 발언 본문 일부 축약" if coverage["text_truncated"] else ""
        appended = " · 선택 이후 추가된 발언은 미포함" if coverage.get("appended_after_selection_count") else ""
        return (f"저장 확정 비시스템 발언 {count}/{total}개, {span}(선택 발언 기준), {selection}{cut}{appended}. "
                "질의 블록 미분류로 실제 질의 유무·위원별 경계·기관 관련 범위는 미확정이며 전체 감사 수록 여부도 미검증.")

    def _unclassified_input(self, sid: str) -> tuple[list[dict], list[str], dict]:
        repo = self.rt.repo
        total = self._final_session_count(repo, sid)
        # 최근 행만 읽으며 초기 5000행 LIMIT에 가려진 오래된 회의의 끝부분도 선택한다.
        rows = repo.db.query("SELECT id,session_id,seq,ts_start,ts_end,text,original_text,speaker,speaker_role,"
                             "speaker_source,is_final FROM utterances WHERE session_id=? AND is_final=1 "
                             "AND COALESCE(speaker_role,'')!='system' AND trim(text,?)!='' "
                             "ORDER BY ts_start DESC,COALESCE(NULLIF(source_order,''),printf('%012d',seq)) DESC,seq DESC LIMIT ?",
                             (sid, " \t\r\n\v\f", SESSION_UTTERANCE_LIMIT))
        chosen, lines, size, truncated = [], [], 0, False
        for row in rows:
            label = re.sub(r"\s+", " ", prompts.speaker_label(row))
            if len(label) > 160:
                label = label[:159] + "…"
                truncated = True
            prefix = f"[u{row['id']} {serialize.hms(row['ts_start'])} {label}] "
            text = re.sub(r"\s+", " ", row.get("text") or "").strip()
            line = prefix + text
            if size + len(line) + bool(lines) > SESSION_DIGEST_CHARS:
                if lines:
                    break  # 이미 고른 최근 발언은 온전히 유지하고 더 오래된 행은 제외한다.
                marker = " …(입력 한도로 중간 생략)… "
                room = max(0, SESSION_DIGEST_CHARS - len(prefix) - len(marker))
                head, tail = (room + 1) // 2, room // 2
                line = prefix + text[:head] + marker + (text[-tail:] if tail else "")
                truncated = True
            size += len(line) + bool(lines)
            chosen.append(row)
            lines.append(line)
        chosen.reverse()
        lines.reverse()
        partial = len(chosen) < total or truncated
        coverage = {"source": "final_utterances", "selection": "recent_bounded",
                    "question_boundary_status": "unclassified", "question_presence_unverified": True,
                    "meeting_coverage_verified": False, "eligible_utterance_count": total,
                    "eligible_utterance_count_at_request": total, "selected_utterance_count": len(chosen),
                    "selected_utterance_ids": [row["id"] for row in chosen],
                    "selected_start_ms": chosen[0]["ts_start"] if chosen else None,
                    "selected_end_ms": max(row.get("ts_end") or row["ts_start"] for row in chosen) if chosen else None,
                    "omitted_utterance_count": total - len(chosen), "partial": partial, "text_truncated": truncated,
                    "stored_utterance_coverage_complete": not partial, "input_chars": len("\n".join(lines)),
                    "max_input_chars": SESSION_DIGEST_CHARS, "max_utterances": SESSION_UTTERANCE_LIMIT,
                    "selection_as_of_ms": now_ms(), "appended_after_selection_count": 0}
        return chosen, lines, coverage

    async def _run_unclassified_session(self, job: Job, session: dict, ctx: prompts.PromptContext) -> dict:
        """질의 경계 없는 수동 요청도 실제 발언을 보낸다. 기존 블록 요약 경로는 건드리지 않는다."""
        repo = self.rt.repo
        rows, lines, coverage = self._unclassified_input(job.sid)
        source_signature = self._session_source_signature(rows)
        context_signature = self._session_prompt_context_signature(job.sid, ctx)
        previous = repo.latest_summary(job.sid, job.scope)
        provider, cfg = await self._get_provider()
        content, reason = None, ""
        if lines and not isinstance(provider, RulesProvider):
            system, user = prompts.session_utterance_messages(ctx, lines, self._session_coverage_note(coverage))
            try:
                content, ms = await self._ask(provider, system, user, max(int(cfg.get("max_tokens") or 1200), 1500),
                    lambda data: prompts.normalize_session_summary(data, set(), {row["id"] for row in rows}), safe_errors=True)
                content.update(provider=provider.name, model=provider.model, generated_at=now_ms(), latency_ms=ms)
                self._record(provider.name, ms, None)
            except Exception:
                reason = "session_utterance_summary_request_failed"
                log.warning("회의 발언 요약 실패 → 규칙 대체: %s", reason)
                self._record(FALLBACK_LABEL, None, reason)
        if content is None:
            label = "rules" if isinstance(provider, RulesProvider) else FALLBACK_LABEL
            reason = reason or ("no_final_utterances" if not lines else "rules_provider_selected")
            content = {"overview": "질의 경계가 분류되지 않아 실제 질의 유무와 기관 관련 범위는 미확정임. "
                                   "선택 발언의 AI 요약을 완료하지 못해 범위 안내만 표시함.",
                       "issues": [], "hashtags": [], "org_points": [], "next_actions": [],
                       "provider": label, "model": "rules", "generated_at": now_ms(), "fallback_reason": reason}
        with repo.db.transaction():
            current = [repo.get_utterance(row["id"]) for row in rows]
            latest = repo.latest_summary(job.sid, job.scope)
            if (any(row is None for row in current) or self._session_source_signature(current) != source_signature
                    or latest != previous
                    or self._session_prompt_context_signature(job.sid) != context_signature):
                self.last_error = "회의 요약 중 선택한 발언·기존 요약·기관 또는 제공자 맥락이 바뀌어 이전 결과를 저장하지 않았습니다"
                return latest or {}
            # 새 발언 추가는 허용하되 결과가 현재 저장 발언 전부를 다뤘다고 표시하지 않는다.
            current_total = self._final_session_count(repo, job.sid)
            coverage.update(eligible_utterance_count=current_total,
                            omitted_utterance_count=current_total - len(rows),
                            appended_after_selection_count=max(0, current_total - coverage["eligible_utterance_count_at_request"]))
            coverage["partial"] = len(rows) < current_total or coverage["text_truncated"]
            coverage["stored_utterance_coverage_complete"] = not coverage["partial"]
            content.update(coverage=coverage, coverage_note=self._session_coverage_note(coverage),
                           covered_until_ms=coverage["selected_end_ms"] or 0)
            row = repo.add_summary(job.sid, job.scope, content, provider=content["provider"], model=content["model"])
        self.rt.bus.publish(job.sid, "summary.update", {"scope": job.scope, "summary": serialize.summary(row)})
        return content

    def _session_review_signature(self, sid: str) -> str | None:
        """열린 발언의 정상 진행은 막지 않고 닫힌 질의의 판본 변경을 감지한다."""
        if not settings.get(self.rt.repo, "transcript_review").get("enabled"):
            return None
        from ..review_context import processing_signature
        from ..review_display import get_block_display
        from ..turn_processing import latest_block_review
        repo = self.rt.repo
        values = []
        for block in repo.list_blocks(sid):
            if block.get("is_open") or block.get("kind") != "question":
                continue
            review = latest_block_review(repo, sid, block["id"])
            rows = repo.list_utterances(sid, block_id=block["id"], final_only=True)
            values.append((get_block_display(repo, block),
                (review["id"], review["status"], review.get("input_hash")) if review else None,
                [(r["id"], r.get("text"), r.get("speaker"), r.get("speaker_role"),
                  r.get("speaker_source"), r.get("review_parts")) for r in rows]))
        return hashlib.sha256(json.dumps([processing_signature(repo), values], ensure_ascii=False,
                                         sort_keys=True).encode()).hexdigest()

    def _ground_session(self, content: dict, pairs: list[tuple[dict, dict]], items: list[dict]) -> None:
        """우리 기관 관련 요지는 블록 관련도(언급 근거로 보정됨)에서 만들고, 후속조치·이슈 근거 발언을 보강한다."""
        org_pairs = [(b, sm) for b, sm in pairs if sm.get("org_relevance") in ("high", "medium")]
        org_n = len(org_pairs)
        content["org_points"] = session_org_points(pairs)
        content["overview"] = ground_overview(content.get("overview") or "", org_pairs)
        content["next_actions"] = merge_actions(content.get("next_actions") or [], session_actions(items, org_n))
        refs = {b["id"]: block_ref_utterance(sm) for b, sm in pairs}
        for it in content.get("issues") or []:
            if it.get("utterance_id") is None and it.get("block_id") in refs:
                it["utterance_id"] = refs[it["block_id"]] or self._first_member_utt(it["block_id"])

    def _first_member_utt(self, block_id: int) -> int | None:
        row = self.rt.repo.db.query_one(
            "SELECT id FROM utterances WHERE block_id=? AND speaker_role IN ('member','unknown','other') "
            "ORDER BY ts_start, seq LIMIT 1", (block_id,))
        return row["id"] if row else None

    @staticmethod
    def _digests(pairs: list[tuple[dict, dict]]) -> list[str]:
        """블록 요지 줄. 너무 길면 중요도 높은 블록부터 남기고 시간순으로 되돌린다."""
        lines = [(b["ts_start"], int(sm.get("importance") or 0), prompts.block_digest(b, sm)) for b, sm in pairs]
        if sum(len(x[2]) for x in lines) <= SESSION_DIGEST_CHARS:
            return [x[2] for x in lines]
        keep, total = [], 0
        for x in sorted(lines, key=lambda x: (-x[1], x[0])):
            if total + len(x[2]) > SESSION_DIGEST_CHARS:
                continue
            keep.append(x)
            total += len(x[2])
        return [x[2] for x in sorted(keep, key=lambda x: x[0])]
