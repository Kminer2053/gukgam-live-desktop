// 국감 라이브 — 공용 모듈: API 호출, DOM 헬퍼, 형식 변환, 토스트·대화상자·메뉴, 라벨 상수.
// 모든 화면 스크립트(ES 모듈)가 import 한다. 외부 의존성 없음.

/* ── 페이지 컨텍스트(템플릿이 body data-* 로 넘김) ───────────────── */
export const ctx = (() => {
  const d = document.body ? document.body.dataset : {};
  return {
    role: d.role || 'viewer',
    name: d.name || '',
    auth: d.auth === '1',
    version: d.version || '',
    sid: d.sid || '',
    uid: d.uid ? Number(d.uid) : null,
    cloud: d.cloud === '1',
    offline: d.offline === '1',
  };
})();
export const isOperator = () => ctx.role === 'operator';

/* ── 라벨·상수(serialize.py / models.py 와 같은 값) ─────────────── */
export const SESSION_STATUS = {
  idle: { label: '대기', cls: 'b-idle' },
  live: { label: 'LIVE', cls: 'b-live' },
  paused: { label: '일시정지', cls: 'b-paused' },
  ended: { label: '종료', cls: 'b-ended' },
};
export const SOURCE_LABELS = {
  assembly_caption: '국회 AI자막 수신',
  assembly_hls: '국회 중계영상 음성인식',
  browser: '브라우저 마이크·탭 음성',
  file: '녹음 파일',
  replay: '리플레이(시연용)',
  manual: '수기 입력',
};
export const SOURCE_ICONS = {
  assembly_caption: 'caption', assembly_hls: 'video', browser: 'mic', file: 'file', replay: 'replay', manual: 'keyboard',
};
export const ROLE_LABELS = { chair: '위원장', member: '위원', answer: '답변', other: '기타', unknown: '미상', system: '기록' };
export const BLOCK_STATUS = {
  ai_inferred: { label: 'AI 추정', cls: 'b-blue' },
  needs_name: { label: '성명 확인 필요', cls: 'b-orange' },
  confirmed: { label: '확인됨', cls: 'b-green' },
};
export const ITEM_KINDS = {
  data_request: { label: '요구자료', short: '요구자료', icon: '📄' },
  commitment: { label: '약속·검토사항', short: '약속', icon: '✅' },
  issue: { label: '지적사항', short: '지적', icon: '⚠' },
  witness_request: { label: '증인·참고인 요구', short: '증인', icon: '👤' },
  written_question: { label: '서면질의', short: '서면', icon: '✉' },
};
export const STRENGTHS = {
  commitment: ['확약', '검토', '의례'],
  data_request: ['자료제출', '서면보고', '조사', '사실확인'],
};
export const ITEM_STATUS = { open: '미처리', in_progress: '처리중', done: '완료', dismissed: '제외' };
export const ITEM_ORIGIN = { rule: '규칙', llm: 'AI', manual: '수동' };
export const DUE_PRESETS = ['당일', '종합감사 전', '결과보고서 대응'];
export const PARTIES = ['더불어민주당', '국민의힘', '조국혁신당', '개혁신당', '진보당', '기본소득당', '사회민주당', '무소속'];
export const ORG_RELEVANCE = {
  high: { label: '기관 관련 높음', cls: 'b-red' },
  medium: { label: '기관 관련 보통', cls: 'b-orange' },
  low: { label: '기관 관련 낮음', cls: 'b-gray' },
};
export const PHASE_LABELS = {
  connecting: '연결 중', connected: '연결됨', receiving: '수신 중', reconnecting: '재연결 중',
  stopped: '중지', error: '오류', done: '완료',
};
export const NOTICE = 'AI 음성인식·요약 결과로 오류가 있을 수 있습니다 · 내부 업무용(국회법 제149조② 상업적 이용 금지)';
/** 서버가 받는 녹음 파일 확장자(routes_api.UPLOAD_EXTS 와 같은 값) */
export const UPLOAD_EXTS = ['wav', 'mp3', 'm4a', 'mp4', 'webm', 'ogg', 'aac', 'flac'];
export const UPLOAD_ACCEPT = UPLOAD_EXTS.map((e) => `.${e}`).join(',');
/** 파일 이름의 확장자가 서버 허용 목록에 있는지 */
export function uploadExtOk(name) {
  const m = /\.([0-9A-Za-z]+)$/.exec(String(name || '').trim());
  return !!m && UPLOAD_EXTS.includes(m[1].toLowerCase());
}
/** 블록 요약 편집용 선택지(llm/prompts.py 와 같은 값) */
export const RESPONSE_TYPES = ['사실확인 필요', '자료요구', '책임추궁', '제도개선 요구', '예산', '기타'];
export const URGENCY = ['당일', '종합감사 전', '결과보고서 대응'];
/** 기록 표지 프리셋(정회·속개 등) */
export const MARKER_PRESETS = ['정회', '속개', '중계 중단', '비공개 전환', '산회', '메모'];

/* ── DOM 헬퍼 ───────────────────────────────────────────────── */
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 요소 생성. 텍스트는 항상 textContent 로 넣는다(XSS 방지). */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  let value;
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class' || k === 'className') el.className = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'style' && ctx.cloud) {
        // 클라우드 CSP에 맞춰 문자열 속성 대신 CSS 속성을 하나씩 지정한다.
        for (const declaration of String(v).split(';')) {
          const colon = declaration.indexOf(':');
          if (colon > 0) el.style.setProperty(declaration.slice(0, colon).trim(), declaration.slice(colon + 1).trim());
        }
      }
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'text') el.textContent = v;
      else if (k === 'value' && (tag === 'input' || tag === 'textarea' || tag === 'select')) value = v;
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  appendAll(el, children);
  if (value !== undefined) el.value = value; // select 는 option 을 붙인 뒤에 값을 정한다
  return el;
}

export function appendAll(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false || c === '') continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

/** replaceChildren 의 null 안전판(null·false·'' 는 건너뛴다 — 그대로 넘기면 'null' 글자가 들어간다) */
export function setKids(el, ...kids) {
  el.replaceChildren();
  return appendAll(el, kids);
}

const SVGNS = 'http://www.w3.org/2000/svg';
/** 아이콘 스프라이트(/static/img/icons.svg) 참조 SVG */
export function icon(name, cls = '') {
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', `ic ${cls}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVGNS, 'use');
  use.setAttribute('href', ctx.offline ? `#${name}` : `/static/img/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

export function badge(text, cls = 'b-gray', title = '') {
  return h('span', { class: `badge ${cls}`, title: title || null }, text);
}

export function emptyState(title, desc = '', ic = 'info') {
  return h('div', { class: 'empty' }, icon(ic), h('b', null, title), desc ? h('span', null, desc) : null);
}

export function loadingEl(text = '불러오는 중…', small = false) {
  return h('div', { class: small ? 'loading loading-sm' : 'loading', role: 'status' }, text);
}

export function errorEl(err) {
  return h('div', { class: 'error-box', role: 'alert' }, errMsg(err));
}

export function setBusy(btn, busy) {
  if (!btn) return;
  btn.classList.toggle('busy', !!busy);
  btn.disabled = !!busy;
  btn.setAttribute('aria-busy', busy ? 'true' : 'false');
}

export function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/* ── API ────────────────────────────────────────────────────── */
export class ApiError extends Error {
  constructor(status, message, data = null) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

function detailMessage(data) {
  if (!data) return '';
  const d = data.detail ?? data.error ?? data.message;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    const msgs = d.map((x) => (typeof x === 'string' ? x : x?.msg)).filter(Boolean);
    return msgs.length ? `입력값을 확인하세요: ${msgs.join(', ')}` : '';
  }
  return '';
}

export function errMsg(err) {
  if (!err) return '알 수 없는 오류가 발생했습니다.';
  if (typeof err === 'string') return err;
  return err.message || String(err);
}

let lastAuthToast = 0;
function handleAuth(status) {
  const now = Date.now();
  if (now - lastAuthToast < 4000) return;
  lastAuthToast = now;
  const next = encodeURIComponent(location.pathname + location.search + location.hash);
  if (status === 401) {
    if (location.pathname.startsWith('/login')) return;
    toast('로그인이 필요합니다. 로그인 화면으로 이동합니다.', { type: 'warn', timeout: 2500 });
    setTimeout(() => { location.href = `/login?next=${next}`; }, 1200);
  } else {
    toast(ctx.auth ? '이 작업은 운영자 권한이 필요합니다.' : '권한이 없습니다.', {
      type: 'warn',
      title: '권한 없음',
      actions: ctx.auth ? [{ label: '운영자로 로그인', href: `/login?next=${next}` }] : [],
    });
  }
}

/**
 * JSON API 호출. 쓰기 요청에는 X-Requested-With: gukgam 과 Content-Type: application/json 을 붙인다.
 * path 가 '/' 로 시작하지 않으면 '/api/' 를 앞에 붙인다.
 */
const cloudRequests = new Map();

/** 실패·처리 중인 같은 입력은 같은 번호로 이어 받고 성공한 뒤에는 새 동작을 시작한다. */
function cloudRequestId(key) {
  const now = Date.now();
  for (const [stored, value] of cloudRequests) {
    if (now - value.at >= 3600000) cloudRequests.delete(stored);
  }
  if (!cloudRequests.has(key)) {
    if (cloudRequests.size >= 1000) cloudRequests.delete(cloudRequests.keys().next().value);
    cloudRequests.set(key, { id: crypto.randomUUID(), at: now });
  }
  return cloudRequests.get(key).id;
}

function cloudResponseMessage(status) {
  return ({
    202: '수집 PC가 처리 중입니다. 다시 누르지 말고 잠시 뒤 확인하세요.',
    413: '파일이나 요청이 너무 큽니다. 수집 PC 화면에서 처리하세요.',
    503: '수집 PC 연결을 확인하세요. 지금은 저장된 기록을 열람할 수 있습니다.',
    504: '수집 PC 응답이 늦습니다. 잠시 뒤 처리 결과를 확인하세요.',
  })[status] || '';
}

export async function api(path, { method = 'GET', body, query, signal, raw = false, headers = {} } = {}) {
  if (ctx.offline) return window.gukgamOffline.api(path, { method, body, query, signal, raw });
  let url = path.startsWith('/') ? path : `/api/${path}`;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue;
      qs.append(k, typeof v === 'boolean' ? (v ? '1' : '0') : String(v));
    }
    const s = qs.toString();
    if (s) url += (url.includes('?') ? '&' : '?') + s;
  }
  const m = method.toUpperCase();
  const hdr = { Accept: 'application/json', ...headers };
  let payload;
  let requestKey = '';
  if (m !== 'GET' && m !== 'HEAD') {
    hdr['X-Requested-With'] = 'gukgam';
    if (body instanceof FormData) {
      payload = body; // multipart 경계는 브라우저가 정한다
    } else {
      hdr['Content-Type'] = 'application/json';
      payload = JSON.stringify(body === undefined ? {} : body);
    }
    if (ctx.cloud) {
      requestKey = `${m}\n${url}\n${typeof payload === 'string' ? payload : ''}`;
      if (!Object.keys(hdr).some((key) => key.toLowerCase() === 'x-gukgam-request-id')) {
        hdr['X-Gukgam-Request-Id'] = cloudRequestId(requestKey);
      }
    }
  }
  let res;
  try {
    res = await fetch(url, { method: m, headers: hdr, body: payload, signal, credentials: 'same-origin' });
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;
    throw new ApiError(0, '서버에 연결할 수 없습니다. 네트워크 상태를 확인하세요.');
  }
  if (ctx.cloud && [202, 413, 503, 504].includes(res.status)) {
    let data = null;
    try { data = await res.json(); } catch { /* 오류 안내만 표시한다 */ }
    const message = detailMessage(data) || cloudResponseMessage(res.status);
    toast(message, { type: 'warn' });
    // 202를 저장 성공으로 처리하지 않아 입력창과 같은 요청 번호를 유지한다.
    throw new ApiError(res.status, message, data);
  }
  if (requestKey && (res.ok || res.status >= 400 && res.status < 500)) cloudRequests.delete(requestKey);
  if (res.status === 401 || res.status === 403) {
    handleAuth(res.status);
    let data = null;
    try { data = await res.json(); } catch { /* 무시 */ }
    throw new ApiError(res.status, detailMessage(data) || (res.status === 401 ? '로그인이 필요합니다.' : '권한이 없습니다.'), data);
  }
  if (raw) {
    if (!res.ok) throw new ApiError(res.status, `요청을 처리하지 못했습니다(${res.status}).`);
    return res;
  }
  const ct = res.headers.get('content-type') || '';
  let data = null;
  if (res.status !== 204) {
    if (ct.includes('json')) data = await res.json().catch(() => null);
    else {
      const t = await res.text().catch(() => '');
      data = t ? { detail: t.slice(0, 300) } : null;
    }
  }
  if (!res.ok) {
    const fallback = res.status === 404 ? '대상을 찾을 수 없습니다.'
      : res.status === 409 ? '현재 상태에서는 처리할 수 없습니다.'
        : res.status >= 500 ? `서버 오류가 발생했습니다(${res.status}).` : `요청을 처리하지 못했습니다(${res.status}).`;
    throw new ApiError(res.status, detailMessage(data) || fallback, data);
  }
  return data;
}

/**
 * 서버에 아직 없는 API(구버전 서버)인지: 405, 또는 라우터 기본 404('Not Found').
 * 대상이 없어서 난 404(한국어 detail)와 구분해, 새 기능 버튼을 조용히 숨길 때 쓴다.
 */
export function isMissingRoute(err) {
  if (!err || typeof err !== 'object') return false;
  if (err.status === 405) return true;
  if (err.status !== 404) return false;
  const d = err.data && err.data.detail;
  return d == null || d === 'Not Found';
}

/* ── 시각·기간 형식(KST) ─────────────────────────────────────── */
const KST_FMT = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
const KST_WD = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', weekday: 'short' });

function kparts(ms) {
  const o = {};
  for (const p of KST_FMT.formatToParts(new Date(ms))) o[p.type] = p.value;
  if (o.hour === '24') o.hour = '00';
  return o;
}

export const fmt = {
  hms(ms) { if (!ms) return ''; const p = kparts(ms); return `${p.hour}:${p.minute}:${p.second}`; },
  hm(ms) { if (!ms) return ''; const p = kparts(ms); return `${p.hour}:${p.minute}`; },
  ymd(ms) { if (!ms) return ''; const p = kparts(ms); return `${p.year}-${p.month}-${p.day}`; },
  ymdDot(ms) { if (!ms) return ''; const p = kparts(ms); return `${p.year}.${p.month}.${p.day}`; },
  dateTime(ms) { if (!ms) return ''; const p = kparts(ms); return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`; },
  weekday(ms) { return ms ? KST_WD.format(new Date(ms)) : ''; },
  /** 'YYYY-MM-DD' → 'YYYY.MM.DD.(목)' */
  meetingDate(s) {
    if (!s) return '';
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    if (!m) return s;
    const d = new Date(`${m[1]}-${m[2]}-${m[3]}T12:00:00+09:00`);
    return `${m[1]}.${m[2]}.${m[3]}.(${KST_WD.format(d)})`;
  },
  /** 초 → '3분 10초' */
  dur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
    if (hh) return `${hh}시간 ${mm}분`;
    if (mm) return ss ? `${mm}분 ${ss}초` : `${mm}분`;
    return `${ss}초`;
  },
  /** 초 → 'MM:SS' 또는 'H:MM:SS' */
  clock(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
    const p2 = (n) => String(n).padStart(2, '0');
    return hh ? `${hh}:${p2(mm)}:${p2(ss)}` : `${p2(mm)}:${p2(ss)}`;
  },
  ago(ms) {
    if (!ms) return '';
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 5) return '방금';
    if (s < 60) return `${s}초 전`;
    if (s < 3600) return `${Math.floor(s / 60)}분 전`;
    if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
    return fmt.dateTime(ms);
  },
  num(n) { return (n ?? 0).toLocaleString('ko-KR'); },
};

export function todayKST() { return fmt.ymd(Date.now()); }

/* ── 화자·인용 ───────────────────────────────────────────────── */
export function speakerLabel(u) {
  const s = (u.speaker || '').trim();
  if (u.acoustic_overlap) return s ? `${s} · 겹침(화자 확인 필요)` : '겹친 발언 · 화자 확인 필요';
  if (!s && u.acoustic_speaker_label) {
    const role = { member: '위원', chair: '위원장', answer: '답변' }[u.speaker_role];
    return role && !u.acoustic_overlap ? `${u.acoustic_speaker_label} · ${role}` : u.acoustic_speaker_label;
  }
  switch (u.speaker_role) {
    case 'member': return s ? (/(위원|의원)$/.test(s) ? s : `${s} 위원`) : '위원(성명 확인 필요)';
    case 'chair': return s ? (/위원장(\s*대리)?$/.test(s) ? s : `${s} 위원장`) : '위원장';
    case 'answer': return s || '답변';
    case 'system': return '기록';
    case 'other': return s || '기타';
    default: return s || '미상';
  }
}

const COMMITTEE_SHORT = {
  국회운영위원회: '운영위', 운영위원회: '운영위', 법제사법위원회: '법사위', 정무위원회: '정무위',
  기획재정위원회: '기재위', 재정경제기획위원회: '재경위', 교육위원회: '교육위', 과학기술정보방송통신위원회: '과방위',
  외교통일위원회: '외통위', 국방위원회: '국방위', 행정안전위원회: '행안위', 문화체육관광위원회: '문체위',
  농림축산식품해양수산위원회: '농해수위', 산업통상자원중소벤처기업위원회: '산자위', 산업통상벤처기업위원회: '산자위',
  보건복지위원회: '복지위', 환경노동위원회: '환노위', 기후에너지환경노동위원회: '기후노동위', 국토교통위원회: '국토위',
  정보위원회: '정보위', 여성가족위원회: '여가위', 성평등가족위원회: '성평등위', 예산결산특별위원회: '예결위',
};
export function committeeShort(name) {
  if (!name) return '';
  const n = name.replace(/\s+/g, '');
  return COMMITTEE_SHORT[n] || n.replace(/위원회$/, '위');
}
export function meetingKind(title) {
  if (!title) return '';
  if (/종합감사|종합\s*국감/.test(title)) return '종합감사';
  if (/국정감사|국감/.test(title)) return '국감';
  if (/청문/.test(title)) return '청문회';
  return '회의';
}
/** 인용문: '[2026.10.08 국토위 국감, 10:04:40, 김남근 위원] …' */
export function quoteText(u, session) {
  const date = session && session.meeting_date ? session.meeting_date.replaceAll('-', '.') : fmt.ymdDot(u.ts_start);
  const what = [committeeShort(session?.committee || ''), meetingKind(session?.title || '')].filter(Boolean).join(' ');
  const head = [date, what].filter(Boolean).join(' ');
  return `[${head}, ${u.time || fmt.hms(u.ts_start)}, ${speakerLabel(u)}] ${u.text}`;
}
export const absUrl = (path) => ctx.offline ? window.gukgamOffline.href(path) : new URL(path, location.origin).href;
export const initial = (name) => (name || '?').trim().charAt(0) || '?';

/* ── 텍스트 강조(감시 키워드 <mark>, 검색 하이라이트) ─────────── */
export function normText(t) { return (t || '').normalize('NFC').replace(/\s+/g, ' ').trim(); }
const compact = (s) => (s || '').replace(/\s+/g, '');

/** mentions(start/end 는 정규화 문자열 기준)를 범위로. 오프셋이 어긋나면 키워드로 다시 찾는다. */
export function mentionRanges(text, mentions) {
  const out = [];
  if (!mentions || !mentions.length) return out;
  for (const m of mentions) {
    const kw = m.keyword || '';
    let s = m.start, e = m.end;
    if (!(Number.isInteger(s) && Number.isInteger(e) && e > s && compact(text.slice(s, e)) === compact(kw))) {
      if (!kw) continue;
      const re = new RegExp(compact(kw).split('').map(escapeRegExp).join('\\s?'), 'g');
      let mm;
      while ((mm = re.exec(text))) {
        out.push({ start: mm.index, end: mm.index + mm[0].length, cls: 'mention', title: `감시 키워드: ${kw}` });
        if (!mm[0].length) re.lastIndex++;
      }
      continue;
    }
    out.push({ start: s, end: e, cls: 'mention', title: `감시 키워드: ${kw}` });
  }
  return out;
}

export function termRanges(text, terms) {
  const out = [];
  if (!terms || !terms.length) return out;
  const lower = text.toLowerCase();
  for (const t of terms) {
    const q = t.toLowerCase();
    if (!q) continue;
    let i = lower.indexOf(q);
    while (i >= 0) {
      out.push({ start: i, end: i + q.length, cls: 'hl' });
      i = lower.indexOf(q, i + q.length);
    }
  }
  return out;
}

/** 겹치지 않게 범위를 골라 <mark> 로 감싼 조각을 만든다(앞 목록이 우선). */
export function markedText(text, ...rangeLists) {
  const taken = [];
  for (const list of rangeLists) {
    for (const r of list) {
      if (taken.some((t) => !(r.end <= t.start || r.start >= t.end))) continue;
      taken.push(r);
    }
  }
  const frag = document.createDocumentFragment();
  if (!taken.length) { frag.append(text); return frag; }
  taken.sort((a, b) => a.start - b.start);
  let pos = 0;
  for (const r of taken) {
    if (r.start > pos) frag.append(text.slice(pos, r.start));
    const mk = document.createElement('mark');
    if (r.cls === 'hl') mk.className = 'hl';
    if (r.title) mk.title = r.title;
    mk.textContent = text.slice(r.start, r.end);
    frag.append(mk);
    pos = r.end;
  }
  if (pos < text.length) frag.append(text.slice(pos));
  return frag;
}

export function splitTerms(q) {
  return (q || '').trim().split(/\s+/).filter(Boolean);
}

/* ── 알림(토스트·스크린리더) ───────────────────────────────── */
export function toast(message, { type = 'info', title = '', timeout = 4500, actions = [] } = {}) {
  const box = document.getElementById('toasts');
  if (!box) return { close() {} };
  const close = () => { el.remove(); };
  const acts = actions.length ? h('div', { class: 't-actions' }, actions.map((a) => (a.href
    ? h('a', { class: 'btn btn-sm', href: a.href }, a.label)
    : h('button', { class: 'btn btn-sm', type: 'button', onclick: () => { a.onClick?.(); close(); } }, a.label)))) : null;
  const el = h('div', { class: `toast ${type}`, role: type === 'err' || type === 'alert' ? 'alert' : 'status' },
    h('div', { class: 't-body' }, title ? h('div', { class: 't-title' }, title) : null, h('div', null, message), acts),
    h('button', { class: 't-close', type: 'button', 'aria-label': '알림 닫기', onclick: close }, icon('x', 'ic-sm')));
  box.append(el);
  while (box.children.length > 5) box.firstElementChild.remove();
  if (timeout) setTimeout(close, timeout);
  return { close, el };
}

export function announce(msg) {
  const el = document.getElementById('sr-live');
  if (!el) return;
  el.textContent = '';
  requestAnimationFrame(() => { el.textContent = msg; });
}

/* ── 대화상자(<dialog>) ─────────────────────────────────────── */
let dlgSeq = 0;
function makeDialog(title, bodyNodes, footNodes, { wide = false } = {}) {
  const id = `dlg-${++dlgSeq}`;
  const dlg = h('dialog', { class: 'dlg', 'aria-labelledby': `${id}-t` });
  if (wide) dlg.style.width = 'min(720px, calc(100vw - 24px))';
  const form = h('form', { method: 'dialog', novalidate: true },
    h('div', { class: 'dlg-h' }, h('h2', { id: `${id}-t` }, title),
      h('button', { class: 'btn btn-ghost btn-icon btn-sm', type: 'button', 'aria-label': '닫기', onclick: () => dlg.close('cancel') }, icon('x'))),
    h('div', { class: 'dlg-b' }, bodyNodes),
    h('div', { class: 'dlg-f' }, footNodes));
  dlg.append(form);
  document.body.append(dlg);
  dlg.addEventListener('close', () => setTimeout(() => dlg.remove(), 0));
  return { dlg, form };
}

/** 내용만 보여 주는 대화상자. 반환: { dlg, close } */
export function infoDialog({ title, body, wide = false, closeLabel = '닫기', actions = [] } = {}) {
  const extra = actions.map((a) => h('button', { class: `btn ${a.primary ? 'btn-primary' : ''}`, type: 'button', onclick: () => a.onClick?.(close) }, a.label));
  const { dlg } = makeDialog(title, Array.isArray(body) ? body : [body],
    [...extra, h('button', { class: 'btn', type: 'button', onclick: () => dlg.close('close') }, closeLabel)], { wide });
  const close = () => { if (dlg.open) dlg.close('close'); };
  dlg.showModal();
  return { dlg, close };
}

export function confirmDialog({ title = '확인', message = '', ok = '확인', cancel = '취소', danger = false } = {}) {
  return new Promise((resolve) => {
    const okBtn = h('button', { class: `btn ${danger ? 'btn-danger-solid' : 'btn-primary'}`, type: 'submit', value: 'ok' }, ok);
    const { dlg, form } = makeDialog(title, [h('p', null, message)],
      [h('button', { class: 'btn', type: 'button', onclick: () => dlg.close('cancel') }, cancel), okBtn]);
    form.addEventListener('submit', (e) => { e.preventDefault(); dlg.close('ok'); });
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'));
    dlg.showModal();
    okBtn.focus();
  });
}

/** formDialog 의 입력 칸(라벨·힌트를 감싼 .field)을 숨기거나 보인다 */
export function showField(ctl, show) {
  const wrap = ctl && (ctl.closest('.field') || ctl.closest('label.check'));
  if (wrap) wrap.hidden = !show;
}

/**
 * 입력 대화상자. fields: [{name,label,type,value,options:[[값,라벨]],list:[...],placeholder,required,hint,rows,onInput}]
 * onOpen(controls): 열린 직후 한 번(칸 표시 초기화 등). 숨긴 칸은 필수 검사에서 빠진다.
 * 반환: 값 객체 또는 null(취소)
 */
export function formDialog({ title, fields, submit = '저장', description = '', wide = false, validate, onOpen } = {}) {
  return new Promise((resolve) => {
    const controls = {};
    const nodes = [];
    if (description) nodes.push(h('p', { class: 'small muted' }, description));
    for (const f of fields) {
      const fid = `fd-${dlgSeq + 1}-${f.name}`;
      let ctl;
      if (f.type === 'textarea') {
        ctl = h('textarea', { id: fid, name: f.name, rows: f.rows || 5, placeholder: f.placeholder || null, required: !!f.required });
        ctl.value = f.value ?? '';
      } else if (f.type === 'select') {
        ctl = h('select', { id: fid, name: f.name },
          (f.options || []).map(([v, l]) => h('option', { value: v }, l)));
        ctl.value = f.value ?? '';
      } else if (f.type === 'checkbox') {
        ctl = h('input', { id: fid, name: f.name, type: 'checkbox' });
        ctl.checked = !!f.value;
      } else {
        ctl = h('input', { id: fid, name: f.name, type: f.type || 'text', placeholder: f.placeholder || null, required: !!f.required, autocomplete: 'off' });
        ctl.value = f.value ?? '';
        if (f.list && f.list.length) {
          const lid = `${fid}-list`;
          ctl.setAttribute('list', lid);
          nodes.push(h('datalist', { id: lid }, f.list.map((v) => (Array.isArray(v)
            ? h('option', { value: v[0] }, v[1]) : h('option', { value: v })))));
        }
      }
      controls[f.name] = ctl;
      if (f.type === 'checkbox') {
        nodes.push(h('label', { class: 'check', for: fid }, ctl, f.label));
      } else {
        nodes.push(h('div', { class: 'field' },
          h('label', { for: fid }, f.label, f.required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null),
          ctl, f.hint ? h('span', { class: 'hint' }, f.hint) : null));
      }
      if (f.onInput) ctl.addEventListener('input', () => f.onInput(ctl, controls));
    }
    const err = h('div', { class: 'form-error', role: 'alert' });
    nodes.push(err);
    const { dlg, form } = makeDialog(title, nodes, [
      h('button', { class: 'btn', type: 'button', onclick: () => dlg.close('cancel') }, '취소'),
      h('button', { class: 'btn btn-primary', type: 'submit', value: 'ok' }, submit),
    ], { wide });
    let result = null;
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const vals = {};
      for (const [k, c] of Object.entries(controls)) vals[k] = c.type === 'checkbox' ? c.checked : c.value;
      for (const f of fields) {
        const wrap = controls[f.name].closest('.field');
        if (wrap && wrap.hidden) continue;
        if (f.required && !String(vals[f.name] ?? '').trim()) { err.textContent = `${f.label}을(를) 입력하세요.`; controls[f.name].focus(); return; }
      }
      const msg = validate ? validate(vals) : '';
      if (msg) { err.textContent = msg; return; }
      result = vals;
      dlg.close('ok');
    });
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok' ? result : null));
    dlg.showModal();
    if (onOpen) onOpen(controls);
    const first = fields.find((f) => f.autofocus) || fields[0];
    if (first && controls[first.name]) {
      controls[first.name].focus();
      if (controls[first.name].select && first.type !== 'select') controls[first.name].select();
    }
  });
}

/* ── 팝업 메뉴 ─────────────────────────────────────────────── */
let openMenu = null;
export function closeMenu() {
  if (!openMenu) return;
  const { el, anchor } = openMenu;
  openMenu = null;
  el.remove();
  anchor?.setAttribute('aria-expanded', 'false');
}
/** items: [{label, icon, onClick, href, danger, hidden}] 또는 'sep' */
export function popupMenu(anchor, items, { label = '메뉴' } = {}) {
  if (openMenu && openMenu.anchor === anchor) { closeMenu(); return; }
  closeMenu();
  const el = h('div', { class: 'menu', role: 'menu', 'aria-label': label });
  for (const it of items) {
    if (!it || it.hidden) continue;
    if (it === 'sep') { el.append(h('div', { class: 'sep', role: 'separator' })); continue; }
    const node = it.href
      ? h('a', { href: it.href, role: 'menuitem', target: it.target || null, rel: it.target ? 'noopener' : null }, it.icon ? icon(it.icon) : null, it.label)
      : h('button', { type: 'button', role: 'menuitem' }, it.icon ? icon(it.icon) : null, it.label);
    if (it.danger) node.style.color = 'var(--danger)';
    node.addEventListener('click', (e) => { if (!it.href) e.preventDefault(); closeMenu(); it.onClick?.(); anchor?.focus?.({ preventScroll: true }); });
    el.append(node);
  }
  document.body.append(el);
  const r = anchor.getBoundingClientRect();
  const mw = el.offsetWidth, mh = el.offsetHeight;
  let left = Math.min(r.right - mw, window.innerWidth - mw - 8);
  left = Math.max(8, left);
  let top = r.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  anchor.setAttribute('aria-expanded', 'true');
  openMenu = { el, anchor, dy: top - r.bottom };
  const btns = $$('[role="menuitem"]', el);
  btns[0]?.focus();
  el.addEventListener('keydown', (e) => {
    const i = btns.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); btns[(i + 1) % btns.length]?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); btns[(i - 1 + btns.length) % btns.length]?.focus(); }
    else if (e.key === 'Escape') { e.preventDefault(); closeMenu(); anchor.focus(); }
    else if (e.key === 'Tab') { closeMenu(); }
  });
}
document.addEventListener('pointerdown', (e) => {
  if (openMenu && !openMenu.el.contains(e.target) && !openMenu.anchor.contains(e.target)) closeMenu();
}, true);
window.addEventListener('resize', closeMenu);
window.addEventListener('scroll', () => {
  // 자동 스크롤 중에도 메뉴가 기준 버튼을 따라가게 한다(화면 밖으로 나가면 닫음)
  if (!openMenu) return;
  const r = openMenu.anchor.getBoundingClientRect();
  if (r.bottom < 0 || r.top > window.innerHeight || !openMenu.anchor.isConnected) { closeMenu(); return; }
  openMenu.el.style.top = `${r.bottom + openMenu.dy}px`;
}, { passive: true });

/* ── 복사·내려받기 ──────────────────────────────────────────── */
export async function copyText(text, okMsg = '클립보드에 복사했습니다.') {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch { /* 대체 경로 */ }
  if (!ok) {
    const ta = h('textarea', { style: { position: 'fixed', top: '-1000px', opacity: '0' }, readonly: true });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
  }
  if (ok) toast(okMsg, { type: 'ok', timeout: 2200 });
  else toast('복사하지 못했습니다. 직접 선택해 복사하세요.', { type: 'err' });
  return ok;
}

export function downloadText(filename, text, mime = 'text/plain;charset=utf-8') {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** CSV 칸 하나. =,+,-,@,탭,CR 로 시작하면 엑셀이 수식으로 읽으므로(#NAME?·수식 주입) 앞에 ' 를 붙인다. */
export function csvCell(v) {
  let s = v == null ? '' : String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCSV(rows) {
  return '﻿' + rows.map((r) => r.map(csvCell).join(',')).join('\r\n');
}

/* ── 기타 유틸 ─────────────────────────────────────────────── */
export function debounce(fn, ms = 200) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export const store = {
  get(key, def = null) {
    try { const v = localStorage.getItem(`gukgam.${key}`); return v == null ? def : JSON.parse(v); } catch { return def; }
  },
  set(key, val) {
    try { localStorage.setItem(`gukgam.${key}`, JSON.stringify(val)); } catch { /* 저장 불가 무시 */ }
  },
};

export function queryParams() {
  return Object.fromEntries(new URLSearchParams(location.search).entries());
}

export function setQuery(params, replace = true) {
  const u = new URL(location.href);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '' || v === false) u.searchParams.delete(k);
    else u.searchParams.set(k, String(v));
  }
  history[replace ? 'replaceState' : 'pushState'](null, '', u.pathname + u.search + u.hash);
}

// 글자를 입력하지 않는 input(스위치·라디오·버튼 등): 여기에 포커스가 있어도 단축키는 동작해야 한다
const NON_TEXT_INPUTS = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'file', 'color', 'image']);

/** 입력 중인지(단축키 무시 판단). 체크박스·라디오·버튼에 포커스가 있는 것은 입력 중이 아니다. */
export function isTyping(e) {
  const t = e.target;
  if (!t || !(t instanceof Element)) return false;
  if (t.closest('dialog')) return true;
  const el = t.closest('input, textarea, select, [contenteditable="true"]');
  if (!el) return false;
  if (String(el.tagName).toUpperCase() === 'INPUT') {
    const type = String(el.getAttribute('type') || 'text').toLowerCase();
    if (NON_TEXT_INPUTS.has(type)) return false;
  }
  return true;
}

export function statusBadge(status) {
  const st = SESSION_STATUS[status] || { label: status, cls: 'b-gray' };
  return badge(st.label, st.cls);
}

export function orgRelevanceBadge(level) {
  const r = ORG_RELEVANCE[level];
  return r ? badge(r.label, r.cls) : null;
}

/** 요약 객체 정규화: serialize.summary 래퍼({content,...}) 또는 SessionSummary 그 자체 */
export function summaryContent(s) {
  if (!s) return null;
  if (s.content && typeof s.content === 'object') return { ...s.content, _time: s.time || '', _created_at: s.created_at, _provider: s.provider || s.content.provider, _model: s.model || s.content.model };
  if (typeof s.content === 'string') {
    try { return summaryContent({ ...s, content: JSON.parse(s.content) }); } catch { return null; }
  }
  return { ...s, _created_at: s.generated_at, _time: s.generated_at ? fmt.dateTime(s.generated_at) : '' };
}

/** 저장된 생성 출처와 입력 범위만 표시한다. 실패 사유 원문은 화면에 넘기지 않는다. */
export function summaryPresentation(value) {
  let s = value && typeof value === 'object' ? value : {};
  if (typeof s.content === 'string') {
    try { s = { ...s, content: JSON.parse(s.content) }; } catch { s = {}; }
  }
  const c = s.content && typeof s.content === 'object' && !Array.isArray(s.content) ? s.content : s;
  const rawProvider = c.provider || c._provider || s.provider || '';
  const provider = typeof rawProvider === 'string' ? rawProvider.trim() : '';
  const referenceContent = Object.hasOwn(s, 'content') ? s.content : s;
  const referenceId = referenceContent && typeof referenceContent === 'object' && !Array.isArray(referenceContent)
    ? referenceContent.source_document_id : undefined;
  let kind = 'unknown', label = '생성 출처 미확인', note = '저장된 요약의 생성 출처를 확인할 수 없습니다. 원문과 대조해 확인하세요.';
  if (provider === 'rules') {
    kind = 'rules'; label = '규칙 정리 초안'; note = '저장된 발언을 규칙으로 발췌·정리했습니다. 원문과 대조해 확인하세요.';
  } else if (provider === 'rules(대체)') {
    kind = 'fallback'; label = '규칙 대체 초안'; note = 'AI 요약 결과를 사용하지 못해 규칙 정리로 대체했습니다. 원문과 대조하고 필요하면 다시 요약하세요.';
  } else if (provider === 'closed_block' && Number.isSafeInteger(referenceId) && referenceId > 0) {
    kind = 'reference'; label = '보정 문서 참조 요약'; note = '저장된 보정 문서의 요약을 참조합니다. 이 표시만으로 생성 모델이나 검수 완료를 확인할 수 없습니다. 원문과 보정 문서를 대조해 확인하세요.';
  } else if (['desktop_chatgpt', 'local_mlx', 'openai_compat', 'anthropic'].includes(provider)) {
    kind = 'ai'; label = provider === 'desktop_chatgpt' ? 'ChatGPT 요약 초안' : 'AI 요약 초안'; note = 'AI가 생성한 요약 초안입니다. 검수 전에는 원문과 대조해 확인하세요.';
  }
  const originLabel = label;
  if (typeof c.edited_by === 'string' && c.edited_by.trim()) {
    kind = 'human'; label = '사람 수정 요약'; note = `사람이 수정한 요약입니다. 검수 여부는 별도 표시를 확인하세요. 수정 전 출처: ${originLabel}.`;
  }
  const coverage = c.coverage && typeof c.coverage === 'object' && !Array.isArray(c.coverage) ? c.coverage : null;
  const coverageNotes = [];
  let coverageLabel = '';
  if (coverage) {
    const boundaryUnknown = coverage.question_boundary_status === 'unclassified' || coverage.question_presence_unverified === true;
    if (boundaryUnknown) {
      coverageLabel = '질의 경계 미확정 · 발언 기반';
      coverageNotes.push('실제 질의 유무와 화자별 질의·답변 경계를 확인하지 않은 발언 기반 요약입니다.');
    }
    const n = coverage.eligible_utterance_count, k = coverage.selected_utterance_count;
    if (coverage.source === 'final_utterances' && coverage.selection === 'recent_bounded'
        && Number.isSafeInteger(n) && Number.isSafeInteger(k) && n >= k && k >= 0) {
      coverageNotes.push(`확정 발언 ${n}건 중 ${k}건을 입력했습니다.`);
      const added = coverage.appended_after_selection_count;
      if (Number.isSafeInteger(added) && added > 0 && added <= n - k) {
        coverageNotes.push(`입력 선택 후 추가 저장된 ${added}건은 이 요약에 포함되지 않았습니다.`);
      }
      const start = coverage.selected_start_ms, end = coverage.selected_end_ms;
      if (Number.isSafeInteger(start) && Number.isSafeInteger(end) && start > 0 && end >= start && end < 8640000000000000 - 32400000) {
        const time = ms => new Date(ms + 32400000).toISOString().slice(11, 19);
        coverageNotes.push(`입력 시각 범위: ${time(start)}–${time(end)}(한국 시각).`);
      }
    }
    if (coverage.partial === true || coverage.text_truncated === true) {
      coverageLabel = coverageLabel ? `일부 발언 · ${coverageLabel}` : '일부 발언 요약';
      coverageNotes.push('입력에 포함되지 않은 발언이나 축약된 본문이 있습니다. 회의 전체를 빠짐없이 다뤘다고 볼 수 없습니다.');
    } else if (coverage.meeting_coverage_verified === false) {
      coverageNotes.push('회의 전체 내용의 반영 여부는 검증하지 않았습니다.');
    }
  }
  return { kind, label, note, coverageLabel, coverageNote: coverageNotes.join(' ') };
}

/** 음성 수집 경로의 본문 한계 안내. 연결 오류·경고 상태와는 별개다. */
export function audioTranscriptNotice(sourceType) {
  return ['assembly_hls', 'browser', 'file'].includes(sourceType)
    ? '음성 전사 초안입니다. 화자·질의 경계와 용어·숫자는 원음을 대조해 확인하세요.' : '';
}

/**
 * 회의 전체 요약(a)과 실시간(롤링) 요약(b) 중 더 최신 것(summaryContent 결과끼리).
 * 둘 다 다룬 구간 끝(covered_until_ms)이 있으면 그것으로, 없으면 만든 시각으로 비교한다(같으면 a).
 * 일시정지 때 만든 회의 전체 요약에 '주요 이슈'가 고정되지 않게 한다.
 */
export function newerSummary(a, b) {
  if (!a) return b || null;
  if (!b) return a;
  const ca = Number(a.covered_until_ms) || 0;
  const cb = Number(b.covered_until_ms) || 0;
  if (ca && cb) return cb > ca ? b : a;
  const ta = Number(a._created_at || a.generated_at) || 0;
  const tb = Number(b._created_at || b.generated_at) || 0;
  return tb > ta ? b : a;
}

/**
 * 타임라인 스크러버 키보드 이동: 현재 시각(ms) curT 에서 key 만큼 옮긴 시각. 해당 키가 아니면 null.
 * 위치를 정수 % 로 되읽으면 긴 회의에서 1분 걸음이 반올림에 묻혀 제자리이므로 ms 로 계산한다.
 */
export function scrubKeyTarget(key, curT, t0, t1) {
  const step = { ArrowLeft: -60000, ArrowRight: 60000, ArrowDown: -60000, ArrowUp: 60000, PageDown: -600000, PageUp: 600000 }[key];
  if (key === 'Home') return t0;
  if (key === 'End') return t1;
  if (!step) return null;
  const base = Number.isFinite(curT) ? curT : t1;
  return Math.max(t0, Math.min(t1, base + step));
}

/** 블록 요약 정규화(문자열 JSON 대비) */
export function blockSummary(b) {
  let s = b && b.summary;
  if (!s) return null;
  if (typeof s === 'string') { try { s = JSON.parse(s); } catch { return null; } }
  return typeof s === 'object' && Object.keys(s).length ? s : null;
}

/** 저장된 요약 생성 출처와 별도로 검수 이름·시각을 표시한다. */
export function reviewNote(b) {
  const label = summaryPresentation(blockSummary(b)).label;
  if (b && b.review_status === 'reviewed') {
    return `※${label} / 검수: ${b.reviewed_by || '운영자'}${b.reviewed_at ? `(${fmt.dateTime(b.reviewed_at)})` : ''}`;
  }
  return `※${label} / 검수: 미검수`;
}

/** 사람이 고친 블록 요약 표기: '수정: 이름(시각)'. 고친 적이 없으면 '' */
export function editNote(sm) {
  if (!sm || !sm.edited_by) return '';
  return `수정: ${sm.edited_by}${sm.edited_at ? `(${fmt.dateTime(sm.edited_at)})` : ''}`;
}

/* ── 전역: data-copy 버튼 ──────────────────────────────────── */
document.addEventListener('click', (e) => {
  const el = e.target instanceof Element ? e.target.closest('[data-copy]') : null;
  if (el) { e.preventDefault(); copyText(el.getAttribute('data-copy')); }
});

/** 수집 PC 상태를 브라우저 SSE 연결 상태와 따로 표시한다. */
export function cloudAgentView(agent = {}) {
  if (!agent.online) {
    const last = fmt.hm(Number(agent.heartbeat_at));
    return { state: 'offline', cls: 'b-red', label: `수집 PC 끊김${last ? ` (마지막 신호 ${last})` : ''}` };
  }
  if (agent.mode === 'idle') return { state: 'idle', cls: 'b-gray', label: '수집 PC 대기 중' };
  if (agent.read_mode !== 'source' && Number(agent.push_delay_ms) >= 120000) return { state: 'delayed', cls: 'b-orange', label: '수집 PC 연결됨(지연)' };
  return { state: 'online', cls: 'b-green', label: '수집 PC 연결됨' };
}

function initCloudControls() {
  if (!ctx.cloud) return;
  const badgeEl = $('#cloud-agent-badge');
  const labelEl = $('#cloud-agent-label');
  let agent = {};
  try { agent = JSON.parse(document.body.dataset.agent || '{}'); } catch { /* 초기 신호 없음 */ }
  function render() {
    if (!badgeEl || !labelEl) return;
    const view = cloudAgentView(agent);
    badgeEl.className = `badge cloud-agent-badge ${view.cls}`;
    badgeEl.dataset.state = view.state;
    labelEl.textContent = view.label;
  }
  render();
  let timer;
  let stopped = false;
  async function poll() {
    if (stopped) return;
    if (!document.hidden) {
      try { agent = (await api('health')).agent || {}; }
      catch { agent = { ...agent, online: false }; }
      render();
    }
    if (!stopped) timer = setTimeout(poll, 15000);
  }
  if (!location.pathname.startsWith('/login')) timer = setTimeout(poll, 15000);
  window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); }, { once: true });
  $('#cloud-resync')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    setBusy(button, true);
    try {
      await api('cloud/resync', { method: 'POST' });
      toast('클라우드 기록을 다시 맞추도록 요청했습니다.');
    } catch (error) { toast(errMsg(error), { type: 'warn' }); }
    finally { setBusy(button, false); }
  });
}

initCloudControls();
/** 본문은 바꾸지 않고 저장된 최종 상태의 확인 표식만 공통으로 표시한다. */
export function correctionNotices(value) {
  if (!value || typeof value !== 'object') return [];
  const labels = [];
  if ((value.document_status ?? value.status) === 'partial'
      || value.text_uncertain === true && value.text_status !== 'held') labels.push('일부 보정 확인 필요');
  if (value.text_status === 'held') labels.push('본문 보정 보류 · 원문 유지');
  if (value.role_uncertain === true) labels.push('화자 확인 필요');
  return labels;
}
