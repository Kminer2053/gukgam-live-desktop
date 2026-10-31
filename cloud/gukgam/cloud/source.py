"""원본 PC가 온라인일 때만 기존 명령 통로로 공개 기록을 읽는다."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..web.auth import User, require_viewer
from . import policy
from .mirror import BookmarkIn, _bookmarks
from .tunnel import tunnel

router = APIRouter()
SID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def require_source(request):
    """기존 완료 결과나 오래된 미러를 읽기 전에 현재 원본 연결부터 확인한다."""
    state = request.app.state.store.agent_status()
    if not state.get("online"):
        raise HTTPException(503, "원본 PC가 연결되어 있지 않습니다. PC가 켜지고 연결된 뒤 열람할 수 있습니다")
    if state.get("read_mode") != "source":
        raise HTTPException(503, "원본 PC의 직접 중계 설정을 적용하고 있습니다. 잠시 뒤 다시 확인하세요")


async def read(request: Request, path: str | None = None):
    """공개 어댑터로만 전달한다. 원본과 클라우드의 SSE 번호는 서로 섞지 않는다."""
    require_source(request)
    original = path or request.url.path.removeprefix("/api/")
    if not original.isascii():
        raise HTTPException(404, "자료를 찾을 수 없습니다")
    target_path = "/api/cloud-read/" + original
    if policy.match("GET", target_path, request.url.query) is None:
        raise HTTPException(403, "허용되지 않은 원본 조회입니다")
    sid = original.split("/")[1] if original.startswith("sessions/") else None
    if sid is not None and not SID_RE.fullmatch(sid):
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    is_snapshot = original.endswith("/snapshot")
    # 조회 중 발생한 변경까지 이후 SSE로 받을 수 있도록 원본 조회보다 먼저 잡는다.
    cursor = request.app.state.store.max_event_id(sid) if is_snapshot else None
    scope = dict(request.scope)
    scope["path"] = target_path
    scope["raw_path"] = scope["path"].encode("ascii")
    # GET은 매번 새 원본 조회다. 과거 request_id의 결과를 재사용하지 않는다.
    scope["headers"] = [(key, value) for key, value in scope.get("headers", [])
                        if key.lower() != b"x-gukgam-request-id"]
    if is_snapshot:
        query = dict(parse_qsl(request.url.query, keep_blank_values=True))
        if query.get("bookmarks_only", "").lower() in {"1", "true", "on", "yes"}:
            user = request.app.state.auth.user_from_cookies(request.cookies)
            ids = _bookmarks(request.app.state.store, sid, user).get("ids", [])
            if not isinstance(ids, list) or any(type(uid) is not int or uid <= 0 for uid in ids):
                raise HTTPException(503, "개인 북마크를 확인하지 못했습니다")
            if len(ids) > 2000:
                raise HTTPException(413, "북마크가 너무 많습니다. 질의별로 선택해 주세요")
            query.pop("bookmarks_only", None)
            query["utterance_ids"] = ",".join(map(str, ids))
        query["recent"] = "true"
        query.setdefault("utterance_limit", "100")
        scope["query_string"] = urlencode(query).encode("utf-8")
    forwarded = Request(scope, request.receive)
    response = await tunnel(forwarded)
    if response.status_code == 202:
        return JSONResponse({"detail": "원본 조회가 아직 끝나지 않았습니다. 잠시 뒤 다시 확인하세요"},
                            status_code=503, background=response.background)
    if response.status_code != 200:
        return response
    try:
        data = json.loads(response.body)
        user = request.app.state.auth.user_from_cookies(request.cookies)
        if is_snapshot:
            data["last_event_id"] = cursor
            data["me"] = user.public()
            data["bookmarks"] = _bookmarks(request.app.state.store, sid, user).get("ids", [])
        elif original.startswith("utterances/"):
            data["bookmarked"] = data["utterance"]["id"] in _bookmarks(
                request.app.state.store, data["session"]["id"], user).get("ids", [])
        else:
            return response
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(502, "원본 조회 응답을 확인할 수 없습니다") from None
    return JSONResponse(data, headers={key: value for key, value in response.headers.items()
        if key.lower() in {"x-gukgam-request-id", "cache-control"}}, background=response.background)


@router.get("/sessions")
@router.get("/sessions/{sid}")
@router.get("/sessions/{sid}/snapshot")
@router.get("/sessions/{sid}/transcript-stages")
@router.get("/utterances/{uid}")
@router.get("/search")
async def records(request: Request, user: User = Depends(require_viewer)):
    return await read(request)


@router.get("/items")
async def items(request: Request, user: User = Depends(require_viewer)):
    if request.query_params.get("format", "json").lower() == "csv":
        return await tunnel(request)
    return await read(request)


@router.post("/utterances/{uid}/bookmark")
async def bookmark(request: Request, uid: int, body: BookmarkIn | None = None,
                   user: User = Depends(require_viewer)):
    # 클라우드의 기존 개인 북마크는 보존한다. 원본 존재 확인도 공개 GET만 사용한다.
    scope = dict(request.scope)
    scope["method"] = "GET"
    scope["query_string"] = b""
    scope["headers"] = [(key, value) for key, value in scope.get("headers", [])
                        if key.lower() not in {b"content-length", b"content-type"}]
    async def empty_receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    response = await read(Request(scope, empty_receive), f"utterances/{uid}")
    if response.status_code != 200:
        return response
    try:
        sid = json.loads(response.body)["session"]["id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, "원본 발언을 확인하지 못했습니다") from None
    if response.background:
        await response.background()
    on = request.app.state.store.toggle_bookmark(sid, user.actor, uid, body.note if body else "")
    return {"bookmarked": on}
