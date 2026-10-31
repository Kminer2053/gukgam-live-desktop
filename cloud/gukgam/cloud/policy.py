"""클라우드 터널 허용목록과 본문 제약(표준 라이브러리만 사용)."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs


@dataclass(frozen=True)
class Rule:
    """메서드·경로별 최소 역할과 실행 레인."""

    method: str
    path: str
    min_role: str
    lane: str = "fast"
    constraint: str = ""
    query_format: str = ""
    query_keys: tuple[str, ...] | None = None
    json_only: bool = False
    _pattern: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        pattern = "/".join(
            "[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
            for part in self.path.split("/")
        )
        object.__setattr__(self, "_pattern", re.compile(pattern))

    def allows(self, role: str | None) -> bool:
        """미로그인·알 수 없는 역할을 거절하고 최소 역할을 확인한다."""
        return role == "operator" or (role == "viewer" and self.min_role == "viewer")


# DESIGN 4장: 압축 표기는 로컬 라우트의 실제 메서드로 풀어 둔다.
RULES = (
    Rule("GET", "/api/cloud-read/sessions", "viewer", query_keys=("status", "limit")),
    Rule("GET", "/api/cloud-read/sessions/{sid}", "viewer", query_keys=()),
    Rule("GET", "/api/cloud-read/sessions/{sid}/snapshot", "viewer",
         query_keys=("recent", "utterance_limit", "before_utterance_id", "block_id",
                     "around_utterance_id", "bookmarks_only", "q", "utterance_ids", "block_summaries")),
    Rule("GET", "/api/cloud-read/sessions/{sid}/transcript-stages", "viewer",
         query_keys=("recent", "before", "limit", "block_id")),
    Rule("GET", "/api/cloud-read/utterances/{uid}", "viewer", query_keys=()),
    Rule("GET", "/api/cloud-read/items", "viewer", json_only=True,
         query_keys=("session_id", "kind", "status", "dept", "block_id", "include_dismissed", "format", "limit")),
    Rule("GET", "/api/cloud-read/search", "viewer", query_keys=("q", "session_id", "speaker", "limit", "offset")),
    Rule("GET", "/api/sessions/{sid}/report", "viewer", "slow"),
    Rule("GET", "/api/sessions/{sid}/export", "viewer", "slow"),
    Rule("GET", "/api/blocks/{bid}/card", "viewer"),
    Rule("GET", "/api/items", "viewer", query_format="csv"),
    Rule("GET", "/api/preflight", "viewer"),
    Rule("GET", "/api/assembly/live", "viewer"),
    Rule("GET", "/api/assembly/org-schedule", "viewer"),
    Rule("POST", "/api/sessions/{sid}/alerts/ack", "viewer"),
    Rule("POST", "/api/sessions", "operator", constraint="session_create"),
    Rule("PATCH", "/api/sessions/{sid}", "operator", constraint="session_patch"),
    Rule("POST", "/api/sessions/{sid}/start", "operator"),
    Rule("POST", "/api/sessions/{sid}/pause", "operator"),
    Rule("POST", "/api/sessions/{sid}/stop", "operator"),
    Rule("POST", "/api/sessions/{sid}/manual", "operator", constraint="manual"),
    Rule("POST", "/api/sessions/{sid}/summarize", "operator", "slow"),
    Rule("PUT", "/api/sessions/{sid}/roster", "operator"),
    Rule("PATCH", "/api/utterances/{uid}", "operator"),
    Rule("PATCH", "/api/blocks/{bid}", "operator"),
    Rule("POST", "/api/blocks/{bid}/summarize", "operator", "slow"),
    Rule("POST", "/api/blocks/{bid}/review", "operator"),
    Rule("PATCH", "/api/blocks/{bid}/summary", "operator"),
    Rule("POST", "/api/items", "operator"),
    Rule("PATCH", "/api/items/{iid}", "operator"),
    Rule("GET", "/api/settings", "operator"),
    Rule("GET", "/api/assembly/agencies", "operator"),
    Rule("GET", "/api/assembly/roster", "operator"),
    Rule("GET", "/api/audit", "operator"),
    Rule("PUT", "/api/settings/org", "operator"),
    Rule("PUT", "/api/settings/watch_keywords", "operator"),
    Rule("PUT", "/api/settings/departments", "operator"),
    Rule("POST", "/api/cloud/resync", "operator"),
)


def _query_value(query: str | bytes | Mapping[str, Any], key: str) -> Any:
    if isinstance(query, bytes):
        query = query.decode("utf-8", "replace")
    if isinstance(query, str):
        value = parse_qs(query.lstrip("?"), keep_blank_values=True).get(key, [])
    else:
        value = query.get(key, "")
    return value[-1] if isinstance(value, (list, tuple)) and value else value


def match(method: str, path: str, query: str | bytes | Mapping[str, Any] = "") -> Rule | None:
    """허용된 메서드·경로·쿼리에 맞는 규칙을 반환한다."""
    for rule in RULES:
        if rule.method != method.upper() or not rule._pattern.fullmatch(path):
            continue
        if rule.query_format and _query_value(query, "format") != rule.query_format:
            continue
        if rule.query_keys is not None:
            try:
                parsed = (parse_qs(query.decode("utf-8", "strict").lstrip("?"), keep_blank_values=True)
                          if isinstance(query, bytes) else
                          parse_qs(query.lstrip("?"), keep_blank_values=True) if isinstance(query, str) else query)
                if (not isinstance(parsed, Mapping) or set(parsed) - set(rule.query_keys)
                        or any(isinstance(value, (list, tuple)) and len(value) != 1 for value in parsed.values())):
                    continue
            except (UnicodeDecodeError, ValueError, TypeError):
                continue
        if rule.json_only and (_query_value(query, "format") or "") not in ("", "json"):
            continue
        return rule
    return None


def check_body(rule: Rule, body: Any) -> str | None:
    """제약 위반 사유를 반환한다. 세부 입력 검증은 로컬 API에 맡긴다."""
    if not rule.constraint:
        return None
    if isinstance(body, (bytes, str)):
        try:
            body = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return "JSON 객체 본문이 필요합니다"
    if not isinstance(body, Mapping):
        return "JSON 객체 본문이 필요합니다"

    if rule.constraint == "session_create":
        source_type = body.get("source_type")
        if not isinstance(source_type, str) or source_type not in {"assembly_caption", "assembly_hls", "manual"}:
            return "이 수집원은 수집 PC 화면에서만 사용할 수 있습니다"
        source_config = body.get("source_config", {})
        if not isinstance(source_config, Mapping) or set(source_config) - {"xcode", "xcgcd"}:
            return "수집원 설정은 xcode·xcgcd만 지정할 수 있습니다"
    elif rule.constraint == "session_patch":
        if set(body) - {"title", "committee", "agencies", "meeting_date", "notes"}:
            return "클라우드에서는 세션의 수집원을 변경할 수 없습니다"
    elif rule.constraint == "manual":
        text = body.get("text")
        if not isinstance(text, str):
            return "수동 입력은 텍스트여야 합니다"
        if len(text) > 2000:
            return "수동 입력은 2,000자 이하로 작성하세요"
    return None
