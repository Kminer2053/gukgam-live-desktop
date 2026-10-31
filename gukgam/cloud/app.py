"""클라우드 앱: 강한 기동 설정·인증·미러 페이지·공통 보안 처리."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import jinja2
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__
from ..config import Config
from ..web.auth import Auth, User, clean_name, csrf_protect, same_origin
from .store import CloudStore, make_store, now_ms

log = logging.getLogger(__name__)
COOKIE_NAME = "__Host-gukgam_auth"
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
SID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
NOTICE = "AI 음성인식·요약 결과로 오류가 있을 수 있습니다 · 내부 업무용(국회법 제149조② 상업적 이용 금지)"
UNAVAILABLE = "클라우드 서비스를 일시적으로 사용할 수 없습니다"
UNSUPPORTED = "이 기능은 수집 PC 화면에서만 할 수 있습니다"
CSP = "default-src 'self'; frame-ancestors 'none'"


class CloudAuth(Auth):
    """서명 알고리즘을 재사용하며 로컬 비밀 파일은 읽거나 만들지 않는다."""

    def __init__(self, config: Config, *, ttl_s: int = 12 * 3600):
        self.config = config
        self.ttl_s = ttl_s
        self._secret = config.secret_key.encode("utf-8")
        self._key = hmac.new(self._secret, b"gukgam-cookie-v1", hashlib.sha256).digest()

    def issue(self, user: User) -> str:
        now = int(time.time())
        return self.sign({"r": user.role, "n": clean_name(user.name)[:18], "iat": now,
                          "exp": now + self.ttl_s, "pv": self._passver(user.role)})

    def user_from_cookies(self, cookies) -> User | None:
        try:
            user = self.user_from_token(cookies.get(COOKIE_NAME))
        except (ValueError, TypeError, OverflowError):
            return None
        return User(user.role, clean_name(user.name)[:18]) if user else None


def _same_origin(request: Request) -> bool:
    """기존 출처 검사에 스킴·정규화 포트 검사를 더한다."""
    if not same_origin(request):
        return False
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return True
    try:
        given = urlsplit(origin)
        actual = urlsplit(str(request.url))
        given_port = given.port or (443 if given.scheme == "https" else 80)
        actual_port = actual.port or (443 if actual.scheme == "https" else 80)
        return (given.scheme, given.hostname, given_port) == (actual.scheme, actual.hostname, actual_port)
    except ValueError:
        return False


class CloudMiddleware:
    """응답 완료까지 요청 연결을 유지하는 순수 ASGI 미들웨어."""

    def __init__(self, app: ASGIApp, *, owner: FastAPI):
        self.app = app
        self.owner = owner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_api = path == "/api" or path.startswith("/api/")
        started = False

        async def secured_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in {b"x-content-type-options", b"content-security-policy"}]
                headers.extend([(b"x-content-type-options", b"nosniff"),
                                (b"content-security-policy", CSP.encode("ascii"))])
                if is_api and not any(key.lower() == b"cache-control" for key, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        async def error(status: int, detail: str) -> None:
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, secured_send)

        if not self.owner.state.ready:
            await error(503, UNAVAILABLE)
            return
        try:
            with self.owner.state.store.request_scope():
                request = Request(scope, receive)
                user = self.owner.state.auth.user_from_cookies(request.cookies)
                request.state.user = user
                if is_api:
                    self.owner.state.store.mark_wanted()
                    is_login = path == "/api/auth/login" and request.method == "POST"
                    if not is_login and user is None:
                        await error(401, "로그인이 필요합니다")
                        return
                    try:
                        csrf_protect(request)
                    except HTTPException as exc:
                        _audit(request, "request_rejected", user=user, status=exc.status_code)
                        await error(exc.status_code, str(exc.detail))
                        return
                    if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(request):
                        _audit(request, "request_rejected", user=user, status=403)
                        await error(403, "같은 사이트에서 요청해야 합니다")
                        return
                elif path in {"/login", "/logout"} and request.method == "POST" and not _same_origin(request):
                    await error(403, "같은 사이트에서 요청해야 합니다")
                    return
                await self.app(scope, receive, secured_send)
        except HTTPException as exc:
            if not started:
                await error(exc.status_code, str(exc.detail))
        except Exception as exc:
            # 드라이버 오류 메시지에는 접속 정보가 들어갈 수 있으므로 종류만 기록한다.
            log.error("클라우드 요청 처리 실패: %s", type(exc).__name__)
            if not started:
                await error(503, UNAVAILABLE)


def _safe_next(path: Any) -> str:
    path = str(path or "").strip()
    decoded = unquote(path)
    if (not path.startswith("/") or decoded.startswith("//") or "\\" in decoded
            or any(char in decoded for char in "\r\n")):
        return "/"
    return path


def _user(request: Request) -> User | None:
    return request.app.state.auth.user_from_cookies(request.cookies)


def _operator_connection_id(state: dict, user: User | None) -> str | None:
    """현재 원본 연결 식별자는 인증된 운영자에게만 제한된 형식으로 보인다."""
    value = state.get("connection_id")
    return value if (user is not None and user.is_operator and isinstance(value, str)
                     and re.fullmatch(r"[0-9a-f]{32}", value)) else None


def _guard(request: Request, *, operator: bool = False) -> User | Response:
    user = _user(request)
    if user is None or operator and not user.is_operator:
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        error = "&error=operator" if user else ""
        return RedirectResponse(f"/login?next={quote(nxt, safe='/')}{error}", status_code=303)
    return user


def _render(request: Request, template: str, user: User | None, *, status_code: int = 200, **ctx: Any) -> Response:
    state = request.app.state
    agent = state.store.agent_status()
    connection_id = _operator_connection_id(agent, user)
    agent.pop("connection_id", None)
    if connection_id is not None:
        agent["connection_id"] = connection_id
    context = {"request": request, "me": user.public() if user else {"role": "", "name": "", "role_label": ""},
               "auth_enabled": True, "auth_warning": "", "version": __version__, "notice": NOTICE,
               "shared_passcode": state.shared_passcode,
               "page": template.rsplit(".", 1)[0], "sid": None, "uid": None, "bid": None,
               "cloud": True, "cloud_read_mode": getattr(state.config, "cloud_read_mode", "mirror"),
               "agent": agent}
    context.update(ctx)
    return state.templates.TemplateResponse(request, template, context, status_code=status_code)


def _session(request: Request, sid: str) -> dict:
    if getattr(request.app.state.config, "cloud_read_mode", "mirror") == "source":
        if not SID_RE.fullmatch(sid):
            raise HTTPException(404, "세션을 찾을 수 없습니다")
        # 본문은 인증된 원본 API로 읽는다. 페이지 틀을 만들며 큰 미러를 조회하지 않는다.
        return {"id": sid, "title": "국감 회의"}
    session = request.app.state.store.get_doc("session", sid) if SID_RE.fullmatch(sid) else None
    if session is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    return session


def _client_ip(request: Request) -> str:
    value = request.headers.get("x-real-ip")
    if value:
        return value.strip()
    value = request.headers.get("x-forwarded-for")
    if value:
        return value.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _audit(request: Request, kind: str, *, user: User | None = None, name: str = "", status: int = 200) -> None:
    request.app.state.store.audit({"kind": kind, "ip": _client_ip(request),
        "ua": request.headers.get("user-agent", ""), "name": user.name if user else name,
        "role": user.role if user else "", "method": request.method, "path": request.url.path, "status": status})


async def _login(request: Request, passcode: str, name: str) -> User:
    store = request.app.state.store
    name = clean_name(name)[:18]
    ip = _client_ip(request)
    limits = store.login_status(ip)
    if limits["global_slow"]:
        await asyncio.sleep(2)
    if limits["blocked"]:
        _audit(request, "login_blocked", name=name, status=429)
        raise HTTPException(429, "로그인 시도가 너무 많습니다. 10분 뒤 다시 시도하세요")
    role = request.app.state.auth.check_passcode(passcode)
    store.login_attempt(ip, role is not None)
    if role is None:
        _audit(request, "login_fail", name=name, status=401)
        raise HTTPException(401, "패스코드가 올바르지 않습니다")
    user = User(role, name)
    _audit(request, "login_ok", user=user)
    return user


def _set_cookie(response: Response, auth: CloudAuth, user: User) -> None:
    response.set_cookie(COOKIE_NAME, auth.issue(user), max_age=auth.ttl_s, path="/", secure=True,
                        httponly=True, samesite="lax")


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")


async def _login_body(request: Request) -> dict:
    if "application/json" in request.headers.get("content-type", ""):
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "JSON 본문이 올바르지 않습니다") from None
    else:
        form = await request.form(max_files=0, max_fields=10)
        data = dict(form)
    if not isinstance(data, dict) or any(not isinstance(data.get(key, ""), str) for key in ("passcode", "name")):
        raise HTTPException(422, "로그인 요청 값이 올바르지 않습니다")
    return data


def create_cloud_app(store: CloudStore | None = None) -> FastAPI:
    """팩토리 호출에서만 설정·저장소를 선택한다. import는 파일·DB를 만들지 않는다."""
    app = FastAPI(title="국감 라이브 클라우드", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ready = False
    app.state.shared_passcode = False
    app.state.version = __version__
    app.state.notice = NOTICE
    app.state.store = store
    app.state.sse_duration_s = 25
    app.state.sse_poll_s = 1
    app.state.sse_ping_s = 15
    problems: list[str] = []
    try:
        config = Config()
        ttl_s = int(float(os.environ.get("GUKGAM_SESSION_TTL_H", "12")) * 3600)
        personal_test = os.environ.get("GUKGAM_CLOUD_PERSONAL_TEST") == "1"
        if not config.operator_passcode or (not personal_test and len(config.operator_passcode) < 16):
            problems.append("운영자 패스코드 길이 부족")
        if not config.viewer_passcode or (not personal_test and len(config.viewer_passcode) < 12):
            problems.append("열람자 패스코드 길이 부족")
        # 개인 시험을 명시한 경우만 공통 패스코드를 허용한다(인증은 운영자 우선).
        app.state.shared_passcode = (personal_test
                                    and config.operator_passcode == config.viewer_passcode)
        if config.operator_passcode == config.viewer_passcode and not app.state.shared_passcode:
            problems.append("역할 패스코드 중복")
        if len(config.secret_key.encode("utf-8")) < 32:
            problems.append("서명 키 길이 부족")
        if len(config.agent_token.encode("utf-8")) < 32:
            problems.append("수집 PC 토큰 길이 부족")
        if ttl_s <= 0:
            problems.append("쿠키 유효 시간 설정 오류")
        if os.environ.get("VERCEL") == "1" and not (
                os.environ.get("MONGODB_URI") or os.environ.get("DATABASE_URL")):
            problems.append("운영 DB 설정 없음")
        app.state.config = config
        if not problems:
            app.state.store = store if store is not None else make_store()
            app.state.auth = CloudAuth(config, ttl_s=ttl_s)
            app.state.ready = True
    except Exception as exc:
        problems.append(f"설정 또는 저장소 준비 실패({type(exc).__name__})")
    if problems:
        log.error("클라우드 기동 설정 검증 실패: %s", "; ".join(problems))

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.undefined = jinja2.ChainableUndefined
    templates.env.globals.update(version=__version__, notice=NOTICE)
    app.state.templates = templates
    if os.environ.get("VERCEL") != "1":
        app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.post("/api/auth/login")
    async def api_login(request: Request):
        data = await _login_body(request)
        user = await _login(request, data.get("passcode", ""), data.get("name", ""))
        response = JSONResponse({"ok": True, "auth_enabled": True, **user.public()})
        _set_cookie(response, app.state.auth, user)
        return response

    @app.post("/api/auth/logout")
    async def api_logout(request: Request):
        _audit(request, "logout", user=_user(request))
        response = JSONResponse({"ok": True})
        _clear_cookie(response)
        return response

    @app.get("/api/auth/me")
    async def api_me(request: Request):
        return {"auth_enabled": True, **_user(request).public()}

    @app.get("/api/health")
    async def health(request: Request):
        state = app.state.store.agent_status()
        agent = {key: state[key] for key in ("online", "mode", "heartbeat_at", "push_delay_ms", "queue")}
        agent["read_mode"] = state.get("read_mode", "mirror")
        connection_id = _operator_connection_id(state, _user(request))
        if connection_id is not None:
            agent["connection_id"] = connection_id
        warnings = ["로그인 실패가 많아 로그인 응답을 늦추고 있습니다"] if app.state.store.login_status()["global_slow"] else []
        return {"ok": True, "cloud": True, "read_mode": getattr(app.state.config, "cloud_read_mode", "mirror"),
                "version": __version__, "agent": agent,
                "warnings": warnings, "time": now_ms()}

    if getattr(getattr(app.state, "config", None), "cloud_read_mode", "mirror") == "source":
        from .source import router as source_router
        app.include_router(source_router, prefix="/api")
    from .mirror import router as mirror_router
    app.include_router(mirror_router, prefix="/api")
    from .stream import router as stream_router
    app.include_router(stream_router, prefix="/api")

    from .tunnel import router as agent_router, tunnel
    app.include_router(agent_router)
    app.state.tunnel = tunnel
    app.state.tunnel_fast_s = 25
    app.state.tunnel_slow_s = 55
    app.state.tunnel_poll_s = 0.5
    app.state.command_poll_s = 0.5

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def unsupported(request: Request, path: str):
        if path == "cloud-read" or path.startswith("cloud-read/"):
            # 이 이름은 서버가 원본으로 전달할 때만 쓴다. 공개 별도 API가 아니다.
            raise HTTPException(403, "허용되지 않은 원본 조회입니다")
        if request.method == "POST" and re.fullmatch(r"sessions/[^/]+/upload", path):
            _audit(request, "request_rejected", user=_user(request), status=400)
            raise HTTPException(400, "녹음 파일 업로드는 수집 PC 화면에서 하세요")
        return await tunnel(request)

    @app.websocket("/ws/ingest/{sid}")
    async def unsupported_ingest(websocket: WebSocket, sid: str):
        await websocket.close(code=1008)

    @app.get("/", include_in_schema=False)
    async def dashboard(request: Request):
        user = _guard(request)
        return user if isinstance(user, Response) else _render(request, "dashboard.html", user)

    @app.get("/sessions/new", include_in_schema=False)
    async def session_new(request: Request):
        user = _guard(request, operator=True)
        if isinstance(user, Response):
            return user
        return _render(request, "session_new.html", user, xcode=request.query_params.get("xcode", "")[:40],
                       xcgcd=request.query_params.get("xcgcd", "")[:40])

    @app.get("/s/{sid}", include_in_schema=False)
    async def session_page(request: Request, sid: str):
        user = _guard(request)
        if isinstance(user, Response):
            return user
        session = _session(request, sid)
        try:
            bid = int(request.query_params.get("b") or request.query_params.get("bid") or "")
        except ValueError:
            bid = None
        return _render(request, "session.html", user, sid=sid, bid=bid, session_title=session["title"])

    @app.get("/s/{sid}/report", include_in_schema=False)
    async def report_page(request: Request, sid: str):
        user = _guard(request)
        if isinstance(user, Response):
            return user
        session = _session(request, sid)
        return _render(request, "report.html", user, sid=sid, session_title=session["title"])

    @app.get("/u/{uid}", include_in_schema=False)
    async def utterance_page(request: Request, uid: int):
        user = _guard(request)
        if isinstance(user, Response):
            return user
        if getattr(app.state.config, "cloud_read_mode", "mirror") == "source":
            return _render(request, "utterance.html", user, uid=uid)
        utterance = app.state.store.get_doc("utterance", str(uid))
        if utterance is None:
            raise HTTPException(404, "발언을 찾을 수 없습니다")
        return _render(request, "utterance.html", user, uid=uid, sid=utterance["session_id"], bid=utterance.get("block_id"))

    @app.get("/items", include_in_schema=False)
    async def items_page(request: Request):
        user = _guard(request)
        if isinstance(user, Response):
            return user
        sid = request.query_params.get("session_id") or None
        return _render(request, "items.html", user, sid=sid if sid and SID_RE.fullmatch(sid) else None)

    @app.get("/search", include_in_schema=False)
    async def search_page(request: Request):
        user = _guard(request)
        return user if isinstance(user, Response) else _render(request, "search.html", user, q=request.query_params.get("q", "")[:100])

    @app.get("/admin", include_in_schema=False)
    async def admin_page(request: Request):
        user = _guard(request, operator=True)
        return user if isinstance(user, Response) else _render(request, "admin.html", user)

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        messages = {"1": "패스코드가 올바르지 않습니다.",
                    "rate": "로그인 시도가 너무 많습니다. 10분 뒤 다시 시도하세요.",
                    "operator": "운영자 권한이 필요한 화면입니다. 운영자 패스코드로 로그인하세요."}
        return _render(request, "login.html", _user(request), next=_safe_next(request.query_params.get("next")),
                       error=messages.get(request.query_params.get("error", ""), ""))

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request):
        data = await _login_body(request)
        nxt = _safe_next(data.get("next"))
        try:
            user = await _login(request, data.get("passcode", ""), data.get("name", ""))
        except HTTPException as exc:
            if exc.status_code == 429:
                return _render(request, "login.html", _user(request), status_code=429, next=nxt, error=exc.detail)
            return RedirectResponse(f"/login?error=1&next={quote(nxt, safe='/')}", status_code=303)
        response = RedirectResponse(nxt, status_code=303)
        _set_cookie(response, app.state.auth, user)
        return response

    @app.api_route("/logout", methods=["GET", "POST"], include_in_schema=False)
    async def logout(request: Request):
        user = _user(request)
        if user:
            _audit(request, "logout", user=user)
        response = RedirectResponse("/login", status_code=303)
        _clear_cookie(response)
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) and not exc.detail.isascii() else "요청을 처리할 수 없습니다"
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "요청 값이 올바르지 않습니다"}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        log.error("클라우드 처리 실패: %s", type(exc).__name__)
        return JSONResponse({"detail": UNAVAILABLE}, status_code=503)

    app.add_middleware(CloudMiddleware, owner=app)
    return app
