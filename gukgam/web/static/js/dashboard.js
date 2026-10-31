// 대시보드: 진행 중 세션, 오늘 국회 중계(우리 기관 매칭), 최근 세션, 미처리 후속조치 요약.
import {
  $, api, badge, confirmDialog, ctx, debounce, emptyState, errorEl, fmt, h, icon, isMissingRoute, isOperator, ITEM_KINDS,
  SOURCE_LABELS, statusBadge, toast, setKids, todayKST,
} from './common.js';

const elLive = $('#live-sessions');
const elAsm = $('#assembly');
const elAsmAt = $('#asm-fetched');
const elSched = $('#org-schedule');
const elSchedAt = $('#orgsched-at');
const elRecent = $('#recent');
const elItems = $('#open-items');
const elFilter = $('#recent-filter');

let sessions = [];

const itemTotal = (s) => Object.values(s.stats?.items || {}).reduce((a, b) => a + b, 0);

/* ── 세션 ─────────────────────────────────────────────────── */
async function loadSessions() {
  try {
    const r = await api('sessions');
    sessions = r.items || [];
    renderLive();
    renderRecent();
  } catch (e) {
    setKids(elLive, errorEl(e));
    setKids(elRecent, h('tr', null, h('td', { colspan: 9 }, errorEl(e))));
  }
}

function renderLive() {
  const live = sessions.filter((s) => s.status === 'live' || s.status === 'paused');
  if (!live.length) {
    setKids(elLive, h('div', { class: 'card', style: { gridColumn: '1 / -1' } },
      emptyState('진행 중인 세션이 없습니다', isOperator()
        ? '아래 ‘오늘 국회 중계’에서 우리 기관 회의를 골라 수집 세션을 만드세요.'
        : '운영자가 수집을 시작하면 여기에 표시됩니다.', 'live')));
    return;
  }
  setKids(elLive, ...live.map((s) => {
    const st = s.stats || {};
    return h('a', { class: 'card card-link live-card', href: `/s/${encodeURIComponent(s.id)}` },
      h('div', { class: 'row' }, s.recording_replay && s.status === 'live' ? badge('저장 자료 재생 중', 'b-blue') : statusBadge(s.status), s.committee ? badge(s.committee, 'b-blue') : null,
        st.alerts_unack ? badge(`언급 ${st.alerts_unack}`, 'b-red', '확인하지 않은 우리 기관 언급') : null),
      h('div', { class: 't' }, s.title),
      h('div', { class: 'm' },
        s.agencies ? h('span', null, s.agencies) : null,
        h('span', null, `발언 ${fmt.num(st.utterances)}`),
        h('span', null, `질의 ${fmt.num(st.question_blocks)}`),
        st.needs_name ? h('span', { style: { color: 'var(--warn-text)' } }, `성명 확인 필요 ${st.needs_name}`) : null,
        h('span', null, s.source_label || SOURCE_LABELS[s.source_type] || s.source_type),
        s.started_at ? h('span', null, `${fmt.hm(s.started_at)} 시작`) : null));
  }));
}

function renderRecent() {
  const q = (elFilter.value || '').trim().toLowerCase();
  const rows = sessions.filter((s) => !q || `${s.title} ${s.committee} ${s.agencies}`.toLowerCase().includes(q));
  if (!rows.length) {
    setKids(elRecent, h('tr', null, h('td', { colspan: 9 },
      emptyState(q ? '조건에 맞는 세션이 없습니다' : '아직 만든 세션이 없습니다',
        q ? '' : '새 수집 세션을 만들면 기록이 여기에 쌓입니다.', 'list'))));
    return;
  }
  setKids(elRecent, ...rows.map((s) => {
    const st = s.stats || {};
    const del = !ctx.cloud && isOperator() && s.status !== 'live'
      ? h('button', {
        class: 'btn btn-ghost btn-icon btn-xs', type: 'button', title: '세션 삭제', 'aria-label': `${s.title} 삭제`,
        onclick: () => removeSession(s),
      }, icon('trash', 'ic-sm')) : null;
    return h('tr', null,
      h('td', { class: 'wrap' },
        h('a', { href: `/s/${encodeURIComponent(s.id)}`, style: { fontWeight: 650 } }, s.title),
        h('div', { class: 'small muted' }, [s.committee, s.agencies].filter(Boolean).join(' · '))),
      h('td', { class: 'nowrap' }, s.meeting_date || fmt.ymd(s.created_at)),
      h('td', null, s.recording_replay && s.status === 'live' ? badge('저장 자료 재생 중', 'b-blue') : statusBadge(s.status)),
      h('td', { class: 'nowrap small' }, s.source_label || SOURCE_LABELS[s.source_type] || s.source_type),
      h('td', { class: 'num' }, fmt.num(st.utterances)),
      h('td', { class: 'num' }, fmt.num(st.question_blocks)),
      h('td', { class: 'num' }, fmt.num(itemTotal(s))),
      h('td', { class: 'num' }, st.alerts_unack ? h('span', { style: { color: 'var(--live)', fontWeight: 700 } }, `${st.alerts_unack}/${st.alerts}`) : fmt.num(st.alerts)),
      h('td', { class: 'nowrap' },
        h('a', { class: 'btn btn-ghost btn-icon btn-xs', href: `/s/${encodeURIComponent(s.id)}/report`, title: '상황보고' }, icon('report', 'ic-sm'), h('span', { class: 'sr-only' }, '상황보고')),
        del));
  }));
}

async function removeSession(s) {
  if (ctx.cloud) return;
  const ok = await confirmDialog({
    title: '세션 삭제',
    message: `‘${s.title}’ 세션과 발언·요약·후속조치 기록을 모두 삭제합니다. 되돌릴 수 없습니다.`,
    ok: '삭제', danger: true,
  });
  if (!ok) return;
  try {
    await api(`sessions/${encodeURIComponent(s.id)}`, { method: 'DELETE' });
    toast('세션을 삭제했습니다.', { type: 'ok' });
    loadSessions();
  } catch (e) {
    toast(e.message, { type: 'err' });
  }
}

/* ── 사전 점검 경고 띠(문제가 있을 때만) ───────────────────── */
const DESKTOP_CONNECTION_INTERVAL = 20000;
let desktopLocal = false, preflightCache = null, preflightFlight = null, lastDesktopConnectionAt = -Infinity;
async function loadPreflight(connectionOnly = false) {
  const el = $('#preflight-band');
  if (!el) return;
  if (preflightFlight) return preflightFlight;
  const startedAt = performance.now();
  if (connectionOnly && (!desktopLocal || ctx.cloud || document.hidden
      || startedAt - lastDesktopConnectionAt < DESKTOP_CONNECTION_INTERVAL)) return;
  if (connectionOnly) lastDesktopConnectionAt = startedAt;
  const task = (async () => {
  let r;
  try {
    if (connectionOnly) {
      const current = await api('desktop/connection-status', { signal: AbortSignal.timeout(5000) });
      const check = current?.check;
      if (!check || check.key !== 'llm' || !['ok', 'warn', 'error'].includes(check.level)) return;
      const checks = (preflightCache?.checks || []).filter(value => value.key !== 'llm');
      checks.push(check);
      r = { ...preflightCache, checks, ok: !checks.some(value => value.level === 'error') };
    } else {
      r = await api('preflight');
      // ctx에는 설치판 플래그가 없다. 서버가 확인한 명시적 boolean만 사용한다.
      desktopLocal = !ctx.cloud && r?.desktop_local === true;
      if (desktopLocal) lastDesktopConnectionAt = startedAt;
    }
    preflightCache = r;
  } catch (e) {
    if (connectionOnly) return; // 현재 점검 결과를 유지하고 다음 유한 주기에서 다시 확인한다.
    if (!isMissingRoute(e)) console.warn('사전 점검 실패', e); // 구버전 서버(404/405)·일시 오류는 띠를 띄우지 않는다
    el.hidden = true;
    return;
  }
  const checks = Array.isArray(r && r.checks) ? r.checks : [];
  const lv = (c) => c.level || (c.ok ? 'ok' : 'error');
  const bad = checks.filter((c) => lv(c) !== 'ok');
  if (!bad.length && r.ok !== false) { el.hidden = true; el.replaceChildren(); return; }
  const isErr = bad.some((c) => lv(c) === 'error') || (!bad.length && r.ok === false);
  el.className = `pf-band ${isErr ? 'err' : 'warn'}`;
  setKids(el, icon('alert'),
    h('div', { class: 'grow' },
      h('b', null, isErr ? '국감 전 점검: 해결이 필요한 문제가 있습니다' : '국감 전 점검: 확인할 항목이 있습니다'),
      h('ul', null, bad.map((c) => h('li', null, h('b', null, c.label || c.key || ''), c.detail ? ` — ${c.detail}` : '')))),
    isOperator() ? h('a', { class: 'btn btn-sm', href: '/admin#sec-health' }, '설정 > 시스템 상태') : null);
  el.hidden = false;
  })();
  preflightFlight = task;
  try { return await task; }
  finally { if (preflightFlight === task) preflightFlight = null; }
}

/* ── 오늘 국회 중계 ───────────────────────────────────────── */
let asmDisabled = false; // 서버가 자동 조회를 끈 경우(응답 disabled:true): 주기 갱신을 멈추고 [다시 불러오기]로만 조회
async function loadAssembly(manual = false) {
  try {
    const r = await api('assembly/live', { query: manual ? { refresh: 1 } : undefined });
    asmDisabled = !!(r && r.disabled);
    renderAssembly(r);
  } catch (e) {
    setKids(elAsm, errorEl(e));
  }
}

function assemblyPlayer(it) {
  const q = new URLSearchParams({ xcode: it.xcode || '', xcgcd: it.xcgcd || '' });
  return `https://assembly.webcast.go.kr/main/player.asp?${q}`;
}

function renderAssembly(r) {
  elAsmAt.textContent = r.fetched_at ? `${fmt.hm(r.fetched_at)} 기준` : '';
  const all = [...(r.items || [])].sort((a, b) => Number(!!b.org_match) - Number(!!a.org_match));
  // 오늘 중계 일정이 없는 위원회(xcgcd 없음)는 접어서 한 줄로만 보여 준다
  const items = all.filter((it) => it.xcgcd);
  const idle = all.filter((it) => !it.xcgcd);
  const nodes = [];
  if (r.error) nodes.push(h('div', { class: 'note warn', style: { margin: '0 0 8px' } }, `국회 의사중계 정보를 가져오지 못했습니다: ${r.error}`));
  if (r.disabled && !items.length) {
    setKids(elAsm, emptyState('자동 조회가 꺼져 있습니다', '오른쪽 위 [다시 불러오기]를 누르면 국회 중계 목록을 조회합니다.', 'video'));
    return;
  }
  if (!items.length) {
    nodes.push(emptyState(r.error ? '중계 목록을 표시할 수 없습니다' : '지금 중계 중인 회의가 없습니다',
      r.error ? '외부망 접속이 막혀 있으면 세션 생성 화면에서 회의 코드를 직접 입력하세요.' : '국회 의사중계 일정이 시작되면 자동으로 표시됩니다.', 'video'));
    setKids(elAsm, ...nodes);
    return;
  }
  for (const it of items) {
    const existing = sessions.find((s) => (s.status === 'live' || s.status === 'paused')
      && s.source_config && String(s.source_config.xcode || '') === String(it.xcode || '')
      && (!it.xcgcd || String(s.source_config.xcgcd || '') === String(it.xcgcd)));
    const params = new URLSearchParams({
      source: 'assembly_caption', xcode: it.xcode || '', xcgcd: it.xcgcd || '',
      title: it.xsubj || it.xname || '', committee: it.xname || '',
    });
    const stateText = it.xdesc || (/^\d+$/.test(String(it.xstat || '')) ? '' : it.xstat) || '중계';
    nodes.push(h('div', { class: `assembly-row${it.org_match ? ' org' : ''}` },
      h('div', null, badge(stateText, /정회|종료|중지|산회/.test(stateText) ? 'b-gray' : 'b-red')),
      h('div', { style: { minWidth: 0 } },
        h('div', { class: 'row' }, h('span', { class: 'an' }, it.xname || '회의'),
          it.org_match ? badge('우리 기관', 'b-solid-blue', '피감기관·감시 키워드와 일치') : null),
        h('div', { class: 'ad' }, it.xsubj || '')),
      h('div', { class: 'row', style: { justifyContent: 'flex-end' } },
        h('a', { class: 'btn btn-ghost btn-sm', href: assemblyPlayer(it), target: '_blank', rel: 'noopener', title: '국회 의사중계 플레이어(새 창)' }, icon('external', 'ic-sm'), '중계'),
        existing
          ? h('a', { class: 'btn btn-soft btn-sm', href: `/s/${encodeURIComponent(existing.id)}` }, '수집 중 세션 열기')
          : (isOperator() ? h('a', { class: `btn ${it.org_match ? 'btn-primary' : 'btn'} btn-sm`, href: `/sessions/new?${params}` }, icon('plus', 'ic-sm'), '이 회의 수집 세션 만들기') : null))));
  }
  if (idle.length) {
    nodes.push(h('details', { class: 'asm-idle' },
      h('summary', null, `오늘 중계 없는 위원회 ${idle.length}곳`),
      h('div', { class: 'small muted' }, idle.map((it) => it.xname).filter(Boolean).join(' · '))));
  }
  setKids(elAsm, ...nodes);
}

/* ── 우리 기관 국감 일정 ─────────────────────────────────── */
const WEEK = ['일', '월', '화', '수', '목', '금', '토'];
let schedTimer = null;

function cleanSubj(s) { return String(s || '').replace(/\s*\[\d{1,2}:\d{2}\]\s*$/, '').trim(); }

async function loadOrgSchedule(refresh = false) {
  clearTimeout(schedTimer);
  try {
    const r = await api('assembly/org-schedule', { query: refresh ? { refresh: 1 } : undefined });
    renderOrgSchedule(r);
    if (r.pending) schedTimer = setTimeout(() => loadOrgSchedule(false), 5000);
  } catch (e) {
    setKids(elSched, errorEl(e));
  }
}

function renderOrgSchedule(r) {
  elSchedAt.textContent = r.pending ? '조회 중…' : (r.fetched_at ? `${fmt.hm(r.fetched_at)} 기준` : '');
  if (r.configured === false) {
    setKids(elSched, emptyState('우리 기관이 설정되지 않았습니다',
      isOperator() ? '설정 > 우리 기관에 기관명과 별칭을 입력하면 이번 달 국감 일정을 자동으로 찾습니다.' : '운영자에게 기관 설정을 요청하세요.', 'calendar'));
    return;
  }
  const items = r.items || [];
  const nodes = [];
  if (r.error) nodes.push(h('div', { class: 'note warn', style: { margin: '0 0 8px' } }, r.error));
  if (!items.length) {
    nodes.push(r.pending ? h('div', { class: 'loading', role: 'status' }, '국회 일정·피감기관 목록을 조회하는 중… (처음엔 30초~1분)')
      : emptyState('이번 달 남은 일정에서 찾지 못했습니다', `찾은 이름: ${(r.orgs || []).join(', ')}`, 'calendar'));
    setKids(elSched, ...nodes);
    return;
  }
  const today = todayKST();
  for (const it of items) {
    // 달력 날짜의 요일(시간대 영향 없도록 UTC 자정으로 계산)
    const dayLabel = `${it.date.slice(5).replace('-', '.')}(${WEEK[new Date(`${it.date}T00:00:00Z`).getUTCDay()]})`;
    const isToday = it.date === today;
    const existing = sessions.find((s) => s.source_config && String(s.source_config.xcgcd || '') === String(it.xcgcd || '') && s.status !== 'ended');
    const params = new URLSearchParams({
      source: 'assembly_caption', xcode: it.xcode || '', xcgcd: it.xcgcd || '',
      title: cleanSubj(it.xsubj) || it.xname || '', committee: it.xname || '',
      agencies: (it.agencies || []).join(', '), meeting_date: it.date || '',
    });
    nodes.push(h('div', { class: `sched-row${isToday ? ' today' : ''}` },
      h('div', { class: 'sd' }, h('b', null, dayLabel), h('span', null, it.time || ''), isToday ? badge('오늘', 'b-red') : null),
      h('div', { style: { minWidth: 0 } },
        h('div', { class: 'row' }, h('span', { class: 'an' }, it.xname || ''), h('span', { class: 'ad' }, cleanSubj(it.xsubj))),
        h('div', { class: 'small' }, '피감기관: ',
          ...(it.agencies || []).flatMap((a, i) => [i ? ', ' : '', (it.matched || []).includes(a) ? h('mark', null, a) : a]))),
      h('div', { class: 'row', style: { justifyContent: 'flex-end' } },
        existing ? h('a', { class: 'btn btn-soft btn-sm', href: `/s/${encodeURIComponent(existing.id)}` }, '세션 열기')
          : (isOperator() ? h('a', { class: 'btn btn-primary btn-sm', href: `/sessions/new?${params}` }, icon('plus', 'ic-sm'), '수집 세션 준비') : null))));
  }
  setKids(elSched, ...nodes);
}

$('#orgsched-refresh').addEventListener('click', () => { setKids(elSched, h('div', { class: 'loading', role: 'status' }, '다시 조회하는 중…')); loadOrgSchedule(true); });

/* ── 미처리 후속조치 ──────────────────────────────────────── */
async function loadItems() {
  try {
    const r = await api('items', { query: { status: 'open' } });
    renderItems(r.items || []);
  } catch (e) {
    setKids(elItems, errorEl(e));
  }
}

function renderItems(items) {
  if (!items.length) {
    setKids(elItems, emptyState('미처리 후속조치가 없습니다', '요구자료·약속사항이 추출되면 여기에 모입니다.', 'checklist'));
    return;
  }
  const byKind = {};
  const byDept = {};
  for (const it of items) {
    byKind[it.kind] = (byKind[it.kind] || 0) + 1;
    const d = it.assignee_dept || '미배정';
    byDept[d] = (byDept[d] || 0) + 1;
  }
  const kpi = h('div', { class: 'kpi' },
    h('div', { class: 'stat' }, h('b', null, fmt.num(items.length)), h('span', null, '미처리 전체')),
    Object.entries(ITEM_KINDS).filter(([k]) => byKind[k]).map(([k, v]) => h('a', { class: 'stat', href: `/items?status=open&kind=${k}`, style: { color: 'inherit' } },
      h('b', null, fmt.num(byKind[k])), h('span', null, `${v.icon} ${v.label}`))));
  const depts = Object.entries(byDept).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const deptEl = h('div', { class: 'chips', style: { marginTop: '12px' } },
    depts.map(([d, n]) => h('a', { class: 'chip', href: d === '미배정' ? '/items?status=open' : `/items?status=open&dept=${encodeURIComponent(d)}` }, `${d} ${n}`)));
  const latest = [...items].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0)).slice(0, 6);
  const list = h('ul', { class: 'list-plain', style: { marginTop: '12px' } }, latest.map((it) => h('li', null,
    h('a', { href: it.utterance_id ? `/s/${encodeURIComponent(it.session_id)}#u-${it.utterance_id}` : `/s/${encodeURIComponent(it.session_id)}` },
      h('div', { class: 'row' }, h('span', { class: `chip k-${it.kind}` }, `${ITEM_KINDS[it.kind]?.icon || ''} ${it.kind_label || ''}`),
        it.strength ? badge(it.strength, 'b-outline badge-sm') : null,
        h('span', { class: 'small muted ellipsis grow' }, it.session_title || '')),
      h('div', { class: 'clamp-2', style: { marginTop: '4px' } }, it.text)))));
  setKids(elItems, kpi, deptEl, list);
}

/* ── 시작 ─────────────────────────────────────────────────── */
elFilter.addEventListener('input', debounce(renderRecent, 120));
$('#asm-refresh').addEventListener('click', () => { setKids(elAsm, h('div', { class: 'loading', role: 'status' }, '불러오는 중…')); loadAssembly(true); });

async function boot() {
  loadPreflight();
  await loadSessions();
  loadAssembly();
  loadOrgSchedule();
  loadItems();
}
boot();

let ticks = 0;
setInterval(() => {
  if (document.hidden) return;
  ticks += 1;
  loadSessions();
  if (ticks % 3 === 0) { if (!asmDisabled) loadAssembly(); loadItems(); }
  if (ticks % 15 === 0) loadPreflight(); // 전체 점검의 기존 5분 주기는 유지한다.
  else if (desktopLocal) loadPreflight(true); // 설치판 계정 상태만 확인하며 외부 점검은 실행하지 않는다.
}, 20000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { loadSessions(); loadPreflight(true); } });
window.addEventListener('focus', () => { if (!document.hidden) loadPreflight(true); });
