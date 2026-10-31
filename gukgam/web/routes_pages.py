"""HTML 페이지 라우트. 템플릿에는 최소 컨텍스트만 넘기고 화면 데이터는 JS 가 /api 로 가져온다."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import jinja2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .auth import (User, attempt_login, clear_login_cookie, current_user, get_auth, same_origin,
                   set_login_cookie)

router = APIRouter()

SID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
LOGIN_ERRORS = {
    "1": "패스코드가 올바르지 않습니다.",
    "rate": "로그인 시도가 너무 많습니다. 1분 뒤 다시 시도하세요.",
    "operator": "운영자 권한이 필요한 화면입니다. 운영자 패스코드로 로그인하세요.",
    "origin": "잘못된 경로의 로그인 요청입니다. 로그인 화면에서 다시 시도하세요.",
}


def safe_next(path: str | None) -> str:
    """로그인 후 이동 경로: 이 사이트 내부 경로만 허용(오픈 리다이렉트 방지)."""
    p = (path or "").strip()
    if not p.startswith("/") or p.startswith("//") or p.startswith("/\\") or any(c in p for c in "\r\n"):
        return "/"
    return p


def _login_redirect(request: Request, reason: str = "") -> RedirectResponse:
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    url = f"/login?next={quote(nxt, safe='/')}"
    if reason:
        url += f"&error={reason}"
    return RedirectResponse(url, status_code=303)


def _guard(request: Request, need: str = "viewer") -> User | RedirectResponse:
    user = current_user(request)
    if user is None:
        return _login_redirect(request)
    if need == "operator" and not user.is_operator:
        return _login_redirect(request, "operator")
    return user


def render(request: Request, template: str, user: User | None, status_code: int = 200, **ctx: Any) -> Response:
    """계약된 템플릿 렌더. 공통 컨텍스트: request, me, auth_enabled, version, notice."""
    state = request.app.state
    auth_enabled = get_auth(request).enabled
    context: dict[str, Any] = {
        "request": request,
        "me": user.public() if user else {"role": "", "name": "", "role_label": ""},
        "auth_enabled": auth_enabled,
        "desktop_local": (state.config.desktop_bridge is not None
                          and state.config.host in ("127.0.0.1", "localhost", "::1")),
        "auth_warning": "" if auth_enabled else state.auth_warning,
        "version": state.version,
        "notice": state.notice,
        "page": template.rsplit(".", 1)[0],
        "sid": None, "uid": None, "bid": None,
    }
    context.update(ctx)
    try:
        return state.templates.TemplateResponse(request, template, context, status_code=status_code)
    except jinja2.TemplateNotFound:
        raise HTTPException(503, f"화면 템플릿({template})이 아직 준비되지 않았습니다")


def _rt(request: Request):
    return request.app.state.rt


def _check_sid(request: Request, sid: str) -> dict:
    if not SID_RE.match(sid):
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    s = _rt(request).repo.get_session(sid)
    if s is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    return s


def _int_or_none(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


# ── 페이지 ───────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def dashboard(request: Request):
    user = _guard(request)
    if isinstance(user, Response):
        return user
    return render(request, "dashboard.html", user)


@router.get("/sessions/new", include_in_schema=False)
async def session_new(request: Request):
    user = _guard(request, "operator")
    if isinstance(user, Response):
        return user
    return render(request, "session_new.html", user, xcode=request.query_params.get("xcode", "")[:40],
                  xcgcd=request.query_params.get("xcgcd", "")[:40])


@router.get("/s/{sid}", include_in_schema=False)
async def session_page(request: Request, sid: str):
    user = _guard(request)
    if isinstance(user, Response):
        return user
    s = _check_sid(request, sid)
    bid = _int_or_none(request.query_params.get("b") or request.query_params.get("bid"))
    return render(request, "session.html", user, sid=sid, bid=bid, session_title=s["title"])


@router.get("/s/{sid}/report", include_in_schema=False)
async def report_page(request: Request, sid: str):
    user = _guard(request)
    if isinstance(user, Response):
        return user
    s = _check_sid(request, sid)
    return render(request, "report.html", user, sid=sid, session_title=s["title"])


@router.get("/u/{uid}", include_in_schema=False)
async def utterance_page(request: Request, uid: int):
    user = _guard(request)
    if isinstance(user, Response):
        return user
    u = _rt(request).repo.get_utterance(uid)
    if u is None:
        raise HTTPException(404, "발언을 찾을 수 없습니다")
    return render(request, "utterance.html", user, uid=uid, sid=u["session_id"], bid=u.get("block_id"))


@router.get("/items", include_in_schema=False)
async def items_page(request: Request):
    user = _guard(request)
    if isinstance(user, Response):
        return user
    sid = request.query_params.get("session_id") or None
    return render(request, "items.html", user, sid=sid if sid and SID_RE.match(sid) else None)


@router.get("/search", include_in_schema=False)
async def search_page(request: Request):
    user = _guard(request)
    if isinstance(user, Response):
        return user
    return render(request, "search.html", user, q=request.query_params.get("q", "")[:100])


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    user = _guard(request, "operator")
    if isinstance(user, Response):
        return user
    return render(request, "admin.html", user)


# ── 로그인 ───────────────────────────────────────────────────

@router.get("/login", include_in_schema=False)
async def login_page(request: Request):
    user = current_user(request)
    err = request.query_params.get("error", "")
    return render(request, "login.html", user, next=safe_next(request.query_params.get("next")),
                  error=LOGIN_ERRORS.get(err, "패스코드가 올바르지 않습니다." if err else ""))


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    """폼 로그인(passcode, name, next) — 성공 시 next 로 303. JSON 본문도 받는다."""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
    else:
        form = await request.form(max_files=0, max_fields=10)
        data = {k: str(v) for k, v in form.items()}
    nxt = safe_next(data.get("next"))
    if not same_origin(request):
        return RedirectResponse(f"/login?error=origin&next={quote(nxt, safe='/')}", status_code=303)
    try:
        user = attempt_login(request, str(data.get("passcode") or ""), str(data.get("name") or ""))
    except HTTPException as e:
        code = "rate" if e.status_code == 429 else "1"
        return RedirectResponse(f"/login?error={code}&next={quote(nxt, safe='/')}", status_code=303)
    resp = RedirectResponse(nxt, status_code=303)
    set_login_cookie(resp, request, user)
    return resp


@router.api_route("/logout", methods=["GET", "POST"], include_in_schema=False)
async def logout(request: Request):
    user = current_user(request)
    if user and get_auth(request).enabled:
        _rt(request).repo.audit(user.actor, "auth.logout", user.role)
    resp = RedirectResponse("/login", status_code=303)
    clear_login_cookie(resp)
    return resp


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)
