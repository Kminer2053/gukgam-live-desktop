"""비공개 실험 자료의 제한 읽기와 새 파일 쓰기."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import wave

from ..secure_files import create_private_file, make_private_directory, open_regular_file


def digest(path: Path) -> str:
    """큰 모델도 메모리에 통째로 올리지 않고 식별한다."""
    h = hashlib.sha256()
    with open_regular_file(path) as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path, max_bytes: int = 8 * 1024 * 1024):
    """자료 크기를 제한하고 비표준 숫자를 거부한다."""
    with open_regular_file(path) as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("실험 자료 크기 한도를 넘었습니다")
    def invalid(_value):
        raise ValueError("유한한 JSON 숫자만 허용합니다")
    def finite(value):
        number = float(value)
        return number if math.isfinite(number) else invalid(value)
    return json.loads(data, parse_constant=invalid, parse_float=finite)


def safe_output_path(path: Path) -> Path:
    """직접 워커 실행에도 운영 data·심볼릭 링크 경계를 동일하게 적용한다."""
    path = Path(os.path.abspath(path.expanduser()))
    production = Path(__file__).resolve().parents[2] / "data"
    if path.is_relative_to(production):
        raise ValueError("운영 data에는 실험 결과를 쓰지 않습니다")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("실험 출력 경로에 링크를 사용할 수 없습니다")
        # macOS에서는 Data도 data와 같은 디렉터리일 수 있다.
        if parent.exists() and production.exists() and parent.samefile(production):
            raise ValueError("운영 data에는 실험 결과를 쓰지 않습니다")
    return path


def new_directory(path: Path) -> Path:
    """기존 산출물·운영 data를 덮어쓰지 않는다."""
    path = safe_output_path(path)
    make_private_directory(path, parents=True)
    return path


def write_json(path: Path, value) -> None:
    """권한 600의 새 파일에만 결과를 기록한다."""
    path = safe_output_path(path)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    with create_private_file(path) as f:
        view = memoryview(encoded)
        while view:
            written = f.write(view)
            if not written:
                raise OSError("비공개 JSON 파일을 모두 기록하지 못했습니다")
            view = view[written:]


def wav_info(path: Path) -> dict:
    """첫 실험은 변환 없는 16kHz 모노 PCM16, 최대 15분으로 한정한다."""
    with open_regular_file(path) as source:
        before = os.fstat(source.fileno())
        with wave.open(source, "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
            if (w.getnchannels(), w.getsampwidth(), rate, w.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("실험 입력은 16kHz 모노 PCM16 WAV여야 합니다")
            duration = frames / rate
            if not 0 < duration <= 900:
                raise ValueError("실험 음성은 0초보다 길고 15분 이하여야 합니다")
            if len(w.readframes(frames + 1)) != frames * 2:
                raise ValueError("WAV 본문 길이가 헤더와 다릅니다")
        # 형식·본문 해시·크기는 같은 열린 파일에서 얻는다.
        source.seek(0)
        sha = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
        after = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("검사 중 WAV 파일의 내용이 변경되었습니다")
    return {"duration_seconds": duration, "sample_rate": rate, "channels": 1,
            "sha256": sha.hexdigest(), "bytes": after.st_size}
