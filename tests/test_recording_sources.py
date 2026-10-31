"""녹음 가상 출처의 원문·좌표·판본·완료 및 사람 수정 보호 회귀."""
from copy import deepcopy

import pytest

from gukgam.block_correction import _restore_coordinates, _selected_sources
from gukgam.recording_sources import (recording_source, recording_source_ready,
                                      recording_sources, select_recording_sources)
from gukgam.stage2_sentences import split_sentences


def row(uid, body, *, block_id=2, **extra):
    return {"id": uid, "session_id": "recording-test", "seq": uid, "block_id": block_id,
            "source_key": f"nd-{uid * 1000}-{uid * 1000 + 900}#0", "ts_start": uid * 1000,
            "ts_end": uid * 1000 + 900, "text": body, "original_text": body,
            "is_final": True, "speaker_role": "unknown", "speaker_source": "inferred", **extra}


def block(**extra):
    return {"id": 2, "kind": "question", "is_open": False, "questioner": "", **extra}


def select(rows, *, target=None, blocks=None, roster=(), complete=True):
    target = target or block()
    return select_recording_sources(target, blocks or [target], rows, roster,
                                    recording_complete=complete)


def test_virtual_source_preserves_original_identity_time_acoustic_and_is_deep_copy():
    original = row(7, "금액이 얼마입니까?", source_speaker_hint="hwa:nemo:2",
                   diarization={"speakers": ["speaker_2"], "complete": True})
    frozen = deepcopy(original)
    source = recording_source(original)
    assert original == frozen
    assert source["id"] == source["origin_utterance_id"] == 7
    assert source["source_kind"] == "recording_asr"
    assert source["source_key"] == original["source_key"]
    assert source["ts_start"] == source["first_rx"] == 7000
    assert source["ts_end"] == source["stable_at"] == 7900
    assert source["parts"] == [{"label": "nemo:2", "text": original["original_text"]}]
    assert source["acoustic_speaker_id"] == "nemo:2"
    assert source["recording_metadata"]["source_speaker_hint"] == "hwa:nemo:2"
    assert "scd" not in source and source["ground_truth_verified"] is False
    assert 0 < source["revision"] <= 2 ** 48 < 2 ** 53
    assert recording_source_ready(source)
    source["recording_metadata"]["diarization"]["speakers"].append("speaker_3")
    assert original == frozen
    assert not recording_source_ready(source)


def test_public_rows_keep_overlap_and_unknown_without_inventing_identity():
    overlap = recording_source(row(1, "네 맞습니다", acoustic_overlap=True,
                                   acoustic_speaker_label="겹친 발언 · 화자 확인 필요"))
    unknown = recording_source(row(2, "확인하겠습니다", acoustic_overlap=False,
                                   acoustic_speaker_label="화자 미정"))
    for source in (overlap, unknown):
        assert source["parts"][0]["label"] is None
        assert "acoustic_speaker_id" not in source
        assert recording_source_ready(source)
    assert overlap["acoustic_overlap"] is True
    assert unknown["acoustic_speaker_label"] == "화자 미정"


def test_hash_is_deterministic_and_acoustic_change_creates_new_revision():
    original = row(1, "질의합니다", acoustic_speaker_id="nemo:1")
    first = recording_source(original)
    same = recording_source(dict(reversed(list(original.items()))))
    changed = recording_source({**original, "acoustic_speaker_id": "nemo:2"})
    assert first["source_hash"] == same["source_hash"]
    assert first["revision"] == same["revision"]
    assert changed["source_hash"] != first["source_hash"]
    assert changed["revision"] != first["revision"]


@pytest.mark.parametrize("change", [
    {"text": "누락"}, {"original_text": "누락"}, {"id": True}, {"ts_start": True},
    {"ts_end": 9999}, {"final": 1}, {"is_final": False}, {"revision": 1},
    {"source_hash": "0" * 64}, {"source_kind": "assembly_caption"}, {"scd": True},
    {"parts": [{"label": "nemo:4", "text": "자료를 제출하세요"}]},
])
def test_ready_rejects_tampered_source_even_without_scd(change):
    source = recording_source(row(1, "자료를 제출하세요", acoustic_speaker_id="nemo:1"))
    source.update(change)
    assert not recording_source_ready(source)


@pytest.mark.parametrize("change", [
    {"id": True}, {"id": -1}, {"seq": 0}, {"ts_start": True}, {"ts_end": 0},
    {"is_final": "1"}, {"acoustic_speaker_id": "nemo:8"}, {"acoustic_overlap": "true"},
    {"acoustic_speaker_id": "nemo:2", "source_speaker_hint": "hwa:nemo:1"},
])
def test_malformed_origin_is_not_silently_repaired(change):
    with pytest.raises(ValueError):
        recording_source(row(1, "원문입니다", **change))


@pytest.mark.parametrize("change", [
    {"edited": True}, {"text": "사람이 고친 문장"}, {"speaker_source": "manual"},
    {"speaker_source": "reviewed"}, {"review_parts": [{"start": 0, "end": 1}]},
    {"review_status": "reviewed"}, {"edited_by": "operator"},
])
def test_manual_or_edited_source_keeps_original_and_blocks_model_input(change):
    original = row(1, "자동 처리 전 원문", **change)
    source = recording_source(original)
    assert source["text"] == "자동 처리 전 원문"
    assert source["protected"]
    assert not recording_source_ready(source)
    result = select([original])
    assert result["status"] == "excluded" and "protected" in result["reasons"]


def test_system_markers_are_not_model_body_and_duplicate_ids_or_seq_are_rejected():
    assert recording_source(row(9, "구간 표지", speaker_role="system")) is None
    assert [s["id"] for s in recording_sources([row(2, "둘"), row(1, "하나")])] == [1, 2]
    with pytest.raises(ValueError):
        recording_sources([row(1, "하나"), row(1, "둘")])
    with pytest.raises(ValueError):
        recording_sources([row(1, "하나"), row(2, "둘", seq=1)])


def test_eof_is_explicit_and_unknown_questioner_does_not_drop_the_closed_body():
    rows = [row(1, "질의하겠습니다."), row(2, "답변드립니다.")]
    before = deepcopy(rows)
    result = select(rows)
    assert result["status"] == "ready"
    assert result["question_identity"]["status"] == "needs_name"
    assert result["question_identity"]["questioner"] == ""
    assert result["boundary_evidence"]["kind"] == "recording_eof"
    assert [s["selected_text"] for s in result["sources"]] == [r["text"] for r in rows]
    assert rows == before
    pending = select(rows, complete=False)
    assert pending["status"] == "excluded" and "recording_not_complete" in pending["reasons"]
    assert select(rows, target=block(is_open=0))["status"] == "ready"
    assert select(rows, target=block(is_open=True))["status"] == "excluded"


def test_whole_body_keeps_final_answer_and_excludes_next_call_and_next_member():
    target = block()
    previous = {"id": 1, "kind": "other", "is_open": False}
    following = {"id": 3, "kind": "question", "is_open": False, "questioner": "김영수"}
    roster = [{"name": "이민수", "role": "member", "party": "무소속"}]
    rows = [row(1, "다음은 이민수 위원님 추가질의해 주시겠습니다.", block_id=1),
            row(2, "이유가 무엇입니까?", source_speaker_hint="hwa:nemo:0"),
            row(3, "예, 검토하지 않았습니다. 다음은 김영수 위원님 질의해 주시겠습니다. 다음 발언입니다.",
                source_speaker_hint="hwa:nemo:1"),
            row(4, "새 질의를 시작합니다.", block_id=3)]
    result = select(rows, blocks=[previous, target, following], roster=roster)
    assert result["status"] == "ready"
    assert result["question_identity"]["questioner"] == "이민수"
    assert result["question_identity"]["ground_truth_verified"] is False
    assert [s["id"] for s in result["sources"]] == [2, 3]
    assert result["sources"][-1]["selected_text"] == "예, 검토하지 않았습니다. "
    assert result["dependency_source_ids"] == [1, 2, 3, 4]
    assert result["boundary_evidence"]["end_anchor"]["name"] == "김영수"
    assert all("scd" not in s for s in result["sources"])
    assert all(recording_source_ready(s) for s in result["sources"])
    # 실제 보정기의 문장 분할·선택 좌표 복원까지 원문과 정확히 일치한다.
    selected, mappings = _selected_sources(result["sources"])
    sentences = split_sentences(selected, split_provider_parts=True)
    _restore_coordinates(result["sources"], sentences, mappings)
    by_id = {s["id"]: s for s in result["sources"]}
    for sentence in sentences:
        assert sentence["original_text"] == "".join(by_id[p["source_id"]]["text"][p["start"]:p["end"]]
                                                     for p in sentence["source_spans"])
    assert [(p["source_id"], i) for sentence in sentences for p in sentence["source_spans"]
            for i in range(p["start"], p["end"])] == [(p["source_id"], i) for p in result["source_spans"]
                                                     for i in range(p["start"], p["end"])]


def test_inline_start_and_cross_row_closing_call_preserve_substring_coordinates():
    rows = [row(1, "네, 다음은 이민수 위원님 질의해 주시겠습니다. 자료를 제출하세요."),
            row(2, "제출하겠습니다. 다음은"), row(3, "김영수 위원님 질의해 주시겠습니다. 새 질의입니다.")]
    result = select(rows)
    assert result["status"] == "ready"
    assert [s["selected_text"] for s in result["sources"]] == [" 자료를 제출하세요.", "제출하겠습니다. "]
    assert result["boundary_evidence"]["end_anchor"]["source_spans"][-1]["source_id"] == 3


@pytest.mark.parametrize("text", [
    '보고서에는 “다음은 김영수 위원님 질의해 주시겠습니다”라고 적혀 있습니다. 이 설명이 맞습니까?',
    '어제 김영수 위원님 질의해 주시겠습니다라고 말한 것을 기억합니다. 답변해 주세요.',
])
def test_reported_or_quoted_calls_do_not_truncate_recording_body(text):
    result = select([row(1, text), row(2, "그렇습니다.")])
    assert result["status"] == "ready"
    assert result["boundary_evidence"]["end_anchor"] is None
    assert result["sources"][0]["selected_text"] == text


def test_unknown_or_ambiguous_call_name_stays_unknown_without_fuzzy_match():
    previous = {"id": 1, "kind": "chair", "is_open": False}
    rows = [row(1, "다음은 이민구 위원님 질의해 주시겠습니다.", block_id=1), row(2, "질의합니다.")]
    result = select(rows, blocks=[previous, block()], roster=[{"name": "이민수", "role": "member"}])
    assert result["status"] == "ready"
    assert result["question_identity"]["questioner"] == ""
    assert result["question_identity"]["raw_call_name"] == "이민구"
    duplicate = select(rows, blocks=[previous, block()], roster=[{"name": "이민구", "role": "member"}] * 2)
    assert duplicate["question_identity"]["questioner"] == ""


def test_explicit_unique_roster_floor_frame_keeps_asr_spelling_and_exact_coordinates():
    previous = {"id": 1, "kind": "other", "is_open": False}
    announcement = "네 다음은 존경하는 이민수 의원님 추가질이 해주시겠습니다."
    rows = [row(1, announcement, block_id=1), row(2, "자료가 없습니까?"),
            row(3, "없습니다. 다음은 존경하는 김영수 의원님 추가지리 해주시겠습니다. 새 발언입니다.")]
    roster = [{"name": name, "role": "member"} for name in ("이민수", "김영수")]
    result = select(rows, blocks=[previous, block()], roster=roster)
    assert result["status"] == "ready"
    assert result["question_identity"]["questioner"] == "이민수"
    assert result["question_identity"]["uncertain"] is True
    assert result["sources"][-1]["selected_text"] == "없습니다. "
    anchor = result["boundary_evidence"]["start_anchor"]
    assert anchor["origin"] == "recording_explicit_floor_frame"
    assert announcement[anchor["source_spans"][0]["start"]:anchor["source_spans"][0]["end"]] == announcement[2:]
    assert rows[0]["text"] == announcement and "추가질이" in rows[0]["text"]
    unknown = select(rows, blocks=[previous, block()], roster=[])
    assert unknown["question_identity"]["questioner"] == ""
    ambiguous = select(rows, blocks=[previous, block()], roster=[roster[0], roster[0], roster[1]])
    assert ambiguous["question_identity"]["questioner"] == ""


@pytest.mark.parametrize("body", [
    '보고서에 “다음은 존경하는 이민수 의원님 추가질이 해주시겠습니다”라고 적혀 있습니다. 답변해 주세요.',
    '어제 다음은 존경하는 이민수 의원님 추가질이 해주시겠습니다라고 말한 것을 기억합니다. 확인해 주세요.',
    '다음은 존경하는 이민수 의원님 입장을 여쭙겠습니다. 이것은 의원에게 묻는 질문입니다.',
])
def test_recording_floor_frame_does_not_turn_reported_or_unfinished_request_into_boundary(body):
    result = select([row(1, body), row(2, "답변드립니다.")], roster=[{"name": "이민수", "role": "member"}])
    assert result["sources"][0]["selected_text"] == body
    assert result["boundary_evidence"]["start_anchor"] is None
    assert result["boundary_evidence"]["end_anchor"] is None


def test_unique_same_floor_call_restores_question_prefix_from_previous_other_block():
    previous = {"id": 1, "kind": "other", "is_open": False}
    roster = [{"name": "이민수", "role": "member"}]
    rows = [row(1, "앞 의원님 수고하셨습니다. 다음은 존경하는 이민수 의원님 추가질이 해주시겠습니다. 첫 질문입니다.", block_id=1),
            row(2, "장관님 자료부터 확인하겠습니다.", block_id=1),
            row(3, "이 자료의 금액이 맞습니까?"), row(4, "그렇습니다.")]
    result = select(rows, blocks=[previous, block()], roster=roster)
    assert result["status"] == "ready"
    assert [source["id"] for source in result["sources"]] == [1, 2, 3, 4]
    assert result["sources"][0]["selected_text"] == " 첫 질문입니다."
    assert result["metrics"]["restored_utterance_count"] == 2
    assert result["utterance_membership"] == result["membership"]
    assert [m["original_block_id"] for m in result["membership"]] == [1, 1, 2, 2]
    assert [m["restored_from_boundary"] for m in result["membership"]] == [True, True, False, False]
    assert result["boundary_evidence"]["restored_previous_block_id"] == 1


@pytest.mark.parametrize("condition", ["unknown_roster", "duplicate_roster", "other_questioner", "previous_question"])
def test_prefix_restore_requires_unique_same_owner_and_nonquestion_previous_block(condition):
    previous = {"id": 1, "kind": "other", "is_open": False}
    target = block()
    roster = [{"name": "이민수", "role": "member"}]
    if condition == "unknown_roster":
        roster = []
    elif condition == "duplicate_roster":
        roster *= 2
    elif condition == "other_questioner":
        target = block(questioner="김영수")
    else:
        previous["kind"] = "question"
    rows = [row(1, "다음은 존경하는 이민수 의원님 추가질이 해주시겠습니다. 앞부분 질문입니다.", block_id=1),
            row(2, "이어지는 질문입니다.")]
    result = select(rows, target=target, blocks=[previous, target], roster=roster)
    assert [source["id"] for source in result["sources"]] == [2]
    assert result["metrics"]["restored_utterance_count"] == 0
    if condition == "other_questioner":
        assert result["status"] == "excluded" and "start_call_name_conflict" in result["reasons"]


@pytest.mark.parametrize("change", [{"edited": True}, {"speaker_source": "manual"}, {"is_final": False}])
def test_restored_prefix_obeys_the_same_manual_and_final_protection(change):
    previous = {"id": 1, "kind": "chair", "is_open": False}
    rows = [row(1, "다음은 이민수 위원님 질의해 주시겠습니다.", block_id=1),
            row(2, "원래 앞부분 질문", block_id=1, **change), row(3, "이어서 질문합니다.")]
    result = select(rows, blocks=[previous, block()], roster=[{"name": "이민수", "role": "member"}])
    assert result["status"] == "excluded"
    assert "protected" in result["reasons"] or "utterance_not_final" in result["reasons"]


@pytest.mark.parametrize("change", [{"review_status": "reviewed"}, {"summary": {"edited_by": "operator"}}])
def test_block_protection_prevents_ready_and_changes_stamp(change):
    rows = [row(1, "질의합니다.")]
    initial = select(rows)
    protected = select(rows, target=block(**change))
    assert protected["status"] == "excluded" and "protected" in protected["reasons"]
    assert protected["selection_stamp"] != initial["selection_stamp"]


def test_ai_summary_does_not_invalidate_own_document_but_source_and_membership_changes_do():
    rows = [row(1, "질의합니다.")]
    initial = select(rows)
    summarized = select(rows, target=block(summary={"one_line": "새 AI 요약", "generated_at": 9000}, summary_at=9000))
    assert summarized["selection_stamp"] == initial["selection_stamp"]
    revised = select([row(1, "질의합니다!", acoustic_speaker_id="nemo:1")])
    assert revised["selection_stamp"] != initial["selection_stamp"]
    moved = select([row(1, "질의합니다.", block_id=3)])
    assert moved["selection_stamp"] != initial["selection_stamp"]


def test_unfinished_row_and_interleaved_foreign_block_are_not_silently_accepted():
    unfinished = select([row(1, "미완료", is_final=False)])
    assert "utterance_not_final" in unfinished["reasons"]
    interleaved = select([row(1, "질문"), row(2, "다른 질의", block_id=3), row(3, "답변")])
    assert "block_membership_interleaved" in interleaved["reasons"]
