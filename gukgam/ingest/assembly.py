"""국회 인터넷의사중계(assembly.webcast.go.kr) 연동: 일정·중계 정보 조회와 AI자막 WebSocket 수신.

자세한 프로토콜·주의사항은 docs/ASSEMBLY_FEED.md 참고.
- service/*.asp 는 JSON 을 돌려준다. 쿼리 끝에 `vv=<unix>&` 캐시버스터, 일반 브라우저 UA, 서버에서만 호출(CORS 없음).
- 요청 간 최소 1.5초(모든 클라이언트 공용), 타임아웃 10초, 결과는 60초 캐시(피감기관·명단은 1시간).
- xcgcd 는 추측하지 말고 calendar/live_list 에서 받는다. 자막·영상 주소는 (재)시작 때마다 live_play 로 새로 받는다.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import httpx

from ..config import KST
from ..db import now_ms
from ..models import SegmentEvent, Turn
from ..nlp.speaker import (INDEPENDENT_ANSWER, has_explicit_turn_cue, has_explicit_turn_start,
                           is_incomplete_fragment, is_chair_text)
from ..nlp.role_evidence import role_evidence
from ..nlp.caption_fragments import find_caption_fragment_joins
from .assembly_http import create_http_client
from .base import Emit, GapMarker, Source, SourceContext, SourceError, hm, sleep_or_stop

logger = logging.getLogger(__name__)

ORIGIN = "https://assembly.webcast.go.kr"
BASE_URL = ORIGIN + "/main/service"
REFERER = ORIGIN + "/main/player.asp"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
# 속기 자막(socket.io) 서버 — 원본 플레이어와 같은 판별식. 이 경우 AI자막 WS 가 아니다.
STENO_RE = re.compile(r"smi(-hy|-dw)?(\d)?\.webcast\.go\.kr", re.I)

MIN_INTERVAL = 1.5
TIMEOUT = 10.0
CACHE_TTL = 60.0
LONG_TTL = 3600.0


class AssemblyError(RuntimeError):
    """의사중계 서버 호출 실패(사용자에게 보일 한국어 메시지)."""


# ── 공용 요청 간격·캐시(모든 AssemblyClient 가 공유) ─────────────────
class _Throttle:
    """요청 시작 시각을 interval 간격으로 예약한다(이벤트 루프와 무관하게 동작)."""

    def __init__(self, interval: float):
        self.interval = interval
        self._next = 0.0

    async def wait(self, interval: float | None = None) -> None:
        iv = self.interval if interval is None else interval
        if iv <= 0:
            return
        now = time.monotonic()
        slot = max(now, self._next)
        self._next = slot + iv
        if slot > now:
            await asyncio.sleep(slot - now)


_THROTTLE = _Throttle(MIN_INTERVAL)
_CACHE: dict[str, tuple[float, Any]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _url(path: str, params: dict[str, Any]) -> tuple[str, str]:
    """(요청 URL, 캐시 키). 원본 플레이어처럼 `...&vv=<unix>&` 로 끝난다."""
    qs = "".join(f"{k}={quote(str(v))}&" for k, v in params.items())
    key = f"{path}?{qs}"
    return f"{BASE_URL}/{path}?{qs}vv={int(time.time())}&", key


class AssemblyClient:
    """service/*.asp 비동기 클라이언트. client 를 주면 그것을 쓰고(닫지 않음), 없으면 직접 만든다."""

    def __init__(self, client: httpx.AsyncClient | None = None, *, min_interval: float = MIN_INTERVAL,
                 timeout: float = TIMEOUT, cache_ttl: float = CACHE_TTL):
        self._own = client is None
        self.client = client or create_http_client(timeout=timeout)
        self.min_interval = min_interval
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.requests = 0          # 실제 네트워크 요청 수(테스트·진단용)

    async def aclose(self) -> None:
        if self._own:
            await self.client.aclose()

    async def __aenter__(self) -> "AssemblyClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def is_cached(self, path: str, params: dict[str, Any], ttl: float | None = None) -> bool:
        _, key = _url(path, params)
        hit = _CACHE.get(key)
        ttl = self.cache_ttl if ttl is None else ttl
        return bool(hit and ttl > 0 and time.monotonic() - hit[0] < ttl)

    async def get_json(self, path: str, params: dict[str, Any] | None = None, *, ttl: float | None = None,
                       fresh: bool = False) -> Any:
        params = params or {}
        url, key = _url(path, params)
        ttl = self.cache_ttl if ttl is None else ttl
        if not fresh and ttl > 0:
            hit = _CACHE.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        await _THROTTLE.wait(self.min_interval)
        self.requests += 1
        try:
            r = await self.client.get(url, headers=HEADERS, timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise AssemblyError(f"국회 의사중계 응답 시간 초과({self.timeout:g}초): {path}") from e
        except httpx.HTTPError as e:
            raise AssemblyError(f"국회 의사중계 연결 실패: {type(e).__name__} {e}") from e
        if r.status_code != 200:
            raise AssemblyError(f"국회 의사중계 응답 오류(HTTP {r.status_code}): {path}")
        try:
            data = json.loads(r.content.decode("utf-8", "replace").lstrip("﻿"))
        except ValueError as e:
            raise AssemblyError(f"국회 의사중계 응답 형식 오류: {path}") from e
        if ttl > 0:
            _CACHE[key] = (time.monotonic(), data)
        return data

    # ── 엔드포인트 ────────────────────────────────────────────
    async def live_list(self) -> list[dict]:
        """오늘 위원회별 중계 현황(service/live_list.asp). xstat '1' = 생중계 중."""
        d = await self.get_json("live_list.asp")
        items = d.get("xlist") if isinstance(d, dict) else None
        out = []
        for it in items or []:
            if isinstance(it, dict):
                it = {k: (v.strip() if isinstance(v, str) else v) for k, v in it.items()}
                it["is_live"] = it.get("xstat") == "1"
                out.append(it)
        return out

    async def live_play(self, xcode: str, xcgcd: str, *, fresh: bool = False) -> dict:
        """회의 중계 정보(service/live_play.asp): xsami(자막 WS), xhls(영상), xsubj, xstat, xdesc ..."""
        d = await self.get_json("live_play.asp", {"xcode": xcode, "xcgcd": xcgcd}, fresh=fresh)
        if not isinstance(d, dict) or d.get("xcode") == "invalid-request":
            _CACHE.pop(_url("live_play.asp", {"xcode": xcode, "xcgcd": xcgcd})[1], None)
            raise AssemblyError(f"유효하지 않은 회의 코드입니다(xcode={xcode}, xcgcd={xcgcd})")
        return d

    async def calendar(self, yyyymm: str) -> dict:
        """월간 일정(service/calendar.asp?qdate=YYYYMM)."""
        d = await self.get_json("calendar.asp", {"qdate": yyyymm})
        return d if isinstance(d, dict) else {}

    async def calendar_meetings(self, yyyymm: str) -> list[dict]:
        cal = await self.calendar(yyyymm)
        out = []
        for day in cal.get("xdays") or []:
            for m in day.get("xlist") or []:
                if isinstance(m, dict):
                    m = dict(m)
                    m.setdefault("xdate", day.get("xdate", ""))
                    out.append(m)
        return out

    async def live_angun(self, xcode: str, xcgcd: str) -> list[str]:
        """피감기관(국감) 또는 심사안건 목록(service/live_angun.asp) — billname 의 줄바꿈 목록을 편다."""
        d = await self.get_json("live_angun.asp", {"xcode": xcode, "xcgcd": xcgcd}, ttl=LONG_TTL)
        names: list[str] = []
        if isinstance(d, dict):
            for blk in d.get("angun") or []:
                for it in (blk or {}).get("xlist") or []:
                    for s in str((it or {}).get("billname") or "").splitlines():
                        s = html.unescape(s).strip()
                        # 공식 종합감사 목록의 구분 표제는 피감기관이 아니다.
                        if s == "[종합감사]":
                            continue
                        if s and s not in names:
                            names.append(s)
        return names

    async def committee_roster(self, xcode: str) -> list[dict]:
        """위원 명단(service/live_map.asp 의석배치도): center=위원장, left/right=위원."""
        d = await self.get_json("live_map.asp", {"xcode": xcode}, ttl=LONG_TTL)
        return parse_live_map(d)

    async def find_org_meetings(self, org_names: list[str], yyyymm: str | None = None, *,
                                since: str | None = None, max_lookups: int = 40,
                                audit_only: bool = True) -> list[dict]:
        """우리 기관이 피감기관인 회의 찾기.

        calendar(월) → 회의명 괄호 속 대표 기관으로 1차 매칭 → live_angun(전체 피감기관)으로 2차 매칭.
        live_angun 조회는 오늘에 가까운 회의부터 최대 max_lookups 회(캐시 적중은 세지 않음).
        since='YYYYMMDD' 면 그날 이후만. 결과: [{date,time,xdate,xcode,xcgcd,xname,xsubj,xdesc,agencies,matched}]
        """
        names = [n for n in (o.strip() for o in org_names or []) if n]
        if not names:
            return []
        today = datetime.now(KST).date()
        yyyymm = yyyymm or today.strftime("%Y%m")
        meetings = await self.calendar_meetings(yyyymm)
        if since:
            meetings = [m for m in meetings if str(m.get("xdate", "")) >= since]
        if audit_only:
            meetings = [m for m in meetings if "국정감사" in str(m.get("xsubj", ""))]

        def proximity(m: dict) -> tuple[int, int]:
            d = _parse_ymd(m.get("xdate", ""))
            if d is None:
                return (2, 0)
            delta = (d - today).days
            return (0 if delta >= 0 else 1, abs(delta))

        lookups = 0
        results: list[dict] = []
        for m in sorted(meetings, key=proximity):
            xcode, xcgcd = str(m.get("xcode", "")), str(m.get("xcgcd", ""))
            agencies = subject_agencies(str(m.get("xsubj", "")))
            if xcode and xcgcd:
                cached = self.is_cached("live_angun.asp", {"xcode": xcode, "xcgcd": xcgcd}, LONG_TTL)
                if cached or lookups < max_lookups:
                    try:
                        full = await self.live_angun(xcode, xcgcd)
                        if full:
                            agencies = full
                    except AssemblyError as e:
                        logger.warning("피감기관 조회 실패(%s %s): %s", xcode, xcgcd, e)
                    if not cached:
                        lookups += 1
            matched = match_orgs(names, agencies)
            if matched:
                xdate = str(m.get("xdate", ""))
                d = _parse_ymd(xdate)
                results.append({
                    "date": d.isoformat() if d else xdate,
                    "time": meeting_time(str(m.get("xsubj", "")), str(m.get("xdesc", ""))),
                    "xdate": xdate, "xcode": xcode, "xcgcd": xcgcd, "xname": m.get("xname", ""),
                    "xsubj": m.get("xsubj", ""), "xdesc": m.get("xdesc", ""),
                    "agencies": agencies, "matched": matched,
                })
        if lookups >= max_lookups:
            logger.info("피감기관 조회 상한(%d회) 도달 — 나머지는 회의명으로만 매칭", max_lookups)
        results.sort(key=lambda r: (r["xdate"], r["time"]))
        return results


# ── 모듈 수준 함수(웹 모듈이 지연 import 로 사용) ───────────────────
async def live_list(client: httpx.AsyncClient) -> list[dict]:
    return await AssemblyClient(client).live_list()


async def live_play(client: httpx.AsyncClient, xcode: str, xcgcd: str) -> dict:
    return await AssemblyClient(client).live_play(xcode, xcgcd)


async def committee_roster(client: httpx.AsyncClient, xcode: str) -> list[dict]:
    return await AssemblyClient(client).committee_roster(xcode)


async def calendar(client: httpx.AsyncClient, yyyymm: str) -> dict:
    return await AssemblyClient(client).calendar(yyyymm)


async def live_angun(client: httpx.AsyncClient, xcode: str, xcgcd: str) -> list[str]:
    return await AssemblyClient(client).live_angun(xcode, xcgcd)


async def find_org_meetings(client: httpx.AsyncClient, org_names: list[str], yyyymm: str | None = None,
                            **kw: Any) -> list[dict]:
    return await AssemblyClient(client).find_org_meetings(org_names, yyyymm, **kw)


# ── 파싱 도우미 ───────────────────────────────────────────────
def _parse_ymd(s: str) -> date | None:
    try:
        return datetime.strptime(str(s), "%Y%m%d").date()
    except ValueError:
        return None


def meeting_time(xsubj: str, xdesc: str = "") -> str:
    """'... [10:00]' 또는 xdesc '10:00' 에서 시각. 없으면 xdesc 그대로(예: '본회의 산회 직후')."""
    m = re.search(r"\[(\d{1,2}:\d{2})\]", xsubj) or re.search(r"(\d{1,2}:\d{2})", xdesc)
    return m.group(1) if m else xdesc.strip()


def subject_agencies(xsubj: str) -> list[str]:
    """'국토교통위원회 국정감사(한국도로공사 등) [10:00]' → ['한국도로공사']."""
    m = re.search(r"국정감사\s*\((.+?)\)\s*(?:\[|$)", xsubj)
    if not m:
        return []
    out = []
    for part in re.split(r"[,·ㆍ]", m.group(1)):
        part = re.sub(r"\s*등\s*$", "", part).strip()
        if part:
            out.append(part)
    return out


def _norm_org(s: str) -> str:
    return re.sub(r"\(주\)|㈜|주식회사|\s+", "", s or "")


def match_orgs(org_names: list[str], agencies: list[str]) -> list[str]:
    """기관명 일치(공백·(주) 무시). '한국도로공사' 가 '한국도로공사서비스(주)' 에 붙는 오탐을 막으려고
    부분 문자열이 아니라 같은 이름 또는 '이름(괄호 설명)' 만 인정한다."""
    out: list[str] = []
    norms = [n for n in (_norm_org(o) for o in org_names) if n]
    for a in agencies:
        na = _norm_org(a)
        if any(na == n or na.startswith(n + "(") for n in norms) and a not in out:
            out.append(a)
    return out


def parse_live_map(d: Any) -> list[dict]:
    """live_map.asp 응답 → [{name, role: chair|member, party}] (위원장 먼저, 이름 중복 제거)."""
    if not isinstance(d, dict) or str(d.get("result", "")).lower() != "ok":
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def add(m: Any, role: str) -> None:
        if not isinstance(m, dict):
            return
        name = re.sub(r"\s+", "", html.unescape(str(m.get("mname") or "")))
        name = re.sub(r"(위원장|위원|의원)$", "", name)
        if not name or name in seen:
            return
        seen.add(name)
        out.append({"name": name, "role": role, "party": html.unescape(str(m.get("mdang") or "")).strip()})

    for m in d.get("center") or []:
        add(m, "chair")
    for side in ("left", "right"):
        for m in d.get(side) or []:
            add(m, "member")
    return out


def hls_urls(info: dict) -> dict:
    """live_play 의 xhls[0] ({'default','240p',...}). 없으면 {}."""
    xhls = info.get("xhls")
    if isinstance(xhls, list) and xhls and isinstance(xhls[0], dict):
        return xhls[0]
    return {}


def caption_ws_url(xsami: str, variant: str = "hls") -> str:
    """AI자막 WS 주소. 원본 플레이어는 HLS 재생 시 xsami + '/hls' 에 붙는다(variant='hls')."""
    url = xsami.strip().rstrip("/")
    if variant == "hls" and not url.endswith("/hls"):
        url += "/hls"
    return url


# ── 자막 메시지 → SegmentEvent ────────────────────────────────────
RESET_GAP_MS = 300_000     # 미확정 segment 가 이만큼 쉬었다 같은 번호로 다시 오면 새 발언(새 key)으로 본다
FINAL_HOLD_MS = 2_000      # final 표시를 받은 뒤 이만큼 더 갱신이 없으면 확정
SUPERSEDE_HOLD_MS = 3_000  # 다른 segment 가 진행 중일 때 이만큼 갱신이 없으면 확정('다음 segment 시작 = 이전 확정')
IDLE_FINAL_MS = 20_000     # 아무 표시가 없어도 이만큼 갱신이 없으면 확정
SPEAKER_WAIT_MS = 20_000   # 화자 판정이 없는 본문 final은 별도 유한 대기 후 마감
STABILITY_MAX_MS = 40_000  # 계속 바뀌는 선두 원조각이 후속 분석을 무한정 막지 않는다
REORDER_WINDOW_MS = 30_000  # 앞 번호가 늦게 처음 오면, 이 시간 안에 시작한 뒤 번호보다 앞에 놓는다
REORDER_MAX_GAP = 3         # 번호 차이가 이보다 크면(번호 재시작 등) 순서를 고치지 않는다
MIN_EXTEND_CHARS = 6        # 오래전에 확정한 발언을 '연장'으로 이어 받을 최소 길이(짧으면 새 발언으로 본다)

_NORM_RE = re.compile(r"[\s.,?!·…~\"'“”‘’\-]+")
_KEY_RE = re.compile(r"^s(.+?)(?:\.(\d+))?$")


def norm_caption(text: str) -> str:
    """내용 비교용 정규화(공백·문장부호 제거)."""
    return _NORM_RE.sub("", text or "")


def _seg_num(seg: str | None) -> int | None:
    try:
        return int(str(seg))
    except (TypeError, ValueError):
        return None


def _turn_sig(turns: list[Turn]) -> tuple:
    return tuple((t.speaker_hint, t.new_speaker, norm_caption(t.text)) for t in turns)


@dataclass
class CaptionState:
    """parse_caption_message 가 쓰는 수신 상태.

    - 진행 중 segment는 pending에 둔다. scd 지원 메시지는 화자 판정까지 기다리며,
      판정 없이 20초간 내용 변화가 없으면 제한 마감한다. 화자 판정 뒤 final은 2초 대기한다.
      scd 없는 호환 입력은 final 2초·다른 segment 진행 3초·무신호 20초를 유지한다.
      같은 판본의 재전송은 대기를 연장하지 않는다.
    - 확정한 key 는 정규화 본문을 기억해, 같은 번호가 다시 오면 내용으로 판정한다:
      같거나 앞부분이면 재전송(무시), 이어지는 내용이면 같은 발언의 늦은 갱신(다시 열기), 다르면 번호 재사용(새 key).
    """

    prior_keys: dict[str, int] = field(default_factory=dict)    # 이전 실행에서 쓴 key → 마지막 갱신(ms)
    prior_texts: dict[str, str] = field(default_factory=dict)   # 이전 실행에서 쓴 key → 본문
    reset_gap_ms: int = RESET_GAP_MS
    final_hold_ms: int = FINAL_HOLD_MS
    supersede_hold_ms: int = SUPERSEDE_HOLD_MS
    idle_final_ms: int = IDLE_FINAL_MS
    speaker_wait_ms: int = SPEAKER_WAIT_MS
    seg_keys: dict[str, str] = field(default_factory=dict)      # segment 번호 → 진행 중 key
    key_seg: dict[str, str] = field(default_factory=dict)       # key → segment 번호
    first_rx: dict[str, int] = field(default_factory=dict)
    last_rx: dict[str, int] = field(default_factory=dict)
    stable_rx: dict[str, int] = field(default_factory=dict)    # 본문·분할·확정 단계가 실제 바뀐 시각
    opened_rx: dict[str, int] = field(default_factory=dict)    # 이번 열린 판본의 대기 시작(재개 때 갱신)
    pending: dict[str, SegmentEvent] = field(default_factory=dict)
    done: dict[str, str] = field(default_factory=dict)          # 확정한 key → 정규화 본문
    done_signatures: dict[str, tuple] = field(default_factory=dict)  # 확정 뒤에도 늦은 화자 판정을 구별
    done_at: dict[str, int] = field(default_factory=dict)       # 확정한 key → 마지막 갱신(ms)
    done_by_seg: dict[str, list[str]] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)
    last_key: str | None = None
    last_any_key: str | None = None
    duplicates: int = 0        # 재전송으로 보고 버린 메시지 수
    reopened: int = 0          # 확정 뒤 늦게 온 갱신으로 다시 연 횟수

    def __post_init__(self) -> None:
        for key, t in self.prior_keys.items():
            m = _KEY_RE.match(key)
            if not m:
                continue
            seg = m.group(1)
            self.used.add(key)
            self.key_seg[key] = seg
            self.done[key] = norm_caption(self.prior_texts.get(key, ""))
            self.done_at[key] = int(t)
            self.done_by_seg.setdefault(seg, []).append(key)

    # ── key 판정 ──────────────────────────────────────────────
    def resolve(self, seg: str, norm: str, now: int, signature: tuple | None = None) -> tuple[str | None, str]:
        """(key, 판정). 판정: pending(진행 중 갱신)|new(새 발언)|reopen(확정 뒤 늦은 갱신)|dup(재전송 → key None)."""
        key = self.seg_keys.get(seg)
        if key is not None and key in self.pending and now - self.last_rx.get(key, now) <= self.reset_gap_ms:
            return key, "pending"
        for k in reversed(self.done_by_seg.get(seg, [])):
            ft = self.done.get(k, "")
            recent = now - self.done_at.get(k, 0) <= self.reset_gap_ms
            if ft and norm and (norm == ft or ft.startswith(norm)):
                old = self.done_signatures.get(k)
                if (norm == ft and recent and old is not None and signature is not None
                        and old != signature and not (old[0] and not signature[0])):
                    return k, "reopen"   # 같은 본문이라도 scd·라벨·조각·전환 표지가 바뀌었다
                return None, "dup"
            if norm.startswith(ft) and (recent or len(ft) >= MIN_EXTEND_CHARS):
                return k, "reopen"
        base = f"s{seg}"
        key = base
        n = 1
        while key in self.used:
            key = f"{base}.{n}"
            n += 1
        self.used.add(key)
        self.key_seg[key] = seg
        return key, "new"

    def key_for(self, seg: str, now: int, norm: str = "") -> str:
        """(호환) 내용 없이 key 만 정한다."""
        key, mode = self.resolve(seg, norm, now)
        if key is None:          # 빈 내용으로는 재전송 판정이 나지 않지만 방어
            key = self.done_by_seg[seg][-1]
        if mode == "reopen":
            self._reopen(key, seg)
        self.seg_keys[seg] = key
        return key

    def _reopen(self, key: str, seg: str) -> None:
        self.done.pop(key, None)
        self.done_signatures.pop(key, None)
        self.done_at.pop(key, None)
        lst = self.done_by_seg.get(seg) or []
        if key in lst:
            lst.remove(key)
        self.reopened += 1

    def _order_start(self, key: str, seg: str, now: int) -> int:
        """늦게 처음 온 앞 번호 segment 는, 이미 진행 중인 바로 뒤 번호들보다 앞 시각으로 놓는다."""
        num = _seg_num(seg)
        start = now
        if num is None:
            return start
        for k in self.pending:
            n2 = _seg_num(self.key_seg.get(k))
            t2 = self.first_rx.get(k)
            if n2 is None or t2 is None or k == key:
                continue
            if 0 < n2 - num <= REORDER_MAX_GAP and now - t2 <= REORDER_WINDOW_MS and t2 <= start:
                start = t2 - 1
        return start

    # ── 확정 ──────────────────────────────────────────────────
    def _hold(self, key: str) -> int:
        ev = self.pending[key]
        if "scd" in ev.raw and not _truthy(ev.raw["scd"]):
            return self.speaker_wait_ms
        if ev.is_final:
            return self.final_hold_ms
        last = self.last_rx.get(key, 0)
        other_active = (self.last_any_key not in (None, key)
                        and self.last_rx.get(self.last_any_key, 0) >= last)  # type: ignore[arg-type]
        return self.supersede_hold_ms if other_active else self.idle_final_ms

    def next_due(self, now: int) -> int | None:
        """가장 이른 확정 예정까지 남은 ms(없으면 None)."""
        best: int | None = None
        for k in self.pending:
            left = self._due_at(k) - now
            best = left if best is None else min(best, left)
        return None if best is None else max(0, best)

    def _due_at(self, key: str) -> int:
        due = self.stable_rx.get(key, self.last_rx.get(key, 0)) + self._hold(key)
        if "scd" in self.pending[key].raw:
            due = min(due, self.opened_rx.get(key, self.first_rx.get(key, 0)) + STABILITY_MAX_MS)
        return due

    def _finalize(self, key: str, by: str) -> SegmentEvent:
        ev = self.pending.pop(key)
        self.opened_rx.pop(key, None)
        seg = self.key_seg.get(key)
        if seg is not None:
            if self.seg_keys.get(seg) == key:
                del self.seg_keys[seg]
            lst = self.done_by_seg.setdefault(seg, [])
            if key not in lst:
                lst.append(key)
        last = self.last_rx.get(key, ev.ts_end or ev.ts_start)
        self.done[key] = norm_caption(ev.text)
        self.done_signatures[key] = (_truthy(ev.raw.get("scd")), _turn_sig(ev.turns))
        self.done_at[key] = last
        return SegmentEvent(key=ev.key, ts_start=ev.ts_start, ts_end=last, turns=ev.turns, is_final=True,
                            raw={**ev.raw, "_finalized_by": by})

    def take_due(self, now: int) -> list[SegmentEvent]:
        """보류 시간이 지난 미확정 segment 를 확정 이벤트로 꺼낸다(첫 수신 순)."""
        out = []
        for k in sorted(self.pending, key=lambda k: (self.first_rx.get(k, 0), k)):
            hold = self._hold(k)
            if now >= self._due_at(k):
                unstable = now - self.stable_rx.get(k, self.last_rx.get(k, now)) < hold
                by = ("stability_limit" if unstable else
                      "speaker_timeout" if "scd" in self.pending[k].raw
                      and not _truthy(self.pending[k].raw["scd"]) else
                      "final" if self.pending[k].is_final else
                      "next_segment" if hold == self.supersede_hold_ms else "idle")
                out.append(self._finalize(k, by))
        return out

    def take_superseded(self, current_key: str | None) -> list[SegmentEvent]:
        """현재 key 가 아닌 미확정 segment 를 보류 없이 바로 확정한다."""
        keys = sorted((k for k in self.pending if k != current_key), key=lambda k: (self.first_rx.get(k, 0), k))
        return [self._finalize(k, "next_segment") for k in keys]

    def take_all_pending(self) -> list[SegmentEvent]:
        """수집 종료 시 남은 미확정 segment 를 모두 확정한다."""
        keys = sorted(self.pending, key=lambda k: (self.first_rx.get(k, 0), k))
        return [self._finalize(k, "stop") for k in keys]


# ── 발언(화자 턴) 묶기 ──────────────────────────────────────────
# 원자막은 부분 → 본문 final → scd 화자 판정으로 갱신된다. 크기·시간은 고정되지 않는다.
# 색과 표지는 경계 후보이며 인물·문답 역할의 정답이 아니다. 원조각과 경계가 안정된 뒤에만
# 분석용 발언을 닫고, 임시 구간은 재계산한다. 서로 다른 직접 역할 근거와 짧은 문법 연결도 함께 본다.
TURN_GAP_MS = 6000         # 앞 segment 와 이만큼 떨어지면 새 발언
TURN_MAX_CHARS = 420       # 같은 화자라도 이 길이를 넘으면 이어지는 새 발언(실시간 분석이 늦지 않게)
TURN_MAX_MS = 40_000       # 발언 하나의 최대 길이(파이프라인 강제 확정 45초보다 짧게)
TURN_IDLE_MS = 8000        # 마지막 segment 가 확정된 뒤 이만큼 새 segment 가 없으면 발언 확정
TURN_STALE_MS = 20_000     # 미확정 segment 가 남았어도 이만큼 갱신이 없으면 발언 확정
TURN_KEEP_CLOSED = 6       # 늦은 갱신을 반영하려고 기억해 두는 닫힌 발언 수
TURN_REVIEW_SIGNATURES = 128  # 최근 재분할 후보의 재전송·취소를 구별하는 서명 수
TURN_MIN_CHARS = 6         # 이보다 짧은 발언('이렇게', '와서')은 화자 라벨 튐으로 보고 다음 발언 앞에 붙인다
_SHORT_RESPONSE = INDEPENDENT_ANSWER


@dataclass
class _TSeg:
    key: str                # '{segment key}~{조각 번호}'
    base: str               # segment key(CaptionState 가 정한 's1944' 등)
    text: str
    final: bool
    hwa: str | None
    marker: bool            # 본문 앞 '-'(화자 전환 표시)
    ts_start: int
    ts_end: int
    scd: bool = False
    uncertain: bool = False
    joined: bool = False     # 같은 원조각 안 강한 문법 연결. 원문 조각과 라벨은 따로 보존한다.


@dataclass
class _TTurn:
    key: str
    segs: list[_TSeg]
    hwa: str | None = None
    new_speaker: bool = False
    turn_id: str = ""
    boundary_reason: str = "start"

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segs if s.text).strip()

    @property
    def ts_start(self) -> int:
        return min(s.ts_start for s in self.segs)

    @property
    def ts_end(self) -> int:
        return max(s.ts_end for s in self.segs)


def _decided_hwa(hint: str | None) -> str | None:
    if not hint or not hint.startswith("hwa:"):
        return None
    v = hint[4:].strip()
    return None if v in ("", "-1", "None", "null") else v


class TurnAssembler:
    """segment 단위 SegmentEvent → 화자 턴(발언) 단위 SegmentEvent(key 't'+첫 조각 key).

    국회 서버는 한 segment 를 부분 결과로 보내다가, 화자 판정 메시지에서 그 segment 를 여러 화자 구간
    ([[0,'- …'],[1,'- …'],…])으로 다시 쪼개 보낸다. 그래서 같은 segment 가 다시 오면 그 segment 의 조각을
    통째로 바꾸고, 아직 닫지 않은 구간(window) 전체의 발언 경계를 다시 계산한다.
    경계: 화자 라벨(hwa 0/1)이 바뀜 · 본문 앞 '-' · segment 사이 간격 · 길이/시간 한도.
    원조각과 다음 경계가 확정된 앞부분만 닫는다. 아직 바뀔 수 있는 여러 발언은 함께 열어 둔다.
    """

    def __init__(self) -> None:
        self.window: list[_TSeg] = []
        self.closed: list[_TTurn] = []
        self.prev_hwa: str | None = None
        self.open_key: str | None = None
        self.open_keys: set[str] = set()
        self.updated = 0
        self._review_signatures: dict[str, tuple] = {}

    # ── 내보내기 ────────────────────────────────────────────
    @staticmethod
    def _event(t: _TTurn, final: bool) -> SegmentEvent:
        hint = f"hwa:{t.hwa}" if t.hwa is not None else None
        first = t.segs[0]
        m = _KEY_RE.fullmatch(first.base)
        number = _seg_num(m.group(1)) if m else None
        raw = {"turn": True, "hwa": t.hwa, "segments": {s.key: s.text for s in t.segs},
               "turn_id": t.turn_id or t.key, "boundary_reason": t.boundary_reason,
               "scd": all(s.scd for s in t.segs)}
        if number is not None:
            raw["source_order"] = [number, int(m.group(2) or 0), int(first.key.rsplit("~", 1)[1])]
        return SegmentEvent(key=t.key, ts_start=t.ts_start, ts_end=t.ts_end,
                            turns=[Turn(text=t.text, speaker_hint=hint, new_speaker=t.new_speaker)],
                            is_final=final, raw=raw)

    @staticmethod
    def _retract(key: str, ts: int) -> SegmentEvent:
        """더 이상 쓰지 않는 미확정 발언 key 를 지운다(파이프라인은 턴 0개를 '전부 지움'으로 처리)."""
        return SegmentEvent(key=key, ts_start=ts, ts_end=ts, turns=[], is_final=False, raw={"turn": True, "retract": True})

    # ── 경계 계산 ───────────────────────────────────────────
    @staticmethod
    def _boundary(segs: list[_TSeg], hwa: str | None, s: _TSeg) -> str | None:
        if not segs:
            return None
        if s.joined and s.base == segs[-1].base:
            return None
        if s.base != segs[-1].base:
            left = " ".join(item.text for item in segs)
            before, after = role_evidence(left), role_evidence(s.text)
            # 같은 기계 색이어도 직접 역할이 바뀌면 한 화자로 합치지 않는다.
            if (is_chair_text(left) and after["role"] == "member" and not is_chair_text(s.text)
                    or before["role"] and after["role"] and before["role"] != after["role"]):
                return "semantic"
            if s.uncertain or segs[-1].uncertain:
                return "uncertain_marker"
        if s.marker:
            return "uncertain_marker" if s.uncertain else "marker"
        if s.hwa is not None and hwa is not None and s.hwa != hwa:
            return "uncertain_marker" if s.uncertain else "label"
        if s.ts_start - max(x.ts_end for x in segs) > TURN_GAP_MS:
            return "gap"
        if sum(len(x.text) + 1 for x in segs) >= TURN_MAX_CHARS:
            return "limit_chars"
        if s.ts_start - min(x.ts_start for x in segs) >= TURN_MAX_MS:
            return "limit_time"
        return None

    @staticmethod
    def _mark_uncertain(segs: list[_TSeg]) -> list[_TSeg]:
        """같은 segment의 불완전 구절 반복 분할만 보류하며 원문 경계·조각은 유지한다."""
        switches = sum(a.hwa in ("0", "1") and b.hwa in ("0", "1") and a.hwa != b.hwa
                       for a, b in zip(segs, segs[1:]))
        if switches < 3:
            return segs
        incomplete = sum(not has_explicit_turn_cue(s.text) and is_incomplete_fragment(s.text)
                         for s in segs[:-1])
        if incomplete < 2:
            return segs
        return [replace(s, uncertain=not has_explicit_turn_cue(s.text)) for s in segs]

    def _groups(self) -> list[_TTurn]:
        groups: list[_TTurn] = []
        cur: list[_TSeg] = []
        cur_hwa: str | None = None
        reason = "uncertain_marker" if self.window and self.window[0].uncertain else "start"
        if self.window and self.closed:
            prev = self.closed[-1]
            reason = self._boundary(prev.segs, prev.hwa, self.window[0]) or "start"
        for s in self.window:
            if cur:
                boundary = self._boundary(cur, cur_hwa, s)
                if boundary:
                    groups.append(_TTurn(key=f"t{cur[0].key}", segs=cur, hwa=cur_hwa, boundary_reason=reason))
                    cur, cur_hwa = [], None
                    reason = boundary
            cur.append(s)
            if cur_hwa is None and s.hwa is not None:
                cur_hwa = s.hwa
        if cur:
            groups.append(_TTurn(key=f"t{cur[0].key}", segs=cur, hwa=cur_hwa, boundary_reason=reason))
        # 화자 라벨이 잠깐 튀어 생긴 아주 짧은 발언은 다음 발언 앞에 붙인다(마지막 열린 발언은 아직 모르므로 제외)
        merged: list[_TTurn] = []
        carry: list[_TSeg] = []
        for gi, g in enumerate(groups):
            if carry and has_explicit_turn_start(g.text):
                # 독립 응답·호명·자기소개 앞의 미완성 구절을 그 화자에게 흡수하지 않는다.
                merged.append(_TTurn(key=f"t{carry[0].key}", segs=carry,
                                     hwa=next((s.hwa for s in reversed(carry) if s.hwa is not None), None),
                                     boundary_reason="uncertain_marker" if any(s.uncertain for s in carry) else "marker"))
                carry = []
            segs = carry + g.segs
            short_text = " ".join(x.text for x in segs).strip()
            if gi < len(groups) - 1 and len(short_text) < TURN_MIN_CHARS and not _SHORT_RESPONSE.fullmatch(short_text):
                carry = segs
                continue
            hw = g.hwa if g.hwa is not None else next((x.hwa for x in segs if x.hwa is not None), None)
            if carry:
                # 붙인 짧은 조각의 화자 전환 표시는 버리고, 원래 발언의 표시를 앞에 둔다
                segs = [replace(x, marker=(g.segs[0].marker if i == 0 else False)) if i < len(carry) else x
                        for i, x in enumerate(segs)]
            merged.append(_TTurn(key=f"t{segs[0].key}", segs=segs, hwa=hw,
                                 boundary_reason="uncertain_marker" if any(s.uncertain for s in segs) else g.boundary_reason))
            carry = []
        groups = merged
        # 화자 전환 여부: 앞 발언(닫힌 것 포함)과 라벨이 다르거나 '-' 표시로 시작
        prev = self.prev_hwa
        previous_turn = self.closed[-1] if self.closed else None
        for g in groups:
            g.new_speaker = (g.boundary_reason == "semantic" or bool(g.segs[0].marker)
                             or (g.hwa is not None and prev is not None and g.hwa != prev))
            g.turn_id = (previous_turn.turn_id or previous_turn.key
                         if previous_turn is not None and g.boundary_reason in ("limit_chars", "limit_time")
                         else g.key)
            previous_turn = g
            if g.hwa is not None:
                prev = g.hwa
        return groups

    def _regroup(self, out: list[SegmentEvent], close_all: bool = False) -> None:
        groups = self._groups()
        ready = len(groups) if close_all else 0
        if not close_all:
            for i, group in enumerate(groups[:-1]):
                # 다음 임시 색 경계가 사라지면 앞 발언도 달라진다. 구성 조각과 경계 양쪽을 기다린다.
                if not all(s.final for s in group.segs) or not groups[i + 1].segs[0].final:
                    break
                ready += 1
        to_close = groups[:ready]
        for g in to_close:
            if g.text:
                out.append(self._event(g, True))
                self.closed.append(g)
            if g.hwa is not None:
                self.prev_hwa = g.hwa
            if g.key == self.open_key:
                self.open_key = None
        if len(self.closed) > TURN_KEEP_CLOSED:
            # 한 source가 여러 조각이면 형제를 함께 보존한다. 조각 수를 source 수로 착각하지 않는다.
            recent_bases = {s.base for g in self.closed[-TURN_KEEP_CLOSED:] for s in g.segs}
            first = next(i for i, g in enumerate(self.closed) if any(s.base in recent_bases for s in g.segs))
            del self.closed[:first]
        closed_keys = {s.key for g in to_close for s in g.segs}
        self.window = [s for s in self.window if s.key not in closed_keys]
        opened = [g for g in groups[ready:] if g.text]
        current_keys = {g.key for g in opened}
        committed_keys = {g.key for g in to_close}
        for key in sorted(self.open_keys - current_keys - committed_keys):
            out.append(self._retract(key, self.updated))
        for group in opened:
            out.append(self._event(group, False))
        self.open_keys = current_keys
        self.open_key = opened[-1].key if opened else None

    # ── 입력 ────────────────────────────────────────────────
    def feed(self, ev: SegmentEvent, now: int | None = None) -> list[SegmentEvent]:
        now = now or now_ms()
        out: list[SegmentEvent] = []
        parts = [t for t in ev.turns if t.text and t.text.strip()]
        if not parts:
            return out
        base = ev.key
        new = [_TSeg(key=f"{base}~{j}", base=base, text=tr.text.strip(), final=ev.is_final,
                     hwa=_decided_hwa(tr.speaker_hint), marker=bool(tr.new_speaker),
                     ts_start=ev.ts_start, ts_end=ev.ts_end or ev.ts_start,
                     scd=_truthy(ev.raw.get("scd"))) for j, tr in enumerate(parts)]
        new = self._mark_uncertain(new)
        if _truthy(ev.raw.get("scd")):
            joined = {i for group in find_caption_fragment_joins([s.text for s in new])
                      for i in range(group.start + 1, group.stop)}
            new = [replace(s, joined=i in joined) for i, s in enumerate(new)]
        if ev.is_final and (ev.raw.get("_finalized_by") == "stability_limit"
                           or "scd" in ev.raw and not _truthy(ev.raw["scd"])):
            new = [replace(s, uncertain=True) for s in new]
        prior_turns = [t for t in self.closed if any(s.base == base for s in t.segs)]
        if prior_turns and not ev.is_final:
            return out  # 이미 분석한 판본에는 원조각의 새 확정 뒤에만 갱신·검토 후보를 전달한다.
        closed_pieces = {s.key: s for t in prior_turns for s in t.segs if s.base == base}
        window_pieces = {s.key: s for s in self.window if s.base == base}
        old_keys = set(closed_pieces) | set(window_pieces)
        by_key = {s.key: s for s in new}
        signature = tuple((s.text, s.hwa, s.marker, s.uncertain) for s in new)
        prior_signature = self._review_signatures.get(base)
        if ev.raw.get("_source_reopened") and not prior_turns and not window_pieces:
            if not ev.is_final:
                return out
            if signature == prior_signature:
                return out
            # 기억 창 밖의 과거 조각을 새 발언으로 만들지 않는다. 저장된 원출처에 검토 판본만 붙인다.
            revision = {"source_key": base, "parts": [
                {"piece_index": j, "text": s.text, "hwa": s.hwa, "new_speaker": s.marker}
                for j, s in enumerate(new)], "prior_turn_keys": [], "reason": "expired_source"}
            self._review_signatures[base] = signature
            while len(self._review_signatures) > TURN_REVIEW_SIGNATURES:
                self._review_signatures.pop(next(iter(self._review_signatures)))
            return [SegmentEvent(key=base, ts_start=ev.ts_start, ts_end=ev.ts_end, turns=[], is_final=True,
                                 raw={"speaker_revision_only": True, "speaker_revision": revision})]
        inner_boundary_changed = any(
            i > 0 and s.base == base and (n := by_key.get(s.key)) is not None
            and (n.marker != s.marker or (n.hwa is not None and t.hwa is not None and n.hwa != t.hwa))
            for t in prior_turns for i, s in enumerate(t.segs))
        if (prior_turns and all(s.final for s in closed_pieces.values())
                and (old_keys != set(by_key) or inner_boundary_changed
                     or (prior_signature is not None and signature != prior_signature))):
            if signature == prior_signature:
                return out  # 같은 후보의 확정 확인·재전송은 새 검토 입력이 아니다
            # 확정 본문을 줄이거나 새 final 행을 겹쳐 만들지 않는다. 최종 재분할은 별도 검토 판본의 후보다.
            revision = {"source_key": base, "parts": [
                {"piece_index": j, "text": s.text, "hwa": s.hwa, "new_speaker": s.marker}
                for j, s in enumerate(new)], "prior_turn_keys": [t.key for t in prior_turns],
                "reason": "late_repartition"}
            # 경계에 다른 원조각이 함께 묶이면 일부 형제는 기억 창 밖에 있다.
            # 저장소가 원출처로 전체 형제를 찾도록 후보만 보내며 본문을 재수신하지 않는다.
            out.append(SegmentEvent(key=base, ts_start=ev.ts_start, ts_end=ev.ts_end, turns=[], is_final=True,
                                    raw={"speaker_revision_only": True, "speaker_revision": revision}))
            self._review_signatures.pop(base, None)
            self._review_signatures[base] = signature
            while len(self._review_signatures) > TURN_REVIEW_SIGNATURES:
                self._review_signatures.pop(next(iter(self._review_signatures)))
            return out
        # 1) 이미 닫힌 발언에 들어간 조각은 그 자리에서 고친다(늦은 갱신) — 경계는 다시 나누지 않는다
        placed: set[str] = set()
        for t in self.closed:
            changed = False
            metadata_changed = False
            # 닫힌 발언 '끝부분'의 조각이 늦은 화자 판정에서 다른 화자로 나오면 떼어 내 열린 구간으로 보낸다
            tail = len(t.segs)
            while tail > 1 and t.segs[tail - 1].base == base:
                n = next((x for x in new if x.key == t.segs[tail - 1].key), None)
                if n is None or not (n.marker or (n.hwa is not None and t.hwa is not None and n.hwa != t.hwa)):
                    break
                tail -= 1
            if tail < len(t.segs) and t is self.closed[-1]:
                t.segs = t.segs[:tail]
                changed = True
            for i, sg in enumerate(t.segs):
                if sg.base != base:
                    continue
                n = next((x for x in new if x.key == sg.key), None)
                if n is None:
                    continue
                placed.add(n.key)
                metadata_changed = metadata_changed or ((n.hwa or sg.hwa) != sg.hwa or n.marker != sg.marker
                                                        or (n.scd and not sg.scd) or n.uncertain != sg.uncertain)
                if n.text != sg.text or (n.final and not sg.final) or metadata_changed:
                    t.segs[i] = _TSeg(sg.key, base, n.text, sg.final or n.final, n.hwa or sg.hwa, n.marker,
                                      sg.ts_start, max(sg.ts_end, n.ts_end), n.scd or sg.scd, n.uncertain, n.joined)
                    changed = True
            if changed:
                # 단순 확정 재전송은 짧은 구절을 병합할 때 고른 대표 라벨을 바꾸지 않는다.
                if metadata_changed:
                    t.hwa = next((s.hwa for s in reversed(t.segs) if s.hwa is not None), None)
                    t.new_speaker = t.segs[0].marker
                    if any(s.uncertain for s in t.segs):
                        t.boundary_reason = "uncertain_marker"
                        t.turn_id = t.key
                    elif t.new_speaker:
                        t.boundary_reason = "marker"
                        t.turn_id = t.key
                revised = self._event(t, True)
                if metadata_changed:
                    revised.raw["speaker_metadata_changed"] = True
                out.append(revised)
        if self.closed:
            self.prev_hwa = self.closed[-1].hwa
        rest = [x for x in new if x.key not in placed]
        if not rest:
            return out
        # 2) 열린 구간: 이 segment 의 조각을 통째로 바꾼다(재분할 대응)
        idx = [i for i, sg in enumerate(self.window) if sg.base == base]
        if idx:
            old = {sg.key: sg for sg in self.window if sg.base == base}
            merged = []
            for x in rest:
                o = old.get(x.key)
                same_version = bool(o and (x.text, x.hwa, x.marker, x.scd, x.uncertain)
                                    == (o.text, o.hwa, o.marker, o.scd, o.uncertain)
                                    and not ev.raw.get("_source_reopened"))
                merged.append(_TSeg(x.key, base, x.text, x.final or bool(o and o.final and same_version), x.hwa,
                                    x.marker, min(x.ts_start, o.ts_start) if o else x.ts_start, x.ts_end,
                                    x.scd, x.uncertain, x.joined))
            i0 = idx[0]
            self.window = self.window[:i0] + merged + [sg for sg in self.window[i0:] if sg.base != base]
        else:
            # 시각 순서를 지키며 끼워 넣는다(닫힌 발언에서 떼어 낸 조각은 열린 구간 앞쪽에 온다)
            pos = next((i for i, sg in enumerate(self.window) if sg.ts_start > rest[0].ts_start), len(self.window))
            self.window[pos:pos] = rest
        self.updated = now
        self._regroup(out)
        return out

    def take_idle(self, now: int | None = None) -> list[SegmentEvent]:
        """잠잠해진 열린 구간을 닫는다(수집 루프의 주기 점검에서 부른다)."""
        now = now or now_ms()
        out: list[SegmentEvent] = []
        if not self.window:
            return out
        quiet = now - self.updated
        if (all(s.final for s in self.window) and quiet >= TURN_IDLE_MS) or quiet >= TURN_STALE_MS:
            self._regroup(out, close_all=True)
        return out

    def flush(self) -> list[SegmentEvent]:
        out: list[SegmentEvent] = []
        if self.window:
            self._regroup(out, close_all=True)
        return out

    def next_due(self, now: int) -> int | None:
        if not self.window:
            return None
        limit = TURN_IDLE_MS if all(s.final for s in self.window) else TURN_STALE_MS
        return max(0, self.updated + limit - now)


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "y", "yes", "t"}
    return bool(v)


def _clean(text: Any) -> tuple[str, bool]:
    """자막 텍스트 정리. 앞 '-' 는 화자 전환 표시 → (본문, True)."""
    s = html.unescape(str(text if text is not None else "")).replace(" ", " ").strip()
    new = s.startswith("-")
    if new:
        s = s[1:].strip()
    return re.sub(r"\s+", " ", s), new


def parse_caption_message(msg: Any, state: CaptionState, now: int | None = None) -> SegmentEvent | None:
    """AI자막 메시지 {segment, transcript, transcripts:[[hwa,text],...], scd, final} → SegmentEvent.

    같은 segment 가 여러 번 오면 같은 key(`s{segment}`)로 갱신한다. 반환 이벤트의 is_final 은 메시지의 final 값이고,
    이벤트는 state.pending 에 보류된다(실제 확정은 state.take_due()/take_all_pending() 가 꺼낸다).
    transcripts 의 각 줄은 Turn(speaker_hint='hwa:{hwa}', 앞 '-' 는 new_speaker). 없으면 transcript 한 줄.
    빈 내용·segment 없는 메시지, 이미 받은 내용의 재전송, 진행 중 segment 의 같은 내용 반복은 None.
    """
    if isinstance(msg, (bytes, bytearray)):
        msg = msg.decode("utf-8", "replace")
    if isinstance(msg, str):
        try:
            msg = json.loads(msg)
        except ValueError:
            return None
    if not isinstance(msg, dict):
        return None
    seg = msg.get("segment")
    if seg is None or str(seg).strip() == "":
        return None
    now = now or now_ms()
    turns: list[Turn] = []
    tr = msg.get("transcripts")
    if isinstance(tr, list) and tr:
        for item in tr:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                hwa, text = item[0], item[1]
            elif isinstance(item, dict):
                hwa, text = item.get("hwa"), item.get("text")
            else:
                continue
            t, new = _clean(text)
            if t:
                hint = f"hwa:{str(hwa).strip()}" if hwa is not None and str(hwa).strip() != "" else None
                turns.append(Turn(text=t, speaker_hint=hint, new_speaker=new))
    else:
        t, new = _clean(msg.get("transcript"))
        if t:
            turns.append(Turn(text=t, new_speaker=new))
    if not turns:
        return None
    seg = str(seg).strip()
    current = state.pending.get(state.seg_keys.get(seg, ""))
    if current is not None and _truthy(current.raw.get("scd")) and not _truthy(msg.get("scd")):
        current_text = norm_caption(current.text)
        incoming_text = norm_caption(" ".join(t.text for t in turns))
        if current_text and incoming_text and current_text.startswith(incoming_text):
            state.duplicates += 1
            return None  # 늦게 재전송된 본문 단계가 이미 받은 화자 판정을 되돌리지 않는다.
    signature = (_truthy(msg.get("scd")), _turn_sig(turns))
    key, mode = state.resolve(seg, norm_caption(" ".join(t.text for t in turns)), now, signature)
    if key is None:
        state.duplicates += 1
        return None
    is_final = _truthy(msg.get("final"))
    same = False
    if mode == "pending":
        prev = state.pending[key]
        same = (_turn_sig(prev.turns) == _turn_sig(turns)
                and _truthy(prev.raw.get("scd")) == _truthy(msg.get("scd"))
                and (prev.is_final or not is_final))
    elif mode == "reopen":
        state._reopen(key, seg)
    elif mode == "new":
        state.first_rx[key] = state._order_start(key, seg, now)
    if mode != "pending":
        state.opened_rx[key] = now
    state.seg_keys[seg] = key
    state.last_rx[key] = now
    if not same:
        state.stable_rx[key] = now
    state.last_key = state.last_any_key = key
    raw = {**msg, "_source_reopened": True} if mode == "reopen" else msg
    if mode == "pending" and state.pending[key].raw.get("_source_reopened"):
        raw = {**raw, "_source_reopened": True}
    ev = SegmentEvent(key=key, ts_start=state.first_rx.setdefault(key, now), ts_end=now, turns=turns,
                      is_final=is_final or (same and state.pending[key].is_final), raw=raw)
    state.pending[key] = ev
    return None if same else ev



# ── 회의 상태 감시(live_play 주기 확인) ──────────────────────────────
STATUS_MARK_RE = re.compile(r"정회|속개|산회|중지|종료|개의|개회")
# 회의가 끝났다는 xdesc(산회·감사종료·회의종료 …). '감사중지'·'정회'는 끝이 아니다.
END_DESC_RE = re.compile(r"산회|종료|마침")
# 쉬는 중이라 곧 이어질 수 있는 xdesc — 이때는 '오래 수신 없음'만으로 자동 종료하지 않는다(점심 정회 등)
PAUSE_DESC_RE = re.compile(r"정회|중지|계속|속개|예정|대기")
AUTO_STOP_DEFAULT_MIN = 30.0
END_GRACE_MIN = 5.0        # 종료 문구를 본 뒤 이만큼(설정값이 더 작으면 그만큼) 조용하면 종료
DATE_PASSED_QUIET_MIN = 180.0  # 회의일이 지난 세션: 이만큼(설정값이 더 크면 그만큼) 조용하면 종료(자정 넘긴 정회 보호)


class MeetingWatcher:
    """live_play 로 회의 상태(xstat/xdesc)를 주기 확인해 state 에 반영하고, 정회·속개 같은 변화는 기록 표지로 남긴다."""

    def __init__(self, ctx: SourceContext, client: AssemblyClient, xcode: str, xcgcd: str,
                 interval_sec: float = 60.0):
        self.ctx = ctx
        self.client = client
        self.xcode = xcode
        self.xcgcd = xcgcd
        self.interval = interval_sec
        self._next = 0.0
        self.xstat: str | None = None
        self.xdesc: str | None = None
        self.checked_ms: int | None = None
        self.seen_live = False
        self.offline_since: float | None = None   # 생중계 뒤 xstat!='1' 이 된 시각(monotonic)

    async def tick(self, force: bool = False) -> dict | None:
        now = time.monotonic()
        if not force and now < self._next:
            return None
        self._next = now + self.interval
        try:
            info = await self.client.live_play(self.xcode, self.xcgcd)
        except AssemblyError as e:
            self.ctx.log(f"회의 상태 확인 실패: {e}", "warning")
            return None
        await self.apply(info)
        return info

    async def apply(self, info: dict) -> None:
        xstat, xdesc = str(info.get("xstat") or ""), str(info.get("xdesc") or "").strip()
        prev_stat, prev_desc = self.xstat, self.xdesc
        self.xstat, self.xdesc = xstat, xdesc
        self.checked_ms = now_ms()
        if xstat == "1":
            self.seen_live = True
            self.offline_since = None
        elif self.seen_live and self.offline_since is None:
            self.offline_since = time.monotonic()
        label = ("생중계 중" if xstat == "1" else "생중계 아님") + (f" · {xdesc}" if xdesc else "")
        self.ctx.set_state(meeting={"xstat": xstat, "xdesc": xdesc, "xsubj": info.get("xsubj", ""),
                                    "checked_ms": self.checked_ms}, detail=label)
        changed = prev_desc is not None and (xdesc != prev_desc or xstat != prev_stat)
        if changed:
            self.ctx.log(f"회의 상태 변경: {prev_desc or '-'} → {xdesc or '-'} (xstat {prev_stat}→{xstat})")
            if xdesc and STATUS_MARK_RE.search(xdesc):
                t = now_ms()
                try:
                    await self.ctx.pipeline.add_marker(f"— {xdesc}({hm(t)}) —", ts=t, key=f"st{t}")
                except Exception:  # pragma: no cover - 방어
                    logger.exception("회의 상태 표지 기록 실패")


class AutoStop:
    """산회 후 자동 종료 판단(설정 assembly.auto_stop_minutes, 0=끔; 세션 source_config.auto_stop_min 이 있으면 그 값).

    다음 중 하나면 종료 사유(문자열)를 돌려준다. 회의 상태(xstat)를 모르면 판단하지 않는다.
    1) xstat != '1' 이고 xdesc 가 산회·종료류인 상태가 min(설정, 5분) 이어지고, 그동안 새 발언도 없을 때.
    2) 한 번이라도 발언을 받은 뒤 '설정 분' 동안 아무것도 못 받았고 xstat != '1' 일 때.
       단 xdesc 가 정회·감사중지·감사계속·속개 예정이면 쉬는 중으로 보고 기다린다.
    3) 회의일(세션 meeting_date)이 지났는데(KST 기준) max(설정 분, 3시간) 동안 새 발언이 없고 xstat != '1' 일 때
       — 산회 후 [종료]를 잊고 밤을 넘긴 세션에 다음 날 같은 방 자막이 섞이지 않게(xdesc 와 무관).
       자정을 넘긴 국감의 짧은 정회는 3시간보다 짧으므로 끊지 않는다.
    """

    def __init__(self, ctx: SourceContext, watcher: MeetingWatcher | None, override: Any = None):
        self.ctx = ctx
        self.watcher = watcher
        self.override = override
        self.last_activity_ms: int | None = None
        self.end_since_ms: int | None = None
        self.started_ms = now_ms()
        self.meeting_date = str((getattr(ctx, "session", None) or {}).get("meeting_date") or "")

    @staticmethod
    def today() -> str:
        return datetime.now(KST).date().isoformat()

    def minutes(self) -> float:
        v = self.override
        if v is None or v == "":
            try:
                v = (self.ctx.settings("assembly") or {}).get("auto_stop_minutes", AUTO_STOP_DEFAULT_MIN)
            except Exception:  # pragma: no cover - 방어
                v = AUTO_STOP_DEFAULT_MIN
        try:
            return max(0.0, float(v or 0))
        except (TypeError, ValueError):
            return AUTO_STOP_DEFAULT_MIN

    def note_activity(self, t: int | None = None) -> None:
        self.last_activity_ms = t or now_ms()

    def check(self, now: int | None = None) -> str | None:
        mins = self.minutes()
        w = self.watcher
        if mins <= 0 or w is None or w.xstat is None:
            self.end_since_ms = None
            return None
        now = now or now_ms()
        if w.xstat == "1":
            self.end_since_ms = None
            return None
        desc = w.xdesc or ""
        if END_DESC_RE.search(desc):
            if self.end_since_ms is None:
                self.end_since_ms = now
            grace = min(mins, END_GRACE_MIN) * 60_000
            quiet = self.last_activity_ms is None or now - self.last_activity_ms >= grace
            if now - self.end_since_ms >= grace and quiet:
                return f"회의 종료 확인({desc}) 후 {grace / 60_000:g}분 경과"
            return None
        self.end_since_ms = None
        quiet_since = self.last_activity_ms if self.last_activity_ms is not None else self.started_ms
        overnight = max(mins, DATE_PASSED_QUIET_MIN)
        if self.meeting_date and self.meeting_date < self.today() and now - quiet_since >= overnight * 60_000:
            return f"회의일({self.meeting_date})이 지났고 {overnight:g}분 동안 새 발언이 없음" + (f"({desc})" if desc else "")
        if PAUSE_DESC_RE.search(desc):
            return None
        if self.last_activity_ms is not None and now - self.last_activity_ms >= mins * 60_000:
            return f"마지막 수신 후 {mins:g}분 동안 새 발언이 없고 생중계가 아님" + (f"({desc})" if desc else "")
        return None


async def resolve_meeting_codes(ctx: SourceContext, client: AssemblyClient, config: dict) -> tuple[str, str]:
    """source_config 의 (xcode, xcgcd). xcode 만 있으면 오늘 live_list 에서 xcgcd 를 찾아 source_config 에 기록한다."""
    xcode = str(config.get("xcode") or "").strip()
    xcgcd = str(config.get("xcgcd") or "").strip()
    if xcode and not xcgcd:
        try:
            items = await client.live_list()
        except AssemblyError as e:
            raise SourceError(f"오늘 중계 목록을 받지 못했습니다: {e}") from e
        it = next((i for i in items if str(i.get("xcode")) == xcode), None)
        xcgcd = str((it or {}).get("xcgcd") or "")
        if not xcgcd:
            raise SourceError(f"오늘 이 위원회(xcode={xcode})의 중계 일정이 없습니다(xcgcd 미확인)")
        config["xcgcd"] = xcgcd
        try:
            ctx.repo.update_session(ctx.sid, source_config=config)
        except Exception:  # pragma: no cover - 방어
            logger.exception("source_config 갱신 실패")
        ctx.log(f"회의 코드 확인: xcgcd={xcgcd}")
    return xcode, xcgcd


class _ResolveError(AssemblyError):
    """설정·회의 정보 문제(자막 주소 없음 등). 한 번도 연결하지 못했으면 바로 중단, 연결된 적이 있으면 재시도."""


# ── AI자막 수집원 ─────────────────────────────────────────────────
class AssemblyCaptionSource(Source):
    """국회 AI자막 WebSocket 수신. source_config: xcode, xcgcd(선택), xsami(선택: 직접 지정),
    caption_path('hls'|'rtsp', 기본 'hls'), auto_stop_min(선택: 이 세션만 자동 종료 분, 없으면 설정값)."""

    kind = "assembly_caption"
    BACKOFF_START = 5.0
    BACKOFF_MAX = 60.0
    RECV_POLL = 5.0
    IDLE_RECONNECT_FIRST = 60.0   # 생중계 중(xstat=1)인데 연결 후 첫 메시지가 이만큼 없으면 재연결
    IDLE_RECONNECT = 90.0         # 생중계 중인데 마지막 메시지 후 이만큼 없으면 재연결
    HOUSEKEEP_SEC = 5.0

    def __init__(self, ctx: SourceContext):
        super().__init__(ctx)
        self.client: AssemblyClient | None = None
        self.state = CaptionState()
        self.watcher: MeetingWatcher | None = None
        self.gap = GapMarker(ctx)
        self.autostop = AutoStop(ctx, None, self.config.get("auto_stop_min"))
        self._raw_fp: Any = None
        self._auto_stop = asyncio.Event()
        self._emit: Emit | None = None
        self._emit_turn: Emit | None = None
        self.turns = TurnAssembler()
        self._last_xsami: str | None = None
        self._connected_once = False
        self._idle_streak = 0
        self._dup_logged = 0
        # 매니저가 정상 종료 때 감사 기록에 쓴다
        self.end_action = ""
        self.end_detail = ""

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        if self.config.get("caption_replay") is True:
            from .caption_replay import run_recording
            await run_recording(self, emit, stop)
            return
        if not self.ctx.config.allow_assembly_network:
            raise SourceError("외부망(국회 의사중계) 호출이 꺼져 있습니다(GUKGAM_ALLOW_ASSEMBLY_NETWORK=0)")
        import websockets

        self.client = AssemblyClient()
        # segment 단위 이벤트는 TurnAssembler 로 화자 턴(발언) 단위로 묶어 파이프라인에 넘긴다
        self.turns = TurnAssembler()
        self._emit_turn = emit
        emit = self._feed_turns
        self._emit = emit
        hk: asyncio.Task | None = None
        try:
            xcode, xcgcd = await self._resolve_codes()
            keys, texts = self._prior_state()
            self.state = CaptionState(prior_keys=keys, prior_texts=texts)
            self._open_raw()
            if xcode and xcgcd:
                self.watcher = MeetingWatcher(self.ctx, self.client, xcode, xcgcd)
            self.autostop.watcher = self.watcher
            hk = asyncio.create_task(self._housekeeping(stop), name=f"caption-hk-{self.ctx.sid}")
            backoff = self.BACKOFF_START
            while not stop.is_set() and not self._auto_stop.is_set():
                try:
                    url = await self._resolve_ws(xcode, xcgcd)
                except AssemblyError as e:
                    config_err = isinstance(e, _ResolveError) or "유효하지 않은" in str(e)
                    if config_err and not self._connected_once:
                        raise SourceError(str(e)) from e
                    self.ctx.set_state(phase="reconnecting", error=str(e),
                                       detail=f"중계 정보 조회 실패 — {backoff:.0f}초 후 재시도")
                    self.ctx.log(str(e), "warning")
                    if await sleep_or_stop(stop, backoff):
                        break
                    backoff = min(backoff * 2, self.BACKOFF_MAX)
                    continue
                self.ctx.set_state(phase="connecting", ws_url=url)
                opened = time.monotonic()
                got = 0
                try:
                    async with websockets.connect(
                        url, subprotocols=["echo-protocol"], origin=ORIGIN, user_agent_header=UA,
                        open_timeout=10, ping_interval=20, ping_timeout=20, close_timeout=3, max_size=2**22,
                    ) as ws:
                        self._connected_once = True
                        self.ctx.set_state(phase="connected", error=None)
                        self.ctx.log(f"자막 서버 연결: {url}")
                        got, idle = await self._recv_loop(ws, emit, stop)
                    if not stop.is_set() and not idle and not self._auto_stop.is_set():
                        self.ctx.log("자막 연결이 서버에서 종료됨")
                except websockets.ConnectionClosed as e:
                    code = e.rcvd.code if e.rcvd else None
                    self.ctx.log(f"자막 연결 종료(code={code})", "warning")
                except (OSError, asyncio.TimeoutError, websockets.InvalidHandshake, websockets.InvalidURI) as e:
                    self.ctx.set_state(error=f"자막 서버 연결 실패: {type(e).__name__} {e}")
                    self.ctx.log(f"자막 서버 연결 실패: {type(e).__name__} {e}", "warning")
                await self._finalize_due()
                if stop.is_set() or self._auto_stop.is_set():
                    break
                # 잠깐 열렸다 바로 닫히는(없는 방 1005) 경우엔 백오프를 늘리고, 오래 유지됐으면 초기화
                if got or time.monotonic() - opened > 30:
                    backoff = self.BACKOFF_START
                wait = backoff + random.random()
                self.ctx.set_state(phase="reconnecting", detail=f"{wait:.0f}초 후 재연결")
                if await sleep_or_stop(stop, wait):
                    break
                backoff = min(backoff * 2, self.BACKOFF_MAX)
            # 남은 미확정 segment 를 확정으로 넘기고, 열린 발언도 닫는다
            for ev in self.state.take_all_pending():
                await emit(ev)
            for ev in self.turns.flush():
                await self._emit_turn(ev)
            if self._auto_stop.is_set():
                self.ctx.set_state(phase="done", detail=f"자동 종료: {self.end_detail}" if self.end_detail
                                   else "생중계 종료로 자동 종료")
        finally:
            if hk is not None:
                hk.cancel()
            if self._raw_fp is not None:
                self._raw_fp.close()
                self._raw_fp = None
            if self.client is not None:
                await self.client.aclose()

    def _idle_limit(self, got_any: bool) -> float:
        base = self.IDLE_RECONNECT if got_any else self.IDLE_RECONNECT_FIRST
        return base * (2 ** min(self._idle_streak, 3))       # 계속 비면 간격을 늘린다(최대 8배)

    async def _recv_loop(self, ws: Any, emit: Emit, stop: asyncio.Event) -> tuple[int, bool]:
        """(받은 메시지 수, 무수신 재연결 여부)."""
        got = 0
        opened = time.monotonic()
        last: float | None = None
        stop_w = asyncio.ensure_future(stop.wait())
        auto_w = asyncio.ensure_future(self._auto_stop.wait())
        try:
            while True:
                timeout = self.RECV_POLL
                nowm = now_ms()
                for due in (self.state.next_due(nowm), self.turns.next_due(nowm)):
                    if due is not None:
                        timeout = min(timeout, max(0.05, due / 1000))
                recv = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait({recv, stop_w, auto_w}, timeout=timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if recv not in done:
                    recv.cancel()
                    try:
                        await recv
                    except (asyncio.CancelledError, Exception):
                        pass
                    if stop_w in done or auto_w in done:
                        return got, False
                    await self._finalize_due()
                    # 생중계 중인데 이 연결로 아무것도 오지 않으면 다시 연결한다(원본 플레이어의 무수신 재연결과 같은 취지)
                    w = self.watcher
                    idle = time.monotonic() - (last or opened)
                    if w is not None and w.xstat == "1" and idle >= self._idle_limit(last is not None):
                        self._idle_streak += 1
                        what = "첫 자막이" if last is None else "자막이"
                        self.ctx.log(f"생중계 중인데 {idle:.0f}초 동안 {what} 오지 않아 다시 연결합니다", "warning")
                        self.ctx.set_state(detail="생중계 중이나 자막 무수신 — 재연결")
                        return got, True
                    continue
                raw = recv.result()       # ConnectionClosed 는 위로 올린다
                got += 1
                last = time.monotonic()
                self._idle_streak = 0
                await self.on_raw(raw, emit)
        finally:
            for t in (stop_w, auto_w):
                t.cancel()

    async def on_raw(self, raw: Any, emit: Emit) -> None:
        """원본 보존(data/raw/{sid}.jsonl) → 파싱 → 보류가 끝난 segment 확정 → 새 내용은 미확정으로 emit."""
        t = now_ms()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if self._raw_fp is not None:
            try:
                entry = {"rx": t, "msg": raw}
                if self.config.get("caption_replay") is True:
                    entry["recording_replay"] = True
                self._raw_fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._raw_fp.flush()
            except OSError:  # pragma: no cover - 디스크 부족 등
                logger.exception("원본 자막 저장 실패")
        dup0, reopen0 = self.state.duplicates, self.state.reopened
        ev = parse_caption_message(raw, self.state, now=t)
        replayed = (self.state.duplicates > dup0 or bool(ev is not None
                    and ev.raw.get("_source_reopened") and ev.key in self.state.prior_keys))
        # 이전 실행의 재전송도 원단위로 보존하되 현재 음성 시각·새 모델 입력으로 오인하지 않는다.
        # 말묶음 emit보다 먼저 저장해야 기존 자동 모델 경로와 중복 실행되지 않는다.
        stages = getattr(getattr(self.ctx, "rt", None), "stages", None)
        if stages is not None:
            try:
                message = json.loads(raw) if isinstance(raw, str) else raw
                stages.ingest(self.ctx.sid, message, t, replayed=replayed)
            except (ValueError, TypeError):
                self.ctx.log("원수신 자막 형식을 확인해야 합니다", "warning")
        await self.gap.on_rx(t)
        self.ctx.mark_rx()
        if self.state.duplicates > dup0 and self.state.duplicates >= self._dup_logged * 2 + 1:
            self._dup_logged = self.state.duplicates
            self.ctx.log(f"이미 받은 자막 재전송을 건너뜀(누적 {self.state.duplicates}건)")
        if self.state.reopened > reopen0 and ev is not None:
            self.ctx.log(f"확정된 발언의 늦은 갱신 반영: {ev.key}")
        for old in self.state.take_due(t):
            await emit(old)
        if ev is None:
            return
        self.autostop.note_activity(t)
        if ev.key not in self.state.pending:
            return  # 같은 수신에서 절대 한도로 이미 마감한 판본을 다시 부분 결과로 열지 않는다.
        await emit(replace(ev, is_final=False) if ev.is_final else ev)

    async def _feed_turns(self, ev: SegmentEvent) -> None:
        """segment 이벤트 → TurnAssembler → 발언 이벤트 emit."""
        for out in self.turns.feed(ev):
            await self._emit_turn(out)

    async def _finalize_due(self) -> None:
        if self._emit is None:
            return
        now = now_ms()
        for ev in self.state.take_due(now):
            await self._emit(ev)
        for ev in self.turns.take_idle(now):       # 잠잠해진 발언 확정
            await self._emit_turn(ev)

    async def _housekeeping(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._finalize_due()       # 연결이 끊긴 동안에도 보류가 끝난 segment 를 확정
                gap_opened = await self.gap.check()
                if self.watcher is not None:
                    await self.watcher.tick(force=gap_opened)     # 끊기면 회의 상태(정회 등)를 바로 확인
                reason = self.autostop.check()
                if reason:
                    self.end_action = "session.auto_stop"
                    self.end_detail = reason
                    self.ctx.log(f"자동 종료: {reason}")
                    self._auto_stop.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 방어
                logger.exception("자막 수집 주기 점검 실패")
            if await sleep_or_stop(stop, self.HOUSEKEEP_SEC):
                return

    # ── 설정 해석 ─────────────────────────────────────────────
    async def _resolve_codes(self) -> tuple[str, str]:
        assert self.client is not None
        if not str(self.config.get("xcode") or "").strip() and not self.config.get("xsami"):
            raise SourceError("수집원 설정에 위원회 코드(xcode) 또는 자막 주소(xsami)가 없습니다")
        return await resolve_meeting_codes(self.ctx, self.client, self.config)

    async def _resolve_ws(self, xcode: str, xcgcd: str) -> str:
        """자막 WS 주소. live_play 가 실패하거나 불완전해도 이전에 받은 주소가 있으면 그것으로 재연결한다."""
        assert self.client is not None
        info: dict = {}
        fixed = str(self.config.get("xsami") or "").strip()
        if xcode and xcgcd:
            try:
                info = await self.client.live_play(xcode, xcgcd, fresh=True)
                if self.watcher is not None:
                    await self.watcher.apply(info)
            except AssemblyError as e:
                fallback = fixed or self._last_xsami
                if not fallback:
                    raise
                self.ctx.log(f"중계 정보 조회 실패 — 이전 자막 주소로 재연결합니다: {e}", "warning")
                info = {}
        xsami = fixed or str(info.get("xsami") or "").strip()
        if not xsami.startswith(("ws://", "wss://")):
            if self._last_xsami:
                self.ctx.log("중계 정보에 자막 주소(xsami)가 없어 이전 주소로 재연결합니다", "warning")
                xsami = self._last_xsami
            else:
                raise _ResolveError("이 회의는 자막 서버 정보(xsami)가 없습니다")
        if STENO_RE.search(xsami):
            raise SourceError("속기 자막(socket.io) 서버는 아직 지원하지 않습니다 — '국회 중계영상 음성인식'을 쓰세요")
        self._last_xsami = xsami
        url = caption_ws_url(xsami, str(self.config.get("caption_path") or "hls"))
        hls = hls_urls(info)
        extra: dict[str, Any] = {}
        if hls.get("default"):
            extra["hls_url"] = hls["default"]
        if info.get("xsubj"):
            extra["xsubj"] = info["xsubj"]
        if extra:
            self.ctx.set_state(**extra)
        return url

    def _prior_keys(self) -> dict[str, int]:
        return self._prior_state()[0]

    def _prior_state(self) -> tuple[dict[str, int], dict[str, str]]:
        """이전 실행에서 쓴 자막 segment key → (마지막 갱신 시각, 본문). 재개 때 재전송을 걸러 내는 근거.

        발언 묶기(TurnAssembler) 이후 segments 테이블의 key 는 발언 key('ts1944~0')이고, 그 raw.segments 에
        조각 key('s1944~0')별 본문이 있다. 옛 방식(segment key 's1944') 행도 그대로 읽는다.
        """
        rows = self.ctx.repo.db.query(
            "SELECT source_key, updated_at, text, raw FROM segments WHERE session_id=? "
            "AND (source_key LIKE 's%' OR source_key LIKE 't%')", (self.ctx.sid,))
        keys: dict[str, int] = {}
        texts: dict[str, str] = {}
        parts: dict[str, dict[str, str]] = {}
        for r in rows:
            k = r["source_key"]
            if k.startswith("s"):
                keys[k] = int(r["updated_at"])
                texts[k] = r.get("text") or ""
                continue
            try:
                raw = json.loads(r.get("raw") or "{}")
            except ValueError:
                continue
            for sub, txt in (raw.get("segments") or {}).items():
                base, _, idx = str(sub).partition("~")
                keys[base] = max(keys.get(base, 0), int(r["updated_at"]))
                parts.setdefault(base, {})[idx or "0"] = txt or ""
        for base, d in parts.items():
            texts[base] = " ".join(d[i] for i in sorted(d, key=lambda x: int(x) if x.isdigit() else 0))
        return keys, texts

    def _open_raw(self) -> None:
        from ..maintenance import raw_path

        p = raw_path(self.ctx.config, self.ctx.sid)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            self._raw_fp = open(p, "a", encoding="utf-8")
        except OSError:  # pragma: no cover - 방어
            logger.exception("원본 자막 파일을 열 수 없습니다")
            self._raw_fp = None
