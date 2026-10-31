"""운영 설정(DB 저장, 관리자 화면에서 변경). 기본값과 정규화를 한곳에서 관리한다."""
from __future__ import annotations

import copy
import os
from typing import Any

from .repo import Repo

DEFAULTS: dict[str, Any] = {
    "transcript_review": {
        "enabled": False,          # 키가 있어도 명시적으로 켠 경우만 외부 판단
        "auto_block": True,
        "role_decision_provider": "none",  # none|jev|julia. 후속 ChatGPT 보정·요약은 그대로 유지한다
        "use_jev": False,          # 이전 설정·호출자의 호환용. 새 선택과 함께 정규화한다
        "julia_threads": 2,
        "julia_max_tokens": 2048,
        "julia_load_timeout_seconds": 60,
        "julia_request_timeout_seconds": 20,
    },
    "audio_review": {
        "enabled": False,          # 자막과 별도로 제한된 원음 수신
        "buffer_seconds": 600,
        "max_clip_seconds": 30,
        "quota_mb": 64,
        "verify_transcription": False,
    },
    # 우리 기관 — 언급 감지·상황보고 머리말에 쓴다
    "org": {
        "name": "",                 # 예: 한국도로공사
        "aliases": [],              # 예: ["도로공사", "도공"]
        "head_title": "",           # 예: 사장
        "head_name": "",
    },
    # 감시 키워드: [{"keyword": "하이패스", "category": "project"}]
    "watch_keywords": [],
    # 담당부서 자동 배정 규칙: [{"name": "계약처", "keywords": ["하도급", "발주"]}]
    "departments": [],
    # AI 요약·분석 엔진
    "llm": {
        "provider": "local_mlx",    # local_mlx|desktop_chatgpt|openai_compat|anthropic|rules
        "desktop_model": "",        # Electron이 연결한 계정의 선택 모델, 빈 값이면 계정 기본값
        "local_model": "mlx-community/gemma-4-e4b-it-4bit",
        "openai_base_url": "",      # 예: http://10.0.0.5:8000/v1 (사내 vLLM/Ollama)
        "openai_connection": "custom",  # custom|ollama|lmstudio|openai. 실행 제공자는 openai_compat.
        "openai_model": "",
        "openai_api_key": "",
        "openai_key_base_url": "",  # 키를 입력한 서버 주소. 다른 서버로 기존 키를 넘기지 않는다.
        "anthropic_model": "claude-sonnet-5-5",
        "anthropic_api_key": "",
        "auto_block_summary": True,  # 질의 블록이 끝나면 자동 요약
        "rolling_interval_sec": 300,  # 롤링 요약 주기(수집 중)
        "max_tokens": 1200,
    },
    # 음성인식(국회 AI자막 수신 외 경로에서 사용)
    "stt": {
        "provider": "mlx_whisper",  # mlx_whisper|nemotron_cpu|none. Windows 최초 설치는 none.
        "nemotron_cpu_auto": True,  # 현재 PC에서 허용된 논리 CPU 전체를 자동 사용한다.
        "nemotron_cpu_cores": 2,  # 자동을 끈 경우의 소유 작업자 논리 CPU 상한.
        "nemotron_load_timeout_seconds": 180,
        "nemotron_request_timeout_seconds": 60,
        "diarize_recordings": True,  # 녹음 전체의 목소리 경계를 구한 뒤 전사한다.
        # 프로젝트 models/ 아래 로컬 경로 우선(폐쇄망), 없으면 HF repo id 로 내려받음
        "model": "models/whisper-large-v3-turbo",
        "language": "ko",
        "prompt": "국정감사 질의응답. 위원장님, 위원님, 질의하겠습니다. 답변드리겠습니다.",
        "glossary": [],             # 인식 보정용 고유명사(위원·기관·사업명)
        "corrections": {"지뢰하겠습니다": "질의하겠습니다", "지례하겠습니다": "질의하겠습니다"},  # 사후 치환
        "vad_min_silence_ms": 400,
        "partial_every_sec": 1.5,
        "max_segment_sec": 15,
    },
    # 보존·보안 — gukgam.maintenance 가 1시간마다 기한 지난 파일·기록을 지운다
    "retention": {
        "audio_days": 7,            # 원음(data/audio) 보존 일수. 0 = 새로 저장하지 않음(이미 있는 파일은 기한 없이 둠)
        "raw_days": 30,             # 국회 자막 원본(data/raw/{sid}.jsonl) 보존 일수. 0 = 기한 없이 보관
        "transcript_days": 0,       # 종료된 세션 기록(발언·요약·후속조치) 보존 일수. 0 = 무기한
    },
    # 국회 의사중계 연동
    "assembly": {
        "auto_detect": True,        # 대시보드에서 오늘 우리 기관 감사 일정을 자동 조회·표시(끄면 국회 일정 조회 안 함)
        "auto_stop_minutes": 30,    # 산회 후 자동 종료(분). 0 = 끔
    },
}

SECRET_PATHS = (("llm", "openai_api_key"), ("llm", "anthropic_api_key"))
ROLE_DECISION_PROVIDERS = {"none", "jev", "julia"}
JULIA_LIMITS = {
    "julia_threads": (1, 2),
    "julia_max_tokens": (512, 8192),
    "julia_load_timeout_seconds": (10, 180),
    "julia_request_timeout_seconds": (1, 60),
}
NEMOTRON_LIMITS = {"nemotron_cpu_cores": (1, 64), "nemotron_load_timeout_seconds": (1, 600),
                   "nemotron_request_timeout_seconds": (1, 600)}


def _review_choice(value: dict[str, Any], *, fallback: str = "none") -> dict[str, Any]:
    """저장된 구형 use_jev만 이행한다. 명시한 새 선택을 구형 값으로 덮지 않는다."""
    out = dict(value)
    provider = out.get("role_decision_provider")
    if not isinstance(provider, str) or provider not in ROLE_DECISION_PROVIDERS:
        provider = "jev" if out.get("use_jev") is True else fallback
    out["role_decision_provider"] = provider
    out["use_jev"] = provider == "jev"
    return out


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else v
        return out
    return copy.deepcopy(over) if over is not None else copy.deepcopy(base)


def get(repo: Repo, key: str, *, allow_unreadable_secrets: bool = False) -> Any:
    base = copy.deepcopy(DEFAULTS.get(key))
    if key in {"transcript_review", "audio_review"} and isinstance(base, dict):
        name = "GUKGAM_TRANSCRIPT_REVIEW" if key == "transcript_review" else "GUKGAM_AUDIO_REVIEW"
        base["enabled"] = os.environ.get(name, "").lower() in {"1", "true", "on"}
    saved = repo.get_setting(key)
    if key == "transcript_review" and isinstance(saved, dict):
        saved = _review_choice(saved)
    merged = _merge(base, saved)
    if key == "llm" and isinstance(merged, dict):
        from .llm.credential_store import CredentialError, unprotect_secret
        for _, field in SECRET_PATHS:
            try:
                merged[field] = unprotect_secret(merged.get(field) or "")
            except CredentialError:
                if not allow_unreadable_secrets:
                    raise
    return merged


def get_all(repo: Repo) -> dict[str, Any]:
    return {k: get(repo, k, allow_unreadable_secrets=True) for k in DEFAULTS}


def update(repo: Repo, key: str, value: Any) -> Any:
    if key not in DEFAULTS:
        raise KeyError(key)
    if key == "stt" and isinstance(value, dict):
        if "nemotron_cpu_auto" in value and type(value["nemotron_cpu_auto"]) is not bool:
            raise ValueError("CPU 자동 사용 설정은 참·거짓 값이어야 합니다")
        if "diarize_recordings" in value and type(value["diarize_recordings"]) is not bool:
            raise ValueError("녹음 화자 분리 설정은 참·거짓 값이어야 합니다")
        for field, (lo, hi) in NEMOTRON_LIMITS.items():
            if field in value and (type(value[field]) is not int or not lo <= value[field] <= hi):
                raise ValueError(f"{field}는 {lo}~{hi} 범위의 정수여야 합니다")
        # 이전 API처럼 코어 수만 명시하면 수동 제한을 고른 것으로 해석한다.
        if "nemotron_cpu_cores" in value and "nemotron_cpu_auto" not in value:
            value = {**value, "nemotron_cpu_auto": False}
    if key in {"transcript_review", "audio_review"}:
        if not isinstance(value, dict) or set(value) - set(DEFAULTS[key]):
            raise ValueError("화자·음성 검토 설정 필드를 확인하세요")
        for field in ("enabled", "auto_block", "use_jev", "verify_transcription"):
            if field in value and not isinstance(value[field], bool):
                raise ValueError("검토 설정의 허용 여부는 참·거짓 값이어야 합니다")
        if "role_decision_provider" in value and (not isinstance(value["role_decision_provider"], str)
                or value["role_decision_provider"] not in ROLE_DECISION_PROVIDERS):
            raise ValueError("사전 역할 판정은 추가 모델 없음·Jev·Julia-1 중에서 선택하세요")
        for field, (lo, hi) in JULIA_LIMITS.items():
            if field in value and (type(value[field]) is not int or not lo <= value[field] <= hi):
                raise ValueError(f"{field}는 {lo}~{hi} 범위의 정수여야 합니다")
        for field, lo, hi in (("buffer_seconds", 30, 600), ("max_clip_seconds", 1, 30), ("quota_mb", 1, 64)):
            if field in value and (type(value[field]) is not int or not lo <= value[field] <= hi):
                raise ValueError(f"{field}는 {lo}~{hi} 범위여야 합니다")
    current = get(repo, key, allow_unreadable_secrets=key == "llm")
    clear_secrets = set()
    supplied_openai_key = False
    if key == "llm" and isinstance(value, dict):
        from .llm.compat_config import PRESETS, connection_url, credential_for_endpoint
        value = dict(value)
        supplied_openai_key = isinstance(value.get("openai_api_key"), str) and bool(value["openai_api_key"]) and not value["openai_api_key"].startswith("••")
        value.pop("openai_key_base_url", None)  # 키 결합 주소는 화면 입력이 아닌 저장 로직만 바꾼다.
        for _, field in SECRET_PATHS:
            clear = value.pop("clear_" + field, False)
            if type(clear) is not bool:
                raise ValueError("저장된 키 삭제 여부를 확인하세요.")
            if clear:
                clear_secrets.add(field)
        if "openai_connection" in value:
            mode = value["openai_connection"]
            if not isinstance(mode, str) or mode not in PRESETS:
                raise ValueError("AI 연결 방식을 확인하세요.")
            if mode != current.get("openai_connection") and "openai_base_url" not in value:
                value["openai_base_url"] = PRESETS[mode]
    if key == "transcript_review":
        # 구형 화면이 use_jev만 보냈을 때에도 명시적 선택 변경으로 처리한다.
        if "role_decision_provider" not in value and "use_jev" in value:
            value = {**value, "role_decision_provider": "jev" if value["use_jev"] else "none"}
        value = _review_choice(value, fallback=current["role_decision_provider"])
    if isinstance(current, dict) and isinstance(value, dict):
        # 비밀값은 빈 문자열/마스킹 값이 오면 기존 값을 유지한다
        for sec_key, field in SECRET_PATHS:
            if sec_key == key and field in value and field not in clear_secrets and (not value[field] or str(value[field]).startswith("••")):
                value = {**value, field: current.get(field, "")}
        merged = _merge(current, value)
    else:
        merged = value
    stored = copy.deepcopy(merged)
    if key == "llm" and isinstance(merged, dict):
        from .llm.credential_store import protect_secret
        for field in clear_secrets:
            merged[field] = ""
        if "openai_api_key" in clear_secrets:
            merged["openai_key_base_url"] = ""
        elif supplied_openai_key:
            endpoint = connection_url(merged)
            merged["openai_key_base_url"] = endpoint or "unbound"
            if endpoint:
                credential_for_endpoint(merged, endpoint=endpoint)
        elif current.get("openai_api_key") and not current.get("openai_key_base_url"):
            merged["openai_key_base_url"] = connection_url(current) or "unbound"
        if "openai_base_url" in merged:
            merged["openai_base_url"] = connection_url(merged)
        stored = copy.deepcopy(merged)
        for _, field in SECRET_PATHS:
            stored[field] = protect_secret(merged.get(field) or "")
    repo.set_setting(key, stored)
    return merged


def public_view(all_settings: dict[str, Any]) -> dict[str, Any]:
    """화면 표시용: 비밀값을 마스킹한다."""
    out = copy.deepcopy(all_settings)
    if isinstance(out.get("llm"), dict):
        from .llm.compat_config import normalize_base_url
        from urllib.parse import unquote
        llm = out["llm"]
        llm.pop("openai_key_base_url", None)
        configured_keys = [llm.get(field) for _, field in SECRET_PATHS if llm.get(field)]
        for field, value in list(llm.items()):
            if field not in {name for _, name in SECRET_PATHS} and isinstance(value, str) and any(str(secret) in value for secret in configured_keys):
                llm[field] = ""
        try:
            endpoint = normalize_base_url(llm.get("openai_base_url") or "")
            if any(llm.get(field) and str(llm[field]) in unquote(endpoint) for _, field in SECRET_PATHS):
                endpoint = ""
            llm["openai_base_url"] = endpoint
        except ValueError:
            llm["openai_base_url"] = ""
    for sec_key, field in SECRET_PATHS:
        v = out.get(sec_key, {}).get(field)
        if v:
            from .llm.credential_store import CredentialError, is_protected, unprotect_secret
            if is_protected(v) or str(v).startswith("dpapi:"):
                try:
                    v = unprotect_secret(v)
                except CredentialError:
                    out[sec_key][field] = "••••"
                    continue
            out[sec_key][field] = "••••" + (str(v)[-4:] if len(str(v)) > 4 else "")
    return out


def watch_terms(repo: Repo) -> list[tuple[str, str]]:
    """(키워드, 범주) 목록 — 기관명·별칭·기관장 + 사용자 감시 키워드. 긴 것 우선."""
    org = get(repo, "org")
    terms: list[tuple[str, str]] = []
    if org.get("name"):
        terms.append((org["name"].strip(), "agency"))
    for a in org.get("aliases") or []:
        if a and a.strip():
            terms.append((a.strip(), "agency"))
    if org.get("head_name"):
        terms.append((org["head_name"].strip(), "person"))
    for w in get(repo, "watch_keywords") or []:
        kw = (w.get("keyword") if isinstance(w, dict) else str(w)).strip()
        if kw:
            terms.append((kw, (w.get("category") if isinstance(w, dict) else None) or "custom"))
    seen: set[str] = set()
    uniq = []
    for t, c in sorted(terms, key=lambda x: -len(x[0])):
        if t not in seen and len(t) >= 2:
            seen.add(t)
            uniq.append((t, c))
    return uniq
