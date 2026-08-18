from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
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
            tg_user_id=208000 + seed,
            username=f"views-report-{seed}",
            full_name=f"Views Report {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(208100 + seed * 10),
            title=f"Views Report Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(source)
        await session.flush()

        options = dict(runtime_options)
        if "forward_to" in options:
            target = Channel(
                tg_chat_id=-(208101 + seed * 10),
                title=f"Views Report Target {seed}",
                owner_id=int(owner.id),
                is_active=True,
            )
            session.add(target)
            await session.flush()
            options["forward_to"] = [int(target.id)]

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Views report {seed}"}]
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


def test_views_report_requires_views_fact_then_canonical_claims(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=1,
                now=now,
                runtime_options={
                    "autodelete_views": 100,
                    "autodelete_report": True,
                },
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
                    holder="canonical-views-report",
                    ttl_seconds=60,
                    now=now,
                    allow_views_autodelete=True,
                )
                assert claim is not None
                assert claim.plan.runtime_options() == {
                    "autodelete_views": 100,
                    "autodelete_report": True,
                }

            async with Session() as check_session:
                assert await check_session.get(PostTask, task_id) is None
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                state = await check_session.get(
                    PublicationAutodeleteViewState,
                    publication_id,
                )
                assert state is not None
                assert int(state.threshold) == 100
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_views_report_family_includes_proven_pin_forward_composition(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-report-pin-forward.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
                Session,
                seed=2,
                now=now,
                runtime_options={
                    "pin_on": True,
                    "forward_to": [],
                    "silent": True,
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
                    allow_forward=True,
                    allow_views_autodelete=True,
                )
                assert result.outcome == "retired"
                assert result.legacy_post_task_id == task_id

            async with Session() as claim_session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    claim_session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="canonical-pin-forward-views-report",
                    ttl_seconds=60,
                    now=now,
                    allow_views_autodelete=True,
                )
                assert claim is not None
                options = claim.plan.runtime_options()
                assert options["pin_on"] is True
                assert options["autodelete_views"] == 100
                assert options["autodelete_report"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_views_report_scheduler_requires_started_views_fact(tmp_path) -> None:
    async def run_case(*, seed: int, views_available: bool) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'views-report-worker-{seed}.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed(
                Session,
                seed=seed,
                now=datetime.now(timezone.utc),
                runtime_options={
                    "autodelete_views": 100,
                    "autodelete_report": True,
                },
            )
            set_canonical_publication_delivery_primary_worker(
                primary,
                views_autodelete_available=views_available,
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
                if views_available:
                    assert items == []
                else:
                    assert len(items) == 1
                    assert items[0].status == "processing"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert publication is not None
                if views_available:
                    assert task is None
                    assert publication.status == "queued"
                    assert publication.legacy_post_task_id is None
                else:
                    assert task is not None and task.status == "processing"
                    assert publication.legacy_post_task_id == task_id
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    async def run() -> None:
        await run_case(seed=3, views_available=False)
        await run_case(seed=4, views_available=True)

    asyncio.run(run())


def test_views_report_keeps_invalid_compositions_closed(tmp_path) -> None:
    async def assert_closed(*, seed: int, runtime_options: dict, suffix: str) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'views-report-{suffix}.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
            publication_id, task_id = await _seed(
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
                    allow_forward=True,
                )
                assert result.outcome == "ineligible"
            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                assert task is not None and task.status == "pending"
        finally:
            await engine.dispose()

    async def run() -> None:
        await assert_closed(
            seed=5,
            runtime_options={
                "autodelete_views": 100,
                "autodelete_seconds": 60,
                "autodelete_report": True,
            },
            suffix="mixed-delete",
        )
        await assert_closed(
            seed=6,
            runtime_options={"autodelete_report": True},
            suffix="report-without-delete",
        )

    asyncio.run(run())
