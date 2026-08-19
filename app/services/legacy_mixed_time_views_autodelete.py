from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.legacy_time_views_delete_action import LegacyTimeViewsDeleteAction
from app.domain.models import Channel, Client, PostTask
from app.services.legacy_time_views_delete_action_ledger import (
    LegacyTimeViewsDeleteActionLedger,
)


@dataclass(frozen=True, slots=True)
class LegacyMixedTimeViewsTick:
    selected: int = 0
    observed: int = 0
    below_threshold: int = 0
    delete_winners: int = 0
    already_handled: int = 0
    unavailable: int = 0
    failures: int = 0


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value) if int(value) >= 0 else None


def _message_ids(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        result = tuple(int(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result and all(item > 0 for item in result) else None


class LegacyMixedTimeViewsAutodeleteObserver:
    """Observe legacy mixed time+views intents without owning a DELETE generation.

    Mixed legacy PostTasks retain the historical fallback timer, but the views side of
    the user contract must be observed too. This service only elects candidates and
    reads views. Threshold winners delegate destructive authorization to the existing
    ``LegacyTimeViewsDeleteActionLedger`` used by both scheduler timer paths, so timer
    and views race through the same occurrence-local reservation and permanent
    no-replay barrier.
    """

    def __init__(
        self,
        *,
        view_source,
        delete_provider,
        session_factory: async_sessionmaker[AsyncSession],
        batch_size: int = 25,
    ) -> None:
        self.view_source = view_source
        self.delete_provider = delete_provider
        self.session_factory = session_factory
        self.batch_size = max(1, min(int(batch_size), 200))
        self._ledger = LegacyTimeViewsDeleteActionLedger(session_factory)
        self._cursor_post_task_id = 0

    async def _candidate_ids_after(self, post_task_id: int) -> tuple[int, ...]:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(PostTask.id)
                .outerjoin(
                    LegacyTimeViewsDeleteAction,
                    LegacyTimeViewsDeleteAction.post_task_id == PostTask.id,
                )
                .where(
                    PostTask.id > int(post_task_id),
                    PostTask.status == "done",
                    LegacyTimeViewsDeleteAction.post_task_id.is_(None),
                    PostTask.payload["autodelete_views"].as_integer() > 0,
                    or_(
                        PostTask.payload[
                            "autodelete_effective_seconds"
                        ].as_integer()
                        > 0,
                        PostTask.payload["autodelete_seconds"].as_integer() > 0,
                    ),
                )
                .order_by(PostTask.id.asc())
                .limit(self.batch_size)
            )
            ids = tuple(int(value) for value in rows.scalars().all())
            await session.rollback()
            return ids

    async def _select_candidate_ids(self) -> tuple[int, ...]:
        ids = await self._candidate_ids_after(self._cursor_post_task_id)
        if not ids and self._cursor_post_task_id:
            self._cursor_post_task_id = 0
            ids = await self._candidate_ids_after(0)
        if ids:
            self._cursor_post_task_id = ids[-1]
        return ids

    async def _snapshot(
        self, post_task_id: int
    ) -> tuple[int, int, tuple[int, ...]] | None:
        async with self.session_factory() as session:
            post = await session.get(PostTask, int(post_task_id))
            if post is None or str(post.status) != "done":
                await session.rollback()
                return None
            payload = dict(post.payload or {})
            seconds = _positive_int(
                payload.get("autodelete_effective_seconds")
                or payload.get("autodelete_seconds")
            )
            threshold = _positive_int(payload.get("autodelete_views"))
            message_ids = _message_ids(payload.get("result_ids"))
            if (
                seconds is None
                or threshold is None
                or message_ids is None
                or payload.get("autodeleted") is True
            ):
                await session.rollback()
                return None
            channel = await session.get(Channel, int(post.channel_id))
            try:
                chat_id = int(channel.tg_chat_id) if channel is not None else 0
            except (TypeError, ValueError, OverflowError):
                chat_id = 0
            await session.rollback()
            if chat_id == 0:
                return None
            return chat_id, threshold, message_ids

    async def _send_report_best_effort(self, post_task_id: int) -> None:
        try:
            async with self.session_factory() as session:
                post = await session.get(PostTask, int(post_task_id))
                if post is None:
                    await session.rollback()
                    return
                payload = dict(post.payload or {})
                if payload.get("autodelete_report") is not True:
                    await session.rollback()
                    return
                channel = await session.get(Channel, int(post.channel_id))
                owner = (
                    await session.get(Client, int(channel.owner_id))
                    if channel is not None
                    else None
                )
                recipient = getattr(owner, "tg_user_id", None) if owner is not None else None
                result_link = payload.get("result_link")
                await session.rollback()

            if recipient is None:
                return
            text = "🗑️ Пост удалён по просмотрам"
            if result_link:
                text = f"{text}\n{result_link}"
            await self.delete_provider.send_message(
                chat_id=int(recipient),
                text=text,
                disable_web_page_preview=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Legacy mixed views observer: report failed post_task_id={} type={}",
                int(post_task_id),
                type(exc).__name__,
            )

    async def run_once(self) -> LegacyMixedTimeViewsTick:
        try:
            candidate_ids = await self._select_candidate_ids()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Legacy mixed views observer: candidate selection failed type={}",
                type(exc).__name__,
            )
            return LegacyMixedTimeViewsTick(failures=1)

        observed_count = 0
        below_threshold = 0
        delete_winners = 0
        already_handled = 0
        unavailable = 0
        failures = 0

        for post_task_id in candidate_ids:
            try:
                snapshot = await self._snapshot(post_task_id)
                if snapshot is None:
                    unavailable += 1
                    continue
                chat_id, threshold, message_ids = snapshot

                counts: list[int] = []
                for message_id in message_ids:
                    raw_views = await self.view_source.get_message_views(
                        chat_id,
                        int(message_id),
                    )
                    views = _nonnegative_int(raw_views)
                    if views is None:
                        counts = []
                        break
                    counts.append(views)
                if not counts:
                    unavailable += 1
                    continue

                observed_count += 1
                observed_views = min(counts)
                if observed_views < threshold:
                    below_threshold += 1
                    continue

                # Do not authorize from a stale observation if the legacy intent,
                # threshold, exact message set or target chat changed while views
                # were being read. The shared ledger then independently validates
                # the persisted target again before its unique reservation INSERT.
                if await self._snapshot(post_task_id) != snapshot:
                    unavailable += 1
                    continue

                result = await self._ledger.delete_once(
                    bot=self.delete_provider,
                    post_task_id=int(post_task_id),
                    chat_id=chat_id,
                    message_ids=message_ids,
                )
                if result.succeeded:
                    delete_winners += 1
                    await self._send_report_best_effort(int(post_task_id))
                elif result.handled:
                    already_handled += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Legacy mixed views observer: candidate failed post_task_id={} type={}",
                    int(post_task_id),
                    type(exc).__name__,
                )

        return LegacyMixedTimeViewsTick(
            selected=len(candidate_ids),
            observed=observed_count,
            below_threshold=below_threshold,
            delete_winners=delete_winners,
            already_handled=already_handled,
            unavailable=unavailable,
            failures=failures,
        )
