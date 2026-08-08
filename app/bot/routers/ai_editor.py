from __future__ import annotations

from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from app.bot.bot_instance import bot as tg_bot
from app.bot.fsm.states import PostFSM
from app.core.callbacks import CB
from app.core.db import AsyncSessionLocal

# Preview helpers stay in main during the first extraction step. main is imported
# before this module by routers/__init__.py, so this does not create a cycle.
from app.bot.routers.main import (
    _build_preview_kb,
    _build_pro_upsell_kb,
    _log_ai_limit_to_admin,
    _safe_edit_reply_markup,
    _send_preview_message,
)


router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


# --- Обработчики кнопки ИИ в редакторе постов ---


@router.callback_query(F.data == CB.POST_AI)
async def cb_post_ai(callback: CallbackQuery, state: FSMContext):
    """Открыть меню ИИ для генерации/улучшения контента поста."""
    data = await state.get_data()
    data.get("channel_id", 0)
    # Показать меню ИИ в том же сообщении предпросмотра (HTML)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Генерация текста", callback_data="ai_gen_topic"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Генерация из ссылки", callback_data="ai_gen_link"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Улучшить текст", callback_data="ai_improve"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="ai_back_to_preview")],
        ]
    )
    with suppress(TelegramBadRequest):
        pid = (await state.get_data()).get(
            "preview_msg_id"
        ) or callback.message.message_id
        await tg_bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=int(pid),
            text="🤖 <b>ИИ</b>\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data == "ai_back_to_preview")
async def cb_ai_back_to_preview(callback: CallbackQuery, state: FSMContext):
    """Вернуться из меню ИИ обратно к предпросмотру."""
    data = await state.get_data()
    # Восстановим «Редактор поста» и клавиатуру в том же сообщении
    prev_id = data.get("preview_msg_id") or callback.message.message_id
    instr_html = "🖊️ <b>Редактор поста</b>\n\nЕсли нужно изменить текст — просто пришлите новый текст.\nЧтобы добавить фото или видео, отправьте их боту."
    kb = await _build_preview_kb(data)
    with suppress(TelegramBadRequest):
        await tg_bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=int(prev_id),
            text=instr_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data == "ai_gen_topic")
async def cb_ai_gen_topic(callback: CallbackQuery, state: FSMContext):
    """Генерация текста по теме."""
    # Переходим в режим ввода темы
    await state.set_state(PostFSM.ai_topic_input)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ai_back_to_preview")]
        ]
    )

    text = """📝 **Генерация текста по теме**

Введите тему для генерации поста.

**Примеры:**
• Новая акция на товары со скидкой 50%
• Обзор последних новостей в IT
• Как улучшить продуктивность
• Розыгрыш призов среди подписчиков"""

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "ai_gen_link")
async def cb_ai_gen_link(callback: CallbackQuery, state: FSMContext):
    """Генерация из ссылки."""
    await state.set_state(PostFSM.ai_link_input)
    # Текущий режим (summary|rewrite|paraphrase)
    data = await state.get_data()
    mode = (data.get("ai_link_mode") or "summary").lower().strip()
    mode_label = {
        "summary": "Summary",
        "rewrite": "Rewrite",
        "paraphrase": "Paraphrase",
    }.get(mode, "Summary")
    # Сохраним режим по умолчанию, если ещё не был
    await state.update_data(ai_link_mode=mode)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Режим: {mode_label}", callback_data="ai_link_mode"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="ai_back_to_preview")],
        ]
    )

    text = """🔗 Генерация из ссылки

Отправьте ссылку на статью. Режим определяет тип обработки: Summary / Rewrite / Paraphrase.

Поддерживаются:
• Новостные сайты
• Блоги
• Статьи (Medium, VC.ru и др.)

Пример: https://vc.ru/marketing/123456-article"""

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "ai_link_mode")
async def cb_ai_link_mode(callback: CallbackQuery, state: FSMContext):
    """Переключить режим обработки ссылки: summary → rewrite → paraphrase."""
    data = await state.get_data()
    cur = (data.get("ai_link_mode") or "summary").lower().strip()
    next_map = {"summary": "rewrite", "rewrite": "paraphrase", "paraphrase": "summary"}
    new_mode = next_map.get(cur, "summary")
    await state.update_data(ai_link_mode=new_mode)
    label = {
        "summary": "Summary",
        "rewrite": "Rewrite",
        "paraphrase": "Paraphrase",
    }.get(new_mode, "Summary")
    await _safe_edit_reply_markup(
        tg_bot,
        callback.message.chat.id,
        callback.message.message_id,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Режим: {label}", callback_data="ai_link_mode"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена", callback_data="ai_back_to_preview"
                    )
                ],
            ]
        ),
    )
    await callback.answer(f"Режим: {label}")


@router.callback_query(F.data == "ai_improve")
async def cb_ai_improve(callback: CallbackQuery, state: FSMContext):
    """Улучшить существующий текст."""
    data = await state.get_data()
    payload = data.get("payload", {})

    # Проверяем, есть ли текст в посте
    current_text = ""
    if payload.get("type") == "text":
        current_text = payload.get("text", "")
    elif payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "album",
    }:
        current_text = payload.get("caption", "")

    if not current_text:
        await callback.answer("❌ Сначала добавьте текст в пост", show_alert=True)
        return

    # Показываем меню с вариантами улучшения
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Улучшить общий стиль", callback_data="ai_improve_style"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📏 Укоротить", callback_data="ai_improve_shorten"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📖 Удлинить", callback_data="ai_improve_lengthen"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎨 Изменить тон", callback_data="ai_improve_tone_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    text="😊 Добавить эмодзи", callback_data="ai_improve_emoji"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="ai_back_to_preview")],
        ]
    )

    try:
        await callback.message.edit_text(
            "✨ **Улучшение текста**\n\nВыберите, что сделать с текстом:",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


# --- Обработчик ввода темы для генерации ---
@router.message(PostFSM.ai_topic_input)
async def handle_ai_topic_input(message: Message, state: FSMContext):
    """Обработка ввода темы для генерации."""
    topic = message.text.strip()

    if not topic:
        await message.answer("❌ Тема не может быть пустой. Попробуйте ещё раз.")
        return

    # Получаем данные из состояния
    data = await state.get_data()
    channel_id = data.get("channel_id", 0)
    ai_menu_msg_id = data.get("ai_menu_msg_id")

    # Удаляем сообщение пользователя
    with suppress(TelegramBadRequest):
        await message.delete()

    # Stream the model directly into Telegram's ephemeral draft preview.
    async with AsyncSessionLocal() as session:
        from app.bot.ai_editor_runtime import run_editor_ai_request
        from app.repositories.ai_settings import ChannelAISettingsRepo

        try:
            repo = ChannelAISettingsRepo(session)
            await repo.update_enabled(channel_id, True)
        except Exception:
            pass

        context = {
            "brand": data.get("brand", ""),
            "audience": data.get("audience", "подписчики канала"),
            "cta": data.get("cta", ""),
        }
        request = {
            "kind": "topic",
            "topic": topic,
            "extra": context,
            "prompt_key": "ai_text",
        }
        await state.update_data(ai_last_request=request)
        result = await run_editor_ai_request(
            tg_bot,
            session=session,
            request=request,
            channel_id=channel_id,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            seed=message.message_id,
        )
    if not result["success"]:
        err = (result.get("error") or "").lower()
        # Апселл при лимитах токенов
        if ("лимит токенов" in err) or ("limit" in err and "token" in err):
            kb = await _build_pro_upsell_kb(message, channel_id)
            with suppress(TelegramBadRequest):
                await message.answer(
                    "Превышен лимит токенов. В Pro — больше лимиты и быстрый доступ к ИИ.",
                    reply_markup=kb,
                )
            await _log_ai_limit_to_admin(message, channel_id)
        else:
            error_text = (
                f"❌ Ошибка генерации:\n{result.get('error', 'Неизвестная ошибка')}"
            )
            await message.answer(error_text)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="← Назад", callback_data="ai_back_to_preview"
                        )
                    ]
                ]
            )
            if ai_menu_msg_id:
                with suppress(TelegramBadRequest):
                    await tg_bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=ai_menu_msg_id,
                        text=error_text,
                        reply_markup=kb,
                    )
        await state.set_state(PostFSM.preview)
        return

    # Успешная генерация - обновляем payload поста
    generated_text = result["text"]

    # Обновляем payload с новым текстом
    payload = data.get("payload", {})
    if not payload:
        # Создаём новый payload если его не было
        payload = {"type": "text", "text": generated_text}
    else:
        # Обновляем текст или подпись
        if payload.get("type") == "text":
            payload["text"] = generated_text
        elif payload.get("type") in {
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "album",
        }:
            payload["caption"] = generated_text
        else:
            payload["text"] = generated_text
            payload["type"] = "text"

    await state.update_data(payload=payload)

    # Очищаем состояние ИИ
    await state.set_state(PostFSM.preview)

    # Удаляем меню ИИ
    if ai_menu_msg_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=message.chat.id, message_id=ai_menu_msg_id
            )

    # Показываем новый предпросмотр с сгенерированным текстом
    notify_on = bool(data.get("notify_on", True))
    autosign_on = bool(data.get("autosign_on", False))
    pin_on = bool(data.get("pin_on", False))
    comments_on = bool(data.get("comments_on", True))
    is_draft = bool(data.get("is_draft", False))
    edit_mode = bool(data.get("edit_mode", False))

    preview = await _send_preview_message(
        message,
        payload,
        notify_on=notify_on,
        autosign_on=autosign_on,
        pin_on=pin_on,
        comments_on=comments_on,
        is_draft=is_draft,
        edit_mode=edit_mode,
        has_buttons=bool(payload.get("buttons")),
        state=state,
    )

    await state.update_data(preview_msg_id=preview.message_id)

    from app.bot.ai_editor_runtime import ai_result_actions_kb, ai_result_summary

    await message.answer(
        ai_result_summary(result, label="Текст сгенерирован"),
        reply_markup=ai_result_actions_kb(),
    )


# --- Обработчик ввода ссылки для генерации ---


@router.message(PostFSM.ai_link_input)
async def handle_ai_link_input(message: Message, state: FSMContext):
    """Обработка ввода ссылки для саммари."""
    url = message.text.strip()

    # Проверка, что это похоже на URL
    if not url.startswith("http://") and not url.startswith("https://"):
        await message.answer("❌ Это не похоже на ссылку. Попробуйте ещё раз.")
        return

    with suppress(TelegramBadRequest):
        await message.delete()

    data = await state.get_data()
    channel_id = data.get("channel_id", 0)

    mode = (data.get("ai_link_mode") or "summary").lower().strip()
    if mode not in {"summary", "rewrite", "paraphrase"}:
        mode = "summary"
    request = {
        "kind": "link",
        "url": url,
        "mode": mode,
        "prompt_key": "ai_link",
    }
    await state.update_data(ai_last_request=request)

    try:
        async with AsyncSessionLocal() as session:
            from app.bot.ai_editor_runtime import run_editor_ai_request

            result = await run_editor_ai_request(
                tg_bot,
                session=session,
                request=request,
                channel_id=channel_id,
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                seed=message.message_id,
            )
        if not result["success"]:
            err = (result.get("error") or "").lower()
            if ("лимит токенов" in err) or ("limit" in err and "token" in err):
                kb = await _build_pro_upsell_kb(message, channel_id)
                with suppress(TelegramBadRequest):
                    await message.answer(
                        "Превышен лимит токенов. В Pro — больше лимиты и быстрый доступ к ИИ.",
                        reply_markup=kb,
                    )
                await _log_ai_limit_to_admin(message, channel_id)
            else:
                await message.answer(
                    f"❌ Ошибка:\n{result.get('error', 'Неизвестная ошибка')}"
                )
            await state.set_state(PostFSM.preview)
            return

        # Обновляем payload
        generated_text = result["text"]

        payload = data.get("payload", {})
        if payload.get("type") in {
            "photo",
            "video",
            "animation",
            "audio",
            "voice",
            "album",
        }:
            payload["caption"] = generated_text
        else:
            payload["type"] = "text"
            payload["text"] = generated_text

        await state.update_data(payload=payload)
        await state.set_state(PostFSM.preview)

        # Показываем предпросмотр
        notify_on = bool(data.get("notify_on", True))
        autosign_on = bool(data.get("autosign_on", False))
        pin_on = bool(data.get("pin_on", False))
        comments_on = bool(data.get("comments_on", True))
        is_draft = bool(data.get("is_draft", False))
        edit_mode = bool(data.get("edit_mode", False))

        preview = await _send_preview_message(
            message,
            payload,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=is_draft,
            edit_mode=edit_mode,
            has_buttons=bool(payload.get("buttons")),
            state=state,
        )

        await state.update_data(preview_msg_id=preview.message_id)
        mode_done = {
            "summary": "Саммари",
            "rewrite": "Рерайт",
            "paraphrase": "Перефраз",
        }.get(mode, "Саммари")
        from app.bot.ai_editor_runtime import ai_result_actions_kb, ai_result_summary

        await message.answer(
            ai_result_summary(result, label=f"{mode_done} готов"),
            reply_markup=ai_result_actions_kb(),
        )

    except Exception as e:
        logger.error(f"Ошибка при генерации из ссылки: {e}")
        await message.answer(f"❌ Произошла ошибка: {str(e)}")
        await state.set_state(PostFSM.preview)


# --- Обработчики улучшения текста ---


@router.callback_query(F.data.startswith("ai_improve_"))
async def cb_ai_improve_action(callback: CallbackQuery, state: FSMContext):
    """Выполнение конкретного улучшения текста."""
    from app.bot.ai_editor_runtime import improve_action_name

    action = improve_action_name(callback.data)

    instructions = {
        "style": "улучши общий стиль текста, сделай его более привлекательным и читабельным",
        "shorten": "укороти этот текст, оставив только главное",
        "lengthen": "расширь этот текст, добавив больше деталей и примеров",
        "emoji": "добавь эмодзи в текст, чтобы сделать его более живым",
    }

    # Для "tone_menu" показываем подменю
    if action == "tone_menu":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="😊 Дружелюбный", callback_data="ai_improve_tone_friendly"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🎓 Экспертный", callback_data="ai_improve_tone_expert"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 Официальный", callback_data="ai_improve_tone_official"
                    )
                ],
                [InlineKeyboardButton(text="← Назад", callback_data="ai_improve")],
            ]
        )
        try:
            await callback.message.edit_text(
                "🎨 Выберите желаемый тон:", reply_markup=kb
            )
        except TelegramBadRequest:
            pass
        return await callback.answer()

    # Если это выбор тона
    if action.startswith("tone_"):
        tone = action.replace("tone_", "")
        tone_labels = {
            "friendly": "дружелюбным",
            "expert": "экспертным",
            "official": "официальным",
        }
        instructions[action] = (
            f"перепиши этот текст в {tone_labels.get(tone, tone)} тоне"
        )

    instruction = instructions.get(action, "улучши текст")

    data = await state.get_data()
    channel_id = data.get("channel_id", 0)
    payload = data.get("payload", {})

    # Извлекаем текущий текст
    current_text = ""
    if payload.get("type") == "text":
        current_text = payload.get("text", "")
    elif payload.get("type") in {
        "photo",
        "video",
        "animation",
        "audio",
        "voice",
        "album",
    }:
        current_text = payload.get("caption", "")

    if not current_text:
        await callback.answer("❌ Нет текста для улучшения", show_alert=True)
        return

    # Удаляем меню
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    try:
        request = {
            "kind": "improve",
            "original_text": current_text,
            "instruction": instruction,
            "prompt_key": "ai_improve",
        }
        await state.update_data(ai_last_request=request)
        async with AsyncSessionLocal() as session:
            from app.bot.ai_editor_runtime import run_editor_ai_request

            result = await run_editor_ai_request(
                tg_bot,
                session=session,
                request=request,
                channel_id=channel_id,
                user_id=callback.from_user.id,
                chat_id=callback.message.chat.id,
                seed=callback.message.message_id,
            )

        if not result["success"]:
            await callback.message.answer(
                f"❌ Ошибка:\n{result.get('error', 'Неизвестная ошибка')}"
            )
            await state.set_state(PostFSM.preview)
            return

        # Обновляем payload
        improved_text = result["text"]

        if payload.get("type") == "text":
            payload["text"] = improved_text
        else:
            payload["caption"] = improved_text

        await state.update_data(payload=payload)
        await state.set_state(PostFSM.preview)

        # Показываем предпросмотр
        notify_on = bool(data.get("notify_on", True))
        autosign_on = bool(data.get("autosign_on", False))
        pin_on = bool(data.get("pin_on", False))
        comments_on = bool(data.get("comments_on", True))
        is_draft = bool(data.get("is_draft", False))
        edit_mode = bool(data.get("edit_mode", False))

        preview = await _send_preview_message(
            callback.message,
            payload,
            notify_on=notify_on,
            autosign_on=autosign_on,
            pin_on=pin_on,
            comments_on=comments_on,
            is_draft=is_draft,
            edit_mode=edit_mode,
            has_buttons=bool(payload.get("buttons")),
            state=state,
        )

        await state.update_data(preview_msg_id=preview.message_id)
        from app.bot.ai_editor_runtime import ai_result_actions_kb, ai_result_summary

        await callback.message.answer(
            ai_result_summary(result, label="Текст улучшен"),
            reply_markup=ai_result_actions_kb(),
        )

    except Exception as e:
        logger.error(f"Ошибка при улучшении текста: {e}")
        await callback.message.answer(f"❌ Произошла ошибка: {str(e)}")
        await state.set_state(PostFSM.preview)
