from __future__ import annotations

import asyncio
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
        logger.info(f"{self.name}: start")
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name=self.name)

    async def stop(self) -> None:
        logger.info(f"{self.name}: stop requested")
        self._stopping.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                self._task.cancel()

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.on_tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"{self.name}: tick error: {e}")
            # базовый sleep + джиттер
            try:
                base = self.interval_seconds
                if base > 0:
                    await asyncio.sleep(base)
                    if self.jitter_seconds > 0:
                        await asyncio.sleep(min(self.jitter_seconds, 1))
            except Exception:
                pass
