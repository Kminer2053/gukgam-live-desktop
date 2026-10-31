"""인증: 패스코드 로그인 + 서명 쿠키(HMAC-SHA256, 표준 라이브러리만 사용).

역할은 operator(운영자, 쓰기 가능)와 viewer(열람자) 두 가지다.
config.auth_enabled=False(패스코드 미설정)이면 모두 operator 로 보고 화면에 경고 배너를 띄운다.
쓰기 요청(POST/PUT/PATCH/DELETE)은 CSRF 대책으로 'X-Requested-With: gukgam' 헤더를 요구한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from fastapi import HTTPException, Request, WebSocket
from starlette.responses import Response

from ..config import Config

log = logging.getLogger(__name__)

COOKIE_NAME = "gukgam_auth"
SESSION_TTL = 48 * 3600            # 쿠키 유효 시간(초) — 자정을 넘기는 국감도 한 번 로그인으로
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "gukgam"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ROLES = ("operator", "viewer")
ROLE_LABELS = {"operator": "운영자", "viewer": "열람자"}
LOGIN_LIMIT = 10                    # IP별 분당 로그인 시도 허용 횟수
LOGIN_WINDOW = 60.0


@dataclass(frozen=True)
class User:
    role: str                      # operator|viewer
    name: str = ""

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"

    @property
    def actor(self) -> str:
        """감사 기록·북마크 소유자 표기."""
        return self.name or self.role

    def public(self) -> dict[str, Any]:
        return {"role": self.role, "name": self.name, "role_label": ROLE_LABELS.get(self.role, "")}


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_secret(config: Config) -> bytes:
    """서명 키: config.secret_key, 없으면 data/secret.key(권한 600)를 만들거나 읽는다."""
    if config.secret_key:
        return config.secret_key.encode("utf-8")
    path: Path = config.data_dir / "secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        data = path.read_text(encoding="utf-8").strip()
        if len(data) >= 32:
            try:
                if path.stat().st_mode & 0o077:
                    os.chmod(path, 0o600)  # 권한이 넓으면 조인다
            except OSError:
                pass
            return data.encode("utf-8")
        # 비었거나 손상 → 새로 쓴다
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
        os.chmod(path, 0o600)
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    log.info("서명 키 생성: %s", path)
    return key.encode("utf-8")


class RateLimiter:
    """키(IP)별 슬라이딩 윈도 횟수 제한."""

    def __init__(self, limit: int = LOGIN_LIMIT, window: float = LOGIN_WINDOW, max_keys: int = 10_000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> bool:
        """시도를 기록하고 허용 여부를 돌려준다."""
        now = time.monotonic()
        if len(self._hits) > self.max_keys:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.window}
        dq = self._hits.setdefault(key, deque())
        while dq and now - dq[0] >= self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


def clean_name(name: str | None) -> str:
    """표시 이름: 제어문자 제거, 20자 제한."""
    name = re.sub(r"[\x00-\x1f\x7f<>\"']", "", (name or "")).strip()
    return name[:20]


class Auth:
    def __init__(self, config: Config):
        self.config = config
        self._secret = load_secret(config)
        # 용도별 키 분리(쿠키 서명 전용)
        self._key = hmac.new(self._secret, b"gukgam-cookie-v1", hashlib.sha256).digest()
        self.limiter = RateLimiter()

    @property
    def enabled(self) -> bool:
        return self.config.auth_enabled

    # ── 토큰 ─────────────────────────────────────────────────
    def sign(self, payload: dict[str, Any]) -> str:
        body = _b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        sig = _b64e(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def unsign(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token or len(token) > 4096:
            return None
        body, sig = token.rsplit(".", 1)
        expected = _b64e(hmac.new(self._key, body.encode("ascii", "ignore"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            payload = json.loads(_b64d(body))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or int(payload.get("exp") or 0) < time.time():
            return None
        return payload

    def _passver(self, role: str) -> str:
        """패스코드가 바뀌면 기존 쿠키가 무효가 되도록 패스코드 지문을 넣는다."""
        if not self.enabled:
            return ""
        code = self.config.operator_passcode if role == "operator" else self.config.viewer_passcode
        return hmac.new(self._key, f"{role}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()[:12]

    def issue(self, user: User) -> str:
        now = int(time.time())
        return self.sign({"r": user.role, "n": user.name, "iat": now, "exp": now + SESSION_TTL,
                          "pv": self._passver(user.role)})

    def user_from_token(self, token: str | None) -> User | None:
        payload = self.unsign(token)
        if not self.enabled:
            # 인증 꺼짐: 모두 운영자. 이름만 쿠키에서 가져온다.
            return User("operator", clean_name(payload.get("n")) if payload else "")
        if not payload:
            return None
        role = payload.get("r")
        if role not in ROLES or payload.get("pv") != self._passver(role):
            return None
        return User(role, clean_name(payload.get("n")))

    def user_from_cookies(self, cookies: Mapping[str, str]) -> User | None:
        return self.user_from_token(cookies.get(COOKIE_NAME))

    # ── 패스코드 ─────────────────────────────────────────────
    def check_passcode(self, passcode: str) -> str | None:
        """맞는 역할을 돌려준다(운영자 우선). 틀리면 None."""
        given = (passcode or "").encode("utf-8")
        for role, code in (("operator", self.config.operator_passcode), ("viewer", self.config.viewer_passcode)):
            if code and hmac.compare_digest(given, code.encode("utf-8")):
                return role
        return None


# ── 요청 헬퍼 ─────────────────────────────────────────────────

def get_auth(request: Request | WebSocket) -> Auth:
    return request.app.state.auth


def client_ip(request: Request | WebSocket) -> str:
    return request.client.host if request.client else "unknown"


def current_user(request: Request) -> User | None:
    """로그인 사용자(없으면 None). 인증이 꺼져 있으면 항상 운영자."""
    return get_auth(request).user_from_cookies(request.cookies)


def require_viewer(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "로그인이 필요합니다")
    return user


def require_operator(request: Request) -> User:
    user = require_viewer(request)
    if not user.is_operator:
        raise HTTPException(403, "운영자 권한이 필요합니다")
    return user


def csrf_protect(request: Request) -> None:
    """쓰기 요청은 'X-Requested-With: gukgam' 헤더가 있어야 한다(교차 사이트 요청 차단)."""
    if request.method in SAFE_METHODS:
        return
    if request.headers.get(CSRF_HEADER, "").strip().lower() != CSRF_VALUE:
        raise HTTPException(403, "잘못된 요청입니다(X-Requested-With 헤더 없음). 화면을 새로고침하세요.")


def same_origin(request: Request | WebSocket) -> bool:
    """Origin(없으면 Referer)이 이 서버와 같은 호스트인지. 둘 다 없으면 허용(비브라우저 클라이언트).

    역방향 프록시 뒤에서는 Host 를 보존하거나 X-Forwarded-Host 를 넘겨야 한다.
    """
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return True
    m = re.match(r"^(?:https?|wss?)://([^/?#]+)", origin)
    if not m:
        return False
    hosts = {request.headers.get("host", "").lower()}
    hosts |= {h.strip().lower() for h in request.headers.get("x-forwarded-host", "").split(",") if h.strip()}
    return m.group(1).lower() in hosts


def ws_user(ws: WebSocket) -> User | None:
    return get_auth(ws).user_from_cookies(ws.cookies)


def attempt_login(request: Request, passcode: str, name: str = "") -> User:
    """패스코드 확인. 실패 시 감사 기록 후 HTTPException(401/429)."""
    auth = get_auth(request)
    rt = request.app.state.rt
    ip = client_ip(request)
    name = clean_name(name)
    if not auth.limiter.hit(ip):
        rt.repo.audit(f"ip:{ip}", "auth.login_blocked", "", {"ip": ip})
        raise HTTPException(429, "로그인 시도가 너무 많습니다. 1분 뒤 다시 시도하세요.")
    if not auth.enabled:
        # 인증 꺼짐: 이름만 기록(감사·검수자 표기용)
        return User("operator", name)
    role = auth.check_passcode(passcode)
    if role is None:
        rt.repo.audit(f"ip:{ip}", "auth.login_failed", "", {
            "ip": ip, "name": name, "ua": (request.headers.get("user-agent") or "")[:120]})
        raise HTTPException(401, "패스코드가 올바르지 않습니다")
    user = User(role, name)
    rt.repo.audit(user.actor, "auth.login", role, {"ip": ip})
    return user


def set_login_cookie(response: Response, request: Request, user: User) -> None:
    response.set_cookie(
        COOKIE_NAME, get_auth(request).issue(user), max_age=SESSION_TTL, path="/", httponly=True,
        samesite="lax", secure=request.url.scheme == "https",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")
