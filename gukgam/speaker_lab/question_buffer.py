"""이미 수신한 PCM을 한정된 순환 파일에 보관하고 질의 구간을 복사한다."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import struct
import threading
from typing import Callable
import uuid

from ..db import now_ms
from ..secure_files import (create_private_file, make_private_directory, positional_read,
                            positional_write, unlink_owned_file)
from .files import safe_output_path


SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
MAX_BUFFER_SECONDS = 1800
MAX_CLIP_SECONDS = 1200
MAX_EXTRACT_BYTES = 256 * 1024 * 1024
MAX_EXTRACTS = 128
DISK_RESERVE_BYTES = 64 * 1024 * 1024
_IO_BYTES = 64 * 1024
_SAMPLES_PER_MS = SAMPLE_RATE // 1000


def _time(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**53:
        raise ValueError("음성 시각은 범위 안의 정수 밀리초여야 합니다")
    return value


def _limit(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"음성 보관 한도는 1~{maximum}초의 정수여야 합니다")
    return value


def _mkdir_parents(path: Path) -> None:
    missing = []
    for parent in (path, *path.parents):
        if parent.exists():
            if not parent.is_dir():
                raise ValueError("음성 보관 경로는 디렉터리여야 합니다")
            break
        missing.append(parent)
    for parent in reversed(missing):
        make_private_directory(safe_output_path(parent))


def _disk_space(path: Path, needed: int) -> None:
    try:
        existing = next(parent for parent in (path, *path.parents) if parent.exists())
        if shutil.disk_usage(existing).free < needed + DISK_RESERVE_BYTES:
            raise ValueError("음성 보관을 위한 디스크 여유가 부족합니다")
    except OSError:
        raise ValueError("음성 보관의 디스크 여유를 확인하지 못했습니다") from None


def _header(size: int) -> bytes:
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + size, b"WAVE",
                       b"fmt ", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * SAMPLE_WIDTH,
                       SAMPLE_WIDTH, 16, b"data", size)


def _identity(path: Path) -> tuple[int, int]:
    st = path.lstat()
    return st.st_dev, st.st_ino


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    """재귀 삭제 없이 이 객체가 만든 일반 파일만 회수한다."""
    try:
        safe_output_path(path)
        unlink_owned_file(path, identity)
    except (OSError, ValueError):
        pass


class QuestionAudioBuffer:
    """16kHz 모노 PCM16의 최신 연속 구간을 디스크 순환 버퍼에 보관한다.

    임시 PCM은 파일 하나, 메모리 I/O는 최대 64KiB다. 명시 시각이 예상 샘플
    시각과 다르면 앞 구간을 폐기한다. 빠진 음성을 채우거나 재연결 구간을
    이어 붙이지 않는다. 수신 시계 자체가 방송 발언 시각을 보증하지는 않는다.
    추출 자료는 자동 삭제하지 않으며 객체당 256MiB·128건에서 보류한다.
    """

    def __init__(self, root: Path, *, max_buffer_seconds: int = MAX_BUFFER_SECONDS,
                 max_clip_seconds: int = MAX_CLIP_SECONDS,
                 clock: Callable[[], int] = now_ms):
        seconds = _limit(max_buffer_seconds, MAX_BUFFER_SECONDS)
        self._max_clip_seconds = _limit(max_clip_seconds, MAX_CLIP_SECONDS)
        self.max_bytes = seconds * SAMPLE_RATE * SAMPLE_WIDTH
        self._clock = clock
        self._lock = threading.RLock()
        self._start_sample: int | None = None
        self._total_bytes = 0
        self._export_bytes = 0
        self._exports = 0
        self._closed = False
        self._fd: int | None = None
        self._file = None
        self._file_identity: tuple[int, int] | None = None
        self._directory_identity: tuple[int, int] | None = None
        self.discontinuities = 0
        root = safe_output_path(root)
        _disk_space(root, self.max_bytes + self._max_clip_seconds * SAMPLE_RATE * SAMPLE_WIDTH + 44)
        self.path = safe_output_path(root / (".question-pcm-" + uuid.uuid4().hex))
        self._pcm_path = self.path / "buffer.pcm"
        try:
            _mkdir_parents(root)
            make_private_directory(self.path)
            self._directory_identity = _identity(self.path)
            self._file = create_private_file(self._pcm_path, read_write=True)
            self._fd = self._file.fileno()
            info = os.fstat(self._fd)
            self._file_identity = info.st_dev, info.st_ino
        except Exception:
            self.close()
            raise ValueError("질의 음성 버퍼를 준비하지 못했습니다") from None

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return min(self._total_bytes, self.max_bytes)

    def window(self) -> tuple[int, int] | None:
        """완전한 밀리초 경계 안쪽의 최신 연속 구간을 반환한다."""
        with self._lock:
            if self._closed or self._start_sample is None or not self._total_bytes:
                return None
            end = self._start_sample + self._total_bytes // SAMPLE_WIDTH
            begin = end - self.size_bytes // SAMPLE_WIDTH
            start_ms = (begin + _SAMPLES_PER_MS - 1) // _SAMPLES_PER_MS
            end_ms = end // _SAMPLES_PER_MS
            return (start_ms, end_ms) if start_ms < end_ms else None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("닫힌 질의 음성 버퍼입니다")

    def _reset(self) -> None:
        self._start_sample = None
        self._total_bytes = 0
        self.discontinuities += 1
        try:
            os.ftruncate(self._fd, 0)
        except OSError:
            self.close()
            raise ValueError("질의 음성 버퍼를 초기화하지 못했습니다") from None

    def discontinuity(self) -> None:
        """종료·재연결·누락 때 앞 구간을 추출 가능한 범위에서 제외한다."""
        with self._lock:
            if not self._closed:
                self._reset()

    def feed(self, pcm: bytes, ts_start_ms: int | None = None) -> None:
        """기존 PCM만 복사한다. 새 연결·전사·모델 실행은 만들지 않는다."""
        with self._lock:
            self._ensure_open()
            if not isinstance(pcm, bytes) or len(pcm) % SAMPLE_WIDTH:
                self._reset()
                raise ValueError("음성은 16비트 PCM의 온전한 바이트 샘플이어야 합니다")
            if not pcm:
                self._reset()
                return
            try:
                supplied = None if ts_start_ms is None else _time(ts_start_ms) * _SAMPLES_PER_MS
                expected = (None if self._start_sample is None else
                            self._start_sample + self._total_bytes // SAMPLE_WIDTH)
                if supplied is not None and expected is not None and supplied != expected:
                    self._reset()
                    expected = None
                if expected is None:
                    start = (supplied if supplied is not None else
                             _time(self._clock()) * _SAMPLES_PER_MS - len(pcm) // SAMPLE_WIDTH)
                    if start < 0:
                        raise ValueError("음성 시작 시각은 음수일 수 없습니다")
                else:
                    start = expected
                view = memoryview(pcm)
                # 한 번에 큰 입력이 와도 버릴 앞부분을 복제하거나 기록하지 않는다.
                dropped = max(0, len(view) - self.max_bytes)
                view = view[dropped:]
                if self._start_sample is None:
                    self._start_sample = start
                total_before = self._total_bytes
                new_size = min(self.max_bytes, total_before + len(pcm))
                _disk_space(self.path, max(0, new_size - os.fstat(self._fd).st_size))
                cursor = total_before + dropped
                while view:
                    position = cursor % self.max_bytes
                    count = min(len(view), _IO_BYTES, self.max_bytes - position)
                    written = positional_write(self._fd, view[:count], position)
                    if not written or written < 0 or written > count:
                        raise OSError("질의 음성 저장 실패")
                    view = view[written:]
                    cursor += written
                self._total_bytes += len(pcm)
            except Exception:
                if not self._closed:
                    self._reset()
                raise ValueError("질의 음성 입력 또는 저장 조건을 확인해야 합니다") from None

    def extract(self, start_ms: int, end_ms: int, out_dir: Path) -> dict:
        """전체 요청 구간이 남아 있을 때만 새 폴더에 독립 WAV를 만든다."""
        start_ms, end_ms = _time(start_ms), _time(end_ms)
        if not 0 < end_ms - start_ms <= self._max_clip_seconds * 1000:
            raise ValueError(f"질의 음성 구간은 0초보다 길고 {self._max_clip_seconds}초 이하여야 합니다")
        out_dir = safe_output_path(out_dir)
        if out_dir.is_relative_to(self.path):
            raise ValueError("추출 자료는 임시 순환 버퍼 밖에 보관해야 합니다")
        size = (end_ms - start_ms) * _SAMPLES_PER_MS * SAMPLE_WIDTH
        with self._lock:
            self._ensure_open()
            available = self.window()
            if available is None or start_ms < available[0] or end_ms > available[1]:
                raise ValueError("질의 음성의 전체 연속 구간이 버퍼에 없습니다")
            if self._exports >= MAX_EXTRACTS or self._export_bytes + size + 44 > MAX_EXTRACT_BYTES:
                raise ValueError("질의 음성 추출 보관 한도에 도달했습니다")
            _disk_space(out_dir, size + 44)
            target = out_dir / "audio.wav"
            file_identity = None
            directory_identity = None
            try:
                _mkdir_parents(out_dir.parent)
                make_private_directory(out_dir)
                directory_identity = _identity(out_dir)
                sha = hashlib.sha256()
                with create_private_file(target) as stream:
                    info = os.fstat(stream.fileno())
                    file_identity = info.st_dev, info.st_ino
                    def write(data: bytes) -> None:
                        view = memoryview(data)
                        while view:
                            count = stream.write(view)
                            if not count or count < 0 or count > len(view):
                                raise OSError("질의 음성 추출 실패")
                            sha.update(view[:count])
                            view = view[count:]
                    write(_header(size))
                    cursor = (start_ms * _SAMPLES_PER_MS - self._start_sample) * SAMPLE_WIDTH
                    remaining = size
                    while remaining:
                        position = cursor % self.max_bytes
                        count = min(remaining, _IO_BYTES, self.max_bytes - position)
                        chunk = positional_read(self._fd, count, position)
                        if len(chunk) != count:
                            raise OSError("보관된 질의 음성의 길이가 부족합니다")
                        write(chunk)
                        cursor += count
                        remaining -= count
                    os.fsync(stream.fileno())
                self._exports += 1
                self._export_bytes += size + 44
                return {"audio_path": str(target), "start_ms": start_ms, "end_ms": end_ms,
                        "duration_seconds": (end_ms - start_ms) / 1000,
                        "sha256": sha.hexdigest(), "sample_rate": SAMPLE_RATE,
                        "sample_count": size // SAMPLE_WIDTH, "channels": 1,
                        "timing_uncertain": True, "audio_verified": False}
            except Exception:
                if file_identity is not None:
                    _unlink_owned(target, file_identity)
                if directory_identity is not None:
                    try:
                        safe_output_path(out_dir)
                        if _identity(out_dir) == directory_identity:
                            out_dir.rmdir()
                    except (OSError, ValueError):
                        pass
                raise ValueError("질의 음성 자료를 새 파일로 보관하지 못했습니다") from None

    def close(self) -> None:
        """자신이 만든 임시 PCM만 회수한다. 추출 WAV와 다른 파일은 보존한다."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._start_sample = None
            self._total_bytes = 0
            if self._fd is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._fd = None
                self._file = None
            if self._file_identity is not None:
                _unlink_owned(self._pcm_path, self._file_identity)
            if self._directory_identity is not None:
                try:
                    safe_output_path(self.path)
                    if _identity(self.path) == self._directory_identity:
                        self.path.rmdir()
                except (OSError, ValueError):
                    pass
