"""데이터 접근 계층. 모든 모듈은 SQL 대신 이 Repo 를 쓴다."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any, Iterable

from .db import Database, now_ms
from .repo_stages import StageRepoMixin
from .repo_display import DisplayRepoMixin

_JSON_FIELDS = {
    "sessions": ("source_config", "source_state"),
    "roster": ("aliases",),
    "segments": ("raw",),
    "utterances": ("mentions", "review_parts"),
    "transcript_reviews": ("payload",),
    "audio_review_evidence": ("payload",),
    "blocks": ("summary",),
    "summaries": ("content",),
}


def _decode(table: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    for f in _JSON_FIELDS.get(table, ()):
        v = row.get(f)
        if isinstance(v, str):
            if v == "":
                row[f] = None if f == "summary" else {}
                continue
            try:
                row[f] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return row


def _decode_all(table: str, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_decode(table, r) for r in rows]  # type: ignore[misc]


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def new_session_id() -> str:
    # URL 에 쓰기 좋은 짧은 id(예: 20261008-a1b2c3)
    from datetime import datetime

    from .config import KST

    return datetime.now(KST).strftime("%Y%m%d") + "-" + secrets.token_hex(3)


def fingerprint(*parts: str) -> str:
    norm = "|".join(re.sub(r"\s+", "", p or "") for p in parts)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _like(term: str) -> str:
    """LIKE '%term%' 인자(와일드카드 문자는 글자 그대로 찾는다; ESCAPE '\\' 와 함께 쓴다)."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Repo(StageRepoMixin, DisplayRepoMixin):
    def __init__(self, db: Database):
        self.db = db
        self._review_invalidations: set[int] = set()
        self._review_state_invalidations: set[int] = set()

    # ------------------------------------------------------------------ sessions
    def create_session(self, *, title: str, source_type: str, committee: str = "", agencies: str = "",
                       meeting_date: str = "", source_config: dict | None = None, save_audio: bool = False,
                       created_by: str = "", notes: str = "", session_id: str | None = None) -> dict:
        sid = session_id or new_session_id()
        t = now_ms()
        self.db.execute(
            "INSERT INTO sessions(id,title,committee,agencies,meeting_date,status,source_type,source_config,"
            "save_audio,created_at,updated_at,created_by,notes) VALUES(?,?,?,?,?,'idle',?,?,?,?,?,?,?)",
            (sid, title, committee, agencies, meeting_date, source_type, _j(source_config or {}),
             int(save_audio), t, t, created_by, notes),
        )
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, sid: str) -> dict | None:
        return _decode("sessions", self.db.query_one("SELECT * FROM sessions WHERE id=?", (sid,)))

    def list_sessions(self, limit: int = 100, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.query("SELECT * FROM sessions WHERE status=? ORDER BY created_at DESC LIMIT ?",
                                 (status, limit))
        else:
            rows = self.db.query("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,))
        return _decode_all("sessions", rows)

    def update_session(self, sid: str, **fields: Any) -> dict | None:
        allowed = {"title", "committee", "agencies", "meeting_date", "status", "source_type", "source_config",
                   "source_state", "save_audio", "started_at", "ended_at", "notes"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"수정할 수 없는 필드: {k}")
            if k in ("source_config", "source_state"):
                v = _j(v)
            if k == "save_audio":
                v = int(bool(v))
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return self.get_session(sid)
        sets.append("updated_at=?")
        params.append(now_ms())
        params.append(sid)
        self.db.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", tuple(params))
        return self.get_session(sid)

    def delete_session(self, sid: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))

    def session_stats(self, sid: str) -> dict:
        u = self.db.query_one(
            "SELECT COUNT(*) n, MIN(ts_start) t0, MAX(COALESCE(ts_end, ts_start)) t1 FROM utterances "
            "WHERE session_id=? AND is_final=1", (sid,)) or {}
        b = self.db.query_one(
            "SELECT COUNT(*) n, SUM(status='needs_name') needs FROM blocks WHERE session_id=? AND kind='question'",
            (sid,)) or {}
        items = self.db.query("SELECT kind, COUNT(*) n FROM items WHERE session_id=? "
                              "AND status NOT IN ('dismissed','superseded') GROUP BY kind", (sid,))
        alerts = self.db.query_one("SELECT COUNT(*) n, SUM(ack=0) unack FROM alerts WHERE session_id=?", (sid,)) or {}
        return {
            "utterances": u.get("n") or 0,
            "ts_first": u.get("t0"),
            "ts_last": u.get("t1"),
            "question_blocks": b.get("n") or 0,
            "needs_name": b.get("needs") or 0,
            "items": {r["kind"]: r["n"] for r in items},
            "alerts": alerts.get("n") or 0,
            "alerts_unack": alerts.get("unack") or 0,
        }

    # ------------------------------------------------------------------ roster
    def set_roster(self, sid: str | None, members: list[dict]) -> list[dict]:
        old = [{k: m.get(k) for k in ("name", "role", "aliases")} for m in self.get_roster(sid, include_global=False)]
        with self.db.transaction() as c:
            if sid is None:
                c.execute("DELETE FROM roster WHERE session_id IS NULL")
            else:
                c.execute("DELETE FROM roster WHERE session_id=?", (sid,))
            for m in members:
                name = (m.get("name") or "").strip()
                if not name:
                    continue
                c.execute(
                    "INSERT INTO roster(session_id,name,role,party,title,aliases) VALUES(?,?,?,?,?,?)",
                    (sid, name, m.get("role") or "member", m.get("party") or "", m.get("title") or "",
                     _j(m.get("aliases") or [])),
                )
        result = self.get_roster(sid, include_global=False)
        current = [{k: m.get(k) for k in ("name", "role", "aliases")} for m in result]
        if current != old:
            query = "SELECT id FROM blocks" if sid is None else "SELECT id FROM blocks WHERE session_id=?"
            for block in self.db.query(query, () if sid is None else (sid,)):
                self.invalidate_block_reviews(block["id"])
        return result

    def get_roster(self, sid: str | None, include_global: bool = True) -> list[dict]:
        if sid is None:
            rows = self.db.query("SELECT * FROM roster WHERE session_id IS NULL ORDER BY role, name")
        elif include_global:
            rows = self.db.query(
                "SELECT * FROM roster WHERE session_id=? OR session_id IS NULL ORDER BY session_id IS NULL, role, name",
                (sid,))
        else:
            rows = self.db.query("SELECT * FROM roster WHERE session_id=? ORDER BY role, name", (sid,))
        return _decode_all("roster", rows)

    # ------------------------------------------------------------------ segments
    def get_segment(self, sid: str, key: str) -> dict | None:
        return _decode("segments", self.db.query_one("SELECT * FROM segments WHERE session_id=? AND source_key=?", (sid, key)))

    def upsert_segment(self, sid: str, key: str, ts_start: int, ts_end: int | None, text: str,
                       is_final: bool, raw: dict | None = None) -> None:
        t = now_ms()
        self.db.execute(
            "INSERT INTO segments(session_id,source_key,ts_start,ts_end,text,raw,is_final,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,0,?,?) ON CONFLICT(session_id, source_key) DO UPDATE SET "
            "ts_end=excluded.ts_end, text=excluded.text, raw=excluded.raw, "
            "is_final=MAX(segments.is_final, excluded.is_final), revision=segments.revision+1, updated_at=excluded.updated_at",
            (sid, key, ts_start, ts_end, text, _j(raw or {}), int(is_final), t, t),
        )

    def merge_source_revision(self, sid: str, base: str, revision: dict) -> list[str]:
        """기억 창 안팎의 원조각을 포함한 기존 발언에 검토 후보만 덧붙인다.

        새 발언이나 원문 판본은 만들지 않는다. 조립된 발언의 첫 key가 다른 원조각에서
        시작할 수도 있으므로 저장된 raw.segments의 정확한 조각 key로 찾는다.
        """
        if (not isinstance(base, str) or not base or not isinstance(revision, dict)
                or revision.get("source_key") != base or revision.get("reason") not in {"expired_source", "late_repartition"}
                or not isinstance(revision.get("parts"), list) or not revision["parts"]
                or any(not isinstance(part, dict) or not isinstance(part.get("text"), str)
                       for part in revision["parts"])):
            return []
        prefix = base + "~"
        rows = self.db.query(
            "SELECT DISTINCT s.id,s.source_key,s.raw FROM segments s, "
            "json_each(CASE WHEN json_valid(s.raw) THEN s.raw ELSE '{}' END,'$.segments') piece "
            "WHERE s.session_id=? AND substr(CAST(piece.key AS TEXT),1,?)=? ORDER BY s.id",
            (sid, len(prefix), prefix))
        changed: list[str] = []
        for row in rows:
            raw = json.loads(row["raw"])
            pieces = raw.get("segments") if isinstance(raw, dict) else None
            if (not isinstance(pieces, dict) or not any(
                    key.startswith(prefix) and re.fullmatch(r"[0-9]+", key[len(prefix):])
                    for key in pieces) or raw.get("speaker_revision") == revision):
                continue
            raw["speaker_revision"] = revision
            self.db.execute("UPDATE segments SET raw=?,revision=revision+1,updated_at=? WHERE id=?",
                            (_j(raw), now_ms(), row["id"]))
            changed.append(row["source_key"])
        return changed

    # ------------------------------------------------------------------ utterances
    def get_utterance(self, uid: int) -> dict | None:
        return _decode("utterances", self.db.query_one("SELECT * FROM utterances WHERE id=?", (uid,)))

    def get_utterance_by_key(self, sid: str, key: str) -> dict | None:
        return _decode("utterances", self.db.query_one(
            "SELECT * FROM utterances WHERE session_id=? AND source_key=?", (sid, key)))

    def upsert_utterance(self, sid: str, key: str, *, ts_start: int, ts_end: int | None, text: str,
                         is_final: bool, speaker: str | None = None, speaker_role: str | None = None,
                         speaker_source: str | None = None, speaker_hint: str | None = None,
                         new_speaker: bool = False) -> tuple[dict, bool]:
        """(행, 새로 생성 여부). 이미 확정(is_final)된 발언은 텍스트를 덮어쓰지 않는다(수동 교정 보호)."""
        row, created, _changed = self.upsert_utterance_tracked(
            sid, key, ts_start=ts_start, ts_end=ts_end, text=text, is_final=is_final, speaker=speaker,
            speaker_role=speaker_role, speaker_source=speaker_source, speaker_hint=speaker_hint,
            new_speaker=new_speaker)
        return row, created

    def upsert_utterance_tracked(self, sid: str, key: str, *, ts_start: int, ts_end: int | None, text: str,
                                 is_final: bool, speaker: str | None = None, speaker_role: str | None = None,
                                 speaker_source: str | None = None, speaker_hint: str | None = None,
                                 new_speaker: bool = False, source_order: str = "", turn_id: str = "",
                                 boundary_reason: str = "", source_speaker_hint: str = "") -> tuple[dict, bool, bool]:
        """upsert_utterance 와 같되 (행, 새로 생성 여부, 기존 행의 본문이 바뀌었는지)를 돌려준다.

        분석이 끝난 발언(예: 45초 강제 확정)의 본문이 늦게 온 확정 결과로 바뀌면 파이프라인이 다시 맞춰야 한다.
        """
        existing = self.get_utterance_by_key(sid, key)
        t = now_ms()
        if existing is None:
            seq_row = self.db.query_one("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM utterances WHERE session_id=?",
                                        (sid,))
            uid = self.db.insert(
                "INSERT INTO utterances(session_id,seq,source_key,ts_start,ts_end,speaker,speaker_role,speaker_source,"
                "text,original_text,speaker_hint,new_speaker,is_final,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, seq_row["s"], key, ts_start, ts_end, speaker or "", speaker_role or "unknown",
                 speaker_source or "inferred", text, text, speaker_hint or "", int(new_speaker), int(is_final), t, t),
            )
            row = self.update_utterance(uid, source_order=source_order, turn_id=turn_id,
                                        boundary_reason=boundary_reason, source_speaker_hint=source_speaker_hint)
            if is_final:
                self.invalidate_context_reviews(sid, uid)
            return row, True, False  # type: ignore[return-value]
        if existing["text"] != existing["original_text"]:
            # 사람이 본문을 교정한 발언은 수집원 갱신으로 되돌리지 않는다. 확정 신호만 받아들인다.
            if is_final and not existing["is_final"]:
                return self.update_utterance(existing["id"], is_final=1), False, False  # type: ignore[return-value]
            return existing, False, False
        if existing["is_final"] and not is_final:
            # 확정 뒤에 늦게 도착한 부분 결과는 버린다
            return existing, False, False
        fields: dict[str, Any] = {"ts_end": ts_end, "text": text, "original_text": text,
                                  "is_final": int(is_final or bool(existing["is_final"])),
                                  "speaker_hint": speaker_hint or "", "new_speaker": int(new_speaker)}
        for name, value in (("source_order", source_order), ("turn_id", turn_id),
                            ("boundary_reason", boundary_reason), ("source_speaker_hint", source_speaker_hint)):
            if value:
                fields[name] = value
        metadata_changed = any(existing.get(k) != fields[k] for k in (
            "speaker_hint", "new_speaker", "source_order", "turn_id", "boundary_reason", "source_speaker_hint")
            if k in fields)
        if existing["text"] != text or metadata_changed:
            fields["review_parts"] = []
        if speaker is not None and existing["speaker_source"] != "manual":
            fields["speaker"] = speaker
        if speaker_role is not None and existing["speaker_source"] != "manual":
            fields["speaker_role"] = speaker_role
        if speaker_source is not None and existing["speaker_source"] != "manual":
            fields["speaker_source"] = speaker_source
        row = self.update_utterance(existing["id"], **fields)
        return row, False, existing["text"] != text or metadata_changed  # type: ignore[return-value]

    def update_utterance(self, uid: int, **fields: Any) -> dict | None:
        allowed = {"block_id", "ts_start", "ts_end", "speaker", "speaker_role", "speaker_source", "text",
                   "original_text", "speaker_hint", "new_speaker", "is_final", "analyzed", "mentions",
                   "source_order", "turn_id", "boundary_reason", "review_parts", "source_speaker_hint"}
        old = self.get_utterance(uid)
        semantic = {"text", "original_text", "speaker", "speaker_role", "speaker_source", "speaker_hint",
                    "source_speaker_hint", "new_speaker", "block_id", "source_order"}
        semantic.update({"turn_id", "boundary_reason"})
        actual_changed = bool(old and any(old.get(k) != v for k, v in fields.items() if k in semantic))
        context_changed = actual_changed or bool(old and any(old.get(k) != fields[k]
            for k in ("ts_start", "ts_end", "is_final") if k in fields))
        if actual_changed and old.get("block_id"):
            # 바뀐 행을 사용한 판본 전체를 해제한다. 무관한 창의 채택은 유지한다.
            self.invalidate_block_reviews(old["block_id"], changed_uids={uid})
        if "review_parts" not in fields and actual_changed:
            fields["review_parts"] = []
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"수정할 수 없는 필드: {k}")
            if k in ("mentions", "review_parts"):
                v = _j(v)
            sets.append(f"{k}=?")
            params.append(v)
        if sets:
            sets.append("updated_at=?")
            params += [now_ms(), uid]
            self.db.execute(f"UPDATE utterances SET {', '.join(sets)} WHERE id=?", tuple(params))
        if old and context_changed:
            self.invalidate_context_reviews(old["session_id"], uid)
        return self.get_utterance(uid)

    def take_review_invalidations(self, sid: str) -> list[dict]:
        """판본 전체 무효화의 갱신 행을 세션 채널에 전달한다."""
        rows = []
        for uid in list(self._review_invalidations):
            u = self.get_utterance(uid)
            if u is None or u["session_id"] == sid:
                self._review_invalidations.discard(uid)
                if u:
                    rows.append(u)
        return rows

    def take_review_state_invalidations(self, sid: str) -> list[dict]:
        """원문 변경으로 폐기된 검토 상태도 화면 캐시에 전파한다."""
        rows = []
        for rid in list(self._review_state_invalidations):
            review = self.get_transcript_review(rid)
            if review is None or review["session_id"] == sid:
                self._review_state_invalidations.discard(rid)
                if review:
                    rows.append(review)
        return rows

    def review_target_ids(self, review: dict) -> set[int]:
        """판본이 표시한 원문 범위를 구한다. 기존 질의 판본은 블록 전체다."""
        scope = (review.get("payload") or {}).get("scope") or {}
        if isinstance(scope, dict) and scope.get("kind") == "window":
            return {uid for uid in scope.get("utterance_ids") or []
                    if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0}
        return {u["id"] for u in self.list_utterances(review["session_id"], block_id=review["block_id"])}

    def _clear_review_parts(self, ids: set[int]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        params = tuple(sorted(ids))
        affected = self.db.query(f"SELECT id FROM utterances WHERE id IN ({marks}) AND review_parts<>'[]'", params)
        self._review_invalidations.update(u["id"] for u in affected)
        self.db.execute(f"UPDATE utterances SET review_parts='[]',updated_at=? WHERE id IN ({marks}) AND review_parts<>'[]'",
                        (now_ms(), *params))

    def invalidate_block_reviews(self, bid: int, *, changed_uids: set[int] | None = None,
                                 preserve_accepted: bool = False) -> None:
        reviews = _decode_all("transcript_reviews", self.db.query(
            "SELECT * FROM transcript_reviews WHERE block_id=? AND status IN ('accepted','draft','held')", (bid,)))
        affected: set[int] = set()
        for review in reviews:
            if preserve_accepted and review["status"] == "accepted":
                continue
            targets = self.review_target_ids(review)
            scope = (review.get("payload") or {}).get("scope") or {}
            context = set(scope.get("context_ids") or []) if isinstance(scope, dict) else set()
            if changed_uids is not None and not changed_uids.intersection(targets | context):
                continue
            if review["status"] == "accepted":
                affected.update(targets)
            self.set_transcript_review_status(review["id"], "stale")
        if changed_uids is None and not preserve_accepted:
            affected.update(u["id"] for u in self.db.query("SELECT id FROM utterances WHERE block_id=?", (bid,)))
        self._clear_review_parts(affected)

    def invalidate_context_reviews(self, sid: str, uid: int) -> None:
        """다른 블록의 앞뒤 세 발언 문맥도 바뀐 판본만 즉시 무효화한다."""
        if not self.db.query_one("SELECT 1 FROM transcript_reviews WHERE session_id=? "
            "AND status IN ('draft','held','accepted') AND "
            "json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.scope.context_radius') IN (2,3) LIMIT 1", (sid,)):
            return
        row = self.get_utterance(uid)
        if not row or row.get("session_id") != sid:
            return
        # 새 확정 행이 중간에 끼면 예전 범위에는 그 ID가 없다. 인접 대상도 함께 검사한다.
        adjacent = {uid, *(item["id"] for item in self.context_utterances(uid, radius=3))}
        marks = ",".join("?" for _ in adjacent)
        valid = "CASE WHEN json_valid(r.payload) THEN r.payload ELSE '{}' END"
        candidates = _decode_all("transcript_reviews", self.db.query(
            f"SELECT r.* FROM transcript_reviews r WHERE r.session_id=? "
            "AND r.status IN ('draft','held','accepted') "
            f"AND json_extract({valid},'$.scope.context_radius') IN (2,3) AND ("
            f"EXISTS (SELECT 1 FROM json_each({valid},'$.scope.context_ids') WHERE value=?) OR "
            f"EXISTS (SELECT 1 FROM json_each({valid},'$.scope.utterance_ids') WHERE value IN ({marks})))",
            (sid, uid, *sorted(adjacent))))
        if not candidates:
            return
        from .transcript_review import snapshot_hash, take_window_snapshot
        affected = set()
        for review in candidates:
            scope = review["payload"].get("scope") or {}
            try:
                snapshot = take_window_snapshot(self, sid, review["block_id"], scope.get("utterance_ids") or [],
                    scope.get("context_ids") or [], context_radius=scope["context_radius"])
                if snapshot_hash(snapshot) == review["input_hash"]:
                    continue
            except (KeyError, TypeError, ValueError):
                pass
            if review["status"] == "accepted":
                for target in self.review_target_ids(review):
                    current = self.get_utterance(target)
                    parts = (current or {}).get("review_parts") or []
                    expected = []
                    for part in review["payload"].get("turns") or []:
                        if part.get("utterance_id") != target:
                            continue
                        exported = {key: part[key] for key in ("start", "end", "role", "speaker") if key in part}
                        if part.get("group_id"):
                            exported["group_id"] = f"r{review['id']}:{part['group_id']}"
                        expected.append(exported)
                    # 후속 사람 수정은 남기고 이 채택 판본이 만든 표시 부분만 해제한다.
                    if (current and current.get("speaker_source") != "manual"
                            and current.get("text") == current.get("original_text")
                            and parts and parts == expected):
                        affected.add(target)
            self.set_transcript_review_status(review["id"], "stale")
        self._clear_review_parts(affected)

    def source_block_ids(self, sid: str, key: str) -> list[int]:
        return [r["block_id"] for r in self.db.query(
            "SELECT DISTINCT block_id FROM utterances WHERE session_id=? AND source_key>? AND source_key<? "
            "AND block_id IS NOT NULL", (sid, f"{key}#", f"{key}$"))]

    def source_utterance_ids(self, sid: str, key: str) -> set[int]:
        return {r["id"] for r in self.db.query(
            "SELECT id FROM utterances WHERE session_id=? AND source_key>? AND source_key<?",
            (sid, f"{key}#", f"{key}$"))}

    def list_utterances(self, sid: str, *, block_id: int | None = None, final_only: bool = False,
                        since_seq: int = 0, limit: int = 5000, speaker: str | None = None) -> list[dict]:
        sql = "SELECT * FROM utterances WHERE session_id=? AND seq>?"
        params: list[Any] = [sid, since_seq]
        if block_id is not None:
            sql += " AND block_id=?"
            params.append(block_id)
        if final_only:
            sql += " AND is_final=1"
        if speaker:
            sql += " AND speaker=?"
            params.append(speaker)
        sql += " ORDER BY ts_start, COALESCE(NULLIF(source_order,''),printf('%012d',seq)), seq LIMIT ?"
        params.append(limit)
        return _decode_all("utterances", self.db.query(sql, tuple(params)))

    def neighbor_utterances(self, uid: int, *, final_only: bool = True) -> tuple[dict | None, dict | None]:
        """기본은 확정 행이며 실제 인접성 검사에서는 미확정 행도 포함한다."""
        u = self.get_utterance(uid)
        if not u:
            return None, None
        order = "COALESCE(NULLIF(source_order,''),printf('%012d',seq))"
        final = " AND is_final=1" if final_only else ""
        key = (u["session_id"], u["ts_start"], u.get("source_order") or f"{u['seq']:012d}", u["seq"])
        prev = self.db.query_one(
            f"SELECT * FROM utterances WHERE session_id=?{final} AND (ts_start,{order},seq)<(?,?,?) "
            f"ORDER BY ts_start DESC,{order} DESC,seq DESC LIMIT 1", key)
        nxt = self.db.query_one(
            f"SELECT * FROM utterances WHERE session_id=?{final} AND (ts_start,{order},seq)>(?,?,?) "
            f"ORDER BY ts_start,{order},seq LIMIT 1", key)
        return _decode("utterances", prev), _decode("utterances", nxt)

    def context_utterances(self, uid: int, *, radius: int = 2) -> list[dict]:
        """시스템·부분 자막을 제외한 실제 앞뒤 확정 발언을 한정 조회한다."""
        if type(radius) is not int or not 0 <= radius <= 3:
            raise ValueError("문맥 조회 반경은 0~3입니다")
        row = self.get_utterance(uid)
        if not row or not row.get("is_final") or row.get("speaker_role") == "system":
            return []
        order = "COALESCE(NULLIF(source_order,''),printf('%012d',seq))"
        key = (row["session_id"], row["ts_start"], row.get("source_order") or f"{row['seq']:012d}", row["seq"], radius)
        base = "SELECT * FROM utterances WHERE session_id=? AND is_final=1 AND COALESCE(speaker_role,'')!='system' "
        before = self.db.query(base + f"AND (ts_start,{order},seq)<(?,?,?) "
            f"ORDER BY ts_start DESC,{order} DESC,seq DESC LIMIT ?", key)
        after = self.db.query(base + f"AND (ts_start,{order},seq)>(?,?,?) "
            f"ORDER BY ts_start,{order},seq LIMIT ?", key)
        return _decode_all("utterances", list(reversed(before))) + [row] + _decode_all("utterances", after)

    def pending_final_utterances(self, sid: str) -> list[dict]:
        """확정됐지만 아직 분석(화자추정·추출)하지 않은 발언을 시간순으로."""
        return _decode_all("utterances", self.db.query(
            "SELECT * FROM utterances WHERE session_id=? AND is_final=1 AND analyzed=0 "
            "ORDER BY ts_start, COALESCE(NULLIF(source_order,''),printf('%012d',seq)), seq",
            (sid,)))

    # ------------------------------------------------------------------ 화자 검토
    def save_transcript_review(self, sid: str, bid: int, payload: dict) -> dict:
        """원문과 별도로 초안을 저장한다. 큰 결과는 저장 전에 거부한다."""
        encoded = _j(payload)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("화자 검토 결과가 저장 한도를 넘었습니다")
        t = now_ms()
        rid = self.db.insert(
            "INSERT INTO transcript_reviews(session_id,block_id,input_hash,status,payload,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)", (sid, bid, str(payload.get("input_hash") or ""),
            str(payload.get("status") or "held"), encoded, t, t))
        return self.get_transcript_review(rid)  # type: ignore[return-value]

    def get_transcript_review(self, rid: int) -> dict | None:
        return _decode("transcript_reviews", self.db.query_one(
            "SELECT * FROM transcript_reviews WHERE id=?", (rid,)))

    def presentation_block_ids(self, sid: str) -> list[int]:
        """창 검토가 많아도 완성 표시 초안의 모든 블록을 작은 목록으로 구한다."""
        return [row["block_id"] for row in self.db.query(
            "SELECT DISTINCT block_id FROM transcript_reviews WHERE session_id=? AND "
            "json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.presentation.ready')=1",
            (sid,))]

    def list_transcript_reviews(self, sid: str, bid: int | None = None) -> list[dict]:
        sql, params = "SELECT * FROM transcript_reviews WHERE session_id=?", [sid]
        if bid is not None:
            sql += " AND block_id=?"
            params.append(bid)
        return _decode_all("transcript_reviews", self.db.query(sql + " ORDER BY id DESC LIMIT 100", tuple(params)))

    def latest_block_transcript_review(self, sid: str, bid: int) -> dict | None:
        """창 초안 수와 무관하게 전체 판본의 최신 상태를 찾는다. 실패·거절도 포함한다."""
        return _decode("transcript_reviews", self.db.query_one(
            "SELECT * FROM transcript_reviews WHERE session_id=? AND block_id=? AND "
            "COALESCE(json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,"
            "'$.scope.kind'),'')<>'window' ORDER BY id DESC LIMIT 1", (sid, bid)))

    def accepted_transcript_review_by_hash(self, sid: str, bid: int, digest: str) -> dict | None:
        """이력 건수와 무관하게 같은 입력의 사람 채택 판본을 보호한다."""
        return _decode("transcript_reviews", self.db.query_one(
            "SELECT * FROM transcript_reviews WHERE session_id=? AND block_id=? "
            "AND input_hash=? AND status='accepted' ORDER BY id DESC LIMIT 1", (sid, bid, digest)))

    def latest_window_transcript_reviews(self, sid: str, bid: int) -> list[dict]:
        """대상 발언별 최신 창만 읽어 이력 100건 밖의 유효 초안도 유지한다.

        상태로 거르지 않아 새 실패·거절·stale가 이전 초안을 가린다. 여러 발언을
        묶은 이전 창도 남은 대상이 있으면 반환하여 호출자가 전체 겹침을 검사한다.
        손상된 대상 범위는 공개 단계에서 닫힌 상태로 처리하도록 함께 반환한다.
        """
        return _decode_all("transcript_reviews", self.db.query("""
            WITH candidates AS (
                SELECT id, CASE WHEN json_valid(payload) THEN payload ELSE '{}' END AS value
                FROM transcript_reviews WHERE session_id=? AND block_id=?
                AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,
                                 '$.scope.kind')='window'
            ), targets AS (
                SELECT candidates.id AS review_id, target.value AS utterance_id
                FROM candidates, json_each(candidates.value, '$.scope.utterance_ids') AS target
            ), newest AS (
                SELECT MAX(review_id) AS id FROM targets GROUP BY utterance_id
            )
            SELECT review.* FROM transcript_reviews AS review
            JOIN candidates ON candidates.id=review.id
            WHERE review.id IN (SELECT id FROM newest)
                OR COALESCE(json_type(candidates.value,'$.scope.utterance_ids'),'')<>'array'
                OR json_array_length(candidates.value,'$.scope.utterance_ids') NOT BETWEEN 1 AND 6
                OR EXISTS (SELECT 1 FROM json_each(candidates.value,'$.scope.utterance_ids') AS item
                           WHERE item.type<>'integer' OR item.value<1)
            ORDER BY review.id DESC
            """, (sid, bid)))

    def set_transcript_review_status(self, rid: int, status: str) -> dict | None:
        if status not in {"accepted", "rejected", "stale"}:
            raise ValueError("잘못된 화자 검토 상태입니다")
        self.db.execute("UPDATE transcript_reviews SET status=?,updated_at=? WHERE id=?", (status, now_ms(), rid))
        if status == "stale":
            self._review_state_invalidations.add(rid)
        return self.get_transcript_review(rid)

    def supersede_transcript_reviews(self, sid: str, bid: int, active: int, *, target_ids: set[int] | None = None) -> set[int]:
        """겹치는 채택 판본의 표시 범위 전체를 해제한다."""
        rows = _decode_all("transcript_reviews", self.db.query(
            "SELECT * FROM transcript_reviews WHERE session_id=? AND block_id=? AND status='accepted' AND id<>?",
            (sid, bid, active)))
        affected: set[int] = set()
        for row in rows:
            old_targets = self.review_target_ids(row)
            if target_ids is not None and not old_targets.intersection(target_ids):
                continue
            affected.update(old_targets)
            self.set_transcript_review_status(row["id"], "stale")
        self._clear_review_parts(affected)
        return affected

    def review_source_boundaries(self, sid: str, bid: int, *, rows: list[dict] | None = None) -> dict[str, list[dict]]:
        """늦은 국회 재분할 조각이 실제 원문에 있는 경우만 문자 위치를 연결한다."""
        out: dict[str, list[dict]] = {}
        for u in self.list_utterances(sid, block_id=bid) if rows is None else rows:
            key = str(u.get("source_key") or "").rpartition("#")[0]
            seg = _decode("segments", self.db.query_one(
                "SELECT raw FROM segments WHERE session_id=? AND source_key=?", (sid, key)))
            rev = ((seg or {}).get("raw") or {}).get("speaker_revision") or {}
            parts = rev.get("parts") or []
            if len(parts) < 2:
                continue
            cursor, boundaries = 0, []
            for part in parts:
                text = str(part.get("text") or "").strip()
                if not text:
                    boundaries = []
                    break
                pos = u["text"].find(text, cursor)
                if pos < 0 or u["text"].find(text, pos + 1) >= 0:
                    boundaries = []
                    break
                if pos > 0:
                    boundaries.append({"offset": pos, "reason": "국회 화자 전환 조각"})
                cursor = pos + len(text)
            if boundaries:
                out[str(u["id"])] = boundaries
        return out

    def review_utterances(self, sid: str, bid: int) -> list[dict]:
        """채택한 원문 범위 표시 판본을 요약 입력으로 확장한다. 근거 UID는 유지한다."""
        out = []
        for u in self.list_utterances(sid, block_id=bid, final_only=True):
            parts = u.get("review_parts") or []
            if not parts:
                out.append(u)
                continue
            for part in parts:
                out.append({**u, "text": u["text"][part["start"]:part["end"]],
                            "speaker_role": part["role"], "speaker": part.get("speaker") or "",
                            "speaker_source": "reviewed"})
        return out

    def save_audio_review_evidence(self, sid: str, uid: int, payload: dict) -> dict:
        if len(_j(payload).encode("utf-8")) > 100_000:
            raise ValueError("음성 대조 결과가 한도를 넘었습니다")
        rid = self.db.insert("INSERT INTO audio_review_evidence(session_id,utterance_id,payload,created_at) "
                             "VALUES(?,?,?,?)", (sid, uid, _j(payload), now_ms()))
        return _decode("audio_review_evidence", self.db.query_one("SELECT * FROM audio_review_evidence WHERE id=?", (rid,)))

    def list_audio_review_evidence(self, sid: str, uid: int | None = None) -> list[dict]:
        sql, params = "SELECT * FROM audio_review_evidence WHERE session_id=?", [sid]
        if uid is not None:
            sql += " AND utterance_id=?"
            params.append(uid)
        return _decode_all("audio_review_evidence", self.db.query(sql + " ORDER BY id DESC LIMIT 100", tuple(params)))

    def get_audio_review_evidence(self, eid: int) -> dict | None:
        return _decode("audio_review_evidence", self.db.query_one("SELECT * FROM audio_review_evidence WHERE id=?", (eid,)))

    def search_utterances(self, q: str, *, session_id: str | None = None, speaker: str | None = None,
                          limit: int = 50, offset: int = 0) -> list[dict]:
        q = q.strip()
        if not q:
            return []
        filters, params = [], []
        if session_id:
            filters.append("u.session_id=?")
            params.append(session_id)
        if speaker:
            filters.append("u.speaker=?")
            params.append(speaker)
        where = (" AND " + " AND ".join(filters)) if filters else ""
        tokens = [t for t in q.split() if t]
        short = [t for t in tokens if len(t) < 3]
        long_ = [t for t in tokens if len(t) >= 3]
        if len(q) >= 3 and long_:
            # trigram FTS: 3글자 이상 토큰을 AND 로 묶고 각 토큰을 구문(phrase)으로 감싼다(짧은 토큰은 LIKE)
            match = " AND ".join('"' + t.replace('"', '""') + '"' for t in long_)
            like_sql = "".join(" AND u.text LIKE ? ESCAPE '\\'" for _ in short)
            sql = ("SELECT u.*, s.title AS session_title, s.meeting_date FROM utterances_fts f "
                   "JOIN utterances u ON u.id=f.rowid JOIN sessions s ON s.id=u.session_id "
                   f"WHERE utterances_fts MATCH ?{like_sql}{where} ORDER BY u.ts_start DESC LIMIT ? OFFSET ?")
            rows = self.db.query(sql, tuple([match] + [_like(t) for t in short] + params + [limit, offset]))
        else:
            # 짧은 질의 또는 모든 단어가 2글자 이하('자료 제출'): 단어마다 LIKE 를 AND 로(FTS 경로와 같은 뜻)
            like_sql = " AND ".join("u.text LIKE ? ESCAPE '\\'" for _ in tokens)
            sql = ("SELECT u.*, s.title AS session_title, s.meeting_date FROM utterances u "
                   f"JOIN sessions s ON s.id=u.session_id WHERE {like_sql}{where} "
                   "ORDER BY u.ts_start DESC LIMIT ? OFFSET ?")
            rows = self.db.query(sql, tuple([_like(t) for t in tokens] + params + [limit, offset]))
        return _decode_all("utterances", rows)

    # ------------------------------------------------------------------ blocks
    def create_block(self, sid: str, *, kind: str, ts_start: int, questioner: str = "", questioner_party: str = "",
                     status: str = "ai_inferred", name_basis: str = "") -> dict:
        t = now_ms()
        bid = self.db.insert(
            "INSERT INTO blocks(session_id,kind,questioner,questioner_party,status,name_basis,ts_start,ts_end,"
            "is_open,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
            (sid, kind, questioner, questioner_party, status, name_basis, ts_start, ts_start, t, t),
        )
        return self.get_block(bid)  # type: ignore[return-value]

    def get_block(self, bid: int) -> dict | None:
        return self.stage_filter_block_summary(_decode("blocks", self.db.query_one("SELECT * FROM blocks WHERE id=?", (bid,))))

    def list_blocks(self, sid: str, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.db.query("SELECT * FROM blocks WHERE session_id=? AND kind=? ORDER BY ts_start, id", (sid, kind))
        else:
            rows = self.db.query("SELECT * FROM blocks WHERE session_id=? ORDER BY ts_start, id", (sid,))
        return [self.stage_filter_block_summary(row) for row in _decode_all("blocks", rows)]

    def update_block(self, bid: int, **fields: Any) -> dict | None:
        allowed = {"kind", "questioner", "questioner_party", "status", "name_basis", "ts_start", "ts_end", "is_open",
                   "summary", "summary_at", "review_status", "reviewed_by", "reviewed_at"}
        old = self.get_block(bid)
        changed = bool(old and any(old.get(k) != v for k, v in fields.items()
                                  if k in {"kind", "questioner", "review_status"}))
        if old and "summary" in fields:
            before, after = old.get("summary"), fields["summary"]
            changed = changed or (bool(isinstance(before, dict) and before.get("edited_by"))
                                   != bool(isinstance(after, dict) and after.get("edited_by")))
        if changed:
            self.invalidate_block_reviews(bid)
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"수정할 수 없는 필드: {k}")
            if k == "summary" and not isinstance(v, str):
                v = _j(v)
            sets.append(f"{k}=?")
            params.append(v)
        if sets:
            sets.append("updated_at=?")
            params += [now_ms(), bid]
            self.db.execute(f"UPDATE blocks SET {', '.join(sets)} WHERE id=?", tuple(params))
        return self.get_block(bid)

    # ------------------------------------------------------------------ items
    # 상태 'superseded' = 재추출·재요약으로 대체된 항목. 화면·통계·내보내기에 나오지 않는 내부 상태다
    # (하드 삭제하면 같은 id 가 재사용되어 이미 내려받은 화면·링크가 다른 항목을 가리킨다).
    _ITEM_SELECT = ("SELECT i.*, s.title AS session_title, s.meeting_date, u.ts_start AS utt_ts FROM items i "
                    "JOIN sessions s ON s.id=i.session_id LEFT JOIN utterances u ON u.id=i.utterance_id")

    def add_item(self, sid: str, *, kind: str, text: str, quote: str = "", speaker: str = "", requester: str = "",
                 block_id: int | None = None, utterance_id: int | None = None, origin: str = "rule",
                 confidence: float = 0.6, assignee_dept: str = "", strength: str = "", due_date: str = "",
                 dedupe: bool = True) -> tuple[dict, bool]:
        """(행, 새로 생성 여부). 수동 생성(origin='manual')은 처음부터 '사람이 손댄 항목'(edited=1)이다."""
        fp = fingerprint(kind, str(utterance_id or ""), quote or text) if dedupe else ""
        if fp:
            ex = self.db.query_one("SELECT * FROM items WHERE session_id=? AND fingerprint=?", (sid, fp))
            if ex:
                return ex, False
        t = now_ms()
        iid = self.db.insert(
            "INSERT INTO items(session_id,block_id,utterance_id,kind,strength,text,quote,speaker,requester,assignee_dept,"
            "due_date,status,origin,confidence,fingerprint,edited,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?)",
            (sid, block_id, utterance_id, kind, strength, text, quote, speaker, requester, assignee_dept, due_date,
             origin, confidence, fp, int(origin == "manual"), t, t),
        )
        return self.get_item(iid), True  # type: ignore[return-value]

    def get_item(self, iid: int) -> dict | None:
        return self.db.query_one("SELECT * FROM items WHERE id=?", (iid,))

    def get_item_full(self, iid: int) -> dict | None:
        """list_items 와 같은 모양(세션 제목·발언 시각 포함)의 항목 한 건(serialize.item 용)."""
        return self.db.query_one(self._ITEM_SELECT + " WHERE i.id=?", (iid,))

    def update_item(self, iid: int, *, by_human: bool = False, **fields: Any) -> dict | None:
        """항목 수정. by_human=True(운영자 PATCH·수동 생성)면 edited=1 도 함께 둔다 —
        사람이 손댄 항목은 규칙 재추출·AI 재요약·질의자 변경에 따른 요구 위원 갱신이 덮어쓰지 않는다."""
        allowed = {"kind", "strength", "text", "quote", "speaker", "requester", "assignee_dept", "due_date", "status",
                   "note", "block_id"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"수정할 수 없는 필드: {k}")
            sets.append(f"{k}=?")
            params.append(v)
        if by_human:
            sets.append("edited=1")
        if sets:
            sets.append("updated_at=?")
            params += [now_ms(), iid]
            self.db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", tuple(params))
        return self.get_item(iid)

    def supersede_items(self, ids: Iterable[int]) -> None:
        """재추출·재요약으로 대체된 항목을 숨긴다(status='superseded'). 하드 삭제 대신 — id 재사용 방지.

        지문(fingerprint)은 비워 같은 내용이 다시 추출되면 새 항목으로 들어갈 수 있게 한다.
        """
        ids = [int(i) for i in ids]
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.db.execute(f"UPDATE items SET status='superseded', fingerprint='', updated_at=? WHERE id IN ({marks})",
                        tuple([now_ms()] + ids))

    def items_for_utterance(self, uid: int, origin: str | None = None) -> list[dict]:
        """발언에 딸린 항목(대체된 항목 제외, 제외(dismissed) 포함)."""
        sql = "SELECT * FROM items WHERE utterance_id=? AND status<>'superseded'"
        params: list[Any] = [uid]
        if origin:
            sql += " AND origin=?"
            params.append(origin)
        return self.db.query(sql + " ORDER BY id", tuple(params))

    def list_items(self, sid: str | None = None, *, kind: str | None = None, status: str | None = None,
                   block_id: int | None = None, dept: str | None = None, include_dismissed: bool = False,
                   limit: int = 2000) -> list[dict]:
        """항목 목록. 'superseded'(대체됨)는 어떤 조건에서도 나오지 않는다."""
        sql = self._ITEM_SELECT + " WHERE i.status<>'superseded'"
        params: list[Any] = []
        if sid:
            sql += " AND i.session_id=?"
            params.append(sid)
        if kind:
            sql += " AND i.kind=?"
            params.append(kind)
        if status:
            sql += " AND i.status=?"
            params.append(status)
        elif not include_dismissed:
            sql += " AND i.status<>'dismissed'"
        if block_id is not None:
            sql += " AND i.block_id=?"
            params.append(block_id)
        if dept:
            sql += " AND i.assignee_dept=?"
            params.append(dept)
        sql += " ORDER BY COALESCE(u.ts_start, i.created_at), i.id LIMIT ?"
        params.append(limit)
        return self.db.query(sql, tuple(params))

    def delete_items_for_block(self, block_id: int, origin: str) -> list[int]:
        """이 블록에서 사람이 손대지 않은 미처리 항목(edited=0, status='open', 메모 없음)을 대체 처리한다.

        이름은 예전 그대로지만 하드 삭제하지 않는다(supersede_items). 대체한 id 목록을 돌려준다.
        """
        rows = self.db.query("SELECT id FROM items WHERE block_id=? AND origin=? AND status='open' AND edited=0 "
                             "AND note=''", (block_id, origin))
        ids = [r["id"] for r in rows]
        self.supersede_items(ids)
        return ids

    # ------------------------------------------------------------------ summaries
    def add_summary(self, sid: str, scope: str, content: dict, provider: str = "", model: str = "") -> dict:
        sid_ = self.db.insert(
            "INSERT INTO summaries(session_id,scope,content,provider,model,created_at) VALUES(?,?,?,?,?,?)",
            (sid, scope, _j(content), provider, model, now_ms()))
        return _decode("summaries", self.db.query_one("SELECT * FROM summaries WHERE id=?", (sid_,)))  # type: ignore

    def latest_summary(self, sid: str, scope: str) -> dict | None:
        return _decode("summaries", self.db.query_one(
            "SELECT * FROM summaries WHERE session_id=? AND scope=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (sid, scope)))

    # ------------------------------------------------------------------ alerts
    def add_alert(self, sid: str, utterance_id: int, keyword: str, category: str) -> tuple[dict | None, bool]:
        try:
            aid = self.db.insert(
                "INSERT INTO alerts(session_id,utterance_id,keyword,category,created_at) VALUES(?,?,?,?,?)",
                (sid, utterance_id, keyword, category, now_ms()))
        except Exception:  # UNIQUE 위반 = 이미 알림
            return None, False
        return self.db.query_one("SELECT * FROM alerts WHERE id=?", (aid,)), True

    def prune_alerts(self, utterance_id: int, keep: Iterable[str]) -> list[int]:
        """본문 교정으로 사라진 감시 키워드의 미확인 알림을 지운다(확인한 알림은 기록으로 남긴다). 지운 id 목록."""
        keep = set(keep)
        gone = [r["id"] for r in self.db.query("SELECT id, keyword FROM alerts WHERE utterance_id=? AND ack=0",
                                               (utterance_id,)) if r["keyword"] not in keep]
        for aid in gone:
            self.db.execute("DELETE FROM alerts WHERE id=?", (aid,))
        return gone

    def list_alerts(self, sid: str, unack_only: bool = False, limit: int = 500) -> list[dict]:
        sql = ("SELECT a.*, u.text, u.speaker, u.ts_start FROM alerts a JOIN utterances u ON u.id=a.utterance_id "
               "WHERE a.session_id=?")
        if unack_only:
            sql += " AND a.ack=0"
        sql += " ORDER BY u.ts_start DESC LIMIT ?"
        return self.db.query(sql, (sid, limit))

    def ack_alerts(self, sid: str, alert_id: int | None = None) -> None:
        if alert_id is None:
            self.db.execute("UPDATE alerts SET ack=1 WHERE session_id=?", (sid,))
        else:
            self.db.execute("UPDATE alerts SET ack=1 WHERE session_id=? AND id=?", (sid, alert_id))

    # ------------------------------------------------------------------ bookmarks
    def toggle_bookmark(self, sid: str, uid: int, user: str, note: str = "") -> bool:
        ex = self.db.query_one("SELECT id FROM bookmarks WHERE utterance_id=? AND created_by=?", (uid, user))
        if ex:
            self.db.execute("DELETE FROM bookmarks WHERE id=?", (ex["id"],))
            return False
        self.db.insert("INSERT INTO bookmarks(session_id,utterance_id,note,created_by,created_at) VALUES(?,?,?,?,?)",
                       (sid, uid, note, user, now_ms()))
        return True

    def list_bookmarks(self, sid: str, user: str | None = None) -> list[dict]:
        sql = ("SELECT b.*, u.text, u.speaker, u.ts_start FROM bookmarks b JOIN utterances u ON u.id=b.utterance_id "
               "WHERE b.session_id=?")
        params: list[Any] = [sid]
        if user is not None:
            sql += " AND b.created_by=?"
            params.append(user)
        return self.db.query(sql + " ORDER BY u.ts_start", tuple(params))

    # ------------------------------------------------------------------ settings & audit
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, _j(value)))

    def audit(self, actor: str, action: str, target: str = "", detail: Any = "") -> None:
        if not isinstance(detail, str):
            detail = _j(detail)
        self.db.execute("INSERT INTO audit_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                        (now_ms(), actor, action, target, detail))

    def list_audit(self, limit: int = 300) -> list[dict]:
        return self.db.query("SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
