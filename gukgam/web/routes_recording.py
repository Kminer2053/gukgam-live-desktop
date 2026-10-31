"""수집 PC 운영자만 사용하는 검증용 녹음 제어와 비공개 자료 다운로드."""
from __future__ import annotations

import asyncio
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import csrf_protect, require_operator
from .routes_api import _session_or_404


def _local_operator(request: Request):
    user = require_operator(request)
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or any(h in request.headers for h in ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip"))):
        raise HTTPException(403, "검증 녹음은 수집 PC의 로컬 운영자 화면에서만 사용할 수 있습니다")
    return user


router = APIRouter(dependencies=[Depends(csrf_protect), Depends(_local_operator)])


class _PrivateRecordingResponse(FileResponse):
    """전송 완료·끊김 모두 전용 임시 파일을 회수하고 다음 다운로드를 허용한다."""

    def __init__(self, archive, temporary, lock, **kwargs):
        super().__init__(archive, **kwargs)
        self.temporary, self.lock = temporary, lock

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                shutil.rmtree(self.temporary)
            finally:
                self.lock.release()


class RecordingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_seconds: int = Field(default=900, strict=True, ge=600, le=900)


def _service(request: Request, sid: str):
    rt = request.app.state.rt
    _session_or_404(rt, sid)
    return rt.audio_review.recordings


@router.get("/sessions/{sid}/audio-recording")
async def recording_status(request: Request, sid: str):
    try:
        return _service(request, sid).status(sid)
    except (ValueError, OSError):
        raise HTTPException(409, "녹음 보관 경로를 확인하세요") from None


@router.post("/sessions/{sid}/audio-recording")
async def recording_start(request: Request, sid: str, body: RecordingIn):
    try:
        return _service(request, sid).start(sid, body.duration_seconds)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except OSError:
        raise HTTPException(409, "녹음 보관 경로·권한·디스크 여유를 확인하세요") from None


@router.post("/sessions/{sid}/audio-recording/stop")
async def recording_stop(request: Request, sid: str):
    try:
        return _service(request, sid).stop(sid)
    except (ValueError, OSError):
        raise HTTPException(409, "녹음 저장 상태를 확인하세요") from None


@router.get("/sessions/{sid}/audio-recording/download")
async def recording_download(request: Request, sid: str,
                             recording_id: str = Query(pattern=r"^[a-f0-9]{32}$")):
    service = _service(request, sid)
    if service._download_lock.locked():
        raise HTTPException(409, "다른 녹음 자료를 묶고 있습니다. 잠시 후 다시 시도하세요")
    await service._download_lock.acquire()
    task = asyncio.create_task(asyncio.to_thread(service.bundle, sid, recording_id))
    try:
        archive, temporary = await asyncio.shield(task)
    except asyncio.CancelledError:
        # 스레드는 요청 취소로 중단되지 않는다. 생성이 끝난 뒤에도 회수를 보장한다.
        def clean_finished(finished):
            try:
                _archive, temporary = finished.result()
                shutil.rmtree(temporary)
            except Exception:
                pass
            finally:
                service._download_lock.release()
        task.add_done_callback(clean_finished)
        raise
    except (ValueError, OSError, KeyError):
        service._download_lock.release()
        raise HTTPException(409, "녹음·원자막 파일과 저장 무결성을 확인하세요") from None
    except BaseException:
        service._download_lock.release()
        raise
    return _PrivateRecordingResponse(archive, temporary, service._download_lock,
                                     media_type="application/zip", filename=f"recording-{recording_id}.zip",
                                     headers={"Cache-Control": "no-store"})
