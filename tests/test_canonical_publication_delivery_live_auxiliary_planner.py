from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, ChannelSettings, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.admin import AdminConfigRepo
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
)
from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlanner,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_claim(
    Session,
    *,
    seed: int,
    scheduled_at: datetime,
    repeat_rule: dict | None = None,
):
    async with Session() as session:
        owner = Client(
            tg_user_id=136000 + seed,
            username=f"live-aux-owner-{seed}",
            full_name=f"Live Aux Owner {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100136000 + seed),
            title=f"Live Aux Channel {seed}",
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
                filters={"tz": "Europe/Paris"},
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
                        "text": "Live auxiliary planning proof",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options={},
            repeat_rule=repeat_rule,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()

        await AdminConfigRepo(session).set_log_chat(-(999000 + seed))
        claim = await CanonicalPublicationDeliveryClaimService(session).claim(
            publication_id=int(publication.id),
            holder="live-aux-test",
            now=scheduled_at + timedelta(seconds=1),
        )
        assert claim is not None
        return claim, int(channel.id), int(owner.id)


def _context(claim, *, finished_at: datetime):
    chat_id = str(int(claim.plan.telegram_chat_id))
    assert chat_id.startswith("-100")
    return CanonicalPublicationDeliveryPostSendContext(
        publication_id=int(claim.plan.publication_id),
        lease=claim.lease,
        plan=claim.plan,
        message_ids=(1901, 1902),
        result_link=f"https://t.me/c/{chat_id[4:]}/1902",
        primary_finished_at=finished_at,
    )


def test_live_auxiliary_planner_builds_owner_and_admin_before_terminal_commit(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-aux-planner.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim, _channel_id, _owner_id = await _seed_claim(
                Session,
                seed=1,
                scheduled_at=scheduled_at,
            )
            finished_at = scheduled_at + timedelta(seconds=2)

            async with Session() as session:
                plan = await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(
                    _context(claim, finished_at=finished_at),
                    at=finished_at,
                )
                assert plan is not None
                assert plan.publication_id == claim.plan.publication_id
                assert plan.owner_notice is not None
                assert plan.owner_notice.owner_tg_user_id == 136001
                assert plan.owner_notice.owner_username == "live-aux-owner-1"
                assert plan.owner_notice.channel_title == "Live Aux Channel 1"
                assert plan.owner_notice.result_link == "https://t.me/c/136001/1902"
                assert plan.owner_notice.delivered_count == 2
                assert plan.owner_notice.timezone_code == "Europe/Paris"
                assert plan.owner_notice.local_date_iso == "2026-08-11"
                assert plan.owner_notice.callback_data.startswith(
                    f"cp_open_pub:{claim.plan.publication_id}:"
                )

                assert plan.admin_log is not None
                assert plan.admin_log.log_chat_id == -999001
                assert plan.admin_log.primary_message_id == 1902
                assert plan.admin_log.author_tg_user_id == 136001
                assert plan.admin_log.author_username == "live-aux-owner-1"

                publication = await session.get(
                    Publication,
                    int(claim.plan.publication_id),
                )
                assert publication is not None
                assert publication.status == "sending"
                assert publication.telegram_message_ids is None
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id
                            == int(claim.plan.publication_id)
                        )
                    )
                ).scalar_one()
                assert attempt.status == "sending"
                assert attempt.finished_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_live_auxiliary_planner_uses_current_owner_after_primary_send(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-aux-owner-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim, channel_id, _owner_id = await _seed_claim(
                Session,
                seed=2,
                scheduled_at=scheduled_at,
            )
            finished_at = scheduled_at + timedelta(seconds=2)

            async with Session() as session:
                new_owner = Client(
                    tg_user_id=236002,
                    username="new-live-owner",
                    full_name="New Live Owner",
                    ui_settings={},
                )
                session.add(new_owner)
                await session.flush()
                channel = await session.get(Channel, channel_id)
                assert channel is not None
                channel.owner_id = int(new_owner.id)
                await session.commit()

            async with Session() as session:
                plan = await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(
                    _context(claim, finished_at=finished_at),
                    at=finished_at,
                )
                assert plan is not None
                assert plan.owner_notice is not None
                assert plan.owner_notice.owner_tg_user_id == 236002
                assert plan.owner_notice.owner_username == "new-live-owner"
                # Author provenance remains the immutable ContentItem creator.
                assert plan.admin_log is not None
                assert plan.admin_log.author_tg_user_id == 136002
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_suppresses_owner_notice_but_keeps_admin_log(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-aux-repeat.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim, _channel_id, _owner_id = await _seed_claim(
                Session,
                seed=3,
                scheduled_at=scheduled_at,
                repeat_rule={"enabled": True, "seconds": 3600},
            )
            finished_at = scheduled_at + timedelta(seconds=2)

            async with Session() as session:
                plan = await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(
                    _context(claim, finished_at=finished_at),
                    at=finished_at,
                )
                assert plan is not None
                assert plan.owner_notice is None
                assert plan.admin_log is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_malformed_owner_settings_fail_closed_only_for_owner_notice(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-aux-settings.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim, channel_id, _owner_id = await _seed_claim(
                Session,
                seed=4,
                scheduled_at=scheduled_at,
            )
            finished_at = scheduled_at + timedelta(seconds=2)

            async with Session() as session:
                settings = (
                    await session.execute(
                        select(ChannelSettings).where(
                            ChannelSettings.channel_id == channel_id
                        )
                    )
                ).scalar_one()
                settings.filters = ["malformed"]  # type: ignore[assignment]
                await session.commit()

            async with Session() as session:
                plan = await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(
                    _context(claim, finished_at=finished_at),
                    at=finished_at,
                )
                assert plan is not None
                assert plan.owner_notice is None
                assert plan.admin_log is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_claimed_intent_drift_blocks_auxiliaries_before_provider_actions(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-aux-intent-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

            runtime_claim, _channel_id, _owner_id = await _seed_claim(
                Session,
                seed=5,
                scheduled_at=scheduled_at,
            )
            runtime_finished = scheduled_at + timedelta(seconds=2)
            async with Session() as session:
                publication = await session.get(
                    Publication,
                    int(runtime_claim.plan.publication_id),
                )
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
            async with Session() as session:
                assert await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(
                    _context(runtime_claim, finished_at=runtime_finished),
                    at=runtime_finished,
                ) is None

            repeat_claim, _channel_id, _owner_id = await _seed_claim(
                Session,
                seed=6,
                scheduled_at=scheduled_at,
            )
            repeat_finished = scheduled_at + timedelta(seconds=2)
            async with Session() as session:
                publication = await session.get(
                    Publication,
                    int(repeat_claim.plan.publication_id),
                )
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id or 0),
                )
                assert schedule is not None
                schedule.repeat_rule = {"enabled": True, "seconds": 3600}
                await session.commit()
            async with Session() as session:
                assert await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(
                    _context(repeat_claim, finished_at=repeat_finished),
                    at=repeat_finished,
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_source_destination_or_lease_drift_blocks_entire_auxiliary_plan(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live-aux-core-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            scheduled_at = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            claim, channel_id, _owner_id = await _seed_claim(
                Session,
                seed=7,
                scheduled_at=scheduled_at,
            )
            finished_at = scheduled_at + timedelta(seconds=2)
            context = _context(claim, finished_at=finished_at)

            async with Session() as session:
                channel = await session.get(Channel, channel_id)
                assert channel is not None
                channel.tg_chat_id = int(channel.tg_chat_id) - 500000
                await session.commit()

            async with Session() as session:
                assert await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(context, at=finished_at) is None

            # Restore destination, then prove the exact TTL boundary is also a barrier.
            async with Session() as session:
                channel = await session.get(Channel, channel_id)
                assert channel is not None
                channel.tg_chat_id = int(claim.plan.telegram_chat_id)
                await session.commit()

            async with Session() as session:
                assert await CanonicalPublicationDeliveryLiveAuxiliaryPlanner(
                    session
                ).plan(context, at=claim.lease.expires_at) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
