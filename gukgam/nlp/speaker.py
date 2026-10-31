"""화자·질의 블록 추정(규칙 기반 상태기계).

국정감사 진행 관례를 이용한다.
  • 위원장이 "다음은 ○○○ 위원님 질의해 주시기 바랍니다"라고 호명 → 다음 발언부터 ○○○ 위원 질의 블록
  • 호명에 이름이 빠지면(또는 STT 가 놓치면) → '성명 확인 필요' 블록
  • 위원 자기소개("서울 성북을의 김남근 위원입니다") → 블록 질의자 보정
  • 블록 안에서 짧은 "예 알겠습니다/검토하겠습니다"류 → 답변
  • 수집원(국회 AI자막 등)이 화자명·화자전환을 주면 그것을 우선한다
사람이 교정한 값(speaker_source='manual', block.status='confirmed')은 덮어쓰지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..db import now_ms
from ..repo import Repo
from .floor import (call_fragment_order, call_tail_requires_review, completed_closing_call, examination_transition, explicit_chair_declarations, is_question_intro_prefix, is_reported_anchor,
                    new_floor_owner_conflict, reported_call_context, reported_examination, scan_anchors, split_call_fragment)
from .role_evidence import has_direct_question, role_evidence, weak_response_spans
from .role_binding import matches_source, revision_fingerprint, source_ref
from .text import PARTIES, edit_distance, is_valid_name, norm

_PARTY_RE = "(?:" + "|".join(PARTIES) + r")\s*(?:의\s*)?"
_NAME = r"(?P<name>[가-힣]{2,4})"

# 위원장 호명(다음 질의자 지정)
ANNOUNCE_PATTERNS = [
    re.compile(
        r"(?:(?:다음(?:은|으로)?|이어서|그다음(?:은)?|계속해서)\s*)?(?:" + _PARTY_RE + r")?" + _NAME +
        r"\s*위원(?:님)?\s*(?:께서\s*)?(?:의\s*)?(?:보충\s*|추가\s*|마지막\s*)?(?:질의|질문|발언)\s*"
        r"(?:해\s*주(?:시기\s*바랍니다|십시오|세요|시겠습니까|시겠습니다)|하(?:시겠습니다|십시오|여\s*주십시오|시죠|시기\s*바랍니다)|"
        r"(?:를|을)\s*(?:시작|계속)|순서|이어)"
    ),
    re.compile(_NAME + r"\s*위원(?:님)?\s*(?:차례|순서)(?:입니다)?"),
    # 화자 라벨이 바뀌며 발언 끝에서 잘린 호명('다음은 정희용 위원님 질의해 주시기' / '바랍니다 …', 2026-10-08 실측)
    re.compile(
        r"(?:다음(?:은|으로)?|이어서|그다음(?:은)?)\s*(?:" + _PARTY_RE + r")?" + _NAME +
        r"\s*위원(?:님)?\s*(?:께서\s*)?(?:보충\s*|추가\s*)?(?:질의|질문)(?:\s*해)?(?:\s*주(?:시기|십|시|세)?)?\s*$"
    ),
]
# 이름 없는 호명
ANNOUNCE_NONAME = re.compile(
    r"다음(?:은|으로)?\s*(?:위원님\s*)?(?:보충\s*|추가\s*)?(?:질의|질문)\s*(?:해\s*주(?:시기\s*바랍니다|십시오|세요)|하시겠습니다)"
)
# 질의 종료·회의 진행 표지(질의 블록을 닫는다)
CLOSING_PATTERNS = re.compile(
    r"(?:속개|개의|개회)(?:를|을)?\s*선포|선포합니다|속개하겠습니다|감사를\s*(?:시작|개시|실시)하겠"
)
# 위원장 발언으로 볼 강한 단서(짧은 발언에서만)
CHAIR_CUES = re.compile(
    r"^수고(?:하셨습니다|많으셨습니다)|질의해\s*주시기\s*바랍니다|피감기관\s*관계자|서면질의에\s*대한\s*답변서|"
    r"회의록에\s*게재|의사일정\s*제?\s*\d+\s*항|상정합니다|간사\s*(?:간|위원)\s*(?:협의|합의)|"
    r"위원님들께서는|발언\s*시간|(?:답변|질의)\s*시간(?:을|은|이)|마이크(?:를|가)|증인\s*선서|선서를\s*하겠|"
    r"(?:오전|오후)\s*\d+\s*시(?:\s*\d+\s*분)?\s*(?:까지|에)\s*(?:정회|속개)"
)
# 짧은 답변 단서
ANSWER_START = re.compile(
    r"^(?:(?:예|네|아\s*예)(?=\s|[.!?。]|$)|그렇습니다|알겠습니다|맞습니다|맞는\s*말씀|"
    r"위원님(?:\s*말씀|이|께서)|저희(?:가|는)|검토|답변|지적하신|말씀하신|제가\s*(?:알기로는|보고받기로는))"
)
ANSWER_END = re.compile(r"(?:겠습니다|드리겠습니다|그렇습니다|맞습니다|않습니다)$")
INDEPENDENT_ANSWER = re.compile(
    r"^(?:예|네|아\s*예|아니요|맞습니다|그렇습니다|아닙니다|알겠습니다|알겠어요|그럼요|"
    r"감사합니다|있습니다|없습니다|알고\s*있습니다)[.!?。]*$"
)
ACK_START = re.compile(r"^(?:예|네|아\s*예)(?=\s|[.!?。]|$)")
# 위원의 진행 표현('계속 질의하겠습니다', '한 가지 더 여쭤보겠습니다') — '~겠습니다' 로 끝나도 답변이 아니다
MEMBER_PROGRESS_END = re.compile(
    r"(?:(?:질의|질문)(?:를|을)?\s*(?:계속\s*|좀\s*|더\s*)?(?:하|드리|이어\s*가)(?:도록\s*하)?겠습니다|"
    r"(?:여쭤|여쭈어|여쭙)\S*\s*(?:보|드리)?겠습니다|묻겠습니다)$"
)
SELF_INTRO = re.compile(r"(?:의\s*)?" + _NAME + r"\s*위원입니다")
ADDRESS_HEAD = re.compile(r"^(?:\S+\s+){0,2}(?:장관|차관|사장|원장|청장|이사장|처장|총장|본부장|회장|실장|국장|직무대행|권한대행)님")

# 수집원 화자 힌트 해석: "강득구 위원", "유의동 위원장", "홍지선 국토교통부장관", "답변"
HINT_ROLE = [
    (re.compile(r"위원장(?:\s*대리)?$"), "chair"),
    (re.compile(r"위원$"), "member"),
    (re.compile(r"(?:의원)$"), "member"),
    (re.compile(r"^답변"), "answer"),
    (re.compile(r"(?:장관|차관|사장|원장|청장|이사장|처장|총장|본부장|회장|실장|국장|대표|증인|참고인|부장)$"), "answer"),
]

# 한 자막 줄 안의 위원장 짧은 호명("수고하셨습니다 김은혜 위원님 …") — 화자 전환 표지가 없을 때만 쓴다
HANDOVER = re.compile(
    r"(?:수고하셨습니다|수고\s*많으셨습니다|알겠습니다|고맙습니다|감사합니다)\s+" + _NAME +
    r"\s*위원님\s+(?!(?:말씀|지적|제기|질의하|질문하|요청|요구하|주장|얘기|언급|의견|께서|이\s|의\s))"
)
ESTIMATED_PREFIX = "추정:"
MIN_HANDOVER_TAIL = 30       # 호명 뒤 이어지는 발언이 이보다 짧으면 분할하지 않는다

MAX_ANSWER_LEN = 90          # 이보다 짧고 답변 단서가 있으면 답변으로 본다
MAX_CHAIR_CUE_LEN = 260      # 위원장 단서 판정을 적용할 최대 길이
# 수집 중단으로 블록이 닫힌 뒤(종료·구버전 일시정지) 이 시간 안에 같은 위원 발언이 이어지면 직전 질의 블록을 다시 연다
RESUME_MAX_GAP_MS = 30 * 60 * 1000
ROLE_CONTEXT_MAX_GAP_MS = 90 * 1000  # 긴 무수신 뒤에는 직전 문답·색 라벨을 새 근거로 쓰지 않는다.


@dataclass
class Announcement:
    name: str | None
    party: str = ""


@dataclass
class Decision:
    """한 발언에 대한 추정 결과(파이프라인이 DB 에 반영)."""

    speaker: str
    role: str
    source: str                 # feed|inferred
    block_id: int
    block_changed: list[int] = field(default_factory=list)   # 생성·갱신된 블록 id
    closed_blocks: list[int] = field(default_factory=list)   # 이번에 닫힌 질의 블록(요약 트리거)
    relabeled_utterances: list[int] = field(default_factory=list)
    updated_items: list[int] = field(default_factory=list)   # 질의자 확정으로 요구 위원·발언자가 갱신된 항목(item.upsert 대상)
    review_required: bool = False
    review_reason: str = ""


@dataclass
class RosterEntry:
    name: str
    role: str
    party: str
    title: str
    aliases: list[str]


class Roster:
    def __init__(self, rows: list[dict]):
        self.entries = [RosterEntry(r["name"], r.get("role") or "member", r.get("party") or "",
                                    r.get("title") or "", list(r.get("aliases") or [])) for r in rows]

    @property
    def chair(self) -> RosterEntry | None:
        return next((e for e in self.entries if e.role == "chair"), None)

    def match(self, name: str | None, roles: tuple[str, ...] = ("member", "chair")) -> tuple[RosterEntry | None, bool]:
        """(명단 항목, 퍼지 여부). STT 오인식 1글자까지 허용."""
        if not name:
            return None, False
        cands = [e for e in self.entries if e.role in roles] or []
        for e in cands:
            if name == e.name or name in e.aliases:
                return e, False
        best, best_d = None, 99
        tied = False
        for e in cands:
            if len(e.name) != len(name):
                continue
            d = edit_distance(name, e.name)
            if d < best_d:
                best, best_d = e, d
                tied = False
            elif d == best_d:
                tied = True
        if best is not None and best_d <= 1 and not tied:
            return best, True
        return None, False

    def __bool__(self) -> bool:
        return bool(self.entries)


def split_handover(text: str, member_names: set[str]) -> list[tuple[str, str | None]]:
    """위원장이 같은 줄 안에서 다음 위원을 호명한 지점에서 자른다.

    [(앞부분, None), (뒷부분, '추정:이름 위원')] 또는 자를 곳이 없으면 [(text, None)].
    오탐을 막기 위해 명단에 있는 위원 이름일 때만, 뒤따르는 발언이 충분히 길 때만 자른다.
    """
    text = norm(text)
    if not member_names:
        return [(text, None)]
    # 완결 호명과 명단에 있는 자기소개가 붙었을 때만 진행→질의로 분리한다. 인용문은 대상이 아니다.
    for m in ANNOUNCE_PATTERNS[0].finditer(text):
        name = m.group("name")
        head, tail = text[:m.end()].strip(), text[m.end():].strip()
        if (name not in member_names or not tail or not re.match(
                r"^(?:다음|이어서|그다음|계속해서|수고|감사|고맙|알겠습니다)", text)):
            continue
        if re.match(r"^(?:라고|라는|하고|하며)", tail):
            continue
        intro = re.search(re.escape(name) + r"\s*(?:위원)?입니다(?:\s|[.!?]|$)", tail[:100])
        if intro:
            return [(head, f"{ESTIMATED_PREFIX}위원장"), (tail, f"{ESTIMATED_PREFIX}{name} 위원")]
    # 앞 segment에서 잘린 호명 종결부가 자기소개와 같은 줄로 온 경우.
    leading = re.match(r"^(?:주시기\s*)?바랍니다(?:[.!?])?\s+", text)
    if leading:
        tail = text[leading.end():].strip()
        for name in sorted(member_names):
            if re.search(re.escape(name) + r"\s*(?:위원)?입니다(?:\s|[.!?]|$)", tail[:100]):
                return [(text[:leading.end()].strip(), f"{ESTIMATED_PREFIX}위원장"),
                        (tail, f"{ESTIMATED_PREFIX}{name} 위원")]
    for m in HANDOVER.finditer(text):
        name = m.group("name")
        if name not in member_names:
            continue
        head, tail = text[:m.end()].strip(), text[m.end():].strip()
        if len(tail) < MIN_HANDOVER_TAIL or not head:
            continue
        rest = split_handover(tail, member_names)
        rest[0] = (rest[0][0], f"{ESTIMATED_PREFIX}{name} 위원")
        return [(head, None)] + rest
    return [(text, None)]


def parse_hint(hint: str | None) -> tuple[str | None, str | None]:
    """수집원 화자 힌트 → (이름, 역할). 'hwa:0' 같은 기계 힌트는 (None, None).
    '추정:' 접두는 파이프라인이 본문에서 추정한 힌트다(수집원 힌트보다 약함)."""
    if not hint or hint.startswith("hwa:"):
        return None, None
    hint = norm(hint)
    if hint.startswith(ESTIMATED_PREFIX):
        hint = hint[len(ESTIMATED_PREFIX):].strip()
    role = None
    for pat, r in HINT_ROLE:
        if pat.search(hint):
            role = r
            break
    name = None
    m = re.match(r"([가-힣]{2,4})\s+(?:위원장|위원|의원|.+)$", hint)
    if m and is_valid_name(m.group(1)):
        name = m.group(1)
    elif re.fullmatch(r"[가-힣]{2,4}", hint) and is_valid_name(hint):
        name = hint
    return name, role


def _hwa_label(hint: str | None) -> str | None:
    """'hwa:1' → '1'. 미정(-1)·없음은 None."""
    if not hint or not hint.startswith("hwa:"):
        return None
    v = hint[4:].strip()
    return None if v in ("", "-1") else v


def find_announcement(text: str) -> Announcement | None:
    text = norm(text)
    best: tuple[int, Announcement] | None = None
    for pat in ANNOUNCE_PATTERNS:
        for m in pat.finditer(text):
            if is_reported_anchor(text, m.start(), m.end()):
                continue
            name = m.group("name")
            if not is_valid_name(name):
                name = None
            party = ""
            pm = re.search(_PARTY_RE + r"$", text[max(0, m.start() - 12):m.start("name") if name else m.start()])
            if pm:
                party = next((p for p in PARTIES if p in pm.group(0)), "")
            ann = Announcement(name=name, party=party)
            # 같은 발언에 호명이 여럿이면 마지막 것이 '다음 질의자'
            if best is None or m.start() >= best[0]:
                best = (m.start(), ann)
    if best:
        return best[1]
    if any(not is_reported_anchor(text, m.start(), m.end()) for m in ANNOUNCE_NONAME.finditer(text)):
        return Announcement(name=None)
    return None


def is_chair_text(text: str) -> bool:
    text = norm(text)
    if find_announcement(text) or explicit_chair_declarations(text) or _direct_cue(CLOSING_PATTERNS, text):
        return True
    return len(text) <= MAX_CHAIR_CUE_LEN and _direct_cue(CHAIR_CUES, text)


def _direct_cue(pattern: re.Pattern, text: str) -> bool:
    """인용·회고·문구 속 진행 표현은 현재 발언권으로 읽지 않는다."""
    return any(not is_reported_anchor(text, m.start(), m.end()) for m in pattern.finditer(text))


def _direct_intro(text: str, limit: int = 100, known_names: set[str] | None = None) -> re.Match | None:
    """보고서의 이름이나 인용된 자기소개를 지금 말하는 사람으로 정하지 않는다."""
    for match in SELF_INTRO.finditer(text[:limit]):
        before = text[:match.start()].strip()
        region_prefix = text[match.start():match.start("name")].strip() == "의"
        direct = (not before or region_prefix or bool(re.search(r"(?:저는|제가|안녕하세요|의|바랍니다|입니다)\s*$", before))
                  or any(before.endswith(party) for party in PARTIES)
                  or (match["name"] in (known_names or set()) and is_question_intro_prefix(before)))
        if direct and not is_reported_anchor(text, match.start(), match.end()):
            return match
    return None


# 답변자가 말을 시작하는 전형적 표현(발언 첫머리) — 화자 라벨 대응이 뒤바뀌었을 때 다시 맞추는 근거
ANSWER_LEAD = re.compile(
    r"^(?:(?:예|네|아\s*예)\s*)?(?:저희(?:가|는|도)\s|위원님\s*(?:말씀|께서|지적)|말씀(?:을)?\s*드리|그\s*부분은\s|"
    r"제가\s*(?:알기로는|보고받기로는|파악하기로는))"
)

# 요청·질문으로 끝나는 말은 위원 쪽이다('확인하시고 … 제출해 주십시오', '… 마련됐습니까')
MEMBER_REQUEST_END = re.compile(r"(?:주십시오|주세요|주시기\s*바랍니다|하십시오|하세요|습니까|십니까|입니까|나요|겠어요)\s*$")
_REVERSE_QUESTION_START = re.compile(
    r"^(?:(?:예|네)\s*[,，.!?。]?\s*){0,2}위원님(?:께)?\s*[,，]?\s*"
    r"(?:역으로|반대로)(?=$|[\s,.!?。])"
)


def is_answer_text(text: str) -> bool:
    text = norm(text)
    if len(text) > MAX_ANSWER_LEN:
        return False
    if ADDRESS_HEAD.search(text) or MEMBER_PROGRESS_END.search(text) or MEMBER_REQUEST_END.search(text):
        return False
    return (bool(INDEPENDENT_ANSWER.fullmatch(text)) or bool(ANSWER_START.search(text))
            or (len(text) <= 40 and bool(ANSWER_END.search(text))))


def has_explicit_turn_start(text: str) -> bool:
    """앞의 미완성 구절을 흡수하지 않아야 할 명시적인 발언 시작."""
    text = norm(text)
    return bool(is_chair_text(text) or SELF_INTRO.search(text[:100]) or ADDRESS_HEAD.search(text)
                or INDEPENDENT_ANSWER.fullmatch(text) or ACK_START.search(text) or ANSWER_LEAD.search(text))


def has_explicit_turn_cue(text: str) -> bool:
    """불완전 구절과 달리 따로 남겨야 할 호명·자기소개·질문·독립 응답."""
    text = norm(text)
    return bool(has_explicit_turn_start(text) or MEMBER_REQUEST_END.search(text) or MEMBER_PROGRESS_END.search(text))


def is_incomplete_fragment(text: str) -> bool:
    """반복 라벨 분할을 의심할 구절 끝. 문장 연속성만으로 화자를 합치지는 않는다."""
    text = norm(text).rstrip(".,!?。")
    return bool(re.search(r"(?:[은는이가을를의와과에]|라고|하고|하며|으며|면서|는데|지만|니까|그래서|"
                          r"그러면|그러니까|아직|지금|내일|이지)$", text))


def _source_fragment_key(row: dict) -> str | None:
    """실제 자막 키에서 색상·부분 분할 전 원 segment만 구분한다."""
    match = re.fullmatch(r"(?P<source>t?s\d+(?:\.\d+)?)~\d+#\d+", str(row.get("source_key") or ""))
    return match["source"] if match else None


def _source_linking_tail(text: str) -> bool:
    """같은 원 segment에서만 미완성 연결 어미·보조사 꼬리를 확인한다."""
    text = norm(text).rstrip(" ,.!?。")
    return is_incomplete_fragment(text) or bool(re.search(r"(?:으면|다면|라면|하면|하며|조차|마저)$", text))


def needs_answer_question_review(row: dict, block: dict | None = None) -> bool:
    """역할 근거 없는 답변 안의 직접 질문은 역할을 바꾸지 않고 검토만 요청한다."""
    if (row.get("speaker_role") != "answer" or parse_hint(row.get("speaker_hint"))[1]
            or row.get("review_parts") or BlockTracker._protected(row, block)):
        return False
    text = norm(row.get("text") or "")
    evidence = role_evidence(text)
    if (evidence["role"] is not None or evidence["mixed"] or ANSWER_LEAD.search(text)
            or re.match(r"^(?:(?:예|네|우선|먼저|아니)\s*){0,3}위원님(?:께)?(?=$|\s)", text)):
        return False
    # 뒤에 붙은 가정·기사·대본 예고로 앞선 실제 질문까지 숨기지 않는다.
    reported = re.search(r"(?:만약|가령|가정(?:해\s*보|하면|한다면)|예를\s*들|신문\s*기사|"
                         r"기사(?:에는|에서|에\s*따르면)|인용문|대본|자문자답|문구\s*[:：])", text)
    direct_text = text[:reported.start()] if reported else text
    return has_direct_question(direct_text)


class BlockTracker:
    """세션 하나의 화자·블록 상태. 상태는 settings(tracker:<sid>)에 저장해 재기동 후에도 이어간다."""

    def _block_record(self, bid):
        """수집 판정은 원자료만 읽고 보정 표시의 전체 검증을 역호출하지 않는다."""
        read = getattr(self.repo, "stage_block_record", None)
        return read(self.sid, bid) if read is not None else self.repo.get_block(bid)

    def __init__(self, repo: Repo, session_id: str):
        self.repo = repo
        self.sid = session_id
        self.roster = Roster(repo.get_roster(session_id))
        st = repo.get_setting(self._key, {}) or {}
        self.pending: Announcement | None = (
            Announcement(st["pending"].get("name"), st["pending"].get("party", "")) if st.get("pending") else None
        )
        self.split_call: dict = dict(st.get("split_call") or {})
        self.last_role: str | None = st.get("last_role")
        # 현재 질의 블록에서 질의 위원에게 붙은 수집원 화자 라벨(국회 자막 hwa '0'/'1') — 같은 라벨=위원, 다른 라벨=답변
        self.member_hwa: str | None = st.get("member_hwa")
        self.answer_hwa: str | None = st.get("answer_hwa")
        self.hwa_untrusted: bool = bool(st.get("hwa_untrusted"))
        self.role_votes: list[dict] = [dict(vote) for vote in st.get("role_votes", [])
                                      if isinstance(vote, dict)][-4:]
        self.role_binding: dict = dict(st.get("role_binding") or {})
        self._legacy_binding = bool(self.member_hwa is not None and not self.role_binding)
        self.role_learning_uncertain = bool(st.get("role_learning_uncertain"))
        self.last_question: bool = bool(st.get("last_question"))
        self.last_weak_split: bool = bool(st.get("last_weak_split", self.last_role == "unknown"))
        self.role_context_ts: int = int(st.get("role_context_ts") or 0)
        self.local_turn: dict = dict(st.get("local_turn") or {})
        self.chair_name: str = st.get("chair_name") or (self.roster.chair.name if self.roster.chair else "")
        # close_all 직전의 질의 블록·호명 대기(재개 첫 발언이 같은 위원 질의를 이어 가도록)
        self.resume: dict | None = st.get("resume") or None
        self.chair_context: bool = bool(st.get("chair_context"))
        self.chair_context_ts: int = int(st.get("chair_context_ts") or 0)
        self.question_role: str = st.get("question_role") or "member"

    @property
    def _key(self) -> str:
        return f"tracker:{self.sid}"

    def reload_roster(self) -> None:
        self.roster = Roster(self.repo.get_roster(self.sid))
        if self.roster.chair:
            self.chair_name = self.roster.chair.name

    def _save(self) -> None:
        self.repo.set_setting(self._key, {
            "pending": ({"name": self.pending.name, "party": self.pending.party} if self.pending else None),
            "split_call": self.split_call,
            "last_role": self.last_role,
            "member_hwa": self.member_hwa,
            "answer_hwa": self.answer_hwa,
            "hwa_untrusted": self.hwa_untrusted,
            "role_votes": self.role_votes,
            "role_binding": self.role_binding,
            "role_learning_uncertain": self.role_learning_uncertain,
            "last_question": self.last_question,
            "last_weak_split": self.last_weak_split,
            "role_context_ts": self.role_context_ts,
            "local_turn": self.local_turn,
            "chair_name": self.chair_name,
            "resume": self.resume,
            "chair_context": self.chair_context,
            "chair_context_ts": self.chair_context_ts,
            "question_role": self.question_role,
        })

    def _floor_anchors(self, text: str, hint_role: str | None, block: dict | None, *, allow_context: bool = True,
                       ts: int | None = None, defer_bare: bool = False, current: dict | None = None) -> list[dict]:
        """짧은 호명·자기 질의는 실제 진행 근거가 있는 경우에만 쓴다."""
        rows = [{"name": entry.name, "role": entry.role} for entry in self.roster.entries]
        recent = ts is None or 0 <= ts - self.chair_context_ts <= RESUME_MAX_GAP_MS
        context = hint_role == "chair" or (allow_context and recent and self.chair_context)
        pending = self.pending is not None
        anchors = []
        from .floor import closed_short_calls
        short = closed_short_calls(text, rows)
        candidates = [anchor for anchor in scan_anchors(text, rows) if not any(
            anchor["kind"] == "call" and anchor["speaker"] == call["speaker"]
            and anchor["end"] == call["end"] for call in short)] + short
        for anchor in sorted(candidates, key=lambda a: (a["start"], a["end"])):
            if anchor.get("owner_guard") and current and new_floor_owner_conflict(current):
                continue
            kind = anchor["kind"]
            if kind not in {"call", "chair_return", "chair_question", "chair_procedure"}:
                continue
            if defer_bare and kind == "call" and anchor.get("requires_context"):
                continue
            if anchor.get("requires_context") and (not context or (kind == "chair_question" and pending)):
                continue
            anchors.append(anchor)
            context = True
            if kind == "call":
                pending = True
            elif kind in {"chair_return", "chair_procedure"}:
                pending = False
        return anchors

    def _split_rows(self, u: dict, *, final_only: bool = True) -> tuple[list[dict], int]:
        """원문 순서의 인접 조각만 읽는다. 파생 역할은 호명 근거로 쓰지 않는다."""
        if not u.get("id") or not self.repo.get_utterance(u["id"]):
            return [], -1
        rows = [u]
        for _ in range(8):
            previous, _next = self.repo.neighbor_utterances(rows[-1]["id"], final_only=final_only)
            if previous is None:
                break
            rows.append(previous)
        rows.reverse()
        return rows, len(rows) - 1

    def _call_piece_ok(self, row: dict) -> bool:
        block = self._block_record(row.get("block_id")) if row.get("block_id") else None
        _, role = parse_hint(row.get("speaker_hint"))
        explicit_other = role in {"member", "answer"} and not (row.get("speaker_hint") or "").startswith(ESTIMATED_PREFIX)
        return not explicit_other and not self._protected(row, block)

    @staticmethod
    def _short_call_step(left: dict, right: dict) -> bool:
        gap = right["ts_start"] - (left.get("ts_end") or left["ts_start"])
        return bool(call_fragment_order(left) < call_fragment_order(right) and -3000 <= gap <= 5000
                    and 0 <= right["ts_start"] - left["ts_start"] <= 20_000)

    def _split_call_candidate(self, u: dict, block: dict | None, *, revised: bool = False) -> dict | None:
        """명단·인접 순서·짧은 시각·인용·사람 보호를 통과한 완성 호명만 복원한다."""
        if not block or not self._call_piece_ok(u):
            return None
        roster = [{"name": person.name, "role": person.role} for person in self.roster.entries]
        finish = split_call_fragment(norm(u.get("text") or ""), roster)
        if not finish or finish["kind"] != "finish":
            return None
        rows, index = self._split_rows(u)
        if index < 2:
            return None
        lead, name = rows[index - 2:index]
        a, b = (split_call_fragment(norm(row.get("text") or ""), roster) for row in (lead, name))
        if not a or a["kind"] != "lead" or not b or b["kind"] != "name":
            return None
        if (lead.get("block_id") != name.get("block_id") or not lead.get("block_id")
                or (not revised and lead["block_id"] != block["id"])
                or any(not self._call_piece_ok(row) for row in (lead, name))
                or not self._short_call_step(lead, name) or not self._short_call_step(name, u)
                or u["ts_start"] - lead["ts_start"] > 20_000):
            return None
        context = " ".join(norm(row.get("text") or "") for row in rows[max(0, index - 5):index - 2])
        if context and reported_call_context(context):
            return None
        texts = [norm(row.get("text") or "") for row in (lead, name, u)]
        joined = " ".join(texts)
        if is_reported_anchor(joined, a["start"], len(texts[0]) + 1 + len(texts[1]) + 1 + finish["end"]):
            return None
        if finish.get("examination") and reported_examination(context + " " + joined,
                len(context) + 1 + a["start"], len(context) + 1 + len(joined)):
            return None
        return {"name": b["name"], "finish": finish["end"], "rows": [lead, name, u], "joined": joined,
                "examination": finish.get("examination", False)}

    def _defer_split_name(self, u: dict) -> bool:
        roster = [{"name": person.name, "role": person.role} for person in self.roster.entries]
        piece = split_call_fragment(norm(u.get("text") or ""), roster)
        if not piece or piece["kind"] != "name":
            return False
        rows, index = self._split_rows(u)
        if index < 1:
            return False
        previous = rows[index - 1]
        lead = split_call_fragment(norm(previous.get("text") or ""), roster)
        return bool(lead and lead["kind"] == "lead" and self._call_piece_ok(previous)
                    and self._short_call_step(previous, u))

    @staticmethod
    def _pending_source_ref(row: dict, block_id: int) -> dict:
        """완성 호명과 유한 대기행의 원문·수집원 판본을 함께 고정한다."""
        return {"id": row["id"], "source_key": row.get("source_key"), "block_id": block_id,
                **{key: row.get(key) for key in ("source_order", "ts_start", "ts_end", "speaker_hint",
                    "source_speaker_hint", "new_speaker", "turn_id", "boundary_reason")},
                "hash": hashlib.sha256(norm(row["text"]).encode()).hexdigest(),
                "raw_hash": hashlib.sha256(row["text"].encode()).hexdigest()}

    def _pending_ref_current(self, ref: dict, *, call: bool = False) -> dict | None:
        row = self.repo.get_utterance(ref["id"])
        block = self._block_record(row.get("block_id")) if row and row.get("block_id") else None
        keys = ("source_order", "ts_start", "speaker_hint", "source_speaker_hint", "new_speaker", "turn_id", "boundary_reason")
        old_end, end = ref.get("ts_end"), (row or {}).get("ts_end")
        # 같은 원문·원메타의 늦은 끝 확정만 허용한다. 저장된 트래커나 과거 블록은 되감지 않는다.
        end_valid = end == old_end or bool(ref.get("raw_hash") and all(key in ref for key in keys)
            and isinstance(old_end, int) and isinstance(end, int) and row and row["ts_start"] <= old_end < end
            and 0 <= end - row["ts_start"] <= ROLE_CONTEXT_MAX_GAP_MS)
        if (not row or not row.get("is_final") or not row.get("analyzed") or self._protected(row, block)
                or row.get("review_parts") or call and not self._call_piece_ok(row)
                or row.get("block_id") != ref["block_id"] or row.get("source_key") != ref["source_key"]
                or not end_valid
                or any(row.get(key) != ref[key] for key in keys if key in ref)
                or hashlib.sha256(norm(row.get("text") or "").encode()).hexdigest() != ref["hash"]
                or ref.get("raw_hash") and hashlib.sha256((row.get("text") or "").encode()).hexdigest() != ref["raw_hash"]):
            return None
        return row

    def _split_pending_valid(self, u: dict) -> bool:
        if not self.split_call:
            return True
        if not self.pending or self.pending.name != self.split_call.get("name"):
            self.split_call = {}
            return True
        roster = [{"name": person.name, "role": person.role} for person in self.roster.entries]
        complete = any(anchor["kind"] == "call" and anchor["speaker"] == self.pending.name
                       and not anchor.get("requires_context") for anchor in scan_anchors(self.split_call.get("joined", ""), roster))
        refs = self.split_call.get("refs", [])
        valid = bool(refs and all(self._pending_ref_current(ref, call=True) for ref in refs))
        rows, index = self._split_rows(u, final_only=False)
        anchor = next((i for i, row in enumerate(rows) if row["id"] == self.split_call.get("uid")), -1)
        if (anchor < 0 or index <= anchor or index - anchor > 6
                or any(call_fragment_order(left) >= call_fragment_order(right)
                       for left, right in zip(rows[anchor:index], rows[anchor + 1:index + 1]))):
            valid = False
        stored = self.repo.get_utterance(u.get("id") or 0)
        input_keys = ("source_key", "text", "original_text", "source_order", "ts_start", "ts_end", "speaker_hint",
                      "source_speaker_hint", "new_speaker", "turn_id", "boundary_reason")
        if (not stored or not stored.get("is_final") or stored.get("review_parts")
                or any(stored.get(key) != u.get(key) for key in input_keys)):
            valid = False
        waiting = self.split_call.get("waiting", [])
        if ([row["id"] for row in rows[anchor + 1:index]] != [ref["id"] for ref in waiting]
                or any(not self._pending_ref_current(ref) for ref in waiting)):
            valid = False
        if valid:
            floor_id = refs[-1]["block_id"]
            if (any(row.get("block_id") != floor_id or row.get("speaker_role") not in {"unknown", "answer"}
                    for row in rows[anchor + 1:index]) or stored.get("block_id") not in {None, floor_id}):
                valid = False
            if complete:
                # 잘린 호명 형성의 20초 제한과 별개로, 완료된 호명 뒤 실제 겹침·대기 구간만 잇는다.
                chain = rows[anchor:index + 1]
                valid = valid and all(0 <= right["ts_start"] - left["ts_start"]
                    and right["ts_start"] <= (left.get("ts_end") or left["ts_start"]) + 15_000
                    for left, right in zip(chain, chain[1:]))
                # 새 발언이 길다는 이유로 이미 제시간에 시작한 질의를 버리지 않는다.
                # 호명과 앞 대기행의 끝, 현재 발언의 시작까지가 대기 시간이다.
                waited_until = max([u["ts_start"], *(row.get("ts_end") or row["ts_start"] for row in chain[:-1])])
                valid = valid and waited_until - chain[0]["ts_start"] <= ROLE_CONTEXT_MAX_GAP_MS
            else:
                valid = valid and -3000 <= u["ts_start"] - self.split_call.get("ts_end", 0) <= 15_000
        if not any(person["name"] == self.pending.name and person["role"] == "member" for person in roster):
            valid = False
        joined = self.split_call.get("joined", "") + " " + norm(u.get("text") or "")
        if (is_reported_anchor(joined, 0, self.split_call.get("joined_end", 0))
                or is_reported_anchor(joined, 0, len(self.split_call.get("joined", "")))
                or self.split_call.get("examination") and reported_examination(joined, 0, len(self.split_call.get("joined", "")))):
            valid = False
        if not valid:
            self.pending, self.split_call = None, {}
        return valid

    def _intro(self, text: str, limit: int = 100) -> re.Match | None:
        """지역 수식이 붙은 새 질의 시작은 명단에 정확히 있는 위원만 인정한다."""
        names = {entry.name for entry in self.roster.entries if entry.role == "member"}
        return _direct_intro(text, limit, names)

    def _completed_call_intro_owner(self, u: dict, text: str, evidence: dict) -> bool:
        """완전 종료 호명 직후의 독립 서론은 질의 소유권만 시작한다."""
        if (not self.pending or not self.split_call or self.split_call.get("waiting")
                or not u.get("new_speaker") or u.get("boundary_reason") != "marker"
                or any(parse_hint(u.get(key))[1] is not None for key in ("speaker_hint", "source_speaker_hint"))
                or evidence["role"] is not None or evidence["mixed"] or len(text) < 60
                or ANSWER_LEAD.search(text) or INDEPENDENT_ANSWER.fullmatch(text) or is_chair_text(text)
                or re.match(r"^(?:잠시|회의(?:장|진행)|장내|기술(?:적|상)|중계|방송|마이크|연결(?:을|이))", text)
                or text.startswith(('“', '‘', '"', "'", '「', '『'))
                or re.match(r"^(?:인용|대본|예시|문구|라는\s*문구|라고\s*(?:말|하))", text)):
            return False
        roster = [{"name": person.name, "role": person.role} for person in self.roster.entries]
        if scan_anchors(text, roster):
            return False
        joined = self.split_call.get("joined", "")
        if completed_closing_call(joined, roster, self.pending.name or "") is None:
            return False
        refs = self.split_call.get("refs") or []
        if not refs:
            return False
        previous = self.repo.get_utterance(refs[-1]["id"])
        if not previous:
            return False
        # 이미 검증한 인접 판본의 끝 뒤에서 새로 시작한 첫 독립 발언만 허용한다.
        gap = u["ts_start"] - (previous.get("ts_end") or previous["ts_start"])
        return 0 <= gap <= 15_000 and call_fragment_order(previous) < call_fragment_order(u)

    def _distrust_hwa(self) -> None:
        """새 충돌은 이전 대응의 근거를 버리며 반대 라벨을 임의로 만들지 않는다."""
        if not self.hwa_untrusted:
            self.role_votes = []
        self.hwa_untrusted = True
        self.role_binding = {}

    def _bind_member(self, u: dict, block: dict, label: str, *, role: str = "member", uncertain: bool = False) -> None:
        """현재 행에서 새로 배운 위원 대응도 출처를 남겨 후속 사용 전에 확인한다."""
        if uncertain or u.get("boundary_reason") == "uncertain_marker":
            return
        self.member_hwa = label
        if not u.get("id") or not self.repo.get_utterance(u["id"]):
            return  # 저장소 없이 사용하는 순수 트래커 호출은 기존 동작을 유지한다.
        self.role_binding = {"version": 1, "block_id": block["id"], "questioner": block.get("questioner"),
                             "member": label, "answer": self.answer_hwa,
                             "refs": [source_ref(u, block["id"], role, revision_hash=self._role_revision_hash(u))]}
        self._legacy_binding = False

    def _role_revision_hash(self, u: dict | None) -> str:
        """보존한 발언 본문과 별도로 저장된 원조각 개정 후보의 의미 판본을 읽는다."""
        key = str((u or {}).get("source_key") or "").rpartition("#")[0]
        segment = self.repo.get_segment(self.sid, key) if key else None
        return revision_fingerprint((segment or {}).get("raw"))

    def _validate_role_binding(self, u: dict, block: dict | None) -> None:
        """기존 근거가 철회되면 대응만 버린다. 과거 행·블록·호명은 되감지 않는다."""
        if self.hwa_untrusted or self.member_hwa is None:
            return
        if self._legacy_binding:
            # 예전 설정에는 학습 출처가 없다. 실제 선행 행이 있는 운영 상태는 다시 증명한다.
            previous = self.repo.neighbor_utterances(u["id"], final_only=False)[0] if u.get("id") else None
            if previous and previous.get("analyzed") and block and previous.get("block_id") == block["id"]:
                self._legacy_binding = False
                self._distrust_hwa()
            return
        binding = self.role_binding
        if not binding:
            return
        refs = binding.get("refs")
        valid = (binding.get("version") == 1 and block and binding.get("block_id") == block["id"]
                 and binding.get("questioner") == block.get("questioner")
                 and binding.get("member") == self.member_hwa and binding.get("answer") == self.answer_hwa
                 and isinstance(refs, list) and 1 <= len(refs) <= 2)
        if valid:
            for ref in refs:
                row = self.repo.get_utterance(ref.get("uid")) if isinstance(ref, dict) and ref.get("uid") else None
                if (not isinstance(ref, dict)
                        or not matches_source(ref, row, revision_hash=self._role_revision_hash(row))
                        or self._protected(row, block)):
                    valid = False
                    break
        if not valid:
            self._distrust_hwa()
            self.role_votes = []

    def _learn_roles(self, u: dict, block: dict, evidence: dict, label: str | None, *, uncertain: bool = False) -> None:
        """서로 다른 최근 원 조각의 두 역할 근거가 일치할 때만 대응을 복구한다."""
        if (uncertain or u.get("boundary_reason") == "uncertain_marker" or not self.hwa_untrusted
                or label is None or evidence["mixed"] or not evidence["role"]):
            return
        text, ts = norm(u.get("text") or ""), u["ts_start"]
        if ts < self.role_context_ts or len(text) < 15 or INDEPENDENT_ANSWER.fullmatch(text) \
                or re.search(r'[“”‘’「」『』"\']|(?:라고|다고|라는)\s*(?:답변|말씀|말|질문)|자문자답|예를\s*들|대본', text):
            return  # 단답·인용·같은 과거 입력의 재전송은 근거 표에 넣지 않는다.
        role = evidence["role"]
        if role == "member" and not evidence["question"]:
            return
        key = str(u.get("source_key") or "").split("#", 1)[0].split("~", 1)[0]
        if not key:
            return
        kept = [vote for vote in self.role_votes
                if vote.get("block_id") == block["id"] and vote.get("key") != key
                and isinstance(vote.get("ts"), int) and 0 <= ts - vote["ts"] <= ROLE_CONTEXT_MAX_GAP_MS
                and self._current_vote(vote, block)]
        kept.append({"key": key, "role": role, "label": label, "ts": ts, "block_id": block["id"],
                     "uid": u.get("id"), "source_key": u.get("source_key"),
                     "boundary_reason": u.get("boundary_reason") or "", "uncertain": uncertain,
                     "revision_hash": self._role_revision_hash(u),
                     "text_hash": hashlib.sha256(text.encode()).hexdigest()})
        self.role_votes = kept[-4:]
        other = next((vote for vote in reversed(kept[:-1]) if vote["role"] != role), None)
        if not other or other["label"] == label:
            return
        member, answer = (kept[-1], other) if role == "member" else (other, kept[-1])
        self.member_hwa, self.answer_hwa = member["label"], answer["label"]
        self.hwa_untrusted, self.role_votes = False, []
        refs = []
        for vote in (member, answer):
            row = u if vote.get("uid") == u.get("id") else self.repo.get_utterance(vote.get("uid"))
            if row:
                refs.append(source_ref(row, block["id"], vote["role"], revision_hash=self._role_revision_hash(row)))
        if len(refs) == 2:
            self.role_binding = {"version": 1, "block_id": block["id"], "questioner": block.get("questioner"),
                                 "member": self.member_hwa, "answer": self.answer_hwa, "refs": refs}
            self._legacy_binding = False

    def _current_vote(self, vote: dict, block: dict) -> bool:
        """늦은 원문·수동 수정으로 사라진 과거 근거를 복구 표에 재사용하지 않는다."""
        row = self.repo.get_utterance(vote["uid"]) if vote.get("uid") else None
        if not row or row.get("session_id") != self.sid or row.get("block_id") != block["id"] \
                or row.get("source_key") != vote.get("source_key") or self._protected(row, block):
            return False
        if (row.get("boundary_reason") == "uncertain_marker" or vote.get("uncertain")
                or (row.get("boundary_reason") or "") != vote.get("boundary_reason")):
            return False
        text = norm(row.get("text") or "")
        if hashlib.sha256(text.encode()).hexdigest() != vote.get("text_hash") \
                or self._role_revision_hash(row) != vote.get("revision_hash") \
                or _hwa_label(row.get("source_speaker_hint") or row.get("speaker_hint")) != vote.get("label"):
            return False
        evidence = role_evidence(text, after_question=self._previous_question(row, block))
        return bool(not evidence["mixed"] and evidence["role"] == vote.get("role"))

    def _current_local_source(self, block: dict) -> bool:
        """국소 문맥의 원문·라벨·사람 수정이 그대로인 경우에만 재사용한다."""
        old = self.local_turn
        if old.get("block_id") != block["id"]:
            return False
        if old.get("persisted"):
            row = self.repo.get_utterance(old["uid"])
            if not row or self._protected(row, block) or row.get("block_id") != block["id"] \
                    or row.get("source_key") != old.get("source_key") \
                    or hashlib.sha256(norm(row.get("text") or "").encode()).hexdigest() != old.get("text_hash") \
                    or _hwa_label(row.get("source_speaker_hint") or row.get("speaker_hint")) != old.get("label"):
                return False
        return True

    def _source_fragment_hold(self, u: dict, block: dict, text: str) -> bool:
        """현재 원문 주변 여섯 조각의 미종결 색상 분할만 검토로 보류한다."""
        source = _source_fragment_key(u)
        if not source or not u.get("id") or block.get("kind") != "question":
            return False
        current = self.repo.get_utterance(u["id"])
        fields = ("text", "original_text", "source_key", "source_order", "source_speaker_hint", "speaker_hint",
                  "turn_id", "boundary_reason", "new_speaker", "ts_start", "ts_end")
        if not current or any(current.get(key) != u.get(key) for key in fields):
            return False
        chain = [current]
        for _ in range(6):
            previous, _next = self.repo.neighbor_utterances(chain[-1]["id"], final_only=False)
            if not previous or _source_fragment_key(previous) != source:
                break
            chain.append(previous)
        if len(chain) < 2 or len(chain) > 6:
            return False

        def neutral(row: dict) -> bool:
            body = norm(row.get("text") or "")
            _, hint_role = parse_hint(row.get("speaker_hint"))
            evidence = role_evidence(body)
            return not (hint_role or evidence["role"] or evidence["mixed"] or has_direct_question(body)
                or has_explicit_turn_cue(body) or is_answer_text(body) or ANSWER_LEAD.search(body))

        def current_source(row: dict, *, incoming: bool = False) -> bool:
            order = row.get("source_order") or ""
            body = norm(row.get("text") or "")
            return bool((row.get("block_id") == block["id"] or incoming and row.get("block_id") is None)
                and row.get("is_final") and (incoming or row.get("analyzed"))
                and not self._protected(row, block) and not row.get("review_parts")
                and parse_hint(row.get("speaker_hint"))[1] is None
                and _hwa_label(row.get("source_speaker_hint") or row.get("speaker_hint")) is not None
                and isinstance(order, str) and len(order.split(":")) >= 3
                and all(value.isdigit() for value in order.split(":"))
                and row.get("boundary_reason") in {"marker", "source_marker"}
                and not re.search(r'[“”‘’「」『』"\']|(?:대본|인용문|자문자답)|(?:예를\s*들|가정해\s*보)', body)
                and not is_reported_anchor(body, 0, len(body)))

        held = False
        ordered = list(reversed(chain))
        for previous, row in zip(ordered, ordered[1:]):
            before_label = _hwa_label(previous.get("source_speaker_hint") or previous.get("speaker_hint"))
            label = _hwa_label(row.get("source_speaker_hint") or row.get("speaker_hint"))
            gap = row["ts_start"] - previous["ts_start"]
            duration = max(row.get("ts_end") or row["ts_start"], previous.get("ts_end") or previous["ts_start"]) - previous["ts_start"]
            held = bool(current_source(previous) and current_source(row, incoming=row["id"] == u["id"])
                and call_fragment_order(previous) < call_fragment_order(row) and 0 <= gap <= 3000
                and 0 <= duration <= ROLE_CONTEXT_MAX_GAP_MS and neutral(previous) and neutral(row)
                and _source_linking_tail(previous.get("text") or "") and (before_label != label or held))
        return held

    def needs_answer_fragment_review(self, row: dict, block: dict | None = None) -> bool:
        """목적격 꼬리와 다음 원 segment의 연결어만 역할 변경 없이 검토로 보낸다."""
        if (not block or block.get("kind") != "question" or row.get("speaker_role") != "answer"
                or not row.get("id")):
            return False
        current = self.repo.get_utterance(row["id"])
        fields = ("text", "original_text", "source_key", "source_order", "source_speaker_hint", "speaker_hint",
                  "turn_id", "boundary_reason", "new_speaker", "ts_start", "ts_end")
        if (not current or not current.get("is_final") or any(current.get(key) != row.get(key) for key in fields)
                or current.get("analyzed") and current.get("speaker_role") != row.get("speaker_role")):
            return False
        previous, _next = self.repo.neighbor_utterances(row["id"], final_only=False)
        if (not previous or not previous.get("is_final") or not previous.get("analyzed")
                or previous.get("speaker_role") != "member" or previous.get("block_id") != block["id"]
                or current.get("block_id") not in {None, block["id"]}
                or previous.get("boundary_reason") not in {"limit_time", "limit_chars"}
                or current.get("boundary_reason") not in {"marker", "source_marker"}):
            return False

        def order(item: dict) -> tuple[int, ...] | None:
            key = re.fullmatch(r"t?s(\d+)(?:\.(\d+))?~(\d+)#(\d+)", str(item.get("source_key") or ""))
            value = item.get("source_order") or ""
            pieces = value.split(":") if isinstance(value, str) else []
            if not key or len(pieces) != 4 or not all(piece.isdigit() for piece in pieces):
                return None
            actual, expected = tuple(map(int, pieces)), tuple(int(piece or 0) for piece in key.groups())
            return actual if actual == expected else None

        before_order, current_order = order(previous), order(current)
        before_source, source = _source_fragment_key(previous), _source_fragment_key(current)
        labels = [_hwa_label(item.get("source_speaker_hint") or item.get("speaker_hint")) for item in (previous, current)]
        if (not before_order or not current_order or before_order >= current_order or not before_source or not source
                or before_source == source or set(labels) != {"0", "1"}):
            return False
        for item in (previous, current):
            body = norm(item.get("text") or "")
            if (self._protected(item, block) or item.get("review_parts")
                    or not str(item.get("speaker_hint") or "").startswith("hwa:")
                    or _hwa_label(item.get("speaker_hint")) != _hwa_label(item.get("source_speaker_hint") or item.get("speaker_hint"))
                    or re.search(r'[“”‘’「」『』"\']|(?:대본|자문자답|인용문)', body)):
                return False
        before_start, start = previous.get("ts_start"), current.get("ts_start")
        before_end, end = previous.get("ts_end") or before_start, current.get("ts_end") or start
        if (any(isinstance(value, bool) or not isinstance(value, int) for value in (before_start, start, before_end, end))
                or not 0 <= start - before_start <= 10_000 or before_end < before_start or end < start
                or start > before_end + 3000 or max(before_end, end) - before_start > ROLE_CONTEXT_MAX_GAP_MS):
            return False
        body, preceding = norm(row.get("text") or ""), norm(previous.get("text") or "")
        evidence = role_evidence(body)
        return bool(preceding.endswith(("을", "를")) and re.match(r"^(?:해서|하여)(?=\s|[,.!?。])", body)
            and (_source_linking_tail(body) or re.search(r"(?:할|될|볼|줄|갈|쓸|낼|던)$", body))
            and not evidence["role"] and not evidence["mixed"] and not has_direct_question(body)
            and not has_explicit_turn_cue(body) and not is_answer_text(body) and not ANSWER_LEAD.search(body))

    def _local_continuation(self, u: dict, block: dict, text: str, *, source_hold: bool | None = None) -> tuple[bool, str | None, bool]:
        """같은 원 발언의 명확한 역할과 혼합 보류만 좁게 잇는다. 색 대응 학습은 하지 않는다."""
        old, turn = self.local_turn, u.get("turn_id")
        held_by_source = self._source_fragment_hold(u, block, text) if source_hold is None else source_hold
        if held_by_source:
            return True, None, False
        if old.get("source_hold"):
            return False, None, False  # 원 segment 보류를 다른 발언의 혼합 문맥으로 확장하지 않는다.
        gap = u["ts_start"] - int(old.get("ts") or 0)
        if not self._current_local_source(block) or not 0 <= gap <= ROLE_CONTEXT_MAX_GAP_MS:
            return False, None, False
        limit = u.get("boundary_reason") in {"limit_time", "limit_chars"}
        same = bool(turn and old.get("turn_id") == turn and limit and not u.get("new_speaker"))
        label = _hwa_label(u.get("source_speaker_hint") or u.get("speaker_hint"))
        if same:
            return bool(old.get("mixed")), (old.get("role") if label == old.get("label") else None), False
        # 같은 혼합 발언의 미완성 병렬구문은 새 표지라도 1회만 검토 대상으로 잇는다.
        end_gap = u["ts_start"] - int(old.get("ts_end") or old.get("ts") or 0)
        tail = old.get("tail") or ""
        marker = u.get("boundary_reason") in {None, "", "marker", "source_marker"}
        available = not old.get("parallel") and -3000 <= end_gap <= 3000 and marker
        unfinished = not re.search(r"(?:습니다|합니다|했다|한다|다|요|죠|까)[,.!?。]*$", tail)
        parallel = bool(available and unfinished and (old.get("mixed") or label != old.get("label"))
                        and re.match(r"^(?:또는|및)\s", text))
        mixed_subject = bool(not old.get("parallel") and marker and old.get("mixed")
                             and re.search(r"(?:저희[가는도]?|우리[가는]?|제가|저는)\s*$", tail))
        condition = re.search(r"[가-힣]{1,12}면$", tail)
        repeated_condition = bool(available and label != old.get("label") and condition
                                  and text.startswith(condition.group()))
        held = parallel or mixed_subject or repeated_condition
        return held, None, held

    def _answer_tail_then_question(self, u: dict, block: dict, text: str) -> bool:
        """직전 미종결 답변의 종결 조각 뒤 새 질문이 붙은 경우만 경계로 보류한다."""
        old = self.local_turn
        gap = u["ts_start"] - int(old.get("ts_end") or old.get("ts") or 0)
        ordered_overlap = bool(old.get("source_order") and u.get("source_order")
                               and str(u["source_order"]) > str(old["source_order"])
                               and 0 <= u["ts_start"] - int(old.get("ts") or 0) <= ROLE_CONTEXT_MAX_GAP_MS
                               and gap < 0)
        tail = old.get("tail") or ""
        completion = bool((tail.endswith("하도록") and re.match(r"^하겠습니다[,.!?。\s]+", text))
                          or (is_incomplete_fragment(tail) and re.match(
                              r"^(?:하고\s*있습니다|있습니다|하겠습니다)[,.!?。\s]+(?:하여튼|그런데|그러면)\s", text)))
        return bool(self._current_local_source(block) and has_direct_question(text) and old.get("role") == "answer"
                    and old.get("block_id") == block["id"]
                    and (-3000 <= gap <= 3000 or ordered_overlap) and completion)

    def _mixed_floor(self, text: str, anchors: list[dict], hint_role: str | None) -> bool:
        """진행과 다른 사람의 발언이 한 행에 붙어도 본문을 자동 분할하지 않는다."""
        if not anchors:
            return False
        first = anchors[0]
        leading = text[:first["start"]].strip()
        if first.get("examination") and examination_transition(leading):
            leading = ""
        if (leading and not re.fullmatch(r"(?:(?:예|네|그러면|그럼|다음은|이어서|계속해서|"
                                        r"수고하셨습니다|수고\s*많으셨습니다|감사합니다|고맙습니다|알겠습니다)[,\s]*)+", leading)
                and (hint_role in {"member", "answer"} or self.last_role in {"member", "answer", "unknown"}
                     or ANSWER_LEAD.search(leading))):
            return True
        for anchor in anchors:
            tail = text[anchor["end"]:].strip()
            if call_tail_requires_review(text, anchor):
                return True
            if anchor["kind"] == "call" and tail:
                if tail.startswith(('“', '‘', '"', "'", '「', '『')) or re.match(r"^(?:예를|문구|인용|보고서|대본)", tail):
                    continue
                if self._intro(tail) or re.match(r"^(?:\S+\s+){0,2}(?:교육감|장관|기관장)님", tail) \
                        or MEMBER_PROGRESS_END.search(tail):
                    return True
        return _direct_cue(re.compile(r"(?:십시오|습니까|십니까|하시고)\s+(?:예|네|그\s*부분은\s*죄송)|"
                                     r"실장님\s+(?:예|네)(?:\s|$)"), text)

    @staticmethod
    def _protected(u: dict, block: dict | None) -> bool:
        """사람 수정과 검수된 표시를 진행 규칙으로 덮어쓰지 않는다."""
        summary = (block or {}).get("summary") or {}
        return bool(u.get("speaker_source") == "manual" or u.get("edited") or u.get("edited_by")
            or (u.get("original_text") is not None and u.get("original_text") != u.get("text"))
            or (block or {}).get("review_status") == "reviewed"
            or (isinstance(summary, dict) and summary.get("edited_by")))

    # ── 블록 헬퍼 ────────────────────────────────────────────
    def _open_block(self) -> dict | None:
        row = self.repo.db.query_one(
            "SELECT id FROM blocks WHERE session_id=? AND is_open=1 ORDER BY ts_start DESC, id DESC LIMIT 1", (self.sid,))
        return self._block_record(row["id"]) if row else None

    def _close(self, block: dict | None, closed: list[int]) -> None:
        if block and block["is_open"]:
            self.repo.update_block(block["id"], is_open=0)
            if block["kind"] == "question":
                closed.append(block["id"])

    def _resolve_name(self, raw: str | None, party: str = "", *, supplied: bool = False) -> tuple[str, str, bool]:
        """(표시 이름, 정당, 명단 일치 여부)"""
        if not raw:
            return "", party, False
        e, _fuzzy = self.roster.match(raw)
        if e:
            return e.name, e.party or party, True
        # 명시 수집원·운영자 힌트와 달리 본문에서 추정한 명단 밖 이름은 확정 표시하지 않는다.
        return (raw if supplied else ""), party, False

    def _new_question_block(self, ts: int, ann: Announcement | None, basis: str, changed: list[int]) -> dict:
        self.member_hwa = None
        self.answer_hwa = None
        self.hwa_untrusted = False
        self.role_votes = []
        self.role_binding = {}
        self._legacy_binding = False
        self.role_learning_uncertain = False
        self.last_question = False
        self.last_weak_split = False
        self.local_turn = {}
        self.question_role = "member"
        self.chair_context = False
        name, party, _ = self._resolve_name(ann.name if ann else None, ann.party if ann else "")
        status = "ai_inferred" if name else "needs_name"
        b = self.repo.create_block(self.sid, kind="question", ts_start=ts, questioner=name, questioner_party=party,
                                   status=status, name_basis=basis if name else "")
        changed.append(b["id"])
        self.last_role = None
        return b

    def _set_questioner(self, block: dict, name: str, party: str, basis: str, changed: list[int],
                        relabeled: list[int], updated_items: list[int]) -> dict:
        if block["status"] == "confirmed" or block.get("review_status") == "reviewed":
            return block
        old = block.get("questioner") or ""
        b = self.repo.update_block(block["id"], questioner=name, questioner_party=party, status="ai_inferred",
                                   name_basis=basis)
        changed.append(block["id"])
        # 이미 붙은 위원 발언의 화자 표시도 고친다
        member_uids: set[int] = set()
        for u in self.repo.list_utterances(self.sid, block_id=block["id"]):
            if u["speaker_role"] == "member" and not self._protected(u, block):
                member_uids.add(u["id"])
                if u["speaker"] != name:
                    self.repo.update_utterance(u["id"], speaker=name)
                    relabeled.append(u["id"])
        self._sync_item_people(block["id"], old, name, member_uids, updated_items)
        return b  # type: ignore[return-value]

    def _sync_item_people(self, block_id: int, old: str, name: str, member_uids: set[int],
                          updated_items: list[int]) -> None:
        """질의자 자동 확정 시 이 블록에서 이미 추출된 항목의 요구 위원(requester)·발언자(speaker)를 맞춘다.

        • 사람이 손댄 항목(edited=1)과 수동 항목(origin='manual')은 건드리지 않는다.
        • 비어 있거나 옛 질의자 이름인 값만 바꾼다(다른 사람 이름은 사람이·수집원이 준 값으로 본다).
        • 발언자는 그 항목이 위원 발언(이번에 재표시 대상)에서 나왔을 때만 바꾼다.
        시스템 갱신이므로 by_human=False(edited 표시를 남기지 않음).
        """
        if not name:
            return
        keep = {"", old}
        for it in self.repo.list_items(self.sid, block_id=block_id, include_dismissed=True):
            if it.get("edited") or it.get("origin") not in ("rule", "llm"):
                continue
            fields: dict[str, str] = {}
            if (it.get("requester") or "") in keep and it.get("requester") != name:
                fields["requester"] = name
            if (it.get("utterance_id") in member_uids and (it.get("speaker") or "") in keep
                    and it.get("speaker") != name):
                fields["speaker"] = name
            if fields:
                self.repo.update_item(it["id"], by_human=False, **fields)
                updated_items.append(it["id"])

    def _try_resume(self, resume: dict, u: dict[str, Any], text: str, hint_role: str | None,
                    hint_name: str | None, changed: list[int]) -> dict | None:
        """수집 중단으로 블록이 닫힌 뒤 첫 발언: 위원장 호명 대기를 되살리거나 직전 질의 블록을 다시 연다."""
        ts = u["ts_start"]
        if ts - int(resume.get("ts") or 0) > RESUME_MAX_GAP_MS:
            return None
        p = resume.get("pending")
        if p:
            if self.pending is None:
                self.pending = Announcement(p.get("name"), p.get("party", ""))
            return None
        bid = resume.get("block_id")
        old = self._block_record(bid) if bid else None
        if not old or old["kind"] != "question" or old["is_open"] or old.get("review_status") == "reviewed":
            return None  # 검수 완료한 블록은 다시 열지 않는다
        latest = self.repo.db.query_one(
            "SELECT id FROM blocks WHERE session_id=? ORDER BY ts_start DESC, id DESC LIMIT 1", (self.sid,))
        if not latest or latest["id"] != old["id"]:
            return None  # 그사이 다른 블록이 생겼다
        # 다른 위원 이름이 오면(수집원 힌트·자기소개) 이어 붙이지 않는다
        other = hint_name if hint_role == "member" else None
        if not other:
            intro = self._intro(text, 80)
            other = intro.group("name") if intro and is_valid_name(intro.group("name")) else None
        if other and old["questioner"] and self._resolve_name(other)[0] != old["questioner"]:
            return None
        b = self.repo.update_block(old["id"], is_open=1)
        changed.append(old["id"])
        return b

    # ── 메인 ────────────────────────────────────────────────
    def _previous_question(self, u: dict, block: dict) -> bool:
        """늦은 수정은 현재 트래커 대신 실제 직전 원문만 문맥으로 쓴다."""
        if not u.get("id"):
            return False
        rows = self.repo.list_utterances(self.sid, block_id=block["id"], final_only=True)
        positions = {row["id"]: index for index, row in enumerate(rows)}
        position = positions.get(u["id"], 0)
        if not position:
            return False
        previous = rows[position - 1]
        gap = (u.get("ts_start") or 0) - (previous.get("ts_start") or 0)
        return bool(0 <= gap <= ROLE_CONTEXT_MAX_GAP_MS and role_evidence(previous.get("text") or "")["question"])

    def revise_speaker(self, u: dict[str, Any]) -> dict[str, str] | None:
        """이미 저장한 발언의 강한 역할 근거만 다시 본다. 블록·트래커 상태는 바꾸지 않는다."""
        block = self._block_record(u.get("block_id")) if u.get("block_id") else None
        if not block or self._protected(u, block):
            return None
        text = norm(u.get("text") or "")
        split = self._split_call_candidate(u, block, revised=True)
        if split and text[split["finish"]:].strip(" ,.!?。"):
            return {"speaker": "", "speaker_role": "unknown", "speaker_source": "inferred"}
        name, hint_role = parse_hint(u.get("speaker_hint"))
        estimated = (u.get("speaker_hint") or "").startswith(ESTIMATED_PREFIX)
        source = "feed" if hint_role and not estimated else "inferred"
        opened = self._open_block()
        anchors = self._floor_anchors(text, hint_role, block, allow_context=bool(opened and opened["id"] == block["id"]),
                                      ts=u.get("ts_start"), current=u)
        if self._mixed_floor(text, anchors, hint_role):
            return {"speaker": "", "speaker_role": "unknown", "speaker_source": "inferred"}
        if anchors:
            return {"speaker": self.chair_name or "위원장", "speaker_role": "chair", "speaker_source": "inferred"}
        if hint_role == "chair" or (hint_role is None and is_chair_text(text)):
            return {"speaker": name or self.chair_name or "위원장", "speaker_role": "chair", "speaker_source": source}
        evidence = role_evidence(text, after_question=self._previous_question(u, block))
        if evidence["mixed"]:
            intro = self._intro(text)
            intro_name = self._resolve_name(intro.group("name"))[0] if intro else ""
            primary_member = bool(evidence.get("backchannel_only") and len(text) >= 180
                                  and block.get("kind") == "question" and block.get("questioner")
                                  and (intro_name == block["questioner"]
                                       or (u.get("speaker_role") == "member"
                                           and u.get("speaker") == block["questioner"])))
            if primary_member:
                return {"speaker": block["questioner"], "speaker_role": "member", "speaker_source": source}
            return {"speaker": "", "speaker_role": "unknown", "speaker_source": "inferred"}
        if self._source_fragment_hold(u, block, text):
            return {"speaker": "", "speaker_role": "unknown", "speaker_source": "inferred"}
        if hint_role is None and evidence["role"] == "answer":
            return {"speaker": "답변", "speaker_role": "answer", "speaker_source": "inferred"}
        if (hint_role is None and u.get("boundary_reason") == "uncertain_marker"
                and not evidence["role"] and not has_explicit_turn_cue(text)):
            return {"speaker": "", "speaker_role": "unknown", "speaker_source": source}
        if hint_role == "answer" or (hint_role is None and evidence["role"] != "member"
                                    and (is_answer_text(text) or ANSWER_LEAD.search(text))):
            return {"speaker": name or "답변", "speaker_role": "answer", "speaker_source": source}
        intro = self._intro(text)
        intro_name = self._resolve_name(intro.group("name"))[0] if intro else ""
        strong_member = (hint_role == "member" or evidence["role"] == "member" or intro_name or ADDRESS_HEAD.search(text)
                         or MEMBER_REQUEST_END.search(text) or MEMBER_PROGRESS_END.search(text))
        if strong_member and block["kind"] == "question":
            if self.question_role == "chair" and block.get("questioner") == self.chair_name:
                return {"speaker": self.chair_name or "위원장", "speaker_role": "chair", "speaker_source": source}
            if hint_role == "member" and name:
                name = self._resolve_name(name, supplied=not estimated)[0]
            elif intro_name:
                name = intro_name
            else:
                name = block.get("questioner") or ""
            return {"speaker": name or "", "speaker_role": "member", "speaker_source": source}
        return None     # 라벨 변화·'-'만으로 과거 발언의 역할을 뒤집지 않는다

    def process(self, u: dict[str, Any]) -> Decision:
        text = norm(u["text"])
        ts = u["ts_start"]
        ts_end = u.get("ts_end") or ts
        changed: list[int] = []
        closed: list[int] = []
        relabeled: list[int] = []
        updated: list[int] = []
        hint_name, hint_role = parse_hint(u.get("speaker_hint"))
        estimated = (u.get("speaker_hint") or "").startswith(ESTIMATED_PREFIX)
        new_speaker = bool(u.get("new_speaker"))
        uncertain = (u.get("boundary_reason") == "uncertain_marker"
                     or (u.get("boundary_reason") in ("limit_chars", "limit_time")
                         and self.last_role == "unknown" and self.last_weak_split))
        learning_uncertain = (uncertain or (u.get("boundary_reason") in ("limit_chars", "limit_time")
                                           and self.role_learning_uncertain))
        source = "feed" if hint_role and not estimated else "inferred"
        hint_basis = "호명 추정" if estimated else "수집원"
        block = self._open_block()
        saved_block = self._block_record(u.get("block_id")) if u.get("block_id") else block
        if self._protected(u, saved_block) and saved_block:
            return Decision(speaker=u.get("speaker") or "", role=u.get("speaker_role") or "unknown",
                            source=u.get("speaker_source") or "inferred", block_id=saved_block["id"])

        # 종료 직후에는 열린 블록이 없다. 재개 단서를 검사한 뒤 같은 블록의 근거를 확인한다.
        deferred_binding_check = block is None and self.resume is not None
        if not deferred_binding_check:
            self._validate_role_binding(u, block)

        split_cancelled = not self._split_pending_valid(u)
        split = self._split_call_candidate(u, block)

        if self.role_context_ts and ts - self.role_context_ts > ROLE_CONTEXT_MAX_GAP_MS:
            self.last_question = False
            if self.member_hwa is not None:
                self._distrust_hwa()
                self.role_votes = []
        evidence = role_evidence(text, after_question=self.last_question)
        weak_review = bool(weak_response_spans(u["text"]))
        semantic_review, semantic_conflict = False, False
        source_hold = self._source_fragment_hold(u, block, text) if block else False
        local_hold, local_role, local_parallel = self._local_continuation(u, block, text, source_hold=source_hold) if block else (False, None, False)
        if block and self._answer_tail_then_question(u, block, text):
            evidence = {"role": None, "mixed": True, "question": False,
                        "binding_conflict": evidence.get("binding_conflict", False),
                        "reason": "직전 미종결 답변의 끝과 새 질문이 같은 원문 행에 포함"}

        recent_chair = 0 <= ts - self.chair_context_ts <= RESUME_MAX_GAP_MS
        if not recent_chair:
            self.chair_context = False
        anchors = self._floor_anchors(text, hint_role, block, ts=ts, defer_bare=self._defer_split_name(u), current=u)
        mixed_floor = self._mixed_floor(text, anchors, hint_role)
        latest = anchors[-1] if anchors else None
        called_member = bool(self.pending and self.pending.name)
        floor_ann = (Announcement(split["name"]) if split else
                     Announcement(latest["speaker"]) if latest and latest["kind"] == "call" else None)
        if split and text[split["finish"]:].strip(" ,.!?。"):
            mixed_floor = True
        floor_return = any(a["kind"] == "chair_return" for a in anchors)
        chair_question = bool(latest and latest["kind"] == "chair_question")
        reply_to_chair = bool(self.chair_context and self.pending is None and block and block["kind"] == "chair"
            and (hint_role == "answer" or (hint_role is None and (is_answer_text(text) or ANSWER_LEAD.search(text)))))
        intro_cue = self._intro(text)
        direct_intro = bool(intro_cue)
        strict_pending = bool(self.split_call and self.pending and self.pending.name == self.split_call.get("name"))
        if strict_pending and hint_role == "member" and hint_name and hint_name != self.pending.name and not estimated:
            self.pending, self.split_call, strict_pending = None, {}, False
        # 질의 블록의 소유권과 한 행 내부의 화자 확신은 별개다. 직접 질문에 짧은
        # 응답이 섞여도 유효 호명 뒤의 질의는 시작하며, 아래 역할 판정은 미상으로 남긴다.
        question_started = bool(hint_role != "answer" and (hint_role == "member" or direct_intro
            or evidence["role"] == "member" or ADDRESS_HEAD.search(text)
            or (has_direct_question(text) and evidence["role"] != "answer" and not ANSWER_LEAD.search(text))))
        # 호명 없는 전사도 직접 질문이 든 문답 전체를 질의 블록으로 보존한다.
        # 혼합 행의 단일 화자는 아래에서 미정으로 두며 인용·기관 반문으로 이름을 만들지 않는다.
        text_question_start = bool(hint_role != "answer" and not _REVERSE_QUESTION_START.match(text)
            and (evidence["role"] == "member" and not evidence["mixed"]
                 or evidence["mixed"] and question_started))
        intro_owner = bool(strict_pending and not split_cancelled and not question_started
                           and self._completed_call_intro_owner(u, text, evidence))
        if intro_owner:
            question_started = True
            learning_uncertain = True  # 소유권 근거를 첫 색상·문장 화자 학습에 사용하지 않는다.
        pending_reply = bool(self.pending and not question_started and
            (hint_role == "answer" or evidence["role"] == "answer"
             or INDEPENDENT_ANSWER.fullmatch(text) or ANSWER_LEAD.search(text)))
        defer_split = split_cancelled or (strict_pending and not question_started) or pending_reply
        continues_chair = bool(self.chair_context and self.pending is None and block and block["kind"] == "chair"
            and hint_role is None and not reply_to_chair and not direct_intro and not new_speaker and not uncertain)

        is_chair = bool(split) or bool(anchors) or continues_chair or hint_role == "chair" or (hint_role is None and is_chair_text(text))
        # 재개 단서는 다음 발언 하나에만 쓴다(위원장 발언이면 버린다)
        resume, self.resume = self.resume, None
        if resume and block is None and not is_chair:
            block = self._try_resume(resume, u, text, hint_role, hint_name, changed)
        if deferred_binding_check:
            self._validate_role_binding(u, block)
        # 긴 주발언 속 짧은 맞장구는 검증된 대응으로만 대표 위원 표시를 유지한다.
        dominant_member = bool(evidence.get("backchannel_only") and len(text) >= 180
                               and (direct_intro or (self.last_role == "member" and not self.hwa_untrusted
                                    and _hwa_label(u.get("source_speaker_hint") or u.get("speaker_hint")) == self.member_hwa
                                    and self.member_hwa is not None)))
        if is_chair:
            if hint_name and hint_role == "chair":
                self.chair_name = hint_name
            ann = floor_ann or find_announcement(text)
            if ann and re.search(r"해\s*주시겠습니다", text) and not any(
                    person.name == ann.name and person.role == "member" for person in self.roster.entries):
                # 새 호명 어미의 이름은 명단과 정확히 맞을 때만 쓴다. 비슷한 이름으로 고치지 않는다.
                ann = Announcement(None)
            closing = (_direct_cue(CLOSING_PATTERNS, text) or floor_return
                       or any(anchor.get("closes_question") for anchor in anchors))
            if chair_question:
                if block and not (block["kind"] == "question" and self.question_role == "chair"):
                    self._close(block, closed)
                    block = None
                if block is None or block["kind"] != "question":
                    block = self._new_question_block(ts, Announcement(self.chair_name or None), "위원장 직접 질의", changed)
                self.question_role = "chair"
                self.pending = None
                label = _hwa_label(u.get("source_speaker_hint") or u.get("speaker_hint"))
                if label is not None and not learning_uncertain and not mixed_floor:
                    self._bind_member(u, block, label, role="chair", uncertain=learning_uncertain)
            elif ann or closing:
                # 질의 블록을 닫고 위원장 진행 블록으로
                if block and block["kind"] != "chair":
                    self._close(block, closed)
                    block = None
                if block is None:
                    block = self.repo.create_block(self.sid, kind="chair", ts_start=ts, status="confirmed",
                                                   name_basis="진행")
                    changed.append(block["id"])
                self.pending = ann if ann else None
                self.split_call = {}
                guarded_call = split
                roster = [{"name": person.name, "role": person.role} for person in self.roster.entries]
                terminal_call = bool(ann and new_speaker and u.get("boundary_reason") == "marker"
                                     and completed_closing_call(text, roster, ann.name or ""))
                if (not guarded_call and ann and (mixed_floor or terminal_call) and latest and latest["kind"] == "call"
                        and u.get("id") and u.get("source_key") and self.repo.get_utterance(u["id"])):
                    guarded_call = {"name": ann.name, "rows": [u], "joined": text,
                                    "finish": latest["end"], "examination": latest.get("examination", False)}
                if guarded_call:
                    refs = []
                    for row in guarded_call["rows"]:
                        refs.append(self._pending_source_ref(row, block["id"] if row["id"] == u["id"] else row["block_id"]))
                    self.split_call = {"name": guarded_call["name"], "refs": refs, "uid": u["id"],
                        "ts_end": ts_end, "joined": guarded_call["joined"],
                        "waiting": [],
                        "examination": guarded_call.get("examination", False),
                        "joined_end": len(guarded_call["joined"]) - len(text) + guarded_call["finish"]}
            elif block is None:
                block = self.repo.create_block(self.sid, kind="chair", ts_start=ts, status="confirmed",
                                               name_basis="진행")
                changed.append(block["id"])
            speaker = self.chair_name or "위원장"
            role = "chair"
            self.chair_context = True
        elif defer_split:
            # 완성 호명 뒤 답변·불명확 조각이 먼저 와도 새 위원의 이름을 붙이지 않는다.
            role = "answer" if hint_role == "answer" or evidence["role"] == "answer" or pending_reply else "unknown"
            speaker = (hint_name or "답변") if role == "answer" else ""
            semantic_review = True
        else:
            # 위원 자기소개(앞 100자 안, 위원장 호명 꼬리 '바랍니다' 등이 앞에 붙어도 됨)
            intro = self._intro(text)
            intro_raw = intro.group("name") if intro and is_valid_name(intro.group("name")) else None
            intro_name = self._resolve_name(intro_raw)[0] or None
            # 수집원이 위원 이름을 주면 그 이름으로 블록을 맞춘다
            if hint_role == "member" and hint_name:
                name, party, _ = self._resolve_name(hint_name, supplied=not estimated)
                hint_name = name or None
                if block and block["kind"] == "question":
                    if block["status"] == "needs_name" or not block["questioner"]:
                        block = self._set_questioner(block, name, party, hint_basis, changed, relabeled, updated)
                    elif block["questioner"] != name and self.pending is None:
                        self._close(block, closed)
                        block = self._new_question_block(ts, Announcement(name, party), hint_basis, changed)
                if block is None or block["kind"] != "question" or self.pending is not None:
                    if block:
                        self._close(block, closed)
                    ann = self.pending if (self.pending and self.pending.name) else Announcement(name, party)
                    block = self._new_question_block(
                        ts, ann, "위원장 호명" if self.pending and self.pending.name else hint_basis, changed)
                    self.pending = None
            elif (self.pending is not None or block is None or (block["kind"] == "chair" and not reply_to_chair)
                  or (block["kind"] == "other" and text_question_start)):
                if block:
                    self._close(block, closed)
                if self.pending is not None:
                    block = self._new_question_block(ts, self.pending, "위원장 호명", changed)
                elif intro_name:
                    block = self._new_question_block(ts, Announcement(intro_name), "자기소개", changed)
                elif text_question_start:
                    # 질의 도중부터 받기 시작한 경우: 누구인지 모르는 위원 질의로 둔다(※성명 확인 필요)
                    block = self._new_question_block(ts, None, "", changed)
                else:
                    block = self.repo.create_block(self.sid, kind="other", ts_start=ts, status="ai_inferred")
                    changed.append(block["id"])
                self.pending = None
            elif intro_name and (block["status"] != "confirmed" or intro_raw == intro_name):
                # 위원 자기소개('경북 고령 성주 칠곡의 정희용 위원입니다')는 새 질의의 시작이다.
                # 이름이 빈 질의 블록이면 그 블록의 질의자로, 아니면(다른 위원·기타 블록) 새 질의 블록을 연다.
                rname, rparty, _ = self._resolve_name(intro_name)
                if block["kind"] == "question" and (block["status"] == "needs_name" or not block["questioner"]):
                    block = self._set_questioner(block, rname, rparty, "자기소개", changed, relabeled, updated)
                elif block["kind"] != "question" or block["questioner"] != rname:
                    self._close(block, closed)
                    block = self._new_question_block(ts, Announcement(intro_name), "자기소개", changed)

            # 블록 안 역할 결정
            if hint_role in ("member", "answer"):
                role = hint_role
                if role == "member" and not learning_uncertain:
                    label = _hwa_label(u.get("source_speaker_hint") or u.get("speaker_hint"))
                    if label is not None:
                        self.hwa_untrusted = False
                        self.answer_hwa, self.role_votes = None, []
                        self._bind_member(u, block, label, uncertain=learning_uncertain)
            elif reply_to_chair:
                role = "answer"
            elif block["kind"] == "question":
                # 직접 본문 근거를 먼저 쓰며, 충돌한 색 대응은 독립된 두 역할 근거로만 복구한다.
                label = _hwa_label(u.get("source_speaker_hint") or u.get("speaker_hint"))
                machine = (u.get("speaker_hint") or "").startswith("hwa:")
                if evidence["mixed"]:
                    role, semantic_review = ("member" if dominant_member else "unknown"), True
                elif intro_name:
                    role = "member"
                    if label is not None and not learning_uncertain:
                        self.hwa_untrusted = False
                        self.answer_hwa, self.role_votes = None, []
                        self._bind_member(u, block, label, uncertain=learning_uncertain)
                elif evidence["role"]:
                    role = evidence["role"]
                    if label is not None and self.member_hwa is not None:
                        mapped = "member" if label == self.member_hwa else "answer"
                        if role != mapped:
                            self._distrust_hwa()
                            semantic_review, semantic_conflict = True, True
                    self._learn_roles(u, block, evidence, label, uncertain=learning_uncertain)
                elif local_hold:
                    role, semantic_review = "unknown", True
                elif local_role and not uncertain and not is_answer_text(text) and not ANSWER_LEAD.search(text):
                    role = local_role
                elif (called_member and not uncertain and not INDEPENDENT_ANSWER.fullmatch(text)
                        and not ANSWER_LEAD.search(text)) or intro_name or ADDRESS_HEAD.search(text):
                    role = "member"
                    if label is not None and not learning_uncertain:
                        if called_member:
                            self.hwa_untrusted = False
                            self.answer_hwa, self.role_votes = None, []
                        self._bind_member(u, block, label, uncertain=learning_uncertain)
                elif uncertain:
                    if INDEPENDENT_ANSWER.fullmatch(text) or ACK_START.search(text) or ANSWER_LEAD.search(text):
                        role = "answer"
                    elif MEMBER_REQUEST_END.search(text) or MEMBER_PROGRESS_END.search(text):
                        role = "member"
                    else:
                        role = "unknown"             # 불완전 구절의 0/1 반복을 문답 역할로 확정하지 않는다
                elif is_answer_text(text):
                    role = "answer"
                elif label is not None and ANSWER_LEAD.search(text):
                    role = "answer"
                    if label == self.member_hwa:
                        self._distrust_hwa()
                        semantic_review, semantic_conflict = True, True
                elif self.hwa_untrusted and machine:
                    role, semantic_review = "unknown", True
                elif label is not None and self.member_hwa is not None:
                    # 신뢰 중인 대응과 실제 관찰한 두 라벨만 쓴다.
                    if self.answer_hwa is not None and label not in {self.member_hwa, self.answer_hwa}:
                        role, semantic_review = "unknown", True
                    else:
                        role = "member" if label == self.member_hwa else "answer"
                elif label is not None and self.last_role in (None, "chair"):
                    role = "member"                   # 호명 직후 첫 발언 = 질의 위원
                    self._bind_member(u, block, label, uncertain=learning_uncertain)
                elif MEMBER_REQUEST_END.search(text) or MEMBER_PROGRESS_END.search(text):
                    role = "member"
                    if label is not None:
                        self._bind_member(u, block, label, uncertain=learning_uncertain)
                elif new_speaker:
                    role = "unknown"                 # '-'만으로 다른 인물·문답 교대를 단정하지 않는다
                elif machine and self.last_role in ("member", "answer", "unknown"):
                    role = self.last_role
                elif self.last_role == "unknown":
                    role = "unknown"
                else:
                    role = "member"
                if role == "member" and label is not None and self.member_hwa is None and not learning_uncertain:
                    self._bind_member(u, block, label, uncertain=learning_uncertain)
            else:
                role = ("unknown" if uncertain and not has_explicit_turn_cue(text) else
                        "answer" if is_answer_text(text) else ("member" if ADDRESS_HEAD.search(text) else "other"))

            # 자기소개로 질의자 보정(블록이 '기타'였다면 질의 블록으로 승격)
            if role == "member":
                intro = self._intro(text, 80)
                if intro and is_valid_name(intro.group("name")) and self._resolve_name(intro.group("name"))[0]:
                    name, party, _ = self._resolve_name(intro.group("name"))
                    if block["kind"] == "other":
                        block = self.repo.update_block(block["id"], kind="question")  # type: ignore[assignment]
                    if block["questioner"] != name:
                        block = self._set_questioner(block, name, party, "자기소개", changed, relabeled, updated)

            if role == "member":
                if self.question_role == "chair" and block.get("questioner") == self.chair_name:
                    role = "chair"
                    speaker = self.chair_name or "위원장"
                else:
                    speaker = (hint_name if hint_role == "member" and hint_name else None) or block["questioner"] or ""
                    self.chair_context = False
            elif role == "answer":
                speaker = hint_name if (hint_role == "answer" and hint_name) else (u.get("speaker_hint") if hint_role == "answer" else "답변")
                speaker = speaker or "답변"
            else:
                speaker = hint_name or ""
            self.last_role = role

        if mixed_floor:
            speaker, role, source = "", "unknown", "inferred"
        if intro_owner:
            speaker, role, source = "", "unknown", "inferred"
            semantic_review = True
            self.chair_context = False
            self._distrust_hwa()
        if evidence["mixed"] and hint_role not in {"chair"}:
            if not dominant_member:
                speaker, role, source = "", "unknown", "inferred"
            semantic_review = True
            if (hint_role is None and evidence.get("binding_conflict")
                    and _hwa_label(u.get("source_speaker_hint") or u.get("speaker_hint")) is not None):
                # 완결된 응답과 재질문이 한 색에 섞이면 지난 색 대응을 철회한다.
                # 미완결 혼합의 국소 보류는 다른 원 발언으로 퍼뜨리지 않는다.
                # 혼합 전후의 질문·응답 표를 한 쌍으로 이어 붙여 복구하지 않는다.
                self._distrust_hwa()
                self.role_votes = []
        if anchors or role == "chair":
            self.chair_context_ts = ts
        if role == "chair":
            self.role_votes = []  # 진행자의 개입 전후 근거를 한 쌍으로 합쳐 복구하지 않는다.
            self.local_turn = {}
        elif block["kind"] == "question" and hint_role is None:
            same_turn = (u.get("turn_id") and self.local_turn.get("turn_id") == u["turn_id"]
                         and self.local_turn.get("block_id") == block["id"])
            carry_role = evidence["role"] or (local_role if not local_hold else None)
            if not carry_role and not evidence["mixed"] and role == "answer" \
                    and (is_answer_text(text) or ANSWER_LEAD.search(text)):
                carry_role = "answer"
            self.local_turn = {"block_id": block["id"], "turn_id": u.get("turn_id"), "ts": ts, "ts_end": ts_end,
                "label": _hwa_label(u.get("source_speaker_hint") or u.get("speaker_hint")),
                "mixed": bool(evidence["mixed"] or local_hold),
                "source_hold": source_hold,
                "role": carry_role,
                "tail": text[-90:], "parallel": bool(local_parallel or (same_turn and self.local_turn.get("parallel"))),
                "uid": u.get("id"), "source_key": u.get("source_key"),
                "source_order": u.get("source_order"),
                "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                "persisted": bool(u.get("id") and self.repo.get_utterance(u["id"]))}
        else:
            self.local_turn = {}
        self.last_role = role
        if not self.pending or self.pending.name != self.split_call.get("name"):
            self.split_call = {}
        elif u.get("id") != self.split_call.get("uid") and self.split_call:
            self.split_call["waiting"] = [*self.split_call.get("waiting", []), self._pending_source_ref(u, block["id"])][-6:]
        self.last_question = bool(evidence["question"] and role in {"member", "chair"})
        self.last_weak_split = bool(uncertain and role == "unknown")
        self.role_learning_uncertain = learning_uncertain
        self.role_context_ts = ts
        answer_question_review = needs_answer_question_review({**u, "speaker_role": role}, block)
        answer_fragment_review = self.needs_answer_fragment_review({**u, "speaker_role": role}, block)

        # 블록 시간 범위 갱신
        if block["ts_end"] < ts_end or block["ts_start"] > ts:
            self.repo.update_block(block["id"], ts_end=max(block["ts_end"], ts_end), ts_start=min(block["ts_start"], ts))
            if block["id"] not in changed:
                changed.append(block["id"])
        self._save()
        return Decision(speaker=speaker, role=role, source=source, block_id=block["id"], block_changed=changed,
                        closed_blocks=closed, relabeled_utterances=relabeled, updated_items=updated,
                        review_required=mixed_floor or semantic_review or weak_review or answer_question_review or answer_fragment_review,
                        review_reason=("한 원문 행의 진행·질의·답변 혼합 경계 확인" if mixed_floor else
                                       "명단 호명으로 질의 범위를 시작하고 서론 화자는 미상으로 유지" if intro_owner else
                                       evidence["reason"] if evidence["mixed"] else
                                       "기계 라벨과 본문 역할 충돌" if semantic_conflict else
                                       "같은 원 segment의 미종결 색상 분할 조각 보류" if source_hold and semantic_review else
                                       "같은 원 발언의 혼합 경계 뒤 중립 구절 보류" if local_hold and semantic_review else
                                       "본문 역할과 충돌한 기계 라벨의 단독 판정 보류" if semantic_review else
                                       "확인형 뒤 짧은 응답의 경계 검토 후보" if weak_review else
                                       "직접 질문이 포함된 답변 역할의 경계 검토 후보" if answer_question_review else
                                       "앞 목적격 꼬리와 이어지는 답변 조각의 경계 검토 후보" if answer_fragment_review else ""))

    def close_all(self) -> list[int]:
        """열린 블록을 모두 닫는다(수집 종료). 일시정지는 블록을 닫지 않는다(pipeline.flush(close_blocks=False)).

        닫기 직전의 질의 블록·호명 대기는 재개 단서(resume)로 남긴다. 종료 후 다시 시작해 같은 위원 발언이
        RESUME_MAX_GAP_MS 안에 이어지면 그 블록을 다시 열고, 호명 직후였다면 호명된 위원 블록을 만든다.
        """
        split_waiting = bool(self.split_call and self.pending and self.pending.name == self.split_call.get("name"))
        closed: list[int] = []
        latest: dict | None = None
        for b in self.repo.db.query(
                "SELECT id FROM blocks WHERE session_id=? AND is_open=1 ORDER BY ts_start, id", (self.sid,)):
            blk = self._block_record(b["id"])
            if blk:
                latest = blk
            self._close(blk, closed)
        resume: dict | None = None
        if (self.pending is not None and not split_waiting) or (latest and latest["kind"] == "question"):
            ts = max((latest or {}).get("ts_end") or 0, (latest or {}).get("ts_start") or 0)
            resume = {
                "block_id": latest["id"] if latest and latest["kind"] == "question" else None,
                "pending": ({"name": self.pending.name, "party": self.pending.party} if self.pending else None),
                "ts": ts or now_ms(),
            }
        self.resume = resume
        self.pending = None
        self.split_call = {}
        self._save()
        return closed


def apply_manual_questioner(repo: Repo, block_id: int, name: str, party: str = "",
                            actor: str = "operator") -> tuple[dict, list[int]]:
    """운영자 교정: 블록 질의자를 확정하고 위원 발언 화자를 일괄 변경."""
    block = repo.get_block(block_id)
    if not block:
        raise KeyError(block_id)
    b = repo.update_block(block_id, questioner=name, questioner_party=party, status="confirmed",
                          name_basis="수동 확인", kind="question")
    relabeled = []
    for u in repo.list_utterances(block["session_id"], block_id=block_id):
        if u["speaker_role"] in ("member", "unknown", "other") and u["speaker_source"] != "manual":
            repo.update_utterance(u["id"], speaker=name, speaker_role="member")
            relabeled.append(u["id"])
    repo.audit(actor, "block.questioner", str(block_id), json.dumps({"name": name, "party": party},
                                                                      ensure_ascii=False))
    return b, relabeled  # type: ignore[return-value]
