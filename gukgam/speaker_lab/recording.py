"""기존 PCM 수신을 복사해 한정된 연속 음성을 보관한다. 새 연결은 만들지 않는다."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
from typing import BinaryIO, Callable
import uuid

from ..db import now_ms
from ..secure_files import atomic_replace, create_private_file, make_private_directory
from .files import safe_output_path


SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
DISK_RESERVE_BYTES = 64 * 1024 * 1024
_WRITE_CHUNK_BYTES = 64 * 1024
_SID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_REASONS = frozenset({
    "operator_stop", "session_stopped", "source_stopped", "source_ended",
    "server_shutdown", "audio_discontinuity", "timestamp_discontinuity",
    "duration_reached", "source_error", "cancelled", "interrupted",
    "audio_timeout", "wall_time_limit", "service_stopped", "collection_stopped",
})


def _timestamp(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**53:
        raise ValueError("수신 시각은 범위 안의 정수 밀리초여야 합니다")
    return value


def _header(data_bytes: int) -> bytes:
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes,
                       b"WAVE", b"fmt ", 16, 1, 1, SAMPLE_RATE,
                       SAMPLE_RATE * SAMPLE_WIDTH, SAMPLE_WIDTH, 16,
                       b"data", data_bytes)


def _mkdir(path: Path) -> None:
    """새로 생기는 상위 작업 폴더도 비공개 권한으로 만든다."""
    missing = []
    for parent in (path, *path.parents):
        if parent.exists():
            if not parent.is_dir():
                raise ValueError("녹음 작업 경로는 디렉터리여야 합니다")
            break
        missing.append(parent)
    for parent in reversed(missing):
        make_private_directory(safe_output_path(parent))


class ContinuousRecording:
    """16kHz 모노 PCM16을 최대 15분 저장하며 끊긴 자료를 완료로 표시하지 않는다.

    한 수신 루프에서 순차 호출한다. ``None`` 시각은 최초 실제 수신 시각을
    기준으로 샘플 수만큼 진행하며, 수신 지연을 실제 방송 시각으로 보증하지 않는다.
    종료 뒤 호출은 무시해 같은 녹음이 재개되거나 덮어써지지 않게 한다.
    """

    def __init__(self, root: Path, session_id: str, duration_seconds: int = 900,
                 *, clock_ms: Callable[[], int] = now_ms):
        if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int)
                or not 600 <= duration_seconds <= 900):
            raise ValueError("연속 녹음은 정수 600~900초만 허용합니다")
        if not isinstance(session_id, str) or not _SID.fullmatch(session_id):
            raise ValueError("회의 식별자 형식이 잘못됐습니다")
        root = safe_output_path(root)
        existing = next(parent for parent in (root, *root.parents) if parent.exists())
        needed = duration_seconds * SAMPLE_RATE * SAMPLE_WIDTH + 44 + DISK_RESERVE_BYTES
        try:
            if shutil.disk_usage(existing).free < needed:
                raise ValueError("연속 녹음을 위한 디스크 여유가 부족합니다")
        except OSError:
            raise ValueError("연속 녹음의 디스크 여유를 확인하지 못했습니다") from None
        self._clock_ms = clock_ms
        self._created_at_ms = _timestamp(clock_ms())
        self._sid = session_id
        self._requested = duration_seconds
        self._limit_bytes = duration_seconds * SAMPLE_RATE * SAMPLE_WIDTH
        self._data_bytes = 0
        self._first_received_at_ms: int | None = None
        self._start_ms: int | None = None
        self._finished_at_ms: int | None = None
        self._timestamp_source: str | None = None
        self._state = "recording"
        self._reason: str | None = None
        self._sha256: str | None = None
        self._manifest_saved = False
        self._last_manifest_bytes = 0
        self._manifest_received_saved = False
        self._file: BinaryIO | None = None
        self.path = safe_output_path(root / uuid.uuid4().hex)
        try:
            _mkdir(root)
            make_private_directory(self.path)
            self._file = create_private_file(self.path / "audio.wav", read_write=True)
            self._write_all(_header(0))
            self._save_manifest()
        except Exception:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise ValueError("연속 녹음 파일을 준비하지 못했습니다") from None

    @property
    def active(self) -> bool:
        return self._state == "recording"

    def status(self) -> dict:
        """원문·비밀값·자유 형식 오류를 넣지 않은 독립 상태 사본을 반환한다."""
        samples = self._data_bytes // SAMPLE_WIDTH
        return {
            "schema_version": 1, "session_id": self._sid, "recording_id": self.path.name,
            "id": self.path.name,
            "state": self._state, "active": self.active, "reason": self._reason,
            "requested_duration_seconds": self._requested,
            "sample_rate": SAMPLE_RATE, "channels": 1, "sample_width": SAMPLE_WIDTH,
            "sample_count": samples, "duration_seconds": samples / SAMPLE_RATE,
            "pcm_bytes": self._data_bytes, "created_at_ms": self._created_at_ms,
            "first_received_at_ms": self._first_received_at_ms,
            "start_ms": self._start_ms,
            "end_ms": None if self._start_ms is None else self._start_ms + samples * 1000 / SAMPLE_RATE,
            "finished_at_ms": self._finished_at_ms, "timestamp_source": self._timestamp_source,
            "sha256": self._sha256, "manifest_saved": self._manifest_saved,
            "timing_uncertain": True, "audio_verified": False, "quality_resolved": False,
        }

    def feed(self, pcm: bytes, ts_start_ms: int | None = None) -> None:
        """입력 전체를 복제하지 않고 작은 쓰기로 저장하며 도달 시 샘플 단위로 자른다."""
        if not self.active:
            return
        if not isinstance(pcm, bytes) or len(pcm) % SAMPLE_WIDTH:
            self._finish("failed", "invalid_pcm")
            return
        if not pcm:
            self.discontinuity()
            return
        try:
            supplied = None if ts_start_ms is None else _timestamp(ts_start_ms)
            if self._start_ms is None:
                self._first_received_at_ms = _timestamp(self._clock_ms())
                self._start_ms = supplied if supplied is not None else max(
                    0, self._first_received_at_ms - round(len(pcm) * 1000 / (SAMPLE_RATE * SAMPLE_WIDTH)))
                self._timestamp_source = "supplied" if supplied is not None else "first_receive_and_samples"
            elif supplied is not None:
                expected = self._start_ms + self._data_bytes * 1000 / (SAMPLE_RATE * SAMPLE_WIDTH)
                # 정수 밀리초를 사용하는 수신원의 반올림 오차만 허용한다.
                if abs(supplied - expected) > 1:
                    self.discontinuity("timestamp_discontinuity")
                    return
        except Exception:
            self._finish("failed", "invalid_timestamp")
            return
        try:
            remaining = self._limit_bytes - self._data_bytes
            data = memoryview(pcm)[:remaining]
            for offset in range(0, len(data), _WRITE_CHUNK_BYTES):
                self._write_all(data[offset:offset + _WRITE_CHUNK_BYTES], audio=True)
            self._flush_header()
            if self._data_bytes == self._limit_bytes:
                self._finish("completed", "duration_reached")
            elif (not self._manifest_received_saved
                  or self._data_bytes - self._last_manifest_bytes >= SAMPLE_RATE * SAMPLE_WIDTH):
                # 수십 ms씩 들어와도 진행 메타데이터는 음성 1초마다만 쓴다.
                self._save_manifest()
        except Exception:
            self._finish("failed", "storage_error")

    def finish(self, reason: str = "operator_stop") -> None:
        """수동 종료·수집 종료는 요청 길이 미충족이므로 중단 자료로 남긴다."""
        self._finish("interrupted", reason if isinstance(reason, str) and reason in _REASONS else "interrupted")

    def discontinuity(self, reason: str = "audio_discontinuity") -> None:
        """재연결/시각 단절 뒤 음성을 앞 자료에 이어 붙이지 않는다."""
        self._finish("interrupted", reason if isinstance(reason, str) and reason in _REASONS else "audio_discontinuity")

    def _write_all(self, data: bytes | memoryview, *, audio: bool = False) -> None:
        view = memoryview(data)
        while view:
            written = self._file.write(view)
            if not written or written < 0 or written > len(view):
                raise OSError("녹음 저장 실패")
            if audio:
                self._data_bytes += written
            view = view[written:]

    def _flush_header(self) -> None:
        self._file.seek(0)
        self._write_all(_header(self._data_bytes))
        self._file.seek(0, os.SEEK_END)
        self._file.flush()

    def _finish(self, state: str, reason: str) -> None:
        if not self.active:
            return
        self._state, self._reason = state, reason
        try:
            self._finished_at_ms = _timestamp(self._clock_ms())
        except Exception:
            self._finished_at_ms = None
        try:
            self._flush_header()
            os.fsync(self._file.fileno())
            self._file.seek(0)
            sha = hashlib.sha256()
            for chunk in iter(lambda: self._file.read(_WRITE_CHUNK_BYTES), b""):
                sha.update(chunk)
            self._sha256 = sha.hexdigest()
        except Exception:
            self._state, self._reason = "failed", "storage_error"
        finally:
            try:
                self._file.close()
            except Exception:
                self._state, self._reason = "failed", "storage_error"
            self._file = None
        try:
            self._save_manifest()
        except Exception:
            self._manifest_saved = False
            self._state, self._reason = "failed", "manifest_error"

    def _save_manifest(self) -> None:
        """소유한 manifest만 원자 교체한다. 음성 본문은 삭제/재작성하지 않는다."""
        target = safe_output_path(self.path / "manifest.json")
        temporary = safe_output_path(self.path / (".manifest-" + uuid.uuid4().hex + ".json"))
        value = self.status()
        value["manifest_saved"] = True
        encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        with create_private_file(temporary) as stream:
            stream.write(encoded)
            stream.flush()
            if not self.active:
                os.fsync(stream.fileno())
        atomic_replace(temporary, target)
        self._manifest_saved = True
        self._last_manifest_bytes = self._data_bytes
        self._manifest_received_saved = self._start_ms is not None
