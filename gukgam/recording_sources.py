"""확정 녹음 전사를 원문 좌표가 있는 가상 출처로 고정한다. DB·모델 호출은 하지 않는다."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .block_sources import _anchor_matches
from .nlp.floor import is_reported_anchor
from .serialize import acoustic_identity


SOURCE_KIND = "recording_asr"
_META_FIELDS = (
    "session_id", "block_id", "source_key", "speaker", "speaker_role", "speaker_source",
    "speaker_hint", "source_speaker_hint", "acoustic_speaker_id", "acoustic_speaker_label",
    "acoustic_overlap", "review_parts", "review_status", "edited_by", "diarization",
    "stt_provenance", "offset_ms", "dur_ms",
)
_ACK = re.compile(r"^(?:(?:예|네|그럼|그러면)[,.!?。\s]*)*$")
_RECORDING_CALL = re.compile(
    r"(?:(?:다음은|다음으로|다음에는)\s*(?:존경하는\s*)?|존경하는\s*)"
    r"(?P<name>[가-힣]{2,5})\s*(?:위원|의원)(?:님)?\s*"
    r"[^.!?。]{0,40}?해\s*주시겠습니다[.!?。]*")


def _digest(value) -> str:
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("녹음 출처 메타데이터가 잘못되었습니다") from None


def _integer(value, *, minimum=0) -> bool:
    return type(value) is int and value >= minimum


def _validate_metadata(meta: dict) -> None:
    if (not isinstance(meta, dict) or not _integer(meta.get("utterance_id"), minimum=1)
            or not _integer(meta.get("seq"), minimum=1) or not _integer(meta.get("ts_start"))
            or not _integer(meta.get("ts_end")) or meta["ts_end"] < meta["ts_start"]
            or type(meta.get("is_final")) is not bool or type(meta.get("edited")) is not bool
            or not isinstance(meta.get("current_text_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", meta["current_text_sha256"])):
        raise ValueError("녹음 출처 식별자·시간·확정 상태가 잘못되었습니다")
    for key in ("source_key", "speaker_hint", "source_speaker_hint", "acoustic_speaker_label"):
        if meta.get(key) is not None and not isinstance(meta[key], str):
            raise ValueError("녹음 출처의 화자 단서가 잘못되었습니다")
    if meta.get("acoustic_overlap") is not None and type(meta["acoustic_overlap"]) is not bool:
        raise ValueError("녹음 출처의 겹침 단서가 잘못되었습니다")
    identity = meta.get("acoustic_speaker_id")
    if identity is not None and (not isinstance(identity, str)
            or not re.fullmatch(r"nemo:(?:e[1-9][0-9]{0,5}:)?[0-7]", identity)):
        raise ValueError("녹음 출처의 익명 화자 단서가 잘못되었습니다")


def _acoustic(meta: dict) -> dict:
    result = acoustic_identity(meta)
    if (meta.get("acoustic_speaker_id") is not None
            and result.get("acoustic_speaker_id") != meta["acoustic_speaker_id"]):
        raise ValueError("녹음 출처의 익명 화자 단서가 서로 다릅니다")
    if (meta.get("acoustic_overlap") is not None and result.get("acoustic_overlap", False)
            != meta["acoustic_overlap"]):
        raise ValueError("녹음 출처의 겹침 단서가 서로 다릅니다")
    # 미정 표시도 출처에 보존한다. 익명 ID나 실명은 새로 만들지 않는다.
    if "acoustic_speaker_label" in meta and not result:
        result["acoustic_speaker_label"] = meta["acoustic_speaker_label"]
    return result


def _protection(meta: dict, text: str) -> list[str]:
    reasons = []
    if meta["edited"] or hashlib.sha256(text.encode("utf-8")).hexdigest() != meta["current_text_sha256"]:
        reasons.append("utterance_edited")
    if meta.get("speaker_source") in {"manual", "reviewed"}:
        reasons.append("speaker_protected")
    if meta.get("review_parts") or meta.get("review_status") == "reviewed" or meta.get("edited_by"):
        reasons.append("utterance_reviewed")
    return reasons


def _virtual_source(text: str, meta: dict) -> dict:
    _validate_metadata(meta)
    if not isinstance(text, str):
        raise ValueError("녹음 원문이 없습니다")
    acoustic = _acoustic(meta)
    source_hash = _digest({"source_kind": SOURCE_KIND, "text": text, "recording_metadata": meta})
    protected = _protection(meta, text)
    return {
        "id": meta["utterance_id"], "source_kind": SOURCE_KIND,
        "source_key": meta.get("source_key") or f"recording_asr:{meta['utterance_id']}",
        "origin_utterance_id": meta["utterance_id"], "origin_seq": meta["seq"],
        "session_id": meta.get("session_id"), "block_id": meta.get("block_id"),
        "text": text, "original_text": text,
        "revision": 1 + int(source_hash[:12], 16), "source_hash": source_hash,
        "ts_start": meta["ts_start"], "ts_end": meta["ts_end"],
        "first_rx": meta["ts_start"], "last_rx": meta["ts_end"],
        "stable_at": meta["ts_end"] if meta["is_final"] else None,
        "timestamp_basis": "recording_audio_timeline",
        "final": meta["is_final"], "is_final": meta["is_final"],
        "parts": [{"label": acoustic.get("acoustic_speaker_id"), "text": text}],
        "recording_metadata": deepcopy(meta), **acoustic,
        "protected": bool(protected), "protection_reasons": protected,
        "ground_truth_verified": False,
    }


def recording_source(row: dict) -> dict | None:
    """DB/공개 발언 한 행을 같은 UID의 가상 출처로 복사한다. 시스템 표지는 제외한다."""
    if not isinstance(row, dict):
        raise ValueError("녹음 발언 형식이 잘못되었습니다")
    if row.get("speaker_role") == "system":
        return None
    text = row.get("original_text") if "original_text" in row else row.get("text")
    current = row.get("text", text)
    final = row.get("is_final")
    if not isinstance(text, str) or not isinstance(current, str) or type(final) not in (bool, int) or final not in (0, 1):
        raise ValueError("녹음 원문·확정 상태가 잘못되었습니다")
    meta = {key: deepcopy(row[key]) for key in _META_FIELDS if key in row}
    meta.update(utterance_id=row.get("id"), seq=row.get("seq", row.get("id")),
                ts_start=row.get("ts_start"), ts_end=row.get("ts_end"), is_final=bool(final),
                edited=bool(row.get("edited")), current_text_sha256=hashlib.sha256(current.encode("utf-8")).hexdigest())
    return _virtual_source(text, meta)


def recording_sources(utterances: list[dict]) -> list[dict]:
    """원 순번으로 정렬하되 중복 식별자·순번을 임의로 합치지 않는다."""
    sources = [source for row in utterances if (source := recording_source(row)) is not None]
    if (len({source["id"] for source in sources}) != len(sources)
            or len({source["origin_seq"] for source in sources}) != len(sources)):
        raise ValueError("녹음 발언 식별자·순번이 중복됐습니다")
    return sorted(sources, key=lambda source: (source["origin_seq"], source["id"]))


def recording_source_ready(source: dict) -> bool:
    """자막 scd를 사용하지 않고 원문·시간·음향·확정 판본의 자체 일치를 검증한다."""
    if (not isinstance(source, dict) or source.get("source_kind") != SOURCE_KIND or "scd" in source
            or source.get("final") is not True or source.get("is_final") is not True
            or not _integer(source.get("id"), minimum=1)
            or not _integer(source.get("revision"), minimum=1) or source["revision"] > 2 ** 48
            or not _integer(source.get("ts_start")) or not _integer(source.get("ts_end"))):
        return False
    try:
        expected = _virtual_source(source["text"], source["recording_metadata"])
        return (expected["final"] is True and not expected["protected"]
                and all(key in source and source[key] == value for key, value in expected.items()))
    except (KeyError, TypeError, ValueError):
        return False


def _block_protected(block: dict) -> bool:
    summary = block.get("summary")
    return bool(block.get("review_status") == "reviewed" or block.get("edited_by")
                or isinstance(summary, dict) and summary.get("edited_by"))


def _identity(block: dict, anchor: dict | None, roster) -> dict:
    current, raw = str(block.get("questioner") or "").strip(), str((anchor or {}).get("name") or "")
    exact = [person for person in roster if person.get("role") == "member" and person.get("name") == raw]
    result = {"kind": "stored_question", "original_kind": block.get("kind"),
              "original_questioner": current, "questioner": current,
              "questioner_party": block.get("questioner_party") or "", "raw_call_name": raw,
              "status": "stored" if current else "needs_name", "uncertain": not bool(current),
              "raw_name_rewritten": False, "ground_truth_verified": False}
    if not current and raw and len(exact) == 1:
        result.update(kind="derived_from_named_call", questioner=raw,
                      questioner_party=exact[0].get("party") or "", status="roster_exact_call", uncertain=True)
    elif raw and current and raw != current and len(exact) == 1:
        result.update(status="conflict", uncertain=True)
    return result


def _recording_anchors(text: str, roster) -> list[dict]:
    """ASR의 질의 명사 철자를 고치지 않고 완결된 명시 진행 서식만 보완한다."""
    anchors = _anchor_matches(text, closing=True, roster=roster)
    names = [person.get("name") for person in roster if person.get("role") == "member"]
    for match in _RECORDING_CALL.finditer(text):
        name = match["name"]
        if names.count(name) != 1 or is_reported_anchor(text, match.start(), match.end()):
            continue
        candidate = {"kind": "call", "name": name, "origin": "recording_explicit_floor_frame",
                     "start": match.start(), "end": match.end(), "_literal_start": match.start("name")}
        same = [anchor for anchor in anchors if anchor.get("name") == name and anchor["end"] == candidate["end"]]
        if same:
            for anchor in same:
                if candidate["start"] < anchor["start"]:
                    anchor.update(candidate)
        else:
            anchors.append(candidate)
    return sorted(anchors, key=lambda anchor: (anchor["start"], anchor["end"]))


def _spans(sources: list[dict], offsets: dict, left: int, right: int) -> list[dict]:
    if right <= left:
        return []
    return [{"source_id": source["id"], "start": max(0, left - offsets[source["id"]]),
             "end": min(len(source["text"]), right - offsets[source["id"]])}
            for source in sources if offsets[source["id"]] < right
            and offsets[source["id"]] + len(source["text"]) > left]


def _block_stamp(block: dict | None):
    if block is None:
        return None
    summary = block.get("summary")
    return {**{key: deepcopy(block.get(key)) for key in
               ("id", "kind", "questioner", "questioner_party", "is_open", "review_status", "reviewed_by", "reviewed_at")},
            "summary_protection": {key: summary.get(key) for key in ("edited_by", "edited_at")}
            if isinstance(summary, dict) and summary.get("edited_by") else None}


def select_recording_sources(block: dict, blocks: list[dict], utterances: list[dict], roster=(), *,
                             recording_complete: bool = False) -> dict:
    """폐쇄 질의의 ASR 범위를 동결한다. EOF 확인은 호출자가 실제 파일 상태로 전달한다."""
    if not isinstance(block, dict) or not _integer(block.get("id"), minimum=1):
        raise ValueError("녹음 질의 식별자가 잘못되었습니다")
    if type(recording_complete) is not bool:
        raise ValueError("녹음 완료 확인값이 잘못되었습니다")
    sources = recording_sources(utterances)
    targets = [source for source in sources if source.get("block_id") == block["id"]]
    previous = max((b for b in blocks if b["id"] < block["id"]), key=lambda b: b["id"], default=None)
    following = min((b for b in blocks if b["id"] > block["id"]), key=lambda b: b["id"], default=None)
    dependency_blocks = {b["id"] for b in (block, previous, following) if b}
    dependencies = [source for source in sources if source.get("block_id") in dependency_blocks]
    offsets, cursor = {}, 0
    for source in dependencies:
        offsets[source["id"]] = cursor
        cursor += len(source["text"]) + 1
    joined = "\n".join(source["text"] for source in dependencies)
    reasons = []
    if block.get("kind") != "question":
        reasons.append("not_question")
    if type(block.get("is_open")) not in (bool, int) or block["is_open"] != 0:
        reasons.append("block_open")
    if not recording_complete:
        reasons.append("recording_not_complete")
    if not targets:
        reasons.append("no_utterances")
    if _block_protected(block) or any(source["protected"] for source in targets):
        reasons.append("protected")
    if any(not source["final"] for source in targets):
        reasons.append("utterance_not_final")
    left = offsets[targets[0]["id"]] if targets else 0
    right = offsets[targets[-1]["id"]] + len(targets[-1]["text"]) if targets else 0
    anchors = _recording_anchors(joined, roster)
    starts = [anchor for anchor in anchors if anchor["kind"] == "call" and anchor["end"] <= left]
    start_anchor = max(starts, key=lambda anchor: anchor["end"], default=None)
    if start_anchor is None and targets:
        first_end = offsets[targets[0]["id"]] + len(targets[0]["text"])
        inline = [anchor for anchor in anchors if anchor["kind"] == "call" and left <= anchor["start"] < first_end
                  and _ACK.fullmatch(joined[left:anchor["start"]].strip())]
        if inline:
            start_anchor = min(inline, key=lambda anchor: anchor["start"])
            left = start_anchor["end"]
    identity = _identity(block, start_anchor, roster)
    if identity["status"] == "conflict":
        reasons.append("start_call_name_conflict")
    # 현재 floor의 완결 호명 뒤에 잘못 other/chair로 남은 질문 앞부분만 복원한다.
    # 명단에 없는 이름이나 철자 후보를 근거로 앞 블록을 가져오지는 않는다.
    raw_name = identity["raw_call_name"]
    unique_call = sum(person.get("role") == "member" and person.get("name") == raw_name for person in roster) == 1
    restore_previous = bool(start_anchor and previous and previous.get("kind") in {"other", "chair"}
                            and raw_name and unique_call and identity["questioner"] == raw_name
                            and identity["status"] != "conflict" and start_anchor["end"] <= left)
    if restore_previous:
        left = start_anchor["end"]
    current_names = {identity["questioner"], identity["raw_call_name"]} - {""}
    endings = [anchor for anchor in anchors if anchor["start"] >= left
               and anchor is not start_anchor and (anchor["kind"] != "call" or anchor.get("name") not in current_names)]
    end_anchor = min(endings, key=lambda anchor: anchor["start"], default=None)
    if end_anchor:
        right = min(right, end_anchor["start"])
    if targets and any(source.get("block_id") != block["id"] and
                       targets[0]["origin_seq"] < source["origin_seq"] < targets[-1]["origin_seq"] for source in sources):
        reasons.append("block_membership_interleaved")
    chosen, membership = [], []
    selectable = [source for source in dependencies if source.get("block_id") == block["id"]
                  or restore_previous and source.get("block_id") == previous["id"]]
    for source in selectable:
        slices = _spans([source], offsets, left, right)
        if not slices:
            continue
        selected = [{"start": span["start"], "end": span["end"]} for span in slices]
        chosen.append({**deepcopy(source), "selected_spans": selected,
                       "selected_text": "".join(source["text"][s["start"]:s["end"]] for s in selected)})
        membership.append({"utterance_id": source["id"], "original_block_id": source.get("block_id"),
                           "source_key": source["source_key"], "spans": slices, "selected_spans": slices,
                           "source_hash": source["source_hash"],
                           "restored_from_boundary": source.get("block_id") != block["id"], "reason": None})
    if any(source["protected"] for source in chosen) or restore_previous and _block_protected(previous):
        reasons.append("protected")
    if any(not source["final"] for source in chosen):
        reasons.append("utterance_not_final")
    if not chosen or not any(source["selected_text"].strip() for source in chosen):
        reasons.append("no_selected_text")
    if len(chosen) > 512:
        reasons.append("source_limit")
    if any(not recording_source_ready(source) for source in chosen):
        reasons.append("source_not_ready")
    def public_anchor(anchor):
        return ({**{key: deepcopy(value) for key, value in anchor.items() if not key.startswith("_")},
                 "source_spans": _spans(dependencies, offsets, anchor["start"], anchor["end"])} if anchor else None)
    evidence = {"kind": (end_anchor or {}).get("kind", "recording_eof"),
                "start_anchor": public_anchor(start_anchor), "end_anchor": public_anchor(end_anchor),
                "recording_complete": recording_complete, "next_block_id": (following or {}).get("id"),
                "restored_previous_block_id": previous["id"] if restore_previous else None,
                "membership_method": "녹음 확정 발언 소속과 다음 진행 호명의 원문 좌표",
                "shutdown_closed_is_completion": False, "ground_truth_verified": False}
    dependency_hashes = {str(source["id"]): source["source_hash"] for source in dependencies}
    stamp = _digest({"block": _block_stamp(block), "neighbor_blocks": [_block_stamp(previous), _block_stamp(following)],
                     "source_hashes": dependency_hashes,
                     "selection": [{"id": source["id"], "selected_spans": source["selected_spans"]} for source in chosen],
                     "boundary_evidence": evidence, "roster": list(roster), "recording_complete": recording_complete})
    reasons = sorted(set(reasons))
    return {"source_kind": SOURCE_KIND, "block_id": block["id"], "questioner": identity["questioner"],
            "status": "ready" if not reasons else "excluded", "reasons": reasons,
            "sources": chosen, "question_identity": identity, "boundary_evidence": evidence,
            "source_spans": [span for row in membership for span in row["selected_spans"]],
            "utterance_membership": membership, "membership": deepcopy(membership),
            "dependency_source_ids": [source["id"] for source in dependencies],
            "dependency_source_hashes": dependency_hashes,
            "dependency_block_ids": [b["id"] for b in (previous, following) if b],
            "selection_stamp": stamp, "recording_complete": recording_complete,
            "metrics": {"source_count": len(chosen), "utterance_count": len(targets),
                        "membership_utterance_count": len(membership),
                        "restored_utterance_count": sum(row["restored_from_boundary"] for row in membership),
                        "selected_characters": sum(len(source["selected_text"]) for source in chosen)},
            "ground_truth_verified": False}
