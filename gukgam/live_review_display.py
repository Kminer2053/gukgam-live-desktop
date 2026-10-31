"""진행 중 질의의 기존 창 검토를 원문과 분리한 표시 초안으로 검증한다."""
from __future__ import annotations

import json
from typing import Any

from . import serialize
from .transcript_review import snapshot_hash, take_window_snapshot, validate_result, utterance_order

MAX_LIVE_DISPLAY_BYTES = 512 * 1024


def _locked(row: dict) -> bool:
    """사람이 확정한 원문은 자동 표시로도 가리지 않는다."""
    return bool(row.get("speaker_source") == "manual" or row.get("edited") or row.get("edited_by")
                or row.get("speaker_role") == "system"
                or row.get("original_text") is not None and row.get("text") != row.get("original_text"))


def get_live_review_display(repo: Any, block: dict, *, include_closed: bool = False,
                            limit_output: bool = True) -> dict | None:
    """현재 유효 창을 검증한다. 내부 최종 입력은 공개 전송 한도로 잘라내지 않는다."""
    sid, bid = block["session_id"], block["id"]
    current = repo.get_block(bid)
    if (not current or current.get("session_id") != sid or current.get("kind") != "question"
            or (not include_closed and not current.get("is_open")) or current.get("review_status") == "reviewed"
            or isinstance(current.get("summary"), dict) and current["summary"].get("edited_by")):
        return None
    rows = repo.list_utterances(sid, block_id=bid, final_only=True, limit=-1)
    by_id = {row["id"]: row for row in rows}
    positions = {row["id"]: index for index, row in enumerate(rows)}
    claimed: set[int] = set()
    windows = []
    used_bytes = 0
    select = getattr(repo, "latest_window_transcript_reviews", repo.list_transcript_reviews)
    for candidate in select(sid, bid):
        payload = candidate.get("payload")
        scope = payload.get("scope") if isinstance(payload, dict) else None
        if not isinstance(scope, dict) or scope.get("kind") != "window":
            continue
        ids = scope.get("utterance_ids")
        radius = scope.get("context_radius", 1)
        if (set(scope) - {"kind", "utterance_ids", "context_ids", "context_radius"} or not isinstance(ids, list)
                or not 1 <= len(ids) <= 6 or any(type(uid) is not int or uid < 1 for uid in ids)
                or len(set(ids)) != len(ids) or not isinstance(scope.get("context_ids", []), list)):
            return None  # 손상된 최신 범위에서 오래된 판정을 되살리지 않는다.
        overlap = claimed.intersection(ids)
        claimed.update(ids)
        if overlap or candidate.get("status") != "draft":
            continue
        try:
            snapshot = take_window_snapshot(repo, sid, bid, ids, context_ids=scope.get("context_ids", []),
                                             context_radius=radius)
            digest = snapshot_hash(snapshot)
            if candidate.get("input_hash") != digest or payload.get("input_hash") != digest:
                continue
            references = snapshot["utterances"] + snapshot.get("context_utterances", [])
            reference_ids = {row["id"] for row in references}
            reference_rows = ({row["id"]: repo.get_utterance(row["id"]) for row in references}
                              if radius in {2, 3} else by_id)
            if (any(not reference_rows.get(uid) for uid in reference_ids)
                    or any(_locked(by_id[uid]) for uid in ids)
                    or any(reference_rows[uid].get("review_parts") for uid in (ids if radius in {2, 3} else reference_ids))):
                continue
            turns = validate_result(snapshot, payload.get("turns"))
            parts = []
            for part in turns:
                uid = part["utterance_id"]
                exported = {key: part[key] for key in ("utterance_id", "start", "end", "role", "speaker")}
                exported["text"] = by_id[uid]["text"][part["start"]:part["end"]]
                if part.get("group_id"):
                    exported["group_id"] = f"r{candidate['id']}:{part['group_id']}"
                parts.append(exported)
            window = {"id": candidate["id"], "input_hash": digest, "utterance_ids": list(ids),
                "context_ids": list(snapshot["scope"]["context_ids"]), "parts": parts,
                "source_rows": [serialize.utterance(reference_rows[row["id"]]) for row in sorted(
                    references, key=utterance_order)]}
            if radius in {2, 3}:
                window["context_radius"] = radius
                correction = payload.get("context_correction")
                if isinstance(correction, dict) and correction.get("schema_version") == 1:
                    from .context_window_correction import validate_context_correction
                    window["context_correction"] = validate_context_correction(snapshot, correction, turns)
            size = len(json.dumps(window, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if limit_output and used_bytes + size > MAX_LIVE_DISPLAY_BYTES - 128:
                continue  # 오래된 창이 많아도 동기화 본문 한도를 침범하지 않는다.
            windows.append(window)
            used_bytes += size + 1
        except (KeyError, TypeError, ValueError):
            continue
    if not windows:
        return None
    windows.sort(key=lambda window: positions[window["utterance_ids"][0]])
    return {"schema_version": 1, "status": "draft", "windows": windows}
