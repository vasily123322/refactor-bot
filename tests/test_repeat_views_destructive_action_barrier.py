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
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publishing.models import Publication, PublicationAttempt, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.canonical_repeat_plan_reservation import CanonicalRepeatPlanReservationService
from app.services.canonical_repeat_transport_adapter import CanonicalRepeatTransportAdapter
from app.services.publication_autodelete_lease import (
    PublicationAutodeleteLeaseHandle,
    PublicationAutodeleteLeaseService,
)
from app.services.publication_autodelete_views_action_ledger import (
    VIEWS_ACTION_LEDGER_META_KEY,
    PublicationAutodeleteViewsActionLedger,
)
from app.services.publication_autodelete_views import (
    PublicationAutodeleteViewsSyncConflict,
)
from app.services.publication_autodelete_views_destructive import (
    PublicationAutodeleteViewsDestructiveService,
)
from app.services.publication_autodelete_views_state import PublicationAutodeleteViewStateService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


async def _seed_terminal_repeat_views(
    Session,
    *,
    seed: int,
    now: datetime,
    message_count: int = 2,
    threshold: int = 17,
) -> int:
    async with Session() as session:
        owner = Client(
            tg_user_id=216000 + seed,
            username=f"repeat-views-barrier-{seed}",
            full_name=f"Repeat Views Barrier {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100216000 + seed),
            title=f"Repeat Views Barrier {seed}",
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
                        "text": f"repeat views barrier {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=now - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={"silent": True, "autodelete_views": threshold},
        )
        assert publication.legacy_post_task_id is not None
        task = await session.get(PostTask, int(publication.legacy_post_task_id))
        schedule = await session.get(ScheduleEntry, int(publication.schedule_entry_id))
        assert task is not None and schedule is not None

        await PublicationAutodeleteViewStateService(session).sync_intent(
            publication_id=int(publication.id),
            threshold=threshold,
            now=now - timedelta(minutes=1),
        )
        message_ids = [8600 + seed * 10 + index for index in range(message_count)]
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
        return int(publication.id)


async def _acquire(Session, publication_id: int, *, holder: str):
    async with Session() as session:
        handle = await PublicationAutodeleteLeaseService(session).acquire(
            publication_id=publication_id,
            holder=holder,
            ttl_seconds=180,
            allow_linked=True,
        )
        assert handle is not None
        return handle


async def _release(Session, handle: PublicationAutodeleteLeaseHandle) -> None:
    async with Session() as session:
        await PublicationAutodeleteLeaseService(session).release(handle)


async def _action_states(Session, publication_id: int) -> list[str]:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        ledger = dict(publication.meta or {}).get(VIEWS_ACTION_LEDGER_META_KEY)
        assert isinstance(ledger, dict)
        actions = ledger.get("actions")
        assert isinstance(actions, list)
        return [str(action["state"]) for action in actions]


async def _publication(Session, publication_id: int) -> Publication:
    async with Session() as session:
        publication = await session.get(Publication, publication_id)
        assert publication is not None
        return publication


class _Views:
    def __init__(self, value: int = 999) -> None:
        self.value = value
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target: str | int, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return self.value


class _DeleteProvider:
    def __init__(self, outcomes=()) -> None:
        self.outcomes = list(outcomes)
        self.delete_calls: list[tuple[int, int]] = []
        self.report_calls: list[int] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.delete_calls.append((int(chat_id), int(message_id)))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ) -> None:
        self.report_calls.append(int(chat_id))


class _LoseFinalizeService(PublicationAutodeleteViewsDestructiveService):
    async def _finish_action(self, reservation, state):
        return False


class _DriftAfterFirstDeleteProvider(_DeleteProvider):
    def __init__(self, Session, publication_id: int) -> None:
        super().__init__()
        self.Session = Session
        self.publication_id = publication_id

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        await super().delete_message(chat_id=chat_id, message_id=message_id)
        if len(self.delete_calls) == 1:
            async with self.Session() as session:
                publication = await session.get(Publication, self.publication_id)
                assert publication is not None
                schedule = await session.get(
                    ScheduleEntry,
                    int(publication.schedule_entry_id),
                )
                assert schedule is not None
                schedule.timezone = "Europe/London"
                await session.commit()


async def _run_service(
    Session,
    publication_id: int,
    *,
    handle: PublicationAutodeleteLeaseHandle,
    provider,
    views=None,
    service_cls=PublicationAutodeleteViewsDestructiveService,
):
    source = views or _Views()
    async with Session() as session:
        result = await service_cls(
            session,
            view_source=source,
            delete_provider=provider,
            allow_repeat_views=True,
            lease_handle=handle,
        ).evaluate_and_delete(publication_id)
    return result, source


def test_repeat_views_generic_delete_ambiguity_is_permanent_no_replay(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ambiguous.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal_repeat_views(Session, seed=1, now=now)
            handle = await _acquire(Session, publication_id, holder="first")
            provider = _DeleteProvider([RuntimeError("transport outcome unknown")])
            result, _ = await _run_service(
                Session, publication_id, handle=handle, provider=provider
            )
            assert result.outcome == "ambiguous"
            assert len(provider.delete_calls) == 1
            assert await _action_states(Session, publication_id) == ["unknown"]

            await _release(Session, handle)
            replay_handle = await _acquire(Session, publication_id, holder="replay")
            replay_provider = _DeleteProvider()
            replay_views = _Views()
            replay, _ = await _run_service(
                Session,
                publication_id,
                handle=replay_handle,
                provider=replay_provider,
                views=replay_views,
            )
            assert replay.outcome == "ambiguous"
            assert replay_provider.delete_calls == []
            assert replay_views.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_cancellation_records_unknown_or_reserved_and_never_replays(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal_repeat_views(Session, seed=2, now=now)
            handle = await _acquire(Session, publication_id, holder="cancel")
            provider = _DeleteProvider([asyncio.CancelledError()])
            with pytest.raises(asyncio.CancelledError):
                await _run_service(Session, publication_id, handle=handle, provider=provider)
            assert len(provider.delete_calls) == 1
            assert (await _action_states(Session, publication_id))[0] in {
                "reserved",
                "unknown",
            }

            replay_provider = _DeleteProvider()
            replay, replay_views = await _run_service(
                Session,
                publication_id,
                handle=handle,
                provider=replay_provider,
                views=_Views(),
            )
            assert replay.outcome == "ambiguous"
            assert replay_provider.delete_calls == []
            assert replay_views.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_partial_batch_stops_after_ambiguous_message(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal_repeat_views(
                Session, seed=3, now=now, message_count=3
            )
            handle = await _acquire(Session, publication_id, holder="partial")
            provider = _DeleteProvider([None, RuntimeError("ambiguous second delete")])
            result, _ = await _run_service(
                Session, publication_id, handle=handle, provider=provider
            )
            assert result.outcome == "ambiguous"
            assert len(provider.delete_calls) == 2
            assert await _action_states(Session, publication_id) == [
                "succeeded",
                "unknown",
            ]

            await _release(Session, handle)
            replay_handle = await _acquire(Session, publication_id, holder="partial-replay")
            replay_provider = _DeleteProvider()
            replay, replay_views = await _run_service(
                Session,
                publication_id,
                handle=replay_handle,
                provider=replay_provider,
                views=_Views(),
            )
            assert replay.outcome == "ambiguous"
            assert replay_provider.delete_calls == []
            assert replay_views.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_success_without_finalize_leaves_reserved_no_replay_barrier(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'crash-window.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal_repeat_views(Session, seed=4, now=now)
            handle = await _acquire(Session, publication_id, holder="crash-window")
            provider = _DeleteProvider()
            result, _ = await _run_service(
                Session,
                publication_id,
                handle=handle,
                provider=provider,
                service_cls=_LoseFinalizeService,
            )
            assert result.outcome == "ambiguous"
            assert len(provider.delete_calls) == 1
            assert await _action_states(Session, publication_id) == ["reserved"]

            replay_provider = _DeleteProvider()
            replay, replay_views = await _run_service(
                Session,
                publication_id,
                handle=handle,
                provider=replay_provider,
                views=_Views(),
            )
            assert replay.outcome == "ambiguous"
            assert replay_provider.delete_calls == []
            assert replay_views.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_terminal_actions_finalize_once_and_replay_zero_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'terminal.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal_repeat_views(Session, seed=5, now=now)
            handle = await _acquire(Session, publication_id, holder="terminal")
            provider = _DeleteProvider([RuntimeError("message to delete not found"), None])
            result, _ = await _run_service(
                Session, publication_id, handle=handle, provider=provider
            )
            assert result.outcome == "deleted"
            assert result.deleted_count == 1
            assert result.unavailable_count == 1
            assert await _action_states(Session, publication_id) == [
                "unavailable",
                "succeeded",
            ]
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None
                runtime = dict(publication.meta or {}).get(AUTODELETE_RUNTIME_META_KEY)
                assert isinstance(runtime, dict)
                assert runtime.get("deleted") is True
                assert state is None

            replay_provider = _DeleteProvider()
            replay, _ = await _run_service(
                Session, publication_id, handle=handle, provider=replay_provider
            )
            assert replay.outcome == "already_deleted"
            assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_stale_reservation_token_or_fingerprint_cannot_finalize(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale-token.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal_repeat_views(Session, seed=6, now=now)
            handle = await _acquire(Session, publication_id, holder="token")
            async with Session() as session:
                service = PublicationAutodeleteViewsDestructiveService(
                    session,
                    view_source=_Views(),
                    delete_provider=_DeleteProvider(),
                    allow_repeat_views=True,
                    lease_handle=handle,
                )
                candidate, _ = await service._candidate(publication_id, now=now)
                assert candidate is not None
                fingerprint = await service._snapshot_fingerprint(candidate)
                assert isinstance(fingerprint, str)
                outcome, reserve_result = await service._reserve(
                    candidate,
                    handle,
                    message_id=candidate.telegram_message_ids[0],
                    fingerprint=fingerprint,
                )
                assert outcome == "reserved"
                assert reserve_result is not None
                reservation = reserve_result.reservation
                assert reservation is not None
                stale = replace(reservation, reservation_token="stale-token")
                stale_fingerprint = replace(reservation, authority_fingerprint="0" * 64)
                ledger = PublicationAutodeleteViewsActionLedger(session)
                assert await ledger.mark_succeeded(stale) is False
                assert await ledger.mark_succeeded(stale_fingerprint) is False
                assert await ledger.mark_unknown(reservation) is True
            assert await _action_states(Session, publication_id) == ["unknown"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_wrong_or_expired_lease_performs_zero_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal_repeat_views(Session, seed=7, now=now)
            handle = await _acquire(Session, publication_id, holder="lease")
            wrong = replace(handle, lease_token="wrong-token")
            provider = _DeleteProvider()
            result, _ = await _run_service(
                Session, publication_id, handle=wrong, provider=provider
            )
            assert result.outcome == "ambiguous"
            assert provider.delete_calls == []
            assert VIEWS_ACTION_LEDGER_META_KEY not in dict(
                (await _publication(Session, publication_id)).meta or {}
            )

            await _release(Session, handle)
            async with Session() as session:
                expired = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="expired",
                    ttl_seconds=30,
                    now=datetime.now(timezone.utc) - timedelta(hours=1),
                    allow_linked=True,
                )
                assert expired is not None
            expired_provider = _DeleteProvider()
            expired_result, _ = await _run_service(
                Session,
                publication_id,
                handle=expired,
                provider=expired_provider,
            )
            assert expired_result.outcome == "ambiguous"
            assert expired_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_source_drift_after_first_delete_blocks_every_later_delete(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drift.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            publication_id = await _seed_terminal_repeat_views(
                Session, seed=8, now=now, message_count=3
            )
            handle = await _acquire(Session, publication_id, holder="drift")
            provider = _DriftAfterFirstDeleteProvider(Session, publication_id)
            with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                await _run_service(Session, publication_id, handle=handle, provider=provider)
            assert len(provider.delete_calls) == 1
            assert await _action_states(Session, publication_id) == ["succeeded"]

            replay_provider = _DeleteProvider()
            with pytest.raises(PublicationAutodeleteViewsSyncConflict):
                await _run_service(
                    Session,
                    publication_id,
                    handle=handle,
                    provider=replay_provider,
                )
            assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_action_ledger_is_not_inherited_by_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'successor.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(timezone.utc)
            source_id = await _seed_terminal_repeat_views(Session, seed=9, now=now)
            handle = await _acquire(Session, source_id, holder="successor")

            async with Session() as session:
                service = PublicationAutodeleteViewsDestructiveService(
                    session,
                    view_source=_Views(),
                    delete_provider=_DeleteProvider(),
                    allow_repeat_views=True,
                    lease_handle=handle,
                )
                candidate, _ = await service._candidate(source_id, now=now)
                assert candidate is not None
                fingerprint = await service._snapshot_fingerprint(candidate)
                assert isinstance(fingerprint, str)
                outcome, reserve_result = await service._reserve(
                    candidate,
                    handle,
                    message_id=candidate.telegram_message_ids[0],
                    fingerprint=fingerprint,
                )
                assert outcome == "reserved"
                assert reserve_result is not None
                assert reserve_result.reservation is not None

            async with Session() as session:
                reserved = await CanonicalRepeatPlanReservationService(session).reserve_next(
                    source_id, after=now
                )
                assert reserved.outcome == "reserved"
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
                assert VIEWS_ACTION_LEDGER_META_KEY in dict(source.meta or {})
                assert VIEWS_ACTION_LEDGER_META_KEY not in dict(successor.meta or {})
                assert AUTODELETE_RUNTIME_META_KEY not in dict(successor.meta or {})
                assert successor.telegram_message_ids in (None, [])
        finally:
            await engine.dispose()

    asyncio.run(run())
