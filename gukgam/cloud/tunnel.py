"""클라우드 명령 터널과 수집 PC 인증·push·롱폴·결과 수신."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import math
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTask

from ..web.auth import User, csrf_protect, same_origin
from . import policy, store as cloud_store

router = APIRouter()
REQUEST_BYTES = 1_000_000
PUSH_BYTES = 2_000_000
RESULT_BYTES = 3_000_000
RESULT_JSON_BYTES = 4_100_000
RESULT_TTL_MS = 3_600_000
UNSUPPORTED = "이 기능은 수집 PC 화면에서만 할 수 있습니다"
CONTENT_TYPES = frozenset({"application/json", "text/csv", "text/markdown", "text/plain",
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
COMMAND_FIELDS = ("id", "request_id", "lane", "method", "path", "query", "headers", "body_b64",
                  "actor", "role", "created_at", "deadline_at")


async def _read_body(request: Request, limit: int) -> bytes:
    """길이 헤더를 믿지 않고 스트림을 한도까지 읽는다."""
    length = request.headers.get("content-length", "")
    try:
        if int(length or 0) > limit:
            raise HTTPException(413, "요청 본문이 너무 큽니다")
    except ValueError:
        raise HTTPException(400, "본문 길이 값이 올바르지 않습니다") from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "요청 본문이 너무 큽니다")
        body.extend(chunk)
    return bytes(body)


async def _read_json(request: Request, limit: int) -> dict:
    try:
        data = json.loads(await _read_body(request, limit))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "JSON 본문이 올바르지 않습니다") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON 객체 본문이 필요합니다")
    return data


def _require_agent(request: Request) -> None:
    """수집 PC 토큰은 값이나 비교 실패 이유를 노출하지 않는다."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    expected = request.app.state.config.agent_token
    if scheme.lower() != "bearer" or not token or not hmac.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "수집 PC 인증이 필요합니다")


def _same_origin(request: Request) -> bool:
    """기존 동일 출처 검사에 스킴과 기본 포트 정규화를 더한다."""
    if not same_origin(request):
        return False
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return True
    try:
        given, actual = urlsplit(origin), urlsplit(str(request.url))
        return (given.scheme, given.hostname, given.port or (443 if given.scheme == "https" else 80)) == (
            actual.scheme, actual.hostname, actual.port or (443 if actual.scheme == "https" else 80))
    except ValueError:
        return False


def _audit(request: Request, user: User, kind: str, status: int, command_id: int | None = None) -> None:
    ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",", 1)[0]
    request.app.state.store.audit({"kind": kind, "ip": ip.strip() or (request.client.host if request.client else "unknown"),
        "ua": request.headers.get("user-agent", ""), "name": user.name, "role": user.role,
        "method": request.method, "path": request.url.path, "status": status, "command_id": command_id})


def _reject(request: Request, user: User, status: int, detail: str, command_id: int | None = None) -> Response:
    _audit(request, user, "tunnel_rejected", status, command_id)
    return JSONResponse({"detail": detail}, status_code=status)


def _type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _safe_header(value: str) -> bool:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def _decode_result(result: dict) -> tuple[int, dict[str, str], bytes]:
    """신뢰하지 않는 수집 PC 결과에서 허용된 응답 정보만 꺼낸다."""
    if not isinstance(result, dict):
        raise ValueError("결과 객체가 필요합니다")
    status = result.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
        raise ValueError("결과 상태가 올바르지 않습니다")
    incoming = result.get("headers", {})
    if not isinstance(incoming, dict):
        raise ValueError("결과 헤더가 올바르지 않습니다")
    headers = {key.lower(): value for key, value in incoming.items()
               if isinstance(key, str) and key.lower() in {"content-type", "content-disposition"}}
    headers.setdefault("content-type", "application/json")
    if any(not _safe_header(value) for value in headers.values()) or _type(headers["content-type"]) not in CONTENT_TYPES:
        raise ValueError("허용되지 않은 결과 형식입니다")
    encoded = result.get("body_b64", "")
    if not isinstance(encoded, str):
        raise ValueError("결과 본문이 올바르지 않습니다")
    if len(encoded) > 4 * ((RESULT_BYTES + 2) // 3):
        raise OverflowError("결과 본문이 너무 큽니다")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("결과 본문이 올바르지 않습니다") from None
    if len(body) > RESULT_BYTES:
        raise OverflowError("결과 본문이 너무 큽니다")
    if _type(headers["content-type"]) != "application/json":
        disposition = headers.get("content-disposition", "")
        _, separator, parameters = disposition.partition(";")
        headers["content-disposition"] = "attachment" + (";" + parameters if separator else "")
    return status, headers, body


def _same_command(command: dict, desired: dict) -> bool:
    fields = ("actor", "role", "method", "path", "query", "request_hash")
    return bool(command.get("request_hash")) and all(command.get(key) == desired.get(key) for key in fields)


def _result_response(request: Request, user: User, command: dict) -> Response:
    store = request.app.state.store
    if (not command.get("result") or cloud_store.now_ms() >= int(command.get("done_at") or 0) + RESULT_TTL_MS):
        store.clear_command_payload(command["id"])
        return _reject(request, user, 410, "이 요청의 결과 보관 시간이 지났습니다. 다시 실행하지 않습니다", command["id"])
    try:
        status, headers, body = _decode_result(command["result"])
        response = Response(body, status_code=status, headers=headers)
    except (ValueError, OverflowError):
        response = JSONResponse({"detail": "수집 PC 응답 형식을 확인할 수 없습니다"}, status_code=502)
    response.headers["X-Gukgam-Request-Id"] = command["request_id"]
    # 응답 전송 뒤 입력만 지우고 재접속용 결과와 지문은 보존한다.
    clear = (store.clear_command_payload if command.get("method") == "GET"
             and getattr(request.app.state.config, "cloud_read_mode", "mirror") == "source"
             else store.clear_command_body)
    response.background = BackgroundTask(clear, command["id"])
    _audit(request, user, "tunnel_result", response.status_code, command["id"])
    return response


def _seconds(request: Request, key: str, default: float) -> float:
    value = float(getattr(request.app.state, key, default))
    return max(0.001, value) if math.isfinite(value) else default


async def tunnel(request: Request) -> Response:
    """브라우저 요청을 허용목록과 동일 요청 지문으로 확인한 뒤 전달한다."""
    user = request.app.state.auth.user_from_cookies(request.cookies)
    if user is None:
        raise HTTPException(401, "로그인이 필요합니다")
    if request.method == "GET" and getattr(request.app.state.config, "cloud_read_mode", "mirror") == "source":
        from .source import require_source
        require_source(request)
    try:
        csrf_protect(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(request):
            raise HTTPException(403, "같은 사이트에서 요청해야 합니다")
    except HTTPException as exc:
        return _reject(request, user, exc.status_code, str(exc.detail))
    method, path, query = request.method, request.url.path, request.url.query
    rule = policy.match(method, path, query)
    if rule is None:
        return _reject(request, user, 403, UNSUPPORTED)
    if not rule.allows(user.role):
        return _reject(request, user, 403, "운영자만 할 수 있습니다")
    try:
        body = await _read_body(request, REQUEST_BYTES)
    except HTTPException as exc:
        return _reject(request, user, exc.status_code, str(exc.detail))
    violation = policy.check_body(rule, body)
    if violation:
        return _reject(request, user, 403, violation)
    request_id = request.headers.get("x-gukgam-request-id") or str(uuid.uuid4())
    if len(request_id) > 200 or not _safe_header(request_id):
        return _reject(request, user, 400, "요청 식별자가 올바르지 않습니다")
    content_type = request.headers.get("content-type", "")
    if not _safe_header(content_type):
        return _reject(request, user, 400, "요청 본문 형식이 올바르지 않습니다")
    desired = {"request_id": request_id, "actor": "☁" + user.name, "role": user.role,
        "method": method, "path": path, "query": query, "body": body, "lane": rule.lane,
        "headers": {"content-type": content_type} if content_type else {},
        "request_hash": cloud_store.command_fingerprint(method, path, query, content_type, body)}
    store = request.app.state.store
    command = store.get_command_by_request_id(request_id)
    if command is not None:
        if not _same_command(command, desired):
            return _reject(request, user, 409, "같은 요청 식별자를 다른 작업에 사용할 수 없습니다")
        if command["status"] == "done":
            return _result_response(request, user, command)
        if command["status"] == "expired":
            store.clear_command_payload(command["id"])
            return _reject(request, user, 410, "이 요청은 만료됐습니다. 다시 실행하지 않습니다", command["id"])
    else:
        state = store.agent_status()
        if not state["online"]:
            heartbeat = int(state.get("heartbeat_at") or 0)
            last = datetime.fromtimestamp(heartbeat / 1000, ZoneInfo("Asia/Seoul")).strftime("%H:%M") if heartbeat else "없음"
            return _reject(request, user, 503,
                f"수집 PC가 연결되어 있지 않습니다(마지막 신호 {last}). 지금은 기록 열람만 됩니다.")
        if state.get("mode") == "idle":
            store.mark_wanted()
            return _reject(request, user, 503, "수집 PC 대기 중 — 1~5분 안에 연결됩니다. 잠시 뒤 다시 시도하세요.")
        duration = _seconds(request, "tunnel_slow_s" if rule.lane == "slow" else "tunnel_fast_s", 55 if rule.lane == "slow" else 25)
        created = cloud_store.now_ms()
        desired.update(created_at=created, deadline_at=created + int(duration * 1000))
        command = store.enqueue_command(desired)
        # 동시 요청의 INSERT 경합도 기존 동작과 같은지 재검사한다.
        if not _same_command(command, desired):
            return _reject(request, user, 409, "같은 요청 식별자를 다른 작업에 사용할 수 없습니다")
        _audit(request, user, "tunnel_request", 202, command["id"])
    deadline = time.monotonic() + max(0, (command["deadline_at"] - cloud_store.now_ms()) / 1000)
    poll = _seconds(request, "tunnel_poll_s", 0.5)
    while True:
        command = store.get_command(command["id"])
        if command is None or command["status"] == "expired":
            if command is not None:
                store.clear_command_payload(command["id"])
            return _reject(request, user, 410, "이 요청은 만료됐습니다. 다시 실행하지 않습니다")
        if command["status"] == "done":
            return _result_response(request, user, command)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if store.expire_command(command["id"]):
                response = _reject(request, user, 504, "수집 PC 응답이 늦습니다. 잠시 뒤 다시 시도하세요.", command["id"])
                response.background = BackgroundTask(store.clear_command_body, command["id"])
                return response
            # 만료 직전에 도착한 결과를 한 번 더 읽는다.
            latest = store.get_command(command["id"])
            if latest and latest["status"] == "done":
                return _result_response(request, user, latest)
            if not latest or latest["status"] == "expired":
                if latest:
                    store.clear_command_payload(command["id"])
                return _reject(request, user, 410, "이 요청은 만료됐습니다. 다시 실행하지 않습니다", command["id"])
            response = JSONResponse({"detail": "수집 PC가 처리 중입니다. 다시 누르지 말고 잠시 뒤 확인하세요.",
                                    "command_id": command["id"]}, status_code=202)
            response.headers["X-Gukgam-Request-Id"] = request_id
            response.background = BackgroundTask(store.clear_command_body, command["id"])
            _audit(request, user, "tunnel_result", 202, command["id"])
            return response
        await asyncio.sleep(min(poll, remaining))


@router.post("/agent/push", dependencies=[Depends(_require_agent)])
async def push(request: Request):
    data = await _read_json(request, PUSH_BYTES)
    if (not isinstance(data.get("boot_id"), str) or not data["boot_id"]
            or isinstance(data.get("seq"), bool) or not isinstance(data.get("seq"), int) or data["seq"] < 1
            or not isinstance(data.get("ops"), list) or any(not isinstance(op, dict) for op in data["ops"])):
        raise HTTPException(400, "push 요청 값이 올바르지 않습니다")
    try:
        return request.app.state.store.apply_push(data["boot_id"], data["seq"], data["ops"])
    except (ValueError, KeyError, TypeError, OverflowError):
        raise HTTPException(400, "push 요청 값이 올바르지 않습니다") from None


@router.post("/agent/heartbeat", dependencies=[Depends(_require_agent)])
async def heartbeat(request: Request):
    data = await _read_json(request, PUSH_BYTES)
    mode = data.get("mode", "idle")
    if not isinstance(mode, str) or mode not in {"active", "idle"}:
        raise HTTPException(400, "수집 PC 상태 값이 올바르지 않습니다")
    for key in ("interval_s", "queue", "last_push_ok_ms", "sessions_live"):
        if key in data and (isinstance(data[key], bool) or not isinstance(data[key], int)
                            or data[key] < (1 if key == "interval_s" else 0)):
            raise HTTPException(400, "수집 PC 상태 값이 올바르지 않습니다")
    return request.app.state.store.heartbeat(data)


@router.get("/agent/commands", dependencies=[Depends(_require_agent)])
async def commands(request: Request):
    try:
        wait = float(request.query_params.get("wait", "20"))
        if not math.isfinite(wait) or wait < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "대기 시간 값이 올바르지 않습니다") from None
    deadline = time.monotonic() + min(20, wait)
    store = request.app.state.store
    poll = _seconds(request, "command_poll_s", 0.5)
    while True:
        rows = store.take_commands(limit=10, max_bytes=RESULT_BYTES)
        if rows or time.monotonic() >= deadline or await request.is_disconnected():
            return {"commands": [{key: row.get(key) for key in COMMAND_FIELDS} for row in rows], "wanted": store.wanted()}
        await asyncio.sleep(min(poll, max(0, deadline - time.monotonic())))


@router.post("/agent/commands/{id}/result", dependencies=[Depends(_require_agent)])
async def result(request: Request, id: int):
    data = await _read_json(request, RESULT_JSON_BYTES)
    status = data.get("status")
    if (isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599
            or not isinstance(data.get("headers", {}), dict) or not isinstance(data.get("body_b64", ""), str)):
        raise HTTPException(400, "수집 PC 결과 값이 올바르지 않습니다")
    try:
        _, headers, _ = _decode_result(data)
    except OverflowError:
        raise HTTPException(413, "수집 PC 결과 본문이 너무 큽니다") from None
    except ValueError:
        # 금지된 형식도 명령 결과로 남겨 브라우저에서는 안전한 502를 돌려준다.
        headers = data.get("headers", {})
        try:
            decoded = base64.b64decode(data.get("body_b64", ""), validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(400, "수집 PC 결과 본문이 올바르지 않습니다") from None
        if len(decoded) > RESULT_BYTES:
            raise HTTPException(413, "수집 PC 결과 본문이 너무 큽니다")
    # 결과 헤더는 전달 허용목록만 보존한다. 형식 검사는 반환 때도 반복한다.
    if not isinstance(headers, dict):
        raise HTTPException(400, "수집 PC 결과 헤더가 올바르지 않습니다")
    saved = {"status": data["status"], "headers": {key.lower(): value for key, value in headers.items()
             if isinstance(key, str) and key.lower() in {"content-type", "content-disposition"}},
             "body_b64": data.get("body_b64", "")}
    if not request.app.state.store.finish_command(id, saved):
        raise HTTPException(409, "이미 완료됐거나 만료된 명령입니다")
    return {"ok": True}
