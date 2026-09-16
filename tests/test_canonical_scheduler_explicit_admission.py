from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

import app.workers.canonical_repeat_continuation_scheduler as scheduler_module
from app.domain.models import PostTask
from app.domain.publishing.models import ScheduleEntry
from app.services.canonical_scheduler_admission import (
    CanonicalSchedulerAdmission,
    CanonicalSchedulerAdmissionKind,
    CanonicalSchedulerAdmissionService,
)
from app.services.publication_execution_mode import execution_mode_from_legacy_payload
from app.workers.canonical_recovery_scheduler import Scheduler as RecoveryScheduler


class _Scalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)


class _Session:
    def __init__(self, *, publications=(), task=None, schedule=None, scalar_values=()):
        self.publications = list(publications)
        self.task = task
        self.schedule = schedule
        self.scalar_values = list(scalar_values)
        self.rollbacks = 0

    async def execute(self, _statement):
        return _Result(self.publications)

    async def get(self, model, _identity, **_kwargs):
        if model is PostTask:
            return self.task
        if model is ScheduleEntry:
            return self.schedule
        return None

    async def scalar(self, _statement):
        if self.scalar_values:
            return self.scalar_values.pop(0)
        return None

    async def rollback(self):
        self.rollbacks += 1


def _linked(*, options=None, repeat_rule=None, schedule_entry_id=20):
    options = {} if options is None else dict(options)
    publication = SimpleNamespace(
        id=10,
        status="queued",
        attempt_count=0,
        legacy_post_task_id=1,
        telegram_message_ids=None,
        result_link=None,
        last_error=None,
        schedule_entry_id=schedule_entry_id,
        channel_id=2,
        content_item_id=3,
        content_revision=4,
        execution_mode=execution_mode_from_legacy_payload(options),
        meta={"runtime_options": options},
    )
    task = SimpleNamespace(id=1, status="pending")
    schedule = SimpleNamespace(
        id=20,
        status="pending",
        channel_id=2,
        content_item_id=3,
        content_revision=4,
        repeat_rule={} if repeat_rule is None else dict(repeat_rule),
        meta={"runtime_options": options},
    )
    return publication, task, schedule


async def _classify(
    _monkeypatch,
    *,
    options=None,
    repeat_rule=None,
    started=False,
    scalar_values=(),
):
    publication, task, schedule = _linked(options=options, repeat_rule=repeat_rule)
    session = _Session(
        publications=[publication],
        task=task,
        schedule=schedule,
        scalar_values=scalar_values,
    )
    return await CanonicalSchedulerAdmissionService(session).classify(task_id=1)


@pytest.mark.asyncio
async def test_unlinked_legacy_occurrence_is_explicitly_allowed():
    result = await CanonicalSchedulerAdmissionService(_Session()).classify(task_id=1)
    assert result.kind is CanonicalSchedulerAdmissionKind.LEGACY_UNLINKED
    assert result.legacy_allowed is True


@pytest.mark.asyncio
async def test_time_views_is_intentional_legacy_fallback(monkeypatch):
    result = await _classify(
        monkeypatch,
        options={"autodelete_seconds": 60, "autodelete_views": 100},
        started=True,
    )
    assert result.kind is CanonicalSchedulerAdmissionKind.LEGACY_TIME_VIEWS
    assert result.legacy_allowed is True


@pytest.mark.asyncio
async def test_report_is_exact_legacy_fallback(monkeypatch):
    result = await _classify(
        monkeypatch,
        options={"autodelete_seconds": 60, "autodelete_report": True},
        started=True,
    )
    assert result.kind is CanonicalSchedulerAdmissionKind.LEGACY_REPORT
    assert result.profile == "time"


@pytest.mark.asyncio
async def test_supported_profile_not_started_still_requires_canonical_proof(monkeypatch):
    result = await _classify(monkeypatch, options={}, started=False)
    assert result.kind is CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED
    assert result.profile == "plain"
    assert result.legacy_allowed is False


@pytest.mark.asyncio
async def test_supported_started_profile_requires_canonical_proof(monkeypatch):
    result = await _classify(monkeypatch, options={}, started=True)
    assert result.kind is CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED
    assert result.repeat is False


@pytest.mark.asyncio
async def test_supported_started_parity_drift_does_not_fall_back_to_legacy(monkeypatch):
    admission = CanonicalSchedulerAdmission(
        CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED,
        publication_id=10,
        profile="plain",
        repeat=False,
    )
    parent_items = []

    class _AdmissionService:
        def __init__(self, _session):
            pass

        async def classify(self, *, task_id):
            assert task_id == 1
            return admission

    async def _parent_mark(_self, _session, items):
        parent_items.extend(items)

    async def _proof_false(_self, _session, *, task_id):
        assert task_id == 1
        return False

    monkeypatch.setattr(scheduler_module, "CanonicalSchedulerAdmissionService", _AdmissionService)
    monkeypatch.setattr(RecoveryScheduler, "_mark_processing", _parent_mark)
    scheduler = object.__new__(scheduler_module.Scheduler)
    scheduler._yield_proven_nonrepeat_to_canonical_primary = MethodType(_proof_false, scheduler)
    items = [SimpleNamespace(id=1)]
    await scheduler._mark_processing(_Session(), items)
    assert items == []
    assert parent_items == []


@pytest.mark.asyncio
async def test_malformed_linked_identity_fails_closed(monkeypatch):
    publication, task, _schedule = _linked(schedule_entry_id=None)
    result = await CanonicalSchedulerAdmissionService(
        _Session(publications=[publication], task=task)
    ).classify(task_id=1)
    assert result.kind is CanonicalSchedulerAdmissionKind.FAIL_CLOSED


@pytest.mark.asyncio
async def test_conflicting_canonical_lease_fails_closed(monkeypatch):
    result = await _classify(
        monkeypatch,
        options={},
        started=True,
        scalar_values=[None, 99],
    )
    assert result.kind is CanonicalSchedulerAdmissionKind.FAIL_CLOSED


@pytest.mark.asyncio
async def test_proof_exception_fails_closed_instead_of_legacy(monkeypatch):
    admission = CanonicalSchedulerAdmission(
        CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED,
        publication_id=10,
        profile="plain",
        repeat=False,
    )
    parent_items = []

    class _AdmissionService:
        def __init__(self, _session):
            pass

        async def classify(self, *, task_id):
            return admission

    async def _parent_mark(_self, _session, items):
        parent_items.extend(items)

    async def _proof_error(_self, _session, *, task_id):
        raise RuntimeError("proof failed")

    monkeypatch.setattr(scheduler_module, "CanonicalSchedulerAdmissionService", _AdmissionService)
    monkeypatch.setattr(RecoveryScheduler, "_mark_processing", _parent_mark)
    scheduler = object.__new__(scheduler_module.Scheduler)
    scheduler._yield_proven_nonrepeat_to_canonical_primary = MethodType(_proof_error, scheduler)
    items = [SimpleNamespace(id=1)]
    session = _Session()
    await scheduler._mark_processing(session, items)
    assert items == []
    assert parent_items == []
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_repeat_profile_ownership_is_independent_of_started_readiness(monkeypatch):
    result = await _classify(
        monkeypatch,
        options={"pin_on": True},
        repeat_rule={"enabled": True, "seconds": 3600},
        started=False,
    )
    assert result.kind is CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED
    assert result.profile == "pin"
    assert result.repeat is True


@pytest.mark.asyncio
async def test_time_views_never_becomes_canonical_capability(monkeypatch):
    result = await _classify(
        monkeypatch,
        options={
            "pin_on": True,
            "autodelete_seconds": 60,
            "autodelete_views": 100,
        },
        repeat_rule={"enabled": True, "seconds": 3600},
        started=True,
    )
    assert result.kind is CanonicalSchedulerAdmissionKind.LEGACY_TIME_VIEWS
    assert result.kind is not CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED


@pytest.mark.asyncio
async def test_historical_unlinked_claim_reaches_legacy_parent(monkeypatch):
    admission = CanonicalSchedulerAdmission(
        CanonicalSchedulerAdmissionKind.LEGACY_UNLINKED
    )
    parent_items = []

    class _AdmissionService:
        def __init__(self, _session):
            pass

        async def classify(self, *, task_id):
            return admission

    async def _parent_mark(_self, _session, items):
        parent_items.extend(items)

    monkeypatch.setattr(scheduler_module, "CanonicalSchedulerAdmissionService", _AdmissionService)
    monkeypatch.setattr(RecoveryScheduler, "_mark_processing", _parent_mark)
    scheduler = object.__new__(scheduler_module.Scheduler)
    post = SimpleNamespace(id=1, status="pending")
    session = _Session(task=post)
    items = [post]
    await scheduler._mark_processing(session, items)
    assert parent_items == [post]
