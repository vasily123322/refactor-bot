from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta
import html
from zoneinfo import ZoneInfo  # pyright: ignore[reportUnusedImport]
from contextlib import suppress
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import AsyncSessionLocal
from app.core.config import settings
from app.domain.models import PostTask, Channel, Client
from app.repositories.settings import ChannelSettingsRepo
from app.services.posting import PostingService
from app.services.legacy_time_views_delete_action_ledger import (
    LegacyTimeViewsDeleteActionLedger,
)
from app.services.scheduling import (
    as_utc as _as_utc_svc,
    compute_next_repeat_time as _compute_next_repeat_time_svc,
    cleanup_runtime_fields as _cleanup_rt_svc,
    inherit_flags_for_repeat as _inherit_flags_svc,
)

SCHEDULER_BUILD_ID = "2025-10-22T16:05-autodel-precheck-fallback"


class Scheduler:
    def __init__(
        self, session_or_factory, posting: PostingService, interval_seconds: int = 5
    ):
        self.session = (
            session_or_factory if isinstance(session_or_factory, AsyncSession) else None
        )
        self.session_factory = (
            session_or_factory
            if not isinstance(session_or_factory, AsyncSession)
            else None
        )
        self.posting = posting
        self._legacy_time_views_delete_ledger = LegacyTimeViewsDeleteActionLedger(
            AsyncSessionLocal
        )
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._boot_time: datetime | None = None
        self._boot_group_scheduled: set[int] = set()
        self._del_task: asyncio.Task | None = None
        self._boot_cleanup_done: bool = False
        self._del_interval_seconds: int = max(10, int(self.interval_seconds) * 3)

    @staticmethod
    def _as_utc(dt) -> datetime:
        """Return aware UTC datetime for naive/aware input."""
        return _as_utc_svc(dt)

    @staticmethod
    def _cleanup_runtime_fields(payload: dict) -> dict:
        """Remove transient fields from payload before creating next repeat task."""
        return _cleanup_rt_svc(payload)

    @staticmethod
    def _inherit_flags_for_repeat(orig: dict, base_post_id: int) -> dict:
        """Carry forward stable flags for next repeat (autosign_applied, repeat_group_id)."""
        return _inherit_flags_svc(orig, base_post_id)

    @staticmethod
    def _compute_next_repeat_time(
        start_utc: datetime, repeat_seconds: int, after_utc: datetime
    ) -> datetime:
        """Return the first time > after_utc stepping by repeat_seconds from start_utc."""
        return _compute_next_repeat_time_svc(start_utc, repeat_seconds, after_utc)

    async def _del_later(
        self,
        bot,
        chat_id: int,
        msg_ids: list[int],
        delay: int,
        post_id_val: int,
        report: bool,
        link_val: str | None,
    ):
        """Класс‑уровневый удалитель сообщений по таймеру (используется как primary)."""
        try:
            await asyncio.sleep(delay)

            mixed_result = await self._legacy_time_views_delete_ledger.delete_once(
                bot=bot,
                post_task_id=int(post_id_val),
                chat_id=int(chat_id),
                message_ids=tuple(msg_ids),
            )
            if mixed_result.handled:
                if mixed_result.succeeded:
                    logger.info(
                        f"Scheduler: durable mixed autodelete done for post id={post_id_val} messages={len(msg_ids)}"
                    )
                    if report:
                        try:
                            async with AsyncSessionLocal() as s2:
                                p2 = await s2.get(PostTask, int(post_id_val))
                                if p2:
                                    ch2 = await s2.get(Channel, int(p2.channel_id))
                                    owner2 = (
                                        await s2.get(Client, int(ch2.owner_id))
                                        if ch2 is not None
                                        else None
                                    )
                                    if owner2 and getattr(owner2, "tg_user_id", None):
                                        with suppress(Exception):
                                            await bot.send_message(
                                                chat_id=int(owner2.tg_user_id),
                                                text=(
                                                    "🗑️ Пост удалён по таймеру\n"
                                                    + (link_val or "")
                                                ),
                                                disable_web_page_preview=True,
                                            )
                        except Exception:
                            pass
                return

            success_count = 0
            for mid in msg_ids:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=int(mid))
                    success_count += 1
                except Exception:
                    pass
            if success_count > 0:
                logger.info(
                    f"Scheduler: autodelete done for post id={post_id_val} messages={success_count}/{len(msg_ids)}"
                )
            else:
                logger.warning(
                    f"Scheduler: autodelete failed (no messages deleted) for post id={post_id_val}"
                )
            # Пометим запись и, если нужно, отправим короткий отчёт
            try:
                async with AsyncSessionLocal() as s2:
                    p2 = await s2.get(PostTask, int(post_id_val))
                    if p2:
                        pl2 = dict(p2.payload or {})
                        if success_count > 0 and (
                            (
                                int(
                                    pl2.get("autodelete_effective_seconds")
                                    or pl2.get("autodelete_seconds")
                                    or 0
                                )
                                > 0
                            )
                            or (int(pl2.get("autodelete_views") or 0) > 0)
                        ):
                            pl2["autodeleted"] = True
                            from datetime import datetime as _dt, timezone as _tz

                            pl2["autodeleted_at"] = _dt.now(_tz.utc).isoformat()
                            p2.payload = pl2
                            await s2.commit()
                            if report:
                                owner2 = await s2.get(
                                    Client,
                                    getattr(
                                        await s2.get(Channel, int(p2.channel_id)),
                                        "owner_id",
                                        0,
                                    ),
                                )
                                if owner2 and getattr(owner2, "tg_user_id", None):
                                    with suppress(Exception):
                                        await bot.send_message(
                                            chat_id=int(owner2.tg_user_id),
                                            text=(
                                                "🗑️ Пост удалён по таймеру\n"
                                                + (pl2.get("result_link") or "")
                                            ),
                                            disable_web_page_preview=True,
                                        )
            except Exception:
                pass
        except Exception:
            # Любые непойманные ошибки таймера удалителя не должны падать наружу
            pass

    async def _apply_autodelete(
        self,
        session: AsyncSession,
        post: PostTask,
        tg_chat_id: int,
        pl: dict,
        ids: list[int],
    ) -> None:
        """Единый расчёт и запуск автоудаления для опубликованного поста."""
        try:
            ad_base = int(pl.get("autodelete_seconds") or 0)
            ad_eff = int(pl.get("autodelete_effective_seconds") or 0)
            rep_sec = int(pl.get("repeat_seconds") or 0)
            use_sec = ad_eff if ad_eff > 0 else ad_base
            # Не подставляем таймер из repeat_seconds, если пользователь его не задавал
            align_to_repeat = False
            try:
                if (
                    bool(pl.get("repeat_on", False))
                    and rep_sec > 0
                    and use_sec == rep_sec
                ):
                    align_to_repeat = True
            except Exception:
                align_to_repeat = False
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td

            if use_sec > 0 and ids:
                existing_eff = int(pl.get("autodelete_effective_seconds") or 0)
                pl["autodelete_effective_seconds"] = int(max(existing_eff, use_sec))
                # Гарантируем базовый seconds
                pl["autodelete_seconds"] = int(
                    max(int(pl.get("autodelete_seconds") or 0), use_sec)
                )
                if align_to_repeat and getattr(post, "scheduled_at", None) is not None:
                    base = post.scheduled_at
                    base_aware = (
                        base
                        if (getattr(base, "tzinfo", None) is not None)
                        else base.replace(tzinfo=_tz.utc)
                    )
                    base_aware = base_aware.astimezone(_tz.utc)
                    due = base_aware + _td(seconds=int(rep_sec))
                else:
                    due = _dt.now(_tz.utc) + _td(seconds=int(use_sec))
                pl["autodelete_at"] = due.isoformat()
                post.payload = pl
                await session.commit()
                try:
                    logger.info(
                        f"Scheduler: set autodelete post_id={int(post.id)} scheduled_at={(getattr(post, 'scheduled_at', None).isoformat() if getattr(post, 'scheduled_at', None) else 'None')} autodelete_at={pl['autodelete_at']}"
                    )
                except Exception:
                    pass
                # локальный таймер
                try:
                    delay = max(0, int((due - _dt.now(_tz.utc)).total_seconds()))
                    asyncio.create_task(
                        self._del_later(
                            self.posting.bot,
                            tg_chat_id,
                            list(ids),
                            delay,
                            int(post.id),
                            bool(pl.get("autodelete_report", False)),
                            pl.get("result_link"),
                        )
                    )
                except Exception:
                    pass
            else:
                try:
                    has_ids = bool(ids)
                    logger.info(
                        f"Scheduler: autodelete skipped post_id={int(post.id)} use_sec={int(use_sec)} rep_sec={int(rep_sec)} has_ids={has_ids} ad_seconds_now={int(pl.get('autodelete_seconds') or 0)}"
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.exception(
                f"Scheduler: autodelete schedule error for post id={int(post.id)}: {e}"
            )

    async def _run_deletor(self) -> None:
        while not self._stopping.is_set():
            try:
                if self.session_factory is not None:
                    async with self.session_factory() as session:
                        await self._process_due_deletions(session)
                else:
                    await self._process_due_deletions(self.session)
            except Exception:
                pass
            await asyncio.sleep(self._del_interval_seconds)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        logger.info(f"Scheduler: start build={SCHEDULER_BUILD_ID}")
        self._stopping.clear()
        # зафиксируем момент запуска для логики пропуска просроченных repeat-постов
        self._boot_time = datetime.now(timezone.utc)
        self._task = asyncio.create_task(self._run(), name="scheduler")
        # фоновый воркер автоудаления без полного сканирования
        if self._del_task is None or self._del_task.done():
            self._del_task = asyncio.create_task(
                self._run_deletor(), name="scheduler-autodelete"
            )

    async def stop(self) -> None:
        logger.info("Scheduler: stop requested")
        self._stopping.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                self._task.cancel()
        if self._del_task:
            try:
                await asyncio.wait_for(self._del_task, timeout=5)
            except Exception:
                self._del_task.cancel()
        # Закроем возможные долгоживущие сессии, чтобы вернуть соединения в пул
        try:
            if self.session is not None:
                await self.session.close()
        except Exception:
            pass

    async def _skip_overdue_repeat_and_schedule_next(
        self, session: AsyncSession, post: PostTask, pl: dict
    ) -> bool:
        """Freeze historical overdue repeat continuation without creating transport."""
        if not bool(pl.get("repeat_on", False)):
            return False
        sched = getattr(post, "scheduled_at", None)
        if sched is None:
            return False
        boot_time = self._boot_time or datetime.now(timezone.utc)
        if self._as_utc(sched) > boot_time:
            return False
        payload = dict(pl)
        payload["repeat_on"] = False
        payload["legacy_repeat_continuation_frozen"] = True
        post.payload = payload
        post.status = "skipped"
        await session.commit()
        logger.info(
            "Scheduler: frozen overdue legacy repeat post_id={} without successor",
            int(post.id),
        )
        return True
    async def _dedupe_repeat_series(
        self, session: AsyncSession, post: PostTask, pl: dict
    ) -> None:
        try:
            if not bool(pl.get("repeat_on", False)):
                return
            rg = pl.get("repeat_group_id")
            if rg is None:
                return
            others = await session.execute(
                select(PostTask).where(
                    (PostTask.status == "pending")
                    & (PostTask.channel_id == int(post.channel_id))
                )
            )
            for p2 in list(others.scalars().all()):
                if p2.id == post.id:
                    continue
                pl2 = dict(p2.payload or {})
                if (
                    pl2.get("repeat_group_id") == rg
                    and p2.scheduled_at
                    and p2.scheduled_at <= datetime.now(timezone.utc)
                ):
                    p2.status = "skipped"
                    await session.commit()
        except Exception:
            pass

    async def _boot_cleanup_repeats(
        self, session: AsyncSession, items: list[PostTask]
    ) -> list[PostTask]:
        """Freeze boot-time legacy repeat continuation without successor creation."""
        if self._boot_cleanup_done or self._boot_time is None or not items:
            self._boot_cleanup_done = True
            return items

        remaining: list[PostTask] = []
        changed = False
        for post in items:
            payload = dict(getattr(post, "payload", None) or {})
            when = getattr(post, "scheduled_at", None)
            if (
                bool(payload.get("repeat_on", False))
                and when is not None
                and self._as_utc(when) <= self._boot_time
            ):
                payload["repeat_on"] = False
                payload["legacy_repeat_continuation_frozen"] = True
                post.payload = payload
                post.status = "skipped"
                changed = True
                continue
            remaining.append(post)
        if changed:
            await session.commit()
        self._boot_cleanup_done = True
        return remaining
    async def _prevent_repeat_overflow(
        self, session: AsyncSession, items: list[PostTask]
    ) -> None:
        try:
            if items:
                res2 = await session.execute(
                    select(PostTask)
                    .where((PostTask.status == "pending"))
                    .order_by(PostTask.scheduled_at.asc())
                    .limit(500)
                )
                all_pending = list(res2.scalars().all())
                groups: dict[int, list[PostTask]] = {}
                for p in all_pending:
                    pl0 = dict(getattr(p, "payload", None) or {})
                    if not bool(pl0.get("repeat_on", False)):
                        continue
                    rg = int(pl0.get("repeat_group_id") or int(p.id))
                    groups.setdefault(rg, []).append(p)
                for rg, lst in groups.items():
                    limit = max(1, int(getattr(settings, "repeat_overflow_limit", 2)))
                    if len(lst) > limit:
                        lst_sorted = sorted(
                            [x for x in lst if x.scheduled_at is not None],
                            key=lambda x: x.scheduled_at,
                        )
                        keep = None
                        for x in lst_sorted:
                            if x.scheduled_at and x.scheduled_at > datetime.now(
                                timezone.utc
                            ):
                                keep = x
                                break
                        if keep is None and lst_sorted:
                            keep = lst_sorted[-1]
                        for x in lst:
                            if keep and x.id == keep.id:
                                plx = dict(x.payload or {})
                                plx["repeat_on"] = False
                                x.payload = plx
                                continue
                            x.status = "skipped"
                await session.commit()
        except Exception:
            pass

    async def _mark_processing(
        self, session: AsyncSession, items: list[PostTask]
    ) -> None:
        try:
            if items:
                ids_to_mark = [int(getattr(p, "id")) for p in items]
                await session.execute(
                    update(PostTask)
                    .where(PostTask.id.in_(ids_to_mark))
                    .values(status="processing")
                )
                await session.commit()
        except Exception:
            pass

    async def _send_admin_log_if_configured(
        self, tg_chat_id: int, ids: list[int], pl: dict
    ) -> None:
        try:
            from app.core.db import AsyncSessionLocal as _Sess
            from app.repositories.admin import AdminConfigRepo as _AdminCfg

            async with _Sess() as _slog:
                log_chat_id = await _AdminCfg(_slog).get_log_chat_id()
                if not (log_chat_id and ids):
                    return
                post_link = None
                try:
                    mid = int(list(ids)[-1])
                    chat_info = await self.posting.bot.get_chat(tg_chat_id)
                    uname = getattr(chat_info, "username", None)
                    if uname:
                        post_link = f"https://t.me/{uname}/{mid}"
                    else:
                        cid = str(tg_chat_id)
                        if cid.startswith("-100"):
                            post_link = f"https://t.me/c/{cid[4:]}/{mid}"
                except Exception:
                    post_link = None
                chan_link = None
                try:
                    chat_info = await self.posting.bot.get_chat(tg_chat_id)
                    uname = getattr(chat_info, "username", None)
                    if uname:
                        chan_link = f"https://t.me/{uname}"
                    else:
                        inv = await self.posting.bot.create_chat_invite_link(
                            chat_id=tg_chat_id,
                            name="post-log",
                            creates_join_request=False,
                        )
                        chan_link = getattr(inv, "invite_link", None)
                except Exception:
                    chan_link = None
                author = dict(pl.get("meta", {}) or {})
                uname = author.get("author_username")
                uid = author.get("author_user_id")
                fname = author.get("author_full_name")
                if uname:
                    user_html = f'<a href="https://t.me/{uname}">@{uname}</a>'
                elif uid:
                    user_html = f'<a href="tg://user?id={uid}">{fname or uid}</a>'
                else:
                    user_html = "пользователь"
                text_log = f"пользователь {user_html} отправил пост: "
                if post_link:
                    text_log += f'<a href="{post_link}">ссылка</a>'
                else:
                    text_log += "(без ссылки)"
                text_log += " в канал/чат: "
                if chan_link:
                    text_log += f'<a href="{chan_link}">перейти</a>'
                else:
                    text_log += str(tg_chat_id)
                from contextlib import suppress as _s

                with _s(Exception):
                    await self.posting.bot.send_message(
                        log_chat_id, text_log, disable_web_page_preview=True
                    )
        except Exception:
            pass

    async def _persist_result_fields(
        self,
        session: AsyncSession,
        post: PostTask,
        tg_chat_id: int,
        pl: dict,
        ids: list[int],
    ) -> None:
        try:
            pl["result_ids"] = list(ids)
            link = pl.get("result_link")
            if not link:
                mid = None
                try:
                    mid = int(list(ids)[-1])
                except Exception:
                    mid = None
                if mid is not None:
                    uname = None
                    try:
                        chat_info = await self.posting.bot.get_chat(tg_chat_id)
                        uname = getattr(chat_info, "username", None)
                    except Exception:
                        uname = None
                    if uname:
                        link = f"https://t.me/{uname}/{mid}"
                    else:
                        cid = str(tg_chat_id)
                        if cid.startswith("-100"):
                            link = f"https://t.me/c/{cid[4:]}/{mid}"
                if link:
                    pl["result_link"] = link
            post.payload = pl
            await session.commit()
        except Exception:
            pass

    async def _pin_if_needed(self, tg_chat_id: int, ids: list[int], pl: dict) -> None:
        try:
            if bool(pl.get("pin_on", False)) and ids:
                with suppress(Exception):
                    await self.posting.bot.pin_chat_message(
                        chat_id=tg_chat_id, message_id=int(list(ids)[-1])
                    )
        except Exception:
            pass

    async def _forward_if_needed(
        self, session: AsyncSession, tg_chat_id: int, ids: list[int], pl: dict
    ) -> None:
        try:
            fw: list[int] = []
            try:
                fw = list(pl.get("forward_to") or [])
            except Exception:
                fw = []
            if fw and ids:
                for t_cid in fw:
                    try:
                        ch2 = await session.get(Channel, int(t_cid))
                        if not ch2:
                            continue
                        target_chat_id = int(ch2.tg_chat_id)
                        for mid in list(ids):
                            with suppress(Exception):
                                await self.posting.bot.forward_message(
                                    chat_id=target_chat_id,
                                    from_chat_id=tg_chat_id,
                                    message_id=int(mid),
                                    disable_notification=bool(pl.get("silent", False)),
                                )
                    except Exception:
                        continue
        except Exception:
            pass

    async def _published_notice_open_callback(
        self,
        session: AsyncSession,
        post: PostTask,
        date_iso: str,
    ) -> str:
        # Compatibility default. PublicationAwareScheduler overrides this only after
        # canonical linkage is proven; historical/base scheduler behavior stays intact.
        return f"cp_open_post:{post.id}:{date_iso}"

    async def _notify_owner_published(
        self,
        session: AsyncSession,
        ch: Channel,
        post: PostTask,
        tg_chat_id: int,
        ids: list[int],
        pl: dict,
    ) -> None:
        try:
            if bool(pl.get("repeat_on", False)):
                return
            owner: Client | None = await session.get(Client, ch.owner_id)
            repo = ChannelSettingsRepo(session)
            st = await repo.get_by_channel_id(post.channel_id)
            tz_code = (st.filters or {}).get("tz") if (st and st.filters) else None
            chan_link = None
            try:
                chat_info2 = await self.posting.bot.get_chat(tg_chat_id)
                uname2 = getattr(chat_info2, "username", None)
                if uname2:
                    chan_link = f"https://t.me/{uname2}"
            except Exception:
                chan_link = None
            link = pl.get("result_link")
            if not link:
                try:
                    mid2 = int(list(ids)[-1]) if ids else None
                except Exception:
                    mid2 = None
                if mid2 is not None:
                    if chan_link and chan_link.startswith("https://t.me/"):
                        uname3 = chan_link.split("/")[-1]
                        link = f"https://t.me/{uname3}/{mid2}"
                    else:
                        cid = str(tg_chat_id)
                        if cid.startswith("-100"):
                            link = f"https://t.me/c/{cid[4:]}/{mid2}"
            from datetime import datetime as _dt

            utc_now = _dt.now(timezone.utc)
            local_dt = utc_now
            try:
                local_dt = utc_now.astimezone(ZoneInfo(tz_code)) if tz_code else utc_now
            except Exception:
                try:
                    from app.core.timezone import offset_minutes_from_tz as _off

                    local_dt = utc_now + timedelta(minutes=_off(tz_code))
                except Exception:
                    local_dt = utc_now
            date_line = f"📅 {local_dt.strftime('%d.%m.%Y')} • 🕔 {local_dt.strftime('%H:%M')} ({tz_code or 'UTC'})"
            uname = owner.username if owner and owner.username else None
            author = f"@{uname}" if uname else "—"
            chan_title = ch.title or str(tg_chat_id)
            title_esc = html.escape(chan_title)
            chan_line = (
                f'Канал: <a href="{chan_link}">{title_esc}</a> | Автор: {author}'
                if locals().get("chan_link")
                else f"Канал: {title_esc} | Автор: {author}"
            )
            delivered_line = f"👀 Доставлено: {len(ids)}/1"
            forwarded_header = "Переслано: 0"
            link_part = f"\n🔗 Ссылка на пост {link}" if locals().get("link") else ""
            text = (
                "✅ Пост успешно опубликован\n"
                f"{link_part}\n\n"
                f"{date_line}\n"
                f"{delivered_line}\n"
                f"{forwarded_header}\n\n"
                f"{chan_line}"
            )
            if owner:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

                pl_note = dict(post.payload or {})
                pl_note["notify_context"] = {
                    "type": "published_notice",
                    "post_id": post.id,
                    "date": local_dt.date().isoformat(),
                }
                post.payload = pl_note
                await session.commit()
                btn = InlineKeyboardButton(
                    text="Редактировать",
                    callback_data=await self._published_notice_open_callback(
                        session,
                        post,
                        local_dt.date().isoformat(),
                    ),
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[[btn]])
                await self.posting.bot.send_message(
                    chat_id=int(owner.tg_user_id),
                    text=text,
                    disable_web_page_preview=True,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
        except Exception:
            pass

    async def _schedule_next_repeat_if_needed(
        self, session: AsyncSession, post: PostTask, pl: dict
    ) -> None:
        """Freeze legacy repeat successor creation after the current occurrence."""
        if not bool(pl.get("repeat_on", False)):
            return
        payload = dict(pl)
        payload["repeat_on"] = False
        payload["legacy_repeat_continuation_frozen"] = True
        post.payload = payload
        await session.commit()
        logger.info(
            "Scheduler: legacy repeat continuation frozen post_id={} without successor",
            int(post.id),
        )
    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if self.session_factory is not None:
                    async with self.session_factory() as session:
                        now = datetime.now(timezone.utc)
                        res = await session.execute(
                            select(PostTask)
                            .where(
                                (PostTask.status == "pending")
                                & (PostTask.scheduled_at.is_not(None))
                                & (PostTask.scheduled_at <= now)
                            )
                            .order_by(PostTask.scheduled_at.asc())
                            .limit(10)
                        )
                        items = list(res.scalars().all())
                        # На старте: пропустим просроченные автоповторы и создадим ближайший будущий (однократно)
                        items = await self._boot_cleanup_repeats(session, items)
                        # Анти‑переполнение автоповторов при старте: если по одной группе > 30 pending — оставим только ближайший будущий, остальные отметим skipped, и выключим repeat_on у оставленного
                        await self._prevent_repeat_overflow(session, items)
                        # Пометим выбранные задания как processing одним UPDATE, чтобы не отобрать их повторно в этом цикле
                        await self._mark_processing(session, items)
                        # Обработаем выбранные задания
                        await self._process_items(session, items)
                        # Убрано: отдельный делетор крутится параллельно
                else:
                    now = datetime.now(timezone.utc)
                    res = await self.session.execute(
                        select(PostTask)
                        .where(
                            (PostTask.status == "pending")
                            & (PostTask.scheduled_at.is_not(None))
                            & (PostTask.scheduled_at <= now)
                        )
                        .order_by(PostTask.scheduled_at.asc())
                        .limit(10)
                    )
                    items = list(res.scalars().all())
                    # На старте: пропустим просроченные автоповторы (ветка с одной сессией, однократно)
                    items = await self._boot_cleanup_repeats(self.session, items)
                    # Анти‑переполнение автоповторов при старте (ветка с одной сессией)
                    await self._prevent_repeat_overflow(self.session, items)
                    # Пометим выбранные задания как processing одним UPDATE
                    await self._mark_processing(self.session, items)
                    await self._process_items(self.session, items)
                    # Убрано: отдельный делетор крутится параллельно
                if not items:
                    await asyncio.sleep(self.interval_seconds)
                    continue
            except Exception as loop_err:
                logger.exception(f"Scheduler loop error: {loop_err}")
                await asyncio.sleep(self.interval_seconds)

    async def _process_items(
        self, session: AsyncSession, items: list[PostTask]
    ) -> None:
        if not items:
            return
        for post in items:
            try:
                logger.info(
                    f"Scheduler: dispatch post id={post.id} channel={post.channel_id} at={post.scheduled_at}"
                )
                ch = await session.get(Channel, post.channel_id)
                if not ch:
                    raise RuntimeError("channel not found for PostTask")
                tg_chat_id = int(ch.tg_chat_id)
                pl = dict(post.payload or {})
                # Если бот перезапускался и это автоповтор: пропускаем просроченные слоты и планируем ближайший будущий
                if await self._skip_overdue_repeat_and_schedule_next(session, post, pl):
                    continue

                # Зафиксируем, что задача в обработке, чтобы не подобрать её повторно на следующей итерации
                try:
                    if getattr(post, "status", "pending") == "pending":
                        post.status = "processing"
                        await session.commit()
                except Exception:
                    pass
                try:
                    repo = ChannelSettingsRepo(session)
                    st = await repo.get_by_channel_id(post.channel_id)
                    autosign_text = (st.autosign or None) if st else None
                    pl = await self._apply_autosign_if_needed(pl, autosign_text)
                except Exception:
                    pass

                # Для автоповтора: если накопились дубликаты этой серии (после простоя) — оставим только один
                await self._dedupe_repeat_series(session, post, pl)

                # Передадим служебные флаги, чтобы отправитель при необходимости тоже запланировал автоудаление (дедуплицируем по факту)
                ids = await self.posting.send_now(
                    tg_chat_id,
                    dict(
                        pl,
                        _via_scheduler=True,
                        _post_task_id=int(post.id),
                        _also_schedule_autodelete=True,
                    ),
                )
                # Отправим лог админу о посте
                await self._send_admin_log_if_configured(tg_chat_id, ids, pl)
                if ids:
                    await self._persist_result_fields(
                        session, post, tg_chat_id, pl, ids
                    )
                    # Планирование автоудаления сразу после успешной отправки
                    try:
                        use0 = int(
                            pl.get("autodelete_effective_seconds")
                            or pl.get("autodelete_seconds")
                            or 0
                        )
                        logger.info(
                            f"Scheduler: autodel pre-check post_id={int(post.id)} use_sec={use0} ids={len(ids) if ids else 0}"
                        )
                    except Exception:
                        pass
                    await self._apply_autodelete(
                        session, post, tg_chat_id, pl, list(ids)
                    )
                    # Фолбэк: если таймер есть, ids есть, а autodelete_at не выставлен — поставим now+seconds
                    try:
                        sec_fb = int(pl.get("autodelete_seconds") or 0)
                        if (
                            sec_fb > 0
                            and (len(ids) > 0)
                            and not pl.get("autodelete_at")
                        ):
                            from datetime import (
                                datetime as _dt,
                                timezone as _tz,
                                timedelta as _td,
                            )

                            due_fb = _dt.now(_tz.utc) + _td(seconds=sec_fb)
                            pl["autodelete_at"] = due_fb.isoformat()
                            post.payload = pl
                            await session.commit()
                            logger.info(
                                f"Scheduler: autodelete fallback set post_id={int(post.id)} autodelete_at={pl['autodelete_at']}"
                            )
                    except Exception:
                        pass
                    # Закрепление при необходимости
                    await self._pin_if_needed(tg_chat_id, ids, pl)
                    # Форвард в дополнительные каналы при необходимости
                    await self._forward_if_needed(session, tg_chat_id, ids, pl)

                    # Уведомление владельцу о публикации (не шлём, если включён автоповтор)
                    await self._notify_owner_published(
                        session, ch, post, tg_chat_id, ids, pl
                    )

                    post.status = "done"
                    post.payload = pl
                    await session.commit()
                    # Если включён автоповтор — запланируем следующую публикацию
                    await self._schedule_next_repeat_if_needed(session, post, pl)
                else:
                    post.status = "failed"
                    post.error = "no message ids returned"
                    await session.commit()
                    continue
            except Exception as e:
                logger.exception(f"Scheduler: failed id={post.id}: {e}")
                post.status = "failed"
                post.error = str(e)
                await session.commit()

    async def _process_due_deletions(self, session: AsyncSession) -> None:
        """Удалить сообщения с истёкшим таймером (используется как fallback/при старте)."""
        now = datetime.now(timezone.utc)
        # Быстрый выход: если нет ничего с autodelete_at в ближайшем окне, не сканируем глубоко
        res = await session.execute(
            select(PostTask)
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
        items = list(res.scalars().all())
        if not items:
            return
        for post in items:
            pl = dict(post.payload or {})
            if pl.get("autodeleted"):
                continue
            ids = list(pl.get("result_ids") or [])
            if not ids:
                continue
            # Диагностика кандидатов на удаление
            try:
                sec_dbg = int(
                    pl.get("autodelete_effective_seconds")
                    or pl.get("autodelete_seconds")
                    or 0
                )
            except Exception:
                sec_dbg = 0
            has_ad_at = bool(pl.get("autodelete_at"))
            logger.info(
                f"Scheduler: due candidate id={int(post.id)} has_autodelete_at={has_ad_at} sec={sec_dbg} ids={len(ids)}"
            )
            # Используем autodelete_at, если он есть; иначе fallback к scheduled_at + seconds
            due_at = None
            try:
                ad_at_raw = pl.get("autodelete_at")
                if ad_at_raw:
                    from datetime import datetime as _dt

                    ad_str = str(ad_at_raw)
                    if ad_str.endswith("Z"):
                        ad_str = ad_str[:-1] + "+00:00"
                    due_at = _dt.fromisoformat(ad_str)
            except Exception:
                pass
            # Диагностика: если нет autodelete_at — сообщим, какой будет fallback
            if due_at is None:
                sec = int(
                    pl.get("autodelete_effective_seconds")
                    or pl.get("autodelete_seconds")
                    or 0
                )
                when = post.scheduled_at
                # лог убран ради снижения шума
            if due_at is None:
                sec = int(
                    pl.get("autodelete_effective_seconds")
                    or pl.get("autodelete_seconds")
                    or 0
                )
                when = post.scheduled_at
                if not when or sec <= 0:
                    continue
                when_aware = self._as_utc(when)
                from datetime import timedelta as _td

                due_at = when_aware + _td(seconds=sec)
            logger.info(
                f"Scheduler: due deletion check id={int(post.id)} due_at={due_at.isoformat()} now={now.isoformat()} ids={len(ids)}"
            )
            if due_at > now:
                continue
            ch = await session.get(Channel, post.channel_id)
            if not ch:
                continue
            chat_id = int(ch.tg_chat_id)

            try:
                mixed_views = int(pl.get("autodelete_views") or 0)
            except (TypeError, ValueError):
                mixed_views = 0
            if sec_dbg > 0 and mixed_views > 0:
                mixed_post_id = int(post.id)
                mixed_chat_id = int(chat_id)
                mixed_message_ids = tuple(ids)

                # End the read-only scanner transaction before the durable
                # reservation/provider path. AsyncSessionLocal keeps loaded
                # candidates usable across commit (expire_on_commit=False).
                await session.commit()
                mixed_result = await self._legacy_time_views_delete_ledger.delete_once(
                    bot=self.posting.bot,
                    post_task_id=mixed_post_id,
                    chat_id=mixed_chat_id,
                    message_ids=mixed_message_ids,
                )
                if mixed_result.succeeded:
                    logger.info(
                        f"Scheduler: durable mixed autodelete success id={mixed_post_id} deleted={len(mixed_message_ids)}"
                    )
                continue

            success = 0
            not_found = 0
            cannot_delete = 0
            for mid in ids:
                try:
                    await self.posting.bot.delete_message(
                        chat_id=chat_id, message_id=int(mid)
                    )
                    success += 1
                except Exception as e:
                    logger.warning(
                        f"Scheduler: delete failed chat_id={chat_id} msg_id={int(mid)} err={e}"
                    )
                    msg = str(e).lower()
                    if ("message to delete not found" in msg) or (
                        "message_id_invalid" in msg
                    ):
                        not_found += 1
                    elif ("can't be deleted" in msg) or (
                        "message can't be deleted" in msg
                    ):
                        cannot_delete += 1
            # отметить и отчёт только при успешном удалении
            # Завершаем ретраи, если удалили хотя бы одно сообщение,
            # либо все сообщения недоступны (not found) или запрещены к удалению (например, старые)
            if (success > 0) or (not_found == len(ids)) or (cannot_delete == len(ids)):
                pl["autodeleted"] = True
                pl["autodeleted_at"] = now.isoformat()
                post.payload = pl
                await session.commit()
                if success > 0:
                    logger.info(
                        f"Scheduler: autodeleted success id={int(post.id)} deleted={success}/{len(ids)}"
                    )
            else:
                logger.warning(
                    f"Scheduler: autodelete failed (no messages deleted) for post id={int(post.id)}"
                )
                # если ни одно сообщение не удалено — оставим пост без отметки, чтобы повторить попытку на следующем цикле
                # Дополнительно: если ошибка была 'can't be deleted' хотя бы один раз — выставим autodeleted, чтобы не зацикливаться
                if cannot_delete > 0:
                    pl["autodeleted"] = True
                    pl["autodeleted_at"] = now.isoformat()
                    post.payload = pl
                    await session.commit()
                if pl.get("autodelete_report"):
                    owner: Client | None = await session.get(Client, ch.owner_id)
                    if owner and getattr(owner, "tg_user_id", None):
                        # Соберём расширенный отчёт
                        try:
                            # Заголовок поста
                            if pl.get("type") == "text":
                                title_line = (
                                    (pl.get("text") or "").strip().splitlines()[0]
                                )
                            else:
                                cap0 = pl.get("caption") or pl.get("text") or ""
                                title_line = cap0.strip().splitlines()[0] or ""
                            title_line = (title_line or "Без названия")[:60]
                            # Локальная дата публикации
                            months = [
                                "января",
                                "февраля",
                                "марта",
                                "апреля",
                                "мая",
                                "июня",
                                "июля",
                                "августа",
                                "сентября",
                                "октября",
                                "ноября",
                                "декабря",
                            ]
                            weekdays = [
                                "понедельник",
                                "вторник",
                                "среда",
                                "четверг",
                                "пятница",
                                "суббота",
                                "воскресенье",
                            ]
                            local_str = "—"
                            try:
                                repo = ChannelSettingsRepo(session)
                                st2 = await repo.get_by_channel_id(int(post.channel_id))
                                tz_code2 = (
                                    (st2.filters or {}).get("tz")
                                    if (st2 and st2.filters)
                                    else None
                                )
                                sched = getattr(post, "scheduled_at", None)
                                if sched is not None:
                                    sched_aware = (
                                        sched
                                        if (getattr(sched, "tzinfo", None) is not None)
                                        else sched.replace(tzinfo=timezone.utc)
                                    )
                                    local_dt = sched_aware
                                    with suppress(Exception):
                                        local_dt = (
                                            sched_aware.astimezone(ZoneInfo(tz_code2))
                                            if tz_code2
                                            else sched_aware
                                        )
                                    local_str = f"{local_dt.day} {months[local_dt.month - 1]} {local_dt.year} {local_dt.strftime('%H:%M')} ({weekdays[local_dt.weekday()]})"
                            except Exception:
                                pass
                            chan_name = getattr(ch, "title", None) or str(
                                getattr(ch, "tg_chat_id", "")
                            )
                            # Параметры удаления
                            vv = int(pl.get("autodelete_views") or 0)
                            views_line2 = f"👁 Просмотры: {vv}" if vv > 0 else ""
                            lab_views = (
                                (
                                    f"{vv // 1000}к"
                                    if (vv >= 1000 and vv % 1000 == 0)
                                    else str(vv)
                                )
                                if vv > 0
                                else None
                            )
                            from app.bot.routers.post_editor import (
                                _format_duration_label as _lab,
                            )

                            secs2 = int(pl.get("autodelete_seconds") or 0)
                            timer_line = f"🗑 Таймер удаления: {_lab(secs2) if secs2 > 0 else 'нет'}"
                            link_val = pl.get("result_link")
                            link_part = f"\n{link_val}" if link_val else ""
                            chan_line = f"{chan_name}{((f' (👁 {lab_views})') if lab_views else '')}"
                            text2 = (
                                f"Отчёт об удалении поста «{title_line}», опубликованного {local_str} в каналах:\n\n"
                                f"{chan_line}\n\n"
                                f"{timer_line}\n"
                                f"{views_line2}"
                                f"{link_part}"
                            )
                            with suppress(Exception):
                                await self.posting.bot.send_message(
                                    chat_id=int(owner.tg_user_id),
                                    text=text2,
                                    disable_web_page_preview=True,
                                )
                        except Exception:
                            # Фолбэк: короткое сообщение
                            text = "🗑️ Пост удалён по таймеру"
                            link = pl.get("result_link")
                            if link:
                                text = f"{text}\n{link}"
                            with suppress(Exception):
                                await self.posting.bot.send_message(
                                    chat_id=int(owner.tg_user_id),
                                    text=text,
                                    disable_web_page_preview=True,
                                )
