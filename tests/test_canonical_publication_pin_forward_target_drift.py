from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryAction
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_live_post_action_executor import (
    CanonicalPublicationDeliveryLivePostActionExecutor,
)
from app.services.canonical_publication_delivery_live_post_actions import (
    CanonicalPublicationDeliveryLivePostActionPlanner,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.publication_bridge import LegacyPublicationBridge


class _NoCallBot:
    def __init__(self) -> None:
        self.calls = 0

    async def pin_chat_message(self, **kwargs) -> None:
        self.calls += 1
        raise AssertionError("target drift must suppress provider calls")

    async def forward_message(self, **kwargs) -> None:
        self.calls += 1
        raise AssertionError("target drift must suppress provider calls")


async def _seed(Session, *, now: datetime):
    async with Session() as session:
        owner = Client(
            tg_user_id=191001,
            username="forward-target-drift",
            full_name="Forward Target Drift",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-100191001,
            title="Forward Drift Source",
            owner_id=int(owner.id),
            is_active=True,
        )
        target = Channel(
            tg_chat_id=-100191002,
            title="Forward Drift Target",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target])
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {"id": "b1", "type": "text", "text": "Forward drift proof"}
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options={"forward_to": [int(target.id)]},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()

        claim = await CanonicalPublicationDeliveryCapabilityClaimService(
            session
        ).claim_supported(
            publication_id=int(publication.id),
            holder="target-drift-test",
            ttl_seconds=180,
            now=now,
        )
        assert claim is not None
        return int(publication.id), int(target.id), claim


def test_forward_target_drift_after_claim_blocks_planning_before_reservation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-drift-before-reserve.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id, target_id, claim = await _seed(Session, now=now)
            context = CanonicalPublicationDeliveryPostSendContext(
                publication_id=publication_id,
                lease=claim.lease,
                plan=claim.plan,
                message_ids=(2601,),
                result_link=None,
                primary_finished_at=now + timedelta(seconds=1),
            )

            async with Session() as session:
                target = await session.get(Channel, target_id)
                assert target is not None
                target.tg_chat_id = -100191999
                await session.commit()

            async with Session() as session:
                assert (
                    await CanonicalPublicationDeliveryLivePostActionPlanner(
                        session
                    ).plan(context)
                    is None
                )
                rows = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_forward_target_drift_after_reservation_marks_suppressed_before_provider(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'forward-drift-after-reserve.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id, target_id, claim = await _seed(Session, now=now)
            context = CanonicalPublicationDeliveryPostSendContext(
                publication_id=publication_id,
                lease=claim.lease,
                plan=claim.plan,
                message_ids=(2602,),
                result_link=None,
                primary_finished_at=now + timedelta(seconds=1),
            )

            class _DriftAfterReserveExecutor(
                CanonicalPublicationDeliveryLivePostActionExecutor
            ):
                async def _reserve(self, context, action):
                    result = await super()._reserve(context, action)
                    if result.outcome == "reserved":
                        async with self.session_factory() as session:
                            target = await session.get(Channel, target_id)
                            assert target is not None
                            target.tg_chat_id = -100191998
                            await session.commit()
                    return result

            bot = _NoCallBot()
            result = await _DriftAfterReserveExecutor(
                bot=bot,
                session_factory=Session,
            ).execute(context)
            assert result.planned == 1
            assert result.reserved == 1
            assert result.suppressed == 1
            assert result.succeeded == 0
            assert bot.calls == 0

            async with Session() as session:
                rows = (
                    await session.execute(
                        select(PublicationDeliveryAction).where(
                            PublicationDeliveryAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].state == "suppressed"
        finally:
            await engine.dispose()

    asyncio.run(run())
