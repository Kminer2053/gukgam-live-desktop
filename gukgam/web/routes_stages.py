"""원수신 실황과 종료 질의 전체 보정 판본 조회."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from .auth import csrf_protect, require_viewer
from .routes_api import _session_or_404
from .routes_recording import _local_operator

router = APIRouter(dependencies=[Depends(csrf_protect),Depends(require_viewer)])


class NoOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _service(request, sid):
    rt = request.app.state.rt
    _session_or_404(rt,sid)
    service = getattr(rt,"stages",None)
    if service is None:
        raise HTTPException(503,"자막 단계별 처리가 준비되지 않았습니다")
    return service


def _recent_snapshot(view, sid, *, before=None, limit=100, block_id=None):
    """유효 ID 권위는 유지하고 선택 보정본의 원행·옆 화자·출처만 한 번에 보낸다."""
    from .. import serialize
    from ..live_stages import public_source
    read_context = {}
    payload = view.snapshot(sid, before=before, limit=limit, block_id=block_id, _read_context=read_context)
    if "display_generation" in payload:
        return payload
    if (view.rt.repo.get_session(sid) or {}).get("source_type") == "file":
        return payload  # 파일의 표시본 초기화 전에는 자막 출처 확장 경로로 보내지 않는다.
    repo = view.rt.repo
    sources = {row["id"]: row for row in payload["sources"]}
    needed = set(sources)
    selected_blocks = {doc["block_id"] for doc in payload["documents"] if type(doc.get("block_id")) is int}
    if block_id is not None:
        selected_blocks.add(block_id)
    all_turns = read_context["live_turns"]
    selected_turns = [turn for turn in all_turns if turn.get("block_id") in selected_blocks
                      or needed.intersection(turn.get("source_ids") or [])]
    for turn in selected_turns:
        needed.update(turn.get("source_ids") or [])
        needed.update(int(uid) for uid in (turn.get("speaker_guess") or {}).get("source_revisions", {}) if str(uid).isdigit())
    guesses = read_context["source_guesses"]
    needed.update(int(ref) for uid in list(needed) for ref in
                  (guesses.get(uid) or {}).get("source_revisions", {}) if str(ref).isdigit())
    for source in repo.caption_sources_by_ids(sid, sorted(needed - set(sources))):
        sources[source["id"]] = public_source({**source, "speaker_guess": guesses.get(source["id"])})
    ids = {turn["id"] for turn in selected_turns}
    for bid in selected_blocks:
        ids.update(row["id"] for row in repo.list_utterances(sid, block_id=bid, limit=-1))
    for doc in payload["documents"]:
        stored = repo.get_stage_document(sid, doc["id"]) or {}
        ids.update((stored.get("membership") or {}).get("utterance_ids") or [])
        for proof in doc.get("source_supersessions") or []:
            ids.add(proof.get("utterance_id"))
            ids.update(row.get("utterance_id") for row in proof.get("replacement_utterances") or [])
    payload.update(sources=sorted(sources.values(), key=lambda row: row["id"]), live_turns=selected_turns,
                   utterances=[serialize.utterance(row) for row in repo.stage_utterances_by_ids(sid, list(ids))])
    return payload


@router.get("/sessions/{sid}/transcript-stages")
async def stages(request: Request,sid: str,before: int | None = Query(None,gt=0),limit: int = Query(100,ge=1,le=100),
                 block_id: int | None = Query(None,gt=0), recent: bool = False):
    service = _service(request,sid)
    def read(view):
        if block_id is not None and not view.rt.repo.stage_block_record(sid,block_id):
            raise HTTPException(404,"질의 블록을 찾을 수 없습니다")
        return (_recent_snapshot(view, sid, before=before, limit=limit, block_id=block_id) if recent
                else view.snapshot(sid,before=before,limit=limit,block_id=block_id))
    return await service.read_call(read)


@router.post("/sessions/{sid}/transcript-stages/blocks/{bid}/request",status_code=202,
             dependencies=[Depends(_local_operator)])
async def request_block(request: Request,sid: str,bid: int,body: NoOptions | None = None):
    """수집을 건드리지 않고 지정한 종료 질의를 기존 직렬 보정 큐에 넣는다."""
    service = _service(request,sid)
    rt = request.app.state.rt
    def validate(view):
        block = view.rt.repo.stage_block_record(sid,bid)
        if not block:
            raise HTTPException(404,"질의 블록을 찾을 수 없습니다")
        if block.get("kind") != "question" or block.get("is_open"):
            raise HTTPException(409,"종료된 질의 블록만 보정할 수 있습니다")
        if not view._configured() and view.builder is None:
            raise HTTPException(409,"종료 질의 보정 설정이 꺼져 있습니다")
        selection = view._selection(sid,block)
        if not view._ready(selection):
            raise HTTPException(409,"질의 범위와 입력 원문의 확정을 기다리고 있습니다")
        dependencies,_,membership = view._dependencies(sid,block,selection)
        if not view._sources_ready(dependencies,allow_unsettled=view._allows_unsettled(selection)):
            raise HTTPException(409,"질의 경계 원자막의 final·scd 확정을 기다리고 있습니다")
        if view._protected_overlap(sid,selection["sources"],membership):
            raise HTTPException(409,"사람이 수정·검수한 기록은 자동 보정으로 바꾸지 않습니다")
    await service.read_call(validate)
    service.on_block_closed(sid,bid)
    user = require_viewer(request)
    rt.repo.audit(user.actor,"transcript.stage.request",sid,{"block_id":bid})
    return {"state":"queued","block_id":bid,"detail":"같은 입력의 결과는 재실행하지 않고 기존 결과를 사용합니다"}


@router.post("/sessions/{sid}/transcript-stages/audio/enable",dependencies=[Depends(_local_operator)])
async def audio_enable(request: Request,sid: str,body: NoOptions | None = None):
    _service(request,sid)
    raise HTTPException(410,"음성 녹음 보정은 제거했습니다. 종료된 자막 블록 전체를 보정합니다.")


@router.post("/sessions/{sid}/transcript-stages/audio/disable",dependencies=[Depends(_local_operator)])
async def audio_disable(request: Request,sid: str,body: NoOptions | None = None):
    _service(request,sid)
    raise HTTPException(410,"음성 녹음 보정은 제거했습니다")
