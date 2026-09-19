from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.routers.content_plan_publication import (
    _control_callback_data,
    _parse_control_callback,
)
from app.bot.routers.utils.content_plan_hybrid import (
    canonical_published_button_row,
    pending_content_plan_button_row,
)
from app.core.db import Base
from app.domain.models import Channel, Client
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.services.content_plan_history_identity import HistoryPublicationIdentityKind
from app.services.content_plan_pending_rows import (
    list_pending_content_plan_rows,
    paginate_pending_content_plan_rows,
)
from app.services.content_plan_publication_cancellation import (
    ContentPlanPublicationCancellationService,
)
from app.services.content_plan_publication_controls import (
    ContentPlanPublicationControlError,
    ContentPlanPublicationControlService,
    ContentPlanRepeatUnsupported,
    ContentPlanStaleControl,
    content_plan_schedule_state_token,
)
from app.services.canonical_publication_delivery_claim import (
    CanonicalPublicationDeliveryClaimService,
)
from app.services.content_plan_published_rows import list_published_content_plan_rows
from app.services.posting import PostingService
from app.services.publication_execution_mode import (
    CANONICAL_EXECUTION_MODE,
    UnsupportedSchedulingProfileError,
)
from app.services.scheduling import as_utc


class _Bot:
    pass


async def _new_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _channel(Session, suffix: int) -> tuple[Client, Channel]:
    async with Session() as session:
        owner = Client(
            tg_user_id=9_951_000 + suffix,
            username=f"p3{suffix}",
            full_name="P3 Content Plan Fixture",
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-1_009_951_000_000 - suffix,
            title=f"P3 {suffix}",
            owner_id=int(owner.id),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(owner)
        await session.refresh(channel)
        return owner, channel


async def _schedule(
    Session,
    *,
    channel_id: int,
    when: datetime,
    suffix: str,
    mixed: bool = False,
) -> Publication:
    payload = {"type": "text", "text": f"P3 canonical {suffix}"}
    if mixed:
        payload.update(
            {
                "autodelete_seconds": 600,
                "autodelete_views": 100,
                "autodelete_report": True,
            }
        )
    result = await PostingService(_Bot(), Session).schedule(
        int(channel_id),
        payload,
        when,
        dedupe_key=f"p3-{suffix}",
    )
    assert isinstance(result, Publication)
    return result


async def _post_task_count(Session) -> int:
    _ = Session
    assert "post_tasks" not in Base.metadata.tables
    return 0


async def _schedule_token(Session, publication_id: int) -> str:
    async with Session() as session:
        publication = await session.get(Publication, int(publication_id))
        assert publication is not None and publication.schedule_entry_id is not None
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert schedule is not None
        return content_plan_schedule_state_token(
            schedule_entry_id=int(schedule.id),
            scheduled_at=schedule.scheduled_at,
            repeat_rule=schedule.repeat_rule,
        )


async def _claim_counts(Session, publication_id: int) -> tuple[int, int]:
    async with Session() as session:
        attempts = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(PublicationAttempt)
                    .where(PublicationAttempt.publication_id == int(publication_id))
                )
            ).scalar_one()
        )
        leases = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(PublicationDeliveryLease)
                    .where(PublicationDeliveryLease.publication_id == int(publication_id))
                )
            ).scalar_one()
        )
        return attempts, leases


class _GatedSessionFactory:
    def __init__(self, base, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.base = base
        self.entered = entered
        self.release = release

    def __call__(self):
        session = self.base()
        entered = self.entered
        release = self.release

        class _Context:
            async def __aenter__(self):
                entered.set()
                await release.wait()
                return await session.__aenter__()

            async def __aexit__(self, exc_type, exc, tb):
                return await session.__aexit__(exc_type, exc, tb)

        return _Context()


async def _new_file_db(path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"timeout": 5},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_canonical_mixed_pending_is_posttask_free_and_publication_keyed() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 1)
            when = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="mixed-pending",
                mixed=True,
            )
            assert await _post_task_count(Session) == 0

            async with Session() as session:
                rows = await list_pending_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    tg_user_id=int(owner.tg_user_id),
                    start_at=when - timedelta(hours=1),
                    end_at=when + timedelta(hours=1),
                )
            async with Session() as session:
                foreign_rows = await list_pending_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    tg_user_id=int(owner.tg_user_id) + 999,
                    start_at=when - timedelta(hours=1),
                    end_at=when + timedelta(hours=1),
                )
            assert foreign_rows == []

            assert len(rows) == 1
            row = rows[0]
            assert row.authority == "canonical"
            assert row.publication_id == int(publication.id)
            assert row.runtime_options == {
                "autodelete_seconds": 600,
                "autodelete_views": 100,
                "autodelete_report": True,
            }

            rendered = pending_content_plan_button_row(
                row,
                date_iso="2026-09-20",
                tz_code="UTC",
            )
            assert rendered.buttons[0].callback_data == (
                f"cp_open_pub:{publication.id}:2026-09-20"
            )
            assert "👁 100" in rendered.buttons[0].text
            assert "🗑️ 10 мин" in rendered.buttons[0].text
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_count_and_pagination_are_canonical_publication_keyed() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 2)
            start = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)
            first_publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=start + timedelta(hours=9),
                suffix="page-one",
            )
            second_publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=start + timedelta(hours=10),
                suffix="page-two",
            )
            async with Session() as session:
                rows = await list_pending_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    tg_user_id=int(owner.tg_user_id),
                    start_at=start,
                    end_at=start + timedelta(days=1) - timedelta(microseconds=1),
                )
            assert [row.authority for row in rows] == ["canonical", "canonical"]
            assert [row.publication_id for row in rows] == [
                int(first_publication.id),
                int(second_publication.id),
            ]
            first = paginate_pending_content_plan_rows(rows, page=0, page_size=1)
            second = paginate_pending_content_plan_rows(rows, page=1, page_size=1)
            assert first.total_count == 2
            assert first.total_pages == 2
            assert first.rows[0].publication_id == int(first_publication.id)
            assert second.rows[0].publication_id == int(second_publication.id)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_reschedule_moves_day_and_preserves_order_without_posttask() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 3)
            day1 = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
            moved = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=day1 + timedelta(hours=8),
                suffix="move",
                mixed=True,
            )
            later = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=day1 + timedelta(days=1, hours=12),
                suffix="later",
            )

            token = await _schedule_token(Session, int(moved.id))
            result = await ContentPlanPublicationControlService(Session).reschedule(
                publication_id=int(moved.id),
                tg_user_id=int(owner.tg_user_id),
                expected_schedule_token=token,
                delta_seconds=26 * 3600,
            )
            assert result.schedule_entry_id == int(moved.schedule_entry_id)
            assert await _post_task_count(Session) == 0

            async with Session() as session:
                old_rows = await list_pending_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    tg_user_id=int(owner.tg_user_id),
                    start_at=day1,
                    end_at=day1 + timedelta(days=1) - timedelta(microseconds=1),
                )
                new_rows = await list_pending_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    tg_user_id=int(owner.tg_user_id),
                    start_at=day1 + timedelta(days=1),
                    end_at=day1 + timedelta(days=2) - timedelta(microseconds=1),
                )
            assert old_rows == []
            assert [row.publication_id for row in new_rows] == [
                int(moved.id),
                int(later.id),
            ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_repeat_toggle_is_posttask_free_and_mixed_repeat_on_is_atomic_reject() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 4)
            when = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
            plain = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="repeat-plain",
            )
            controls = ContentPlanPublicationControlService(Session)
            token = await _schedule_token(Session, int(plain.id))
            enabled = await controls.set_repeat(
                publication_id=int(plain.id),
                tg_user_id=int(owner.tg_user_id),
                repeat_seconds=3600,
                expected_schedule_token=token,
            )
            assert enabled.repeat_rule == {"enabled": True, "seconds": 3600}
            assert await _post_task_count(Session) == 0

            token = await _schedule_token(Session, int(plain.id))
            disabled = await controls.set_repeat(
                publication_id=int(plain.id),
                tg_user_id=int(owner.tg_user_id),
                repeat_seconds=None,
                expected_schedule_token=token,
            )
            assert disabled.repeat_rule == {}

            mixed = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when + timedelta(hours=1),
                suffix="repeat-mixed",
                mixed=True,
            )
            mixed_token = await _schedule_token(Session, int(mixed.id))
            with pytest.raises(ContentPlanRepeatUnsupported):
                await controls.set_repeat(
                    publication_id=int(mixed.id),
                    tg_user_id=int(owner.tg_user_id),
                    repeat_seconds=3600,
                    expected_schedule_token=mixed_token,
                )

            async with Session() as session:
                persisted = await session.get(Publication, int(mixed.id))
                schedule = await session.get(
                    ScheduleEntry, int(persisted.schedule_entry_id or 0)
                )
                assert persisted is not None and schedule is not None
                assert schedule.repeat_rule == {}
                assert "repeat_group_id" not in dict(schedule.meta or {})
                assert "repeat_group_id" not in dict(persisted.meta or {})
            assert await _post_task_count(Session) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rejected_fresh_profile_never_appears_as_pending_fallback() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 5)
            when = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
            with pytest.raises(UnsupportedSchedulingProfileError):
                await PostingService(_Bot(), Session).schedule(
                    int(channel.id),
                    {
                        "type": "text",
                        "text": "rejected report-only",
                        "autodelete_report": True,
                    },
                    when,
                    dedupe_key="p3-rejected",
                )
            async with Session() as session:
                rows = await list_pending_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    tg_user_id=int(owner.tg_user_id),
                    start_at=when - timedelta(hours=1),
                    end_at=when + timedelta(hours=1),
                )
            assert rows == []
            assert await _post_task_count(Session) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fresh_mixed_cancel_is_publication_native_and_published_row_keeps_both_triggers() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            _owner, channel = await _channel(Session, 6)
            when = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
            cancellable = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="mixed-cancel",
                mixed=True,
            )
            cancel_token = await _schedule_token(Session, int(cancellable.id))
            cancelled = await ContentPlanPublicationCancellationService(Session).delete(
                int(cancellable.id),
                expected_schedule_token=cancel_token,
            )
            assert cancelled.outcome == "cancelled"
            assert await _post_task_count(Session) == 0

            published = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when + timedelta(hours=1),
                suffix="mixed-published",
                mixed=True,
            )
            async with Session() as session:
                publication = await session.get(Publication, int(published.id))
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert schedule is not None
                publication.status = "published"
                schedule.status = "completed"
                await session.commit()

            async with Session() as session:
                rows = await list_published_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    start_at=when,
                    end_at=when + timedelta(days=1),
                )
            row = next(item for item in rows if item.publication_id == int(published.id))
            assert row.autodelete_seconds == 600
            assert row.autodelete_views == 100
            rendered = canonical_published_button_row(
                row,
                date_iso="2026-09-26",
                tz_code="UTC",
            )
            assert rendered is not None
            assert "👁 100" in rendered.buttons[0].text
            assert "🗑️ 10 мин" in rendered.buttons[0].text
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_old_callback_alias_redirect_is_owner_filtered_before_canonical_handoff(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot.routers import content_plan_cancellation as bridge

        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 7)
            when = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="callback-owner",
            )
            callback_id = 44
            async with Session() as session:
                persisted = await session.get(Publication, int(publication.id))
                assert persisted is not None
                persisted.meta = {
                    **dict(persisted.meta or {}),
                    "legacy_post_task_callback_id": callback_id,
                }
                await session.commit()

            monkeypatch.setattr(bridge, "AsyncSessionLocal", Session)
            owned = await bridge._owned_identity_for_post_id(
                callback_id,
                tg_user_id=int(owner.tg_user_id),
            )
            foreign = await bridge._owned_identity_for_post_id(
                callback_id,
                tg_user_id=int(owner.tg_user_id) + 999,
            )
            assert owned.kind is HistoryPublicationIdentityKind.CANONICAL_LINKED
            assert owned.publication_id == int(publication.id)
            assert foreign.kind is HistoryPublicationIdentityKind.FAIL_CLOSED
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_canonical_controls_fail_closed_after_delivery_claim_evidence() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 8)
            when = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="claimed-control",
            )
            async with Session() as session:
                persisted = await session.get(Publication, int(publication.id))
                assert persisted is not None
                persisted.attempt_count = 1
                await session.commit()

            controls = ContentPlanPublicationControlService(Session)
            token = await _schedule_token(Session, int(publication.id))
            with pytest.raises(ContentPlanPublicationControlError):
                await controls.reschedule(
                    publication_id=int(publication.id),
                    tg_user_id=int(owner.tg_user_id),
                    expected_schedule_token=token,
                    delta_seconds=86400,
                )
            with pytest.raises(ContentPlanPublicationControlError):
                await controls.set_repeat(
                    publication_id=int(publication.id),
                    tg_user_id=int(owner.tg_user_id),
                    repeat_seconds=3600,
                    expected_schedule_token=token,
                )

            async with Session() as session:
                persisted = await session.get(Publication, int(publication.id))
                schedule = await session.get(
                    ScheduleEntry, int(persisted.schedule_entry_id or 0)
                )
                assert schedule is not None
                assert as_utc(schedule.scheduled_at) == when
                assert schedule.repeat_rule == {}
            assert await _post_task_count(Session) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_cross_day_reschedule_moves_canonical_row_off_old_day() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 9)
            day1 = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=day1 + timedelta(hours=8),
                suffix="linked-cross-day",
            )
            token = await _schedule_token(Session, int(publication.id))
            await ContentPlanPublicationControlService(Session).reschedule(
                publication_id=int(publication.id),
                tg_user_id=int(owner.tg_user_id),
                expected_schedule_token=token,
                delta_seconds=86400,
            )

            async with Session() as session:
                old_rows = await list_pending_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    tg_user_id=int(owner.tg_user_id),
                    start_at=day1,
                    end_at=day1 + timedelta(days=1) - timedelta(microseconds=1),
                )
                new_rows = await list_pending_content_plan_rows(
                    session,
                    channel_id=int(channel.id),
                    tg_user_id=int(owner.tg_user_id),
                    start_at=day1 + timedelta(days=1),
                    end_at=day1 + timedelta(days=2) - timedelta(microseconds=1),
                )
            assert old_rows == []
            assert len(new_rows) == 1
            assert new_rows[0].authority == "canonical"
            assert new_rows[0].publication_id == int(publication.id)
        finally:
            await engine.dispose()

    asyncio.run(run())



def test_stale_reschedule_callback_fails_after_first_success() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 10)
            when = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="stale-reschedule",
            )
            controls = ContentPlanPublicationControlService(Session)
            token = await _schedule_token(Session, int(publication.id))

            first = await controls.reschedule(
                publication_id=int(publication.id),
                tg_user_id=int(owner.tg_user_id),
                expected_schedule_token=token,
                delta_seconds=3600,
            )
            assert as_utc(first.scheduled_at) == when + timedelta(hours=1)

            with pytest.raises(ContentPlanStaleControl):
                await controls.reschedule(
                    publication_id=int(publication.id),
                    tg_user_id=int(owner.tg_user_id),
                    expected_schedule_token=token,
                    delta_seconds=3600,
                )

            async with Session() as session:
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert schedule is not None
                assert as_utc(schedule.scheduled_at) == when + timedelta(hours=1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_two_controls_from_same_t0_have_exactly_one_coherent_winner() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 11)
            when = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="same-t0-controls",
            )
            controls = ContentPlanPublicationControlService(Session)
            token = await _schedule_token(Session, int(publication.id))
            second_entered = asyncio.Event()
            allow_second = asyncio.Event()
            second_task = asyncio.create_task(
                ContentPlanPublicationControlService(
                    _GatedSessionFactory(Session, second_entered, allow_second)
                ).reschedule(
                    publication_id=int(publication.id),
                    tg_user_id=int(owner.tg_user_id),
                    expected_schedule_token=token,
                    delta_seconds=3600,
                )
            )
            await second_entered.wait()

            repeat_result = await controls.set_repeat(
                publication_id=int(publication.id),
                tg_user_id=int(owner.tg_user_id),
                repeat_seconds=3600,
                expected_schedule_token=token,
            )
            assert repeat_result.repeat_rule == {"enabled": True, "seconds": 3600}

            allow_second.set()
            with pytest.raises(ContentPlanStaleControl):
                await second_task

            async with Session() as session:
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert schedule is not None
                assert as_utc(schedule.scheduled_at) == when
                assert schedule.repeat_rule == {"enabled": True, "seconds": 3600}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_delete_after_reschedule_fails_closed() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 12)
            when = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="stale-delete",
            )
            token = await _schedule_token(Session, int(publication.id))
            await ContentPlanPublicationControlService(Session).reschedule(
                publication_id=int(publication.id),
                tg_user_id=int(owner.tg_user_id),
                expected_schedule_token=token,
                delta_seconds=3600,
            )

            result = await ContentPlanPublicationCancellationService(Session).delete(
                int(publication.id),
                expected_schedule_token=token,
            )
            assert result.outcome == "cannot_cancel"
            assert result.reason == "stale_schedule_state"

            async with Session() as session:
                persisted = await session.get(Publication, int(publication.id))
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert persisted is not None and schedule is not None
                assert persisted.status == "queued"
                assert schedule.status == "pending"
                assert as_utc(schedule.scheduled_at) == when + timedelta(hours=1)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_repeat_after_reschedule_fails_closed() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 13)
            when = datetime(2026, 10, 2, 11, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="stale-repeat",
            )
            controls = ContentPlanPublicationControlService(Session)
            token = await _schedule_token(Session, int(publication.id))
            await controls.reschedule(
                publication_id=int(publication.id),
                tg_user_id=int(owner.tg_user_id),
                expected_schedule_token=token,
                delta_seconds=3600,
            )

            with pytest.raises(ContentPlanStaleControl):
                await controls.set_repeat(
                    publication_id=int(publication.id),
                    tg_user_id=int(owner.tg_user_id),
                    repeat_seconds=3600,
                    expected_schedule_token=token,
                )

            async with Session() as session:
                schedule = await session.get(
                    ScheduleEntry, int(publication.schedule_entry_id or 0)
                )
                assert schedule is not None
                assert schedule.repeat_rule == {}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fresh_controls_after_reschedule_use_new_state_and_fit_callback_limit() -> None:
    async def run() -> None:
        engine, Session = await _new_db()
        try:
            owner, channel = await _channel(Session, 14)
            when = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
            publication = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="fresh-after-reschedule",
            )
            controls = ContentPlanPublicationControlService(Session)
            old_token = await _schedule_token(Session, int(publication.id))
            await controls.reschedule(
                publication_id=int(publication.id),
                tg_user_id=int(owner.tg_user_id),
                expected_schedule_token=old_token,
                delta_seconds=3600,
            )
            fresh_token = await _schedule_token(Session, int(publication.id))
            assert fresh_token != old_token

            callback_data = _control_callback_data(
                "cp_repeat_set_pub",
                publication_id=int(publication.id),
                date_iso="2026-10-03",
                schedule_token=fresh_token,
                value=3600,
            )
            assert len(callback_data.encode("utf-8")) <= 64
            parsed = _parse_control_callback(callback_data, "cp_repeat_set_pub")
            assert parsed == (
                int(publication.id),
                "2026-10-03",
                fresh_token,
                3600,
            )

            updated = await controls.set_repeat(
                publication_id=int(publication.id),
                tg_user_id=int(owner.tg_user_id),
                repeat_seconds=3600,
                expected_schedule_token=fresh_token,
            )
            assert updated.repeat_rule == {"enabled": True, "seconds": 3600}

            max_callback = _control_callback_data(
                "cp_resched_set_pub",
                publication_id=(2**63) - 1,
                date_iso="2026-12-31",
                schedule_token="z" * 13,
                value=604800,
            )
            assert len(max_callback.encode("utf-8")) == 64
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_delivery_claim_cancel_overlap_has_one_winner_each_direction(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_file_db(tmp_path / "p3-claim-cancel.db")
        try:
            owner, channel = await _channel(Session, 15)
            when = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)

            cancel_winner = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="cancel-wins",
            )
            cancel_token = await _schedule_token(Session, int(cancel_winner.id))

            async with Session() as claim_session:
                claimer = CanonicalPublicationDeliveryClaimService(claim_session)
                original_lock = claimer._lock_delivery_rows
                claim_entered = asyncio.Event()
                allow_claim = asyncio.Event()

                async def gated_claim_lock(publication_id: int):
                    claim_entered.set()
                    await allow_claim.wait()
                    return await original_lock(publication_id)

                claimer._lock_delivery_rows = gated_claim_lock
                claim_task = asyncio.create_task(
                    claimer.claim(
                        publication_id=int(cancel_winner.id),
                        holder="p3-cancel-winner",
                        now=when + timedelta(minutes=1),
                    )
                )
                await claim_entered.wait()
                cancel_result = await ContentPlanPublicationCancellationService(
                    Session
                ).delete(
                    int(cancel_winner.id),
                    expected_schedule_token=cancel_token,
                )
                allow_claim.set()
                claim_result = await claim_task

            assert cancel_result.outcome == "cancelled"
            assert claim_result is None
            assert await _claim_counts(Session, int(cancel_winner.id)) == (0, 0)
            async with Session() as session:
                persisted = await session.get(Publication, int(cancel_winner.id))
                schedule = await session.get(
                    ScheduleEntry, int(cancel_winner.schedule_entry_id or 0)
                )
                assert persisted is not None and schedule is not None
                assert persisted.status == "cancelled"
                assert schedule.status == "cancelled"
                assert persisted.telegram_message_ids in (None, [])
                assert persisted.result_link is None
                assert persisted.last_error is None

            claim_winner = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when + timedelta(hours=1),
                suffix="claim-wins-cancel",
            )
            claim_token = await _schedule_token(Session, int(claim_winner.id))
            cancel_entered = asyncio.Event()
            allow_cancel = asyncio.Event()
            gated_factory = _GatedSessionFactory(
                Session,
                cancel_entered,
                allow_cancel,
            )
            cancel_task = asyncio.create_task(
                ContentPlanPublicationCancellationService(gated_factory).delete(
                    int(claim_winner.id),
                    expected_schedule_token=claim_token,
                )
            )
            await cancel_entered.wait()
            async with Session() as claim_session:
                claim_result = await CanonicalPublicationDeliveryClaimService(
                    claim_session
                ).claim(
                    publication_id=int(claim_winner.id),
                    holder="p3-claim-winner-cancel",
                    now=when + timedelta(hours=1, minutes=1),
                )
            allow_cancel.set()
            cancel_result = await cancel_task

            assert claim_result is not None
            assert cancel_result.outcome == "cannot_cancel"
            assert await _claim_counts(Session, int(claim_winner.id)) == (1, 1)
            async with Session() as session:
                persisted = await session.get(Publication, int(claim_winner.id))
                schedule = await session.get(
                    ScheduleEntry, int(claim_winner.schedule_entry_id or 0)
                )
                assert persisted is not None and schedule is not None
                assert persisted.status == "sending"
                assert persisted.attempt_count == 1
                assert schedule.status == "pending"
                assert as_utc(schedule.scheduled_at) == when + timedelta(hours=1)
                assert persisted.telegram_message_ids in (None, [])
                assert persisted.result_link is None
                assert persisted.last_error is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_delivery_claim_reschedule_overlap_has_one_winner_each_direction(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_file_db(tmp_path / "p3-claim-reschedule.db")
        try:
            owner, channel = await _channel(Session, 16)
            when = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

            control_winner = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when,
                suffix="reschedule-wins",
            )
            control_token = await _schedule_token(Session, int(control_winner.id))

            async with Session() as claim_session:
                claimer = CanonicalPublicationDeliveryClaimService(claim_session)
                original_lock = claimer._lock_delivery_rows
                claim_entered = asyncio.Event()
                allow_claim = asyncio.Event()

                async def gated_claim_lock(publication_id: int):
                    claim_entered.set()
                    await allow_claim.wait()
                    return await original_lock(publication_id)

                claimer._lock_delivery_rows = gated_claim_lock
                claim_task = asyncio.create_task(
                    claimer.claim(
                        publication_id=int(control_winner.id),
                        holder="p3-reschedule-winner",
                        now=when + timedelta(minutes=1),
                    )
                )
                await claim_entered.wait()
                moved = await ContentPlanPublicationControlService(Session).reschedule(
                    publication_id=int(control_winner.id),
                    tg_user_id=int(owner.tg_user_id),
                    expected_schedule_token=control_token,
                    delta_seconds=3600,
                )
                allow_claim.set()
                claim_result = await claim_task

            assert as_utc(moved.scheduled_at) == when + timedelta(hours=1)
            assert claim_result is None
            assert await _claim_counts(Session, int(control_winner.id)) == (0, 0)
            async with Session() as session:
                persisted = await session.get(Publication, int(control_winner.id))
                schedule = await session.get(
                    ScheduleEntry, int(control_winner.schedule_entry_id or 0)
                )
                assert persisted is not None and schedule is not None
                assert persisted.status == "queued"
                assert persisted.attempt_count == 0
                assert schedule.status == "pending"
                assert as_utc(schedule.scheduled_at) == when + timedelta(hours=1)
                assert persisted.telegram_message_ids in (None, [])
                assert persisted.result_link is None
                assert persisted.last_error is None

            claim_winner = await _schedule(
                Session,
                channel_id=int(channel.id),
                when=when + timedelta(hours=2),
                suffix="claim-wins-reschedule",
            )
            claim_token = await _schedule_token(Session, int(claim_winner.id))
            control_entered = asyncio.Event()
            allow_control = asyncio.Event()
            gated_factory = _GatedSessionFactory(
                Session,
                control_entered,
                allow_control,
            )
            control_task = asyncio.create_task(
                ContentPlanPublicationControlService(gated_factory).reschedule(
                    publication_id=int(claim_winner.id),
                    tg_user_id=int(owner.tg_user_id),
                    expected_schedule_token=claim_token,
                    delta_seconds=3600,
                )
            )
            await control_entered.wait()
            async with Session() as claim_session:
                claim_result = await CanonicalPublicationDeliveryClaimService(
                    claim_session
                ).claim(
                    publication_id=int(claim_winner.id),
                    holder="p3-claim-winner-reschedule",
                    now=when + timedelta(hours=2, minutes=1),
                )
            allow_control.set()
            with pytest.raises(ContentPlanPublicationControlError):
                await control_task

            assert claim_result is not None
            assert await _claim_counts(Session, int(claim_winner.id)) == (1, 1)
            async with Session() as session:
                persisted = await session.get(Publication, int(claim_winner.id))
                schedule = await session.get(
                    ScheduleEntry, int(claim_winner.schedule_entry_id or 0)
                )
                assert persisted is not None and schedule is not None
                assert persisted.status == "sending"
                assert persisted.attempt_count == 1
                assert schedule.status == "pending"
                assert as_utc(schedule.scheduled_at) == when + timedelta(hours=2)
                assert persisted.telegram_message_ids in (None, [])
                assert persisted.result_link is None
                assert persisted.last_error is None
        finally:
            await engine.dispose()

    asyncio.run(run())
