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
    holder = "atomic-handoff-unit"
    lease_seconds = 120
    allow_time_autodelete = False

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[int] = []
        self.claim_calls: list[object] = []

    async def execute(self, publication_id: int):
        self.events.append("delegate-id")
        self.calls.append(int(publication_id))
        return _DelegateResult("published")

    async def execute_claim(self, claim):
        self.events.append("delegate-claim")
        self.claim_calls.append(claim)
        return _DelegateResult("published")


def test_handoff_executor_delegates_canonical_only_without_atomic_transfer(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)

        class UnexpectedAtomicTransfer:
            def __init__(self, session) -> None:
                raise AssertionError("canonical-only delivery must not construct handoff")

        monkeypatch.setattr(
            module,
            "CanonicalPublicationAtomicHandoffClaimService",
            UnexpectedAtomicTransfer,
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
        assert delegate.claim_calls == []
        assert events == ["delegate-id"]

    asyncio.run(run())


def test_handoff_executor_executes_only_committed_atomic_claim(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)
        claim = object()
        captured: dict[str, object] = {}

        class SuccessfulAtomicTransfer:
            def __init__(self, session) -> None:
                pass

            async def claim_linked(self, publication_id: int, **kwargs):
                events.append("atomic-claim")
                captured.update(kwargs)
                return SimpleNamespace(outcome="claimed", claim=claim)

        monkeypatch.setattr(
            module,
            "CanonicalPublicationAtomicHandoffClaimService",
            SuccessfulAtomicTransfer,
        )
        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(
                SimpleNamespace(legacy_post_task_id=9001)
            ),  # type: ignore[arg-type]
        )

        result = await wrapper.execute(42)

        assert result.outcome == "published"
        assert delegate.calls == []
        assert delegate.claim_calls == [claim]
        assert events == ["atomic-claim", "delegate-claim"]
        assert captured == {
            "holder": "atomic-handoff-unit",
            "ttl_seconds": 120,
            "allow_time_autodelete": False,
        }

    asyncio.run(run())


def test_handoff_executor_blocks_delegate_on_nonclaimed_atomic_transfer(monkeypatch) -> None:
    async def run() -> None:
        for transfer_outcome in ("contention", "conflict", "ineligible"):
            events: list[str] = []
            delegate = _Delegate(events)

            class BlockedAtomicTransfer:
                def __init__(self, session) -> None:
                    pass

                async def claim_linked(self, publication_id: int, **kwargs):
                    events.append("atomic-claim")
                    return SimpleNamespace(outcome=transfer_outcome, claim=None)

            monkeypatch.setattr(
                module,
                "CanonicalPublicationAtomicHandoffClaimService",
                BlockedAtomicTransfer,
            )
            wrapper = CanonicalPublicationDeliveryHandoffExecutor(
                executor=delegate,
                session_factory=_session_factory(
                    SimpleNamespace(legacy_post_task_id=9002)
                ),  # type: ignore[arg-type]
            )

            result = await wrapper.execute(43)

            assert result.outcome == "ineligible"
            assert result.handoff_outcome == transfer_outcome
            assert delegate.calls == []
            assert delegate.claim_calls == []
            assert events == ["atomic-claim"]

    asyncio.run(run())


def test_handoff_executor_claim_unavailable_is_lease_lost_not_retryable(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)

        class UnavailableAtomicTransfer:
            def __init__(self, session) -> None:
                pass

            async def claim_linked(self, publication_id: int, **kwargs):
                events.append("atomic-claim")
                return SimpleNamespace(outcome="claim_unavailable", claim=None)

        monkeypatch.setattr(
            module,
            "CanonicalPublicationAtomicHandoffClaimService",
            UnavailableAtomicTransfer,
        )
        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(
                SimpleNamespace(legacy_post_task_id=9003)
            ),  # type: ignore[arg-type]
        )

        result = await wrapper.execute(44)
        assert result.outcome == "lease_lost"
        assert result.handoff_outcome == "claim_unavailable"
        assert delegate.calls == []
        assert delegate.claim_calls == []
        assert events == ["atomic-claim"]

    asyncio.run(run())


def test_handoff_executor_propagates_atomic_transfer_cancellation(monkeypatch) -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)

        class CancelledAtomicTransfer:
            def __init__(self, session) -> None:
                pass

            async def claim_linked(self, publication_id: int, **kwargs):
                events.append("atomic-claim")
                raise asyncio.CancelledError

        monkeypatch.setattr(
            module,
            "CanonicalPublicationAtomicHandoffClaimService",
            CancelledAtomicTransfer,
        )
        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(
                SimpleNamespace(legacy_post_task_id=9004)
            ),  # type: ignore[arg-type]
        )

        try:
            await wrapper.execute(45)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("atomic handoff cancellation must propagate")

        assert delegate.calls == []
        assert delegate.claim_calls == []
        assert events == ["atomic-claim"]

    asyncio.run(run())


def test_handoff_executor_missing_publication_is_ineligible() -> None:
    async def run() -> None:
        events: list[str] = []
        delegate = _Delegate(events)
        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(None),  # type: ignore[arg-type]
        )

        result = await wrapper.execute(46)

        assert result.outcome == "ineligible"
        assert result.handoff_outcome == "missing_publication"
        assert delegate.calls == []
        assert delegate.claim_calls == []

    asyncio.run(run())
