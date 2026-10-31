"""직접 호명·자기소개로 역할과 실제 발언자 이름을 분리하는 순수 보완 규칙."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .text import PARTIES


_CLOSE = re.compile(r"(?:이상입니다|질의(?:를)?\s*마치겠습니다|발언(?:을)?\s*마치겠습니다|(?:증인\s*)?신문(?:을)?\s*마치겠습니다)[.!?。]*\s*$")
_REPORT = re.compile(r"^(?:라고|라는|라며|이라고|이라는|고\s*(?:말|발언|하|했|지시|요청)|"
                     r"(?:로|으로)\s*(?:호명|말|써|작성)|(?:처럼|같이)\s*(?:호명|말))")
_SELF_QUESTION = re.compile(r"(?:저도|저는|제가)[^.?!。]{0,50}?(?:질의|질문|(?:증인\s*)?신문)(?:를|을)?\s*(?:좀\s*)?(?:드리|하)겠습니다")
_PROCEDURE = re.compile(
    r"좌석(?:을)?\s*정돈해\s*주십시오|감사(?:를)?\s*속개합니다|"
    r"두\s*분\s*다\s*조용히\s*하십시오|신상발언\s*드리겠습니다|"
    r"마이크\s*(?:꺼진|켜지신|켜신)\s*(?:상태|분)[^.?!。]{0,70}(?:말씀|들어|계속)|"
    r"(?:증인|참고인)[^.?!。]{0,60}(?:이석|퇴장)(?:해도|하시|하셔)"
)
_PARTIAL_PROCEDURE = re.compile(r"^의사진행(?:과|에)\s*관련된\s*(?:말|발언)(?:은|만)")
_TRANSITION = re.compile(r"(?:(?:그러면|그럼|다음은|이어서|계속해서|감사합니다)[,\s.!?。]*)+")
_CHAIR_LEAD = re.compile(r"(?:말고\s*)?마이크\s*(?:켜지신|켜신)\s*분\s*"
                         r"말씀(?:을)?\s*(?:좀\s*)?들어\s*주십시오[.!?。]*")
_SOURCE_KEY = re.compile(r"^t?s(?P<num>\d+)(?:\.(?P<occ>\d+))?~(?P<piece>\d+)(?:#(?P<part>\d+))?$")
_REGION_WORD = (r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주|"
                r"국제도시|신도시|[가-힣]{1,10}(?:특별자치시|특별자치도|특별시|광역시|시|도|구|군|동|읍|면|갑|을|병))")
_QUESTION_INTRO_PREFIX = re.compile(
    r"(?:(?:예|네|안녕하세요)[,.!?。]?\s*)?(?:질의|질문|(?:증인\s*)?신문)(?:를|을)?\s*"
    r"(?:시작(?:하도록)?\s*)?(?:하|드리)겠습니다[.!?。]?"
    r"(?:\s+(?:저는|제가))?(?:\s+" + _REGION_WORD + r"){0,6}(?:\s*의)?"
)
_DECLARATION_LEAD = r"(?<![가-힣])(?:(?:그러면|그럼|이제|이상으로)\s*)?"
_QUESTION_DECLARATION = re.compile(
    _DECLARATION_LEAD
    + r"(?P<absence>(?:더\s*이상\s*)?질의하실\s*(?:위원|의원)(?:님)?들?(?:이|께서)?\s*안\s*계시므로\s*)?"
    r"(?P<collective>(?:여러\s*)?(?:위원|의원)(?:님)?들(?:의)?\s*)?"
    r"(?:(?:보충|추가|보완|재)\s*)?질의(?:를)?\s*(?P<all>모두\s*)?"
    r"(?:(?:종결|종료)(?:하도록\s*)?\s*(?:하겠습니다|합니다)|(?P<finished>마치겠습니다|마쳤습니다))"
    r"(?=$|[\s,.!?。])"
)
_QUESTION_INVITATION = re.compile(
    r"(?:(?:추가로|더|혹시)\s*)?질의하실\s*(?:위원|의원)(?:님)?들?\s*(?:더\s*)?계십니까[,.!?。\s]*$"
)
_PAUSE_OR_END_DECLARATION = re.compile(
    _DECLARATION_LEAD + r"(?:잠시\s*)?(?:"
    r"(?:국정\s*)?감사(?:를)?\s*(?:잠시\s*)?(?:"
    r"(?:중지|중단|종료)(?:(?:를)?\s*선포(?:합니다|하겠습니다)|(?:하도록\s*)?\s*(?:합니다|하겠습니다))|"
    r"(?:모두\s*)?마치겠습니다)|"
    r"(?:정회|산회)(?:를)?\s*(?:선포(?:합니다|하겠습니다)|하겠습니다|합니다))"
    r"(?=$|[\s,.!?。])"
)


def is_question_intro_prefix(prefix: str) -> bool:
    """질의 시작과 짧은 지역 수식만 있는 자기소개 앞말을 판별한다.

    실제 이름의 명단 일치는 호출자가 확인한다. 앞의 다른 본문·제삼자 설명을
    함께 허용하지 않아 새 자기소개자의 이름이 이전 발언까지 덮지 않게 한다.
    """
    return bool(_QUESTION_INTRO_PREFIX.fullmatch(prefix.strip()))


def _quoted(text: str, start: int, end: int) -> bool:
    """인용·회고·호명 지시문을 실제 발언권 변경으로 읽지 않는다."""
    before, after = text[:start], text[end:].lstrip()
    if _REPORT.match(after):
        return True
    for opening, closing in (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』")):
        if before.count(opening) > before.count(closing):
            return True
    if before.count('"') % 2 or before.count("'") % 2:
        return True
    if re.search(r"(?:인용문|지시문|대본|예시|문구)\s*[:：]", before):
        return True
    if re.search(r"(?:예를\s*들어|예를\s*들면|가령|이를테면)\s*", before):
        return True
    if re.search(r"(?:호명|소개|말씀)[^.?!。]{0,40}(?:주세요|십시오|하라|해라)\s*[:：]", before):
        return True
    return bool(re.search(r"(?:위원장|의장)(?:께서|이|가)\s*(?:다음(?:은)?\s*)?$", before))


def is_reported_anchor(text: str, start: int, end: int) -> bool:
    """호명·진행 문구의 문자 범위가 인용·회고·지시문 안에 있는지 판단한다."""
    return _quoted(text, start, end)


def reported_examination(text: str, start: int, end: int) -> bool:
    """새 신문 절차는 기사·가정·보고문 속 문구를 현재 호명으로 사용하지 않는다."""
    return bool(_quoted(text, start, end)
        or re.search(r"(?:신문\s*기사|신문(?:을|를)?\s*(?:읽|보니|보면|펴)|기사(?:에는|에서|를|에\s*따르면|의\s*내용)|보도문).{0,200}$", text[:start])
        or re.search(r"(?:신문\s*기사|신문(?:을|를)?\s*(?:읽|보니|보면|펴)|기사(?:에는|에서|를)|보도문|만약|가정(?:해|하|으로)|가상의)[^.?!。]{0,150}$", text[:start])
        or re.match(r"^(?:(?:예|네)\s*)?(?:(?:저는|제가)\s*(?:[가-힣]{2,4}\s*)?증인(?:입니다|으로서|인)|증인(?:인|으로서))", text[:start])
        or re.match(r"\s*이렇게\s*(?:적혀|쓰여|보도|나와)", text[end:]))


def examination_transition(text: str) -> bool:
    """명단 호명 앞의 신문 시작 안내만 인정한다. 단독 발언권 근거는 아니다."""
    return bool(re.fullmatch(r"(?:(?:그러면|그럼)\s*(?:바로\s*)?|지금부터\s*)?"
        r"증인(?:에\s*대한)?\s*신문(?:을|를)?\s*(?:시작|실시|진행|계속)(?:하도록\s*)?\s*하겠습니다[,.!?。]*", text.strip()))


def new_floor_owner_conflict(row: dict[str, Any]) -> bool:
    """명시적인 비진행자 수집원은 새 신문·속개 규칙으로 덮지 않는다."""
    if row.get("speaker_source") == "manual" and row.get("speaker_role") != "chair":
        return True
    hint = str(row.get("speaker_hint") or "").strip()
    if not hint or hint.startswith(("hwa:", "추정:")) or re.search(r"위원장(?:\s*대리)?$", hint):
        return False
    return bool(re.search(r"(?:위원|의원|증인|참고인|장관|차관|사장|원장|청장|이사장|처장|총장|본부장|회장|실장|국장|대표|부장)$", hint)
                or hint.startswith("답변"))


def call_tail_requires_review(text: str, anchor: dict[str, Any]) -> bool:
    """완성 호명 뒤 실제 질문·질의 진행은 공통 본문 근거로 혼합 경계에 남긴다."""
    if anchor.get("kind") != "call":
        return False
    tail = text[anchor["end"]:].strip(" ,.!?。")
    if not tail:
        return False
    if anchor.get("examination"):
        return True
    if (tail.startswith(('“', '‘', '"', "'", '「', '『'))
            or re.match(r"^(?:예를|문구|인용|보고서|대본)", tail)
            or re.match(r"^(?:자문자답|스스로\s*(?:묻|질문|답)|제가\s*묻고)", tail)
            or reported_examination(text, anchor["start"], anchor["end"])):
        return False
    # floor를 가져오는 의미 모듈과의 순환 import를 모듈 초기화 뒤로 미룬다.
    from .role_evidence import role_evidence
    evidence = role_evidence(tail)
    return bool(evidence["mixed"] or evidence["role"] == "member")


def split_call_fragment(text: str, roster: list[dict[str, Any]]) -> dict[str, Any] | None:
    """세 원문에 걸친 호명의 단서만 반환한다. 이것만으로 발언권을 바꾸지 않는다."""
    lead = re.search(r"(?:다음은|다음으로|그다음은|이어서)\s*$", text)
    if lead and len(text) <= 120 and not _quoted(text, lead.start(), lead.end()):
        return {"kind": "lead", "start": lead.start(), "end": lead.end()}
    names = sorted({row["name"] for row in roster if row.get("role") == "member" and row.get("name")}, key=len, reverse=True)
    if names:
        name = re.fullmatch(r"(?P<name>" + "|".join(re.escape(value) for value in names)
                            + r")\s*(?:위원|의원)님[,.!?。]*", text)
        if name:
            return {"kind": "name", "name": name["name"], "start": 0, "end": name.end()}
    finish = re.match(r"(?:(?:보충|추가)\s*)?(?:질의|질문|발언|(?:증인\s*)?신문)(?:을|를)?\s*"
                      r"(?:해\s*주시기\s*바랍니다|해\s*주십시오|해\s*주세요|하시기\s*바랍니다|하십시오)"
                      r"(?=$|[\s,.!?。])", text)
    if finish and not _quoted(text, finish.start(), finish.end()):
        examination = "신문" in finish.group()
        if examination and reported_examination(text, finish.start(), finish.end()):
            return None
        return {"kind": "finish", "start": 0, "end": finish.end(), "examination": examination}
    return None


def call_fragment_order(row: dict[str, Any]) -> tuple:
    """동시각 조각도 수집원 순서로 비교하고 거꾸로 도착한 조각은 합치지 않는다."""
    return _order(row)


def reported_call_context(text: str) -> bool:
    """바로 앞의 명시적인 인용 예고는 새 호명 후보의 시작에서 제외한다."""
    return bool(re.search(r"(?:인용|대본|예시|문구|읽겠|읽어|말씀하셨|말했|발언하셨)[^.?!。]{0,40}[:：]\s*$", text)
                or re.search(r"(?:위원장|의장)(?:께서|이|가)\s*(?:다음과\s*같이|이렇게|아래와\s*같이)\s*"
                             r"(?:말씀|발언)(?:하셨습니다|했습니다|하셨어요)[.!?。]*\s*$", text)
                or text.rstrip().endswith(("“", "‘", "「", "『", '"', "'")))


def _direct_prefix(text: str, start: int) -> bool:
    before = text[:start].strip()
    return not before or bool(re.search(
        r"(?:[.!?。]|그러면|그럼|다음은|이어서|계속해서|수고하셨습니다|감사합니다|이상입니다|하겠습니다|드립니다|겁니다)\s*$", before))


def explicit_chair_declarations(text: str) -> list[dict[str, Any]]:
    """직접 선언의 원문 좌표만 반환한다. 실제 진행자·사람 보호는 호출자가 확인한다.

    개인의 질의 마침은 전체 질의 종료와 구별한다. 앞의 질의자 부재 설명도
    같은 선언 범위에 넣어 원자막에서 종료 경계를 검증할 때 빠지지 않게 한다.
    """
    declarations = []
    for kind, pattern in (("explicit_chair_declaration", _QUESTION_DECLARATION),
                          ("explicit_pause_or_end", _PAUSE_OR_END_DECLARATION)):
        for match in pattern.finditer(text):
            direct = _direct_prefix(text, match.start())
            if not direct and kind == "explicit_chair_declaration" and match["absence"]:
                invitation = _QUESTION_INVITATION.search(text[:match.start()])
                direct = bool(invitation and _direct_prefix(text, invitation.start())
                              and not _quoted(text, invitation.start(), invitation.end()))
            if _quoted(text, match.start(), match.end()) or not direct:
                continue
            if kind == "explicit_chair_declaration" and match["finished"] \
                    and not any(match[name] for name in ("absence", "collective", "all")):
                continue
            declarations.append({"kind": kind, "start": match.start(), "end": match.end()})
    return sorted(declarations, key=lambda value: (value["start"], value["end"]))


def closed_short_calls(text: str, roster: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """종료 인사와 바로 이어진 명단 의원의 독립 호명만 진행 경계로 인정한다."""
    members = sorted({row["name"] for row in roster if isinstance(row, dict)
                      and row.get("role") == "member" and isinstance(row.get("name"), str)
                      and row["name"]}, key=len, reverse=True)
    if not members:
        return []
    names = "(?:" + "|".join(re.escape(name) for name in members) + ")"
    pattern = re.compile(r"(?<![가-힣])(?:(?P<finished>" + names + r")\s*(?:위원|의원)(?:님)?\s*)?"
        r"수고\s*(?:하셨습니다|많으셨습니다)[,.!?。]*\s*"
        r"(?:(?:다음(?:은|으로)?|이어서)\s*)?(?P<name>" + names + r")\s*(?:위원|의원)님"
        r"[,.!?。]*[ \t]*(?=$|\n)")
    out = []
    for match in pattern.finditer(text):
        before = text[:match.start()].rstrip()
        # 의원이 기관장에게 한 인사와 뒤의 동료 이름을 진행자 교대로 묶지 않는다.
        if (not match.group("finished") and re.search(
                r"(?:장관|차관|사장|원장|청장|본부장|실장|국장|위원장|이사장|대표)(?:님)?[,.!?。]*$", before)):
            continue
        # 전체 블록 앞부분의 끝난 예시가 뒤의 실제 호명까지 막지 않게 인용 예고는 근처만 본다.
        # 다만 닫히지 않은 인용부호는 전체 앞말로 확인한다.
        quote_prefix = text[:match.start()]
        unclosed = any(quote_prefix.count(a) > quote_prefix.count(b)
                       for a, b in (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』")))
        unclosed = unclosed or bool(quote_prefix.count('"') % 2 or quote_prefix.count("'") % 2)
        window = max(0, match.start() - 160)
        if (unclosed or _quoted(text[window:], match.start() - window, match.end() - window)
                or _quoted(text[window:], match.start("name") - window, match.end() - window)):
            continue
        out.append({"kind": "call", "start": match.start(), "end": match.end(),
                    "speaker": match["name"], "role": "member", "caller_role": "chair",
                    "message": "종료 인사에 바로 이어진 명단 의원의 짧은 호명",
                    "origin": "closed_short_call", "owner_guard": True})
    return out


def completed_closing_call(text: str, roster: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """종료 인사 직후의 완전 명단 호명이 행 끝에서 마감됐는지 확인한다."""
    for anchor in reversed(scan_anchors(text, roster)):
        if (anchor["kind"] != "call" or anchor["speaker"] != name or anchor.get("requires_context")
                or text[anchor["end"]:].strip(" ,.!?。\n\t")):
            continue
        prefix = text[:anchor["start"]]
        closing = re.search(r"수고\s*(?:하셨습니다|많으셨습니다)[,.!?。]*\s*"
                            r"(?:(?:다음(?:은|으로)?|이어서)\s*)?$", prefix)
        if closing and not _quoted(text, closing.start(), anchor["end"]):
            return {**anchor, "closing_start": closing.start()}
    return None


def scan_anchors(text: str, roster: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """명단과 정확히 일치하는 발언권 단서와 원문 문자 위치를 반환한다.

    call의 speaker·role은 호명 대상이며 caller_role은 진행자다. 짧은 호명과
    자기 질의는 requires_context가 참이므로 앞 진행 문맥 없이 확정하지 않는다.
    """
    people = {entry["name"]: entry.get("role", "member") for entry in roster
              if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]}
    members = sorted((name for name, role in people.items() if role == "member"), key=len, reverse=True)
    chairs = [name for name, role in people.items() if role == "chair"]
    chair = chairs[0] if len(chairs) == 1 else "위원장"
    anchors: list[dict[str, Any]] = []

    def append(kind: str, match: re.Match, speaker: str, role: str, message: str, **extra: Any) -> None:
        if not _quoted(text, match.start(), match.end()):
            anchors.append({"kind": kind, "start": match.start(), "end": match.end(),
                            "speaker": speaker, "role": role, "message": message, **extra})

    if members:
        names = "(?:" + "|".join(re.escape(name) for name in members) + ")"
        call = re.compile(r"(?<![가-힣])(?P<name>" + names + r")\s*(?:위원|의원)(?:님)?\s*"
            r"(?:(?:(?:의사진행|신상|보충|추가)\s*)?(?P<turn>발언|질의|질문|(?:증인\s*)?신문)(?:을|를)?\s*"
            r"(?:해\s*주시기\s*바랍니다|해\s*주십시오|해\s*주세요|하십시오|하시기\s*바랍니다|하시죠|드리겠습니다)"
            r"|말씀(?:을)?\s*계속(?:하십시오|해\s*주십시오|해\s*주세요|해\s*주시기\s*바랍니다))")
        for match in call.finditer(text):
            if _direct_prefix(text, match.start()) or _CHAIR_LEAD.fullmatch(text[:match.start()].strip()):
                examination = bool(match["turn"] and "신문" in match["turn"])
                if examination and reported_examination(text, match.start(), match.end()):
                    continue
                extra = {"examination": True, "owner_guard": True} if examination else {}
                append("call", match, match["name"], "member", "원문의 직접 호명", caller_role="chair", **extra)
        thanks = re.compile(r"(?<![가-힣])(?P<name>" + names + r")\s*(?:위원|의원)(?:님)?\s*수고\s*(?:하셨습니다|많으셨습니다)")
        for match in thanks.finditer(text):
            if _direct_prefix(text, match.start()):
                append("chair_return", match, chair, "chair", "위원 질의 종료 뒤 진행자 발언",
                       finished_member=match["name"])
        bare = re.compile(r"(?<![가-힣])(?P<name>" + names + r")\s*(?:위원|의원)님[.!?。]*\s*$")
        for match in bare.finditer(text):
            party_prefix = re.fullmatch(r"(?:(?:다음은|다음으로|그다음은|이어서)\s*)?(?:"
                + "|".join(map(re.escape, PARTIES)) + r")(?:의)?", text[:match.start()].strip())
            if (_direct_prefix(text, match.start()) or party_prefix) and not any(a["start"] <= match.start() < a["end"] for a in anchors):
                preceding_close = bool(re.search(r"(?:이상입니다|말씀\s*드립니다|드립니다|겁니다)[.!?。]*\s*$", text[:match.start()]))
                previous_count = len(anchors)
                append("call", match, match["name"], "member", "독립된 짧은 호명 후보", caller_role="chair",
                       requires_context=True, after_close=preceding_close)
                if party_prefix and len(anchors) > previous_count:
                    anchors[-1]["start"] = 0  # 정당 접두어도 같은 직접 호명의 문자 범위다.
    if people:
        names = "(?:" + "|".join(re.escape(name) for name in sorted(people, key=len, reverse=True)) + ")"
        intro = re.compile(r"(?<![가-힣])(?P<name>" + names + r")\s*(?:위원장|위원|의원)?입니다(?=$|[\s.!?。])")
        for match in intro.finditer(text):
            before = text[:match.start()].strip()
            after_call = any(anchor["kind"] == "call" and anchor["end"] <= match.start()
                             and not text[anchor["end"]:match.start()].strip() for anchor in anchors)
            if (not before or after_call or re.search(r"(?:저는|제가|안녕하세요|의)\s*$", before)
                    or (people[match["name"]] == "member" and is_question_intro_prefix(before))):
                append("self_intro", match, match["name"], people[match["name"]], "명단과 일치하는 직접 자기소개")
    for match in _PROCEDURE.finditer(text):
        if _direct_prefix(text, match.start()) or match.start() == 0:
            append("chair_procedure", match, chair, "chair", "발언권·회의 진행에 관한 명시적 안내")
    for declaration in explicit_chair_declarations(text):
        start = declaration["start"]
        invitation = (_QUESTION_INVITATION.search(text[:start])
                      if declaration["kind"] == "explicit_chair_declaration" else None)
        # 문장부호 없이 붙은 추가질의 초대도 같은 진행자 발언이다.
        # 선언 helper의 경계 좌표는 유지하고 화자 판정 범위만 초대문까지 포함한다.
        if invitation:
            start = invitation.start()
        anchors.append({"kind": "chair_procedure", "start": start, "end": declaration["end"],
                        "speaker": chair, "role": "chair", "message": "질의 종결·정회·감사 종료의 명시적 선언",
                        "owner_guard": True, "closes_question": True,
                        "declaration_kind": declaration["kind"]})
    for match in _SELF_QUESTION.finditer(text):
        examination = "신문" in match.group()
        if examination and reported_examination(text, match.start(), match.end()):
            continue
        extra = {"examination": True, "owner_guard": True} if examination else {}
        append("chair_question", match, chair, "chair", "진행자가 자신의 질의를 시작하는 후보", requires_context=True, **extra)
    resume = re.search(r"(?:의석|좌석)(?:을)?\s*정돈해\s*(?:주십시오|주시기\s*바랍니다)[,.!?。\s]+"
        r"국정감사(?:를)?\s*(?:계속|속개)(?:하도록\s*)?\s*하겠습니다[.!?。\s]*$", text)
    if (resume and len(text) <= 160 and len(text[:resume.start()].split()) <= 1
            and not reported_examination(text, resume.start(), resume.end())):
        anchors.append({"kind": "chair_procedure", "start": 0, "end": resume.end(), "speaker": chair,
            "role": "chair", "message": "의석 정돈과 국정감사 재개를 함께 지시한 진행", "owner_guard": True})
    return sorted(anchors, key=lambda anchor: (anchor["start"], anchor["end"]))


def _protected(snapshot: dict[str, Any], row: dict[str, Any]) -> bool:
    block = snapshot.get("block") or {}
    return bool(block.get("review_status") == "reviewed" or block.get("edited_summary")
                or row.get("speaker_source") == "manual" or row.get("edited") or row.get("edited_by")
                or row.get("speaker_role") == "system"
                or (row.get("original_text") is not None and row.get("text") != row.get("original_text")))


def _order(row: dict[str, Any]) -> tuple:
    order = row.get("source_order")
    if isinstance(order, str) and order:
        try:
            return (0, *(int(value) for value in order.split(":")))
        except ValueError:
            pass
    match = _SOURCE_KEY.fullmatch(str(row.get("source_key") or ""))
    if match:
        return (0, *(int(match[name] or 0) for name in ("num", "occ", "piece", "part")))
    return (1, row.get("ts_start") or row.get("seq") or row["id"], row["id"])


def resolve_floor(snapshot: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, Any]:
    """역할만으로 실명을 만들지 않고 원문의 발언권 단서로 부분 이름을 보완한다."""
    output = deepcopy(parts)
    targets = {row["id"]: row for row in snapshot.get("utterances", [])}
    rows = {row["id"]: row for row in snapshot.get("context_utterances", [])}
    rows.update(targets)
    roster = snapshot.get("roster") or []
    chair_names = [entry["name"] for entry in roster if entry.get("role") == "chair"]
    chair = chair_names[0] if len(chair_names) == 1 else "위원장"
    member_names = {entry["name"] for entry in roster if entry.get("role") == "member"}
    by_uid: dict[int, list[dict[str, Any]]] = {}
    for part in output:
        uid, start, end = part.get("utterance_id"), part.get("start"), part.get("end")
        if uid not in targets or any(isinstance(n, bool) or not isinstance(n, int) for n in (start, end)) \
                or not 0 <= start <= end <= len(targets[uid].get("text") or ""):
            raise ValueError("발언권 보완의 원문 식별자·문자 범위가 잘못되었습니다")
        by_uid.setdefault(uid, []).append(part)
    issues, evidence = [], []
    active: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    ended = False
    unverified: set[str] = set()
    changed_groups: set[str] = set()

    def issue(kind: str, uid: int, start: int, end: int, message: str) -> None:
        issues.append({"kind": kind, "utterance_id": uid, "start": start, "end": end, "message": message})

    for row in sorted(rows.values(), key=_order):
        uid, full_text = row["id"], row.get("text") or ""
        row_parts = sorted(by_uid.get(uid) or [{"start": 0, "end": len(full_text), "role": row.get("speaker_role")}],
                           key=lambda part: part["start"])
        for part in row_parts:
            start, end = part["start"], part["end"]
            text = full_text[start:end]
            before = active
            # 분리된 조각만 보면 원행의 열린 인용부호·회고 문맥이 사라질 수 있다.
            anchors = [anchor for anchor in scan_anchors(text, roster)
                       if not is_reported_anchor(full_text, start + anchor["start"], start + anchor["end"])]
            used = []
            for anchor in anchors:
                if anchor.get("owner_guard") and new_floor_owner_conflict(row):
                    continue
                if anchor.get("requires_context"):
                    valid = ((anchor["kind"] == "call" and (part.get("role") == "chair" or ended
                              or anchor.get("after_close") or (active and active["role"] == "chair")))
                             or (anchor["kind"] == "chair_question" and active and active["role"] == "chair" and not pending))
                    if not valid:
                        continue
                used.append(anchor)
                evidence.append({**anchor, "utterance_id": uid, "start": start + anchor["start"], "end": start + anchor["end"]})
                if anchor["kind"] == "call":
                    active = {"speaker": chair, "role": "chair"}
                    pending = {"speaker": anchor["speaker"], "role": "member"}
                elif anchor["kind"] == "self_intro":
                    active = {"speaker": anchor["speaker"], "role": anchor["role"]}
                    pending = None
                else:
                    active = {"speaker": chair, "role": "chair"}
                    if anchor["kind"] != "chair_question":
                        pending = None
                ended = False
            target = uid in targets
            protected = _protected(snapshot, row)
            roles = {"chair" if a["kind"] == "call" else a["role"] for a in used}
            leading = text[:used[0]["start"]].strip() if used else ""
            transition_only = not leading or bool(_TRANSITION.fullmatch(leading)) \
                or bool(_CHAIR_LEAD.fullmatch(leading)) or bool(used and used[0].get("examination") and examination_transition(leading))
            identities = {(a["role"], a["speaker"]) for a in used if a["kind"] == "self_intro"}
            before_member = part.get("role") == "member" or bool(before and before["role"] == "member")
            before_nonchair = part.get("role") in {"member", "answer"} or bool(
                part.get("role") != "chair" and before and before["role"] == "member")
            mixed = len(roles) > 1 or len(identities) > 1 or bool(
                used and not transition_only and before_nonchair and roles == {"chair"}) or bool(
                before_member and any(anchor["kind"] == "self_intro" and anchor["start"] > 100
                                      for anchor in used))
            mixed = mixed or any(call_tail_requires_review(text, anchor) for anchor in used)
            desired = None
            if mixed:
                if target:
                    issue("floor_mixed", uid, start, end, "같은 부분에 호명·새 자기소개 등 서로 다른 발언권 근거가 있어 먼저 경계를 확인해야 합니다")
            elif used and roles == {"chair"}:
                desired = {"role": "chair", "speaker": chair}
            elif used and roles == {"member"}:
                if part.get("role") == "member":
                    desired = {"role": "member", "speaker": active["speaker"]}
                elif target:
                    issue("floor_role_conflict", uid, start, end, "명단 자기소개와 기존 부분 역할이 충돌하여 실명만 강제로 붙이지 않습니다")
            elif pending and part.get("role") == "member" and not _PARTIAL_PROCEDURE.search(text.strip()):
                active, pending = pending, None
                desired = {"role": "member", "speaker": active["speaker"]}
                evidence.append({"kind": "called_floor", "utterance_id": uid, "start": start, "end": end,
                                 **desired, "message": "앞의 직접 호명에 이어 시작된 위원 발언"})
            elif active and not pending and part.get("role") == "member":
                if active["role"] == "chair" and re.match(r"^(?:존경하는\s*)?위원장(?:님|께서)", text.strip()):
                    active = None
                else:
                    desired = {"role": active["role"], "speaker": active["speaker"]}
            if target and not protected and desired:
                changed = any(part.get(name) != value for name, value in desired.items())
                if changed:
                    if part.get("group_id"):
                        changed_groups.add(part["group_id"])
                    part.update(desired)
                    part.pop("confidence", None)
                    part.pop("probability", None)
                    issue("floor_resolved", uid, start, end, "직접 호명·자기소개·진행 문맥으로 이름 또는 위원장 역할을 보완했습니다. 모델의 화자 확률로 해석하지 않습니다")
            elif target and not protected and part.get("role") == "member" and not desired:
                name = str(part.get("speaker") or "위원")
                if name not in unverified:
                    unverified.add(name)
                    issue("floor_unverified", uid, start, end, "직접 실명 근거가 없어 기존 추정 표시만 유지합니다. 블록 질문자나 역할을 실명 확정 근거로 쓰지 않습니다")
            if protected and row.get("speaker_source") == "manual" and row.get("speaker") in member_names | {chair}:
                active = {"speaker": row["speaker"], "role": row.get("speaker_role")}
                pending = None
            close = _CLOSE.search(text)
            if (part.get("role") == "member" and close and not any(a["kind"] == "call" for a in used)
                    and not ("신문" in close.group() and (new_floor_owner_conflict(row)
                                                         or reported_examination(text, close.start(), close.end())))):
                active = None
                ended = True
    groups: dict[str, list[dict[str, Any]]] = {}
    for part in output:
        if part.get("group_id"):
            groups.setdefault(part["group_id"], []).append(part)
    for group_id, grouped in groups.items():
        if group_id in changed_groups and len({(part.get("role"), part.get("speaker")) for part in grouped}) > 1:
            for part in grouped:
                if not _protected(snapshot, targets[part["utterance_id"]]):
                    part.pop("group_id", None)
            first = grouped[0]
            issue("floor_group_split", first["utterance_id"], first["start"], first["end"],
                  "같은 연결 묶음 안에 실제 이름·역할이 달라지는 근거가 있어 묶음 식별자를 해제했습니다")
    return {"parts": output, "issues": issues, "evidence": evidence}
