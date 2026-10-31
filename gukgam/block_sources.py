"""질의 블록의 원자막 문자 범위를 확인한다. 저장·모델 호출·본문 추정은 하지 않는다."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import re
from typing import Any

from .nlp.floor import (closed_short_calls, explicit_chair_declarations as _floor_declarations,
                        is_reported_anchor, reported_examination)

_PIECE = re.compile(r"^s(?P<segment>\d+)(?:\.(?P<occurrence>\d+))?~(?P<part>\d+)$")
_CALL = re.compile(r"(?P<name>[가-힣]{2,5})\s*(?:위원|의원)(?:님)?(?P<formal>께서)?\s*"
    r"(?:다음\s*)?(?:(?:보충|추가)\s*)?(?:질의|질문|발언|신문)(?:을|를)?\s*"
    r"(?:해\s*주시기\s*바랍니다|해\s*주십시오|해\s*주세요|해\s*주시겠습니다|하십시오|하시기\s*바랍니다|하시겠습니다|하시죠|드리겠습니다)[.!?。]*")
_CLOSING_LEAD = re.compile(r"(?:감사합니다|수고(?:하셨|했)습니다)[,.!?。]*\s*"
    r"(?:추가로\s*질의하실\s*위원(?:님)?(?:들)?\s*계십니까[,.!?。]*\s*)?$")
_CHAIR_QUESTION_TRANSITION = re.compile(r"수고(?:하셨|했)습니다[,.!?。\s]*제가\s*"
    r"(?:주질의|보충질의)(?:를)?\s*마치기\s*전에[^.?!。\n]{0,45}"
    r"(?:장관|기관장)(?:님)?께\s*(?:여쭤보려고|여쭈어보려고|질문(?:을)?\s*드리려고)")
_RESUME = re.compile(r"(?:의석|좌석)(?:을)?\s*정돈(?:해|하여)\s*(?:주십시오|주시기\s*바랍니다)"
    r"[,.!?。\s]*국정감사(?:를)?\s*(?:계속|속개)(?:하도록\s*)?\s*하겠습니다|"
    r"지금부터\s*증인과\s*참고인에\s*대한\s*신문(?:을|를)?\s*실시하도록\s*하겠습니다")
_TRANSITION_PREFIX = re.compile(r"(?:수고하셨습니다|다음은|다음으로|다음에는|"
    r"보충질의(?:를)?\s*시작하겠습니다|그러면\s*순서에\s*의해서)[^.?!。]{0,90}$|다음\s*$")


def _marker_start(text: str, offset: int) -> int:
    start = text.rfind("\n", 0, offset) + 1
    return start if re.fullmatch(r"\s*-\s*", text[start:offset]) else offset


def _chars(text: str) -> list[tuple[str, int]]:
    """공백과 줄 첫 화자 전환 표지만 제외하며 나머지 원문 문자를 그대로 대조한다."""
    markers = {m.start() for m in re.finditer(r"(?m)^\s*-\s+", text)}
    skip = set()
    for start in markers:
        match = re.match(r"\s*-\s+", text[start:])
        skip.update(range(start, start + match.end()))
    return [(char, i) for i, char in enumerate(text) if not char.isspace() and i not in skip]


def _compact(text: str) -> str:
    return "".join(char for char, _ in _chars(text))


def explicit_chair_declarations(text: str) -> list[dict]:
    """직접 종료 선언의 원문 범위만 반환한다. 실제 진행자 소속은 호출자가 검증한다."""
    found = []
    for anchor in _floor_declarations(text):
        lead = _CLOSING_LEAD.search(text[:anchor["start"]])
        literal = text[anchor["start"]:anchor["end"]]
        declaration_type = ("question_complete" if anchor["kind"] == "explicit_chair_declaration" else
                            "audit_end" if "종료" in literal else "pause")
        found.append({**anchor, "name": "", "declaration_type": declaration_type,
                      "_literal_start": anchor["start"],
                      "start": lead.start() if lead else anchor["start"]})
    for match in _CHAIR_QUESTION_TRANSITION.finditer(text):
        if not is_reported_anchor(text, match.start(), match.end()):
            found.append({"kind": "explicit_chair_declaration", "name": "",
                          "declaration_type": "chair_question_transition",
                          "_literal_start": match.start(), "start": match.start(), "end": match.end()})
    return sorted(found, key=lambda anchor: (anchor["start"], anchor["end"]))


def _merge(spans: list[dict]) -> list[dict]:
    out: list[dict] = []
    for span in spans:
        if out and out[-1]["source_id"] == span["source_id"] and span["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], span["end"])
        else:
            out.append(dict(span))
    return out


def selected_short_call_boundaries(sources: list[dict], source_selection: list[dict], *,
                                   roster: list[dict] | tuple = (), questioner: str = "",
                                   include_complete: bool = True) -> list[dict]:
    """선택 원문 안의 종료 인사·다른 의원 완전 호명을 읽기 전용으로 찾는다.

    저장 문서의 source_selection을 그대로 받는다. 누락·잘못된 좌표는 ValueError로
    알리며, 선택하지 않은 문자나 원조각 사이를 건너뛰어 호명을 만들지 않는다.
    """
    by_id = {source["id"]: source for source in sources}
    selected = {}
    if not isinstance(source_selection, list):
        raise ValueError("원문 선택 목록이 필요합니다")
    for row in source_selection:
        if (not isinstance(row, dict) or type(row.get("id")) is not int
                or row["id"] not in by_id or row["id"] in selected
                or not isinstance(row.get("selected_spans"), list)):
            raise ValueError("원문 선택 출처가 유효하지 않습니다")
        spans = row["selected_spans"]
        cursor = 0
        for span in spans:
            if (not isinstance(span, dict) or type(span.get("start")) is not int
                    or type(span.get("end")) is not int
                    or not cursor <= span["start"] < span["end"] <= len(by_id[row["id"]]["text"])):
                raise ValueError("원문 선택 문자 좌표가 유효하지 않습니다")
            cursor = span["end"]
        selected[row["id"]] = spans
    text, positions = [], []
    def separator(gap=False):
        value = "\n␞\n" if gap else "\n"
        text.extend(value)
        positions.extend([None] * len(value))
    for source in sorted(sources, key=lambda s: (s.get("first_rx", 0), s["id"])):
        spans = selected.get(source["id"])
        if not spans:
            separator(True)
            continue
        cursor = 0
        for span in spans:
            if cursor < span["start"]:
                separator(True)
            value = source["text"][span["start"]:span["end"]]
            text.extend(value)
            positions.extend((source["id"], i) for i in range(span["start"], span["end"]))
            cursor = span["end"]
        separator(cursor < len(source["text"]))
    joined = "".join(text)
    out = []
    for anchor in closed_short_calls(joined, list(roster)):
        if anchor["speaker"] == questioner:
            continue
        refs = [ref for ref in positions[anchor["start"]:anchor["end"]] if ref is not None]
        if not refs:
            continue
        out.append({"kind": "call", "origin": "closed_short_call", "name": anchor["speaker"],
                    "text": joined[anchor["start"]:anchor["end"]].strip(),
                    "source_start": {"source_id": refs[0][0], "offset": refs[0][1]},
                    "source_end": {"source_id": refs[-1][0], "offset": refs[-1][1] + 1},
                    "source_ids": list(dict.fromkeys(ref[0] for ref in refs))})
    if include_complete:
        normalized = re.sub(r"(?m)^[ \t]*-[ \t]+", lambda match: " " * len(match.group()), joined)
        for anchor in _complete_roster_calls(normalized, roster=roster, questioner=questioner):
            refs = [ref for ref in positions[anchor["start"]:anchor["end"]] if ref is not None]
            if not refs:
                continue
            out.append({"kind": "call", "origin": "complete_roster_call", "name": anchor["name"],
                        "text": joined[anchor["start"]:anchor["end"]].strip(),
                        "source_start": {"source_id": refs[0][0], "offset": refs[0][1]},
                        "source_end": {"source_id": refs[-1][0], "offset": refs[-1][1] + 1},
                        "source_ids": list(dict.fromkeys(ref[0] for ref in refs))})
    return out


def _complete_roster_calls(text: str, *, roster, questioner: str) -> list[dict]:
    """명단의 다른 의원에게 직접 넘어가는 진행 프레임과 완전 호명만 찾는다."""
    names = [person.get("name") for person in roster if person.get("role") == "member"]
    return [anchor for anchor in _anchor_matches(text, closing=False, roster=roster)
            if anchor["name"] != questioner and names.count(anchor["name"]) == 1
            and anchor["start"] < anchor["_literal_start"]
            and "␞" not in text[anchor["start"]:anchor["end"]]]


def _anchor_matches(text: str, *, closing: bool, roster) -> list[dict]:
    """진행 호명과 종료 인사의 문자 범위를 찾되 인용은 경계로 삼지 않는다."""
    found = []
    for match in _CALL.finditer(text):
        if (is_reported_anchor(text, match.start(), match.end())
                or match.re is _CALL and "신문" in match.group()
                and reported_examination(text, match.start(), match.end())):
            continue
        transition = _TRANSITION_PREFIX.search(text[:match.start()])
        found.append({"kind": "call",
                      "name": match.groupdict().get("name", ""),
                      **({"formal_declaration": True} if match.groupdict().get("formal") else {}),
                      "_literal_start": match.start(),
                      "start": transition.start() if transition else match.start(), "end": match.end()})
    if closing:
        found.extend(explicit_chair_declarations(text))
        for anchor in closed_short_calls(text, list(roster)):
            found.append({"kind": "call", "origin": "closed_short_call", "name": anchor["speaker"],
                          "start": anchor["start"], "end": anchor["end"]})
    return found


def _row_anchor(row: dict, mapping: dict, sources_by_id: dict, offsets: dict, *, closing=False, roster=()) -> list[dict]:
    """기존 발언 안 호명의 문자 위치를 최신 원자막 좌표로 되돌린다."""
    original = row.get("original_text") or row.get("text") or ""
    positions = []
    for span in mapping["spans"].get(row["id"], []):
        source = sources_by_id[span["source_id"]]
        positions.extend((span["source_id"], span["start"] + index)
                         for _, index in _chars(source["text"][span["start"]:span["end"]]))
    if len(positions) != len(_chars(original)):
        return []
    found = []
    for anchor in _anchor_matches(original, closing=closing, roster=roster):
        literal_at = anchor.pop("_literal_start", anchor["start"])
        start_at, end_at = anchor["start"], anchor["end"]
        start_index, end_index = len(_chars(original[:start_at])), len(_chars(original[:end_at]))
        if start_index >= end_index or end_index > len(positions):
            continue
        sid, start = positions[start_index]
        start = _marker_start(sources_by_id[sid]["text"], start)
        last_id, end = positions[end_index - 1]
        source_ids = list(dict.fromkeys(item[0] for item in positions[start_index:end_index]))
        if anchor["kind"] in {"explicit_pause_or_end", "explicit_chair_declaration"}:
            # 저장 행이 중간 원조각을 건너뛰어도 실제 수신 순서에 없는 선언을 만들지 않는다.
            left, right = offsets[sid] + start, offsets[last_id] + end + 1
            actual = [source for source in sources_by_id.values()
                      if offsets[source["id"]] < right and offsets[source["id"]] + len(source["text"]) > left]
            value = "\n".join(source["text"][max(0, left - offsets[source["id"]]):
                min(len(source["text"]), right - offsets[source["id"]])] for source in actual)
            if _compact(value) != _compact(original[start_at:end_at]):
                continue
            source_ids = [source["id"] for source in actual]
            literal_index = len(_chars(original[:literal_at]))
            anchor["_literal_source_spans"] = [{"source_id": uid, "start": offset, "end": offset + 1}
                for uid, offset in positions[literal_index:end_index]]
        found.append({**anchor, "utterance_id": row["id"], "text": original[start_at:end_at],
                      "start": offsets[sid] + start, "end": offsets[last_id] + end + 1,
                      "source_start": {"source_id": sid, "offset": start},
                      "source_end": {"source_id": last_id, "offset": end + 1},
                      "source_ids": source_ids})
    return found


def _source_boundary_anchors(rows: list[dict], mapping: dict, sources_by_id: dict, offsets: dict, *,
                             closing=False, roster=(), confirmed_next_name="", include_literal_spans=False) -> list[dict]:
    """진행 행과 실제로 겹치는 호명만 최신 원조각의 문자 좌표로 완결한다.

    화자 전환 표식 때문에 호명 끝이 다음 질의 첫 행으로 갈린 경우에도 같은 원문을
    읽는다. 바로 앞뒤 원조각 한 개는 호명 문장 연결에만 쓰며 시각으로 범위를 넓히지 않는다.
    """
    spans = [span for row in rows if row["id"] not in mapping["failures"]
             for span in mapping["spans"].get(row["id"], [])]
    selected_ids = {span["source_id"] for span in spans}
    if not selected_ids:
        return []
    all_ids = list(sources_by_id)
    indices = {index for index, sid in enumerate(all_ids) if sid in selected_ids}
    expanded = indices | {adjacent for index in indices for adjacent in (index - 1, index + 1)
                          if 0 <= adjacent < len(all_ids)}
    text, positions = [], []
    previous = None
    for index in sorted(expanded):
        if text:
            separator = "\n" if previous == index - 1 else "\n␞\n"
            text.extend(separator)
            positions.extend([None] * len(separator))
        source = sources_by_id[all_ids[index]]
        # 제공자가 붙인 줄 첫 '- '만 같은 길이의 공백으로 바꿔 좌표를 유지한다.
        value = re.sub(r"(?m)^[ \t]*-[ \t]+", lambda match: " " * len(match.group()), source["text"])
        text.extend(value)
        positions.extend((source["id"], offset) for offset in range(len(value)))
        previous = index
    joined = "".join(text)
    found = []
    anchors = _anchor_matches(joined, closing=closing, roster=roster)
    if confirmed_next_name:
        # 뒤 질의자의 저장 이름·명단 일치를 확인한 경우에만 중복 '다음은'을 허용한다.
        short = re.compile(r"수고(?:하셨|했)습니다[,.!?。]*\s*"
            r"(?:(?:다음(?:은|으로)?|이어서)\s*){1,2}(?P<name>"
            + re.escape(confirmed_next_name) + r")\s*(?:위원|의원)님(?=$|[\s,.!?。])")
        for match in short.finditer(joined):
            if not is_reported_anchor(joined, match.start(), match.end()):
                anchors.append({"kind": "call", "name": confirmed_next_name,
                                "origin": "confirmed_next_question_short_call",
                                "_literal_start": match.start("name"),
                                "start": match.start(), "end": match.end()})
    for anchor in anchors:
        literal_start = anchor.pop("_literal_start", anchor["start"])
        refs = [ref for ref in positions[anchor["start"]:anchor["end"]] if ref is not None]
        literal_refs = [ref for ref in positions[literal_start:anchor["end"]] if ref is not None]
        if not refs or not any(span["source_id"] == sid and span["start"] <= offset < span["end"]
                               for sid, offset in literal_refs for span in spans):
            continue
        first, last = refs[0], refs[-1]
        if anchor["kind"] in {"explicit_pause_or_end", "explicit_chair_declaration"} or include_literal_spans:
            first = (first[0], _marker_start(sources_by_id[first[0]]["text"], first[1]))
            anchor["_literal_source_spans"] = [{"source_id": uid, "start": offset, "end": offset + 1}
                for uid, offset in literal_refs]
        found.append({**anchor, "origin": anchor.get("origin", "source_boundary_completion"),
                      "text": joined[anchor["start"]:anchor["end"]],
                      "start": offsets[first[0]] + first[1], "end": offsets[last[0]] + last[1] + 1,
                      "source_start": {"source_id": first[0], "offset": first[1]},
                      "source_end": {"source_id": last[0], "offset": last[1] + 1},
                      "source_ids": list(dict.fromkeys(ref[0] for ref in refs)),
                      "utterance_ids": [row["id"] for row in rows]})
    return found


def _chair_internal_calls(rows: list[dict], mapping: dict, sources_by_id: dict, offsets: dict,
                           roster, questioner: str) -> list[dict]:
    """저장 질의 안의 실제 진행자 완전 호명을 별도 의원의 시작 경계로 읽는다."""
    chairs = [row for row in rows if row.get("speaker_role") == "chair"]
    out = []
    for anchor in _source_boundary_anchors(chairs, mapping, sources_by_id, offsets, roster=roster,
                                           include_literal_spans=True):
        literal = anchor.pop("_literal_source_spans", [])
        if not _complete_roster_calls(anchor["text"], roster=roster, questioner=questioner):
            continue
        owners = [row for row in chairs if _intersections(mapping["spans"].get(row["id"], []), literal)]
        if not owners:
            continue
        # 진행자 원행 앞의 답변·질의 종료 인사를 함께 지우지 않는다.
        first = min((span for row in owners for span in mapping["spans"].get(row["id"], [])),
                    key=lambda span: offsets[span["source_id"]] + span["start"])
        start = _marker_start(sources_by_id[first["source_id"]]["text"], first["start"])
        left = max(anchor["start"], offsets[first["source_id"]] + start)
        selected = [source for source in sources_by_id.values() if offsets[source["id"]] < anchor["end"]
                    and offsets[source["id"]] + len(source["text"]) > left]
        if not selected:
            continue
        first_id = selected[0]["id"]
        anchor.update(start=left, source_start={"source_id": first_id, "offset": left - offsets[first_id]},
                      source_ids=[source["id"] for source in selected],
                      origin="interior_complete_roster_call", chair_utterance_ids=[row["id"] for row in owners])
        anchor["text"] = "\n".join(source["text"][max(0, left - offsets[source["id"]]):
            min(len(source["text"]), anchor["end"] - offsets[source["id"]])] for source in selected)
        out.append(anchor)
    return out


def _chair_declaration_anchors(rows: list[dict], mapping: dict, sources_by_id: dict,
                               offsets: dict, roster) -> list[dict]:
    """선언 본문과 실제로 겹치는 진행자 원행만 종료 권위로 반환한다."""
    chairs = [row for row in rows if row.get("speaker_role") == "chair"]
    candidates = [anchor for row in chairs
                  for anchor in _row_anchor(row, mapping, sources_by_id, offsets, closing=True, roster=roster)]
    candidates.extend(_source_boundary_anchors(chairs, mapping, sources_by_id, offsets, closing=True, roster=roster))
    out = []
    for anchor in candidates:
        literal = anchor.pop("_literal_source_spans", [])
        if anchor["kind"] not in {"explicit_pause_or_end", "explicit_chair_declaration"}:
            continue
        owners = [row["id"] for row in chairs if _intersections(mapping["spans"].get(row["id"], []), literal)]
        if owners:
            out.append({**anchor, "chair_utterance_ids": owners})
    return out


def _confirmed_next_question_call(following: dict | None, blocks: list[dict], next_rows: list[dict],
                                  mapping: dict, sources_by_id: dict, offsets: dict, roster) -> list[dict]:
    """종료 인사·진행 행의 짧은 호명·다음 질의자 세 근거가 일치할 때만 연결한다."""
    if not following or following.get("kind") != "chair" or not next_rows:
        return []
    later = min((row for row in blocks if row["id"] > following["id"]),
                key=lambda row: row["id"], default=None)
    name = str((later or {}).get("questioner") or "").strip()
    if (not later or later.get("kind") != "question" or not name
            or sum(person.get("name") == name and person.get("role") == "member" for person in roster) != 1):
        return []
    return [anchor for anchor in _source_boundary_anchors(next_rows, mapping, sources_by_id, offsets,
                closing=True, roster=roster, confirmed_next_name=name)
            if anchor.get("origin") == "confirmed_next_question_short_call"]


def _group_anchors(rows: list[dict], mapping: dict, sources_by_id: dict, offsets: dict, *, closing=False, roster=()) -> list[dict]:
    """호명 이름과 진행 동사가 다른 저장 행으로 갈라진 경우에도 원문 좌표로 연결한다."""
    ordered = sorted(rows, key=lambda row: row.get("seq", row["id"]))
    if not ordered:
        return []
    if any(row["id"] in mapping["failures"] or row["id"] not in mapping["spans"] for row in ordered):
        return [anchor for row in ordered for anchor in _row_anchor(row, mapping, sources_by_id, offsets, closing=closing, roster=roster)]
    key = "joined-boundary"
    combined = {"id": key, "original_text": "\n".join(row.get("original_text") or row.get("text") or "" for row in ordered)}
    joined_mapping = {"spans": {key: [span for row in ordered for span in mapping["spans"][row["id"]]]}}
    anchors = _row_anchor(combined, joined_mapping, sources_by_id, offsets, closing=closing, roster=roster)
    for anchor in anchors:
        anchor.pop("utterance_id")
        anchor["utterance_ids"] = [row["id"] for row in ordered]
    anchors.extend(_source_boundary_anchors(ordered, mapping, sources_by_id, offsets, closing=closing, roster=roster))
    if closing and all(row.get("speaker_role") == "chair" for row in ordered):
        # '○○ 위원님께서 질의하시겠습니다' 앞의 진행자 소개도 앞 질의에 섞지 않는다.
        starts = [span for row in ordered for span in mapping["spans"].get(row["id"], [])]
        if starts:
            first = min(starts, key=lambda span: offsets[span["source_id"]] + span["start"])
            for anchor in anchors:
                if anchor.get("formal_declaration"):
                    start = _marker_start(sources_by_id[first["source_id"]]["text"], first["start"])
                    if offsets[first["source_id"]] + start >= anchor["start"]:
                        continue
                    anchor["start"] = offsets[first["source_id"]] + start
                    anchor["source_start"] = {"source_id": first["source_id"], "offset": start}
                    source_ids = [sid for sid, source in sources_by_id.items() if offsets[sid] < anchor["end"]
                                  and offsets[sid] + len(source["text"]) > anchor["start"]]
                    anchor["source_ids"] = source_ids
                    anchor["text"] = "\n".join(sources_by_id[sid]["text"][max(0, anchor["start"] - offsets[sid]):
                        min(len(sources_by_id[sid]["text"]), anchor["end"] - offsets[sid])] for sid in source_ids)
    return anchors


def map_utterance_sources(utterances: list[dict], segments: list[dict], sources: list[dict]) -> dict:
    """저장 턴의 원조각 목록과 원발언을 최신 자막에 대조한다. 시간만으로 포함하지 않는다."""
    segment_map = {row["source_key"]: row for row in segments}
    by_number: dict[str, list[dict]] = defaultdict(list)
    for source in sources:
        by_number[str(source.get("segment"))].append(source)
    siblings: dict[str, list[dict]] = defaultdict(list)
    for row in utterances:
        if row.get("speaker_role") != "system":
            siblings[str(row.get("source_key", "")).rsplit("#", 1)[0]].append(row)
    mapped, failures = {}, {}
    for key, rows in siblings.items():
        row = segment_map.get(key)
        pieces = (row or {}).get("raw", {}).get("segments", {})
        if not isinstance(pieces, dict) or not pieces:
            for utterance in rows:
                failures[utterance["id"]] = "source_membership_missing"
            continue
        stream, positions, errors = [], [], []
        for piece_key, fragment in pieces.items():
            match = _PIECE.fullmatch(piece_key)
            if not match or not isinstance(fragment, str):
                errors.append("source_membership_invalid")
                continue
            needle = _compact(fragment)
            candidates = []
            for source in by_number.get(match["segment"], []):
                chars = _chars(source["text"])
                haystack = "".join(char for char, _ in chars)
                starts = [m.start() for m in re.finditer(re.escape(needle), haystack)] if needle else []
                # 같은 본문이 한 원조각 안에서 반복될 때 제공자 부분 번호를 함께 확인한다.
                part_no = int(match["part"])
                parts = source.get("parts", [])
                if len(starts) > 1 and part_no < len(parts):
                    prefix = "\n".join(part["text"] for part in parts[:part_no])
                    prefix_len = len(_compact(prefix))
                    part_text = _compact(parts[part_no]["text"])
                    starts = [start for start in starts if prefix_len <= start and start + len(needle) <= prefix_len + len(part_text)]
                if len(starts) == 1:
                    start = starts[0]
                    candidates.append((source, chars[start:start + len(needle)]))
            if len(candidates) != 1:
                errors.append("source_revision_unmatched" if not candidates else "source_membership_ambiguous")
                continue
            source, chars = candidates[0]
            stream.extend(char for char, _ in chars)
            positions.extend((source["id"], offset) for _, offset in chars)
        if errors:
            for utterance in rows:
                failures[utterance["id"]] = sorted(set(errors))[0]
            continue
        text, cursor = "".join(stream), 0
        for utterance in sorted(rows, key=lambda u: int(u["source_key"].rsplit("#", 1)[-1])):
            needle = _compact(utterance.get("original_text") or utterance.get("text") or "")
            if not needle:
                mapped[utterance["id"]] = []
                continue
            start = text.find(needle, cursor)
            if start < 0 or text.find(needle, start + 1) >= 0:
                failures[utterance["id"]] = "utterance_source_unmatched" if start < 0 else "utterance_source_ambiguous"
                continue
            # 형제 발언 사이에서 알 수 없는 본문을 건너뛰어 경계를 맞추지 않는다.
            if start != cursor:
                failures[utterance["id"]] = "utterance_source_gap"
                continue
            spans = []
            for source_id, offset in positions[start:start + len(needle)]:
                if spans and spans[-1]["source_id"] == source_id:
                    spans[-1]["end"] = offset + 1
                else:
                    spans.append({"source_id": source_id, "start": offset, "end": offset + 1})
            mapped[utterance["id"]] = spans
            cursor = start + len(needle)
        if cursor < len(text):
            for utterance in rows:
                failures.setdefault(utterance["id"], "utterance_source_tail_unassigned")
    return {"spans": mapped, "failures": failures}


def select_block_sources(block: dict, blocks: list[dict], utterances: list[dict],
                         segments: list[dict], sources: list[dict], *,
                         interruptions: list[dict] | tuple = (), mapping: dict | None = None,
                         roster: list[dict] | tuple = (), allow_unsettled: bool = False) -> dict[str, Any]:
    """질의 문자 소속을 검증한다. 명시한 대기 상한 정책만 원수신 확정 대기를 생략한다.

    allow_unsettled는 호출자가 후속 질의 두 건의 종료를 확인한 경우에만 전달한다.
    원문의 final/scd·본문·판본은 변경하지 않고 소속·종료·내부 발언 확정 검증은 유지한다.
    """
    mapping = mapping or map_utterance_sources(utterances, segments, sources)
    by_id = {source["id"]: source for source in sources}
    targets = sorted((row for row in utterances if row.get("block_id") == block["id"]
                      and row.get("speaker_role") != "system"), key=lambda row: row.get("seq", row["id"]))
    reasons, raw_spans, trace = [], [], []
    if block.get("kind") != "question":
        reasons.append("not_question")
    if block.get("is_open"):
        reasons.append("block_open")
    if not str(block.get("questioner") or "").strip():
        reasons.append("questioner_unknown")
    if not targets:
        reasons.append("no_utterances")
    for row in targets:
        if not row.get("is_final"):
            reasons.append("utterance_not_final")
        if row["id"] in mapping["failures"]:
            reasons.append(mapping["failures"][row["id"]])
        spans = mapping["spans"].get(row["id"], [])
        raw_spans.extend(spans)
        trace.append({"utterance_id": row["id"], "source_key": row["source_key"], "spans": deepcopy(spans),
                      "reason": mapping["failures"].get(row["id"])})
    # 블록 시각은 내부 말묶음 보간으로 서로 겹칠 수 있으므로 membership의 근거로 쓰지 않는다.
    later = [candidate for candidate in blocks if candidate["id"] > block["id"]]
    following = min(later, key=lambda b: b["id"]) if later else None
    next_rows = sorted((row for row in utterances if following and row.get("block_id") == following["id"]
                        and row.get("speaker_role") != "system"), key=lambda row: row.get("seq", row["id"]))
    boundary_kind = "next_chair" if following and following["kind"] == "chair" else (
        "next_named_question" if following and following["kind"] == "question" and following.get("questioner") else "unconfirmed")
    if boundary_kind == "unconfirmed" or not next_rows:
        reasons.append("completion_unconfirmed")
    for interruption in interruptions:
        start = interruption.get("start_ms", interruption.get("at_ms", 0))
        end = interruption.get("end_ms", start)
        if targets and start <= max(row.get("ts_end") or row["ts_start"] for row in targets) and end >= min(row["ts_start"] for row in targets):
            reasons.append("collection_interruption")
    ordered = sorted(raw_spans, key=lambda span: (by_id.get(span["source_id"], {}).get("first_rx", 0), str(span["source_id"]), span["start"]))
    for before, after in zip(ordered, ordered[1:]):
        if before["source_id"] == after["source_id"] and before["end"] > after["start"]:
            reasons.append("source_overlap")
    spans = _merge(ordered)
    # 기존 chair 행에 답변 꼬리·다음 질의 앞부분이 함께 들어가도 명시 호명 경계로 되찾는다.
    offsets, total = {}, 0
    for source in sources:
        offsets[source["id"]] = total
        total += len(source["text"])
    previous = max((b for b in blocks if b["id"] < block["id"]), key=lambda b: b["id"], default=None)
    previous_rows = [row for row in utterances if previous and row.get("block_id") == previous["id"]]
    starts = _group_anchors(previous_rows, mapping, by_id, offsets, roster=roster) if previous and previous.get("kind") == "chair" else []
    declaration_kinds = {"explicit_pause_or_end", "explicit_chair_declaration"}
    ends = _group_anchors(next_rows, mapping, by_id, offsets, closing=True, roster=roster) if following and following.get("kind") == "chair" else []
    ends = [anchor for anchor in ends if anchor["kind"] not in declaration_kinds]
    if following and following.get("kind") == "chair":
        ends.extend(_chair_declaration_anchors(next_rows, mapping, by_id, offsets, roster))
    ends.extend(_confirmed_next_question_call(following, blocks, next_rows, mapping, by_id, offsets, roster))
    start_anchor = max(starts, key=lambda anchor: anchor["start"], default=None)
    end_anchor = min(ends, key=lambda anchor: anchor["start"], default=None)
    if end_anchor and end_anchor["kind"] in declaration_kinds:
        boundary_kind = end_anchor["kind"]
    identity = _question_identity(block, start_anchor, roster)
    if identity["kind"] in {"derived_from_named_call", "uncertain_named_call"}:
        reasons = [reason for reason in reasons if reason not in ("not_question", "questioner_unknown")]
    if identity["status"] == "conflict":
        reasons.append("start_call_name_conflict")
    left = start_anchor["end"] if start_anchor else min((offsets[s["source_id"]] + s["start"] for s in spans), default=None)
    right = end_anchor["start"] if end_anchor else None
    if right is None and boundary_kind == "next_named_question":
        right = min((offsets[s["source_id"]] + _marker_start(by_id[s["source_id"]]["text"], s["start"])
                     for row in next_rows for s in mapping["spans"].get(row["id"], [])), default=None)
    # 잘못 길어진 DB 블록도 실제 감사 중지 선포 뒤의 속개·선서를 같은 질의에 넣지 않는다.
    declarations = _chair_declaration_anchors(targets, mapping, by_id, offsets, roster)
    pauses = [anchor for anchor in declarations
              if anchor["kind"] in declaration_kinds and left is not None
              and left < anchor["start"] and (right is None or anchor["start"] < right)]
    if pauses:
        end_anchor = min(pauses, key=lambda anchor: anchor["start"])
        right, boundary_kind = end_anchor["start"], end_anchor["kind"]
        reasons = [reason for reason in reasons if reason != "completion_unconfirmed"]
    # 기존 DB 블록 안에 다른 의원의 자료 요구가 섞여 있어도 종료 인사·명단 호명으로 자른다.
    if left is not None:
        upper = right if right is not None else max((offsets[span["source_id"]] + span["end"] for span in spans), default=left)
        candidates = [{"id": source["id"], "selected_spans": [{"start": max(0, left - offsets[source["id"]]),
            "end": min(len(source["text"]), upper - offsets[source["id"]])}]}
            for source in sources if offsets[source["id"]] < upper
            and offsets[source["id"]] + len(source["text"]) > left]
        short_ends = selected_short_call_boundaries(sources, candidates, roster=roster,
                                                   questioner=identity["questioner"], include_complete=False)
        if short_ends:
            short_end = min(short_ends, key=lambda a: offsets[a["source_start"]["source_id"]] + a["source_start"]["offset"])
            point = offsets[short_end["source_start"]["source_id"]] + short_end["source_start"]["offset"]
            if right is None or point < right:
                end_anchor, right = {**short_end, "start": point,
                    "end": offsets[short_end["source_end"]["source_id"]] + short_end["source_end"]["offset"]}, point
                reasons = [reason for reason in reasons if reason != "completion_unconfirmed"]
    completion_declaration_anchor, completion_scope = None, None
    internal_calls = [anchor for anchor in _chair_internal_calls(targets, mapping, by_id, offsets,
                      roster, identity["questioner"]) if left is not None and left < anchor["start"]
                      and (right is None or anchor["start"] < right)]
    if internal_calls:
        if (end_anchor and end_anchor["kind"] in declaration_kinds and following
                and following.get("kind") == "chair"):
            completion_declaration_anchor = deepcopy(end_anchor)
            completion_scope = {"kind": "interior_named_call_in_stored_question", "block_id": block["id"],
                                "following_chair_block_id": following["id"]}
        end_anchor = min(internal_calls, key=lambda anchor: anchor["start"])
        right, boundary_kind = end_anchor["start"], "interior_named_call"
        reasons = [reason for reason in reasons if reason != "completion_unconfirmed"]
    if boundary_kind == "next_chair" and end_anchor is None:
        reasons.append("closing_anchor_unconfirmed")
    if left is not None and right is not None and left < right:
        spans = [{"source_id": source["id"], "start": max(0, left - offsets[source["id"]]),
                  "end": min(len(source["text"]), right - offsets[source["id"]])}
                 for source in sources if offsets[source["id"]] < right and offsets[source["id"]] + len(source["text"]) > left]
    elif right is not None:
        reasons.append("boundary_order_invalid")
    chosen, unsettled_ids = [], []
    for source in sources:
        selected = [{"start": span["start"], "end": span["end"]} for span in spans if span["source_id"] == source["id"]]
        if not selected:
            continue
        if not source.get("final") or not source.get("scd"):
            unsettled_ids.append(source["id"])
            if not allow_unsettled:
                reasons.append("source_not_final_scd")
        # 원단위의 표식·공백도 가능한 범위에서는 그대로 보존한다.
        chars = _chars(source["text"])
        if not start_anchor and chars and selected[0]["start"] == chars[0][1]:
            selected[0]["start"] = 0
        if not end_anchor and chars and selected[-1]["end"] == chars[-1][1] + 1:
            selected[-1]["end"] = len(source["text"])
        chosen.append({**deepcopy(source), "selected_spans": selected,
                       "selected_text": "".join(source["text"][s["start"]:s["end"]] for s in selected)})
    selected_body = "\n".join(re.sub(r"(?m)^[ \t]*-[ \t]+", " ", source["selected_text"]) for source in chosen)
    remaining_calls = selected_short_call_boundaries(chosen,
        [{"id": source["id"], "selected_spans": source["selected_spans"]} for source in chosen],
        roster=roster, questioner=identity["questioner"])
    if any(anchor.get("origin") == "complete_roster_call" for anchor in remaining_calls):
        reasons.append("interior_named_call_requires_split")
    if any(not is_reported_anchor(selected_body, match.start(), match.end()) for match in _RESUME.finditer(selected_body)):
        reasons.append("interior_procedure_requires_split")
    # 범위 밖 chair 행에서 복원한 문자에도 같은 사람 수정 보호를 적용한다.
    selected_spans = [{"source_id": source["id"], **span}
                      for source in chosen for span in source["selected_spans"]]
    trace = []
    for row in sorted(utterances, key=lambda value: value.get("seq", value["id"])):
        if row.get("speaker_role") == "system":
            continue
        row_spans = mapping["spans"].get(row["id"], [])
        overlap = _intersections(row_spans, selected_spans)
        is_target = row.get("block_id") == block["id"]
        if not overlap and not is_target:
            continue
        trace.append({"utterance_id": row["id"], "source_key": row["source_key"],
                      "original_block_id": row.get("block_id"), "target_block_member": is_target,
                      "restored_from_boundary": not is_target, "spans": deepcopy(row_spans),
                      "selected_spans": overlap, "reason": mapping["failures"].get(row["id"])})
        if overlap and not row.get("is_final"):
            reasons.append("utterance_not_final")
    # 경계를 판단할 때 읽은 행 전체를 의존성으로 둔다. 호명 추가·삭제도 기존 초안을 무효화한다.
    dependency_ids = {span["source_id"] for row in previous_rows + next_rows
                      for span in mapping["spans"].get(row["id"], [])}
    dependency_ids.update(span["source_id"] for row in trace for span in row["spans"])
    dependency_blocks = {candidate["id"] for candidate in (previous, following) if candidate}
    dependency_blocks.update(row["original_block_id"] for row in trace if row["original_block_id"] is not None)
    dependency_blocks.discard(block["id"])
    reasons = sorted(set(reasons))
    superseded = [deepcopy(proof) for proof in mapping.get("superseded", []) if proof.get("block_id") == block["id"]]
    return {"block_id": block["id"], "questioner": identity["questioner"], "question_identity": identity,
            "status": "ready" if not reasons else "excluded", "reasons": reasons,
            **({"source_supersessions": superseded} if superseded else {}),
            **({"source_settlement_policy": "after_two_closed_questions",
                "unsettled_source_ids": unsettled_ids} if allow_unsettled else {}),
            "sources": chosen, "source_spans": selected_spans, "utterance_membership": trace,
            "dependency_source_ids": [source["id"] for source in sources if source["id"] in dependency_ids],
            "dependency_block_ids": sorted(dependency_blocks),
            "boundary_evidence": {"kind": boundary_kind, "next_block_id": following["id"] if following else None,
                "start_anchor": start_anchor, "end_anchor": end_anchor,
                **({"completion_declaration_anchor": completion_declaration_anchor,
                    "completion_scope": completion_scope} if completion_declaration_anchor else {}),
                "question_identity": identity,
                "membership_method": "명시 호명 사이 원문 좌표; 기존 chair 행의 질의 꼬리·머리 복원",
                "next_questioner": end_anchor["name"] if end_anchor and end_anchor.get("origin") in {
                    "closed_short_call", "confirmed_next_question_short_call", "interior_complete_roster_call"}
                    else following.get("questioner", "") if following else "",
                "next_utterance_ids": [row["id"] for row in next_rows[:2]],
                "next_text": [row.get("original_text") or row.get("text", "") for row in next_rows[:2]],
                "shutdown_closed_is_completion": False},
            "metrics": {"source_count": len(chosen), "selected_characters": sum(len(s["selected_text"]) for s in chosen),
                        "utterance_count": len(targets), "membership_utterance_count": len(trace),
                        "restored_utterance_count": sum(row["restored_from_boundary"] for row in trace),
                        "unmapped_utterances": sum(bool(row["reason"]) for row in trace)},
            "ground_truth_verified": False}


def _intersections(left: list[dict], right: list[dict]) -> list[dict]:
    """동일 원단위에서 겹치는 실제 문자만 반환한다."""
    return _merge([{"source_id": a["source_id"], "start": max(a["start"], b["start"]),
                    "end": min(a["end"], b["end"])} for a in left for b in right
                   if a["source_id"] == b["source_id"] and max(a["start"], b["start"]) < min(a["end"], b["end"])])


def _single_jamo_difference(left: str, right: str) -> bool:
    """한글 이름의 자모 한 개 차이만 후보 근거로 삼는다. 이름을 교체하지 않는다."""
    if len(left) != len(right):
        return False
    def components(value):
        out = []
        for char in value:
            code = ord(char) - 0xAC00
            if not 0 <= code < 11172:
                return None
            out.extend((code // 588, code // 28 % 21, code % 28))
        return out
    a, b = components(left), components(right)
    return a is not None and b is not None and sum(x != y for x, y in zip(a, b)) == 1


def _question_identity(block: dict, anchor: dict | None, roster) -> dict:
    """호명 경계와 이름 신뢰도를 분리한다. 유일한 철자 차이도 원문 이름을 치환하지 않는다."""
    questioner = str(block.get("questioner") or "").strip()
    raw_name = str((anchor or {}).get("name") or "")
    members = [person for person in roster if person.get("role") == "member"]
    exact = [person for person in members if person.get("name") == raw_name]
    result = {"kind": "stored_question" if block.get("kind") == "question" else "unresolved",
              "original_kind": block.get("kind"), "original_questioner": questioner,
              "questioner": questioner, "questioner_party": block.get("questioner_party") or "",
              "raw_call_name": raw_name, "status": "not_available" if not raw_name else "exact",
              "raw_name_rewritten": False, "uncertain": False}
    if block.get("kind") in {"question", "other"} and not questioner and len(exact) == 1:
        result.update(kind="derived_from_named_call", questioner=raw_name,
                      questioner_party=exact[0].get("party") or "", status="roster_exact_call")
        return result
    if block.get("kind") == "question" and not questioner and raw_name and not exact:
        candidates = [person["name"] for person in members if _single_jamo_difference(person["name"], raw_name)]
        if len(candidates) == 1:
            # 후보는 문맥 모델에 전달하되 저장 질의자·원문 이름을 확정하거나 치환하지 않는다.
            result.update(kind="uncertain_named_call", status="roster_candidate_uncertain",
                          uncertain=True, roster_candidates=candidates)
            return result
    if not questioner or not raw_name or questioner == raw_name:
        return result
    # 다른 실제 의원의 정확한 이름이거나 후보가 둘 이상이면 경계를 임의로 붙이지 않는다.
    candidates = [person["name"] for person in members if len(person["name"]) == len(raw_name)
                  and sum(a != b for a, b in zip(person["name"], raw_name)) == 1]
    if not exact and len(candidates) == 1 and candidates[0] == questioner:
        result.update(status="name_mismatch_uncertain", uncertain=True, roster_candidates=candidates)
    else:
        result.update(status="conflict", uncertain=True, roster_candidates=[p["name"] for p in exact] or candidates)
    return result
