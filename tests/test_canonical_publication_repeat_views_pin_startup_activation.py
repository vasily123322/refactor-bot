from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
)
from app.core.db import Base
from app.domain.content import PostDocument
from app.domain.models import Channel, Client, PostTask
from app.domain.publication_autodelete import PublicationAutodeleteViewState
from app.domain.publication_delivery import PublicationDeliveryLease
from app.domain.publishing.models import Publication
from app.repositories.content import ContentRepo
from app.services.canonical_publication_repeat_handoff_executor import (
    CanonicalPublicationRepeatHandoffExecutor,
)
from app.services.canonical_publication_safe_repeat_runtime import (
    build_canonical_publication_safe_repeat_runtime,
)
from app.services.publication_bridge import LegacyPublicationBridge


def _config() -> CanonicalPublicationDeliveryPrimarySettings:
    return CanonicalPublicationDeliveryPrimarySettings(
        _env_file=None,
        CANONICAL_PUBLICATION_DELIVERY_WORKER_ENABLED=True,
        CANONICAL_PUBLICATION_DELIVERY_RECOVERY_WORKER_ENABLED=True,
    )


def test_dispatcher_views_worker_publishes_exact_started_pin_fact_only_after_start(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )
        seen: list[dict[str, bool]] = []

        class Worker:
            def __init__(self, **kwargs) -> None:
                self.allow_repeat_views = bool(kwargs["allow_repeat_views"])
                self.allow_repeat_views_pin = bool(kwargs["allow_repeat_views_pin"])
                self._started = False
                seen.append(
                    {
                        "repeat_views": self.allow_repeat_views,
                        "repeat_views_pin": self.allow_repeat_views_pin,
                    }
                )

            @property
            def repeat_views_available(self) -> bool:
                return bool(self._started and self.allow_repeat_views)

            @property
            def repeat_views_pin_available(self) -> bool:
                return bool(
                    self._started
                    and self.allow_repeat_views
                    and self.allow_repeat_views_pin
                )

            async def start(self) -> None:
                self._started = True

            async def stop(self) -> None:
                self._started = False

        monkeypatch.setattr(dispatcher, "PublicationAutodeleteViewsPinWorker", Worker)

        closed = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
            repeat_continuation_available=False,
        )
        assert closed is not None
        assert closed.repeat_views_available is False
        assert closed.repeat_views_pin_available is False

        opened = await dispatcher._start_publication_autodelete_views_worker_if_enabled(
            userbot_available=True,
            repeat_continuation_available=True,
        )
        assert opened is not None
        assert opened.repeat_views_available is True
        assert opened.repeat_views_pin_available is True
        assert seen == [
            {"repeat_views": False, "repeat_views_pin": True},
            {"repeat_views": True, "repeat_views_pin": True},
        ]

    asyncio.run(run())


def test_views_pin_worker_start_failure_is_cleaned_up_before_primary_can_start(
    monkeypatch,
) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        monkeypatch.setattr(
            dispatcher.settings,
            "publication_autodelete_views_worker_enabled",
            True,
        )
        stopped: list[bool] = []

        class FailingWorker:
            def __init__(self, **kwargs) -> None:
                self.repeat_views_available = False
                self.repeat_views_pin_available = False

            async def start(self) -> None:
                raise RuntimeError("views+pin startup failed")

            async def stop(self) -> None:
                stopped.append(True)

        monkeypatch.setattr(
            dispatcher,
            "PublicationAutodeleteViewsPinWorker",
            FailingWorker,
        )

        with pytest.raises(RuntimeError, match="startup failed"):
            await dispatcher._start_publication_autodelete_views_worker_if_enabled(
                userbot_available=True,
                repeat_continuation_available=True,
            )
        assert stopped == [True]

    asyncio.run(run())


def test_primary_startup_forwards_views_pin_fact_independently(monkeypatch) -> None:
    async def run() -> None:
        from app.bot import dispatcher

        recovery = object()
        captured: list[tuple[bool, bool]] = []

        async def start_recovery():
            return recovery

        async def start_primary(**kwargs):
            captured.append(
                (
                    bool(kwargs["repeat_views_executor_available"]),
                    bool(kwargs["repeat_views_pin_executor_available"]),
                )
            )
            return object()

        monkeypatch.setattr(
            dispatcher,
            "_start_canonical_publication_delivery_recovery_worker_if_enabled",
            start_recovery,
        )
        monkeypatch.setattr(
            dispatcher,
            "start_canonical_publication_safe_repeat_primary_if_enabled",
            start_primary,
        )

        await dispatcher._start_canonical_publication_delivery_workers(
            _config(),
            views_autodelete_executor_available=True,
            repeat_continuation_executor_available=True,
            repeat_views_executor_available=True,
            repeat_views_pin_executor_available=False,
        )
        await dispatcher._start_canonical_publication_delivery_workers(
            _config(),
            views_autodelete_executor_available=True,
            repeat_continuation_executor_available=True,
            repeat_views_executor_available=True,
            repeat_views_pin_executor_available=True,
        )
        assert captured == [(True, False), (True, True)]

    asyncio.run(run())


async def _seed_linked_views_pin(Session) -> tuple[int, int]:
    async with Session() as session:
        owner = Client(
            tg_user_id=224101,
            username="views-pin-activation-closed",
            full_name="Views Pin Activation Closed",
            ui_settings={},
        )
        session.add(owner)
        await session.flush()
        channel = Channel(
            tg_chat_id=-100224101,
            title="Views Pin Activation Closed",
            owner_id=int(owner.id),
            is_active=True,
        )
        session.add(channel)
        await session.commit()
        item = await ContentRepo(session).create(
            channel_id=int(channel.id),
            document=PostDocument(
                blocks=[{"id": "b1", "type": "text", "text": "views pin activation"}]
            ),
            created_by_tg_user_id=int(owner.tg_user_id),
        )
        publication = await LegacyPublicationBridge(session).queue(
            content_item_id=int(item.id),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            repeat_rule={"enabled": True, "seconds": 60},
            runtime_options={
                "silent": True,
                "pin_on": True,
                "autodelete_views": 31,
            },
        )
        assert publication.legacy_post_task_id is not None
        return int(publication.id), int(publication.legacy_post_task_id)


class _NoProviderCalls:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"provider boundary must remain closed: {name}")


def test_missing_narrow_started_fact_keeps_linked_occurrence_fully_pristine(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'views-pin-production-closed.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            publication_id, task_id = await _seed_linked_views_pin(Session)
            bot = _NoProviderCalls()
            runtime = build_canonical_publication_safe_repeat_runtime(
                bot=bot,
                session_factory=Session,
                allow_views_autodelete=True,
                allow_repeat=True,
                allow_repeat_views=True,
                allow_repeat_views_pin=False,
                repeat_owner_policy_enforced=True,
            )
            router = CanonicalPublicationRepeatHandoffExecutor(
                executor=runtime.executor,
                session_factory=Session,
            )

            result = await router.execute(publication_id)
            assert result.outcome == "ineligible"
            assert bot.calls == []

            async with Session() as session:
                publication = await session.get(Publication, publication_id)
                task = await session.get(PostTask, task_id)
                state = await session.get(PublicationAutodeleteViewState, publication_id)
                lease = await session.get(PublicationDeliveryLease, publication_id)
                assert publication is not None
                assert publication.status == "queued"
                assert int(publication.attempt_count or 0) == 0
                assert publication.legacy_post_task_id == task_id
                assert publication.telegram_message_ids in (None, [])
                assert task is not None and task.status == "pending"
                assert state is None
                assert lease is None
        finally:
            await engine.dispose()

    asyncio.run(run())
