"""보관한 국회 원수신 JSONL을 네트워크 없이 기존 자막 처리 경로로 재생한다."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import time

from .base import SourceError, sleep_or_stop

MAX_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 512 * 1024
MAX_MESSAGES = 20_000
MAX_DURATION_MS = 6 * 60 * 60 * 1000


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("중복 JSON 필드")
        result[key] = value
    return result


def _json(value):
    def invalid(_):
        raise ValueError("유한하지 않은 JSON 값")
    return json.loads(value, object_pairs_hook=_pairs, parse_constant=invalid)


def replay_speed(value=1) -> float:
    if isinstance(value, bool):
        raise ValueError("재생 배속은 0.5~10 사이의 숫자여야 합니다")
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise ValueError("재생 배속은 0.5~10 사이의 숫자여야 합니다") from None
    if not math.isfinite(speed) or not .5 <= speed <= 10:
        raise ValueError("재생 배속은 0.5~10 사이의 숫자여야 합니다")
    return speed


def replay_path(data_dir: Path, value) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("먼저 보관한 국회 자막 JSONL 파일을 선택해 올리세요")
    root = (Path(data_dir) / "uploads").resolve()
    path = Path(value).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() != ".jsonl":
        raise ValueError("이 앱에 올린 국회 자막 JSONL 파일만 재생할 수 있습니다")
    return path


def load_recording(path: Path) -> list[tuple[int, str]]:
    """원본 문자열·순서는 유지한다. 일부 잘못된 행을 조용히 생략하지 않는다."""
    from ..live_stages import source_payload
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("저장 국회 자막 파일은 비어 있지 않은 16MB 이하 JSONL이어야 합니다")
    rows = []
    previous = None
    with path.open("rb") as stream:
        for line in stream:
            if not line.strip():
                continue
            if len(line) > MAX_LINE_BYTES or len(rows) >= MAX_MESSAGES:
                raise ValueError("저장 자막의 한 행 크기 또는 20,000건 한도를 넘었습니다")
            try:
                envelope = _json(line.decode("utf-8-sig"))
                stamp, wire = envelope["rx"], envelope["msg"]
                if type(stamp) is not int or not 0 <= stamp <= 2**53 or not isinstance(wire, str):
                    raise ValueError
                message = _json(wire)
                segment = message["segment"]
                if (isinstance(segment, bool) or not isinstance(segment, (int, str))
                        or not str(segment).isascii() or not str(segment).isdigit()
                        or len(str(segment)) > 12
                        or type(message.get("final")) is not bool or type(message.get("scd")) is not bool
                        or source_payload(message) is None):
                    raise ValueError
            except (KeyError, TypeError, ValueError, UnicodeError):
                raise ValueError(f"저장 자막 {len(rows)+1}번째 행의 수신 시각·문장 ID·JSON 형식이 잘못되었습니다") from None
            if previous is not None and stamp < previous:
                raise ValueError("저장 자막 수신 시각의 순서가 바뀌었습니다. 원본을 확인하세요")
            if rows and stamp - rows[0][0] > MAX_DURATION_MS:
                raise ValueError("저장 자막 한 파일의 재생 구간은 6시간 이하여야 합니다")
            rows.append((stamp, wire))
            previous = stamp
    if not rows:
        raise ValueError("저장 자막 JSONL에 재생할 메시지가 없습니다")
    return rows


async def run_recording(source, emit, stop: asyncio.Event) -> None:
    from .assembly import CaptionState, TurnAssembler
    try:
        path = replay_path(source.ctx.config.data_dir, source.config.get("caption_replay_file"))
        speed = replay_speed(source.config.get("caption_replay_speed", 1))
        rows = await asyncio.to_thread(load_recording, path)
    except (OSError, ValueError) as error:
        raise SourceError(str(error)) from None
    previous = source.ctx.prev_state
    position = previous.get("caption_replay_position", 0)
    if type(position) is not int or not 0 <= position <= len(rows):
        raise SourceError("저장 자막의 재개 위치를 확인하세요")
    if position == len(rows):
        source.ctx.set_state(phase="done", recording_replay=True,
            detail="저장 국회 자막 재생 완료 · 실방송 아님", caption_replay_position=position)
        return
    keys, texts = source._prior_state()
    source.state = CaptionState(prior_keys=keys, prior_texts=texts)
    source.turns = TurnAssembler()
    source._emit_turn, source._emit = emit, source._feed_turns
    source._open_raw()
    source.ctx.set_state(phase="connected", error=None, recording_replay=True,
        caption_replay_position=position, detail=f"저장 국회 자막 재생 · 실방송 아님 · {position}/{len(rows)}건 · {speed:g}배속")
    source.ctx.log(f"저장 국회 자막 재생 시작 · 실방송 아님 · {speed:g}배속")
    began, first_rx = time.monotonic(), rows[position][0]
    try:
        for index in range(position, len(rows)):
            rx, wire = rows[index]
            wait = (rx - first_rx) / 1000 / speed - (time.monotonic() - began)
            if stop.is_set() or wait > 0 and await sleep_or_stop(stop, wait):
                break
            await source.on_raw(wire, source._feed_turns)
            source.ctx.set_state(caption_replay_position=index+1, progress=round((index+1)/len(rows), 4),
                recording_replay=True, detail=f"저장 국회 자막 재생 · 실방송 아님 · {index+1}/{len(rows)}건")
            await source._finalize_due()
            await asyncio.sleep(0)  # 같은 시각의 메시지가 밀려도 화면·정지 요청에 양보한다.
        for event in source.state.take_all_pending():
            await source._feed_turns(event)
        for event in source.turns.flush():
            await emit(event)
        if not stop.is_set():
            source.ctx.set_state(phase="done", recording_replay=True,
                caption_replay_position=len(rows), progress=1.0, detail="저장 국회 자막 재생 완료 · 실방송 아님")
            source.ctx.log("저장 국회 자막 재생 완료 · 실방송 아님")
    finally:
        if source._raw_fp is not None:
            source._raw_fp.close()
            source._raw_fp = None
