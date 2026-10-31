"""닫힌 질의를 같은 판본의 화자·문장·요약 초안으로 조립한다."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import hashlib

from . import settings
from .db import now_ms
from .llm import prompts
from .llm.providers import (LocalMLXProvider, RulesProvider, ground_org_relevance,
                            ground_ref_uids, merge_rule_refs, rules_block_summary)
from .review_presentation import build_presentation
from .review_context import processing_signature
from .transcript_review import _original_turn, snapshot_hash, take_snapshot, validate_result


def latest_block_review(repo, sid: str, bid: int) -> dict | None:
    """전체 판본의 최신 상태만 선택하며 거절한 이전 초안을 되살리지 않는다."""
    select = getattr(repo, "latest_block_transcript_review", None)
    rows = [select(sid, bid)] if select else repo.list_transcript_reviews(sid, bid)
    for review in rows:
        if not review:
            return None
        payload = review.get("payload")
        if not isinstance(payload, dict):
            return None
        scope = payload.get("scope")
        if isinstance(scope, dict) and scope.get("kind") == "window":
            continue
        if scope and (not isinstance(scope, dict) or set(scope) - {"kind"} or scope.get("kind") != "block"):
            return None
        return review
    return None


def _accepted_signature(repo, sid: str, bid: int) -> str:
    """생성 중 창 채택·되돌리기를 포함한 사람의 표시 판본 변경을 감지한다."""
    rows = repo.list_utterances(sid, block_id=bid, final_only=True)
    return json.dumps([(r["id"], r.get("review_parts") or [], r.get("ts_start"), r.get("ts_end"),
                        r.get("seq"), r.get("source_order")) for r in rows],
                      ensure_ascii=False, sort_keys=True)


def requires_question_audio(review: dict | None) -> bool:
    """음성 대조용 초안은 근거가 사라져도 일반 자막 처리로 바꾸지 않는다."""
    payload = (review or {}).get("payload") or {}
    return payload.get("audio_evidence_required") is True or payload.get("model") == "질의 음성 대조"


def _manuscript_rules(block: dict, rows: list[dict], terms: list, *, provider: str = "rules") -> dict:
    """귀속 미확정 문맥을 규칙 대체가 위원의 요구나 답변자의 약속으로 확정하지 않는다."""
    confirmed = [r for r in rows if r.get("attribution_confirmed")]
    member = [r for r in confirmed if r["speaker_role"] == "member"]
    answer = [r for r in confirmed if r["speaker_role"] == "answer"]
    result = rules_block_summary(block, confirmed, terms, provider=provider)
    questions = rules_block_summary(block, member, terms, provider=provider)
    answers = rules_block_summary(block, answer, terms, provider=provider)
    result.update(question_points=questions["question_points"], answer_points=answers["answer_points"],
                  data_requests=questions["data_requests"], commitments=answers["commitments"])
    return result


def _hold_manuscript_attribution(summary: dict, rows: list[dict]) -> dict:
    """모델이 붙인 요구·약속 참조도 최종 발언 역할과 일치할 때만 유지한다."""
    by_id = {r["id"]: r for r in rows}
    held = []
    for key, roles in (("data_requests", {"member"}), ("commitments", {"answer"}),
                       ("issues", {"member", "answer"})):
        kept = []
        for item in summary.get(key) or []:
            row = by_id.get(item.get("utterance_id")) or {}
            if row.get("attribution_confirmed") and row.get("speaker_role") in roles:
                kept.append(item)
            else:
                held.append({**item, "kind": key, "reason": "최종 발언의 귀속 근거 확인 필요"})
        summary[key] = kept
    if held:
        summary["held_attributions"] = held
    return summary


async def prepare_integrated_review(analysis, job, block: dict) -> dict | None:
    """기존 원문 요약과 구별한 완료 초안만 새 검토 판본으로 원자적으로 공개한다."""
    repo = analysis.rt.repo
    if job.manual or block.get("is_open") or block.get("kind") != "question" \
            or analysis._protected(block) or not settings.get(repo, "transcript_review").get("enabled") \
            or not settings.get(repo, "transcript_review").get("auto_block") \
            or not analysis._llm_cfg().get("auto_block_summary", True) \
            or not hasattr(repo, "list_transcript_reviews"):
        return None
    if any(r.get("review_parts") for r in repo.list_utterances(job.sid, block_id=block["id"], final_only=True)):
        return None  # 사람이 채택한 중간 범위를 자동 전체 초안으로 가리지 않는다.
    source = latest_block_review(repo, job.sid, block["id"])
    if not source or source["status"] not in {"draft", "held", "failed", "timeout", "unavailable"}:
        return None
    snapshot = take_snapshot(repo, job.sid, block["id"])
    digest = snapshot_hash(snapshot)
    if source.get("input_hash") != digest:
        return None
    ready = (source.get("payload") or {}).get("presentation") or {}
    if ready.get("ready"):
        from .review_display import get_block_display
        display = get_block_display(repo, block)
        if display.get("ai_review"):
            return deepcopy(ready["summary"])
        return None
    accepted = _accepted_signature(repo, job.sid, block["id"])
    configuration = processing_signature(repo)
    question_audio = getattr(analysis.rt, "question_audio", None)
    audio_evidence = question_audio.evidence(job.sid, block["id"], digest, source["id"]) if question_audio else None
    if requires_question_audio(source) and not audio_evidence:
        return block.get("summary") or {}
    if audio_evidence:
        if analysis._llm_cfg().get("provider") != "local_mlx":
            return block.get("summary") or {}
        snapshot["audio_evidence"] = audio_evidence
        snapshot["realtime_corrections"] = deepcopy(audio_evidence.get("realtime_corrections", []))

    def current() -> bool:
        try:
            latest = latest_block_review(repo, job.sid, block["id"])
            current_block = repo.get_block(block["id"])
            return bool(settings.get(repo, "transcript_review").get("enabled")
                        and settings.get(repo, "transcript_review").get("auto_block")
                        and not analysis._question_audio_holds(job.sid, block["id"], completing=True)
                        and current_block and not current_block.get("is_open")
                        and current_block.get("kind") == "question" and not analysis._protected(current_block)
                        and processing_signature(repo) == configuration
                        and latest and latest["id"] == source["id"]
                        and latest["status"] == source["status"]
                        and (not audio_evidence or question_audio.evidence(job.sid, block["id"], digest, source["id"]) is not None)
                        and snapshot_hash(take_snapshot(repo, job.sid, block["id"])) == digest
                        and _accepted_signature(repo, job.sid, block["id"]) == accepted)
        except (KeyError, ValueError):
            return False

    base = deepcopy((source.get("payload") or {}).get("turns") or [])
    try:
        validate_result(snapshot, base)
    except (ValueError, KeyError, TypeError):
        base = [_original_turn(row) for row in snapshot["utterances"]]
    provider, cfg = await analysis._get_provider()
    if audio_evidence and not isinstance(provider, LocalMLXProvider):
        return block.get("summary") or {}
    editor = {"status": "skipped", "provider": "", "model": "",
              "reason": "로컬 생성 모델이 없어 검증된 화자 초안을 유지했습니다"}
    issues = deepcopy((source.get("payload") or {}).get("issues") or [])
    turns = base
    final_document = None
    two_stage = bool(audio_evidence and (source.get("payload") or {}).get("speaker_text_pipeline") in {1, 2})
    if two_stage and isinstance(provider, LocalMLXProvider):
        from .question_refinement import refine_question
        engine = getattr(getattr(analysis.rt, "reviews", None), "engine", None)
        decision_provider = getattr(engine, "provider", None)
        if decision_provider is not None or audio_evidence.get("whole_audio"):
            try:
                refined = await refine_question(snapshot, base, decision_provider, provider, is_current=current)
                if current() and hasattr(question_audio, "store_refinement"):
                    question_audio.store_refinement(job.sid, block["id"], source["id"], snapshot, refined)
                turns, editor = refined["turns"], refined["editor"]
                if refined.get("final_document"):
                    from .manuscript_presentation import public_document
                    editorial_snapshot = {**snapshot, "speaker_review": deepcopy(refined["speaker_review"])}
                    final_document = public_document(editorial_snapshot, refined["final_document"])
                issues.extend(refined.get("issues", []))
            except asyncio.CancelledError:
                raise
            except Exception:
                # 근거 보관까지 마치지 못한 판본은 공개하거나 후속 요약에 쓰지 않는다.
                turns = base
                final_document = None
                editor.update(status="held", reason="두 단계 검토·근거 보관을 완료하지 못해 기존 초안을 보존했습니다")
        else:
            editor.update(status="held", reason="Jev 화자 판정 제공자가 없어 본문 교정을 보류했습니다")
    elif isinstance(provider, LocalMLXProvider):
        from .transcript_editor import refine_transcript
        try:
            refined = await asyncio.wait_for(refine_transcript(snapshot, base, provider,
                max_tokens=min(2400, max(1600, int(cfg.get("max_tokens") or 1600)))), timeout=90)
            turns, editor = refined["turns"], refined["editor"]
            issues.extend(refined.get("issues") or [])
        except asyncio.CancelledError:
            raise
        except Exception:
            editor = {"status": "held", "provider": provider.name, "model": provider.model,
                      "reason": "로컬 문맥 보정을 완료하지 못해 기존 부분을 보존했습니다"}
    if not current():
        return (repo.get_block(block["id"]) or {}).get("summary") or {}
    presentation = build_presentation(snapshot, turns)
    rows = [r for r in presentation["source_rows"]
            if r.get("speaker_role") != "system" and (r.get("text") or "").strip()]
    ownership = None
    summary_block = block
    if final_document:
        from .manuscript_presentation import manuscript_scope, manuscript_summary_rows
        # 요약과 화면은 같은 최종 문서 판본을 사용한다. 원음 인용은 로컬 보고서에만 둔다.
        public_snapshot = {k: v for k, v in snapshot.items() if k != "audio_evidence"}
        ownership = manuscript_scope(public_snapshot, final_document)
        rows = manuscript_summary_rows(public_snapshot, final_document)
        summary_block = {**block, "questioner": ownership.get("questioner") or "질의자 확인 필요",
                         "questioner_party": ""}
        presentation["final_document"] = final_document
        from .question_audio import first_pass_proofs
        presentation["first_pass_proofs"] = first_pass_proofs(audio_evidence.get("realtime_corrections", []))
        covered = {r["id"] for c in audio_evidence.get("realtime_corrections", []) for r in c.get("rows", [])}
        presentation["first_pass_coverage"] = {"covered_ids": sorted(covered),
            "code_fallback_ids": [r["id"] for r in snapshot["utterances"] if r["id"] not in covered]}
        presentation["ownership"] = ownership
        presentation["ownership_context"] = [{"id": row["id"], "position": row.get("position"),
            "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
            **({"source_stamp": deepcopy(row["source_stamp"])} if "source_stamp" in row else {})}
            for row in audio_evidence["context"]]
    elif audio_evidence:
        from .speaker_lab.question_ownership import derive_question_ownership
        ownership = derive_question_ownership(snapshot, turns)
        included = {(p["utterance_id"], p["start"], p["end"]) for p in ownership["core_turns"]}
        rows = [r for r in rows if (r["id"], r["start"], r["end"]) in included]
        summary_block = {**block, "questioner": ownership.get("questioner") or "질의자 확인 필요",
                         "questioner_party": ""}
        presentation["ownership"] = ownership
        presentation["ownership_context"] = [{"id": row["id"], "position": row.get("position"),
            "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
            **({"source_stamp": deepcopy(row["source_stamp"])} if "source_stamp" in row else {})}
            for row in audio_evidence["context"]]
    ctx, terms, _departments = analysis._context(job.sid, repo.get_session(job.sid))
    summary = None
    if ownership and (ownership["status"] == "held" or editor.get("status") in {"held", "skipped", "protected"}):
        rows = []
        summary = {"one_line": "질의 범위·화자 보정안을 확인해야 하므로 자동 요약을 보류합니다",
                   "question_points": [], "answer_points": [], "issues": [], "data_requests": [],
                   "commitments": [], "keywords": [], "org_relevance": "none", "provider": "rules(범위 보류)"}
    if rows and not isinstance(provider, RulesProvider):
        system, user = prompts.block_messages(ctx, summary_block, rows, reviewed=True)
        if final_document:
            system += ("\n입력은 전체 음성·원자막을 대조한 검수 전 최종 전사 초안이다. "
                       "질문자의 요구를 답변자의 약속으로 바꾸지 않는다. 서로 다른 주제를 빠뜨리지 말고 "
                       "수치·부정·시점을 보존한다. 귀속 확인 필요 발언은 문맥으로 읽되 특정 의원 질문이나 "
                       "답변자 약속으로 확정하지 않는다. 원문 참조는 해당 최종 문장에 붙은 번호만 사용한다.")
            # 기존 원자막 요약의 가운데 생략을 최종 전사에 적용하지 않는다.
            user = "\n".join([prompts.TASK_BLOCK, ctx.meeting_line(), prompts.block_header(summary_block),
                              "[최종 전사 초안]", *prompts.utterance_lines(rows, max_chars=100_000),
                              "전체 주제를 읽고 지정한 JSON 형식으로만 답하십시오."])
        try:
            summary, ms = await analysis._ask(provider, system, user, int(cfg.get("max_tokens") or 1200),
                lambda value: prompts.normalize_block_summary(value, {r["id"] for r in rows}))
            ground_org_relevance(summary, rows, terms)
            ground_ref_uids(summary, [r for r in rows if r.get("attribution_confirmed")] if final_document else rows)
            merge_rule_refs(summary, _manuscript_rules(summary_block, rows, terms) if final_document
                            else rules_block_summary(summary_block, rows, terms))
            summary.update(provider=provider.name, model=provider.model, generated_at=now_ms(), latency_ms=ms)
            analysis._record(provider.name, ms, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            summary = None
    if summary is None:
        summary = (_manuscript_rules if final_document else rules_block_summary)(summary_block, rows, terms,
                                      provider="rules" if isinstance(provider, RulesProvider) else "rules(대체)")
    summary.update(input_source="ai_review_draft", input_hash=digest)
    if final_document:
        from .manuscript_presentation import map_manuscript_summary_refs
        _hold_manuscript_attribution(summary, rows)
        summary = map_manuscript_summary_refs(summary, rows)
        summary.update(input_stage="question_audio_final", manuscript_input_hash=final_document["input_hash"],
                       coverage_status="partial" if ownership.get("unresolved_source_ids") or summary.get("held_attributions") else "review_draft",
                       unresolved_source_ids=deepcopy(ownership.get("unresolved_source_ids", [])))
        if summary["coverage_status"] == "partial":
            summary["review_note"] = "귀속 미확정 발언도 문맥에 포함했습니다. 해당 부분의 질문·답변 귀속은 검수가 필요합니다."
    if not current():
        return (repo.get_block(block["id"]) or {}).get("summary") or {}
    presentation.update(schema_version=1, ready=True, input_hash=digest, summary=summary, editor=editor,
                        processing_hash=configuration)
    payload = {**deepcopy(source.get("payload") or {}), "status": "draft", "input_hash": digest,
               "turns": turns, "issues": issues, "presentation": presentation,
               "source_review_id": source["id"], "source_turns": base,
               "generated_at": now_ms()}
    payload.pop("scope", None)
    validate_result(snapshot, turns)
    # 모델 대기 후부터 저장·공개까지 await 없이 동일 판본을 유지한다.
    with repo.db.transaction():
        if not current():
            return (repo.get_block(block["id"]) or {}).get("summary") or {}
        saved = repo.save_transcript_review(job.sid, block["id"], payload)
        repo.set_transcript_review_status(source["id"], "stale")
    from .review_display import get_block_display
    analysis.rt.bus.publish(job.sid, "block.upsert", get_block_display(repo, repo.get_block(block["id"])))
    review_service = getattr(analysis.rt, "reviews", None)
    if review_service is not None:
        review_service._publish_review(job.sid, saved)
    if audio_evidence:
        question_audio.on_integrated_review(job.sid, block["id"], source["id"], payload)
    session = repo.get_session(job.sid) or {}
    if session.get("status") in {"ended", "paused"}:
        from .llm.service import Job, PRIO_SESSION, SESSION_DEBOUNCE_SEC
        analysis._schedule(Job("session", job.sid, priority=PRIO_SESSION), SESSION_DEBOUNCE_SEC)
    return summary
