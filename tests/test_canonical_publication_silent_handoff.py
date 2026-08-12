from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_legacy_transport_handoff import (
    CanonicalPublicationLegacyTransportHandoffService,
)
from app.services.legacy_content_mirror import mirror_legacy_post_task
from app.services.publication_bridge import LegacyPublicationBridge


async def _channel(Session, *, seed: int) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=183000 + seed,
            username=f"silent-handoff-{seed}",
            full_name=f"Silent Handoff {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(183100 + seed),
            title=f"Silent Handoff {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        return int(channel.id), int(owner.tg_user_id)


async def _bridge_publication(
    Session,
    *,
    seed: int,
    silent: bool,
) -> tuple[int, int]:
    channel_id, owner_tg_id = await _channel(Session, seed=seed)
    async with Session() as session:
        item = await ContentRepo(session).create(
            channel_id=channel_id,
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Silent handoff {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=owner_tg_id,
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options={"silent": silent},
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_handoff_accepts_exact_explicit_silent_true_and_false(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-handoff-exact.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            for seed, intended in ((1, True), (2, False)):
                publication_id, task_id = await _bridge_publication(
                    Session,
                    seed=seed,
                    silent=intended,
                )
                async with Session() as before_session:
                    task = await before_session.get(PostTask, task_id)
                    assert task is not None
                    assert dict(task.payload or {}).get("silent") is intended

                async with Session() as handoff_session:
                    result = await CanonicalPublicationLegacyTransportHandoffService(
                        handoff_session
                    ).retire_for_canonical_delivery(publication_id)
                    assert result.outcome == "retired"

                async with Session() as check_session:
                    publication = await check_session.get(Publication, publication_id)
                    assert publication is not None
                    assert publication.status == "queued"
                    assert publication.legacy_post_task_id is None
                    assert await check_session.get(PostTask, task_id) is None
                    assert dict(publication.meta or {}).get("runtime_options") == {
                        "silent": intended
                    }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_handoff_rejects_silent_drift_and_restores_pending_transport(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-handoff-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _bridge_publication(
                Session,
                seed=3,
                silent=True,
            )

            async with Session() as drift_session:
                task = await drift_session.get(PostTask, task_id)
                assert task is not None
                payload = dict(task.payload or {})
                payload["silent"] = False
                task.payload = payload
                await drift_session.commit()

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(publication_id)
                assert result.outcome == "ineligible"

            async with Session() as check_session:
                task = await check_session.get(PostTask, task_id)
                publication = await check_session.get(Publication, publication_id)
                assert task is not None
                assert task.status == "pending"
                assert dict(task.payload or {}).get("silent") is False
                assert publication is not None
                assert publication.status == "queued"
                assert publication.legacy_post_task_id == task_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_hidden_mirrored_silent_true_is_not_inferred_as_canonical_intent(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'silent-handoff-hidden.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            channel_id, _owner_tg_id = await _channel(Session, seed=4)

            async with Session() as seed_session:
                task = PostTask(
                    channel_id=channel_id,
                    status="pending",
                    payload={
                        "type": "text",
                        "text": "hidden legacy silent",
                        "silent": True,
                    },
                    scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
                seed_session.add(task)
                await seed_session.commit()
                await seed_session.refresh(task)
                task_id = int(task.id)
                publication = await mirror_legacy_post_task(seed_session, task)
                assert publication is not None
                publication_id = int(publication.id)
                assert dict(publication.meta or {}).get("runtime_options") is None

            async with Session() as handoff_session:
                result = await CanonicalPublicationLegacyTransportHandoffService(
                    handoff_session
                ).retire_for_canonical_delivery(publication_id)
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
