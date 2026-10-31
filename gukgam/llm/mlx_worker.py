"""로컬 MLX LLM 워커(서브프로세스). 포트 없이 stdin/stdout JSON lines 로 통신한다.

    python -m gukgam.llm.mlx_worker --model mlx-community/gemma-4-e4b-it-4bit

기동이 끝나면 {"event": "ready", "model", "load_ms"} 한 줄을 낸다(실패 시 {"event": "fatal", "error"}).
요청:  {"id": "1", "system": "...", "user": "...", "max_tokens": 1200, "json_mode": true, "temperature": 0.0}
       {"id": "2", "op": "ping"}
응답:  {"id": "1", "text": "...", "prompt_tokens": n, "gen_tokens": n, "elapsed_ms": n, "gen_tps": x}
       {"id": "1", "error": "..."}
표준출력은 프로토콜 전용이다. 라이브러리 출력은 표준오류로 돌린다.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import sys
import time
from typing import Any, TextIO


def _protocol_stream() -> TextIO:
    """fd 1 을 복제해 프로토콜 전용으로 쓰고, fd 1 자체는 stderr 로 돌린다(라이브러리 print 차단)."""
    fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(fd, "w", buffering=1, encoding="utf-8")


def _send(out: TextIO, msg: dict[str, Any]) -> None:
    out.write(json.dumps(msg, ensure_ascii=False) + "\n")
    out.flush()


class _JsonEnd:
    """스트리밍 출력에서 최상위 JSON 객체가 닫히는 시점을 찾는다(문자열 안의 괄호는 무시)."""

    def __init__(self, depth: int = 0):
        self.depth = depth
        self.in_str = False
        self.esc = False
        self.started = depth > 0

    def feed(self, s: str) -> int | None:
        """닫힌 위치(s 안 인덱스+1)를 돌려준다. 아직이면 None."""
        for i, ch in enumerate(s):
            if self.in_str:
                if self.esc:
                    self.esc = False
                elif ch == "\\":
                    self.esc = True
                elif ch == '"':
                    self.in_str = False
                continue
            if ch == '"':
                self.in_str = True
            elif ch in "{[":
                self.depth += 1
                self.started = True
            elif ch in "}]":
                self.depth -= 1
                if self.started and self.depth <= 0:
                    return i + 1
        return None


def build_prompt(tokenizer: Any, system: str, user: str, *, direct_output: bool = False) -> str:
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}] if system else \
        [{"role": "user", "content": user}]
    options = {"add_generation_prompt":True,"tokenize":False}
    if direct_output:
        options["enable_thinking"] = False
    try:
        prompt = tokenizer.apply_chat_template(msgs, **options)
    except Exception:
        # system 역할을 지원하지 않는 템플릿이면 사용자 메시지 앞에 붙인다.
        merged = [{"role": "user", "content": (system + "\n\n" + user) if system else user}]
        prompt = tokenizer.apply_chat_template(merged, **options)
    return prompt


def handle(model: Any, tokenizer: Any, req: dict[str, Any], progress=None) -> dict[str, Any]:
    from mlx_lm import stream_generate
    from mlx_lm.sample_utils import make_sampler

    t0 = time.perf_counter()
    json_mode = bool(req.get("json_mode", True))
    prompt = build_prompt(tokenizer, req.get("system") or "", req.get("user") or "",
                          direct_output=req.get("direct_output") is True)
    prefix = req.get("response_prefix") or ""
    if not isinstance(prefix,str) or len(prefix)>100 or "\n" in prefix:
        raise ValueError("모델 출력 시작 형식이 잘못되었습니다")
    if prefix and (json_mode or req.get("direct_output") is not True):
        raise ValueError("출력 시작 형식은 직접 응답 요청에서만 사용할 수 있습니다")
    if json_mode:
        prefix = "{"          # 응답을 JSON 객체로 시작하게 한다(작은 모델의 형식 이탈 방지)
    prompt += prefix
    stop_line = req.get("stop_line") or ""
    if not isinstance(stop_line,str) or len(stop_line)>32 or (stop_line and not re.fullmatch(r"[A-Z_]+",stop_line)):
        raise ValueError("모델 출력 종료 행이 잘못되었습니다")
    if stop_line and (json_mode or req.get("direct_output") is not True):
        raise ValueError("종료 행은 직접 응답 요청에서만 사용할 수 있습니다")
    stop_pattern = re.compile(r"(?m)^[ \t]*"+re.escape(stop_line)+r"(?=[ \t]*(?:$|\r?\n|<channel\|>|<\|))") if stop_line else None
    sampler = make_sampler(temp=float(req.get("temperature") or 0.0))
    max_tokens = max(16, min(int(req.get("max_tokens") or 1200), 8192))
    tracker = _JsonEnd(depth=1) if json_mode else None
    parts: list[str] = [prefix]
    last = None
    first_token_ms = None
    emitted_tokens = 0
    for resp in stream_generate(model, tokenizer, prompt, max_tokens=max_tokens, sampler=sampler):
        last = resp
        generated = int(getattr(resp, "generation_tokens", 0) or 0)
        if first_token_ms is None:
            first_token_ms = int((time.perf_counter() - t0) * 1000)
            if progress:
                progress({"event": "first_token", "first_token_ms": first_token_ms,
                          "gen_tokens": generated, "prompt_tokens": getattr(resp, "prompt_tokens", 0)})
        elif progress and generated >= emitted_tokens + 32:
            progress({"event": "generation_progress", "gen_tokens": generated,
                      "elapsed_ms": int((time.perf_counter() - t0) * 1000)})
            emitted_tokens = generated
        seg = resp.text or ""
        if tracker is not None:
            end = tracker.feed(seg)
            if end is not None:
                parts.append(seg[:end])
                break
        if stop_pattern is not None:
            candidate = "".join(parts)+seg
            match = stop_pattern.search(candidate)
            if match:
                parts = [candidate[:match.end()]]
                break
        parts.append(seg)
    text = "".join(parts)
    return {
        "text": text,
        "prompt_tokens": getattr(last, "prompt_tokens", 0) if last else 0,
        "gen_tokens": getattr(last, "generation_tokens", 0) if last else 0,
        "gen_tps": round(float(getattr(last, "generation_tps", 0.0) or 0.0), 1) if last else 0.0,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "first_token_ms": first_token_ms,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="국감 라이브 로컬 LLM 워커")
    ap.add_argument("--model", required=True)
    args = ap.parse_args(argv)
    out = _protocol_stream()
    t0 = time.perf_counter()
    try:
        from mlx_lm import load

        model, tokenizer = load(args.model)
    except Exception as e:  # 모델 로딩 실패는 부모에게 알리고 종료
        _send(out, {"event": "fatal", "error": f"모델 로딩 실패: {type(e).__name__}: {e}"})
        return 2
    _send(out, {"event": "ready", "model": args.model, "load_ms": int((time.perf_counter() - t0) * 1000)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _send(out, {"id": None, "error": "요청이 JSON 이 아닙니다"})
            continue
        rid = req.get("id")
        if req.get("op") == "ping":
            _send(out, {"id": rid, "pong": True, "model": args.model})
            continue
        if req.get("op") == "shutdown":
            break
        try:
            progress = (lambda event: _send(out, {"id": rid, **event})) if req.get("progress") else None
            _send(out, {"id": rid, **handle(model, tokenizer, req, progress)})
        except Exception as e:
            _send(out, {"id": rid, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
