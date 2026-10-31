"""기계 라벨 대응을 뒷받침한 원문 판본의 유한 참조."""
from __future__ import annotations

import hashlib
import json
from typing import Any


SOURCE_FIELDS = ("source_key", "source_order", "ts_start", "speaker_hint",
                 "source_speaker_hint", "new_speaker", "turn_id", "boundary_reason")


def revision_fingerprint(raw: Any) -> str:
    """본문을 덮지 않는 재분할 후보도 역할 근거 판본에 포함한다. 수신 시각은 제외한다."""
    revision = raw.get("speaker_revision") if isinstance(raw, dict) else None
    if revision is None:
        return ""
    if isinstance(revision, dict) and isinstance(revision.get("parts"), list):
        semantic = {"source_key": revision.get("source_key"), "parts": [
            {key: part.get(key) for key in ("piece_index", "text", "hwa", "new_speaker")}
            if isinstance(part, dict) else part for part in revision["parts"]]}
    else:
        semantic = revision  # 손상된 후보도 없었던 판본과 같은 것으로 취급하지 않는다.
    return hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def source_ref(row: dict[str, Any], block_id: int, role: str, *, revision_hash: str) -> dict[str, Any]:
    """출처·본문만 고정한다. 같은 본문의 종료 시각 확인은 새 역할 근거가 아니다."""
    return {"uid": row.get("id"), "block_id": block_id, "role": role,
            "text_hash": hashlib.sha256((row.get("text") or "").encode()).hexdigest(),
            "revision_hash": revision_hash,
            **{key: row.get(key) for key in SOURCE_FIELDS}}


def matches_source(ref: dict[str, Any], row: dict[str, Any] | None, *, revision_hash: str) -> bool:
    """현재 확정 행의 역할과 원문 판본이 근거와 같은지 확인한다."""
    return bool(row and row.get("id") == ref.get("uid") and row.get("block_id") == ref.get("block_id")
                and row.get("is_final") and not row.get("review_parts")
                and row.get("speaker_role") == ref.get("role")
                and ref.get("revision_hash") == revision_hash
                and all(key in ref and row.get(key) == ref[key] for key in SOURCE_FIELDS)
                and hashlib.sha256((row.get("text") or "").encode()).hexdigest() == ref.get("text_hash"))
