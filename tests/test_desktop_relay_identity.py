"""소유 부모 파이프의 중계 식별자 RPC를 외부 자원 없이 검증한다."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from gukgam import desktop_runtime as desktop
from gukgam.llm.providers import LLMError


REQUEST_ID = "1" * 32
NEXT_REQUEST_ID = "2" * 32
FIRST_CONNECTION = "a" * 32
NEXT_CONNECTION = "b" * 32


class Output:
    def __init__(self, *, fail_once=False):
        self.messages = []
        self.calls = 0
        self.fail_once = fail_once

    async def emit(self, message):
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise LLMError("합성 출력 실패")
        self.messages.append(message)


def fake_app(cloud=None):
    return SimpleNamespace(state=SimpleNamespace(rt=SimpleNamespace(cloud=cloud)))


@pytest.mark.parametrize("cloud", [
    None,
    SimpleNamespace(enabled=False, boot_id=FIRST_CONNECTION),
    SimpleNamespace(enabled=1, boot_id=FIRST_CONNECTION),
    SimpleNamespace(enabled=True),
    *[SimpleNamespace(enabled=True, boot_id=value) for value in (
        None, 7, "", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 31 + "\n",
    )],
])
def test_current_connection_requires_enabled_cloud_and_exact_lower_hex(cloud):
    assert desktop._current_relay_connection_id(fake_app(cloud)) is None


@pytest.mark.parametrize("app", [
    SimpleNamespace(), SimpleNamespace(state=SimpleNamespace()),
    SimpleNamespace(state=SimpleNamespace(rt=None)),
])
def test_missing_runtime_is_unavailable(app):
    assert desktop._current_relay_connection_id(app) is None


def test_current_connection_rechecks_replaced_cloud_and_ignores_unrelated_fields():
    app = fake_app(SimpleNamespace(enabled=True, boot_id=FIRST_CONNECTION,
                                   token="synthetic-not-returned"))
    assert desktop._current_relay_connection_id(app) == FIRST_CONNECTION
    app.state.rt.cloud = SimpleNamespace(enabled=True, boot_id=NEXT_CONNECTION)
    assert desktop._current_relay_connection_id(app) == NEXT_CONNECTION
    app.state.rt.cloud = None
    assert desktop._current_relay_connection_id(app) is None


async def test_reply_returns_current_value_or_null_with_only_protocol_fields():
    app = fake_app(SimpleNamespace(enabled=True, boot_id=FIRST_CONNECTION))
    output, bridge = Output(), SimpleNamespace(closed=False)
    await desktop._reply_relay_connection(app, bridge, output, REQUEST_ID)
    app.state.rt.cloud = SimpleNamespace(enabled=False, boot_id=FIRST_CONNECTION)
    await desktop._reply_relay_connection(app, bridge, output, NEXT_REQUEST_ID)
    assert output.messages == [
        {"type": "relay.connection.result", "id": REQUEST_ID,
         "connection_id": FIRST_CONNECTION},
        {"type": "relay.connection.result", "id": NEXT_REQUEST_ID,
         "connection_id": None},
    ]


@pytest.mark.parametrize("request_id", [None, 7, "", "1" * 31, "1" * 33, "A" * 32, "g" * 32])
async def test_reply_rejects_invalid_request_id_without_output(request_id):
    output = Output()
    await desktop._reply_relay_connection(fake_app(), SimpleNamespace(closed=False),
                                         output, request_id)
    assert output.calls == 0 and output.messages == []


async def test_reply_does_not_read_current_cloud_after_bridge_closed():
    class State:
        @property
        def rt(self):
            raise AssertionError("닫힌 브리지에서 런타임을 읽으면 안 됩니다")

    output = Output()
    await desktop._reply_relay_connection(SimpleNamespace(state=State()),
                                         SimpleNamespace(closed=True), output, REQUEST_ID)
    assert output.calls == 0


@pytest.fixture
def engine_harness(monkeypatch):
    """앱·DB 생성과 설정/환경 읽기, 포트·스레드 시작을 모두 대체한다."""
    app = fake_app(SimpleNamespace(enabled=True, boot_id=FIRST_CONNECTION))
    initial, config, stream = object(), object(), object()
    bridges, servers, readers = [], [], []
    scenario = None

    class Socket:
        closed = False

        def close(self):
            self.closed = True

    sock = Socket()
    real_bridge = desktop.DesktopBridge

    class Bridge(real_bridge):
        def __init__(self, output):
            super().__init__(output)
            self.received = []
            bridges.append(self)

        def receive(self, message):
            self.received.append(message)
            super().receive(message)

    def configure(value, bridge):
        assert value is initial and bridge is bridges[0]
        return config

    module = ModuleType("gukgam.web.app")

    def create_app(value):
        assert value is config
        return app

    module.create_app = create_app
    monkeypatch.setitem(sys.modules, "gukgam.web.app", module)
    monkeypatch.setattr(desktop, "DesktopBridge", Bridge)
    monkeypatch.setattr(desktop, "engine_config", configure)
    monkeypatch.setattr(desktop, "bind_socket", lambda: sock)

    def reader(value, loop, dispatch):
        assert value is stream and loop is asyncio.get_running_loop()
        readers.append(dispatch)

    monkeypatch.setattr(desktop, "start_input_reader", reader)

    def server_config(value, **kwargs):
        assert value is app and kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8765
        return config

    monkeypatch.setattr(desktop.uvicorn, "Config", server_config)

    class Server:
        def __init__(self, value, output):
            assert value is config
            self.output = output
            self.started = False
            self.ready_emitted = True
            self.should_exit = False
            servers.append(self)

        async def serve(self, sockets):
            assert sockets == [sock] and len(readers) == 1
            self.started = True
            await scenario(self, readers[0], bridges[0], app)

    monkeypatch.setattr(desktop, "DesktopServer", Server)

    async def run(action, output=None):
        nonlocal scenario
        scenario = action
        output = output if output is not None else Output()
        result = await asyncio.wait_for(desktop.run_engine(initial, input_stream=stream,
                                                         output=output), 1)
        assert result == 0 and sock.closed and bridges[0].closed
        assert len(servers) == len(bridges) == len(readers) == 1
        return output

    return run


async def test_dispatch_accepts_only_exact_owned_request_fields(engine_harness):
    rejected = [
        {"type": "relay.connection.request"},
        {"type": "relay.connection.request", "id": None},
        {"type": "relay.connection.request", "id": 7},
        {"type": "relay.connection.request", "id": "A" * 32},
        {"type": "relay.connection.request", "id": "1" * 31},
        {"type": "relay.connection.request", "id": "1" * 33},
        {"type": "relay.connection.request", "id": REQUEST_ID,
         "connection_id": NEXT_CONNECTION},
        {"type": "relay.connection.request", "id": REQUEST_ID,
         "source": "foreign-app"},
    ]
    unrelated = {"type": "synthetic.other", "id": NEXT_REQUEST_ID}

    async def scenario(server, dispatch, bridge, app):
        for message in rejected:
            dispatch(message)
        dispatch(unrelated)
        await asyncio.sleep(0)
        assert server.output.messages == []
        assert bridge.received == [unrelated]
        assert not server.should_exit and not bridge.closed
        dispatch({"type": "relay.connection.request", "id": REQUEST_ID})
        await asyncio.sleep(0)
        assert server.output.messages == [{"type": "relay.connection.result",
                                          "id": REQUEST_ID,
                                          "connection_id": FIRST_CONNECTION}]
        assert bridge.received == [unrelated]
        dispatch({"type": "shutdown"})
        assert server.should_exit and bridge.closed

    await engine_harness(scenario)


async def test_emit_failure_keeps_own_server_alive_and_next_request_rechecks_cloud(engine_harness):
    output = Output(fail_once=True)

    async def scenario(server, dispatch, bridge, app):
        dispatch({"type": "relay.connection.request", "id": REQUEST_ID})
        await asyncio.sleep(0)
        assert output.calls == 1 and output.messages == []
        assert not server.should_exit and not bridge.closed
        app.state.rt.cloud = SimpleNamespace(enabled=True, boot_id=NEXT_CONNECTION)
        dispatch({"type": "relay.connection.request", "id": NEXT_REQUEST_ID})
        await asyncio.sleep(0)
        assert output.messages == [{"type": "relay.connection.result",
                                    "id": NEXT_REQUEST_ID,
                                    "connection_id": NEXT_CONNECTION}]
        assert not server.should_exit and not bridge.closed
        dispatch({"type": "shutdown"})

    await engine_harness(scenario, output)


async def test_dispatch_after_shutdown_emits_no_connection_reply(engine_harness):
    async def scenario(server, dispatch, bridge, app):
        dispatch({"type": "shutdown"})
        dispatch({"type": "relay.connection.request", "id": REQUEST_ID})
        await asyncio.sleep(0)
        assert server.should_exit and bridge.closed
        assert server.output.calls == 0

    await engine_harness(scenario)


async def test_engine_finally_cancels_and_awaits_only_owned_connection_tasks(engine_harness, monkeypatch):
    entered, cancelled = [], []
    blocker = asyncio.Event()

    async def reply(app, bridge, output, request_id):
        entered.append(request_id)
        try:
            await blocker.wait()
        finally:
            cancelled.append(request_id)

    monkeypatch.setattr(desktop, "_reply_relay_connection", reply)

    async def scenario(server, dispatch, bridge, app):
        dispatch({"type": "relay.connection.request", "id": REQUEST_ID})
        dispatch({"type": "relay.connection.request", "id": NEXT_REQUEST_ID})
        await asyncio.sleep(0)
        assert entered == [REQUEST_ID, NEXT_REQUEST_ID]
        assert cancelled == [] and not server.should_exit

    await engine_harness(scenario)
    assert sorted(cancelled) == sorted(entered)
