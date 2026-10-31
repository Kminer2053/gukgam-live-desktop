"""같은 원자막의 미확정 옛 판본이 중복 저장된 경우에만 읽기 소속을 정리한다."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re


_PIECE = re.compile(r"^s(?P<segment>\d+)(?:\.(?P<occurrence>\d+))?~(?P<part>\d+)$")
METHOD = "same_source_revision_replacement_v1"


def _protected(row: dict) -> bool:
    return bool(row.get("speaker_source") == "manual" or row.get("review_parts")
                or row.get("edited") or row.get("edited_by")
                or not isinstance(row.get("original_text"), str)
                or row.get("text") != row.get("original_text"))


def _source_key(segment: str, offset) -> str:
    value = json.dumps([str(segment), str(offset)], ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def resolve_superseded_occurrences(utterances: list[dict], segments: list[dict], sources: list[dict],
                                  mapping: dict, *, blocks: list[dict] | tuple = (),
                                  source_revisions: list[dict] | tuple = ()) -> dict:
    """과거 수신 판본과 최신 행의 완전한 대체를 입증한 옛 행만 빈 범위로 반환한다.

    저장 행·원문·신호는 바꾸지 않는다. 한 옛 행의 한 원조각에만 한정하며, 최신
    source의 고유성·같은 질의·과거 payload의 실제 일치·같은 offset·최신 행 전체
    문자 대조를 모두 요구한다. 불완전한 증거는 기존 실패를 그대로 둔다.
    """
    from .block_sources import _chars, _compact

    by_id = {row["id"]: row for row in utterances}
    raw_by_key = {row["source_key"]: row for row in segments}
    source_by_id = {source["id"]: source for source in sources}
    block_by_id = {block["id"]: block for block in blocks}
    result, evidence = mapping, []
    for uid, reason in mapping.get("failures", {}).items():
        if reason != "source_revision_unmatched":
            continue
        old = by_id.get(uid)
        if not old or _protected(old) or not old.get("is_final"):
            continue
        block = block_by_id.get(old.get("block_id"))
        summary = (block or {}).get("summary")
        if (not block or block.get("kind") != "question" or block.get("is_open")
                or block.get("review_status") == "reviewed"
                or isinstance(summary, dict) and summary.get("edited_by")):
            continue
        base = str(old.get("source_key", "")).rsplit("#", 1)[0]
        siblings = [row for row in utterances if str(row.get("source_key", "")).rsplit("#", 1)[0] == base]
        parts = (raw_by_key.get(base) or {}).get("raw", {}).get("segments", {})
        if len(siblings) != 1 or not isinstance(parts, dict) or len(parts) != 1:
            continue
        piece_key, fragment = next(iter(parts.items()))
        piece = _PIECE.fullmatch(piece_key)
        if (not piece or int(piece["occurrence"] or 0) != 0 or int(piece["part"]) != 0
                or not isinstance(fragment, str) or not _compact(fragment)
                or _compact(old["original_text"]) != _compact(fragment)):
            continue
        candidates = [source for source in sources if str(source.get("segment")) == piece["segment"]]
        if len(candidates) != 1:
            continue
        source = candidates[0]
        offset = source.get("provider_offset")
        if (offset is None or source.get("source_key") != _source_key(piece["segment"], offset)
                or not source.get("final") or not source.get("scd") or source.get("replayed") is True):
            continue
        historical = []
        for revision in source_revisions:
            payload = revision.get("payload")
            if (revision.get("source_id") != source["id"] or type(revision.get("revision")) is not int
                    or not 0 < revision["revision"] < source.get("revision", 0)
                    or not isinstance(payload, dict) or not isinstance(payload.get("text"), str)
                    or payload.get("final") is not False or payload.get("scd") is not False
                    or str(payload.get("provider_offset")) != str(offset)
                    or _compact(payload["text"]) != _compact(fragment)):
                continue
            historical.append(revision)
        if not historical:
            continue
        history = max(historical, key=lambda item: item["revision"])
        replacements, covered, invalid = [], set(), False
        for row in utterances:
            spans = mapping.get("spans", {}).get(row["id"], [])
            relevant = [span for span in spans if span["source_id"] == source["id"]]
            if not relevant:
                continue
            raw = raw_by_key.get(str(row.get("source_key", "")).rsplit("#", 1)[0]) or {}
            own_parts = raw.get("raw", {}).get("segments", {})
            own = [_PIECE.fullmatch(key) for key in own_parts]
            actual = [match for match in own if match and match["segment"] == piece["segment"]]
            if (row.get("block_id") != block["id"] or _protected(row) or not row.get("is_final")
                    or row["id"] in mapping.get("failures", {}) or row.get("seq", row["id"]) <= old.get("seq", old["id"])
                    or not actual or any(int(match["occurrence"] or 0) <= 0 for match in actual)):
                invalid = True
                break
            located, revisions = [], {}
            for span in spans:
                linked = source_by_id.get(span.get("source_id"))
                if (not linked or type(span.get("start")) is not int or type(span.get("end")) is not int
                        or not 0 <= span["start"] < span["end"] <= len(linked.get("text", ""))):
                    invalid = True
                    break
                located.append(linked["text"][span["start"]:span["end"]])
                revisions[str(linked["id"])] = linked["revision"]
            if invalid or _compact("".join(located)) != _compact(row["original_text"]):
                invalid = True
                break
            needed = {position for _, position in _chars(source["text"])}
            positions = {position for span in relevant for position in range(span["start"], span["end"])} & needed
            if covered & positions:
                invalid = True
                break
            covered.update(positions)
            replacements.append({"utterance_id": row["id"], "source_key": row["source_key"],
                "block_id": row["block_id"], "original_text": row["original_text"],
                "source_spans": deepcopy(spans), "source_revisions": revisions})
        needed = {position for _, position in _chars(source["text"])}
        if invalid or not replacements or covered != needed:
            continue
        if result is mapping:
            result = deepcopy(mapping)
        result["spans"][uid] = []
        result["failures"].pop(uid)
        evidence.append({"method": METHOD, "utterance_id": uid, "source_key": old["source_key"],
            "block_id": block["id"], "original_text": old["original_text"],
            "replacement_utterances": replacements,
            "sources": [{"source_id": source["id"], "source_key": source["source_key"],
                "segment": str(source["segment"]), "revision": source["revision"],
                "historical_revision": history["revision"], "historical_text": history["payload"]["text"]}]})
    if evidence:
        result["superseded"] = [*deepcopy(mapping.get("superseded", [])), *evidence]
    return result
