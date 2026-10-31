"""명단에 근거한 진행 안내와 직접 자기소개를 원문 문자 범위로 나눈다."""
from __future__ import annotations

import re

from .floor import is_reported_anchor, is_question_intro_prefix
from .text import PARTIES


_REQUEST = re.compile(
    r"^\s*(?:(?:보충|추가)\s*)?(?:질의|질문|발언)(?:을|를)?\s*"
    r"(?:해\s*주시기\s*바랍니다|해\s*주십시오|해\s*주세요|하시기\s*바랍니다|하십시오)"
    r"[.!?。]*(?=$|\s)"
)
_REGION = re.compile(r"^(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)(?:\s|$)")
_OTHER_PERSON = re.compile(r"소개|출석|참석|사람|이분|저분|그분|분은|분이|분께|동료|선배|후배|친구|대표|관계자|저희|우리|의원|위원|말씀|발언|인용|예를|보고서|대본")
_INTRO_REPORT = re.compile(
    r"^(?:라고|라는|라며|이라고|이라는|"
    r"하(?:고|며|면서)\s*(?:말|소개|적혀|적힌|기재|쓰여|쓰인|써|표현|밝혔|설명))"
)



def _intro_prefix(prefix: str) -> bool:
    """짧은 본인·정당·지역 소개만 허용하고 제삼자 설명은 제외한다."""
    prefix = prefix.strip()
    if not prefix or prefix in {"저는", "제가", "안녕하세요"} or is_question_intro_prefix(prefix):
        return True
    if _OTHER_PERSON.search(prefix) or re.search(r"[.!?。:：“”‘’「」『』\"']", prefix):
        return False
    for party in PARTIES:
        if prefix == party or prefix == party + "의":
            return True
    if not _REGION.match(prefix):
        return False
    words = prefix.split()
    return (len(words) <= 6 and all(re.fullmatch(r"[가-힣]{1,10}", word) for word in words)
            and not any(word.endswith(("은", "는", "할", "한", "인", "하는", "하신")) for word in words))


def _short_call_intro_parts(text: str, member_names: set[str], expected_name: str | None
                            ) -> list[tuple[str, str | None]] | None:
    """짧은 직접 호명 직후 같은 명단 이름으로 본인을 소개한 경우만 나눈다."""
    # 직함 없는 짧은 이름은 일반 어절·동명이인과 구분할 근거가 부족하다.
    names = [name for name in member_names if re.fullmatch(r"[가-힣]{3,4}", name)]
    if not names:
        return None
    pattern = (r"^\s*(?:(?:다음은|다음으로|그다음은|이어서)\s*)?"
               r"(?:(?:" + "|".join(map(re.escape, PARTIES)) + r")(?:의)?\s+)?"
               r"(?P<name>" + "|".join(map(re.escape, sorted(names, key=len, reverse=True)))
               + r")\s*(?:위원|의원)님[,.!?。]*")
    call = re.match(pattern, text)
    if not call or expected_name and call["name"] != expected_name:
        return None
    tail = text[call.end():]
    intro = re.match(r"\s+(?:(?:저는|제가)\s+)?" + re.escape(call["name"])
                     + r"\s*(?:위원|의원)?입니다(?=$|[\s.!?。])", tail)
    if (not intro or is_reported_anchor(text, call.start(), call.end())
            or is_reported_anchor(text, call.end() + intro.start(), call.end() + intro.end())
            or _INTRO_REPORT.match(tail[intro.end():].lstrip(" \t\r\n,.!?。…"))):
        return None
    return [(text[:call.end()], "추정:위원장"), (tail, f"추정:{call['name']} 위원")]


def direct_handover_parts(text: str, member_names: set[str], *, expected_name: str | None = None,
                          allow_short_call: bool = True
                          ) -> list[tuple[str, str | None]] | None:
    """명백한 독립 안내 또는 안내→자기소개만 반환하며 원문 문자를 보존한다.

    이름·역할은 추정 힌트다. 본문에 없는 이름을 만들거나 이웃 문장을 합치지 않는다.
    기존 호명 대상이 있으면 자기소개 이름도 일치해야 한다.
    """
    if not member_names:
        return None
    if allow_short_call:
        paired = _short_call_intro_parts(text, member_names, expected_name)
        if paired:
            return paired
    names = "(?:" + "|".join(re.escape(name) for name in sorted(member_names, key=len, reverse=True)) + ")"
    bare = re.fullmatch(r"\s*(?:다음은|다음으로|그다음은|이어서)\s*" + names
                        + r"\s*(?:위원|의원)님[,.!?。]*\s*", text)
    if bare and not is_reported_anchor(text, bare.start(), bare.end()):
        return [(text, "추정:위원장")]
    request = _REQUEST.match(text)
    if not request or is_reported_anchor(text, request.start(), request.end()):
        return None
    tail = text[request.end():]
    intro = re.search(r"(?<![가-힣])(?P<name>" + names + r")\s*(?:위원|의원)입니다(?=$|[\s.!?。])", tail[:120])
    if (not intro or not _intro_prefix(tail[:intro.start()])
            or expected_name and intro["name"] != expected_name
            or is_reported_anchor(text, request.end() + intro.start(), request.end() + intro.end())
            or _INTRO_REPORT.match(tail[intro.end():].lstrip(" \t\r\n,.!?。…"))):
        return None
    return [(text[:request.end()], "추정:위원장"),
            (tail, f"추정:{intro['name']} 위원")]
