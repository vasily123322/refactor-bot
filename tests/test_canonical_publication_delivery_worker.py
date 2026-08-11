from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.canonical_publication_delivery_candidates import (
    CanonicalPublicationDeliveryCandidate,
    CanonicalPublicationDeliveryCandidateBatch,
    CanonicalPublicationDeliveryCandidateCursor,
)
from app.workers import canonical_publication_delivery as worker_module
from app.workers.canonical_publication_delivery import CanonicalPublicationDeliveryWorker


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _session_factory():
    return _SessionContext()


@dataclass(frozen=True)
class _Result:
    outcome: str


class _Executor:
    def __init__(self, outcomes: dict[int, str]) -> None:
        self.outcomes = dict(outcomes)
        self.calls: list[int] = []

    async def execute(self, publication_id: int) -> _Result:
        self.calls.append(int(publication_id))
        return _Result(self.outcomes[int(publication_id)])


def _candidate(
    publication_id: int,
    second: int,
) -> CanonicalPublicationDeliveryCandidate:
    return CanonicalPublicationDeliveryCandidate(
        publication_id=publication_id,
        scheduled_at=datetime(2026, 8, 11, 10, 0, second, tzinfo=timezone.utc),
    )


def _cursor(
    publication_id: int,
    second: int,
) -> CanonicalPublicationDeliveryCandidateCursor:
    return CanonicalPublicationDeliveryCandidateCursor(
        scheduled_at=datetime(2026, 8, 11, 10, 0, second, tzinfo=timezone.utc),
        publication_id=publication_id,
    )


def test_worker_advances_keyset_cursor_and_wraps_only_when_page_done(monkeypatch) -> None:
    async def run() -> None:
        seen_after: list[CanonicalPublicationDeliveryCandidateCursor | None] = []
        first_cursor = _cursor(11, 1)
        pages = [
            CanonicalPublicationDeliveryCandidateBatch(
                candidates=(_candidate(11, 1),),
                next_cursor=first_cursor,
                done=False,
            ),
            CanonicalPublicationDeliveryCandidateBatch(
                candidates=(_candidate(12, 2),),
                next_cursor=_cursor(12, 2),
                done=True,
            ),
            CanonicalPublicationDeliveryCandidateBatch(
                candidates=(),
                next_cursor=None,
                done=True,
            ),
        ]

        class FakeSelector:
            def __init__(self, session) -> None:
                pass

            async def scan_page(self, *, limit: int, scan_limit: int, after=None):
                assert limit == 7
                assert scan_limit == 41
                seen_after.append(after)
                return pages.pop(0)

        monkeypatch.setattr(
            worker_module,
            "CanonicalPublicationDeliveryCandidateSelector",
            FakeSelector,
        )
        executor = _Executor({11: "published", 12: "ineligible"})
        worker = CanonicalPublicationDeliveryWorker(
            executor=executor,
            session_factory=_session_factory,  # type: ignore[arg-type]
            batch_size=7,
            scan_limit=41,
        )

        first = await worker.run_once()
        second = await worker.run_once()
        third = await worker.run_once()

        assert seen_after == [None, first_cursor, None]
        assert executor.calls == [11, 12]
        assert first.published == 1 and first.cursor_reset is False
        assert second.ineligible == 1 and second.cursor_reset is True
        assert third.selected == 0 and third.cursor_reset is True

    asyncio.run(run())


def test_worker_counts_executor_outcomes_and_continues_after_candidate_error(
    monkeypatch,
) -> None:
    async def run() -> None:
        class FakeSelector:
            def __init__(self, session) -> None:
                pass

            async def scan_page(self, **kwargs):
                return CanonicalPublicationDeliveryCandidateBatch(
                    candidates=tuple(
                        _candidate(publication_id, publication_id)
                        for publication_id in range(1, 7)
                    ),
                    next_cursor=None,
                    done=True,
                )

        class MixedExecutor:
            async def execute(self, publication_id: int):
                if publication_id == 1:
                    return _Result("published")
                if publication_id == 2:
                    return _Result("failed")
                if publication_id == 3:
                    return _Result("ineligible")
                if publication_id == 4:
                    return _Result("lease_lost")
                if publication_id == 5:
                    return _Result("future-new-outcome")
                raise RuntimeError("provider secret must not be logged")

        monkeypatch.setattr(
            worker_module,
            "CanonicalPublicationDeliveryCandidateSelector",
            FakeSelector,
        )
        worker = CanonicalPublicationDeliveryWorker(
            executor=MixedExecutor(),
            session_factory=_session_factory,  # type: ignore[arg-type]
        )

        tick = await worker.run_once()

        assert tick.selected == 6
        assert tick.published == 1
        assert tick.failed == 1
        assert tick.ineligible == 1
        assert tick.lease_lost == 1
        assert tick.unexpected == 1
        assert tick.failures == 1
        assert tick.cursor_reset is True

    asyncio.run(run())


def test_worker_cancellation_keeps_previous_cursor_and_skips_later_candidates(
    monkeypatch,
) -> None:
    async def run() -> None:
        calls: list[int] = []
        previous_cursor = _cursor(20, 0)

        class FakeSelector:
            def __init__(self, session) -> None:
                pass

            async def scan_page(self, **kwargs):
                assert kwargs["after"] == previous_cursor
                return CanonicalPublicationDeliveryCandidateBatch(
                    candidates=(_candidate(21, 1), _candidate(22, 2)),
                    next_cursor=_cursor(22, 2),
                    done=False,
                )

        class CancellingExecutor:
            async def execute(self, publication_id: int):
                calls.append(publication_id)
                raise asyncio.CancelledError

        monkeypatch.setattr(
            worker_module,
            "CanonicalPublicationDeliveryCandidateSelector",
            FakeSelector,
        )
        worker = CanonicalPublicationDeliveryWorker(
            executor=CancellingExecutor(),
            session_factory=_session_factory,  # type: ignore[arg-type]
        )
        worker._cursor = previous_cursor

        try:
            await worker.run_once()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("delivery worker must propagate cancellation")

        assert calls == [21]
        assert worker._cursor == previous_cursor

    asyncio.run(run())


def test_worker_bounds_polling_batch_and_scan_configuration() -> None:
    worker = CanonicalPublicationDeliveryWorker(
        executor=_Executor({}),
        session_factory=_session_factory,  # type: ignore[arg-type]
        interval_seconds=0,
        batch_size=9999,
        scan_limit=9999,
    )
    assert worker.interval_seconds == 1
    assert worker.batch_size == 500
    assert worker.scan_limit == 500
