"""Nemotron 한국어 CPU 전사의 고정 출처·입력 계약·소유 작업자.

런타임·모델을 내려받거나 불러오지 않는다. v0.2.0 C ABI의 스레드 제어가
노출이 없으므로 소유 자식의 논리 CPU 코어 상한만 제한한다. 스레드 수 보장이 아니다.
"""
from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import sysconfig
from array import array
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


RUNTIME_TAG = "v0.2.0"
RUNTIME_COMMIT = "6a3ca369370782acee0dd155e34394ba60b920c4"
RUNTIME_ARCHIVE = "nemo-speech-0.2.0-windows-x86_64-cpu.zip"
RUNTIME_BYTES = 5_528_418
RUNTIME_SHA256 = "82c80451086e86194aba9af19f800da77cd66d1c28d993d0efceffd6cdb49bb8"
MODEL_REPO = "nvidia/nemotron-3.5-asr-streaming-0.6b"
MODEL_REVISION = "1c8deaecc64b91f034d73e08dd8b64625eb3395d"
MODEL_FILE = "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
MODEL_BYTES = 741_548_352
MODEL_SHA256 = "a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae"
MODEL_LICENSE = "OpenMDW-1.1"
LANGUAGE = "ko-KR"
SAMPLE_RATE = 16_000
MAX_AUDIO_SECONDS = 30
MAX_PCM_BYTES = SAMPLE_RATE * 2 * MAX_AUDIO_SECONDS
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_RESULT_CHARS = 64_000
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z", re.ASCII)
MAX_CPU_CORES = 64


class NemotronContractError(ValueError):
    """경로·음성 본문·네이티브 예외를 포함하지 않는 짧은 오류 코드."""


def source_worker_command(module: str, frozen_flag: str) -> list[str]:
    """설치판 진입점은 유지하고 개발 Windows의 실제 자식을 직접 소유한다."""
    if getattr(sys, "frozen", False):
        return [sys.executable, frozen_flag]
    if os.name != "nt":
        return [sys.executable, "-m", module]
    paths = list(dict.fromkeys([
        str(Path(__file__).resolve().parents[2]),
        sysconfig.get_path("purelib"), sysconfig.get_path("platlib"),
    ]))
    # -I는 외부 PYTHONPATH·사용자 site·현재 폴더를 제외한다. 앱 소스와
    # 현재 venv 의존성만 명시해 Windows venv launcher의 간접 자식을 피한다.
    bootstrap = (f"import runpy,sys;sys.path[:0]={paths!r};"
                 "module=sys.argv.pop(1);runpy.run_module(module,run_name='__main__')")
    return [getattr(sys, "_base_executable", sys.executable), "-I", "-X", "utf8", "-c", bootstrap, module]


def validate_cpu_cores(cpu_cores: int) -> int:
    if type(cpu_cores) is not int or not 1 <= cpu_cores <= MAX_CPU_CORES:
        raise NemotronContractError("invalid_cpu_core_limit")
    return cpu_cores


def available_logical_cpus() -> int:
    """현재 프로세스에서 사용할 수 있는 논리 CPU 수를 안전한 상한 안에서 반환한다."""
    counter = getattr(os, "process_cpu_count", None)
    try:
        count = counter() if callable(counter) else os.cpu_count()
    except (OSError, ValueError):
        count = None
    return max(1, min(MAX_CPU_CORES, count if type(count) is int and count > 0 else 1))


def effective_cpu_cores(config: dict | None = None) -> int:
    """자동이면 가용 CPU 전체, 수동이면 장치의 실제 가용 수까지로 제한한다."""
    config = config or {}
    available = available_logical_cpus()
    if config.get("nemotron_cpu_auto") is True:
        return available
    requested = config.get("nemotron_cpu_cores", min(2, available))
    try:
        return min(validate_cpu_cores(requested), available)
    except NemotronContractError:
        return min(2, available)


def encode_pcm_request(request_id: str, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> dict:
    """기존 앱의 16kHz mono s16le 입력만 허용한다. 자르거나 재표본화하지 않는다."""
    if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
        raise NemotronContractError("invalid_request_id")
    if type(sample_rate) is not int or sample_rate != SAMPLE_RATE:
        raise NemotronContractError("pcm_rate_requires_conversion")
    if not isinstance(pcm16, bytes) or not pcm16 or len(pcm16) % 2 or len(pcm16) > MAX_PCM_BYTES:
        raise NemotronContractError("invalid_pcm_length")
    return {"protocol": 1, "op": "transcribe", "id": request_id, "sample_rate": sample_rate,
            "language": LANGUAGE, "pcm16_base64": base64.b64encode(pcm16).decode("ascii")}


@dataclass(frozen=True)
class AudioRequest:
    request_id: str
    pcm16: bytes
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.pcm16) / 2 / self.sample_rate

    def float_samples(self) -> array:
        """작업자 안에서만 호출한다. C ABI가 받는 mono float32로 변환한다."""
        import sys
        values = array("h")
        values.frombytes(self.pcm16)
        if sys.byteorder != "little":
            values.byteswap()
        return array("f", (value / 32768 for value in values))


def decode_pcm_request(value: object) -> AudioRequest:
    keys = {"protocol", "op", "id", "sample_rate", "language", "pcm16_base64"}
    if not isinstance(value, dict) or set(value) != keys or type(value["protocol"]) is not int \
            or value["protocol"] != 1 or value["op"] != "transcribe" or value["language"] != LANGUAGE:
        raise NemotronContractError("invalid_audio_request")
    encoded = value["pcm16_base64"]
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PCM_BYTES + 2) // 3):
        raise NemotronContractError("invalid_pcm_length")
    try:
        pcm = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise NemotronContractError("invalid_pcm_encoding") from None
    encode_pcm_request(value["id"], pcm, value["sample_rate"])
    return AudioRequest(value["id"], pcm, value["sample_rate"])


def validate_segments(value: object, duration: float) -> list[dict]:
    """음성 범위를 벗어난 시각·과도한 본문·알 수 없는 필드를 결과로 채택하지 않는다."""
    if not isinstance(value, list) or len(value) > 1024:
        raise NemotronContractError("invalid_asr_result")
    result, chars, previous = [], 0, 0.0
    for row in value:
        if not isinstance(row, dict) or set(row) != {"start", "end", "text"}:
            raise NemotronContractError("invalid_asr_result")
        start, end, text = row["start"], row["end"], row["text"]
        if (any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end))
                or not previous <= start <= end <= duration or not isinstance(text, str) or not text.strip()):
            raise NemotronContractError("invalid_asr_result")
        chars += len(text)
        if chars > MAX_RESULT_CHARS:
            raise NemotronContractError("asr_result_too_large")
        result.append({"start": float(start), "end": float(end), "text": text})
        previous = end
    return result


def preparation_contract() -> dict:
    return {"provider": "nemotron_cpu", "language": LANGUAGE, "runtime_tag": RUNTIME_TAG,
            "runtime_commit": RUNTIME_COMMIT, "model_revision": MODEL_REVISION,
            "native_adapter_implemented": True, "installed_worker_entry_implemented": True,
            "stable_c_abi_has_thread_control": False, "cpu_limit_kind": "owned_child_logical_cpu_affinity",
            "ready": False, "reason": "native_assets_and_inference_not_verified_here"}


class NemotronCPU:
    """추론 하나·대기열 없음. 취소·만료는 자신이 만든 자식만 정리한다."""
    def __init__(self, data_dir: Path, *, cpu_cores: int = 2, load_timeout: float = 180,
                 request_timeout: float = 60, process_factory=None):
        self.data_dir = Path(data_dir)
        self.cpu_cores = validate_cpu_cores(cpu_cores)
        if any(type(t) not in (int, float) or not math.isfinite(t) or not 0 < t <= 600
               for t in (load_timeout, request_timeout)):
            raise NemotronContractError("invalid_timeout")
        self.load_timeout, self.request_timeout = load_timeout, request_timeout
        self._factory = process_factory or asyncio.create_subprocess_exec
        self._proc, self._busy, self._closed = None, False, False
        self.state = "idle"
        self.provenance = None

    def command(self) -> list[str]:
        entry = source_worker_command("gukgam.stt.nemotron_worker", "--nemotron-worker")
        return [*entry, "--data-dir", str(self.data_dir), "--cpu-cores", str(self.cpu_cores)]

    async def _stop(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._proc = None
            return
        try:
            try:
                proc.terminate()
            except ProcessLookupError:
                self._proc = None
                return
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    self._proc = None
                    return
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    raise NemotronContractError("owned_worker_cleanup_incomplete") from None
            if proc.returncode is None:
                raise NemotronContractError("owned_worker_cleanup_incomplete")
        except BaseException:
            # 종료 실패·재취소 뒤에는 이전 자식을 재사용하거나 다른 자식을 만들지 않는다.
            self._closed = True
            raise
        if self._proc is proc:
            self._proc = None

    async def _read(self) -> dict:
        line = await self._proc.stdout.readline()
        if not line or len(line) > MAX_LINE_BYTES:
            raise NemotronContractError("invalid_worker_response")
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            raise NemotronContractError("invalid_worker_response") from None
        if not isinstance(value, dict):
            raise NemotronContractError("invalid_worker_response")
        return value

    async def _ensure_worker(self):
        if self._proc is not None and self._proc.returncode is None:
            return
        await self._stop()
        self.state = "loading"
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed or k.startswith("_PYI_")}
        env.update(PYTHONUTF8="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", CUDA_VISIBLE_DEVICES="-1")
        options = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE,
                   "stderr": asyncio.subprocess.DEVNULL, "limit": MAX_LINE_BYTES + 1, "env": env}
        if os.name == "nt":
            options["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: 소유 작업자 창을 띄우지 않는다.
        async with asyncio.timeout(self.load_timeout):
            self._proc = await self._factory(*self.command(), **options)
            if self._closed:
                raise NemotronContractError("worker_closed")
            ready = await self._read()
            if (ready.get("ready") is not True or type(ready.get("protocol")) is not int or ready.get("protocol") != 1
                    or ready.get("cpu_core_limit") != self.cpu_cores
                    or ready.get("affinity_verified") is not True
                    or ready.get("asset_integrity_verified") is not True
                    or ready.get("gpu") is not False or ready.get("language") != LANGUAGE
                    or type(ready.get("logical_cpus_used")) is not int or not 1 <= ready["logical_cpus_used"] <= self.cpu_cores):
                raise NemotronContractError("worker_initialization_failed")
        self.provenance = {"provider": "nemotron_cpu", "runtime_tag": RUNTIME_TAG,
            "runtime_commit": RUNTIME_COMMIT, "runtime_archive_sha256": RUNTIME_SHA256,
            "model": MODEL_REPO, "model_revision": MODEL_REVISION, "model_sha256": MODEL_SHA256,
            "model_license": MODEL_LICENSE, "asset_integrity_verified": True, "affinity_verified": True,
            "language": LANGUAGE, "gpu": False, "cpu_core_limit": self.cpu_cores,
            "logical_cpus_used": ready["logical_cpus_used"],
            "cpu_limit_kind": "owned_child_logical_cpu_affinity", "time_basis": "whole_input_clip"}
        self.state = "ready"

    async def transcribe(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE, *, request_guard=None) -> dict:
        if self._closed:
            raise NemotronContractError("worker_closed")
        if self._busy:
            raise NemotronContractError("worker_busy_no_queue")
        request = encode_pcm_request(uuid4().hex, pcm16, sample_rate)
        self._busy = True
        read_task = None
        try:
            if request_guard is not None and not request_guard():
                raise NemotronContractError("input_no_longer_current")
            await self._ensure_worker()
            async with asyncio.timeout(self.request_timeout):
                if self._closed or request_guard is not None and not request_guard():
                    raise NemotronContractError("input_no_longer_current")
                self.state = "running"
                self._proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                await self._proc.stdin.drain()
                read_task = asyncio.create_task(self._read())
                while not read_task.done():
                    if self._closed or request_guard is not None and not request_guard():
                        raise NemotronContractError("input_no_longer_current")
                    await asyncio.wait({read_task}, timeout=.1)
                response = read_task.result()
                if response.get("id") != request["id"] or response.get("error"):
                    raise NemotronContractError("worker_result_rejected")
                if request_guard is not None and not request_guard():
                    raise NemotronContractError("input_no_longer_current")
                segments = validate_segments(response.get("segments"), len(pcm16) / 2 / sample_rate)
                audio_hash = response.get("audio_sha256")
                if not isinstance(audio_hash, str) or re.fullmatch(r"[0-9a-f]{64}", audio_hash) is None:
                    raise NemotronContractError("invalid_audio_hash")
                expected_hash = await asyncio.to_thread(lambda: hashlib.sha256(pcm16).hexdigest())
                if audio_hash != expected_hash:
                    raise NemotronContractError("audio_hash_mismatch")
                if request_guard is not None and not request_guard():
                    raise NemotronContractError("input_no_longer_current")
                self.state = "ready"
                return {"segments": segments, "audio_sha256": audio_hash, "provenance": {
                    **self.provenance, "inference_completed": True, "input_sample_count": len(pcm16) // 2}}
        except BaseException:
            self.state = "closed" if self._closed else "error"
            if read_task is not None and not read_task.done():
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
            await self._stop()
            raise
        finally:
            self._busy = False

    async def close(self):
        self._closed = True
        self.state = "closed"
        await self._stop()


def safe_stt_provenance(segments: list[dict]) -> dict | None:
    """완료된 CPU 전사의 고정 출처만 원수신 기록에 붙인다. 임의 중첩 필드는 넘기지 않는다."""
    if not segments:
        return None
    value = segments[0].get("stt_provenance")
    expected = {"provider": "nemotron_cpu", "runtime_tag": RUNTIME_TAG, "runtime_commit": RUNTIME_COMMIT,
        "runtime_archive_sha256": RUNTIME_SHA256, "model": MODEL_REPO, "model_revision": MODEL_REVISION,
        "model_sha256": MODEL_SHA256, "model_license": MODEL_LICENSE, "language": LANGUAGE,
        "gpu": False, "asset_integrity_verified": True, "affinity_verified": True, "inference_completed": True,
        "cpu_limit_kind": "owned_child_logical_cpu_affinity", "time_basis": "whole_input_clip"}
    if (not isinstance(value, dict) or any(value.get(k) != v for k, v in expected.items())
            or any(row.get("stt_provenance") != value for row in segments)):
        return None
    if any(value.get(k) is not expected[k] for k in
           ("gpu", "asset_integrity_verified", "affinity_verified", "inference_completed")):
        return None
    cores, used, samples, digest = (value.get(k) for k in
        ("cpu_core_limit", "logical_cpus_used", "input_sample_count", "audio_sha256"))
    if (type(cores) is not int or not 1 <= cores <= MAX_CPU_CORES
            or type(used) is not int or not 1 <= used <= cores
            or type(samples) is not int or not 0 < samples <= SAMPLE_RATE * MAX_AUDIO_SECONDS
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        return None
    return {**expected, "cpu_core_limit": cores, "logical_cpus_used": used,
            "input_sample_count": samples, "audio_sha256": digest}
