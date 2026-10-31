"""녹음 파일 수집원: ffmpeg 로 16kHz mono s16le 디코드 → VAD → STT. 실시간보다 빠르게 처리하고 끝나면 정상 종료.

source_config: path(필수, 상대경로는 업로드 폴더 기준), base_time_ms(파일 0초의 실제 시각, 선택 — 없으면 처리 시작 시각).
일시정지 후 다시 시작하면 '전사까지 끝난' 마지막 구간의 끝(state.resume_ms)부터 이어 간다.
읽기 위치(position_ms)는 진행률 표시용이다(읽었지만 아직 전사하지 못한 구간은 다시 처리한다).
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections import deque
from typing import AsyncIterator

from ..db import now_ms
from .audio import BYTES_PER_MS, AudioChunk, run_audio_pipeline
from .base import Emit, Source, SourceContext, SourceError, resolve_path
from .hls import drain_stderr, kill_proc, stop_task

logger = logging.getLogger(__name__)

READ_BYTES = 32000           # 1초


async def probe_duration_ms(path: str) -> int | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return int(float(json.loads(out)["format"]["duration"]) * 1000)
    except Exception:
        return None


class FileSource(Source):
    kind = "file"

    def __init__(self, ctx: SourceContext):
        super().__init__(ctx)
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: deque[str] = deque(maxlen=20)

    async def close(self) -> None:
        await kill_proc(self._proc, drain=False)

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        p = str(self.config.get("path") or "").strip()
        if not p:
            raise SourceError("처리할 녹음 파일이 없습니다(파일을 먼저 올려 주세요)")
        path = resolve_path(self.ctx, p)
        if not path.is_file():
            raise SourceError(f"파일을 찾을 수 없습니다: {p}")
        if not shutil.which("ffmpeg"):
            raise SourceError("ffmpeg 를 찾을 수 없습니다(설치 필요)")
        duration = await probe_duration_ms(str(path))
        prev = self.ctx.prev_state
        start_ms = 0
        if prev.get("file_path") == str(path) and prev.get("phase") != "done":
            if prev.get("resume_ms") is not None:
                start_ms = max(0, int(prev.get("resume_ms") or 0))
            else:   # 이전 판의 상태(읽기 위치만 있음)
                start_ms = max(0, int(prev.get("position_ms") or 0) - 1000)
            if duration and start_ms >= duration:
                start_ms = 0
        base = int(self.config.get("base_time_ms") or prev.get("file_base_time_ms") or 0) or now_ms()
        diarized = (self.ctx.settings("stt").get("provider") == "nemotron_cpu"
                    and self.ctx.settings("stt").get("diarize_recordings", True))
        self.ctx.set_state(phase="connecting", file_path=str(path), duration_ms=duration, position_ms=start_ms,
                           resume_ms=start_ms, file_base_time_ms=base, progress=round(start_ms / duration, 4) if duration else None,
                           detail=f"파일 처리 시작: {path.name}")
        self.ctx.log(f"파일 처리 시작: {path.name}" + (f" ({start_ms // 1000}초부터)" if start_ms else ""))

        def done_upto(c: AudioChunk) -> None:
            # 전사까지 끝난 위치 = 재개 위치(구간은 순서대로 처리된다)
            self.ctx.set_state(resume_ms=(0 if diarized else start_ms) + c.end_ms_offset)

        # key 접두는 ms 단위 시작 위치 — 재개를 거듭해도 앞 실행의 key 와 겹치지 않는다
        if diarized:
            from .recording import run_diarized_recording
            # 재개 때도 처음부터 같은 목소리 추적을 복원하고 완료한 전사 구간만 건너뛴다.
            stats = await run_diarized_recording(
                self.ctx, self._pcm(str(path), 0, duration, stop), emit, base,
                stop=stop, resume_ms=start_ms, on_final_done=done_upto)
        else:
            stats = await run_audio_pipeline(
                self.ctx, self._pcm(str(path), start_ms, duration, stop), emit,
                lambda off: base + start_ms + off, key_prefix=f"f{start_ms}-", partials=False, max_pending=4,
                live=False, stop=stop, on_final_done=done_upto)
        if stop.is_set():
            return
        rc = self._proc.returncode if self._proc is not None else 0
        if rc not in (0, None):
            raise SourceError(f"파일 디코딩 실패(ffmpeg {rc}): {self._stderr[-1] if self._stderr else ''}")
        end = duration or self.ctx.state.get("position_ms")
        self.ctx.set_state(phase="done", progress=1.0, position_ms=end, resume_ms=end,
                           detail=f"파일 처리 완료(발화 {stats['finals']}건)")
        self.ctx.log(f"파일 처리 완료: 구간 {stats['chunks']}개, 발화 {stats['finals']}건")

    async def _pcm(self, path: str, start_ms: int, duration: int | None, stop: asyncio.Event) -> AsyncIterator[bytes]:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
        if start_ms:
            cmd += ["-ss", f"{start_ms / 1000:.3f}"]
        cmd += ["-i", path, "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._proc = proc
        err_task = asyncio.create_task(drain_stderr(proc, self._stderr))
        done_bytes = 0
        first = True
        try:
            while not stop.is_set():
                data = await proc.stdout.read(READ_BYTES)  # type: ignore[union-attr]
                if not data:
                    break
                if first:
                    self.ctx.set_state(phase="receiving", error=None)
                    first = False
                done_bytes += len(data)
                pos = start_ms + done_bytes // BYTES_PER_MS
                self.ctx.mark_rx(position_ms=pos, progress=round(min(1.0, pos / duration), 4) if duration else None,
                                 detail=f"처리 중 {pos // 60000}분 {pos // 1000 % 60}초"
                                        + (f" / {duration // 60000}분 {duration // 1000 % 60}초" if duration else ""))
                yield data
            if stop.is_set():
                # 일시정지·종료: ffmpeg 는 파이프가 차서 쓰기에 막혀 있으므로 기다리지 말고 바로 끝낸다
                await stop_task(err_task)
                await kill_proc(proc)
            else:
                await proc.wait()
        finally:
            if proc.returncode is None:
                await stop_task(err_task)
                await kill_proc(proc)
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(err_task), timeout=1)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    await stop_task(err_task)
                await kill_proc(proc)
