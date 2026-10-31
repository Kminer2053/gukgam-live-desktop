"""수집 PC 이벤트를 클라우드 미러에 묶어 보내고 세대 백필로 복구한다."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from . import serialize, settings
from . import __version__
from .db import now_ms
from .events import Event
from .stage_mirror import public_source, public_document

log = logging.getLogger(__name__)
DAY_MS = 86_400_000
EVENT_KINDS = {"utterance.upsert": "utterance", "block.upsert": "block", "item.upsert": "item",
               "alert": "alert", "summary.update": "summary", "session.status": "session"}


def redact(kind: str, data: Any, *, sensitive: bool = False) -> Any:
    """원본을 깊게 복사하고 민감 필드는 모든 전송 경로에서 같은 방식으로 비운다."""
    out = copy.deepcopy(data)
    if not sensitive and isinstance(out, dict):
        if kind == "item":
            out.update(assignee_dept="", note="")
        elif kind == "settings_public":
            out["departments"] = []
    return out


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


class CloudSync:
    """환경변수 두 값이 있을 때만 켜지는 독립 동기화 수명주기."""

    def __init__(self, rt, app=None, *, client: httpx.AsyncClient | None = None, transport=None,
                 batch_interval: float = 0.5, queue_limit: int = 20_000, max_batch_ops: int = 200,
                 max_batch_bytes: int | None = None, max_batch_write_models: int = 100,
                 active_heartbeat_s: float = 15,
                 idle_heartbeat_s: float = 300, backfill_interval_s: float = 1800,
                 retry_initial_s: float = 1, retry_max_s: float = 30,
                 command_wait_s: float = 20, command_timeout_s: float = 55,
                  command_poll_retry_s: float = 1, command_result_retry_s: float = 1,
                  connection_allowed=None):
        self.rt, self.app, self.config = rt, app, rt.config
        self.read_mode = getattr(self.config, "cloud_read_mode", "mirror")
        if self.read_mode not in {"mirror", "source"}:
            raise ValueError("클라우드 열람 방식은 mirror 또는 source여야 합니다")
        self._connection_allowed = connection_allowed
        self.enabled = bool(self.config.cloud_url and self.config.agent_token)
        self.batch_interval, self.queue_limit = batch_interval, queue_limit
        self.max_batch_ops = max_batch_ops
        self.max_batch_bytes = 512_000 if max_batch_bytes is None else max_batch_bytes
        # 기본 묶음만 줄인다. 쪼갤 수 없는 기존 단일 문서는 API의 2MB 안에서 고립 전송한다.
        self._single_document_bytes = 2_000_000 if max_batch_bytes is None else max_batch_bytes
        if type(max_batch_write_models) is not int or max_batch_write_models < 3:
            raise ValueError("클라우드 쓰기 모델 예산은 3 이상의 정수여야 합니다")
        self.max_batch_write_models = max_batch_write_models
        self.active_heartbeat_s, self.idle_heartbeat_s = active_heartbeat_s, idle_heartbeat_s
        self.backfill_interval_s = backfill_interval_s
        self.retry_initial_s, self.retry_max_s = retry_initial_s, retry_max_s
        self.command_wait_s, self.command_timeout_s = command_wait_s, command_timeout_s
        self.command_poll_retry_s, self.command_result_retry_s = command_poll_retry_s, command_result_retry_s
        self._command_runner = None
        self.boot_id, self.seq = uuid.uuid4().hex, 0
        self.needs_backfill: set[str] = set()
        self._source_resync: set[str] = set()
        self._queue: deque[dict] = deque()
        self._queue_bytes = 0
        self._expanding_count = 0
        self._pending: dict | None = None
        self._send_suffix: deque[list[dict]] = deque()
        self._send_plan_invalid = False
        self._backfill_cursor = None
        self._backfill_scope: str | None = None
        self._backfill_retry_marker = None
        self._completed_backfill_cursor = None
        self._client, self._transport = client, transport
        self._own_client = client is None
        self._send_lock, self._backfill_lock = asyncio.Lock(), asyncio.Lock()
        self._wake, self._backfill_wake, self._state_wake = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._loop = None
        self._thread_id = None
        self._running = False
        self._wanted, self._online = False, False
        self.mode = "idle"
        self.last_push_ok_ms = 0
        self.last_error = ""
        self._session_types: dict[str, str] = {}
        self._generation = int(self.boot_id[:15], 16)
        self._last_backfill = 0.0
        self._live_turn_cache: dict[str, dict[str, dict]] = {}
        self._source_guess_cache: dict[str, dict[str, dict]] = {}
        self._read_executor = None

    @property
    def enabled(self) -> bool:
        if not self._enabled:
            return False
        try:
            return self._connection_allowed is None or bool(self._connection_allowed())
        except Exception:
            return False

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    @property
    def command_runner(self):
        """로컬 앱이 있는 수집 PC에서만 명령 실행기를 지연 생성한다."""
        if self._command_runner is None:
            from .cloudcommands import CommandRunner
            self._command_runner = CommandRunner(self)
        return self._command_runner

    async def execute_command(self, cmd: dict) -> dict | None:
        if not self.enabled or self.app is None:
            return None
        return await self.command_runner.execute_command(cmd)

    async def poll_commands(self, wait: float = 20) -> list[dict]:
        if not self.enabled or self.app is None:
            return []
        return await self.command_runner.poll_commands(wait)

    async def dispatch_commands(self, commands: list[dict]) -> None:
        if self.enabled and self.app is not None:
            await self.command_runner.dispatch_commands(commands)

    def status(self) -> dict:
        return {"enabled": self.enabled, "online": self._online, "mode": self.mode, "read_mode": self.read_mode,
                "connection_id": self.boot_id,
                "last_push_ok_ms": self.last_push_ok_ms,
                "queue": len(self._queue) + len(self._source_resync) + self._expanding_count + len(self._pending["ops"] if self._pending else []),
                "backfill_in_progress": self._backfill_cursor is not None,
                "backfill_remaining_packets": len(self._send_suffix) + int(self._pending is not None),
                "needs_backfill_count": len(self.needs_backfill),
                "last_error": self.last_error}

    def _sessions(self) -> list[dict]:
        repo = getattr(self.rt, "repo", None)
        return repo.list_sessions(limit=1_000_000) if repo is not None else []

    def _recent(self, session: dict, cutoff: int) -> bool:
        """회의 메타와 화면에 올릴 하위 기록의 최근 수정 시각을 함께 확인한다."""
        if (session.get("updated_at") or session.get("created_at") or 0) >= cutoff:
            return True
        repo, sid = self.rt.repo, session["id"]
        for read, kwargs in ((repo.list_utterances, {"limit": 1_000_000}), (repo.list_blocks, {}),
                             (repo.list_items, {"include_dismissed": True, "limit": 1_000_000})):
            if any((row.get("updated_at") or row.get("created_at") or 0) >= cutoff for row in read(sid, **kwargs)):
                return True
        return any(row and row["created_at"] >= cutoff
                   for scope in ("session", "rolling") if (row := repo.latest_summary(sid, scope)))

    def mode_update(self) -> str:
        self.mode = "active" if self.read_mode == "source" or self._wanted or any(s["status"] == "live" for s in self._sessions()) else "idle"
        return self.mode

    def _gen(self) -> int:
        self._generation += 1
        return self._generation

    def _replay(self, sid: str, data: dict | None = None) -> bool:
        source = (data or {}).get("source_type")
        row = self.rt.repo.get_session(sid)
        if row is not None:
            source = row["source_type"]
        if source:
            self._session_types[sid] = source
        return self._session_types.get(sid) == "replay"

    def _redact(self, kind: str, data: Any) -> Any:
        return redact(kind, data, sensitive=self.config.cloud_mirror_sensitive)

    def _public_settings(self) -> dict:
        return self._redact("settings_public", {key: settings.get(self.rt.repo, key)
                                                for key in ("org", "watch_keywords", "departments")})

    def _roster(self, sid: str) -> dict:
        return {"members": [{"id": r["id"], "name": r["name"], "role": r.get("role") or "member",
                             "party": r.get("party") or "", "title": r.get("title") or "",
                             "aliases": r.get("aliases") or [],
                             "scope": "global" if r.get("session_id") is None else "session"}
                            for r in self.rt.repo.get_roster(sid)]}

    def event_op(self, channel: str, event: Event) -> dict | None:
        """원본 조회 모드는 공개 이벤트만 감싸 문서 자동 투영을 막는다."""
        if self.read_mode != "source":
            return self._mirror_event_op(channel, event)
        if not self.enabled:
            return None
        if event.type == "transcript.stage":
            if not channel or channel == "*" or self._replay(channel, event.data):
                return None
            identifier = event.data.get("id")
            if type(identifier) is not int or identifier <= 0:
                return None
            op = {"sid": channel, "type": event.type, "ts": now_ms(), "data": {"id": identifier}}
        else:
            op = self._mirror_event_op(channel, event)
        if op is None:
            return None
        data = {"id": channel} if op["type"] == "session.deleted" else op["data"]
        generation = event.data.get("display_input_generation")
        if type(generation) is int and 0 <= generation <= 9_007_199_254_740_991:
            data = {**data, "display_input_generation": generation}
        from .cloud import source_projection
        data = source_projection.event(op["type"], data, sensitive=self.config.cloud_mirror_sensitive)
        return {"op": "event", "sid": channel, "type": "source.event", "ts": op["ts"],
                "data": {"type": op["type"], "data": data}}

    def _mirror_event_op(self, channel: str, event: Event) -> dict | None:
        """채널을 잃지 않고 정렬 시각과 공개 데이터를 채운다."""
        if not self.enabled or not channel or channel == "*" or self._replay(channel, event.data):
            return None
        type_ = "session.status" if event.type == "session.created" else event.type
        if type_ == "caption.source":
            # 수신기가 이미 저장·정제한 이벤트는 동기 탭에서 DB를 다시 읽지 않는다.
            packet = event.data
            if (type(packet.get("id")) is int and type(packet.get("revision")) is int
                    and isinstance(packet.get("text"), str) and isinstance(packet.get("source_key"), str)
                    and type(packet.get("first_rx")) is int and "invalidated_document_ids" in packet):
                source = packet
                invalidated = packet.get("invalidated_document_ids") or []
            else:
                rows = self.rt.repo.caption_sources_by_ids(channel, [packet.get("id")])
                source = rows[0] if rows else None
                invalidated = (self.rt.repo.stage_invalidated_document_ids(channel, source["id"])
                               if source is not None else [])
            if source is None or source.get("replayed") is True:
                return None
            data = public_source(source)
            data["invalidated_document_ids"] = [did for did in invalidated if type(did) is int and did > 0]
            return {"op": "event", "sid": channel, "type": type_, "ts": int(data.get("first_rx") or 0),
                    "data": data, "_live_turn_reload": True}
        if type_ == "transcript.stage":
            row = self.rt.repo.get_stage_document(channel, event.data.get("id"))
            if row is None:
                return None
            return {"op": "event", "sid": channel, "type": type_, "ts": now_ms(), "data": {"id": row["id"]},
                    "_stage_reload": True}
        data = self._redact(EVENT_KINDS.get(type_, ""), event.data)
        ts = data.get("ts_start") or data.get("created_at") or now_ms()
        if type_ == "item.upsert":
            item = self.rt.repo.get_item_full(data.get("id"))
            ts = (item or {}).get("utt_ts") or (item or {}).get("created_at") or ts
        elif type_ in {"alert", "alert.delete"}:
            utterance = self.rt.repo.get_utterance(data.get("utterance_id"))
            ts = (utterance or {}).get("ts_start") or ts
        elif type_ == "summary.update":
            ts = (data.get("summary") or {}).get("created_at") or ts
        elif type_ == "stats":
            ts = data.get("ts_last") or ts
        op = {"op": "event", "sid": channel, "type": type_, "ts": int(ts), "data": data}
        if (type_ in {"utterance.upsert", "utterance.delete", "block.upsert", "session.status"}
                and hasattr(self.rt.repo, "has_caption_sources") and self.rt.repo.has_caption_sources(channel)):
            op["_stage_reload"] = True
        return op

    def _enqueue(self, op: dict) -> None:
        self._queue.append(op)
        self._queue_bytes += self._weight(op)
        self._trim_queue()
        size = self._queue_bytes + len(_bytes(self._packet([]))) + max(0, len(self._queue) - 1)
        if len(self._queue) >= self.max_batch_ops or size >= self.max_batch_bytes:
            self._wake.set()

    @staticmethod
    def _weight(op: dict) -> int:
        if "_bytes" not in op:
            op["_bytes"] = len(_bytes({key: value for key, value in op.items() if not key.startswith("_")}))
        return op["_bytes"]

    def _trim_queue(self, reserved: int = 0) -> None:
        pending = self._expanding_count + (len(self._pending["ops"]) if self._pending else 0)
        while self._queue and len(self._queue) + pending + reserved > self.queue_limit:
            dropped = self._queue.popleft()
            self._queue_bytes -= self._weight(dropped)
            target = "*" if dropped.get("type") == "session.deleted" else dropped.get("sid") or "*"
            self._missing([target])
            pair = dropped.get("_pair")
            while pair and self._queue and self._queue[0].get("_pair") == pair:
                self._queue_bytes -= self._weight(self._queue.popleft())
            if dropped.get("op") == "replace":
                # 중간 조각을 잃은 세대는 final도 보내지 않아 기존 세대를 지우지 않는다.
                same = lambda op: op.get("op") == "replace" and all(
                    op.get(key) == dropped.get(key) for key in ("sid", "kind", "gen"))
                pairs = {op.get("_pair") for op in self._queue if same(op) and op.get("_pair")}
                keep = deque()
                for op in self._queue:
                    if same(op) or op.get("_pair") in pairs:
                        self._queue_bytes -= self._weight(op)
                    else:
                        keep.append(op)
                self._queue = keep
            self._backfill_wake.set()

    def _missing(self, targets) -> None:
        """원본 조회는 가벼운 재조회 신호로 복구하고 문서 백필은 만들지 않는다."""
        if self.read_mode == "source":
            for target in targets:
                if target and target != "*":
                    self._source_resync.add(target)
                else:
                    self._source_resync.update(sid for sid, source in self._session_types.items() if source != "replay")
        else:
            self.needs_backfill.update(targets)

    @staticmethod
    def _source_resync_op(sid, reason="source_queue_overflow"):
        return {"op": "event", "sid": sid, "type": "source.event", "ts": now_ms(),
                "data": {"type": "resync", "data": {"reason": reason}}}

    def _prepend(self, ops: list[dict], *, reserved: int = 0) -> None:
        self._queue.extendleft(reversed(ops))
        self._queue_bytes += sum(self._weight(op) for op in ops)
        self._trim_queue(reserved)

    def on_event(self, channel: str, event: Event) -> None:
        """탭은 동기로 복사·적재만 하고 네트워크와 재시도는 작업 태스크에 맡긴다."""
        if not self.enabled or not channel or self._loop is not None and not self._running:
            return
        if self._loop is not None and threading.get_ident() != self._thread_id:
            # 보존 정리는 작업 스레드에서도 발행하므로 큐·Event 변경은 앱 루프로 모은다.
            self._loop.call_soon_threadsafe(self._deliver_event, channel, copy.deepcopy(event))
            return
        try:
            if self.read_mode == "source":
                if event.type in {"settings.updated", "roster.updated"}:
                    targets = list(self._session_types) if channel == "*" else [channel]
                    for sid in targets:
                        if sid and self._session_types.get(sid) != "replay":
                            self._enqueue({**self._source_resync_op(sid, event.type), "_event_id": event.id})
                    return
                op = self.event_op(channel, event)
                if op is not None:
                    self._enqueue({**op, "_event_id": event.id})
                return
            if event.type == "settings.updated":
                self._enqueue({"op": "doc", "kind": "settings_public", "id": "global", "sid": None,
                               "ts": now_ms(), "data": self._public_settings(), "_event_id": event.id})
                return
            if event.type == "roster.updated":
                sids = [s["id"] for s in self._sessions()] if channel == "*" else [channel]
                for sid in sids:
                    if not sid or self._replay(sid):
                        continue
                    self._enqueue({"op": "doc", "kind": "roster", "id": sid, "sid": sid,
                                   "ts": now_ms(), "data": self._roster(sid), "_event_id": event.id})
                    self._enqueue({"op": "event", "sid": sid, "type": "resync", "ts": now_ms(),
                                   "data": {"reason": "roster_updated"}, "_event_id": event.id,
                                   "_stage_reload": bool(hasattr(self.rt.repo, "has_caption_sources")
                                                         and self.rt.repo.has_caption_sources(sid)),
                                   "_source_guess_reload": True})
                return
            op = self.event_op(channel, event)
            if op is not None:
                op["_event_id"] = event.id
                if event.type == "items.reload":
                    op["_reload"] = True
                self._enqueue(op)
                if event.type in {"session.status", "session.created", "session.deleted"}:
                    self._state_wake.set()
        except Exception as exc:
            self.last_error = "이벤트 변환 실패"
            self._missing([channel or "*"])
            log.error("클라우드 이벤트 변환 실패: %s", type(exc).__name__)

    def _deliver_event(self, channel: str, event: Event) -> None:
        if self._running:
            self.on_event(channel, event)

    async def _http(self, path: str, data: dict) -> dict | None:
        if not self.enabled:
            return None
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport,
                timeout=httpx.Timeout(connect=5, read=30, write=10, pool=5), follow_redirects=False)
        started = time.monotonic()
        status = None
        try:
            response = await self._client.post(self.config.cloud_url.rstrip("/") + path,
                content=_bytes(data), headers={"Authorization": "Bearer " + self.config.agent_token,
                                              "Content-Type": "application/json"})
            status = response.status_code
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            self._online, self.last_error = True, ""
            self._wanted = bool(result.get("wanted", self._wanted))
            self.mode_update()
            return result
        except Exception as exc:
            self._online = False
            self.last_error = "클라우드 인증 또는 연결 실패" if isinstance(exc, httpx.HTTPStatusError) else "클라우드 연결 실패"
            route = path if path in {"/agent/push", "/agent/heartbeat"} else "기타"
            log.warning("클라우드 요청 실패(route=%s, error=%s, elapsed_ms=%d, status=%s)",
                route, type(exc).__name__, int((time.monotonic()-started)*1000), status)
            return None

    def _packet(self, ops: list[dict], *, seq: int | None = None) -> dict:
        return {"boot_id": self.boot_id, "seq": self.seq + 1 if seq is None else seq,
                "ops": [{key: value for key, value in op.items() if not key.startswith("_")} for op in ops]}

    @staticmethod
    def _write_models(ops: list[dict]) -> int:
        """Mongo 미러 쓰기 모델과 agent/counter 고정 갱신을 센다. 삭제 영향 행수는 제외한다."""
        count = 2  # agent 잠금 갱신과 완료 정보 갱신. commit은 쓰기 모델이 아니다.
        if any(op.get("op") == "event" for op in ops):
            count += 1  # 이벤트 번호는 패킷당 한 번 확보한다.
        projected = {"utterance.upsert", "block.upsert", "item.upsert", "alert",
                     "utterance.delete", "alert.delete", "summary.update",
                     "session.status", "stats", "analysis.status"}
        for op in ops:
            action = op.get("op")
            if action in {"docs", "replace"}:
                count += len(op.get("docs") or [])
                count += int(action == "replace" and bool(op.get("final")))
            elif action in {"doc", "delete"}:
                count += 1
            elif action == "drop_session":
                count += 2 if op.get("sid") is not None else 0
            elif action == "sessions_index":
                count += 2
            elif action == "event":
                count += 1
                if op.get("type") in projected:
                    count += 1
                elif op.get("type") == "caption.source":
                    data = op.get("data") or {}
                    count += 1 + sum(type(did) is int for did in data.get("invalidated_document_ids", []))
                elif op.get("type") == "session.deleted" and op.get("sid") is not None:
                    count += 2
            else:
                count += 1  # 알 수 없는 작업도 예산 없는 작업으로 취급하지 않는다.
        return count

    def _fits(self, ops: list[dict], *, seq: int | None = None) -> bool:
        if self._write_models(ops) > self.max_batch_write_models:
            return False
        size = len(_bytes(self._packet(ops, seq=seq)))
        if size <= self.max_batch_bytes:
            return True
        if size > self._single_document_bytes:
            return False
        if len(ops) == 1:
            op = ops[0]
            return (op.get("op") in {"doc", "event"}
                    or op.get("op") in {"docs", "replace"} and len(op.get("docs") or []) == 1)
        # 큰 마지막 문서도 원래 완료 신호와 같은 패킷에 유지한다.
        return (len(ops) == 2 and ops[0].get("op") in {"docs", "replace"}
                and len(ops[0].get("docs") or []) == 1 and ops[1].get("op") == "event"
                and ops[1].get("type") in {"items.reload", "resync"})

    def _split_document_op(self, op: dict, *, reserve: list[dict] | None = None,
                           seq: int | None = None) -> list[dict]:
        """새 docs/replace만 나누고 세대·출처를 보존하며 final과 후속 신호 공간을 남긴다."""
        reserve = reserve or []
        if op.get("op") not in {"docs", "replace"} or self._fits([op, *reserve], seq=seq):
            return [op]
        template = {key: value for key, value in op.items() if key not in {"docs", "_bytes", "_pair"}}
        if op["op"] == "replace":
            template["final"] = False
        empty = {**template, "docs": []}
        base_size = len(_bytes(self._packet([empty], seq=seq)))
        base_models = self._write_models([empty])

        def fits(count, used, size=base_size, models=base_models):
            return (models + count <= self.max_batch_write_models
                    and size + used + max(0, count - 1) <= self.max_batch_bytes)

        chunks, part, used = [], [], 0
        for doc in op.get("docs") or []:
            size = len(_bytes(doc))
            if not fits(1, size):
                single = {**template, "docs": [doc]}
                if not self._fits([single], seq=seq):
                    raise ValueError("한 문서가 클라우드 전송 한도를 넘습니다")
                if part:
                    chunks.append({**template, "docs": part})
                    part, used = [], 0
                chunks.append(single)
                continue
            if part and not fits(len(part) + 1, used + size):
                chunks.append({**template, "docs": part})
                part, used = [], 0
            part.append(doc)
            used += size
        last = {**template, "docs": []}
        if op["op"] == "replace":
            last["final"] = bool(op.get("final"))
        final_size = len(_bytes(self._packet([last, *reserve], seq=seq)))
        final_models = self._write_models([last, *reserve])
        if not fits(len(part), used, final_size, final_models):
            if part:
                chunks.append({**template, "docs": part})
                part = []
            if not fits(0, 0, final_size, final_models):
                raise ValueError("최종 조각과 완료 신호가 클라우드 전송 한도를 넘습니다")
        last["docs"] = part
        if op.get("_pair"):
            last["_pair"] = op["_pair"]
        chunks.append(last)
        return chunks

    def _split_write_chunks(self, ops: list[dict], *, seq: int | None = None) -> list[dict]:
        out = []
        for group in self._groups(ops):
            for i, op in enumerate(group):
                out.extend(self._split_document_op(op, reserve=group[:i] + group[i + 1:], seq=seq))
        return out

    async def _read_call(self, callback):
        """미러 투영은 수신 연결·잠금과 다른 읽기 전용 연결에서 계산한다."""
        repo = getattr(self.rt,"repo",None)
        db = getattr(repo,"db",None)
        analysis = {}
        if getattr(self.rt,"analysis",None) is not None:
            try:
                analysis = copy.deepcopy(self.rt.analysis.status())
            except Exception:
                pass
        def read():
            if db is None or not hasattr(db,"read_connection"):
                # 저장소 없는 CLI와 작은 모의 저장소의 기존 계약만 유지한다.
                return callback(self)
            from .repo import Repo
            from types import SimpleNamespace
            with db.read_connection() as read_db:
                with read_db.transaction():
                    view = copy.copy(self)
                    view.rt = copy.copy(self.rt)
                    view.rt.repo = Repo(read_db)
                    if hasattr(view.rt,"db"):
                        view.rt.db = read_db
                    view.rt.analysis = SimpleNamespace(status=lambda:analysis)
                    # 읽기 도중 원본 큐·이벤트·캐시를 바꾸지 않는다.
                    view._session_types = dict(self._session_types)
                    view._source_guess_cache = dict(self._source_guess_cache)
                    view._live_turn_cache = dict(self._live_turn_cache)
                    return callback(view)
        if self._read_executor is None:
            self._read_executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix="gukgam-mirror-read")
        future = asyncio.get_running_loop().run_in_executor(self._read_executor,read)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # 수신 DB 종료 전 독립 읽기 연결의 finally를 끝낸다.
            await asyncio.gather(asyncio.shield(future),return_exceptions=True)
            raise

    async def _projection_data(self, rows):
        if self.read_mode == "source":
            return {}
        stage = {op["sid"] for op in rows if op.get("_stage_reload")}
        turns = {op["sid"] for op in rows if op.get("_stage_reload") or op.get("_live_turn_reload")}
        sources = {op["sid"] for op in rows if op.get("type")=="caption.source" or op.get("_source_guess_reload")}
        items = {op["sid"] for op in rows if op.get("_reload")}
        def read(view):
            return {"stage":{sid:view._stage_docs(sid) for sid in stage},
                "turn":{sid:view._live_turn_docs(sid) for sid in turns},
                "source":{sid:view._source_guess_docs(sid) for sid in sources},
                "item":{sid:view._item_docs(sid) for sid in items}}
        return await self._read_call(read)

    async def _expand_async(self, rows):
        return self._expand(rows,projections=await self._projection_data(rows))

    def _expand(self, rows: list[dict], *, projections: dict | None = None) -> list[dict]:
        if self.read_mode == "source":
            return self._split_write_chunks([row for row in rows if row.get("op") == "event" and row.get("type") == "source.event"])
        last_reload = {op["sid"]: i for i, op in enumerate(rows) if op.get("_reload")}
        last_stage = {op["sid"]: i for i, op in enumerate(rows) if op.get("_stage_reload")}
        last_turn = {op["sid"]: i for i, op in enumerate(rows) if op.get("_stage_reload") or op.get("_live_turn_reload")}
        last_source = {op["sid"]: i for i, op in enumerate(rows)
                       if op.get("type") == "caption.source" or op.get("_source_guess_reload")}
        source_changes = {}
        for sid in last_source:
            ids = {str(op.get("data", {}).get("id")) for op in rows
                   if op.get("sid") == sid and op.get("type") == "caption.source"}
            source_changes[sid] = self._source_guess_ops(sid, event_source_ids=ids,
                docs=projections["source"][sid] if projections is not None else None)
        out = []
        for i, op in enumerate(rows):
            if op.get("sid") in source_changes:
                changes, current = source_changes[op["sid"]]
                if op.get("type") == "caption.source":
                    source = current.get(str(op.get("data", {}).get("id")))
                    if source is not None:
                        data = copy.deepcopy(source["data"])
                        data["invalidated_document_ids"] = list(op.get("data",{}).get("invalidated_document_ids") or [])
                        op = {**op, "data": data}
                if last_source[op["sid"]] == i:
                    out.extend(changes)
            if last_turn.get(op.get("sid")) == i:
                # 배치마다 한 번만 읽기 투영하고 달라진 발언·삭제만 전송한다.
                # 원자막 판본이 먼저 바뀌어도 미러 조회가 옛 힌트를 즉시 제외한다.
                changes = self._live_turn_ops(op["sid"],docs=projections["turn"][op["sid"]] if projections is not None else None)
                for piece in changes:
                    if "_event_id" in op:
                        piece["_event_id"] = op["_event_id"]
                out.extend(changes)
            if op.get("_stage_reload") and last_stage[op["sid"]] == i:
                group = self._replace_chunks("transcript_stage", op["sid"], (projections["stage"][op["sid"]] if projections is not None else self._stage_docs(op["sid"])),
                                             self._gen(), reserve=op)
                for piece in group:
                    if "_event_id" in op:
                        piece["_event_id"] = op["_event_id"]
                pair_id = uuid.uuid4().hex
                group[-1]["_pair"] = pair_id
                out.extend(group)
                op = {**op, "_pair": pair_id}
            op = {key: value for key, value in op.items()
                  if key not in {"_stage_reload", "_live_turn_reload", "_source_guess_reload"}}
            if op.get("_reload"):
                if last_reload[op["sid"]] != i:
                    continue
                group = self._replace_chunks("item", op["sid"], (projections["item"][op["sid"]] if projections is not None else self._item_docs(op["sid"])), self._gen(), reserve=op)
                if "_event_id" in op:
                    for piece in group:
                        piece["_event_id"] = op["_event_id"]
                pair_id = uuid.uuid4().hex
                group[-1]["_pair"] = pair_id
                group.append({**{key: value for key, value in op.items() if key != "_reload"}, "_pair": pair_id})
                out.extend(group)
            else:
                out.append(op)
        return self._split_write_chunks(out)

    def _take_rows(self):
        rows = []
        while self._queue and (len(rows) < self.max_batch_ops or rows and rows[-1].get("_pair")
                               and self._queue[0].get("_pair") == rows[-1]["_pair"]):
            op = self._queue.popleft()
            self._queue_bytes -= self._weight(op)
            rows.append(op)
        if not rows and self.read_mode == "source":
            targets = sorted(self._source_resync)[:max(1, self.max_batch_ops)]
            for target in targets:
                self._source_resync.discard(target)
                rows.append(self._source_resync_op(target))
        return rows

    def _next_packet(self) -> dict | None:
        """동기 단위 시험·이전 호출 호환. 실제 송신은 비동기 읽기 경로를 쓴다."""
        return self._pack_rows(self._take_rows())

    async def _next_packet_async(self):
        rows = self._take_rows()
        if not rows:
            return None
        self._expanding_count = len(rows)
        try:
            projections = await self._projection_data(rows)
            self._expanding_count = 0
            return self._pack_rows(rows,projections=projections)
        except asyncio.CancelledError:
            self._expanding_count = 0
            self._prepend(rows)
            raise
        except Exception:
            self._expanding_count = 0
            self._prepend(rows)
            self._missing(op.get("sid") or "*" for op in rows)
            self._backfill_wake.set()
            raise

    def _pack_rows(self, rows, *, projections=None):
        while rows:
            try:
                ops = self._expand(rows,projections=projections)
            except ValueError:
                self._missing(op.get("sid") or "*" for op in rows)
                self.last_error = "한 문서가 클라우드 전송 한도를 넘습니다"
                self._backfill_wake.set()
                return None
            if len(ops) <= self.max_batch_ops and self._fits(ops):
                return self._packet(ops)
            part, rest = [], []
            for group in self._groups(ops):
                if rest or part and (len(part) + len(group) > self.max_batch_ops or not self._fits(part + group)):
                    rest.extend(group)
                elif not self._fits(group):
                    self._missing(op.get("sid") or "*" for op in group)
                    self.last_error = "클라우드 전송 한도를 넘는 기록이 있어 백필이 필요합니다"
                    self._backfill_wake.set()
                    continue
                else:
                    part.extend(group)
            self._prepend(rest, reserved=len(part))
            return self._packet(part) if part else None
        return None

    async def _send_pending(self) -> bool:
        if self._pending is None:
            return True
        result = await self._http("/agent/push", self._pending)
        if result is None:
            return False
        self.seq = self._pending["seq"]
        self._pending = None
        self.last_push_ok_ms = now_ms()
        return True

    async def flush(self, timeout: float | None = None) -> bool:
        """재시도 중 같은 묶음의 boot·seq·ops를 보존하고 성공할 때만 seq를 진행한다."""
        if not self.enabled:
            return True
        async def send_all() -> bool:
            delay = self.retry_initial_s
            while True:
                # 옛 final이 새 live 기록을 추월하지 않게 백필의 후속 단계까지 먼저 마친다.
                if self._backfill_cursor is not None:
                    if not await self.backfill(self._backfill_scope):
                        await asyncio.sleep(delay)
                        delay = min(self.retry_max_s, max(delay*2, self.retry_initial_s))
                        continue
                async with self._send_lock:
                    if self._backfill_cursor is not None:
                        continue
                    while self._pending is not None or self._send_suffix or self._queue or self._source_resync:
                        if self._pending is None:
                            if self._send_suffix:
                                self._pending = self._packet(self._send_suffix.popleft())
                            else:
                                try:
                                    self._pending = await self._next_packet_async()
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:
                                    self.last_error = "미러 기록 읽기를 마치지 못해 다시 시도합니다"
                                    log.warning("미러 기록 읽기 실패(%s)", type(exc).__name__)
                                    await asyncio.sleep(delay)
                                    delay = min(self.retry_max_s, max(delay*2, self.retry_initial_s))
                                    continue
                                if self._pending is None:
                                    return not self._queue
                        if not await self._send_pending():
                            await asyncio.sleep(delay)
                            delay = min(self.retry_max_s, max(delay*2, self.retry_initial_s))
                            continue
                        delay = self.retry_initial_s
                        await asyncio.sleep(0)
                if self.needs_backfill:
                    self._backfill_wake.set()
                return True
        try:
            if timeout is not None:
                async with asyncio.timeout(timeout):
                    return await send_all()
            return await send_all()
        except TimeoutError:
            return False

    def _item_docs(self, sid: str) -> list[dict]:
        return [{"id": str(row["id"]), "ts": row.get("utt_ts") or row.get("created_at") or 0,
                 "data": self._redact("item", serialize.item(row))}
                for row in self.rt.repo.list_items(sid, include_dismissed=True, limit=1_000_000)]

    def _stored_display(self, sid: str) -> dict | None:
        """새 저장소는 변경 때 만든 표시본만 읽고, 구 모의 저장소만 이전 계약을 쓴다."""
        repo = self.rt.repo
        read = getattr(repo, "display_snapshot", None)
        initialized = getattr(repo, "display_initialized", None)
        if not callable(read) or not callable(initialized) or not initialized(sid):
            return None
        # dirty 판본은 예외를 전파한다. 조회 중 재검증하거나 옛 후보를 되살리지 않는다.
        return read(sid)

    def _live_turn_docs(self, sid: str) -> list[dict]:
        """변경 때 저장한 코드 추정만 전송한다. 읽기에서 화자를 다시 판단하지 않는다."""
        stored = self._stored_display(sid)
        if stored is not None:
            return [{"id": str(row["id"]), "ts": row.get("ts_start") or 0, "seq": row.get("seq") or 0,
                     "data": copy.deepcopy(row)} for row in stored.get("live_turns", [])]
        from .live_turns import session_live_turns
        return [{"id": str(row["id"]), "ts": row.get("ts_start") or 0, "seq": row.get("seq") or 0,
                 "data": row} for row in session_live_turns(self.rt.repo, sid)]

    def _source_guess_docs(self, sid: str) -> list[dict]:
        """조립 대기 원문도 최신 자막만 읽은 표시 후보를 기존 원조각에 포함한다."""
        repo = self.rt.repo
        stored = self._stored_display(sid)
        if stored is not None:
            return [{"id": str(row["id"]), "ts": row.get("first_rx") or 0, "data": public_source(row)}
                    for row in stored.get("sources", []) if row.get("replayed") is not True]
        if not hasattr(repo, "all_caption_sources"):
            return []
        from .live_turns import source_speaker_guesses
        sources = [row for row in repo.all_caption_sources(sid) if row.get("replayed") is not True]
        guesses = source_speaker_guesses(sources, repo.get_roster(sid) if hasattr(repo, "get_roster") else [])
        return [{"id": str(row["id"]), "ts": row["first_rx"],
                 "data": public_source({**row, **({"speaker_guess": guesses[row["id"]]} if row["id"] in guesses else {})})}
                for row in sources]

    def _source_guess_ops(self, sid: str, *, event_source_ids: set[str] | None = None, docs: list[dict] | None = None) -> tuple[list[dict], dict]:
        current = {row["id"]: row for row in (self._source_guess_docs(sid) if docs is None else docs)}
        previous = self._source_guess_cache.get(sid)
        # 최초 전체 기준은 백필이 보낸다. 현재 이벤트만으로 전날 원문 전체를 재전송하지 않는다.
        previous = previous if previous is not None else current
        changes = [{"op": "doc", "kind": "caption_source", "sid": sid, **row}
            for uid, row in current.items() if uid not in (event_source_ids or set()) and
            (uid not in previous or row["data"].get("revision") != previous[uid]["data"].get("revision")
             or row["data"].get("speaker_guess") != previous[uid]["data"].get("speaker_guess"))]
        self._source_guess_cache[sid] = copy.deepcopy(current)
        return changes, current

    def _live_turn_ops(self, sid: str, *, docs: list[dict] | None = None) -> list[dict]:
        """전체 교체는 백필에 맡기고 실시간은 캐시와 비교한 변경분만 예약한다."""
        current = {row["id"]: row for row in (self._live_turn_docs(sid) if docs is None else docs)}
        previous = self._live_turn_cache.get(sid, {})
        changed = [{"op": "doc", "kind": "live_turn", "sid": sid, **row}
                   for uid, row in current.items() if row != previous.get(uid)]
        changed.extend({"op": "delete", "kind": "live_turn", "sid": sid, "id": uid}
                       for uid in sorted(previous.keys() - current.keys()))
        # 확장한 작업은 pending/retry 큐에 보존된다. 큐 손실은 기존 needs_backfill로 복구한다.
        self._live_turn_cache[sid] = copy.deepcopy(current)
        return changed

    def _stage_docs(self, sid: str) -> list[dict]:
        """변경 때 검증해 저장한 보정 문서만 전송한다. GET·동기화에서 재검증하지 않는다."""
        repo = self.rt.repo
        stored = self._stored_display(sid)
        if stored is not None:
            return [{"id": str(row["id"]), "ts": row.get("first_rx") or 0, "data": public_document(row)}
                    for row in stored.get("documents", [])]
        if not hasattr(repo, "list_stage_documents"):
            return []
        docs = []
        try:
            rows = repo.list_stage_documents(sid)
        except Exception:
            log.warning("보정 판본 목록을 확인할 수 없어 릴레이의 이전 판본을 비웁니다")
            return []
        for row in rows:
            if row.get("mode") != "closed_block_v1" or row.get("status") == "stale":
                continue
            try:
                current = repo.stage_document_current(sid, row["id"])
            except Exception:
                log.warning("보정 판본 현재성을 확인할 수 없어 해당 릴레이 판본을 제외합니다")
                continue
            if current:
                sources = repo.caption_sources_by_ids(sid, row.get("source_ids") or [])
                if any(source.get("replayed") is True for source in sources):
                    continue
                docs.append({"id": str(row["id"]), "ts": row.get("first_rx") or 0, "data": public_document(row)})
        return docs

    def _snapshot(self, sid: str) -> list[tuple[str, list[dict]]]:
        repo = self.rt.repo
        session = repo.get_session(sid)
        if session is None or session["source_type"] == "replay":
            return []
        self._session_types[sid] = session["source_type"]
        analysis = {}
        if getattr(self.rt, "analysis", None) is not None:
            try:
                analysis = self.rt.analysis.status()
            except Exception:
                pass
        session_data = serialize.session(session, repo.session_stats(sid))
        session_data["analysis"] = analysis
        summaries = [row for scope in ("session", "rolling") if (row := repo.latest_summary(sid, scope))]
        source_docs = self._source_guess_docs(sid)
        self._source_guess_cache[sid] = {row["id"]: copy.deepcopy(row) for row in source_docs}
        stored = self._stored_display(sid)
        block_out = serialize.block
        if stored is None and hasattr(repo, "list_transcript_reviews") and settings.get(repo, "transcript_review").get("enabled"):
            from .review_display import get_block_display
            block_out = lambda row: get_block_display(repo, row)
        return [
            ("session", [{"id": sid, "ts": session["created_at"], "data": session_data}]),
            ("roster", [{"id": sid, "ts": session["created_at"], "data": self._roster(sid)}]),
            ("block", [{"id": str(row["id"]), "ts": row["ts_start"],
                        "data": copy.deepcopy(row) if stored is not None else block_out(row)}
                       for row in (stored.get("blocks", []) if stored is not None else repo.list_blocks(sid))]),
            ("utterance", [{"id": str(row["id"]), "ts": row["ts_start"], "seq": row["seq"],
                           "data": serialize.utterance(row)} for row in repo.list_utterances(sid, limit=1_000_000)]),
            ("item", self._item_docs(sid)),
            ("alert", [{"id": str(row["id"]), "ts": row.get("ts_start") or row.get("created_at") or 0,
                       "data": serialize.alert(row)} for row in repo.list_alerts(sid, limit=1_000_000)]),
            ("summary", [{"id": f"{sid}:{row['scope']}", "ts": row["created_at"], "data": serialize.summary(row)}
                         for row in summaries]),
            ("caption_source", source_docs),
            ("transcript_stage", self._stage_docs(sid)),
            ("live_turn", self._live_turn_docs(sid)),
        ]

    def _replace_chunks(self, kind: str, sid: str | None, docs: list[dict], gen: int,
                        *, reserve: dict | None = None) -> list[dict]:
        op = {"op": "replace", "kind": kind, "sid": sid, "gen": gen, "docs": docs, "final": True}
        return self._split_document_op(op, reserve=[reserve] if reserve else [])

    @staticmethod
    def _groups(ops: list[dict]):
        """최종 replace와 reload 이벤트는 크기·건수 분할에서도 함께 유지한다."""
        i = 0
        while i < len(ops):
            group = [ops[i]]
            pair = ops[i].get("_pair")
            i += 1
            while pair and i < len(ops) and ops[i].get("_pair") == pair:
                group.append(ops[i])
                i += 1
            yield group

    async def _drain_send_suffix(self) -> bool:
        """이미 만든 pending은 그대로, 남은 조각은 같은 세대와 순서로 이어 보낸다."""
        while self._pending is not None or self._send_suffix:
            if self._pending is None:
                self._pending = self._packet(self._send_suffix.popleft())
            if not await self._send_pending():
                return False
            await asyncio.sleep(0)
        return True

    async def _send_ops(self, ops: list[dict]) -> bool:
        # 처음부터 전체 계획을 확정해야 중간 응답 실패·취소에도 suffix를 잃지 않는다.
        part, packets = [], []
        self._send_plan_invalid = False
        # 미래 seq의 자리수 증가도 미리 센다. 문서마다 조각과 빈 final이 생겨도
        # 이 수보다 패킷이 많아지지 않으므로 나중에 실제 seq로 감쌀 때 넘치지 않는다.
        last_seq = ((self._pending["seq"] if self._pending else self.seq) + len(self._send_suffix)
                    + sum(len(op.get("docs") or [])+1 for op in ops))
        try:
            for group in self._groups(self._split_write_chunks(ops, seq=last_seq)):
                if part and (len(part)+len(group) > self.max_batch_ops or not self._fits(part+group, seq=last_seq)):
                    packets.append(copy.deepcopy(part))
                    part = []
                if not self._fits(group, seq=last_seq):
                    raise ValueError("한 기록이 클라우드 전송 한도를 넘습니다")
                part.extend(group)
            if part:
                packets.append(copy.deepcopy(part))
        except ValueError:
            self._send_plan_invalid = True
            self.last_error = "한 기록이 클라우드 전송 한도를 넘습니다"
            return False
        self._send_suffix.extend(packets)
        return await self._drain_send_suffix()

    def _local_stamp(self):
        db = getattr(getattr(self.rt, "repo", None), "db", None)
        return (self.rt.bus.last_id, getattr(db, "change_epoch", None))

    def _mark_backfill_retry(self, scope):
        target = scope or "*"
        # 이미 있던 손실 신호와 이번 전송 재시도 신호를 구분한다.
        if target not in self.needs_backfill:
            self.needs_backfill.add(target)
            self._backfill_retry_marker = (target, self._local_stamp())
        self._backfill_wake.set()

    async def backfill(self, sid: str | None = None) -> bool:
        """중단된 같은 스냅샷을 이어 보내고 그동안 생긴 로컬 변경도 후속 검증한다."""
        if not self.enabled:
            return True
        if self.read_mode == "source":
            targets = [sid] if sid else list(self._session_types)
            for target in targets:
                if target and self._session_types.get(target) != "replay":
                    self._enqueue(self._source_resync_op(target, "source_requested"))
            self._wake.set()
            return True
        try:
            return await self._backfill_once(sid)
        except asyncio.CancelledError:
            self._mark_backfill_retry(self._backfill_scope if self._backfill_cursor else sid)
            raise
        except Exception as exc:
            self.last_error = "미러 백필 기록을 읽지 못해 다시 시도합니다"
            self._mark_backfill_retry(self._backfill_scope if self._backfill_cursor else sid)
            log.warning("미러 백필 읽기 실패(%s)", type(exc).__name__)
            return False

    async def _backfill_once(self, sid: str | None = None) -> bool:
        waiting_cursor, waiting_scope = self._backfill_cursor, self._backfill_scope
        async with self._backfill_lock, self._send_lock:
            # 같은 중단 지점을 기다리던 다른 송신자가 이미 마쳤다면 중복 백필하지 않는다.
            if (waiting_cursor is not None and waiting_cursor is self._completed_backfill_cursor
                    and waiting_scope == sid):
                return True
            marker, self._backfill_retry_marker = self._backfill_retry_marker, None
            if marker is not None and marker[1] == self._local_stamp():
                self.needs_backfill.discard(marker[0])
            if self._backfill_cursor is not None and self._backfill_scope != sid:
                self.needs_backfill.add(sid or "*")
            while True:
                if not await self._drain_send_suffix():
                    self._mark_backfill_retry(self._backfill_scope if self._backfill_cursor else sid)
                    return False
                if self._backfill_cursor is None:
                    self._backfill_scope = sid
                    self._backfill_cursor = self._backfill_batches(sid)
                scope = self._backfill_scope
                try:
                    ops = await anext(self._backfill_cursor)
                except StopAsyncIteration:
                    self._completed_backfill_cursor = self._backfill_cursor
                    self._backfill_cursor = None
                    self._backfill_scope = None
                    if scope != sid:
                        continue
                    return True
                except BaseException:
                    # 읽기 중 오류·취소는 generator를 닫는다. 남은 확정 조각을 먼저
                    # 마친 뒤 최신 자료를 다시 읽으며, 전송 중 취소는 이 경로에 오지 않는다.
                    self._mark_backfill_retry(scope)
                    self._backfill_cursor = None
                    self._backfill_scope = None
                    raise
                if not await self._send_ops(ops):
                    if self._send_plan_invalid:
                        await self._backfill_cursor.aclose()
                        self._backfill_cursor = None
                        self._backfill_scope = None
                    self._mark_backfill_retry(scope)
                    return False

    async def _backfill_batches(self, sid):
        """yield 뒤에는 그 전송 계획이 모두 성공한 경우에만 다음 단계로 진행한다."""
        def select(view):
            sessions = [s for s in view._sessions() if s["source_type"] != "replay"]
            return sessions, ([s for s in sessions if s["id"] == sid] if sid else [
                s for s in sessions if view._recent(s, now_ms()-7*DAY_MS)])
        sessions, selected = await self._read_call(select)
        self._session_types.update({s["id"]: s["source_type"] for s in sessions})
        if sid is None:
            self.needs_backfill.discard("*")
        elif not selected:
            self.needs_backfill.discard(sid)
        for session in selected:
            target = session["id"]
            while True:
                self.needs_backfill.discard(target)
                start = self.rt.bus.last_id
                snapshot = await self._read_call(lambda view: view._snapshot(target))
                source_rows = dict(snapshot).get("caption_source", [])
                self._source_guess_cache[target] = {row["id"]: copy.deepcopy(row) for row in source_rows}
                ops = []
                for kind, docs in snapshot:
                    ops.extend(self._replace_chunks(kind, target, docs, self._gen()))
                yield ops
                if self.rt.bus.lost_since(target, start):
                    continue
                replay = self.rt.bus.replay_since(target, start)
                rows = []
                for event in replay:
                    if event.type == "roster.updated":
                        rows.extend([{"op": "doc", "kind": "roster", "id": target, "sid": target,
                                      "ts": now_ms(), "data": self._roster(target)},
                                     {"op": "event", "sid": target, "type": "resync", "ts": now_ms(),
                                      "data": {}, "_stage_reload": True}])
                    elif (op := self.event_op(target, event)) is not None:
                        op["_reload"] = event.type == "items.reload"
                        rows.append(op)
                yield await self._expand_async(rows)
                last = replay[-1].id if replay else start
                self._queue = deque(op for op in self._queue if not (
                    op.get("sid") == target and op.get("_event_id", last+1) <= last))
                self._queue_bytes = sum(self._weight(op) for op in self._queue)
                break
        def final_data(view):
            sessions = [s for s in view._sessions() if s["source_type"] != "replay"]
            ids = {s["id"] for s in sessions}
            projections = {s["id"]: {"source": view._source_guess_docs(s["id"]),
                "turn": view._live_turn_docs(s["id"]), "stage": view._stage_docs(s["id"])}
                for s in selected if s["id"] in ids}
            return view._public_settings(), sessions, projections
        public_settings, latest_sessions, projections = await self._read_call(final_data)
        final = self._replace_chunks("settings_public", None,
            [{"id": "global", "ts": now_ms(), "data": public_settings}], self._gen())
        self._session_types.update({s["id"]: s["source_type"] for s in latest_sessions})
        latest_ids = {s["id"] for s in latest_sessions}
        final.append({"op": "sessions_index", "sids": [s["id"] for s in latest_sessions]})
        final_turns = {}
        for session in selected:
            target = session["id"]
            if target in latest_ids:
                final.extend(self._source_guess_ops(target, docs=projections[target]["source"])[0])
                live_rows = projections[target]["turn"]
                final_turns[target] = {row["id"]: row for row in live_rows}
                final.extend(self._replace_chunks("live_turn", target, live_rows, self._gen()))
                final.extend(self._replace_chunks("transcript_stage", target, projections[target]["stage"], self._gen()))
        final.extend({"op": "event", "sid": s["id"], "type": "resync", "ts": now_ms(),
                      "data": {"reason": "backfill"}} for s in selected if s["id"] in latest_ids)
        yield final
        self._live_turn_cache.update(copy.deepcopy(final_turns))
        self._last_backfill = time.monotonic()

    async def heartbeat(self) -> bool:
        if not self.enabled:
            return True
        self.mode_update()
        interval = self.active_heartbeat_s if self.mode == "active" else self.idle_heartbeat_s
        payload = {"boot_id": self.boot_id, "connection_id": self.boot_id, "version": __version__, "mode": self.mode,
            "read_mode": self.read_mode,
            "interval_s": max(1, int(interval)), "queue": self.status()["queue"],
            "needs_backfill": sorted(self.needs_backfill), "last_push_ok_ms": self.last_push_ok_ms,
            "sessions_live": sum(s["status"] == "live" for s in self._sessions())}
        return await self._http("/agent/heartbeat", payload) is not None

    async def check(self) -> bool:
        """설정과 인증 상태만 확인한다. repo·bus 없는 CLI에서도 쓸 수 있다."""
        ok = await self.heartbeat()
        if self._own_client and self._client is not None and not self._running:
            await self._client.aclose()
            self._client = None
        return ok

    async def _wait(self, event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(event.wait(), max(0.001, seconds))
        except TimeoutError:
            pass
        event.clear()

    @staticmethod
    async def _retry_pause(seconds: float) -> None:
        """타이머가 일찍 깨어나도 정한 재시도 간격을 지키며 취소는 전파한다."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _push_loop(self) -> None:
        while self._running:
            await self._wait(self._wake, self.batch_interval)
            if self._queue or self._source_resync or self._pending or self._send_suffix or self._backfill_cursor is not None:
                await self.flush()

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await self.heartbeat()
            self._backfill_wake.set()
            interval = self.active_heartbeat_s if self.mode == "active" else self.idle_heartbeat_s
            await self._wait(self._state_wake, interval)

    async def _backfill_loop(self) -> None:
        first, delay = True, self.retry_initial_s
        while self._running:
            due = self.mode == "active" and time.monotonic() - self._last_backfill >= self.backfill_interval_s
            if first or self.needs_backfill or due or self._backfill_cursor is not None:
                target = (self._backfill_scope if self._backfill_cursor is not None else
                    None if first or "*" in self.needs_backfill or due else next(iter(self.needs_backfill)))
                first = False
                if await self.backfill(target):
                    delay = self.retry_initial_s
                else:
                    await self._retry_pause(delay)
                    delay = min(self.retry_max_s, max(delay * 2, self.retry_initial_s))
                    continue
            await self._wait(self._backfill_wake, self.backfill_interval_s if self.mode == "active" else self.idle_heartbeat_s)

    async def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        self._loop, self._thread_id = asyncio.get_running_loop(), threading.get_ident()
        self._session_types.update({s["id"]: s["source_type"] for s in self._sessions()})
        if self.read_mode == "source":
            for sid, source in self._session_types.items():
                if source != "replay":
                    self._enqueue(self._source_resync_op(sid, "source_started"))
        self.rt.bus.add_tap(self.on_event)
        self._tasks = [asyncio.create_task(self._push_loop(), name="cloud-push"),
                       asyncio.create_task(self._heartbeat_loop(), name="cloud-heartbeat")]
        if self.read_mode == "mirror":
            self._tasks.append(asyncio.create_task(self._backfill_loop(), name="cloud-backfill"))
        if self.app is not None:
            self.command_runner.start()

    async def stop(self, flush_timeout: float = 2) -> None:
        if self._command_runner is not None:
            await self._command_runner.stop()
        if self._running:
            self._running = False
            self.rt.bus.remove_tap(self.on_event)
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        if self.enabled and flush_timeout > 0:
            await self.flush(timeout=flush_timeout)
        if self._backfill_cursor is not None:
            self._mark_backfill_retry(self._backfill_scope)
            await self._backfill_cursor.aclose()
            self._backfill_cursor = None
            self._backfill_scope = None
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._read_executor is not None:
            executor,self._read_executor = self._read_executor,None
            await asyncio.to_thread(executor.shutdown,wait=True,cancel_futures=True)
