"""상황보고(저장 요약의 전체 개조식 평문 + 마크다운). 요약 생성 출처·검수 표시.

AI 요약이 없는 블록은 규칙 기반 요약으로 채워 보고서가 항상 만들어지게 한다.
"""
from __future__ import annotations

import re
from copy import deepcopy
from collections import Counter
from datetime import datetime
from typing import Any

from .. import settings
from ..config import KST
from ..db import now_ms
from ..llm.providers import SIM_ANY_UTTERANCE, rules_block_summary, text_overlap
from .summary_status import audio_transcript_notice, summary_status, correction_document_metadata, correction_document_lines
from ..models import ITEM_KIND_LABELS, STATUS_LABELS
from ..nlp import rules
from ..repo import Repo
from ..serialize import hms, ymd_hm
from .display import report_display, source_utterance_id, stored_summaries, utterance_blocks, summary_reference_fields

NOTICE = "AI 음성인식·요약 결과로 오류가 있을 수 있습니다 · 내부 업무용(국회법 제149조② 상업적 이용 금지)"
SOURCE_SHORT = {
    "assembly_caption": "국회 AI자막", "assembly_hls": "국회 중계 음성인식", "browser": "브라우저 음성인식",
    "file": "녹음 파일 음성인식", "replay": "리플레이", "manual": "수기 입력",
}
_LATEST_SESSION_SUMMARY = object()
SESSION_SUMMARY_FILTER_NOTICE = "회의 요약은 필터가 없는 전체 보기에서 확인하세요. 현재 보고 범위에는 전체 요약을 포함하지 않았습니다."


def stored_session_summary(row: Any) -> dict[str, Any] | None:
    """저장 행을 읽기 전용으로 투영한다. 오류 원문·미사용 메타는 보고서에 옮기지 않는다."""
    if not isinstance(row, dict) or not isinstance(row.get("content"), dict):
        return None
    source = row["content"]
    raw_provider = source.get("provider") or source.get("_provider") or row.get("provider") or ""
    provider = raw_provider.strip() if isinstance(raw_provider, str) else ""
    content: dict[str, Any] = {}
    for key in ("overview", "model", "edited_by"):
        value = source.get(key)
        if isinstance(value, str):
            content[key] = value
    content["provider"] = provider
    for key in ("org_points", "next_actions", "hashtags"):
        content[key] = [value for value in source.get(key, []) if isinstance(value, str) and value.strip()] \
            if isinstance(source.get(key), list) else []
    content["issues"] = [{"title": value["title"]} for value in source.get("issues", [])
                         if isinstance(value, dict) and isinstance(value.get("title"), str) and value["title"].strip()] \
        if isinstance(source.get("issues"), list) else []
    if not (content.get("overview", "").strip() or any(content[key] for key in ("issues", "org_points", "next_actions"))):
        return None
    if isinstance(source.get("coverage"), dict):
        keys = ("source", "selection", "question_boundary_status", "question_presence_unverified",
                "meeting_coverage_verified", "eligible_utterance_count", "selected_utterance_count",
                "selected_start_ms", "selected_end_ms", "partial", "text_truncated",
                "stored_utterance_coverage_complete", "appended_after_selection_count")
        content["coverage"] = deepcopy({key: source["coverage"][key] for key in keys if key in source["coverage"]})
    return {"id": row.get("id"), "created_at": row.get("created_at"), "time": ymd_hm(row.get("created_at")),
            "provider": provider, "content": content}


def session_summary_lines(sections: dict[str, Any], *, markdown: bool = False) -> list[str]:
    """화면과 같은 저장 행으로 평문·마크다운의 별도 회의 요약 절을 만든다."""
    row = sections.get("session_summary")
    notice = sections.get("session_summary_notice")
    if not row:
        return [f"> {notice}" if markdown else notice] if notice else []
    content = row["content"]
    status = summary_status(row)
    lines = ["### 저장된 회의 요약" if markdown else "□ 저장된 회의 요약"]
    lines.append(f"{status['label']}" + (f" · {row['time']} 생성" if row.get("time") else ""))
    lines += [value for value in (status["note"], status["coverageLabel"], status["coverageNote"]) if value]
    if content.get("overview"):
        lines.append(content["overview"])
    for key, label in (("issues", "주요 이슈"), ("org_points", "우리 기관 관련 포인트"), ("next_actions", "후속조치 제안")):
        values = [value["title"] for value in content[key]] if key == "issues" else content[key]
        if values:
            lines.append(f"{label}:")
            lines.extend(f"- {value}" for value in values)
    if content.get("hashtags"):
        lines.append(" ".join(content["hashtags"]))
    return lines


def block_summary_detail_lines(summary: dict, correction: dict | None) -> list[str]:
    """본문 보정 상태와 요약 항목 제외 상태를 별도 줄로 표시한다."""
    metadata = correction_document_metadata(correction)
    if metadata is None or summary_reference_fields(summary).get("source_document_id") != metadata["document_id"]:
        return []
    lines = correction_document_lines(metadata)
    fields = summary_reference_fields(summary)
    partial, held = fields.get("partial"), fields.get("held_bullet_count")
    if partial is False and held == 0:
        lines.append("요약 항목: 별도 제외 없음")
    elif partial is True and type(held) is int:
        lines.append(f"요약 항목: {held}개 제외" if held else "요약 항목: 일부 확인 필요")
    return lines


COMMITTEE_SHORT = {
    "국회운영위원회": "운영위", "법제사법위원회": "법사위", "정무위원회": "정무위", "기획재정위원회": "기재위",
    "재정경제기획위원회": "재경위", "교육위원회": "교육위", "과학기술정보방송통신위원회": "과방위",
    "외교통일위원회": "외통위", "국방위원회": "국방위", "행정안전위원회": "행안위", "문화체육관광위원회": "문체위",
    "농림축산식품해양수산위원회": "농해수위", "산업통상자원중소벤처기업위원회": "산자중기위",
    "산업통상벤처위원회": "산벤위", "보건복지위원회": "복지위", "환경노동위원회": "환노위",
    "기후에너지환경노동위원회": "기후노동위", "국토교통위원회": "국토위", "정보위원회": "정보위",
    "여성가족위원회": "여가위", "성평등가족위원회": "성평등가족위",
}
_WEEKDAY = "월화수목금토일"
_LABEL_PREFIX = re.compile(r"^\[[^\]]{1,20}\]\s*")
_OPEN_RE = re.compile(r"개의|개회|감사를\s*(?:시작|개시|실시)|국정감사를\s*시작")
_CLOSE_RE = re.compile(r"감사\s*종료|감사종료|산회|국정감사를\s*(?:모두\s*)?마치겠")
_RECESS_RE = re.compile(r"정회|감사\s*중지|감사중지|수신\s*없음")
MAX_LIST = 20


# ── 공통 도우미(카드·내보내기에서도 씀) ─────────────────────────────

def hm(ms: int | None) -> str:
    return hms(ms)[:5] if ms else ""


def kst_stamp(ms: int | None = None) -> str:
    """'2026.10.08.(목) 14:30'"""
    d = datetime.fromtimestamp((ms or now_ms()) / 1000, KST)
    return f"{d:%Y.%m.%d}.({_WEEKDAY[d.weekday()]}) {d:%H:%M}"


def committee_short(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if name in COMMITTEE_SHORT:
        return COMMITTEE_SHORT[name]
    return name[:-3] + "위" if name.endswith("위원회") else name


def meeting_head(session: dict[str, Any]) -> str:
    """'국토교통위원회 국정감사(국토교통부 등)'"""
    committee = (session.get("committee") or "").strip()
    agencies = (session.get("agencies") or "").strip()
    if committee:
        return f"{committee} 국정감사" + (f"({agencies})" if agencies else "")
    return session.get("title") or "국정감사"


def clean_item_text(text: str) -> str:
    return _LABEL_PREFIX.sub("", text or "").strip()


def member_label(name: str, party: str = "") -> str:
    if not name:
        return "성명 미확인 위원"
    return f"{name} 위원" + (f"({party})" if party else "")


def block_summary(repo: Repo, block: dict[str, Any], terms: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """저장된 블록 요약, 없으면 규칙 기반 요약(저장하지 않음)."""
    sm = block.get("summary")
    if isinstance(sm, dict) and sm.get("one_line"):
        return sm
    utts = repo.list_utterances(block["session_id"], block_id=block["id"], final_only=True)
    return rules_block_summary(block, utts, terms if terms is not None else settings.watch_terms(repo))


def edited_by(block: dict[str, Any]) -> str:
    """사람이 요약을 고쳤으면 고친 사람(blocks.summary.edited_by), 아니면 ''."""
    sm = block.get("summary")
    return (sm.get("edited_by") or "") if isinstance(sm, dict) else ""


def block_review_label(block: dict[str, Any]) -> str:
    """블록 하나의 검수 표기. 사람이 요약을 고쳤으면 '검수(수정)'·'수정: 이름'으로 구분한다."""
    editor = edited_by(block)
    label = summary_status(block.get("summary"))["label"]
    if block.get("review_status") == "reviewed":
        tag = "검수(수정)" if editor else "검수"
        return f"※{label} / {tag}: {block.get('reviewed_by') or '검수자'}({hm(block.get('reviewed_at'))})"
    if editor:
        when = hm((block.get("summary") or {}).get("edited_at"))
        return f"※{label}(수정: {editor}{' ' + when if when else ''}) / 검수: 미검수"
    return f"※{label} / 검수: 미검수"


def review_label(blocks: list[dict[str, Any]]) -> str:
    """전체 요약의 검수 현황. 생성 출처는 각 요약에서 별도로 표시한다."""
    q = [b for b in blocks if b.get("kind") == "question"]
    done = [b for b in q if b.get("review_status") == "reviewed"]
    n_edit = sum(1 for b in q if edited_by(b))
    tail = f" · 요약 수정 {n_edit}건" if n_edit else ""
    if not done:
        return "※요약 초안 / 검수: 미검수" + tail
    last = max(done, key=lambda b: b.get("reviewed_at") or 0)
    who = f"{last.get('reviewed_by') or '검수자'}({hm(last.get('reviewed_at'))})"
    if len(done) < len(q):
        return f"※요약 초안 / 검수: {who} — {len(done)}/{len(q)}개 질의 검수" + tail
    return f"※요약 초안 / 검수: {who}" + tail


def match_item(text: str, uid: int | None, kind: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """요약의 요구·약속 문구에 해당하는 추적 항목(담당·기한을 가져올 곳). 같은 발언에 여러 건이면 문구가 가장
    많이 겹치는 것, 근거 발언이 없으면 문구가 크게 겹치는 같은 종류 항목. 없으면 {}."""
    same = [i for i in items if i["kind"] == kind and uid is not None and i.get("utterance_id") == uid]
    if len(same) == 1:
        return same[0]
    pool = same or [i for i in items if i["kind"] == kind]
    best, score = None, 0.0
    for i in pool:
        sc = text_overlap(text, f"{i.get('text') or ''} {i.get('quote') or ''}")
        if sc > score:
            best, score = i, sc
    if best is not None and (same or score >= SIM_ANY_UTTERANCE):
        return best
    return same[0] if same else {}


def org_block_ids(repo: Repo, sid: str, qblocks: list[dict[str, Any]], sums: dict[int, dict[str, Any]],
                  utt_block: dict[int, int | None] | None = None) -> set[int]:
    """우리 기관 관련 질의 블록: 요약 관련도(high·medium) + 기관명·기관장 언급 알림이 있는 블록."""
    if utt_block is None:
        utt_block = {r["id"]: r["block_id"] for r in repo.db.query(
            "SELECT id, block_id FROM utterances WHERE session_id=?", (sid,))}
    alert_blocks = {utt_block.get(a["utterance_id"]) for a in repo.list_alerts(sid, limit=100000)
                    if a.get("category") in ("agency", "person")}
    return {b["id"] for b in qblocks
            if (sums.get(b["id"]) or {}).get("org_relevance") in ("high", "medium") or b["id"] in alert_blocks}


def item_is_org(it: dict[str, Any], org_ids: set[int], org_terms: list[tuple[str, str]]) -> bool:
    """우리 기관 관련 항목: 관련 블록의 항목이거나 요지·인용에 기관명·기관장이 나오는 항목."""
    if it.get("block_id") in org_ids:
        return True
    return bool(org_terms and rules.find_mentions(f"{it.get('text', '')} {it.get('quote', '')}", org_terms))


def utterance_link(sid: str, uid: int | None, base_url: str = "") -> str:
    if not uid:
        return ""
    return f"{base_url.rstrip('/')}/s/{sid}#u-{uid}"


def _org_terms(terms: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [t for t in terms if t[1] in ("agency", "person")]


# ── 보고서 ───────────────────────────────────────────────────────

def _progress(session: dict[str, Any], blocks: list[dict[str, Any]], stats: dict[str, Any],
              markers: list[dict[str, Any]], chair_utts: list[dict[str, Any]]) -> dict[str, Any]:
    qn = sum(1 for b in blocks if b["kind"] == "question" and not b.get("is_open"))
    opened = next((u for u in chair_utts + markers if _OPEN_RE.search(u["text"])), None)
    closed = next((u for u in reversed(chair_utts + markers) if _CLOSE_RE.search(u["text"])), None)
    t0 = opened["ts_start"] if opened else (stats.get("ts_first") or session.get("started_at"))
    start = f"{hm(t0)} {'개의' if opened else '수집 시작'}" if t0 else "수집 전"
    status = session["status"]
    ob = next((b for b in reversed(blocks) if b.get("is_open")), None)
    last_marker = markers[-1] if markers else None
    if status == "ended" or closed:
        if closed:
            now = f"{hm(closed['ts_start'])} 감사 종료"
        else:
            now = f"{hm(session.get('ended_at') or stats.get('ts_last'))} 수집 종료"
        count = f"질의 {qn}회"
    else:
        if last_marker and _RECESS_RE.search(last_marker["text"]) and (
                not stats.get("ts_last") or last_marker["ts_start"] >= stats["ts_last"] - 1000):
            now = f"현재 {last_marker['text'].strip('— ')}"
        elif ob is not None and ob["kind"] == "question":
            now = f"현재 {member_label(ob.get('questioner') or '')} 질의 중"
        elif ob is not None and ob["kind"] == "chair":
            now = "현재 위원장 진행 중"
        elif status == "paused":
            now = "수집 일시정지"
        else:
            now = "진행 중"
        count = f"질의 {qn}회 완료"
    line = f"{start} → {now} ({count})"
    return {"start_ms": t0, "status": status, "status_label": STATUS_LABELS.get(status, status), "current": now,
            "question_count": qn, "line": line}


def _block_actions(sm: dict[str, Any], items: list[dict[str, Any]], departments: list[dict],
                   exclude_ritual: bool, limit: int = 5) -> list[dict[str, Any]]:
    """블록의 조치(요구자료·약속). AI 요약(또는 사람이 고친 요약)이면 요약 문구를, 아니면 추적 항목을 쓴다.
    담당·기한은 문구가 맞는 추적 항목에서."""
    out: list[dict[str, Any]] = []
    if not str(sm.get("provider") or "rules").startswith("rules") or sm.get("edited_by"):
        for key, kind in (("data_requests", "data_request"), ("commitments", "commitment")):
            for r in sm.get(key) or []:
                if exclude_ritual and r.get("strength") == "의례":
                    continue
                text = clean_item_text(r.get("text") or "")
                it = match_item(text, r.get("utterance_id"), kind, items)
                out.append({"kind": kind, "text": text,
                            "dept": it.get("assignee_dept") or rules.assign_department(text, departments),
                            "due_date": it.get("due_date") or ""})
    if not out:
        for it in items:
            if it["kind"] in ("data_request", "commitment") and not (exclude_ritual and it.get("strength") == "의례"):
                out.append({"kind": it["kind"], "text": clean_item_text(it["text"]),
                            "dept": it.get("assignee_dept") or "", "due_date": it.get("due_date") or ""})
    return out[:limit]


def build_situation_report(repo: Repo, sid: str, *, org_only: bool = False, since_ms: int | None = None,
                           exclude_ritual: bool = True, base_url: str = "", _display: dict | None = None,
                           _session_summary: Any = _LATEST_SESSION_SUMMARY) -> dict[str, Any]:
    """상황보고. 반환: {"title","generated_at","text","markdown","sections"}.

    org_only: 우리 기관 관련 질의·항목만. since_ms: 이 시각 이후 변경분만(진행 현황은 전체).
    exclude_ritual: 의례적 약속('노력하겠습니다')을 목록에서 뺀다.
    """
    session = repo.get_session(sid)
    if session is None:
        raise KeyError(sid)
    terms = settings.watch_terms(repo)
    org_terms = _org_terms(terms)
    stats = repo.session_stats(sid)
    display = report_display(repo, sid) if _display is None else _display
    blocks, utts = display["blocks"], display["utts"]
    markers = [u for u in utts if u.get("speaker_role") == "system"]
    chair_utts = [u for u in utts if u.get("speaker_role") == "chair"]
    utt_block = utterance_blocks(utts)
    progress = _progress(session, blocks, stats, markers, chair_utts)

    # 우리 기관 관련 블록: 요약 관련도(high·medium) + 기관명·기관장 언급 알림
    qblocks = [b for b in blocks if b["kind"] == "question"]
    sums = stored_summaries(display, repo, sid, terms, block_summary)
    org_ids = org_block_ids(repo, sid, qblocks, sums, utt_block)

    def in_range(b: dict) -> bool:
        return since_ms is None or b["ts_end"] >= since_ms

    items = repo.list_items(sid, limit=-1)
    departments = settings.get(repo, "departments") or []
    by_block_items: dict[int, list[dict]] = {}
    for it in items:
        by_block_items.setdefault(it.get("block_id") or 0, []).append(it)

    def item_org(it: dict) -> bool:
        return item_is_org(it, org_ids, org_terms)

    def item_in_range(it: dict) -> bool:
        return since_ms is None or (it.get("utt_ts") or it.get("created_at") or 0) >= since_ms

    # 우리 기관 관련 질의
    org_questions = []
    for b in qblocks:
        if b["id"] not in org_ids or not in_range(b):
            continue
        sm = sums[b["id"]]
        org_questions.append({
            "block_id": b["id"], "time": hm(b["ts_start"]), "questioner": b.get("questioner") or "",
            "party": b.get("questioner_party") or "", "relevance": sm.get("org_relevance") or "",
            "reason": sm.get("org_reason") or "",
            "question": "; ".join(sm.get("question_points") or [sm.get("one_line") or ""]),
            "answer": "; ".join(sm.get("answer_points") or []),
            "actions": _block_actions(sm, by_block_items.get(b["id"], []), departments, exclude_ritual),
            "utterance_id": next((source_utterance_id(u) for u in utts if u.get("block_id") == b["id"]
                                  and u.get("speaker_role") == "member"), None),
        })

    # 위원별 질의·답변 요지
    members = []
    for b in qblocks:
        if not in_range(b) or (org_only and b["id"] not in org_ids):
            continue
        sm = sums[b["id"]]
        members.append({
            "block_id": b["id"], "time_range": f"{hm(b['ts_start'])}~{hm(b['ts_end'])}",
            "questioner": b.get("questioner") or "", "party": b.get("questioner_party") or "",
            "one_line": sm.get("one_line") or "", "question_points": sm.get("question_points") or [],
            "answer_points": sm.get("answer_points") or [], "provider": sm.get("provider") or "",
            "review_status": b.get("review_status") or "draft", "is_open": bool(b.get("is_open")),
            "reviewed_by": b.get("reviewed_by") or "", "reviewed_at": b.get("reviewed_at"),
            "edited_by": sm.get("edited_by") or "", "edited_at": sm.get("edited_at"),
            "org_related": b["id"] in org_ids,
            **summary_reference_fields(sm),
            **({"correction_document": deepcopy(display["correction_documents"][b["id"]])}
               if b["id"] in display.get("correction_documents", {}) else {}),
        })

    def item_row(it: dict) -> dict:
        return {"id": it["id"], "block_id": it.get("block_id"), "kind": it["kind"], "text": clean_item_text(it["text"]),
                "requester": it.get("requester") or "", "speaker": it.get("speaker") or "",
                "dept": it.get("assignee_dept") or "", "due_date": it.get("due_date") or "",
                "strength": it.get("strength") or "", "status": it.get("status") or "open",
                "time": hm(it.get("utt_ts")), "utterance_id": it.get("utterance_id"), "origin": it.get("origin")}

    def pick(kind: str) -> list[dict]:
        rows = [it for it in items if it["kind"] == kind and item_in_range(it)]
        if org_only:
            rows = [it for it in rows if item_org(it)]
        if kind == "commitment" and exclude_ritual:
            rows = [it for it in rows if it.get("strength") != "의례"]
        return [item_row(it) for it in rows]

    data_requests = pick("data_request")
    commitments = pick("commitment")
    special = [{"kind": r["kind"], "label": ITEM_KIND_LABELS.get(r["kind"], r["kind"]), **r}
               for r in pick("witness_request") + pick("written_question")]
    for m in markers:
        if since_ms is None or m["ts_start"] >= since_ms:
            special.append({"kind": "marker", "label": "기록", "text": m["text"], "time": hm(m["ts_start"]),
                            "utterance_id": source_utterance_id(m), "requester": "", "dept": ""})
    special.sort(key=lambda r: r.get("time") or "")

    review = review_label(blocks)
    generated = now_ms()
    title = f"[국정감사 상황보고] {meeting_head(session)}"
    sub = f"{kst_stamp(generated)} 기준 | 수집: {SOURCE_SHORT.get(session['source_type'], session['source_type'])}"
    if org_only:
        sub += " | 우리 기관 관련만"
    if since_ms:
        sub += f" | {hm(since_ms)} 이후 변경분"
    sections = {"progress": progress, "org_questions": org_questions, "members": members,
                "data_requests": data_requests, "commitments": commitments, "special": special,
                "review": review, "notice": NOTICE, "subtitle": sub,
                "transcription_notice": audio_transcript_notice(session["source_type"]),
                "org_name": (settings.get(repo, "org") or {}).get("name", "")}
    if org_only or since_ms is not None:
        sections["session_summary_notice"] = SESSION_SUMMARY_FILTER_NOTICE
    else:
        row = repo.latest_summary(sid, "session") if _session_summary is _LATEST_SESSION_SUMMARY else _session_summary
        summary = stored_session_summary(row)
        if summary:
            sections["session_summary"] = summary
    return {"title": title, "generated_at": generated, "text": _render_text(title, sections),
            "markdown": _render_md(title, sections, sid, base_url), "sections": sections}


# ── 렌더링 ───────────────────────────────────────────────────────

def _req_line(r: dict, n: int) -> str:
    who = f"{r['requester']} 위원" if r.get("requester") else ""
    tail = [x for x in (who, f"담당: {r['dept']}" if r.get("dept") else "담당: 미지정",
                        f"기한: {r['due_date']}" if r.get("due_date") else "") if x]
    st = f" [{r['strength']}]" if r.get("strength") else ""
    return f" {n}. {r['text']}{st} — " + " / ".join(tail)


def _commit_line(r: dict, n: int) -> str:
    tail = [x for x in (f"요구: {r['requester']} 위원" if r.get("requester") else "",
                        f"담당: {r['dept']}" if r.get("dept") else "",
                        f"기한: {r['due_date']}" if r.get("due_date") else "") if x]
    st = f" [{r['strength']}]" if r.get("strength") else ""
    return f" {n}. {r['text']}{st}" + (" — " + " / ".join(tail) if tail else "")


def _special_tail(r: dict) -> str:
    tail = [x for x in (f"{r['requester']} 위원" if r.get("requester") else "",
                        f"기한: {r['due_date']}" if r.get("due_date") else "") if x]
    return f" ({', '.join(tail)})" if tail else ""


def _capped(rows: list, render, limit: int = MAX_LIST) -> list[str]:
    out = [render(r, i + 1) for i, r in enumerate(rows[:limit])]
    if len(rows) > limit:
        out.append(f" … 외 {len(rows) - limit}건")
    return out


def _render_text(title: str, s: dict[str, Any]) -> str:
    L = [title, s["subtitle"], "□ 진행 현황", f" ○ {s['progress']['line']}"]
    if s.get("transcription_notice"):
        L.append(s["transcription_notice"])
    L += session_summary_lines(s)
    L.append(f"□ 우리 기관 관련 질의 ({len(s['org_questions'])}건)")
    if not s["org_questions"]:
        L.append(" ○ 해당 없음")
    for q in s["org_questions"]:
        L.append(f" ○ [{q['time']}] {member_label(q['questioner'], q['party'])}")
        L.append(f"   - 질의: {q['question'] or '-'}")
        if q["answer"]:
            L.append(f"   - 답변: {q['answer']}")
        for a in q["actions"]:
            extra = ", ".join(x for x in (f"담당: {a['dept']}" if a["dept"] else "",
                                          f"기한: {a['due_date']}" if a["due_date"] else "") if x)
            L.append(f"   - 조치: {a['text']}" + (f" ({extra})" if extra else ""))
    L.append(f"□ 위원별 질의·답변 요지 ({len(s['members'])}건)")
    for m in s["members"]:
        L.append(f" ○ [{m['time_range']}] {member_label(m['questioner'], m['party'])}"
                 + (" (질의 중)" if m["is_open"] else "") + f" · {summary_status(m)['label']}")
        if m["question_points"]:
            L.append("   - 질의: " + "; ".join(m["question_points"]))
        elif m["one_line"]:
            L.append(f"   - 요지: {m['one_line']}")
        if m["answer_points"]:
            L.append("   - 답변: " + "; ".join(m["answer_points"]))
        L.extend(f"   - {line}" for line in block_summary_detail_lines(m, m.get("correction_document")))
    L.append(f"□ 요구자료 ({len(s['data_requests'])}건)")
    L += _capped(s["data_requests"], _req_line) or [" ○ 해당 없음"]
    L.append(f"□ 약속·검토사항 ({len(s['commitments'])}건)")
    L += _capped(s["commitments"], _commit_line) or [" ○ 해당 없음"]
    L.append("□ 특이사항(증인 요구·서면질의 등)")
    if not s["special"]:
        L.append(" ○ 해당 없음")
    for r in s["special"][:MAX_LIST]:
        L.append(f" ○ [{r.get('time') or '-'}] {r['label']}: {r['text']}{_special_tail(r)}")
    L.append(f"{s['review']} · {s['notice']}")
    return "\n".join(L)


def _render_md(title: str, s: dict[str, Any], sid: str, base_url: str) -> str:
    def link(uid: int | None) -> str:
        url = utterance_link(sid, uid, base_url)
        return f" [원문]({url})" if url else ""

    L = [f"## {title}", "", f"**{s['subtitle']}**", "", "### □ 진행 현황", f"- {s['progress']['line']}", "",
         f"### □ 우리 기관 관련 질의 ({len(s['org_questions'])}건)"]
    if s.get("transcription_notice"):
        L[4:4] = [s["transcription_notice"], ""]
    summary_lines = session_summary_lines(s, markdown=True)
    if summary_lines:
        L[4:4] = [*summary_lines, ""]
    if not s["org_questions"]:
        L.append("- 해당 없음")
    for q in s["org_questions"]:
        L.append(f"- **[{q['time']}] {member_label(q['questioner'], q['party'])}**{link(q['utterance_id'])}")
        L.append(f"  - 질의: {q['question'] or '-'}")
        if q["answer"]:
            L.append(f"  - 답변: {q['answer']}")
        for a in q["actions"]:
            extra = ", ".join(x for x in (f"담당: {a['dept']}" if a["dept"] else "",
                                          f"기한: {a['due_date']}" if a["due_date"] else "") if x)
            L.append(f"  - 조치: {a['text']}" + (f" ({extra})" if extra else ""))
    L += ["", f"### □ 위원별 질의·답변 요지 ({len(s['members'])}건)"]
    for m in s["members"]:
        L.append(f"- **[{m['time_range']}] {member_label(m['questioner'], m['party'])}** — {m['one_line']} · {summary_status(m)['label']}")
        for p in m["question_points"]:
            L.append(f"  - 질의: {p}")
        for p in m["answer_points"]:
            L.append(f"  - 답변: {p}")
        L.extend(f"  - {line}" for line in block_summary_detail_lines(m, m.get("correction_document")))
    def numbered(rows: list[dict], render) -> list[str]:
        out = [render(r, i + 1).lstrip() + link(r.get("utterance_id")) for i, r in enumerate(rows[:MAX_LIST])]
        if len(rows) > MAX_LIST:
            out.append(f"- … 외 {len(rows) - MAX_LIST}건")
        return out or ["- 해당 없음"]

    L += ["", f"### □ 요구자료 ({len(s['data_requests'])}건)", *numbered(s["data_requests"], _req_line)]
    L += ["", f"### □ 약속·검토사항 ({len(s['commitments'])}건)", *numbered(s["commitments"], _commit_line)]
    L += ["", "### □ 특이사항(증인 요구·서면질의 등)"]
    if not s["special"]:
        L.append("- 해당 없음")
    for r in s["special"][:MAX_LIST]:
        L.append(f"- [{r.get('time') or '-'}] {r['label']}: {r['text']}{_special_tail(r)}{link(r.get('utterance_id'))}")
    L += ["", f"> {s['review']}  ", f"> {s['notice']}"]
    return "\n".join(L)


def departments_of(items: list[dict[str, Any]]) -> list[str]:
    c = Counter(i.get("assignee_dept") for i in items if i.get("assignee_dept"))
    return [d for d, _ in c.most_common()]
