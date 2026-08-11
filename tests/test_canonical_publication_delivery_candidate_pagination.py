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
            tg_user_id=126000 + seed,
            username=f"candidate-page-{seed}",
            full_name=f"Candidate Page {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(126100 + seed),
            title=f"Candidate Page {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Page {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=scheduled_at,
            runtime_options={"silent": True},
        )
        return int(publication.id)


def test_keyset_cursor_advances_past_planner_invalid_front_page(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'candidate-pagination.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
            first = await _seed(Session, seed=1, scheduled_at=base)
            second = await _seed(
                Session,
                seed=2,
                scheduled_at=base + timedelta(seconds=1),
            )
            valid = await _seed(
                Session,
                seed=3,
                scheduled_at=base + timedelta(seconds=2),
            )

            async with Session() as session:
                for publication_id in (first, second):
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

                selector = CanonicalPublicationDeliveryCandidateSelector(session)
                first_page = await selector.scan_page(
                    limit=1,
                    scan_limit=2,
                    at=base + timedelta(minutes=1),
                )
                assert first_page.candidates == ()
                assert first_page.next_cursor is not None
                assert first_page.next_cursor.publication_id == second
                assert first_page.done is False

                second_page = await selector.scan_page(
                    limit=1,
                    scan_limit=2,
                    at=base + timedelta(minutes=1),
                    after=first_page.next_cursor,
                )
                assert [item.publication_id for item in second_page.candidates] == [valid]
                assert second_page.next_cursor is not None
                assert second_page.next_cursor.publication_id == valid
                assert second_page.done is True
        finally:
            await engine.dispose()

    asyncio.run(run())
