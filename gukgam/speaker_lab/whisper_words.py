"""기존 로컬 Whisper의 단어 시각을 실험용 파일로만 저장하는 독립 워커."""
from __future__ import annotations

import argparse
import contextlib
import math
import os
from pathlib import Path
import sys
import time
import wave

from .files import digest, safe_output_path, wav_info, write_json

try:
    import resource
except ImportError:  # Windows는 실험 모듈을 읽을 때도 POSIX 자원 측정을 요구하지 않는다.
    resource = None


def _peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def transcribe_words(audio: Path, model: Path, output: Path) -> dict:
    """다운로드 없이 설치된 모델을 실행한다. 운영 워커와 포트를 공유하지 않는다."""
    output = safe_output_path(output)
    if not model.is_dir() or not all((model / p).is_file() for p in ("config.json", "weights.safetensors")):
        raise ValueError("설치된 Whisper 모델 경로가 필요합니다")
    if output.exists() or output.is_symlink():
        raise ValueError("새 단어 시각 파일 경로가 필요합니다")
    info = wav_info(audio)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    start = time.perf_counter()
    # 모델의 원문·진단 출력을 터미널에 보내지 않는다.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        import numpy as np
        import mlx_whisper
        # 이미 검사한 PCM을 직접 넘겨 전역 ffmpeg 검색·추가 변환을 피한다.
        with wave.open(str(audio), "rb") as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        result = mlx_whisper.transcribe(
            pcm, path_or_hf_repo=str(model.resolve()), language="ko", verbose=None,
            condition_on_previous_text=False, temperature=0.0, word_timestamps=True,
        )
    words = []
    rejected_words = 0
    for segment in result.get("segments", []):
        for word in segment.get("words", []):
            lo, hi = float(word["start"]), float(word["end"])
            text = word.get("word")
            if (not isinstance(text, str) or not text.strip() or not all(map(math.isfinite, (lo, hi)))
                    or not 0 <= lo < hi <= info["duration_seconds"] + .05
                    or lo >= info["duration_seconds"]):
                rejected_words += 1
                continue
            words.append({"text": text, "start": lo, "end": min(hi, info["duration_seconds"])})
    payload = {"media_sha256": info["sha256"], "words": words, "rejected_word_count": rejected_words,
               "provider": "local_mlx_whisper", "model_sha256": digest(model / "weights.safetensors"),
               "elapsed_seconds": time.perf_counter() - start,
               "process_peak_rss_bytes": _peak_rss_bytes(),
               "validation_status": "not_audio_verified"}
    write_json(output, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        transcribe_words(args.audio, args.model, args.output)
    except Exception:
        print("로컬 단어 시각 추출 실패. 입력·설치 모델·메모리를 확인하세요", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
