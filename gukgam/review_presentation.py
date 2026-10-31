"""검증한 화자 초안을 원문 연결이 남는 표시 카드와 요약 입력으로 조립한다."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from .nlp.readability import apply_edits, format_text


def build_presentation(snapshot: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, Any]:
    """원문을 재작성하지 않고 같은 검토 그룹의 인접 부분만 이어 표시한다.

    카드의 ``source_text``와 ``source_rows``는 원문 부분을 정확히 보존한다.
    ``text``는 다른 UID 사이에만 표시용 공백을 더하며 그 위치를 ``joins``에 남긴다.
    ``readable_text``는 ``text``에 부호·개행만 더한 ``readability.edits``로 재현한다.
    """
    # 검토 엔진에서도 이 조립기를 사용할 수 있도록 실제 호출 때만 가져온다.
    from .transcript_review import _protected, validate_result

    checked = validate_result(snapshot, turns)
    rows = {row["id"]: row for row in snapshot["utterances"]}
    bid = snapshot["block"].get("id")
    cards: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    previous: dict[str, Any] | None = None
    for part in checked:
        uid, start, end = part["utterance_id"], part["start"], part["end"]
        row = rows[uid]
        text = (row.get("text") or "")[start:end]
        protected = _protected(snapshot, row)
        gid = part.get("group_id")
        if gid and not text:
            raise ValueError("빈 원문은 연속 표시 그룹으로 묶을 수 없습니다")
        if row.get("block_id") is not None and bid is not None and row["block_id"] != bid:
            raise ValueError("다른 질의 블록의 원문은 함께 조립할 수 없습니다")
        source = {"id": uid, "utterance_id": uid, "block_id": row.get("block_id", bid),
                  "text": text, "speaker": part["speaker"], "speaker_role": part["role"],
                  "start": start, "end": end, "source_span": {"uid": uid, "start": start, "end": end}}
        for field in ("ts_start", "ts_end", "seq"):
            if field in row:
                source[field] = deepcopy(row[field])
        source_rows.append(source)
        join = bool(gid and previous and previous.get("group_id") == gid
                    and not protected and not cards[-1]["protected"]
                    and cards[-1]["speaker_role"] == part["role"]
                    and cards[-1]["speaker"] == part["speaker"])
        if not join:
            cards.append({"id": f"ai-u{uid}-{start}-{end}", "text": "", "source_text": "",
                          "speaker": part["speaker"], "speaker_role": part["role"],
                          "block_id": source["block_id"], "group_id": gid,
                          "source_utterance_ids": [], "source_spans": [], "joins": [], "join_separator": " ",
                          "protected": protected, "manual_protected": protected,
                          "status": "AI 정리 초안", "timing_estimated": False})
        card = cards[-1]
        if join and previous["utterance_id"] != uid:
            card["joins"].append({"offset": len(card["text"]), "insert": card["join_separator"],
                                  "left_uid": previous["utterance_id"], "right_uid": uid})
            card["text"] += card["join_separator"]
        card["text"] += text
        card["source_text"] += text
        if uid not in card["source_utterance_ids"]:
            card["source_utterance_ids"].append(uid)
        card["source_spans"].append(deepcopy(source["source_span"]))
        card["timing_estimated"] |= bool(start or end != len(row.get("text") or ""))
        if "ts_start" in source:
            card.setdefault("ts_start", source["ts_start"])
        if "ts_end" in source:
            card["ts_end"] = source["ts_end"]
        counts["joined_boundaries"] += int(join)
        previous = part

    for card in cards:
        readable, metadata = format_text(card["text"], system=card["protected"])
        metadata["protected_preserved"] = card["protected"]
        metadata["system_preserved"] = card["speaker_role"] == "system"
        if apply_edits(card["text"], metadata["edits"]) != readable:
            raise ValueError("표시 본문과 원문 삽입 이력이 다릅니다")
        card["readable_text"], card["readability"] = readable, metadata
        counts["source_chars"] += len(card["source_text"])
        counts["join_separator_insertions"] += len(card["joins"])
        counts["display_chars"] += len(readable)
        counts["protected_cards"] += int(card["protected"])
        for key in ("punctuation_insertions", "paragraph_break_insertions", "sentence_count", "paragraph_count"):
            counts[key] += metadata[key]

    spans = [span for card in cards for span in card["source_spans"]]
    expected = [{"uid": p["utterance_id"], "start": p["start"], "end": p["end"]} for p in checked]
    if spans != expected or "".join(card["source_text"] for card in cards) != "".join(row["text"] for row in source_rows):
        raise ValueError("표시 카드와 요약 입력의 원문 범위가 다릅니다")
    return {"cards": cards, "source_rows": source_rows,
            "readability_report": {"status": "AI 정리 초안", "format_version": 1,
                "source_utterances": len(rows), "source_fragments": len(source_rows), "cards": len(cards),
                **{key: counts[key] for key in ("source_chars", "display_chars", "protected_cards",
                    "joined_boundaries", "join_separator_insertions", "punctuation_insertions", "paragraph_break_insertions",
                    "sentence_count", "paragraph_count")},
                "unchanged_content": True, "source_links_complete": True}}
