from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import PostTask
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import PublicationRuntimeProjector
from app.services.scheduler_errors import SAFE_DELIVERY_ERROR
from app.services.scheduler_task_lease import (
    DEFAULT_SCHEDULER_LEASE_SECONDS,
    SchedulerTaskLeaseHandle,
    SchedulerTaskLeaseService,
)
from app.workers.reliable_scheduler import Scheduler as ReliableScheduler


SAFE_AUXILIARY_ERROR = "Telegram auxiliary operation failed"
SAFE_DELETE_NOT_FOUND = "message to delete not found"
SAFE_DELETE_FORBIDDEN = "message can't be deleted"


class SchedulerDeliveryError(RuntimeError):
    """Safe transport-boundary failure suitable for durable/user-visible state."""


class SchedulerAuxiliaryError(RuntimeError):
    """Safe Telegram auxiliary failure suitable for legacy logs/fallback logic."""


def _safe_auxiliary_message(operation: str, exc: Exception) -> str:
    if operation == "delete_message":
        raw = str(exc).lower()
        if "message to delete not found" in raw or "message_id_invalid" in raw:
            return SAFE_DELETE_NOT_FOUND
        if "can't be deleted" in raw or "message can't be deleted" in raw:
            return SAFE_DELETE_FORBIDDEN
    return SAFE_AUXILIARY_ERROR


class _RedactedTelegramBot:
    """Proxy auxiliary Bot API calls without exposing provider exception text."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def _call(self, operation: str, *args, **kwargs):
        method = getattr(self._delegate, operation)
        try:
            return await method(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Scheduler: Telegram auxiliary call failed operation={} type={}",
                operation,
                type(exc).__name__,
            )
            raise SchedulerAuxiliaryError(
                _safe_auxiliary_message(operation, exc)
            ) from None

    async def get_chat(self, *args, **kwargs):
        return await self._call("get_chat", *args, **kwargs)

    async def delete_message(self, *args, **kwargs):
        return await self._call("delete_message", *args, **kwargs)

    async def send_message(self, *args, **kwargs):
        return await self._call("send_message", *args, **kwargs)

    async def pin_chat_message(self, *args, **kwargs):
        return await self._call("pin_chat_message", *args, **kwargs)

    async def forward_message(self, *args, **kwargs):
        return await self._call("forward_message", *args, **kwargs)


class _RedactedPostingService:
    """Delegate PostingService while replacing provider exception text at the edge."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._bot_delegate = None
        self._bot_proxy = None

    @property
    def bot(self):
        # Several state-machine tests intentionally supply a posting stub without a
        # bot because they never execute Telegram calls. Preserve that constructor
        # seam and only require/wrap `.bot` when legacy code actually accesses it.
        raw_bot = getattr(self._delegate, "bot")
        if self._bot_proxy is None or self._bot_delegate is not raw_bot:
            self._bot_delegate = raw_bot
            self._bot_proxy = _RedactedTelegramBot(raw_bot)
        return self._bot_proxy

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def send_now(self, *args, **kwargs):
        try:
            return await self._delegate.send_now(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Telegram/provider exceptions can contain request URLs, payloads or
            # credentials. The mature base scheduler persists/logs str(exception), so
            # replace the exception before it crosses that compatibility boundary.
            logger.warning(
                "Scheduler: Telegram delivery transport failed type={}",
                type(exc).__name__,
            )
            raise SchedulerDeliveryError(SAFE_DELIVERY_ERROR) from None


class Scheduler(ReliableScheduler):
    """Reliable scheduler with leased atomic claim and Publication projection.

    PostTask remains the compatibility transport during migration. A durable lease
    records liveness for each task owned in `processing`; Telegram provider exception
    text is redacted before entering legacy persistence/logging boundaries. Generated
    autodelete lifecycle is mirrored into Publication metadata so the canonical domain
    no longer loses deletion state when PostTask is eventually retired.
    """

    def __init__(
        self,
        session_or_factory,
        posting,
        interval_seconds: int = 5,
        *,
        lease_ttl_seconds: int = DEFAULT_SCHEDULER_LEASE_SECONDS,
        lease_heartbeat_seconds: float = 45.0,
    ) -> None:
        self._posting_delegate = posting
        super().__init__(
            session_or_factory,
            _RedactedPostingService(posting),
            interval_seconds=interval_seconds,
        )
        self._lease_holder = f"scheduler-{uuid.uuid4().hex[:16]}"
        self._lease_ttl_seconds = max(30, min(int(lease_ttl_seconds), 3600))
        self._lease_heartbeat_seconds = max(
            1.0,
            min(float(lease_heartbeat_seconds), self._lease_ttl_seconds / 2),
        )
        self._active_leases: dict[int, SchedulerTaskLeaseHandle] = {}

    async def _project_runtime(
        self,
        session: AsyncSession,
        post: PostTask,
    ) -> None:
        try:
            await PublicationRuntimeProjector(session).project_task(
                int(post.id),
                dict(post.payload or {}),
            )
        except Exception as exc:
            await self._rollback(session, "publication runtime projection")
            logger.warning(
                "Scheduler: publication runtime projection failed post_id={} type={}",
                int(post.id),
                type(exc).__name__,
            )

    async def _project_task_by_id(self, task_id: int) -> None:
        if self.session_factory is None:
            return
        try:
            async with self.session_factory() as projection_session:
                post = await projection_session.get(PostTask, int(task_id))
                if post is None:
                    return
                await self._project_runtime(projection_session, post)
        except Exception as exc:
            logger.warning(
                "Scheduler: delayed runtime projection failed post_id={} type={}",
                int(task_id),
                type(exc).__name__,
            )

    async def _project_publication(
        self,
        session: AsyncSession,
        post: PostTask,
    ) -> None:
        task_id = int(post.id)
        try:
            if self.session_factory is not None:
                async with self.session_factory() as projection_session:
                    projection_task = await projection_session.get(PostTask, task_id)
                    if projection_task is None:
                        return
                    publication = await LegacyPublicationBridge(
                        projection_session
                    ).reconcile_task(projection_task)
                    if publication is not None:
                        await self._project_runtime(projection_session, projection_task)
            else:
                publication = await LegacyPublicationBridge(session).reconcile_task(post)
                if publication is not None:
                    await self._project_runtime(session, post)
        except Exception as exc:
            if self.session_factory is None:
                await self._rollback(session, "publication projection")
            logger.warning(
                "Scheduler: publication projection failed post_id={} type={}",
                task_id,
                type(exc).__name__,
            )
            return

        if publication is not None:
            logger.trace(
                "Scheduler: projected post_id={} publication_id={} status={}",
                task_id,
                int(publication.id),
                publication.status,
            )

    async def _mark_processing(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        """Atomically claim pending tasks and persist execution liveness leases."""
        if not items:
            return

        selected = list(items)
        claimed: list[PostTask] = []
        lease_service = SchedulerTaskLeaseService(session)
        for post in selected:
            try:
                handle = await lease_service.claim_pending(
                    task_id=int(post.id),
                    holder=self._lease_holder,
                    ttl_seconds=self._lease_ttl_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "Scheduler: leased claim failed post_id={} type={}",
                    int(post.id),
                    type(exc).__name__,
                )
                continue
            if handle is None:
                continue
            self._active_leases[int(post.id)] = handle
            post.status = "processing"
            claimed.append(post)

        items[:] = claimed
        for post in claimed:
            await self._project_publication(session, post)

        skipped = len(selected) - len(claimed)
        if skipped:
            logger.info(
                "Scheduler: claim contention selected={} claimed={} skipped={}",
                len(selected),
                len(claimed),
                skipped,
            )

    async def _renew_lease(self, task_id: int) -> None:
        handle = self._active_leases.get(int(task_id))
        if handle is None or self.session_factory is None:
            return
        try:
            async with self.session_factory() as lease_session:
                renewed = await SchedulerTaskLeaseService(lease_session).renew(
                    handle,
                    ttl_seconds=self._lease_ttl_seconds,
                )
        except Exception as exc:
            logger.warning(
                "Scheduler: lease heartbeat failed post_id={} type={}",
                int(task_id),
                type(exc).__name__,
            )
            return
        if renewed is None:
            logger.warning(
                "Scheduler: lease heartbeat lost ownership post_id={}",
                int(task_id),
            )
            return
        self._active_leases[int(task_id)] = renewed

    async def _lease_heartbeat(self, task_ids: list[int]) -> None:
        try:
            while True:
                await asyncio.sleep(self._lease_heartbeat_seconds)
                for task_id in task_ids:
                    await self._renew_lease(task_id)
        except asyncio.CancelledError:
            raise

    async def _release_lease_if_terminal(
        self,
        session: AsyncSession,
        post: PostTask,
    ) -> None:
        task_id = int(post.id)
        handle = self._active_leases.get(task_id)
        if handle is None:
            return

        try:
            if self.session_factory is not None:
                async with self.session_factory() as lease_session:
                    current = await lease_session.get(PostTask, task_id)
                    if current is None or str(current.status) == "processing":
                        return
                    released = await SchedulerTaskLeaseService(lease_session).release(
                        handle
                    )
            else:
                if str(post.status) == "processing":
                    return
                released = await SchedulerTaskLeaseService(session).release(handle)
        except Exception as exc:
            logger.warning(
                "Scheduler: lease release failed post_id={} type={}",
                task_id,
                type(exc).__name__,
            )
            return

        if released:
            self._active_leases.pop(task_id, None)
        else:
            logger.warning("Scheduler: lease release lost ownership post_id={}", task_id)

    async def _del_later(
        self,
        bot,
        chat_id: int,
        msg_ids: list[int],
        delay: int,
        post_id_val: int,
        report: bool,
        link_val: str | None,
    ) -> None:
        await super()._del_later(
            bot,
            chat_id,
            msg_ids,
            delay,
            post_id_val,
            report,
            link_val,
        )
        await self._project_task_by_id(int(post_id_val))

    async def _due_autodelete_candidate_ids(
        self,
        session: AsyncSession,
    ) -> list[int]:
        result = await session.execute(
            select(PostTask.id)
            .where(
                (PostTask.status == "done")
                & (
                    (PostTask.payload["autodelete_at"].as_string().is_not(None))
                    | (PostTask.payload["autodelete_seconds"].as_integer() > 0)
                )
                & (
                    (PostTask.payload["autodeleted"].as_boolean().is_(None))
                    | (PostTask.payload["autodeleted"].as_boolean().is_(False))
                )
            )
            .order_by(PostTask.id.desc())
            .limit(50)
        )
        return [int(task_id) for task_id in result.scalars().all()]

    async def _process_due_deletions(self, session: AsyncSession) -> None:
        candidate_ids = await self._due_autodelete_candidate_ids(session)
        await super()._process_due_deletions(session)
        for task_id in candidate_ids:
            post = await session.get(PostTask, task_id)
            if post is not None:
                await self._project_runtime(session, post)

    async def _process_items(
        self,
        session: AsyncSession,
        items: list[PostTask],
    ) -> None:
        task_ids = [
            int(post.id)
            for post in items
            if int(post.id) in self._active_leases
        ]
        heartbeat = None
        if task_ids and self.session_factory is not None:
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(task_ids),
                name="scheduler-task-lease-heartbeat",
            )

        completed_normally = False
        try:
            await super()._process_items(session, items)
            completed_normally = True
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

        if not completed_normally:
            # Keep leases until expiry. The recovery worker can identify these
            # lease-backed ambiguous processing rows without touching old legacy rows.
            return

        for post in items:
            await self._release_lease_if_terminal(session, post)
            await self._project_publication(session, post)
