// 세션 생성: 수집원 카드 선택, 수집원별 설정, 국회 회의 선택 → 명단 자동 채움, 만들고 바로 시작.
import { $, $$, api, confirmDialog, ctx, errMsg, h, isOperator, queryParams, setBusy, toast, todayKST, setKids,
  UPLOAD_ACCEPT, uploadExtOk,
} from './common.js';

const form = $('#new-form');
const elErr = $('#form-error');
const asmSel = $('#asm-select');
const rosterTa = $('#roster_text');
let asmItems = [];
let autoTitle = '';          // 회의 선택으로 자동 채운 제목(사용자가 고치지 않았으면 갱신)
let autoCommittee = '';      // 회의 선택으로 자동 채운 위원회(제목과 같은 방식)
let rosterFromAssembly = ''; // 국회 명단으로 채운 내용
let submitter = 'create';
// 이미 만든 세션(파일 업로드·시작이 실패한 뒤 다시 누르면 새로 만들지 않고 이 세션을 고쳐 이어 간다)
let createdSid = null;
let createdRoster = null;

const source = () => (form.querySelector('input[name="source_type"]:checked') || {}).value || 'assembly_caption';
const isAssembly = (s = source()) => s === 'assembly_caption' || s === 'assembly_hls';
const cloudSources = ['assembly_caption', 'assembly_hls', 'manual'];

/* ── 수집원별 영역 표시 ─────────────────────────────────── */
function applySource() {
  const s = source();
  for (const el of $$('[data-for]')) {
    const keys = el.getAttribute('data-for').split(/\s+/);
    el.hidden = !keys.includes(s) || (ctx.cloud && !cloudSources.includes(s));
  }
  if (isAssembly(s) && !asmItems.length) loadAssembly();
}

/* ── 국회 회의 목록 ─────────────────────────────────────── */
async function loadAssembly() {
  setKids(asmSel, h('option', { value: '' }, '불러오는 중…'));
  try {
    const r = await api('assembly/live');
    asmItems = [...(r.items || [])].sort((a, b) => Number(!!b.org_match) - Number(!!a.org_match));
    const opts = [h('option', { value: '' }, asmItems.length ? '— 회의를 고르세요 —' : '지금 중계 중인 회의가 없습니다')];
    asmItems.forEach((it, i) => {
      opts.push(h('option', { value: String(i) }, `${it.org_match ? '★ ' : ''}${it.xname || ''} — ${it.xsubj || ''}${it.xdesc ? ` (${it.xdesc})` : ''}`));
    });
    setKids(asmSel, ...opts);
    $('#asm-hint').textContent = r.error
      ? `국회 의사중계 정보를 가져오지 못했습니다(${r.error}). 회의 코드를 직접 입력하세요.`
      : '회의를 고르면 회의 코드·제목·위원 명단이 자동으로 채워집니다. ★ 는 우리 기관 관련 회의입니다.';
    const q = queryParams();
    if (q.xcode) {
      const idx = asmItems.findIndex((it) => String(it.xcode) === q.xcode && (!q.xcgcd || String(it.xcgcd) === q.xcgcd));
      if (idx >= 0) asmSel.value = String(idx);
    }
  } catch (e) {
    setKids(asmSel, h('option', { value: '' }, '목록을 불러오지 못했습니다'));
    $('#asm-hint').textContent = `${errMsg(e)} 회의 코드를 직접 입력하세요.`;
  }
}

function onPickAssembly() {
  const it = asmItems[Number(asmSel.value)];
  if (!it || asmSel.value === '') return;
  $('#xcode').value = it.xcode || '';
  $('#xcgcd').value = it.xcgcd || '';
  const t = $('#title');
  const newTitle = String(it.xsubj || it.xname || '').replace(/\s*\[\d{1,2}:\d{2}\]\s*$/, '').trim();
  if (!t.value || t.value === autoTitle) { t.value = newTitle; autoTitle = newTitle; }
  // 위원회도 사용자가 직접 고치지 않았으면 새로 고른 회의 값으로 바꾼다(다시 고르면 첫 선택 값이 남던 문제)
  const c = $('#committee');
  if (it.xname && (!c.value || c.value === autoCommittee)) { c.value = it.xname; autoCommittee = it.xname; }
  loadRoster(true);
  loadAgencies();
}

let autoAgencies = '';
async function loadAgencies() {
  const xcode = $('#xcode').value.trim();
  const xcgcd = $('#xcgcd').value.trim();
  const el = $('#agencies');
  if (!xcode || !xcgcd || (el.value && el.value !== autoAgencies && !el.dataset.auto)) return;
  try {
    const r = await api('assembly/agencies', { query: { xcode, xcgcd } });
    const list = (r.agencies || []).join(', ');
    if (list) { el.value = list; autoAgencies = list; el.dataset.auto = '1'; }
  } catch { /* 국회 연동이 막혀 있으면 직접 입력 */ }
}

/* ── 명단 ───────────────────────────────────────────────── */
function membersToText(members) {
  return members.map((m) => {
    const parts = [m.name];
    if (m.role === 'chair') parts.push('위원장');
    if (m.party) parts.push(m.party);
    return parts.join(' ');
  }).join('\n');
}

async function loadRoster(auto = false) {
  const xcode = $('#xcode').value.trim();
  if (!xcode) { if (!auto) toast('먼저 회의를 고르거나 회의 코드를 입력하세요.', { type: 'warn' }); return; }
  const cur = rosterTa.value.trim();
  if (cur && cur !== rosterFromAssembly.trim()) {
    if (auto) return; // 사용자가 직접 쓴 명단은 자동으로 덮어쓰지 않는다
    const ok = await confirmDialog({ title: '명단 덮어쓰기', message: '지금 입력한 명단을 국회 명단으로 바꿀까요?', ok: '바꾸기' });
    if (!ok) return;
  }
  const btn = $('#roster-load');
  setBusy(btn, true);
  try {
    const r = await api('assembly/roster', { query: { xcode } });
    const members = r.members || [];
    if (!members.length) { if (!auto) toast('국회 명단을 찾지 못했습니다. 직접 입력하세요.', { type: 'warn' }); return; }
    rosterFromAssembly = membersToText(members);
    rosterTa.value = rosterFromAssembly;
    updateRosterCount();
    toast(`위원 명단 ${members.length}명을 채웠습니다.`, { type: 'ok', timeout: 2500 });
  } catch (e) {
    if (!auto) toast(errMsg(e), { type: 'err' });
  } finally {
    setBusy(btn, false);
  }
}

function updateRosterCount() {
  const lines = rosterTa.value.split('\n').map((l) => l.trim()).filter(Boolean);
  const chair = lines.filter((l) => /위원장/.test(l)).length;
  $('#roster-count').textContent = lines.length
    ? `명단 ${lines.length}명${chair ? ` (위원장 ${chair}명 포함)` : ' — 위원장을 표시하면 진행 발언 구분이 정확해집니다'}`
    : '명단이 있으면 위원장 호명·자기소개로 질의 위원을 더 정확히 찾습니다. 나중에 세션 화면에서도 고칠 수 있습니다.';
}

/* ── 제출 ───────────────────────────────────────────────── */
function showError(msg) {
  elErr.textContent = msg;
  elErr.hidden = !msg;
  if (msg) elErr.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

function buildPayload() {
  const s = source();
  const v = (id) => ($(`#${id}`)?.value || '').trim();
  const cfg = {};
  if (isAssembly(s)) {
    if (v('xcode')) cfg.xcode = v('xcode');
    if (v('xcgcd')) cfg.xcgcd = v('xcgcd');
    if (!ctx.cloud && s === 'assembly_caption' && v('xsami')) cfg.xsami = v('xsami');
    if (!ctx.cloud && s === 'assembly_hls' && v('hls_url')) cfg.hls_url = v('hls_url');
  } else if (!ctx.cloud && s === 'replay') {
    if (v('fixture')) cfg.fixture = v('fixture');
    cfg.speed = Number(v('speed')) || 20;
    cfg.with_hints = $('#with_hints').checked;
  } else if (!ctx.cloud && s === 'caption_replay') {
    cfg.caption_replay = true;
    cfg.caption_replay_speed = Number(v('caption-replay-speed')) || 1;
  } else if (!ctx.cloud && s === 'file') {
    const bt = v('base_time');
    if (bt) {
      const ms = Date.parse(`${bt.length === 16 ? `${bt}:00` : bt}+09:00`);
      if (!Number.isNaN(ms)) cfg.base_time_ms = ms;
    }
  }
  return {
    title: v('title'),
    committee: v('committee'),
    agencies: v('agencies'),
    meeting_date: v('meeting_date'),
    source_type: s === 'caption_replay' ? 'assembly_caption' : s,
    source_config: cfg,
    save_audio: ['assembly_hls', 'browser', 'file'].includes(s) && !!$('#save_audio')?.checked,
    roster_text: rosterTa.value.trim(),
    notes: v('notes'),
  };
}

function validate(p) {
  if (ctx.cloud && !cloudSources.includes(p.source_type)) return ['이 수집 방법은 수집 PC 화면에서 고르세요.', 'title'];
  if (!p.title) return ['회의 제목을 입력하세요.', 'title'];
  if (p.source_type === 'assembly_caption' && !p.source_config.caption_replay && !p.source_config.xcode) return ['국회 회의를 고르거나 회의 코드(xcode)를 입력하세요.', 'xcode'];
  if (p.source_config.caption_replay) {
    const file = $('#caption-replay-input')?.files?.[0];
    if (!file || !file.name.toLowerCase().endsWith('.jsonl')) return ['보관한 국회 원수신 JSONL 파일을 고르세요.', 'caption-replay-input'];
    if (file.size > 16 * 1024 * 1024) return ['저장 자막 파일은 최대 16MB입니다.', 'caption-replay-input'];
    if (p.source_config.caption_replay_speed < .5 || p.source_config.caption_replay_speed > 10) return ['재생 배속은 0.5~10 사이여야 합니다.', 'caption-replay-speed'];
  }
  if (p.source_type === 'assembly_hls' && !p.source_config.xcode && !p.source_config.hls_url) return ['회의 코드 또는 HLS 주소를 입력하세요.', 'xcode'];
  if (p.source_type === 'file' && !$('#file-input').files.length) return ['올릴 녹음 파일을 고르세요.', 'file-input'];
  if (p.source_type === 'file' && !uploadExtOk($('#file-input').files[0].name)) {
    return [`올릴 수 없는 파일 형식입니다. 허용: ${UPLOAD_ACCEPT.replaceAll(',', ', ')}`, 'file-input'];
  }
  return null;
}

// 만들어 둔 세션을 다시 시도할 때는 클라우드에서 바꿀 수 있는 회의 정보만 보낸다.
function editingSessionPayload(p) {
  const { start: _s, roster_text: _r, ...fields } = p;
  if (!ctx.cloud) return fields;
  return Object.fromEntries(['title', 'committee', 'agencies', 'meeting_date', 'notes'].map((k) => [k, p[k]]));
}

function uploadFile(sid, file, onProgress, captionReplay = false) {
  // 진행률 표시를 위해 XHR 사용(fetch 는 업로드 진행률을 주지 않는다)
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/sessions/${encodeURIComponent(sid)}/${captionReplay ? 'caption-replay-upload' : 'upload'}`);
    xhr.setRequestHeader('X-Requested-With', 'gukgam');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* 무시 */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error((data && data.detail) || `업로드에 실패했습니다(${xhr.status}).`));
    };
    xhr.onerror = () => reject(new Error('업로드 중 연결이 끊겼습니다.'));
    const fd = new FormData();
    fd.append('file', file);
    xhr.send(fd);
  });
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  submitter = (e.submitter && e.submitter.value) || submitter;
  showError('');
  const p = buildPayload();
  const bad = validate(p);
  if (bad) { showError(bad[0]); $(`#${bad[1]}`)?.focus(); return; }
  const captionReplay = !!p.source_config.caption_replay;
  const isFile = p.source_type === 'file' || captionReplay;
  const wantStart = submitter === 'start';
  p.start = wantStart && !isFile; // 파일은 올린 뒤에 시작
  const btns = [$('#btn-create'), $('#btn-create-start')];
  btns.forEach((b) => setBusy(b, true));
  try {
    let sid = createdSid;
    if (sid) {
      // 앞서 만든 세션을 다시 쓴다: 고친 회의 정보·수집원 설정·명단을 반영한다(중복 세션 방지)
      const rosterText = p.roster_text;
      const fields = editingSessionPayload(p);
      await api(`sessions/${encodeURIComponent(sid)}`, { method: 'PATCH', body: fields });
      if (rosterText !== createdRoster) {
        await api(`sessions/${encodeURIComponent(sid)}/roster`, { method: 'PUT', body: { text: rosterText } });
        createdRoster = rosterText;
      }
      if (!isFile && wantStart) await api(`sessions/${encodeURIComponent(sid)}/start`, { method: 'POST' });
    } else {
      const s = await api('sessions', { method: 'POST', body: p });
      sid = s.id;
      createdSid = sid;
      createdRoster = p.roster_text;
    }
    if (isFile) {
      const f = $(captionReplay ? '#caption-replay-input' : '#file-input').files[0];
      const t = toast(`파일을 올리는 중… 0%`, { timeout: 0 });
      try {
        await uploadFile(sid, f, (r) => { t.el && (t.el.querySelector('.t-body > div:last-child').textContent = `파일을 올리는 중… ${Math.round(r * 100)}%`); }, captionReplay);
      } finally { t.close(); }
      if (wantStart) await api(`sessions/${encodeURIComponent(sid)}/start`, { method: 'POST' });
    }
    location.href = `/s/${encodeURIComponent(sid)}`;
  } catch (err) {
    showError(errMsg(err));
    if (createdSid) {
      // 세션은 이미 만들어졌다: 다시 누르면 같은 세션으로 이어 가고, 세션 화면에서 파일을 올릴 수도 있다
      elErr.append(h('div', { class: 'small', style: { marginTop: '6px' } },
        '세션은 만들어 두었습니다. 다시 누르면 같은 세션으로 이어서 진행합니다. ',
        h('a', { href: `/s/${encodeURIComponent(createdSid)}` }, '세션 화면에서 계속하기')));
      $('#btn-create').textContent = '이어서 진행';
    }
    btns.forEach((b) => setBusy(b, false));
  }
});

/* ── 초기화 ─────────────────────────────────────────────── */
function prefill() {
  const q = queryParams();
  if (q.source && (!ctx.cloud || cloudSources.includes(q.source))) {
    const r = form.querySelector(`input[name="source_type"][value="${CSS.escape(q.source)}"]`);
    if (r) r.checked = true;
  } else if (q.xcode) {
    form.querySelector('input[value="assembly_caption"]').checked = true;
  }
  if (q.xcode) $('#xcode').value = q.xcode;
  if (q.xcgcd) $('#xcgcd').value = q.xcgcd;
  if (q.title) { const tt = q.title.replace(/\s*\[\d{1,2}:\d{2}\]\s*$/, '').trim(); $('#title').value = tt; autoTitle = tt; }
  if (q.committee) { $('#committee').value = q.committee; autoCommittee = q.committee; }
  if (q.agencies) $('#agencies').value = q.agencies;
  $('#meeting_date').value = q.meeting_date || todayKST();
}

async function prefillOrg() {
  if (!isOperator() || $('#agencies').value) return;
  try {
    const st = await api('settings');
    const name = st?.org?.name;
    if (name && !$('#agencies').value) { $('#agencies').value = name; $('#agencies').dataset.auto = '1'; } // 우리 기관을 기본값으로(고칠 수 있음)
  } catch { /* 설정을 못 읽어도 무방 */ }
}

form.addEventListener('change', (e) => { if (e.target.name === 'source_type') applySource(); });
asmSel.addEventListener('change', onPickAssembly);
$('#asm-reload').addEventListener('click', loadAssembly);
$('#roster-load').addEventListener('click', () => loadRoster(false));
rosterTa.addEventListener('input', updateRosterCount);
$('#title').addEventListener('input', () => { if ($('#title').value !== autoTitle) autoTitle = ''; });
$('#committee').addEventListener('input', () => { if ($('#committee').value !== autoCommittee) autoCommittee = ''; });
$('#file-input')?.setAttribute('accept', UPLOAD_ACCEPT); // 서버 허용 형식과 같게(템플릿 값과 함께 유지)
for (const b of [$('#btn-create'), $('#btn-create-start')]) b.addEventListener('click', () => { submitter = b.value; });

prefill();
applySource();
updateRosterCount();
$('#agencies').addEventListener('input', () => { delete $('#agencies').dataset.auto; });
prefillOrg();
if (queryParams().xcode) { loadRoster(true); if (!queryParams().agencies) loadAgencies(); }
