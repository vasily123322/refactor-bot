from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_authority import (
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_legacy_transport_handoff import (
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed_pin_views_publication(
    Session,
    *,
    seed: int,
    now: datetime,
    runtime_options: dict | None = None,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=201000 + seed,
            username=f"pin-views-retirement-{seed}",
            full_name=f"Pin Views Retirement {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(201100 + seed),
            title=f"Pin Views Retirement {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Pin Views {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options=(
                {"pin_on": True, "autodelete_views": 100}
                if runtime_options is None
                else dict(runtime_options)
            ),
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_pin_views_handoff_requires_views_fact_then_canonical_claims(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-views-handoff.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 5, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_pin_views_publication(
                Session,
                seed=1,
                now=now,
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
                    allow_views_autodelete=True,
                )
                assert result.outcome == "retired"
                assert result.legacy_post_task_id == task_id

            async with Session() as claim_session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    claim_session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="canonical-pin-views-primary",
                    ttl_seconds=60,
                    now=now,
                    allow_views_autodelete=True,
                )
                assert claim is not None
                assert claim.plan.runtime_options() == {
                    "pin_on": True,
                    "autodelete_views": 100,
                }

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.attempt_count == 1
                assert publication.legacy_post_task_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_primary_without_views_worker_preserves_pin_views_legacy_claim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-views-no-worker.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id, task_id = await _seed_pin_views_publication(
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
                assert len(items) == 1
                assert items[0].status == "processing"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None and task.status == "processing"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_started_views_worker_retires_pin_views_before_legacy_claim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-views-worker.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id, task_id = await _seed_pin_views_publication(
                Session,
                seed=3,
                now=now,
            )
            set_canonical_publication_delivery_primary_worker(
                primary,
                views_autodelete_available=True,
            )
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


def test_pin_views_legacy_pin_drift_stays_legacy_owned(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-views-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 5, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_pin_views_publication(
                Session,
                seed=4,
                now=now,
            )

            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload.pop("pin_on", None)
                task.payload = payload
                await drift_session.commit()

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_views_autodelete=True,
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

    asyncio.run(run())


def test_pin_views_keeps_report_and_mixed_delete_closed(tmp_path) -> None:
    async def assert_ineligible(*, seed: int, runtime_options: dict, suffix: str) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'pin-views-{suffix}.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 5, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed_pin_views_publication(
                Session,
                seed=seed,
                now=now,
                runtime_options=runtime_options,
            )
            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_time_autodelete=True,
                    allow_views_autodelete=True,
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
            runtime_options={
                "pin_on": True,
                "autodelete_views": 100,
                "autodelete_report": True,
            },
            suffix="report-closed",
        )
        await assert_ineligible(
            seed=6,
            runtime_options={
                "pin_on": True,
                "autodelete_views": 100,
                "autodelete_seconds": 60,
            },
            suffix="mixed-delete-closed",
        )

    asyncio.run(run())
