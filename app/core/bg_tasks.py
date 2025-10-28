from __future__ import annotations
import asyncio
from contextlib import suppress
from typing import Set

_tasks: Set[asyncio.Task] = set()

def register(task: asyncio.Task) -> asyncio.Task:
    """Register background task for graceful shutdown."""
    _tasks.add(task)
    def _cleanup(_t: asyncio.Task) -> None:
        with suppress(Exception):
            _tasks.discard(_t)
    task.add_done_callback(_cleanup)
    return task

async def cancel_all() -> None:
    """Cancel all registered background tasks and wait for them to finish."""
    for t in list(_tasks):
        with suppress(Exception):
            t.cancel()
    for t in list(_tasks):
        with suppress(asyncio.CancelledError, Exception):
            await t
    _tasks.clear()



