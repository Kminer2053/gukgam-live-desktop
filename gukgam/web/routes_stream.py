"""실시간 스트림: SSE(세션 이벤트)와 WebSocket(브라우저 음성 수집)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from .auth import User, require_viewer, same_origin, ws_user

log = logging.getLogger(__name__)

router = APIRouter()

PING_SEC = 15.0                    # keep-alive 주석 주기
RETRY_MS = 3000                    # 브라우저 재연결 대기
MAX_PCM_FRAME = 512 * 1024         # WS 바이너리 프레임 최대 크기(16kHz s16le 약 16초)
SAMPLE_RATE = 16000

# WS 종료 코드(4000번대 = 앱 정의)
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403
WS_NOT_FOUND = 4404
WS_NOT_READY = 4409
WS_BAD_INPUT = 4400


def _parse_last_id(header: str | None, query: int | None) -> int | None:
    for v in (header, query):
        if v is None or v == "":
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            return n
    return None


def _resync_reason(bus, sid: str, last_id: int | None) -> str | None:
    """재전송으로 메울 수 없는 경우(서버 재시작·기록 초과) 클라이언트에 스냅샷 재요청을 알린다.

    이벤트 id 는 프로세스 기동 시각 기반으로 시작하므로(EventBus.start_id) 이전 프로세스에서 받은 id 는
    start_id 보다 작다. 새 프로세스가 이벤트를 많이 발행한 뒤에 늦게 재연결해도 재기동을 알아챈다.
    last_id=0 은 '처음부터'(남아 있는 기록 전체 재전송)라는 뜻으로 그대로 둔다.
    """
    if last_id is None:
        return None
    if last_id > bus.last_id or 0 < last_id < bus.start_id:
        return "server_restarted"
    if bus.lost_since(sid, last_id):
        return "history_overflow"
    return None


@router.get("/api/sessions/{sid}/events")
async def session_events(request: Request, sid: str, last_id: int | None = Query(None, ge=0),
                         user: User = Depends(require_viewer)):
    """SSE. Last-Event-ID 헤더(또는 ?last_id=) 이후 이벤트를 재전송하고 실시간으로 이어 보낸다."""
    rt = request.app.state.rt
    if rt.repo.get_session(sid) is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    lid = _parse_last_id(request.headers.get("last-event-id"), last_id)
    bus = rt.bus

    async def gen() -> AsyncIterator[str]:
        yield f"retry: {RETRY_MS}\n: connected\n\n"
        reason = _resync_reason(bus, sid, lid)
        start_id = lid
        if reason:
            # id 없이 보낸다(클라이언트의 Last-Event-ID 를 바꾸지 않게) → 클라이언트는 스냅샷을 다시 받는다
            yield f"event: resync\ndata: {json.dumps({'reason': reason})}\n\n"
            start_id = None
        async for ev in bus.subscribe(sid, last_id=start_id, heartbeat=PING_SEC):
            if ev is None:
                if await request.is_disconnected():
                    break
                yield ": ping\n\n"
                continue
            yield ev.sse()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


async def _ws_fail(ws: WebSocket, code: int, message: str) -> None:
    try:
        await ws.send_json({"type": "error", "message": message})
        await ws.close(code=code, reason=message[:100].encode("utf-8")[:120].decode("utf-8", "ignore"))
    except Exception:
        pass


@router.websocket("/ws/ingest/{sid}")
async def ws_ingest(ws: WebSocket, sid: str):
    """브라우저 마이크·탭 음성 수집. 바이너리 프레임 = 16kHz mono s16le PCM.

    텍스트 프레임: {"type":"hello","sample_rate":16000} → {"type":"ready"} / {"type":"ping"} → {"type":"pong"}
    오류는 {"type":"error","message"} 를 보낸 뒤 4000번대 코드로 닫는다.
    """
    if not same_origin(ws):
        await ws.close(code=WS_FORBIDDEN)  # 교차 사이트 WebSocket 차단(수락 전 거부)
        return
    await ws.accept()
    user = ws_user(ws)
    if user is None:
        await _ws_fail(ws, WS_UNAUTHORIZED, "로그인이 필요합니다")
        return
    if not user.is_operator:
        await _ws_fail(ws, WS_FORBIDDEN, "운영자 권한이 필요합니다")
        return
    rt = ws.app.state.rt
    s = rt.repo.get_session(sid)
    if s is None:
        await _ws_fail(ws, WS_NOT_FOUND, "세션을 찾을 수 없습니다")
        return
    if s["source_type"] != "browser":
        await _ws_fail(ws, WS_BAD_INPUT, "브라우저 음성 수집 세션이 아닙니다")
        return
    if rt.ingest.browser_feed(sid) is None:
        await _ws_fail(ws, WS_NOT_READY, "수집 중이 아닙니다. 먼저 수집을 시작하세요.")
        return
    rt.repo.audit(user.actor, "ingest.browser_connect", sid)
    received = 0
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                if len(data) > MAX_PCM_FRAME:
                    await _ws_fail(ws, WS_BAD_INPUT, "음성 프레임이 너무 큽니다")
                    return
                feed = rt.ingest.browser_feed(sid)   # 일시정지·종료되면 None
                if feed is None:
                    await _ws_fail(ws, WS_NOT_READY, "수집이 멈췄습니다. 다시 시작한 뒤 연결하세요.")
                    return
                try:
                    feed(data)
                except Exception:
                    log.exception("브라우저 음성 전달 실패: %s", sid)
                    await _ws_fail(ws, 1011, "음성 처리 중 오류가 발생했습니다")
                    return
                received += len(data)
                continue
            text = msg.get("text")
            if text is None:
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                obj = {}
            kind = obj.get("type") if isinstance(obj, dict) else None
            if kind == "hello":
                rate = obj.get("sample_rate", SAMPLE_RATE)
                if rate != SAMPLE_RATE:
                    await _ws_fail(ws, WS_BAD_INPUT, f"표본화율은 {SAMPLE_RATE}Hz 여야 합니다(받은 값: {rate})")
                    return
                await ws.send_json({"type": "ready", "sample_rate": SAMPLE_RATE})
            elif kind == "ping":
                await ws.send_json({"type": "pong", "received_bytes": received})
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("WS 수집 오류: %s", sid)
    finally:
        log.info("브라우저 음성 연결 종료: %s (%d bytes)", sid, received)
