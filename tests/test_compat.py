"""Tests for cross-version asyncio behavior."""

import asyncio

import pytest

from swarmbus._compat import asyncio_timeout


@pytest.mark.asyncio
async def test_asyncio_timeout_preserves_external_cancellation():
    entered = asyncio.Event()

    async def wait_forever():
        async with asyncio_timeout(60):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_asyncio_timeout_translates_its_own_deadline():
    with pytest.raises(asyncio.TimeoutError):
        async with asyncio_timeout(0):
            await asyncio.Event().wait()
