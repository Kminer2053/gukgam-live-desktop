"""호환 서버의 주소·키 전송 범위를 정규화한다. 연결하거나 환경을 읽지 않는다."""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

PRESETS = {
    "custom": "", "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1", "openai": "https://api.openai.com/v1",
}


def normalize_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("AI 서버 주소를 확인하세요.")
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(ord(c) <= 32 for c in value)):
            raise ValueError
        parsed.port
    except ValueError:
        raise ValueError("AI 서버 주소에는 http(s) 주소만 입력하세요. 키·로그인정보·질의문은 넣지 마세요.") from None
    if parsed.path.rstrip("/").endswith(("/chat/completions", "/responses")):
        raise ValueError("AI 서버 주소는 /v1까지 입력하세요. 개별 요청 경로는 붙이지 마세요.")
    return value


def connection_url(cfg: dict) -> str:
    mode = cfg.get("openai_connection") or "custom"
    if not isinstance(mode, str) or mode not in PRESETS:
        raise ValueError("AI 연결 방식은 Ollama·LM Studio·OpenAI·다른 호환 서버 중에서 선택하세요.")
    return normalize_base_url(cfg.get("openai_base_url") or PRESETS[mode])


def credential_for_endpoint(cfg: dict, *, endpoint: str) -> str:
    key = cfg.get("openai_api_key") or ""
    if not isinstance(key, str) or len(key) > 8192 or any(c in key for c in "\r\n\x00"):
        raise ValueError("API 키 형식을 확인하세요.")
    if key and (key in unquote(endpoint) or key in str(cfg.get("openai_model") or "")):
        raise ValueError("API 키는 서버 주소가 아닌 API 키 칸에만 입력하세요.")
    binding = cfg.get("openai_key_base_url") or ""
    if key and binding == "unbound":
        raise ValueError("서버 주소를 정한 뒤 이 서버의 API 키를 다시 입력하거나 저장된 키를 삭제하세요.")
    if key and binding and normalize_base_url(binding) != endpoint:
        raise ValueError("서버 주소가 바뀌었습니다. 이 서버의 API 키를 다시 입력하거나 저장된 키를 삭제하세요.")
    return key
