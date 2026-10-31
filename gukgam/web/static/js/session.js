// 메인 중계·기록 화면(/s/{sid}).
// /snapshot 으로 초기 렌더 → EventSource(/events) 로 증분 반영. 이벤트는 큐에 모아 requestAnimationFrame 한 번에 반영하고,
// 발언 행·블록 DOM 은 재사용한다(수천 발언에서도 버벅이지 않게).
import {
  $, $$, absUrl, announce, api, badge, BLOCK_STATUS, blockSummary, committeeShort, confirmDialog, copyText, ctx,
  debounce, DUE_PRESETS, editNote, emptyState, errMsg, errorEl, fmt, formDialog, h, icon, infoDialog, initial,
  isMissingRoute, isOperator, isTyping, ITEM_KINDS, ITEM_ORIGIN, ITEM_STATUS, MARKER_PRESETS, markedText,
  meetingKind, mentionRanges, newerSummary, normText, NOTICE, orgRelevanceBadge, PARTIES, PHASE_LABELS, popupMenu,
  quoteText, RESPONSE_TYPES, reviewNote, scrubKeyTarget, SESSION_STATUS, setBusy, showField, SOURCE_LABELS,
  speakerLabel, splitTerms, store, STRENGTHS, summaryContent, termRanges, toast, setKids, UPLOAD_ACCEPT,
  uploadExtOk, URGENCY, correctionNotices, summaryPresentation, audioTranscriptNotice,
} from './common.js';

const SID = ctx.sid;
const OP = isOperator();
const enc = encodeURIComponent;
const EVENT_TYPES = ['session.status', 'utterance.upsert', 'utterance.delete', 'block.upsert', 'item.upsert',
  'items.reload', 'alert', 'alert.delete', 'summary.update', 'stats', 'analysis.status', 'transcript.review',
  'caption.source', 'transcript.stage', 'transcript.display', 'roster.updated', 'settings.updated'];

/* ── 상태 ─────────────────────────────────────────────────── */
const S = {
  session: null,
  stats: null,
  blocks: new Map(),
  utts: new Map(),
  items: new Map(),
  alerts: new Map(),
  summary: null,
  rolling: null,
  roster: [],
  bookmarks: new Set(),
  analysis: null,
  lastEventId: 0,
};
const itemsByUtt = new Map();   // 발언 id → Set(항목 id)
const reviewCache = new Map(); // 블록 id → 이미 보관된 초안 목록(조회만 재사용)
const reviewLoads = new Map();
const reviewVersions = new Map();
const reviewNotices = new Map();
const aiOriginalChoice = new Map(); // 블록별 원문 선택은 새 초안이 와도 유지한다.
let displayNavigation = 0; // 늦게 끝난 검색 이동이 사용자의 다음 이동을 덮지 않는다.
let pendingDisplayNavigation = 0;
let viewInteraction = 0;
function cancelPendingDisplayNavigation() {
  viewInteraction += 1;
  if (!pendingDisplayNavigation) return;
  displayNavigation += 1;
  pendingDisplayNavigation = 0;
}
const stageRemainders = new Map(); // 보정이 소비한 문자만 표시에서 제외하고 원본 UID는 보존한다.
let stageClaimsReady = false;
const stageClaimsByBlock = new Map();
const stageClaimOwners = new Map();
const stageUpdateQueue = new Set();
let stageUpdateTimer = 0;
const order = [];               // 시간순 발언 id
const rowEl = new Map();        // 발언 id → <li>
const groupMap = new Map();     // 그룹 키 → 그룹
const groups = [];              // DOM 순서의 그룹 목록
const dirtyGroups = new Set();
const dirty = { members: true, items: true, alerts: true, summary: true, cur: true, scrub: true, counts: true };

const V = {
  tab: 'feed',
  autoscroll: true,
  showTime: store.get('sess.showTime', true),
  fontSize: store.get('sess.fontSize', 15),
  q: '',
  terms: [],
  bmOnly: false,
  speaker: '',       // 이 화자 발언만
  questioner: '',    // 이 위원 질의 블록만
  emph: '',          // 이 화자 강조
  focusId: null,
  focusRow: null,
  selectedBlock: null,
  newSincePause: 0,
  sound: store.get('sess.sound', false),
  notify: store.get('sess.notify', false),
  manualOpen: false,  // 수기 입력 세션이 아니어도 '보충 발언 입력' 바를 연 상태
  booted: false,
};
let summaryEditOk = true;  // 서버가 블록 요약 수정 API 를 지원하는지(404/405 면 버튼을 숨긴다)

const feed = $('#feed');
const feedHead = $('.feed-head');
const history = { partial: false, nextBefore: null, loading: false, blocks: new Map(), filterGeneration: 0,
  revision: 0, fullRequest: null, allLoaded: false };
const recentCorrections = { ids: [], timer: null, running: false, stopped: false };
const olderPrefetch = { entry: null, timer: null, epoch: 0, attemptedBefore: null };
const HISTORY_PREFETCH_TTL = 30000;

function invalidateHistoryPrefetch() {
  olderPrefetch.epoch += 1;
  clearTimeout(olderPrefetch.timer); olderPrefetch.timer = null;
  olderPrefetch.entry?.abort.abort(); olderPrefetch.entry = null;
}

function currentOlderPrefetch(entry, before) {
  return entry && entry.before === before && entry.epoch === olderPrefetch.epoch
    && entry.eventId === S.lastEventId && (!entry.received || Date.now() - entry.received < HISTORY_PREFETCH_TTL);
}

function canPrefetchOlderHistory() {
  const view = window.gukgamTranscriptStages;
  return V.booted && V.tab === 'feed' && !filtering() && !document.hidden && !recentCorrections.stopped
    && ['ended', 'stopped', 'paused'].includes(S.session?.status)
    && history.partial && history.nextBefore !== null && !history.loading && !history.foreground
    && !recentCorrections.running
    && (S.session?.source_type !== 'assembly_caption' || view?.loaded && !view.loading && !view.blockReads?.size);
}

function queueOlderHistoryPrefetch(delay = 300) {
  // 최초 판본·사용자 조회 뒤 다음 원문 한 페이지만, 질의별 배경 보완보다 먼저 준비한다.
  if (olderPrefetch.timer !== null || olderPrefetch.entry || olderPrefetch.attemptedBefore === history.nextBefore
      || !canPrefetchOlderHistory()) return;
  olderPrefetch.timer = setTimeout(() => {
    olderPrefetch.timer = null;
    if (!canPrefetchOlderHistory()) return;
    const entry = { before: history.nextBefore, epoch: olderPrefetch.epoch, eventId: S.lastEventId,
      started: Date.now(), abort: new AbortController(), snapshot: null };
    olderPrefetch.entry = entry;
    olderPrefetch.attemptedBefore = entry.before;
    entry.promise = readSessionSnapshot(`sessions/${enc(SID)}/snapshot`, { signal: entry.abort.signal,
      query: { recent: true, utterance_limit: 100, before_utterance_id: entry.before } })
      .then((snap) => {
        if (!currentOlderPrefetch(entry, history.nextBefore)) {
          if (olderPrefetch.entry === entry) olderPrefetch.entry = null;
          return null;
        }
        entry.received = Date.now();
        entry.snapshot = snap;
        return snap;
      }).catch(() => {
        if (olderPrefetch.entry === entry) olderPrefetch.entry = null;
        return null; // 준비 실패는 버튼의 명시 조회를 막지 않는다.
      });
  }, delay);
}

async function olderHistorySnapshot(before) {
  const entry = olderPrefetch.entry;
  if (currentOlderPrefetch(entry, before)) {
    const snap = entry.snapshot || await entry.promise;
    if (snap && currentOlderPrefetch(entry, before)) return snap;
  }
  invalidateHistoryPrefetch();
  return readSessionSnapshot(`sessions/${enc(SID)}/snapshot`, {
    query: { recent: true, utterance_limit: 100, before_utterance_id: before } });
}

function queueHistoryCorrections(ids, { first = false } = {}) {
  const view = window.gukgamTranscriptStages;
  // 이미 검증·로딩된 파생 문서도 발견하되 원행 소속으로 새 질의를 추측하지 않는다.
  const candidates = [...ids, ...(view?.closedBlockIds?.() || [])].filter(Number.isSafeInteger)
    .filter((id) => { const block = S.blocks.get(id); return block?.kind === 'question' && !block.is_open; });
  recentCorrections.ids = [...new Set(first ? [...candidates, ...recentCorrections.ids]
    : [...recentCorrections.ids, ...candidates])];
  queueRecentCorrections();
}

function queueRecentCorrections(delay = 200) {
  if (recentCorrections.timer !== null || recentCorrections.running || recentCorrections.stopped
      || document.hidden || !recentCorrections.ids.length) return;
  recentCorrections.timer = setTimeout(() => {
    recentCorrections.timer = null;
    hydrateRecentCorrection();
  }, delay);
}

function seedRecentCorrections(snap) {
  if (!snap.utterance_page?.partial || S.session?.source_type !== 'assembly_caption') return;
  // 첫 원행의 질의와 이미 검증된 파생 판본만 고정한다. 경계 원행으로 범위를 넓히지 않는다.
  recentCorrections.ids = [...new Set([...(snap.utterances || []).map((row) => row.block_id),
    ...(window.gukgamTranscriptStages?.closedBlockIds?.() || [])])]
    .filter((id) => {
      const block = S.blocks.get(id);
      return block?.kind === 'question' && !block.is_open;
    }).sort((a, b) => (S.blocks.get(b)?.ts_start || 0) - (S.blocks.get(a)?.ts_start || 0) || b - a);
  queueRecentCorrections();
  queueOlderHistoryPrefetch();
}

async function hydrateRecentCorrection() {
  const view = window.gukgamTranscriptStages;
  if (recentCorrections.running || recentCorrections.stopped || document.hidden || !recentCorrections.ids.length) return;
  // 사용자 요청과 첫 최신 판본 조회가 먼저다. 읽기 큐에 여러 배경 요청을 미리 넣지 않는다.
  if (!V.booted || V.tab !== 'feed' || filtering() || history.loading || history.foreground
      || !view?.loaded || view.loading || view.stopped || view.blockReads?.size) {
    queueRecentCorrections(300);
    return;
  }
  queueOlderHistoryPrefetch(0);
  if (olderPrefetch.timer !== null || olderPrefetch.entry && !olderPrefetch.entry.snapshot) {
    queueRecentCorrections(300);
    return;
  }
  let id;
  while (recentCorrections.ids.length) {
    const candidate = recentCorrections.ids.shift(), block = S.blocks.get(candidate);
    const doc = view.store.latestDocument(candidate);
    if (!block || block.is_open || block.review_status === 'reviewed' || blockSummary(block)?.edited_by
        || doc && view.store.documentCurrent(doc)) continue;
    id = candidate;
    break;
  }
  if (id === undefined) { queueOlderHistoryPrefetch(); return; }
  recentCorrections.running = true;
  try { await view.loadBlocks([id]); }
  catch { /* 배경 보완 실패는 현재 원문을 유지한다. 명시적 질의 선택은 다시 조회할 수 있다. */ }
  finally {
    recentCorrections.running = false;
    // 이번 하나가 끝난 뒤 이벤트·사용자 요청에 실행 기회를 먼저 준다.
    queueRecentCorrections(200);
    queueOlderHistoryPrefetch();
  }
}

function historyControls() {
  const button = $('#feed-older');
  if (!button) return;
  button.hidden = !history.partial || history.nextBefore === null;
  button.disabled = history.loading;
  button.textContent = history.loading ? '이전 기록을 불러오는 중…' : '이전 기록 더 보기';
  const all = $('#feed-all');
  if (!all || ctx.cloud) return;
  all.hidden = !history.fullRequest && !history.allLoaded && !history.partial;
  all.disabled = history.loading;
  all.textContent = history.fullRequest ? '전체 기록을 불러오는 중…'
    : history.allLoaded ? '맨 처음으로' : '처음부터 전체 기록 열기';
  $('#feed-all-cancel').hidden = !history.fullRequest;
  const status = $('#feed-all-status');
  status.hidden = !history.fullRequest && !history.allLoaded;
  const loose = window.gukgamTranscriptStages?.displayLooseRows?.().length || 0;
  status.textContent = history.fullRequest ? '모든 발언과 저장된 보정본문·요약을 불러옵니다.'
    : `전체 ${fmt.num(S.utts.size)}개 발언${loose ? `과 개별 원자막 ${fmt.num(loose)}개` : ''}을 열었습니다.`;
}

function cancelFullHistory() {
  history.fullRequest?.abort.abort();
}

function firstHistoryRow() {
  const first = visibleRowsInDom()[0];
  if (first) scrollToRow(first, { center: false });
}

function fullHistoryBlockSignature(block) {
  return JSON.stringify(['kind', 'questioner', 'questioner_party', 'status', 'review_status', 'reviewed_by',
    'reviewed_at', 'ts_start', 'ts_end', 'summary', 'summary_at'].map((field) => block?.[field] ?? null)
    .concat([!!block?.is_open]));
}

function fullHistoryConflicts(request, snap) {
  if (request.revision === history.revision) return false;
  const incomingRows = new Map(snap.utterances.map((row) => [row.id, row]));
  const incomingBlocks = new Map((snap.blocks || []).map((block) => [block.id, block]));
  // 첫 화면의 배경 조회가 같은 원행을 추가한 것은 자료 변경이 아니다. 실제 바뀐 입력만 대조한다.
  for (const id of new Set([...request.rows.keys(), ...S.utts.keys()])) {
    const current = S.utts.get(id), signature = current ? aiSourceSignature(current) : null;
    if (request.rows.get(id) === signature || !current && !request.rows.has(id)) continue;
    const incoming = incomingRows.get(id);
    if (!current || !incoming || signature !== aiSourceSignature(incoming)) return true;
  }
  for (const id of new Set([...request.blocks.keys(), ...S.blocks.keys()])) {
    const current = S.blocks.get(id), signature = current ? fullHistoryBlockSignature(current) : null;
    if (request.blocks.get(id) === signature || !current && !request.blocks.has(id)) continue;
    const incoming = incomingBlocks.get(id);
    if (!current || !incoming || signature !== fullHistoryBlockSignature(incoming)) return true;
  }
  return false;
}

async function loadAllHistory() {
  if (ctx.cloud || history.loading) return;
  setAutoscroll(false);
  if (history.allLoaded) { firstHistoryRow(); return; }
  invalidateHistoryPrefetch();
  const request = { abort: new AbortController(), revision: history.revision, eventId: S.lastEventId,
    navigation: displayNavigation, interaction: viewInteraction, filter: history.filterGeneration,
    tab: V.tab, scrollY: window.scrollY, focus: document.activeElement,
    rows: new Map([...S.utts].map(([id, row]) => [id, aiSourceSignature(row)])),
    blocks: new Map([...S.blocks].map(([id, block]) => [id, fullHistoryBlockSignature(block)])) };
  history.fullRequest = request; history.loading = true;
  history.foreground = (history.foreground || 0) + 1;
  historyControls();
  try {
    // 저장된 공통 표시본을 한 번 읽는다. 이전 100개 조회나 보정·요약 모델을 반복 실행하지 않는다.
    const snap = await readSessionSnapshot(`sessions/${enc(SID)}/snapshot`, {
      query: { recent: false }, signal: request.abort.signal });
    if (request.abort.signal.aborted || history.fullRequest !== request) return;
    if (snap.utterance_page?.partial || !Array.isArray(snap.utterances)) throw Error('전체 기록 응답이 완전하지 않습니다. 다시 시도해 주세요.');
    if (request.eventId !== S.lastEventId || fullHistoryConflicts(request, snap)
        || Object.hasOwn(snap, 'display_blocks') && window.gukgamTranscriptStages?.acceptsDisplayPayload?.(snap) === false)
      throw Error('조회 중 기록이 갱신되었습니다. 최신 내용을 보존했으니 전체 기록을 다시 열어 주세요.');
    // 눌렀던 버튼을 비활성화하면 브라우저가 포커스를 body로 옮긴다. 이 내부 변화는 이동 취소가 아니다.
    const sameFocus = request.focus === document.activeElement
      || request.focus === $('#feed-all') && document.activeElement === document.body;
    const goFirst = !V.autoscroll && request.navigation === displayNavigation && request.interaction === viewInteraction
      && request.filter === history.filterGeneration && request.tab === V.tab && request.scrollY === window.scrollY
      && sameFocus;
    mergeSnapshotRows({ ...snap, valid_utterance_ids: snap.utterances.map((row) => row.id) });
    // 같은 저장 표시본을 즉시 그려 원문 위치로 갔다가 보정 위치로 옮겨지는 일을 막는다.
    notifyLegacyStages({ synchronous: true, utteranceIds: snap.utterances.map((row) => row.id) });
    history.partial = false; history.nextBefore = null; history.allLoaded = true;
    recentCorrections.ids = [];
    if (goFirst && V.tab === 'feed') firstHistoryRow();
    announce(`전체 ${fmt.num(S.utts.size)}개 발언을 열었습니다.`);
  } catch (error) {
    if (!request.abort.signal.aborted && error.name !== 'AbortError')
      toast(errMsg(error), { type: 'err', title: '전체 기록 조회 실패' });
  } finally {
    if (history.fullRequest === request) { history.fullRequest = null; history.loading = false; }
    history.foreground -= 1;
    historyControls(); queueRecentCorrections(); queueOlderHistoryPrefetch();
  }
}

async function readSessionSnapshot(path, options = {}) {
  const controller = new AbortController(), cancel = () => controller.abort();
  const abortError = () => { const error = new Error('조회 취소'); error.name = 'AbortError'; return error; };
  let deadline = 0, deadlineTimer = 0, expired = false, pending = null;
  if (options.signal?.aborted) controller.abort();
  options.signal?.addEventListener('abort', cancel, { once: true });
  window.addEventListener('pagehide', cancel, { once: true });
  try {
    for (let attempt = 0; ; attempt++) {
      if (controller.signal.aborted) throw abortError();
      try {
        const response = await api(path, { ...options, signal: controller.signal });
        if (controller.signal.aborted) throw abortError();
        return response;
      }
      catch (error) {
        window.gukgamTranscriptStages?.displayPending?.(error);
        // 초기 저장 작업의 짧은 대기만 자동 재조회한다. 다른 오류는 그대로 보여 준다.
        if (error.status !== 503 || error.data?.display_state !== 'updating') throw error;
        pending = error;
        if (!deadline) {
          deadline = Date.now() + 10000;
          deadlineTimer = setTimeout(() => { expired = true; controller.abort(); }, 10000);
        }
        if (attempt >= 20 || Date.now() >= deadline) throw error;
        await new Promise((resolve, reject) => {
          let timer;
          const stop = () => { clearTimeout(timer); reject(abortError()); };
          controller.signal.addEventListener('abort', stop, { once: true });
          timer = setTimeout(() => { controller.signal.removeEventListener('abort', stop); resolve(); },
            Math.min(attempt ? 500 : 250, Math.max(0, deadline - Date.now())));
          if (controller.signal.aborted) stop();
        });
      }
    }
  } catch (error) {
    if (expired && pending) throw pending;
    throw error;
  } finally {
    clearTimeout(deadlineTimer);
    options.signal?.removeEventListener('abort', cancel);
    window.removeEventListener?.('pagehide', cancel);
  }
}

function mergeSnapshotRows(snap) {
  if (Object.hasOwn(snap || {}, 'display_blocks')
      && window.gukgamTranscriptStages?.acceptsDisplayPayload?.(snap) === false) return [];
  // 새 전체 유효ID·원문 판본을 받은 뒤에는 예전 사전 응답을 다시 병합하지 않는다.
  invalidateHistoryPrefetch();
  const page = snap.utterance_page;
  const anchor = V.booted && !V.autoscroll ? visibleRowsInDom()
    .find((row) => row.getBoundingClientRect().bottom > 80) : null;
  const top = anchor?.getBoundingClientRect().top;
  const stored = Object.hasOwn(snap, 'display_blocks') || snap.display_closure === true;
  for (const b of snap.blocks || []) if (window.gukgamTranscriptStages?.acceptsDisplayBlock?.(snap, b) !== false)
    applyBlock(b, { silent: true, stageNotify: !stored && page?.scope !== 'stage' });
  for (const u of snap.utterances || []) if (window.gukgamTranscriptStages?.acceptsDisplayUtterance?.(snap, u) !== false)
    applyUtt(u, { stageNotify: !stored && page?.scope !== 'stage' });
  if (Array.isArray(snap.valid_utterance_ids)) {
    const valid = new Set(snap.valid_utterance_ids);
    for (const id of [...S.utts.keys()]) if (!valid.has(id)) removeUtt(id);
  }
  if (page?.partial && ['tail', 'before'].includes(page.scope)) {
    history.partial = true;
    if (page.scope === 'before' || history.nextBefore === null) history.nextBefore = page.next_before ?? null;
  }
  window.gukgamTranscriptStages?.ingestDisplay?.(snap);
  for (const g of dirtyGroups) refreshGroupVisibility(g);
  dirtyGroups.clear();
  if (V.booted) { renderDirty(); updateFeedEmpty(); }
  historyControls();
  if (anchor?.isConnected && Number.isFinite(top)) {
    const delta = anchor.getBoundingClientRect().top - top;
    if (Math.abs(delta) > 1) { markProgrammatic(); window.scrollBy(0, delta); }
  }
  return [...new Set((snap.utterances || []).map((row) => row.block_id).filter(Number.isSafeInteger))];
}
window.gukgamTranscriptMergeSnapshot = mergeSnapshotRows;

async function loadHistory(query = {}, { hydrate = true, backgroundHydrate = false, snapshot = null } = {}) {
  history.foreground = (history.foreground || 0) + 1;
  try {
    const snap = await (snapshot || readSessionSnapshot(`sessions/${enc(SID)}/snapshot`, { query: { recent: true, utterance_limit: 100, ...query } }));
    const ids = mergeSnapshotRows(snap);
    if (hydrate && S.session?.source_type === 'assembly_caption') await window.gukgamTranscriptStages?.loadBlocks?.(ids);
    else if (backgroundHydrate && S.session?.source_type === 'assembly_caption') queueHistoryCorrections(ids, { first: true });
    return snap;
  } finally {
    history.foreground -= 1;
    queueRecentCorrections();
    queueOlderHistoryPrefetch();
  }
}

async function loadHistoryBlock(bid) {
  if (!Number.isSafeInteger(bid) || bid <= 0) return;
  if (!history.blocks.has(bid)) {
    const request = loadHistory({ block_id: bid }).catch((error) => { history.blocks.delete(bid); throw error; });
    history.blocks.set(bid, request);
  }
  return history.blocks.get(bid);
}
window.gukgamTranscriptLoadBlock = loadHistoryBlock;

async function loadOlderHistory() {
  if (history.loading || history.nextBefore === null) return;
  history.loading = true;
  setAutoscroll(false);
  historyControls();
  const before = history.nextBefore;
  try { await loadHistory({ before_utterance_id: before }, {
    hydrate: false, backgroundHydrate: true, snapshot: olderHistorySnapshot(before) }); }
  catch (error) { toast(errMsg(error), { type: 'err', title: '이전 기록 조회 실패' }); }
  finally { history.loading = false; historyControls(); queueOlderHistoryPrefetch(); }
}

async function loadBlockSummaries() {
  if (!history.partial || history.summaryRequest) return history.summaryRequest;
  history.summaryRequest = (async () => {
    try {
      const snap = await readSessionSnapshot(`sessions/${enc(SID)}/snapshot`, {
        query: { recent: true, utterance_limit: 1, block_summaries: true } });
      for (const block of snap.blocks || []) applyBlock(block, { silent: true });
      summaryStale = true;
      if (V.tab === 'summary') renderSummaryTab();
    } catch (error) { toast(errMsg(error), { type: 'err', title: '요약 조회 실패' }); }
    finally { history.summaryRequest = null; }
  })();
  return history.summaryRequest;
}

/* ── 정렬·그룹 ───────────────────────────────────────────── */
const cmpUtt = (a, b) => (a.ts_start - b.ts_start)
  || String(a.source_order || String(a.seq).padStart(12, '0')).localeCompare(String(b.source_order || String(b.seq).padStart(12, '0')))
  || (a.seq - b.seq) || (a.id - b.id);
const cmpGroup = (a, b) => (a.start - b.start) || (a.tie - b.tie) || (a.id - b.id);

function orderInsert(u) {
  const n = order.length;
  if (!n || cmpUtt(S.utts.get(order[n - 1]), u) <= 0) { order.push(u.id); return; }
  let lo = 0, hi = n;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (cmpUtt(S.utts.get(order[mid]), u) <= 0) lo = mid + 1; else hi = mid;
  }
  order.splice(lo, 0, u.id);
}
function orderRemove(id) {
  const i = order.lastIndexOf(id);
  if (i >= 0) order.splice(i, 1);
}

function insertGroup(g) {
  const n = groups.length;
  if (!n || cmpGroup(groups[n - 1], g) <= 0) {
    groups.push(g);
    feed.append(g.el);
    return;
  }
  let lo = 0, hi = n;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (cmpGroup(groups[mid], g) <= 0) lo = mid + 1; else hi = mid;
  }
  feed.insertBefore(g.el, groups[lo].el);
  groups.splice(lo, 0, g);
}
function removeGroup(g) {
  const i = groups.indexOf(g);
  if (i >= 0) groups.splice(i, 1);
  g.el.remove();
  groupMap.delete(g.key);
  dirtyGroups.delete(g);
}
function repositionGroup(g) {
  const i = groups.indexOf(g);
  const okPrev = i <= 0 || cmpGroup(groups[i - 1], g) <= 0;
  const okNext = i < 0 || i >= groups.length - 1 || cmpGroup(g, groups[i + 1]) <= 0;
  if (i >= 0 && okPrev && okNext) return;
  if (i >= 0) groups.splice(i, 1);
  insertGroup(g);
}

function ensureBlockGroup(bid, startHint) {
  const key = `b${bid}`;
  let g = groupMap.get(key);
  if (g) return g;
  const b = S.blocks.get(bid);
  const head = h('div', { class: 'blk-h' });
  const sum = h('div', { class: 'blk-sumwrap' });
  const ai = h('div', { class: 'ai-review-panel', hidden: true });
  const rows = h('ol', { class: 'rows' });
  const el = h('section', { class: 'blk', id: `b-${bid}`, dataset: { bid: String(bid) }, 'aria-label': b ? b.title : '질의 블록' },
    head, h('div', { class: 'blk-body' }, sum, ai, rows));
  el.hidden = true;
  g = { key, id: bid, type: 'b', start: b ? b.ts_start : (startHint ?? Number.MAX_SAFE_INTEGER), tie: 0, el, head, sum, ai, rows };
  groupMap.set(key, g);
  insertGroup(g);
  renderBlock(g);
  return g;
}

function ensureSoloGroup(u) {
  const key = `s${u.id}`;
  let g = groupMap.get(key);
  if (g) return g;
  const rows = h('ol', { class: 'rows' });
  const el = h('section', { class: 'blk solo' }, rows);
  g = { key, id: u.id, type: 's', start: u.ts_start, tie: 1, el, rows, head: null, sum: null };
  groupMap.set(key, g);
  insertGroup(g);
  return g;
}

function insertRowSorted(ol, li, u) {
  const kids = ol.children;
  const n = kids.length;
  if (!n || cmpUtt(S.utts.get(Number(kids[n - 1].dataset.uid)), u) <= 0) { ol.append(li); return; }
  let lo = 0, hi = n;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    const mu = S.utts.get(Number(kids[mid].dataset.uid));
    if (mu && cmpUtt(mu, u) <= 0) lo = mid + 1; else hi = mid;
  }
  ol.insertBefore(li, kids[lo] || null);
}

function placeRow(u, li, moved) {
  const target = u.block_id ? `b${u.block_id}` : `s${u.id}`;
  if (li._gkey === target && li.isConnected && !moved) return;
  const old = li._gkey ? groupMap.get(li._gkey) : null;
  li.remove();
  if (old) {
    if (old.type === 's' && old.key !== target) removeGroup(old);
    else dirtyGroups.add(old);
  }
  const g = u.block_id ? ensureBlockGroup(u.block_id, u.ts_start) : ensureSoloGroup(u);
  if (g.type === 's' && g.start !== u.ts_start) { g.start = u.ts_start; repositionGroup(g); }
  insertRowSorted(g.rows, li, u);
  li._gkey = target;
  dirtyGroups.add(g);
}

function refreshGroupVisibility(g) {
  if (g.type === 'b') syncLiveAiRows(g);
  if (g.stageProjection && g.ai?.children[0]) {
    [...g.ai.children[0].children].forEach((li, index) => {
      const card = g.stageProjection.cards[index];
      li.hidden = !card || !stageCardMatches(card, S.blocks.get(g.id));
    });
  }
  let vis = false;
  for (const li of g.rows.children) { if (!li.hidden) { vis = true; break; } }
  if (g.stageProjection && !aiOriginalChoice.get(g.id)) {
    vis = vis || g.stageProjection.cards.some((card) => stageCardMatches(card, S.blocks.get(g.id)));
  }
  g.el.hidden = !vis;
  refreshReviewGrouping(g);
  if (g.type === 'b') syncAiVisibility(g);
}

const reviewRoleName = (part) => part.speaker || { member: '위원', answer: '답변', chair: '위원장', unknown: '미상' }[part.role] || '미상';

function displayReviewParts(u) {
  const raw = Array.from(String(u.text || ''));
  const parts = Array.isArray(u.review_parts) ? u.review_parts : [];
  const out = [];
  let cursor = 0;
  for (const part of parts) {
    if (!part || !Number.isInteger(part.start) || !Number.isInteger(part.end) || part.start !== cursor
        || part.end <= part.start || part.end > raw.length) return [];
    const copy = { ...part, text: raw.slice(part.start, part.end).join('') };
    const prev = out[out.length - 1];
    if (prev && sameReviewGroup(prev, copy)) {
      prev.end = copy.end; prev.text += copy.text;
    } else out.push(copy);
    cursor = part.end;
  }
  return cursor === raw.length ? out : [];
}

function blockLiveAiReview(b) {
  const ai = b?.ai_live_review;
  if (!b || !b.is_open || b.kind !== 'question' || b.review_status === 'reviewed' || blockSummary(b)?.edited_by
      || ai?.status !== 'draft' || ai.schema_version !== 1 || !Array.isArray(ai.windows)) return null;
  const used = new Set();
  const windows = [];
  for (const window of ai.windows) {
    if (!window || !['string', 'number'].includes(typeof window.id) || !String(window.id).length
        || typeof window.input_hash !== 'string' || !window.input_hash.length
        || !Array.isArray(window.utterance_ids) || !window.utterance_ids.length || window.utterance_ids.length > 6
        || !Array.isArray(window.parts) || !window.parts.length
        || !Array.isArray(window.context_ids)
        || window.context_ids.length > ([2, 3].includes(window.context_radius) ? 2 * window.context_radius : window.context_correction?.stage === 'realtime_context' ? 4 : 2)
        || !Array.isArray(window.source_rows)) continue;
    const ids = new Set(window.utterance_ids);
    if (ids.size !== window.utterance_ids.length || [...ids].some((uid) => !Number.isSafeInteger(uid) || uid < 1 || used.has(uid))) continue;
    const context = new Set(window.context_ids);
    const sources = new Set([...ids, ...context]);
    if (context.size !== window.context_ids.length || [...context].some((uid) => !Number.isSafeInteger(uid) || uid < 1 || ids.has(uid))
        || window.source_rows.length !== sources.size || new Set(window.source_rows.map((u) => u?.id)).size !== sources.size
        || window.source_rows.some((copy) => {
          const u = S.utts.get(copy?.id);
          const readOnlyContext = [2, 3].includes(window.context_radius) && context.has(copy?.id);
          return !sources.has(copy?.id) || !u || (!readOnlyContext && u.block_id !== b.id) || !u.is_final
            || aiSourceSignature(copy) !== aiSourceSignature(u) || (!readOnlyContext && u.review_parts?.length);
        })) continue;
    let valid = true;
    const cursors = new Map();
    for (const uid of ids) {
      const u = S.utts.get(uid);
      if (!u || u.block_id !== b.id || !u.is_final || u.speaker_source === 'manual' || u.edited || u.edited_by
          || u.speaker_role === 'system' || u.review_parts?.length
          || (typeof u.original_text === 'string' && u.text !== u.original_text)) { valid = false; break; }
      cursors.set(uid, 0);
    }
    if (!valid) continue;
    for (const part of window.parts) {
      const raw = Array.from(String(S.utts.get(part?.utterance_id)?.text || ''));
      if (!part || !ids.has(part.utterance_id) || !Number.isInteger(part.start) || !Number.isInteger(part.end)
          || part.start !== cursors.get(part.utterance_id) || part.end <= part.start || part.end > raw.length
          || typeof part.text !== 'string' || part.text !== raw.slice(part.start, part.end).join('')
          || !['member', 'answer', 'chair', 'unknown', 'other'].includes(part.role)
          || typeof part.speaker !== 'string') { valid = false; break; }
      cursors.set(part.utterance_id, part.end);
    }
    if (!valid || [...ids].some((uid) => cursors.get(uid) !== Array.from(String(S.utts.get(uid).text || '')).length)) continue;
    windows.push(window);
    for (const uid of ids) used.add(uid);
  }
  return windows.length ? { ...ai, windows } : null;
}

function displayLiveReviewParts(u) {
  if (globalThis.window.gukgamTranscriptStages?.usesStoredDisplay?.() === true) return [];
  if (aiOriginalChoice.get(u.block_id) || filtering()) return [];
  const ai = blockLiveAiReview(S.blocks.get(u.block_id));
  const window = ai?.windows.find((item) => item.utterance_ids.includes(u.id));
  if (!window) return [];
  const out = [];
  for (const part of window.parts.filter((item) => item.utterance_id === u.id)) {
    // 서로 다른 검토 창의 우연히 같은 그룹 이름은 이어 붙이지 않는다.
    const copy = { ...part, group_id: part.group_id ? JSON.stringify(['live', window.id, window.input_hash, part.group_id]) : '' };
    const prev = out[out.length - 1];
    if (prev && sameReviewGroup(prev, copy)) { prev.end = copy.end; prev.text += copy.text; }
    else out.push(copy);
  }
  return contextCorrectedParts(window, u, out);
}

function contextCorrectedParts(window, u, parts) {
  const correction = window.context_correction;
  if (!correction || correction.stage !== 'realtime_context' || correction.preview_only !== true
      || !['draft', 'unchanged'].includes(correction.status) || correction.source_hash !== window.input_hash) return parts;
  const row = correction.rows?.find(row => row?.id === u.id);
  if (!row || row.original_text !== u.text || typeof row.text !== 'string' || !Array.isArray(row.changes)) return parts;
  const chars = Array.from(u.text), changes = row.changes;
  let cursor = 0, proposed = '';
  for (const change of changes) {
    if (!change || !Number.isSafeInteger(change.start) || !Number.isSafeInteger(change.end)
        || change.start < cursor || change.end <= change.start || change.end > chars.length
        || typeof change.after !== 'string' || chars.slice(change.start, change.end).join('') !== change.before
        || !parts.some(part => change.start >= part.start && change.end <= part.end)) return parts;
    proposed += chars.slice(cursor, change.start).join('') + change.after;
    cursor = change.end;
  }
  proposed += chars.slice(cursor).join('');
  if (proposed !== row.text) return parts;
  return parts.map(part => {
    let start = part.start, text = '';
    for (const change of changes.filter(change => change.start >= part.start && change.end <= part.end)) {
      text += chars.slice(start, change.start).join('') + change.after;
      start = change.end;
    }
    return { ...part, text: text + chars.slice(start, part.end).join(''), context_draft: true, context_radius: window.context_radius || 2 };
  });
}

function syncLiveAiRows(g) {
  if (g.type !== 'b') return;
  if (window.gukgamTranscriptStages?.usesStoredDisplay?.() === true) return;
  const ai = !aiOriginalChoice.get(g.id) && !filtering() ? blockLiveAiReview(S.blocks.get(g.id)) : null;
  const stamp = JSON.stringify(ai?.windows || []);
  if (g.liveAiStamp === stamp) return;
  const ids = new Set([...(g.liveAiIds || []), ...(ai?.windows.flatMap((window) => window.utterance_ids) || [])]);
  g.liveAiStamp = stamp;
  g.liveAiIds = new Set(ai?.windows.flatMap((window) => window.utterance_ids) || []);
  for (const uid of ids) {
    const u = S.utts.get(uid);
    if (u?.block_id === g.id && rowEl.has(uid)) applyFilterRow(renderRow(u), u);
  }
}

function sameReviewGroup(a, b) {
  return !!a && !!b && typeof a.group_id === 'string' && a.group_id.length > 0
    && a.group_id === b.group_id && a.role === b.role && (a.speaker || '') === (b.speaker || '');
}

function refreshReviewGrouping(g) {
  // UID의 DOM·메뉴·원문은 유지한다. 필터로 제외한 행을 넘어 본문을 이어 보이지 않는다.
  let previous = null;
  for (const li of g.rows.children) {
    li.classList.remove('review-linked', 'review-connect-next', 'review-continuation', 'review-continued-only');
    for (const node of li._reviewPartNodes || []) node.classList.remove('review-part-follow');
    const u = S.utts.get(Number(li.dataset.uid));
    const parts = li._reviewParts || [];
    if (li.hidden || !u || !parts.length || u.speaker_source === 'manual' || u.edited || u.speaker_role === 'system') {
      previous = null; continue;
    }
    if (previous && previous.u.block_id === u.block_id
        && sameReviewGroup(previous.parts[previous.parts.length - 1], parts[0])) {
      previous.li.classList.add('review-linked', 'review-connect-next');
      li.classList.add('review-linked', 'review-continuation');
      li.classList.toggle('review-continued-only', parts.length === 1);
      li._reviewPartNodes?.[0]?.classList.add('review-part-follow');
    }
    previous = { li, u, parts };
  }
}

/* ── 발언 행 ─────────────────────────────────────────────── */
function rowClass(u) {
  let c = `u r-${u.speaker_role || 'unknown'}`;
  if (!u.is_final) c += ' partial';
  if (V.focusId === u.id) c += ' focus';
  if (V.emph) c += speakerLabel(u) === V.emph || u.speaker === V.emph ? ' emph' : '';
  return c;
}

const SRC_TITLE = { feed: '수집원이 준 화자', inferred: 'AI 추정 화자', manual: '운영자가 지정한 화자' };

function itemChips(uid) {
  const set = itemsByUtt.get(uid);
  if (!set || !set.size) return null;
  const chips = [];
  for (const iid of set) {
    const it = S.items.get(iid);
    if (!it || it.status === 'dismissed') continue;
    const k = ITEM_KINDS[it.kind] || { short: it.kind, icon: '' };
    chips.push(h('button', {
      type: 'button', class: `chip k-${it.kind}${it.strength === '의례' ? ' weak' : ''}`,
      dataset: { act: 'item', iid: String(it.id) }, title: `${it.kind_label || k.short}: ${it.text}`,
    }, `${k.icon} ${k.short}${it.strength ? `·${it.strength}` : ''}`));
  }
  return chips.length ? h('span', { class: 'u-flags' }, chips) : null;
}

function renderRow(u) {
  let li = rowEl.get(u.id);
  if (!li) {
    li = h('li', { id: `u-${u.id}`, tabindex: '-1', dataset: { uid: String(u.id) } });
    rowEl.set(u.id, li);
  }
  const stored = !originalDisplayChoice(u) ? window.gukgamTranscriptStages?.displayUtterance?.(u.id) : null;
  const storedParts = stored?.length > 1 ? stored.map((row) => ({ text: row.text, speaker: row.speaker,
    role: row.speaker_role })) : [];
  const adoptedParts = stored?.length ? storedParts : displayReviewParts(u);
  const liveParts = adoptedParts.length ? [] : displayLiveReviewParts(u);
  const parts = adoptedParts.length ? adoptedParts : liveParts;
  const liveDraft = liveParts.length > 0;
  li._liveAiDraft = liveDraft;
  li._reviewParts = parts;
  li._reviewPartNodes = [];
  const display = stored?.length === 1 ? { ...u, speaker: stored[0].speaker, speaker_role: stored[0].speaker_role }
    : parts.length === 1 ? { ...u, speaker: parts[0].speaker, speaker_role: parts[0].role }
    : parts.length > 1 ? { ...u, speaker: '여러 화자', speaker_role: 'unknown' } : u;
  li.className = rowClass(display) + (liveDraft ? ' live-ai-draft' : '');
  const text = normText(stageSourceText(u) ?? '');
  const label = speakerLabel(display);
  const isSys = u.speaker_role === 'system';
  const needsSpeakerReview = u.boundary_reason === 'uncertain_marker'
    || (u.is_final && (!parts.length ? u.speaker_role === 'unknown' : parts.some((part) => part.role === 'unknown')) && u.speaker_source !== 'manual');
  // 검색어 하이라이트를 입힌 행은 기록해 둔다(검색을 지우거나 바꿀 때 다시 그려 하이라이트를 걷어 내도록)
  if (!isSys && V.terms.length && termRanges(text, V.terms).length) markedRows.add(u.id);
  else markedRows.delete(u.id);
  const spk = h('div', { class: 'u-spk' },
    h('button', { type: 'button', dataset: { act: 'speaker', speaker: label }, title: `${SRC_TITLE[u.speaker_source] || ''} — 누르면 이 화자 발언만 봅니다` }, label),
    display.speaker_role === 'answer' && label !== '답변' ? h('span', { class: 'role' }, '답변') : null,
    stored?.length === 1 && stored[0].estimated === true ? h('span', { class: 'role' }, '추정') : null,
    !u.is_final ? h('span', { class: 'role' }, '수신 중') : null,
    u.turn_id && ['limit_chars', 'limit_time'].includes(u.boundary_reason) ? h('span', { class: 'role' }, '이어지는 발언') : null,
    needsSpeakerReview ? h('span', { class: 'role review-uncertain' }, '화자 확인 필요') : null,
    parts.length ? h('span', { class: `role${liveDraft ? ' live-ai-label' : ''}` }, stored?.length ? '실시간 화자 추정' : liveDraft
      ? parts.some(part => part.context_draft) ? `앞뒤 ${parts.find(part => part.context_draft).context_radius}발언 · 1차 보정 초안` : '실시간 AI 초안' : '화자 보정 채택') : null,
    parts.length ? h('a', { class: 'review-source', href: `#u-${u.id}`, dataset: { act: 'goto', uid: String(u.id) },
      title: `원문 #${u.id} · 이 행의 메뉴에서 수정·북마크` }, `원문 #${u.id}`) : null);
  const reviewedText = parts.length ? h('div', { class: 'review-turns' }, parts.map((part) => {
    const node = h('p', { class: 'review-part' },
      parts.length > 1 ? h('b', { class: 'review-part-label' }, `${reviewRoleName(part)}: `) : null,
      markedText(part.text, termRanges(part.text, V.terms), mentionRanges(part.text, u.mentions)));
    li._reviewPartNodes.push(node);
    return node;
  }),
    h('details', null, h('summary', { class: 'small muted' }, '원문 표시'), h('p', null, u.text))) : null;
  const body = h('div', { class: 'u-text' },
    S.bookmarks.has(u.id) ? h('span', { class: 'bm', title: '북마크' }, '★') : null,
    reviewedText || (isSys ? `— ${text.replace(/^[—–\-\s]+|[—–\-\s]+$/g, '')} —` : markedText(text, termRanges(text, V.terms), mentionRanges(text, u.mentions))),
    u.edited ? h('span', { class: 'edited', title: '운영자가 본문을 고쳤습니다' }, '(수정됨)') : null,
    itemChips(u.id));
  setKids(li, 
    h('div', { class: 'u-time' }, h('a', { href: `#u-${u.id}`, tabindex: '-1', title: '이 발언으로 이동(링크)' }, u.time || fmt.hms(u.ts_start))),
    spk,
    body,
    h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs u-more', dataset: { act: 'menu' }, 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-label': `${label} ${u.time || fmt.hms(u.ts_start)} 발언 메뉴` }, icon('more', 'ic-sm')));
  return li;
}

function rerenderRow(uid) {
  const u = S.utts.get(uid);
  if (!u) return;
  const li = renderRow(u);
  applyFilterRow(li, u);
  const g = li._gkey ? groupMap.get(li._gkey) : null;
  if (g) {
    if (g.stageProjection) renderStageBlock(g, S.blocks.get(g.id), g.stageProjection);
    dirtyGroups.add(g);
  }
  for (const owner of groups) if (owner !== g && owner.stageProjection?.cards
    .some((card) => card.source_utterance_ids.includes(uid))) {
    renderStageBlock(owner, S.blocks.get(owner.id), owner.stageProjection);
  }
}

/* ── 블록 헤더·요약 ───────────────────────────────────────── */
function blockAiReview(b) {
  const ai = b?.ai_review;
  if (!b || b.is_open || b.review_status === 'reviewed' || blockSummary(b)?.edited_by
      || ai?.status !== 'draft' || !Array.isArray(ai.cards) || !ai.cards.length
      || !ai.summary || typeof ai.summary !== 'object' || Array.isArray(ai.summary)) return null;
  if (ai.cards.some((card) => !card || typeof card.text !== 'string'
      || typeof card.readable_text !== 'string' || !Array.isArray(card.source_utterance_ids)
      || !card.source_utterance_ids.length || card.source_utterance_ids.some((uid) => !Number.isSafeInteger(uid) || uid < 1))) return null;
  if (ai.final_document && !blockFinalDocument(ai)) return null;
  return ai;
}

function blockFinalDocument(ai) {
  const document = ai?.final_document;
  const ids = new Set(ai?.cards?.flatMap(card => card.source_utterance_ids) || []);
  if (!document || document.preview_only !== true || !['draft', 'partial'].includes(document.status)
      || document.source_hash !== ai.input_hash || !Array.isArray(document.units) || !document.units.length
      || document.units.some(unit => !unit || typeof unit.text !== 'string' || typeof unit.speaker !== 'string'
        || !Array.isArray(unit.source_spans) || !unit.source_spans.length
        || unit.source_spans.some(span => !Number.isSafeInteger(span?.utterance_id) || !ids.has(span.utterance_id)))) return null;
  return document;
}

function shownAiReview(g, b = S.blocks.get(g.id)) {
  if (aiOriginalChoice.get(g.id)) return null;
  if (window.gukgamTranscriptStages?.usesStoredDisplay?.() === true) return g.stageProjection || null;
  return g.stageProjection || (!filtering() ? blockAiReview(b) : null);
}

function stageBlockReview(b) {
  if (window.gukgamTranscriptStages?.usesStoredDisplay?.() === true)
    return b ? window.gukgamTranscriptStages.closedBlock(b) : null;
  if (!b || b.is_open || b.review_status === 'reviewed' || blockSummary(b)?.edited_by) return null;
  const g = groupMap.get(`b${b.id}`);
  if (g && [...g.rows.children].some((li) => {
    const row = S.utts.get(Number(li.dataset.uid));
    return row && (row.edited || row.speaker_source === 'manual' || row.review_parts?.length);
  })) return null;
  return window.gukgamTranscriptStages?.closedBlock?.(b, S.utts) || null;
}

function stageCardMatches(card, block) {
  if (V.bmOnly && !card.source_utterance_ids.some((id) => S.bookmarks.has(id))) return false;
  if (V.speaker && ![card.speaker, speakerLabel({ speaker: card.speaker, speaker_role: card.speaker_role })].includes(V.speaker)) return false;
  if (V.questioner && block?.questioner !== V.questioner) return false;
  const hay = `${card.text} ${card.speaker || ''}`.toLowerCase();
  return V.terms.every((term) => hay.includes(term.toLowerCase()));
}

function renderStageBlock(g, b, projection) {
  g.stageProjection = projection;
  const stamp = JSON.stringify([projection.fingerprint, V.terms, V.bmOnly, V.speaker, V.questioner,
    projection.cards.map((card) => card.source_utterance_ids.filter((id) => S.bookmarks.has(id)))]);
  if (g.stageStamp !== stamp) {
    g.stageStamp = stamp;
    const rows = h('ol', { class: 'rows legacy-corrected' });
    for (const card of projection.cards) {
      const uid = card.source_utterance_ids[0], label = card.speaker || speakerLabel({ speaker_role: card.speaker_role });
      const body = h('div', { class: 'u-text' });
      const warnings = correctionNotices({ ...card, document_status: undefined });
      if (warnings.length) body.append(h('div', { class: 'chips', role: 'note' },
        ...warnings.map(label => badge(label, 'b-orange badge-sm'))));
      if (card.source_utterance_ids.some((id) => S.bookmarks.has(id))) body.append(h('span', { class: 'bm', title: '북마크' }, '★'));
      const chunks = window.gukgamTranscriptStages?.readingChunks?.(card.text) || [card.text];
      chunks.forEach((chunk, index) => {
        body.append(markedText(chunk, termRanges(chunk, V.terms), []));
        if (index < chunks.length - 1 && !/[\r\n]/.test(chunk.match(/\s*$/)?.[0] || '')) body.append(h('br'));
      });
      const li = h('li', { class: `u r-${card.speaker_role || 'unknown'}`, tabindex: '-1',
        dataset: { uid: String(uid), stageCard: card.key, tsStart: String(card.ts_start) }, hidden: !stageCardMatches(card, b) },
        h('div', { class: 'u-time' }, fmt.hms(card.ts_start)),
        h('div', { class: 'u-spk' }, h('button', { type: 'button', dataset: { act: 'speaker', speaker: label }, title: '이 화자 발언만 보기' }, label)),
        body,
        h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs u-more', dataset: { act: 'menu' },
          'aria-haspopup': 'menu', 'aria-label': `${label} 발언 메뉴` }, icon('more', 'ic-sm')));
      rows.append(li);
    }
    const warnings = correctionNotices({ document_status: projection.document?.status });
    const notice = warnings.length ? h('div', { class: 'chips', role: 'note' },
      ...warnings.map(label => badge(label, 'b-orange badge-sm'))) : null;
    setKids(g.ai, ...(notice ? [rows, notice] : [rows]));
  }
  syncAiVisibility(g);
}

function updateStageBlock(bid) {
  let g = groupMap.get(`b${bid}`);
  const anchor = V.tab === 'feed' && !V.autoscroll ? visibleRowsInDom()
    .find((row) => row.getBoundingClientRect().bottom > 80) : null;
  const top = anchor?.getBoundingClientRect().top;
  const restoreFocus = g && window.gukgamTranscriptStages?.preserveMemberFocus?.(g.el);
  const projection = stageBlockReview(S.blocks.get(bid));
  // 원발언 소속과 다른 파생 질의도 검증된 출처 연결이 있을 때만 별도 표시한다.
  if (!g && projection) {
    g = ensureBlockGroup(bid, S.blocks.get(bid)?.ts_start);
    g.stageOnly = true;
  }
  const previous = stageClaimsByBlock.get(bid) || new Map();
  const claims = new Map(!projection || aiOriginalChoice.get(bid) ? []
    : Object.entries(projection.consumed_utterance_ranges || {}).map(([id, ranges]) => [Number(id), ranges]));
  const affected = new Set([...previous.keys(), ...claims.keys()]);
  for (const id of previous.keys()) {
    const owners = stageClaimOwners.get(id); owners?.delete(bid);
    if (!owners?.size) stageClaimOwners.delete(id);
  }
  for (const [id, ranges] of claims) {
    if (!stageClaimOwners.has(id)) stageClaimOwners.set(id, new Map());
    stageClaimOwners.get(id).set(bid, ranges);
  }
  if (claims.size) stageClaimsByBlock.set(bid, claims);
  else stageClaimsByBlock.delete(bid);
  stageClaimsReady = true;
  const changedGroups = new Set(g ? [g] : []);
  for (const id of affected) {
    const old = stageRemainders.get(id), had = stageRemainders.has(id), u = S.utts.get(id);
    const ranges = [...(stageClaimOwners.get(id)?.values() || [])].flat();
    if (u && ranges.length) {
      const text = Array.from(u.text || '').filter((_, index) => !ranges.some((range) => range.start <= index && index < range.end)).join('');
      stageRemainders.set(id, text.trim() ? text : null);
    } else stageRemainders.delete(id);
    if (had !== stageRemainders.has(id) || old !== stageRemainders.get(id)) {
      rerenderRow(id);
      const owner = groupMap.get(rowEl.get(id)?._gkey); if (owner) changedGroups.add(owner);
    }
  }
  if (g?.stageOnly && !g.rows.children.length && !projection) {
    changedGroups.delete(g);
    removeGroup(g);
    g = null;
  }
  if (g) renderBlock(g);
  for (const owner of changedGroups) refreshGroupVisibility(owner);
  if (g?.stageOnly && projection && aiOriginalChoice.get(bid) && !filtering()) g.el.hidden = false;
  restoreFocus?.();
  visGroupsCache = null;
  if (filtering()) renderQCount();
  if (V.booted) renderCur();
  if (anchor?.isConnected && Number.isFinite(top)) {
    const delta = anchor.getBoundingClientRect().top - top;
    if (Math.abs(delta) > 1) { markProgrammatic(); window.scrollBy(0, delta); }
  }
  queueLegacyViewport();
}

function drainStageUpdates() {
  stageUpdateTimer = 0;
  const bid = stageUpdateQueue.values().next().value;
  if (bid === undefined) return;
  stageUpdateQueue.delete(bid);
  updateStageBlock(bid);
  // 지난 보정을 한 블록씩 반영해 다음 실황·스크롤·키 입력에 실행 기회를 넘긴다.
  if (stageUpdateQueue.size) stageUpdateTimer = setTimeout(drainStageUpdates, 0);
}

function notifyLegacyStages(change) {
  syncStoredLooseRows();
  for (const id of change?.utteranceIds || []) if (S.utts.has(id)) rerenderRow(id);
  if (!change || change.synchronous === true) {
    // 원문/보정판 수동 전환은 즉시 표시하고 예약된 중복 갱신을 걷어낸다.
    clearTimeout(stageUpdateTimer); stageUpdateTimer = 0; stageUpdateQueue.clear();
    const ids = new Set([...groups.filter((g) => g.type === 'b').map((g) => g.id),
      ...(window.gukgamTranscriptStages?.closedBlockIds?.() || [])]);
    for (const id of ids) updateStageBlock(id);
    return;
  }
  const ids = [...(change.invalidatedBlockIds || []), ...(change.blockIds || [])];
  for (const id of ids) if (Number.isSafeInteger(id)
      && (S.blocks.has(id) || groupMap.has(`b${id}`) || stageClaimsByBlock.has(id))) stageUpdateQueue.add(id);
  if (stageUpdateQueue.size && !stageUpdateTimer) stageUpdateTimer = setTimeout(drainStageUpdates, 0);
}
window.gukgamTranscriptLegacyUpdated = notifyLegacyStages;

function originalDisplayChoice(u) {
  return aiOriginalChoice.get(u.block_id) || (window.gukgamTranscriptStages?.displayOwners?.(u.id) || [])
    .some((id) => aiOriginalChoice.get(id));
}

function stageSourceText(u) {
  if (originalDisplayChoice(u)) return u.text;
  const stored = window.gukgamTranscriptStages?.displayUtterance?.(u.id);
  if (Array.isArray(stored)) return stored.length ? stored.map((row) => row.text).join('\n') : null;
  return stageRemainders.has(u.id) ? stageRemainders.get(u.id) : u.text;
}

function syncStoredLooseRows() {
  const rows = window.gukgamTranscriptStages?.displayLooseRows?.() || [], wanted = new Set(rows.map((row) => `display:${row.key}`));
  let changed = false;
  for (const g of [...groups]) if (g.type === 'display' && !wanted.has(g.key)) { removeGroup(g); changed = true; }
  for (const row of rows) {
    const key = `display:${row.key}`;
    let g = groupMap.get(key);
    if (!g) {
      const list = h('ol', { class: 'rows' }), el = h('section', { class: 'blk solo' }, list);
      g = { key, id: row.source_ids?.[0] || 0, type: 'display', start: row.ts_start || 0, tie: 2, el, rows: list };
      groupMap.set(key, g); insertGroup(g);
      g.displayTime = h('div', { class: 'u-time' }); g.displaySpeaker = h('div', { class: 'u-spk' });
      g.displayBody = h('div', { class: 'u-text' });
      g.displayLi = h('li', { class: 'u', tabindex: '-1', dataset: { displayRow: row.key } },
        g.displayTime, g.displaySpeaker, g.displayBody);
      list.append(g.displayLi);
    }
    const stamp = JSON.stringify([row, V.terms, V.speaker, V.questioner, V.bmOnly]);
    if (g.displayStamp !== stamp) {
      g.displayStamp = stamp; g.displayRow = row;
      g.displayLi.className = `u r-${row.speaker_role || 'unknown'}${row.is_final === false ? ' partial' : ''}`;
      g.displayTime.textContent = fmt.hms(row.ts_start);
      g.displaySpeaker.textContent = `${row.speaker || '발언'}${row.estimated === true ? ' (추정)' : ''}`;
      if (!V.terms.length && g.displayBody.firstChild?.nodeType === 3) g.displayBody.firstChild.data = row.text;
      else setKids(g.displayBody, markedText(row.text, termRanges(row.text, V.terms), []));
      changed = true;
    }
    g.rows.children[0].hidden = !stageCardMatches(row, S.blocks.get(row.block_id));
    refreshGroupVisibility(g);
  }
  if (changed) {
    visGroupsCache = null;
    if (V.booted) updateFeedEmpty();
    queueLegacyViewport();
  }
}

function syncAiVisibility(g) {
  if (!g.ai || !g.rows) return;
  syncLiveAiRows(g);
  const showing = !!shownAiReview(g);
  g.ai.hidden = !showing;
  const residual = g.stageProjection && stageClaimsReady && [...g.rows.children].some((li) => {
    const u = S.utts.get(Number(li.dataset.uid));
    return u && stageSourceText(u)?.trim() && (!filtering() || rowMatches(u));
  });
  g.rows.hidden = showing && !residual;
  if (g.aiShowing !== showing) {
    g.aiShowing = showing;
    renderBlockSummary(g, S.blocks.get(g.id));
  }
}

function setBlockOriginal(bid, original) {
  cancelPendingDisplayNavigation();
  aiOriginalChoice.set(bid, !!original);
  const g = groupMap.get(`b${bid}`);
  if (g) {
    for (const row of g.rows.children) rerenderRow(Number(row.dataset.uid));
    renderBlock(g);
  }
  stageUpdateQueue.delete(bid);
  updateStageBlock(bid);
}

function renderAiBlock(g, b) {
  if (!g.ai) return;
  const projection = stageBlockReview(b);
  if (projection) { renderStageBlock(g, b, projection); return; }
  if (window.gukgamTranscriptStages?.usesStoredDisplay?.() === true) {
    g.stageProjection = null; g.stageStamp = null; setKids(g.ai); syncAiVisibility(g); return;
  }
  g.stageProjection = null; g.stageStamp = null;
  const ai = blockAiReview(b);
  const editor = ai?.editor;
  const document = blockFinalDocument(ai);
  const cards = document ? document.units.map(unit => ({ ...unit, readable_text: unit.text, speaker_role: unit.role,
    source_utterance_ids: [...new Set(unit.source_spans.map(span => span.utterance_id))],
    ts_start: unit.ts_start ?? S.utts.get(unit.source_spans[0].utterance_id)?.ts_start })) : ai?.cards || [];
  const sourceIds = new Set(ai?.cards.flatMap((card) => card.source_utterance_ids) || []);
  const heldIds = new Set(['partial', 'held', 'skipped'].includes(editor?.status)
    && Array.isArray(editor?.held_utterance_ids)
    ? editor.held_utterance_ids.filter((uid) => Number.isSafeInteger(uid) && sourceIds.has(uid)) : []);
  const changedQuestioner = !!ai && typeof b.questioner === 'string' && !!b.questioner.trim()
    && ai.cards.some((card) => card.speaker_role === 'member' && typeof card.speaker === 'string'
      && !!card.speaker.trim() && card.speaker !== '위원' && card.speaker.trim() !== b.questioner.trim());
  if (!ai) g.ai.replaceChildren();
  else setKids(g.ai,
    h('p', { class: 'ai-review-note small muted' }, document
      ? '질의 전체 전사 보정 · 최종 초안입니다. 원음 정답 검증 전이며 원문·화자·요약에 자동 채택되지 않습니다.'
      : 'AI 정리 초안입니다. 화자와 문장 경계를 원문과 대조해 확인하세요.'),
    ai.ownership?.status === 'draft' && ai.ownership?.questioner
      ? h('p', { class: 'ai-review-note small' }, `음성 대조 초안의 질의자: ${ai.ownership.questioner} · 요약은 확인된 질의 범위만 포함합니다.`) : null,
    ai.ownership?.status === 'held'
      ? h('p', { class: 'ai-review-note small' }, '질의 범위 확인 필요 · 다른 의원 내용이 섞이지 않도록 자동 요약을 보류했습니다.') : null,
    heldIds.size ? h('p', { class: 'ai-review-note small' },
      badge(`문맥 판정 보류 ${heldIds.size}행 · 원문 판본 유지`, 'b-orange badge-sm')) : null,
    typeof editor?.reason === 'string' && editor.reason.trim()
      ? h('p', { class: 'ai-review-note small muted' }, editor.reason) : null,
    changedQuestioner ? h('p', { class: 'ai-review-note small muted' }, '수집 당시 질의 묶음과 정리 화자가 다를 수 있습니다.') : null,
    h('ol', { class: 'rows ai-review-cards' }, cards.map((card) => {
      const role = ['member', 'chair', 'answer', 'unknown', 'system', 'other'].includes(card.speaker_role) ? card.speaker_role : 'unknown';
      return h('li', { class: `u ai-review-card r-${role}` },
        h('div', { class: 'u-time ai-review-time' }, card.ts_start != null ? fmt.hms(card.ts_start) : ''),
        h('div', { class: 'u-spk' },
          h('strong', null, card.speaker || ({ member: '위원', chair: '위원장', answer: '답변' }[role] || '미상')),
          h('span', { class: 'role' }, document ? '최종 보정 초안' : 'AI 정리 초안'),
          card.source_utterance_ids.some((uid) => heldIds.has(uid))
            ? badge('화자 확인 필요', 'b-orange badge-sm') : null),
        h('div', { class: 'u-text' },
        card.risk_flags?.includes('speaker_identity_unverified') ? h('p', { class: 'small muted' }, '답변자·발언자 실명 확인 필요') : null,
        card.scope === 'context' || card.scope === 'uncertain' ? h('p', { class: 'small muted' },
          card.scope === 'context' ? '질의 밖 문맥' : '질의 범위 확인 필요') : null,
        h('p', { class: 'ai-review-text' }, card.readable_text),
        h('div', { class: 'review-source-links' }, card.source_utterance_ids.map((uid) =>
          h('button', { type: 'button', class: 'linkbtn small', dataset: { act: 'goto', uid: String(uid) } }, `원문 #${uid}`)))));
    })));
  syncAiVisibility(g);
}

const AI_SOURCE_FIELDS = ['text', 'original_text', 'speaker', 'speaker_role', 'speaker_source', 'speaker_hint',
  'source_speaker_hint', 'source_order', 'turn_id', 'boundary_reason', 'new_speaker', 'source_key', 'edited',
  'edited_by', 'is_final', 'block_id', 'seq', 'ts_start', 'ts_end', 'review_parts'];
const aiSourceSignature = (u) => JSON.stringify(AI_SOURCE_FIELDS.map((field) => u?.[field]));

function withoutAiSource(b, uid) {
  if (!b?.ai_review && !b?.ai_live_review) return b;
  const current = { ...b };
  delete current.ai_review;
  if (current.ai_live_review) {
    const windows = current.ai_live_review.windows?.filter((window) => uid != null
      && !window.utterance_ids?.includes(uid) && !window.context_ids?.includes(uid));
    if (!b.ai_review && windows?.length === current.ai_live_review.windows?.length) return b;
    if (windows?.length) current.ai_live_review = { ...current.ai_live_review, windows };
    else delete current.ai_live_review;
  }
  return current;
}

function invalidateBlockAi(bid, uid = null) {
  const b = S.blocks.get(bid);
  const current = withoutAiSource(b, uid);
  if (!b || current === b) return;
  S.blocks.set(bid, current);
  const g = groupMap.get(`b${bid}`);
  if (g) renderBlock(g);
}

function invalidateAiSource(uid, bids, queued = false) {
  for (const bid of new Set(bids)) {
    invalidateBlockAi(bid, uid);
    if (queued && Q.blk.has(bid)) Q.blk.set(bid, withoutAiSource(Q.blk.get(bid), uid));
  }
}

function renderBlock(g) {
  const b = S.blocks.get(g.id);
  if (!b) {
    g.el.className = 'blk other';
    setKids(g.head, h('span', { class: 'bt' }, '발언'));
    return;
  }
  const q = b.kind === 'question';
  g.el.className = `blk ${q ? 'q' : b.kind}${q && b.status === 'needs_name' ? ' needs' : ''}${b.is_open && S.session?.status === 'live' ? ' open' : ''}`;
  g.el.setAttribute('aria-label', `${b.title} ${b.time_range}`);
  const st = BLOCK_STATUS[b.status];
  const ai = blockAiReview(b);
  const stage = stageBlockReview(b);
  const liveAi = blockLiveAiReview(b);
  const contextAi = liveAi?.windows.some(window => window.context_correction?.preview_only === true
    && window.context_correction.source_hash === window.input_hash
    && ['draft', 'unchanged'].includes(window.context_correction.status));
  const acts = h('span', { class: 'bact' });
  if (stage) acts.append(h('button', { type: 'button', class: 'badge b-violet badge-sm', dataset: { act: 'stage-evidence' },
    title: '원문과 AI 보정 내역 보기' }, 'AI 보정'));
  if (stage && aiOriginalChoice.get(b.id)) acts.append(h('button', { type: 'button', class: 'btn btn-ghost btn-xs',
    dataset: { act: 'ai-original' } }, '보정 대화록 보기'));
  if (!stage && (ai || liveAi)) acts.append(h('button', { type: 'button', class: 'btn btn-ghost btn-xs', dataset: { act: 'ai-original' },
    'aria-pressed': String(!!aiOriginalChoice.get(b.id)) }, aiOriginalChoice.get(b.id)
      ? ai?.final_document ? '최종 보정 초안 보기' : contextAi ? '1차 보정 초안 보기' : 'AI 정리 보기' : '원문 보기'));
  if (q && OP) {
    if (!ctx.cloud && !stage) {
      const notice = reviewNotices.get(b.id);
      const name = notice?.status === 'accepted' ? '화자 보정' : notice?.status === 'held' ? '경계 보류' : '화자 초안';
      acts.append(h('button', { type: 'button', class: 'btn btn-ghost btn-xs', dataset: { act: 'speaker-review' } },
        notice ? `${name} · 변경 ${notice.changed} · 보류 ${notice.held}` : '화자 검토'));
    }
    if (!blockSummary(b) && !ai) acts.append(h('button', { type: 'button', class: 'btn btn-ghost btn-xs', dataset: { act: 'bsummarize' }, title: '이 질의 블록 AI 요약' }, icon('sparkles', 'ic-sm'), '요약'));
  }
  setKids(g.head, 
    h('span', { class: 'bt' }, b.title),
    q && OP ? h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', dataset: { act: 'questioner' }, title: '질의 위원 지정(Shift+S)', 'aria-label': '질의 위원 지정' }, icon('edit', 'ic-sm')) : null,
    h('span', { class: 'btr' }, b.time_range),
    b.duration_sec ? h('span', { class: 'bdur' }, `(${fmt.dur(b.duration_sec)})`) : null,
    q && st && !(stage && b.status === 'ai_inferred') ? badge(st.label, st.cls, b.name_basis ? `근거: ${b.name_basis}` : '') : null,
    !stage && ai ? badge(ai.final_document ? '질의 전체 전사 · 최종 초안' : 'AI 정리 초안', 'b-violet badge-sm', '원문을 보존한 표시 초안입니다. 자동 채택되지 않습니다.') : null,
    liveAi ? badge(contextAi ? '실시간 보정 · 1차 초안' : '실시간 AI 초안', 'b-violet badge-sm', '검토한 발언만 임시로 표시합니다. 원문과 미검토 발언을 보존하며 자동 채택되지 않습니다.') : null,
    q && b.question_ownership?.status === 'accepted' && typeof b.question_ownership.questioner === 'string'
      && b.question_ownership.questioner.trim()
      ? h('span', { class: 'small muted' }, `채택한 질의 범위: ${b.question_ownership.questioner} · 요약은 확인된 질의 범위만 포함합니다.`) : null,
    acts);
  renderAiBlock(g, b);
  if (g.rows) refreshReviewGrouping(g);
  renderBlockSummary(g, b);
}

function pointsList(title, arr, withLinks = false) {
  if (!arr || !arr.length) return null;
  return h('div', null, h('h4', null, title), h('ul', null, arr.map((x) => {
    const text = typeof x === 'string' ? x : x.text;
    const uid = typeof x === 'object' ? x.utterance_id : null;
    const ids = [...new Set((typeof x === 'object' && Array.isArray(x.source_utterance_ids)
      ? x.source_utterance_ids : [uid]).filter(id => Number.isSafeInteger(id) && id > 0))];
    return h('li', null, text, withLinks ? ids.map(id => h('button', { type: 'button', class: 'linkbtn small',
      dataset: { act: 'goto', uid: String(id) }, style: { marginLeft: '6px' } },
      ids.length > 1 ? `원문 #${id}` : '발언 보기')) : null);
  })));
}

function renderBlockSummary(g, b) {
  const ai = shownAiReview(g, b);
  const saved = blockSummary(b);
  const sm = ai?.document ? ai.summary : window.gukgamTranscriptStages?.usesStoredDisplay?.() === true
    ? window.gukgamTranscriptStages.displaySummary(b) : ai?.summary || saved;
  if (!sm) { g.sum.replaceChildren(); return; }
  const prev = g.sum.querySelector('details');
  const wasOpen = prev ? prev.open : false;
  const reviewed = b.review_status === 'reviewed';
  const edited = editNote(sm);
  const origin = summaryPresentation(sm);
  const det = h('details', { class: `bsum${reviewed ? ' reviewed' : ''}` });
  det.open = wasOpen;
  const tags = [];
  if (sm.response_type) tags.push(badge(sm.response_type, 'b-outline badge-sm', '대응 유형'));
  if (sm.urgency) tags.push(badge(sm.urgency, 'b-orange badge-sm', '대응 시급성'));
  det.append(
    h('summary', null,
      reviewed ? badge('검수 완료', 'b-green badge-sm', reviewNote(b)) : null,
      badge(origin.label, `${origin.kind === 'ai' ? 'b-violet' : 'b-gray'} badge-sm`, origin.note),
      edited ? badge('수정됨', 'b-blue badge-sm', `${edited} — 사람이 고친 요약은 자동 재요약이 덮어쓰지 않습니다`) : null,
      h('span', { class: 'one' }, sm.one_line || '블록 요약'),
      orgRelevanceBadge(sm.org_relevance),
      icon('chevron-down', 'chev')),
    h('div', { class: 'bsum-b' },
      summaryExplanation(sm),
      typeof sm.review_note === 'string' && sm.review_note.trim()
        ? h('p', { class: 'full small review-summary-note' }, sm.review_note) : null,
      pointsList('질의 요지', sm.question_points),
      pointsList('답변 요지', sm.answer_points),
      pointsList('지적사항', sm.issues, true),
      pointsList('요구자료', sm.data_requests, true),
      pointsList('약속·검토사항', sm.commitments, true),
      pointsList('귀속 검토', sm.held_attributions, true),
      sm.org_reason ? h('div', null, h('h4', null, '우리 기관 관련 근거'), h('p', { style: { margin: 0 } }, sm.org_reason)) : null,
      (sm.keywords && sm.keywords.length) || tags.length ? h('div', { class: 'full chips' }, tags,
        (sm.keywords || []).map((k) => h('button', { type: 'button', class: 'chip chip-tag', dataset: { act: 'tag', tag: k } }, `#${String(k).replace(/^#/, '')}`))) : null,
      h('div', { class: 'bsum-f' },
        h('span', null, origin.label),
        sm.generated_at ? h('span', null, fmt.dateTime(sm.generated_at)) : null,
        edited ? h('span', { class: 'bsum-edited' }, edited) : null,
        h('span', null, reviewNote(b)),
        ai ? h('span', { class: 'small' }, '화자·말묶음 정리와 같은 판본의 요약') : h('span', { class: 'acts' },
          OP && summaryEditOk ? h('button', { type: 'button', class: 'btn btn-xs', dataset: { act: 'bedit' }, title: '요지·요구자료 등 요약 내용을 고칩니다' }, icon('edit', 'ic-sm'), '요약 수정') : null,
          OP && !reviewed ? h('button', { type: 'button', class: 'btn btn-ok btn-xs', dataset: { act: 'review' } }, icon('check', 'ic-sm'), '검수 완료') : null,
          OP && reviewed ? h('button', { type: 'button', class: 'btn btn-ghost btn-xs', dataset: { act: 'unreview' }, title: 'AI 초안 상태로 되돌립니다' }, '검수 취소') : null,
          h('button', { type: 'button', class: 'btn btn-xs', dataset: { act: 'card' }, title: '메신저 전파용 질의·답변 요지 카드' }, icon('copy', 'ic-sm'), '요지 카드 복사'),
          OP ? h('button', { type: 'button', class: 'btn btn-ghost btn-xs', dataset: { act: 'bsummarize' } }, icon('refresh', 'ic-sm'), '다시 요약') : null))));
  setKids(g.sum, det);
}

/* ── 모델 반영 ───────────────────────────────────────────── */
function applyBlock(b, { silent = false, stageNotify = true } = {}) {
  if (stageNotify) window.gukgamTranscriptStages?.receive('block.upsert', b);
  const prev = S.blocks.get(b.id);
  // 로컬 수정 응답은 SSE 번호보다 먼저 도착할 수 있어 예전 준비 판본을 폐기한다.
  if (!prev || JSON.stringify(prev) !== JSON.stringify(b)) { history.revision += 1; invalidateHistoryPrefetch(); }
  S.blocks.set(b.id, b);
  const g = groupMap.get(`b${b.id}`);
  if (g) {
    if (g.start !== b.ts_start) { g.start = b.ts_start; repositionGroup(g); }
    renderBlock(g);
  }
  if (!silent && V.booted && b.kind === 'question' && S.session?.status === 'live') {
    if (!prev) announce(`${b.title} 시작`);
    else if (prev.status === 'needs_name' && b.questioner) announce(`질의 위원 확인: ${b.questioner} 위원`);
  }
  dirty.members = dirty.cur = dirty.scrub = dirty.summary = true;
}

function applyUtt(u, { stageNotify = true } = {}) {
  const prev = S.utts.get(u.id);
  if (prev && JSON.stringify(prev) === JSON.stringify(u)) return false;
  history.revision += 1;
  invalidateHistoryPrefetch();
  // 초기 스냅샷·재동기화·SSE 모두 같은 읽기 화자 투영을 갱신한다.
  if (stageNotify) window.gukgamTranscriptStages?.receive('utterance.upsert', u);
  if ((prev && aiSourceSignature(prev) !== aiSourceSignature(u)) || (!prev && V.booted)) {
    invalidateAiSource(u.id, [prev?.block_id, u.block_id]);
  }
  S.utts.set(u.id, u);
  let moved = false;
  if (!prev) orderInsert(u);
  else if (prev.ts_start !== u.ts_start || prev.seq !== u.seq || prev.source_order !== u.source_order) { orderRemove(u.id); orderInsert(u); moved = true; }
  const li = renderRow(u);
  placeRow(u, li, moved);
  applyFilterRow(li, u);
  const g = li._gkey ? groupMap.get(li._gkey) : null;
  if (g) {
    if (S.blocks.get(u.block_id)?.ai_live_review) renderBlock(g);
    dirtyGroups.add(g);
  }
  dirty.scrub = dirty.cur = dirty.counts = true;
  return !prev;
}

function removeUtt(id) {
  if (S.utts.has(id)) history.revision += 1;
  // 아직 표시하지 않은 행의 삭제도 준비 페이지에서 되살아나지 않게 한다.
  invalidateHistoryPrefetch();
  window.gukgamTranscriptStages?.receive('utterance.delete', { id });
  const previous = S.utts.get(id);
  invalidateAiSource(id, [previous?.block_id]);
  const li = rowEl.get(id);
  if (li) {
    const g = li._gkey ? groupMap.get(li._gkey) : null;
    li.remove();
    rowEl.delete(id);
    if (g) { if (g.type === 's') removeGroup(g); else dirtyGroups.add(g); }
  }
  if (S.utts.delete(id)) orderRemove(id);
  dirty.scrub = dirty.counts = true;
}

function indexItem(it, add) {
  if (!it || !it.utterance_id) return;
  let set = itemsByUtt.get(it.utterance_id);
  if (add) {
    if (!set) itemsByUtt.set(it.utterance_id, (set = new Set()));
    set.add(it.id);
  } else if (set) set.delete(it.id);
}

function applyItem(it) {
  const prev = S.items.get(it.id);
  if (prev) indexItem(prev, false);
  if (it.status === 'superseded') {
    // AI 재요약·본문 교정으로 대체된 항목(내부 상태): 화면에서 뺀다
    S.items.delete(it.id);
    if (prev && prev.utterance_id) rerenderRow(prev.utterance_id);
    dirty.items = true;
    return;
  }
  S.items.set(it.id, it);
  indexItem(it, true);
  if (prev && prev.utterance_id && prev.utterance_id !== it.utterance_id) rerenderRow(prev.utterance_id);
  if (it.utterance_id) rerenderRow(it.utterance_id);
  dirty.items = true;
}

/** 항목 전체 교체(재조회·재동기화). 바뀐 발언 행의 칩도 다시 그린다. */
function replaceItems(list) {
  const touched = new Set();
  for (const it of S.items.values()) if (it.utterance_id) touched.add(it.utterance_id);
  S.items.clear();
  itemsByUtt.clear();
  for (const it of list) {
    if (it.status === 'superseded') continue;
    S.items.set(it.id, it); indexItem(it, true); if (it.utterance_id) touched.add(it.utterance_id);
  }
  for (const uid of touched) rerenderRow(uid);
  dirty.items = true;
}

async function reloadItems() {
  try {
    // 목록 API 는 기본으로 '제외' 항목을 빼므로 따로 받아 합친다(스냅샷과 같은 범위)
    const [act, dis] = await Promise.all([
      api('items', { query: { session_id: SID } }),
      api('items', { query: { session_id: SID, status: 'dismissed' } }),
    ]);
    replaceItems([...(act.items || []), ...(dis.items || [])]);
    scheduleFlush();
  } catch (e) {
    console.warn('후속조치 다시 불러오기 실패', e);
  }
}

function applyAlert(a) {
  if (a._delete) { // 본문 교정으로 더 이상 언급이 아니게 된 미확인 알림
    S.alerts.delete(a.id);
    dirty.alerts = dirty.scrub = true;
    return;
  }
  const prev = S.alerts.get(a.id);
  S.alerts.set(a.id, prev ? { ...prev, ...a } : a);
  dirty.alerts = dirty.scrub = true;
}

function applySummary(d) {
  const scope = d.scope || d.summary?.scope || 'session';
  const s = d.summary !== undefined ? d.summary : d;
  if (scope === 'rolling') S.rolling = s; else S.summary = s;
  dirty.summary = true;
}

/* ── 이벤트 큐(rAF 일괄 반영) ─────────────────────────────── */
const Q = { utt: new Map(), del: new Set(), blk: new Map(), item: new Map(), reviews: new Map(), reloadItems: false, alerts: [], summary: [], session: null, stats: null, analysis: null, display: null };
let flushScheduled = false;

function scheduleFlush() {
  if (flushScheduled) return;
  flushScheduled = true;
  if (document.hidden) setTimeout(flush, 300); // 백그라운드 탭에서는 rAF 가 멈춘다
  else requestAnimationFrame(flush);
}

function enqueue(type, d) {
  if (['caption.source', 'transcript.stage', 'transcript.display', 'roster.updated', 'settings.updated'].includes(type)) {
    window.gukgamTranscriptStages?.receive(type, d);
    if (type === 'roster.updated' && Array.isArray(d?.members)) {
      S.roster = d.members; window.gukgamTranscriptRoster = S.roster; renderRosterLists();
    }
    return;
  }
  switch (type) {
    case 'utterance.upsert': {
      const previous = Q.utt.get(d.id) || S.utts.get(d.id);
      if ((previous && aiSourceSignature(previous) !== aiSourceSignature(d)) || (!previous && V.booted)) {
        invalidateAiSource(d.id, [previous?.block_id, d.block_id], true);
      }
      Q.del.delete(d.id); Q.utt.set(d.id, d); break;
    }
    case 'utterance.delete': {
      const previous = Q.utt.get(d.id) || S.utts.get(d.id);
      invalidateAiSource(d.id, [previous?.block_id], true);
      Q.utt.delete(d.id); Q.del.add(d.id); break;
    }
    case 'block.upsert': window.gukgamTranscriptStages?.receive(type, d); Q.blk.set(d.id, d); break;
    case 'item.upsert': Q.item.set(d.id, d); break;
    case 'items.reload': Q.reloadItems = true; break;
    case 'alert': notifyAlert(d); Q.alerts.push(d); break;
    case 'alert.delete': Q.alerts.push({ ...d, _delete: true }); break;
    case 'summary.update': Q.summary.push(d); break;
    case 'stats': Q.stats = d; break;
    case 'session.status': Q.session = d; break;
    case 'analysis.status': Q.analysis = d; break;
    case 'transcript.review': if (OP && !ctx.cloud && d?.id && d?.block_id) Q.reviews.set(d.id, d); else return; break;
    default: return;
  }
  scheduleFlush();
}

function flush() {
  flushScheduled = false;
  let added = 0;
  let changedUtt = false;
  if (Q.session) {
    const prevStatus = S.session?.status;
    S.session = Q.session;
    if (Q.session.stats) S.stats = Q.session.stats;
    Q.session = null;
    renderHeader();
    if (prevStatus && prevStatus !== S.session.status) {
      announce(`수집 상태: ${SESSION_STATUS[S.session.status]?.label || S.session.status}`);
      for (const g of groupMap.values()) if (g.type === 'b') renderBlock(g);
      onCaptureSessionStatus(S.session.status);
    }
  }
  if (Q.stats) { S.stats = Q.stats; Q.stats = null; dirty.counts = true; }
  for (const id of Q.del) { removeUtt(id); changedUtt = true; }
  Q.del.clear();
  const stored = Q.display && window.gukgamTranscriptStages?.acceptsDisplayPayload?.(Q.display) !== false;
  const storedRows = new Set(stored ? Q.display.utterances || [] : []), storedBlocks = new Set(stored ? Q.display.blocks || [] : []);
  for (const u of Q.utt.values()) {
    if (storedRows.has(u) && window.gukgamTranscriptStages?.acceptsDisplayUtterance?.(Q.display, u) === false) continue;
    if (applyUtt(u, { stageNotify: !storedRows.has(u) })) added += 1; changedUtt = true;
  }
  Q.utt.clear();
  // 같은 화면 갱신의 원문 변경으로 오래된 초안을 해제한 뒤 새 완료 판본을 표시한다.
  for (const b of Q.blk.values()) if (!storedBlocks.has(b)
      || window.gukgamTranscriptStages?.acceptsDisplayBlock?.(Q.display, b) !== false)
    applyBlock(b, { stageNotify: !storedBlocks.has(b) });
  Q.blk.clear();
  if (Q.display) { window.gukgamTranscriptStages?.ingestDisplay?.(Q.display); Q.display = null; }
  for (const it of Q.item.values()) applyItem(it);
  Q.item.clear();
  if (Q.reloadItems) { Q.reloadItems = false; reloadItems(); }
  for (const a of Q.alerts) applyAlert(a);
  Q.alerts.length = 0;
  for (const s of Q.summary) applySummary(s);
  Q.summary.length = 0;
  if (Q.analysis) { S.analysis = Q.analysis; Q.analysis = null; renderAnalysis(); }
  for (const review of Q.reviews.values()) notifyTranscriptReview(review);
  Q.reviews.clear();

  for (const g of dirtyGroups) refreshGroupVisibility(g);
  if (dirtyGroups.size) visGroupsCache = null;
  dirtyGroups.clear();
  renderDirty();

  if (changedUtt) {
    if (filtering()) renderQCount(); // 검색 중 새로 들어온·바뀐 발언도 건수에 반영
    if (V.autoscroll && V.tab === 'feed') scrollToBottom();
    else if (added && !V.autoscroll) { V.newSincePause += added; updateToLatest(); }
  }
  scheduleScrubPos();
}

function renderDirty() {
  if (dirty.counts) { dirty.counts = false; renderCounts(); }
  if (dirty.cur) { dirty.cur = false; renderCur(); }
  if (dirty.alerts) { dirty.alerts = false; renderAlertFeed(); if (V.tab === 'alerts') renderAlertsTab(); renderTabCounts(); }
  if (dirty.summary) { dirty.summary = false; renderIssues(); renderRolling(); if (V.tab === 'summary') renderSummaryTab(); else summaryStale = true; }
  if (dirty.members) { dirty.members = false; renderTabCounts(); if (V.tab === 'members') renderMembers(); else membersStale = true; }
  if (dirty.items) { dirty.items = false; renderTabCounts(); if (V.tab === 'items') renderItemsTab(); else itemsStale = true; }
  if (dirty.scrub) { dirty.scrub = false; renderScrubber(); }
}
let summaryStale = true, membersStale = true, itemsStale = true;

/* ── SSE ─────────────────────────────────────────────────── */
let es = null;
let esRetry = 0;
let esTimer = 0;
let esNoticeTimer = 0;

function clearReconnectNotice() {
  clearTimeout(esNoticeTimer);
  esNoticeTimer = 0;
}

function setConn(state) {
  const el = $('#conn-sse');
  const map = {
    connecting: ['연결 중', 'warn'], open: ['실시간 연결됨', 'ok'], reconnecting: ['다시 연결 중', 'warn'], closed: ['연결 끊김', 'err'],
  };
  const [label, cls] = map[state] || map.connecting;
  setKids(el, h('i', { class: `dot ${cls}` }), h('span', null, label));
  el.title = state === 'open' ? '서버와 실시간으로 연결되어 있습니다' : '연결이 끊기면 자동으로 다시 연결합니다';
}

/**
 * 연결을 닫고 스냅샷으로 맞춘 뒤, 스냅샷의 이벤트 번호부터 다시 이어 받는다.
 * 연결을 연 채 스냅샷을 받으면 그사이 먼저 반영된 새 이벤트(발언·요약)를 늦게 온 옛 스냅샷이 지우거나 덮어쓴다.
 * 닫고 다시 붙으면 스냅샷 이후 이벤트가 스냅샷 뒤에 재전송되어 항상 최신 값이 남는다.
 */
function resyncAndReconnect(delay = 0) {
  clearReconnectNotice();
  if (es) { es.close(); es = null; }
  clearTimeout(esTimer);
  esTimer = setTimeout(async () => {
    try {
      await resync();
    } catch (e) {
      console.warn('재동기화 실패', e);
      setConn('closed');
      const wait = Math.min(30000, 1000 * 2 ** esRetry);
      esRetry += 1;
      resyncAndReconnect(wait);
      return;
    }
    connectSSE();
  }, delay);
}

function connectSSE() {
  if (ctx.offline) {
    setKids($('#conn-sse'), h('span', null, '단독 열람 · 전체 기록 포함'));
    return;
  }
  clearReconnectNotice();
  if (es) es.close();
  clearTimeout(esTimer);
  setConn('connecting');
  const src = new EventSource(`/api/sessions/${enc(SID)}/events?last_id=${S.lastEventId || 0}`);
  es = src;
  src.onopen = () => {
    if (es !== src) return;
    clearReconnectNotice();
    esRetry = 0;
    setConn('open');
  };
  src.onerror = () => {
    if (es !== src) return;
    if (src.readyState === EventSource.CLOSED) {
      setConn('closed');
      const delay = Math.min(30000, 1000 * 2 ** esRetry);
      esRetry += 1;
      resyncAndReconnect(delay);
    } else {
      // 클라우드는 25초마다 정상 종료한다. 짧은 자동 재연결 동안 기존 배지를 유지한다.
      if (!ctx.cloud) setConn('reconnecting');
      else if (!esNoticeTimer) esNoticeTimer = setTimeout(() => {
        esNoticeTimer = 0;
        if (es === src && src.readyState !== EventSource.OPEN) setConn('reconnecting');
      }, 2500);
    }
  };
  // 서버 재시작·이벤트 기록 초과: 재전송으로 메울 수 없으니 연결을 닫고 스냅샷으로 맞춘 뒤 다시 붙는다
  src.addEventListener('resync', (ev) => {
    if (es !== src) return;
    let reason = {}; try { reason = JSON.parse(ev.data); } catch { /* 접속 재동기화는 원문을 보존한다. */ }
    window.gukgamTranscriptStages?.receive('resync', reason);
    setConn('reconnecting');
    resyncAndReconnect(0);
  });
  for (const type of EVENT_TYPES) {
    src.addEventListener(type, (ev) => {
      if (es !== src) return; // 닫은 연결에 남아 있던 이벤트는 버린다(스냅샷 이후 번호부터 다시 받는다)
      const id = Number(ev.lastEventId);
      if (id) { S.lastEventId = id; invalidateHistoryPrefetch(); }
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      enqueue(type, data);
    });
  }
}

/** 오래 끊겼을 때: 스냅샷으로 차이를 맞춘다(이벤트 기록 보관 한도 대비) */
async function resync() {
  invalidateHistoryPrefetch();
  window.gukgamTranscriptStages?.receive('resync', { reason: 'snapshot_reload' });
  const snap = await readSessionSnapshot(`sessions/${enc(SID)}/snapshot`, { query: { recent: true, utterance_limit: 100 } });
  if (Object.hasOwn(snap || {}, 'display_blocks')
      && window.gukgamTranscriptStages?.acceptsDisplayPayload?.(snap) === false) return;
  const seen = new Set();
  if (Object.hasOwn(snap, 'display_blocks')) Q.display = snap;
  Q.session = snap.session;
  for (const b of snap.blocks || []) Q.blk.set(b.id, b);
  for (const u of snap.utterances || []) { seen.add(u.id); Q.utt.set(u.id, u); }
  const valid = Array.isArray(snap.valid_utterance_ids) ? new Set(snap.valid_utterance_ids) : snap.utterance_page?.partial ? null : seen;
  for (const id of S.utts.keys()) if (valid && !valid.has(id)) Q.del.add(id);
  // 연결을 닫기 전에 받아 둔(스냅샷보다 오래된) 발언 중 스냅샷에 없는 것은 반영하지 않는다
  for (const id of [...Q.utt.keys()]) if (valid && !valid.has(id)) Q.utt.delete(id);
  for (const a of snap.alerts || []) Q.alerts.push(a);
  if (snap.summary) Q.summary.push({ scope: 'session', summary: snap.summary });
  if (snap.rolling) Q.summary.push({ scope: 'rolling', summary: snap.rolling });
  Q.item.clear();
  Q.reloadItems = false;
  replaceItems(snap.items || []);
  if (Array.isArray(snap.roster)) enqueue('roster.updated', { members: snap.roster });
  if (snap.analysis) Q.analysis = snap.analysis;
  if (Array.isArray(snap.bookmarks)) { S.bookmarks = new Set(snap.bookmarks.map(Number)); }
  S.lastEventId = snap.last_event_id || 0;
  scheduleFlush();
}

/* ── 초기 로드 ───────────────────────────────────────────── */
function initFromSnapshot(snap) {
  S.session = snap.session;
  S.stats = snap.session?.stats || null;
  S.roster = snap.roster || [];
  window.gukgamTranscriptRoster = S.roster;
  window.gukgamTranscriptStages?.receive('roster.updated', { members: S.roster, baseline: true });
  S.summary = snap.summary || null;
  S.rolling = snap.rolling || null;
  S.lastEventId = snap.last_event_id || 0;
  S.analysis = snap.analysis || null;
  history.partial = snap.utterance_page?.partial === true;
  history.nextBefore = snap.utterance_page?.next_before ?? null;
  for (const it of snap.items || []) { S.items.set(it.id, it); indexItem(it, true); }
  for (const a of snap.alerts || []) S.alerts.set(a.id, a);
  const bms = snap.bookmarks || [];
  for (const b of bms) S.bookmarks.add(typeof b === 'object' ? (b.utterance_id ?? b.id) : Number(b));
  for (const u of snap.utterances || []) if (u.bookmarked) S.bookmarks.add(u.id);
  if (!bms.length) for (const id of store.get(`bm.${SID}`, [])) S.bookmarks.add(id);

  // DOM 을 떼어 낸 상태에서 한 번에 만든다(레이아웃 비용 최소화)
  const parent = feed.parentNode;
  const next = feed.nextSibling;
  feed.remove();
  const stored = Object.hasOwn(snap, 'display_blocks');
  for (const b of [...(snap.blocks || [])].sort((a, c) => a.ts_start - c.ts_start || a.id - c.id))
    applyBlock(b, { silent: true, stageNotify: !stored });
  const utts = [...(snap.utterances || [])].sort(cmpUtt);
  for (const u of utts) applyUtt(u, { stageNotify: !stored });
  window.gukgamTranscriptStages?.ingestDisplay?.(snap);
  for (const g of groupMap.values()) refreshGroupVisibility(g);
  dirtyGroups.clear();
  parent.insertBefore(feed, next);

  $('#sess-loading').hidden = true;
  $('#sess').hidden = false;
  $('#fab').hidden = false;
  renderHeader();
  renderRosterLists();
  renderAnalysis();
  dirty.members = dirty.items = dirty.alerts = dirty.summary = dirty.cur = dirty.scrub = dirty.counts = true;
  renderDirty();
  updateFeedEmpty();
  V.booted = true;
  historyControls();
  seedRecentCorrections(snap);
}

function updateFeedEmpty() {
  const el = $('#feed-empty');
  if (S.utts.size || window.gukgamTranscriptStages?.displayLooseRows?.().length) { el.hidden = true; return; }
  const st = S.session?.status;
  const replay = S.session?.recording_replay === true;
  setKids(el, emptyState(
    st === 'live' ? replay ? '저장 자막을 재생하는 중입니다' : '발언을 기다리는 중입니다' : '아직 수집된 발언이 없습니다',
    st === 'live' ? replay ? '선택한 보관 파일의 자막을 순서대로 재생합니다. 실방송 수신이 아닙니다.'
      : '수집원이 연결되면 발언이 실시간으로 표시됩니다. 증인 요청 등으로 중계가 끊긴 구간은 기록되지 않습니다.'
      : (OP ? '위의 [시작]을 누르면 수집을 시작합니다.' : '운영자가 수집을 시작하면 여기에 표시됩니다.'), 'message'));
  el.hidden = false;
}

/* ── 헤더·수집원 상태 ─────────────────────────────────────── */
function renderHeader() {
  const s = S.session;
  if (!s) return;
  const replay = s.recording_replay === true;
  window.gukgamTranscriptSession = { source_type: s.source_type, status: s.status };
  window.gukgamTranscriptStages?.setSession(window.gukgamTranscriptSession);
  const st = SESSION_STATUS[s.status] || { label: s.status, cls: 'b-gray' };
  const sb = $('#sh-status');
  sb.className = `badge ${st.cls}`;
  sb.textContent = s.status === 'live' ? replay ? '저장 자료 재생 중' : 'LIVE' : st.label;
  const com = $('#sh-committee');
  com.hidden = !s.committee;
  com.textContent = s.committee || '';
  com.title = committeeShort(s.committee);
  const kind = meetingKind(s.title);
  $('#sh-kind').hidden = !kind;
  $('#sh-kind').textContent = kind === '국감' ? '국정감사' : kind;
  setKids($('#sh-source'), icon(sourceIcon(replay ? 'replay' : s.source_type), 'ic-sm'), s.source_label || SOURCE_LABELS[s.source_type] || s.source_type);
  const audioNote = $('#audio-transcript-note');
  if (audioNote) {
    const notice = audioTranscriptNotice(s.source_type);
    audioNote.hidden = !notice;
    audioNote.textContent = notice;
  }
  $('#sh-title').textContent = s.title;
  document.title = `${s.status === 'live' ? replay ? '▷ ' : '● ' : ''}${s.title} · 국감 라이브`;
  renderMeta();
  $('#feed-live').hidden = s.status !== 'live' || replay;
  $('#ai-report').href = `/s/${enc(SID)}/report`;
  $('#it-csv').href = `/api/sessions/${enc(SID)}/export?format=csv`;

  if (OP) {
    const live = s.status === 'live';
    $('#c-start').hidden = live;
    $('#c-start').querySelector('span').textContent = s.status === 'paused' ? '재개' : (s.status === 'ended' ? '다시 시작' : '시작');
    $('#c-pause').hidden = !live;
    $('#c-stop').hidden = s.status === 'ended' || s.status === 'idle';
    const mk = $('#c-marker');
    if (mk) mk.hidden = s.status === 'ended';
  }
  renderSourceState();
  renderSourcePanel();
  onAudioRecordingSessionStatus();
  // 수기 입력 세션은 늘, 다른 수집원은 운영자가 '보충 발언 입력'을 켰을 때 입력 바를 보인다
  const manual = OP && s.status !== 'ended' && (s.source_type === 'manual' || V.manualOpen);
  const bar = $('#manual-bar');
  if (bar) bar.hidden = !manual;
  document.body.classList.toggle('has-manual', !!manual);
  $('#scrubber').hidden = V.tab !== 'feed';
  updateFeedEmpty();
}

function sourceIcon(t) {
  return { assembly_caption: 'caption', assembly_hls: 'video', browser: 'mic', file: 'file', replay: 'replay', manual: 'keyboard' }[t] || 'live';
}

function renderMeta() {
  const s = S.session;
  const st = S.stats || {};
  const parts = [];
  if (s.meeting_date) parts.push(h('span', null, icon('calendar', 'ic-sm'), fmt.meetingDate(s.meeting_date)));
  if (s.agencies) parts.push(h('span', null, icon('building', 'ic-sm'), s.agencies));
  parts.push(h('span', { id: 'meta-utt' }, `발언 ${fmt.num(st.utterances ?? S.utts.size)}`));
  parts.push(h('span', null, `질의 ${fmt.num(st.question_blocks ?? 0)}`));
  if (st.needs_name) parts.push(h('span', { style: { color: 'var(--warn-text)' } }, `성명 확인 필요 ${st.needs_name}`));
  if (s.started_at) parts.push(h('span', { id: 'meta-elapsed' }, elapsedText()));
  if (s.notes) {
    parts.push(/^https?:\/\//.test(s.notes.trim())
      ? h('span', null, h('a', { href: s.notes.trim(), target: '_blank', rel: 'noopener' }, '공식 회의록', icon('external', 'ic-sm')))
      : h('span', { title: s.notes }, '메모 있음'));
  }
  setKids($('#sh-meta'), ...parts);
}

function elapsedText() {
  const s = S.session;
  if (!s || !s.started_at) return '';
  if (s.status === 'ended' && s.ended_at) return `수집 ${fmt.hm(s.started_at)}~${fmt.hm(s.ended_at)} (${fmt.dur((s.ended_at - s.started_at) / 1000)})`;
  if (s.status === 'live') return `수집 ${fmt.hm(s.started_at)}~ · 경과 ${fmt.clock((Date.now() - s.started_at) / 1000)}`;
  return `수집 ${fmt.hm(s.started_at)}~ · ${SESSION_STATUS[s.status]?.label || ''}`;
}

function renderSourceState() {
  const s = S.session;
  const ss = s.source_state || {};
  const el = $('#conn-src');
  const phase = ss.phase;
  if (!phase && s.source_type === 'manual') { el.hidden = true; $('#src-details').hidden = true; return; }
  el.hidden = false;
  const cls = { receiving: 'ok', connected: 'ok', done: 'blue', connecting: 'warn', reconnecting: 'warn', error: 'err', stopped: '' }[phase] ?? '';
  const bits = [h('i', { class: `dot ${cls}` }), h('span', null, `수집원: ${PHASE_LABELS[phase] || (s.status === 'live' ? '준비 중' : '대기')}`)];
  if (ss.latency_ms != null && (phase === 'receiving' || phase === 'connected')) bits.push(h('span', null, `· 지연 ${(ss.latency_ms / 1000).toFixed(1)}초`));
  if (ss.last_rx_ms) bits.push(h('span', { id: 'src-rx' }, `· 마지막 수신 ${fmt.ago(ss.last_rx_ms)}`));
  if (typeof ss.progress === 'number' && ss.progress > 0 && ss.progress < 1) bits.push(h('span', null, `· 진행 ${Math.round(ss.progress * 100)}%`));
  setKids(el, ...bits);
  const mt = ss.meeting && (ss.meeting.xdesc || (ss.meeting.xstat === '1' ? '생중계 중' : ''));
  const kb = $('#sh-kind');
  if (mt) { kb.hidden = false; kb.textContent = `국회: ${mt}`; kb.className = `badge ${/정회|중지|종료|산회/.test(mt) ? 'b-orange' : 'b-outline'}`; kb.title = '국회 의사중계 회의 상태'; }
  el.title = [ss.detail, ss.error].filter(Boolean).join(' / ') || '수집원 상태';
  const det = $('#src-details');
  const logs = Array.isArray(ss.log) ? ss.log : [];
  det.hidden = !(logs.length || ss.detail || ss.error);
  $('#src-log').textContent = [ss.detail ? `상태: ${ss.detail}` : '', ss.error ? `오류: ${ss.error}` : '', ss.rx_count != null ? `수신 ${ss.rx_count}건` : '', ...logs].filter(Boolean).join('\n');
}

function renderCounts() {
  const n = S.stats?.utterances ?? [...S.utts.values()].filter((u) => u.is_final).length;
  $('#feed-total').textContent = `총 ${fmt.num(n)}개 발언`;
  if (S.session) renderMeta();
  updateFeedEmpty();
}

/* ── 운영자 제어 ─────────────────────────────────────────── */
async function control(action) {
  const s = S.session;
  if (action === 'pause') {
    const ok = await confirmDialog({ title: '수집 일시정지', message: '수집을 멈추고 지금까지 받은 발언을 확정합니다. [재개]로 다시 이어서 수집할 수 있습니다.', ok: '일시정지' });
    if (!ok) return;
  } else if (action === 'stop') {
    const ok = await confirmDialog({ title: '수집 종료', message: '수집을 끝내고 회의 전체 요약을 만듭니다. 종료 후에도 기록·요약·후속조치는 계속 볼 수 있습니다.', ok: '종료', danger: true });
    if (!ok) return;
  } else if (action === 'start' && s.status === 'ended') {
    const ok = await confirmDialog({ title: '다시 시작', message: '종료한 세션의 수집을 다시 시작할까요?', ok: '다시 시작' });
    if (!ok) return;
  }
  const btn = $(`#c-${action}`);
  setBusy(btn, true);
  try {
    const r = await api(`sessions/${enc(SID)}/${action}`, { method: 'POST' });
    if (r && r.id) enqueue('session.status', r);
    toast({ start: '수집을 시작했습니다.', pause: '수집을 일시정지했습니다.', stop: '수집을 종료했습니다.' }[action], { type: 'ok', timeout: 2500 });
  } catch (e) {
    toast(errMsg(e), { type: 'err', title: '수집 제어 실패' });
  } finally {
    setBusy(btn, false);
  }
}

function sessionMenu(anchor) {
  const s = S.session;
  const canSupplement = s.source_type !== 'manual' && s.status !== 'ended';
  popupMenu(anchor, [
    { label: '위원 명단 편집', icon: 'users', onClick: editRoster },
    { label: '회의 정보 수정', icon: 'edit', onClick: editSessionInfo },
    // 수집원 전환·회의 코드 수정은 일시정지·대기 상태에서만(수집 중에는 서버가 거절한다)
    { label: '수집 설정 변경', icon: 'sliders', hidden: ctx.cloud || !(s.status === 'paused' || s.status === 'idle'), onClick: editSourceConfig },
    { label: '기록 표지 남기기', icon: 'flag', hidden: s.status === 'ended', onClick: addMarker },
    { label: V.manualOpen ? '보충 발언 입력 닫기' : '보충 발언 입력', icon: 'keyboard', hidden: !canSupplement, onClick: toggleSupplement },
    { label: '상황보고', icon: 'report', href: `/s/${enc(SID)}/report` },
    'sep',
    { label: '세션 삭제', icon: 'trash', danger: true, hidden: ctx.cloud || s.status === 'live', onClick: deleteSession },
  ], { label: '세션 메뉴' });
}

/* ── 수집 설정 변경(수집원 전환·회의 코드) ─────────────────── */
const SOURCE_FIELDS = {
  assembly_caption: ['xcode', 'xcgcd', 'xsami'],
  assembly_hls: ['xcode', 'xcgcd', 'hls_url'],
};
const CODE_RE = /^[A-Za-z0-9_.-]{1,40}$/;

function sourceConfigError(t, v) {
  for (const k of ['xcode', 'xcgcd']) if (v[k] && !CODE_RE.test(v[k])) return `${k} 형식이 올바르지 않습니다(영문·숫자).`;
  if (t === 'assembly_caption' && !v.xcode && !v.xsami) return '국회 회의 코드(xcode)를 입력하세요.';
  if (t === 'assembly_hls' && !v.xcode && !v.hls_url) return '회의 코드(xcode) 또는 영상 주소(HLS)를 입력하세요.';
  if (t === 'assembly_caption' && v.xsami && !/^wss?:\/\//.test(v.xsami)) return '자막 주소(xsami)는 ws:// 또는 wss:// 로 시작해야 합니다.';
  if (t === 'assembly_hls' && v.hls_url && !/^https?:\/\//.test(v.hls_url)) return '영상 주소는 http:// 또는 https:// 로 시작해야 합니다.';
  return '';
}

async function editSourceConfig() {
  if (ctx.cloud) return;
  const s = S.session;
  const c = s.source_config || {};
  const types = Object.keys(SOURCE_LABELS).filter((k) => k !== 'replay' || s.source_type === 'replay');
  const applyVis = (controls) => {
    const t = controls.source_type.value;
    const keys = SOURCE_FIELDS[t] || [];
    for (const k of ['xcode', 'xcgcd', 'xsami', 'hls_url']) showField(controls[k], keys.includes(k));
  };
  const vals = await formDialog({
    title: '수집 설정 변경',
    description: '같은 세션에서 수집원을 바꾸거나 회의 코드를 고칩니다. 지금까지의 발언·명단·질의 블록·후속조치는 그대로 이어집니다. 녹음 파일로 바꾸면 저장한 뒤 오른쪽 ‘녹음 파일’ 패널에서 파일을 올립니다.',
    fields: [
      { name: 'source_type', label: '수집 방법', type: 'select', value: s.source_type, options: types.map((k) => [k, SOURCE_LABELS[k]]),
        onInput: (_ctl, controls) => applyVis(controls) },
      { name: 'xcode', label: '회의 코드(xcode)', value: c.xcode || '', placeholder: '예: 25' },
      { name: 'xcgcd', label: '회의 일련번호(xcgcd)', value: c.xcgcd || '', placeholder: '오후 차수 등으로 바뀌면 고칩니다',
        hint: '국회 의사중계 주소의 xcgcd 값입니다.' },
      { name: 'xsami', label: '자막 웹소켓 주소(xsami, 고급)', value: c.xsami || '', placeholder: '비워 두면 자동으로 찾습니다' },
      { name: 'hls_url', label: '영상 주소(HLS, 선택)', value: c.hls_url || '', placeholder: '비워 두면 회의 코드로 찾습니다(…/playlist.m3u8)' },
    ],
    submit: '바꾸기',
    validate: (v) => sourceConfigError(v.source_type, {
      xcode: v.xcode.trim(), xcgcd: v.xcgcd.trim(), xsami: v.xsami.trim(), hls_url: v.hls_url.trim(),
    }),
    onOpen: applyVis,
  });
  if (!vals) return;
  const t = vals.source_type;
  // 기존 설정(파일 경로·리플레이 배속 등)은 두고, 이 화면에서 다루는 값만 바꾼다
  const cfg = { ...c };
  for (const k of ['xcode', 'xcgcd', 'xsami', 'hls_url']) {
    const v = (vals[k] || '').trim();
    if ((SOURCE_FIELDS[t] || []).includes(k) && v) cfg[k] = v;
    else if ((SOURCE_FIELDS[t] || []).includes(k)) delete cfg[k];
  }
  await patchSource(t, cfg);
}

async function patchSource(sourceType, sourceConfig) {
  if (ctx.cloud) return;
  let r;
  try {
    r = await api(`sessions/${enc(SID)}`, { method: 'PATCH', body: { source_type: sourceType, source_config: sourceConfig } });
  } catch (e) { toast(errMsg(e), { type: 'err', title: '수집 설정을 바꾸지 못했습니다' }); return; }
  if (r && r.id) enqueue('session.status', { ...S.session, ...r });
  const st = (r && r.status) || S.session.status;
  if (sourceType === 'file' && !(sourceConfig && sourceConfig.path)) {
    toast('수집 설정을 바꿨습니다. 오른쪽 ‘녹음 파일’ 패널에서 파일을 올린 뒤 [시작]을 누르세요.', { type: 'ok', timeout: 6000 });
    return;
  }
  if (st === 'paused' || st === 'idle') {
    const go = await confirmDialog({
      title: '수집 설정을 바꿨습니다',
      message: `${SOURCE_LABELS[sourceType] || sourceType}(으)로 바꿨습니다. 새 설정으로 지금 수집을 ${st === 'paused' ? '재개' : '시작'}할까요?`,
      ok: st === 'paused' ? '재개' : '시작', cancel: '나중에',
    });
    if (go) await control('start');
  } else {
    toast('수집 설정을 바꿨습니다.', { type: 'ok' });
  }
}

/* ── 기록 표지(정회·속개·중계 중단 등) ─────────────────────── */
/** 'HH:MM' → 마지막 발언 날짜(없으면 오늘, KST)의 그 시각(ms). 형식이 틀리면 null */
function markerTs(hm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec((hm || '').trim());
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return null;
  const last = order.length ? S.utts.get(order[order.length - 1]) : null;
  const day = fmt.ymd(last ? last.ts_start : Date.now());
  const ms = Date.parse(`${day}T${m[1].padStart(2, '0')}:${m[2]}:00+09:00`);
  return Number.isNaN(ms) ? null : ms;
}

async function addMarker() {
  const nowHm = fmt.hm(Date.now());
  const vals = await formDialog({
    title: '기록 표지 남기기',
    description: '정회·속개·중계 중단처럼 기록의 공백이나 사유를 발언 목록에 표지로 남깁니다. 상황보고·내보내기에도 들어갑니다.',
    fields: [
      { name: 'preset', label: '종류', type: 'select', value: MARKER_PRESETS[0], options: MARKER_PRESETS.map((p) => [p, p]) },
      { name: 'time', label: '시각(HH:MM)', value: nowHm, placeholder: '예: 14:20', hint: '지난 시각을 적으면 그 시각의 표지로 남깁니다. 비우면 지금 시각.' },
      { name: 'memo', label: '덧붙일 말(선택)', placeholder: '예: 증인 채택 문제로 비공개 전환' },
    ],
    submit: '남기기',
    validate: (v) => {
      if (v.time.trim() && markerTs(v.time) == null) return '시각은 HH:MM 형식으로 입력하세요(예: 14:20).';
      if (v.preset === '메모' && !v.memo.trim()) return '메모 내용을 입력하세요.';
      return '';
    },
  });
  if (!vals) return;
  const hm = vals.time.trim() || nowHm;
  const memo = vals.memo.trim();
  const head = vals.preset === '메모' ? memo : `${vals.preset}(${hm}${['정회', '중계 중단', '비공개 전환'].includes(vals.preset) ? '~' : ''})`;
  const text = `— ${vals.preset === '메모' ? `메모(${hm}) · ${head}` : (memo ? `${head} · ${memo}` : head)} —`;
  if (ctx.cloud && Array.from(text).length > 2000) {
    toast('클라우드에서는 기록 표지를 2,000자까지 입력할 수 있습니다.', { type: 'warn' });
    return;
  }
  const body = { marker: true, text };
  // 시각을 바꿨을 때만 보낸다(서버가 시각 지정을 지원하면 그 시각에 놓이고, 아니면 지금 시각)
  const ts = vals.time.trim() && vals.time.trim() !== nowHm ? markerTs(vals.time) : null;
  if (ts) body.ts = ts;
  try {
    await api(`sessions/${enc(SID)}/manual`, { method: 'POST', body });
    toast('기록 표지를 남겼습니다.', { type: 'ok', timeout: 2000 });
  } catch (e) { toast(errMsg(e), { type: 'err', title: '기록 표지를 남기지 못했습니다' }); }
}

function toggleSupplement() {
  V.manualOpen = !V.manualOpen;
  renderHeader();
  if (V.manualOpen) $('#m-text')?.focus();
}

async function editRoster() {
  const text = S.roster.filter((m) => m.scope !== 'global' && m.session_id !== null).map((m) => [m.name, m.role === 'chair' ? '위원장' : '', m.party].filter(Boolean).join(' ')).join('\n');
  const vals = await formDialog({
    title: '위원 명단 편집',
    description: '한 줄에 한 명: 이름, (위원장), 정당. 명단은 위원장 호명·자기소개로 질의 위원을 찾을 때 씁니다.',
    fields: [{ name: 'text', label: '명단', type: 'textarea', rows: 12, value: text, placeholder: '유의동 위원장 국민의힘\n김남근 더불어민주당' }],
    submit: '저장',
  });
  if (!vals) return;
  try {
    const r = await api(`sessions/${enc(SID)}/roster`, { method: 'PUT', body: { text: vals.text } });
    enqueue('roster.updated', { members: r.members || [] });
    toast(`명단 ${S.roster.length}명을 저장했습니다.`, { type: 'ok' });
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

async function editSessionInfo() {
  const s = S.session;
  const vals = await formDialog({
    title: '회의 정보 수정',
    fields: [
      { name: 'title', label: '회의 제목', value: s.title, required: true },
      { name: 'committee', label: '위원회', value: s.committee },
      { name: 'agencies', label: '피감기관', value: s.agencies },
      { name: 'meeting_date', label: '회의일', type: 'date', value: s.meeting_date },
      { name: 'notes', label: '메모·공식 회의록 링크', value: s.notes },
    ],
  });
  if (!vals) return;
  try {
    const r = await api(`sessions/${enc(SID)}`, { method: 'PATCH', body: vals });
    enqueue('session.status', { ...S.session, ...r });
    toast('회의 정보를 저장했습니다.', { type: 'ok' });
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

async function deleteSession() {
  if (ctx.cloud) return;
  const ok = await confirmDialog({ title: '세션 삭제', message: '이 세션의 발언·요약·후속조치 기록을 모두 삭제합니다. 되돌릴 수 없습니다.', ok: '삭제', danger: true });
  if (!ok) return;
  try {
    await api(`sessions/${enc(SID)}`, { method: 'DELETE' });
    location.href = '/';
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

function renderRosterLists() {
  const names = [...new Map(S.roster.map((m) => [m.name, m])).values()];
  const dl = $('#roster-list');
  setKids(dl, ...names.map((m) => h('option', { value: m.name }, [m.role === 'chair' ? '위원장' : '', m.party].filter(Boolean).join(' '))));
}

/* ── 현재 질의 위원 카드 ──────────────────────────────────── */
function latestBlocks() {
  let lastQ = null, last = null;
  for (const b of S.blocks.values()) {
    if (!last || b.ts_start > last.ts_start || (b.ts_start === last.ts_start && b.id > last.id)) last = b;
    if (b.kind === 'question' && (!lastQ || b.ts_start > lastQ.ts_start || (b.ts_start === lastQ.ts_start && b.id > lastQ.id))) lastQ = b;
  }
  return { lastQ, last };
}

let lastCurKey = null;
function renderCur() {
  const el = $('#cur-card');
  const { lastQ, last } = latestBlocks();
  const sessLive = S.session?.status === 'live';
  // 발언마다 카드를 통째로 다시 만들면 Tab 으로 옮긴 포커스가 사라진다 — 보이는 내용이 바뀔 때만 다시 그린다
  // (경과 시간은 1초 타이머가 #cur-timer 글자만 바꾼다)
  // 진행 중인 블록은 발언마다 끝 시각(duration_sec)이 늘어나므로, 그때는 길이를 비교하지 않는다(타이머가 경과를 센다)
  const openLive = sessLive && !!lastQ?.is_open;
  const corrected = !!stageBlockReview(lastQ);
  const key = [lastQ?.id, lastQ?.status, lastQ?.questioner, lastQ?.questioner_party, lastQ?.is_open, lastQ?.ts_start,
    openLive ? '' : lastQ?.duration_sec, sessLive, last?.id, last?.kind, last?.ts_start, corrected].join('|');
  if (key === lastCurKey && el.childElementCount) return;
  lastCurKey = key;
  setKids($('#h-cur'), icon('user', 'ic-sm'), sessLive || !lastQ ? '현재 질의' : '마지막 질의');
  if (!lastQ) {
    setKids(el, h('div', { class: 'small muted' }, last && last.kind === 'chair' ? '위원장 진행 중입니다. 질의가 시작되면 표시됩니다.' : '아직 질의가 시작되지 않았습니다.'));
    return;
  }
  const live = S.session?.status === 'live' && lastQ.is_open;
  const needs = lastQ.status === 'needs_name' || !lastQ.questioner;
  const st = BLOCK_STATUS[lastQ.status];
  setKids(el, 
    h('div', { class: 'cur-q' },
      h('div', { class: `avatar${needs ? ' warn' : ''}`, 'aria-hidden': 'true' }, needs ? '?' : initial(lastQ.questioner)),
      h('div', { style: { minWidth: 0 } },
        h('div', { class: 'nm' }, needs ? '성명 확인 필요' : `${lastQ.questioner} 위원`),
        h('div', { class: 'pt' }, lastQ.questioner_party || (needs ? '위원장 호명에서 이름을 찾지 못했습니다' : '')),
        h('div', { style: { marginTop: '4px' } }, corrected ? badge('AI 보정', 'b-violet badge-sm')
          : st ? badge(st.label, `${st.cls} badge-sm`) : null)),
      h('div', { class: 'timer' },
        h('b', { id: 'cur-timer' }, fmt.clock(live ? (Date.now() - lastQ.ts_start) / 1000 : lastQ.duration_sec)),
        h('span', null, live ? '경과' : '질의 시간'))),
    h('div', { class: 'row', style: { marginTop: '8px' } },
      h('button', { type: 'button', class: 'btn btn-sm', onclick: () => gotoBlock(lastQ.id) }, '블록으로 이동'),
      OP && needs ? h('button', { type: 'button', class: 'btn btn-sm btn-soft', onclick: () => assignQuestioner(lastQ.id) }, icon('edit', 'ic-sm'), '질의자 지정') : null,
      sessLive && last && last.kind === 'chair' && last.ts_start > lastQ.ts_start ? h('span', { class: 'small muted' }, '지금은 위원장 진행') : null));
  el.dataset.live = live ? String(lastQ.ts_start) : '';
}

/* ── 알림(우리 기관 언급) ─────────────────────────────────── */
// 브라우저 자동 재생 정책: 사용자가 화면을 누르기 전에 만든 AudioContext 는 멈춘(suspended) 채로 시작되고,
// 제스처 없이 부른 resume() 은 소용없다. 그래서 첫 클릭·키 입력 때 미리 깨워 두고, 그래도 막혀 있으면 안내한다.
let actx = null;
let soundWarned = false;
function audioCtx() {
  if (!actx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    actx = new AC();
  }
  return actx;
}
function unlockAudio() {
  try { const c = audioCtx(); if (c && c.state === 'suspended') c.resume().catch(() => {}); } catch { /* 무시 */ }
}
function armAudioUnlock() {
  window.addEventListener('pointerdown', unlockAudio, { once: true, capture: true });
  window.addEventListener('keydown', unlockAudio, { once: true, capture: true });
}
function warnSoundBlocked() {
  const b = $('#al-sound');
  if (b) b.title = '알림 소리 켜짐 — 브라우저가 소리를 막고 있습니다. 화면을 한 번 누르면 소리가 납니다.';
  if (soundWarned) return;
  soundWarned = true;
  toast('브라우저가 알림 소리를 막고 있습니다. 화면을 한 번 누르면 다음 알림부터 소리가 납니다.', { type: 'warn', timeout: 8000 });
}
function playTones(c) {
  const t = c.currentTime;
  [880, 1175].forEach((f, i) => {
    const o = c.createOscillator();
    const g = c.createGain();
    o.type = 'sine';
    o.frequency.value = f;
    const t0 = t + i * 0.17;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(0.22, t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.15);
    o.connect(g).connect(c.destination);
    o.start(t0);
    o.stop(t0 + 0.16);
  });
}
function beep() {
  try {
    const c = audioCtx();
    if (!c) return;
    if (c.state === 'running') { playTones(c); return; }
    const asked = performance.now();
    // 허용되면 바로 울리고(늦게 풀린 소리는 버린다), 잠시 뒤에도 막혀 있으면 안내한다
    c.resume().then(() => { if (performance.now() - asked < 1500) playTones(c); }).catch(() => {});
    setTimeout(() => { if (c.state !== 'running') warnSoundBlocked(); }, 400);
  } catch { /* 소리 실패 무시 */ }
}

const notified = new Set();
function notifyAlert(a) {
  if (!V.booted || S.alerts.has(a.id) || notified.has(a.id) || a.ack) return;
  notified.add(a.id);
  const snippet = (a.text || '').slice(0, 90);
  toast(`${a.speaker ? `${a.speaker}: ` : ''}${snippet}${(a.text || '').length > 90 ? '…' : ''}`, {
    type: 'alert', title: `우리 기관 언급 · ${a.keyword} (${a.time})`, timeout: 9000,
    actions: [{ label: '발언 보기', onClick: () => gotoUtt(a.utterance_id) }, { label: '확인', onClick: () => ackAlert(a.id) }],
  });
  announce(`우리 기관 언급: ${a.keyword}`);
  if (V.sound) beep();
  if (V.notify && 'Notification' in window && Notification.permission === 'granted' && (document.hidden || !document.hasFocus())) {
    try {
      const n = new Notification(`우리 기관 언급: ${a.keyword}`, { body: `${a.time} ${a.speaker || ''} ${snippet}`, tag: `gukgam-alert-${a.id}` });
      n.onclick = () => { window.focus(); gotoUtt(a.utterance_id); n.close(); };
    } catch { /* 알림 실패 무시 */ }
  }
}

function sortedAlerts() {
  const ts = (a) => S.utts.get(a.utterance_id)?.ts_start ?? a.id;
  return [...S.alerts.values()].sort((a, b) => ts(b) - ts(a));
}

function alertItem(a, { compact = false } = {}) {
  const u = S.utts.get(a.utterance_id);
  const text = normText(a.text || u?.text || '');
  // 서버는 글자 사이 공백을 허용해 언급을 잡는다('도로 공사' = '도로공사') — 강조도 같은 방식(mentionRanges)으로
  const ms = (u?.mentions || []).filter((m) => m.keyword === a.keyword);
  const ranges = mentionRanges(text, ms.length ? ms : [{ keyword: a.keyword }]);
  return h('li', { class: `alert-item${a.ack ? ' ack' : ''}` },
    h('div', { class: 'ah' }, h('span', { class: 't' }, a.time || ''), badge(a.keyword, 'b-red badge-sm'), h('b', { class: 'small' }, a.speaker || ''), a.ack ? h('span', { class: 'small faint' }, '확인함') : null),
    h('div', { class: `ab${compact ? ' clamp-3' : ''}` }, markedText(text, ranges)),
    h('div', { class: 'aa' },
      h('button', { type: 'button', class: 'btn btn-xs', onclick: () => gotoUtt(a.utterance_id) }, '발언 보기'),
      !a.ack ? h('button', { type: 'button', class: 'btn btn-xs btn-soft', onclick: () => ackAlert(a.id) }, icon('check', 'ic-sm'), '확인') : null));
}

function renderAlertFeed() {
  const el = $('#al-feed');
  const unack = sortedAlerts().filter((a) => !a.ack);
  if (!unack.length) {
    setKids(el, h('li', { class: 'small muted' }, S.alerts.size ? `새 언급이 없습니다(전체 ${S.alerts.size}건 확인함).` : '아직 우리 기관 언급이 없습니다. 기관명·별칭·감시 키워드는 설정에서 정합니다.'));
    return;
  }
  setKids(el, ...unack.slice(0, 8).map((a) => alertItem(a, { compact: true })),
    h('li', { class: 'row-between' },
      h('button', { type: 'button', class: 'linkbtn small', onclick: () => selectTab('alerts') }, `전체 ${S.alerts.size}건 보기`),
      h('button', { type: 'button', class: 'btn btn-xs', onclick: () => ackAlert(null) }, '모두 확인')));
}

function renderAlertsTab() {
  const el = $('#al-list');
  const only = $('#al-unack').checked;
  const list = sortedAlerts().filter((a) => !only || !a.ack);
  if (!list.length) {
    setKids(el, h('li', null, emptyState(only ? '확인하지 않은 언급이 없습니다' : '아직 우리 기관 언급이 없습니다', '기관명·별칭·기관장·감시 키워드가 발언에 나오면 여기에 모입니다.', 'bell')));
    return;
  }
  setKids(el, ...list.map((a) => alertItem(a)));
}

async function ackAlert(id) {
  try {
    await api(`sessions/${enc(SID)}/alerts/ack`, { method: 'POST', body: id == null ? {} : { alert_id: id } });
    for (const a of S.alerts.values()) if (id == null || a.id === id) a.ack = true;
    dirty.alerts = true;
    scheduleFlush();
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

function setSound(on) {
  V.sound = on;
  store.set('sess.sound', on);
  if (on) soundWarned = false;
  const b = $('#al-sound');
  b.setAttribute('aria-pressed', String(on));
  setKids(b, icon(on ? 'volume' : 'volume-off', 'ic-sm'), h('span', { class: 'sr-only' }, '알림 소리'));
  b.title = on ? '알림 소리 켜짐' : '알림 소리 꺼짐';
}
async function setNotify(on) {
  if (on && 'Notification' in window && Notification.permission !== 'granted') {
    const p = await Notification.requestPermission().catch(() => 'denied');
    if (p !== 'granted') { toast('브라우저 알림이 허용되지 않았습니다. 브라우저 설정에서 허용하세요.', { type: 'warn' }); on = false; }
  }
  if (on && !('Notification' in window)) { toast('이 브라우저는 알림을 지원하지 않습니다.', { type: 'warn' }); on = false; }
  V.notify = on;
  store.set('sess.notify', on);
  const b = $('#al-notify');
  b.setAttribute('aria-pressed', String(on));
  setKids(b, icon(on ? 'bell' : 'bell-off', 'ic-sm'), h('span', { class: 'sr-only' }, '브라우저 알림'));
  b.title = on ? '브라우저 알림 켜짐(다른 창을 보고 있을 때)' : '브라우저 알림 꺼짐';
}

/* ── 주요 이슈·요약 ───────────────────────────────────────── */
function summaryExplanation(summary) {
  const origin = summaryPresentation(summary);
  return h('p', { class: 'small muted summary-origin-note' },
    [origin.note, origin.coverageLabel, origin.coverageNote].filter(Boolean).join(' '));
}

/** 회의 전체 요약과 실시간 요약 중 최신 것(일시정지 때 만든 전체 요약에 이슈가 고정되지 않게) */
function latestSummary() {
  return newerSummary(summaryContent(S.summary), summaryContent(S.rolling));
}

function renderIssues() {
  const c = latestSummary();
  const issues = (c && c.issues) || [];
  const tags = (c && c.hashtags) || [];
  const det = $('#issues');
  det.hidden = !issues.length && !tags.length;
  if (det.hidden) return;
  $('#iss-n').textContent = String(issues.length);
  $('#iss-time').textContent = c._time ? `${c._time} 요약` : '';
  setKids($('#iss-list'), ...issues.map((it) => {
    const u = it.utterance_id ? S.utts.get(it.utterance_id) : null;
    const b = it.block_id ? S.blocks.get(it.block_id) : null;
    return h('li', null, h('button', { type: 'button', onclick: () => (it.utterance_id ? gotoUtt(it.utterance_id) : (it.block_id ? gotoBlock(it.block_id) : null)) },
      h('span', null, it.title || it.text || ''), h('span', { class: 't' }, u ? u.time : (b ? fmt.hms(b.ts_start) : ''))));
  }));
  setKids($('#iss-tags'), ...tags.map((t) => h('button', { type: 'button', class: 'chip chip-tag', dataset: { act: 'tag', tag: t } }, t.startsWith('#') ? t : `#${t}`)));
}

function renderRolling() {
  const c = summaryContent(S.rolling);
  const el = $('#rolling');
  $('#roll-time').textContent = c && c._time ? c._time : '';
  if (!c || !c.overview) {
    setKids(el, h('span', { class: 'small muted' }, S.session?.status === 'live' ? '수집 중에는 몇 분마다 지금까지의 흐름을 요약합니다.' : '실시간 요약이 없습니다.'));
    return;
  }
  setKids(el, badge(summaryPresentation(c).label, 'b-gray badge-sm'),
    h('p', { class: 'clamp-3', style: { margin: 0 }, title: c.overview }, c.overview), summaryExplanation(c),
    h('button', { type: 'button', class: 'linkbtn small', onclick: () => selectTab('summary') }, '요약 탭에서 보기'));
}

function summaryPlain(c) {
  const lines = [`[회의 요약] ${S.session?.title || ''}`, ''];
  if (c.overview) lines.push(c.overview, '');
  if (c.issues?.length) { lines.push('■ 주요 이슈'); c.issues.forEach((x, i) => lines.push(` ${i + 1}. ${x.title}`)); lines.push(''); }
  if (c.org_points?.length) { lines.push('■ 우리 기관 관련'); c.org_points.forEach((x) => lines.push(` - ${x}`)); lines.push(''); }
  if (c.next_actions?.length) { lines.push('■ 후속조치 제안'); c.next_actions.forEach((x) => lines.push(` - ${x}`)); lines.push(''); }
  if (c.hashtags?.length) lines.push(c.hashtags.map((t) => (t.startsWith('#') ? t : `#${t}`)).join(' '));
  const origin = summaryPresentation(c);
  lines.push('', `※${origin.label} — ${origin.note}`);
  if (origin.coverageNote) lines.push([origin.coverageLabel, origin.coverageNote].filter(Boolean).join(' · '));
  return lines.join('\n');
}

function renderSummaryTab() {
  summaryStale = false;
  const box = $('#sum-box');
  const c = summaryContent(S.summary);
  const nodes = [];
  const head = h('div', { class: 'row-between' },
    h('h2', null, '회의 전체 요약'),
    h('div', { class: 'row' },
      OP ? h('button', { type: 'button', class: 'btn btn-sm', id: 'sum-redo', onclick: (e) => summarizeSession(e.currentTarget) }, icon('refresh', 'ic-sm'), c ? '다시 요약' : '요약 만들기') : null,
      c ? h('button', { type: 'button', class: 'btn btn-sm', onclick: () => copyText(summaryPlain(c), '요약을 복사했습니다.') }, icon('copy', 'ic-sm'), '복사') : null,
      h('a', { class: 'btn btn-sm btn-primary', href: `/s/${enc(SID)}/report` }, icon('report', 'ic-sm'), '상황보고')));
  nodes.push(head);
  if (!c) {
    nodes.push(h('div', { class: 'card' }, emptyState('아직 회의 전체 요약이 없습니다',
      OP ? '[요약 만들기]를 누르거나 수집을 종료하면 자동으로 만들어집니다.' : '수집이 끝나면 자동으로 만들어집니다.', 'sparkles')));
  } else {
    const origin = summaryPresentation(c);
    const card = h('div', { class: 'card sum-sec' },
      h('div', { class: 'row' }, badge(origin.label, origin.kind === 'ai' ? 'b-violet' : 'b-gray'),
        origin.coverageLabel ? badge(origin.coverageLabel, 'b-outline') : null,
        c._time ? h('span', { class: 'small muted' }, `${c._time} 생성`) : null),
      summaryExplanation(c),
      c.overview ? h('p', { class: 'overview' }, c.overview) : null,
      c.issues?.length ? h('div', null, h('h3', null, `주요 이슈 ${c.issues.length}건`), h('ol', { class: 'iss-list', style: { marginTop: '8px' } },
        c.issues.map((it) => h('li', null, h('button', { type: 'button', onclick: () => (it.utterance_id ? gotoUtt(it.utterance_id) : gotoBlock(it.block_id)) },
          h('span', null, it.title), h('span', { class: 't' }, S.utts.get(it.utterance_id)?.time || '')))))) : null,
      c.org_points?.length ? h('div', { class: 'note info' }, h('h3', { style: { marginBottom: '6px' } }, '우리 기관 관련 포인트'), h('ul', null, c.org_points.map((x) => h('li', null, x)))) : null,
      c.next_actions?.length ? h('div', null, h('h3', null, '후속조치 제안'), h('ul', null, c.next_actions.map((x) => h('li', null, x)))) : null,
      c.hashtags?.length ? h('div', { class: 'chips' }, c.hashtags.map((t) => h('button', { type: 'button', class: 'chip chip-tag', dataset: { act: 'tag', tag: t } }, t.startsWith('#') ? t : `#${t}`))) : null,
      h('p', { class: 'small faint', style: { margin: 0 } }, `※${origin.label} · ${NOTICE}`));
    nodes.push(card);
  }
  const qb = [...S.blocks.values()].filter((b) => b.kind === 'question').sort((a, b) => a.ts_start - b.ts_start);
  if (qb.length) {
    nodes.push(h('h3', { style: { marginTop: '20px' } }, `질의 블록별 요약 (${qb.length})`));
    nodes.push(h('div', { class: 'bsum-list', style: { marginTop: '8px' } }, qb.map((b) => {
      const sm = window.gukgamTranscriptStages?.usesStoredDisplay?.() === true
        ? window.gukgamTranscriptStages.displaySummary(b) : blockSummary(b);
      return h('div', { class: 'bsum-row' },
        h('button', { type: 'button', class: 'linkbtn mono small', onclick: () => gotoBlock(b.id) }, b.time_range),
        h('div', null, h('b', null, b.title), h('div', { class: sm ? '' : 'muted' }, sm?.one_line || '요약 전')),
        h('div', { class: 'row' }, sm && b.review_status === 'reviewed' ? badge('검수 완료', 'b-green badge-sm') : null,
          sm ? badge(summaryPresentation(sm).label, 'b-gray badge-sm', summaryPresentation(sm).note) : null,
          sm && sm.edited_by ? badge('수정됨', 'b-blue badge-sm', editNote(sm)) : null,
          sm ? orgRelevanceBadge(sm.org_relevance) : null));
    })));
  }
  setKids(box, ...nodes);
}

async function summarizeSession(btn) {
  setBusy(btn, true);
  toast('회의 전체 요약을 만드는 중입니다. 잠시 기다려 주세요.', { timeout: 3000 });
  try {
    const r = await api(`sessions/${enc(SID)}/summarize`, { method: 'POST' });
    if (r && Object.keys(r).length) enqueue('summary.update', { scope: 'session', summary: r });
    toast('회의 전체 요약을 갱신했습니다.', { type: 'ok' });
  } catch (e) { toast(errMsg(e), { type: 'err', title: '요약 실패' }); }
  finally { setBusy(btn, false); }
}

function renderAnalysis() {
  const a = S.analysis;
  const el = $('#ai-status');
  if (!a) { setKids(el, h('span', null, 'AI 요약은 질의 블록이 끝나면 자동으로 만들어집니다.')); return; }
  setKids(el, h('i', { class: `dot ${a.running ? 'live' : 'ok'}` }),
    h('span', null, `${a.provider === 'local_mlx' ? 'MLX' : `엔진 ${a.provider || '-'}`}${a.queue ? ` · 대기 ${a.queue}건` : ''}${a.running ? ` · 사용 중: ${a.running}` : ' · 대기 중'}`));
  const recent = a.last_activity;
  if (recent?.finished_at && recent.session_id === SID) {
    const state = { completed: '응답 완료', failed: '실패', cancelled: '취소' }[recent.status] || '종료';
    el.append(h('span', { class: 'small muted' }, `최근 모델 호출 ${fmt.hms(recent.finished_at)} · ${state} (${(Number(recent.latency_ms || 0) / 1000).toFixed(1)}초)`));
  }
}

/* ── 위원별 ──────────────────────────────────────────────── */
function renderMembers() {
  membersStale = false;
  const el = $('#mem-list');
  const restoreMemberFocus = window.gukgamTranscriptStages?.preserveMemberFocus?.(el);
  const qb = [...S.blocks.values()].filter((b) => b.kind === 'question').sort((a, b) => a.ts_start - b.ts_start);
  if (!qb.length) { setKids(el, emptyState('아직 질의 블록이 없습니다', '위원장이 질의 위원을 호명하면 블록이 만들어집니다.', 'users')); return; }
  const byName = new Map();
  for (const b of qb) {
    const key = b.questioner || '';
    if (!byName.has(key)) byName.set(key, { name: key, party: b.questioner_party, blocks: [], sec: 0 });
    const g = byName.get(key);
    g.blocks.push(b);
    g.sec += b.duration_sec || 0;
    if (!g.party && b.questioner_party) g.party = b.questioner_party;
  }
  const list = [...byName.values()].sort((a, b) => (a.name ? 0 : 1) - (b.name ? 0 : 1) || a.blocks[0].ts_start - b.blocks[0].ts_start);
  setKids(el, ...list.map((g) => {
    const named = !!g.name;
    const modernBlocks = g.blocks.map((b) => window.gukgamTranscriptStages?.memberDetails?.(b,
      [...S.utts.values()].filter((u) => u.block_id === b.id).sort(cmpUtt)));
    const modern = modernBlocks.every(Boolean);
    return h('div', { class: 'mem' },
      h('div', { class: `avatar${named ? '' : ' warn'}`, 'aria-hidden': 'true' }, named ? initial(g.name) : '?'),
      h('div', { style: { minWidth: 0 } },
        h('div', { class: 'nm' }, named ? `${g.name} 위원` : '※성명 확인 필요 위원', g.party ? h('span', { class: 'small muted', style: { fontWeight: 500, marginLeft: '6px' } }, g.party) : null),
        h('div', { class: 'meta' }, `질의 ${g.blocks.length}회 · 총 ${fmt.dur(g.sec)}`),
        h('div', { class: modern ? 'blinks transcript-member-blocks' : 'blinks' }, g.blocks.map((b, index) => h('div', { class: modern ? 'transcript-member-slot' : 'row', style: { gap: '2px' } },
          modern ? modernBlocks[index] : h('button', { type: 'button', class: 'chip', onclick: () => gotoBlock(b.id), title: b.title }, b.time_range),
          OP && !named ? h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', title: '질의자 지정', 'aria-label': `${b.time_range} 질의자 지정`, onclick: () => assignQuestioner(b.id) }, icon('edit', 'ic-sm')) : null)))),
      named && !modern ? h('div', { class: 'acts' },
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { setFilter({ questioner: g.name }); selectTab('feed'); } }, icon('filter', 'ic-sm'), '질의만 보기'),
        h('button', { type: 'button', class: 'btn btn-sm', 'aria-pressed': String(V.emph === `${g.name} 위원`), onclick: () => { setFilter({ emph: V.emph === `${g.name} 위원` ? '' : `${g.name} 위원` }); selectTab('feed'); } }, '발언 강조')) : null);
  }));
  restoreMemberFocus?.();
}
window.gukgamTranscriptMembersRefresh = () => { membersStale = true; if (V.tab === 'members') renderMembers(); };
window.gukgamTranscriptGoFeed = () => selectTab('feed');

/* ── 후속조치 탭 ─────────────────────────────────────────── */
function kindOptions(short = false) {
  return Object.entries(ITEM_KINDS).map(([k, v]) => h('option', { value: k }, `${v.icon} ${short ? v.short : v.label}`));
}

let itemsPendingRender = false;
function renderItemsTab() {
  const tbody = $('#it-body');
  if (tbody.contains(document.activeElement)) { itemsPendingRender = true; return; } // 편집 중이면 미룬다
  itemsStale = false;
  itemsPendingRender = false;
  const kind = $('#it-kind').value;
  const status = $('#it-status').value;
  const hideRitual = $('#it-hide-ritual').checked;
  const ts = (it) => S.utts.get(it.utterance_id)?.ts_start ?? it.updated_at ?? 0;
  const list = [...S.items.values()]
    .filter((it) => (!kind || it.kind === kind) && (status ? it.status === status : it.status !== 'dismissed') && !(hideRitual && it.strength === '의례'))
    .sort((a, b) => ts(a) - ts(b) || a.id - b.id);
  const depts = new Set(S.items.size ? [...S.items.values()].map((i) => i.assignee_dept).filter(Boolean) : []);
  for (const d of extraDepts) depts.add(d);
  setKids($('#dept-list'), ...[...depts].sort().map((d) => h('option', { value: d })));
  setKids($('#due-list'), ...DUE_PRESETS.map((d) => h('option', { value: d })));
  if (!list.length) {
    setKids(tbody, h('tr', null, h('td', { colspan: 6 }, emptyState(S.items.size ? '조건에 맞는 항목이 없습니다' : '아직 추출된 후속조치가 없습니다',
      '요구자료·약속·지적사항은 발언에서 자동으로 추출됩니다. 발언 메뉴의 ‘후속조치로 등록’으로 직접 추가할 수도 있습니다.', 'checklist'))));
    return;
  }
  setKids(tbody, ...list.map(itemRow));
}

function itemRow(it) {
  const k = ITEM_KINDS[it.kind] || { label: it.kind_label, icon: '' };
  const strengths = STRENGTHS[it.kind] || [];
  // 비교·복구는 렌더 당시 객체가 아니라 최신 상태로(편집 중에는 다시 그리기를 미루므로 클로저의 it 은 낡을 수 있다)
  const latest = () => S.items.get(it.id) || it;
  const patch = (field) => async (e) => {
    const v = e.target.value;
    const cur = latest();
    if ((cur[field] ?? '') === v) return;
    try {
      const r = await api(`items/${it.id}`, { method: 'PATCH', body: { [field]: v } });
      applyItem(r && r.id ? r : { ...cur, [field]: v, edited: true });
      scheduleFlush();
    } catch (err) { toast(errMsg(err), { type: 'err' }); e.target.value = latest()[field] ?? ''; }
  };
  const tr = h('tr', { class: it.strength === '의례' ? 'dim' : null, dataset: { iid: String(it.id) } },
    h('td', { class: 'nowrap mono' }, it.utterance_id ? h('button', { type: 'button', class: 'linkbtn', onclick: () => gotoUtt(it.utterance_id) }, it.time || '발언') : (it.time || '-')),
    h('td', null, h('div', { class: 'cell-stack' },
      OP ? h('select', { class: 'input-sm', value: it.kind, 'aria-label': '종류', onchange: patch('kind') }, kindOptions(true))
        : h('span', { class: `chip k-${it.kind}` }, `${k.icon} ${k.label}`),
      OP && strengths.length ? h('select', { class: 'input-sm', 'aria-label': '강도', value: it.strength || '', onchange: patch('strength') }, h('option', { value: '' }, '-'), strengths.map((s) => h('option', { value: s }, s)))
        : (it.strength ? badge(it.strength, 'b-outline badge-sm') : h('span', { class: 'faint' }, '-')))),
    h('td', { class: 'wrap' },
      h('div', null, it.text, ' ', badge(ITEM_ORIGIN[it.origin] || it.origin, it.origin === 'llm' ? 'b-violet badge-sm' : 'b-gray badge-sm', it.origin === 'llm' ? 'AI 추출(검수 필요)' : '추출 경로'),
        it.edited ? h('span', { class: 'item-edited', title: '사람이 고친 항목 — AI 재요약·자동 갱신이 덮어쓰지 않습니다' }, '수정됨') : null,
        OP ? h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', title: '요지·메모 수정', 'aria-label': '요지·메모 수정', onclick: () => editItemText(latest()) }, icon('edit', 'ic-sm')) : null),
      it.quote && it.quote !== it.text ? h('div', { class: 'small muted clamp-2', title: it.quote }, `“${it.quote}”`) : null,
      it.note ? h('div', { class: 'small', style: { color: 'var(--primary-text)' } }, `메모: ${it.note}`) : null),
    h('td', { class: 'small' }, it.requester || it.speaker || '-'),
    h('td', null, h('div', { class: 'cell-stack' },
      OP ? h('input', { class: 'input-sm', type: 'text', list: 'dept-list', value: it.assignee_dept, placeholder: '담당 부서', 'aria-label': '담당 부서', onchange: patch('assignee_dept') }) : h('span', null, it.assignee_dept || '담당 미정'),
      OP ? h('input', { class: 'input-sm', type: 'text', list: 'due-list', value: it.due_date, placeholder: '기한', 'aria-label': '기한', onchange: patch('due_date') }) : h('span', { class: 'small muted' }, it.due_date ? `기한 ${it.due_date}` : ''))),
    h('td', null, OP
      ? h('select', { class: 'input-sm', 'aria-label': '상태', value: it.status, onchange: patch('status') }, Object.entries(ITEM_STATUS).map(([v, l]) => h('option', { value: v }, l)))
      : badge(it.status_label || ITEM_STATUS[it.status], it.status === 'done' ? 'b-green' : it.status === 'in_progress' ? 'b-blue' : 'b-gray')));
  return tr;
}

async function editItemText(it) {
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
    applyItem(r && r.id ? r : { ...(S.items.get(it.id) || it), ...vals, edited: true });
    scheduleFlush();
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

async function addItem(u = null) {
  const b = u?.block_id ? S.blocks.get(u.block_id) : null;
  const vals = await formDialog({
    title: '후속조치 등록',
    description: u ? `${u.time} ${speakerLabel(u)} 발언에 연결합니다.` : '',
    fields: [
      { name: 'kind', label: '종류', type: 'select', value: u?.speaker_role === 'answer' ? 'commitment' : 'data_request', options: Object.entries(ITEM_KINDS).map(([k, v]) => [k, `${v.icon} ${v.label}`]) },
      { name: 'text', label: '요지', type: 'textarea', rows: 3, required: true, value: u ? normText(u.text).slice(0, 120) : '' },
      { name: 'requester', label: '요구 위원', value: b && b.kind === 'question' ? b.questioner : '', list: S.roster.map((m) => m.name) },
      { name: 'assignee_dept', label: '담당 부서', list: [...new Set([...S.items.values()].map((i) => i.assignee_dept).filter(Boolean).concat(extraDepts))] },
      { name: 'due_date', label: '기한', list: DUE_PRESETS },
    ],
    submit: '등록',
  });
  if (!vals) return;
  try {
    const body = { session_id: SID, kind: vals.kind, text: vals.text.trim(), requester: vals.requester.trim(), assignee_dept: vals.assignee_dept.trim(), due_date: vals.due_date.trim() };
    if (u) { body.utterance_id = u.id; if (u.block_id) body.block_id = u.block_id; }
    const r = await api('items', { method: 'POST', body });
    if (r && r.id) { applyItem(r); scheduleFlush(); }
    toast('후속조치를 등록했습니다.', { type: 'ok' });
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

let extraDepts = [];
async function loadDepartments() {
  if (!OP) return;
  try {
    const st = await api('settings');
    extraDepts = (st.departments || []).map((d) => d.name).filter(Boolean);
    dirty.items = true;
    scheduleFlush();
  } catch { /* 무시 */ }
}

function renderTabCounts() {
  const members = new Set([...S.blocks.values()].filter((b) => b.kind === 'question' && b.questioner).map((b) => b.questioner));
  $('#cnt-members').textContent = String(members.size);
  $('#cnt-items').textContent = String([...S.items.values()].filter((i) => i.status !== 'dismissed').length);
  const unack = [...S.alerts.values()].filter((a) => !a.ack).length;
  const c = $('#cnt-alerts');
  c.textContent = String(unack || S.alerts.size);
  c.classList.toggle('hot', unack > 0);
  c.title = unack ? `확인하지 않은 언급 ${unack}건` : `전체 ${S.alerts.size}건`;
}

/* ── 탭 ──────────────────────────────────────────────────── */
const TABS = ['feed', 'members', 'items', 'alerts', 'summary'];
function selectTab(name, focus = false) {
  cancelPendingDisplayNavigation();
  V.tab = name;
  window.gukgamTranscriptMainTab = name;
  window.gukgamTranscriptStages?.setMainTab(name);
  for (const t of TABS) {
    const btn = $(`#tb-${t}`);
    const on = t === name;
    btn.setAttribute('aria-selected', String(on));
    btn.tabIndex = on ? 0 : -1;
    $(`#tab-${t}`).hidden = !on;
    if (on && focus) btn.focus();
  }
  $('#fab').hidden = name !== 'feed';
  if (S.session) $('#scrubber').hidden = name !== 'feed';
  if (name === 'members' && membersStale) renderMembers();
  if (name === 'items' && (itemsStale || itemsPendingRender)) renderItemsTab();
  if (name === 'alerts') renderAlertsTab();
  if (name === 'summary' && summaryStale) renderSummaryTab();
  if (name === 'summary') loadBlockSummaries();
  if (name !== 'feed') $('#to-latest').hidden = true; else updateToLatest();
}

/* ── 검색·거르기 ─────────────────────────────────────────── */
function rowMatches(u) {
  const text = stageSourceText(u);
  if (text === null) return false;
  if (V.bmOnly && !S.bookmarks.has(u.id)) return false;
  const stored = !originalDisplayChoice(u) ? window.gukgamTranscriptStages?.displayUtterance?.(u.id) : null;
  const speakers = stored?.length ? stored.flatMap((row) => [row.speaker, speakerLabel(row)])
    : u.review_parts?.length ? u.review_parts.map((part) => speakerLabel({ speaker: part.speaker, speaker_role: part.role })) : [speakerLabel(u), u.speaker];
  if (V.speaker && !speakers.includes(V.speaker) && !(V.speaker === '여러 화자' && u.review_parts?.length > 1)) return false;
  if (V.questioner) {
    const b = u.block_id ? S.blocks.get(u.block_id) : null;
    if (!b || b.questioner !== V.questioner) return false;
  }
  if (V.terms.length) {
    if (u.speaker_role === 'system') return false;
    const names = displayReviewParts(u).map(reviewRoleName).join(' ');
    const hay = `${text} ${speakers.join(' ')} ${names}`.toLowerCase();
    if (!V.terms.every((t) => hay.includes(t.toLowerCase()))) return false;
  }
  return true;
}
const filtering = () => V.bmOnly || !!V.speaker || !!V.questioner || V.terms.length > 0;

function applyFilterRow(li, u) {
  const hide = stageSourceText(u) === null || filtering() && !rowMatches(u);
  if (li.hidden !== hide) {
    li.hidden = hide;
    const g = li._gkey ? groupMap.get(li._gkey) : null;
    if (g) dirtyGroups.add(g);
  }
}

// 검색어 하이라이트(<mark class="hl">)가 입혀진 행. 여기에 없는 행은 하이라이트가 없다(renderRow 가 늘 등록).
let markedRows = new Set();
function applyFilters({ rerenderMarks = false } = {}) {
  let n = 0;
  const prevMarked = markedRows;
  if (rerenderMarks) markedRows = new Set(); // 다시 그리는 행은 renderRow 가 새 검색어 기준으로 다시 등록한다
  for (const id of order) {
    const u = S.utts.get(id);
    const li = rowEl.get(id);
    if (!u || !li) continue;
    const match = stageSourceText(u) !== null && (!filtering() || rowMatches(u));
    // 새 검색어에 맞는 행과, 옛 하이라이트가 남아 있는 행(숨은 행 포함)을 모두 다시 그린다
    if (rerenderMarks && ((match && V.terms.length) || prevMarked.has(id))) renderRow(u);
    if (li.hidden !== !match) li.hidden = !match;
    if (match && filtering() && u.speaker_role !== 'system') n += 1;
    if (V.emph !== undefined) li.classList.toggle('emph', !!V.emph && (speakerLabel(u) === V.emph || u.speaker === V.emph));
  }
  for (const g of groups) refreshGroupVisibility(g);
  syncStoredLooseRows();
  for (const g of groups) if (g.stageProjection) {
    renderStageBlock(g, S.blocks.get(g.id), g.stageProjection);
    refreshGroupVisibility(g);
  }
  visGroupsCache = null;
  $('#q-count').textContent = filtering() ? `${fmt.num(resultCount())}건` : '';
  renderFilterChips();
  scheduleScrubPos();
}

/** 거르기 건수만 다시 센다(검색 중 새 발언이 들어왔을 때) */
function renderQCount() {
  $('#q-count').textContent = filtering() ? `${fmt.num(resultCount())}건` : '';
}

function resultCount() {
  let n = 0;
  for (const id of order) {
    const u = S.utts.get(id);
    const group = u?.block_id ? groupMap.get(`b${u.block_id}`) : null;
    if (group?.stageProjection && !aiOriginalChoice.get(group.id) && !stageClaimsReady) continue;
    if (u && u.speaker_role !== 'system' && rowMatches(u)) n += 1;
  }
  for (const g of groups) if (g.stageProjection && !aiOriginalChoice.get(g.id)) {
    n += g.stageProjection.cards.filter((card) => stageCardMatches(card, S.blocks.get(g.id))).length;
  }
  for (const g of groups) if (g.type === 'display' && stageCardMatches(g.displayRow, S.blocks.get(g.displayRow.block_id))) n += 1;
  return n;
}

function setFilter(patch) {
  cancelPendingDisplayNavigation();
  window.gukgamTranscriptStages?.showLegacy();
  const termsChanged = 'q' in patch && patch.q !== V.q;
  Object.assign(V, patch);
  if ('q' in patch) {
    V.terms = splitTerms(V.q);
    // 입력창은 내용이 실제로 다를 때만 고쳐 쓴다 — 끝 공백만 다르면 두어야 '도로 하도급'처럼 띄어 쓴 여러 낱말을 칠 수 있다
    if ($('#q').value.trim() !== V.q) $('#q').value = V.q;
  }
  $('#f-bm').setAttribute('aria-pressed', String(V.bmOnly));
  if (filtering() && V.autoscroll) setAutoscroll(false);
  applyFilters({ rerenderMarks: termsChanged });
  if (history.partial && ('q' in patch || 'bmOnly' in patch || 'questioner' in patch || 'speaker' in patch)) loadFilterHistory();
  if (V.tab !== 'feed' || patch.emph !== undefined && Object.keys(patch).length === 1) return;
  // 거르기를 바꾸면 첫 결과로, 모두 해제하면 보던(포커스) 발언으로 돌아간다
  if (filtering()) {
    const first = visibleRowsInDom()[0];
    if (first) scrollToRow(first, { center: false });
  } else if (V.focusId != null && rowEl.get(V.focusId)) {
    scrollToRow(displayRowForUid(V.focusId) || rowEl.get(V.focusId));
  }
}

async function loadFilterHistory() {
  const generation = ++history.filterGeneration;
  const query = V.q, bookmarked = V.bmOnly, questioner = V.questioner, speaker = V.speaker;
  if (!query && !bookmarked && !questioner && !speaker) return;
  $('#q-count').textContent = '필요한 이전 기록을 찾는 중…';
  try {
    if (questioner) await Promise.all([...S.blocks.values()].filter((b) => b.questioner === questioner)
      .map((b) => loadHistoryBlock(b.id)));
    else await loadHistory(query ? { q: query } : bookmarked ? { bookmarks_only: true }
      : { q: speaker.replace(/\s+(위원|위원장|답변)$/, '') });
    if (generation !== history.filterGeneration) return;
    applyFilters({ rerenderMarks: true });
  } catch (error) {
    if (generation !== history.filterGeneration) return;
    toast(errMsg(error), { type: 'err', title: '이전 기록 조회 실패' });
    renderQCount();
  }
}

function renderFilterChips() {
  const el = $('#f-chips');
  const chips = [];
  const chip = (label, clear) => h('span', { class: 'chip chip-tag' }, label,
    h('button', { type: 'button', class: 'chip-x', 'aria-label': `${label} 거르기 해제`, onclick: clear }, icon('x', 'ic-sm')));
  if (V.speaker) chips.push(chip(`화자: ${V.speaker}`, () => setFilter({ speaker: '' })));
  if (V.questioner) chips.push(chip(`${V.questioner} 위원 질의만`, () => setFilter({ questioner: '' })));
  if (V.emph) chips.push(chip(`강조: ${V.emph}`, () => setFilter({ emph: '' })));
  if (V.bmOnly) chips.push(chip('북마크만', () => setFilter({ bmOnly: false })));
  if (chips.length > 1 || (chips.length && V.q)) chips.push(h('button', { type: 'button', class: 'linkbtn small', onclick: clearFilters }, '모두 해제'));
  setKids(el, ...chips);
}

function clearFilters() {
  setFilter({ q: '', bmOnly: false, speaker: '', questioner: '', emph: '' });
}

/* ── 스크롤·포커스 ───────────────────────────────────────── */
let progUntil = 0;
let userIntentUntil = 0;
let lastY = window.scrollY;
// 스크러버를 키보드로 옮긴 시각(ms). 다른 경로로 화면이 움직이면 버린다(그때는 보이는 발언 시각에서 다시 센다)
let scrubKeyT = null;
let legacyViewportFrame = null;
function queueLegacyViewport() {
  if (!V.booted || V.tab !== 'feed' || !V.autoscroll || filtering()
      || window.gukgamTranscriptStages?.isActive() || legacyViewportFrame !== null) return;
  legacyViewportFrame = requestAnimationFrame(() => {
    legacyViewportFrame = null;
    if (V.tab === 'feed' && V.autoscroll && !filtering() && !window.gukgamTranscriptStages?.isActive()) scrollToBottom();
  });
}
if (typeof ResizeObserver === 'function') new ResizeObserver(queueLegacyViewport).observe(feed);
const markProgrammatic = () => { progUntil = performance.now() + 300; scrubKeyT = null; };
// 화면 아래를 가리는 고정 요소(스크러버·수기 입력 바) 높이
function bottomInset() {
  let px = 0;
  const sc = $('#scrubber');
  if (sc && !sc.hidden) px += sc.offsetHeight;
  const mb = $('#manual-bar');
  if (mb && !mb.hidden) px += mb.offsetHeight;
  return px;
}
// '맨 아래' = 문서 끝이 아니라 발언 목록의 끝(좁은 화면에서는 사이드바가 목록 아래에 온다)
const nearBottom = (px = 60) => feed.getBoundingClientRect().bottom <= window.innerHeight - bottomInset() + px;

function scrollToBottom() {
  if (window.gukgamTranscriptStages?.isActive()) return;
  markProgrammatic();
  const target = feed.getBoundingClientRect().bottom + window.scrollY - (window.innerHeight - bottomInset() - 16);
  window.scrollTo(0, Math.max(0, target));
}

function setAutoscroll(on) {
  cancelPendingDisplayNavigation();
  V.autoscroll = on;
  $('#t-auto').checked = on;
  if (on) { V.newSincePause = 0; scrollToBottom(); }
  updateToLatest();
}

function updateToLatest() {
  const btn = $('#to-latest');
  const show = !window.gukgamTranscriptStages?.isActive() && !V.autoscroll && V.tab === 'feed' && S.utts.size > 0;
  btn.hidden = !show;
  $('#to-latest-t').textContent = V.newSincePause ? `새 발언 ${V.newSincePause}건 · 최신 발언으로` : '최신 발언으로';
}

function goLatest() {
  if (filtering()) clearFilters();
  selectTab('feed');
  if (window.gukgamTranscriptStages?.isActive()) { window.gukgamTranscriptStages.followLatest(); return; }
  setAutoscroll(true);
  const last = order[order.length - 1];
  if (last) setFocus(last, { scroll: false });
}

function markUserScrollIntent(duration = 800) {
  cancelPendingDisplayNavigation();
  userIntentUntil = performance.now() + duration;
}
for (const ev of ['wheel', 'touchmove']) window.addEventListener(ev, () => markUserScrollIntent(), { passive: true });
window.addEventListener('keydown', (e) => { if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(e.key)) markUserScrollIntent(); });
document.addEventListener('pointerdown', (e) => { if (e.target === document.documentElement) markUserScrollIntent(2000); });
window.addEventListener('scroll', () => {
  const y = window.scrollY;
  if (window.gukgamTranscriptStages?.isActive()) { lastY = y; return; }
  const now = performance.now();
  if (now > progUntil) scrubKeyT = null; // 사용자가 직접 스크롤함
  if (V.tab === 'feed' && now > progUntil && now < userIntentUntil) {
    if (V.autoscroll && y < lastY - 2) setAutoscroll(false);
    else if (!V.autoscroll && !filtering() && y > lastY && nearBottom(30)) setAutoscroll(true);
  }
  lastY = y;
  scheduleScrubPos();
}, { passive: true });

function scrollToRow(li, { flash = false, center = true } = {}) {
  markProgrammatic();
  const opts = { block: center ? 'center' : 'start' };
  li.scrollIntoView(opts);
  // content-visibility 로 건너뛴 블록이 그려지며 높이가 바뀔 수 있어 한 번 더 맞춘다
  requestAnimationFrame(() => { markProgrammatic(); li.scrollIntoView(opts); });
  if (flash) { li.classList.remove('flash'); void li.offsetWidth; li.classList.add('flash'); }
}

function setFocus(id, { scroll = true, row = null } = {}) {
  cancelPendingDisplayNavigation();
  const prev = V.focusRow || (V.focusId != null ? rowEl.get(V.focusId) : null);
  if (prev) prev.classList.remove('focus');
  V.focusId = id;
  let li = rowEl.get(id);
  const u = S.utts.get(id);
  if (!li || !u) return;
  const g = u.block_id ? groupMap.get(`b${u.block_id}`) : null;
  const index = g?.stageProjection && !aiOriginalChoice.get(g.id)
    ? g.stageProjection.cards.findIndex((card) => card.source_utterance_ids.includes(id)) : -1;
  const corrected = row?.dataset.stageCard ? row : index >= 0 ? g.ai.children[0]?.children[index] : null;
  if (corrected && !corrected.hidden) li = corrected;
  else if (u.block_id && (li._liveAiDraft || g?.rows?.hidden)) setBlockOriginal(u.block_id, true);
  V.focusRow = li;
  li.classList.add('focus');
  V.selectedBlock = corrected ? Number(li.closest?.('.blk')?.dataset?.bid) || u.block_id : u.block_id || null;
  li.focus({ preventScroll: true });
  if (scroll) { markProgrammatic(); li.scrollIntoView({ block: 'nearest' }); }
}

async function gotoUtt(id, { flash = true } = {}) {
  const navigation = ++displayNavigation;
  pendingDisplayNavigation = 0;
  window.gukgamTranscriptStages?.showLegacy();
  id = Number(id);
  let li = rowEl.get(id);
  if (!li && history.partial) {
    setAutoscroll(false);
    try { await loadHistory({ around_utterance_id: id }); li = rowEl.get(id); }
    catch (error) {
      if (navigation === displayNavigation) toast(errMsg(error), { type: 'err', title: '발언 조회 실패' });
      return;
    }
  }
  if (navigation !== displayNavigation) return;
  if (!li) {
    toast('이 화면에 없는 발언입니다. 발언 상세로 이동합니다.', { timeout: 1800 });
    setTimeout(() => { if (navigation === displayNavigation) location.href = `/u/${id}`; }, 600);
    return;
  }
  const u = S.utts.get(id);
  for (const owner of window.gukgamTranscriptStages?.displayOwners?.(id) || []) setBlockOriginal(owner, true);
  if (u?.block_id) setBlockOriginal(u.block_id, true);
  selectTab('feed');
  if (li.hidden) clearFilters();
  setAutoscroll(false);
  setFocus(id, { scroll: false });
  scrollToRow(li, { flash });
}

function storedDisplayTarget(key) {
  for (const g of groups) {
    const index = g.stageProjection?.cards.findIndex((card) => card.key === key) ?? -1;
    if (index >= 0 && !aiOriginalChoice.get(g.id)) return g.ai.children[0]?.children[index] || null;
    if (g.type === 'display' && g.displayRow?.key === key) return g.displayLi;
  }
  for (const [uid, li] of rowEl) {
    const rows = window.gukgamTranscriptStages?.displayUtterance?.(uid);
    const index = rows?.findIndex((row) => row.key === key) ?? -1;
    if (index >= 0) return rows.length > 1 ? li._reviewPartNodes[index] || li : li;
  }
  return null;
}

async function gotoDisplay(key, { blockId = 0, utteranceId = 0, flash = true } = {}) {
  const navigation = ++displayNavigation;
  pendingDisplayNavigation = 0;
  window.gukgamTranscriptStages?.showLegacy();
  setAutoscroll(false);
  pendingDisplayNavigation = navigation;
  try {
    if (blockId) await loadHistory({ block_id: blockId }, { hydrate: false });
    else if (utteranceId && !rowEl.has(utteranceId)) await loadHistory({ around_utterance_id: utteranceId });
  } catch (error) {
    if (navigation === displayNavigation) toast(errMsg(error), { type: 'err', title: '보정 발언 조회 실패' });
    return;
  } finally {
    if (pendingDisplayNavigation === navigation) pendingDisplayNavigation = 0;
  }
  if (navigation !== displayNavigation) return;
  // 원행의 옛 질의 소속이 아니라 검색한 저장 말묶음의 질의·키를 따른다.
  if (blockId) setBlockOriginal(blockId, false);
  syncStoredLooseRows();
  selectTab('feed');
  let li = storedDisplayTarget(key);
  if (li?.hidden || filtering()) { clearFilters(); li = storedDisplayTarget(key); }
  if (!li) {
    toast('표시 판본이 바뀌어 해당 말묶음을 찾을 수 없습니다. 다시 검색해 주세요.', { type: 'warn' });
    return;
  }
  const prev = V.focusRow || rowEl.get(V.focusId);
  prev?.classList.remove('focus');
  V.focusId = utteranceId || null; V.focusRow = li; V.selectedBlock = blockId || null;
  li.classList.add('focus');
  li.setAttribute('tabindex', '-1');
  li.focus({ preventScroll: true });
  scrollToRow(li, { flash });
}

async function gotoBlock(bid) {
  const navigation = ++displayNavigation;
  pendingDisplayNavigation = 0;
  window.gukgamTranscriptStages?.showLegacy();
  if (history.partial) {
    setAutoscroll(false);
    try { await loadHistoryBlock(Number(bid)); }
    catch (error) {
      if (navigation === displayNavigation) toast(errMsg(error), { type: 'err', title: '질의 조회 실패' });
      return;
    }
  }
  if (navigation !== displayNavigation) return;
  const g = groupMap.get(`b${bid}`);
  if (!g) { toast('블록을 찾을 수 없습니다.', { type: 'warn' }); return; }
  selectTab('feed');
  if (g.el.hidden) clearFilters();
  setAutoscroll(false);
  V.selectedBlock = bid;
  markProgrammatic();
  g.el.scrollIntoView({ block: 'start' });
  requestAnimationFrame(() => {
    markProgrammatic();
    const top = g.el.getBoundingClientRect().top + window.scrollY - (stickyTop() + 8);
    window.scrollTo(0, Math.max(0, top));
  });
  const first = activeGroupRows(g)[0];
  if (first) { first.classList.remove('flash'); void first.offsetWidth; first.classList.add('flash'); }
}

function visibleRowsInDom() {
  return $$('.blk:not([hidden]) .u:not([hidden])', feed).filter((li) => {
    for (let node = li; node && node !== feed; node = node.parentNode) if (node.hidden) return false;
    return true;
  });
}

function activeGroupRows(g) {
  const rows = g.stageProjection && !aiOriginalChoice.get(g.id) ? g.ai.children[0]?.children : g.rows.children;
  return [...(rows || [])].filter((row) => !row.hidden);
}

function displayRowForUid(id) {
  const u = S.utts.get(id), g = u?.block_id ? groupMap.get(`b${u.block_id}`) : null;
  if (g?.stageProjection && !aiOriginalChoice.get(g.id)) {
    const index = g.stageProjection.cards.findIndex((card) => card.source_utterance_ids.includes(id));
    const row = index >= 0 ? g.ai.children[0]?.children[index] : null;
    return row && !row.hidden && !g.el.hidden ? row : null;
  }
  const row = rowEl.get(id);
  return row && !row.hidden && !g?.el.hidden ? row : null;
}

function moveFocus(delta) {
  const rows = visibleRowsInDom();
  if (!rows.length) return;
  let i = V.focusId != null ? rows.indexOf(V.focusRow || rowEl.get(V.focusId)) : -1;
  if (i < 0) {
    const r = topVisibleRow();
    i = r ? rows.indexOf(r) - (delta > 0 ? 1 : 0) : (delta > 0 ? -1 : rows.length);
  }
  const j = Math.max(0, Math.min(rows.length - 1, i + delta));
  const li = rows[j];
  if (V.autoscroll && delta < 0) setAutoscroll(false);
  setFocus(Number(li.dataset.uid), { row: li });
}

/* ── 타임라인 스크러버 ───────────────────────────────────── */
let visGroupsCache = null;
function visibleGroups() {
  if (!visGroupsCache) visGroupsCache = groups.filter((g) => !g.el.hidden);
  return visGroupsCache;
}

/** 화면 위쪽을 가리는 고정 영역의 아래 끝(상단 바, 고정된 발언 도구 줄) */
function stickyTop() {
  const bar = $('.topbar').getBoundingClientRect().bottom;
  if (getComputedStyle(feedHead).position !== 'sticky') return bar; // 좁은 화면: 도구 줄이 함께 스크롤된다
  return Math.max(bar, feedHead.getBoundingClientRect().bottom);
}

function topVisibleRow() {
  const top = stickyTop() + 4;
  const gs = visibleGroups();
  let lo = 0, hi = gs.length - 1, gi = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const r = gs[mid].el.getBoundingClientRect();
    if (r.bottom <= top) lo = mid + 1; else { gi = mid; hi = mid - 1; }
  }
  if (gi < 0) return null;
  for (const li of activeGroupRows(gs[gi])) {
    if (li.hidden) continue;
    if (li.getBoundingClientRect().bottom > top) return li;
  }
  return gs[gi + 1] ? activeGroupRows(gs[gi + 1])[0] || null : null;
}

function scrubRange() {
  if (!order.length) return null;
  const first = S.utts.get(order[0]);
  let t1 = 0;
  for (let i = order.length - 1; i >= Math.max(0, order.length - 20); i -= 1) {
    const u = S.utts.get(order[i]);
    t1 = Math.max(t1, u.ts_end || u.ts_start);
  }
  const t0 = first.ts_start;
  return { t0, t1: Math.max(t1, t0 + 1000) };
}

let scrubLastRange = '';
function renderScrubber() {
  const r = scrubRange();
  const sc = $('#scrubber');
  if (!r) { $('#scrub-start').textContent = ''; $('#scrub-end').textContent = ''; $('#scrub-ticks').replaceChildren(); return; }
  $('#scrub-start').textContent = fmt.hms(r.t0);
  $('#scrub-end').textContent = fmt.hms(r.t1);
  const key = `${r.t0}-${r.t1}-${S.blocks.size}-${S.alerts.size}`;
  if (key === scrubLastRange) return;
  scrubLastRange = key;
  const span = r.t1 - r.t0;
  const pct = (t) => `${Math.max(0, Math.min(100, ((t - r.t0) / span) * 100)).toFixed(3)}%`;
  const ticks = [];
  for (const b of S.blocks.values()) {
    if (b.kind !== 'question') continue;
    ticks.push(h('span', { class: `scrub-tick q${b.status === 'needs_name' ? ' needs' : ''}`, style: { left: pct(b.ts_start) }, title: `${b.title} ${b.time_range}` }));
  }
  for (const a of S.alerts.values()) {
    const u = S.utts.get(a.utterance_id);
    if (u) ticks.push(h('span', { class: 'scrub-tick alert', style: { left: pct(u.ts_start) }, title: `우리 기관 언급: ${a.keyword} ${u.time}` }));
  }
  setKids($('#scrub-ticks'), ...ticks);
  sc.dataset.t0 = String(r.t0);
  sc.dataset.t1 = String(r.t1);
  scheduleScrubPos();
}

let scrubPosQueued = false;
let dragging = false;
function scheduleScrubPos() {
  if (scrubPosQueued || dragging) return;
  scrubPosQueued = true;
  requestAnimationFrame(() => { scrubPosQueued = false; updateScrubPos(); });
}

function setThumb(t, r) {
  const f = Math.max(0, Math.min(1, (t - r.t0) / (r.t1 - r.t0)));
  $('#scrub-thumb').style.left = `${(f * 100).toFixed(3)}%`;
  $('#scrub-fill').style.width = `${(f * 100).toFixed(3)}%`;
  const tr = $('#scrub-track');
  tr.dataset.t = String(Math.round(t)); // 실제 위치(ms). aria-valuenow(정수 %)는 표시용
  tr.setAttribute('aria-valuenow', String(Math.round(f * 100)));
  tr.setAttribute('aria-valuetext', `${fmt.hms(t)} (${fmt.hms(r.t0)}~${fmt.hms(r.t1)})`);
  $('#scrub-cur').textContent = fmt.hms(t);
}

function updateScrubPos() {
  const r = scrubRange();
  if (!r || V.tab !== 'feed') return;
  let t;
  if (V.autoscroll) t = r.t1;
  else if (scrubKeyT != null) t = Math.max(r.t0, Math.min(r.t1, scrubKeyT));
  else if (nearBottom(10)) t = r.t1;
  else {
    const li = topVisibleRow();
    const u = li ? S.utts.get(Number(li.dataset.uid)) : null;
    t = li?.dataset.tsStart ? Number(li.dataset.tsStart) : u ? u.ts_start : r.t0;
  }
  setThumb(t, r);
}

function nearestUttAt(t) {
  let lo = 0, hi = order.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (S.utts.get(order[mid]).ts_start < t) lo = mid + 1; else hi = mid;
  }
  // 거르기로 숨은 발언은 건너뛰고 가장 가까운 보이는 발언
  for (let d = 0; d < order.length; d += 1) {
    for (const i of [lo - d, lo + d]) {
      if (i < 0 || i >= order.length) continue;
      const li = displayRowForUid(order[i]);
      if (li && !li.hidden && !(li.parentElement?.closest('.blk')?.hidden)) return order[i];
    }
    if (lo - d < 0 && lo + d >= order.length) break;
  }
  return null;
}

function scrubTo(frac, { final = false } = {}) {
  cancelPendingDisplayNavigation();
  const r = scrubRange();
  if (!r) return;
  frac = Math.max(0, Math.min(1, frac));
  const t = r.t0 + frac * (r.t1 - r.t0);
  setThumb(t, r);
  const tip = $('#scrub-tip');
  tip.hidden = !dragging;
  tip.style.left = `${(frac * 100).toFixed(2)}%`;
  tip.textContent = fmt.hms(t);
  if (frac >= 0.998) { if (final || !dragging) setAutoscroll(true); else scrollToBottom(); return; }
  if (V.autoscroll) setAutoscroll(false);
  const candidates = visibleRowsInDom();
  const li = candidates.reduce((best, row) => {
    const time = row.dataset.tsStart ? Number(row.dataset.tsStart) : S.utts.get(Number(row.dataset.uid))?.ts_start;
    const bestTime = best?.dataset.tsStart ? Number(best.dataset.tsStart) : S.utts.get(Number(best?.dataset.uid))?.ts_start;
    return Number.isFinite(time) && (!best || Math.abs(time - t) < Math.abs(bestTime - t)) ? row : best;
  }, null);
  if (li) { markProgrammatic(); li.scrollIntoView({ block: 'start' }); }
}

function initScrubber() {
  const track = $('#scrub-track');
  const fracOf = (e) => { const r = track.getBoundingClientRect(); return (e.clientX - r.left) / r.width; };
  track.addEventListener('pointerdown', (e) => {
    if (V.tab !== 'feed') selectTab('feed');
    dragging = true;
    try { track.setPointerCapture(e.pointerId); } catch { /* 이미 해제된 포인터 등 */ }
    scrubTo(fracOf(e));
  });
  track.addEventListener('pointermove', (e) => { if (dragging) scrubTo(fracOf(e)); });
  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    $('#scrub-tip').hidden = true;
    scrubTo(fracOf(e), { final: true });
  };
  track.addEventListener('pointerup', end);
  track.addEventListener('pointercancel', () => { dragging = false; $('#scrub-tip').hidden = true; });
  track.addEventListener('keydown', (e) => {
    const r = scrubRange();
    if (!r) return;
    // 현재 위치는 ms 로 읽는다(정수 % 로 되읽으면 3시간 넘는 회의에서 1분 걸음이 반올림에 묻혀 제자리)
    const shown = Number(track.dataset.t);
    const cur = scrubKeyT != null ? scrubKeyT : (Number.isFinite(shown) && shown ? shown : r.t1);
    const t = scrubKeyTarget(e.key, cur, r.t0, r.t1);
    if (t == null) return;
    e.preventDefault();
    scrubTo((t - r.t0) / (r.t1 - r.t0), { final: true });
    scrubKeyT = t >= r.t1 ? null : t; // 끝(최신)이면 자동 스크롤로 돌아간다
    setThumb(t, r);
  });
}

/* ── 행 메뉴·동작 ────────────────────────────────────────── */
async function toggleBookmark(id) {
  try {
    const r = await api(`utterances/${id}/bookmark`, { method: 'POST' });
    const on = r && typeof r.bookmarked === 'boolean' ? r.bookmarked : !S.bookmarks.has(id);
    if (on) S.bookmarks.add(id); else S.bookmarks.delete(id);
    store.set(`bm.${SID}`, [...S.bookmarks]);
    rerenderRow(id);
    if (V.bmOnly) applyFilters();
    toast(on ? '북마크했습니다.' : '북마크를 해제했습니다.', { type: 'ok', timeout: 1500 });
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

async function editSpeaker(u) {
  const names = [...new Set(S.roster.map((m) => m.name))];
  const vals = await formDialog({
    title: '화자 수정',
    description: `${u.time} 발언의 화자를 고칩니다. 고친 화자는 수집원 갱신으로 바뀌지 않습니다.`,
    fields: [
      { name: 'speaker', label: '화자 이름', value: u.speaker, list: names, placeholder: '예: 김남근 / 홍지선 국토교통부장관' },
      { name: 'speaker_role', label: '역할', type: 'select', value: ['chair', 'member', 'answer', 'other'].includes(u.speaker_role) ? u.speaker_role : 'other', options: [['member', '위원'], ['chair', '위원장'], ['answer', '답변(피감기관·증인)'], ['other', '기타']] },
    ],
  });
  if (!vals) return;
  try {
    const r = await api(`utterances/${u.id}`, { method: 'PATCH', body: { speaker: vals.speaker.trim(), speaker_role: vals.speaker_role } });
    if (r && r.id) enqueue('utterance.upsert', r);
    toast('화자를 고쳤습니다.', { type: 'ok', timeout: 1800 });
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

async function editText(u) {
  const vals = await formDialog({
    title: '본문 수정',
    description: '음성인식 오류를 바로잡습니다. 원문은 따로 보관되고, 감사 기록에 남습니다.',
    fields: [{ name: 'text', label: '본문', type: 'textarea', rows: 8, value: u.text, required: true }],
    wide: true,
  });
  if (!vals) return;
  try {
    const r = await api(`utterances/${u.id}`, { method: 'PATCH', body: { text: vals.text.trim() } });
    if (r && r.id) enqueue('utterance.upsert', r);
    toast('본문을 고쳤습니다.', { type: 'ok', timeout: 1800 });
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

async function assignQuestioner(bid) {
  const b = S.blocks.get(bid);
  if (!b) return;
  const members = S.roster.filter((m) => m.role !== 'chair' && m.role !== 'agency' && m.role !== 'witness');
  const vals = await formDialog({
    title: '질의 위원 지정',
    description: `${b.time_range} 질의 블록의 질의 위원을 확정합니다. 이 블록의 위원 발언 화자도 함께 바뀝니다.`,
    fields: [
      { name: 'questioner', label: '위원 이름', value: b.questioner, required: true, list: members.map((m) => [m.name, m.party || '']),
        onInput: (ctl, c) => { const m = S.roster.find((x) => x.name === ctl.value.trim()); if (m && m.party) c.questioner_party.value = m.party; } },
      { name: 'questioner_party', label: '정당', value: b.questioner_party, list: PARTIES },
    ],
    submit: '지정',
  });
  if (!vals) return;
  try {
    const r = await api(`blocks/${bid}`, { method: 'PATCH', body: { questioner: vals.questioner.trim(), questioner_party: vals.questioner_party.trim() } });
    if (r && r.id) enqueue('block.upsert', r);
    toast(`${vals.questioner.trim()} 위원 질의로 지정했습니다.`, { type: 'ok' });
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

async function summarizeBlock(bid, btn) {
  const sm = blockSummary(S.blocks.get(bid));
  if (sm && sm.edited_by) {
    // 사람이 고친 요약은 자동 재요약이 덮어쓰지 않는다 — 수동 재요약일 때만, 확인을 받고 덮어쓴다
    const ok = await confirmDialog({
      title: '고친 요약 덮어쓰기',
      message: `이 블록 요약은 ${editNote(sm)} 사람이 고쳤습니다. 다시 요약하면 고친 내용이 AI 요약으로 바뀝니다. 계속할까요?`,
      ok: '다시 요약', danger: true,
    });
    if (!ok) return;
  }
  setBusy(btn, true);
  try {
    const r = await api(`blocks/${bid}/summarize`, { method: 'POST' });
    if (r && r.id) enqueue('block.upsert', r);
    const state = r?.summary_state;
    if (!state) toast('블록 요약을 만들었습니다.', { type: 'ok', timeout: 2000 });
    else {
      const messages = { ready: '저장된 의원별 요약을 불러왔습니다.', processing: '의원별 보정·요약을 처리 중입니다.',
        waiting: '의원별 보정·요약이 완료되면 표시합니다.', held: '의원별 요약을 확인해야 합니다.', protected: '사람이 확인한 요약을 유지합니다.' };
      toast(state.state === 'ready' ? messages.ready : state.detail || messages[state.state] || messages.waiting,
        { type: state.state === 'ready' || state.state === 'protected' ? 'ok' : state.state === 'held' ? 'warn' : 'info', timeout: 3000 });
    }
  } catch (e) { toast(errMsg(e), { type: 'err', title: '요약 실패' }); }
  finally { if (btn && btn.isConnected) setBusy(btn, false); }
}

/* ── 블록 요약 수정(검수자가 AI 초안을 고친다) ───────────────── */
const splitLines = (s) => String(s || '').split('\n').map((x) => x.trim()).filter(Boolean);
const itemText = (x) => (typeof x === 'string' ? x : (x && x.text) || '');
const ORG_LEVEL_OPTS = [['high', '높음 — 우리 기관을 직접 지목'], ['medium', '보통 — 예시 등으로 언급'], ['low', '낮음 — 감시 키워드만'], ['none', '무관']];

/** 줄 목록 → 원래 모양(문자열 또는 {text, utterance_id, strength}) 유지. 같은 글이면 근거 발언 링크를 그대로 둔다. */
function refList(text, orig) {
  const old = Array.isArray(orig) ? orig : [];
  const out = splitLines(text);
  const sameCount = out.length === old.length;
  return out.map((t, i) => {
    const hit = old.find((x) => itemText(x) === t) || (sameCount ? old[i] : null);
    if (hit && typeof hit === 'object') return { ...hit, text: t };
    return { text: t, utterance_id: null };
  });
}

async function editBlockSummary(bid, btn) {
  const b = S.blocks.get(bid);
  const sm = blockSummary(b);
  if (!b || !sm) return;
  const join = (arr) => (arr || []).map(itemText).filter(Boolean).join('\n');
  const opts = (list, cur) => [...new Set([...(cur ? [cur] : []), ...list])].map((v) => [v, v]);
  const vals = await formDialog({
    title: '블록 요약 수정',
    description: `${b.title} ${b.time_range} — 한 줄에 하나씩 적습니다. 고친 요약은 ‘수정: 이름(시각)’으로 표시되고, 자동 재요약이 덮어쓰지 않습니다.`,
    wide: true,
    fields: [
      { name: 'one_line', label: '한 줄 요약', value: sm.one_line || '', required: true },
      { name: 'question_points', label: '질의 요지', type: 'textarea', rows: 3, value: join(sm.question_points) },
      { name: 'answer_points', label: '답변 요지', type: 'textarea', rows: 3, value: join(sm.answer_points) },
      { name: 'issues', label: '지적사항', type: 'textarea', rows: 2, value: join(sm.issues) },
      { name: 'data_requests', label: '요구자료', type: 'textarea', rows: 2, value: join(sm.data_requests) },
      { name: 'commitments', label: '약속·검토사항', type: 'textarea', rows: 2, value: join(sm.commitments) },
      { name: 'keywords', label: '키워드(쉼표로)', value: (sm.keywords || []).join(', ') },
      { name: 'response_type', label: '대응 유형', type: 'select', value: sm.response_type || '기타', options: opts(RESPONSE_TYPES, sm.response_type) },
      { name: 'urgency', label: '대응 시급성', type: 'select', value: sm.urgency || '결과보고서 대응', options: opts(URGENCY, sm.urgency) },
      { name: 'org_relevance', label: '우리 기관 관련도', type: 'select', value: sm.org_relevance || 'none', options: ORG_LEVEL_OPTS },
      { name: 'org_reason', label: '우리 기관 관련 근거', type: 'textarea', rows: 2, value: sm.org_reason || '' },
    ],
    submit: '고쳐서 저장',
  });
  if (!vals) return;
  const next = {
    one_line: vals.one_line.trim(),
    question_points: splitLines(vals.question_points),
    answer_points: splitLines(vals.answer_points),
    issues: refList(vals.issues, sm.issues),
    data_requests: refList(vals.data_requests, sm.data_requests),
    commitments: refList(vals.commitments, sm.commitments),
    keywords: vals.keywords.split(/[,\n]/).map((k) => k.trim().replace(/^#/, '')).filter(Boolean),
    response_type: vals.response_type,
    urgency: vals.urgency,
    org_relevance: vals.org_relevance,
    org_reason: vals.org_reason.trim(),
  };
  // 바뀐 칸만 보낸다
  const patch = {};
  for (const [k, v] of Object.entries(next)) {
    const before = sm[k] ?? (Array.isArray(v) ? [] : '');
    if (JSON.stringify(before) !== JSON.stringify(v)) patch[k] = v;
  }
  if (!Object.keys(patch).length) { toast('바뀐 내용이 없습니다.', { timeout: 1800 }); return; }
  setBusy(btn, true);
  try {
    const r = await api(`blocks/${bid}/summary`, { method: 'PATCH', body: patch });
    if (r && r.id) enqueue('block.upsert', r);
    toast('블록 요약을 고쳤습니다.', { type: 'ok', timeout: 2000 });
  } catch (e) {
    if (isMissingRoute(e)) {
      // 요약 수정을 지원하지 않는 서버: 버튼을 숨긴다
      summaryEditOk = false;
      for (const g of groupMap.values()) if (g.type === 'b') renderBlock(g);
      toast('이 서버에서는 아직 요약 수정을 쓸 수 없습니다.', { type: 'warn' });
    } else toast(errMsg(e), { type: 'err', title: '요약을 고치지 못했습니다' });
  } finally { if (btn && btn.isConnected) setBusy(btn, false); }
}

async function reviewBlock(bid, btn, status = 'reviewed') {
  setBusy(btn, true);
  try {
    const r = await api(`blocks/${bid}/review`, { method: 'POST', body: { status } });
    if (r && r.id) enqueue('block.upsert', r);
    toast(status === 'reviewed' ? '검수 완료로 표시했습니다.' : 'AI 초안 상태로 되돌렸습니다.', { type: 'ok', timeout: 2000 });
  } catch (e) { toast(errMsg(e), { type: 'err' }); setBusy(btn, false); }
}

async function copyCard(bid, btn) {
  setBusy(btn, true);
  try {
    const r = await api(`blocks/${bid}/card`);
    await copyText(r.text || r.markdown || '', '질의·답변 요지 카드를 복사했습니다. 메신저에 붙여 넣으세요.');
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
  finally { setBusy(btn, false); }
}

function rowMenu(anchor, u) {
  const inQ = u.block_id && S.blocks.get(u.block_id)?.kind === 'question';
  popupMenu(anchor, [
    { label: '링크 복사', icon: 'link', onClick: () => copyText(absUrl(`/u/${u.id}`), '발언 링크를 복사했습니다.') },
    { label: '인용문 복사', icon: 'copy', onClick: () => copyText(quoteText(u, S.session), '인용문을 복사했습니다.') },
    { label: '발언 상세 보기', icon: 'external', href: `/u/${u.id}` },
    { label: S.bookmarks.has(u.id) ? '북마크 해제' : '북마크(Shift+Q)', icon: S.bookmarks.has(u.id) ? 'star-fill' : 'star', onClick: () => toggleBookmark(u.id) },
    OP ? 'sep' : null,
    { label: '화자 수정', icon: 'user', hidden: !OP || u.speaker_role === 'system', onClick: () => editSpeaker(u) },
    { label: '질의 위원 지정', icon: 'users', hidden: !OP || !inQ, onClick: () => assignQuestioner(u.block_id) },
    { label: '본문 수정', icon: 'edit', hidden: !OP, onClick: () => editText(u) },
    { label: '저장된 원음 보기', icon: 'mic', hidden: !OP || ctx.cloud || u.speaker_role === 'system', onClick: () => showSavedAudio(u) },
    { label: '후속조치로 등록', icon: 'checklist', hidden: !OP || u.speaker_role === 'system', onClick: () => addItem(u) },
  ].filter(Boolean), { label: '발언 메뉴' });
}

function reviewMetrics(review, fallback = {}) {
  const payload = review?.payload || review || {};
  const metrics = payload.metrics || fallback.metrics || {};
  const count = (value) => Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
  return { changed: count(metrics.changed_turns), held: count(metrics.held_utterances),
    status: review?.status || fallback.status || 'held' };
}

function invalidateReviews(bid) {
  reviewCache.delete(bid);
  reviewVersions.set(bid, (reviewVersions.get(bid) || 0) + 1);
}

async function loadReviews(bid) {
  if (reviewCache.has(bid)) return reviewCache.get(bid);
  if (reviewLoads.has(bid)) {
    await reviewLoads.get(bid);
    return loadReviews(bid); // 수신 중 새 이벤트가 오면 오래된 조회 결과를 재사용하지 않는다.
  }
  const version = reviewVersions.get(bid) || 0;
  const request = api(`sessions/${enc(SID)}/transcript-reviews`, { query: { block_id: bid } });
  reviewLoads.set(bid, request);
  try {
    const result = await request;
    const items = Array.isArray(result.items) ? result.items : [];
    if ((reviewVersions.get(bid) || 0) === version) reviewCache.set(bid, items);
    return items;
  } finally {
    if (reviewLoads.get(bid) === request) reviewLoads.delete(bid);
  }
}

const announcedReviews = new Set();
async function notifyTranscriptReview(event) {
  if (!OP || ctx.cloud || !event?.block_id || !event?.id) return;
  const bid = Number(event.block_id);
  invalidateReviews(bid);
  let review = event;
  try {
    const items = await loadReviews(bid);
    review = items.find((item) => item.id === event.id) || event;
  } catch (_) { /* 기존 서버에 목록 API가 없으면 이벤트의 간단한 안내만 표시한다. */ }
  const metrics = reviewMetrics(review, event);
  reviewNotices.set(bid, metrics);
  const group = groupMap.get(`b${bid}`);
  if (group) renderBlock(group);
  const key = `${event.id}:${event.status}`;
  if (!['draft', 'held', 'stale', 'failed', 'timeout'].includes(event.status) || announcedReviews.has(key)) return;
  announcedReviews.add(key);
  // 연결 복원으로 긴 목록이 재생돼도 알림 기록은 제한한다.
  if (announcedReviews.size > 200) announcedReviews.delete(announcedReviews.values().next().value);
  const title = event.status === 'draft' ? '화자·경계 검토 초안이 준비됐습니다' : '화자·경계 검토에 확인할 부분이 있습니다';
  toast(`변경 ${metrics.changed}건 · 보류 ${metrics.held}건`, { title, timeout: 10000,
    actions: [{ label: '초안 열기', onClick: () => speakerReview(bid, null, { reviewId: event.id, cachedOnly: true }) }] });
}

function previewReviewGroups(payload) {
  const groups = [];
  let previous = null;
  for (const part of Array.isArray(payload.turns) ? payload.turns : []) {
    const u = S.utts.get(part?.utterance_id);
    const raw = Array.from(String(u?.text || ''));
    if (!u || !Number.isInteger(part.start) || !Number.isInteger(part.end)
        || part.start < 0 || part.end <= part.start || part.end > raw.length) { previous = null; continue; }
    const segment = { uid: u.id, start: part.start, end: part.end, text: raw.slice(part.start, part.end).join('') };
    if (previous && sameReviewGroup(previous.part, part)) {
      previous.segments.push(segment);
    } else {
      previous = { part, segments: [segment] };
      groups.push(previous);
    }
  }
  return groups;
}

function reviewRangeLabel(review) {
  const payload = review.payload || review;
  const scope = payload.scope || {};
  const ids = [...new Set((Array.isArray(scope.utterance_ids) ? scope.utterance_ids
    : (Array.isArray(payload.turns) ? payload.turns : []).map((part) => part.utterance_id))
    .filter((id) => Number.isInteger(id) && id > 0))];
  const first = S.utts.get(ids[0]);
  const last = S.utts.get(ids[ids.length - 1]);
  const times = [...new Set([first, last].filter(Boolean).map((u) => u.time || fmt.hms(u.ts_start)).filter(Boolean))];
  const range = ids.length > 1 ? `원문 #${ids[0]}~#${ids[ids.length - 1]}` : ids.length ? `원문 #${ids[0]}` : '';
  return [scope.kind === 'window' ? `의심 구간 ${ids.length}행` : '질의 전체', times.join('~'), range].filter(Boolean).join(' · ');
}

function reviewPreviewBody(review, bid) {
  const payload = review.payload || review;
  const labels = { draft: '검토 초안', accepted: '채택됨', held: '판단 보류', unavailable: '연결 설정 필요', stale: '원문 변경·재검토 필요', failed: '검토 실패', busy: '다른 검토 진행 중', timeout: '시간 초과', rejected: '채택·초안 해제됨' };
  const metrics = reviewMetrics(review);
  const body = h('div', { class: 'stack review-preview' },
    h('p', null, `${labels[review.status] || review.status || '검토 초안'} · ${reviewRangeLabel(review)} · 변경 ${metrics.changed}건 · 보류 ${metrics.held}건`),
    h('p', { class: 'small muted' }, '원문은 보존됩니다. 이어지는 말묶음과 화자 경계를 확인한 뒤 채택하세요.'),
    payload.detail ? h('p', null, payload.detail) : null,
    (Array.isArray(payload.issues) ? payload.issues : []).map((issue) => h('p', { class: 'small' },
      issue.message || issue.reason || '원음 확인 필요',
      issue.suggested ? ` · ${issue.original || issue.word || '원문 후보'} → ${issue.suggested}` : '',
      issue.utterance_id ? h('button', { type: 'button', class: 'linkbtn small', onclick: () => gotoUtt(issue.utterance_id) }, '발언 보기') : null)));
  const saved = reviewCache.get(bid) || [];
  const currentIndex = saved.findIndex((item) => item.id === review.id);
  const openSaved = (item) => async (event) => {
    event.currentTarget.closest('dialog')?.close();
    await speakerReview(bid, null, { reviewId: item.id, cachedOnly: true });
  };
  if (saved.length > 1) {
    const windowStart = Math.max(0, Math.min(currentIndex - 4, saved.length - 10));
    const nearby = saved.slice(windowStart, windowStart + 10).filter((item) => item.id !== review.id);
    // 긴 질의에서도 앞뒤 탐색으로 모든 구간에 도달하며, 목록은 현재 초안 주변만 표시한다.
    body.append(h('nav', { class: 'review-source-links', 'aria-label': '보관 초안 탐색' },
      currentIndex > 0 ? h('button', { type: 'button', class: 'btn btn-ghost btn-xs', onclick: openSaved(saved[currentIndex - 1]) }, '이전 초안') : null,
      h('span', { class: 'small muted' }, `보관 초안 ${currentIndex >= 0 ? currentIndex + 1 : '—'}/${saved.length} · 검토 #${review.id}`),
      currentIndex >= 0 && currentIndex + 1 < saved.length ? h('button', { type: 'button', class: 'btn btn-ghost btn-xs', onclick: openSaved(saved[currentIndex + 1]) }, '다음 초안') : null),
    h('details', null, h('summary', null, '같은 질의의 다른 구간 초안 선택'),
      h('div', { class: 'stack' }, nearby.map((item) => h('button', { type: 'button', class: 'linkbtn small', onclick: openSaved(item) },
        `검토 #${item.id} · ${labels[item.status] || item.status} · ${reviewRangeLabel(item)}`)))));
  }
  const candidates = previewReviewGroups(payload);
  for (const candidate of candidates) {
    const uniqueIds = [...new Set(candidate.segments.map((part) => part.uid))];
    body.append(h('section', { class: 'review-preview-group' },
      h('div', { class: 'row' }, h('b', null, reviewRoleName(candidate.part)),
        uniqueIds.length > 1 ? badge(`이어지는 원문 ${uniqueIds.length}행`, 'b-blue badge-sm') : null),
      h('p', { class: 'review-preview-text' }, candidate.segments.map((part) => part.text).join(' ')),
      h('div', { class: 'review-source-links' }, candidate.segments.map((part) =>
        h('button', { type: 'button', class: 'linkbtn small', title: `원문 문자 ${part.start}~${part.end}`,
          onclick: (event) => { event.currentTarget.closest('dialog')?.close(); gotoUtt(part.uid); } }, `원문 #${part.uid}`)))));
  }
  if (!candidates.length) body.append(h('p', { class: 'small muted' }, '표시할 원문 범위가 없습니다. 이 초안은 새 추론 없이 보관 상태로 열었습니다.'));
  return body;
}

async function speakerReview(bid, button, { reviewId = null, cachedOnly = false, force = false } = {}) {
  if (!OP || ctx.cloud) return;
  setBusy(button, true);
  try {
    const previous = await loadReviews(bid);
    let review = reviewId == null ? previous[0] : previous.find((item) => item.id === reviewId);
    if (force || (!review && !cachedOnly)) {
      invalidateReviews(bid);
      review = await api(`sessions/${enc(SID)}/blocks/${bid}/transcript-review`, { method: 'POST' });
      if (review.id) reviewCache.set(bid, [review, ...previous.filter((item) => item.id !== review.id)]);
    }
    if (!review) { toast('보관된 초안을 찾을 수 없습니다. 목록을 다시 확인하세요.'); return; }
    const actions = review.id && ['draft', 'accepted'].includes(review.status) ? [{
      label: review.status === 'draft' ? '보정 채택' : '채택 되돌리기', primary: true,
      onClick: async (close) => {
        try {
          await api(`sessions/${enc(SID)}/transcript-reviews/${review.id}/${review.status === 'draft' ? 'accept' : 'reject'}`, { method: 'POST' });
          invalidateReviews(bid); close(); await resync(); toast('화자 표시 판본을 갱신했습니다.');
        } catch (e) { toast(errMsg(e), { type: 'err' }); }
      },
    }] : [];
    actions.push({ label: '질의 전체 다시 검토', onClick: (close) => { close(); speakerReview(bid, null, { force: true }); } });
    infoDialog({ title: '화자·연속 발언 검토', body: reviewPreviewBody(review, bid), wide: true, actions });
  } catch (e) { toast(errMsg(e), { type: 'err', title: '화자 검토' }); }
  finally { setBusy(button, false); }
}

async function showSavedAudio(u) {
  try {
    const saved = await api(`sessions/${enc(SID)}/audio-review/evidence`, { query: { utterance_id: u.id } });
    const evidence = Array.isArray(saved.items) ? saved.items[0] : null;
    if (!evidence) {
      infoDialog({ title: '저장된 원음', body: h('p', null, '이 발언에 보관된 원음 자료가 없습니다.') });
      return;
    }
    const result = { ...evidence.payload, evidence_id: evidence.id };
    infoDialog({ title: '저장된 원음', body: h('div', { class: 'stack' },
      h('p', null, result.detail || '이전에 보관한 원음 자료입니다.'),
      h('p', null, result.transcript || result.text || ''),
      Number.isInteger(result.evidence_id) && result.evidence_id > 0 ? h('audio', { controls: true, preload: 'none', src: `/api/sessions/${enc(SID)}/audio-review/clips/${result.evidence_id}` }) : null,
      result.clip_path ? h('p', { class: 'small' }, `보관 위치: ${result.clip_path}`) : null,
      h('p', { class: 'small muted' }, '보관 자료 열람이며 자막·화자는 바꾸지 않습니다.')) });
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

function onFeedClick(e) {
  const t = e.target instanceof Element ? e.target : null;
  if (!t) return;
  const actEl = t.closest('[data-act]');
  const li = t.closest('.u');
  const blk = t.closest('.blk');
  const bid = blk && blk.dataset.bid ? Number(blk.dataset.bid) : null;
  if (actEl) {
    const act = actEl.dataset.act;
    const u = li ? S.utts.get(Number(li.dataset.uid)) : null;
    switch (act) {
      case 'menu': if (u) { setFocus(u.id, { scroll: false, row: li }); rowMenu(actEl, u); } return;
      case 'speaker': if (u) { const label = actEl.dataset.speaker || speakerLabel(u); setFilter({ speaker: V.speaker === label ? '' : label }); } return;
      case 'item': {
        const it = S.items.get(Number(actEl.dataset.iid));
        if (it) { selectTab('items'); highlightItemRow(it.id); }
        return;
      }
      case 'questioner': if (bid) assignQuestioner(bid); return;
      case 'bsummarize': if (bid) summarizeBlock(bid, actEl); return;
      case 'speaker-review': if (bid) speakerReview(bid, actEl); return;
      case 'stage-evidence': {
        const projection = bid && stageBlockReview(S.blocks.get(bid));
        if (projection) infoDialog({ title: 'AI 보정 · 원문 대조',
          body: window.gukgamTranscriptStages.correctionEvidence(projection) });
        return;
      }
      case 'ai-original': if (bid) setBlockOriginal(bid, !aiOriginalChoice.get(bid)); return;
      case 'bedit': if (bid) editBlockSummary(bid, actEl); return;
      case 'review': if (bid) reviewBlock(bid, actEl); return;
      case 'unreview': if (bid) reviewBlock(bid, actEl, 'draft'); return;
      case 'card': if (bid) copyCard(bid, actEl); return;
      case 'goto': gotoUtt(actEl.dataset.uid); return;
      case 'tag': setFilter({ q: String(actEl.dataset.tag || '').replace(/^#/, '') }); return;
      default: return;
    }
  }
  if (li && !t.closest('a, button, summary, mark[title]')) {
    if (li.dataset.displayRow) return;
    if (li.dataset.stageCard) { if (bid) V.selectedBlock = bid; return; }
    setFocus(Number(li.dataset.uid), { scroll: false });
    if (blk && bid) V.selectedBlock = bid;
  }
}

function highlightItemRow(iid) {
  requestAnimationFrame(() => {
    const tr = $(`#it-body tr[data-iid="${iid}"]`);
    if (!tr) return;
    tr.scrollIntoView({ block: 'center' });
    tr.style.transition = 'background .6s';
    tr.style.background = 'var(--flash)';
    setTimeout(() => { tr.style.background = ''; }, 1600);
  });
}

/* ── 키워드 분석(클라이언트 집계) ─────────────────────────── */
const STOP = new Set(['그리고', '그래서', '그러니까', '그런데', '하지만', '지금', '우리', '이것', '그것', '저것', '여기', '거기', '이런', '그런', '저런', '이렇게', '그렇게',
  '위원님', '장관님', '장관', '위원장', '위원', '말씀', '생각', '부분', '문제', '관련', '관련해서', '대해서', '하겠습니다', '있습니다', '없습니다', '합니다', '했습니다',
  '그렇습니다', '알겠습니다', '수고하셨습니다', '감사합니다', '그래서요', '이제', '좀', '한번', '정도', '경우', '때문에', '것은', '것이', '것을', '거예요', '거지요',
  '거죠', '있는', '없는', '하는', '되는', '같은', '많이', '계속', '다시', '바랍니다', '주시기', '해서', '해야', '돼', '돼요', '됩니다', '예', '네', '아니', '그거', '이거']);

function keywordStats() {
  const counts = new Map();
  const add = (k, n = 1, src = 'ai') => {
    const key = String(k).replace(/^#/, '').trim();
    if (!key) return;
    const cur = counts.get(key) || { n: 0, src };
    cur.n += n;
    counts.set(key, cur);
  };
  for (const b of S.blocks.values()) for (const k of (window.gukgamTranscriptStages?.usesStoredDisplay?.() === true
    ? window.gukgamTranscriptStages.displaySummary(b) : blockSummary(b))?.keywords || []) add(k, 3, 'ai');
  for (const t of latestSummary()?.hashtags || []) add(t, 5, 'ai');
  for (const a of S.alerts.values()) add(a.keyword, 2, 'watch');
  if (counts.size < 8) {
    const freq = new Map();
    for (const u of S.utts.values()) {
      if (!u.is_final || u.speaker_role === 'system') continue;
      for (const w of normText(u.text).split(' ')) {
        const base = w.replace(/(은|는|이|가|을|를|의|에|에서|으로|로|과|와|도|만|까지|부터|이나|나|하고|이고|입니다|이라|라고)$/, '');
        if (base.length < 2 || /\d/.test(base) || STOP.has(base) || STOP.has(w)) continue;
        freq.set(base, (freq.get(base) || 0) + 1);
      }
    }
    [...freq.entries()].sort((a, b) => b[1] - a[1]).slice(0, 30).forEach(([k, n]) => { if (n >= 2 && !counts.has(k)) counts.set(k, { n, src: 'freq' }); });
  }
  return [...counts.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, 40);
}

function showKeywords() {
  const ks = keywordStats();
  let close = () => {};
  const body = ks.length
    ? [h('p', { class: 'small muted' }, 'AI 요약 키워드·해시태그·감시 키워드를 모으고, 부족하면 발언에서 자주 나온 낱말을 더했습니다. 누르면 발언을 검색합니다.'),
      h('div', { class: 'chips' }, ks.map(([k, v]) => h('button', {
        type: 'button', class: `chip${v.src === 'watch' ? ' k-issue' : v.src === 'ai' ? ' chip-tag' : ''}`,
        onclick: () => { close(); selectTab('feed'); setFilter({ q: k }); },
      }, `#${k}`, h('span', { class: 'faint', style: { marginLeft: '4px' } }, String(v.n)))))]
    : [emptyState('분석할 발언이 아직 부족합니다', '', 'hash')];
  ({ close } = infoDialog({ title: '키워드 분석', body, wide: true }));
}

/* ── 수집원 패널(브라우저 음성·국회 영상·파일·수기) ─────────── */
let srcPanelType = '';
let capture = null;
let hlsInst = null;

function assemblyPlayerUrl() {
  const c = S.session?.source_config || {};
  const q = new URLSearchParams({ xcode: c.xcode || '', xcgcd: c.xcgcd || '' });
  return `https://assembly.webcast.go.kr/main/player.asp?${q}`;
}

function renderSourcePanel() {
  const s = S.session;
  const t = s.source_type;
  const card = $('#src-card');
  const panel = $('#src-panel');
  if (!card || !panel) return;
  if (ctx.offline) { card.hidden = true; panel.replaceChildren(); return; }
  if (ctx.cloud && ['browser', 'file', 'replay'].includes(t)) {
    card.hidden = true;
    panel.replaceChildren();
    stopCapture();
    srcPanelType = t;
    return;
  }
  if (srcPanelType === t) { updateSourcePanel(); return; }
  if (srcPanelType === 'browser' && t !== 'browser') stopCapture(); // 수집원을 바꾸면 음성 보내기를 멈춘다
  srcPanelType = t;
  card.hidden = false;
  const title = $('#h-src');
  if (t === 'browser') {
    setKids(title, icon('mic', 'ic-sm'), '음성 보내기');
    if (!OP) { setKids(panel, h('p', { class: 'small muted', style: { margin: 0 } }, '운영자 PC의 마이크·탭 음성을 받아 적습니다.')); return; }
    setKids(panel, 
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn btn-sm', id: 'cap-mic' }, icon('mic', 'ic-sm'), '마이크'),
        h('button', { type: 'button', class: 'btn btn-sm', id: 'cap-tab' }, icon('monitor', 'ic-sm'), '탭 오디오'),
        h('button', { type: 'button', class: 'btn btn-sm btn-danger', id: 'cap-stop', hidden: true }, icon('stop', 'ic-sm'), '보내기 중지')),
      h('div', { class: 'level', style: { marginTop: '10px' }, 'aria-hidden': 'true' }, h('i', { id: 'cap-level' })),
      h('div', { class: 'small muted', id: 'cap-state', style: { marginTop: '6px' }, 'aria-live': 'polite' }, '대기 중 — 수집이 ‘수집 중’일 때 서버가 음성을 받습니다.'),
      h('p', { class: 'small faint', style: { margin: '6px 0 0' } }, '‘탭 오디오’는 공유 창에서 국회 중계 탭을 고르고 ‘탭 오디오 공유’를 켜세요.'));
    $('#cap-mic').addEventListener('click', () => startCapture('mic'));
    $('#cap-tab').addEventListener('click', () => startCapture('tab'));
    $('#cap-stop').addEventListener('click', stopCapture);
  } else if (t === 'assembly_caption' || t === 'assembly_hls') {
    setKids(title, icon('video', 'ic-sm'), '국회 영상');
    setKids(panel, 
      h('div', { class: 'row' },
        !ctx.cloud ? h('button', { type: 'button', class: 'btn btn-sm', id: 'vid-toggle', 'aria-pressed': 'false' }, icon('video', 'ic-sm'), '영상 보기') : null,
        h('a', { class: 'btn btn-sm btn-ghost', href: assemblyPlayerUrl(), target: '_blank', rel: 'noopener', id: 'vid-ext' }, icon('external', 'ic-sm'), '국회 플레이어')),
      !ctx.cloud ? h('div', { id: 'vid-box', style: { marginTop: '8px' }, hidden: true }) : null,
      h('p', { class: 'video-notice' }, `영상 출처: 국회 의사중계. ${NOTICE}`),
      !ctx.cloud && OP && t === 'assembly_caption' ? h('div', { id: 'src-switch', class: 'note warn', style: { marginTop: '8px' }, hidden: true },
        h('div', { class: 'small' }, 'AI자막을 받지 못했습니다. 같은 회의의 중계영상 음성인식으로 바꾸면 이 세션에 이어서 기록합니다.'),
        h('button', { type: 'button', class: 'btn btn-sm', style: { marginTop: '6px' }, id: 'src-to-hls' }, icon('video', 'ic-sm'), '중계영상 음성인식으로 전환')) : null);
    $('#vid-toggle')?.addEventListener('click', toggleVideo);
    $('#src-to-hls')?.addEventListener('click', () => {
      const c = { ...(S.session.source_config || {}) };
      delete c.xsami;
      patchSource('assembly_hls', c);
    });
  } else if (t === 'manual') {
    setKids(title, icon('keyboard', 'ic-sm'), '수기 입력');
    setKids(panel, h('p', { class: 'small muted', style: { margin: 0 } }, OP ? '화면 아래 입력창에서 화자를 고르고 Enter 로 발언을 입력합니다.' : '운영자가 직접 입력한 발언입니다.'));
  } else if (t === 'file') {
    setKids(title, icon('file', 'ic-sm'), '녹음 파일');
    setKids(panel, h('div', { id: 'file-info', class: 'small' }),
      OP ? h('form', { id: 'file-form', class: 'stack-sm', style: { marginTop: '8px' } },
        h('input', { type: 'file', id: 'file-up', accept: UPLOAD_ACCEPT, 'aria-label': '녹음 파일 선택' }),
        h('span', { class: 'hint' }, `올릴 수 있는 형식: ${UPLOAD_ACCEPT.replaceAll(',', ', ')}`),
        h('button', { type: 'submit', class: 'btn btn-sm' }, icon('upload', 'ic-sm'), '파일 올리기')) : null);
    $('#file-form')?.addEventListener('submit', uploadFile);
  } else if (t === 'replay') {
    setKids(title, icon('replay', 'ic-sm'), '리플레이(시연용)');
    setKids(panel, h('div', { id: 'replay-info', class: 'small muted' }));
  } else {
    card.hidden = true;
  }
  updateSourcePanel();
}

function updateSourcePanel() {
  const s = S.session;
  const ss = s.source_state || {};
  if (s.source_type === 'file') {
    const p = s.source_config?.path;
    const el = $('#file-info');
    if (el) el.textContent = p ? `파일: ${p.split('/').pop()}${ss.detail ? ` · ${ss.detail}` : ''}` : '아직 올린 파일이 없습니다.';
  } else if (s.source_type === 'replay') {
    const el = $('#replay-info');
    const c = s.source_config || {};
    if (el) el.textContent = `배속 ×${c.speed || 20}${c.with_hints ? ' · 화자 정보 포함' : ''}${ss.detail ? ` · ${ss.detail}` : ''}`;
  } else if (s.source_type === 'assembly_caption' || s.source_type === 'assembly_hls') {
    const ext = $('#vid-ext');
    if (ext) ext.href = assemblyPlayerUrl();
    const sw = $('#src-switch');
    // 자막 수신 실패(오류·속기 서버 등)로 멈춰 있을 때만 전환 버튼을 보인다(수집 중에는 서버가 수집원 변경을 거절)
    if (sw) sw.hidden = ctx.cloud || !(s.source_type === 'assembly_caption' && (s.status === 'paused' || s.status === 'idle') && (ss.phase === 'error' || ss.error));
  }
}

async function uploadFile(e) {
  e.preventDefault();
  if (ctx.cloud) return;
  const f = $('#file-up').files[0];
  if (!f) { toast('파일을 고르세요.', { type: 'warn' }); return; }
  if (!uploadExtOk(f.name)) { toast(`올릴 수 없는 파일 형식입니다. 허용: ${UPLOAD_ACCEPT.replaceAll(',', ', ')}`, { type: 'warn' }); return; }
  const btn = e.target.querySelector('button');
  setBusy(btn, true);
  try {
    const fd = new FormData();
    fd.append('file', f);
    await api(`sessions/${enc(SID)}/upload`, { method: 'POST', body: fd });
    const s = await api(`sessions/${enc(SID)}`);
    enqueue('session.status', s);
    toast('파일을 올렸습니다. [시작]을 누르면 받아쓰기를 시작합니다.', { type: 'ok' });
  } catch (err) { toast(errMsg(err), { type: 'err' }); }
  finally { setBusy(btn, false); }
}

async function startCapture(kind) {
  if (ctx.cloud) return;
  if (S.session.status !== 'live') {
    const ok = await confirmDialog({ title: '수집 시작', message: '수집이 시작되지 않았습니다. 수집을 시작하고 음성을 보낼까요?', ok: '시작하고 보내기' });
    if (!ok) return;
    try { const r = await api(`sessions/${enc(SID)}/start`, { method: 'POST' }); if (r && r.id) enqueue('session.status', r); } catch (e) { toast(errMsg(e), { type: 'err' }); return; }
  }
  try {
    const { AudioCapture } = await import('./audio-capture.js');
    if (capture) capture.stop();
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    capture = new AudioCapture({
      wsUrl: `${proto}://${location.host}/ws/ingest/${enc(SID)}`,
      workletUrl: `/static/js/pcm-worklet.js?v=${enc(ctx.version)}`,
      onLevel: (lv) => { const el = $('#cap-level'); if (el) el.style.width = `${Math.min(100, Math.round(lv * 140))}%`; },
      onState: (st) => {
        const el = $('#cap-state');
        if (el) el.textContent = st.text;
        const running = st.running;
        $('#cap-stop').hidden = !running;
        $('#cap-mic').hidden = running;
        $('#cap-tab').hidden = running;
        if (!running) { const lv = $('#cap-level'); if (lv) lv.style.width = '0%'; }
      },
    });
    await capture.start(kind);
  } catch (e) {
    // 권한 거부 등: 토스트가 사라진 뒤에도 패널 상태 줄에 실패가 남게 한다
    const st = $('#cap-state');
    if (st) st.textContent = `시작하지 못했습니다: ${errMsg(e)}${S.session.status === 'live' ? ' — 수집은 ‘수집 중’ 상태입니다. 다시 시도하거나 [일시정지]하세요.' : ''}`;
    toast(errMsg(e), { type: 'err', title: '음성 캡처 실패' });
  }
}

function stopCapture() {
  if (capture) { capture.stop(); capture = null; }
}

/* ── 검증용 연속 녹음: 기존 음성 수신을 저장하며 수집 제어와 분리한다 ── */
const audioRecording = {
  snapshot: null, loading: false, busy: false, error: '', timer: null,
  controller: null, left: false, sessionKey: '', ready: false,
};

function audioRecordingAvailable() {
  return OP && !ctx.cloud && !!$('#audio-recording-panel') && S.session?.source_type === 'assembly_caption';
}

function renderAudioRecording() {
  const panel = $('#audio-recording-panel');
  if (!panel) return;
  panel.hidden = !audioRecordingAvailable();
  if (panel.hidden) return;
  const rec = audioRecording.snapshot?.recording;
  const active = !!rec?.active;
  const phase = audioRecording.snapshot?.audio_phase;
  const validSeconds = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0;
  const recorded = validSeconds(rec?.recorded_seconds);
  const target = [600, 900].includes(rec?.duration_seconds) ? rec.duration_seconds : Number($('#audio-recording-duration').value);
  const status = {
    recording: '녹음 중', completed: '녹음 저장 완료', interrupted: '녹음 중단 · 연속 구간 확인 필요', failed: '녹음 실패',
  }[rec?.status] || '녹음 대기';
  $('#audio-recording-status').textContent = audioRecording.error ? '녹음 상태 확인 필요'
    : audioRecording.busy ? '녹음 요청 처리 중' : !audioRecording.ready ? '녹음 상태 확인 중' : status;
  $('#audio-recording-start').disabled = audioRecording.busy || audioRecording.loading || !audioRecording.ready
    || !!audioRecording.error || active || S.session.status !== 'live' || phase !== 'receiving';
  $('#audio-recording-start').hidden = active;
  $('#audio-recording-stop').hidden = !active;
  $('#audio-recording-stop').disabled = audioRecording.busy || audioRecording.loading;
  $('#audio-recording-duration').disabled = active || audioRecording.busy;
  $('#audio-recording-refresh').disabled = audioRecording.busy || audioRecording.loading;
  const progress = $('#audio-recording-progress');
  progress.hidden = !rec;
  progress.max = target || 900;
  progress.value = Math.min(recorded, progress.max);
  const time = $('#audio-recording-time');
  time.hidden = !rec;
  time.textContent = `실제 저장 ${Math.floor(recorded)}초 / 목표 ${target || 900}초`;
  let detail = rec?.detail || '';
  if (audioRecording.error) detail = audioRecording.error;
  else if (!active && S.session.status !== 'live') detail = [detail, '수집 중인 회의에서만 새 녹음을 시작할 수 있습니다.'].filter(Boolean).join(' ');
  else if (!active && phase !== 'receiving') detail = [detail, '음성 수신 중에만 녹음할 수 있습니다. 음성 대조 설정과 수신 상태를 확인하세요.'].filter(Boolean).join(' ');
  $('#audio-recording-detail').textContent = detail;
  const download = $('#audio-recording-download');
  const ended = ['completed', 'interrupted', 'failed'].includes(rec?.status);
  download.hidden = !ended || active || !rec?.id || recorded <= 0 || rec.download_ready === false;
  if (download.hidden) download.removeAttribute('href');
  else download.href = `/api/sessions/${enc(SID)}/audio-recording/download?recording_id=${enc(rec.id)}`;
}

function clearAudioRecordingTimer() {
  clearTimeout(audioRecording.timer);
  audioRecording.timer = null;
}

function scheduleAudioRecording() {
  clearAudioRecordingTimer();
  // 화면을 떠나거나 숨기면 상태 조회만 멈춘다. 서버 녹음은 자동 중지하지 않는다.
  if (!audioRecordingAvailable() || audioRecording.left || document.hidden || !audioRecording.snapshot?.recording?.active) return;
  audioRecording.timer = setTimeout(() => refreshAudioRecording(), 5000);
}

async function refreshAudioRecording() {
  if (!audioRecordingAvailable() || audioRecording.left || document.hidden || audioRecording.loading || audioRecording.busy) return;
  clearAudioRecordingTimer();
  audioRecording.loading = true;
  const controller = new AbortController();
  audioRecording.controller = controller;
  renderAudioRecording();
  try {
    const snapshot = await api(`sessions/${enc(SID)}/audio-recording`, { signal: controller.signal });
    if (controller.signal.aborted || audioRecording.left) return;
    audioRecording.snapshot = snapshot;
    audioRecording.ready = true;
    audioRecording.error = '';
  } catch (err) {
    if (!controller.signal.aborted) audioRecording.error = errMsg(err);
  } finally {
    if (audioRecording.controller === controller) audioRecording.controller = null;
    audioRecording.loading = false;
    renderAudioRecording();
    if (controller.signal.aborted && !document.hidden && !audioRecording.left) refreshAudioRecording();
    else scheduleAudioRecording();
  }
}

async function controlAudioRecording(action) {
  if (!audioRecordingAvailable() || audioRecording.left || audioRecording.busy || audioRecording.loading) return;
  const active = !!audioRecording.snapshot?.recording?.active;
  if (action === 'start' && (active || !audioRecording.ready || audioRecording.error
      || S.session.status !== 'live' || audioRecording.snapshot?.audio_phase !== 'receiving')) return;
  if (action === 'stop' && !active) return;
  if (action !== 'start' && action !== 'stop') return;
  const seconds = Number($('#audio-recording-duration').value);
  if (action === 'start' && ![600, 900].includes(seconds)) return;
  clearAudioRecordingTimer();
  audioRecording.busy = true;
  audioRecording.error = '';
  renderAudioRecording();
  try {
    // 녹음 API만 호출한다. 수집 시작·종료나 음성 연결을 이 버튼에서 만들지 않는다.
    audioRecording.snapshot = await api(`sessions/${enc(SID)}/audio-recording${action === 'stop' ? '/stop' : ''}`, {
      method: 'POST', ...(action === 'start' ? { body: { duration_seconds: seconds } } : {}),
    });
    audioRecording.ready = true;
  } catch (err) {
    audioRecording.error = errMsg(err);
    toast(audioRecording.error, { type: 'err', title: '녹음 요청 확인' });
  } finally {
    audioRecording.busy = false;
    renderAudioRecording();
    // 요청 결과가 불확실해도 재시작하지 않고 GET으로 실제 상태를 다시 확인한다.
    if (audioRecording.error) await refreshAudioRecording();
    else scheduleAudioRecording();
  }
}

function onAudioRecordingSessionStatus() {
  renderAudioRecording();
  if (!audioRecordingAvailable()) { clearAudioRecordingTimer(); return; }
  const key = `${S.session.source_type}:${S.session.status}`;
  if (audioRecording.sessionKey !== key) {
    audioRecording.sessionKey = key;
    refreshAudioRecording();
  }
}

function initAudioRecording() {
  if (!OP || ctx.cloud || !$('#audio-recording-panel')) return;
  $('#audio-recording-start').addEventListener('click', () => controlAudioRecording('start'));
  $('#audio-recording-stop').addEventListener('click', () => controlAudioRecording('stop'));
  $('#audio-recording-refresh').addEventListener('click', () => refreshAudioRecording());
  $('#audio-recording-panel').addEventListener('toggle', () => { if ($('#audio-recording-panel').open) refreshAudioRecording(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearAudioRecordingTimer();
      audioRecording.controller?.abort();
    } else refreshAudioRecording();
  });
  window.addEventListener('pagehide', () => {
    invalidateHistoryPrefetch();
    audioRecording.left = true;
    clearAudioRecordingTimer();
    audioRecording.controller?.abort();
  });
  window.addEventListener('pageshow', () => {
    audioRecording.left = false;
    refreshAudioRecording();
  });
}

/** 세션 상태가 바뀔 때 음성 보내기: 종료면 멈춰 마이크를 놓고, 재개면 기다리던 재연결을 바로 한다 */
function onCaptureSessionStatus(status) {
  if (!capture) return;
  if (status === 'ended') {
    stopCapture();
    const st = $('#cap-state');
    if (st) st.textContent = '수집이 종료되어 음성 보내기를 멈췄습니다(마이크·탭 공유 해제).';
  } else if (status === 'live') {
    capture.reconnectNow(); // 일시정지 중에는 서버가 거절(4409)하므로 늘어난 재시도 간격을 기다리지 않는다
  }
}

function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  return new Promise((resolve) => {
    const sc = document.createElement('script');
    sc.src = '/static/vendor/hls.min.js';
    sc.onload = () => resolve(window.Hls || null);
    sc.onerror = () => resolve(null);
    document.head.append(sc);
  });
}

async function toggleVideo() {
  const btn = $('#vid-toggle');
  const box = $('#vid-box');
  if (btn.getAttribute('aria-pressed') === 'true') {
    if (hlsInst) { hlsInst.destroy(); hlsInst = null; }
    box.replaceChildren();
    box.hidden = true;
    btn.setAttribute('aria-pressed', 'false');
    btn.lastChild.textContent = '영상 보기';
    return;
  }
  btn.setAttribute('aria-pressed', 'true');
  btn.lastChild.textContent = '영상 닫기';
  box.hidden = false;
  const s = S.session;
  const url = s.source_config?.hls_url || s.source_state?.hls_url || s.source_state?.xhls || '';
  const fallback = (msg) => setKids(box, h('div', { class: 'note' }, msg, ' ',
    h('a', { href: assemblyPlayerUrl(), target: '_blank', rel: 'noopener' }, '국회 플레이어에서 보기', icon('external', 'ic-sm'))));
  if (!url) { fallback('아직 영상 주소를 받지 못했습니다. 수집을 시작하면 주소를 찾습니다.'); return; }
  const video = h('video', { controls: true, playsinline: true, muted: true, autoplay: true, 'aria-label': '국회 중계 영상' });
  setKids(box, h('div', { class: 'video-box' }, video));
  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
  } else {
    const Hls = await loadHls();
    if (!Hls || !Hls.isSupported()) { fallback('이 브라우저에서는 영상을 바로 재생할 수 없습니다.'); return; }
    hlsInst = new Hls({ lowLatencyMode: true, liveSyncDurationCount: 3 });
    hlsInst.on(Hls.Events.ERROR, (_e, data) => {
      if (data && data.fatal) { hlsInst.destroy(); hlsInst = null; fallback('영상을 불러오지 못했습니다(접근 제한 또는 중계 종료).'); }
    });
    hlsInst.loadSource(url);
    hlsInst.attachMedia(video);
  }
  video.play().catch(() => { /* 자동 재생 차단 시 사용자가 재생 */ });
}

/* ── 수기 입력 ───────────────────────────────────────────── */
function initManual() {
  const form = $('#manual-form');
  if (!form) return;
  const spk = $('#m-spk');
  const role = $('#m-role');
  spk.addEventListener('change', () => {
    const m = S.roster.find((x) => x.name === spk.value.trim());
    if (m) role.value = m.role === 'chair' ? 'chair' : (m.role === 'member' ? 'member' : role.value);
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = $('#m-text').value.trim();
    if (!text) return;
    if (ctx.cloud && Array.from(text).length > 2000) {
      toast('클라우드에서는 발언을 2,000자까지 입력할 수 있습니다.', { type: 'warn' });
      return;
    }
    const btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      await api(`sessions/${enc(SID)}/manual`, { method: 'POST', body: { text, speaker: spk.value.trim(), role: role.value } });
      $('#m-text').value = '';
      if (!V.autoscroll) setAutoscroll(true);
    } catch (err) { toast(errMsg(err), { type: 'err' }); }
    finally { btn.disabled = false; $('#m-text').focus(); }
  });
}

/* ── 단축키 ─────────────────────────────────────────────── */
function showHelp() {
  const tpl = $('#tpl-help');
  infoDialog({ title: '단축키', body: tpl.content.cloneNode(true) });
}

function onKey(e) {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector('dialog[open]')) return;
  if (isTyping(e)) {
    if (e.key === 'Escape' && e.target.id === 'q') { setFilter({ q: '' }); e.target.blur(); }
    return;
  }
  const k = e.key;
  if (k === '/') { e.preventDefault(); window.gukgamTranscriptStages?.showLegacy(); selectTab('feed'); $('#q').focus(); $('#q').select(); return; }
  if (k === '?') { e.preventDefault(); showHelp(); return; }
  if (V.tab !== 'feed') return;
  if (window.gukgamTranscriptStages?.isActive()) return;
  if (!e.shiftKey && (k === 'j' || k === 'J')) { e.preventDefault(); moveFocus(1); return; }
  if (!e.shiftKey && (k === 'k' || k === 'K')) { e.preventDefault(); moveFocus(-1); return; }
  if ((k === 'g' || k === 'G') && !e.shiftKey) { e.preventDefault(); goLatest(); return; }
  if (k === 'Escape') { if (filtering()) clearFilters(); return; }
  const u = V.focusId != null ? S.utts.get(V.focusId) : null;
  if (e.shiftKey && (k === 'Q' || k === 'q')) {
    e.preventDefault();
    if (u) toggleBookmark(u.id); else toast('먼저 발언을 누르거나 J/K 로 고르세요.', { timeout: 2000 });
    return;
  }
  if (e.shiftKey && (k === 'S' || k === 's')) {
    e.preventDefault();
    if (!OP) { toast('운영자만 발언자를 지정할 수 있습니다.', { type: 'warn' }); return; }
    if (!u) { toast('먼저 발언을 누르거나 J/K 로 고르세요.', { timeout: 2000 }); return; }
    const b = u.block_id ? S.blocks.get(u.block_id) : null;
    if (b && b.kind === 'question' && (u.speaker_role === 'member' || b.status === 'needs_name')) assignQuestioner(b.id);
    else editSpeaker(u);
    return;
  }
  if (k === 'Enter' && u && document.activeElement === rowEl.get(u.id)) {
    e.preventDefault();
    const btn = rowEl.get(u.id).querySelector('.u-more');
    if (btn) rowMenu(btn, u);
  }
}

/* ── 글자 크기·토글 ─────────────────────────────────────── */
function setFont(size) {
  V.fontSize = Math.max(12, Math.min(24, size));
  store.set('sess.fontSize', V.fontSize);
  feed.style.setProperty('--feed-font', `${V.fontSize}px`);
}
function setShowTime(on) {
  V.showTime = on;
  store.set('sess.showTime', on);
  feed.classList.toggle('no-time', !on);
  $('#t-time').checked = on;
}

function exportMenu(anchor) {
  const base = `/api/sessions/${enc(SID)}/export?format=`;
  popupMenu(anchor, [
    { label: '전체 세션 단독 HTML (관리자)', icon: 'download',
      hidden: !OP || ctx.cloud || ctx.offline || S.session?.status !== 'ended'
        || !['localhost', '127.0.0.1', '::1', '[::1]'].includes(location.hostname),
      onClick: async () => {
        setBusy(anchor, true);
        try {
          const response = await api(`sessions/${enc(SID)}/offline.html`, { raw: true });
          const blob = await response.blob(), url = URL.createObjectURL(blob);
          const name = /filename\*=UTF-8''([^;]+)/i.exec(response.headers.get('Content-Disposition') || '');
          const link = h('a', { href: url, download: name ? decodeURIComponent(name[1]) : `${SID}_단독열람.html` });
          document.body.append(link); link.click(); link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          toast('전체 기록을 담은 단독 HTML을 내려받았습니다. 파일을 가진 사람은 로그인 없이 열 수 있습니다.', { type: 'ok', timeout: 7000 });
        } catch (error) { toast(errMsg(error), { type: 'err', title: '단독 HTML을 만들지 못했습니다' }); }
        finally { setBusy(anchor, false); }
      } },
    { label: '워드 문서(.docx)', icon: 'file-text', href: `${base}docx` },
    { label: '마크다운(.md)', icon: 'file', href: `${base}md` },
    { label: '텍스트(.txt)', icon: 'file', href: `${base}txt` },
    { label: '후속조치 표(.csv)', icon: 'checklist', href: `${base}csv` },
    { label: 'JSON(원자료)', icon: 'file', href: `${base}json` },
    'sep',
    { label: '인쇄', icon: 'report', onClick: () => window.print() },
  ], { label: '내보내기' });
}

/* ── 1초 타이머(경과 시간 등) ─────────────────────────────── */
function tick() {
  if (!S.session) return;
  const el = $('#meta-elapsed');
  if (el) el.textContent = elapsedText();
  const cur = $('#cur-card');
  const t0 = Number(cur.dataset.live || 0);
  if (t0) { const tm = $('#cur-timer'); if (tm) tm.textContent = fmt.clock((Date.now() - t0) / 1000); }
  const rx = $('#src-rx');
  const ss = S.session.source_state || {};
  if (rx && ss.last_rx_ms) rx.textContent = `· 마지막 수신 ${fmt.ago(ss.last_rx_ms)}`;
}

/* ── 해시(표시 말묶음 #d, 명시한 원문 #u, 질의 #b) ─────── */
function handleHash() {
  const display = /^#d-(\d+)-(\d+)-(.+)$/.exec(location.hash);
  if (display) {
    const blockId = Number(display[1]), utteranceId = Number(display[2]);
    if (!Number.isSafeInteger(blockId) || !Number.isSafeInteger(utteranceId)) return false;
    try { gotoDisplay(decodeURIComponent(display[3]), { blockId, utteranceId }); return true; }
    catch { return false; }
  }
  const m = /^#u-(\d+)$/.exec(location.hash);
  if (m) { gotoUtt(Number(m[1])); return true; }
  const b = /^#b-(\d+)$/.exec(location.hash);
  if (b) { gotoBlock(Number(b[1])); return true; }
  return false;
}

/* ── 시작 ───────────────────────────────────────────────── */
function bindUI() {
  const older = h('button', { id: 'feed-older', type: 'button', class: 'btn btn-sm', hidden: true,
    onclick: loadOlderHistory }, '이전 기록 더 보기');
  const historyActions = h('div', { class: 'row', style: { justifyContent: 'center', flexWrap: 'wrap', marginBottom: '12px' } }, older);
  if (!ctx.cloud) historyActions.append(
    h('button', { id: 'feed-all', type: 'button', class: 'btn btn-sm', hidden: true,
      onclick: loadAllHistory }, '처음부터 전체 기록 열기'),
    h('button', { id: 'feed-all-cancel', type: 'button', class: 'btn btn-sm', hidden: true,
      onclick: cancelFullHistory }, '불러오기 취소'),
    h('span', { id: 'feed-all-status', class: 'small muted', role: 'status', 'aria-live': 'polite', hidden: true }));
  feed.before(historyActions);
  // 탭(화살표 키 이동 포함)
  for (const t of TABS) $(`#tb-${t}`).addEventListener('click', () => selectTab(t));
  $('.tabs').addEventListener('keydown', (e) => {
    const i = TABS.indexOf(V.tab);
    if (e.key === 'ArrowRight') { e.preventDefault(); selectTab(TABS[(i + 1) % TABS.length], true); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); selectTab(TABS[(i - 1 + TABS.length) % TABS.length], true); }
  });
  feed.addEventListener('click', onFeedClick);
  $('#issues').addEventListener('click', onFeedClick);
  $('#sum-box').addEventListener('click', onFeedClick);
  const filterInput = debounce(() => setFilter({ q: $('#q').value.trim() }), 160);
  $('#q').addEventListener('input', () => { cancelPendingDisplayNavigation(); filterInput(); });
  $('#f-bm').addEventListener('click', () => setFilter({ bmOnly: !V.bmOnly }));
  $('#t-auto').addEventListener('change', (e) => setAutoscroll(e.target.checked));
  $('#t-time').addEventListener('change', (e) => setShowTime(e.target.checked));
  $('#fs-dec').addEventListener('click', () => setFont(V.fontSize - 1));
  $('#fs-inc').addEventListener('click', () => setFont(V.fontSize + 1));
  $('#export-btn').addEventListener('click', (e) => exportMenu(e.currentTarget));
  $('#help-btn').addEventListener('click', showHelp);
  $('#to-latest').addEventListener('click', goLatest);
  $('#fab-top').addEventListener('click', () => { setAutoscroll(false); markProgrammatic(); window.scrollTo(0, 0); });
  $('#fab-bottom').addEventListener('click', goLatest);
  $('#fab-inc').addEventListener('click', () => setFont(V.fontSize + 1));
  $('#fab-dec').addEventListener('click', () => setFont(V.fontSize - 1));
  $('#fab-search').addEventListener('click', () => { $('#q').focus(); $('#q').select(); });
  $('#al-sound').addEventListener('click', () => { setSound(!V.sound); if (V.sound) beep(); });
  $('#al-notify').addEventListener('click', () => setNotify(!V.notify));
  $('#al-unack').addEventListener('change', renderAlertsTab);
  $('#al-ack-all').addEventListener('click', () => ackAlert(null));
  for (const id of ['#it-kind', '#it-status', '#it-hide-ritual']) $(id).addEventListener('change', renderItemsTab);
  $('#it-kind').append(...kindOptions());
  $('#it-body').addEventListener('focusout', () => { setTimeout(() => { if (itemsPendingRender && !$('#it-body').contains(document.activeElement)) renderItemsTab(); }, 0); });
  $('#it-add')?.addEventListener('click', () => addItem(null));
  $('#ai-keywords').addEventListener('click', showKeywords);
  $('#ai-session')?.addEventListener('click', (e) => summarizeSession(e.currentTarget));
  $('#ai-block')?.addEventListener('click', (e) => {
    let bid = V.selectedBlock;
    if (!bid || S.blocks.get(bid)?.kind !== 'question') bid = latestBlocks().lastQ?.id;
    if (!bid) { toast('요약할 질의 블록이 없습니다.', { type: 'warn' }); return; }
    summarizeBlock(bid, e.currentTarget);
  });
  if (OP) {
    $('#c-start').addEventListener('click', () => control('start'));
    $('#c-pause').addEventListener('click', () => control('pause'));
    $('#c-stop').addEventListener('click', () => control('stop'));
    $('#c-marker')?.addEventListener('click', addMarker);
    $('#c-more').addEventListener('click', (e) => sessionMenu(e.currentTarget));
  }
  document.addEventListener('keydown', onKey);
  window.addEventListener('hashchange', handleHash);
  window.addEventListener('beforeunload', () => {
    clearReconnectNotice();
    clearTimeout(esTimer);
    if (es) es.close();
    stopCapture();
  });
  window.addEventListener('pagehide', () => {
    cancelFullHistory();
    recentCorrections.stopped = true;
    clearTimeout(recentCorrections.timer); recentCorrections.timer = null;
  });
  window.addEventListener('pageshow', () => { recentCorrections.stopped = false; queueRecentCorrections(); queueOlderHistoryPrefetch(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(recentCorrections.timer); recentCorrections.timer = null; invalidateHistoryPrefetch(); }
    else { queueRecentCorrections(); queueOlderHistoryPrefetch(); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && flushScheduled) { flushScheduled = false; scheduleFlush(); } });
  initScrubber();
  initManual();
  initAudioRecording();
  setFont(V.fontSize);
  setShowTime(V.showTime);
  setSound(V.sound);
  if (V.sound) armAudioUnlock(); // 소리 켜짐이 복원돼도 첫 클릭·키 입력 때 오디오를 깨워 둬야 알림음이 난다
  setNotify(V.notify && 'Notification' in window && Notification.permission === 'granted');
}

async function boot() {
  if (!SID) { setKids($('#sess-loading'), errorEl('세션 id 가 없습니다.')); return; }
  bindUI();
  let snap;
  try {
    snap = await readSessionSnapshot(`sessions/${enc(SID)}/snapshot`, { query: { recent: true, utterance_limit: 100 } });
  } catch (e) {
    if (e?.name === 'AbortError') return;
    $('#sess-loading').hidden = true;
    const box = $('#sess-error');
    box.hidden = false;
    setKids(box, h('div', { class: 'card' }, emptyState(e.status === 404 ? '세션을 찾을 수 없습니다' : '회의 기록을 불러오지 못했습니다', errMsg(e), 'alert'),
      h('div', { class: 'row', style: { justifyContent: 'center' } }, h('a', { class: 'btn', href: '/' }, '대시보드로'), h('button', { class: 'btn btn-primary', type: 'button', onclick: () => location.reload() }, '다시 시도'))));
    return;
  }
  initFromSnapshot(snap);
  connectSSE();
  loadDepartments();
  setInterval(tick, 1000);
  if (!handleHash()) {
    if (S.session.status === 'live') scrollToBottom();
    else setAutoscroll(false);
  }
  scheduleScrubPos();
}

boot();
