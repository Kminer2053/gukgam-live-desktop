// 발언 상세(퍼머링크 /u/{uid}): 화자 카드, 회의 메타, 본문, 블록 요약, 추출 항목, 이전/다음, 같은 화자 다른 발언, 인용문 복사.
import {
  $, absUrl, api, badge, BLOCK_STATUS, blockSummary, copyText, ctx, emptyState, errMsg, fmt, h, icon, initial,
  ITEM_KINDS, ITEM_STATUS, markedText, mentionRanges, normText, NOTICE, orgRelevanceBadge, quoteText, reviewNote,
  ROLE_LABELS, SOURCE_LABELS, speakerLabel, toast, setKids,
} from './common.js';

const UID = ctx.uid;
const root = $('#u-root');
const enc = encodeURIComponent;
let D = null;

const SRC = { feed: '수집원 제공', inferred: 'AI 추정', manual: '운영자 지정', ai_corrected: 'AI 보정' };
const AVATAR = { member: '', chair: ' navy', answer: ' green', system: ' gray', other: ' gray', unknown: ' gray' };

function speakerParty(u, block, roster) {
  if (block && block.kind === 'question' && u.speaker_role === 'member' && block.questioner === u.speaker) return block.questioner_party;
  const m = (roster || []).find((r) => r.name === u.speaker);
  return m ? m.party : '';
}

function speakerCard(u, block, roster) {
  const party = speakerParty(u, block, roster);
  return h('div', { class: 'speech-card' },
    h('div', { class: `avatar lg${AVATAR[u.speaker_role] || ''}`, 'aria-hidden': 'true' }, initial(u.speaker || ROLE_LABELS[u.speaker_role])),
    h('div', null,
      h('div', { class: 'nm' }, speakerLabel(u)),
      h('div', { class: 'row', style: { marginTop: '4px' } },
        badge(ROLE_LABELS[u.speaker_role] || '미상', u.speaker_role === 'member' ? 'b-blue' : u.speaker_role === 'chair' ? 'b-navy' : u.speaker_role === 'answer' ? 'b-green' : 'b-gray'),
        party ? badge(party, 'b-outline') : null,
        badge(SRC[u.speaker_source] || '추정', 'b-gray', '화자 정보 출처'))));
}

function blockSummaryCard(b) {
  const sm = blockSummary(b);
  const st = BLOCK_STATUS[b.status];
  const head = h('div', { class: 'card-head' },
    h('h3', null, icon('message', 'ic-sm'), `${b.title} `, h('span', { class: 'mono small muted' }, b.time_range)),
    b.kind === 'question' && st ? badge(st.label, st.cls) : null);
  if (!sm) return h('section', { class: 'card' }, head, h('p', { class: 'small muted', style: { margin: 0 } }, b.kind === 'question' ? '이 질의 블록의 AI 요약이 아직 없습니다.' : '진행 발언 구간입니다.'));
  const list = (title, arr) => (arr && arr.length ? h('div', null, h('h4', null, title), h('ul', { style: { margin: '4px 0 0', paddingLeft: '18px' } },
    arr.map((x) => h('li', null, typeof x === 'string' ? x : x.text, typeof x === 'object' && x.utterance_id
      ? h('a', { href: `/u/${x.utterance_id}`, class: 'small', style: { marginLeft: '6px' } }, '발언') : null)))) : null);
  return h('section', { class: 'card' }, head,
    h('div', { class: 'row', style: { marginBottom: '8px' } },
      b.review_status === 'reviewed' ? badge('검수 완료', 'b-green') : badge('검수 전 초안', 'b-gray'),
      orgRelevanceBadge(sm.org_relevance), sm.response_type ? badge(sm.response_type, 'b-outline') : null, sm.urgency ? badge(sm.urgency, 'b-orange') : null),
    sm.one_line ? h('p', { style: { fontWeight: 650 } }, sm.one_line) : null,
    h('div', { class: 'grid-2' }, list('질의 요지', sm.question_points), list('답변 요지', sm.answer_points)),
    h('div', { class: 'grid-2', style: { marginTop: '8px' } }, list('요구자료', sm.data_requests), list('약속·검토사항', sm.commitments)),
    list('지적사항', sm.issues),
    sm.org_reason ? h('p', { class: 'note info', style: { marginTop: '8px' } }, `우리 기관 관련: ${sm.org_reason}`) : null,
    h('div', { class: 'row-between small muted', style: { marginTop: '12px' } },
      h('span', null, reviewNote(b)),
      h('button', { type: 'button', class: 'btn btn-sm', onclick: (e) => copyCard(b.id, e.currentTarget) }, icon('copy', 'ic-sm'), '요지 카드 복사')));
}

async function copyCard(bid) {
  try {
    const r = await api(`blocks/${bid}/card`);
    await copyText(r.text || r.markdown || '', '질의·답변 요지 카드를 복사했습니다.');
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

function itemsCard(items) {
  if (!items || !items.length) return null;
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h3', null, icon('checklist', 'ic-sm'), `이 발언에서 추출한 후속조치 ${items.length}건`), h('a', { class: 'btn btn-ghost btn-sm', href: `/items?session_id=${enc(D.session.id)}` }, '트래커')),
    h('ul', { class: 'list-plain' }, items.map((it) => h('li', { class: 'li', style: { borderBottom: '1px solid var(--border)' } },
      h('div', { class: 'row' }, h('span', { class: `chip k-${it.kind}` }, `${ITEM_KINDS[it.kind]?.icon || ''} ${it.kind_label || ITEM_KINDS[it.kind]?.label || it.kind}`),
        it.strength ? badge(it.strength, 'b-outline badge-sm') : null,
        badge(it.status_label || ITEM_STATUS[it.status], it.status === 'done' ? 'b-green badge-sm' : 'b-gray badge-sm'),
        it.assignee_dept ? h('span', { class: 'small muted' }, `담당 ${it.assignee_dept}`) : null,
        it.due_date ? h('span', { class: 'small muted' }, `기한 ${it.due_date}`) : null),
      h('div', { style: { marginTop: '4px' } }, it.text)))));
}

function navCard(u, dir) {
  if (!u) return h('div', { class: 'small faint', style: { padding: '12px' } }, dir === 'prev' ? '처음 발언입니다' : '마지막 발언입니다');
  return h('a', { href: `/u/${u.id}`, class: dir === 'next' ? 'next' : '' },
    h('span', { class: 'd' }, dir === 'prev' ? '← 이전 발언' : '다음 발언 →'),
    h('b', null, `${u.time || fmt.hms(u.ts_start)} ${speakerLabel(u)}`),
    h('span', { class: 'clamp-2 small muted' }, normText(u.text)));
}

function render() {
  const { utterance: u, block: b, prev, next, items, session: s } = D;
  const selected = Array.isArray(D.display_rows) ? D.display_rows : [];
  const shown = selected[0] || u;
  document.title = `${speakerLabel(shown)} ${u.time} 발언 · 국감 라이브`;
  setKids($('#crumbs'), h('a', { href: '/' }, '대시보드'), ' › ', h('a', { href: `/s/${enc(s.id)}` }, s.title), ' › ', `발언 ${u.time}`);
  const text = normText(u.text);
  const selectedBody = selected.length ? h('div', { class: 'saved-display-body' }, selected.map(row =>
    h('div', { class: 'speech-text' }, selected.length > 1 ? h('b', null, `${speakerLabel(row)} `) : null,
      markedText(row.text, mentionRanges(row.text, row.mentions))))) : null;
  const storedRecord = selected.some(row => row.display_stage === 3) ? h('details', { class: 'small' },
    h('summary', null, '저장 기록과 비교'),
    h('p', { class: 'muted' }, '현재 저장된 기록 본문입니다. 사람의 수정 내용이 포함될 수 있습니다.'),
    h('p', { class: 'speech-text' }, `${speakerLabel(u)}: ${text}`)) : null;
  const quote = selected.length ? selected.map(row => quoteText(row, s)).join('\n\n') : quoteText(u, s);
  const main = h('div', { class: 'stack' },
    h('section', { class: 'card' },
      speakerCard(shown, b, D.roster),
      h('div', { class: 'speech-time', style: { marginTop: '16px' } }, icon('clock', 'ic-sm'), ` ${fmt.meetingDate(fmt.ymd(u.ts_start))} ${u.time}`,
        u.edited ? badge('수정됨', 'b-gray badge-sm') : null, !u.is_final ? badge('수신 중(미확정)', 'b-orange badge-sm') : null),
      selectedBody || h('p', { class: 'speech-text' }, u.speaker_role === 'system' ? `— ${text} —` : markedText(text, mentionRanges(text, u.mentions))),
      storedRecord,
      u.mentions && u.mentions.length ? h('div', { class: 'chips', style: { marginBottom: '12px' } }, [...new Set(u.mentions.map((m) => m.keyword))].map((k) => badge(`감시 키워드: ${k}`, 'b-red badge-sm'))) : null,
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn btn-primary btn-sm', onclick: () => copyText(quote, '인용문을 복사했습니다.') }, icon('copy', 'ic-sm'), '인용문 복사'),
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => copyText(absUrl(`/u/${u.id}`), '링크를 복사했습니다.') }, icon('link', 'ic-sm'), '링크 복사'),
        h('a', { class: 'btn btn-sm', href: `/s/${enc(s.id)}#u-${u.id}` }, icon('list', 'ic-sm'), '회의에서 보기'),
        h('button', { type: 'button', class: 'btn btn-sm', id: 'bm-btn', 'aria-pressed': String(!!D.bookmarked), onclick: toggleBookmark }, icon(D.bookmarked ? 'star-fill' : 'star', 'ic-sm'), D.bookmarked ? '북마크됨' : '북마크')),
      h('p', { class: 'small faint', style: { margin: '12px 0 0' } }, '인용 형식: ', h('span', { class: 'mono' }, `${quoteText(u, s).split('] ')[0]}] …`))),
    b ? blockSummaryCard(b) : null,
    itemsCard(items),
    h('div', { class: 'nav-pair' }, navCard(prev, 'prev'), navCard(next, 'next')));

  const side = h('div', { class: 'stack' },
    h('section', { class: 'card' },
      h('h3', null, icon('info', 'ic-sm'), ' 회의 정보'),
      h('dl', { class: 'kv', style: { marginTop: '8px' } },
        h('dt', null, '회의'), h('dd', null, h('a', { href: `/s/${enc(s.id)}` }, s.title)),
        s.committee ? [h('dt', null, '위원회'), h('dd', null, s.committee)] : null,
        s.agencies ? [h('dt', null, '피감기관'), h('dd', null, s.agencies)] : null,
        h('dt', null, '회의일'), h('dd', null, s.meeting_date ? fmt.meetingDate(s.meeting_date) : '-'),
        b ? [h('dt', null, '질의 블록'), h('dd', null, h('a', { href: `/s/${enc(s.id)}#b-${b.id}` }, b.title), h('div', { class: 'mono small muted' }, b.time_range))] : null,
        h('dt', null, '수집'), h('dd', null, s.source_label || SOURCE_LABELS[s.source_type] || s.source_type),
        h('dt', null, '발언 번호'), h('dd', { class: 'mono' }, `#${u.seq}`))),
    h('section', { class: 'card' },
      h('h3', null, icon('user', 'ic-sm'), ` 같은 화자 다른 발언`),
      h('div', { id: 'same-speaker', style: { marginTop: '8px' } }, h('div', { class: 'loading loading-sm' }, '찾는 중…'))),
    h('p', { class: 'small faint' }, NOTICE));
  setKids(root, h('div', { class: 'speech' }, main, side));
  loadSameSpeaker();
}

async function toggleBookmark() {
  try {
    const r = await api(`utterances/${UID}/bookmark`, { method: 'POST' });
    toast(r && r.bookmarked === false ? '북마크를 해제했습니다.' : '북마크했습니다.', { type: 'ok', timeout: 1500 });
    const btn = $('#bm-btn');
    if (btn && r) { setKids(btn, icon(r.bookmarked ? 'star-fill' : 'star', 'ic-sm'), r.bookmarked ? '북마크됨' : '북마크'); btn.setAttribute('aria-pressed', String(!!r.bookmarked)); }
  } catch (e) { toast(errMsg(e), { type: 'err' }); }
}

async function loadSameSpeaker() {
  const el = $('#same-speaker');
  const u = D.utterance;
  if (!u.speaker || u.speaker_role === 'system' || u.speaker === '답변') { setKids(el, h('p', { class: 'small muted', style: { margin: 0 } }, '화자 이름이 없어 찾지 않았습니다.')); return; }
  let list = D.same_speaker;
  try {
    if (!Array.isArray(list)) {
      const snap = await api(`sessions/${enc(D.session.id)}/snapshot`);
      D.roster = D.roster || snap.roster;
      list = (snap.utterances || []).filter((x) => x.speaker === u.speaker && x.id !== u.id && x.is_final);
      list.sort((a, b) => Math.abs(a.ts_start - u.ts_start) - Math.abs(b.ts_start - u.ts_start));
      list = list.slice(0, 12).sort((a, b) => a.ts_start - b.ts_start);
    }
  } catch (e) {
    setKids(el, h('p', { class: 'small muted', style: { margin: 0 } }, `불러오지 못했습니다: ${errMsg(e)}`));
    return;
  }
  if (!list.length) { setKids(el, h('p', { class: 'small muted', style: { margin: 0 } }, '이 회의에서 다른 발언이 없습니다.')); return; }
  setKids(el, h('ul', { class: 'list-plain' }, list.map((x) => h('li', null, h('a', { href: `/u/${x.id}` },
    h('span', { class: 'mono small muted' }, x.time || fmt.hms(x.ts_start)), ' ', h('span', { class: 'clamp-2' }, normText(x.text)))))));
}

async function boot() {
  if (!UID) { setKids(root, emptyState('발언 번호가 없습니다', '', 'alert')); return; }
  try {
    D = await api(`utterances/${UID}`);
    D.items = D.items || [];
    render();
  } catch (e) {
    setKids(root, h('div', { class: 'card' }, emptyState(e.status === 404 ? '발언을 찾을 수 없습니다' : '발언을 불러오지 못했습니다', errMsg(e), 'alert'),
      h('div', { class: 'row', style: { justifyContent: 'center' } }, h('a', { class: 'btn', href: '/' }, '대시보드로'))));
  }
}
boot();
