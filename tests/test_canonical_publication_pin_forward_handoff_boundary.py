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
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_linked(Session, *, seed: int, runtime_options: dict) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=190000 + seed,
            username=f"handoff-boundary-{seed}",
            full_name=f"Handoff Boundary {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(190100 + seed),
            title=f"Handoff Boundary Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-(190200 + seed),
            title=f"Handoff Boundary Target {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()

        options = dict(runtime_options)
        if options.pop("__use_target__", False):
            options["forward_to"] = [int(target.id)]
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": f"Boundary {seed}"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=options,
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_linked_pin_and_forward_remain_legacy_owned_in_this_runtime_stage(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-forward-handoff-boundary.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            cases = [
                (1, {"pin_on": True}),
                (2, {"__use_target__": True}),
                (3, {"pin_on": True, "__use_target__": True, "silent": True}),
            ]
            for seed, options in cases:
                publication_id, task_id = await _seed_linked(
                    Session,
                    seed=seed,
                    runtime_options=options,
                )
                async with Session() as session:
                    result = await CanonicalPublicationLegacyTransportHandoffService(
                        session
                    ).retire_for_canonical_delivery(publication_id)
                    assert result.outcome == "ineligible"

                async with Session() as session:
                    publication = await session.get(Publication, publication_id)
                    task = await session.get(PostTask, task_id)
                    assert publication is not None
                    assert publication.status == "queued"
                    assert publication.legacy_post_task_id == task_id
                    assert task is not None
                    assert task.status == "pending"
        finally:
            await engine.dispose()

    asyncio.run(run())
