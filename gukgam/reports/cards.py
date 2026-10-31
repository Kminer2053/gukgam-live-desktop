"""질의·답변 요지 카드(메신저 전파용, 계약서 5장 5번).

    [국감 상황 #07] 국토위 / 한국도로공사 / 10:42~10:49 (7분)
    ■ 질의: 김남근 위원(더불어민주당)
    ■ 질의요지: …
    ■ 답변: …
    ■ 요구자료/약속: ① … (기한: 종합감사 전) ② …
    ■ 담당(추천): 계약처   ■ 쟁점: #하도급 #공공발주
    ■ 원문: http://<host>/s/<sid>#u-<id>   ※AI 초안 / 검수: 미검수
"""
from __future__ import annotations

import re
from typing import Any

from .. import settings
from ..llm.providers import block_ref_utterance
from ..nlp import rules
from ..repo import Repo
from .display import report_display, source_utterance_id, stored_summaries
from .situation import (block_review_label, block_summary, clean_item_text, committee_short, departments_of, hm,
                        match_item, member_label, utterance_link)

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
_GENERAL_AUDIT = re.compile(r"종합\s*(?:감사|국감)|종감")
MAX_ACTIONS = 4
MAX_POINT_LEN = 70     # 메신저 카드는 짧게(규칙 요약은 원문 발췌라 길다)


def _short(text: str, n: int = MAX_POINT_LEN) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _review(block: dict[str, Any]) -> str:
    return block_review_label(block)


def _actions(summary: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    """요구자료·약속(의례 제외). 요약 문구를 쓰고 기한은 그 문구에 맞는 추적 항목에서 가져온다.

    블록 전체의 긴급도(urgency)로 기한을 채우지 않는다 — 다른 문장(증인 요구 등)의 '종합감사'가 모든 요구에
    번지기 때문. 항목에 기한이 없으면 그 요구 문구 자체에 종합감사가 나올 때만 '종합감사 전'으로 적는다.
    """
    out: list[str] = []
    for key, kind in (("data_requests", "data_request"), ("commitments", "commitment")):
        for r in summary.get(key) or []:
            if r.get("strength") == "의례":
                continue
            text = clean_item_text(r.get("text") or "")
            due = match_item(text, r.get("utterance_id"), kind, items).get("due_date") or (
                "종합감사 전" if _GENERAL_AUDIT.search(text) else "")
            out.append(_short(text) + (f" (기한: {due})" if due else ""))
    if not out:   # 요약에 없으면 추적 항목으로
        for i in items:
            if i["kind"] in ("data_request", "commitment") and i.get("strength") != "의례":
                out.append(_short(clean_item_text(i["text"])) + (f" (기한: {i['due_date']})" if i.get("due_date") else ""))
    seen: list[str] = []
    for a in out:
        if a not in seen:
            seen.append(a)
    return seen[:MAX_ACTIONS]


def build_block_card(repo: Repo, block_id: int, *, base_url: str = "") -> dict[str, str]:
    """질의 블록 → {"text","markdown"}. base_url 은 원문 링크 앞부분(예: http://10.0.0.5:8765)."""
    lookup = getattr(repo, "stage_block_session", None)
    sid = lookup(block_id) if callable(lookup) else None
    # 단건 소속 조회는 요약의 현재성 판정을 실행하지 않는다.
    display = report_display(repo, sid) if sid else None
    block = next((row for row in display["blocks"] if row["id"] == block_id), None) if display else repo.get_block(block_id)
    if block is None:
        raise KeyError(block_id)
    sid = block["session_id"]
    session = repo.get_session(sid) or {}
    org = settings.get(repo, "org") or {}
    display = display or report_display(repo, sid)
    if display["snapshot"] is not None:
        block = next((row for row in display["blocks"] if row["id"] == block_id), None)
        if block is None:
            raise KeyError(block_id)
        sm = stored_summaries(display, repo, sid, None, block_summary).get(block_id) or {}
        qblocks = [row for row in display["blocks"] if row.get("kind") == "question"]
    else:
        sm = block_summary(repo, block)
        qblocks = [row for row in display["blocks"] if row.get("kind") == "question"]
    no = next((i + 1 for i, b in enumerate(qblocks) if b["id"] == block_id), 0)
    items = repo.list_items(sid, block_id=block_id)

    minutes = max(1, round((block["ts_end"] - block["ts_start"]) / 60000))
    agency = (org.get("name") or "").strip() or (session.get("agencies") or "").strip()
    head_parts = [p for p in (committee_short(session.get("committee") or ""), agency,
                              f"{hm(block['ts_start'])}~{hm(block['ts_end'])} ({minutes}분)") if p]
    head = f"[국감 상황 #{no:02d}] " + " / ".join(head_parts)
    if block["kind"] == "question":
        who = member_label(block.get("questioner") or "", block.get("questioner_party") or "")
        if block.get("status") == "needs_name":
            who += " [성명 확인 필요]"
    else:
        who = "위원장 진행" if block["kind"] == "chair" else "기타 발언"
    rules_based = str(sm.get("provider") or "").startswith("rules")
    points = " / ".join(_short(p) for p in (sm.get("question_points") or [])[:2 if rules_based else 3]) \
        or sm.get("one_line") or "-"
    answer = " / ".join(_short(p) for p in (sm.get("answer_points") or [])[:2]) or "(답변 요지 미추출)"
    acts = _actions(sm, items)
    acts_txt = " ".join(f"{_CIRCLED[i]} {a}" for i, a in enumerate(acts)) if acts else "없음"
    depts = departments_of(items)
    if not depts:
        dept = rules.assign_department(" ".join([sm.get("one_line") or "", *(sm.get("question_points") or [])]),
                                       settings.get(repo, "departments") or [])
        depts = [dept] if dept else []
    dept_txt = ", ".join(depts[:2]) if depts else "미지정"
    tags = " ".join("#" + k.lstrip("#").replace(" ", "") for k in (sm.get("keywords") or [])[:4]) or "-"
    uid = block_ref_utterance(sm)
    if uid is None:
        if display["snapshot"] is not None:
            uid = next((source_utterance_id(row) for row in display["utts"]
                        if row.get("block_id") == block_id and row.get("speaker_role") != "system"), None)
        else:
            row = repo.db.query_one("SELECT id FROM utterances WHERE block_id=? AND speaker_role<>'system' "
                                    "ORDER BY ts_start, seq LIMIT 1", (block_id,))
            uid = row["id"] if row else None
    url = utterance_link(sid, uid, base_url) or "-"
    review = _review(block)

    text = "\n".join([
        head,
        f"■ 질의: {who}",
        f"■ 질의요지: {points}",
        f"■ 답변: {answer}",
        f"■ 요구자료/약속: {acts_txt}",
        f"■ 담당(추천): {dept_txt}   ■ 쟁점: {tags}",
        f"■ 원문: {url}   {review}",
    ])
    link = f"[원문 보기]({url})" if url != "-" else "-"
    markdown = "\n".join([
        f"**{head}**",
        f"- **질의**: {who}",
        f"- **질의요지**: {points}",
        f"- **답변**: {answer}",
        f"- **요구자료/약속**: {acts_txt}",
        f"- **담당(추천)**: {dept_txt} · **쟁점**: {tags}",
        f"- **원문**: {link}",
        "",
        f"_{review}_",
    ])
    return {"text": text, "markdown": markdown}
