"""질의 종료 후 제한된 화자 검토와 원문을 보존하는 채택 처리."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from copy import deepcopy
from typing import Callable

from . import serialize, settings
from .db import now_ms
from .transcript_review import CONTEXT_RADIUS, ReviewEngine, snapshot_hash, take_snapshot, take_window_snapshot, validate_result

WINDOW_DEBOUNCE_SEC = 3
CLOSED_DEBOUNCE_SEC = 3
ACTIVE_REVIEW_LIMIT = 2
CLOSED_QUEUE_LIMIT = 32
CLOSED_RETRY_LIMIT = 1
LIVE_WINDOW_SPAN_MS = 5 * 60 * 1000
CONTEXT_QUEUE_LIMIT = 128
CONTEXT_TEXT_TIMEOUT_SEC = 30
log = logging.getLogger(__name__)


class TranscriptReviewService:
    def __init__(self, rt, *, engine=None, context_provider=None, context_windows: bool = True):
        self.rt = rt
        self.engine = engine or ReviewEngine(rt)
        self._lock = asyncio.Lock()
        self._tasks: dict[tuple, asyncio.Task] = {}
        self._callbacks: dict[tuple, list[Callable]] = defaultdict(list)
        self._waiting_blocks: dict[tuple, None] = {}
        self._block_versions: dict[tuple, int] = {}
        self._deferred_blocks: dict[tuple, str] = {}
        self._window_targets: dict[tuple, tuple[int, ...]] = {}
        self._window_seen: dict[tuple, str] = {}
        self._context_provider = context_provider
        self._context_windows = context_windows
        self._context_waiting: dict[tuple, str] = {}
        self._context_seen: dict[tuple, str] = {}
        self._context_rescan: set[tuple] = set()
        self._context_flushed: set[tuple] = set()
        self._closed = False
        self._audio_tasks: set[asyncio.Task] = set()

    def enabled(self) -> bool:
        cfg = settings.get(self.rt.repo, "transcript_review")
        return bool(cfg.get("enabled") and cfg.get("use_jev", True))

    def has_pending_closed(self, sid: str, block_id: int | None = None) -> bool:
        """종료 검토의 완료를 확인한다. 블록 지정 시 다른 질의는 기다리지 않는다."""
        return any(key[0] == sid and len(key) == 2 and (block_id is None or key[1] == block_id)
                   for key in self._tasks) or any(
            key[0] == sid and (block_id is None or key[1] == block_id) for key in self._waiting_blocks)

    def status(self) -> dict:
        provider = getattr(self.engine, "provider", None)
        result = {"enabled": self.enabled(), "configured": bool(getattr(provider, "available", False)),
                  "queue": len(self._tasks), "running": self._lock.locked()}
        cfg = settings.get(self.rt.repo, "transcript_review")
        selected = cfg.get("role_decision_provider", "jev" if cfg.get("use_jev", False) else "none")
        result["role_decision_provider"] = selected
        result["legacy_review_enabled"] = self.enabled()
        result["closed_block_role_enabled"] = bool(cfg.get("enabled") and cfg.get("auto_block") and selected != "none")
        state = getattr(provider, "status", None)
        if callable(state):
            result["role_decision_state"] = state()
        provenance = getattr(provider, "provenance", None)
        if isinstance(provenance, dict):
            result["role_decision_provenance"] = deepcopy(provenance)
        if self._waiting_blocks:
            result["pending_closed"] = len(self._waiting_blocks)
        if self._context_waiting or self._context_rescan:
            result["pending_context"] = len(self._context_waiting)
            result["context_rescan_blocks"] = len(self._context_rescan)
        if self._deferred_blocks:
            result["deferred_closed"] = [{"session_id": key[0], "block_id": key[1], "reason": reason}
                                          for key, reason in self._deferred_blocks.items()]
        return result

    def _defer_block(self, key: tuple, reason: str) -> None:
        """한도를 넘거나 계속 바뀐 종료 검토를 제한된 상태 목록에 남긴다."""
        self._deferred_blocks.pop(key, None)
        self._deferred_blocks[key] = reason
        while len(self._deferred_blocks) > CLOSED_QUEUE_LIMIT:
            self._deferred_blocks.pop(next(iter(self._deferred_blocks)))

    def _promote_closed(self) -> None:
        """빈 자리는 의심 창보다 먼저 종료된 질의에 배정한다."""
        if self._closed:
            return
        while self._waiting_blocks and len(self._tasks) < ACTIVE_REVIEW_LIMIT:
            key = next(iter(self._waiting_blocks))
            self._waiting_blocks.pop(key)
            sid, bid = key
            self._tasks[key] = asyncio.create_task(self._scheduled(sid, bid), name=f"speaker-review-{bid}")
        self._promote_context()

    def _context_enabled(self) -> bool:
        return bool(self._context_windows and not self._closed and self.enabled()
                    and settings.get(self.rt.repo, "transcript_review").get("auto_block"))

    def _context_target_allowed(self, row: dict, block: dict | None) -> bool:
        from .transcript_review import _protected
        return bool(block and row.get("is_final") and row.get("speaker_role") != "system"
                    and not row.get("review_parts") and not _protected({"block": {
                        **block, "edited_summary": bool(isinstance(block.get("summary"), dict)
                                                        and block["summary"].get("edited_by"))}}, row))

    def _queue_context(self, sid: str, *, changed_uid: int | None = None,
                       block_id: int | None = None, promote: bool = True) -> None:
        """확정 발언마다 뒤 세 발언을 기다리며 수정 통지는 같은 대상에 합친다."""
        if not self._context_enabled():
            return
        if changed_uid is not None:
            candidates = self.rt.repo.context_utterances(changed_uid, radius=CONTEXT_RADIUS)
        else:
            candidates = self.rt.repo.list_utterances(sid, block_id=block_id, final_only=True, limit=-1)
        blocks = {}
        for row in candidates:
            bid = row.get("block_id")
            if not bid:
                continue
            if bid not in blocks:
                blocks[bid] = self.rt.repo.get_block(bid)
            block = blocks[bid]
            key = (sid, bid, "context", row["id"])
            if not self._context_target_allowed(row, block):
                self._context_waiting.pop(key, None)
                continue
            adjacent = self.rt.repo.context_utterances(row["id"], radius=CONTEXT_RADIUS)
            positions = {r["id"]: i for i, r in enumerate(adjacent)}
            if row["id"] not in positions:
                continue
            if len(adjacent) - positions[row["id"]] - 1 < CONTEXT_RADIUS and (sid, bid) not in self._context_flushed:
                continue
            try:
                digest = snapshot_hash(take_window_snapshot(self.rt.repo, sid, bid, [row["id"]], context_radius=CONTEXT_RADIUS))
            except (KeyError, ValueError):
                continue
            if self._context_seen.get(key) == digest:
                continue
            if key not in self._context_waiting and len(self._context_waiting) >= CONTEXT_QUEUE_LIMIT:
                self._context_rescan.add((sid, bid))
                continue  # 큐가 줄면 같은 블록을 다시 훑어 빠뜨린 대상을 채운다.
            self._context_waiting[key] = digest
        if promote:
            self._promote_context()

    def flush_context(self, sid: str, block_id: int | None = None) -> None:
        """질의·수집 종료 시 뒤 문맥이 부족한 마지막 발언도 가용 문맥으로 검토한다."""
        if not self._context_enabled():
            return
        ids = {block_id} if block_id is not None else {
            r.get("block_id") for r in self.rt.repo.list_utterances(sid, final_only=True, limit=-1) if r.get("block_id")}
        for bid in ids:
            self._context_flushed.add((sid, bid))
            self._queue_context(sid, block_id=bid, promote=False)
        self._promote_context()

    def _promote_context(self) -> None:
        if not self._context_enabled():
            self._context_waiting.clear()
            self._context_rescan.clear()
            return
        if self._waiting_blocks:
            return
        if len(self._context_waiting) < CONTEXT_QUEUE_LIMIT and self._context_rescan:
            sid, bid = self._context_rescan.pop()
            self._queue_context(sid, block_id=bid, promote=False)
        while self._context_waiting and len(self._tasks) < ACTIVE_REVIEW_LIMIT:
            available = next((k for k in self._context_waiting if k not in self._tasks), None)
            if available is None:
                break
            self._context_waiting.pop(available)
            self._tasks[available] = asyncio.create_task(self._scheduled_context(available),
                                                        name=f"context-window-{available[3]}")

    async def _scheduled_context(self, key: tuple) -> None:
        sid, bid, _, uid = key
        digest = None
        try:
            await asyncio.sleep(WINDOW_DEBOUNCE_SEC)
            if not self._context_enabled() or not self._context_target_allowed(
                    self.rt.repo.get_utterance(uid) or {}, self.rt.repo.get_block(bid)):
                return
            snapshot = take_window_snapshot(self.rt.repo, sid, bid, [uid], context_radius=CONTEXT_RADIUS)
            digest = snapshot_hash(snapshot)
            if self._context_seen.get(key) == digest:
                return
            for saved in self.rt.repo.latest_window_transcript_reviews(sid, bid):
                scope = (saved.get("payload") or {}).get("scope") or {}
                if (scope.get("context_radius") == CONTEXT_RADIUS and saved.get("input_hash") == digest
                        and saved.get("status") in {"draft", "held", "accepted", "failed", "timeout", "unavailable", "rejected"}):
                    self._context_seen[key] = digest
                    return
            result = await self.review(sid, bid, wait=True, utterance_ids=[uid],
                              context_ids=snapshot["scope"]["context_ids"], context_radius=CONTEXT_RADIUS)
            # 실패한 동일 입력도 재통지마다 모델을 반복 호출하지 않는다.
            if result.get("status") != "stale":
                self._context_seen[key] = digest
        except (KeyError, ValueError):
            pass
        finally:
            self._tasks.pop(key, None)
            if self._context_waiting.get(key) == digest:
                self._context_waiting.pop(key, None)
            self._promote_closed()

    def schedule(self, sid: str, bid: int, after: Callable | None = None) -> bool:
        """활성 작업 2개를 유지하고 종료 질의는 유한 대기열에서 먼저 처리한다."""
        if self._closed or not self.enabled() or not settings.get(self.rt.repo, "transcript_review").get("auto_block"):
            return False
        key = (sid, bid)
        existing = key in self._tasks or key in self._waiting_blocks
        if not existing and len(self._tasks) >= ACTIVE_REVIEW_LIMIT and len(self._waiting_blocks) >= CLOSED_QUEUE_LIMIT:
            self._defer_block(key, "종료 검토 대기 한도를 넘겨 기존 요약 경로로 진행했습니다")
            return False
        if after is not None and after not in self._callbacks[key]:
            self._callbacks[key].append(after)
        self._block_versions[key] = self._block_versions.get(key, 0) + 1
        self._deferred_blocks.pop(key, None)
        if existing:
            return True
        self._waiting_blocks[key] = None
        self._promote_closed()
        return True

    async def _scheduled(self, sid: str, bid: int) -> None:
        key = (sid, bid)
        try:
            await asyncio.sleep(CLOSED_DEBOUNCE_SEC)
            for attempt in range(CLOSED_RETRY_LIMIT + 1):
                version = self._block_versions.get(key)
                result = await self.review(sid, bid, wait=True)
                if self._closed:
                    return
                changed = version != self._block_versions.get(key) or result.get("status") == "stale"
                if not changed:
                    break
                if attempt == CLOSED_RETRY_LIMIT:
                    self._defer_block(key, "검토 중 입력 변경이 이어져 최신 원문 재검토를 보류했습니다")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._defer_block(key, "종료 검토를 완료하지 못해 기존 요약 경로로 진행했습니다")
            log.warning("질의 종료 검토를 완료하지 못했습니다")
        finally:
            self._tasks.pop(key, None)
            self._block_versions.pop(key, None)
            callbacks = self._callbacks.pop(key, [])
            self._promote_closed()
            if not self._closed:
                for callback in callbacks:
                    try:
                        callback()
                    except Exception:
                        log.warning("질의 종료 후속 처리를 완료하지 못했습니다")

    def schedule_window(self, sid: str, bid: int, ids: list[int], source_key: str) -> bool:
        """한 원본 조각의 반복 통지는 모아 처리하고 질의 종료를 기다리지 않는다."""
        if not 1 <= len(ids) <= 6:
            return False
        return self._schedule_source_windows(sid, bid, ids, source_key)

    def _schedule_source_windows(self, sid: str, bid: int, ids: list[int], source_key: str) -> bool:
        if self._closed or not self.enabled() or not settings.get(self.rt.repo, "transcript_review").get("auto_block"):
            return False
        if not 1 <= len(ids) <= 64:
            return False
        key = (sid, bid, "window", source_key)
        self._window_targets[key] = tuple(ids)
        if key in self._tasks:
            return True
        if self._waiting_blocks or len(self._tasks) >= ACTIVE_REVIEW_LIMIT:
            self._window_targets.pop(key, None)
            return False
        task = asyncio.create_task(self._scheduled_window(key), name=f"speaker-window-{bid}")
        self._tasks[key] = task
        return True

    async def _scheduled_window(self, key: tuple) -> None:
        sid, bid, _, _ = key
        used: tuple[int, ...] = ()
        retry = False
        try:
            await asyncio.sleep(WINDOW_DEBOUNCE_SEC)
            used = self._window_targets.get(key, ())
            count = (len(used) + 5) // 6
            size, extra = divmod(len(used), count)
            offset = 0
            for index in range(count):
                width = size + int(index < extra)
                ids = list(used[offset:offset + width])
                offset += width
                snapshot = take_window_snapshot(self.rt.repo, sid, bid, ids)
                digest = snapshot_hash(snapshot)
                cache_key = (*key, tuple(ids))
                if self._window_seen.get(cache_key) == digest:
                    continue
                scope = snapshot["scope"]
                result = await self.review(sid, bid, wait=True, utterance_ids=ids, context_ids=scope["context_ids"])
                retry = retry or result.get("status") == "stale"
                if result.get("status") in {"draft", "held", "accepted"}:
                    self._window_seen[cache_key] = digest
                    while len(self._window_seen) > 128:
                        self._window_seen.pop(next(iter(self._window_seen)))
        except (KeyError, ValueError):
            pass  # 대기 중 삭제·재분할이 생기면 다음 확정 통지를 기다린다.
        finally:
            self._tasks.pop(key, None)
            latest = self._window_targets.pop(key, ())
            self._promote_closed()
            if not self._closed and latest and (latest != used or retry):
                self._schedule_source_windows(sid, bid, list(latest), key[3])

    def _snapshot(self, sid: str, bid: int, scope: dict | None = None) -> dict:
        if scope is not None and not isinstance(scope, dict):
            raise ValueError("검토 범위가 잘못되었습니다")
        if scope and scope.get("kind") == "window":
            if set(scope) - {"kind", "utterance_ids", "context_ids", "context_radius"}:
                raise ValueError("검토 범위에 허용하지 않은 필드가 있습니다")
            return take_window_snapshot(self.rt.repo, sid, bid, scope.get("utterance_ids") or [],
                                        context_ids=scope.get("context_ids") or [],
                                        context_radius=scope.get("context_radius", 1))
        if scope and scope.get("kind") not in {None, "block"}:
            raise ValueError("검토 범위가 잘못되었습니다")
        return take_snapshot(self.rt.repo, sid, bid)

    def _publish_review(self, sid: str, review: dict) -> None:
        payload = review.get("payload") or {}
        metrics = payload.get("metrics") or {}
        self.rt.bus.publish(sid, "transcript.review", {"id": review["id"], "block_id": review["block_id"],
            "status": review["status"], "scope": payload.get("scope"),
            "metrics": {name: metrics.get(name, 0) for name in ("changed_turns", "held_utterances")}})
        # 열람자·클라우드도 창 초안의 저장·거절·무효화를 같은 공개 판본으로 받는다.
        scope = payload.get("scope")
        if isinstance(scope, dict) and scope.get("kind") == "window":
            from .review_display import get_block_display
            block = self.rt.repo.get_block(review["block_id"])
            if block:
                self.rt.bus.publish(sid, "block.upsert", get_block_display(self.rt.repo, block))

    async def review(self, sid: str, bid: int, *, wait: bool = False,
                     utterance_ids: list[int] | None = None, context_ids: list[int] | None = None,
                     context_radius: int = 1) -> dict:
        """같은 입력의 채택 판본은 재판정하지 않는다. 실패도 제한된 초안으로 기록한다."""
        if self._closed or not self.enabled():
            return {"status": "unavailable", "detail": "Jev 화자 검토가 꺼져 있습니다"}
        if self._lock.locked() and not wait:
            return {"status": "busy", "detail": "다른 화자 검토가 진행 중입니다"}
        async with self._lock:
            if self._closed:
                return {"status": "unavailable", "detail": "화자 검토 서비스가 종료되었습니다"}
            snapshot = (take_window_snapshot(self.rt.repo, sid, bid, utterance_ids, context_ids=context_ids,
                                             context_radius=context_radius)
                        if utterance_ids is not None else take_snapshot(self.rt.repo, sid, bid))
            scope = snapshot.get("scope")
            digest = snapshot_hash(snapshot)
            accepted = self.rt.repo.accepted_transcript_review_by_hash(sid, bid, digest)
            if accepted is not None:
                return accepted
            try:
                if scope and scope.get("kind") == "window":
                    options = {"context_radius": context_radius} if context_radius in {2, 3} else {}
                    job = self.engine.review_window(sid, bid, scope["utterance_ids"],
                                                    context_ids=scope["context_ids"], **options)
                else:
                    job = self.engine.review_block(sid, bid)
                payload = await asyncio.wait_for(job, timeout=20)
            except asyncio.CancelledError:
                raise
            except Exception:
                payload = {"status": "failed", "detail": "화자 검토를 완료하지 못했습니다", "turns": []}
            if payload.get("input_hash") not in {None, digest}:
                payload.update(status="stale", detail="검토한 입력 판본이 달라졌습니다")
            payload["input_hash"] = digest
            if scope:
                payload["scope"] = scope
            else:
                payload.pop("scope", None)
            if context_radius in {2, 3}:
                payload = await self._correct_context(snapshot, payload)
            if self.rt.repo.get_session(sid) is None or self.rt.repo.get_block(bid) is None:
                return {"status": "stale", "detail": "검토 중 기록이 삭제되었습니다"}
            try:
                current_digest = snapshot_hash(self._snapshot(sid, bid, scope))
            except (KeyError, ValueError):
                current_digest = ""
            if current_digest != digest:
                payload.update(status="stale", detail="검토 중 원문 또는 검수 상태가 바뀌었습니다")
            if context_radius in {2, 3} and not self._context_enabled():
                payload.update(status="stale", detail="1차 문맥 보정 중 자동 검토가 중지되었습니다")
            if payload.get("status") == "draft":
                try:
                    validate_result(snapshot, payload.get("turns") or [])
                except (ValueError, KeyError, TypeError):
                    payload.update(status="held", detail="원문 범위·수동 수정 보호 검증을 통과하지 못했습니다")
            saved = self.rt.repo.save_transcript_review(sid, bid, payload)
            self._publish_review(sid, saved)
            return saved

    async def _correct_context(self, snapshot: dict, payload: dict) -> dict:
        """화자 판정 실패와 본문 교정을 구별하며 기존 로컬 제공자만 이용한다."""
        from .context_window_correction import correct_context_window
        from .llm.providers import LocalMLXProvider, LLMRequestStale
        from .transcript_review import _original_turn
        sid, bid = snapshot["session_id"], snapshot["block"]["id"]
        try:
            current = self._snapshot(sid, bid, snapshot["scope"])
            current_target = self.rt.repo.get_utterance(snapshot["utterances"][0]["id"]) or {}
            current_matches = (snapshot_hash(current) == snapshot_hash(snapshot)
                               and self._context_target_allowed(current_target, self.rt.repo.get_block(bid)))
        except (KeyError, ValueError):
            current_matches = False
        if not self._context_enabled() or not current_matches or payload.get("status") == "stale":
            return {**payload, "status": "stale", "detail": "1차 본문 보정 전에 입력·보호 상태가 바뀌었습니다"}
        turns = payload.get("turns") or []
        try:
            turns = validate_result(snapshot, turns)
        except (ValueError, TypeError, KeyError):
            turns = [_original_turn(row) for row in snapshot["utterances"]]
        provider = self._context_provider
        if provider is None:
            analysis = getattr(self.rt, "analysis", None)
            cfg = settings.get(self.rt.repo, "llm") or {}
            if (cfg.get("provider") != "local_mlx" or getattr(getattr(self.rt, "config", None), "disable_heavy_engines", False)
                    or analysis is None or not hasattr(analysis, "_get_provider")):
                payload["context_correction"] = {"status": "unavailable", "stage": "realtime_context",
                    "source_hash": snapshot_hash(snapshot), "detail": "로컬 본문 모델을 사용할 수 없어 화자 초안만 유지합니다"}
                return payload
            provider, _ = await analysis._get_provider()
            if not isinstance(provider, LocalMLXProvider):
                return payload
        def current_request() -> bool:
            try:
                return bool(self._context_enabled()
                    and snapshot_hash(self._snapshot(sid, bid, snapshot["scope"])) == snapshot_hash(snapshot)
                    and self._context_target_allowed(self.rt.repo.get_utterance(snapshot["utterances"][0]["id"]) or {},
                                                    self.rt.repo.get_block(bid)))
            except (KeyError, ValueError):
                return False

        class ContextProvider:
            async def complete(inner, *args, **kwargs):
                if isinstance(provider, LocalMLXProvider):
                    # 기존 전체 질의의 모델 잠금 대기는 본문 생성 예산에서 제외한다.
                    return await provider.complete(*args, **kwargs, execution_timeout=CONTEXT_TEXT_TIMEOUT_SEC,
                                                   request_guard=current_request)
                if not current_request():
                    raise LLMRequestStale("1차 본문 요청 전에 입력·자동 검토 상태가 바뀌었습니다")
                return await asyncio.wait_for(provider.complete(*args, **kwargs), timeout=CONTEXT_TEXT_TIMEOUT_SEC)
        try:
            correction = await correct_context_window(snapshot, turns, ContextProvider())
            payload["context_correction"] = correction
            payload.setdefault("metrics", {})["semantic_text_changes"] = sum(
                len(row["changes"]) for row in correction["rows"])
            payload["speaker_review_status"] = payload.get("status")
            if payload.get("status") != "stale":
                payload.update(status="draft", turns=turns)
        except asyncio.CancelledError:
            raise
        except LLMRequestStale:
            payload.update(status="stale", detail="본문 모델 대기 중 입력·자동 검토 상태가 바뀌어 호출하지 않았습니다")
        except Exception:
            payload["context_correction"] = {"status": "held", "stage": "realtime_context",
                "source_hash": snapshot_hash(snapshot), "detail": "문맥 본문 교정이 시간 또는 근거 검증 한도에서 보류됐습니다"}
        return payload

    def _suspect_window(self, rows: list[dict], u: dict) -> tuple[list[int], str]:
        """의심 조각 앞의 같은 원 발언도 최대 6행까지만 함께 검토한다."""
        source = str(u.get("source_key") or "").split("~", 1)[0]
        ids = [r["id"] for r in rows if str(r.get("source_key") or "").split("~", 1)[0] == source]
        turn = u.get("turn_id")
        if not u.get("review_required") or not turn or len(ids) > 6:
            return ids, source
        positions = {r["id"]: i for i, r in enumerate(rows)}
        if not ids or u.get("id") not in positions or any(uid not in positions for uid in ids):
            return ids, source
        end = max(positions[uid] for uid in ids)
        start = min(positions[uid] for uid in ids)
        # 직접 단서가 없는 앞 조각을 운영 원문에서 다시 이름 붙이지 않는다.
        # 같은 턴도 수동 수정·채택 판본·시각 역전 앞에서는 검토 대상을 넓히지 않는다.
        def eligible(row: dict, *, sibling: bool = False) -> bool:
            return bool((row.get("turn_id") == turn or sibling) and row.get("speaker_source") != "manual"
                        and row.get("speaker_role") != "system" and not row.get("review_parts")
                        and not row.get("edited") and not row.get("edited_by")
                        and (row.get("original_text") is None or row.get("text") == row.get("original_text")))
        if not all(eligible(row) for row in rows[start:end + 1]):
            return ids, source
        while start > 0 and end - start + 1 < 6:
            before, current = rows[start - 1], rows[start]
            span = rows[end]["ts_start"] - before["ts_start"]
            gap = current["ts_start"] - before["ts_start"]
            if not eligible(before) or not 0 <= gap <= 90_000 or not 0 <= span <= LIVE_WINDOW_SPAN_MS:
                break
            start -= 1
        # 앞 제한 조각의 원 segment가 여러 색으로 갈렸어도 그 형제를 빠뜨리지 않는다.
        while start > 0 and end - start + 1 < 6:
            before, current = rows[start - 1], rows[start]
            prefix = str(current.get("source_key") or "").split("~", 1)[0]
            span = rows[end]["ts_start"] - before["ts_start"]
            gap = current["ts_start"] - before["ts_start"]
            if (not prefix or str(before.get("source_key") or "").split("~", 1)[0] != prefix
                    or not eligible(before, sibling=True) or not 0 <= gap <= 90_000
                    or not 0 <= span <= LIVE_WINDOW_SPAN_MS):
                break
            start -= 1
        selected = [row["id"] for row in rows[start:end + 1]]
        return selected, f"turn:{turn}" if selected != ids else source

    def _covered_window(self, sid: str, block: dict, rows: list[dict], u: dict) -> tuple[list[int], str] | None:
        """기존 확장창에 포함된 원 segment의 늦은 확정만 같은 작업으로 다시 모은다."""
        summary = block.get("summary")
        if block.get("review_status") == "reviewed" or isinstance(summary, dict) and summary.get("edited_by"):
            return None
        prefix = (sid, block["id"], "window")
        known = {key[:4]: key[4] for key in self._window_seen
                 if key[:3] == prefix and str(key[3]).startswith("turn:")}
        known.update({key: targets for key, targets in self._window_targets.items()
                      if key[:3] == prefix and str(key[3]).startswith("turn:")})
        by_id = {row["id"]: row for row in rows}
        positions = {row["id"]: index for index, row in enumerate(rows)}
        source = str(u.get("source_key") or "").split("~", 1)[0]
        for key, targets in reversed(list(known.items())):
            sources = {str(by_id[uid].get("source_key") or "").split("~", 1)[0]
                       for uid in targets if uid in by_id}
            sources.discard("")
            if u.get("id") not in targets and (not source or source not in sources):
                continue
            selected = [row for row in rows if row["id"] in targets
                        or str(row.get("source_key") or "").split("~", 1)[0] in sources]
            if not selected:
                return None
            interval = rows[positions[selected[0]["id"]]:positions[selected[-1]["id"]] + 1]
            # 같은 원 segment의 새 형제만 포함하고 외부 발언을 건너 창을 잇지 않는다.
            if len(interval) != len(selected):
                return None
            selected = interval
            span = selected[-1]["ts_start"] - selected[0]["ts_start"]
            if (len(selected) > 6 or not 0 <= span <= LIVE_WINDOW_SPAN_MS
                    or any(not 0 <= right["ts_start"] - left["ts_start"] <= 90_000
                           for left, right in zip(selected, selected[1:]))
                    or any(row.get("speaker_source") == "manual" or row.get("speaker_role") == "system"
                           or row.get("review_parts") or row.get("edited") or row.get("edited_by")
                           or row.get("original_text") is not None and row.get("text") != row["original_text"]
                           for row in selected)):
                return None
            return [row["id"] for row in selected], key[3]
        return None

    def note_utterance(self, sid: str, u: dict) -> None:
        """미상·혼합 의심 발언만 원음 후보를 남긴다. 수집 lock에서 기다리지 않는다."""
        from .transcript_review import boundary_candidates
        if self._context_windows and u.get("is_final") and u.get("speaker_role") != "system":
            self._queue_context(sid, changed_uid=u.get("id"))
        block = self.rt.repo.get_block(u.get("block_id") or 0)
        # 늦은 확정의 revise_speaker는 Decision 통지를 거치지 않으므로 본문 근거도 확인한다.
        structural_mix = bool(block and u.get("speaker_role") == "unknown" and boundary_candidates(
            {"block": block, "source_boundaries": {}}, u))
        source = str(u.get("source_key") or "").split("~", 1)[0]
        source_job = (sid, block["id"], "window", source) if block else None
        pending_source = bool(source and source_job and (source_job in self._tasks
                              or any(key[:4] == source_job for key in self._window_seen)))
        expanded = bool(block and any(key[:3] == (sid, block["id"], "window") and str(key[3]).startswith("turn:")
                        for key in (*self._window_targets, *self._window_seen)))
        needs_rows = u.get("boundary_reason") == "uncertain_marker" or u.get("review_required") or structural_mix or pending_source or expanded
        rows = (self.rt.repo.list_utterances(sid, block_id=block["id"], final_only=True)
                if block and block.get("is_open") and needs_rows else [])
        covered = self._covered_window(sid, block, rows, u) if rows else None
        if not self._context_windows and not self._closed and u.get("is_final") and (u.get("boundary_reason") == "uncertain_marker"
                or u.get("review_required") or structural_mix or pending_source or covered):
            if block and block.get("is_open"):
                ids, source = covered or self._suspect_window(rows, u)
                self._schedule_source_windows(sid, block["id"], ids, source)
        audio = getattr(self.rt, "audio_review", None)
        if self._closed or audio is None or len(self._audio_tasks) >= 2:
            return
        if not block:
            return
        snap = {"block": block, "source_boundaries": {}}
        suspect = u.get("speaker_role") == "unknown" or bool(boundary_candidates(snap, u))
        if not suspect or "buffer_bytes" not in audio.status(sid):
            return
        end = max(u["ts_start"] + 1000, u.get("ts_end") or u["ts_start"] + 1000)
        clip_ms = max(1, min(30, int(settings.get(self.rt.repo, "audio_review").get("max_clip_seconds") or 30))) * 1000
        start = max(u["ts_start"], end - clip_ms)
        task = asyncio.create_task(self.capture_audio(sid, u["id"], start, end))
        self._audio_tasks.add(task)
        task.add_done_callback(self._audio_done)

    async def capture_audio(self, sid: str, uid: int, start: int, end: int) -> dict:
        result = await self.rt.audio_review.retain(sid, start, end, "미상·혼합 화자 검토 후보", uid)
        current = self.rt.repo.get_utterance(uid)
        if result.get("clip_path") and current and current["session_id"] == sid:
            self.rt.repo.save_audio_review_evidence(sid, uid, result)
        return result

    def _audio_done(self, task: asyncio.Task) -> None:
        self._audio_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # 실패가 자막 처리에 전파되지 않게 소비한다.

    def _ready_summary(self, review: dict, snapshot: dict, turns: list[dict]) -> dict | None:
        """완성 초안의 원문·표시 조립을 다시 확인한 뒤 같은 판본의 요약만 돌려준다."""
        payload = review.get("payload") or {}
        presentation = payload.get("presentation")
        if isinstance(presentation, dict) and presentation.get("final_document") is not None:
            raise ValueError("전체 음성 최종 초안은 비교·검수용이며 기존 화자 채택으로 본문·요약을 함께 채택할 수 없습니다")
        if not isinstance(presentation, dict) or presentation.get("ready") is not True:
            return None
        from .review_presentation import build_presentation
        scope = payload.get("scope") or {}
        block = snapshot["block"]
        summary = presentation.get("summary")
        if (scope and (not isinstance(scope, dict) or scope.get("kind") != "block" or set(scope) - {"kind"})
                or block.get("kind") != "question" or block.get("review_status") == "reviewed"
                or block.get("edited_summary") or type(presentation.get("schema_version")) is not int
                or presentation["schema_version"] != 1 or not isinstance(summary, dict)
                or summary.get("edited_by") or not isinstance(presentation.get("editor"), dict)):
            raise ValueError("완성된 질의 전체 초안의 요약만 함께 채택할 수 있습니다")
        digest = snapshot_hash(snapshot)
        if any(value != digest for value in (payload.get("input_hash"), presentation.get("input_hash"),
                                             summary.get("input_hash", digest))):
            raise ValueError("화자와 요약의 원문 판본이 달라졌습니다")
        rebuilt = build_presentation(snapshot, turns)
        if any(presentation.get(key) != rebuilt[key] for key in ("cards", "source_rows", "readability_report")):
            raise ValueError("완성 초안의 표시 본문과 원문 범위가 다릅니다")
        from .review_display import accepted_question_ownership, validate_question_ownership
        validate_question_ownership(self.rt.repo, snapshot, turns, presentation)
        adopted = {**deepcopy(summary), "input_source": "accepted_review", "review_id": review["id"],
                   "input_hash": digest}
        if "ownership" in presentation:
            adopted["question_ownership"] = accepted_question_ownership(presentation["ownership"], review["id"], digest)
        return adopted

    def _display_changed(self, sid: str, bid: int) -> None:
        """통합 초안의 채택·해제 뒤 회의 전체 요약 갱신을 알린다."""
        changed = getattr(self.rt.analysis, "on_review_display_changed", None)
        if callable(changed):
            changed(sid, bid)

    async def accept(self, sid: str, rid: int, actor: str) -> dict:
        """수집 lock에서 입력을 다시 확인하고 완성 초안은 같은 판본의 요약도 함께 채택한다."""
        async with self.rt.pipeline(sid).lock:
            repo = self.rt.repo
            with repo.db.transaction():
                review = repo.get_transcript_review(rid)
                if not review or review["session_id"] != sid:
                    raise KeyError("화자 검토를 찾을 수 없습니다")
                if review["status"] == "accepted":
                    return review
                if review["status"] != "draft":
                    raise ValueError("검증된 화자 초안만 채택할 수 있습니다")
                scope = (review.get("payload") or {}).get("scope")
                snap = self._snapshot(sid, review["block_id"], scope)
                if snapshot_hash(snap) != review["input_hash"]:
                    raise ValueError("원문이나 검수 상태가 바뀌었습니다. 다시 검토하세요")
                turns = review["payload"].get("turns") or []
                validate_result(snap, turns)
                prepared = (review.get("payload") or {}).get("presentation") or {}
                if prepared.get("ready") and prepared.get("processing_hash") is not None:
                    from .review_context import processing_signature
                    if prepared["processing_hash"] != processing_signature(repo):
                        raise ValueError("정리 후 처리 설정이 바뀌었습니다. 다시 검토하세요")
                ready_summary = self._ready_summary(review, snap, turns)
                block = repo.get_block(review["block_id"])
                if ready_summary is not None and (not block or block.get("is_open")):
                    raise ValueError("종료된 질의 전체의 완성 초안만 함께 채택할 수 있습니다")
                grouped = defaultdict(list)
                for part in turns:
                    stored = {k: part[k] for k in ("start", "end", "role", "speaker") if k in part}
                    if part.get("group_id"):
                        stored["group_id"] = f"r{rid}:{part['group_id']}"
                    grouped[part["utterance_id"]].append(stored)
                affected = repo.supersede_transcript_reviews(sid, review["block_id"], rid, target_ids=set(grouped))
                for uid, parts in grouped.items():
                    repo.update_utterance(uid, review_parts=parts)
                if ready_summary is not None:
                    repo.update_block(review["block_id"], summary=ready_summary, summary_at=now_ms())
                repo.set_transcript_review_status(rid, "accepted")
                repo.audit(actor, "transcript.review.accept", sid, {"review_id": rid, "block_id": review["block_id"]})
            affected.update(row["id"] for row in repo.take_review_invalidations(sid))
            for uid in affected | set(grouped):
                row = repo.get_utterance(uid)
                if row:
                    self.rt.bus.publish(sid, "utterance.upsert", serialize.utterance(row))
            for invalid in repo.take_review_state_invalidations(sid):
                self._publish_review(sid, invalid)
            self._publish_review(sid, repo.get_transcript_review(rid))
            from .review_display import get_block_display
            self.rt.bus.publish(sid, "block.upsert", get_block_display(repo, repo.get_block(review["block_id"])))
            if ready_summary is None:
                self.rt.analysis.on_review_accepted(sid, review["block_id"])
            else:
                self._display_changed(sid, review["block_id"])
            return repo.get_transcript_review(rid)

    async def reject(self, sid: str, rid: int, actor: str) -> dict:
        async with self.rt.pipeline(sid).lock:
            review = self.rt.repo.get_transcript_review(rid)
            if not review or review["session_id"] != sid:
                raise KeyError("화자 검토를 찾을 수 없습니다")
            presentation = (review.get("payload") or {}).get("presentation")
            ready = isinstance(presentation, dict) and presentation.get("ready") is True
            if review["status"] == "accepted":
                with self.rt.repo.db.transaction():
                    ids = self.rt.repo.review_target_ids(review)
                    for uid in ids:
                        self.rt.repo.update_utterance(uid, review_parts=[])
                    block = self.rt.repo.get_block(review["block_id"])
                    summary = (block or {}).get("summary")
                    if (ready and block and block.get("review_status") != "reviewed"
                            and isinstance(summary, dict) and not summary.get("edited_by")
                            and summary.get("input_source") == "accepted_review"
                            and summary.get("review_id") == rid and summary.get("input_hash") == review["input_hash"]):
                        self.rt.repo.update_block(review["block_id"], summary=None, summary_at=None)
                    self.rt.repo.set_transcript_review_status(rid, "rejected")
                for uid in ids:
                    row = self.rt.repo.get_utterance(uid)
                    if row:
                        self.rt.bus.publish(sid, "utterance.upsert", serialize.utterance(row))
                self.rt.analysis.on_review_accepted(sid, review["block_id"])
            elif review["status"] in {"draft", "held", "failed", "unavailable", "timeout", "busy"}:
                self.rt.repo.set_transcript_review_status(rid, "rejected")
                if ready:
                    self.rt.analysis.on_review_accepted(sid, review["block_id"])
            else:
                raise ValueError("이 판본은 이미 바뀌었거나 보류 처리되었습니다")
        self.rt.repo.audit(actor, "transcript.review.reject", sid, {"review_id": rid})
        result = self.rt.repo.get_transcript_review(rid)
        self._publish_review(sid, result)
        if ready:
            from .review_display import get_block_display
            block = self.rt.repo.get_block(review["block_id"])
            if block:
                self.rt.bus.publish(sid, "block.upsert", get_block_display(self.rt.repo, block))
            self._display_changed(sid, review["block_id"])
        return result

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values()) + list(self._audio_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._callbacks.clear()
        self._waiting_blocks.clear()
        self._block_versions.clear()
        self._deferred_blocks.clear()
        self._window_targets.clear()
        self._window_seen.clear()
        self._context_waiting.clear()
        self._context_seen.clear()
        self._context_rescan.clear()
        self._context_flushed.clear()
        self._audio_tasks.clear()
        async with self._lock:
            provider = getattr(self.engine, "provider", None)
            if provider is not None:
                await provider.close()
