// 후속조치 트래커(전체 세션): 거르기·인라인 수정(운영자)·CSV 내려받기.
import {
  $, api, badge, debounce, downloadText, DUE_PRESETS, emptyState, errMsg, errorEl, fmt, formDialog, h, icon,
  isOperator, ITEM_KINDS, ITEM_ORIGIN, ITEM_STATUS, queryParams, setQuery, STRENGTHS, toast, toCSV, setKids,
} from './common.js';

const OP = isOperator();
const enc = encodeURIComponent;
const tbody = $('#rows');
let items = [];
let sessions = [];
let depts = [];
let pendingRender = false;

const F = {
  session: $('#f-session'), kind: $('#f-kind'), status: $('#f-status'), dept: $('#f-dept'), q: $('#f-q'), ritual: $('#f-ritual'),
};

function fillSelects() {
  F.kind.append(...Object.entries(ITEM_KINDS).map(([k, v]) => h('option', { value: k }, `${v.icon} ${v.label}`)));
  setKids($('#due-list'), ...DUE_PRESETS.map((d) => h('option', { value: d })));
}

async function loadSessions() {
  try {
    const r = await api('sessions');
    sessions = r.items || [];
    const cur = F.session.value || queryParams().session_id || '';
    setKids(F.session, h('option', { value: '' }, '모든 세션'),
      ...sessions.map((s) => h('option', { value: s.id }, `${s.meeting_date || fmt.ymd(s.created_at)} ${s.title}`)));
    F.session.value = cur;
  } catch { /* 세션 목록 없이도 동작 */ }
}

async function loadDepts() {
  if (!OP) return;
  try {
    const st = await api('settings');
    depts = (st.departments || []).map((d) => d.name).filter(Boolean);
    renderDeptList();
  } catch { /* 무시 */ }
}

function renderDeptList() {
  const all = new Set([...depts, ...items.map((i) => i.assignee_dept).filter(Boolean)]);
  setKids($('#dept-list'), ...[...all].sort().map((d) => h('option', { value: d })));
}

async function load() {
  setQuery({ session_id: F.session.value, kind: F.kind.value, status: F.status.value, dept: F.dept.value.trim() });
  setKids(tbody, h('tr', null, h('td', { colspan: 7 }, h('div', { class: 'loading' }, '불러오는 중…'))));
  try {
    const r = await api('items', { query: { session_id: F.session.value, kind: F.kind.value, status: F.status.value, dept: F.dept.value.trim() } });
    items = r.items || [];
    renderDeptList();
    render();
  } catch (e) {
    setKids(tbody, h('tr', null, h('td', { colspan: 7 }, errorEl(e))));
  }
}

function filtered() {
  const q = F.q.value.trim().toLowerCase();
  return items.filter((it) => !(F.ritual.checked && it.strength === '의례')
    && (!q || `${it.text} ${it.quote} ${it.requester} ${it.speaker} ${it.note} ${it.session_title || ''}`.toLowerCase().includes(q)));
}

function renderKpi(list) {
  const by = {};
  for (const it of list) by[it.status] = (by[it.status] || 0) + 1;
  setKids($('#kpi'), h('span', { class: 'chip' }, `전체 ${fmt.num(list.length)}`),
    ...Object.entries(ITEM_STATUS).filter(([k]) => by[k]).map(([k, l]) => h('button', {
      type: 'button', class: `chip${F.status.value === k ? ' chip-tag' : ''}`, onclick: () => { F.status.value = F.status.value === k ? '' : k; load(); },
    }, `${l} ${fmt.num(by[k])}`)));
}

function render() {
  if (tbody.contains(document.activeElement)) { pendingRender = true; return; }
  pendingRender = false;
  const list = filtered();
  renderKpi(list);
  if (!list.length) {
    setKids(tbody, h('tr', null, h('td', { colspan: 7 }, emptyState(items.length ? '조건에 맞는 항목이 없습니다' : '후속조치가 없습니다',
      items.length ? '거르기 조건을 바꿔 보세요.' : '회의에서 요구자료·약속사항이 추출되면 여기에 모입니다.', 'checklist'))));
    return;
  }
  setKids(tbody, ...list.map(row));
}

function row(it) {
  const k = ITEM_KINDS[it.kind] || { label: it.kind_label, icon: '' };
  const strengths = STRENGTHS[it.kind] || [];
  const patch = (field) => async (e) => {
    const v = e.target.value;
    if ((it[field] ?? '') === v) return;
    try {
      const r = await api(`items/${it.id}`, { method: 'PATCH', body: { [field]: v } });
      const wasEdited = !!it.edited;
      Object.assign(it, r && r.id ? r : { [field]: v });
      toast('저장했습니다.', { type: 'ok', timeout: 1200 });
      // '수정됨' 표시가 새로 붙으면 다시 그린다(입력 중이면 render 가 포커스가 빠질 때까지 미룬다)
      if (field === 'kind' || field === 'strength' || field === 'status' || !!it.edited !== wasEdited) render();
    } catch (err) { toast(errMsg(err), { type: 'err' }); e.target.value = it[field] ?? ''; }
  };
  const link = it.utterance_id ? `/s/${enc(it.session_id)}#u-${it.utterance_id}` : `/s/${enc(it.session_id)}`;
  return h('tr', { class: it.strength === '의례' ? 'dim' : null },
    h('td', { class: 'small', style: { minWidth: '140px' } }, h('a', { href: `/s/${enc(it.session_id)}` }, it.session_title || it.session_id)),
    h('td', { class: 'nowrap mono' }, h('a', { href: link, title: '원문 발언 보기' }, it.time || '보기')),
    h('td', null, h('div', { class: 'cell-stack' },
      OP ? h('select', { class: 'input-sm', value: it.kind, 'aria-label': '종류', onchange: patch('kind') }, Object.entries(ITEM_KINDS).map(([kk, v]) => h('option', { value: kk }, `${v.icon} ${v.short}`)))
        : h('span', { class: `chip k-${it.kind}` }, `${k.icon} ${k.label}`),
      OP && strengths.length ? h('select', { class: 'input-sm', value: it.strength || '', 'aria-label': '강도', onchange: patch('strength') }, h('option', { value: '' }, '-'), strengths.map((s) => h('option', { value: s }, s)))
        : (it.strength ? badge(it.strength, 'b-outline badge-sm') : h('span', { class: 'faint' }, '-')))),
    h('td', { class: 'wrap' },
      h('div', null, it.text, ' ', badge(ITEM_ORIGIN[it.origin] || it.origin, it.origin === 'llm' ? 'b-violet badge-sm' : 'b-gray badge-sm'),
        it.edited ? h('span', { class: 'item-edited', title: '사람이 고친 항목 — AI 재요약·자동 갱신이 덮어쓰지 않습니다' }, '수정됨') : null,
        OP ? h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', title: '요지·메모 수정', 'aria-label': '요지·메모 수정', onclick: () => editText(it) }, icon('edit', 'ic-sm')) : null),
      it.quote && it.quote !== it.text ? h('div', { class: 'small muted clamp-2', title: it.quote }, `“${it.quote}”`) : null,
      it.note ? h('div', { class: 'small', style: { color: 'var(--primary-text)' } }, `메모: ${it.note}`) : null),
    h('td', { class: 'small' }, it.requester || it.speaker || '-'),
    h('td', null, h('div', { class: 'cell-stack' },
      OP ? h('input', { class: 'input-sm', type: 'text', list: 'dept-list', value: it.assignee_dept, placeholder: '담당 부서', 'aria-label': '담당 부서', onchange: patch('assignee_dept') }) : h('span', null, it.assignee_dept || '담당 미정'),
      OP ? h('input', { class: 'input-sm', type: 'text', list: 'due-list', value: it.due_date, placeholder: '기한', 'aria-label': '기한', onchange: patch('due_date') }) : h('span', { class: 'small muted' }, it.due_date ? `기한 ${it.due_date}` : ''))),
    h('td', null, OP
      ? h('select', { class: 'input-sm', value: it.status, 'aria-label': '상태', onchange: patch('status') }, Object.entries(ITEM_STATUS).map(([v, l]) => h('option', { value: v }, l)))
      : badge(it.status_label || ITEM_STATUS[it.status], it.status === 'done' ? 'b-green' : it.status === 'in_progress' ? 'b-blue' : 'b-gray')));
}

async function editText(it) {
  const vals = await formDialog({
    title: '후속조치 수정',
    fields: [
      { name: 'text', label: '요지', type: 'textarea', rows: 3, value: it.text, required: true },
      { name: 'note', label: '메모(조치 계획 등)', type: 'textarea', rows: 3, value: it.note },
    ],
  });
  if (!vals) return;
  try {
    const r = await api(`items/${it.id}`, { method: 'PATCH', body: { text: vals.text.trim(), note: vals.note.trim() } });
    Object.assign(it, r && r.id ? r : vals);
    render();
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

function downloadCsv() {
  const sid = F.session.value;
  if (sid && !F.q.value.trim() && !F.ritual.checked && !F.kind.value && !F.status.value && !F.dept.value.trim()) {
    location.href = `/api/sessions/${enc(sid)}/export?format=csv`;
    return;
  }
  const list = filtered();
  if (!list.length) { toast('내려받을 항목이 없습니다.', { type: 'warn' }); return; }
  const rows = [['회의', '회의일', '발언 시각', '종류', '강도', '요지', '원문', '요구 위원', '발언자', '담당 부서', '기한', '상태', '추출', '메모', '발언 링크']];
  for (const it of list) {
    rows.push([it.session_title || it.session_id, it.meeting_date || '', it.time, it.kind_label || ITEM_KINDS[it.kind]?.label, it.strength, it.text, it.quote,
      it.requester, it.speaker, it.assignee_dept, it.due_date, it.status_label || ITEM_STATUS[it.status], ITEM_ORIGIN[it.origin] || it.origin, it.note,
      it.utterance_id ? `${location.origin}/s/${it.session_id}#u-${it.utterance_id}` : '']);
  }
  downloadText(`후속조치_${fmt.ymd(Date.now())}.csv`, toCSV(rows), 'text/csv;charset=utf-8');
}

function initFromQuery() {
  const q = queryParams();
  if (q.kind) F.kind.value = q.kind;
  if (q.status) F.status.value = q.status;
  if (q.dept) F.dept.value = q.dept;
}

fillSelects();
initFromQuery();
for (const k of ['session', 'kind', 'status']) F[k].addEventListener('change', load);
F.dept.addEventListener('change', load);
F.q.addEventListener('input', debounce(render, 150));
F.ritual.addEventListener('change', render);
$('#csv').addEventListener('click', downloadCsv);
tbody.addEventListener('focusout', () => setTimeout(() => { if (pendingRender && !tbody.contains(document.activeElement)) render(); }, 0));

(async () => {
  await loadSessions();
  await load();
  loadDepts();
})();
