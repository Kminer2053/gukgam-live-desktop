"""클라우드 명령의 로컬 실행과 독립 레인·결과 재전송."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from collections import deque

import httpx

from . import serialize
from .cloud import policy
from .db import now_ms
from .web.auth import COOKIE_NAME, User

log = logging.getLogger(__name__)
MAX_RESULT_BYTES = 3_000_000
MAX_REQUEST_BYTES = 1_000_000


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _error(status: int, detail: str) -> dict:
    return {"status": status, "headers": {"content-type": "application/json"},
            "body_b64": base64.b64encode(_json({"detail": detail})).decode("ascii")}


class CommandRunner:
    """fast는 순차, slow는 한 개씩 실행하며 결과 업로드 실패는 실행과 분리한다."""

    def __init__(self, sync):
        self.sync = sync
        self.local_transport = httpx.ASGITransport(app=sync.app)
        self._recent: deque[int] = deque()
        self._seen: set[int] = set()
        self._executing: set[int] = set()
        self._working: set[int] = set()
        self._pending_mirror: dict[int, tuple[dict, dict]] = {}
        self._results: dict[int, dict] = {}
        self._result_commands: dict[int, dict] = {}
        self._queues = {"fast": asyncio.Queue(), "slow": asyncio.Queue()}
        self._tasks: list[asyncio.Task] = []
        self._poll_task = None
        self._running = False

    def _remember(self, id: int) -> None:
        self._seen.add(id)
        self._recent.append(id)
        if len(self._recent) > 1000:
            self._seen.discard(self._recent.popleft())

    async def execute_command(self, cmd: dict) -> dict | None:
        """기한·허용목록을 재검사하고 같은 ID는 다시 실행하지 않는다."""
        if not self.sync.enabled or self.sync.app is None:
            return None
        deadline = cmd.get("deadline_at")
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= now_ms():
            return _error(410, "이 명령의 기한이 지났습니다. 다시 실행하지 않습니다")
        try:
            rule = policy.match(cmd.get("method", ""), cmd.get("path", ""), cmd.get("query", ""))
        except (TypeError, AttributeError):
            rule = None
        if rule is None or not rule.allows(cmd.get("role")):
            return _error(403, "이 기능은 클라우드에서 실행할 수 없습니다")
        cmd = {**cmd, "method": rule.method, "lane": rule.lane}
        try:
            body = base64.b64decode(cmd.get("body_b64", ""), validate=True)
        except (ValueError, TypeError, binascii.Error):
            return _error(403, "명령 본문을 확인할 수 없습니다")
        if len(body) > MAX_REQUEST_BYTES:
            return _error(413, "명령 본문이 너무 큽니다")
        violation = policy.check_body(rule, body)
        if violation:
            return _error(403, violation)
        id = cmd.get("id")
        if not isinstance(id, int) or isinstance(id, bool):
            return _error(403, "명령 식별자를 확인할 수 없습니다")
        if id in self._seen or id in self._executing or id in self._pending_mirror or id in self._results:
            return None
        self._remember(id)
        self._executing.add(id)
        try:
            try:
                result = await self._local(cmd, body)
            except Exception as exc:
                log.error("클라우드 명령 실행 실패: %s", type(exc).__name__)
                result = _error(502, "수집 PC에서 명령을 처리하지 못했습니다")
            # 동기화 실패 때도 실행 결과는 남겨 두고 문서 전송만 재시도한다.
            self._pending_mirror[id] = (cmd, result)
            return await self._sync_result(id)
        finally:
            self._executing.discard(id)

    async def _sync_result(self, id: int) -> dict | None:
        cmd, result = self._pending_mirror[id]
        if self.sync.read_mode == "source":
            # 원본 응답은 알림 전송·백필 성공과 독립적으로 즉시 돌려준다.
            self._pending_mirror.pop(id, None)
            return result
        try:
            if not await self.sync.flush():
                self.sync.last_error = "명령 후 동기화 실패"
                return None
            if not await self._recover_mirror() or not await self._followup(cmd, result) or not await self._recover_mirror():
                self.sync.last_error = "명령 후 문서 동기화 실패"
                return None
        except Exception as exc:
            log.error("클라우드 명령 동기화 실패: %s", type(exc).__name__)
            self.sync.last_error = "명령 후 문서 동기화 실패"
            return None
        self._pending_mirror.pop(id, None)
        return result

    async def _recover_mirror(self) -> bool:
        """큐 유실로 빠진 문서도 결과 응답 전 백필로 복구한다."""
        if self.sync.read_mode == "source":
            return True
        while self.sync.needs_backfill:
            target = None if "*" in self.sync.needs_backfill else next(iter(self.sync.needs_backfill))
            if not await self.sync.backfill(target):
                return False
        return True

    async def _local(self, cmd: dict, body: bytes) -> dict:
        auth = self.sync.app.state.auth
        actor = cmd.get("actor", "")
        if not isinstance(actor, str):
            return _error(403, "명령 요청자를 확인할 수 없습니다")
        cookie = auth.issue(User(cmd["role"], actor))
        headers = {"Cookie": COOKIE_NAME + "=" + cookie, "X-Requested-With": "gukgam",
                   "Origin": self.sync.config.cloud_url}
        incoming = cmd.get("headers", {})
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                if isinstance(key, str) and key.lower() == "content-type" and isinstance(value, str):
                    headers["Content-Type"] = value
        path = cmd["path"] + ("?" + cmd["query"] if cmd.get("query") else "")
        timeout = self.sync.command_timeout_s if cmd["lane"] == "slow" else None
        if self.sync.read_mode == "source":
            timeout = min(self.sync.command_timeout_s, max(0, (cmd["deadline_at"] - now_ms()) / 1000))
        async with asyncio.timeout(timeout), httpx.AsyncClient(transport=self.local_transport,
                base_url=self.sync.config.cloud_url, timeout=None, follow_redirects=False) as client:
            async with client.stream(cmd["method"], path, headers=headers, content=body) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESULT_BYTES:
                        return _error(413, "파일이 커서 클라우드로 내려받을 수 없습니다 — 수집 PC 화면에서 내려받으세요")
                    content.extend(chunk)
                data = bytes(content)
                if cmd["method"] == "GET" and cmd["path"] == "/api/settings" and response.status_code == 200:
                    original = json.loads(data)
                    public = {key: original[key] for key in ("org", "watch_keywords", "departments") if key in original}
                    data = _json(public)
                allowed = {key: value for key in ("content-type", "content-disposition")
                           if (value := response.headers.get(key)) is not None}
                return {"status": response.status_code, "headers": allowed,
                        "body_b64": base64.b64encode(data).decode("ascii")}

    async def _followup(self, cmd: dict, result: dict) -> bool:
        if self.sync.read_mode == "source":
            return True
        if not 200 <= result["status"] < 300:
            return True
        method, path = cmd["method"], cmd["path"]
        repo, sync = self.sync.rt.repo, self.sync
        ops = []
        sid = None
        if method == "POST" and path == "/api/sessions":
            output = json.loads(base64.b64decode(result["body_b64"]))
            sid = output.get("id")
        elif method == "PATCH" and (match := re.fullmatch(r"/api/sessions/([^/]+)", path)):
            sid = match[1]
        if sid:
            session = repo.get_session(sid)
            if session and session["source_type"] != "replay":
                data = serialize.session(session, repo.session_stats(sid))
                analysis = getattr(sync.rt, "analysis", None)
                try:
                    data["analysis"] = analysis.status() if analysis is not None else {}
                except Exception:
                    data["analysis"] = {}
                ops.extend([{"op": "doc", "kind": "session", "id": sid, "sid": sid,
                             "ts": session["created_at"], "data": data},
                            {"op": "doc", "kind": "roster", "id": sid, "sid": sid,
                             "ts": now_ms(), "data": sync._roster(sid)}])
        if method == "PUT" and (match := re.fullmatch(r"/api/sessions/([^/]+)/roster", path)):
            sid = match[1]
            session = repo.get_session(sid)
            if session and session["source_type"] != "replay":
                ops.append({"op": "doc", "kind": "roster", "id": sid, "sid": sid,
                            "ts": now_ms(), "data": sync._roster(sid)})
        if method == "POST" and (match := re.fullmatch(r"/api/sessions/([^/]+)/alerts/ack", path)):
            sid = match[1]
            session = repo.get_session(sid)
            if session and session["source_type"] != "replay":
                docs = [{"id": str(row["id"]), "ts": row.get("ts_start") or row.get("created_at") or 0,
                         "data": serialize.alert(row)} for row in repo.list_alerts(sid, limit=1_000_000)]
                ops.extend(sync._replace_chunks("alert", sid, docs, sync._gen()))
        if method == "PUT" and path.startswith("/api/settings/"):
            ops.append({"op": "doc", "kind": "settings_public", "id": "global", "sid": None,
                        "ts": now_ms(), "data": sync._public_settings()})
        for op in ops:
            sync._enqueue(op)
        if ops:
            return await sync.flush()
        return True

    async def poll_commands(self, wait: float = 20) -> list[dict]:
        if not self.sync.enabled or self.sync.app is None or self.sync.mode_update() != "active":
            return []
        if self.sync._client is None:
            self.sync._client = httpx.AsyncClient(transport=self.sync._transport, timeout=25, follow_redirects=False)
        try:
            response = await self.sync._client.get(self.sync.config.cloud_url.rstrip("/") + "/agent/commands",
                params={"wait": min(20, max(0, wait))}, timeout=25,
                headers={"Authorization": "Bearer " + self.sync.config.agent_token})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("commands"), list):
                raise ValueError
            self.sync._online, self.sync.last_error = True, ""
            self.sync._wanted = bool(data.get("wanted", self.sync._wanted))
            self.sync.mode_update()
            return [cmd for cmd in data["commands"] if isinstance(cmd, dict)]
        except Exception:
            self.sync._online, self.sync.last_error = False, "클라우드 명령 연결 실패"
            return []

    def _workers(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [asyncio.create_task(self._worker(lane), name="cloud-command-" + lane)
                       for lane in ("fast", "slow")]
        # 같은 실행기를 다시 켜도 보관 결과를 올릴 뿐 로컬 명령을 다시 실행하지 않는다.
        for cmd, _ in self._pending_mirror.values():
            self._queue_command(cmd)
        for cmd in self._result_commands.values():
            self._queue_command(cmd)

    def _queue_command(self, cmd: dict) -> None:
        try:
            rule = policy.match(cmd.get("method", ""), cmd.get("path", ""), cmd.get("query", ""))
        except (TypeError, AttributeError):
            rule = None
        self._queues[rule.lane if rule else "fast"].put_nowait(cmd)

    async def dispatch_commands(self, commands: list[dict]) -> None:
        if not self.sync.enabled or self.sync.app is None:
            return
        self._workers()
        for cmd in commands:
            self._queue_command(cmd)

    async def _worker(self, lane: str) -> None:
        queue = self._queues[lane]
        while self._running:
            cmd = await queue.get()
            id = cmd.get("id")
            if id in self._working:
                queue.task_done()
                continue
            self._working.add(id)
            try:
                result = self._results.get(id)
                if result is None and id not in self._pending_mirror:
                    result = await self.execute_command(cmd)
                delay = self.sync.command_result_retry_s
                while result is None and id in self._pending_mirror and self._running:
                    await asyncio.sleep(delay)
                    result = await self._sync_result(id)
                    delay = min(30, max(self.sync.command_result_retry_s, delay * 2))
                if result is None:
                    continue
                self._results[id] = result
                self._result_commands[id] = cmd
                delay = self.sync.command_result_retry_s
                while self._running:
                    if await self._post_result(id, result):
                        self._results.pop(id, None)
                        self._result_commands.pop(id, None)
                        break
                    await asyncio.sleep(delay)
                    delay = min(30, max(self.sync.command_result_retry_s, delay * 2))
            except Exception as exc:
                log.error("클라우드 명령 작업 실패: %s", type(exc).__name__)
            finally:
                self._working.discard(id)
                queue.task_done()

    async def _post_result(self, id: int, result: dict) -> bool:
        """응답 유실 뒤 409는 이미 완료·만료된 명령이므로 재전송을 마친다."""
        if not self.sync.enabled:
            return False
        if self.sync._client is None:
            self.sync._client = httpx.AsyncClient(transport=self.sync._transport, timeout=25,
                                                follow_redirects=False)
        try:
            response = await self.sync._client.post(
                self.sync.config.cloud_url.rstrip("/") + f"/agent/commands/{id}/result",
                content=_json(result), headers={"Authorization": "Bearer " + self.sync.config.agent_token,
                                               "Content-Type": "application/json"})
            if response.status_code == 409:
                self.sync._online, self.sync.last_error = True, ""
                return True
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            self.sync._online, self.sync.last_error = True, ""
            self.sync._wanted = bool(data.get("wanted", self.sync._wanted))
            self.sync.mode_update()
            return True
        except Exception:
            self.sync._online, self.sync.last_error = False, "클라우드 결과 전송 실패"
            return False

    async def _poll(self) -> None:
        delay = self.sync.command_poll_retry_s
        while self._running:
            if self.sync.mode_update() != "active":
                await asyncio.sleep(min(1, self.sync.idle_heartbeat_s))
                continue
            rows = await self.poll_commands(self.sync.command_wait_s)
            if rows:
                await self.dispatch_commands(rows)
            if self.sync._online:
                delay = self.sync.command_poll_retry_s
                await asyncio.sleep(0.01)
            else:
                await asyncio.sleep(delay)
                delay = min(30, max(self.sync.command_poll_retry_s, delay * 2))

    def start(self) -> None:
        self._workers()
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll(), name="cloud-command-poll")

    async def stop(self) -> None:
        self._running = False
        tasks = self._tasks + ([self._poll_task] if self._poll_task is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks, self._poll_task = [], None
