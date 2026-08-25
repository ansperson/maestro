from __future__ import annotations

import asyncio

import pytest

from maestro.errors import ServerBusyError
from maestro.execution import AdmissionController


@pytest.mark.asyncio
async def test_admission_bounds_active_and_queue() -> None:
    admission = AdmissionController(max_concurrency=1, max_queue_size=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def occupy() -> None:
        async with admission.slot():
            entered.set()
            await release.wait()

    first = asyncio.create_task(occupy())
    await entered.wait()
    second = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    assert admission.active == 1
    assert admission.waiting == 1
    with pytest.raises(ServerBusyError):
        async with admission.slot():
            pytest.fail("a saturated admission controller admitted extra work")
    release.set()
    await asyncio.gather(first, second)
    assert admission.active == 0
    assert admission.waiting == 0


@pytest.mark.asyncio
async def test_waiting_cancellation_releases_queue_count() -> None:
    admission = AdmissionController(max_concurrency=1, max_queue_size=1)
    release = asyncio.Event()

    async def occupy() -> None:
        async with admission.slot():
            await release.wait()

    first = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert admission.waiting == 0
    release.set()
    await first


@pytest.mark.asyncio
async def test_shutdown_cancels_active_and_waiting_work() -> None:
    admission = AdmissionController(max_concurrency=1, max_queue_size=1)
    blocker = asyncio.Event()

    async def occupy() -> None:
        async with admission.slot():
            await blocker.wait()

    active = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    waiting = asyncio.create_task(occupy())
    await asyncio.sleep(0)
    await admission.shutdown()
    assert active.cancelled()
    assert waiting.cancelled()
    with pytest.raises(ServerBusyError, match="shutting down"):
        async with admission.slot():
            pytest.fail("shutdown admission accepted work")
