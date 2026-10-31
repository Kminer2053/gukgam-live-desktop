"""저장된 세 판본을 원음과 함께 비교하는 독립 HTML을 만든다."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import re
from typing import Any


ROLE_LABELS = {"member": "질의 위원", "answer": "답변", "chair": "위원장", "unknown": "미상", "mixed": "혼합", "system": "진행 안내"}
STATUS_LABELS = {"held": "보류", "needs_review": "확인 필요", "draft": "AI 초안", "complete": "처리 완료", "partial": "일부 보류", "aligned": "음성 연결", "unaligned": "연결 보류", "matched": "음성 연결", "single_speaker": "단일 음성 화자", "unchanged": "변경 없음", "preserved": "보존본"}

REASON_LABELS = {
    "exact_word_and_single_speaker": "단어 일치·단일 음성 화자 연결",
    "unspoken_spacing_or_punctuation": "공백·문장부호로 음성 대응 제외",
    "unaligned_text": "음성 전사와 연결되지 않은 본문",
    "incomplete_speaker_coverage": "단어 전체의 화자 근거가 부족함",
    "overlapping_speakers": "둘 이상의 음성 화자가 겹침",
    "speaker_change_inside_word": "단어 도중 음성 화자가 바뀜",
}


CSS = r"""
:root{color-scheme:light;--ink:#17283c;--muted:#5b6a7b;--line:#dce3ec;--blue:#2359a4;--paper:#fff;--base:#f4f6fa}
*{box-sizing:border-box}body{margin:0;background:var(--base);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;font-size:15px;line-height:1.6}button,input,textarea,select{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.52}button,input,select,textarea{border:1px solid #bcc9d8;border-radius:7px;background:white;color:var(--ink)}button{padding:6px 11px}button:hover:not(:disabled){background:#edf3fc;border-color:#6687b5}button:focus-visible,input:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid #82adf0;outline-offset:2px}a{color:var(--blue)}.wrap{max-width:1640px;margin:auto;padding:26px 26px 50px}.eyebrow{font-size:12px;font-weight:800;color:#46617d;letter-spacing:.1em}h1{font-size:28px;letter-spacing:-.04em;line-height:1.3;margin:5px 0 7px}h2{font-size:15px;margin:0}.subtitle{color:var(--muted);margin:0}.intro{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.export{white-space:nowrap;margin-top:7px}.note{background:#fff7e6;border:1px solid #ebd4a7;border-radius:9px;padding:11px 15px;margin:17px 0 12px;font-size:14px}.note p{margin:0}.note p+p{margin-top:5px}.player-panel{display:grid;grid-template-columns:auto minmax(240px,1fr) minmax(180px,1fr);gap:16px;align-items:center;background:white;border:1px solid var(--line);border-radius:10px;padding:12px 15px}.player-panel audio{width:100%;height:38px}.player-status{font-size:13px;color:var(--muted);margin:0}.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:18px 0 12px}.search{flex:1;min-width:180px;padding:8px 11px}.filters{display:flex;gap:5px}.filters button[aria-pressed=true]{background:#244d80;color:white;border-color:#244d80}.goto{display:flex;gap:5px;align-items:center;font-size:13px}.goto input{width:62px;padding:7px}.count{font-size:13px;color:var(--muted);margin-left:auto}.legend{font-size:12px;color:var(--muted);display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:9px 0}.pill{display:inline-block;border-radius:5px;padding:1px 7px;font-size:12px;font-weight:650;background:#edf0f4;color:#526071}.r-member .pill,.legend .member{background:#e6efff;color:#285998}.r-answer .pill,.legend .answer{background:#e0f3e9;color:#256a4b}.r-chair .pill,.legend .chair{background:#eee7fa;color:#644097}.r-unknown .pill,.legend .unknown{background:#eceff3;color:#626b79}.r-mixed .pill{background:#fff0cf;color:#805d15}.table-shell{border:1px solid #cfd9e5;border-radius:10px;background:white;overflow:auto;max-height:calc(100vh - 175px);min-height:260px;scroll-padding-top:105px}.grid-head,.stage-grid{display:grid;grid-template-columns:repeat(3,minmax(260px,1fr));min-width:870px}.grid-head{position:sticky;top:0;z-index:4;background:#edf2f8;box-shadow:0 1px 0 #cad6e4}.heading{padding:13px 18px;font-size:16px;font-weight:750;border-right:1px solid #d5dfea}.heading:last-child{border-right:0}.heading small{display:block;font-size:12px;font-weight:450;color:#5b6a7b;margin-top:2px}.step{display:inline-block;font-size:11px;border:1px solid #a8bad1;border-radius:4px;padding:0 4px;margin-right:6px;vertical-align:2px}.compare-row{min-width:870px;border-top:1px solid #cfd9e5;scroll-margin-top:100px}.compare-row[hidden]{display:none}.row-bar{display:flex;gap:10px;align-items:center;padding:8px 14px;background:#f8fafc;flex-wrap:wrap;font-size:12px;border-bottom:1px solid #e3e8ef}.row-number{font-size:14px;font-weight:800;color:#304b6b}.source-id{color:#6b7b8b}.time{font-variant-numeric:tabular-nums;color:#4c6075}.tag{font-size:11px;padding:1px 6px;border:1px solid #ccd8e6;border-radius:5px;background:#edf4ff;color:#3f6090}.tag.held{background:#fff2d9;border-color:#e6d2ac;color:#785916}.row-audio{margin-left:auto;font-size:12px;padding:3px 8px}.cell{padding:16px 18px 18px;min-width:0;border-right:1px solid #e0e6ee;overflow-wrap:anywhere}.cell:last-child{border-right:0}.stage-status{font-size:11px;color:#718094;margin-bottom:9px;display:flex;gap:6px;flex-wrap:wrap}.speaker-line{display:flex;gap:7px;align-items:center;margin-bottom:6px;font-size:13px;font-weight:700}.turn+.turn{border-top:1px dashed #dce4ed;margin-top:12px;padding-top:11px}.turn-text{white-space:pre-wrap;line-height:1.9;font-size:16px;letter-spacing:-.01em}.group{font-size:10px;color:#64748a;font-weight:400}.source-note{font-size:11px;color:#6b7b8b;margin-top:11px;line-height:1.55}.row-foot{display:grid;grid-template-columns:minmax(0,1fr) minmax(210px,1fr);gap:18px;padding:10px 15px;border-top:1px solid #e4e9ef;background:#fcfdff}.evidence{font-size:12px;min-width:0}.evidence summary{cursor:pointer;color:#4d6482}.evidence pre{white-space:pre-wrap;word-break:break-word;font:inherit;background:#f3f6fa;padding:9px;border-radius:6px}.evidence-table{width:100%;font-size:11px;border-collapse:collapse;margin-top:8px}.evidence-table td,.evidence-table th{border-bottom:1px solid #dce3ed;text-align:left;padding:5px 6px;vertical-align:top}.evidence-table button{font-size:11px;padding:1px 6px;white-space:nowrap}.review-control{font-size:12px;display:flex;flex-wrap:wrap;align-items:flex-start;gap:8px}.review-control label{display:flex;gap:5px;align-items:center;white-space:nowrap}.review-control textarea{flex:1;min-width:150px;resize:vertical;padding:6px 8px;min-height:36px;font-size:12px}.review-control input{accent-color:#34649f}.compare-row.flagged .row-bar{box-shadow:inset 4px 0 #be8a24}.compare-row.focused{outline:3px solid #6d9fdb;outline-offset:-3px}.empty{padding:32px;text-align:center;color:var(--muted)}.bottom-note{font-size:12px;color:var(--muted);margin:10px 1px}.technical{margin-top:18px;border:1px solid var(--line);border-radius:8px;background:white;padding:12px 15px;font-size:12px;color:#56677c}.technical summary{cursor:pointer}.technical pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.noscript{border:1px solid #e3c58b;background:#fff6df;padding:10px}.mobile-note{display:none}
@media(max-width:900px){.wrap{padding:18px 12px}.intro{display:block}h1{font-size:23px}.export{margin-top:12px}.player-panel{grid-template-columns:1fr;gap:7px}.table-shell{max-height:78vh}.mobile-note{display:block;color:var(--muted);font-size:12px}.toolbar{gap:7px}.count{width:100%;margin:0}.heading{padding:10px 14px}}
@media print{.wrap{max-width:none;padding:0}.toolbar,.player-panel,.row-audio,.review-control,.export,.bottom-note,.mobile-note{display:none}.table-shell{max-height:none;overflow:visible;border:0}.grid-head{position:static}.grid-head,.stage-grid,.compare-row{min-width:0}.cell{padding:8px}.turn-text{font-size:10px;line-height:1.6}.heading{font-size:11px;padding:7px}.heading small{font-size:9px}.compare-row{break-inside:avoid}.row-foot{display:block}.technical{break-before:page}}
"""


SCRIPT = r"""
'use strict';
(() => {
  const data = JSON.parse(document.getElementById('comparison-data').textContent);
  const rows = Array.from(document.querySelectorAll('.compare-row'));
  const search = document.getElementById('search');
  const count = document.getElementById('count');
  const player = document.getElementById('audio-player');
  const playbackStatus = document.getElementById('playback-status');
  const notes = new Map();
  let mode = 'all';
  let clipEnd = null;
  let pendingPlayback = null;
  let generation = 0;
  let dirty = false;
  const normalized = value => String(value || '').normalize('NFKC').toLocaleLowerCase();
  const searchable = rows.map(row => normalized(row.querySelector('.row-bar').textContent + ' ' + row.querySelector('.stage-grid').textContent));
  function filterRows() {
    const term = normalized(search.value.trim());
    let visible = 0;
    rows.forEach((row, index) => {
      const show = (!term || searchable[index].includes(term)) &&
        (mode === 'all' || (mode === 'changed' && row.dataset.changed === 'true') ||
         (mode === 'held' && row.dataset.held === 'true') || (mode === 'flagged' && notes.get(row.dataset.key)?.flagged));
      row.hidden = !show;
      visible += show ? 1 : 0;
    });
    count.textContent = visible + ' / ' + rows.length + '행 표시';
    document.getElementById('empty').hidden = visible !== 0;
  }
  function chooseMode(next) {
    mode = next;
    document.querySelectorAll('[data-filter]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.filter === mode)));
    filterRows();
  }
  function stamp(seconds) {
    const value = Math.max(0, Number(seconds));
    const minutes = Math.floor(value / 60);
    return minutes + ':' + String(Math.floor(value % 60)).padStart(2, '0');
  }
  function startClip(start, end, label) {
    if (!player || !Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) return;
    const token = ++generation;
    player.pause();
    clipEnd = end;
    const play = () => {
      if (token !== generation) return;
      pendingPlayback = null;
      if (Number.isFinite(player.duration) && end > player.duration + .05) {
        clipEnd = null;
        playbackStatus.textContent = '파일 길이를 벗어난 구간입니다. 근거 시각을 확인해 주세요.';
        return;
      }
      player.currentTime = start;
      playbackStatus.textContent = label + ' · ' + stamp(start) + '–' + stamp(end) + ' 재생';
      const promise = player.play();
      if (promise && typeof promise.catch === 'function') promise.catch(() => {
        if (token === generation) playbackStatus.textContent = '원음 재생을 시작하지 못했습니다. 재생 버튼을 눌러 주세요.';
      });
    };
    if (player.readyState >= 1) play();
    else { pendingPlayback = play; player.load(); }
  }
  document.querySelectorAll('[data-play-start]').forEach(button => button.addEventListener('click', () => {
    if (!button.disabled) startClip(Number(button.dataset.playStart), Number(button.dataset.playEnd), button.dataset.playLabel);
  }));
  if (player) {
    player.addEventListener('loadedmetadata', () => { if (pendingPlayback) pendingPlayback(); });
    player.addEventListener('timeupdate', () => {
      if (clipEnd !== null && player.currentTime >= clipEnd) {
        player.pause(); clipEnd = null;
        playbackStatus.textContent = '선택 구간 재생이 끝났습니다.';
      }
    });
    player.addEventListener('error', () => { generation++; pendingPlayback = null; clipEnd = null; playbackStatus.textContent = '내장 원음 파일을 재생하지 못했습니다.'; });
  }
  document.getElementById('play-all').addEventListener('click', () => {
    if (!player) return;
    generation++; pendingPlayback = null; clipEnd = null;
    player.currentTime = 0;
    playbackStatus.textContent = '전체 원음 · 처음부터 재생';
    const promise = player.play();
    if (promise && typeof promise.catch === 'function') promise.catch(() => { playbackStatus.textContent = '재생 버튼을 눌러 주세요.'; });
  });
  search.addEventListener('input', filterRows);
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => chooseMode(button.dataset.filter)));
  function jump() {
    const value = document.getElementById('goto-row').value.trim();
    const target = rows.find(row => row.dataset.ordinal === value);
    if (!target) { document.getElementById('jump-status').textContent = '해당 행 번호가 없습니다.'; return; }
    search.value = ''; chooseMode('all');
    rows.forEach(row => row.classList.remove('focused'));
    target.classList.add('focused'); target.scrollIntoView({block:'start', behavior:'smooth'});
    target.focus({preventScroll:true});
    document.getElementById('jump-status').textContent = value + '행으로 이동했습니다.';
  }
  document.getElementById('goto-button').addEventListener('click', jump);
  document.getElementById('goto-row').addEventListener('keydown', event => { if (event.key === 'Enter') jump(); });
  rows.forEach(row => {
    const checkbox = row.querySelector('.review-flag');
    const textarea = row.querySelector('.review-note');
    const update = () => {
      notes.set(row.dataset.key, {flagged: checkbox.checked, note: textarea.value});
      dirty = true; row.classList.toggle('flagged', checkbox.checked);
      if (mode === 'flagged') filterRows();
      document.getElementById('note-status').textContent = '저장하지 않은 검토 메모가 있습니다. 파일을 닫기 전에 내보내 주세요.';
    };
    checkbox.addEventListener('change', update); textarea.addEventListener('input', update);
  });
  document.getElementById('export-notes').addEventListener('click', () => {
    const review = data.rows.map((row, index) => ({...row, ...(notes.get(String(index)) || {flagged:false,note:''})}));
    const saved = {schema_version:1, kind:'three_stage_human_review', title:data.title,
      meeting_label:data.meeting_label, source_fingerprint:data.source_fingerprint,
      exported_at:new Date().toISOString(), review};
    const blob = new Blob([JSON.stringify(saved, null, 2)], {type:'application/json;charset=utf-8'});
    const url = URL.createObjectURL(blob); const anchor = document.createElement('a');
    anchor.href = url; anchor.download = '국감_3단계_검토메모.json';
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    document.getElementById('note-status').textContent = '검토 메모 내보내기를 요청했습니다. 다운로드된 JSON 파일을 확인해 주세요.';
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  filterRows();
})();
"""


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _safe_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    for old, new in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(old, new)
    return text


def _digest(value: str) -> str:
    return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()


def _role(value: Any) -> str:
    return str(value) if value in ROLE_LABELS else "unknown"


def _label(value: Any) -> str:
    return STATUS_LABELS.get(str(value), str(value or ""))


def _turns(stage: dict[str, Any], *, raw: bool = False) -> str:
    """지정한 범위 사이에 빈틈이 있어도 본문 문자를 모두 그대로 표시한다."""
    text = stage.get("text", "")
    parts = stage.get("parts", [])
    if not isinstance(text, str) or not isinstance(parts, list):
        raise ValueError("각 판본의 text는 문자열, parts는 목록이어야 합니다")
    spans = []
    cursor = 0
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("화자 범위는 객체여야 합니다")
        start, end = part.get("start"), part.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= cursor <= start < end <= len(text):
            raise ValueError("화자 범위가 겹치거나 원문 범위를 벗어났습니다")
        if cursor < start:
            spans.append((cursor, start, {"role": "unknown", "speaker": "화자 미지정"}))
        spans.append((start, end, part))
        cursor = end
    if cursor < len(text) or not spans:
        spans.append((cursor, len(text), {"role": stage.get("role", "unknown"), "speaker": stage.get("speaker_label", stage.get("speaker", "화자 미지정"))}))
    rendered = []
    for start, end, part in spans:
        role = _role(part.get("role", "unknown"))
        speaker = part.get("speaker") or part.get("speaker_label") or "화자 미지정"
        pill = "수집원 라벨" if raw else ROLE_LABELS[role]
        group = f'<span class="group">연속 묶음 {_escape(part["group_id"])}</span>' if part.get("group_id") is not None else ""
        rendered.append(f'<div class="turn r-{role}"><div class="speaker-line"><span class="pill">{_escape(pill)}</span><span>{_escape(speaker)}</span>{group}</div><div class="turn-text">{_escape(text[start:end])}</div></div>')
    return "".join(rendered)


def _range(start: Any, end: Any, duration: Any) -> tuple[float, float] | None:
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
        return None
    if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 and end > duration + .05:
        return None
    return float(start), float(end)


def _play_button(start: Any, end: Any, duration: Any, label: str, *, enabled: bool, text: str = "구간 재생") -> str:
    span = _range(start, end, duration) if enabled else None
    if span is None:
        return '<button class="row-audio" type="button" disabled title="대응하는 음성 시각이 확인되지 않았습니다">음성 시각 없음</button>'
    return f'<button class="row-audio" type="button" data-play-start="{span[0]:.4f}" data-play-end="{span[1]:.4f}" data-play-label="{_escape(label)}">{_escape(text)}</button>'


def _audio_evidence(audio: dict[str, Any], duration: Any, has_audio: bool, ordinal: Any) -> str:
    parts = audio.get("parts", [])
    content = [f'<details class="evidence"><summary>원음 전사·단어별 연결 근거 ({len(parts)}개)</summary>']
    if audio.get("source_note"):
        content.append(f'<p>{_escape(audio["source_note"])}</p>')
    content.append(f'<pre>{_escape(audio.get("text") or "저장된 음성 전사 없음")}</pre>')
    if parts:
        content.append('<table class="evidence-table"><thead><tr><th>자막 단어</th><th>음성 화자·시각</th><th>연결 상태</th><th>원음</th></tr></thead><tbody>')
        for part in parts:
            start_ms, end_ms = part.get("start_ms"), part.get("end_ms")
            start = start_ms / 1000 if type(start_ms) in (int, float) else None
            end = end_ms / 1000 if type(end_ms) in (int, float) else None
            status = part.get("status", "unaligned")
            valid = has_audio and status in {"draft", "aligned", "matched", "single_speaker", "supported", "ok"}
            stamp = f'{start:.2f}–{end:.2f}초' if _range(start, end, duration) else "시각 미정렬"
            button = _play_button(start, end, duration, f'{ordinal}행 · {part.get("text", "단어")}', enabled=valid, text="듣기")
            content.append(f'<tr><td>{_escape(part.get("text", ""))}</td><td>{_escape(part.get("speaker_id", ""))}<br>{_escape(stamp)}</td><td>{_escape(_label(status))}<br>{_escape(REASON_LABELS.get(str(part.get("reason", "")), "추가 확인 필요"))}</td><td>{button}</td></tr>')
        content.append('</tbody></table>')
    content.append('</details>')
    return "".join(content)


def render(payload: dict[str, Any], audio_data_uri: str = "") -> str:
    """판본·원음 사본만 사용하며 파일·서버·모델에는 접근하지 않는다."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("rows 목록이 필요합니다")
    if audio_data_uri and not re.fullmatch(r"data:audio/(?:wav|wave|x-wav|mpeg|mp3|ogg|mp4);base64,[A-Za-z0-9+/]*={0,2}", audio_data_uri):
        raise ValueError("원음은 내장된 audio data URI만 허용합니다")
    rows = payload["rows"]
    keys = [str(row.get("id")) for row in rows]
    if any(row.get("id") is None for row in rows) or len(set(keys)) != len(keys):
        raise ValueError("원본 발언 ID가 없거나 중복됐습니다")
    ordinals = [str(row.get("ordinal", index + 1)) for index, row in enumerate(rows)]
    if len(set(ordinals)) != len(ordinals):
        raise ValueError("행 번호가 중복됐습니다")
    title = str(payload.get("title", "국감 전사 3단계 비교"))
    duration = payload.get("duration_seconds")
    has_audio = bool(audio_data_uri)
    body_rows = []
    for index, row in enumerate(rows):
        ordinal = row.get("ordinal", index + 1)
        changed = row.get("changed_fields", [])
        badges = "".join(f'<span class="tag">{_escape(field)}</span>' for field in changed)
        if row.get("held"):
            badges += '<span class="tag held">보류 포함</span>'
        audio = row.get("audio") or {}
        button = _play_button(audio.get("start"), audio.get("end"), duration, f"{ordinal}행 원음", enabled=has_audio, text="연결된 부분 듣기")
        cells = []
        for name in ("raw", "live", "post"):
            stage = row.get(name)
            if not isinstance(stage, dict):
                raise ValueError("raw·live·post 판본이 모두 필요합니다")
            status = f'<div class="stage-status">{_escape(_label(stage.get("status")))}</div>' if stage.get("status") else ""
            note = f'<div class="source-note">{_escape(stage["source_note"])}</div>' if stage.get("source_note") else ""
            pieces = stage.get("pieces", []) if name == "raw" else []
            if pieces:
                note += '<details class="evidence"><summary>원수신 자막 조각</summary>'
                for piece in pieces:
                    note += f'<p>표식 {_escape(piece.get("hwa", ""))} · {"화자 판정 있음" if piece.get("scd") is True else "화자 판정 대기"} · {"확정" if piece.get("final") else "미확정"}</p><pre>{_escape(piece.get("raw_text", piece.get("text", "")))}</pre>'
                note += '</details>'
            cells.append(f'<section class="cell" data-stage="{name}">{status}{_turns(stage, raw=name == "raw")}{note}</section>')
        body_rows.append(f'<article class="compare-row" id="row-{index}" data-key="{index}" data-ordinal="{_escape(ordinal)}" data-changed="{str(bool(changed)).lower()}" data-held="{str(bool(row.get("held"))).lower()}" tabindex="-1"><div class="row-bar"><span class="row-number">{_escape(ordinal)}행</span><span class="time">{_escape(row.get("time_label", ""))}</span><span class="source-id">발언 {_escape(row["id"])}</span>{badges}{button}</div><div class="stage-grid">{"".join(cells)}</div><div class="row-foot">{_audio_evidence(audio, duration, has_audio, ordinal)}<div class="review-control"><label><input class="review-flag" type="checkbox">확인 필요</label><textarea class="review-note" rows="1" aria-label="{_escape(ordinal)}행 검토 메모" placeholder="어느 열·단어·화자가 다른지 메모"></textarea></div></div></article>')
    metadata = {"title": title, "meeting_label": payload.get("meeting_label", ""), "source_fingerprint": payload.get("source_fingerprint", ""), "rows": [{"id": row["id"], "ordinal": row.get("ordinal", index + 1)} for index, row in enumerate(rows)]}
    notices = payload.get("notices", [])
    notice_html = "".join(f'<p>{_escape(notice)}</p>' for notice in notices)
    if not notice_html:
        notice_html = '<p>저장된 판본을 비교하는 검토용 사본입니다. 음성 화자와 AI 보정은 원음으로 확인해 주세요.</p>'
    audio_html = f'<audio id="audio-player" controls preload="metadata" src="{_escape(audio_data_uri)}"></audio>' if has_audio else '<span class="player-status">이 파일에는 원음이 포함되지 않았습니다.</span>'
    disabled = "" if has_audio else " disabled"
    technical = {"판본": payload.get("versions", []), "출처": payload.get("provenance", {})}
    csp = f"default-src 'none'; script-src 'sha256-{_digest(SCRIPT)}'; style-src 'sha256-{_digest(CSS)}'; media-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{_escape(csp)}"><title>{_escape(title)}</title><style>{CSS}</style></head><body><main class="wrap"><header class="intro"><div><div class="eyebrow">국감 라이브 · 품질 검토</div><h1>{_escape(title)}</h1><p class="subtitle">{_escape(payload.get("meeting_label", ""))} · {_escape(payload.get("questioner", ""))} · {len(rows)}개 발언</p></div><button id="export-notes" class="export" type="button">검토 메모 내보내기</button></header><aside class="note">{notice_html}</aside><section class="player-panel" aria-label="저장 원음"><div><h2>원음 함께 듣기</h2><button id="play-all" type="button"{disabled}>전체 원음 재생</button></div>{audio_html}<p id="playback-status" class="player-status" role="status">행별 듣기는 연결된 음성 시각을 사용합니다. 음성 화자 번호는 실제 이름과 다릅니다.</p></section><div class="toolbar"><input id="search" class="search" type="search" placeholder="본문·화자·발언 번호 검색" aria-label="비교 본문 검색"><div class="filters" aria-label="발언 필터"><button type="button" data-filter="all" aria-pressed="true">전체</button><button type="button" data-filter="changed" aria-pressed="false">변경 있음</button><button type="button" data-filter="held" aria-pressed="false">보류</button><button type="button" data-filter="flagged" aria-pressed="false">내 확인 표시</button></div><div class="goto"><label for="goto-row">행 번호</label><input id="goto-row" type="number" min="1" aria-label="이동할 행 번호"><button id="goto-button" type="button">이동</button></div><span id="count" class="count" role="status">{len(rows)} / {len(rows)}행 표시</span><span id="jump-status" role="status"></span></div><div class="legend"><span class="pill member">질의 위원</span><span class="pill answer">답변</span><span class="pill chair">위원장</span><span class="pill unknown">미상</span><span>같은 행의 세 판본을 좌우로 읽어 보세요. 변경 표시는 정확도 판정이 아닙니다.</span></div><p class="mobile-note">좁은 화면에서는 표를 좌우로 밀어 세 판본을 비교할 수 있습니다.</p><noscript><p class="noscript">본문은 모두 표시됩니다. 검색·구간 재생·검토 메모 내보내기는 자바스크립트가 필요합니다.</p></noscript><div class="table-shell"><div class="grid-head"><div class="heading"><span class="step">1</span>AI 자막 원문<small>국회 자막 수집원에서 받은 본문·화자 라벨</small></div><div class="heading"><span class="step">2</span>실시간 코드 보정<small>수집 중 저장된 화자·구간 판본</small></div><div class="heading"><span class="step">3</span>질의 종료 후 보정<small>네모트론·Whisper → Jev → LLM의 저장된 결과</small></div></div>{"".join(body_rows)}<p id="empty" class="empty" hidden>조건에 맞는 발언이 없습니다.</p></div><p class="bottom-note" id="note-status">메모는 이 화면에만 남습니다. 닫기 전에 내보내 주세요. 서버로 자동 전송하지 않습니다.</p><details class="technical"><summary>판본 시점·처리 범위·자료 출처</summary><pre>{_escape(json.dumps(technical, ensure_ascii=False, indent=2, allow_nan=False))}</pre></details></main><script id="comparison-data" type="application/json">{_safe_json(metadata)}</script><script>{SCRIPT}</script></body></html>'''
