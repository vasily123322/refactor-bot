from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_candidates import (
    CanonicalPublicationDeliveryCandidateSelector,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session, *, seed: int, scheduled_at: datetime) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=121000 + seed,
            username=f"candidate-regression-{seed}",
            full_name=f"Candidate Regression {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(121100 + seed),
            title=f"Candidate Regression {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Row {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options={"silent": True},
        )
        return int(publication.id)


def test_small_output_limit_does_not_starve_valid_candidate_behind_drifted_rows(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'candidate-starvation.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_ids: list[int] = []
            for index in range(6):
                publication_ids.append(
                    await _seed(
                        Session,
                        seed=index + 1,
                        scheduled_at=base + timedelta(seconds=index),
                    )
                )

            async with Session() as session:
                for publication_id in publication_ids[:5]:
                    publication = await session.get(Publication, publication_id)
                    assert publication is not None
                    schedule = await session.get(
                        ScheduleEntry,
                        int(publication.schedule_entry_id or 0),
                    )
                    assert schedule is not None
                    schedule.meta = {
                        **dict(schedule.meta or {}),
                        "runtime_options": {"silent": False},
                    }
                await session.commit()

                candidates = await CanonicalPublicationDeliveryCandidateSelector(
                    session
                ).due(limit=1, at=base + timedelta(minutes=1))
                assert [item.publication_id for item in candidates] == [
                    publication_ids[5]
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_empty_message_id_evidence_remains_planner_eligible(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'candidate-empty-evidence.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            publication_id = await _seed(Session, seed=20, scheduled_at=base)

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                publication.telegram_message_ids = []
                await session.commit()

                candidates = await CanonicalPublicationDeliveryCandidateSelector(
                    session
                ).due(limit=1, at=base + timedelta(minutes=1))
                assert [item.publication_id for item in candidates] == [publication_id]
        finally:
            await engine.dispose()

    asyncio.run(run())
