from __future__ import annotations

from contextlib import suppress
from datetime import date

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
)
from loguru import logger

from app.bot.bot_instance import bot as tg_bot
from app.bot.routers.utils.post_payload import (
    clip_text_len,
    maybe_append_autosign,
)
from app.core.db import AsyncSessionLocal
from app.services.canonical_publication_edit import (
    CanonicalPublicationEditCoordinator,
    CanonicalPublicationEditSyncFailed,
)
from app.services.publication_edit_persistence import (
    PublicationEditConflictError,
    PublicationEditPersistenceError,
)
from app.services.telegram_edit_outcome import TelegramEditFailed


def _canonical_identity(data: dict) -> tuple[int, int] | None:
    context = data.get("canonical_edit_context")
    if not isinstance(context, dict):
        return None
    try:
        publication_id = int(context.get("publication_id") or 0)
        expected_revision = int(context.get("expected_revision") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if publication_id <= 0 or expected_revision <= 0:
        return None
    return publication_id, expected_revision


def _canonical_return(data: dict, publication_id: int) -> str | None:
    ret = data.get("canonical_return_to_notice")
    if not isinstance(ret, dict):
        return None
    try:
        ret_publication_id = int(ret.get("publication_id") or 0)
        date_iso = date.fromisoformat(str(ret.get("date"))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None
    if ret_publication_id != int(publication_id):
        return None
    return date_iso


async def _canonical_result(
    *,
    callback: CallbackQuery,
    data: dict,
    payload: dict,
    publication_id: int,
    expected_revision: int,
):
    coordinator = CanonicalPublicationEditCoordinator(
        provider=tg_bot,
        session_factory=AsyncSessionLocal,
    )
    payload_type = str(payload.get("type") or "")
    user_id = int(callback.from_user.id)

    if payload_type == "text":
        text0 = await maybe_append_autosign(payload.get("text", ""), data)
        return await coordinator.edit_text_and_persist(
            publication_id=publication_id,
            tg_user_id=user_id,
            expected_revision=expected_revision,
            payload=payload,
            text=clip_text_len(text0, 4096),
        )

    if payload_type == "photo":
        caption0 = await maybe_append_autosign(payload.get("caption") or "", data)
        media = InputMediaPhoto(
            media=payload.get("file_id"),
            caption=clip_text_len(caption0, 1024),
            parse_mode="Markdown",
            has_spoiler=bool(payload.get("media_spoiler", False)),
            show_caption_above_media=(str(payload.get("media_pos")) == "bottom"),
        )
        return await coordinator.edit_media_and_persist(
            publication_id=publication_id,
            tg_user_id=user_id,
            expected_revision=expected_revision,
            payload=payload,
            media=media,
        )

    if payload_type == "video":
        caption0 = await maybe_append_autosign(payload.get("caption") or "", data)
        media = InputMediaVideo(
            media=payload.get("file_id"),
            caption=clip_text_len(caption0, 1024),
            parse_mode="Markdown",
            has_spoiler=bool(payload.get("media_spoiler", False)),
            show_caption_above_media=(str(payload.get("media_pos")) == "bottom"),
        )
        return await coordinator.edit_media_and_persist(
            publication_id=publication_id,
            tg_user_id=user_id,
            expected_revision=expected_revision,
            payload=payload,
            media=media,
        )

    if payload_type == "animation":
        media = InputMediaAnimation(
            media=payload.get("file_id"),
            caption=payload.get("caption"),
            parse_mode="Markdown",
        )
        return await coordinator.edit_media_and_persist(
            publication_id=publication_id,
            tg_user_id=user_id,
            expected_revision=expected_revision,
            payload=payload,
            media=media,
        )

    if payload_type == "audio":
        media = InputMediaAudio(
            media=payload.get("file_id"),
            caption=payload.get("caption"),
            parse_mode="Markdown",
        )
        return await coordinator.edit_media_and_persist(
            publication_id=publication_id,
            tg_user_id=user_id,
            expected_revision=expected_revision,
            payload=payload,
            media=media,
        )

    raise PublicationEditPersistenceError("unsupported canonical editor payload")


async def handle_canonical_publication_edit(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    data: dict,
    payload: dict,
) -> None:
    identity = _canonical_identity(data)
    if identity is None:
        await callback.answer("Ошибка canonical edit state", show_alert=True)
        return
    publication_id, expected_revision = identity

    try:
        result = await _canonical_result(
            callback=callback,
            data=data,
            payload=payload,
            publication_id=publication_id,
            expected_revision=expected_revision,
        )
    except PublicationEditConflictError:
        await callback.answer(
            "Публикация изменилась. Откройте её заново.",
            show_alert=True,
        )
        return
    except TelegramEditFailed as exc:
        logger.warning(
            "Canonical publication Telegram edit failed error_type={}",
            exc.provider_error_type,
        )
        await callback.answer("Не удалось отредактировать публикацию", show_alert=True)
        return
    except CanonicalPublicationEditSyncFailed as exc:
        logger.warning(
            "Canonical publication edit sync failed conflict={} error_type={}",
            exc.conflict,
            exc.error_type,
        )
        await callback.answer(
            "Сообщение могло измениться, но данные не синхронизированы. Откройте публикацию заново.",
            show_alert=True,
        )
        return
    except PublicationEditPersistenceError:
        await callback.answer(
            "Публикация недоступна для редактирования. Откройте её заново.",
            show_alert=True,
        )
        return
    except Exception as exc:
        logger.warning(
            "Canonical publication edit failed error_type={}",
            type(exc).__name__,
        )
        await callback.answer("Не удалось сохранить изменение", show_alert=True)
        return

    if bool(data.get("pin_on", False)):
        with suppress(Exception):
            await tg_bot.pin_chat_message(
                chat_id=result.tg_chat_id,
                message_id=result.message_id,
            )

    date_iso = _canonical_return(data, publication_id)
    if date_iso is not None:
        # The card reloads all authoritative delivery/content state from canonical DB.
        # Do not leave edit transport fields live in FSM after a successful save.
        await state.update_data(
            edit_chat_id=None,
            edit_msg_id=None,
            result_ids=None,
            canonical_edit_context=None,
            prev_editor_restore=None,
        )
        from app.bot.routers.content_plan_publication import cb_cp_open_publication

        cb2 = callback.model_copy(
            update={"data": f"cp_open_pub:{publication_id}:{date_iso}"}
        )  # type: ignore
        await cb_cp_open_publication(cb2, state)
        return

    preview_id = data.get("preview_msg_id")
    if preview_id:
        with suppress(TelegramBadRequest):
            await tg_bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=int(preview_id),
            )
    with suppress(TelegramBadRequest):
        await callback.answer("Готово")
    await state.clear()
