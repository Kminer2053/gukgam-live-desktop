"""Jev 의사결정 API. 생성·전사 없이 제한된 선택지만 판정한다."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"
MAX_QUESTIONS = 64
MAX_OPTIONS = 12
MAX_STATE_BYTES = 48_000
MAX_RESPONSE_BYTES = 96_000
REQUEST_SECONDS = 20.0
_QUESTION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class DecisionError(RuntimeError):
    """본문·키를 포함하지 않는 제공자 실패 상태."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class DecisionProvider(Protocol):
    """생성·전사와 분리된 제한 선택 제공자. 실제 실행은 비동기로 기다린다."""

    name: str
    model: str

    @property
    def available(self) -> bool: ...

    @property
    def provenance(self) -> dict[str, Any]: ...

    async def evaluate(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]: ...
    async def close(self) -> None: ...


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DecisionError("invalid_response", "의사결정 응답의 숫자 형식이 잘못되었습니다")
    result = float(value)
    if not 0 <= result <= 1:
        raise DecisionError("invalid_response", "의사결정 응답의 확률 범위가 잘못되었습니다")
    return result


def validate_questions(state: Any, questions: dict[str, Any]) -> None:
    """허용 질문·입력 크기를 전송 전에 검사한다."""
    if not isinstance(state, (str, dict, list)):
        raise DecisionError("invalid_request", "검토 입력 형식이 잘못되었습니다")
    try:
        size = len(json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise DecisionError("invalid_request", "검토 입력을 JSON으로 표현할 수 없습니다") from None
    if size > MAX_STATE_BYTES:
        raise DecisionError("input_limit", "검토 입력 크기 한도를 넘었습니다")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise DecisionError("input_limit", "검토 질문 수 한도를 넘었습니다")
    for name, question in questions.items():
        if not isinstance(name, str) or not _QUESTION_NAME.fullmatch(name) or not isinstance(question, dict):
            raise DecisionError("invalid_request", "검토 질문 식별자가 잘못되었습니다")
        if set(question) - {"type", "instructions", "criteria"}:
            raise DecisionError("invalid_request", "지원하지 않는 질문 필드입니다")
        instruction = question.get("instructions", "")
        if not isinstance(instruction, str) or len(instruction) > 2_000:
            raise DecisionError("input_limit", "검토 질문 길이 한도를 넘었습니다")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_OPTIONS:
                raise DecisionError("input_limit", "검토 선택지 수 한도를 넘었습니다")
            if any(not isinstance(k, str) or not k or len(k) > 120 or not isinstance(v, str)
                   or len(v) > 2_000 for k, v in criteria.items()):
                raise DecisionError("invalid_request", "검토 선택지 형식이 잘못되었습니다")
        elif kind == "noul":
            if criteria is not None and (not isinstance(criteria, dict)
                    or set(criteria) != {"true", "false"}
                    or any(not isinstance(v, str) or len(v) > 2_000 for v in criteria.values())):
                raise DecisionError("invalid_request", "참거짓 질문 기준이 잘못되었습니다")
        else:
            raise DecisionError("invalid_request", "검토는 선택·참거짓 질문만 지원합니다")
    if len(json.dumps(questions, ensure_ascii=False).encode("utf-8")) > MAX_STATE_BYTES:
        raise DecisionError("input_limit", "검토 질문 크기 한도를 넘었습니다")


def validate_answer_set(response: Any, questions: dict[str, Any], *, model: str) -> dict[str, Any]:
    """제공자 공통의 모델·질문 ID·순서·확률 계약만 확인한다."""
    if not isinstance(response, dict) or response.get("model") != model:
        raise DecisionError("invalid_response", "검토 모델 버전이 일치하지 않습니다")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise DecisionError("invalid_response", "검토 응답 질문 목록이 일치하지 않습니다")
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise DecisionError("invalid_response", "검토 응답 종류가 일치하지 않습니다")
        if question["type"] == "noul":
            _number(answer.get("noul"))
            continue
        probabilities, choice = answer.get("probabilities"), answer.get("choice")
        if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]):
            raise DecisionError("invalid_response", "선택 응답 후보가 일치하지 않습니다")
        if not isinstance(choice, str) or choice not in probabilities:
            raise DecisionError("invalid_response", "선택 응답이 허용 후보를 벗어났습니다")
        values = [_number(v) for v in probabilities.values()]
        if abs(sum(values) - 1) > 0.02 or abs(float(probabilities[choice]) - max(values)) > 0.001:
            raise DecisionError("invalid_response", "선택 응답 확률이 일치하지 않습니다")
    return answers


def validate_jev_answers(response: Any, questions: dict[str, Any]) -> dict[str, Any]:
    """Jev 고유 사용량·정규화 확신 통계를 공통 계약과 별도로 검사한다."""
    answers = validate_answer_set(response, questions, model=JEV_MODEL)
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(isinstance(usage.get(k), bool)
            or not isinstance(usage.get(k), int) or usage[k] < 0
            for k in ("input_tokens", "output_tokens")):
        raise DecisionError("invalid_response", "검토 응답 사용량 형식이 잘못되었습니다")
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise DecisionError("invalid_response", "검토 응답 종류가 일치하지 않습니다")
        if question["type"] == "noul":
            if set(answer) != {"type", "noul"}:
                raise DecisionError("invalid_response", "참거짓 응답 형식이 잘못되었습니다")
            _number(answer["noul"])
            continue
        if set(answer) != {"type", "choice", "confidence", "probabilities"}:
            raise DecisionError("invalid_response", "선택 응답 형식이 잘못되었습니다")
        probabilities, choice = answer["probabilities"], answer["choice"]
        if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]):
            raise DecisionError("invalid_response", "선택 응답 후보가 일치하지 않습니다")
        if not isinstance(choice, str) or choice not in probabilities:
            raise DecisionError("invalid_response", "선택 응답이 허용 후보를 벗어났습니다")
        values = [_number(v) for v in probabilities.values()]
        top = max(values)
        confidence = _number(answer["confidence"])
        expected = (top - 1 / len(values)) / (1 - 1 / len(values))
        if abs(sum(values) - 1) > 0.02 or abs(float(probabilities[choice]) - top) > 0.001:
            raise DecisionError("invalid_response", "선택 응답 확률이 일치하지 않습니다")
        if abs(confidence - expected) > 0.025:
            raise DecisionError("invalid_response", "선택 응답 확신 통계가 일치하지 않습니다")
    return answers


def validate_answers(response: Any, questions: dict[str, Any]) -> dict[str, Any]:
    """기존 Jev 호출자의 공개 이름을 보존한다."""
    return validate_jev_answers(response, questions)


class JevDecisionProvider:
    """고정 주소·버전·시간 제한. 대기열 없이 동시 호출 1건만 허용한다."""

    model = JEV_MODEL
    name = "jev"

    @property
    def provenance(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "confidence_kind": "jev_normalized"}

    @property
    def decision_metadata(self) -> dict[str, Any]:
        return self.provenance

    def __init__(self, api_key: str | None = None, *, client: httpx.AsyncClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        key = os.environ.get("GUKGAM_TYPESAFE_API_KEY", "") if api_key is None else api_key
        self._key = key.strip() if isinstance(key, str) else ""
        if any(ord(c) < 32 or ord(c) > 126 for c in self._key):
            self._key = ""
        self._client = client
        self._owns_client = client is None
        self._transport = transport
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def evaluate(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        validate_questions(state, questions)
        if not self.available:
            raise DecisionError("unavailable", "Jev API 키가 설정되지 않았습니다")
        if self._lock.locked():
            raise DecisionError("busy", "Jev 검토가 진행 중입니다")
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(transport=self._transport, follow_redirects=False,
                    trust_env=False, timeout=httpx.Timeout(REQUEST_SECONDS, connect=5.0))
            try:
                response = await asyncio.wait_for(self._request(state, questions), timeout=REQUEST_SECONDS)
            except (asyncio.TimeoutError, httpx.TimeoutException):
                raise DecisionError("timeout", "Jev 검토 응답 시간 한도를 넘었습니다") from None
            except httpx.HTTPError:
                raise DecisionError("failed", "Jev 검토 연결에 실패했습니다") from None
            return validate_answers(response, questions)

    async def _request(self, state: Any, questions: dict[str, Any]) -> Any:
        assert self._client is not None
        async with self._client.stream("POST", JEV_URL,
                headers={"Authorization": "Bearer " + self._key},
                json={"model": self.model, "state": state, "questions": questions},
                follow_redirects=False) as result:
            if result.status_code != 200:
                status = "unavailable" if result.status_code in (401, 403) else "failed"
                raise DecisionError(status, "Jev 검토 요청을 완료하지 못했습니다")
            chunks, size = [], 0
            async for chunk in result.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise DecisionError("invalid_response", "Jev 응답 크기 한도를 넘었습니다")
                chunks.append(chunk)
            try:
                return json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise DecisionError("invalid_response", "Jev 응답 JSON 형식이 잘못되었습니다") from None

    async def close(self) -> None:
        async with self._lock:
            if self._owns_client and self._client is not None:
                await self._client.aclose()
                self._client = None


class NullDecisionProvider:
    """추가 모델을 선택하지 않은 상태를 실행 성공과 구별한다."""

    name = "none"
    model = ""
    available = False

    @property
    def provenance(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model}

    @property
    def decision_metadata(self) -> dict[str, Any]:
        return self.provenance

    async def evaluate(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        raise DecisionError("unavailable", "추가 역할 판정 모델을 사용하지 않습니다")

    async def close(self) -> None:
        pass


def selected_decision_provider(settings: dict[str, Any]) -> str:
    """명시 선택이 우선이며 기존 use_jev만 있는 저장 설정은 호환한다."""
    selected = settings.get("role_decision_provider")
    if isinstance(selected, str) and selected in {"none", "jev", "julia"}:
        return selected
    return "jev" if settings.get("use_jev") is True else "none"


def decision_provider_metadata(settings: dict[str, Any]) -> dict[str, Any]:
    selected = selected_decision_provider(settings)
    if selected == "julia":
        from .julia import julia_provenance
        return julia_provenance(settings)
    return JevDecisionProvider("").provenance if selected == "jev" else NullDecisionProvider().provenance


def create_decision_provider(*, settings: dict[str, Any], data_dir: Path) -> DecisionProvider:
    selected = selected_decision_provider(settings)
    if selected == "julia":
        from .julia import JuliaDecisionProvider
        return JuliaDecisionProvider(data_dir, settings=settings)
    return JevDecisionProvider() if selected == "jev" else NullDecisionProvider()
