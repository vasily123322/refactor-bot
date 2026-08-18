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
    runtime_options: dict,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=207000 + seed,
            username=f"time-report-{seed}",
            full_name=f"Time Report {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(207100 + seed * 10),
            title=f"Time Report Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(source)
        await session.flush()

        options = dict(runtime_options)
        if "forward_to" in options:
            target = Channel(
                tg_chat_id=-(207101 + seed * 10),
                title=f"Time Report Target {seed}",
                owner_id=int(owner.id),
                is_active=True,
            )
            session.add(target)
            await session.flush()
            options["forward_to"] = [int(target.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Report {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_time_report_requires_time_fact_then_canonical_claims(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'time-report.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 11, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                now=now,
                runtime_options={
                    "autodelete_seconds": 60,
                    "autodelete_report": True,
                },
            )

            async with Session() as disabled_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    disabled_session
                ).retire_for_canonical_delivery(publication_id, at=now)
                assert result.outcome == "ineligible"

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_time_autodelete=True,
                )
                assert result.outcome == "retired"
                assert result.legacy_post_task_id == task_id

            async with Session() as claim_session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    claim_session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="canonical-time-report",
                    ttl_seconds=60,
                    now=now,
                    allow_time_autodelete=True,
                )
                assert claim is not None
                assert claim.plan.runtime_options() == {
                    "autodelete_seconds": 60,
                    "autodelete_report": True,
                }

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.legacy_post_task_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_time_report_family_includes_proven_pin_forward_composition(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'time-report-pin-forward.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 11, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=2,
                now=now,
                runtime_options={
                    "pin_on": True,
                    "forward_to": [],
                    "silent": True,
                    "autodelete_seconds": 60,
                    "autodelete_report": True,
                },
            )

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(
                    publication_id,
                    at=now,
                    allow_forward=True,
                    allow_time_autodelete=True,
                )
                assert result.outcome == "retired"
                assert result.legacy_post_task_id == task_id

            async with Session() as claim_session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    claim_session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="canonical-pin-forward-time-report",
                    ttl_seconds=60,
                    now=now,
                    allow_time_autodelete=True,
                )
                assert claim is not None
                assert claim.plan.runtime_options()["pin_on"] is True
                assert claim.plan.runtime_options()["autodelete_report"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_started_time_fact_retires_time_report_before_legacy_claim(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'time-report-scheduler.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=3,
                now=datetime.now(timezone.utc),
                runtime_options={
                    "autodelete_seconds": 60,
                    "autodelete_report": True,
                },
            )
            set_canonical_publication_delivery_primary_worker(
                primary,
                time_autodelete_available=True,
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


def test_views_report_remains_closed_in_time_report_slice(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 11, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=4,
                now=now,
                runtime_options={
                    "autodelete_views": 100,
                    "autodelete_report": True,
                },
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
                assert task is not None and task.status == "pending"
        finally:
            await engine.dispose()

    asyncio.run(run())
