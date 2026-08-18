from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CanonicalPublicationAtomicHandoffClaimService,
)
from app.services.canonical_publication_delivery_authority import (
    set_canonical_publication_delivery_primary_worker,
)
from app.services.canonical_publication_nonrepeat_authority import (
    canonical_publication_delivery_nonrepeat_pin_started,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.workers.canonical_repeat_continuation_scheduler import Scheduler


class _PrimaryWorker:
    pass


async def _seed(
    Session,
    *,
    seed: int,
    runtime_options: dict,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=220000 + seed,
            username=f"nonrepeat-pin-{seed}",
            full_name=f"Nonrepeat Pin {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(102520000 + seed),
            title=f"Nonrepeat Pin Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(102620000 + seed),
            title=f"Nonrepeat Pin Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.flush()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Pin {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        options = dict(runtime_options)
        if options.pop("_use_target", False):
            options["forward_to"] = [int(target.id)]
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id), int(target.id)


async def _mark(Session, task_id: int) -> list[PostTask]:
    scheduler = Scheduler(Session, SimpleNamespace(), repeat_continuation_enabled=False)
    async with Session() as session:
        task = await session.get(PostTask, task_id)
        assert task is not None
        items = [task]
        await scheduler._mark_processing(session, items)
        return items


def test_pin_started_is_profile_specific_primary_fact() -> None:
    primary = _PrimaryWorker()
    try:
        set_canonical_publication_delivery_primary_worker(None)
        assert canonical_publication_delivery_nonrepeat_pin_started() is False
        set_canonical_publication_delivery_primary_worker(primary)
        assert canonical_publication_delivery_nonrepeat_pin_started() is True
    finally:
        set_canonical_publication_delivery_primary_worker(None)


def test_scheduler_yields_exact_pin_then_atomic_claim_owns_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'nonrepeat-pin-yield.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(
                Session,
                seed=1,
                runtime_options={"pin_on": True},
            )
            set_canonical_publication_delivery_primary_worker(primary)

            assert await _mark(Session, task_id) == []
            async with Session() as check:
                task = await check.get(PostTask, task_id)
                publication = await check.get(Publication, publication_id)
                assert task is not None and task.status == "pending"
                assert publication is not None
                assert publication.legacy_post_task_id == task_id

            async with Session() as session:
                claimed = await CanonicalPublicationAtomicHandoffClaimService(
                    session
                ).claim_linked(
                    publication_id,
                    holder="nonrepeat-pin-canonical",
                    ttl_seconds=180,
                )
                assert claimed.outcome == "claimed"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_missing_pin_fact_preserves_legacy(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'nonrepeat-pin-missing.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id, _ = await _seed(
                Session,
                seed=2,
                runtime_options={"pin_on": True},
            )
            set_canonical_publication_delivery_primary_worker(primary)
            monkeypatch.setattr(
                "app.services.canonical_publication_nonrepeat_scheduler_proof."
                "canonical_publication_delivery_nonrepeat_pin_started",
                lambda: False,
            )
            items = await _mark(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())


def test_pin_slice_keeps_exact_forward_on_legacy(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'nonrepeat-pin-forward-closed.db'}"
        )
        primary = _PrimaryWorker()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            _, task_id, _ = await _seed(
                Session,
                seed=3,
                runtime_options={"_use_target": True},
            )
            set_canonical_publication_delivery_primary_worker(primary)
            items = await _mark(Session, task_id)
            assert len(items) == 1 and items[0].status == "processing"
        finally:
            set_canonical_publication_delivery_primary_worker(None)
            await engine.dispose()

    asyncio.run(run())
