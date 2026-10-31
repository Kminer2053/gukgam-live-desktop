"""Julia-1 ONNX CPU 작업자. 모델·토큰화·추론은 이 프로세스 안에만 둔다.

직렬화는 Apache-2.0인 SupersonicLabs Julia-1의 julia/data.py(sequence,
Collator), julia/typed.py와 동일한 마커·배열 계약을 NumPy로 옮겼다.
원본 판본 a85b127321d580d65176c89ced8273f305745d85.
공식 CPU 패리티: Julia-1-ONNX/parity.py, parity-cpu.json.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any

from .decision import DecisionError, validate_questions
from .julia import (JULIA_FILES, JULIA_MODEL, JULIA_RUNTIME, MAX_PIPE_BYTES,
                    _verified_file, julia_options, julia_provenance, protocol_json)

ROLE_CRITERIA = {
    "member": "현재 질의 의원이 직접 한 질문이나 의견",
    "answer": "피감기관 관계자가 직접 한 답변이나 설명",
    "chair": "위원장이 직접 한 진행, 호명, 발언시간 안내",
    "other": "현재 질의 의원·피감기관 답변자·진행 위원장 외의 실제 발언자. 끼어든 다른 의원 등",
}
ROLE_INSTRUCTIONS = (
    "target_sentence_id 문장을 실제로 발언한 사람의 역할을 고르세요. "
    "인접 문장과 context의 질의자·위원장·피감기관·명단을 참고하세요. "
    "언급된 이름을 화자로 단정하지 마세요. 인용·전언을 해도 실제 발언자의 역할을 유지하세요."
)


def project_role_rows(state: Any, questions: dict[str, Any]) -> list[dict[str, Any]]:
    """전체 원문을 자르지 않고 대상·앞뒤 한 문장과 블록 메타를 명시적으로 구성한다."""
    validate_questions(state, questions)
    if not isinstance(state, dict) or not isinstance(state.get("context"), dict):
        raise DecisionError("invalid_request", "Julia 역할 판정의 블록 문맥이 없습니다")
    sentences = state.get("sentences")
    if not isinstance(sentences, list) or not sentences or len(sentences) > 512:
        raise DecisionError("invalid_request", "Julia 역할 판정의 문장 목록이 잘못되었습니다")
    known: dict[str, int] = {}
    for index, row in enumerate(sentences):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or not isinstance(row.get("text"), str):
            raise DecisionError("invalid_request", "Julia 입력 문장 ID와 원문을 확인하세요")
        if row["id"] in known:
            raise DecisionError("invalid_request", "Julia 입력 문장 ID가 중복됐습니다")
        known[row["id"]] = index
    target_ids = state.get("target_sentence_ids")
    expected = [name.removeprefix("role_") for name in questions]
    if (not all(name.startswith("role_") for name in questions) or target_ids != expected
            or len(set(expected)) != len(expected) or any(sid not in known for sid in expected)):
        raise DecisionError("invalid_request", "Julia 대상 문장 ID·순서·범위를 확인하세요")
    result = []
    for name, sid in zip(questions, expected):
        question = questions[name]
        if question.get("type") != "choice" or set(question.get("criteria", {})) != set(ROLE_CRITERIA):
            raise DecisionError("invalid_request", "Julia는 네 화자 역할만 판정합니다")
        index = known[sid]
        selected = [{"id": row["id"], "text": row["text"]} for row in
                    sentences[max(0, index - 1):index + 2]]
        result.append({"state": {"context": state["context"], "target_sentence_id": sid,
                                  "sentences": selected}, "question": ROLE_INSTRUCTIONS,
                       "options": [ROLE_CRITERIA[key] for key in question["criteria"]], "type": "choice"})
    return result


def encode_row(tokenizer: Any, row: dict[str, Any], *, max_tokens: int, head_tokens: int = 512) -> dict:
    """공식 strict sequence와 같은 결과. 초과 입력은 자르지 않고 전체 요청을 거부한다."""
    if head_tokens + 4 >= max_tokens:
        raise DecisionError("input_limit", "Julia 질문 예산 뒤에 문맥 공간이 필요합니다")
    kind = row.get("type", "choice")
    options = row.get("options")
    if (kind not in {"choice", "noul", "score"} or not isinstance(row.get("question"), str)
            or not isinstance(row.get("state"), (str, dict, list)) or not isinstance(options, list)
            or not 2 <= len(options) <= 20 or any(not isinstance(x, str) or not x for x in options)
            or kind == "noul" and len(options) != 2):
        raise DecisionError("invalid_request", "Julia 선택 입력이 잘못되었습니다")
    state = row["state"] if isinstance(row["state"], str) else json.dumps(row["state"], ensure_ascii=False, allow_nan=False)
    if any("<mask>" in value for value in [state, row["question"], *options]):
        raise DecisionError("invalid_request", "Julia 입력에 예약된 모델 마커가 있습니다")
    ids_for = lambda value: tokenizer.encode(value, add_special_tokens=False).ids
    head = ids_for(f"{kind} question: {row['question']}")
    option_ids = [ids_for(" " + value) for value in options]
    if any(len(value) > 48 for value in option_ids):
        raise DecisionError("input_limit", "Julia 선택지의 48토큰 한도를 넘었습니다. 입력을 자르지 않았습니다")
    special = {name: tokenizer.token_to_id(name) for name in ("<mask>", "<bos>", "<eos>", "<pad>")}
    if any(value is None for value in special.values()):
        raise DecisionError("invalid_model", "Julia 토크나이저 특수 토큰을 확인하지 못했습니다")
    packed_options = [[special["<mask>"]] + value for value in option_ids]
    budget = head_tokens - sum(map(len, packed_options))
    if budget < 16:
        per_option = max(4, (head_tokens - 16) // len(options))
        if any(len(value) > per_option for value in packed_options):
            raise DecisionError("input_limit", "Julia 질문·선택지 한도를 넘었습니다. 입력을 자르지 않았습니다")
    if len(head) > budget:
        raise DecisionError("input_limit", "Julia 질문·선택지 한도를 넘었습니다. 입력을 자르지 않았습니다")
    ids = [special["<bos>"]] + head + [special["<eos>"]]
    markers = []
    for option in packed_options:
        markers.append(len(ids))
        ids.extend(option)
    ids.append(special["<eos>"])
    state_ids = ids_for(state)
    room = max_tokens - len(ids) - 1
    if room < 1 or len(state_ids) > room:
        raise DecisionError("input_limit", "Julia 문맥 토큰 한도를 넘었습니다. 입력을 자르지 않았습니다")
    return {"ids": ids + state_ids + [special["<eos>"]], "markers": markers,
            "qtype": {"choice": 0, "score": 1, "noul": 2}[kind], "pad": special["<pad>"]}


class OnnxJuliaEngine:
    """공식 마커 입력을 batch=1 CPU 그래프에 전달하고 세션을 재사용한다."""

    def __init__(self, root: Path, *, threads: int = 2, max_tokens: int = 2048):
        options = julia_options({"julia_threads": threads, "julia_max_tokens": max_tokens})
        for name, spec in JULIA_FILES.items():
            if not _verified_file(root / name, spec):
                raise DecisionError("invalid_model", "Julia 공식 모델 파일 해시가 일치하지 않습니다")
        os.environ.update(CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS=str(threads),
                          MKL_NUM_THREADS=str(threads), TOKENIZERS_PARALLELISM="false")
        try:
            import numpy as np
            import onnxruntime as ort
            import tokenizers
        except ImportError:
            raise DecisionError("runtime_unavailable", "설치판의 Julia CPU 도구를 확인하세요") from None
        actual = {"onnxruntime": ort.__version__, "tokenizers": tokenizers.__version__, "numpy": np.__version__}
        if actual != JULIA_RUNTIME:
            raise DecisionError("runtime_unavailable", "Julia CPU 도구의 검증 버전이 일치하지 않습니다")
        self.np = np
        self.tokenizer = tokenizers.Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.max_tokens = options["max_tokens"]
        config = ort.SessionOptions()
        config.intra_op_num_threads = threads
        config.inter_op_num_threads = 1
        config.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        config.enable_cpu_mem_arena = False
        config.log_severity_level = 3
        self.session = ort.InferenceSession(str(root / "model.onnx"), sess_options=config,
                                            providers=["CPUExecutionProvider"])
        if self.session.get_providers() != ["CPUExecutionProvider"]:
            raise DecisionError("runtime_unavailable", "Julia CPU 전용 실행을 확인하지 못했습니다")

    def logits_rows(self, rows: list[dict[str, Any]], *, head_tokens: int = 512) -> list[list[float]]:
        if not 1 <= len(rows) <= 64:
            raise DecisionError("input_limit", "Julia 요청 수 한도를 넘었습니다")
        encoded = [encode_row(self.tokenizer, row, max_tokens=self.max_tokens,
                              head_tokens=min(head_tokens, self.max_tokens - 5)) for row in rows]
        result = []
        np = self.np
        for item in encoded:
            length = ((len(item["ids"]) + 7) // 8) * 8
            count = len(item["markers"])
            ids = np.full((1, length), item["pad"], dtype=np.int64)
            attention = np.zeros((1, length), dtype=np.int64)
            ids[0, :len(item["ids"])] = item["ids"]
            attention[0, :len(item["ids"])] = 1
            feed = {"input_ids": ids, "attention_mask": attention,
                    "marker_pos": np.asarray([item["markers"]], dtype=np.int64),
                    "marker_mask": np.ones((1, count), dtype=np.bool_),
                    "qtype": np.asarray([item["qtype"]], dtype=np.int64)}
            scores = self.session.run(None, feed)[0]
            if scores.shape != (1, count):
                raise DecisionError("invalid_response", "Julia 출력 선택지 수가 일치하지 않습니다")
            values = [float(value) for value in scores[0]]
            if not all(math.isfinite(value) for value in values):
                raise DecisionError("invalid_response", "Julia 출력 점수가 유한하지 않습니다")
            result.append(values)
        return result

    def evaluate(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        rows = project_role_rows(state, questions)
        tick = time.perf_counter()
        logits = self.logits_rows(rows)
        answers = {}
        for (name, question), values in zip(questions.items(), logits):
            weights = [math.exp(value - max(values)) for value in values]
            probabilities = [value / sum(weights) for value in weights]
            keys = list(question["criteria"])
            winner = max(range(len(values)), key=values.__getitem__)
            answers[name] = {"type": "choice", "choice": keys[winner],
                             "probabilities": dict(zip(keys, probabilities)), "confidence": max(probabilities),
                             "confidence_kind": "uncalibrated_softmax"}
        return {"model": JULIA_MODEL, "answers": answers,
                "elapsed_ms": round((time.perf_counter() - tick) * 1000, 3)}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description="Julia-1 CPU 역할 판정 작업자")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--load-timeout-seconds", type=float, default=60)
    parser.add_argument("--request-timeout-seconds", type=float, default=20)
    args = parser.parse_args(argv)
    settings = {"julia_threads": args.threads, "julia_max_tokens": args.max_tokens,
                "julia_load_timeout_seconds": args.load_timeout_seconds,
                "julia_request_timeout_seconds": args.request_timeout_seconds}
    # C 확장 출력도 프로토콜과 섞이지 않도록 stdout 파일 기술자 자체를 분리한다.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1, encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr

    def send(value):
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        output.flush()

    try:
        provenance = julia_provenance(settings)
        tick = time.perf_counter()
        with redirect_stdout(sys.stderr):
            engine = OnnxJuliaEngine(args.model_dir, threads=args.threads, max_tokens=args.max_tokens)
        send({"event": "ready", "model": JULIA_MODEL, "provenance": provenance,
              "load_ms": round((time.perf_counter() - tick) * 1000, 3)})
    except Exception as exc:
        send({"event": "fatal", "status": exc.status if isinstance(exc, DecisionError) else "failed",
              "error": "Julia CPU 모델을 준비하지 못했습니다"})
        return 2
    while True:
        line = sys.stdin.buffer.readline(MAX_PIPE_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_PIPE_BYTES:
            send({"event": "fatal", "status": "input_limit", "error": "Julia 입력 한도를 넘었습니다"})
            return 2
        rid = None
        try:
            request = protocol_json(line)
            if not isinstance(request, dict) or not isinstance(request.get("id"), str) or len(request["id"]) > 64:
                raise DecisionError("invalid_request", "Julia 요청 번호가 잘못되었습니다")
            rid = request["id"]
            if request.get("op") == "shutdown":
                return 0
            with redirect_stdout(sys.stderr):
                result = engine.evaluate(request.get("state"), request.get("questions"))
            send({"id": rid, **result, "provenance": provenance})
        except Exception as exc:
            send({"id": rid, "status": exc.status if isinstance(exc, DecisionError) else "failed",
                  "error": "Julia 역할 판정을 완료하지 못했습니다"})


if __name__ == "__main__":
    raise SystemExit(main())
