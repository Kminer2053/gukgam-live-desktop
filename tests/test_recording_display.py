"""녹음 표시본의 원문 보존·문서 현재성·사람 보호를 합성 입력으로 검증한다."""
from copy import deepcopy
import json

import pytest

from gukgam.display_materializer import _BuildRepo, _input_bundle, build
from gukgam.display_reads import display_utterances, stage_page
from gukgam.recording_display import build_recording_display
from gukgam.recording_sources import recording_sources
from gukgam.repo_stages import StageRepoMixin
from gukgam import serialize


class RecordingRepo:
    def __init__(self):
        self.rows = [
            {"id": 101, "session_id": "r", "seq": 1, "block_id": 7, "ts_start": 1000,
             "ts_end": 2000, "source_key": "file:1", "text": "자료를 제출하시겠습니까?",
             "original_text": "자료를 제출하시겠습니까?", "is_final": True,
             "speaker": "질의 의원", "speaker_role": "member", "speaker_source": "inferred",
             "source_speaker_hint": "hwa:nemo:0"},
            {"id": 102, "session_id": "r", "seq": 2, "block_id": 7, "ts_start": 2000,
             "ts_end": 3000, "source_key": "file:2", "text": "예, 제출하겠습니다.",
             "original_text": "예, 제출하겠습니다.", "is_final": True,
             "speaker": "답변 기관", "speaker_role": "answer", "speaker_source": "inferred",
             "source_speaker_hint": "hwa:nemo:1"}]
        self.blocks = [{"id": 7, "session_id": "r", "kind": "question", "is_open": False,
                        "questioner": "질의 의원", "ts_start": 1000, "ts_end": 3000,
                        "summary": {"provider": "legacy", "one_line": "과거 자동 요약"}}]
        sources = recording_sources(self.rows)
        self.documents = [{
            "id": 20, "session_id": "r", "source_kind": "recording_asr",
            "mode": "closed_block_v1", "status": "draft", "block_id": 7,
            "questioner": "질의 의원", "updated_at": 10,
            "source_ids": [s["id"] for s in sources],
            "source_revisions": {str(s["id"]): s["revision"] for s in sources},
            "source_hashes": {str(s["id"]): s["source_hash"] for s in sources},
            "sentences": [
                {"id": "s1", "text": "자료 제출을 요청합니다.", "role": "member", "speaker": "질의 의원",
                 "source_spans": [{"source_id": 101, "start": 0, "end": len(self.rows[0]["text"])}]},
                {"id": "s2", "text": "예. 제출하겠습니다.", "role": "answer", "speaker": "답변 기관",
                 "source_spans": [{"source_id": 102, "start": 0, "end": len(self.rows[1]["text"])}]}],
            "paragraphs": [], "summary": {"status": "draft", "title": "자료 제출 확인",
                                             "bullets": ["자료 제출을 요청했고 제출하겠다고 답변함."]}}]
        self.current_checks = []
        self.stage_summary_value = StageRepoMixin.stage_summary_value

    def get_session(self, sid):
        return {"id": sid, "source_type": "file"}

    def list_utterances(self, sid, **kwargs):
        assert sid == "r" and kwargs == {"limit": -1}
        return self.rows

    def display_build_blocks(self, sid):
        return self.blocks

    def list_stage_documents(self, sid):
        return self.documents

    def get_roster(self, sid):
        return []

    def stage_document_current(self, sid, did):
        self.current_checks.append(did)
        doc = next(d for d in self.documents if d["id"] == did)
        sources = {s["id"]: s for s in recording_sources(self.rows)}
        return (not doc.get("invalid") and all(
            uid in sources and sources[uid]["revision"] == doc["source_revisions"].get(str(uid))
            and sources[uid]["source_hash"] == doc["source_hashes"].get(str(uid))
            for uid in doc["source_ids"]))

    def __getattr__(self, name):
        if "caption" in name or name in {"stage_raw_segments", "display_document_source_ids"}:
            raise AssertionError(f"녹음 표시가 자막 전용 조회를 시도함: {name}")
        raise AttributeError(name)


def test_corrected_body_summary_and_identity_sources_share_one_document():
    repo = RecordingRepo()
    before = deepcopy((repo.rows, repo.blocks, repo.documents))
    payload, stage2, cache = build_recording_display(repo, "r", 3)
    assert (repo.rows, repo.blocks, repo.documents) == before
    assert payload["display_generation"] == 3 and payload["source_kind"] == "recording_asr"
    assert [r["stage"] for r in payload["display_rows"]] == [3, 3]
    assert [r["text"] for r in payload["display_rows"]] == ["자료 제출을 요청합니다.", "예. 제출하겠습니다."]
    assert payload["blocks"][0]["summary"]["source_document_id"] == 20
    assert payload["blocks"][0]["summary"]["one_line"] == "자료 제출 확인"
    assert payload["display_blocks"][0]["summary"]["source_document_id"] == 20
    for source, turn, row in zip(payload["sources"], payload["live_turns"], repo.rows):
        assert source["id"] == turn["id"] == row["id"]
        assert source["origin_utterance_id"] == row["id"]
        assert source["text"] == turn["original_text"] == row["original_text"]
        assert turn["source_spans"] == [{"source_id": row["id"], "start": 0, "end": len(row["text"])}]
    assert [row["acoustic_speaker_id"] for row in payload["display_rows"]] == ["nemo:0", "nemo:1"]
    assert all(json.loads(row["payload"])["source_kind"] == "recording_asr" for row in stage2)
    assert json.loads(cache[20])["valid"] is True


@pytest.mark.parametrize("mutation", ["stale", "revision", "foreign_session", "caption_kind", "bad_span"])
def test_invalid_documents_fall_back_without_stale_automatic_summary(mutation):
    repo = RecordingRepo()
    document = repo.documents[0]
    if mutation == "stale":
        document["status"] = "stale"
    elif mutation == "revision":
        repo.rows[0]["original_text"] = repo.rows[0]["text"] = "새 원문입니다."
    elif mutation == "foreign_session":
        document["session_id"] = "another-recording"
    elif mutation == "caption_kind":
        document["source_kind"] = "assembly_caption"
    else:
        document["sentences"][0]["source_spans"][0]["end"] = 99999
    payload, _, _ = build_recording_display(repo, "r", 1)
    assert payload["display_blocks"] == []
    assert all(row["stage"] == 2 for row in payload["display_rows"])
    assert [row["text"] for row in payload["display_rows"]] == [row["text"] for row in repo.rows]
    assert payload["blocks"][0]["summary"] is None


@pytest.mark.parametrize("protection", ["body", "speaker", "reviewed_speaker", "parts", "block", "summary"])
def test_manual_and_reviewed_edits_survive_without_automatic_replacement(protection):
    repo = RecordingRepo()
    row, block = repo.rows[0], repo.blocks[0]
    if protection == "body":
        row["text"] = "사람이 직접 수정한 본문"
    elif protection == "speaker":
        row.update(speaker="사람 지정 화자", speaker_source="manual")
    elif protection == "reviewed_speaker":
        row.update(speaker="검수된 화자", speaker_source="reviewed")
    elif protection == "parts":
        row["review_parts"] = [{"start": 0, "end": len(row["text"]), "speaker": "사람 지정", "role": "member"}]
    elif protection == "block":
        block["review_status"] = "reviewed"
    else:
        block["summary"] = {"edited_by": "검수자", "one_line": "사람 요약"}
    before = deepcopy((repo.rows, repo.blocks))
    payload, _, _ = build_recording_display(repo, "r", 1)
    assert payload["display_blocks"] == []
    assert all(item["stage"] == 2 for item in payload["display_rows"])
    assert (repo.rows, repo.blocks) == before
    if protection in {"block", "summary"}:
        assert payload["blocks"][0]["summary"] == block["summary"]
    else:
        assert payload["blocks"][0]["summary"] is None
    assert "".join(item["text"] for item in payload["display_rows"]) == "".join(item["text"] for item in repo.rows)


def test_latest_processing_document_blocks_older_projection_until_ready():
    repo = RecordingRepo()
    newer = deepcopy(repo.documents[0])
    newer.update(id=21, updated_at=11, status="processing", sentences=[], paragraphs=[])
    repo.documents.append(newer)
    payload, _, _ = build_recording_display(repo, "r", 1)
    assert payload["display_blocks"] == [] and payload["blocks"][0]["summary"] is None
    assert all(row["stage"] == 2 for row in payload["display_rows"])


def test_held_summary_hidden_while_corrected_body_remains_visible():
    repo = RecordingRepo()
    repo.documents[0]["summary"]["status"] = "held"
    payload, _, _ = build_recording_display(repo, "r", 1)
    assert len(payload["display_blocks"]) == 1
    assert payload["display_blocks"][0]["summary"] is None
    assert payload["blocks"][0]["summary"] is None


def test_partial_projection_keeps_uncovered_original_rows_and_metadata():
    repo = RecordingRepo()
    repo.documents[0]["status"] = "partial"
    repo.documents[0]["sentences"].pop()
    payload, _, _ = build_recording_display(repo, "r", 1)
    assert [row["stage"] for row in payload["display_rows"]] == [3, 2]
    assert payload["display_rows"][1]["text"] == repo.rows[1]["original_text"]
    assert payload["display_rows"][1]["acoustic_speaker_id"] == "nemo:1"


def test_stage_page_and_export_utterances_use_same_projection_and_preserve_originals():
    repo = RecordingRepo()
    payload, _, _ = build_recording_display(repo, "r", 1)
    page = stage_page(payload, block_id=7)
    assert page["display_rows"] == payload["display_rows"]
    exported = [serialize.utterance(row) for row in display_utterances(payload, "r")]
    assert [row["text"] for row in exported] == [row["text"] for row in payload["display_rows"]]
    assert [row["acoustic_speaker_id"] for row in exported] == ["nemo:0", "nemo:1"]
    assert [row["original_text"] for row in payload["utterances"]] == [row["original_text"] for row in repo.rows]


def test_dispatch_and_cached_bundle_never_query_caption_tables_and_recheck_currentness():
    repo = RecordingRepo()
    bundle = _input_bundle(repo, "r")
    assert bundle["segments"] == [] and bundle["sources"][0]["id"] == 101
    view = _BuildRepo(repo, bundle, {})
    payload, _, cache = build(view, "r", 1)
    assert payload["display_blocks"][0]["id"] == 20
    repo.documents[0]["invalid"] = True
    payload2, _, _ = build(view, "r", 2, previous_payload=payload,
                           previous_cache={key: json.loads(value) for key, value in cache.items()}, changes=[])
    assert repo.current_checks == [20, 20]
    assert payload2["display_blocks"] == [] and payload2["blocks"][0]["summary"] is None


def test_incremental_bundle_rebuilds_recording_source_revision_after_original_change():
    repo = RecordingRepo()
    previous = deepcopy(_input_bundle(repo, "r"))
    repo.rows[0]["text"] = repo.rows[0]["original_text"] = "뒤늦게 확정된 원문입니다."
    current = _input_bundle(repo, "r", previous, [{"kind": "utterances", "row_key": "101"}])
    assert previous["sources"][0]["text"] == "자료를 제출하시겠습니까?"
    assert current["sources"][0]["text"] == "뒤늦게 확정된 원문입니다."
    assert current["sources"][0]["revision"] != previous["sources"][0]["revision"]
    assert current["sources"][0]["source_hash"] != previous["sources"][0]["source_hash"]
    payload, _, _ = build(_BuildRepo(repo, current, {}), "r", 2)
    assert payload["display_blocks"] == [] and payload["blocks"][0]["summary"] is None


def test_latest_valid_document_selects_body_and_summary_together():
    repo = RecordingRepo()
    newer = deepcopy(repo.documents[0])
    newer.update(id=21, updated_at=11)
    newer["sentences"][0]["text"] = "새 판본의 자료 제출 요청입니다."
    newer["summary"]["title"] = "새 판본 요약"
    repo.documents.insert(0, newer)
    payload, _, _ = build_recording_display(repo, "r", 1)
    assert [row["id"] for row in payload["display_blocks"]] == [21]
    assert payload["display_rows"][0]["text"] == "새 판본의 자료 제출 요청입니다."
    assert payload["blocks"][0]["summary"]["source_document_id"] == 21
    assert payload["blocks"][0]["summary"]["one_line"] == "새 판본 요약"
