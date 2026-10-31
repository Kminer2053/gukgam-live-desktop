"""기존 발언과 표시 전용 화자 후보를 읽기 투영한다. 확정·저장·모델 호출은 하지 않는다."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import re

from .block_sources import _compact, map_utterance_sources
from .nlp.floor import completed_closing_call, scan_anchors, call_fragment_order, reported_call_context
from .nlp.role_evidence import role_evidence
from .stage_mirror import public_live_turn


@lru_cache(maxsize=2048)
def _text_analysis(text: str, roster_key: tuple) -> tuple[dict, tuple, tuple[str, ...], bool]:
    """본문·명단이 같은 순수 의미분석만 제한 보관한다. 출처·판본·소유권은 보관하지 않는다."""
    roster = [{"name": name, "role": role} for name, role in roster_key]
    anchors = tuple(scan_anchors(text, roster))
    # 명단 전체를 다시 탐색하지 않고 실제 완전 호명의 대상 이름만 검사한다.
    called = tuple(dict.fromkeys(anchor["speaker"] for anchor in anchors if anchor["kind"] == "call"
        and completed_closing_call(text, roster, anchor["speaker"])))
    return role_evidence(text), anchors, called, reported_call_context(text)


def session_live_turns(repo, sid: str, *, checkpoint: dict | None = None) -> list[dict]:
    """Repo의 읽기 메서드만 사용한다. 레거시·시험 저장소에 새 출처가 없으면 비워 둔다."""
    if not all(hasattr(repo, name) for name in ("all_caption_sources", "stage_raw_segments", "list_utterances", "list_blocks")):
        return []
    # 실황 화자 후보에는 자동 요약 본문이 필요 없다. 사람 요약·검수 보호는 작은 헤더에 남는다.
    blocks = repo.stage_block_headers(sid) if hasattr(repo, "stage_block_headers") else repo.list_blocks(sid)
    return project_live_turns(repo.list_utterances(sid, limit=-1), repo.stage_raw_segments(sid),
                              repo.all_caption_sources(sid), blocks,
                              repo.get_roster(sid) if hasattr(repo, "get_roster") else [], checkpoint=checkpoint)


def session_source_guesses(repo, sid: str, *, checkpoint: dict | None = None) -> dict[int, dict]:
    """조립 발언이 뒤따르기 전에도 최신 원조각의 표시 후보를 읽는다."""
    if not hasattr(repo, "all_caption_sources"):
        return {}
    return source_speaker_guesses(repo.all_caption_sources(sid),
                                  repo.get_roster(sid) if hasattr(repo, "get_roster") else [], checkpoint=checkpoint)


def source_speaker_guesses(sources: list[dict], roster: list[dict] | tuple = (), *, checkpoint: dict | None = None) -> dict[int, dict]:
    """실제 원자막 ID별 후보만 반환한다. 새 발언 ID·조립 행·색 학습은 만들지 않는다."""
    ordered = sorted((source for source in sources if type(source.get("id")) is int and source["id"] > 0
        and type(source.get("revision")) is int
        and source.get("replayed") is not True and isinstance(source.get("text"), str)),
        key=lambda source: (source.get("first_rx", 0), source["id"]))
    inputs, projections, previous_label = [], [], None
    for source in ordered:
        parts = source.get("parts") or []
        labels = [part.get("label") for part in parts if isinstance(part, dict) and part.get("label") is not None]
        first_label, last_label = (labels[0], labels[-1]) if labels else (None, None)
        text = re.sub(r"(?m)^\s*-\s*", "", source["text"])
        row = {"id": source["id"], "seq": len(inputs), "text": text,
               "ts_start": source.get("first_rx", 0), "ts_end": source.get("last_rx") or source.get("first_rx", 0),
               "speaker": "", "speaker_role": "unknown", "speaker_source": "inferred",
               "speaker_hint": f"hwa:{first_label}" if first_label is not None else "",
               "new_speaker": bool(labels and previous_label != first_label or re.match(r"^\s*-", source["text"])),
               "is_final": bool(source.get("final") and source.get("scd")),
               "analyzed": bool(source.get("final") and source.get("scd")),
               "source_order": f"{len(inputs):012d}", "turn_id": f"원표식:{first_label}",
               "_display_mixed_source": len(set(labels)) > 1, "_source_display": True}
        inputs.append(row)
        projections.append({**row, "source_revisions": {str(source["id"]): source["revision"]}})
        previous_label = last_label
    _display_guesses(projections, inputs, list(roster), {}, checkpoint=checkpoint)
    return {row["id"]: row["speaker_guess"] for row in projections if row.get("speaker_guess")}


def _guess(turn, speaker, role, basis, *, estimated=True, dependencies=None):
    return {"speaker": speaker, "role": role, "estimated": estimated, "basis": basis, "source": "display_only",
            "override_existing": bool(estimated and (turn.get("speaker") or turn.get("speaker_role") in {"member", "answer", "chair"})),
            "source_revisions": {**(dependencies or {}), **turn["source_revisions"]}}


def _adjacent(previous, row):
    start, end = row.get("ts_start"), previous.get("ts_end") or previous.get("ts_start")
    return bool(type(start) is int and type(end) is int and type(previous.get("ts_start")) is int
        and previous["ts_start"] <= start <= end + 15_000
        and call_fragment_order(previous) < call_fragment_order(row))


def _display_guesses(turns, rows, roster, blocks, *, checkpoint=None):
    """블록 소유권·확정 역할·색 학습에는 쓰지 않는 별도 표시 후보만 만든다."""
    by_id = {turn["id"]: turn for turn in turns}
    members = {p["name"] for p in roster if p.get("role") == "member" and p.get("name")}
    chairs = [p["name"] for p in roster if p.get("role") == "chair" and p.get("name")]
    chair = chairs[0] if len(chairs) == 1 else "위원장"
    # 이름·역할만 의미분석에 쓰며 명단 판본이 바뀌면 다른 캐시 키가 된다.
    roster_key = tuple((person["name"], person.get("role", "member")) for person in roster
                       if isinstance(person, dict) and isinstance(person.get("name"), str) and person["name"])
    ordered = sorted((row for row in rows if type(row.get("id")) is int),
                     key=lambda row: (row.get("seq", row["id"]), row["id"]))
    owner_context = None
    start_index=0
    states={}
    if checkpoint is not None:
        # 행마다 직전 진행 상태를 보관하여 호명·색 문맥을 잃지 않고 변경된 접미부만 읽는다.
        scope=hashlib.sha256(json.dumps(roster_key,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
        prior=checkpoint.get("previous") or {}
        inputs={row["id"]:{"row":{k:v for k,v in row.items() if k not in {"created_at","updated_at"}},
            "turn":{k:v for k,v in (by_id.get(row["id"]) or {}).items() if k not in {"speaker_guess","updated_at"}},
            "block":{k:blocks.get(row.get("block_id"),{}).get(k) for k in ("kind","questioner")}}
            for row in ordered}
        retained=[]
        previous_rows=checkpoint.get("previous_rows")
        previous_turns={r["id"]:r for r in checkpoint.get("previous_turns") or []}
        if isinstance(previous_rows,list):
            old_rows={r["id"]:r for r in previous_rows}
            old_blocks={b["id"]:b for b in checkpoint.get("previous_blocks") or []}
            count=0
            for row in ordered:
                old_row=old_rows.get(row["id"])
                old_input={"row":{k:v for k,v in (old_row or {}).items() if k not in {"created_at","updated_at"}},
                    "turn":{k:v for k,v in previous_turns.get(row["id"],{}).items() if k not in {"speaker_guess","updated_at"}},
                    "block":{k:old_blocks.get((old_row or {}).get("block_id"),{}).get(k) for k in ("kind","questioner")}}
                if inputs[row["id"]]!=old_input:break
                count+=1
            checkpoint["prefix"]=count
            checkpoint["prefix_guesses"]={uid:r.get("speaker_guess") for uid,r in previous_turns.items()}
        prefix=checkpoint.get("prefix",0)
        if type(prefix) is int and 0<prefix<=len(ordered) and callable(checkpoint.get("state_for")):
            state=checkpoint["state_for"](ordered[prefix-1]["id"])
            if isinstance(state,dict) and state.get("scope")==scope and state.get("input")==inputs[ordered[prefix-1]["id"]]:
                start_index=prefix;owner_context=state.get("owner_context")
                for row in ordered[:prefix]:
                    uid=row["id"];retained.append(uid)
                    guess=(checkpoint.get("prefix_guesses") or {}).get(uid)
                    if uid in by_id:
                        if guess is None:by_id[uid].pop("speaker_guess",None)
                        else:by_id[uid]["speaker_guess"]=guess
        for row in ordered[start_index:] if not retained else []:
            uid=row["id"];old=prior.get(uid) or prior.get(str(uid))
            if not isinstance(old,dict) or old.get("scope")!=scope or old.get("input")!=inputs[uid]:
                break
            states[uid]=old
            if uid in by_id:
                if old.get("guess") is not None:by_id[uid]["speaker_guess"]=old["guess"]
                else:by_id[uid].pop("speaker_guess",None)
            owner_context=old.get("owner_context")
            start_index+=1
        checkpoint["reused"]=start_index
        checkpoint["retained_ids"]=retained
        def remember(index):
            uid=ordered[index]["id"]
            states[uid]={"scope":scope,"input":inputs[uid],"owner_context":owner_context,
                "guess":(by_id.get(uid) or {}).get("speaker_guess")}
    for index in range(start_index,len(ordered)):
        if checkpoint is not None and index>start_index:
            remember(index-1)
        row=ordered[index]
        turn = by_id.get(row["id"])
        if not turn:
            owner_context = None
            continue
        role, speaker = row.get("speaker_role"), row.get("speaker") or ""
        stored = (_guess(turn, speaker, role, "기존 실시간 규칙이 추정한 화자")
                  if speaker and role in {"member", "answer", "chair"} else None)
        if speaker and row.get("speaker_source") == "manual":
            turn["speaker_guess"] = _guess(turn, speaker, role if role in {"member", "answer", "chair"} else "other",
                                           "사람이 지정한 화자", estimated=False)
            owner_context = None
            continue
        if turn.get("protected") or row.get("review_parts"):
            owner_context = None
            continue
        text = row.get("text") or ""
        # 명단 밖 이름이나 명시 수집원 화자를 문맥상 질의자 이름으로 덮지 않는다.
        if any(value and not str(value).startswith(("hwa:", "추정:"))
               for value in (row.get("speaker_hint"), row.get("source_speaker_hint"))):
            if stored:
                turn["speaker_guess"] = {**stored, "estimated": False, "override_existing": False,
                                         "basis": "수집원이 명시한 화자"}
            owner_context = None
            continue
        identity = re.search(r"(?:^|저는\s+|제가\s+)([가-힣]{2,5})\s*(?:위원|의원)입니다", text[:100])
        if identity and identity[1] not in members:
            owner_context = None
            continue
        evidence, anchors, _, reported = _text_analysis(text, roster_key)
        calls = [anchor for anchor in anchors if anchor["kind"] == "call"]
        mixed_call = any(text[a["end"]:].strip(" ,.!?。") or not re.fullmatch(
            r"(?:(?:그러면|그럼|다음은|다음으로|이어서|감사합니다|수고하셨습니다)[,.!?。\s]*)*",
            text[:a["start"]].strip()) for a in calls)
        if evidence["mixed"] or mixed_call or row.get("_display_mixed_source"):
            turn["speaker_guess"] = _guess(turn, "복수 화자", "mixed", "한 말묶음 안의 질문·답변·진행 경계가 섞임")
            owner_context = None
            continue
        if calls or any(a["kind"] in {"chair_procedure", "chair_return"} for a in anchors):
            turn["speaker_guess"] = stored if stored and role == "chair" else _guess(turn, chair, "chair", "원문의 직접 호명·회의 진행 표현")
            owner_context = None
            continue
        introductions = [a for a in anchors if a["kind"] == "self_intro" and a["speaker"] in members]
        if introductions:
            turn["speaker_guess"] = _guess(turn, introductions[-1]["speaker"], "member", "명단과 일치하는 직접 자기소개")
            owner_context = {"name": introductions[-1]["speaker"], "refs": turn["source_revisions"]}
            continue
        if (re.search(r"(?:위원장님|위원님)[,.!?。\s]*(?:잠깐만요|잠시만요|의사진행\s*발언)", text)
                or re.match(r"^(?:잠깐만요|잠시만요|의사진행\s*발언)", text)):
            turn["speaker_guess"] = _guess(turn, "끼어든 참석자", "other", "직접 끼어듦·의사진행 발언 표현")
            owner_context = None
            continue
        if re.match(r"^(?:회의(?:장|진행)|장내|기술(?:적|상)|중계|방송|마이크|연결(?:을|이))", text):
            turn["speaker_guess"] = _guess(turn, "진행 관련 참석자", "other", "중계·장비·회의 진행 안내 표현")
            owner_context = None
            continue
        block = blocks.get(row.get("block_id")) or {}
        owner = owner_context["name"] if owner_context else block.get("questioner") if block.get("kind") == "question" else None
        bare_answer = bool(re.fullmatch(r"(?:예|네|아니요|아닙니다|맞습니다|그렇습니다|알겠습니다)[,.!?。\s]*", text.strip()))
        # 표시 후보에 한해 업무 주체가 직접 말하는 짧은 검토·추진 답변도 구분한다.
        own_work_answer = bool(evidence["role"] not in {"member", "chair"}
            and not reported and not re.match(r"^\s*[\"'“‘「『]", text)
            and re.fullmatch(r"(?:(?:예|네|우선|현재|지금)\s*){0,2}저희(?:가|는|도|들이)\s+"
                             r"(?:검토|확인|조사|추진|준비|협의)(?:를\s*)?(?:하고|중에|중)\s*"
                             r"(?:있습니다|입니다)[,.!?。\s]*", text.strip()))
        if evidence["role"] in {"member", "answer"} or bare_answer or own_work_answer:
            guessed_role = evidence["role"] if evidence["role"] in {"member", "answer"} else "answer"
            name = (owner if owner in members else "질의 의원") if guessed_role == "member" else "피감기관 답변"
            turn["speaker_guess"] = stored if stored and role == guessed_role else _guess(turn, name, guessed_role,
                "원문의 직접 질문·요구 표현" if guessed_role == "member" else "원문의 직접 업무 답변 표현",
                dependencies=owner_context["refs"] if owner_context else None)
            continue
        if reported or re.match(r"^\s*[\"'“‘「『]", text):
            owner_context = None
            continue
        previous = ordered[index - 1] if index else None
        prior = by_id.get(previous["id"]) if previous else None
        if not previous or not prior or not _adjacent(previous, row):
            owner_context = None
            turn["speaker_guess"] = stored or _guess(turn, "질의·답변 참석자", "other", "직접 역할 단서가 부족한 임시 표시")
            continue
        if (previous.get("is_final") and previous.get("analyzed")
                and (row.get("new_speaker") or row.get("_source_display")) and len(_compact(text)) >= 12):
            called = _text_analysis(previous.get("text") or "", roster_key)[2]
            if len(called) == 1:
                turn["speaker_guess"] = _guess(turn, called[0], "member", "완전한 위원장 명단 호명 직후 별도 서론",
                    dependencies=prior["source_revisions"])
                owner_context = {"name": called[0], "refs": prior["source_revisions"]}
                continue
        guess = prior.get("speaker_guess") or {}
        same_turn = (row.get("turn_id") and row.get("turn_id") == previous.get("turn_id") and not row.get("new_speaker")
                     and row.get("speaker_hint") == previous.get("speaker_hint"))
        if ((same_turn or owner_context)
                and guess.get("role") in {"member", "answer", "chair"}):
            turn["speaker_guess"] = stored if stored and role == guess["role"] and speaker == guess["speaker"] else _guess(
                turn, guess["speaker"], guess["role"], "유효 호명 이후 중립 서론의 인접한 이어말하기" if owner_context else "같은 원문 말묶음의 인접한 이어말하기",
                dependencies={**(owner_context["refs"] if owner_context else {}), **prior["source_revisions"]})
        elif stored:
            turn["speaker_guess"] = stored
        else:
            turn["speaker_guess"] = _guess(turn, "질의·답변 참석자", "other", "직접 역할 단서가 부족한 임시 표시")
    if checkpoint is not None:
        if len(ordered)>start_index:remember(len(ordered)-1)
        checkpoint["current"]=states
    return turns


def project_live_turns(utterances: list[dict], segments: list[dict], sources: list[dict],
                       blocks: list[dict] | tuple = (), roster: list[dict] | tuple = (), *, checkpoint: dict | None = None) -> list[dict]:
    """조립 출처와 본문이 일치할 때만 기존 화자·말묶음을 원자막 위에 표시한다."""
    sources = [row for row in sources if type(row.get("id")) is int and row["id"] > 0
               and isinstance(row.get("text"), str) and row.get("replayed") is not True]
    rows = [row for row in utterances if type(row.get("id")) is int and row["id"] > 0
            and row.get("speaker_role") != "system" and str(row.get("source_key", "")).rsplit("#", 1)[-1].isdigit()]
    try:
        mapping = map_utterance_sources(rows, segments, sources)
    except (TypeError, ValueError, KeyError, IndexError):
        return []  # 불완전한 과거 기록도 시각으로 소속을 추정하지 않는다.
    source_by_id = {row["id"]: row for row in sources}
    raw_by_key = {row["source_key"]: row for row in segments}
    block_by_id = {row["id"]: row for row in blocks}
    output = []
    for row in rows:
        spans = mapping["spans"].get(row["id"])
        if not spans or row["id"] in mapping["failures"]:
            continue
        ids = list(dict.fromkeys(span["source_id"] for span in spans))
        raw = raw_by_key.get(row["source_key"].rsplit("#", 1)[0]) or {}
        block = block_by_id.get(row.get("block_id")) or {}
        summary = block.get("summary") or {}
        protected = bool(row.get("speaker_source") == "manual" or row.get("review_parts")
            or row.get("text") != row.get("original_text") or block.get("review_status") == "reviewed"
            or isinstance(summary, dict) and summary.get("edited_by"))
        if not protected and not row.get("is_final") and (
                type(raw.get("updated_at")) is not int or type(row.get("updated_at")) is not int
                or row["updated_at"] < raw["updated_at"]
                or any(type(source_by_id[uid].get("stable_at")) is not int for uid in ids)):
            continue
        # 새 원자막을 저장한 뒤 이전 조립 발언이 아직 갱신되지 않은 짧은 창은 원문만 보인다.
        if not protected and (any(source_by_id[uid].get("stable_at", 0) > raw.get("updated_at", 0)
                                  for uid in ids if raw.get("updated_at") is not None)
                              or (raw.get("raw") or {}).get("speaker_revision")):
            continue
        original = row.get("original_text") or row.get("text") or ""
        located = "".join(source_by_id[s["source_id"]]["text"][s["start"]:s["end"]] for s in spans)
        if _compact(original) != _compact(located):
            continue
        projected = public_live_turn({**row, "protected": protected, "source_ids": ids,
            "source_spans": spans, "source_revisions": {str(uid): source_by_id[uid]["revision"] for uid in ids},
            "is_final": bool(row.get("is_final")), "analyzed": bool(row.get("analyzed")),
            "raw_revision": raw.get("revision", 0)})
        output.append(projected)
    _display_guesses(output, utterances, list(roster), block_by_id, checkpoint=checkpoint)
    return sorted(output, key=lambda row: (row.get("ts_start", 0), row.get("seq", 0), row["id"]))
