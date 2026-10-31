"""규칙 기반 즉시 추출: 요구자료·약속사항·지적사항·증인요구·서면질의, 감시 키워드, 담당부서, 키워드.

LLM 이 없거나 느려도 실시간으로 동작해야 하므로 정규식으로 1차 추출하고, LLM 요약이 끝나면
블록 단위로 보강한다(origin='llm'). 문장부호 없는 STT 원문을 전제로 한다.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..models import ExtractedItem, Mention
from .text import compact, flexible_pattern, norm, sentence_at, sentence_head, split_sentences


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    roles: frozenset[str]          # 이 규칙을 적용할 발언 역할
    confidence: float
    label: str
    strength: str = ""             # 약속: 확약|검토|의례, 요구자료: 자료제출|서면보고|조사|사실확인


def _r(p: str) -> re.Pattern[str]:
    return re.compile(p)


_ANY_SPEAKER = frozenset({"member", "answer", "other", "unknown"})
_MEMBERISH = frozenset({"member", "other", "unknown"})
_ANSWERISH = frozenset({"answer", "member", "other", "unknown"})  # STT 가 질의·답변을 한 발언으로 합치는 일이 잦다

RULES: list[Rule] = [
    # ── 요구자료 ──────────────────────────────────────────────
    Rule("data_request", _r(r"자료(?:를|도|는)?\s*(?:좀\s*)?(?:제출|제공|보내|요구|요청|전달)"), _MEMBERISH, 0.8, "자료 제출 요구", "자료제출"),
    Rule("data_request", _r(r"(?:서면|문서|자료)으?로\s*(?:좀\s*)?(?:제출|보고|답변|정리)(?:해|하여)?\s*주"), _MEMBERISH, 0.8, "서면 제출 요구", "서면보고"),
    Rule("data_request", _r(r"보고(?:를|도)?\s*(?:좀\s*)?(?:해\s*)?주(?:시고|십시오|세요|시기\s*바랍니다|셨으면)"), _MEMBERISH, 0.75, "보고 요구", "서면보고"),
    Rule("data_request", _r(r"(?:제출|전달|공유)(?:해|하여)\s*주(?:시고|십시오|세요|시기\s*바랍니다|셨으면)"), _MEMBERISH, 0.75, "제출 요구", "자료제출"),
    Rule("data_request", _r(r"(?:확인|파악)(?:하고|해서|하셔서)\s*(?:\S+\s+){0,6}?(?:얘기|말씀|알려|보고)(?:해)?\s*주(?:십시오|세요|시기)"), _MEMBERISH, 0.6, "사실확인 요구", "사실확인"),
    Rule("data_request", _r(r"(?:조사|파악|점검|분석)(?:를|을)?\s*(?:좀\s*)?(?:한번\s*)?(?:해\s*)?(?:보셨으면|해\s*주십시오|해\s*주시기)"), _MEMBERISH, 0.6, "조사 요구", "조사"),
    # '자료 좀 주세요', '명단 주십시오', '사본 보내 주십시오', '자료를 주시기 바랍니다'
    Rule("data_request", _r(r"(?:자료|명단|사본|현황|내역|목록|계획서?|보고서|원문|근거)(?:를|을|도|는|로|으로)?\s*(?:좀\s*)?(?:한번\s*)?"
                            r"(?:(?:보내|내)\s*)?주(?:십시오|세요|시기\s*바랍니다|시고|셨으면)"), _MEMBERISH, 0.75, "자료 제출 요구", "자료제출"),
    # '그 명단을 제출하시기 바랍니다', '보고하십시오'
    Rule("data_request", _r(r"(?:제출|보고|송부)(?:를|을)?\s*하(?:시기\s*바랍니다|십시오|세요|시고)"), _MEMBERISH, 0.75, "제출 요구", "자료제출"),
    # '정리해서 주세요', '취합해서 보내 주십시오'
    Rule("data_request", _r(r"(?:정리|취합|작성)(?:를|을)?\s*(?:해서|하셔서|하여)\s*(?:좀\s*)?(?:보내\s*)?주(?:십시오|세요|시기|시고)"), _MEMBERISH, 0.65, "자료 정리 요구", "자료제출"),
    # ── 약속·검토사항(답변 측) ──────────────────────────────
    Rule("commitment", _r(r"검토(?:를|도)?\s*(?:해\s*보|하|해\s*나가)(?:도록\s*하)?겠습니다"), _ANSWERISH, 0.8, "검토 약속", "검토"),
    Rule("commitment", _r(r"확인(?:을|도)?\s*(?:해\s*보|하|해\s*드리)(?:도록\s*하)?겠습니다"), _ANSWERISH, 0.75, "확인 약속", "확약"),
    Rule("commitment", _r(r"(?:보고|답변|설명)(?:을|를)?\s*(?:따로\s*|별도로\s*|서면으로\s*)?드리(?:도록\s*하)?겠습니다"), _ANSWERISH, 0.8, "보고·답변 약속", "확약"),
    Rule("commitment", _r(r"(?:조치|개선|시정|마련|추진|반영|협의|점검|조사|보완|정비|강화|재검토)(?:를|을|도)?\s*(?:하|해\s*나가|해\s*보)(?:도록\s*하)?겠습니다"), _ANSWERISH, 0.7, "조치 약속", "확약"),
    Rule("commitment", _r(r"(?:고민|노력|최선)(?:을|를)?\s*(?:하|다하)(?:도록\s*하)?겠습니다"), _ANSWERISH, 0.6, "노력 약속", "의례"),
    Rule("commitment", _r(r"(?:챙기|살펴보|들여다보|알아보)(?:도록\s*하)?겠습니다"), _ANSWERISH, 0.6, "확인 약속", "검토"),
    Rule("commitment", _r(r"(?:공급|지원|배분|시행|도입)(?:을|를)?\s*하도록\s*하겠습니다"), _ANSWERISH, 0.55, "이행 약속", "확약"),
    # '제출하겠습니다', '자료 제출하도록 하겠습니다', '종합감사 전까지 자료 드리겠습니다', '보내 드리겠습니다'
    Rule("commitment", _r(r"(?:자료(?:를|는|도|로|으로)?\s*(?:제출|송부|전달)?|제출|송부|전달)(?:토록|을|를)?\s*"
                          r"(?:하|해\s*드리|드리)(?:도록\s*하)?겠습니다"), _ANSWERISH, 0.8, "자료 제출 약속", "확약"),
    Rule("commitment", _r(r"(?:보내|송부해|전달해|제출해|정리해서)\s*드리(?:도록\s*하)?겠습니다"), _ANSWERISH, 0.8, "자료 제출 약속", "확약"),
    # ── 지적·요구사항(위원 측) ──────────────────────────────
    Rule("issue", _r(r"(?:마련|개선|보완|시정|정비|해결|조치|강화)(?:을|를)?\s*해야\s*(?:되지\s*않|하지\s*않|됩니다|합니다|되겠|하겠|될\s*것|할\s*것)"), _MEMBERISH, 0.6, "개선 촉구"),
    Rule("issue", _r(r"(?:검토|조치|개선|시정)\s*(?:하십시오|해\s*주십시오|해\s*주세요|해\s*주시기\s*바랍니다)"), _MEMBERISH, 0.65, "조치 요구"),
    Rule("issue", _r(r"(?:챙겨|신경\s*(?:좀\s*)?써)\s*주(?:세요|십시오|시기|도록)"), _MEMBERISH, 0.5, "관심 촉구"),
    Rule("issue", _r(r"(?:지적|문제\s*제기)(?:을|를)?\s*(?:드립니다|합니다|하겠습니다|했습니다)"), _MEMBERISH, 0.6, "지적"),
    # ── 증인·참고인, 서면질의 ───────────────────────────────
    Rule("witness_request", _r(r"증인(?:으로|을|을\s*추가로)?\s*(?:부르|채택|출석|소환|신청|요청)"), _ANY_SPEAKER | {"chair"}, 0.8, "증인 요구"),
    Rule("written_question", _r(r"서면(?:으로)?\s*(?:질의|질문|드릴|드리겠|보내)"), _MEMBERISH, 0.7, "서면질의 예고"),
    Rule("written_question", _r(r"서면질의(?:에\s*대한)?\s*답변서"), frozenset({"chair", "other", "unknown"}), 0.8, "서면답변 제출 지시"),
]

# 약속 규칙이 잘못 잡는 진행·질의 표현
_COMMIT_NEG = _r(r"(?:질의|질문|말씀|소개|선포|종결|마치|실시|묻|여쭙|시작)(?:를|을)?\s*(?:갖다\s*)?(?:드리|하)(?:도록\s*하)?겠습니다$")

# 위원(또는 역할 미상) 발언의 '~하겠습니다'가 기관 약속이 아닌 경우
#  • 인용: '검토하겠습니다 이렇게 답변하셨는데', '개선하겠습니다라고 하셨잖아요'
_QUOTE_VERB = r"(?:답변|답하|말씀|얘기|약속|하셨|하시|했|넘어)"
_COMMIT_QUOTED = _r(r"^\s*(?:(?:이렇게|그렇게|하고)\s*" + _QUOTE_VERB + r"|라고|이라고|하셨|했는데|했잖|라는|라며|라던|"
                    r"해\s*놓고|하더니)")
#  • 위원 본인의 행동 예고·과거 언급: '제가 다시 확인하겠습니다', '저도 끝까지 챙기겠습니다', '작년 국감 때 …'
#    ('저희가'는 답변자가 기관을 가리킬 때 더 흔해 넣지 않는다 — 한 줄로 합쳐진 답변의 약속을 놓치지 않도록)
_MEMBER_SELF = _r(r"(?:^|\s)(?:제가|저도|저는|본\s*위원(?:이|은|도)?)(?=\s|$)|작년|지난해|지난번|지난\s*국감|"
                  r"당시|그때|전에도|예전에|다음\s*(?:질의|질문|보충|차례)")
# 줄 안에서 답변이 시작되는 표지(STT 가 질의·답변을 한 줄로 합친 경우) — 이 뒤는 답변자의 말로 본다
_ANSWER_TURN = _r(r"(?:^|\s)(?:예|네|아\s*예|알겠습니다|그렇습니다|맞습니다)(?=\s|$)")
_MEMBER_CHECK_WINDOW = 25

# 지적 규칙이 서두('한번 제가 지적을 하겠습니다')만 잡은 경우 — 내용이 없으면 항목으로 만들지 않는다
_ISSUE_PREFACE = _r(r"^(?:(?:한번|제가|좀|이제|그래서|그리고|또|먼저|마지막으로|추가로|하나|한\s*가지|몇\s*가지|두\s*가지|"
                    r"세\s*가지|다시|이\s*부분|이\s*문제)\s*)*$")


def _member_commitment_excluded(text: str, m: re.Match[str], role: str) -> bool:
    """약속 규칙 매치가 기관의 약속이 아니라 인용·위원 본인 행동이면 True."""
    if _COMMIT_QUOTED.search(text[m.end():m.end() + 15]):
        return True  # 과거 답변·약속 인용은 누가 말했든 새 약속이 아니다
    if role not in ("member", "unknown"):
        return False
    window = sentence_head(text, m.start())[-_MEMBER_CHECK_WINDOW:]
    if _ANSWER_TURN.search(window):
        return False  # 줄 안에서 답변이 시작됐다('…보셨습니까 예 제가 확인하겠습니다') — 답변자의 약속
    return bool(_MEMBER_SELF.search(window))


def _issue_preface_only(text: str, m: re.Match[str]) -> bool:
    head = sentence_head(text, m.start()).strip()
    return len(head) <= 30 and bool(_ISSUE_PREFACE.match(head))


_DUE = [
    (_r(r"일주일\s*(?:내|이내|안)"), lambda d: d + timedelta(days=7)),
    # 'N월 D일 안으로/이내/까지' 는 'D일 이내' 보다 먼저 본다(날짜를 +D일로 오인하지 않도록)
    (_r(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:까지|내|이내|안|전)"), None),
    (_r(r"(?<![\d월])(?<!월\s)(\d{1,2})\s*일\s*(?:내|이내|안)"), None),
    (_r(r"(?:종합\s*(?:감사|국감)|(?<![가-힣])종감)\s*(?:전|때|까지)"), None),
]


def due_hint(text: str, base: date | None = None) -> str:
    """'일주일 내', '3일 이내', '10월 23일까지', '종합감사 전'('종감 때') 등을 날짜/문구로.

    base 는 그 발언이 나온 날짜(KST)다. 'N월 D일' 이 기준일보다 한참 앞이면 다음 해로 본다(12월 → 1월).
    """
    base = base or datetime.now().date()
    for pat, fn in _DUE:
        m = pat.search(text)
        if not m:
            continue
        if fn is not None:
            return fn(base).isoformat()
        g = m.groups()
        if len(g) == 1 and g[0]:
            return (base + timedelta(days=int(g[0]))).isoformat()
        if len(g) == 2:
            try:
                d = date(base.year, int(g[0]), int(g[1]))
            except ValueError:
                return ""
            if d < base - timedelta(days=60):
                try:
                    d = date(base.year + 1, d.month, d.day)
                except ValueError:
                    return ""
            return d.isoformat()
        return "종합감사 전"
    return ""


def extract_items(text: str, role: str, speaker: str = "", requester: str = "",
                  base_date: date | None = None) -> list[ExtractedItem]:
    """한 발언에서 규칙 적중 항목을 뽑는다. 같은 문장에 여러 규칙이 걸리면 종류별 1건만 남긴다.

    base_date: 그 발언이 나온 날짜(KST). 'N일 이내' 같은 기한은 이 날짜 기준으로 계산한다(없으면 오늘).
    """
    text = norm(text)
    if not text:
        return []
    found: dict[tuple[str, str], ExtractedItem] = {}
    for rule in RULES:
        if role not in rule.roles:
            continue
        for m in rule.pattern.finditer(text):
            quote = sentence_at(text, m.start(), normalized=True)
            if rule.kind == "commitment":
                tail = text[max(0, m.start() - 12):m.end()]
                if _COMMIT_NEG.search(tail) or _member_commitment_excluded(text, m, role):
                    continue
            elif rule.kind == "issue" and rule.label == "지적" and _issue_preface_only(text, m):
                continue
            key = (rule.kind, quote)
            if key in found:
                continue
            item = ExtractedItem(kind=rule.kind, text=summarize_quote(rule.kind, quote, rule.label), quote=quote,
                                 strength=rule.strength, due_date=due_hint(quote, base_date),
                                 speaker=speaker, requester=requester, confidence=rule.confidence, origin="rule")
            found[key] = item
    return list(found.values())


_LEAD_FILLERS = re.compile(r"^(?:그리고|그래서|그러니까|그런데|그러면|예|네|아니|이제|좀|저는|제가|한번|또|특히)\s+")


def summarize_quote(kind: str, quote: str, label: str) -> str:
    """규칙 추출 항목의 '요지' — LLM 없이 쓸 수 있도록 인용문을 다듬는다."""
    q = quote.strip("… ")
    for _ in range(3):
        q2 = _LEAD_FILLERS.sub("", q)
        if q2 == q:
            break
        q = q2
    if len(q) > 90:
        q = q[:88].rstrip() + "…"
    return f"[{label}] {q}"


# ── 감시 키워드(우리 기관 언급) ─────────────────────────────────

# 낱말 경계를 지켜야 하는 범주(기관명·별칭·기관장). 사업·정책·기타 감시 키워드는 합성어 안에서도 잡는다('재하도급'의 '하도급').
BOUNDED_CATEGORIES = frozenset({"agency", "person"})


def find_mentions(text: str, terms: list[tuple[str, str]]) -> list[Mention]:
    text = norm(text)
    out: list[Mention] = []
    taken: list[tuple[int, int]] = []
    for term, cat in terms:  # 긴 키워드 우선(호출자가 정렬)
        for m in flexible_pattern(term, cat in BOUNDED_CATEGORIES).finditer(text):
            s, e = m.span()
            if any(not (e <= a or s >= b) for a, b in taken):
                continue  # '한국도로공사' 안의 '도로공사' 중복 방지
            taken.append((s, e))
            out.append(Mention(keyword=term, category=cat, start=s, end=e))
    out.sort(key=lambda x: x.start)
    return out


def assign_department(text: str, departments: list[dict]) -> str:
    c = compact(text)
    best, best_hits = "", 0
    for d in departments or []:
        hits = sum(1 for kw in d.get("keywords") or [] if kw and compact(kw) in c)
        if hits > best_hits:
            best, best_hits = d.get("name", ""), hits
    return best


# ── 키워드(해시태그) — LLM 없을 때 대체 ──────────────────────────

_JOSA = re.compile(
    r"(?:에서는|에서도|으로는|으로도|에게는|이라는|라는|이라고|라고|에서|에게|께서|으로|부터|까지|처럼|보다|하고|이나|"
    r"이랑|에는|에도|과는|와는|들이|들을|들은|들의|은|는|이|가|을|를|에|의|도|로|와|과|만|요)$"
)
_STOP = set("""
질의 질문 답변 말씀 생각 부분 문제 그런 이런 저런 지금 우리 이제 그래서 그리고 그러니까 그런데 하는 있는 없는 같은
것 거 예 네 장관 장관님 위원 위원님 위원장 위원장님 사장 사장님 원장 원장님 청장 정도 경우 그것 이것 저것 여기
거기 수고 감사 국정감사 오늘 시간 다음 한번 조금 많이 계속 사실 관련 대해서 대한 통해서 때문에 그렇게 이렇게
어떻게 왜 뭐 무슨 그때 지난번 아까 지금까지 모두 여러 각 해당 정말 진짜 바로 다시 또 좀 더 가장 굉장히 그거
이거 저희 제가 저는 우리가 여러분 국민 정부 국가 생각합니다 하겠습니다 있습니다 없습니다 됩니다 합니다
""".split())


def keywords(texts: list[str], top: int = 8) -> list[str]:
    cnt: Counter[str] = Counter()
    for t in texts:
        for tok in norm(t).split():
            tok = re.sub(r"[^가-힣A-Za-z0-9]", "", tok)
            for _ in range(2):
                tok = _JOSA.sub("", tok)
            if len(tok) < 2 or tok in _STOP or tok.isdigit():
                continue
            if re.search(r"(?:습니다|니다|니까|세요|어요|아요|지요|겠|했|하는|하고|해서|되는|된|할)$", tok):
                continue
            cnt[tok] += 1
    return [w for w, n in cnt.most_common(top * 2) if n >= 2][:top]


def extractive_points(texts: list[str], limit: int = 3, max_len: int = 110) -> list[str]:
    """블록 요약 대체: 키워드 밀도가 높은 문장 상위 N개(원래 순서 유지)."""
    kws = set(keywords(texts, top=12))
    sents: list[tuple[int, int, str]] = []
    i = 0
    for t in texts:
        for s in split_sentences(t):
            if len(s) < 12:
                continue
            score = sum(1 for k in kws if k in s) * 10 + min(len(s), 80) // 20
            sents.append((score, i, s))
            i += 1
    chosen = sorted(sorted(sents, key=lambda x: -x[0])[:limit], key=lambda x: x[1])
    out = []
    for _, _, s in chosen:
        s = _LEAD_FILLERS.sub("", s)
        out.append(s if len(s) <= max_len else s[: max_len - 1].rstrip() + "…")
    return out
