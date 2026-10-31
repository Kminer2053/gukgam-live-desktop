"""수집 수명주기와 분리된 선택적 중계 연결 관리자."""
from __future__ import annotations

import asyncio
import logging

from . import settings
from .relay_policy import policy_status

log = logging.getLogger(__name__)


class RelayLifecycle:
    def __init__(self, rt, app, *, clock=None, interval_s: float = 1):
        self.rt, self.app, self.clock = rt, app, clock
        self.interval_s = interval_s
        self._policy = settings.get(rt.repo, "relay")
        self._lock = asyncio.Lock()
        self._task = None
        self._closed = False

    def status(self) -> dict:
        configured = bool(self.rt.config.cloud_url and self.rt.config.agent_token)
        return policy_status(self._policy, configured=configured, now=self.clock() if self.clock else None)

    def allowed(self) -> bool:
        return not self._closed and self.status()["active"]

    async def reconcile(self, *, reload: bool = False) -> None:
        async with self._lock:
            if reload:
                self._policy = settings.get(self.rt.repo, "relay")
            if self.allowed():
                if self.rt.cloud is None:
                    from .cloudsync import CloudSync
                    cloud = CloudSync(self.rt, self.app,
                                      connection_allowed=lambda: self.allowed() and self.rt.cloud is cloud)
                    self.rt.cloud = cloud
                    await cloud.start()
            elif self.rt.cloud is not None:
                cloud, self.rt.cloud = self.rt.cloud, None
                # 기간 밖에서는 종료용 전송도 하지 않는다. 수집·AI 서비스에는 손대지 않는다.
                await cloud.stop(flush_timeout=0)

    async def start(self) -> None:
        await self.reconcile()
        if self._task is None and self.status()["configured"]:
            self._task = asyncio.create_task(self._watch(), name="relay-period-watch")

    async def _watch(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.interval_s)
            try:
                await self.reconcile()
            except Exception as exc:
                log.warning("중계 연결 상태 적용 실패(%s)", type(exc).__name__)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        async with self._lock:
            if self.rt.cloud is not None:
                cloud, self.rt.cloud = self.rt.cloud, None
                await cloud.stop(flush_timeout=0)
