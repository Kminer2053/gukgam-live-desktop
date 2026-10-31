"""혼합 부모 문장의 exact-copy 역할 부분을 검증하고 원문 좌표로만 나눈다."""
from __future__ import annotations

from copy import deepcopy
import re

from .nlp.floor import is_reported_anchor
from .nlp.role_evidence import _self_dialogue_before
from .stage2_sentences import _protected

ROLES = {"member", "answer", "chair", "other"}
MAX_PARTS = 6
MAX_ROLE_UNITS = 256
UNRESOLVED_SPEAKER = "혼합 발언 · 화자 확인 필요"


def _display(text):
    return re.sub(r"\s+", " ", re.sub(r"(?m)^[ \t]*-[ \t]+", "", text)).strip()


def _coordinates(row, sources):
    original, spans = row.get("original_text"), row.get("source_spans")
    if not isinstance(original, str) or not isinstance(spans, list) or not spans:
        raise ValueError("parent_source_invalid")
    text, coordinates = [], []
    for span in spans:
        if not isinstance(span, dict):
            raise ValueError("parent_source_invalid")
        sid, start, end = (span.get(k) for k in ("source_id", "start", "end"))
        source = sources.get(sid)
        if (type(sid) is not int or not isinstance(source, str) or type(start) is not int
                or type(end) is not int or not 0 <= start < end <= len(source)):
            raise ValueError("parent_source_invalid")
        text.append(source[start:end])
        coordinates.extend((sid, offset) for offset in range(start, end))
    if "".join(text) != original or len(coordinates) != len(set(coordinates)):
        raise ValueError("parent_source_invalid")
    return coordinates


def _spans(coordinates):
    spans = []
    for sid, offset in coordinates:
        if spans and spans[-1]["source_id"] == sid and spans[-1]["end"] == offset:
            spans[-1]["end"] += 1
        else:
            spans.append({"source_id": sid, "start": offset, "end": offset + 1})
    return spans


def _original_parts(original, proposals):
    """공백 차이만 있으면 누적 비공백 문자 수로 원문 연속 부분을 복원한다."""
    if "".join(part["text"] for part in proposals) == original:
        return proposals, False
    positions = [offset for offset, char in enumerate(original) if not char.isspace()]
    proposed = ["".join(char for char in part["text"] if not char.isspace()) for part in proposals]
    if "".join(proposed) != "".join(original[offset] for offset in positions):
        raise ValueError("parts_not_exact_copy")
    restored, start, used = [], 0, 0
    for index, part in enumerate(proposals):
        used += len(proposed[index])
        end = len(original) if index == len(proposals) - 1 else positions[used]
        restored.append({**part, "text": original[start:end]})
        start = end
    return restored, True


def _reject(parent, reason):
    row = deepcopy(parent)
    row.update(role="other", speaker=UNRESOLVED_SPEAKER, role_uncertain=True,
        role_status="uncertain", role_decided_by="llm_parts_rejected", role_parts_status="rejected",
        role_parts_reason=reason, role_reason="혼합 화자 부분의 원문·출처 검증에 실패해 화자 확정을 보류했습니다")
    row["uncertainties"] = list(dict.fromkeys([*row.get("uncertainties", []), "role_parts_rejected", "role_uncertain"]))
    return row


def apply_role_parts(rows, proposals, sources, *, max_units=MAX_ROLE_UNITS):
    """입력은 수정하지 않는다. 잘못된 부분은 재요청 없이 부모 원문·좌표를 보존한다."""
    original = deepcopy(rows)
    ids = [row.get("id") for row in original]
    report = {"parents": [], "accepted": [], "rejected": [], "input_units": len(rows)}
    source_texts = {source["id"]: source["text"] for source in sources
                   if isinstance(source, dict) and type(source.get("id")) is int and isinstance(source.get("text"), str)}
    if (any(not isinstance(sid, str) or not sid for sid in ids) or len(set(ids)) != len(ids)
            or not isinstance(proposals, dict) or any(not isinstance(key, str) or key not in ids for key in proposals)):
        report["parents"] = deepcopy(original)
        report["rejected"] = [{"id": row["id"], "reason": "parts_parent_ids_invalid"} for row in original]
        return [_reject(row, "parts_parent_ids_invalid") for row in original], report
    output, seen_ids, total = [], set(ids), len(original)
    for parent in original:
        sid = parent["id"]
        if sid not in proposals:
            output.append(parent)
            continue
        report["parents"].append(deepcopy(parent))
        try:
            coordinates = _coordinates(parent, source_texts)
            parts = proposals[sid]
            if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_PARTS:
                raise ValueError("parts_count_invalid")
            if any(not isinstance(part, dict) or set(part) - {"text", "role", "uncertain"}
                   or not isinstance(part.get("text"), str) or not part["text"].strip()
                   or part.get("role") not in ROLES
                   or "uncertain" in part and type(part["uncertain"]) is not bool for part in parts):
                raise ValueError("parts_value_invalid")
            parts, whitespace_restored = _original_parts(parent["original_text"], parts)
            if len({part["role"] for part in parts}) < 2:
                raise ValueError("parts_not_mixed")
            if total + len(parts) - 1 > max_units:
                raise ValueError("parts_unit_limit")
            child_ids = [f"{sid}.{index + 1}" for index in range(len(parts))]
            if any(child_id in seen_ids for child_id in child_ids):
                raise ValueError("parts_id_collision")
            cursor = 0
            children = []
            protected = _protected(parent["original_text"])
            for index, part in enumerate(parts):
                end = cursor + len(part["text"])
                first_content = next((offset for offset in range(cursor, end)
                                      if not parent["original_text"][offset].isspace()), cursor)
                if index and part["role"] != parts[index - 1]["role"] and (
                        protected[first_content]
                        or is_reported_anchor(parent["original_text"], cursor, min(cursor + 1, end))
                        or _self_dialogue_before(parent["original_text"], cursor)):
                    raise ValueError("parts_reported_switch")
                child = deepcopy(parent)
                child.update(id=child_ids[index], original_text=part["text"], text=_display(part["text"]),
                    source_spans=_spans(coordinates[cursor:end]), role=part["role"],
                    role_uncertain=True, role_status="uncertain", role_decided_by="llm_exact_copy_parts",
                    role_parts_status="accepted", parent_sentence_id=sid, role_part_index=index + 1,
                    role_part_count=len(parts), role_candidate_scope="parent_sentence",
                    role_part_whitespace_restored=whitespace_restored,
                    role_part_model_uncertain=part.get("uncertain", True),
                    parent_jev_role=parent.get("jev_role"), parent_jev_role_uncertain=parent.get("jev_role_uncertain"),
                    jev_role_uncertain=True, edits=[],
                    role_reason=f"부모 문장 {sid}의 {index + 1}/{len(parts)} 원문 구간입니다. 사전 역할 후보가 있는 경우 부모 전체의 근거이며 이 구간을 별도 판정한 확률이 아닙니다")
                child["uncertainties"] = list(dict.fromkeys([*parent.get("uncertainties", []),
                    "role_part_boundary_uncertain", "role_uncertain"]))
                children.append(child)
                cursor = end
            if [coordinate for child in children for coordinate in _coordinates(child, source_texts)] != coordinates:
                raise ValueError("parts_source_coverage_invalid")
        except (KeyError, TypeError, ValueError) as error:
            reason = str(error) if str(error) in {
                "parent_source_invalid", "parts_count_invalid", "parts_value_invalid", "parts_not_exact_copy",
                "parts_not_mixed", "parts_unit_limit", "parts_id_collision", "parts_reported_switch",
                "parts_source_coverage_invalid"} else "parts_value_invalid"
            output.append(_reject(parent, reason))
            report["rejected"].append({"id": sid, "reason": reason})
        else:
            output.extend(children)
            seen_ids.update(child_ids)
            total += len(children) - 1
            report["accepted"].append({"id": sid, "child_ids": child_ids,
                                       "whitespace_restored": whitespace_restored})
    report["output_units"] = len(output)
    return output, report
