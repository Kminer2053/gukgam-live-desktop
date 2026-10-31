"""같은 원자막의 어절 분할에서 강한 문법 연결만 찾는다.

화자 색이나 인물·역할을 추정하지 않는다. 호출자는 같은 원자막의 안정된
조각 목록만 전달하고 원본 조각·라벨을 보존해야 한다. 반환 범위는 발언권
변경의 정답이 아니라 좁게 제한한 문장 연결 근거다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .role_evidence import has_direct_question


_MIN_ANCHOR_CHARS = 10
_MAX_TAIL_PIECES = 4
_QUOTE = re.compile(r"[\"'“”‘’「」『』]")
_REPORTED = re.compile(r"(?:라고|라며)\s*(?:묻|질문|답|대답)|자문자답|대본|인용문")
_FLOOR = re.compile(r"(?:다음은|이어서)\s*[가-힣]{2,4}\s*위원|"
                    r"위원님\s*(?:질의|질문|신문)|(?:정회|산회|속개|감사\s*종료)")
_NOUN_END = re.compile(r"[가-힣]{2,}(?:이|가|은|는)$")
_DEPENDENT_FORMS = ("같습니다", "같아요", "으로보입니다", "입니다")
_PREDICATE_FORMS = tuple(verb + "것" + ending
                         for verb in ("될", "할", "한", "하는", "된", "되는")
                         for ending in _DEPENDENT_FORMS)
_REQUEST_HEAD = re.compile(r"(?:^|\s)(?:해|하여)$")
_REQUEST_END = re.compile(r"바랍니다[.!。]?(?:\s|$)")
_TOPIC_LINK = re.compile(r"(?:대해서|대하여|관해서|관하여|관련해서|관련하여|대해|관해)$")
_OBJECT_PHRASE = re.compile(r"^(?:[가-힣0-9]+\s+){0,4}[가-힣]{2,12}(?:을|를)$")
_DO_PREDICATE = re.compile(r"^(?:했습니다|했었습니다|하였습니다|하였었습니다|합니다|해왔습니다|"
                           r"해오고\s*있습니다|하고\s*있습니다|하겠습니다)(?=$|[\s.!。])")
_INDEPENDENT_RESPONSE = re.compile(r"(?<![가-힣])(?:예|네|맞습니다|그렇습니다|알겠습니다)(?=$|[\s.!?。])|"
                                   r"위원님\s*(?:말씀|질문|지적|의견)|"
                                   r"(?:저희|우리)\s*(?:기관|공사|회사)(?:가|는|에서)")
_ACK_SUBJECT = re.compile(r"(?:(?:충분히|정말|참으로|전적으로|매우)\s+)?"
                          r"(?:위원|의원)님(?:의)?\s*(?:말씀|지적|의견)(?:이|은)")
_ACK_PREDICATE = re.compile(r"(?:맞는|옳은|타당한|정확한)\s*(?:말씀|지적|의견)(?:이십니다|입니다)[.!。]?")
_COORDINATE = re.compile(r"(?:했고|하였고)(?:\s*(?:또|그리고))?")
_PURPOSE = re.compile(r"수\s*있도록(?:\s*(?:많이|충분히|잘|더))?")
_PREPARATION = re.compile(r"(?:준비|조치|보완|검토)(?:를|을)?\s*(?:했습니다|하였습니다|해왔습니다)[.!。]?")
_CLAUSE_BREAK = re.compile(r"(?:니다|이에요|예요|어요|아요|해요|지요|죠|아니요|아니오|잠깐만요)"
                           r"(?=$|[\s,.!?。])|[.!?。]")


@dataclass(frozen=True)
class CaptionFragmentJoin:
    """원본 조각의 연속 범위. stop은 끝 조각 다음의 인덱스다."""

    start: int
    stop: int
    reason: str


def _text(value: str) -> str:
    text = value.strip()
    if text.startswith("-"):
        text = text[1:].strip()
    return re.sub(r"\s+", " ", text)


def _anchor(text: str) -> bool:
    """짧은 맞장구나 독립 발언을 앞 문장으로 오인하지 않는다."""
    return (len(re.sub(r"\s", "", text)) >= _MIN_ANCHOR_CHARS
            and len(text.split()) >= 3
            and not re.search(r"[.?!。]$", text)
            and not _QUOTE.search(text)
            and not _REPORTED.search(text)
            and not _FLOOR.search(text)
            and not has_direct_question(text))


def _short_tail(text: str) -> str | None:
    """어절 꼬리만 허용한다. 응답·질문 등을 포함한 긴 조각은 흡수하지 않는다."""
    if not text or len(text) > 12 or len(text.split()) > 3:
        return None
    if not re.fullmatch(r"[가-힣 ]+[.!。]?", text):
        return None
    return re.sub(r"\s", "", text.rstrip(".!。"))


def _local_clause(clean: list[str], start: int) -> CaptionFragmentJoin | None:
    """독립 응답이 아닌 주어·서술어와 다섯 조각의 연결 절만 인정한다."""
    left, right = clean[start:start + 2]
    if _ACK_SUBJECT.fullmatch(left) and _ACK_PREDICATE.fullmatch(right):
        return CaptionFragmentJoin(start, start + 2, "acknowledgment_predicate")
    parts = clean[start:start + 5]
    if len(parts) != 5:
        return None
    obj, coordinate, clause, purpose, preparation = parts
    # '목적어 / 했고 또 / 긴 절 …할 / 수 있도록 / 준비를 했습니다'는
    # 각 조각에 독립 종결·호명·응답이 없어야 한다. 새 원자막으로 확장하지 않는다.
    if (len(obj) > 30 or not _OBJECT_PHRASE.fullmatch(obj)
            or not _COORDINATE.fullmatch(coordinate) or not _anchor(clause) or _CLAUSE_BREAK.search(clause)
            or not _PURPOSE.fullmatch(purpose) or not _PREPARATION.fullmatch(preparation)):
        return None
    last = clause[-1]
    if not ("가" <= last <= "힣" and (ord(last) - ord("가")) % 28 == 8):
        return None  # '할/이룰/작동할'의 관형형 ㄹ 뒤에서만 의존 명사 '수'를 연결한다.
    whole = " ".join(parts)
    if (any(pattern.search(whole) for pattern in (_QUOTE, _REPORTED, _FLOOR, _INDEPENDENT_RESPONSE))
            or re.search(r"예를\s*들어|가령|만약|가정하면", whole)
            or has_direct_question(whole)):
        return None
    return CaptionFragmentJoin(start, start + 5, "coordinated_purpose_clause")


def find_caption_fragment_joins(texts: Sequence[str]) -> tuple[CaptionFragmentJoin, ...]:
    """강한 문법 연결을 가진 겹치지 않는 연속 조각 범위만 반환한다.

    예: ``긴 절 … 운영이 / 될 / 것 / 같습니다``. 독립 발언이 될 수 있는
    ``예``, ``맞습니다``, 질문, 자기소개, 호명 등은 꼬리 문법에 들어가지
    않는다. ``바랍니다 예 알겠습니다`` 같은 혼합 조각 전체도 흡수하지
    않는다. 본문·순서·라벨은 변경하지 않으며 새 문장을 만들어 넣지 않는다.
    """
    clean = [_text(t) for t in texts]
    joins: list[CaptionFragmentJoin] = []
    i = 0
    while i < len(clean) - 1:
        local = _local_clause(clean, i)
        if local is not None:
            joins.append(local)
            i = local.stop
            continue
        left = clean[i]
        if not _anchor(left):
            i += 1
            continue
        stop: int | None = None
        reason = ""
        if _NOUN_END.search(left) or re.search(r"(?:^|\s)것$", left):
            forms = _DEPENDENT_FORMS if re.search(r"(?:^|\s)것$", left) else _PREDICATE_FORMS
            tail = ""
            for j in range(i + 1, min(len(clean), i + 1 + _MAX_TAIL_PIECES)):
                part = _short_tail(clean[j])
                if part is None:
                    break
                tail += part
                if tail in forms:
                    stop, reason = j + 1, "dependent_predicate"
                    break
                # 꼬리에 어휘가 붙으면 다음 조각을 넘겨 문장을 꾸며 맞추지 않는다.
                if not any(candidate.startswith(tail) for candidate in forms):
                    break
        if stop is None and _REQUEST_HEAD.search(left) and clean[i + 1] == "주시기":
            # 혼합 마지막 조각을 흡수하지 않고, 직접 이어지는 요청 어절만 묶는다.
            if i + 2 < len(clean) and _REQUEST_END.match(clean[i + 2]):
                stop = i + 3 if clean[i + 2].rstrip(".!。") == "바랍니다" else i + 2
                reason = "request_morpheme"
        if stop is None and i + 2 < len(clean) and _TOPIC_LINK.search(left):
            middle, following = clean[i + 1:i + 3]
            # '긴 주제에 대해서 / 여러 차례 질의를 / 했습니다…'의 목적어는
            # 앞뒤 서술어 없이 독립 답변이 되지 않는다. 같은 원자막 안에서만 묶는다.
            if (len(middle) <= 30 and _OBJECT_PHRASE.fullmatch(middle)
                    and _DO_PREDICATE.match(following)
                    and not any(pattern.search(middle + " " + following)
                                for pattern in (_QUOTE, _REPORTED, _FLOOR, _INDEPENDENT_RESPONSE))
                    and not has_direct_question(following)):
                stop, reason = i + 3, "object_predicate"
        if stop is None:
            i += 1
            continue
        joins.append(CaptionFragmentJoin(i, stop, reason))
        i = stop
    return tuple(joins)
