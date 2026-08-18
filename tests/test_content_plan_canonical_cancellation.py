from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.domain.scheduler import SchedulerTaskLease
from app.services.canonical_publication_delivery_candidates import (
    CanonicalPublicationDeliveryCandidateSelector,
)
from app.services.content_plan_cancellation import ContentPlanCancellationService
from app.services.posting import PostingService


class _Bot:
    pass


async def _new_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _channel(Session, suffix: int) -> Channel:
    async with Session() as session:
        owner = Client(
            tg_user_id=9_930_000 + suffix,
            username=f"cancel{suffix}",
            full_name="Canonical Cancellation Fixture",
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1_009_930_000_000 - suffix,
            title=f"Cancel {suffix}",
            owner_id=int(owner.id),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel


async def _linked(Session, suffix: int, *, when: datetime | None = None):
    channel = await _channel(Session, suffix)
    scheduled_at = when or datetime.now(timezone.utc) + timedelta(hours=1)
    task = await PostingService(_Bot(), Session).schedule(
        int(channel.id),
        {"type": "text", "text": f"Cancellation {suffix}"},
        scheduled_at,
        dedupe_key=f"canonical-cancel-{suffix}",
    )
    async with Session() as session:
        publication = (
            await session.execute(
                select(Publication).where(
                    Publication.legacy_post_task_id == int(task.id)
                )
            )
        ).scalar_one()
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert schedule is not None
        return (
            int(channel.id),
            int(task.id),
            int(publication.id),
            int(schedule.id),
            scheduled_at,
        )


def test_queued_linked_delete_cancels_publication_before_retiring_task() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _, task_id, publication_id, _, _ = await _linked(Session, 1)
            result = await ContentPlanCancellationService(Session).delete(task_id)
            assert result.outcome == "cancelled"
            assert result.compatibility_retired is True

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None
                assert publication.status == "cancelled"
                assert publication.legacy_post_task_id is None
                assert task is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_delete_cancels_schedule_entry_too() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _, task_id, _, schedule_id, _ = await _linked(Session, 2)
            result = await ContentPlanCancellationService(Session).delete(task_id)
            assert result.outcome == "cancelled"
            async with Session() as session:
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert schedule is not None
                assert schedule.status == "cancelled"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cancelled_publication_is_removed_from_canonical_candidate_selector() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            when = datetime.now(timezone.utc) + timedelta(minutes=20)
            _, task_id, publication_id, _, _ = await _linked(Session, 3, when=when)
            scan_at = when + timedelta(minutes=1)
            async with Session() as session:
                before = await CanonicalPublicationDeliveryCandidateSelector(session).due(
                    at=scan_at
                )
                assert publication_id in {row.publication_id for row in before}

            await ContentPlanCancellationService(Session).delete(task_id)

            async with Session() as session:
                after = await CanonicalPublicationDeliveryCandidateSelector(session).due(
                    at=scan_at
                )
                assert publication_id not in {row.publication_id for row in after}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_compatibility_task_is_not_retired_until_canonical_cancellation_is_durable() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _, task_id, publication_id, schedule_id, _ = await _linked(Session, 4)
            service = ContentPlanCancellationService(Session)
            observed = False

            async def observe_before_retirement(cancellation) -> bool:
                nonlocal observed
                async with Session() as session:
                    publication = await session.get(Publication, publication_id)
                    schedule = await session.get(ScheduleEntry, schedule_id)
                    task = await session.get(PostTask, task_id)
                    assert publication is not None and schedule is not None and task is not None
                    assert publication.status == "cancelled"
                    assert publication.legacy_post_task_id == task_id
                    assert schedule.status == "cancelled"
                    assert task.status == "cancelled"
                    observed = True
                return False

            service._retire_compatibility = observe_before_retirement  # type: ignore[method-assign]
            result = await service.delete(task_id)
            assert observed is True
            assert result.outcome == "cancelled"
            assert result.compatibility_retired is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_attempt_delivery_lease_or_scheduler_lease_fail_closed_without_task_delete() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            fixtures = [await _linked(Session, suffix) for suffix in (5, 6, 7)]
            now = datetime.now(timezone.utc)
            async with Session() as session:
                _, attempt_task, attempt_pub, _, _ = fixtures[0]
                session.add(
                    PublicationAttempt(
                        publication_id=attempt_pub,
                        attempt=1,
                        status="sending",
                    )
                )

                _, lease_task, lease_pub, _, _ = fixtures[1]
                session.add(
                    PublicationDeliveryLease(
                        publication_id=lease_pub,
                        lease_token="canonical-cancel-delivery-lease",
                        holder="fixture",
                        expires_at=now + timedelta(minutes=5),
                    )
                )

                _, scheduler_task, _, _, _ = fixtures[2]
                session.add(
                    SchedulerTaskLease(
                        task_id=scheduler_task,
                        lease_token="canonical-cancel-scheduler-lease",
                        holder="fixture",
                        expires_at=now - timedelta(minutes=5),
                    )
                )
                await session.commit()

            service = ContentPlanCancellationService(Session)
            for _, task_id, publication_id, _, _ in fixtures:
                result = await service.delete(task_id)
                assert result.outcome == "cannot_cancel"
                async with Session() as session:
                    task = await session.get(PostTask, task_id)
                    publication = await session.get(Publication, publication_id)
                    assert task is not None
                    assert task.status == "pending"
                    assert publication is not None
                    assert publication.status == "queued"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sending_or_ambiguous_publication_fails_closed() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _, task_id, publication_id, _, _ = await _linked(Session, 8)
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                publication.status = "sending"
                await session.commit()

            result = await ContentPlanCancellationService(Session).delete(task_id)
            assert result.outcome == "cannot_cancel"
            assert result.reason == "publication_sending"
            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None and publication.status == "sending"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unlinked_intentional_legacy_fallback_keeps_legacy_delete_semantics() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 9)
            async with Session() as session:
                task = PostTask(
                    channel_id=int(channel.id),
                    status="pending",
                    payload={"type": "unsupported_cancel_fixture", "text": "legacy"},
                    dedupe_key="legacy-content-plan-delete",
                    scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
                session.add(task)
                await session.commit()
                task_id = int(task.id)

            result = await ContentPlanCancellationService(Session).delete(task_id)
            assert result.outcome == "legacy_deleted"
            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                publication = (
                    await session.execute(
                        select(Publication).where(
                            Publication.legacy_post_task_id == task_id
                        )
                    )
                ).scalar_one_or_none()
                assert publication is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unlinked_time_views_remains_legacy_owned_and_is_not_canonicalized() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            channel = await _channel(Session, 10)
            async with Session() as session:
                task = PostTask(
                    channel_id=int(channel.id),
                    status="pending",
                    payload={
                        "type": "text",
                        "text": "legacy mixed owner",
                        "autodelete_seconds": 600,
                        "delete_after_views": 100,
                    },
                    dedupe_key="legacy-mixed-content-plan-delete",
                    scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
                session.add(task)
                await session.commit()
                task_id = int(task.id)

            result = await ContentPlanCancellationService(Session).delete(task_id)
            assert result.outcome == "legacy_deleted"
            async with Session() as session:
                assert await session.get(PostTask, task_id) is None
                count = len(list((await session.execute(select(Publication))).scalars()))
                assert count == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_published_linked_row_is_not_raw_deleted_or_treated_as_provider_delete() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _, task_id, publication_id, schedule_id, _ = await _linked(Session, 11)
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None and schedule is not None and task is not None
                publication.status = "published"
                schedule.status = "completed"
                task.status = "done"
                await session.commit()

            # The cancellation authority has no Telegram/provider dependency. A linked
            # published row is therefore protected instead of being reinterpreted as a
            # destructive provider action.
            result = await ContentPlanCancellationService(Session).delete(task_id)
            assert result.outcome == "cannot_cancel"
            assert result.reason == "publication_published"
            async with Session() as session:
                assert await session.get(PostTask, task_id) is not None
                publication = await session.get(Publication, publication_id)
                assert publication is not None and publication.status == "published"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeated_delete_is_terminal_and_does_not_resurrect_work() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _, task_id, publication_id, schedule_id, _ = await _linked(Session, 12)
            service = ContentPlanCancellationService(Session)
            first = await service.delete(task_id)
            second = await service.delete(task_id)
            assert first.outcome == "cancelled"
            assert second.outcome == "already_absent"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                schedule = await session.get(ScheduleEntry, schedule_id)
                assert publication is not None and publication.status == "cancelled"
                assert publication.legacy_post_task_id is None
                assert schedule is not None and schedule.status == "cancelled"
                assert await session.get(PostTask, task_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
