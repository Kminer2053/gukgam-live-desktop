"""모든 입력·DB는 합성이다. 운영 앱·수집·AI 제공자는 시작하지 않는다."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from gukgam import db as db_module
from gukgam.db import COLUMN_MIGRATIONS, SCHEMA, Database
from gukgam.llm.service import AnalysisService
from gukgam.nlp.rules import ExtractedItem
from gukgam.pipeline import SessionPipeline
from gukgam.repo import Repo, fingerprint


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, *event):
        self.events.append(event)


def seed_legacy(path, rows):
    """v5 스키마의 불투명한 16자리 지문을 생성한다. 이전 해시를 실행하지 않는다."""
    schema = "\n".join(line for line in SCHEMA.splitlines()
                       if not line.lstrip().startswith(("fingerprint_basis ", "fingerprint_state ")))
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
        for table, column, ddl, _backfill in COLUMN_MIGRATIONS:
            if column in {"fingerprint_basis", "fingerprint_state"}:
                continue
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        connection.execute("INSERT INTO settings VALUES('schema_version','5')")
        connection.execute("INSERT INTO sessions(id,title,source_type,created_at,updated_at) VALUES('s','합성 기록','manual',10,10)")
        for bid in (1, 2):
            connection.execute("INSERT INTO blocks(id,session_id,kind,questioner,ts_start,ts_end,review_status,reviewed_by,reviewed_at,created_at,updated_at) "
                               "VALUES(?,'s','question','질의자',10,20,'reviewed','합성 검토자',30,10,20)", (bid,))
        for uid in (0, 1, 2):
            connection.execute("INSERT INTO utterances(id,session_id,seq,source_key,block_id,ts_start,text,original_text,is_final,created_at,updated_at) "
                               "VALUES(?,'s',?,?,1,10,'합성 원문 발언','보존된 합성 원문',1,10,20)", (uid, uid + 1, str(uid)))
        connection.execute("INSERT INTO transcript_reviews(session_id,block_id,input_hash,status,payload,created_at,updated_at) "
                           "VALUES('s',1,'synthetic','accepted','{\"reviewed\":true}',10,20)")
        for index, patch in enumerate(rows, 1):
            row = dict(id=index, session_id="s", block_id=1, utterance_id=1, kind="data_request", text="자료 요지",
                       quote="자료를 제출해 주세요", speaker="합성 화자", requester="질의자", assignee_dept="담당팀",
                       due_date="2030-01-01", status="open", origin="rule", confidence=0.7,
                       fingerprint=f"{index:016x}", edited=0, note="", created_at=10, updated_at=20)
            row.update(patch)
            columns = ",".join(row)
            connection.execute(f"INSERT INTO items({columns}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
                for table in ("items", "utterances", "blocks", "transcript_reviews", "sessions")}


def assert_preserved(database, before):
    for table, original in before.items():
        current = database.query(f"SELECT * FROM {table} ORDER BY id")
        if table == "items":
            excluded = {"fingerprint", "fingerprint_basis", "fingerprint_state"}
            current = [{key: value for key, value in row.items() if key not in excluded} for row in current]
            original = [{key: value for key, value in row.items() if key != "fingerprint"} for row in original]
        assert current == original


def rule_refresh(repo, uid=1, *, text="자료 요지", quote="자료를 제출해 주세요"):
    pipeline = SessionPipeline(repo, Bus(), "s")
    pipeline._rule_items = lambda _utterance: [ExtractedItem(kind="data_request", text=text, quote=quote)]
    return pipeline._reextract_rules(repo.get_utterance(uid))


def llm_refresh(repo, *, text="자료 요지", uid=1, bid=1):
    service = AnalysisService(SimpleNamespace(repo=repo, bus=Bus()))
    summary = {"data_requests": [{"text": text, "utterance_id": uid}]}
    service._sync_llm_items(repo.get_block(bid), summary, [repo.get_utterance(uid)] if uid is not None else [], [])


def test_full_sha256_unambiguous_and_whitespace_compatible():
    assert len(fingerprint("자료", "1", "제출")) == 64
    assert fingerprint("자료", "1", "자료 제출") == fingerprint("자료", "1", "자료\n제출")
    assert fingerprint("a|b", "c") != fingerprint("a", "b|c")


def test_migrated_dedup_lookup_uses_index_and_reopening_repairs_missing_index(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [{}])
    database = Database(path)
    try:
        before = database.query("SELECT * FROM items")
        plan = database.query(
            "EXPLAIN QUERY PLAN SELECT * FROM items WHERE session_id=? AND fingerprint_basis=? "
            "AND fingerprint<>'' ORDER BY id LIMIT 1", ("s", before[0]["fingerprint_basis"]))
        assert any("ix_items_basis" in row["detail"] for row in plan)
        database.execute("DROP INDEX ix_items_basis")
        database.migrate()
        assert database.query("SELECT * FROM items") == before
        assert database.query_one("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_items_basis'")
    finally:
        database.close()


def test_migration_preserves_all_content_human_and_review_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    before = seed_legacy(path, [{}, {"origin": "manual", "edited": 1, "quote": "", "text": "사람이 바꾼 요지", "note": "메모", "status": "done"},
                                {"fingerprint": "", "origin": "manual", "edited": 1},
                                {"fingerprint": "", "status": "superseded"}])
    database = Database(path)
    try:
        assert_preserved(database, before)
        assert json.loads(database.query_one("SELECT value FROM settings WHERE key='schema_version'")["value"]) == 6
        rows = database.query("SELECT * FROM items ORDER BY id")
        assert rows[0]["fingerprint"] == fingerprint(rows[0]["kind"], "1", rows[0]["quote"])
        assert rows[1]["fingerprint_state"] == "legacy_edited"
        assert [row["fingerprint"] for row in rows[2:]] == ["", ""]
        assert [row["fingerprint_basis"] for row in rows[2:]] == ["", ""]
    finally:
        database.close()


def test_reopen_is_idempotent_even_after_human_edit(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [{}])
    database = Database(path)
    repo = Repo(database)
    repo.update_item(1, by_human=True, text="새 수동 요지", quote="새 수동 인용")
    before = database.query("SELECT * FROM items")
    database.migrate()
    assert database.query("SELECT * FROM items") == before
    database.close()
    reopened = Database(path)
    try:
        assert reopened.query("SELECT * FROM items") == before
        row, created = Repo(reopened).add_item("s", kind="data_request", utterance_id=1, text="자료 요지", quote="자료를 제출해 주세요")
        assert not created and row["id"] == 1 and row["text"] == "새 수동 요지"
    finally:
        reopened.close()


def test_same_legacy_rule_reextract_keeps_id_and_does_not_supersede(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    before = seed_legacy(path, [{}])
    database = Database(path)
    try:
        assert not rule_refresh(Repo(database))
        assert len(database.query("SELECT * FROM items")) == 1
        assert_preserved(database, before)
    finally:
        database.close()


def test_same_legacy_llm_resync_keeps_id_and_does_not_supersede(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [{"origin": "llm", "quote": ""}])
    database = Database(path)
    try:
        llm_refresh(Repo(database))
        rows = database.query("SELECT * FROM items")
        assert len(rows) == 1 and rows[0]["id"] == 1 and rows[0]["status"] == "open"
    finally:
        database.close()


@pytest.mark.parametrize("origin", ["rule", "llm", "manual"])
def test_legacy_edited_empty_quote_keeps_unknown_original_candidate(tmp_path, origin):
    path = tmp_path / "legacy.sqlite3"
    before = seed_legacy(path, [{"origin": origin, "edited": 1, "quote": "", "text": "사람이 전혀 다르게 고친 요지"}])
    database = Database(path)
    try:
        repo = Repo(database)
        row, created = repo.add_item("s", kind="data_request", utterance_id=1, text="생성 당시 원래 추출 내용", origin="llm")
        assert not created and row["id"] == 1
        rule_refresh(repo, text="생성 당시 원래 추출 내용", quote="")
        llm_refresh(repo, text="생성 당시 원래 추출 내용")
        assert_preserved(database, before)
        assert len(database.query("SELECT * FROM items")) == 1
    finally:
        database.close()


def test_null_uid_protection_is_block_scoped_not_session_wide(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [{"utterance_id": None, "origin": "manual", "edited": 1, "quote": "", "text": "수동 요지"}])
    database = Database(path)
    try:
        repo = Repo(database)
        same, created = repo.add_item("s", kind="data_request", text="이전 자동 후보", block_id=1, origin="llm")
        assert not created and same["id"] == 1
        other, created = repo.add_item("s", kind="data_request", text="다른 질의 후보", block_id=2, origin="llm")
        assert created and other["id"] != 1
        manual, created = repo.add_item("s", kind="data_request", text="별도 수동 항목", block_id=1, origin="manual")
        assert created and manual["edited"] == 1
    finally:
        database.close()


@pytest.mark.parametrize("protection", [{"edited": 1}, {"note": "사람이 남긴 메모"}, {"origin": "manual"}])
def test_legacy_changed_nonempty_quote_cannot_be_treated_as_creation_input(tmp_path, protection):
    path = tmp_path / "legacy.sqlite3"
    before = seed_legacy(path, [{"quote": "사람이 바꾼 인용", "text": "사람이 바꾼 요지", **protection}])
    database = Database(path)
    try:
        repo = Repo(database)
        assert repo.get_item(1)["fingerprint_state"] == "legacy_edited"
        row, created = repo.add_item("s", kind="data_request", utterance_id=1,
                                     text="생성 당시 요지", quote="생성 당시 인용", origin="rule")
        assert not created and row["id"] == 1
        rule_refresh(repo, text="생성 당시 요지", quote="생성 당시 인용")
        llm_refresh(repo, text="생성 당시 요지")
        assert_preserved(database, before)
        assert len(database.query("SELECT * FROM items")) == 1
    finally:
        database.close()


def test_missing_uid_and_block_fails_without_arbitrary_merging(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    before = seed_legacy(path, [{"utterance_id": None, "block_id": None, "origin": "manual", "edited": 1, "quote": ""}])
    database = Database(path)
    try:
        with pytest.raises(ValueError, match="연결 근거"):
            Repo(database).add_item("s", kind="data_request", text="다른 자동 후보", origin="llm")
        assert_preserved(database, before)
    finally:
        database.close()


@pytest.mark.parametrize("edited_empty_quote", [False, True])
def test_legacy_converged_duplicates_are_preserved_and_held(tmp_path, edited_empty_quote):
    path = tmp_path / "legacy.sqlite3"
    changes = [{"edited": 1, "quote": ""}, {"edited": 1, "quote": ""}] if edited_empty_quote else [{}, {"text": "다른 보관 요지"}]
    before = seed_legacy(path, changes)
    database = Database(path)
    try:
        repo = Repo(database)
        rows = database.query("SELECT * FROM items ORDER BY id")
        assert {row["fingerprint_state"] for row in rows} == ({"legacy_edited"} if edited_empty_quote else {"collision"})
        assert len({row["fingerprint"] for row in rows}) == 2
        rule_refresh(repo)
        llm_refresh(repo)
        repo.supersede_items([1, 2])
        assert repo.delete_items_for_block(1, "rule") == []
        assert_preserved(database, before)
    finally:
        database.close()


def test_forced_digest_collision_migration_never_merges_different_inputs(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    before = seed_legacy(path, [{}, {"quote": "다른 근거 발언", "text": "다른 요구"}])
    monkeypatch.setattr(db_module, "_item_fingerprint_digest", lambda _basis: "c" * 64)
    database = Database(path)
    try:
        repo = Repo(database)
        one, new_one = repo.add_item("s", kind="data_request", utterance_id=1, text="자료 요지", quote="자료를 제출해 주세요")
        two, new_two = repo.add_item("s", kind="data_request", utterance_id=1, text="다른 요구", quote="다른 근거 발언")
        assert not new_one and not new_two and (one["id"], two["id"]) == (1, 2)
        assert_preserved(database, before)
    finally:
        database.close()


def test_new_digest_collision_uses_identity_and_preserves_both_rows(monkeypatch):
    monkeypatch.setattr(db_module, "_item_fingerprint_digest", lambda _basis: "d" * 64)
    database = Database(":memory:")
    try:
        repo = Repo(database)
        repo.create_session(title="합성", source_type="manual", session_id="s")
        one, created_one = repo.add_item("s", kind="issue", text="첫 요지")
        two, created_two = repo.add_item("s", kind="issue", text="다른 요지")
        again, created_again = repo.add_item("s", kind="issue", text="다른 요지")
        assert created_one and created_two and not created_again
        assert one["id"] != two["id"] == again["id"]
        assert one["fingerprint"] != two["fingerprint"]
        assert len(database.query("SELECT * FROM items")) == 2
    finally:
        database.close()


def test_none_and_zero_uid_keep_previous_normalization(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [{"utterance_id": None}])
    database = Database(path)
    try:
        row, created = Repo(database).add_item("s", kind="data_request", utterance_id=0, text="資料", quote="자료를 제출해 주세요")
        assert not created and row["id"] == 1
    finally:
        database.close()


def test_failed_migration_rolls_back_items_and_version_then_retries(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [{}])
    original = db_module._migrate_item_fingerprints

    def fail_after_updates(connection):
        original(connection)
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(db_module, "_migrate_item_fingerprints", fail_after_updates)
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT fingerprint FROM items").fetchone()[0] == "0000000000000001"
        assert connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "5"
    monkeypatch.setattr(db_module, "_migrate_item_fingerprints", original)
    database = Database(path)
    try:
        assert len(database.query_one("SELECT fingerprint FROM items")["fingerprint"]) == 64
    finally:
        database.close()


def test_add_item_remains_atomic_inside_existing_transaction():
    database = Database(":memory:")
    try:
        repo = Repo(database)
        repo.create_session(title="합성", source_type="manual", session_id="s")
        with pytest.raises(RuntimeError):
            with database.transaction():
                repo.add_item("s", kind="issue", text="롤백 대상")
                raise RuntimeError("rollback")
        assert database.query("SELECT * FROM items") == []
    finally:
        database.close()


def test_migration_does_not_dirty_display_generation_or_review_cache(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [{}])
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO display_state(session_id,input_generation,built_generation,built_at) VALUES('s',7,7,30)")
        before = connection.execute("SELECT * FROM display_state").fetchall()
    database = Database(path)
    try:
        current = database.query("SELECT * FROM display_state")
        assert [tuple(row.values()) for row in current] == before
        assert database.query("SELECT * FROM display_changes") == []
        assert database.query_one("SELECT status,payload FROM transcript_reviews") == {"status": "accepted", "payload": '{"reviewed":true}'}
    finally:
        database.close()


def test_live_llm_resync_disambiguates_new_hash_collision_by_basis(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [])
    monkeypatch.setattr(db_module, "_item_fingerprint_digest", lambda _basis: "e" * 64)
    database = Database(path)
    try:
        repo = Repo(database)
        entries = [{"text": "예산 집행 내역 제출", "utterance_id": 1}, {"text": "시설 점검 결과 보고", "utterance_id": 1}]
        ids = [repo.add_item("s", kind="data_request", text=row["text"], utterance_id=1, block_id=1, origin="llm")[0]["id"] for row in entries]
        service = AnalysisService(SimpleNamespace(repo=repo, bus=Bus()))
        service._sync_llm_items(repo.get_block(1), {"data_requests": entries}, [repo.get_utterance(1)], [])
        rows = database.query("SELECT id,status FROM items ORDER BY id")
        assert rows == [{"id": iid, "status": "open"} for iid in ids]
    finally:
        database.close()


def test_normal_changed_rule_still_supersedes_only_old_automatic_item(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    seed_legacy(path, [{}])
    database = Database(path)
    try:
        assert rule_refresh(Repo(database), text="다른 요구", quote="다른 근거")
        rows = database.query("SELECT * FROM items ORDER BY id")
        assert len(rows) == 2 and rows[0]["status"] == "superseded" and rows[1]["status"] == "open"
    finally:
        database.close()
