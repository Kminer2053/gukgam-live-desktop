"""자막 루프와 독립된 단일 보정 실행기·읽기 실행기·기존 모델 호출 연결."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock


class StageJob:
    def __init__(self, main_loop):
        self.main_loop = main_loop
        self.cancelled = Event()
        self.worker_loop = None
        self.worker_task = None
        self.bridges = set()
        self._lock = Lock()

    async def bridge(self, coroutine, *, cleanup=False):
        """기존 모델·상태·짧은 저장만 원래 루프에 요청한다."""
        if self.cancelled.is_set() and not cleanup:
            coroutine.close()
            raise asyncio.CancelledError
        future = asyncio.run_coroutine_threadsafe(coroutine, self.main_loop)
        if not cleanup:
            with self._lock:
                self.bridges.add(future)
        try:
            return await asyncio.wrap_future(future)
        finally:
            with self._lock:
                self.bridges.discard(future)

    async def worker_call(self, callback):
        """모델의 비동기 guard도 무거운 검증을 보정 루프로 되돌린다."""
        if self.cancelled.is_set():
            return False
        async def call():
            return callback()
        if asyncio.get_running_loop() is self.worker_loop:
            return callback()
        future = asyncio.run_coroutine_threadsafe(call(), self.worker_loop)
        return await asyncio.wrap_future(future)

    def cancel(self):
        with self._lock:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            for future in tuple(self.bridges):
                future.cancel()
            loop, task = self.worker_loop, self.worker_task
        if loop is not None and task is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)

    def execute(self, factory):
        async def run():
            self.worker_loop = asyncio.get_running_loop()
            self.worker_task = asyncio.current_task()
            if self.cancelled.is_set():
                raise asyncio.CancelledError
            return await factory(self)
        return asyncio.run(run())


class StageExecution:
    def __init__(self):
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gukgam-correction")
        self._readers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gukgam-stage-read")
        self._jobs = {}
        self._closed = False

    async def run(self, factory):
        if self._closed:
            raise asyncio.CancelledError
        loop = asyncio.get_running_loop()
        job = StageJob(loop)
        future = loop.run_in_executor(self._worker, job.execute, factory)
        self._jobs[job] = future
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            job.cancel()
            # 독립 연결의 finally·중단 상태 저장이 끝나기 전에 DB를 닫지 않는다.
            await asyncio.gather(asyncio.shield(future), return_exceptions=True)
            raise
        finally:
            self._jobs.pop(job, None)

    async def read(self, callback):
        if self._closed:
            raise asyncio.CancelledError
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._readers, callback)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        jobs = list(self._jobs.items())
        for job, _ in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*(asyncio.shield(future) for _, future in jobs), return_exceptions=True)
        # 스레드 종료 대기도 수신 루프 밖에서 마친다.
        await asyncio.to_thread(self._worker.shutdown, wait=True, cancel_futures=True)
        await asyncio.to_thread(self._readers.shutdown, wait=True, cancel_futures=True)
