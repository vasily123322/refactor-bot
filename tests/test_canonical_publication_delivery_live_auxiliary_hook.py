from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, ChannelSettings, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.admin import AdminConfigRepo
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.canonical_publication_delivery_live_auxiliary_executor import (
    CanonicalPublicationDeliveryLiveAuxiliaryExecution,
    CanonicalPublicationDeliveryLiveAuxiliaryExecutor,
)
from app.services.canonical_publication_delivery_live_auxiliary_hook import (
    CanonicalPublicationDeliveryLiveAuxiliaryHook,
)
from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlan,
    CanonicalPublicationLiveAdminLogPlan,
    CanonicalPublicationLiveOwnerNoticePlan,
)
from app.services.canonical_publication_result_link import (
    CanonicalPublicationResultLinkResolver,
)
from app.services.publication_bridge import LegacyPublicationBridge


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _dummy_session_factory():
    return _SessionContext()


def _unit_plan(publication_id: int = 7) -> CanonicalPublicationDeliveryLiveAuxiliaryPlan:
    return CanonicalPublicationDeliveryLiveAuxiliaryPlan(
        publication_id=publication_id,
        admin_log=CanonicalPublicationLiveAdminLogPlan(
            publication_id=publication_id,
            log_chat_id=-9000,
            source_telegram_chat_id=-100123,
            primary_message_id=77,
            result_link="https://t.me/c/123/77",
            author_tg_user_id=None,
            author_username=None,
            author_full_name=None,
        ),
        owner_notice=CanonicalPublicationLiveOwnerNoticePlan(
            publication_id=publication_id,
            owner_tg_user_id=501,
            owner_username=None,
            channel_title="Unit Channel",
            source_telegram_chat_id=-100123,
            result_link="https://t.me/c/123/77",
            delivered_count=1,
            timezone_code="UTC",
            local_date_iso="2026-08-11",
            local_date_text="11.08.2026",
            local_time_text="12:00",
            callback_data=f"cp_open_pub:{publication_id}:2026-08-11",
        ),
    )


class _RecordingExecutor:
    def __init__(self, *, cancel: bool = False) -> None:
        self.cancel = cancel
        self.calls: list[CanonicalPublicationDeliveryLiveAuxiliaryPlan] = []

    async def execute(self, plan):
        self.calls.append(plan)
        if self.cancel:
            raise asyncio.CancelledError
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id),
            admin_attempted=1 if plan.admin_log is not None else 0,
            admin_sent=1 if plan.admin_log is not None else 0,
            owner_attempted=1 if plan.owner_notice is not None else 0,
            owner_sent=1 if plan.owner_notice is not None else 0,
        )


def test_hook_replans_between_admin_and_owner(monkeypatch) -> None:
    async def run() -> None:
        from app.services import canonical_publication_delivery_live_auxiliary_hook as module

        plans = [_unit_plan(), _unit_plan()]
        contexts: list[object] = []

        class FakePlanner:
            def __init__(self, session) -> None:
                pass

            async def plan(self, context):
                contexts.append(context)
                return plans.pop(0)

        monkeypatch.setattr(module, "CanonicalPublicationDeliveryLiveAuxiliaryPlanner", FakePlanner)
        executor = _RecordingExecutor()
        hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
            executor=executor,
            session_factory=_dummy_session_factory,  # type: ignore[arg-type]
        )
        context = SimpleNamespace(publication_id=7)

        await hook.execute(context)  # type: ignore[arg-type]

        assert contexts == [context, context]
        assert len(executor.calls) == 2
        assert executor.calls[0].admin_log is not None
        assert executor.calls[0].owner_notice is None
        assert executor.calls[1].admin_log is None
        assert executor.calls[1].owner_notice is not None

    asyncio.run(run())


def test_hook_skips_owner_if_second_reauthorization_fails(monkeypatch) -> None:
    async def run() -> None:
        from app.services import canonical_publication_delivery_live_auxiliary_hook as module

        plans = [_unit_plan(), None]

        class FakePlanner:
            def __init__(self, session) -> None:
                pass

            async def plan(self, context):
                return plans.pop(0)

        monkeypatch.setattr(module, "CanonicalPublicationDeliveryLiveAuxiliaryPlanner", FakePlanner)
        executor = _RecordingExecutor()
        hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
            executor=executor,
            session_factory=_dummy_session_factory,  # type: ignore[arg-type]
        )

        await hook.execute(SimpleNamespace(publication_id=7))  # type: ignore[arg-type]

        assert len(executor.calls) == 1
        assert executor.calls[0].admin_log is not None
        assert executor.calls[0].owner_notice is None

    asyncio.run(run())


def test_hook_propagates_cancellation_without_second_planner_pass(monkeypatch) -> None:
    async def run() -> None:
        from app.services import canonical_publication_delivery_live_auxiliary_hook as module

        planner_calls = 0

        class FakePlanner:
            def __init__(self, session) -> None:
                pass

            async def plan(self, context):
                nonlocal planner_calls
                planner_calls += 1
                return _unit_plan()

        monkeypatch.setattr(module, "CanonicalPublicationDeliveryLiveAuxiliaryPlanner", FakePlanner)
        hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
            executor=_RecordingExecutor(cancel=True),
            session_factory=_dummy_session_factory,  # type: ignore[arg-type]
        )
        with pytest.raises(asyncio.CancelledError):
            await hook.execute(SimpleNamespace(publication_id=7))  # type: ignore[arg-type]
        assert planner_calls == 1

    asyncio.run(run())


class _Bot:
    def __init__(self, *, cancel_chat_id: int | None = None) -> None:
        self.cancel_chat_id = cancel_chat_id
        self.sent: list[int] = []

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(username="canonical_live_channel")

    async def create_chat_invite_link(self, **kwargs):
        return SimpleNamespace(invite_link="https://t.me/+CanonicalSafeInvite")

    async def send_message(self, chat_id: int, text: str, **kwargs):
        safe_chat_id = int(chat_id)
        self.sent.append(safe_chat_id)
        if self.cancel_chat_id == safe_chat_id:
            raise asyncio.CancelledError
        return SimpleNamespace(message_id=3101)


class _Sender:
    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        return [2101]


class _RuntimeDriftSender:
    def __init__(self, Session, publication_id: int) -> None:
        self.Session = Session
        self.publication_id = publication_id

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        async with self.Session() as session:
            publication = await session.get(Publication, self.publication_id)
            assert publication is not None
            schedule = await session.get(
                ScheduleEntry,
                int(publication.schedule_entry_id or 0),
            )
            assert schedule is not None
            changed = {"runtime_options": {"silent": True}}
            publication.meta = dict(changed)
            schedule.meta = dict(changed)
            await session.commit()
        return [2101]


async def _seed_plain_publication(Session, *, seed: int) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=137000 + seed,
            username=f"live-hook-owner-{seed}",
            full_name=f"Live Hook Owner {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100137000 + seed),
            title=f"Live Hook Channel {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        session.add(
            ChannelSettings(
                channel_id=int(channel.id),
                autosign=None,
                split_rules=None,
                filters={"tz": "UTC"},
            )
        )
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": "Live hook end-to-end proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        publication.legacy_post_task_id = None
        if task is not None:
            await session.delete(task)
        await session.commit()
        log_chat_id = -(998000 + seed)
        await AdminConfigRepo(session).set_log_chat(log_chat_id)
        return int(publication.id), log_chat_id


def _live_hook(Session, bot: _Bot) -> CanonicalPublicationDeliveryLiveAuxiliaryHook:
    return CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=CanonicalPublicationDeliveryLiveAuxiliaryExecutor(bot),
        session_factory=Session,
    )


def test_canonical_executor_runs_live_admin_and_owner_before_published_commit(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-hook-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, log_chat_id = await _seed_plain_publication(Session, seed=1)
            bot = _Bot()
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_Sender(),
                result_link_resolver=CanonicalPublicationResultLinkResolver(bot),
                post_send_hook=_live_hook(Session, bot),
            ).execute(publication_id)

            assert result.outcome == "published"
            assert bot.sent == [log_chat_id, 137001]
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "published"
                assert publication.telegram_message_ids == [2101]
                assert await session.get(PublicationDeliveryLease, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_auxiliary_cancellation_leaves_primary_delivery_ambiguous(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-hook-cancel.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, log_chat_id = await _seed_plain_publication(Session, seed=2)
            bot = _Bot(cancel_chat_id=log_chat_id)
            executor = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_Sender(),
                result_link_resolver=CanonicalPublicationResultLinkResolver(bot),
                post_send_hook=_live_hook(Session, bot),
            )
            with pytest.raises(asyncio.CancelledError):
                await executor.execute(publication_id)

            assert bot.sent == [log_chat_id]
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.telegram_message_ids is None
                assert await session.get(PublicationDeliveryLease, publication_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_send_intent_drift_authorizes_no_auxiliaries_and_cannot_publish(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-hook-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _log_chat_id = await _seed_plain_publication(Session, seed=3)
            bot = _Bot()
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=_RuntimeDriftSender(Session, publication_id),
                result_link_resolver=CanonicalPublicationResultLinkResolver(bot),
                post_send_hook=_live_hook(Session, bot),
                heartbeat_interval_seconds=120,
            ).execute(publication_id)

            assert result.outcome == "lease_lost"
            assert bot.sent == []
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.telegram_message_ids is None
                assert await session.get(PublicationDeliveryLease, publication_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
