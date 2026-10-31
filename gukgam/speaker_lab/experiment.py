"""원음·자막의 실현성을 먼저 확인하는 오프라인 실험. 운영 API를 호출하지 않는다."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import math
import os
from pathlib import Path
import subprocess
import sys

from .files import digest, new_directory, read_json, wav_info, write_json


def caption_text(payload: dict) -> tuple[str, list[dict]]:
    """질의 블록과 독립한 원문 문자 좌표를 만든다. 화자 라벨은 입력하지 않는다."""
    rows = payload.get("captions")
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise ValueError("1~1000개 원문 자막이 필요합니다")
    pieces, coordinates, cursor, seen = [], [], 0, set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not row["text"]:
            raise ValueError("원문 자막 형식이 잘못됐습니다")
        uid = row.get("id")
        if not isinstance(uid, (str, int)) or isinstance(uid, bool) or uid in seen:
            raise ValueError("자막 식별자는 중복되지 않아야 합니다")
        seen.add(uid)
        if pieces:
            cursor += 1
        text = row["text"]
        coordinates.append({"id": uid, "start": cursor, "end": cursor + len(text)})
        cursor += len(text)
        pieces.append(text)
    if cursor > 120000:
        raise ValueError("자막 문자 수 한도를 넘었습니다")
    return "\n".join(pieces), coordinates


def media_payload(path: Path, sha: str) -> dict:
    """다른 음성의 캐시·정답을 섞지 않는다."""
    value = read_json(path)
    if not isinstance(value, dict) or value.get("media_sha256") != sha:
        raise ValueError("음성 해시와 입력 근거가 일치하지 않습니다")
    return value


def timed_words(payload: dict, duration: float):
    """단위 변환 전에 실제 음성 범위와 유한 숫자를 검사한다."""
    from .alignment import ASRWord
    rows = payload.get("words")
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("단어 시각 자료 형식·한도가 잘못됐습니다")
    result = []
    for word in rows:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str):
            raise ValueError("단어 형식이 잘못됐습니다")
        lo, hi = word.get("start"), word.get("end")
        if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in (lo, hi)):
            raise ValueError("단어 시각은 유한 숫자여야 합니다")
        if not 0 <= lo < hi <= duration:
            raise ValueError("단어 시각이 음성 범위를 벗어났습니다")
        result.append(ASRWord(word["text"], round(lo * 1000), round(hi * 1000)))
    return result


def run_experiment(args) -> dict:
    args._created_output = None
    info = wav_info(args.audio)
    inputs = {"audio": digest(args.audio)}
    text, coordinates = caption_text(read_json(args.captions))
    inputs["captions"] = digest(args.captions)
    media_id = info["sha256"]
    cached = media_payload(args.turns, media_id) if args.turns else None
    words = media_payload(args.words, media_id) if args.words else None
    reference = media_payload(args.reference, media_id) if args.reference else None
    if words is not None:
        timed_words(words, info["duration_seconds"])
    for key in ("turns", "words", "reference"):
        if getattr(args, key):
            inputs[key] = digest(getattr(args, key))
    if args.run_local and (not args.executable or not args.model):
        raise ValueError("실제 실행에는 로컬 실행물과 모델이 모두 필요합니다")
    if args.run_local and cached:
        raise ValueError("실제 실행과 화자 캐시 재생은 분리합니다")
    if args.whisper_model and (not args.run_local or words):
        raise ValueError("새 단어 추출은 실제 실행에서만 하며 캐시와 혼합하지 않습니다")
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 1800:
        raise ValueError("제한 시간은 0~1800초입니다")
    out = new_directory(args.output)
    args._created_output = out
    result = {"schema_version": 1, "audio": info, "caption_coordinates": coordinates,
              "caption_text": text, "input_hashes": inputs, "production_changed": False,
              "quality_resolved": False, "release_ready": False,
              "mode": "local_model" if args.run_local else "cached" if cached else "preflight",
              "models_executed": [], "limitations": [
                  "모델 출력은 원음 정답이 아닙니다",
                  "익명 화자 ID는 이 음성 실행 안에서만 유효하며 실명·역할·질의자가 아닙니다",
                  "본문·원문 DB·사람 수정·운영 화면에는 적용하지 않습니다",
              ]}
    if args.run_local:
        from .nemo import diarize_local
        result["model_sha256"] = digest(args.model)
        result["executable_sha256"] = digest(args.executable)
        execution = diarize_local(args.audio, executable=args.executable, model_path=args.model,
                                 device=args.device, timeout_seconds=args.timeout)
        cached = {"media_sha256": media_id, "segments": [
            {"start": seg.start, "end": seg.end, "speaker": seg.speaker_id}
            for seg in execution.segments]}
        write_json(out / "diarization.json", cached)
        result["models_executed"].append("local_nemo_diarization")
        result["performance"] = {"elapsed_seconds": execution.elapsed_seconds,
                                 "real_time_factor": execution.elapsed_seconds / info["duration_seconds"],
                                 "process_peak_rss_bytes": execution.peak_rss_bytes,
                                 "system_pressure_measured": False}
        write_json(out / "diarization-performance.json", result["performance"])
        if args.whisper_model:
            # 두 모델을 동시에 메모리에 올리지 않는다. 앞 자식 종료 뒤 전사한다.
            env = {k: os.environ[k] for k in ("HOME", "TMPDIR", "LANG", "LC_ALL") if k in os.environ}
            env.update(PATH="/usr/bin:/bin", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            words_path = out / "words.json"
            proc = subprocess.run([sys.executable, "-m", "gukgam.speaker_lab.whisper_words",
                                   "--audio", str(args.audio.resolve()), "--model", str(args.whisper_model.resolve()),
                                   "--output", str(words_path)], cwd=Path(__file__).resolve().parents[2],
                                  env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=args.timeout, check=False)
            if proc.returncode:
                raise ValueError("로컬 단어 시각 추출 실패. 보존된 음성 분리 결과를 확인하세요")
            words = media_payload(words_path, media_id)
            result["models_executed"].append("local_mlx_whisper")
            result["asr_performance"] = {k: words.get(k) for k in ("elapsed_seconds", "process_peak_rss_bytes")}
    if cached:
        from .evaluation import evaluate_diarization
        result["diarization"] = cached["segments"]
        result["evaluation"] = evaluate_diarization(
            reference.get("segments") if reference else None, cached["segments"],
            duration_seconds=info["duration_seconds"],
            reference_verified=bool(reference and reference.get("annotation_status") == "audio_verified"))
    if words:
        from .alignment import SpeakerInterval, align_caption, project_speakers
        anchors = align_caption(text, timed_words(words, info["duration_seconds"]), media_id=media_id)
        result["alignment"] = asdict(anchors)
        if cached:
            result["speaker_spans"] = [asdict(span) for span in project_speakers(
                anchors, [SpeakerInterval(round(seg["start"] * 1000), round(seg["end"] * 1000), seg["speaker"])
                          for seg in cached["segments"]], media_id=media_id)]
    else:
        result["limitations"].append("단어 시각이 없어 자막과 음성의 정렬은 검증하지 못했습니다")
    for key, expected in inputs.items():
        if digest(getattr(args, key)) != expected:
            raise ValueError("실험 도중 입력 자료가 변경되어 결과를 채택하지 않습니다")
    write_json(out / "result.json", result)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ("audio", "captions", "output"):
        p.add_argument("--" + flag, type=Path, required=True)
    for flag in ("turns", "words", "reference", "executable", "model", "whisper-model"):
        p.add_argument("--" + flag, type=Path)
    p.add_argument("--run-local", action="store_true", help="이미 준비한 로컬 모델의 실제 실행을 명시")
    p.add_argument("--device", default="metal", choices=("metal", "cpu"))
    p.add_argument("--timeout", type=float, default=900)
    args = p.parse_args()
    try:
        result = run_experiment(args)
    except Exception as exc:
        # 실패한 단계의 앞 결과도 남기며 예외 원문·전사 본문을 로그에 쓰지 않는다.
        try:
            if getattr(args, "_created_output", None) is not None:
                write_json(args.output / "failure.json", {"status": "failed", "error_type": type(exc).__name__,
                                                         "release_ready": False, "production_changed": False})
        except (OSError, ValueError):
            pass
        print("실험 실패: 입력 형식·로컬 모델·출력 경로를 확인하세요. 운영 데이터는 변경하지 않았습니다.", file=sys.stderr)
        return 1
    print(f"실험 자료 저장 완료 · 방식 {result['mode']} · 실제 화자 품질 해결 판정 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
