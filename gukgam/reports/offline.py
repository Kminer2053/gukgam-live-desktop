"""종료 세션의 저장 판본과 현재 화면을 네트워크 없는 한 파일로 묶는다."""
from __future__ import annotations

import base64
import hashlib
import gzip
import json
import re
from copy import deepcopy
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, ChainableUndefined, select_autoescape

from .. import serialize
from ..db import now_ms
from ..stage_mirror import public_source
from .export import export_session, _filename
from .situation import build_situation_report

WEB = Path(__file__).resolve().parents[1] / "web"
STATIC = WEB / "static"
SESSION_FIELDS = frozenset({"id", "title", "committee", "agencies", "meeting_date", "status", "status_label",
    "source_type", "source_label", "started_at", "ended_at", "created_at", "notes", "stats"})


def _final_snapshot(value: dict) -> dict:
    """최신 표시값과 원문 연결만 남기고 중간 처리 자료를 제외한다."""
    result = deepcopy(value)
    for key in ("documents", "live_turns", "valid_live_turn_ids"):
        result.pop(key, None)
    fields = {"id", "block_id", "questioner", "status", "created_at", "updated_at", "source_ids", "source_revisions"}
    sentence_fields = {"id", "original_text", "text", "role", "speaker", "role_uncertain", "text_uncertain", "source_spans"}
    for projection in result.get("display_blocks", []):
        document = projection.get("document") or {}
        final = {key: document[key] for key in fields if key in document}
        final["sentences"] = [{key: row[key] for key in sentence_fields if key in row}
                              for row in document.get("sentences", [])]
        policy = document.get("source_settlement_policy")
        if isinstance(policy, dict):
            final["source_settlement_policy"] = {key: policy[key] for key in ("policy", "unconfirmed_source_ids") if key in policy}
        projection["document"] = final
    return result


def _json(value) -> str:
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for before, after in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
                          ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        result = result.replace(before, after)
    return result


def collect_offline_session(repo, sid: str, actor: str) -> dict:
    """호출부의 독립 RO 트랜잭션에서 전체를 읽는다. 모델·표시본 재구성은 하지 않는다."""
    session = repo.get_session(sid)
    if session is None:
        raise KeyError(sid)
    if session["status"] != "ended":
        raise ValueError("종료한 세션만 단독 HTML로 내려받을 수 있습니다")
    documents = repo.list_stage_documents(sid)
    if any(row.get("status") == "processing" for row in documents):
        raise ValueError("진행 중인 보정이 끝난 뒤 단독 HTML을 내려받으세요")
    raw = repo.list_utterances(sid, limit=-1)
    originals = [{**serialize.utterance(row), "original_text": row.get("original_text", ""),
                  "review_parts": deepcopy(row.get("review_parts") or [])} for row in raw]
    common = repo.display_snapshot(sid) if repo.display_managed(sid) or repo.display_initialized(sid) else None
    source_rows = repo.all_caption_sources(sid)
    saved_sources = {row["id"]: row for row in (common or {}).get("sources", [])}
    sources = [public_source({**row, "speaker_guess": saved_sources.get(row["id"], {}).get("speaker_guess")})
               for row in source_rows]
    snapshot = _final_snapshot(common) if common is not None else {
        "utterances": [serialize.utterance(row) for row in raw],
        "blocks": [serialize.block(row) for row in repo.list_blocks(sid)],
    }
    # 원문과 저장 표시값은 별도로 남긴다. 화면은 선택된 3→2→1 값을 그대로 쓴다.
    snapshot.update(session={k: v for k, v in serialize.session(session, repo.session_stats(sid)).items()
                             if k in SESSION_FIELDS},
        items=[serialize.item(row) for row in repo.list_items(sid, include_dismissed=True, limit=-1)],
        alerts=[serialize.alert(row) for row in repo.list_alerts(sid, limit=-1)],
        roster=[{k: row.get(k) for k in ("name", "role", "party", "title", "aliases")}
                for row in repo.get_roster(sid)],
        summary=serialize.summary(repo.latest_summary(sid, "session")),
        rolling=serialize.summary(repo.latest_summary(sid, "rolling")),
        bookmarks=[row["utterance_id"] for row in repo.list_bookmarks(sid, actor)],
        analysis=None, last_event_id=0, me={"role": "viewer", "name": ""})
    snapshot["sources"] = sources
    snapshot.pop("utterance_page", None)
    ids = {row["id"] for row in originals}
    if ids != {row["id"] for row in snapshot["utterances"]}:
        raise ValueError("전체 원문과 저장 표시본의 발언 범위가 달라 다운로드를 중단했습니다")
    reports = {str(int(org)): build_situation_report(repo, sid, org_only=org, _session_summary=snapshot["summary"])
               for org in (False, True)}
    exports = {}
    for org in (False, True):
        for fmt in ("md", "txt", "json", "csv", "docx"):
            body, mime, name = export_session(repo, sid, fmt, org_only=org, _session_summary=snapshot["summary"])
            if fmt == "json":
                safe_export = json.loads(body)
                safe_export["session"] = {k: v for k, v in safe_export["session"].items() if k in SESSION_FIELDS}
                body = json.dumps(safe_export, ensure_ascii=False, indent=1).encode()
            exports[f"{int(org)}:{fmt}"] = {"name": name, "mime": mime,
                                           "base64": base64.b64encode(body).decode("ascii")}
    # 과거 판본·처리 후보를 보관하지 않고 최신 결과만 넣는다.
    counts = {"utterances": len(originals), "sources": len(sources),
              "blocks": len(snapshot["blocks"]), "display_rows": len(snapshot.get("display_rows", [])),
              "corrected_blocks": len(snapshot.get("display_blocks", [])),
              "items": len(snapshot["items"]), "alerts": len(snapshot["alerts"]),
              "manual_speakers": sum(row.get("speaker_source") == "manual" for row in originals),
              "edited_utterances": sum(row["edited"] for row in originals)}
    payload = {"snapshot": snapshot, "originals": originals, "reports": reports, "exports": exports,
               "manifest": {"format": "gukgam-offline-v2", "generated_at": now_ms(), "counts": counts,
                            "contents": "최신 정리 기록과 원문 연결; 중간 처리 자료·과거 판본 제외",
                            "display_generation": snapshot.get("display_generation"),
                            "content_sha256": hashlib.sha256(_json([snapshot, originals]).encode()).hexdigest()}}
    return payload


def _bundle_modules(names: list[str]) -> str:
    """정해진 작은 ES 모듈 집합을 결정적으로 묶는다. 런타임 로더·외부 의존성은 없다."""
    built = []
    known = set()
    pattern = re.compile(r"^import\s*\{([\s\S]*?)\}\s*from\s*['\"]\./([^'\"]+)['\"];", re.M)
    for name in names:
        source = (STATIC / "js" / name).read_text(encoding="utf-8")
        # JS 식별자에는 $도 포함된다. DOM 선택 함수 $/$$를 빠뜨리면 화면 기동이 중단된다.
        exported = re.findall(r"^export\s+(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)", source, re.M)

        def imported(match):
            dependency = match[2]
            if dependency not in known:
                raise ValueError(f"단독 화면 모듈 순서를 확인하세요: {dependency}")
            members = re.sub(r"\b(\w+)\s+as\s+(\w+)\b", r"\1: \2", match[1])
            return f"const {{{members}}} = modules[{json.dumps(dependency)}];"

        source = pattern.sub(imported, source)
        source = re.sub(r"^export\s+(?=(?:async\s+)?(?:function|class|const|let|var)\s)", "", source, flags=re.M)
        if re.search(r"^\s*(?:import\s|export\s)", source, re.M):
            raise ValueError(f"단독 화면이 지원하지 않는 모듈 선언입니다: {name}")
        built.append(f"modules[{json.dumps(name)}] = (() => {{\n{source}\nreturn {{{','.join(exported)}}};\n}})();")
        known.add(name)
    return "\n".join(built)


def render_offline_session(payload: dict) -> str:
    """현재 로컬 템플릿·스타일·동작을 같은 자료에 연결한 한 파일을 반환한다."""
    env = Environment(loader=FileSystemLoader(WEB / "templates"), undefined=ChainableUndefined,
                      autoescape=select_autoescape(("html",)))
    snapshot = payload["snapshot"]
    document = env.get_template("session.html").render(sid=snapshot["session"]["id"],
        session_title=snapshot["session"]["title"], me={"role": "viewer", "name": ""},
        auth_enabled=True, version="", cloud=False, offline=True, request=None)
    # 템플릿에서 허용한 로컬 스타일·이미지만 인라인한다.
    def stylesheet(match):
        path = STATIC / match[1]
        if path.parent != STATIC / "css":
            raise ValueError("알 수 없는 단독 화면 스타일입니다")
        return "<style>" + path.read_text(encoding="utf-8").replace("</style", "<\\/style") + "</style>"
    document = re.sub(r'<link rel="stylesheet" href="/static/([^"?]+)(?:\?[^\"]*)?">', stylesheet, document)
    document = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', "", document)
    document = re.sub(r'<link rel="icon"[^>]+>', "", document)
    document = document.replace('</head>', '<style>' + (STATIC / 'css/report.css').read_text(encoding="utf-8") + '</style></head>')
    logo = base64.b64encode((STATIC / "img/logo.svg").read_bytes()).decode()
    document = document.replace('src="/static/img/logo.svg"', f'src="data:image/svg+xml;base64,{logo}"')
    icons = (STATIC / "img/icons.svg").read_text(encoding="utf-8")
    icons = re.sub(r'<\?xml[^>]*>', "", icons)
    icons = icons.replace('<svg ', '<svg aria-hidden="true" style="display:none" ', 1)
    document = document.replace('<body ', '<body data-offline="1" ', 1)
    adapter = (STATIC / "js/offline-session.js").read_text(encoding="utf-8")
    modules = _bundle_modules(["common.js", "closed-block-view.js", "transcript-stages.js", "report-view.js", "session.js"])
    script = "(async () => { 'use strict'; const modules=Object.create(null);\n" + adapter + "\n" + modules + "\nwindow.gukgamOffline.mount(modules);\n})().catch(error => {\n" \
        "const target=document.getElementById('sess-loading')||document.getElementById('main');\n" \
        "if(target){const notice=document.createElement('p');notice.setAttribute('role','alert');" \
        "notice.textContent='보관 파일을 열지 못했습니다. '+String(error?.message||error);target.replaceChildren(notice);}\n});"
    if "</script" in script.lower():
        raise ValueError("실행 코드에 HTML 종료 태그가 있습니다")
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    policy = ("default-src 'none'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
              f"script-src 'sha256-{digest}'; style-src 'unsafe-inline'; img-src data:; font-src 'none';")
    document = document.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{policy}">', 1)
    packed = base64.b64encode(gzip.compress(_json(payload).encode(), mtime=0)).decode()
    document = document.replace('</body>', icons + '<script id="offline-data" type="application/octet-stream" data-compression="gzip">' +
        packed + '</script><script>' + script + '</script></body>')
    return document


def offline_filename(payload: dict) -> str:
    return _filename(payload["snapshot"]["session"], "html").replace(".html", "_단독열람.html")
