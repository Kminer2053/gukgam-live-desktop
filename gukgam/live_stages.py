"""국회 원수신 실황과 종료 질의 전체 보정을 분리한 실행 서비스."""
from __future__ import annotations

import asyncio
from copy import copy, deepcopy
from contextlib import contextmanager
import hashlib
import json
import math
import logging
import re

from . import settings
from .db import now_ms

STABLE_MS = 2_000
BATCH_CHARS = 1_800
BATCH_SOURCES = 8
BATCH_WAIT_MS = 12_000
TAIL_WAIT_MS = 30_000
logger = logging.getLogger(__name__)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def source_payload(message: dict) -> tuple[str, dict] | None:
    """표시용 원조각은 화자 번호나 본문을 추정·정규화하지 않는다."""
    if not isinstance(message, dict) or isinstance(message.get("segment"), bool):
        return None
    segment = message.get("segment")
    if not isinstance(segment, (str, int)) or not str(segment) or len(str(segment)) > 100:
        return None
    transcript = message.get("transcript", "")
    if not isinstance(transcript, str) or len(transcript) > 100_000:
        return None
    parts = []
    incoming = message.get("transcripts")
    if isinstance(incoming, list):
        if len(incoming) > 512:
            return None
        for part in incoming:
            if not isinstance(part, list) or len(part) != 2 or not isinstance(part[1], str):
                return None
            if not isinstance(part[0], (int, str, type(None))) or isinstance(part[0], bool):
                return None
            parts.append({"label": part[0], "text": part[1]})
    if not parts:
        parts = [{"label": None, "text": transcript}]
    text = "\n".join(p["text"] for p in parts)
    if len(text) > 100_000:
        return None
    offset = message.get("segment-offset")
    if not isinstance(offset, (str, int, float, type(None))) or isinstance(offset, bool):
        return None
    if isinstance(offset, float) and not math.isfinite(offset):
        return None
    # 제공자 번호가 재시작해도 음성 offset이 다르면 서로 다른 원조각이다.
    key = digest([str(segment), str(offset)])[:32]
    return key, {"transcript": transcript, "text": text, "parts": parts,
                 "final": message.get("final") is True, "scd": message.get("scd") is True,
                 "provider_offset": offset}


def public_source(source: dict) -> dict:
    fields = ("id", "source_key", "segment", "revision", "first_rx", "last_rx", "final", "scd", "text", "transcript", "parts", "replayed",
              "source_kind", "origin_utterance_id", "source_hash", "acoustic_speaker_id", "acoustic_overlap")
    out = {k: deepcopy(source[k]) for k in fields if k in source}
    from .stage_mirror import public_source as mirror_source
    guess = mirror_source(source).get("speaker_guess")
    if guess is not None:
        out["speaker_guess"] = guess
    return out


def public_document(document: dict) -> dict:
    """원음 전사·경로·모델 입력을 공개 화면으로 전달하지 않는다."""
    allowed = ("id", "input_hash", "source_ids", "source_revisions", "status", "sentences", "paragraphs",
               "created_at", "updated_at", "issues", "detail", "first_rx", "last_rx",
               "mode", "editor_version", "block_id", "questioner", "summary", "boundary_evidence",
               "source_supersessions", "role_decision", "processing_settings_hash", "source_kind", "question_identity")
    out = {k: deepcopy(document[k]) for k in allowed if k in document}
    from .stage_mirror import processing_receipts
    out.update(processing_receipts(document))
    from .stage_mirror import source_settlement_policy
    policy = source_settlement_policy(document)
    if policy is not None:
        out["source_settlement_policy"] = policy
    verification = document.get("verification") or {}
    out["verification"] = {k: deepcopy(verification[k]) for k in
        ("state", "sentences", "paragraphs", "audio_verified", "detail") if k in verification}
    out["verification"]["checks"] = [{k: deepcopy(check[k]) for k in
        ("sentence_id", "edit_index", "kind", "status", "reason", "reason_code", "detail", "before", "after") if k in check}
        for check in verification.get("checks", []) if isinstance(check, dict)]
    return out


class LiveStagesService:
    """원문을 즉시 보존하고 종료·확정된 질의 전체만 직렬 보정한다."""

    MODE = "closed_block_v1"
    EDITOR_VERSION = 4

    def __init__(self, rt, *, builder=None, verifier=None, clock=now_ms):
        self.rt, self.clock, self.builder = rt, clock, builder
        self._active: set[str] = set()
        self._requested: dict[str, set[int]] = {}
        self._request_versions: dict[tuple[str, int], int] = {}
        self._recovered_sessions: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._settings_epoch = 0
        self._execution = None
        self._pending_recovery: set[str] = set()
        self._pending_invalidated: dict[str, set[int]] = {}

    def settings_changed(self) -> None:
        self._settings_epoch += 1
        self._wake.set()

    def _execution_key(self):
        return digest([self._settings_epoch, settings.get(self.rt.repo,"transcript_review"),
                       settings.get(self.rt.repo,"llm"), self._role_decision_metadata()])

    def _role_decision_metadata(self):
        """서명에는 모델 출처·설정을 고정하고 실행 성공 여부는 결과에 별도 기록한다."""
        from .llm.decision import decision_provider_metadata
        metadata = decision_provider_metadata(settings=settings.get(self.rt.repo, "transcript_review"))
        bridge = getattr(self.rt.config, "desktop_bridge", None)
        if metadata.get("provider") == "jev" and bridge is not None:
            from .desktop_jev import credential_revision
            metadata["credential_revision"] = credential_revision(bridge)
        if metadata.get("provider") == "julia":
            from .llm.julia import model_fingerprint, model_status, runtime_fingerprint
            # 준비 전 실패와 준비 후 결과를 같은 캐시로 재사용하지 않는다. 파일 내용 해시는 작업자가 검사한다.
            metadata["artifact_fingerprint"] = model_fingerprint(self.rt.config.data_dir)
            metadata["runtime_present"] = model_status(self.rt.config.data_dir)["runtime_present"]
            metadata["runtime_fingerprint"] = runtime_fingerprint()
        return metadata

    async def _decision_provider(self):
        # 제공자 교체·작업자 수명주기는 메인 루프의 ReviewEngine 한 곳이 소유한다.
        cfg = settings.get(self.rt.repo, "transcript_review")
        selected = cfg.get("role_decision_provider", "jev" if cfg.get("use_jev", False) else "none")
        if selected == "none":
            return None
        engine = getattr(getattr(self.rt, "reviews", None), "engine", None)
        getter = getattr(engine, "get_provider", None)
        return await getter() if getter is not None else getattr(engine, "provider", None)

    def owns(self, sid: str) -> bool:
        if sid in self._active or self.rt.repo.has_caption_sources(sid):
            return True
        from .recording_stages import is_recording_session
        return bool(is_recording_session(self.rt.repo, sid) and (
            self._configured() and any(b.get("kind") == "question" for b in self.rt.repo.stage_block_headers(sid))
            or any(d.get("source_kind") == "recording_asr" for d in self.rt.repo.list_stage_documents(sid))))

    def _configured(self) -> bool:
        cfg = settings.get(self.rt.repo,"transcript_review") or {}
        return bool(cfg.get("enabled") and cfg.get("auto_block") and not self.rt.config.disable_heavy_engines)

    def _ensure_task(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(),name="closed-block-correction")

    def ingest(self, sid: str, message: dict, received_at: int, *, replayed: bool = False) -> dict | None:
        parsed = source_payload(message)
        if parsed is None or self._closed:
            return None
        key, payload = parsed
        payload["replayed"] = replayed
        source, changed = self.rt.repo.upsert_caption_source(sid,key,str(message["segment"]),payload,received_at)
        self._active.add(sid)
        if not replayed and sid not in self._recovered_sessions:
            self._pending_recovery.add(sid)
        if changed:
            # 수신 경로는 원자막 저장·증분 ID 전달만 한다. 문서 해석은 별도 읽기 작업이다.
            invalidated = source.pop("_invalidated_document_ids", [])
            if invalidated:
                self._pending_invalidated.setdefault(sid, set()).update(invalidated)
            event = public_source(source)
            event["invalidated_document_ids"] = invalidated
            self.rt.bus.publish(sid,"caption.source",event)
        self._wake.set()
        self._ensure_task()
        return source

    def _restore_missing_closed_requests(self, sid: str) -> None:
        """현재 수집 회의의 미실행 종료 질의만 복구하며 기존 실패·완료 문서는 반복하지 않는다."""
        if sid in self._recovered_sessions:
            return
        session = self.rt.repo.get_session(sid)
        if not session or session.get("source_type") != "assembly_caption" or session.get("status") != "live":
            return
        handled = {row.get("block_id") for row in self.rt.repo.list_stage_documents(sid)
                   if row.get("mode") == self.MODE}
        missing = {row["id"] for row in self.rt.repo.list_blocks(sid)
                   if row.get("kind") == "question" and not row.get("is_open") and row["id"] not in handled}
        self._requested.setdefault(sid,set()).update(missing)
        self._recovered_sessions.add(sid)

    def on_block_closed(self, sid: str, block_id: int) -> None:
        if self._closed:
            return
        # 자동 요약의 현재성 재검사를 거치지 않는 단건 메타 조회만 한다.
        block = self.rt.repo.stage_block_record(sid,block_id)
        if not block or block.get("kind") != "question" or block.get("is_open"):
            return
        from .recording_stages import is_recording_session
        if is_recording_session(self.rt.repo, sid):
            self.rt.repo.enable_recording_stage_display(sid)
        self._mark_requested(sid,block_id)
        self._wake.set()
        self._ensure_task()

    def _mark_requested(self, sid: str, bid: int) -> None:
        self._requested.setdefault(sid,set()).add(bid)
        key = sid,bid
        self._request_versions[key] = self._request_versions.get(key,0)+1

    def flush(self, sid: str) -> None:
        """대기 중인 종료 질의를 깨우되 열린 질의를 강제로 확정하지 않는다."""
        self._wake.set()

    def audio_status(self, sid: str) -> dict:
        return {"enabled":False,"can_enable":False,"phase":"removed",
                "detail":"음성 녹음 보정은 제거했습니다. 종료된 자막 블록 전체를 보정합니다."}

    def enable_audio(self, sid: str) -> dict:
        raise ValueError("음성 녹음 보정은 제거했습니다")

    def disable_audio(self, sid: str) -> dict:
        return self.audio_status(sid)

    def _protection(self, sid: str, sources: list[dict] | None = None, *, membership: dict | None = None) -> dict:
        return self.rt.repo.stage_document_protection(sid,sources,membership)

    def _protected_overlap(self, sid: str, sources: list[dict], membership: dict) -> bool:
        protection = self._protection(sid,sources,membership=membership)
        return bool(protection["rows"] or protection["blocks"])

    def _block_stamp(self, block: dict) -> str:
        return self.rt.repo.stage_block_stamp(block)

    def _selection(self, sid: str, block: dict) -> dict:
        from .recording_stages import is_recording_session, selection_for
        if is_recording_session(self.rt.repo, sid):
            return selection_for(self.rt.repo, sid, block)
        from .block_sources import map_utterance_sources, select_block_sources
        from .source_supersession import resolve_superseded_occurrences
        blocks = self.rt.repo.list_blocks(sid)
        utterances = self.rt.repo.list_utterances(sid,limit=-1)
        segments = self.rt.repo.stage_raw_segments(sid)
        sources = self.rt.repo.all_caption_sources(sid)
        mapping = map_utterance_sources(utterances,segments,sources)
        # 실패한 옛 조각의 이력만 읽는다. 전체 수신 이력을 메모리에 적재하지 않는다.
        failed_bases = {str(row.get("source_key","")).rsplit("#",1)[0] for row in utterances
                        if mapping["failures"].get(row["id"]) == "source_revision_unmatched"}
        numbers = {match[1] for row in segments if row["source_key"] in failed_bases
                   for key in ((row.get("raw") or {}).get("segments") or {})
                   if (match := re.fullmatch(r"s(\d+)(?:\.\d+)?~\d+",key))}
        ids = [row["id"] for row in sources if str(row.get("segment")) in numbers]
        history_reader = getattr(self.rt.repo,"stage_caption_source_revisions",None)
        if ids and history_reader is not None:
            mapping = resolve_superseded_occurrences(utterances,segments,sources,mapping,
                blocks=blocks,source_revisions=history_reader(sid,ids))
        following = self._following_closed_questions(block, blocks)
        selected = select_block_sources(block,blocks,
            utterances,segments,sources,mapping=mapping,roster=self.rt.repo.get_roster(sid),
            allow_unsettled=len(following) == 2)
        declaration = self._declaration(sid, selected, sources=sources)
        dependency_ids = set(selected.get("dependency_source_ids") or [])
        dependency_ids.update(source["id"] for source in selected.get("sources") or [])
        needs_settlement = any((not source.get("final") or not source.get("scd"))
                              and source["id"] in dependency_ids for source in sources)
        if declaration is not None and needs_settlement:
            # 실제 확정된 위원장 선언은 다음 두 질의를 기다리는 대신 즉시 입력 경계로 쓴다.
            selected = select_block_sources(block,blocks,utterances,segments,sources,mapping=mapping,
                roster=self.rt.repo.get_roster(sid),allow_unsettled=True)
            selected.update(source_settlement_policy="explicit_chair_declaration", declaration=declaration, session_id=sid)
        if len(following) == 2:
            selected["following_closed_question_ids"] = following
        return selected

    def _declaration_evidence(self, sid: str, selection: dict) -> dict | None:
        """별도 종결 근거는 내부 호명으로 잘린 같은 질의와 바로 다음 진행 블록에만 묶는다."""
        from .stage_mirror import declaration_anchor
        evidence = selection.get("boundary_evidence") or {}
        if not isinstance(evidence,dict):
            return None
        end = evidence.get("end_anchor") or {}
        if not isinstance(end,dict):
            return None
        anchor = declaration_anchor(end)
        if anchor is not None and evidence.get("kind") == anchor["kind"]:
            return end
        completion = evidence.get("completion_declaration_anchor")
        scope = evidence.get("completion_scope")
        bid = selection.get("block_id")
        if (evidence.get("kind") != "interior_named_call" or not isinstance(scope, dict)
                or scope.get("kind") != "interior_named_call_in_stored_question"
                or type(bid) is not int or bid <= 0 or scope.get("block_id") != bid
                or type(scope.get("block_id")) is not int
                or type(scope.get("following_chair_block_id")) is not int
                or scope["following_chair_block_id"] <= bid
                or evidence.get("next_block_id") != scope["following_chair_block_id"]
                or end.get("kind") != "call" or declaration_anchor(completion) is None):
            return None
        end_ids = end.get("source_ids")
        if (not isinstance(end_ids,list) or not end_ids or any(type(uid) is not int or uid <= 0 for uid in end_ids)
                or len(end_ids) != len(set(end_ids))):
            return None
        following = min((row for row in self.rt.repo.list_blocks(sid) if row["id"] > bid),
                        key=lambda row:row["id"], default=None)
        if not following or following["id"] != scope["following_chair_block_id"] or following.get("kind") != "chair":
            return None
        for raw, owner in ((end,bid),(completion,following["id"])):
            ids = raw.get("chair_utterance_ids")
            if (not isinstance(ids,list) or not ids or any(type(uid) is not int or uid <= 0 for uid in ids)
                    or len(ids) != len(set(ids))):
                return None
            rows = self.rt.repo.stage_utterances_by_ids(sid,ids)
            if len(rows) != len(ids) or any(row.get("speaker_role") != "chair" or row.get("block_id") != owner for row in rows):
                return None
        source_ids = set(end_ids) | set(completion["source_ids"])
        source_ids.update(source["id"] for source in selection.get("sources") or [])
        sources = self.rt.repo.caption_sources_by_ids(sid,list(source_ids))
        by_id = {source["id"]:source for source in sources}
        def point(ref):
            if not isinstance(ref,dict) or type(ref.get("source_id")) is not int or type(ref.get("offset")) is not int:
                return None
            row = by_id.get(ref["source_id"])
            if not row or not 0 <= ref["offset"] <= len(row["text"]):
                return None
            return row["first_rx"],row["id"],ref["offset"]
        left, right, completed = point(end.get("source_start")),point(end.get("source_end")),point(completion.get("source_start"))
        if left is None or right is None or completed is None or not left < right <= completed:
            return None
        for source in selection.get("sources") or []:
            for span in source.get("selected_spans") or []:
                selected_end = point({"source_id":source["id"],"offset":span.get("end")})
                if selected_end is None or selected_end > left:
                    return None
        end_sources = [by_id[uid] for uid in end_ids if uid in by_id]
        if len(end_sources) != len(end_ids) or not self._sources_ready(end_sources):
            return None
        return completion

    def _declaration(self, sid: str, selection: dict, *, sources: list[dict] | None = None) -> dict | None:
        """상태값 대신 실제 확정·안정 원문의 명시 선언만 미확정 본문 대기 예외로 인정한다."""
        from .stage_mirror import declaration_anchor, explicit_declaration_current
        raw_anchor = self._declaration_evidence(sid,selection) or {}
        anchor = declaration_anchor(raw_anchor)
        if anchor is None:
            return None
        chair_ids = raw_anchor.get("chair_utterance_ids")
        if (not isinstance(chair_ids, list) or not chair_ids or any(type(uid) is not int or uid <= 0 for uid in chair_ids)
                or len(chair_ids) != len(set(chair_ids))):
            return None
        chairs = self.rt.repo.stage_utterances_by_ids(sid, chair_ids)
        if len(chairs) != len(chair_ids) or any(row.get("speaker_role") != "chair" for row in chairs):
            return None
        rows = (self.rt.repo.caption_sources_by_ids(sid, anchor["source_ids"]) if sources is None else
                [source for source in sources if source.get("id") in anchor["source_ids"]])
        refs = {str(source["id"]):source["revision"] for source in rows}
        if not self._sources_ready(rows) or not explicit_declaration_current(anchor, rows, refs):
            return None
        return anchor

    @staticmethod
    def _following_closed_questions(block: dict, blocks: list[dict]) -> list[int]:
        """다음 진행·질의가 시작된 종료 회차만 세며 종료 정리로 닫힌 마지막 질의는 제외한다."""
        last_boundary = max((row["id"] for row in blocks if row.get("kind") in {"chair","question"}),default=0)
        return sorted(row["id"] for row in blocks if row.get("kind") == "question"
                      and block["id"] < row["id"] < last_boundary and not row.get("is_open"))[:2]

    @staticmethod
    def _allows_unsettled(selection: dict) -> bool:
        if selection.get("source_settlement_policy") == "explicit_chair_declaration":
            from .stage_mirror import declaration_anchor
            return declaration_anchor(selection.get("declaration")) is not None
        following = selection.get("following_closed_question_ids")
        block_id = selection.get("block_id")
        return bool(selection.get("source_settlement_policy") == "after_two_closed_questions"
                    and type(block_id) is int and block_id > 0
                    and isinstance(following,list) and len(following) == 2
                    and all(type(bid) is int and bid > block_id for bid in following)
                    and len(set(following)) == 2)

    def _dependencies(self, sid: str, block: dict, selection: dict) -> tuple[list[dict], dict, dict]:
        """본문 밖 호명과 실제 문자 소속도 파생 문서의 입력 판본으로 묶는다."""
        if selection.get("source_kind") == "recording_asr":
            from .recording_stages import dependencies_for
            return dependencies_for(self.rt.repo, sid, block, selection)
        ids = set(selection.get("dependency_source_ids", []))
        evidence = selection.get("boundary_evidence") or {}
        for key in ("start_anchor", "end_anchor", "completion_declaration_anchor"):
            anchor = evidence.get(key) or {}
            ids.update(anchor.get(part, {}).get("source_id") for part in ("source_start", "source_end"))
            ids.update(anchor.get("source_ids") or [])
        ids.discard(None)
        dependencies = self.rt.repo.caption_sources_by_ids(sid,list(ids))
        block_ids = set(selection.get("dependency_block_ids", []))
        if evidence.get("next_block_id"):
            block_ids.add(evidence["next_block_id"])
        stamps = {str(bid):self.rt.repo.stage_boundary_stamp(row) for bid in block_ids
                  if (row:=self.rt.repo.get_block(bid)) is not None}
        member_ids = {m["utterance_id"] for m in selection.get("utterance_membership", [])}
        if selection.get("source_settlement_policy") == "explicit_chair_declaration":
            for anchor in (evidence.get("end_anchor") or {}, evidence.get("completion_declaration_anchor") or {}):
                member_ids.update(anchor.get("utterance_ids") or [])
                member_ids.update(anchor.get("chair_utterance_ids") or [])
                if type(anchor.get("utterance_id")) is int:
                    member_ids.add(anchor["utterance_id"])
        rows = [r for r in self.rt.repo.list_utterances(sid,limit=-1) if r["id"] in member_ids]
        membership = {"utterance_ids":sorted(member_ids),
                      "block_ids":sorted({block["id"],*(r["block_id"] for r in rows if r.get("block_id"))})}
        return dependencies,stamps,membership

    def _sources_ready(self, sources: list[dict], *, allow_unsettled: bool = False) -> bool:
        if sources and all(s.get("source_kind") == "recording_asr" for s in sources):
            from .recording_sources import recording_source_ready
            return all(recording_source_ready(s) for s in sources)
        return all((allow_unsettled or s.get("final") and s.get("scd")) and not s.get("replayed")
                   and self.clock()-s["stable_at"]>=STABLE_MS for s in sources)

    def _ready(self, selection: dict) -> bool:
        sources = selection.get("sources") or []
        return bool(selection.get("status")=="ready" and sources
                    and (selection.get("source_settlement_policy") != "explicit_chair_declaration"
                         or self._declaration(selection.get("session_id"), selection) == selection.get("declaration"))
                    and self._sources_ready(sources,allow_unsettled=self._allows_unsettled(selection)))

    def _context(self, sid: str, block: dict) -> dict:
        return self.rt.repo.stage_block_context(sid,block)

    def _executor(self):
        if self._execution is None:
            from .stage_execution import StageExecution
            self._execution = StageExecution()
        return self._execution

    @contextmanager
    def reader(self):
        """현재 읽기 호출 동안만 수신 연결과 독립된 일관 판본을 연다."""
        from .repo import Repo
        db = self.rt.repo.db
        if not hasattr(db,"read_connection"):
            # DB 없는 작은 모의 객체만 이전 단위 시험 계약을 유지한다.
            if getattr(db,"path",None) not in (None,":memory:"):
                raise RuntimeError("보정 읽기 전용 연결을 열 수 없습니다")
            yield self
            return
        with db.read_connection() as read_db:
            with read_db.transaction():
                view = copy(self)
                view.rt = copy(self.rt)
                view.rt.repo = Repo(read_db)
                if hasattr(view.rt,"db"):
                    view.rt.db = read_db
                yield view

    async def read_call(self, callback):
        def read():
            with self.reader() as view:
                return callback(view)
        return await self._executor().read(read)

    async def read_snapshot(self, sid: str, **kwargs) -> dict:
        return await self.read_call(lambda view: view.snapshot(sid,**kwargs))

    async def _drain_updates(self):
        recovery = set(self._pending_recovery)-self._recovered_sessions
        invalidated = self._pending_invalidated
        self._pending_recovery.clear()
        self._pending_invalidated = {}
        def collect(view):
            missing, events, retry = {}, [], {}
            for sid in recovery:
                session = view.rt.repo.get_session(sid)
                if not session or session.get("source_type") != "assembly_caption" or session.get("status") != "live":
                    continue
                handled = {row.get("block_id") for row in view.rt.repo.list_stage_documents(sid)
                           if row.get("mode") == self.MODE}
                missing[sid] = {row["id"] for row in view.rt.repo.list_blocks(sid)
                    if row.get("kind") == "question" and not row.get("is_open") and row["id"] not in handled}
            from . import serialize
            for sid, ids in invalidated.items():
                for did in ids:
                    old = view.rt.repo.get_stage_document(sid,did)
                    if not old or old.get("mode") != self.MODE or not old.get("block_id"):
                        continue
                    retry.setdefault(sid,set()).add(old["block_id"])
                    block = view.rt.repo.get_block(old["block_id"])
                    if block:
                        events.append((sid,serialize.block(block)))
            return missing,retry,events
        missing,retry,events = await self.read_call(collect)
        self._recovered_sessions.update(recovery)
        for groups in (missing,retry):
            for sid,ids in groups.items():
                for bid in ids:
                    self._mark_requested(sid,bid)
        for sid,data in events:
            self.rt.bus.publish(sid,"block.upsert",data)

    async def _run(self) -> None:
        while not self._closed:
            self._wake.clear()
            if self._pending_recovery or self._pending_invalidated:
                await self._drain_updates()
            configured = self.builder is not None or await self.read_call(lambda view: view._configured())
            if configured:
                for sid in sorted(self._requested):
                    for bid in sorted(self._requested[sid]):
                        version = self._request_versions.get((sid,bid),0)
                        try:
                            result = await self.process_block(sid,bid)
                        except asyncio.CancelledError:
                            raise
                        except ValueError:
                            continue
                        except Exception:
                            logger.error("종료 질의 보정 결과 저장에 실패했습니다")
                            continue
                        if (not result or result.get("status") != "stale") and version == self._request_versions.get((sid,bid),0):
                            self._requested[sid].discard(bid)
                        break
            try:
                await asyncio.wait_for(self._wake.wait(),timeout=1)
            except asyncio.TimeoutError:
                pass

    def _prepare_block(self, sid: str, block_id: int, *, selection: dict | None = None) -> dict:
        block = self.rt.repo.get_block(block_id)
        if not block or block["session_id"]!=sid or block.get("kind")!="question" or block.get("is_open"):
            raise ValueError("종료된 질의 블록만 보정할 수 있습니다")
        from .recording_stages import is_recording_session, prepare
        if is_recording_session(self.rt.repo, sid):
            return prepare(self, sid, block, selection)
        selection = selection or self._selection(sid,block)
        if not self._ready(selection):
            raise ValueError("블록 전체의 원문 출처·final·scd 확정이 필요합니다")
        sources = selection["sources"]
        context = self._context(sid,block)
        context_hash = digest(context)
        if selection.get("question_identity"):
            context = {**context,"question_identity":selection["question_identity"]}
        dependencies,dependency_blocks,membership = self._dependencies(sid,block,selection)
        allow_unsettled = self._allows_unsettled(selection)
        explicit = selection.get("source_settlement_policy") == "explicit_chair_declaration"
        following = self._following_closed_questions(block,self.rt.repo.list_blocks(sid))
        if allow_unsettled and not explicit and selection["following_closed_question_ids"] != following:
            raise ValueError("후속 질의 종료 판본이 변경됐습니다")
        if not self._sources_ready(dependencies,allow_unsettled=allow_unsettled):
            raise ValueError("질의 종료 호명까지 final·scd 확정이 필요합니다")
        unconfirmed = sorted({source["id"] for source in [*sources,*dependencies]
                              if not source.get("final") or not source.get("scd")})
        settlement = None
        if allow_unsettled and unconfirmed:
            if explicit:
                declaration = self._declaration(sid, selection)
                if declaration is None:
                    raise ValueError("위원장 종료 선언의 확정 원문이 변경됐습니다")
                settlement = {"policy":"explicit_chair_declaration", "declaration":declaration,
                              "unconfirmed_source_ids":unconfirmed}
            else:
                settlement = {"policy":"after_two_closed_questions","following_closed_question_ids":following,
                              "unconfirmed_source_ids":unconfirmed}
                for bid in following:
                    dependency_blocks[str(bid)] = self.rt.repo.stage_boundary_stamp(self.rt.repo.get_block(bid))
        declaration_membership = None
        declaration_chair_ids = None
        if settlement and explicit:
            # 새 예외에만 선언자의 역할 판본을 고정한다. 기존 완료 문서의 서명은 바꾸지 않는다.
            evidence = selection["boundary_evidence"]
            anchor = self._declaration_evidence(sid,selection)
            if anchor is None:
                raise ValueError("위원장 선언의 소속 범위가 변경됐습니다")
            anchors = [anchor]
            if evidence.get("completion_declaration_anchor") is anchor:
                anchors.append(evidence["end_anchor"])
            ids = {uid for row in anchors for uid in row.get("utterance_ids") or []}
            declaration_chair_ids = sorted({uid for row in anchors for uid in row.get("chair_utterance_ids") or []})
            ids.update(declaration_chair_ids)
            ids.update(row["utterance_id"] for row in anchors if type(row.get("utterance_id")) is int)
            if not ids or any(type(uid) is not int or uid <= 0 for uid in ids):
                raise ValueError("위원장 선언의 발언 소속이 필요합니다")
            rows = self.rt.repo.stage_utterances_by_ids(sid, sorted(ids))
            if len(rows) != len(ids):
                raise ValueError("위원장 선언의 발언 소속이 변경됐습니다")
            declaration_membership = [{"utterance_id":row["id"], "block_id":row.get("block_id"),
                "speaker_role":row.get("speaker_role") or "", "speaker_source":row.get("speaker_source") or ""}
                for row in sorted(rows, key=lambda row:row["id"])]
            from .stage_mirror import declaration_role_proof
            if not declaration_role_proof({"block_id":block_id,"boundary_evidence":evidence,
                                           "source_settlement_policy":settlement,
                                           "declaration_membership":declaration_membership,
                                           "declaration_chair_utterance_ids":declaration_chair_ids}):
                raise ValueError("위원장 선언의 진행자 역할이 변경됐습니다")
        protection = digest(self._protection(sid,sources,membership=membership))
        execution = self._execution_key()
        stamp = self._block_stamp(block)
        refs = {str(s["id"]):s["revision"] for s in sources}
        dependency_refs = {str(s["id"]):s["revision"] for s in dependencies}
        membership_hash = self.rt.repo.stage_membership_stamp(sid,membership)
        if not membership_hash:
            raise ValueError("블록의 발언 연결 근거가 변경됐습니다")
        signature_parts = [self.MODE,self.EDITOR_VERSION,stamp,refs,[s.get("selected_spans") for s in sources],
                           dependency_refs,dependency_blocks,membership_hash,context,protection]
        role_decision = self._role_decision_metadata()
        processing_settings_hash = digest([settings.get(self.rt.repo,"transcript_review"), settings.get(self.rt.repo,"llm")])
        signature_parts.append({"role_decision":role_decision,"processing_settings_hash":processing_settings_hash})
        if settlement:
            signature_parts.append(settlement)
        if declaration_membership:
            signature_parts.append({"declaration_membership":declaration_membership,
                                    "declaration_chair_utterance_ids":declaration_chair_ids})
        supersessions = selection.get("source_supersessions")
        if supersessions:
            signature_parts.append({"source_supersessions":supersessions})
        signature = digest(signature_parts)
        existing = self.rt.repo.stage_document_by_signature(sid,signature)
        if existing:
            return existing
        base = {"mode":self.MODE,"editor_version":self.EDITOR_VERSION,"block_id":block_id,"questioner":block.get("questioner") or "",
                "block_stamp":stamp,"source_ids":[s["id"] for s in sources],"source_revisions":refs,
                "source_selection":[{"id":s["id"],"selected_spans":s.get("selected_spans")} for s in sources],
                "status":"processing","first_rx":sources[0]["first_rx"],"last_rx":sources[-1]["stable_at"],
                "protection_hash":protection,"membership":membership,"membership_hash":membership_hash,
                "context_hash":context_hash,"dependency_blocks":dependency_blocks,
                "boundary_evidence":selection.get("boundary_evidence",{}),
                "role_decision":role_decision,"processing_settings_hash":processing_settings_hash}
        if settlement:
            base["source_settlement_policy"] = settlement
            base["source_revisions"] = {**dependency_refs,**refs}
        if declaration_membership:
            base["declaration_membership"] = declaration_membership
            base["declaration_chair_utterance_ids"] = declaration_chair_ids
        if supersessions:
            base["source_supersessions"] = deepcopy(supersessions)
        return {"base":base,"sources":sources,"dependencies":dependencies,"context":context,
                "signature":signature,"execution":execution,"settlement":settlement,
                "protected":self._protected_overlap(sid,sources,membership)}

    async def process_block(self, sid: str, block_id: int, *, selection: dict | None = None) -> dict | None:
        if self._closed:
            raise asyncio.CancelledError
        return await self._executor().run(lambda job:self._process_isolated(job,sid,block_id,selection))

    async def _create_processing(self, sid, prepared):
        if self._closed:
            raise asyncio.CancelledError
        existing = self.rt.repo.stage_document_by_signature(sid,prepared["signature"])
        if existing:
            return existing,True
        row = self.rt.repo.create_stage_document(sid,prepared["signature"],prepared["sources"],prepared["base"],
            context_sources=prepared["dependencies"])
        self.rt.bus.publish(sid,"transcript.stage",{"id":row["id"]})
        return row,False

    async def _process_isolated(self, job, sid, block_id, selection):
        from .closed_block_editor import build_block_document
        with self.reader() as view:
            prepared = view._prepare_block(sid,block_id,selection=selection)
        if "base" not in prepared:
            return prepared  # 같은 입력의 저장 결과는 실패를 포함해 다시 실행하지 않는다.
        row,existing = await job.bridge(self._create_processing(sid,prepared))
        if existing:
            return row
        base,sources,context = prepared["base"],prepared["sources"],prepared["context"]
        protection,execution,settlement = base["protection_hash"],prepared["execution"],prepared["settlement"]
        def allowed():
            if self._closed or job.cancelled.is_set():
                return False
            with self.reader() as view:
                return bool((self.builder is not None or view._configured())
                    and view._execution_key()==execution and view._current(sid,row["id"],protection))
        async def async_allowed():
            return await job.worker_call(allowed)
        cancelled = False
        text_provider_metadata = None
        text_calls = {"calls_started": 0, "calls_completed": 0}
        try:
            if prepared["protected"]:
                result = {"status":"protected","issues":["사람이 수정·검수한 기록을 유지했습니다"]}
            elif self.builder is not None:
                result = await self.builder(sources,context=context)
            else:
                if not allowed():
                    raise ValueError("처리 허용 상태 변경")
                decision_provider = await job.bridge(self._decision_provider())
                class BridgedDecision:
                    available = getattr(decision_provider,"available",False)
                    name = getattr(decision_provider,"name","jev")
                    model = getattr(decision_provider,"model","")
                    provenance = deepcopy(getattr(decision_provider,"provenance",{}))
                    max_batch_sentences = getattr(decision_provider,"max_batch_sentences",32)
                    call_timeout_seconds = getattr(decision_provider,"call_timeout_seconds",20)
                    async def evaluate(_, *args, **kwargs):
                        # Julia는 이 루프에서 파이프 I/O만 기다린다. 로딩·토큰화·CPU 추론은 자식 작업자다.
                        return await job.bridge(decision_provider.evaluate(*args,**kwargs))
                provider,cfg = await job.bridge(self.rt.analysis._get_provider())
                llm = None
                if provider is not None and provider.name in {"local_mlx", "desktop_chatgpt", "openai_compat", "anthropic"}:
                    analysis = self.rt.analysis
                    text_provider_metadata = {"provider": provider.name, "model": provider.model}
                    if provider.name == "openai_compat":
                        text_provider_metadata["connection"] = cfg.get("openai_connection") or "custom"
                    class GuardedLLM:
                        name, model = provider.name, provider.model
                        async def complete(_,system,user,max_tokens,*,json_mode=True,direct_output=False,response_prefix="",stop_line=""):
                            text_calls["calls_started"] += 1
                            text = await job.bridge(analysis.complete_for_task(sid,f"블록 #{block_id} 보정",provider,system,user,max_tokens=max_tokens,
                                request_guard=async_allowed,execution_timeout=180,json_mode=json_mode,direct_output=direct_output,response_prefix=response_prefix,stop_line=stop_line))
                            text_calls["calls_completed"] += 1
                            return text
                    llm = GuardedLLM()
                options = {"settlement_document":{"block_id":block_id,
                    "source_revisions":dict(base["source_revisions"]),
                    "source_settlement_policy":deepcopy(settlement),
                    **({"declaration_sources":list({source["id"]:source for source in [*prepared["dependencies"],*sources]
                        if source["id"] in settlement["declaration"]["source_ids"]}.values()),
                        "boundary_evidence":deepcopy(base["boundary_evidence"]),
                        "declaration_membership":deepcopy(base["declaration_membership"]),
                        "declaration_chair_utterance_ids":list(base["declaration_chair_utterance_ids"])}
                       if settlement["policy"] == "explicit_chair_declaration" else {})}} if settlement else {}
                result = await build_block_document(sources,BridgedDecision() if decision_provider is not None else None,llm,
                    context=context,allowed=allowed,**options)
            payload = {**base,**result}
            if settlement:
                payload["source_ids"] = list(base["source_ids"])
                payload["source_revisions"] = dict(base["source_revisions"])
                payload["source_settlement_policy"] = settlement
                payload["metrics"] = {**(payload.get("metrics") or {}),"source_settlement_policy":settlement}
                payload["issues"] = [*(payload.get("issues") or []),
                    {"stage":"input","code":settlement["policy"],
                     "message":("위원장의 확정된 종료·정회 선언에 따라 현재 판본으로 보정했습니다. 원수신 final·scd 값과 미확정 부분 표시는 그대로 보존합니다."
                        if settlement["policy"] == "explicit_chair_declaration" else
                        "후속 질의 2건이 종료되어 미확정 자막의 현재 판본으로 보정했습니다. 원수신 final·scd는 그대로 보존합니다."),
                     **settlement}]
        except asyncio.CancelledError:
            cancelled = True
            payload = {**base,"status":"interrupted","issues":["서비스 종료로 보정이 중단됐습니다"]}
        except Exception as exc:
            payload = {**base,"status":"failed","issues":["블록 보정을 완료하지 못해 원문을 유지했습니다"],
                       "failure_code":"timeout" if isinstance(exc,asyncio.TimeoutError) else type(exc).__name__}
        payload["source_kind"] = base.get("source_kind") or "assembly_caption"
        if text_provider_metadata is not None:
            payload["text_provider"] = {**text_provider_metadata, **text_calls,
                                        "executed": text_calls["calls_completed"] > 0}
        try:
            saved = await self._finalize_isolated(job,sid,block_id,row,base,payload,execution,cancelled)
        except asyncio.CancelledError:
            cancelled = True
            payload = {**base,"status":"interrupted","issues":["서비스 종료로 보정이 중단됐습니다"],
                       "source_kind": base.get("source_kind") or "assembly_caption"}
            saved = await self._finalize_isolated(job,sid,block_id,row,base,payload,execution,True)
        if cancelled or job.cancelled.is_set():
            raise asyncio.CancelledError
        return saved

    def _summary_value(self, sid, bid, document):
        summary = document.get("summary") or {}
        if document.get("status") in {"stale","failed","interrupted","protected"} or summary.get("status")!="draft":
            return None
        block = self.rt.repo.get_block(bid)
        if not block or block.get("is_open") or block.get("review_status")=="reviewed":
            return None
        old = block.get("summary") or {}
        if isinstance(old,dict) and old.get("edited_by"):
            return None
        return {**summary,"one_line":summary.get("title", ""),"question_points":summary.get("bullets",[]),
            "answer_points":[],"issues":[],"data_requests":[],"commitments":[],
            "provider":"closed_block","source_document_id":document["id"],"generated_at":self.clock(),"ai_draft":True}

    async def _finalize_isolated(self, job, sid, bid, row, base, payload, execution, cancelled):
        db = self.rt.repo.db
        for _ in range(12):
            state = self._settings_epoch,self._closed
            token = db.change_token() if hasattr(db,"change_token") else None
            with self.reader() as view:
                current = view._current(sid,row["id"],base["protection_hash"])
                enabled = (self.builder is not None or view._configured()) and view._execution_key()==execution
                candidate = deepcopy(payload)
                if current and (cancelled or self._closed or job.cancelled.is_set()):
                    candidate = {**base,"status":"interrupted","issues":["서비스 종료로 보정이 중단됐습니다"]}
                elif not current or not enabled:
                    candidate = {**base,"status":"stale","issues":["입력·사람 판본·처리 설정이 변경되어 이전 결과를 표시하지 않습니다"]}
                summary = view._summary_value(sid,bid,{**candidate,"id":row["id"]})
            accepted,saved = await job.bridge(self._commit_result(sid,bid,row["id"],candidate,summary,token,state),cleanup=True)
            if accepted:
                return saved
            await asyncio.sleep(0)
        # 무관한 수신이 계속 바뀌어도 검증되지 않은 요약을 채택하지 않는다.
        stale = {**base,"status":"stale","issues":["동시 입력 변경이 이어져 저장 결과를 표시하지 않습니다"]}
        _,saved = await job.bridge(self._commit_result(sid,bid,row["id"],stale,None,None),cleanup=True)
        return saved

    async def _commit_result(self, sid, bid, did, payload, summary, token, state=None):
        db = self.rt.repo.db
        with db.transaction():
            if (token is not None and db.change_token()!=token
                    or state is not None and state!=(self._settings_epoch,self._closed)):
                return False,None
            row,block = self.rt.repo.stage_commit_result(sid,did,bid,payload,summary)
        from . import serialize
        if block:
            self.rt.bus.publish(sid,"block.upsert",serialize.block(block))
        if row:
            self.rt.bus.publish(sid,"transcript.stage",{"id":row["id"]})
        return True,row

    def _save_summary(self, sid: str, bid: int, document: dict) -> None:
        summary = document.get("summary") or {}
        if document.get("status") in {"stale","failed","interrupted","protected"} or summary.get("status")!="draft":
            return
        block = self.rt.repo.get_block(bid)
        if not block or block.get("is_open") or block.get("review_status")=="reviewed":
            return
        old = block.get("summary") or {}
        if isinstance(old,dict) and old.get("edited_by"):
            return
        value = {**summary,"one_line":summary.get("title", ""),"question_points":summary.get("bullets",[]),
                 "answer_points":[],"issues":[],"data_requests":[],"commitments":[],
                 "provider":"closed_block","source_document_id":document["id"],"generated_at":self.clock(),"ai_draft":True}
        updated = self.rt.repo.update_block(bid,summary=value,summary_at=self.clock())
        from . import serialize
        self.rt.bus.publish(sid,"block.upsert",serialize.block(updated))

    def _current(self, sid: str, did: int, protection_hash: str) -> bool:
        document = self.rt.repo.get_stage_document(sid,did)
        return bool(document and document.get("protection_hash")==protection_hash
                    and self.rt.repo.stage_document_current(sid,did))

    def snapshot(self, sid: str, *, before: int | None = None, limit: int = 100,
                 block_id: int | None = None, _read_context: dict | None = None) -> dict:
        from .display_reads import enabled, stage_page
        from .recording_stages import is_recording_session
        if is_recording_session(self.rt.repo, sid):
            if enabled(self.rt.repo, sid):
                return stage_page(self.rt.repo.display_snapshot(sid), before=before, limit=limit, block_id=block_id)
            # 아직 표시 작업자가 저장하지 않은 파일은 원행만 보이며 모델·표시 작업을 조회에서 만들지 않는다.
            return {"version":3,"mode":self.MODE,"sources":[],"documents":[],"live_turns":[],
                    "valid_document_ids":[],"valid_live_turn_ids":[],"next_before":None,
                    "utterances":[],"blocks":[]}
        if not getattr(self.rt.repo, "_display_building", False) and (
                enabled(self.rt.repo, sid) or getattr(self.rt, "display", None) is not None
                and (self.rt.repo.get_session(sid) or {}).get("source_type") == "assembly_caption"):
            # 저장 표시본의 페이지 선택만 한다. 조회가 검증·화자 구성·표시본 생성 작업을 예약하지 않는다.
            return stage_page(self.rt.repo.display_snapshot(sid), before=before, limit=limit, block_id=block_id)
        snapshot_at = now_ms()
        sources = self.rt.repo.list_caption_sources(sid,before=before,limit=limit) if block_id is None else []
        documents = self.rt.repo.list_stage_documents(sid)
        valid = []
        for document in documents:
            if document.get("mode") != self.MODE or document["status"] == "stale":
                continue
            try:
                if self._current(sid, document["id"], document.get("protection_hash", "")):
                    valid.append(document)
            except Exception:
                logger.warning("보정 판본 현재성을 확인할 수 없어 표시에서 제외합니다")
        page_ids = {source["id"] for source in sources}
        current = [document for document in valid if (document.get("block_id") == block_id if block_id is not None
                   else page_ids.intersection(document.get("source_ids", [])))]
        from .live_turns import session_live_turns, session_source_guesses
        valid_turns = session_live_turns(self.rt.repo, sid)
        source_guesses = session_source_guesses(self.rt.repo, sid)
        if _read_context is not None:
            # 같은 RO 요청의 출처 확장만 재사용한다. 응답·전역 캐시에는 넣지 않는다.
            _read_context.update(live_turns=valid_turns, source_guesses=source_guesses)
        live_turns = [turn for turn in valid_turns if (turn.get("block_id") == block_id if block_id is not None
                      else page_ids.intersection(turn["source_ids"]))]
        needed = ({uid for d in current for uid in d.get("source_ids", [])}
                  | {int(uid) for d in current for uid in (d.get("source_revisions") or {}) if str(uid).isdigit()}
                  | {uid for turn in live_turns for uid in turn["source_ids"]}
                  | {int(uid) for turn in live_turns for uid in
                     (turn.get("speaker_guess") or {}).get("source_revisions", {}) if str(uid).isdigit()}) - page_ids
        # 새 부분 자막은 턴 조립이 끝나기 전에도 그 판본에서 얻은 표시 후보를 전달한다.
        needed |= {int(ref) for uid in page_ids | needed for ref in
                   (source_guesses.get(uid) or {}).get("source_revisions", {}) if str(ref).isdigit()} - page_ids
        extra = self.rt.repo.caption_sources_by_ids(sid,list(needed))
        return {"version":3,"mode":self.MODE,"snapshot_at":snapshot_at,
                "sources":[public_source({**s, "speaker_guess":source_guesses.get(s["id"])})
                           for s in sorted(sources+extra,key=lambda s:s["id"])],
                "documents":[public_document(d) for d in current],
                "valid_document_ids": sorted(d["id"] for d in valid),
                "live_turns": live_turns, "valid_live_turn_ids": sorted(turn["id"] for turn in valid_turns),
                "next_before":sources[0]["id"] if block_id is None and len(sources)==limit else None,
                **({"block_id":block_id} if block_id is not None else {})}

    async def close(self) -> None:
        self._closed=True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task,return_exceptions=True)
        if self._execution is not None:
            await self._execution.close()
