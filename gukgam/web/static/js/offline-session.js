// 단독 파일의 읽기 API. 네트워크나 서버 쓰기 대신 내장한 같은 저장 판본만 반환한다.
async function readArchive() {
  const data = document.getElementById('offline-data');
  if (!data) throw Error('저장된 회의 자료가 없습니다.');
  if (data.dataset.compression !== 'gzip') return JSON.parse(data.textContent);
  if (typeof DecompressionStream !== 'function') throw Error('압축 해제를 지원하는 최신 브라우저에서 열어주세요.');
  const bytes = Uint8Array.from(atob(data.textContent.trim()), (value) => value.charCodeAt(0));
  const text = await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
  return JSON.parse(text);
}
const archive = await readArchive();
const saved = archive.snapshot;
const sid = saved.session.id;
const clone = (value) => JSON.parse(JSON.stringify(value));
const abort = () => { const error = new Error('조회 취소'); error.name = 'AbortError'; throw error; };
const failure = (message, status = 404) => { const error = new Error(message); error.status = status; throw error; };
const internalHref = (path) => {
  const value = String(path);
  const utt = /^\/u\/(\d+)/.exec(value);
  if (utt) return `#u-${utt[1]}`;
  if (value.includes('/report')) return '#offline-report';
  if (value.startsWith('/items')) return '#offline-items';
  if (value.startsWith('/search')) return '#offline-search';
  if (value.includes('#')) return value.slice(value.indexOf('#'));
  const block = /[?&](?:b|bid)=(\d+)/.exec(value);
  return block ? `#b-${block[1]}` : '#offline-feed';
};
function download(entry) {
  const bytes = Uint8Array.from(atob(entry.base64), (value) => value.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: entry.mime }));
  const link = document.createElement('a'); link.href = url; link.download = entry.name;
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
window.gukgamOffline = {
  href: internalHref,
  async api(path, { method = 'GET', body, query, signal } = {}) {
    if (signal?.aborted) return abort();
    const url = new URL(path.startsWith('/') ? path : `/api/${path}`, 'file:///단독열람.html');
    for (const [key, value] of Object.entries(query || {})) if (value != null) url.searchParams.set(key, String(value));
    const route = url.pathname.replace(/^\/api\//, '');
    const bookmark = /^utterances\/(\d+)\/bookmark$/.exec(route);
    if (method.toUpperCase() !== 'GET') {
      if (bookmark && method.toUpperCase() === 'POST') {
        const id = Number(bookmark[1]);
        if (!saved.utterances.some((row) => row.id === id)) return failure('발언이 없습니다');
        const was = saved.bookmarks.includes(id);
        saved.bookmarks = was ? saved.bookmarks.filter((uid) => uid !== id) : [...saved.bookmarks, id];
        return { bookmarked: !was, utterance_id: id };
      }
      if (route === `sessions/${sid}/alerts/ack`) {
        for (const row of saved.alerts) if (body?.alert_id == null || row.id === body.alert_id) row.ack = true;
        return { ok: true };
      }
      return failure('보관본에서는 서버 기록을 수정하거나 새 AI 분석을 실행하지 않습니다', 403);
    }
    if (route === `sessions/${sid}/snapshot` || route === `sessions/${sid}/transcript-stages`) {
      // 전체 자료가 이미 포함되어 있으므로 최신/과거 범위 요청도 같은 전체 판본을 반환한다.
      return clone({ ...saved, documents: [], live_turns: [], next_before: null,
        snapshot_at: archive.manifest.generated_at,
        valid_utterance_ids: saved.utterances.map((row) => row.id) });
    }
    if (route === `sessions/${sid}`) return clone(saved.session);
    if (route === `sessions/${sid}/report`) return clone(archive.reports[url.searchParams.get('org_only') === '1' ? '1' : '0']);
    if (route === `sessions/${sid}/transcript-reviews`) return { items: [] };
    if (route === 'settings') return { departments: [] };
    if (route === 'sessions') return { items: [clone(saved.session)] };
    if (route === 'items') return { items: clone(saved.items.filter((row) =>
      !url.searchParams.get('status') || row.status === url.searchParams.get('status'))) };
    const card = /^blocks\/(\d+)\/card$/.exec(route);
    if (card) {
      const block = saved.blocks.find((row) => row.id === Number(card[1]));
      if (!block) return failure('질의가 없습니다');
      const summary = block.summary || {};
      return { text: [block.title, summary.one_line, ...(summary.question_points || []),
        ...(summary.answer_points || [])].filter(Boolean).join('\n') };
    }
    return failure('이 기능은 보관본에 포함되지 않습니다');
  },
  mount(modules) {
    const { h, setKids, infoDialog, copyText, fmt } = modules['common.js'];
    const { renderSituationReport, setReportExpanded } = modules['report-view.js'];
    const sessionView = document.getElementById('sess');
    const reportPage = h('section', { id: 'offline-report-page', hidden: true });
    document.getElementById('main').append(reportPage);
    let orgOnly = false;
    const report = () => archive.reports[orgOnly ? '1' : '0'];
    const renderReport = () => {
      const filter = h('input', { type: 'checkbox', checked: orgOnly, onchange: (event) => { orgOnly = event.target.checked; renderReport(); } });
      const view = renderSituationReport(report(), { idPrefix: 'offline-rp',
        sessionHref: (id) => `#b-${id}`, sourceHref: (id) => `#u-${id}`,
      });
      setKids(reportPage, h('div', { class: 'page-head' }, h('div', null,
        h('h1', null, '국정감사 상황보고'),
        h('p', { class: 'small muted' }, `저장 시점 ${fmt.dateTime(archive.manifest.generated_at)} · 서버 연결 없음`)),
        h('div', { class: 'row' },
          h('button', { class: 'btn btn-primary', type: 'button', onclick: () => copyText(report().text) }, '복사'),
          h('button', { class: 'btn', type: 'button', onclick: () => window.print() }, '인쇄'),
          h('button', { class: 'btn', type: 'button', onclick: () => download(archive.exports[`${orgOnly ? 1 : 0}:docx`]) }, '워드 다운로드'))),
        h('div', { class: 'toolbar' }, h('label', { class: 'switch' }, filter, '우리 기관 관련만'),
          h('button', { class: 'btn btn-sm', type: 'button', onclick: () => setReportExpanded(view, true) }, '모두 펼치기'),
          h('button', { class: 'btn btn-sm', type: 'button', onclick: () => setReportExpanded(view, false) }, '모두 접기')),
        view);
    };
    function show(tab) {
      const isReport = tab === 'report';
      sessionView.hidden = isReport; reportPage.hidden = !isReport;
      document.body.classList.toggle('page-report', isReport);
      if (isReport) renderReport();
      else {
        const target = tab === 'search' ? 'feed' : tab;
        document.getElementById(`tb-${target}`)?.click();
        if (tab === 'search') document.getElementById('q')?.focus();
      }
    }
    document.addEventListener('click', (event) => {
      const anchor = event.target.closest?.('a[href]');
      if (!anchor || anchor.hasAttribute('download')) return;
      const href = anchor.getAttribute('href');
      if (!href || href === '#') return;
      if (href.startsWith('blob:')) return;
      const exportPath = /^\/api\/sessions\/[^/]+\/export\?(.+)$/.exec(href);
      if (exportPath) {
        event.preventDefault();
        const query = new URLSearchParams(exportPath[1]);
        const key = `${query.get('org_only') === '1' ? 1 : 0}:${query.get('format') || 'md'}`;
        if (archive.exports[key]) download(archive.exports[key]);
        return;
      }
      const internal = href.startsWith('/') ? internalHref(href) : href;
      const tabLink = /^#offline-(feed|members|items|alerts|summary|search|report)$/.exec(internal);
      if (tabLink) {
        event.preventDefault(); show(tabLink[1]); return;
      }
      if (/^#[ubd]-/.test(internal)) {
        event.preventDefault(); show('feed');
        const original = /^#u-(\d+)$/.exec(internal);
        if (href.startsWith('/u/') && original) {
          const row = archive.originals.find((value) => value.id === Number(original[1]));
          if (row) infoDialog({ title: `${row.time} ${row.speaker || row.role_label}`, wide: true,
            body: h('div', null, h('h3', null, '현재 저장 본문'), h('p', { class: 'prewrap' }, row.text),
              h('h3', null, '원문 보존본'), h('p', { class: 'prewrap' }, row.original_text)) });
        } else location.hash = internal;
        return;
      }
      if (internal.startsWith('#') && document.getElementById(internal.slice(1))) return;
      event.preventDefault();
      infoDialog({ title: '단독 열람본', body: h('p', null, '이 파일은 외부 페이지나 서버에 연결하지 않습니다. 회의 기록은 상단 메뉴에서 확인하세요.') });
    }, true);
    // 제3자 자료가 아니라 저장 사실을 설명하는 별도 내역이다.
    const manifestButton = h('button', { class: 'btn btn-sm', type: 'button', onclick: () => infoDialog({
      title: '포함된 회의 자료', body: h('div', null, h('p', null,
        `발언 ${archive.manifest.counts.utterances}개 · 원자막 ${archive.manifest.counts.sources}개 · 저장 보정 ${archive.manifest.counts.corrected_blocks}블록`),
        h('p', null, '최신 정리 기록·미보정 구간·원문 연결·사람 수정·의원별 요약·후속조치·알림·상황보고를 포함합니다. 중간 판정·과거 판본·계정·원음·영상·AI 모델은 포함하지 않습니다.')),
    }) }, '포함 자료 확인');
    document.querySelector('.userbox, .topbar')?.append(manifestButton);
    let printState = null;
    window.addEventListener('beforeprint', () => {
      if (reportPage.hidden) return;
      printState = [...reportPage.querySelectorAll('details.rp-question')].map((element) => [element, element.open]);
      setReportExpanded(reportPage, true);
    });
    window.addEventListener('afterprint', () => {
      for (const [element, wasOpen] of printState || []) element.open = wasOpen;
      printState = null;
    });
  },
};
