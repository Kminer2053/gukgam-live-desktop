"""회의 자료 없이 선택한 AI에 짧은 예문 하나로 연결을 확인한다."""
from __future__ import annotations

import asyncio
import time

from .providers import AnthropicProvider, RulesProvider, make_provider, parse_json

NOTICE = "연결을 확인하지 못했습니다. 서버 실행 상태·주소·모델 이름·API 키와 이용 한도를 확인하세요."
PROBE_TIMEOUT_SECONDS = 90


async def test_connection(cfg: dict, rt, *, factory=None) -> dict:
    provider = None
    started = time.perf_counter()
    kind = cfg.get("provider") if cfg.get("provider") in {"rules", "local_mlx", "desktop_chatgpt", "openai_compat", "anthropic"} else "unknown"
    try:
        provider = (factory or make_provider)(cfg, rt)
        if isinstance(provider, RulesProvider):
            return {"ok": False, "provider": provider.name, "error": "규칙 기반은 AI 서버 연결을 사용하지 않습니다."}
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            options = {"connection_probe": True} if isinstance(provider, AnthropicProvider) else {}
            text = await provider.complete('연결 확인입니다. 다른 설명 없이 {"ok":true} JSON만 답하세요.',
                                           "연결을 확인합니다.", max_tokens=128, json_mode=False,
                                           execution_timeout=PROBE_TIMEOUT_SECONDS, **options)
        if parse_json(text).get("ok") is not True:
            return {"ok": False, "provider": kind, "error": NOTICE}
        keys = [str(cfg.get(field) or "") for field in ("openai_api_key", "anthropic_api_key")]
        model = str(provider.model or "")
        if any(key and key in model for key in keys):
            return {"ok": False, "provider": kind, "error": NOTICE}
        return {"ok": True, "provider": provider.name, "model": model,
                "latency_ms": int((time.perf_counter() - started) * 1000), "synthetic_only": True}
    except Exception:
        return {"ok": False, "provider": kind, "error": NOTICE}
    finally:
        if provider is not None:
            try:
                await provider.close()
            except Exception:
                pass
