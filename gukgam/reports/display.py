"""보고서도 화면과 같은 저장 표시본을 읽는다. 조회 중 단계 선택은 하지 않는다."""
from __future__ import annotations

from copy import deepcopy

from .summary_status import correction_document_metadata

MAX_PUBLIC_INTEGER = 9_007_199_254_740_991


def _positive_id(value) -> bool:
    return type(value) is int and 0 < value <= MAX_PUBLIC_INTEGER


def summary_reference_fields(summary: dict) -> dict:
    """생성 문서 참조와 요약 항목 상태를 서로 바꾸지 않고 유한한 값만 옮긴다."""
    result = {}
    if _positive_id(summary.get("source_document_id")):
        result["source_document_id"] = summary["source_document_id"]
    if type(summary.get("partial")) is bool:
        result["partial"] = summary["partial"]
    count = summary.get("held_bullet_count")
    if type(count) is int and 0 <= count <= MAX_PUBLIC_INTEGER:
        result["held_bullet_count"] = count
    return result


def _correction_documents(saved: dict, sid: str, blocks: list[dict]) -> dict[int, dict]:
    """한 저장 판본에서 실제 채택된 카드의 상태만 센다. 원문·문서별 SQL은 읽지 않는다."""
    if saved.get("mode") != "closed_block_v1" or saved.get("session_id", sid) != sid:
        return {}
    valid = saved.get("valid_document_ids")
    projections = saved.get("display_blocks")
    if (not isinstance(valid, list) or not all(_positive_id(value) for value in valid)
            or len(set(valid)) != len(valid) or not isinstance(projections, list)):
        return {}
    by_block = {block["id"]: block for block in blocks if _positive_id(block.get("id"))}
    valid_ids, id_counts, block_counts = set(valid), {}, {}
    for projection in projections:
        if not isinstance(projection, dict):
            continue
        for value, counts in ((projection.get("id"), id_counts), (projection.get("blockId"), block_counts)):
            if _positive_id(value):
                counts[value] = counts.get(value, 0) + 1
    result = {}
    for projection in projections:
        if not isinstance(projection, dict):
            continue
        did, bid = projection.get("id"), projection.get("blockId")
        if (not _positive_id(did) or not _positive_id(bid) or did not in valid_ids
                or id_counts.get(did) != 1 or block_counts.get(bid) != 1):
            continue
        block = by_block.get(bid)
        document, projected_summary = projection.get("document"), projection.get("summary")
        if not block or not isinstance(document, dict) or not isinstance(projected_summary, dict):
            continue
        summary = block.get("summary")
        status = document.get("status")
        if (block.get("session_id") != sid or block.get("is_open") or block.get("review_status") == "reviewed"
                or not isinstance(summary, dict) or summary.get("provider") != "closed_block"
                or summary.get("edited_by") not in (None, "")
                or not _positive_id(summary.get("source_document_id")) or summary["source_document_id"] != did
                or not _positive_id(document.get("id")) or document["id"] != did
                or not _positive_id(document.get("block_id")) or document["block_id"] != bid
                or document.get("session_id", sid) != sid or projection.get("session_id", sid) != sid
                or document.get("mode") != "closed_block_v1" or not isinstance(status, str)
                or status not in {"draft", "partial", "ready"}
                or not isinstance(document.get("summary"), dict) or document["summary"].get("status") != "draft"
                or not _positive_id(projected_summary.get("source_document_id"))
                or projected_summary["source_document_id"] != did):
            continue
        cards = projection.get("cards")
        if (not isinstance(cards, list) or not cards or any(not isinstance(card, dict)
                or card.get("document_status") != status or not isinstance(card.get("text_status"), str)
                or card["text_status"] not in {"draft", "held", "pending", "unknown"}
                or type(card.get("text_uncertain")) is not bool or type(card.get("role_uncertain")) is not bool
                for card in cards)):
            continue
        # 사용되지 않은 과거 paragraph 대신 현재 표시되는 문단 카드와 같은 집계를 쓴다.
        metadata = correction_document_metadata({"document_id": did, "status": status, "paragraph_count": len(cards),
            "held_paragraph_count": sum(card["text_status"] == "held" for card in cards),
            "text_uncertain_paragraph_count": sum(card["text_uncertain"] for card in cards),
            "role_uncertain_paragraph_count": sum(card["role_uncertain"] for card in cards)})
        if metadata is not None:
            result[bid] = metadata
    return result


def stored_display(repo, sid: str) -> dict | None:
    read = getattr(repo, "display_snapshot", None)
    initialized = getattr(repo, "display_initialized", None)
    if not callable(read) or not callable(initialized) or not initialized(sid):
        # 새 계약이 없는 작은 모의 저장소·미전환 자료의 구 계약만 보존한다.
        return None
    return read(sid)


def report_display(repo, sid: str, *, snapshot: dict | None = None) -> dict:
    """저장된 표시 행·블록을 그대로 복사한다. dirty 예외를 숨기거나 재구성하지 않는다."""
    saved = stored_display(repo, sid) if snapshot is None else snapshot
    if saved is None:
        return {"blocks": repo.list_blocks(sid),
                "utts": repo.list_utterances(sid, final_only=True, limit=100000), "snapshot": None,
                "correction_documents": {}}
    blocks = deepcopy(saved.get("blocks", []))
    return {"blocks": blocks, "utts": deepcopy(saved.get("display_rows", [])), "snapshot": saved,
            "correction_documents": _correction_documents(saved, sid, blocks)}


def stored_summaries(display: dict, repo, sid: str, terms, fallback) -> dict:
    """공통 표시본의 요약을 사용하고 구 계약일 때만 예전 규칙 요약을 허용한다."""
    blocks = display["blocks"]
    if display["snapshot"] is None:
        return {row["id"]: fallback(repo, row, terms) for row in blocks if row.get("kind") == "question"}
    return {row["id"]: deepcopy(row.get("summary") or {}) for row in blocks if row.get("kind") == "question"}


def source_utterance_id(row: dict):
    """표시 카드의 문자열 키를 원발언 링크로 오인하지 않는다."""
    if type(row.get("utterance_id")) is int:
        return row["utterance_id"]
    refs = row.get("source_utterance_ids")
    if isinstance(refs, list):
        return next((uid for uid in refs if type(uid) is int and uid > 0), None)
    return row.get("id") if type(row.get("id")) is int else None


def utterance_blocks(rows: list[dict]) -> dict:
    """알림·후속조치의 기존 UID는 저장 표시 행의 출처 UID로 연결한다."""
    result = {}
    for row in rows:
        refs = row.get("source_utterance_ids")
        if not isinstance(refs, list):
            refs = [row.get("id")]
        for uid in refs:
            if type(uid) is int:
                result[uid] = row.get("block_id")
    return result
