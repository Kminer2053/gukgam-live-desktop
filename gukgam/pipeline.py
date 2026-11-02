"""세션 파이프라인: 수집원 SegmentEvent → 발언 저장 → (확정 시) 화자·블록 추정, 규칙 추출, 감시 키워드 → 이벤트 발행.

순서 보장: 확정(final) 발언도 그보다 앞선 미확정 발언이 남아 있으면 분석을 미룬다(워터마크).
미확정이 STALE_MS 이상 갱신되지 않거나 수집이 멈추면 강제 확정한다.
강제 확정·분석한 발언에 늦게 다른 본문의 확정 결과가 오면 본문을 바꾸고 감시 키워드·알림·규칙 항목을 다시 맞춘다.
조각의 턴 수가 줄면(부분 결과 포함) 분석 전의 남는 턴은 바로 지운다. 사람이 고친 본문은 수집원이 덮거나 지우지 않는다.
사람이 손댄 항목(edited=1)·처리한 항목은 재추출이 바꾸지 않는다. 대체되는 자동 항목은 지우지 않고 'superseded' 로 숨긴다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Protocol

from . import serialize, settings
from .config import KST
from .db import now_ms
from .events import EventBus
from .models import SegmentEvent, Turn
from .nlp import rules
from .nlp.handover_parts import direct_handover_parts
from .nlp.speaker import BlockTracker, apply_manual_questioner, needs_answer_question_review, parse_hint, split_handover
from .db import item_fingerprint_basis
from .repo import Repo

log = logging.getLogger(__name__)

STALE_MS = 45_000
STATS_MIN_SEC = 1.0          # stats 이벤트 최소 간격(몰아치는 갱신은 마지막 값으로 한 번 더 보낸다)
# 질의자를 지정하면 요구 위원(requester)을 맞추는 항목 종류
REQUESTER_KINDS = ("data_request", "issue", "written_question", "commitment", "witness_request")


class AnalysisHooks(Protocol):
    """LLM 분석 서비스가 구현(gukgam.llm.service.AnalysisService)."""

    def on_block_closed(self, session_id: str, block_id: int) -> None: ...
    def on_utterance_final(self, session_id: str, utterance: dict[str, Any]) -> None: ...
    def on_session_stopped(self, session_id: str) -> None: ...


class SessionPipeline:
    def __init__(self, repo: Repo, bus: EventBus, session_id: str, hooks: AnalysisHooks | None = None):
        self.repo = repo
        self.bus = bus
        self.sid = session_id
        self.hooks = hooks
        self.tracker = BlockTracker(repo, session_id)
        self.lock = asyncio.Lock()
        # 미확정·분석 대기 발언이 남았는지(주기 tick 대상 판단). 처음에는 모르므로 True.
        self.has_unfinished = True
        self._stats_at = 0.0
        self._stats_timer: asyncio.TimerHandle | None = None
        self._turn_tails: dict[str, str] = {}
        self.reload_settings()

    def reload_settings(self) -> None:
        self.terms = settings.watch_terms(self.repo)
        self.departments = settings.get(self.repo, "departments") or []
        self.tracker.reload_roster()

    # ── 발행 헬퍼 ────────────────────────────────────────────
    def _pub(self, type_: str, data: dict[str, Any]) -> None:
        self.bus.publish(self.sid, type_, data)

    def _pub_utt(self, u: dict | None) -> None:
        if u:
            self._pub("utterance.upsert", serialize.utterance(u))
        for other in self.repo.take_review_invalidations(self.sid):
            if not u or other["id"] != u["id"]:
                self._pub("utterance.upsert", serialize.utterance(other))
        for review in self.repo.take_review_state_invalidations(self.sid):
            self._pub("transcript.review", {"id": review["id"], "block_id": review["block_id"], "status": review["status"]})

    def _pub_block(self, bid: int) -> None:
        stages = getattr(getattr(self.hooks, "rt", None), "stages", None)
        if stages is not None and stages.owns(self.sid):
            # 실황 수신에서는 과거 보정 문서 전체를 다시 검증하지 않는다.
            # 보정 본문·요약은 별도 조회/보정 이벤트가 같은 판본으로 전달한다.
            b = self.repo.stage_block_record(self.sid, bid)
            if b:
                summary = b.get("summary")
                if (isinstance(summary, dict) and not summary.get("edited_by")
                        and b.get("review_status") != "reviewed"):
                    b = {**b, "summary": None, "summary_at": None}
                self._pub("block.upsert", serialize.block(b))
            self._pub_utt(None)
            return
        b = self.repo.get_block(bid)
        if b:
            from .review_display import get_block_display
            self._pub("block.upsert", get_block_display(self.repo, b))
        self._pub_utt(None)

    def _pub_item(self, iid: int) -> None:
        row = self.repo.get_item_full(iid)
        if row:
            self._pub("item.upsert", serialize.item(row))

    def _open_block_ids(self) -> list[int]:
        return [r["id"] for r in self.repo.db.query(
            "SELECT id FROM blocks WHERE session_id=? AND is_open=1", (self.sid,))]

    def publish_stats(self, force: bool = False) -> None:
        """stats 발행. 세션 통계 쿼리가 발언 수에 비례하므로 STATS_MIN_SEC 안의 반복은 묶어 마지막에 한 번 보낸다."""
        now = time.monotonic()
        wait = self._stats_at + STATS_MIN_SEC - now
        if not force and wait > 0:
            if self._stats_timer is None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    self._stats_timer = loop.call_later(wait, self._stats_due)
                    return
            else:
                return
        if self._stats_timer is not None:
            self._stats_timer.cancel()
            self._stats_timer = None
        self._stats_at = now
        self._pub("stats", self.repo.session_stats(self.sid))

    def _stats_due(self) -> None:
        self._stats_timer = None
        try:
            self.publish_stats(force=True)
        except Exception:  # pragma: no cover - 종료 중(DB 닫힘) 등
            log.debug("지연 stats 발행 실패: %s", self.sid, exc_info=True)

    # ── 수집원 입력 ──────────────────────────────────────────
    async def handle(self, ev: SegmentEvent) -> None:
        async with self.lock:
            if ev.raw.get("speaker_revision_only"):
                self._ingest_source_revision(ev)
                return
            changed = self._ingest(ev)
            self._analyze_ready(force=False, dirty=ev.is_final or changed)

    def _ingest_source_revision(self, ev: SegmentEvent) -> None:
        """늦은 원조각 후보를 저장하되 기존 발언·블록·분석 대기열은 건드리지 않는다."""
        if ev.turns or not ev.is_final:
            return
        changed_keys = self.repo.merge_source_revision(self.sid, ev.key, ev.raw.get("speaker_revision"))
        blocks: dict[int, set[int]] = {}
        for key in changed_keys:
            ids = self.repo.source_utterance_ids(self.sid, key)
            for bid in self.repo.source_block_ids(self.sid, key):
                blocks.setdefault(bid, set()).update(ids)
        for bid, ids in blocks.items():
            # 원본문은 그대로다. 새 기계 후보만으로 사람이 채택한 표시 판본을 해제하지 않는다.
            self.repo.invalidate_block_reviews(bid, changed_uids=ids, preserve_accepted=True)
            if self.hooks:
                self.hooks.on_block_closed(self.sid, bid)
            self._pub_block(bid)

    def _split_turns(self, turns: list[Turn], *, allow_short_call: bool = True) -> list[Turn]:
        """직접 수집원 이름을 보존하고 진행 안내와 명확한 본인 소개를 나눈다."""
        names = {e.name for e in self.tracker.roster.entries if e.role == "member"}
        if not names:
            return turns
        out: list[Turn] = []
        for t in turns:
            if t.speaker_hint and not t.speaker_hint.startswith("hwa:"):
                out.append(t)
                continue
            pending_name = self.tracker.pending.name if self.tracker.pending else None
            parts = direct_handover_parts(t.text, names, expected_name=pending_name,
                                          allow_short_call=allow_short_call)
            if parts is None:
                parts = split_handover(t.text, names)
            if len(parts) == 1 and not parts[0][1]:
                out.append(t)
                continue
            for j, (text, hint) in enumerate(parts):
                out.append(Turn(text=text, speaker_hint=hint or (t.speaker_hint if j == 0 else None),
                                new_speaker=t.new_speaker if j == 0 else True,
                                source_speaker_hint=t.source_speaker_hint or t.speaker_hint))
        return out

    def _fixed_handover_turns(self, key: str, turns: list[Turn], first: dict | None, second: dict | None) -> list[Turn] | None:
        """이미 분석한 두 행의 호명 판본은 현재 다른 위원의 대기 이름과 분리한다."""
        if not first or not second or not (first.get("analyzed") or second.get("analyzed")):
            return None
        name, role = parse_hint(second.get("speaker_hint"))
        if (first.get("speaker_hint") != "추정:위원장" or role != "member" or not name
                or not str(second.get("speaker_hint") or "").startswith("추정:")):
            return None
        rows = [first, second]
        old = [row.get("original_text") or row.get("text") or "" for row in rows]
        previous = direct_handover_parts(" ".join(old), {name}, expected_name=name)
        if not previous or len(previous) != 2 or [text.strip() for text, _ in previous] != old:
            return None
        # 앞 두 행만 고정하다 세 번째 답변을 버리지 않도록 같은 원조각 전체를 보존한다.
        rows = sorted((row for uid in self.repo.source_utterance_ids(self.sid, key)
                       if (row := self.repo.get_utterance(uid))),
                      key=lambda row: int(row["source_key"].rsplit("#", 1)[-1]))
        old = [row.get("original_text") or row.get("text") or "" for row in rows]
        protected = any(row.get("review_parts") or row.get("speaker_source") == "manual"
                        or row.get("text") != row.get("original_text")
                        or (row.get("block_id") and (self.repo.stage_block_record(self.sid, row["block_id"]) or {}).get("review_status") == "reviewed")
                        for row in rows)
        # 다른 이름·경계와 사람 검수 뒤의 늦은 본문은 segments에 보존한다.
        parts = (direct_handover_parts(turns[0].text, {name}, expected_name=name)
                 if len(turns) == 1 and len(rows) == 2 and not protected else None)
        if not parts or len(parts) != 2:
            return [Turn(text=text, speaker_hint=row.get("speaker_hint"),
                         new_speaker=bool(row.get("new_speaker")),
                         source_speaker_hint=row.get("source_speaker_hint")) for text, row in zip(old, rows)]
        return [Turn(text=text, speaker_hint=hint, new_speaker=bool(row.get("new_speaker")),
                     source_speaker_hint=turns[0].source_speaker_hint or turns[0].speaker_hint)
                for (text, hint), row in zip(parts, rows)]

    def _ingest(self, ev: SegmentEvent) -> bool:
        """발언 저장. 통계가 바뀌었을 수 있으면(턴 삭제·분석된 발언 본문 변경) True."""
        turns = [t for t in ev.turns if t.text and t.text.strip()] or ev.turns
        first = self.repo.get_utterance_by_key(self.sid, f"{ev.key}#0")
        second = self.repo.get_utterance_by_key(self.sid, f"{ev.key}#1") if first else None
        # 이미 분석한 단일 행에 뒤늦게 자기소개가 붙어도 새 행·과거 질의를 만들지 않는다.
        fixed_single = bool(first and (first.get("analyzed") or first.get("speaker_source") == "manual")
                            and not second)
        fixed = self._fixed_handover_turns(ev.key, turns, first, second)
        turns = fixed if fixed is not None else self._split_turns(turns, allow_short_call=not fixed_single)
        previous = self.repo.get_segment(self.sid, ev.key) if ev.raw.get("speaker_revision") else None
        revision_changed = (bool(ev.raw.get("speaker_revision"))
                            and ev.raw["speaker_revision"] != ((previous or {}).get("raw") or {}).get("speaker_revision"))
        self.repo.upsert_segment(self.sid, ev.key, ev.ts_start, ev.ts_end, ev.text, ev.is_final, ev.raw)
        if revision_changed:
            for bid in self.repo.source_block_ids(self.sid, ev.key):
                self.repo.invalidate_block_reviews(bid, changed_uids=self.repo.source_utterance_ids(self.sid, ev.key))
        n = len(turns)
        total_chars = sum(len(t.text) for t in turns) or 1
        span = max(0, (ev.ts_end or ev.ts_start) - ev.ts_start)
        acc = 0
        revised: list[dict] = []
        base_turn = str(ev.raw.get("turn_id") or "")
        continuation = ev.raw.get("boundary_reason") in {"limit_chars", "limit_time"}
        inherited = self._turn_tails.get(base_turn, base_turn) if continuation else base_turn
        for i, turn in enumerate(turns):
            # 한 조각 안 여러 화자 턴의 시작 시각을 글자 수 비율로 보간
            ts_i = ev.ts_start + (span * acc // total_chars if n > 1 else 0)
            acc += len(turn.text)
            ts_end_i = ev.ts_start + (span * acc // total_chars) if ev.ts_end else None
            row, _created, changed = self.repo.upsert_utterance_tracked(
                self.sid, f"{ev.key}#{i}", ts_start=ts_i, ts_end=ts_end_i, text=turn.text.strip(),
                is_final=ev.is_final, speaker_hint=turn.speaker_hint, new_speaker=turn.new_speaker,
                source_order=_source_order(ev.raw.get("source_order"), i),
                turn_id=(inherited + (f"#{i}" if n > 1 else "")) if base_turn else "",
                boundary_reason=str(ev.raw.get("boundary_reason") or ""),
                source_speaker_hint=turn.source_speaker_hint or turn.speaker_hint or "",
            )
            if not row["analyzed"]:
                self._pub_utt(row)
            elif changed:
                # 이미 분석한 발언(45초 강제 확정 등)의 본문이 늦은 확정 결과로 바뀌었다 → 다시 맞춘다
                revised.append(row)
        if base_turn:
            self._turn_tails[base_turn] = inherited + (f"#{n - 1}" if n > 1 else "")
            while len(self._turn_tails) > 64:
                self._turn_tails.pop(next(iter(self._turn_tails)))
        dropped = self._drop_shrunk_turns(ev.key, n)
        for row in revised:
            review_required = False
            block = self.repo.stage_block_record(self.sid, row["block_id"]) if row.get("block_id") else None
            revise = getattr(self.tracker, "revise_speaker", None)
            if (revise is not None and row.get("speaker_source") != "manual" and block
                    and block.get("review_status") != "reviewed"):
                from .nlp.role_evidence import role_evidence, weak_response_spans
                fields = revise(row)
                if fields:
                    previous_role = row.get("speaker_role")
                    row = self.repo.update_utterance(row["id"], **fields) or row
                    body_evidence = role_evidence(row.get("text") or "")
                    review_required = (fields.get("speaker_role") == "unknown"
                                       or body_evidence["mixed"]
                                       or bool(body_evidence["role"] and previous_role != fields.get("speaker_role")))
                summary = block.get("summary") or {}
                review_protected = (row.get("review_parts") or row.get("edited") or row.get("edited_by")
                    or row.get("original_text") is not None and row.get("text") != row["original_text"]
                    or isinstance(summary, dict) and summary.get("edited_by"))
                fragment_review = getattr(self.tracker, "needs_answer_fragment_review", None)
                # 역할 수정이 없어도 응답 후보·직접 질문·미종결 연결을 기존 검토 창으로 보낸다.
                if not review_protected and (weak_response_spans(row.get("text") or "")
                        or needs_answer_question_review(row, block)
                        or fragment_review is not None and fragment_review(row, block)):
                    review_required = True
            u2 = self._refresh_text(row)
            self._pub_utt(u2)
            if self.hooks:
                notice = ({**u2, "review_required": True, "review_reason": "확정 갱신 뒤 화자 역할 확인"}
                          if review_required else u2)
                self.hooks.on_utterance_final(self.sid, notice)
                if block and not block.get("is_open"):
                    self.hooks.on_block_closed(self.sid, block["id"])
        if revision_changed and self.hooks:
            for bid in self.repo.source_block_ids(self.sid, ev.key):
                self.hooks.on_block_closed(self.sid, bid)
        self._pub_utt(None)
        return bool(dropped or revised)

    def _drop_shrunk_turns(self, key: str, n: int) -> int:
        """조각의 턴 수가 줄었으면(부분·확정 모두) 남는 옛 턴을 바로 지운다.

        아직 분석 전이고 사람이 고치거나 검수하지 않은 행만 대상이다. 확정 여부는 보지 않는다 — 45초 강제 확정에 먼저
        걸린 옛 턴이 유령 발언으로 남지 않게. source_key 범위 검색이라 세션 전체를 훑지 않는다. 지운 행 수.
        """
        dropped = 0
        for r in self.repo.db.query(
            "SELECT id, source_key FROM utterances WHERE session_id=? AND source_key>? AND source_key<? "
            "AND analyzed=0 AND text=original_text AND speaker_source<>'manual' "
            "AND (block_id IS NULL OR NOT EXISTS (SELECT 1 FROM blocks b "
            "WHERE b.id=utterances.block_id AND b.review_status='reviewed'))",
            (self.sid, f"{key}#", f"{key}$"),
        ):
            base, _, idx = r["source_key"].rpartition("#")
            if base != key or not idx.isdigit() or int(idx) < n:
                continue
            self.repo.db.execute("DELETE FROM utterances WHERE id=?", (r["id"],))
            self._pub("utterance.delete", {"id": r["id"]})
            dropped += 1
        return dropped

    def _analyze_ready(self, force: bool, dirty: bool = False) -> None:
        now = now_ms()
        if force:
            cur = self.repo.db.execute("UPDATE utterances SET is_final=1 WHERE session_id=? AND is_final=0",
                                       (self.sid,))
        else:
            # 오래 갱신되지 않은 미확정은 확정으로 간주
            cur = self.repo.db.execute(
                "UPDATE utterances SET is_final=1 WHERE session_id=? AND is_final=0 AND updated_at<?",
                (self.sid, now - STALE_MS))
        dirty = dirty or force or (cur.rowcount or 0) > 0
        wm_row = self.repo.db.query_one(
            "SELECT MIN(ts_start) AS wm FROM utterances WHERE session_id=? AND is_final=0", (self.sid,))
        watermark = wm_row["wm"] if wm_row and wm_row["wm"] is not None else None
        for u in self.repo.pending_final_utterances(self.sid):
            if watermark is not None and u["ts_start"] >= watermark:
                break
            self._analyze(u)
            dirty = True
        self.has_unfinished = watermark is not None
        if dirty:
            self.publish_stats(force=force)

    def _analyze(self, u: dict[str, Any]) -> None:
        open_before = self._open_block_ids()
        d = self.tracker.process(u)
        manual = u["speaker_source"] == "manual"
        mentions = rules.find_mentions(u["text"], self.terms)
        fields: dict[str, Any] = {
            "block_id": d.block_id,
            "analyzed": 1,
            "mentions": _mention_rows(mentions),
        }
        if not manual:
            fields.update(speaker=d.speaker, speaker_role=d.role, speaker_source=d.source)
        u2 = self.repo.update_utterance(u["id"], **fields)
        assert u2 is not None
        # 생성·갱신된 블록과 이번에 닫힌 블록(위원장·기타 블록 포함: is_open=0 을 화면에 알린다)
        still_open = set(self._open_block_ids()) if open_before else set()
        closed_now = [bid for bid in open_before if bid not in still_open]
        for bid in dict.fromkeys(d.block_changed + d.closed_blocks + closed_now):
            self._pub_block(bid)
        for rid in d.relabeled_utterances:
            self._pub_utt(self.repo.get_utterance(rid))
        for iid in d.updated_items:      # 질의자 확정으로 요구 위원·발언자가 채워진 항목
            self._pub_item(iid)
        self._pub_utt(u2)
        self._extract(u2)
        self._add_alerts(u2, mentions)
        if self.hooks:
            for bid in d.closed_blocks:
                self.hooks.on_block_closed(self.sid, bid)
            if getattr(d, "review_required", False) and not manual:
                self.hooks.on_utterance_final(self.sid, {**u2, "review_required": True,
                    "review_reason": getattr(d, "review_reason", "")})
            else:
                self.hooks.on_utterance_final(self.sid, u2)

    def _add_alerts(self, u: dict[str, Any], mentions: list) -> bool:
        created_any = False
        for m in mentions:
            a, created = self.repo.add_alert(self.sid, u["id"], m.keyword, m.category)
            if created and a:
                a.update(text=u["text"], speaker=u["speaker"], ts_start=u["ts_start"])
                self._pub("alert", serialize.alert(a))
                created_any = True
        return created_any

    def _rule_items(self, u: dict[str, Any]) -> list[rules.ExtractedItem]:
        block = self.repo.stage_block_record(self.sid, u["block_id"]) if u.get("block_id") else None
        requester = block["questioner"] if block and block["kind"] == "question" else ""
        # 기한('일주일 내' 등)은 처리한 날이 아니라 그 발언이 나온 날(KST) 기준
        base = datetime.fromtimestamp(u["ts_start"] / 1000, KST).date()
        return rules.extract_items(u["text"], u["speaker_role"], speaker=u["speaker"], requester=requester,
                                   base_date=base)

    def _extract(self, u: dict[str, Any], items: list[rules.ExtractedItem] | None = None) -> list[int]:
        created_ids: list[int] = []
        for it in (self._rule_items(u) if items is None else items):
            dept = rules.assign_department(it.quote or it.text, self.departments)
            row, created = self.repo.add_item(
                self.sid, kind=it.kind, text=it.text, quote=it.quote, speaker=it.speaker, requester=it.requester,
                block_id=u.get("block_id"), utterance_id=u["id"], origin=it.origin, confidence=it.confidence,
                assignee_dept=dept, strength=it.strength, due_date=it.due_date,
            )
            if created:
                created_ids.append(row["id"])
                self._pub_item(row["id"])
        return created_ids

    def _refresh_text(self, u: dict[str, Any]) -> dict[str, Any]:
        """분석이 끝난 발언의 본문이 바뀐 뒤(운영자 교정·늦은 확정): 감시 키워드·알림·규칙 항목을 다시 맞춘다."""
        mentions = rules.find_mentions(u["text"], self.terms)
        u2 = self.repo.update_utterance(u["id"], mentions=_mention_rows(mentions))
        assert u2 is not None
        alerts_changed = self._add_alerts(u2, mentions)
        for aid in self.repo.prune_alerts(u2["id"], keep=[m.keyword for m in mentions]):
            self._pub("alert.delete", {"id": aid, "utterance_id": u2["id"]})
            alerts_changed = True
        items_changed = self._reextract_rules(u2)
        if alerts_changed or items_changed:
            self.publish_stats()
        return u2

    def _reextract_rules(self, u: dict[str, Any]) -> bool:
        """규칙 항목 재추출. 사람이 손대지 않은(edited=0, status='open', 메모 없음) 규칙 항목만 대체한다.

        새 추출에 그대로 다시 나오는 항목은 id 를 유지하고, 사라진 항목만 supersede 한다. 사람이 손댄(또는 처리한)
        항목이 있는 종류는 다시 만들지 않는다(교정된 요지·담당부서·기한을 지키고 중복을 막는다).
        """
        uid = u["id"]
        olds = self.repo.items_for_utterance(uid, origin="rule")
        replaceable = [i for i in olds if i["status"] == "open" and not i.get("edited") and not i.get("note")
                       and self.repo.item_auto_replaceable(i)]
        rep_ids = {i["id"] for i in replaceable}
        keep_kinds = {i["kind"] for i in olds if i["id"] not in rep_ids}
        extracted = self._rule_items(u)
        bases = {item_fingerprint_basis(it.kind, str(uid or ""), it.quote or it.text) for it in extracted}
        stale = [i["id"] for i in replaceable if self.repo.item_basis(i) not in bases]
        self.repo.supersede_items(stale)
        created = self._extract(u, [it for it in extracted if it.kind not in keep_kinds])
        if stale:
            self._pub("items.reload", {"utterance_id": uid, "block_id": u.get("block_id")})
        return bool(stale or created)

    # ── 운영자 조작 ──────────────────────────────────────────
    async def flush(self, *, close_blocks: bool = True) -> list[int]:
        """미확정 발언을 확정·분석한다. close_blocks=True(종료)면 열린 블록을 닫고 대기 호명을 지운 뒤 요약을 건다.

        close_blocks=False(일시정지·수집원 오류로 자동 일시정지)면 열린 블록·대기 호명(pending)을 그대로 둔다 —
        재개하면 같은 질의 블록에 이어 붙는다. 이때는 블록 닫힘·세션 종료 훅을 부르지 않는다.
        돌려주는 값은 닫은 질의 블록 id 목록.
        """
        async with self.lock:
            self._analyze_ready(force=True)
            if not close_blocks:
                return []
            open_before = self._open_block_ids()
            closed = self.tracker.close_all()
            # 위원장·기타 블록도 닫혔음을 알린다(close_all 은 질의 블록만 돌려준다)
            for bid in dict.fromkeys(open_before + closed):
                self._pub_block(bid)
            if self.hooks:
                for bid in closed:
                    self.hooks.on_block_closed(self.sid, bid)
                self.hooks.on_session_stopped(self.sid)
            return closed

    async def tick(self) -> None:
        """주기 호출: 오래된 미확정 발언 정리."""
        async with self.lock:
            self._analyze_ready(force=False)

    async def add_manual(self, text: str, speaker: str = "", role: str = "", ts: int | None = None) -> None:
        """속기 보조: 운영자가 직접 입력한 발언."""
        ts = ts or now_ms()
        hint = None
        if speaker:
            hint = speaker if role != "chair" or speaker.endswith("위원장") else f"{speaker} 위원장"
            if role == "member" and not speaker.endswith(("위원", "의원")):
                hint = f"{speaker} 위원"
            if role == "answer" and not hint.startswith("답변"):
                hint = f"답변 {speaker}" if not speaker.endswith(("장관", "사장", "원장", "청장", "이사장")) else speaker
        ev = SegmentEvent(key=f"m{ts}", ts_start=ts, ts_end=ts, turns=[Turn(text=text, speaker_hint=hint)],
                          is_final=True, raw={"manual": True})
        await self.handle(ev)

    async def add_marker(self, text: str, ts: int | None = None, key: str | None = None) -> dict:
        """정회·속개·수신 중단 같은 기록 표지(speaker_role='system'). 화자추정·추출 대상이 아니다."""
        ts = ts or now_ms()
        async with self.lock:
            row, _ = self.repo.upsert_utterance(self.sid, key or f"x{ts}", ts_start=ts, ts_end=ts, text=text,
                                                is_final=True, speaker="", speaker_role="system",
                                                speaker_source="feed")
            ob = self.repo.db.query_one(
                "SELECT id FROM blocks WHERE session_id=? AND is_open=1 ORDER BY ts_start DESC, id DESC LIMIT 1",
                (self.sid,))
            row = self.repo.update_utterance(row["id"], analyzed=1, speaker_role="system", speaker_source="feed",
                                             block_id=ob["id"] if ob else None)
            self._pub_utt(row)
            return row  # type: ignore[return-value]

    async def set_block_questioner(self, block_id: int, name: str, party: str = "", actor: str = "operator") -> dict:
        async with self.lock:
            before = self.repo.get_block(block_id)
            old_name = (before or {}).get("questioner") or ""
            b, relabeled = apply_manual_questioner(self.repo, block_id, name, party, actor=actor)
            self._pub("block.upsert", serialize.block(b))
            for uid in relabeled:
                self._pub_utt(self.repo.get_utterance(uid))
            # 이 블록 자동 항목의 요구 위원(requester) 갱신: 비어 있거나 이전 질의자였던 것만.
            # 사람이 손댄 항목(edited=1)·수동 항목은 시스템이 덮어쓰지 않는다(보충 요구 위원 등 보존).
            for it in self.repo.list_items(self.sid, block_id=block_id):
                if it["origin"] == "manual" or it.get("edited") or it["kind"] not in REQUESTER_KINDS:
                    continue
                if it["requester"] == name or it["requester"] not in ("", old_name):
                    continue
                self.repo.update_item(it["id"], requester=name)
                self._pub_item(it["id"])
            self.publish_stats(force=True)
            if self.hooks:
                self.hooks.on_block_closed(self.sid, block_id)  # 이름이 바뀌었으니 요약 갱신
            return b

    async def edit_utterance(self, uid: int, *, text: str | None = None, speaker: str | None = None,
                             speaker_role: str | None = None, actor: str = "operator") -> dict:
        async with self.lock:
            u = self.repo.get_utterance(uid)
            if not u or u["session_id"] != self.sid:
                raise KeyError(uid)
            fields: dict[str, Any] = {}
            if text is not None and text.strip() != u["text"]:
                fields["text"] = text.strip()
            if speaker is not None:
                fields["speaker"] = speaker.strip()
                fields["speaker_source"] = "manual"
            if speaker_role is not None:
                fields["speaker_role"] = speaker_role
                fields["speaker_source"] = "manual"
            if not fields:
                return u
            if "text" in fields and not u["is_final"]:
                # '수신 중' 발언을 사람이 고치면 그 본문으로 확정한다(뒤늦은 수집원 확정이 지우거나 덮지 않게)
                fields["is_final"] = 1
            u2 = self.repo.update_utterance(uid, **fields)
            assert u2 is not None
            self.repo.audit(actor, "utterance.edit", str(uid),
                            {k: v for k, v in fields.items() if k != "is_final"})
            if "text" in fields:
                if u["analyzed"]:
                    u2 = self._refresh_text(u2)
                else:
                    # 아직 분석 전: 화자 추정·추출·알림은 정상 분석 경로가 고친 본문으로 한다
                    self._analyze_ready(force=False, dirty=True)
                    u2 = self.repo.get_utterance(uid) or u2
            self._pub_utt(u2)
            return u2


def _mention_rows(mentions: list) -> list[dict[str, Any]]:
    return [{"keyword": m.keyword, "category": m.category, "start": m.start, "end": m.end} for m in mentions]


def _source_order(value: Any, piece: int) -> str:
    """출처 정수 순서를 같은 시각의 행에서 비교 가능한 문자열로 만든다."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return ""
    if any(type(n) is not int or n < 0 or n >= 10 ** 12 for n in value):
        return ""
    return ":".join(f"{n:012d}" for n in (*value, piece))
