"""질의 전체 편집의 공개 초안과 같은 판본의 요약 입력을 만든다."""
from __future__ import annotations

from copy import deepcopy
import re

from .question_manuscript import validate_document


def public_document(snapshot: dict, document: dict) -> dict:
    """음성 원전사·인용을 공개 응답에서 제외하고 출처 좌표는 보존한다."""
    checked = validate_document(snapshot, document)
    result = {key: deepcopy(checked[key]) for key in (
        "schema", "stage", "source_hash", "input_hash", "status", "model_status",
        "preview_only", "audio_verified", "source_text_changed", "metrics") if key in checked}
    result.update(public_projection=True, evidence_verified=False, units=[])
    for unit in checked["units"]:
        copied = {key: deepcopy(unit[key]) for key in (
            "source_spans", "source_text", "text", "speaker", "role", "scope", "protected",
            "ts_start", "ts_end", "review_required", "risk_flags") if key in unit}
        copied["changes"] = [{key: change[key] for key in ("before", "after")}
                             for change in unit.get("changes", [])]
        copied["evidence_verified"] = False
        result["units"].append(copied)
    # 공개 사본도 현재 원문과 완전히 연결돼야 한다.
    source = {key: value for key, value in snapshot.items() if key != "audio_evidence"}
    return validate_document(source, result)


def manuscript_scope(snapshot: dict, document: dict) -> dict:
    """최종 초안에서 한 의원의 명확한 질문·답변만 요약 대상으로 고른다."""
    document = validate_document(snapshot, document)
    units = [u for u in document["units"] if u["scope"] == "question" and u["role"] in {"member", "answer"}]
    names = {u["speaker"] for u in units if u["role"] == "member" and
             u["speaker"] not in {"", "위원", "미상", "답변", "위원장"}}
    ready = document["status"] in {"draft", "partial"} and len(names) == 1 and bool(units)
    issues = []
    if not ready:
        issues.append("whole_question_owner_unresolved")
    unresolved = list(dict.fromkeys(s["utterance_id"] for u in document["units"]
        if u["scope"] != "context" and (u["scope"] == "uncertain" or u["role"] in {"unknown", "other"}
            or "speaker_identity_unverified" in u.get("risk_flags", []))
        for s in u["source_spans"]))
    if unresolved:
        issues.append("unresolved_units_have_no_confirmed_attribution")
    return {"method": "whole_question_manuscript", "status": "draft" if ready else "held",
            "questioner": next(iter(names)) if len(names) == 1 else None,
            "scope": {"source_spans": [deepcopy(s) for u in units for s in u["source_spans"]]} if ready else None,
            "issues": issues, "unresolved_source_ids": unresolved, "validation_status": "not_audio_verified"}


def manuscript_summary_rows(snapshot: dict, document: dict) -> list[dict]:
    """요약용 문장도 동일한 최종 초안의 본문·역할·원문 연결만 사용한다."""
    checked = validate_document(snapshot, document)
    if manuscript_scope(snapshot, checked)["status"] != "draft":
        return []
    result = []
    for unit_index, unit in enumerate(checked["units"], start=1):
        if unit["scope"] == "context" or unit["role"] == "system":
            continue
        spans = unit["source_spans"]
        confirmed = (unit["scope"] == "question" and unit["role"] in {"member", "answer"}
                     and "speaker_identity_unverified" not in unit.get("risk_flags", []))
        # 모델의 u번호는 최종 문장 단위다. 반환 후 실제 원문 번호로 반드시 변환한다.
        result.append({"id": unit_index, "manuscript_unit_id": unit_index, "text": unit["text"],
                       "speaker": unit["speaker"] if confirmed else "귀속 확인 필요",
                       "speaker_role": unit["role"] if confirmed else "unknown",
                       "attribution_confirmed": confirmed,
                       "ts_start": unit.get("ts_start"), "ts_end": unit.get("ts_end"),
                       "source_spans": deepcopy(spans),
                       "source_utterance_ids": list(dict.fromkeys(s["utterance_id"] for s in spans))})
    return result


def map_manuscript_summary_refs(summary: dict, rows: list[dict]) -> dict:
    """최종 문장 임시 번호를 그 문장의 모든 원문 출처로 되돌린다."""
    if summary.get("reference_namespace") == "source_utterance":
        raise ValueError("이미 원문 번호로 변환한 요약입니다")
    indexed = {}
    for row in rows:
        uid = row.get("id")
        ids = row.get("source_utterance_ids")
        spans = row.get("source_spans")
        if (type(uid) is not int or uid < 1 or uid in indexed or not isinstance(ids, list)
                or not ids or any(type(value) is not int or value < 1 for value in ids)
                or not isinstance(spans, list) or not spans
                or any(not isinstance(span, dict) or span.get("utterance_id") not in ids for span in spans)):
            raise ValueError("최종 요약의 원문 출처표가 올바르지 않습니다")
        indexed[uid] = row
    result = deepcopy(summary)
    if isinstance(result.get("org_reason"), str):
        # 규칙 근거의 괄호 안 u번호는 최종 문장 임시 번호다. 실제 원문처럼 표시하지 않는다.
        result["org_reason"] = re.sub(r"\s*\(\s*u\d+(?:\s*,\s*u\d+)*\s*\)", "",
                                     result["org_reason"]).strip()
    for field in ("issues", "data_requests", "commitments", "held_attributions"):
        for item in result.get(field) or []:
            if not isinstance(item, dict):
                continue
            uid = item.get("utterance_id")
            row = indexed.get(uid) if type(uid) is int else None
            if row is None:
                item.update(utterance_id=None, source_utterance_ids=[], source_spans=[],
                            reference_status="unresolved", attribution_confirmed=False)
                item.pop("manuscript_unit_id", None)
                continue
            ids = list(dict.fromkeys(row["source_utterance_ids"]))
            item.update(utterance_id=ids[0], source_utterance_ids=ids,
                        source_spans=deepcopy(row["source_spans"]), manuscript_unit_id=uid,
                        reference_status="linked", attribution_confirmed=row.get("attribution_confirmed") is True)
    result["reference_namespace"] = "source_utterance"
    return result
