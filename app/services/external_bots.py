import asyncio
import contextlib
from typing import Dict
from loguru import logger
from aiogram import Dispatcher, Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from app.core.db import AsyncSessionLocal
from app.core.fsm_storage import build_fsm_storage
from app.repositories.external_bots import ExternalBotsRepo
from app.bot.routers.join_requests_ext import build_router_for_external_bot


class ExternalBotsManager:
    def __init__(self):
        self._tasks: Dict[int, asyncio.Task] = {}
        self._bots: Dict[int, Bot] = {}
        self._dps: Dict[int, Dispatcher] = {}
        self._approver_tasks: Dict[int, asyncio.Task] = {}
        self._restarts: Dict[int, int] = {}

    async def start_all(self) -> None:
        async with AsyncSessionLocal() as session:
            repo = ExternalBotsRepo(session)
            items = await repo.get_active()
        for ext in items:
            if not ext.token:
                continue
            await self.start_one(ext.id, ext.token)

    async def start_one(self, ext_id: int, token: str) -> None:
        if ext_id in self._tasks:
            return
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=build_fsm_storage())
        dp.include_router(build_router_for_external_bot(ext_id))
        task = asyncio.create_task(self._poll(dp, bot, ext_id))
        self._bots[ext_id] = bot
        self._dps[ext_id] = dp
        self._tasks[ext_id] = task
        self._approver_tasks[ext_id] = asyncio.create_task(self._approver_loop(ext_id))
        logger.info(f"External bot started ext_id={ext_id}")

    async def stop_all(self) -> None:
        for ext_id in list(self._tasks.keys()):
            await self.stop_one(ext_id)

    async def stop_one(self, ext_id: int) -> None:
        task = self._tasks.pop(ext_id, None)
        self._dps.pop(ext_id, None)
        bot = self._bots.pop(ext_id, None)
        apr = self._approver_tasks.pop(ext_id, None)
        if task:
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        if apr:
            apr.cancel()
            with contextlib.suppress(Exception):
                await apr
        if bot:
            with contextlib.suppress(Exception):
                await bot.session.close()
        logger.info(f"External bot stopped ext_id={ext_id}")
        self._restarts.pop(ext_id, None)

    async def restart_one(self, ext_id: int) -> None:
        """Перезапустить один внешний бот (если активен)."""
        async with AsyncSessionLocal() as session:
            repo = ExternalBotsRepo(session)
            ext = await repo.get_by_id(ext_id)
            if not (ext and ext.is_active and ext.token):
                return
        await self.stop_one(ext_id)
        await self.start_one(ext_id, ext.token)

    def is_running(self, ext_id: int) -> bool:
        return ext_id in self._tasks

    async def _poll(self, dp: Dispatcher, bot: Bot, ext_id: int) -> None:
        try:
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                polling_timeout=50,
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            from aiogram.exceptions import TelegramNetworkError

            if not isinstance(e, TelegramNetworkError):
                logger.error(f"External bot polling failed ext_id={ext_id}: {e}")
            try:
                async with AsyncSessionLocal() as session:
                    repo = ExternalBotsRepo(session)
                    ext = await repo.get_by_id(ext_id)
                if not (ext and ext.is_active and ext.token):
                    return
                delay = min(60, 5 * (self._restarts.get(ext_id, 0) + 1))
                self._restarts[ext_id] = self._restarts.get(ext_id, 0) + 1
                await asyncio.sleep(delay)
                await self.restart_one(ext_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("External bot auto-restart failed ext_id={}", ext_id)

    async def _approve_pending_item(
        self,
        ext_id: int,
        channel_id: int,
        jr,
        *,
        ch_repo,
        subs_repo,
        jr_repo,
        session,
    ) -> bool:
        """Approve one solved pending request and persist state only on success."""
        if (
            jr.challenge_type
            and jr.attempts_left is not None
            and int(jr.attempts_left) > 0
        ):
            return False

        ch = await ch_repo.get_by_id(channel_id)
        bot = self._bots.get(ext_id)
        if not ch or not bot:
            return False

        try:
            await bot.approve_chat_join_request(
                chat_id=int(ch.tg_chat_id), user_id=int(jr.user_id)
            )
        except Exception:
            logger.exception(
                "Delayed join approve failed ext_id={} channel_id={} user_id={}",
                ext_id,
                channel_id,
                jr.user_id,
            )
            return False

        try:
            await subs_repo.add(channel_id, jr.user_id, None, None)
            payload = dict(getattr(jr, "challenge_payload", {}) or {})
            utm = payload.get("utm")
            if utm:
                await subs_repo.add_tag(channel_id, jr.user_id, str(utm))
            await jr_repo.set_status(channel_id, jr.user_id, "approved")
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "Delayed join DB update failed after Telegram approval channel_id={} user_id={}",
                channel_id,
                jr.user_id,
            )
            return False
        return True

    async def _approver_loop(self, ext_id: int) -> None:
        from app.core.db import AsyncSessionLocal
        from app.repositories.join_requests import JoinRequestsRepo
        from app.repositories.channels import ChannelsRepo
        from app.repositories.subscribers import SubscribersRepo

        while True:
            try:
                await asyncio.sleep(30)
                async with AsyncSessionLocal() as session:
                    jr_repo = JoinRequestsRepo(session)
                    ch_repo = ChannelsRepo(session)
                    subs_repo = SubscribersRepo(session)
                    from sqlalchemy import select
                    from app.domain.models import ChannelBot

                    res = await session.execute(
                        select(ChannelBot).where(
                            ChannelBot.external_bot_id == ext_id, ChannelBot.mode == 1
                        )
                    )
                    channels = list(res.scalars().all())
                    for cb in channels:
                        cid = int(cb.channel_id)
                        items = await jr_repo.list_pending(cid, limit=10)
                        for jr in items:
                            await self._approve_pending_item(
                                ext_id,
                                cid,
                                jr,
                                ch_repo=ch_repo,
                                subs_repo=subs_repo,
                                jr_repo=jr_repo,
                                session=session,
                            )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("External bot delayed approver tick failed ext_id={}", ext_id)
