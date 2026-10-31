// 설정 화면: 기관·감시 키워드·부서 규칙·AI 엔진(연결 테스트)·STT·보존·국회 연동, 시스템 상태, 감사 기록.
import { $, $$, api, badge, cloudAgentView, ctx, debounce, emptyState, errMsg, errorEl, fmt, h, icon, isMissingRoute, setBusy, toast, setKids,
} from './common.js';

let ST = null;
let auditRows = [];
let roleStatusTimer = null;
let sttStatusTimer = null;
const CATS = [['agency', '기관'], ['person', '인물'], ['project', '사업·정책'], ['custom', '기타']];

const splitList = (s) => (s || '').split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
const num = (id, def) => { const el = $(id); const v = Number(el?.value); return el && Number.isFinite(v) && el.value !== '' ? v : def; };

async function save(key, value, form) {
  if (ctx.cloud && (!ST || !['org', 'watch_keywords', 'departments'].includes(key))) return false;
  const btn = form.querySelector('button[type="submit"]');
  setBusy(btn, true);
  try {
    const r = await api(`settings/${key}`, { method: 'PUT', body: value });
    // 응답은 갱신값 그 자체(계약) — {key, value} 꼴로 와도 받아 준다
    if (r !== null && r !== undefined) {
      const wrapped = !Array.isArray(r) && typeof r === 'object' && 'value' in r && Object.keys(r).every((k) => k === 'value' || k === 'key');
      ST[key] = wrapped ? r.value : r;
    }
    toast('저장했습니다. 진행 중인 세션에도 적용됩니다.', { type: 'ok', timeout: 2500 });
    return true;
  } catch (e) {
    toast(errMsg(e), { type: 'err', title: '저장 실패' });
    return false;
  } finally {
    setBusy(btn, false);
  }
}

/* ── 우리 기관 ──────────────────────────────────────────── */
function renderOrg() {
  const o = ST.org || {};
  $('#org-name').value = o.name || '';
  $('#org-aliases').value = (o.aliases || []).join(', ');
  $('#org-head-title').value = o.head_title || '';
  $('#org-head-name').value = o.head_name || '';
}
$('#f-org').addEventListener('submit', (e) => {
  e.preventDefault();
  save('org', {
    name: $('#org-name').value.trim(), aliases: splitList($('#org-aliases').value),
    head_title: $('#org-head-title').value.trim(), head_name: $('#org-head-name').value.trim(),
  }, e.currentTarget);
});

/* ── 감시 키워드 ────────────────────────────────────────── */
function watchRow(w = { keyword: '', category: 'custom' }) {
  const kw = typeof w === 'string' ? { keyword: w, category: 'custom' } : w;
  const row = h('div', { class: 'rule-row' },
    h('input', { type: 'text', class: 'input-sm', value: kw.keyword || '', placeholder: '키워드(2글자 이상)', 'aria-label': '키워드', dataset: { f: 'keyword' } }),
    h('select', { class: 'input-sm', value: kw.category || 'custom', 'aria-label': '범주', dataset: { f: 'category' } }, CATS.map(([v, l]) => h('option', { value: v }, l))),
    h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': '삭제', title: '삭제', onclick: () => row.remove() }, icon('trash', 'ic-sm')));
  return row;
}
function renderWatch() {
  const list = ST.watch_keywords || [];
  setKids($('#watch-rows'), ...(list.length ? list.map(watchRow) : [watchRow()]));
}
$('#watch-add').addEventListener('click', () => { const r = watchRow(); $('#watch-rows').append(r); r.querySelector('input').focus(); });
$('#f-watch').addEventListener('submit', (e) => {
  e.preventDefault();
  const out = [];
  for (const row of $$('#watch-rows .rule-row')) {
    const keyword = row.querySelector('[data-f="keyword"]').value.trim();
    if (keyword.length >= 2) out.push({ keyword, category: row.querySelector('[data-f="category"]').value });
  }
  save('watch_keywords', out, e.currentTarget);
});

/* ── 담당 부서 ──────────────────────────────────────────── */
function deptRow(d = { name: '', keywords: [] }) {
  const row = h('div', { class: 'rule-row dept' },
    h('input', { type: 'text', class: 'input-sm', value: d.name || '', placeholder: '부서(예: 계약처)', 'aria-label': '부서 이름', dataset: { f: 'name' } }),
    h('input', { type: 'text', class: 'input-sm', value: (d.keywords || []).join(', '), placeholder: '낱말(쉼표로): 하도급, 발주, 입찰', 'aria-label': '부서 키워드', dataset: { f: 'keywords' } }),
    h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': '삭제', title: '삭제', onclick: () => row.remove() }, icon('trash', 'ic-sm')));
  return row;
}
function renderDept() {
  const list = ST.departments || [];
  setKids($('#dept-rows'), ...(list.length ? list.map(deptRow) : [deptRow()]));
}
$('#dept-add').addEventListener('click', () => { const r = deptRow(); $('#dept-rows').append(r); r.querySelector('input').focus(); });
$('#f-dept').addEventListener('submit', (e) => {
  e.preventDefault();
  const out = [];
  for (const row of $$('#dept-rows .rule-row')) {
    const name = row.querySelector('[data-f="name"]').value.trim();
    const keywords = splitList(row.querySelector('[data-f="keywords"]').value);
    if (name) out.push({ name, keywords });
  }
  save('departments', out, e.currentTarget);
});

/* ── AI 엔진 ────────────────────────────────────────────── */
function llmProvider() { return ($('#f-llm input[name="provider"]:checked') || {}).value || 'rules'; }
function applyProvider() {
  const p = llmProvider();
  for (const el of $$('#f-llm [data-p]')) el.hidden = el.dataset.p !== p;
}
function renderLlm() {
  if (ctx.cloud || !$('#f-llm')) return;
  const l = ST.llm || {};
  const r = $(`#f-llm input[name="provider"][value="${l.provider || 'rules'}"]`);
  if (r) r.checked = true;
  const desktopModel = $('#llm-desktop-model');
  if (desktopModel) desktopModel.textContent = l.desktop_model || '계정 기본 모델';
  $('#llm-local-model').value = l.local_model || '';
  $('#llm-openai-url').value = l.openai_base_url || '';
  $('#llm-openai-model').value = l.openai_model || '';
  $('#llm-openai-key').value = '';
  $('#llm-openai-key').placeholder = l.openai_api_key ? `저장됨(${l.openai_api_key}) — 비워 두면 유지` : '비워 두면 기존 값을 유지합니다';
  $('#llm-anth-model').value = l.anthropic_model || '';
  $('#llm-anth-key').value = '';
  $('#llm-anth-key').placeholder = l.anthropic_api_key ? `저장됨(${l.anthropic_api_key}) — 비워 두면 유지` : '비워 두면 기존 값을 유지합니다';
  $('#llm-rolling').value = l.rolling_interval_sec ?? 300;
  $('#llm-max-tokens').value = l.max_tokens ?? 1200;
  $('#llm-auto').checked = l.auto_block_summary !== false;
  applyProvider();
}
function llmValue() {
  const v = {
    provider: llmProvider(),
    local_model: $('#llm-local-model').value.trim(),
    openai_base_url: $('#llm-openai-url').value.trim(),
    openai_model: $('#llm-openai-model').value.trim(),
    anthropic_model: $('#llm-anth-model').value.trim(),
    auto_block_summary: $('#llm-auto').checked,
    rolling_interval_sec: num('#llm-rolling', 300),
    max_tokens: num('#llm-max-tokens', 1200),
  };
  // 비밀값: 새로 입력했을 때만 보낸다(빈 값·마스킹 값은 서버가 기존 값 유지)
  if ($('#llm-openai-key').value.trim()) v.openai_api_key = $('#llm-openai-key').value.trim();
  if ($('#llm-anth-key').value.trim()) v.anthropic_api_key = $('#llm-anth-key').value.trim();
  return v;
}
(ctx.cloud ? null : $('#f-llm'))?.addEventListener('change', (e) => { if (e.target.name === 'provider') applyProvider(); });
(ctx.cloud ? null : $('#f-llm'))?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const v = llmValue();
  if (v.provider === 'anthropic' && !(ST.llm || {}).anthropic_api_key && !v.anthropic_api_key) {
    toast('Anthropic API 키를 입력하세요.', { type: 'warn' });
    return;
  }
  if (await save('llm', v, e.currentTarget)) renderLlm();
});
(ctx.cloud ? null : $('#llm-test'))?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const out = $('#llm-test-result');
  setBusy(btn, true);
  out.hidden = false;
  out.className = 'test-result';
  out.textContent = '연결을 시험하는 중…(로컬 모델은 처음 불러올 때 시간이 걸립니다)';
  try {
    const r = await api('settings/llm/test', { method: 'POST', body: llmValue() });
    out.className = `test-result ${r.ok ? 'ok' : 'err'}`;
    out.textContent = r.ok
      ? `연결 성공 · ${r.provider || ''} ${r.model || ''} · ${r.latency_ms != null ? `${r.latency_ms}ms` : ''}${r.sample ? ` · 응답: ${String(r.sample).slice(0, 120)}` : ''}`
      : `연결 실패: ${r.error || '알 수 없는 오류'}`;
  } catch (err) {
    out.className = 'test-result err';
    out.textContent = `연결 실패: ${errMsg(err)}`;
  } finally {
    setBusy(btn, false);
  }
});

/* ── 음성인식·보존·국회 ─────────────────────────────────── */
function renderStt() {
  if (ctx.cloud || !$('#f-stt')) return;
  const s = ST.stt || {};
  $('#stt-provider').value = s.provider || 'none';
  if (!$('#stt-provider').value) $('#stt-provider').value = 'none';
  $('#stt-model').value = s.model || '';
  $('#stt-language').value = s.language || 'ko';
  $('#stt-glossary').value = (s.glossary || []).join('\n');
  $('#stt-cpu-cores').value = String(s.nemotron_cpu_cores || 2);
  if ($('#stt-diarize-recordings')) $('#stt-diarize-recordings').checked = s.diarize_recordings !== false;
  applySttProvider();
}
function applySttProvider() {
  const cpu = $('#stt-provider')?.value === 'nemotron_cpu';
  $('#nemotron-prepare-panel').hidden = !cpu;
  $('#nemotron-core-field').hidden = !cpu;
  $('#stt-model').readOnly = cpu;
  $('#stt-language').readOnly = cpu;
  if (cpu) {
    $('#stt-model').value = 'nvidia/nemotron-3.5-asr-streaming-0.6b';
    $('#stt-language').value = 'ko-KR';
    loadSttModelStatus();
  } else {
    clearTimeout(sttStatusTimer);
    $('#stt-model').value = ST.stt?.model || '';
    $('#stt-language').value = ST.stt?.language || 'ko';
  }
}
async function loadSttModelStatus() {
  if (ctx.cloud || !$('#nemotron-status')) return;
  clearTimeout(sttStatusTimer);
  try {
    const r = await api('settings/stt/nemotron/status');
    $('#nemotron-status').textContent = r.message || '파일 준비 상태를 확인하지 못했습니다';
    const p = $('#nemotron-progress');
    p.hidden = !r.preparing;
    p.value = r.progress || 0;
    $('#nemotron-prepare').disabled = r.preparing;
    $('#nemotron-cancel').disabled = !r.preparing;
    if (r.preparing) sttStatusTimer = setTimeout(loadSttModelStatus, 2000);
  } catch (error) { $('#nemotron-status').textContent = errMsg(error); }
}
$('#stt-provider')?.addEventListener('change', applySttProvider);
$('#nemotron-refresh')?.addEventListener('click', loadSttModelStatus);
$('#nemotron-prepare')?.addEventListener('click', async () => {
  if (!$('#nemotron-license').checked) {
    toast('라이선스 확인 항목을 먼저 선택하세요.', { type: 'err' });
    return;
  }
  setBusy($('#nemotron-prepare'), true);
  try { await api('settings/stt/nemotron/prepare', { method: 'POST', body: { confirm: true, accept_license: true } }); }
  catch (error) { toast(errMsg(error), { type: 'err', title: '음성 모델 준비 실패' }); }
  finally { setBusy($('#nemotron-prepare'), false); await loadSttModelStatus(); }
});
$('#nemotron-cancel')?.addEventListener('click', async () => {
  try { await api('settings/stt/nemotron/cancel', { method: 'POST' }); }
  catch (error) { toast(errMsg(error), { type: 'err', title: '준비 취소 확인 실패' }); }
  finally { await loadSttModelStatus(); }
});
window.addEventListener('beforeunload', () => clearTimeout(sttStatusTimer));
(ctx.cloud ? null : $('#f-stt'))?.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (await save('stt', {
    provider: $('#stt-provider').value, model: $('#stt-model').value.trim(), language: $('#stt-language').value.trim() || 'ko',
    nemotron_cpu_cores: num('#stt-cpu-cores', 2),
    diarize_recordings: $('#stt-diarize-recordings')?.checked !== false,
    glossary: splitList($('#stt-glossary').value.replace(/,/g, '\n')),
  }, e.currentTarget)) renderStt();
});

function renderRetention() {
  if (ctx.cloud || !$('#f-retention')) return;
  const r = ST.retention || {};
  $('#ret-audio').value = r.audio_days ?? 7;
  $('#ret-raw').value = r.raw_days ?? 30;
  $('#ret-transcript').value = r.transcript_days ?? 0;
}
(ctx.cloud ? null : $('#f-retention'))?.addEventListener('submit', (e) => {
  e.preventDefault();
  const raw = Math.max(1, num('#ret-raw', 30));
  save('retention', { audio_days: num('#ret-audio', 7), raw_days: raw, transcript_days: num('#ret-transcript', 0) }, e.currentTarget);
});

function renderAssembly() {
  if (ctx.cloud || !$('#f-assembly')) return;
  const a = ST.assembly || {};
  $('#asm-auto-stop').value = a.auto_stop_minutes ?? 30;
  $('#asm-auto').checked = a.auto_detect !== false;
}
(ctx.cloud ? null : $('#f-assembly'))?.addEventListener('submit', (e) => {
  e.preventDefault();
  const mins = Math.max(0, Math.round(num('#asm-auto-stop', 30)));
  save('assembly', { auto_stop_minutes: mins, auto_detect: $('#asm-auto').checked }, e.currentTarget);
});

/* ── 시스템 상태 ────────────────────────────────────────── */
function kvRows(obj) {
  if (!obj || typeof obj !== 'object') return [h('dt', null, '-'), h('dd', null, String(obj ?? '-'))];
  return Object.entries(obj).flatMap(([k, v]) => [h('dt', null, k), h('dd', { class: 'small' }, typeof v === 'object' ? JSON.stringify(v) : String(v))]);
}
const LEVEL = {
  ok: { label: '정상', cls: 'b-green' }, warn: { label: '주의', cls: 'b-orange' }, error: { label: '문제', cls: 'b-red' },
};
const checkLevel = (c) => c.level || (c.ok ? 'ok' : 'error');

/** 사전 점검(GET /api/preflight): STT·AI 엔진·디스크·국회 연동·인증·기관 설정·ffmpeg. 구버전 서버(404/405)면 숨긴다. */
async function loadPreflight() {
  const el = $('#preflight');
  el.hidden = false;
  setKids(el, h('div', { class: 'loading loading-sm' }, '사전 점검 중…'));
  let r;
  try {
    r = await api('preflight');
  } catch (e) {
    if (isMissingRoute(e)) { el.hidden = true; el.replaceChildren(); return; }
    setKids(el, errorEl(e));
    return;
  }
  const checks = Array.isArray(r && r.checks) ? r.checks : [];
  const bad = checks.filter((c) => checkLevel(c) !== 'ok');
  setKids(el,
    h('div', { class: 'row', style: { marginBottom: '8px' } },
      h('h4', { style: { margin: 0 } }, '국감 전 사전 점검'),
      badge(r.ok && !bad.length ? '모두 정상' : `확인 필요 ${bad.length}건`, r.ok && !bad.length ? 'b-green' : (checks.some((c) => checkLevel(c) === 'error') ? 'b-red' : 'b-orange'))),
    h('ul', { class: 'preflight-list' }, checks.map((c) => {
      const lv = LEVEL[checkLevel(c)] || LEVEL.error;
      return h('li', { class: `pf-${checkLevel(c)}` }, badge(lv.label, `${lv.cls} badge-sm`),
        h('b', null, c.label || c.key || ''), c.detail ? h('span', { class: 'small muted' }, c.detail) : null);
    })));
}

async function loadHealth() {
  loadPreflight();
  const el = $('#health');
  setKids(el, h('div', { class: 'loading loading-sm' }, '확인 중…'));
  try {
    const r = await api('health');
    if (ctx.cloud) {
      const agent = r.agent || {};
      const view = cloudAgentView(agent);
      setKids(el,
        h('div', { class: 'row' }, badge(view.label, view.cls), badge(`버전 ${r.version || '-'}`, 'b-gray')),
        h('dl', { class: 'kv' },
          h('dt', null, '마지막 신호'), h('dd', null, agent.heartbeat_at ? fmt.dateTime(agent.heartbeat_at) : '아직 없음'),
          h('dt', null, '동기화 지연'), h('dd', null, agent.push_delay_ms != null ? `${Math.round(agent.push_delay_ms / 1000)}초` : '-'),
          h('dt', null, '대기 작업'), h('dd', null, String(agent.queue ?? 0))),
        ...(r.warnings || []).map((w) => h('p', { class: 'note warn' }, String(w))));
      return;
    }
    setKids(el, 
      h('div', { class: 'row', style: { marginBottom: '12px' } },
        badge(r.ok ? '정상' : '점검 필요', r.ok ? 'b-green' : 'b-red'),
        badge(`버전 ${r.version || '-'}`, 'b-gray'),
        badge(r.auth_enabled ? '인증 켜짐' : r.desktop_local ? '이 PC 전용' : '인증 꺼짐', r.auth_enabled || r.desktop_local ? 'b-green' : 'b-orange')),
      h('div', { class: 'grid-2' },
        h('div', null, h('h4', null, 'AI 요약'), h('dl', { class: 'kv', style: { marginTop: '6px' } }, kvRows(r.llm))),
        h('div', null, h('h4', null, r.stt_optional ? '음성인식(선택 기능)' : '음성인식'),
          r.stt_optional ? h('p', { class: 'small muted' }, '기본 국회 자막 수집에는 음성인식 모델이 필요하지 않습니다.') : null,
          h('dl', { class: 'kv', style: { marginTop: '6px' } }, kvRows(r.stt)))),
      r.cloud?.enabled ? h('div', null, h('h4', null, '클라우드 동기화'),
        h('dl', { class: 'kv' }, kvRows({
          '연결': r.cloud.online ? '연결됨' : '연결 대기',
          '모드': r.cloud.mode === 'active' ? '활성' : '대기',
          '마지막 전송': r.cloud.last_push_ok_ms ? fmt.dateTime(r.cloud.last_push_ok_ms) : '아직 없음',
          '대기 작업': r.cloud.queue ?? 0, '오류': r.cloud.last_error || '없음',
        }))) : null);
  } catch (e) {
    setKids(el, errorEl(e));
  }
}
$('#health-refresh').addEventListener('click', loadHealth);

/* ── 감사 기록 ──────────────────────────────────────────── */
const ACTIONS = {
  'session.start': '수집 시작', 'session.pause': '수집 일시정지', 'session.stop': '수집 종료', 'session.create': '세션 생성',
  'session.delete': '세션 삭제', 'session.update': '세션 수정', 'utterance.edit': '발언 수정', 'block.questioner': '질의자 지정',
  'block.review': '요약 검수', 'item.update': '후속조치 수정', 'item.create': '후속조치 등록', 'settings.update': '설정 변경',
  'login': '로그인', 'login.fail': '로그인 실패', 'logout': '로그아웃', 'roster.update': '명단 변경',
  'auth.login': '로그인', 'auth.login_failed': '로그인 실패', 'auth.login_blocked': '로그인 차단(시도 과다)', 'auth.logout': '로그아웃',
  'session.export': '내보내기', 'ingest.browser_connect': '브라우저 음성 연결', 'session.roster': '명단 변경',
  'session.upload': '파일 업로드', 'session.manual': '수기 입력', 'block.summarize': '블록 요약', 'session.summarize': '회의 요약',
  'block.summary_edit': '요약 수정', 'block.summary': '요약 수정', 'auto_stop': '자동 종료(산회 후)', 'session.auto_stop': '자동 종료(산회 후)',
  'retention.purge': '보존 기간 정리', 'session.marker': '기록 표지',
  'login_ok': '로그인', 'login_fail': '로그인 실패', 'login_blocked': '로그인 차단(시도 과다)',
};
async function loadAudit() {
  const tbody = $('#audit-rows');
  setKids(tbody, h('tr', null, h('td', { colspan: 5 }, h('div', { class: 'loading loading-sm' }, '불러오는 중…'))));
  try {
    const r = await api(ctx.cloud ? 'cloud/audit' : 'audit', { query: { limit: 300 } });
    auditRows = ctx.cloud ? (r.items || []).map((a) => ({
      ts: a.ts, actor: [a.name, a.role].filter(Boolean).join(' · '), action: a.kind,
      target: [a.method, a.path].filter(Boolean).join(' '),
      detail: [a.status ? `응답 ${a.status}` : '', a.ip || '', a.command_id ? `명령 ${a.command_id}` : ''].filter(Boolean).join(' · '),
    })) : r.items || [];
    renderAudit();
  } catch (e) {
    setKids(tbody, h('tr', null, h('td', { colspan: 5 }, errorEl(e))));
  }
}
function renderAudit() {
  const q = $('#audit-filter').value.trim().toLowerCase();
  const list = auditRows.filter((a) => !q || `${a.actor} ${a.action} ${ACTIONS[a.action] || ''} ${a.target} ${a.detail}`.toLowerCase().includes(q));
  const tbody = $('#audit-rows');
  if (!list.length) { setKids(tbody, h('tr', null, h('td', { colspan: 5 }, emptyState('기록이 없습니다', '', 'list')))); return; }
  setKids(tbody, ...list.map((a) => h('tr', null,
    h('td', { class: 'nowrap mono small' }, fmt.dateTime(a.ts)),
    h('td', { class: 'small' }, a.actor || '-'),
    h('td', { class: 'small nowrap', title: a.action }, ACTIONS[a.action] || a.action),
    h('td', { class: 'small mono' }, a.target || ''),
    h('td', { class: 'small wrap' }, h('span', { class: 'clamp-2', title: a.detail }, a.detail || '')))));
}
$('#audit-filter').addEventListener('input', debounce(renderAudit, 120));
$('#audit-refresh').addEventListener('click', loadAudit);

/* ── 시작 ───────────────────────────────────────────────── */
function renderSpeakerReview() {
  if (ctx.cloud || !$('#f-speaker-review')) return;
  $('#review-enabled').checked = ST.transcript_review?.enabled === true;
  $('#review-auto').checked = ST.transcript_review?.auto_block !== false;
  $('#role-decision-provider').value = ST.transcript_review?.role_decision_provider || 'none';
  $('#julia-threads').value = String(ST.transcript_review?.julia_threads ?? 2);
  const audioEnabled = $('#audio-review-enabled'), audioStt = $('#audio-review-stt');
  if (audioEnabled) audioEnabled.checked = ST.audio_review?.enabled === true;
  if (audioStt) audioStt.checked = ST.audio_review?.verify_transcription === true;
  applyRoleDecision();
  loadRoleDecisionStatus();
}
function applyRoleDecision() {
  const selected = $('#role-decision-provider').value;
  $('#julia-prepare-panel').hidden = selected !== 'julia';
  $('#role-decision-description').textContent = {
    none: '추가 역할 판정 요청 없이 기존 ChatGPT의 전체 질의 문맥 보정·요약으로 처리합니다.',
    jev: '설정된 Jev 키로 외부 역할 판정을 요청한 뒤 기존 ChatGPT 보정·요약으로 처리합니다.',
    julia: '이 PC의 CPU에서 네 역할을 판정한 뒤 기존 ChatGPT 보정·요약으로 처리합니다. 기본 사용에 필수인 모델은 아닙니다.',
  }[selected] || '';
}
async function loadRoleDecisionStatus() {
  if (ctx.cloud || !$('#role-decision-provider')) return;
  clearTimeout(roleStatusTimer);
  try {
    const status = await api('settings/role-decision/status');
    const selected = status.selected_provider;
    const worker = status.worker || {};
    const model = status.model || {};
    const label = { none: '추가 판정 모델 없음', jev: 'Jev', julia: 'Julia-1 · CPU' }[selected] || selected;
    $('#jev-status').textContent = `저장된 선택: ${label} · ${selected === 'none' ? '기존 ChatGPT 처리 유지' : worker.available ? '사전 역할 판정 준비됨' : '사전 판정 준비 필요 · 기존 ChatGPT 처리 유지'}`;
    $('#julia-model-status').textContent = status.prepare_error || model.message || '모델 준비 상태를 확인하지 못했습니다.';
    const workerLabels = { idle: '대기', loading: 'CPU 모델 로딩 중', ready: 'CPU 준비 완료', running: 'CPU 역할 판정 중', error: 'CPU 판정 실패', closed: '작업자 해제', unavailable: '준비 필요' };
    $('#julia-worker-status').textContent = `작업자: ${workerLabels[worker.state] || '미기동'}${worker.message ? ` · ${worker.message}` : ''}`;
    const preparing = status.preparing === true || model.state === 'preparing';
    const progress = $('#julia-prepare-progress');
    progress.hidden = !preparing;
    progress.value = Number.isFinite(model.progress) ? Math.max(0, Math.min(1, model.progress)) : 0;
    $('#julia-prepare').disabled = preparing;
    $('#julia-prepare-cancel').disabled = !preparing;
    if (preparing || ['loading', 'running'].includes(worker.state)) roleStatusTimer = setTimeout(loadRoleDecisionStatus, 2000);
  } catch (error) {
    $('#jev-status').textContent = `사전 역할 판정 상태를 확인하지 못했습니다: ${errMsg(error)}`;
  }
}
$('#role-decision-provider')?.addEventListener('change', applyRoleDecision);
$('#role-decision-refresh')?.addEventListener('click', loadRoleDecisionStatus);
$('#julia-prepare')?.addEventListener('click', async (event) => {
  const btn = event.currentTarget;
  setBusy(btn, true);
  try { await api('settings/role-decision/julia/prepare', { method: 'POST', body: { confirm: true } }); }
  catch (error) { toast(errMsg(error), { type: 'err', title: '모델 준비 실패' }); }
  finally { setBusy(btn, false); await loadRoleDecisionStatus(); }
});
$('#julia-prepare-cancel')?.addEventListener('click', async (event) => {
  const btn = event.currentTarget;
  setBusy(btn, true);
  try { await api('settings/role-decision/julia/cancel', { method: 'POST' }); }
  catch (error) { toast(errMsg(error), { type: 'err', title: '준비 취소 확인 실패' }); }
  finally { setBusy(btn, false); await loadRoleDecisionStatus(); }
});
window.addEventListener('beforeunload', () => clearTimeout(roleStatusTimer));
$('#f-speaker-review')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (await save('transcript_review', { enabled: $('#review-enabled').checked, auto_block: $('#review-auto').checked,
    role_decision_provider: $('#role-decision-provider').value, julia_threads: num('#julia-threads', 2) }, e.target)) renderSpeakerReview();
});
$('#f-audio-review')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  await save('audio_review', { enabled: $('#audio-review-enabled').checked, verify_transcription: $('#audio-review-stt').checked }, e.target);
});

(async () => {
  try {
    ST = await api('settings');
  } catch (e) {
    setKids($('#admin-loading'), h('div', { class: 'card' }, emptyState(e.status === 403 ? '운영자만 설정을 볼 수 있습니다' : '설정을 불러오지 못했습니다', errMsg(e), 'alert')));
    if (ctx.cloud) {
      // 수집 PC가 없어도 클라우드 상태·감사 기록은 열람한다. 못 읽은 설정을 빈 값으로 저장하지 않는다.
      $('#admin').hidden = false;
      for (const id of ['#f-org', '#f-watch', '#f-dept']) {
        for (const el of $$(`${id} input, ${id} select, ${id} button`)) el.disabled = true;
      }
      for (const id of ['#watch-add', '#dept-add']) { const el = $(id); if (el) el.disabled = true; }
      loadHealth();
      loadAudit();
    }
    return;
  }
  $('#admin-loading').hidden = true;
  $('#admin').hidden = false;
  renderOrg();
  renderWatch();
  renderDept();
  renderLlm();
  renderSpeakerReview();
  renderStt();
  renderRetention();
  renderAssembly();
  loadHealth();
  loadAudit();
})();
