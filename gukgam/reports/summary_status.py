"""저장된 요약의 생성 출처·입력 범위를 고정 문구로 표시한다."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..config import KST


def correction_document_metadata(value: Any) -> dict[str, Any] | None:
    """같은 표시본에서 연결한 보정 문서의 공개 상태·수치만 검증한다."""
    if not isinstance(value, dict):
        return None
    safe_integer = 9_007_199_254_740_991
    document_id = value.get("document_id")
    if type(document_id) is not int or not 0 < document_id <= safe_integer:
        return None
    if value.get("status") not in ("draft", "partial", "ready"):
        return None
    counts = ("paragraph_count", "held_paragraph_count", "text_uncertain_paragraph_count",
              "role_uncertain_paragraph_count")
    if any(type(value.get(key)) is not int or not 0 <= value[key] <= safe_integer for key in counts):
        return None
    if any(value[key] > value["paragraph_count"] for key in counts[1:]):
        return None
    return {"document_id": document_id, "status": value["status"], **{key: value[key] for key in counts}}


def correction_document_lines(value: Any) -> list[str]:
    """문서 보정 상태와 요약 항목의 부분 여부를 구분하는 고정 안내다."""
    metadata = correction_document_metadata(value)
    if metadata is None:
        return []
    label = {"draft": "검수 전 초안", "partial": "일부 보정 확인 필요",
             "ready": "저장된 보정본 · 검수 여부 별도 확인"}[metadata["status"]]
    lines = [f"보정 문서 상태: {label}"]
    for key, note in (("held_paragraph_count", "본문 보정 보류 · 원문 유지"),
                      ("text_uncertain_paragraph_count", "본문 보정 확인 필요"),
                      ("role_uncertain_paragraph_count", "화자 확인 필요")):
        if metadata[key]:
            lines.append(f"{note}: {metadata[key]}문단")
    lines.append("보정 문서의 상태입니다. 요약 항목의 부분 여부와 별도로 확인하세요.")
    return lines


def summary_status(value: Any) -> dict[str, str]:
    """실패 사유·제공자 임의 문자열을 사용자 안내로 복사하지 않는다."""
    row = value if isinstance(value, dict) else {}
    content = row.get("content", row)
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            content, row = {}, {}
    content = content if isinstance(content, dict) else {}
    provider = content.get("provider") or content.get("_provider") or row.get("provider") or ""
    provider = provider.strip() if isinstance(provider, str) else ""
    kind, label, note = "unknown", "생성 출처 미확인", "저장된 요약의 생성 출처를 확인할 수 없습니다. 원문과 대조해 확인하세요."
    if provider == "rules":
        kind, label, note = "rules", "규칙 정리 초안", "저장된 발언을 규칙으로 발췌·정리했습니다. 원문과 대조해 확인하세요."
    elif provider == "rules(대체)":
        kind, label, note = "fallback", "규칙 대체 초안", "AI 요약 결과를 사용하지 못해 규칙 정리로 대체했습니다. 원문과 대조하고 필요하면 다시 요약하세요."
    elif provider in ("desktop_chatgpt", "local_mlx", "openai_compat", "anthropic"):
        kind, label, note = "ai", "ChatGPT 요약 초안" if provider == "desktop_chatgpt" else "AI 요약 초안", "AI가 생성한 요약 초안입니다. 검수 전에는 원문과 대조해 확인하세요."
    elif (provider == "closed_block" and type(content.get("source_document_id")) is int
            and 0 < content["source_document_id"] <= 9_007_199_254_740_991):
        kind, label, note = "reference", "보정 문서 참조 요약", "저장된 보정 문서의 요약을 참조합니다. 이 표시만으로 생성 모델이나 검수 완료를 확인할 수 없습니다. 원문과 보정 문서를 대조해 확인하세요."
    if isinstance(content.get("edited_by"), str) and content["edited_by"].strip():
        kind, label, note = "human", "사람 수정 요약", f"사람이 수정한 요약입니다. 검수 여부는 별도 표시를 확인하세요. 수정 전 출처: {label}."
    coverage = content.get("coverage")
    coverage_label, notes = "", []
    if isinstance(coverage, dict):
        if coverage.get("question_boundary_status") == "unclassified" or coverage.get("question_presence_unverified") is True:
            coverage_label = "질의 경계 미확정 · 발언 기반"
            notes.append("실제 질의 유무와 화자별 질의·답변 경계를 확인하지 않은 발언 기반 요약입니다.")
        n, k = coverage.get("eligible_utterance_count"), coverage.get("selected_utterance_count")
        if (coverage.get("source") == "final_utterances" and coverage.get("selection") == "recent_bounded"
                and type(n) is int and type(k) is int and 0 <= k <= n):
            notes.append(f"확정 발언 {n}건 중 {k}건을 입력했습니다.")
            added = coverage.get("appended_after_selection_count")
            if type(added) is int and 0 < added <= n - k:
                notes.append(f"입력 선택 후 추가 저장된 {added}건은 이 요약에 포함되지 않았습니다.")
            start, end = coverage.get("selected_start_ms"), coverage.get("selected_end_ms")
            if type(start) is int and type(end) is int and 0 < start <= end:
                try:
                    times = [datetime.fromtimestamp(t / 1000, KST).strftime("%H:%M:%S") for t in (start, end)]
                    notes.append(f"입력 시각 범위: {'–'.join(times)}(한국 시각).")
                except (ValueError, OverflowError, OSError):
                    pass
        if coverage.get("partial") is True or coverage.get("text_truncated") is True:
            coverage_label = f"일부 발언 · {coverage_label}" if coverage_label else "일부 발언 요약"
            notes.append("입력에 포함되지 않은 발언이나 축약된 본문이 있습니다. 회의 전체를 빠짐없이 다뤘다고 볼 수 없습니다.")
        elif coverage.get("meeting_coverage_verified") is False:
            notes.append("회의 전체 내용의 반영 여부는 검증하지 않았습니다.")
    return {"kind": kind, "label": label, "note": note, "coverageLabel": coverage_label, "coverageNote": " ".join(notes)}


def audio_transcript_notice(source_type: str) -> str:
    return ("음성 전사 초안입니다. 화자·질의 경계와 용어·숫자는 원음을 대조해 확인하세요."
            if source_type in ("assembly_hls", "browser", "file") else "")
