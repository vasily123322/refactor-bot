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


async def _seed(
    Session,
    *,
    seed: int,
    now: datetime,
    extra_options: dict | None = None,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=206000 + seed,
            username=f"pin-forward-views-{seed}",
            full_name=f"Pin Forward Views {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(206100 + seed * 10),
            title=f"Pin Forward Views Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(206101 + seed * 10),
            title=f"Pin Forward Views Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"PFV {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        options = {
            "pin_on": True,
            "forward_to": [int(target.id)],
            "silent": True,
            "autodelete_views": 100,
            **dict(extra_options or {}),
        }
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id), int(target.id)


def test_exact_pin_forward_views_retires_only_with_views_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-forward-views.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
            publication_id, task_id, target_id = await _seed(
                Session,
                seed=1,
                now=now,
            )

            async with Session() as disabled_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    disabled_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_forward=True,
                )
                assert result.outcome == "ineligible"

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_forward=True,
                    allow_views_autodelete=True,
                )
                assert result.outcome == "retired"

            async with Session() as claim_session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    claim_session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="canonical-pin-forward-views",
                    ttl_seconds=60,
                    now=now,
                    allow_views_autodelete=True,
                )
                assert claim is not None
                assert claim.plan.runtime_options() == {
                    "pin_on": True,
                    "forward_to": [target_id],
                    "silent": True,
                    "autodelete_views": 100,
                }

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None and publication.status == "sending"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_started_views_fact_routes_pin_forward_views_before_legacy_claim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-forward-views-scheduler.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(
                Session,
                seed=2,
                now=datetime.now(timezone.utc),
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
                assert publication.legacy_post_task_id is None
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_pin_forward_views_keeps_time_and_report_closed(tmp_path) -> None:
    async def assert_closed(*, seed: int, extra: dict, suffix: str) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'pin-forward-views-{suffix}.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
            publication_id, task_id, _ = await _seed(
                Session,
                seed=seed,
                now=now,
                extra_options=extra,
            )
            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_forward=True,
                    allow_time_autodelete=True,
                    allow_views_autodelete=True,
                )
                assert result.outcome == "ineligible"
            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                assert task is not None and task.status == "pending"
        finally:
            await engine.dispose()

    async def run() -> None:
        await assert_closed(
            seed=3,
            extra={"autodelete_seconds": 60},
            suffix="time-closed",
        )
        await assert_closed(
            seed=4,
            extra={"autodelete_report": True},
            suffix="report-closed",
        )

    asyncio.run(run())
