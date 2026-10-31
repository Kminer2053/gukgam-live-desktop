// 전체 검색: 저장한 표시 판본의 말묶음으로 이동하고 원행 링크는 별도로 보존한다.
import {
  $, api, badge, correctionNotices, emptyState, errorEl, fmt, h, markedText, mentionRanges, normText, queryParams, setQuery, speakerLabel,
  splitTerms, termRanges, setKids,
} from './common.js';

const LIMIT = 50;
const form = $('#search-form');
const elQ = $('#q');
const elSession = $('#session');
const elSpeaker = $('#speaker');
const elResults = $('#results');
const elSummary = $('#summary');
const elMore = $('#more');
let offset = 0;
let total = 0;
let lastQuery = null;
let searchGeneration = 0;
let searchRequest = null;

async function loadSessions() {
  try {
    const r = await api('sessions');
    const cur = queryParams().session_id || '';
    elSession.append(...(r.items || []).map((s) => h('option', { value: s.id }, `${s.meeting_date || fmt.ymd(s.created_at)} ${s.title}`)));
    elSession.value = cur;
  } catch { /* 목록 없이도 검색 가능 */ }
}

/** 긴 발언은 검색어 주변만 잘라 보여 준다 */
function snippet(text, terms, max = 220) {
  if (text.length <= max) return text;
  const lower = text.toLowerCase();
  let pos = -1;
  for (const t of terms) { const i = lower.indexOf(t.toLowerCase()); if (i >= 0 && (pos < 0 || i < pos)) pos = i; }
  if (pos < 0) return `${text.slice(0, max)}…`;
  const start = Math.max(0, pos - Math.floor(max / 3));
  return `${start > 0 ? '…' : ''}${text.slice(start, start + max)}${start + max < text.length ? '…' : ''}`;
}

function resultEl(u, terms) {
  const full = normText(u.text);
  const text = snippet(full, terms);
  const cut = text !== full;
  const uid = Number.isSafeInteger(u.id) && u.id > 0 ? u.id : 0;
  const bid = Number.isSafeInteger(u.block_id) && u.block_id > 0 ? u.block_id : 0;
  const anchor = typeof u.display_key === 'string' && u.display_key && (uid || bid)
    ? `#d-${bid}-${uid}-${encodeURIComponent(u.display_key)}` : uid ? `#u-${uid}` : '';
  return h('a', { class: 'result', href: `/s/${encodeURIComponent(u.session_id)}${anchor}` },
    h('div', { class: 'rh' },
      h('span', { class: 'mono' }, `${u.meeting_date || fmt.ymd(u.ts_start)} ${u.time || fmt.hms(u.ts_start)}`),
      h('span', null, '·'),
      h('span', { class: 'ellipsis', style: { maxWidth: '420px' } }, u.session_title || '')),
    h('div', { class: 'rs' }, speakerLabel(u)),
    correctionNotices(u).length ? h('div', { class: 'chips', role: 'note' },
      ...correctionNotices(u).map(label => badge(label, 'b-orange badge-sm'))) : null,
    h('div', { class: 'rt' }, markedText(text, termRanges(text, terms), cut ? [] : mentionRanges(text, u.mentions))));
}

async function run({ append = false } = {}) {
  if (append && searchRequest) return;
  const generation = append ? searchGeneration : ++searchGeneration;
  if (!append) { searchRequest?.abort(); searchRequest = null; }
  // '더 보기'는 지금 화면에 보이는 결과(마지막으로 실행한 검색)를 이어 받는다 — 입력창을 고쳤어도 섞지 않는다
  const src = append && lastQuery ? lastQuery
    : { q: elQ.value.trim(), session_id: elSession.value, speaker: elSpeaker.value.trim() };
  const { q, session_id, speaker } = src;
  if (!append) setQuery({ q, session_id, speaker });
  if (!q) {
    offset = 0; total = 0; lastQuery = null;
    setKids(elResults, emptyState('검색어를 입력하세요', '발언 본문과 화자 이름에서 찾습니다.', 'search'));
    elSummary.textContent = '';
    elMore.hidden = true;
    return;
  }
  if (!append) { offset = 0; total = 0; setKids(elResults, h('div', { class: 'loading' }, '찾는 중…')); }
  lastQuery = { q, session_id, speaker };
  const controller = new AbortController(), pageOffset = offset;
  searchRequest = controller;
  elMore.disabled = true;
  try {
    const r = await api('search', { signal: controller.signal,
      query: { q, session_id, speaker, limit: LIMIT, offset: pageOffset } });
    if (generation !== searchGeneration || searchRequest !== controller) return;
    const list = r.items || [];
    const terms = splitTerms(q);
    if (!append) elResults.replaceChildren();
    elResults.append(...list.map((u) => resultEl(u, terms)));
    total = append ? total + list.length : list.length;
    offset = pageOffset + list.length;
    elMore.hidden = list.length < LIMIT;
    elSummary.textContent = total ? `‘${q}’ 검색 결과 ${fmt.num(total)}건${list.length === LIMIT ? ' 이상' : ''} · 최신 발언 순` : '';
    if (!total) setKids(elResults, emptyState('결과가 없습니다', '다른 낱말이나 더 짧은 검색어로 찾아 보세요. 음성인식 결과라 표기가 다를 수 있습니다.', 'search'));
  } catch (e) {
    if (generation !== searchGeneration || searchRequest !== controller || e?.name === 'AbortError') return;
    if (!append) setKids(elResults, errorEl(e));
    else elResults.append(errorEl(e));
  } finally {
    if (searchRequest === controller) { searchRequest = null; elMore.disabled = false; }
  }
}

form.addEventListener('submit', (e) => { e.preventDefault(); run(); });
elSession.addEventListener('change', () => { if (elQ.value.trim()) run(); });
elMore.addEventListener('click', () => { if (lastQuery) run({ append: true }); });

(async () => {
  const p = queryParams();
  elQ.value = p.q || '';
  elSpeaker.value = p.speaker || '';
  await loadSessions();
  if (!searchGeneration) run(); // 목록을 기다리는 동안 사용자가 실행한 검색을 중복 실행하지 않는다.
})();
