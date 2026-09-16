from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
)
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge


class _SnapshotCheckingSender:
    def __init__(self, Session, *, publication_id: int, expected_snapshot: list[dict]) -> None:
        self.Session = Session
        self.publication_id = publication_id
        self.expected_snapshot = expected_snapshot
        self.calls = 0

    async def send_document(
        self,
        chat_id: int,
        document: PostDocument,
        *,
        asset_channel_id: int | None = None,
    ) -> list[int]:
        self.calls += 1
        async with self.Session() as session:
            attempt = await session.get(
                PublicationAttempt,
                {"publication_id": self.publication_id, "attempt": 1},
            )
            assert attempt is not None
            assert attempt.status == "sending"
            assert dict(attempt.meta or {}).get(FORWARD_TARGET_SNAPSHOT_META_KEY) == (
                self.expected_snapshot
            )
        return [2801]


class _Hook:
    def __init__(self) -> None:
        self.contexts = []

    async def execute(self, context) -> None:
        self.contexts.append(context)


def test_executor_persists_forward_destination_snapshot_before_provider_call(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-snapshot-context.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)

            async with Session() as session:
                owner = Client(
                    tg_user_id=192001,
                    username="forward-snapshot-context",
                    full_name="Forward Snapshot Context",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                source = Channel(
                    tg_chat_id=-100192001,
                    title="Forward Snapshot Source",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                target = Channel(
                    tg_chat_id=-100192002,
                    title="Forward Snapshot Target",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                session.add_all([source, target])
                await session.commit()
                item = await ContentRepo(session).create(
                    channel_id=int(source.id),
                    document=PostDocument(
                        blocks=[
                            {
                                "id": "b1",
                                "type": "text",
                                "text": "Forward snapshot context",
                            }
                        ]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    scheduled_at=now - timedelta(minutes=1),
                    runtime_options={"forward_to": [int(target.id)]},
                )
                assert publication.legacy_post_task_id is None
                await session.commit()
                publication_id = int(publication.id)
                target_id = int(target.id)

            expected_snapshot = [
                {"channel_id": target_id, "telegram_chat_id": -100192002}
            ]
            sender = _SnapshotCheckingSender(
                Session,
                publication_id=publication_id,
                expected_snapshot=expected_snapshot,
            )
            hook = _Hook()
            result = await CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                post_send_hook=hook,
                heartbeat_interval_seconds=0.01,
            ).execute(publication_id, now=now)
            assert result.outcome == "published"
            assert sender.calls == 1
            assert len(hook.contexts) == 1
            assert hook.contexts[0].publication_id == publication_id

            async with Session() as session:
                attempt = await session.get(
                    PublicationAttempt,
                    {"publication_id": publication_id, "attempt": 1},
                )
                publication = await session.get(Publication, publication_id)
                assert attempt is not None
                assert publication is not None and publication.status == "published"
                assert dict(attempt.meta or {})[FORWARD_TARGET_SNAPSHOT_META_KEY] == (
                    expected_snapshot
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
