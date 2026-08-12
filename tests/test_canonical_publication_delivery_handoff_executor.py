from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from app.services import canonical_publication_delivery_handoff_executor as module
from app.services.canonical_publication_delivery_handoff_executor import (
    CanonicalPublicationDeliveryHandoffExecutor,
)


class _Session:
    def __init__(self, publication) -> None:
        self.publication = publication

    async def get(self, model, publication_id: int):
        return self.publication


class _SessionContext:
    def __init__(self, publication) -> None:
        self.session = _Session(publication)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _session_factory(publication):
    return lambda: _SessionContext(publication)


@dataclass(frozen=True)
class _DelegateResult:
    outcome: str


class _Delegate:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[int] = []

    async def execute(self, publication_id: int):
        self.events.append("delegate")
        self.calls.append(int(publication_id))
        return _DelegateResult("published")


def test_handoff_executor_delegates_canonical_only_without_handoff(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)

        class UnexpectedHandoff:
            def __init__(self, session) -> None:
                raise AssertionError("canonical-only delivery must not construct handoff")

        monkeypatch.setattr(
            module,
            "CanonicalPublicationLegacyTransportHandoffService",
            UnexpectedHandoff,
        )
        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(
                SimpleNamespace(legacy_post_task_id=None)
            ),  # type: ignore[arg-type]
        )

        result = await wrapper.execute(41)

        assert result.outcome == "published"
        assert delegate.calls == [41]
        assert events == ["delegate"]

    asyncio.run(run())


def test_handoff_executor_commits_linked_handoff_before_delegate(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)

        class SuccessfulHandoff:
            def __init__(self, session) -> None:
                pass

            async def retire_for_canonical_delivery(self, publication_id: int):
                events.append("handoff")
                return SimpleNamespace(outcome="retired")

        monkeypatch.setattr(
            module,
            "CanonicalPublicationLegacyTransportHandoffService",
            SuccessfulHandoff,
        )
        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(
                SimpleNamespace(legacy_post_task_id=9001)
            ),  # type: ignore[arg-type]
        )

        result = await wrapper.execute(42)

        assert result.outcome == "published"
        assert delegate.calls == [42]
        assert events == ["handoff", "delegate"]

    asyncio.run(run())


def test_handoff_executor_blocks_delegate_on_any_nonretired_handoff(monkeypatch) -> None:
    async def run() -> None:
        for handoff_outcome in ("contention", "conflict", "ineligible"):
            events: list[str] = []
            delegate = _Delegate(events)

            class BlockedHandoff:
                def __init__(self, session) -> None:
                    pass

                async def retire_for_canonical_delivery(self, publication_id: int):
                    events.append("handoff")
                    return SimpleNamespace(outcome=handoff_outcome)

            monkeypatch.setattr(
                module,
                "CanonicalPublicationLegacyTransportHandoffService",
                BlockedHandoff,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=_session_factory(
                    SimpleNamespace(legacy_post_task_id=9002)
                ),  # type: ignore[arg-type]
            )

            result = await wrapper.execute(43)

            assert result.outcome == "ineligible"
            assert result.handoff_outcome == handoff_outcome
            assert delegate.calls == []
            assert events == ["handoff"]

    asyncio.run(run())


def test_handoff_executor_propagates_cancellation_without_delegate(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)

        class CancelledHandoff:
            def __init__(self, session) -> None:
                pass

            async def retire_for_canonical_delivery(self, publication_id: int):
                events.append("handoff")
                raise asyncio.CancelledError

        monkeypatch.setattr(
            module,
            "CanonicalPublicationLegacyTransportHandoffService",
            CancelledHandoff,
        )
        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(
                SimpleNamespace(legacy_post_task_id=9003)
            ),  # type: ignore[arg-type]
        )

        try:
            await wrapper.execute(44)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("handoff cancellation must propagate")

        assert delegate.calls == []
        assert events == ["handoff"]

    asyncio.run(run())


def test_handoff_executor_missing_publication_is_ineligible() -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)
        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(None),  # type: ignore[arg-type]
        )

        result = await wrapper.execute(45)

        assert result.outcome == "ineligible"
        assert result.handoff_outcome == "missing_publication"
        assert delegate.calls == []

    asyncio.run(run())
