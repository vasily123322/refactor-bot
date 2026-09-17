from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client
from app.domain.publication_autodelete import (
    PublicationAutodeleteAction,
    PublicationAutodeleteViewState,
)
from app.domain.publishing.models import Publication, ScheduleEntry
from app.repositories.content import ContentRepo
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseService
from app.services.publication_mixed_autodelete import PublicationMixedAutodeleteService
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY


class _Views:
    def __init__(self, value: int = 100) -> None:
        self.value = value
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target: str | int, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return self.value


class _Provider:
    def __init__(
        self,
        *,
        failures: dict[int, BaseException] | None = None,
        fail_report: bool = False,
    ) -> None:
        self.failures = dict(failures or {})
        self.fail_report = fail_report
        self.delete_calls: list[tuple[int, int]] = []
        self.report_calls: list[tuple[int, str]] = []

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.delete_calls.append((int(chat_id), int(message_id)))
        failure = self.failures.get(int(message_id))
        if failure is not None:
            raise failure

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool,
    ) -> None:
        self.report_calls.append((int(chat_id), str(text)))
        if self.fail_report:
            raise RuntimeError("report transport failed")


class _LostSuccessFinalizeService(PublicationMixedAutodeleteService):
    async def _best_effort_mark_action(self, reservation, state):
        if state == "succeeded":
            return False
        return await super()._best_effort_mark_action(reservation, state)


async def _new_db(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    Session,
    *,
    seed: int,
    now: datetime,
    message_ids: tuple[int, ...] = (95001,),
    report: bool = False,
) -> tuple[int, int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=509_000 + seed,
            username=f"mixed-{seed}",
            full_name=f"Mixed {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-(100_509_000 + seed),
            title=f"Mixed {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()

        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": f"mixed {seed}"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        options = {
            "autodelete_seconds": 600,
            "autodelete_views": 50,
        }
        if report:
            options["autodelete_report"] = True
        due_at = now - timedelta(minutes=1)
        schedule = ScheduleEntry(
            content_item_id=int(item.id),
            content_revision=int(item.current_revision),
            channel_id=int(channel.id),
            scheduled_at=now - timedelta(hours=1),
            timezone="UTC",
            status="completed",
            repeat_rule={},
            meta={"runtime_options": dict(options)},
        )
        session.add(schedule)
        await session.flush()
        publication = Publication(
            schedule_entry_id=int(schedule.id),
            content_item_id=int(item.id),
            content_revision=int(item.current_revision),
            channel_id=int(channel.id),
            status="published",
            execution_mode="canonical",
            legacy_post_task_id=None,
            telegram_message_ids=list(message_ids),
            result_link=f"https://t.me/c/{seed}/{message_ids[0]}",
            attempt_count=1,
            meta={
                "runtime_options": dict(options),
                AUTODELETE_RUNTIME_META_KEY: {
                    "scheduled_at": due_at.isoformat(),
                    "effective_seconds": 600,
                    "deleted": False,
                },
            },
        )
        session.add(publication)
        await session.flush()
        session.add(
            PublicationAutodeleteViewState(
                publication_id=int(publication.id),
                threshold=50,
                last_views=None,
                last_checked_at=None,
                next_check_at=now - timedelta(minutes=1),
            )
        )
        await session.commit()
        return int(owner.tg_user_id), int(channel.tg_chat_id), int(publication.id)


async def _lease(Session, publication_id: int, holder: str):
    async with Session() as session:
        handle = await PublicationAutodeleteLeaseService(session).acquire(
            publication_id=publication_id,
            holder=holder,
        )
        assert handle is not None
        return handle


def test_timer_winner_blocks_views_and_uses_timer_report(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "timer-wins.db")
        try:
            now = datetime.now(timezone.utc)
            owner_id, chat_id, publication_id = await _seed(
                Session, seed=1, now=now, report=True
            )
            provider = _Provider()
            lease = await _lease(Session, publication_id, "timer-wins")
            async with Session() as session:
                result = await PublicationMixedAutodeleteService(
                    session,
                    delete_provider=provider,
                    allow_report=True,
                ).timer_delete_if_due(publication_id, now=now, lease=lease)
            assert result is not None and result.outcome == "deleted"
            assert provider.delete_calls == [(chat_id, 95001)]
            assert provider.report_calls == [
                (owner_id, "🗑️ Пост удалён по таймеру\nhttps://t.me/c/1/95001")
            ]

            loser_provider = _Provider()
            async with Session() as session:
                loser = await PublicationMixedAutodeleteService(
                    session,
                    view_source=_Views(),
                    delete_provider=loser_provider,
                    allow_report=True,
                ).views_evaluate_and_delete(publication_id, lease=lease, now=now)
            assert loser is not None
            assert loser_provider.delete_calls == []
            assert loser_provider.report_calls == []
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
                assert await session.get(PublicationAutodeleteViewState, publication_id) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_views_winner_blocks_timer_and_uses_views_report(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "views-wins.db")
        try:
            now = datetime.now(timezone.utc)
            owner_id, chat_id, publication_id = await _seed(
                Session, seed=2, now=now, report=True
            )
            provider = _Provider()
            lease = await _lease(Session, publication_id, "views-wins")
            async with Session() as session:
                result = await PublicationMixedAutodeleteService(
                    session,
                    view_source=_Views(),
                    delete_provider=provider,
                    allow_report=True,
                ).views_evaluate_and_delete(publication_id, lease=lease, now=now)
            assert result is not None and result.outcome == "deleted"
            assert provider.delete_calls == [(chat_id, 95001)]
            assert provider.report_calls == [
                (owner_id, "🗑️ Пост удалён по просмотрам\nhttps://t.me/c/2/95001")
            ]

            loser_provider = _Provider()
            async with Session() as session:
                loser = await PublicationMixedAutodeleteService(
                    session,
                    delete_provider=loser_provider,
                    allow_report=True,
                ).timer_delete_if_due(publication_id, now=now, lease=lease)
            assert loser is not None
            assert loser_provider.delete_calls == []
            assert loser_provider.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_timer_views_race_has_exactly_one_delete_winner(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "race.db")
        try:
            now = datetime.now(timezone.utc)
            _, chat_id, publication_id = await _seed(Session, seed=3, now=now)
            entered = asyncio.Event()
            release = asyncio.Event()

            class _BlockingProvider(_Provider):
                async def delete_message(self, *, chat_id: int, message_id: int) -> None:
                    self.delete_calls.append((int(chat_id), int(message_id)))
                    entered.set()
                    await release.wait()

            timer_provider = _BlockingProvider()
            views_provider = _Provider()
            lease = await _lease(Session, publication_id, "shared-race-lease")

            async def timer_trigger():
                async with Session() as session:
                    return await PublicationMixedAutodeleteService(
                        session,
                        delete_provider=timer_provider,
                    ).timer_delete_if_due(publication_id, now=now, lease=lease)

            timer_task = asyncio.create_task(timer_trigger())
            await entered.wait()
            async with Session() as session:
                views = await PublicationMixedAutodeleteService(
                    session,
                    view_source=_Views(),
                    delete_provider=views_provider,
                ).views_evaluate_and_delete(publication_id, lease=lease, now=now)

            assert views is not None and views.outcome == "ambiguous"
            assert views_provider.delete_calls == []
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
            timer = await timer_task
            assert timer is not None and timer.outcome == "deleted"
            assert timer_provider.delete_calls == [(chat_id, 95001)]
            assert len(timer_provider.delete_calls) + len(views_provider.delete_calls) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_views_timer_race_has_exactly_one_delete_winner(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "views-first-race.db")
        try:
            now = datetime.now(timezone.utc)
            _, chat_id, publication_id = await _seed(Session, seed=13, now=now)
            entered = asyncio.Event()
            release = asyncio.Event()

            class _BlockingProvider(_Provider):
                async def delete_message(self, *, chat_id: int, message_id: int) -> None:
                    self.delete_calls.append((int(chat_id), int(message_id)))
                    entered.set()
                    await release.wait()

            views_provider = _BlockingProvider()
            timer_provider = _Provider()
            lease = await _lease(Session, publication_id, "shared-views-first-race-lease")

            async def views_trigger():
                async with Session() as session:
                    return await PublicationMixedAutodeleteService(
                        session,
                        view_source=_Views(),
                        delete_provider=views_provider,
                    ).views_evaluate_and_delete(publication_id, lease=lease, now=now)

            views_task = asyncio.create_task(views_trigger())
            await entered.wait()

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

            async with Session() as session:
                timer = await PublicationMixedAutodeleteService(
                    session,
                    delete_provider=timer_provider,
                ).timer_delete_if_due(publication_id, now=now, lease=lease)

            assert timer is not None and timer.outcome == "ambiguous"
            assert timer_provider.delete_calls == []

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
            views = await views_task
            assert views is not None and views.outcome == "deleted"
            assert views_provider.delete_calls == [(chat_id, 95001)]
            assert len(views_provider.delete_calls) + len(timer_provider.delete_calls) == 1

            async with Session() as session:
                action = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                assert action.state == "succeeded"
                publication = await session.get(Publication, publication_id)
                assert publication is not None
                assert publication.meta[AUTODELETE_RUNTIME_META_KEY]["deleted"] is True
                assert await session.get(PublicationAutodeleteViewState, publication_id) is None
        finally:
            release.set()
            await engine.dispose()

    asyncio.run(run())

def test_committed_reserved_without_provider_call_is_permanent_no_replay(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "reserved-no-replay.db")
        try:
            now = datetime.now(timezone.utc)
            _, _, publication_id = await _seed(Session, seed=4, now=now)
            lease = await _lease(Session, publication_id, "reserved-crash")
            async with Session() as session:
                service = PublicationMixedAutodeleteService(
                    session,
                    delete_provider=_Provider(),
                )
                candidate, early = await service._candidate(publication_id)
                assert candidate is not None and early is not None
                outcome, reserved = await service._reserve_message(
                    candidate,
                    lease,
                    message_id=95001,
                    now=now,
                )
                assert outcome == "reserved"
                assert reserved is not None and reserved.reservation is not None

            replay_provider = _Provider()
            async with Session() as session:
                replay = await PublicationMixedAutodeleteService(
                    session,
                    view_source=_Views(),
                    delete_provider=replay_provider,
                ).views_evaluate_and_delete(publication_id, lease=lease, now=now)
            assert replay is not None and replay.outcome == "ambiguous"
            assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unknown_and_multimessage_ambiguity_block_opposite_trigger(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "unknown-no-replay.db")
        try:
            now = datetime.now(timezone.utc)
            ids = (95101, 95102, 95103)
            _, chat_id, publication_id = await _seed(
                Session, seed=5, now=now, message_ids=ids
            )
            first_provider = _Provider(
                failures={ids[1]: RuntimeError("provider outcome unknown")}
            )
            lease = await _lease(Session, publication_id, "unknown-winner")
            async with Session() as session:
                first = await PublicationMixedAutodeleteService(
                    session,
                    delete_provider=first_provider,
                ).timer_delete_if_due(publication_id, now=now, lease=lease)
            assert first is not None and first.outcome == "ambiguous"
            assert first_provider.delete_calls == [(chat_id, ids[0]), (chat_id, ids[1])]

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
                assert [(int(row.telegram_message_id), row.state) for row in actions] == [
                    (ids[0], "succeeded"),
                    (ids[1], "unknown"),
                ]

            replay_provider = _Provider()
            async with Session() as session:
                replay = await PublicationMixedAutodeleteService(
                    session,
                    view_source=_Views(),
                    delete_provider=replay_provider,
                ).views_evaluate_and_delete(publication_id, lease=lease, now=now)
            assert replay is not None and replay.outcome == "ambiguous"
            assert replay_provider.delete_calls == []
            assert ids[2] not in [message_id for _, message_id in first_provider.delete_calls]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_success_with_lost_finalize_stays_reserved_and_never_replays(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "lost-finalize.db")
        try:
            now = datetime.now(timezone.utc)
            _, chat_id, publication_id = await _seed(Session, seed=6, now=now)
            provider = _Provider()
            lease = await _lease(Session, publication_id, "lost-finalize")
            async with Session() as session:
                result = await _LostSuccessFinalizeService(
                    session,
                    delete_provider=provider,
                ).timer_delete_if_due(publication_id, now=now, lease=lease)
            assert result is not None and result.outcome == "ambiguous"
            assert provider.delete_calls == [(chat_id, 95001)]
            async with Session() as session:
                action = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id == publication_id
                        )
                    )
                ).scalar_one()
                assert action.state == "reserved"

            replay_provider = _Provider()
            async with Session() as session:
                replay = await PublicationMixedAutodeleteService(
                    session,
                    view_source=_Views(),
                    delete_provider=replay_provider,
                ).views_evaluate_and_delete(publication_id, lease=lease, now=now)
            assert replay is not None and replay.outcome == "ambiguous"
            assert replay_provider.delete_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_report_failure_is_best_effort_and_cannot_reauthorize_delete(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "report-failure.db")
        try:
            now = datetime.now(timezone.utc)
            _, chat_id, publication_id = await _seed(
                Session, seed=7, now=now, report=True
            )
            provider = _Provider(fail_report=True)
            lease = await _lease(Session, publication_id, "report-failure")
            async with Session() as session:
                result = await PublicationMixedAutodeleteService(
                    session,
                    view_source=_Views(),
                    delete_provider=provider,
                    allow_report=True,
                ).views_evaluate_and_delete(publication_id, lease=lease, now=now)
            assert result is not None and result.outcome == "deleted"
            assert provider.delete_calls == [(chat_id, 95001)]
            assert len(provider.report_calls) == 1

            replay_provider = _Provider()
            async with Session() as session:
                replay = await PublicationMixedAutodeleteService(
                    session,
                    delete_provider=replay_provider,
                    allow_report=True,
                ).timer_delete_if_due(publication_id, now=now, lease=lease)
            assert replay is not None
            assert replay_provider.delete_calls == []
            assert replay_provider.report_calls == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_time_only_probe_falls_through_without_views_state(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "time-only-fallthrough.db")
        try:
            now = datetime.now(timezone.utc)
            _, _, publication_id = await _seed(Session, seed=14, now=now)
            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert publication is not None and state is not None
                meta = dict(publication.meta or {})
                meta["runtime_options"] = {"autodelete_seconds": 600}
                publication.meta = meta
                await session.delete(state)
                await session.commit()

            provider = _Provider()
            async with Session() as session:
                result = await PublicationMixedAutodeleteService(
                    session,
                    delete_provider=provider,
                ).timer_delete_if_due(publication_id, now=now)
            assert result is None
            assert provider.delete_calls == []

            async with Session() as session:
                actions = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert actions == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_mixed_missing_views_state_remains_fail_closed(tmp_path) -> None:
    async def run() -> None:
        engine, Session = await _new_db(tmp_path / "mixed-missing-views-state.db")
        try:
            now = datetime.now(timezone.utc)
            _, _, publication_id = await _seed(Session, seed=15, now=now)
            async with Session() as session:
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                assert state is not None
                await session.delete(state)
                await session.commit()

            provider = _Provider()
            async with Session() as session:
                result = await PublicationMixedAutodeleteService(
                    session,
                    delete_provider=provider,
                ).timer_delete_if_due(publication_id, now=now)
            assert result is not None
            assert result.outcome == "ineligible"
            assert provider.delete_calls == []

            async with Session() as session:
                actions = (
                    await session.execute(
                        select(PublicationAutodeleteAction).where(
                            PublicationAutodeleteAction.publication_id == publication_id
                        )
                    )
                ).scalars().all()
                assert actions == []
        finally:
            await engine.dispose()

    asyncio.run(run())

