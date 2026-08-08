from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone, timedelta
from aiogram import Bot
from aiogram.types import ChatJoinRequest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select as _select

from app.domain.models import JoinRequest
from app.repositories.channels import ChannelsRepo
from app.repositories.external_bots import ChannelBotsRepo
from app.repositories.subscribers import SubscribersRepo


class JoinRequestsService:
    def __init__(self, bot: Bot, session: AsyncSession):
        self.bot = bot
        self.session = session

    @staticmethod
    def _build_challenge_payload(require_mode: int) -> dict | None:
        if int(require_mode) != 2:
            return None
        import random

        a = random.randint(2, 9)
        b = random.randint(2, 9)
        return {"a": a, "b": b, "answer": str(a + b)}

    async def _write_modlog(
        self, channel_id: int, action: str, user_id: int, meta: dict | None = None
    ) -> None:
        try:
            from app.repositories.modlog import ModLogRepo

            ml = ModLogRepo(self.session)
            await ml.write(channel_id, action, user_id, None, meta or {})
        except Exception:
            pass

    async def handle_join_request(
        self, external_bot_id: int, event: ChatJoinRequest
    ) -> None:
        chat_id = int(getattr(event.chat, "id", 0))
        user = getattr(event, "from_user", None)
        if not chat_id or not user:
            return
        user_id = int(getattr(user, "id", 0))
        username = getattr(user, "username", None)
        full_name = " ".join(
            [
                p
                for p in [
                    getattr(user, "first_name", None),
                    getattr(user, "last_name", None),
                ]
                if p
            ]
        )

        ch_repo = ChannelsRepo(self.session)
        cb_repo = ChannelBotsRepo(self.session)
        SubscribersRepo(self.session)

        ch = await ch_repo.get_by_chat_id(chat_id)
        if not ch:
            return
        cb = await cb_repo.get_by_channel_id(ch.id)
        if not cb or int(cb.external_bot_id) != int(external_bot_id):
            return

        mode = int(getattr(cb, "mode", 0))
        meta = dict(getattr(cb, "meta", {}) or {})
        require_mode = int(
            meta.get("require_dm_mode", 0)
        )  # 0 off, 1 simple, 2 captcha, 3 keyword
        filters = dict(meta.get("request_filters", {}) or {})
        wl_u = [str(x).lower() for x in filters.get("whitelist_usernames", []) or []]
        bl_u = [str(x).lower() for x in filters.get("blacklist_usernames", []) or []]
        wl_i = [int(x) for x in filters.get("whitelist_ids", []) or []]
        bl_i = [int(x) for x in filters.get("blacklist_ids", []) or []]
        stopw = [str(x).lower() for x in filters.get("stop_words", []) or []]

        uname = (username or "").lower()
        fullname = (full_name or "").lower()

        # whitelist
        if (wl_u or wl_i) and not ((uname and uname in wl_u) or (user_id in wl_i)):
            await self.session.add(
                JoinRequest(channel_id=ch.id, user_id=user_id, status="rejected")
            )
            await self.session.commit()
            await self._write_modlog(
                ch.id, "reject", user_id, {"reason": "whitelist_miss"}
            )
            return

        # blacklist
        if (uname and uname in bl_u) or (user_id in bl_i):
            await self.session.add(
                JoinRequest(channel_id=ch.id, user_id=user_id, status="rejected")
            )
            await self.session.commit()
            await self._write_modlog(ch.id, "reject", user_id, {"reason": "blacklist"})
            return

        # stop words
        if any(sw in fullname for sw in stopw):
            await self.session.add(
                JoinRequest(channel_id=ch.id, user_id=user_id, status="rejected")
            )
            await self.session.commit()
            await self._write_modlog(ch.id, "reject", user_id, {"reason": "stop_word"})
            return

        # Mode 0 (auto) with optional DM requirement
        if mode == 0:
            dm_ok = True
            if require_mode != 0:
                try:
                    await event.bot.send_chat_action(user_id, "typing")
                except Exception:
                    dm_ok = False
            if not dm_ok:
                payload = {}
                if require_mode == 2:
                    import random

                    a = random.randint(2, 9)
                    b = random.randint(2, 9)
                    payload = {"a": a, "b": b, "answer": str(a + b)}
                invite = None
                try:
                    invite = getattr(
                        getattr(event, "invite_link", None), "invite_link", None
                    )
                except Exception:
                    invite = None
                jr = JoinRequest(
                    channel_id=ch.id,
                    user_id=user_id,
                    status="pending",
                    challenge_type=(
                        "captcha"
                        if require_mode == 2
                        else ("keyword" if require_mode == 3 else "simple")
                    ),
                    challenge_payload=payload if require_mode == 2 else None,
                    expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)),
                    attempts_left=3,
                    invite_link=invite,
                )
                self.session.add(jr)
                await self.session.commit()
                return

            # DM is OK and challenge required: create pending and send DM
            if require_mode != 0:
                invite = None
                try:
                    invite = getattr(
                        getattr(event, "invite_link", None), "invite_link", None
                    )
                except Exception:
                    invite = None
                jr = JoinRequest(
                    channel_id=ch.id,
                    user_id=user_id,
                    status="pending",
                    challenge_type=(
                        "captcha"
                        if require_mode == 2
                        else ("keyword" if require_mode == 3 else "simple")
                    ),
                    challenge_payload=self._build_challenge_payload(require_mode),
                    expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)),
                    attempts_left=3,
                    invite_link=invite,
                )
                self.session.add(jr)
                await self.session.commit()

                cfg = dict(meta.get("require_dm_config", {}) or {})
                if require_mode == 1:
                    text = cfg.get("simple_text") or "Подтвердите, что вы человек"
                    label = cfg.get("simple_button") or "Я человек"
                    utm = str(cfg.get("simple_tag") or "").strip()
                    res2 = await self.session.execute(
                        _select(JoinRequest).where(
                            JoinRequest.channel_id == ch.id,
                            JoinRequest.user_id == user_id,
                            JoinRequest.status == "pending",
                        )
                    )
                    jr_simple = res2.scalars().first()
                    if jr_simple is not None and utm:
                        pp2 = dict(getattr(jr_simple, "challenge_payload", {}) or {})
                        pp2["utm"] = utm
                        jr_simple.challenge_payload = pp2
                        await self.session.commit()
                    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

                    kb = ReplyKeyboardMarkup(
                        keyboard=[[KeyboardButton(text=label)]],
                        resize_keyboard=True,
                        one_time_keyboard=True,
                        is_persistent=False,
                    )
                    with suppress(Exception):
                        await event.bot.send_message(user_id, text, reply_markup=kb)
                elif require_mode == 2:
                    res = await self.session.execute(
                        _select(JoinRequest).where(
                            JoinRequest.channel_id == ch.id,
                            JoinRequest.user_id == user_id,
                            JoinRequest.status == "pending",
                        )
                    )
                    jr_obj = res.scalars().first()
                    pp = dict(getattr(jr_obj, "challenge_payload", {}) or {})
                    a = pp.get("a")
                    b = pp.get("b")
                    text = (
                        cfg.get("captcha_prompt") or "Решите пример: {a} + {b} = ?"
                    ).format(a=a, b=b)
                    with suppress(Exception):
                        await event.bot.send_message(user_id, text)
                elif require_mode == 3:
                    word = cfg.get("keyword_word") or "START"
                    text = (cfg.get("keyword_prompt") or "Отправьте слово: {w}").format(
                        w=word
                    )
                    with suppress(Exception):
                        await event.bot.send_message(user_id, text)
                return

            with suppress(Exception):
                await event.bot.approve_chat_join_request(
                    chat_id=chat_id, user_id=user_id
                )
            await self._write_modlog(ch.id, "approve", user_id, {"mode": "auto"})
            from app.services.subscribers import save_subscriber_preference

            with suppress(Exception):
                inv = None
                try:
                    inv = getattr(
                        getattr(event, "invite_link", None), "invite_link", None
                    )
                except Exception:
                    inv = None
                await save_subscriber_preference(
                    self.session,
                    channel_id=ch.id,
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                    tag=(f"inv:{inv}" if inv else None),
                )
            if getattr(cb, "welcome_text", None):
                with suppress(Exception):
                    await event.bot.send_message(user_id, cb.welcome_text)  # type: ignore[arg-type]
            return

        # Mode 1 or 2
        pp = {}
        ctype = "simple"
        if require_mode == 2:
            import random

            a = random.randint(2, 9)
            b = random.randint(2, 9)
            pp = {"a": a, "b": b, "answer": str(a + b)}
            ctype = "captcha"
        elif require_mode == 3:
            ctype = "keyword"
        jr = JoinRequest(
            channel_id=ch.id,
            user_id=user_id,
            status="pending",
            challenge_type=ctype,
            challenge_payload=pp if require_mode == 2 else None,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)),
            attempts_left=3,
        )
        self.session.add(jr)
        await self.session.commit()

        # Anti-raid throttle
        if bool(meta.get("anti_raid_enabled", False)):
            thr = int(meta.get("anti_raid_threshold", 30))
            res = await self.session.execute(
                _select(JoinRequest).where(
                    JoinRequest.channel_id == ch.id,
                    JoinRequest.status == "pending",
                    JoinRequest.created_at
                    > (datetime.now(timezone.utc) - timedelta(minutes=1)),
                )
            )
            cnt = len(list(res.scalars().all()))
            if cnt >= thr and int(getattr(cb, "mode", 0)) != 2:
                cb.mode = 2
                await self.session.commit()
