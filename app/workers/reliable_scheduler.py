from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.domain.models import Channel, Client, PostTask
from app.workers.scheduler import Scheduler as BaseScheduler


class Scheduler(BaseScheduler):
    """Reliability-hardened runtime scheduler.

    The legacy scheduler contains a large amount of mature posting behavior. This
    subclass deliberately overrides only lifecycle and core state transitions so
    failures are observable and database sessions are recovered before the next
    scheduler operation.
    """

    @staticmethod
    async def _rollback(session: AsyncSession, context: str) -> None:
        try:
            await session.rollback()
        except Exception:
            logger.exception("Scheduler: rollback failed after {}", context)

    async def stop(self) -> None:
        logger.info("Scheduler: stop requested")
        self._stopping.set()

        for name, task in (
            ("scheduler", self._task),
            ("scheduler-autodelete", self._del_task),
        ):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Scheduler: {} stop timed out; cancelling", name)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler: {} failed during shutdown", name)

        if self.session is not None:
            try:
                await self.session.close()
            except Exception:
                logger.exception("Scheduler: failed to close long-lived DB session")

    async def _run_deletor(self) -> None:
        while not self._stopping.is_set():
            try:
                if self.session_factory is not None:
                    async with self.session_factory() as session:
                        await self._process_due_deletions(session)
                else:
                    await self._process_due_deletions(self.session)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler: autodelete worker iteration failed")

            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._del_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

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
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

        success_count = 0
        for mid in msg_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=int(mid))
                success_count += 1
            except Exception as exc:
                logger.warning(
                    "Scheduler: timed autodelete failed post_id={} chat_id={} msg_id={} err={!r}",
                    post_id_val,
                    chat_id,
                    int(mid),
                    exc,
                )

        if success_count == 0:
            logger.warning(
                "Scheduler: timed autodelete removed no messages post_id={}",
                post_id_val,
            )
            return

        logger.info(
            "Scheduler: timed autodelete done post_id={} messages={}/{}",
            post_id_val,
            success_count,
            len(msg_ids),
        )

        try:
            async with AsyncSessionLocal() as session:
                post = await session.get(PostTask, int(post_id_val))
                if post is None:
                    logger.warning(
                        "Scheduler: timed autodelete post row missing post_id={}",
                        post_id_val,
                    )
                    return

                payload = dict(post.payload or {})
                payload["autodeleted"] = True
                payload["autodeleted_at"] = datetime.now(timezone.utc).isoformat()
                post.payload = payload
                try:
                    await session.commit()
                except Exception:
                    await self._rollback(session, "timed autodelete persistence")
                    logger.exception(
                        "Scheduler: failed to persist timed autodelete post_id={}",
                        post_id_val,
                    )
                    return

                if not report:
                    return

                channel = await session.get(Channel, int(post.channel_id))
                owner = (
                    await session.get(Client, int(channel.owner_id))
                    if channel and channel.owner_id
                    else None
                )
                if owner and getattr(owner, "tg_user_id", None):
                    try:
                        await bot.send_message(
                            chat_id=int(owner.tg_user_id),
                            text=(
                                "🗑️ Пост удалён по таймеру\n"
                                + (payload.get("result_link") or link_val or "")
                            ),
                            disable_web_page_preview=True,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Scheduler: autodelete report failed post_id={} err={!r}",
                            post_id_val,
                            exc,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Scheduler: timed autodelete DB/report handling failed post_id={}",
                post_id_val,
            )

    async def _apply_autodelete(
        self,
        session: AsyncSession,
        post: PostTask,
        tg_chat_id: int,
        pl: dict,
        ids: list[int],
    ) -> None:
        ad_base = int(pl.get("autodelete_seconds") or 0)
        ad_eff = int(pl.get("autodelete_effective_seconds") or 0)
        rep_sec = int(pl.get("repeat_seconds") or 0)
        use_sec = ad_eff if ad_eff > 0 else ad_base

        if use_sec <= 0 or not ids:
            logger.debug(
                "Scheduler: autodelete skipped post_id={} use_sec={} ids={}",
                int(post.id),
                use_sec,
                len(ids),
            )
            return

        align_to_repeat = (
            bool(pl.get("repeat_on", False))
            and rep_sec > 0
            and use_sec == rep_sec
            and getattr(post, "scheduled_at", None) is not None
        )
        if align_to_repeat:
            due = self._as_utc(post.scheduled_at) + timedelta(seconds=rep_sec)
        else:
            due = datetime.now(timezone.utc) + timedelta(seconds=use_sec)

        existing_eff = int(pl.get("autodelete_effective_seconds") or 0)
        pl["autodelete_effective_seconds"] = max(existing_eff, use_sec)
        pl["autodelete_seconds"] = max(
            int(pl.get("autodelete_seconds") or 0), use_sec
        )
        pl["autodelete_at"] = due.isoformat()
        post.payload = pl

        try:
            await session.commit()
        except Exception:
            await self._rollback(session, "autodelete schedule persistence")
            post.payload = pl
            logger.exception(
                "Scheduler: failed to persist autodelete schedule post_id={}; "
                "payload kept for final post commit/fallback worker",
                int(post.id),
            )
            return

        logger.info(
            "Scheduler: autodelete scheduled post_id={} at={}",
            int(post.id),
            pl["autodelete_at"],
        )

        delay = max(0, int((due - datetime.now(timezone.utc)).total_seconds()))
        try:
            asyncio.create_task(
                self._del_later(
                    self.posting.bot,
                    tg_chat_id,
                    list(ids),
                    delay,
                    int(post.id),
                    bool(pl.get("autodelete_report", False)),
                    pl.get("result_link"),
                ),
                name=f"scheduler-autodelete-{int(post.id)}",
            )
        except Exception:
            logger.exception(
                "Scheduler: failed to create local autodelete timer post_id={}; "
                "DB fallback remains active",
                int(post.id),
            )

    async def _skip_overdue_repeat_and_schedule_next(
        self, session: AsyncSession, post: PostTask, pl: dict
    ) -> bool:
        if not bool(pl.get("repeat_on", False)):
            return False

        boot_time = self._boot_time or datetime.now(timezone.utc)
        scheduled_at = getattr(post, "scheduled_at", None)
        if scheduled_at is None:
            return False
        scheduled_at = self._as_utc(scheduled_at)
        if scheduled_at > boot_time:
            return False

        post.status = "skipped"
        repeat_seconds = int(pl.get("repeat_seconds") or 0)
        if repeat_seconds > 0:
            next_when = self._compute_next_repeat_time(
                scheduled_at, repeat_seconds, boot_time
            )
            payload = self._cleanup_runtime_fields(dict(pl))
            ad_base = int(
                pl.get("autodelete_seconds")
                or pl.get("autodelete_effective_seconds")
                or 0
            )
            if ad_base > 0:
                payload["autodelete_seconds"] = ad_base
            payload.pop("autodelete_at", None)
            payload = self._inherit_flags_for_repeat(payload, int(post.id))
            session.add(
                PostTask(
                    channel_id=int(post.channel_id),
                    payload=payload,
                    dedupe_key=None,
                    scheduled_at=next_when,
                )
            )

        try:
            await session.commit()
        except Exception:
            await self._rollback(session, "overdue repeat reschedule")
            logger.exception(
                "Scheduler: failed to skip/reschedule overdue repeat post_id={}",
                int(post.id),
            )
            raise
        return True

    async def _dedupe_repeat_series(
        self, session: AsyncSession, post: PostTask, pl: dict
    ) -> None:
        if not bool(pl.get("repeat_on", False)):
            return
        repeat_group = pl.get("repeat_group_id")
        if repeat_group is None:
            return

        try:
            result = await session.execute(
                select(PostTask).where(
                    (PostTask.status == "pending")
                    & (PostTask.channel_id == int(post.channel_id))
                )
            )
            changed = False
            now = datetime.now(timezone.utc)
            for other in list(result.scalars().all()):
                if other.id == post.id:
                    continue
                payload = dict(other.payload or {})
                if (
                    payload.get("repeat_group_id") == repeat_group
                    and other.scheduled_at
                    and self._as_utc(other.scheduled_at) <= now
                ):
                    other.status = "skipped"
                    changed = True
            if changed:
                await session.commit()
        except Exception:
            await self._rollback(session, "repeat deduplication")
            logger.exception(
                "Scheduler: repeat deduplication failed post_id={}", int(post.id)
            )
            raise

    async def _boot_cleanup_repeats(
        self, session: AsyncSession, items: list[PostTask]
    ) -> list[PostTask]:
        if self._boot_cleanup_done or self._boot_time is None or not items:
            self._boot_cleanup_done = True
            return items

        boot_time = self._boot_time
        overdue: list[PostTask] = []
        for post in items:
            payload = dict(getattr(post, "payload", None) or {})
            when = getattr(post, "scheduled_at", None)
            if bool(payload.get("repeat_on", False)) and when is not None:
                if self._as_utc(when) <= boot_time:
                    overdue.append(post)

        if not overdue:
            self._boot_cleanup_done = True
            return items

        scheduled_groups: set[int] = set()
        try:
            for post in overdue:
                post.status = "skipped"
                payload = dict(getattr(post, "payload", None) or {})
                repeat_seconds = int(payload.get("repeat_seconds") or 0)
                if repeat_seconds <= 0:
                    continue
                group_id = int(payload.get("repeat_group_id") or int(post.id))
                if group_id in scheduled_groups or group_id in self._boot_group_scheduled:
                    continue
                scheduled_groups.add(group_id)
                next_when = self._compute_next_repeat_time(
                    self._as_utc(post.scheduled_at), repeat_seconds, boot_time
                )
                next_payload = self._cleanup_runtime_fields(payload)
                ad_base = int(
                    payload.get("autodelete_seconds")
                    or payload.get("autodelete_effective_seconds")
                    or 0
                )
                if ad_base > 0:
                    next_payload["autodelete_seconds"] = ad_base
                next_payload.pop("autodelete_at", None)
                next_payload = self._inherit_flags_for_repeat(
                    next_payload, int(post.id)
                )
                session.add(
                    PostTask(
                        channel_id=int(post.channel_id),
                        payload=next_payload,
                        dedupe_key=None,
                        scheduled_at=next_when,
                    )
                )
            await session.commit()
        except Exception:
            await self._rollback(session, "boot repeat cleanup")
            logger.exception("Scheduler: boot repeat cleanup failed")
            raise

        self._boot_group_scheduled.update(scheduled_groups)
        self._boot_cleanup_done = True
        return [post for post in items if post not in overdue]

    async def _prevent_repeat_overflow(
        self, session: AsyncSession, items: list[PostTask]
    ) -> None:
        if not items:
            return
        try:
            result = await session.execute(
                select(PostTask)
                .where(PostTask.status == "pending")
                .order_by(PostTask.scheduled_at.asc())
                .limit(500)
            )
            groups: dict[int, list[PostTask]] = {}
            for post in list(result.scalars().all()):
                payload = dict(getattr(post, "payload", None) or {})
                if not bool(payload.get("repeat_on", False)):
                    continue
                group_id = int(payload.get("repeat_group_id") or int(post.id))
                groups.setdefault(group_id, []).append(post)

            changed = False
            now = datetime.now(timezone.utc)
            limit = max(1, int(getattr(settings, "repeat_overflow_limit", 2)))
            for posts in groups.values():
                if len(posts) <= limit:
                    continue
                with_schedule = [p for p in posts if p.scheduled_at is not None]
                ordered = sorted(with_schedule, key=lambda p: self._as_utc(p.scheduled_at))
                keep = next(
                    (p for p in ordered if self._as_utc(p.scheduled_at) > now),
                    ordered[-1] if ordered else None,
                )
                for post in posts:
                    if keep is not None and post.id == keep.id:
                        payload = dict(post.payload or {})
                        payload["repeat_on"] = False
                        post.payload = payload
                    else:
                        post.status = "skipped"
                    changed = True
            if changed:
                await session.commit()
        except Exception:
            await self._rollback(session, "repeat overflow prevention")
            logger.exception("Scheduler: repeat overflow prevention failed")
            raise

    async def _mark_processing(
        self, session: AsyncSession, items: list[PostTask]
    ) -> None:
        if not items:
            return
        ids_to_mark = [int(post.id) for post in items]
        try:
            await session.execute(
                update(PostTask)
                .where(PostTask.id.in_(ids_to_mark))
                .values(status="processing")
            )
            await session.commit()
        except Exception:
            await self._rollback(session, "mark processing")
            logger.exception(
                "Scheduler: failed to mark posts processing ids={}", ids_to_mark
            )
            raise

    async def _persist_result_fields(
        self,
        session: AsyncSession,
        post: PostTask,
        tg_chat_id: int,
        pl: dict,
        ids: list[int],
    ) -> None:
        pl["result_ids"] = list(ids)
        link = pl.get("result_link")
        if not link and ids:
            mid = int(list(ids)[-1])
            username = None
            try:
                chat_info = await self.posting.bot.get_chat(tg_chat_id)
                username = getattr(chat_info, "username", None)
            except Exception as exc:
                logger.debug(
                    "Scheduler: result link lookup failed post_id={} err={!r}",
                    int(post.id),
                    exc,
                )
            if username:
                link = f"https://t.me/{username}/{mid}"
            else:
                cid = str(tg_chat_id)
                if cid.startswith("-100"):
                    link = f"https://t.me/c/{cid[4:]}/{mid}"
            if link:
                pl["result_link"] = link

        post.payload = pl
        try:
            await session.commit()
        except Exception:
            await self._rollback(session, "result field persistence")
            post.payload = pl
            logger.exception(
                "Scheduler: failed to persist result fields post_id={}; "
                "final status commit will retry payload persistence",
                int(post.id),
            )

    async def _schedule_next_repeat_if_needed(
        self, session: AsyncSession, post: PostTask, pl: dict
    ) -> None:
        if not bool(pl.get("repeat_on", False)):
            return
        repeat_seconds = int(pl.get("repeat_seconds") or 0)
        if repeat_seconds <= 0:
            return

        base = getattr(post, "scheduled_at", None)
        base = self._as_utc(base) if base is not None else datetime.now(timezone.utc)
        next_when = base + timedelta(seconds=repeat_seconds)
        now = datetime.now(timezone.utc)
        while next_when <= now:
            next_when += timedelta(seconds=repeat_seconds)

        payload = self._cleanup_runtime_fields(dict(pl))
        ad_base = int(
            pl.get("autodelete_seconds")
            or pl.get("autodelete_effective_seconds")
            or 0
        )
        if ad_base > 0:
            payload["autodelete_seconds"] = ad_base
            payload["autodelete_at"] = (
                next_when + timedelta(seconds=ad_base)
            ).isoformat()
        payload = self._inherit_flags_for_repeat(payload, int(post.id))
        session.add(
            PostTask(
                channel_id=int(post.channel_id),
                payload=payload,
                dedupe_key=None,
                scheduled_at=next_when,
            )
        )
        try:
            await session.commit()
        except Exception:
            await self._rollback(session, "next repeat scheduling")
            logger.exception(
                "Scheduler: failed to schedule next repeat after published post_id={}",
                int(post.id),
            )
            return

        logger.info(
            "Scheduler: repeat scheduled next post for id={} at={}",
            int(post.id),
            next_when,
        )
