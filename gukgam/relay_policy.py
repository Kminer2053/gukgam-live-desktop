"""중계 사용 기간은 한국 날짜로 판정하며 미설정은 연결하지 않는다."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .config import KST

DEFAULT_POLICY = {"enabled": False, "start_date": "", "end_date": ""}


def normalize_policy(value: Any, current: dict | None = None) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError("중계 서비스 설정 항목을 확인하세요")
    out = {**DEFAULT_POLICY, **(current or {}), **value}
    if type(out["enabled"]) is not bool:
        raise ValueError("중계 서비스 사용 여부는 참·거짓 값이어야 합니다")
    for key in ("start_date", "end_date"):
        text = out[key]
        try:
            valid = isinstance(text, str) and (not text or (len(text) == 10 and date.fromisoformat(text).isoformat() == text))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("중계 시작일과 종료일은 YYYY-MM-DD 날짜로 입력하세요")
    start, end = out["start_date"], out["end_date"]
    if bool(start) != bool(end) or (start and start > end):
        raise ValueError("중계 시작일과 종료일을 함께 입력하고 날짜 순서를 확인하세요")
    if out["enabled"] and not start:
        raise ValueError("중계 서비스를 켜려면 시작일과 종료일을 먼저 정하세요")
    return out


def policy_status(value: Any, *, configured: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise ValueError("시간대가 있는 현재 시각이 필요합니다")
    today = now.astimezone(KST).date().isoformat()
    try:
        policy = normalize_policy(value)
    except (ValueError, TypeError):
        policy = dict(DEFAULT_POLICY)
        state = "invalid"
    else:
        state = ("unconfigured" if not policy["start_date"] else "disabled" if not policy["enabled"] else
                 "scheduled" if today < policy["start_date"] else "expired" if today > policy["end_date"] else
                 "active" if configured else "no_connection")
    return {**policy, "state": state, "active": state == "active", "configured": bool(configured),
            "timezone": "Asia/Seoul", "today": today}
