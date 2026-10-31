"""MongoDB Atlas의 명시적 스키마·요청 세션·원자적 클라우드 저장소."""
from __future__ import annotations

import base64
import copy
import json
import logging
import re
import threading
import time
import zlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .store import DAY_MS, command_fingerprint, now_ms

COLLECTIONS = ("docs", "events", "commands", "agent", "cloud_audit", "login_attempts", "counters")
COUNTERS = ("events", "commands", "cloud_audit", "login_attempts")
log = logging.getLogger(__name__)
DIAGNOSTIC_SLOW_MS = 1000
DIAGNOSTIC_FAST_FAILURE_LIMIT = 3
CLEANUP_TIMEOUT_S = 2
DOCUMENT_READ_TIMEOUT_S = 30
PAYLOAD_MAGIC = b"gukgam-json-zlib-v1\x00"
PAYLOAD_MIN_BYTES = 256
PAYLOAD_MAX_BYTES = 2_000_000
COMMAND_RESULT_MAGIC = b"gukgam-command-result-zlib-v1\x00"
COMMAND_RESULT_MIN_BYTES = 16_384
COMMAND_RESULT_MAX_BYTES = 4_100_000
COMMAND_RESULT_CODEC = "json-zlib-v1"
COMMAND_RESULT_LEGACY_GRACE_MS = 120_000
PRIVATE_ROW_FIELDS = ("_id", "numeric_id", "_revision", "search_text", "search_speaker", "session_title",
                      "meeting_date", "_payload_source", "_payload_z")
DIAGNOSTIC_OPERATIONS = frozenset({"operation", "transaction", "read", "push", "heartbeat", "mark_wanted",
                                 "document", "documents", "login_status", "agent_status", "prune"})
DIAGNOSTIC_PHASES = frozenset({"operation", "connect", "begin", "callback", "commit", "abort", "retry_wait", "find", "cache",
    "update", "agent_lock", "prepare_actions", "docs_write", "events_write", "agent_update", "expired_events",
    "events_scan", "surplus_events", "commands_cleanup", "expired_sessions", "audit_cleanup", "login_cleanup"})
DIAGNOSTIC_COMMANDS = frozenset({"insert", "update", "delete", "findAndModify", "find", "aggregate", "getMore",
                               "commitTransaction", "abortTransaction"})


def _driver():
    """클라우드 저장소를 실제 사용할 때만 선택 드라이버를 불러온다."""
    import pymongo
    return pymongo


def _encode(value: Any) -> str:
    """JSON 본문은 BSON 숫자 범위와 무관하게 원래 정밀도를 보존한다."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and "_compressed_payload" in value:
        try:
            if set(value) != {"_compressed_payload"}:
                raise ValueError
            payload = value["_compressed_payload"]
            if (not isinstance(payload, bytes) or not payload.startswith(PAYLOAD_MAGIC)
                    or len(payload) > PAYLOAD_MAX_BYTES + len(PAYLOAD_MAGIC)):
                raise ValueError
            decoder = zlib.decompressobj()
            raw = decoder.decompress(payload[len(PAYLOAD_MAGIC):], PAYLOAD_MAX_BYTES + 1)
            if (len(raw) > PAYLOAD_MAX_BYTES or decoder.unconsumed_tail
                    or not decoder.eof or decoder.unused_data):
                raise ValueError
            return json.loads(raw.decode("utf-8"))
        except (TypeError, ValueError, zlib.error):
            raise ValueError("문서 압축 읽기 표현을 확인할 수 없습니다") from None
    return json.loads(value) if isinstance(value, str) else value


def _encode_command_result(value: dict) -> str | bytes:
    """큰 임시 결과의 DB 표현만 줄인다. 에이전트의 원래 응답 봉투는 바꾸지 않는다."""
    encoded = _encode(value)
    raw = encoded.encode("utf-8")
    if not COMMAND_RESULT_MIN_BYTES <= len(raw) <= COMMAND_RESULT_MAX_BYTES:
        return encoded
    compressed = COMMAND_RESULT_MAGIC + zlib.compress(raw, level=1)
    return compressed if len(compressed) < len(raw) else encoded


def _decode_command_result(value: Any) -> Any:
    """기존 JSON은 그대로 읽고 새 이진 결과만 한도 안의 단일 스트림으로 복원한다."""
    if not isinstance(value, bytes):
        return _decode(value)
    try:
        if (not value.startswith(COMMAND_RESULT_MAGIC)
                or len(value) > COMMAND_RESULT_MAX_BYTES + len(COMMAND_RESULT_MAGIC)):
            raise ValueError
        decoder = zlib.decompressobj()
        raw = decoder.decompress(value[len(COMMAND_RESULT_MAGIC):], COMMAND_RESULT_MAX_BYTES + 1)
        if (len(raw) > COMMAND_RESULT_MAX_BYTES or decoder.unconsumed_tail
                or not decoder.eof or decoder.unused_data):
            raise ValueError
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (TypeError, ValueError, zlib.error):
        raise ValueError("임시 명령 결과의 압축 표현을 확인할 수 없습니다") from None


def _command_result_can_compress(row: dict, now: int) -> bool:
    """구 조회가 새 표현을 읽지 않게 새 명령 또는 충분히 지난 일회성 원본 GET만 고른다."""
    if row.get("_result_codec") == COMMAND_RESULT_CODEC:
        return True
    if row.get("_result_codec") is not None:
        return False
    created = row.get("created_at")
    # 원본 GET은 매번 새 UUID이며 구 배포 함수의 최대 실행시간은 60초다.
    return (row.get("method") == "GET" and isinstance(row.get("path"), str)
            and row["path"].startswith("/api/cloud-read/")
            and isinstance(created, int) and not isinstance(created, bool)
            and 0 < created <= now - COMMAND_RESULT_LEGACY_GRACE_MS)


def _payload_fields(encoded: str) -> dict:
    """원본 문자열을 바꾸지 않고 이득이 있는 제한 크기 읽기 표현만 만든다."""
    raw = encoded.encode("utf-8")
    if not PAYLOAD_MIN_BYTES <= len(raw) <= PAYLOAD_MAX_BYTES:
        return {}
    compressed = PAYLOAD_MAGIC + zlib.compress(raw, level=1)
    return {"_payload_source": encoded, "_payload_z": compressed} if len(compressed) < len(raw) else {}


def _payload_projection() -> list[dict]:
    """단순 이진 문자열이 같은 경우만 캐시를 고르고 원본 복사본은 전송하지 않는다."""
    return [{"$set": {"data": {"$cond": [
        {"$and": [{"$eq": ["$data", {"$ifNull": ["$_payload_source", None]}]},
                   {"$ne": [{"$ifNull": ["$_payload_z", None]}, None]}]},
        {"_compressed_payload": "$_payload_z"}, "$data"]}}},
        {"$project": {name: 0 for name in PRIVATE_ROW_FIELDS}}]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) or value is None else _encode(value)


def _doc_key(kind: str, id: Any) -> str:
    return json.dumps([kind, str(id)], ensure_ascii=False, separators=(",", ":"))


def _public(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {key: value for key, value in row.items() if key not in PRIVATE_ROW_FIELDS}


@dataclass(repr=False)
class _Scope:
    session: Any
    lock: Any = field(default_factory=threading.RLock)
    closed: bool = False


class MongoStore:
    """한 요청의 논리 세션을 재사용하고 각 변경은 제한 시간 내 트랜잭션으로 끝낸다."""

    dialect = "mongo"

    def __init__(self, uri: str, database: str = "gukgam_cloud", *, client=None,
                 retention_days: int = 30, audit_days: int = 90, transaction_timeout_s: float = 10):
        if not isinstance(uri, str) or not uri.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MongoDB 연결 설정이 올바르지 않습니다")
        if (not isinstance(database, str) or not database or len(database.encode("utf-8")) > 38
                or database in {"admin", "local", "config"} or re.search(r'[ /\\.\"$\x00]', database)):
            raise ValueError("클라우드 데이터베이스 이름이 올바르지 않습니다")
        if not 0 < transaction_timeout_s <= 55:
            raise ValueError("트랜잭션 제한 시간이 올바르지 않습니다")
        self._uri, self.database = uri, database
        self._client, self._own_client = client, client is None
        self._db = None
        self.retention_days, self.audit_days = retention_days, audit_days
        self.transaction_timeout_s = transaction_timeout_s
        self._scope: ContextVar[_Scope | None] = ContextVar(f"mongo_scope_{id(self)}", default=None)
        self._client_lock = threading.RLock()
        self._diagnostic_context = ContextVar(f"mongo_diagnostic_{id(self)}", default=None)
        self._wanted_lock = threading.RLock()
        self._wanted_cached_at = 0
        self._wanted_seen_at = 0

    def __repr__(self) -> str:
        return "<MongoStore>"

    def _database(self):
        with self._client_lock:
            if self._client is None:
                self._client = _driver().MongoClient(self._uri, connect=False, minPoolSize=0, maxPoolSize=5,
                    maxIdleTimeMS=60_000, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                    socketTimeoutMS=10_000, waitQueueTimeoutMS=5000, retryWrites=True, appname="gukgam-cloud",
                    compressors="zlib", zlibCompressionLevel=1,
                    event_listeners=[self._command_listener()])
            if self._db is None:
                pm = _driver()
                # URI의 읽기 선호·관심 수준이 미러/SSE의 최신 조회 계약을 바꾸지 않게 한다.
                self._db = self._client.get_database(self.database, read_preference=pm.ReadPreference.PRIMARY,
                    read_concern=pm.read_concern.ReadConcern("majority"),
                    write_concern=pm.write_concern.WriteConcern("majority"))
            return self._db

    @contextmanager
    def request_scope(self) -> Iterator[Any]:
        """SSE·롱폴은 세션만 유지한다. 읽기 사이에 트랜잭션을 열어 두지 않는다."""
        current = self._scope.get()
        if current is not None and not current.closed:
            yield current.session
            return
        self._database()
        scope = _Scope(self._client.start_session(causal_consistency=False))
        token = self._scope.set(scope)
        try:
            yield scope.session
        except BaseException:
            self._abort(scope.session)
            raise
        finally:
            with scope.lock:
                scope.closed = True
                scope.session.end_session()
            self._scope.reset(token)

    @staticmethod
    def _abort(session) -> None:
        try:
            if session.in_transaction:
                session.abort_transaction()
        except Exception:
            pass

    @staticmethod
    def _label(error: BaseException, name: str) -> bool:
        return bool(hasattr(error, "has_error_label") and error.has_error_label(name))

    @staticmethod
    def _duplicate(error: BaseException) -> bool:
        if getattr(error, "code", None) == 11000:
            return True
        details = getattr(error, "details", None) or {}
        return bool(details.get("writeErrors")) and all(row.get("code") == 11000 for row in details["writeErrors"])

    @contextmanager
    def _diagnostic(self, operation: str, phase: str):
        """실패·느린 연산의 고정 단계와 수치만 기록하고 입력·예외 본문은 남기지 않는다."""
        started = time.monotonic()
        trace = {"operation": operation if operation in DIAGNOSTIC_OPERATIONS else "operation",
                 "phase": phase if phase in DIAGNOSTIC_PHASES else "operation", "phase_started": started, "phases": {},
                 "attempts": 0, "commit_attempts": 0, "bulk": {}, "commands": {}, "inflight": {},
                 "fast_failure_logs": {}}
        token = self._diagnostic_context.set(trace)
        failure = None
        try:
            yield trace
        except BaseException as error:
            failure = error
            raise
        finally:
            finished = time.monotonic()
            phase_ms = max(0, int((finished - trace["phase_started"]) * 1000))
            try:
                self._finish_phase(trace, finished)
            except Exception:
                pass
            self._diagnostic_context.reset(token)
            elapsed = max(0, int((finished - started) * 1000))
            if failure is not None or elapsed >= DIAGNOSTIC_SLOW_MS:
                try:
                    code = getattr(failure, "code", None)
                    name = type(failure).__name__ if failure is not None else "none"
                    # 임의 입력이나 예외 설명이 고정 진단 필드를 통해 나오지 않게 한다.
                    op = trace["operation"]
                    phase = trace.get("failure_phase", trace["phase"]) if failure is not None else trace["phase"]
                    phase = phase if phase in DIAGNOSTIC_PHASES else "operation"
                    phase_ms = trace.get("failure_phase_ms", phase_ms) if failure is not None else phase_ms
                    name = name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name) else "Exception"
                    phases = {key: max(0, int(value * 1000)) for key, value in trace["phases"].items()}
                    log.warning("MongoDB 지연 진단 operation=%s phase=%s elapsed_ms=%d exception=%s code=%s "
                                "phase_ms=%d attempts=%d commit_attempts=%d phases=%s bulk=%s commands=%s",
                                op, phase, elapsed, name, code if type(code) is int and abs(code) < 2**31 else "none",
                                phase_ms, trace["attempts"], trace["commit_attempts"],
                                _encode(phases), _encode(trace["bulk"]), _encode(trace["commands"]))
                except Exception:
                    pass  # 진단 실패가 원래 저장 예외나 롤백을 바꾸지 않는다.

    @staticmethod
    def _finish_phase(trace, now) -> None:
        phase = trace["phase"] if trace["phase"] in DIAGNOSTIC_PHASES else "operation"
        trace["phases"][phase] = trace["phases"].get(phase, 0.0) + max(0, now - trace["phase_started"])
        trace["phase_started"] = now

    def _diagnostic_phase(self, phase: str) -> None:
        trace = self._diagnostic_context.get()
        if trace is not None:
            try:
                self._finish_phase(trace, time.monotonic())
            except Exception:
                pass
            trace["phase"] = phase if phase in DIAGNOSTIC_PHASES else "operation"

    def _command_listener(self):
        """실제 송신 명령의 고정 이름·개수·크기·시간만 모으는 지연 생성 관찰자."""
        owner = self
        class Listener(_driver().monitoring.CommandListener):
            def started(self, event):
                owner._command_started(event)
            def succeeded(self, event):
                owner._command_finished(event, failed=False)
            def failed(self, event):
                owner._command_finished(event, failed=True)
        return Listener()

    def _command_started(self, event) -> None:
        try:
            trace = self._diagnostic_context.get()
            command = event.command_name
            if trace is None or command not in DIAGNOSTIC_COMMANDS:
                return
            raw = event.command
            collection = ("transaction" if command in {"commitTransaction", "abortTransaction"}
                          else raw.get("collection" if command == "getMore" else command))
            if collection not in (*COLLECTIONS, "transaction"):
                return
            field = {"insert": "documents", "update": "updates", "delete": "deletes"}.get(command)
            count = len(raw.get(field, [])) if field else int(command == "findAndModify")
            from bson import BSON
            trace["inflight"][event.request_id] = {"command": command, "collection": collection,
                "model_count": count, "command_bytes": len(BSON.encode(raw)), "phase": trace["phase"]}
        except Exception:
            pass  # 관찰 실패가 드라이버 작업이나 원래 예외를 바꾸지 않는다.

    def _command_finished(self, event, *, failed: bool) -> None:
        try:
            trace = self._diagnostic_context.get()
            info = trace["inflight"].pop(event.request_id, None) if trace is not None else None
            if info is None:
                return
            elapsed = max(0, int(event.duration_micros / 1000))
            key = info["collection"] + ":" + info["command"]
            total = trace["commands"].setdefault(key, {"count": 0, "ms": 0, "models": 0, "bytes": 0, "failed": 0})
            for name, value in (("count", 1), ("ms", elapsed), ("models", info["model_count"]),
                                ("bytes", info["command_bytes"]), ("failed", int(failed))):
                total[name] += value
            failure = getattr(event, "failure", None) if failed else None
            code = failure.get("code") if isinstance(failure, dict) else getattr(failure, "code", None)
            code = code if type(code) is int and abs(code) < 2**31 else None
            if code is not None:
                codes = total.setdefault("codes", {})
                codes[str(code)] = codes.get(str(code), 0) + 1
            slow = elapsed >= DIAGNOSTIC_SLOW_MS
            emit = slow
            if failed and not slow:
                count = trace["fast_failure_logs"].get(key, 0)
                emit = count < DIAGNOSTIC_FAST_FAILURE_LIMIT
                if emit:
                    trace["fast_failure_logs"][key] = count + 1
            if emit:
                op = trace["operation"]
                log.warning("MongoDB 하위 명령 진단 operation=%s phase=%s command=%s collection=%s "
                            "model_count=%d command_bytes=%d elapsed_ms=%d failed=%d code=%s",
                            op, info["phase"], info["command"], info["collection"], info["model_count"],
                            info["command_bytes"], elapsed, int(failed), code if code is not None else "none")
        except Exception:
            pass

    def _run(self, callback: Callable[[Any], Any], *, operation: str = "transaction") -> Any:
        """콜백 재실행과 커밋 재전송을 구분하고 드라이버 전체 제한 시간도 적용한다."""
        pm = _driver()
        deadline = time.monotonic() + self.transaction_timeout_s
        retry_wait = 0.05
        with self._diagnostic(operation, "connect"), self.request_scope() as session, self._scope.get().lock:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("클라우드 트랜잭션 제한 시간이 지났습니다")
                retry = False
                trace = self._diagnostic_context.get()
                trace["attempts"] += 1
                trace.pop("failure_phase", None)
                trace.pop("failure_phase_ms", None)
                try:
                    # 정리는 이 작업 CSOT를 벗어난 뒤 처음 시도한다. 만료된 안에서
                    # abort를 먼저 호출하면 드라이버가 전송 없이 ABORTED로 바꿀 수 있다.
                    with pm.timeout(max(0.001, deadline - time.monotonic())):
                        self._diagnostic_phase("begin")
                        session.start_transaction(read_concern=pm.read_concern.ReadConcern("snapshot"),
                            write_concern=pm.write_concern.WriteConcern("majority"),
                            read_preference=pm.ReadPreference.PRIMARY,
                            max_commit_time_ms=max(1, int((deadline - time.monotonic()) * 1000)))
                        self._diagnostic_phase("callback")
                        result = callback(session)
                        while True:
                            try:
                                if time.monotonic() >= deadline:
                                    raise TimeoutError("클라우드 트랜잭션 제한 시간이 지났습니다")
                                self._diagnostic_phase("commit")
                                trace["commit_attempts"] += 1
                                session.commit_transaction()
                                return result
                            except Exception as error:
                                if (self._label(error, "UnknownTransactionCommitResult")
                                        and not getattr(error, "timeout", False)):
                                    if time.monotonic() >= deadline:
                                        raise TimeoutError("클라우드 커밋 확인 시간이 지났습니다") from None
                                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                                    continue
                                raise
                except BaseException as error:
                    trace["failure_phase"] = trace["phase"]
                    trace["failure_phase_ms"] = max(0, int((time.monotonic() - trace["phase_started"]) * 1000))
                    self._diagnostic_phase("abort")
                    with pm.timeout(CLEANUP_TIMEOUT_S):
                        self._abort(session)
                    retry = isinstance(error, Exception) and not getattr(error, "timeout", False) and (
                        self._label(error, "TransientTransactionError") or self._duplicate(error))
                    if not retry:
                        raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("클라우드 트랜잭션 재시도 시간이 지났습니다") from None
                self._diagnostic_phase("retry_wait")
                # 충돌 중 즉시 재시도가 공유 행의 경합과 진단량을 증폭하지 않게 한다.
                time.sleep(min(retry_wait, max(0, deadline - time.monotonic())))
                retry_wait = min(0.5, retry_wait * 2)

    def _read(self, callback: Callable[[Any], Any], *, operation: str = "read", timeout_s: float | None = None) -> Any:
        budget = self.transaction_timeout_s if timeout_s is None else timeout_s
        with self._diagnostic(operation, "find"), _driver().timeout(budget), self.request_scope() as session, self._scope.get().lock:
            return callback(session)

    def _bulk(self, collection: str, operations: list, session) -> None:
        if operations:
            trace = self._diagnostic_context.get()
            if trace is not None and collection in COLLECTIONS:
                try:
                    bulk = trace["bulk"].setdefault(collection, {"calls": 0, "models": 0})
                    bulk["calls"] += 1
                    bulk["models"] += len(operations)
                except Exception:
                    pass
            self._db[collection].bulk_write(operations, ordered=True, session=session)

    def init_schema(self) -> None:
        """컬렉션·인덱스는 CLI 초기화에서만 준비하고 그 뒤 초기 상태를 넣는다."""
        db, pm = self._database(), _driver()
        with pm.timeout(self.transaction_timeout_s):
            existing = set(db.list_collection_names())
            for name in COLLECTIONS:
                if name not in existing:
                    try:
                        db.create_collection(name)
                    except pm.errors.CollectionInvalid:
                        pass
                    except pm.errors.OperationFailure as error:
                        if error.code != 48:
                            raise
            indexes = {
                "docs": [([("kind", 1), ("id", 1)], "uq_docs_kind_id", True),
                         ([("kind", 1), ("sid", 1), ("ts", 1), ("seq", 1), ("numeric_id", 1), ("id", 1)], "ix_docs_kind_sid_ts", False),
                         ([("kind", 1), ("sid", 1), ("ts", -1), ("seq", -1), ("numeric_id", 1), ("id", 1)], "ix_docs_kind_sid_desc", False),
                         ([("kind", 1), ("ts", 1), ("seq", 1), ("numeric_id", 1), ("id", 1)], "ix_docs_kind_ts", False),
                         ([("kind", 1), ("ts", -1), ("seq", -1), ("numeric_id", 1), ("id", 1)], "ix_docs_kind_desc", False),
                         ([("sid", 1)], "ix_docs_sid", False),
                         ([("kind", 1), ("updated_at", 1)], "ix_docs_retention", False)],
                "events": [([("sid", 1), ("id", 1)], "ix_events_sid_id", False),
                           ([("created_at", 1)], "ix_events_created", False)],
                "commands": [([("request_id", 1)], "uq_commands_request_id", True),
                             ([("status", 1), ("id", 1), ("deadline_at", 1)], "ix_commands_pending", False),
                             ([("created_at", 1)], "ix_commands_created", False),
                             ([("done_at", 1)], "ix_commands_done", False)],
                "cloud_audit": [([("ts", -1), ("id", -1)], "ix_cloud_audit_ts", False)],
                "login_attempts": [([("ok", 1), ("ts", 1), ("ip", 1)], "ix_login_failures", False),
                                   ([("ts", 1)], "ix_login_attempts_ts", False)],
            }
            for name, definitions in indexes.items():
                for keys, label, unique in definitions:
                    db[name].create_index(keys, name=label, unique=unique)

        def seed(session):
            db["agent"].update_one({"_id": 1}, {"$setOnInsert": {"id": 1, "info": _encode({}), "heartbeat_at": 0,
                                  "wanted_at": 0, "pruned_at": 0, "_revision": 0}}, upsert=True, session=session)
            self._bulk("counters", [pm.UpdateOne({"_id": name}, {"$setOnInsert": {"value": 0}}, upsert=True)
                                    for name in COUNTERS], session)
        self._run(seed)

    def close(self) -> None:
        with self._client_lock:
            if self._own_client and self._client is not None:
                self._client.close()
                self._client, self._db = None, None

    def _counter(self, name: str, count: int, session) -> int:
        row = self._db["counters"].find_one_and_update({"_id": name}, {"$inc": {"value": count}},
            return_document=_driver().ReturnDocument.AFTER, session=session)
        if row is None:
            raise RuntimeError("클라우드 스키마가 초기화되지 않았습니다")
        return int(row["value"]) - count + 1

    def _agent(self, session, *, lock: bool = False) -> dict:
        if lock:
            row = self._db["agent"].find_one_and_update({"_id": 1}, {"$inc": {"_revision": 1}},
                return_document=_driver().ReturnDocument.AFTER, session=session)
        else:
            row = self._db["agent"].find_one({"_id": 1}, session=session)
        if row is None:
            raise RuntimeError("클라우드 스키마가 초기화되지 않았습니다")
        row["info"] = _decode(row["info"])
        return row

    @staticmethod
    def _is_wanted(row: dict, now: int) -> bool:
        return bool(row["wanted_at"] and row["wanted_at"] > now - 600_000)

    def wanted(self) -> bool:
        return self._read(lambda session: self._is_wanted(self._agent(session), now_ms()))

    def _doc_update(self, kind: str, id: Any, sid: str | None, data: dict, ts: int, seq: int,
                    gen: int | None, now: int):
        if not isinstance(data, dict):
            raise ValueError("문서 data는 객체여야 합니다")
        id = str(id)
        numeric = int(id) if id and id.isascii() and id.isdecimal() else 0
        if numeric > 2**63 - 1:
            raise ValueError("문서 정렬 식별자가 정수 범위를 넘습니다")
        encoded = _encode(data)
        fields = {"kind": kind, "id": id, "sid": sid, "ts": int(ts or 0), "seq": int(seq or 0),
                  "data": encoded, "updated_at": now, "numeric_id": numeric,
                  "search_text": _text(data.get("text")), "search_speaker": _text(data.get("speaker")),
                  "session_title": _text(data.get("title")), "meeting_date": _text(data.get("meeting_date"))}
        payload = _payload_fields(encoded)
        if payload:
            fields["_payload_z"] = payload["_payload_z"]
            values = {key: {"$literal": value} for key, value in fields.items()}
            values["gen"] = {"$ifNull": ["$gen", None]} if gen is None else {"$literal": gen}
            # 원문은 한 번만 보내고 같은 원자 갱신 안에서 서버가 정확한 복사본을 만든다.
            update = [{"$set": values}, {"$set": {"_payload_source": "$data"}}]
        else:
            update = {"$set": fields, "$unset": {"_payload_source": "", "_payload_z": ""}}
            if gen is None:
                update["$setOnInsert"] = {"gen": None}
            else:
                fields["gen"] = gen
        return _driver().UpdateOne({"_id": _doc_key(kind, id)}, update, upsert=True)

    def apply_push(self, boot_id: str, seq: int, ops: list[dict]) -> dict:
        if not isinstance(boot_id, str) or not boot_id or seq < 1:
            raise ValueError("boot_id와 양수 seq가 필요합니다")
        now = now_ms()

        def apply(session):
            # 첫 쓰기가 agent에 경합을 걸어 동시에 읽은 boot/seq가 중복 적용되지 않게 한다.
            self._diagnostic_phase("agent_lock")
            agent = self._agent(session, lock=True)
            info = dict(agent["info"])
            last = int(info.get("last_seq", 0)) if info.get("boot_id") == boot_id else 0
            if seq <= last:
                return {"ok": True, "applied": 0, "duplicate": True, "wanted": self._is_wanted(agent, now)}
            self._diagnostic_phase("prepare_actions")
            docs, events = self._push_actions(session, ops, now)
            self._diagnostic_phase("docs_write")
            self._bulk("docs", docs, session)
            self._diagnostic_phase("events_write")
            self._bulk("events", events, session)
            info.update(boot_id=boot_id, last_seq=seq)
            self._diagnostic_phase("agent_update")
            self._db["agent"].update_one({"_id": 1}, {"$set": {"info": _encode(info)}}, session=session)
            return {"ok": True, "applied": len(ops), "duplicate": False, "wanted": self._is_wanted(agent, now)}
        return self._run(apply, operation="push")

    def _push_actions(self, session, ops: list[dict], now: int) -> tuple[list, list]:
        """재시도마다 새 버퍼를 만들고 같은 push 안의 변경 순서를 ordered bulk로 보존한다."""
        pm = _driver()
        session_ids = {str(op["sid"]) for op in ops if op.get("sid") and (
            op.get("kind") == "session" or op.get("type") in {"session.status", "stats", "analysis.status"})}
        sessions = {row["id"]: {"data": _decode(row["data"]), "ts": row["ts"], "sid": row["sid"], "gen": row.get("gen")}
                    for row in self._db["docs"].find({"kind": "session", "id": {"$in": sorted(session_ids)}}, session=session)}
        count = sum(op.get("op") == "event" for op in ops)
        next_id = self._counter("events", count, session) if count else 0
        docs, events = [], []

        def put(kind, id, sid, data, ts=0, seq=0, gen=None):
            docs.append(self._doc_update(kind, id, sid, data, ts, seq, gen, now))
            if kind == "session":
                previous_gen = sessions.get(str(id), {}).get("gen")
                sessions[str(id)] = {"data": copy.deepcopy(data), "ts": ts, "sid": sid,
                                     "gen": gen if gen is not None else previous_gen}

        def drop(sid):
            # SQL의 NULL 비교는 아무 행도 지우지 않는다. 전역 문서를 삭제하지 않게 맞춘다.
            if sid is None:
                return
            docs.append(pm.DeleteMany({"$or": [{"sid": sid}, {"kind": "session", "id": sid}]}))
            events.append(pm.DeleteMany({"sid": sid}))
            sessions.pop(sid, None)

        for op in ops:
            action, sid = op["op"], op.get("sid")
            if action == "event":
                if not sid:
                    raise ValueError("이벤트의 sid가 필요합니다")
                type_, data = op["type"], op["data"]
                if not isinstance(data, dict):
                    raise ValueError("이벤트 data는 객체여야 합니다")
                events.append(pm.InsertOne({"_id": next_id, "id": next_id, "sid": sid, "type": type_,
                                           "data": _encode(data), "created_at": now}))
                next_id += 1
                if type_ in {"utterance.upsert", "block.upsert", "item.upsert", "alert"}:
                    kind = {"utterance.upsert": "utterance", "block.upsert": "block", "item.upsert": "item", "alert": "alert"}[type_]
                    if kind == "item" and data.get("status") == "superseded":
                        docs.append(pm.DeleteOne({"_id": _doc_key(kind, data["id"])}))
                    else:
                        ts = data.get("ts_start", op.get("ts", 0)) if kind in {"utterance", "block"} else op.get("ts", 0)
                        put(kind, data["id"], sid, data, ts, data.get("seq", 0) if kind == "utterance" else 0)
                elif type_ in {"utterance.delete", "alert.delete"}:
                    docs.append(pm.DeleteOne({"_id": _doc_key(type_.split(".")[0], data["id"])}))
                elif type_ == "summary.update":
                    put("summary", f"{sid}:{data['scope']}", sid, data["summary"], op.get("ts", 0))
                elif type_ == "caption.source":
                    from ..stage_mirror import public_source
                    put("caption_source", data["id"], sid, public_source(data), data.get("first_rx", 0))
                    for did in data.get("invalidated_document_ids", []):
                        if type(did) is int:
                            docs.append(pm.DeleteMany({"kind": "transcript_stage", "sid": sid, "id": str(did)}))
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
                    docs.append(pm.DeleteMany({"kind": kind, "sid": sid, "gen": {"$ne": gen}}))
                    if kind == "session":
                        sessions = {key: value for key, value in sessions.items() if value["sid"] != sid or value["gen"] == gen}
            elif action == "delete":
                docs.append(pm.DeleteOne({"_id": _doc_key(op["kind"], op["id"])}))
                if op["kind"] == "session":
                    sessions.pop(str(op["id"]), None)
            elif action == "drop_session":
                drop(sid)
            elif action == "sessions_index":
                sids = [str(value) for value in op["sids"]]
                # Mongo의 $nin은 null도 포함하므로 전역 문서는 명시적으로 제외한다.
                selector = {"sid": {"$nin": sids, "$ne": None}}
                docs.append(pm.DeleteMany(selector))
                events.append(pm.DeleteMany(selector))
                sessions = {key: value for key, value in sessions.items() if key in sids}
            else:
                raise ValueError("알 수 없는 push op입니다")
        return docs, events

    def heartbeat(self, info: dict) -> dict:
        """상태만 병합하고 push 판정용 boot_id·last_seq는 보존한다."""
        now = now_ms()

        def beat(session):
            self._diagnostic_phase("agent_lock")
            row = self._agent(session, lock=True)
            merged = dict(row["info"])
            merged.update({key: value for key, value in info.items() if key not in {"boot_id", "last_seq"}})
            self._diagnostic_phase("agent_update")
            self._db["agent"].update_one({"_id": 1}, {"$set": {"info": _encode(merged), "heartbeat_at": now}}, session=session)
            return {"ok": True, "wanted": self._is_wanted(row, now)}, row["pruned_at"]
        result, pruned = self._run(beat, operation="heartbeat")
        if now - pruned >= 600_000:
            self.prune(now)
        return result

    def agent_status(self) -> dict:
        row = self._read(lambda session: self._agent(session), operation="agent_status")
        info, now = row["info"], now_ms()
        last = int(info.get("last_push_ok_ms") or 0)
        return {**info, "online": bool(row["heartbeat_at"] and
                row["heartbeat_at"] > now - 3 * int(info.get("interval_s") or 15) * 1000),
                "mode": info.get("mode", "idle"), "heartbeat_at": row["heartbeat_at"],
                "push_delay_ms": max(0, now - last) if last else 0,
                "queue": int(info.get("queue") or 0), "wanted": self._is_wanted(row, now),
                "pruned_at": row["pruned_at"]}

    def mark_wanted(self) -> None:
        """확인된 열람 시각만 재사용하고 단일 문서 원자 갱신으로 수요를 표시한다."""
        def fresh(stamp: Any, now: int) -> bool:
            return isinstance(stamp, int) and not isinstance(stamp, bool) and stamp > 0 and 0 <= now - stamp < 60_000

        with self._diagnostic("mark_wanted", "cache"), _driver().timeout(self.transaction_timeout_s), self.request_scope() as session, self._scope.get().lock, self._wanted_lock:
            now = now_ms()
            if now < self._wanted_seen_at:
                self._wanted_cached_at = 0
            self._wanted_seen_at = now
            if fresh(self._wanted_cached_at, now):
                return
            self._wanted_cached_at = 0

            def current() -> Any:
                self._diagnostic_phase("find")
                row = self._db["agent"].find_one({"_id": 1}, {"wanted_at": 1}, session=session)
                if row is None:
                    raise RuntimeError("클라우드 스키마가 초기화되지 않았습니다")
                return row.get("wanted_at")

            stamp = current()
            if fresh(stamp, now):
                self._wanted_cached_at = int(stamp)
                return
            self._diagnostic_phase("update")
            result = self._db["agent"].update_one(
                {"_id": 1, "wanted_at": {"$lte": now - 60_000}},
                {"$set": {"wanted_at": now}}, session=session)
            if result.matched_count:
                self._wanted_cached_at = now
            else:
                # 다른 인스턴스의 선갱신과 행 누락을 성공으로 가정하지 않는다.
                stamp = current()
                if fresh(stamp, now):
                    self._wanted_cached_at = int(stamp)

    def get_doc(self, kind: str, id: str) -> dict | None:
        pipeline = [{"$match": {"_id": _doc_key(kind, id)}}, {"$limit": 1}, *_payload_projection()]
        rows = self._read(lambda session: list(self._db["docs"].aggregate(pipeline, session=session,
            collation={"locale": "simple"}, batchSize=1)), operation="document")
        return _decode(rows[0]["data"]) if rows else None

    def doc_rows(self, kind: str, sid: str | None = None, *, limit: int = 50_000) -> list[dict]:
        limit = max(0, limit)
        if not limit:
            return []
        selector = {"kind": kind}
        if sid is not None:
            selector["sid"] = sid
        direction = -1 if kind in {"session", "alert"} else 1
        # 전체 목록은 쓰기와 별도 읽기 예산을 쓰고, 큰 보정 문서의 응답 묶음은 줄인다.
        batch_size = 4 if kind == "transcript_stage" else 500
        pipeline = [{"$match": selector}, {"$sort": {"ts": direction, "seq": direction, "numeric_id": 1, "id": 1}},
                    {"$limit": limit}, *_payload_projection()]
        rows = self._read(lambda session: list(self._db["docs"].aggregate(pipeline, session=session,
            collation={"locale": "simple"}, batchSize=batch_size)), operation="documents", timeout_s=DOCUMENT_READ_TIMEOUT_S)
        result = []
        for row in rows:
            public = _public(row)
            public["data"] = _decode(public["data"])
            result.append(public)
        return result

    def list_docs(self, kind: str, sid: str | None = None, *, limit: int = 50_000) -> list[dict]:
        return [row["data"] for row in self.doc_rows(kind, sid, limit=limit)]

    def put_doc(self, kind: str, id: str, sid: str | None, data: dict) -> None:
        now = now_ms()
        operation = self._doc_update(kind, id, sid, data, data.get("ts_start", data.get("created_at", 0)),
                                     data.get("seq", 0), None, now)
        self._run(lambda session: self._bulk("docs", [operation], session))

    def toggle_bookmark(self, sid: str, actor: str, uid: int, note: str = "") -> bool:
        now, id = now_ms(), f"{sid}:{actor}"

        def toggle(session):
            # 없는 문서 생성과 첫 쓰기 경합도 같은 트랜잭션에 포함한다.
            self._db["docs"].update_one({"_id": _doc_key("bookmark_set", id)}, {"$setOnInsert": {
                "kind": "bookmark_set", "id": id, "sid": sid, "ts": 0, "seq": 0, "gen": None,
                "data": _encode({"ids": [], "notes": {}}), "updated_at": now, "numeric_id": 0}},
                upsert=True, session=session)
            row = self._db["docs"].find_one({"_id": _doc_key("bookmark_set", id)}, session=session)
            if row is None:
                raise RuntimeError("북마크 문서를 찾을 수 없습니다")
            data = _decode(row["data"])
            ids, notes = list(data.get("ids", [])), dict(data.get("notes", {}))
            on = uid not in ids
            if on:
                ids.append(uid)
                notes[str(uid)] = note
            else:
                ids.remove(uid)
                notes.pop(str(uid), None)
            self._db["docs"].update_one({"_id": _doc_key("bookmark_set", id)},
                {"$set": {"data": _encode({"ids": ids, "notes": notes}), "updated_at": now,
                          "search_text": None, "search_speaker": None, "session_title": None, "meeting_date": None}},
                session=session)
            return on
        return self._run(toggle)

    def search_utterances(self, q: str, *, sid: str | None = None, speaker: str | None = None,
                          limit: int = 50, offset: int = 0) -> list[dict]:
        q, limit, offset = q.strip(), max(0, limit), max(0, offset)
        if len(q) < 2 or not limit:
            return []
        selector: dict = {"kind": "utterance", "$and": [
            {"search_text": {"$regex": re.escape(token), "$options": "i"}} for token in q.split()]}
        if sid:
            selector["sid"] = sid
        if speaker:
            selector["search_speaker"] = speaker
        # 조인 전 인덱스 순서로 읽어 Atlas Free의 메모리 정렬 상한을 피한다.
        pipeline = [{"$match": selector}, {"$sort": {"ts": -1, "seq": -1}},
            {"$lookup": {"from": "docs", "localField": "sid", "foreignField": "id", "as": "session"}},
            {"$unwind": "$session"}, {"$match": {"session.kind": "session"}},
            {"$skip": offset}, {"$limit": limit},
            {"$project": {"_id": 0, "data": 1, "session_title": "$session.session_title", "meeting_date": "$session.meeting_date"}}]
        rows = self._read(lambda session: list(self._db["docs"].aggregate(pipeline, session=session)))
        return [{**_decode(row["data"]), "session_title": row.get("session_title"),
                 "meeting_date": row.get("meeting_date")} for row in rows]

    def max_event_id(self, sid: str) -> int:
        row = self._read(lambda session: self._db["events"].find_one({"sid": sid}, sort=[("id", -1)], session=session))
        return int(row["id"]) if row else 0

    def min_event_id(self, sid: str) -> int | None:
        row = self._read(lambda session: self._db["events"].find_one({"sid": sid}, sort=[("id", 1)], session=session))
        return int(row["id"]) if row else None

    def events_since(self, sid: str, last_id: int, limit: int = 2000) -> list[dict]:
        limit = max(0, limit)
        if not limit:
            return []
        rows = self._read(lambda session: list(self._db["events"].find(
            {"sid": sid, "id": {"$gt": last_id}}, session=session).sort("id", 1).limit(limit)))
        result = []
        for row in rows:
            public = _public(row)
            public["data"] = _decode(public["data"])
            result.append(public)
        return result

    @staticmethod
    def _command(row: dict | None) -> dict | None:
        public = _public(row)
        if public is None:
            return None
        public.pop("_result_codec", None)
        public["headers"], public["result"] = _decode(public["headers"]), _decode_command_result(public["result"])
        if public.get("body") is not None:
            public["body"] = bytes(public["body"])
        public["body_b64"] = base64.b64encode(public.get("body") or b"").decode("ascii")
        return public

    def enqueue_command(self, cmd: dict) -> dict:
        body = cmd.get("body")
        if body is None and cmd.get("body_b64") is not None:
            body = base64.b64decode(cmd["body_b64"], validate=True)
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = cmd.get("headers", {})
        content_type = next((str(value) for key, value in headers.items() if key.lower() == "content-type"), "")
        fingerprint = cmd.get("request_hash") or command_fingerprint(cmd["method"], cmd["path"], cmd.get("query", ""),
                                                                     content_type, body or b"")
        created = int(cmd.get("created_at") or now_ms())

        def enqueue(session):
            row = self._db["commands"].find_one({"request_id": cmd["request_id"]}, session=session)
            if row is not None:
                return self._command(row)
            id = self._counter("commands", 1, session)
            row = {"_id": id, "id": id, "request_id": cmd["request_id"], "request_hash": fingerprint,
                   "lane": cmd.get("lane", "fast"), "method": cmd["method"], "path": cmd["path"],
                   "query": cmd.get("query", ""), "headers": _encode(headers), "body": body,
                   "actor": cmd.get("actor", ""), "role": cmd.get("role", "viewer"), "status": "pending",
                   "_result_codec": COMMAND_RESULT_CODEC,
                   "result": None, "created_at": created,
                   "deadline_at": cmd.get("deadline_at", created + (55_000 if cmd.get("lane") == "slow" else 25_000)),
                   "taken_at": None, "done_at": None}
            self._db["commands"].update_one({"_id": id}, {"$setOnInsert": row}, upsert=True, session=session)
            return self._command(row)
        return self._run(enqueue)

    def get_command(self, id: int) -> dict | None:
        return self._command(self._read(lambda session: self._db["commands"].find_one({"_id": id}, session=session)))

    def get_command_by_request_id(self, request_id: str) -> dict | None:
        return self._command(self._read(lambda session: self._db["commands"].find_one({"request_id": request_id}, session=session)))

    def take_commands(self, limit: int = 10, max_bytes: int = 3_000_000) -> list[dict]:
        now, limit = now_ms(), min(10, max(0, limit))
        if not limit:
            return []

        def take(session):
            rows = self._db["commands"].find({"status": "pending", "deadline_at": {"$gt": now}},
                                           session=session).sort("id", 1).limit(limit)
            selected, size = [], 0
            for row in rows:
                length = len(row["body"] or b"")
                if size + length > max_bytes:
                    break
                size += length
                row.update(status="taken", taken_at=now)
                selected.append(row)
            if selected:
                self._db["commands"].update_many({"_id": {"$in": [row["id"] for row in selected]}, "status": "pending"},
                    {"$set": {"status": "taken", "taken_at": now}}, session=session)
            return [self._command(row) for row in selected]
        return self._run(take)

    def finish_command(self, id: int, result: dict) -> bool:
        encoded, now = _encode_command_result(result), now_ms()

        def finish(session):
            selected, value = {"_id": id, "status": "taken"}, encoded
            if isinstance(encoded, bytes):
                fields = ("_result_codec", "created_at", "method", "path")
                row = self._db["commands"].find_one(selected,
                    {"_id": 0, **{field: 1 for field in fields}}, session=session)
                if row is None:
                    return False
                selected.update({field: row.get(field) for field in fields})
                if not _command_result_can_compress(row, now):
                    value = _encode(result)
            return self._db["commands"].update_one(selected,
                {"$set": {"status": "done", "result": value, "done_at": now}}, session=session).modified_count == 1
        return self._run(finish)

    def expire_command(self, id: int) -> bool:
        return self._run(lambda session: self._db["commands"].update_one({"_id": id, "status": "pending"},
            {"$set": {"status": "expired"}}, session=session).modified_count == 1)

    def clear_command_body(self, id: int) -> None:
        self._run(lambda session: self._db["commands"].update_one({"_id": id}, {"$set": {"body": None}}, session=session))

    def clear_command_payload(self, id: int) -> None:
        self._run(lambda session: self._db["commands"].update_one({"_id": id}, {"$set": {"body": None, "result": None}}, session=session))

    def audit(self, row: dict) -> None:
        now = now_ms()

        def save(session):
            id = self._counter("cloud_audit", 1, session)
            self._bulk("cloud_audit", [_driver().InsertOne({"_id": id, "id": id, "ts": row.get("ts", now),
                "kind": row["kind"], "ip": row.get("ip", ""), "ua": row.get("ua", "")[:200],
                "name": row.get("name", ""), "role": row.get("role", ""), "method": row.get("method", ""),
                "path": row.get("path", "").split("?", 1)[0], "status": row.get("status", 0),
                "command_id": row.get("command_id")})], session)
        self._run(save)

    def list_audit(self, limit: int = 300) -> list[dict]:
        limit = max(0, limit)
        if not limit:
            return []
        rows = self._read(lambda session: list(self._db["cloud_audit"].find({}, session=session).sort(
            [("ts", -1), ("id", -1)]).limit(limit)))
        return [_public(row) for row in rows]

    def _login_status(self, session, ip: str, now: int) -> dict:
        pipeline = [{"$match": {"ok": False, "ts": {"$gt": now - 600_000}}},
            {"$group": {"_id": None, "ip_fail": {"$sum": {"$cond": [{"$eq": ["$ip", ip]}, 1, 0]}},
                        "total_fail": {"$sum": 1}}}]
        rows = list(self._db["login_attempts"].aggregate(pipeline, session=session))
        counts = rows[0] if rows else {"ip_fail": 0, "total_fail": 0}
        return {"blocked": counts["ip_fail"] >= 5, "global_slow": counts["total_fail"] >= 50}

    def login_status(self, ip: str = "") -> dict:
        return self._read(lambda session: self._login_status(session, ip, now_ms()), operation="login_status")

    def login_attempt(self, ip: str, ok: bool) -> dict:
        now = now_ms()

        def attempt(session):
            id = self._counter("login_attempts", 1, session)
            self._bulk("login_attempts", [_driver().InsertOne({"_id": id, "id": id, "ip": ip, "ok": bool(ok), "ts": now})], session)
            return self._login_status(session, ip, now)
        return self._run(attempt)

    def prune(self, now: int) -> None:
        def cleanup(session):
            self._diagnostic_phase("agent_lock")
            self._agent(session, lock=True)
            db, pm = self._db, _driver()
            self._diagnostic_phase("expired_events")
            db["events"].delete_many({"created_at": {"$lt": now - 7 * DAY_MS}}, session=session)
            # 금지된 집계 단계와 큰 $group 배열 대신 정렬된 최소 메타만 스트리밍한다.
            removals, sid, count = [], None, 0
            self._diagnostic_phase("events_scan")
            for row in db["events"].find({}, {"sid": 1, "id": 1}, session=session).sort([("sid", -1), ("id", -1)]):
                if row["sid"] != sid:
                    sid, count = row["sid"], 0
                count += 1
                if count == 5001:
                    removals.append(pm.DeleteMany({"sid": sid, "id": {"$lte": row["id"]}}))
            self._diagnostic_phase("surplus_events")
            self._bulk("events", removals, session)
            self._diagnostic_phase("commands_cleanup")
            db["commands"].update_many({"created_at": {"$lte": now - 3_600_000}}, {"$set": {"body": None}}, session=session)
            db["commands"].update_many({"done_at": {"$ne": None, "$lte": now - 3_600_000}}, {"$set": {"result": None}}, session=session)
            db["commands"].delete_many({"created_at": {"$lt": now - 7 * DAY_MS}}, session=session)
            expiry = now - self.retention_days * DAY_MS
            self._diagnostic_phase("expired_sessions")
            expired = [row["id"] for row in db["docs"].find({"kind": "session", "updated_at": {"$lt": expiry}},
                                                           {"id": 1}, session=session)]
            for start in range(0, len(expired), 1000):
                chunk = expired[start:start + 1000]
                db["events"].delete_many({"sid": {"$in": chunk}}, session=session)
                db["docs"].delete_many({"sid": {"$in": chunk}, "kind": {"$ne": "session"}}, session=session)
            db["docs"].delete_many({"kind": "session", "updated_at": {"$lt": expiry}}, session=session)
            self._diagnostic_phase("audit_cleanup")
            db["cloud_audit"].delete_many({"ts": {"$lt": now - self.audit_days * DAY_MS}}, session=session)
            self._diagnostic_phase("login_cleanup")
            db["login_attempts"].delete_many({"ts": {"$lt": now - DAY_MS}}, session=session)
            self._diagnostic_phase("agent_update")
            db["agent"].update_one({"_id": 1}, {"$set": {"pruned_at": now}}, session=session)
        self._run(cleanup, operation="prune")
