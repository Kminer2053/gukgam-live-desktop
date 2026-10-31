"""원문 문자와 띄어쓰기를 보존하는 표시용 문장·문단 삽입 규칙."""
from __future__ import annotations

import re
from typing import Any


FORMAT_VERSION = 1
PARAGRAPH_TARGET = 260
PARAGRAPH_LIMIT = 320
ALLOWED_INSERT = re.compile(r"[.!?,\n]+\Z")

# 명사 '필요'와 평서·인용 '다고'를 자르지 않도록 확인 가능한 어미만 쓴다.
QUESTION_END = re.compile(r"(?:습니까|입니까|인가요|일까요|을까요|할까요|될까요|볼까요|"
                          r"아시죠|계시죠|하시죠|맞죠|그렇죠|있죠|없죠|되지요|하시지요|"
                          r"셨지요|그러지요|들지요|나요)\Z")
STATEMENT_END = re.compile(r"(?:니다|시오|바랍니다|드립니다|주세요|세요|예요|이에요|"
                           r"어요|아요|해요|돼요|봐요|줘요|워요|여요|거든요|더라고요|잖아요|네요)\Z")
WORD = re.compile(r"[가-힣]+(?:[.!?…]+)?")
REPORTED_NEXT = re.compile(r"(?:라고|라는|라며|라면서|하고|하는|한다고|고(?:\s|$))")
CONNECTOR = re.compile(r"(?<!\S)(?:그런데|그러나|다음으로|또한|반면에|첫째|둘째|셋째|넷째|"
                       r"다섯째|이어서|따라서|그래서|그리고|다만|우선|마지막으로)(?=\s)")
QUOTE_PAIRS = {"\"": "\"", "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』",
               "(": ")", "[": "]", "{": "}", "〈": "〉", "《": "》"}


class ReadabilityError(ValueError):
    """원문·비밀값을 출력하지 않는 형식 검증 오류."""


def apply_edits(text: str, edits: list[dict[str, Any]]) -> str:
    """원문 위치의 부호·개행 삽입만 허용해 표시 본문을 재현한다."""
    if not isinstance(text, str) or not isinstance(edits, list):
        raise ReadabilityError("본문과 삽입 이력 형식이 잘못됐습니다.")
    parts: list[str] = []
    cursor = 0
    previous = -1
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"offset", "insert"}:
            raise ReadabilityError("삽입 이력은 위치와 추가 문자만 가질 수 있습니다.")
        offset, insertion = edit["offset"], edit["insert"]
        if (type(offset) is not int or not previous < offset <= len(text) or
                not isinstance(insertion, str) or not ALLOWED_INSERT.fullmatch(insertion)):
            raise ReadabilityError("본문 교체·삭제 또는 부호 이외의 삽입은 허용하지 않습니다.")
        parts.extend((text[cursor:offset], insertion))
        cursor = offset
        previous = offset
    parts.append(text[cursor:])
    return "".join(parts)


def _protected_characters(text: str) -> list[bool]:
    """인용·괄호 안의 문장과 영문 단어 내부의 따옴표를 구별한다."""
    protected = [False] * len(text)
    stack: list[str] = []
    for index, char in enumerate(text):
        inside_word = (char in ("'", "\"") and index > 0 and index + 1 < len(text) and
                       text[index - 1].isalnum() and text[index + 1].isalnum())
        if stack:
            protected[index] = True
            if char == stack[-1] and not inside_word:
                stack.pop()
            elif char in QUOTE_PAIRS and char not in ("'", "\""):
                stack.append(QUOTE_PAIRS[char])
        elif char in QUOTE_PAIRS and not inside_word:
            protected[index] = True
            stack.append(QUOTE_PAIRS[char])
    return protected


def _next_word(text: str, offset: int) -> str:
    return text[offset:].lstrip()


def _after_gap(text: str, offset: int) -> int:
    """원래 공백은 남겨 두고 다음 글자 앞에 문단을 추가한다."""
    while offset < len(text) and text[offset] in " \t":
        offset += 1
    return offset


def _existing_sentence_end(text: str, index: int, protected: list[bool]) -> bool:
    """소수점·버전·영문 약어·목록 번호의 점을 문장 종결로 세지 않는다."""
    char = text[index]
    if protected[index] or char not in ".!?…":
        return False
    if index + 1 < len(text) and text[index + 1] in ".!?…":
        return False
    if index + 1 < len(text) and not text[index + 1].isspace():
        return False
    if char == ".":
        start = index
        while start and not text[start - 1].isspace():
            start -= 1
        token = text[start:index]
        if not token or token.isdigit() or re.fullmatch(r"[A-Za-z0-9.]+", token):
            return False
    if REPORTED_NEXT.match(_next_word(text, index + 1)):
        return False
    return True


def _sentence_edits(text: str, protected: list[bool]) -> tuple[dict[int, str], list[int]]:
    """격식·구어 종결어미 뒤에만 부호를 넣고 인용 연결은 보류한다."""
    additions: dict[int, str] = {}
    boundaries = {i + 1 for i in range(len(text)) if _existing_sentence_end(text, i, protected)}
    for match in WORD.finditer(text):
        word = match.group()
        if word[-1] in ".!?…":
            continue
        end = match.end()
        if any(protected[match.start():end]) or (end < len(text) and not text[end].isspace()):
            continue
        if REPORTED_NEXT.match(_next_word(text, end)):
            continue
        question = QUESTION_END.search(word)
        if question or STATEMENT_END.search(word):
            additions[end] = "?" if question else "."
            boundaries.add(end)
    # '네 가지' 같은 수량 표현과 '예 들면'을 짧은 응답으로 오인하지 않는다.
    first = re.match(r"\s*(예|네)(?=\s|$)", text)
    if first and not any(protected[first.start():first.end()]):
        rest = _next_word(text, first.end())
        quantity = r"(?:가지|개|명|분|사람|차례|번째)(?:를|은|는|이|가|로|의|에|에서|이나|만|도)?(?:\s|$)"
        if not (re.match(quantity, rest) or re.match(r"(?:들면|들어|들자면|드리면)(?:\s|$)", rest)):
            additions[first.end()] = "."
            boundaries.add(first.end())
    return additions, sorted(boundaries)


def _paragraphs(text: str, boundaries: list[int], protected: list[bool]) -> list[int]:
    """세 문장 또는 긴 문단을 나누고 미종결 부분에는 개행만 더한다."""
    breaks: set[int] = set()
    existing = {m.end() for m in re.finditer(r"\n[ \t]*\n", text)}
    paragraph_start = 0
    sentence_count = 0
    for end in boundaries:
        preceding = [offset for offset in existing if paragraph_start < offset <= end]
        if preceding:
            paragraph_start = max(preceding)
            sentence_count = 0
        sentence_count += 1
        offset = _after_gap(text, end)
        if (offset < len(text) and text[offset:].strip() and
                (sentence_count >= 3 or end - paragraph_start >= PARAGRAPH_TARGET)):
            if not text[end:offset + 1].startswith("\n"):
                breaks.add(offset)
            paragraph_start = offset
            sentence_count = 0

    # 긴 미종결 문장은 마침표를 추정하지 않고 기존 공백에서만 숨을 나눈다.
    known = sorted(breaks | existing | {len(text)})
    start = 0
    for end in known:
        cursor = start
        while end - cursor > PARAGRAPH_LIMIT:
            lower, upper = cursor + 150, min(end, cursor + PARAGRAPH_LIMIT)
            connectors = [m.start() for m in CONNECTOR.finditer(text, lower, upper)
                          if not protected[m.start()]]
            if connectors:
                offset = min(connectors, key=lambda x: abs(x - (cursor + PARAGRAPH_TARGET)))
            else:
                # 문장 중간 부호보다 개행을 택하며, 인용 안·단어 내부는 건드리지 않는다.
                gaps = [m.end() for m in re.finditer(r"[ \t]+", text[lower:upper])
                        if lower + m.end() < len(text) and not protected[lower + m.end()]]
                offsets = [lower + gap for gap in gaps]
                if not offsets:
                    break
                offset = min(offsets, key=lambda x: abs(x - (cursor + PARAGRAPH_TARGET)))
            breaks.add(offset)
            cursor = offset
        start = end
    return sorted(breaks)


def format_text(text: str, *, system: bool = False) -> tuple[str, dict[str, Any]]:
    """원래 글자와 띄어쓰기를 모두 남기는 표시용 문장·문단 초안을 만든다."""
    if not isinstance(text, str):
        raise ReadabilityError("말묶음 본문은 문자열이어야 합니다.")
    protected = _protected_characters(text)
    additions, boundaries = ({}, []) if system else _sentence_edits(text, protected)
    breaks = [] if system else _paragraphs(text, boundaries, protected)
    for offset in breaks:
        additions[offset] = additions.get(offset, "") + "\n\n"
    edits = [{"offset": offset, "insert": insertion} for offset, insertion in sorted(additions.items())]
    readable = apply_edits(text, edits)
    last = boundaries[-1] if boundaries else 0
    sentences = len(boundaries) + int(bool(text[last:].strip())) if text.strip() else 0
    paragraphs = len([part for part in re.split(r"\n[ \t]*\n", readable) if part.strip()])
    metadata = {"status": "자동 가독성 초안", "format_version": FORMAT_VERSION,
                "method": "원문 문자 보존 · 종결어미와 기존 공백 기준 부호·문단 삽입",
                "edits": edits, "sentence_count": sentences, "paragraph_count": paragraphs,
                "punctuation_insertions": sum(len(s.replace("\n", "")) for s in additions.values()),
                "paragraph_break_insertions": len(breaks), "unchanged_content": True,
                "system_preserved": system}
    if apply_edits(text, metadata["edits"]) != readable:
        raise ReadabilityError("표시 본문과 삽입 이력의 역검증에 실패했습니다.")
    return readable, metadata
