"""운영과 분리한 자막 문자 범위·음성 단어 시각의 보수적 대응 도구.

수신 시각을 입력받지 않으며, 음성 파일의 같은 시간축에서 얻은 단어 시각만
쓴다. 결과는 실제 청취 검증 전 초안이다. 원문을 고치거나 글자별로 시간을
등분하지 않고, 고유한 완전 일치가 없는 범위는 보류한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from difflib import Match, SequenceMatcher
from typing import Literal, Sequence


DraftStatus = Literal["draft", "held"]
_MAX_CHARS = 20_000
_MAX_WORDS = 10_000
_MAX_COMPARISON_PRODUCT = 4_000_000
_UNKNOWN_SPEAKERS = frozenset({"", "unknown", "미상", "unassigned", "<unknown>"})


@dataclass(frozen=True)
class ASRWord:
    """실제 ASR이 반환한 단어와 음성 파일 기준 밀리초 구간."""

    text: str
    start_ms: float
    end_ms: float


@dataclass(frozen=True)
class SpeakerInterval:
    """같은 음성 파일에서 얻은 익명 화자의 구간. 실명·역할이 아니다."""

    start_ms: float
    end_ms: float
    speaker_id: str


@dataclass(frozen=True)
class WordAnchor:
    """원문 [char_start, char_end)와 실제 ASR 단어 구간의 대응 후보."""

    char_start: int
    char_end: int
    start_ms: float
    end_ms: float
    word_index: int


@dataclass(frozen=True)
class AlignmentResult:
    """held에도 일치 후보를 남기지만 검증된 화자 근거로 채택하지 않는다."""

    text: str
    media_id: str
    status: DraftStatus
    anchors: tuple[WordAnchor, ...]
    issues: tuple[str, ...]
    matched_chars: int
    total_chars: int
    validation_status: Literal["not_verified"] = "not_verified"


@dataclass(frozen=True)
class CaptionSpeakerSpan:
    """원문을 빠짐없이 보존하는 파생 범위. 시각은 단어 구간만 쓴다."""

    char_start: int
    char_end: int
    text: str
    status: DraftStatus
    speaker_id: str | None
    start_ms: float | None
    end_ms: float | None
    reason: str
    validation_status: Literal["not_verified"] = "not_verified"


def _compact(text: str) -> tuple[str, tuple[int, ...]]:
    """공백만 제외하며 각 문자의 원문 위치를 유지한다."""
    positions = tuple(i for i, char in enumerate(text) if not char.isspace())
    return "".join(text[i] for i in positions), positions


def _valid_time(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _unique(haystack: str, needle: str) -> bool:
    first = haystack.find(needle)
    return first >= 0 and haystack.find(needle, first + 1) < 0


def align_caption(
    text: str,
    words: Sequence[ASRWord],
    *,
    media_id: str,
    max_unmatched_ratio: float = 0.2,
    min_anchor_chars: int = 4,
    max_unmatched_run: int = 24,
) -> AlignmentResult:
    """고유 연속 일치 안에 완전히 들어간 ASR 단어만 대응 후보로 반환한다.

    띄어쓰기 차이만 허용한다. 반복된 단어는 문맥상 순서를 추측하지 않고
    보류한다. 많은 누락·시간 역행·큰 불일치는 정렬 전체를 held로 반환한다.
    ASR이 원문보다 긴 경우에도 원문 바깥 단어를 억지로 끌어오지 않는다.
    """
    if not isinstance(text, str) or not isinstance(media_id, str) or not media_id.strip():
        raise ValueError("원문 문자열과 음성 파일 식별자가 필요합니다")
    if (not isinstance(max_unmatched_ratio, (int, float))
            or isinstance(max_unmatched_ratio, bool)
            or not 0 <= max_unmatched_ratio < 1):
        raise ValueError("미일치 비율 한도는 0 이상 1 미만이어야 합니다")
    if (not isinstance(min_anchor_chars, int) or isinstance(min_anchor_chars, bool)
            or min_anchor_chars < 1 or not isinstance(max_unmatched_run, int)
            or isinstance(max_unmatched_run, bool) or max_unmatched_run < 1):
        raise ValueError("일치 길이와 연속 누락 한도는 양의 정수여야 합니다")
    compact, positions = _compact(text)

    def held(issue: str) -> AlignmentResult:
        return AlignmentResult(text, media_id, "held", (), (issue,), 0, len(compact))

    if len(compact) > _MAX_CHARS or len(words) > _MAX_WORDS:
        return held("input_too_large")
    if not compact:
        return held("empty_caption")
    if not words:
        return held("missing_asr_words")
    pieces: list[str] = []
    ranges: list[tuple[int, int]] = []
    previous_start = previous_end = -1.0
    size = 0
    for word in words:
        if not isinstance(word, ASRWord) or not isinstance(word.text, str):
            return held("invalid_asr_word")
        if (not _valid_time(word.start_ms) or not _valid_time(word.end_ms)
                or word.start_ms >= word.end_ms):
            return held("invalid_asr_time")
        if word.start_ms < previous_start or word.end_ms < previous_end:
            return held("nonmonotonic_asr_time")
        previous_start, previous_end = word.start_ms, word.end_ms
        piece, _ = _compact(word.text)
        if not piece:
            return held("empty_asr_word")
        ranges.append((size, size + len(piece)))
        size += len(piece)
        if size > _MAX_CHARS:
            return held("input_too_large")
        pieces.append(piece)
    transcript = "".join(pieces)
    # 긴 반복 문자열에 대한 이차 비교로 공유 머신을 점유하지 않는다.
    exact_start = transcript.find(compact)
    if exact_start >= 0 and _unique(transcript, compact):
        blocks = [Match(0, exact_start, len(compact))]
    elif len(compact) * len(transcript) > _MAX_COMPARISON_PRODUCT:
        return held("alignment_window_too_large")
    else:
        blocks = SequenceMatcher(None, compact, transcript, autojunk=False).get_matching_blocks()
    anchors: list[WordAnchor] = []
    matched: set[int] = set()
    issues: set[str] = set()
    for block in blocks:
        if block.size < min_anchor_chars:
            continue
        phrase = compact[block.a:block.a + block.size]
        if not _unique(compact, phrase) or not _unique(transcript, phrase):
            issues.add("ambiguous_repeated_phrase")
            continue
        for index, (begin, end) in enumerate(ranges):
            if begin < block.b or end > block.b + block.size:
                continue
            piece = pieces[index]
            if not _unique(compact, piece) or not _unique(transcript, piece):
                issues.add("ambiguous_repeated_word")
                continue
            start = block.a + begin - block.b
            stop = start + len(piece)
            word = words[index]
            anchors.append(WordAnchor(positions[start], positions[stop - 1] + 1,
                                      word.start_ms, word.end_ms, index))
            matched.update(range(start, stop))
    anchors.sort(key=lambda anchor: anchor.char_start)
    status: DraftStatus = "draft"
    if not anchors:
        issues.add("no_unique_complete_word")
        status = "held"
    if 1 - len(matched) / len(compact) > max_unmatched_ratio:
        issues.add("excessive_caption_mismatch")
        status = "held"
    longest_gap = current_gap = 0
    for i in range(len(compact)):
        current_gap = 0 if i in matched else current_gap + 1
        longest_gap = max(longest_gap, current_gap)
    if longest_gap > max_unmatched_run:
        issues.add("long_unmatched_caption_run")
        status = "held"
    if len(matched) < len(compact):
        issues.add("unmatched_caption_text")
    return AlignmentResult(text, media_id, status, tuple(anchors), tuple(sorted(issues)),
                           len(matched), len(compact))


def _speaker_for_word(
    anchor: WordAnchor, intervals: Sequence[SpeakerInterval],
) -> tuple[str | None, str]:
    clipped = [(max(anchor.start_ms, part.start_ms), min(anchor.end_ms, part.end_ms), part.speaker_id.strip())
               for part in intervals if part.start_ms < anchor.end_ms and part.end_ms > anchor.start_ms]
    if not clipped:
        return None, "missing_speaker_interval"
    if any(speaker.casefold() in _UNKNOWN_SPEAKERS for _, _, speaker in clipped):
        return None, "unknown_speaker_interval"
    if len({part[2] for part in clipped}) > 1:
        for i, (start, end, speaker) in enumerate(clipped):
            if any(other != speaker and max(start, other_start) < min(end, other_end)
                   for other_start, other_end, other in clipped[i + 1:]):
                return None, "overlapping_speakers"
        return None, "speaker_change_inside_word"
    covered_to = anchor.start_ms
    for start, end, _ in sorted(clipped):
        if start > covered_to:
            return None, "incomplete_speaker_coverage"
        covered_to = max(covered_to, end)
    if covered_to < anchor.end_ms:
        return None, "incomplete_speaker_coverage"
    return clipped[0][2], "exact_word_and_single_speaker"


def project_speakers(
    alignment: AlignmentResult,
    intervals: Sequence[SpeakerInterval],
    *,
    media_id: str,
) -> tuple[CaptionSpeakerSpan, ...]:
    """원문 전체를 연속 범위로 보존하며 단어 안의 교대·겹침은 보류한다.

    실제 ASR 단어의 경계에서만 음성 화자를 붙인다. 중간 화자 교대 시각을
    글자 위치로 균등 배분하지 않는다. 공백을 포함한 미정렬 범위도 남긴다.
    """
    text = alignment.text

    def unknown(start: int, end: int, reason: str) -> CaptionSpeakerSpan:
        return CaptionSpeakerSpan(start, end, text[start:end], "held", None, None, None, reason)

    if not text:
        return ()
    if media_id != alignment.media_id:
        return (unknown(0, len(text), "media_id_mismatch"),)
    if alignment.status != "draft":
        return (unknown(0, len(text), "alignment_held"),)
    if any(not isinstance(part, SpeakerInterval) or not _valid_time(part.start_ms)
           or not _valid_time(part.end_ms) or part.start_ms >= part.end_ms
           or not isinstance(part.speaker_id, str) or not part.speaker_id.strip()
           for part in intervals):
        return (unknown(0, len(text), "invalid_speaker_interval"),)
    cursor = 0
    previous_start = previous_end = -1.0
    previous_word_index = -1
    for anchor in alignment.anchors:
        if (not isinstance(anchor, WordAnchor) or not isinstance(anchor.char_start, int)
                or isinstance(anchor.char_start, bool) or not isinstance(anchor.char_end, int)
                or isinstance(anchor.char_end, bool) or anchor.char_start < cursor
                or anchor.char_end <= anchor.char_start or anchor.char_end > len(text)
                or not _valid_time(anchor.start_ms) or not _valid_time(anchor.end_ms)
                or anchor.start_ms >= anchor.end_ms or anchor.start_ms < previous_start
                or anchor.end_ms < previous_end or not isinstance(anchor.word_index, int)
                or isinstance(anchor.word_index, bool) or anchor.word_index < 0
                or anchor.word_index <= previous_word_index):
            return (unknown(0, len(text), "invalid_anchor_order"),)
        cursor = anchor.char_end
        previous_start, previous_end = anchor.start_ms, anchor.end_ms
        previous_word_index = anchor.word_index
    spans: list[CaptionSpeakerSpan] = []
    cursor = 0
    for anchor in alignment.anchors:
        if cursor < anchor.char_start:
            spans.append(unknown(cursor, anchor.char_start, "unaligned_text"))
        speaker, reason = _speaker_for_word(anchor, intervals)
        spans.append(CaptionSpeakerSpan(anchor.char_start, anchor.char_end,
                                       text[anchor.char_start:anchor.char_end],
                                       "draft" if speaker is not None else "held", speaker,
                                       anchor.start_ms, anchor.end_ms, reason))
        cursor = anchor.char_end
    if cursor < len(text):
        spans.append(unknown(cursor, len(text), "unaligned_text"))
    return tuple(spans)
