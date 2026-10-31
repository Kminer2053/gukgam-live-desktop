"""설치된 음성 모델을 질의 한 건씩 실행하고 검증된 로컬 결과만 반환한다."""
from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from .evaluation import evaluate_diarization
from .experiment import caption_text, media_payload, timed_words
from .files import digest, read_json, safe_output_path, wav_info
from ..secure_files import make_private_directory, verify_private_file


ROOT = Path(__file__).resolve().parents[2]
MAX_RUN_SECONDS = 1200
_STOP_GRACE_SECONDS = 2
_MODELS = ["local_nemo_diarization", "local_mlx_whisper"]
_ENVIRONMENT_KEYS = ("HOME", "TMPDIR", "LANG", "LC_ALL")


async def _uninterrupted(task: asyncio.Task):
    """외부 취소가 반복돼도 자신이 시작한 자식의 회수는 끝까지 기다린다."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


async def _stop_group(process) -> None:
    """별도 세션으로 생성한 이번 실행의 프로세스 그룹만 종료한다."""
    pid = process.pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or pid == os.getpgrp():
        raise ValueError("음성 실행의 소유 프로세스를 확인하지 못했습니다")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    waiting = asyncio.create_task(process.wait())
    try:
        await asyncio.wait_for(asyncio.shield(waiting), timeout=_STOP_GRACE_SECONDS)
    except TimeoutError:
        pass
    # 부모가 먼저 종료해도 같은 그룹의 모델 자식이 남을 수 있다.
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await waiting


def _private_parents(path: Path) -> None:
    missing = []
    for parent in (path, *path.parents):
        if parent.exists():
            if not parent.is_dir():
                raise ValueError("음성 실행 출력 경로는 디렉터리여야 합니다")
            break
        missing.append(parent)
    for parent in reversed(missing):
        make_private_directory(safe_output_path(parent))


def _finite_number(value: object, *, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def _performance(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("음성 실행 시간 자료가 없습니다")
    elapsed, rss = value.get("elapsed_seconds"), value.get("process_peak_rss_bytes")
    if not _finite_number(elapsed) or not _finite_number(rss, allow_none=True):
        raise ValueError("음성 실행 시간 자료가 잘못됐습니다")
    if rss is not None and (not isinstance(rss, int) or rss < 0):
        raise ValueError("음성 실행 메모리 자료가 잘못됐습니다")
    return {"elapsed_seconds": elapsed, "process_peak_rss_bytes": rss}


class LocalQuestionRunner:
    """최대 15분의 기존 WAV를 네모트론→Whisper 순서로 1회씩 실행한다.

    다운로드·외부 통신·운영 설정 변경은 만들지 않는다. 실행은 한 객체에서
    직렬화하며 전체 20분 제한을 적용한다. 취소·제한 시간 초과는 자신의
    프로세스 그룹을 회수한 뒤에 반환하고, 기존 출력 폴더를 재사용하지 않는다.
    """

    def __init__(self, root: Path = ROOT):
        self.root = Path(root).expanduser().absolute()
        self.executable = self.root / "outputs/speaker-lab-runtime-0.2.0/extracted/nemo-speech-0.2.0-macos-aarch64-metal/bin/nemo-speech"
        self.model = self.root / "models/speaker-lab/Nemotron-3-Diarization.q8_0.gguf"
        self.whisper_model = self.root / "models/whisper-large-v3-turbo"
        self._lock = asyncio.Lock()

    def ready(self) -> bool:
        """모델을 불러오지 않고 고정 설치 자산과 실행 권한만 확인한다."""
        try:
            paths = [self.executable, self.model, self.whisper_model / "config.json",
                     self.whisper_model / "weights.safetensors"]
            if any(not path.is_file() or path.stat().st_size == 0 for path in paths):
                return False
            if not os.access(self.executable, os.X_OK) or not os.access(sys.executable, os.X_OK):
                return False
            with self.model.open("rb") as source:
                return source.read(4) == b"GGUF"
        except (OSError, ValueError):
            return False

    async def run(self, audio: Path, captions: Path, output: Path) -> dict:
        """같은 입력의 정상 완료 결과만 반환하며 모델 출력은 정답으로 보지 않는다."""
        async with self._lock:
            process = None
            spawn_task = None
            started = time.monotonic()
            try:
                if not self.ready():
                    raise ValueError("설치된 음성 모델과 실행물을 확인해야 합니다")
                audio = Path(audio).expanduser().resolve(strict=True)
                captions = Path(captions).expanduser().resolve(strict=True)
                output = safe_output_path(output)
                if output.exists():
                    raise ValueError("음성 실행은 새로운 출력 폴더가 필요합니다")
                info = wav_info(audio)
                caption_payload = read_json(captions)
                source_text, coordinates = caption_text(caption_payload)
                input_hashes = {"audio": info["sha256"], "captions": digest(captions)}
                _private_parents(output.parent)
                environment = {key: os.environ[key] for key in _ENVIRONMENT_KEYS if key in os.environ}
                environment.update(PATH="/usr/bin:/bin", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                                   HF_DATASETS_OFFLINE="1", NO_COLOR="1")
                command = [sys.executable, "-m", "gukgam.speaker_lab.experiment", "--run-local",
                           "--audio", str(audio), "--captions", str(captions), "--output", str(output),
                           "--executable", str(self.executable), "--model", str(self.model),
                           "--whisper-model", str(self.whisper_model), "--device", "metal",
                           "--timeout", str(MAX_RUN_SECONDS)]
                remaining = MAX_RUN_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                        *command, cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True, close_fds=True))
                    # 생성 중 취소돼도 반환할 프로세스 핸들을 먼저 회수한다.
                    process = await asyncio.shield(spawn_task)
                    if await process.wait() != 0:
                        raise ValueError("질의 음성 모델 실행이 정상 완료되지 않았습니다")
                result = self._validate_output(output, input_hashes, info["duration_seconds"],
                                               source_text, coordinates)
                if digest(audio) != input_hashes["audio"] or digest(captions) != input_hashes["captions"]:
                    raise ValueError("음성 대조 중 입력이 바뀌어 결과를 보류합니다")
                if time.monotonic() - started > MAX_RUN_SECONDS:
                    raise TimeoutError
                result["performance"]["elapsed_seconds"] = time.monotonic() - started
                return result
            except BaseException as error:
                if process is None and spawn_task is not None:
                    try:
                        process = await _uninterrupted(spawn_task)
                    except (Exception, asyncio.CancelledError):
                        process = None
                if process is not None:
                    cleanup = asyncio.create_task(_stop_group(process))
                    try:
                        await _uninterrupted(cleanup)
                    except Exception:
                        raise ValueError("질의 음성 실행의 자식 프로세스 회수를 확인하지 못했습니다") from None
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, TimeoutError):
                    raise ValueError("질의 음성 대조의 전체 제한 시간을 초과했습니다") from None
                if not isinstance(error, Exception):
                    raise
                raise ValueError("질의 음성 대조의 입력·설치 상태·실행 결과를 확인해야 합니다") from None

    @staticmethod
    def _validate_output(output: Path, hashes: dict, duration: float,
                         text: str, coordinates: list[dict]) -> dict:
        paths = {name: safe_output_path(output / name) for name in
                 ("result.json", "diarization.json", "words.json")}
        for path in paths.values():
            verify_private_file(path)
        result = read_json(paths["result.json"])
        if (not isinstance(result, dict) or result.get("mode") != "local_model"
                or result.get("models_executed") != _MODELS or result.get("input_hashes") != hashes
                or result.get("production_changed") is not False
                or result.get("quality_resolved") is not False or result.get("release_ready") is not False
                or result.get("caption_text") != text or result.get("caption_coordinates") != coordinates):
            raise ValueError("음성 대조 실행과 입력 근거가 일치하지 않습니다")
        media_id = hashes["audio"]
        turns = media_payload(paths["diarization.json"], media_id)
        words = media_payload(paths["words.json"], media_id)
        if (not isinstance(result.get("audio"), dict) or result["audio"].get("sha256") != media_id
                or result["audio"].get("duration_seconds") != duration
                or words.get("provider") != "local_mlx_whisper"):
            raise ValueError("음성 결과의 원음 또는 전사 실행 근거가 다릅니다")
        segments = turns.get("segments")
        if not isinstance(segments, list) or len(segments) > 10_000 or result.get("diarization") != segments:
            raise ValueError("음성 화자 구간 형식 또는 한도가 잘못됐습니다")
        evaluate_diarization(None, segments, duration_seconds=duration)
        for segment in segments:
            if not isinstance(segment["speaker"], str) or not re.fullmatch(r"speaker_[0-7]", segment["speaker"]):
                raise ValueError("음성 화자 ID 형식이 잘못됐습니다")
        converted = timed_words(words, duration)
        previous_start, previous_end = -1, -1
        for word in converted:
            if (not word.text.strip() or len(word.text) > 1000 or "\0" in word.text
                    or word.start_ms >= word.end_ms
                    or word.start_ms < previous_start or word.end_ms < previous_end):
                raise ValueError("음성 단어 내용 또는 시각 순서가 잘못됐습니다")
            previous_start, previous_end = word.start_ms, word.end_ms
        if sum(len(word.text) for word in converted) > 120_000:
            raise ValueError("음성 단어 문자 수 한도를 넘었습니다")
        return {"words": [{"text": row["text"], "start": row["start"], "end": row["end"]}
                          for row in words["words"]],
                "segments": [{"start": row["start"], "end": row["end"], "speaker": row["speaker"]}
                             for row in segments], "media_sha256": media_id,
                "performance": {"diarization": _performance(result.get("performance")),
                                "transcription": _performance(result.get("asr_performance")),
                                "system_pressure_measured": False},
                "audio_verified": False, "quality_resolved": False}
