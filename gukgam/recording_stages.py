"""확정된 파일 전사를 기존 종료 질의 편집기에 연결하는 판본 계약."""
from __future__ import annotations

from copy import deepcopy

from . import settings


SOURCE_KIND = "recording_asr"


def is_recording_session(repo, sid: str) -> bool:
    return (repo.get_session(sid) or {}).get("source_type") == "file"


def recording_complete(session: dict) -> bool:
    state = session.get("source_state") or {}
    return bool(session.get("source_type") == "file" and session.get("status") == "ended"
                and state.get("phase") == "done" and not state.get("error"))


def selection_for(repo, sid: str, block: dict) -> dict:
    from .recording_sources import select_recording_sources
    return select_recording_sources(block, repo.display_build_blocks(sid), repo.list_utterances(sid, limit=-1),
        repo.get_roster(sid), recording_complete=recording_complete(repo.get_session(sid) or {}))


def dependencies_for(repo, sid: str, block: dict, selection: dict) -> tuple[list[dict], dict, dict]:
    from .recording_sources import recording_sources
    ids = set(selection.get("dependency_source_ids") or [])
    sources = [s for s in recording_sources(repo.list_utterances(sid, limit=-1)) if s["id"] in ids]
    block_ids = set(selection.get("dependency_block_ids") or [])
    stamps = {str(bid): repo.stage_boundary_stamp(row) for bid in block_ids
              if (row := repo.stage_block_record(sid, bid)) is not None}
    uids = {r["utterance_id"] for r in selection.get("utterance_membership") or []}
    uids.update(s["id"] for s in selection.get("sources") or [])
    uids.update(ids)
    rows = repo.stage_utterances_by_ids(sid, sorted(uids))
    membership = {"utterance_ids": sorted(uids),
                  "block_ids": sorted({block["id"], *(r["block_id"] for r in rows if r.get("block_id"))})}
    return sources, stamps, membership


def prepare(service, sid: str, block: dict, selection: dict | None = None) -> dict:
    """caption 표식과 FK를 만들지 않고 같은 editor·실행 격리·사람 보호를 쓴다."""
    from .live_stages import digest
    repo = service.rt.repo
    selection = selection or selection_for(repo, sid, block)
    if not service._ready(selection):
        raise ValueError("녹음 처리가 완료된 질의의 확정 전사 원문이 필요합니다")
    sources = selection["sources"]
    dependencies, dependency_blocks, membership = dependencies_for(repo, sid, block, selection)
    context = service._context(sid, block)
    context_hash = digest(context)
    identity = selection.get("question_identity") or {}
    if identity:
        context = {**context, "question_identity": deepcopy(identity)}
        if (not context.get("questioner") and identity.get("questioner")
                and identity.get("status") == "roster_exact_call"):
            # 명단과 호명이 일치한 후보를 문맥에 제공하되 identity.uncertain을 유지한다.
            context["questioner"] = identity["questioner"]
            context["questioner_party"] = identity.get("questioner_party") or ""
    protection = digest(service._protection(sid, sources, membership=membership))
    membership_hash = repo.stage_membership_stamp(sid, membership)
    if not membership_hash:
        raise ValueError("녹음의 발언 연결 근거가 변경됐습니다")
    role_decision = service._role_decision_metadata()
    processing_hash = digest([settings.get(repo, "transcript_review"), settings.get(repo, "llm")])
    refs = {str(s["id"]): s["revision"] for s in sources}
    source_hashes = {str(s["id"]): s["source_hash"] for s in [*dependencies, *sources]}
    base = {"mode": service.MODE, "editor_version": service.EDITOR_VERSION, "source_kind": SOURCE_KIND,
        "block_id": block["id"], "questioner": context.get("questioner") or "",
        "block_stamp": service._block_stamp(block), "context_hash": context_hash,
        "source_ids": [s["id"] for s in sources], "source_revisions": refs,
        "source_selection": [{"id": s["id"], "selected_spans": s.get("selected_spans")} for s in sources],
        "recording_source_hashes": source_hashes, "recording_input_stamp": selection["selection_stamp"],
        "recording_complete": True, "dependency_blocks": dependency_blocks,
        "boundary_evidence": deepcopy(selection.get("boundary_evidence") or {}),
        "question_identity": deepcopy(identity), "membership": membership, "membership_hash": membership_hash,
        "protection_hash": protection, "status": "processing", "first_rx": sources[0]["first_rx"],
        "last_rx": sources[-1]["stable_at"], "role_decision": role_decision,
        "processing_settings_hash": processing_hash}
    signature = digest([base, context])
    existing = repo.stage_document_by_signature(sid, signature)
    if existing:
        return existing
    return {"base": base, "sources": sources, "dependencies": dependencies, "context": context,
        "signature": signature, "execution": service._execution_key(), "settlement": None,
        "protected": service._protected_overlap(sid, sources, membership)}


def document_current(repo, sid: str, document: dict, block: dict | None = None) -> bool:
    """정수 ID가 같은 자막과 녹음을 혼동하지 않고 원발언의 전체 hash를 대조한다."""
    from .live_stages import digest
    if (document.get("source_kind") != SOURCE_KIND or document.get("mode") != "closed_block_v1"
            or document.get("session_id") != sid or not recording_complete(repo.get_session(sid) or {})):
        return False
    block = block or repo.stage_block_record(sid, document.get("block_id"))
    if (not block or block.get("kind") != "question" or block.get("is_open")
            or repo.stage_block_stamp(block) != document.get("block_stamp")
            or digest(repo.stage_block_context(sid, block)) != document.get("context_hash")):
        return False
    selection = selection_for(repo, sid, block)
    if selection.get("status") != "ready" or selection.get("selection_stamp") != document.get("recording_input_stamp"):
        return False
    deps, stamps, membership = dependencies_for(repo, sid, block, selection)
    if (stamps != document.get("dependency_blocks") or membership != document.get("membership")
            or repo.stage_membership_stamp(sid, membership) != document.get("membership_hash")
            or digest(repo.stage_document_protection(sid, membership=membership)) != document.get("protection_hash")):
        return False
    sources = selection["sources"]
    return bool(document.get("recording_complete") is True
        and document.get("source_ids") == [s["id"] for s in sources]
        and document.get("source_revisions") == {str(s["id"]): s["revision"] for s in sources}
        and document.get("source_selection") == [{"id": s["id"], "selected_spans": s.get("selected_spans")} for s in sources]
        and document.get("recording_source_hashes") == {str(s["id"]): s["source_hash"] for s in [*deps, *sources]})
