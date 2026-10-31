-- 국감 라이브 클라우드 미러·명령 큐 스키마 (Neon Postgres 16+) — 2판
-- 설계: docs/cloud/DESIGN.md, docs/cloud/API.md
--
-- 초기화는 배포 때 CLI로 한 번만 한다(앱 요청 경로에서 실행하지 않음):
--   DATABASE_URL_UNPOOLED=… python -m gukgam.cloud.store --init-schema
-- 직접 연결(UNPOOLED)을 쓰는 이유: CREATE EXTENSION·DDL은 PgBouncer transaction 모드를 거치지 않는 편이 안전하다.
-- 모든 문장은 IF NOT EXISTS라 여러 번 실행해도 안전하다.

-- ── 미러 문서 ─────────────────────────────────────────────────────────
-- data = 수집 PC serialize.* 출력 그대로. kind 별로 id 공간이 따로다.
CREATE TABLE IF NOT EXISTS docs (
  kind        TEXT        NOT NULL,           -- session|block|utterance|item|alert|summary|roster|settings_public|bookmark_set
  id          TEXT        NOT NULL,           -- utterance/block/item/alert: 수집 PC 정수 id(문자열)
                                              -- summary: '{sid}:{scope}', roster: sid, settings_public: 'global',
                                              -- bookmark_set: '{sid}:{로그인 이름}'(클라우드 자체 상태)
  sid         TEXT,                           -- 소속 세션(설정류는 NULL)
  ts          BIGINT      NOT NULL DEFAULT 0, -- 정렬 키(epoch ms). op.ts로 cloudsync가 채운다(API.md 2.2)
                                              --   utterance·block: ts_start / item: 발언 ts, 없으면 created_at
                                              --   alert: 발언 ts / session: created_at
  seq         BIGINT      NOT NULL DEFAULT 0, -- 같은 ts 안 보조 정렬(utterance.seq). 그 밖은 0
  gen         BIGINT,                         -- replace op 세대. final 조각을 받으면 같은 (kind,sid)에서 gen이 다른 행 삭제
  data        JSONB       NOT NULL,
  updated_at  BIGINT      NOT NULL,
  PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS ix_docs_kind_sid_ts ON docs (kind, sid, ts, seq);
CREATE INDEX IF NOT EXISTS ix_docs_sid ON docs (sid);
-- 검색(부분일치): utterance 본문. pg_trgm GIN 인덱스로 ILIKE 가속(Neon 지원)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS ix_docs_utt_text_trgm ON docs USING GIN ((data->>'text') gin_trgm_ops) WHERE kind = 'utterance';

-- ── SSE 재전송용 이벤트 ─────────────────────────────────────────────
-- 클라우드 자체 증가 id(수집 PC EventBus id와 무관). 브라우저 Last-Event-ID는 이 값이다.
CREATE TABLE IF NOT EXISTS events (
  id          BIGSERIAL   PRIMARY KEY,
  sid         TEXT        NOT NULL,
  type        TEXT        NOT NULL,
  data        JSONB       NOT NULL,
  created_at  BIGINT      NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_sid_id ON events (sid, id);

-- ── 터널 명령 큐 ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS commands (
  id          BIGSERIAL   PRIMARY KEY,
  request_id  TEXT        NOT NULL UNIQUE,    -- 브라우저 X-Gukgam-Request-Id(없으면 서버가 UUID 생성). 재전송 멱등
  request_hash TEXT       NOT NULL DEFAULT '', -- 입력 지문(SHA256). 본문 삭제 후에도 동일 요청 검증, 메타와 함께 7일 보존
  lane        TEXT        NOT NULL DEFAULT 'fast',        -- fast|slow(summarize·report·export)
  method      TEXT        NOT NULL,
  path        TEXT        NOT NULL,           -- 반드시 '/api/'로 시작, policy.match 통과한 것만
  query       TEXT        NOT NULL DEFAULT '',
  headers     JSONB       NOT NULL DEFAULT '{}'::jsonb,   -- content-type 등 필요한 것만
  body        BYTEA,                          -- 원본 1MB 이하. 첫 응답 후 NULL(미전달 입력도 생성 1시간 후 정리)
  actor       TEXT        NOT NULL DEFAULT '',            -- '☁{로그인 이름}'(18자 이하)
  role        TEXT        NOT NULL DEFAULT 'viewer',      -- operator|viewer
  status      TEXT        NOT NULL DEFAULT 'pending',     -- pending|taken|done|expired
  result      JSONB,                                      -- {"status","headers","body_b64"}. 완료 후 1시간 유효(만료 시 NULL)
  created_at  BIGINT      NOT NULL,
  deadline_at BIGINT      NOT NULL,           -- 빠른 레인 +25초, 느린 레인 +55초
  taken_at    BIGINT,
  done_at     BIGINT
);
-- 앞서 초기화한 2판 DB도 입력 지문을 보존하도록 비파괴적으로 보강한다.
ALTER TABLE commands ADD COLUMN IF NOT EXISTS request_hash TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS ix_commands_pending ON commands (id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_commands_created ON commands (created_at);

-- ── 수집 PC 상태(한 행) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent (
  id            INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  info          JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- boot_id, last_seq, version, mode, interval_s, queue,
                                                           -- needs_backfill, last_push_ok_ms, sessions_live
                                                           -- 하트비트는 병합(boot_id·last_seq는 apply_push만 갱신)
  heartbeat_at  BIGINT      NOT NULL DEFAULT 0,
  wanted_at     BIGINT      NOT NULL DEFAULT 0,            -- 마지막 브라우저 /api 요청(1분 디바운스)
  pruned_at     BIGINT      NOT NULL DEFAULT 0             -- 마지막 prune(10분에 1회)
);
INSERT INTO agent (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- ── 클라우드 감사 ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cloud_audit (
  id          BIGSERIAL   PRIMARY KEY,
  ts          BIGINT      NOT NULL,
  kind        TEXT        NOT NULL,           -- login_ok|login_fail|login_blocked|tunnel|reject|resync
  ip          TEXT        NOT NULL DEFAULT '',
  ua          TEXT        NOT NULL DEFAULT '',            -- 200자로 자름
  name        TEXT        NOT NULL DEFAULT '',            -- 로그인 이름(본인이 적은 값 — 신원 증명 아님)
  role        TEXT        NOT NULL DEFAULT '',
  method      TEXT        NOT NULL DEFAULT '',
  path        TEXT        NOT NULL DEFAULT '',            -- 쿼리 제외. 본문은 남기지 않는다
  status      INT         NOT NULL DEFAULT 0,
  command_id  BIGINT
);
CREATE INDEX IF NOT EXISTS ix_cloud_audit_ts ON cloud_audit (ts DESC);

-- ── 로그인 시도 제한 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS login_attempts (
  id          BIGSERIAL   PRIMARY KEY,
  ip          TEXT        NOT NULL,
  ok          BOOLEAN     NOT NULL,
  ts          BIGINT      NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_attempts_ip_ts ON login_attempts (ip, ts);
CREATE INDEX IF NOT EXISTS ix_login_attempts_ts ON login_attempts (ts);

-- ── 자주 쓰는 질의(참고) ─────────────────────────────────────────────
-- 연결 규칙(API.md 6장): psycopg.connect(DATABASE_URL, prepare_threshold=None).
-- pooled(PgBouncer transaction 모드)라 세션 SET·LISTEN/NOTIFY·PREPARE·WITH HOLD 커서·세션 advisory lock 금지.

-- push 중복 판정(apply_push 트랜잭션 첫 문장, 행 잠금)
-- SELECT info FROM agent WHERE id=1 FOR UPDATE;
--   info.boot_id <> :boot_id 이면 last_seq=0 으로 보고, :seq <= last_seq 면 duplicate

-- replace 마무리(final 조각)
-- DELETE FROM docs WHERE kind=:kind AND sid=:sid AND gen IS DISTINCT FROM :gen;

-- 명령 원자적 수령(롱폴). 한 트랜잭션 안에서만 SKIP LOCKED
-- UPDATE commands SET status='taken', taken_at=:now
--  WHERE id IN (SELECT id FROM commands WHERE status='pending' AND deadline_at > :now
--               ORDER BY id LIMIT :limit FOR UPDATE SKIP LOCKED)
--  RETURNING id, request_id, lane, method, path, query, headers, body, actor, role, created_at, deadline_at;
--  (본문 합계 3MB 초과분은 같은 트랜잭션에서 status='pending'으로 되돌린다)

-- 결과 기록(taken 일 때만)
-- UPDATE commands SET status='done', result=:result, done_at=:now WHERE id=:id AND status='taken';

-- 시간 초과(pending 일 때만 expired — 0행이면 이미 taken → 브라우저에 202)
-- UPDATE commands SET status='expired' WHERE id=:id AND status='pending';

-- 첫 응답 후 입력만 삭제(결과는 done_at + 1시간까지 동일 요청자에게 재전송)
-- UPDATE commands SET body=NULL WHERE id=:id;

-- SSE 재전송
-- SELECT id, type, data FROM events WHERE sid=:sid AND id > :last_id ORDER BY id LIMIT 2000;

-- 시도 제한(IP당 실패 5회/10분, 전체 실패 50회/10분)
-- SELECT count(*) FROM login_attempts WHERE ip=:ip AND NOT ok AND ts > :now - 600000;
-- SELECT count(*) FROM login_attempts WHERE NOT ok AND ts > :now - 600000;

-- 정리(prune, 하트비트 때 10분에 1회)
-- DELETE FROM events WHERE created_at < :now - 7*86400000;
-- DELETE FROM events e USING (SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY sid ORDER BY id DESC) rn FROM events) t WHERE rn > 5000) old
--  WHERE e.id = old.id;
-- UPDATE commands SET body=NULL WHERE created_at < :now - 3600000;   -- 오래된 입력(미전달 포함)
-- UPDATE commands SET result=NULL WHERE done_at < :now - 3600000;      -- 완료 후 1시간 지난 결과
-- DELETE FROM commands WHERE created_at < :now - 7*86400000;
-- DELETE FROM docs WHERE sid IN (SELECT id FROM docs WHERE kind='session' AND updated_at < :now - :retention_days*86400000);
-- DELETE FROM docs WHERE kind='session' AND updated_at < :now - :retention_days*86400000;
-- DELETE FROM cloud_audit WHERE ts < :now - :audit_days*86400000;
-- DELETE FROM login_attempts WHERE ts < :now - 86400000;
