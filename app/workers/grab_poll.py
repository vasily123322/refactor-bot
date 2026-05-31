from loguru import logger
from typing import Dict
from app.core.runner import PollingLoop
from app.core.db import AsyncSessionLocal
from app.repositories.ai_settings import AISourcesRepo
from app.userbot.client import app as userbot
from app.services.ai_generation import AIGenerationService
from app.services.posting import PostingService
from app.bot.bot_instance import bot as tg_bot
import hashlib


class GrabPoller:
    def __init__(self, interval_seconds: int = 30):
        self.interval_seconds = interval_seconds
        self._loop = PollingLoop(
            interval_seconds=interval_seconds,
            on_tick=self._tick,
            name="grab_poller",
            jitter_seconds=0,
        )
        # Память о последних обработанных сообщениях источников (в пределах процесса)
        self._last_processed: Dict[int, int] = {}

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    async def _tick(self) -> None:
        try:
            logger.trace("GrabPoller: tick (telegram sources check)")
            # Периодически убеждаемся, что userbot подписан на все телеграм-источники
            async with AsyncSessionLocal() as session:
                repo = AISourcesRepo(session)
                sources = await repo.list_all_telegram_enabled()
            logger.trace(f"GrabPoller: telegram sources count={len(sources)}")
            for s in sources:
                val = (s.source_value or "").strip()
                if not val:
                    continue
                join_target = self._normalize_join_target(val)
                await self._try_join(join_target)

                # Получим последнее сообщение
                m = await self._fetch_latest_message(join_target)
                if m is None:
                    continue
                last_id = self._last_processed.get(int(s.id))
                # При первом запуске/рестарте игнорируем текущее последнее сообщение,
                # чтобы не публиковать старые посты до момента старта бота
                if last_id is None:
                    self._last_processed[int(s.id)] = int(m.id)
                    continue
                if last_id is not None and m.id <= last_id:
                    continue
                # Извлечём текст
                orig_text = (
                    getattr(m, "caption", None) or getattr(m, "text", None) or ""
                ).strip()
                if not orig_text:
                    self._last_processed[int(s.id)] = int(m.id)
                    continue
                    # Генерация, настройка и публикация — в рамках одной краткоживущей сессии
                    mode_raw = (s.mode or "summary").lower().strip()
                    force_custom = False
                    mode = mode_raw
                    if mode_raw == "custom":
                        force_custom = True
                        mode = "rewrite"
                    instruction = {
                        "summary": "создай краткое саммари текста для поста в Telegram",
                        "rewrite": "перепиши текст своими словами, сохранив смысл",
                        "paraphrase": "перефразируй текст, сделай его более понятным",
                    }.get(mode, "создай краткое саммари текста для поста в Telegram")
                    async with AsyncSessionLocal() as s_all:
                        # AI-настройки и генерация
                        from app.repositories.ai_settings import ChannelAISettingsRepo

                        ai_repo = ChannelAISettingsRepo(s_all)
                        ai_set = await ai_repo.get_or_create(int(s.channel_id))
                        if force_custom and (ai_set.custom_prompt or "").strip():
                            priority = "custom"
                        elif ai_set.preset_id:
                            priority = "preset"
                        elif (ai_set.custom_prompt or "").strip():
                            priority = "custom"
                        else:
                            priority = "default"

                        def _make_prompt_key(
                            priority: str,
                            mode: str,
                            preset_id,
                            custom_system: str | None,
                            custom_user: str | None,
                            model_name: str | None,
                        ) -> str:
                            base = f"{priority}|{mode}|{preset_id or ''}|{(custom_system or '').strip()}|{(custom_user or '').strip()}|{(model_name or '').strip()}"
                            return hashlib.sha1(base.encode("utf-8")).hexdigest()

                        prompt_key = _make_prompt_key(
                            priority,
                            mode,
                            ai_set.preset_id,
                            ai_set.custom_prompt,
                            ai_set.user_prompt_template,
                            ai_set.model,
                        )
                        gen = AIGenerationService(s_all)
                        # user_id владельца канала
                        user_id_val = None
                        try:
                            from app.repositories.channels import ChannelsRepo
                            from app.domain.models import Client

                            ch_repo2 = ChannelsRepo(s_all)
                            ch2 = await ch_repo2.get_by_id(int(s.channel_id))
                            if ch2 and ch2.owner_id:
                                client = await s_all.get(Client, int(ch2.owner_id))
                                if client and client.tg_user_id:
                                    user_id_val = int(client.tg_user_id)
                        except Exception:
                            user_id_val = None
                    result = await gen.run_pipeline(
                        channel_id=int(s.channel_id),
                        mode="improve",
                        original_text=orig_text,
                        instruction=instruction,
                        extra={"force_custom": force_custom},
                        user_id=user_id_val,
                        prompt_key=prompt_key,
                    )
                    if not result.get("success"):
                        logger.warning(
                            f"GrabPoller: generation failed for channel_id={s.channel_id}: {result.get('error')}"
                        )
                        self._last_processed[int(s.id)] = int(m.id)
                        continue
                    text_out = result.get("text") or ""
                    # Цитирование источника
                    text_out = await self._append_citation(
                        text_out, join_target, m.id, s.citation_enabled
                    )

                    # Применим автоподпись, если задана для целевого канала
                    from app.repositories.settings import ChannelSettingsRepo

                    set_repo = ChannelSettingsRepo(s_all)
                    st2 = await set_repo.get_by_channel_id(int(s.channel_id))
                    autosign_text = (st2.autosign or None) if st2 else None
                    if autosign_text:
                        try:
                            from app.bot.routers.utils.text_utils import (
                                convert_message_entities_to_markdown,
                            )

                            autosign_text = convert_message_entities_to_markdown(
                                autosign_text, None
                            )
                        except Exception:
                            pass
                        base = text_out or ""
                        combined = base + ("\n\n" if base else "") + autosign_text
                        text_out = (
                            combined
                            if len(combined) <= 4096
                            else (combined[:4095] + "…")
                        )

                    # Публикация
                    try:
                        from app.repositories.channels import ChannelsRepo

                        ch_repo = ChannelsRepo(s_all)
                        ch = await ch_repo.get_by_id(int(s.channel_id))
                        if not ch:
                            self._last_processed[int(s.id)] = int(m.id)
                            continue
                        target_chat_id = int(ch.tg_chat_id)
                        if target_chat_id > 0:
                            logger.warning(
                                f"GrabPoller: skip send, suspicious tg_chat_id={target_chat_id} for channel_id={s.channel_id}"
                            )
                            self._last_processed[int(s.id)] = int(m.id)
                            continue
                        posting = PostingService(tg_bot, AsyncSessionLocal)
                        ids = await posting.send_now(
                            channel_id=target_chat_id,
                            payload={"type": "text", "text": text_out, "silent": True},
                        )
                        if ids:
                            logger.info(
                                f"GrabPoller: posted to target_tg={target_chat_id} msg_ids={ids}"
                            )
                        self._last_processed[int(s.id)] = int(m.id)
                    except Exception as e:
                        logger.exception(
                            f"GrabPoller: history/process error for source_id={s.id}: {e}"
                        )
                # end for each source
        except Exception as e:
            logger.warning(f"GrabPoller loop error: {e}")

    def _normalize_join_target(self, val: str) -> str:
        join_target = val
        try:
            if join_target.startswith("http"):
                path = (
                    join_target.split("t.me/", 1)[1]
                    if "t.me/" in join_target
                    else join_target
                )
                path = path.strip("/")
                if not (path.startswith("+") or path.startswith("joinchat/")):
                    join_target = path.split("/", 1)[0]
            elif join_target.startswith("@"):
                join_target = join_target[1:]
        except Exception:
            pass
        return join_target

    async def _try_join(self, join_target: str) -> None:
        try:
            logger.trace(f"GrabPoller: try join_chat target={join_target}")
            await userbot.join_chat(join_target)
            logger.trace(f"GrabPoller: join_chat ok target={join_target}")
        except Exception:
            logger.trace("GrabPoller: join_chat skipped/failed")

    async def _fetch_latest_message(self, join_target: str):
        m = None
        try:
            ait = userbot.get_chat_history(join_target, limit=1)
            async for msg in ait:
                m = msg
                break
        except Exception:
            logger.trace("GrabPoller: get_chat_history failed")
        return m

    async def _append_citation(
        self, text: str, join_target: str, msg_id: int, enabled: bool
    ) -> str:
        if not enabled:
            return text
        try:
            chat = await userbot.get_chat(join_target)
            uname = getattr(chat, "username", None)
            if uname:
                link = f"https://t.me/{uname}/{msg_id}"
            else:
                sid = str(int(chat.id))
                if sid.startswith("-100"):
                    sid = sid[4:]
                link = f"https://t.me/c/{sid}/{msg_id}"
            return f"{text}\n\nИсточник: {('@' + uname) if uname else ''}\n{link}"
        except Exception:
            return text
