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
from app.services.canonical_publication_delivery_capability_claim import (
    FORWARD_TARGET_SNAPSHOT_META_KEY,
)
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
from app.workers.publication_autodelete_views_forward import (
    PublicationAutodeleteViewsForwardWorker,
)


async def _seed(Session, *, seed: int, threshold: int) -> dict[str, object]:
    async with Session() as session:
        owner = Client(
            tg_user_id=232000 + seed,
            username=f"repeat-views-forward-e2e-{seed}",
            full_name=f"Repeat Views Forward E2E {seed}",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        source = Channel(
            tg_chat_id=-(100232000 + seed),
            title=f"Repeat Views Forward Source {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_a = Channel(
            tg_chat_id=-(100332000 + seed),
            title=f"Repeat Views Forward A {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        target_b = Channel(
            tg_chat_id=-(100432000 + seed),
            title=f"Repeat Views Forward B {seed}",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add_all([source, target_a, target_b])
        await session.commit()

        ordered_ids = [int(target_b.id), int(target_a.id)]
        ordered_tg = [int(target_b.tg_chat_id), int(target_a.tg_chat_id)]
        item = await ContentRepo(session).create(
            channel_id=int(source.id),
            document=PostDocument(
                blocks=[
                    {
                        "id": "b1",
                        "type": "text",
                        "text": f"repeat views forward e2e {seed}",
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
                "forward_to": ordered_ids,
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
            "target_ids": ordered_ids,
            "target_tg": ordered_tg,
        }


class _Sender:
    def __init__(self, message_id: int) -> None:
        self.message_id = int(message_id)
        self.calls: list[dict] = []

    async def send_document(self, chat_id, document, **kwargs) -> list[int]:
        self.calls.append({"chat_id": int(chat_id), **dict(kwargs)})
        return [self.message_id]


class _ForwardBot:
    def __init__(self) -> None:
        self.forward_calls: list[dict[str, object]] = []
        self.pin_calls: list[tuple[int, int]] = []

    async def pin_chat_message(self, *, chat_id: int, message_id: int) -> None:
        self.pin_calls.append((int(chat_id), int(message_id)))
        raise AssertionError("views+forward profile must not pin")

    async def forward_message(
        self,
        *,
        chat_id: int,
        from_chat_id: int,
        message_id: int,
        disable_notification: bool,
    ) -> None:
        self.forward_calls.append(
            {
                "chat_id": int(chat_id),
                "from_chat_id": int(from_chat_id),
                "message_id": int(message_id),
                "disable_notification": bool(disable_notification),
            }
        )


class _ForwardHook:
    def __init__(
        self,
        executor: CanonicalPublicationDeliveryLivePostActionExecutor,
    ) -> None:
        self.executor = executor
        self.delivery_lease_tokens: list[str] = []

    async def execute(self, context) -> None:
        self.delivery_lease_tokens.append(str(context.lease.lease_token))
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


async def _build_primary(Session, sender: _Sender, forward_bot: _ForwardBot):
    live_actions = CanonicalPublicationDeliveryLivePostActionExecutor(
        bot=forward_bot,
        session_factory=Session,
    )
    hook = _ForwardHook(live_actions)
    delegate = CanonicalPublicationSafeRepeatDeliveryExecutor(
        Session,
        sender=sender,
        post_send_hook=hook,
        allow_repeat=True,
        allow_views_autodelete=True,
        allow_repeat_views=True,
        allow_repeat_views_forward=True,
        heartbeat_interval_seconds=120,
    )
    return (
        CanonicalPublicationRepeatHandoffExecutor(
            executor=delegate,
            session_factory=Session,
        ),
        hook,
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


async def _source_forward_actions(
    Session,
    publication_id: int,
) -> dict[str, PublicationDeliveryAction]:
    async with Session() as session:
        rows = list(
            (
                await session.execute(
                    select(PublicationDeliveryAction).where(
                        PublicationDeliveryAction.publication_id == publication_id
                    )
                )
            ).scalars().all()
        )
        assert rows
        assert all(str(row.action_type) == "forward" for row in rows)
        return {str(row.action_key): row for row in rows}


def _single_views_action(meta: dict, message_id: int) -> tuple[dict, dict]:
    ledger = meta.get(AUTODELETE_VIEWS_ACTIONS_META_KEY)
    assert isinstance(ledger, dict)
    actions = ledger.get("actions")
    assert isinstance(actions, dict)
    assert set(actions) == {str(message_id)}
    action = actions[str(message_id)]
    assert isinstance(action, dict)
    return ledger, action


async def _assert_published_source_and_snapshot(
    Session,
    seeded: dict[str, object],
    *,
    message_id: int,
) -> None:
    publication_id = int(seeded["publication_id"])
    async with Session() as session:
        source = await session.get(Publication, publication_id)
        assert source is not None
        assert source.status == "published"
        assert source.legacy_post_task_id is None
        assert source.telegram_message_ids == [message_id]
        assert await session.get(PostTask, int(seeded["task_id"])) is None
        assert await session.get(PublicationDeliveryLease, publication_id) is None
        state = await session.get(PublicationAutodeleteViewState, publication_id)
        assert state is not None
        assert int(state.threshold) == int(seeded["threshold"])
        assert state.last_views is None
        attempt = (
            await session.execute(
                select(PublicationAttempt).where(
                    PublicationAttempt.publication_id == publication_id,
                    PublicationAttempt.attempt == 1,
                )
            )
        ).scalar_one()
        assert attempt.status == "published"
        assert attempt.telegram_message_ids == [message_id]
        meta = dict(attempt.meta or {})
        assert meta.get("canonical_delivery") is True
        expected_snapshot = [
            {
                "channel_id": int(channel_id),
                "telegram_chat_id": int(chat_id),
            }
            for channel_id, chat_id in zip(
                seeded["target_ids"],
                seeded["target_tg"],
                strict=True,
            )
        ]
        assert meta.get(FORWARD_TARGET_SNAPSHOT_META_KEY) == expected_snapshot


async def _assert_one_pristine_successor(
    Session,
    source_id: int,
    *,
    target_ids: list[int],
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
        "forward_to": list(target_ids),
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
        assert payload.get("forward_to") == list(target_ids)
        assert payload.get("autodelete_views") == int(threshold)
        assert payload.get("autodelete_report") is True
        assert payload.get("result_ids") in (None, [])
        assert payload.get("autodeleted") in (None, False)
        assert "reservation_token" not in repr(payload)
        assert "authority_fingerprint" not in repr(payload)
    return successor_id


def _expected_forward_calls(
    seeded: dict[str, object],
    *,
    message_id: int,
) -> list[dict[str, object]]:
    return [
        {
            "chat_id": int(target_tg),
            "from_chat_id": int(seeded["source_tg"]),
            "message_id": int(message_id),
            "disable_notification": True,
        }
        for target_tg in seeded["target_tg"]
    ]


def test_repeat_views_forward_clean_lifecycle_is_ordered_and_single_effect(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-forward-clean-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=1, threshold=19)
            sender = _Sender(9501)
            forward_bot = _ForwardBot()
            router, hook = await _build_primary(Session, sender, forward_bot)

            first = await router.execute(int(seeded["publication_id"]))
            assert first.outcome == "published"
            assert first.message_ids == (9501,)
            assert len(sender.calls) == 1
            assert sender.calls[0]["chat_id"] == int(seeded["source_tg"])
            assert sender.calls[0].get("disable_notification") is True
            assert forward_bot.pin_calls == []
            assert forward_bot.forward_calls == _expected_forward_calls(
                seeded,
                message_id=9501,
            )
            assert len(hook.delivery_lease_tokens) == 1
            delivery_lease_token = hook.delivery_lease_tokens[0]
            assert delivery_lease_token

            await _assert_published_source_and_snapshot(
                Session,
                seeded,
                message_id=9501,
            )
            action_rows = await _source_forward_actions(
                Session,
                int(seeded["publication_id"]),
            )
            expected_keys = {
                f"forward:{int(channel_id)}:9501"
                for channel_id in seeded["target_ids"]
            }
            assert set(action_rows) == expected_keys
            for row in action_rows.values():
                assert row.state == "succeeded"
                assert len(str(row.intent_fingerprint)) == 64
                assert str(row.reserved_by_lease_token) == delivery_lease_token
                assert row.finished_at is not None

            views = _Views(int(seeded["threshold"]))
            delete_provider = _DeleteProvider()
            delete_worker = PublicationAutodeleteViewsForwardWorker(
                view_source=views,
                delete_provider=delete_provider,
                session_factory=Session,
                batch_size=10,
                next_check_seconds=15,
                allow_repeat_views=True,
                allow_repeat_views_forward=True,
            )
            delete_tick = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=2)
            )
            assert delete_tick.deleted == 1
            assert delete_tick.ambiguous == 0
            assert views.calls == [(int(seeded["source_tg"]), 9501)]
            assert delete_provider.delete_calls == [(int(seeded["source_tg"]), 9501)]
            assert delete_provider.report_calls == [int(seeded["owner_tg"])]

            async with Session() as session:
                source = await session.get(Publication, int(seeded["publication_id"]))
                assert source is not None
                assert await session.get(
                    PublicationAutodeleteViewState,
                    int(seeded["publication_id"]),
                ) is None
                meta = dict(source.meta or {})
                runtime = meta.get(AUTODELETE_RUNTIME_META_KEY)
                assert isinstance(runtime, dict)
                assert runtime.get("mode") == "views"
                assert runtime.get("deleted") is True
                ledger, action = _single_views_action(meta, 9501)
                assert int(ledger["threshold"]) == int(seeded["threshold"])
                assert int(ledger["observed_views"]) == int(seeded["threshold"])
                assert len(str(ledger["authority_fingerprint"])) == 64
                assert action["state"] in {"succeeded", "unavailable"}
                assert action["authority_fingerprint"] == ledger["authority_fingerprint"]
                assert str(action["reservation_token"])
                assert str(action["autodelete_lease_token"])
                assert str(action["autodelete_lease_holder"])
                assert int(action["telegram_chat_id"]) == int(seeded["source_tg"])
                assert int(action["telegram_message_id"]) == 9501

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
                int(seeded["publication_id"]),
                target_ids=list(seeded["target_ids"]),
                threshold=int(seeded["threshold"]),
            )

            primary_replay = await router.execute(int(seeded["publication_id"]))
            assert primary_replay.outcome == "ineligible"
            assert len(sender.calls) == 1
            assert forward_bot.forward_calls == _expected_forward_calls(
                seeded,
                message_id=9501,
            )

            continuation_replay = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=4)
            )
            assert continuation_replay.materialized == 0
            successors = await _successors(Session, int(seeded["publication_id"]))
            assert len(successors) == 1
            assert int(successors[0].id) == successor_id

            views_before = list(views.calls)
            delete_replay = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=5)
            )
            assert delete_replay.deleted == 0
            assert delete_provider.delete_calls == [(int(seeded["source_tg"]), 9501)]
            assert views.calls == views_before
            assert forward_bot.forward_calls == _expected_forward_calls(
                seeded,
                message_id=9501,
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repeat_views_forward_ambiguous_delete_never_redirects_or_reauthorizes(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'repeat-views-forward-ambiguous-e2e.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            seeded = await _seed(Session, seed=2, threshold=23)
            sender = _Sender(9601)
            forward_bot = _ForwardBot()
            router, hook = await _build_primary(Session, sender, forward_bot)

            first = await router.execute(int(seeded["publication_id"]))
            assert first.outcome == "published"
            assert len(sender.calls) == 1
            expected_forwards = _expected_forward_calls(seeded, message_id=9601)
            assert forward_bot.forward_calls == expected_forwards
            assert len(hook.delivery_lease_tokens) == 1
            action_rows = await _source_forward_actions(
                Session,
                int(seeded["publication_id"]),
            )
            assert len(action_rows) == 2
            assert all(row.state == "succeeded" for row in action_rows.values())

            views = _Views(int(seeded["threshold"]) + 5)
            delete_provider = _DeleteProvider(
                [RuntimeError("provider connection lost after DELETE request")]
            )
            delete_worker = PublicationAutodeleteViewsForwardWorker(
                view_source=views,
                delete_provider=delete_provider,
                session_factory=Session,
                batch_size=10,
                next_check_seconds=15,
                allow_repeat_views=True,
                allow_repeat_views_forward=True,
            )
            first_delete = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=2)
            )
            assert first_delete.deleted == 0
            assert first_delete.ambiguous == 1
            assert delete_provider.delete_calls == [(int(seeded["source_tg"]), 9601)]

            async with Session() as session:
                source = await session.get(Publication, int(seeded["publication_id"]))
                assert source is not None
                state = await session.get(
                    PublicationAutodeleteViewState,
                    int(seeded["publication_id"]),
                )
                assert state is not None
                meta = dict(source.meta or {})
                assert AUTODELETE_RUNTIME_META_KEY not in meta
                ledger, action = _single_views_action(meta, 9601)
                assert int(ledger["observed_views"]) == int(seeded["threshold"]) + 5
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
                int(seeded["publication_id"]),
                target_ids=list(seeded["target_ids"]),
                threshold=int(seeded["threshold"]),
            )

            primary_replay = await router.execute(int(seeded["publication_id"]))
            assert primary_replay.outcome == "ineligible"
            assert len(sender.calls) == 1
            assert forward_bot.forward_calls == expected_forwards

            continuation_replay = await continuation.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=4)
            )
            assert continuation_replay.materialized == 0
            successors = await _successors(Session, int(seeded["publication_id"]))
            assert len(successors) == 1
            assert int(successors[0].id) == successor_id

            views_before = list(views.calls)
            second_delete = await delete_worker.run_once(
                now=datetime.now(timezone.utc) + timedelta(seconds=400)
            )
            assert second_delete.deleted == 0
            assert second_delete.ambiguous == 1
            assert views.calls == views_before
            assert delete_provider.delete_calls == [(int(seeded["source_tg"]), 9601)]
            assert len(sender.calls) == 1
            assert forward_bot.forward_calls == expected_forwards
            assert len(await _successors(Session, int(seeded["publication_id"]))) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
