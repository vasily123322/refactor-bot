from __future__ import annotations

import asyncio
from contextlib import suppress

import uvicorn
from loguru import logger

from app.api.studio.app import create_studio_app
from app.api.studio.config import StudioConfig, studio_config


class StudioServer:
    """Lifecycle wrapper for the optional in-process Mini App API server.

    Readiness means uvicorn.Server.started became true while its serve task
    remained alive. After readiness, unexpected task termination is fatal to the
    parent runtime; only stop() marks termination as intentional.
    """

    def __init__(
        self,
        config: StudioConfig | None = None,
        *,
        startup_timeout_seconds: float = 10.0,
    ):
        self.config = config or studio_config
        self.startup_timeout_seconds = max(0.01, float(startup_timeout_seconds))
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled
            and self._server is not None
            and self._task is not None
            and not self._task.done()
            and getattr(self._server, "started", False)
        )

    async def _serve(self) -> None:
        """Run embedded Uvicorn without allowing its SystemExit to escape the task."""

        server = self._server
        if server is None:
            raise RuntimeError("Studio API server is not initialized")
        try:
            await server.serve()
        except SystemExit as exc:
            raise RuntimeError(
                f"Embedded Uvicorn exited with code {exc.code!r}"
            ) from exc

    async def _raise_startup_failure(self) -> None:
        task = self._task
        self._task = None
        self._server = None
        if task is None:
            raise RuntimeError("Studio API startup task is missing")
        try:
            await task
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise RuntimeError("Studio API failed before readiness") from exc
        raise RuntimeError("Studio API stopped before readiness")

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Studio API disabled (STUDIO_ENABLED=false)")
            return
        if self._task is not None and not self._task.done():
            return

        self._stopping = False
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
        self._task = asyncio.create_task(self._serve(), name="studio-api")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout_seconds
        while not getattr(self._server, "started", False):
            if self._task.done():
                await self._raise_startup_failure()
            if loop.time() >= deadline:
                await self.stop()
                raise TimeoutError(
                    f"Studio API readiness timed out after {self.startup_timeout_seconds:g}s"
                )
            await asyncio.sleep(0.01)

        if self._task.done():
            await self._raise_startup_failure()
        logger.info(
            "Studio API ready on {}:{} public_url={}",
            self.config.host,
            self.config.port,
            self.config.public_url or "not configured",
        )

    async def wait_for_termination(self) -> None:
        """Wait for the serve task and fail if it ends outside intentional shutdown."""

        task = self._task
        if not self.enabled or task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._stopping:
                return
            raise RuntimeError("Studio API server task failed after readiness") from exc
        if not self._stopping:
            raise RuntimeError("Studio API server stopped unexpectedly after readiness")

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.should_exit = True
        if self._task is None:
            self._server = None
            return
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Studio API graceful shutdown timed out; cancelling")
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        except asyncio.CancelledError:
            raise
        except BaseException:
            # Runtime supervision owns the failure signal. Shutdown only needs to
            # consume an already-failed task and clear lifecycle state.
            logger.debug("Studio API task had already failed before shutdown")
        finally:
            self._task = None
            self._server = None
