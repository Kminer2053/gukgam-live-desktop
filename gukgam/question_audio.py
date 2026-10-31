"""기존 수신 음성으로 닫힌 질의를 대조한다. 원문과 사람 수정은 보존한다."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from uuid import uuid4

from . import settings
from .config import ROOT
from .db import now_ms
from .reports.question_comparison import final_stage, initial_stages
from .secure_files import make_private_directory, verify_private_file
from .speaker_lab.files import new_directory, safe_output_path, write_json
from .speaker_lab.question_buffer import QuestionAudioBuffer
from .transcript_review import _original_turn, snapshot_hash, take_snapshot, validate_result

PADDING_MS = 60_000
MAX_AUDIO_MS = 900_000
MAX_PENDING = 4
MAX_STORED_BYTES = 512 * 1024 * 1024


def _first_pass_corrections(repo, sid: str, bid: int) -> list[dict]:
    """최종 입력에 사용할 현재 1차 문맥 초안과 그 화자 판본을 함께 고정한다."""
    from .live_review_display import get_live_review_display
    live = get_live_review_display(repo, repo.get_block(bid), include_closed=True, limit_output=False) or {}
    return [{**deepcopy(window["context_correction"]), "parts": deepcopy(window["parts"])}
            for window in live.get("windows", []) if window.get("context_correction")]


def first_pass_hash(corrections: list[dict]) -> str:
    """사용한 1차 화자·본문·문맥 판본을 함께 비교한다."""
    return hashlib.sha256(json.dumps(corrections, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def first_pass_proofs(corrections: list[dict]) -> list[dict]:
    return [{"target_ids": [r["id"] for r in c.get("rows", [])], "content_hash": first_pass_hash([c])}
            for c in corrections]


def first_pass_current(repo, sid: str, bid: int, proofs: list[dict]) -> bool:
    """이미 사용한 1차 초안만 검사한다. 늦게 생긴 다른 행의 초안은 기존 최종안을 무효화하지 않는다."""
    current = {tuple(p["target_ids"]): p["content_hash"]
               for p in first_pass_proofs(_first_pass_corrections(repo, sid, bid))}
    return isinstance(proofs, list) and all(isinstance(p, dict) and isinstance(p.get("target_ids"), list)
        and p["target_ids"] and all(type(uid) is int for uid in p["target_ids"])
        and current.get(tuple(p["target_ids"])) == p.get("content_hash") for p in proofs)


class QuestionAudioService:
    """운영자가 켠 뒤 완결되는 질의만 직렬 처리하는 런타임 선택 기능이다."""

    def __init__(self, rt, *, root: Path | None = None, runner=None, clock=now_ms):
        self.rt, self.clock = rt, clock
        self.root = root or ROOT / "outputs" / "question-audio"
        if runner is None:
            from .speaker_lab.question_runner import LocalQuestionRunner
            runner = LocalQuestionRunner()
        self.runner = runner
        self._sid: str | None = None
        self._buffer: QuestionAudioBuffer | None = None
        self._jobs: dict[tuple[str, int], dict] = {}
        self._reports: dict[tuple[str, int], dict] = {}
        self._evidence: dict[tuple[str, int], dict] = {}
        self._refinement_proofs: dict[tuple[str, int], dict] = {}
        self._comparison_proofs: dict[tuple[str, int], dict] = {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self._closed = False

    def _ready(self, sid):
        session = self.rt.repo.get_session(sid) or {}
        audio = self.rt.audio_review.status(sid)
        review = settings.get(self.rt.repo, "transcript_review")
        llm = settings.get(self.rt.repo, "llm")
        return bool(not self._closed and not self.rt.config.disable_heavy_engines
                    and (self._sid == sid or self._task is None or self._task.done())
                    and self.runner.ready() and session.get("status") == "live"
                    and review.get("enabled") and review.get("auto_block")
                    and llm.get("auto_block_summary", True) and llm.get("provider") == "local_mlx"
                    and session.get("source_type") == "assembly_caption"
                    and audio.get("phase") == "receiving" and audio.get("window_ms")
                    and (self._sid is None or self._sid == sid))

    def status(self, sid):
        enabled = self._sid == sid
        jobs = [self._public_job(j) for (s, _), j in self._jobs.items() if s == sid][-50:]
        active = next((j for j in jobs if j["state"] in {"running", "waiting", "queued", "reviewing"}), None)
        return {"enabled": enabled, "can_enable": self._ready(sid),
                "audio_phase": self.rt.audio_review.status(sid).get("phase"),
                "phase": active["state"] if active else "listening" if enabled else "disabled",
                "detail": ("다음 완결 질의의 앞뒤 60초를 보관해 대조합니다" if enabled
                           else "기존 음성 수신 중 켤 수 있습니다. 재기동 뒤에는 다시 켜야 합니다"),
                "jobs": jobs, "validation_status": "not_audio_verified"}

    @staticmethod
    def _public_job(job):
        return {k: deepcopy(job[k]) for k in ("block_id", "questioner", "state", "detail",
                "created_at", "finished_at", "report_ready") if k in job}

    def _capacity(self):
        if self.root.is_symlink() or self.root.is_junction():
            raise ValueError("음성 보관 경로에 링크를 사용할 수 없습니다")
        if self.root.exists():
            files = list(self.root.rglob("*"))
            if any(p.is_symlink() or p.is_junction() for p in files) or len(files) > 2000:
                raise ValueError("음성 보관 항목 한도를 확인하세요")
            if sum(p.stat().st_size for p in files if p.is_file()) > MAX_STORED_BYTES - 128 * 1024 * 1024:
                raise ValueError("음성 보관 용량 한도에 도달했습니다")
        base = self.root
        while not base.exists():
            base = base.parent
        if shutil.disk_usage(base).free < 256 * 1024 * 1024:
            raise ValueError("음성 대조를 위한 디스크 여유가 부족합니다")

    def enable(self, sid):
        if self._sid == sid:
            return self.status(sid)
        if not self._ready(sid):
            raise ValueError("실제 자막 수집·기존 음성 수신·로컬 모델 준비가 필요합니다")
        self._capacity()
        make_private_directory(safe_output_path(self.root), parents=True, exist_ok=True)
        if os.name == "nt":
            verify_private_file(self.root, directory=True)
        else:
            self.root.chmod(0o700)
        self._buffer = QuestionAudioBuffer(self.root / ("buffer-" + uuid4().hex), max_clip_seconds=900)
        self._sid = sid
        self._generation += 1
        # 이미 지나간 질의와 끊긴 부분을 과거 버퍼에서 복원한 것처럼 처리하지 않는다.
        self._task = asyncio.create_task(self._watch(), name="question-audio-watch")
        return self.status(sid)

    def disable(self, sid):
        if self._sid == sid:
            self._sid = None
            self._generation += 1
            if self._task:
                self._task.cancel()
            if self._buffer:
                self._buffer.close()
                self._buffer = None
            for (s, _), job in self._jobs.items():
                if s == sid and job["state"] in {"waiting", "queued", "running", "reviewing"}:
                    self._finish(job, "held", "대조 중지로 자동 처리를 보류했습니다")
            self._evidence = {key: value for key, value in self._evidence.items() if key[0] != sid}
        return self.status(sid)

    async def close(self):
        self._closed = True
        task = self._task
        if self._sid:
            self.disable(self._sid)
        if task:
            await asyncio.gather(task, return_exceptions=True)

    def feed(self, sid, pcm, *, ts_start_ms=None):
        if self._sid != sid or self._buffer is None:
            return
        try:
            if self._buffer.size_bytes == 0 and ts_start_ms is not None:
                # 최초 부분 밀리초만 제외하고 이후에는 같은 샘플 수로 이어 간다.
                start = math.ceil(ts_start_ms)
                trim = round((start - ts_start_ms) * 16) * 2
                pcm = pcm[trim:]
                if not pcm:
                    return
                self._buffer.feed(pcm, start)
            else:
                self._buffer.feed(pcm)
        except (OSError, ValueError, RuntimeError):
            self.disable(sid)

    def discontinuity(self, sid):
        if self._sid != sid or self._buffer is None:
            return
        try:
            self._buffer.discontinuity()
        except (OSError, ValueError, RuntimeError):
            self.disable(sid)
            return
        for (s, _), job in self._jobs.items():
            if s == sid and job["state"] == "waiting":
                self._finish(job, "held", "수신 단절로 질의 전체 연속 음성을 확보하지 못했습니다")

    def blocks_automatic_summary(self, sid, bid, *, completing=False):
        """맡은 음성 작업은 초안 저장 전·보류 후에 일반 요약으로 대체하지 않는다."""
        job = self._jobs.get((sid, bid))
        allowed = {"reviewing", "draft", "waiting"} if completing else {"reviewing", "draft"}
        return bool(job and job["state"] not in allowed)

    def on_block_closed(self, sid, bid) -> bool:
        """이 종료 질의를 맡았으면 True다. 중복·보류도 일반 검토로 넘기지 않는다."""
        if (sid, bid) in self._jobs:
            return True
        if self._sid != sid:
            return False
        if len(self._jobs) >= 128:
            self.disable(sid)
            return False
        block = self.rt.repo.get_block(bid) or {}
        if block.get("session_id") != sid or block.get("is_open") or block.get("kind") != "question":
            return False
        rows = self.rt.repo.list_utterances(sid, block_id=bid, final_only=True)
        rows = [r for r in rows if r.get("speaker_role") != "system"]
        if not rows:
            return False
        job = {"block_id": bid, "questioner": block.get("questioner") or "위원 확인 필요",
               "state": "waiting", "detail": "뒤쪽 음성 60초와 늦게 오는 자막을 기다립니다",
               "created_at": self.clock(), "report_ready": False,
               "start_ms": min(r["ts_start"] for r in rows) - PADDING_MS,
               "end_ms": max(r.get("ts_end") or r["ts_start"] for r in rows) + PADDING_MS}
        pending = sum(j["state"] in {"waiting", "queued", "running", "reviewing"} for j in self._jobs.values())
        self._jobs[sid, bid] = job
        if pending >= MAX_PENDING:
            self._finish(job, "held", "대기 질의 한도를 넘어 추가 모델 실행을 보류했습니다")
        elif job["end_ms"] - job["start_ms"] > MAX_AUDIO_MS:
            self._finish(job, "held", "앞뒤 여유를 포함한 질의가 15분을 넘어 분할 검토가 필요합니다")
        return True

    def _prior_block_work(self, sid, bid):
        """활성화 전에 예약된 같은 질의의 검토·요약만 끝나기를 기다린다."""
        review_pending = getattr(getattr(self.rt, "reviews", None), "has_pending_closed", None)
        analysis_running = getattr(self.rt.analysis, "is_block_running", None)
        return bool(review_pending and review_pending(sid, bid)
                    or analysis_running and analysis_running(sid, bid))

    def _finish(self, job, state, detail):
        job.update(state=state, detail=detail, finished_at=now_ms())
        for key, candidate in self._jobs.items():
            if candidate is job and key in self._reports:
                self._reports[key].update(status=state, detail=detail)
                if state == "held":
                    for row in self._reports[key].get("rows", []):
                        post = (row.get("stages") or {}).get("post")
                        if post and post.get("status") == "reviewing":
                            post.update(status="unavailable", text_status="skipped", speaker_held=True, text_held=True,
                                        source_note="완성한 사후 보정 판본이 없습니다. " + detail)

    async def _watch(self):
        """PCM 콜백은 파일 수신만 수행하고 무거운 대조는 이 작업에서 직렬 실행한다."""
        try:
            while self._sid:
                await self.process_pending()
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise

    async def process_pending(self):
        if self._lock.locked():
            return
        async with self._lock:
            for (sid, bid), job in list(self._jobs.items()):
                if self._sid == sid and job["state"] == "reviewing" and self.clock() - job["created_at"] > 1800_000:
                    self._finish(job, "held", "문맥 편집 대기 시간을 넘어 보류했습니다")
                    self._evidence.pop((sid, bid), None)
                if self._sid != sid or job["state"] != "waiting" or not self._buffer:
                    continue
                if not self._ready(sid):
                    self._finish(job, "held", "수집·음성 수신 또는 자동 검토 설정이 바뀌어 보류했습니다")
                    continue
                rows = self.rt.repo.list_utterances(sid, block_id=bid, final_only=True)
                rows = [r for r in rows if r.get("speaker_role") != "system"]
                if not rows:
                    self._finish(job, "held", "대상 질의 원문을 확인할 수 없습니다")
                    continue
                job.update(start_ms=min(r["ts_start"] for r in rows) - PADDING_MS,
                           end_ms=max(r.get("ts_end") or r["ts_start"] for r in rows) + PADDING_MS)
                if job["end_ms"] - job["start_ms"] > MAX_AUDIO_MS:
                    self._finish(job, "held", "늦게 도착한 원문을 포함한 음성이 15분 한도를 넘었습니다")
                    continue
                window = self._buffer.window()
                if (not window or window[1] < job["end_ms"]) and self.clock() - job["created_at"] > 180_000:
                    self._finish(job, "held", "후속 음성 수신 대기 시간을 넘었습니다")
                    continue
                if not window:
                    continue
                if job["start_ms"] < window[0]:
                    self._finish(job, "held", "시작 전 여유 음성이 없어 다음 완결 질의를 기다립니다")
                    continue
                if window[1] < job["end_ms"] or self.clock() < job["end_ms"] + 10_000:
                    continue
                if self._prior_block_work(sid, bid):
                    if self.clock() - job["created_at"] > 180_000:
                        self._finish(job, "held", "같은 질의의 선행 검토 대기 시간을 넘어 음성 대조를 보류했습니다")
                    else:
                        job["detail"] = "같은 질의의 선행 검토가 끝나면 음성 대조를 시작합니다"
                    continue
                await self._process(sid, bid, job)

    def _current(self, sid, bid, generation, digest, version):
        from .turn_processing import latest_block_review, _accepted_signature
        block = self.rt.repo.get_block(bid) or {}
        latest = latest_block_review(self.rt.repo, sid, bid)
        return bool(self._sid == sid and generation == self._generation and not block.get("is_open")
                    and block.get("review_status") != "reviewed"
                    and not (block.get("summary") or {}).get("edited_by")
                    and snapshot_hash(take_snapshot(self.rt.repo, sid, bid)) == digest
                    and (latest["id"] if latest else None, latest["status"] if latest else None,
                         _accepted_signature(self.rt.repo, sid, bid)) == version)

    async def _process(self, sid, bid, job):
        from .speaker_lab.question_alignment import align_question
        from .turn_processing import latest_block_review, _accepted_signature
        generation = self._generation
        try:
            if not self._ready(sid):
                raise ValueError("실행 전에 수집·자동 검토 조건이 바뀌었습니다")
            self._capacity()
            snapshot = take_snapshot(self.rt.repo, sid, bid)
            if snapshot["block"].get("review_status") == "reviewed" or snapshot["block"].get("edited_summary"):
                raise ValueError("검수한 질의는 자동 대조 판본으로 덮지 않습니다")
            digest = snapshot_hash(snapshot)
            before = latest_block_review(self.rt.repo, sid, bid)
            if before and before["status"] in {"rejected", "accepted", "stale"}:
                raise ValueError("사람이 결정했거나 무효화한 판본을 자동으로 되살리지 않습니다")
            version = (before["id"] if before else None, before["status"] if before else None,
                       _accepted_signature(self.rt.repo, sid, bid))
            rows = self.rt.repo.list_utterances(sid, block_id=bid, final_only=True)
            if any(r.get("review_parts") for r in rows):
                raise ValueError("사람이 채택한 부분은 자동 전체 초안으로 가리지 않습니다")
            base = deepcopy((before or {}).get("payload", {}).get("turns") or [])
            comparison_base = deepcopy(base) or [_original_turn(r) for r in snapshot["utterances"]]
            try:
                validate_result(snapshot, base)
            except (ValueError, KeyError, TypeError):
                from .transcript_review import _protected
                base = [{**_original_turn(r), **({} if _protected(snapshot, r) else
                        {"role": "unknown", "speaker": ""})} for r in snapshot["utterances"]]
                validate_result(snapshot, base)
            out = new_directory(self.root / uuid4().hex)
            audio = self._buffer.extract(job["start_ms"], job["end_ms"], out / "source")
            context = [r for r in self.rt.repo.list_utterances(sid, final_only=True)
                       if r["ts_start"] <= job["end_ms"] + PADDING_MS
                       and (r.get("ts_end") or r["ts_start"]) >= job["start_ms"] - PADDING_MS
                       and r.get("speaker_role") != "system"]
            if len(context) > 300 or sum(len(r["text"]) for r in context) > 30000:
                raise ValueError("질의 주변 원자막 대조 크기 한도를 넘었습니다")
            captions = {"captions": [{"id": r["id"], "text": r["text"]} for r in context]}
            write_json(out / "captions.json", captions)
            write_json(out / "snapshot.json", snapshot)
            job.update(state="running", detail="Nemotron 화자 분리 뒤 Whisper 전사를 순차 실행합니다")
            result = await self.runner.run(Path(audio["audio_path"]), out / "captions.json", out / "models")
            if result["media_sha256"] != audio["sha256"]:
                raise ValueError("음성 결과와 보관 파일 해시가 다릅니다")
            aligned = align_question(captions["captions"], result["words"], result["segments"], media_sha256=audio["sha256"])
            write_json(out / "alignment.json", aligned)
            if not self._current(sid, bid, generation, digest, version):
                raise ValueError("대조 중 원문·검수·표시 판본이 바뀌어 자동 반영을 보류했습니다")
            if not self._ready(sid):
                raise ValueError("대조 중 수집 또는 자동 검토 설정이 바뀌었습니다")
            ids = {r["id"] for r in snapshot["utterances"]}
            evidence = self._make_evidence(aligned, result["words"], ids)
            # 자막에 대응하지 못한 음성도 최종 편집 입력에서 버리지 않는다.
            # 정렬된 조각은 화자 참고이고 전체 Whisper 전사는 별도의 독립 근거다.
            whole_audio = {"words": [{"text": w["text"], "start_ms": round(w["start"] * 1000),
                                      "end_ms": round(w["end"] * 1000)} for w in result["words"]],
                "segments": [{"speaker": p["speaker"], "start_ms": round(p["start"] * 1000),
                              "end_ms": round(p["end"] * 1000)} for p in result["segments"]],
                "start_ms": 0, "end_ms": job["end_ms"] - job["start_ms"],
                "core_start_ms": PADDING_MS, "core_end_ms": job["end_ms"] - job["start_ms"] - PADDING_MS,
                "recording_start_ms": job["start_ms"], "media_sha256": audio["sha256"],
                "audio_verified": False}
            corrections = _first_pass_corrections(self.rt.repo, sid, bid)
            positions = {r["id"]: index for index, r in enumerate(context)}
            core_positions = [positions[uid] for uid in ids if uid in positions]
            left, right = min(core_positions), max(core_positions)
            adjacent = ([{**r, "position": "before"} for r in context[max(0, left - 4):left]]
                        + [{**r, "position": "after"} for r in context[right + 1:right + 5]])
            self._evidence[sid, bid] = {"input_hash": digest, "rows": evidence,
                "context": [{"id": r["id"], "text": r["text"][:1000], "position": r["position"],
                             "source_stamp": {k: r.get(k) for k in
                                 ("ts_start", "ts_end", "source_order", "seq", "is_final", "speaker_role")}}
                            for r in adjacent],
                "whole_audio": whole_audio, "realtime_corrections": corrections,
                "base": base, "out": out, "generation": generation}
            report_rows = [{"utterance_id": r["id"], "original_text": r["text"],
                           "before_speaker": " / ".join(p["speaker"] for p in comparison_base if p["utterance_id"] == r["id"]),
                           "before_role": " / ".join(p["role"] for p in comparison_base if p["utterance_id"] == r["id"]),
                           "audio_text": next((e["audio_text"] for e in evidence if e["id"] == r["id"]), ""),
                           "stages": initial_stages(r, next((c for c in corrections
                               if any(row["id"] == r["id"] for row in c.get("rows", []))), None),
                               next((c["parts"] for c in corrections
                               if any(row["id"] == r["id"] for row in c.get("rows", []))), None),
                               context_radius=next((c.get("context_radius", 2) for c in corrections
                               if any(row["id"] == r["id"] for row in c.get("rows", []))), None)),
                           "ts_start": r.get("ts_start"), "ts_end": r.get("ts_end"),
                           "reason": "음성 후보와 자막 문맥 대조 중"} for r in snapshot["utterances"]]
            self._reports[sid, bid] = {"status": "reviewing", "rows": report_rows,
                "comparison_version": 1, "stages_order": ["raw", "live", "post"],
                "detail": "원문을 보존한 화자·경계 보정과 동일 판본 요약을 준비합니다",
                "validation_status": "not_audio_verified", "summary": {}}
            # 음성·전사·경로는 로컬 파일/메모리에만 둔다. 기존 검토 저장에는 검증된 자막 부분만 넣는다.
            payload = {"status": "draft", "input_hash": digest, "turns": base,
                       "model": "질의 음성 대조", "audio_evidence_required": True,
                       "speaker_text_pipeline": 2,
                       "issues": [], "generated_at": self.clock()}
            saved = self.rt.repo.save_transcript_review(sid, bid, payload)
            self._evidence[sid, bid]["source_review_id"] = saved["id"]
            self._comparison_proofs[sid, bid] = {"input_hash": digest, "accepted_signature": version[2],
                "review_id": saved["id"], "context": self._context_proofs(self._evidence[sid, bid]["context"])}
            job.update(state="reviewing", detail="음성 근거와 문맥으로 직전 질의 초안을 정리합니다", report_ready=True)
            self.rt.analysis.on_review_accepted(sid, bid)
        except asyncio.CancelledError:
            self._finish(job, "held", "대조가 취소되어 원문과 이전 판본을 유지합니다")
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self._finish(job, "held", "연속 음성·시간 대응·원문 판본 또는 로컬 실행 조건을 확인해야 합니다")

    @staticmethod
    def _make_evidence(aligned, words, ids):
        rows = []
        for row in aligned["rows"]:
            if row["id"] not in ids:
                continue
            parts = [{k: p.get(k) for k in ("start", "end", "text", "speaker_id", "start_ms", "end_ms")}
                     for p in row.get("parts", []) if p.get("status") == "draft" and p.get("speaker_id")]
            anchors = row.get("anchors") or []
            # 양끝의 검증된 앵커 사이 전체 전사를 비교한다. 불일치 단어를 일치 부분만으로 숨기지 않는다.
            spans = ([(min(a["start_ms"] for a in anchors) / 1000,
                       max(a["end_ms"] for a in anchors) / 1000)] if anchors else [])
            audio_text = " ".join(w["text"].strip() for w in words
                                  if any(w["start"] < hi and w["end"] > lo for lo, hi in spans))
            rows.append({"id": row["id"], "parts": parts, "audio_text": audio_text,
                         "audio_range_ms": ([min(a["start_ms"] for a in anchors), max(a["end_ms"] for a in anchors)]
                                            if anchors else None),
                         "status": row.get("status"), "comparison_only": True,
                         "note": "전사는 양끝 앵커 사이 비교 후보이며 문자 치환 근거가 아님. 익명 ID는 이 녹음 안에서만 유효"})
        return rows

    @staticmethod
    def _context_proofs(rows):
        return [{"id": r["id"], "position": r["position"],
                 "text_sha256": hashlib.sha256(r["text"].encode()).hexdigest(),
                 **({"source_stamp": deepcopy(r["source_stamp"])} if "source_stamp" in r else {})}
                for r in rows]

    def evidence(self, sid, bid, digest, source_review_id):
        value = self._evidence.get((sid, bid))
        if (not value or self._sid != sid or value["generation"] != self._generation
                or value["input_hash"] != digest or value["source_review_id"] != source_review_id):
            return None
        from .review_display import rebuild_question_context
        proofs = self._context_proofs(value["context"])
        try:
            rebuild_question_context(self.rt.repo, take_snapshot(self.rt.repo, sid, bid), proofs)
            if "realtime_corrections" in value:
                if not first_pass_current(self.rt.repo, sid, bid, first_pass_proofs(value["realtime_corrections"])):
                    return None
        except (KeyError, ValueError, TypeError):
            return None
        return {"rows": deepcopy(value["rows"]), "context": deepcopy(value["context"]),
                **{key: deepcopy(value[key]) for key in ("whole_audio", "realtime_corrections") if key in value}}

    def store_refinement(self, sid, bid, source_review_id, snapshot, result):
        """전사·교정 후보는 운영 DB·클라우드 대신 기존 로컬 근거와 함께 보관한다."""
        evidence = self._evidence.get((sid, bid))
        if (not evidence or evidence.get("source_review_id") != source_review_id
                or evidence.get("input_hash") != snapshot_hash(snapshot)):
            return
        from .transcript_correction import corrected_rows
        from .turn_processing import _accepted_signature
        correction = result["text_correction"]
        rows = []
        if correction.get("source_hash"):
            correction_snapshot = deepcopy(snapshot)
            correction_snapshot["speaker_hold_ids"] = deepcopy(result.get("speaker_hold_ids", []))
            rows = corrected_rows(correction_snapshot, result["turns"], correction)
        refinement = {"speaker_status": result["speaker_review"]["status"],
            "text_status": correction["status"], "timings": deepcopy(result["timings"]),
            "text_model_status": correction.get("model_status", "not_called"),
            "text_reason": correction.get("reason", ""),
            "patches": deepcopy(correction.get("patches", [])),
            "held": deepcopy(correction.get("held", [])), "rows": rows,
            "held_utterance_ids": deepcopy(result.get("speaker_hold_ids", [])),
            "held_continuity_boundaries": [{k: p[k] for k in ("previous_utterance_id", "utterance_id")}
                for p in result["speaker_review"].get("held_continuity_boundaries", [])],
            "complete": result["speaker_review"].get("complete") is True,
            "source_text_changed": False, "audio_verified": False}
        if result.get("final_document"):
            refinement["final_document"] = deepcopy(result["final_document"])
            refinement["complete"] = result["final_document"].get("status") == "draft"
        proof = {"input_hash": snapshot_hash(snapshot),
                 "accepted_signature": _accepted_signature(self.rt.repo, sid, bid),
                 "review_id": source_review_id, "context": self._context_proofs(evidence["context"])}
        if result.get("final_document"):
            proof["first_pass_proofs"] = first_pass_proofs(evidence.get("realtime_corrections", []))
        write_json(evidence["out"] / "two-stage-review.json", result)
        evidence["refinement"] = refinement
        self._refinement_proofs[sid, bid] = proof

    def on_integrated_review(self, sid, bid, source_review_id, payload):
        evidence = self._evidence.get((sid, bid))
        if not evidence or evidence.get("source_review_id") != source_review_id:
            return
        report = self._reports[sid, bid]
        from .turn_processing import latest_block_review
        latest = latest_block_review(self.rt.repo, sid, bid)
        if latest and (latest["id"] == source_review_id
                       or latest.get("payload", {}).get("source_review_id") == source_review_id):
            for proofs in (self._comparison_proofs, self._refinement_proofs):
                if (sid, bid) in proofs:
                    proofs[sid, bid]["review_id"] = latest["id"]
        if evidence.get("refinement"):
            report["refinement"] = deepcopy(evidence["refinement"])
            if report["refinement"].get("final_document"):
                report["final_document"] = deepcopy(report["refinement"]["final_document"])
        for row in report["rows"]:
            parts = [p for p in payload["turns"] if p["utterance_id"] == row["utterance_id"]]
            if report.get("comparison_version") == 1:
                row["stages"]["post"] = final_stage(row, payload["turns"], report.get("refinement") or {})
            row.update(proposed_speaker=" / ".join(p["speaker"] for p in parts),
                       proposed_role=" / ".join(p["role"] for p in parts),
                       parts=[{"start": p["start"], "end": p["end"], "role": p["role"], "speaker": p["speaker"],
                               "text": row["original_text"][p["start"]:p["end"]]} for p in parts],
                       reason="음성 후보·자막 문맥 기반 AI 초안, 원음 정답 미확인")
        editor = payload["presentation"]["editor"]
        detail = "음성 근거를 반영한 보정 초안·같은 판본 요약입니다. 원음 정답 검수는 별도입니다"
        state = "draft" if editor.get("status") in {"refined", "unchanged", "partial"} else "held"
        if state == "held":
            detail = "문맥 편집을 완료하지 못해 기존 초안을 유지했습니다"
        if evidence.get("refinement"):
            detail = "화자 판정과 전사 교정안을 분리했습니다. 단어 수정은 검토 후보이며 원문·요약 본문에 채택하지 않았습니다"
        if report.get("final_document"):
            detail = "원자막·1차 문맥 초안·질의 전체 음성 전사를 대조한 최종 초안입니다. 원음 검수·채택은 별도입니다"
        report.update(status=state, detail=detail, summary=deepcopy(payload["presentation"]["summary"]))
        ownership = payload["presentation"].get("ownership")
        if ownership:
            report["ownership"] = {k: deepcopy(ownership.get(k)) for k in ("status", "questioner", "scope", "issues")}
            if ownership["status"] == "held":
                state, detail = "held", "질의 경계·질의자 확인이 필요해 자동 요약을 보류했습니다"
                report.update(status=state, detail=detail)
        self._finish(self._jobs[sid, bid], state, detail)
        try:
            write_json(evidence["out"] / "comparison.json", report)
        except (OSError, ValueError):
            self._finish(self._jobs[sid, bid], "held", "비교 보고서 보관 상태를 확인하세요")

    def report(self, sid, bid):
        # 모델 원출력·로컬 파일 경로·인증 값은 API에 포함하지 않는다.
        report = deepcopy(self._reports[sid, bid])
        if not report.get("refinement") and report.get("comparison_version") != 1:
            return report  # 기존 대조 보고서 계약은 바꾸지 않는다.
        from .turn_processing import _accepted_signature, latest_block_review
        from .review_display import rebuild_question_context
        proofs = self._refinement_proofs if report.get("refinement") else self._comparison_proofs
        proof = proofs.get((sid, bid))
        try:
            snapshot = take_snapshot(self.rt.repo, sid, bid)
            latest = latest_block_review(self.rt.repo, sid, bid)
            current = bool(proof and latest and latest["id"] == proof["review_id"] and latest["status"] == "draft"
                           and proof["input_hash"] == snapshot_hash(snapshot)
                           and proof["accepted_signature"] == _accepted_signature(self.rt.repo, sid, bid))
            if current:
                rebuild_question_context(self.rt.repo, snapshot, proof["context"])
                if "first_pass_proofs" in proof:
                    current = first_pass_current(self.rt.repo, sid, bid, proof["first_pass_proofs"])
        except (KeyError, TypeError, ValueError):
            current = False
        if not current:
            # 저장 당시 증거는 유지하되 바뀐 원문·사람 판본 위에 과거 후보를 표시하지 않는다.
            report.update(status="stale", rows=[], summary={},
                detail="원문·앞뒤 문맥 또는 검토 판본이 바뀌어 이전 교정안을 숨겼습니다. 저장 당시 근거는 로컬에 보존됩니다.")
            report.pop("ownership", None)
            report.pop("final_document", None)
            if report.get("refinement"):
                report["refinement"].pop("final_document", None)
                report["refinement"].update(status="stale", speaker_status="stale", text_status="stale",
                                          patches=[], held=[], rows=[], held_utterance_ids=[],
                                          held_continuity_boundaries=[], complete=False)
        return report
