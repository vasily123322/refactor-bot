from loguru import logger
from pyrogram import filters
from app.userbot.client import app
from app.core.db import AsyncSessionLocal
from app.repositories.ai_settings import AISourcesRepo
from app.services.ai_generation import AIGenerationService
from app.services.posting import PostingService
from app.bot.bot_instance import bot as tg_bot
import hashlib


# Отметим загрузку модуля слушателя при импортe
logger.info("listener: module loaded")


@app.on_message(filters.command(["ping"]))
async def _ping_handler(_, message):
    try:
        await message.reply_text("pong")
    except Exception as e:
        logger.warning(f"userbot listener ping error: {e}")


@app.on_message(filters.channel)
async def _on_channel_post(client, message):
    """Реагируем на новые посты в каналах и прокидываем их в целевые каналы
    согласно таблице ai_sources (тип telegram).
    """
    try:
        logger.info("listener: got channel event")
        chat = getattr(message, "chat", None)
        if chat is None:
            return
        chat_id = int(getattr(chat, "id", 0))
        if not chat_id:
            return
        username = getattr(chat, "username", None)
        uname = f"@{username}" if username else None
        logger.info(
            f"listener: channel post chat_id={chat_id} uname={uname} has_text={bool(getattr(message, 'text', None)) or bool(getattr(message, 'caption', None))}"
        )

        # Найдём подходящие источники (включённые) по username/ID
        async with AsyncSessionLocal() as session:
            repo = AISourcesRepo(session)
            sources = await repo.list_telegram_matches(chat_id=chat_id, username=uname)
        if not sources:
            logger.info("listener: no matching AISource found for this channel")
            return
        logger.info(f"listener: matched {len(sources)} ai_sources")

        # Подготовим текст исходного поста
        orig_text = (
            getattr(message, "caption", None) or getattr(message, "text", None) or ""
        ).strip()
        if not orig_text:
            logger.info("listener: skip channel post without text/caption")
            return

        # Для каждого источника выполним генерацию по режиму и отправим в целевой канал
        posting = PostingService(tg_bot, AsyncSessionLocal)
        for s in sources:
            mode_raw = (s.mode or "summary").lower().strip()
            force_custom = False
            mode = mode_raw
            if mode_raw == "custom":
                force_custom = True
                mode = "rewrite"
            instr_map = {
                "summary": "создай краткое саммари текста для поста в Telegram",
                "rewrite": "перепиши текст своими словами, сохранив смысл",
                "paraphrase": "перефразируй текст, сделай его более понятным",
            }
            instruction = instr_map.get(mode, instr_map["summary"])

            async with AsyncSessionLocal() as session:
                gen = AIGenerationService(session)
                # Получим AI-настройки для вычисления prompt_key и приоритета
                from app.repositories.ai_settings import ChannelAISettingsRepo

                ai_repo = ChannelAISettingsRepo(session)
                ai_set = await ai_repo.get_or_create(int(s.channel_id))
                # Вычислим приоритет
                if force_custom and (ai_set.custom_prompt or "").strip():
                    priority = "custom"
                elif ai_set.preset_id:
                    priority = "preset"
                elif (ai_set.custom_prompt or "").strip():
                    priority = "custom"
                else:
                    priority = "default"

                # Построим prompt_key
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
                # Вычислим user_id владельца канала (tg_user_id)
                user_id_val = None
                try:
                    from app.repositories.channels import ChannelsRepo
                    from app.domain.models import Client

                    ch_repo2 = ChannelsRepo(session)
                    ch2 = await ch_repo2.get_by_id(int(s.channel_id))
                    if ch2 and ch2.owner_id:
                        client = await session.get(Client, int(ch2.owner_id))
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
                    f"listener: generation failed for channel_id={s.channel_id}: {result.get('error')}"
                )
                continue

            text_out = result.get("text") or ""
            # Добавим цитирование источника при необходимости
            if getattr(s, "citation_enabled", False):
                link = None
                if username:
                    try:
                        link = f"https://t.me/{username}/{message.id}"
                    except Exception:
                        link = None
                else:
                    # формат t.me/c/<id>/<msg> для приватных каналов (-100 prefix убираем)
                    try:
                        sid = str(chat_id)
                        if sid.startswith("-100"):
                            sid = sid[4:]
                        link = f"https://t.me/c/{sid}/{message.id}"
                    except Exception:
                        link = None
                cite_lines = []
                if uname:
                    cite_lines.append(f"Источник: {uname}")
                if link:
                    cite_lines.append(link)
                if cite_lines:
                    text_out = f"{text_out}\n\n" + "\n".join(cite_lines)

            # Применим автоподпись, если задана для целевого канала
            try:
                async with AsyncSessionLocal() as s_set:
                    from app.repositories.settings import ChannelSettingsRepo

                    set_repo = ChannelSettingsRepo(s_set)
                    st = await set_repo.get_by_channel_id(int(s.channel_id))
                    autosign_text = (st.autosign or None) if st else None
                    if autosign_text:
                        # Автоконвертация text_link-entities в Markdown якоря для автоподписи
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
            except Exception:
                pass

            # Разрешим внутренний channel_id -> tg_chat_id
            target_chat_id = None
            try:
                async with AsyncSessionLocal() as s_ch:
                    from app.repositories.channels import ChannelsRepo

                    ch_repo = ChannelsRepo(s_ch)
                    ch = await ch_repo.get_by_id(int(s.channel_id))
                    if ch:
                        target_chat_id = int(ch.tg_chat_id)
            except Exception as e:
                logger.warning(
                    f"listener: resolve tg_chat_id failed for channel_id={s.channel_id}: {e}"
                )
            if not target_chat_id:
                logger.warning(
                    f"listener: skip post, target channel not found for channel_id={s.channel_id}"
                )
                continue
            payload = {"type": "text", "text": text_out, "silent": True}
            ids = await posting.send_now(channel_id=target_chat_id, payload=payload)
            if ids:
                logger.info(
                    f"listener: posted to target_tg={target_chat_id} msg_ids={ids}"
                )
    except Exception as e:
        logger.exception(f"userbot listener error: {e}")
