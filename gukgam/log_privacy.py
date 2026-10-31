"""실행 진단에 원문 예외 대신 고정된 오류 종류만 남긴다."""
from __future__ import annotations

import re

import httpx


_ERROR_KINDS = frozenset({
    "AttributeError", "CancelledError", "ConnectionError", "FileNotFoundError",
    "ImportError", "ModuleNotFoundError", "OSError", "PermissionError",
    "RuntimeError", "TimeoutError", "TypeError", "ValueError",
    "DatabaseError", "IntegrityError", "OperationalError",
    "HTTPError", "HTTPStatusError", "ConnectError", "ConnectTimeout",
    "ReadError", "ReadTimeout", "RemoteProtocolError", "WriteError", "WriteTimeout",
    "LLMError", "LLMParseError", "LLMRequestStale",
})


def exception_label(error: BaseException) -> str:
    """예외 메시지·인자·경로와 임의의 사용자 정의 종류를 진단에 싣지 않는다."""
    name = type(error).__name__
    return name if name in _ERROR_KINDS else "Exception"


AI_FAILURE_NOTICE = "AI 처리를 완료하지 못했습니다. 앱에서 연결 상태와 이용 한도를 확인하세요."
_TIMEOUT_MESSAGE = re.compile(r"로컬 모델 (?:기동|응답) 시간 초과\([0-9]{1,6}초\)")
_PROVIDER_MESSAGES = {
    "OpenAI 호환 서버 주소(openai_base_url)가 설정되지 않았습니다": "AI 연결 주소를 확인하세요.",
    "Anthropic API 키가 올바르지 않습니다": "AI 연결 인증을 확인하세요.",
    "Anthropic API 사용량 한도 초과(429)": "AI 이용 한도를 확인하세요.",
    "데스크톱 ChatGPT 연결이 준비되지 않았습니다": "앱에서 ChatGPT 연결 상태를 확인하세요.",
    "모델이 요청을 거절했습니다(refusal)": "AI가 요청을 처리하지 못했습니다.",
}
_PUBLIC_ERRORS = frozenset({
    AI_FAILURE_NOTICE, "시간 초과", "AI 연결 상태를 확인하세요.", "AI 응답 형식을 확인하세요.",
    *_PROVIDER_MESSAGES.values(),
    "AI 분석 서비스가 종료되었습니다",
    "요약 중 원문·화자 판본이 바뀌어 이전 결과를 저장하지 않았습니다",
    "회의 요약 중 정리 초안·원문·설정이 바뀌어 이전 결과를 저장하지 않았습니다",
    "회의 요약 중 선택한 발언·기존 요약·기관 또는 제공자 맥락이 바뀌어 이전 결과를 저장하지 않았습니다",
})


def failure_reason(error: BaseException) -> str:
    """저장 요약에도 예외 원문 대신 유한한 원인만 남긴다."""
    from .llm.providers import LLMError, LLMParseError

    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "시간 초과"
    if isinstance(error, (ConnectionError, httpx.NetworkError)):
        return "AI 연결 상태를 확인하세요."
    if isinstance(error, (LLMParseError, ValueError)):
        return "AI 응답 형식을 확인하세요."
    if isinstance(error, LLMError) and len(error.args) == 1 and isinstance(error.args[0], str):
        message = error.args[0]
        if _TIMEOUT_MESSAGE.fullmatch(message):
            return "시간 초과"
        if message in _PROVIDER_MESSAGES:
            return _PROVIDER_MESSAGES[message]
    return AI_FAILURE_NOTICE


def public_error(value: object) -> str | None:
    """이전 내부 오류 값도 health·상태·SSE에 그대로 내보내지 않는다."""
    if value is None or value == "":
        return None
    return value if isinstance(value, str) and value in _PUBLIC_ERRORS else AI_FAILURE_NOTICE
