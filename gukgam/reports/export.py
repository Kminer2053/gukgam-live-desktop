"""세션 내보내기: md · txt · docx(표 포함) · json · csv(후속조치 항목, UTF-8 BOM).

export_session(repo, sid, fmt, org_only=False, base_url='') → (본문 bytes, media_type, 파일명).
org_only 면 우리 기관 관련 질의 블록(요약 관련도 high·medium 또는 기관명·기관장 언급 알림)과 그 항목·발언만 담는다
(상황보고 화면의 '우리 기관 관련만'과 같은 범위). 파일명은 한글을 포함하므로 웹 계층에서 Content-Disposition 을
RFC 5987(filename*=UTF-8'') 로 내보내야 한다.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from typing import Any

from .. import serialize, settings
from ..config import KST
from ..db import now_ms
from ..models import ITEM_KIND_LABELS, ITEM_STATUS_LABELS, ROLE_LABELS, SOURCE_LABELS, STATUS_LABELS
from ..repo import Repo
from .display import report_display, stored_summaries, utterance_blocks
from .summary_status import audio_transcript_notice, summary_status
from .situation import (NOTICE, block_summary, build_situation_report, clean_item_text, edited_by, hm, item_is_org,
                        kst_stamp, member_label, org_block_ids, review_label, SESSION_SUMMARY_FILTER_NOTICE,
                        session_summary_lines, stored_session_summary, block_summary_detail_lines)

FORMATS = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "json": "application/json; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}
_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')
_LATEST_EXPORT_SUMMARY = object()
CSV_COLUMNS = ["번호", "세션", "시각", "구분", "강도", "요지", "인용", "화자", "요구 위원", "담당부서", "기한", "상태",
               "출처", "신뢰도", "메모", "발언ID", "블록ID"]


def _filename(session: dict[str, Any], fmt: str, org_only: bool = False) -> str:
    day = (session.get("meeting_date") or "").replace("-", "") or datetime.fromtimestamp(
        (session.get("created_at") or now_ms()) / 1000, KST).strftime("%Y%m%d")
    title = _UNSAFE.sub("_", session.get("title") or "국정감사").strip("_")[:60] or "국정감사"
    suffix = ("_후속조치" if fmt == "csv" else "") + ("_우리기관" if org_only else "")
    return f"{day}_{title}{suffix}.{fmt}"


def _speaker(u: dict[str, Any]) -> str:
    role = u.get("speaker_role") or "unknown"
    name = u.get("speaker") or ""
    if role == "system":
        return "기록"
    label = ROLE_LABELS.get(role, "")
    if name and role in ("member", "chair") and name not in ("위원장",):
        return f"{name}({label})"
    return name or label or "미상"


def _collect(repo: Repo, sid: str, org_only: bool = False, *, _session_summary: Any = _LATEST_EXPORT_SUMMARY) -> dict[str, Any]:
    session = repo.get_session(sid)
    if session is None:
        raise KeyError(sid)
    terms = settings.watch_terms(repo)
    display = report_display(repo, sid)
    blocks, utts = display["blocks"], display["utts"]
    items = repo.list_items(sid, include_dismissed=True, limit=-1)
    sums = stored_summaries(display, repo, sid, terms, block_summary)
    summary = None if org_only else (repo.latest_summary(sid, "session")
                                    if _session_summary is _LATEST_EXPORT_SUMMARY else _session_summary)
    org_ids: set[int] | None = None
    if org_only:
        qblocks = [b for b in blocks if b["kind"] == "question"]
        org_ids = org_block_ids(repo, sid, qblocks, sums, utterance_blocks(utts))
        org_terms = [t for t in terms if t[1] in ("agency", "person")]
        blocks = [b for b in blocks if b["id"] in org_ids]
        utts = [u for u in utts if u.get("block_id") in org_ids]
        items = [i for i in items if item_is_org(i, org_ids, org_terms)]
        sums = {k: v for k, v in sums.items() if k in org_ids}
    return {"session": session, "stats": repo.session_stats(sid), "blocks": blocks, "utts": utts, "items": items,
            "sums": sums, "summary": summary, "roster": repo.get_roster(sid, include_global=False),
            "org_only": org_only, "org_ids": org_ids, "_display": display,
            "correction_documents": {b["id"]: display["correction_documents"][b["id"]] for b in blocks
                                     if b["id"] in display.get("correction_documents", {})}}


def _block_title(b: dict[str, Any]) -> str:
    if b["kind"] == "question":
        t = member_label(b.get("questioner") or "", b.get("questioner_party") or "") + " 질의"
        if b.get("status") == "needs_name":
            t += " [성명 확인 필요]"
        return t
    return "위원장 진행" if b["kind"] == "chair" else "기타 발언"


def _meta_rows(d: dict[str, Any]) -> list[tuple[str, str]]:
    s, st = d["session"], d["stats"]
    return [
        ("회의", s.get("title") or ""),
        ("위원회", s.get("committee") or "-"),
        ("피감기관", s.get("agencies") or "-"),
        ("회의일", s.get("meeting_date") or "-"),
        ("수집원", SOURCE_LABELS.get(s["source_type"], s["source_type"])),
        *([("음성 전사 안내", audio_transcript_notice(s["source_type"]))]
          if audio_transcript_notice(s["source_type"]) else []),
        ("수집 시간", f"{serialize.ymd_hm(st.get('ts_first'))} ~ {serialize.ymd_hm(st.get('ts_last'))}"
         if st.get("ts_first") else "-"),
        ("상태", STATUS_LABELS.get(s["status"], s["status"])),
        ("발언 수", str(st.get("utterances") or 0)),
        ("질의 블록", str(st.get("question_blocks") or 0)),
        *([("범위", f"우리 기관 관련 질의 {len(d['org_ids'] or ())}건만(다른 질의·발언·항목 제외)")]
          if d.get("org_only") else []),
        ("내보낸 시각", kst_stamp()),
    ]


def _engine(sm: dict[str, Any]) -> str:
    """저장된 출처의 고정 라벨과 사람 수정 여부를 표시한다."""
    who = sm.get("edited_by") or ""
    return summary_status(sm)["label"] + (f" · 수정: {who}" if who else "")


def _summary_lines(d: dict[str, Any]) -> list[str]:
    if d.get("org_only"):
        return session_summary_lines({"session_summary_notice": SESSION_SUMMARY_FILTER_NOTICE})
    return session_summary_lines({"session_summary": stored_session_summary(d["summary"])})


# ── 형식별 ───────────────────────────────────────────────────────

def _md(repo: Repo, sid: str, d: dict[str, Any], base_url: str = "") -> str:
    s = d["session"]
    L = [f"# {s.get('title')}" + (" (우리 기관 관련)" if d.get("org_only") else ""), "", f"> {NOTICE}", "",
         "| 항목 | 내용 |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in _meta_rows(d)]
    rep = build_situation_report(repo, sid, org_only=bool(d.get("org_only")), base_url=base_url,
                                 _display=d.get("_display"), _session_summary=d["summary"])
    L += ["", rep["markdown"].replace("## ", "## 상황보고 — ", 1)]
    L += ["", "## 후속조치 항목", "", "| 시각 | 구분 | 요지 | 요구 위원 | 담당 | 기한 | 상태 |", "|---|---|---|---|---|---|---|"]
    for i in d["items"]:
        text = clean_item_text(i["text"]).replace("|", "\\|")
        L.append(f"| {hm(i.get('utt_ts'))} | {ITEM_KIND_LABELS.get(i['kind'], i['kind'])} | {text} | "
                 f"{i.get('requester') or ''} | {i.get('assignee_dept') or ''} | {i.get('due_date') or ''} | "
                 f"{ITEM_STATUS_LABELS.get(i.get('status') or 'open', '')} |")
    L += ["", "## 발언 기록"]
    by_block: dict[int | None, list[dict]] = {}
    for u in d["utts"]:
        by_block.setdefault(u.get("block_id"), []).append(u)
    for b in d["blocks"]:
        L += ["", f"### {_block_title(b)} {serialize.hms(b['ts_start'])}–{serialize.hms(b['ts_end'])}"]
        sm = d["sums"].get(b["id"])
        if sm:
            L.append(f"> 요약({_engine(sm)}): {sm.get('one_line') or ''}")
            for p in sm.get("question_points") or []:
                L.append(f"> - 질의: {p}")
            for p in sm.get("answer_points") or []:
                L.append(f"> - 답변: {p}")
            L.extend(f"> {line}" for line in block_summary_detail_lines(sm, d["correction_documents"].get(b["id"])))
            L.append("")
        for u in by_block.get(b["id"], []):
            L.append(f"- `{serialize.hms(u['ts_start'])}` **{_speaker(u)}** {u['text']}")
    loose = by_block.get(None, [])
    if loose:
        L += ["", "### 블록 미지정 발언"] + [f"- `{serialize.hms(u['ts_start'])}` **{_speaker(u)}** {u['text']}"
                                         for u in loose]
    L += ["", "---", f"{review_label(d['blocks'])} · {NOTICE}"]
    return "\n".join(L) + "\n"


def _txt(repo: Repo, sid: str, d: dict[str, Any], base_url: str = "") -> str:
    s = d["session"]
    L = [s.get("title") or "", *[f"{k}: {v}" for k, v in _meta_rows(d)], f"※ {NOTICE}", "", "=" * 40,
         build_situation_report(repo, sid, org_only=bool(d.get("org_only")), base_url=base_url,
                                _display=d.get("_display"), _session_summary=d["summary"])["text"], "=" * 40]
    L += ["", "[발언 기록]"]
    cur = object()
    for u in d["utts"]:
        if u.get("block_id") != cur:
            cur = u.get("block_id")
            b = next((x for x in d["blocks"] if x["id"] == cur), None)
            if b:
                L += ["", f"── {_block_title(b)} {serialize.hms(b['ts_start'])}–{serialize.hms(b['ts_end'])} ──"]
        L.append(f"[{serialize.hms(u['ts_start'])}] {_speaker(u)}: {u['text']}")
    return "\n".join(L) + "\n"


def _json(repo: Repo, sid: str, d: dict[str, Any]) -> bytes:
    org = bool(d.get("org_only"))
    rolling = None if org else repo.latest_summary(sid, "rolling")
    out = {
        "exported_at": now_ms(),
        "notice": NOTICE,
        "session": serialize.session(d["session"], d["stats"]),
        "roster": [{k: m.get(k) for k in ("name", "role", "party", "title")} for m in d["roster"]],
        "blocks": [serialize.block(b) for b in d["blocks"]],
        "utterances": d["utts"] if d["_display"]["snapshot"] is not None
                      else [serialize.utterance(u) for u in d["utts"]],
        "items": [serialize.item(i) for i in d["items"]],
        "summary": None if org else serialize.summary(d["summary"]),
        "rolling": None if org else serialize.summary(rolling),
    }
    if d["_display"]["snapshot"] is not None:
        out["display_generation"] = d["_display"]["snapshot"].get("display_generation")
    if org:
        out["filter"] = {"org_only": True, "block_ids": sorted(d["org_ids"] or ())}
    return json.dumps(out, ensure_ascii=False, indent=1, default=str).encode("utf-8")


_CSV_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(v: Any) -> Any:
    """엑셀이 수식으로 해석하지 않도록 =,+,-,@ 로 시작하는 문자열 칸 앞에 ' 를 붙인다(CSV 수식 주입 방지)."""
    if isinstance(v, str) and v.startswith(_CSV_FORMULA):
        return "'" + v
    return v


def _csv(d: dict[str, Any]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    title = d["session"].get("title") or ""
    for i in d["items"]:
        w.writerow([csv_cell(v) for v in [i["id"], title, serialize.hms(i.get("utt_ts")) if i.get("utt_ts") else "",
                    ITEM_KIND_LABELS.get(i["kind"], i["kind"]), i.get("strength") or "", clean_item_text(i["text"]),
                    i.get("quote") or "", i.get("speaker") or "", i.get("requester") or "",
                    i.get("assignee_dept") or "", i.get("due_date") or "",
                    ITEM_STATUS_LABELS.get(i.get("status") or "open", i.get("status") or ""),
                    {"rule": "규칙", "llm": "AI", "manual": "수동"}.get(i.get("origin") or "", i.get("origin") or ""),
                    i.get("confidence") if i.get("confidence") is not None else "", i.get("note") or "",
                    i.get("utterance_id") or "", i.get("block_id") or ""]])
    return ("﻿" + buf.getvalue()).encode("utf-8")   # 엑셀 한글 깨짐 방지(BOM)


def _docx(repo: Repo, sid: str, d: dict[str, Any], base_url: str = "") -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    doc = Document()
    font = "맑은 고딕"
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        st = doc.styles[name]
        st.font.name = font
        rpr = st.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(rfonts)
        rfonts.set(qn("w:eastAsia"), font)
    doc.styles["Normal"].font.size = Pt(10)

    def table(rows: list[list[str]], header: list[str] | None = None, widths: list[float] | None = None) -> None:
        t = doc.add_table(rows=0, cols=len(rows[0]) if rows else len(header or [1]))
        t.style = "Table Grid"
        if header:
            cells = t.add_row().cells
            for c, h in zip(cells, header):
                c.text = ""
                run = c.paragraphs[0].add_run(h)
                run.bold = True
        for r in rows:
            cells = t.add_row().cells
            for c, v in zip(cells, r):
                c.text = str(v)
        if widths:
            from docx.shared import Cm
            for row in t.rows:
                for c, w in zip(row.cells, widths):
                    c.width = Cm(w)

    s = d["session"]
    doc.add_heading((s.get("title") or "국정감사 기록") + (" (우리 기관 관련)" if d.get("org_only") else ""), level=0)
    p = doc.add_paragraph()
    run = p.add_run(f"※ {NOTICE}")
    run.italic = True
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x88, 0x55, 0x00)
    table([[k, v] for k, v in _meta_rows(d)], widths=[3.0, 13.0])

    rep = build_situation_report(repo, sid, org_only=bool(d.get("org_only")), base_url=base_url,
                                 _display=d.get("_display"), _session_summary=d["summary"])
    doc.add_heading("상황보고", level=1)
    for line in rep["text"].splitlines()[1:]:
        para = doc.add_paragraph(line)
        if line.startswith("□"):
            para.runs[0].bold = True

    doc.add_heading("후속조치 항목", level=1)
    rows = [[hm(i.get("utt_ts")), ITEM_KIND_LABELS.get(i["kind"], i["kind"]), clean_item_text(i["text"]),
             i.get("requester") or "", i.get("assignee_dept") or "", i.get("due_date") or "",
             ITEM_STATUS_LABELS.get(i.get("status") or "open", "")] for i in d["items"]]
    if rows:
        table(rows, header=["시각", "구분", "요지", "요구 위원", "담당", "기한", "상태"],
              widths=[1.4, 2.0, 7.0, 1.8, 1.6, 1.8, 1.4])
    else:
        doc.add_paragraph("추출된 항목 없음")

    doc.add_heading("질의 블록별 요지", level=1)
    for b in d["blocks"]:
        if b["kind"] != "question":
            continue
        sm = d["sums"].get(b["id"]) or {}
        doc.add_heading(f"{_block_title(b)}  {serialize.hms(b['ts_start'])}–{serialize.hms(b['ts_end'])}", level=2)
        doc.add_paragraph(sm.get("one_line") or "")
        for pt in sm.get("question_points") or []:
            doc.add_paragraph(f"질의: {pt}", style="List Bullet")
        for pt in sm.get("answer_points") or []:
            doc.add_paragraph(f"답변: {pt}", style="List Bullet")
        for line in block_summary_detail_lines(sm, d["correction_documents"].get(b["id"])):
            doc.add_paragraph(line)
        note = doc.add_paragraph()
        r = note.add_run(f"요약 엔진: {_engine(sm) if sm else '-'} · "
                         f"{'검수 완료' if b.get('review_status') == 'reviewed' else '요약 초안(미검수)'}"
                         + (f" · 요약 수정: {edited_by(b)}" if edited_by(b) else ""))
        r.font.size = Pt(8)

    doc.add_heading("발언 기록", level=1)
    cur = object()
    for u in d["utts"]:
        if u.get("block_id") != cur:
            cur = u.get("block_id")
            b = next((x for x in d["blocks"] if x["id"] == cur), None)
            if b:
                doc.add_heading(f"{_block_title(b)}  {serialize.hms(b['ts_start'])}–{serialize.hms(b['ts_end'])}",
                                level=3)
        para = doc.add_paragraph()
        t = para.add_run(f"[{serialize.hms(u['ts_start'])}] ")
        t.font.size = Pt(8)
        sp = para.add_run(f"{_speaker(u)} ")
        sp.bold = True
        para.add_run(u["text"])

    foot = doc.add_paragraph()
    foot.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    fr = foot.add_run(review_label(d["blocks"]))
    fr.font.size = Pt(8)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_session(repo: Repo, sid: str, fmt: str, *, org_only: bool = False,
                   base_url: str = "", _session_summary: Any = _LATEST_EXPORT_SUMMARY) -> tuple[bytes, str, str]:
    """(본문, media_type, 파일명). fmt: md|txt|docx|json|csv(후속조치 항목).

    org_only: 우리 기관 관련 질의 블록과 그 항목·발언만(상황보고 화면의 '우리 기관 관련만'과 같은 범위).
    base_url: 상황보고 원문 링크 앞부분(예: http://10.0.0.5:8765).
    """
    fmt = (fmt or "md").lower().lstrip(".")
    if fmt not in FORMATS:
        raise ValueError(f"지원하지 않는 형식입니다: {fmt} (md, txt, docx, json, csv 중 선택)")
    d = _collect(repo, sid, org_only=org_only, _session_summary=_session_summary)
    if fmt == "md":
        body = _md(repo, sid, d, base_url).encode("utf-8")
    elif fmt == "txt":
        body = _txt(repo, sid, d, base_url).encode("utf-8")
    elif fmt == "json":
        body = _json(repo, sid, d)
    elif fmt == "csv":
        body = _csv(d)
    else:
        body = _docx(repo, sid, d, base_url)
    return body, FORMATS[fmt], _filename(d["session"], fmt, org_only)
