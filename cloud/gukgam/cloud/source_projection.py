"""원본 조회 중계의 공개 필드. 모델 입력·원음·로컬 경로를 전송하지 않는다."""
from __future__ import annotations

from copy import deepcopy
import json
import re

from ..stage_mirror import public_document, public_live_turn, public_source


def _pick(row, names):
    return {key: deepcopy(row[key]) for key in names if key in row} if isinstance(row, dict) else {}


SUMMARY_KEYS = frozenset(("id", "scope", "content", "provider", "model", "created_at", "time", "one_line",
    "question_points", "answer_points", "issues", "data_requests", "commitments", "org_relevance", "org_reason",
    "response_type", "urgency", "generated_at", "latency_ms", "edited_by", "edited_at", "input_source", "review_id",
    "input_hash", "source_document_id", "ai_draft", "question_ownership", "status", "questioner", "start", "end",
    "utterance_id", "offset", "validation_status", "points", "title", "bullets", "key_requests", "evidence",
    "sentence_ids", "bullet", "text", "quote", "speaker", "strength", "due_date", "kind", "basis", "partial",
    "held_bullet_count", "production_changed", "reviewed_by", "reviewed_at", "source_ids", "excluded_count",
    "top_issues", "action_items", "watch_points", "org_mentions", "progress", "recommended_actions", "block_id"))


_SUMMARY_PROVIDERS = frozenset(("rules", "rules(대체)", "desktop_chatgpt", "local_mlx", "openai_compat", "anthropic", "closed_block"))
_COVERAGE_FLAGS = frozenset(("question_presence_unverified", "meeting_coverage_verified", "partial", "text_truncated",
                           "stored_utterance_coverage_complete"))
_COVERAGE_NUMBERS = frozenset(("eligible_utterance_count", "eligible_utterance_count_at_request",
    "selected_utterance_count", "selected_start_ms", "selected_end_ms", "omitted_utterance_count", "input_chars",
    "max_input_chars", "max_utterances", "selection_as_of_ms", "appended_after_selection_count"))
_SAFE_INTEGER = 9_007_199_254_740_991


def _provider(value):
    if isinstance(value, str) and value.strip() in _SUMMARY_PROVIDERS:
        return value.strip()
    return "unknown"


def _effective_provider(content, row):
    if not isinstance(content, dict):
        return "unknown"
    for container, keys in ((content, ("provider", "_provider")), (row, ("provider", "_provider"))):
        for key in keys:
            if key not in container or container[key] is None or container[key] == "":
                continue
            # 잘못된 타입도 명시된 출처다. 필터 뒤 빈 객체가 되어 바깥 AI 출처로 승격하지 않는다.
            return _provider(container[key])
    return "unknown"


def _coverage(value):
    """회의 요약의 범위 메타만 허용한다. 요약 본문 허용목록을 중첩 범위에 재사용하지 않는다."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, expected in (("source", "final_utterances"), ("selection", "recent_bounded"),
                          ("question_boundary_status", "unclassified")):
        if value.get(key) == expected:
            out[key] = expected
    for key in _COVERAGE_FLAGS:
        if type(value.get(key)) is bool:
            out[key] = value[key]
    for key in _COVERAGE_NUMBERS:
        part = value.get(key)
        maximum = 9000 if key in ("input_chars", "max_input_chars") else 300 if key in ("selected_utterance_count", "max_utterances") else _SAFE_INTEGER
        if type(part) is int and 0 <= part <= maximum:
            out[key] = part
        elif key in ("selected_start_ms", "selected_end_ms") and key in value and part is None:
            out[key] = None
    ids = value.get("selected_utterance_ids")
    if (isinstance(ids, list) and len(ids) <= 300
            and all(type(uid) is int and 0 < uid <= _SAFE_INTEGER for uid in ids)
            and len(set(ids)) == len(ids)):
        out["selected_utterance_ids"] = list(ids)
    return out


def summary(value, *, decode_container=False, session_fields=False):
    """중첩 요약의 표시 계약만 보존한다. 사람이 쓴 본문 문자열은 손대지 않는다."""
    if isinstance(value, dict):
        out = {}
        for key, part in value.items():
            if key in ("provider", "_provider"):
                out[key] = _provider(part)
            elif key == "source_document_id":
                if type(part) is int and 0 < part <= _SAFE_INTEGER:
                    out[key] = part
            elif key == "content":
                out[key] = summary(part, decode_container=True, session_fields=session_fields)
            elif session_fields and key == "coverage":
                if isinstance(part, dict):
                    out[key] = _coverage(part)
            elif session_fields and key == "overview":
                if isinstance(part, str):
                    out[key] = part
            elif session_fields and key in ("org_points", "next_actions", "hashtags"):
                if isinstance(part, list):
                    out[key] = [entry for entry in part if isinstance(entry, str)]
            elif session_fields and key == "covered_until_ms":
                if type(part) is int and 0 <= part <= _SAFE_INTEGER:
                    out[key] = part
            elif key in SUMMARY_KEYS:
                out[key] = summary(part)
        if "content" in value:
            original = value["content"]
            try:
                original = json.loads(original) if isinstance(original, str) else original
            except (TypeError, ValueError):
                original = None
            # summaryContent는 행의 provider를 _provider로 투영하므로 양쪽 출처를 같은 우선순위로 고정한다.
            origin = _effective_provider(original, value)
            out["provider"] = origin
            public_content = out.get("content")
            if isinstance(public_content, dict):
                public_content["provider"] = origin
            elif isinstance(public_content, str):
                try:
                    decoded = json.loads(public_content)
                    if isinstance(decoded, dict):
                        decoded["provider"] = origin
                        out["content"] = json.dumps(decoded, ensure_ascii=False)
                except ValueError:
                    pass
        return out
    if isinstance(value, list):
        return [summary(part, session_fields=session_fields) for part in value]
    if isinstance(value, str) and decode_container:
        # 구 요약은 JSON 문자열로도 저장됐다. 내부 필드가 그대로 통과하지 않게 한다.
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(summary(json.loads(value), session_fields=session_fields), ensure_ascii=False)
            except ValueError:
                return "invalid_summary_container"
        if session_fields:
            return "invalid_summary_container"
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _session_summary(value, scope):
    """회별·롤링 요약 위치에서만 SessionSummary 필드를 공개한다. 블록 요약으로 확장하지 않는다."""
    allowed = scope in ("session", "rolling")
    container = value
    if isinstance(container, str):
        try:
            container = json.loads(container)
        except ValueError:
            container = None
    if isinstance(container, dict) and container.get("scope") not in (None, scope):
        allowed = False
    return summary(value, decode_container=True, session_fields=allowed)


def analysis(value):
    out = _pick(value, ("queue", "running", "running_session", "provider", "loaded", "last_provider", "last_latency_ms"))
    out["last_error"] = "AI 처리 오류가 있습니다. 원본 PC에서 확인하세요" if value.get("last_error") else ""
    return out


def session(row):
    out = _pick(row, ("id", "title", "committee", "agencies", "meeting_date", "status", "status_label",
        "source_type", "source_label", "save_audio", "started_at", "ended_at", "created_at", "notes", "stats"))
    out["source_config"] = _pick(row.get("source_config"), ("xcode", "xcgcd", "xsami", "quality"))
    out["source_state"] = {}
    return out


def utterance(row):
    if row is None:
        return None
    out = _pick(row, ("id", "session_id", "seq", "block_id", "ts_start", "ts_end", "time", "speaker",
        "speaker_role", "role_label", "speaker_source", "text", "edited", "is_final", "mentions",
        "source_order", "turn_id", "boundary_reason", "review_parts", "session_title", "meeting_date",
        "display_key", "display_stage", "stage", "document_id", "source_utterance_ids", "source_ids"))
    if "review_parts" in out:
        out["review_parts"] = [_pick(part, ("start", "end", "role", "speaker", "group_id", "text"))
                               for part in out["review_parts"]]
    return out


def block(row):
    if row is None:
        return None
    out = _pick(row, ("id", "session_id", "kind", "title", "questioner", "questioner_party", "status",
        "status_label", "name_basis", "ts_start", "ts_end", "time_range", "duration_sec", "is_open", "summary",
        "summary_at", "review_status", "reviewed_by", "reviewed_at", "question_ownership"))
    for key in ("summary", "question_ownership"):
        if key in out:
            out[key] = summary(out[key], decode_container=key == "summary")
    return out


def item(row, *, sensitive=False):
    out = _pick(row, ("id", "session_id", "session_title", "block_id", "utterance_id", "time", "kind",
        "kind_label", "strength", "text", "quote", "speaker", "requester", "assignee_dept", "due_date", "status",
        "status_label", "origin", "confidence", "note", "edited", "updated_at"))
    if not sensitive:
        out.update(assignee_dept="", note="")
    return out


def document(row):
    out = public_document(row)
    # 같은 원조각의 구 수신 행을 대체하는 공개 증명도 배지 드릴다운에 필요하다.
    if "source_supersessions" in row:
        proofs = []
        for proof in row["source_supersessions"] if isinstance(row["source_supersessions"], list) else []:
            if not isinstance(proof, dict):
                continue
            value = _pick(proof, ("method", "utterance_id", "source_key", "block_id", "original_text"))
            value["sources"] = [_pick(source, ("source_id", "source_key", "segment", "revision",
                "historical_revision", "historical_text")) for source in proof.get("sources", [])]
            replacements = []
            for replacement in proof.get("replacement_utterances", []):
                part = _pick(replacement, ("utterance_id", "source_key", "block_id", "original_text"))
                part["source_spans"] = [_pick(span, ("source_id", "start", "end"))
                                        for span in replacement.get("source_spans", [])]
                refs = replacement.get("source_revisions", {})
                part["source_revisions"] = {str(uid): revision for uid, revision in refs.items()
                                           if str(uid).isdigit() and type(revision) is int}
                replacements.append(part)
            value["replacement_utterances"] = replacements
            proofs.append(value)
        out["source_supersessions"] = proofs
    return out


def display_row(row):
    """서버가 저장한 공통 표시값만 전달한다. 여기서 단계 선택·현재성 판정은 하지 않는다."""
    out = _pick(row, ("id", "key", "session_id", "seq", "block_id", "stage", "text", "original_text", "utterance_id",
        "speaker", "speaker_role", "speaker_source", "role_label", "ts_start", "ts_end", "time", "is_final",
        "edited", "protected", "document_id", "source_order", "turn_id", "boundary_reason", "estimated",
        "display_key", "display_stage", "start", "end", "order"))
    for key in ("source_utterance_ids", "source_ids", "sentence_ids"):
        if isinstance(row.get(key), list):
            out[key] = [value for value in row[key] if isinstance(value, (str, int)) and not isinstance(value, bool)]
    if isinstance(row.get("source_spans"), list):
        out["source_spans"] = [_pick(span, ("source_id", "start", "end"))
                               for span in row["source_spans"] if isinstance(span, dict)]
    if isinstance(row.get("speaker_guess"), dict):
        out["speaker_guess"] = public_live_turn(row).get("speaker_guess")
    sort_key = row.get("sort_key")
    if (isinstance(sort_key, list) and len(sort_key) == 3
            and all(type(value) is int and -9_007_199_254_740_991 <= value <= 9_007_199_254_740_991
                    for value in sort_key)):
        out["sort_key"] = list(sort_key)
    return out


def display_block(row):
    """공통 보정 카드와 출처 좌표의 공개 허용목록이다. 모델 원응답은 제외한다."""
    out = _pick(row, ("blockId", "block_id", "id", "display_generation"))
    if isinstance(row.get("fingerprint"), str) and re.fullmatch(r"[a-fA-F0-9]{32,128}", row["fingerprint"]):
        out["fingerprint"] = row["fingerprint"]
    if isinstance(row.get("document"), dict):
        out["document"] = document(row["document"])
    out["summary"] = summary(row.get("summary"))
    out["cards"] = [display_row(card) for card in row.get("cards", []) if isinstance(card, dict)]
    ranges = row.get("consumed_utterance_ranges")
    out["consumed_utterance_ranges"] = {str(uid): [_pick(span, ("start", "end")) for span in spans
        if isinstance(span, dict)] for uid, spans in ranges.items() if str(uid).isdigit() and isinstance(spans, list)
        } if isinstance(ranges, dict) else {}
    return out


def display_fields(data):
    """원본·릴레이·내보내기가 공유하는 저장 표시본의 공개 전송 형태를 유지한다."""
    out = _pick(data, ("display_generation", "display_contract", "display_version",
                       "display_valid_block_ids", "display_valid_row_ids", "valid_document_ids",
                       "display_utterance_ids"))
    if "display_rows" in data:
        out["display_rows"] = [display_row(row) for row in data["display_rows"] if isinstance(row, dict)]
    if "display_blocks" in data:
        out["display_blocks"] = [display_block(row) for row in data["display_blocks"] if isinstance(row, dict)]
    return out


def project(path, data, *, sensitive=False):
    """허용된 로컬 GET의 응답만 새 객체로 만든다. 알 수 없는 경로는 거절한다."""
    if not isinstance(data, dict):
        raise ValueError("원본 조회 응답 객체가 필요합니다")
    if path == "sessions":
        return {"items": [session(row) for row in data.get("items", []) if row.get("source_type") != "replay"]}
    if path.startswith("sessions/") and path.endswith("/snapshot"):
        out = _pick(data, ("alerts", "summary", "rolling", "roster", "bookmarks", "last_event_id", "me", "utterance_page",
                           "valid_utterance_ids"))
        for key in ("summary", "rolling"):
            if key in out:
                out[key] = _session_summary(out[key], "session" if key == "summary" else "rolling")
        out.update(session=session(data["session"]), blocks=[block(row) for row in data.get("blocks", [])],
            utterances=[utterance(row) for row in data.get("utterances", [])],
            items=[item(row, sensitive=sensitive) for row in data.get("items", [])], analysis=None)
        if "sources" in data:
            out["sources"] = [public_source(row) for row in data["sources"]]
        out.update(display_fields(data))
        return out
    if path.startswith("sessions/") and path.endswith("/transcript-stages"):
        out = _pick(data, ("version", "mode", "snapshot_at", "valid_document_ids", "valid_live_turn_ids",
            "next_before", "block_id", "source_page"))
        out.update(sources=[public_source(row) for row in data.get("sources", [])],
            documents=[document(row) for row in data.get("documents", [])],
            live_turns=[public_live_turn(row) for row in data.get("live_turns", [])])
        if "utterances" in data:
            out["utterances"] = [utterance(row) for row in data["utterances"]]
        if "legacy_utterances" in data:
            out["legacy_utterances"] = [utterance(row) for row in data["legacy_utterances"]]
        if "blocks" in data:
            out["blocks"] = [block(row) for row in data["blocks"]]
        out.update(display_fields(data))
        return out
    if path.startswith("sessions/") and path.count("/") == 1:
        return session(data)
    if path.startswith("utterances/") and path.count("/") == 1:
        out = {"utterance": utterance(data["utterance"]), "session": session(data["session"]),
            "block": block(data.get("block")), "prev": utterance(data.get("prev")), "next": utterance(data.get("next")),
            "same_speaker": [utterance(row) for row in data.get("same_speaker", [])],
            "items": [item(row, sensitive=sensitive) for row in data.get("items", [])], "bookmarked": bool(data.get("bookmarked"))}
        out.update(display_fields(data))
        return out
    if path == "items":
        return {"items": [item(row, sensitive=sensitive) for row in data.get("items", [])]}
    if path == "search":
        return {"q": data.get("q", ""), "items": [utterance(row) for row in data.get("items", [])]}
    raise ValueError("허용되지 않은 원본 조회입니다")


def event(type_, data, *, sensitive=False):
    """입력 세대는 정수만 전달하고 공개 본문은 허용 목록으로 제한한다."""
    out = _event(type_, data, sensitive=sensitive)
    generation = data.get("display_input_generation")
    if type(generation) is int and 0 <= generation <= 9_007_199_254_740_991:
        out["display_input_generation"] = generation
    return out


def _event(type_, data, *, sensitive=False):
    """문서 투영을 만들지 않는 짧은 SSE 알림에도 같은 공개 규칙을 적용한다."""
    if type_ == "session.status":
        return session(data)
    if type_ == "utterance.upsert":
        return utterance(data)
    if type_ == "block.upsert":
        return block(data)
    if type_ == "item.upsert":
        return item(data, sensitive=sensitive)
    if type_ == "caption.source":
        out = public_source(data)
        out["invalidated_document_ids"] = [uid for uid in data.get("invalidated_document_ids", [])
                                           if type(uid) is int and uid > 0]
        return out
    if type_ == "summary.update":
        return {"scope": data.get("scope"), "summary": _session_summary(data.get("summary"), data.get("scope"))}
    if type_ == "analysis.status":
        return analysis(data)
    if type_ == "transcript.display":
        return _pick(data, ("generation", "display_generation"))
    if type_ == "alert":
        return _pick(data, ("id", "session_id", "utterance_id", "keyword", "category", "ack", "speaker", "text", "time"))
    if type_ == "stats":
        return _pick(data, ("utterances", "blocks", "questions", "question_blocks", "needs_name", "alerts_unack", "items", "alerts", "ts_first", "ts_last",
                            "duration_sec", "speakers", "final_utterances", "open_items"))
    return _pick(data, ("id", "utterance_id", "block_id", "reason"))
