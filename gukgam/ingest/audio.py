"""음성 경로 공통: VAD 로 발화 구간을 자르고(VadSegmenter) STT 로 넘겨 SegmentEvent 로 만든다(run_audio_pipeline).

설계 원칙(실측으로 확인한 것)
- 버퍼는 '현재 발화 구간 + 짧은 앞여유'만 유지한다. 전체 누적 후 자르기는 지연이 19~45초로 폭증했다.
- Whisper 에 긴 오디오를 통째로·고정 길이 청크로 넣지 않는다. 발화 단위(최대 15초)로만 넣는다.
- 발화 중 1.5초마다 부분 전사(STT 가 밀리면 생략), 무음 400ms 에서 확정 전사.
- Silero VAD(threshold 0.5, min_silence 400ms, speech_pad 100ms, 512샘플 창). 못 쓰면 에너지 기반 VAD.
"""
from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
import wave
from array import array
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from sys import byteorder
from typing import Any, AsyncIterator, Callable
from uuid import uuid4

from ..models import SegmentEvent, Turn
from .base import Emit, SourceContext, SourceError, hm
from ..stt.nemotron_cpu import safe_stt_provenance

logger = logging.getLogger(__name__)

SR = 16000
WIN = 512                  # Silero 창(32ms)
BYTES_PER_MS = SR * 2 // 1000


@dataclass
class AudioChunk:
    """발화 구간. start_ms_offset 은 스트림 시작(0) 기준. final=False 면 진행 중인 구간의 스냅샷(부분 전사용)."""

    start_ms_offset: int
    pcm: bytes
    seg_id: int = 0
    final: bool = True

    @property
    def duration_ms(self) -> int:
        return len(self.pcm) // BYTES_PER_MS

    @property
    def end_ms_offset(self) -> int:
        return self.start_ms_offset + self.duration_ms


# ── VAD 엔진 ──────────────────────────────────────────────────────
class _SileroVad:
    name = "silero"

    def __init__(self, threshold: float, min_silence_ms: int, speech_pad_ms: int):
        import torch
        from silero_vad import VADIterator, load_silero_vad

        torch.set_num_threads(1)
        self._torch = torch
        self.it = VADIterator(load_silero_vad(), threshold=threshold, sampling_rate=SR,
                              min_silence_duration_ms=min_silence_ms, speech_pad_ms=speech_pad_ms)

    def __call__(self, frame: Any) -> dict | None:
        return self.it(self._torch.from_numpy(frame))

    def reset(self) -> None:
        self.it.reset_states()


class _EnergyVad:
    """silero 대체: 32ms 창 RMS(dBFS) + 적응형 잡음 바닥 + 히스테리시스. 반환 형식은 VADIterator 와 같다."""

    name = "energy"

    def __init__(self, min_silence_ms: int, speech_pad_ms: int, on_db: float = 12.0, floor_db: float = -48.0):
        self.min_silence = SR * min_silence_ms // 1000
        self.pad = SR * speech_pad_ms // 1000
        self.on_db = on_db
        self.floor_db = floor_db
        self.reset()

    def reset(self) -> None:
        self.cur = 0
        self.triggered = False
        self.temp_end = 0
        self.run = 0
        self.noise = -70.0

    def __call__(self, frame: Any) -> dict | None:
        n = len(frame)
        rms = math.sqrt(math.fsum(float(value) * float(value) for value in frame) / n) if n else 0.0
        return self._advance(n, rms)

    def pcm16(self, frame: array) -> dict | None:
        """원래 정수 PCM의 제곱합을 사용해 추가 네이티브 수치 라이브러리를 피한다."""
        n = len(frame)
        rms = math.sqrt(sum(value * value for value in frame) / n) / 32768.0 if n else 0.0
        return self._advance(n, rms)

    def _advance(self, n: int, rms: float) -> dict | None:
        self.cur += n
        db = 20.0 * math.log10(rms + 1e-9)
        thr_on = max(self.noise + self.on_db, self.floor_db)
        speech, quiet = db >= thr_on, db < thr_on - 4.0
        if not self.triggered:
            # 잡음 바닥: 내려갈 땐 빨리, 올라갈 땐 천천히
            self.noise = db if db < self.noise else self.noise + 0.02 * (db - self.noise)
            self.noise = max(self.noise, -90.0)
        if speech and self.temp_end:
            self.temp_end = 0
        if not self.triggered:
            self.run = self.run + 1 if speech else 0
            if self.run >= 3:
                self.triggered = True
                self.run = 0
                return {"start": max(0, self.cur - self.pad - 3 * n)}
            return None
        if quiet:
            if not self.temp_end:
                self.temp_end = self.cur
            if self.cur - self.temp_end >= self.min_silence:
                end = self.temp_end + self.pad - n
                self.temp_end = 0
                self.triggered = False
                return {"end": end}
        return None


def make_vad(engine: str, threshold: float, min_silence_ms: int, speech_pad_ms: int) -> Any:
    if engine in ("auto", "silero"):
        try:
            return _SileroVad(threshold, min_silence_ms, speech_pad_ms)
        except Exception as e:
            if engine == "silero":
                raise
            logger.warning("Silero VAD 를 쓸 수 없어 에너지 기반 VAD 로 대체합니다: %s", e)
    return _EnergyVad(min_silence_ms, speech_pad_ms)


def _pipeline_vad(cfg: dict, *, installed_windows: bool, heavy_disabled: bool) -> tuple[str, str, str]:
    """Windows CPU 기본 경로는 추가 VAD 모델을 불러오지 않는다. 명시 선택은 유지한다."""
    requested = str(cfg.get("vad") or "auto")
    if requested == "auto" and heavy_disabled:
        return requested, "energy", "heavy_engines_disabled"
    if requested == "auto" and installed_windows and cfg.get("provider") == "nemotron_cpu":
        return requested, "energy", "windows_cpu_default"
    return requested, requested, "requested"


# ── 구간 분할기 ───────────────────────────────────────────────────
class VadSegmenter:
    """16kHz mono s16le 입력 → 발화 단위 청크.

    feed() 는 확정 구간(final) 목록을 돌려주고, on_partial 이 있으면 발화 중 partial_every_ms 마다
    현재 구간 스냅샷(final=False)을 콜백한다. 내부 버퍼는 현재 구간 + 앞여유(preroll)만 유지한다.
    """

    def __init__(self, sample_rate: int = 16000, min_speech_ms: int = 300, end_silence_ms: int = 400,
                 max_chunk_ms: int = 15000, *, partial_every_ms: int = 1500,
                 on_partial: Callable[[AudioChunk], None] | None = None, threshold: float = 0.5,
                 speech_pad_ms: int = 100, engine: str = "auto", preroll_ms: int = 600):
        if sample_rate != SR:
            raise ValueError("VadSegmenter 는 16kHz 만 지원합니다")
        self.min_speech = SR * min_speech_ms // 1000
        self.max_samples = SR * max_chunk_ms // 1000
        self.partial_samples = SR * partial_every_ms // 1000
        self.preroll = SR * preroll_ms // 1000
        self.on_partial = on_partial
        self._vad_args = (engine, threshold, end_silence_ms, speech_pad_ms)
        self._vad: Any = None
        self._tail = b""                 # 창(512샘플)에 못 미친 나머지 바이트
        self._buf = bytearray()          # _buf_start 부터 _pos 까지의 PCM
        self._buf_start = 0              # 절대 샘플 위치
        self._pos = 0                    # VAD 에 넣은 샘플 수
        self._seg_start: int | None = None
        self._last_partial = 0
        self._seg_id = 0
        self._vad_base = 0               # VAD 를 마지막으로 초기화한 절대 샘플 위치(VAD 좌표 → 절대 좌표)
        self.max_buffer_bytes = 0        # 진단용: 버퍼가 커진 최대 크기

    @property
    def engine(self) -> str:
        return self._vad.name if self._vad is not None else self._vad_args[0]

    @property
    def in_speech(self) -> bool:
        return self._seg_start is not None

    @property
    def position_ms(self) -> int:
        return self._pos * 1000 // SR

    def _ensure_vad(self) -> None:
        if self._vad is None:
            self._vad = make_vad(*self._vad_args)

    def feed(self, pcm: bytes) -> list[AudioChunk]:
        self._ensure_vad()
        out: list[AudioChunk] = []
        data = self._tail + bytes(pcm)
        step = WIN * 2
        nwin = len(data) // step
        used = nwin * step
        self._tail = data[used:]
        if nwin == 0:
            return out
        self._buf += data[:used]
        energy = isinstance(self._vad, _EnergyVad)
        if energy:
            arr = array("h")
            arr.frombytes(data[:used])
            if byteorder != "little":
                arr.byteswap()
        else:
            import numpy as np
            arr = np.frombuffer(data[:used], dtype=np.int16).astype(np.float32) / 32768.0
        for k in range(nwin):
            self._pos += WIN
            frame = arr[k * WIN:(k + 1) * WIN]
            ev = self._vad.pcm16(frame) if energy else self._vad(frame)
            if ev:
                if "start" in ev and self._seg_start is None:
                    self._seg_start = max(int(ev["start"]) + self._vad_base, self._buf_start)
                    self._last_partial = self._pos
                elif "end" in ev and self._seg_start is not None:
                    end = max(min(int(ev["end"]) + self._vad_base, self._pos), self._seg_start)
                    self._emit(out, self._seg_start, end)
                    self._seg_start = None
            if self._seg_start is not None:
                if self._pos - self._seg_start >= self.max_samples:
                    cut = self._find_cut()
                    self._emit(out, self._seg_start, cut)
                    self._seg_start = cut
                    self._last_partial = self._pos
                elif self.on_partial is not None and self._pos - self._last_partial >= self.partial_samples:
                    self._last_partial = self._pos
                    self.on_partial(AudioChunk(self._seg_start * 1000 // SR, self._slice(self._seg_start, self._pos),
                                               self._seg_id, final=False))
            self.max_buffer_bytes = max(self.max_buffer_bytes, len(self._buf))
        self._trim()
        return out

    def flush(self) -> list[AudioChunk]:
        """입력 끝: 진행 중인 구간을 확정하고 VAD 상태를 초기화한다."""
        out: list[AudioChunk] = []
        if self._tail:
            n = len(self._tail) // 2
            self._buf += self._tail[:n * 2]
            self._pos += n
            self._tail = b""
        if self._seg_start is not None:
            self._emit(out, self._seg_start, self._pos)
            self._seg_start = None
        if self._vad is not None:
            # VAD 내부 샘플 위치가 0 으로 돌아가므로 이후 좌표는 _vad_base 를 더해 절대 위치로 바꾼다
            self._vad.reset()
            self._vad_base = self._pos
        self._buf = bytearray()
        self._buf_start = self._pos
        return out

    # ── 내부 ───────────────────────────────────────────────
    def _slice(self, s: int, e: int) -> bytes:
        a = max(0, s - self._buf_start) * 2
        b = max(0, e - self._buf_start) * 2
        return bytes(self._buf[a:b])

    def _emit(self, out: list[AudioChunk], s: int, e: int) -> None:
        seg_id = self._seg_id
        self._seg_id += 1
        if e - s < self.min_speech:
            return
        out.append(AudioChunk(s * 1000 // SR, self._slice(s, e), seg_id, final=True))

    def _find_cut(self) -> int:
        """최대 길이 도달 시 마지막 2초 안에서 가장 조용한 32ms 지점에서 자른다(단어 중간 절단 완화)."""
        assert self._seg_start is not None
        lo = max(self._seg_start + self.max_samples // 2, self._pos - 2 * SR, self._buf_start)
        region = self._slice(lo, self._pos)
        n = len(region) // (WIN * 2)
        if n < 2:
            return self._pos
        if isinstance(self._vad, _EnergyVad):
            values = array("h")
            values.frombytes(region[:n * WIN * 2])
            if byteorder != "little":
                values.byteswap()
            # 창 크기가 같으므로 평균 대신 정수 제곱합을 비교한다. 동률은 첫 창을 고른다.
            k = min(range(n), key=lambda index: sum(value * value for value in values[index * WIN:(index + 1) * WIN]))
        else:
            import numpy as np
            a = np.frombuffer(region[:n * WIN * 2], dtype=np.int16).astype(np.float32).reshape(n, WIN)
            energy = np.mean(a * a, axis=1)
            k = int(np.argmin(energy))
        return lo + k * WIN + WIN // 2

    def _trim(self) -> None:
        keep_from = self._seg_start if self._seg_start is not None else self._pos - self.preroll
        keep_from = max(keep_from, self._buf_start)
        drop = keep_from - self._buf_start
        if drop > 0:
            del self._buf[:drop * 2]
            self._buf_start = keep_from


# ── STT 연결 ──────────────────────────────────────────────────────
def build_prompt(ctx: SourceContext, cfg: dict | None = None, limit_names: int = 30) -> str:
    """initial prompt = 명단·기관·용어(앞) + 기본 문구(뒤). Whisper 는 프롬프트 뒤쪽을 남기므로 기본 문구를 끝에 둔다."""
    cfg = cfg or ctx.settings("stt")
    names: list[str] = []
    try:
        for m in ctx.repo.get_roster(ctx.sid)[:limit_names]:
            role = m.get("role")
            suffix = " 위원장" if role == "chair" else (" 위원" if role == "member" else "")
            if m.get("name"):
                names.append(m["name"] + suffix)
        org = ctx.settings("org") or {}
        if org.get("name"):
            names.append(org["name"])
    except Exception:  # pragma: no cover - 방어
        logger.exception("STT 프롬프트용 명단 조회 실패")
    gl = [g for g in (cfg.get("glossary") or []) if isinstance(g, str) and g.strip()]
    head = ", ".join(names + gl)
    base = (cfg.get("prompt") or "").strip()
    return (head + ". " + base).strip(". ").strip() if head else base


def save_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


LIVE_RETRY_DELAYS = (2.0, 5.0)   # 실시간 수집: 같은 구간 STT 재시도 간격(정지 중이면 재시도하지 않음)
FILE_RETRY_DELAYS = (1.0, 3.0)   # 파일: 재시도 뒤에도 실패하면 멈춘다(다시 시작하면 그 구간부터)
LIVE_FAIL_WARN = 3               # 실시간: 연속 실패 구간이 이만큼이면 상태에 오류 표시(수집은 계속)
BACKLOG_WARN_SEC = 60.0          # 실시간: 확정 구간이 이만큼 밀리면 경고
BACKLOG_DROP_SEC = 600.0         # 실시간: 이보다 오래 밀린 구간은 전사하지 않고 '미전사' 표지로 남긴다


class LossMarker:
    """전사하지 못한 구간을 기록 표지 하나로 묶어 남긴다: '— {label}(HH:MM~HH:MM, N건) —'(같은 key 를 갱신)."""

    def __init__(self, ctx: SourceContext, label: str, key_prefix: str):
        self.ctx = ctx
        self.label = label
        self.prefix = key_prefix
        self.key: str | None = None
        self.start = 0
        self.end = 0
        self.count = 0

    async def add(self, ts_start: int, ts_end: int, n: int = 1) -> None:
        if self.key is None:
            self.key, self.start, self.end, self.count = f"{self.prefix}{ts_start}", ts_start, ts_end, 0
        self.count += n
        self.end = max(self.end, ts_end)
        text = f"— {self.label}({hm(self.start)}~{hm(self.end)}, {self.count}건) —"
        try:
            await self.ctx.pipeline.add_marker(text, ts=self.start, key=self.key)
        except Exception:  # pragma: no cover - 방어
            logger.exception("%s 표지 기록 실패", self.label)

    def close(self) -> None:
        self.key = None


LIVE_DIARIZATION_BUFFER_SAMPLES = 120 * SR
LIVE_DIARIZATION_PUSH_SAMPLES = 2 * SR
LIVE_DIARIZATION_LAG_SAMPLES = SR * 304 // 10  # 공식 v3-offline cache chunk 30.4초


class _LiveDiarization:
    """수신은 bounded PCM 큐만 쓴다. native 호출은 기존 전사 worker 하나가 직렬 수행한다."""

    def __init__(self, ctx, cfg, factory=None, *, stop=None):
        self.ctx, self.cfg, self.factory, self.stop = ctx, cfg, factory, stop
        previous = getattr(ctx, "prev_state", {}).get("diarization_epoch", 0)
        self.epoch = (previous if type(previous) is int and previous >= 0 else 0) + 1
        self.stream_id = f"live-{uuid4().hex}"
        self.scope = f"e{self.epoch}"
        self.queue = deque()
        self.pending_samples = self.received_samples = self.processed_samples = 0
        self.tail = b""
        self.provider = None
        self.started = self.finished = self.failed = self.reset_needed = False
        self.result = {"segments": [], "provenance": {}}
        self.state = "waiting"
        self.boundary_readiness = "waiting"
        ctx.set_state(diarization_epoch=self.epoch, diarization_state=self.state,
                      diarization_scope=self.scope, diarization_partials_deferred=True,
                      diarization_identity_reset=False, diarization_preset="v3-offline",
                      diarization_extra_wait_seconds=30.4)
        if ctx.config.disable_heavy_engines and factory is None:
            self.degrade("unavailable", "화자 분리 모델을 사용할 수 없어 음성 전사만 계속합니다")

    def degrade(self, state, message):
        if self.failed:
            return
        self.failed, self.reset_needed, self.state = True, True, state
        self.queue.clear()
        self.pending_samples = 0
        self.result = {"segments": [], "provenance": {}}
        self.ctx.set_state(diarization_state=state, diarization_detail=message,
                           diarization_identity_reset=True, diarization_pending_samples=0)

    def enqueue(self, pcm):
        data = self.tail + bytes(pcm)
        self.tail = data[len(data) // 2 * 2:]
        data = data[:len(data) // 2 * 2]
        offset = self.received_samples
        self.received_samples += len(data) // 2
        if self.failed or not data:
            return
        if self.pending_samples + len(data) // 2 > LIVE_DIARIZATION_BUFFER_SAMPLES:
            self.degrade("degraded", "화자 분리가 실시간 입력을 따라가지 못해 이번 구간의 화자 표시를 중단하고 전사는 계속합니다")
            return
        step = LIVE_DIARIZATION_PUSH_SAMPLES * 2
        for start in range(0, len(data), step):
            part = data[start:start + step]
            self.queue.append((offset + start // 2, part))
        self.pending_samples += len(data) // 2

    def allowed(self):
        return (not self.failed and not (self.stop is not None and self.stop.is_set())
                and self.ctx.settings("stt").get("provider") == "nemotron_cpu")

    def timeout(self, key, default):
        value = self.cfg.get(key, default)
        return value if type(value) in (int, float) and math.isfinite(value) and 1 <= value <= 600 else default

    async def _reset(self):
        if self.reset_needed and self.provider is not None:
            self.reset_needed = False
            try:
                await self.provider.reset_stream(self.stream_id)
            except Exception:
                pass  # 힌트는 이미 해제됐다. 원문 예외를 UI/로그에 남기지 않는다.

    async def _request(self, coroutine):
        """모델 준비 중에도 포화·중지를 감시해 이 요청과 소유 자식만 취소한다."""
        task = asyncio.create_task(coroutine)
        try:
            while not task.done():
                if not self.allowed():
                    raise ValueError("stream_input_no_longer_current")
                await asyncio.wait({task}, timeout=.05)
            if not self.allowed():
                raise ValueError("stream_input_no_longer_current")
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _accept(self, result, expected_samples):
        if self.failed:
            return
        if (not isinstance(result, dict) or result.get("stream_id") != self.stream_id
                or type(result.get("sample_count")) is not int
                or result["sample_count"] != expected_samples
                or not isinstance(result.get("segments"), list)
                or not isinstance(result.get("provenance", {}), dict)):
            raise ValueError("invalid_stream_result")
        self.processed_samples = result["sample_count"]
        self.result = result

    async def advance(self, until=None):
        """offset은 원 PCM만 기준으로 삼는다. partial/VAD pad/ASR retry는 다시 push하지 않는다."""
        if self.failed:
            await self._reset()
            return
        if not self.allowed():
            self.degrade("degraded", "화자 분리 입력이 중지되거나 설정이 바뀌어 남은 음성은 화자 미정으로 전사합니다")
            await self._reset()
            return
        try:
            if self.queue and self.provider is None:
                if self.factory is None:
                    from ..stt.nemotron_diarization import NemotronDiarizationCPU
                    factory = NemotronDiarizationCPU
                else:
                    factory = self.factory
                from ..stt.nemotron_cpu import effective_cpu_cores
                cores = effective_cpu_cores(self.cfg)
                self.provider = factory(self.ctx.config.data_dir,
                    cpu_cores=cores,
                    load_timeout=self.timeout("nemotron_load_timeout_seconds", 180),
                    request_timeout=self.timeout("nemotron_request_timeout_seconds", 60),
                    preset="v3-offline")
                self.provider.check()
                self._accept(await self._request(self.provider.start_stream(self.stream_id, request_guard=self.allowed)), 0)
                self.started = True
                self.state = "buffering"
                self.ctx.set_state(diarization_state=self.state,
                    diarization_detail="CPU 화자 경계를 기다리고 있습니다. 음성 전사에 약 30초가 추가로 걸릴 수 있습니다")
            while self.queue and not self.failed:
                if until is not None and self.processed_samples >= until:
                    break
                offset, pcm = self.queue.popleft()
                self.pending_samples -= len(pcm) // 2
                result = await self._request(self.provider.push_stream(self.stream_id, pcm, sample_rate=SR,
                    offset_samples=offset, request_guard=self.allowed))
                self._accept(result, offset + len(pcm) // 2)
                if until is None:
                    break  # 대기 중인 VAD 확정 구간이 raw 큐 뒤에서 굶지 않게 한 번에 한 push만 한다.
        except asyncio.CancelledError:
            raise
        except Exception:
            self.degrade("unavailable", "화자 분리 모델 준비 또는 연속 처리를 마치지 못해 음성 전사만 계속합니다")
        await self._reset()

    def boundary_ready(self, end_sample):
        """출력 범위/보수적 cache lag를 확인한다. native commit frame 확정을 뜻하지 않는다."""
        if self.failed:
            self.boundary_readiness = "asr_only"
            return True
        if self.finished:
            self.boundary_readiness = "finished"
            return True
        furthest = 0
        try:
            for row in self.result["segments"]:
                start, end, who = row.get("start"), row.get("end"), row.get("speaker")
                if (type(start) not in (int, float) or type(end) not in (int, float)
                        or not math.isfinite(start) or not math.isfinite(end)
                        or not 0 <= start < end <= self.processed_samples / SR
                        or who not in {f"speaker_{n}" for n in range(8)}):
                    raise ValueError
                furthest = max(furthest, round(end * SR))
        except Exception:
            self.degrade("degraded", "화자 분리 경계가 불완전해 화자 미정으로 전사를 계속합니다")
            self.boundary_readiness = "asr_only"
            return True
        if furthest >= end_sample:
            self.boundary_readiness = "segment_extent"
            return True
        if self.processed_samples >= end_sample + LIVE_DIARIZATION_LAG_SAMPLES:
            self.boundary_readiness = "conservative_lag"
            return True
        self.boundary_readiness = "waiting"
        return False

    async def finish(self):
        while self.queue and not self.failed:
            await self.advance()
        if self.started and not self.failed and not self.finished:
            try:
                self._accept(await self._request(self.provider.finish_stream(self.stream_id, request_guard=self.allowed)), self.processed_samples)
                self.finished = True
                self.state = "finished"
                self.ctx.set_state(diarization_state=self.state)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.degrade("degraded", "마지막 화자 경계를 확정하지 못해 음성 전사만 보존합니다")
        await self._reset()

    def metadata(self, speakers=()):
        provenance = self.result.get("provenance") or {}
        return {"speakers": list(speakers), "overlap": len(speakers) > 1,
                "scope": self.scope, "epoch": self.epoch, "state": self.state,
                "provisional": not self.finished, "identity_reset": self.failed,
                "boundary_readiness": self.boundary_readiness, "commit_frame_verified": False,
                "provenance": {**provenance, "speaker_identity_scope": "stream_session", "stream_scope": self.scope,
                               "input_sample_count": self.processed_samples,
                               **({"audio_sha256": self.result["audio_sha256"]}
                                  if isinstance(self.result.get("audio_sha256"), str)
                                  and len(self.result["audio_sha256"]) == 64
                                  and all(char in "0123456789abcdef" for char in self.result["audio_sha256"]) else {})}}

    def regions(self, chunk):
        """음성 샘플 경계로만 나눈다. 미덮인 부분도 버리지 않고 화자 미정으로 전사한다."""
        start = chunk.start_ms_offset * SR // 1000
        samples = len(chunk.pcm) // 2
        if self.failed or not self.started or not samples:
            return [(chunk, ())]
        changes = {0: [], samples: []}
        try:
            for row in self.result["segments"]:
                a, b, who = row.get("start"), row.get("end"), row.get("speaker")
                if (type(a) not in (int, float) or type(b) not in (int, float)
                        or not math.isfinite(a) or not math.isfinite(b) or not 0 <= a < b
                        or b > self.processed_samples / SR
                        or who not in {f"speaker_{n}" for n in range(8)}):
                    raise ValueError
                left, right = max(start, round(a * SR)), min(start + samples, round(b * SR))
                if left < right:
                    changes.setdefault(left - start, []).append((who, 1))
                    changes.setdefault(right - start, []).append((who, -1))
            active, output, previous = {}, [], 0
            for position, events in sorted(changes.items()):
                speakers = tuple(sorted(who for who, count in active.items() if count > 0))
                if position > previous:
                    if output and output[-1][1] == previous and output[-1][2] == speakers:
                        output[-1] = (output[-1][0], position, speakers)
                    else:
                        output.append((previous, position, speakers))
                for who, delta in events:
                    active[who] = active.get(who, 0) + delta
                previous = position
            return [(AudioChunk((start + left) * 1000 // SR, chunk.pcm[left * 2:right * 2], chunk.seg_id), speakers)
                    for left, right, speakers in output]
        except Exception:
            self.degrade("degraded", "화자 분리 경계가 불완전해 화자 미정으로 전사를 계속합니다")
            return [(chunk, ())]

    async def close(self):
        if self.provider is not None:
            try:
                await self.provider.reset_stream(self.stream_id)
            except Exception:
                pass
            finally:
                try:
                    await self.provider.close()
                except Exception:
                    self.ctx.set_state(diarization_state="degraded", diarization_identity_reset=True,
                        diarization_detail="화자 분리 작업자 종료를 확인하지 못했습니다. 전사 기록은 보존했습니다")


async def run_audio_pipeline(ctx: SourceContext, pcm_iter: AsyncIterator[bytes], emit: Emit,
                             base_ms_fn: Callable[[int], int], key_prefix: str, *, partials: bool = True,
                             max_pending: int = 0, segmenter: VadSegmenter | None = None, live: bool | None = None,
                             stop: asyncio.Event | None = None,
                             on_final_done: Callable[[AudioChunk], None] | None = None,
                             diarizer_factory=None) -> dict:
    """PCM 스트림 → VAD → STT(rt.stt.transcribe, 동시 1건) → SegmentEvent.

    부분 결과는 is_final=False 같은 key 로, 확정은 is_final=True. STT 가 밀리면 부분 전사는 건너뛰고 확정은 반드시 처리.
    max_pending>0 이면 확정 대기열이 그만큼 차면 입력을 멈춘다(파일 처리용 역압).
    live(기본: max_pending==0): 실시간 수집원. STT 가 실패해도 멈추지 않고(구간마다 2번 재시도 뒤 '음성인식 실패' 표지),
    대기열이 오래 밀리면 경고·건너뛰기. live=False(파일)는 재시도 뒤에도 실패하면 SourceError.
    on_final_done(chunk): 확정 구간 하나의 처리가 끝날 때마다(파일 재개 위치 기록용).
    pcm_iter 가 빈 bytes 를 내면 불연속(재연결 등)으로 보고 진행 중인 구간을 확정한다. 반환: 통계 dict.
    취소되면(정지 대기 시간 초과) 남은 구간 수를 로그·기록 표지로 남긴다.
    """
    from ..stt.service import STTError

    live = (max_pending == 0) if live is None else live
    cfg = ctx.settings("stt")
    stt = ctx.rt.stt
    acoustic = _LiveDiarization(ctx, cfg, diarizer_factory, stop=stop) if live and cfg.get("provider") == "nemotron_cpu" else None
    check = getattr(stt, "check", None)
    if check is not None:
        try:
            check()                      # 설정 문제(꺼짐·모델 없음)는 시작하자마자 알린다
        except STTError as e:
            raise SourceError(str(e)) from e
    warm: asyncio.Task | None = None
    if acoustic is None and getattr(stt, "warmup", None) is not None:
        warm = asyncio.create_task(stt.warmup(), name=f"stt-warmup-{ctx.sid}")

        def _warm_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                ctx.log(f"음성인식 엔진 준비 실패: {t.exception()}", "warning")
        warm.add_done_callback(_warm_done)
    latest: dict[str, AudioChunk | None] = {"partial": None}

    def on_partial(c: AudioChunk) -> None:
        latest["partial"] = c

    installed_windows = sys.platform == "win32" and getattr(ctx.config, "desktop_bridge", None) is not None
    requested, engine, reason = _pipeline_vad(cfg, installed_windows=installed_windows,
                                             heavy_disabled=ctx.config.disable_heavy_engines)
    known_vad = {"auto", "energy", "silero"}
    ctx.set_state(vad_requested=requested if requested in known_vad else "other",
                  vad_selected=engine if engine in known_vad else "energy", vad_selection_reason=reason)
    if reason == "windows_cpu_default":
        ctx.log("Windows CPU 음성 구간 감지는 에너지 방식을 사용합니다(추가 모델 없음)")
    seg = segmenter or VadSegmenter(
        end_silence_ms=int(cfg.get("vad_min_silence_ms") or 400),
        max_chunk_ms=int(float(cfg.get("max_segment_sec") or 15) * 1000),
        partial_every_ms=int(float(cfg.get("partial_every_sec") or 1.5) * 1000),
        engine=engine,
    )
    if partials and acoustic is None:
        seg.on_partial = on_partial
    elif acoustic is not None:
        seg.on_partial = None  # 가변 전체-VAD partial과 최종 화자별 key를 함께 저장하지 않는다.
    save_audio = bool(ctx.session.get("save_audio")) and int((ctx.settings("retention") or {}).get("audio_days") or 0) > 0
    audio_dir = Path(ctx.config.audio_dir) / ctx.sid

    finals: asyncio.Queue[tuple[AudioChunk, float]] = asyncio.Queue()
    work = asyncio.Event()
    finalized: set[int] = set()
    partial_text: dict[int, str] = {}
    stats = {"chunks": 0, "finals": 0, "partials": 0, "partials_skipped": 0, "stt_errors": 0, "failed": 0,
             "dropped": 0, "lost": 0, "audio_ms": 0, "vad": None}
    reader_done = False
    prompt_cache: dict[str, Any] = {"t": 0.0, "v": ""}
    inflight: dict[str, AudioChunk | None] = {"c": None}
    health = {"fails": 0, "lag_level": 0}
    acoustic_emitted_end = 0
    acoustic_previous = None
    fail_mark = LossMarker(ctx, "음성인식 실패", f"{key_prefix}fail")
    drop_mark = LossMarker(ctx, "음성인식 지연으로 건너뜀", f"{key_prefix}skip")

    def backlog() -> None:
        ctx.stt_backlog = finals.qsize() + (1 if inflight["c"] is not None else 0)

    def prompt() -> str:
        if time.monotonic() - prompt_cache["t"] > 60:
            prompt_cache["v"] = build_prompt(ctx, cfg)
            prompt_cache["t"] = time.monotonic()
        return prompt_cache["v"]

    text_proofs: dict[int, tuple[str, dict | None, bool]] = {}

    async def stt_text(c: AudioChunk) -> str:
        cpu = cfg.get("provider") == "nemotron_cpu"
        if cpu:
            # 최초 요청에는 소유 작업자의 준비도 포함된다. 음성 본문·경로는 상태에 넣지 않는다.
            ctx.set_state(stt_phase="cpu_asr_pending")
        try:
            segs = await stt.transcribe(c.pcm, SR, prompt=prompt())
        except BaseException:
            if cpu:
                ctx.set_state(stt_phase="cpu_asr_interrupted")
            raise
        if cpu:
            ctx.set_state(stt_phase="cpu_asr_ready")
        text = " ".join(s["text"].strip() for s in segs if s.get("text")).strip()
        if text:
            text_proofs[c.seg_id] = (text, safe_stt_provenance(segs), not c.final)
        return text

    def key_of(c: AudioChunk) -> str:
        return f"{key_prefix}{c.seg_id:05d}"

    def event(c: AudioChunk, text: str, final: bool, *, speakers=None, suffix="") -> SegmentEvent:
        nonlocal acoustic_previous
        proof = text_proofs.get(c.seg_id)
        metadata = {"stt_provenance": proof[1], "stt_input_was_partial": proof[2]} \
            if proof and proof[0] == text and proof[1] is not None else {}
        hint = None
        changed_speaker = False
        if acoustic is not None:
            speakers = speakers or ()
            hint = (f"hwa:nemo:{acoustic.scope}:{speakers[0].split('_')[-1]}" if len(speakers) == 1 else
                    "nemo:overlap" if len(speakers) > 1 else None)
            changed_speaker = acoustic_previous is not None and acoustic_previous != speakers
            acoustic_previous = speakers
            metadata["diarization"] = {**acoustic.metadata(speakers), "complete": bool(speakers)}
            if not speakers and not acoustic.failed:
                ctx.set_state(diarization_state="partial_boundary",
                              diarization_detail="화자 경계가 미정인 음성도 전사해 보존하고 있습니다")
            elif len(speakers) > 1:
                ctx.set_state(diarization_overlap=True)
        return SegmentEvent(key=key_of(c) + suffix, ts_start=base_ms_fn(c.start_ms_offset), ts_end=base_ms_fn(c.end_ms_offset),
                            turns=[Turn(text=text, speaker_hint=hint, source_speaker_hint=hint,
                                        new_speaker=changed_speaker)], is_final=final,
                            raw={"engine": "stt", "seg_id": c.seg_id, "offset_ms": c.start_ms_offset,
                                 "dur_ms": c.duration_ms, "partial": not final, **metadata})

    async def keep_wav(c: AudioChunk) -> None:
        if save_audio:
            try:
                await asyncio.to_thread(save_wav, audio_dir / f"{key_of(c)}.wav", c.pcm)
            except OSError as e:  # pragma: no cover - 디스크 부족 등
                ctx.log(f"원음 저장 실패: {e}", "warning")

    async def transcribe_final(c: AudioChunk) -> str | None:
        """확정 구간 전사(재시도 포함). 끝내 실패하면 실시간은 None, 파일은 SourceError."""
        delays = LIVE_RETRY_DELAYS if live else FILE_RETRY_DELAYS
        if live and health["fails"] >= LIVE_FAIL_WARN:
            delays = ()          # 엔진이 계속 실패 중이면 구간마다 한 번만 시도해 대기열이 쌓이지 않게
        attempt = 0
        while True:
            try:
                return await stt_text(c)
            except STTError as e:
                stats["stt_errors"] += 1
                attempt += 1
                ctx.log(f"음성인식 실패({attempt}회째): {e}", "warning")
                if attempt <= len(delays) and not (stop is not None and stop.is_set()):
                    await asyncio.sleep(delays[attempt - 1])
                    continue
                if not live:
                    raise SourceError(f"음성인식이 계속 실패합니다: {e}") from e
                return None

    def watch_lag(lag: float) -> None:
        if not live:
            return
        level = 0 if lag < BACKLOG_WARN_SEC else (1 if lag < BACKLOG_WARN_SEC * 5 else 2)
        if level > health["lag_level"]:
            ctx.log(f"음성인식이 실시간보다 늦습니다(지연 {lag:.0f}초, 대기 {finals.qsize()}건)", "warning")
            ctx.set_state(detail=f"음성인식 지연 {lag:.0f}초 · 대기 {finals.qsize()}건")
        elif level == 0 and health["lag_level"] > 0:
            ctx.log("음성인식 지연이 해소되었습니다")
        health["lag_level"] = level

    async def prepare_acoustic_boundary(c: AudioChunk) -> None:
        # c는 worker의 inflight에 남겨 둔다. 뒤 final을 앞뒤로 다시 넣지 않아 순서를 보존한다.
        end = c.start_ms_offset * SR // 1000 + len(c.pcm) // 2
        while True:
            if reader_done:
                await acoustic.finish()
                acoustic.boundary_ready(end)
                ctx.set_state(diarization_waiting_seg_id=None,
                    diarization_boundary_readiness=acoustic.boundary_readiness)
                return
            if not acoustic.allowed():
                await acoustic.advance()
            if acoustic.boundary_ready(end):
                await acoustic._reset()
                if not acoustic.failed:
                    acoustic.state = "buffered"
                ctx.set_state(diarization_state=acoustic.state, diarization_waiting_seg_id=None,
                    diarization_boundary_readiness=acoustic.boundary_readiness)
                return
            if acoustic.queue:
                await acoustic.advance()
                continue
            acoustic.state = "buffering"
            ctx.set_state(diarization_state="buffering", diarization_waiting_seg_id=c.seg_id,
                diarization_detail="화자 경계를 기다리며 음성은 계속 받고 있습니다. 약 30초가 추가로 걸릴 수 있습니다")
            work.clear()
            # 모델 준비/전사 await 사이 들어온 새 PCM이나 EOF는 기다리지 않고 처리한다.
            if acoustic.queue or reader_done:
                continue
            waking = asyncio.create_task(work.wait())
            stopping = asyncio.create_task(stop.wait()) if stop is not None else None
            waits = [waking] + ([stopping] if stopping is not None else [])
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*waits, return_exceptions=True)

    async def worker() -> None:
        nonlocal acoustic_emitted_end
        while True:
            if not finals.empty():
                c, detected = finals.get_nowait()
                if acoustic is not None and c.seg_id in finalized:
                    continue
                inflight["c"] = c
                backlog()
                ts0, ts1 = base_ms_fn(c.start_ms_offset), base_ms_fn(c.end_ms_offset)
                if live and time.monotonic() - detected > BACKLOG_DROP_SEC:
                    # 너무 밀린 구간: 실시간 기록을 따라잡도록 건너뛰고 표지·원음(저장 설정 시)으로 남긴다
                    stats["dropped"] += 1
                    if stats["dropped"] == 1 or stats["dropped"] % 20 == 0:
                        ctx.log(f"음성인식이 {BACKLOG_DROP_SEC:.0f}초 넘게 밀려 구간을 건너뜁니다(누적 {stats['dropped']}건)",
                                "warning")
                    await drop_mark.add(ts0, ts1)
                    finalized.add(c.seg_id)
                    partial_text.pop(c.seg_id, None)
                    text_proofs.pop(c.seg_id, None)
                    await keep_wav(c)
                    inflight["c"] = None
                    backlog()
                    if on_final_done is not None:
                        on_final_done(c)
                    continue
                drop_mark.close()
                pieces = [(c, None)]
                if acoustic is not None:
                    await prepare_acoustic_boundary(c)
                    # 앞 VAD의 pad와 겹친 샘플을 다시 전사하거나 native에 재투입하지 않는다.
                    start = c.start_ms_offset * SR // 1000
                    end = start + len(c.pcm) // 2
                    trim = min(len(c.pcm) // 2, max(0, acoustic_emitted_end - start))
                    clipped = AudioChunk((start + trim) * 1000 // SR, c.pcm[trim * 2:], c.seg_id)
                    pieces = acoustic.regions(clipped) if clipped.pcm else []
                    acoustic_emitted_end = max(acoustic_emitted_end, end)
                for index, (piece, speakers) in enumerate(pieces):
                    text = await transcribe_final(piece)
                    if text is None:
                        stats["failed"] += 1
                        health["fails"] += 1
                        text = partial_text.get(c.seg_id, "")
                        if not text:
                            await fail_mark.add(base_ms_fn(piece.start_ms_offset), base_ms_fn(piece.end_ms_offset))
                        if health["fails"] == LIVE_FAIL_WARN:
                            ctx.set_state(error="음성인식이 계속 실패합니다 — 엔진 상태를 확인하세요(수집은 계속됩니다)")
                    else:
                        if health["fails"]:
                            ctx.log(f"음성인식 복구(실패 구간 {health['fails']}건)")
                            if health["fails"] >= LIVE_FAIL_WARN:
                                ctx.set_state(error=None)
                        health["fails"] = 0
                        fail_mark.close()
                    text = text or partial_text.get(c.seg_id, "")
                    if text:
                        suffix = f"-d{index:03d}" if len(pieces) > 1 else ""
                        await emit(event(piece, text, True, speakers=speakers, suffix=suffix))
                        stats["finals"] += 1
                        ctx.set_state(latency_ms=int((time.monotonic() - detected) * 1000))
                finalized.add(c.seg_id)
                partial_text.pop(c.seg_id, None)
                text_proofs.pop(c.seg_id, None)
                await keep_wav(c)
                inflight["c"] = None
                backlog()
                if on_final_done is not None:
                    on_final_done(c)
                watch_lag(time.monotonic() - detected)
                continue
            if acoustic is not None and acoustic.queue:
                await acoustic.advance()
                continue
            p = latest["partial"]
            if p is not None:
                latest["partial"] = None
                if p.seg_id in finalized:
                    continue
                try:
                    text = await stt_text(p)
                except STTError:
                    text = ""
                # 전사하는 동안 확정이 들어왔으면 부분 결과는 버린다(확정이 곧 처리된다)
                if text and p.seg_id not in finalized and finals.empty():
                    partial_text[p.seg_id] = text
                    await emit(event(p, text, False))
                    stats["partials"] += 1
                else:
                    stats["partials_skipped"] += 1
                continue
            if reader_done:
                if acoustic is not None:
                    await acoustic.finish()
                return
            work.clear()
            await work.wait()

    async def put(chunks: list[AudioChunk], wt: asyncio.Task) -> None:
        now = time.monotonic()
        for c in chunks:
            while max_pending and finals.qsize() >= max_pending:
                if wt.done():
                    return
                await asyncio.sleep(0.05)
            finals.put_nowait((c, now))
            stats["chunks"] += 1
            # 새 확정이 생기면 대기 중이던 부분 스냅샷은 낡은 것이다
            p = latest["partial"]
            if p is not None and p.seg_id <= c.seg_id:
                latest["partial"] = None
                stats["partials_skipped"] += 1
            backlog()
            work.set()

    async def record_lost() -> None:
        """취소(정지 대기 시간 초과) 때 처리하지 못한 구간을 로그·기록 표지·원음(저장 설정 시)으로 남긴다."""
        lost: list[AudioChunk] = []
        if inflight["c"] is not None:
            lost.append(inflight["c"])
        while not finals.empty():
            lost.append(finals.get_nowait()[0])
        try:
            lost += seg.flush()
        except Exception:  # pragma: no cover - 방어
            pass
        if not lost:
            return
        stats["lost"] = len(lost)
        t0 = base_ms_fn(min(c.start_ms_offset for c in lost))
        t1 = base_ms_fn(max(c.end_ms_offset for c in lost))
        if not live:
            ctx.log(f"처리하지 못한 구간 {len(lost)}건 — 다시 시작하면 그 위치부터 처리합니다", "warning")
            return
        ctx.log(f"정지 대기 시간이 지나 전사하지 못한 구간 {len(lost)}건({hm(t0)}~{hm(t1)})", "warning")
        for c in lost:
            if save_audio:
                try:
                    save_wav(audio_dir / f"{key_of(c)}.wav", c.pcm)
                except OSError:  # pragma: no cover
                    pass
        try:
            await asyncio.wait_for(ctx.pipeline.add_marker(f"— 미전사 구간 {len(lost)}건({hm(t0)}~{hm(t1)}) —",
                                                           ts=t0, key=f"{key_prefix}lost{t0}"), 3)
        except BaseException:  # 다시 취소돼도 원래 예외를 올린다
            logger.warning("미전사 구간 표지를 남기지 못했습니다: %s", ctx.sid)

    wt = asyncio.create_task(worker(), name=f"stt-worker-{ctx.sid}")
    first_pcm = True
    try:
        async for pcm in pcm_iter:
            if wt.done():
                break
            if not pcm:
                # 빈 조각 = 불연속(재연결·끊김): 진행 중인 구간을 확정해 다음 오디오와 이어 붙이지 않는다
                await put(seg.flush(), wt)
                if acoustic is not None:
                    acoustic.degrade("degraded", "음성 수신이 끊겨 이전 화자 대응을 해제하고 전사는 계속합니다")
                    work.set()
                continue
            if acoustic is not None:
                acoustic.enqueue(pcm)
                work.set()
            stats["audio_ms"] += len(pcm) // BYTES_PER_MS
            if first_pcm:
                ctx.set_state(audio_pipeline_phase="first_pcm_received")
                ctx.log("첫 PCM 음성 조각을 받았습니다")
            if first_pcm and installed_windows and cfg.get("provider") == "nemotron_cpu" and engine == "energy":
                from .audio_diagnostics import cpu_vad_startup_trace
                with cpu_vad_startup_trace(ctx, enabled=True):
                    chunks = await asyncio.to_thread(seg.feed, pcm)
            else:
                chunks = await asyncio.to_thread(seg.feed, pcm)
            if first_pcm:
                active_vad = seg.engine if seg.engine in {"auto", "energy", "silero"} else "other"
                ctx.set_state(audio_pipeline_phase="vad_ready", vad_active=active_vad)
                ctx.log("음성 구간 감지 준비를 마쳤습니다")
                first_pcm = False
            stats["vad"] = seg.engine
            if chunks:
                await put(chunks, wt)
            if latest["partial"] is not None:
                if finals.qsize() > 0:
                    latest["partial"] = None          # 확정이 밀려 있으면 부분 전사 생략
                    stats["partials_skipped"] += 1
                else:
                    work.set()
        if not wt.done():
            await put(seg.flush(), wt)
        reader_done = True
        work.set()
        await wt          # 확정 대기열을 모두 처리(워커 오류는 여기서 올라온다)
    except BaseException:
        wt.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(wt, return_exceptions=True), 2)
        except BaseException:
            pass
        await record_lost()
        raise
    finally:
        ctx.stt_backlog = 0
        if acoustic is not None:
            await acoustic.close()
        if warm is not None and not warm.done():
            warm.cancel()
        aclose = getattr(pcm_iter, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # pragma: no cover - 방어
                pass
    if wt.done() and not wt.cancelled() and wt.exception() is not None:
        raise wt.exception()  # type: ignore[misc]
    return stats


async def transcribe_chunks(ctx: SourceContext, chunks: list[AudioChunk], emit: Emit, base_ms: int,
                            key_prefix: str) -> None:
    """(계약 1.2) 이미 잘린 구간들을 차례로 STT → SegmentEvent(is_final=True). 빈 결과는 건너뛴다."""
    prompt = build_prompt(ctx)
    for c in chunks:
        segs = await ctx.rt.stt.transcribe(c.pcm, SR, prompt=prompt)
        text = " ".join(s["text"].strip() for s in segs if s.get("text")).strip()
        if text:
            proof = safe_stt_provenance(segs)
            await emit(SegmentEvent(key=f"{key_prefix}{c.seg_id:05d}", ts_start=base_ms + c.start_ms_offset,
                                    ts_end=base_ms + c.end_ms_offset, turns=[Turn(text=text)], is_final=True,
                                    raw={"engine": "stt", "seg_id": c.seg_id, "offset_ms": c.start_ms_offset,
                                         **({"stt_provenance": proof, "stt_input_was_partial": not c.final} if proof else {})}))
