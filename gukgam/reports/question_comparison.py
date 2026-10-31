"""질의 대조의 보존 원문·실시간 판정·사후 교정안을 읽기 전용으로 투영한다."""
from __future__ import annotations

from copy import deepcopy

from ..transcript_review import _original_turn


def display_parts(parts: list[dict], patches: list[dict]) -> list[dict]:
    """검증된 교정안의 길이 변화만 화면 좌표에 반영하고 원문 좌표는 보존한다."""
    result = []
    for part in parts:
        def offset(end):
            return sum(len(p["after"]) - (p["end"] - p["start"])
                       for p in patches if p["end"] <= end)
        result.append({**deepcopy(part), "source_start": part["start"], "source_end": part["end"],
                       "start": part["start"] + offset(part["start"]),
                       "end": part["end"] + offset(part["end"])})
    return result


def initial_stages(row: dict, context_correction: dict | None = None,
                   live_parts: list[dict] | None = None, *, context_radius: int | None = None) -> dict:
    """보존 원문과 검증된 1차 화자·문맥 초안을 각각 보관한다."""
    raw = row.get("original_text")
    manual_text = row.get("edited") or row.get("edited_by") or isinstance(raw, str) and row["text"] != raw
    source_note = ("사람이 수정한 본문을 보존한 당시 판정" if manual_text
                   else "수집 중 저장된 코드 판정")
    if row.get("speaker_source") == "manual":
        source_note = ("사람이 수정한 본문·화자를 보존한 당시 판정" if manual_text
                       else "사람이 지정한 화자를 보존한 당시 판정")
    stages = {
        "raw": {"text": raw if isinstance(raw, str) else None,
                "status": "preserved" if isinstance(raw, str) else "unavailable"},
        "live": {"text": row["text"], "parts": [_original_turn(row)], "status": "saved",
                 "source_note": source_note},
        "post": {"text": None, "parts": [], "status": "reviewing", "speaker_held": False,
                 "text_held": False, "continuity_held": False},
    }
    if context_radius is None:
        context_radius = (context_correction or {}).get("context_radius", 2)
    protected = manual_text or row.get("speaker_source") == "manual" or row.get("review_parts")
    parts = [_original_turn(row)]
    if live_parts is not None and not protected:
        cursor, copied = 0, []
        for part in live_parts:
            if (not isinstance(part, dict) or part.get("utterance_id") != row["id"]
                    or type(part.get("start")) is not int or type(part.get("end")) is not int
                    or part["start"] != cursor or not cursor < part["end"] <= len(row["text"])
                    or part.get("role") not in {"member", "answer", "chair", "unknown", "other"}
                    or not isinstance(part.get("speaker"), str)):
                return stages
            copied.append(deepcopy(part))
            cursor = part["end"]
        if cursor != len(row["text"]) or not copied:
            return stages
        parts = copied
        stages["live"].update(parts=parts, status="draft", stage="realtime_context",
            source_note=f"발언 종료 후 앞뒤 {context_radius}개 발언으로 검토한 1차 화자 초안 · 본문은 저장 판본")
    # 현재 원문 판본 검사를 마친 창 초안만 호출자가 전달한다. 사람 수정은 별도로 보호한다.
    if (not protected
            and isinstance(context_correction, dict)
            and context_correction.get("preview_only") is True
            and context_correction.get("status") in {"draft", "unchanged"}):
        corrected = next((r for r in context_correction.get("rows", [])
                          if isinstance(r, dict) and r.get("id") == row["id"]), None)
        if (corrected and corrected.get("original_text") == row["text"]
                and isinstance(corrected.get("text"), str)):
            changes = corrected.get("changes")
            if not isinstance(changes, list):
                return stages
            cursor, chunks = 0, []
            for change in changes:
                if (not isinstance(change, dict) or type(change.get("start")) is not int
                        or type(change.get("end")) is not int
                        or not cursor <= change["start"] < change["end"] <= len(row["text"])
                        or change.get("before") != row["text"][change["start"]:change["end"]]
                        or not isinstance(change.get("after"), str)
                        or not any(p["start"] <= change["start"] and change["end"] <= p["end"] for p in parts)):
                    return stages
                chunks.extend((row["text"][cursor:change["start"]], change["after"]))
                cursor = change["end"]
            chunks.append(row["text"][cursor:])
            proposed = corrected["text"]
            if proposed != "".join(chunks):
                return stages
            stages["live"].update(text=proposed, status="draft", stage="realtime_context",
                parts=display_parts(parts, changes),
                source_text=row["text"], source_hash=context_correction.get("source_hash"),
                source_note=f"발언 종료 후 앞뒤 {context_radius}개 발언의 문맥으로 정리한 1차 초안 · 원문 보존")
    return stages


def _document_stage(uid: int, document: dict) -> dict | None:
    """분리·병합한 최종 문장을 첫 원문 행에만 표시하며 나머지는 연결을 남긴다."""
    if (document.get("preview_only") is not True or document.get("status") not in {"draft", "partial"}
            or not isinstance(document.get("units"), list)):
        return None
    anchored, joined = [], []
    for index, unit in enumerate(document["units"]):
        spans = unit.get("source_spans", []) if isinstance(unit, dict) else []
        if (not spans or not isinstance(unit.get("text"), str)
                or any(not isinstance(s, dict) or type(s.get("utterance_id")) is not int
                       or s["utterance_id"] < 1 for s in spans)):
            return None
        if uid not in {s["utterance_id"] for s in spans}:
            continue
        if spans[0]["utterance_id"] == uid:
            anchored.append({**deepcopy(unit), "unit_index": index})
        else:
            joined.append({"unit_index": index, "utterance_id": spans[0]["utterance_id"]})
    if not anchored and not joined:
        return None
    uncertain = any(u.get("scope") == "uncertain" or u.get("role") in {"unknown", "other"}
                    or "speaker_identity_unverified" in u.get("risk_flags", []) for u in anchored)
    return {"text": "\n\n".join(u["text"] for u in anchored), "parts": [], "units": anchored,
            "joined_into": joined, "status": "draft", "stage": "question_audio_final",
            "speaker_held": uncertain, "text_held": any(u.get("scope") == "uncertain" for u in anchored),
            "continuity_held": False, "text_status": document["status"],
            "source_hash": document.get("source_hash"), "preview_only": True, "audio_verified": False,
            "source_note": "질의 전체 녹음 전사와 원자막·1차 초안을 대조한 최종 초안 · 검수 전, 원문 보존"}


def final_stage(row: dict, turns: list[dict], refinement: dict) -> dict:
    """동일한 최종 화자 판본의 검증된 교정 본문만 3열에 표시한다."""
    uid = row["utterance_id"]
    document = refinement.get("final_document")
    if isinstance(document, dict):
        stage = _document_stage(uid, document)
        if stage is not None:
            return stage
    parts = [deepcopy(p) for p in turns if p["utterance_id"] == uid]
    text = row["stages"]["live"]["text"]
    held = refinement.get("speaker_status") != "draft" or uid in refinement.get("held_utterance_ids", [])
    text_status = refinement.get("text_status", "skipped")
    held_text = refinement.get("held", [])
    # 정상 종료한 후보 미선택·국소 보류를 전체 질의의 본문 실패로 확대하지 않는다.
    failed_text = text_status in {"timeout", "failed", "skipped"} or text_status == "held" and (
        refinement.get("text_model_status") in {"failed", "completed_stale"}
        or refinement.get("text_reason") in {"model_unavailable", "input_changed", "model_or_response_error"}
        or not held_text)
    text_held = failed_text or any(p.get("utterance_id") == uid for p in held_text)
    edges = refinement.get("held_continuity_boundaries", [])
    neighbours = sorted({v for edge in edges
                         if uid in (edge["previous_utterance_id"], edge["utterance_id"])
                         for v in (edge["previous_utterance_id"], edge["utterance_id"]) if v != uid})
    result = {"text": text, "parts": parts, "status": "held" if held else "draft",
              "speaker_held": held, "text_status": text_status, "text_held": text_held,
              "continuity_held": bool(neighbours), "continuity_with": neighbours,
              "source_note": "화자·본문은 검토 전 제안이며 원문과 요약 본문을 덮어쓰지 않습니다."}
    corrected = next((r for r in refinement.get("rows", []) if r["utterance_id"] == uid), None)
    if corrected is not None:
        # 앞 단계와 다른 그룹·좌표·판정의 문장을 섞어 표시하지 않는다.
        if corrected.get("locked_turns") != parts or corrected.get("original_text") != text:
            result.update(text=None, parts=[], status="unavailable", text_held=True,
                          source_note="교정안과 최종 화자 판본이 달라 사후 본문을 숨겼습니다.")
        else:
            result.update(text=corrected["proposed_text"],
                          parts=display_parts(parts, corrected["patches"]))
    return result
