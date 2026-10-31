"""수집 PC 없이 읽는 클라우드 미러 API와 클라우드 자체 북마크."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import weakref
from collections import OrderedDict
from contextlib import nullcontext
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..web.auth import User, require_operator, require_viewer

router = APIRouter()

SID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
ITEM_KINDS = frozenset({"data_request", "commitment", "issue", "witness_request", "written_question"})
ITEM_STATUSES = frozenset({"open", "in_progress", "done", "dismissed"})
MAX_DOCS = 50_000
# 필터를 적용하기 전에 뒤쪽 기록을 잘라내지 않는다(SQL LIMIT의 정수 범위).
ALL_DOCS = 2**63 - 1
MANIFEST_KINDS = ("session", "roster", "block", "utterance", "item", "alert", "summary",
                  "caption_source", "transcript_stage", "live_turn")
STAGE_CACHE_SESSIONS = 2
STAGE_CACHE_BYTES = 32 * 1024 * 1024
STAGE_CACHE_WAIT_S = 35
_STAGE_CACHES = weakref.WeakKeyDictionary()
_STAGE_CACHES_LOCK = threading.Lock()


class _StageFlight:
    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.status = None


class _StageSnapshotCache:
    """한 Store의 공개 자막 판정만 제한된 메모리에 보관한다. I/O 중 메타 잠금은 풀어 둔다."""

    def __init__(self, max_sessions=STAGE_CACHE_SESSIONS, max_bytes=STAGE_CACHE_BYTES):
        self.max_sessions, self.max_bytes = max_sessions, max_bytes
        self.lock = threading.Lock()
        self.entries, self.loading = OrderedDict(), {}
        self.bytes = 0

    def get_or_build(self, sid, fence, build):
        key = (sid, fence)
        with self.lock:
            if key in self.entries:
                value, size = self.entries.pop(key)
                self.entries[key] = (value, size)
                return value
            flight = self.loading.get(key)
            owner = flight is None
            if owner:
                if len(self.loading) >= self.max_sessions:
                    raise HTTPException(503, "다른 자막 판본을 읽고 있습니다. 잠시 뒤 다시 확인하세요")
                flight = self.loading[key] = _StageFlight()
        if not owner:
            if not flight.done.wait(STAGE_CACHE_WAIT_S):
                raise HTTPException(503, "자막 판본 준비 시간을 초과했습니다. 잠시 뒤 다시 확인하세요")
            if flight.status is not None:
                raise HTTPException(flight.status, "자막 판본을 확인하지 못했습니다. 다시 확인하세요")
            return flight.value
        try:
            value = build()
            size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            with self.lock:
                # 같은 회의의 이전 판본은 다시 사용하지 않는다.
                for old in [old for old in self.entries if old[0] == sid]:
                    self.bytes -= self.entries.pop(old)[1]
                if size <= self.max_bytes:
                    while self.entries and (len(self.entries) >= self.max_sessions or self.bytes+size > self.max_bytes):
                        self.bytes -= self.entries.popitem(last=False)[1][1]
                    self.entries[key] = (value, size)
                    self.bytes += size
                flight.value = value
            return value
        except BaseException as exc:
            flight.status = exc.status_code if isinstance(exc, HTTPException) else 503
            raise
        finally:
            with self.lock:
                self.loading.pop(key, None)
                flight.done.set()


def _stage_cache(store):
    with _STAGE_CACHES_LOCK:
        cache = _STAGE_CACHES.get(store)
        if cache is None:
            cache = _STAGE_CACHES[store] = _StageSnapshotCache()
        return cache


def _stage_fence(store):
    try:
        value = store.agent_status()
        fence = tuple(value.get(key) for key in ("boot_id", "last_seq", "pruned_at"))
        if (fence[0] is not None and not isinstance(fence[0], str)
                or (fence[0] is None) != (fence[1] is None)
                or isinstance(fence[0], str) and not fence[0]
                or fence[1] is not None and (not isinstance(fence[1], int) or isinstance(fence[1], bool) or fence[1] < 0)
                or not isinstance(fence[2], int) or isinstance(fence[2], bool) or fence[2] < 0):
            raise ValueError
        # 실제 BSON Int64도 정수다. 캐시 키는 일반 int로 통일한다.
        return fence[0], int(fence[1]) if fence[1] is not None else None, int(fence[2])
    except Exception:
        raise HTTPException(503, "자막 판본 상태를 확인하지 못했습니다. 잠시 뒤 다시 확인하세요") from None


def _stage_fence_matches(store, fence):
    if _stage_fence(store) != fence:
        raise HTTPException(409, "동기화 중 자막 판본이 바뀌었습니다. 다시 확인하세요")


class BookmarkIn(BaseModel):
    """북마크 메모의 길이는 기존 로컬 API와 같다."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    note: str = Field("", max_length=500)


def _store(request: Request) -> Any:
    return request.app.state.store


def _session(store: Any, sid: str) -> dict:
    row = store.get_doc("session", sid) if SID_RE.fullmatch(sid) else None
    if row is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    return row


def _session_out(row: dict, *, with_stats: bool = True) -> dict:
    """분석 상태는 스냅샷 최상위에 둬 기존 serialize.session 형태를 지킨다."""
    excluded = {"analysis"} if with_stats else {"analysis", "stats"}
    return {key: value for key, value in row.items() if key not in excluded}


def _utterance(store: Any, uid: int) -> dict:
    row = store.get_doc("utterance", str(uid))
    if row is None:
        raise HTTPException(404, "발언을 찾을 수 없습니다")
    return row


def _bookmark_id(sid: str, user: User) -> str:
    return f"{sid}:{user.actor}"


def _bookmarks(store: Any, sid: str, user: User) -> dict:
    return store.get_doc("bookmark_set", _bookmark_id(sid, user)) or {"ids": []}


def _item_matches(item: dict, *, kind: str | None = None, status: str | None = None,
                  dept: str | None = None, block_id: int | None = None,
                  include_dismissed: bool = False) -> bool:
    if item.get("status") == "superseded":
        return False
    if kind and item.get("kind") != kind:
        return False
    if status:
        if item.get("status") != status:
            return False
    elif not include_dismissed and item.get("status") == "dismissed":
        return False
    if dept and item.get("assignee_dept") != dept:
        return False
    return block_id is None or item.get("block_id") == block_id


@router.get("/sessions")
async def list_sessions(request: Request, status: str | None = None,
                        limit: int = Query(100, ge=1, le=500),
                        user: User = Depends(require_viewer)):
    rows = _store(request).list_docs("session", limit=ALL_DOCS)
    return {"items": [_session_out(row) for row in rows if not status or row.get("status") == status][:limit]}


@router.get("/sessions/{sid}")
async def get_session(request: Request, sid: str, user: User = Depends(require_viewer)):
    return _session_out(_session(_store(request), sid))


@router.get("/sessions/{sid}/snapshot")
async def snapshot(request: Request, sid: str, user: User = Depends(require_viewer)):
    store = _store(request)
    # 읽는 동안 생긴 변경은 이 번호 뒤의 SSE로 이어 받는다.
    last_id = store.max_event_id(sid)
    session = _session(store, sid)
    roster = store.get_doc("roster", sid) or {}
    items = [row for row in store.list_docs("item", sid, limit=ALL_DOCS)
             if _item_matches(row, include_dismissed=True)][:2000]
    return {
        "session": _session_out(session),
        "blocks": store.list_docs("block", sid, limit=MAX_DOCS),
        "utterances": store.list_docs("utterance", sid, limit=MAX_DOCS),
        "items": items,
        "alerts": store.list_docs("alert", sid, limit=500),
        "summary": store.get_doc("summary", f"{sid}:session"),
        "rolling": store.get_doc("summary", f"{sid}:rolling"),
        "roster": roster.get("members", []),
        "bookmarks": _bookmarks(store, sid, user).get("ids", []),
        "analysis": session.get("analysis"),
        "last_event_id": last_id,
        "me": user.public(),
    }


def manifest_digest(kind: str, rows: list[dict]) -> dict:
    """배열 순서를 보존한 공개 문서의 논리 ID·본문만 비교한다. 저장 메타는 제외한다."""
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: str(item["id"])):
        data = row["data"]
        if kind == "session":
            data = {key: value for key, value in data.items() if key != "analysis"}
        digest.update(json.dumps({"id": str(row["id"]), "data": data}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return {"count": len(rows), "sha256": digest.hexdigest()}


@router.get("/sessions/{sid}/mirror-manifest")
async def mirror_manifest(request: Request, sid: str, kind: str | None = None,
                          user: User = Depends(require_operator)):
    """운영자에게 원문 대신 고정 미러 종류의 건수·해시만 제공한다. 모델은 실행하지 않는다."""
    store = _store(request)
    budget = nullcontext()
    if getattr(store, "dialect", None) == "mongo":
        from .mongo_store import _driver
        # 내부 개별 읽기 예산도 전체 남은 시간을 연장하지 못하게 한다.
        budget = _driver().timeout(40)
    with budget:
        return _mirror_manifest(store, sid, kind=kind)


def _mirror_manifest(store, sid: str, *, kind: str | None = None) -> dict:
    """진행·보존 정리 판본이 같은 경우에만 전체 결과를 반환한다."""
    if kind is not None and kind not in (*MANIFEST_KINDS, "settings_public"):
        raise HTTPException(400, "알 수 없는 미러 대조 종류입니다")
    deadline = time.monotonic() + 40
    _session(store, sid)

    def remaining():
        if time.monotonic() >= deadline:
            raise HTTPException(503, "미러 대조 시간을 초과했습니다. 잠시 뒤 다시 확인하세요")

    before = store.agent_status()
    result = {}
    for selected in MANIFEST_KINDS if kind is None else (kind,):
        remaining()
        if selected == "settings_public":
            settings = store.get_doc("settings_public", "global")
            rows = [{"id": "global", "data": settings}] if settings is not None else []
        else:
            rows = store.doc_rows(selected, sid, limit=ALL_DOCS)
        result[selected] = manifest_digest(selected, rows)
    if kind is None:
        remaining()
        settings = store.get_doc("settings_public", "global")
        result["settings_public"] = manifest_digest("settings_public",
            [{"id": "global", "data": settings}] if settings is not None else [])
    remaining()
    after = store.agent_status()
    remaining()
    if any(before.get(key) != after.get(key) for key in ("boot_id", "last_seq", "pruned_at")):
        raise HTTPException(409, "동기화 중 자료가 바뀌었습니다. 완료 후 다시 확인하세요")
    return {"version": 1, "consistent": True, "kinds": result,
            **({"checkpoint": {key: after.get(key) for key in ("boot_id", "last_seq", "pruned_at")}}
               if kind is not None else {})}


def _position(utterance: dict) -> tuple[int, str, int]:
    seq = int(utterance.get("seq") or 0)
    return int(utterance.get("ts_start") or 0), str(utterance.get("source_order") or f"{seq:012d}"), seq


def _read_stage_snapshot(store, sid):
    """공개 필드와 현재 출처로만 전체 판정한다. 요청별 페이지는 캐시 밖에서 고른다."""
    from ..stage_mirror import (public_source, public_document, public_live_turn,
                               live_turn_current, live_turn_guess_current, document_source_ready)
    sources = {int(row["id"]): public_source(row)
               for row in store.list_docs("caption_source", sid, limit=ALL_DOCS) if row.get("replayed") is not True}
    for source in sources.values():
        if not live_turn_guess_current(source, sources):
            source.pop("speaker_guess", None)
    documents = []
    valid_document_ids = []
    declaration_sources = list(sources.values())
    for row in store.list_docs("transcript_stage", sid, limit=ALL_DOCS):
        refs = row.get("source_revisions") or {}
        if (row.get("mode") != "closed_block_v1" or row.get("status") == "stale"
                or not refs):
            continue
        if any(not str(uid).isdigit() or int(uid) not in sources
               or sources[int(uid)].get("revision") != revision
               or not document_source_ready(row, sources[int(uid)], declaration_sources=declaration_sources)
               for uid, revision in refs.items()):
            continue
        valid_document_ids.append(row["id"])
        documents.append(public_document(row))
    valid_turns = [public_live_turn(row) for row in store.list_docs("live_turn", sid, limit=ALL_DOCS)
                   if live_turn_current(row, sources)]
    for turn in valid_turns:
        if not live_turn_guess_current(turn, sources):
            turn.pop("speaker_guess", None)
    return {"sources": sources, "documents": documents, "valid_document_ids": sorted(valid_document_ids),
            "live_turns": valid_turns, "valid_live_turn_ids": sorted(row["id"] for row in valid_turns)}


def _current_stage_snapshot(store, sid, fence):
    def build():
        _session(store, sid)
        value = _read_stage_snapshot(store, sid)
        _stage_fence_matches(store, fence)
        return value
    # 최초 push 전의 모의/초기 저장소는 판본 키가 없으므로 저장 캐시를 쓰지 않는다.
    if not fence[0] or fence[1] is None:
        return build()
    return _stage_cache(store).get_or_build(sid, fence, build)


@router.get("/sessions/{sid}/transcript-stages")
async def transcript_stages(request: Request, sid: str, before: int | None = Query(None, gt=0),
                            limit: int = Query(100, ge=1, le=100), block_id: int | None = Query(None, gt=0),
                            user: User = Depends(require_viewer)):
    """공개 전체 판정은 같은 agent 판본에서 재사용하고 응답 직전 다시 판본을 확인한다."""
    snapshot_at = time.time_ns() // 1_000_000
    store = _store(request)
    _session(store, sid)
    fence = _stage_fence(store)
    if block_id is not None and not any(row.get("id") == block_id
            for row in store.list_docs("block", sid, limit=ALL_DOCS)):
        raise HTTPException(404, "질의 블록을 찾을 수 없습니다")
    try:
        snapshot = _current_stage_snapshot(store, sid, fence)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "자막 판본을 읽지 못했습니다. 잠시 뒤 다시 확인하세요") from None
    sources = snapshot["sources"]
    ordered = sorted((row for uid, row in sources.items() if before is None or uid < before), key=lambda row: row["id"])
    page = ordered[-limit:] if block_id is None else []
    page_ids = {row["id"] for row in page}
    documents = [row for row in snapshot["documents"] if (row.get("block_id") == block_id if block_id is not None
                 else page_ids.intersection(row.get("source_ids") or []))]
    live_turns = [row for row in snapshot["live_turns"] if (row.get("block_id") == block_id if block_id is not None
                  else page_ids.intersection(row["source_ids"]))]
    needed = ({uid for row in documents for uid in row.get("source_ids", [])}
              | {int(uid) for row in documents for uid in row.get("source_revisions", {}) if str(uid).isdigit()}
              | {uid for row in live_turns for uid in row["source_ids"]}
              | {int(uid) for row in live_turns for uid in
                 (row.get("speaker_guess") or {}).get("source_revisions", {}) if str(uid).isdigit()})
    needed |= {int(ref) for uid in page_ids | needed for ref in
               (sources.get(uid, {}).get("speaker_guess") or {}).get("source_revisions", {}) if str(ref).isdigit()}
    output = sorted((row for uid, row in sources.items() if uid in page_ids | needed), key=lambda row: row["id"])
    result = {"version": 3, "mode": "closed_block_v1", "snapshot_at": snapshot_at,
            "sources": output, "documents": documents,
            "valid_document_ids": snapshot["valid_document_ids"],
            "live_turns": live_turns, "valid_live_turn_ids": snapshot["valid_live_turn_ids"],
            "next_before": page[0]["id"] if block_id is None and len(page) == limit else None,
            **({"block_id": block_id} if block_id is not None else {})}

    result = deepcopy(result)
    _stage_fence_matches(store, fence)
    return result

@router.get("/utterances/{uid}")
async def get_utterance(request: Request, uid: int, user: User = Depends(require_viewer)):
    store = _store(request)
    utterance = _utterance(store, uid)
    sid = utterance["session_id"]
    session = _session(store, sid)
    block_id = utterance.get("block_id")
    block = store.get_doc("block", str(block_id)) if block_id else None
    rows = store.list_docs("utterance", sid, limit=ALL_DOCS)
    final = sorted((row for row in rows if row.get("is_final")), key=_position)
    position = _position(utterance)
    before = [row for row in final if _position(row) < position]
    after = [row for row in final if _position(row) > position]
    speaker = utterance.get("speaker")
    # 2판은 같은 화자의 최근 20건을 요구한다. 현재 발언은 제외한다.
    same = [row for row in reversed(final) if speaker and row.get("speaker") == speaker and row["id"] != uid][:20]
    items = [row for row in store.list_docs("item", sid, limit=ALL_DOCS)
             if row.get("utterance_id") == uid and _item_matches(row, include_dismissed=True)]
    return {
        "utterance": utterance,
        "block": block,
        "prev": before[-1] if before else None,
        "next": after[0] if after else None,
        "items": items,
        "session": _session_out(session, with_stats=False),
        "same_speaker": same,
        "bookmarked": uid in _bookmarks(store, sid, user).get("ids", []),
    }


@router.get("/items")
async def list_items(request: Request, session_id: str | None = None, kind: str | None = None,
                     status: str | None = None, dept: str | None = None, block_id: int | None = None,
                     include_dismissed: bool = False, format: str = "json",
                     limit: int = Query(2000, ge=1, le=10000),
                     user: User = Depends(require_viewer)):
    if format.lower() == "csv":
        tunnel = getattr(request.app.state, "tunnel", None)
        if tunnel is None:
            raise HTTPException(403, "이 기능은 수집 PC 화면에서만 할 수 있습니다")
        return await tunnel(request)
    if kind and kind not in ITEM_KINDS:
        raise HTTPException(400, "알 수 없는 항목 종류입니다")
    if status and status not in ITEM_STATUSES:
        raise HTTPException(400, "알 수 없는 처리 상태입니다")
    rows = _store(request).list_docs("item", session_id or None, limit=ALL_DOCS)
    return {"items": [row for row in rows if _item_matches(
        row, kind=kind, status=status, dept=dept, block_id=block_id,
        include_dismissed=include_dismissed)][:limit]}


@router.get("/search")
async def search(request: Request, q: str = Query("", max_length=100),
                 session_id: str | None = None, speaker: str | None = Query(None, max_length=40),
                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100_000),
                 user: User = Depends(require_viewer)):
    q = q.strip()
    items = _store(request).search_utterances(q, sid=session_id or None, speaker=speaker or None,
                                             limit=limit, offset=offset) if len(q) >= 2 else []
    return {"items": items, "q": q}


@router.post("/utterances/{uid}/bookmark")
async def bookmark(request: Request, uid: int, body: BookmarkIn | None = None,
                   user: User = Depends(require_viewer)):
    store = _store(request)
    utterance = _utterance(store, uid)
    sid = utterance["session_id"]
    on = store.toggle_bookmark(sid, user.actor, uid, body.note if body else "")
    return {"bookmarked": on}


@router.get("/cloud/audit")
async def cloud_audit(request: Request, limit: int = Query(300, ge=1, le=2000),
                      user: User = Depends(require_operator)):
    return {"items": _store(request).list_audit(limit)}
