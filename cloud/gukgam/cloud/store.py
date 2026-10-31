"""클라우드 문서·이벤트·명령 저장소(SQLite 개발용, MongoDB·Postgres 운영용)."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import groupby
from pathlib import Path
from typing import Any, Iterator, Protocol

DAY_MS = 86_400_000
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "cloud" / "schema.sql"


def now_ms() -> int:
    """저장 시각은 epoch 밀리초로 통일한다."""
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def command_fingerprint(method: str, path: str, query: str, content_type: str, body: bytes) -> str:
    """입력을 지운 뒤에도 메서드·경로·쿼리·허용 본문 형식·원본문이 같은지 확인한다."""
    parts = [method.strip().upper().encode("utf-8"), path.encode("utf-8"), query.encode("utf-8"),
             content_type.encode("utf-8"), bytes(body)]
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


class CloudStore(Protocol):
    """클라우드 앱이 사용하는 저장소 계약."""

    def init_schema(self) -> None: ...
    def request_scope(self) -> Any: ...
    def close(self) -> None: ...
    def apply_push(self, boot_id: str, seq: int, ops: list[dict]) -> dict: ...
    def heartbeat(self, info: dict) -> dict: ...
    def agent_status(self) -> dict: ...
    def mark_wanted(self) -> None: ...
    def wanted(self) -> bool: ...
    def get_doc(self, kind: str, id: str) -> dict | None: ...
    def doc_rows(self, kind: str, sid: str | None = None, *, limit: int = 50_000) -> list[dict]: ...
    def list_docs(self, kind: str, sid: str | None = None, *, limit: int = 50_000) -> list[dict]: ...
    def search_utterances(self, q: str, *, sid: str | None, speaker: str | None,
                          limit: int, offset: int) -> list[dict]: ...
    def max_event_id(self, sid: str) -> int: ...
    def min_event_id(self, sid: str) -> int | None: ...
    def events_since(self, sid: str, last_id: int, limit: int = 2000) -> list[dict]: ...
    def put_doc(self, kind: str, id: str, sid: str | None, data: dict) -> None: ...
    def toggle_bookmark(self, sid: str, actor: str, uid: int, note: str = "") -> bool: ...
    def enqueue_command(self, cmd: dict) -> dict: ...
    def get_command(self, id: int) -> dict | None: ...
    def get_command_by_request_id(self, request_id: str) -> dict | None: ...
    def take_commands(self, limit: int = 10, max_bytes: int = 3_000_000) -> list[dict]: ...
    def finish_command(self, id: int, result: dict) -> bool: ...
    def expire_command(self, id: int) -> bool: ...
    def clear_command_body(self, id: int) -> None: ...
    def clear_command_payload(self, id: int) -> None: ...
    def audit(self, row: dict) -> None: ...
    def list_audit(self, limit: int = 300) -> list[dict]: ...
    def login_status(self, ip: str = "") -> dict: ...
    def login_attempt(self, ip: str, ok: bool) -> dict: ...
    def prune(self, now: int) -> None: ...


class _Store:
    """요청 연결 하나를 재사용하며 각 호출의 트랜잭션은 끝낸다."""

    dialect = "sqlite"

    def __init__(self, *, retention_days: int = 30, audit_days: int = 90):
        self.retention_days = retention_days
        self.audit_days = audit_days
        self._connection: ContextVar[Any] = ContextVar(f"cloud_conn_{id(self)}", default=None)

    def close(self) -> None:
        """SQL 연결은 request_scope 종료 때 닫는다. SQLite 메모리 연결은 별도로 닫는다."""

    def _connect(self) -> Any:
        raise NotImplementedError

    @contextmanager
    def request_scope(self) -> Iterator[Any]:
        """SSE·롱폴을 포함해 요청 안에서는 같은 연결을 빌려준다."""
        current = self._connection.get()
        if current is not None:
            yield current
            return
        conn = self._connect()
        token = self._connection.set(conn)
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._connection.reset(token)
            conn.close()

    @contextmanager
    def _cursor(self, conn: Any) -> Iterator[Any]:
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    @property
    def _json_param(self) -> str:
        return "?::jsonb" if self.dialect == "postgres" else "?"

    def _one(self, conn: Any, sql: str, params: tuple = ()) -> dict | None:
        with self._cursor(conn) as cur:
            if params:
                cur.execute(self._sql(sql), params)
            else:
                cur.execute(self._sql(sql))
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def _all(self, conn: Any, sql: str, params: tuple = ()) -> list[dict]:
        with self._cursor(conn) as cur:
            if params:
                cur.execute(self._sql(sql), params)
            else:
                cur.execute(self._sql(sql))
            return [dict(row) for row in cur.fetchall()]

    def _write(self, conn: Any, sql: str, params: tuple = ()) -> int:
        with self._cursor(conn) as cur:
            if params:
                cur.execute(self._sql(sql), params)
            else:
                cur.execute(self._sql(sql))
            return cur.rowcount

    def _many(self, conn: Any, sql: str, params: list[tuple]) -> None:
        if params:
            with self._cursor(conn) as cur:
                cur.executemany(self._sql(sql), params)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with self.request_scope() as conn:
            if self.dialect == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _read_one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.request_scope() as conn:
            try:
                return self._one(conn, sql, params)
            finally:
                conn.commit()

    def _read_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.request_scope() as conn:
            try:
                return self._all(conn, sql, params)
            finally:
                conn.commit()

    @property
    def _upsert_sql(self) -> str:
        return (
            "INSERT INTO docs(kind,id,sid,ts,seq,gen,data,updated_at) "
            f"VALUES(?,?,?,?,?,?,{self._json_param},?) "
            "ON CONFLICT(kind,id) DO UPDATE SET sid=excluded.sid,ts=excluded.ts,"
            "seq=excluded.seq,gen=COALESCE(excluded.gen,docs.gen),"
            "data=excluded.data,updated_at=excluded.updated_at"
        )

    def _doc_values(self, kind: str, id: Any, sid: str | None, data: dict,
                    ts: int, seq: int, gen: int | None, now: int) -> tuple:
        if not isinstance(data, dict):
            raise ValueError("문서 data는 객체여야 합니다")
        return (kind, str(id), sid, int(ts or 0), int(seq or 0), gen, _json(data), now)

    def _agent_row(self, conn: Any, *, lock: bool = False) -> dict:
        suffix = " FOR UPDATE" if lock and self.dialect == "postgres" else ""
        row = self._one(conn, "SELECT * FROM agent WHERE id=1" + suffix)
        if row is None:
            raise RuntimeError("클라우드 스키마가 초기화되지 않았습니다")
        row["info"] = _decode(row["info"])
        return row

    @staticmethod
    def _is_wanted(row: dict, now: int) -> bool:
        return bool(row["wanted_at"] and row["wanted_at"] > now - 600_000)

    def wanted(self) -> bool:
        row = self._read_one("SELECT wanted_at FROM agent WHERE id=1")
        return bool(row and self._is_wanted(row, now_ms()))

    def apply_push(self, boot_id: str, seq: int, ops: list[dict]) -> dict:
        """한 push를 원자적으로 반영하고 연속되는 같은 SQL은 묶어 보낸다."""
        if not isinstance(boot_id, str) or not boot_id or seq < 1:
            raise ValueError("boot_id와 양수 seq가 필요합니다")
        now = now_ms()
        with self._transaction() as conn:
            agent = self._agent_row(conn, lock=True)
            info = agent["info"]
            last_seq = int(info.get("last_seq", 0)) if info.get("boot_id") == boot_id else 0
            if seq <= last_seq:
                return {"ok": True, "applied": 0, "duplicate": True,
                        "wanted": self._is_wanted(agent, now)}
            docs, events = self._push_actions(conn, ops, now)
            for actions in (docs, events):
                for sql, rows in groupby(actions, key=lambda action: action[0]):
                    self._many(conn, sql, [row[1] for row in rows])
            info.update(boot_id=boot_id, last_seq=seq)
            self._write(conn, f"UPDATE agent SET info={self._json_param} WHERE id=1", (_json(info),))
            return {"ok": True, "applied": len(ops), "duplicate": False,
                    "wanted": self._is_wanted(agent, now)}

    def _push_actions(self, conn: Any, ops: list[dict], now: int) -> tuple[list, list]:
        session_ids = {str(op["sid"]) for op in ops if op.get("sid") and (
            op.get("kind") == "session" or op.get("type") in {"session.status", "stats", "analysis.status"})}
        sessions: dict[str, dict] = {}
        ids = sorted(session_ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = self._all(conn, "SELECT id,sid,ts,gen,data FROM docs WHERE kind='session' AND id IN (" +
                             ",".join("?" for _ in chunk) + ")", tuple(chunk))
            sessions.update({row["id"]: {"data": _decode(row["data"]), "ts": row["ts"],
                                        "sid": row["sid"], "gen": row["gen"]} for row in rows})
        docs: list[tuple[str, tuple]] = []
        events: list[tuple[str, tuple]] = []

        def put(kind: str, id: Any, sid: str | None, data: dict, ts: int = 0,
                seq: int = 0, gen: int | None = None) -> None:
            docs.append((self._upsert_sql, self._doc_values(kind, id, sid, data, ts, seq, gen, now)))
            if kind == "session":
                previous_gen = sessions.get(str(id), {}).get("gen")
                sessions[str(id)] = {"data": data, "ts": ts, "sid": sid,
                                     "gen": gen if gen is not None else previous_gen}

        def drop(sid: str) -> None:
            docs.append(("DELETE FROM docs WHERE sid=? OR (kind='session' AND id=?)", (sid, sid)))
            events.append(("DELETE FROM events WHERE sid=?", (sid,)))
            sessions.pop(sid, None)

        for op in ops:
            action = op["op"]
            sid = op.get("sid")
            if action == "event":
                if not sid:
                    raise ValueError("이벤트의 sid가 필요합니다")
                type_ = op["type"]
                data = op["data"]
                if not isinstance(data, dict):
                    raise ValueError("이벤트 data는 객체여야 합니다")
                events.append((f"INSERT INTO events(sid,type,data,created_at) VALUES(?,?,{self._json_param},?)",
                               (sid, type_, _json(data), now)))
                if type_ in {"utterance.upsert", "block.upsert", "item.upsert", "alert"}:
                    kind = {"utterance.upsert": "utterance", "block.upsert": "block",
                            "item.upsert": "item", "alert": "alert"}[type_]
                    if kind == "item" and data.get("status") == "superseded":
                        docs.append(("DELETE FROM docs WHERE kind=? AND id=?", (kind, str(data["id"]))))
                    else:
                        ts = data.get("ts_start", op.get("ts", 0)) if kind in {"utterance", "block"} else op.get("ts", 0)
                        put(kind, data["id"], sid, data, ts, data.get("seq", 0) if kind == "utterance" else 0)
                elif type_ in {"utterance.delete", "alert.delete"}:
                    kind = type_.split(".")[0]
                    docs.append(("DELETE FROM docs WHERE kind=? AND id=?", (kind, str(data["id"]))))
                elif type_ == "summary.update":
                    put("summary", f"{sid}:{data['scope']}", sid, data["summary"], op.get("ts", 0))
                elif type_ == "caption.source":
                    from ..stage_mirror import public_source
                    put("caption_source", data["id"], sid, public_source(data), data.get("first_rx", 0))
                    for did in data.get("invalidated_document_ids", []):
                        if type(did) is int:
                            docs.append(("DELETE FROM docs WHERE kind='transcript_stage' AND sid=? AND id=?", (sid, str(did))))
                elif type_ in {"session.status", "stats", "analysis.status"}:
                    previous = sessions.get(sid, {"data": {"id": sid}, "ts": op.get("ts", 0)})
                    merged = dict(previous["data"])
                    if type_ == "session.status":
                        merged.update(data)
                    else:
                        merged["stats" if type_ == "stats" else "analysis"] = data
                    put("session", sid, sid, merged, previous["ts"])
                elif type_ == "session.deleted":
                    drop(sid)
            elif action == "doc":
                put(op["kind"], op["id"], sid, op["data"], op.get("ts", 0), op.get("seq", 0), op.get("gen"))
            elif action in {"docs", "replace"}:
                kind = op["kind"]
                gen = int(op["gen"]) if action == "replace" else op.get("gen")
                for row in op["docs"]:
                    put(kind, row["id"], sid, row["data"], row.get("ts", 0), row.get("seq", 0), gen)
                if action == "replace" and op.get("final"):
                    docs.append(("DELETE FROM docs WHERE kind=? AND sid IS NOT DISTINCT FROM ? "
                                 "AND gen IS DISTINCT FROM ?", (kind, sid, gen)))
                    if kind == "session":
                        sessions = {key: value for key, value in sessions.items()
                                    if value["sid"] != sid or value["gen"] == gen}
            elif action == "delete":
                docs.append(("DELETE FROM docs WHERE kind=? AND id=?", (op["kind"], str(op["id"]))))
                if op["kind"] == "session":
                    sessions.pop(str(op["id"]), None)
            elif action == "drop_session":
                drop(sid)
            elif action == "sessions_index":
                sids = [str(value) for value in op["sids"]]
                markers = ",".join("?" for _ in sids)
                where = f"sid NOT IN ({markers})" if sids else "sid IS NOT NULL"
                docs.append((f"DELETE FROM docs WHERE {where}", tuple(sids)))
                events.append((f"DELETE FROM events WHERE {where}", tuple(sids)))
                sessions = {key: value for key, value in sessions.items() if key in sids}
            else:
                raise ValueError("알 수 없는 push op입니다")
        return docs, events

    def heartbeat(self, info: dict) -> dict:
        """상태를 병합한다. boot_id·last_seq는 apply_push만 바꾼다."""
        now = now_ms()
        with self._transaction() as conn:
            row = self._agent_row(conn, lock=True)
            merged = dict(row["info"])
            merged.update({key: value for key, value in info.items() if key not in {"boot_id", "last_seq"}})
            self._write(conn, f"UPDATE agent SET info={self._json_param},heartbeat_at=? WHERE id=1",
                        (_json(merged), now))
            wanted = self._is_wanted(row, now)
        if now - row["pruned_at"] >= 600_000:
            self.prune(now)
        return {"ok": True, "wanted": wanted}

    def agent_status(self) -> dict:
        with self.request_scope() as conn:
            row = self._agent_row(conn)
            conn.commit()
        info = row["info"]
        now = now_ms()
        last_push = int(info.get("last_push_ok_ms") or 0)
        return {**info, "online": bool(row["heartbeat_at"] and
                row["heartbeat_at"] > now - 3 * int(info.get("interval_s") or 15) * 1000),
                "mode": info.get("mode", "idle"), "heartbeat_at": row["heartbeat_at"],
                "push_delay_ms": max(0, now - last_push) if last_push else 0,
                "queue": int(info.get("queue") or 0), "wanted": self._is_wanted(row, now),
                "pruned_at": row["pruned_at"]}

    def mark_wanted(self) -> None:
        now = now_ms()
        with self._transaction() as conn:
            self._write(conn, "UPDATE agent SET wanted_at=? WHERE id=1 AND wanted_at<=?", (now, now - 60_000))

    def get_doc(self, kind: str, id: str) -> dict | None:
        row = self._read_one("SELECT data FROM docs WHERE kind=? AND id=?", (kind, str(id)))
        return _decode(row["data"]) if row is not None else None

    def doc_rows(self, kind: str, sid: str | None = None, *, limit: int = 50_000) -> list[dict]:
        where = "kind=?"
        params: list[Any] = [kind]
        if sid is not None:
            where += " AND sid=?"
            params.append(sid)
        direction = "DESC" if kind in {"session", "alert"} else "ASC"
        numeric = ("CASE WHEN id ~ '^[0-9]+$' THEN CAST(id AS BIGINT) ELSE 0 END" if self.dialect == "postgres"
                   else "CASE WHEN id<>'' AND id NOT GLOB '*[^0-9]*' THEN CAST(id AS INTEGER) ELSE 0 END")
        sql = f"SELECT * FROM docs WHERE {where} ORDER BY ts {direction},seq {direction},{numeric},id LIMIT ?"
        rows = self._read_all(sql, tuple(params + [max(0, limit)]))
        for row in rows:
            row["data"] = _decode(row["data"])
        return rows

    def list_docs(self, kind: str, sid: str | None = None, *, limit: int = 50_000) -> list[dict]:
        return [row["data"] for row in self.doc_rows(kind, sid, limit=limit)]

    def put_doc(self, kind: str, id: str, sid: str | None, data: dict) -> None:
        now = now_ms()
        with self._transaction() as conn:
            self._write(conn, self._upsert_sql,
                        self._doc_values(kind, id, sid, data, data.get("ts_start", data.get("created_at", 0)),
                                         data.get("seq", 0), None, now))

    def toggle_bookmark(self, sid: str, actor: str, uid: int, note: str = "") -> bool:
        """최초 생성도 같은 트랜잭션에 넣어 동시 변경이 서로 덮어쓰지 않게 한다."""
        now = now_ms()
        id = f"{sid}:{actor}"
        with self._transaction() as conn:
            # 없는 행은 먼저 만든다. Postgres의 유일 키 충돌도 상대 트랜잭션을 기다린다.
            self._write(conn, "INSERT INTO docs(kind,id,sid,ts,seq,gen,data,updated_at) "
                        f"VALUES('bookmark_set',?,?,0,0,NULL,{self._json_param},?) "
                        "ON CONFLICT(kind,id) DO NOTHING", (id, sid, _json({"ids": [], "notes": {}}), now))
            lock = " FOR UPDATE" if self.dialect == "postgres" else ""
            row = self._one(conn, "SELECT data FROM docs WHERE kind='bookmark_set' AND id=?" + lock, (id,))
            if row is None:
                raise RuntimeError("북마크 문서를 찾을 수 없습니다")
            current = _decode(row["data"])
            ids = list(current.get("ids", []))
            notes = dict(current.get("notes", {}))
            on = uid not in ids
            if on:
                ids.append(uid)
                notes[str(uid)] = note
            else:
                ids.remove(uid)
                notes.pop(str(uid), None)
            self._write(conn, f"UPDATE docs SET data={self._json_param},updated_at=? "
                        "WHERE kind='bookmark_set' AND id=?", (_json({"ids": ids, "notes": notes}), now, id))
            return on

    def _text(self, column: str, key: str) -> str:
        return f"{column}->>'{key}'" if self.dialect == "postgres" else f"json_extract({column},'$.{key}')"

    def search_utterances(self, q: str, *, sid: str | None = None, speaker: str | None = None,
                          limit: int = 50, offset: int = 0) -> list[dict]:
        q = q.strip()
        if len(q) < 2:
            return []
        like = "ILIKE" if self.dialect == "postgres" else "LIKE"
        filters = ["u.kind='utterance'"]
        params: list[Any] = []
        for token in q.split():
            filters.append(f"{self._text('u.data', 'text')} {like} ? ESCAPE '\\'")
            params.append("%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if sid:
            filters.append("u.sid=?")
            params.append(sid)
        if speaker:
            filters.append(f"{self._text('u.data', 'speaker')}=?")
            params.append(speaker)
        sql = ("SELECT u.data," + self._text("s.data", "title") + " AS session_title," +
               self._text("s.data", "meeting_date") + " AS meeting_date FROM docs u "
               "JOIN docs s ON s.kind='session' AND s.id=u.sid WHERE " + " AND ".join(filters) +
               " ORDER BY u.ts DESC,u.seq DESC LIMIT ? OFFSET ?")
        rows = self._read_all(sql, tuple(params + [max(0, limit), max(0, offset)]))
        return [{**_decode(row["data"]), "session_title": row["session_title"],
                 "meeting_date": row["meeting_date"]} for row in rows]

    def max_event_id(self, sid: str) -> int:
        row = self._read_one("SELECT MAX(id) AS id FROM events WHERE sid=?", (sid,))
        return int(row["id"] or 0)

    def min_event_id(self, sid: str) -> int | None:
        row = self._read_one("SELECT MIN(id) AS id FROM events WHERE sid=?", (sid,))
        return row["id"]

    def events_since(self, sid: str, last_id: int, limit: int = 2000) -> list[dict]:
        rows = self._read_all("SELECT id,sid,type,data,created_at FROM events WHERE sid=? AND id>? ORDER BY id LIMIT ?",
                              (sid, last_id, max(0, limit)))
        for row in rows:
            row["data"] = _decode(row["data"])
        return rows

    @staticmethod
    def _command(row: dict | None) -> dict | None:
        if row is None:
            return None
        row["headers"] = _decode(row["headers"])
        row["result"] = _decode(row["result"])
        if row.get("body") is not None:
            row["body"] = bytes(row["body"])
        row["body_b64"] = base64.b64encode(row.get("body") or b"").decode("ascii")
        return row

    def enqueue_command(self, cmd: dict) -> dict:
        body = cmd.get("body")
        if body is None and cmd.get("body_b64") is not None:
            body = base64.b64decode(cmd["body_b64"], validate=True)
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = cmd.get("headers", {})
        content_type = next((str(v) for k, v in headers.items() if k.lower() == "content-type"), "")
        request_hash = cmd.get("request_hash") or command_fingerprint(
            cmd["method"], cmd["path"], cmd.get("query", ""), content_type, body or b"")
        created = int(cmd.get("created_at") or now_ms())
        with self._transaction() as conn:
            self._write(conn, "INSERT INTO commands(request_id,request_hash,lane,method,path,query,headers,body,actor,role,"
                        f"created_at,deadline_at) VALUES(?,?,?,?,?,?,{self._json_param},?,?,?,?,?) "
                        "ON CONFLICT(request_id) DO NOTHING", (
                            cmd["request_id"], request_hash, cmd.get("lane", "fast"), cmd["method"], cmd["path"], cmd.get("query", ""),
                            _json(headers), body, cmd.get("actor", ""), cmd.get("role", "viewer"),
                            created, cmd.get("deadline_at", created + (55_000 if cmd.get("lane") == "slow" else 25_000))))
            return self._command(self._one(conn, "SELECT * FROM commands WHERE request_id=?", (cmd["request_id"],)))

    def get_command(self, id: int) -> dict | None:
        return self._command(self._read_one("SELECT * FROM commands WHERE id=?", (id,)))

    def get_command_by_request_id(self, request_id: str) -> dict | None:
        return self._command(self._read_one("SELECT * FROM commands WHERE request_id=?", (request_id,)))

    def take_commands(self, limit: int = 10, max_bytes: int = 3_000_000) -> list[dict]:
        now = now_ms()
        with self._transaction() as conn:
            suffix = " FOR UPDATE SKIP LOCKED" if self.dialect == "postgres" else ""
            rows = self._all(conn, "SELECT * FROM commands WHERE status='pending' AND deadline_at>? "
                             "ORDER BY id LIMIT ?" + suffix, (now, min(10, max(0, limit))))
            selected, size = [], 0
            for row in rows:
                length = len(row["body"] or b"")
                if size + length > max_bytes:
                    break
                size += length
                row.update(status="taken", taken_at=now)
                selected.append(row)
            self._many(conn, "UPDATE commands SET status='taken',taken_at=? WHERE id=? AND status='pending'",
                       [(now, row["id"]) for row in selected])
            return [self._command(row) for row in selected]

    def finish_command(self, id: int, result: dict) -> bool:
        with self._transaction() as conn:
            return self._write(conn, f"UPDATE commands SET status='done',result={self._json_param},done_at=? "
                               "WHERE id=? AND status='taken'", (_json(result), now_ms(), id)) == 1

    def expire_command(self, id: int) -> bool:
        with self._transaction() as conn:
            return self._write(conn, "UPDATE commands SET status='expired' WHERE id=? AND status='pending'", (id,)) == 1

    def clear_command_payload(self, id: int) -> None:
        """명시적 완전 정리: 입력 본문과 결과를 함께 지운다."""
        with self._transaction() as conn:
            self._write(conn, "UPDATE commands SET body=NULL,result=NULL WHERE id=?", (id,))

    def clear_command_body(self, id: int) -> None:
        """첫 응답 뒤 입력만 지운다. 결과는 완료 후 한 시간 동안 남긴다."""
        with self._transaction() as conn:
            self._write(conn, "UPDATE commands SET body=NULL WHERE id=?", (id,))

    def audit(self, row: dict) -> None:
        with self._transaction() as conn:
            self._write(conn, "INSERT INTO cloud_audit(ts,kind,ip,ua,name,role,method,path,status,command_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (row.get("ts", now_ms()), row["kind"], row.get("ip", ""),
                            row.get("ua", "")[:200], row.get("name", ""), row.get("role", ""),
                            row.get("method", ""), row.get("path", "").split("?", 1)[0],
                            row.get("status", 0), row.get("command_id")))

    def list_audit(self, limit: int = 300) -> list[dict]:
        return self._read_all("SELECT * FROM cloud_audit ORDER BY ts DESC,id DESC LIMIT ?", (max(0, limit),))

    def _login_status(self, conn: Any, ip: str, now: int) -> dict:
        row = self._one(conn, "SELECT SUM(CASE WHEN ip=? THEN 1 ELSE 0 END) AS ip_fail,COUNT(*) AS total_fail "
                        "FROM login_attempts WHERE NOT ok AND ts>?", (ip, now - 600_000))
        return {"blocked": int(row["ip_fail"] or 0) >= 5, "global_slow": row["total_fail"] >= 50}

    def login_status(self, ip: str = "") -> dict:
        with self.request_scope() as conn:
            result = self._login_status(conn, ip, now_ms())
            conn.commit()
            return result

    def login_attempt(self, ip: str, ok: bool) -> dict:
        now = now_ms()
        with self._transaction() as conn:
            self._write(conn, "INSERT INTO login_attempts(ip,ok,ts) VALUES(?,?,?)", (ip, bool(ok), now))
            return self._login_status(conn, ip, now)

    def prune(self, now: int) -> None:
        with self._transaction() as conn:
            self._write(conn, "DELETE FROM events WHERE created_at<?", (now - 7 * DAY_MS,))
            self._write(conn, "DELETE FROM events WHERE id IN (SELECT id FROM (SELECT id,"
                        "row_number() OVER (PARTITION BY sid ORDER BY id DESC) AS rn FROM events) old WHERE rn>5000)")
            self._write(conn, "UPDATE commands SET body=NULL WHERE created_at<=?", (now - 3_600_000,))
            self._write(conn, "UPDATE commands SET result=NULL WHERE done_at<=?", (now - 3_600_000,))
            self._write(conn, "DELETE FROM commands WHERE created_at<?", (now - 7 * DAY_MS,))
            expiry = now - self.retention_days * DAY_MS
            expired = "SELECT id FROM docs WHERE kind='session' AND updated_at<?"
            self._write(conn, f"DELETE FROM events WHERE sid IN ({expired})", (expiry,))
            self._write(conn, f"DELETE FROM docs WHERE sid IN ({expired}) AND kind<>'session'", (expiry,))
            self._write(conn, "DELETE FROM docs WHERE kind='session' AND updated_at<?", (expiry,))
            self._write(conn, "DELETE FROM cloud_audit WHERE ts<?", (now - self.audit_days * DAY_MS,))
            self._write(conn, "DELETE FROM login_attempts WHERE ts<?", (now - DAY_MS,))
            self._write(conn, "UPDATE agent SET pruned_at=? WHERE id=1", (now,))


class SQLiteStore(_Store):
    """운영 원본과 분리된 개발·테스트용 SQLite 미러."""

    def __init__(self, path: str | Path, *, retention_days: int = 30, audit_days: int = 90):
        super().__init__(retention_days=retention_days, audit_days=audit_days)
        self.path = path
        self._memory_uri = f"file:gukgam_cloud_{uuid.uuid4().hex}?mode=memory&cache=shared" if str(path) == ":memory:" else ""
        self._keeper = self._connect() if self._memory_uri else None

    def _connect(self) -> sqlite3.Connection:
        if not self._memory_uri:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._memory_uri or str(self.path), uri=bool(self._memory_uri),
                               timeout=5, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init_schema(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        schema = re.sub(r"--[^\n]*", "", schema)
        schema = re.sub(r"CREATE EXTENSION[^;]+;", "", schema)
        schema = re.sub(r"CREATE INDEX[^;]+USING GIN[^;]+;", "", schema)
        schema = re.sub(r"ALTER TABLE commands ADD COLUMN IF NOT EXISTS request_hash[^;]+;", "", schema)
        schema = re.sub(r"BIGSERIAL\s+PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT", schema)
        schema = schema.replace("::jsonb", "")
        schema = re.sub(r"\bJSONB\b", "TEXT", schema, flags=re.I)
        schema = re.sub(r"\bBIGINT\b", "INTEGER", schema)
        schema = re.sub(r"\bBYTEA\b", "BLOB", schema)
        schema = re.sub(r"\bBOOLEAN\b", "INTEGER", schema)
        with self.request_scope() as conn:
            conn.executescript(schema)
            with self._transaction() as transaction:
                columns = {row["name"] for row in self._all(transaction, "PRAGMA table_info(commands)")}
                if "request_hash" not in columns:
                    self._write(transaction, "ALTER TABLE commands ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
            conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None


class PostgresStore(_Store):
    """Neon pooled 연결에서 psycopg를 필요할 때만 불러온다."""

    dialect = "postgres"

    def __init__(self, dsn: str, *, retention_days: int = 30, audit_days: int = 90):
        super().__init__(retention_days=retention_days, audit_days=audit_days)
        self._dsn = dsn

    def _connect(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self._dsn, autocommit=False, connect_timeout=5,
                               prepare_threshold=None, row_factory=dict_row)

    def init_schema(self) -> None:
        with self._transaction() as conn:
            with self._cursor(conn) as cur:
                cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def make_store() -> CloudStore:
    """환경 선택만 한다. 스키마 초기화와 접속은 호출자가 명시한다."""
    dsn = os.environ.get("DATABASE_URL", "")
    kwargs = {"retention_days": int(os.environ.get("GUKGAM_CLOUD_RETENTION_DAYS", "30")),
              "audit_days": int(os.environ.get("GUKGAM_CLOUD_AUDIT_DAYS", "90"))}
    uri = os.environ.get("MONGODB_URI", "")
    if uri:
        from .mongo_store import MongoStore
        return MongoStore(uri, os.environ.get("MONGODB_DATABASE", "gukgam_cloud"), **kwargs)
    if dsn.startswith("postgres"):
        return PostgresStore(dsn, **kwargs)
    if os.environ.get("VERCEL") == "1":
        raise RuntimeError("운영 클라우드 DB 설정이 필요합니다")
    return SQLiteStore(os.environ.get("GUKGAM_CLOUD_SQLITE", "data/cloud.sqlite3"), **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="클라우드 저장소 스키마 초기화")
    parser.add_argument("--init-schema", action="store_true", required=True)
    parser.parse_args()
    store = None
    try:
        dsn = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL", "")
        if not os.environ.get("MONGODB_URI") and dsn.startswith("postgres"):
            store = PostgresStore(dsn)
        else:
            store = make_store()
        store.init_schema()
    except Exception:
        raise SystemExit("클라우드 스키마 초기화에 실패했습니다") from None
    finally:
        close = getattr(store, "close", None)
        if close:
            try:
                close()
            except Exception:
                raise SystemExit("클라우드 저장소 종료에 실패했습니다") from None
    print("클라우드 스키마 초기화를 완료했습니다")


if __name__ == "__main__":
    main()
