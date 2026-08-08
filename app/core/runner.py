from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Awaitable, Callable, Optional

from loguru import logger


class PollingLoop:
    """Универсальный цикл опроса: start/stop, sleep, jitter, логирование ошибок.

    Использование:
            loop = PollingLoop(interval_seconds=30, on_tick=handler, name="grab_poller")
            await loop.start(); await loop.stop()
    """

    def __init__(
        self,
        *,
        interval_seconds: int,
        on_tick: Callable[[], Awaitable[None]],
        name: Optional[str] = None,
        jitter_seconds: int = 0,
    ):
        self.interval_seconds = max(0, int(interval_seconds))
        self.on_tick = on_tick
        self.name = name or "polling-loop"
        self.jitter_seconds = max(0, int(jitter_seconds))
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        logger.info("{}: start", self.name)
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name=self.name)

    async def stop(self) -> None:
        logger.info("{}: stop requested", self.name)
        self._stopping.set()
        if not self._task:
            return

        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning("{}: stop timed out; cancelling task", self.name)
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("{}: worker task failed during shutdown", self.name)

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.on_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("{}: tick error", self.name)

            try:
                if self.interval_seconds > 0:
                    await asyncio.sleep(self.interval_seconds)
                    if self.jitter_seconds > 0:
                        await asyncio.sleep(min(self.jitter_seconds, 1))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("{}: sleep error", self.name)
