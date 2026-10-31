// 질의 종료 뒤 음성 대조 제어. 기존 수집·녹음 제어와 독립적으로 동작한다.
import { $, api, ctx, errMsg, h, infoDialog, isOperator, ROLE_LABELS, toast } from './common.js';

const state = { snapshot: null, loading: false, busy: false, error: '', timer: null,
  controller: null, left: false, ready: false, reportLoading: false };
const base = `sessions/${encodeURIComponent(ctx.sid)}/question-audio`;
const labels = {
  disabled: '꺼짐', idle: '대기', receiving: '음성 수신 중', collecting: '질의 녹음 보관 중', listening: '질의 녹음 보관 중',
  waiting: '대기', waiting_tail: '뒤 문맥 대기', queued: '분석 대기', analyzing: '음성 분석 중',
  processing: '음성 분석 중', running: '음성 분석 중', reviewing: '보정 초안 정리 중', draft: '보정 초안', completed: '대조 완료 · 초안 확인',
  needs_review: '확인 필요', held: '확인 필요', failed: '분석 실패', interrupted: '음성 끊김 · 확인 필요',
  partial: '일부 확인 필요', unchanged: '수정 없음', stale: '판본 변경 · 재검토 필요',
  timeout: '시간 초과', skipped: '실행 보류',
};

function available() {
  return isOperator() && !ctx.cloud && !!ctx.sid && !!$('#question-audio-panel');
}

function phaseLabel(phase) { return labels[phase] || '확인 필요'; }
function roleLabel(role) {
  return typeof role === 'string' ? role.split(/\s*\/\s*/).map(value => ROLE_LABELS[value] || '미확정').join(' / ') : '';
}

function render() {
  const panel = $('#question-audio-panel');
  if (!panel) return;
  panel.hidden = !available();
  if (panel.hidden) return;
  const current = state.snapshot;
  const enabled = current?.enabled === true;
  $('#question-audio-status').textContent = state.error ? '음성 대조 상태 확인 필요'
    : state.busy ? '요청 처리 중' : !state.ready ? '음성 대조 상태 확인 중'
      : current?.phase === 'disabled' ? '꺼짐' : `${enabled ? '켜짐' : '꺼짐'} · ${phaseLabel(current?.phase || 'disabled')}`;
  $('#question-audio-detail').textContent = state.error || current?.detail || '';
  $('#question-audio-enable').hidden = enabled;
  $('#question-audio-enable').disabled = state.loading || state.busy || !state.ready || !!state.error
    || current?.can_enable !== true;
  $('#question-audio-disable').hidden = !enabled;
  $('#question-audio-disable').disabled = state.loading || state.busy;
  $('#question-audio-refresh').disabled = state.loading || state.busy;
  const jobs = $('#question-audio-jobs');
  jobs.replaceChildren();
  for (const job of Array.isArray(current?.jobs) ? current.jobs.slice(0, 32) : []) {
    const bid = Number(job.block_id);
    const canOpen = job.report_ready === true && Number.isSafeInteger(bid) && bid > 0;
    jobs.append(h('div', { class: 'stack-sm' },
      h('strong', { class: 'small' }, `${job.questioner || '질의자 확인 필요'} · ${phaseLabel(job.state)}`),
      job.detail ? h('p', { class: 'small muted' }, job.detail) : null,
      canOpen ? h('button', { class: 'btn btn-xs', type: 'button', onclick: () => openReport(bid) }, '보정 결과 열기') : null));
  }
  if (!jobs.childNodes.length) jobs.append(h('p', { class: 'small muted' }, '아직 대조한 질의가 없습니다.'));
}

function clearTimer() { clearTimeout(state.timer); state.timer = null; }

function schedule() {
  clearTimer();
  // 숨겨진 화면에서는 GET 조회만 멈춘다. 서버 작업에는 제어 요청을 보내지 않는다.
  if (!available() || state.left || document.hidden) return;
  const activeJob = state.snapshot?.jobs?.some(job => ['waiting', 'waiting_tail', 'queued', 'analyzing', 'processing', 'running', 'reviewing'].includes(job.state));
  if (state.snapshot?.enabled || activeJob) state.timer = setTimeout(() => refresh(), 5000);
}

async function refresh() {
  if (!available() || state.left || document.hidden || state.loading || state.busy) return;
  clearTimer();
  state.loading = true;
  const controller = new AbortController();
  state.controller = controller;
  render();
  try {
    const result = await api(`${base}/status`, { signal: controller.signal });
    if (controller.signal.aborted || state.left) return;
    state.snapshot = result; state.ready = true; state.error = '';
  } catch (error) {
    if (!controller.signal.aborted) state.error = errMsg(error);
  } finally {
    if (state.controller === controller) state.controller = null;
    state.loading = false;
    render();
    if (controller.signal.aborted && !document.hidden && !state.left) refresh();
    else schedule();
  }
}

async function control(action) {
  if (!available() || state.left || state.busy || state.loading || !state.ready) return;
  if (!['enable', 'disable'].includes(action)) return;
  if (action === 'enable' && (state.error || state.snapshot?.enabled || state.snapshot?.can_enable !== true)) return;
  if (action === 'disable' && !state.snapshot?.enabled) return;
  clearTimer(); state.busy = true; state.error = ''; render();
  try {
    // 서비스는 기존 PCM만 사용한다. 이 버튼에서 수집 시작이나 설정 변경을 요청하지 않는다.
    state.snapshot = await api(`${base}/${action}`, { method: 'POST' });
    state.ready = true;
  } catch (error) {
    state.error = errMsg(error);
    toast(state.error, { type: 'err', title: '질의 음성 대조' });
  } finally {
    state.busy = false; render();
    // 결과 불명 요청을 반복하지 않고 실제 상태만 읽는다.
    if (state.error) await refresh();
    else schedule();
  }
}

function stageParts(stage) {
  // 서버의 문자 좌표는 유니코드 문자 기준이다. 잘못된 범위는 전체 본문으로 보존한다.
  if (typeof stage?.text !== 'string') return [];
  const chars = Array.from(stage.text);
  const parts = Array.isArray(stage.parts) ? stage.parts : [];
  const result = [];
  let cursor = 0;
  for (const part of parts) {
    if (!part || !Number.isSafeInteger(part.start) || !Number.isSafeInteger(part.end)
      || part.start < cursor || part.end <= part.start || part.end > chars.length) {
      return [{ text: stage.text, role: 'unknown', speaker: '화자 구간 확인 필요' }];
    }
    if (part.start > cursor) result.push({ text: chars.slice(cursor, part.start).join(''), role: 'unknown', speaker: '' });
    result.push({ text: chars.slice(part.start, part.end).join(''), role: part.role,
      speaker: typeof part.speaker === 'string' ? part.speaker : '', group_id: part.group_id });
    cursor = part.end;
  }
  if (cursor < chars.length || !result.length) {
    result.push({ text: chars.slice(cursor).join(''), role: 'unknown', speaker: '' });
  }
  return result;
}

function stageHolds(stage) {
  // 행별 보류가 있으면 전체 작업 상태보다 우선한다. 구판에는 실패 상태만 대체 적용한다.
  return {
    speaker: typeof stage?.speaker_held === 'boolean' ? stage.speaker_held : stage?.status === 'held',
    text: typeof stage?.text_held === 'boolean' ? stage.text_held
      : ['held', 'failed', 'timeout', 'interrupted', 'stale'].includes(stage?.text_status),
    continuity: stage?.continuity_held === true,
  };
}

function stageCell(name, stage) {
  const titles = { raw: 'AI 실시간 자막 · 원형 보존', live: '실시간 보정 · 1차 초안', post: '질의 전체 전사 보정 · 최종 초안' };
  const cell = h('section', { class: `qa-stage qa-stage-${name}` }, h('h4', null, titles[name]));
  const status = typeof stage?.status === 'string' ? stage.status : 'unavailable';
  const text = typeof stage?.text === 'string' ? stage.text : null;
  if (name === 'raw') {
    cell.append(h('p', { class: 'small muted' }, status === 'preserved' ? '보존된 AI 자막 원문' : '원문 보존본 확인 필요'));
  } else if (name === 'live') {
    cell.append(h('p', { class: 'small muted' }, typeof stage?.source_note === 'string'
      ? stage.source_note : '수집 중 저장된 코드 판정'));
  } else {
    const holds = stageHolds(stage);
    cell.append(h('p', { class: `small ${holds.speaker ? 'qa-stage-held' : 'muted'}` },
      status === 'reviewing' ? '검토 중 · 아직 최종 보정 결과가 아닙니다.'
        : status === 'unavailable' ? '보정 결과 없음'
          : holds.speaker ? '화자 보류 · 원음 확인 필요' : 'AI 보정 초안 · 검수 필요'));
    if (holds.text) cell.append(h('p', { class: 'small qa-stage-held' }, '문장 교정 보류 · 확인 필요'));
    if (holds.continuity) cell.append(h('p', { class: 'small qa-stage-held' }, '앞뒤 발언 연결 보류 · 확인 필요'));
    if (stage?.text_status) cell.append(h('p', { class: 'small muted' }, `문장 교정: ${phaseLabel(stage.text_status)}`));
  }
  if (name !== 'live' && typeof stage?.source_note === 'string' && stage.source_note) {
    cell.append(h('p', { class: 'small muted' }, stage.source_note));
  }
  if (name === 'post' && Array.isArray(stage?.units)) {
    for (const unit of stage.units) {
      cell.append(h('div', { class: `qa-stage-part qa-stage-role-${safeRole(unit.role)}` },
        h('p', { class: 'qa-stage-speaker small' }, [unit.speaker, roleLabel(unit.role), scopeLabel(unit.scope)].filter(Boolean).join(' · ')),
        unit.risk_flags?.includes('speaker_identity_unverified') ? h('p', { class: 'small qa-stage-held' }, '답변자·발언자 실명 확인 필요') : null,
        h('div', { class: 'qa-stage-text' }, unit.text), sourceLinks(unit.source_spans)));
    }
    for (const joined of Array.isArray(stage.joined_into) ? stage.joined_into : []) {
      if (Number.isSafeInteger(joined.utterance_id) && joined.utterance_id > 0) {
        cell.append(h('p', { class: 'small muted' }, '앞선 발언과 함께 정리: ',
          h('a', { href: `#qa-source-${joined.utterance_id}` }, `발언 ${joined.utterance_id}`)));
      }
    }
  } else if (text === null) {
    cell.append(h('p', { class: 'qa-stage-empty' }, name === 'raw' ? '이 발언의 AI 자막 원문 보존본이 없습니다.'
      : name === 'post' && status === 'reviewing' ? '질의 종료 후 결과를 정리하고 있습니다.' : '표시할 판본이 없습니다.'));
  } else if (name === 'raw') {
    cell.append(h('div', { class: 'qa-stage-text' }, text));
  } else {
    for (const part of stageParts(stage)) {
      const role = ['member', 'answer', 'chair', 'unknown', 'system'].includes(part.role) ? part.role : 'unknown';
      const label = [part.speaker, roleLabel(role)].filter(Boolean).join(' · ');
      const group = typeof part.group_id === 'string' || typeof part.group_id === 'number' ? ` · 연속 묶음 ${part.group_id}` : '';
      cell.append(h('div', { class: `qa-stage-part qa-stage-role-${role}` },
        part.text.trim() ? h('p', { class: 'qa-stage-speaker small' }, `${label || '화자 확인 필요'}${group}`) : null,
        h('div', { class: 'qa-stage-text' }, part.text)));
    }
  }
  return cell;
}

function changedStages(stages) {
  const { raw, live, post } = stages;
  if (typeof raw?.text === 'string' && typeof live?.text === 'string' && raw.text !== live.text) return true;
  if (typeof live?.text !== 'string' || typeof post?.text !== 'string') return false;
  if (post.stage === 'question_audio_final') {
    if (post.joined_into?.length || post.units?.length !== 1) return true;
    const unit = post.units[0];
    const liveParts = Array.isArray(live.parts) ? live.parts : [];
    return live.text !== unit.text || liveParts.length !== 1 || liveParts[0].role !== unit.role
      || (liveParts[0].speaker || '') !== (unit.speaker || '');
  }
  if (live.text !== post.text) return true;
  const signature = stage => JSON.stringify((Array.isArray(stage?.parts) ? stage.parts : []).map(part =>
    [part?.start, part?.end, part?.role || 'unknown', part?.speaker || '', part?.group_id ?? null]));
  return signature(live) !== signature(post);
}

function threeStageRows(rows) {
  const outer = h('section', { class: 'qa-comparison stack-sm' });
  const content = h('div', { class: 'qa-comparison-rows stack-sm' });
  const shown = [];
  const count = h('span', { class: 'small muted', role: 'status' });
  let mode = 'all';
  const choices = [['all', '전체'], ['changed', '변경 있음'], ['held', '보류']];
  const buttons = [];
  const update = () => {
    let visible = 0;
    for (const item of shown) {
      item.el.hidden = !(mode === 'all' || mode === 'changed' && item.changed || mode === 'held' && item.held);
      if (!item.el.hidden) visible++;
    }
    buttons.forEach(item => { item.button.setAttribute('aria-pressed', String(item.value === mode)); });
    count.textContent = `${visible} / ${shown.length}개 발언 표시`;
  };
  for (const [value, title] of choices) {
    const button = h('button', { type: 'button', class: 'btn btn-xs', 'aria-pressed': value === mode ? 'true' : 'false',
      onclick: () => { mode = value; update(); } }, title);
    buttons.push({ value, button });
  }
  outer.append(h('div', { class: 'qa-comparison-toolbar' }, buttons.map(item => item.button), count),
    h('p', { class: 'small muted' }, '3열은 별도 보정 제안입니다. 변경 표시는 정확도 판정이 아니며, 원문과 실시간 판본은 보존됩니다.'), content);
  for (const row of rows) {
    const stages = row.stages;
    const changed = changedStages(stages);
    const holds = stageHolds(stages.post);
    const held = holds.speaker || holds.text || holds.continuity;
    const el = h('article', { class: 'qa-comparison-row', id: `qa-source-${row.utterance_id}` },
      h('div', { class: 'qa-comparison-row-title' }, h('strong', null, row.utterance_id ? `발언 ${row.utterance_id}` : '대조 구간'),
        changed ? h('span', { class: 'qa-comparison-badge' }, '판본 변경') : null,
        held ? h('span', { class: 'qa-comparison-badge qa-stage-held' }, '보류 포함') : null),
      h('div', { class: 'qa-stages' }, ['raw', 'live', 'post'].map(name => stageCell(name, stages[name]))),
      h('details', { class: 'qa-comparison-evidence' }, h('summary', null, '음성 전사·보정 근거'),
        h('p', { class: 'qa-stage-text' }, typeof row.audio_text === 'string' ? row.audio_text : typeof row.transcript === 'string' ? row.transcript : '저장된 음성 전사가 없습니다.'),
        h('p', { class: 'small muted' }, row.reason || row.detail || '보정 근거 확인 필요')));
    shown.push({ el, changed, held });
    content.append(el);
  }
  update();
  return outer;
}

function safeRole(role) { return ['member', 'answer', 'chair', 'unknown', 'system', 'other'].includes(role) ? role : 'unknown'; }
function scopeLabel(scope) { return { question: '', context: '질의 밖 문맥', uncertain: '범위 확인 필요' }[scope] || ''; }
function sourceLinks(spans) {
  const ids = [...new Set((Array.isArray(spans) ? spans : []).map(s => s?.utterance_id))]
    .filter(uid => Number.isSafeInteger(uid) && uid > 0);
  return h('div', { class: 'review-source-links' }, ids.map(uid => h('a', { class: 'small', href: `#qa-source-${uid}` }, `원문 #${uid}`)));
}

function finalDocument(report) {
  const document = report.final_document || report.refinement?.final_document;
  if (!document || !['draft', 'partial'].includes(document.status) || document.preview_only !== true
      || !Array.isArray(document.units) || !document.units.length) return null;
  const sourceIds = new Set((Array.isArray(report.rows) ? report.rows : []).map(row => row.utterance_id));
  if (document.units.some(unit => !unit || typeof unit.text !== 'string' || typeof unit.speaker !== 'string'
      || !Array.isArray(unit.source_spans) || !unit.source_spans.length
      || unit.source_spans.some(span => !Number.isSafeInteger(span?.utterance_id) || !sourceIds.has(span.utterance_id)))) return null;
  return document;
}

function documentRows(document, rows) {
  const sources = new Map(rows.map(row => [row.utterance_id, row]));
  const time = ms => Number.isFinite(ms) ? new Date(ms).toLocaleTimeString('ko-KR', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
  return h('section', { class: 'qa-final-document stack-sm' },
    h('h3', null, '질의 전체 전사 보정 · 최종 초안'),
    h('p', { class: 'small muted' }, '원자막과 1차 초안, 질의 전체 녹음 전사를 대조한 검수용 본문입니다. 원문·화자·요약 채택과 별개이며 원음 정답 검증 전입니다.'),
    document.status === 'partial' ? h('p', { class: 'small qa-stage-held' }, '확인이 필요한 구간을 함께 표시합니다.') : null,
    h('ol', { class: 'rows qa-final-rows' }, document.units.map(unit => {
      const first = unit.source_spans[0].utterance_id;
      const ts = unit.ts_start ?? sources.get(first)?.ts_start;
      return h('li', { class: `u qa-final-row r-${safeRole(unit.role)}` },
        h('div', { class: 'u-time' }, time(ts) || `#${first}`),
        h('div', { class: 'u-spk' }, h('strong', null, unit.speaker || roleLabel(unit.role)),
          h('span', { class: 'role' }, roleLabel(unit.role))),
        h('div', { class: 'u-text' },
          unit.risk_flags?.includes('speaker_identity_unverified') ? h('p', { class: 'small qa-stage-held' }, '답변자·발언자 실명 확인 필요') : null,
          scopeLabel(unit.scope) ? h('p', { class: 'small qa-stage-held' }, scopeLabel(unit.scope)) : null,
          h('p', { class: 'qa-stage-text' }, unit.text), sourceLinks(unit.source_spans)));
    })));
}

function reportBody(report) {
  const rows = Array.isArray(report.rows) ? report.rows : [];
  const document = finalDocument(report);
  const body = h('div', { class: 'stack' },
    h('p', null, report.detail || '원자막과 음성 대조 근거를 확인하세요.'),
    h('p', { class: 'small muted' }, 'AI 보정 초안입니다. 모델 간 일치나 처리 완료는 화자 정확도 검증을 뜻하지 않습니다.'));
  const ownership = report.ownership;
  if (ownership && typeof ownership.questioner === 'string' && ownership.questioner) {
    body.append(h('p', null, `이 초안의 질의자: ${ownership.questioner}`));
  }
  if (ownership?.status === 'held') {
    body.append(h('p', { class: 'small' }, '질의 범위 확인 필요 · 자동 요약 보류'));
  }
  const summary = typeof report.summary === 'string' ? [report.summary]
    : Array.isArray(report.summary) ? report.summary.filter(line => typeof line === 'string') : [];
  if (report.summary && !Array.isArray(report.summary) && typeof report.summary === 'object') {
    if (typeof report.summary.one_line === 'string') summary.push(report.summary.one_line);
    for (const [field, label] of [['question_points', '질의'], ['answer_points', '답변'],
      ['issues', '지적'], ['data_requests', '요구자료'], ['commitments', '약속·검토'], ['held_attributions', '귀속 검토']]) {
      for (const value of Array.isArray(report.summary[field]) ? report.summary[field] : []) {
        const text = typeof value === 'string' ? value : value && typeof value.text === 'string' ? value.text : '';
        if (text) summary.push(`${label}: ${text}`);
      }
    }
  }
  if (summary.length) body.append(h('section', { class: 'stack-sm' }, h('strong', null, '질의 내용 정리'),
    typeof report.summary?.review_note === 'string' && report.summary.review_note.trim()
      ? h('p', { class: 'small qa-stage-held' }, report.summary.review_note) : null,
    summary.map(line => h('p', null, line))));
  if (document) body.append(documentRows(document, rows));
  else if (report.final_document || report.refinement?.final_document) {
    body.append(h('p', { class: 'small qa-stage-held' }, '최종 본문은 보류되었거나 원문 연결을 확인해야 합니다. 아래 보존 판본을 확인하세요.'));
  }
  if (report.refinement && !(report.final_document || report.refinement?.final_document)) {
    const refinement = report.refinement;
    const patches = Array.isArray(refinement.patches) ? refinement.patches : [];
    body.append(h('section', { class: 'stack-sm' }, h('strong', null, '전사문 교정 제안'),
      h('p', { class: 'small muted' }, '화자 구간을 고정한 별도 검토안입니다. 원문과 위 요약의 본문에는 아직 반영하지 않았습니다.'),
      h('p', null, `화자 검토: ${phaseLabel(refinement.speaker_status)} · 문장 교정: ${phaseLabel(refinement.text_status)}`),
      refinement.complete === false ? h('p', { class: 'small qa-stage-held' },
        '화자 구간 또는 행 사이 연결에 확인이 필요한 부분이 남아 있습니다.') : null,
      patches.length ? patches.map(patch => h('p', null,
        `발언 ${patch.utterance_id || ''}: ${typeof patch.before === 'string' ? patch.before : ''} → ${typeof patch.after === 'string' ? patch.after : ''} · 검수 필요`))
        : h('p', null, '선택된 단어 교정안이 없습니다.')));
  }
  const comparisonRows = rows.filter(row => row.stages && typeof row.stages === 'object');
  if (comparisonRows.length) body.append(threeStageRows(comparisonRows));
  for (const row of rows.filter(row => !row.stages || typeof row.stages !== 'object')) {
    const role = roleLabel(row.proposed_role);
    const beforeRole = roleLabel(row.before_role);
    const parts = Array.isArray(row.parts) ? row.parts.filter(part => part && typeof part.text === 'string') : [];
    body.append(h('section', { class: 'stack-sm' },
      h('strong', null, row.utterance_id ? `발언 ${row.utterance_id}` : '대조 구간'),
      h('p', null, `원자막: ${row.original_text || row.caption_text || ''}`),
      h('p', null, `음성 전사: ${row.audio_text || row.transcript || ''}`),
      h('p', null, `기존 판정: ${[row.before_speaker, beforeRole].filter(Boolean).join(' · ') || '미확정'}`),
      row.proposed_speaker || role ? h('p', null, `보정 후보: ${[row.proposed_speaker, role].filter(Boolean).join(' · ')}`) : null,
      parts.length ? h('div', { class: 'stack-sm' }, h('strong', { class: 'small' }, '보정 후 발언 구분'),
        parts.map(part => h('div', { class: 'small' },
          h('strong', null, `${[part.speaker, roleLabel(part.role)].filter(Boolean).join(' · ') || '화자 확인 필요'}: `),
          part.text))) : null,
      h('p', { class: 'small muted' }, row.reason || row.detail || '확인 필요')));
  }
  if (!rows.length) body.append(h('p', null, '표시할 대조 행이 없습니다. 처리 상태와 보류 사유를 확인하세요.'));
  return body;
}

async function openReport(bid) {
  if (!available() || state.left || state.reportLoading || !Number.isSafeInteger(bid) || bid <= 0) return;
  state.reportLoading = true;
  try {
    const report = await api(`${base}/blocks/${bid}`);
    if (state.left) return;
    const comparison = Array.isArray(report.rows) && report.rows.some(row => row.stages && typeof row.stages === 'object');
    const opened = infoDialog({ title: comparison ? '질의 음성 대조 · 3판본 비교' : '질의 음성 대조 · 보정 초안', body: reportBody(report), wide: true });
    if (comparison && opened?.dlg?.style) opened.dlg.style.width = 'min(1280px, calc(100vw - 24px))';
  } catch (error) { toast(errMsg(error), { type: 'err', title: '음성 대조 결과' }); }
  finally { state.reportLoading = false; }
}

function init() {
  if (!available()) return;
  $('#question-audio-enable').addEventListener('click', () => control('enable'));
  $('#question-audio-disable').addEventListener('click', () => control('disable'));
  $('#question-audio-refresh').addEventListener('click', () => refresh());
  $('#question-audio-panel').addEventListener('toggle', () => { if ($('#question-audio-panel').open) refresh(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimer(); state.controller?.abort(); }
    else refresh();
  });
  window.addEventListener('pagehide', () => { state.left = true; clearTimer(); state.controller?.abort(); });
  window.addEventListener('pageshow', () => { state.left = false; refresh(); });
  refresh();
}

init();
