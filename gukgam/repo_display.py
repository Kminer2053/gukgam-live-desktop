"""표시 판본 저장소. 화면 조회에서는 저장값의 SELECT와 JSON 해석만 수행한다."""
from __future__ import annotations

import json

from .db import now_ms


class DisplayPendingError(RuntimeError):
    """입력 변경 뒤 새 표시 판본이 아직 확정되지 않았다."""

    def __init__(self, sid: str, generation: int = 0):
        super().__init__("표시 판본을 갱신하고 있습니다")
        self.session_id = sid
        self.generation = generation


class DisplayRepoMixin:
    def display_managed(self, sid: str) -> bool:
        return bool(self.db.query_one("SELECT id FROM sessions s WHERE id=? AND (source_type='assembly_caption' "
            "OR EXISTS(SELECT 1 FROM caption_sources c WHERE c.session_id=s.id) "
            "OR EXISTS(SELECT 1 FROM display_state d WHERE d.session_id=s.id))",(sid,)))

    def display_initialized(self, sid: str) -> bool:
        return bool(self.db.query_one("SELECT session_id FROM display_materializations WHERE session_id=?", (sid,)))

    def display_state(self, sid: str) -> dict | None:
        return self.db.query_one("SELECT * FROM display_state WHERE session_id=?", (sid,))

    def display_prime_sessions(self) -> list[str]:
        """기동 때 최근·진행 중인 저장 회의 두 개만 읽기 캐시에 준비한다."""
        return [row["id"] for row in self.db.query(
            "SELECT s.id FROM sessions s JOIN display_materializations m ON m.session_id=s.id "
            "ORDER BY (s.status IN ('live','paused')) DESC,s.updated_at DESC,s.id DESC LIMIT 2")]

    def display_dirty_sessions(self) -> list[str]:
        return [r["session_id"] for r in self.db.query(
            "SELECT d.session_id FROM display_state d JOIN sessions s ON s.id=d.session_id "
            "WHERE d.input_generation<>d.built_generation ORDER BY d.session_id")]

    def display_snapshot(self, sid: str) -> dict:
        """현재 저장 판본만 읽는다. dirty일 때 이전 AI보정본으로 성공 응답하지 않는다."""
        row = self.db.query_one(
            "SELECT s.input_generation,s.built_generation,m.generation,m.payload "
            "FROM display_state s LEFT JOIN display_materializations m ON m.session_id=s.session_id "
            "WHERE s.session_id=?", (sid,))
        if (not row or row["input_generation"] != row["built_generation"]
                or row["generation"] != row["input_generation"] or not row["payload"]):
            raise DisplayPendingError(sid, row["input_generation"] if row else 0)
        payload=self._display_decode(sid,row["payload"])
        after=self.display_state(sid)
        if not after or after["input_generation"]!=row["input_generation"] or after["built_generation"]!=row["input_generation"]:
            raise DisplayPendingError(sid,after["input_generation"] if after else 0)
        return payload

    def _display_decode(self, sid: str, encoded: str) -> dict:
        payload=json.loads(encoded)
        kinds=payload.pop("_display_arrays",None)
        if kinds is not None:
            payload.update({kind:[] for kind in kinds})
            for row in self.db.query("SELECT kind,payload FROM display_values WHERE session_id=? ORDER BY kind,sort_order",(sid,)):
                if row["kind"] in payload:
                    payload[row["kind"]].append(json.loads(row["payload"]))
        from .display_materializer import annotate_display_status
        return annotate_display_status(payload)

    def display_stage2_rows(self, sid: str) -> list[dict]:
        return self.db.query("SELECT * FROM display_stage2_rows WHERE session_id=? ORDER BY kind,row_key", (sid,))

    def display_build_snapshot(self, sid: str) -> dict | None:
        """변경 작업자만 이전 입력의 계산 상태를 재사용한다. 공개 GET에서는 호출하지 않는다."""
        row=self.db.query_one("SELECT payload FROM display_materializations WHERE session_id=?",(sid,))
        return self._display_decode(sid,row["payload"]) if row else None

    def display_guess_checkpoints(self, sid: str) -> dict:
        rows=self.db.query("SELECT kind,row_key,payload FROM display_stage2_rows WHERE session_id=? "
            "AND kind IN ('live_state','source_state','mapping_state')",(sid,))
        result={"live_state":{},"source_state":{},"mapping_state":{}}
        for row in rows:
            result[row["kind"]][row["row_key"]]=json.loads(row["payload"])
        return result

    def display_guess_state(self, sid: str, kind: str, key: str) -> dict | list | None:
        row=self.db.query_one("SELECT payload FROM display_stage2_rows WHERE session_id=? AND kind=? AND row_key=?",(sid,kind,key))
        return json.loads(row["payload"]) if row else None

    def display_values_initialized(self, sid: str) -> bool:
        return bool(self.db.query_one("SELECT 1 FROM display_values WHERE session_id=? LIMIT 1",(sid,)))

    def display_build_cache(self, sid: str) -> dict[int, dict]:
        return {r["document_id"]:json.loads(r["payload"]) for r in self.db.query(
            "SELECT document_id,payload FROM display_document_cache WHERE session_id=?", (sid,))}

    def display_changes(self, sid: str, *, after: int = 0) -> list[dict]:
        return self.db.query("SELECT generation,kind,row_key,block_id FROM display_changes WHERE session_id=? AND generation>? ORDER BY id", (sid,after))

    def display_build_blocks(self, sid: str) -> list[dict]:
        """빌더는 저장된 요약을 중복 검증하지 않고 입력 행만 읽는다."""
        rows=self.db.query("SELECT * FROM blocks WHERE session_id=? ORDER BY ts_start,id",(sid,))
        for row in rows:
            row["summary"]=json.loads(row["summary"]) if row.get("summary") else None
        return rows

    def display_document_source_ids(self, sid: str, did: int) -> list[int]:
        document = self.get_stage_document(sid, did)
        if document and document.get("source_kind") == "recording_asr":
            return sorted(int(uid) for uid in document.get("recording_source_hashes") or {})
        return [r["source_id"] for r in self.db.query(
            "SELECT r.source_id FROM transcript_stage_sources r JOIN transcript_stage_documents d ON d.id=r.document_id "
            "WHERE d.session_id=? AND d.id=?",(sid,did))]

    def display_commit(self, sid: str, generation: int, payload: str, stage2: list[dict],
                       document_cache: dict[int, str | None] | None = None, values: list[dict] | None = None) -> bool:
        """같은 입력 세대에 한해 최신 표시와 변경된 2단계 행 이력을 원자 저장한다."""
        t = now_ms()
        with self.db.transaction():
            state = self.display_state(sid)
            if not state or state["input_generation"] != generation:
                return False
            if values is not None:
                previous_values={(r["kind"],r["row_key"]):r for r in self.db.query(
                    "SELECT kind,row_key,sort_order,fingerprint FROM display_values WHERE session_id=?",(sid,))}
                wanted_values=set()
                for row in values:
                    key=row["kind"],row["row_key"];wanted_values.add(key);old=previous_values.get(key)
                    if row.get("reuse") and old:
                        if old["sort_order"]!=row["sort_order"]:
                            self.db.execute("UPDATE display_values SET sort_order=? WHERE session_id=? AND kind=? AND row_key=?",
                                (row["sort_order"],sid,*key))
                        continue
                    if old and old["sort_order"]==row["sort_order"] and old["fingerprint"]==row["fingerprint"]:
                        continue
                    self.db.execute("INSERT INTO display_values(session_id,kind,row_key,sort_order,payload,fingerprint) "
                        "VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,kind,row_key) DO UPDATE SET "
                        "sort_order=excluded.sort_order,payload=excluded.payload,fingerprint=excluded.fingerprint",
                        (sid,*key,row["sort_order"],row["payload"],row["fingerprint"]))
                for key in previous_values.keys()-wanted_values:
                    self.db.execute("DELETE FROM display_values WHERE session_id=? AND kind=? AND row_key=?",(sid,*key))
            previous = {(r["kind"],r["row_key"]):r for r in self.db.query(
                "SELECT kind,row_key,fingerprint FROM display_stage2_rows WHERE session_id=?",(sid,))}
            wanted = {(r["kind"],r["row_key"]):r for r in stage2}
            for key, row in wanted.items():
                old = previous.get(key)
                if old and (row.get("reuse") or old["fingerprint"] == row["fingerprint"]):
                    continue
                self.db.execute(
                    "INSERT INTO display_stage2_rows(session_id,kind,row_key,payload,fingerprint,generation,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id,kind,row_key) DO UPDATE SET "
                    "payload=excluded.payload,fingerprint=excluded.fingerprint,generation=excluded.generation,updated_at=excluded.updated_at",
                    (sid,*key,row["payload"],row["fingerprint"],generation,t))
                self.db.execute(
                    "INSERT INTO display_stage2_revisions(session_id,kind,row_key,generation,action,payload,fingerprint,created_at) "
                    "VALUES(?,?,?,?,'upsert',?,?,?)", (sid,*key,generation,row["payload"],row["fingerprint"],t))
            for key, old in previous.items():
                if key in wanted:
                    continue
                prior=self.db.query_one("SELECT payload FROM display_stage2_rows WHERE session_id=? AND kind=? AND row_key=?",(sid,*key))
                self.db.execute(
                    "INSERT INTO display_stage2_revisions(session_id,kind,row_key,generation,action,payload,fingerprint,created_at) "
                    "VALUES(?,?,?,?,'delete',?,?,?)", (sid,*key,generation,prior["payload"],old["fingerprint"],t))
                self.db.execute("DELETE FROM display_stage2_rows WHERE session_id=? AND kind=? AND row_key=?", (sid,*key))
            self.db.execute(
                "INSERT INTO display_materializations(session_id,generation,payload,built_at) VALUES(?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET generation=excluded.generation,payload=excluded.payload,built_at=excluded.built_at",
                (sid,generation,payload,t))
            self.db.execute("UPDATE display_state SET built_generation=?,built_at=? WHERE session_id=?", (generation,t,sid))
            if document_cache is not None:
                for did, encoded in document_cache.items():
                    if encoded is None:
                        continue
                    self.db.execute("INSERT INTO display_document_cache(session_id,document_id,payload) VALUES(?,?,?) "
                        "ON CONFLICT(session_id,document_id) DO UPDATE SET payload=excluded.payload", (sid,did,encoded))
                ids = tuple(document_cache)
                self.db.execute("DELETE FROM display_document_cache WHERE session_id=?" +
                    (" AND document_id NOT IN ("+','.join('?' for _ in ids)+")" if ids else ""), (sid,*ids))
            self.db.execute("DELETE FROM display_changes WHERE session_id=? AND generation<=?", (sid,generation))
        return True
