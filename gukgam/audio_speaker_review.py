"""음성 대조 근거를 더한 Jev 화자·경계 검토. 본문 교정과 저장은 하지 않는다."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import re
from typing import Any

from .llm.decision import (DecisionError, JEV_MODEL, MAX_STATE_BYTES, validate_answers,
                           validate_questions)
from .review_boundaries import _quoted_ranges
from .transcript_editor import _audio_input
from .transcript_review import (MAX_BOUNDARIES, ReviewEngine, _normalize_groups,
                                _original_turn, _protected, boundary_candidates,
                                snapshot_hash, validate_result)

_AUDIO_RULE = (
    " 음성 대조는 추가 자료다. audio_evidence.rows는 columns, parts는 part_columns 순서다. "
    "speaker_index는 이 녹음에서만 유효한 익명 묶음이며 실명·역할이 아니다. "
    "동일한 사람이 서로 다른 음성 ID로 나뉘거나 다른 사람이 같은 ID를 가질 수 있다. "
    "ID 일치·변화만으로 연결·분리를 확정하지 말고 원자막·별도 전사·호명·자기소개를 함께 읽는다. "
    "held_ranges와 parts가 덮지 않은 문자에는 확정된 음성 근거가 없다. "
    "A의원-답변자-A의원의 앞뒤가 동일 인물이어도 가운데 답변을 합치거나 없애지 않는다. "
    "본문 단어·숫자·이름·띄어쓰기를 교정하지 않으며 불충분하면 보류한다. "
    "앞뒤 문맥 행은 판정 자료이며 수정·병합 대상이 아니다."
)
_UNRESOLVED = {
    "mixed_group", "uncertain", "missing_boundary", "boundary_held",
    "part_held", "floor_mixed", "group_role_uncertain", "semantic_boundary_held",
    "sentence_tail_held", "audio_boundary_held", "audio_boundary_limit",
    "audio_context_order_held",
}


def _safe_context(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """여유 구간의 공개 본문만 받고 식별자·중복·위치를 확인한다."""
    context = snapshot["audio_evidence"].get("context", [])
    if not isinstance(context, list):
        raise ValueError("음성 앞뒤 문맥 목록 오류")
    seen = {row["id"] for row in snapshot["utterances"]}
    result = []
    for row in context:
        if not isinstance(row, dict):
            raise ValueError("음성 앞뒤 문맥 행 오류")
        uid, text, position = row.get("id"), row.get("text"), row.get("position")
        if (isinstance(uid, bool) or not isinstance(uid, int) or uid < 1 or uid in seen
                or not isinstance(text, str) or position not in {"before", "after"}):
            raise ValueError("음성 앞뒤 문맥 범위 오류")
        seen.add(uid)
        result.append({"id": uid, "text": text, "position": position})
    return result


def _held_ids(issues: list[dict], turns: list[dict]) -> set[int]:
    """행 내부 판정 보류만 복원한다. 행간 연결 보류는 양쪽 역할을 취소하지 않는다."""
    held = {part["utterance_id"] for part in turns if part.get("role") == "unknown"}
    for issue in issues:
        if issue.get("kind") not in _UNRESOLVED:
            continue
        held.update(uid for uid in (issue.get("utterance_id"), issue.get("previous_utterance_id"),
                                    *issue.get("utterance_ids", [])) if isinstance(uid, int))
    return held


def _held_edges(issues: list[dict]) -> list[dict[str, int]]:
    """같은 사람으로 연결하지 못한 행간 경계는 별도 상태로 남긴다."""
    pairs = {(issue.get("previous_utterance_id"), issue.get("utterance_id")) for issue in issues
             if issue.get("kind") == "continuity_held"}
    return [{"previous_utterance_id": left, "utterance_id": right} for left, right in sorted(
        (left, right) for left, right in pairs if type(left) is int and type(right) is int)]


def _break_held_connections(turns: list[dict], boundaries: list[dict]) -> None:
    """복원한 이전 초안에도 보류 경계를 건너는 묶음을 남기지 않는다."""
    pairs = {(item["previous_utterance_id"], item["utterance_id"]) for item in boundaries}
    blocked = {left["group_id"] for left, right in zip(turns, turns[1:])
               if left.get("group_id") and left.get("group_id") == right.get("group_id")
               and (left["utterance_id"], right["utterance_id"]) in pairs}
    for part in turns:
        if part.get("group_id") in blocked:
            part.pop("group_id", None)


def _final_metrics(turns: list[dict], baseline: list[dict]) -> dict[str, int]:
    """최종 의미 판본만 비교한다. 확률 필드 추가를 화자 수정으로 세지 않는다."""
    keys = ("utterance_id", "start", "end", "role", "speaker", "group_id")
    previous = {tuple(part.get(key) for key in keys) for part in baseline}
    groups: dict[str, list[dict]] = {}
    for part in turns:
        if part.get("group_id"):
            groups.setdefault(part["group_id"], []).append(part)
    return {"changed_turns": sum(tuple(part.get(key) for key in keys) not in previous for part in turns),
            "continuity_groups": len(groups),
            "joined_boundaries": sum(len(parts) - 1 for parts in groups.values())}


def _context_for_floor(snapshot: dict, context: dict) -> dict | None:
    """로컬 발언권 추적에는 공개 본문의 실제 출처 순서도 함께 보존한다."""
    raw = next(row for row in snapshot["audio_evidence"]["context"] if row["id"] == context["id"])
    stamp = raw.get("source_stamp") or {}
    if not isinstance(stamp, dict):
        return None
    result = {"id": context["id"], "text": context["text"], "original_text": context["text"],
              "speaker_role": "unknown", "speaker": ""}
    order = stamp.get("source_order")
    if isinstance(order, str) and re.fullmatch(r"[0-9]+(?::[0-9]+)*", order):
        result["source_order"] = order
    for key in ("ts_start", "ts_end", "seq"):
        if type(stamp.get(key)) is int and stamp[key] >= 0:
            result[key] = stamp[key]
    targets = snapshot["utterances"]
    if any(row.get("source_order") or row.get("source_key") for row in targets):
        return result if "source_order" in result else None
    if any(row.get("ts_start") for row in targets):
        return result if result.get("ts_start") else None
    ids = [row["id"] for row in targets]
    if ids and ((context["position"] == "before" and context["id"] >= min(ids))
                or (context["position"] == "after" and context["id"] <= max(ids))):
        return None
    return result


def _checked_audio(snapshot: dict[str, Any]) -> tuple[dict[int, list], dict, list[dict]]:
    """전사·앵커를 빠짐없이 압축하고 부가 경로·설정 필드는 가져오지 않는다."""
    evidence = snapshot.get("audio_evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("rows"), list):
        raise ValueError("음성 대조 근거가 없습니다")
    packed, schema = _audio_input(snapshot)
    for source in evidence["rows"]:
        uid = source["id"]
        text = next(row.get("text") or "" for row in snapshot["utterances"] if row["id"] == uid)
        explicit = source.get("held_ranges", [])
        if not isinstance(explicit, list):
            raise ValueError("음성 미확정 범위 목록 오류")
        for item in explicit:
            if isinstance(item, dict):
                start, end = item.get("start"), item.get("end")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                start, end = item[:2]
            else:
                raise ValueError("음성 미확정 범위 오류")
            if (any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end))
                    or not 0 <= start < end <= len(text)):
                raise ValueError("음성 미확정 문자 범위 오류")
            if any(start < part[1] and part[0] < end for part in packed[uid][3]):
                raise ValueError("음성 앵커와 미확정 범위가 충돌합니다")
            if [start, end] not in packed[uid][4]:
                packed[uid][4].append([start, end])
        packed[uid][4].sort()
    # 원래 익명 ID 문자열도 보내지 않고 요청 내부의 순서 번호만 사용한다.
    schema["speakers"] = [f"audio_{index}" for index in range(len(schema["speakers"]))]
    return packed, schema, _safe_context(snapshot)


def _add_audio_boundaries(snapshot: dict[str, Any], packed: dict[int, list]) -> tuple[dict, list, dict]:
    """맞닿은 음성 앵커 전환을 안전한 원문 단어 경계 후보로만 추가한다."""
    updated = deepcopy(snapshot)
    mapping = updated.setdefault("source_boundaries", {})
    issues, added = [], {}
    for row in snapshot["utterances"]:
        uid, text = row["id"], row.get("text") or ""
        if _protected(snapshot, row):
            continue
        existing = {candidate["offset"] for candidate in boundary_candidates(snapshot, row)}
        quotes, proposed = _quoted_ranges(text), []
        for left, right in zip(packed[uid][3], packed[uid][3][1:]):
            if left[2] == right[2]:
                continue
            offset = right[0]
            gap = text[left[1]:offset]
            # 음성 대응이 끊긴 사이에 이미 검증 가능한 문답 경계가 있으면 그 후보를 묻는다.
            # 시각을 임의 보간하거나 앵커의 시작점으로 그 경계를 옮기지는 않는다.
            if any(left[1] <= candidate <= offset
                   and not any(start <= candidate < end for start, end in quotes) for candidate in existing):
                issues.append({"kind": "audio_boundary_text_candidate", "utterance_id": uid,
                    "start": left[1], "end": offset,
                    "message": "음성 전환 사이의 기존 원문 경계 후보를 문맥 검토 대상으로 유지합니다"})
                continue
            # 비공백 누락·겹치는 음성·1초 이상 단절·어절 내부·인용 안은 새 후보로 만들지 않는다.
            safe = (0 <= right[3] - left[4] <= 1000 and (not gap or gap.isspace())
                    and 0 < offset < len(text)
                    and (text[offset - 1].isspace() or text[offset - 1] in ".!?。")
                    and not any(start <= offset < end for start, end in quotes)
                    and not any(start <= offset < end for start, end in packed[uid][4]))
            if not safe:
                issues.append({"kind": "audio_boundary_held", "utterance_id": uid,
                    "start": left[1], "end": offset,
                    "message": "음성 ID 전환을 안전한 원문 경계와 연결하지 못해 자동 분리를 보류합니다"})
                continue
            if offset not in existing and offset not in proposed:
                proposed.append(offset)
        capacity = MAX_BOUNDARIES - len(existing)
        if len(proposed) > capacity:
            issues.append({"kind": "audio_boundary_limit", "utterance_id": uid,
                "message": "음성 전환 후보가 한도를 넘어 일부 후보를 확정하지 않았습니다"})
        selected = proposed[:capacity]
        if selected:
            # 이 복사본에서만 기존 원문 좌표 후보 경로에 연결한다. 수집원 DB는 변경하지 않는다.
            original = mapping.get(str(uid), mapping.get(uid, []))
            mapping[str(uid)] = deepcopy(original) + [{"offset": offset} for offset in selected]
            added[uid] = selected
    return updated, issues, added


class _AudioReviewEngine(ReviewEngine):
    """기존 발언권·경계·보호 가드에 음성 추가 자료만 연결한다."""

    def __init__(self, provider: Any, packed: dict[int, list], schema: dict, context: list[dict], added: dict):
        super().__init__(None, provider=provider)
        self.audio = {**schema, "rows": list(packed.values())}
        self.context, self.added = context, added
        self.calls = 0

    async def _ask(self, state: dict[str, Any], questions: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        # 단어 후보는 두 번째 본문 교정 단계의 소관이다. Jev에 용어 교정 질문은 하지 않는다.
        words = {name: question for name, question in questions.items() if name.startswith("w_")}
        result = {name: {"type": "choice", "choice": "hold", "confidence": 1.0,
                        "probabilities": {key: float(key == "hold") for key in question["criteria"]}}
                  for name, question in words.items()}
        questions = {name: deepcopy(question) for name, question in questions.items() if name not in words}
        if not questions:
            return result
        reference_ids = {row["id"] for row in state.get("context_utterances", [])}
        supplied = {**deepcopy(state), "audio_evidence": self.audio,
                    "audio_context": [{key: value for key, value in row.items()
                                       if key != "text" or row["id"] not in reference_ids} for row in self.context],
                    "audio_evidence_note": _AUDIO_RULE}
        # 원문 좌표 후보를 수집원 확정 경계라고 잘못 설명하지 않는다.
        for row in supplied.get("utterances", []):
            for boundary in row.get("boundaries", []):
                if boundary["offset"] in self.added.get(row["id"], []):
                    boundary["reason"] = "음성 전환의 원문 단어 경계 후보·미확정"
        for question in questions.values():
            question["instructions"] += _AUDIO_RULE
            if "hold" in question.get("criteria", {}):
                question["criteria"]["hold"] = "원자막·음성 근거·앞뒤 문맥으로 확정할 수 없어 유지·보류한다."
        # 가짜 제공자에서도 실 제공자와 같은 상한을 지킨다. 큰 입력을 잘라 숨기지 않는다.
        if len(json.dumps(supplied, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_STATE_BYTES:
            raise DecisionError("input_limit", "음성 검토 입력 한도 초과")
        pending = {}
        for name, question in questions.items():
            try:
                validate_questions(supplied, {**pending, name: question})
            except DecisionError as error:
                if error.status != "input_limit" or not pending:
                    raise
                result.update(await self._evaluate(supplied, pending, draft))
                pending = {}
                validate_questions(supplied, {name: question})
            pending[name] = question
        if pending:
            result.update(await self._evaluate(supplied, pending, draft))
        return result

    async def _evaluate(self, state: dict, questions: dict, draft: dict) -> dict:
        draft["metrics"]["provider_calls"] += 1
        self.calls += 1
        answers = await self.provider.evaluate(state, questions)
        return validate_answers({"model": JEV_MODEL, "answers": answers,
            "usage": {"input_tokens": 0, "output_tokens": 0}}, questions)


async def review_audio_speakers(snapshot: dict[str, Any], baseline: list[dict[str, Any]], provider: Any
                                ) -> dict[str, Any]:
    """저장·채택 없이 화자 초안을 반환한다. 실패·취소 시 새 본문은 만들지 않는다."""
    original = [_original_turn(row) for row in snapshot["utterances"]]
    try:
        saved = validate_result(snapshot, baseline)
    except (KeyError, TypeError, ValueError):
        saved = original
    fallback = {"input_hash": snapshot_hash(snapshot), "model": JEV_MODEL,
        "engine_revision": "audio-speaker-jev-v1", "stage": "audio_speaker_review", "complete": False,
        "status": "held", "turns": deepcopy(saved), "issues": [],
        "held_utterance_ids": sorted(row["id"] for row in snapshot["utterances"]),
        "held_continuity_boundaries": [],
        "metrics": {"provider_calls": 0, "source_utterances": len(original),
                    "source_chars": sum(part["end"] for part in original),
                    "semantic_text_changes": 0, "changed_turns": 0}}
    fallback["metrics"].update(_final_metrics(saved, saved))
    engine = None
    try:
        packed, schema, context = _checked_audio(snapshot)
        prepared, boundary_issues, added = _add_audio_boundaries(snapshot, packed)
        # roster에 우연히 들어간 설정·경로 등은 공개 명단 필드와 함께 전달하지 않는다.
        prepared["roster"] = [{key: deepcopy(person[key]) for key in ("name", "role", "aliases") if key in person}
                              for person in snapshot.get("roster", [])]
        prepared["glossary"] = []
        # 앞뒤 문맥을 기존 발언권 추적에도 제공하며 수정 대상에 넣지 않는다.
        reference = {row["id"]: deepcopy(row) for row in prepared.get("context_utterances", [])}
        for row in context:
            existing = reference.get(row["id"])
            if existing and existing.get("text") != row["text"]:
                raise ValueError("동일 문맥 식별자의 본문이 다릅니다")
            if not existing:
                floor_context = _context_for_floor(snapshot, row)
                if floor_context is None:
                    boundary_issues.append({"kind": "audio_context_order_held",
                        "utterance_ids": [item["id"] for item in snapshot["utterances"]],
                        "message": "앞뒤 문맥의 실제 출처 순서를 확인하지 못해 자동 화자 확정을 보류합니다"})
                else:
                    reference[row["id"]] = floor_context
        if reference:
            prepared["context_utterances"] = list(reference.values())
            scope = prepared.get("scope") or {"kind": "block"}
            prepared["scope"] = {key: deepcopy(scope[key]) for key in ("kind", "utterance_ids", "context_ids") if key in scope}
            prepared["scope"].setdefault("context_ids", list(reference))
        engine = _AudioReviewEngine(provider, packed, schema, context, added)
        draft = await engine._review_snapshot(prepared, lambda: prepared)
        draft.update(input_hash=fallback["input_hash"], stage=fallback["stage"],
                     engine_revision=fallback["engine_revision"])
        if "scope" not in snapshot:
            draft.pop("scope", None)
        draft["issues"].extend(boundary_issues)
        held_edges = _held_edges(draft["issues"])
        draft["metrics"]["audio_input"] = {"rows": len(packed), "source_parts": sum(row[5] for row in packed.values()),
            "packed_spans": sum(len(row[3]) for row in packed.values()),
            "held_ranges": sum(len(row[4]) for row in packed.values()),
            "missing_row_ids": schema["missing_row_ids"], "added_boundaries": sum(map(len, added.values())),
            "audio_chars": sum(len(row[1]) for row in packed.values()), "omitted_parts": 0}
        if draft["status"] != "draft":
            draft["turns"] = deepcopy(saved)
            held = {row["id"] for row in snapshot["utterances"]}
        else:
            # 판정 보류 행을 이전 검증된 초안보다 나쁘게 덮지 않는다.
            held = _held_ids(draft["issues"], draft["turns"])
            parts = []
            for row in snapshot["utterances"]:
                source = saved if row["id"] in held else draft["turns"]
                parts.extend(deepcopy(part) for part in source if part["utterance_id"] == row["id"])
            _break_held_connections(parts, held_edges)
            _normalize_groups(parts)
            validate_result(snapshot, parts)
            draft["turns"] = parts
        held.update(part["utterance_id"] for part in draft["turns"] if part.get("role") == "unknown")
        for issue in draft["issues"]:
            affected = {issue.get("utterance_id"), *(issue.get("utterance_ids") or [])}
            if held & affected and (issue.get("applied") is True
                    or issue.get("kind") in {"floor_resolved", "sentence_tail_resolved"}):
                issue["applied"] = False
        draft["held_utterance_ids"] = sorted(held)
        draft["held_continuity_boundaries"] = held_edges
        draft["complete"] = draft["status"] == "draft" and not held and not held_edges
        draft["metrics"]["held_utterances"] = len(held)
        draft["metrics"].update(_final_metrics(draft["turns"], saved))
        return draft
    except asyncio.CancelledError:
        raise
    except Exception as error:
        status = error.status if isinstance(error, DecisionError) else "invalid_input"
        fallback["status"] = status if status in {"timeout", "busy", "unavailable"} else "held"
        fallback["metrics"]["provider_calls"] = engine.calls if engine else 0
        fallback["issues"].append({"kind": "input_limit" if status == "input_limit" else "audio_review_held",
            "message": "음성 화자 검토를 완료하지 못해 기존 초안을 보존합니다"})
        return fallback
