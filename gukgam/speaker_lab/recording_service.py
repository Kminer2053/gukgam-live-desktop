"""기존 음성 수신에 붙는 검증 녹음. 수집·모델·운영 DB를 조작하지 않는다."""
from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid
import wave
import zipfile

from .. import maintenance
from ..secure_files import create_private_file, make_private_directory, open_regular_file
from .files import read_json, safe_output_path, wav_info
from .recording import ContinuousRecording, _REASONS

_ID = re.compile(r"[a-f0-9]{32}\Z")
MAX_STORED_BYTES = 256 * 1024 * 1024
RESERVE_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 1024
BUNDLE_RESERVE_BYTES = 128 * 1024 * 1024
_DETAIL = {
    "recording": "연속 녹음 중입니다",
    "completed": "요청한 길이의 녹음을 마쳤습니다. 음성·자막 정렬은 별도 검증이 필요합니다",
    "interrupted": "녹음이 중단되었습니다. 저장된 부분과 중단 사유를 확인하세요",
    "failed": "저장에 실패했습니다. 디스크와 보관 경로를 확인하세요",
}


class RecordingService:
    """한 번에 한 녹음만 받고 종료 파일은 재기동 후에도 조회한다."""

    def __init__(self, rt, audio):
        self.rt, self.audio = rt, audio
        self.root = Path(rt.config.data_dir).parent / "outputs" / "speaker-recordings"
        self._latest: dict[str, ContinuousRecording] = {}
        self._watcher: asyncio.Task | None = None
        self._last_feed = 0.0
        self.no_audio_timeout = 20.0
        self._download_lock = asyncio.Lock()

    def _usage(self) -> int:
        safe_output_path(self.root)
        if not self.root.exists():
            return 0
        total = 0
        count = 0
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("녹음 보관 경로에 링크가 있습니다")
                count += 1
                if count > MAX_ENTRIES:
                    raise ValueError("녹음 보관 파일 한도에 도달했습니다")
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
        return total

    def _saved(self, sid: str, recording_id: str | None = None) -> tuple[dict, Path] | None:
        safe_output_path(self.root)
        if not self.root.exists():
            return None
        if recording_id is not None and not _ID.fullmatch(recording_id):
            raise ValueError("녹음 식별자를 확인하세요")
        candidates = [self.root / recording_id] if recording_id else []
        if recording_id is None:
            for count, entry in enumerate(self.root.iterdir(), 1):
                if count > MAX_ENTRIES:
                    raise ValueError("녹음 보관 파일 한도에 도달했습니다")
                if _ID.fullmatch(entry.name):
                    candidates.append(entry)
        found = []
        for folder in candidates:
            safe_output_path(folder)
            manifest = safe_output_path(folder / "manifest.json")
            if not manifest.is_file():
                continue
            try:
                value = read_json(manifest, 64 * 1024)
            except (ValueError, OSError):
                continue
            if self._valid_manifest(value, sid):
                value["id"] = folder.name
                reason = value.get("reason")
                if not isinstance(reason, str) or reason not in _REASONS | {
                    "invalid_pcm", "invalid_timestamp", "storage_error", "manifest_error", "process_ended"
                }:
                    value["reason"] = None
                # 현재 프로세스가 소유하지 않는 열린 파일을 정상 완료로 승격하지 않는다.
                if value.get("state") == "recording" or value.get("active"):
                    value.update(state="interrupted", active=False, reason="process_ended", sha256=None)
                found.append((value, folder))
        return max(found, key=lambda pair: (pair[0]["created_at_ms"], pair[1].name)) if found else None

    @staticmethod
    def _valid_manifest(value, sid: str) -> bool:
        """종료 기록도 형식·크기·시각을 확인한 값만 상태 화면에 올린다."""
        if not isinstance(value, dict) or value.get("session_id") != sid:
            return False
        if not isinstance(value.get("state"), str) or value["state"] not in _DETAIL:
            return False
        if not isinstance(value.get("active"), bool) or not isinstance(value.get("manifest_saved"), bool):
            return False
        if (value.get("sample_rate"), value.get("channels"), value.get("sample_width")) != (16000, 1, 2):
            return False
        for key, lo, hi in (("sample_count", 0, 900 * 16000),
                            ("requested_duration_seconds", 600, 900), ("created_at_ms", 0, 2**53)):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, int) or not lo <= number <= hi:
                return False
        if value["sample_count"] > value["requested_duration_seconds"] * 16000:
            return False
        if value["state"] == "completed" and value["sample_count"] != value["requested_duration_seconds"] * 16000:
            return False
        if value["sample_count"]:
            start, end = value.get("start_ms"), value.get("end_ms")
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                   for x in (start, end)) or not 0 <= start < end < 2**53:
                return False
            if abs(end - start - value["sample_count"] / 16) > 0.001:
                return False
        elif value.get("start_ms") is not None or value.get("end_ms") is not None:
            return False
        sha = value.get("sha256")
        return sha is None or isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha) is not None

    @staticmethod
    def _public(value: dict) -> dict:
        state = value.get("state", "failed")
        seconds = value.get("sample_count", 0) / 16000
        return {
            "id": value.get("id"), "status": state, "active": bool(value.get("active")),
            "duration_seconds": value.get("requested_duration_seconds", 900),
            "recorded_seconds": seconds, "start_ms": value.get("start_ms"), "end_ms": value.get("end_ms"),
            "reason": value.get("reason"), "detail": _DETAIL.get(state, _DETAIL["failed"]),
            "download_ready": state != "recording" and seconds > 0 and bool(value.get("sha256"))
                              and value.get("manifest_saved") is True,
            "timing_uncertain": True, "audio_verified": False, "quality_resolved": False,
        }

    def status(self, sid: str) -> dict:
        writer = self._latest.get(sid)
        saved = None if writer else self._saved(sid)
        value = writer.status() if writer else saved[0] if saved else None
        return {"recording": self._public(value) if value else None,
                "audio_phase": self.audio.status(sid).get("phase", "disabled")}

    def start(self, sid: str, seconds: int) -> dict:
        session = self.rt.repo.get_session(sid)
        if not session or session.get("status") != "live" or session.get("source_type") != "assembly_caption":
            raise ValueError("수집 중인 국회 자막 회의에서 시작하세요")
        if not self.rt.ingest.is_running(sid) or self.audio.status(sid).get("phase") != "receiving":
            raise ValueError("설정에서 최근 음성 버퍼를 켜고 음성이 수신된 뒤 시작하세요")
        if any(writer.active for writer in self._latest.values()):
            raise ValueError("이미 녹음 중입니다. 현재 녹음을 마친 뒤 시작하세요")
        capture_bytes = seconds * 32000 + 65536
        # 저장에 성공한 자료는 이후 묶음 생성 공간도 남아 있어야 한다.
        if self._usage() + capture_bytes + BUNDLE_RESERVE_BYTES > MAX_STORED_BYTES:
            raise ValueError("검증 녹음 보관 한도에 도달했습니다. 보관 자료를 확인하세요")
        existing = next(p for p in (self.root, *self.root.parents) if p.exists())
        if shutil.disk_usage(existing).free < capture_bytes + BUNDLE_RESERVE_BYTES + RESERVE_BYTES:
            raise ValueError("녹음과 자료 묶음을 위한 디스크 여유가 부족합니다")
        writer = ContinuousRecording(self.root, sid, seconds)
        self._latest[sid] = writer
        self._last_feed = time.monotonic()
        if self._watcher is not None:
            self._watcher.cancel()
        self._watcher = asyncio.create_task(self._watch(sid, writer, seconds), name=f"recording-limit-{sid}")
        return self.status(sid)

    async def _watch(self, sid: str, writer: ContinuousRecording, seconds: int) -> None:
        deadline = time.monotonic() + seconds + 30
        try:
            while writer.active:
                await asyncio.sleep(min(1.0, self.no_audio_timeout))
                if self._latest.get(sid) is not writer:
                    return
                if time.monotonic() - self._last_feed >= self.no_audio_timeout:
                    writer.discontinuity("audio_timeout")
                elif time.monotonic() >= deadline:
                    writer.finish("wall_time_limit")
        except asyncio.CancelledError:
            pass

    def feed(self, sid: str, pcm: bytes, ts_start_ms: int | None = None) -> None:
        writer = self._latest.get(sid)
        if writer is not None and writer.active:
            self._last_feed = time.monotonic()
            writer.feed(pcm, ts_start_ms)

    def stop(self, sid: str, reason: str = "operator_stop") -> dict:
        self.finish_owned(sid, reason)
        return self.status(sid)

    def finish_owned(self, sid: str, reason: str) -> None:
        """수집 생명주기에서는 과거 보관 폴더를 읽지 않고 소유한 녹음만 닫는다."""
        writer = self._latest.get(sid)
        if writer is not None and writer.active:
            writer.finish(reason)

    def discontinuity(self, sid: str) -> None:
        writer = self._latest.get(sid)
        if writer is not None and writer.active:
            writer.discontinuity("audio_discontinuity")

    async def close(self) -> None:
        for sid in list(self._latest):
            self.finish_owned(sid, "service_stopped")
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None

    def bundle(self, sid: str, recording_id: str) -> tuple[Path, Path]:
        """종료 자료를 새 임시 ZIP으로 묶는다. 원본과 기존 결과는 덮어쓰지 않는다."""
        from .capture_export import export_captions

        writer = self._latest.get(sid)
        if writer and writer.path.name == recording_id:
            value, folder = writer.status(), writer.path
        else:
            saved = self._saved(sid, recording_id)
            if saved is None:
                raise ValueError("녹음 자료를 찾을 수 없습니다")
            value, folder = saved
        if not self._public(value)["download_ready"]:
            raise ValueError("녹음이 끝난 뒤 저장된 자료를 내려받으세요")
        if self._usage() + BUNDLE_RESERVE_BYTES > MAX_STORED_BYTES:
            raise ValueError("녹음 자료 묶음의 보관 한도를 확인하세요")
        if shutil.disk_usage(self.root).free < BUNDLE_RESERVE_BYTES + RESERVE_BYTES:
            raise ValueError("녹음 자료 묶음을 위한 디스크 여유가 부족합니다")
        audio = safe_output_path(folder / "audio.wav")
        if audio.stat().st_size > 900 * 32000 + 4096:
            raise ValueError("저장된 원음의 무결성을 확인할 수 없습니다")
        try:
            info = wav_info(audio)
        except (wave.Error, EOFError):
            raise ValueError("저장된 원음의 형식을 확인할 수 없습니다") from None
        if (info["sha256"] != value.get("sha256")
                or round(info["duration_seconds"] * 16000) != value["sample_count"]):
            raise ValueError("저장된 원음과 녹음 기록이 다릅니다")
        temp = safe_output_path(folder / f"download-{uuid.uuid4().hex}")
        make_private_directory(temp)
        try:
            export_captions(maintenance.raw_path(self.rt.config, sid), temp / "captions",
                            session_id=sid, start_ms=math.floor(value["start_ms"]),
                            end_ms=math.ceil(value["end_ms"]))
            archive = temp / "recording.zip"
            with create_private_file(archive) as out, zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as z:
                def add_file(path, name):
                    with open_regular_file(path) as source, z.open(name, "w") as destination:
                        shutil.copyfileobj(source, destination, length=64 * 1024)
                add_file(audio, "audio.wav")
                add_file(safe_output_path(folder / "manifest.json"), "recording.json")
                for file in sorted((temp / "captions").iterdir()):
                    if file.is_file() and not file.is_symlink():
                        add_file(file, file.name)
            return archive, temp
        except BaseException:
            shutil.rmtree(temp)
            raise
