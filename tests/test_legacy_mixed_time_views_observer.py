from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.legacy_time_views_delete_action import LegacyTimeViewsDeleteAction
from app.domain.models import Channel, Client, PostTask
from app.services.legacy_mixed_time_views_autodelete import (
    LegacyMixedTimeViewsAutodeleteObserver,
)
from app.services.legacy_time_views_delete_action_ledger import (
    LegacyTimeViewsDeleteActionLedger,
)
from app.services.publication_autodelete_views import _view_intent
from app.workers.scheduler import Scheduler


class _Views:
    def __init__(self, values: dict[int, int | None]):
        self.values = dict(values)
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target: int, message_id: int) -> int | None:
        self.calls.append((int(target), int(message_id)))
        return self.values.get(int(message_id))


class _Bot:
    def __init__(self, *, delete_error: Exception | None = None):
        self.delete_error = delete_error
        self.delete_calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.delete_calls.append((int(chat_id), int(message_id)))
        if self.delete_error is not None:
            raise self.delete_error

    async def send_message(self, *args, **kwargs) -> None:
        return None


async def _fixture(
    *,
    seconds: int = 3600,
    views: int = 25,
    message_ids: tuple[int, ...] = (701,),
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    async with Session() as session:
        owner = Client(tg_user_id=9001, username=None, full_name=None)
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1009001001,
            title="legacy mixed observer fixture",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        payload = {
            "autodelete_seconds": int(seconds),
            "autodelete_effective_seconds": int(seconds),
            "autodelete_views": int(views),
            "autodelete_at": (now + timedelta(seconds=max(seconds, 1))).isoformat(),
            "autodeleted": False,
            "result_ids": list(message_ids),
        }
        post = PostTask(
            channel_id=int(channel.id),
            status="done",
            payload=payload,
            dedupe_key=None,
            scheduled_at=now - timedelta(minutes=5),
        )
        session.add(post)
        await session.commit()
        return engine, Session, int(post.id), int(channel.tg_chat_id), message_ids


def _observer(Session, views: _Views, bot: _Bot) -> LegacyMixedTimeViewsAutodeleteObserver:
    return LegacyMixedTimeViewsAutodeleteObserver(
        view_source=views,
        delete_provider=bot,
        session_factory=Session,
        batch_size=25,
    )


def _scheduler(Session, bot: _Bot) -> Scheduler:
    scheduler = Scheduler(Session, SimpleNamespace(bot=bot), interval_seconds=1)
    scheduler._legacy_time_views_delete_ledger = LegacyTimeViewsDeleteActionLedger(
        Session
    )
    return scheduler


def test_mixed_below_threshold_does_not_delete() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _fixture()
        views = _Views({message_ids[0]: 24})
        bot = _Bot()
        try:
            tick = await _observer(Session, views, bot).run_once()
            assert tick.selected == 1
            assert tick.observed == 1
            assert tick.below_threshold == 1
            assert tick.delete_winners == 0
            assert bot.delete_calls == []
            async with Session() as session:
                actions = list(
                    (
                        await session.execute(
                            select(LegacyTimeViewsDeleteAction).where(
                                LegacyTimeViewsDeleteAction.post_task_id == post_id
                            )
                        )
                    ).scalars().all()
                )
                assert actions == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_views_threshold_wins_before_timer_exactly_once() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _fixture()
        views = _Views({message_ids[0]: 25})
        views_bot = _Bot()
        timer_bot = _Bot()
        try:
            first = await _observer(Session, views, views_bot).run_once()
            assert first.delete_winners == 1
            assert views_bot.delete_calls == [(chat_id, message_ids[0])]

            await _scheduler(Session, timer_bot)._del_later(
                timer_bot,
                chat_id,
                list(message_ids),
                0,
                post_id,
                False,
                None,
            )
            assert timer_bot.delete_calls == []

            async with Session() as session:
                action = (
                    await session.execute(
                        select(LegacyTimeViewsDeleteAction).where(
                            LegacyTimeViewsDeleteAction.post_task_id == post_id
                        )
                    )
                ).scalar_one()
                post = await session.get(PostTask, post_id)
                assert action.state == "succeeded"
                assert post is not None
                assert dict(post.payload or {}).get("autodeleted") is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_timer_wins_first_later_views_do_not_replay() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _fixture()
        timer_bot = _Bot()
        later_bot = _Bot()
        views = _Views({message_ids[0]: 100})
        try:
            await _scheduler(Session, timer_bot)._del_later(
                timer_bot,
                chat_id,
                list(message_ids),
                0,
                post_id,
                False,
                None,
            )
            assert timer_bot.delete_calls == [(chat_id, message_ids[0])]

            tick = await _observer(Session, views, later_bot).run_once()
            assert tick.selected == 0
            assert views.calls == []
            assert later_bot.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_restart_before_threshold_resumes_observation() -> None:
    async def run() -> None:
        engine, Session, _post_id, chat_id, message_ids = await _fixture()
        views = _Views({message_ids[0]: 10})
        first_bot = _Bot()
        second_bot = _Bot()
        try:
            first = await _observer(Session, views, first_bot).run_once()
            assert first.below_threshold == 1
            assert first_bot.delete_calls == []

            views.values[message_ids[0]] = 30
            restarted = await _observer(Session, views, second_bot).run_once()
            assert restarted.delete_winners == 1
            assert second_bot.delete_calls == [(chat_id, message_ids[0])]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_restart_after_committed_reservation_never_reauthorizes() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _fixture()
        views = _Views({message_ids[0]: 100})
        bot = _Bot()
        try:
            reserved = await LegacyTimeViewsDeleteActionLedger(Session).reserve(
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert reserved.outcome == "reserved"

            restarted = await _observer(Session, views, bot).run_once()
            assert restarted.selected == 0
            assert views.calls == []
            assert bot.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ambiguous_provider_result_remains_terminal_no_replay() -> None:
    async def run() -> None:
        engine, Session, post_id, chat_id, message_ids = await _fixture()
        views = _Views({message_ids[0]: 100})
        ambiguous_bot = _Bot(delete_error=RuntimeError("ambiguous timeout"))
        retry_bot = _Bot()
        try:
            first = await _observer(Session, views, ambiguous_bot).run_once()
            assert first.already_handled == 1
            assert ambiguous_bot.delete_calls == [(chat_id, message_ids[0])]

            async with Session() as session:
                action = (
                    await session.execute(
                        select(LegacyTimeViewsDeleteAction).where(
                            LegacyTimeViewsDeleteAction.post_task_id == post_id
                        )
                    )
                ).scalar_one()
                assert action.state == "unknown"

            replay = await _observer(Session, views, retry_bot).run_once()
            assert replay.selected == 0
            assert retry_bot.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_views_intent_still_rejects_mixed_profile() -> None:
    assert _view_intent(
        {"autodelete_seconds": 60, "autodelete_views": 25},
        allow_report=True,
    ) == (False, None, False)


def test_observer_does_not_claim_time_only_or_views_only() -> None:
    async def run_profile(*, seconds: int, views_threshold: int) -> None:
        engine, Session, post_id, chat_id, message_ids = await _fixture(
            seconds=seconds,
            views=views_threshold,
        )
        views = _Views({message_ids[0]: 100})
        bot = _Bot()
        try:
            tick = await _observer(Session, views, bot).run_once()
            assert tick.selected == 0
            assert views.calls == []
            assert bot.delete_calls == []
            ledger_result = await LegacyTimeViewsDeleteActionLedger(Session).reserve(
                post_task_id=post_id,
                chat_id=chat_id,
                message_ids=message_ids,
            )
            assert ledger_result.outcome == "not_applicable"
        finally:
            await engine.dispose()

    asyncio.run(run_profile(seconds=60, views_threshold=0))
    asyncio.run(run_profile(seconds=0, views_threshold=25))
