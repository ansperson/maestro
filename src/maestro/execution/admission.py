"""Bounded local admission and graceful active-work cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import TracebackType

from maestro.errors import ServerBusyError


class AdmissionController:
    """Bound active workers and queued waiters without unbounded accumulation."""

    def __init__(self, max_concurrency: int, max_queue_size: int) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_queue_size = max_queue_size
        self._state_lock = asyncio.Lock()
        self._waiting = 0
        self._closing = False
        self._active: set[asyncio.Task[object]] = set()
        self._waiters: set[asyncio.Task[object]] = set()

    @property
    def waiting(self) -> int:
        """Return the current queue size for tests and observability."""

        return self._waiting

    @property
    def active(self) -> int:
        """Return the current active worker count."""

        return len(self._active)

    @asynccontextmanager
    async def slot(self) -> AsyncGenerator[None]:
        """Admit one task, queue it within bounds, and always release capacity."""

        queued = False
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("admission requires an asyncio task")
        async with self._state_lock:
            if self._closing:
                raise ServerBusyError("The verifier is shutting down.")
            if self._semaphore.locked():
                if self._waiting >= self._max_queue_size:
                    raise ServerBusyError
                self._waiting += 1
                self._waiters.add(task)
                queued = True
        try:
            await self._semaphore.acquire()
        except BaseException:
            if queued:
                async with self._state_lock:
                    self._waiting -= 1
                    self._waiters.discard(task)
            raise
        async with self._state_lock:
            if queued:
                self._waiting -= 1
                self._waiters.discard(task)
            if self._closing:
                self._semaphore.release()
                raise ServerBusyError("The verifier is shutting down.")
            self._active.add(task)
        try:
            yield
        finally:
            async with self._state_lock:
                self._active.discard(task)
                self._semaphore.release()

    async def shutdown(self) -> None:
        """Stop admission, cancel active work, and wait for cleanup to finish."""

        async with self._state_lock:
            self._closing = True
            current = asyncio.current_task()
            tasks = [task for task in self._active | self._waiters if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> AdmissionController:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.shutdown()
