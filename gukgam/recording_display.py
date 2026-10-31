"""녹음 원발언을 별도 출처로 유지하며 검증된 종료 보정본만 표시한다."""
from __future__ import annotations

from copy import deepcopy

from . import serialize
from .db import now_ms
from .recording_sources import recording_sources
from .stage_mirror import public_source


SOURCE_KIND = "recording_asr"


def _turn(row, source):
    """표시용 말묶음은 원발언 ID와 원문 문자 좌표를 그대로 사용한다."""
    from .display_materializer import _protected

    value = {**deepcopy(row), "source_kind": SOURCE_KIND,
             "origin_utterance_id": row["id"],
             "protected": bool(_protected(row) or source and source.get("protected")),
             "source_ids": [], "source_revisions": {}, "source_spans": []}
    if source is not None:
        value.update(original_text=source["text"], source_ids=[source["id"]],
                     source_revisions={str(source["id"]): source["revision"]},
                     source_hashes={str(source["id"]): source["source_hash"]})
        if source["text"]:
            value["source_spans"] = [{"source_id": source["id"], "start": 0,
                                      "end": len(source["text"])}]
    return value


def _sources(rows):
    """출처의 공개 허용 필드만 내보내며 원음의 익명 ID도 함께 보존한다."""
    result = []
    for source in recording_sources(rows):
        value = public_source(source)
        for key in ("source_kind", "source_hash", "origin_utterance_id", "origin_seq",
                    "ts_start", "ts_end", "is_final", "protected"):
            if key in source:
                value[key] = source[key]
        value.update(serialize.acoustic_identity(source))
        result.append(value)
    return result


def build_recording_display(repo, sid, generation, *, previous_cache=None, changes=None,
                            previous_payload=None):
    """모델·자막 저장소 조회 없이 같은 읽기 스냅샷으로 본문과 요약을 선택한다."""
    from .display_materializer import (_display_rows, _hash, _json, _public_document,
                                       closed_block_projection)

    rows = deepcopy(repo.list_utterances(sid, limit=-1))
    sources = _sources(rows)
    source_map = {source["id"]: source for source in sources}
    turns = [_turn(row, source_map.get(row["id"])) for row in rows
             if row.get("speaker_role") != "system"]
    blocks = deepcopy(repo.display_build_blocks(sid))
    by_block = {block["id"]: block for block in blocks}
    valid, cache = [], {}
    # 녹음은 종료된 입력에 대한 명시 처리다. 캐시의 과거 valid 값을 재사용하지 않고
    # 원문 판본·보호 상태를 현재 읽기 트랜잭션에서 매번 다시 확인한다.
    for document in repo.list_stage_documents(sid):
        if (document.get("source_kind") != SOURCE_KIND
                or document.get("session_id", sid) != sid
                or document.get("mode") != "closed_block_v1"
                or document.get("status") == "stale"):
            continue
        did = document["id"]
        current = bool(repo.stage_document_current(sid, did))
        public = _public_document(document)
        public["source_kind"] = SOURCE_KIND
        block = by_block.get(document.get("block_id")) or {}
        projection = (closed_block_projection(block, public, sources, turns, rows)
                      if current and not block.get("edited_by") else None)
        cache[did] = {"document_hash": _hash(document), "valid": current,
                      "projectable": projection is not None,
                      "dependency_source_ids": list(document.get("source_ids") or []),
                      "segment_keys": []}
        if current:
            valid.append(public)
        cache[did]["projection"] = projection

    latest = {}
    for document in valid:
        bid = document["block_id"]
        stamp = lambda value: (value.get("updated_at") or value.get("created_at") or 0, value["id"])
        if bid not in latest or stamp(document) > stamp(latest[bid]):
            latest[bid] = document
    projections, used = [], {}
    for document in sorted(latest.values(), key=lambda value: value["id"], reverse=True):
        projection = cache[document["id"]]["projection"]
        if projection is None:
            continue
        ranges = projection["consumed_utterance_ranges"]
        if any(any(max(a["start"], b["start"]) < min(a["end"], b["end"])
                   for a in spans for b in used.get(uid, [])) for uid, spans in ranges.items()):
            continue
        if (document.get("summary") or {}).get("status") != "draft":
            projection["summary"] = None
        for card in projection["cards"]:
            card["source_kind"] = SOURCE_KIND
        projections.append(projection)
        for uid, spans in ranges.items():
            used.setdefault(uid, []).extend(spans)
    projections.sort(key=lambda value: (by_block[value["blockId"]]["ts_start"], value["blockId"]))
    selected = {value["blockId"]: value["document"] for value in projections}
    for block in blocks:
        summary = block.get("summary")
        if (block.get("review_status") == "reviewed" or block.get("edited_by")
                or isinstance(summary, dict) and summary.get("edited_by")):
            continue
        document = selected.get(block["id"])
        if document and (document.get("summary") or {}).get("status") == "draft":
            block["summary"] = repo.stage_summary_value(document, summary if isinstance(summary, dict) else {})
        else:
            block["summary"] = None
            block["summary_at"] = None

    display_rows = _display_rows(sid, rows, sources, turns, projections, {})
    stage2_display = _display_rows(sid, rows, sources, turns, [], {})
    for row in [*display_rows, *stage2_display]:
        row["source_kind"] = SOURCE_KIND
    payload = {"version": 3, "mode": "closed_block_v1", "source_kind": SOURCE_KIND,
               "snapshot_at": now_ms(), "display_generation": generation,
               "sources": sources, "live_turns": turns, "documents": valid,
               "valid_document_ids": sorted(value["id"] for value in valid),
               "valid_live_turn_ids": sorted(value["id"] for value in turns),
               "utterances": rows, "blocks": [serialize.block(block) for block in blocks],
               "display_blocks": projections, "display_rows": display_rows,
               "display_utterance_ids": sorted(row["id"] for row in rows),
               "display_valid_block_ids": sorted(value["blockId"] for value in projections),
               "display_valid_row_ids": [row["key"] for row in display_rows], "next_before": None}
    stage2 = []
    for kind, values in (("live_turn", turns),
                         ("display_row", [row for row in stage2_display if row["stage"] == 2])):
        for value in values:
            stage2.append({"kind": kind, "row_key": str(value["id"]), "payload": _json(value),
                           "fingerprint": _hash(value)})
    compact_cache = {did: _json({key: value for key, value in item.items() if key != "projection"})
                     for did, item in cache.items()}
    return payload, stage2, compact_cache
