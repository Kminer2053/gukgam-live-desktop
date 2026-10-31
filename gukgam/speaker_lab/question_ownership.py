"""명시적인 원문 호명으로 질의 요약 범위만 정하는 비파괴 파생 판정."""
from __future__ import annotations

from copy import deepcopy
import re

from ..nlp.floor import is_question_intro_prefix, scan_anchors
from ..nlp.handover_parts import direct_handover_parts
from ..transcript_review import _protected, validate_result


_TRIM = " \t\r\n,.!?。:："
_TRANSITION = re.compile(r"(?:(?:다음은|다음으로|그다음은|이어서|그러면|그럼|계속해서|감사합니다|수고하셨습니다)\s*)*")
_INTRO = re.compile(r"(?:(?:예|네|안녕하세요|저는|제가|국회의원)[,.!?。]?\s*)*")
_REGION_TAIL = re.compile(r"서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주")
_REGION_BODY = re.compile(r"(?:그가|그는|그의|제가|저는|저기|계신|자료|답변|설명|질의|질문|요청|확인|읽|말|인용|예고|예시|소개|보고|대본|대상|사례|(?:^|\s)(?:그|이|저|분|누구)(?=\s|$))")
_REGION_ENDING = re.compile(r"(?:니다|니까|세요|어요|아요|네요|군요|지요|죠|라고|라며|라는|면서|했다|한다|이다)$")


def _regular_call(text: str, cue: dict) -> bool:
    """의사진행·신상 발언을 새 정규 질의로 취급하지 않는다."""
    quote = text[cue["start"]:cue["end"]]
    return bool(cue["kind"] == "call" and not cue.get("requires_context")
                and re.search(r"질의|질문|신문", quote)
                and not re.search(r"의사진행|신상", quote))


def _evidence(uid, cue: dict, position: str = "core") -> dict:
    return {"utterance_id": uid, "start": cue["start"], "end": cue["end"],
            "kind": cue["kind"], "name": cue.get("speaker"), "position": position}


def _lead(prefix: str, kind: str) -> bool:
    prefix = prefix.strip(_TRIM)
    if not prefix:
        return True
    if kind == "self_intro":
        return bool(_INTRO.fullmatch(prefix) or is_question_intro_prefix(prefix)
                    or (len(prefix) <= 40 and re.fullmatch(r"[가-힣\s]{1,38}의", prefix)))
    return bool(_TRANSITION.fullmatch(prefix))


def _split_region_intro(last: dict, call: dict, row: dict, part: dict, roster: list[dict]) -> dict | None:
    """호명 뒤 지역 첫말과 바로 다음 행 자기소개를 기존 강한 가드로 연결한다."""
    tail = last["text"][call["end"]:].strip(_TRIM)
    if (not _REGION_TAIL.fullmatch(tail) or part["utterance_id"] != row["id"]
            or part["start"] != 0 or part["role"] not in {"member", "unknown"}):
        return None
    text = row.get("text") or ""
    intro = re.search(r"(?<![가-힣])" + re.escape(call["speaker"])
                      + r"\s*(?:위원|의원)입니다(?=$|[\s.!?。])", text[:80])
    if not intro or intro.end() > part["end"]:
        return None
    prefix = text[:intro.start()].strip()
    # 지역명으로 보이는 제한된 앞말만 잇는다. 명시 본문·제삼자·인용 예고는 제외한다.
    if (len(prefix) > 40 or _REGION_BODY.search(prefix)
            or any(_REGION_ENDING.search(word) for word in prefix.split())):
        return None
    request = re.search(r"질의|질문", last["text"][call["start"]:call["end"]])
    if not request:
        return None
    joined = last["text"][call["start"] + request.start():] + "\n" + text
    members = {entry["name"] for entry in roster if entry.get("role") == "member"}
    if not direct_handover_parts(joined, members, expected_name=call["speaker"], allow_short_call=False):
        return None
    return {"kind": "self_intro", "start": intro.start(), "end": intro.end(),
            "speaker": call["speaker"], "role": "member"}


def derive_question_ownership(snapshot: dict, turns: list[dict]) -> dict:
    """검증된 원문 부분을 유지하며 주질의자와 요약에 넣을 연속 범위를 제안한다.

    `audio_evidence.context`의 before/after는 호출자가 확인한 원출처 순서다.
    바로 앞 호명만 시작 근거로 쓰며 문맥 행을 core에 넣지 않는다. 내부 경계가
    기존 부분 중간에 있거나 이름·보호 근거가 충돌하면 전체 요약을 보류한다.
    익명 음성 ID·기존 블록 이름·다수결은 이름 확정 근거로 사용하지 않는다.
    """
    original = deepcopy(turns) if isinstance(turns, list) else []
    original_questioner = (snapshot.get("block") or {}).get("questioner") if isinstance(snapshot, dict) else None
    common = {"validation_status": "not_audio_verified", "production_changed": False,
              "original_questioner": original_questioner}

    def held(issue: str, evidence: list[dict] | None = None) -> dict:
        return {**common, "status": "held", "questioner": None, "core_turns": [],
                "excluded_turns": deepcopy(original), "issues": [issue],
                "evidence": evidence or [], "scope": None}

    try:
        canonical = validate_result(snapshot, turns)
    except (ValueError, KeyError, TypeError, AttributeError):
        return held("invalid_canonical_turns")
    rows = snapshot.get("utterances") or []
    if (snapshot.get("block") or {}).get("kind") != "question":
        return held("not_question_block")
    if not rows or not canonical:
        return held("empty_question")
    if any(_protected(snapshot, row) for row in rows):
        return held("protected_question_source")
    roster = snapshot.get("roster") or []
    by_id = {row["id"]: row for row in rows}
    part_indices: dict[int, list[int]] = {}
    for index, part in enumerate(canonical):
        part_indices.setdefault(part["utterance_id"], []).append(index)

    cues = []
    for row in rows:
        text = row.get("text") or ""
        for cue in scan_anchors(text, roster):
            if cue["kind"] == "self_intro" and cue.get("role") != "member":
                continue
            if cue["kind"] not in {"self_intro", "call", "chair_return", "chair_question"}:
                continue
            containing = [i for i in part_indices[row["id"]]
                          if canonical[i]["start"] <= cue["start"] < canonical[i]["end"]]
            if not containing:
                return held("anchor_outside_canonical_parts")
            cues.append((containing[0], row, cue))

    before = [row for row in (snapshot.get("audio_evidence") or {}).get("context", [])
              if isinstance(row, dict) and row.get("position") == "before"]
    context_owner, context_proof, split_context_call = None, [], None
    if before:
        last = before[-1]
        text = last.get("text")
        if isinstance(text, str):
            calls = [cue for cue in scan_anchors(text, roster) if _regular_call(text, cue)]
            if len(calls) == 1 and not text[calls[0]["end"]:].strip(_TRIM):
                context_owner = calls[0]["speaker"]
                context_proof = [_evidence(last.get("id"), calls[0], "before")]
            elif len(calls) == 1 and _REGION_TAIL.fullmatch(text[calls[0]["end"]:].strip(_TRIM)):
                split_context_call = calls[0]
                intro = _split_region_intro(last, calls[0], rows[0], canonical[0], roster)
                if intro:
                    context_owner = calls[0]["speaker"]
                    context_proof = [_evidence(last.get("id"), calls[0], "before"),
                                     _evidence(rows[0]["id"], intro)]
                    if not any(row["id"] == rows[0]["id"] and cue["start"] == intro["start"]
                               and cue["end"] == intro["end"] for _, row, cue in cues):
                        cues.insert(0, (0, rows[0], intro))

    strong = [(index, row, cue) for index, row, cue in cues
              if cue["kind"] == "self_intro" or _regular_call(row.get("text") or "", cue)]
    start, owner, proof = 0, context_owner, list(context_proof)
    opening = None
    if owner is None:
        if (split_context_call and strong and strong[0][0] == 0
                and strong[0][2]["kind"] == "self_intro"
                and strong[0][2]["speaker"] != split_context_call["speaker"]):
            return held("adjacent_call_and_intro_conflict", [
                _evidence(before[-1].get("id"), split_context_call, "before"),
                _evidence(strong[0][1]["id"], strong[0][2])])
        if not strong:
            return held("missing_explicit_question_owner")
        index, row, cue = strong[0]
        # 호명 전에 기관 답변·다른 위원 본문이 있으면 뒤 이름으로 소급하지 않는다.
        if any(p["role"] not in {"chair", "unknown"} or
               (p["role"] == "unknown" and (by_id[p["utterance_id"]].get("text") or "")[p["start"]:p["end"]].strip())
               for p in canonical[:index]):
            return held("unanchored_question_prefix", [_evidence(row["id"], cue)])
        part = canonical[index]
        prefix = row["text"][part["start"]:cue["start"]]
        if not _lead(prefix, cue["kind"]) or cue["end"] > part["end"]:
            return held("owner_boundary_inside_canonical_part", [_evidence(row["id"], cue)])
        if cue["kind"] == "call":
            if part["role"] != "chair" or row["text"][cue["end"]:part["end"]].strip(_TRIM):
                return held("owner_boundary_inside_canonical_part", [_evidence(row["id"], cue)])
            start = index + 1
        else:
            if part["role"] not in {"member", "unknown"}:
                return held("owner_anchor_role_conflict", [_evidence(row["id"], cue)])
            start = index
        owner, opening = cue["speaker"], cue
        proof.append(_evidence(row["id"], cue))
    elif strong:
        index, row, cue = strong[0]
        if index == 0 and cue["kind"] == "self_intro" and cue["speaker"] != owner:
            return held("adjacent_call_and_intro_conflict", proof + [_evidence(row["id"], cue)])

    stop = len(canonical)
    end_cue = None
    for index, row, cue in cues:
        if index < start or cue is opening:
            continue
        part = canonical[index]
        name, kind = cue.get("speaker"), cue["kind"]
        ending = ((kind == "chair_return" and cue.get("finished_member") == owner)
                  or (kind == "self_intro" and name != owner)
                  or (_regular_call(row["text"], cue) and name != owner))
        if stop < len(canonical):
            if ((kind == "self_intro" or _regular_call(row["text"], cue)) and name == owner):
                return held("discontinuous_question_owner", proof + [_evidence(row["id"], cue)])
            continue
        if kind == "call" and not _regular_call(row["text"], cue):
            if name != owner:
                return held("non_question_floor_change", proof + [_evidence(row["id"], cue)])
            continue
        if kind == "chair_question":
            return held("chair_question_inside_scope", proof + [_evidence(row["id"], cue)])
        if not ending:
            continue
        prefix = row["text"][part["start"]:cue["start"]]
        expected_role = "member" if kind == "self_intro" else "chair"
        if (not _lead(prefix, kind) or cue["end"] > part["end"]
                or part["role"] not in {expected_role, "unknown"}):
            return held("owner_boundary_inside_canonical_part", proof + [_evidence(row["id"], cue)])
        stop, end_cue = index, cue
        proof.append(_evidence(row["id"], cue))

    core = canonical[start:stop]
    if not core or not any(p["role"] in {"member", "answer", "unknown"} for p in core):
        return held("empty_question_scope", proof)
    for part in core:
        if part["role"] == "member" and part["speaker"] not in {"", "위원", owner}:
            return held("named_member_conflicts_with_owner", proof)
    # 수동 채택의 표시 묶음을 이 함수에서 고치지 않는다. 요약 소비자는 원문 범위만 쓴다.
    excluded = canonical[:start] + canonical[stop:]
    first, last = core[0], core[-1]
    return {**common, "status": "draft", "questioner": owner, "core_turns": deepcopy(core),
            "excluded_turns": deepcopy(excluded), "issues": [], "evidence": proof,
            "scope": {"start": {"utterance_id": first["utterance_id"], "offset": first["start"]},
                      "end": {"utterance_id": last["utterance_id"], "offset": last["end"]}},
            "questioner_changed": owner != original_questioner,
            "excluded_following_question": bool(end_cue is not None
                and end_cue["kind"] in {"call", "self_intro"} and end_cue.get("speaker") != owner)}
