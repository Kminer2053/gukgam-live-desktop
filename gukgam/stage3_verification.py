"""1차 보정의 변경·불명확 부분을 짧은 독립 전사와 대조한다.

수신 시각은 발화 시각이 아니다. 시각은 음성 후보를 찾는 데만 사용하고,
원문 양쪽의 고유 문구가 전사에 대응할 때만 별도 확인 초안을 만든다.
ASR 일치는 사람의 원음 검수가 아니므로 audio_verified는 항상 False다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from difflib import SequenceMatcher
import re
import time
import unicodedata
from typing import Any, Awaitable, Callable

MAX_CLIP_MS = 28_000
CONTEXT_MS = 10_000
OVERLAP_MS = 10_000
MAX_CLIPS_PER_TARGET = 4
MAX_CALLS = 12
TRANSCRIBE_TIMEOUT = 45
TOTAL_SECONDS = 180
ANCHOR_CHARS = 18
MIN_ANCHOR_CHARS = 6
MAX_REPLACEMENT_CHARS = 24

_NEGATION = re.compile(r"(?:않|못|없|아니|불가|반대)|(?:^|\s)안(?:\s|$)")
_BODY_UNCERTAINTIES = {"unfinished_sentence", "text_model_failed", "correction_rejected",
                       "text_model_unavailable", "text_model_timeout", "text_response_invalid",
                       "model_uncertain", "input_limit", "number_changed", "negation_changed",
                       "name_changed", "lexical_changed"}
_ROLE_UNCERTAINTIES = {"role_held", "role_uncertain", "speaker_held"}
_EDIT_UNCERTAINTIES = {"lexical_changed", "number_changed", "negation_changed", "name_changed"}
_CONTEXT_UNCERTAINTIES = {"model_uncertain", "unfinished_sentence"}
_ADAPTER_REASONS = {
    "invalid_range": "audio_result_invalid", "not_authorized": "permission_revoked",
    "cancelled": "permission_revoked", "audio_discontinuity": "audio_discontinuous",
    "audio_range_missing": "audio_outside_buffer", "stt_unavailable": "audio_model_unavailable",
    "queue_full": "audio_queue_full", "storage_quota": "audio_storage_quota",
    "storage_path": "audio_storage_failed", "storage_io": "audio_storage_failed",
    "prepared_capacity": "audio_prepared_capacity", "prepared_invalid": "audio_prepared_invalid",
    "prepared_missing": "audio_outside_buffer",
    "disk_full": "audio_disk_full", "stt_timeout": "audio_timeout",
    "stt_failed": "audio_transcription_failed", "empty_transcription": "audio_empty",
}
_REASONS = {
    "source_missing": "대응할 원자막 판본이 없습니다",
    "source_mismatch": "문장의 원문 좌표와 원자막 판본이 일치하지 않습니다",
    "edit_invalid": "본문 변경 좌표가 현재 원문·1차 보정과 일치하지 않습니다",
    "timing_missing": "음성 후보를 찾을 수신 시각이 없습니다",
    "audio_unavailable": "대조할 원음이 없습니다",
    "audio_discontinuous": "음성 구간이 끊겨 대조를 보류합니다",
    "audio_outside_buffer": "요청 음성 구간이 보존 범위에 없습니다",
    "audio_storage_quota": "음성 후보 보관 한도에 도달했습니다",
    "audio_disk_full": "음성 후보를 저장할 디스크 여유가 부족합니다",
    "audio_storage_failed": "음성 후보 저장에 실패했습니다",
    "audio_model_unavailable": "설치된 음성 전사 모델을 사용할 수 없습니다",
    "audio_queue_full": "음성 대조 대기 한도에 도달했습니다",
    "audio_prepared_capacity": "먼저 확보한 음성 후보의 대기 한도에 도달했습니다",
    "audio_prepared_invalid": "먼저 확보한 음성 후보의 무결성을 확인하지 못했습니다",
    "audio_timeout": "음성 전사 제한 시간을 초과했습니다",
    "audio_budget_exhausted": "문서의 음성 확인 총 제한 시간을 초과했습니다",
    "audio_transcription_failed": "음성 전사 호출을 완료하지 못했습니다",
    "audio_empty": "음성 전사에 대조할 문구가 없습니다",
    "audio_result_invalid": "음성 전사 결과의 구간·형식이 올바르지 않습니다",
    "audio_mismatch": "양쪽 문맥 사이의 전사가 보정 전후 문구와 다릅니다",
    "alignment_missing": "전사에서 양쪽 고유 문맥의 대응을 찾지 못했습니다",
    "alignment_ambiguous": "문맥이 반복되거나 서로 다른 전사가 있어 대응이 불명확합니다",
    "sensitive_change": "수치·부정·인명 변경은 원음으로 사람이 검수해야 합니다",
    "role_requires_review": "음성 전사만으로 발언자의 역할·실명을 확정할 수 없습니다",
    "body_context_requires_review": "전사 문구의 일치와 별개로 문맥 불명확·미완성 부분은 검토가 남았습니다",
    "permission_revoked": "음성 확인 권한 또는 설정이 해제돼 결과 반영을 중단했습니다",
    "clip_limit": "후보 음성 구간이 한도를 넘어 일부를 확인하지 못했습니다",
    "call_limit": "문서의 음성 전사 호출 한도에 도달했습니다",
    "asr_supports_edit": "고유 문맥 사이의 전사가 1차 교정 문구를 뒷받침합니다",
    "asr_supports_original": "고유 문맥 사이의 전사가 원문을 뒷받침해 원문으로 되돌린 별도 안입니다",
    "asr_alternative": "양쪽 고유 문맥 사이의 짧은 전사 차이를 별도 교정안으로 제시합니다",
    "asr_supports_sentence": "독립 전사에서 원문 문장의 고유 대응을 확인했습니다",
}


def _normalized(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def _normalized_positions(text: str) -> tuple[str, list[int]]:
    chars, positions = [], []
    for pos, char in enumerate(text):
        for value in unicodedata.normalize("NFKC", char).casefold():
            if value.isalnum():
                chars.append(value)
                positions.append(pos)
    return "".join(chars), positions


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _occurrences(text: str, phrase: str) -> int:
    """겹치는 반복도 두 번 이상으로 판정한다. 정확한 전체 횟수는 필요 없다."""
    first = text.find(phrase)
    if first < 0:
        return 0
    return 2 if text.find(phrase, first + 1) >= 0 else 1


def _sensitive(before: str, after: str, risks=()) -> bool:
    return (bool(set(risks or ()) & {"number_changed", "negation_changed", "name_changed",
                                   "number", "negation", "name", "sensitive"})
            or any(ch.isdigit() for ch in before + after)
            or bool(_NEGATION.search(before) or _NEGATION.search(after)))


def _held(check: dict, reason: str) -> dict:
    return {**check, "status": "held", "reason": reason,
            "detail": _REASONS.get(reason, "음성 확인을 보류합니다")}


def _source_context(sentence: dict, sources: list[dict]) -> tuple[str, int, int, list[dict]]:
    """정확히 인접한 원문 좌표만 연결한다. 임의 문자나 시간 보간은 하지 않는다."""
    spans = sentence.get("source_spans")
    original = sentence.get("original_text")
    if not isinstance(original, str) or not isinstance(spans, list) or not spans:
        raise ValueError("source_missing")
    lookup = {row.get("id"): (index, row) for index, row in enumerate(sources)}
    pieces, used = [], []
    previous_index = previous_end = None
    for span in spans:
        if not isinstance(span, dict) or span.get("source_id") not in lookup:
            raise ValueError("source_missing")
        index, source = lookup[span["source_id"]]
        text, start, end = source.get("text"), span.get("start"), span.get("end")
        if (not isinstance(text, str) or not _int(start) or not _int(end)
                or not 0 <= start < end <= len(text)):
            raise ValueError("source_mismatch")
        if previous_index is not None:
            same = index == previous_index and start == previous_end
            adjacent = (index == previous_index + 1 and start == 0
                        and previous_end == len(sources[previous_index]["text"]))
            if not same and not adjacent:
                raise ValueError("source_mismatch")
        pieces.append(text[start:end])
        if not used or used[-1] is not source:
            used.append(source)
        previous_index, previous_end = index, end
    if "".join(pieces) != original:
        raise ValueError("source_mismatch")
    first_index = lookup[spans[0]["source_id"]][0]
    last_index = lookup[spans[-1]["source_id"]][0]
    # 한 문장이 원조각 가장자리에 있어도 바로 인접한 원문에서 앵커를 얻는다.
    lo, hi = max(0, first_index - 1), min(len(sources), last_index + 2)
    context = "".join(str(row.get("text") or "") for row in sources[lo:hi])
    start = sum(len(str(row.get("text") or "")) for row in sources[lo:first_index]) + spans[0]["start"]
    return context, start, start + len(original), used


def _validated_edits(sentence: dict) -> list[dict]:
    original, current = sentence.get("original_text"), sentence.get("text")
    if not isinstance(original, str) or not isinstance(current, str):
        raise ValueError("edit_invalid")
    edits = sentence.get("edits") or []
    if not isinstance(edits, list):
        raise ValueError("edit_invalid")
    parts, cursor = [], 0
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("edit_invalid")
        start, end, before, after = (edit.get(key) for key in ("start", "end", "before", "after"))
        if (not _int(start) or not _int(end) or not cursor <= start <= end <= len(original)
                or not isinstance(before, str) or not isinstance(after, str)
                or before != original[start:end] or before == after):
            raise ValueError("edit_invalid")
        parts.extend((original[cursor:start], after))
        cursor = end
    parts.append(original[cursor:])
    if "".join(parts) != current:
        raise ValueError("edit_invalid")
    return edits


def _clip_ranges(sources: list[dict]) -> tuple[list[tuple[int, int]], bool]:
    intervals = []
    for source in sources:
        start, end = source.get("first_rx"), source.get("last_rx")
        if not _int(start) or not _int(end) or not 0 <= start <= end:
            raise ValueError("timing_missing")
        intervals.append((max(0, start - CONTEXT_MS), end + CONTEXT_MS))
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    ranges = []
    for start, end in merged:
        while start < end:
            finish = min(end, start + MAX_CLIP_MS)
            ranges.append((start, finish))
            if len(ranges) > MAX_CLIPS_PER_TARGET:
                return ranges[:MAX_CLIPS_PER_TARGET], True
            if finish == end:
                break
            start = finish - OVERLAP_MS
    return ranges[:MAX_CLIPS_PER_TARGET], len(ranges) > MAX_CLIPS_PER_TARGET


def _failure_reason(result: Any) -> str:
    if not isinstance(result, dict):
        return "audio_result_invalid"
    code = result.get("reason_code") or result.get("reason")
    if isinstance(code, str) and code in _ADAPTER_REASONS:
        return _ADAPTER_REASONS[code]
    if isinstance(code, str) and code in _REASONS:
        return code
    detail = str(result.get("detail") or "")
    for pattern, reason in (("끊|불연속", "audio_discontinuous"), ("보관 한도|quota", "audio_storage_quota"),
                            ("디스크|용량 부족", "audio_disk_full"), ("보관 경로|저장", "audio_storage_failed"),
                            ("모델|다운로드", "audio_model_unavailable"), ("대기 한도", "audio_queue_full"),
                            ("전 구간|보존 범위", "audio_outside_buffer"), ("버퍼|원음", "audio_unavailable"),
                            ("꺼져|중지|권한", "permission_revoked"), ("시간|timeout", "audio_timeout")):
        if re.search(pattern, detail, re.I):
            return reason
    return "audio_transcription_failed"


def _audio_text(result: dict, start: int, end: int) -> str:
    if (result.get("start_ms", start) != start or result.get("end_ms", end) != end
            or result.get("discontinuous") is True):
        raise ValueError("audio_discontinuous" if result.get("discontinuous") else "audio_result_invalid")
    text = result.get("transcript")
    if text is None:
        segments = result.get("segments")
        if not isinstance(segments, list) or any(not isinstance(row, dict) or not isinstance(row.get("text"), str)
                                                for row in segments):
            raise ValueError("audio_result_invalid")
        text = " ".join(row["text"] for row in segments)
    if not isinstance(text, str) or len(text) > 20_000:
        raise ValueError("audio_result_invalid")
    if not _normalized(text):
        raise ValueError("audio_empty")
    return text


def _locate(context: str, start: int, end: int, transcript: str) -> tuple[str, str]:
    """양 고유 앵커 사이의 짧은 원음 전사 후보만 반환한다."""
    before, after = _normalized(context[:start])[-ANCHOR_CHARS:], _normalized(context[end:])[:ANCHOR_CHARS]
    if min(len(before), len(after)) < MIN_ANCHOR_CHARS:
        return "alignment_missing", ""
    entire = _normalized(context)
    audio, positions = _normalized_positions(transcript)
    anchors = []
    for fragment, is_left in ((before, True), (after, False)):
        repeated = False
        for length in range(len(fragment), MIN_ANCHOR_CHARS - 1, -1):
            anchor = fragment[-length:] if is_left else fragment[:length]
            counts = _occurrences(entire, anchor), _occurrences(audio, anchor)
            repeated |= 2 in counts
            if counts == (1, 1):
                anchors.append(anchor)
                break
        else:
            return "alignment_ambiguous" if repeated else "alignment_missing", ""
    left, right = anchors
    lo = audio.index(left) + len(left)
    hi = audio.index(right)
    if hi < lo or hi - lo > MAX_REPLACEMENT_CHARS:
        return "alignment_ambiguous", ""
    if hi == lo:
        return "matched", ""
    # 음성에서 얻은 원래 띄어쓰기를 보존한다. 원문 경계 시각으로 위장하지 않는다.
    return "matched", transcript[positions[lo]:positions[hi - 1] + 1]


def _judge(check: dict, context: str, start: int, end: int, transcript: str) -> dict:
    reason, candidate = _locate(context, start, end, transcript)
    if reason != "matched":
        return _held(check, reason)
    before, after = check["before"], check["after"]
    if _sensitive(before, after, check.get("risks")) or _sensitive(candidate, candidate):
        return _held(check, "sensitive_change")
    if _normalized(candidate) == _normalized(after):
        status, reason, replacement = "supported", "asr_supports_edit", after
    elif _normalized(candidate) == _normalized(before):
        status, reason, replacement = "reverted", "asr_supports_original", before
    elif (candidate and max(len(_normalized(before)), len(_normalized(after)), len(_normalized(candidate))) <= 12
          and SequenceMatcher(None, _normalized(before), _normalized(candidate), autojunk=False).ratio() >= .45):
        status, reason, replacement = "corrected", "asr_alternative", candidate
    else:
        return _held(check, "audio_mismatch")
    return {**check, "status": status, "reason": reason, "detail": _REASONS[reason],
            "final": replacement, "evidence": "unique_two_sided_context"}


def _body_judge(check: dict, context: str, start: int, end: int, transcript: str) -> dict:
    original = check["before"]
    text, audio = _normalized(original), _normalized(transcript)
    proposed = _normalized(check["after"])
    if (len(proposed) >= 12 and _occurrences(audio, proposed) == 1
            and _occurrences(_normalized(context), text) == 1):
        return {**check, "status": "supported", "reason": "asr_supports_sentence",
                "detail": _REASONS["asr_supports_sentence"], "final": check["after"],
                "evidence": "unique_complete_sentence"}
    if len(text) >= 12 and _occurrences(audio, text) == 1 and _occurrences(_normalized(context), text) == 1:
        return {**check, "status": "supported", "reason": "asr_supports_sentence",
                "detail": _REASONS["asr_supports_sentence"], "final": original,
                "evidence": "unique_complete_sentence"}
    if check.get("has_edits"):
        return _held(check, "alignment_missing")
    reason, candidate = _locate(context, start, end, transcript)
    if reason != "matched":
        return _held(check, reason)
    # 불명확 문장 전체를 재작성하지 않고, 하나의 짧은 차이만 허용한다.
    changes = [item for item in SequenceMatcher(None, original, candidate, autojunk=False).get_opcodes()
               if item[0] != "equal"]
    if len(changes) != 1:
        return _held(check, "audio_mismatch")
    _, a, b, c, d = changes[0]
    if max(b - a, d - c) > 12 or SequenceMatcher(None, original, candidate, autojunk=False).ratio() < .72:
        return _held(check, "audio_mismatch")
    if _sensitive(original[a:b], candidate[c:d], check.get("risks")):
        return _held(check, "sensitive_change")
    return {**check, "status": "corrected", "reason": "asr_alternative", "detail": _REASONS["asr_alternative"],
            "final": candidate, "evidence": "unique_two_sided_context"}


def _edit_groups(targets: list[tuple[int | None, dict]], original: str) -> list[list[tuple[int | None, dict]]]:
    """이웃 교정 단어가 앵커를 가리지 않게 짧은 인접 변경을 한 범위로 대조한다."""
    groups = []
    for target in targets:
        index, edit = target
        if groups and index is not None and groups[-1][0][0] is not None:
            previous = groups[-1][-1][1]
            start = groups[-1][0][1]["start"]
            close = len(_normalized(original[previous["end"]:edit["start"]])) < ANCHOR_CHARS
            bounded = len(_normalized(original[start:edit["end"]])) <= MAX_REPLACEMENT_CHARS
            if close and bounded and len(groups[-1]) < 6:
                groups[-1].append(target)
                continue
        groups.append([target])
    return groups


def _judge_group(bases: list[dict], context: str, sentence_start: int, original: str,
                 transcript: str) -> list[dict]:
    if len(bases) == 1:
        base = bases[0]
        judge = _body_judge if base["edit_index"] is None else _judge
        return [judge(base, context, sentence_start + base["start"], sentence_start + base["end"], transcript)]
    start, end = bases[0]["start"], bases[-1]["end"]
    reason, candidate = _locate(context, sentence_start + start, sentence_start + end, transcript)
    if reason != "matched":
        return [_held(base, reason) for base in bases]
    if _sensitive(candidate, candidate):
        return [_held(base, "sensitive_change") for base in bases]
    # 수정 전/후를 조합하되 최대 6개(64개 조합)로 한정한다. 정확히 하나의
    # 조합만 전사와 일치해야 각 변경을 독립적으로 유지하거나 되돌린다.
    matches = []
    for mask in range(1 << len(bases)):
        pieces, cursor = [], start
        for index, base in enumerate(bases):
            pieces.extend((original[cursor:base["start"]], base["after"] if mask & (1 << index) else base["before"]))
            cursor = base["end"]
        pieces.append(original[cursor:end])
        if _normalized("".join(pieces)) == _normalized(candidate):
            matches.append(mask)
    if len(matches) != 1:
        return [_held(base, "alignment_ambiguous" if matches else "audio_mismatch") for base in bases]
    results = []
    for index, base in enumerate(bases):
        kept = bool(matches[0] & (1 << index))
        reason = "asr_supports_edit" if kept else "asr_supports_original"
        results.append({**base, "status": "supported" if kept else "reverted", "reason": reason,
                        "detail": _REASONS[reason], "final": base["after"] if kept else base["before"],
                        "evidence": "unique_two_sided_edit_group", "group_edit_indices": [row["edit_index"] for row in bases]})
    return results


def plan_document_clips(document: dict, sources: list[dict]) -> list[tuple[int, int]]:
    """모델 대기 전에 확보할 짧은 원음 후보만 계획한다. 입력·수신 상태는 바꾸지 않는다.

    수신 시각은 발화 시각이 아니므로 실제 대응은 이후 고유 문맥 대조에서 확인한다.
    역할만 불명확하거나 민감 변경만 있는 문장은 전사 호출 대상에서 제외한다.
    """
    intervals: list[tuple[int, int]] = []
    for sentence in document.get("sentences") or []:
        if not isinstance(sentence, dict):
            continue
        try:
            edits = _validated_edits(sentence)
            uncertainties = set(sentence.get("uncertainties") or [])
            targets = [(index, edit) for index, edit in enumerate(edits)
                       if _normalized(edit["before"]) != _normalized(edit["after"])]
            body_uncertain = bool(uncertainties & _BODY_UNCERTAINTIES)
            if not targets and not body_uncertain:
                continue
            if not targets or uncertainties & (_BODY_UNCERTAINTIES - _EDIT_UNCERTAINTIES):
                targets.append((None, {"start": 0, "end": len(sentence["original_text"]),
                                       "before": sentence["original_text"], "after": sentence["text"],
                                       "risk": list(uncertainties)}))
            groups = _edit_groups(targets, sentence["original_text"])
            if not any(not any(index is not None and _sensitive(edit["before"], edit["after"], edit.get("risk"))
                               for index, edit in group) for group in groups):
                continue
            _context, _start, _end, used = _source_context(sentence, sources)
            ranges, _truncated = _clip_ranges(used)
        except (ValueError, TypeError, KeyError):
            continue
        for interval in ranges:
            if interval not in intervals:
                intervals.append(interval)
                if len(intervals) == MAX_CALLS:
                    return intervals
    return intervals


async def verify_document(document: dict, sources: list[dict],
                          transcribe: Callable[[int, int, str], Awaitable[dict]], *,
                          allowed: Callable[[], bool] = lambda: True) -> dict:
    """저장·모델 연결 없이 주입된 짧은 전사만 호출한다. 취소는 호출자에게 전달한다."""
    started = time.monotonic()
    frozen, sources = deepcopy(document), deepcopy(sources)
    sentences = deepcopy(frozen.get("sentences") or [])
    checks, cache = [], {}
    metrics = {"transcribe_calls": 0, "cache_hits": 0, "clip_ms": 0, "targets": 0}
    revoked = False

    def permitted() -> bool:
        try:
            return allowed() is True
        except Exception:
            return False

    async def get_audio(interval: tuple[int, int]) -> tuple[str, str]:
        nonlocal revoked
        if not permitted():
            revoked = True
            return "permission_revoked", ""
        if interval in cache:
            metrics["cache_hits"] += 1
            return cache[interval]
        remaining = TOTAL_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            return "audio_budget_exhausted", ""
        if metrics["transcribe_calls"] >= MAX_CALLS:
            return "call_limit", ""
        metrics["transcribe_calls"] += 1
        metrics["clip_ms"] += interval[1] - interval[0]
        try:
            result = await asyncio.wait_for(transcribe(*interval, "stage3_changed_or_uncertain_sentence"),
                                            timeout=min(TRANSCRIBE_TIMEOUT, remaining))
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            answer = "audio_timeout", ""
        except Exception:
            answer = "audio_transcription_failed", ""
        else:
            if not permitted():
                revoked = True
                return "permission_revoked", ""
            if not isinstance(result, dict) or result.get("status") != "candidate":
                answer = _failure_reason(result), ""
            else:
                try:
                    answer = "candidate", _audio_text(result, *interval)
                except ValueError as error:
                    answer = str(error), ""
        if not permitted():
            revoked = True
            return "permission_revoked", ""
        cache[interval] = answer
        return answer

    for sentence in sentences:
        identity = {"sentence_id": sentence.get("id"), "edit_index": None,
                    "before": sentence.get("original_text", ""), "after": sentence.get("text", "")}
        uncertainties = sentence.get("uncertainties") or []
        if sentence.get("role_status") == "held" or set(uncertainties) & _ROLE_UNCERTAINTIES:
            checks.append(_held({**identity, "kind": "role"}, "role_requires_review"))
        if set(uncertainties) & _CONTEXT_UNCERTAINTIES:
            checks.append(_held({**identity, "kind": "context",
                                "unresolved": sorted(set(uncertainties) & _CONTEXT_UNCERTAINTIES)},
                               "body_context_requires_review"))
        try:
            edits = _validated_edits(sentence)
        except ValueError as error:
            checks.append(_held(identity, str(error)))
            continue
        targets = [(index, edit) for index, edit in enumerate(edits)
                   if _normalized(edit["before"]) != _normalized(edit["after"])]
        body_uncertain = bool(set(uncertainties) & _BODY_UNCERTAINTIES)
        if not targets and not body_uncertain:
            continue
        try:
            context, sentence_start, sentence_end, used = _source_context(sentence, sources)
            ranges, truncated = _clip_ranges(used)
        except ValueError as error:
            checks.append(_held(identity, str(error)))
            continue
        if not targets or set(uncertainties) & (_BODY_UNCERTAINTIES - _EDIT_UNCERTAINTIES):
            targets.append((None, {"start": 0, "end": len(sentence["original_text"]),
                                   "before": sentence["original_text"], "after": sentence["text"],
                                   "risk": uncertainties, "has_edits": bool(edits)}))
        replacements = {}
        whole_replacement = None
        for group in _edit_groups(targets, sentence["original_text"]):
            metrics["targets"] += len(group)
            bases = [{"sentence_id": sentence.get("id"), "edit_index": index,
                      "kind": "body" if index is None else "edit", "before": edit["before"],
                      "after": edit["after"], "risks": edit.get("risk") or [],
                      "start": edit["start"], "end": edit["end"],
                      "has_edits": edit.get("has_edits", False)} for index, edit in group]
            if any(_sensitive(base["before"], base["after"], base["risks"]) and base["edit_index"] is not None
                   for base in bases):
                checks.extend(_held(base, "sensitive_change") for base in bases)
                continue
            grouped_outcomes, failures = [[] for _ in bases], []
            for interval in ranges:
                reason, audio = await get_audio(interval)
                if reason != "candidate":
                    failures.append(reason)
                    if reason in {"permission_revoked", "call_limit", "audio_budget_exhausted"}:
                        break
                    continue
                for rows, outcome in zip(grouped_outcomes, _judge_group(
                        bases, context, sentence_start, sentence["original_text"], audio)):
                    outcome["clip"] = {"start_ms": interval[0], "end_ms": interval[1]}
                    rows.append(outcome)
            for base, outcomes in zip(bases, grouped_outcomes):
                supported = [row for row in outcomes if row["status"] != "held"]
                finals = {_normalized(row.get("final", "")) for row in supported}
                if revoked:
                    check = _held(base, "permission_revoked")
                elif len(finals) > 1 or any(row["reason"] == "alignment_ambiguous" for row in outcomes):
                    check = _held(base, "alignment_ambiguous")
                elif supported and any(row["reason"] in {"audio_mismatch", "sensitive_change"} for row in outcomes):
                    check = _held(base, "alignment_ambiguous")
                elif supported:
                    check = supported[0]
                elif outcomes:
                    check = next((row for row in outcomes if row["reason"] != "alignment_missing"), outcomes[0])
                    if truncated and check["reason"] == "alignment_missing":
                        check = _held(base, "clip_limit")
                else:
                    check = _held(base, failures[0] if failures else "alignment_missing")
                checks.append(check)
                if check["status"] in {"reverted", "corrected"}:
                    if base["edit_index"] is None:
                        whole_replacement = check["final"]
                    else:
                        replacements[base["edit_index"]] = check["final"]
        if whole_replacement is not None:
            sentence["text"] = whole_replacement
        elif replacements:
            original, parts, cursor = sentence["original_text"], [], 0
            for index, edit in enumerate(edits):
                parts.extend((original[cursor:edit["start"]], replacements.get(index, edit["after"])))
                cursor = edit["end"]
            parts.append(original[cursor:])
            sentence["text"] = "".join(parts)

    # 검토 도중 권한이 바뀌면 이미 얻은 부분 결과도 표시용 최종본으로 쓰지 않는다.
    if checks and (revoked or not permitted()):
        sentences = deepcopy(frozen.get("sentences") or [])
        checks = [_held(check, "permission_revoked") for check in checks]
    for sentence in sentences:
        own = [row for row in checks if row["sentence_id"] == sentence.get("id")]
        sentence["verification"] = {"state": _state(own), "audio_verified": False,
                                    "checks": [checks.index(row) for row in own]}
    paragraphs = deepcopy(frozen.get("paragraphs") or [])
    lookup = {row.get("id"): row for row in sentences}
    for paragraph in paragraphs:
        ids = paragraph.get("sentence_ids")
        if isinstance(ids, list) and all(identity in lookup for identity in ids):
            paragraph["text"] = " ".join(lookup[identity]["text"].strip() for identity in ids).strip()
    metrics["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return {"state": _state(checks), "checks": checks, "sentences": sentences, "paragraphs": paragraphs,
            "metrics": metrics, "audio_verified": False, "timing_uncertain": True,
            "preview_only": True, "source_clock": "caption_reception_not_speech_time"}


def _state(checks: list[dict]) -> str:
    if not checks:
        return "not_needed"
    completed = sum(row["status"] in {"supported", "reverted", "corrected"} for row in checks)
    return "complete" if completed == len(checks) else "partial" if completed else "held"
