"""음성인식 서비스. 엔진은 settings['stt'] (mlx_whisper | nemotron_cpu | none).

mlx_whisper: `python -m gukgam.stt.mlx_worker --model <경로|repo>` 서브프로세스(JSON lines)를 지연 기동해
요청을 하나씩(asyncio.Lock) 보낸다. 워커가 죽으면 다음 요청에서 다시 띄우고(한 번 재시도), 60초 안에 답이 없으면
워커를 죽이고 STTError. 모델은 로컬 경로(프로젝트 ROOT 기준 상대경로 허용)를 쓰고 HF_HUB_OFFLINE=1 로 띄운다.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import sys
import time
import wave
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .. import settings
from ..config import ROOT

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60.0
LOAD_TIMEOUT = 180.0

# Whisper 가 무음·잡음에서 지어내는 대표 문구(문장 전체가 이것뿐이면 버린다)
HALLUCINATIONS = re.compile(
    r"^(시청해\s*주셔서\s*감사합니다|구독과\s*좋아요.*|MBC\s*뉴스.*|KBS\s*뉴스.*|자막\s*(제공|편집).*|"
    r"다음\s*영상에서\s*만나요|감사합니다\s*감사합니다.*)[.!\s]*$"
)


class STTError(RuntimeError):
    pass


def resolve_model(model: str) -> tuple[str, bool]:
    """(worker 에 넘길 경로/ID, 로컬 경로 여부). 상대경로는 프로젝트 ROOT 기준."""
    m = (model or "").strip()
    if not m:
        raise STTError("음성인식 모델이 설정되지 않았습니다(설정 > STT > model)")
    p = Path(m).expanduser()
    cand = p if p.is_absolute() else ROOT / p
    if cand.exists():
        return str(cand), True
    if p.is_absolute() or m.startswith((".", "models/", "models" + os.sep)):
        raise STTError(f"음성인식 모델 폴더가 없습니다: {cand}")
    return m, False          # HF repo id — 오프라인 캐시에 있어야 한다


def apply_corrections(text: str, corrections: dict[str, str] | None) -> str:
    for src, dst in (corrections or {}).items():
        if src:
            text = text.replace(src, dst)
    return text


class STTService:
    def __init__(self, rt: Any):
        self.rt = rt
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_model: str | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=30)
        self._ids = itertools.count(1)
        self._loaded = False
        self.last_latency_ms: int | None = None
        self.last_error: str | None = None
        self.requests = 0
        # 테스트·진단용: 워커 명령을 바꿔 끼울 수 있다({model} 자리표시자 사용)
        self.worker_argv: list[str] | None = None
        self.request_timeout = REQUEST_TIMEOUT
        self.load_timeout = LOAD_TIMEOUT
        self._nemotron = None
        self._nemotron_key = None
        self.last_provenance: dict | None = None
        # 비정상 종료로 남은 이전 프로세스의 임시파일 정리(정상 경로에서는 요청마다 지운다)
        try:
            from ..maintenance import purge_stt_tmp

            n = purge_stt_tmp(rt.config)
            if n:
                logger.info("이전 실행의 음성인식 임시파일 %d개를 지웠습니다", n)
        except Exception:  # pragma: no cover - 방어
            logger.exception("음성인식 임시파일 정리 실패")

    # ── 설정 ──────────────────────────────────────────────────
    def _cfg(self) -> dict:
        return settings.get(self.rt.repo, "stt")

    def _tmp_dir(self) -> Path:
        d = Path(self.rt.config.data_dir) / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _check_enabled(self, cfg: dict) -> None:
        if self.rt.config.disable_heavy_engines:
            raise STTError("음성인식 엔진이 비활성화되어 있습니다(테스트 모드)")
        if (cfg.get("provider") or "none") == "none":
            raise STTError("음성인식이 꺼져 있습니다(설정 > STT)")
        if cfg.get("provider") not in {"mlx_whisper", "nemotron_cpu"}:
            raise STTError(f"지원하지 않는 음성인식 엔진입니다: {cfg.get('provider')}")

    @staticmethod
    def _nemotron_config_key(cfg):
        from .nemotron_cpu import effective_cpu_cores
        return (cfg.get("provider"), effective_cpu_cores(cfg),
                cfg.get("nemotron_load_timeout_seconds", 180), cfg.get("nemotron_request_timeout_seconds", 60))

    def _check_nemotron_files(self):
        from .nemotron_assets import status
        state = status(self.rt.config.data_dir)
        task = getattr(self.rt, "stt_model_prepare_task", None)
        if state["state"] == "preparing" or task is not None and not task.done():
            raise STTError("CPU 음성 모델을 준비 중입니다. 준비를 마친 뒤 전사하세요")
        if not state["files_present"]:
            raise STTError("설정에서 공식 CPU 런타임과 한국어 음성 모델을 먼저 준비하세요")

    async def _nemotron_for(self, cfg):
        from .nemotron_cpu import NemotronCPU
        key = self._nemotron_config_key(cfg)
        if self._nemotron is not None and self._nemotron_key != key:
            await self._nemotron.close()
            self._nemotron = None
        if self._nemotron is None:
            self._nemotron = NemotronCPU(self.rt.config.data_dir, cpu_cores=key[1],
                load_timeout=key[2], request_timeout=key[3])
            self._nemotron_key = key
        return self._nemotron

    def default_prompt(self, cfg: dict | None = None) -> str:
        cfg = cfg or self._cfg()
        gl = [g for g in (cfg.get("glossary") or []) if isinstance(g, str) and g.strip()]
        parts = [", ".join(gl)] if gl else []
        parts.append(cfg.get("prompt") or "")
        return " ".join(p for p in parts if p).strip()

    # ── 공개 API ──────────────────────────────────────────────
    def check(self) -> None:
        """설정 점검만(워커를 띄우지 않음). 쓸 수 없으면 STTError — 수집 시작 시 바로 알리기 위해."""
        cfg = self._cfg()
        self._check_enabled(cfg)
        if cfg.get("provider") == "nemotron_cpu":
            self._check_nemotron_files()
            return
        resolve_model(cfg.get("model") or "")

    async def warmup(self) -> None:
        """워커를 미리 띄워 모델을 올려 둔다(첫 발화 지연 방지)."""
        cfg = self._cfg()
        self._check_enabled(cfg)
        if cfg.get("provider") == "nemotron_cpu":
            # 최초 실제 전사가 모델을 올린다. 단순 준비/설정/점검은 추론을 실행하지 않는다.
            self._check_nemotron_files()
            return
        model, _local = resolve_model(cfg.get("model") or "")
        async with self._lock:
            await self._ensure_worker(model)

    async def transcribe(self, pcm16: bytes, sample_rate: int = 16000, prompt: str | None = None,
                         *, apply_text_corrections: bool = True,
                         request_guard: Callable[[], bool] | None = None) -> list[dict]:
        """[{'start': 초, 'end': 초, 'text': str}]. 실패 시 STTError."""
        cfg = self._cfg()
        self._check_enabled(cfg)
        if not pcm16:
            return []
        if cfg.get("provider") == "nemotron_cpu":
            if self._lock.locked():
                raise STTError("음성 전사 한 건을 처리 중입니다. 완료한 뒤 다시 요청하세요")
            async with self._lock:
                if request_guard is not None and not request_guard():
                    raise STTError("전사 실행 전에 입력 또는 허용 상태가 바뀌었습니다")
                self._check_nemotron_files()
                self._check_enabled(self._cfg())
                key = self._nemotron_config_key(cfg)
                if self._nemotron_config_key(self._cfg()) != key:
                    raise STTError("전사 실행 전에 음성 모델 설정이 바뀌었습니다")
                await self._kill()  # 이전 MLX 자식이 남아도 두 모델을 동시에 실행하지 않는다.
                self._check_nemotron_files()
                native = await self._nemotron_for(cfg)
                def allowed():
                    from .nemotron_assets import status
                    task = getattr(self.rt, "stt_model_prepare_task", None)
                    return (not self.rt.config.disable_heavy_engines and self._nemotron_config_key(self._cfg()) == key
                        and status(self.rt.config.data_dir)["state"] != "preparing"
                        and (task is None or task.done()) and (request_guard is None or request_guard()))
                began = time.perf_counter()
                try:
                    result = await native.transcribe(pcm16, sample_rate, request_guard=allowed)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.last_error = "CPU 한국어 음성 전사를 완료하지 못했습니다. 입력 길이와 모델 준비 상태를 확인하세요"
                    raise STTError(self.last_error) from None
                self.last_latency_ms = int((time.perf_counter() - began) * 1000)
                self.last_provenance = {**result["provenance"], "audio_sha256": result["audio_sha256"]}
                self.last_error = None
                self.requests += 1
                # Whisper 전용 환각·compression_ratio·initial_prompt를 이 모델에 적용하지 않는다.
                return [{**row, "stt_provenance": dict(self.last_provenance)} for row in result["segments"]]
        if self._nemotron is not None:
            await self._nemotron.close()
            self._nemotron = None
            self._nemotron_key = None
        model, _local = resolve_model(cfg.get("model") or "")
        async with self._lock:
            if request_guard is not None and not request_guard():
                raise STTError("전사 대기 중 입력 또는 허용 상태가 변경되었습니다")
            self._check_enabled(self._cfg())
            path = self._write_audio(pcm16, sample_rate)
            try:
                req = {"language": cfg.get("language") or "ko",
                       "prompt": prompt if prompt is not None else self.default_prompt(cfg)}
                req["pcm_path" if sample_rate == 16000 else "wav_path"] = str(path)
                t0 = time.perf_counter()
                for attempt in (1, 2):
                    try:
                        if request_guard is not None and not request_guard():
                            raise STTError("전사 실행 전 입력 또는 허용 상태가 변경되었습니다")
                        await self._ensure_worker(model)
                        if request_guard is not None and not request_guard():
                            raise STTError("모델 준비 중 입력 또는 허용 상태가 변경되었습니다")
                        resp = await self._request(req)
                        if request_guard is not None and not request_guard():
                            raise STTError("전사 응답 중 입력 또는 허용 상태가 변경되었습니다")
                        break
                    except _WorkerDied as e:
                        await self._kill()
                        if attempt == 2:
                            raise STTError(f"음성인식 엔진이 종료되었습니다: {e}") from e
                        logger.warning("STT 워커 종료 감지 — 다시 띄웁니다: %s", e)
                self.last_latency_ms = int((time.perf_counter() - t0) * 1000)
                self.requests += 1
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass
        if resp.get("error"):
            self.last_error = str(resp["error"])
            raise STTError(f"음성인식 실패: {resp['error']}")
        self.last_error = None
        out = []
        corrections = (cfg.get("corrections") or {}) if apply_text_corrections else {}
        for s in resp.get("segments") or []:
            text = apply_corrections(str(s.get("text") or "").strip(), corrections)
            if not text or HALLUCINATIONS.match(text):
                continue
            if float(s.get("compression_ratio") or 0) > 2.6:      # 반복 환각
                continue
            out.append({"start": float(s.get("start") or 0.0), "end": float(s.get("end") or 0.0), "text": text})
        return out

    async def health(self) -> dict:
        cfg = self._cfg()
        provider = cfg.get("provider") or "none"
        native_state = self._nemotron.state if self._nemotron is not None else "idle"
        native_proc = getattr(self._nemotron, "_proc", None)
        native_busy = bool(self._nemotron is not None and self._nemotron._busy)
        native_running = native_busy or native_proc is not None and native_proc.returncode is None and native_state not in {"ready"}
        native_loaded = native_state in {"ready", "running"}
        if provider == "nemotron_cpu":
            from .nemotron_cpu import MODEL_REPO
            from .nemotron_assets import status
            prepared = status(self.rt.config.data_dir)
            task = getattr(self.rt, "stt_model_prepare_task", None)
            preparing = prepared["state"] == "preparing" or task is not None and not task.done()
            worker_state = self._nemotron.state if self._nemotron is not None else "idle"
            enabled = not self.rt.config.disable_heavy_engines
            ready_files = prepared["files_present"] and not preparing
            return {**prepared, "ok": bool(enabled and ready_files and self.last_error is None),
                "provider": provider, "model": MODEL_REPO, "loaded": worker_state in {"ready", "running"},
                "running": native_running or worker_state in {"loading", "running"}, "worker_state": worker_state,
                "cpu_core_limit": self._nemotron_config_key(cfg)[1],
                "cpu_core_policy": "auto" if cfg.get("nemotron_cpu_auto") is True else "manual",
                "last_latency_ms": self.last_latency_ms,
                "provenance": self.last_provenance,
                "detail": "CPU 음성 모델을 준비 중입니다" if preparing else self.last_error or (
                    prepared["message"] if enabled else "음성인식 엔진이 비활성화되어 있습니다")}
        out: dict[str, Any] = {"ok": False, "provider": provider, "model": cfg.get("model") or "",
                               "loaded": self._alive() and self._loaded, "last_latency_ms": self.last_latency_ms}
        if self._nemotron is not None:
            # 선택을 껐어도 소유 모델이 남아 있으면 준비 API가 교체를 시작하지 않게 한다.
            out.update(worker_state=native_state, nemotron_loaded=native_loaded, running=native_running,
                       loaded=out["loaded"] or native_loaded)
        try:
            self._check_enabled(cfg)
            resolve_model(cfg.get("model") or "")
            out["ok"] = self.last_error is None
            if self.last_error:
                out["detail"] = self.last_error
        except STTError as e:
            out["detail"] = str(e)
        return out

    async def shutdown(self) -> None:
        if self._nemotron is not None:
            await self._nemotron.close()
            self._nemotron = None
            self._nemotron_key = None
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.returncode is None and proc.stdin is not None:
                proc.stdin.write(b'{"cmd": "quit"}\n')
                await proc.stdin.drain()
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=3)
        except (asyncio.TimeoutError, OSError, ConnectionError):
            pass
        await self._kill()

    # ── 워커 관리 ─────────────────────────────────────────────
    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _ensure_worker(self, model: str) -> None:
        if self._alive() and self._proc_model == model:
            return
        await self._kill()
        argv = ([a.replace("{model}", model) for a in self.worker_argv] if self.worker_argv else
                [sys.executable, "-m", "gukgam.stt.mlx_worker", "--model", model])
        env = dict(os.environ)
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
        env["PYTHONUNBUFFERED"] = "1"
        # JSONL 파이프는 부모의 Windows 코드 페이지와 무관하게 UTF-8로 주고받는다.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        logger.info("STT 워커 기동: %s", " ".join(argv))
        self._stderr_tail.clear()
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(ROOT), env=env, limit=2**22)
        self._proc_model = model
        self._loaded = False
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc), name="stt-stderr")
        deadline = time.monotonic() + self.load_timeout
        msg: dict = {}
        while "ready" not in msg:
            try:
                line = await asyncio.wait_for(self._proc.stdout.readline(),  # type: ignore[union-attr]
                                              timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError as e:
                await self._kill()
                raise STTError(f"음성인식 모델 로딩 시간 초과({self.load_timeout:.0f}초)") from e
            if not line:
                await asyncio.sleep(0.2)
                tail = " / ".join(list(self._stderr_tail)[-3:])
                await self._kill()
                raise STTError(f"음성인식 엔진을 시작하지 못했습니다: {tail or '원인 불명'}")
            try:
                parsed = json.loads(line)
            except ValueError:
                logger.debug("STT 워커 비JSON 출력: %r", line[:200])
                continue
            msg = parsed if isinstance(parsed, dict) else {}
        if not msg.get("ready"):
            await self._kill()
            raise STTError(f"음성인식 모델을 불러오지 못했습니다: {msg.get('error')}")
        self._loaded = True
        logger.info("STT 워커 준비 완료(%.1f초): %s", float(msg.get("load_sec") or 0), model)

    async def _request(self, req: dict) -> dict:
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        rid = next(self._ids)
        try:
            proc.stdin.write((json.dumps({"id": rid, **req}, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as e:
            raise _WorkerDied(str(e)) from e
        deadline = time.monotonic() + self.request_timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                await self._kill()
                raise STTError(f"음성인식 응답 시간 초과({self.request_timeout:.0f}초) — 엔진을 다시 시작합니다")
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=left)
            except asyncio.TimeoutError:
                continue
            if not line:
                tail = " / ".join(list(self._stderr_tail)[-3:])
                raise _WorkerDied(tail or "출력 종료")
            try:
                msg = json.loads(line)
            except ValueError:
                logger.debug("STT 워커 비JSON 출력: %r", line[:200])
                continue
            if msg.get("ready"):          # 워밍업이 중간에 취소됐던 워커의 준비 완료 줄
                self._loaded = True
                continue
            if msg.get("id") == rid:
                self._loaded = True
                return msg

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        try:
            async for raw in proc.stderr:
                s = raw.decode("utf-8", "replace").rstrip()
                if s:
                    self._stderr_tail.append(s[:300])
                    logger.debug("[stt-worker] %s", s)
        except Exception:  # pragma: no cover - 방어
            pass

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._loaded = False
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None

    def _write_audio(self, pcm16: bytes, sample_rate: int) -> Path:
        n = next(self._ids)
        if sample_rate == 16000:
            path = self._tmp_dir() / f"stt-{os.getpid()}-{n}.pcm"
            path.write_bytes(pcm16)
        else:
            path = self._tmp_dir() / f"stt-{os.getpid()}-{n}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(pcm16)
        return path


class _WorkerDied(Exception):
    pass
