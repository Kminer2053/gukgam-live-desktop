"""FastAPI 앱 조립: 런타임 수명주기, 템플릿·정적 파일, 라우터, 공통 오류 응답, 보안 헤더."""
from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import jinja2
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__
from ..config import Config
from ..runtime import Runtime
from .auth import Auth

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# 화면 상시 고지(결정사항 5.7)
NOTICE = "AI 음성인식·요약 결과로 오류가 있을 수 있습니다 · 내부 업무용(국회법 제149조② 상업적 이용 금지)"
AUTH_WARNING = "인증이 꺼져 있습니다(로컬 개발 모드) — 접속한 모든 사용자가 운영자 권한을 가집니다."

_STATUS_KO = {
    400: "잘못된 요청입니다", 401: "로그인이 필요합니다", 403: "권한이 없습니다", 404: "찾을 수 없습니다",
    405: "허용되지 않는 요청 방식입니다", 409: "현재 상태에서는 처리할 수 없습니다", 413: "요청이 너무 큽니다",
    422: "요청 값이 올바르지 않습니다", 429: "요청이 너무 많습니다", 500: "서버 오류가 발생했습니다",
    503: "일시적으로 사용할 수 없습니다",
}


class SecurityHeaders:
    """순수 ASGI 미들웨어(SSE 스트리밍을 감싸지 않도록 BaseHTTPMiddleware 를 쓰지 않는다)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_api = path.startswith("/api/")
        is_static = path.startswith("/static/")

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                names = {k.lower() for k, _ in headers}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"referrer-policy", b"same-origin"),
                    (b"content-security-policy", b"frame-ancestors 'self'"),
                    (b"permissions-policy", b"microphone=(self), camera=(), geolocation=()"),
                ]
                if is_api:
                    extra.append((b"cache-control", b"no-store"))
                elif is_static:
                    # 같은 버전 문자열로 JS 를 고쳐도 옛 캐시를 쓰지 않도록 매번 ETag 재검증
                    extra.append((b"cache-control", b"no-cache"))
                headers += [(k, v) for k, v in extra if k not in names]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


def _ko_detail(status: int, detail: Any) -> Any:
    if isinstance(detail, str) and detail and not detail.isascii():
        return detail
    if detail in (None, "") or isinstance(detail, str):
        return _STATUS_KO.get(status, detail or "오류가 발생했습니다")
    return detail


def _html_error(status: int, message: str) -> HTMLResponse:
    body = (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>국감 라이브</title>"
        "<style>body{font-family:system-ui,-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;"
        "margin:0;padding:48px 16px;color:#1f2937;background:#fff}main{max-width:560px;margin:auto}"
        "a{color:#1d6ff2}@media (prefers-color-scheme:dark){body{background:#0f1115;color:#e5e7eb}}</style>"
        f"</head><body><main><h1>{status}</h1><p>{html.escape(message)}</p>"
        "<p><a href='/'>처음 화면으로</a></p></main></body></html>"
    )
    return HTMLResponse(body, status_code=status)


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "text/html" not in request.headers.get("accept", "")


def create_app(config: Config | None = None, *, autostart: list[str] | None = None) -> FastAPI:
    """앱 팩토리. autostart: 기동 직후 수집을 시작할 세션 id(리플레이 시연용)."""
    config = config or Config()
    config.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = Runtime(config)
        await rt.start()
        app.state.rt = rt
        await rt.attach_app(app)
        for sid in autostart or []:
            try:
                await rt.ingest.start(sid, actor="replay-demo")
            except Exception:  # 시연 시작 실패가 서버를 막지 않게
                log.exception("자동 시작 실패: %s", sid)
        try:
            yield
        finally:
            await rt.shutdown()

    app = FastAPI(title="국감 라이브", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.auth = Auth(config)
    app.state.version = __version__
    app.state.notice = NOTICE
    app.state.auth_warning = AUTH_WARNING

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # 템플릿이 모르는 변수를 참조해도 500 이 나지 않게(빈 값으로 렌더)
    templates.env.undefined = jinja2.ChainableUndefined
    templates.env.globals.update(version=__version__, notice=NOTICE)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from . import routes_api, routes_pages, routes_stream, routes_recording, routes_question_audio, routes_stages, routes_cloud_read, routes_offline

    app.include_router(routes_api.router, prefix="/api")
    app.include_router(routes_recording.router, prefix="/api")
    app.include_router(routes_question_audio.router, prefix="/api")
    app.include_router(routes_stages.router, prefix="/api")
    app.include_router(routes_cloud_read.router, prefix="/api")
    app.include_router(routes_offline.router, prefix="/api")
    app.include_router(routes_stream.router)
    app.include_router(routes_pages.router)

    from ..repo_display import DisplayPendingError

    @app.exception_handler(DisplayPendingError)
    async def _display_pending(request: Request, exc: DisplayPendingError):
        return JSONResponse({"detail": "기록 변경을 반영하고 있습니다. 잠시 후 다시 확인합니다.",
                             "display_state": "updating", "display_generation": exc.generation},
                            status_code=503, headers={"Retry-After": "1"})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        detail = _ko_detail(exc.status_code, exc.detail)
        headers = getattr(exc, "headers", None)
        if not _wants_json(request):
            return _html_error(exc.status_code, str(detail))
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        errors = jsonable_encoder(exc.errors())
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", ()) if x not in ("body", "query", "path"))
        reason = "필수 항목입니다" if first.get("type") == "missing" else first.get("msg", "")
        msg = "요청 값이 올바르지 않습니다" + (f"({loc}: {reason})" if loc else "")
        return JSONResponse({"detail": msg, "errors": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("처리되지 않은 오류: %s %s", request.method, request.url.path)
        if not _wants_json(request):
            return _html_error(500, _STATUS_KO[500])
        return JSONResponse({"detail": _STATUS_KO[500]}, status_code=500)

    app.add_middleware(SecurityHeaders)
    return app
