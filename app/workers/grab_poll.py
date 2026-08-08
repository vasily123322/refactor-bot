import hashlib
from typing import Dict

from loguru import logger

from app.bot.bot_instance import bot as tg_bot
from app.core.db import AsyncSessionLocal
from app.core.runner import PollingLoop
from app.domain.models import Client
from app.repositories.ai_settings import AISourcesRepo, ChannelAISettingsRepo
from app.repositories.channels import ChannelsRepo
from app.repositories.settings import ChannelSettingsRepo
from app.services.ai_generation import AIGenerationService
from app.services.posting import PostingService
from app.userbot.client import app as userbot


class GrabPoller:
    def __init__(self, interval_seconds: int = 30):
        self.interval_seconds = interval_seconds
        self._loop = PollingLoop(
            interval_seconds=interval_seconds,
            on_tick=self._tick,
            name="grab_poller",
            jitter_seconds=0,
        )
        self._last_processed: Dict[int, int] = {}

    async def start(self) -> None:
        await self._loop.start()

    async def stop(self) -> None:
        await self._loop.stop()

    async def _tick(self) -> None:
        logger.trace("GrabPoller: tick (telegram sources check)")
        async with AsyncSessionLocal() as session:
            repo = AISourcesRepo(session)
            sources = await repo.list_all_telegram_enabled()

        logger.trace("GrabPoller: telegram sources count={}", len(sources))
        for source in sources:
            try:
                await self._process_source(source)
            except Exception:
                logger.exception(
                    "GrabPoller: source processing failed source_id={} channel_id={}",
                    getattr(source, "id", None),
                    getattr(source, "channel_id", None),
                )

    async def _process_source(self, source) -> None:
        source_id = int(source.id)
        value = (source.source_value or "").strip()
        if not value:
            return

        join_target = self._normalize_join_target(value)
        await self._try_join(join_target)

        message = await self._fetch_latest_message(join_target)
        if message is None:
            return

        message_id = int(message.id)
        last_id = self._last_processed.get(source_id)
        if last_id is None:
            self._last_processed[source_id] = message_id
            return
        if message_id <= last_id:
            return

        original_text = (
            getattr(message, "caption", None) or getattr(message, "text", None) or ""
        ).strip()
        if not original_text:
            self._last_processed[source_id] = message_id
            return

        mode_raw = (source.mode or "summary").lower().strip()
        force_custom = mode_raw == "custom"
        mode = "rewrite" if force_custom else mode_raw
        instruction = {
            "summary": "создай краткое саммари текста для поста в Telegram",
            "rewrite": "перепиши текст своими словами, сохранив смысл",
            "paraphrase": "перефразируй текст, сделай его более понятным",
        }.get(mode, "создай краткое саммари текста для поста в Telegram")

        async with AsyncSessionLocal() as session:
            ai_repo = ChannelAISettingsRepo(session)
            ai_settings = await ai_repo.get_or_create(int(source.channel_id))
            priority = self._prompt_priority(ai_settings, force_custom=force_custom)
            prompt_key = self._make_prompt_key(
                priority,
                mode,
                ai_settings.preset_id,
                ai_settings.custom_prompt,
                ai_settings.user_prompt_template,
                ai_settings.model,
            )
            user_id = await self._get_channel_owner_user_id(
                session, int(source.channel_id)
            )

            generation = AIGenerationService(session)
            result = await generation.run_pipeline(
                channel_id=int(source.channel_id),
                mode="improve",
                original_text=original_text,
                instruction=instruction,
                extra={"force_custom": force_custom},
                user_id=user_id,
                prompt_key=prompt_key,
            )

        if not result.get("success"):
            logger.warning(
                "GrabPoller: generation failed for channel_id={}: {}",
                source.channel_id,
                result.get("error"),
            )
            self._last_processed[source_id] = message_id
            return

        text_out = await self._append_citation(
            result.get("text") or "",
            join_target,
            message_id,
            bool(source.citation_enabled),
        )

        async with AsyncSessionLocal() as session:
            settings_repo = ChannelSettingsRepo(session)
            channel_settings = await settings_repo.get_by_channel_id(
                int(source.channel_id)
            )
            autosign_text = (
                channel_settings.autosign if channel_settings else None
            ) or None
            if autosign_text:
                autosign_text = self._format_autosign(autosign_text, source_id)
                base = text_out or ""
                combined = base + ("\n\n" if base else "") + autosign_text
                text_out = combined if len(combined) <= 4096 else combined[:4095] + "…"

            channel = await ChannelsRepo(session).get_by_id(int(source.channel_id))

        if not channel:
            self._last_processed[source_id] = message_id
            return

        target_chat_id = int(channel.tg_chat_id)
        if target_chat_id > 0:
            logger.warning(
                "GrabPoller: skip send, suspicious tg_chat_id={} for channel_id={}",
                target_chat_id,
                source.channel_id,
            )
            self._last_processed[source_id] = message_id
            return

        posting = PostingService(tg_bot, AsyncSessionLocal)
        ids = await posting.send_now(
            channel_id=target_chat_id,
            payload={"type": "text", "text": text_out, "silent": True},
        )
        if ids:
            logger.info(
                "GrabPoller: posted to target_tg={} msg_ids={}", target_chat_id, ids
            )
        self._last_processed[source_id] = message_id

    @staticmethod
    def _prompt_priority(ai_settings, *, force_custom: bool) -> str:
        if force_custom and (ai_settings.custom_prompt or "").strip():
            return "custom"
        if ai_settings.preset_id:
            return "preset"
        if (ai_settings.custom_prompt or "").strip():
            return "custom"
        return "default"

    @staticmethod
    def _make_prompt_key(
        priority: str,
        mode: str,
        preset_id,
        custom_system: str | None,
        custom_user: str | None,
        model_name: str | None,
    ) -> str:
        raw = (
            f"{priority}|{mode}|{preset_id or ''}|{(custom_system or '').strip()}|"
            f"{(custom_user or '').strip()}|{(model_name or '').strip()}"
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    async def _get_channel_owner_user_id(session, channel_id: int) -> int | None:
        channel = await ChannelsRepo(session).get_by_id(channel_id)
        if not channel or not channel.owner_id:
            return None
        client = await session.get(Client, int(channel.owner_id))
        if not client or not client.tg_user_id:
            return None
        return int(client.tg_user_id)

    @staticmethod
    def _format_autosign(autosign_text: str, source_id: int) -> str:
        try:
            from app.bot.routers.utils.text_utils import (
                convert_message_entities_to_markdown,
            )

            return convert_message_entities_to_markdown(autosign_text, None)
        except Exception as exc:
            logger.warning(
                "GrabPoller: autosign formatting failed source_id={}: {!r}",
                source_id,
                exc,
            )
            return autosign_text

    @staticmethod
    def _normalize_join_target(value: str) -> str:
        join_target = value
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
        return join_target

    async def _try_join(self, join_target: str) -> None:
        try:
            logger.trace("GrabPoller: try join_chat target={}", join_target)
            await userbot.join_chat(join_target)
            logger.trace("GrabPoller: join_chat ok target={}", join_target)
        except Exception as exc:
            logger.trace(
                "GrabPoller: join_chat skipped/failed target={}: {!r}",
                join_target,
                exc,
            )

    async def _fetch_latest_message(self, join_target: str):
        try:
            async for message in userbot.get_chat_history(join_target, limit=1):
                return message
        except Exception as exc:
            logger.trace(
                "GrabPoller: get_chat_history failed target={}: {!r}",
                join_target,
                exc,
            )
        return None

    async def _append_citation(
        self, text: str, join_target: str, msg_id: int, enabled: bool
    ) -> str:
        if not enabled:
            return text
        try:
            chat = await userbot.get_chat(join_target)
            username = getattr(chat, "username", None)
            if username:
                link = f"https://t.me/{username}/{msg_id}"
            else:
                sid = str(int(chat.id))
                if sid.startswith("-100"):
                    sid = sid[4:]
                link = f"https://t.me/c/{sid}/{msg_id}"
            source_label = f"@{username}" if username else ""
            return f"{text}\n\nИсточник: {source_label}\n{link}"
        except Exception as exc:
            logger.trace(
                "GrabPoller: citation lookup failed target={}: {!r}",
                join_target,
                exc,
            )
            return text
