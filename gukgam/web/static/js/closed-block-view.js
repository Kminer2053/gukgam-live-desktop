// 검증된 종료 질의 보정 문서를 기존 기록 화면에 읽기 전용으로 투영한다.
// 호출자는 StageStore가 현재 판본으로 확인한 문서와 말묶음만 전달해야 한다.

const validId = (value) => Number.isSafeInteger(value) && value > 0;
const values = (value) => value instanceof Map ? [...value.values()] : Array.isArray(value) ? value : [];
const list = (value) => Array.isArray(value) ? value : [];
const ROLE_NAMES = { member: '질의 의원', answer: '답변 기관', chair: '위원장 진행', other: '기타 발언' };
const role = (sentence) => Object.hasOwn(ROLE_NAMES, sentence.role) ? sentence.role : 'other';
const speaker = (sentence) => typeof sentence.speaker === 'string' && sentence.speaker.trim()
  && sentence.speaker !== '화자 확인 필요' ? sentence.speaker : ROLE_NAMES[role(sentence)];
const protectedRow = (row) => !!row && (row.protected === true || !!row.edited || !!row.edited_by
  || row.speaker_source === 'manual' || list(row.review_parts).length > 0
  || typeof row.original_text === 'string' && row.text !== row.original_text);

function savedSummary(block) {
  let summary = block.summary;
  if (typeof summary === 'string') {
    try { summary = JSON.parse(summary); } catch { return null; }
  }
  return summary && typeof summary === 'object' && !Array.isArray(summary) ? summary : null;
}

function validSpan(span, sources) {
  const source = sources.get(span?.source_id);
  return !!source && typeof source.text === 'string' && validId(span.source_id)
    && Number.isSafeInteger(span.start) && Number.isSafeInteger(span.end)
    && span.start >= 0 && span.end > span.start && span.end <= Array.from(source.text).length;
}

function meaningfulPositions(text) {
  const chars = Array.from(text), skipped = new Set();
  for (const match of text.matchAll(/^[\t ]*-\s+/gm)) {
    const start = Array.from(text.slice(0, match.index)).length;
    for (let index = start; index < start + Array.from(match[0]).length; index++) skipped.add(index);
  }
  return chars.map((char, index) => !/\s/u.test(char) && !skipped.has(index) ? index : -1)
    .filter((index) => index >= 0);
}

// 검증된 턴의 원자막 좌표를 실제 화면 본문의 유니코드 문자 좌표에 정확히 잇는다.
function turnCoordinates(turn, row, sources, meaningful) {
  if (typeof turn.original_text !== 'string' || typeof row.text !== 'string') return null;
  const original = Array.from(turn.original_text), text = Array.from(row.text);
  const originalPositions = meaningfulPositions(turn.original_text), positions = meaningfulPositions(row.text);
  const refs = turn.source_spans.flatMap((span) => {
    const chars = Array.from(sources.get(span.source_id).text);
    return (meaningful.get(span.source_id) || []).filter((index) => index >= span.start && index < span.end)
      .map((index) => ({ source_id: span.source_id, source_offset: index, char: chars[index] }));
  });
  const content = refs.map((ref) => ref.char).join('');
  if (content !== originalPositions.map((index) => original[index]).join('')
      || content !== positions.map((index) => text[index]).join('')) return null;
  return { text, positions, refs: refs.map((ref, index) => ({ ...ref, offset: positions[index] })) };
}

function consumedRanges(coordinates, selected) {
  const ranges = [];
  let first = null, last = null;
  const flush = () => {
    if (first === null) return;
    ranges.push({ start: first === 0 ? 0 : coordinates.positions[first],
      end: last === coordinates.positions.length - 1 ? coordinates.text.length : coordinates.positions[last] + 1 });
    first = last = null;
  };
  coordinates.positions.forEach((position, index) => {
    if (!selected.has(position)) { flush(); return; }
    if (first === null) first = index;
    last = index;
  });
  flush();
  return ranges;
}

// 문단이 문장을 빠뜨리거나 중복·재배열·화자 혼합하면 문장별 표시로 되돌린다.
function sentenceGroups(sentences, paragraphs) {
  const byId = new Map(sentences.map((sentence) => [String(sentence.id), sentence]));
  const groups = paragraphs.map((paragraph) => list(paragraph?.sentence_ids).map((id) => byId.get(String(id))));
  const actual = groups.flat().map((sentence) => String(sentence?.id));
  const expected = sentences.map((sentence) => String(sentence.id));
  const valid = groups.length > 0 && actual.length === expected.length
    && actual.every((id, index) => id === expected[index])
    && groups.every((group) => group.length > 0 && group.every((sentence) => sentence
      && role(sentence) === role(group[0]) && speaker(sentence) === speaker(group[0])));
  return valid ? groups.map((group, index) => ({ group, paragraph: paragraphs[index] }))
    : sentences.map((sentence) => ({ group: [sentence], paragraph: null }));
}

function summaryProjection(summary, document) {
  if (!summary || typeof summary !== 'object' || Array.isArray(summary) || summary.status === 'held') return null;
  return { ...summary, one_line: typeof summary.title === 'string' ? summary.title : summary.one_line || '',
    question_points: list(summary.bullets), answer_points: list(summary.answer_points),
    issues: list(summary.issues), data_requests: list(summary.key_requests), commitments: list(summary.commitments),
    provider: 'closed_block', source_document_id: document.id, ai_draft: true };
}

function compactText(text) {
  const chars = Array.from(text);
  return meaningfulPositions(text).map((index) => chars[index]).join('');
}
const sameSpans = (a, b) => Array.isArray(a) && Array.isArray(b) && a.length === b.length
  && a.every((span, index) => ['source_id', 'start', 'end'].every((key) => span?.[key] === b[index]?.[key]));
function sameRevisions(a, b, sources) {
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) || Array.isArray(b)) return false;
  const keys = Object.keys(a), other = Object.keys(b);
  return keys.length > 0 && keys.length === other.length && keys.every((id) => /^\d+$/.test(id)
    && Object.hasOwn(b, id) && validId(a[id]) && a[id] === b[id] && sources.get(Number(id))?.revision === a[id]);
}

// 오래된 수신 행은 검증된 같은 원조각의 완전 대체 증거가 있을 때만 표시에서 대체한다.
function supersededRanges(block, doc, sources, turns, rows, cards, consumed) {
  if (!Object.hasOwn(doc, 'source_supersessions')) return {};
  if (!Array.isArray(doc.source_supersessions)) return null;
  const result = {}, seen = new Set(), turnMap = new Map(turns.map((turn) => [turn.id, turn]));
  const sentenceSpans = list(doc.sentences).flatMap((sentence) => list(sentence.source_spans));
  for (const proof of doc.source_supersessions) {
    const uid = proof?.utterance_id, old = rows.get(uid);
    if (proof?.method !== 'same_source_revision_replacement_v1' || !validId(uid) || seen.has(uid)
        || !old || protectedRow(old) || old.block_id !== block.id || proof.block_id !== block.id
        || typeof proof.source_key !== 'string' || !proof.source_key
        || Object.hasOwn(old, 'source_key') && old.source_key !== proof.source_key
        || typeof proof.original_text !== 'string' || !proof.original_text || old.text !== proof.original_text
        || typeof old.original_text === 'string' && old.original_text !== proof.original_text
        || !Array.isArray(proof.sources) || proof.sources.length !== 1
        || !Array.isArray(proof.replacement_utterances) || !proof.replacement_utterances.length
        || consumed[uid] || cards.some((card) => card.source_utterance_ids.includes(uid))) return null;
    const historical = proof.sources[0], source = sources.get(historical?.source_id);
    if (!source || !list(doc.source_ids).includes(source.id) || !validId(historical.source_id)
        || typeof historical.source_key !== 'string' || !historical.source_key || source.source_key !== historical.source_key
        || !['string', 'number'].includes(typeof historical.segment) || String(source.segment) !== String(historical.segment)
        || !validId(historical.revision) || source.revision !== historical.revision
        || doc.source_revisions?.[String(source.id)] !== historical.revision
        || !validId(historical.historical_revision) || historical.historical_revision >= historical.revision
        || typeof historical.historical_text !== 'string' || !compactText(historical.historical_text)
        || compactText(historical.historical_text) !== compactText(proof.original_text)
        || source.final !== true || source.scd !== true || source.replayed === true) return null;
    const required = meaningfulPositions(source.text);
    if (!required.length || required.some((index) => !sentenceSpans.some((span) => span.source_id === source.id
        && span.start <= index && index < span.end))) return null;
    const replacements = new Set(), replacementSpans = [];
    for (const replacement of proof.replacement_utterances) {
      const id = replacement?.utterance_id, row = rows.get(id), turn = turnMap.get(id);
      if (!validId(id) || id === uid || replacements.has(id) || !row || !turn || protectedRow(row) || protectedRow(turn)
          || replacement.block_id !== block.id || row.block_id !== block.id || turn.block_id !== block.id
          || typeof replacement.source_key !== 'string' || !replacement.source_key
          || Object.hasOwn(row, 'source_key') && row.source_key !== replacement.source_key
          || Object.hasOwn(turn, 'source_key') && turn.source_key !== replacement.source_key
          || typeof replacement.original_text !== 'string' || !replacement.original_text
          || row.text !== replacement.original_text || turn.original_text !== replacement.original_text
          || typeof row.original_text === 'string' && row.original_text !== replacement.original_text
          || !sameSpans(replacement.source_spans, turn.source_spans)
          || !sameRevisions(replacement.source_revisions, turn.source_revisions, sources)
          || !cards.some((card) => card.source_utterance_ids.includes(id))) return null;
      const ranges = consumed[id];
      if (!Array.isArray(ranges) || ranges.length !== 1 || ranges[0].start !== 0
          || ranges[0].end !== Array.from(row.text).length) return null;
      replacements.add(id); replacementSpans.push(...turn.source_spans);
    }
    if (required.some((index) => !replacementSpans.some((span) => span.source_id === source.id
        && span.start <= index && index < span.end))) return null;
    seen.add(uid);
    result[uid] = [{ start: 0, end: Array.from(old.text).length }];
  }
  return result;
}

/**
 * 자막 ID와 발언 UID를 혼동하지 않는다. 북마크·메뉴는 source_utterance_ids의 기존 UID를 쓴다.
 * 한 카드라도 원발언에 완전히 연결되지 않으면 문서를 부분 누락시키지 않고 원기록을 유지한다.
 */
export function closedBlockProjection(block, doc, sources, turns, rows) {
  if (!block || !validId(block.id) || block.is_open || block.review_status === 'reviewed'
      || savedSummary(block)?.edited_by || !doc || !validId(doc.id) || doc.block_id !== block.id
      || doc.mode !== 'closed_block_v1' || !['draft', 'partial', 'ready'].includes(doc.status)
      || block.questioner && doc.questioner !== block.questioner) return null;
  const sentences = list(doc.sentences), paragraphs = list(doc.paragraphs);
  const sentenceIds = sentences.map((sentence) => sentence?.id);
  if (!sentences.length || sentenceIds.some((id) => !['string', 'number'].includes(typeof id) || !String(id))
      || new Set(sentenceIds.map(String)).size !== sentenceIds.length
      || sentences.some((sentence) => !sentence || typeof (sentence.text ?? sentence.original_text) !== 'string'
        || !list(sentence.source_spans).length)) return null;
  const sourceMap = new Map(values(sources).filter((source) => validId(source?.id) && typeof source.text === 'string')
    .map((source) => [source.id, source]));
  const rowMap = new Map(values(rows).filter((row) => validId(row?.id)).map((row) => [row.id, row]));
  if ([...rowMap.values()].some((row) => row.block_id === block.id && protectedRow(row))) return null;
  const sourceIds = new Set(list(doc.source_ids));
  if (sentences.some((sentence) => sentence.source_spans.some((span) => !validSpan(span, sourceMap)
      || !sourceIds.has(span.source_id)))) return null;
  const candidates = values(turns).filter((turn) => rowMap.has(turn?.id)
    && list(turn.source_spans).length && turn.source_spans.every((span) => validSpan(span, sourceMap)));
  const meaningful = new Map([...sourceMap].map(([id, source]) => [id, meaningfulPositions(String(source.text || ''))]));
  const cards = [], coordinates = new Map(), consumed = new Map();
  for (const { group, paragraph } of sentenceGroups(sentences, paragraphs)) {
    const spans = group.flatMap((sentence) => sentence.source_spans), mapped = new Set();
    const first = group[0], useParagraph = paragraph?.body_basis === 'source_group_v2'
      && typeof paragraph.text === 'string' && paragraph.text.trim();
    const text = useParagraph ? paragraph.text : group.map((sentence) => sentence.text ?? sentence.original_text).join('\n');
    const sourceCount = spans.reduce((count, span) => count + meaningful.get(span.source_id)
      .filter((index) => index >= span.start && index < span.end).length, 0);
    // 단독 화자 전환 표지는 발언 UID가 없을 수 있다. 새 본문을 만들어 내거나 실제 문자를 지우지는 않는다.
    if (!sourceCount || !meaningfulPositions(text).length) {
      if (!sourceCount && !meaningfulPositions(text).length) continue;
      return null;
    }
    for (const span of spans) {
      const significant = meaningful.get(span.source_id).filter((index) => index >= span.start && index < span.end);
      if (!significant.length) continue;
      const matches = candidates.filter((turn) => turn.source_spans.some((ref) => ref.source_id === span.source_id
        && ref.start < span.end && span.start < ref.end));
      if (!matches.length || matches.some((turn) => protectedRow(turn) || protectedRow(rowMap.get(turn.id)))) return null;
      const refs = [];
      for (const turn of matches) {
        if (!coordinates.has(turn.id)) coordinates.set(turn.id, turnCoordinates(turn, rowMap.get(turn.id), sourceMap, meaningful));
        const projection = coordinates.get(turn.id);
        if (!projection) return null;
        const used = projection.refs.filter((ref) => ref.source_id === span.source_id
          && ref.source_offset >= span.start && ref.source_offset < span.end);
        if (!used.length) continue;
        if (!consumed.has(turn.id)) consumed.set(turn.id, new Set());
        for (const ref of used) { consumed.get(turn.id).add(ref.offset); refs.push(ref.source_offset); }
        mapped.add(turn.id);
      }
      if (significant.some((index) => !refs.includes(index))) return null;
    }
    const ids = [...mapped];
    if (!ids.length) return null;
    const times = ids.map((id) => rowMap.get(id).ts_start).filter((value) => Number.isFinite(value));
    const fallbackTime = sourceMap.get(spans[0].source_id)?.first_rx;
    const statusParagraph = useParagraph ? paragraph : null;
    cards.push({ key: `closed-${doc.id}-${String(first.id)}`, text, speaker: speaker(first), speaker_role: role(first),
      source_utterance_ids: ids, ts_start: times.length ? Math.min(...times) : Number.isFinite(fallbackTime) ? fallbackTime : null,
      sentence_ids: group.map((sentence) => sentence.id), document_status: doc.status,
      text_status: statusParagraph && ['draft', 'held', 'pending'].includes(statusParagraph.text_status) ? statusParagraph.text_status
        : group.some(sentence => sentence.text_status === 'held') ? 'held'
          : group.every(sentence => sentence.text_status === 'draft') ? 'draft' : 'unknown',
      text_uncertain: statusParagraph?.text_uncertain === true || group.some(sentence => sentence.text_uncertain === true),
      role_uncertain: statusParagraph?.role_uncertain === true || group.some(sentence => sentence.role_uncertain === true) });
  }
  if (!cards.length) return null;
  const summary = summaryProjection(doc.summary, doc);
  const consumed_utterance_ranges = Object.fromEntries([...consumed].map(([id, selected]) =>
    [id, consumedRanges(coordinates.get(id), selected)]));
  const superseded = supersededRanges(block, doc, sourceMap, candidates, rowMap, cards, consumed_utterance_ranges);
  if (superseded === null) return null;
  Object.assign(consumed_utterance_ranges, superseded);
  return { id: doc.id, blockId: block.id, document: doc, summary, cards, consumed_utterance_ranges,
    fingerprint: JSON.stringify([doc, cards, summary, consumed_utterance_ranges,
      [...sourceMap.values()].filter((source) => sourceIds.has(source.id)).map((source) => [source.id, source.revision])]) };
}
