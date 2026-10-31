"""입력 변경 때만 세 판본을 검증하여 공통 표시 결과를 저장한다. 모델 호출은 없다."""
from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
from functools import lru_cache
import hashlib
import json
import re
import threading
import weakref

from . import serialize
from .db import now_ms
from .live_turns import session_live_turns, session_source_guesses
from .stage_mirror import public_document, public_source

ROLE_NAMES = {"member":"질의 의원", "answer":"답변 기관", "chair":"위원장 진행", "other":"기타 발언"}
STATUS_FIELDS = ("document_status", "text_status", "text_uncertain", "role_uncertain")
_MEMOS=weakref.WeakKeyDictionary()
_MEMO_LOCK=threading.RLock()


def _memo_get(db,sid):
    with _MEMO_LOCK:
        sessions=_MEMOS.get(db)
        if not sessions:return None
        value=sessions.get(sid)
        if value:sessions.move_to_end(sid)
        return value


def _memo_put(db,sid,value):
    with _MEMO_LOCK:
        sessions=_MEMOS.setdefault(db,OrderedDict())
        sessions[sid]=value;sessions.move_to_end(sid)
        while len(sessions)>2:sessions.popitem(last=False)


def _input_bundle(repo,sid,previous=None,changes=()):
    """직전 RO 입력을 유지하고 변경 기록의 행만 새 RO 연결에서 다시 읽는다."""
    if (repo.get_session(sid) or {}).get("source_type")=="file":
        # 녹음 원발언은 caption_sources에 넣지 않는다. 명시 처리한 파일의 입력을
        # 같은 RO 스냅샷으로 보관하고 build 입구의 녹음 전용 투영에 전달한다.
        from .recording_sources import recording_sources
        rows=repo.list_utterances(sid,limit=-1)
        return {"sources":recording_sources(rows),"rows":rows,
            "blocks":repo.display_build_blocks(sid),"segments":[],
            "documents":repo.list_stage_documents(sid),"roster":repo.get_roster(sid)}
    if previous is None:
        return {"sources":repo.all_caption_sources(sid),"rows":repo.list_utterances(sid,limit=-1),
            "blocks":repo.display_build_blocks(sid),"segments":repo.stage_raw_segments(sid),
            "documents":repo.list_stage_documents(sid),"roster":repo.get_roster(sid)}
    value={kind:list(rows) for kind,rows in previous.items()}
    def numeric(kind):
        return {int(c["row_key"]) for c in changes if c["kind"]==kind and c["row_key"].isdigit()}
    def replace(kind,ids,rows,key="id"):
        if not ids:return
        mapped={row[key]:row for row in value[kind] if row[key] not in ids}
        mapped.update({row[key]:row for row in rows})
        value[kind]=list(mapped.values())
    source_ids=numeric("caption_sources")
    replace("sources",source_ids,repo.caption_sources_by_ids(sid,sorted(source_ids)))
    segment_keys={c["row_key"] for c in changes if c["kind"]=="segments"}
    replace("segments",segment_keys,[row for key in segment_keys if (row:=repo.get_segment(sid,key))],"source_key")
    uids=numeric("utterances")|{row["id"] for row in value["rows"] if str(row.get("source_key") or "").rpartition("#")[0] in segment_keys}
    replace("rows",uids,repo.stage_utterances_by_ids(sid,sorted(uids)))
    bids=numeric("blocks")
    replace("blocks",bids,[row for bid in bids if (row:=repo.stage_block_record(sid,bid))])
    dids=numeric("transcript_stage_documents")|numeric("transcript_stage_sources")
    replace("documents",dids,[row for did in dids if (row:=repo.get_stage_document(sid,did))])
    if any(c["kind"]=="roster" for c in changes):value["roster"]=repo.get_roster(sid)
    value["sources"].sort(key=lambda r:(r["first_rx"],r["id"]))
    value["rows"].sort(key=lambda r:(r["ts_start"],r.get("source_order") or f"{r['seq']:012d}",r["seq"]))
    value["blocks"].sort(key=lambda r:(r["ts_start"],r["id"]))
    value["documents"].sort(key=lambda r:r["id"])
    return value


class _BuildRepo:
    """실제 현재성 질의만 RO Repo에 맡기며 저장 입력 목록은 작업자 메모리를 재사용한다."""
    _display_building=True
    def __init__(self,repo,bundle,memo):self._repo,self.bundle,self.memo=repo,bundle,memo or {}
    def __getattr__(self,name):return getattr(self._repo,name)
    def all_caption_sources(self,_sid):return self.bundle["sources"]
    def list_utterances(self,_sid,**_kwargs):return self.bundle["rows"]
    def display_build_blocks(self,_sid):return [dict(b) for b in self.bundle["blocks"]]
    def stage_block_headers(self,_sid):return self.bundle["blocks"]
    def stage_raw_segments(self,_sid):return self.bundle["segments"]
    def list_stage_documents(self,_sid):return self.bundle["documents"]
    def get_roster(self,_sid):return self.bundle["roster"]
    def display_guess_state(self,sid,kind,key):
        states=self.memo.get("states")
        return states.get((kind,key)) if states is not None else self._repo.display_guess_state(sid,kind,key)


def _json(value):
    return json.dumps(value,ensure_ascii=False,separators=(",",":"),allow_nan=False)


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(",",":"),allow_nan=False).encode()).hexdigest()


def _public_document(document):
    """원문 대체 증명도 보존하되 내부 모델 요청·파일 경로는 표시판에 넣지 않는다."""
    result=public_document(document)
    if "source_supersessions" in document:
        def pick(value,keys):
            return {k:deepcopy(value[k]) for k in keys if k in value} if isinstance(value,dict) else None
        proofs=[]
        for proof in _list(document.get("source_supersessions")):
            value=pick(proof,("method","utterance_id","source_key","block_id","original_text"))
            if value is None:
                proofs.append(None);continue
            value["sources"]=[pick(s,("source_id","source_key","segment","revision","historical_revision","historical_text"))
                for s in _list(proof.get("sources"))]
            value["replacement_utterances"]=[]
            for row in _list(proof.get("replacement_utterances")):
                replacement=pick(row,("utterance_id","source_key","block_id","original_text"))
                if replacement is not None:
                    replacement["source_spans"]=[pick(s,("source_id","start","end")) for s in _list(row.get("source_spans"))]
                    refs=row.get("source_revisions")
                    replacement["source_revisions"]={str(k):v for k,v in refs.items() if type(v) is int} if isinstance(refs,dict) else {}
                value["replacement_utterances"].append(replacement)
            proofs.append(value)
        result["source_supersessions"]=proofs if isinstance(document.get("source_supersessions"),list) else None
    return result


def _valid_id(value):
    return type(value) is int and value > 0


def _list(value):
    return value if isinstance(value,list) else []


def _protected(row):
    return bool(row and (row.get("protected") or row.get("edited") or row.get("edited_by")
        or row.get("speaker_source")=="manual" or row.get("review_parts")
        or isinstance(row.get("original_text"),str) and row.get("text")!=row["original_text"]))


@lru_cache(maxsize=8192)
def _positions(text):
    skipped = set()
    for match in re.finditer(r"^[\t ]*-\s+",text,re.MULTILINE):
        skipped.update(range(match.start(),match.end()))
    return tuple(i for i,c in enumerate(text) if not c.isspace() and i not in skipped)


@lru_cache(maxsize=8192)
def _time(ms):
    return serialize.hms(ms)


def _utterance(row):
    """공개 원행 스키마를 유지하고 반복되는 표시 시각 문자열만 재사용한다."""
    out={"id":row["id"],"session_id":row["session_id"],"seq":row["seq"],"block_id":row.get("block_id"),
        "ts_start":row["ts_start"],"ts_end":row.get("ts_end"),"time":_time(row["ts_start"]),
        "speaker":row.get("speaker") or "","speaker_role":row.get("speaker_role") or "unknown",
        "speaker_source":row.get("speaker_source") or "inferred","text":row.get("text") or "",
        "edited":(row.get("text") or "")!=(row.get("original_text") or ""),"is_final":bool(row.get("is_final")),
        "mentions":row.get("mentions") or []}
    from .models import ROLE_LABELS
    out["role_label"]=ROLE_LABELS.get(out["speaker_role"],"")
    out.update(serialize.acoustic_identity(row))
    for key in ("source_order","turn_id","boundary_reason"):
        if row.get(key):out[key]=row[key]
    if row.get("review_parts"):
        out["review_parts"]=[{**p,"text":out["text"][p["start"]:p["end"]]} for p in row["review_parts"]]
    return out


def _compact(text):
    return "".join(text[i] for i in _positions(text))


def _valid_span(span,sources):
    if not isinstance(span,dict):
        return False
    uid,start,end = span.get("source_id"),span.get("start"),span.get("end")
    source = sources.get(uid)
    return bool(source and _valid_id(uid) and isinstance(source.get("text"),str)
        and type(start) is int and type(end) is int and 0<=start<end<=len(source["text"]))


def _coordinates(turn,row,sources,meaningful):
    if not isinstance(turn.get("original_text"),str) or not isinstance(row.get("text"),str):
        return None
    refs = [(span["source_id"],i,sources[span["source_id"]]["text"][i])
        for span in turn["source_spans"] for i in meaningful[span["source_id"]]
        if span["start"]<=i<span["end"]]
    positions = _positions(row["text"])
    content = "".join(r[2] for r in refs)
    if content!=_compact(turn["original_text"]) or content!=_compact(row["text"]):
        return None
    return {"text":row["text"],"positions":positions,
        "refs":[{"source_id":r[0],"source_offset":r[1],"offset":positions[i]} for i,r in enumerate(refs)]}


def _ranges(text,positions,selected):
    output,first,last = [],None,None
    def flush():
        nonlocal first,last
        if first is not None:
            output.append({"start":0 if first==0 else positions[first],
                "end":len(text) if last==len(positions)-1 else positions[last]+1})
        first=last=None
    for index,pos in enumerate(positions):
        if pos not in selected:
            flush()
        else:
            first=index if first is None else first
            last=index
    flush()
    return output


def _groups(sentences,paragraphs):
    by_id = {str(s["id"]):s for s in sentences}
    groups = [[by_id.get(str(uid)) for uid in _list(p.get("sentence_ids"))] if isinstance(p,dict) else [] for p in paragraphs]
    expected = [str(s["id"]) for s in sentences]
    actual = [str(s.get("id")) if s else "None" for g in groups for s in g]
    valid = bool(groups and actual==expected and all(g and all(s and _role(s)==_role(g[0])
        and _speaker(s)==_speaker(g[0]) for s in g) for g in groups))
    return list(zip(groups,paragraphs)) if valid else [([s],None) for s in sentences]


def _role(sentence):
    return sentence.get("role") if sentence.get("role") in ROLE_NAMES else "other"


def _speaker(sentence):
    speaker=sentence.get("speaker")
    return speaker if isinstance(speaker,str) and speaker.strip() and speaker!="화자 확인 필요" else ROLE_NAMES[_role(sentence)]


def _summary(summary,document):
    if not isinstance(summary,dict) or summary.get("status")=="held":
        return None
    return {**summary,"one_line":summary.get("title") or summary.get("one_line") or "",
        "question_points":_list(summary.get("bullets")),"answer_points":_list(summary.get("answer_points")),
        "issues":_list(summary.get("issues")),"data_requests":_list(summary.get("key_requests")),
        "commitments":_list(summary.get("commitments")),"provider":"closed_block",
        "source_document_id":document["id"],"ai_draft":True}


def _superseded(block,doc,sources,turns,rows,cards,consumed):
    """옛 조립 행 전체 대체는 완전한 같은 원조각 판본 증명에만 허용한다."""
    if "source_supersessions" not in doc:
        return {}
    if not isinstance(doc["source_supersessions"],list):
        return None
    result,seen={},set()
    turn_map={r["id"]:r for r in turns}
    sentence_spans=[span for s in doc["sentences"] for span in s["source_spans"]]
    for proof in doc["source_supersessions"]:
        if not isinstance(proof,dict):
            return None
        uid=proof.get("utterance_id"); old=rows.get(uid)
        if (proof.get("method")!="same_source_revision_replacement_v1" or not _valid_id(uid) or uid in seen
            or not old or _protected(old) or old.get("block_id")!=block["id"] or proof.get("block_id")!=block["id"]
            or not isinstance(proof.get("source_key"),str) or not proof["source_key"]
            or "source_key" in old and old["source_key"]!=proof["source_key"]
            or not isinstance(proof.get("original_text"),str) or not proof["original_text"]
            or old.get("text")!=proof["original_text"] or old.get("original_text",proof["original_text"])!=proof["original_text"]
            or len(_list(proof.get("sources")))!=1 or not _list(proof.get("replacement_utterances"))
            or consumed.get(str(uid)) or any(uid in c["source_utterance_ids"] for c in cards)):
            return None
        historical=proof["sources"][0]
        source=sources.get(historical.get("source_id")) if isinstance(historical,dict) else None
        if (not source or source["id"] not in doc["source_ids"] or not historical.get("source_key")
            or source.get("source_key")!=historical["source_key"] or str(source.get("segment"))!=str(historical.get("segment"))
            or not _valid_id(historical.get("revision")) or source["revision"]!=historical["revision"]
            or doc.get("source_revisions",{}).get(str(source["id"]))!=historical["revision"]
            or not _valid_id(historical.get("historical_revision")) or historical["historical_revision"]>=historical["revision"]
            or not isinstance(historical.get("historical_text"),str) or not _compact(historical["historical_text"])
            or _compact(historical["historical_text"])!=_compact(proof["original_text"])
            or source.get("final") is not True or source.get("scd") is not True or source.get("replayed") is True):
            return None
        required=_positions(source["text"])
        if not required or any(not any(s["source_id"]==source["id"] and s["start"]<=i<s["end"] for s in sentence_spans) for i in required):
            return None
        replacements,spans=set(),[]
        for replacement in proof["replacement_utterances"]:
            if not isinstance(replacement,dict):
                return None
            rid=replacement.get("utterance_id"); row=rows.get(rid); turn=turn_map.get(rid)
            refs=replacement.get("source_revisions")
            if (not _valid_id(rid) or rid==uid or rid in replacements or not row or not turn or _protected(row) or _protected(turn)
                or any(v.get("block_id")!=block["id"] for v in (replacement,row,turn))
                or not isinstance(replacement.get("source_key"),str) or not replacement["source_key"]
                or any("source_key" in v and v["source_key"]!=replacement["source_key"] for v in (row,turn))
                or not isinstance(replacement.get("original_text"),str) or not replacement["original_text"]
                or row.get("text")!=replacement["original_text"] or turn.get("original_text")!=replacement["original_text"]
                or row.get("original_text",replacement["original_text"])!=replacement["original_text"]
                or replacement.get("source_spans")!=turn.get("source_spans")
                or not isinstance(refs,dict) or not refs or refs!=turn.get("source_revisions")
                or any(not str(k).isdigit() or not _valid_id(v) or sources.get(int(k),{}).get("revision")!=v for k,v in refs.items())
                or not any(rid in c["source_utterance_ids"] for c in cards)
                or consumed.get(str(rid))!=[{"start":0,"end":len(row["text"])}]):
                return None
            replacements.add(rid);spans.extend(turn["source_spans"])
        if any(not any(s["source_id"]==source["id"] and s["start"]<=i<s["end"] for s in spans) for i in required):
            return None
        seen.add(uid);result[str(uid)]=[{"start":0,"end":len(old["text"])}]
    return result


def _card_status(document, group, paragraph=None):
    document_status=document.get("status")
    text_status=paragraph.get("text_status") if paragraph else None
    return {"document_status":document_status if isinstance(document_status,str) and document_status in {"draft","partial","ready"} else "unknown",
        "text_status":(text_status if isinstance(text_status,str) and text_status in {"draft","held","pending"}
            else "held" if any(s.get("text_status")=="held" for s in group)
            else "draft" if group and all(s.get("text_status")=="draft" for s in group) else "unknown"),
        "text_uncertain":bool(paragraph and paragraph.get("text_uncertain") is True)
            or any(s.get("text_uncertain") is True for s in group),
        "role_uncertain":bool(paragraph and paragraph.get("role_uncertain") is True)
            or any(s.get("role_uncertain") is True for s in group)}


def annotate_display_status(payload):
    """옛 저장 카드의 본문·선택을 바꾸지 않고 같은 저장 문서의 상태만 보충한다."""
    cards={}
    for projection in _list(payload.get("display_blocks")):
        if not isinstance(projection,dict):continue
        document=projection.get("document") if isinstance(projection.get("document"),dict) else {}
        sentences={str(row.get("id")):row for row in _list(document.get("sentences")) if isinstance(row,dict)}
        paragraphs={tuple(map(str,_list(row.get("sentence_ids")))):row for row in _list(document.get("paragraphs")) if isinstance(row,dict)}
        for card in _list(projection.get("cards")):
            if not isinstance(card,dict):continue
            if not all(key in card for key in STATUS_FIELDS):
                ids=tuple(map(str,_list(card.get("sentence_ids"))))
                paragraph=paragraphs.get(ids)
                if not (paragraph and paragraph.get("body_basis")=="source_group_v2" and paragraph.get("text")==card.get("text")):
                    paragraph=None
                status=_card_status(document,[sentences[uid] for uid in ids if uid in sentences],paragraph)
                for key,value in status.items():
                    card.setdefault(key,value)
            cards[card.get("key")]=(projection.get("blockId"),card)
    for row in _list(payload.get("display_rows")):
        if not isinstance(row,dict):continue
        owner,card=cards.get(row.get("key"),(None,None))
        if row.get("stage")==3 and card is not None and row.get("block_id")==owner:
            for key in STATUS_FIELDS:
                row.setdefault(key,card[key])
    return payload


def closed_block_projection(block,doc,sources,turns,rows):
    """불완전한 과거 문서는 원문을 유지하고 표시 대체만 거절한다."""
    try:
        return _closed_block_projection(block,doc,sources,turns,rows)
    except (TypeError,ValueError,KeyError,IndexError,AttributeError):
        return None


def _closed_block_projection(block,doc,sources,turns,rows):
    """검증된 출처 문자만 대체한다. 기존 JS 표시 계약과 같은 유니코드 문자 좌표를 쓴다."""
    summary=block.get("summary") or {}
    if (not _valid_id(block.get("id")) or block.get("is_open") or block.get("review_status")=="reviewed"
        or isinstance(summary,dict) and summary.get("edited_by") or doc.get("block_id")!=block["id"]
        or doc.get("mode")!="closed_block_v1" or doc.get("status") not in {"draft","partial","ready"}
        or block.get("questioner") and block["questioner"]!=doc.get("questioner")):
        return None
    sentences,paragraphs=_list(doc.get("sentences")),_list(doc.get("paragraphs"))
    if (not sentences or any(not isinstance(s,dict) or not isinstance(s.get("id"),(str,int))
        or isinstance(s["id"],bool) or not str(s["id"]) or not isinstance(s.get("text",s.get("original_text")),str)
        or not _list(s.get("source_spans")) for s in sentences)
        or len({str(s["id"]) for s in sentences})!=len(sentences)):
        return None
    source_map={s["id"]:s for s in sources if _valid_id(s.get("id")) and isinstance(s.get("text"),str)}
    row_map={r["id"]:r for r in rows if _valid_id(r.get("id"))}
    if any(r.get("block_id")==block["id"] and _protected(r) for r in rows):
        return None
    source_ids=set(_list(doc.get("source_ids")))
    if any(not _valid_span(s,source_map) or s["source_id"] not in source_ids for sentence in sentences for s in sentence["source_spans"]):
        return None
    candidates=[t for t in turns if t.get("id") in row_map and _list(t.get("source_spans"))
        and all(_valid_span(s,source_map) for s in t["source_spans"])]
    meaningful={uid:_positions(s["text"]) for uid,s in source_map.items()}
    cards,coordinates,consumed=[],{},{}
    for group,paragraph in _groups(sentences,paragraphs):
        spans=[span for sentence in group for span in sentence["source_spans"]]
        use_paragraph=bool(paragraph and paragraph.get("body_basis")=="source_group_v2"
            and isinstance(paragraph.get("text"),str) and paragraph["text"].strip())
        text=paragraph["text"] if use_paragraph else "\n".join(s.get("text",s.get("original_text")) for s in group)
        count=sum(sum(span["start"]<=i<span["end"] for i in meaningful[span["source_id"]]) for span in spans)
        if not count or not _positions(text):
            if not count and not _positions(text):
                continue
            return None
        mapped=[]
        for span in spans:
            significant=[i for i in meaningful[span["source_id"]] if span["start"]<=i<span["end"]]
            if not significant:
                continue
            matches=[t for t in candidates if any(r["source_id"]==span["source_id"] and r["start"]<span["end"]
                and span["start"]<r["end"] for r in t["source_spans"])]
            if not matches or any(_protected(t) or _protected(row_map[t["id"]]) for t in matches):
                return None
            refs=set()
            for turn in matches:
                uid=turn["id"]
                if uid not in coordinates:
                    coordinates[uid]=_coordinates(turn,row_map[uid],source_map,meaningful)
                projection=coordinates[uid]
                if projection is None:
                    return None
                used=[r for r in projection["refs"] if r["source_id"]==span["source_id"] and span["start"]<=r["source_offset"]<span["end"]]
                if not used:
                    continue
                consumed.setdefault(uid,set()).update(r["offset"] for r in used)
                refs.update(r["source_offset"] for r in used)
                if uid not in mapped:
                    mapped.append(uid)
            if not set(significant)<=refs:
                return None
        if not mapped:
            return None
        times=[row_map[uid].get("ts_start") for uid in mapped if type(row_map[uid].get("ts_start")) in (int,float)]
        cards.append({"key":f"closed-{doc['id']}-{group[0]['id']}","text":text,"speaker":_speaker(group[0]),
            "speaker_role":_role(group[0]),"source_utterance_ids":mapped,
            "ts_start":min(times) if times else source_map[spans[0]["source_id"]].get("first_rx"),
            "sentence_ids":[s["id"] for s in group],**_card_status(doc,group,paragraph if use_paragraph else None)})
        voices=[serialize.acoustic_identity(row_map[uid]) for uid in mapped]
        if voices and voices[0] and all(value==voices[0] for value in voices):
            cards[-1].update(voices[0])
    if not cards:
        return None
    ranges={str(uid):_ranges(coordinates[uid]["text"],coordinates[uid]["positions"],selected) for uid,selected in consumed.items()}
    superseded=_superseded(block,doc,source_map,candidates,row_map,cards,ranges)
    if superseded is None:
        return None
    ranges.update(superseded)
    projected={"id":doc["id"],"blockId":block["id"],"document":doc,"summary":_summary(doc.get("summary"),doc),
        "cards":cards,"consumed_utterance_ranges":ranges}
    projected["fingerprint"]=_hash([doc,cards,projected["summary"],ranges,
        [[s["id"],s["revision"]] for s in sources if s["id"] in source_ids]])
    return projected


def _affected(document,cache,changes):
    if not cache or cache.get("document_hash")!=_hash(document):
        return True
    if any(not all(key in card for key in STATUS_FIELDS) for card in (cache.get("projection") or {}).get("cards", [])):
        return True
    uids=set((document.get("membership") or {}).get("utterance_ids") or [])
    bids=set((document.get("membership") or {}).get("block_ids") or [])|{document.get("block_id")}
    bids.update(int(x) for x in (document.get("dependency_blocks") or {}) if str(x).isdigit())
    sources=set(document.get("source_ids") or [])|{int(x) for x in document.get("source_revisions") or {} if str(x).isdigit()}
    sources.update(cache.get("dependency_source_ids") or [])
    keys=set(cache.get("segment_keys") or [])
    for change in changes:
        kind,key=change["kind"],change["row_key"]
        numeric=int(key) if key.isdigit() else None
        if (kind in {"sessions","roster","settings"}
            or kind=="caption_sources" and numeric in sources
            or kind=="segments" and key in keys
            or kind=="blocks" and numeric in bids
            or kind=="utterances" and (numeric in uids or change.get("block_id") in bids)
            or kind=="transcript_reviews" and change.get("block_id") in bids
            or kind in {"transcript_stage_documents","transcript_stage_sources"} and numeric==document["id"]):
            return True
    return False


def _residual(text,ranges):
    cursor=0
    for r in sorted(ranges,key=lambda r:(r["start"],r["end"])):
        if cursor<r["start"]:
            yield cursor,r["start"]
        cursor=max(cursor,r["end"])
    if cursor<len(text):
        yield cursor,len(text)


def _review_slices(row,start,end):
    """사람이 채택한 화자 구간을 그대로 유지하고 미지정 구간도 원문으로 남긴다."""
    parts=_list(row.get("review_parts"))
    if (not parts or any(not isinstance(p,dict) or type(p.get("start")) is not int
        or type(p.get("end")) is not int or not 0<=p["start"]<p["end"]<=len(row["text"])
        or p.get("role") not in {*ROLE_NAMES,"unknown","system"} for p in parts)):
        return [(start,end,None)]
    ordered=sorted(parts,key=lambda p:(p["start"],p["end"]))
    if any(a["end"]>b["start"] for a,b in zip(ordered,ordered[1:])):
        return [(start,end,None)]
    cuts=sorted({start,end,*[i for p in ordered for i in (p["start"],p["end"]) if start<i<end]})
    return [(a,b,next((p for p in ordered if p["start"]<=a and b<=p["end"]),None)) for a,b in zip(cuts,cuts[1:])]


def _display_rows(sid,rows,sources,turns,projections,guesses):
    """원행·원조각을 남기고 검증된 문자 범위만 3→2→1 순서로 선택한다."""
    result,consumed,claimed=[],{},{}
    by_turn={t["id"]:t for t in turns}; by_source={s["id"]:s for s in sources}
    meaningful={uid:_positions(s["text"]) for uid,s in by_source.items()}
    for projection in projections:
        doc=projection["document"]
        for uid,ranges in projection["consumed_utterance_ranges"].items():
            consumed.setdefault(int(uid),[]).extend(ranges)
        sentences={str(s["id"]):s for s in doc["sentences"]}
        for card in projection["cards"]:
            spans=[span for uid in card["sentence_ids"] for span in sentences[str(uid)]["source_spans"]]
            result.append({**card,"id":card["key"],"stage":3,"document_id":doc["id"],"block_id":projection["blockId"],
                "source_ids":list(dict.fromkeys(s["source_id"] for s in spans)),"source_spans":spans,
                "speaker_source":"ai_corrected","estimated":False,"is_final":True,"edited":False,"ts_end":None})
    for row in rows:
        uid=row["id"];turn=by_turn.get(uid)
        if turn:
            for span in turn["source_spans"]:
                claimed.setdefault(span["source_id"],set()).update(range(span["start"],span["end"]))
        if not turn and sources and not _protected(row) and row.get("speaker_role")!="system":
            continue
        slices=[part for start,end in _residual(row["text"],consumed.get(uid,[])) for part in _review_slices(row,start,end)]
        for start,end,review in slices:
            text=row["text"][start:end]
            if not _positions(text) and row.get("speaker_role")!="system":
                continue
            guess=(turn or {}).get("speaker_guess") or {}
            speaker=row.get("speaker") or "";role=row.get("speaker_role") or "unknown"
            if guess and not _protected(row) and (guess.get("override_existing") or not speaker or role=="unknown"):
                speaker=guess.get("speaker") or speaker;role=guess.get("role") or role
                estimated=guess.get("estimated") is True
            else:
                estimated=not _protected(row) and row.get("speaker_source")=="inferred" and bool(speaker)
            if review:
                speaker=review.get("speaker") or "";role=review["role"];estimated=False
            key=f"utterance-{uid}-{start}-{end}"
            spans=(turn or {}).get("source_spans") or []
            if turn and (start or end!=len(row["text"])):
                coordinates=_coordinates(turn,row,by_source,meaningful)
                if coordinates:
                    sliced=[]
                    for span in spans:
                        refs=[r["source_offset"] for r in coordinates["refs"] if r["source_id"]==span["source_id"]
                            and span["start"]<=r["source_offset"]<span["end"] and start<=r["offset"]<end]
                        if refs:
                            sliced.append({"source_id":span["source_id"],"start":min(refs),"end":max(refs)+1})
                    spans=sliced
            result.append({**_utterance(row),"id":key,"key":key,"stage":2 if turn or _protected(row) else 1,
                "utterance_id":uid,"source_utterance_ids":[uid],"source_ids":list(dict.fromkeys(s["source_id"] for s in spans)),
                "source_spans":spans,"start":start,"end":end,"speaker":speaker,"speaker_role":role,"text":text,
                "speaker_guess":guess or None,"estimated":estimated,
                "speaker_source":"reviewed" if review else row.get("speaker_source") or "inferred"})
    for source in sources:
        if source.get("replayed") is True:
            continue
        positions=_positions(source["text"])
        available=set(positions)-claimed.get(source["id"],set())
        for span in _ranges(source["text"],positions,available):
            start,end=span["start"],span["end"]
            guess=guesses.get(source["id"]) or {}
            key=f"source-{source['id']}-{start}-{end}"
            result.append({"id":key,"key":key,"stage":2 if guess else 1,"session_id":sid,"block_id":None,
                "speaker":guess.get("speaker") or "","speaker_role":guess.get("role") or "unknown",
                "speaker_source":"inferred" if guess else "feed","speaker_guess":guess or None,
                "estimated":guess.get("estimated") is True,
                "source_utterance_ids":[],"source_ids":[source["id"]],
                "source_spans":[{"source_id":source["id"],"start":start,"end":end}],
                "ts_start":source.get("first_rx"),"ts_end":source.get("last_rx"),
                "is_final":bool(source.get("final") and source.get("scd")),"edited":False,"text":source["text"][start:end]})
    # 같은 시각의 카드도 원래 문장 순서를 유지한다. 과거 보정이 새 자막 뒤로 이동하지 않는다.
    result.sort(key=lambda row:(row.get("ts_start") or 0,
        min(row.get("source_ids") or [0]),
        row["source_spans"][0]["start"] if row.get("source_spans") else row.get("start",0)))
    for seq,row in enumerate(result,1):
        row.setdefault("session_id",sid);row["seq"]=seq;row["order"]=seq
        row["sort_key"]=[row.get("ts_start") or 0,min(row.get("source_ids") or [0]),
            row["source_spans"][0]["start"] if row.get("source_spans") else row.get("start",0)]
    return result


def build(repo,sid,generation,*,previous_cache=None,changes=None,previous_payload=None):
    """하나의 읽기 스냅샷에서 모든 단계를 만든다. HTTP나 모델은 호출하지 않는다."""
    repo._display_building=True
    if (repo.get_session(sid) or {}).get("source_type")=="file":
        from .recording_display import build_recording_display
        return build_recording_display(repo,sid,generation,previous_cache=previous_cache,
            changes=changes,previous_payload=previous_payload)
    sources=repo.all_caption_sources(sid)
    rows=repo.list_utterances(sid,limit=-1)
    blocks=repo.display_build_blocks(sid)
    changes=changes if changes is not None else [{"kind":"sessions","row_key":sid}]
    previous=previous_payload if previous_payload is not None else repo.display_build_snapshot(sid)
    live_checkpoint={"previous_rows":(previous or {}).get("utterances"),"previous_turns":(previous or {}).get("live_turns"),
        "previous_blocks":(previous or {}).get("blocks"),
        "state_for":lambda uid:repo.display_guess_state(sid,"live_state",str(uid))}
    source_checkpoint={"state_for":lambda uid:repo.display_guess_state(sid,"source_state",str(uid))}
    mapping=repo.display_guess_state(sid,"mapping_state","numbers")
    origin_kinds={c["kind"] for c in changes}
    only_sources=bool(previous and mapping is not None and origin_kinds<={"caption_sources","transcript_stage_documents","transcript_stage_sources"})
    touched={int(c["row_key"]) for c in changes if c["kind"]=="caption_sources" and c["row_key"].isdigit()}
    touched_numbers={str(s.get("segment")) for s in [*sources,*((previous or {}).get("sources") or [])] if s["id"] in touched}
    reuse_turns=only_sources and not touched_numbers.intersection(mapping)
    if reuse_turns:
        turns=previous["live_turns"]
        live_checkpoint["retained_ids"]=[row["id"] for row in previous["utterances"]]
    else:
        turns=session_live_turns(repo,sid,checkpoint=live_checkpoint)
        from .block_sources import _PIECE
        mapping=sorted({match["segment"] for segment in repo.stage_raw_segments(sid)
            for key in ((segment.get("raw") or {}).get("segments") or {})
            if (match:=_PIECE.fullmatch(key)) is not None})
    if previous and not any(c["kind"]=="roster" for c in changes):
        old_sources=[s for s in previous["sources"] if s.get("replayed") is not True]
        guess_sources=[s for s in sources if s.get("replayed") is not True]
        prefix=0
        for index,source in enumerate(guess_sources):
            if (index>=len(old_sources) or source["id"] in touched or any(old_sources[index].get(k)!=source.get(k)
                for k in ("id","revision","first_rx","last_rx","final","scd","parts","text","replayed"))):
                break
            prefix+=1
        source_checkpoint.update(prefix=prefix,prefix_guesses={s["id"]:s.get("speaker_guess") for s in old_sources})
    guesses=session_source_guesses(repo,sid,checkpoint=source_checkpoint)
    public_sources=[public_source({**s,"speaker_guess":guesses.get(s["id"])}) for s in sources]
    by_block={b["id"]:b for b in blocks};by_row={r["id"]:r for r in rows}
    documents=repo.list_stage_documents(sid)
    cache,valid={},[]
    unchanged_cache=set()
    old=previous_cache or {}
    old_documents={d["id"]:d for d in (previous or {}).get("documents") or []}
    old_projections={p["id"]:p for p in (previous or {}).get("display_blocks") or []}
    for document in documents:
        did=document["id"]
        if document.get("mode")!="closed_block_v1" or document.get("status")=="stale":
            continue
        if not _affected(document,old.get(did),changes):
            checked={**old[did],"document":old_documents.get(did) or old[did].get("document"),
                "projection":old_projections.get(did)}
            if checked["valid"] and checked["document"] is None:
                checked["document"]=_public_document(document)
            if checked.get("projectable") and checked["projection"] is None:
                checked["projection"]=closed_block_projection(by_block.get(document.get("block_id")) or {},
                    checked["document"],public_sources,turns,rows)
            unchanged_cache.add(did)
        else:
            current=repo.stage_document_current(sid,did)
            public=_public_document(document)
            projection=closed_block_projection(by_block.get(document.get("block_id")) or {},public,public_sources,turns,rows) if current else None
            uids=(document.get("membership") or {}).get("utterance_ids") or []
            checked={"document_hash":_hash(document),"valid":current,"document":public,"projection":projection,
                "projectable":projection is not None,
                "dependency_source_ids":repo.display_document_source_ids(sid,did),
                "segment_keys":sorted({str(by_row[uid].get("source_key") or "").rpartition("#")[0] for uid in uids if uid in by_row})}
        cache[did]=checked
        if checked["valid"]:
            valid.append(checked["document"])
    latest={}
    for document in valid:
        bid=document["block_id"]
        if bid not in latest or (document.get("updated_at") or document.get("created_at") or 0,document["id"])>(latest[bid].get("updated_at") or latest[bid].get("created_at") or 0,latest[bid]["id"]):
            latest[bid]=document
    projections=[];used={}
    for doc in sorted(latest.values(),key=lambda d:d["id"],reverse=True):
        if doc.get("status") not in {"draft","partial","ready"}:
            continue
        projection=cache[doc["id"]]["projection"]
        if projection is None:
            continue
        if any(any(max(a["start"],b["start"])<min(a["end"],b["end"]) for a in spans for b in used.get(uid,[]))
                for uid,spans in projection["consumed_utterance_ranges"].items()):
            continue
        projections.append(projection)
        for uid,spans in projection["consumed_utterance_ranges"].items():
            used.setdefault(uid,[]).extend(spans)
    projections.sort(key=lambda p:(by_block[p["blockId"]]["ts_start"],p["blockId"]))
    selected_by_id={p["id"]:p["document"] for p in projections}
    for block in blocks:
        summary=block.get("summary")
        if isinstance(summary,dict) and not summary.get("edited_by") and block.get("review_status")!="reviewed":
            document=selected_by_id.get(summary.get("source_document_id"))
            if (summary.get("provider")!="closed_block" or not document or document.get("block_id")!=block["id"]
                or document.get("status") not in {"draft","partial","ready"}
                or (document.get("summary") or {}).get("status")!="draft"):
                block["summary"]=None;block["summary_at"]=None
            else:
                block["summary"]=repo.stage_summary_value(document,summary)
    display_rows=_display_rows(sid,rows,sources,turns,projections,guesses)
    payload={"version":3,"mode":"closed_block_v1","snapshot_at":now_ms(),"display_generation":generation,
        "sources":public_sources,"live_turns":turns,"documents":valid,
        "valid_document_ids":sorted(d["id"] for d in valid),"valid_live_turn_ids":sorted(t["id"] for t in turns),
        "utterances":rows,
        "blocks":[serialize.block(b) for b in blocks],
        "display_blocks":projections,"display_rows":display_rows,"display_utterance_ids":sorted(by_row),
        "display_valid_block_ids":sorted(p["blockId"] for p in projections),
        "display_valid_row_ids":[r["key"] for r in display_rows],"next_before":None}
    stage2=[]
    stage2_display=_display_rows(sid,rows,sources,turns,[],guesses)
    for kind,values in (("live_turn",turns),("source_guess",[{"id":uid,**value} for uid,value in guesses.items()]),
                        ("display_row",[r for r in stage2_display if r["stage"]==2]),
                        ("live_state",[{"id":uid,**value} for uid,value in live_checkpoint.get("current",{}).items()]),
                        ("source_state",[{"id":uid,**value} for uid,value in source_checkpoint.get("current",{}).items()]),
                        ("mapping_state",[{"id":"numbers","numbers":mapping}])):
        for value in values:
            content=value["numbers"] if kind=="mapping_state" else value
            stage2.append({"kind":kind,"row_key":str(value["id"]),"payload":_json(content),"fingerprint":_hash(content)})
    for kind,checkpoint in (("live_state",live_checkpoint),("source_state",source_checkpoint)):
        stage2.extend({"kind":kind,"row_key":str(uid),"reuse":True} for uid in checkpoint.get("retained_ids",[]))
    compact_cache={did:_json({k:v for k,v in value.items() if k not in {"document","projection"}}) for did,value in cache.items()}
    return payload,stage2,compact_cache


DISPLAY_ARRAYS=("sources","live_turns","documents","utterances","blocks","display_blocks","display_rows")


def _partition(payload,previous,initialized):
    """큰 전체 문자열을 덮지 않고 바뀐 표시 행만 저장한다. 배열 조립은 SELECT 순서뿐이다."""
    values=[]
    for kind in DISPLAY_ARRAYS:
        old={str(v["id"] if kind!="display_rows" else v["key"]):v for v in (previous or {}).get(kind) or []}
        for index,value in enumerate(payload[kind]):
            key=str(value["id"] if kind!="display_rows" else value["key"])
            row={"kind":kind,"row_key":key,"sort_order":index}
            if initialized and old.get(key)==value:
                row["reuse"]=True
            else:
                row.update(payload=_json(value),fingerprint=_hash(value))
            values.append(row)
    header={k:v for k,v in payload.items() if k not in DISPLAY_ARRAYS}
    header["_display_arrays"]=list(DISPLAY_ARRAYS)
    return _json(header),values


def refresh(repo,sid):
    """전용 실행기의 동기 함수. RO 계산 뒤 같은 입력 세대일 때만 짧게 저장한다."""
    from .repo import Repo
    version=repo.db.change_token()[1] if hasattr(repo.db,"change_token") else None
    memo=_memo_get(repo.db,sid)
    with repo.db.read_connection() as reader:
        with reader.transaction():
            view=Repo(reader);view._display_building=True
            if not view.display_managed(sid):
                return {"committed":False,"generation":0,"reason":"unmanaged"}
            state=view.display_state(sid)
            if state is None:
                return {"committed":False,"generation":0,"reason":"missing_session"}
            generation=state["input_generation"]
            if memo and (memo["built_generation"]!=state["built_generation"] or memo["db_version"]!=version
                         or memo["generation"]>generation):memo=None
            changes=view.display_changes(sid,after=memo["generation"] if memo else 0)
            if memo and generation>memo["generation"] and {r["generation"] for r in changes}!=set(range(memo["generation"]+1,generation+1)):
                memo=None;changes=view.display_changes(sid)
            if memo:
                previous=memo["payload"];cache=memo["cache"];states=memo["states"]
                bundle=_input_bundle(view,sid,memo["bundle"],changes)
            else:
                previous=view.display_build_snapshot(sid);cache=view.display_build_cache(sid)
                saved=view.display_guess_checkpoints(sid)
                states={(kind,key):value for kind,rows in saved.items() for key,value in rows.items()}
                bundle=_input_bundle(view,sid)
            if generation==state["built_generation"] and view.display_initialized(sid):
                _memo_put(repo.db,sid,{"generation":generation,"built_generation":generation,"db_version":version,
                    "payload":previous,"cache":cache,"states":states,"bundle":bundle})
                return {"committed":False,"generation":generation,"reason":"current"}
            proxy=_BuildRepo(view,bundle,{"states":states})
            payload,stage2,cache=build(proxy,sid,generation,previous_cache=cache,
                changes=changes,previous_payload=previous)
            encoded,values=_partition(payload,previous,view.display_values_initialized(sid))
    committed=repo.display_commit(sid,generation,encoded,stage2,cache,values)
    if committed:
        states=dict(states)
        wanted={(r["kind"],r["row_key"]) for r in stage2 if r["kind"] in {"source_state","live_state","mapping_state"}}
        states={key:value for key,value in states.items() if key in wanted}
        for row in stage2:
            if (row["kind"],row["row_key"]) in wanted and not row.get("reuse"):
                states[row["kind"],row["row_key"]]=json.loads(row["payload"])
        _memo_put(repo.db,sid,{"generation":generation,"built_generation":generation,"db_version":version,
            "payload":payload,"cache":{did:json.loads(value) for did,value in cache.items()},"states":states,"bundle":bundle})
    return {"committed":committed,"generation":generation,"reason":"built" if committed else "input_changed"}
