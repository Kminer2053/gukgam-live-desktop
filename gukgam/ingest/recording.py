"""녹음 전체의 음성 화자 경계를 먼저 구한 뒤 각 구간을 CPU로 전사한다."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
import tempfile
import wave

from ..models import SegmentEvent, Turn
from .audio import AudioChunk
from .base import SourceError

SR = 16_000


def speaker_regions(segments: list[dict], samples: int) -> list[tuple[int, int, tuple[str, ...]]]:
    """겹친 발언은 한 사람으로 단정하지 않고 원래 음성 위치에서 한 번만 전사한다."""
    changes: dict[int, list[tuple[str, int]]] = {0: [], samples: []}
    for item in segments:
        start, end, speaker = item.get("start"), item.get("end"), item.get("speaker")
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end <= samples / SR + .02
                or speaker not in {f"speaker_{n}" for n in range(8)}):
            raise SourceError("화자 분리 결과의 시간 또는 화자 번호를 확인하지 못했습니다")
        a, b = max(0, round(start * SR)), min(samples, round(end * SR))
        if a >= b:
            continue
        changes.setdefault(a, []).append((speaker, 1))
        changes.setdefault(b, []).append((speaker, -1))
    active: dict[str, int] = {}
    regions: list[tuple[int, int, tuple[str, ...]]] = []
    previous = 0
    for position, events in sorted(changes.items()):
        speakers = tuple(sorted(key for key, count in active.items() if count > 0))
        if position > previous:
            if regions and regions[-1][1] == previous and regions[-1][2] == speakers:
                regions[-1] = (regions[-1][0], position, speakers)
            else:
                regions.append((previous, position, speakers))
        for speaker, delta in events:
            active[speaker] = active.get(speaker, 0) + delta
        previous = position
    joined: list[tuple[int, int, tuple[str, ...]]] = []
    for start, end, speakers in regions:
        if joined and joined[-1][2] == speakers and start - joined[-1][1] <= SR * .4:
            joined[-1] = (joined[-1][0], end, speakers)
        else:
            joined.append((start, end, speakers))
    return joined


async def _until_stopped(awaitable, stop: asyncio.Event):
    task = asyncio.ensure_future(awaitable)
    stopped = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((task, stopped), return_when=asyncio.FIRST_COMPLETED)
        if stopped in done and stop.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise asyncio.CancelledError
        return await task
    finally:
        stopped.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, stopped, return_exceptions=True)


async def run_diarized_recording(ctx, pcm_stream, emit, base_ms: int, *, stop: asyncio.Event,
                                 resume_ms: int = 0, on_final_done=None, diarizer_factory=None) -> dict:
    """익명 음성 화자 ID를 전체 녹음에서 유지한다. 이름·역할은 기존 문맥 판정과 구분한다."""
    from ..stt.nemotron_diarization import NemotronDiarizationCPU
    from ..stt.nemotron_cpu import effective_cpu_cores

    cfg = ctx.settings("stt")
    if diarizer_factory is None:
        diarizer_factory = NemotronDiarizationCPU
    diarizer = diarizer_factory(ctx.config.data_dir, cpu_cores=effective_cpu_cores(cfg), preset="v3-offline")
    tmp_root = Path(ctx.config.data_dir) / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    stats = {"chunks": 0, "finals": 0, "overlap_regions": 0, "speakers": 0}
    try:
        try:
            diarizer.check()
        except Exception:
            raise SourceError("녹음 화자 분리 모델이 준비되지 않았습니다. 설정에서 CPU 전사·화자 분리 모델을 준비하세요") from None
        with tempfile.TemporaryDirectory(prefix="recording-diar-", dir=tmp_root) as folder:
            wav_path = Path(folder) / "recording.wav"
            samples = 0
            pending_byte = b""
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(SR)
                async for pcm in pcm_stream:
                    if stop.is_set():
                        return stats
                    block = pending_byte + pcm
                    size = len(block) // 2 * 2
                    output.writeframesraw(block[:size])
                    pending_byte = block[size:]
                    samples += size // 2
            if pending_byte:
                raise SourceError("녹음 음성의 마지막 샘플이 잘렸습니다")
            if not samples or stop.is_set():
                return stats
            ctx.set_state(audio_pipeline_phase="diarizing", detail="녹음 전체의 목소리별 발언 구간을 확인하고 있습니다")
            result = await _until_stopped(diarizer.diarize_wav(wav_path, request_guard=lambda: not stop.is_set()), stop)
            regions = speaker_regions(result["segments"], samples)
            stats["speakers"] = len({s for _, _, speakers in regions for s in speakers})
            ctx.set_state(audio_pipeline_phase="transcribing_speakers", acoustic_speaker_count=stats["speakers"],
                          acoustic_speaker_limit_reached=stats["speakers"] == 8,
                          detail="목소리로 나눈 발언을 전사하고 있습니다")
            previous_speakers = None
            with wave.open(str(wav_path), "rb") as source:
                for start, end, speakers in regions:
                    for offset in range(start, end, SR * 28):
                        last = min(end, offset + SR * 28)
                        if last * 1000 // SR <= resume_ms:
                            previous_speakers = speakers
                            continue
                        if stop.is_set():
                            return stats
                        source.setpos(offset)
                        pcm = source.readframes(last - offset)
                        # 화자 모델이 놓친 구간도 전사한다. 정확한 디지털 무음만 건너뛴다.
                        rows = [] if not any(pcm) else await _until_stopped(
                            ctx.rt.stt.transcribe(pcm, SR, request_guard=lambda: not stop.is_set()), stop)
                        text = " ".join(row["text"].strip() for row in rows if row.get("text")).strip()
                        stats["chunks"] += 1
                        overlap = len(speakers) > 1
                        if overlap:
                            stats["overlap_regions"] += 1
                        hint = (f"hwa:nemo:{speakers[0].split('_')[-1]}" if len(speakers) == 1
                                else "nemo:overlap" if overlap else "nemo:unknown")
                        if text:
                            event = SegmentEvent(key=f"nd-{offset}-{last}", ts_start=base_ms + offset * 1000 // SR,
                                ts_end=base_ms + last * 1000 // SR, is_final=True,
                                turns=[Turn(text=text, speaker_hint=hint if speakers else None, source_speaker_hint=hint,
                                            new_speaker=previous_speakers is not None and previous_speakers != speakers)],
                                raw={"engine": "stt", "diarization": {"speakers": list(speakers), "overlap": overlap,
                                     "complete": bool(speakers),
                                     "provenance": result["provenance"]}, "offset_ms": offset * 1000 // SR,
                                     "dur_ms": (last - offset) * 1000 // SR,
                                     "stt_provenance": rows[0].get("stt_provenance") if rows else None})
                            await emit(event)
                            stats["finals"] += 1
                        previous_speakers = speakers
                        if on_final_done is not None:
                            chunk = AudioChunk(start_ms_offset=offset * 1000 // SR, pcm=pcm, final=True)
                            on_final_done(chunk)
                        ctx.set_state(diarization_overlap_regions=stats["overlap_regions"],
                                      transcription_position_ms=last * 1000 // SR)
            return stats
    except asyncio.CancelledError:
        if not stop.is_set():
            raise
        return stats
    finally:
        await diarizer.close()
