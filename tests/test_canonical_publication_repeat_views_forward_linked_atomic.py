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
    CanonicalPublicationAtomicHandoffClaimResult,
)
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
)
from app.services.canonical_publication_delivery_atomic_handoff_claim import CUTOVER_META_KEY
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
            tg_user_id=229000 + seed,
            username=f"views-forward-linked-{seed}",
            full_name=f"Views Forward Linked {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100229000 + seed),
            title=f"Views Forward Linked Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100329000 + seed),
            title=f"Views Forward Linked A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100429000 + seed),
            title=f"Views Forward Linked B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()
        ordered = [int(target_b.id), int(target_a.id)]
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "views forward linked"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "forward_to": ordered,
                "autodelete_views": 29,
                "autodelete_report": True,
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


def test_linked_views_forward_missing_narrow_fact_rolls_back_entire_cutover(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-linked-rollback.db'}"
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
                    holder="views-forward-missing-fact",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                )
                assert result.outcome == "claim_unavailable"
            await _assert_rolled_back(Session, publication_id, task_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_linked_views_forward_exact_fact_commits_cutover_threshold_and_snapshot(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-linked-claimed.db'}"
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
                    holder="views-forward-exact-fact",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_forward=True,
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
                cutover = dict(publication.meta or {}).get(CUTOVER_META_KEY)
                assert isinstance(cutover, dict) and cutover.get("retired") is True
                assert task is None
                assert state is not None and int(state.threshold) == 29
                assert lease is not None
                assert dict(attempt.meta or {}).get(FORWARD_TARGET_SNAPSHOT_META_KEY) == expected_snapshot
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_handoff_router_forwards_exact_views_forward_fact(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-routing.db'}"
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
                holder = "views-forward-routing"
                lease_seconds = 180
                allow_repeat = True
                allow_views_autodelete = True
                allow_repeat_views = True
                allow_repeat_views_pin = False
                allow_repeat_views_forward = True

                async def execute(self, publication_id: int):
                    raise AssertionError("linked repeat must not route to direct execute")

            router = CanonicalPublicationRepeatHandoffExecutor(
                executor=Executor(),  # type: ignore[arg-type]
                session_factory=Session,
            )
            result = await router.execute(publication_id)
            assert result.outcome == "ineligible"
            assert captured == {
                "holder": "views-forward-routing",
                "ttl_seconds": 180,
                "allow_repeat": True,
                "allow_views_autodelete": True,
                "allow_repeat_views": True,
                "allow_repeat_views_pin": False,
                "allow_repeat_views_forward": True,
            }
        finally:
            await engine.dispose()

    asyncio.run(run())
