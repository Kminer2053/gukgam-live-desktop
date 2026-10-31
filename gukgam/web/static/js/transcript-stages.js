// 최종 보정과 진행 중인 자막을 읽는 화면. 원문·처리 근거는 AI 보정 배지 안에 보존한다.
import { api, correctionNotices, ctx, fmt, h, setKids, summaryPresentation } from './common.js';
import { closedBlockProjection } from './closed-block-view.js';

const STAGES = ['corrected', 'live'];
const ROLE_NAMES = { member: '질의 의원', answer: '답변 기관', chair: '위원장 진행', other: '기타 발언' };
const UNCERTAINTIES = {
  unfinished_sentence: '문장 종결 확인 필요', role_held: '화자 역할 확인 필요', role_uncertain: '화자 역할이 불명확함',
  speaker_held: '화자 확인 필요', role_context_conflict: '사전 역할 판정과 발언 문맥이 다름',
  role_rule_conflict: '사전 역할 판정이 명확한 발언 근거와 충돌함',
  text_model_failed: '본문 보정 실패', correction_rejected: '본문 수정 근거 확인 필요',
  text_model_unavailable: '본문 보정 모델 미준비', text_model_timeout: '본문 보정 시간 초과',
  text_response_invalid: '본문 보정 결과 형식 확인 필요', model_uncertain: '문맥 확인 필요',
  input_limit: '검토 가능한 문맥 길이 초과', number_changed: '수치 수정 확인 필요',
  negation_changed: '긍정·부정 의미 수정 확인 필요', name_changed: '이름 수정 확인 필요', lexical_changed: '단어 수정 확인 필요',
  jev_failed: '화자 판정 실패', jev_timeout: '화자 판정 시간 초과', jev_unavailable: '화자 판정 모델 미준비',
};
const GUIDES = {
  live: '국회가 보내 준 자막 단위와 화자 표식을 그대로 표시합니다. 수신 중인 자막은 계속 바뀔 수 있습니다.',
  corrected: '질의가 끝나면 보정한 내용으로 정리합니다. AI 보정 배지를 누르면 원문과 수정 내역을 볼 수 있습니다.',
};
const FILE_GUIDES = {
  live: '음성에서 인식한 원전사와 익명 음향 화자 표식을 표시합니다. 인식 오류나 화자 혼동이 있을 수 있으며 실명을 뜻하지 않습니다.',
  corrected: '저장된 AI 보정 내용과 아직 보정되지 않은 원전사를 표시합니다. AI 보정 배지에서 원전사와 수정 내역을 확인할 수 있습니다.',
};
const supportsStages = (session) => ['assembly_caption', 'file'].includes(session?.source_type);
const recordingSource = (source) => source?.source_kind === 'recording_asr';
const validId = (value) => Number.isSafeInteger(value) && value > 0;
const settledSource = (source) => source?.final === true && (recordingSource(source)
  ? validId(source.origin_utterance_id) && source.origin_utterance_id === source.id && /^[a-f0-9]{64}$/.test(source.source_hash || '')
  : source.scd === true);
const array = (value) => Array.isArray(value) ? value : [];
const sourceOrder = (a, b) => Number(a.first_rx || 0) - Number(b.first_rx || 0) || a.id - b.id;
const LIVE_ROLES = { ...ROLE_NAMES, unknown: '발언', mixed: '복수 화자' };
const displayOrder = (a, b) => {
  if (Array.isArray(a.sort_key) && Array.isArray(b.sort_key)) {
    for (let index = 0; index < Math.max(a.sort_key.length, b.sort_key.length); index++) {
      const difference = Number(a.sort_key[index] || 0) - Number(b.sort_key[index] || 0);
      if (difference) return difference;
    }
  }
  return Number(a.order ?? a.ts_start ?? 0) - Number(b.order ?? b.ts_start ?? 0)
    || Number(a.ts_start || 0) - Number(b.ts_start || 0) || Number(a.start || 0) - Number(b.start || 0);
};

// 같은 행은 DOM에서 떼지 않아 키보드 포커스와 펼친 배지를 유지한다.
function reconcileChildren(parent, nodes) {
  const wanted = new Set(nodes);
  for (const node of [...parent.children]) if (!wanted.has(node)) node.remove();
  nodes.forEach((node, index) => { if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null); });
}

function descendantById(container, id) {
  if (container?.id === id) return container;
  for (const child of container?.children || []) { const found = descendantById(child, id); if (found) return found; }
  return null;
}

function focusSnapshot(container) {
  const active = document.activeElement;
  let node = active;
  const path = [];
  while (node && node !== container) {
    if (node.id) {
      let owner = node;
      while (owner && owner !== container) owner = owner.parentNode;
      return owner ? { active, id: node.id, path, container } : null;
    }
    const parent = node.parentNode;
    if (!parent) return null;
    path.unshift([...parent.children].indexOf(node));
    node = parent;
  }
  return null;
}

function restoreFocus(snapshot) {
  if (!snapshot || document.activeElement === snapshot.active) return;
  let node = descendantById(snapshot.container, snapshot.id);
  for (const index of snapshot.path) node = node?.children[index];
  node?.focus({ preventScroll: true });
}

function viewportSnapshot(viewport, list) {
  const bounds = viewport.getBoundingClientRect();
  const anchor = [...list.children].find((node) => node.getBoundingClientRect().bottom > bounds.top);
  return { top: Number(viewport.scrollTop || 0), id: anchor?.id, anchorTop: anchor?.getBoundingClientRect().top,
    focus: focusSnapshot(list), openIds: [...list.querySelectorAll('details[open]')].map((node) => node.id).filter(Boolean) };
}

function pinLatest(viewport) {
  const bottom = Math.max(0, Number(viewport.scrollHeight || 0) - Number(viewport.clientHeight || 0));
  if (Math.abs(Number(viewport.scrollTop || 0) - bottom) > 1) viewport.scrollTop = bottom;
}

function restoreViewport(viewport, snapshot, follow = false) {
  for (const id of snapshot.openIds) { const node = descendantById(viewport, id); if (node) node.open = true; }
  restoreFocus(snapshot.focus);
  if (follow) pinLatest(viewport);
  else {
    const anchor = snapshot.id && descendantById(viewport, snapshot.id);
    viewport.scrollTop = snapshot.top + (anchor && snapshot.anchorTop !== undefined
      ? anchor.getBoundingClientRect().top - snapshot.anchorTop : 0);
  }
}

function currentSpeakerGuess(turn, sources) {
  const guess = turn.speaker_guess;
  if (!guess || guess.source !== 'display_only' || typeof guess.speaker !== 'string'
      || !Object.hasOwn(LIVE_ROLES, guess.role) || typeof guess.estimated !== 'boolean') return null;
  const revisions = revisionsFor(guess);
  if (!revisions.size || [...revisions].some(([id, revision]) => !sources.has(id)
      || sources.get(id).replayed === true || Number(sources.get(id).revision) !== revision)) return null;
  return guess;
}

function effectiveTurn(turn, sources) { return { ...turn, speaker_guess: currentSpeakerGuess(turn, sources) }; }

function contentPositions(text) {
  const value = String(text || ''), ignored = new Set();
  for (const match of value.matchAll(/^\s*-\s+/gm)) {
    const start = Array.from(value.slice(0, match.index)).length;
    for (let i = start; i < start + Array.from(match[0]).length; i++) ignored.add(i);
  }
  return Array.from(value).map((char, index) => ({ char, index }))
    .filter((part) => !/\s/u.test(part.char) && !ignored.has(part.index));
}

function protectedTurn(turn) {
  return turn.protected === true || turn.speaker_source === 'manual' || turn.edited === true
    || array(turn.review_parts).length > 0 || turn.text !== turn.original_text;
}

function validLiveTurn(turn, sources) {
  if (!turn || !validId(turn.id) || typeof turn.original_text !== 'string' || typeof turn.text !== 'string') return false;
  const spans = array(turn.source_spans), revisions = revisionsFor(turn);
  if (!spans.length || !revisions.size) return false;
  if ([...revisions].some(([id, revision]) => !sources.has(id) || sources.get(id).replayed === true
      || Number(sources.get(id).revision) !== revision)) return false;
  let previous = null, original = '';
  for (const span of spans) {
    const source = sources.get(span.source_id), chars = Array.from(String(source?.text || ''));
    if (!source || !revisions.has(span.source_id) || !Number.isSafeInteger(span.start) || !Number.isSafeInteger(span.end)
        || span.start < 0 || span.end <= span.start || span.end > chars.length) return false;
    if (previous && (sourceOrder(previous.source, source) > 0
        || previous.source.id === source.id && previous.end > span.start)) return false;
    original += chars.slice(span.start, span.end).join('');
    previous = { source, end: span.end };
  }
  return contentPositions(original).map((p) => p.char).join('') === contentPositions(turn.original_text).map((p) => p.char).join('');
}

function revisionsFor(doc) {
  if (Array.isArray(doc.source_revisions)) {
    return new Map(doc.source_revisions.map((item) => [Number(item.source_id ?? item.id), Number(item.revision)]));
  }
  return new Map(Object.entries(doc.source_revisions || {}).map(([id, revision]) => [Number(id), Number(revision)]));
}

function confirmedDeclaration(policy, sources, revisions) {
  const anchor = policy?.declaration, ids = anchor?.source_ids;
  const start = anchor?.source_start, end = anchor?.source_end;
  if (policy?.policy !== 'explicit_chair_declaration' || !anchor
      || !['explicit_pause_or_end', 'explicit_chair_declaration'].includes(anchor.kind)
      || !Array.isArray(ids) || !ids.length || !ids.every(validId) || new Set(ids).size !== ids.length
      || start?.source_id !== ids[0] || end?.source_id !== ids.at(-1)
      || !Number.isSafeInteger(start?.offset) || !Number.isSafeInteger(end?.offset)
      || start.offset < 0 || end.offset <= 0
      || ids.some((id, index) => {
        const row = sources.get(id), previous = index ? sources.get(ids[index - 1]) : null;
        return !row || typeof row.text !== 'string' || row.final !== true || row.scd !== true
          || row.replayed === true || !validId(revisions.get(id)) || revisions.get(id) !== Number(row.revision)
          || previous && sourceOrder(previous, row) >= 0;
      })) return false;
  if (start.offset >= Array.from(sources.get(start.source_id).text).length
      || end.offset > Array.from(sources.get(end.source_id).text).length
      || ids.length === 1 && start.offset >= end.offset) return false;
  return !array(policy.unconfirmed_source_ids).some((id) => ids.includes(id));
}

function validDocument(doc, sources) {
  if (!doc || doc.mode !== 'closed_block_v1' || !validId(doc.id) || !['draft', 'partial', 'held', 'ready', 'processing'].includes(doc.status)) return false;
  const ids = array(doc.source_ids), revisions = revisionsFor(doc);
  if (!ids.length || ids.some((id) => !validId(id) || !sources.has(id))) return false;
  if (ids.some((id) => !revisions.has(id))) return false;
  const policy = doc.source_settlement_policy, following = policy?.following_closed_question_ids;
  const unconfirmed = policy?.unconfirmed_source_ids;
  // 서버가 검증한 선언 출처 또는 두 후속 질의 증거가 허용한 조각만 미확정 상태로 읽는다.
  const twoFollowing = policy?.policy === 'after_two_closed_questions' && Array.isArray(following)
    && following.length === 2 && following.every((id) => validId(id) && id > doc.block_id) && new Set(following).size === 2;
  const fallback = validId(doc.block_id) && doc.source_revisions && typeof doc.source_revisions === 'object'
    && !Array.isArray(doc.source_revisions) && (twoFollowing || confirmedDeclaration(policy, sources, revisions))
    && Array.isArray(unconfirmed) && unconfirmed.length > 0
    && unconfirmed.every((id) => validId(id) && revisions.has(id) && validId(revisions.get(id)))
    && new Set(unconfirmed).size === unconfirmed.length ? new Set(unconfirmed) : new Set();
  if ([...revisions].some(([id, revision]) => !sources.has(id) || revision !== Number(sources.get(id).revision)
    || sources.get(id).replayed === true || !settledSource(sources.get(id))
      && (recordingSource(sources.get(id)) || !fallback.has(id)))) return false;
  return array(doc.sentences).length > 0 || ['held', 'processing'].includes(doc.status);
}

/** 최신 조회와 과거 페이지의 응답 순서가 바뀌어도 낮은 원문 판본으로 되돌아가지 않는다. */
export class StageStore {
  constructor() {
    this.sources = new Map();
    this.documents = new Map();
    this.invalidDocuments = new Set();
    this.validDocumentIds = null;
    this.liveTurns = new Map();
    this.blocks = new Map();
    this.validLiveTurnIds = null;
    this.snapshotAt = 0;
    this.nextBefore = null;
    this.paginationInitialized = false;
    this.documentDeps = new Map();
    this.documentsBySource = new Map();
    this.documentsByBlock = new Map();
    this.turnDeps = new Map();
    this.turnsBySource = new Map();
    this.blockVersions = new Map();
    this.legacyChangedBlocks = new Set();
    this.legacyInvalidatedBlocks = new Set();
    this.legacyChangedTurns = new Set();
    // 새 서버는 3→2→1 선택과 출처 연결을 저장한다. 화면은 그 선택을 다시 계산하지 않는다.
    this.canonical = false;
    this.displayGeneration = 0;
    this.displayFloor = 0;
    this.displayBlocks = new Map();
    this.displayRows = new Map();
    this.displayPendingBlocks = new Map();
    this.displayRowsByUtterance = new Map();
    this.displayRowsByBlock = new Map();
    this.displayRowsBySource = new Map();
    this.displayBlocksByUtterance = new Map();
    this.displayBlocksBySource = new Map();
    this.displayLooseRows = [];
    this.displayCoveredUtterances = new Set();
    this.displayPendingUtterances = new Map();
    this.displayPendingSources = new Map();
    this.displayAwaiting = false;
    this.displaySummaries = new Map();
  }
  acceptsDisplay(payload) {
    if (!Object.hasOwn(payload || {}, 'display_blocks')) return !this.canonical;
    return validId(payload.display_generation)
      && payload.display_generation >= Math.max(this.displayGeneration, this.displayFloor);
  }
  rebuildDisplayIndex() {
    this.displayRowsByUtterance.clear(); this.displayRowsByBlock.clear();
    this.displayRowsBySource.clear();
    this.displayBlocksByUtterance.clear(); this.displayBlocksBySource.clear();
    this.displayLooseRows = [];
    const add = (index, id, value) => {
      if (!validId(id)) return;
      if (!index.has(id)) index.set(id, new Set());
      index.get(id).add(value);
    };
    for (const row of this.displayRows.values()) {
      add(this.displayRowsByBlock, row.block_id, row.key);
      for (const uid of new Set([row.utterance_id, ...array(row.source_utterance_ids)]))
        add(this.displayRowsByUtterance, uid, row.key);
      for (const id of array(row.source_ids)) add(this.displayRowsBySource, id, row.key);
      if (row.stage !== 3 && !validId(row.utterance_id) && !array(row.source_utterance_ids).some(validId)) this.displayLooseRows.push(row);
    }
    for (const projection of this.displayBlocks.values()) {
      for (const uid of Object.keys(projection.consumed_utterance_ranges || {}).map(Number))
        add(this.displayBlocksByUtterance, uid, projection.blockId);
      for (const id of new Set([...array(projection.document?.source_ids),
        ...revisionsFor(projection.document || {}).keys()])) add(this.displayBlocksBySource, id, projection.blockId);
    }
  }
  displayRowsForBlock(id) {
    return [...(this.displayRowsByBlock.get(id) || [])].map((key) => this.displayRows.get(key)).filter(Boolean).sort(displayOrder);
  }
  displayRowsForUtterance(id) {
    return [...(this.displayRowsByUtterance.get(id) || [])].map((key) => this.displayRows.get(key))
      .filter((row) => row && row.stage !== 3).sort(displayOrder);
  }
  orderedDisplayRows() {
    return [...this.displayRows.values()].sort(displayOrder);
  }
  retireDisplay({ blockIds = [], utteranceIds = [], sourceIds = [], all = false, generation = null } = {}) {
    if (!this.canonical) return;
    const threshold = validId(generation) ? generation - 1 : this.displayGeneration;
    const require = (index, id) => index.set(id, Math.max(index.get(id) || 0, threshold));
    const affected = new Set(blockIds.filter(validId));
    for (const uid of utteranceIds) for (const id of this.displayBlocksByUtterance.get(uid) || []) affected.add(id);
    for (const sid of sourceIds) for (const id of this.displayBlocksBySource.get(sid) || []) affected.add(id);
    if (all) for (const id of this.displayBlocks.keys()) affected.add(id);
    const keys = new Set(all ? this.displayRows.keys() : [...affected].flatMap((id) => [...(this.displayRowsByBlock.get(id) || [])]));
    for (const id of utteranceIds) for (const key of this.displayRowsByUtterance.get(id) || []) keys.add(key);
    for (const id of sourceIds) for (const key of this.displayRowsBySource.get(id) || []) keys.add(key);
    for (const uid of utteranceIds.filter(validId)) require(this.displayPendingUtterances, uid);
    for (const sid of sourceIds.filter(validId)) require(this.displayPendingSources, sid);
    const remove = (index, id, value) => {
      const owners = index.get(id); owners?.delete(value); if (!owners?.size) index.delete(id);
    };
    for (const id of affected) {
      require(this.displayPendingBlocks, id);
      const projection = this.displayBlocks.get(id);
      if (projection) {
        for (const uid of Object.keys(projection.consumed_utterance_ranges || {}).map(Number)) remove(this.displayBlocksByUtterance, uid, id);
        for (const sid of new Set([...array(projection.document?.source_ids), ...revisionsFor(projection.document || {}).keys()]))
          remove(this.displayBlocksBySource, sid, id);
        this.displayBlocks.delete(id); this.markBlock(id); this.legacyInvalidatedBlocks.add(id);
      }
    }
    for (const key of keys) {
      const row = this.displayRows.get(key); if (!row) continue;
      this.displayRows.delete(key); this.markBlock(row.block_id);
      remove(this.displayRowsByBlock, row.block_id, key);
      for (const uid of [row.utterance_id, ...array(row.source_utterance_ids)]) remove(this.displayRowsByUtterance, uid, key);
      for (const sid of array(row.source_ids)) remove(this.displayRowsBySource, sid, key);
      for (const id of [row.utterance_id, ...array(row.source_utterance_ids)].filter(validId)) {
        this.legacyChangedTurns.add(id); require(this.displayPendingUtterances, id);
      }
    }
    if (keys.size) this.displayLooseRows = this.displayLooseRows.filter((row) => !keys.has(row.key));
  }
  displayInputThreshold({ blockId = null, utteranceIds = [], sourceIds = [] } = {}) {
    return Math.max(this.displayPendingBlocks.get(blockId) || 0,
      ...utteranceIds.map((id) => this.displayPendingUtterances.get(id) || 0),
      ...sourceIds.map((id) => this.displayPendingSources.get(id) || 0));
  }
  displayInputCurrent(generation, inputs = {}) {
    return generation > this.displayInputThreshold(inputs);
  }
  mergeDisplay(payload, older, pagination) {
    if (!this.acceptsDisplay(payload)) return false;
    const sameGeneration = this.canonical && payload.display_generation === this.displayGeneration;
    if (!Array.isArray(payload.display_blocks) || !Array.isArray(payload.display_rows))
      throw new Error('저장된 표시 판본이 올바르지 않습니다.');
    if (!this.canonical) {
      for (const id of [...this.documents.keys()]) this.deleteDocument(id);
      for (const id of [...this.liveTurns.keys()]) this.deleteLiveTurn(id);
    }
    this.canonical = true;
    this.displayAwaiting = false;
    this.displayGeneration = payload.display_generation;
    if (validId(payload.snapshot_at)) this.snapshotAt = Math.max(this.snapshotAt, payload.snapshot_at);
    if (Array.isArray(payload.valid_document_ids)) this.validDocumentIds = new Set(payload.valid_document_ids.filter(validId));
    const blocks = Array.isArray(payload.display_valid_block_ids) ? new Set(payload.display_valid_block_ids) : null;
    const keys = Array.isArray(payload.display_valid_row_ids) ? new Set(payload.display_valid_row_ids) : null;
    const covered = new Set(array(payload.display_utterance_ids));
    for (const uid of covered) {
      this.displayCoveredUtterances.add(uid);
    }
    const incoming = new Set(payload.display_rows.map((row) => row.key));
    for (const [id, projection] of this.displayBlocks) if (blocks && !blocks.has(id)
        || this.validDocumentIds && !this.validDocumentIds.has(projection.id)) {
      this.displayBlocks.delete(id); this.markBlock(id); this.legacyInvalidatedBlocks.add(id);
    }
    // 같은 세대의 최근 페이지가 늦게 와도 전역 유효 목록에 남은 이전 표시행은 보존한다.
    for (const [key, row] of this.displayRows) if (keys && !keys.has(key)
        || !incoming.has(key) && (!sameGeneration || !keys?.has(key))
          && [row.utterance_id, ...array(row.source_utterance_ids)].some((id) => covered.has(id))) {
      this.displayRows.delete(key); this.markBlock(row.block_id);
      for (const id of [row.utterance_id, ...array(row.source_utterance_ids)].filter(validId)) {
        this.legacyChangedTurns.add(id);
        // 다른 페이지의 새 선택은 이 응답에 없을 수 있다. 그 범위는 원기록을 남기고 명시 조회를 기다린다.
        if (!covered.has(id)) this.displayPendingUtterances.set(id, payload.display_generation - 1);
      }
    }
    // 원문은 배지의 드릴다운용이다. 새 표시 경로에서는 판본·문자 연결을 재판정하지 않는다.
    for (const source of array(payload.sources)) {
      const previous = this.sources.get(source.id);
      if (!previous || Number(previous.revision) <= Number(source.revision)) this.sources.set(source.id, source);
    }
    for (const block of array(payload.blocks)) {
      this.blocks.set(block.id, block); this.displaySummaries.set(block.id, block.summary || null);
    }
    for (const [id, doc] of this.documents) if (this.validDocumentIds && !this.validDocumentIds.has(id)) this.deleteDocument(id);
    for (const doc of array(payload.documents)) if (!this.validDocumentIds || this.validDocumentIds.has(doc.id)) {
      const threshold = this.displayInputThreshold({ blockId: doc.block_id,
        sourceIds: [...array(doc.source_ids), ...revisionsFor(doc).keys()] });
      if (payload.display_generation <= threshold) this.displayPendingBlocks.set(doc.block_id, threshold);
      this.setDocument(doc);
    }
    for (const projection of payload.display_blocks) {
      const id = projection?.blockId;
      if (!validId(id) || !validId(projection.id) || blocks && !blocks.has(id)
          || this.validDocumentIds && !this.validDocumentIds.has(projection.id)) continue;
      const threshold = this.displayInputThreshold({ blockId: id,
            utteranceIds: Object.keys(projection.consumed_utterance_ranges || {}).map(Number),
            sourceIds: [...array(projection.document?.source_ids), ...revisionsFor(projection.document || {}).keys()] });
      if (payload.display_generation <= threshold) { this.displayPendingBlocks.set(id, threshold); continue; }
      if (this.displayBlocks.get(id)?.fingerprint !== projection.fingerprint) {
        this.displayBlocks.set(id, projection); this.markBlock(id);
      }
      if (projection.document) this.setDocument(projection.document);
    }
    for (const row of payload.display_rows) {
      if (typeof row?.key !== 'string' || ![1, 2, 3].includes(row.stage) || typeof row.text !== 'string'
          || keys && !keys.has(row.key) || !this.displayInputCurrent(payload.display_generation,
            { blockId: row.block_id, utteranceIds: [row.utterance_id, ...array(row.source_utterance_ids)],
              sourceIds: array(row.source_ids) })) continue;
      if (JSON.stringify(this.displayRows.get(row.key)) === JSON.stringify(row)) continue;
      this.displayRows.set(row.key, row); this.markBlock(row.block_id);
      for (const id of [row.utterance_id, ...array(row.source_utterance_ids)].filter(validId)) this.legacyChangedTurns.add(id);
    }
    for (const [id, generation] of this.displayPendingBlocks) if (payload.display_generation > generation)
      this.displayPendingBlocks.delete(id);
    for (const id of covered) if (payload.display_generation > (this.displayPendingUtterances.get(id) || 0))
      this.displayPendingUtterances.delete(id);
    this.rebuildDisplayIndex();
    if (pagination && (older || !this.paginationInitialized)) {
      this.nextBefore = validId(payload.next_before) ? payload.next_before : null; this.paginationInitialized = true;
    }
    return true;
  }
  markBlock(id) {
    if (!validId(id)) return;
    this.legacyChangedBlocks.add(id);
    this.blockVersions.set(id, (this.blockVersions.get(id) || 0) + 1);
  }
  indexDependencies(id, value, dependencies, index) {
    for (const sourceId of dependencies.get(id) || []) {
      const owners = index.get(sourceId); owners?.delete(id);
      if (!owners?.size) index.delete(sourceId);
    }
    dependencies.delete(id);
    if (!value) return;
    const refs = new Set(revisionsFor(value).keys());
    dependencies.set(id, refs);
    for (const sourceId of refs) {
      if (!index.has(sourceId)) index.set(sourceId, new Set());
      index.get(sourceId).add(id);
    }
  }
  setDocument(doc) {
    const previous = this.documents.get(doc.id);
    if (previous && JSON.stringify(previous) === JSON.stringify(doc)) return false;
    if (previous) this.documentsByBlock.get(previous.block_id)?.delete(doc.id);
    this.documents.set(doc.id, doc);
    this.indexDependencies(doc.id, doc, this.documentDeps, this.documentsBySource);
    if (!this.documentsByBlock.has(doc.block_id)) this.documentsByBlock.set(doc.block_id, new Set());
    this.documentsByBlock.get(doc.block_id).add(doc.id);
    this.markBlock(doc.block_id);
    return true;
  }
  deleteDocument(id) {
    const doc = this.documents.get(id);
    if (!doc) return false;
    this.documents.delete(id);
    this.indexDependencies(id, null, this.documentDeps, this.documentsBySource);
    this.documentsByBlock.get(doc.block_id)?.delete(id);
    this.markBlock(doc.block_id);
    this.legacyInvalidatedBlocks.add(doc.block_id);
    return true;
  }
  markTurnDocuments(turn) {
    for (const sourceId of revisionsFor(turn).keys()) for (const id of this.documentsBySource.get(sourceId) || []) {
      const doc = this.documents.get(id);
      if (doc) this.markBlock(doc.block_id);
    }
  }
  setLiveTurn(turn) {
    const previous = this.liveTurns.get(turn.id);
    if (previous && JSON.stringify(previous) === JSON.stringify(turn)) return false;
    if (previous) { this.markTurnDocuments(previous); this.markBlock(previous.block_id); }
    this.liveTurns.set(turn.id, turn);
    this.markBlock(turn.block_id);
    this.indexDependencies(turn.id, turn, this.turnDeps, this.turnsBySource);
    this.markTurnDocuments(turn);
    this.legacyChangedTurns.add(turn.id);
    return true;
  }
  deleteLiveTurn(id) {
    const turn = this.liveTurns.get(id);
    if (!turn) return false;
    this.markTurnDocuments(turn); this.markBlock(turn.block_id);
    this.liveTurns.delete(id);
    this.indexDependencies(id, null, this.turnDeps, this.turnsBySource);
    this.legacyChangedTurns.add(id);
    return true;
  }
  takeLegacyChanges() {
    const change = { blockIds: [...this.legacyChangedBlocks], invalidatedBlockIds: [...this.legacyInvalidatedBlocks],
      utteranceIds: [...this.legacyChangedTurns] };
    this.legacyChangedBlocks.clear(); this.legacyInvalidatedBlocks.clear(); this.legacyChangedTurns.clear();
    return change;
  }
  latestDocument(blockId) {
    return [...(this.documentsByBlock.get(blockId) || [])].map((id) => this.documents.get(id))
      .filter((doc) => doc && ['draft', 'partial', 'ready'].includes(doc.status))
      .sort((a, b) => Number(b.updated_at || b.created_at || 0) - Number(a.updated_at || a.created_at || 0) || b.id - a.id)[0] || null;
  }
  turnsForSources(ids) {
    const wanted = new Set([...ids].flatMap((id) => [...(this.turnsBySource.get(id) || [])]));
    return [...wanted].map((id) => this.liveTurns.get(id)).filter((turn) => turn && validLiveTurn(turn, this.sources));
  }
  source(source, preserveGuess = false, deferred = false) {
    let invalidated = false;
    for (const id of array(source?.invalidated_document_ids).filter(validId)) {
      this.invalidDocuments.add(id);
      invalidated = this.deleteDocument(id) || invalidated;
    }
    if (!source || !validId(source.id) || !Number.isSafeInteger(Number(source.revision))) return invalidated;
    const previous = this.sources.get(source.id);
    if (previous && Number(previous.revision) > Number(source.revision)) return invalidated;
    // 기본 SSE에는 읽기 투영 후보가 없다. 동일 원문 판본의 생략만 보존하고 API의 철회는 따른다.
    if (preserveGuess && previous && !Object.hasOwn(source, 'speaker_guess')
        && Number(previous.revision) === Number(source.revision)
        && JSON.stringify([previous.text, previous.parts, previous.final, previous.scd, previous.replayed === true])
          === JSON.stringify([source.text, source.parts, source.final, source.scd, source.replayed === true])) {
      const guess = currentSpeakerGuess(previous, this.sources);
      if (guess) source = { ...source, speaker_guess: guess };
    }
    const changed = !previous || JSON.stringify(previous) !== JSON.stringify(source);
    if (changed) {
      this.sources.set(source.id, source);
      for (const id of this.turnsBySource.get(source.id) || []) this.markBlock(this.liveTurns.get(id)?.block_id);
      for (const id of this.documentsBySource.get(source.id) || []) {
        const doc = this.documents.get(id); if (doc) this.markBlock(doc.block_id);
      }
    }
    if (!deferred && changed) this.invalidate(new Set([source.id]));
    return true;
  }
  invalidate(sourceIds = null) {
    const docs = sourceIds ? new Set([...sourceIds].flatMap((id) => [...(this.documentsBySource.get(id) || [])]))
      : new Set(this.documents.keys());
    const turns = sourceIds ? new Set([...sourceIds].flatMap((id) => [...(this.turnsBySource.get(id) || [])]))
      : new Set(this.liveTurns.keys());
    for (const id of docs) if (!this.documentCurrent(this.documents.get(id))) this.deleteDocument(id);
    for (const id of turns) if (!validLiveTurn(this.liveTurns.get(id), this.sources)) this.deleteLiveTurn(id);
  }
  documentCurrent(doc) {
    if (this.canonical) return !!doc && this.documents.get(doc.id) === doc
      && (!this.validDocumentIds || this.validDocumentIds.has(doc.id)) && !this.displayPendingBlocks.has(doc.block_id);
    const block = this.blocks.get(doc.block_id);
    return validDocument(doc, this.sources) && (!block || block.review_status !== 'reviewed' && !humanSummary(block) && !block.is_open
      && (!block.questioner || block.questioner === doc.questioner));
  }
  invalidateMetadata(block = null, generation = null) {
    if (this.canonical) {
      if (!block) this.displayFloor = Math.max(this.displayFloor, validId(generation) ? generation : this.displayGeneration + 1);
      this.retireDisplay(block ? { blockIds: [block.id], generation } : { all: true, generation }); return;
    }
    const affected = new Set();
    for (const doc of this.documents.values()) if (!block || doc.block_id === block.id) {
      for (const id of revisionsFor(doc).keys()) affected.add(id);
    }
    for (const turn of this.liveTurns.values()) if (!block || turn.block_id === block.id) {
      for (const id of revisionsFor(turn).keys()) affected.add(id);
    }
    for (const source of this.sources.values()) if (!block || Number.isFinite(block.ts_start)
        && source.first_rx >= block.ts_start && (!block.ts_end || source.first_rx <= block.ts_end)) affected.add(source.id);
    for (const [id, doc] of this.documents) if (!block || doc.block_id === block.id
        || [...revisionsFor(doc).keys()].some((uid) => affected.has(uid))) this.deleteDocument(id);
    for (const [id, source] of this.sources) if (!block || affected.has(id)
        || [...revisionsFor(source.speaker_guess || {}).keys()].some((uid) => affected.has(uid))) {
      const { speaker_guess, ...plain } = source; this.sources.set(id, plain);
    }
    for (const [id, turn] of this.liveTurns) if (!block || turn.block_id === block.id
        || [...revisionsFor(turn.speaker_guess || {}).keys()].some((uid) => affected.has(uid))) {
      if (protectedTurn(turn) || turn.speaker_source === 'feed') {
        const { speaker_guess, ...plain } = turn; this.setLiveTurn(plain);
      } else this.deleteLiveTurn(id);
    }
  }
  block(value) {
    if (!validId(value?.id)) return false;
    const previous = this.blocks.get(value.id), block = { ...previous, ...value };
    this.blocks.set(block.id, block);
    if (this.canonical) return false;
    const stamp = (item) => JSON.stringify([item.questioner, item.questioner_party, item.kind, item.ts_start,
      item.is_open, item.is_open ? null : item.ts_end, item.review_status || 'draft', humanSummary(item)]);
    const mismatch = [...this.documents.values()].some((doc) => doc.block_id === block.id
      && (block.is_open || block.questioner && doc.questioner !== block.questioner));
    if (previous ? stamp(previous) === stamp(block) : block.review_status !== 'reviewed' && !humanSummary(block) && !mismatch) return false;
    this.invalidateMetadata(block);
    return true;
  }
  merge(payload, older = false, pagination = true) {
    if (payload?.version !== 3) throw new Error('지원하지 않는 자막 판본입니다.');
    if (Object.hasOwn(payload, 'display_blocks')) return this.mergeDisplay(payload, older, pagination);
    if (this.canonical) return false;
    const incoming = array(payload.sources), docs = array(payload.documents);
    if (Number.isSafeInteger(payload.snapshot_at) && payload.snapshot_at > 0) {
      if (payload.snapshot_at < this.snapshotAt) {
        for (const source of incoming) if (!this.sources.has(source.id)
            || Number(source.revision) > Number(this.sources.get(source.id).revision)) this.source(source);
        return false;
      }
      this.snapshotAt = payload.snapshot_at;
    }
    // 전체 유효 ID가 있는 새 API는 현재 페이지 밖에 남은 예전 AI 판정도 무효화한다.
    if (Array.isArray(payload.valid_document_ids) && payload.valid_document_ids.every(validId)) {
      this.validDocumentIds = new Set(payload.valid_document_ids);
    }
    const authoritativeIds = this.validDocumentIds;
    const touched = new Set(incoming.filter((s) => !this.sources.has(s.id)
      || Number(s.revision) >= Number(this.sources.get(s.id).revision)
        && JSON.stringify(s) !== JSON.stringify(this.sources.get(s.id))).map((s) => s.id));
    const receivedIds = new Set(docs.map((doc) => doc.id));
    const covered = new Set(incoming.filter((row) => !this.sources.has(row.id)
      || Number(row.revision) >= Number(this.sources.get(row.id).revision)).map((row) => row.id));
    // 서버가 공개하지 않는 판본은 이전 응답에서 되살리지 않는다.
    for (const [id, doc] of this.documents) {
      if (authoritativeIds && !authoritativeIds.has(id)) {
        this.deleteDocument(id);
        continue;
      }
      if (!authoritativeIds && array(doc.source_ids).some((sid) => covered.has(sid)) && !receivedIds.has(id)) this.deleteDocument(id);
    }
    for (const source of incoming) this.source(source, false, true);
    this.invalidate(touched);
    for (const doc of docs) {
      if (authoritativeIds && !authoritativeIds.has(doc.id)) {
        continue;
      }
      const previous = this.documents.get(doc.id);
      if (previous && JSON.stringify(previous) === JSON.stringify(doc)) continue;
      if (this.invalidDocuments.has(doc.id) || !this.documentCurrent(doc)) continue;
      if (previous && Number(previous.updated_at || previous.created_at || 0) > Number(doc.updated_at || doc.created_at || 0)) continue;
      this.setDocument(doc);
    }
    if (Array.isArray(payload.valid_live_turn_ids) && payload.valid_live_turn_ids.every(validId)) {
      this.validLiveTurnIds = new Set(payload.valid_live_turn_ids);
      for (const id of this.liveTurns.keys()) if (!this.validLiveTurnIds.has(id)) this.deleteLiveTurn(id);
    }
    for (const turn of array(payload.live_turns)) {
      const previous = this.liveTurns.get(turn.id);
      if (previous && JSON.stringify(previous) === JSON.stringify(turn)) continue;
      if (this.validLiveTurnIds && !this.validLiveTurnIds.has(turn.id) || !validLiveTurn(turn, this.sources)) continue;
      if (previous && Number(previous.updated_at || 0) > Number(turn.updated_at || 0)) continue;
      this.setLiveTurn(turn);
    }
    // 최신 페이지 갱신이 이미 펼친 과거 페이지의 커서를 되감지 않게 한다.
    if (pagination && (older || !this.paginationInitialized)) {
      this.nextBefore = validId(payload.next_before) ? payload.next_before : null;
      this.paginationInitialized = true;
    }
    return true;
  }
  orderedSources() { return [...this.sources.values()].sort(sourceOrder); }
  orderedLiveTurns() { return [...this.liveTurns.values()].filter((turn) => validLiveTurn(turn, this.sources))
    .map((turn) => effectiveTurn(turn, this.sources)); }
  liveUtterance(type, value) {
    if (!validId(value?.id)) return false;
    const previous = this.liveTurns.get(value.id);
    if (!previous) return false;
    if (type === 'utterance.delete' || typeof value.text !== 'string'
        || value.text !== previous.text && value.edited !== true && value.original_text !== previous.original_text) {
      return this.deleteLiveTurn(value.id);
    }
    const updated = { ...previous };
    for (const key of ['speaker', 'speaker_role', 'speaker_source', 'text', 'is_final', 'review_parts', 'block_id', 'edited']) {
      if (key in value) updated[key] = value[key];
    }
    updated.protected = previous.protected === true || protectedTurn(updated);
    if (!validLiveTurn(updated, this.sources)) return this.deleteLiveTurn(value.id);
    return this.setLiveTurn(updated);
  }
  orderedDocuments() {
    if (this.canonical) return [...this.documents.values()].filter((doc) => this.documentCurrent(doc))
      .sort((a, b) => Number(a.created_at || 0) - Number(b.created_at || 0) || a.id - b.id);
    return [...this.documents.values()].filter((doc) => this.documentCurrent(doc)).sort((a, b) => {
      const first = (doc) => Math.min(...doc.source_ids.map((id) => Number(this.sources.get(id)?.first_rx || 0)));
      return first(a) - first(b) || a.id - b.id;
    });
  }
}

function sourceState(source) {
  if (recordingSource(source)) return source.final === true
    ? '전사 확정 · 인식 오류 가능' : '전사 중';
  if (source.replayed === true) return '이전 자막 재전송 · 보정 제외';
  if (source.final === true && source.scd === true) return '최종 분할 수신';
  if (source.final === true) return '화자 분할 대기';
  return '수신 중';
}
function sourceLabel(label, source) {
  if (recordingSource(source)) return typeof label === 'string' && /^nemo:(?:e[1-9][0-9]{0,5}:)?[0-7]$/.test(label)
    ? `익명 음향 화자 ${label}${source.acoustic_overlap === true ? ' · 겹침 확인 필요' : ''}` : '음향 화자 미지정';
  return label === null || label === undefined || label === '' || Number(label) < 0 ? '국회 표식 미지정' : `국회 표식 ${String(label)}`;
}
function sourceTitle(source) { return `${recordingSource(source) ? '녹음 전사 조각' : '자막'} ${source.segment ?? source.id}`; }
function partsFor(source) {
  const parts = array(source.parts);
  return parts.length ? parts : [{ label: null, text: source.transcript ?? source.text ?? '' }];
}
function timeFor(source) { return Number(source?.first_rx) > 0 ? fmt.hms(Number(source.first_rx)) : '시각 미상'; }
function pill(text, kind = '') { return h('span', { class: `transcript-stage-pill ${kind}` }, text); }

function sourceDetails(source) {
  return h('details', { class: 'transcript-source-detail' }, h('summary', null, recordingSource(source) ? '원전사 보기' : '수신 원문 보기'),
    h('p', { class: 'small muted' }, `${sourceTitle(source)} · 판본 ${source.revision} · ${sourceState(source)}`),
    h('div', { class: 'transcript-source-parts' }, ...partsFor(source).map((part) => h('p', null,
      h('strong', null, `${sourceLabel(part.label, source)}: `), String(part.text ?? '')))));
}

export function renderSource(source) {
  const article = h('article', { class: 'transcript-source', id: `caption-source-${source.id}`, tabindex: '-1' });
  article.dataset.sourceId = String(source.id);
  article.append(h('div', { class: 'transcript-source-heading' }, h('time', null, timeFor(source)),
    h('span', { class: 'small muted' }, sourceTitle(source)),
    pill(sourceState(source), !source.replayed && settledSource(source) ? 'settled' : 'pending')));
  if (source.replayed) article.append(h('p', { class: 'small muted' }, '표시 시각은 재수신 시각입니다. 새 블록의 보정 대상에는 넣지 않습니다.'));
  for (const part of partsFor(source)) {
    article.append(h('div', { class: 'transcript-stage-row' },
      h('span', { class: 'transcript-stage-time', 'aria-hidden': 'true' }, ''),
      h('div', { class: 'transcript-stage-speaker' }, sourceLabel(part.label, source)),
      h('div', { class: 'transcript-stage-body transcript-source-body' }, String(part.text ?? ''))));
  }
  return article;
}

function roleName(sentence) {
  const name = String(sentence.speaker || '');
  return name && name !== '화자 확인 필요' ? name : ROLE_NAMES[sentence.role] || '기타 발언';
}
function roleUncertain(sentence) {
  if (typeof sentence.role_uncertain === 'boolean') return sentence.role_uncertain;
  return ['uncertain', 'held'].includes(sentence.role_status) || !ROLE_NAMES[sentence.role];
}
function sentenceUncertain(sentence) {
  return roleUncertain(sentence) || sentence.text_uncertain === true;
}
function sentenceSourceIds(sentence) {
  return [...new Set(array(sentence.source_spans).map((span) => Number(span.source_id)).filter(validId))];
}
function readableUncertainty(value) {
  if (typeof value === 'string') return UNCERTAINTIES[value] || (/^[a-z_]+$/.test(value) ? '추가 확인 필요' : value);
  return String(value?.message || value?.reason || value?.text || '문맥 확인 필요');
}
function sentenceEvidence(sentence, store, onSource, prefix) {
  const edits = array(sentence.edits);
  const unclear = array(sentence.uncertainties).filter((value) => !['role_held', 'role_uncertain'].includes(value));
  const details = h('details', { class: 'transcript-sentence-evidence', id: `${prefix}-evidence-${String(sentence.id).replace(/[^a-zA-Z0-9_-]/g, '_')}` }, h('summary', null,
    `원문·수정 내역${edits.length ? ` ${edits.length}건` : ''}`));
  details.append(h('p', { class: 'transcript-original' }, String(sentence.original_text ?? sentence.text ?? '')));
  for (const edit of edits) {
    details.append(h('p', { class: 'transcript-edit' }, h('span', { class: 'transcript-before' }, String(edit.before ?? '')),
      h('span', { class: 'sr-only' }, '에서'), ' → ', h('strong', null, String(edit.after ?? '')),
      edit.reason ? h('span', { class: 'small muted' }, ` · ${String(edit.reason)}`) : null));
  }
  if (unclear.length) details.append(h('ul', { class: 'transcript-uncertainties' }, ...unclear.map((u) => h('li', null, readableUncertainty(u)))));
  const jevRole = sentence.jev_role || sentence.jev?.role || sentence.jev?.choice || sentence.jev?.raw_choice;
  if (sentence.jev && ROLE_NAMES[jevRole]) {
    const candidate = ROLE_NAMES[jevRole];
    details.append(h('p', { class: 'small muted' }, `Jev 초기 후보: ${candidate} · 문맥 보정 후: ${ROLE_NAMES[sentence.role] || '기타 발언'}`));
    const probabilities = sentence.jev.probabilities;
    if (probabilities && typeof probabilities === 'object') {
      const values = Object.entries(ROLE_NAMES).filter(([role]) => Number.isFinite(probabilities[role]) && probabilities[role] >= 0 && probabilities[role] <= 1)
        .map(([role, name]) => `${name} ${(probabilities[role] * 100).toFixed(1)}%`);
      if (values.length) details.append(h('p', { class: 'small muted' }, `모델 후보 분포 · ${values.join(' / ')}. 실제 정확도를 뜻하지 않습니다.`));
    }
  }
  const refs = h('div', { class: 'transcript-source-links' });
  for (const id of sentenceSourceIds(sentence)) {
    const source = store.sources.get(id);
    if (!source) continue;
    refs.append(h('button', { type: 'button', class: 'btn btn-xs btn-ghost', onclick: () => onSource(id) }, `${sourceTitle(source)} 보기`));
    refs.append(sourceDetails(source));
  }
  details.append(refs);
  return details;
}

/** 문장 누락·중복·화자 혼합이 있는 문단 묶음은 문장별 표시로 되돌린다. */
export function groupsFor(sentences, paragraphs) {
  const byId = new Map(sentences.map((s) => [String(s.id), s]));
  const ids = sentences.map((s) => String(s.id));
  const groups = array(paragraphs).map((p) => array(p.sentence_ids).map((id) => byId.get(String(id))));
  const actual = groups.flat().map((s) => String(s?.id));
  if (!groups.length || actual.join('\0') !== ids.join('\0') || groups.some((g) => !g.length || g.some((s) => !s || s.role !== g[0].role || roleName(s) !== roleName(g[0])))) {
    return sentences.map((s) => [s]);
  }
  return groups;
}

function summaryText(item) {
  return typeof item === 'string' ? item : String(item?.text || item?.content || item?.description || '');
}

function sentenceAnchor(prefix, id) { return `${prefix}-sentence-${String(id).replace(/[^a-zA-Z0-9_-]/g, '_')}`; }

export function renderBlockSummary(summary, sentences = [], prefix = '', showEvidence = false) {
  if (!summary || typeof summary !== 'object') return null;
  if (summary.status === 'held') return showEvidence ? h('p', { class: 'transcript-document-status' }, '요약 초안이 생성되지 않았습니다.') : null;
  const sections = [
    ['질의 요지', array(summary.bullets)], ['요구 사항', array(summary.key_requests)], ['답변·약속', array(summary.commitments)],
  ].filter(([, items]) => items.some((item) => summaryText(item)));
  if (!summary.title && !sections.length) return null;
  const validSentenceIds = new Set(sentences.map((sentence) => String(sentence.id)));
  const evidence = new Map(array(summary.evidence).filter((row) => Number.isSafeInteger(row.bullet) && row.bullet >= 0)
    .map((row) => [row.bullet, array(row.sentence_ids).map(String).filter((id) => validSentenceIds.has(id))]));
  const sectionNodes = sections.map(([title, items]) => {
    const list = h('ul');
    items.forEach((item, index) => {
      if (!summaryText(item)) return;
      const refs = showEvidence && title === '질의 요지' && prefix ? (evidence.get(index) || []) : [];
      list.append(h('li', null, summaryText(item), refs.length ? h('span', { class: 'transcript-summary-evidence' },
        ...refs.map((id) => h('a', { href: `#${sentenceAnchor(prefix, id)}`, class: 'small' }, `근거 ${id}`))) : null));
    });
    return h('div', { class: 'transcript-summary-section' }, h('h4', null, title), list);
  });
  const origin = summaryPresentation(summary);
  return h('section', { class: 'transcript-block-summary', 'aria-label': '질의 블록 요약 초안' },
    h('div', { class: 'transcript-summary-heading' }, h('strong', null, summary.title || '질의 블록 요약')),
    h('p', { class: 'small muted summary-origin-note' },
      [origin.label, origin.note, origin.coverageLabel, origin.coverageNote].filter(Boolean).join(' ')), ...sectionNodes);
}

function correctionDetails(doc, sentences, store, onSource, prefix) {
  const unsettled = doc.source_settlement_policy?.policy === 'explicit_chair_declaration'
    && array(doc.source_settlement_policy.unconfirmed_source_ids).length > 0;
  const detail = h('details', { class: 'transcript-correction-detail', id: `${prefix}-correction` },
    h('summary', { class: 'transcript-correction-badge', title: '원문과 보정 내역 보기' },
      doc.status === 'processing' ? 'AI 보정 중' : doc.status === 'held' ? 'AI 보정 미적용'
        : unsettled ? 'AI 보정 · 미확정 자막 포함' : 'AI 보정'));
  const content = h('div', { class: 'transcript-correction-content' });
  content.append(h('p', { class: 'small muted' }, 'AI가 정리한 내용입니다. 원문과 수정 내역을 대조할 수 있습니다.'));
  if (unsettled) content.append(h('p', { class: 'small' },
    '위원장의 종료·정회 선언을 확인해 보정했습니다. 국회 final·scd가 없는 조각은 원문과 미확정 상태를 그대로 보존했습니다.'));
  if (array(doc.issues).length) content.append(h('ul', { class: 'transcript-uncertainties' }, ...array(doc.issues).map((issue) => h('li', null, readableUncertainty(issue)))));
  const summary = renderBlockSummary(doc.summary, sentences, prefix, true);
  if (summary) content.append(summary);
  for (const proposal of array(doc.paragraphs).filter((p) => p.body_basis === 'source_group_v2')) {
    const edits = array(proposal.edits);
    if (!edits.length && !proposal.text_uncertain && !proposal.role_uncertain) continue;
    content.append(h('section', { class: 'transcript-group-evidence' }, h('h4', null, '말묶음 수정 내역'),
      h('p', { class: 'transcript-original' }, String(proposal.original_text || '')),
      ...edits.map((edit) => h('p', null, `${String(edit.before || '')} → ${String(edit.after || '')} · ${String(edit.reason || '')}`)),
      proposal.role_uncertain || proposal.text_uncertain ? pill('불확실', 'pending') : null));
  }
  for (const sentence of sentences) {
    const section = h('section', { class: 'transcript-evidence-row' }, h('strong', null, roleName(sentence)));
    if (sentenceUncertain(sentence)) section.append(pill('불확실', 'pending'));
    section.append(sentenceEvidence(sentence, store, onSource, prefix));
    content.append(section);
  }
  detail.append(content);
  return detail;
}

function correctionWarnings(value) {
  const labels = correctionNotices(value);
  return labels.length ? h('div', { class: 'transcript-correction-warnings', role: 'note' },
    ...labels.map(label => pill(label, 'pending'))) : null;
}

/** 보정 본문의 표시 줄만 나눈다. 모든 원문 문자와 기존 줄바꿈은 그대로 남긴다. */
export function correctionReadingChunks(value) {
  const text = String(value ?? ''), mask = Array(text.length).fill(false), stack = [];
  const pairs = { '“': '”', '‘': '’', '「': '」', '『': '』', '(': ')', '[': ']', '{': '}' };
  for (let index = 0; index < text.length; index++) {
    const char = text[index], before = stack.length > 0;
    if (pairs[char]) stack.push(pairs[char]);
    else if (char === '"' || char === "'") {
      const apostrophe = char === "'" && /[A-Za-z]/.test(text[index - 1] || '') && /[A-Za-z]/.test(text[index + 1] || '');
      if (!apostrophe) { if (stack.at(-1) === char) stack.pop(); else stack.push(char); }
    } else if (stack.at(-1) === char) stack.pop();
    mask[index] = before || stack.length > 0;
  }
  for (const match of text.matchAll(/\b(?:https?:\/\/|www\.)\S+/g)) {
    for (let index = match.index; index < match.index + match[0].length; index++) mask[index] = true;
  }
  const ending = /(?:니다|습니까|십니까|십시오|십시다|주세요|세요|지요|인가요|나요|까요|해요|돼요|어요|아요|군요|네요|거든요|잖아요|예요|이에요|죠|했나|겠나|었나|았나|했다|한다|된다|있다|없다|이다|아니다|그렇다|맞다|겠다|였다|었다|았다)$/;
  const quotative = /^(?:라고|이라는|이라고|라는|라며|이라며|고\s*(?:말|하|했)|는\s*(?:말|문구|표현))/;
  const terminal = (word) => ending.test(word) || word.endsWith('니까') && word.length >= 3
    && (word.charCodeAt(word.length - 3) - 0xac00) % 28 === 17;
  const chunks = [];
  let start = 0;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    let end = null;
    if (!mask[index]) {
      if (/[.?!。！？]/.test(char)) {
        const before = text[index - 1] || '', after = text.slice(index + 1), prefix = text.slice(start, index).trim();
        const numeric = char === '.' && /\d/.test(before) && (/^\s*\d/.test(after)
          || /^\d{1,4}(?:\s*\.\s*\d{1,2}){0,2}$/.test(prefix));
        const abbreviation = char === '.' && (/[A-Za-z]/.test(before) && /^[A-Za-z]/.test(after)
          || /(?:^|\s)(?:[A-Za-z]\.){2,}$/.test(text.slice(start, index + 1)));
        if (!numeric && !abbreviation) {
          end = index + 1;
          while (end < text.length && /[.?!。！？]/.test(text[end])) end++;
        }
      } else if (/\s/.test(char)) {
        const word = /[가-힣]+$/.exec(text.slice(start, index));
        if (word && terminal(word[0]) && !quotative.test(text.slice(index).trimStart())) end = index;
      }
    }
    if (/[”’」』"']/.test(char) && mask[index] && (index + 1 === text.length || !mask[index + 1])
        && /[.?!。！？]$/.test(text.slice(start, index).trimEnd())
        && !quotative.test(text.slice(index + 1).trimStart())) end = index + 1;
    if (end !== null && end > start) {
      while (end < text.length && /\s/.test(text[end])) end++;
      chunks.push(text.slice(start, end)); start = end; index = end - 1;
    }
  }
  if (start < text.length) chunks.push(text.slice(start));
  return chunks;
}

function appendCorrectionText(parent, text) {
  const chunks = correctionReadingChunks(text);
  chunks.forEach((chunk, index) => {
    parent.append(chunk);
    if (index < chunks.length - 1 && !/[\r\n]/.test(chunk.match(/\s*$/)?.[0] || '')) {
      parent.append(h('br', { class: 'transcript-reading-break' }));
    }
  });
}

export function renderDocument(doc, store, stage, onSource = () => {}, options = {}) {
  let sentences = array(doc.sentences);
  const missing = !sentences.length;
  if (missing) sentences = array(doc.source_ids).map((id) => {
    const source = store.sources.get(id);
    return { id: `waiting-${id}`, original_text: source?.text || '', text: source?.text || '', role: 'other', role_status: 'draft',
      source_spans: [{ source_id: id }], edits: [], uncertainties: [] };
  });
  const groups = groupsFor(sentences, doc.paragraphs);
  const article = h('article', { class: 'transcript-document', id: `transcript-${stage}-${doc.id}` });
  article.append(h('div', { class: 'transcript-block-heading' },
    h('h3', null, options.title || (doc.questioner ? `${doc.questioner} 질의` : '종료된 발언 블록')),
    correctionDetails(doc, sentences, store, onSource, article.id)));
  const documentWarning = correctionWarnings({ document_status: doc.status });
  if (documentWarning) article.append(documentWarning);
  const summary = renderBlockSummary(doc.summary, sentences, article.id);
  if (summary && options.includeSummary !== false) article.append(summary);
  const proposals = new Map(array(doc.paragraphs).filter((p) => p.body_basis === 'source_group_v2')
    .map((p) => [array(p.sentence_ids).map(String).join('\0'), p]));
  for (const group of groups) {
    const first = group[0], ids = sentenceSourceIds(first), source = store.sources.get(ids[0]);
    const body = h('div', { class: 'transcript-stage-body' });
    const paragraph = h('p', { class: 'transcript-paragraph' });
    group.forEach((sentence, index) => {
      if (index) {
        const previous = String(group[index - 1].text ?? group[index - 1].original_text ?? '');
        paragraph.append(' ');
        if (!/[\r\n]/.test(previous.match(/\s*$/)?.[0] || '')) paragraph.append(h('br', { class: 'transcript-reading-break' }));
      }
      const span = h('span', { class: 'transcript-sentence', id: sentenceAnchor(article.id, sentence.id), tabindex: '-1' });
      appendCorrectionText(span, sentence.text ?? sentence.original_text ?? '');
      paragraph.append(span);
    });
    const proposal = proposals.get(group.map((s) => String(s.id)).join('\0'));
    if (proposal && typeof proposal.text === 'string') {
      setKids(paragraph, ...group.map((sentence) => h('span', { id: sentenceAnchor(article.id, sentence.id), tabindex: '-1' })));
      appendCorrectionText(paragraph, proposal.text);
    }
    const warning = correctionWarnings({
      text_status: proposal?.text_status || (group.some(sentence => sentence.text_status === 'held') ? 'held' : ''),
      text_uncertain: proposal?.text_uncertain === true || group.some(sentence => sentence.text_uncertain === true),
      role_uncertain: proposal?.role_uncertain === true || group.some(sentence => sentence.role_uncertain === true),
    });
    if (warning) body.append(warning);
    body.append(paragraph);
    const speaker = h('div', { class: 'transcript-stage-speaker' }, roleName(first));
    const row = h('div', { class: `transcript-stage-row role-${ROLE_NAMES[first.role] ? first.role : 'other'}` },
      h('time', { class: 'transcript-stage-time' }, timeFor(source)), speaker, body);
    article.append(row);
  }
  return article;
}

function overlaps(a, b) {
  return a.source_id === b.source_id && a.start < b.end && b.start < a.end;
}

function liveLabel(turn) {
  const parts = array(turn?.review_parts);
  const names = new Set(parts.map((part) => `${part.role}\0${part.speaker || ''}`));
  if (parts.length && names.size > 1) return { role: 'unknown', speaker: '여러 화자' };
  const value = parts[0] || { role: turn?.speaker_role, speaker: turn?.speaker };
  const guess = turn?.speaker_guess;
  if (!parts.length && !protectedTurn(turn) && !['manual', 'feed'].includes(turn?.speaker_source)
      && (['unknown', 'other', undefined, ''].includes(value.role)
        || guess?.override_existing === true && turn?.speaker_source === 'inferred')
      && guess && Object.hasOwn(LIVE_ROLES, guess.role)) {
    return { role: guess.role, speaker: guess.speaker || LIVE_ROLES[guess.role], estimated: guess.estimated,
      basis: guess.basis };
  }
  const role = Object.hasOwn(LIVE_ROLES, value.role) ? value.role : 'unknown';
  const name = String(value.speaker || '');
  return { role, speaker: name && !['미상', '화자 확인 필요', 'unknown'].includes(name) ? name
    : role === 'unknown' ? turn?.is_final ? '발언' : '발언 중' : LIVE_ROLES[role] };
}

/** 보정·기존 화자 판본의 정확한 문자 범위만 덮고 나머지 원문을 순서대로 남긴다. */
export function timelineEntries(sources, documents, liveTurns = []) {
  const covered = new Map(), entries = [], byId = new Map(sources.map((source) => [source.id, source]));
  const turns = liveTurns.filter((turn) => validLiveTurn(turn, byId)).map((turn) => effectiveTurn(turn, byId));
  const protectedSpans = turns.filter(protectedTurn).flatMap((turn) => array(turn.source_spans));
  const starts = new Map(), globalChars = [];
  for (const source of sources) {
    starts.set(source.id, globalChars.length);
    for (const char of String(source.text || '')) globalChars.push(char);
    globalChars.push('\n');
  }
  const at = (span) => (starts.get(span.source_id) || 0) + span.start;
  const locate = (doc) => {
    const spans = array(doc.sentences).flatMap((sentence) => array(sentence.source_spans));
    const first = spans.filter((span) => byId.has(span.source_id)).sort((a, b) => at(a) - at(b))[0];
    return first ? { source: byId.get(first.source_id), offset: first.start }
      : { source: sources.find((source) => array(doc.source_ids).includes(source.id)), offset: 0 };
  };
  const consume = (span) => {
    const source = byId.get(span.source_id), size = Array.from(String(source?.text || '')).length;
    if (!source || !Number.isSafeInteger(span.start) || !Number.isSafeInteger(span.end)
        || span.start < 0 || span.end <= span.start || span.end > size) return;
    if (!covered.has(span.source_id)) covered.set(span.source_id, []);
    covered.get(span.source_id).push([span.start, span.end]);
  };
  for (const doc of documents) {
    const spans = array(doc.sentences).flatMap((sentence) => array(sentence.source_spans));
    if (doc.status === 'processing' || !array(doc.sentences).length) {
      entries.push({ kind: 'processing', doc,
        source: sources.find((source) => array(doc.source_ids).includes(source.id)), offset: 0 });
      continue;
    }
    // 운영자가 고친 범위에 남아 있는 AI 문서는 화면에서도 우선하지 않는다.
    if (spans.some((span) => protectedSpans.some((human) => overlaps(span, human)))) continue;
    for (const span of spans) consume(span);
    entries.push({ kind: 'document', doc, ...locate(doc) });
  }
  const unconsumed = (span) => !(covered.get(span.source_id) || [])
    .some(([start, end]) => span.start < end && start < span.end);
  // 전체 발언의 출처 사이에 다른 내용이 없을 때만 기존 읽기 본문을 한 번 표시한다.
  for (const turn of turns) {
    const spans = array(turn.source_spans), first = spans[0], last = spans[spans.length - 1];
    if (!spans.every(unconsumed)) continue;
    if (turns.some((other) => other.id !== turn.id && !(protectedTurn(turn) && !protectedTurn(other))
        && array(other.source_spans).some((a) => spans.some((b) => overlaps(a, b))))) continue;
    const spanText = globalChars.slice(at(first), at(last) + last.end - last.start).join('');
    if (contentPositions(spanText).map((part) => part.char).join('')
        !== contentPositions(turn.original_text).map((part) => part.char).join('')) continue;
    for (const span of spans) consume(span);
    entries.push({ kind: 'live', turn, source: byId.get(first.source_id), offset: first.start });
  }
  for (const source of sources) {
    if (source.replayed) continue;
    const chars = Array.from(String(source.text || '')), ranges = covered.get(source.id) || [];
    const labels = turns.flatMap((turn) => array(turn.source_spans).filter((span) => span.source_id === source.id)
      .map((span) => ({ ...span, turn })));
    const cuts = [...new Set([0, chars.length, ...ranges.flat(), ...labels.flatMap((span) => [span.start, span.end])])].sort((a, b) => a - b);
    const meaningful = new Set(contentPositions(source.text).map((part) => part.index));
    for (let index = 0; index < cuts.length - 1; index++) {
      const start = cuts[index], end = cuts[index + 1];
      if (ranges.some(([a, b]) => a <= start && b >= end)) continue;
      if (!chars.slice(start, end).some((_, offset) => meaningful.has(start + offset))) continue;
      const candidates = labels.filter((span) => span.start <= start && span.end >= end);
      const human = candidates.filter((span) => protectedTurn(span.turn));
      const candidate = human.length === 1 ? human[0] : !human.length && candidates.length === 1 ? candidates[0] : null;
      entries.push({ kind: 'source', source, offset: start, end, text: chars.slice(start, end).join(''),
        live_turn: candidate?.turn || null, speaker_guess: currentSpeakerGuess(source, byId) });
    }
  }
  const order = { processing: 0, document: 1, live: 2, source: 3 };
  entries.sort((a, b) => sourceOrder(a.source || { id: 0 }, b.source || { id: 0 }) || a.offset - b.offset || order[a.kind] - order[b.kind]);
  const grouped = [];
  for (const entry of entries) {
    const previous = grouped[grouped.length - 1];
    if (entry.kind === 'source' && previous?.kind === 'source' && entry.live_turn
        && previous.live_turn?.id === entry.live_turn.id) {
      const from = (starts.get(previous.lastSourceId || previous.source.id) || 0) + previous.end;
      const to = (starts.get(entry.source.id) || 0) + entry.offset;
      if (!contentPositions(globalChars.slice(from, to).join('')).length) {
        previous.text += (previous.source.id === entry.source.id ? globalChars.slice(from, to).join('') : '\n') + entry.text;
        previous.end = entry.end; previous.lastSourceId = entry.source.id;
        continue;
      }
    }
    grouped.push(entry);
  }
  return grouped;
}

function liveRow(text, label, time) {
  const speaker = label.estimated ? h('details', { class: 'transcript-speaker-guess' },
    h('summary', null, `${label.speaker} (추정)`),
    h('p', { class: 'small muted' }, /^[a-z_]+$/.test(label.basis || '')
      ? '현재 발언의 문맥과 진행 정보를 바탕으로 표시한 화자 추정입니다.'
      : String(label.basis || '현재 발언의 문맥을 바탕으로 표시한 화자 추정입니다.'))) : label.speaker;
  return h('div', { class: `transcript-stage-row role-${label.role || 'other'}` },
    h('time', { class: 'transcript-stage-time' }, time),
    h('div', { class: 'transcript-stage-speaker' }, speaker),
    h('div', { class: 'transcript-stage-body transcript-source-body' }, text));
}

function renderPendingSource(entry) {
  const label = entry.live_turn ? liveLabel(entry.live_turn) : entry.speaker_guess
    ? liveLabel({ text: entry.text, original_text: entry.text, speaker_role: 'unknown', speaker_source: 'inferred',
      is_final: entry.source.final, speaker_guess: entry.speaker_guess }) : { role: 'unknown', speaker: '발언 중' };
  return h('article', { class: 'transcript-pending-source', id: `caption-pending-${entry.source.id}-${entry.offset}` },
    liveRow(entry.text, label, timeFor(entry.source)));
}

function renderLiveTurn(entry) {
  const turn = entry.turn, chars = Array.from(turn.text), parts = array(turn.review_parts);
  const time = Number(turn.ts_start) > 0 ? fmt.hms(Number(turn.ts_start)) : timeFor(entry.source);
  let cursor = 0;
  const validParts = parts.length && parts.every((part) => {
    if (!Number.isSafeInteger(part.start) || !Number.isSafeInteger(part.end) || part.start !== cursor
        || part.end <= part.start || part.end > chars.length) return false;
    cursor = part.end; return true;
  }) && cursor === chars.length;
  const article = h('article', { class: 'transcript-live-turn', id: `transcript-live-turn-${turn.id}` });
  if (validParts) for (const part of parts) article.append(liveRow(chars.slice(part.start, part.end).join(''),
    liveLabel({ ...turn, speaker: part.speaker, speaker_role: part.role, review_parts: [] }), time));
  else article.append(liveRow(turn.text, liveLabel({ ...turn, review_parts: [] }), time));
  return article;
}

function streamMarker(source, offset) {
  let start = 0;
  for (const part of array(source?.parts)) {
    const end = start + Array.from(String(part.text || '')).length;
    if (offset < end) return part.label ?? null;
    start = end + 1;
  }
  return null;
}

function streamIdentity(entry, sources) {
  const turn = entry.turn || entry.live_turn;
  const last = array(turn?.source_spans).at(-1);
  const marker = entry.streamLabel !== undefined ? entry.streamLabel : entry.kind === 'source'
    ? streamMarker(entry.source, entry.offset) : last
      ? streamMarker(sources.get(last.source_id), last.end - 1) : null;
  return { turn: turn?.turn_id || '', marker, source: entry.source?.id,
    key: turn?.turn_id ? `turn:${turn.turn_id}` : marker !== null ? `marker:${marker}` : `source:${entry.source?.id}` };
}

function streamLabel(entry) {
  const turn = entry.turn || entry.live_turn;
  return turn ? liveLabel(turn) : entry.speaker_guess
    ? liveLabel({ speaker_role: 'unknown', speaker_source: 'inferred', speaker_guess: entry.speaker_guess })
    : { role: 'unknown', speaker: '발언 중' };
}

/** 현재 턴만 고정 실황 창으로 분리한다. 저장·보정 범위와 원자막 판본은 변경하지 않는다. */
export function splitStreamTimeline(timeline, sources = new Map()) {
  const entries = timeline.flatMap((entry) => {
    if (entry.kind !== 'source' || entry.lastSourceId || array(entry.source?.parts).length < 2
        || entry.source.parts.map((part) => String(part.text || '')).join('\n') !== entry.source.text) return [entry];
    const chars = Array.from(String(entry.source.text || '')), parts = [];
    let cursor = 0;
    for (const part of entry.source.parts) {
      const end = cursor + Array.from(String(part.text || '')).length;
      const from = Math.max(cursor, entry.offset), to = Math.min(end, entry.end);
      if (to > from) parts.push({ ...entry, offset: from, end: to, text: chars.slice(from, to).join(''), streamLabel: part.label ?? null });
      cursor = end + 1;
    }
    return parts.length ? parts : [entry];
  });
  const last = entries.at(-1);
  if (!last || !['live', 'source'].includes(last.kind)) return { history: entries, current: null };
  if (array((last.turn || last.live_turn)?.review_parts).length > 1) return { history: entries, current: null };
  let start = entries.length - 1;
  while (start > 0 && ['live', 'source'].includes(entries[start - 1].kind)) {
    const previous = entries[start - 1], next = entries[start];
    const left = streamIdentity(previous, sources), right = streamIdentity(next, sources);
    const leftTurn = previous.turn || previous.live_turn, rightTurn = next.turn || next.live_turn;
    const human = leftTurn && protectedTurn(leftTurn) || rightTurn && protectedTurn(rightTurn);
    const leftLabel = streamLabel(previous), rightLabel = streamLabel(next);
    if (human && (leftLabel.role !== rightLabel.role || leftLabel.speaker !== rightLabel.speaker)) break;
    if (!left.turn && right.turn && next.kind === 'live'
        && !['limit_chars', 'limit_time'].includes(rightTurn.boundary_reason)
        && !array(rightTurn.source_spans).some((span) => span.source_id === previous.source.id)) break;
    const same = left.turn && right.turn ? left.turn === right.turn
      : left.marker !== null && right.marker !== null ? left.marker === right.marker : left.source === right.source;
    if (!same || entries[start].kind === 'source' && /^\s*-\s*/.test(entries[start].text)
        && (entries[start - 1].source.id !== entries[start].source.id || entries[start - 1].offset !== entries[start].offset)
        || array((entries[start - 1].turn || entries[start - 1].live_turn)?.review_parts).length > 1) break;
    start--;
  }
  const current = entries.slice(start), first = current[0];
  let label = streamLabel(last);
  // 원자막 후보가 늦게 붙어도 같은 표식의 이미 표시한 화자를 이어 쓴다.
  if (label.role === 'unknown') {
    const known = [...current].reverse().find((entry) => entry.turn || entry.live_turn || entry.speaker_guess);
    if (known) label = streamLabel(known);
  }
  return { history: entries.slice(0, start), current: { entries: current,
    text: current.map((entry) => entry.kind === 'live' ? entry.turn.text : entry.text).join('\n'),
    label, time: first.turn?.ts_start ? fmt.hms(first.turn.ts_start) : timeFor(first.source),
    key: streamIdentity(first, sources).key } };
}

/** 표시가 끝난 원문 범위를 기억해 늦은 화자 투영이 현재 행에 과거 발언을 끌어오지 않게 한다. */
export class StreamTimeline {
  constructor() { this.frontier = null; this.turnId = ''; }
  point(source, offset = 0) { return { id: source.id, first_rx: source.first_rx || 0, offset }; }
  compare(a, b) { return sourceOrder(a, b) || a.offset - b.offset; }
  span(entry, sources) {
    if (entry.kind === 'live') {
      const spans = array(entry.turn.source_spans), first = spans[0], last = spans.at(-1);
      return [this.point(sources.get(first.source_id), first.start), this.point(sources.get(last.source_id), last.end)];
    }
    return [this.point(entry.source, entry.offset), this.point(sources.get(entry.lastSourceId) || entry.source, entry.end)];
  }
  remember(point, sources) {
    const source = sources.get(point.id), chars = Array.from(String(source?.text || ''));
    const parts = array(source?.parts), offsets = []; let cursor = 0;
    for (const part of parts) { offsets.push(cursor); cursor += Array.from(String(part.text || '')).length + 1; }
    const labels = parts.map((part) => part.label ?? null);
    const partIndex = parts.map((part) => String(part.text || '')).join('\n') === chars.join('')
      ? offsets.indexOf(point.offset) : -1;
    const dashes = [...chars.join('').matchAll(/^[ \t]*-\s+/gm)]
      .map((match) => Array.from(chars.join('').slice(0, match.index)).length);
    this.frontier = { ...point, prefix: chars.slice(0, point.offset).join(''),
      head: chars.slice(point.offset, point.offset + 24).join(''), text: chars.join(''),
      partIndex, labels, dashIndex: dashes.indexOf(point.offset) };
  }
  resolve(sources) {
    const source = sources.get(this.frontier.id);
    if (!source) return;
    const chars = Array.from(String(source.text || '')), at = this.frontier.offset;
    if (chars.slice(0, at).join('') === this.frontier.prefix) { this.remember(this.frontier, sources); return; }
    const parts = array(source.parts), labels = parts.map((part) => part.label ?? null);
    if (this.frontier.partIndex > 0 && JSON.stringify(labels) === JSON.stringify(this.frontier.labels)
        && parts.map((part) => String(part.text || '')).join('\n') === chars.join('')) {
      const offset = parts.slice(0, this.frontier.partIndex).reduce((sum, part) => sum + Array.from(String(part.text || '')).length + 1, 0);
      this.remember(this.point(source, offset), sources); return;
    }
    // 앞 부분의 분할 길이만 달라졌다면 현재 문구의 유일한 시작점으로 다시 맞춘다.
    const head = this.frontier.head, text = chars.join(''), index = head ? text.indexOf(head) : -1;
    if (index >= 0 && text.indexOf(head, index + head.length) < 0) {
      this.remember(this.point(source, Array.from(text.slice(0, index)).length), sources);
      return;
    }
    const dashes = [...text.matchAll(/^[ \t]*-\s+/gm)];
    if (this.frontier.dashIndex >= 0 && dashes[this.frontier.dashIndex]) {
      this.remember(this.point(source, Array.from(text.slice(0, dashes[this.frontier.dashIndex].index)).length), sources);
      return;
    }
    // 경계를 가로질러 원문이 다시 쓰이면 옛 좌표로 새 현재 문구를 잘라 버리지 않는다.
    const old = Array.from(this.frontier.text); let common = 0;
    while (common < at && old[common] === chars[common] && common < chars.length) common++;
    this.remember(this.point(source, common), sources);
  }
  pieces(entry, sources) {
    if (entry.kind === 'source' && !entry.lastSourceId) return [entry];
    const spans = entry.kind === 'live' ? array(entry.turn.source_spans) : [...sources.values()]
      .sort(sourceOrder).filter((source) => sourceOrder(source, entry.source) >= 0
        && sourceOrder(source, sources.get(entry.lastSourceId)) <= 0).map((source) => ({ source_id: source.id,
          start: source.id === entry.source.id ? entry.offset : 0,
          end: source.id === entry.lastSourceId ? entry.end : Array.from(String(source.text || '')).length }));
    return spans.map((span) => {
      const source = sources.get(span.source_id);
      return { kind: 'source', source, offset: span.start, end: span.end,
        text: Array.from(String(source.text || '')).slice(span.start, span.end).join(''),
        live_turn: entry.turn || entry.live_turn || null, speaker_guess: currentSpeakerGuess(source, sources) };
    });
  }
  split(timeline, sources = new Map()) {
    const proposed = splitStreamTimeline(timeline, sources);
    if (!proposed.current) return proposed;
    const candidate = proposed.current.entries[0], start = this.span(candidate, sources)[0];
    if (!this.frontier) this.remember(start, sources);
    this.resolve(sources);
    if (this.compare(start, this.frontier) > 0) {
      const previous = proposed.history.at(-1), left = previous && ['live', 'source'].includes(previous.kind)
        ? streamIdentity(previous, sources) : null, right = streamIdentity(candidate, sources);
      const turn = candidate.turn || candidate.live_turn;
      const explicit = candidate.kind === 'source' && /^\s*-\s*/.test(candidate.text);
      const changedTurn = right.turn && (left?.turn && left.turn !== right.turn || this.turnId && right.turn !== this.turnId)
        && !['limit_chars', 'limit_time'].includes(turn?.boundary_reason);
      const changedMarker = left?.marker !== null && left?.marker !== undefined
        && right.marker !== null && left.marker !== right.marker;
      const manual = turn && protectedTurn(turn) || previous && protectedTurn(previous.turn || previous.live_turn || {});
      if (!left || explicit || changedTurn || changedMarker || manual) this.remember(start, sources);
    }
    const history = [], current = [];
    const entries = [...proposed.history, ...proposed.current.entries];
    for (const entry of entries) {
      if (!['live', 'source'].includes(entry.kind)) { history.push(entry); continue; }
      const [from, to] = this.span(entry, sources);
      if (this.compare(to, this.frontier) <= 0) { history.push(entry); continue; }
      if (this.compare(from, this.frontier) >= 0) { current.push(entry); continue; }
      if (entry.kind === 'live' && protectedTurn(entry.turn)) { history.push(entry); continue; }
      for (const piece of this.pieces(entry, sources)) {
        const [a, b] = this.span(piece, sources);
        if (this.compare(b, this.frontier) <= 0) history.push(piece);
        else if (this.compare(a, this.frontier) >= 0) current.push(piece);
        else {
          const cut = this.frontier.offset, chars = Array.from(String(piece.source.text || ''));
          history.push({ ...piece, end: cut, text: chars.slice(piece.offset, cut).join('') });
          current.push({ ...piece, offset: cut, text: chars.slice(cut, piece.end).join('') });
        }
      }
    }
    if (!current.length) return { history, current: null };
    const first = current[0], last = current.at(-1);
    let label = streamLabel(last);
    if (label.role === 'unknown') {
      const known = [...current].reverse().find((entry) => entry.turn || entry.live_turn || entry.speaker_guess);
      if (known) label = streamLabel(known);
    }
    const knownTurn = [...current].reverse().map((entry) => entry.turn || entry.live_turn).find((turn) => turn?.turn_id);
    if (knownTurn) this.turnId = knownTurn.turn_id;
    return { history, current: { entries: current,
      text: current.map((entry) => entry.kind === 'live' ? entry.turn.text : entry.text).join('\n'), label,
      time: first.turn?.ts_start ? fmt.hms(first.turn.ts_start) : timeFor(first.source),
      key: `range:${this.frontier.id}:${this.frontier.offset}` } };
  }
}

function renderProcessingDocument(doc, store, onSource) {
  const article = h('article', { class: 'transcript-document transcript-processing', id: `transcript-processing-${doc.id}` });
  const detail = correctionDetails(doc, [], store, onSource, article.id);
  const content = detail.children[1];
  for (const id of array(doc.source_ids)) {
    const source = store.sources.get(id);
    if (source) content.append(sourceDetails(source));
  }
  article.append(h('div', { class: 'transcript-block-heading' },
    h('h3', null, doc.questioner ? `${doc.questioner} 질의` : '종료된 발언 블록'), detail));
  return article;
}

function humanSummary(block) {
  let summary = block.summary;
  if (typeof summary === 'string') { try { summary = JSON.parse(summary); } catch { return null; } }
  return summary && (block.review_status === 'reviewed' || summary.edited_by) ? summary : null;
}

/** 저장된 공통 표시 카드는 전체 발언과 위원별 내역이 같은 본문을 사용한다. */
export function renderStoredBlock(projection, store, prefix, onSource = () => {}, options = {}) {
  const doc = projection.document || {}, article = h('article', {
    class: 'transcript-document', id: `transcript-${prefix}-${projection.id}` });
  article.append(h('div', { class: 'transcript-block-heading' },
    h('h3', null, options.title || (doc.questioner ? `${doc.questioner} 질의` : '종료된 발언 블록')),
    correctionDetails(doc, array(doc.sentences), store, onSource, article.id)));
  const documentWarning = correctionWarnings({ document_status: doc.status });
  if (documentWarning) article.append(documentWarning);
  if (options.includeSummary !== false) {
    const summary = renderBlockSummary(projection.summary, array(doc.sentences), article.id);
    if (summary) article.append(summary);
  }
  for (const card of array(projection.cards)) {
    const paragraph = h('p', { class: 'transcript-paragraph' });
    appendCorrectionText(paragraph, card.text);
    const body = h('div', { class: 'transcript-stage-body' });
    const warning = correctionWarnings({ ...card, document_status: undefined });
    if (warning) body.append(warning);
    body.append(paragraph);
    article.append(h('div', { class: `transcript-stage-row role-${card.speaker_role || 'other'}`,
      dataset: { displayKey: card.key } },
    h('time', { class: 'transcript-stage-time' }, Number.isFinite(card.ts_start) ? fmt.hms(card.ts_start) : ''),
    h('div', { class: 'transcript-stage-speaker' }, card.acoustic_overlap
      ? `${card.speaker ? card.speaker + ' · ' : ''}겹친 발언 · 화자 확인 필요`
      : card.speaker || card.acoustic_speaker_label || LIVE_ROLES[card.speaker_role] || '발언'),
    body));
  }
  return article;
}

function renderStoredRow(row) {
  return liveRow(row.text, { role: row.speaker_role, speaker: row.acoustic_overlap
    ? `${row.speaker ? row.speaker + ' · ' : ''}겹친 발언 · 화자 확인 필요`
    : row.speaker || row.acoustic_speaker_label || LIVE_ROLES[row.speaker_role] || '발언',
    estimated: row.estimated === true },
    Number.isFinite(row.ts_start) ? fmt.hms(row.ts_start) : '');
}

function renderStoredMember(block, store, onSource) {
  const section = h('div', { class: 'transcript-member-content' });
  const projection = store.displayBlocks.get(block.id), saved = humanSummary(block);
  const summaryBox = h('section', { class: 'transcript-member-summary' }, h('h3', null, '발언내용 요약'));
  const summary = projection ? renderBlockSummary(projection.summary, array(projection.document?.sentences),
    `transcript-member-${block.id}-${projection.id}`) : saved && renderBlockSummary(saved, [], `transcript-member-${block.id}-saved`);
  summaryBox.append(summary || h('p', { class: 'small muted' }, projection
    ? '이 보정 판본의 요약이 아직 없습니다.' : '저장된 표시 판본을 기다리고 있습니다.'));
  section.append(summaryBox);
  if (projection) section.append(renderStoredBlock(projection, store, `member-${block.id}`, onSource,
    { title: '최종보정원문', includeSummary: false }));
  else {
    const body = h('section', { class: 'transcript-member-live' }, h('h3', null, saved ? '최종보정원문' : '실황 추정 원문'));
    for (const row of store.displayRowsForBlock(block.id).filter((row) => row.stage !== 3)) body.append(renderStoredRow(row));
    if (body.children.length === 1) body.append(h('p', { class: 'small muted' }, '저장된 표시 내용을 기다리고 있습니다.'));
    section.append(body);
  }
  return section;
}

/** 위원별 내역은 유효한 최종 문서가 있으면 그 판본의 본문과 요약만 표시한다. */
export function renderMemberContent(block, store, fallbackRows = [], onSource = () => {}, { recording = false } = {}) {
  if (store.canonical) return renderStoredMember(block, store, onSource);
  const section = h('div', { class: 'transcript-member-content' }), saved = humanSummary(block);
  const turns = store.orderedLiveTurns().filter((turn) => turn.block_id === block.id);
  const humanSpans = store.orderedLiveTurns().filter(protectedTurn).flatMap((turn) => array(turn.source_spans));
  const candidates = store.orderedDocuments().filter((doc) => doc.block_id === block.id && !block.is_open && !saved
    && ['draft', 'partial', 'ready'].includes(doc.status) && array(doc.sentences).length
    && (!block.questioner || doc.questioner === block.questioner)
    && !array(doc.sentences).flatMap((sentence) => array(sentence.source_spans))
      .some((span) => humanSpans.some((human) => overlaps(span, human))))
    .sort((a, b) => Number(b.updated_at || b.created_at || 0) - Number(a.updated_at || a.created_at || 0) || b.id - a.id);
  const final = candidates[0];
  const summaryBox = h('section', { class: 'transcript-member-summary' }, h('h3', null, '발언내용 요약'));
  if (final) {
    const summary = renderBlockSummary(final.summary, final.sentences, `transcript-member-${block.id}-${final.id}`);
    summaryBox.append(summary || h('p', { class: 'small muted' }, '이 보정 판본의 요약이 아직 없습니다.'));
    section.append(summaryBox, renderDocument(final, store, `member-${block.id}`, onSource,
      { title: '최종보정원문', includeSummary: false }));
    return section;
  }
  if (saved) {
    const title = saved.title || saved.one_line;
    if (title) summaryBox.append(h('p', null, String(title)));
    const points = [...array(saved.bullets), ...array(saved.question_points), ...array(saved.answer_points),
      ...array(saved.issues), ...array(saved.data_requests), ...array(saved.commitments)];
    if (points.length) summaryBox.append(h('ul', null, ...points.map((item) => h('li', null, summaryText(item)))));
    summaryBox.append(h('p', { class: 'small muted' }, '사람이 수정·검수한 요약입니다.'));
  } else summaryBox.append(h('p', { class: 'small muted' }, block.is_open
    ? '질의 종료 후 최종 보정 내용으로 요약합니다.' : '블록 보정과 요약을 기다리고 있습니다.'));
  section.append(summaryBox);
  const processing = store.orderedDocuments().find((doc) => doc.block_id === block.id && doc.status === 'processing');
  if (processing && !saved) section.append(renderProcessingDocument(processing, store, onSource));
  const body = h('section', { class: 'transcript-member-live' }, h('h3', null, saved ? '최종보정원문' : recording ? '원전사' : '실황 추정 원문'),
    h('p', { class: 'small muted' }, saved ? '사람이 수정·검수한 발언 기록을 우선 표시합니다.'
      : recording ? processing ? 'AI 보정 중 · 녹음 인식 결과' : '최종 보정 전 · 녹음 인식 결과에는 오류가 있을 수 있습니다.'
      : block.is_open ? '질의 진행 중 · 실시간 화자 추정' : processing ? 'AI 보정 중 · 실시간 기록' : '최종 보정 전 · 실시간 기록'));
  const rows = saved && fallbackRows.length ? fallbackRows : turns.length ? turns : fallbackRows;
  for (const row of rows) {
    const turn = { ...row, original_text: row.original_text ?? row.text ?? '', text: String(row.text || '') };
    body.append(renderLiveTurn({ turn: effectiveTurn(turn, store.sources), source: { first_rx: row.ts_start } }));
  }
  if (!rows.length) body.append(h('p', { class: 'small muted' }, '이 질의의 발언 기록을 기다리고 있습니다.'));
  section.append(body);
  return section;
}

export class TranscriptStages {
  constructor(root) {
    this.root = root;
    this.store = new StageStore();
    this.stage = 'corrected';
    this.legacy = true;
    this.session = null;
    this.knownSources = false;
    this.chosen = false;
    this.loaded = false;
    this.loading = false;
    this.refreshAgain = false;
    this.timer = 0;
    this.liveRefreshTimer = 0;
    this.renderTimer = 0;
    this.abort = null;
    this.generation = 0;
    this.stopped = false;
    this.fingerprint = '';
    this.streamTimeline = new StreamTimeline();
    this.sourceNodes = new Map();
    this.documentNodes = new Map();
    this.memberPanels = new Map();
    this.blockReads = new Map();
    this.readQueue = Promise.resolve();
    this.readEpoch = 0;
    this.legacyFrame = null;
    this.legacyCache = new Map();
    this.legacyRowOwners = new Map();
    this.legacyPendingBlocks = new Set();
    this.legacyPendingInvalidated = new Set();
    this.legacyPendingUtterances = new Set();
    this.rosterSignature = null;
    this.mainTab = 'feed';
    this.viewportFrame = null;
    this.readPositions = new Map();
    this.get = (id) => document.getElementById(id);
    this.legacyEl = this.get('transcript-legacy');
    this.currentTime = h('time', { class: 'transcript-stage-time' });
    this.currentSpeaker = h('div', { class: 'transcript-stage-speaker' });
    this.currentText = h('span', { class: 'transcript-current-text' });
    this.currentBody = h('div', { id: 'transcript-current-body', class: 'transcript-stage-body transcript-source-body' }, this.currentText);
    this.currentRow = h('div', { class: 'transcript-stage-row' }, this.currentTime, this.currentSpeaker, this.currentBody);
    this.currentEl = h('article', { id: 'transcript-current', class: 'transcript-live-turn transcript-current', 'aria-label': '현재 발언 실황' }, this.currentRow);
    this.currentEl.hidden = true;
    this.bind();
  }
  bind() {
    for (const stage of STAGES) this.get(`transcript-tab-${stage}`).addEventListener('click', () => this.select(stage));
    this.root.querySelector('[role="tablist"]').addEventListener('keydown', (event) => {
      const index = STAGES.indexOf(this.stage);
      const target = { ArrowRight: (index + 1) % STAGES.length, ArrowLeft: (index + STAGES.length - 1) % STAGES.length, Home: 0, End: STAGES.length - 1 }[event.key];
      if (target === undefined) return;
      event.preventDefault();
      this.select(STAGES[target]);
      this.get(`transcript-tab-${STAGES[target]}`).focus();
    });
    this.get('transcript-legacy-toggle').addEventListener('click', () => { this.chosen = true; this.legacy = !this.legacy; this.renderMode(); });
    this.get('transcript-stage-older').addEventListener('click', () => {
      this.get('transcript-stage-follow').checked = false;
      this.refresh(true);
    });
    this.get('transcript-stage-follow').addEventListener('change', () => {
      if (this.get('transcript-stage-follow').checked) this.readPositions.clear();
      else this.rememberReading();
      this.fitViewport(); this.queueViewport();
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { this.cancel(); return; }
      this.stopped = false;
      this.refresh();
    });
    window.addEventListener('pagehide', () => this.cancel());
    window.addEventListener('pageshow', () => { this.stopped = false; this.refresh(); });
    const layoutChanged = () => { this.fitViewport(); this.queueViewport(); };
    window.addEventListener('resize', layoutChanged);
    window.addEventListener('scroll', layoutChanged, { passive: true });
    for (const stage of STAGES) this.bindReadingScroll(this.get(`transcript-panel-${stage}`),
      () => this.stage === stage, layoutChanged);
    if (typeof ResizeObserver === 'function') {
      this.layoutObserver = new ResizeObserver(layoutChanged);
      // 보정 교체 뒤 늦은 높이 변화와 자막 위 요약·진행 카드의 배치 변화도 감시한다.
      const targets = new Set(STAGES.flatMap((stage) => [this.get(`transcript-panel-${stage}`), this.get(`transcript-list-${stage}`)]));
      for (const node of [this.currentEl, this.currentBody, this.currentText]) targets.add(node);
      for (let node = this.root; node && node !== document.body; node = node.parentNode) targets.add(node);
      const issues = this.get('issues'); if (issues) targets.add(issues);
      for (const node of targets) this.layoutObserver.observe(node);
    }
  }
  bindReadingScroll(area, active, layoutChanged) {
    let touchStart = null, pointerReading = false, previousTop = area.scrollTop || 0;
    const pause = () => {
      if (!this.stopped && this.mainTab === 'feed' && this.isActive() && active()) {
        this.get('transcript-stage-follow').checked = false;
        this.rememberReading();
      }
    };
    // 자동 배치 변화와 구분해 사용자의 위쪽 읽기 의도만 따라가기를 해제한다.
    area.addEventListener('wheel', (event) => {
      if (!event.ctrlKey && event.deltaY < 0) pause();
    }, { passive: true });
    area.addEventListener('touchstart', (event) => {
      touchStart = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null;
    }, { passive: true });
    area.addEventListener('touchmove', (event) => {
      const point = event.touches.length === 1 ? event.touches[0] : null;
      if (point && touchStart && point.clientY - touchStart.y > 3
          && point.clientY - touchStart.y > Math.abs(point.clientX - touchStart.x)) pause();
      if (!point) touchStart = null;
    }, { passive: true });
    for (const name of ['touchend', 'touchcancel']) area.addEventListener(name, () => { touchStart = null; }, { passive: true });
    area.addEventListener('keydown', (event) => {
      if (event.defaultPrevented || event.target?.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return;
      if (event.key === ' ' && event.target?.closest?.('button, summary')) return;
      if (['ArrowUp', 'PageUp', 'Home'].includes(event.key) || event.key === ' ' && event.shiftKey) pause();
    });
    area.addEventListener('pointerdown', (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      pointerReading = event.target === area; previousTop = area.scrollTop || 0;
    }, { passive: true });
    for (const name of ['pointerup', 'pointercancel']) document.addEventListener(name, () => { pointerReading = false; }, { passive: true });
    area.addEventListener('scroll', () => {
      if (pointerReading && area.scrollTop < previousTop - 1) pause();
      if (active() && this.get('transcript-stage-follow').checked) layoutChanged();
      else if (active()) this.rememberReading();
      previousTop = area.scrollTop || 0;
    }, { passive: true });
  }
  setSession(session) {
    const previous = this.session;
    this.session = session;
    const eligible = supportsStages(session);
    this.root.hidden = !eligible;
    if (!eligible) { this.cancel(); this.legacyEl.hidden = false; document.body.classList.remove('show-transcript-stages', 'has-transcript-stages'); return; }
    this.stopped = false;
    this.get('transcript-tab-live').textContent = session.source_type === 'file' ? '원전사' : '원자막';
    this.renderMode();
    if (!previous || previous.source_type !== session.source_type || !this.loaded || previous.status !== session.status) this.refresh();
  }
  isActive() { return !this.root.hidden && !this.legacy; }
  notifyLegacy(change = {}) {
    const stored = this.store.takeLegacyChanges();
    const blocks = new Set([...stored.blockIds, ...array(change.blockIds)]);
    const invalidated = new Set([...stored.invalidatedBlockIds, ...array(change.invalidatedBlockIds)]);
    for (const id of [...stored.utteranceIds, ...array(change.utteranceIds)]) {
      if (this.store.canonical) this.legacyPendingUtterances.add(id);
      for (const blockId of this.legacyRowOwners.get(id) || []) blocks.add(blockId);
    }
    for (const id of blocks) {
      const cached = this.legacyCache.get(id);
      if (cached) cached.dirty = true;
      if (this.store.canonical || cached || this.store.latestDocument(id)) this.legacyPendingBlocks.add(id);
    }
    for (const id of invalidated) {
      this.legacyPendingInvalidated.add(id); this.legacyPendingBlocks.add(id);
    }
    // 새 실황 원조각은 과거 보정 문서에 의존하지 않는다. 변경된 종료 블록만 별도로 넘긴다.
    if ((!this.store.canonical && !this.legacyPendingBlocks.size && !this.legacyPendingUtterances.size) || this.legacyFrame !== null) return;
    this.legacyFrame = requestAnimationFrame(() => {
      this.legacyFrame = null;
      const update = { blockIds: [...this.legacyPendingBlocks], invalidatedBlockIds: [...this.legacyPendingInvalidated],
        ...(this.store.canonical ? { utteranceIds: [...this.legacyPendingUtterances] } : {}) };
      this.legacyPendingBlocks.clear(); this.legacyPendingInvalidated.clear();
      this.legacyPendingUtterances.clear();
      window.gukgamTranscriptLegacyUpdated?.(update);
    });
  }
  closedBlockIds() {
    if (this.store.canonical) return [...this.store.displayBlocks.keys()];
    // 이미 읽은 현재 보정만 알린다. 원발언이 없는 파생 표시 그룹을 찾기 위한 목록이다.
    return [...this.store.documentsByBlock.keys()].filter((id) => {
      const doc = this.store.latestDocument(id);
      return doc && this.store.documentCurrent(doc);
    });
  }
  closedBlock(block, rows = []) {
    if (this.store.canonical) return this.store.displayBlocks.get(block?.id) || null;
    const rowMap = rows instanceof Map ? rows : new Map(rows.map((row) => [row.id, row]));
    const doc = this.store.latestDocument(block.id), stamp = JSON.stringify(block);
    const previous = this.legacyCache.get(block.id);
    if (previous && !previous.dirty && previous.doc === doc && previous.stamp === stamp
        && [...previous.rows].every(([id, row]) => rowMap.get(id) === row)) return previous.projection;
    const documentSources = new Set(array(doc?.source_ids));
    const candidates = doc ? this.store.turnsForSources(documentSources) : [];
    const turns = candidates.filter((turn) => rowMap.has(turn.id));
    const ids = new Set(candidates.map((turn) => turn.id));
    for (const row of rowMap.values()) if (row.block_id === block.id) ids.add(row.id);
    for (const proof of array(doc?.source_supersessions)) {
      ids.add(proof.utterance_id);
      for (const row of array(proof.replacement_utterances)) ids.add(row.utterance_id);
    }
    const relevant = new Map([...ids].map((id) => [id, rowMap.get(id)]));
    const sourceIds = new Set([...array(doc?.source_ids), ...revisionsFor(doc || {}).keys(),
      ...turns.flatMap((turn) => array(turn.source_spans).map((span) => span.source_id))]);
    const sources = new Map([...sourceIds].map((id) => [id, this.store.sources.get(id)]).filter(([, source]) => source));
    const projection = doc && this.store.documentCurrent(doc)
      ? closedBlockProjection(block, doc, sources, turns, new Map([...relevant].filter(([, row]) => row))) : null;
    for (const id of previous?.rows.keys() || []) {
      const owners = this.legacyRowOwners.get(id); owners?.delete(block.id);
      if (!owners?.size) this.legacyRowOwners.delete(id);
    }
    for (const id of relevant.keys()) {
      if (!this.legacyRowOwners.has(id)) this.legacyRowOwners.set(id, new Set());
      this.legacyRowOwners.get(id).add(block.id);
    }
    this.legacyCache.set(block.id, { doc, stamp, rows: relevant, projection, dirty: false });
    return projection;
  }
  correctionEvidence(projection) {
    // 배지 안에서만 원수신과 수정 근거를 펼친다. 표시 조회는 모델을 실행하지 않는다.
    const doc = projection.document;
    const detail = correctionDetails(doc, array(doc.sentences), this.store, (id) => {
      const source = this.store.sources.get(id);
      if (!source) return;
      const content = h('pre', { class: 'transcript-original' }, String(source.text || ''));
      detail.append(content);
    }, `legacy-correction-${doc.block_id}-${doc.id}`);
    detail.open = true;
    return detail;
  }
  readingChunks(text) { return correctionReadingChunks(text); }
  usesStoredDisplay() { return this.store.canonical; }
  displayUtterance(id) {
    if (!this.store.canonical || this.store.displayAwaiting || this.store.displayPendingUtterances.has(id)) return null;
    const rows = this.store.displayRowsForUtterance(id);
    return rows.length || this.store.displayCoveredUtterances.has(id) ? rows : null;
  }
  displayOwners(id) { return [...(this.store.displayBlocksByUtterance.get(id) || [])]; }
  displayLooseRows() { return this.store.canonical ? this.store.displayLooseRows : []; }
  displaySummary(block) {
    if (!this.store.canonical) return null;
    const human = humanSummary(block || {});
    if (human) return human;
    if (this.store.displayAwaiting || this.store.displayPendingBlocks.has(block?.id)) return null;
    return this.store.displayBlocks.get(block?.id)?.summary || this.store.displaySummaries.get(block?.id) || null;
  }
  acceptsDisplayPayload(payload) { return this.store.acceptsDisplay(payload); }
  acceptsDisplayUtterance(payload, row) {
    return !this.store.canonical || !validId(payload?.display_generation)
      || this.store.displayInputCurrent(payload.display_generation, { blockId: row.block_id, utteranceIds: [row.id] });
  }
  acceptsDisplayBlock(payload, block) {
    return !this.store.canonical || !validId(payload?.display_generation)
      || this.store.displayInputCurrent(payload.display_generation, { blockId: block.id });
  }
  ingestDisplay(payload) {
    if (!Object.hasOwn(payload || {}, 'display_blocks') || !this.store.merge({ ...payload, version: 3 }, false, false)) return false;
    this.loaded = true; this.activateSources(); this.notifyLegacy(); this.renderMemberPanels(); return true;
  }
  displayPending(error) {
    if (error?.data?.display_state !== 'updating') return false;
    const generation = error.data.display_generation;
    const scoped = this.store.canonical && validId(generation)
      && [this.store.displayPendingBlocks, this.store.displayPendingUtterances, this.store.displayPendingSources]
        .some((index) => [...index.values()].some((threshold) => threshold >= generation - 1));
    if (!scoped && validId(generation)) this.store.displayFloor = Math.max(this.store.displayFloor, generation);
    if (!this.store.canonical) {
      for (const id of [...this.store.documents.keys()]) this.store.deleteDocument(id);
      for (const id of [...this.store.liveTurns.keys()]) this.store.deleteLiveTurn(id);
      this.store.canonical = true;
    }
    if (!scoped) {
      this.store.displayAwaiting = true; this.store.retireDisplay({ all: true, generation });
    }
    this.notifyLegacy({ blockIds: scoped ? [] : [...this.store.blocks.keys(), ...this.legacyCache.keys()] });
    this.render(); return true;
  }
  rememberReading() {
    if (this.stopped || this.mainTab !== 'feed' || !this.isActive() || this.get('transcript-stage-follow').checked) return;
    const panel = this.get(`transcript-panel-${this.stage}`), list = this.get(`transcript-list-${this.stage}`);
    if (panel.hidden) return;
    const snapshot = viewportSnapshot(panel, list);
    this.readPositions.set(this.stage, { id: snapshot.id,
      offset: snapshot.anchorTop - panel.getBoundingClientRect().top });
  }
  restoreReading(panel) {
    const position = this.readPositions.get(this.stage);
    const anchor = position?.id && descendantById(panel, position.id);
    if (!anchor || !Number.isFinite(position.offset)) return;
    const delta = anchor.getBoundingClientRect().top - panel.getBoundingClientRect().top - position.offset;
    if (Math.abs(delta) > 0.5) panel.scrollTop = Number(panel.scrollTop || 0) + delta;
  }
  fitViewport(follow = true) {
    if (this.stopped || this.mainTab !== 'feed' || !this.isActive() || this.get('tab-feed')?.hidden) return;
    const panel = this.get(`transcript-panel-${this.stage}`);
    if (panel.hidden) return;
    // 상단 카드·도구 높이가 달라도 최신 꼬리가 실제 화면 하단 안에 들어오게 한다.
    const remaining = Math.max(0, Math.floor(window.innerHeight - Math.max(0, panel.getBoundingClientRect().top) - 16));
    const maxHeight = `${remaining}px`, minHeight = `${Math.min(220, remaining)}px`;
    if (panel.style.maxHeight !== maxHeight) panel.style.maxHeight = maxHeight;
    if (panel.style.minHeight !== minHeight) panel.style.minHeight = minHeight;
    if (follow && this.get('transcript-stage-follow').checked) pinLatest(panel);
    else if (follow && !this.get('transcript-stage-follow').checked) this.restoreReading(panel);
  }
  queueViewport() {
    if (this.stopped || this.viewportFrame !== null) return;
    this.viewportFrame = requestAnimationFrame(() => {
      this.viewportFrame = null;
      // 예약 당시의 설정이 아니라 화면 배치가 끝난 현재의 ON/OFF·탭을 따른다.
      this.fitViewport();
    });
  }
  followLatest() {
    this.get('transcript-stage-follow').checked = true;
    this.readPositions.clear();
    this.fitViewport(); this.queueViewport();
  }
  preserveMemberFocus(container) {
    const snapshot = focusSnapshot(container);
    return () => restoreFocus(snapshot);
  }
  setMainTab(name) {
    this.mainTab = name;
    if (name === 'feed') {
      this.render(true); this.refresh();
      requestAnimationFrame(() => this.fitViewport());
    }
    else if (name === 'members') { this.renderMemberPanels(); this.refreshOpenMembers(); }
    else { clearTimeout(this.timer); clearTimeout(this.liveRefreshTimer); this.liveRefreshTimer = 0; }
  }
  readSnapshot(path, signal) {
    const request = this.readQueue.catch(() => {}).then(() => {
      if (signal.aborted) { const error = new Error('조회 취소'); error.name = 'AbortError'; throw error; }
      return api(path, { signal }).catch((error) => { this.displayPending(error); throw error; });
    });
    this.readQueue = request.catch(() => {});
    return request;
  }
  mergeUtteranceClosure(payload) {
    if (!this.store.acceptsDisplay(payload)) return;
    // 보정 저장소가 거절할 과거 응답은 원행 표시에도 먼저 반영하지 않는다.
    if (!Object.hasOwn(payload || {}, 'display_blocks') && Number.isSafeInteger(payload?.snapshot_at) && payload.snapshot_at > 0
        && payload.snapshot_at < this.store.snapshotAt) return;
    if (!Array.isArray(payload?.utterances) || typeof window.gukgamTranscriptMergeSnapshot !== 'function') return;
    return window.gukgamTranscriptMergeSnapshot({ utterances: payload.utterances,
      ...(Object.hasOwn(payload, 'display_blocks') ? { blocks: array(payload.blocks), display_closure: true,
        display_generation: payload.display_generation } : {}),
      utterance_page: { partial: true, scope: 'stage', next_before: null } });
  }
  async loadBlocks(ids) {
    // 명시적으로 펼친 과거 기록만 보강한다. 화면 탭과 현재 읽는 위치는 바꾸지 않는다.
    if (this.stopped || this.root.hidden || !supportsStages(this.session)) return false;
    for (const id of new Set(array(ids).filter(validId))) {
      let read = this.blockReads.get(id);
      if (!read) {
        const abort = new AbortController(), epoch = this.readEpoch;
        const promise = (async () => {
          const payload = await this.readSnapshot(`sessions/${encodeURIComponent(ctx.sid)}/transcript-stages?recent=true&block_id=${id}&limit=100`, abort.signal);
          if (this.stopped || abort.signal.aborted || epoch !== this.readEpoch) return false;
          const merged = this.mergeUtteranceClosure(payload);
          if (merged?.then) await merged;
          if (this.stopped || abort.signal.aborted || epoch !== this.readEpoch
              || this.store.merge(payload, false, false) === false) return false;
          this.activateSources();
          this.notifyLegacy();
          this.renderMemberPanels();
          return true;
        })();
        read = { abort, promise };
        this.blockReads.set(id, read);
      }
      try {
        if (!await read.promise) return false;
      } finally {
        if (this.blockReads.get(id) === read) this.blockReads.delete(id);
      }
    }
    return true;
  }
  memberVersion(block, rows) {
    if (this.store.canonical) {
      const projection = this.store.displayBlocks.get(block.id), summaryDocument = projection?.document || null;
      return { summaryDocument, stamp: JSON.stringify([block.id, block.questioner, block.is_open,
        block.review_status, projection?.fingerprint, projection?.summary,
        this.store.displayRowsForBlock(block.id), projection ? null : humanSummary(block)]) };
    }
    const candidate = this.store.latestDocument(block.id);
    const protectedRows = rows.some((row) => row.protected === true || row.edited || row.speaker_source === 'manual'
      || array(row.review_parts).length || typeof row.original_text === 'string' && row.text !== row.original_text);
    const summaryDocument = candidate && candidate.summary && !block.is_open && block.review_status !== 'reviewed'
      && (!block.questioner || candidate.questioner === block.questioner) && !humanSummary(block) && !protectedRows
      && this.store.documentCurrent(candidate)
      && !this.store.turnsForSources(array(candidate.source_ids)).some(protectedTurn) ? candidate : null;
    // 수신 경로의 가벼운 block 이벤트는 자동 요약을 생략한다. 같은 유효 문서의 화면은 유지한다.
    const summaryStamp = summaryDocument ? ['closed_block', summaryDocument.id, summaryDocument.summary] : block.summary;
    const stamp = JSON.stringify([block.questioner, block.is_open, block.ts_start, block.ts_end,
      block.review_status, summaryStamp]);
    return { stamp, summaryDocument };
  }
  memberDetails(block, rows = []) {
    if (!supportsStages(this.session) || !validId(block?.id)) return null;
    let panel = this.memberPanels.get(block.id);
    const { stamp, summaryDocument } = this.memberVersion(block, rows);
    if (!panel) {
      const details = h('details', { class: 'transcript-member-block', id: `member-block-${block.id}` },
        h('summary', null, block.time_range || block.title || '질의 내역'));
      const body = h('div', { class: 'transcript-member-block-body' }); details.append(body);
      panel = { details, body, block, rows, stamp, summaryDocument, loaded: false, loading: false, generation: 0,
        again: false, abort: null, error: '', fingerprint: '' };
      this.memberPanels.set(block.id, panel);
      details.addEventListener('toggle', () => {
        if (details.open) { this.renderMemberPanel(panel); this.loadMemberBlock(panel); }
        else { panel.abort?.abort(); ++panel.generation; panel.loading = false; }
      });
    } else {
      if (panel.stamp !== stamp || panel.summaryDocument !== summaryDocument) {
        panel.abort?.abort(); ++panel.generation; panel.loading = false; panel.loaded = false;
      }
      panel.block = block; panel.rows = rows; panel.stamp = stamp; panel.summaryDocument = summaryDocument;
      panel.details.children[0].textContent = block.time_range || block.title || '질의 내역';
      if (panel.details.open) { this.renderMemberPanel(panel); this.loadMemberBlock(panel); }
    }
    return panel.details;
  }
  renderMemberPanel(panel) {
    if (!panel.details.open) return;
    const stamp = JSON.stringify([panel.stamp, panel.loaded, panel.error, panel.rows,
      this.store.blockVersions.get(panel.block.id) || 0]);
    if (stamp === panel.renderStamp) return;
    panel.renderStamp = stamp;
    const documents = this.store.canonical ? [] : this.store.orderedDocuments().filter((doc) => doc.block_id === panel.block.id);
    const turns = this.store.canonical ? [] : this.store.orderedLiveTurns().filter((turn) => turn.block_id === panel.block.id);
    const dependencyIds = new Set([...documents, ...turns, ...turns.map((turn) => turn.speaker_guess || {})]
      .flatMap((value) => [...revisionsFor(value).keys()]));
    const signature = JSON.stringify([panel.block, panel.rows, panel.loaded, panel.error,
      documents, turns, this.store.canonical ? [this.store.displayBlocks.get(panel.block.id)?.fingerprint,
        this.store.displayRowsForBlock(panel.block.id)] : [...dependencyIds].map((id) => [id, this.store.sources.get(id)?.revision])]);
    if (signature === panel.fingerprint) return;
    panel.fingerprint = signature;
    const position = viewportSnapshot(panel.body, panel.body);
    const nodes = [];
    if (panel.error) nodes.push(h('p', { class: 'small muted', role: 'status' }, panel.error));
    if (!panel.loaded) nodes.push(h('p', { class: 'small muted', role: 'status' }, '최신 보정 내역을 불러오는 중입니다.'));
    else nodes.push(renderMemberContent(panel.block, this.store, panel.rows, (id) => {
      window.gukgamTranscriptGoFeed?.(); this.revealSource(id);
    }, { recording: this.session?.source_type === 'file' }));
    setKids(panel.body, ...nodes);
    restoreViewport(panel.body, position);
  }
  renderMemberPanels() {
    if (this.mainTab !== 'members') return;
    for (const panel of this.memberPanels.values()) this.renderMemberPanel(panel);
  }
  async refreshOpenMembers() {
    if (this.stopped || document.hidden || this.mainTab !== 'members') return;
    await Promise.all([...this.memberPanels.values()].filter((panel) => panel.details.open)
      .map((panel) => this.loadMemberBlock(panel)));
  }
  async loadMemberBlock(panel) {
    if (this.stopped || document.hidden || this.mainTab !== 'members' || !panel.details.open) return;
    if (panel.loading) { panel.again = true; return; }
    panel.loading = true;
    const generation = ++panel.generation, epoch = this.readEpoch;
    panel.abort = new AbortController();
    try {
      const payload = await this.readSnapshot(`sessions/${encodeURIComponent(ctx.sid)}/transcript-stages?recent=true&block_id=${panel.block.id}&limit=100`, panel.abort.signal);
      if (this.stopped || generation !== panel.generation || !panel.details.open) return;
      if (epoch !== this.readEpoch) { panel.again = true; return; }
      const merged = this.mergeUtteranceClosure(payload);
      if (merged?.then) await merged;
      if (this.stopped || generation !== panel.generation || !panel.details.open) return;
      if (epoch !== this.readEpoch || this.store.merge(payload, false, false) === false) { panel.again = true; return; }
      panel.loaded = true; panel.error = '';
      // 처음 lazy 조회로 문서를 받은 경우도 이후 경량 SSE와 같은 판본 기준으로 비교한다.
      Object.assign(panel, this.memberVersion(panel.block, panel.rows));
      this.notifyLegacy();
      this.renderMemberPanels();
    } catch (error) {
      if (generation !== panel.generation || error?.name === 'AbortError') return;
      panel.error = '최신 보정 내역을 조회하지 못했습니다. 다시 확인합니다.';
    } finally {
      if (generation === panel.generation) {
        panel.loading = false; panel.abort = null;
        this.renderMemberPanel(panel);
        const again = panel.again; panel.again = false;
        this.schedule(again ? 200 : 15000);
      }
    }
  }
  showLegacy() {
    if (this.root.hidden) return;
    this.chosen = true;
    this.legacy = true;
    this.renderMode();
  }
  select(stage) {
    if (!STAGES.includes(stage)) return;
    this.chosen = true;
    this.stage = stage;
    this.legacy = false;
    this.renderMode();
    this.render(true);
  }
  renderMode() {
    this.legacyEl.hidden = !this.legacy;
    document.body.classList.toggle('show-transcript-stages', this.isActive());
    document.body.classList.toggle('has-transcript-stages', this.knownSources && !this.root.hidden);
    for (const stage of STAGES) {
      const selected = !this.legacy && stage === this.stage;
      const button = this.get(`transcript-tab-${stage}`);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = stage === this.stage ? 0 : -1;
      this.get(`transcript-panel-${stage}`).hidden = !selected;
    }
    const button = this.get('transcript-legacy-toggle');
    const recording = this.session?.source_type === 'file';
    button.textContent = this.legacy ? recording ? '보정·원전사 보기' : '새 자막 보기' : '이전 기록';
    button.setAttribute('aria-expanded', String(this.legacy));
    this.get('transcript-stage-guide').textContent = this.legacy
      ? recording ? '저장된 녹음 전사·검수·요약을 표시합니다. 종료 질의의 AI 보정이 있으면 함께 표시하고, 없는 구간은 원전사를 유지합니다.'
        : this.store.canonical ? '실시간 발언과 종료 질의의 AI 보정 결과를 같은 기록 화면에 표시합니다.'
          : '이전 방식으로 저장된 발언·검수·요약 기록입니다. 새 블록 보정과 구분해 보존합니다.'
      : (recording ? FILE_GUIDES : GUIDES)[this.stage];
    this.get('transcript-stage-follow').closest('label').hidden = this.legacy;
    this.get('transcript-stage-older').hidden = this.legacy || this.store.nextBefore === null;
    this.currentEl.hidden = this.legacy || this.stage !== 'corrected';
    this.root.classList.toggle('transcript-data-only', this.legacy);
  }
  receive(type, data) {
    if (this.root.hidden || this.stopped) return;
    if (type === 'transcript.display') {
      this.schedule(0); return;
    }
    if (this.store.canonical) {
      // 입력 변경은 저장 선택의 갱신을 기다린다. 이전 3단계를 구형 조립 경로로 되살리지 않는다.
      if (type === 'utterance.upsert' || type === 'utterance.delete') {
        this.store.retireDisplay({ utteranceIds: [data.id],
          blockIds: this.store.displayBlocks.has(data.block_id) ? [data.block_id] : [], generation: data.display_input_generation });
      } else if (type === 'caption.source') {
        this.store.retireDisplay({ sourceIds: [(data?.source || data)?.id], generation: data.display_input_generation });
      } else if (type === 'transcript.stage' && validId(data?.block_id)) {
        this.store.retireDisplay({ blockIds: [data.block_id], generation: data.display_input_generation });
      } else if (type === 'block.upsert') {
        const previous = this.store.blocks.get(data.id);
        const stamp = (block) => JSON.stringify([block?.questioner, block?.kind, block?.ts_start, block?.ts_end,
          block?.is_open, block?.review_status, humanSummary(block || {})]);
        if (previous && stamp(previous) !== stamp({ ...previous, ...data })) {
          this.store.retireDisplay({ blockIds: [data.id], generation: data.display_input_generation });
          this.notifyLegacy(); this.queueLiveRefresh(); this.renderMemberPanels();
        }
      }
    }
    if (type === 'block.upsert' || type === 'roster.updated' || type === 'settings.updated' || type === 'resync') {
      let changed = false;
      if (type === 'block.upsert') changed = this.store.block(data);
      if (type === 'roster.updated') {
        const signature = JSON.stringify(array(data?.members).map((row) => [row.name, row.role, row.party, row.agency, row.scope])
          .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))));
        changed = !data?.baseline && signature !== this.rosterSignature;
        this.rosterSignature = signature;
        if (changed) this.store.invalidateMetadata(null, data?.display_input_generation);
      }
      if (type === 'settings.updated' && ['llm', 'org', 'watch_keywords', 'transcript_review', 'stt'].includes(data?.key)
          || type === 'resync' && data?.reason === 'roster_updated') {
        changed = true; this.store.invalidateMetadata(null, data?.display_input_generation);
      }
      if (changed || type === 'resync') {
        if (!this.store.canonical) ++this.readEpoch;
        this.render();
        this.queueLiveRefresh();
        this.notifyLegacy({ blockIds: type === 'block.upsert' ? [data.id] : [] });
      }
      return;
    }
    if (!this.store.canonical && (type === 'transcript.stage' || type === 'utterance.delete'
        || type === 'utterance.upsert' && (data?.speaker_source === 'manual' || data?.edited || array(data?.review_parts).length)
        || type === 'caption.source' && array(data?.invalidated_document_ids || data?.source?.invalidated_document_ids).length)) ++this.readEpoch;
    if (type === 'caption.source') {
      const source = data?.source ? { ...data.source, invalidated_document_ids: data.invalidated_document_ids || data.source.invalidated_document_ids } : data;
      if (this.store.source(source, true)) {
        this.activateSources();
        this.queueRender();
        this.queueLiveRefresh();
      }
      if (array(source?.invalidated_document_ids).length) this.schedule(200);
    }
    if (type === 'utterance.upsert' || type === 'utterance.delete') {
      if (this.store.liveUtterance(type, data)) {
        this.queueRender();
      }
      this.queueLiveRefresh();
    }
    this.notifyLegacy(type === 'utterance.upsert' || type === 'utterance.delete'
      ? { utteranceIds: [data.id], blockIds: [data.block_id].filter(validId) } : {});
    // 이미 열린 SSE를 재사용하고 변경된 문서만 읽기 API로 다시 받는다.
    if (type === 'transcript.stage') this.schedule(200);
  }
  queueRender() {
    // 계속 도착하는 부분 자막이 타이머를 뒤로 미루지 않도록 짧게 묶어 이어 그린다.
    if (!this.renderTimer) this.renderTimer = setTimeout(() => {
      this.renderTimer = 0; this.render();
    }, 50);
  }
  queueLiveRefresh() {
    // 부분 자막만 계속 와도 후보를 읽고, 연속 수신이 타이머를 끝없이 미루지 않게 한다.
    if (!this.liveRefreshTimer) this.liveRefreshTimer = setTimeout(() => {
      this.liveRefreshTimer = 0;
      this.refresh();
    }, 500);
  }
  activateSources() {
    if (!this.knownSources && (this.store.sources.size || this.store.canonical && this.store.displayRows.size)) {
      this.knownSources = true;
      // 원수신이 생겨도 검색·북마크가 있는 기본 기록 화면을 바꾸지 않는다.
      this.renderMode();
    }
  }
  schedule(delay = 15000) {
    clearTimeout(this.timer);
    if (ctx.offline) return;
    if (this.stopped || document.hidden || this.root.hidden || !['feed', 'members'].includes(this.mainTab)
        || this.mainTab === 'members' && ![...this.memberPanels.values()].some((panel) => panel.details.open)) return;
    this.timer = setTimeout(() => this.refresh(), delay);
  }
  cancel() {
    this.stopped = true;
    if (this.legacyFrame !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.legacyFrame);
    this.legacyFrame = null;
    this.legacyPendingBlocks.clear(); this.legacyPendingInvalidated.clear();
    this.legacyPendingUtterances.clear();
    for (const cached of this.legacyCache.values()) cached.dirty = true;
    if (this.viewportFrame !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.viewportFrame);
    this.viewportFrame = null;
    ++this.generation;
    this.abort?.abort();
    this.abort = null;
    clearTimeout(this.timer);
    clearTimeout(this.liveRefreshTimer);
    this.liveRefreshTimer = 0;
    clearTimeout(this.renderTimer);
    this.renderTimer = 0;
    this.loading = false;
    this.refreshAgain = false;
    for (const panel of this.memberPanels.values()) {
      panel.abort?.abort(); ++panel.generation; panel.loading = false;
    }
    for (const read of this.blockReads.values()) read.abort.abort();
    this.blockReads.clear();
  }
  async refresh(older = false) {
    if (this.stopped || document.hidden || this.root.hidden || !this.session) return;
    if (this.mainTab === 'members') { await this.refreshOpenMembers(); return; }
    if (this.mainTab !== 'feed') return;
    if (this.loading) { this.refreshAgain = true; return; }
    if (older && this.store.nextBefore === null) return;
    this.loading = true;
    const generation = ++this.generation, epoch = this.readEpoch;
    this.abort = new AbortController();
    const before = older ? `&before=${this.store.nextBefore}` : '';
    const limit = 100;
    const olderButton = this.get('transcript-stage-older');
    olderButton.disabled = true;
    try {
      const payload = await this.readSnapshot(`sessions/${encodeURIComponent(ctx.sid)}/transcript-stages?recent=true&limit=${limit}${before}`, this.abort.signal);
      if (this.stopped || generation !== this.generation) return;
      if (epoch !== this.readEpoch) { this.refreshAgain = true; return; }
      const merged = this.mergeUtteranceClosure(payload);
      if (merged?.then) await merged;
      if (this.stopped || generation !== this.generation) return;
      if (epoch !== this.readEpoch || this.store.merge(payload, older) === false) { this.refreshAgain = true; return; }
      this.loaded = true;
      this.activateSources();
      if (!this.knownSources && !this.chosen) this.legacy = true;
      this.renderMode();
      this.get('transcript-stage-status').textContent = this.store.sources.size
        ? '' : this.session.source_type === 'file' ? '저장된 보정 출처가 아직 없습니다. 원전사 기록을 표시합니다.'
          : '새 방식의 자막 기록이 아직 없습니다. 이전 기록을 표시합니다.';
      this.render(false, older);
      this.notifyLegacy();
    } catch (error) {
      if (generation !== this.generation || error?.name === 'AbortError') return;
      this.get('transcript-stage-status').textContent = this.session.source_type === 'file'
        ? '녹음 보정·원전사 조회가 지연되고 있습니다. 표시된 기록을 유지하고 다시 확인합니다.'
        : '새 자막 조회가 지연되고 있습니다. 표시된 기록을 유지하고 다시 확인합니다.';
      if (!this.loaded) { this.legacy = true; this.renderMode(); }
    } finally {
      if (generation === this.generation) {
        this.loading = false;
        olderButton.disabled = false;
        const again = this.refreshAgain;
        this.refreshAgain = false;
        this.schedule(again ? 200 : 15000);
      }
    }
  }
  revealSource(id) {
    this.get('transcript-stage-follow').checked = false;
    this.select('live');
    const panel = this.get('transcript-panel-live');
    const source = descendantById(panel, `caption-source-${id}`);
    source?.focus({ preventScroll: true });
    if (source) panel.scrollTop = Number(panel.scrollTop || 0) + source.getBoundingClientRect().top
      - panel.getBoundingClientRect().top - Number(panel.clientHeight || 0) / 2;
  }
  renderStoredTimeline(force = false) {
    const rows = this.store.orderedDisplayRows(), signature = JSON.stringify([rows,
      [...this.store.displayBlocks.values()].map((projection) => projection.fingerprint)]);
    if (!force && signature === this.fingerprint) { this.fitViewport(); this.queueViewport(); return; }
    this.fingerprint = signature;
    const panel = this.get('transcript-panel-corrected'), list = this.get('transcript-list-corrected');
    const position = viewportSnapshot(panel, list), used = new Set(), nodes = [];
    const last = rows.at(-1), streaming = last && last.stage !== 3 && (last.is_final === false
      || last.stage === 1 && array(last.source_ids).some((id) => this.store.sources.get(id)?.final === false));
    this.currentEl.hidden = !streaming;
    if (streaming) {
      if (this.currentText.textContent !== last.text) {
        if (this.currentText.firstChild?.nodeType === 3) this.currentText.firstChild.data = last.text;
        else this.currentText.textContent = last.text;
      }
      this.currentTime.textContent = Number.isFinite(last.ts_start) ? fmt.hms(last.ts_start) : '';
      this.currentSpeaker.textContent = `${last.speaker || LIVE_ROLES[last.speaker_role] || '발언'}${last.estimated ? ' (추정)' : ''}`;
      this.currentEl.dataset.turn = last.key; this.currentEl.dataset.role = last.speaker_role || 'unknown';
      this.currentRow.className = `transcript-stage-row role-${last.speaker_role || 'unknown'}`;
    }
    for (const row of rows) {
      if (streaming && row === last) { nodes.push(this.currentEl); continue; }
      if (row.stage === 3) {
        const projection = this.store.displayBlocks.get(row.block_id);
        if (!projection || used.has(projection.blockId)) continue;
        used.add(projection.blockId);
        const key = `stored:${projection.blockId}`, fingerprint = projection.fingerprint;
        let cached = this.documentNodes.get(key);
        if (!cached || cached.fingerprint !== fingerprint) {
          cached = { fingerprint, node: renderStoredBlock(projection, this.store, 'corrected', (id) => this.revealSource(id)) };
          this.documentNodes.set(key, cached);
        }
        nodes.push(cached.node);
      } else {
        const key = `stored:${row.key}`, fingerprint = JSON.stringify(row);
        let cached = this.sourceNodes.get(key);
        if (!cached || cached.fingerprint !== fingerprint) {
          cached = { fingerprint, node: renderStoredRow(row) }; this.sourceNodes.set(key, cached);
        }
        nodes.push(cached.node);
      }
    }
    // 부분 응답에 3단계 표시 카드만 포함되어도 저장된 블록 자체는 빠뜨리지 않는다.
    for (const projection of this.store.displayBlocks.values()) if (!used.has(projection.blockId))
      nodes.push(renderStoredBlock(projection, this.store, 'corrected', (id) => this.revealSource(id)));
    if (!nodes.length) nodes.push(h('p', { class: 'transcript-stage-empty' }, '저장된 표시 판본을 기다리고 있습니다.'));
    reconcileChildren(list, nodes);
    this.get('transcript-stage-count').textContent = `표시 ${rows.length}개 · 종료 블록 ${this.store.displayBlocks.size}개`;
    this.fitViewport(false);
    restoreViewport(panel, position, !panel.hidden && !this.get('tab-feed')?.hidden && this.mainTab === 'feed'
      && this.get('transcript-stage-follow').checked);
    this.queueViewport();
  }
  render(force = false, older = false) {
    this.renderMemberPanels();
    if (this.legacy) { this.currentEl.hidden = true; return; }
    if (this.store.canonical && this.stage === 'corrected') { this.renderStoredTimeline(force); return; }
    const sources = this.store.orderedSources(), docs = this.store.orderedDocuments();
    const turns = this.store.orderedLiveTurns();
    const allEntries = this.stage === 'corrected' ? timelineEntries(sources, docs, turns) : [];
    const stream = this.streamTimeline.split(allEntries, this.store.sources);
    const timeline = stream.history;
    this.currentEl.hidden = this.legacy || this.stage !== 'corrected' || !stream.current;
    const currentText = stream.current?.text || '새 발언을 기다리고 있습니다.';
    if (this.currentText.textContent !== currentText) {
      if (this.currentText.firstChild?.nodeType === 3) this.currentText.firstChild.data = currentText;
      else this.currentText.textContent = currentText;
    }
    this.currentTime.textContent = stream.current?.time || '';
    this.currentSpeaker.textContent = stream.current ? `${stream.current.label.speaker}${stream.current.label.estimated ? ' (추정)' : ''}` : '';
    this.currentEl.dataset.turn = stream.current?.key || '';
    this.currentEl.dataset.role = stream.current?.label.role || 'unknown';
    this.currentRow.className = `transcript-stage-row role-${stream.current?.label.role || 'unknown'}`;
    const pendingKey = (entry) => entry.kind === 'live' ? `live:${entry.turn.id}`
      : `pending:${entry.source.id}:${entry.offset}:${entry.lastSourceId || entry.source.id}:${entry.end}`;
    const pendingKeys = new Set(timeline.filter((entry) => ['source', 'live'].includes(entry.kind)).map(pendingKey));
    for (const key of this.sourceNodes.keys()) if (typeof key === 'string' && !pendingKeys.has(key)) this.sourceNodes.delete(key);
    const currentDocuments = new Set(timeline.filter((entry) => ['document', 'processing'].includes(entry.kind))
      .map((entry) => `${entry.kind}:${entry.doc.id}`));
    for (const key of this.documentNodes.keys()) if (!currentDocuments.has(key)) this.documentNodes.delete(key);
    this.get('transcript-stage-count').textContent = `${this.session?.source_type === 'file' ? '전사 조각' : '자막'} ${sources.length}개 · 종료 블록 ${docs.length}개`;
    const signature = JSON.stringify(this.stage === 'live' ? [this.stage, sources.map((s) => [s.id, s.revision])]
      : [this.stage, sources.map((s) => [s.id, s.revision, s.speaker_guess]), docs, turns]);
    if (!force && signature === this.fingerprint) { this.fitViewport(); this.queueViewport(); return; }
    this.fingerprint = signature;
    const list = this.get(`transcript-list-${this.stage}`);
    const panel = this.get(`transcript-panel-${this.stage}`);
    const position = viewportSnapshot(panel, list);
    const nodes = this.stage === 'live' ? sources.map((source) => {
      let entry = this.sourceNodes.get(source.id);
      if (!entry || entry.revision !== source.revision) {
        entry = { revision: source.revision, node: renderSource(source) };
        this.sourceNodes.set(source.id, entry);
      }
      return entry.node;
    }) : timeline.map((entry) => {
      if (entry.kind === 'source' || entry.kind === 'live') {
        const key = pendingKey(entry), fingerprint = JSON.stringify(entry);
        let cached = this.sourceNodes.get(key);
        if (!cached || cached.fingerprint !== fingerprint) {
          cached = { fingerprint, node: entry.kind === 'live' ? renderLiveTurn(entry) : renderPendingSource(entry) };
          this.sourceNodes.set(key, cached);
        }
        return cached.node;
      }
      const doc = entry.doc;
      const key = `${entry.kind}:${doc.id}`, fingerprint = JSON.stringify(doc);
      let cached = this.documentNodes.get(key);
      if (!cached || cached.fingerprint !== fingerprint) {
        cached = { fingerprint, node: entry.kind === 'processing'
          ? renderProcessingDocument(doc, this.store, (id) => this.revealSource(id))
          : renderDocument(doc, this.store, this.stage, (id) => this.revealSource(id)) };
        this.documentNodes.set(key, cached);
      }
      return cached.node;
    });
    if (this.stage === 'corrected' && stream.current) nodes.push(this.currentEl);
    if (!nodes.length) nodes.push(h('p', { class: 'transcript-stage-empty' }, this.session?.source_type === 'file'
      ? '저장된 녹음 전사·보정 기록을 기다리고 있습니다.' : this.stage === 'live'
        ? '새로 수신되는 국회 자막을 기다리고 있습니다.' : '새 발언을 기다리고 있습니다. 질의가 끝나면 보정한 내용을 표시합니다.'));
    reconcileChildren(list, nodes);
    this.fitViewport(false);
    const visible = !this.legacy && !panel.hidden && !this.get('tab-feed')?.hidden && this.mainTab === 'feed';
    restoreViewport(panel, position, visible && this.get('transcript-stage-follow').checked);
    if (visible) this.rememberReading();
    this.queueViewport();
  }
}

const stageRoot = document.getElementById('transcript-stages');
if (stageRoot) {
  const viewer = new TranscriptStages(stageRoot);
  window.gukgamTranscriptStages = viewer;
  if (window.gukgamTranscriptSession) viewer.setSession(window.gukgamTranscriptSession);
  if (window.gukgamTranscriptRoster) viewer.receive('roster.updated', { members: window.gukgamTranscriptRoster, baseline: true });
  if (window.gukgamTranscriptMainTab) viewer.setMainTab(window.gukgamTranscriptMainTab);
  window.gukgamTranscriptMembersRefresh?.();
}
