"""로컬 관리자 전용 종료 세션 단독 HTML 다운로드."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..repo import Repo
from ..reports.offline import collect_offline_session, offline_filename, render_offline_session
from .auth import require_operator, User
from .routes_api import _content_disposition

router = APIRouter()


def local_archive_operator(request: Request, user: User = Depends(require_operator)) -> User:
    """실제 루프백 접속만 허용한다. 릴레이·전달 헤더로 우회할 수 없다."""
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or any(key in request.headers for key in ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip"))):
        raise HTTPException(403, "단독 HTML은 로컬 관리자 화면에서만 내려받을 수 있습니다")
    return user


@router.get("/sessions/{sid}/offline.html")
async def offline_session(request: Request, sid: str, user: User = Depends(local_archive_operator)):
    rt = request.app.state.rt

    def collect():
        with rt.repo.db.read_connection() as db:
            with db.transaction():
                return collect_offline_session(Repo(db), sid, user.actor)

    try:
        payload = await asyncio.to_thread(collect)
    except KeyError:
        raise HTTPException(404, "세션을 찾을 수 없습니다") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    document = await asyncio.to_thread(render_offline_session, payload)
    rt.repo.audit(user.actor, "session.offline_export", sid, {"counts": payload["manifest"]["counts"]})
    return Response(document, media_type="text/html", headers={
        "Content-Disposition": _content_disposition(offline_filename(payload)),
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })
