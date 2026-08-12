from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services import canonical_publication_repeat_handoff_executor as routing_module
from app.services.canonical_publication_delivery_atomic_handoff_claim import (
    CUTOVER_META_KEY,
    CanonicalPublicationAtomicHandoffClaimResult,
)
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
)
from app.services.canonical_publication_linked_repeat_atomic_handoff import (
    CanonicalPublicationLinkedRepeatAtomicHandoffService,
)
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed(Session, *, seed: int) -> tuple[int, int, list[dict[str, int]]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=236000 + seed,
            username=f"views-pin-forward-linked-{seed}",
            full_name=f"Views Pin Forward Linked {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100236000 + seed),
            title=f"Combined Linked Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100336000 + seed),
            title=f"Combined Linked A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100436000 + seed),
            title=f"Combined Linked B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        ordered = [int(target_b.id), int(target_a.id)]
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "combined linked"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "pin_on": True,
                "forward_to": ordered,
                "autodelete_views": 53,
            },
        )
        assert publication.legacy_post_task_id is not None
        snapshot = [
            {
                "channel_id": int(target_b.id),
                "telegram_chat_id": int(target_b.tg_chat_id),
            },
            {
                "channel_id": int(target_a.id),
                "telegram_chat_id": int(target_a.tg_chat_id),
            },
        ]
        return int(publication.id), int(publication.legacy_post_task_id), snapshot


async def _assert_rolled_back(Session, publication_id: int, task_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        task = await session.get(PostTask, task_id)
        state = await session.get(PublicationAutodeleteViewState, publication_id)
        lease = await session.get(PublicationDeliveryLease, publication_id)
        attempts = list(
            (
                await session.execute(
                    select(PublicationAttempt).where(
                        PublicationAttempt.publication_id == publication_id
                    )
                )
            ).scalars().all()
        )
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        assert publication.legacy_post_task_id == task_id
        assert CUTOVER_META_KEY not in dict(publication.meta or {})
        assert task is not None and task.status == "pending"
        assert state is None
        assert lease is None
        assert attempts == []


def test_combined_linked_missing_narrow_fact_rolls_back_despite_independent_facts(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-linked-rollback.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, _ = await _seed(Session, seed=1)

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="combined-linked-missing",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                    allow_repeat_views_forward=True,
                    allow_repeat_views_pin_forward=False,
                )
                assert result.outcome == "claim_unavailable"
            await _assert_rolled_back(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_combined_linked_exact_fact_commits_cutover_threshold_and_snapshot(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-linked-open.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id, expected_snapshot = await _seed(Session, seed=2)

            async with Session() as session:
                result = await CanonicalPublicationLinkedRepeatAtomicHandoffService(
                    session
                ).claim_linked_repeat(
                    publication_id,
                    holder="combined-linked-exact",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                    allow_repeat_views_forward=True,
                    allow_repeat_views_pin_forward=True,
                )
                assert result.outcome == "claimed"
                assert result.claim is not None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                lease = await session.get(PublicationDeliveryLease, publication_id)
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert publication is not None
                assert publication.status == "sending"
                assert int(publication.attempt_count or 0) == 1
                assert publication.legacy_post_task_id is None
                assert task is None
                assert state is not None and int(state.threshold) == 53
                assert lease is not None
                assert dict(attempt.meta or {}).get(FORWARD_TARGET_SNAPSHOT_META_KEY) == expected_snapshot
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_combined_router_forwards_exact_narrow_fact(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'combined-routing.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _, _ = await _seed(Session, seed=3)
            captured: dict[str, object] = {}

            class FakeTransfer:
                def __init__(self, session) -> None:
                    pass

                async def claim_linked_repeat(self, publication_id: int, **kwargs):
                    captured.update(kwargs)
                    return CanonicalPublicationAtomicHandoffClaimResult(
                        publication_id=publication_id,
                        outcome="ineligible",
                    )

            monkeypatch.setattr(
                routing_module,
                "CanonicalPublicationLinkedRepeatAtomicHandoffService",
                FakeTransfer,
            )

            class Executor:
                holder = "combined-routing"
                lease_seconds = 180
                allow_repeat = True
                allow_views_autodelete = True
                allow_repeat_views = True
                allow_repeat_views_pin = True
                allow_repeat_views_forward = True
                allow_repeat_views_pin_forward = True

                async def execute(self, publication_id: int):
                    raise AssertionError("linked repeat must not route direct")

            router = CanonicalPublicationRepeatHandoffExecutor(
                executor=Executor(),  # type: ignore[arg-type]
                session_factory=Session,
            )
            result = await router.execute(publication_id)
            assert result.outcome == "ineligible"
            assert captured["allow_repeat_views_pin_forward"] is True
            assert captured["allow_repeat_views_pin"] is True
            assert captured["allow_repeat_views_forward"] is True
        finally:
            await engine.dispose()

    asyncio.run(run())
