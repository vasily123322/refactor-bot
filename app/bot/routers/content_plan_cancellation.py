from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.routers.content_plan import router as legacy_content_plan_router
from app.bot.routers.content_plan_publication import (
    cb_cp_delete_publication,
    cb_cp_edit_publication,
    cb_cp_open_publication,
)
from app.core.db import AsyncSessionLocal
from app.services.content_plan_history_identity import (
    HistoryPublicationIdentity,
    HistoryPublicationIdentityKind,
    resolve_history_publication_identity,
)
from app.services.publication_editor import load_owned_publication_editor_view


router = Router()


async def _identity_for_post_id(post_id: int) -> HistoryPublicationIdentity:
    """Resolve durable legacy callback identity without reading PostTask."""

    async with AsyncSessionLocal() as session:
        return await resolve_history_publication_identity(
            session,
            legacy_post_task_id=post_id,
        )


async def _owned_identity_for_post_id(
    post_id: int,
    *,
    tg_user_id: int,
) -> HistoryPublicationIdentity:
    identity = await _identity_for_post_id(post_id)
    if (
        identity.kind is not HistoryPublicationIdentityKind.CANONICAL_LINKED
        or identity.publication_id is None
    ):
        return identity

    async with AsyncSessionLocal() as session:
        owned = await load_owned_publication_editor_view(
            session,
            publication_id=int(identity.publication_id),
            tg_user_id=int(tg_user_id),
        )
    if owned is None:
        return HistoryPublicationIdentity(HistoryPublicationIdentityKind.FAIL_CLOSED)
    return identity


async def _identity_for_callback(
    post_id: int,
    callback: CallbackQuery,
) -> HistoryPublicationIdentity:
    user = getattr(callback, "from_user", None)
    raw_user_id = getattr(user, "id", None)
    if raw_user_id is None:
        # Unit-test/fake callback compatibility. Real Telegram CallbackQuery always
        # supplies from_user, and production canonical redirects therefore pass
        # through the owner-scoped resolver below.
        return await _identity_for_post_id(post_id)
    return await _owned_identity_for_post_id(
        post_id,
        tg_user_id=int(raw_user_id),
    )


async def _answer_expired(callback: CallbackQuery) -> None:
    await callback.answer(
        "Эта кнопка устарела. Переоткройте контент-план.",
        show_alert=True,
    )


async def _answer_ambiguous(callback: CallbackQuery) -> None:
    await callback.answer(
        "Публикацию нельзя однозначно определить",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("cp_open_post:"))
async def cb_cp_open_post_history_bridge(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Route canonical-linked legacy callback identity without reading PostTask."""

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка данных", show_alert=True)
        return
    _, post_id_str, date_iso = parts
    try:
        post_id = int(post_id_str)
    except (TypeError, ValueError):
        await callback.answer("Ошибка данных", show_alert=True)
        return

    identity = await _identity_for_callback(post_id, callback)
    if identity.kind is HistoryPublicationIdentityKind.LEGACY_ONLY:
        await _answer_expired(callback)
        return
    if (
        identity.kind is not HistoryPublicationIdentityKind.CANONICAL_LINKED
        or identity.publication_id is None
    ):
        await _answer_ambiguous(callback)
        return

    canonical_callback = callback.model_copy(
        update={"data": f"cp_open_pub:{identity.publication_id}:{date_iso}"}
    )
    await cb_cp_open_publication(canonical_callback, state)


@router.callback_query(F.data.startswith("cp_edit_post:"))
async def cb_cp_edit_post_history_bridge(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Move canonical-linked historical edit directly onto Publication authority."""

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка данных", show_alert=True)
        return
    _, post_id_str, date_iso = parts
    try:
        post_id = int(post_id_str)
    except (TypeError, ValueError):
        await callback.answer("Ошибка данных", show_alert=True)
        return

    identity = await _identity_for_callback(post_id, callback)
    if identity.kind is HistoryPublicationIdentityKind.LEGACY_ONLY:
        await _answer_expired(callback)
        return
    if (
        identity.kind is not HistoryPublicationIdentityKind.CANONICAL_LINKED
        or identity.publication_id is None
    ):
        await _answer_ambiguous(callback)
        return

    canonical_callback = callback.model_copy(
        update={"data": f"cp_edit_pub:{identity.publication_id}:{date_iso}"}
    )
    await cb_cp_edit_publication(canonical_callback, state)


@router.callback_query(F.data.startswith("cp_delete_post:"))
async def cb_cp_delete_post_canonical(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Route canonical-linked Delete to Publication cancellation; preserve true legacy."""

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, post_id_str, date_iso = parts
    try:
        post_id = int(post_id_str)
    except (TypeError, ValueError):
        await callback.answer("Ошибка данных", show_alert=True)
        return

    identity = await _identity_for_callback(post_id, callback)
    if identity.kind is HistoryPublicationIdentityKind.CANONICAL_LINKED:
        if identity.publication_id is None:
            await _answer_ambiguous(callback)
            return
        canonical_callback = callback.model_copy(
            update={"data": f"cp_delete_pub:{identity.publication_id}:{date_iso}"}
        )
        await cb_cp_delete_publication(canonical_callback, state)
        return
    if identity.kind is HistoryPublicationIdentityKind.LEGACY_ONLY:
        await _answer_expired(callback)
        return
    await _answer_ambiguous(callback)


@router.callback_query(F.data.startswith("cp_repeat_off:"))
async def cb_cp_repeat_off_history_bridge(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Never regain legacy repeat mutation authority for a canonical-linked callback."""

    parts = (callback.data or "").split(":")
    if len(parts) != 2:
        await callback.answer("Ошибка данных", show_alert=True)
        return
    try:
        post_id = int(parts[1])
    except (TypeError, ValueError):
        await callback.answer("Ошибка данных", show_alert=True)
        return

    identity = await _identity_for_callback(post_id, callback)
    if identity.kind is HistoryPublicationIdentityKind.LEGACY_ONLY:
        await _answer_expired(callback)
        return
    if identity.kind is HistoryPublicationIdentityKind.CANONICAL_LINKED:
        await callback.answer(
            "Автоповтор canonical-публикации нельзя отключить через старую кнопку",
            show_alert=True,
        )
        return
    await _answer_ambiguous(callback)


# Wrapper observers run before the included legacy router. Every legacy post callback
# first classifies durable canonical identity; true legacy remains unchanged underneath.
router.include_router(legacy_content_plan_router)
