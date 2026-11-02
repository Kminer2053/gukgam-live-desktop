"""SQLite 저장소: 스키마, 마이그레이션, 연결.

단일 쓰기 연결(WAL)과 독립된 읽기 전용 연결로 운영한다. 짧은 쓰기는 이벤트 루프에서
처리하고, 보정·무거운 조회는 별도 실행기와 읽기 연결을 사용한다. 쓰기 충돌은 RLock으로 직렬화한다.
시각은 모두 epoch milliseconds(INTEGER)로 저장하고 화면에서 KST 로 바꾼다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 6


def item_fingerprint_basis(*parts: str) -> str:
    """항목의 생성 입력. 구분자가 본문에 있어도 서로 다른 입력이 합쳐지지 않는다."""
    return json.dumps([re.sub(r"\s+", "", p or "") for p in parts], ensure_ascii=False, separators=(",", ":"))


def _item_fingerprint_digest(basis: str) -> str:
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def item_fingerprint(*parts: str) -> str:
    return _item_fingerprint_digest(item_fingerprint_basis(*parts))

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  committee     TEXT NOT NULL DEFAULT '',
  agencies      TEXT NOT NULL DEFAULT '',          -- 피감기관 표기(예: 한국도로공사 등)
  meeting_date  TEXT NOT NULL DEFAULT '',          -- YYYY-MM-DD
  status        TEXT NOT NULL DEFAULT 'idle',      -- idle|live|paused|ended
  source_type   TEXT NOT NULL,                     -- assembly_caption|assembly_hls|browser|file|replay|manual
  source_config TEXT NOT NULL DEFAULT '{}',
  source_state  TEXT NOT NULL DEFAULT '{}',        -- 연결상태·지연 등 휘발성 상태 스냅샷
  save_audio    INTEGER NOT NULL DEFAULT 0,
  started_at    INTEGER,
  ended_at      INTEGER,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  created_by    TEXT NOT NULL DEFAULT '',
  notes         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS roster (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT REFERENCES sessions(id) ON DELETE CASCADE,  -- NULL 이면 전역 명단
  name        TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'member',   -- chair|member|witness|agency|other
  party       TEXT NOT NULL DEFAULT '',
  title       TEXT NOT NULL DEFAULT '',         -- 직책(예: 국토교통부장관)
  aliases     TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_roster_session ON roster(session_id);

-- 수집원이 보낸 원본 조각(감사 추적·재처리용). source_key 로 upsert.
CREATE TABLE IF NOT EXISTS segments (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  source_key  TEXT NOT NULL,
  ts_start    INTEGER NOT NULL,
  ts_end      INTEGER,
  text        TEXT NOT NULL DEFAULT '',
  raw         TEXT NOT NULL DEFAULT '{}',
  is_final    INTEGER NOT NULL DEFAULT 0,
  revision    INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  UNIQUE(session_id, source_key)
);

-- 질의 블록: 위원 1인의 질의 시간 단위(위원장 진행 구간은 kind='chair')
CREATE TABLE IF NOT EXISTS blocks (
  id               INTEGER PRIMARY KEY,
  session_id       TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL,                -- question|chair|other
  questioner       TEXT NOT NULL DEFAULT '',
  questioner_party TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'ai_inferred',  -- ai_inferred|needs_name|confirmed
  name_basis       TEXT NOT NULL DEFAULT '',     -- 추정 근거(위원장 호명/자기소개/수집원/수동)
  ts_start         INTEGER NOT NULL,
  ts_end           INTEGER NOT NULL,
  is_open          INTEGER NOT NULL DEFAULT 1,
  summary          TEXT NOT NULL DEFAULT '',     -- JSON(BlockSummary)
  summary_at       INTEGER,
  review_status    TEXT NOT NULL DEFAULT 'draft',  -- draft(AI 초안)|reviewed(검수 완료)
  reviewed_by      TEXT NOT NULL DEFAULT '',
  reviewed_at      INTEGER,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_blocks_session ON blocks(session_id, ts_start);

CREATE TABLE IF NOT EXISTS utterances (
  id             INTEGER PRIMARY KEY,
  session_id     TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq            INTEGER NOT NULL,
  source_key     TEXT NOT NULL,
  block_id       INTEGER REFERENCES blocks(id) ON DELETE SET NULL,
  ts_start       INTEGER NOT NULL,
  ts_end         INTEGER,
  speaker        TEXT NOT NULL DEFAULT '',
  speaker_role   TEXT NOT NULL DEFAULT 'unknown',   -- chair|member|answer|other|unknown|system(정회·수신중단 표지)
  speaker_source TEXT NOT NULL DEFAULT 'inferred',  -- feed|inferred|manual
  text           TEXT NOT NULL DEFAULT '',
  original_text  TEXT NOT NULL DEFAULT '',          -- 수동 교정 전 원문
  speaker_hint   TEXT NOT NULL DEFAULT '',          -- 수집원이 준 화자 정보(원본)
  new_speaker    INTEGER NOT NULL DEFAULT 0,        -- 수집원이 화자 전환을 표시했는지
  is_final       INTEGER NOT NULL DEFAULT 0,
  analyzed       INTEGER NOT NULL DEFAULT 0,        -- 화자추정·규칙추출 완료 여부
  mentions       TEXT NOT NULL DEFAULT '[]',        -- 감시 키워드 적중 목록
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  UNIQUE(session_id, source_key)
);
CREATE INDEX IF NOT EXISTS ix_utt_session ON utterances(session_id, ts_start, seq);
CREATE INDEX IF NOT EXISTS ix_utt_block ON utterances(block_id);
-- 수집 이벤트마다 도는 질의가 세션 전체를 훑지 않게 하는 보조 인덱스
CREATE INDEX IF NOT EXISTS ix_utt_seq ON utterances(session_id, seq);
CREATE INDEX IF NOT EXISTS ix_utt_partial ON utterances(session_id, ts_start) WHERE is_final=0;
CREATE INDEX IF NOT EXISTS ix_utt_pending ON utterances(session_id, ts_start, seq) WHERE is_final=1 AND analyzed=0;

CREATE VIRTUAL TABLE IF NOT EXISTS utterances_fts USING fts5(
  text, speaker, content='utterances', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS utt_ai AFTER INSERT ON utterances BEGIN
  INSERT INTO utterances_fts(rowid, text, speaker) VALUES (new.id, new.text, new.speaker);
END;
CREATE TRIGGER IF NOT EXISTS utt_ad AFTER DELETE ON utterances BEGIN
  INSERT INTO utterances_fts(utterances_fts, rowid, text, speaker) VALUES ('delete', old.id, old.text, old.speaker);
END;
CREATE TRIGGER IF NOT EXISTS utt_au AFTER UPDATE OF text, speaker ON utterances BEGIN
  INSERT INTO utterances_fts(utterances_fts, rowid, text, speaker) VALUES ('delete', old.id, old.text, old.speaker);
  INSERT INTO utterances_fts(rowid, text, speaker) VALUES (new.id, new.text, new.speaker);
END;

-- 추출 항목(요구자료·약속사항·지적사항 등) = 후속조치 트래커
CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  block_id      INTEGER REFERENCES blocks(id) ON DELETE SET NULL,
  utterance_id  INTEGER REFERENCES utterances(id) ON DELETE SET NULL,
  kind          TEXT NOT NULL,      -- data_request|commitment|issue|witness_request|written_question
  strength      TEXT NOT NULL DEFAULT '',  -- 약속 강도: 확약|검토|의례 (요구자료는 자료제출|서면보고|조사)
  text          TEXT NOT NULL,      -- 요지
  quote         TEXT NOT NULL DEFAULT '',
  speaker       TEXT NOT NULL DEFAULT '',
  requester     TEXT NOT NULL DEFAULT '',   -- 요구한 위원
  assignee_dept TEXT NOT NULL DEFAULT '',
  due_date      TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'open',   -- open|in_progress|done|dismissed|superseded(재추출로 대체됨·화면 비표시)
  origin        TEXT NOT NULL DEFAULT 'rule',   -- rule|llm|manual
  confidence    REAL NOT NULL DEFAULT 0.5,
  fingerprint   TEXT NOT NULL DEFAULT '',
  fingerprint_basis TEXT NOT NULL DEFAULT '', -- 생성 입력: 사람이 요지를 고쳐도 바꾸지 않는다
  fingerprint_state TEXT NOT NULL DEFAULT '', -- ready|legacy_edited|collision
  note          TEXT NOT NULL DEFAULT '',
  edited        INTEGER NOT NULL DEFAULT 0,     -- 사람이 손댄 항목(자동 재추출·요구 위원 갱신이 덮어쓰지 않음)
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_session ON items(session_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_fp ON items(session_id, fingerprint) WHERE fingerprint <> '';

CREATE TABLE IF NOT EXISTS summaries (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  scope       TEXT NOT NULL,          -- session|rolling|report
  content     TEXT NOT NULL,          -- JSON
  provider    TEXT NOT NULL DEFAULT '',
  model       TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_summaries_session ON summaries(session_id, scope, created_at);

CREATE TABLE IF NOT EXISTS alerts (
  id           INTEGER PRIMARY KEY,
  session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  utterance_id INTEGER REFERENCES utterances(id) ON DELETE CASCADE,
  keyword      TEXT NOT NULL,
  category     TEXT NOT NULL DEFAULT 'agency',
  ack          INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL,
  UNIQUE(utterance_id, keyword)
);

CREATE TABLE IF NOT EXISTS bookmarks (
  id           INTEGER PRIMARY KEY,
  session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  utterance_id INTEGER NOT NULL REFERENCES utterances(id) ON DELETE CASCADE,
  note         TEXT NOT NULL DEFAULT '',
  created_by   TEXT NOT NULL DEFAULT '',
  created_at   INTEGER NOT NULL,
  UNIQUE(utterance_id, created_by)
);

CREATE TABLE IF NOT EXISTS settings (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id      INTEGER PRIMARY KEY,
  ts      INTEGER NOT NULL,
  actor   TEXT NOT NULL DEFAULT '',
  action  TEXT NOT NULL,
  target  TEXT NOT NULL DEFAULT '',
  detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);

-- 화자 검토 초안과 채택 이력. 발언 원문·근거 UID는 그대로 보존한다.
CREATE TABLE IF NOT EXISTS transcript_reviews (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  block_id INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_review_block ON transcript_reviews(session_id, block_id, id);
CREATE TABLE IF NOT EXISTS audio_review_evidence (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  utterance_id INTEGER REFERENCES utterances(id) ON DELETE CASCADE,
  payload TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audio_evidence_session ON audio_review_evidence(session_id,id);

-- 원수신 자막과 세 단계 파생 문서를 기존 발언·사람 수정과 분리한다.
CREATE TABLE IF NOT EXISTS caption_sources (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  source_key TEXT NOT NULL,
  segment TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  first_rx INTEGER NOT NULL,
  last_rx INTEGER NOT NULL,
  stable_at INTEGER NOT NULL,
  final INTEGER NOT NULL,
  scd INTEGER NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(session_id,source_key)
);
CREATE INDEX IF NOT EXISTS ix_caption_source_session ON caption_sources(session_id,id);
CREATE TABLE IF NOT EXISTS caption_source_revisions (
  source_id INTEGER NOT NULL REFERENCES caption_sources(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  payload TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  PRIMARY KEY(source_id,revision)
);
CREATE TABLE IF NOT EXISTS transcript_stage_documents (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stage_document_session ON transcript_stage_documents(session_id,id);
CREATE TABLE IF NOT EXISTS transcript_stage_sources (
  document_id INTEGER NOT NULL REFERENCES transcript_stage_documents(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES caption_sources(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  is_target INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(document_id,source_id)
);
CREATE INDEX IF NOT EXISTS ix_stage_source_documents ON transcript_stage_sources(source_id,document_id);

-- 화면 조회는 이 저장 판본을 읽기만 한다. 입력 변경과 계산 완료 세대가 다르면 대기한다.
CREATE TABLE IF NOT EXISTS display_state (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  input_generation INTEGER NOT NULL DEFAULT 1,
  built_generation INTEGER NOT NULL DEFAULT 0,
  built_at INTEGER
);
CREATE TABLE IF NOT EXISTS display_changes (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  generation INTEGER NOT NULL,
  kind TEXT NOT NULL,
  row_key TEXT NOT NULL,
  block_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_display_changes_session ON display_changes(session_id,generation);
CREATE TABLE IF NOT EXISTS display_materializations (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  generation INTEGER NOT NULL,
  payload TEXT NOT NULL,
  built_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS display_values (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  row_key TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  payload TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  PRIMARY KEY(session_id,kind,row_key)
);
CREATE INDEX IF NOT EXISTS ix_display_values_order ON display_values(session_id,kind,sort_order);
CREATE TABLE IF NOT EXISTS display_stage2_rows (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  row_key TEXT NOT NULL,
  payload TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  generation INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(session_id,kind,row_key)
);
CREATE TABLE IF NOT EXISTS display_stage2_revisions (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  row_key TEXT NOT NULL,
  generation INTEGER NOT NULL,
  action TEXT NOT NULL,
  payload TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_display_stage2_history ON display_stage2_revisions(session_id,kind,row_key,id);
CREATE TABLE IF NOT EXISTS display_document_cache (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  document_id INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(session_id,document_id)
);
"""


# 기존 DB 에 없는 열을 더한다: (테이블, 열, 정의, 열을 새로 만든 직후 한 번 돌릴 보정 SQL)
COLUMN_MIGRATIONS: list[tuple[str, str, str, str | None]] = [
    ("utterances", "source_order", "TEXT NOT NULL DEFAULT ''", None),
    ("utterances", "source_speaker_hint", "TEXT NOT NULL DEFAULT ''", None),
    ("utterances", "turn_id", "TEXT NOT NULL DEFAULT ''", None),
    ("utterances", "boundary_reason", "TEXT NOT NULL DEFAULT ''", None),
    ("utterances", "review_parts", "TEXT NOT NULL DEFAULT '[]'", None),
    # 이전 판에서 사람이 고친 것으로 볼 수 있는 항목(수동 생성·메모·수정 이력)은 '사람이 손댄 항목'으로 보존한다
    ("items", "edited", "INTEGER NOT NULL DEFAULT 0",
     "UPDATE items SET edited=1 WHERE origin='manual' OR note<>'' OR updated_at<>created_at"),
    ("items", "fingerprint_basis", "TEXT NOT NULL DEFAULT ''", None),
    ("items", "fingerprint_state", "TEXT NOT NULL DEFAULT ''", None),
]


def _migrate_item_fingerprints(connection: sqlite3.Connection) -> None:
    """기존 지문만 교체한다. 충돌·복구 불가 수동 입력은 행을 보존하고 자동 대체를 보류한다."""
    groups = {}
    for row in connection.execute("SELECT * FROM items WHERE fingerprint<>'' ORDER BY id").fetchall():
        basis = item_fingerprint_basis(row["kind"], str(row["utterance_id"] or ""), row["quote"] or row["text"])
        digest = _item_fingerprint_digest(basis)
        plan = {"id": row["id"], "basis": basis, "digest": digest,
                "legacy_edited": bool(row["edited"] or row["note"] or row["origin"] == "manual")}
        groups.setdefault((row["session_id"], digest), []).append(plan)
    # UNIQUE 인덱스의 이전 값과 새 값이 교차하더라도 전체 교체는 한 트랜잭션 안에서만 보인다.
    connection.execute("UPDATE items SET fingerprint='' WHERE fingerprint<>''")
    for group in groups.values():
        collision = len(group) > 1
        for plan in group:
            state = "legacy_edited" if plan["legacy_edited"] else "collision" if collision else "ready"
            value = plan["digest"] + (":" + str(plan["id"]) if collision else "")
            connection.execute("UPDATE items SET fingerprint=?,fingerprint_basis=?,fingerprint_state=? WHERE id=?",
                               (value, plan["basis"], state, plan["id"]))


def _display_trigger_schema() -> str:
    """입력 행의 실제 변경만 기록한다. 파생 표시 테이블은 다시 더럽히지 않는다."""
    statements = []
    def changed(table, operation, sid, key, fields=(), block="NULL", condition=None):
        when = condition or (" OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in fields)
                             if operation == "UPDATE" else "1")
        # 삭제 cascade 중에는 세션 자체가 이미 사라질 수 있다.
        live = f"EXISTS(SELECT 1 FROM sessions WHERE id={sid} AND (source_type='assembly_caption' OR EXISTS(SELECT 1 FROM caption_sources WHERE session_id={sid}) OR EXISTS(SELECT 1 FROM display_state WHERE session_id={sid})))"
        body = (f"INSERT INTO display_state(session_id,input_generation,built_generation) SELECT {sid},1,0 WHERE {live} "
                "ON CONFLICT(session_id) DO UPDATE SET input_generation=input_generation+1;"
                f"INSERT INTO display_changes(session_id,generation,kind,row_key,block_id) "
                f"SELECT {sid},input_generation,'{table}',CAST({key} AS TEXT),{block} FROM display_state WHERE session_id={sid};")
        statements.append(f"CREATE TRIGGER IF NOT EXISTS display_{table}_{operation.lower()} AFTER {operation} ON {table} "
                          f"WHEN {when} BEGIN {body} END;")
    definitions = {
        "utterances": ("id", "text original_text speaker speaker_role speaker_source block_id ts_start ts_end seq source_key is_final analyzed source_order source_speaker_hint turn_id boundary_reason review_parts mentions".split()),
        "segments": ("source_key", "text raw is_final revision ts_start ts_end".split()),
        "blocks": ("id", "kind questioner questioner_party status name_basis ts_start ts_end is_open summary summary_at review_status reviewed_by reviewed_at".split()),
        "caption_sources": ("id", "revision payload final scd stable_at first_rx last_rx source_key segment".split()),
        "transcript_stage_documents": ("id", "input_hash status payload".split()),
        "transcript_reviews": ("id", "status payload block_id".split()),
    }
    for table, (key, fields) in definitions.items():
        fields=list(dict.fromkeys(["session_id",key,*fields]))
        for operation in ("INSERT", "UPDATE", "DELETE"):
            side = "OLD" if operation == "DELETE" else "NEW"
            block = f"{side}.block_id" if table in {"utterances", "transcript_reviews"} else f"{side}.id" if table == "blocks" else "NULL"
            changed(table, operation, f"{side}.session_id", f"{side}.{key}", fields, block)
        # 이전 블록이나 세션의 검증 캐시도 재귀속 뒤 무효화한다.
        if table == "utterances":
            statements.append("CREATE TRIGGER IF NOT EXISTS display_utterances_old_owner AFTER UPDATE ON utterances "
                "WHEN OLD.session_id IS NOT NEW.session_id OR OLD.block_id IS NOT NEW.block_id OR OLD.id IS NOT NEW.id BEGIN "
                "INSERT INTO display_state(session_id,input_generation,built_generation) SELECT OLD.session_id,1,0 "
                "WHERE EXISTS(SELECT 1 FROM sessions WHERE id=OLD.session_id AND (source_type='assembly_caption' "
                "OR EXISTS(SELECT 1 FROM caption_sources WHERE session_id=OLD.session_id) OR EXISTS(SELECT 1 FROM display_state WHERE session_id=OLD.session_id))) ON CONFLICT(session_id) DO UPDATE SET input_generation=input_generation+1; "
                "INSERT INTO display_changes(session_id,generation,kind,row_key,block_id) "
                "SELECT OLD.session_id,input_generation,'utterances',CAST(OLD.id AS TEXT),OLD.block_id FROM display_state WHERE session_id=OLD.session_id; END;")
        else:
            # 세션이나 식별 키를 바꾼 입력도 옛 연결에서 철회되어야 한다.
            statements.append(f"CREATE TRIGGER IF NOT EXISTS display_{table}_old_session AFTER UPDATE ON {table} "
                f"WHEN OLD.session_id IS NOT NEW.session_id OR OLD.{key} IS NOT NEW.{key} BEGIN "
                "INSERT INTO display_state(session_id,input_generation,built_generation) SELECT OLD.session_id,1,0 "
                "WHERE EXISTS(SELECT 1 FROM sessions WHERE id=OLD.session_id AND (source_type='assembly_caption' "
                "OR EXISTS(SELECT 1 FROM caption_sources WHERE session_id=OLD.session_id) OR EXISTS(SELECT 1 FROM display_state WHERE session_id=OLD.session_id))) "
                "ON CONFLICT(session_id) DO UPDATE SET input_generation=input_generation+1; "
                f"INSERT INTO display_changes(session_id,generation,kind,row_key) SELECT OLD.session_id,input_generation,'{table}',CAST(OLD.{key} AS TEXT) "
                "FROM display_state WHERE session_id=OLD.session_id; END;")
    for operation in ("INSERT", "UPDATE"):
        changed("sessions", operation, "NEW.id", "NEW.id", "title committee agencies meeting_date source_type status started_at ended_at".split())
    # 명단의 전역 행은 모든 세션에, 개별 행은 해당 세션에만 영향을 준다.
    for operation in ("INSERT", "UPDATE", "DELETE"):
        side = "OLD" if operation == "DELETE" else "NEW"
        condition = "1" if operation != "UPDATE" else " OR ".join(f"OLD.{k} IS NOT NEW.{k}" for k in ("id","session_id","name","role","party","title","aliases"))
        scope = f"({side}.session_id IS NULL OR id={side}.session_id" + (" OR OLD.session_id IS NULL OR id=OLD.session_id)" if operation == "UPDATE" else ")")
        scope += " AND (source_type='assembly_caption' OR EXISTS(SELECT 1 FROM caption_sources c WHERE c.session_id=sessions.id) "
        scope += "OR EXISTS(SELECT 1 FROM display_state d WHERE d.session_id=sessions.id))"
        statements.append(f"CREATE TRIGGER IF NOT EXISTS display_roster_{operation.lower()} AFTER {operation} ON roster WHEN {condition} BEGIN "
            f"INSERT INTO display_state(session_id,input_generation,built_generation) SELECT id,1,0 FROM sessions WHERE {scope} "
            "ON CONFLICT(session_id) DO UPDATE SET input_generation=input_generation+1; "
            f"INSERT INTO display_changes(session_id,generation,kind,row_key) SELECT session_id,input_generation,'roster',CAST({side}.id AS TEXT) "
            f"FROM display_state WHERE {side}.session_id IS NULL OR session_id={side}.session_id"
            + (" OR OLD.session_id IS NULL OR session_id=OLD.session_id" if operation == "UPDATE" else "") + "; END;")
    for operation in ("INSERT", "UPDATE", "DELETE"):
        side = "OLD" if operation == "DELETE" else "NEW"
        sid = f"(SELECT session_id FROM transcript_stage_documents WHERE id={side}.document_id)"
        changed("transcript_stage_sources",operation,sid,f"{side}.document_id",
                ("document_id","source_id","revision","is_target"))
    statements.append("CREATE TRIGGER IF NOT EXISTS display_stage_sources_old_owner AFTER UPDATE ON transcript_stage_sources "
        "WHEN OLD.document_id IS NOT NEW.document_id BEGIN "
        "UPDATE display_state SET input_generation=input_generation+1 WHERE session_id=(SELECT session_id FROM transcript_stage_documents WHERE id=OLD.document_id); "
        "INSERT INTO display_changes(session_id,generation,kind,row_key) SELECT session_id,input_generation,'transcript_stage_sources',CAST(OLD.document_id AS TEXT) "
        "FROM display_state WHERE session_id=(SELECT session_id FROM transcript_stage_documents WHERE id=OLD.document_id); END;")
    for operation in ("INSERT", "UPDATE", "DELETE"):
        side = "OLD" if operation == "DELETE" else "NEW"
        condition = (f"({side}.key IN ('transcript_review','llm') OR OLD.key IN ('transcript_review','llm')) "
            "AND (OLD.value IS NOT NEW.value OR OLD.key IS NOT NEW.key)" if operation == "UPDATE"
            else f"{side}.key IN ('transcript_review','llm')")
        statements.append(f"CREATE TRIGGER IF NOT EXISTS display_settings_{operation.lower()} AFTER {operation} ON settings WHEN {condition} BEGIN "
            "INSERT INTO display_state(session_id,input_generation,built_generation) SELECT id,1,0 FROM sessions WHERE "
            "source_type='assembly_caption' OR EXISTS(SELECT 1 FROM caption_sources c WHERE c.session_id=sessions.id) "
            "OR EXISTS(SELECT 1 FROM display_state d WHERE d.session_id=sessions.id) "
            "ON CONFLICT(session_id) DO UPDATE SET input_generation=input_generation+1; "
            f"INSERT INTO display_changes(session_id,generation,kind,row_key) SELECT session_id,input_generation,'settings',{side}.key FROM display_state; END;")
    statements.append("INSERT INTO display_state(session_id,input_generation,built_generation) SELECT id,1,0 FROM sessions "
        "WHERE source_type='assembly_caption' OR EXISTS(SELECT 1 FROM caption_sources c WHERE c.session_id=sessions.id) ON CONFLICT(session_id) DO NOTHING;")
    # 같은 스키마 버전의 보완판도 새 감시 필드를 빠뜨리지 않게 정의만 교체한다.
    return "\n".join((f"DROP TRIGGER IF EXISTS {match[1]};\n" if
        (match:=re.match(r"CREATE TRIGGER IF NOT EXISTS ([a-z_]+)",statement)) else "")+statement for statement in statements)


def now_ms() -> int:
    return int(time.time() * 1000)


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._change_epoch = 0
        self._target = (f"file:gukgam-{uuid.uuid4().hex}?mode=memory&cache=shared"
                        if self.path == ":memory:" else self.path)
        self._conn = sqlite3.connect(self._target, uri=self.path == ":memory:",
                                     check_same_thread=False, isolation_level=None)
        self._conn.row_factory = _dict_factory
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        try:
            self.migrate()
        except BaseException:
            self._conn.close()
            raise

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            for table, column, ddl, backfill in COLUMN_MIGRATIONS:
                cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column in cols:
                    continue
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                if backfill:
                    self._conn.execute(backfill)
            self._conn.executescript(_display_trigger_schema())
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
                version = int(json.loads(row["value"])) if row else 0
                if version < 6:
                    _migrate_item_fingerprints(self._conn)
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_items_basis ON items(session_id,fingerprint_basis) "
                    "WHERE fingerprint<>''")
                self._conn.execute(
                    "INSERT INTO settings(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(max(version, SCHEMA_VERSION)),))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def change_epoch(self) -> int:
        """같은 연결의 쓰기 세대. 보정 검증과 최종 저장 사이의 변경을 감지한다."""
        with self._lock:
            return self._change_epoch

    def change_token(self) -> tuple[int, int]:
        """같은 연결의 쓰기와 다른 연결의 커밋을 함께 구분한다."""
        with self._lock:
            return self._change_epoch, self._conn.execute("PRAGMA data_version").fetchone()["data_version"]

    @contextmanager
    def read_connection(self) -> Iterator[ReadDatabase]:
        """수신 쓰기 연결·잠금을 공유하지 않는 보정/조회 전용 읽기 연결."""
        reader = ReadDatabase(self._target, memory=self.path == ":memory:")
        try:
            yield reader
        finally:
            reader.close()

    # --- 기본 연산 -------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            before = self._conn.total_changes
            try:
                return self._conn.execute(sql, params)
            finally:
                if self._conn.total_changes != before:
                    self._change_epoch += 1

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def insert(self, sql: str, params: tuple | dict = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._change_epoch += 1
            return int(cur.lastrowid)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            before = self._conn.total_changes
            nested = self._conn.in_transaction
            point = "gukgam_" + uuid.uuid4().hex
            self._conn.execute("SAVEPOINT " + point if nested else "BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                if nested:
                    self._conn.execute("ROLLBACK TO " + point)
                    self._conn.execute("RELEASE " + point)
                else:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("RELEASE " + point if nested else "COMMIT")
            finally:
                if self._conn.total_changes != before:
                    self._change_epoch += 1


class ReadDatabase:
    """읽기 전용 연결. 모델 대기 중에는 트랜잭션을 열어 두지 않는다."""

    def __init__(self, target: str, *, memory: bool = False):
        self.path = target
        uri = target if memory else Path(target).resolve().as_uri() + "?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = _dict_factory
        self._conn.execute("PRAGMA query_only=ON")
        self._conn.execute("PRAGMA busy_timeout=250")

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        finally:
            self._conn.execute("ROLLBACK")

    def close(self) -> None:
        self._conn.close()
