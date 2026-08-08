from __future__ import annotations

from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.fsm.states import PostFSM
from app.bot.keyboards.builders import build_root_settings_kb
from app.bot.keyboards.navigation import home_dashboard_kb, home_dashboard_text
from app.bot.keyboards.reply import add_channel_kb
from app.core.callbacks import CB
from app.core.db import AsyncSessionLocal
from app.repositories.channels import ChannelsRepo
from app.repositories.clients import ClientsRepo


router = Router()
router.callback_query.filter(F.message.chat.type == "private")


async def _edit_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            home_dashboard_text(inline_mode=True),
            reply_markup=home_dashboard_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


async def _render_channel_picker(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    draft: bool,
) -> None:
    user = callback.from_user
    await state.clear()

    async with AsyncSessionLocal() as session:
        clients = ClientsRepo(session)
        channels = ChannelsRepo(session)
        client = await clients.create_or_get(user.id, user.username, user.full_name)
        ui_settings = await clients.get_ui_settings(client.id)
        last_channel_id = await clients.get_last_channel_id(client.id)
        items = await channels.list_by_owner(client.id)

    if not items:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="➕ Добавить канал / чат", callback_data=CB.GM_ADD_CHANNEL
                    )
                ],
                [InlineKeyboardButton(text="← Главное меню", callback_data=CB.GM_GLOBAL_MENU)],
            ]
        )
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(
                "У вас пока нет добавленных каналов или чатов.", reply_markup=kb
            )
        await callback.answer()
        return

    await state.update_data(
        ui_settings=dict(ui_settings or {}),
        ui_client_id=int(client.id),
        is_draft=bool(draft),
    )

    valid = {int(ch.id): ch for ch in items}
    remember = bool((ui_settings or {}).get("remember_channel", False))
    last_id = int(last_channel_id) if last_channel_id is not None else None
    if remember and last_id in valid:
        from app.bot.routers.main import _open_create_card_for_channel

        await _open_create_card_for_channel(
            state=state,
            channel_id=last_id,
            user=user,
            via_callback=callback,
        )
        await callback.answer()
        return

    rows: list[list[InlineKeyboardButton]] = []
    for channel in items:
        title = (channel.title or str(channel.tg_chat_id))[:40]
        rows.append(
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=f"{CB.POST_PICK_CH_PREFIX}{channel.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="← Главное меню", callback_data=CB.GM_GLOBAL_MENU)]
    )
    prompt = "Выберите канал для черновика" if draft else "Выберите канал для публикации"
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            f"{'📝' if draft else '✍️'} <b>{prompt}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == CB.GM_GLOBAL_MENU)
async def cb_home(callback: CallbackQuery, state: FSMContext) -> None:
    await _edit_home(callback, state)


@router.callback_query(F.data == CB.GM_CREATE_POST)
async def cb_create_post(callback: CallbackQuery, state: FSMContext) -> None:
    await _render_channel_picker(callback, state, draft=False)


@router.callback_query(F.data == CB.GM_DRAFT)
async def cb_create_draft(callback: CallbackQuery, state: FSMContext) -> None:
    await _render_channel_picker(callback, state, draft=True)


@router.callback_query(F.data == CB.GM_EDIT_POST)
async def cb_edit_post(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PostFSM.edit_pick)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Главное меню", callback_data=CB.GM_GLOBAL_MENU)]
        ]
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "✏️ <b>Редактировать пост</b>\n\n"
            "Перешлите сюда пост из вашего канала. Бот откроет его в редакторе и изменит только выбранную публикацию.",
            reply_markup=kb,
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == CB.CP_OPEN)
async def cb_content_plan(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    from app.bot.routers.content_plan import _render_cp_channels_list

    await _render_cp_channels_list(callback, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == CB.GM_SETTINGS)
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "⚙️ <b>Настройки</b>\n\n"
            "Управляйте каналами, часовым поясом и интерфейсом бота.",
            reply_markup=build_root_settings_kb(callback.from_user.id),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == CB.GM_ADD_CHANNEL)
async def cb_add_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer(
        "➕ Выберите, что подключаем. После выбора Telegram запросит нужные права администратора.",
        reply_markup=add_channel_kb(),
    )
    await callback.answer()
