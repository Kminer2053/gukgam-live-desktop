"""원문 문자 위치로만 만드는 혼합 발언 경계·분리 계획 후보.

후보는 화자 전환의 확정이 아니다. 사람 수정 보호와 후보 채택은 호출자가 맡는다.
"""
from __future__ import annotations

import re
from typing import Any

from .nlp.floor import (call_tail_requires_review, is_reported_anchor, new_floor_owner_conflict,
                        scan_anchors)
from .nlp.role_evidence import answer_then_new_question_boundaries, enclosed_response_spans, weak_response_spans

MAX_BOUNDARIES = 8
MAX_PLANS = 10  # 유지·보류 선택지까지 합쳐 제공자의 12개 한도를 지킨다.

_QUESTION = re.compile(r"(?:습니까|십니까|입니까|합니까|됩니까|나요|가요|까요|시죠|했죠|됐죠|계시죠|그렇죠|있지요|었지요|아닙니까)[?.!。]*\s*$")
_ADDRESS = re.compile(r"(?:위원장|위원|의원|교육감|장관|차관|실장|사장|이사장|총장)님[,.!?。]*\s*$")
_DIRECTIVE = re.compile(r"(?:하십시오|주십시오|주시기\s*바랍니다|하세요|하시고)[.!。]*\s*$")
_NEXT_QUESTION = re.compile(r"(?:습니까|십니까|입니까|합니까|됩니까|나요|가요|까요|시죠|그렇죠|아닙니까)(?:[?.!。\s]|$)")
_NEXT_DIRECTIVE = re.compile(r"(?:하십시오|주십시오|주시기\s*바랍니다|보시죠|넘겨\s*주세요|하시고)(?:[.!。\s]|$)")
_RETURN = re.compile(r"(?:그런데|그러면|그럼|그래서|그리고|아니|제가|지금|왜|예\s+[가-힣]{2,5}\s*교육감님)")
_SHORT_BODY = (
    r"(?:그렇게\s*하겠습니다|명심하겠습니다|알고\s*있습니다|잘\s*알고\s*있습니다|"
    r"확인하겠습니다|알겠습니다|맞습니다|그렇습니다|동의합니다|아닙니다|아니요|모릅니다|"
    r"(?:이번에\s*(?:지금\s*)?|지금\s*)?알게\s*되었습니다|"
    r"(?:정확한|좋은|옳은)\s*(?:지적|말씀)(?:인)?\s*것\s*같습니다)"
)
_SHORT = re.compile(r"(?<![가-힣A-Za-z0-9])(?:(?:예|네)\s+)?" + _SHORT_BODY + r"[.!。]*(?![가-힣A-Za-z0-9])")
_BARE = re.compile(r"(?<![가-힣A-Za-z0-9])(?:예|네)[.!。]*(?![가-힣A-Za-z0-9])")
_TRAILING_ADDRESS = re.compile(r"\s+(?:위원장|위원)님[.!。]*(?=\s|$)")
_REPORT = re.compile(r"(?:라고|다고|라는)\s*(?:대답|답변|말씀|말|표현|질문|문구|얘기|하[였는고]|했|한|한다)")
_EXAMPLE = re.compile(r"(?:예를\s*들어|가령|만약|가정하면|가정해\s*보면|스스로\s*묻고|자문자답)")
_FORMAL = re.compile(
    r"(?<![가-힣])(?:"
    r"존경하는\s+[가-힣]{2,5}\s*(?:위원|의원)님|"
    r"(?:답변|말씀)(?:을)?\s*드리겠습니다|"
    r"그\s*부분(?:에\s*대해서|은)?\s*죄송합니다|"
    r"(?:\d{4}(?:년도|년)(?:\s+\d{4}(?:년도|년))?\s*(?:의\s*)?경우에는\s*)?"
    r"(?:아까|앞서|방금)\s*말씀드린\s*대로)"
)
_FLOOR = re.compile(
    r"(?<![가-힣])(?:[가-힣]{2,5}\s*위원님\s*수고하셨습니다|"
    r"(?:다음은\s*)?[가-힣]{2,5}\s*위원님\s*(?:질의|질문|신문)(?:해|하여)?\s*"
    r"(?:주십시오|주시기\s*(?:바랍니다|바라겠습니다))|"
    r"저도\s*(?:자리\s*정리할\s*동안에\s*)?(?:질의|질문)(?:를|을)?\s*(?:좀\s*)?드리겠습니다|"
    r"저도\s*(?:증인\s*)?신문(?:을)?\s*(?:좀\s*)?하겠습니다)"
)
_SELF = re.compile(r"(?<![가-힣])[가-힣]{2,5}\s*(?:위원|의원)입니다[.!。]*(?=\s|$)")
_NEW_QUESTION = re.compile(r"(?:아니\s+(?:그러니까|그동안|왜|지금)|그런데\s+(?:교육감님|장관님|위원님)|그리고\s+마지막으로)")


def _after_space(text: str, offset: int) -> int:
    while offset < len(text) and text[offset].isspace():
        offset += 1
    return offset


def _quoted_ranges(text: str) -> list[tuple[int, int]]:
    """따옴표와 명시적 인용·가정의 후보를 보수적으로 가린다."""
    ranges: list[tuple[int, int]] = []
    pairs = {'"': '"', "'": "'", '“': '”', '‘': '’', '「': '」', '『': '』'}
    stack: list[tuple[str, int]] = []
    for index, char in enumerate(text):
        if char == "'" and 0 < index < len(text) - 1 and text[index - 1].isalnum() and text[index + 1].isalnum():
            continue
        if stack and char == stack[-1][0]:
            _end, start = stack.pop()
            ranges.append((start, index + 1))
        elif char in pairs:
            stack.append((pairs[char], index))
    ranges.extend((start, len(text)) for _end, start in stack)
    for match in _REPORT.finditer(text):
        punctuation = list(re.finditer(r"[.!。\n]", text[:match.start()]))
        start = punctuation[-1].end() if punctuation else 0
        ranges.append((start, match.end()))
    for match in _EXAMPLE.finditer(text):
        # 가정 속 문답의 물음표·마침표는 실제 발언 전환으로 삼지 않는다.
        actual = re.search(r"(?:그런데\s*실제로|반면\s*실제로|실제\s*현장에서는)", text[match.end():])
        end = match.end() + actual.start() if actual else len(text)
        ranges.append((match.start(), end))
    return ranges


def _inside(offset: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in ranges)


def _short_spans(text: str, quotes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """완결 응답만 감싸며 질문자의 단순 맞장구는 경계 근거로 삼지 않는다."""
    spans: list[tuple[int, int]] = []
    for match in _SHORT.finditer(text):
        start, end = match.span()
        if _inside(start, quotes) or _inside(end - 1, quotes):
            continue
        before = text[max(0, start - 120):start]
        after = text[_after_space(text, end):]
        explicit = match.group().startswith(("예 ", "네 ")) or any(
            cue in match.group() for cue in ("알겠습니다", "확인하겠습니다", "명심", "그렇게 하겠습니다"))
        if start and not (explicit or _QUESTION.search(before) or _ADDRESS.search(before)
                          or _DIRECTIVE.search(before)):
            continue
        address = _TRAILING_ADDRESS.match(text, end)
        if address:
            end = address.end()
        end = _after_space(text, end)
        if end < len(text) and not (explicit or _RETURN.match(text, end) or _ADDRESS.match(text, end)
                                    or re.match(r"[가-힣]{2,5}\s*교육감님", text[end:])
                                    or _QUESTION.search(before) or _ADDRESS.search(before)):
            continue
        spans.append((start, end))
    for match in _BARE.finditer(text):
        if any(start <= match.start() < end for start, end in spans) or _inside(match.start(), quotes):
            continue
        before = text[max(0, match.start() - 120):match.start()]
        if _QUESTION.search(before) or _ADDRESS.search(before) or _DIRECTIVE.search(before):
            end = _after_space(text, match.end())
            following = text[end:end + 160]
            if end == len(text) or _RETURN.match(following) or _NEXT_QUESTION.search(following) \
                    or _NEXT_DIRECTIVE.search(following):
                spans.append((match.start(), end))
    return sorted(set(spans))


def call_tail_boundaries(snapshot: dict[str, Any], row: dict[str, Any]) -> list[int]:
    """호명 뒤 독립 질문을 같은 본문 단서로 확인하되 이름은 확정하지 않는다."""
    text = row.get("text") or ""
    anchors = scan_anchors(text, snapshot.get("roster") or [])
    # 명단이 비어도 기존 일반 호명 패턴의 문자 경계만 검증 근거로 쓴다.
    for match in _FLOOR.finditer(text):
        activity = re.search(r"위원님\s*(질의|질문|신문)", match.group())
        if not activity or any(anchor["kind"] == "call" and anchor["end"] == match.end() for anchor in anchors):
            continue
        anchors.append({"kind": "call", "start": match.start(), "end": match.end(),
                        "examination": activity[1] == "신문", "owner_guard": activity[1] == "신문"})
    quotes = _quoted_ranges(text)
    return sorted({anchor["end"] for anchor in anchors if anchor["kind"] == "call"
        and 0 < anchor["end"] < len(text) and not _inside(anchor["start"], quotes)
        and not is_reported_anchor(text, anchor["start"], anchor["end"])
        and not (anchor.get("owner_guard") and new_floor_owner_conflict(row))
        and call_tail_requires_review(text, anchor)})


def boundary_candidates(snapshot: dict[str, Any], row: dict[str, Any], *, limit: int = MAX_BOUNDARIES) -> list[dict[str, Any]]:
    """강한 출처와 응답 양쪽을 우선해 최대 8개의 원문 경계를 반환한다."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_BOUNDARIES:
        raise ValueError("경계 후보 한도는 1~8이어야 합니다")
    text = row.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ValueError("원문은 문자열이어야 합니다")
    if not text:
        return []
    quotes = _quoted_ranges(text)
    units: list[tuple[int, tuple[int, ...], str]] = []

    def add(offsets: list[int], priority: int, reason: str, *, source: bool = False) -> None:
        valid = tuple(sorted(set(offset for offset in offsets if isinstance(offset, int)
            and not isinstance(offset, bool) and 0 < offset < len(text)
            and (source or not _inside(offset, quotes)))))
        if valid:
            units.append((priority, valid, reason))

    mapping = snapshot.get("source_boundaries") or {}
    if not isinstance(mapping, dict):
        mapping = {}
    source = mapping.get(str(row.get("id")), mapping.get(row.get("id"), []))
    if not isinstance(source, (list, tuple)):
        source = []
    for candidate in source or []:
        if isinstance(candidate, dict):
            add([candidate.get("offset")], 0, "수집원 문자 매핑 경계", source=True)
    responses = _short_spans(text, quotes)
    for start, end in responses:
        add([start, end], 1, "짧은 완결 응답의 시작·종료")
    for start, end in enclosed_response_spans(text):
        add([start, end], 1, "직접 질문·확인 사이 인지·판단 응답의 시작·종료")
    # 약한 단서는 강한 후보 뒤에만 선택지로 더하며 역할을 확정하지 않는다.
    for start, end in weak_response_spans(text):
        add([start, end], 4, "약한 응답 단서의 선택 가능 범위")
    for offset in answer_then_new_question_boundaries(text):
        add([offset], 3, "기관 응답 뒤 새 질문 복귀 후보")
    for match in _BARE.finditer(text):
        if _inside(match.start(), quotes) or any(start <= match.start() < end for start, end in responses):
            continue
        before = text[max(0, match.start() - 120):match.start()]
        if _QUESTION.search(before) or _ADDRESS.search(before) or _DIRECTIVE.search(before):
            # 장황한 답변의 첫 예/네는 시작만 후보로 두고 그 뒤 설명을 자르지 않는다.
            add([match.start()], 3, "질문·호명 뒤 응답 시작 후보")
    for pattern, reason in ((_FLOOR, "위원장 진행·호명 전환"), (_SELF, "의원의 자기소개 전환")):
        for match in pattern.finditer(text):
            if pattern is _FLOOR and re.search(r"위원님\s*신문|저도\s*(?:증인\s*)?신문", match.group()) \
                    and is_reported_anchor(text, match.start(), match.end()):
                continue
            if pattern is _SELF and snapshot.get("roster") and not any(
                    anchor["kind"] == "self_intro" and anchor["start"] == match.start()
                    for anchor in scan_anchors(text, snapshot["roster"])):
                continue
            if not _inside(match.start(), quotes):
                add([match.start()], 2, reason)
                ending_reason = "위원장 호명 종결" if pattern is _FLOOR and re.search(r"위원님.*(?:질의|질문|신문)", match.group()) else reason + " 종결"
                add([_after_space(text, match.end())], 3, ending_reason)
    # 발언권 추적과 같은 단서를 써서 짧은 호명·의사진행·재개 안내를 놓치지 않는다.
    procedures_seen = False
    for anchor in scan_anchors(text, snapshot.get("roster") or []):
        if anchor["kind"] == "chair_procedure":
            if not procedures_seen:
                add([anchor["start"]], 2, "명시적 진행 발언 시작 후보")
            procedures_seen = True
            continue  # 같은 진행자의 연속 안내 사이를 새 사람 경계로 만들지 않는다.
        # 다른 발언권 단서를 지났다면 이후 진행 복귀도 독립 경계로 검토한다.
        procedures_seen = False
        add([anchor["start"]], 2, "명단·발언권 단서의 전환 후보")
        add([_after_space(text, anchor["end"])], 3, "명단·발언권 단서의 종결 후보")
    for match in _FORMAL.finditer(text):
        if re.fullmatch(r"(?:답변|말씀)(?:을)?\s*드리겠습니다", match.group()):
            before = text[max(0, match.start() - 120):match.start()]
            if match.start() and not (_QUESTION.search(before) or _ADDRESS.search(before)):
                continue  # 의원 자신의 설명 예고를 새 공식 답변으로 오인하지 않는다.
        add([match.start()], 2, "공식 답변·소명 시작")
    for match in _NEW_QUESTION.finditer(text):
        add([match.start()], 3, "답변 뒤 질문·지적 복귀 후보")
    found: dict[int, str] = {}
    for _priority, offsets, reason in sorted(units, key=lambda unit: (unit[0], unit[1][0], unit[1])):
        fresh = set(offsets) - found.keys()
        if len(found) + len(fresh) > limit:
            continue  # 응답 양쪽을 한 단위로 골라 한쪽만 남기지 않는다.
        for offset in offsets:
            found.setdefault(offset, reason)
    return [{"offset": offset, "reason": reason, "before": text[max(0, offset - 32):offset],
             "after": text[offset:offset + 32]} for offset, reason in sorted(found.items())]


def split_ranges(text: str, offsets: list[int]) -> list[dict[str, int]]:
    """문자 위치만으로 전체 원문을 순서대로 정확히 한 번 나눈다."""
    if not isinstance(text, str) or not isinstance(offsets, list) or len(offsets) > MAX_BOUNDARIES:
        raise ValueError("분리 원문·경계 목록이 잘못되었습니다")
    if any(isinstance(offset, bool) or not isinstance(offset, int) or not 0 < offset < len(text) for offset in offsets):
        raise ValueError("분리 경계는 원문 내부의 정수 위치여야 합니다")
    if offsets != sorted(set(offsets)):
        raise ValueError("분리 경계가 중복됐거나 원문 순서와 다릅니다")
    positions = [0, *offsets, len(text)]
    return [{"start": start, "end": end} for start, end in zip(positions, positions[1:])]


def boundary_plans(text: str, candidates: list[dict[str, Any]]) -> dict[str, list[int]]:
    """응답 양쪽 계획을 우선하고 기존 단일 c번호와 전체 계획을 제한한다."""
    if not isinstance(candidates, list) or any(not isinstance(candidate, dict) for candidate in candidates):
        raise ValueError("경계 후보 목록이 잘못되었습니다")
    offsets = [candidate.get("offset") for candidate in candidates]
    split_ranges(text, offsets)
    if not offsets:
        return {}
    plans: dict[str, list[int]] = {}
    for start, end in _short_spans(text, _quoted_ranges(text)):
        if start in offsets and end in offsets and len(plans) < MAX_PLANS - 2:
            pair = [start, end]
            if pair not in plans.values():
                plans[f"p{len(plans)}"] = pair
    for start, end in enclosed_response_spans(text):
        if start in offsets and end in offsets and len(plans) < MAX_PLANS - 2:
            pair = [start, end]
            if pair not in plans.values():
                plans[f"p{len(plans)}"] = pair
    for start, end in weak_response_spans(text):
        if start in offsets and end in offsets and len(plans) < MAX_PLANS - 2:
            pair = [start, end]
            if pair not in plans.values():
                plans[f"p{len(plans)}"] = pair
    reserved = int(len(offsets) > 1)
    for index, offset in enumerate(offsets):
        if len(plans) >= MAX_PLANS - reserved:
            break
        plans[f"c{index}"] = [offset]
    if reserved:
        plans["all"] = offsets
    return plans
