from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_autodelete_runtime_planner import (
    CanonicalPublicationAutodeleteRuntimePlanner,
)
from app.services.canonical_publication_delivery_capability_claim import (
    CanonicalPublicationDeliveryCapabilityClaimService,
)
from app.services.canonical_publication_delivery_executor import (
    CanonicalPublicationDeliveryExecutor,
)
from app.services.canonical_publication_delivery_finalizer import (
    CanonicalPublicationDeliveryFinalizer,
)
from app.services.canonical_publication_delivery_live_autodelete import (
    CanonicalPublicationDeliveryLiveAutodeleteResult,
    CanonicalPublicationDeliveryLiveAutodeleteWriter,
)
from app.services.canonical_publication_delivery_live_auxiliary_executor import (
    CanonicalPublicationDeliveryLiveAuxiliaryExecution,
)
from app.services.canonical_publication_delivery_live_auxiliary_hook import (
    CanonicalPublicationDeliveryLiveAuxiliaryHook,
)
from app.services.canonical_publication_delivery_post_send import (
    CanonicalPublicationDeliveryPostSendContext,
)
from app.services.publication_autodelete_candidates import (
    PublicationAutodeleteCandidateSelector,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_retired(
    Session,
    *,
    seed: int,
    runtime_options: dict,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=193000 + seed,
            username=f"timer-delivery-{seed}",
            full_name=f"Timer Delivery {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100193000 + seed),
            title=f"Timer Delivery {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"Timer delivery proof {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            runtime_options=runtime_options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        assert task is not None
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(publication.id)


async def _assert_unclaimed(Session, publication_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        assert publication.status == "queued"
        assert int(publication.attempt_count or 0) == 0
        attempts = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id
                )
            )
        ).scalars().all()
        assert attempts == []
        assert await session.get(PublicationDeliveryLease, publication_id) is None


def test_timer_claim_is_default_off_and_explicitly_enabled_only_with_executor() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            disabled_id = await _seed_retired(
                Session,
                seed=1,
                runtime_options={"autodelete_seconds": 60},
            )
            async with Session() as session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=disabled_id,
                    holder="timer-disabled",
                    ttl_seconds=180,
                )
                assert claim is None
            await _assert_unclaimed(Session, disabled_id)

            enabled_id = await _seed_retired(
                Session,
                seed=2,
                runtime_options={
                    "autodelete_seconds": 60,
                    "autodelete_report": True,
                },
            )
            async with Session() as session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=enabled_id,
                    holder="timer-enabled",
                    ttl_seconds=180,
                    allow_time_autodelete=True,
                )
                assert claim is not None
                assert claim.plan.runtime_options()["autodelete_seconds"] == 60
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == enabled_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "sending"
                assert dict(attempt.meta or {}).get("canonical_delivery") is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_live_timer_materialization_matches_terminal_runtime_and_is_not_prematurely_due() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired(
                Session,
                seed=3,
                runtime_options={
                    "autodelete_seconds": 90,
                    "autodelete_report": True,
                },
            )
            claim_at = datetime.now(timezone.utc)
            async with Session() as session:
                claim = await CanonicalPublicationDeliveryCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="timer-live-writer",
                    ttl_seconds=180,
                    now=claim_at,
                    allow_time_autodelete=True,
                )
                assert claim is not None

            primary_finished_at = claim_at + timedelta(seconds=2)
            context = CanonicalPublicationDeliveryPostSendContext(
                publication_id=publication_id,
                lease=claim.lease,
                plan=claim.plan,
                message_ids=(3301,),
                result_link=None,
                primary_finished_at=primary_finished_at,
            )
            async with Session() as session:
                writer = CanonicalPublicationDeliveryLiveAutodeleteWriter(session)
                created = await writer.materialize(
                    context,
                    at=claim_at + timedelta(seconds=3),
                )
                assert created.outcome == "created"

            expected_due = primary_finished_at + timedelta(seconds=90)
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert dict(publication.meta or {})[AUTODELETE_RUNTIME_META_KEY] == {
                    "deleted": False,
                    "effective_seconds": 90,
                    "scheduled_at": expected_due.isoformat(),
                }
                repeated = await CanonicalPublicationDeliveryLiveAutodeleteWriter(
                    session
                ).materialize(
                    context,
                    at=claim_at + timedelta(seconds=4),
                )
                assert repeated.outcome == "existing"

                # Generated runtime may exist while primary ownership is still live, but
                # the mature destructive selector requires terminal `published` and must
                # not expose this row to the delete worker yet.
                before_terminal = await PublicationAutodeleteCandidateSelector(
                    session
                ).select_batch(limit=200)
                assert publication_id not in before_terminal.publication_ids

            async with Session() as session:
                finalized = await CanonicalPublicationDeliveryFinalizer(
                    session
                ).complete_success(
                    claim.lease,
                    plan=claim.plan,
                    message_ids=[3301],
                    result_link=None,
                    finished_at=primary_finished_at,
                    now=claim_at + timedelta(seconds=5),
                )
                assert finalized.outcome == "published"

            async with Session() as session:
                terminal_plan = await CanonicalPublicationAutodeleteRuntimePlanner(
                    session
                ).plan(publication_id)
                assert terminal_plan is not None
                assert terminal_plan.existing is True
                assert terminal_plan.scheduled_at == expected_due
                after_terminal = await PublicationAutodeleteCandidateSelector(
                    session
                ).select_batch(limit=200)
                assert publication_id in after_terminal.publication_ids
        finally:
            await engine.dispose()

    asyncio.run(run())


class _Sender:
    def __init__(self) -> None:
        self.calls = 0

    async def send_document(self, *args, **kwargs) -> list[int]:
        self.calls += 1
        return [3401]


class _NoopAuxiliaryExecutor:
    async def execute(self, plan) -> CanonicalPublicationDeliveryLiveAuxiliaryExecution:
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id)
        )


class _ConflictTimerWriter:
    async def materialize(self, context) -> CanonicalPublicationDeliveryLiveAutodeleteResult:
        return CanonicalPublicationDeliveryLiveAutodeleteResult(
            publication_id=int(context.publication_id),
            outcome="conflict",
        )


def test_required_timer_conflict_after_primary_send_leaves_ambiguous_and_never_resends() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id = await _seed_retired(
                Session,
                seed=4,
                runtime_options={"autodelete_seconds": 120},
            )
            sender = _Sender()
            hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
                executor=_NoopAuxiliaryExecutor(),
                autodelete_writer=_ConflictTimerWriter(),
                session_factory=Session,
            )
            executor = CanonicalPublicationDeliveryExecutor(
                Session,
                sender=sender,
                post_send_hook=hook,
                allow_time_autodelete=True,
                heartbeat_interval_seconds=120,
            )

            first = await executor.execute(publication_id)
            assert first.outcome == "lease_lost"
            assert first.post_send_hook_failed is True
            assert first.message_ids == (3401,)
            assert sender.calls == 1

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.status == "sending"
                assert publication.telegram_message_ids is None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(publication.meta or {})
                attempt = (
                    await session.execute(
                        select(PublicationAttempt).where(
                            PublicationAttempt.publication_id == publication_id,
                            PublicationAttempt.attempt == 1,
                        )
                    )
                ).scalar_one()
                assert attempt.status == "sending"
                assert attempt.finished_at is None
                assert await session.get(PublicationDeliveryLease, publication_id) is not None

            second = await executor.execute(publication_id)
            assert second.outcome == "ineligible"
            assert sender.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
