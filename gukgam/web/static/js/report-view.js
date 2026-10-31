// 저장된 상황보고를 읽기 좋은 화면으로 표시한다. 서버 요청·재요약 없이 오프라인에서도 재사용한다.
import { summaryPresentation } from './common.js';

function node(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (name === 'class') el.className = value;
    else if (name.startsWith('on') && typeof value === 'function') el.addEventListener(name.slice(2), value);
    else el.setAttribute(name, value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child != null && child !== false && child !== '') el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

function rows(value) { return Array.isArray(value) ? value : []; }
function text(value) { return typeof value === 'string' ? value.trim() : ''; }
function points(value) {
  // 같은 목록 안에 완전히 같은 문구가 반복될 때만 화면 중복을 제거한다. 저장 데이터는 바꾸지 않는다.
  return [...new Set(rows(value).map(text).filter(Boolean))];
}
function memberName(row) { return text(row.questioner) ? `${row.questioner} 위원` : '성명 미확인 위원'; }
function pill(label, kind = '') { return node('span', { class: `rp-pill ${kind}`.trim() }, label); }
function list(values, empty) {
  const content = points(values);
  return content.length ? node('ul', { class: 'rp-points' }, content.map(value => node('li', {}, value)))
    : node('p', { class: 'rp-empty-note' }, empty);
}
function sectionHead(number, title, description, count) {
  return node('header', { class: 'rp-section-head' },
    node('div', {}, node('span', { class: 'rp-section-number' }, number), node('h2', {}, title),
      count == null ? null : node('span', { class: 'rp-section-count' }, String(count))),
    description ? node('p', {}, description) : null);
}
function safeAnchor(makeHref, value, label, className = 'rp-source-link') {
  if (typeof makeHref !== 'function' || value == null) return null;
  const href = makeHref(value);
  if (!href || /^(?:javascript|data|vbscript):/i.test(String(href).trim())) return null;
  return node('a', { href, class: className }, label);
}

/** 모든 질의 상세를 열거나 닫는다. 인쇄 전에도 같은 함수를 사용한다. */
export function setReportExpanded(root, expanded) {
  for (const detail of root.querySelectorAll('details.rp-question')) detail.open = Boolean(expanded);
}

function reviewBadge(row) {
  const origin = summaryPresentation(row);
  if (row.review_status === 'reviewed') return pill(`${origin.label} · 검수 완료`, 'rp-pill-ok');
  return pill(`${origin.label} · 미검수`, origin.kind === 'human' ? 'rp-pill-warn' : '');
}

function correctionDocumentNotice(row) {
  // 다른 문서의 상태나 잘못된 수치를 요약의 처리 결과로 표시하지 않는다.
  const value = row.correction_document;
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !Number.isSafeInteger(value.document_id) || value.document_id <= 0
      || !Number.isSafeInteger(row.source_document_id) || value.document_id !== row.source_document_id
      || !['draft', 'partial', 'ready'].includes(value.status)) return null;
  const names = ['paragraph_count', 'held_paragraph_count', 'text_uncertain_paragraph_count', 'role_uncertain_paragraph_count'];
  if (names.some(name => !Number.isSafeInteger(value[name]) || value[name] < 0)
      || names.slice(1).some(name => value[name] > value.paragraph_count)) return null;
  const needsCheck = value.status === 'partial' || names.slice(1).some(name => value[name] > 0);
  const statusLabels = { draft: '보정 문서 상태: 검수 전 초안', partial: '보정 문서 상태: 일부 보정 확인 필요',
    ready: '보정 문서 상태: 저장된 보정본 · 검수 여부 별도 확인' };
  const labels = [node('strong', { class: needsCheck ? 'rp-pill-warn' : '' }, statusLabels[value.status]),
    node('span', {}, `총 ${value.paragraph_count}문단`)];
  if (value.held_paragraph_count) labels.push(node('span', {}, `본문 보정 보류 · 원문 유지: ${value.held_paragraph_count}문단`));
  if (value.text_uncertain_paragraph_count) labels.push(node('span', {}, `본문 보정 확인 필요: ${value.text_uncertain_paragraph_count}문단`));
  if (value.role_uncertain_paragraph_count) labels.push(node('span', {}, `화자 확인 필요: ${value.role_uncertain_paragraph_count}문단`));
  const held = row.held_bullet_count;
  const summaryNote = Number.isSafeInteger(held) && held >= 0
    ? row.partial === true && held > 0 ? `요약 항목: ${held}개 제외`
      : row.partial === true && held === 0 ? '요약 항목: 일부 확인 필요'
        : row.partial === false && held === 0 ? '요약 항목: 별도 제외 없음' : '' : '';
  return node('aside', { class: 'rp-empty-note', role: 'note', 'aria-label': '보정 문서 상태' },
    node('div', { class: 'rp-question-links' }, labels),
    node('p', {}, '보정 문서의 상태입니다. 요약 항목의 부분 여부와 별도로 확인하세요.'),
    summaryNote ? node('p', {}, summaryNote) : null);
}

function storedSessionSummary(sections, options) {
  // 필터 안내가 있으면 전체 요약 데이터가 함께 주어져도 표시하지 않는다.
  const notice = text(sections.session_summary_notice);
  if (notice) return node('p', { class: 'rp-empty-note', id: `${options.idPrefix}-session-summary` }, notice);
  const row = sections.session_summary;
  if (!row || typeof row !== 'object' || !row.content || typeof row.content !== 'object') return null;
  const content = row.content, origin = summaryPresentation(row);
  const groups = [
    ['주요 이슈', rows(content.issues).map(value => value?.title)],
    ['우리 기관 관련 포인트', content.org_points], ['후속조치 제안', content.next_actions],
  ];
  return node('section', { class: 'rp-section', id: `${options.idPrefix}-session-summary` },
    sectionHead('00', '저장된 회의 요약', '질의별 요약 건수와 별개로 저장된 회의 요약을 표시합니다.'),
    node('div', { class: 'rp-session-summary-meta' }, pill(origin.label), row.time ? node('span', {}, `${row.time} 생성`) : null),
    node('p', { class: 'rp-empty-note summary-origin-note' }, origin.note),
    origin.coverageLabel ? node('p', { class: 'rp-empty-note' }, origin.coverageLabel) : null,
    origin.coverageNote ? node('p', { class: 'rp-empty-note' }, origin.coverageNote) : null,
    text(content.overview) ? node('p', {}, content.overview) : null,
    groups.filter(([, values]) => points(values).length).map(([label, values]) =>
      node('section', { class: 'rp-qa' }, node('h3', {}, label), list(values, ''))),
    points(content.hashtags).length ? node('p', { class: 'rp-empty-note' }, points(content.hashtags).join(' ')) : null);
}

function questionCard(row, index, options, actionCount) {
  const key = `${options.idPrefix}-question-${row.block_id}`;
  const questions = points(row.question_points), answers = points(row.answer_points);
  const topic = text(row.one_line) || questions[0] || '저장된 요약이 없습니다';
  const summary = node('summary', {},
    node('span', { class: 'rp-question-no', 'aria-hidden': 'true' }, String(index + 1).padStart(2, '0')),
    node('span', { class: 'rp-question-heading' },
      node('span', { class: 'rp-member-meta' }, node('strong', {}, memberName(row)),
        row.party ? node('span', { class: 'rp-party' }, row.party) : null,
        node('span', { class: 'rp-question-time' }, row.time_range || '시각 미확인')),
      node('span', { class: 'rp-question-topic' }, topic)),
    node('span', { class: 'rp-question-badges' }, row.is_open ? pill('질의 중', 'rp-pill-live') : null,
      reviewBadge(row), node('span', { class: 'rp-chevron', 'aria-hidden': 'true' }, '⌄')));
  const content = node('div', { class: 'rp-question-body' },
    node('p', { class: 'rp-empty-note summary-origin-note' }, summaryPresentation(row).note),
    correctionDocumentNotice(row),
    node('div', { class: 'rp-qa-grid' },
      node('section', { class: 'rp-qa' }, node('h3', {}, '질의 요지'), list(questions,
        text(row.one_line) ? row.one_line : '저장된 질의 요지가 없습니다.')),
      node('section', { class: 'rp-qa rp-answer' }, node('h3', {}, '답변 요지'), list(answers, '저장된 답변 요지가 없습니다.'))),
    node('footer', { class: 'rp-question-foot' },
      node('span', {}, row.review_status === 'reviewed'
        ? `검수: ${text(row.reviewed_by) || '검수자'}` : '원문과 대조해 확인할 수 있는 보고 초안입니다.',
      text(row.edited_by) ? ` · 요약 수정: ${row.edited_by}` : ''),
      node('span', { class: 'rp-question-links' }, actionCount ? node('a', { href: `#${options.idPrefix}-actions` }, `후속조치 ${actionCount}건`) : null,
        safeAnchor(options.sessionHref, row.block_id, '질의 전문 보기'))));
  return node('details', { class: 'rp-question', id: key, open: options.expandAll || Boolean(row.org_related) }, summary, content);
}

function actionTable(values, kind, options) {
  if (!values.length) return node('p', { class: 'rp-empty-note' }, '해당 항목이 없습니다.');
  const statusLabels = { open: '미처리', in_progress: '처리 중', done: '완료', dismissed: '제외' };
  return node('div', { class: 'rp-table-wrap' }, node('table', { class: 'rp-action-table' },
    node('caption', { class: 'sr-only' }, kind),
    node('thead', {}, node('tr', {}, ['내용', '요구 위원', '담당·기한', '상태'].map(label => node('th', { scope: 'col' }, label)))),
    node('tbody', {}, values.map(row => node('tr', {},
      node('td', { 'data-label': '내용' }, node('p', { class: 'rp-action-text' }, row.text || ''),
        row.strength ? node('span', { class: 'rp-action-kind' }, row.strength) : null,
        safeAnchor(options.sourceHref, row.utterance_id, '관련 발언')),
      node('td', { 'data-label': '요구 위원' }, row.requester ? `${row.requester} 위원` : '기록 없음'),
      node('td', { 'data-label': '담당·기한' }, node('span', { class: 'rp-action-dept' }, row.dept || '담당 미지정'),
        node('span', { class: 'rp-action-due' }, row.due_date ? `기한 ${row.due_date}` : '기한 미기재')),
      node('td', { 'data-label': '상태' }, pill(statusLabels[row.status] || row.status || '미처리',
        row.status === 'done' ? 'rp-pill-ok' : '')))))));
}

/**
 * build_situation_report() 결과를 입력받는 공통 표시 함수.
 * options.sessionHref(blockId), sourceHref(uid)는 온라인/단독 HTML 내부 이동 주소를 주입한다.
 * API 호출, LLM 실행, 원문·요약 수정은 수행하지 않는다.
 */
export function renderSituationReport(report, options = {}) {
  options = { idPrefix: 'rp', expandAll: false, ...options };
  const s = report.sections || {}, progress = s.progress || {};
  const members = rows(s.members), orgQuestions = rows(s.org_questions);
  const requests = rows(s.data_requests), commitments = rows(s.commitments), special = rows(s.special);
  const reviewed = members.filter(row => row.review_status === 'reviewed').length;
  const actionCounts = new Map();
  for (const row of [...requests, ...commitments]) {
    if (row.block_id != null) actionCounts.set(row.block_id, (actionCounts.get(row.block_id) || 0) + 1);
  }
  const root = node('article', { class: 'situation-report', 'aria-label': '국정감사 상황보고' });
  const hero = node('header', { class: 'rp-cover' },
    node('div', { class: 'rp-cover-label' }, '국정감사 상황보고', pill(reviewed === members.length && members.length ? '전체 검수 완료' : '검수 전 초안',
      reviewed === members.length && members.length ? 'rp-pill-ok' : '')),
    node('h2', {}, String(report.title || '국정감사').replace(/^\[국정감사 상황보고\]\s*/, '')),
    node('p', { class: 'rp-cover-meta' }, s.subtitle || ''),
    node('div', { class: 'rp-progress' }, node('span', { class: 'rp-progress-dot', 'aria-hidden': 'true' }), progress.line || '저장된 진행 정보가 없습니다.'),
    node('dl', { class: 'rp-metrics' }, [
      ['질의 요약', members.length, '건'], ['우리 기관 관련', orgQuestions.length, '건'],
      ['요구자료', requests.length, '건'], ['약속·검토', commitments.length, '건'], ['검수 완료', `${reviewed}/${members.length}`, '건'],
    ].map(([label, value, unit]) => node('div', {}, node('dt', {}, label), node('dd', {}, String(value), node('span', {}, unit))))));
  const toc = node('nav', { class: 'rp-toc', 'aria-label': '보고서 목차' },
    node('a', { href: `#${options.idPrefix}-focus` }, '우리 기관 관련'),
    node('a', { href: `#${options.idPrefix}-questions` }, '질의·답변'),
    node('a', { href: `#${options.idPrefix}-actions` }, '후속조치'),
    node('a', { href: `#${options.idPrefix}-special` }, '특이사항'));
  const focus = node('section', { class: 'rp-section rp-focus', id: `${options.idPrefix}-focus` },
    sectionHead('01', '우리 기관 관련 질의', s.org_name ? `${s.org_name} 관련으로 기록된 질의를 먼저 확인합니다.` : '관련 질의를 먼저 확인합니다.', orgQuestions.length),
    orgQuestions.length ? node('ul', { class: 'rp-focus-list' }, orgQuestions.map(row => {
      const member = members.find(m => m.block_id === row.block_id);
      return node('li', {}, node('a', { href: `#${options.idPrefix}-question-${row.block_id}`,
        onclick: () => { const detail = root.querySelector(`#${options.idPrefix}-question-${row.block_id}`); if (detail) detail.open = true; } },
        node('span', { class: 'rp-focus-person' }, memberName(row), node('span', {}, row.time || '')),
        node('span', {}, text(member?.one_line) || row.question || '저장된 요약이 없습니다'),
        node('span', { class: 'rp-focus-arrow', 'aria-hidden': 'true' }, '↗')),
      text(row.reason) ? node('p', { class: 'rp-focus-reason' }, row.reason) : null);
    })) : node('p', { class: 'rp-empty-note' }, '현재 보고 범위에 우리 기관 관련으로 기록된 질의가 없습니다.'));
  const questions = node('section', { class: 'rp-section', id: `${options.idPrefix}-questions` },
    sectionHead('02', '질의·답변 요지', '위원별 핵심 요지를 읽고 필요한 질의만 펼쳐 상세 답변을 확인합니다.', members.length),
    node('div', { class: 'rp-expand-tools' },
      node('button', { type: 'button', class: 'btn btn-sm', onclick: () => setReportExpanded(root, true) }, '모두 펼치기'),
      node('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => setReportExpanded(root, false) }, '요지만 보기')),
    members.length ? node('div', { class: 'rp-questions' }, members.map((row, index) => questionCard(row, index, options, actionCounts.get(row.block_id) || 0)))
      : node('p', { class: 'rp-empty-note' }, '현재 보고 범위에 저장된 질의 요약이 없습니다.'));
  const actions = node('section', { class: 'rp-section', id: `${options.idPrefix}-actions` },
    sectionHead('03', '후속조치', '저장된 요구·약속을 담당과 기한 기준으로 확인합니다.', requests.length + commitments.length),
    node('h3', { class: 'rp-subhead' }, '요구자료', node('span', {}, `${requests.length}건`)), actionTable(requests, '요구자료', options),
    node('h3', { class: 'rp-subhead' }, '약속·검토사항', node('span', {}, `${commitments.length}건`)), actionTable(commitments, '약속·검토사항', options));
  const notes = node('section', { class: 'rp-section', id: `${options.idPrefix}-special` },
    sectionHead('04', '특이사항·진행 기록', '증인·서면질의 요구와 정회·종료 등 기록입니다.', special.length),
    special.length ? node('ol', { class: 'rp-notes' }, special.map(row => node('li', {},
      node('span', { class: 'rp-note-time' }, row.time || '시각 미확인'), node('div', {},
        node('span', { class: 'rp-note-kind' }, row.label || row.kind || '기록'), node('p', {}, row.text || ''),
        row.requester ? node('span', { class: 'rp-note-meta' }, `${row.requester} 위원`) : null,
        row.due_date ? node('span', { class: 'rp-note-meta' }, `기한 ${row.due_date}`) : null,
        safeAnchor(options.sourceHref, row.utterance_id, '관련 발언')))))
      : node('p', { class: 'rp-empty-note' }, '현재 보고 범위에 특이사항이 없습니다.'));
  root.append(...[hero, s.transcription_notice ? node('p', { class: 'rp-empty-note', role: 'note' }, s.transcription_notice) : null,
    storedSessionSummary(s, options), toc, focus, questions, actions, notes,
    node('footer', { class: 'rp-review-note' }, node('strong', {}, s.review || '요약 초안 · 미검수'), node('p', {}, s.notice || ''))]
    .filter(child => child != null));
  return root;
}
