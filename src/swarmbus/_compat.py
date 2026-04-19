"""Python version compatibility shims."""
from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from asyncio import timeout as asyncio_timeout
else:
    import asyncio
    from contextlib import asynccontextmanager

    @asynccontextmanager  # type: ignore[misc]
    async def asyncio_timeout(delay: float):
        """Minimal asyncio.timeout backport for Python 3.10."""
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None, "asyncio_timeout must run inside a Task"
        handle = loop.call_later(delay, task.cancel)
        try:
            yield
        except asyncio.CancelledError as exc:
            raise asyncio.TimeoutError() from exc
        finally:
            handle.cancel()


__all__ = ["asyncio_timeout"]
