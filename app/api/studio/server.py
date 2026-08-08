from __future__ import annotations

import asyncio
from contextlib import suppress

import uvicorn
from loguru import logger

from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig, studio_config


class StudioServer:
    """Lifecycle wrapper for the optional in-process Mini App API server."""

    def __init__(self, config: StudioConfig | None = None):
        self.config = config or studio_config
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("Studio API disabled (STUDIO_ENABLED=false)")
            return
        if self._task is not None and not self._task.done():
            return
        app = create_studio_app(self.config)
        uvicorn_config = uvicorn.Config(
            app,
            host=self.config.host,
            port=self.config.port,
            log_config=None,
            access_log=False,
            loop="asyncio",
        )
        self._server = uvicorn.Server(uvicorn_config)
        self._task = asyncio.create_task(self._server.serve(), name="studio-api")
        await asyncio.sleep(0)
        logger.info(
            "Studio API starting on {}:{} public_url={}",
            self.config.host,
            self.config.port,
            self.config.public_url or "not configured",
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Studio API graceful shutdown timed out; cancelling")
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        finally:
            self._task = None
            self._server = None
