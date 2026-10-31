"""수집 루프와 분리된 Windows CPU 화자분리 작업자 수명주기."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4
import wave

from ..secure_files import open_regular_file
from .nemotron_cpu import (NemotronCPU, NemotronContractError, RUNTIME_TAG, RUNTIME_COMMIT, RUNTIME_SHA256,
                           encode_pcm_request, REQUEST_ID, source_worker_command)
from .nemotron_worker import _hash
from .nemotron_diarization_assets import (MODEL_REPO, MODEL_REVISION, MODEL_SHA256, MODEL_LICENSE,
                                         status, prepare, asset_root, bundled_directory)
from .nemotron_diarization_worker import validate_segments, validate_preset, MAX_AUDIO_SECONDS

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
LIMIT_ERRORS = {"diarization_duration_limit", "diarization_segment_limit", "diarization_response_limit"}


class NemotronDiarizationCPU(NemotronCPU):
    """한 녹음의 익명 음성 화자 구간을 반환한다. 실명·역할 판정은 하지 않는다."""
    def __init__(self, data_dir, *, cpu_cores=2, load_timeout=180, request_timeout=1800,
                 bundled_dir=None, process_factory=None, preset="v3-streaming"):
        self.preset = validate_preset(preset)
        if any(type(t) not in (float, int) or not math.isfinite(t) or not 0 < t <= 3600
               for t in (load_timeout, request_timeout)):
            raise NemotronContractError("invalid_timeout")
        super().__init__(data_dir, cpu_cores=cpu_cores, load_timeout=min(load_timeout, 600),
                         request_timeout=min(request_timeout, 600), process_factory=process_factory)
        self.load_timeout, self.request_timeout = load_timeout, request_timeout
        self.bundled_dir = Path(bundled_dir).absolute() if bundled_dir is not None else bundled_directory()
        self._stream_id = None
        self._stream_samples = 0
        self._stream_digest = hashlib.sha256()

    def check(self):
        if status(self.data_dir, bundled_dir=self.bundled_dir).get("preparing"):
            raise NemotronContractError("diarization_assets_preparing")
        asset_root(self.data_dir, self.bundled_dir)

    def status(self):
        return {**status(self.data_dir, bundled_dir=self.bundled_dir), "worker_state": self.state,
                "running": self._busy, "cpu_core_limit": self.cpu_cores, "preset": self.preset}

    def command(self):
        entry = source_worker_command("gukgam.stt.nemotron_diarization_worker", "--nemotron-diarization-worker")
        command = [*entry, "--data-dir", str(self.data_dir), "--cpu-cores", str(self.cpu_cores), "--preset", self.preset]
        if self.bundled_dir is not None:
            command.extend(["--bundled-dir", str(self.bundled_dir)])
        return command

    async def _read(self):
        try:
            line = await self._proc.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raise NemotronContractError("diarization_response_limit") from None
        if len(line) > MAX_RESPONSE_BYTES:
            raise NemotronContractError("diarization_response_limit")
        if not line:
            raise NemotronContractError("invalid_diarization_response")
        try:
            result = json.loads(line)
        except (ValueError, UnicodeError):
            raise NemotronContractError("invalid_diarization_response") from None
        if not isinstance(result, dict):
            raise NemotronContractError("invalid_diarization_response")
        return result

    async def _ensure_worker(self):
        if self._proc is not None and self._proc.returncode is None:
            return
        await self._stop()
        self.state = "loading"
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed or k.startswith("_PYI_")}
        env.update(PYTHONUTF8="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", CUDA_VISIBLE_DEVICES="-1")
        options = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE,
                   "stderr": asyncio.subprocess.DEVNULL, "limit": MAX_RESPONSE_BYTES + 1, "env": env}
        if os.name == "nt":
            options["creationflags"] = 0x08000000
        async with asyncio.timeout(self.load_timeout):
            self._proc = await self._factory(*self.command(), **options)
            if self._closed:
                raise NemotronContractError("worker_closed")
            ready = await self._read()
            if (ready.get("ready") is not True or type(ready.get("protocol")) is not int or ready.get("protocol") != 1
                    or ready.get("task") != "diarization" or ready.get("gpu") is not False
                    or ready.get("preset") != self.preset
                    or ready.get("cpu_core_limit") != self.cpu_cores or ready.get("affinity_verified") is not True
                    or ready.get("asset_integrity_verified") is not True
                    or type(ready.get("logical_cpus_used")) is not int
                    or not 1 <= ready["logical_cpus_used"] <= self.cpu_cores):
                raise NemotronContractError("diarization_initialization_failed")
        self.provenance = {"provider": "nemotron_diarization_cpu", "model": MODEL_REPO,
            "model_revision": MODEL_REVISION, "model_sha256": MODEL_SHA256, "model_license": MODEL_LICENSE,
            "runtime_tag": RUNTIME_TAG, "runtime_commit": RUNTIME_COMMIT, "runtime_archive_sha256": RUNTIME_SHA256,
            "preset": self.preset,
            "gpu": False, "cpu_core_limit": self.cpu_cores, "logical_cpus_used": ready["logical_cpus_used"],
            "affinity_verified": True, "asset_integrity_verified": True,
            "speaker_identity_scope": "whole_recording", "time_basis": "audio_samples", "frame_seconds": .010,
            "speaker_capacity": 8, "independent_accuracy_verified": False,
            "max_audio_seconds": MAX_AUDIO_SECONDS, "max_segments": 100000, "max_response_bytes": MAX_RESPONSE_BYTES}
        self.state = "ready"

    async def diarize_wav(self, path, *, request_guard=None):
        if self._stream_id is not None:
            raise NemotronContractError("diarization_stream_already_open")
        if self._closed or self._busy:
            raise NemotronContractError("worker_closed" if self._closed else "worker_busy_no_queue")
        path = Path(path).absolute()
        self._busy = True
        waiting = None
        try:
            def allowed():
                if self._closed or request_guard is not None and not request_guard():
                    raise NemotronContractError("input_no_longer_current")
            allowed()
            self.check()
            with open_regular_file(path) as source:
                def inspect():
                    before = os.fstat(source.fileno())
                    digest = _hash(source)
                    with wave.open(source, "rb") as audio:
                        if ((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE")
                                or audio.getnframes() <= 0):
                            raise NemotronContractError("invalid_diarization_wav")
                        if audio.getnframes() > MAX_AUDIO_SECONDS * 16000:
                            raise NemotronContractError("diarization_duration_limit")
                        samples = audio.getnframes()
                    return before, digest, samples
                before, expected, samples = await asyncio.to_thread(inspect)
                allowed()
                await self._ensure_worker()
                allowed()
                request = {"id": uuid4().hex, "op": "wav", "path": str(path)}
                async with asyncio.timeout(self.request_timeout):
                    self.state = "running"
                    self._proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                    await self._proc.stdin.drain()
                    waiting = asyncio.create_task(self._read())
                    while not waiting.done():
                        allowed()
                        await asyncio.wait({waiting}, timeout=.1)
                    result = waiting.result()
                allowed()
                if result.get("id") == request["id"] and result.get("error") in LIMIT_ERRORS:
                    raise NemotronContractError(result["error"])
                after = os.fstat(source.fileno())
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or result.get("id") != request["id"] or result.get("error")
                        or result.get("audio_sha256") != expected or result.get("sample_count") != samples):
                    raise NemotronContractError("diarization_result_rejected")
                rows = validate_segments(result.get("segments"), samples / 16000)
                self.state = "ready"
                return {"segments": rows, "audio_sha256": expected,
                        "provenance": {**self.provenance, "inference_completed": True, "input_sample_count": samples}}
        except BaseException:
            self.state = "closed" if self._closed else "error"
            if waiting is not None and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await self._stop()
            raise
        finally:
            self._busy = False

    async def _stream_exchange(self, payload, *, samples=None, digest=None, request_guard=None):
        if self._closed or self._busy:
            raise NemotronContractError("worker_closed" if self._closed else "worker_busy_no_queue")
        self._busy, waiting = True, None
        try:
            def allowed():
                if self._closed or request_guard is not None and not request_guard():
                    raise NemotronContractError("input_no_longer_current")
            allowed()
            self.check()
            await self._ensure_worker()
            allowed()
            async with asyncio.timeout(self.request_timeout):
                self.state = "running"
                self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                await self._proc.stdin.drain()
                waiting = asyncio.create_task(self._read())
                while not waiting.done():
                    allowed()
                    await asyncio.wait({waiting}, timeout=.1)
                result = waiting.result()
            allowed()
            if result.get("id") == payload["id"] and result.get("error") in LIMIT_ERRORS:
                raise NemotronContractError(result["error"])
            if (result.get("id") != payload["id"] or result.get("error")
                    or result.get("stream_id") != payload["session_id"]):
                raise NemotronContractError("diarization_result_rejected")
            if payload["op"] == "reset":
                if set(result) != {"id", "stream_id", "reset"} or result.get("reset") is not True:
                    raise NemotronContractError("diarization_result_rejected")
            else:
                if (set(result) != {"id", "stream_id", "segments", "sample_count", "audio_sha256", "final"}
                        or type(result.get("sample_count")) is not int or result["sample_count"] != samples
                        or result.get("audio_sha256") != digest
                        or result.get("final") is not (payload["op"] == "finish")):
                    raise NemotronContractError("diarization_result_rejected")
                result["segments"] = validate_segments(result.get("segments"), samples / 16000)
            self.state = "ready"
            return {key: value for key, value in result.items() if key != "id"} | {
                "provenance": {**self.provenance, "speaker_identity_scope": "stream_session",
                               "inference_completed": bool(samples)}}
        except BaseException:
            self._stream_id = None
            self.state = "closed" if self._closed else "error"
            if waiting is not None and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            await self._stop()
            raise
        finally:
            self._busy = False

    @staticmethod
    def _session(session_id):
        if not isinstance(session_id, str) or REQUEST_ID.fullmatch(session_id) is None:
            raise NemotronContractError("diarization_stream_id_invalid")
        return session_id

    async def start_stream(self, session_id, *, request_guard=None):
        sid = self._session(session_id)
        if self._stream_id is not None:
            raise NemotronContractError("diarization_stream_already_open")
        result = await self._stream_exchange({"id": uuid4().hex, "op": "open", "session_id": sid},
            samples=0, digest=hashlib.sha256().hexdigest(), request_guard=request_guard)
        self._stream_id, self._stream_samples, self._stream_digest = sid, 0, hashlib.sha256()
        return result

    async def push_stream(self, session_id, pcm16, sample_rate=16000, *, offset_samples=None, request_guard=None):
        sid = self._session(session_id)
        if self._stream_id != sid:
            raise NemotronContractError("diarization_stream_mismatch")
        request_id = uuid4().hex
        audio = encode_pcm_request(request_id, pcm16, sample_rate)
        offset = self._stream_samples if offset_samples is None else offset_samples
        if type(offset) is not int or offset < self._stream_samples:
            raise NemotronContractError("diarization_stream_offset_invalid")
        if offset + len(pcm16) // 2 > MAX_AUDIO_SECONDS * 16000:
            raise NemotronContractError("diarization_duration_limit")
        digest = self._stream_digest.copy()
        missing = offset - self._stream_samples
        while missing:
            count = min(missing, 32000)
            digest.update(bytes(count * 2))
            missing -= count
        digest.update(pcm16)
        samples = offset + len(pcm16) // 2
        result = await self._stream_exchange({"id": request_id, "op": "push", "session_id": sid,
            "audio": audio, "offset_samples": offset}, samples=samples, digest=digest.hexdigest(), request_guard=request_guard)
        self._stream_samples, self._stream_digest = samples, digest
        return result

    async def finish_stream(self, session_id, *, request_guard=None):
        sid = self._session(session_id)
        if self._stream_id != sid:
            raise NemotronContractError("diarization_stream_mismatch")
        result = await self._stream_exchange({"id": uuid4().hex, "op": "finish", "session_id": sid},
            samples=self._stream_samples, digest=self._stream_digest.hexdigest(), request_guard=request_guard)
        self._stream_id = None
        return result

    async def reset_stream(self, session_id):
        sid = self._session(session_id)
        if self._stream_id is None:
            return {"stream_id": sid, "reset": True}
        if self._stream_id != sid:
            raise NemotronContractError("diarization_stream_mismatch")
        result = await self._stream_exchange({"id": uuid4().hex, "op": "reset", "session_id": sid})
        self._stream_id = None
        return result
