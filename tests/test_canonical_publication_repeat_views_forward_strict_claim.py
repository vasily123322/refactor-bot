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
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
)
from app.services.canonical_publication_repeat_capability_claim import (
    CanonicalPublicationRepeatCapabilityClaimService,
)
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.canonical_publication_safe_repeat_runtime import (
    build_canonical_publication_safe_repeat_runtime,
)
from app.services.publication_bridge import LegacyPublicationBridge


async def _seed_retired(
    Session,
    *,
    seed: int,
    pin: bool = False,
    empty_forward: bool = False,
) -> tuple[int, list[dict[str, int]]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=228000 + seed,
            username=f"views-forward-claim-{seed}",
            full_name=f"Views Forward Claim {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100228000 + seed),
            title=f"Views Forward Claim Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100328000 + seed),
            title=f"Views Forward Claim A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100428000 + seed),
            title=f"Views Forward Claim B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()

        ordered_ids = [] if empty_forward else [int(target_b.id), int(target_a.id)]
        options: dict[str, object] = {
            "silent": True,
            "forward_to": ordered_ids,
            "autodelete_views": 27,
            "autodelete_report": True,
        }
        if pin:
            options["pin_on"] = True

        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "views forward claim"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options=options,
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()

        expected_snapshot = (
            []
            if empty_forward
            else [
                {
                    "channel_id": int(target_b.id),
                    "telegram_chat_id": int(target_b.tg_chat_id),
                },
                {
                    "channel_id": int(target_a.id),
                    "telegram_chat_id": int(target_a.tg_chat_id),
                },
            ]
        )
        return int(publication.id), expected_snapshot


async def _assert_pristine(Session, publication_id: int) -> None:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        state = await session.get(PublicationAutodeleteViewState, publication_id)
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
        assert state is None
        assert attempts == []


def test_repeat_views_forward_requires_dedicated_strict_fact(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-claim-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, _ = await _seed_retired(Session, seed=1)

            async with Session() as session:
                claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="plain-views-and-pin-facts-only",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                )
                assert claim is None
            await _assert_pristine(Session, publication_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_forward_exact_fact_stages_threshold_and_ordered_target_snapshot(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-claim-open.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, expected_snapshot = await _seed_retired(Session, seed=2)

            async with Session() as session:
                claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=publication_id,
                    holder="views-forward-explicit",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_forward=True,
                )
                assert claim is not None

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
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
                assert state is not None and int(state.threshold) == 27
                assert dict(attempt.meta or {}).get(FORWARD_TARGET_SNAPSHOT_META_KEY) == expected_snapshot
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_empty_forward_and_pin_forward_combination_remain_strictly_closed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-forward-shapes-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            empty_id, _ = await _seed_retired(Session, seed=3, empty_forward=True)
            async with Session() as session:
                empty_claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=empty_id,
                    holder="views-forward-empty",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_forward=True,
                )
                assert empty_claim is None
            await _assert_pristine(Session, empty_id)

            combined_id, _ = await _seed_retired(Session, seed=4, pin=True)
            async with Session() as session:
                combined_claim = await CanonicalPublicationRepeatCapabilityClaimService(
                    session
                ).claim_supported(
                    publication_id=combined_id,
                    holder="views-pin-forward-independent-facts",
                    ttl_seconds=180,
                    allow_repeat=True,
                    allow_views_autodelete=True,
                    allow_repeat_views=True,
                    allow_repeat_views_pin=True,
                    allow_repeat_views_forward=True,
                )
                assert combined_claim is None
            await _assert_pristine(Session, combined_id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_safe_repeat_runtime_keeps_views_forward_fact_independent_and_default_off() -> None:
    class _Bot:
        def __getattr__(self, name):
            raise AssertionError(f"construction must not call provider method {name}")

    default = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_views_autodelete=True,
        allow_repeat=True,
        allow_repeat_views=True,
        allow_repeat_views_pin=True,
        repeat_owner_policy_enforced=True,
    )
    assert default.executor.allow_repeat_views is True
    assert default.executor.allow_repeat_views_pin is True
    assert default.executor.allow_repeat_views_forward is False

    explicit = build_canonical_publication_safe_repeat_runtime(
        bot=_Bot(),
        session_factory=object(),  # type: ignore[arg-type]
        allow_views_autodelete=True,
        allow_repeat=True,
        allow_repeat_views=True,
        allow_repeat_views_forward=True,
        repeat_owner_policy_enforced=True,
    )
    assert explicit.executor.allow_repeat_views is True
    assert explicit.executor.allow_repeat_views_pin is False
    assert explicit.executor.allow_repeat_views_forward is True

    direct = CanonicalPublicationSafeRepeatDeliveryExecutor(
        object(),  # type: ignore[arg-type]
        sender=object(),
        allow_repeat_views=True,
    )
    assert direct.allow_repeat_views_forward is False
