"""현재 원문 검증을 통과한 진행 중 창·종료 질의 AI 표시 초안만 공개한다."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any

from . import serialize, settings
from .review_presentation import build_presentation
from .review_context import processing_signature
from .transcript_review import snapshot_hash, take_snapshot, validate_result


_CONTEXT_STAMP_FIELDS = ("ts_start", "ts_end", "source_order", "seq", "is_final", "speaker_role")


def rebuild_question_context(repo: Any, snapshot: dict, proofs: list[dict]) -> list[dict]:
    """저장한 호명 근거를 현재의 실제 인접 원문에서만 다시 만든다.

    과거 본문 해시가 같아도 새 호명이 사이에 들어오거나 시각·확정 상태가
    바뀌면 사용할 수 없다. 미확정 행도 인접성 검사에 포함해 건너뛰지 않는다.
    원문이나 검토 상태는 수정하지 않는다.
    """
    if not isinstance(proofs, list) or len(proofs) > 8:
        raise ValueError("질의 소유권 문맥 범위가 잘못됐습니다")
    sid = snapshot["session_id"]
    core = [r for r in snapshot["utterances"] if r.get("speaker_role") != "system"]
    if not core:
        raise ValueError("질의 소유권의 원문 범위가 없습니다")
    core_ids = [r["id"] for r in core]
    if len(set(core_ids)) != len(core_ids):
        raise ValueError("질의 원문 범위가 중복됐습니다")

    def neighbor(uid, direction):
        # 시스템 수신 표지만 제외한다. 일반 원문·미확정 발언은 건너뛰지 않는다.
        seen = {uid}
        for _ in range(32):
            row = repo.neighbor_utterances(uid, final_only=False)[direction]
            if row is None:
                return None
            if row.get("session_id") != sid or row["id"] in seen:
                raise ValueError("질의 문맥의 출처 순서를 확인하지 못했습니다")
            if row.get("speaker_role") != "system":
                return row
            uid = row["id"]
            seen.add(uid)
        raise ValueError("질의 주변 시스템 표지 범위를 확인해야 합니다")

    # 다른 블록으로 저장된 새 발언도 내부에 끼면 기존 질의 전체로 넘기지 않는다.
    for left, right in zip(core_ids, core_ids[1:]):
        next_row = neighbor(left, 1)
        if next_row is None or next_row["id"] != right:
            raise ValueError("질의 원문 사이에 새로운 발언이 있습니다")

    by_position = {"before": [], "after": []}
    ids, after_seen = set(), False
    context = []
    for proof in proofs:
        if not isinstance(proof, dict) or proof.get("position") not in by_position:
            raise ValueError("질의 문맥 방향이 잘못됐습니다")
        uid, position = proof.get("id"), proof["position"]
        if isinstance(uid, bool) or not isinstance(uid, int) or uid in ids or uid in core_ids:
            raise ValueError("질의 문맥 식별자가 잘못됐습니다")
        if position == "before" and after_seen:
            raise ValueError("질의 문맥 순서가 잘못됐습니다")
        after_seen |= position == "after"
        ids.add(uid)
        by_position[position].append(uid)
        if len(by_position[position]) > 4:
            raise ValueError("질의 문맥은 앞뒤 네 행까지 확인합니다")
        row = repo.get_utterance(uid)
        if (not row or row.get("session_id") != sid or not row.get("is_final")
                or row.get("speaker_role") == "system"):
            raise ValueError("질의 문맥의 확정 원문을 확인해야 합니다")
        text = row["text"][:1000]
        if hashlib.sha256(text.encode()).hexdigest() != proof.get("text_sha256"):
            raise ValueError("질의 소유권의 원문 근거가 바뀌었습니다")
        if "source_stamp" in proof:
            stamp = proof["source_stamp"]
            if (not isinstance(stamp, dict) or set(stamp) != set(_CONTEXT_STAMP_FIELDS)
                    or stamp != {key: row.get(key) for key in _CONTEXT_STAMP_FIELDS}):
                raise ValueError("질의 문맥의 시각 또는 출처 순서가 바뀌었습니다")
        context.append({"id": uid, "text": text, "position": position})

    for position, direction, edge in (("before", 0, core_ids[0]), ("after", 1, core_ids[-1])):
        actual, uid = [], edge
        for _ in by_position[position]:
            row = neighbor(uid, direction)
            if row is None:
                raise ValueError("질의 주변 원문 범위가 달라졌습니다")
            actual.append(row["id"])
            uid = row["id"]
        if direction == 0:
            actual.reverse()
        if actual != by_position[position]:
            raise ValueError("질의 직전·직후 원문 순서가 달라졌습니다")
    return context


def validate_question_ownership(repo: Any, snapshot: dict, turns: list[dict], presentation: dict) -> None:
    """표시와 사람 채택에 같은 현재 소유권·요약 범위 검사를 적용한다."""
    if presentation.get("final_document"):
        from .question_manuscript import validate_document
        from .question_audio import first_pass_current
        validate_document(snapshot, presentation["final_document"])
        if not first_pass_current(repo, snapshot["session_id"], snapshot["block"]["id"],
                                  presentation.get("first_pass_proofs")):
            raise ValueError("최종 전사가 사용한 1차 문맥 초안 판본이 달라졌습니다")
        if "ownership" not in presentation:
            raise ValueError("최종 전사 초안의 요약 범위가 없습니다")
    if "ownership" not in presentation:
        return
    from .speaker_lab.question_ownership import derive_question_ownership
    context = rebuild_question_context(repo, snapshot, presentation.get("ownership_context", []))
    if presentation.get("final_document"):
        from .manuscript_presentation import manuscript_scope
        if manuscript_scope(snapshot, presentation["final_document"]) != presentation["ownership"]:
            raise ValueError("전체 음성 초안의 질의 범위 또는 판본이 달라졌습니다")
        return
    current = {**snapshot, "audio_evidence": {"context": context}}
    if derive_question_ownership(current, turns) != presentation["ownership"]:
        raise ValueError("질의 소유권 또는 요약 범위의 근거가 달라졌습니다")


def accepted_question_ownership(ownership: dict, review_id: int, digest: str) -> dict:
    """원음 정답 검증과 구별한 사람의 초안 채택 메타데이터를 만든다."""
    return {**{key: deepcopy(ownership.get(key)) for key in ("questioner", "scope", "issues")},
            "status": "accepted" if ownership.get("status") == "draft" else "held",
            "review_id": review_id, "input_hash": digest, "validation_status": "not_audio_verified"}


def _accepted_question_display(repo: Any, block: dict, original: dict) -> dict | None:
    """채택된 요약의 이름·범위를 현재 근거로 확인해 응답 사본에만 적용한다."""
    summary = block.get("summary")
    if not isinstance(summary, dict) or "question_ownership" not in summary:
        return None
    if summary.get("edited_by") or block.get("review_status") == "reviewed":
        return original  # 사람이 후속 편집·검수한 본문과 지정은 자동 표시보다 우선한다.
    try:
        if summary.get("input_source") != "accepted_review":
            raise ValueError("질의 범위가 같은 채택 판본을 참조하지 않습니다")
        review = repo.get_transcript_review(summary["review_id"])
        if (not review or review.get("status") != "accepted" or block.get("is_open")
                or review.get("session_id") != block["session_id"] or review.get("block_id") != block["id"]):
            raise ValueError("현재 채택된 질의 판본이 아닙니다")
        snapshot = take_snapshot(repo, block["session_id"], block["id"])
        digest = snapshot_hash(snapshot)
        payload = review["payload"]
        presentation = payload["presentation"]
        if (presentation.get("ready") is not True or presentation.get("schema_version") != 1
                or "ownership" not in presentation
                or any(value != digest for value in (review.get("input_hash"), payload.get("input_hash"),
                           presentation.get("input_hash"), summary.get("input_hash")))):
            raise ValueError("채택된 원문 판본이 바뀌었습니다")
        turns = validate_result(snapshot, payload.get("turns"))
        rebuilt = build_presentation(snapshot, turns)
        if any(presentation.get(key) != rebuilt[key] for key in ("cards", "source_rows", "readability_report")):
            raise ValueError("채택된 원문 표시 범위가 바뀌었습니다")
        validate_question_ownership(repo, snapshot, turns, presentation)
        if presentation.get("final_document"):
            from .question_manuscript import validate_document
            validate_document(snapshot, presentation["final_document"])
        grouped = {}
        for part in turns:
            stored = {key: part[key] for key in ("start", "end", "role", "speaker") if key in part}
            if part.get("group_id"):
                stored["group_id"] = f"r{review['id']}:{part['group_id']}"
            grouped.setdefault(part["utterance_id"], []).append(stored)
        for row in repo.list_utterances(block["session_id"], block_id=block["id"], final_only=True):
            if row.get("review_parts") != grouped.get(row["id"], []):
                raise ValueError("사람이 채택한 발언 부분이 바뀌었습니다")
        public = accepted_question_ownership(presentation["ownership"], review["id"], digest)
        if summary["question_ownership"] != public:
            raise ValueError("채택한 요약과 질의 범위가 다릅니다")
        # 저장 원문·블록 이름은 유지하고 채택 판본을 보여 주는 사본만 바꾼다.
        display = {**block, "questioner": public["questioner"] or "", "questioner_party": ""}
        return {**serialize.block(display), "question_ownership": public}
    except (KeyError, ValueError, TypeError):
        # 낡은 자동 요약은 현재 원문 범위의 요약인 것처럼 재사용하지 않는다.
        return {**original, "summary": None, "question_ownership": {
            "status": "held", "questioner": None, "scope": None,
            "issues": ["accepted_question_evidence_changed"], "validation_status": "not_audio_verified"}}


def get_block_display(repo: Any, block: dict[str, Any]) -> dict[str, Any]:
    """원문을 바꾸지 않고 진행 중 창 또는 종료 질의의 검증된 표시만 추가한다.

    최신 전체 검토가 준비 중이거나 취소·실패했으면 과거 초안으로 돌아가지 않는다.
    범위가 제한된 창 검토는 종료 질의의 전체 표시 후보로 사용하지 않는다.
    """
    original = serialize.block(block)
    from .display_reads import enabled
    if not getattr(repo, "_display_building", False) and enabled(repo, block["session_id"]):
        # 새 공통 표시본이 소유하는 회의에서는 구형 AI 판정을 다시 검증·조립하지 않는다.
        return repo._display_saved_block(block["session_id"], block["id"]) or original
    accepted = _accepted_question_display(repo, block, original)
    if accepted is not None:
        return accepted
    if not settings.get(repo, "transcript_review").get("enabled"):
        return original
    if block.get("kind") != "question" or block.get("review_status") == "reviewed":
        return original
    summary = block.get("summary")
    if isinstance(summary, dict) and summary.get("edited_by"):
        return original
    if block.get("is_open"):
        from .live_review_display import get_live_review_display
        live = get_live_review_display(repo, block)
        return serialize.block({**block, "ai_live_review": live}) if live else original
    sid, bid = block["session_id"], block["id"]
    candidate = None
    select = getattr(repo, "latest_block_transcript_review", None)
    candidates = [select(sid, bid)] if select else repo.list_transcript_reviews(sid, bid)
    for review in candidates:
        if review is None:
            break
        payload = review.get("payload")
        scope = payload.get("scope") if isinstance(payload, dict) else None
        if isinstance(scope, dict) and scope.get("kind") == "window":
            continue
        candidate = review
        break
    if candidate is None or candidate.get("status") != "draft":
        return original
    payload = candidate.get("payload")
    if not isinstance(payload, dict):
        return original
    scope = payload.get("scope")
    if scope and (not isinstance(scope, dict) or set(scope) - {"kind"} or scope.get("kind") not in {None, "block"}):
        return original
    presentation = payload.get("presentation")
    if (not isinstance(presentation, dict) or presentation.get("ready") is not True
            or type(presentation.get("schema_version")) is not int or presentation["schema_version"] != 1
            or not isinstance(presentation.get("summary"), dict) or not isinstance(presentation.get("editor"), dict)):
        return original
    try:
        if presentation.get("processing_hash") is not None \
                and presentation["processing_hash"] != processing_signature(repo):
            return original
        current = repo.get_block(bid)
        if (not current or current.get("session_id") != sid or current.get("kind") != "question"
                or current.get("is_open") or current.get("review_status") == "reviewed"):
            return original
        current_summary = current.get("summary")
        if isinstance(current_summary, dict) and current_summary.get("edited_by"):
            return original
        snapshot = take_snapshot(repo, sid, bid)
        if any(row.get("review_parts") for row in repo.list_utterances(sid, block_id=bid, final_only=True)):
            return original
        digest = snapshot_hash(snapshot)
        if any(value != digest for value in (candidate.get("input_hash"), payload.get("input_hash"),
                                             presentation.get("input_hash"))):
            return original
        turns = validate_result(snapshot, payload.get("turns"))
        rebuilt = build_presentation(snapshot, turns)
        if any(presentation.get(key) != rebuilt[key] for key in ("cards", "source_rows", "readability_report")):
            return original
        validate_question_ownership(repo, snapshot, turns, presentation)
    except (KeyError, ValueError, TypeError):
        return original
    return serialize.block({**block, "ai_review": {"id": candidate["id"], "status": "draft",
        "input_hash": digest, "cards": deepcopy(rebuilt["cards"]), "summary": deepcopy(presentation["summary"]),
        "readability_report": deepcopy(rebuilt["readability_report"]), "editor": deepcopy(presentation["editor"]),
        **({"final_document": deepcopy(presentation["final_document"])} if presentation.get("final_document") else {}),
        **({"ownership": {k: deepcopy(presentation["ownership"].get(k))
                          for k in ("status", "questioner", "scope", "issues")}} if "ownership" in presentation else {})}})
