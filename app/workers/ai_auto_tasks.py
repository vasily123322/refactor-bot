"""AI Auto Tasks Worker — runs scheduled AI tasks for channels.

Checks every 60 seconds which tasks are due and executes them:
- daily_topics: generate 3 topic suggestions
- evening_digest: generate a digest from sources
- weekly_ideas: collect best ideas of the week
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from contextlib import suppress

from loguru import logger
from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.domain.models import AIAutoTask, Channel, Client
from app.repositories.ai_auto_tasks import AIAutoTaskRepo
from app.bot.bot_instance import bot as tg_bot

# Task type constants
TASK_DAILY_TOPICS = "daily_topics"
TASK_EVENING_DIGEST = "evening_digest"
TASK_WEEKLY_IDEAS = "weekly_ideas"

ALL_TASK_TYPES = (TASK_DAILY_TOPICS, TASK_EVENING_DIGEST, TASK_WEEKLY_IDEAS)

TASK_LABELS = {
    TASK_DAILY_TOPICS: "📋 3 темы дня",
    TASK_EVENING_DIGEST: "📰 Вечерний дайджест",
    TASK_WEEKLY_IDEAS: "💡 Лучшие идеи недели",
}

TASK_DEFAULT_TIMES = {
    TASK_DAILY_TOPICS: "10:00",
    TASK_EVENING_DIGEST: "20:00",
    TASK_WEEKLY_IDEAS: "09:00",
}

TASK_DEFAULT_DAY = {
    TASK_DAILY_TOPICS: None,  # every day
    TASK_EVENING_DIGEST: None,  # every day
    TASK_WEEKLY_IDEAS: 0,  # Monday
}


def _parse_time(t: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    parts = t.split(":")
    return int(parts[0]), int(parts[1])


def _is_due(task: AIAutoTask, now: datetime) -> bool:
    """Check if a task should run at the given time."""
    if not task.enabled:
        return False

    task_hour, task_minute = _parse_time(task.run_at or "10:00")
    now_utc = now.astimezone(timezone.utc)

    # Check if we already ran this minute
    if task.last_run_at:
        last = task.last_run_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        # Don't run again within the same minute
        if (now_utc - last) < timedelta(minutes=1):
            return False

    # Check time match
    if now_utc.hour != task_hour or now_utc.minute != task_minute:
        return False

    # Check day of week for weekly tasks
    if task.schedule == "weekly" and task.day_of_week is not None:
        if now_utc.weekday() != task.day_of_week:
            return False

    return True


async def _run_daily_topics(task: AIAutoTask) -> str:
    """Generate 3 topic suggestions for the channel."""
    from app.services.ai_generation import AIGenerationService
    from app.repositories.ai_settings import ChannelAISettingsRepo

    async with AsyncSessionLocal() as session:
        ai_repo = ChannelAISettingsRepo(session)
        st = await ai_repo.get_or_create(task.channel_id)
        service = AIGenerationService(session)

        result = await service.run_pipeline(
            channel_id=task.channel_id,
            mode="from_scratch",
            topic="Предложи 3 интересные темы для постов в этом канале. "
                  "Каждая тема — одним предложением. "
                  "Нумеруй: 1. 2. 3.",
            user_id=0,
            prompt_key="auto_daily_topics",
        )

    if result["success"]:
        return f"📋 **3 темы дня**\n\n{result['text']}"
    return "📋 Не удалось сгенерировать темы. Попробуйте позже."


async def _run_evening_digest(task: AIAutoTask) -> str:
    """Generate an evening digest from channel sources."""
    try:
        from app.services.llm.source_digest import build_source_digest_context, build_source_digest_instruction
        from app.services.ai_generation import AIGenerationService
        from app.repositories.ai_settings import ChannelAISettingsRepo

        async with AsyncSessionLocal() as session:
            from app.repositories.ai_settings import AISourcesRepo
            sources = await AISourcesRepo(session).list_by_channel(task.channel_id)

        if not sources:
            return "📰 У канала нет источников для дайджеста."

        from app.bot.routers.sources import _collect_digest_source_items
        items = await _collect_digest_source_items(sources)
        if not items:
            return "📰 Нет новых материалов для дайджеста."

        async with AsyncSessionLocal() as session:
            ai_repo = ChannelAISettingsRepo(session)
            st = await ai_repo.get_or_create(task.channel_id)
            service = AIGenerationService(session)

            instruction = build_source_digest_instruction(
                items_count=len(items),
                variant="digest",
            )

            result = await service.run_pipeline(
                channel_id=task.channel_id,
                mode="from_scratch",
                topic=f"Сделай вечерний дайджест из последних новостей.\n\n{instruction}",
                user_id=0,
                prompt_key="auto_evening_digest",
            )

        if result["success"]:
            return f"📰 **Вечерний дайджест**\n\n{result['text']}"
        return "📰 Не удалось сгенерировать дайджест."
    except Exception as e:
        logger.exception(f"Auto digest error for channel {task.channel_id}: {e}")
        return "📰 Ошибка при генерации дайджеста."


async def _run_weekly_ideas(task: AIAutoTask) -> str:
    """Collect and suggest best ideas of the week."""
    from app.services.ai_generation import AIGenerationService
    from app.repositories.ai_settings import ChannelAISettingsRepo

    async with AsyncSessionLocal() as session:
        ai_repo = ChannelAISettingsRepo(session)
        st = await ai_repo.get_or_create(task.channel_id)
        service = AIGenerationService(session)

        result = await service.run_pipeline(
            channel_id=task.channel_id,
            mode="from_scratch",
            topic="Проанализируй канал и предложи 5 лучших идей для постов на следующую неделю. "
                  "Каждая идея — кратко, одним предложением. "
                  "Нумеруй: 1. 2. 3. 4. 5.",
            user_id=0,
            prompt_key="auto_weekly_ideas",
        )

    if result["success"]:
        return f"💡 **Лучшие идеи недели**\n\n{result['text']}"
    return "💡 Не удалось сгенерировать идеи. Попробуйте позже."


TASK_RUNNERS = {
    TASK_DAILY_TOPICS: _run_daily_topics,
    TASK_EVENING_DIGEST: _run_evening_digest,
    TASK_WEEKLY_IDEAS: _run_weekly_ideas,
}


class AIAutoTasksWorker:
    """Background worker that checks and runs scheduled AI tasks."""

    def __init__(self, interval_seconds: int = 60) -> None:
        self.interval_seconds = interval_seconds
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        logger.info("AIAutoTasksWorker: starting")
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="ai-auto-tasks")

    async def stop(self) -> None:
        logger.info("AIAutoTasksWorker: stopping")
        self._stopping.set()
        if self._task:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._task, timeout=5)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("AIAutoTasksWorker: tick error")
            await asyncio.sleep(self.interval_seconds)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as session:
            # Fetch all enabled tasks
            result = await session.execute(
                select(AIAutoTask).where(AIAutoTask.enabled == True)
            )
            tasks = list(result.scalars().all())

        for task in tasks:
            if not _is_due(task, now):
                continue

            logger.info(
                f"AIAutoTasksWorker: running {task.task_type} for channel {task.channel_id}"
            )

            runner = TASK_RUNNERS.get(task.task_type)
            if not runner:
                continue

            try:
                text = await runner(task)
                await self._send_to_owner(task.channel_id, text)
            except Exception:
                logger.exception(
                    f"AIAutoTasksWorker: error running {task.task_type} "
                    f"for channel {task.channel_id}"
                )
            finally:
                # Update last_run_at
                async with AsyncSessionLocal() as session:
                    repo = AIAutoTaskRepo(session)
                    await repo.update_last_run(task.channel_id, task.task_type, now)

    async def _send_to_owner(self, channel_id: int, text: str) -> None:
        """Send the result to the channel owner."""
        async with AsyncSessionLocal() as session:
            ch = await session.get(Channel, channel_id)
            if not ch:
                return
            owner = await session.get(Client, int(getattr(ch, "owner_id", 0)))
            if not owner or not getattr(owner, "tg_user_id", None):
                return
            user_id = int(owner.tg_user_id)

        with suppress(Exception):
            await tg_bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
