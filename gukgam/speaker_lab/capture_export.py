"""기존 원수신 로그에서 녹음 주변 자막을 보존한다. 음성 시각 정렬은 하지 않는다."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

from ..secure_files import create_private_file, make_private_directory, open_regular_file

from .files import digest, safe_output_path, write_json


CONTEXT_MS = 60_000
MAX_RANGE_MS = 900_000
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_LINES = 500_000
MAX_LINE_BYTES = 128 * 1024
MAX_EXPORT_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 30_000
MAX_SEGMENTS = 1000
MAX_TEXT_CHARS = 120_000
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


def _plain_path(path: Path) -> Path:
    """상위 이동과 링크를 허용하지 않고 경로를 절대화한다."""
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise ValueError("자막 내보내기 경로에 상위 이동을 사용할 수 없습니다")
    path = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("자막 내보내기 경로에 링크를 사용할 수 없습니다")
    return path


def _new_output(path: Path) -> Path:
    """새로 만드는 모든 자료 폴더를 700으로 제한한다."""
    path = safe_output_path(_plain_path(path))
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not missing:
        raise ValueError("자막 출력은 새 폴더여야 합니다")
    for part in reversed(missing):
        make_private_directory(part)
    return path


def _load_json(value: str | bytes) -> Any:
    def invalid(_value: str):
        raise ValueError("비유한 JSON 숫자는 허용하지 않습니다")

    def finite(value: str):
        result = float(value)
        if not math.isfinite(result):
            return invalid(value)
        return result

    return json.loads(value, parse_constant=invalid, parse_float=finite)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**15


def _number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value < 10**15)


def _payload(value: Any) -> dict | None:
    """연결 주소·설정·토큰 등 메타데이터를 복사하지 않고 자막 필드만 보존한다."""
    if isinstance(value, str):
        value = _load_json(value)
    if not isinstance(value, dict) or "segment" not in value:
        return None
    segment = value["segment"]
    if not _integer(segment):
        raise ValueError("원자막 번호 형식이 잘못됐습니다")
    text = value.get("transcript")
    if not isinstance(text, str) or not text:
        return None
    if not isinstance(value.get("final"), bool):
        raise ValueError("원자막 확정 표지 형식이 잘못됐습니다")
    result = {"segment": segment, "transcript": text, "final": value["final"]}
    if "scd" in value:
        if not isinstance(value["scd"], bool):
            raise ValueError("원자막 분할 표지 형식이 잘못됐습니다")
        result["scd"] = value["scd"]
    if "transcripts" in value:
        rows = value["transcripts"]
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("원자막 분할 형식이 잘못됐습니다")
        for row in rows:
            if (not isinstance(row, list) or len(row) != 2
                    or not isinstance(row[0], int) or isinstance(row[0], bool)
                    or not -1 <= row[0] <= 1000 or not isinstance(row[1], str)):
                raise ValueError("원자막 분할 형식이 잘못됐습니다")
        result["transcripts"] = rows
    for key in ("segment-offset", "segment-duration"):
        if key in value:
            if not _number(value[key]):
                raise ValueError("원자막 오프셋 형식이 잘못됐습니다")
            result[key] = value[key]
    for key in ("send-timestamp", "recv-timestamp"):
        if key in value:
            stamp = value[key]
            if not isinstance(stamp, str) or len(stamp) > 64:
                raise ValueError("원자막 시각 형식이 잘못됐습니다")
            datetime.fromisoformat(stamp)
            result[key] = stamp
    return result


def _source_key(payload: dict) -> tuple[int, int | float | None]:
    return payload["segment"], payload.get("segment-offset")


def _without_raw(output_dir: Path, session_id: str, start_ms: int, end_ms: int) -> dict:
    """원수신 파일이 없어도 녹음 보존은 가능하되 자막 준비를 주장하지 않는다."""
    out = _new_output(output_dir)
    raw_out = out / "caption-events.jsonl"
    with create_private_file(raw_out):
        pass
    write_json(out / "captions.json", {
        "schema_version": 1, "session_id": session_id, "captions": [], "provenance": [],
        "timing_uncertain": True, "alignment_verified": False,
    })
    result = {
        "schema_version": 1, "session_id": session_id, "status": "no_raw",
        "capture_ready": False, "alignment_verified": False, "quality_resolved": False,
        "timing_uncertain": True, "receive_range_ms": {"start": start_ms, "end": end_ms},
        "context_range_ms": {"start": max(0, start_ms - CONTEXT_MS), "end": end_ms + CONTEXT_MS},
        "context_ms": CONTEXT_MS, "source": {"name": f"{session_id}.jsonl", "exists": False},
        "files": {"events": {"name": raw_out.name, "sha256": digest(raw_out), "bytes": 0},
                  "captions": {"name": "captions.json", "rows": 0,
                               "sha256": digest(out / "captions.json")}},
        "review_reasons": ["raw_file_missing", "no_caption_received_in_capture_range"],
        "production_changed": False, "models_executed": [],
        "policies": ["원수신 파일이 없어 자막을 확보하지 못했다. 음성만 보존된 상태다",
                     "수신 시각과 실제 음성 시각의 일치는 확인하지 않았다"],
    }
    write_json(out / "export-manifest.json", result)
    return result


def export_captions(raw_path: Path, output_dir: Path, *, session_id: str,
                    start_ms: int, end_ms: int) -> dict:
    """녹음 수신 범위 ±60초의 제한 원수신과 마지막 확정 본문을 새 폴더에 쓴다.

    읽기 시작 시점의 파일 길이만 순차 조회한다. 동일 번호·오프셋의 마지막
    final 판본을 쓰되 이후 partial과 범위 밖 후기 갱신 가능성을 숨기지 않는다.
    수신 창은 음성 구간의 정답이 아니므로 준비·정렬·품질 판정은 항상 미검증이다.
    """
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        raise ValueError("회의 식별자 형식이 잘못됐습니다")
    if (not _integer(start_ms) or not _integer(end_ms)
            or not 0 < end_ms - start_ms <= MAX_RANGE_MS):
        raise ValueError("녹음 수신 범위는 0초보다 길고 15분 이하여야 합니다")
    raw_path = _plain_path(raw_path)
    if raw_path.name != f"{session_id}.jsonl":
        raise ValueError("회의 식별자와 원수신 파일 이름이 다릅니다")
    # 읽기 전용 FD를 사용하며 파일 생성·SQLite 접근·수집 연결은 하지 않는다.
    try:
        source = open_regular_file(raw_path)
    except FileNotFoundError:
        return _without_raw(output_dir, session_id, start_ms, end_ms)
    with source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("원수신 입력은 일반 파일이어야 합니다")
        if before.st_size > MAX_SOURCE_BYTES:
            raise ValueError("원수신 파일 크기 한도를 넘었습니다")
        out = _new_output(output_dir)
        try:
            return _export_snapshot(source, before, raw_path, out, session_id, start_ms, end_ms)
        except Exception:
            # 중간 산출물은 증거로 남기되 성공한 묶음으로 사용하지 않는다.
            write_json(out / "export-failure.json", {
                "status": "failed", "capture_ready": False, "alignment_verified": False,
                "reason": "원수신 읽기·검증·출력 실패. 부분 산출물은 평가 입력으로 사용하지 마세요",
            })
            raise


def _export_snapshot(source, before, raw_path: Path, out: Path, session_id: str,
                     start_ms: int, end_ms: int) -> dict:
    lo, hi = max(0, start_ms - CONTEXT_MS), end_ms + CONTEXT_MS
    counts = {"lines": 0, "invalid_lines": 0, "ignored_messages": 0,
              "selected_events": 0, "core_events": 0, "out_of_order_rx": 0,
              "duplicate_events": 0, "final_revisions": 0}
    segments: dict[tuple, dict] = {}
    snapshot_hash, exported_hash = hashlib.sha256(), hashlib.sha256()
    consumed = exported_bytes = 0
    last_rx = None
    selected_rx: list[int] = []
    partial_tail = False
    raw_out = out / "caption-events.jsonl"
    with create_private_file(raw_out) as dest:
        while consumed < before.st_size:
            line = source.readline(min(MAX_LINE_BYTES + 1, before.st_size - consumed))
            if not line:
                break
            consumed += len(line)
            snapshot_hash.update(line)
            counts["lines"] += 1
            if counts["lines"] > MAX_LINES or len(line) > MAX_LINE_BYTES:
                raise ValueError("원수신 줄 수 또는 줄 길이 한도를 넘었습니다")
            if not line.endswith(b"\n"):
                partial_tail = True
                break
            try:
                outer = _load_json(line)
                if not isinstance(outer, dict) or not _integer(outer.get("rx")):
                    raise ValueError("수신 시각 형식 오류")
                rx = outer["rx"]
                if last_rx is not None and rx < last_rx:
                    counts["out_of_order_rx"] += 1
                last_rx = rx
                if not lo <= rx <= hi:
                    continue
                payload = _payload(outer.get("msg"))
                if payload is None:
                    counts["ignored_messages"] += 1
                    continue
            except (ValueError, TypeError, RecursionError, UnicodeError):
                counts["invalid_lines"] += 1
                continue
            encoded = (json.dumps({"rx": rx, "msg": json.dumps(payload, ensure_ascii=False,
                       allow_nan=False)}, ensure_ascii=False, allow_nan=False) + "\n").encode()
            counts["selected_events"] += 1
            if counts["selected_events"] > MAX_EVENTS:
                raise ValueError("내보낼 원수신 건수 한도를 넘었습니다")
            exported_bytes += len(encoded)
            if exported_bytes > MAX_EXPORT_BYTES:
                raise ValueError("내보낼 원수신 크기 한도를 넘었습니다")
            dest.write(encoded)
            exported_hash.update(encoded)
            selected_rx.append(rx)
            in_core = start_ms <= rx <= end_ms
            counts["core_events"] += int(in_core)
            key = _source_key(payload)
            if key not in segments:
                if len(segments) >= MAX_SEGMENTS:
                    raise ValueError("원자막 수 한도를 넘었습니다")
                segments[key] = {"first_rx": rx, "first_line": counts["lines"], "events": 0,
                                 "last_final": None, "core": False, "last_payload": None}
            item = segments[key]
            if item["last_payload"] == payload:
                counts["duplicate_events"] += 1
            item.update(last_rx=rx, latest_final=payload["final"], last_payload=payload,
                        events=item["events"] + 1, core=item["core"] or in_core)
            if payload["final"]:
                if item["last_final"] is not None and item["last_final"] != payload:
                    counts["final_revisions"] += 1
                item["last_final"] = payload
    after = os.fstat(source.fileno())
    try:
        path_after = raw_path.stat(follow_symlinks=False)
        replaced = (path_after.st_dev, path_after.st_ino) != (before.st_dev, before.st_ino)
    except OSError:
        replaced = True
    changed = ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
               or consumed != before.st_size or replaced)
    captions, provenance = [], []
    incomplete = missing_offsets = 0
    text_chars = 0
    for index, (key, item) in enumerate(segments.items()):
        payload = item["last_final"]
        missing_offsets += int(key[1] is None)
        incomplete += int(payload is None or not item["latest_final"])
        if payload is None:
            continue
        uid = f"raw-{index + 1}-s{key[0]}"
        text = payload["transcript"]
        text_chars += len(text) + bool(captions)
        if text_chars > MAX_TEXT_CHARS:
            raise ValueError("정렬 입력 원문 문자 수 한도를 넘었습니다")
        captions.append({"id": uid, "text": text})
        provenance.append({"id": uid, "segment": key[0], "segment_offset_ms": key[1],
                           "first_rx_ms": item["first_rx"], "last_rx_ms": item["last_rx"],
                           "first_source_line": item["first_line"], "events": item["events"],
                           "scope": "capture" if item["core"] else "context",
                           "latest_event_final": item["latest_final"],
                           "scd": payload.get("scd"), "final": True})
    # 공백은 음성 무음인지 수신 손실인지 판정하지 않는다. 의심 구간만 명시한다.
    core_rx = sorted(set(t for t in selected_rx if start_ms <= t <= end_ms))
    gap_bounds = [start_ms, *core_rx, end_ms]
    gaps = [{"start_rx_ms": a, "end_rx_ms": b} for a, b in zip(gap_bounds, gap_bounds[1:])
            if b - a >= CONTEXT_MS]
    reasons = []
    if not counts["core_events"]:
        reasons.append("no_caption_received_in_capture_range")
    if not captions:
        reasons.append("no_final_caption")
    if incomplete:
        reasons.append("partial_or_reopened_caption")
    if missing_offsets:
        reasons.append("segment_reuse_ambiguous_without_offset")
    if partial_tail or consumed != before.st_size:
        reasons.append("incomplete_snapshot_tail")
    if changed:
        reasons.append("source_changed_during_snapshot")
    if counts["invalid_lines"]:
        reasons.append("invalid_source_lines")
    if counts["out_of_order_rx"]:
        reasons.append("non_monotonic_receive_clock")
    if gaps:
        reasons.append("receive_gaps_require_review")
    payload = {"schema_version": 1, "session_id": session_id, "captions": captions,
               "provenance": provenance, "timing_uncertain": True, "alignment_verified": False}
    write_json(out / "captions.json", payload)
    result = {
        "schema_version": 1, "session_id": session_id, "status": "needs_review",
        "capture_ready": False, "alignment_verified": False, "quality_resolved": False,
        "timing_uncertain": True, "receive_range_ms": {"start": start_ms, "end": end_ms},
        "context_range_ms": {"start": lo, "end": hi}, "context_ms": CONTEXT_MS,
        "source": {"name": raw_path.name, "exists": True, "snapshot_bytes": before.st_size,
                   "bytes_read": consumed, "snapshot_sha256": snapshot_hash.hexdigest(),
                   "changed_during_read": changed, "path_replaced": replaced,
                   "incomplete_tail": partial_tail},
        "files": {"events": {"name": raw_out.name, "sha256": exported_hash.hexdigest(),
                              "bytes": exported_bytes},
                  "captions": {"name": "captions.json", "rows": len(captions),
                               "sha256": digest(out / "captions.json")}},
        "counts": counts, "incomplete_segments": incomplete, "receive_gaps": gaps,
        "review_reasons": reasons, "production_changed": False, "models_executed": [],
        "policies": [
            "녹음과 자막의 로컬 수신 시각만 비교했다. 지연·음성 내용의 시간 일치는 미검증이다",
            "앞뒤 60초의 문맥을 포함한다. 문맥 자막이 실제 음성 범위 안이라는 보장은 없다",
            "segment와 offset이 같은 원출처의 파일 순서상 마지막 final 본문을 사용한다",
            "동일 segment의 다른 offset은 별도 원출처로 보존한다. 번호 재사용 판정은 미검증이다",
            "후기 partial은 버리지 않고 원수신에 보존하며 이전 final 선택 사실을 표시한다",
            "파일 읽기 시작 길이와 수신 창 밖 후기 갱신은 포함하지 않는다. 최종판 불변을 보장하지 않는다",
            "transcript 본문은 그대로 보존한다. 색별 조각의 임의 합치기·원문 DB 조회·수정은 없다",
            "원수신 사본에는 자막 필드만 포함한다. 주소·설정·토큰 등 추가 메타데이터는 제외한다",
            "수신 공백은 음성 무음·회의 정회·통신 손실 중 어느 것인지 판정하지 않는다",
        ],
    }
    write_json(out / "export-manifest.json", result)
    return result
