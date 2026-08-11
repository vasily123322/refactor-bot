from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseService
from app.services.publication_autodelete_views_action_ledger import (
    PublicationAutodeleteViewsActionLedger,
    views_action_authority_fingerprint,
)
from app.services.publication_autodelete_views_state import PublicationAutodeleteViewStateService
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session, now: datetime) -> tuple[int, int, tuple[int, ...]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=216990,
            username="repeat-views-order",
            full_name="Repeat Views Order",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-100216990,
            title="Repeat Views Order",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "ordered deletes"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"silent": True, "autodelete_views": 17},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=17,
            now=now - timedelta(minutes=1),
        )
        ids = (86991, 86992, 86993)
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = list(ids)
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=list(ids),
                error=None,
                meta={"canonical_delivery": True},
                finished_at=now - timedelta(seconds=30),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id), int(channel.tg_chat_id), ids


def test_views_action_reservation_is_strict_ordered_prefix(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'views-order.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id, chat_id, ids = await _seed(Session, now)
            async with Session() as session:
                handle = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="views-order",
                    ttl_seconds=180,
                    allow_linked=True,
                )
                assert handle is not None

            fingerprint = views_action_authority_fingerprint(
                {
                    "version": 1,
                    "publication_id": publication_id,
                    "telegram_chat_id": chat_id,
                    "telegram_message_ids": list(ids),
                    "test": "strict-prefix",
                }
            )
            assert fingerprint is not None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                ledger = PublicationAutodeleteViewsActionLedger(session)
                skipped = await ledger.reserve_locked(
                    publication,
                    handle,
                    telegram_chat_id=chat_id,
                    telegram_message_id=ids[2],
                    expected_message_ids=ids,
                    authority_fingerprint=fingerprint,
                )
                assert skipped.outcome == "conflict"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                ledger = PublicationAutodeleteViewsActionLedger(session)
                first = await ledger.reserve_locked(
                    publication,
                    handle,
                    telegram_chat_id=chat_id,
                    telegram_message_id=ids[0],
                    expected_message_ids=ids,
                    authority_fingerprint=fingerprint,
                )
                assert first.outcome == "reserved"
                assert first.reservation is not None
                assert await ledger.mark_succeeded(first.reservation) is True

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                skipped = await PublicationAutodeleteViewsActionLedger(
                    session
                ).reserve_locked(
                    publication,
                    handle,
                    telegram_chat_id=chat_id,
                    telegram_message_id=ids[2],
                    expected_message_ids=ids,
                    authority_fingerprint=fingerprint,
                )
                assert skipped.outcome == "conflict"
        finally:
            await engine.dispose()

    asyncio.run(run())
