// 상황보고: 구조화 보고서·평문·마크다운, 기관 관련만, 복사·내려받기·인쇄.
import { $, api, copyText, ctx, emptyState, errMsg, fmt, h, popupMenu, queryParams, setBusy, setQuery, store, setKids,
} from './common.js';
import { renderSituationReport, setReportExpanded } from './report-view.js';

const SID = ctx.sid;
const enc = encodeURIComponent;
let R = null;
let view = 'report';
let timer = 0;
let loadGeneration = 0;
let loadController = null;
let printedDetails = [];

function setView(v) {
  view = v;
  for (const [id, key] of [['#v-report', 'report'], ['#v-text', 'text'], ['#v-md', 'md']]) {
    const on = key === v;
    $(id).setAttribute('aria-selected', String(on));
    $(id).setAttribute('aria-pressed', String(on));
  }
  render();
}

function render() {
  const body = $('#rp-body');
  if (!R) return;
  if (view === 'report') {
    const previousDetails = new Map([...body.querySelectorAll('details.rp-question')].map(el => [el.id, el.open]));
    const report = renderSituationReport(R, {
      sessionHref: blockId => `/s/${enc(SID)}#b-${Number(blockId)}`,
      sourceHref: uid => `/s/${enc(SID)}#u-${Number(uid)}`,
    });
    for (const detail of report.querySelectorAll('details.rp-question')) {
      if (previousDetails.has(detail.id)) detail.open = previousDetails.get(detail.id);
    }
    setKids(body, report);
    return;
  }
  const txt = view === 'md' ? (R.markdown || R.text || '') : (R.text || R.markdown || '');
  if (!txt.trim()) {
    setKids(body, h('div', { class: 'card' }, emptyState('보고할 내용이 아직 없습니다', '발언이 쌓이고 질의 블록 요약이 만들어지면 채워집니다.', 'report')));
    return;
  }
  setKids(body, h('pre', { class: `report-pre${view === 'md' ? ' md' : ''}`, tabindex: '0', 'aria-label': '상황보고 본문' }, txt));
}

async function load() {
  const btn = $('#rp-refresh');
  const generation = ++loadGeneration;
  loadController?.abort();
  loadController = new AbortController();
  setBusy(btn, true);
  $('#rp-copy').disabled = $('#rp-dl').disabled = true;
  $('#rp-body').setAttribute('aria-busy', 'true');
  $('#rp-status').textContent = '저장된 보고 내용을 불러오는 중…';
  const orgOnly = $('#org-only').checked;
  setQuery({ org_only: orgOnly ? 1 : '' });
  try {
    const result = await api(`sessions/${enc(SID)}/report`, { query: { org_only: orgOnly ? 1 : 0 }, signal: loadController.signal });
    if (generation !== loadGeneration) return;
    R = result;
    if (R.title) document.title = `상황보고 · ${R.title} · 국감 라이브`;
    $('#rp-time').textContent = R.generated_at ? `${fmt.dateTime(R.generated_at)} 기준` : '';
    $('#rp-status').textContent = orgOnly ? '우리 기관 관련 질의·후속조치만 표시하고 있습니다.' : '';
    render();
  } catch (e) {
    if (generation !== loadGeneration || e.name === 'AbortError') return;
    R = null;
    $('#rp-status').textContent = '';
    setKids($('#rp-body'), h('div', { class: 'card' }, emptyState('상황보고를 만들지 못했습니다', errMsg(e), 'alert')));
  } finally {
    if (generation === loadGeneration) {
      setBusy(btn, false);
      $('#rp-body').setAttribute('aria-busy', 'false');
      $('#rp-copy').disabled = $('#rp-dl').disabled = !R;
    }
  }
}

function setAuto(on) {
  store.set('report.auto', on);
  clearInterval(timer);
  if (on) timer = setInterval(() => { if (!document.hidden) load(); }, 60000);
}

$('#v-report').addEventListener('click', () => setView('report'));
$('#v-text').addEventListener('click', () => setView('text'));
$('#v-md').addEventListener('click', () => setView('md'));
$('#org-only').addEventListener('change', load);
$('#auto').addEventListener('change', (e) => setAuto(e.target.checked));
$('#rp-refresh').addEventListener('click', load);
$('#rp-copy').addEventListener('click', () => {
  if (!R) return;
  copyText(view === 'md' ? (R.markdown || R.text) : (R.text || R.markdown), '상황보고를 복사했습니다. 메신저에 붙여 넣으세요.');
});
/** 내려받기 주소: 미리보기와 같은 범위가 되도록 '우리 기관 관련만' 값을 함께 넘긴다 */
function exportHref(fmtName, orgOnly) {
  return `/api/sessions/${enc(SID)}/export?format=${fmtName}${orgOnly ? '&org_only=1' : ''}`;
}

$('#rp-dl').addEventListener('click', (e) => {
  const orgOnly = $('#org-only').checked;
  const tag = orgOnly ? ' · 우리 기관 관련만' : '';
  popupMenu(e.currentTarget, [
    { label: `워드 문서(.docx)${tag}`, icon: 'file-text', href: exportHref('docx', orgOnly) },
    { label: `마크다운(.md)${tag}`, icon: 'file', href: exportHref('md', orgOnly) },
    { label: `텍스트(.txt)${tag}`, icon: 'file', href: exportHref('txt', orgOnly) },
    'sep',
    { label: '인쇄', icon: 'report', onClick: () => window.print() },
  ], { label: '내려받기' });
});

// 접힌 질의도 인쇄본에 빠지지 않게 한 뒤, 인쇄 종료 시 사용자가 읽던 펼침 상태로 돌린다.
window.addEventListener('beforeprint', () => {
  printedDetails = [...$('#rp-body').querySelectorAll('details.rp-question')].map(detail => [detail, detail.open]);
  setReportExpanded($('#rp-body'), true);
});
window.addEventListener('afterprint', () => {
  for (const [detail, open] of printedDetails) detail.open = open;
  printedDetails = [];
});

$('#back').href = `/s/${enc(SID)}`;
$('#org-only').checked = queryParams().org_only === '1';
$('#auto').checked = store.get('report.auto', false);
setAuto($('#auto').checked);
load();
