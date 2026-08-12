from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryAction, PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt
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


class _Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_forward = False

    async def pin_chat_message(self, **kwargs) -> None:
        self.calls.append(("pin", dict(kwargs)))

    async def forward_message(self, **kwargs) -> None:
        self.calls.append(("forward", dict(kwargs)))
        if self.fail_forward:
            raise RuntimeError("provider-secret-must-not-be-retried")


async def _seed_claim(
    Session,
    *,
    seed: int,
    now: datetime,
    runtime_options: dict,
):
    async with Session() as session:
        owner = Client(
            tg_user_id=189000 + seed,
            username=f"pin-forward-{seed}",
            full_name=f"Pin Forward {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(189100 + seed),
            title=f"Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(189200 + seed),
            title=f"Target A {seed}",
            owner_id=int(owner.id),
            is_active=False,
        )
        target_b = Channel(
            tg_chat_id=-(189300 + seed),
            title=f"Target B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Pin forward runtime {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=1),
            runtime_options=runtime_options,
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
            holder="pin-forward-runtime-test",
            ttl_seconds=180,
            now=now,
        )
        return (
            int(publication.id),
            int(source.tg_chat_id),
            int(target_a.id),
            int(target_a.tg_chat_id),
            int(target_b.id),
            int(target_b.tg_chat_id),
            claim,
        )


def test_pin_forward_plan_and_executor_preserve_order_and_replay_zero_duplicates(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-forward-runtime.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)

            # First seed plain to learn stable target IDs, then update runtime before claim.
            async with Session() as session:
                owner = Client(
                    tg_user_id=189901,
                    username="pin-forward-main",
                    full_name="Pin Forward Main",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                source = Channel(
                    tg_chat_id=-100189901,
                    title="Pin Forward Source",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                target_a = Channel(
                    tg_chat_id=-100189902,
                    title="Pin Forward A",
                    owner_id=int(owner.id),
                    is_active=False,
                )
                target_b = Channel(
                    tg_chat_id=-100189903,
                    title="Pin Forward B",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                session.add_all([source, target_a, target_b])
                await session.commit()

                item = await ContentRepo(session).create(
                    channel_id=int(source.id),
                    document=PostDocument(
                        blocks=[
                            {
                                "id": "b1",
                                "type": "text",
                                "text": "Pin forward exact runtime",
                            }
                        ]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                runtime = {
                    "silent": True,
                    "pin_on": True,
                    "forward_to": [int(target_b.id), int(target_a.id)],
                }
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    scheduled_at=now - timedelta(minutes=1),
                    runtime_options=runtime,
                )
                task = await session.get(
                    PostTask,
                    int(publication.legacy_post_task_id or 0),
                )
                assert task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()

                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=int(publication.id),
                    holder="pin-forward-runtime-test",
                    ttl_seconds=180,
                    now=now,
                )
                assert claim is not None
                publication_id = int(publication.id)

            context = CanonicalPublicationDeliveryPostSendContext(
                publication_id=publication_id,
                lease=claim.lease,
                plan=claim.plan,
                message_ids=(2501, 2502),
                result_link="https://t.me/c/189901/2502",
                primary_finished_at=now + timedelta(seconds=1),
            )

            async with Session() as session:
                plan = await CanonicalPublicationDeliveryLivePostActionPlanner(
                    session
                ).plan(context, at=now + timedelta(seconds=1))
                assert plan is not None
                assert [action.action_key for action in plan.actions] == [
                    "pin:2502",
                    f"forward:{int(target_b.id)}:2501",
                    f"forward:{int(target_b.id)}:2502",
                    f"forward:{int(target_a.id)}:2501",
                    f"forward:{int(target_a.id)}:2502",
                ]
                assert plan.actions[0].source_message_id == 2502
                assert plan.actions[1].target_telegram_chat_id == -100189903
                assert plan.actions[3].target_telegram_chat_id == -100189902
                assert all(
                    action.disable_notification is True
                    for action in plan.actions
                    if action.action_type == "forward"
                )

            bot = _Bot()
            executor = CanonicalPublicationDeliveryLivePostActionExecutor(
                bot=bot,
                session_factory=Session,
            )
            first = await executor.execute(context)
            assert first.planned == 5
            assert first.reserved == 5
            assert first.succeeded == 5
            assert first.unknown == 0
            assert [name for name, _ in bot.calls] == [
                "pin",
                "forward",
                "forward",
                "forward",
                "forward",
            ]
            assert bot.calls[0][1] == {
                "chat_id": -100189901,
                "message_id": 2502,
            }
            assert bot.calls[1][1] == {
                "chat_id": -100189903,
                "from_chat_id": -100189901,
                "message_id": 2501,
                "disable_notification": True,
            }

            call_count = len(bot.calls)
            second = await executor.execute(context)
            assert second.planned == 5
            assert second.reserved == 0
            assert second.skipped_reserved == 5
            assert len(bot.calls) == call_count

            async with Session() as session:
                actions = (
                    await session.execute(
                        select(PublicationDeliveryAction)
                        .where(PublicationDeliveryAction.publication_id == publication_id)
                        .order_by(PublicationDeliveryAction.action_key.asc())
                    )
                ).scalars().all()
                assert len(actions) == 5
                assert {action.state for action in actions} == {"succeeded"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_claim_rejects_missing_forward_target_before_attempt_or_lease(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'pin-forward-missing-target.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)

            async with Session() as session:
                owner = Client(
                    tg_user_id=189990,
                    username="missing-forward-target",
                    full_name="Missing Forward Target",
                    ui_settings={},
                )
                session.add(owner)
                await session.flush()
                source = Channel(
                    tg_chat_id=-100189990,
                    title="Missing Target Source",
                    owner_id=int(owner.id),
                    is_active=True,
                )
                session.add(source)
                await session.commit()
                item = await ContentRepo(session).create(
                    channel_id=int(source.id),
                    document=PostDocument(
                        blocks=[{"id": "b1", "type": "text", "text": "Missing"}]
                    ),
                    created_by_tg_user_id=int(owner.tg_user_id),
                )
                publication = await LegacyPublicationBridge(session).queue(
                    content_item_id=int(item.id),
                    scheduled_at=now - timedelta(minutes=1),
                    runtime_options={"forward_to": [999999999]},
                )
                task = await session.get(
                    PostTask,
                    int(publication.legacy_post_task_id or 0),
                )
                assert task is not None
                publication.legacy_post_task_id = None
                await session.delete(task)
                await session.commit()
                publication_id = int(publication.id)

                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="missing-target",
                    ttl_seconds=180,
                    now=now,
                )
                assert claim is None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert publication.attempt_count == 0
                attempts = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert attempts == []
                assert await session.get(PublicationDeliveryLease, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
