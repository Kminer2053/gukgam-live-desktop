"""공식 Sortformer C ABI를 CPU 전용 소유 자식에서 실행한다. 다운로드는 하지 않는다."""
from __future__ import annotations

from array import array
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import wave

from .nemotron_cpu import NemotronContractError, validate_cpu_cores, decode_pcm_request, REQUEST_ID
from .nemotron_worker import apply_own_cpu_affinity, _open_read_locked, _hash
from .nemotron_diarization_assets import verify_assets, asset_root

MAX_AUDIO_SECONDS = 24 * 60 * 60
MAX_SEGMENTS = 100_000
MAX_LINE_BYTES = 4 * 1024 * 1024
PRESETS = ("v3-streaming", "v3-offline")
SPEAKERS = frozenset(f"speaker_{i}" for i in range(8))


def validate_preset(value):
    if not isinstance(value, str) or value not in PRESETS:
        raise NemotronContractError("invalid_diarization_preset")
    return value


class ModelConfig(ctypes.Structure):
    # NVIDIA v0.2.0 include/nemo_speech/diar.h: size-prefixed C ABI.
    _fields_ = [("size", ctypes.c_size_t), ("model_path", ctypes.c_char_p), ("gpu", ctypes.c_int32),
                ("preset", ctypes.c_char_p), *[(name, ctypes.c_int32) for name in
                ("chunk_frames", "right_context_frames", "left_context_frames", "fifo_frames",
                 "spkcache_frames", "update_period_frames")]]


class Segment(ctypes.Structure):
    _fields_ = [("start_time", ctypes.c_double), ("end_time", ctypes.c_double), ("speaker", ctypes.c_int32)]


def validate_segments(rows, duration):
    if isinstance(rows, list) and len(rows) > MAX_SEGMENTS:
        raise NemotronContractError("diarization_segment_limit")
    if not isinstance(rows, list):
        raise NemotronContractError("invalid_diarization_result")
    result, previous = [], -1.0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"start", "end", "speaker"}:
            raise NemotronContractError("invalid_diarization_result")
        start, end, speaker = row["start"], row["end"], row["speaker"]
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not math.isfinite(start) or not math.isfinite(end) or not previous <= start < end
                or start < 0 or start >= duration or end > duration + .010
                or not isinstance(speaker, str) or speaker not in SPEAKERS):
            raise NemotronContractError("invalid_diarization_result")
        result.append({"start": float(start), "end": min(float(end), duration), "speaker": speaker})
        previous = start
    return result


class NativeDiarization:
    def __init__(self, data_dir, cpu_cores=2, *, bundled_dir=None, preset="v3-streaming"):
        self.preset = validate_preset(preset)
        self.cpu_core_limit = validate_cpu_cores(cpu_cores)
        self.logical_cpus_used = apply_own_cpu_affinity(cpu_cores)
        self.affinity_verified = True
        self.assets = self.dll_dir = self.lib = None
        self.model = ctypes.c_void_p()
        self.stream = ctypes.c_void_p()
        try:
            self.assets = verify_assets(asset_root(data_dir, bundled_dir))
            self.dll_dir = os.add_dll_directory(str(self.assets.bin_dir))
            self.lib = ctypes.CDLL(str(self.assets.bin_dir / "nemo_speech_asr_c.dll"), winmode=0x1100)
            specs = {
                "create": ([ctypes.POINTER(ModelConfig), ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
                "destroy": ([ctypes.c_void_p], None),
                "num_speakers": ([ctypes.c_void_p], ctypes.c_int32),
                "seconds_per_frame": ([ctypes.c_void_p], ctypes.c_double),
                "stream_open": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
                "stream_push_f32": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_size_t, ctypes.c_int32], ctypes.c_int),
                "stream_finish": ([ctypes.c_void_p], ctypes.c_int),
                "stream_close": ([ctypes.c_void_p], None),
                "segments": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(Segment), ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_size_t)], ctypes.c_int),
            }
            for name, (args, result) in specs.items():
                call = getattr(self.lib, "nemo_speech_diar_" + name)
                call.argtypes, call.restype = args, result
            config = ModelConfig()
            config.size, config.model_path, config.gpu = ctypes.sizeof(config), str(self.assets.model).encode("utf-8"), -1
            config.preset, config.left_context_frames = self.preset.encode("ascii"), -1
            if self.lib.nemo_speech_diar_create(ctypes.byref(config), ctypes.byref(self.model)) != 0 or not self.model:
                raise NemotronContractError("diarization_initialization_failed")
            if self.lib.nemo_speech_diar_num_speakers(self.model) != 8 or abs(self.lib.nemo_speech_diar_seconds_per_frame(self.model) - .010) > 1e-6:
                raise NemotronContractError("diarization_model_contract_mismatch")
            self.asset_integrity_verified = True
            self.sample_count = 0
        except BaseException:
            self.close()
            raise

    def reset(self):
        if self.stream:
            self.lib.nemo_speech_diar_stream_close(self.stream)
            self.stream = ctypes.c_void_p()
        self.sample_count = 0

    def start(self):
        self.reset()
        if self.lib.nemo_speech_diar_stream_open(self.model, ctypes.byref(self.stream)) != 0 or not self.stream:
            raise NemotronContractError("diarization_stream_open_failed")

    def push(self, pcm):
        if not self.stream or not pcm or len(pcm) % 2:
            raise NemotronContractError("invalid_diarization_pcm")
        if self.sample_count + len(pcm) // 2 > MAX_AUDIO_SECONDS * 16000:
            raise NemotronContractError("diarization_duration_limit")
        samples = array("h", pcm)
        floats = array("f", (v / 32768 for v in samples))
        pointer = (ctypes.c_float * len(floats)).from_buffer(floats)
        if self.lib.nemo_speech_diar_stream_push_f32(self.stream, pointer, len(floats), 16000) != 0:
            raise NemotronContractError("diarization_push_failed")
        self.sample_count += len(floats)

    def segments(self, *, finish=False):
        if not self.stream:
            raise NemotronContractError("diarization_stream_not_open")
        if finish and self.lib.nemo_speech_diar_stream_finish(self.stream) != 0:
            raise NemotronContractError("diarization_finish_failed")
        count = ctypes.c_size_t()
        if self.lib.nemo_speech_diar_segments(self.stream, None, None, 0, ctypes.byref(count)) != 0:
            raise NemotronContractError("diarization_segments_failed")
        if count.value > MAX_SEGMENTS:
            raise NemotronContractError("diarization_segment_limit")
        buffer = (Segment * count.value)()
        capacity = count.value
        if count.value and self.lib.nemo_speech_diar_segments(self.stream, None, buffer, capacity, ctypes.byref(count)) != 0:
            raise NemotronContractError("diarization_segments_failed")
        if count.value > capacity:
            raise NemotronContractError("diarization_segments_failed")
        rows = [{"start": s.start_time, "end": s.end_time, "speaker": f"speaker_{s.speaker - 1}"} for s in buffer[:count.value]]
        return validate_segments(rows, self.sample_count / 16000)

    def diarize_wav(self, path):
        # 입력 전체를 하나의 streaming state로 처리해 VAD 경계마다 ID를 초기화하지 않는다.
        from ..secure_files import open_regular_file
        with open_regular_file(path) as guard, _open_read_locked(Path(path)) as source:
            before, guarded = os.fstat(source.fileno()), os.fstat(guard.fileno())
            if (before.st_dev, before.st_ino) != (guarded.st_dev, guarded.st_ino):
                raise NemotronContractError("diarization_audio_identity_changed")
            audio_hash = _hash(source)
            try:
                with wave.open(source, "rb") as audio:
                    if ((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE")
                            or audio.getnframes() <= 0):
                        raise NemotronContractError("invalid_diarization_wav")
                    if audio.getnframes() > MAX_AUDIO_SECONDS * 16000:
                        raise NemotronContractError("diarization_duration_limit")
                    expected = audio.getnframes()
                    self.start()
                    while pcm := audio.readframes(32000):
                        self.push(pcm)
                    if self.sample_count != expected:
                        raise NemotronContractError("truncated_diarization_wav")
                    rows = self.segments(finish=True)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise NemotronContractError("diarization_audio_changed")
                return {"segments": rows, "audio_sha256": audio_hash, "sample_count": self.sample_count}
            finally:
                self.reset()

    def close(self):
        if self.lib is not None:
            self.reset()
            if self.model:
                self.lib.nemo_speech_diar_destroy(self.model)
                self.model = ctypes.c_void_p()
        if self.dll_dir is not None:
            self.dll_dir.close()
            self.dll_dir = None
        if self.assets is not None:
            self.assets.close()
            self.assets = None


def dispatch(worker, value, state):
    if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
            or len(value["id"]) != 32 or any(c not in "0123456789abcdef" for c in value["id"])):
        raise NemotronContractError("invalid_diarization_request")
    op = value.get("op")
    if op == "wav" and set(value) == {"id", "op", "path"} and isinstance(value["path"], str):
        if state.get("session") is not None:
            raise NemotronContractError("diarization_stream_already_open")
        return worker.diarize_wav(value["path"])
    keys = {"id", "op", "session_id"} | ({"audio", "offset_samples"} if op == "push" else set())
    sid = value.get("session_id")
    if (set(value) != keys or not isinstance(sid, str) or REQUEST_ID.fullmatch(sid) is None
            or op not in {"open", "push", "finish", "reset"}):
        raise NemotronContractError("invalid_diarization_request")
    if op == "open":
        if state.get("session") is not None:
            raise NemotronContractError("diarization_stream_already_open")
        worker.start()
        state.update(session=sid, digest=hashlib.sha256())
        return {"stream_id": sid, "segments": [], "sample_count": 0,
                "audio_sha256": state["digest"].hexdigest(), "final": False}
    if state.get("session") != sid:
        raise NemotronContractError("diarization_stream_mismatch")
    if op == "reset":
        worker.reset()
        state["session"] = None
        return {"stream_id": sid, "reset": True}
    if op == "push":
        audio = decode_pcm_request(value["audio"])
        offset = value["offset_samples"]
        if audio.request_id != value["id"] or type(offset) is not int or offset < worker.sample_count:
            raise NemotronContractError("diarization_stream_offset_invalid")
        if offset + len(audio.pcm16) // 2 > MAX_AUDIO_SECONDS * 16000:
            raise NemotronContractError("diarization_duration_limit")
        missing = offset - worker.sample_count
        # 제거된 무음도 원래 sample 좌표에 남긴다. 큰 gap도 일정한 메모리로 넣는다.
        while missing:
            count = min(missing, 32000)
            silence = bytes(count * 2)
            worker.push(silence)
            state["digest"].update(silence)
            missing -= count
        worker.push(audio.pcm16)
        state["digest"].update(audio.pcm16)
    result = {"stream_id": sid, "segments": worker.segments(finish=op == "finish"),
              "sample_count": worker.sample_count, "audio_sha256": state["digest"].hexdigest(),
              "final": op == "finish"}
    if op == "finish":
        worker.reset()
        state["session"] = None
    return result


def serve(worker, input, output):
    def emit(value):
        output.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
        output.flush()
    emit({"ready": True, "protocol": 1, "task": "diarization", "gpu": False,
          "preset": getattr(worker, "preset", "v3-streaming"),
          "cpu_core_limit": worker.cpu_core_limit, "logical_cpus_used": worker.logical_cpus_used,
          "affinity_verified": worker.affinity_verified, "asset_integrity_verified": worker.asset_integrity_verified})
    state = {"session": None}
    try:
        while line := input.readline(MAX_LINE_BYTES + 1):
            value = {}
            try:
                if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                    raise NemotronContractError("invalid_diarization_request")
                value = json.loads(line)
                result = dispatch(worker, value, state)
                emit({"id": value["id"], **result})
            except Exception as error:
                emit({"id": value.get("id") if isinstance(value, dict) else None,
                      "error": str(error) if isinstance(error, NemotronContractError) else "diarization_failed"})
    finally:
        worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="앱 소유 CPU 음성 화자분리 작업자")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--bundled-dir", type=Path)
    parser.add_argument("--cpu-cores", type=int, default=2)
    parser.add_argument("--preset", choices=PRESETS, default="v3-streaming")
    args = parser.parse_args(argv)
    worker = None
    try:
        worker = NativeDiarization(args.data_dir, args.cpu_cores, bundled_dir=args.bundled_dir, preset=args.preset)
        serve(worker, sys.stdin, sys.stdout)
        return 0
    except Exception:
        if worker is not None:
            worker.close()
        print('{"ready":false,"error":"diarization_initialization_failed"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
