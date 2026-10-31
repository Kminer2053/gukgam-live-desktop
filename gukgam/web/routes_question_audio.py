"""수집 PC에서 의원별 질의 종료 후 음성 대조를 켜고 초안을 조회한다."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict

from .auth import csrf_protect
from .routes_api import _session_or_404
from .routes_recording import _local_operator


router = APIRouter(dependencies=[Depends(csrf_protect), Depends(_local_operator)])


class _NoOptions(BaseModel):
    """버튼 요청으로 수집원·설정·모델 실행 범위를 바꾸지 않는다."""

    model_config = ConfigDict(extra="forbid")


def _service(request: Request, sid: str):
    rt = request.app.state.rt
    _session_or_404(rt, sid)
    service = getattr(rt, "question_audio", None)
    if service is None:
        raise HTTPException(503, "질의 음성 대조가 아직 준비되지 않았습니다")
    return service


@router.get("/sessions/{sid}/question-audio/status")
async def question_audio_status(request: Request, sid: str):
    try:
        return _service(request, sid).status(sid)
    except (ValueError, OSError):
        raise HTTPException(409, "질의 음성 대조의 저장 상태를 확인하세요") from None


@router.post("/sessions/{sid}/question-audio/enable")
async def question_audio_enable(request: Request, sid: str, body: _NoOptions | None = None):
    _session_or_404(request.app.state.rt, sid)
    raise HTTPException(410, "음성 녹음 보정은 제거했습니다. 종료된 자막 블록 전체를 보정합니다.")


@router.post("/sessions/{sid}/question-audio/disable")
async def question_audio_disable(request: Request, sid: str, body: _NoOptions | None = None):
    try:
        return _service(request, sid).disable(sid)
    except (ValueError, OSError):
        raise HTTPException(409, "질의 음성 대조의 종료 상태를 확인하세요") from None


@router.get("/sessions/{sid}/question-audio/blocks/{bid}")
async def question_audio_report(request: Request, sid: str, bid: int = Path(gt=0)):
    try:
        return _service(request, sid).report(sid, bid)
    except KeyError:
        raise HTTPException(404, "이 회의의 질의 음성 대조 결과를 찾을 수 없습니다") from None
    except (ValueError, OSError):
        raise HTTPException(409, "질의 음성 대조 결과를 아직 열 수 없습니다") from None
