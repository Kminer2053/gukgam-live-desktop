"""AI 요약 프롬프트·출력 스키마·결과 정규화.

입력 발언은 '[u123 10:04:40 김남근(위원)] 본문' 줄 형식으로 넣고, 출력은 계약서 2.2 + 5장의
BlockSummary / SessionSummary JSON 이다. 모델이 조금 다르게 답해도 화면이 깨지지 않도록
normalize_* 가 형태를 맞춘다(모르는 값은 기본값, 없는 발언 id 는 None).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..serialize import hms

TASK_BLOCK = "# 작업: 질의 블록 요약"
TASK_SESSION = "# 작업: 회의 요약"

ORG_LEVELS = ("high", "medium", "low", "none")
RESPONSE_TYPES = ("사실확인 필요", "자료요구", "책임추궁", "제도개선 요구", "예산", "기타")
URGENCY = ("당일", "종합감사 전", "결과보고서 대응")
COMMIT_STRENGTH = ("확약", "검토", "의례")
REQUEST_STRENGTH = ("자료제출", "서면보고", "조사", "사실확인")

JSON_RETRY_SUFFIX = ("\n\n[재요청] 앞선 응답을 JSON 으로 읽을 수 없었습니다. 설명·코드펜스 없이 "
                     "위 형식의 JSON 객체 하나만 출력하십시오.")

# 블록 하나에 넣을 발언 원문 최대 글자 수(작은 로컬 모델 속도·정확도 고려)
BLOCK_MAX_CHARS = 7000
RECENT_MAX_CHARS = 3000

_LINE_RE = re.compile(r"^\[u(?P<id>\d+)\s+(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<label>[^\]]*)\]\s?(?P<text>.*)$")


@dataclass
class PromptContext:
    """프롬프트에 들어갈 회의·기관 정보."""

    org_name: str = ""
    org_aliases: list[str] = field(default_factory=list)
    head: str = ""                      # 예: 사장 홍길동
    watch_keywords: list[str] = field(default_factory=list)
    title: str = ""
    committee: str = ""
    agencies: str = ""
    meeting_date: str = ""

    @classmethod
    def from_settings(cls, org: dict, watch: list, session: dict | None) -> "PromptContext":
        kws = []
        for w in watch or []:
            kw = (w.get("keyword") if isinstance(w, dict) else str(w) or "").strip()
            if kw:
                kws.append(kw)
        head = " ".join(x for x in (org.get("head_title") or "", org.get("head_name") or "") if x).strip()
        s = session or {}
        return cls(org_name=(org.get("name") or "").strip(),
                   org_aliases=[a.strip() for a in org.get("aliases") or [] if a and a.strip()],
                   head=head, watch_keywords=kws, title=s.get("title") or "", committee=s.get("committee") or "",
                   agencies=s.get("agencies") or "", meeting_date=s.get("meeting_date") or "")

    def org_block(self) -> str:
        if self.org_name:
            org = self.org_name
            if self.org_aliases:
                org += f"(별칭: {', '.join(self.org_aliases)})"
            if self.head:
                org += f", 기관장: {self.head}"
        else:
            org = "(미설정 — 감시 키워드로만 관련도를 판단)"
        kws = ", ".join(self.watch_keywords) if self.watch_keywords else "(없음)"
        return f"우리 기관: {org}\n감시 키워드: {kws}"

    def meeting_line(self) -> str:
        name = self.title or (f"{self.committee} 국정감사" if self.committee else "국정감사")
        extra = []
        if self.agencies:
            extra.append(f"피감기관: {self.agencies}")
        if self.meeting_date:
            extra.append(self.meeting_date)
        return f"[회의] {name}" + (f" ({' / '.join(extra)})" if extra else "")


# ── 공통 원칙 ─────────────────────────────────────────────────────

_PRINCIPLES = """작성 원칙
1. 사실 중심: 누가 무엇을 묻고 어떻게 답했는지만 적는다. 원문에 없는 수치·기관·사실을 지어내지 않는다.
2. 평가 금지: 위원의 성향·태도·공격성·정당 입장을 평가하거나 추측하지 않는다('강하게 질타', '공세', '맹공' 등 금지).
   '~로 판단됨', '~우려됨'처럼 작성자 의견으로 읽히는 표현도 쓰지 않는다. 위원의 주장은 '~라고 지적함', '~라고 주장함'으로 적는다.
3. 문체: 공공기관 보고 개조식. 각 항목은 1문장 60자 내외로, '~함', '~요구', '~지적', '~답변', '~약속', '~필요' 같은 명사형으로 끝낸다.
4. 인용: 근거가 되는 발언의 id 숫자를 utterance_id 로 적는다(예: [u123 …] → 123). 근거가 없으면 null.
5. 입력은 국회 AI자막(음성인식) 원문이다. 오탈자·띄어쓰기 오류가 많고, 위원 발언 한 줄 안에 답변이 섞여 있는 경우가 많다.
   '예', '알겠습니다', '그렇습니다', '맞는 말씀입니다', '~하도록 하겠습니다', '검토하겠습니다', '확인해 보겠습니다'처럼
   질의에 응하는 말은 줄의 화자 표시와 관계없이 답변자(장관·기관장 등)의 말로 보고 answer_points·commitments 에 반영한다.
   단, 위원이 자기 행동을 예고하는 말('제가 확인하겠습니다', '다음 질의 때 점검하겠습니다')과 과거 답변을 인용한 말
   ('작년에 검토하겠다고 하셨는데')은 이번 회의의 기관 약속이 아니므로 commitments 에 넣지 않는다.
6. 음성인식 오류로 보이는 낱말은 문맥상 명백할 때만 바로잡고, 불확실하면 원문 표기를 따른다.
7. 출력은 JSON 객체 하나뿐이다. 설명 문장·코드펜스(```)를 붙이지 않는다."""

_BLOCK_SCHEMA = """{
  "one_line": "블록 한 줄 요약 — 누가 무엇을 질의했는지 50자 내외",
  "question_points": ["질의요지(개조식) 2~4개"],
  "answer_points": ["답변요지(개조식) 0~3개, 답변이 없으면 빈 배열"],
  "issues": [{"text": "지적사항", "utterance_id": 123}],
  "data_requests": [{"text": "요구한 자료·보고 내용", "utterance_id": 123, "strength": "자료제출|서면보고|조사|사실확인"}],
  "commitments": [{"text": "답변자가 약속·검토하기로 한 사항", "utterance_id": 123, "strength": "확약|검토|의례"}],
  "keywords": ["핵심 쟁점어 2~5개(# 없이)"],
  "org_relevance": "high|medium|low|none",
  "org_reason": "우리 기관 관련 판단 근거(무관하면 빈 문자열)",
  "importance": 3,
  "response_type": "사실확인 필요|자료요구|책임추궁|제도개선 요구|예산|기타",
  "urgency": "당일|종합감사 전|결과보고서 대응"
}"""

_BLOCK_RULES = """필드 기준
- org_relevance: high=우리 기관 이름·별칭·기관장을 직접 지목한 질의·요구, medium=우리 기관 이름이 예시 등으로 언급됨,
  low=감시 키워드만 나옴, none=우리 기관 이름·별칭·감시 키워드가 발언에 없음. 소관을 추측해 올리지 않는다.
- importance: 1(참고)~5(즉시 대응). 우리 기관 직접 관련·자료 요구·약속이 있으면 높게.
- issues: 위원이 문제를 지적하거나 개선·조치를 요구한 것.
- data_requests: '자료 달라', '보고해 달라', '조사해 보라', '확인해서 알려 달라(얘기해 달라)'처럼 무언가를 내거나 알려 달라는 요구.
  strength: 자료제출=자료를 내라, 서면보고=결과를 보고하라, 조사=조사·점검하라, 사실확인=확인해서 알려 달라.
- commitments: 답변자가 하겠다고 한 것('검토하겠습니다', '확인해 보겠습니다', '~하도록 하겠습니다', '답변드리겠습니다').
  strength: 확약=하겠다고 분명히 답함, 검토=검토·확인하겠다, 의례='노력하겠습니다'·'고민하겠습니다' 같은 의례적 표현.
- 요구·약속·지적이 여러 건이면 한 발언 안에 있더라도 한 건씩 따로 적고, 각각 그 말이 나온 발언 id 를 utterance_id 로 적는다.
- response_type: 이 질의에 대응하는 데 필요한 일의 성격 하나.
- urgency: 기한 언급이 없으면 '결과보고서 대응'. '종감 때', '종합감사까지'처럼 종합감사를 기한으로 말하면 '종합감사 전'.
  '오늘 중', '즉시', '감사 끝나기 전'이면 '당일'.
- 해당 내용이 없으면 빈 배열. 같은 내용을 여러 칸에 반복하지 않는다."""


def block_system_prompt(ctx: PromptContext, *, reviewed: bool = False) -> str:
    principles = _PRINCIPLES
    if reviewed:
        start, end = principles.index("5. 입력은"), principles.index("6. 음성인식")
        principles = principles[:start] + (
            "5. 입력은 원문 범위와 사람 수정 보호를 검증한 AI 정리 초안이다. 주어진 부분 화자와 경계를 "
            "요약의 기준으로 사용한다. 짧은 '예', 의문형, 0/1 라벨만으로 역할을 다시 바꾸지 않는다. "
            "미상 부분의 실명·기관 약속을 추측하지 않으며 인용된 약속과 위원 자신의 계획을 기관 약속으로 적지 않는다.\n"
        ) + principles[end:]
    return (
        "당신은 국정감사를 받는 기관의 국정감사 대응 담당자를 돕는 기록 보조자입니다. "
        "위원 1인의 질의 시간(질의 블록) 발언 원문을 읽고 보고용 요지를 정리합니다.\n\n"
        f"{ctx.org_block()}\n\n{principles}\n\n{_BLOCK_RULES}\n\n출력 형식(JSON):\n{_BLOCK_SCHEMA}"
    )


_SESSION_SCHEMA = """{
  "overview": "회의 전체 개요 3~5문장. 각 문장은 '~함', '~임', '~됨'으로 끝낸다(예: '국토교통부 등 3개 기관 대상 감사가 진행됨.')",
  "issues": [{"title": "주요 이슈 제목(30자 내외)", "utterance_id": 123, "block_id": 4}],
  "hashtags": ["#쟁점어 3~8개"],
  "next_actions": ["우리 기관이 앞으로 할 일(개조식) 2~5개"]
}"""


def session_system_prompt(ctx: PromptContext, scope: str) -> str:
    what = ("지금까지 진행된 부분(진행 중)의 중간 요약" if scope == "rolling"
            else "회의 전체 요약")
    return (
        "당신은 국정감사를 받는 기관의 국정감사 대응 담당자를 돕는 기록 보조자입니다. "
        f"질의 블록별 요지와 최근 발언을 읽고 {what}을 만듭니다.\n\n"
        f"{ctx.org_block()}\n\n{_PRINCIPLES}\n\n"
        "필드 기준\n"
        "- 답변자는 그날의 피감기관([회의]의 피감기관) 관계자다. 답변자를 우리 기관으로 단정하지 않는다.\n"
        "- overview: '~습니다'체를 쓰지 않는다. 질의 위원 수, 주요 쟁점, 우리 기관 관련 여부를 사실대로 적는다.\n"
        "  '우리 기관 관련도'가 high·medium 인 블록이 없으면 '우리 기관을 직접 다룬 질의는 없었음'이라고 적는다.\n"
        "- issues: 비중 있게 다뤄진 쟁점 3~7개. 서로 다른 위원의 질의를 한 제목으로 합치지 않는다. "
        "block_id 는 입력의 [b숫자], utterance_id 는 근거 발언 [u숫자]의 숫자(모르면 null).\n"
        "- next_actions: 우리 기관이 앞으로 할 일만 '~준비', '~제출', '~확인', '~관리'로 끝나게 적는다"
        "(예: '○○ 현황 자료 제출 준비(○○○ 위원 요구)'). "
        "이미 끝난 답변을 다시 적거나 평가·전략 조언을 쓰지 않는다.\n\n"
        f"출력 형식(JSON):\n{_SESSION_SCHEMA}"
    )


# ── 발언 줄 ───────────────────────────────────────────────────────

def speaker_label(u: dict[str, Any]) -> str:
    role = u.get("speaker_role") or "unknown"
    name = (u.get("speaker") or "").strip()
    if role == "member":
        return f"{name}(위원)" if name else "위원"
    if role == "chair":
        return f"{name}(위원장)" if name and name != "위원장" else "위원장"
    if role == "answer":
        return "답변" if name in ("", "답변") else f"{name}(답변)"
    if role == "system":
        return "기록"
    return name or "미상"


def _squeeze(text: str, limit: int) -> str:
    """긴 발언은 앞 60%·뒤 40% 만 남기고 가운데를 줄인다."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = max(0, limit - head - 7)
    return text[:head].rstrip() + " …(중략)… " + text[len(text) - tail:].lstrip()


def utterance_lines(utts: list[dict[str, Any]], max_chars: int = BLOCK_MAX_CHARS) -> list[str]:
    """발언 → '[u123 10:04:40 김남근(위원)] 본문'. 전체가 길면 긴 발언부터 비례해서 줄인다."""
    rows = [u for u in utts if (u.get("text") or "").strip()]
    total = sum(len(u["text"]) for u in rows) or 1
    lines = []
    for u in rows:
        n = len(u["text"])
        limit = n if total <= max_chars else max(160, int(max_chars * n / total))
        lines.append(f"[u{u['id']} {hms(u['ts_start'])} {speaker_label(u)}] {_squeeze(u['text'], limit)}")
    return lines


def parse_utterance_lines(text: str) -> list[dict[str, Any]]:
    """프롬프트의 발언 줄을 다시 읽는다(규칙 제공자용)."""
    out = []
    for line in (text or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        label = m.group("label").strip()
        role, name = "unknown", label
        lm = re.match(r"^(.*)\((위원|위원장|답변)\)$", label)
        if lm:
            name = lm.group(1)
            role = {"위원": "member", "위원장": "chair", "답변": "answer"}[lm.group(2)]
        elif label == "답변":
            role, name = "answer", "답변"
        elif label == "위원":
            role, name = "member", ""
        elif label == "위원장":
            role, name = "chair", "위원장"
        out.append({"id": int(m.group("id")), "time": m.group("time"), "speaker": name, "speaker_role": role,
                    "text": m.group("text").strip(), "ts_start": 0})
    return out


def block_header(block: dict[str, Any] | None) -> str:
    if not block:
        return "[질의 블록] (미지정)"
    if block.get("kind") == "question":
        who = f"{block['questioner']} 위원" if block.get("questioner") else "성명 미확인 위원"
        if block.get("questioner_party"):
            who += f"({block['questioner_party']})"
        title = f"{who} 질의"
    elif block.get("kind") == "chair":
        title = "위원장 진행"
    else:
        title = "기타 발언"
    return f"[질의 블록 b{block['id']}] {title} {hms(block['ts_start'])}~{hms(block['ts_end'])}"


def block_messages(ctx: PromptContext, block: dict[str, Any] | None, utts: list[dict[str, Any]],
                   *, reviewed: bool = False) -> tuple[str, str]:
    user = "\n".join([TASK_BLOCK, ctx.meeting_line(), block_header(block), "[발언]", *utterance_lines(utts),
                      "", "위 발언을 읽고 지정한 JSON 형식으로만 답하십시오."])
    return block_system_prompt(ctx, reviewed=reviewed), user


def block_digest(block: dict[str, Any], summary: dict[str, Any] | None, max_len: int = 420) -> str:
    """회의 요약 입력용 블록 한 줄 요지."""
    head = f"[b{block['id']} {hms(block['ts_start'])[:5]}~{hms(block['ts_end'])[:5]}]"
    who = f"{block['questioner']} 위원" if block.get("questioner") else "성명 미확인 위원"
    if block.get("questioner_party"):
        who += f"({block['questioner_party']})"
    s = summary or {}
    parts = [f"{head} {who}: {s.get('one_line') or ''}".rstrip()]
    if s.get("question_points"):
        parts.append("질의: " + "; ".join(s["question_points"][:3]))
    if s.get("answer_points"):
        parts.append("답변: " + "; ".join(s["answer_points"][:2]))
    refs = []
    for key, label in (("data_requests", "요구"), ("commitments", "약속"), ("issues", "지적")):
        for r in (s.get(key) or [])[:2]:
            uid = f"(u{r['utterance_id']})" if r.get("utterance_id") else ""
            refs.append(f"{label}: {r.get('text', '')}{uid}")
    if refs:
        parts.append(" / ".join(refs))
    if s.get("org_relevance") and s["org_relevance"] != "none":
        parts.append(f"우리 기관 관련도: {s['org_relevance']}")
    line = " | ".join(p for p in parts if p)
    return line if len(line) <= max_len else line[: max_len - 1] + "…"


def session_messages(ctx: PromptContext, scope: str, digests: list[str],
                     recent: list[dict[str, Any]] | None = None, progress: str = "") -> tuple[str, str]:
    lines = [TASK_SESSION, ctx.meeting_line(), f"[범위] {'진행 중 중간 요약' if scope == 'rolling' else '회의 전체'}"]
    if progress:
        lines.append(f"[진행] {progress}")
    lines.append("[질의 블록별 요지]")
    lines += digests or ["(질의 블록 없음)"]
    if recent:
        lines += ["", "[최근 발언]", *utterance_lines(recent, max_chars=RECENT_MAX_CHARS)]
    lines += ["", "위 내용을 읽고 지정한 JSON 형식으로만 답하십시오."]
    return session_system_prompt(ctx, scope), "\n".join(lines)


def session_utterance_messages(ctx: PromptContext, lines: list[str], coverage_note: str) -> tuple[str, str]:
    """질의 경계가 미분류된 수동 회의 요약은 선택한 저장 발언만 근거로 쓴다."""
    system = (
        "당신은 국정감사 기록 보조자입니다. 저장된 확정 발언의 명시된 입력 범위만 요약합니다.\n\n"
        f"{ctx.org_block()}\n\n{_PRINCIPLES}\n\n"
        "입력 범위 원칙\n"
        "- 질의 블록이 아직 분류되지 않은 입력이다. 질의 블록 0개를 실제 질의가 없었다는 뜻으로 쓰지 않는다.\n"
        "- 실제 질의 유무·위원별 경계·회의 전체 범위와 우리 기관 관련 여부는 미확정이다. "
        "우리 기관 관련 블록이 없다는 이유로 관련 질의가 없었다고 단정하지 않는다.\n"
        "- 화자 미상·역할 미상은 그대로 두고 실명·질의 위원 수·기관 약속을 추정하지 않는다.\n"
        "- 최근 일부 선택 또는 본문 축약이 명시되면 그 범위 밖의 내용을 검토했다고 쓰지 않는다.\n"
        "- overview는 선택된 발언의 주요 내용만 정리하고 issues의 utterance_id는 입력에 있는 번호만 쓴다. "
        "질의 경계가 없으므로 모든 block_id는 null이다. next_actions는 발언에 근거가 있는 조치만 쓴다.\n\n"
        f"출력 형식(JSON):\n{_SESSION_SCHEMA}"
    )
    user = "\n".join([TASK_SESSION, ctx.meeting_line(), f"[입력 범위] {coverage_note}",
                      "[선택한 저장 확정 발언]", *lines, "", "위 범위만 읽고 지정한 JSON 객체로 답하십시오."])
    return system, user


# ── 정규화 ────────────────────────────────────────────────────────

def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def as_id(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        return int(m.group(0)) if m else None
    return None


_REF_TAG = re.compile(r"\s*[\[(]\s*(?:u\d+\s*[,·]?\s*)+[\])]")


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(_text(x) for x in v).strip()
    # 모델이 본문에 붙이는 '[u6]', '(u9, u11)' 인용 표시는 utterance_id 로 따로 두므로 지운다
    return re.sub(r"\s+", " ", _REF_TAG.sub("", str(v))).strip()


def str_list(v: Any, limit: int = 8, max_len: int = 200) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"\n+", v) if x.strip()]
    if not isinstance(v, (list, tuple)):
        v = [v]
    out: list[str] = []
    for x in v:
        if isinstance(x, dict):
            x = x.get("text") or x.get("title") or x.get("내용") or ""
        s = _text(x).lstrip("-•·○■□* ").strip()
        if s and s not in out:
            out.append(s if len(s) <= max_len else s[: max_len - 1] + "…")
        if len(out) >= limit:
            break
    return out


def _pick(value: Any, allowed: tuple[str, ...], default: str, aliases: dict[str, str] | None = None) -> str:
    s = _compact(_text(value)).lower()
    if not s:
        return default
    for a in allowed:
        if _compact(a).lower() == s:
            return a
    for k, a in (aliases or {}).items():
        if k in s:
            return a
    for a in allowed:
        if _compact(a).lower() in s:
            return a
    return default


_ORG_ALIASES = {"높": "high", "상": "high", "직접": "high", "중": "medium", "간접": "medium", "낮": "low", "하": "low",
                "없": "none", "무관": "none"}
_RTYPE_ALIASES = {"사실확인": "사실확인 필요", "확인": "사실확인 필요", "자료": "자료요구", "책임": "책임추궁",
                  "제도": "제도개선 요구", "개선": "제도개선 요구", "예산": "예산", "재정": "예산"}
_URG_ALIASES = {"당일": "당일", "즉시": "당일", "오늘": "당일", "종합": "종합감사 전", "종감": "종합감사 전",
                "결과": "결과보고서 대응", "보고서": "결과보고서 대응"}


def ref_list(v: Any, valid_ids: set[int] | None, strengths: tuple[str, ...] | None = None,
             limit: int = 10) -> list[dict[str, Any]]:
    """[{"text","utterance_id"(,"strength")}] 로 맞춘다. 존재하지 않는 발언 id 는 None."""
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        v = [v]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for x in v:
        if isinstance(x, dict):
            text = _text(x.get("text") or x.get("내용") or x.get("summary") or x.get("title"))
            uid = as_id(x.get("utterance_id", x.get("uid", x.get("id"))))
            strength = _text(x.get("strength") or x.get("강도"))
        else:
            text, uid, strength = _text(x), None, ""
        text = text.lstrip("-•·○■□* ").strip()
        if not text or _compact(text) in seen:
            continue
        seen.add(_compact(text))
        if valid_ids is not None and uid not in valid_ids:
            uid = None
        item: dict[str, Any] = {"text": text[:220], "utterance_id": uid}
        if strengths:
            st = _pick(strength, strengths, "")
            if st:
                item["strength"] = st
        out.append(item)
        if len(out) >= limit:
            break
    return out


def normalize_block_summary(data: Any, valid_ids: set[int] | None) -> dict[str, Any]:
    """모델 출력 → BlockSummary. 요지가 하나도 없으면 ValueError(호출자가 규칙 대체)."""
    if not isinstance(data, dict):
        raise ValueError("JSON 객체가 아님")
    one_line = _text(data.get("one_line") or data.get("summary") or data.get("한줄요약"))
    q_points = str_list(data.get("question_points") or data.get("질의요지"), limit=6)
    if not one_line and not q_points:
        raise ValueError("요약 필드(one_line/question_points)가 비어 있음")
    if not one_line:
        one_line = q_points[0]
    try:
        importance = int(float(data.get("importance") or 0))
    except (TypeError, ValueError):
        importance = 0
    importance = min(5, max(1, importance or 2))
    keywords = [k.lstrip("#").strip() for k in str_list(data.get("keywords") or data.get("hashtags"), limit=6,
                                                        max_len=30)]
    return {
        "one_line": one_line[:160],
        "question_points": q_points,
        "answer_points": str_list(data.get("answer_points") or data.get("답변요지"), limit=5),
        "issues": ref_list(data.get("issues"), valid_ids),
        "data_requests": ref_list(data.get("data_requests"), valid_ids, REQUEST_STRENGTH),
        "commitments": ref_list(data.get("commitments"), valid_ids, COMMIT_STRENGTH),
        "keywords": [k for k in keywords if k],
        "org_relevance": _pick(data.get("org_relevance"), ORG_LEVELS, "none", _ORG_ALIASES),
        "org_reason": _text(data.get("org_reason"))[:200],
        "importance": importance,
        "response_type": _pick(data.get("response_type"), RESPONSE_TYPES, "기타", _RTYPE_ALIASES),
        "urgency": _pick(data.get("urgency"), URGENCY, "결과보고서 대응", _URG_ALIASES),
    }


# 사람이 고칠 수 있는 블록 요약 필드(계약 C5)
EDITABLE_FIELDS = ("one_line", "question_points", "answer_points", "issues", "data_requests", "commitments",
                   "keywords", "response_type", "urgency", "org_relevance", "org_reason")
_REF_STRENGTHS = {"data_requests": REQUEST_STRENGTH, "commitments": COMMIT_STRENGTH, "issues": None}


def _edit_refs(v: Any, valid_ids: set[int] | None, strengths: tuple[str, ...] | None,
               current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """사람이 고친 지적·요구·약속 목록. 줄(문자열)만 와도 기존 항목과 문구가 같으면 근거 발언·강도를 이어받는다."""
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"\n+", v) if x.strip()]
    if not isinstance(v, (list, tuple)):
        raise ValueError("목록 형식이어야 합니다")
    prev = {_compact(_text(r.get("text"))): r for r in current if isinstance(r, dict) and r.get("text")}
    rows: list[Any] = []
    for x in v:
        if isinstance(x, str):
            text = _text(x).lstrip("-•·○■□* ").strip()
            old = prev.get(_compact(text))
            x = {"text": text, **({"utterance_id": old.get("utterance_id"), "strength": old.get("strength") or ""}
                                  if old else {})}
        elif not isinstance(x, dict):
            raise ValueError("목록의 각 줄은 문자열 또는 {text, utterance_id, strength} 여야 합니다")
        rows.append(x)
    out = ref_list(rows, valid_ids, strengths, limit=12)
    for r in out:
        old = prev.get(_compact(r["text"]))
        if old and old.get("origin"):
            r["origin"] = old["origin"]
    return out


def normalize_summary_patch(patch: Any, valid_ids: set[int] | None,
                            current: dict[str, Any] | None = None) -> dict[str, Any]:
    """사람이 고친 블록 요약 필드 → 정규화된 값(허용 키만·길이 제한). 키·값이 잘못되면 ValueError."""
    if not isinstance(patch, dict):
        raise ValueError("고칠 내용이 없습니다")
    patch = {k: v for k, v in patch.items() if v is not None}     # 보내지 않은(null) 필드는 그대로 둔다
    if not patch:
        raise ValueError("고칠 내용이 없습니다")
    unknown = sorted(k for k in patch if k not in EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"고칠 수 없는 항목입니다: {', '.join(unknown)}")
    cur = current or {}
    out: dict[str, Any] = {}
    enums = {"response_type": RESPONSE_TYPES, "urgency": URGENCY, "org_relevance": ORG_LEVELS}
    for k, v in patch.items():
        if k == "one_line":
            s = _text(v)
            if not s:
                raise ValueError("한 줄 요약은 비울 수 없습니다")
            out[k] = s[:160]
        elif k in ("question_points", "answer_points"):
            if v is not None and not isinstance(v, (str, list, tuple)):
                raise ValueError(f"{k} 는 목록이어야 합니다")
            out[k] = str_list(v, limit=8, max_len=200)
        elif k == "keywords":
            if v is not None and not isinstance(v, (str, list, tuple)):
                raise ValueError("keywords 는 목록이어야 합니다")
            if isinstance(v, str):
                v = [x for x in re.split(r"[\s,]+", v) if x.strip()]
            kws = [x.lstrip("#").strip() for x in str_list(v, limit=8, max_len=30)]
            out[k] = [x for x in kws if x]
        elif k in _REF_STRENGTHS:
            out[k] = _edit_refs(v, valid_ids, _REF_STRENGTHS[k], cur.get(k) or [])
        elif k == "org_reason":
            out[k] = _text(v)[:200]
        else:
            s = _text(v)
            if s not in enums[k]:
                raise ValueError(f"{k} 값은 {', '.join(enums[k])} 중 하나여야 합니다")
            out[k] = s
    return out


def normalize_session_summary(data: Any, valid_blocks: set[int] | None,
                              valid_utts: set[int] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("JSON 객체가 아님")
    overview = _text(data.get("overview") or data.get("summary") or data.get("개요"))
    if not overview:
        raise ValueError("overview 가 비어 있음")
    issues = []
    raw = data.get("issues") or []
    for x in raw if isinstance(raw, list) else [raw]:
        if isinstance(x, dict):
            title = _text(x.get("title") or x.get("text"))
            uid, bid = as_id(x.get("utterance_id")), as_id(x.get("block_id"))
        else:
            title, uid, bid = _text(x), None, None
        if not title:
            continue
        if valid_utts is not None and uid not in valid_utts:
            uid = None
        if valid_blocks is not None and bid not in valid_blocks:
            bid = None
        issues.append({"title": title[:120], "utterance_id": uid, "block_id": bid})
        if len(issues) >= 10:
            break
    tags = []
    for t in str_list(data.get("hashtags") or data.get("keywords"), limit=10, max_len=30):
        t = "#" + re.sub(r"\s+", "", t.lstrip("#"))
        if len(t) > 1 and t not in tags:
            tags.append(t)
    return {
        "overview": overview[:1200],
        "issues": issues,
        "hashtags": tags,
        "org_points": str_list(data.get("org_points"), limit=8),
        "next_actions": str_list(data.get("next_actions"), limit=8),
    }
