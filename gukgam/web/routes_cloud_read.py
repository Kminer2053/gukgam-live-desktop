"""인증·기존 API 검증을 재사용하는 원본 PC의 공개 읽기 전용 어댑터."""
from __future__ import annotations

import json
import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..cloud import policy
from ..cloud.source_projection import project
from .auth import COOKIE_NAME, User, require_viewer

router = APIRouter()


@router.get("/cloud-read/{path:path}")
async def source_read(request: Request, path: str, user: User = Depends(require_viewer)):
    """소켓을 열지 않고 같은 앱의 허용된 GET을 호출한 뒤 공개 필드만 돌려준다."""
    rule = policy.match("GET", request.url.path, request.url.query)
    if rule is None or not rule.allows(user.role):
        raise HTTPException(403, "허용되지 않은 원본 조회입니다")
    config = request.app.state.config
    if getattr(config, "cloud_read_mode", "mirror") != "source":
        raise HTTPException(503, "원본 PC의 직접 중계 설정이 아직 적용되지 않았습니다")
    target = "/api/" + path + ("?" + request.url.query if request.url.query else "")
    headers = {"Cookie": COOKIE_NAME + "=" + request.app.state.auth.issue(user), "X-Requested-With": "gukgam"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app), base_url=str(request.base_url),
                                 headers=headers, follow_redirects=False) as client:
        response = await client.get(target)
    if response.status_code != 200:
        error = {"detail": "원본 자료를 조회하지 못했습니다"}
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        if (response.status_code == 503 and isinstance(payload, dict)
                and payload.get("display_state") == "updating"
                and type(payload.get("display_generation")) is int
                and 0 <= payload["display_generation"] <= 9_007_199_254_740_991):
            # 갱신 중이라는 공개 세대만 이어준다. 내부 예외·모델 진단은 전송하지 않는다.
            error.update(display_state="updating", display_generation=payload["display_generation"])
        return JSONResponse(error, status_code=response.status_code,
                            headers={"Retry-After": "1"} if "display_state" in error else None)
    try:
        data = response.json()
        current_session = data.get("session", data)
        if current_session.get("source_type") == "replay":
            raise HTTPException(404, "세션을 찾을 수 없습니다")
        if path in {"items", "search"}:
            # 미러에서는 애초 제외하던 재생 자료를 전역 원본 검색에서도 노출하지 않는다.
            def filter_replay():
                repo = request.app.state.rt.repo
                selected = request.query_params.get("session_id")
                if selected and (repo.get_session(selected) or {}).get("source_type") == "replay":
                    raise HTTPException(404, "세션을 찾을 수 없습니다")
                types = {sid: (repo.get_session(sid) or {}).get("source_type")
                         for sid in {row.get("session_id") for row in data.get("items", [])}}
                return [row for row in data.get("items", []) if types.get(row.get("session_id")) not in {None, "replay"}]
            data["items"] = await asyncio.to_thread(filter_replay)
        result = project(path, data, sensitive=config.cloud_mirror_sensitive)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, KeyError):
        raise HTTPException(502, "원본 조회 응답을 확인할 수 없습니다") from None
    if len(encoded) > 3_000_000:
        raise HTTPException(413, "선택한 자료가 너무 큽니다. 더 작은 범위를 선택하세요")
    return JSONResponse(result)
