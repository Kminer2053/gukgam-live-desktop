"""리플레이(시연용) 수집원: 'HH:MM:SS|화자|본문' 픽스처를 speed 배속으로 재생한다.

국회 AI자막처럼 보이도록 각 줄을 부분 결과 2~3번(글자 수를 늘려 가며) 보낸 뒤 확정한다.
시각은 '지금' 기준으로 재배치하고 줄 사이 간격은 원본/speed. with_hints 면 화자 힌트(픽스처 2열)를 같이 보낸다.
일시정지 후 다시 시작하면 이어서, 끝까지 재생한 뒤 다시 시작하면 처음부터 새 key 로 재생한다.
source_config: fixture(기본 tests/fixtures/gukto_20261007_tail.txt), speed(기본 20), with_hints(bool), turn_marks(bool)
"""
from __future__ import annotations

import asyncio
import logging
import math

from ..db import now_ms
from ..models import SegmentEvent, Turn
from .base import Emit, Source, SourceError, resolve_path, sleep_or_stop

logger = logging.getLogger(__name__)

DEFAULT_FIXTURE = "tests/fixtures/gukto_20261007_tail.txt"


def load_fixture(text: str) -> list[tuple[int, str, str]]:
    """[(원본 경과초, 화자, 본문)]. 자정을 넘기면 하루를 더한다. 첫 줄 = 0초."""
    rows: list[tuple[int, str, str]] = []
    prev = None
    day = 0
    for ln in text.splitlines():
        if not ln.strip():
            continue
        parts = ln.rstrip("\n").split("|", 2)
        if len(parts) != 3:
            continue
        t, spk, body = parts
        try:
            h, m, s = map(int, t.strip().split(":"))
        except ValueError:
            continue
        sec = h * 3600 + m * 60 + s + day
        if prev is not None and sec < prev:
            day += 86400
            sec += 86400
        prev = sec
        rows.append((sec, spk.strip(), body.strip()))
    if rows:
        t0 = rows[0][0]
        rows = [(t - t0, s, b) for t, s, b in rows]
    return rows


def _prefix(text: str, frac: float) -> str:
    """앞에서부터 frac 비율만큼(단어 경계) 자른 부분 결과."""
    words = text.split()
    n = max(1, math.ceil(len(words) * frac))
    return " ".join(words[:n])


class ReplaySource(Source):
    kind = "replay"

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        path = resolve_path(self.ctx, str(self.config.get("fixture") or DEFAULT_FIXTURE))
        if not path.is_file():
            raise SourceError(f"리플레이 픽스처를 찾을 수 없습니다: {path}")
        rows = load_fixture(path.read_text(encoding="utf-8"))
        if not rows:
            raise SourceError("리플레이 픽스처가 비어 있습니다")
        speed = max(0.1, float(self.config.get("speed") or 20))
        with_hints = bool(self.config.get("with_hints"))
        turn_marks = bool(self.config.get("turn_marks"))
        prev = self.ctx.prev_state
        pass_no = int(prev.get("replay_pass") or 0)
        pos = int(prev.get("position") or 0)
        if pos >= len(rows) or (prev.get("fixture") and prev.get("fixture") != str(path)):
            pass_no += 1           # 처음부터 다시 — 이전 재생과 key 가 겹치지 않게
            pos = 0
        n = len(rows)
        # 재개 위치의 줄이 '지금' 시작하도록 기준을 잡는다
        t0 = now_ms() - int(rows[pos][0] * 1000 / speed)
        self.ctx.set_state(phase="connected", error=None, fixture=str(path), replay_pass=pass_no, position=pos,
                           detail=f"리플레이 {speed:g}배속 · {pos}/{n}줄")
        self.ctx.log(f"리플레이 시작: {path.name} {speed:g}배속" + (f" ({pos}줄부터)" if pos else ""))
        for i in range(pos, n):
            sec, spk, body = rows[i]
            target = t0 + int(sec * 1000 / speed)
            wait = (target - now_ms()) / 1000
            if wait > 0 and await sleep_or_stop(stop, wait):
                return
            slot = ((rows[i + 1][0] - sec) if i + 1 < n else 5) / speed      # 이 줄에 쓸 수 있는 시간(초)
            key = f"rp{pass_no}-{i:04d}"
            hint = (spk or None) if with_hints else None
            new_speaker = turn_marks and i > 0 and spk != rows[i - 1][1]
            ts = now_ms()
            nparts = 2 if len(body.split()) < 12 else 3
            step = min(slot * 0.6 / (nparts + 1), 1.2)
            stopped = False
            for k in range(1, nparts + 1):
                text = _prefix(body, k / (nparts + 1))
                await emit(SegmentEvent(key=key, ts_start=ts, ts_end=now_ms(), is_final=False,
                                        turns=[Turn(text=text, speaker_hint=hint, new_speaker=new_speaker)],
                                        raw={"replay": True, "line": i, "partial": k}))
                if await sleep_or_stop(stop, step):
                    stopped = True
                    break
            ts_end = max(now_ms(), ts + int(slot * 1000 * 0.9))
            await emit(SegmentEvent(key=key, ts_start=ts, ts_end=ts_end, is_final=True,
                                    turns=[Turn(text=body, speaker_hint=hint, new_speaker=new_speaker)],
                                    raw={"replay": True, "line": i}))
            self.ctx.mark_rx(position=i + 1, progress=round((i + 1) / n, 4),
                             detail=f"리플레이 {speed:g}배속 · {i + 1}/{n}줄")
            if stopped or stop.is_set():
                return
        self.ctx.set_state(phase="done", position=n, progress=1.0, detail=f"리플레이 완료({n}줄)")
        self.ctx.log("리플레이 완료")
