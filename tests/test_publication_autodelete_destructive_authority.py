from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteAction
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete import (
    PublicationAutodeleteService,
    PublicationAutodeleteSyncConflict,
)
from app.services.publication_autodelete_action_ledger import (
    PublicationAutodeleteActionLedger,
)
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseService
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


class RecordingProvider:
    def __init__(self, failures: dict[int, BaseException] | None = None) -> None:
        self.failures = dict(failures or {})
        self.calls: list[tuple[int, int]] = []

    async def delete_message(self, *, chat_id: int, message_id: int):
        self.calls.append((int(chat_id), int(message_id)))
        failure = self.failures.get(int(message_id))
        if failure is not None:
            raise failure
        return True

    async def send_message(self, **kwargs):
        return True


async def _seed(
    Session,
    *,
    seed: int,
    due_at: datetime,
    message_ids: list[int],
) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=88000 + seed,
            username=f"destructive-authority-{seed}",
            full_name=f"Destructive Authority {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(10088000 + seed),
            title=f"Destructive Authority {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"Delete {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            runtime_options={"autodelete_seconds": 3600},
        )
        task = await session.get(PostTask, int(publication.legacy_post_task_id or 0))
        schedule = await session.get(
            ScheduleEntry,
            int(publication.schedule_entry_id or 0),
        )
        assert task is not None and schedule is not None
        task.status = "done"
        publication.status = "published"
        publication.telegram_message_ids = list(message_ids)
        schedule.status = "completed"
        publication.meta = {
            **dict(publication.meta or {}),
            "runtime_options": {"autodelete_seconds": 3600},
            AUTODELETE_RUNTIME_META_KEY: {
                "scheduled_at": due_at.astimezone(timezone.utc).isoformat(),
                "effective_seconds": 3600,
                "deleted": False,
            },
        }
        publication.legacy_post_task_id = None
        await session.delete(task)
        await session.commit()
        return int(channel.id), int(publication.id)


def test_snapshot_drift_before_destructive_proof_calls_no_provider(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'snapshot-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            _, publication_id = await _seed(
                Session,
                seed=1,
                due_at=now - timedelta(minutes=1),
                message_ids=[99101],
            )
            provider = RecordingProvider()

            class DriftService(PublicationAutodeleteService):
                changed = False

                async def _reserve_message(self, candidate, handle, *, message_id, now):
                    if not self.changed:
                        self.changed = True
                        async with Session() as mutation:
                            publication = await mutation.get(Publication, publication_id)
                            assert publication is not None
                            meta = dict(publication.meta or {})
                            runtime = dict(meta[AUTODELETE_RUNTIME_META_KEY])
                            runtime["scheduled_at"] = (
                                now + timedelta(hours=1)
                            ).isoformat()
                            meta[AUTODELETE_RUNTIME_META_KEY] = runtime
                            publication.meta = meta
                            await mutation.commit()
                    return await super()._reserve_message(
                        candidate,
                        handle,
                        message_id=message_id,
                        now=now,
                    )

            async with Session() as session:
                with pytest.raises(PublicationAutodeleteSyncConflict):
                    await DriftService(
                        session,
                        provider=provider,
                    ).delete_if_due(publication_id, now=now)
            assert provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_message_and_chat_identity_drift_calls_no_provider(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'identity-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            channel_id, publication_id = await _seed(
                Session,
                seed=2,
                due_at=now - timedelta(minutes=1),
                message_ids=[99201],
            )
            provider = RecordingProvider()

            class DriftService(PublicationAutodeleteService):
                changed = False

                async def _reserve_message(self, candidate, handle, *, message_id, now):
                    if not self.changed:
                        self.changed = True
                        async with Session() as mutation:
                            publication = await mutation.get(Publication, publication_id)
                            channel = await mutation.get(Channel, channel_id)
                            assert publication is not None and channel is not None
                            publication.telegram_message_ids = [999201]
                            channel.tg_chat_id = int(channel.tg_chat_id) - 1
                            await mutation.commit()
                    return await super()._reserve_message(
                        candidate,
                        handle,
                        message_id=message_id,
                        now=now,
                    )

            async with Session() as session:
                with pytest.raises(PublicationAutodeleteSyncConflict):
                    await DriftService(
                        session,
                        provider=provider,
                    ).delete_if_due(publication_id, now=now)
            assert provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_competing_workers_only_one_reaches_destructive_provider(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'competing-workers.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            _, publication_id = await _seed(
                Session,
                seed=3,
                due_at=now - timedelta(minutes=1),
                message_ids=[99301],
            )
            entered = asyncio.Event()
            release = asyncio.Event()

            class BlockingProvider(RecordingProvider):
                async def delete_message(self, *, chat_id: int, message_id: int):
                    self.calls.append((int(chat_id), int(message_id)))
                    entered.set()
                    await release.wait()
                    return True

            first_provider = BlockingProvider()
            second_provider = RecordingProvider()

            async def first_worker():
                async with Session() as session:
                    return await PublicationAutodeleteService(
                        session,
                        provider=first_provider,
                    ).delete_if_due(publication_id, now=now)

            first_task = asyncio.create_task(first_worker())
            await entered.wait()

            async with Session() as session:
                second = await PublicationAutodeleteService(
                    session,
                    provider=second_provider,
                ).delete_if_due(publication_id, now=now)

            assert second.outcome == "retry"
            assert second_provider.calls == []

            async with Session() as session:
                actions = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert len(actions) == 1
                assert actions[0].state == "reserved"

            release.set()
            first = await first_task
            assert first.outcome == "deleted"
            assert len(first_provider.calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_action_token_and_fingerprint_cannot_finish_or_reauthorize(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'exact-token.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            _, publication_id = await _seed(
                Session,
                seed=4,
                due_at=now - timedelta(minutes=1),
                message_ids=[99401],
            )

            async with Session() as session:
                lease = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="exact-token-test",
                    now=now,
                )
                assert lease is not None
                service = PublicationAutodeleteService(
                    session,
                    provider=RecordingProvider(),
                )
                candidate, early = await service._candidate(publication_id, now=now)
                assert candidate is not None
                assert early.outcome == "retry"
                outcome, reserved = await service._reserve_message(
                    candidate,
                    lease,
                    message_id=99401,
                    now=now,
                )
                assert outcome == "reserved"
                reservation = reserved.reservation
                assert reservation is not None

                stale = replace(reservation, reservation_token="stale-token")
                ledger = PublicationAutodeleteActionLedger(session)
                assert await ledger.mark_succeeded(stale, finished_at=now) is False
                assert await ledger.mark_succeeded(
                    reservation,
                    finished_at=now,
                ) is True
                assert await PublicationAutodeleteLeaseService(session).release(lease)

            async with Session() as session:
                new_lease = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="fingerprint-test",
                    now=now + timedelta(seconds=1),
                )
                assert new_lease is not None
                result = await PublicationAutodeleteActionLedger(session).reserve(
                    new_lease,
                    telegram_chat_id=reservation.telegram_chat_id,
                    telegram_message_id=reservation.telegram_message_id,
                    authority_fingerprint="f" * 64,
                    now=now + timedelta(seconds=1),
                )
                assert result.outcome == "conflict"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_ambiguity_blocks_batch_and_all_future_replay(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'provider-ambiguity.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            _, publication_id = await _seed(
                Session,
                seed=5,
                due_at=now - timedelta(minutes=1),
                message_ids=[99501, 99502, 99503],
            )
            first_provider = RecordingProvider(
                {99502: RuntimeError("provider outcome unknown")}
            )
            async with Session() as session:
                first = await PublicationAutodeleteService(
                    session,
                    provider=first_provider,
                ).delete_if_due(publication_id, now=now)

            assert first.outcome == "ambiguous"
            assert first_provider.calls == [
                (-10088005, 99501),
                (-10088005, 99502),
            ]

            async with Session() as session:
                actions = (
                    await session.execute(
                        select(PublicationAutodeleteAction)
                        .where(
                            PublicationAutodeleteAction.publication_id == publication_id
                        )
                        .order_by(PublicationAutodeleteAction.telegram_message_id)
                    )
                ).scalars().all()
                assert [
                    (int(action.telegram_message_id), str(action.state))
                    for action in actions
                ] == [
                    (99501, "succeeded"),
                    (99502, "unknown"),
                ]
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is False

            replay_provider = RecordingProvider()
            async with Session() as session:
                replay = await PublicationAutodeleteService(
                    session,
                    provider=replay_provider,
                ).delete_if_due(
                    publication_id,
                    now=now + timedelta(minutes=1),
                )
            assert replay.outcome == "ambiguous"
            assert replay_provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_post_provider_db_drift_conflicts_without_redirect_or_replay(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'post-provider-drift.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            _, publication_id = await _seed(
                Session,
                seed=6,
                due_at=now - timedelta(minutes=1),
                message_ids=[99601],
            )

            class MutatingProvider(RecordingProvider):
                async def delete_message(self, *, chat_id: int, message_id: int):
                    self.calls.append((int(chat_id), int(message_id)))
                    async with Session() as mutation:
                        publication = await mutation.get(Publication, publication_id)
                        assert publication is not None
                        publication.telegram_message_ids = [99699]
                        await mutation.commit()
                    return True

            first_provider = MutatingProvider()
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteSyncConflict):
                    await PublicationAutodeleteService(
                        session,
                        provider=first_provider,
                    ).delete_if_due(publication_id, now=now)
            assert first_provider.calls == [(-10088006, 99601)]

            replay_provider = RecordingProvider()
            async with Session() as session:
                with pytest.raises(PublicationAutodeleteSyncConflict):
                    await PublicationAutodeleteService(
                        session,
                        provider=replay_provider,
                    ).delete_if_due(
                        publication_id,
                        now=now + timedelta(minutes=1),
                    )
            assert replay_provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_normal_success_persists_terminal_actions_and_replay_is_provider_free(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'normal-success.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            _, publication_id = await _seed(
                Session,
                seed=7,
                due_at=now - timedelta(minutes=1),
                message_ids=[99701, 99702],
            )
            provider = RecordingProvider()
            async with Session() as session:
                result = await PublicationAutodeleteService(
                    session,
                    provider=provider,
                ).delete_if_due(publication_id, now=now)
            assert result.outcome == "deleted"
            assert provider.calls == [
                (-10088007, 99701),
                (-10088007, 99702),
            ]

            async with Session() as session:
                actions = (
                    await session.execute(
                        select(PublicationAutodeleteAction)
                        .where(
                            PublicationAutodeleteAction.publication_id == publication_id
                        )
                        .order_by(PublicationAutodeleteAction.telegram_message_id)
                    )
                ).scalars().all()
                assert [str(action.state) for action in actions] == [
                    "succeeded",
                    "succeeded",
                ]
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True

            replay_provider = RecordingProvider()
            async with Session() as session:
                replay = await PublicationAutodeleteService(
                    session,
                    provider=replay_provider,
                ).delete_if_due(publication_id, now=now + timedelta(minutes=1))
            assert replay.outcome == "already_deleted"
            assert replay_provider.calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_autodelete_lease_cannot_be_renewed(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'lease-expiry.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
            _, publication_id = await _seed(
                Session,
                seed=8,
                due_at=now - timedelta(minutes=1),
                message_ids=[99801],
            )
            async with Session() as session:
                lease = await PublicationAutodeleteLeaseService(session).acquire(
                    publication_id=publication_id,
                    holder="lease-expiry-test",
                    ttl_seconds=30,
                    now=now,
                )
                assert lease is not None

            async with Session() as session:
                assert await PublicationAutodeleteLeaseService(session).renew(
                    lease,
                    ttl_seconds=30,
                    now=lease.expires_at,
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(run())
