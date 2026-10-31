"""기계 화자 색상과 독립한 직접 질문·업무 응답·혼합 발언의 보수적 근거."""
from __future__ import annotations

import re

from .floor import is_reported_anchor
from .text import norm


_TITLE = r"(?:장관|차관|사장|원장|청장|이사장|처장|총장|본부장|실장|국장|회장)님"
_QUESTION = re.compile(r"(?:습니까|십니까|입니까|합니까|됩니까|겁니까|아닙니까|나요|가요|까요|무엇이죠|시죠|했죠|됐죠|계시죠|그렇죠|있지요|없지요|"
                       r"(?:계시|했|하시|아시|들으셨)지요|으세요|생각하세요|하시겠어요|있어요\?)[?!。]*")
_QUESTION_END = re.compile(_QUESTION.pattern + r"\s*(?:" + _TITLE + r")?[,.!?。]*\s*$")
_INFORMATION_QUESTION = re.compile(r"(?<![가-힣])(?:어떤|어느|무슨|누가|왜|어떻게|"
                                   r"무엇(?:이|을|에|으로)?|누구(?:를|에게|한테|의|와)?|"
                                   r"언제(?:부터|까지)?|어디(?:에서|에|로|까지|부터|가|는|를)?|얼마(?:나|큼)?)(?=\s)"
                                   r"[^.?!。]{1,90}?(?:죠|지요)(?=$|[\s,.!?。])[?!。]*")
_INDIRECT_INFORMATION = re.compile(r"(?:는지|은지|인지|을지|일지|는가|인가|건가|이냐|느냐|을까|"
                                  r"든지|든|더라도|아도|어도|여도|와도|해도)(?:를|는|도|가)?(?=$|[\s,])")
_INFORMATION_CLAUSE_END = re.compile(r"(?:습니다|입니다|거든요|는데요|했어요|해요|이에요|예요|없어요|있어요|"
                                    r"거예요|잖아요|까요|가요|래요|라고요)(?=\s)")
_REPORTED_INFORMATION = re.compile(r"(?:기사|보도문|보고서|신문)(?:(?:를|을)?\s*(?:읽|인용|보면)|"
                                  r"에는|에서는|의\s*질문은)[^.?!。]{0,130}$")
_QUESTION_SELF = re.compile(r"^(?:(?:예|네|우선|먼저|현재|지금|그래서)\s*){0,3}"
                            r"(?:저희(?:가|는|도|들이)?|우리(?:가|는)?|제가|저는)(?=\s|[은는이가])")
_QUESTION_FRAGMENT_END = re.compile(r"(?:습니까|십니까|입니까|합니까|됩니까|겁니까|아닙니까|나요|시죠|했죠|됐죠|계시죠|그렇죠|있지요|없지요|"
                                    r"(?:계시|했|하시|아시|들으셨)지요)[?!。]*\s+"
                                    r"(?:지금|우선|그러면|잠깐|이제)[,.!?。]*\s*$")
_INTERIOR_CONFIRM = re.compile(r"(?:계시|했|하시|아시|들으셨)지요[?!。]*")
_REQUEST = re.compile(r"(?:제출|보고|조사|확인|검토|개선|조치|답변|설명|관리|파악|착수)[^.?!。]{0,65}"
                      r"(?:주십시오|주시기\s*바랍니다|주세요|하십시오|하세요|하셔야\s*합니다)[,.!?。]*\s*$")
_ADDRESSED_REQUEST = re.compile(_TITLE + r"[^.?!。]{0,120}말씀(?:을)?\s*해\s*주십시오[,.!?。]*\s*$")
_REQUEST_CLAUSE = re.compile(r"(?:제출|보고|조사|확인|검토|개선|조치|답변|설명|관리|파악|착수)[^.?!。]{0,65}?"
                             r"(?:주십시오|주시기(?:를)?\s*(?:바랍니다|바라고요)|주세요|하십시오|하세요|하셔야\s*합니다)"
                             r"(?=$|[\s,.!?。])")
_ADDRESSED_APPEARANCE = re.compile(_TITLE + r"\s*(?:(?:앞으로|잠깐|잠시|좀)\s*){0,2}"
                                  r"(?:나와|나오셔서|출석해)\s*주(?:십시오|시고요|세요|시기(?:를)?\s*(?:바랍니다|바라고요))"
                                  r"(?=$|[\s,.!?。])")
_REQUEST_BOUNDARY = re.compile(r"(?:주십시오|주세요|주시기(?:를)?\s*(?:바랍니다|바라고요)|"
                               r"하시(?:기|길|기를)\s*바랍니다)(?=$|[\s,.!?。])")
_REQUEST_ACCEPTANCE = re.compile(r"(?:(?:그렇게\s*(?:하도록\s*)?){1,2}하겠습니다|"
                                 r"(?:확인|검토|조사)(?:하고|해서|하여)\s*"
                                 r"(?:제출|보고|조치|처리)(?:하도록\s*하|하|드리)겠습니다)(?=$|[\s,.!?。])")
_ADDRESSED_RESPONSE = re.compile(r"위원님\s*(?:제가|저희(?:가|는|들이)?)\s+")
_BARE_COMMITMENT_TAIL = re.compile(r"^(?:하겠습니다|하도록\s*하겠습니다)(?=$|[\s,.!?。])")
_CONFIRMING_STATEMENT = re.compile(r"(?:군요|거군요)[.!?。]*(?=$|\s)")
_KNOWLEDGE_RESPONSE_END = re.compile(r"(?:(?:알고|모르고)\s*있습니다|알았습니다|몰랐습니다|"
                                     r"(?:생각|인식)(?:을)?\s*하고\s*있습니다|그렇습니다|맞습니다|아닙니다)"
                                     r"(?=$|[\s,.!?。])")
_RESPONSE_OWN_EXPLANATION = re.compile(r"(?<![가-힣])(?:제가|저는|저도|본\s*위원(?:은|이)?|"
                                       r"(?:저희|우리)(?:의)?\s*의원실)(?=$|[\s은는이가의에서])")
_CLARIFYING_REPLY = re.compile(r"(?:뜻|의미)(?:입니까|인가요)|이해하면\s*됩니까")
_PROGRESS_ACT = r"(?:질의|질문|(?<![가-힣])(?:증인\s*)?신문)"
_PROGRESS = re.compile(_PROGRESS_ACT + r"(?:를|을)?\s*(?:계속|진행|시작|더|좀)?\s*"
                       r"(?:하|드리|이어\s*가)(?:도록\s*하)?(?:겠습니다|겠어요)[,.!?。]*\s*$|"
                       r"(?:묻겠습니다|여쭙겠습니다|여쭤보겠습니다|여쭈어\s*보겠습니다|"
                       r"답변(?:을)?\s*기다리겠습니다|\d+\s*초\s*드리겠습니다)[,.!?。\s]*$")
_PROGRESS_BODY = re.compile(r"(?<![가-힣])(?:" + _PROGRESS_ACT + r"(?:를|을)?\s*(?:계속|진행|시작|더|좀)?\s*"
                           r"(?:하|드리|이어\s*가)(?:도록\s*하)?(?:겠습니다|겠어요)|"
                           r"묻겠습니다|여쭙겠습니다|여쭤보겠습니다|여쭈어\s*보겠습니다)(?=$|[,.!?。\s])")
_NUMBERED_PROGRESS = re.compile(r"^(?:(?:예|네|그러면|그럼|이제)\s*[,.!?。]?\s*){0,2}"
                                r"(?:(?:첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열|열한|열두)\s*번째|"
                                r"[1-9]\d?\s*번째|제\s*[1-9]\d?\s*(?:번째)?)\s*"
                                r"(?:질문|질의)입니다(?=$|[\s,.!?。])[,.!?。]*\s*")
_NUMBERED_COUNTER = re.compile(r"^위원님(?:께)?\s*(?:(?:역으로|반대로)|"
                               r"(?:(?:지금|방금|앞서)\s*)?(?:말씀|질문|지적|요청)[^.?!。]{0,100}(?:뜻|의미))")
_NUMBERED_INSTITUTION = re.compile(r"^(?:(?:예|네|우선|먼저)\s*){0,2}(?:저희|우리)\s*"
                                   r"(?:기관|공사|회사|원|청|본부)(?:이|가|는|의|에서|은)")
_PROGRESS_TO_MEMBER = re.compile(r"(?<![가-힣])위원님(?:께)?\s+"
                                r"(?:(?:역으로|반대로|저도|제가|한\s*가지|먼저|좀|다시)\s*){0,4}$")
_OFFICE = re.compile(r"(?:저희|우리)(?:의)?\s*(?:의원실|지역구)|(?:저는|제가)\s*(?:국회의원|의원|위원장|위원)"
                     r"(?:으로|에)?\s*(?:취임|선출|당선)")
_PAST_CAREER = re.compile(r"(?:제가|저는)[^.?!。]{0,45}(?:의원|위원장|국회의원|장관\s*시절|과거|예전에)")
_COUNTER = re.compile(r"^(?:(?:예|네|아니|우선|동감|그래서|저도)\s*){0,2}위원님\s*"
                      r"(?:(?:지금|방금|앞서)\s*)?(?:말씀|질문|지적|요청|의견|께서)")
_RESPONSE = re.compile(r"(?:방금|지금|앞서)\s*위원님(?:께서|께서의|의)?\s*(?:지적|질문|말씀)|"
                       r"위원님\s*(?:의견|말씀)에\s*(?:전적으로\s*)?(?:공감|동감)")
_COMPLETE_AGREEMENT = re.compile(r"^(?:(?:예|네|충분히|전적으로)\s*){0,3}위원님\s*말씀이\s*"
                                 r"맞는\s*말씀(?:이십니다|입니다)[,.!?。]*\s*$")
_ACK = re.compile(r"(?<![가-힣])(?:예|네|그렇습니다|맞습니다|알겠습니다|알고\s*있습니다)(?=$|[\s.!?。])")
_SELF_DIALOGUE = re.compile(r"(?:자문자답|스스로\s*(?:묻|질문|답)|제가\s*묻고|예를\s*들|가정해\s*보|대본|인용문)")
_CLOSED_SCRIPT_REPORT = re.compile(
    r"^(?:에(?:는)?|의|은|을|를)?\s*(?:“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*')"
    r"\s*(?:이?라|다)고\s*(?:적혀|쓰여|나와|기록되어|작성되어)\s*있습니다(?=$|[\s,.!?。])"
)
_INTERRUPT = re.compile(r"(?:됐습니다\s+잠깐만요|잠깐만요\s+우리\s*" + _TITLE + r")")
_SLIDE = re.compile(r"다음\s*(?:슬라이드|자료|화면)\s*(?:보시지요|보시죠|보십시오|보여\s*주세요)")
_ACTION = re.compile(r"(?:수사\s*의뢰|명단(?:을)?\s*공개|회수\s*노력|상습[^.?!。]{0,20}지정|면직|징계|"
                     r"과태료[^.?!。]{0,30}부과|제재[^.?!。]{0,25}강화|"
                     r"진상[^.?!。]{0,20}조사|서면(?:으로)?\s*보고|관계\s*부처[^.?!。]{0,20}협의|"
                     r"시스템[^.?!。]{0,25}(?:강구|개선|구축))")
_ONGOING = re.compile(r"(?:하고\s*있|하고\s*있다는|했|하였|시켰|이루어|하겠습니다|하도록\s*하겠습니다|"
                      r"보고\s*드리(?:는|겠)|조사해서|협의해\s*나가겠습니다)")
_FIRST_PERSON = re.compile(r"^(?:(?:예|네|우선|먼저|현재|지금|그\s*부분은|그래서)\s*){0,3}"
                           r"(?:저희(?:가|는|도|들이)?|우리\s*(?:기관|공사|회사))(?=\s|[은는이가])")
_APPOINTED = re.compile(r"(?:제가|저는)\s*[^.?!。]{0,30}(?:취임|부임)(?:을)?\s*(?:해서|하여|하고|한\s*뒤|한\s*후|한\s*당시|이후)")
_CONSTRAINT = re.compile(r"^(?:(?:예|네|그게|그것은|그\s*부분은|우선|먼저)\s*){1,3}[^.?!。]{0,100}"
                         r"(?:고민|검토|협의)[^.?!。]{0,100}(?:애로|어려움|어려워|곤란|승소)[^.?!。]{0,70}")
_IMPLEMENTATION = re.compile(r"^(?:우선|먼저|현재|지금)\s+[^.?!。]{0,150}"
                             r"(?:지정|공개|수사\s*의뢰|회수|압류|부과|제재|검토)[^.?!。]{0,150}")
_JOB_INTRO = re.compile(r"^(?:(?:예|네)\s*)?(?:위원님\s*|저는\s*|제가\s*)"
                       r"(?:[가-힣]{1,16}\s*){0,2}(?:본부장|실장|국장|처장|원장|청장|사장)입니다")
_PERMISSION = re.compile(r"^(?:위원님\s*)?(?:(?:좀|잠깐|잠시)\s*)?(?:답변|말씀)(?:을)?\s*"
                         r"드려도\s*(?:되겠습니까|될까요|되겠어요)[?!。\s]*(?:위원님)?[?!。\s]*$")
_WORK_PROGRESS = re.compile(r"저희(?:가|는|들이)\s*(?:개선을?\s*(?:지속적으로\s*)?하고\s*있|"
                            r"(?:지속적으로\s*)개선을?\s*하고\s*있)")
_FORMAL_REPORT = re.compile(r"저희(?:가|는|들이)\s*[^.?!。]{0,60}진상[^.?!。]{0,20}조사[^.?!。]{0,100}"
                            r"서면(?:으로)?\s*보고(?:를)?\s*(?:드리|올리)|"
                            r"(?:저희가|제가)\s*[^.?!。]{0,70}위원님께서\s*제기[^.?!。]{0,200}"
                            r"(?:조사|탐문)[^.?!。]{0,180}말씀\s*올립니다")
_STAFF = re.compile(r"(?:저희|우리)\s*직원(?:들)?(?:은|는|이|가|도|의|을|과|에게)?")
_REVIEWEE = re.compile(r"(?:저희(?:가|는|도|들이)?|우리(?:가|는)?|제가|저는)\s*피감기관(?:의)?\s*입장")
_IDENTITY_PAST = re.compile(r"(?:제가|저는)[^.?!。]{0,45}(?:의원|위원장|국회의원|장관|원장|사장)"
                            r"[^.?!。]{0,15}(?:시절|(?:이었|였)을\s*때|재임[^.?!。]{0,5}(?:당시|때))")
_HYPOTHETICAL = re.compile(r"(?:만약|가령|가정(?:해|하면|한다면)|예를\s*들)")
_REPLY_BRIDGE = re.compile(r"^[^.?!。]{1,36}(?:해서|하여|통해|통하여)\s+"
                           r"저희(?:가|는|들이)(?=$|\s)")
_WEAK_RESPONSE = re.compile(r"(?<![가-힣])[가-힣]{1,20}(?:았|었|였|혔)죠[,.!?。\s]*"
                            r"(?P<ack>예|네)(?=$|[\s,.!?。])")
_WEAK_CONFIRMATION = re.compile(r"(?<![가-힣])[가-힣]{1,20}(?:지요|죠)[,.!?。\s]+"
                                r"(?P<ack>맞습니다)(?=$|[\s,.!?。])")
_WEAK_NONRESPONSE = re.compile(r"(?:번째|번|명|개|가지|차례|사람|글자|문자|권|줄|쪽|건|곳|군데|등급|말|의견|생각|표현|문구|단어)"
                               r"(?=$|[\s은는이가을를])")
_WEAK_RETURN = re.compile(r"(?:다음(?:으로|은|에)?|그다음(?:으로|은)?|이어서|계속(?:해서)?|"
                          r"그러면|그럼|그래서|그런데|그리고)(?=$|[\s,.!?。])")
_WEAK_OWN_PROGRESS = re.compile(r"^(?:(?:지금|이제|우선|먼저)\s*){0,2}"
                                r"(?:제가|저는|저희(?:가|는)|우리(?:가|는)|본\s*위원(?:은|이)?)\s*[^.?!。]{0,55}"
                                r"(?:설명|말씀|질문|질의|정리|답변)[^.?!。]{0,20}(?:겠습니다|겠어요)")
_WEAK_FINITE = re.compile(r"(?:습니다|입니다|었어요|았어요|이에요|예요)(?=$|[\s,.!?。])")
_ANSWER_NEW_QUESTION_RETURN = re.compile(r"(?<![가-힣])저희(?:가|는|들이)\s+[^.?!。]{0,100}?"
                                        r"(?:했습니다|있습니다)[,.!?。]*\s+"
                                        r"(?P<return>그럼|그러면|그런데)(?=$|[\s,.!?。])")


def _direct(pattern: re.Pattern, text: str):
    """인용·가정·회고의 문구를 현재 역할 근거에서 제외한다."""
    return next((match for match in pattern.finditer(text)
                 if not is_reported_anchor(text, match.start(), match.end())), None)


def _self_dialogue_before(text: str, offset: int) -> bool:
    """닫힌 자료 인용을 보고한 뒤의 실제 질문까지 대본 속 말로 묶지 않는다."""
    for match in _SELF_DIALOGUE.finditer(text, 0, offset):
        if is_reported_anchor(text, match.start(), match.end()):
            continue
        if match.group() in {"대본", "인용문"} and _CLOSED_SCRIPT_REPORT.match(text[match.end():offset]):
            continue
        return True
    return False


def _current_speech(pattern: re.Pattern, text: str):
    """본문 중간의 자기 역할도 가정·과거 직책의 재연과 구별한다."""
    return next((match for match in pattern.finditer(text)
                 if not is_reported_anchor(text, match.start(), match.end())
                 and not _HYPOTHETICAL.search(text[max(0, match.start() - 100):match.start()])
                 and not _IDENTITY_PAST.search(text[:match.end()])), None)


def _request_clauses(text: str, *, boundaries: bool = False) -> list[re.Match]:
    """요청 절 뒤 설명이 이어져도 근거를 보존하며 인용·가정은 제외한다."""
    found = []
    patterns = (_REQUEST_CLAUSE, _ADDRESSED_APPEARANCE) + ((_REQUEST_BOUNDARY,) if boundaries else ())
    for pattern in patterns:
        for match in pattern.finditer(text):
            if (is_reported_anchor(text, match.start(), match.end())
                    or _HYPOTHETICAL.search(text[max(0, match.start() - 100):match.start()])
                    or _IDENTITY_PAST.search(text[:match.end()])
                    or _SELF_DIALOGUE.search(text[:match.end()])):
                continue
            found.append(match)
    return sorted({match.end(): match for match in found}.values(), key=lambda match: (match.start(), match.end()))


def _progress_speech(pattern: re.Pattern, text: str, *, body: bool = False):
    """신문은 기사·제삼자의 예정이 아니라 현재 본인의 절차 시작일 때만 쓴다."""
    for match in pattern.finditer(text):
        examination = bool(re.match(r"(?:증인\s*)?신문", match.group()))
        if is_reported_anchor(text, match.start(), match.end()):
            continue
        if (body or examination) and (_HYPOTHETICAL.search(text[max(0, match.start() - 100):match.start()])
                                      or _IDENTITY_PAST.search(text[:match.end()])):
            continue
        if examination:
            before = text[:match.start()].rstrip()
            own = (not before or re.search(r"(?:제가|저는|저도|본\s*위원(?:은|이)?)\s*$", before)
                   or re.search(r"(?:^|[.!?。])(?:(?:예|네|그럼|그러면|지금부터|이어서|이제|계속해서|다음으로|다음은)\s*){1,3}$", before))
            if not own:
                continue
        return match
    return None


def _numbered_progress(text: str):
    """행 첫머리 순번 선언만 현재 자기 진행으로 읽고 인용·예고는 제외한다."""
    match = _NUMBERED_PROGRESS.match(text)
    if not match or is_reported_anchor(text, match.start(), match.end()):
        return None
    tail = text[match.end():]
    if (re.match(r"(?:하고|라고|이라는|라는|라며)\s*(?:가정|말|읽|적|예고)", tail)
            or re.search(r"자문자답|스스로\s*(?:묻|질문|답)|제가\s*묻고|제\s*질문에\s*제가", tail[:100])):
        return None
    if re.match(r"(?:어제|과거|당시|내일|다음\s*회의|다음에|나중에|추후)", tail) and re.search(
            r"(?:(?:제가|저는|이렇게)[^.?!。]{0,35}(?:말했|말할|발언|예고)|"
            r"사용할\s*(?:문구|표현)|(?:문구|문장|표현)입니다)", tail[:110]):
        return None
    return match


def answer_then_new_question_boundaries(text: str) -> list[int]:
    """직접 일인칭 설명 뒤 새 두 종결의 질문 복귀 후보를 원문 위치로 반환한다.

    반환값은 그럼·그러면·그런데의 시작이다. 실제 전환 시각이나 화자를 확정하지
    않으며, 인용·자기 진행·기관 반문을 같은 기준으로 제외하는 검토 단서다.
    """
    if not isinstance(text, str) or not text:
        return []
    question = _direct(_QUESTION_END, text) or _current_speech(_QUESTION_FRAGMENT_END, text)
    if not question or not re.match(r"(?:합니까|됩니까)", question.group()):
        return []
    if (_SELF_DIALOGUE.search(text[:question.start()]) or _direct(_OFFICE, text)
            or _direct(_COUNTER, text) or _direct(_JOB_INTRO, text) or _QUESTION_SELF.match(text)
            or _numbered_progress(text) or _progress_speech(_PROGRESS, text)
            or _progress_speech(_PROGRESS_BODY, text, body=True)
            or re.match(r"^(?:본\s*위원|저는\s*(?:위원|의원)|(?:저는\s*)?[가-힣]{2,5}\s*(?:위원|의원)입니다)", text)
            or (_current_speech(_STAFF, text) and _current_speech(_REVIEWEE, text))):
        return []
    found = []
    for match in _ANSWER_NEW_QUESTION_RETURN.finditer(text[:question.start()]):
        offset = match.start("return")
        following = text[match.end():].lstrip()
        if (not 0 < question.start() - offset <= 300
                or is_reported_anchor(text, match.start(), match.end())
                or _direct(_COUNTER, following) or _NUMBERED_INSTITUTION.match(following)
                or re.match(r"위원님(?:께)?\s*(?:역으로|반대로)", following)
                or re.search(r"(?:라고|다고|라는)\s*(?:답변|말씀|말|발언|했)", match.group())):
            continue
        found.append(offset)
    return sorted(set(found))


def weak_response_spans(text: str) -> list[tuple[int, int]]:
    """확인형 뒤 짧은 응답의 약한 검토 선택지만 원문 문자 범위로 반환한다.

    시작은 예·네·맞습니다이며 끝은 뒤 공백까지 포함한 응답 끝이다.
    예·네 뒤 본문이 있으면 첫 완결문 끝도 선택지로 반환한다.
    지요·죠 뒤 맞습니다는 완결 응답 자체만 반환하며 원문 끝도 허용한다.
    같은 시작의 두 범위는 선택지다. 실제 화자 전환·역할·질문 근거가 아니며
    한 사람이 되묻고 이어 설명한 경우도 원음 없이 확정하지 않는다.
    """
    if not isinstance(text, str) or not text:
        return []

    def after_space(offset: int) -> int:
        while offset < len(text) and (text[offset].isspace() or text[offset] in ",.!?。"):
            offset += 1
        return offset

    spans = set()
    matches = [(match, False) for match in _WEAK_RESPONSE.finditer(text)]
    matches.extend((match, True) for match in _WEAK_CONFIRMATION.finditer(text))
    for match, complete in matches:
        start = match.start("ack")
        before = text[:match.start()]
        if (is_reported_anchor(text, match.start(), match.end()) or _SELF_DIALOGUE.search(before)
                or _HYPOTHETICAL.search(before[-100:]) or _IDENTITY_PAST.search(text[:match.end()])
                or _REPORTED_INFORMATION.search(before) or _QUESTION_SELF.match(text)
                or _direct(_COUNTER, text)):
            continue
        end = after_space(match.end("ack"))
        ack_end = end
        if _WEAK_NONRESPONSE.match(text, end):
            continue
        # 짧은 수락 뒤 바로 질문자가 진행을 잇는 경우는 새 약한 후보를 늘리지 않는다.
        for _ in range(3):
            extra = _ACK.match(text, end)
            if extra is None:
                break
            end = after_space(extra.end())
        body = text[end:]
        if (_WEAK_NONRESPONSE.match(body) or _WEAK_RETURN.match(body) or _WEAK_OWN_PROGRESS.match(body)
                or _SELF_DIALOGUE.search(body[:100])):
            continue
        if complete:
            spans.add((start, ack_end))
            continue
        if len(body.strip()) < 4:
            continue
        spans.add((start, end))
        finite = _WEAK_FINITE.search(body[:200])
        if finite:
            spans.add((start, after_space(end + finite.end())))
    return sorted(spans)


def _information_questions(text: str) -> list[re.Match]:
    """의문사와 죠·지요 종결을 함께 쓰는 현재 질문만 추가한다."""
    # 자기 설명의 반문은 새 문법만으로 위원 역할을 만들지 않는다.
    if not _direct(_OFFICE, text) and (_QUESTION_SELF.match(text) or _direct(_COUNTER, text)
            or _direct(_RESPONSE, text) or re.match(r"^위원님(?:께)?\s+", text)):
        return []
    matches = []
    for match in _INFORMATION_QUESTION.finditer(text):
        before, after = text[:match.start()], text[match.end():]
        if (is_reported_anchor(text, match.start(), match.end()) or _SELF_DIALOGUE.search(before)
                or _HYPOTHETICAL.search(before[-100:]) or _IDENTITY_PAST.search(text[:match.end()])
                or _REPORTED_INFORMATION.search(before) or _INDIRECT_INFORMATION.search(match.group())
                or _INFORMATION_CLAUSE_END.search(match.group())
                or re.match(r"\s*(?:답은|정답은|제가\s*답(?:을|하))", after)):
            continue
        matches.append(match)
    return matches


def _question_matches(text: str) -> list[re.Match]:
    """질문 존재·행 끝 역할·후속 응답 경계가 같은 질문 근거를 사용한다."""
    existing = list(_QUESTION.finditer(text))
    ends = {match.end() for match in existing}
    return sorted(existing + [match for match in _information_questions(text) if match.end() not in ends],
                  key=lambda match: (match.start(), match.end()))


def has_direct_question(text: str) -> bool:
    """행 전체 역할을 확정하지 않고 실제 질문 구절의 존재만 확인한다."""
    text = norm(text)
    return any(not is_reported_anchor(text, match.start(), match.end())
               and not _self_dialogue_before(text, match.start()) for match in _question_matches(text))


def enclosed_response_spans(text: str) -> list[tuple[int, int]]:
    """직접 질문·확인과 재질문 사이 인지·판단 응답의 원문 범위를 반환한다.

    한 색으로 전송돼도 이 범위를 단일 화자로 확정하지 않는다. 새 역할이나
    이름은 정하지 않으며 인용·가정·자문자답·명시적 자기 설명은 제외한다.
    질문 뒤 180자, 응답 뒤 120자와 최대 네 응답으로 탐색을 제한한다.
    """
    if not isinstance(text, str) or not text or _SELF_DIALOGUE.search(text) or _COUNTER.match(text):
        return []
    questions = _question_matches(text)
    anchors = sorted(questions + list(_CONFIRMING_STATEMENT.finditer(text)), key=lambda match: match.end())
    found = []
    for asked in anchors:
        if (is_reported_anchor(text, asked.start(), asked.end())
                or _HYPOTHETICAL.search(text[max(0, asked.start() - 120):asked.start()])
                or re.search(r"위원님\s*(?:말씀|질문|요청)", text[max(0, asked.start() - 100):asked.end()])):
            continue
        start = asked.end()
        while start < len(text) and text[start] in " ,.!?。\t\r\n":
            start += 1
        ending = _KNOWLEDGE_RESPONSE_END.search(text, start, min(len(text), start + 180))
        if not ending:
            continue
        response = text[start:ending.end()]
        if (any(start <= other.start() < ending.start() for other in questions)
                or _WEAK_FINITE.search(text[start:ending.start()])
                or _RESPONSE_OWN_EXPLANATION.search(response)
                or is_reported_anchor(text, start, ending.end())):
            continue
        acknowledgement = response.strip(" ,.!?。")
        while (ack := _ACK.match(acknowledgement)) is not None:
            acknowledgement = acknowledgement[ack.end():].strip(" ,.!?。")
        if not acknowledgement and asked.re is not _CONFIRMING_STATEMENT:
            # 질문 뒤 짧은 맞장구는 기존 경계·주된 역할 정책에 맡긴다.
            # 새 강한 근거는 내용 있는 응답 또는 평서 확인 뒤의 명시 동의다.
            continue
        end = ending.end()
        while end < len(text) and text[end] in " ,.!?。\t\r\n":
            end += 1
        returning = next((other for other in questions if end <= other.start() < end + 120), None)
        if (not returning or _WEAK_FINITE.search(text[end:returning.start()])
                or _RESPONSE_OWN_EXPLANATION.search(text[end:returning.end()])
                or _CLARIFYING_REPLY.search(text[end:returning.end()])
                or is_reported_anchor(text, returning.start(), returning.end())):
            continue
        pair = (start, end)
        if 0 < start < end < len(text) and pair not in found:
            found.append(pair)
            if len(found) == 4:
                break
    return sorted(found)


def role_evidence(text: str, *, after_question: bool = False) -> dict:
    """원문에서 확실한 역할과 한 행의 혼합 후보만 판단하며 이름은 만들지 않는다."""
    text = norm(text)
    result = {"role": None, "mixed": False, "question": False, "reason": "직접 역할 근거 없음"}
    if not text:
        return result
    # 실제 기관 반문에서 인용한 요구와 의원의 대본 속 문답은 화자 전환이 아니다.
    counter = bool(_direct(_COUNTER, text))
    office = bool(_direct(_OFFICE, text))
    numbered_progress = _numbered_progress(text)
    if numbered_progress:
        tail = text[numbered_progress.end():]
        if _NUMBERED_COUNTER.match(tail) or _NUMBERED_INSTITUTION.match(tail) or _direct(_JOB_INTRO, tail):
            # 순번 선언을 붙인 기관의 반문·자기소개를 위원 역할로 강제하지 않는다.
            tail_evidence = role_evidence(tail, after_question=after_question)
            return tail_evidence if tail_evidence["role"] == "answer" or tail_evidence["mixed"] else result
    requests = _request_clauses(text)
    question, request, progress = (_direct(_QUESTION_END, text),
        _direct(_REQUEST, text) or next(iter(requests), None), _progress_speech(_PROGRESS, text))
    if question and _self_dialogue_before(text, question.start()):
        question = None  # 실제 질문 존재 검사와 같이 대본·자문자답의 질문을 역할 근거에서 뺀다.
    if not question:
        question = next((match for match in _information_questions(text)
                          if not _self_dialogue_before(text, match.start())
                          and re.fullmatch(r"\s*(?:" + _TITLE + r")?[,.!?。]*\s*", text[match.end():])), None)
    body_progress = _progress_speech(_PROGRESS_BODY, text, body=True)
    if body_progress:
        recipient = _PROGRESS_TO_MEMBER.search(text[:body_progress.start()])
        if recipient and not is_reported_anchor(text, recipient.start(), recipient.end()):
            body_progress = None  # 위원에게 되묻는 표현만으로 발언자를 위원으로 확정하지 않는다.
    addressed_request = _current_speech(_ADDRESSED_REQUEST, text)
    question_fragment = _current_speech(_QUESTION_FRAGMENT_END, text)
    if question_fragment and _self_dialogue_before(text, question_fragment.start()):
        question_fragment = None
    staff, reviewee = _current_speech(_STAFF, text), _current_speech(_REVIEWEE, text)
    agency_self = bool(staff and reviewee and abs(staff.start() - reviewee.start()) <= 220 and not office)
    interior_question = next((match for match in _INTERIOR_CONFIRM.finditer(text)
                              if not is_reported_anchor(text, match.start(), match.end())
                              and not _self_dialogue_before(text, match.start())), None)
    address = re.search(_TITLE + r"[,.!?。]*\s*$", text)
    tail_address = bool(address and not is_reported_anchor(text, address.start(), address.end())
                        and re.search(r"(?:해야|찾아야|개선|보완|대책|생각이\s*드는데요)", text[-180:]))
    independent_member = bool(office or request or progress or numbered_progress or body_progress
        or addressed_request or tail_address
        or address and not is_reported_anchor(text, address.start(), address.end()))
    if _QUESTION_SELF.match(text) and not independent_member:
        # 자기 설명의 반문은 새 두 종결만으로 위원 역할을 확정하지 않는다.
        if question and re.match(r"(?:합니까|됩니까)", question.group()):
            question = None
        if question_fragment and re.match(r"(?:합니까|됩니까)", question_fragment.group()):
            question_fragment = None
    mixed, backchannel_only = False, True
    binding_conflict = False
    if agency_self:
        cutoff = re.search(r"(?:상황에서|입장에서|있는데|했는데|때문에)\s+(?:예|네)\s*알겠습니다\s+"
                           r"(?:저희|우리|제가|저는)\s*$", text)
        if cutoff and not is_reported_anchor(text, cutoff.start(), cutoff.end()):
            mixed, backchannel_only = True, False
    if not counter:
        binding_conflict = bool(enclosed_response_spans(text))
        if answer_then_new_question_boundaries(text) or binding_conflict:
            mixed, backchannel_only = True, False
        # 일반 요청 종결은 혼합 경계 확인에만 쓰고, 이것만으로 위원 역할을 주지 않는다.
        request_boundaries = _request_clauses(text, boundaries=True)
        for requested in request_boundaries:
            raw_following = text[requested.end():]
            following = raw_following.lstrip(" ,.!?。")
            offset = requested.end() + len(raw_following) - len(following)
            # '제가 작년 국감 때…' 같은 자기 설명은 응답 전환이 아니다.
            # 명시적인 수락이나 기관 역할이 있는 후속 절만 혼합 후보로 남긴다.
            response = bool(_direct(_COUNTER, following) or _direct(_JOB_INTRO, following)
                or _direct(_FORMAL_REPORT, following) or _direct(_WORK_PROGRESS, following)
                or _direct(_FIRST_PERSON, following) and _direct(_ACTION, following) and _direct(_ONGOING, following))
            acceptance = _ACK.match(following) or _REQUEST_ACCEPTANCE.match(following) or _ADDRESSED_RESPONSE.match(following)
            accepted = bool(acceptance and not is_reported_anchor(text, offset, offset + acceptance.end()))
            if following and (accepted or response):
                mixed, backchannel_only = True, False
        if after_question and requests:
            head = _ACK.match(text)
            offset = head.end() if head else 0
            leading = text[offset:].lstrip(" ,.!?。")
            offset = len(text) - len(leading)
            acceptance = _REQUEST_ACCEPTANCE.match(leading)
            if (acceptance and not is_reported_anchor(text, offset, offset + acceptance.end())
                    and any(match.start() >= offset + acceptance.end() for match in requests)):
                mixed, backchannel_only = True, False
        bare_tail = _BARE_COMMITMENT_TAIL.match(text)
        if (bare_tail and not is_reported_anchor(text, bare_tail.start(), bare_tail.end())
                and any(match.start() >= bare_tail.end() for match in requests)):
            # 주어 없는 약속 꼬리는 앞 원문의 기관 응답일 수 있다. 뒤 요구만으로
            # 한 의원의 발언·라벨 학습 근거로 삼지 않되 이전 원문은 되돌리지 않는다.
            mixed, backchannel_only = True, False
        for asked in _question_matches(text):
            if is_reported_anchor(text, asked.start(), asked.end()) or _self_dialogue_before(text, asked.start()):
                continue
            following = re.match(r"[,.!?。\s]*(?:" + _TITLE + r"[,.!?。\s]*)?", text[asked.end():])
            offset = asked.end() + following.end()
            acknowledged = _ACK.match(text, offset)
            if acknowledged and not is_reported_anchor(text, acknowledged.start(), acknowledged.end()):
                mixed = True
                rest = text[acknowledged.end():].lstrip(" ,.!?。")
                while (extra := _ACK.match(rest)) is not None:
                    rest = rest[extra.end():].lstrip(" ,.!?。")
                if (rest and not re.match(r"(?:그래서|그러면서|그런데|그러면|그리고|이어서|다음|중간에)(?:\s|$)", rest)) \
                        or re.match(r"(?:(?:그래서|그러면서|그런데)\s*)?(?:저희(?:가|는|들이)?|제가)(?=\s|$)", rest):
                    backchannel_only = False
            response_start = re.match(r"(?:저희(?:가|는|들이)?|제가)(?=\s|$)", text[offset:])
            if response_start and not re.match(r"(?:제가|저희가)\s*(?:말씀드리|묻고|질문)", text[offset:]):
                mixed, backchannel_only = True, False
            bridge = _REPLY_BRIDGE.match(text[offset:])
            if bridge and not re.match(r"\s*(?:말씀드리|제안|질문|생각)", text[offset + bridge.end():]) \
                    and not is_reported_anchor(text, offset, offset + bridge.end()):
                mixed, backchannel_only = True, False
        interrupt, slide = _direct(_INTERRUPT, text), _direct(_SLIDE, text)
        if interrupt and interrupt.start() > 0 and re.search(r"(?:겠습니다|있습니다|했습니다)[,.!?。]*\s*$", text[:interrupt.start()]):
            mixed, backchannel_only = True, False
        if slide and slide.start() > 0 and not _SELF_DIALOGUE.search(text[:slide.start()]):
            leading = text[:slide.start()].strip()
            if re.search(r"(?:드리겠습니다|알고\s*있습니다|그렇습니다)[,.!?。]*$", leading) \
                    and (leading.startswith("드리겠습니다") or _ACTION.search(leading) or _ACK.search(leading)):
                mixed, backchannel_only = True, False
        introduction = _direct(_JOB_INTRO, text)
        if introduction:
            tail = text[introduction.end():]
            if (re.search(r"(?:어떤|무슨|왜|어떻게)[^.?!。]{0,60}(?:지요|습니까|나요)[?!。]*\s*$", tail)
                    or _information_questions(tail)) \
                    and not re.search(r"말씀|의미|뜻|요청", tail):
                mixed, backchannel_only = True, False
        return_fragment = re.search(r"위원님\s*(?:말씀|질문|지적|의견)\s*$", text)
        if return_fragment and return_fragment.start() > 0 \
                and not is_reported_anchor(text, return_fragment.start(), return_fragment.end()) \
                and has_direct_question(text[:return_fragment.start()]):
            mixed, backchannel_only = True, False
    if mixed:
        return {"role": None, "mixed": True, "question": False, "backchannel_only": backchannel_only,
                "binding_conflict": binding_conflict,
                "reason": "한 원문 행의 질문·기관 응답 또는 답변 뒤 질의 복귀 경계 확인"}
    if _direct(_PERMISSION, text):
        return {"role": "answer", "mixed": False, "question": False, "reason": "위원에게 답변 발언 허가를 구하는 직접 요청"}
    if _direct(_COMPLETE_AGREEMENT, text):
        return {"role": "answer", "mixed": False, "question": False, "reason": "위원의 말씀에 대한 완결된 직접 동의 응답"}
    if counter and not (office and (question or request)):
        return {"role": "answer", "mixed": False, "question": False, "reason": "기관 측이 위원 질문을 되묻는 직접 응답"}
    if agency_self:
        return {"role": "answer", "mixed": False, "question": False, "reason": "현재 자기 직원과 피감기관 입장을 함께 밝힌 기관 응답"}
    institution = re.match(r"^(?:(?:예|네|우선|먼저)\s*){0,2}(?:저희|우리)\s*(?:기관|공사|회사|원|청|본부)(?:이|가|는|의|에서|은)", text)
    if (_direct(_JOB_INTRO, text) or institution and not office
            and not is_reported_anchor(text, institution.start(), institution.end())):
        return {"role": "answer", "mixed": False, "question": False, "reason": "기관 자신을 주어로 설명하는 응답·반문"}
    agency_action = bool(_direct(_ACTION, text) and _direct(_ONGOING, text)
                         and (_direct(_FIRST_PERSON, text) or _direct(_APPOINTED, text)))
    interior_member = bool(interior_question and not agency_action and not _direct(_RESPONSE, text))
    fragment_member = bool(question_fragment and not agency_action and not _direct(_RESPONSE, text))
    if office or question or request or progress or numbered_progress or tail_address or interior_member or body_progress or addressed_request or fragment_member:
        return {"role": "member", "mixed": False,
                "question": bool(question or request or tail_address or interior_member or addressed_request or fragment_member),
                "reason": "직접 질문·기관 호칭·자료 요구 또는 위원 자신의 질의 진행"}
    action, ongoing = _direct(_ACTION, text), _direct(_ONGOING, text)
    first_person, appointed = _direct(_FIRST_PERSON, text), _direct(_APPOINTED, text)
    # 질문 직후의 명시적 수락 뒤에 같은 기관의 설명이 이어지는 경우만 허용한다.
    reply_then_self = bool(after_question and re.match(
        r"^(?:말씀하시면\s*)?(?:예\s*|네\s*|그렇게\s*하겠습니다[,.!?。]*\s*)", text)
        and re.search(r"(?:그래서\s*)?저희\s*(?:생각에는|가|는)", text[:110]))
    past_career = bool(_PAST_CAREER.search(text))
    operational = bool(action and ongoing and not past_career and (first_person or appointed or reply_then_self
                       or (after_question and _direct(_IMPLEMENTATION, text))))
    response = bool(_direct(_RESPONSE, text) and (action or re.search(r"제도적?\s*(?:대책|개선)", text)))
    work_progress = bool(_direct(_WORK_PROGRESS, text) and not office and not past_career)
    formal_report = bool(_direct(_FORMAL_REPORT, text) and not office and not past_career)
    constraint = bool(after_question and _direct(_CONSTRAINT, text) and re.search(r"있습니다[,.!?。]*$", text))
    if operational or constraint or response or work_progress or formal_report:
        return {"role": "answer", "mixed": False, "question": False, "reason": "기관의 직접 업무 조치 또는 직전 질의에 대한 이행 제약 설명"}
    return result
