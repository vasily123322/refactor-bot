from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from aiogram.fsm.context import FSMContext
from app.bot.fsm.states import SettingsFSM, PostFSM
from aiogram.exceptions import TelegramBadRequest
from contextlib import suppress
from loguru import logger
from app.core.db import AsyncSessionLocal
from app.bot.routers.shared import build_preview_kb
from app.services.ai_generation import AIGenerationService
from app.services.source_fetch import fetch_public_source_text
from app.services.llm.source_digest import (
    build_source_digest_context,
    build_source_digest_history_input,
    build_source_digest_instruction,
    build_source_digest_payload,
    build_source_digest_prompt_key,
    summarize_source_digest_modes,
)
from app.services.llm.generation_history import remember_generation


router = Router()


def _build_source_digest_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть редактор →", callback_data="ai_back_to_preview")],
            [InlineKeyboardButton(text="🔁 Сделать похожий", callback_data="ai_similar_last")],
        ]
    )


def _telegram_join_target(value: str) -> str:
    target = value.strip()
    if target.startswith("@"):
        return target[1:]
    if target.startswith("http"):
        for marker in ("t.me/", "telegram.me/"):
            if marker not in target:
                continue
            path = target.split(marker, 1)[1].strip("/")
            if not (path.startswith("+") or path.startswith("joinchat/")):
                return path.split("/", 1)[0]
            return path
    return target


def _telegram_fallback_value(value: str) -> str:
    for marker in ("t.me/", "telegram.me/"):
        if marker not in value:
            continue
        path = value.split(marker, 1)[1].strip("/")
        if path and not (path.startswith("+") or path.startswith("joinchat/")):
            return f"@{path.split('/', 1)[0]}"
    return value


async def _resolve_telegram_source(value: str) -> tuple[str, bool]:
    """Resolve a Telegram source without hiding userbot access failures."""
    try:
        from app.userbot.client import app as userbot
    except Exception as exc:
        logger.warning("AI source userbot unavailable value={} error={!r}", value, exc)
        return value, False

    join_target = _telegram_join_target(value)
    try:
        await userbot.join_chat(join_target)
    except Exception as exc:
        # Joining can legitimately fail for an already joined/public source. get_chat
        # below is the authoritative access check.
        logger.debug(
            "AI source userbot join skipped/failed target={} error={!r}",
            join_target,
            exc,
        )

    try:
        chat = await userbot.get_chat(join_target)
    except Exception as exc:
        fallback = _telegram_fallback_value(value)
        logger.warning(
            "AI source userbot cannot resolve target={} fallback={} error={!r}",
            join_target,
            fallback,
            exc,
        )
        return fallback, False

    resolved = (
        f"@{chat.username}"
        if getattr(chat, "username", None)
        else str(int(chat.id))
    )
    return resolved, True


@router.callback_query(F.data.startswith("ai_source_add_"))
async def cb_ai_source_add(callback: CallbackQuery, state: FSMContext):
    """Запросить ввод источника."""
    cid = int(callback.data.split("_")[-1])
    await state.set_state(SettingsFSM.source_input)
    await state.update_data(source_channel_id=cid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"neu_sources_{cid}")]
        ]
    )
    text = (
        "➕ Добавление источника\n\n"
        "Отправьте одним сообщением:\n"
        "• Просто URL (автоопределим тип url/rss)\n"
        "• @username для телеграм-канала\n"
        "• Или формат: rss &lt;url&gt; | url &lt;url&gt; | telegram &lt;@channel&gt;"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.message(SettingsFSM.source_input)
async def handle_source_input(message: Message, state: FSMContext):
    """Обработка ввода источника."""
    data = await state.get_data()
    cid = int(data.get("source_channel_id", 0))
    if not cid:
        await state.clear()
        return await message.answer("❌ Канал не распознан")

    raw = (message.text or "").strip()
    stype = None
    value = None
    parts = raw.split(maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in {"rss", "url", "telegram"}:
        stype = parts[0].lower()
        value = parts[1].strip()
    else:
        if raw.startswith("http://") or raw.startswith("https://"):
            value = raw
            if (
                raw.startswith("https://t.me/")
                or raw.startswith("http://t.me/")
                or raw.startswith("https://telegram.me/")
            ):
                stype = "telegram"
            else:
                stype = "rss" if ("/rss" in raw or raw.endswith(".xml")) else "url"
        elif (
            raw.startswith("t.me/")
            or raw.startswith("telegram.me/")
            or raw.startswith("www.t.me/")
        ):
            stype = "telegram"
            value = raw if raw.startswith("http") else ("https://" + raw)
        elif raw.startswith("@"):
            stype = "telegram"
            value = raw
        else:
            return await message.answer(
                "❌ Не удалось определить тип. Укажите: rss|url|telegram &lt;значение&gt;"
            )

    resolved_value = value
    telegram_access_ok = False
    if stype == "telegram":
        resolved_value, telegram_access_ok = await _resolve_telegram_source(value)

    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import AISourcesRepo

        repo = AISourcesRepo(session)
        existing = await repo.list_by_channel(cid)
        is_duplicate = False
        if stype == "telegram":
            cand = {resolved_value.lower()}
            if resolved_value.startswith("@"):
                name = resolved_value[1:]
                cand.add(f"https://t.me/{name}".lower())
            elif resolved_value.lstrip("-").isdigit():
                cand.add(str(resolved_value))
            for source in existing:
                if (
                    source.source_type == "telegram"
                    and source.source_value
                    and source.source_value.lower() in cand
                ):
                    is_duplicate = True
                    break
        else:
            for source in existing:
                if source.source_type == stype and source.source_value == value:
                    is_duplicate = True
                    break

        if is_duplicate:
            await state.clear()
            await message.answer("⚠️ Такой источник уже добавлен")
            return await cb_ai_source_list_from_message(message, cid)

        await repo.create(
            cid, stype, resolved_value if stype == "telegram" else value
        )

    if stype == "telegram" and telegram_access_ok:
        with suppress(Exception):
            await message.answer("✅ Userbot видит канал-источник")

    await state.clear()
    await message.answer("✅ Источник добавлен")
    await cb_ai_source_list_from_message(message, cid)


async def cb_ai_source_list_from_message(message: Message, cid: int):
    """Показать список источников из контекста message."""
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import AISourcesRepo

        repo = AISourcesRepo(session)
        sources = await repo.list_by_channel(cid)
    rows = []
    if not sources:
        rows.append(
            [InlineKeyboardButton(text="Назад", callback_data=f"neu_sources_{cid}")]
        )
        text = "🔁 Источники\n\nПока пусто. Добавьте первый источник."
        return await message.answer(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    for source in sources:
        status = "✅" if source.enabled else "❌"
        cite = "©" if source.citation_enabled else ""
        label = f"[{source.source_type}] {source.source_value} | {source.mode} {status} {cite}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"ai_source_item_{cid}_{source.id}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Назад", callback_data=f"neu_sources_{cid}")]
    )
    await message.answer(
        "🔁 Источники", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("ai_source_list_"))
async def cb_ai_source_list(callback: CallbackQuery):
    """Список источников с действиями."""
    cid = int(callback.data.split("_")[-1])
    await _edit_ai_sources_list(callback, cid)


async def _edit_ai_sources_list(callback: CallbackQuery, cid: int) -> None:
    """Отрисовать список источников для заданного канала в текущем сообщении."""
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import AISourcesRepo

        repo = AISourcesRepo(session)
        sources = await repo.list_by_channel(cid)
    rows: list[list[InlineKeyboardButton]] = []
    enabled_count = sum(1 for source in sources if getattr(source, "enabled", False))
    if enabled_count:
        rows.append(
            [InlineKeyboardButton(text="🧠 Сделать черновик из источников", callback_data=f"ai_source_digest_{cid}")]
        )
        rows.append(
            [InlineKeyboardButton(text="📝 Сделать черновики (3–5 вариантов)", callback_data=f"ai_source_drafts_{cid}")]
        )
    if not sources:
        rows.append(
            [InlineKeyboardButton(text="Назад", callback_data=f"neu_sources_{cid}")]
        )
        text = "🔁 Источники\n\nПока пусто. Добавьте первый источник."
        try:
            await callback.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()
        return
    for source in sources:
        status_btn = InlineKeyboardButton(
            text=("✅ Вкл" if source.enabled else "☑️ Выкл"),
            callback_data=f"ai_source_toggle_{cid}_{source.id}",
        )
        mode_btn = InlineKeyboardButton(
            text=f"Режим: {source.mode}", callback_data=f"ai_source_mode_{cid}_{source.id}"
        )
        cite_btn = InlineKeyboardButton(
            text=("© Цитировать" if source.citation_enabled else "© Без цитат"),
            callback_data=f"ai_source_cite_{cid}_{source.id}",
        )
        del_btn = InlineKeyboardButton(
            text="🗑 Удалить", callback_data=f"ai_source_delete_{cid}_{source.id}"
        )
        label = f"[{source.source_type}] {source.source_value}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"ai_source_nop_{cid}_{source.id}"
                )
            ]
        )
        rows.append([status_btn, mode_btn])
        rows.append([cite_btn, del_btn])
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"neu_sources_{cid}")]
    )
    try:
        await callback.message.edit_text(
            "🔁 Источники", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


async def _collect_digest_source_items(sources, *, per_source_limit: int = 2) -> list[dict]:
    items: list[dict] = []
    for source in sources:
        if not getattr(source, "enabled", False):
            continue
        stype = (getattr(source, "source_type", "") or "").lower().strip()
        value = (getattr(source, "source_value", "") or "").strip()
        mode = (getattr(source, "mode", "") or "summary").lower().strip()
        citation_enabled = bool(getattr(source, "citation_enabled", False))
        if not value:
            continue

        if stype == "telegram":
            try:
                from app.userbot.client import app as userbot

                target = value[1:] if value.startswith("@") else value
                count = 0
                async for msg in userbot.get_chat_history(target, limit=8):
                    text = (
                        getattr(msg, "text", None)
                        or getattr(msg, "caption", None)
                        or ""
                    ).strip()
                    if not text:
                        continue
                    msg_id = getattr(msg, "id", None)
                    url = ""
                    if value.startswith("@") and msg_id:
                        url = f"https://t.me/{value[1:]}/{msg_id}"
                    items.append(
                        {
                            "source": value,
                            "url": url,
                            "text": text,
                            "mode": mode,
                            "citation_enabled": citation_enabled,
                        }
                    )
                    count += 1
                    if count >= per_source_limit:
                        break
            except Exception as exc:
                logger.warning(
                    "AI source telegram history failed source={} error={!r}", value, exc
                )
                continue
        elif stype in {"url", "rss"} and value.startswith(("http://", "https://")):
            text = await fetch_public_source_text(value)
            if text:
                items.append(
                    {
                        "source": stype,
                        "url": value,
                        "text": text,
                        "mode": mode,
                        "citation_enabled": citation_enabled,
                    }
                )
    return items


@router.callback_query(F.data.startswith("ai_source_digest_"))
async def cb_ai_source_digest(callback: CallbackQuery, state: FSMContext):
    cid = int(callback.data.split("_")[-1])
    status_msg = await callback.message.answer("⏳ Собираю последние материалы из источников...")
    try:
        async with AsyncSessionLocal() as session:
            from app.repositories.ai_settings import AISourcesRepo

            repo = AISourcesRepo(session)
            sources = await repo.list_by_channel(cid)
        items = await _collect_digest_source_items(sources)
        context = build_source_digest_context(items)
        if not context:
            await status_msg.edit_text(
                "❌ Не нашёл свежий текст в источниках. Проверьте, что источники включены и userbot имеет доступ.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data=f"ai_source_list_{cid}")]]
                ),
            )
            return await callback.answer()

        await status_msg.edit_text("⏳ Генерирую черновик из источников...")
        source_summary = summarize_source_digest_modes(items)
        instruction = build_source_digest_instruction(
            len(items),
            modes=source_summary["modes"],
            citation_count=int(source_summary["citation_count"]),
        )
        async with AsyncSessionLocal() as session:
            service = AIGenerationService(session)
            result = await service.run_pipeline(
                channel_id=cid,
                mode="from_link",
                url="sources://digest",
                instruction=instruction,
                extra={"article_text": context, "link_mode": "summary"},
                user_id=int(callback.from_user.id),
                prompt_key=build_source_digest_prompt_key(cid, context),
            )
        if not result.get("success"):
            await status_msg.edit_text(
                f"❌ Не удалось сгенерировать черновик:\n{result.get('error', 'Неизвестная ошибка')}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data=f"ai_source_list_{cid}")]]
                ),
            )
            return await callback.answer()

        text = (result.get("text") or "").strip()
        payload = build_source_digest_payload(text)
        await state.clear()
        await state.set_state(PostFSM.preview)
        await state.update_data(
            channel_id=cid,
            payload=payload,
            notify_on=True,
            autosign_on=False,
            pin_on=False,
            comments_on=True,
            is_draft=True,
            is_ad=False,
            ui_settings={"ai_compose": True},
            ai_generation_history=remember_generation(
                None,
                mode="source_digest",
                input_text=build_source_digest_history_input(len(items), context),
                generated_text=text,
            ),
        )
        preview_kb = await build_preview_kb(await state.get_data())
        preview_msg = await callback.message.answer(text, reply_markup=preview_kb)
        await state.update_data(preview_msg_id=preview_msg.message_id)
        await status_msg.edit_text(
            f"✅ Черновик из источников готов. Материалов: {len(items)}\nТокены: {int(result.get('tokens_used', 0) or 0)}",
            reply_markup=_build_source_digest_done_kb(),
        )
        await callback.answer("Готово")
    except Exception as exc:
        logger.exception("AI source digest failed channel_id={}", cid)
        with suppress(TelegramBadRequest):
            await status_msg.edit_text(f"❌ Ошибка дайджеста источников: {str(exc)}")
        await callback.answer()


_DRAFT_VARIANTS = [
    ("analysis", "🧠 Разбор"),
    ("news", "📰 Новость"),
    ("sales", "💼 Продажи"),
    ("meme", "😂 Мем / лёгкий"),
    ("default", "📝 Обычный пост"),
]


@router.callback_query(F.data.startswith("ai_source_drafts_"))
async def cb_ai_source_drafts(callback: CallbackQuery, state: FSMContext):
    """Generate 3–5 drafts from channel sources for admin to choose."""
    cid = int(callback.data.split("_")[-1])
    status_msg = await callback.message.answer("⏳ Собираю материалы из источников...")
    try:
        async with AsyncSessionLocal() as session:
            from app.repositories.ai_settings import AISourcesRepo

            repo = AISourcesRepo(session)
            sources = await repo.list_by_channel(cid)
        items = await _collect_digest_source_items(sources)
        if not items:
            await status_msg.edit_text(
                "❌ Не нашёл свежий текст в источниках. Проверьте, что источники включены и userbot имеет доступ.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data=f"ai_source_list_{cid}")]]
                ),
            )
            return await callback.answer()

        source_summary = summarize_source_digest_modes(items)
        drafts: list[dict] = []

        for variant_code, variant_label in _DRAFT_VARIANTS:
            await status_msg.edit_text(
                f"⏳ Генерирую черновик: {variant_label}..."
            )
            instruction = build_source_digest_instruction(
                len(items),
                modes=source_summary["modes"],
                citation_count=int(source_summary["citation_count"]),
                variant=variant_code if variant_code != "default" else None,
            )
            context = build_source_digest_context(items)
            async with AsyncSessionLocal() as session:
                service = AIGenerationService(session)
                result = await service.run_pipeline(
                    channel_id=cid,
                    mode="from_link",
                    url="sources://digest",
                    instruction=instruction,
                    extra={"article_text": context, "link_mode": "summary"},
                    user_id=int(callback.from_user.id),
                    prompt_key=build_source_digest_prompt_key(cid, context) + f":{variant_code}",
                )
            if result.get("success"):
                text = (result.get("text") or "").strip()
                if text:
                    drafts.append(
                        {
                            "variant": variant_code,
                            "label": variant_label,
                            "text": text,
                            "tokens": int(result.get("tokens_used", 0) or 0),
                        }
                    )

        if not drafts:
            await status_msg.edit_text(
                "❌ Не удалось сгенерировать ни одного черновика. Попробуйте позже.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data=f"ai_source_list_{cid}")]]
                ),
            )
            return await callback.answer()

        await state.update_data(
            source_drafts_channel_id=cid,
            source_drafts=drafts,
            source_drafts_page=0,
        )

        await status_msg.delete()
        await _render_source_drafts(callback, cid, drafts, page=0, state=state)
        await callback.answer(f"Готово: {len(drafts)} черновиков")

    except Exception as exc:
        logger.exception("AI source draft generation failed channel_id={}", cid)
        with suppress(TelegramBadRequest):
            await status_msg.edit_text(f"❌ Ошибка: {str(exc)}")
        await callback.answer()


async def _render_source_drafts(
    callback: CallbackQuery,
    cid: int,
    drafts: list[dict],
    page: int,
    state: FSMContext,
) -> None:
    """Render a single draft with navigation and actions."""
    draft = drafts[page]
    total = len(drafts)
    text = (
        f"📝 Черновик {page + 1}/{total}: {draft['label']}\n"
        f"Токены: {draft['tokens']}\n\n"
        f"{draft['text'][:3000]}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="📤 Открыть в редакторе",
                callback_data=f"source_draft_open_{cid}_{page}",
            )
        ],
    ]
    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="← Назад", callback_data=f"source_draft_page_{cid}_{page - 1}"
            )
        )
    if page < total - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="Далее →", callback_data=f"source_draft_page_{cid}_{page + 1}"
            )
        )
    if nav_row:
        rows.append(nav_row)
    rows.append(
        [
            InlineKeyboardButton(
                text="← К источникам", callback_data=f"ai_source_list_{cid}"
            )
        ]
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("source_draft_page_"))
async def cb_source_draft_page(callback: CallbackQuery, state: FSMContext):
    """Navigate between drafts."""
    parts = callback.data.split("_")
    cid = int(parts[3])
    page = int(parts[4])
    data = await state.get_data()
    drafts = data.get("source_drafts", [])
    if not drafts or page >= len(drafts):
        return await callback.answer("Черновик не найден", show_alert=True)
    await state.update_data(source_drafts_page=page)
    await _render_source_drafts(callback, cid, drafts, page, state)
    await callback.answer()


@router.callback_query(F.data.startswith("source_draft_open_"))
async def cb_source_draft_open(callback: CallbackQuery, state: FSMContext):
    """Open a draft in the post editor."""
    parts = callback.data.split("_")
    cid = int(parts[3])
    page = int(parts[4])
    data = await state.get_data()
    drafts = data.get("source_drafts", [])
    if not drafts or page >= len(drafts):
        return await callback.answer("Черновик не найден", show_alert=True)
    draft = drafts[page]
    text = draft["text"]

    await state.clear()
    await state.set_state(PostFSM.preview)
    await state.update_data(
        channel_id=cid,
        payload={"type": "text", "text": text},
        notify_on=True,
        autosign_on=False,
        pin_on=False,
        comments_on=True,
        is_draft=True,
        is_ad=False,
        ai_generation_history=remember_generation(
            None,
            mode="source_draft",
            input_text=f"Источники: {draft['label']}",
            generated_text=text,
        ),
    )

    preview_kb = await build_preview_kb(await state.get_data())
    preview_msg = await callback.message.answer(text, reply_markup=preview_kb)
    await state.update_data(preview_msg_id=preview_msg.message_id)
    await callback.answer("Открыл в редакторе")


@router.callback_query(F.data.startswith("ai_source_toggle_"))
async def cb_ai_source_toggle(callback: CallbackQuery):
    cid, sid = map(int, callback.data.split("_")[-2:])
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import AISourcesRepo

        repo = AISourcesRepo(session)
        await repo.toggle_enabled(sid)
    await callback.answer("Готово")
    await _edit_ai_sources_list(callback, cid)


@router.callback_query(F.data.startswith("ai_source_cite_"))
async def cb_ai_source_cite(callback: CallbackQuery):
    cid, sid = map(int, callback.data.split("_")[-2:])
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import AISourcesRepo

        repo = AISourcesRepo(session)
        await repo.toggle_citation(sid)
    await callback.answer("Готово")
    await _edit_ai_sources_list(callback, cid)


@router.callback_query(F.data.startswith("ai_source_mode_"))
async def cb_ai_source_mode(callback: CallbackQuery):
    cid, sid = map(int, callback.data.split("_")[-2:])
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import AISourcesRepo

        repo = AISourcesRepo(session)
        source = await repo.get_by_id(sid)
        if not source:
            return await callback.answer("Источник не найден", show_alert=True)
        current = (source.mode or "summary").lower().strip()
        cycle = {
            "summary": "rewrite",
            "rewrite": "paraphrase",
            "paraphrase": "custom",
            "custom": "summary",
        }
        next_mode = cycle.get(current, "summary")
        await repo.update_mode(sid, next_mode)
    await callback.answer(f"Режим: {next_mode}")
    await _edit_ai_sources_list(callback, cid)


@router.callback_query(F.data.startswith("ai_source_delete_"))
async def cb_ai_source_delete(callback: CallbackQuery):
    cid, sid = map(int, callback.data.split("_")[-2:])
    async with AsyncSessionLocal() as session:
        from app.repositories.ai_settings import AISourcesRepo

        repo = AISourcesRepo(session)
        await repo.delete(sid)
    await callback.answer("Удалено")
    await _edit_ai_sources_list(callback, cid)
