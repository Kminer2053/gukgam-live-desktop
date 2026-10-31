"""원수신 단위와 파생 판본의 저장. 운영 발언·사람 수정은 변경하지 않는다."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

from .db import now_ms


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _source(row):
    if row is None:
        return None
    return {**json.loads(row["payload"]), **{k: v for k, v in row.items() if k != "payload"},
            "final": bool(row["final"]), "scd": bool(row["scd"])}


def _document(row):
    if row is None:
        return None
    return {**json.loads(row["payload"]), **{k: v for k, v in row.items() if k != "payload"}}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _block(row):
    """공개 요약 필터를 재호출하지 않고 저장된 블록을 읽는다."""
    if row is not None and isinstance(row.get("summary"), str):
        try:
            row["summary"] = json.loads(row["summary"]) if row["summary"] else None
        except json.JSONDecodeError:
            pass
    return row


class StageRepoMixin:
    def enable_recording_stage_display(self, sid: str) -> None:
        """요청받은 파일만 새 표시본 대상으로 삼고 원문·음향 행을 변경하지 않는다."""
        if (self.get_session(sid) or {}).get("source_type") != "file":
            return
        self.db.execute("INSERT INTO display_state(session_id,input_generation,built_generation) VALUES(?,1,0) "
                        "ON CONFLICT(session_id) DO NOTHING", (sid,))

    def all_stage_sources(self, sid: str) -> list[dict]:
        if (self.get_session(sid) or {}).get("source_type") == "file":
            from .recording_sources import recording_sources
            return recording_sources(self.list_utterances(sid, limit=-1))
        return self.all_caption_sources(sid)

    def stage_sources_by_ids(self, sid: str, ids: list[int]) -> list[dict]:
        if (self.get_session(sid) or {}).get("source_type") == "file":
            wanted = set(ids)
            return [s for s in self.all_stage_sources(sid) if s["id"] in wanted]
        return self.caption_sources_by_ids(sid, ids)

    def stage_block_headers(self, sid: str) -> list[dict]:
        """첫 화면에는 블록 목록과 사람 요약만 읽고 큰 자동 보정 본문은 펼칠 때 읽는다."""
        return [_block(row) for row in self.db.query(
            "SELECT id,session_id,kind,questioner,questioner_party,status,name_basis,ts_start,ts_end,is_open,"
            "summary_at,review_status,reviewed_by,reviewed_at,created_at,updated_at,"
            "CASE WHEN review_status='reviewed' OR json_extract(CASE WHEN json_valid(summary) THEN summary ELSE '{}' END,'$.edited_by') IS NOT NULL "
            "THEN summary ELSE '' END AS summary FROM blocks WHERE session_id=? ORDER BY ts_start,id", (sid,))]

    def stage_utterance_ids(self, sid: str) -> list[int]:
        """부분 재동기화의 삭제 권위는 작은 ID 목록으로만 전달한다."""
        return [row["id"] for row in self.db.query("SELECT id FROM utterances WHERE session_id=? ORDER BY id", (sid,))]

    def stage_utterances_by_ids(self, sid: str, ids: list[int]) -> list[dict]:
        """보정 출처의 옛 행도 삭제 없이 필요한 UID만 공개 읽기로 가져온다."""
        from .repo import _decode_all
        ids = sorted({value for value in ids if type(value) is int and value > 0})
        if not ids:
            return []
        return _decode_all("utterances", self.db.query(
            "SELECT * FROM utterances WHERE session_id=? AND id IN (" + ",".join("?" for _ in ids)
            + ") ORDER BY ts_start,COALESCE(NULLIF(source_order,''),printf('%012d',seq)),seq,id", (sid, *ids)))

    def stage_utterance_page(self, sid: str, *, limit: int = 100, before_id: int | None = None,
                             block_id: int | None = None, around_id: int | None = None,
                             bookmark_actor: str | None = None, query: str = "",
                             selected_ids: list[int] | None = None) -> tuple[list[dict], dict]:
        """명시된 최근·이전·질의·검색 범위만 읽는다. 커서는 표시 순서의 원행 UID다."""
        from .repo import _decode_all
        order = "COALESCE(NULLIF(source_order,''),printf('%012d',seq))"
        scope = "tail"
        where, args = ["session_id=?"], [sid]
        if around_id is not None:
            anchor = self.db.query_one("SELECT id,block_id FROM utterances WHERE session_id=? AND id=?", (sid, around_id))
            if not anchor:
                raise KeyError("발언을 찾을 수 없습니다")
            if anchor["block_id"] is not None:
                block_id = anchor["block_id"]
            else:
                where.append("id=?")
                args.append(around_id)
            scope = "around"
        if selected_ids is not None:
            where.append("id IN (" + ",".join("?" for _ in selected_ids) + ")" if selected_ids else "0")
            args.extend(selected_ids)
            scope = "ids"
        elif block_id is not None:
            where.append("block_id=?")
            args.append(block_id)
            scope = scope if around_id is not None else "block"
        elif bookmark_actor is not None:
            where.append("id IN (SELECT utterance_id FROM bookmarks WHERE session_id=? AND created_by=?)")
            args.extend([sid, bookmark_actor])
            scope = "bookmarks"
        elif query:
            terms = query.casefold().split()[:12]
            matched_blocks = []
            for document in self.list_stage_documents(sid):
                if document.get("mode") != "closed_block_v1" or document.get("status") not in {"draft", "partial", "ready"}:
                    continue
                body = document.get("paragraphs") or document.get("sentences") or []
                text = " ".join(str(p.get("text", "")) + " " + str(p.get("speaker", ""))
                    for p in body if isinstance(p, dict)).casefold()
                if all(term in text for term in terms) and self.stage_document_current(sid, document["id"]):
                    matched_blocks.append(document["block_id"])
            raw = " AND ".join("instr(lower(text || ' ' || speaker),lower(?))>0" for _ in terms)
            extra = " OR block_id IN (" + ",".join("?" for _ in matched_blocks) + ")" if matched_blocks else ""
            where.append("(" + raw + extra + ")")
            args.extend([*terms, *matched_blocks])
            scope = "search"
        elif before_id is not None:
            anchor = self.db.query_one(f"SELECT ts_start,{order} AS ord,seq,id FROM utterances WHERE session_id=? AND id=?", (sid, before_id))
            if not anchor:
                raise KeyError("발언을 찾을 수 없습니다")
            where.append(f"(ts_start,{order},seq,id)<(?,?,?,?)")
            args.extend([anchor["ts_start"], anchor["ord"], anchor["seq"], anchor["id"]])
            scope = "before"
        complete = scope in {"block", "around", "bookmarks", "search", "ids"}
        suffix = f" ORDER BY ts_start DESC,{order} DESC,seq DESC,id DESC"
        if not complete:
            suffix += " LIMIT ?"
            args.append(limit + 1)
        rows = self.db.query("SELECT * FROM utterances WHERE " + " AND ".join(where) + suffix, tuple(args))
        more = not complete and len(rows) > limit
        if more:
            rows = rows[:limit]
        rows = _decode_all("utterances", list(reversed(rows)))
        return rows, {"partial": True, "scope": scope, "next_before": rows[0]["id"] if more else None}

    def stage_block_record(self, sid: str, bid: int) -> dict | None:
        """수집 경로에서는 파생 요약 검증 없이 저장된 블록 한 행만 읽는다."""
        return _block(self.db.query_one("SELECT * FROM blocks WHERE session_id=? AND id=?", (sid, bid)))

    def stage_block_session(self, bid: int) -> str | None:
        """요약 요청의 대상 존재·소속만 단건으로 확인한다."""
        row = self.db.query_one("SELECT session_id FROM blocks WHERE id=?", (bid,))
        return row["session_id"] if row else None

    def stage_commit_result(self, sid: str, did: int, bid: int, payload: dict,
                            summary_value: dict | None = None):
        """호출자의 CAS 트랜잭션 안에서 파생 결과만 저장하고 재검증 없이 반환한다."""
        t = now_ms()
        self.db.execute("UPDATE transcript_stage_documents SET payload=?,status=?,updated_at=? WHERE session_id=? AND id=?",
                        (_json(payload), payload.get("status", "held"), t, sid, did))
        if summary_value is not None:
            self.db.execute("UPDATE blocks SET summary=?,summary_at=?,updated_at=? WHERE session_id=? AND id=?",
                            (_json(summary_value), t, t, sid, bid))
        document = _document(self.db.query_one(
            "SELECT * FROM transcript_stage_documents WHERE session_id=? AND id=?", (sid, did)))
        return document, self.stage_block_record(sid, bid) if summary_value is not None else None

    @staticmethod
    def stage_block_stamp(block: dict) -> str:
        return _digest({k: block.get(k) for k in
            ("id", "session_id", "kind", "questioner", "questioner_party", "ts_start", "ts_end", "is_open")})

    @staticmethod
    def stage_boundary_stamp(block: dict) -> str:
        """다음 진행 블록의 정상 종료는 앞 질의의 호명 근거를 바꾸지 않는다."""
        return _digest({k: block.get(k) for k in
            ("id", "session_id", "kind", "questioner", "questioner_party", "ts_start")})

    def stage_block_context(self, sid: str, block: dict) -> dict:
        """실행·표시·내보내기가 같은 공개 세션 메타 판본을 사용한다."""
        roster = self.get_roster(sid)
        session = self.get_session(sid) or {}
        return {"session_id": sid, "session_title": session.get("title") or "",
            "meeting_date": session.get("meeting_date") or "", "committee": session.get("committee") or "",
            "block_id": block["id"], "questioner": block.get("questioner") or "",
            "questioner_party": block.get("questioner_party") or "", "roster": roster,
            "chair": next((r["name"] for r in roster if r.get("role") == "chair"), ""),
            "agency": session.get("agencies", ""), "agencies": session.get("agencies", "")}

    def stage_document_protection(self, sid: str, sources: list[dict] | None = None,
                                  membership: dict | None = None) -> dict:
        """사람 판본은 보정 대상 membership으로 보호하고 과거 문서는 시각 기준을 유지한다."""
        if membership is not None:
            from .repo import _decode_all
            uids, bids = set(membership.get("utterance_ids") or []), set(membership.get("block_ids") or [])
            blocks = [_block(row) for row in self.db.query(
                "SELECT * FROM blocks WHERE session_id=? AND id IN (" + ",".join("?" for _ in bids)
                + ") ORDER BY ts_start,id", (sid, *bids))] if bids else []
            rows = _decode_all("utterances", self.db.query(
                "SELECT * FROM utterances WHERE session_id=? AND id IN (" + ",".join("?" for _ in uids)
                + ") ORDER BY ts_start,COALESCE(NULLIF(source_order,''),printf('%012d',seq)),seq",
                (sid, *uids))) if uids else []
        else:
            blocks = [_block(row) for row in self.db.query("SELECT * FROM blocks WHERE session_id=? ORDER BY ts_start,id", (sid,))]
            rows = self.list_utterances(sid, limit=-1)
        protected_blocks = [b for b in blocks if b.get("review_status") == "reviewed"
            or isinstance(b.get("summary"), dict) and b["summary"].get("edited_by")]
        protected = [r for r in rows if r.get("speaker_source") == "manual"
            or r.get("review_parts") or r.get("text") != r.get("original_text")]
        if membership is None and sources is not None:
            def overlaps(row):
                start, end = row.get("ts_start"), row.get("ts_end") or row.get("ts_start")
                return isinstance(start, int) and isinstance(end, int) and any(
                    start <= s["stable_at"] and end >= s["first_rx"] for s in sources)
            protected = [r for r in protected if overlaps(r)]
            protected_blocks = [b for b in protected_blocks if overlaps(b)]
        return {"rows": [{k: r.get(k) for k in
            ("id", "ts_start", "ts_end", "speaker", "speaker_role", "speaker_source", "text", "original_text", "review_parts")}
            for r in protected], "blocks": [{k: b.get(k) for k in
            ("id", "ts_start", "ts_end", "review_status", "summary", "questioner")} for b in protected_blocks],
            "roster": self.get_roster(sid)}

    def stage_membership_stamp(self, sid: str, membership: dict) -> str | None:
        """연결된 원발언의 재귀속·순서·확정·출처 변경도 동일 판본으로 취급하지 않는다."""
        uids, bids = membership.get("utterance_ids"), membership.get("block_ids")
        if (not isinstance(uids, list) or not uids or not isinstance(bids, list) or not bids
                or any(type(value) is not int or value <= 0 for value in [*uids, *bids])
                or len(uids) != len(set(uids)) or len(bids) != len(set(bids))):
            return None
        marks = ",".join("?" for _ in uids)
        rows = self.db.query("SELECT id,source_key,block_id,seq,is_final,original_text,ts_start,ts_end "
                             f"FROM utterances WHERE session_id=? AND id IN ({marks}) ORDER BY id", (sid, *uids))
        if len(rows) != len(uids):
            return None
        block_marks = ",".join("?" for _ in bids)
        # 기존 연결 행이 그대로여도 질의 안 새 확정 행이 들어오면 전체 문맥 판본이 달라진다.
        current_question_rows = self.db.query(
            "SELECT u.block_id,u.id FROM utterances u JOIN blocks b ON b.id=u.block_id "
            f"WHERE u.session_id=? AND b.kind='question' AND b.id IN ({block_marks}) "
            "AND u.speaker_role<>'system' ORDER BY u.block_id,u.id", (sid, *bids))
        # 같은 본문·UID로 다시 조립돼도 원조각 소속·순서가 달라지면 다른 입력이다.
        # 갱신 시각이나 무관한 수집 플래그 때문에 중복 수신을 새 판본으로 만들지는 않는다.
        keys = sorted({str(row.get("source_key") or "").rpartition("#")[0] for row in rows})
        key_marks = ",".join("?" for _ in keys)
        segments = {row["source_key"]: row for row in self.db.query(
            f"SELECT source_key,raw FROM segments WHERE session_id=? AND source_key IN ({key_marks})", (sid, *keys))}
        connections = []
        for key in keys:
            stored = segments.get(key)
            try:
                raw = json.loads(stored["raw"]) if stored and isinstance(stored.get("raw"), str) else (stored or {}).get("raw")
            except (TypeError, ValueError):
                raw = None
            parts = raw.get("segments") if isinstance(raw, dict) else None
            connections.append({"source_key": key, "exists": stored is not None,
                                "parts": list(parts.items()) if isinstance(parts, dict) else None})
        return _digest({"block_ids": sorted(bids), "rows": rows, "question_rows": current_question_rows,
                        "source_connections": connections})

    def stage_document_current(self, sid: str, did: int, *, block: dict | None = None) -> bool:
        """대상·경계 원문과 메타·사람 판본을 함께 읽기 검증한다."""
        document = self.get_stage_document(sid, did)
        if document and document.get("source_kind") == "recording_asr":
            from .recording_stages import document_current
            return document.get("status") != "stale" and document_current(self, sid, document, block)
        if not self.get_session(sid) or not self.stage_document_sources_current(did):
            return False
        document = self.get_stage_document(sid, did)
        if not document or document.get("mode") != "closed_block_v1":
            return False
        bid = document.get("block_id")
        if block is None:
            block = _block(self.db.query_one("SELECT * FROM blocks WHERE session_id=? AND id=?", (sid, bid)))
        if (not block or block.get("session_id") != sid or block.get("id") != bid
                or block.get("is_open") or block.get("kind") != "question"
                or self.stage_block_stamp(block) != document.get("block_stamp")
                or _digest(self.stage_block_context(sid, block)) != document.get("context_hash")):
            return False
        dependencies = document.get("dependency_blocks")
        if not isinstance(dependencies, dict):
            return False
        for dep_id, stamp in dependencies.items():
            if not str(dep_id).isdigit():
                return False
            row = self.db.query_one("SELECT * FROM blocks WHERE session_id=? AND id=?", (sid, int(dep_id)))
            if not row or self.stage_boundary_stamp(row) != stamp:
                return False
        membership = document.get("membership")
        if not isinstance(membership, dict) or not isinstance(membership.get("utterance_ids"), list) \
                or not isinstance(membership.get("block_ids"), list):
            return False
        membership_stamp = self.stage_membership_stamp(sid, membership)
        if not membership_stamp or membership_stamp != document.get("membership_hash"):
            return False
        selection = document.get("source_selection")
        if isinstance(selection, list) and selection:
            from .block_sources import selected_short_call_boundaries
            # 이전 경계 규칙으로 다른 의원의 자료요구까지 묶은 결과는 저장본을 지우지 않고 숨긴다.
            sources = self.caption_sources_by_ids(sid, document.get("source_ids") or [])
            try:
                if selected_short_call_boundaries(sources, selection, roster=self.get_roster(sid),
                        questioner=block.get("questioner") or ""):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        protection = self.stage_document_protection(sid, membership=membership)
        return _digest(protection) == document.get("protection_hash")

    def stage_filter_block_summary(self, block: dict | None) -> dict | None:
        """오래된 자동 요약은 공개 읽기에서 숨기며 저장 원본·사람 검수는 보존한다."""
        if not block:
            return block
        summary = block.get("summary")
        if not isinstance(summary, dict) or summary.get("edited_by") or block.get("review_status") == "reviewed":
            return block
        if not getattr(self, "_display_building", False) and self.display_initialized(block["session_id"]):
            # 변경 작업자가 저장한 동일 판본 요약만 읽는다. dirty일 때 옛 AI요약을 되살리지 않는다.
            current = self._display_saved_block(block["session_id"], block["id"])
            return {**block, "summary": current.get("summary") if current else None,
                    "summary_at": current.get("summary_at") if current else None}
        if summary.get("provider") != "closed_block":
            session = self.get_session(block["session_id"])
            if session and session.get("source_type") == "assembly_caption" and self.has_caption_sources(block["session_id"]):
                return {**block, "summary": None, "summary_at": None}
            return block
        did = summary.get("source_document_id")
        document = self.get_stage_document(block["session_id"], did) if type(did) is int else None
        if (document and document.get("status") in {"draft", "partial"}
                and (document.get("summary") or {}).get("status") == "draft"
                and self.stage_document_current(block["session_id"], did, block=block)):
            return {**block, "summary": self.stage_summary_value(document, summary)}
        return {**block, "summary": None, "summary_at": None}

    @staticmethod
    def stage_summary_value(document: dict, stored: dict) -> dict:
        """모든 요약 표시는 보정 본문과 같은 문서 요약에서 한 형태로 만든다."""
        source = document["summary"]
        summary = {**source, "one_line": source.get("title", ""), "question_points": source.get("bullets", []),
                   "provider": "closed_block", "source_document_id": document["id"]}
        # 저장 당시 표시 스키마는 유지한다. 검수 전후 단순 조회가 새 필드를 끼워 넣지 않는다.
        summary.update({key: [] for key in ("answer_points", "issues", "data_requests", "commitments") if key in stored})
        if "ai_draft" in stored:
            summary["ai_draft"] = True
        if "generated_at" in stored:
            summary["generated_at"] = stored["generated_at"]
        return summary

    def stage_block_summary_result(self, sid: str, bid: int) -> dict:
        """의원별·질의별 요청은 같은 종료 보정 문서의 저장 요약만 읽는다."""
        block = self.stage_block_record(sid, bid)
        if block is None:
            raise KeyError(f"블록을 찾을 수 없습니다: {bid}")
        stored = block.get("summary") if isinstance(block.get("summary"), dict) else None
        if block.get("review_status") == "reviewed" or stored and stored.get("edited_by"):
            return {"block": block, "summary": stored, "summary_state": {"state": "protected", "detail": "사람이 수정·검수한 요약을 유지합니다"}}
        if not getattr(self, "_display_building", False) and self.display_initialized(sid):
            # POST 요약 조회도 이미 선택된 표시본의 저장 요약을 공유한다. 새 모델 요청은 없다.
            payload = self.display_snapshot(sid)
            current = next((value for value in payload.get("blocks", []) if value["id"] == bid), None)
            summary = current.get("summary") if current else None
            return {"block": block, "summary": summary, "summary_state": {
                "state": "ready" if summary else "waiting",
                "source_document_id": summary.get("source_document_id") if summary else None,
                "detail": "저장된 공통 표시 판본의 요약입니다" if summary else "보정 요약을 기다리고 있습니다"}}
        document = self.get_stage_document(sid, stored.get("source_document_id")) \
            if stored and stored.get("provider") == "closed_block" and type(stored.get("source_document_id")) is int else None
        if document and document.get("block_id") == bid and document.get("mode") == "closed_block_v1" \
                and document.get("status") in {"draft", "partial"} \
                and (document.get("summary") or {}).get("status") == "draft" \
                and self.stage_document_current(sid, document["id"], block=block):
            summary = self.stage_summary_value(document, stored)
            return {"block": block, "summary": summary, "summary_state": {"state": "ready", "source_document_id": document["id"],
                    "detail": "최종 보정 원문과 같은 판본의 저장 요약을 사용합니다"}}
        latest = _document(self.db.query_one(
            "SELECT * FROM transcript_stage_documents WHERE session_id=? "
            "AND json_extract(payload,'$.mode')='closed_block_v1' AND json_extract(payload,'$.block_id')=? "
            "ORDER BY id DESC LIMIT 1", (sid, bid)))
        state = "processing" if latest and latest.get("status") == "processing" else "held" \
            if latest and latest.get("status") in {"failed", "held", "interrupted", "protected", "partial", "draft"} else "waiting"
        detail = {"processing": "최종 보정과 의원별 요약을 처리 중입니다", "held": "현재 판본의 보정 요약을 완료하지 못했습니다",
                  "waiting": "최종 보정 원문과 같은 판본의 요약을 기다리고 있습니다"}[state]
        return {"block": block, "summary": None, "summary_state": {"state": state, "detail": detail}}

    def _display_saved_block(self, sid: str, bid: int) -> dict | None:
        row = self.db.query_one(
            "SELECT v.payload FROM display_materializations m JOIN display_state s "
            "ON s.session_id=m.session_id JOIN display_values v ON v.session_id=m.session_id "
            "WHERE m.session_id=? AND m.generation=s.input_generation AND s.built_generation=s.input_generation "
            "AND v.kind='blocks' AND v.row_key=?", (sid, str(bid)))
        return json.loads(row["payload"]) if row else None

    def stage_raw_segments(self, sid: str) -> list[dict]:
        """블록 원문 소유권을 시간 추정 대신 조립 출처로 확인한다."""
        rows = self.db.query("SELECT * FROM segments WHERE session_id=? ORDER BY ts_start,id", (sid,))
        return [{**row, "raw": json.loads(row["raw"]) if isinstance(row.get("raw"), str) else row.get("raw") or {}}
                for row in rows]

    def stage_document_by_signature(self, sid: str, signature: str):
        """같은 종료 블록 판본은 실패를 포함해 자동으로 반복 호출하지 않는다."""
        return _document(self.db.query_one(
            "SELECT * FROM transcript_stage_documents WHERE session_id=? AND input_hash=? AND status<>'stale' "
            "ORDER BY id DESC LIMIT 1", (sid, signature)))

    def get_caption_source(self, sid: str, key: str):
        return _source(self.db.query_one("SELECT * FROM caption_sources WHERE session_id=? AND source_key=?", (sid, key)))

    def caption_sources_by_ids(self, sid: str, ids: list[int]):
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        return [_source(r) for r in self.db.query(
            f"SELECT * FROM caption_sources WHERE session_id=? AND id IN ({marks}) ORDER BY first_rx,id", (sid, *ids))]

    def list_caption_sources(self, sid: str, *, before: int | None = None, limit: int = 100):
        limit = min(max(int(limit), 1), 1000)
        rows = self.db.query("SELECT * FROM caption_sources WHERE session_id=?" +
                             (" AND id<?" if before is not None else "") + " ORDER BY id DESC LIMIT ?",
                             (sid, before, limit) if before is not None else (sid, limit))
        return [_source(r) for r in reversed(rows)]

    def all_caption_sources(self, sid: str):
        return [_source(r) for r in self.db.query("SELECT * FROM caption_sources WHERE session_id=? ORDER BY first_rx,id", (sid,))]

    def stage_caption_source_revisions(self, sid: str, source_ids: list[int] | None = None):
        """동일 원조각의 과거 판본을 출처 대체 증명용으로만 읽는다."""
        if source_ids is not None and not source_ids:
            return []
        ids = sorted(set(source_ids or []))
        if any(type(value) is not int or value <= 0 for value in ids):
            raise ValueError("유효한 원자막 ID가 필요합니다")
        args = (sid,*ids)
        rows = self.db.query("SELECT r.* FROM caption_source_revisions r JOIN caption_sources s ON s.id=r.source_id "
            "WHERE s.session_id=?" + (" AND s.id IN ("+','.join('?' for _ in ids)+")" if source_ids is not None else "")
            + " ORDER BY r.source_id,r.revision",args)
        return [{**row,"payload":json.loads(row["payload"])} for row in rows]

    def has_caption_sources(self, sid: str) -> bool:
        return bool(self.db.query_one("SELECT id FROM caption_sources WHERE session_id=? LIMIT 1", (sid,)))

    def upsert_caption_source(self, sid: str, key: str, segment: str, payload: dict, received_at: int):
        """의미 판본만 증가시킨다. 모든 수신 원문은 별도 JSONL에도 보존된다."""
        previous = self.get_caption_source(sid, key)
        payload = dict(payload)
        # 최초 등록 때만 재전송 여부를 정한다. 뒤의 부분·최종 재수신이 과거를 새 실황으로 바꾸지 않는다.
        # 반대로 이번 실행에서 새로 받은 단위의 중복은 과거 재전송으로 강등하지 않는다.
        payload["replayed"] = (previous.get("replayed") is True if previous is not None
                               else payload.get("replayed") is True)
        comparable = ("transcript", "parts", "text", "final", "scd")
        changed = previous is None or any(previous.get(k) != payload.get(k) for k in comparable)
        # 재접속으로 과거 부분 판본이 재전송돼도 이미 받은 최종 화자 판본을 퇴행시키지 않는다.
        if previous and previous["final"] and previous["scd"] and not (payload["final"] and payload["scd"]):
            return previous, False
        if not changed:
            self.db.execute("UPDATE caption_sources SET last_rx=? WHERE id=?", (received_at, previous["id"]))
            return self.get_caption_source(sid, key), False
        revision = previous["revision"] + 1 if previous else 1
        invalidated = []
        with self.db.transaction():
            self.db.execute("INSERT INTO caption_sources(session_id,source_key,segment,revision,first_rx,last_rx,stable_at,final,scd,payload) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,source_key) DO UPDATE SET "
                            "revision=excluded.revision,last_rx=excluded.last_rx,stable_at=excluded.stable_at,final=excluded.final,scd=excluded.scd,payload=excluded.payload",
                            (sid,key,segment,revision,received_at,received_at,received_at,int(payload["final"]),int(payload["scd"]),_json(payload)))
            row = self.get_caption_source(sid, key)
            self.db.execute("INSERT INTO caption_source_revisions(source_id,revision,payload,received_at) VALUES(?,?,?,?)",
                            (row["id"],revision,_json(payload),received_at))
            invalidated = [value["id"] for value in self.db.query(
                "SELECT d.id FROM transcript_stage_sources r JOIN transcript_stage_documents d ON d.id=r.document_id "
                "WHERE r.source_id=? AND r.revision<>? AND d.status<>'stale'", (row["id"], revision))]
            if previous and previous["final"] and previous["scd"]:
                # 과거 문장창 문서는 넓은 문맥을 사용했다. 종료 블록은 명시 출처만 무효화한다.
                invalidated.extend(value["id"] for value in self.db.query(
                    "SELECT id FROM transcript_stage_documents WHERE session_id=? AND status<>'stale' "
                    "AND json_extract(payload,'$.first_rx')>=? "
                    "AND COALESCE(json_extract(payload,'$.mode'),'')<>'closed_block_v1'",
                    (sid, previous["first_rx"])))
            invalidated = sorted(set(invalidated))
            if invalidated:
                self.db.execute("UPDATE transcript_stage_documents SET status='stale',updated_at=? WHERE id IN ("
                                + ",".join("?" for _ in invalidated) + ")", (received_at, *invalidated))
        cache = getattr(self, "_stage_source_invalidations", None)
        if cache is None:
            cache = self._stage_source_invalidations = OrderedDict()
        cache.pop((sid, row["id"]), None)
        if invalidated:
            cache[sid, row["id"]] = (revision, tuple(invalidated))
            while len(cache) > 4096:
                cache.popitem(last=False)
            # 반환용 임시 값이며 저장하는 원문 payload에는 넣지 않는다.
            row = {**row, "_invalidated_document_ids": invalidated}
        return row, True

    def create_stage_document(self, sid: str, digest: str, sources: list[dict], payload: dict,
                              *, context_sources: list[dict] | None = None):
        t = now_ms()
        with self.db.transaction():
            cursor = self.db.execute("INSERT INTO transcript_stage_documents(session_id,input_hash,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                     (sid,digest,payload.get("status","pending"),_json(payload),t,t))
            did = cursor.lastrowid
            if payload.get("source_kind") == "recording_asr":
                # 자막 FK 표에는 녹음 발언 ID를 쓰지 않는다. 원발언 hash는 payload에 고정한다.
                return self.get_stage_document(sid, did)
            target_ids = {s["id"] for s in sources}
            unique = {s["id"]: s for s in [*(context_sources or []), *sources]}
            for source in unique.values():
                self.db.execute("INSERT INTO transcript_stage_sources(document_id,source_id,revision,is_target) VALUES(?,?,?,?)",
                                (did,source["id"],source["revision"],int(source["id"] in target_ids)))
        return self.get_stage_document(sid, did)

    def get_stage_document(self, sid: str, did: int):
        return _document(self.db.query_one("SELECT * FROM transcript_stage_documents WHERE session_id=? AND id=?", (sid,did)))

    def stage_document_context_sources(self, sid: str, did: int):
        document = self.get_stage_document(sid, did)
        if document and document.get("source_kind") == "recording_asr":
            return self.stage_sources_by_ids(sid, [int(uid) for uid in document.get("recording_source_hashes") or {}])
        rows = self.db.query("SELECT s.* FROM caption_sources s JOIN transcript_stage_sources r ON r.source_id=s.id "
                             "WHERE s.session_id=? AND r.document_id=? AND s.revision=r.revision ORDER BY s.first_rx,s.id", (sid,did))
        return [_source(r) for r in rows]

    def stage_invalidated_document_ids(self, sid: str, source_id: int):
        """현재 원조각 갱신이 새로 무효화한 문서만 돌려주며 과거 stale은 재탐색하지 않는다."""
        value = getattr(self, "_stage_source_invalidations", {}).get((sid, source_id))
        if value is None:
            return []
        current = self.db.query_one("SELECT revision FROM caption_sources WHERE session_id=? AND id=?", (sid, source_id))
        return list(value[1]) if current and current["revision"] == value[0] else []

    def update_stage_document(self, sid: str, did: int, payload: dict):
        self.db.execute("UPDATE transcript_stage_documents SET payload=?,status=?,updated_at=? WHERE session_id=? AND id=?",
                        (_json(payload),payload.get("status","held"),now_ms(),sid,did))
        return self.get_stage_document(sid,did)

    def stage_document_sources_current(self, did: int) -> bool:
        row = self.db.query_one("SELECT * FROM transcript_stage_documents WHERE id=? AND status<>'stale'", (did,))
        if not row:
            return False
        document = _document(row)
        if document.get("source_kind") == "recording_asr":
            from .recording_stages import document_current
            return document_current(self, row["session_id"], document)
        from .stage_mirror import (source_settlement_policy, document_source_ready, explicit_declaration_current,
                                  declaration_role_proof)
        policy = source_settlement_policy(document)
        if document.get("source_settlement_policy") is not None and policy is None:
            return False
        if policy and policy["policy"] == "after_two_closed_questions":
            # 표시용 증거만 믿지 않고 같은 회의의 후속 두 질의가 실제 종료됐는지 재검사한다.
            first, second = policy["following_closed_question_ids"]
            following = self.db.query("SELECT id,kind,is_open FROM blocks WHERE session_id=? AND id IN (?,?)",
                                      (row["session_id"], first, second))
            if (len(following) != 2 or any(block["kind"] != "question" or block["is_open"] for block in following)):
                return False
            if not self.db.query_one("SELECT id FROM blocks WHERE session_id=? AND id>? "
                                     "AND kind IN ('chair','question') LIMIT 1", (row["session_id"], max(first, second))):
                return False
        if policy and policy["policy"] == "explicit_chair_declaration":
            # 본문 membership의 기존 서명은 유지하고 새 선언 예외의 역할 권위만 추가로 고정한다.
            declared = document.get("declaration_membership")
            if not declaration_role_proof(document):
                return False
            ids = [item["utterance_id"] for item in declared]
            if ids != sorted(set(ids)) or not set(ids) <= set((document.get("membership") or {}).get("utterance_ids") or []):
                return False
            actual = self.db.query("SELECT id,block_id,speaker_role,speaker_source FROM utterances WHERE session_id=? AND id IN ("
                                   + ",".join("?" for _ in ids) + ") ORDER BY id", (row["session_id"], *ids))
            current = [{"utterance_id":item["id"], "block_id":item.get("block_id"),
                "speaker_role":item.get("speaker_role") or "", "speaker_source":item.get("speaker_source") or ""} for item in actual]
            if current != declared:
                return False
            scope = (document.get("boundary_evidence") or {}).get("completion_scope")
            if scope is not None:
                # 내부 호명 뒤의 별도 선언은 원래 질의 바로 다음 진행 블록에만 효력이 있다.
                following = self.db.query_one("SELECT id,kind FROM blocks WHERE session_id=? AND id>? ORDER BY id LIMIT 1",
                                              (row["session_id"],document["block_id"]))
                if (not following or following["kind"] != "chair"
                        or following["id"] != scope["following_chair_block_id"]):
                    return False
        sources = self.db.query("SELECT s.*,r.revision AS expected_revision FROM transcript_stage_sources r "
                                "LEFT JOIN caption_sources s ON s.id=r.source_id WHERE r.document_id=?", (did,))
        if not sources:
            return False
        if policy and not set(policy["unconfirmed_source_ids"]).issubset({source["id"] for source in sources}):
            return False
        # 선언 출처는 본문 예외와 분리해 실제 확정·정확한 판본·원문 문구까지 확인한다.
        declarations = [_source(source) for source in sources if source["id"] is not None]
        if policy and policy["policy"] == "explicit_chair_declaration" and not explicit_declaration_current(
                policy["declaration"], declarations, document.get("source_revisions")):
            return False
        return all(source["id"] is not None and source["session_id"] == row["session_id"]
                   and source["revision"] == source["expected_revision"]
                   and not _source(source).get("replayed")
                   and document_source_ready(document, _source(source), declaration_sources=declarations)
                   for source in sources)

    def stage_claimed_source_ids(self, sid: str) -> set[int]:
        rows = self.db.query("SELECT DISTINCT r.source_id FROM transcript_stage_sources r JOIN transcript_stage_documents d ON d.id=r.document_id "
                             "WHERE d.session_id=? AND r.is_target=1 AND d.status<>'stale'", (sid,))
        return {r["source_id"] for r in rows}

    def interrupt_stage_documents(self):
        """재기동 전 실행 중이던 요청은 자동 재실행하지 않고 보류로 남긴다."""
        rows = self.db.query("SELECT * FROM transcript_stage_documents WHERE status<>'stale'")
        for row in rows:
            document = _document(row)
            changed = False
            if document["status"] == "processing":
                document.update(status="held", issues=["서버 종료로 보정이 중단되어 원문을 유지합니다"])
                changed = True
            if (document.get("verification") or {}).get("state") == "running":
                document["verification"] = {"state":"held","detail":"서버 종료로 음성 확인이 중단되었습니다","audio_verified":False}
                changed = True
            if changed:
                self.update_stage_document(document["session_id"], document["id"], document)

    def list_stage_documents(self, sid: str, source_ids: list[int] | None = None):
        if source_ids is not None and not source_ids:
            return []
        if source_ids is not None and (self.get_session(sid) or {}).get("source_type") == "file":
            wanted = set(source_ids)
            return [d for d in self.list_stage_documents(sid) if wanted.intersection(d.get("source_ids") or [])]
        args = (sid,)
        sql = "SELECT d.* FROM transcript_stage_documents d WHERE d.session_id=?"
        if source_ids is not None:
            marks = ",".join("?" for _ in source_ids)
            sql += f" AND d.id IN (SELECT document_id FROM transcript_stage_sources WHERE is_target=1 AND source_id IN ({marks}))"
            args = (sid,*source_ids)
        return [_document(r) for r in self.db.query(sql + " ORDER BY d.id",args)]
