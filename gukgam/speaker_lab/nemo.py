"""명시한 로컬 실행물과 모델로만 음성 화자 분리를 실험한다."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
import wave
from dataclasses import dataclass


# 설치된 NeMo-Speech.cpp v0.2.0의 include/nemo_speech/diar.h는 V3 출력
# 간격을 10ms로 명시한다. WAV 끝을 넘는 최대 한 프레임만 잘라내며,
# 인코더의 거친 80ms 단위나 입력 길이 비례 허용 오차로 확대하지 않는다.
_MAX_END_OVERRUN_SECONDS = 0.010


class NemoDiarizationError(ValueError):
    """입력 검증 또는 격리 실행에 실패했다."""


class _WindowsPipeSelector:
    """소유 자식의 읽기 파이프만 PeekNamedPipe로 준비 여부를 확인한다."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        self._peek.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD)
        self._peek.restype = wintypes.BOOL
        self._keys = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._keys.clear()

    def register(self, stream, events):
        if events != selectors.EVENT_READ:
            raise ValueError("음성 출력 파이프는 읽기만 허용합니다")
        key = selectors.SelectorKey(stream, stream.fileno(), events, None)
        if key.fd in self._keys:
            raise KeyError("이미 등록한 출력 파이프입니다")
        self._keys[key.fd] = key
        return key

    def unregister(self, stream):
        return self._keys.pop(stream if isinstance(stream, int) else stream.fileno())

    def get_map(self):
        return self._keys

    def select(self, timeout=None):
        import msvcrt

        deadline = time.monotonic() + max(0, timeout or 0)
        while True:
            ready = []
            for key in self._keys.values():
                available = self._wintypes.DWORD()
                ok = self._peek(msvcrt.get_osfhandle(key.fd), None, 0, None,
                                self._ctypes.byref(available), None)
                if not ok:
                    # 끊긴 파이프도 준비 상태로 돌려 기존 read의 EOF 처리를 거친다.
                    if self._ctypes.get_last_error() not in (109, 232, 233):
                        raise OSError("음성 실행 출력 파이프를 확인하지 못했습니다")
                    ready.append((key, selectors.EVENT_READ))
                elif available.value:
                    ready.append((key, selectors.EVENT_READ))
            remaining = deadline - time.monotonic()
            if ready or remaining <= 0:
                return ready
            time.sleep(min(remaining, 0.01))


def _pipe_selector():
    # Windows의 기본 selector는 소켓만 지원하므로 익명 파이프를 등록하지 않는다.
    return _WindowsPipeSelector() if os.name == "nt" else selectors.DefaultSelector()


@dataclass(frozen=True)
class DiarizedSegment:
    """입력 WAV 기준 초 단위 구간과 해당 실행 안에서만 유효한 익명 화자다."""

    start: float
    end: float
    speaker_id: str


@dataclass(frozen=True)
class NemoDiarizationResult:
    """정확도나 자원 최대 사용량을 추정하지 않은 로컬 실행 결과다."""

    segments: tuple[DiarizedSegment, ...]
    elapsed_seconds: float
    audio_duration_seconds: float
    peak_rss_bytes: int | None = None


def parse_rttm(
    text: str, *, duration_seconds: float | None = None
) -> tuple[DiarizedSegment, ...]:
    """단일 녹음 RTTM을 읽으며 동시 발화와 반복 구간을 그대로 보존한다.

    화자 ID는 시각순 첫 등장 순서로 익명화한다. 서로 다른 파일의 ID는
    같은 사람을 뜻하지 않는다. 시작은 실제 음성 내부여야 하며, 끝점의
    최대 10ms 초과만 WAV 끝으로 제한한다. 전체 구간 이동·무음 채움은 없다.
    """
    if duration_seconds is not None and (
        not math.isfinite(duration_seconds) or duration_seconds <= 0
    ):
        raise NemoDiarizationError("음성 길이는 양의 유한값이어야 합니다.")
    rows: list[tuple[float, float, str]] = []
    recording: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 10 or fields[0] != "SPEAKER" or fields[2] != "1":
            raise NemoDiarizationError(f"RTTM {number}행의 형식이 잘못됐습니다.")
        if recording is not None and fields[1] != recording:
            raise NemoDiarizationError("서로 다른 녹음의 RTTM을 합칠 수 없습니다.")
        recording = fields[1]
        if fields[7] in {"<NA>", "<na>", "-"}:
            raise NemoDiarizationError(f"RTTM {number}행에 화자 ID가 없습니다.")
        try:
            start, length = float(fields[3]), float(fields[4])
        except ValueError:
            raise NemoDiarizationError(f"RTTM {number}행의 시간이 잘못됐습니다.") from None
        end = start + length
        if (not all(math.isfinite(value) for value in (start, length, end))
                or start < 0 or length <= 0 or end <= start):
            raise NemoDiarizationError(f"RTTM {number}행의 시간 범위가 잘못됐습니다.")
        if duration_seconds is not None:
            if start >= duration_seconds or end > duration_seconds + _MAX_END_OVERRUN_SECONDS:
                raise NemoDiarizationError(f"RTTM {number}행이 입력 음성 범위를 벗어납니다.")
            end = min(end, duration_seconds)
        rows.append((start, end, fields[7]))
    rows.sort(key=lambda row: (row[0], row[1]))
    speakers: dict[str, str] = {}
    result = []
    for start, end, source_speaker in rows:
        speaker = speakers.setdefault(source_speaker, f"speaker_{len(speakers)}")
        result.append(DiarizedSegment(start, end, speaker))
    return tuple(result)


def _local_file(path: Path, description: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise OSError
        return resolved
    except (OSError, RuntimeError, ValueError, TypeError):
        raise NemoDiarizationError(f"{description}의 기존 로컬 파일을 지정해야 합니다.") from None


def _audio_duration(path: Path) -> float:
    if path.suffix.lower() != ".wav":
        raise NemoDiarizationError("WAV 확장자의 로컬 음성 파일이 필요합니다.")
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getcomptype() != "NONE"
                or audio.getsampwidth() != 2
                or audio.getnchannels() not in (1, 2)
                or not 8000 <= audio.getframerate() <= 96000
                or audio.getnframes() <= 0
            ):
                raise NemoDiarizationError("8~96kHz 모노·스테레오 PCM16 WAV가 필요합니다.")
            frames = audio.getnframes()
            frame_size = audio.getsampwidth() * audio.getnchannels()
            # 큰 WAV를 메모리에 올리지 않고 잘린 데이터만 검사한다.
            count = 0
            while chunk := audio.readframes(65536):
                count += len(chunk)
            if count != frames * frame_size:
                raise NemoDiarizationError("WAV의 음성 데이터가 잘렸습니다.")
            return frames / audio.getframerate()
    except (OSError, EOFError, wave.Error):
        raise NemoDiarizationError("읽을 수 있는 PCM16 WAV가 필요합니다.") from None


def _stop_owned_process(process: subprocess.Popen[bytes]) -> None:
    """이번 함수가 만든 자식만 종료하고 회수한다."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _bounded_run(command: list[str], timeout: float, limit: int) -> tuple[str, float]:
    # 모델 설정·인증 비밀값·공유 머신의 실행 주입 환경을 자식에게 전달하지 않는다.
    environment = {name: os.environ[name] for name in ("HOME", "TMPDIR", "LANG", "LC_ALL") if name in os.environ}
    environment.update({"PATH": "/usr/bin:/bin", "NO_COLOR": "1"})
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
        )
    except OSError:
        raise NemoDiarizationError("로컬 화자 분리 실행물을 시작하지 못했습니다.") from None
    assert process.stdout is not None and process.stderr is not None
    output = bytearray()
    total = 0
    try:
        with _pipe_selector() as selector:
            for stream in (process.stdout, process.stderr):
                if os.name != "nt":
                    os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise NemoDiarizationError("로컬 화자 분리 제한 시간을 초과했습니다.")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, min(65536, limit - total + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise NemoDiarizationError("로컬 화자 분리 출력 크기 한도를 초과했습니다.")
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            if process.returncode != 0:
                # 실행물 진단은 경로·본문을 포함할 수 있으므로 오류에 그대로 넣지 않는다.
                raise NemoDiarizationError(f"로컬 화자 분리 실행이 실패했습니다(종료 코드 {process.returncode}).")
        elapsed = time.monotonic() - started
        try:
            return output.decode("utf-8"), elapsed
        except UnicodeDecodeError:
            raise NemoDiarizationError("화자 분리 출력이 UTF-8 형식이 아닙니다.") from None
    finally:
        _stop_owned_process(process)
        process.stdout.close()
        process.stderr.close()


def diarize_local(
    wav_path: Path,
    *,
    executable: Path,
    model_path: Path,
    device: str = "metal",
    timeout_seconds: float = 900,
    max_output_bytes: int = 8 * 1024 * 1024,
) -> NemoDiarizationResult:
    """이미 준비한 로컬 파일만 사용하며 모델 다운로드·서버·녹음은 시작하지 않는다.

    NeMo-Speech.cpp의 기본 상태 유지 스트리밍을 사용한다. 전체 녹음을 한 번에
    어텐션하는 --offline은 긴 입력 한계 때문에 사용하지 않는다. 이 실행의 시간은
    모델 로딩을 포함한 벽시계 시간이며 실제 화자 정확도·최대 메모리는 별도 측정한다.
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise NemoDiarizationError("제한 시간은 양의 유한값이어야 합니다.")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
        raise NemoDiarizationError("출력 크기 한도는 양의 정수여야 합니다.")
    if not isinstance(device, str) or not re.fullmatch(r"(?:metal|cpu|cuda(?::\d+)?|vulkan(?::\d+)?)", device):
        raise NemoDiarizationError("사용할 로컬 연산 장치를 명시해야 합니다.")
    binary = _local_file(executable, "실행물")
    model = _local_file(model_path, "모델")
    audio = _local_file(wav_path, "음성")
    if not os.access(binary, os.X_OK):
        raise NemoDiarizationError("로컬 실행물에 실행 권한이 없습니다.")
    try:
        with model.open("rb") as stream:
            if model.suffix.lower() != ".gguf" or stream.read(4) != b"GGUF":
                raise NemoDiarizationError("이미 준비한 GGUF 모델 파일이 필요합니다.")
    except OSError:
        raise NemoDiarizationError("로컬 모델을 읽지 못했습니다.") from None
    duration = _audio_duration(audio)
    command = [
        str(binary), "diarize", str(audio), "--model", str(model),
        "--format", "rttm", "--device", device, "--recording-id", "recording",
    ]
    rttm, elapsed = _bounded_run(command, timeout_seconds, max_output_bytes)
    return NemoDiarizationResult(parse_rttm(rttm, duration_seconds=duration), elapsed, duration)
