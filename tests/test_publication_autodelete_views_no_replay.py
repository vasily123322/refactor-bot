from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteLease
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseService
from app.services.publication_autodelete_views import (
    PublicationAutodeleteViewsService,
    PublicationAutodeleteViewsSyncConflict,
)
from app.services.publication_autodelete_views_action_ledger import (
    AUTODELETE_VIEWS_ACTIONS_META_KEY,
    PublicationAutodeleteViewsActionLedger,
)
from app.services.publication_autodelete_views_state import PublicationAutodeleteViewStateService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.canonical_repeat_plan_reservation import CanonicalRepeatPlanReservationService
from app.services.canonical_repeat_transport_adapter import CanonicalRepeatTransportAdapter


async def _seed_repeat_views(Session, *, seed: int, now: datetime) -> tuple[int, tuple[int, ...]]:
    async with Session() as session:
        owner = Client(
            tg_user_id=218000 + seed,
            username=f"repeat-views-no-replay-{seed}",
            full_name=f"Repeat Views No Replay {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100218000 + seed),
            title=f"Repeat Views No Replay {seed}",
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
                        "text": f"repeat views no replay {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"silent": True, "autodelete_views": 17},
        )
        assert publication.legacy_post_task_id is not None
        task = await session.get(PostTask, int(publication.legacy_post_task_id))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None

        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=17,
            now=now - timedelta(minutes=1),
        )

        message_ids = (8800 + seed, 8900 + seed, 9000 + seed)
        schedule_meta = dict(schedule.meta or {})
        schedule_meta.pop("legacy_post_task_id", None)
        schedule.meta = schedule_meta
        publication.legacy_post_task_id = None
        publication.status = "published"
        publication.attempt_count = 1
        publication.telegram_message_ids = list(message_ids)
        schedule.status = "completed"
        session.add(
            PublicationAttempt(
                publication_id=int(publication.id),
                attempt=1,
                status="published",
                telegram_message_ids=list(message_ids),
                error=None,
                meta={"canonical_delivery": True},
                finished_at=now - timedelta(seconds=30),
            )
        )
        await session.delete(task)
        await session.commit()
        return int(publication.id), message_ids


async def _acquire(Session, publication_id: int, holder: str):
    async with Session() as session:
        handle = await PublicationAutodeleteLeaseService(session).acquire(
            publication_id=publication_id,
            holder=holder,
        )
        assert handle is not None
        return handle


class _Views:
    def __init__(self, value: int = 99) -> None:
        self.value = value
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target: str | int, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return self.value


class _Provider:
    def __init__(self, outcomes: dict[int, str] | None = None) -> None:
        self.outcomes = dict(outcomes or {})
        self.delete_calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.delete_calls.append((int(chat_id), int(message_id)))
        outcome = self.outcomes.get(int(message_id))
        if outcome == "ambiguous":
            raise RuntimeError("provider connection dropped after request")
        if outcome == "unavailable":
            raise RuntimeError("message to delete not found")
        if outcome == "cancel":
            raise asyncio.CancelledError()

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ) -> None:
        return None


def _states(meta: dict) -> dict[int, str]:
    root = dict(meta[AUTODELETE_VIEWS_ACTIONS_META_KEY])
    actions = dict(root["actions"])
    return {
        int(message_id): str(dict(action)["state"])
        for message_id, action in actions.items()
    }


class _MarkFailureService(PublicationAutodeleteViewsService):
    async def _best_effort_mark_action(self, reservation, state):
        if state == "succeeded":
            return False
        return await super()._best_effort_mark_action(reservation, state)


class _CrashBetweenReservationsService(PublicationAutodeleteViewsService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reserve_calls = 0

    async def _reserve_message(self, candidate, *, message_id: int, observed_views: int):
        self.reserve_calls += 1
        if self.reserve_calls == 2:
            raise RuntimeError("simulated clean crash between message actions")
        return await super()._reserve_message(
            candidate,
            message_id=message_id,
            observed_views=observed_views,
        )


class _DriftAfterFirstReservationService(PublicationAutodeleteViewsService):
    def __init__(self, *args, Session, **kwargs):
        super().__init__(*args, **kwargs)
        self.Session = Session
        self.did_drift = False

    async def _reserve_message(self, candidate, *, message_id: int, observed_views: int):
        result = await super()._reserve_message(
            candidate,
            message_id=message_id,
            observed_views=observed_views,
        )
        outcome, reserve_result = result
        if outcome == "reserved" and not self.did_drift:
            self.did_drift = True
            async with self.Session() as session:
                publication = await session.get(Publication, candidate.publication_id)
                assert publication is not None
                publication.telegram_message_ids = [777777]
                await session.commit()
        return outcome, reserve_result


class _DriftingViews(_Views):
    def __init__(self, Session, publication_id: int) -> None:
        super().__init__(99)
        self.Session = Session
        self.publication_id = publication_id

    async def get_message_views(self, target: str | int, message_id: int) -> int:
        value = await super().get_message_views(target, message_id)
        if len(self.calls) == 3:
            async with self.Session() as session:
                publication = await session.get(Publication, self.publication_id)
                assert publication is not None
                publication.telegram_message_ids = [999999]
                await session.commit()
        return value


def test_generic_provider_ambiguity_is_durable_and_blocks_message_three_and_replay(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-ambiguous.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id, ids = await _seed_repeat_views(Session, seed=1, now=now)
            lease = await _acquire(Session, publication_id, "views-ambiguity")

            first_views = _Views()
            first_provider = _Provider({ids[1]: "ambiguous"})
            async with Session() as session:
                result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=first_views,
                    delete_provider=first_provider,
                    allow_repeat_views=True,
                    lease=lease,
                ).evaluate_and_delete(publication_id, now=now)

            assert result.outcome == "retry"
            assert result.ambiguous_count == 1
            assert [message_id for _, message_id in first_provider.delete_calls] == [
                ids[0],
                ids[1],
            ]
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                states = _states(dict(publication.meta or {}))
                assert states == {ids[0]: "succeeded", ids[1]: "unknown"}
                assert ids[2] not in states

            replay_views = _Views()
            replay_provider = _Provider()
            async with Session() as session:
                replay = await PublicationAutodeleteViewsService(
                    session,
                    view_source=replay_views,
                    delete_provider=replay_provider,
                    allow_repeat_views=True,
                    lease=lease,
                ).evaluate_and_delete(publication_id, now=now)
            assert replay.outcome == "retry"
            assert replay.ambiguous_count == 1
            assert replay_views.calls == []
            assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_success_with_terminal_mark_failure_leaves_reserved_and_replay_calls_zero_delete(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-reserved-crash.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id, ids = await _seed_repeat_views(Session, seed=2, now=now)
            lease = await _acquire(Session, publication_id, "views-mark-crash")
            provider = _Provider()

            async with Session() as session:
                result = await _MarkFailureService(
                    session,
                    view_source=_Views(),
                    delete_provider=provider,
                    allow_repeat_views=True,
                    lease=lease,
                ).evaluate_and_delete(publication_id, now=now)
            assert result.outcome == "retry"
            assert result.ambiguous_count == 1
            assert [message_id for _, message_id in provider.delete_calls] == [ids[0]]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert _states(dict(publication.meta or {})) == {ids[0]: "reserved"}

            replay_provider = _Provider()
            replay_views = _Views()
            async with Session() as session:
                replay = await PublicationAutodeleteViewsService(
                    session,
                    view_source=replay_views,
                    delete_provider=replay_provider,
                    allow_repeat_views=True,
                    lease=lease,
                ).evaluate_and_delete(publication_id, now=now)
            assert replay.outcome == "retry"
            assert replay.ambiguous_count == 1
            assert replay_views.calls == []
            assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_clean_partial_completion_skips_terminal_message_and_resumes_later_messages(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-clean-resume.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id, ids = await _seed_repeat_views(Session, seed=3, now=now)
            lease = await _acquire(Session, publication_id, "views-clean-resume")
            first_provider = _Provider()

            async with Session() as session:
                with pytest.raises(RuntimeError, match="clean crash"):
                    await _CrashBetweenReservationsService(
                        session,
                        view_source=_Views(),
                        delete_provider=first_provider,
                        allow_repeat_views=True,
                        lease=lease,
                    ).evaluate_and_delete(publication_id, now=now)
            assert [message_id for _, message_id in first_provider.delete_calls] == [ids[0]]

            replay_provider = _Provider()
            replay_views = _Views()
            async with Session() as session:
                replay = await PublicationAutodeleteViewsService(
                    session,
                    view_source=replay_views,
                    delete_provider=replay_provider,
                    allow_repeat_views=True,
                    lease=lease,
                ).evaluate_and_delete(publication_id, now=now)
            assert replay.outcome == "deleted"
            assert replay_views.calls == []
            assert [message_id for _, message_id in replay_provider.delete_calls] == [
                ids[1],
                ids[2],
            ]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unavailable_is_terminal_and_exact_lease_and_source_drift_fail_closed(
    tmp_path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-boundaries.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)

            unavailable_id, unavailable_ids = await _seed_repeat_views(
                Session, seed=4, now=now
            )
            unavailable_lease = await _acquire(
                Session, unavailable_id, "views-unavailable"
            )
            unavailable_provider = _Provider({unavailable_ids[0]: "unavailable"})
            async with Session() as session:
                unavailable = await PublicationAutodeleteViewsService(
                    session,
                    view_source=_Views(),
                    delete_provider=unavailable_provider,
                    allow_repeat_views=True,
                    lease=unavailable_lease,
                ).evaluate_and_delete(unavailable_id, now=now)
            assert unavailable.outcome == "deleted"
            assert unavailable.unavailable_count == 1
            assert [message_id for _, message_id in unavailable_provider.delete_calls] == list(
                unavailable_ids
            )

            lease_id, _ = await _seed_repeat_views(Session, seed=5, now=now)
            lease = await _acquire(Session, lease_id, "views-exact-lease")
            forged = replace(lease, holder="wrong-holder")
            forged_provider = _Provider()
            async with Session() as session:
                forged_result = await PublicationAutodeleteViewsService(
                    session,
                    view_source=_Views(),
                    delete_provider=forged_provider,
                    allow_repeat_views=True,
                    lease=forged,
                ).evaluate_and_delete(lease_id, now=now)
            assert forged_result.outcome == "retry"
            assert forged_provider.delete_calls == []

            async with Session() as session:
                row = await session.get(PublicationAutodeleteLease, lease_id)
                assert row is not None
                row.expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
                await session.commit()
            expired_provider = _Provider()
            async with Session() as session:
                expired = await PublicationAutodeleteViewsService(
                    session,
                    view_source=_Views(),
                    delete_provider=expired_provider,
                    allow_repeat_views=True,
                    lease=lease,
                ).evaluate_and_delete(lease_id, now=now)
            assert expired.outcome == "retry"
            assert expired_provider.delete_calls == []

            drift_id, _ = await _seed_repeat_views(Session, seed=6, now=now)
            drift_lease = await _acquire(Session, drift_id, "views-source-drift")
            drift_provider = _Provider()
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                    await PublicationAutodeleteViewsService(
                        session,
                        view_source=_DriftingViews(Session, drift_id),
                        delete_provider=drift_provider,
                        allow_repeat_views=True,
                        lease=drift_lease,
                    ).evaluate_and_delete(drift_id, now=now)
            assert drift_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_reservation_token_cannot_finalize_committed_action(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-stale-token.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id, ids = await _seed_repeat_views(Session, seed=7, now=now)
            lease = await _acquire(Session, publication_id, "views-token")

            async with Session() as session:
                service = PublicationAutodeleteViewsService(
                    session,
                    view_source=_Views(),
                    delete_provider=_Provider(),
                    allow_repeat_views=True,
                    lease=lease,
                )
                candidate, _ = await service._candidate(publication_id, now=now)
                assert candidate is not None
                current = await service._load_current(candidate, require_state=True)
                assert current != "already_deleted"
                reserved = await PublicationAutodeleteViewsActionLedger(session).reserve(
                    lease,
                    telegram_chat_id=candidate.tg_chat_id,
                    telegram_message_id=ids[0],
                    authority_fingerprint=candidate.authority_fingerprint,
                    telegram_message_ids=candidate.telegram_message_ids,
                    threshold=candidate.threshold,
                    observed_views=99,
                )
                assert reserved.outcome == "reserved"
                assert reserved.reservation is not None
                stale = replace(
                    reserved.reservation,
                    reservation_token="stale-reservation-token",
                )
                ledger = PublicationAutodeleteViewsActionLedger(session)
                assert await ledger.mark_succeeded(stale) is False
                assert await ledger.mark_succeeded(reserved.reservation) is True

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert _states(dict(publication.meta or {})) == {ids[0]: "succeeded"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_successor_does_not_inherit_source_views_action_ledger(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-ledger-successor.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            source_id, ids = await _seed_repeat_views(Session, seed=8, now=now)
            lease = await _acquire(Session, source_id, "views-successor-isolation")

            async with Session() as session:
                service = PublicationAutodeleteViewsService(
                    session,
                    view_source=_Views(),
                    delete_provider=_Provider(),
                    allow_repeat_views=True,
                    lease=lease,
                )
                candidate, _ = await service._candidate(source_id, now=now)
                assert candidate is not None
                current = await service._load_current(candidate, require_state=True)
                assert current != "already_deleted"
                reserved = await PublicationAutodeleteViewsActionLedger(session).reserve(
                    lease,
                    telegram_chat_id=candidate.tg_chat_id,
                    telegram_message_id=ids[0],
                    authority_fingerprint=candidate.authority_fingerprint,
                    telegram_message_ids=candidate.telegram_message_ids,
                    threshold=candidate.threshold,
                    observed_views=99,
                )
                assert reserved.outcome == "reserved"
                assert reserved.reservation is not None
                assert await PublicationAutodeleteViewsActionLedger(session).mark_succeeded(
                    reserved.reservation
                )

            async with Session() as session:
                reserved_next = await CanonicalRepeatPlanReservationService(
                    session
                ).reserve_next(source_id, after=now)
                assert reserved_next.outcome == "reserved"

            async with Session() as session:
                materialized = await CanonicalRepeatTransportAdapter(session).materialize(
                    source_id
                )
                assert materialized.outcome == "created"
                assert materialized.publication_id is not None
                successor_id = int(materialized.publication_id)

            async with Session() as session:
                source = await session.get(Publication, source_id)
                successor = await session.get(Publication, successor_id)
                assert source is not None and successor is not None
                assert AUTODELETE_VIEWS_ACTIONS_META_KEY in dict(source.meta or {})
                assert AUTODELETE_VIEWS_ACTIONS_META_KEY not in dict(successor.meta or {})
                assert successor.status == "queued"
                assert successor.telegram_message_ids in (None, [])
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_cancellation_marks_unknown_and_replay_performs_zero_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-cancel-unknown.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id, ids = await _seed_repeat_views(Session, seed=9, now=now)
            lease = await _acquire(Session, publication_id, "views-cancel")
            provider = _Provider({ids[0]: "cancel"})

            async with Session() as session:
                with pytest.raises(asyncio.CancelledError):
                    await PublicationAutodeleteViewsService(
                        session,
                        view_source=_Views(),
                        delete_provider=provider,
                        allow_repeat_views=True,
                        lease=lease,
                    ).evaluate_and_delete(publication_id, now=now)
            assert [message_id for _, message_id in provider.delete_calls] == [ids[0]]

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert _states(dict(publication.meta or {})) == {ids[0]: "unknown"}

            replay_provider = _Provider()
            replay_views = _Views()
            async with Session() as session:
                replay = await PublicationAutodeleteViewsService(
                    session,
                    view_source=replay_views,
                    delete_provider=replay_provider,
                    allow_repeat_views=True,
                    lease=lease,
                ).evaluate_and_delete(publication_id, now=now)
            assert replay.outcome == "retry"
            assert replay.ambiguous_count == 1
            assert replay_views.calls == []
            assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_reservation_source_drift_cannot_redirect_immutable_delete_target(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-post-reserve-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            publication_id, ids = await _seed_repeat_views(Session, seed=10, now=now)
            lease = await _acquire(Session, publication_id, "views-post-reserve-drift")
            provider = _Provider()

            async with Session() as session:
                with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                    await _DriftAfterFirstReservationService(
                        session,
                        Session=Session,
                        view_source=_Views(),
                        delete_provider=provider,
                        allow_repeat_views=True,
                        lease=lease,
                    ).evaluate_and_delete(publication_id, now=now)

            assert [message_id for _, message_id in provider.delete_calls] == [ids[0]]
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                states = _states(dict(publication.meta or {}))
                assert states == {ids[0]: "succeeded"}
        finally:
            await engine.dispose()

    asyncio.run(run())
