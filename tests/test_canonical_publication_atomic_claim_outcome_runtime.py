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
    holder = "outcome-runtime"
    lease_seconds = 120
    allow_time_autodelete = False
    allow_views_autodelete = False

    def __init__(self) -> None:
        self.id_calls = 0
        self.claim_calls = 0

    async def execute(self, publication_id: int):
        self.id_calls += 1
        return _DelegateResult("published")

    async def execute_claim(self, claim):
        self.claim_calls += 1
        return _DelegateResult("published")


def test_exact_atomic_rollback_maps_to_ineligible_claim_rejected(monkeypatch) -> None:
    async def run() -> None:
        delegate = _Delegate()
        events: list[str] = []

        class Transfer:
            def __init__(self, session) -> None:
                pass

            async def claim_linked(self, publication_id: int, **kwargs):
                events.append("transfer")
                return SimpleNamespace(
                    outcome="claim_unavailable",
                    claim=None,
                    legacy_post_task_id=9101,
                )

        class Classifier:
            def __init__(self, session) -> None:
                pass

            async def classify(self, *, publication_id: int, legacy_post_task_id: int):
                events.append("classify")
                assert publication_id == 51
                assert legacy_post_task_id == 9101
                return SimpleNamespace(outcome="claim_rejected")

        monkeypatch.setattr(module, "CanonicalPublicationAtomicHandoffClaimService", Transfer)
        monkeypatch.setattr(module, "CanonicalPublicationAtomicClaimFailureClassifier", Classifier)

        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(
                SimpleNamespace(legacy_post_task_id=9101, meta={})
            ),
        )
        result = await wrapper.execute(51)

        assert result.outcome == "ineligible"
        assert result.handoff_outcome == "claim_rejected"
        assert delegate.id_calls == 0
        assert delegate.claim_calls == 0
        assert events == ["transfer", "classify"]

    asyncio.run(run())


def test_partial_or_committed_atomic_state_maps_to_lease_lost(monkeypatch) -> None:
    async def run() -> None:
        delegate = _Delegate()
        events: list[str] = []

        class Transfer:
            def __init__(self, session) -> None:
                pass

            async def claim_linked(self, publication_id: int, **kwargs):
                events.append("transfer")
                return SimpleNamespace(
                    outcome="claim_unavailable",
                    claim=None,
                    legacy_post_task_id=9102,
                )

        class Classifier:
            def __init__(self, session) -> None:
                pass

            async def classify(self, *, publication_id: int, legacy_post_task_id: int):
                events.append("classify")
                assert publication_id == 52
                assert legacy_post_task_id == 9102
                return SimpleNamespace(outcome="claim_unavailable")

        monkeypatch.setattr(module, "CanonicalPublicationAtomicHandoffClaimService", Transfer)
        monkeypatch.setattr(module, "CanonicalPublicationAtomicClaimFailureClassifier", Classifier)

        wrapper = CanonicalPublicationDeliveryHandoffExecutor(
            executor=delegate,
            session_factory=_session_factory(
                SimpleNamespace(legacy_post_task_id=9102, meta={})
            ),
        )
        result = await wrapper.execute(52)

        assert result.outcome == "lease_lost"
        assert result.handoff_outcome == "claim_unavailable"
        assert delegate.id_calls == 0
        assert delegate.claim_calls == 0
        assert events == ["transfer", "classify"]

    asyncio.run(run())
