from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_authority import (
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed_pin_forward_publication(
    Session,
    *,
    seed: int,
    now: datetime,
    extra_options: dict | None = None,
) -> tuple[int, int, tuple[int, int], tuple[int, int]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=202000 + seed,
            username=f"pin-forward-retirement-{seed}",
            full_name=f"Pin Forward Retirement {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(202100 + seed * 10),
            title=f"Pin Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_one = Channel(
            tg_chat_id=-(202101 + seed * 10),
            title=f"Pin Forward Target A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_two = Channel(
            tg_chat_id=-(202102 + seed * 10),
            title=f"Pin Forward Target B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_one, target_two])
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Pin Forward {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        options = {
            "pin_on": True,
            "forward_to": [int(target_one.id), int(target_two.id)],
            "silent": True,
            **dict(extra_options or {}),
        }
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return (
            int(publication.id),
            int(publication.legacy_post_task_id),
            (int(target_one.id), int(target_one.tg_chat_id)),
            (int(target_two.id), int(target_two.tg_chat_id)),
        )


def test_pin_forward_handoff_retires_then_claims_ordered_targets(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-forward-handoff.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
            publication_id, task_id, target_one, target_two = (
                await _seed_pin_forward_publication(Session, seed=1, now=now)
            )

            async with Session() as disabled_session:
                disabled = await CanonicalPublicationLegacyTransportHandoffService(
                    disabled_session
                ).retire_for_canonical_delivery(publication_id, at=now)
                assert disabled.outcome == "ineligible"

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_forward=True,
                )
                assert result.outcome == "retired"
                assert result.legacy_post_task_id == task_id

            async with Session() as claim_session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    claim_session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="canonical-pin-forward-primary",
                    ttl_seconds=60,
                    now=now,
                )
                assert claim is not None
                assert claim.plan.runtime_options() == {
                    "pin_on": True,
                    "forward_to": [target_one[0], target_two[0]],
                    "silent": True,
                }

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                attempt = (
                    await check_session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert dict(attempt.meta or {})[FORWARD_TARGET_SNAPSHOT_META_KEY] == [
                    {
                        "channel_id": target_one[0],
                        "telegram_chat_id": target_one[1],
                    },
                    {
                        "channel_id": target_two[0],
                        "telegram_chat_id": target_two[1],
                    },
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_started_primary_retires_pin_forward_before_legacy_claim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-forward-scheduler.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id, task_id, _, _ = await _seed_pin_forward_publication(
                Session,
                seed=2,
                now=now,
            )
            set_canonical_publication_delivery_primary_worker(primary)
            scheduler = Scheduler(
                Session,
                SimpleNamespace(),
                repeat_continuation_enabled=False,
            )

            async with Session() as scheduler_session:
                task = await scheduler_session.get(PostTask, task_id)
                assert task is not None
                items = [task]
                await scheduler._mark_processing(scheduler_session, items)
                assert items == []

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.attempt_count == 0
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_pin_forward_pin_or_target_drift_stays_legacy_owned(tmp_path) -> None:
    async def assert_drift(*, seed: int, drift: str) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'pin-forward-{drift}.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
            publication_id, task_id, target_one, target_two = (
                await _seed_pin_forward_publication(Session, seed=seed, now=now)
            )
            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                if drift == "pin":
                    payload.pop("pin_on", None)
                else:
                    payload["forward_to"] = [target_two[0], target_one[0]]
                task.payload = payload
                await drift_session.commit()

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_forward=True,
                )
                assert result.outcome == "ineligible"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    async def run() -> None:
        await assert_drift(seed=3, drift="pin")
        await assert_drift(seed=4, drift="targets")

    asyncio.run(run())


def test_pin_forward_keeps_delete_compositions_closed(tmp_path) -> None:
    async def assert_ineligible(*, seed: int, extra_options: dict, suffix: str) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'pin-forward-{suffix}.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
            publication_id, task_id, _, _ = await _seed_pin_forward_publication(
                Session,
                seed=seed,
                now=now,
                extra_options=extra_options,
            )
            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_time_autodelete=True,
                    allow_views_autodelete=True,
                    allow_forward=True,
                )
                assert result.outcome == "ineligible"
            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    async def run() -> None:
        await assert_ineligible(
            seed=5,
            extra_options={"autodelete_seconds": 60},
            suffix="time-closed",
        )
        await assert_ineligible(
            seed=6,
            extra_options={"autodelete_views": 100},
            suffix="views-closed",
        )

    asyncio.run(run())
