"""DB 행 → API/SSE 공개 JSON. 화면(JS)과의 계약이므로 필드명을 바꾸면 static/js 도 함께 바꾼다."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from .config import KST
from .models import (BLOCK_STATUS_LABELS, ITEM_KIND_LABELS, ITEM_STATUS_LABELS, ROLE_LABELS, SOURCE_LABELS,
                     STATUS_LABELS)


def hms(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%H:%M:%S")


def ymd_hm(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%d %H:%M")


def acoustic_identity(row: dict[str, Any]) -> dict[str, Any]:
    """음성의 익명 ID를 이름·역할과 별도로 표시한다. 자막의 hwa ID와 혼동하지 않는다."""
    hint = row.get("source_speaker_hint") or row.get("speaker_hint") or ""
    if not hint and isinstance(row.get("acoustic_speaker_id"), str):
        hint = "hwa:" + row["acoustic_speaker_id"]
    if not hint and row.get("acoustic_overlap") is True:
        hint = "nemo:overlap"
    if not isinstance(hint, str):
        return {}
    if hint == "nemo:overlap":
        return {"acoustic_overlap": True, "acoustic_speaker_label": "겹친 발언 · 화자 확인 필요"}
    if hint == "nemo:unknown" or not hint and row.get("acoustic_speaker_label") == "화자 미정":
        return {"acoustic_speaker_label": "화자 미정", "acoustic_overlap": False}
    match = re.fullmatch(r"hwa:nemo:(?:(e[1-9][0-9]{0,5}):)?([0-7])", hint)
    if match:
        scope, number = match.group(1), int(match.group(2))
        label = f"화자 {number + 1}"
        if scope and scope != "e1":
            label += f" (구간 {int(scope[1:])})"
        return {"acoustic_speaker_id": f"nemo:{scope + ':' if scope else ''}{number}",
                "acoustic_speaker_label": label, "acoustic_overlap": False}
    return {}


def utterance(u: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": u["id"],
        "session_id": u["session_id"],
        "seq": u["seq"],
        "block_id": u.get("block_id"),
        "ts_start": u["ts_start"],
        "ts_end": u.get("ts_end"),
        "time": hms(u["ts_start"]),
        "speaker": u.get("speaker") or "",
        "speaker_role": u.get("speaker_role") or "unknown",
        "role_label": ROLE_LABELS.get(u.get("speaker_role") or "unknown", ""),
        "speaker_source": u.get("speaker_source") or "inferred",
        "text": u.get("text") or "",
        "edited": (u.get("text") or "") != (u.get("original_text") or ""),
        "is_final": bool(u.get("is_final")),
        "mentions": u.get("mentions") or [],
    }
    out.update(acoustic_identity(u))
    # 선택 메타데이터가 없는 기존 로컬 응답은 같은 모양을 유지한다.
    for key in ("source_order", "turn_id", "boundary_reason"):
        if u.get(key):
            out[key] = u[key]
    if u.get("review_parts"):
        out["review_parts"] = [{**part, "text": out["text"][part["start"]:part["end"]]}
                               for part in u["review_parts"]]
    for key in ("display_key", "display_stage", "source_utterance_ids"):
        if key in u:
            out[key] = u[key]
    for key, allowed in (("document_status", {"draft", "partial", "ready"}),
                         ("text_status", {"draft", "held", "pending", "unknown"})):
        if isinstance(u.get(key), str) and u[key] in allowed:
            out[key] = u[key]
    for key in ("text_uncertain", "role_uncertain"):
        if type(u.get(key)) is bool:
            out[key] = u[key]
    if "display_stage" in u:
        # 문자 범위로 고른 표시행의 길이 차이를 사람 수정으로 오인하지 않는다.
        out["edited"] = bool(u.get("edited"))
    return out


def block(b: dict[str, Any]) -> dict[str, Any]:
    kind = b["kind"]
    if kind == "question":
        if b.get("questioner"):
            party = f"({b['questioner_party']})" if b.get("questioner_party") else ""
            title = f"{b['questioner']} 위원{party} 질의"
        else:
            title = "※성명 확인 필요 위원 질의"
    elif kind == "chair":
        title = "위원장 진행"
    else:
        title = "기타 발언"
    out = {
        "id": b["id"],
        "session_id": b["session_id"],
        "kind": kind,
        "title": title,
        "questioner": b.get("questioner") or "",
        "questioner_party": b.get("questioner_party") or "",
        "status": b.get("status") or "ai_inferred",
        "status_label": BLOCK_STATUS_LABELS.get(b.get("status") or "", ""),
        "name_basis": b.get("name_basis") or "",
        "ts_start": b["ts_start"],
        "ts_end": b["ts_end"],
        "time_range": f"{hms(b['ts_start'])}–{hms(b['ts_end'])}",
        "duration_sec": max(0, (b["ts_end"] - b["ts_start"]) // 1000),
        "is_open": bool(b.get("is_open")),
        "summary": b.get("summary") or None,
        "summary_at": b.get("summary_at"),
        "review_status": b.get("review_status") or "draft",
        "reviewed_by": b.get("reviewed_by") or "",
        "reviewed_at": b.get("reviewed_at"),
    }
    if b.get("ai_review"):
        out["ai_review"] = b["ai_review"]
    if b.get("ai_live_review"):
        out["ai_live_review"] = b["ai_live_review"]
    return out


def item(i: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": i["id"],
        "session_id": i["session_id"],
        "session_title": i.get("session_title"),
        "block_id": i.get("block_id"),
        "utterance_id": i.get("utterance_id"),
        "time": hms(i.get("utt_ts")) if i.get("utt_ts") else "",
        "kind": i["kind"],
        "kind_label": ITEM_KIND_LABELS.get(i["kind"], i["kind"]),
        "strength": i.get("strength") or "",
        "text": i["text"],
        "quote": i.get("quote") or "",
        "speaker": i.get("speaker") or "",
        "requester": i.get("requester") or "",
        "assignee_dept": i.get("assignee_dept") or "",
        "due_date": i.get("due_date") or "",
        "status": i.get("status") or "open",
        "status_label": ITEM_STATUS_LABELS.get(i.get("status") or "open", ""),
        "origin": i.get("origin") or "rule",
        "confidence": i.get("confidence"),
        "note": i.get("note") or "",
        "edited": bool(i.get("edited")),   # 사람이 손댄 항목(자동 재추출·재요약이 덮어쓰지 않음)
        "updated_at": i.get("updated_at"),
    }


def alert(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": a["id"],
        "session_id": a["session_id"],
        "utterance_id": a["utterance_id"],
        "keyword": a["keyword"],
        "category": a.get("category") or "agency",
        "ack": bool(a.get("ack")),
        "speaker": a.get("speaker") or "",
        "text": a.get("text") or "",
        "time": hms(a.get("ts_start")),
    }


def session(s: dict[str, Any], stats: dict[str, Any] | None = None) -> dict[str, Any]:
    recording_replay = s["source_type"] == "assembly_caption" and (s.get("source_config") or {}).get("caption_replay") is True
    out = {
        "id": s["id"],
        "title": s["title"],
        "committee": s.get("committee") or "",
        "agencies": s.get("agencies") or "",
        "meeting_date": s.get("meeting_date") or "",
        "status": s["status"],
        "status_label": STATUS_LABELS.get(s["status"], s["status"]),
        "source_type": s["source_type"],
        "source_label": "저장 국회 자막 재생 · 실방송 아님" if recording_replay else SOURCE_LABELS.get(s["source_type"], s["source_type"]),
        "source_config": s.get("source_config") or {},
        "source_state": s.get("source_state") or {},
        "save_audio": bool(s.get("save_audio")),
        "started_at": s.get("started_at"),
        "ended_at": s.get("ended_at"),
        "created_at": s.get("created_at"),
        "notes": s.get("notes") or "",
    }
    if stats is not None:
        out["stats"] = stats
    if recording_replay:
        out["recording_replay"] = True
    return out


def summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "scope": row["scope"],
        "content": row["content"],
        "provider": row.get("provider") or "",
        "model": row.get("model") or "",
        "created_at": row["created_at"],
        "time": ymd_hm(row["created_at"]),
    }
