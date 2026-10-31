"""Julia 원후보를 보존하며 직접 발언 근거와 충돌하는 역할의 확정만 보류한다."""
from __future__ import annotations

import re

from .nlp.floor import scan_anchors
from .nlp.role_evidence import role_evidence

ROLE_ADOPTION_VERSION = 1
_PROCEDURAL_REMAINDER = re.compile(
    r"(?:(?:예|네|그러면|그럼|다음은|다음으로|이어서|계속해서|존경하는|이제|지금부터)\s*[,，.!?。]*)*\Z"
)


def rule_role_conflict(text: str, role: str, context: dict | None = None) -> dict:
    """혼합·인용·단독 맞장구는 강한 근거로 승격하지 않는다. 실명은 추정하지 않는다."""
    context = context or {}
    evidence = role_evidence(text)
    expected, basis = None, None
    if not evidence["mixed"]:
        # 기존 호명 스캐너의 직접 앞말에는 의례적인 '존경하는'이 없다. 확정된
        # 순서 전환 앞말 바로 뒤의 이 수식만 제거해 검사하며 원문은 수정하지 않는다.
        floor_text = re.sub(r"^((?:(?:예|네|그러면|그럼|다음은|다음으로|이어서)\s*[,，.!?。]*)+)존경하는\s+",
                            lambda match: match[1], text)
        # 명단과 직접 일치하는 순수 호명/종료/진행만 허용한다. 다른 본문을 같이 품은
        # 문장을 진행자 한 명의 말로 단정하지 않도록 나머지 문자열도 검사한다.
        anchors = [anchor for anchor in scan_anchors(floor_text, context.get("roster") or [])
                   if not anchor.get("requires_context") and
                   (anchor.get("caller_role") == "chair" or anchor.get("role") == "chair")]
        if anchors:
            remainder, end = [], 0
            for anchor in anchors:
                if anchor["start"] >= end:
                    remainder.append(floor_text[end:anchor["start"]])
                end = max(end, anchor["end"])
            remainder.append(floor_text[end:])
            if _PROCEDURAL_REMAINDER.fullmatch(" ".join(remainder).strip(" \t\n,.!?。")):
                expected, basis = "chair", "direct_unquoted_floor_anchor"
        if expected is None and evidence["role"] in {"member", "answer"}:
            expected, basis = evidence["role"], "unmixed_direct_role_evidence"
    return {"version": ROLE_ADOPTION_VERSION, "conflict": expected is not None and expected != role,
            "expected_role": expected, "basis": basis, "mixed": bool(evidence["mixed"])}


def apply_julia_role_guard(row: dict, context: dict | None = None, *, provider: str | None = None,
                          stage2: bool = False) -> bool:
    """후보/본문을 바꾸지 않는다. LLM 문맥 보완 뒤에도 남은 충돌은 계속 보류한다."""
    if (provider or row.get("role_candidate_provider")) != "julia":
        return False
    result = rule_role_conflict(row.get("original_text", row.get("text", "")), row["role"], context)
    row["role_rule_guard"] = result
    flags = [flag for flag in row.get("uncertainties", []) if flag != "role_rule_conflict"]
    if result["conflict"]:
        row["role_uncertain"] = True
        row["role_status"] = "held" if stage2 else "uncertain"
        flags.append("role_rule_conflict")
    row["uncertainties"] = flags
    return result["conflict"]
