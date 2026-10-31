"""클라우드 이벤트를 세션별 SSE로 재전송하고 짧게 연결한다."""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..web.auth import User, require_viewer

router = APIRouter()
SID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def _last_id(header: str | None, query: str | None) -> int | None:
    """헤더가 유효하면 우선하며 잘못된 값은 다음 입력으로 넘긴다."""
    for value in (header, query):
        if value is None or value == "":
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


@router.get("/sessions/{sid}/events")
async def session_events(request: Request, sid: str, last_id: str | None = Query(None),
                         user: User = Depends(require_viewer)):
    """남은 기록 뒤를 이어 읽고 25초 후 정상 종료한다."""
    store = request.app.state.store
    source = getattr(request.app.state.config, "cloud_read_mode", "mirror") == "source"
    if not SID_RE.fullmatch(sid):
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    if source:
        from .source import read, require_source
        require_source(request)
        # 미러 없이 새 회의도 열되, 존재하지 않는 회의는 성공 빈 스트림으로 바꾸지 않는다.
        scope = {**request.scope, "query_string": b""}
        checked = await read(Request(scope, request.receive), f"sessions/{sid}")
        if checked.background:
            await checked.background()
        if checked.status_code != 200:
            return checked
    elif store.get_doc("session", sid) is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    latest = store.max_event_id(sid)
    minimum = store.min_event_id(sid)
    cursor = _last_id(request.headers.get("last-event-id"), last_id)
    if cursor is None:
        cursor = latest
    # 0은 처음부터, min-1은 첫 보존 이벤트를 바로 이어받는 유효한 지점이다.
    resync = cursor > latest or (cursor > 0 and minimum is not None and cursor < minimum - 1)
    if resync:
        cursor = latest
    duration = max(0.0, float(getattr(request.app.state, "sse_duration_s", 25)))
    poll_s = max(0.001, float(getattr(request.app.state, "sse_poll_s", 1)))
    ping_s = max(0.001, float(getattr(request.app.state, "sse_ping_s", 15)))

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        deadline = time.monotonic() + duration
        next_ping = time.monotonic() + ping_s
        yield "retry: 1000\n: connected\n\n"
        if resync:
            yield 'event: resync\ndata: {"reason":"history_unavailable"}\n\n'
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                break
            # store의 읽기 메서드는 매번 commit하며 요청 연결을 계속 재사용한다.
            rows = store.events_since(sid, cursor, limit=2000)
            for row in rows:
                if time.monotonic() >= deadline:
                    return
                cursor = row["id"]
                type_, payload = row["type"], row["data"]
                if source:
                    # 전환 전의 문서 복제 이벤트를 새 원본 판본에 섞지 않는다.
                    if type_ != "source.event" or not isinstance(payload, dict):
                        continue
                    type_, payload = payload.get("type"), payload.get("data")
                    if type_ not in {"utterance.upsert", "utterance.delete", "block.upsert", "item.upsert",
                            "items.reload", "alert", "alert.delete", "summary.update", "session.status", "stats",
                            "analysis.status", "session.deleted", "caption.source", "transcript.stage",
                            "transcript.display", "resync"}:
                        continue
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {cursor}\nevent: {type_}\ndata: {data}\n\n"
            now = time.monotonic()
            if now >= next_ping:
                yield ": ping\n\n"
                next_ping = now + ping_s
            pause = min(poll_s, deadline - time.monotonic(), next_ping - time.monotonic())
            if pause > 0:
                await asyncio.sleep(pause)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })
