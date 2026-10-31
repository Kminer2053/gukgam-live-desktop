"""릴레이에 전달할 공개 자막·보정 투영. 실행·원음·모델 입력은 포함하지 않는다."""
from __future__ import annotations

import re


def _fields(row: dict, names: tuple[str, ...]) -> dict:
    return {key: value for key in names if (value := row.get(key)) is not None
            and isinstance(value, (str, int, float, bool))}


def _strings(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _objects(value, project) -> list[dict]:
    return [project(v) for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def public_source(row: dict) -> dict:
    out = _fields(row, ("id", "source_key", "segment", "revision", "first_rx", "last_rx", "final", "scd",
                        "text", "transcript", "replayed"))
    out["parts"] = _objects(row.get("parts"), lambda p: _fields(p, ("label", "text")))
    guess = _public_guess(row.get("speaker_guess"))
    if guess is not None:
        out["speaker_guess"] = guess
    return out


def _span(row):
    return _fields(row, ("source_id", "start", "end"))


def _public_guess(guess):
    if not isinstance(guess, dict):
        return None
    out = _fields(guess, ("speaker", "role", "estimated", "basis", "source", "override_existing"))
    refs = guess.get("source_revisions")
    out["source_revisions"] = ({str(key): value for key, value in refs.items()
        if str(key).isdigit() and type(value) is int} if isinstance(refs, dict) else {})
    return out


def public_live_turn(row: dict) -> dict:
    """로컬에서 확인한 기존 발언의 표시 필드만 미러로 보낸다."""
    out = _fields(row, ("id", "seq", "block_id", "ts_start", "ts_end", "speaker", "speaker_role",
                        "speaker_source", "original_text", "text", "is_final", "analyzed", "updated_at",
                        "protected", "raw_revision", "source_order", "turn_id", "boundary_reason"))
    out["source_ids"] = [value for value in row.get("source_ids", []) if type(value) is int and value > 0]
    refs = row.get("source_revisions") or {}
    out["source_revisions"] = ({str(key): value for key, value in refs.items() if type(value) is int}
                               if isinstance(refs, dict) else {})
    out["source_spans"] = _objects(row.get("source_spans"), _span)
    out["review_parts"] = _objects(row.get("review_parts"),
        lambda part: _fields(part, ("start", "end", "role", "speaker", "group_id")))
    guess = _public_guess(row.get("speaker_guess"))
    if guess is not None:
        out["speaker_guess"] = guess
    return out


def live_turn_guess_current(row: dict, sources: dict[int, dict]) -> bool:
    """표시 추정의 앞 호명·이어말하기 근거도 최신 원자막 판본이어야 한다."""
    guess = row.get("speaker_guess")
    if (not isinstance(guess, dict) or guess.get("source") != "display_only"
            or type(guess.get("estimated")) is not bool or not isinstance(guess.get("speaker"), str)
            or guess.get("role") not in {"member", "answer", "chair", "other", "mixed"}):
        return False
    refs = guess.get("source_revisions")
    if not (isinstance(refs, dict) and refs and all(str(uid).isdigit() and int(uid) in sources
            and type(revision) is int and revision > 0 and sources[int(uid)].get("revision") == revision
            and sources[int(uid)].get("replayed") is not True for uid, revision in refs.items())):
        return False
    if isinstance(row.get("source_ids"), list):
        # 말묶음 자체의 모든 원문 판본도 후보 근거에 반드시 들어 있어야 한다.
        return bool(row["source_ids"] and all(refs.get(str(uid)) == (row.get("source_revisions") or {}).get(str(uid))
                                            and str(uid) in refs for uid in row["source_ids"]))
    return type(row.get("id")) is int and refs.get(str(row["id"])) == row.get("revision")


def live_turn_current(row: dict, sources: dict[int, dict]) -> bool:
    """미러는 화자를 재판정하지 않고 로컬 투영의 원자막 판본·문자 좌표만 확인한다."""
    ids, refs, spans = row.get("source_ids"), row.get("source_revisions"), row.get("source_spans")
    if (type(row.get("id")) is not int or row["id"] <= 0 or not isinstance(ids, list) or not ids
            or any(type(uid) is not int or uid <= 0 for uid in ids) or len(ids) != len(set(ids))
            or not isinstance(refs, dict) or set(refs) != {str(uid) for uid in ids}
            or not isinstance(spans, list) or not spans
            or not isinstance(row.get("original_text"), str) or not isinstance(row.get("text"), str)):
        return False
    if any(uid not in sources or type(refs[str(uid)]) is not int
           or refs[str(uid)] != sources[uid].get("revision") or sources[uid].get("replayed") is True for uid in ids):
        return False
    parts, ends = [], {}
    for span in spans:
        if not isinstance(span, dict):
            return False
        uid, start, end = span.get("source_id"), span.get("start"), span.get("end")
        if type(uid) is not int or uid not in ids or type(start) is not int or type(end) is not int:
            return False
        text = sources[uid].get("text")
        if not isinstance(text, str) or not 0 <= start < end <= len(text) or start < ends.get(uid, 0):
            return False
        ends[uid] = end
        parts.append(text[start:end])
    compact = lambda value: "".join(re.sub(r"(?m)^\s*-\s+", "", value).split())
    return set(ends) == set(ids) and compact(row["original_text"]) == compact("".join(parts))


def _edit(row):
    return {**_fields(row, ("start", "end", "before", "after", "reason", "origin")),
            "risk": _strings(row.get("risk"))}


def _sentence(row):
    out = _fields(row, ("id", "original_text", "text", "role", "speaker", "role_status", "group_id",
                        "text_status", "jev_role", "jev_role_uncertain", "role_uncertain", "text_uncertain",
                        "jev_speaker", "role_decided_by", "role_reason"))
    out.update(source_spans=_objects(row.get("source_spans"), _span),
               edits=_objects(row.get("edits"), _edit), uncertainties=_strings(row.get("uncertainties")))
    return out


def _paragraph(row):
    out = _fields(row, ("id", "original_text", "text", "role", "speaker", "role_status", "role_uncertain",
                        "text_uncertain", "text_status", "body_basis"))
    out.update(sentence_ids=_strings(row.get("sentence_ids")),
               source_spans=_objects(row.get("source_spans"), _span), edits=_objects(row.get("edits"), _edit))
    return out


def _issues(value) -> list:
    """이전 문자열과 새 구조화 메시지를 보존하고 진단 원문은 복사하지 않는다."""
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            issue = _fields(item, ("stage", "code", "message"))
            if isinstance(issue.get("message"), str):
                issue["sentence_ids"] = _strings(item.get("sentence_ids"))
                out.append(issue)
    return out


def declaration_anchor(value: dict) -> dict | None:
    """명시 종료 선언의 출처·문자 좌표만 정제한다. 시각이나 회의 상태는 증거가 아니다."""
    if (not isinstance(value, dict) or not isinstance(value.get("kind"), str)
            or value["kind"] not in {"explicit_pause_or_end", "explicit_chair_declaration"}):
        return None
    ids, start, end = value.get("source_ids"), value.get("source_start"), value.get("source_end")
    if (not isinstance(ids, list) or not ids or any(type(uid) is not int or uid <= 0 for uid in ids)
            or len(set(ids)) != len(ids) or not isinstance(start, dict) or not isinstance(end, dict)
            or start.get("source_id") != ids[0] or end.get("source_id") != ids[-1]
            or type(start.get("source_id")) is not int or type(end.get("source_id")) is not int
            or type(start.get("offset")) is not int or start["offset"] < 0
            or type(end.get("offset")) is not int or end["offset"] <= 0
            or len(ids) == 1 and start["offset"] >= end["offset"]):
        return None
    return {"kind": value["kind"], "source_ids": list(ids),
            "source_start": {"source_id": start["source_id"], "offset": start["offset"]},
            "source_end": {"source_id": end["source_id"], "offset": end["offset"]}}


def declaration_role_proof(document: dict) -> bool:
    """내부 실행·저장 판본은 선언에 실제 겹친 진행자 역할 근거를 별도로 요구한다."""
    roles, ids = document.get("declaration_membership"), document.get("declaration_chair_utterance_ids")
    fields = {"utterance_id", "block_id", "speaker_role", "speaker_source"}
    if (not isinstance(roles, list) or not roles or not isinstance(ids, list) or not ids
            or any(type(uid) is not int or uid <= 0 for uid in ids) or ids != sorted(set(ids))
            or any(not isinstance(row, dict) or set(row) != fields or type(row.get("utterance_id")) is not int
                   or row["utterance_id"] <= 0 or not isinstance(row.get("speaker_role"), str)
                   or not isinstance(row.get("speaker_source"), str) for row in roles)):
        return False
    role_ids = [row["utterance_id"] for row in roles]
    if role_ids != sorted(set(role_ids)):
        return False
    by_id = {row["utterance_id"]:row for row in roles}
    if not all(uid in by_id and by_id[uid]["speaker_role"] == "chair" for uid in ids):
        return False
    evidence = document.get("boundary_evidence") or {}
    if not isinstance(evidence,dict):
        return False
    if "completion_scope" not in evidence and "completion_declaration_anchor" not in evidence:
        return True
    scope, end, completion = evidence.get("completion_scope"), evidence.get("end_anchor"), evidence.get("completion_declaration_anchor")
    bid = document.get("block_id")
    policy = document.get("source_settlement_policy")
    if (not isinstance(scope,dict) or scope.get("kind") != "interior_named_call_in_stored_question"
            or evidence.get("kind") != "interior_named_call" or type(bid) is not int or bid <= 0
            or type(scope.get("block_id")) is not int or scope["block_id"] != bid
            or type(scope.get("following_chair_block_id")) is not int or scope["following_chair_block_id"] <= bid
            or evidence.get("next_block_id") != scope["following_chair_block_id"]
            or not isinstance(end,dict) or end.get("kind") != "call"
            or declaration_anchor(completion) is None or not isinstance(policy,dict)
            or declaration_anchor(policy.get("declaration")) != declaration_anchor(completion)):
        return False
    for anchor, owner in ((end,bid),(completion,scope["following_chair_block_id"])):
        owners = anchor.get("chair_utterance_ids")
        if (not isinstance(owners,list) or not owners or any(type(uid) is not int or uid <= 0 for uid in owners)
                or len(owners) != len(set(owners)) or not set(owners) <= set(ids)
                or any(by_id[uid].get("block_id") != owner for uid in owners)):
            return False
    return True


def explicit_declaration_current(anchor: dict, sources: list[dict], refs: dict) -> bool:
    """동일 판본의 실제 확정 선언을 읽는다. 인용·미확정 선언·단순 종료 상태는 거절한다."""
    anchor = declaration_anchor(anchor)
    if anchor is None or not isinstance(sources, list) or not isinstance(refs, dict):
        return False
    ids = anchor["source_ids"]
    rows = [source for source in sources if isinstance(source, dict) and source.get("id") in ids]
    if len(rows) != len(ids) or len({source["id"] for source in rows}) != len(ids):
        return False
    by_id = {source["id"]: source for source in rows}
    if any(type(source.get("id")) is not int or type(source.get("revision")) is not int
           or source["revision"] <= 0 or type(refs.get(str(source["id"]))) is not int
           or refs[str(source["id"])] != source["revision"]
           or not isinstance(source.get("text"), str)
           or any(type(source.get(key)) not in (bool, int) or source[key] != 1 for key in ("final", "scd"))
           or source.get("replayed", False) is not False for source in rows):
        return False
    if all(type(source.get("first_rx")) is int for source in rows):
        if [source["id"] for source in sorted(rows, key=lambda row: (row["first_rx"], row["id"]))] != ids:
            return False
    offsets, chunks, size = {}, [], 0
    for uid in ids:
        offsets[uid] = size
        chunks.append(by_id[uid]["text"])
        size += len(chunks[-1]) + 1
    start, end = anchor["source_start"], anchor["source_end"]
    if (start["offset"] >= len(by_id[start["source_id"]]["text"])
            or end["offset"] > len(by_id[end["source_id"]]["text"])):
        return False
    left, right = offsets[start["source_id"]] + start["offset"], offsets[end["source_id"]] + end["offset"]
    # 같은 표현·인용 가드를 실제 경계 선택과 공유한다. 클라우드에서는 호출할 때만 가져온다.
    from .nlp.floor import explicit_chair_declarations
    text = re.sub(r"(?m)^[ \t]*-[ \t]+", lambda match: " " * len(match.group()), "\n".join(chunks))
    return any(row.get("kind") == anchor["kind"] and left <= row["start"] < row["end"] <= right
               for row in explicit_chair_declarations(text))


def source_settlement_policy(document: dict) -> dict | None:
    """두 후속 질의 또는 검증된 위원장 선언의 증거만 받으며 원수신 표식은 보존한다."""
    policy = document.get("source_settlement_policy")
    block_id = document.get("block_id")
    if (not isinstance(policy, dict) or not isinstance(policy.get("policy"), str)
            or policy["policy"] not in {"after_two_closed_questions", "explicit_chair_declaration"}
            or type(block_id) is not int or block_id <= 0):
        return None
    unconfirmed = policy.get("unconfirmed_source_ids")
    if (not isinstance(unconfirmed, list) or not unconfirmed
            or any(type(value) is not int or value <= 0 for value in unconfirmed)
            or len(set(unconfirmed)) != len(unconfirmed)):
        return None
    refs = document.get("source_revisions")
    if not isinstance(refs, dict):
        return None
    referenced = {int(uid) for uid, revision in refs.items() if str(uid).isascii() and str(uid).isdecimal()
                  and int(uid) > 0 and type(revision) is int and revision > 0}
    if not set(unconfirmed) <= referenced:
        return None
    if policy["policy"] == "explicit_chair_declaration":
        anchor = declaration_anchor(policy.get("declaration"))
        if (anchor is None or not set(anchor["source_ids"]) <= referenced
                or set(anchor["source_ids"]) & set(unconfirmed)):
            return None
        return {"policy": "explicit_chair_declaration", "unconfirmed_source_ids": list(unconfirmed), "declaration": anchor}
    following = policy.get("following_closed_question_ids")
    if (not isinstance(following, list) or len(following) != 2
            or any(type(value) is not int or value <= block_id for value in following)
            or len(set(following)) != 2):
        return None
    return {"policy": "after_two_closed_questions", "following_closed_question_ids": list(following),
            "unconfirmed_source_ids": list(unconfirmed)}


def document_source_ready(document: dict, source: dict, *, declaration_sources: list[dict] | None = None) -> bool:
    """기존 확정 원문 또는 문서의 제한된 후속 질의 정책이 허용한 원문만 읽는다."""
    if source.get("replayed") is True:
        return False
    if source.get("final") is True and source.get("scd") is True:
        return True
    policy = source_settlement_policy(document)
    if policy and policy["policy"] == "explicit_chair_declaration" and not explicit_declaration_current(
            policy["declaration"], declaration_sources or [], document.get("source_revisions")):
        return False
    return bool(policy and type(source.get("id")) is int
                and source["id"] in policy["unconfirmed_source_ids"])


def public_document(row: dict) -> dict:
    """중첩 필드도 허용목록으로 새로 구성해 과거 원음·모델 요청을 차단한다."""
    out = _fields(row, ("id", "input_hash", "status", "created_at", "updated_at", "first_rx", "last_rx",
                        "mode", "editor_version", "block_id", "questioner"))
    out["source_ids"] = [v for v in row.get("source_ids", []) if type(v) is int]
    refs = row.get("source_revisions") or {}
    out["source_revisions"] = {str(k): v for k, v in refs.items() if type(v) is int} if isinstance(refs, dict) else {}
    policy = source_settlement_policy(row)
    if policy is not None:
        out["source_settlement_policy"] = policy
    out["sentences"] = _objects(row.get("sentences"), _sentence)
    out["paragraphs"] = _objects(row.get("paragraphs"), _paragraph)
    out["issues"] = _issues(row.get("issues"))
    summary = row.get("summary") or {}
    if isinstance(summary, dict):
        out["summary"] = {**_fields(summary, ("title", "status", "partial", "held_bullet_count", "basis")),
            "bullets": _strings(summary.get("bullets")),
            "evidence": _objects(summary.get("evidence"), lambda e: {
                **_fields(e, ("bullet",)), "sentence_ids": _strings(e.get("sentence_ids"))})}
    return out
