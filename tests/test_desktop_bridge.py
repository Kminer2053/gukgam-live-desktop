"""실계정·네트워크 없이 파이프의 직렬화·실패·취소를 검사한다."""
import asyncio
import io
import json
import threading
from types import SimpleNamespace

import pytest

from gukgam.desktop_bridge import DesktopBridge, JsonlOutput, MAX_LINE_CHARS
from gukgam.llm.providers import DesktopChatGPTProvider, LLMError, LLMRequestStale, make_provider


class Output:
    def __init__(self):
        self.messages = []
        self.entered = asyncio.Event()

    async def emit(self, value, **kwargs):
        self.messages.append(value)
        self.entered.set()


async def test_provider_roundtrip_uses_one_request_and_never_copies_parent_error():
    output = Output()
    bridge = DesktopBridge(output)
    provider = make_provider({"provider": "desktop_chatgpt", "desktop_model": "account-model"},
                             SimpleNamespace(desktop_bridge=bridge))
    assert isinstance(provider, DesktopChatGPTProvider) and provider.available
    task = asyncio.create_task(provider.complete("시스템", "질의", 900, direct_output=True))
    await asyncio.wait_for(output.entered.wait(), 1)
    request = output.messages[0]
    assert set(request) == {"type", "id", "system", "user", "max_tokens"}
    assert request["type"] == "llm.request" and request["max_tokens"] == 900
    bridge.receive({"type": "llm.result", "id": "wrong", "text": "다른 요청"})
    assert not task.done()
    bridge.receive({"type": "llm.result", "id": request["id"], "text": '{"title":"결과"}'})
    assert await task == '{"title":"결과"}' and not bridge._pending
    output.entered.clear()
    task = asyncio.create_task(provider.complete("시스템", "질의"))
    await output.entered.wait()
    bridge.receive({"type": "llm.error", "id": output.messages[-1]["id"], "message": "private-token-never-copy"})
    with pytest.raises(LLMError) as failure:
        await task
    assert "private-token" not in str(failure.value)


@pytest.mark.parametrize("kind", ["cancel", "timeout", "stale"])
async def test_cancel_timeout_and_stale_request_remove_pending_and_discard_late_result(kind):
    output = Output()
    bridge = DesktopBridge(output)
    allowed = True
    task = asyncio.create_task(bridge.request("시스템", "질의", 800, timeout=.03 if kind == "timeout" else 1,
                                             guard=lambda: allowed))
    await output.entered.wait()
    request = output.messages[0]
    if kind == "cancel":
        task.cancel()
        error = asyncio.CancelledError
    elif kind == "stale":
        allowed = False
        error = LLMRequestStale
    else:
        error = LLMError
    with pytest.raises(error):
        await asyncio.wait_for(task, 1)
    assert output.messages[-1] == {"type": "llm.cancel", "id": request["id"]}
    bridge.receive({"type": "llm.result", "id": request["id"], "text": "늦은 응답"})
    assert not bridge._pending


async def test_requests_are_serial_and_shutdown_unblocks_waiters():
    output = Output()
    bridge = DesktopBridge(output)
    first = asyncio.create_task(bridge.request("a", "b", 300))
    await output.entered.wait()
    second = asyncio.create_task(bridge.request("c", "d", 300))
    await asyncio.sleep(0)
    assert len(output.messages) == 1
    bridge.close()
    for task in (first, second):
        with pytest.raises(LLMError, match="종료"):
            await asyncio.wait_for(task, 1)
    assert not bridge._pending


async def test_output_locks_utf8_json_lines_and_bounds_blocked_pipe():
    stream = io.StringIO()
    output = JsonlOutput(stream)
    await asyncio.gather(*(output.emit({"type": "test", "text": f"한글{i}"}) for i in range(20)))
    values = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(values) == 20 and {v["text"] for v in values} == {f"한글{i}" for i in range(20)}
    with pytest.raises(LLMError, match="크기"):
        await output.emit({"text": "x" * MAX_LINE_CHARS})
    release = threading.Event()

    class Blocked:
        def write(self, line):
            release.wait()

        def flush(self):
            pass

    try:
        with pytest.raises(LLMError, match="쓰기 시간"):
            await JsonlOutput(Blocked()).emit({"type": "test"}, timeout=.02)
    finally:
        release.set()


async def test_missing_bridge_and_invalid_timeouts_fail_without_model_or_transport():
    provider = make_provider({"provider": "desktop_chatgpt"})
    assert not provider.available
    with pytest.raises(LLMError, match="준비"):
        await provider.complete("s", "u")
    for timeout in (True, 0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            await DesktopBridge(Output()).request("s", "u", 100, timeout=timeout)
