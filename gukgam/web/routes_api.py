"""JSON API(/api 접두). 계약: docs/CONTRACTS.md 3.2 + 5장 추가분.

모든 쓰기 요청은 'X-Requested-With: gukgam' 헤더가 필요하다(auth.csrf_protect).
편집은 SessionPipeline 의 메서드를 거쳐 이벤트(SSE)가 일관되게 나가도록 한다.
"""
from __future__ import annotations

import asyncio
import csv
import importlib
import importlib.util
import inspect
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import UploadFile

from .. import maintenance, serialize, settings
from ..config import KST, ROOT
from ..db import now_ms
from ..log_privacy import exception_label
from ..ingest.assembly_http import AssemblyTLSConfigurationError, create_http_client as assembly_http_client
from ..models import SOURCE_LABELS
from ..nlp import rules
from ..nlp.speaker import Roster
from ..reports.export import csv_cell
from ..review_display import get_block_display
from ..nlp.text import NAME_STOPWORDS, PARTIES, compact
from .auth import (User, attempt_login, clear_login_cookie, csrf_protect, current_user, get_auth, require_operator,
                   require_viewer, set_login_cookie)

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(csrf_protect)])

SID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
SOURCE_TYPES = ("assembly_caption", "assembly_hls", "browser", "file", "replay", "manual")
ITEM_KINDS = ("data_request", "commitment", "issue", "witness_request", "written_question")
ITEM_STATUSES = ("open", "in_progress", "done", "dismissed")
SPEAKER_ROLES = ("chair", "member", "answer", "other", "unknown", "system")
ROSTER_ROLES = ("chair", "member", "witness", "agency", "other")
EXPORT_FORMATS = ("md", "txt", "docx", "json", "csv")

UPLOAD_EXTS = frozenset({"wav", "mp3", "m4a", "mp4", "webm", "ogg", "aac", "flac"})
MAX_UPLOAD_BYTES = 2 * 1024 ** 3          # 2GB
ASSEMBLY_TTL = 60.0                        # 국회 조회 캐시(초)
ASSEMBLY_ERROR_TTL = 15.0
ASSEMBLY_MODULE_MISSING = "국회 연동 모듈 준비 중"
ASSEMBLY_OFFLINE = "국회 의사중계 연동이 꺼져 있습니다(GUKGAM_ALLOW_ASSEMBLY_NETWORK=0)"
CLOUD_RESYNC_WINDOW = 5 * 60.0
AI_SUMMARY_FAILURE = "AI 요약을 완료하지 못했습니다. 앱에서 연결 상태와 이용 한도를 확인한 뒤 다시 시도하세요."


# ══ 공통 헬퍼 ════════════════════════════════════════════════════

def _rt(request: Request):
    return request.app.state.rt


def _pub(rt, sid: str, type_: str, data: dict[str, Any]) -> None:
    rt.bus.publish(sid, type_, data)


def _mutation_display_generation(rt, sid: str, data: dict) -> dict:
    """수정 응답도 같은 입력 세대를 알려 지연된 조회의 역행을 막는다."""
    from ..display_reads import enabled
    if getattr(rt, "display", None) is not None or enabled(rt.repo, sid):
        state = rt.repo.display_state(sid)
        if state:
            data = {**data, "display_input_generation": state["input_generation"]}
    return data


def _session_or_404(rt, sid: str) -> dict:
    s = rt.repo.get_session(sid) if SID_RE.match(sid or "") else None
    if s is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    return s


def _session_out(rt, s: dict) -> dict:
    return serialize.session(s, rt.repo.session_stats(s["id"]))


def _publish_session(rt, sid: str) -> dict:
    s = rt.repo.get_session(sid)
    out = _session_out(rt, s)
    _pub(rt, sid, "session.status", out)
    return out


def _utt_or_404(rt, uid: int) -> dict:
    u = rt.repo.get_utterance(uid)
    if u is None:
        raise HTTPException(404, "발언을 찾을 수 없습니다")
    return u


def _block_or_404(rt, bid: int) -> dict:
    b = rt.repo.get_block(bid)
    if b is None:
        raise HTTPException(404, "질의 블록을 찾을 수 없습니다")
    return b


def _item_full(rt, iid: int) -> dict | None:
    """세션 제목·발언 시각이 붙은 항목 행."""
    row = rt.repo.get_item(iid)
    if row is None:
        return None
    rows = rt.repo.list_items(row["session_id"], block_id=row.get("block_id"), include_dismissed=True)
    return next((x for x in rows if x["id"] == iid), row)


def _is_running(rt, s: dict) -> bool:
    try:
        return bool(rt.ingest.is_running(s["id"])) or s["status"] == "live"
    except Exception:
        return s["status"] == "live"


def _lazy_module(name: str, label: str):
    """동시 작성 중인 모듈을 지연 import. 없으면 503."""
    try:
        return importlib.import_module(name)
    except ImportError as e:
        log.warning("모듈 import 실패(%s)", exception_label(e))
        raise HTTPException(503, f"{label} 모듈 준비 중입니다")


def _call_optional_kwargs(fn, *args, **optional: Any):
    """함수가 받는 경우에만 선택 인자(base_url 등)를 넘긴다."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_var = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs = {k: v for k, v in optional.items() if accepts_var or k in params}
    return fn(*args, **kwargs)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _content_disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _today() -> str:
    return datetime.now(KST).date().isoformat()


# ══ 명단 파싱 ════════════════════════════════════════════════════

ROLE_WORDS = {
    "위원장": "chair", "위원장대리": "chair", "위원장직무대리": "chair", "위원": "member", "의원": "member",
    "간사": "member", "위원님": "member", "증인": "witness", "참고인": "witness", "기관": "agency",
    "피감기관": "agency", "기관장": "agency", "기타": "other",
}
PARTY_ALIASES = {"민주당": "더불어민주당", "더민주": "더불어민주당", "국힘": "국민의힘", "혁신당": "조국혁신당"}
_TITLE_AGENCY = re.compile(r"(?:장관|차관|사장|원장|청장|이사장|처장|총장|본부장|회장|실장|국장|대표|부장)$")


def _norm_role(v: Any) -> str:
    v = str(v or "").strip()
    if v in ROSTER_ROLES:
        return v
    return ROLE_WORDS.get(v.replace(" ", ""), "member")


def _norm_party(v: Any) -> str:
    v = str(v or "").strip()
    return PARTY_ALIASES.get(v, v)


def _parse_roster_piece(piece: str) -> dict | None:
    name: str | None = None
    role: str | None = None
    party = ""
    rest: list[str] = []
    for tok in piece.split():
        glued = re.fullmatch(r"([가-힣]{2,4})(위원장|위원|의원)(?:님)?", tok)
        if name is None and glued and glued.group(1) not in NAME_STOPWORDS:
            name, role = glued.group(1), role or ROLE_WORDS[glued.group(2)]
            continue
        if tok in ROLE_WORDS:
            role = role or ROLE_WORDS[tok]
            continue
        p = PARTY_ALIASES.get(tok, tok)
        if p in PARTIES:
            party = p
            continue
        if name is None and re.fullmatch(r"[가-힣A-Za-z]{2,10}", tok) and tok not in NAME_STOPWORDS:
            name = tok
            continue
        rest.append(tok)
    title = " ".join(rest)[:60]
    if name is None:
        return {"name": None, "role": role, "party": party, "title": title} if (role or party) else None
    if role is None:
        role = "agency" if title and _TITLE_AGENCY.search(title) else "member"
    return {"name": name, "role": role, "party": party, "title": title}


def parse_roster_text(text: str) -> list[dict]:
    """한 줄에 '이름 [역할] [정당] [직책]'. 쉼표로 여러 명을 적어도 된다.

    예) '유의동 위원장 국민의힘', '김남근 더불어민주당', '김남근(더불어민주당)', '홍지선 국토교통부장관'
    """
    members: list[dict] = []
    seen: set[str] = set()
    for raw in re.split(r"[\r\n;]+", text or ""):
        line = re.sub(r"^\s*(?:\d+\s*[.)]|[-*○●□■▶•·])\s*", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        prev: dict | None = None
        for piece in line.split(","):
            piece = re.sub(r"[()\[\]/|\t·•]", " ", piece).strip()
            if not piece:
                continue
            m = _parse_roster_piece(piece)
            if m is None:
                continue
            if m["name"] is None:
                # '김남근 위원, 더불어민주당' 처럼 이름 없는 조각은 앞 사람에 붙인다
                if prev is not None:
                    if m["role"] and prev["role"] == "member":
                        prev["role"] = m["role"]
                    prev["party"] = prev["party"] or m["party"]
                    prev["title"] = prev["title"] or m["title"]
                continue
            if m["name"] in seen:
                continue
            seen.add(m["name"])
            members.append(m)
            prev = m
    return members


def _roster_out(rows: list[dict]) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "role": r.get("role") or "member", "party": r.get("party") or "",
             "title": r.get("title") or "", "aliases": r.get("aliases") or [],
             "scope": "global" if r.get("session_id") is None else "session"} for r in rows]


# ══ 요청 모델 ════════════════════════════════════════════════════

class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class RosterMember(_Model):
    name: str = Field(min_length=1, max_length=30)
    role: str = "member"
    party: str = Field("", max_length=30)
    title: str = Field("", max_length=60)
    aliases: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, v: Any) -> str:
        return _norm_role(v)

    @field_validator("party", mode="before")
    @classmethod
    def _party(cls, v: Any) -> str:
        return _norm_party(v)


def _valid_date(v: str) -> str:
    if not v:
        return ""
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")


class SessionCreate(_Model):
    title: str = Field(min_length=1, max_length=200)
    committee: str = Field("", max_length=100)
    agencies: str = Field("", max_length=300)
    meeting_date: str = Field("", max_length=10)
    source_type: Literal["assembly_caption", "assembly_hls", "browser", "file", "replay", "manual"]
    source_config: dict[str, Any] = Field(default_factory=dict)
    save_audio: bool = False
    roster_text: str = Field("", max_length=20000)
    roster: list[RosterMember] | None = Field(None, max_length=500)
    notes: str = Field("", max_length=5000)
    start: bool = False

    @field_validator("meeting_date")
    @classmethod
    def _date(cls, v: str) -> str:
        return _valid_date(v)


class SessionPatch(_Model):
    title: str | None = Field(None, min_length=1, max_length=200)
    committee: str | None = Field(None, max_length=100)
    agencies: str | None = Field(None, max_length=300)
    meeting_date: str | None = Field(None, max_length=10)
    source_type: Literal["assembly_caption", "assembly_hls", "browser", "file", "replay", "manual"] | None = None
    source_config: dict[str, Any] | None = None
    save_audio: bool | None = None
    notes: str | None = Field(None, max_length=5000)

    @field_validator("meeting_date")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        return v if v is None else _valid_date(v)


class RosterPut(_Model):
    members: list[RosterMember] | None = Field(None, max_length=500)
    text: str | None = Field(None, max_length=20000)


class ManualIn(_Model):
    text: str = Field(min_length=1, max_length=5000)
    speaker: str = Field("", max_length=40)
    role: Literal["", "chair", "member", "answer", "other"] = ""
    marker: bool = False            # True 면 정회·속개 같은 기록 표지(speaker_role='system')


class UtterancePatch(_Model):
    text: str | None = Field(None, min_length=1, max_length=10000)
    speaker: str | None = Field(None, max_length=40)
    speaker_role: Literal["chair", "member", "answer", "other", "unknown", "system"] | None = None


class BlockPatch(_Model):
    questioner: str = Field(min_length=1, max_length=30)
    questioner_party: str = Field("", max_length=30)


class BlockReview(_Model):
    status: Literal["reviewed", "draft"] = "reviewed"


class ItemCreate(_Model):
    session_id: str = Field(min_length=1, max_length=64)
    kind: Literal["data_request", "commitment", "issue", "witness_request", "written_question"]
    text: str = Field(min_length=1, max_length=2000)
    utterance_id: int | None = None
    block_id: int | None = None
    requester: str = Field("", max_length=30)
    assignee_dept: str = Field("", max_length=60)
    due_date: str = Field("", max_length=30)
    strength: str = Field("", max_length=20)
    note: str = Field("", max_length=2000)


class ItemPatch(_Model):
    status: Literal["open", "in_progress", "done", "dismissed"] | None = None
    assignee_dept: str | None = Field(None, max_length=60)
    due_date: str | None = Field(None, max_length=30)
    note: str | None = Field(None, max_length=2000)
    text: str | None = Field(None, min_length=1, max_length=2000)
    kind: Literal["data_request", "commitment", "issue", "witness_request", "written_question"] | None = None
    strength: str | None = Field(None, max_length=20)
    requester: str | None = Field(None, max_length=30)


class AlertAck(_Model):
    alert_id: int | None = None


class BookmarkIn(_Model):
    note: str = Field("", max_length=500)


class LoginIn(_Model):
    passcode: str = Field("", max_length=200)
    name: str = Field("", max_length=40)


class SummarizeIn(_Model):
    scope: Literal["session", "rolling"] = "session"


# ══ 인증 ═════════════════════════════════════════════════════════

@router.post("/auth/login")
async def api_login(request: Request, body: LoginIn):
    user = attempt_login(request, body.passcode, body.name)
    resp = JSONResponse({"ok": True, "auth_enabled": get_auth(request).enabled, **user.public()})
    set_login_cookie(resp, request, user)
    return resp


@router.post("/auth/logout")
async def api_logout(request: Request):
    user = current_user(request)
    if user and get_auth(request).enabled:
        _rt(request).repo.audit(user.actor, "auth.logout", user.role)
    resp = JSONResponse({"ok": True})
    clear_login_cookie(resp)
    return resp


@router.get("/auth/me")
async def api_me(request: Request):
    user = current_user(request)
    me = user.public() if user else {"role": "", "name": "", "role_label": ""}
    return {"auth_enabled": get_auth(request).enabled, **me}


# ══ 상태 ═════════════════════════════════════════════════════════

@router.get("/health")
async def health(request: Request):
    rt = _rt(request)
    try:
        llm = rt.analysis.status()
    except Exception as e:  # 분석 서비스 오류가 헬스체크를 막지 않게
        llm = {"error": "AI 처리 상태를 확인하세요."}
    try:
        stt = await asyncio.wait_for(rt.stt.health(), 3)
    except Exception as e:
        stt = {"ok": False, "error": "음성인식 상태를 확인하세요."}
    try:
        disk = maintenance.disk_status(rt.config.data_dir)
    except OSError:
        disk = None
    desktop = rt.config.desktop_bridge is not None
    return {"ok": True, "version": request.app.state.version, "auth_enabled": get_auth(request).enabled,
            "desktop_local": desktop and rt.config.host in _LOOPBACK,
            "stt_optional": desktop and (settings.get(rt.repo, "stt") or {}).get("provider") == "none",
            "llm": llm, "stt": stt, "assembly_network": rt.config.allow_assembly_network,
            "ffmpeg": shutil.which("ffmpeg") is not None, "disk": disk, "time": now_ms(),
            "cloud": rt.cloud.status() if getattr(rt, "cloud", None) is not None else {"enabled": False}}


@router.post("/cloud/resync")
async def cloud_resync(request: Request, user: User = Depends(require_operator)):
    """운영자가 요청한 전체 백필을 런타임마다 5분에 한 번 시작한다."""
    rt = _rt(request)
    cloud = getattr(rt, "cloud", None)
    if cloud is None or not cloud.enabled:
        raise HTTPException(503, "클라우드 동기화가 설정되어 있지 않습니다")
    started = time.monotonic()
    previous = getattr(rt, "_cloud_resync_at", None)
    if previous is not None and started - previous < CLOUD_RESYNC_WINDOW:
        raise HTTPException(429, "클라우드 다시 맞추기는 5분에 한 번만 할 수 있습니다")
    # await 전에 기록하여 이미 진행 중인 백필도 연속 요청에서 보호한다.
    rt._cloud_resync_at = started
    try:
        ok = await cloud.backfill()
    except Exception as exc:
        log.warning("클라우드 다시 맞추기 실패(%s)", exception_label(exc))
        ok = False
    if not ok:
        raise HTTPException(503, "클라우드 기록을 다시 맞추지 못했습니다. 연결 상태를 확인하세요.")
    return {"ok": True, "detail": "클라우드 기록을 다시 맞췄습니다."}


# ── 사전 점검(국감 전날·당일 리허설) ─────────────────────────────
GB = 1024 ** 3
DISK_WARN = 10 * GB
DISK_ERROR = 3 * GB
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _check(key: str, label: str, level: str, detail: str) -> dict:
    return {"key": key, "label": label, "ok": level == "ok", "level": level, "detail": detail}


def _hf_cached(repo_id: str) -> bool:
    """HF 허브 캐시에 모델이 있는지(망분리 환경에서는 내려받을 수 없다)."""
    home = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    hub = Path(os.environ.get("HF_HUB_CACHE") or (home / "hub"))
    snaps = hub / ("models--" + repo_id.replace("/", "--")) / "snapshots"
    return snaps.is_dir() and any(snaps.glob("*/*"))


def _model_files_ok(path: Path) -> bool:
    names = ("config.json", "weights.npz", "weights.safetensors", "model.safetensors")
    return any((path / n).exists() for n in names) or any(path.glob("*.safetensors"))


async def _preflight_stt(rt) -> dict:
    from ..stt.service import STTError, resolve_model

    label = "음성인식(STT) 모델"
    cfg = settings.get(rt.repo, "stt") or {}
    provider = cfg.get("provider") or "none"
    if provider == "none":
        if rt.config.desktop_bridge is not None:
            return _check("stt", "음성인식(선택 기능)", "ok",
                          "기본 국회 자막 수집과 ChatGPT 보정에는 음성인식 모델이 필요하지 않습니다")
        return _check("stt", label, "warn", "음성인식이 꺼져 있습니다 — 국회 AI자막·수기 입력만 쓸 수 있습니다")
    if rt.config.disable_heavy_engines:
        return _check("stt", label, "warn", "음성인식 엔진이 비활성화되어 있습니다(테스트 모드)")
    if provider == "nemotron_cpu":
        health = await rt.stt.health()
        return _check("stt", label, "ok" if health.get("ok") else "warn",
                      "Nemotron 한국어 · CPU — " + str(health.get("detail") or "실행 전입니다"))
    try:
        model, local = resolve_model(cfg.get("model") or "")
    except STTError as e:
        return _check("stt", label, "error", str(e))
    if importlib.util.find_spec("mlx_whisper") is None:
        return _check("stt", label, "error", "mlx-whisper 가 설치되어 있지 않습니다(requirements-ai.txt)")
    if local and not _model_files_ok(Path(model)):
        return _check("stt", label, "error", f"모델 폴더에 가중치 파일이 없습니다: {model}")
    if not local and not _hf_cached(model):
        return _check("stt", label, "error", f"모델이 로컬 캐시에 없습니다(망분리 환경에서는 내려받을 수 없음): {model}")
    try:
        h = await asyncio.wait_for(rt.stt.health(), 3)
    except Exception as e:
        h = {"ok": False, "detail": str(e) or type(e).__name__}
    name = Path(model).name if local else model
    if not h.get("ok") and h.get("detail"):
        return _check("stt", label, "warn", f"{name} — 최근 오류: {h['detail']}")
    return _check("stt", label, "ok", f"{name} · {'메모리에 올라와 있음' if h.get('loaded') else '첫 사용 때 불러옴'}")


async def _preflight_llm(rt) -> dict:
    label = "AI 요약 엔진"
    fn = getattr(rt.analysis, "health", None) or getattr(rt.analysis, "status", None)
    try:
        h = fn() if fn is not None else {}
        if inspect.isawaitable(h):
            h = await asyncio.wait_for(h, 5)
        h = h if isinstance(h, dict) else {}
    except Exception as e:
        return _check("llm", label, "error", f"AI 엔진 상태를 확인하지 못했습니다: {e}")
    provider = str(h.get("provider") or (settings.get(rt.repo, "llm") or {}).get("provider") or "")
    model = str(h.get("model") or "")
    desc = provider + (f" · {model}" if model else "")
    err = h.get("detail") or h.get("error") or h.get("last_error")
    if h.get("ok") is False:
        return _check("llm", label, "error", f"{desc} — {err or '사용할 수 없습니다'}")
    if provider == "rules":
        return _check("llm", label, "warn", "AI 엔진 없이 규칙 기반 요약만 씁니다")
    if provider == "desktop_chatgpt":
        bridge = getattr(rt, "desktop_bridge", None)
        if bridge is None or bridge.closed:
            return _check("llm", label, "error", "데스크톱 앱 연결이 준비되지 않았습니다")
        health = getattr(bridge, "connection_health", None)
        try:
            state = (await health()).get("state") if callable(health) else "unavailable"
        except Exception:
            state = "unavailable"
        messages = {
            "verified": ("ok", "앱에서 계정 권한과 AI 응답을 확인했습니다"),
            "refreshable": ("ok", "이전에 확인한 계정 연결입니다. 다음 AI 요청에서 갱신을 시도합니다. 현재 갱신·AI 응답 성공은 아직 확인하지 않았습니다"),
            "disconnected": ("warn", "앱에서 ChatGPT 계정을 연결한 뒤 연결 시험을 실행하세요"),
            "unverified": ("warn", "앱의 연결 시험에서 계정 권한과 응답을 확인하세요"),
            "expired": ("warn", "계정 연결의 갱신이 필요합니다. 앱에서 연결 시험을 실행하고 필요하면 다시 로그인하세요"),
            "checking": ("warn", "앱에서 계정 연결을 확인 중입니다. 완료한 뒤 다시 점검하세요"),
            "error": ("warn", "최근 AI 요청 또는 연결 시험을 완료하지 못했습니다. 앱에서 연결 상태와 사용 한도를 확인하세요"),
        }
        level, detail = messages.get(state, ("warn", "앱의 연결 상태를 확인하지 못했습니다. 앱에서 연결 상태를 확인한 뒤 다시 점검하세요"))
        return _check("llm", label, level, f"ChatGPT · {model or '계정 기본 모델'} — {detail}")
    if provider == "local_mlx" and not rt.config.disable_heavy_engines:
        if importlib.util.find_spec("mlx_lm") is None:
            return _check("llm", label, "error", "mlx-lm 이 설치되어 있지 않습니다(requirements-ai.txt) — 규칙 기반 요약으로 대체됩니다")
        repo_id = str((settings.get(rt.repo, "llm") or {}).get("local_model") or model)
        p = Path(repo_id).expanduser()
        local = p if p.is_absolute() else ROOT / p
        if repo_id and not local.exists() and not _hf_cached(repo_id):
            return _check("llm", label, "error",
                          f"모델이 로컬 캐시에 없습니다(망분리 환경에서는 내려받을 수 없음): {repo_id}")
    if err:
        return _check("llm", label, "warn", f"{desc} — 최근 오류: {err}")
    return _check("llm", label, "ok", desc or "설정됨")


@router.get("/preflight")
async def preflight(request: Request, user: User = Depends(require_viewer)):
    """사전 점검: 음성인식·AI 엔진·디스크·국회 연동·인증·우리 기관·ffmpeg. level=error 가 하나라도 있으면 ok=false."""
    rt = _rt(request)
    checks = [await _preflight_stt(rt), await _preflight_llm(rt)]
    try:
        d = maintenance.disk_status(rt.config.data_dir)
        free = d["free"]
        detail = f"여유 {free / GB:.1f}GB / 전체 {d['total'] / GB:.0f}GB"
        level = "error" if free < DISK_ERROR else ("warn" if free < DISK_WARN else "ok")
        if level != "ok":
            detail += " — 원음 저장·자막 원본이 쌓이면 수집이 멈출 수 있습니다(보존 기간·백업 후 정리)"
        checks.append(_check("disk", "디스크 여유", level, detail))
    except OSError as e:
        checks.append(_check("disk", "디스크 여유", "warn", f"확인 실패: {e}"))
    if not rt.config.allow_assembly_network:
        checks.append(_check("assembly", "국회 의사중계 연동", "warn",
                             "외부망이 차단되어 국회 AI자막·중계·일정을 쓸 수 없습니다(GUKGAM_ALLOW_ASSEMBLY_NETWORK=0)"))
    else:
        ent = await _assembly_fetch(request, "live", "live_list")
        n = len(ent.get("data") or []) if isinstance(ent.get("data"), list) else 0
        when = serialize.hms(ent["fetched_at"])[:5] if ent.get("fetched_at") else ""
        if ent.get("error") and not ent.get("data"):
            checks.append(_check("assembly", "국회 의사중계 연동", "error", ent["error"]))
        elif ent.get("error"):
            checks.append(_check("assembly", "국회 의사중계 연동", "warn", f"{ent['error']} — {when} 조회 결과를 표시 중"))
        else:
            checks.append(_check("assembly", "국회 의사중계 연동", "ok", f"오늘 중계 목록 {n}건 조회({when})"))
    if get_auth(request).enabled:
        checks.append(_check("auth", "접속 인증", "ok", "패스코드 인증이 켜져 있습니다"))
    elif rt.config.desktop_bridge is not None and rt.config.host in _LOOPBACK:
        checks.append(_check("auth", "이 PC 기록 화면", "ok",
                             "설치 앱의 기록 화면은 이 PC에서만 열립니다. 동료 열람은 릴레이 사이트의 별도 접속 설정을 사용합니다"))
    elif rt.config.host in _LOOPBACK:
        checks.append(_check("auth", "접속 인증", "warn",
                             "인증이 꺼져 있습니다(이 PC 에서만 접속) — 내부망에 공개하기 전에 패스코드를 설정하세요"))
    else:
        checks.append(_check("auth", "접속 인증", "error",
                             f"인증 없이 {rt.config.host} 로 공개되어 있습니다 — GUKGAM_OPERATOR_PASSCODE 를 설정하세요"))
    org = settings.get(rt.repo, "org") or {}
    if (org.get("name") or "").strip():
        n_alias = len([a for a in org.get("aliases") or [] if str(a).strip()])
        checks.append(_check("org", "우리 기관", "ok", org["name"].strip() + (f" (별칭 {n_alias}개)" if n_alias else "")))
    else:
        checks.append(_check("org", "우리 기관", "warn",
                             "우리 기관이 설정되지 않았습니다 — 언급 알림·관련 질의 표시·일정 찾기가 동작하지 않습니다"))
    ff = shutil.which("ffmpeg")
    if rt.config.desktop_bridge is not None:
        stt_enabled = (settings.get(rt.repo, "stt") or {}).get("provider") != "none"
        if not stt_enabled:
            checks.append(_check("ffmpeg", "음성·영상 파일 처리(선택 기능)", "ok",
                                 "기본 국회 자막 수집과 ChatGPT 보정에는 별도 파일 처리 도구가 필요하지 않습니다"))
        else:
            checks.append(_check("ffmpeg", "음성·영상 파일 처리", "ok", "파일 처리 도구가 준비되어 있습니다") if ff else _check(
                "ffmpeg", "음성·영상 파일 처리", "warn", "파일 처리 도구가 없어 선택한 음성·영상 파일 처리를 쓸 수 없습니다. 기본 국회 자막 수집은 사용할 수 있습니다"))
    else:
        checks.append(_check("ffmpeg", "ffmpeg", "ok", ff) if ff else _check(
            "ffmpeg", "ffmpeg", "warn", "ffmpeg 가 없습니다 — 중계영상 음성인식·녹음 파일 처리를 쓸 수 없습니다(brew install ffmpeg)"))
    return {"checks": checks, "ok": all(c["level"] != "error" for c in checks), "checked_at": now_ms(),
            "desktop_local": rt.config.desktop_bridge is not None and rt.config.host in _LOOPBACK}


@router.get("/desktop/connection-status")
async def desktop_connection_status(request: Request, user: User = Depends(require_viewer)):
    """대시보드의 계정 상태만 갱신한다. 국회 조회·OAuth 갱신·추가 AI 요청을 하지 않는다."""
    rt = _rt(request)
    if rt.config.desktop_bridge is None or rt.config.host not in _LOOPBACK:
        raise HTTPException(404, "설치 앱의 이 PC 기록 화면에서만 사용할 수 있습니다")
    return {"check": await _preflight_llm(rt)}


# ══ 국회 의사중계 연동 ═══════════════════════════════════════════

async def _assembly_fetch(request: Request, key: str, fn_name: str, *args: Any, force: bool = False) -> dict:
    """국회 의사중계 조회(60초 캐시). 반환 {"data", "fetched_at", "error"}."""
    rt = _rt(request)
    state = request.app.state
    if not rt.config.allow_assembly_network:
        return {"data": None, "fetched_at": None, "error": ASSEMBLY_OFFLINE}
    cache: dict[str, dict] = getattr(state, "assembly_cache", None) or {}
    state.assembly_cache = cache
    lock: asyncio.Lock | None = getattr(state, "assembly_lock", None)
    if lock is None:
        lock = state.assembly_lock = asyncio.Lock()

    def fresh(ent: dict | None) -> bool:
        if not ent:
            return False
        ttl = ASSEMBLY_ERROR_TTL if ent["error"] else ASSEMBLY_TTL
        return time.monotonic() - ent["t"] < (min(ttl, 10.0) if force else ttl)

    if fresh(cache.get(key)):
        return cache[key]
    async with lock:
        if fresh(cache.get(key)):
            return cache[key]
        try:
            mod = importlib.import_module("gukgam.ingest.assembly")
            fn = getattr(mod, fn_name)
        except (ImportError, AttributeError) as e:
            log.info("국회 연동 모듈 없음(%s)", exception_label(e))
            return {"data": None, "fetched_at": None, "error": ASSEMBLY_MODULE_MISSING}
        prev = cache.get(key)
        try:
            async with assembly_http_client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                data = await asyncio.wait_for(fn(client, *args), 25)
            ent = {"t": time.monotonic(), "data": data, "fetched_at": now_ms(), "error": None}
        except Exception as e:
            log.warning("국회 의사중계 조회 실패(%s)", e.code if isinstance(e, AssemblyTLSConfigurationError) else exception_label(e))
            ent = {"t": time.monotonic(), "data": prev["data"] if prev else None,
                   "fetched_at": prev["fetched_at"] if prev else None,
                   "error": str(e) if isinstance(e, AssemblyTLSConfigurationError) else f"국회 의사중계 조회에 실패했습니다({type(e).__name__})", "stale": bool(prev)}
        cache[key] = ent
        return ent


def _org_terms(rt) -> list[str]:
    return [compact(t) for t, _c in settings.watch_terms(rt.repo) if compact(t)]


@router.get("/assembly/live")
async def assembly_live(request: Request, refresh: bool = False, user: User = Depends(require_viewer)):
    rt = _rt(request)
    ent = await _assembly_fetch(request, "live", "live_list", force=refresh)
    terms = _org_terms(rt)
    # 회의명에는 대표 기관만 나오므로, 피감기관 전체 목록으로 찾은 일정(org-schedule 캐시)도 함께 본다
    sched = getattr(request.app.state, "org_schedule", None) or {}
    sched_keys = {(str(s.get("xcode")), str(s.get("xcgcd"))): s for s in sched.get("items") or []}
    items = []
    for it in ent.get("data") or []:
        if not isinstance(it, dict):
            continue
        subj = compact(str(it.get("xsubj") or ""))
        matched = [t for t in terms if t and t in subj]
        hit = sched_keys.get((str(it.get("xcode")), str(it.get("xcgcd"))))
        if hit:
            matched += [m for m in hit.get("matched") or [] if m not in matched]
        items.append({**it, "org_match": bool(matched), "matched_terms": matched,
                      "agencies": hit.get("agencies") if hit else None})
    out: dict[str, Any] = {"items": items, "fetched_at": ent.get("fetched_at")}
    if ent.get("error"):
        out["error"] = ent["error"]
    return out


ORG_SCHEDULE_TTL = 1800.0   # 우리 기관 국감 일정(달력+피감기관 목록) 캐시 30분


def _org_names(rt) -> list[str]:
    org = settings.get(rt.repo, "org")
    names = [org.get("name") or ""] + list(org.get("aliases") or [])
    return [n.strip() for n in names if n and n.strip()]


async def _refresh_org_schedule(state, key: str, names: list[str]) -> None:
    """이번 달(20일 이후면 다음 달까지) 국감 일정에서 우리 기관이 피감기관인 회의를 찾는다."""
    from datetime import datetime, timedelta

    from ..config import KST
    today = datetime.now(KST).date()
    months = [today.strftime("%Y%m")]
    if today.day >= 20:
        months.append((today.replace(day=28) + timedelta(days=7)).strftime("%Y%m"))
    items: list[dict] = []
    error = None
    try:
        mod = importlib.import_module("gukgam.ingest.assembly")
        async with assembly_http_client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            for ym in months:
                found = await asyncio.wait_for(
                    mod.find_org_meetings(client, names, ym, since=today.strftime("%Y%m%d"), max_lookups=30), 120)
                items.extend(found)
    except Exception as e:  # 실패해도 이전 결과는 유지
        log.warning("우리 기관 국감 일정 조회 실패(%s)", e.code if isinstance(e, AssemblyTLSConfigurationError) else exception_label(e))
        error = str(e) if isinstance(e, AssemblyTLSConfigurationError) else f"국회 일정 조회에 실패했습니다({type(e).__name__})"
    prev = getattr(state, "org_schedule", None)
    if error and prev and prev.get("key") == key:
        prev.update(t=time.monotonic(), error=error)
        return
    seen, uniq = set(), []
    for it in sorted(items, key=lambda r: (r.get("xdate", ""), r.get("time", ""))):
        k = (it.get("xcode"), it.get("xcgcd"), it.get("xdate"))
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    state.org_schedule = {"key": key, "t": time.monotonic(), "items": uniq, "fetched_at": now_ms(), "error": error}


@router.get("/assembly/org-schedule")
async def assembly_org_schedule(request: Request, refresh: bool = False, user: User = Depends(require_viewer)):
    """설정의 우리 기관(이름·별칭)이 피감기관인 이번 달 국감 일정. 첫 조회는 백그라운드로 돌고 pending=true.

    설정 assembly.auto_detect 가 꺼져 있으면 국회 일정을 조회하지 않고 {"items": [], "disabled": true}.
    """
    rt = _rt(request)
    state = request.app.state
    if not (settings.get(rt.repo, "assembly") or {}).get("auto_detect", True):
        return {"items": [], "disabled": True}
    names = _org_names(rt)
    if not names:
        return {"items": [], "configured": False}
    if not rt.config.allow_assembly_network:
        return {"items": [], "configured": True, "error": ASSEMBLY_OFFLINE}
    key = "|".join(names)
    ent = getattr(state, "org_schedule", None)
    if ent and ent.get("key") != key:
        ent = None
    stale = ent is None or refresh or time.monotonic() - ent["t"] > ORG_SCHEDULE_TTL
    task = getattr(state, "org_schedule_task", None)
    if stale and (task is None or task.done()):
        state.org_schedule_task = asyncio.create_task(_refresh_org_schedule(state, key, names))
    running = bool(getattr(state, "org_schedule_task", None) and not state.org_schedule_task.done())
    out: dict[str, Any] = {"items": (ent or {}).get("items") or [], "configured": True,
                           "fetched_at": (ent or {}).get("fetched_at"), "pending": running, "orgs": names}
    if ent and ent.get("error"):
        out["error"] = ent["error"]
    return out


@router.get("/assembly/agencies")
async def assembly_agencies(request: Request, xcode: str = Query(..., min_length=1, max_length=40),
                            xcgcd: str = Query(..., min_length=1, max_length=40),
                            user: User = Depends(require_viewer)):
    """회의의 전체 피감기관 목록(회의명 괄호에는 대표 기관과 '등'만 나온다)."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", xcode) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", xcgcd):
        raise HTTPException(400, "회의 코드 형식이 올바르지 않습니다")
    ent = await _assembly_fetch(request, f"angun:{xcode}:{xcgcd}", "live_angun", xcode, xcgcd)
    out: dict[str, Any] = {"agencies": [a for a in (ent.get("data") or []) if isinstance(a, str) and a.strip()],
                           "fetched_at": ent.get("fetched_at")}
    if ent.get("error"):
        out["error"] = ent["error"]
    return out


@router.get("/assembly/roster")
async def assembly_roster(request: Request, xcode: str = Query(..., min_length=1, max_length=40),
                          user: User = Depends(require_viewer)):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", xcode):
        raise HTTPException(400, "xcode 형식이 올바르지 않습니다")
    ent = await _assembly_fetch(request, f"roster:{xcode}", "committee_roster", xcode)
    members = []
    for m in ent.get("data") or []:
        if isinstance(m, dict) and (m.get("name") or "").strip():
            members.append({"name": m["name"].strip(), "role": _norm_role(m.get("role")),
                            "party": _norm_party(m.get("party")), "title": m.get("title") or ""})
    out: dict[str, Any] = {"members": members, "fetched_at": ent.get("fetched_at")}
    if ent.get("error"):
        out["error"] = ent["error"]
    return out


# ══ 세션 ═════════════════════════════════════════════════════════

def _validate_source_config(rt, source_type: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """수집원 설정 검증(경로 조작·잘못된 URL 차단). 모르는 키는 그대로 둔다(수집 모듈용)."""
    out: dict[str, Any] = {}
    for k, v in (cfg or {}).items():
        if not isinstance(k, str) or len(k) > 40:
            continue
        if isinstance(v, str) and len(v) > 2000:
            raise HTTPException(422, f"수집원 설정 값이 너무 깁니다({k})")
        out[k] = v
    for k in ("xcode", "xcgcd"):
        if out.get(k) not in (None, ""):
            out[k] = str(out[k]).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", out[k]):
                raise HTTPException(422, f"{k} 형식이 올바르지 않습니다")
    if out.get("xsami") and not re.match(r"^wss?://", str(out["xsami"])):
        raise HTTPException(422, "자막 주소(xsami)는 ws:// 또는 wss:// 로 시작해야 합니다")
    if out.get("hls_url") and not re.match(r"^https?://", str(out["hls_url"])):
        raise HTTPException(422, "영상 주소(hls_url)는 http:// 또는 https:// 로 시작해야 합니다")
    if "caption_replay" in out and type(out["caption_replay"]) is not bool:
        raise HTTPException(422, "저장 자막 재생 선택은 참/거짓이어야 합니다")
    if out.get("caption_replay") is True:
        if source_type != "assembly_caption":
            raise HTTPException(422, "저장 국회 자막 재생은 자막 수집 세션에서만 사용할 수 있습니다")
        from ..ingest.caption_replay import replay_path, replay_speed
        try:
            out["caption_replay_speed"] = replay_speed(out.get("caption_replay_speed", 1))
            if out.get("caption_replay_file"):
                out["caption_replay_file"] = str(replay_path(rt.config.data_dir, out["caption_replay_file"]))
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    elif out.get("caption_replay_file"):
        raise HTTPException(422, "저장 자막 파일을 사용하려면 저장 자료 재생을 선택하세요")
    if source_type == "assembly_caption" and not out.get("caption_replay") and not (out.get("xcode") or out.get("xsami")):
        raise HTTPException(422, "국회 회의(xcode)를 선택하세요")
    if source_type == "assembly_hls" and not (out.get("xcode") or out.get("hls_url")):
        raise HTTPException(422, "국회 회의(xcode) 또는 영상 주소(hls_url)를 지정하세요")
    if source_type == "file" and out.get("path"):
        up = rt.config.upload_dir.resolve()
        p = Path(str(out["path"])).expanduser()
        p = (p if p.is_absolute() else up / p).resolve()
        if not p.is_relative_to(up):
            raise HTTPException(400, "녹음 파일은 이 서버에 업로드한 파일만 지정할 수 있습니다")
        out["path"] = str(p)
    if source_type == "replay":
        if out.get("fixture"):
            p = Path(str(out["fixture"])).expanduser()
            p = (p if p.is_absolute() else ROOT / p).resolve()
            allowed = [(ROOT / "tests" / "fixtures").resolve(), rt.config.data_dir.resolve()]
            if not any(p.is_relative_to(a) for a in allowed) or not p.is_file():
                raise HTTPException(400, "리플레이 파일은 tests/fixtures 또는 데이터 폴더 안의 파일만 지정할 수 있습니다")
            out["fixture"] = str(p)
        if "speed" in out:
            try:
                out["speed"] = float(out["speed"])
            except (TypeError, ValueError):
                raise HTTPException(422, "재생 배속(speed)은 숫자여야 합니다")
            if not 0.1 <= out["speed"] <= 1000:
                raise HTTPException(422, "재생 배속(speed)은 0.1~1000 사이여야 합니다")
    if out.get("base_time_ms") not in (None, ""):
        try:
            out["base_time_ms"] = int(out["base_time_ms"])
        except (TypeError, ValueError):
            raise HTTPException(422, "base_time_ms 는 정수(epoch ms)여야 합니다")
    return out


def _collect_roster(members: list[RosterMember] | None, text: str | None) -> list[dict]:
    out = [m.model_dump() for m in members or []]
    out += parse_roster_text(text or "")
    seen: set[str] = set()
    uniq = []
    for m in out:
        if m["name"] and m["name"] not in seen:
            seen.add(m["name"])
            uniq.append(m)
    return uniq


@router.get("/sessions")
async def list_sessions(request: Request, status: str | None = None, limit: int = Query(100, ge=1, le=500),
                        user: User = Depends(require_viewer)):
    rt = _rt(request)
    rows = rt.repo.list_sessions(limit=limit, status=status or None)
    return {"items": [_session_out(rt, s) for s in rows]}


@router.post("/sessions")
async def create_session(request: Request, body: SessionCreate, user: User = Depends(require_operator)):
    rt = _rt(request)
    cfg = _validate_source_config(rt, body.source_type, body.source_config)
    title = body.title
    if cfg.get("caption_replay") is True and not title.startswith("[저장자막 재생]"):
        title = "[저장자막 재생] " + title
    s = rt.repo.create_session(
        title=title, source_type=body.source_type, committee=body.committee, agencies=body.agencies,
        meeting_date=body.meeting_date or _today(), source_config=cfg, save_audio=body.save_audio,
        created_by=user.actor, notes=body.notes,
    )
    sid = s["id"]
    members = _collect_roster(body.roster, body.roster_text)
    if members:
        rt.repo.set_roster(sid, members)
    rt.repo.audit(user.actor, "session.create", sid, {"title": body.title, "source_type": body.source_type,
                                                      "roster": len(members)})
    _publish_session(rt, sid)
    _pub(rt, sid, "roster.updated", {"members": _roster_out(rt.repo.get_roster(sid))})
    out_extra: dict[str, Any] = {}
    if body.start:
        try:
            await rt.ingest.start(sid, actor=user.actor)
        except Exception as e:
            log.warning("세션 생성 직후 수집 시작 실패(%s)", exception_label(e))
            out_extra["start_error"] = str(e) or type(e).__name__
    out = _session_out(rt, rt.repo.get_session(sid))
    return {**out, **out_extra}


@router.get("/sessions/{sid}")
async def get_session(request: Request, sid: str, user: User = Depends(require_viewer)):
    rt = _rt(request)
    return _session_out(rt, _session_or_404(rt, sid))


# 이 키가 바뀌면 다른 수집 대상이므로 수집원 상태(재개 위치 등)를 새로 시작한다
SOURCE_IDENTITY_KEYS = ("path", "xcode", "xcgcd", "xsami", "hls_url", "fixture", "caption_path", "quality", "caption_replay", "caption_replay_file")


@router.patch("/sessions/{sid}")
async def patch_session(request: Request, sid: str, body: SessionPatch, user: User = Depends(require_operator)):
    """세션 정보 수정. 수집 중(live)이 아닐 때는 수집원 종류·설정도 바꿀 수 있다(검증은 생성과 같다)."""
    rt = _rt(request)
    s = _session_or_404(rt, sid)
    fields = body.model_dump(exclude_none=True)
    source_change: dict[str, Any] | None = None
    if "source_type" in fields or "source_config" in fields:
        if _is_running(rt, s):
            raise HTTPException(409, "수집 중에는 수집 설정을 바꿀 수 없습니다. 먼저 일시정지하세요.")
        old_type, old_cfg = s["source_type"], dict(s.get("source_config") or {})
        st = fields.get("source_type", old_type)
        cfg = _validate_source_config(rt, st, fields.get("source_config", old_cfg))
        fields["source_config"] = cfg
        if st != old_type or cfg != old_cfg:
            source_change = {"from": old_type, "to": st, "keys": sorted(k for k in set(cfg) | set(old_cfg)
                                                                       if cfg.get(k) != old_cfg.get(k))}
            if st != old_type or any(cfg.get(k) != old_cfg.get(k) for k in SOURCE_IDENTITY_KEYS):
                # 다른 수집 대상: 이전 수집원의 재개 위치·오류 표시를 지우고 로그만 남긴다
                prev = dict(s.get("source_state") or {})
                line = (f"{serialize.hms(now_ms())} 수집 설정 변경: {SOURCE_LABELS.get(old_type, old_type)}"
                        f" → {SOURCE_LABELS.get(st, st)}")
                fields["source_state"] = {"phase": "stopped", "detail": "수집 설정이 바뀌었습니다 — 시작하면 새 설정으로 수집합니다",
                                          "last_rx_ms": None, "latency_ms": None, "rx_count": 0, "error": None,
                                          "log": (list(prev.get("log") or []) + [line])[-50:]}
    if fields:
        rt.repo.update_session(sid, **fields)
        rt.repo.audit(user.actor, "session.update", sid, {"fields": sorted(fields)})
    if source_change:
        rt.repo.audit(user.actor, "session.source_change", sid, source_change)
    return _publish_session(rt, sid)


def _remove_session_files(rt, sid: str) -> dict[str, int]:
    """세션의 업로드·원음·국회 자막 원본(data/raw/{sid}.jsonl) 파일을 지운다."""
    return maintenance.remove_session_files(rt.config, sid)


@router.delete("/sessions/{sid}")
async def delete_session(request: Request, sid: str, user: User = Depends(require_operator)):
    rt = _rt(request)
    s = _session_or_404(rt, sid)
    if _is_running(rt, s):
        raise HTTPException(409, "수집 중인 세션은 삭제할 수 없습니다. 먼저 종료하세요.")
    if getattr(rt, "audio_review", None) is not None:
        await rt.audio_review.stop(sid)
    rt.repo.delete_session(sid)
    _pub(rt, sid, "session.deleted", {"sid": sid, "source_type": s["source_type"]})
    rt.drop_pipeline(sid)
    maintenance.forget_session_state(rt.repo, sid)
    removed = await asyncio.to_thread(_remove_session_files, rt, sid)
    rt.repo.audit(user.actor, "session.delete", sid, {"title": s["title"], **removed})
    return {"ok": True}


async def _session_action(request: Request, sid: str, action: str, user: User) -> dict:
    rt = _rt(request)
    _session_or_404(rt, sid)
    try:
        await getattr(rt.ingest, action)(sid, actor=user.actor)
    except KeyError:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e) or "수집 상태를 바꿀 수 없습니다")
    return _session_out(rt, rt.repo.get_session(sid))


@router.post("/sessions/{sid}/start")
async def start_session(request: Request, sid: str, user: User = Depends(require_operator)):
    return await _session_action(request, sid, "start", user)


@router.post("/sessions/{sid}/pause")
async def pause_session(request: Request, sid: str, user: User = Depends(require_operator)):
    return await _session_action(request, sid, "pause", user)


@router.post("/sessions/{sid}/stop")
async def stop_session(request: Request, sid: str, user: User = Depends(require_operator)):
    return await _session_action(request, sid, "stop", user)


def _build_snapshot(rt, sid: str, user: User, last_event_id: int, analysis: dict | None,
                    *, recent: bool = False, utterance_limit: int = 100, before_utterance_id: int | None = None,
                    block_id: int | None = None, around_utterance_id: int | None = None,
                    bookmarks_only: bool = False, q: str = "", selected_ids: list[int] | None = None,
                    block_summaries: bool = False) -> dict:
    repo = rt.repo
    s = repo.get_session(sid)
    if s is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    page = None
    from ..display_reads import enabled as display_enabled, utterance_page as saved_page
    common = (repo.display_snapshot(sid) if display_enabled(repo, sid)
              or getattr(rt, "display", None) is not None and s.get("source_type") == "assembly_caption" else None)
    if common is not None:
        if block_id is not None and not any(row["id"] == block_id for row in common.get("blocks", [])):
            raise HTTPException(404, "질의 블록을 찾을 수 없습니다")
        try:
            rows, page = (saved_page(repo, common, sid, limit=utterance_limit, before_id=before_utterance_id,
                block_id=block_id, around_id=around_utterance_id,
                bookmark_actor=user.actor if bookmarks_only else None, query=q.strip(), selected_ids=selected_ids)
                if recent else (common.get("utterances", []), None))
        except KeyError:
            raise HTTPException(404, "발언을 찾을 수 없습니다") from None
        blocks = common.get("blocks", [])
    elif recent:
        if block_id is not None and not repo.stage_block_record(sid, block_id):
            raise HTTPException(404, "질의 블록을 찾을 수 없습니다")
        try:
            rows, page = repo.stage_utterance_page(sid, limit=utterance_limit, before_id=before_utterance_id,
                block_id=block_id, around_id=around_utterance_id,
                bookmark_actor=user.actor if bookmarks_only else None, query=q.strip(), selected_ids=selected_ids)
        except KeyError:
            raise HTTPException(404, "발언을 찾을 수 없습니다") from None
        blocks = [serialize.block(b) for b in (repo.list_blocks(sid) if block_summaries else repo.stage_block_headers(sid))]
    else:
        rows = repo.list_utterances(sid, limit=-1)
        blocks = [get_block_display(repo, b) for b in repo.list_blocks(sid)]
    display_fields = {}
    if common is not None:
        wanted = {row["id"] for row in rows}
        selected_display = [row for row in common.get("display_rows", [])
                            if not recent or wanted.intersection(row.get("source_utterance_ids", []))]
        selected_blocks = {row.get("block_id") for row in selected_display}
        display_fields = {key: common[key] for key in ("display_generation", "display_contract", "display_version",
                          "display_valid_block_ids", "display_valid_row_ids", "valid_document_ids", "display_utterance_ids") if key in common}
        if recent and "display_utterance_ids" in display_fields:
            display_fields["display_utterance_ids"] = [uid for uid in display_fields["display_utterance_ids"] if uid in wanted]
        display_fields.update(display_rows=selected_display, display_blocks=[row for row in common.get("display_blocks", [])
                              if not recent or row.get("blockId") in selected_blocks])
        needed_sources = {uid for projection in display_fields["display_blocks"]
                          for uid in projection["document"].get("source_ids", [])}
        needed_sources.update(int(uid) for projection in display_fields["display_blocks"]
                              for uid in (projection["document"].get("source_revisions") or {}) if str(uid).isdigit())
        display_fields["sources"] = [row for row in common.get("sources", []) if not recent or row["id"] in needed_sources]
    return {
        "session": _session_out(rt, s),
        "blocks": blocks,
        "utterances": [serialize.utterance(u) for u in rows],
        "items": [serialize.item(i) for i in repo.list_items(sid, include_dismissed=True, limit=2000 if recent else -1)],
        "alerts": [serialize.alert(a) for a in repo.list_alerts(sid, limit=500 if recent else -1)],
        "summary": serialize.summary(repo.latest_summary(sid, "session")),
        "rolling": serialize.summary(repo.latest_summary(sid, "rolling")),
        "roster": _roster_out(repo.get_roster(sid)),
        "bookmarks": [b["utterance_id"] for b in repo.list_bookmarks(sid, user.actor)],
        "analysis": analysis,
        "last_event_id": last_event_id,
        "me": user.public(),
        **display_fields,
        **({"utterance_page": page, "valid_utterance_ids": repo.stage_utterance_ids(sid)} if page is not None else {}),
    }


@router.get("/sessions/{sid}/snapshot")
async def snapshot(request: Request, sid: str, recent: bool = False,
                   utterance_limit: int = Query(100, ge=1, le=500),
                   before_utterance_id: int | None = Query(None, gt=0), block_id: int | None = Query(None, gt=0),
                   around_utterance_id: int | None = Query(None, gt=0), bookmarks_only: bool = False,
                   q: str = Query("", max_length=300), utterance_ids: str | None = Query(None, max_length=20000),
                   block_summaries: bool = False, user: User = Depends(require_viewer)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    # 스냅샷을 읽기 전의 이벤트 번호: 이후 이벤트는 SSE(?last_id=)로 이어 받는다(중복은 upsert 라 무해)
    last_event_id = rt.bus.last_id
    try:
        analysis = rt.analysis.status()
    except Exception:
        analysis = None
    selected_ids = None
    if utterance_ids is not None:
        pieces = utterance_ids.split(",") if utterance_ids else []
        if any(len(value) > 19 or not value.isascii() or not value.isdecimal() or int(value) <= 0
               or int(value) > 2**63-1 for value in pieces):
            raise HTTPException(422, "발언 ID 목록을 확인하세요")
        selected_ids = sorted(set(int(value) for value in pieces))
        if len(selected_ids) > 2000:
            raise HTTPException(413, "한 번에 2,000개 이하 발언을 선택하세요")
    options = dict(recent=recent, utterance_limit=utterance_limit, before_utterance_id=before_utterance_id,
                   block_id=block_id, around_utterance_id=around_utterance_id, bookmarks_only=bookmarks_only, q=q,
                   selected_ids=selected_ids, block_summaries=block_summaries)
    if recent and getattr(rt, "stages", None) is not None:
        return await rt.stages.read_call(lambda view: _build_snapshot(view.rt, sid, user, last_event_id, analysis, **options))
    return await asyncio.to_thread(_build_snapshot, rt, sid, user, last_event_id, analysis, **options)


@router.get("/transcript-review/status")
async def transcript_review_status(request: Request, user: User = Depends(require_operator)):
    return _rt(request).reviews.status()


@router.get("/sessions/{sid}/transcript-reviews")
async def transcript_reviews(request: Request, sid: str, block_id: int | None = None,
                             user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    return {"items": rt.repo.list_transcript_reviews(sid, block_id),
            "audio": rt.audio_review.status(sid)}


@router.get("/sessions/{sid}/audio-review/evidence")
async def audio_review_evidence(request: Request, sid: str, utterance_id: int | None = None,
                                user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    return {"items": rt.repo.list_audio_review_evidence(sid, utterance_id), "audio": rt.audio_review.status(sid)}


@router.get("/sessions/{sid}/audio-review/clips/{eid}")
async def audio_review_clip(request: Request, sid: str, eid: int, user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    evidence = rt.repo.get_audio_review_evidence(eid)
    if not evidence or evidence["session_id"] != sid:
        raise HTTPException(404, "음성 대조 기록을 찾을 수 없습니다")
    path = Path(evidence["payload"].get("clip_path") or "")
    base = rt.config.audio_dir
    root = base / sid / "review"
    parents = [base, base / sid, root]
    if (any(p.is_symlink() for p in parents) or not path.is_file() or path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())):
        raise HTTPException(404, "보관된 원음이 없거나 보존 기간이 끝났습니다")
    try:
        content = await asyncio.to_thread(_read_review_clip, base, sid, path.name)
    except (OSError, ValueError):
        raise HTTPException(404, "안전하게 읽을 수 있는 원음 후보가 없습니다") from None
    return Response(content, media_type="audio/wav", headers={"Cache-Control": "no-store"})


def _read_review_clip(base: Path, sid: str, filename: str) -> bytes:
    """각 경로를 심볼릭 링크 없이 열어 검수용 작은 WAV만 읽는다."""
    from ..secure_files import open_regular_file
    with open_regular_file(base / sid / "review" / filename) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("원음 후보 파일 한도 초과")
        return stream.read(1024 * 1024)


class TranscriptReviewIn(_Model):
    model_config = ConfigDict(extra="forbid")
    utterance_ids: list[int] | None = None

    @field_validator("utterance_ids", mode="before")
    @classmethod
    def check_window(cls, value):
        if value is None:
            return None
        if (not isinstance(value, list) or not 1 <= len(value) <= 6
                or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in value)
                or len(set(value)) != len(value)):
            raise ValueError("검토할 원문 ID는 서로 다른 정수 1~6개여야 합니다")
        return value


@router.post("/sessions/{sid}/blocks/{bid}/transcript-review")
async def transcript_review(request: Request, sid: str, bid: int, body: TranscriptReviewIn | None = Body(None),
                            user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    block = _block_or_404(rt, bid)
    if block["session_id"] != sid:
        raise HTTPException(404, "질의 블록을 찾을 수 없습니다")
    try:
        if body and body.utterance_ids is not None:
            result = await rt.reviews.review(sid, bid, utterance_ids=body.utterance_ids)
        else:
            result = await rt.reviews.review(sid, bid)
    except KeyError:
        raise HTTPException(404, "검토할 원문을 찾을 수 없습니다") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if not (body and body.utterance_ids is not None) and not block.get("is_open"):
        rt.analysis.on_review_accepted(sid, bid)
    rt.repo.audit(user.actor, "transcript.review.request", sid, {"block_id": bid})
    return result


@router.post("/sessions/{sid}/transcript-reviews/{rid}/{action}")
async def transcript_review_action(request: Request, sid: str, rid: int, action: Literal["accept", "reject"],
                                   user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    try:
        return await getattr(rt.reviews, action)(sid, rid, user.actor)
    except KeyError:
        raise HTTPException(404, "화자 검토를 찾을 수 없습니다")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


class AudioReviewAction(_Model):
    mode: Literal["retain", "verify"] = "retain"


@router.post("/sessions/{sid}/utterances/{uid}/audio-review")
async def audio_review(request: Request, sid: str, uid: int, body: AudioReviewAction,
                       user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    u = _utt_or_404(rt, uid)
    if u["session_id"] != sid:
        raise HTTPException(404, "발언을 찾을 수 없습니다")
    raise HTTPException(410, "음성 녹음 보정은 제거했습니다. 종료된 자막 블록 전체를 보정합니다.")


@router.put("/sessions/{sid}/roster")
async def put_roster(request: Request, sid: str, body: RosterPut, user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    members = _collect_roster(body.members, body.text)
    rows = rt.repo.set_roster(sid, members)
    rt.pipeline(sid).reload_settings()
    rt.repo.audit(user.actor, "roster.update", sid, {"count": len(rows)})
    _pub(rt, sid, "roster.updated", {"members": _roster_out(rt.repo.get_roster(sid))})
    for block in rt.repo.list_blocks(sid, kind="question"):
        if block.get("is_open"):
            _pub(rt, sid, "block.upsert", get_block_display(rt.repo, block))
    return {"members": _roster_out(rows)}


@router.post("/sessions/{sid}/manual")
async def manual_input(request: Request, sid: str, body: ManualIn, user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    p = rt.pipeline(sid)
    if body.marker:
        # 기록 표지(정회·속개·중계 중단·메모)는 수집원 종류·상태와 무관하게 남길 수 있다
        row = await p.add_marker(body.text)
        rt.repo.audit(user.actor, "session.marker", sid, {"text": body.text[:200]})
        return {"ok": True, "id": (row or {}).get("id")}
    await p.add_manual(body.text, speaker=body.speaker, role=body.role)
    return {"ok": True}


def _safe_filename(name: str) -> tuple[str, str]:
    """(저장용 이름, 확장자). 경로 성분 제거·허용 문자만 남긴다."""
    base = re.split(r"[\\/]", name or "")[-1].strip()
    base = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", base).lstrip("._")[-100:]
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    return base or "upload", ext


def _copy_limited(src, dest: Path, limit: int) -> int:
    size = 0
    src.seek(0)
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise OverflowError
                out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return size


def _drop_replaced_upload(old: Any, dest: Path, dest_dir: Path) -> str | None:
    """다시 올리면 이 세션 업로드 폴더 안의 이전 파일을 지운다(최대 2GB 고아 파일 방지). 지운 파일 이름."""
    if not old:
        return None
    try:
        op = Path(str(old)).resolve()
        if op == dest or not op.is_relative_to(dest_dir) or not op.is_file():
            return None
        op.unlink()
        return op.name
    except OSError as e:
        log.warning("이전 업로드 파일 삭제 실패(%s)", exception_label(e))
        return None


@router.post("/sessions/{sid}/upload")
async def upload(request: Request, sid: str, user: User = Depends(require_operator)):
    """녹음 파일 업로드(multipart 'file'). 인증 확인 뒤에 본문을 읽는다."""
    rt = _rt(request)
    s = _session_or_404(rt, sid)
    if s["source_type"] != "file":
        raise HTTPException(400, "녹음 파일 세션에서만 업로드할 수 있습니다")
    if _is_running(rt, s):
        raise HTTPException(409, "수집 중에는 파일을 바꿀 수 없습니다")
    try:
        clen = int(request.headers.get("content-length") or 0)
    except ValueError:
        clen = 0
    if clen > MAX_UPLOAD_BYTES + 1024 * 1024:
        raise HTTPException(413, "파일이 너무 큽니다(최대 2GB)")
    try:
        form = await request.form(max_files=1, max_fields=5)
    except Exception as e:
        raise HTTPException(400, f"업로드 형식이 올바르지 않습니다({type(e).__name__})")
    try:
        f = form.get("file")
        if not isinstance(f, UploadFile) or not f.filename:
            raise HTTPException(400, "업로드할 파일(file)이 없습니다")
        name, ext = _safe_filename(f.filename)
        if ext not in UPLOAD_EXTS:
            raise HTTPException(400, "허용되지 않는 파일 형식입니다(허용: " + ", ".join(sorted(UPLOAD_EXTS)) + ")")
        dest_dir = (rt.config.upload_dir / sid).resolve()
        if not dest_dir.is_relative_to(rt.config.upload_dir.resolve()):
            raise HTTPException(400, "잘못된 경로입니다")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = (dest_dir / f"{datetime.now(KST).strftime('%Y%m%d-%H%M%S')}_{name}").resolve()
        if not dest.is_relative_to(dest_dir):
            raise HTTPException(400, "잘못된 파일 이름입니다")
        try:
            size = await asyncio.to_thread(_copy_limited, f.file, dest, MAX_UPLOAD_BYTES)
        except OverflowError:
            raise HTTPException(413, "파일이 너무 큽니다(최대 2GB)")
    finally:
        await form.close()
    cfg = dict(s.get("source_config") or {})
    old_path = cfg.get("path")
    cfg.update(path=str(dest), filename=f.filename[:200], size=size)
    rt.repo.update_session(sid, source_config=cfg)
    replaced = await asyncio.to_thread(_drop_replaced_upload, old_path, dest, dest_dir)
    rt.repo.audit(user.actor, "session.upload", sid, {"filename": f.filename[:200], "size": size,
                                                      **({"replaced": replaced} if replaced else {})})
    _publish_session(rt, sid)
    return {"ok": True, "path": str(dest), "filename": dest.name, "size": size}


@router.post("/sessions/{sid}/caption-replay-upload")
async def upload_caption_replay(request: Request, sid: str, user: User = Depends(require_operator)):
    """선택한 원수신 파일의 독립 사본을 보관한다. 기존 회의 자료는 교체하지 않는다."""
    from ..ingest.caption_replay import MAX_BYTES, load_recording
    rt = _rt(request)
    session = _session_or_404(rt, sid)
    if session["source_type"] != "assembly_caption" or (session.get("source_config") or {}).get("caption_replay") is not True:
        raise HTTPException(400, "저장 국회 자막 재생 세션에서만 올릴 수 있습니다")
    def assert_empty():
        if _is_running(rt, _session_or_404(rt, sid)):
            raise HTTPException(409, "재생 중에는 파일을 바꿀 수 없습니다")
        if rt.repo.has_caption_sources(sid) or rt.repo.list_utterances(sid, limit=1):
            raise HTTPException(409, "기존 기록을 보존합니다. 다른 파일은 새 재생 세션에서 선택하세요")
    assert_empty()
    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    if content_length > MAX_BYTES + 1024 * 1024:
        raise HTTPException(413, "저장 자막 파일은 최대 16MB입니다")
    async def bounded_stream():
        consumed = 0
        async for chunk in request.stream():
            consumed += len(chunk)
            if consumed > MAX_BYTES + 1024 * 1024:
                raise HTTPException(413, "저장 자막 파일은 최대 16MB입니다")
            yield chunk
    try:
        from starlette.formparsers import MultiPartParser
        form = await MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=2).parse()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "저장 자막 업로드 형식을 확인하세요") from None
    dest, adopted = None, False
    try:
        uploaded = form.get("file")
        if not isinstance(uploaded, UploadFile) or not uploaded.filename:
            raise HTTPException(400, "보관한 국회 자막 JSONL 파일을 선택하세요")
        _, extension = _safe_filename(uploaded.filename)
        if extension != "jsonl":
            raise HTTPException(400, "국회 원수신 JSONL 파일만 올릴 수 있습니다")
        folder = (rt.config.upload_dir / sid).resolve()
        if not folder.is_relative_to(rt.config.upload_dir.resolve()):
            raise HTTPException(400, "저장 자막 보관 위치를 확인하세요")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / ("caption-replay-" + uuid4().hex + ".jsonl")
        try:
            size = await asyncio.to_thread(_copy_limited, uploaded.file, dest, MAX_BYTES)
            rows = await asyncio.to_thread(load_recording, dest)
        except OverflowError:
            raise HTTPException(413, "저장 자막 파일은 최대 16MB입니다") from None
        except (OSError, ValueError) as error:
            raise HTTPException(422, str(error)) from None
        assert_empty()
        current = _session_or_404(rt, sid)
        cfg = dict(current.get("source_config") or {})
        if cfg.get("caption_replay") is not True:
            raise HTTPException(409, "재생 방식이 바뀌었습니다. 파일을 다시 선택하세요")
        cfg.update(caption_replay_file=str(dest), caption_replay_messages=len(rows), caption_replay_bytes=size)
        rt.repo.update_session(sid, source_config=cfg, source_state={})
        adopted = True
        rt.repo.audit(user.actor, "session.caption_replay_upload", sid, {"size":size, "messages":len(rows)})
        _publish_session(rt, sid)
        return {"ok":True, "messages":len(rows), "size":size, "recording_replay":True}
    except BaseException:
        if dest is not None and not adopted:
            dest.unlink(missing_ok=True)  # 이번 요청이 만든 미채택 사본만 정리한다.
        raise
    finally:
        await form.close()


@router.post("/sessions/{sid}/summarize")
async def summarize_session(request: Request, sid: str, body: SummarizeIn | None = None,
                            user: User = Depends(require_operator)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    try:
        result = await rt.analysis.summarize_session(sid, scope=(body.scope if body else "session"))
    except Exception as e:
        log.error("회의 요약 실패(%s)", exception_label(e))
        raise HTTPException(503, AI_SUMMARY_FAILURE) from None
    return result or {}


@router.get("/sessions/{sid}/report")
async def situation_report(request: Request, sid: str, org_only: bool = False, since_ms: int | None = None,
                           user: User = Depends(require_viewer)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    mod = _lazy_module("gukgam.reports.situation", "상황보고")
    return await asyncio.to_thread(_call_optional_kwargs, mod.build_situation_report, rt.repo, sid,
                                   org_only=org_only, since_ms=since_ms, base_url=_base_url(request))


def _accepts(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


@router.get("/sessions/{sid}/export")
async def export(request: Request, sid: str, format: str = "md", org_only: bool = False,
                 user: User = Depends(require_viewer)):
    """기록 내보내기. org_only=1 이면 우리 기관 관련 블록과 그 항목만(상황보고 '우리 기관 관련만'과 같은 범위)."""
    rt = _rt(request)
    _session_or_404(rt, sid)
    fmt = format.lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(400, "지원하지 않는 형식입니다(md, txt, docx, json, csv)")
    mod = _lazy_module("gukgam.reports.export", "내보내기")
    fn = mod.export_session
    kwargs: dict[str, Any] = {}
    if org_only:
        if not _accepts(fn, "org_only"):
            # 범위를 줄이지 못하면 전체 기록이 나가므로 거절한다
            raise HTTPException(503, "'우리 기관 관련만' 내보내기 모듈 준비 중입니다")
        kwargs["org_only"] = True
    if _accepts(fn, "base_url"):
        kwargs["base_url"] = _base_url(request)
    body, media_type, filename = await asyncio.to_thread(fn, rt.repo, sid, fmt, **kwargs)
    rt.repo.audit(user.actor, "session.export", sid, {"format": fmt, "org_only": org_only})
    return Response(body, media_type=media_type, headers={"Content-Disposition": _content_disposition(filename)})


@router.post("/sessions/{sid}/alerts/ack")
async def ack_alerts(request: Request, sid: str, body: AlertAck | None = None, user: User = Depends(require_viewer)):
    rt = _rt(request)
    _session_or_404(rt, sid)
    rt.repo.ack_alerts(sid, body.alert_id if body else None)
    _pub(rt, sid, "stats", rt.repo.session_stats(sid))
    return {"ok": True}


# ══ 발언 ═════════════════════════════════════════════════════════

@router.get("/utterances/{uid}")
async def get_utterance(request: Request, uid: int, user: User = Depends(require_viewer)):
    rt = _rt(request)
    repo = rt.repo
    u = _utt_or_404(rt, uid)
    sid = u["session_id"]
    prev, nxt = repo.neighbor_utterances(uid)
    block = repo.get_block(u["block_id"]) if u.get("block_id") else None
    items = [i for i in repo.list_items(sid, block_id=u.get("block_id"), include_dismissed=True)
             if i.get("utterance_id") == uid]
    same = []
    if u.get("speaker"):
        same = [x for x in repo.list_utterances(sid, speaker=u["speaker"], final_only=True, limit=200)
                if x["id"] != uid][:20]
    common = None
    shown = []
    from ..display_reads import enabled as display_enabled, display_utterances
    if display_enabled(repo, sid):
        common = repo.display_snapshot(sid)
        selected = display_utterances(common, sid)
        shown = [row for row in selected if uid in row.get("source_utterance_ids", [])]
        if shown:
            block = next((row for row in common.get("blocks", []) if row["id"] == shown[0].get("block_id")), block)
            same = [row for row in selected if type(row.get("id")) is int and row["id"] > 0
                    and row.get("speaker") == shown[0].get("speaker")
                    and uid not in row.get("source_utterance_ids", [])][:20]
        # 이웃 링크의 표시도 같은 저장본을 쓴다. 원문 행의 UID와 링크는 바꾸지 않는다.
        by_uid = {}
        for row in selected:
            for source_uid in row.get("source_utterance_ids", []):
                by_uid.setdefault(source_uid, row)
        prev = {**by_uid.get(prev["id"], prev), "id": prev["id"]} if prev else None
        nxt = {**by_uid.get(nxt["id"], nxt), "id": nxt["id"]} if nxt else None
    bookmarked = any(b["utterance_id"] == uid for b in repo.list_bookmarks(sid, user.actor))
    return {
        "utterance": serialize.utterance(u),
        "block": serialize.block(block) if block else None,
        "prev": serialize.utterance(prev) if prev else None,
        "next": serialize.utterance(nxt) if nxt else None,
        "items": [serialize.item(i) for i in items],
        "session": serialize.session(repo.get_session(sid)),
        "same_speaker": [serialize.utterance(x) for x in same],
        "bookmarked": bookmarked,
        **({"display_generation": common["display_generation"],
            "display_rows": [serialize.utterance(row) for row in shown]} if common is not None else {}),
    }


@router.patch("/utterances/{uid}")
async def patch_utterance(request: Request, uid: int, body: UtterancePatch, user: User = Depends(require_operator)):
    rt = _rt(request)
    u = _utt_or_404(rt, uid)
    try:
        u2 = await rt.pipeline(u["session_id"]).edit_utterance(
            uid, text=body.text, speaker=body.speaker, speaker_role=body.speaker_role, actor=user.actor)
    except KeyError:
        raise HTTPException(404, "발언을 찾을 수 없습니다")
    return _mutation_display_generation(rt, u["session_id"], serialize.utterance(u2))


@router.post("/utterances/{uid}/bookmark")
async def bookmark(request: Request, uid: int, body: BookmarkIn | None = None, user: User = Depends(require_viewer)):
    rt = _rt(request)
    u = _utt_or_404(rt, uid)
    on = rt.repo.toggle_bookmark(u["session_id"], uid, user.actor, note=body.note if body else "")
    return {"bookmarked": on}


# ══ 질의 블록 ════════════════════════════════════════════════════

def _clean_member_name(name: str) -> str:
    name = re.sub(r"\s+", "", name or "")
    return re.sub(r"(?:위원장|위원|의원)?님?$", "", name) or name


@router.patch("/blocks/{bid}")
async def patch_block(request: Request, bid: int, body: BlockPatch, user: User = Depends(require_operator)):
    rt = _rt(request)
    b = _block_or_404(rt, bid)
    sid = b["session_id"]
    name = _clean_member_name(body.questioner)
    party = _norm_party(body.questioner_party)
    if not party:
        e, _fuzzy = Roster(rt.repo.get_roster(sid)).match(name)
        if e and e.name == name:
            party = e.party
    try:
        b2 = await rt.pipeline(sid).set_block_questioner(bid, name, party, actor=user.name or user.role)
    except KeyError:
        raise HTTPException(404, "질의 블록을 찾을 수 없습니다")
    return _mutation_display_generation(rt, sid, serialize.block(b2))


@router.post("/blocks/{bid}/summarize")
async def summarize_block(request: Request, bid: int, user: User = Depends(require_operator)):
    rt = _rt(request)
    sid = rt.repo.stage_block_session(bid)
    if sid is None:
        raise HTTPException(404, "질의 블록을 찾을 수 없습니다")
    try:
        read_saved = getattr(rt.analysis, "read_stage_summary_result", None)
        saved = await read_saved(sid, bid) if callable(read_saved) else None
        if saved is not None:
            # 의원별 화면과 블록 요청은 같은 저장 요약을 사용하며 실행 중인 작업을 다시 시작하지 않는다.
            return {**serialize.block({**saved["block"], "summary": saved.get("summary")}),
                    "summary_state": saved["summary_state"]}
        await rt.analysis.summarize_block(sid, bid)
    except Exception as e:
        log.error("블록 요약 실패(%s)", exception_label(e))
        raise HTTPException(503, AI_SUMMARY_FAILURE) from None
    return serialize.block(rt.repo.get_block(bid))


@router.post("/blocks/{bid}/review")
async def review_block(request: Request, bid: int, body: BlockReview | None = None,
                       user: User = Depends(require_operator)):
    """AI 초안 검수 완료 표시(결정사항 5.1). {"status":"draft"} 로 되돌릴 수 있다."""
    rt = _rt(request)
    b = _block_or_404(rt, bid)
    status = body.status if body else "reviewed"
    if status == "reviewed":
        fields = {"review_status": "reviewed", "reviewed_by": user.name or "operator", "reviewed_at": now_ms()}
    else:
        fields = {"review_status": "draft", "reviewed_by": "", "reviewed_at": None}
    b2 = rt.repo.update_block(bid, **fields)
    rt.repo.audit(user.actor, "block.review", str(bid), {"status": status})
    out = serialize.block(b2)
    _pub(rt, b["session_id"], "block.upsert", out)
    return _mutation_display_generation(rt, b["session_id"], out)


BLOCK_SUMMARY_KEYS = frozenset({"one_line", "question_points", "answer_points", "issues", "data_requests",
                                "commitments", "keywords", "response_type", "urgency", "org_relevance", "org_reason"})
MAX_SUMMARY_PATCH_BYTES = 64 * 1024


@router.patch("/blocks/{bid}/summary")
async def patch_block_summary(request: Request, bid: int, body: dict[str, Any] = Body(...),
                              user: User = Depends(require_operator)):
    """검수자가 블록 요약(AI 초안)을 고친다. 본문은 고칠 키만(one_line, question_points, answer_points, issues,
    data_requests, commitments, keywords, response_type, urgency, org_relevance, org_reason). 고친 요약은
    자동 재요약이 덮어쓰지 않는다(수동 '다시 요약'만 덮어씀). 반환: block."""
    rt = _rt(request)
    b = _block_or_404(rt, bid)
    if not isinstance(body, dict) or not body:
        raise HTTPException(422, "고칠 내용이 없습니다")
    unknown = sorted(k for k in body if k not in BLOCK_SUMMARY_KEYS)
    if unknown:
        raise HTTPException(422, f"고칠 수 없는 항목입니다: {', '.join(unknown)}")
    if len(json.dumps(body, ensure_ascii=False)) > MAX_SUMMARY_PATCH_BYTES:
        raise HTTPException(413, "요약 내용이 너무 깁니다")
    edit = getattr(rt.analysis, "edit_block_summary", None)
    if edit is None:
        raise HTTPException(503, "요약 수정 기능 준비 중입니다")
    try:
        out = edit(b["session_id"], bid, body, user.name or user.actor)
        if inspect.isawaitable(out):
            out = await out
    except KeyError:
        raise HTTPException(404, "질의 블록을 찾을 수 없습니다")
    except ValueError as e:
        raise HTTPException(422, str(e) or "요약 값이 올바르지 않습니다")
    return _mutation_display_generation(rt, b["session_id"], out)


@router.get("/blocks/{bid}/card")
async def block_card(request: Request, bid: int, user: User = Depends(require_viewer)):
    """질의·답변 요지 카드(메신저 전파용, 결정사항 5.5)."""
    rt = _rt(request)
    _block_or_404(rt, bid)
    mod = _lazy_module("gukgam.reports.cards", "요지 카드")
    return await asyncio.to_thread(_call_optional_kwargs, mod.build_block_card, rt.repo, bid,
                                   base_url=_base_url(request))


# ══ 후속조치 항목 ═════════════════════════════════════════════════

ITEM_CSV_COLUMNS = [("id", "번호"), ("session_title", "회의"), ("time", "시각"), ("kind_label", "종류"),
                    ("strength", "강도"), ("text", "요지"), ("requester", "요구 위원"), ("speaker", "발언자"),
                    ("assignee_dept", "담당부서"), ("due_date", "기한"), ("status_label", "상태"), ("note", "메모"),
                    ("quote", "원문")]


@router.get("/items")
async def list_items(request: Request, session_id: str | None = None, kind: str | None = None,
                     status: str | None = None, dept: str | None = None, block_id: int | None = None,
                     include_dismissed: bool = False, format: str = "json",
                     limit: int = Query(2000, ge=1, le=10000), user: User = Depends(require_viewer)):
    rt = _rt(request)
    if kind and kind not in ITEM_KINDS:
        raise HTTPException(400, "알 수 없는 항목 종류입니다")
    if status and status not in ITEM_STATUSES:
        raise HTTPException(400, "알 수 없는 처리 상태입니다")
    rows = rt.repo.list_items(session_id or None, kind=kind or None, status=status or None, block_id=block_id,
                              dept=dept or None, include_dismissed=include_dismissed, limit=limit)
    items = [serialize.item(r) for r in rows]
    if format.lower() == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([label for _k, label in ITEM_CSV_COLUMNS])
        for it in items:
            w.writerow([csv_cell(it.get(k)) if it.get(k) is not None else "" for k, _l in ITEM_CSV_COLUMNS])
        fname = f"후속조치_{datetime.now(KST).strftime('%Y%m%d-%H%M')}.csv"
        return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": _content_disposition(fname)})
    return {"items": items}


@router.post("/items")
async def create_item(request: Request, body: ItemCreate, user: User = Depends(require_operator)):
    rt = _rt(request)
    repo = rt.repo
    sid = body.session_id
    _session_or_404(rt, sid)
    u = None
    if body.utterance_id is not None:
        u = repo.get_utterance(body.utterance_id)
        if u is None or u["session_id"] != sid:
            raise HTTPException(400, "이 세션의 발언이 아닙니다")
    block_id = body.block_id if body.block_id is not None else (u.get("block_id") if u else None)
    block = None
    if block_id is not None:
        block = repo.get_block(block_id)
        if block is None or block["session_id"] != sid:
            raise HTTPException(400, "이 세션의 질의 블록이 아닙니다")
    requester = body.requester
    if not requester and block and block["kind"] == "question" and body.kind != "commitment":
        requester = block.get("questioner") or ""
    quote_text = (u["text"] if u else "")[:500]
    dept = body.assignee_dept or rules.assign_department(f"{body.text} {quote_text}",
                                                         settings.get(repo, "departments") or [])
    row, _ = repo.add_item(sid, kind=body.kind, text=body.text, quote=quote_text, speaker=(u or {}).get("speaker", ""),
                           requester=requester, block_id=block_id, utterance_id=body.utterance_id, origin="manual",
                           confidence=1.0, assignee_dept=dept, strength=body.strength, due_date=body.due_date,
                           dedupe=False)
    # 수동 생성 항목은 사람 것(edited=1): 재추출·재요약·질의자 변경이 덮어쓰지 않는다
    repo.update_item(row["id"], by_human=True, **({"note": body.note} if body.note else {}))
    repo.audit(user.actor, "item.create", str(row["id"]), {"session_id": sid, "kind": body.kind})
    out = serialize.item(_item_full(rt, row["id"]))
    _pub(rt, sid, "item.upsert", out)
    _pub(rt, sid, "stats", repo.session_stats(sid))
    return out


@router.patch("/items/{iid}")
async def patch_item(request: Request, iid: int, body: ItemPatch, user: User = Depends(require_operator)):
    rt = _rt(request)
    row = rt.repo.get_item(iid)
    if row is None or row.get("status") == "superseded":      # 재추출·재요약으로 대체된 항목은 내부 상태
        raise HTTPException(404, "항목을 찾을 수 없습니다")
    fields = body.model_dump(exclude_none=True)
    if fields:
        rt.repo.update_item(iid, by_human=True, **fields)
        rt.repo.audit(user.actor, "item.update", str(iid), fields)
    out = serialize.item(_item_full(rt, iid))
    _pub(rt, row["session_id"], "item.upsert", out)
    if "status" in fields or "kind" in fields:
        _pub(rt, row["session_id"], "stats", rt.repo.session_stats(row["session_id"]))
    return out


# ══ 검색 ═════════════════════════════════════════════════════════

@router.get("/search")
async def search(request: Request, q: str = Query("", max_length=100), session_id: str | None = None,
                 speaker: str | None = Query(None, max_length=40), limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0, le=100_000), user: User = Depends(require_viewer)):
    rt = _rt(request)
    q = q.strip()
    if not q:
        return {"items": [], "q": q}
    try:
        from ..display_reads import search as display_search
        rows = await asyncio.to_thread(display_search, rt.repo, q, session_id=session_id or None,
                                       speaker=speaker or None, limit=limit, offset=offset)
    except sqlite3.OperationalError as e:
        log.info("검색 실패(%s)", exception_label(e))
        raise HTTPException(400, "검색어를 처리할 수 없습니다. 다른 표현으로 검색하세요.")
    items = [{**serialize.utterance(r), "session_title": r.get("session_title") or "",
              "meeting_date": r.get("meeting_date") or ""} for r in rows]
    return {"items": items, "q": q}


# ══ 설정·감사 ═════════════════════════════════════════════════════

_PROVIDERS = {"llm": ("local_mlx", "desktop_chatgpt", "openai_compat", "anthropic", "rules"), "stt": ("mlx_whisper", "nemotron_cpu", "none")}
_KEYWORD_CATEGORIES = ("agency", "person", "project", "custom")
_INT_LIMITS = {("retention", "audio_days"): 3650, ("retention", "raw_days"): 3650,
               ("retention", "transcript_days"): 36500, ("assembly", "auto_stop_minutes"): 24 * 60}


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(v)


def _str_list(v: Any, limit: int = 500) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    if not isinstance(v, list):
        raise HTTPException(422, "목록(배열) 또는 쉼표로 구분한 문자열이어야 합니다")
    out: list[str] = []
    for x in v:
        s = str(x).strip()[:100]
        if s and s not in out:
            out.append(s)
    return out[:limit]


def _normalize_setting(key: str, value: Any) -> Any:
    """설정 값 형태 검증·정리. 기본값에 없는 하위 키는 버린다."""
    default = settings.DEFAULTS[key]
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise HTTPException(422, f"'{key}' 설정은 객체(JSON object)여야 합니다")
        out: dict[str, Any] = {}
        for k, v in value.items():
            if key == "llm" and k == "openai_key_base_url":
                continue
            if key == "llm" and k in {"clear_openai_api_key", "clear_anthropic_api_key"}:
                if type(v) is not bool:
                    raise HTTPException(422, "저장된 키 삭제 여부를 확인하세요.")
                out[k] = v
                continue
            if k not in default:
                continue
            if key == "stt" and k in settings.NEMOTRON_LIMITS:
                lo, hi = settings.NEMOTRON_LIMITS[k]
                if type(v) is not int or not lo <= v <= hi:
                    raise HTTPException(422, f"'{k}'는 {lo}~{hi} 범위의 정수여야 합니다")
                out[k] = v
                continue
            if key == "transcript_review" and k == "role_decision_provider":
                if not isinstance(v, str) or v not in settings.ROLE_DECISION_PROVIDERS:
                    raise HTTPException(422, "사전 역할 판정은 추가 모델 없음·Jev·Julia-1 중에서 선택하세요")
                out[k] = v
                continue
            if key == "transcript_review" and k in settings.JULIA_LIMITS:
                lo, hi = settings.JULIA_LIMITS[k]
                if type(v) is not int or not lo <= v <= hi:
                    raise HTTPException(422, f"'{k}'는 {lo}~{hi} 범위의 정수여야 합니다")
                out[k] = v
                continue
            d = default[k]
            if isinstance(d, bool):
                out[k] = _as_bool(v)
            elif isinstance(d, int):
                try:
                    out[k] = max(0, int(v))
                except (TypeError, ValueError):
                    raise HTTPException(422, f"'{key}.{k}' 는 정수여야 합니다")
                hi = _INT_LIMITS.get((key, k))
                if hi is not None and out[k] > hi:
                    raise HTTPException(422, f"'{key}.{k}' 는 {hi} 이하여야 합니다")
            elif isinstance(d, list):
                out[k] = _str_list(v)
            else:
                if key == "llm" and k in {"openai_api_key", "anthropic_api_key"}:
                    if not isinstance(v, str) or len(v) > 8192 or any(c in v for c in "\r\n\x00"):
                        raise HTTPException(422, "API 키 형식을 확인하세요.")
                    out[k] = v.strip()
                else:
                    out[k] = str(v if v is not None else "").strip()[:1000]
        prov = out.get("provider")
        if key in _PROVIDERS and prov is not None and prov not in _PROVIDERS[key]:
            raise HTTPException(422, "알 수 없는 AI 연결 방식입니다.")
        if key == "llm":
            from ..llm.compat_config import PRESETS, normalize_base_url
            if "openai_connection" in out and out["openai_connection"] not in PRESETS:
                raise HTTPException(422, "AI 연결 방식을 확인하세요.")
            if "openai_base_url" in out:
                try:
                    out["openai_base_url"] = normalize_base_url(out["openai_base_url"])
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
        return out
    if key == "watch_keywords":
        if not isinstance(value, list):
            value = [{"keyword": k} for k in _str_list(value)]
        out_l, seen = [], set()
        for w in value:
            kw = str((w.get("keyword") if isinstance(w, dict) else w) or "").strip()[:50]
            cat = (w.get("category") if isinstance(w, dict) else "") or "custom"
            if kw and kw not in seen:
                seen.add(kw)
                out_l.append({"keyword": kw, "category": cat if cat in _KEYWORD_CATEGORIES else "custom"})
        return out_l[:500]
    if key == "departments":
        if not isinstance(value, list):
            raise HTTPException(422, "'departments' 설정은 목록이어야 합니다")
        out_d = []
        for d in value:
            if not isinstance(d, dict) or not str(d.get("name") or "").strip():
                continue
            out_d.append({"name": str(d["name"]).strip()[:60], "keywords": _str_list(d.get("keywords") or [])})
        return out_d[:200]
    return value


@router.get("/settings")
async def get_settings(request: Request, user: User = Depends(require_operator)):
    return settings.public_view(settings.get_all(_rt(request).repo))


async def _role_decision_status(rt) -> dict[str, Any]:
    """입력 본문·인증값 없이 선택과 모델 준비/작업자 상태만 돌려준다."""
    from ..llm.julia import model_status

    cfg = settings.get(rt.repo, "transcript_review")
    model = await asyncio.to_thread(model_status, rt.config.data_dir)
    engine = getattr(getattr(rt, "reviews", None), "engine", None)
    get_provider = getattr(engine, "get_provider", None)
    provider = await get_provider() if callable(get_provider) else getattr(engine, "provider", None)
    provider_status = getattr(provider, "status", None)
    worker = provider_status() if callable(provider_status) else {
        "state": "idle", "available": bool(getattr(provider, "available", False)),
    }
    task = getattr(rt, "role_model_prepare_task", None)
    return {"selected_provider": cfg["role_decision_provider"], "enabled": cfg["enabled"],
            "auto_block": cfg["auto_block"], "model": model, "worker": worker,
            "preparing": bool(task and not task.done()),
            "prepare_error": getattr(rt, "role_model_prepare_error", ""),
            "cpu_threads": cfg["julia_threads"],
            "probability_notice": "Julia의 선택 점수는 보정된 정확도나 Jev 확신과 같지 않습니다."}


@router.get("/settings/role-decision/status")
async def role_decision_status(request: Request, user: User = Depends(require_operator)):
    return await _role_decision_status(_rt(request))


@router.post("/settings/role-decision/julia/prepare")
async def prepare_julia_model(request: Request, body: dict[str, Any] = Body(...),
                              user: User = Depends(require_operator)):
    """직원의 명시적 준비 버튼만 고정된 공식 모델 다운로드를 예약한다."""
    if set(body) != {"confirm"} or body.get("confirm") is not True:
        raise HTTPException(422, "Julia-1 모델 준비 버튼에서 다운로드를 시작하세요")
    rt = _rt(request)
    task = getattr(rt, "role_model_prepare_task", None)
    if task and not task.done():
        return await _role_decision_status(rt)
    from ..llm.julia import prepare_model

    rt.role_model_prepare_error = ""

    async def prepare() -> None:
        try:
            await prepare_model(rt.config.data_dir)
            rt.reload_settings()
            _pub(rt, "*", "role-model.updated", {"provider": "julia", "prepared": True})
        except asyncio.CancelledError:
            _pub(rt, "*", "role-model.updated", {"provider": "julia", "cancelled": True})
            raise
        except Exception as exc:
            # 다운로드 URL·사용자 경로·서버 본문을 화면이나 감사 기록으로 전파하지 않는다.
            from ..llm.decision import DecisionError
            rt.role_model_prepare_error = str(exc) if isinstance(exc, DecisionError) else (
                "Julia-1 모델 준비에 실패했습니다. 저장공간과 인터넷 연결을 확인하고 다시 준비하세요.")
            _pub(rt, "*", "role-model.updated", {"provider": "julia", "prepared": False})

    rt.role_model_prepare_task = asyncio.create_task(prepare(), name="prepare-julia-model")
    rt.repo.audit(user.actor, "role_model.prepare", "julia", {"explicit": True})
    return await _role_decision_status(rt)


@router.post("/settings/role-decision/julia/cancel")
async def cancel_julia_model(request: Request, user: User = Depends(require_operator)):
    rt = _rt(request)
    task = getattr(rt, "role_model_prepare_task", None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        rt.repo.audit(user.actor, "role_model.cancel", "julia", {})
    return await _role_decision_status(rt)


@router.post("/settings/llm/test")
async def test_llm(request: Request, body: dict[str, Any] | None = Body(None),
                   user: User = Depends(require_operator)):
    rt = _rt(request)
    try:
        cfg = settings.get(rt.repo, "llm")
    except ValueError:
        return {"ok": False, "error": "저장된 API 키를 읽지 못했습니다. 키를 다시 입력하세요."}
    from ..llm.compat_config import connection_url
    from ..llm.connection_test import test_connection
    if cfg.get("openai_api_key") and not cfg.get("openai_key_base_url"):
        cfg["openai_key_base_url"] = connection_url(cfg) or "unbound"
    secret_fields = {f for k, f in settings.SECRET_PATHS if k == "llm"}
    normalized = _normalize_setting("llm", body or {})
    if normalized.get("openai_connection") != cfg.get("openai_connection") and "openai_connection" in normalized and "openai_base_url" not in normalized:
        from ..llm.compat_config import PRESETS
        cfg["openai_base_url"] = PRESETS[normalized["openai_connection"]]
    new_key = normalized.get("openai_api_key")
    for k, v in normalized.items():
        if k in secret_fields and (not v or str(v).startswith("••")):
            continue  # 마스킹 값이면 저장된 키를 쓴다
        cfg[k] = v
    for field in secret_fields:
        if normalized.get("clear_" + field) is True:
            cfg[field] = ""
            if field == "openai_api_key":
                cfg["openai_key_base_url"] = ""
    if isinstance(new_key, str) and new_key and not new_key.startswith("••"):
        cfg["openai_key_base_url"] = connection_url(cfg) or "unbound"
    try:
        return await test_connection(cfg, rt)
    except Exception:
        return {"ok": False, "error": "AI 연결 상태와 설정을 확인하세요."}


async def _nemotron_status(rt) -> dict:
    from ..stt.nemotron_assets import status
    from ..stt.nemotron_diarization_assets import status as diarization_status
    from ..stt.nemotron_cpu import available_logical_cpus, effective_cpu_cores

    result = await asyncio.to_thread(status, rt.config.data_dir)
    diarization = await asyncio.to_thread(diarization_status, rt.config.data_dir)
    task = getattr(rt, "stt_model_prepare_task", None)
    preparing = bool(task and not task.done())
    ready = result["files_present"] and diarization["files_present"]
    message = result.get("message")
    if result["files_present"]:
        message = "전사·화자 분리 모델 파일 준비됨" if ready else "녹음 화자 분리 모델을 준비하세요"
    if diarization.get("state") == "failed":
        message = diarization.get("message") or "화자 분리 모델 준비를 완료하지 못했습니다"
    total = result.get("total_bytes", 0) + diarization.get("total_bytes", 0)
    complete = ((result.get("total_bytes", 0) if result["files_present"] else result.get("bytes", 0))
                + (diarization.get("total_bytes", 0) if diarization["files_present"] else diarization.get("bytes", 0)))
    if preparing and result["files_present"] and not diarization["files_present"]:
        message = "녹음 화자 분리 모델을 준비하고 있습니다"
    stt_settings = settings.get(rt.repo, "stt")
    return {**result, "files_present": ready, "asr_files_present": result["files_present"],
            "diarization": diarization, "message": message,
            "preparing": preparing, "progress": min(1, complete / total) if total else 0,
            "selected": stt_settings.get("provider") == "nemotron_cpu",
            "cpu_core_policy": "auto" if stt_settings.get("nemotron_cpu_auto") is True else "manual",
            "cpu_core_limit": effective_cpu_cores(stt_settings),
            "logical_cpu_capacity": available_logical_cpus()}


@router.get("/settings/stt/nemotron/status")
async def nemotron_model_status(request: Request, user: User = Depends(require_operator)):
    return await _nemotron_status(_rt(request))


@router.post("/settings/stt/nemotron/prepare")
async def prepare_nemotron_model(request: Request, body: dict[str, Any] = Body(...),
                                 user: User = Depends(require_operator)):
    if set(body) != {"confirm", "accept_license"} or body.get("confirm") is not True or body.get("accept_license") is not True:
        raise HTTPException(422, "음성 모델 라이선스를 확인하고 준비 버튼을 누르세요")
    rt = _rt(request)
    if rt.config.desktop_bridge is None or rt.config.disable_heavy_engines:
        raise HTTPException(409, "Windows 설치 앱에서 음성 모델을 준비하세요")
    ingest = getattr(rt, "ingest", None)
    if ingest is not None and ingest.audio_input_running():
        raise HTTPException(409, "음성 수신·전사를 마친 뒤 모델을 준비하세요")
    task = getattr(rt, "stt_model_prepare_task", None)
    if task and not task.done():
        return await _nemotron_status(rt)
    health = await rt.stt.health()
    # health 대기 중 들어온 다른 준비 요청이 이미 소유권을 얻었을 수 있다.
    task = getattr(rt, "stt_model_prepare_task", None)
    if task and not task.done():
        return await _nemotron_status(rt)
    if health.get("running") or health.get("worker_state") in {"loading", "running", "ready"}:
        raise HTTPException(409, "음성 전사를 종료한 뒤 모델을 준비하세요")
    from ..stt.nemotron_assets import prepare, reserve
    from ..stt.nemotron_diarization_assets import prepare as prepare_diarization, reserve as reserve_diarization
    from ..stt.nemotron_diarization_assets import status as diarization_status, mark_cancelled as clear_diarization_reservation

    async def run() -> None:
        try:
            await asyncio.wait_for(prepare(rt.config.data_dir), 1800)
            await asyncio.wait_for(prepare_diarization(rt.config.data_dir), 1800)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 상세 오류는 준비 모듈의 안전한 상태 안내에만 남긴다.
            pass
        finally:
            if diarization_status(rt.config.data_dir).get("state") == "preparing":
                clear_diarization_reservation(rt.config.data_dir)

    reserve(rt.config.data_dir)
    reserve_diarization(rt.config.data_dir)
    rt.stt_model_prepare_task = asyncio.create_task(run(), name="prepare-nemotron-cpu")
    rt.repo.audit(user.actor, "stt_model.prepare", "nemotron_cpu", {"explicit": True, "license": "OpenMDW-1.1"})
    return await _nemotron_status(rt)


@router.post("/settings/stt/nemotron/cancel")
async def cancel_nemotron_model(request: Request, user: User = Depends(require_operator)):
    rt = _rt(request)
    task = getattr(rt, "stt_model_prepare_task", None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        from ..stt.nemotron_assets import mark_cancelled
        from ..stt.nemotron_diarization_assets import mark_cancelled as mark_diarization_cancelled
        if getattr(rt, "stt_model_prepare_task", None) is task:
            mark_cancelled(rt.config.data_dir)
            mark_diarization_cancelled(rt.config.data_dir)
        rt.repo.audit(user.actor, "stt_model.cancel", "nemotron_cpu", {})
    return await _nemotron_status(rt)


@router.put("/settings/{key}")
async def put_setting(request: Request, key: str, value: Any = Body(...), user: User = Depends(require_operator)):
    rt = _rt(request)
    if key not in settings.DEFAULTS:
        raise HTTPException(404, f"알 수 없는 설정입니다: {key}")
    value = _normalize_setting(key, value)
    try:
        merged = settings.update(rt.repo, key, value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    rt.reload_settings()
    if key in {"llm", "org", "watch_keywords", "transcript_review", "stt"}:
        # 이미 열어 둔 열람 화면도 이전 설정으로 만든 자동 판본을 즉시 해제한다.
        for session in rt.repo.list_sessions(limit=1000):
            sid = session["id"]
            completed = set(rt.repo.presentation_block_ids(sid))
            for bid in completed:
                block = rt.repo.get_block(bid)
                if block:
                    _pub(rt, sid, "block.upsert", get_block_display(rt.repo, block))
                    if key != "stt" and settings.get(rt.repo, "transcript_review").get("enabled"):
                        rt.analysis.on_review_display_changed(sid, bid)
            # 창 초안에는 완성 presentation이 없으므로 열린 질의도 즉시 해제한다.
            for block in rt.repo.list_blocks(sid, kind="question"):
                if block.get("is_open") and block["id"] not in completed:
                    _pub(rt, sid, "block.upsert", get_block_display(rt.repo, block))
    if key == "audio_review" and getattr(rt, "audio_review", None) is not None:
        await rt.audio_review.close()
        # 과거 음성 설정을 수정해도 자막 수집의 음성 보정 연결을 다시 만들지 않는다.
    detail = {"fields": sorted(value)} if isinstance(value, dict) else {"count": len(value) if isinstance(value, list) else 1}
    rt.repo.audit(user.actor, "settings.update", key, detail)
    _pub(rt, "*", "settings.updated", {"key": key})
    return settings.public_view({key: merged})[key]


@router.get("/audit")
async def audit_log(request: Request, limit: int = Query(300, ge=1, le=2000), user: User = Depends(require_operator)):
    return {"items": _rt(request).repo.list_audit(limit)}
