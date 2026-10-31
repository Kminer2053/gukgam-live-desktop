"""mlx-whisper 음성인식 서브프로세스(stdin/stdout JSON lines). 포트를 쓰지 않는다.

실행: python -m gukgam.stt.mlx_worker --model <로컬 경로|HF repo>
  기동 → 모델 로드·워밍업 → {"ready": true, "model", "load_sec"} 출력
요청: {"id", "pcm_path"(16kHz mono s16le) 또는 "wav_path", "language", "prompt"}
응답: {"id", "segments": [{start, end, text, no_speech_prob, avg_logprob, compression_ratio}], "text", "compute_sec"}
      실패 시 {"id", "error"}.  {"cmd": "ping"} → {"pong": true}
라이브러리가 stdout 에 찍는 내용이 프로토콜을 깨지 않도록 stdout 은 stderr 로 돌리고, 응답만 원래 stdout 에 쓴다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

SR = 16000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--no-warmup", action="store_true")
    a = ap.parse_args()

    out = sys.stdout
    sys.stdout = sys.stderr

    def reply(obj: dict) -> None:
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        out.flush()

    t0 = time.perf_counter()
    try:
        import numpy as np
        import mlx_whisper
        from mlx_whisper.audio import load_audio

        if not a.no_warmup:
            mlx_whisper.transcribe(np.zeros(SR, dtype=np.float32), path_or_hf_repo=a.model, language="ko",
                                   verbose=None, condition_on_previous_text=False, temperature=0.0)
    except Exception as e:  # 모델 없음·의존성 문제
        reply({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return
    reply({"ready": True, "model": a.model, "load_sec": round(time.perf_counter() - t0, 2), "pid": os.getpid()})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            reply({"error": "잘못된 요청(JSON 아님)"})
            continue
        if req.get("cmd") == "ping":
            reply({"id": req.get("id"), "pong": True})
            continue
        if req.get("cmd") == "quit":
            break
        rid = req.get("id")
        try:
            if req.get("pcm_path"):
                audio = np.fromfile(req["pcm_path"], dtype=np.int16).astype(np.float32) / 32768.0
            elif req.get("wav_path"):
                audio = load_audio(req["wav_path"])
            else:
                raise ValueError("pcm_path 또는 wav_path 가 필요합니다")
            if len(audio) < SR // 10:
                reply({"id": rid, "segments": [], "text": "", "compute_sec": 0.0})
                continue
            t1 = time.perf_counter()
            r = mlx_whisper.transcribe(
                audio, path_or_hf_repo=a.model, language=req.get("language") or "ko", verbose=None,
                condition_on_previous_text=False, temperature=0.0, initial_prompt=req.get("prompt") or None,
            )
            segs = [{
                "start": round(float(s.get("start", 0.0)), 3),
                "end": round(float(s.get("end", 0.0)), 3),
                "text": str(s.get("text", "")).strip(),
                "no_speech_prob": round(float(s.get("no_speech_prob", 0.0)), 4),
                "avg_logprob": round(float(s.get("avg_logprob", 0.0)), 4),
                "compression_ratio": round(float(s.get("compression_ratio", 0.0)), 3),
            } for s in r.get("segments", [])]
            reply({"id": rid, "segments": segs, "text": str(r.get("text", "")).strip(),
                   "compute_sec": round(time.perf_counter() - t1, 3)})
        except Exception as e:
            reply({"id": rid, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
