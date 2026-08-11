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
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.publication_autodelete_views_action_ledger import (
    VIEWS_ACTION_LEDGER_META_KEY,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker
from app.workers.publication_autodelete_views import PublicationAutodeleteViewsWorker


async def _seed(Session, *, seed: int, threshold: int = 7) -> dict[str, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=219000 + seed,
            username=f"repeat-views-e2e-{seed}",
            full_name=f"Repeat Views E2E {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100219000 + seed),
            title=f"Repeat Views E2E {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(source)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"repeat views e2e {seed}",
                    }
                ]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=2),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "autodelete_views": threshold,
            },
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "source_tg": int(source.tg_chat_id),
            "threshold": threshold,
        }


class _Sender:
    def __init__(self, message_id: int) -> None:
        self.message_id = int(message_id)
        self.calls: list[dict] = []

    async def send_document(self, chat_id, document, **kwargs) -> list[int]:
        self.calls.append({"chat_id": int(chat_id), **dict(kwargs)})
        return [self.message_id]


class _Views:
    def __init__(self, views: int) -> None:
        self.views = int(views)
        self.calls: list[tuple[int, int]] = []

    async def get_message_views(self, target: str | int, message_id: int) -> int:
        self.calls.append((int(target), int(message_id)))
        return self.views


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

    async def send_message(self, *, chat_id: int, text: str, **kwargs) -> None:
        self.report_calls.append(int(chat_id))


async def _build_primary(Session, sender: _Sender):
    delegate = CanonicalPublicationSafeRepeatDeliveryExecutor(
        Session,
        sender=sender,
        allow_repeat=True,
        allow_views_autodelete=True,
        allow_repeat_views=True,
        heartbeat_interval_seconds=120,
    )
    return CanonicalPublicationRepeatHandoffExecutor(
        executor=delegate,
        session_factory=Session,
    )


async def _successors(Session, source_id: int) -> list[Publication]:
    async with Session() as session:
        source = await session.get(Publication, source_id)
        assert source is not None
        return list(
            (
                await session.execute(
                    select(Publication).where(
                        Publication.id != source_id,
                        Publication.content_item_id == int(source.content_item_id),
                        Publication.content_revision == int(source.content_revision),
                        Publication.channel_id == int(source.channel_id),
                    )
                )
            ).scalars().all()
        )


async def _assert_published_source_and_staged_views(
    Session,
    seeded: dict[str, int],
    *,
    message_id: int,
) -> None:
    async with Session() as session:
        source = await session.get(Publication, seeded["publication_id"])
        assert source is not None
        assert source.status == "published"
        assert source.legacy_post_task_id is None
        assert source.telegram_message_ids == [message_id]
        assert await session.get(PostTask, seeded["task_id"]) is None
        state = await session.get(
            PublicationAutodeleteViewState,
            seeded["publication_id"],
        )
        assert state is not None
        assert int(state.threshold) == seeded["threshold"]
        assert state.last_views is None
        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == seeded["publication_id"],
                    PublicationAttempt.attempt == 1,
                )
            )
        ).scalar_one()
        assert attempt.status == "published"
        assert attempt.telegram_message_ids == [message_id]
        assert dict(attempt.meta or {}).get("canonical_delivery") is True


async def _assert_one_pristine_successor(Session, source_id: int, *, threshold: int) -> int:
    successors = await _successors(Session, source_id)
    assert len(successors) == 1
    successor = successors[0]
    assert successor.status == "queued"
    assert successor.telegram_message_ids in (None, [])
    assert successor.result_link is None
    assert dict(successor.meta or {}).get("runtime_options") == {
        "silent": True,
        "autodelete_views": threshold,
    }
    assert VIEWS_ACTION_LEDGER_META_KEY not in dict(successor.meta or {})
    assert AUTODELETE_RUNTIME_META_KEY not in dict(successor.meta or {})
    async with Session() as session:
        state = await session.get(PublicationAutodeleteViewState, int(successor.id))
        assert state is None
        assert successor.legacy_post_task_id is not None
        task = await session.get(PostTask, int(successor.legacy_post_task_id))
        assert task is not None
        assert task.status == "pending"
        payload = dict(task.payload or {})
        assert payload.get("repeat_on") is True
        assert payload.get("repeat_seconds") == 60
        assert payload.get("autodelete_views") == threshold
        assert payload.get("result_ids") in (None, [])
        assert payload.get("autodeleted") in (None, False)
    return int(successor.id)


def test_repeat_views_primary_delete_continuation_and_replay_are_single_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1, threshold=7)
            sender = _Sender(7601)
            router = await _build_primary(Session, sender)

            first = await router.execute(seeded["publication_id"])
            assert first.outcome == "published"
            assert first.message_ids == (7601,)
            assert len(sender.calls) == 1
            assert sender.calls[0]["chat_id"] == seeded["source_tg"]
            assert sender.calls[0].get("disable_notification") is True
            await _assert_published_source_and_staged_views(
                Session,
                seeded,
                message_id=7601,
            )

            continuation = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            continuation_tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=2)
            )
            assert continuation_tick.materialized == 1
            assert continuation_tick.conflicts == 0
            successor_id = await _assert_one_pristine_successor(
                Session,
                seeded["publication_id"],
                threshold=seeded["threshold"],
            )

            views = _Views(seeded["threshold"])
            delete_provider = _DeleteProvider()
            delete_worker = PublicationAutodeleteViewsWorker(
                view_source=views,
                delete_provider=delete_provider,
                session_factory=Session,
                batch_size=10,
                next_check_seconds=15,
                allow_repeat_views=True,
            )
            delete_tick = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert delete_tick.deleted == 1
            assert delete_tick.ambiguous == 0
            assert views.calls == [(seeded["source_tg"], 7601)]
            assert delete_provider.delete_calls == [(seeded["source_tg"], 7601)]
            assert delete_provider.report_calls == []

            async with Session() as session:
                source = await session.get(Publication, seeded["publication_id"])
                assert source is not None
                state = await session.get(
                    PublicationAutodeleteViewState,
                    seeded["publication_id"],
                )
                assert state is None
                meta = dict(source.meta or {})
                runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
                assert isinstance(runtime, dict)
                assert runtime.get("mode") == "views"
                assert runtime.get("deleted") is True
                ledger = meta.get(VIEWS_ACTION_LEDGER_META_KEY)
                assert isinstance(ledger, dict)
                actions = ledger.get("actions")
                assert isinstance(actions, list) and len(actions) == 1
                assert actions[0]["state"] == "succeeded"
                assert int(actions[0]["telegram_message_id"]) == 7601

            primary_replay = await router.execute(seeded["publication_id"])
            assert primary_replay.outcome == "ineligible"
            assert len(sender.calls) == 1

            continuation_replay = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=4)
            )
            assert continuation_replay.materialized == 0
            assert len(await _successors(Session, seeded["publication_id"])) == 1
            assert int((await _successors(Session, seeded["publication_id"]))[0].id) == successor_id

            views_calls_before = list(views.calls)
            delete_replay = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=5)
            )
            assert delete_replay.deleted == 0
            assert delete_provider.delete_calls == [(seeded["source_tg"], 7601)]
            assert views.calls == views_calls_before
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_ambiguous_delete_never_replays_or_duplicates_successor(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-ambiguous-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2, threshold=9)
            sender = _Sender(7701)
            router = await _build_primary(Session, sender)

            first = await router.execute(seeded["publication_id"])
            assert first.outcome == "published"
            assert len(sender.calls) == 1

            continuation = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            continuation_tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=2)
            )
            assert continuation_tick.materialized == 1
            assert continuation_tick.conflicts == 0
            successor_id = await _assert_one_pristine_successor(
                Session,
                seeded["publication_id"],
                threshold=seeded["threshold"],
            )

            views = _Views(seeded["threshold"] + 10)
            delete_provider = _DeleteProvider(
                [RuntimeError("transport outcome unknown after delete request")]
            )
            delete_worker = PublicationAutodeleteViewsWorker(
                view_source=views,
                delete_provider=delete_provider,
                session_factory=Session,
                batch_size=10,
                next_check_seconds=15,
                allow_repeat_views=True,
            )
            first_delete = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert first_delete.deleted == 0
            assert first_delete.ambiguous == 1
            assert delete_provider.delete_calls == [(seeded["source_tg"], 7701)]

            async with Session() as session:
                source = await session.get(Publication, seeded["publication_id"])
                assert source is not None
                state = await session.get(
                    PublicationAutodeleteViewState,
                    seeded["publication_id"],
                )
                assert state is not None
                assert AUTODELETE_RUNTIME_META_KEY not in dict(source.meta or {})
                ledger = dict(source.meta or {}).get(VIEWS_ACTION_LEDGER_META_KEY)
                assert isinstance(ledger, dict)
                actions = ledger.get("actions")
                assert isinstance(actions, list) and len(actions) == 1
                assert actions[0]["state"] == "unknown"
                assert int(actions[0]["telegram_message_id"]) == 7701

            primary_replay = await router.execute(seeded["publication_id"])
            assert primary_replay.outcome == "ineligible"
            assert len(sender.calls) == 1

            continuation_replay = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=4)
            )
            assert continuation_replay.materialized == 0
            successors = await _successors(Session, seeded["publication_id"])
            assert len(successors) == 1
            assert int(successors[0].id) == successor_id

            views_calls_before = list(views.calls)
            second_delete = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=5)
            )
            assert second_delete.deleted == 0
            assert second_delete.ambiguous == 1
            assert delete_provider.delete_calls == [(seeded["source_tg"], 7701)]
            assert views.calls == views_calls_before
            assert len(await _successors(Session, seeded["publication_id"])) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
