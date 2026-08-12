from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services import canonical_publication_linked_repeat_atomic_handoff as module
from app.services.canonical_publication_delivery_atomic_handoff_claim import CUTOVER_META_KEY
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.canonical_publication_linked_repeat_parity import (
    CanonicalPublicationLinkedRepeatParityProof,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_repeat_forward(Session) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=209001,
            username="repeat-strict-claim",
            full_name="Repeat Strict Claim",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-100209001,
            title="Repeat Strict Claim Source",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-100309001,
            title="Repeat Strict Claim Target",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "repeat + forward"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"forward_to": [int(target.id)]},
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


def test_atomic_repeat_handoff_uses_strict_repeat_claim_after_parity(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-atomic-strict-claim.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_repeat_forward(Session)

            class PermissiveFutureParity:
                def prove(self, *, task, publication, schedule, plan):
                    # Simulate the next parity widening before repeat+forward authority is
                    # intentionally widened. The strict repeat claim remains an
                    # independent final barrier and must roll prepared cutover back.
                    return CanonicalPublicationLinkedRepeatParityProof(
                        publication_id=int(publication.id),
                        legacy_post_task_id=int(task.id),
                        repeat_group_id=int(task.id),
                        repeat_seconds=60,
                        root_occurrence=True,
                    )

            monkeypatch.setattr(
                module,
                "CanonicalPublicationLinkedRepeatParityService",
                PermissiveFutureParity,
            )

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="repeat-strict-claim",
                    ttl_seconds=180,
                    allow_repeat=True,
                )
                assert result.outcome == "claim_unavailable"

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
                assert publication.legacy_post_task_id == task_id
                assert CUTOVER_META_KEY not in dict(publication.meta or {})
                assert task is not None and task.status == "pending"
                assert await session.get(PublicationDeliveryLease, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
