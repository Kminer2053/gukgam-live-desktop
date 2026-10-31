"""첫 CPU VAD 준비에만 쓰는 선택적 Python 스택 진단.

15초는 멈춤의 판정 기준이 아니다. 정상적인 준비 지연에도 단발 덤프가 생길 수
있다. faulthandler는 Python 파일명·함수명·행 번호만 기록하며 소스·locals·입력
본문은 기록하지 않는다. Python 3.12 스택으로 네이티브 코드 내부 원인을 확정할
수 없다. 파일은 로컬 진단 폴더에만 두며 외부로 전송하지 않는다.

타이머는 프로세스 전역이므로 이 모듈만 소유한다. repeat=False와 프로세스당
한 번의 시도, 디렉터리의 최대 두 파일로 생성 수를 제한한다. fd에 직접 쓰는
faulthandler의 출력에 엄격한 바이트 상한이 있다고 주장하지 않는다.
"""
from __future__ import annotations

from contextlib import contextmanager
import faulthandler
import os
from pathlib import Path
import stat
import threading
from typing import BinaryIO, Iterator
import uuid


_LOCK = threading.Lock()
_attempted = False
_owner: object | None = None
# cancel이 확인되지 않으면 fd 재사용을 막기 위해 프로세스 수명 동안 한 개만 유지한다.
_retained_stream: BinaryIO | None = None
_TIMEOUT_SECONDS = 15
_MAX_FILES = 2


class _Skip(Exception):
    """경로나 예외 문자열을 포함하지 않는 내부 건너뜀 코드."""


def _notify(ctx, state: str, reason: str) -> None:
    try:
        ctx.set_state(audio_startup_diagnostics=state, audio_startup_diagnostics_reason=reason)
    except Exception:
        pass
    if state in ('skipped', 'retained'):
        try:
            ctx.log('음성 초기화 스택 진단을 건너뜁니다' if state == 'skipped' else
                    '음성 초기화 스택 진단의 정리를 확인하지 못해 전용 파일을 유지합니다',
                    'info' if state == 'skipped' else 'warning')
        except Exception:
            pass


def _claim() -> object | None:
    global _attempted, _owner
    if not _LOCK.acquire(blocking=False):
        return None
    try:
        if _attempted:
            return None
        _attempted = True
        _owner = object()
        return _owner
    finally:
        _LOCK.release()


def _is_reparse(path: Path) -> bool:
    value = path.lstat()
    return stat.S_ISLNK(value.st_mode) or bool(getattr(value, 'st_file_attributes', 0) & 0x400)


def _regular_directory(path: Path) -> None:
    if not path.is_absolute():
        raise _Skip('unsafe_directory')
    for candidate in (*reversed(path.parents), path):
        if _is_reparse(candidate) or not stat.S_ISDIR(candidate.lstat().st_mode):
            raise _Skip('unsafe_directory')
    if path.resolve(strict=True) != path:
        raise _Skip('unsafe_directory')


def _open_file(ctx) -> BinaryIO:
    root = Path(ctx.config.data_dir)
    _regular_directory(root)
    folder = root / 'diagnostics'
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _regular_directory(folder)
    count = 0
    with os.scandir(folder) as entries:
        for entry in entries:
            value = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
                raise _Skip('unsafe_directory')
            if not stat.S_ISREG(value.st_mode):
                raise _Skip('unsafe_directory')
            count += 1
            if count >= _MAX_FILES:
                raise _Skip('file_limit')
    path = folder / ('cpu-vad-startup-' + uuid.uuid4().hex + '.log')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0)
    fd = os.open(path, flags, 0o600)
    try:
        _regular_directory(folder)
        by_fd, by_path = os.fstat(fd), path.lstat()
        if (_is_reparse(path) or not stat.S_ISREG(by_fd.st_mode)
                or not stat.S_ISREG(by_path.st_mode)
                or (by_fd.st_dev, by_fd.st_ino) != (by_path.st_dev, by_path.st_ino)
                or path.resolve(strict=True) != path):
            raise _Skip('unsafe_file')
        stream = os.fdopen(fd, 'wb', buffering=0)
    except BaseException:
        os.close(fd)
        raise
    return stream


def _finish(ctx, token: object, stream: BinaryIO | None, arm_attempted: bool) -> None:
    global _owner, _retained_stream
    # 소유 토큰이 다른 문맥은 마지막 전역 타이머를 취소할 권한이 없다.
    with _LOCK:
        if _owner is not token:
            return
        if arm_attempted:
            try:
                faulthandler.cancel_dump_traceback_later()
            except BaseException as error:
                _retained_stream = stream
                _notify(ctx, 'retained', 'cancel_unconfirmed')
                if not isinstance(error, Exception):
                    raise
                return
        if stream is not None:
            try:
                stream.close()
            except Exception:
                _retained_stream = stream
                _notify(ctx, 'retained', 'close_unconfirmed')
                return
        _owner = None


@contextmanager
def cpu_vad_startup_trace(ctx, *, enabled: bool) -> Iterator[None]:
    """호출자가 선택한 첫 seg.feed의 to_thread 대기만 감싼다.

    enabled는 호출자가 설치 Windows/Nemotron/energy 여부로 계산한다. 준비·파일
    오류는 고정 상태로 건너뛰며 본문 예외와 취소는 그대로 전파한다. 타이머 취소가
    확인되기 전에는 fd를 닫지 않는다. cancel 실패 시 파일 한 개를 열린 채 보유한다.
    """
    if not enabled:
        yield
        return
    token = _claim()
    if token is None:
        _notify(ctx, 'skipped', 'already_attempted')
        yield
        return
    stream = None
    arm_attempted = False
    prepared = False
    try:
        try:
            stream = _open_file(ctx)
            arm_attempted = True
            faulthandler.dump_traceback_later(_TIMEOUT_SECONDS, repeat=False, file=stream, exit=False)
            prepared = True
        except _Skip as error:
            _notify(ctx, 'skipped', str(error))
        except Exception:
            _notify(ctx, 'skipped', 'diagnostic_unavailable')
        if prepared:
            _notify(ctx, 'armed', 'first_feed')
        yield
    finally:
        _finish(ctx, token, stream, arm_attempted)
        if prepared and _owner is None:
            # 취소 전 이미 덤프됐을 수 있으므로 정상/성공/파일 없음이라고 판정하지 않는다.
            _notify(ctx, 'finished', 'watch_cancelled')
