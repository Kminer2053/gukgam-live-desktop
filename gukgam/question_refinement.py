"""음성 화자 제안과 전체 질의 편집을 원문과 분리한 초안으로 만든다."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import time

from .transcript_review import validate_result


async def refine_question(snapshot, baseline, decision_provider, text_provider, *,
                          is_current=None, progress=None, speaker_timeout=60, text_timeout=90,
                          manuscript_timeout=300):
    """저장·설정 변경 없이 두 단계의 독립 초안을 반환한다. 실패를 재시도하지 않는다."""
    from .audio_speaker_review import review_audio_speakers
    from .transcript_correction import correct_transcript, validate_correction_result

    frozen, source_turns = deepcopy(snapshot), deepcopy(baseline)
    validate_result(frozen, source_turns)
    timings = {}

    def current():
        return snapshot == frozen and baseline == source_turns and (is_current is None or is_current())

    def event(stage, state):
        if progress:
            progress({"stage": stage, "state": state, "monotonic": time.monotonic()})

    result = {"turns": deepcopy(source_turns), "issues": [], "timings": timings,
              "speaker_review": {"status": "held"},
              "text_correction": {"status": "skipped", "patches": [], "held": []},
              "editor": {"status": "held", "provider": "jev_then_local_mlx",
                         "reason": "화자 검토를 완료하지 못해 본문 교정을 보류했습니다"}}
    if not current():
        result["editor"]["status"] = "stale"
        return result
    event("speaker", "started")
    started = time.monotonic()
    try:
        speaker = await asyncio.wait_for(review_audio_speakers(deepcopy(frozen),
            deepcopy(source_turns), decision_provider), timeout=speaker_timeout)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        speaker = {"status": "timeout" if isinstance(error, asyncio.TimeoutError) else "failed",
                   "turns": deepcopy(source_turns), "issues": [{"kind": "speaker_stage_failed"}]}
    timings["speaker_seconds"] = time.monotonic() - started
    result["speaker_review"] = speaker
    result["issues"].extend(speaker.get("issues", []))
    event("speaker", speaker["status"])
    if not current():
        result["editor"]["status"] = "stale"
        return result
    # 새 작업은 독립 전체 전사를 읽는다. 과거 저장 자료의 후보 선택 재생은 유지한다.
    if frozen.get("audio_evidence", {}).get("whole_audio"):
        from .question_manuscript import build_held_document, build_question_manuscript
        if speaker.get("status") == "draft":
            try:
                result["turns"] = deepcopy(validate_result(frozen, speaker["turns"]))
            except (KeyError, TypeError, ValueError):
                result["issues"].append({"kind": "speaker_proposal_invalid"})
        result["speaker_hold_ids"] = deepcopy(speaker.get("held_utterance_ids", []))
        editorial_snapshot = deepcopy(frozen)
        editorial_snapshot["speaker_review"] = deepcopy(speaker)
        event("manuscript", "started")
        started = time.monotonic()
        try:
            document = await build_question_manuscript(
                editorial_snapshot, deepcopy(result["turns"]), text_provider, is_current=current,
                timeout_seconds=manuscript_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            document = build_held_document(editorial_snapshot, result["turns"])
            document["model_status"] = "timeout" if isinstance(error, TimeoutError) else "failed"
        timings["manuscript_seconds"] = time.monotonic() - started
        event("manuscript", document["status"])
        if not current():
            result.update(turns=deepcopy(source_turns))
            result["editor"]["status"] = "stale"
            return result
        result["final_document"] = document
        result["text_correction"] = {"status": document["status"], "patches": [], "held": [],
                                     "model_status": document.get("model_status", "not_called"),
                                     "reason": "whole_question_manuscript"}
        result["editor"] = {"status": {"draft": "refined", "partial": "partial"}.get(document["status"], "held"),
            "provider": "whole_question_audio", "model": getattr(text_provider, "model", ""),
            "speaker_status": speaker.get("status"), "text_status": document["status"],
            "reason": "원자막·1차 문맥 초안·전체 음성 전사를 대조한 최종 검토 초안입니다",
            "text_corrections_applied_to_source": False}
        return result
    if speaker["status"] != "draft":
        return result
    turns = validate_result(frozen, speaker["turns"])
    result["turns"] = deepcopy(turns)
    correction_snapshot = deepcopy(frozen)
    correction_snapshot["speaker_hold_ids"] = deepcopy(speaker.get("held_utterance_ids", []))
    result["speaker_hold_ids"] = correction_snapshot["speaker_hold_ids"]
    event("text", "started")
    started = time.monotonic()
    try:
        correction = await asyncio.wait_for(correct_transcript(deepcopy(correction_snapshot), deepcopy(turns),
            text_provider, max_tokens=600), timeout=text_timeout)
        validate_correction_result(correction_snapshot, turns, correction)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        correction = {"status": "timeout" if isinstance(error, asyncio.TimeoutError) else "failed",
                      "patches": [], "held": [], "reason": "본문 교정 미완료·화자 판본 유지"}
    timings["text_seconds"] = time.monotonic() - started
    event("text", correction["status"])
    if not current():
        result.update(turns=deepcopy(source_turns))
        result["editor"]["status"] = "stale"
        return result
    result["text_correction"] = correction
    result["editor"] = {"status": "refined" if turns != source_turns else "unchanged",
        "provider": "jev_then_local_mlx", "model": getattr(text_provider, "model", ""),
        "speaker_status": speaker["status"], "text_status": correction["status"],
        "reason": "Jev 화자 판본을 고정하고 별도 전사 교정안을 작성했습니다",
        "text_corrections_applied_to_source": False}
    if (correction["status"] in {"failed", "timeout", "held", "skipped", "partial"}
            or speaker.get("complete") is False or speaker.get("held_utterance_ids")):
        result["editor"].update(status="partial", reason="화자 또는 본문에 확인이 필요한 부분이 남았습니다")
    return result
