"""Windows 설치형 앱의 전용 엔진 진입점. 설정 파일·익명 파이프만 사용한다."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
from typing import TextIO
from urllib.parse import urlsplit

import uvicorn

from .config import Config, ROOT
from .desktop_bridge import DesktopBridge, JsonlOutput, MAX_LINE_CHARS
from .llm.providers import LLMError

HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}"


def read_initial_config(path: Path) -> dict[str, str]:
    """필드·크기만 확인하며 오류에 파일 본문·비밀값을 포함하지 않는다."""
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        if path.stat().st_size > 65_536:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=object_pairs)
        if not isinstance(value, dict) or set(value) - {"organization", "model", "relayUrl", "relayToken"}:
            raise ValueError
        result = {}
        for key, limit in (("organization", 120), ("model", 200), ("relayUrl", 2048), ("relayToken", 4096)):
            text = value.get(key, "")
            if not isinstance(text, str) or len(text) > limit or any(ord(c) < 32 for c in text):
                raise ValueError
            result[key] = text.strip()
        if not result["organization"] or bool(result["relayUrl"]) != bool(result["relayToken"]):
            raise ValueError
        if result["relayUrl"]:
            url = urlsplit(result["relayUrl"])
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError
            result["relayUrl"] = result["relayUrl"].rstrip("/")
        return result
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ValueError("앱 초기 설정 파일의 필드와 형식을 확인하세요") from None


def engine_config(initial: dict[str, str], bridge: DesktopBridge) -> Config:
    """운영용 .env를 읽지 않고 Electron이 준 별도 데이터 폴더만 사용한다."""
    data = os.environ.get("GUKGAM_DATA_DIR", "")
    path = Path(data).expanduser()
    if not data or not path.is_absolute() or path.resolve() == (ROOT / "data").resolve():
        raise ValueError("설치형 앱 전용 GUKGAM_DATA_DIR 절대 경로가 필요합니다")
    return Config(host=HOST, port=PORT, data_dir=path.resolve(),
                  operator_passcode="", viewer_passcode="", secret_key="",
                  disable_heavy_engines=False, cloud_url=initial["relayUrl"], agent_token=initial["relayToken"],
                  cloud_mirror_sensitive=False, cloud_read_mode="source", desktop_bridge=bridge,
                  desktop_organization=initial["organization"], desktop_model=initial["model"])


class DesktopServer(uvicorn.Server):
    """바인드와 lifespan이 모두 성공한 뒤 준비 완료를 한 번 보낸다."""

    def __init__(self, config: uvicorn.Config, output: JsonlOutput):
        super().__init__(config)
        self.output = output
        self.ready_emitted = False

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started and not self.should_exit:
            try:
                await self.output.emit({"type": "ready", "url": URL})
                self.ready_emitted = True
            except LLMError:
                self.should_exit = True


def bind_socket() -> socket.socket:
    """지정한 단일 포트만 확보한다. 충돌한 프로세스를 조회·종료하지 않는다."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((HOST, PORT))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def start_input_reader(stream: TextIO, loop: asyncio.AbstractEventLoop, dispatch) -> threading.Thread:
    """Windows 파이프도 읽을 수 있는 제한된 daemon reader. EOF는 정상 종료 요청이다."""
    def deliver(message):
        try:
            loop.call_soon_threadsafe(dispatch, message)
        except RuntimeError:
            pass

    def read():
        try:
            while True:
                line = stream.readline(MAX_LINE_CHARS + 1)
                if not line or len(line) > MAX_LINE_CHARS:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                deliver(message)
        except Exception:
            pass
        deliver({"type": "shutdown"})

    thread = threading.Thread(target=read, name="desktop-jsonl-read", daemon=True)
    thread.start()
    return thread


async def run_engine(initial: dict[str, str], *, input_stream: TextIO, output: JsonlOutput) -> int:
    from .web.app import create_app

    bridge = DesktopBridge(output)
    from .desktop_jev import configure_from_parent as configure_jev, clear as clear_jev
    config = engine_config(initial, bridge)
    # 앱·DB 수명주기를 시작하기 전에 포트를 독점 확보한다. 다른 포트로 우회하지 않는다.
    sock = bind_socket()
    server = None
    try:
        app = create_app(config)
        server = DesktopServer(uvicorn.Config(app, host=HOST, port=PORT, log_config=None,
                                             access_log=False, timeout_graceful_shutdown=5), output)

        def dispatch(message):
            if configure_jev(bridge, message):
                return
            if isinstance(message, dict) and message.get("type") == "shutdown":
                bridge.close()
                server.should_exit = True
            else:
                bridge.receive(message)

        start_input_reader(input_stream, asyncio.get_running_loop(), dispatch)
        await server.serve(sockets=[sock])
        return 0 if server.started and server.ready_emitted else 1
    finally:
        clear_jev(bridge)
        bridge.close()
        sock.close()


def main(argv: list[str] | None = None) -> int:
    # 도움말도 한국어이므로 인자 해석보다 먼저 Windows 파이프 인코딩을 맞춘다.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="strict")
    worker_args = list(sys.argv[1:] if argv is None else argv)
    if worker_args and worker_args[0] == "--nemotron-worker":
        from .stt.nemotron_worker import main as nemotron_main
        return nemotron_main(worker_args[1:])
    if worker_args and worker_args[0] == "--nemotron-diarization-worker":
        from .stt.nemotron_diarization_worker import main as diarization_main
        return diarization_main(worker_args[1:])
    if worker_args and worker_args[0] == "--julia-worker":
        # 같은 설치 EXE의 소유 자식으로만 CPU 모델을 실행한다. 웹 포트를 열지 않는다.
        from .llm.julia_worker import main as julia_main
        return julia_main(worker_args[1:])
    parser = argparse.ArgumentParser(description="국감 설치형 앱 전용 엔진")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", type=Path, help="앱이 작성한 초기 설정 JSON 파일")
    mode.add_argument("--relay-init", action="store_true", help="표준입력으로 받은 새 릴레이 DB의 스키마만 준비")
    args = parser.parse_args(argv)
    protocol_output = JsonlOutput(sys.stdout)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
    try:
        if args.relay_init:
            from .desktop_relay import initialize_database
            initialize_database(sys.stdin)
            print(json.dumps({"type": "relay.initialized", "ok": True}), flush=True)
            return 0
        initial = read_initial_config(args.config)
        with redirect_stdout(sys.stderr):
            return asyncio.run(run_engine(initial, input_stream=sys.stdin, output=protocol_output))
    except (Exception, SystemExit):
        # 토큰·URL·요청 본문이 예외 안에 있어도 외부 프로세스 로그에 복사하지 않는다.
        print("엔진을 시작하지 못했습니다. 앱 전용 설정·데이터 폴더와 8765 포트 사용 여부를 확인하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
