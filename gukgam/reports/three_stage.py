"""보존 자료만으로 원자막·실시간 판정·종료 보정의 비교 판본을 만든다."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from gukgam.transcript_correction import corrected_rows
from gukgam.transcript_review import snapshot_hash, validate_result
from .question_comparison import display_parts as _display_parts


def verify_raw_rows(audit: dict, raw_lines: list[bytes]) -> None:
    """감사 매핑을 실제 수신 줄과 다시 대조한다. 전환 기호도 버리지 않는다."""
    for row in audit["rows"]:
        text, position = row["original_text"], 0
        for piece in row["raw_pieces"]:
            raw = raw_lines[piece["raw_line"] - 1]
            if hashlib.sha256(raw).hexdigest() != piece["raw_line_sha256"]:
                raise ValueError("원수신 줄의 판본이 달라졌습니다")
            msg = json.loads(json.loads(raw)["msg"])
            received = msg["transcripts"][piece["piece_index"]]
            if (msg["segment"] != piece["segment"] or received != [piece["hwa"], piece["raw_text"]]
                    or msg.get("scd") != piece["scd"] or msg.get("final") != piece["final"]):
                raise ValueError("원수신 조각의 출처가 다릅니다")
            body = piece["raw_text"]
            marker = body.startswith("- ")
            body = body[2:] if marker else body
            if body != piece["text"] or marker != piece["new_speaker_marker"]:
                raise ValueError("원수신 본문 또는 전환 표식이 다릅니다")
            start, end = piece["start"], piece["end"]
            if (start < position or start > end or end > len(text)
                    or text[position:start].strip() or text[start:end] != body):
                raise ValueError("원수신 조각에 누락·중복·본문 변경이 있습니다")
            position = end
        if not row["raw_pieces"] or text[position:].strip():
            raise ValueError("원수신 조각의 전체 원문을 확인할 수 없습니다")


def _raw_parts(row: dict) -> list[dict]:
    """같은 표식의 이웃 조각만 합쳐 본문 안의 표식 전환을 드러낸다."""
    parts = []
    for piece in row["raw_pieces"]:
        hwa = piece["hwa"]
        label = f"국회 표식 {hwa}" if hwa >= 0 else "국회 표식 미정"
        if parts and parts[-1]["speaker"] == label:
            parts[-1]["end"] = piece["end"]
        else:
            if parts:
                parts[-1]["end"] = piece["start"]
            parts.append({"start": piece["start"], "end": piece["end"],
                          "speaker": label, "role": "unknown"})
    return parts


def build_payload(*, audit: dict, prepared: dict, result: dict, preview: dict,
                  correction: dict, alignment: dict, words: dict, media: dict,
                  meeting_label: str = "보존한 회의") -> dict:
    """운영 DB·모델 접근 없이 검증된 별도 판본만 표시용 자료로 투영한다."""
    snapshot = deepcopy(prepared["snapshot"])
    turns, review = result["turns"], result["speaker_review"]
    validate_result(snapshot, turns)
    if (review["turns"] != turns or review["input_hash"] != snapshot_hash(snapshot)
            or review["session_id"] != snapshot["session_id"] or review["block_id"] != snapshot["block"]["id"]):
        raise ValueError("화자 검토의 질의·판본이 다릅니다")
    snapshot["speaker_hold_ids"] = review.get("held_utterance_ids", [])
    expected = corrected_rows(snapshot, turns, correction)
    if preview["rows"] != expected or preview["turns"] != turns or preview["text_correction"] != correction:
        raise ValueError("보존한 교정 미리보기의 판본이 다릅니다")
    if alignment["media_sha256"] != media["sha256"] or words["media_sha256"] != media["sha256"]:
        raise ValueError("정렬·전사·원음이 같은 녹음이 아닙니다")
    ids = [u["id"] for u in snapshot["utterances"]]
    if ([r["utterance_id"] for r in audit["rows"]] != ids
            or audit["session_id"] != snapshot["session_id"] or audit["block_id"] != snapshot["block"]["id"]):
        raise ValueError("출처 감사의 전체 질의 범위가 다릅니다")
    aligned = {r["id"]: r for r in alignment["rows"]}
    if len(aligned) != len(alignment["rows"]):
        raise ValueError("정렬 행이 중복되었습니다")
    rows = []
    for ordinal, (source, trace, final) in enumerate(zip(snapshot["utterances"], audit["rows"], expected), 1):
        uid, text = source["id"], source["text"]
        before = trace["stage_b"]
        if (trace["original_text"] != source["original_text"] or not trace["raw_reconstruction_exact"]
                or any(before[k] != source[k] for k in ("text", "speaker", "speaker_role", "speaker_source", "ts_start", "ts_end"))
                or trace["source_key"] != source["source_key"] or trace["source_order"] != source["source_order"]):
            raise ValueError("실시간 판본 또는 원문 출처가 달라졌습니다")
        stage_parts = [p for p in turns if p["utterance_id"] == uid]
        if (trace["stage_c_turns"] != stage_parts or trace["stage_c_text"] != final["proposed_text"]
                or trace["stage_c_patches"] != final["patches"]):
            raise ValueError("종료 보정 출처가 달라졌습니다")
        a = aligned.get(uid)
        if not a or a["text"] != text:
            raise ValueError("추가 음성 근거의 원문이 달라졌습니다")
        position = 0
        for part in a["parts"]:
            if part["start"] != position or part["text"] != text[part["start"]:part["end"]] or part["end"] > len(text):
                raise ValueError("추가 음성 근거의 문자 범위가 다릅니다")
            position = part["end"]
            if part["status"] == "draft":
                times = part["start_ms"], part["end_ms"]
                if (not all(type(v) in (int, float) and math.isfinite(v) for v in times)
                        or not 0 <= times[0] < times[1] <= media["duration_seconds"] * 1000 + 0.001):
                    raise ValueError("음성 조각 재생 범위가 원음 밖입니다")
        if position != len(text):
            raise ValueError("추가 음성 근거에 원문이 빠졌습니다")
        anchors = a["anchors"]
        for anchor in anchors:
            start, end = anchor["start_ms"], anchor["end_ms"]
            if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= media["duration_seconds"] * 1000 + 0.001:
                raise ValueError("음성 대응 범위가 원음 밖입니다")
        start = min((x["start_ms"] for x in anchors), default=None)
        end = max((x["end_ms"] for x in anchors), default=None)
        audio_text = "" if start is None else "".join(w["text"] for w in words["words"]
                            if w["start"] * 1000 < end and w["end"] * 1000 > start).strip()
        live = [{"start": 0, "end": len(text), "role": before["speaker_role"], "speaker": before["speaker"]}]
        changes = []
        for key, label in (("role", "역할"), ("speaker", "화자 이름")):
            if any(p[key] != live[0][key] for p in stage_parts):
                changes.append(label)
        if len(stage_parts) != 1:
            changes.append("행 내부 분리")
        if any(p.get("group_id") for p in stage_parts):
            changes.append("연속 발언 묶음")
        if final["proposed_text"] != text:
            changes.append("본문")
        held = uid in snapshot["speaker_hold_ids"]
        if held != trace["stage_c_speaker_held"]:
            raise ValueError("보류 상태의 판본이 다릅니다")
        raw_pieces = [{k: p[k] for k in ("raw_text", "text", "hwa", "scd", "final", "new_speaker_marker")}
                      for p in trace["raw_pieces"]]
        raw_note = "원수신 조각을 같은 발언 단위로 묶었습니다. 표식 번호는 사람 이름이 아닙니다."
        if any(not p["final"] for p in raw_pieces):
            raw_note += " 미확정 수신 조각이 포함되어 있습니다."
        rows.append({"id": uid, "ordinal": ordinal,
            "time_label": datetime.fromtimestamp(source["ts_start"] / 1000, ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S"),
            "raw": {"text": source["original_text"], "parts": _raw_parts(trace), "pieces": raw_pieces,
                    "speaker_label": "원수신 화자 표식", "status": "원수신 보존", "source_note": raw_note},
            "live": {"text": text, "parts": live, "status": "당시 저장 판정",
                     "source_note": "당시 저장된 실시간 코드 판정" if source["speaker_source"] != "manual" else "당시 사람이 지정한 화자"},
            "post": {"text": final["proposed_text"], "parts": _display_parts(stage_parts, final["patches"]),
                     "status": "보류·기존 초안 유지" if held else "검토 전 초안",
                     "source_note": "저장된 음성·Jev·LLM 결과. 최신 정렬 보완은 미반영. 본문은 별도 교정 제안입니다."},
            "changed_fields": changes, "held": held,
            "audio": {"start": None if start is None else start / 1000, "end": None if end is None else end / 1000,
                      "text": audio_text, "parts": deepcopy(a["parts"]),
                      "source_note": "최신 정렬의 일부 대응 범위입니다. 아래 익명 ID는 3열 화자 판정에 아직 반영하지 않았습니다."}})
    return {"schema_version": 1, "title": "질의 전사 · 세 판본 비교", "meeting_label": meeting_label,
        "questioner": snapshot["block"]["questioner"], "duration_seconds": media["duration_seconds"],
        "notices": [f"전체 {len(rows)}행 · 실시간→종료 후 화자·묶음 차이 {sum(bool(r['changed_fields']) for r in rows)}행 · 화자 판정 보류 {sum(r['held'] for r in rows)}행 · 본문 교정 {len(correction['patches'])}건",
            "같은 질의의 전체 원문을 순서대로 정렬했습니다. 차이는 정답을 뜻하지 않습니다.",
            "3열은 실제 저장된 종료 후 보정 초안입니다. 보류 행에는 기존 AI 초안이 남아 있습니다.",
            "최신 짧은 응답 정렬 보완 뒤 Jev·LLM은 재실행하지 않았습니다. 추가 근거는 펼쳐 보기에서 구분합니다.",
            "행 시각은 자막 수신 시각이고, 행별 재생은 음성에서 대응한 일부 범위입니다. 녹음 전체도 들을 수 있습니다."],
        "versions": [
            {"label": "AI 자막 원문", "description": "원수신 본문과 화자 표식", "status": "원문 보존"},
            {"label": "실시간 코드 보정", "description": "수집 당시 저장된 역할·이름", "status": "당시 판본"},
            {"label": "질의 종료 후 보정", "description": "Nemotron·Whisper → Jev → LLM", "status": "검토 전 초안"}],
        "provenance": {"원문": "원수신 조각·당시 저장 판본·종료 보정의 출처를 대조했습니다.",
            "종료 화자 초안 저장 시각": datetime.fromtimestamp(review.get("generated_at", 0) / 1000, ZoneInfo("Asia/Seoul")).isoformat() if review.get("generated_at") else "보존 자료에 시각 없음",
            "실제 모델": "보존된 Nemotron·Whisper 결과와 Jev 응답·Gemma 결과를 표시합니다. 이번 화면 생성의 추가 호출은 0회입니다.",
            "본문 교정": f"선택된 교정 {len(correction['patches'])}건. 선택하지 않은 후보를 본문에 반영하지 않았습니다.",
            "최신 정렬": "짧은 응답 정렬 보완 후 최종 화자 판정 미재실행",
            "운영 반영": "이 비교 화면은 원문·운영 DB·수집 상태를 변경하지 않습니다.",
            "정답 검증": "원음 청취 정답·화자 정확도는 미검증입니다."},
        "rows": rows}
