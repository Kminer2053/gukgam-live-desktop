"""한국어 회의 전사문 공통 유틸(문장부호 없는 STT 문장 분할, 이름 정규화 등)."""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from functools import lru_cache

_WS = re.compile(r"\s+")

# STT 결과에는 문장부호가 거의 없으므로 종결어미 뒤 공백을 문장 경계로 본다.
_SENT_END = re.compile(
    r"(?:니다|니까|세요|시오|어요|아요|해요|지요|네요|군요|구요|고요|래요|게요|에요|예요|겠죠|거든요|잖아요|"
    r"습니까|십시오|바랍니다|합시다|됩니다|않습니까|그렇죠|맞죠|있죠|없죠|하죠|이죠|[.?!])(?=\s)"
)

PARTIES = ("더불어민주당", "국민의힘", "조국혁신당", "개혁신당", "진보당", "기본소득당", "사회민주당", "무소속")

# 이름 자리에 잘못 잡히기 쉬운 단어
NAME_STOPWORDS = {
    "다음은", "다음", "이어서", "수고", "그러면", "우리", "존경하는", "여러", "모든", "질의", "위원장", "위원",
    "간사", "여당", "야당", "그다음", "그다음은", "먼저", "끝으로", "마지막", "마지막으로", "추가", "보충",
    "다른", "어떤", "같은", "해당", "전체", "각", "민주당", "국민의힘", "선배", "동료", "존경", "친애하는",
    # 직위·역할어(화자 힌트가 역할만 줄 때 이름으로 오인하지 않도록)
    "답변", "답변자", "증인", "참고인", "기관", "장관", "차관", "사장", "원장", "청장", "이사장", "처장", "실장",
    "국장", "본부장", "위원님", "의원", "의원님", "간사님", "기록", "미상",
}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    return _WS.sub(" ", text).strip()


def compact(text: str) -> str:
    return _WS.sub("", text or "")


def split_sentences(text: str) -> list[str]:
    text = norm(text)
    if not text:
        return []
    parts: list[str] = []
    last = 0
    for m in _SENT_END.finditer(text):
        parts.append(text[last:m.end()].strip())
        last = m.end()
    parts.append(text[last:].strip())
    return [p for p in parts if p]


@lru_cache(maxsize=64)
def _sentence_spans(text: str) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...]]:
    """정규화된 text 의 (문장 시작 위치들, 문장 (시작, 끝) 위치들). split_sentences 와 같은 경계 —
    한 발언의 여러 매치가 재사용한다."""
    spans: list[tuple[int, int]] = []
    last = 0
    for m in _SENT_END.finditer(text):
        seg = text[last:m.end()]
        lead = len(seg) - len(seg.lstrip())
        if seg.strip():
            spans.append((last + lead, m.end()))
        last = m.end()
    seg = text[last:]
    if seg.strip():
        lead = len(seg) - len(seg.lstrip())
        spans.append((last + lead, last + len(seg.rstrip())))
    return tuple(s for s, _ in spans), tuple(spans)


def sentence_at(text: str, pos: int, min_len: int = 18, max_len: int = 220, *, normalized: bool = False) -> str:
    """pos 를 포함하는 문장(짧으면 앞 문장까지)을 돌려준다. 인용·근거 표시용.

    normalized=True 면 text 가 이미 norm() 을 거친 것으로 보고 다시 정규화하지 않는다(긴 발언의 반복 호출용).
    """
    if not normalized:
        text = norm(text)
    if not text:
        return ""
    starts, spans = _sentence_spans(text)
    if not spans:
        return ""
    i = bisect_right(starts, pos) - 1
    idx = i if i >= 0 and pos <= spans[i][1] else len(spans) - 1
    sents = [text[s:e] for s, e in spans[max(0, idx - 1):idx + 1]]
    out = sents[-1]
    if len(out) < min_len and idx > 0:
        out = sents[0] + " " + out
    if len(out) > max_len:
        # 너무 긴 '문장'(종결어미가 안 잡힌 경우)은 매치 주변만 자른다
        rel = max(0, pos - text.find(out))
        a = max(0, rel - max_len // 2)
        out = out[a:a + max_len]
        out = ("…" if a > 0 else "") + out.strip() + "…"
    return out


def is_valid_name(name: str | None) -> bool:
    if not name:
        return False
    if name in NAME_STOPWORDS or any(name.startswith(p) for p in ("다음", "이어")):
        return False
    return bool(re.fullmatch(r"[가-힣]{2,4}", name))


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def sentence_head(text: str, pos: int, lookback: int = 200) -> str:
    """pos 가 들어 있는 문장의 시작부터 pos 직전까지(종결어미 기준 문장 경계).

    긴 발언에서 매치마다 처음부터 훑지 않도록 pos 앞 lookback 글자 안에서만 경계를 찾는다
    (그 안에 경계가 없으면 lookback 글자만 돌려준다).
    """
    pos = max(0, pos)
    start = max(0, pos - lookback)
    for m in _SENT_END.finditer(text, start, pos):
        start = m.end()
    return text[start:pos]


# ── 기관명·인명 경계 ─────────────────────────────────────────────
# 앞 글자가 한글·영숫자면 다른 낱말의 일부('한국철도공사' 안의 '도공', '고속도로 공사' 안의 '도로 공사')로 본다.
_TERM_LEAD = r"(?<![가-힣A-Za-z0-9])"
# 짧은 별칭·인명 뒤에 붙어도 되는 말(조사·서술격·호칭·직함). 그 밖의 한글이 이어지면 다른 낱말('도공무원')로 본다.
_TERM_TAIL_WORDS = (
    # 조사
    "은", "는", "이", "가", "을", "를", "에", "의", "도", "와", "과", "로", "으로", "랑", "이랑", "하고", "만",
    "까지", "부터", "처럼", "보다", "나", "이나", "야", "이야", "요", "께", "께서", "에게", "한테", "마저", "조차",
    "뿐", "밖에", "라", "이라", "란", "이란", "든", "이든", "며", "이며", "고", "이고", "다", "이다", "입니", "인",
    "였", "이었", "죠", "이죠", "잖", "들", "같", "쪽", "측",
    # 호칭·직함
    "님", "씨", "사장", "장관", "차관", "원장", "청장", "이사장", "처장", "실장", "국장", "본부장", "대표", "회장",
    "총장", "소장", "위원", "의원", "직원", "관계자", "노조",
)
_TERM_TAIL_HANGUL = r"(?=$|[^가-힣]|(?:" + "|".join(sorted(_TERM_TAIL_WORDS, key=len, reverse=True)) + "))"
_TERM_TAIL_ALNUM = r"(?![A-Za-z0-9])"
SHORT_TERM_LEN = 3          # 이 길이 이하 별칭·인명은 앞뒤 경계를 모두 본다(띄어 쓴 형태는 인정하지 않음)
SPACED_TERM_MIN_LEN = 5     # 이 길이 이상 정식명만 글자 사이 띄어쓰기를 허용('한국 도로공사')


def _tail_guard(c: str) -> str:
    return _TERM_TAIL_HANGUL if re.match(r"[가-힣]", c[-1]) else _TERM_TAIL_ALNUM


@lru_cache(maxsize=1024)
def flexible_pattern(term: str, bounded: bool = False) -> re.Pattern[str]:
    """'한국도로공사' 가 '한국 도로공사' 로 받아써져도 잡히도록 글자 사이 공백을 허용한다.

    bounded=True(기관명·별칭·인명)면 낱말 경계를 둔다.
      • 앞: 한글·영숫자가 붙어 있으면 불일치('한국철도공사'의 '도공', '수도공급', '고속도로 공사')
      • 3글자 이하: 뒤에도 조사·호칭 또는 한글이 아닌 글자만 허용('도공무원' 제외), 띄어 쓴 형태 불인정
      • 4글자: 앞 경계만, 띄어 쓴 형태 불인정('도로 공사 현장' 은 일반 명사)
      • 5글자 이상: 붙여 쓴 형태는 앞 경계만, 띄어 쓴 형태('한국 도로공사')는 뒤 경계까지 확인
    모든 패턴은 중첩 반복이 없어 입력 길이에 선형이다(긴 발언에서도 역추적 폭주 없음).
    """
    c = compact(term)
    if not c:
        return re.compile(r"(?!x)x")
    chars = [re.escape(ch) for ch in c]
    if not bounded:
        return re.compile(r"\s?".join(chars))
    literal = "".join(chars)
    tail = _tail_guard(c)
    if len(c) <= SHORT_TERM_LEN:
        return re.compile(_TERM_LEAD + literal + tail)
    if len(c) < SPACED_TERM_MIN_LEN:
        return re.compile(_TERM_LEAD + literal)
    spaced = r"\s?".join(chars)
    return re.compile(_TERM_LEAD + "(?:" + literal + "|" + spaced + tail + ")")
