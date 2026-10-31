"""의심 자막의 짧은 음성만 보관·대조한다. 자막·발언 원문과 수집 상태는 바꾸지 않는다."""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import shutil
import stat
import threading
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .. import settings
from ..db import now_ms
from ..secure_files import (create_private_file, make_private_directory, open_regular_file,
                            unlink_owned_file)

SAMPLE_RATE = 16_000
BYTES_PER_MS = 32
MAX_BUFFER_SECONDS = 600
MAX_CLIP_SECONDS = 30
MAX_QUOTA_BYTES = 64 * 1024 * 1024
MAX_PREPARED_CLIPS = 48  # 동시에 준비하는 4개 문서 × 최대 12개 짧은 구간
MIN_FREE_BYTES = 256 * 1024 * 1024
SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class AudioReviewError(ValueError):
    """음성 후보를 안전하게 만들 수 없는 입력·보관 조건."""


@dataclass(frozen=True)
class _Piece:
    start: int
    pcm: bytes

    @property
    def end(self) -> int:
        return self.start + len(self.pcm) // 2


class PcmRingBuffer:
    """16kHz 모노 PCM16. 시각은 epoch ms이고 내부 좌표는 1/16ms 샘플이다.

    한 조각의 누락·중복·시각 역행은 불연속으로 취급한다. 따라서 추출에는 공백을
    건너뛴 다른 음성이 섞이지 않는다. HLS의 수신 시각은 실제 발언 시각을 보장하지 않는다.
    """

    def __init__(self, seconds: int = MAX_BUFFER_SECONDS, *, clock: Callable[[], int] = now_ms):
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= MAX_BUFFER_SECONDS:
            raise AudioReviewError("순환 버퍼는 1~600초로 설정합니다")
        self.max_bytes = seconds * SAMPLE_RATE * 2
        self._pieces: deque[_Piece] = deque()
        self._bytes = 0
        self._clock = clock
        self._lock = threading.RLock()
        self.discontinuities = 0

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def window(self) -> tuple[float, float] | None:
        with self._lock:
            if not self._pieces:
                return None
            return self._pieces[0].start / 16, self._pieces[-1].end / 16

    def clear(self, *, discontinuity: bool = False) -> None:
        with self._lock:
            self._pieces.clear()
            self._bytes = 0
            if discontinuity:
                self.discontinuities += 1

    def feed(self, pcm: bytes, ts_start_ms: int | None = None) -> None:
        if not isinstance(pcm, (bytes, bytearray, memoryview)) or len(pcm) % 2:
            raise AudioReviewError("음성은 16비트 PCM의 온전한 샘플이어야 합니다")
        if not pcm:
            self.clear(discontinuity=True)
            return
        if ts_start_ms is not None and (isinstance(ts_start_ms, bool) or not isinstance(ts_start_ms, int)):
            raise AudioReviewError("음성 시작 시각은 정수 ms여야 합니다")
        with self._lock:
            if ts_start_ms is None:
                start = self._pieces[-1].end if self._pieces else self._clock() * 16 - len(pcm) // 2
            else:
                start = ts_start_ms * 16
            if self._pieces and start != self._pieces[-1].end:
                self.clear(discontinuity=True)
            # 아주 큰 입력도 전체를 복제한 뒤 버리지 않는다.
            if len(pcm) > self.max_bytes:
                start += (len(pcm) - self.max_bytes) // 2
                pcm = pcm[-self.max_bytes:]
            # 매우 작은 입력이 반복돼도 조각 객체가 수백만 개 쌓이지 않도록 1초씩 합친다.
            remaining = memoryview(pcm)
            block_bytes = SAMPLE_RATE * 2
            if self._pieces and len(self._pieces[-1].pcm) < block_bytes:
                last = self._pieces.pop()
                take = min(block_bytes - len(last.pcm), len(remaining))
                self._pieces.append(_Piece(last.start, last.pcm + bytes(remaining[:take])))
                remaining = remaining[take:]
                start += take // 2
            while remaining:
                take = min(block_bytes, len(remaining))
                self._pieces.append(_Piece(start, bytes(remaining[:take])))
                remaining = remaining[take:]
                start += take // 2
            self._bytes += len(pcm)
            while self._bytes > self.max_bytes:
                piece = self._pieces.popleft()
                excess = self._bytes - self.max_bytes
                if len(piece.pcm) > excess:
                    self._pieces.appendleft(_Piece(piece.start + excess // 2, piece.pcm[excess:]))
                    self._bytes -= excess
                else:
                    self._bytes -= len(piece.pcm)

    def extract(self, start_ms: int, end_ms: int) -> bytes | None:
        """전 구간이 버퍼 안에 있을 때만 원래 PCM을 반환한다. 일부만 겹치면 보류한다."""
        _check_range(start_ms, end_ms)
        start, end = start_ms * 16, end_ms * 16
        with self._lock:
            if not self._pieces or start < self._pieces[0].start or end > self._pieces[-1].end:
                return None
            out = []
            for piece in self._pieces:
                lo, hi = max(start, piece.start), min(end, piece.end)
                if lo < hi:
                    out.append(piece.pcm[(lo - piece.start) * 2:(hi - piece.start) * 2])
            result = b"".join(out)
            return result if len(result) == (end - start) * 2 else None


def _check_range(start_ms: int, end_ms: int, max_seconds: int = MAX_CLIP_SECONDS) -> None:
    if any(isinstance(x, bool) or not isinstance(x, int) for x in (start_ms, end_ms)):
        raise AudioReviewError("음성 구간 시각은 정수 ms여야 합니다")
    if start_ms < 0 or not 0 < end_ms - start_ms <= min(max_seconds, MAX_CLIP_SECONDS) * 1000:
        raise AudioReviewError("음성 대조 구간은 0초보다 길고 30초 이하여야 합니다")


def _session_id(sid: str) -> str:
    if not isinstance(sid, str) or not SID_RE.fullmatch(sid):
        raise AudioReviewError("잘못된 세션 식별자입니다")
    return sid


def _review_dir(base: Path, sid: str) -> Path:
    """기존 디렉터리의 링크를 따라가지 않고 전용 하위 폴더만 만든다."""
    _session_id(sid)
    base = base.expanduser().absolute()
    for path in (*reversed(base.parents), base):
        if path.is_symlink():
            raise AudioReviewError("음성 보관 경로에 심볼릭 링크를 사용할 수 없습니다")
        if path.exists() and not path.is_dir():
            raise AudioReviewError("음성 보관 경로가 디렉터리가 아닙니다")
    make_private_directory(base, parents=True, exist_ok=True)
    current = base
    for name in (sid, "review"):
        current = current / name
        if current.is_symlink():
            raise AudioReviewError("음성 보관 경로에 심볼릭 링크를 사용할 수 없습니다")
        make_private_directory(current, exist_ok=True)
        if not current.is_dir():
            raise AudioReviewError("음성 보관 경로가 디렉터리가 아닙니다")
    return current


def save_clip(base: Path, sid: str, start_ms: int, end_ms: int, pcm: bytes,
              *, quota_bytes: int = MAX_QUOTA_BYTES) -> Path:
    """요청 구간 하나만 새 WAV로 기록한다. 기존 파일은 지우거나 덮어쓰지 않는다."""
    _check_range(start_ms, end_ms)
    if len(pcm) != (end_ms - start_ms) * BYTES_PER_MS:
        raise AudioReviewError("음성 길이와 요청 구간이 일치하지 않습니다")
    if not 0 < quota_bytes <= MAX_QUOTA_BYTES:
        raise AudioReviewError("음성 보관 한도는 세션당 최대 64MiB입니다")
    directory = _review_dir(Path(base), sid)
    occupied = 0
    for child in directory.iterdir():
        st = child.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise AudioReviewError("음성 보관 폴더에 안전하지 않은 항목이 있습니다")
        occupied += st.st_size
    expected = len(pcm) + 44
    if occupied + expected > quota_bytes:
        raise AudioReviewError("세션의 음성 후보 보관 한도에 도달했습니다")
    if shutil.disk_usage(directory).free < MIN_FREE_BYTES + expected:
        raise AudioReviewError("디스크 여유가 부족하여 음성 후보 보관을 보류합니다")
    # 스트림은 검사한 부모 핸들을 보유한다. 새 파일만 비공개 권한으로 만든다.
    name = f"review-{start_ms}-{end_ms}-{uuid.uuid4().hex[:12]}.wav"
    identity = None
    try:
        with create_private_file(directory / name) as file:
            info = os.fstat(file.fileno())
            identity = info.st_dev, info.st_ino
            with wave.open(file, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(SAMPLE_RATE)
                audio.writeframes(pcm)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        if identity is not None:
            unlink_owned_file(directory / name, identity)
        raise
    return directory / name


def _safe_hls_url(url: str) -> str:
    if not isinstance(url, str) or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise AudioReviewError("잘못된 중계 주소입니다")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise AudioReviewError("잘못된 중계 주소입니다") from exc
    host = (parts.hostname or "").lower()
    valid_host = bool(re.fullmatch(r"(?:[a-z0-9-]+-)?assembly\.hscdn\.com", host))
    if (parts.scheme != "https" or not valid_host or parts.username or parts.password or parts.query
            or parts.fragment or port not in (None, 443) or not parts.path.startswith("/assembly/")
            or not parts.path.endswith(".m3u8")):
        raise AudioReviewError("국회 공식 HTTPS 중계 주소만 음성 대조에 사용할 수 있습니다")
    return url


class _ResolveContext:
    """기존 회의 코드 조회를 재사용하되 원본 세션 설정·상태에는 쓰지 않는다."""

    def __init__(self, sid: str):
        self.sid = sid
        self.repo = self

    def update_session(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def log(self, *_args: Any, **_kwargs: Any) -> None:
        pass


@dataclass
class _Capture:
    ring: PcmRingBuffer
    config: dict[str, Any]
    options: dict[str, Any]
    halt: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    proc: Any = None
    phase: str = "connecting"
    detail: str = "짧은 음성 대조 버퍼 연결 중"
    received_bytes: int = 0


class AudioReviewService:
    """명시적으로 켠 세션 하나의 PCM만 수신하고 짧은 후보 전사를 직렬 처리한다.

    start/stop은 비동기이며 status/feed/discontinuity는 동기다. verify의 반환값은
    후보일 뿐이며 Repo의 발언 본문·화자·source_state를 수정하지 않는다.
    """

    def __init__(self, rt: Any):
        self.rt = rt
        from ..speaker_lab.recording_service import RecordingService

        self.recordings = RecordingService(rt, self)
        self._captures: dict[str, _Capture] = {}
        self._diagnostics: dict[str, dict[str, Any]] = {}
        self._verify_lock = asyncio.Lock()
        self._pending = 0
        self.max_pending = 2
        self.read_timeout = 20.0
        self.verify_timeout = 240.0
        self.retry_interval = 5.0
        self.max_attempts = 3
        # 원음은 파일에만 보존한다. 큐 대기 중 PCM 사본을 메모리에 쌓지 않는다.
        self._stage_clips: dict[tuple[str, int, int], dict[str, Any]] = {}

    def _options(self) -> dict[str, Any]:
        cfg = settings.get(self.rt.repo, "audio_review")
        return dict(cfg) if isinstance(cfg, dict) else {}

    def status(self, sid: str) -> dict[str, Any]:
        capture = self._captures.get(sid)
        if capture is None:
            return dict(self._diagnostics.get(sid) or {"phase": "disabled", "detail": "음성 대조가 꺼져 있습니다"})
        return {"phase": capture.phase, "detail": capture.detail, "buffer_bytes": capture.ring.size_bytes,
                "window_ms": capture.ring.window(), "discontinuities": capture.ring.discontinuities,
                "received_bytes": capture.received_bytes, "pending": self._pending, "timing_uncertain": True}

    def _held(self, sid: str, detail: str) -> dict[str, Any]:
        return {"status": "held", "session_id": sid, "detail": detail,
                "timing_uncertain": True, "automatic_body_correction": False}

    async def start(self, sid: str, source_config: dict[str, Any]) -> bool:
        _session_id(sid)
        options = self._options()
        enabled = options.get("enabled") is True or source_config.get("audio_review_enabled") is True
        if not enabled:
            await self.stop(sid)
            self._diagnostics[sid] = {"phase": "disabled", "detail": "음성 대조가 꺼져 있습니다"}
            return False
        if not self.rt.config.allow_assembly_network:
            self._diagnostics[sid] = {"phase": "held", "detail": "국회 외부망 호출이 꺼져 있습니다"}
            return False
        if sid in self._captures:
            return True
        if self._captures:
            self._diagnostics[sid] = {"phase": "held", "detail": "다른 세션의 음성 대조 버퍼가 사용 중입니다"}
            return False
        if not shutil.which("ffmpeg"):
            self._diagnostics[sid] = {"phase": "held", "detail": "ffmpeg를 찾을 수 없습니다"}
            return False
        seconds = options.get("buffer_seconds", MAX_BUFFER_SECONDS)
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            seconds = MAX_BUFFER_SECONDS
        seconds = max(1, min(seconds, MAX_BUFFER_SECONDS))
        capture = _Capture(PcmRingBuffer(seconds), dict(source_config), options)
        self._captures[sid] = capture
        capture.task = asyncio.create_task(self._reader(sid, capture), name=f"audio-review-{sid}")
        return True

    async def stop(self, sid: str) -> None:
        self.recordings.finish_owned(sid, "collection_stopped")
        stages = getattr(self.rt, "stages", None)
        if stages is not None:
            stages.disable_audio(sid)
        questions = getattr(self.rt, "question_audio", None)
        if questions is not None:
            questions.disable(sid)
        capture = self._captures.pop(sid, None)
        if capture is None:
            return
        capture.halt.set()
        if capture.task is not None:
            capture.task.cancel()
            await asyncio.gather(capture.task, return_exceptions=True)
        capture.ring.clear()
        self._diagnostics[sid] = {"phase": "stopped", "detail": "음성 대조 버퍼를 비웠습니다"}

    async def close(self) -> None:
        await self.recordings.close()
        for sid in list(self._captures):
            await self.stop(sid)

    def feed(self, sid: str, pcm: bytes, ts_start_ms: int | None = None) -> None:
        capture = self._captures.get(sid)
        if capture is not None:
            previous_discontinuities = capture.ring.discontinuities
            capture.ring.feed(pcm, ts_start_ms)
            capture.received_bytes += len(pcm)
            self.recordings.feed(sid, pcm, ts_start_ms)
            questions = getattr(self.rt, "question_audio", None)
            if questions is not None:
                if capture.ring.discontinuities != previous_discontinuities:
                    questions.discontinuity(sid)
                window = capture.ring.window()
                if window:
                    # 같은 수신 샘플의 시작을 공유한다. 자막 시각과 일치한다고 가정하지 않는다.
                    questions.feed(sid, pcm, ts_start_ms=window[1] - len(pcm) / 32)

    def discontinuity(self, sid: str) -> None:
        self.recordings.discontinuity(sid)
        questions = getattr(self.rt, "question_audio", None)
        if questions is not None:
            questions.discontinuity(sid)
        capture = self._captures.get(sid)
        if capture is not None:
            capture.ring.clear(discontinuity=True)

    async def _reader(self, sid: str, capture: _Capture) -> None:
        from .assembly import AssemblyClient, hls_urls, resolve_meeting_codes
        from .base import sleep_or_stop
        from .hls import READ_BYTES, drain_stderr, ffmpeg_hls_cmd, kill_proc, stop_task

        client = AssemblyClient(min_interval=1.5)
        cfg = capture.config
        tail: deque[str] = deque(maxlen=2)
        try:
            xcode, xcgcd = str(cfg.get("xcode") or ""), str(cfg.get("xcgcd") or "")
            if not cfg.get("hls_url") and not xcgcd:
                xcode, xcgcd = await resolve_meeting_codes(_ResolveContext(sid), client, cfg)
            if not cfg.get("hls_url") and not (xcode and xcgcd):
                raise AudioReviewError("회의 코드가 없어 음성 대조를 시작할 수 없습니다")
            for attempt in range(self.max_attempts):
                if capture.halt.is_set():
                    return
                proc = None
                err_task = None
                try:
                    if cfg.get("hls_url"):
                        url = _safe_hls_url(str(cfg["hls_url"]))
                    else:
                        info = await client.live_play(xcode, xcgcd, fresh=True)
                        urls = hls_urls(info)
                        url = _safe_hls_url(str(urls.get("240p") or urls.get("default") or ""))
                    cmd = ffmpeg_hls_cmd(url)
                    i = cmd.index("-i")
                    cmd[i:i] = ["-protocol_whitelist", "http,https,tls,tcp,crypto"]
                    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                              stdout=asyncio.subprocess.PIPE,
                                                              stderr=asyncio.subprocess.PIPE)
                    capture.proc = proc
                    err_task = asyncio.create_task(drain_stderr(proc, tail))
                    self.discontinuity(sid)
                    pending_byte = b""
                    # 첫 수신 시각만 대응시킨다. 이후에는 샘플 수로 전진시켜 네트워크 지터를 제거한다.
                    while not capture.halt.is_set():
                        data = await asyncio.wait_for(proc.stdout.read(READ_BYTES), self.read_timeout)
                        if not data:
                            break
                        data = pending_byte + data
                        usable = len(data) // 2 * 2
                        pending_byte = data[usable:]
                        if usable:
                            self.feed(sid, data[:usable])
                        capture.phase, capture.detail = "receiving", "최근 음성만 메모리에 보관 중(시각 대조 필요)"
                    if capture.halt.is_set():
                        return
                except AudioReviewError:
                    raise
                except Exception:
                    # ffmpeg stderr나 URL은 상태·로그에 출력하지 않는다.
                    capture.phase, capture.detail = "reconnecting", "음성 연결이 끊겨 제한된 재시도 중입니다"
                finally:
                    await stop_task(err_task)
                    await kill_proc(proc)
                    capture.proc = None
                self.discontinuity(sid)
                capture.phase, capture.detail = "reconnecting", "음성 연결이 끊겨 제한된 재시도 중입니다"
                if attempt + 1 < self.max_attempts:
                    if await sleep_or_stop(capture.halt, max(1.5, self.retry_interval) * (attempt + 1)):
                        return
            capture.phase, capture.detail = "held", "음성 대조 연결 재시도 한도에 도달했습니다"
        except asyncio.CancelledError:
            raise
        except Exception:
            self.discontinuity(sid)
            capture.phase, capture.detail = "held", "음성 대조 주소·연결을 확인해야 합니다"
        finally:
            await client.aclose()

    def _stt_ready(self) -> bool:
        if self.rt.config.disable_heavy_engines:
            return False
        cfg = settings.get(self.rt.repo, "stt") or {}
        if cfg.get("provider") != "mlx_whisper" or not getattr(self.rt, "stt", None):
            return False
        try:
            from ..stt.service import resolve_model

            model, local = resolve_model(cfg.get("model") or "")
            if not local:
                from huggingface_hub import snapshot_download

                model = snapshot_download(repo_id=model, local_files_only=True)
            path = Path(model)
            return path.is_dir() and (path / "config.json").is_file() and (path / "weights.safetensors").is_file()
        except Exception:
            return False

    async def retain(self, sid: str, start_ms: int, end_ms: int, reason: str,
                     utterance_id: int | None = None) -> dict[str, Any]:
        """STT를 실행하지 않고 의심 구간의 원음만 남긴다. 상시 파일 저장은 하지 않는다."""
        _session_id(sid)
        capture = self._captures.get(sid)
        if capture is None:
            return self._held(sid, "최근 음성 버퍼가 없습니다")
        options = self._options()
        limit = options.get("max_clip_seconds", MAX_CLIP_SECONDS)
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else MAX_CLIP_SECONDS
        _check_range(start_ms, end_ms, max(1, limit))
        pcm = capture.ring.extract(start_ms, end_ms)
        if pcm is None:
            return self._held(sid, "요청 음성 전 구간이 버퍼에 없습니다(시각·끊김·보존 범위 확인)")
        if self._pending >= self.max_pending:
            return self._held(sid, "음성 대조 대기 한도에 도달했습니다")
        self._pending += 1
        try:
            async with self._verify_lock:
                if capture.halt.is_set() or self._captures.get(sid) is not capture:
                    return self._held(sid, "음성 대조 수집이 중지되었습니다")
                quota_mb = options.get("quota_mb", 64)
                quota_mb = quota_mb if isinstance(quota_mb, int) and not isinstance(quota_mb, bool) else 64
                try:
                    path = save_clip(self.rt.config.audio_dir, sid, start_ms, end_ms, pcm,
                                     quota_bytes=max(1, min(quota_mb, 64)) * 1024 * 1024)
                except (AudioReviewError, OSError):
                    return self._held(sid, "음성 보관 경로·한도·디스크 여유를 확인해야 합니다")
                return {"status": "captured", "session_id": sid, "utterance_id": utterance_id,
                        "start_ms": start_ms, "end_ms": end_ms, "reason": str(reason)[:1000],
                        "clip_path": str(path), "timing_uncertain": True, "automatic_body_correction": False,
                        "detail": "의심 구간 원음만 보관했습니다. 자막과 시각을 대조한 뒤 검수하세요"}
        finally:
            self._pending -= 1

    async def verify(self, sid: str, start_ms: int, end_ms: int, reason: str,
                     utterance_id: int | None = None) -> dict[str, Any]:
        """30초 이하의 음성만 기존 로컬 STT로 대조하고 별도 AI 후보를 반환한다."""
        _session_id(sid)
        capture = self._captures.get(sid)
        if capture is None:
            return self._held(sid, "최근 음성 버퍼가 없습니다")
        options = self._options()
        limit = options.get("max_clip_seconds", MAX_CLIP_SECONDS)
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else MAX_CLIP_SECONDS
        _check_range(start_ms, end_ms, max(1, limit))
        if options.get("verify_transcription") is not True:
            return self._held(sid, "의심 구간 음성 전사 대조가 꺼져 있습니다")
        if not self._stt_ready():
            return self._held(sid, "설치된 로컬 음성인식 모델을 사용할 수 없습니다(다운로드하지 않음)")
        if self._pending >= self.max_pending:
            return self._held(sid, "음성 대조 대기 한도에 도달했습니다")
        pcm = capture.ring.extract(start_ms, end_ms)
        if pcm is None:
            return self._held(sid, "요청 음성 전 구간이 버퍼에 없습니다(시각·끊김·보존 범위 확인)")
        self._pending += 1
        try:
            async with self._verify_lock:
                if capture.halt.is_set() or self._captures.get(sid) is not capture:
                    return self._held(sid, "음성 대조 수집이 중지되었습니다")
                # 대기 중 설정을 끈 경우 모델을 실행하지 않는다.
                current_options = self._options()
                if current_options.get("verify_transcription") is not True or not self._stt_ready():
                    return self._held(sid, "음성 전사 대조 설정·로컬 모델 조건이 변경되었습니다")
                quota_mb = current_options.get("quota_mb", 64)
                quota_mb = quota_mb if isinstance(quota_mb, int) and not isinstance(quota_mb, bool) else 64
                try:
                    path = save_clip(self.rt.config.audio_dir, sid, start_ms, end_ms, pcm,
                                     quota_bytes=max(1, min(quota_mb, 64)) * 1024 * 1024)
                except (AudioReviewError, OSError):
                    return self._held(sid, "음성 보관 경로·한도·디스크 여유를 확인해야 합니다")
                try:
                    segments = await asyncio.wait_for(self.rt.stt.transcribe(pcm, sample_rate=SAMPLE_RATE, prompt=""),
                                                      self.verify_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    result = self._held(sid, "짧은 음성 전사에 실패했습니다(원음 후보만 보관)")
                    result["clip_path"] = str(path)
                    return result
                if capture.halt.is_set() or self._captures.get(sid) is not capture:
                    result = self._held(sid, "음성 대조 수집이 중지되었습니다(원음 후보만 보관)")
                    result["clip_path"] = str(path)
                    return result
                clean = []
                for segment in segments:
                    if not isinstance(segment, dict):
                        continue
                    text = str(segment.get("text") or "").strip()
                    try:
                        start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
                    except (TypeError, ValueError):
                        continue
                    if text and math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= len(pcm) / 32000 + .1:
                        clean.append({"start": start, "end": end, "text": text})
                return {"status": "candidate" if clean else "held", "session_id": sid,
                        "utterance_id": utterance_id, "start_ms": start_ms, "end_ms": end_ms,
                        "reason": str(reason)[:1000], "transcript": " ".join(s["text"] for s in clean),
                        "segments": clean, "clip_path": str(path), "timing_uncertain": True,
                        "automatic_body_correction": False, "review_status": "AI 초안",
                        "detail": "수신 시각 기준 음성 후보입니다. 원음·자막 시각을 대조한 뒤 검수하세요"}
        finally:
            self._pending -= 1

    @staticmethod
    def _stage_held(code: str, detail: str) -> dict[str, Any]:
        return {"status": "held", "reason_code": code, "detail": detail, "audio_verified": False}

    @staticmethod
    def _stage_permitted(allowed: Callable[[], bool]) -> bool:
        try:
            return allowed() is True
        except Exception:
            return False

    def release_stage_clips(self, sid: str, ranges: list[tuple[int, int]],
                            *, reservation_id: str | None = None) -> None:
        """종료·취소된 문서의 준비 메타데이터를 회수 가능하게 한다. 파일은 보존한다."""
        for start, end in ranges:
            item = self._stage_clips.get((sid, start, end))
            if item is not None:
                if reservation_id is not None:
                    item["reservations"].discard(reservation_id)
                item["consumed"] = True

    async def prepare_stage_clip(self, sid: str, start_ms: int, end_ms: int, reason: str,
                                 *, allowed: Callable[[], bool], reservation_id: str | None = None) -> dict[str, Any]:
        """이미 받은 PCM을 모델 대기 전에 짧은 WAV로 확보한다. 전사·새 연결은 없다."""
        held = self._stage_held
        _session_id(sid)
        try:
            _check_range(start_ms, end_ms)
        except AudioReviewError:
            return held("invalid_range", "음성 확인 구간은 30초 이내여야 합니다")
        if not self._stage_permitted(allowed):
            return held("not_authorized", "현재 회의의 음성 확인 허용 상태가 변경되었습니다")
        capture = self._captures.get(sid)
        if capture is None or capture.halt.is_set():
            return held("audio_unavailable", "현재 회의의 수신 음성이 없습니다")
        key = (sid, start_ms, end_ms)

        def prepared() -> dict[str, Any] | None:
            item = self._stage_clips.get(key)
            if item is None:
                return None
            if item["capture_id"] != id(capture):
                return held("prepared_invalid", "준비한 원음의 수신 연결이 현재 연결과 다릅니다")
            item["consumed"] = False
            if reservation_id is not None:
                item["reservations"].add(reservation_id)
            return {"status": "prepared", "start_ms": start_ms, "end_ms": end_ms,
                    "timing_uncertain": True, "audio_verified": False}

        cached = prepared()
        if cached is not None:
            return cached
        discontinuities = capture.ring.discontinuities
        # 후속 여유만 최대 15초 기다린다. 이미 사라진 과거 음성은 복원하지 않는다.
        for _ in range(30):
            window = capture.ring.window()
            if window and (window[1] >= end_ms or window[0] > start_ms):
                break
            if not self._stage_permitted(allowed) or capture.halt.is_set() or self._captures.get(sid) is not capture:
                return held("cancelled", "음성 후보 준비가 중단되었습니다")
            await asyncio.sleep(.5)
        if not self._stage_permitted(allowed) or capture.halt.is_set() or self._captures.get(sid) is not capture:
            return held("cancelled", "음성 후보 준비 중 허용 상태 또는 원문 판본이 변경되었습니다")
        # 같은 범위가 동시에 요청되었어도 먼저 저장한 파일 하나를 재사용한다.
        cached = prepared()
        if cached is not None:
            return cached
        if capture.ring.discontinuities != discontinuities:
            return held("audio_discontinuity", "확인 구간에 음성 연결 단절이 있습니다")
        pcm = capture.ring.extract(start_ms, end_ms)
        if pcm is None:
            return held("audio_range_missing", "요청 구간의 연속 음성이 버퍼에 모두 남아 있지 않습니다")
        needed = len(pcm)
        while (len(self._stage_clips) >= MAX_PREPARED_CLIPS
               or sum(item["bytes"] for item in self._stage_clips.values()) + needed > MAX_QUOTA_BYTES):
            recyclable = next((old for old, item in self._stage_clips.items() if item["consumed"] and not item.get("users", 0)
                                and not item["reservations"]), None)
            if recyclable is None:
                return held("prepared_capacity", "먼저 확보한 음성 후보의 대기 한도에 도달했습니다")
            self._stage_clips.pop(recyclable)
        try:
            quota = self._options().get("quota_mb", 64)
            quota = min(max(quota, 1), 64) if type(quota) is int else 64
            path = save_clip(self.rt.config.audio_dir, sid, start_ms, end_ms, pcm,
                             quota_bytes=quota * 1024 * 1024)
        except AudioReviewError as exc:
            if "디스크 여유" in str(exc):
                return held("disk_full", "원음 보관에 필요한 디스크 여유가 부족합니다")
            if "한도" in str(exc):
                return held("storage_quota", "현재 세션의 짧은 원음 보관 한도에 도달했습니다")
            return held("storage_path", "원음 보관 경로를 확인해야 합니다")
        except OSError:
            return held("storage_io", "원음 파일 저장에 실패했습니다")
        self._stage_clips[key] = {"path": path, "sha256": hashlib.sha256(pcm).hexdigest(),
                                  "bytes": needed, "capture_id": id(capture), "consumed": False, "reservations": set()}
        if not self._stage_permitted(allowed):
            self._stage_clips[key]["consumed"] = True
            return held("cancelled", "원음 확보 중 허용 상태 또는 원문 판본이 변경되었습니다")
        return prepared()

    def _read_stage_clip(self, sid: str, start_ms: int, end_ms: int) -> bytes:
        """준비 파일의 형식·크기·내용 해시를 다시 확인한다. 전체 원음은 상주시키지 않는다."""
        item = self._stage_clips.get((sid, start_ms, end_ms))
        if item is None:
            raise AudioReviewError("준비한 음성 후보가 없습니다")
        directory = _review_dir(self.rt.config.audio_dir, sid)
        path = item["path"]
        if path.parent != directory:
            raise AudioReviewError("준비한 음성 후보 경로가 다릅니다")
        with open_regular_file(path) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_size > item["bytes"] + 4096:
                raise AudioReviewError("준비한 음성 후보 파일 형식이 다릅니다")
            with wave.open(stream, "rb") as wav:
                frames = (end_ms - start_ms) * 16
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) != (SAMPLE_RATE, 1, 2, frames):
                    raise AudioReviewError("준비한 음성 후보의 길이·형식이 다릅니다")
                pcm = wav.readframes(frames)
        if len(pcm) != item["bytes"] or hashlib.sha256(pcm).hexdigest() != item["sha256"]:
            raise AudioReviewError("준비한 음성 후보의 내용이 달라졌습니다")
        return pcm

    async def verify_stage_clip(self, sid: str, start_ms: int, end_ms: int, reason: str,
                                *, allowed: Callable[[], bool]) -> dict[str, Any]:
        """확보한 짧은 원음만 전사한다. 준비 이후 순환 버퍼가 만료돼도 파일을 재사용한다."""
        held = self._stage_held
        _session_id(sid)
        try:
            _check_range(start_ms, end_ms)
        except AudioReviewError:
            return held("invalid_range", "음성 확인 구간은 30초 이내여야 합니다")
        if not self._stage_permitted(allowed):
            return held("not_authorized", "현재 회의의 음성 확인 허용 상태가 변경되었습니다")
        capture = self._captures.get(sid)
        if capture is None or capture.halt.is_set():
            return held("audio_unavailable", "현재 회의의 수신 음성이 없습니다")
        if not self._stt_ready():
            return held("stt_unavailable", "설치된 로컬 전사 모델을 사용할 수 없습니다")
        if self._pending >= self.max_pending:
            return held("queue_full", "음성 확인 대기 한도에 도달했습니다")
        prepared = await self.prepare_stage_clip(sid, start_ms, end_ms, reason, allowed=allowed)
        if prepared["status"] != "prepared":
            return prepared
        if self._pending >= self.max_pending:
            return held("queue_full", "음성 확인 대기 한도에 도달했습니다")
        self._pending += 1
        key = (sid, start_ms, end_ms)
        self._stage_clips[key]["users"] = self._stage_clips[key].get("users", 0) + 1
        try:
            async with self._verify_lock:
                if not self._stage_permitted(allowed) or capture.halt.is_set() or self._captures.get(sid) is not capture:
                    return held("cancelled", "대기 중 음성 확인이 중단되었습니다")
                try:
                    pcm = self._read_stage_clip(sid, start_ms, end_ms)
                except (ValueError, OSError, EOFError, wave.Error):
                    return held("prepared_invalid", "준비한 원음의 파일·내용 무결성을 확인하지 못했습니다")
                try:
                    segments = await asyncio.wait_for(self.rt.stt.transcribe(
                        pcm, sample_rate=SAMPLE_RATE, prompt="", apply_text_corrections=False,
                        request_guard=allowed), self.verify_timeout)
                except asyncio.TimeoutError:
                    return held("stt_timeout", "원음은 보관했지만 전사 시간 한도를 넘었습니다")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return held("stt_failed", "원음은 보관했지만 음성 전사에 실패했습니다")
                if not self._stage_permitted(allowed) or capture.halt.is_set() or self._captures.get(sid) is not capture:
                    return held("cancelled", "전사 중 허용 상태 또는 원문 판본이 변경되었습니다")
                if not isinstance(segments, list):
                    return held("stt_failed", "원음은 보관했지만 음성 전사 결과 형식이 잘못되었습니다")
                clean = []
                for segment in segments:
                    if not isinstance(segment, dict) or not isinstance(segment.get("text"), str) or not segment["text"].strip():
                        continue
                    try:
                        start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= (end_ms - start_ms) / 1000 + .1:
                        clean.append({"start": start, "end": end, "text": segment["text"]})
                return {"status": "candidate" if clean else "held", "reason_code": "asr_candidate" if clean else "empty_transcription",
                        "transcript": " ".join(row["text"] for row in clean), "segments": clean,
                        "start_ms": start_ms, "end_ms": end_ms, "clip_path": str(self._stage_clips[key]["path"]),
                        "timing_uncertain": True, "audio_verified": False}
        finally:
            self._pending -= 1
            item = self._stage_clips.get(key)
            if item is not None:
                item["users"] = max(0, item.get("users", 1) - 1)
                item["consumed"] = True
