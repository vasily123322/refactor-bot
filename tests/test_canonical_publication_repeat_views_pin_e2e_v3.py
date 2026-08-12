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
from app.domain.publication_delivery import PublicationDeliveryAction, PublicationDeliveryLease
from app.domain.publishing.models import Publication, PublicationAttempt
from app.repositories.content import ContentRepo
from app.services.canonical_publication_delivery_live_post_action_executor import (
    CanonicalPublicationDeliveryLivePostActionExecutor,
)
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.canonical_publication_safe_repeat_delivery_executor import (
    CanonicalPublicationSafeRepeatDeliveryExecutor,
)
from app.services.publication_autodelete_views_action_ledger import (
    AUTODELETE_VIEWS_ACTIONS_META_KEY,
)
from app.services.publication_bridge import LegacyPublicationBridge
from app.services.publication_runtime import AUTODELETE_RUNTIME_META_KEY
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker
from app.workers.publication_autodelete_views_pin import PublicationAutodeleteViewsPinWorker


async def _seed(Session, *, seed: int, threshold: int) -> dict[str, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=225000 + seed,
            username=f"repeat-views-pin-e2e-{seed}",
            full_name=f"Repeat Views Pin E2E {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100225000 + seed),
            title=f"Repeat Views Pin E2E {seed}",
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
                        "text": f"repeat views pin e2e {seed}",
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
                "pin_on": True,
                "autodelete_views": int(threshold),
                "autodelete_report": True,
            },
        )
        assert publication.legacy_post_task_id is not None
        return {
            "publication_id": int(publication.id),
            "task_id": int(publication.legacy_post_task_id),
            "source_tg": int(source.tg_chat_id),
            "owner_tg": int(owner.tg_user_id),
            "threshold": int(threshold),
        }


class _Sender:
    def __init__(self, message_id: int) -> None:
        self.message_id = int(message_id)
        self.calls: list[dict] = []

    async def send_document(self, chat_id, document, **kwargs) -> list[int]:
        self.calls.append({"chat_id": int(chat_id), **dict(kwargs)})
        return [self.message_id]


class _PinBot:
    def __init__(self) -> None:
        self.pin_calls: list[tuple[int, int]] = []
        self.forward_calls: list[dict] = []

    async def pin_chat_message(self, *, chat_id: int, message_id: int) -> None:
        self.pin_calls.append((int(chat_id), int(message_id)))

    async def forward_message(self, **kwargs) -> None:
        self.forward_calls.append(dict(kwargs))
        raise AssertionError("views+pin E2E must not forward")


class _PinHook:
    def __init__(self, executor: CanonicalPublicationDeliveryLivePostActionExecutor) -> None:
        self.executor = executor

    async def execute(self, context) -> None:
        await self.executor.execute(context)


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


async def _build_primary(Session, sender: _Sender, pin_bot: _PinBot):
    post_actions = CanonicalPublicationDeliveryLivePostActionExecutor(
        bot=pin_bot,
        session_factory=Session,
    )
    delegate = CanonicalPublicationSafeRepeatDeliveryExecutor(
        Session,
        sender=sender,
        post_send_hook=_PinHook(post_actions),
        allow_repeat=True,
        allow_views_autodelete=True,
        allow_repeat_views=True,
        allow_repeat_views_pin=True,
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


async def _source_pin_action(Session, publication_id: int) -> PublicationDeliveryAction:
    async with Session() as session:
        actions = list(
            (
                await session.execute(
                    select(PublicationDeliveryAction).where(
                        PublicationDeliveryAction.publication_id == publication_id
                    )
                )
            ).scalars().all()
        )
        assert len(actions) == 1
        action = actions[0]
        assert action.action_type == "pin"
        assert str(action.action_key).startswith("pin:")
        assert len(str(action.intent_fingerprint)) == 64
        assert str(action.reserved_by_lease_token)
        return action


def _single_views_action(meta: dict, message_id: int) -> tuple[dict, dict]:
    ledger = meta.get(AUTODELETE_VIEWS_ACTIONS_META_KEY)
    assert isinstance(ledger, dict)
    actions = ledger.get("actions")
    assert isinstance(actions, dict)
    assert set(actions) == {str(message_id)}
    action = actions[str(message_id)]
    assert isinstance(action, dict)
    return ledger, action


async def _assert_published_source(
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
        assert await session.get(PublicationDeliveryLease, seeded["publication_id"]) is None
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


async def _assert_one_pristine_successor(
    Session,
    source_id: int,
    *,
    threshold: int,
) -> int:
    successors = await _successors(Session, source_id)
    assert len(successors) == 1
    successor = successors[0]
    successor_id = int(successor.id)
    assert successor.status == "queued"
    assert int(successor.attempt_count or 0) == 0
    assert successor.telegram_message_ids in (None, [])
    assert successor.result_link is None
    assert successor.last_error is None

    meta = dict(successor.meta or {})
    assert meta.get("runtime_options") == {
        "silent": True,
        "pin_on": True,
        "autodelete_views": int(threshold),
        "autodelete_report": True,
    }
    assert AUTODELETE_VIEWS_ACTIONS_META_KEY not in meta
    assert AUTODELETE_RUNTIME_META_KEY not in meta
    assert "reservation_token" not in repr(meta)
    assert "authority_fingerprint" not in repr(meta)
    assert "unknown" not in repr(meta)

    async with Session() as session:
        assert await session.get(PublicationAutodeleteViewState, successor_id) is None
        assert await session.get(PublicationDeliveryLease, successor_id) is None
        attempts = list(
            (
                await session.execute(
                    select(PublicationAttempt).where(
                        PublicationAttempt.publication_id == successor_id
                    )
                )
            ).scalars().all()
        )
        assert attempts == []
        actions = list(
            (
                await session.execute(
                    select(PublicationDeliveryAction).where(
                        PublicationDeliveryAction.publication_id == successor_id
                    )
                )
            ).scalars().all()
        )
        assert actions == []
        assert successor.legacy_post_task_id is not None
        task = await session.get(PostTask, int(successor.legacy_post_task_id))
        assert task is not None and task.status == "pending"
        payload = dict(task.payload or {})
        assert payload.get("repeat_on") is True
        assert payload.get("repeat_seconds") == 60
        assert payload.get("silent") is True
        assert payload.get("pin_on") is True
        assert payload.get("autodelete_views") == int(threshold)
        assert payload.get("autodelete_report") is True
        assert payload.get("result_ids") in (None, [])
        assert payload.get("autodeleted") in (None, False)
        assert "reservation_token" not in repr(payload)
        assert "authority_fingerprint" not in repr(payload)
    return successor_id


def test_repeat_views_pin_clean_lifecycle_is_single_effect_and_successor_is_pristine(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-pin-clean-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1, threshold=13)
            sender = _Sender(9101)
            pin_bot = _PinBot()
            router = await _build_primary(Session, sender, pin_bot)

            first = await router.execute(seeded["publication_id"])
            assert first.outcome == "published"
            assert first.message_ids == (9101,)
            assert len(sender.calls) == 1
            assert sender.calls[0]["chat_id"] == seeded["source_tg"]
            assert sender.calls[0].get("disable_notification") is True
            assert pin_bot.pin_calls == [(seeded["source_tg"], 9101)]
            assert pin_bot.forward_calls == []
            await _assert_published_source(Session, seeded, message_id=9101)

            pin_action = await _source_pin_action(Session, seeded["publication_id"])
            assert pin_action.state == "succeeded"

            views = _Views(seeded["threshold"])
            delete_provider = _DeleteProvider()
            delete_worker = PublicationAutodeleteViewsPinWorker(
                view_source=views,
                delete_provider=delete_provider,
                session_factory=Session,
                batch_size=10,
                next_check_seconds=15,
                allow_repeat_views=True,
                allow_repeat_views_pin=True,
            )
            delete_tick = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=2)
            )
            assert delete_tick.deleted == 1
            assert delete_tick.ambiguous == 0
            assert views.calls == [(seeded["source_tg"], 9101)]
            assert delete_provider.delete_calls == [(seeded["source_tg"], 9101)]
            assert delete_provider.report_calls == [seeded["owner_tg"]]

            async with Session() as session:
                source = await session.get(Publication, seeded["publication_id"])
                assert source is not None
                assert await session.get(
                    PublicationAutodeleteViewState,
                    seeded["publication_id"],
                ) is None
                meta = dict(source.meta or {})
                runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
                assert isinstance(runtime, dict)
                assert runtime.get("mode") == "views"
                assert runtime.get("deleted") is True
                ledger, action = _single_views_action(meta, 9101)
                assert int(ledger["threshold"]) == seeded["threshold"]
                assert int(ledger["observed_views"]) == seeded["threshold"]
                assert len(str(ledger["authority_fingerprint"])) == 64
                assert action["state"] in {"succeeded", "unavailable"}
                assert action["authority_fingerprint"] == ledger["authority_fingerprint"]
                assert str(action["reservation_token"])
                assert str(action["autodelete_lease_token"])
                assert str(action["autodelete_lease_holder"])
                assert int(action["telegram_chat_id"]) == seeded["source_tg"]
                assert int(action["telegram_message_id"]) == 9101

            continuation = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            continuation_tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert continuation_tick.materialized == 1
            assert continuation_tick.conflicts == 0
            successor_id = await _assert_one_pristine_successor(
                Session,
                seeded["publication_id"],
                threshold=seeded["threshold"],
            )

            primary_replay = await router.execute(seeded["publication_id"])
            assert primary_replay.outcome == "ineligible"
            assert len(sender.calls) == 1
            assert pin_bot.pin_calls == [(seeded["source_tg"], 9101)]

            continuation_replay = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=4)
            )
            assert continuation_replay.materialized == 0
            successors = await _successors(Session, seeded["publication_id"])
            assert len(successors) == 1
            assert int(successors[0].id) == successor_id

            views_before = list(views.calls)
            delete_replay = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=5)
            )
            assert delete_replay.deleted == 0
            assert delete_provider.delete_calls == [(seeded["source_tg"], 9101)]
            assert views.calls == views_before
            assert pin_bot.pin_calls == [(seeded["source_tg"], 9101)]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_pin_ambiguous_delete_never_reauthorizes_any_prior_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-pin-ambiguous-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2, threshold=17)
            sender = _Sender(9201)
            pin_bot = _PinBot()
            router = await _build_primary(Session, sender, pin_bot)

            first = await router.execute(seeded["publication_id"])
            assert first.outcome == "published"
            assert len(sender.calls) == 1
            assert pin_bot.pin_calls == [(seeded["source_tg"], 9201)]
            pin_action = await _source_pin_action(Session, seeded["publication_id"])
            assert pin_action.state == "succeeded"

            views = _Views(seeded["threshold"] + 4)
            delete_provider = _DeleteProvider(
                [RuntimeError("provider connection lost after destructive request")]
            )
            delete_worker = PublicationAutodeleteViewsPinWorker(
                view_source=views,
                delete_provider=delete_provider,
                session_factory=Session,
                batch_size=10,
                next_check_seconds=15,
                allow_repeat_views=True,
                allow_repeat_views_pin=True,
            )
            first_delete = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=2)
            )
            assert first_delete.deleted == 0
            assert first_delete.ambiguous == 1
            assert delete_provider.delete_calls == [(seeded["source_tg"], 9201)]

            async with Session() as session:
                source = await session.get(Publication, seeded["publication_id"])
                assert source is not None
                state = await session.get(
                    PublicationAutodeleteViewState,
                    seeded["publication_id"],
                )
                assert state is not None
                meta = dict(source.meta or {})
                assert AUTODELETE_RUNTIME_META_KEY not in meta
                ledger, action = _single_views_action(meta, 9201)
                assert int(ledger["observed_views"]) == seeded["threshold"] + 4
                assert action["state"] in {"reserved", "unknown"}
                assert action["authority_fingerprint"] == ledger["authority_fingerprint"]
                assert str(action["reservation_token"])
                assert str(action["autodelete_lease_token"])

            continuation = CanonicalRepeatContinuationWorker(
                session_factory=Session,
                batch_size=10,
                scan_limit=50,
            )
            continuation_tick = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=3)
            )
            assert continuation_tick.materialized == 1
            assert continuation_tick.conflicts == 0
            successor_id = await _assert_one_pristine_successor(
                Session,
                seeded["publication_id"],
                threshold=seeded["threshold"],
            )

            primary_replay = await router.execute(seeded["publication_id"])
            assert primary_replay.outcome == "ineligible"
            assert len(sender.calls) == 1
            assert pin_bot.pin_calls == [(seeded["source_tg"], 9201)]
            assert pin_bot.forward_calls == []

            continuation_replay = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=4)
            )
            assert continuation_replay.materialized == 0
            successors = await _successors(Session, seeded["publication_id"])
            assert len(successors) == 1
            assert int(successors[0].id) == successor_id

            views_before = list(views.calls)
            second_delete = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=400)
            )
            assert second_delete.deleted == 0
            assert second_delete.ambiguous == 1
            assert views.calls == views_before
            assert delete_provider.delete_calls == [(seeded["source_tg"], 9201)]
            assert len(sender.calls) == 1
            assert pin_bot.pin_calls == [(seeded["source_tg"], 9201)]
            assert len(await _successors(Session, seeded["publication_id"])) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
