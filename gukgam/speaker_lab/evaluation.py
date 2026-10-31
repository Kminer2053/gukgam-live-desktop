"""익명 음성 화자 구간의 오프라인 평가. 단위시험 통과는 실제 품질 증명이 아니다.

구간은 반열림 [start, end), 시간 단위는 초다. 겹친 서로 다른 화자는 각각
집계하고 같은 화자의 중복 구간은 합친다. 무발화 시간도 평가 범위에 남긴다.
이 모듈은 실명·문답 역할·질의 소유권을 평가하거나 운영 적용을 승인하지 않는다.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import math


_UNKNOWN = frozenset({"", "unknown", "미상", "unassigned", "<unknown>"})
_MAX_SPEAKERS = 8


@dataclass(frozen=True)
class _Span:
    start: float
    end: float
    speaker: str | None


@dataclass(frozen=True)
class _Slice:
    start: float
    end: float
    reference: frozenset[str | None]
    prediction: frozenset[str | None]


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}은 유한한 숫자여야 합니다.")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name}은 유한한 숫자여야 합니다.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name}은 유한한 숫자여야 합니다.")
    return result


def _spans(
    rows: Sequence[Mapping[str, object]], duration: float, *, reference: bool,
) -> list[_Span]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise ValueError("화자 구간은 목록이어야 합니다.")
    result = []
    speakers = set()
    for row in rows:
        if not isinstance(row, Mapping) or not {"start", "end", "speaker"} <= row.keys():
            raise ValueError("각 구간에는 start, end, speaker가 필요합니다.")
        start = _number(row["start"], "start")
        end = _number(row["end"], "end")
        if not 0 <= start < end <= duration:
            raise ValueError("구간은 평가 범위 안에 있고 start < end여야 합니다.")
        speaker = row["speaker"]
        if speaker is not None and not isinstance(speaker, str):
            raise ValueError("speaker는 익명 문자열 ID 또는 미상이어야 합니다.")
        if isinstance(speaker, str):
            speaker = speaker.strip()
            if speaker.casefold() in _UNKNOWN:
                speaker = None
        if reference and speaker is None:
            raise ValueError("정답 미상 구간을 원음 정답으로 평가할 수 없습니다.")
        if speaker is not None:
            speakers.add(speaker)
        result.append(_Span(start, end, speaker))
    if len(speakers) > _MAX_SPEAKERS:
        raise ValueError("한 평가 범위의 익명 화자는 최대 8명입니다.")
    return result


def _slices(reference: list[_Span], prediction: list[_Span], duration: float) -> list[_Slice]:
    events: dict[float, list[tuple[int, str | None, int]]] = defaultdict(list)
    for side, spans in enumerate((reference, prediction)):
        for span in spans:
            events[span.start].append((side, span.speaker, 1))
            events[span.end].append((side, span.speaker, -1))
    points = sorted({0.0, duration, *events})
    active: tuple[Counter, Counter] = (Counter(), Counter())
    result = []
    for start, end in zip(points, points[1:]):
        for side, speaker, delta in events[start]:
            active[side][speaker] += delta
            if active[side][speaker] == 0:
                del active[side][speaker]
        result.append(_Slice(start, end, frozenset(active[0]), frozenset(active[1])))
    return result


def _mapping(slices: list[_Slice]) -> dict[str, str]:
    references = sorted({speaker for part in slices for speaker in part.reference if speaker is not None})
    predictions = sorted({speaker for part in slices for speaker in part.prediction if speaker is not None})
    weights: dict[tuple[str, str], float] = defaultdict(float)
    for part in slices:
        for predicted in part.prediction:
            if predicted is not None:
                for expected in part.reference:
                    weights[predicted, expected] += part.end - part.start

    @lru_cache(maxsize=None)
    def choose(index: int, used: int) -> tuple[float, tuple[tuple[str, str], ...]]:
        if index == len(predictions):
            return 0.0, ()
        best_score, best_pairs = choose(index + 1, used)
        predicted = predictions[index]
        for ref_index, expected in enumerate(references):
            weight = weights[predicted, expected]
            if used & (1 << ref_index) or weight <= 0:
                continue
            rest_score, rest_pairs = choose(index + 1, used | (1 << ref_index))
            score = rest_score + weight
            pairs = ((predicted, expected),) + rest_pairs
            if score > best_score or (score == best_score and pairs < best_pairs):
                best_score, best_pairs = score, pairs
        return best_score, best_pairs

    return dict(choose(0, 0)[1])


def _boundaries(slices: list[_Slice], *, reference: bool) -> list[tuple[float, bool]]:
    """무음 자체는 교대가 아니다. 무음 뒤 다른 화자가 시작하면 그 시작을 센다."""
    previous: frozenset[str | None] = frozenset()
    result = []
    for part in slices:
        active = part.reference if reference else part.prediction
        if not active:
            continue
        if previous and active != previous:
            # 미상 경계는 분모에는 남기되 확인된 경계의 정답으로 세지 않는다.
            result.append((part.start, None not in previous and None not in active))
        previous = active
    return result


def _boundary_metrics(slices: list[_Slice], tolerance: float) -> dict:
    expected = [at for at, _ in _boundaries(slices, reference=True)]
    all_predicted = _boundaries(slices, reference=False)
    predicted = [at for at, known in all_predicted if known]
    ref_index = pred_index = matched = 0
    offsets = []
    while ref_index < len(expected) and pred_index < len(predicted):
        offset = predicted[pred_index] - expected[ref_index]
        if abs(offset) <= tolerance:
            matched += 1
            offsets.append(abs(offset))
            ref_index += 1
            pred_index += 1
        elif offset < -tolerance:
            pred_index += 1
        else:
            ref_index += 1
    return {
        "tolerance_seconds": tolerance,
        "reference_count": len(expected),
        "prediction_count": len(all_predicted),
        "unknown_prediction_count": len(all_predicted) - len(predicted),
        "matched_count": matched,
        "false_positive_count": len(all_predicted) - matched,
        "false_negative_count": len(expected) - matched,
        "precision": matched / len(all_predicted) if all_predicted else None,
        "recall": matched / len(expected) if expected else None,
        "mean_matched_offset_seconds": sum(offsets) / matched if matched else None,
        "reference_times": expected,
        "prediction_times": [at for at, _ in all_predicted],
    }


def evaluate_diarization(
    reference: Sequence[Mapping[str, object]] | None,
    prediction: Sequence[Mapping[str, object]],
    *,
    duration_seconds: float,
    boundary_tolerance_seconds: float = 1.0,
    reference_verified: bool = False,
) -> dict:
    """익명 화자 DER·경계를 계산하되 품질 통과나 운영 적용을 선언하지 않는다.

    reference=None은 정답 미제공이고 []는 범위 전체의 무발화 정답이다.
    reference_verified는 호출자가 원음 확인 여부를 진술한 값일 뿐, 이 함수가
    음성을 듣거나 실제 모델을 평가했다는 증거가 아니다. DER에는 collar를 두지
    않는다. 경계만 ±tolerance로 일대일 대조하며 겹침 진입/이탈도 각각 센다.
    """
    duration = _number(duration_seconds, "duration_seconds")
    tolerance = _number(boundary_tolerance_seconds, "boundary_tolerance_seconds")
    if duration <= 0 or tolerance < 0:
        raise ValueError("평가 길이는 양수, 경계 허용 오차는 0 이상이어야 합니다.")
    if not isinstance(reference_verified, bool):
        raise ValueError("reference_verified는 bool이어야 합니다.")
    predicted = _spans(prediction, duration, reference=False)
    result = {
        "evaluated": reference is not None,
        "duration_seconds": duration,
        "reference_verified": reference_verified,
        "scope": "anonymous_acoustic_speakers_only",
        "excluded_dimensions": ["speaker_name", "dialogue_role", "question_owner"],
        "release_ready": False,
        "release_note": "원음 정답·독립 실전 평가집의 충분성·역할/질의자·지연/부하는 별도 검증이 필요합니다.",
        "metrics": None,
    }
    if reference is None:
        result["reason"] = "reference_missing"
        return result
    expected = _spans(reference, duration, reference=True)
    slices = _slices(expected, predicted, duration)
    mapping = _mapping(slices)
    totals = dict.fromkeys((
        "reference_speaker_seconds", "prediction_speaker_seconds", "reference_speech_seconds",
        "reference_silence_seconds", "reference_overlap_seconds", "prediction_overlap_seconds",
        "correct_speaker_seconds", "missed_speaker_seconds", "false_alarm_speaker_seconds",
        "confused_speaker_seconds", "unknown_prediction_seconds", "known_prediction_seconds",
        "known_reference_covered_seconds",
    ), 0.0)
    for part in slices:
        seconds = part.end - part.start
        ref_count, pred_count = len(part.reference), len(part.prediction)
        known_count = len(part.prediction - {None})
        mapped = {mapping[speaker] for speaker in part.prediction if speaker in mapping}
        correct = len(part.reference & mapped)
        totals["reference_speaker_seconds"] += ref_count * seconds
        totals["prediction_speaker_seconds"] += pred_count * seconds
        totals["reference_speech_seconds"] += bool(ref_count) * seconds
        totals["reference_silence_seconds"] += (not ref_count) * seconds
        totals["reference_overlap_seconds"] += (ref_count > 1) * seconds
        totals["prediction_overlap_seconds"] += (pred_count > 1) * seconds
        totals["correct_speaker_seconds"] += correct * seconds
        totals["missed_speaker_seconds"] += max(ref_count - pred_count, 0) * seconds
        totals["false_alarm_speaker_seconds"] += max(pred_count - ref_count, 0) * seconds
        totals["confused_speaker_seconds"] += (min(ref_count, pred_count) - correct) * seconds
        totals["unknown_prediction_seconds"] += (None in part.prediction) * seconds
        totals["known_prediction_seconds"] += known_count * seconds
        totals["known_reference_covered_seconds"] += min(ref_count, known_count) * seconds
    denominator = totals["reference_speaker_seconds"]
    error_seconds = sum(totals[key] for key in (
        "missed_speaker_seconds", "false_alarm_speaker_seconds", "confused_speaker_seconds",
    ))
    known_covered = totals["known_reference_covered_seconds"]
    result["metrics"] = {
        **totals,
        "der": error_seconds / denominator if denominator else None,
        "known_speaker_coverage": known_covered / denominator if denominator else None,
        "known_speaker_accuracy": totals["correct_speaker_seconds"] / known_covered if known_covered else None,
        "speaker_mapping": mapping,
        "boundary": _boundary_metrics(slices, tolerance),
        "der_overlap_included": True,
        "der_collar_seconds": 0.0,
    }
    return result
