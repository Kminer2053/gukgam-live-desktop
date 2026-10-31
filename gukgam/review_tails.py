"""다른 역할의 다음 행에 붙은 짧은 문장 종결을 원문 범위 후보로 찾는다.

문법상 이어질 수 있다는 근거만 반환한다. 실제 동일인 판단과 역할 반영은 호출자가 한다.
"""
from __future__ import annotations

import re
from typing import Any

from .review_boundaries import _quoted_ranges

_ROLES = {"chair", "member", "answer"}
_FIELDS = ("utterance_id", "start", "end", "role", "speaker")
_PREFIX = re.compile(
    r"^\s*(?P<ending>하시더라고요|하더라고요|하겠습니다|했습니다|하더라도|하십니다|합니다|"
    r"있습니다|없습니다|되었습니다|됐습니다|됩니다|것처럼|것으로|것입니다|겁니다|겠습니다|습니다)"
    r"[,.!?。…]*(?=\s|$)\s*"
)
_COMPLETE = re.compile(
    r"(?:[.!?。…]|습니다|십니다|입니다|습니까|십니까|입니까|합니까|나요|가요|까요|"
    r"시죠|했죠|그렇죠|하지요|있지요|었지요|했어요|있어요|없어요|이에요|예요|아닙니까)\s*$"
)
_ACK = re.compile(r"^\s*(?:예|네|아니요|아닙니다|맞습니다|그렇습니다|동의합니다|알겠습니다|확인하겠습니다)(?=\s|$|[.!?。])")
_OBJECT = re.compile(r"(?:[가-힣]{2,20}(?:을|를)|하도록|하기로|해서|하고|하여|하며|그렇게|아무리)\s*$")
_SUBJECT = re.compile(r"(?:[가-힣]{2,20}(?:이|가|은|는)|수)\s*$")
_ADNOMINAL = re.compile(r"[가-힣]{2,20}(?:신|한|할|된|던|는|은)\s*$")
_STEM = re.compile(r"(?:하겠|되겠|였|했|됐|있|없|왔|하셨|하십|합)\s*$")


def _protected(snapshot: dict[str, Any], row: dict[str, Any]) -> bool:
    block = snapshot.get("block") or {}
    return bool(block.get("review_status") == "reviewed" or block.get("edited_summary")
        or row.get("speaker_source") == "manual" or row.get("edited") or row.get("edited_by")
        or row.get("speaker_role") == "system"
        or (row.get("original_text") is not None and row.get("text") != row.get("original_text")))


def _valid(part: Any, rows: dict[int, dict[str, Any]]) -> bool:
    if not isinstance(part, dict) or part.get("role") not in _ROLES or not isinstance(part.get("speaker"), str):
        return False
    uid, start, end = part.get("utterance_id"), part.get("start"), part.get("end")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (uid, start, end)) or uid not in rows:
        return False
    text = rows[uid].get("text")
    return isinstance(text, str) and 0 <= start < end <= len(text)


def _compatible(before: str, ending: str) -> bool:
    if ending in {"것처럼", "것으로", "것입니다", "겁니다"}:
        return bool(_ADNOMINAL.search(before))
    if ending in {"있습니다", "없습니다", "됩니다", "됐습니다", "되었습니다"}:
        return bool(_SUBJECT.search(before))
    if ending in {"습니다", "겠습니다"}:
        return bool(_STEM.search(before))
    return bool(_OBJECT.search(before) or _STEM.search(before))


def prefix_candidates(snapshot: dict[str, Any], parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """인접한 대상 행의 서로 다른 역할에서 짧은 종결 prefix만 제안한다.

    prefix_end는 다음 원문의 Python 문자 인덱스(끝 제외)다. 뒤의 공백도 범위에
    포함하므로 선택해 나누더라도 모든 원문 문자는 정확히 한 번 남는다.
    """
    if not isinstance(parts, list):
        raise ValueError("발언 부분 목록이 필요합니다")
    original_rows = snapshot.get("utterances") or []
    rows = {row["id"]: row for row in original_rows}
    order = {row["id"]: index for index, row in enumerate(original_rows)}
    result = []
    for left, right in zip(parts, parts[1:]):
        if not _valid(left, rows) or not _valid(right, rows):
            continue
        a, b = left["utterance_id"], right["utterance_id"]
        if a == b or order[b] != order[a] + 1 or left["role"] == right["role"]:
            continue
        if _protected(snapshot, rows[a]) or _protected(snapshot, rows[b]):
            continue
        if left["end"] != len(rows[a]["text"]) or right["start"] != 0:
            continue
        before = rows[a]["text"][left["start"]:left["end"]]
        after = rows[b]["text"][:right["end"]]
        stripped = before.rstrip()
        if not stripped or _COMPLETE.search(stripped) or _ACK.match(after):
            continue
        # 인용·가정의 끝말을 실제 다른 발언의 종결로 옮기지 않는다.
        if any(start <= len(stripped) - 1 < end for start, end in _quoted_ranges(before)):
            continue
        match = _PREFIX.match(after)
        if not match or match.end() >= right["end"] or not _compatible(stripped, match["ending"]):
            continue
        first_letter = len(after) - len(after.lstrip())
        if any(start <= first_letter < end for start, end in _quoted_ranges(after)):
            continue
        result.append({"left": {key: left[key] for key in _FIELDS},
            "right": {key: right[key] for key in _FIELDS}, "prefix_end": match.end(),
            "reason": "앞 행의 미완결 구절과 다음 행의 짧은 종결이 문법상 이어질 수 있음·동일 화자 확정 아님"})
    return result
