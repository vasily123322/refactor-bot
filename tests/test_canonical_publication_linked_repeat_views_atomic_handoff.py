from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CUTOVER_META_KEY,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_linked_repeat_views(
    Session,
    *,
    seed: int,
    runtime_options: dict,
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=218000 + seed,
            username=f"linked-repeat-views-{seed}",
            full_name=f"Linked Repeat Views {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100218000 + seed),
            title=f"Linked Repeat Views {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(source)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"linked repeat views {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=runtime_options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_linked_repeat_views_missing_composition_fact_rolls_back_entire_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-repeat-views-rollback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked_repeat_views(
                Session,
                seed=1,
                runtime_options={"silent": True, "autodelete_views": 17},
            )

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="linked-repeat-views-missing-composition",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=False,
                )
                assert result.outcome == "claim_unavailable"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
                assert publication.legacy_post_task_id == task_id
                assert CUTOVER_META_KEY not in dict(publication.meta or {})
                assert task is not None
                assert task.status == "pending"
                assert state is None
                assert lease is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_repeat_views_all_facts_commit_cutover_and_indexed_intent_atomically(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-repeat-views-claimed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked_repeat_views(
                Session,
                seed=2,
                runtime_options={
                    "silent": True,
                    "autodelete_views": 23,
                    "autodelete_report": True,
                },
            )

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="linked-repeat-views-claimed",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                )
                assert result.outcome == "claimed"
                assert result.claim is not None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert int(publication.attempt_count or 0) == 1
                assert publication.legacy_post_task_id is None
                cutover = dict(publication.meta or {}).get(CUTOVER_META_KEY)
                assert isinstance(cutover, dict)
                assert cutover.get("retired") is True
                assert task is None
                assert state is not None
                assert int(state.threshold) == 23
                assert state.last_views is None
                assert lease is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_repeat_views_pin_composition_stays_parity_closed_with_all_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linked-repeat-views-pin-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked_repeat_views(
                Session,
                seed=3,
                runtime_options={"pin_on": True, "autodelete_views": 7},
            )

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="linked-repeat-views-pin-closed",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                )
                assert result.outcome == "ineligible"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == task_id
                assert task is not None and task.status == "pending"
                assert state is None
        finally:
            await engine.dispose()

    asyncio.run(run())
