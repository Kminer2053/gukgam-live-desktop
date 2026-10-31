"""질의 전체의 순서를 지키며 원자막과 같은 녹음의 단어 시각을 대응한다.

고유한 긴 구문으로 먼저 시간 창을 정하고 작은 창 안에서만 추가 비교한다.
행마다 전체 음성을 다시 검색하지 않는다. 부분 근거는 남기되 원문·실명·역할을
수정하거나 모델 출력을 정답으로 승격하지 않는다.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
import heapq
import math
import re
import unicodedata

from .alignment import (
    AlignmentResult, SpeakerInterval, WordAnchor, project_speakers,
)


_SEED_SIZE = 12
_MIN_CONTEXT_WORDS = 2
_MAX_ROWS = 1000
_MAX_CHARS = 120_000
_MAX_WORDS = 20_000
_MAX_SEGMENTS = 10_000
_MAX_PHRASES = 10_000
_MAX_GAP_CAPTION = 240
_MAX_GAP_AUDIO = 360
_MAX_GAP_PRODUCT = 60_000
_MAX_PROJECTION_PRODUCT = 4_000_000
_MAX_SHORT_CAPTION = 80
_MAX_SHORT_AUDIO = 100
_MAX_SHORT_SECONDS = 12


@dataclass(frozen=True)
class _Phrase:
    """정규화 원문·음성 문자열에서 완전히 같은 연속 구문."""

    start: int
    end: int
    audio_start: int
    audio_end: int
    evidence: str = "unique_phrase"


def _normalise(text: str) -> tuple[str, list[int]]:
    """공백·일반 문장부호만 제외한다. 숫자 부호·대소문자·원문 좌표는 보존한다."""
    chars, positions = [], []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        if unicodedata.category(char).startswith("P"):
            numeric = ((index > 0 and text[index - 1].isdigit())
                       or (index + 1 < len(text) and text[index + 1].isdigit()))
            if not numeric and char not in "%/":
                continue
        chars.append(char)
        positions.append(index)
    return "".join(chars), positions


def _time(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 86_400)


def _unique(text: str, phrase: str) -> bool:
    at = text.find(phrase)
    return at >= 0 and text.find(phrase, at + 1) < 0


def _whole_words(start: int, end: int, starts: list[int], ends: list[int]) -> range:
    """구문의 양 끝에 걸친 단어를 제외한다."""
    return range(bisect_left(starts, start), bisect_right(ends, end))


def _phrases(
    captions: str, transcript: str, word_starts: list[int], word_ends: list[int],
) -> list[_Phrase]:
    """고정 길이 색인으로 비교를 제한하고 일치 구문 안에서만 확장한다."""
    caption_grams = Counter(captions[i:i + _SEED_SIZE]
                            for i in range(len(captions) - _SEED_SIZE + 1)
                            if "\0" not in captions[i:i + _SEED_SIZE])
    audio_grams: dict[str, int | None] = {}
    for i in range(len(transcript) - _SEED_SIZE + 1):
        gram = transcript[i:i + _SEED_SIZE]
        audio_grams[gram] = None if gram in audio_grams else i
    result = []
    i = 0
    while i + _SEED_SIZE <= len(captions):
        gram = captions[i:i + _SEED_SIZE]
        at = audio_grams.get(gram)
        if "\0" in gram or caption_grams[gram] != 1 or at is None:
            i += 1
            continue
        begin, audio_begin = i, at
        end, audio_end = i + _SEED_SIZE, at + _SEED_SIZE
        while begin > 0 and audio_begin > 0 and captions[begin - 1] == transcript[audio_begin - 1]:
            begin -= 1
            audio_begin -= 1
        while end < len(captions) and audio_end < len(transcript) and captions[end] == transcript[audio_end]:
            end += 1
            audio_end += 1
        if len(_whole_words(audio_begin, audio_end, word_starts, word_ends)) >= _MIN_CONTEXT_WORDS:
            result.append(_Phrase(begin, end, audio_begin, audio_end))
            if len(result) > _MAX_PHRASES:
                raise ValueError("구문 후보 수 한도를 넘었습니다")
        i = end - _SEED_SIZE + 1
    return result


def _monotonic(phrases: list[_Phrase]) -> tuple[list[_Phrase], bool]:
    """원문과 음성 양쪽에서 겹치지 않는 최대 길이 경로를 찾는다.

    같은 점수의 서로 다른 경로가 있으면 임의로 하나를 택하지 않는다.
    좌표 압축·누적 최댓값 색인으로 긴 질의에서도 후보 제곱 비교를 피한다.
    """
    if not phrases:
        return [], False
    phrases.sort(key=lambda p: (p.start, p.end, p.audio_start))
    ends = sorted({p.audio_end for p in phrases})
    # 점수·마지막 후보·경로 수(최대 2). 같은 경로의 중복 집계는 제외한다.
    tree = [(0, -1, 1)] * (len(ends) + 1)
    states: list[tuple[int, int, int]] = []
    previous: list[int] = []
    pending: list[tuple[int, int]] = []

    def better(left: tuple, right: tuple) -> tuple:
        if left[0] != right[0]:
            return left if left[0] > right[0] else right
        if left[1] == right[1]:
            return left
        return left[0], left[1], 2

    def put(position: int, value: tuple) -> None:
        while position < len(tree):
            tree[position] = better(tree[position], value)
            position += position & -position

    def get(position: int) -> tuple:
        result = (0, -1, 1)
        while position:
            result = better(result, tree[position])
            position -= position & -position
        return result

    best = (0, -1, 1)
    for index, phrase in enumerate(phrases):
        while pending and pending[0][0] <= phrase.start:
            _, available = heapq.heappop(pending)
            put(bisect_left(ends, phrases[available].audio_end) + 1, states[available])
        prior = get(bisect_right(ends, phrase.audio_start))
        previous.append(prior[1])
        state = (prior[0] + phrase.end - phrase.start, index, prior[2])
        states.append(state)
        heapq.heappush(pending, (phrase.end, index))
        best = better(best, state)
    if best[2] > 1:
        return [], True
    indices = []
    index = best[1]
    while index >= 0:
        indices.append(index)
        index = previous[index]
    return [phrases[index] for index in reversed(indices)], False


def _refine(
    captions: str, transcript: str, chain: list[_Phrase],
    word_starts: list[int], word_ends: list[int], words: list[dict],
) -> list[_Phrase]:
    """앞뒤 고유 구문으로 막힌 작은 창에서만 추가 연속 일치를 찾는다."""
    result = list(chain)
    for left, right in zip(chain, chain[1:]):
        raw = captions[left.end:right.start]
        spoken = transcript[left.audio_end:right.audio_start]
        if (not raw or not spoken or len(raw) > _MAX_GAP_CAPTION
                or len(spoken) > _MAX_GAP_AUDIO or len(raw) * len(spoken) > _MAX_GAP_PRODUCT):
            continue
        left_words = _whole_words(left.audio_start, left.audio_end, word_starts, word_ends)
        right_words = _whole_words(right.audio_start, right.audio_end, word_starts, word_ends)
        # 짧은 말은 동일 행의 양끝 고유 문구와 실제 단어 시각으로 범위를 한 번 더 제한한다.
        lower = words[left_words[-1]]["end"]
        upper = words[right_words[0]]["start"]
        short_window = ("\0" not in raw and len(raw) <= _MAX_SHORT_CAPTION
                        and len(spoken) <= _MAX_SHORT_AUDIO
                        and 0 <= upper - lower <= _MAX_SHORT_SECONDS)
        longer = []
        for block in SequenceMatcher(None, raw, spoken, autojunk=False).get_matching_blocks():
            if not block.size:
                continue
            phrase = raw[block.a:block.a + block.size]
            if "\0" in phrase or not _unique(raw, phrase) or not _unique(spoken, phrase):
                continue
            start, end = left.audio_end + block.b, left.audio_end + block.b + block.size
            indices = _whole_words(start, end, word_starts, word_ends)
            if block.size >= 8 and len(indices) >= _MIN_CONTEXT_WORDS:
                longer.append(_Phrase(left.end + block.a, left.end + block.a + block.size,
                                      start, end, "bounded_context_phrase"))
        result.extend(longer)
        if short_window:
            shorter = []
            # SequenceMatcher가 동점 교차 후보를 먼저 버리게 하지 않고 전체 단어를 검사한다.
            for index in _whole_words(left.audio_end, right.audio_start, word_starts, word_ends):
                begin, stop = word_starts[index], word_ends[index]
                complete = transcript[begin:stop]
                if (not 2 <= len(complete) <= 7 or not _unique(raw, complete)
                        or not _unique(spoken, complete)
                        or words[index]["start"] < lower or words[index]["end"] > upper
                        or (index > 0 and words[index - 1]["end"] > words[index]["start"])
                        or (index + 1 < len(words) and words[index]["end"] > words[index + 1]["start"])):
                    continue
                original = left.end + raw.index(complete)
                candidate = _Phrase(original, original + len(complete), begin, stop,
                                    "bounded_short_complete_words")
                if any(p.start <= candidate.start and candidate.end <= p.end
                       and p.audio_start <= begin and stop <= p.audio_end for p in longer):
                    continue
                shorter.append(candidate)
            ordered = sorted([*longer, *shorter], key=lambda p: p.start)
            # 문자 또는 음성의 순서가 하나라도 충돌하면 짧은 후보를 임의로 골라 살리지 않는다.
            if all(a.end <= b.start and a.audio_end <= b.audio_start
                   for a, b in zip(ordered, ordered[1:])):
                result.extend(shorter)
    return sorted(result, key=lambda p: p.start)


def _complete_original_tokens(text: str, anchors: list[dict], positions: list[int]) -> list[dict]:
    """부정어·숫자 부호를 남겨 놓고 같은 어절의 일부만 대응하지 않는다.

    띄어쓰기를 달리한 완전 일치는 허용하지만, 원문 어절에 미일치 문자가
    있으면 그 어절에 걸친 단어 근거 전체를 보류한다.
    """
    covered = set()
    for anchor in anchors:
        covered.update(positions[bisect_left(positions, anchor["start"]):
                                 bisect_left(positions, anchor["end"])])
    intervals = [(a["start"], a["end"], i, False) for i, a in enumerate(anchors)]
    for token in re.finditer(r"\S+", text):
        meaningful = positions[bisect_left(positions, token.start()):bisect_left(positions, token.end())]
        intervals.append((token.start(), token.end(), -1, any(p not in covered for p in meaningful)))
    # ASR 한 단어가 원문 여러 어절에 걸칠 때도 미일치가 이어진 범위를 함께 보류한다.
    accepted, component, end, invalid = set(), [], -1, False
    for start, stop, index, bad in sorted(intervals):
        if start >= end:
            if not invalid:
                accepted.update(component)
            component, end, invalid = [], -1, False
        end, invalid = max(end, stop), invalid or bad
        if index >= 0:
            component.append(index)
    if not invalid:
        accepted.update(component)
    return [a for i, a in enumerate(anchors) if i in accepted]


def align_question(
    rows: list[dict], words: list[dict], segments: list[dict], *, media_sha256: str,
) -> dict:
    """순서대로 받은 질의 원문에 같은 녹음의 초 단위 모델 캐시를 대응한다.

    해시를 전달한 호출자가 두 캐시의 녹음 동일성을 먼저 검사해야 한다.
    이 순수 함수는 파일·DB·모델에 접근하지 않는다. `draft`는 청취 전 대조
    근거이며 실명·역할·질의 귀속의 정답이나 운영 채택 승인이 아니다.
    """
    if not isinstance(media_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", media_sha256):
        raise ValueError("음성 파일 SHA256이 필요합니다")
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        raise ValueError("자막 행 형식·한도가 잘못됐습니다")
    source_rows, seen, size = [], set(), 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise ValueError("원문 자막 형식이 잘못됐습니다")
        uid = row.get("id")
        if not isinstance(uid, (int, str)) or isinstance(uid, bool) or uid in seen:
            raise ValueError("자막 식별자는 중복되지 않아야 합니다")
        seen.add(uid)
        size += len(row["text"])
        if size > _MAX_CHARS or "\0" in row["text"]:
            raise ValueError("원문 문자 수 또는 제어 문자 한도를 넘었습니다")
        source_rows.append({"id": uid, "text": row["text"]})

    normalised, positions, row_starts, row_ends, cursor = [], [], [], [], 0
    for row in source_rows:
        compact, mapping = _normalise(row["text"])
        if normalised:
            cursor += 1
        row_starts.append(cursor)
        cursor += len(compact)
        row_ends.append(cursor)
        normalised.append(compact)
        positions.append(mapping)
    captions = "\0".join(normalised)
    common = {"schema_version": 1, "media_sha256": media_sha256.lower(),
              "validation_status": "not_verified", "quality_resolved": False,
              "accuracy_percent": None, "production_changed": False}

    def finish(results: list[dict], issue: str | None = None, **extra) -> dict:
        parts = [part for row in results for part in row["parts"]]
        summary = {"rows": len(results), "draft_rows": sum(r["status"] == "draft" for r in results),
                   "held_rows": sum(r["status"] == "held" for r in results),
                   "anchors": sum(len(r["anchors"]) for r in results),
                   "draft_parts": sum(p["status"] == "draft" for p in parts),
                   "held_parts": sum(p["status"] == "held" for p in parts),
                   "unspoken_parts": sum(p["reason"] == "unspoken_spacing_or_punctuation" for p in parts),
                   "total_chars": sum(map(len, normalised)),
                   "matched_chars": sum(r.get("matched_chars", 0) for r in results),
                   "validation_status": "not_verified", "accuracy_percent": None,
                   "issues": [issue] if issue else [], **extra}
        return {**common, "rows": results, "summary": summary}

    def held(issue: str) -> dict:
        results = []
        for row in source_rows:
            text = row["text"]
            parts = [{"start": 0, "end": len(text), "text": text, "status": "held",
                      "speaker_id": None, "start_ms": None, "end_ms": None,
                      "reason": issue, "validation_status": "not_verified"}] if text else []
            results.append({**row, "status": "held", "parts": parts, "anchors": [],
                            "issues": [issue], "matched_chars": 0})
        return finish(results, issue)

    if not isinstance(words, list) or len(words) > _MAX_WORDS:
        return held("invalid_asr_words")
    if not words:
        return held("missing_asr_words")
    pieces, word_starts, word_ends = [], [], []
    word_cursor, previous_start, previous_end = 0, -1, -1
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str):
            return held("invalid_asr_word")
        start, end = word.get("start"), word.get("end")
        if not _time(start) or not _time(end) or start >= end:
            return held("invalid_asr_time")
        if start < previous_start or end < previous_end:
            return held("nonmonotonic_asr_time")
        previous_start, previous_end = start, end
        piece, _ = _normalise(word["text"])
        if not piece or "\0" in piece:
            return held("empty_asr_word")
        word_starts.append(word_cursor)
        word_cursor += len(piece)
        word_ends.append(word_cursor)
        pieces.append(piece)
        if word_cursor > _MAX_CHARS:
            return held("input_too_large")
    if not isinstance(segments, list) or len(segments) > _MAX_SEGMENTS:
        return held("invalid_speaker_intervals")
    intervals = []
    for segment in segments:
        if not isinstance(segment, dict):
            return held("invalid_speaker_interval")
        start, end, speaker = segment.get("start"), segment.get("end"), segment.get("speaker")
        if (not _time(start) or not _time(end) or start >= end
                or not isinstance(speaker, str) or not speaker.strip()):
            return held("invalid_speaker_interval")
        intervals.append(SpeakerInterval(start * 1000, end * 1000, speaker))
    transcript = "".join(pieces)
    # 비정상적으로 많은 겹침 구간으로 기존 투영기의 제곱 비용을 만들지 않는다.
    if len(words) * len(intervals) > _MAX_PROJECTION_PRODUCT:
        return held("speaker_projection_input_too_large")
    phrases = _phrases(captions, transcript, word_starts, word_ends)
    chain, ambiguous = _monotonic(phrases)
    if ambiguous:
        return held("ambiguous_monotonic_chain")
    selected = _refine(captions, transcript, chain, word_starts, word_ends, words)
    row_anchors: list[list[dict]] = [[] for _ in source_rows]
    chain_set = set(chain)
    rejected = [p for p in phrases if p not in chain_set]
    for phrase in selected:
        row_index = bisect_right(row_starts, phrase.start) - 1
        if row_index < 0 or phrase.end > row_ends[row_index]:
            continue
        mapping = positions[row_index]
        for word_index in _whole_words(phrase.audio_start, phrase.audio_end, word_starts, word_ends):
            begin = phrase.start + word_starts[word_index] - phrase.audio_start - row_starts[row_index]
            end = begin + len(pieces[word_index])
            word = words[word_index]
            row_anchors[row_index].append({
                "start": mapping[begin], "end": mapping[end - 1] + 1,
                "start_ms": word["start"] * 1000, "end_ms": word["end"] * 1000,
                "word_index": word_index, "evidence": phrase.evidence,
                "phrase_chars": phrase.end - phrase.start,
                "matched_chars": end - begin,
            })
    results = []
    for index, row in enumerate(source_rows):
        text = row["text"]
        anchors = _complete_original_tokens(text, row_anchors[index], positions[index])
        matched_chars = sum(a["matched_chars"] for a in anchors)
        issues = set()
        if any(p.start < row_ends[index] and p.end > row_starts[index] for p in rejected):
            issues.add("nonmonotonic_phrase_rejected")
        if not normalised[index]:
            issues.add("empty_caption")
        elif not anchors:
            issues.add("no_contextual_anchor")
        if matched_chars < len(normalised[index]):
            issues.add("unmatched_caption_text")
        aligned = AlignmentResult(text, media_sha256, "draft", tuple(
            WordAnchor(a["start"], a["end"], a["start_ms"], a["end_ms"], a["word_index"])
            for a in anchors), (), matched_chars, len(normalised[index]))
        parts = []
        for part in project_speakers(aligned, intervals, media_id=media_sha256):
            cosmetic = part.reason == "unaligned_text" and not _normalise(part.text)[0]
            reason = "unspoken_spacing_or_punctuation" if cosmetic else part.reason
            if part.status == "held" and not cosmetic:
                issues.add(reason)
            parts.append({"start": part.char_start, "end": part.char_end, "text": part.text,
                          "status": part.status, "speaker_id": part.speaker_id,
                          "start_ms": part.start_ms, "end_ms": part.end_ms,
                          "reason": reason, "validation_status": "not_verified"})
        status = "draft" if anchors and not issues else "held"
        results.append({**row, "status": status, "parts": parts, "anchors": anchors,
                        "issues": sorted(issues), "matched_chars": matched_chars})
    return finish(results, seed_phrases=len(chain), rejected_phrases=len(rejected),
                  refined_phrases=len(selected) - len(chain),
                  method="monotonic_unique_phrases_and_bounded_windows")
