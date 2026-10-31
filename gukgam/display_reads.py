"""저장된 공통 표시본의 페이지 선택. 화자 판정·문자 연결·현재성 재검사는 수행하지 않는다."""
from __future__ import annotations

from copy import deepcopy


def enabled(repo, sid: str) -> bool:
    return bool(hasattr(repo, "display_initialized") and repo.display_initialized(sid))


def stage_page(payload: dict, *, before=None, limit=100, block_id=None) -> dict:
    sources = payload.get("sources", [])
    page = [row for row in sources if before is None or row["id"] < before][-limit:] if block_id is None else []
    page_ids = {row["id"] for row in page}
    documents = [row for row in payload.get("documents", []) if (
        row.get("block_id") == block_id if block_id is not None
        else page_ids.intersection(row.get("source_ids", [])))]
    selected = {row["block_id"] for row in documents}
    if block_id is not None:
        selected.add(block_id)
    turns = [row for row in payload.get("live_turns", []) if row.get("block_id") in selected
             or page_ids.intersection(row.get("source_ids", []))]
    projections = [row for row in payload.get("display_blocks", []) if row.get("blockId") in selected]
    needed = set(page_ids)
    for row in [*documents, *turns]:
        needed.update(row.get("source_ids", []))
        needed.update(int(uid) for uid in (row.get("source_revisions") or {}) if str(uid).isdigit())
        needed.update(int(uid) for uid in ((row.get("speaker_guess") or {}).get("source_revisions") or {}) if str(uid).isdigit())
    for row in sources:
        if row["id"] in needed:
            needed.update(int(uid) for uid in ((row.get("speaker_guess") or {}).get("source_revisions") or {}) if str(uid).isdigit())
    uids = {uid for row in turns for uid in row.get("utterance_ids", [row.get("id")])}
    for row in projections:
        uids.update(int(uid) for uid in row.get("consumed_utterance_ranges", {}))
        uids.update(uid for card in row.get("cards", []) for uid in card.get("source_utterance_ids", []))
    rows = [row for row in payload.get("utterances", []) if row.get("block_id") in selected or row["id"] in uids]
    display_rows = [row for row in payload.get("display_rows", []) if row.get("block_id") in selected
                    or page_ids.intersection(row.get("source_ids", []))
                    or set(row.get("source_utterance_ids", [])).intersection(uids)]
    selected_arrays = {"sources", "documents", "live_turns", "utterances", "display_blocks", "display_rows"}
    metadata = {key: deepcopy(value) for key, value in payload.items() if key not in selected_arrays}
    if "display_utterance_ids" in metadata:
        metadata["display_utterance_ids"] = [uid for uid in metadata["display_utterance_ids"] if uid in uids]
    return {**metadata, "sources": [deepcopy(row) for row in sources if row["id"] in needed],
            "documents": deepcopy(documents), "live_turns": deepcopy(turns), "utterances": deepcopy(rows),
            "display_blocks": deepcopy(projections), "display_rows": deepcopy(display_rows),
            "next_before": page[0]["id"] if block_id is None and len(page) == limit else None,
            **({"block_id": block_id} if block_id is not None else {})}


def display_utterances(payload: dict, sid: str) -> list[dict]:
    """저장 표시행에 상세 링크용 원행 메타만 얹는다. 본문·역할·블록은 표시본을 따른다."""
    originals = {row["id"]: row for row in payload.get("utterances", [])}
    result = []
    for index, row in enumerate(payload.get("display_rows", [])):
        ids = row.get("source_utterance_ids") or ([row["utterance_id"]] if row.get("utterance_id") else [])
        uid = ids[0] if ids else None
        original = originals.get(uid, {})
        result.append({**original, **row, "id": uid, "session_id": sid,
                       "seq": original.get("seq", index),
                       "original_text": (row.get("text", "") if row.get("stage") == 3
                                         else original.get("original_text", row.get("text", ""))),
                       "display_key": row.get("key"), "display_stage": row.get("stage"),
                       "source_utterance_ids": ids, "is_final": original.get("is_final", row.get("stage") == 3)})
    return result


def utterance_page(repo, payload, sid, *, limit=100, before_id=None, block_id=None,
                   around_id=None, bookmark_actor=None, query="", selected_ids=None):
    """저장한 원행 목록에 페이지 조건만 적용한다. 보정 검색도 저장 표시행을 사용한다."""
    rows = payload.get("utterances", [])
    by_id = {row["id"]: row for row in rows}
    scope = "tail"
    wanted = None
    if around_id is not None:
        anchor = by_id.get(around_id)
        if not anchor:
            raise KeyError(around_id)
        block_id = anchor.get("block_id")
        scope = "around"
        if block_id is None:
            wanted = {around_id}
    if selected_ids is not None:
        wanted, scope = set(selected_ids), "ids"
    elif block_id is not None:
        wanted = {row["id"] for row in rows if row.get("block_id") == block_id}
        wanted.update(uid for row in payload.get("display_rows", []) if row.get("block_id") == block_id
                      for uid in row.get("source_utterance_ids", []))
        scope = "around" if scope == "around" else "block"
    elif bookmark_actor is not None:
        wanted = {row["utterance_id"] for row in repo.list_bookmarks(sid, bookmark_actor)}
        scope = "bookmarks"
    elif query:
        terms = query.casefold().split()[:12]
        wanted = {uid for row in payload.get("display_rows", []) if all(
            term in (row.get("text", "") + " " + row.get("speaker", "")).casefold() for term in terms)
            for uid in row.get("source_utterance_ids", [])}
        scope = "search"
    elif before_id is not None:
        if before_id not in by_id:
            raise KeyError(before_id)
        rows = rows[:next(index for index, row in enumerate(rows) if row["id"] == before_id)]
        scope = "before"
    if wanted is not None:
        rows = [row for row in rows if row["id"] in wanted]
    complete = scope in {"around", "block", "bookmarks", "search", "ids"}
    more = not complete and len(rows) > limit
    if more:
        rows = rows[-limit:]
    return rows, {"partial": True, "scope": scope, "next_before": rows[0]["id"] if more else None}


def search(repo, q, *, session_id=None, speaker=None, limit=50, offset=0):
    """국회 자막은 공통 표시본, 그 외 수집원은 기존 저장 발언을 검색한다."""
    sessions = [repo.get_session(session_id)] if session_id else repo.list_sessions(limit=100000)
    managed = [row for row in sessions if row and enabled(repo, row["id"])]
    if not managed:
        return repo.search_utterances(q, session_id=session_id, speaker=speaker, limit=limit, offset=offset)
    managed_ids = {row["id"] for row in managed}
    tokens = q.casefold().split()
    found = []
    for session in managed:
        payload = repo.display_snapshot(session["id"])
        for row in display_utterances(payload, session["id"]):
            if speaker and row.get("speaker") != speaker:
                continue
            if all(token in (row.get("text", "") + " " + row.get("speaker", "")).casefold() for token in tokens):
                found.append({**row, "session_title": session["title"], "meeting_date": session.get("meeting_date", "")})
    # 별도 수집원의 기존 검색 계약은 유지하며 자막 원행의 중복 검색 결과만 제외한다.
    if session_id is None:
        for session in sessions:
            if session and session["id"] not in managed_ids:
                found.extend(repo.search_utterances(q, session_id=session["id"], speaker=speaker,
                                                   limit=limit + offset, offset=0))
    return sorted(found, key=lambda row: (row.get("ts_start", 0), str(row.get("display_key", row["id"]))), reverse=True)[offset:offset+limit]
