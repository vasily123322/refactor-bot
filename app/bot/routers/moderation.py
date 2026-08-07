from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from aiogram.fsm.context import FSMContext
from app.bot.fsm.states import SettingsFSM
from aiogram.exceptions import TelegramBadRequest
from app.core.db import AsyncSessionLocal


router = Router()


@router.callback_query(F.data.startswith("ai_toggle_moderation_"))
async def cb_ai_toggle_moderation(callback: CallbackQuery):
    """Переключить модерацию."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        await repo.update_params(cid, moderation_enabled=not st.moderation_enabled)

    await callback.answer(
        "✅ Модерация " + ("включена" if not st.moderation_enabled else "выключена")
    )
    # Обновляем меню модерации без изменения callback.data
    try:
        from app.bot.routers.main import (
            cb_neu_moder as _cb_neu_moder,
        )  # delayed import to avoid cycles

        await _cb_neu_moder(callback)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ai_toggle_links_"))
async def cb_ai_toggle_links(callback: CallbackQuery):
    """Переключить разрешение ссылок."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        await repo.update_params(cid, links_allowed=not st.links_allowed)

    await callback.answer(
        "✅ Ссылки " + ("разрешены" if not st.links_allowed else "запрещены")
    )
    try:
        from app.bot.routers.main import cb_neu_moder as _cb_neu_moder

        await _cb_neu_moder(callback)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ai_toggle_utm_"))
async def cb_ai_toggle_utm(callback: CallbackQuery):
    """Переключить UTM-метки."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        await repo.update_params(cid, utm_enabled=not st.utm_enabled)

    await callback.answer(
        "✅ UTM-метки " + ("включены" if not st.utm_enabled else "выключены")
    )
    try:
        from app.bot.routers.main import cb_neu_moder as _cb_neu_moder

        await _cb_neu_moder(callback)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ai_forbidden_words_"))
async def cb_ai_forbidden_words(
    callback: CallbackQuery, state: FSMContext | None = None
):
    """Управление запрещёнными словами."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)

    words = st.forbidden_words or []
    words_list = ", ".join(words) if words else "нет"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить слова", callback_data=f"ai_forbidden_add_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Очистить список", callback_data=f"ai_forbidden_clear_{cid}"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"neu_moder_{cid}")],
        ]
    )

    text = f"📝 **Запрещённые слова**\n\nТекущий список:\n{words_list}\n\nДобавьте слова, которые не должны появляться в сгенерированных постах."

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("ai_forbidden_add_"))
async def cb_ai_forbidden_add(callback: CallbackQuery, state: FSMContext):
    """Добавление запрещённых слов."""
    from app.bot.fsm.states import SettingsFSM

    cid = int(callback.data.split("_")[-1])

    await state.set_state(SettingsFSM.forbidden_input)
    await state.update_data(forbidden_channel_id=cid)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"ai_forbidden_words_{cid}"
                )
            ]
        ]
    )

    text = """📝 **Добавление запрещённых слов**
Отправьте список слов через запятую или пробел.

Пример:
`казино, ставки, азартные игры`

Или:
`блокировка бан удалён`"""

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.message(SettingsFSM.forbidden_input)
async def handle_forbidden_input(message: Message, state: FSMContext):
    """Обработка ввода запрещённых слов."""
    # state is enforced by decorator
    data = await state.get_data()
    cid = data.get("forbidden_channel_id")

    if not cid:
        await message.answer("❌ Ошибка: канал не найден")
        await state.clear()
        return

    text_input = message.text.strip()

    # Парсим слова (разделитель: запятая, пробел, точка с запятой)
    import re

    words = re.split(r"[,;\s]+", text_input)
    words = [w.strip().lower() for w in words if w.strip()]

    if not words:
        await message.answer("❌ Не распознаны слова. Попробуйте ещё раз.")
        return

    # Добавляем к существующим
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)

        existing = list(st.forbidden_words or [])
        # Объединяем и убираем дубли
        all_words = list(set(existing + words))
        await repo.update_forbidden_words(cid, all_words)

    await state.clear()
    await message.answer(
        f"✅ Добавлено {len(words)} слов. Всего в списке: {len(all_words)}"
    )

    # Возвращаемся к меню запрещённых слов
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Назад", callback_data=f"ai_forbidden_words_{cid}"
                )
            ]
        ]
    )
    await message.answer("Вернуться:", reply_markup=kb)


@router.callback_query(F.data.startswith("ai_forbidden_clear_"))
async def cb_ai_forbidden_clear(callback: CallbackQuery):
    """Очистить список запрещённых слов."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_forbidden_words(cid, [])

    await callback.answer("✅ Список очищен")
    # Возвращаемся к меню запрещённых слов без изменения callback.data
    await cb_ai_forbidden_words(callback)


@router.callback_query(F.data.startswith("ai_toggle_hashtags_"))
async def cb_ai_toggle_hashtags(callback: CallbackQuery):
    """Переключить генерацию хештегов."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        new_value = not st.hashtags_enabled
        await repo.update_params(cid, hashtags_enabled=new_value)

    await callback.answer("✅ Хештеги " + ("включены" if new_value else "выключены"))

    # Обновляем меню с актуальными данными
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st_updated = await repo.get_or_create(cid)

    hashtags_status = "✅ Вкл" if st_updated.hashtags_enabled else "❌ Выкл"
    cta_status = "✅ Вкл" if st_updated.cta_enabled else "❌ Выкл"
    text = f"#️⃣ Хештеги/CTA\n\nХештеги: {hashtags_status}\nКоличество: {st_updated.hashtags_count}\nCTA: {cta_status}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ Хештеги" if st_updated.hashtags_enabled else "☑️ Хештеги"),
                    callback_data=f"ai_toggle_hashtags_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Количество хештегов", callback_data=f"ai_hashtags_count_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ CTA" if st_updated.cta_enabled else "☑️ CTA"),
                    callback_data=f"ai_toggle_cta_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data.startswith("ai_hashtags_count_"))
async def cb_ai_hashtags_count(callback: CallbackQuery):
    """Выбор количества хештегов."""
    cid = int(callback.data.split("_")[-1])

    counts = [1, 2, 3, 5, 7]
    kb_rows = []
    for count in counts:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{count} хештегов",
                    callback_data=f"ai_set_hashtags_count_{cid}_{count}",
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"neu_tags_{cid}")]
    )

    text = "#️⃣ **Количество хештегов:**\n\nВыберите, сколько хештегов генерировать."

    try:
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="Markdown",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("ai_set_hashtags_count_"))
async def cb_ai_set_hashtags_count(callback: CallbackQuery):
    """Установить количество хештегов."""
    parts = callback.data.split("_")
    cid = int(parts[4])
    count = int(parts[5])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        await repo.update_params(cid, hashtags_count=count)

    await callback.answer(f"✅ Количество: {count}")
    try:
        from app.bot.routers.main import cb_neu_tags as _cb_neu_tags

        await _cb_neu_tags(callback)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ai_toggle_cta_"))
async def cb_ai_toggle_cta(callback: CallbackQuery):
    """Переключить CTA."""
    cid = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st = await repo.get_or_create(cid)
        new_value = not st.cta_enabled
        await repo.update_params(cid, cta_enabled=new_value)

    await callback.answer("✅ CTA " + ("включен" if new_value else "выключен"))

    # Обновляем меню с актуальными данными
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import ChannelAISettingsRepo

        repo = ChannelAISettingsRepo(session)
        st_updated = await repo.get_or_create(cid)

    hashtags_status = "✅ Вкл" if st_updated.hashtags_enabled else "❌ Выкл"
    cta_status = "✅ Вкл" if st_updated.cta_enabled else "❌ Выкл"
    text = f"#️⃣ Хештеги/CTA\n\nХештеги: {hashtags_status}\nКоличество: {st_updated.hashtags_count}\nCTA: {cta_status}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ Хештеги" if st_updated.hashtags_enabled else "☑️ Хештеги"),
                    callback_data=f"ai_toggle_hashtags_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Количество хештегов", callback_data=f"ai_hashtags_count_{cid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ CTA" if st_updated.cta_enabled else "☑️ CTA"),
                    callback_data=f"ai_toggle_cta_{cid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"settings_neuropost_{cid}"
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
