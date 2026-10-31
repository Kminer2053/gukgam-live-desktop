"""고정 CPU SDK를 소유 자식 안에서만 불러오는 전사 작업자.

다운로드 기능은 없다. CPU affinity는 이 작업자 자신에게만 적용하며 스레드 수를
고정하지 않는다. 네이티브 모델은 기동 때 한 번 읽고 요청 사이에 재사용한다.
"""
from __future__ import annotations

import hashlib
import json
import ctypes
import os
from pathlib import Path, PurePosixPath
import sys
import zipfile
from typing import Callable, Protocol, TextIO

from .nemotron_cpu import (LANGUAGE, MAX_LINE_BYTES, REQUEST_ID, MODEL_FILE, MODEL_BYTES,
    MODEL_SHA256, RUNTIME_ARCHIVE, RUNTIME_BYTES, RUNTIME_SHA256, NemotronContractError,
    decode_pcm_request, validate_segments, validate_cpu_cores)


def _select_cpu_affinity_mask(allowed: int, limit: int, core_masks=None) -> int:
    """가능하면 물리 코어마다 하나씩 고르되 기존 논리 CPU 상한과 허용 범위를 지킨다."""
    validate_cpu_cores(limit)
    width = ctypes.sizeof(ctypes.c_size_t) * 8
    bits = [1 << index for index in range(width) if allowed & (1 << index)]
    fallback = sum(bits[:limit])
    if limit == 1 or not isinstance(core_masks, (tuple, list)) or not core_masks:
        return fallback
    covered, candidates = 0, []
    for core in core_masks:
        if type(core) is not int or core <= 0 or core.bit_length() > width or covered & core:
            return fallback
        covered |= core
        eligible = core & allowed
        if eligible:
            candidates.append(eligible & -eligible)
    if allowed & ~covered:
        return fallback
    selected = sum(sorted(candidates)[:limit])
    for bit in bits:
        if selected.bit_count() >= limit:
            break
        if not selected & bit:
            selected |= bit
    return selected


def _processor_core_masks(kernel, *, _last_error=None):
    """Windows의 단일 그룹 물리 코어 관계만 읽는다. 미지원·오류는 기존 선택으로 복귀한다."""
    if ctypes.sizeof(ctypes.c_size_t) != 8:
        return None
    try:
        query = getattr(kernel, "GetLogicalProcessorInformationEx", None)
        if query is None:
            return None
        class GroupAffinity(ctypes.Structure):
            _fields_ = [("Mask", ctypes.c_size_t), ("Group", ctypes.c_uint16),
                        ("Reserved", ctypes.c_uint16 * 3)]
        class ProcessorRelationship(ctypes.Structure):
            _fields_ = [("Flags", ctypes.c_uint8), ("EfficiencyClass", ctypes.c_uint8),
                        ("Reserved", ctypes.c_uint8 * 20), ("GroupCount", ctypes.c_uint16),
                        ("GroupMask", GroupAffinity * 1)]
        class CoreRelationship(ctypes.Structure):
            _fields_ = [("Relationship", ctypes.c_uint32), ("Size", ctypes.c_uint32),
                        ("Processor", ProcessorRelationship)]
        query.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        query.restype = ctypes.c_int32
        last_error = _last_error or getattr(ctypes, "get_last_error", lambda: -1)
        required = ctypes.c_uint32()
        if query(0, None, ctypes.byref(required)) or last_error() != 122:
            return None
        if not 0 < required.value <= 1024 * 1024:
            return None
        buffer = ctypes.create_string_buffer(required.value)
        if not query(0, buffer, ctypes.byref(required)) or not 0 < required.value <= len(buffer):
            return None
        masks, groups, offset = [], set(), 0
        while offset < required.value:
            if required.value - offset < ctypes.sizeof(CoreRelationship):
                return None
            entry = CoreRelationship.from_buffer_copy(buffer, offset)
            if (entry.Relationship != 0 or entry.Size < ctypes.sizeof(CoreRelationship)
                    or entry.Size > required.value - offset or entry.Processor.GroupCount != 1):
                return None
            groups.add(entry.Processor.GroupMask[0].Group)
            mask = int(entry.Processor.GroupMask[0].Mask)
            if not mask or len(groups) != 1 or len(masks) >= 64:
                return None
            masks.append(mask)
            offset += entry.Size
        return tuple(masks) if masks else None
    except Exception:
        # 선택 최적화 조회 실패가 기존의 검증된 CPU 상한 적용을 막지 않는다.
        return None


def apply_own_cpu_affinity(cpu_cores: int, *, _kernel=None) -> int:
    """다른 PID를 열지 않고 현재 작업자의 기존 허용 마스크에서 최대 N개만 사용한다."""
    validate_cpu_cores(cpu_cores)
    if _kernel is None and os.name != "nt":
        raise NemotronContractError("windows_cpu_runtime_required")
    from ctypes import wintypes
    kernel = _kernel if _kernel is not None else ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    kernel.GetProcessAffinityMask.restype = wintypes.BOOL
    kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel.SetProcessAffinityMask.restype = wintypes.BOOL
    own = kernel.GetCurrentProcess()
    current, system = ctypes.c_size_t(), ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(own, ctypes.byref(current), ctypes.byref(system)):
        raise NemotronContractError("cpu_affinity_unavailable")
    core_masks = _processor_core_masks(kernel) if cpu_cores > 1 and current.value.bit_count() > 1 else None
    mask = _select_cpu_affinity_mask(current.value, cpu_cores, core_masks)
    if not mask or not kernel.SetProcessAffinityMask(own, mask):
        raise NemotronContractError("cpu_affinity_not_applied")
    if not kernel.GetProcessAffinityMask(own, ctypes.byref(current), ctypes.byref(system)) or current.value != mask:
        raise NemotronContractError("cpu_affinity_not_verified")
    return mask.bit_count()


def _open_read_locked(path: Path):
    """Windows에서는 검사부터 DLL/모델 해제까지 쓰기·삭제 공유를 허용하지 않는다."""
    if os.name != "nt":
        return path.open("rb")  # 합성 무모델 파일 검증 시험용. 실제 NativeASR는 Windows만 허용한다.
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise NemotronContractError("asset_read_lock_failed")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def _hash(stream):
    stream.seek(0)
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


class VerifiedAssets:
    def __init__(self, bin_dir, model, opened):
        self.bin_dir, self.model, self.opened = bin_dir, model, opened
    def close(self):
        for stream in reversed(self.opened):
            stream.close()
        self.opened.clear()


def verify_assets(data_dir: Path, *, _archive_bytes=RUNTIME_BYTES, _archive_sha=RUNTIME_SHA256,
                  _model_bytes=MODEL_BYTES, _model_sha=MODEL_SHA256) -> VerifiedAssets:
    """큰 파일 SHA 검사는 작업자 안에서 한다. 공식 ZIP과 설치된 모든 DLL을 서로 대조한다."""
    root = Path(data_dir).resolve()
    archive = root / "runtimes" / RUNTIME_ARCHIVE
    runtime = root / "runtimes" / RUNTIME_ARCHIVE.removesuffix(".zip")
    model = root / "models" / "nemotron35-asr" / MODEL_FILE
    opened = []
    def opened_inside(path):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise NemotronContractError("asset_outside_app_data")
        stream = _open_read_locked(resolved)
        opened.append(stream)
        return stream
    try:
        if archive.stat().st_size != _archive_bytes or model.stat().st_size != _model_bytes:
            raise NemotronContractError("asset_size_mismatch")
        original = opened_inside(archive)
        if _hash(original) != _archive_sha:
            raise NemotronContractError("runtime_archive_hash_mismatch")
        packaged_dlls, seen = set(), set()
        with zipfile.ZipFile(original) as bundle:
            for entry in bundle.infolist():
                parts = PurePosixPath(entry.filename).parts
                raw_name = entry.orig_filename
                raw_parts = PurePosixPath(raw_name).parts
                normalized = "/".join(parts)
                if (normalized.casefold() in seen or raw_name.rstrip("/") != normalized
                        or any(part.endswith((".", " ")) for part in raw_parts)
                        or "\\" in raw_name or ":" in raw_name or
                        not raw_parts or raw_parts[0] != runtime.name or ".." in raw_parts or raw_name.startswith("/")
                        or "\\" in entry.filename or ":" in entry.filename or not parts
                        or parts[0] != runtime.name or ".." in parts or entry.filename.startswith("/")):
                    raise NemotronContractError("invalid_runtime_archive_layout")
                seen.add(normalized.casefold())
                if entry.is_dir():
                    continue
                if not (entry.filename.lower().endswith((".dll", ".exe")) or "/share/licenses/" in entry.filename):
                    continue
                installed = runtime.joinpath(*parts[1:])
                stream = opened_inside(installed)
                if installed.stat().st_size != entry.file_size:
                    raise NemotronContractError("runtime_member_size_mismatch")
                with bundle.open(entry) as packaged:
                    if _hash(stream) != _hash(packaged):
                        raise NemotronContractError("runtime_member_hash_mismatch")
                if installed.parent == runtime / "bin" and installed.suffix.lower() == ".dll":
                    packaged_dlls.add(installed.name.lower())
        actual = {p.name.lower() for p in (runtime / "bin").iterdir() if p.suffix.lower() == ".dll"}
        if actual != packaged_dlls or "nemo_speech_asr_c.dll" not in packaged_dlls:
            raise NemotronContractError("runtime_dll_set_mismatch")
        model_stream = opened_inside(model)
        if _hash(model_stream) != _model_sha:
            raise NemotronContractError("model_hash_mismatch")
        return VerifiedAssets((runtime / "bin").resolve(strict=True), model.resolve(strict=True), opened)
    except BaseException:
        for stream in reversed(opened):
            stream.close()
        raise


class BackendConfig(ctypes.Structure):
    _fields_ = [("size", ctypes.c_size_t), ("gpu", ctypes.c_int32)]


class ModelConfig(ctypes.Structure):
    _fields_ = [("size", ctypes.c_size_t), ("path", ctypes.c_char_p), ("name", ctypes.c_char_p)]


class RecognizerConfig(ctypes.Structure):
    _fields_ = [("size", ctypes.c_size_t), ("backend", ctypes.POINTER(BackendConfig)),
               ("model", ctypes.POINTER(ModelConfig)), *[(key, ctypes.c_void_p) for key in
               ("streaming", "decoder", "vad", "endpointing", "postproc", "diar", "batching")]]


class RecognitionOptions(ctypes.Structure):
    # append-only C ABI의 언어 필드까지인 짧은 구조체. 나머지는 SDK 기본값을 쓴다.
    _fields_ = [("size", ctypes.c_size_t), ("request_id", ctypes.c_char_p), ("language_code", ctypes.c_char_p)]


class NativeASR:
    def __init__(self, data_dir: Path, cpu_cores: int):
        self.cpu_core_limit = validate_cpu_cores(cpu_cores)
        self.logical_cpus_used = apply_own_cpu_affinity(cpu_cores)
        self.affinity_verified = True
        self.assets = self.dll_dir = self.lib = None
        self.recognizer = ctypes.c_void_p()
        try:
            self.assets = verify_assets(data_dir)
            self.dll_dir = os.add_dll_directory(str(self.assets.bin_dir))
            self.lib = ctypes.CDLL(str(self.assets.bin_dir / "nemo_speech_asr_c.dll"), winmode=0x1100)
            library = self.lib
            library.nemo_speech_asr_create.argtypes = [ctypes.POINTER(RecognizerConfig), ctypes.POINTER(ctypes.c_void_p)]
            library.nemo_speech_asr_create.restype = ctypes.c_int
            library.nemo_speech_asr_destroy.argtypes = [ctypes.c_void_p]
            library.nemo_speech_asr_destroy.restype = None
            library.nemo_speech_asr_recognize_f32.argtypes = [ctypes.c_void_p, ctypes.POINTER(RecognitionOptions),
                ctypes.POINTER(ctypes.c_float), ctypes.c_size_t, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p)]
            library.nemo_speech_asr_recognize_f32.restype = ctypes.c_int
            library.nemo_speech_asr_result_alternative_count.argtypes = [ctypes.c_void_p]
            library.nemo_speech_asr_result_alternative_count.restype = ctypes.c_size_t
            library.nemo_speech_asr_result_transcript.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            library.nemo_speech_asr_result_transcript.restype = ctypes.c_char_p
            library.nemo_speech_asr_result_destroy.argtypes = [ctypes.c_void_p]
            library.nemo_speech_asr_result_destroy.restype = None
            backend = BackendConfig(ctypes.sizeof(BackendConfig), -1)
            model = ModelConfig(ctypes.sizeof(ModelConfig), str(self.assets.model).encode("utf-8"), None)
            config = RecognizerConfig()
            config.size, config.backend, config.model = ctypes.sizeof(config), ctypes.pointer(backend), ctypes.pointer(model)
            if library.nemo_speech_asr_create(ctypes.byref(config), ctypes.byref(self.recognizer)) != 0 or not self.recognizer:
                raise NemotronContractError("native_model_initialization_failed")
            self.asset_integrity_verified = True
        except BaseException:
            self.close()
            raise

    def transcribe(self, samples, sample_rate: int, language: str) -> list[dict]:
        if language != LANGUAGE or sample_rate != 16000:
            raise NemotronContractError("unsupported_audio_language_or_rate")
        audio = (ctypes.c_float * len(samples)).from_buffer(samples)
        options = RecognitionOptions(ctypes.sizeof(RecognitionOptions), None, LANGUAGE.encode("ascii"))
        result = ctypes.c_void_p()
        try:
            status = self.lib.nemo_speech_asr_recognize_f32(self.recognizer, ctypes.byref(options), audio,
                len(samples), sample_rate, ctypes.byref(result))
            if status != 0 or not result:
                raise NemotronContractError("native_recognition_failed")
            if not self.lib.nemo_speech_asr_result_alternative_count(result):
                return []
            raw = self.lib.nemo_speech_asr_result_transcript(result, 0)
            text = raw.decode("utf-8", "strict") if raw else ""
            return [{"start": 0, "end": len(samples) / sample_rate, "text": text}] if text.strip() else []
        finally:
            if result:
                self.lib.nemo_speech_asr_result_destroy(result)

    def close(self):
        if self.lib is not None and self.recognizer:
            self.lib.nemo_speech_asr_destroy(self.recognizer)
            self.recognizer = ctypes.c_void_p()
        if self.dll_dir is not None:
            self.dll_dir.close()
            self.dll_dir = None
        if self.assets is not None:
            self.assets.close()
            self.assets = None


class Backend(Protocol):
    def transcribe(self, samples, sample_rate: int, language: str) -> list[dict]: ...
    def close(self) -> None: ...


def _json(line: str):
    def pairs(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise NemotronContractError("invalid_json")
            value[key] = item
        return value
    def invalid(_):
        raise NemotronContractError("invalid_json")
    return json.loads(line, object_pairs_hook=pairs, parse_constant=invalid)


def serve(input_stream: TextIO, output_stream: TextIO, *, cpu_cores: int,
          backend_factory: Callable[[int], Backend]) -> None:
    """한 모델 핸들을 재사용한다. 원문·네이티브 오류·경로를 진단 응답에 복사하지 않는다."""
    validate_cpu_cores(cpu_cores)
    backend = None
    def reply(value):
        output_stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        output_stream.flush()
    try:
        try:
            backend = backend_factory(cpu_cores)
        except Exception:
            reply({"protocol": 1, "ready": False, "error": "native_initialization_failed"})
            return
        reply({"protocol": 1, "ready": True, "cpu_core_limit": cpu_cores,
               "affinity_verified": getattr(backend, "affinity_verified", False),
               "asset_integrity_verified": getattr(backend, "asset_integrity_verified", False),
               "logical_cpus_used": getattr(backend, "logical_cpus_used", None), "gpu": False, "language": LANGUAGE})
        while True:
            line = input_stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_LINE_BYTES or len(line.encode("utf-8")) > MAX_LINE_BYTES:
                reply({"error": "request_line_too_large"})
                return
            rid = None
            try:
                value = _json(line)
                if value == {"op": "quit"}:
                    return
                if isinstance(value, dict) and isinstance(value.get("id"), str) and REQUEST_ID.fullmatch(value["id"]):
                    rid = value["id"]
                request = decode_pcm_request(value)
                result = validate_segments(backend.transcribe(request.float_samples(), request.sample_rate, LANGUAGE),
                                           request.duration)
                reply({"id": request.request_id, "segments": result,
                       "audio_sha256": hashlib.sha256(request.pcm16).hexdigest()})
            except (NemotronContractError, json.JSONDecodeError) as error:
                code = str(error) if isinstance(error, NemotronContractError) else "invalid_json"
                reply({"id": rid, "error": code})
            except Exception:
                reply({"id": rid, "error": "native_request_failed"})
                return
    finally:
        if backend is not None:
            backend.close()


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="앱 소유 Nemotron 한국어 CPU 작업자")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cpu-cores", type=int, choices=range(1, 65), default=2)
    args = parser.parse_args(argv)
    out = sys.stdout
    # 네이티브 라이브러리의 C printf도 프로토콜 파이프로 흘러가지 않게 한다.
    protocol_fd = os.dup(out.fileno())
    with os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1) as protocol:
        sink = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(sink, out.fileno())
        finally:
            os.close(sink)
        serve(sys.stdin, protocol, cpu_cores=args.cpu_cores,
              backend_factory=lambda cores: NativeASR(args.data_dir, cores))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
