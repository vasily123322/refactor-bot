from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.admin_remove_allrepeat as remove_module
from app.services.admin_remove_allrepeat import AdminRemoveAllRepeatService
from app.services.canonical_scheduler_admission import (
    CanonicalSchedulerAdmission,
    CanonicalSchedulerAdmissionKind,
)


class _Scalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _Result:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _Scalars(self._values)


class _Session:
    def __init__(self, tasks):
        self.tasks = list(tasks)
        self.commits = 0

    async def execute(self, _statement):
        return _Result(self.tasks)

    async def commit(self):
        self.commits += 1


async def _execute(monkeypatch, *, tasks, admissions):
    class _AdmissionService:
        def __init__(self, _session):
            pass

        async def classify(self, *, task_id):
            return admissions[int(task_id)]

    monkeypatch.setattr(
        remove_module,
        "CanonicalSchedulerAdmissionService",
        _AdmissionService,
    )
    session = _Session(tasks)
    result = await AdminRemoveAllRepeatService(session).execute()
    return result, session


def _task(task_id: int):
    return SimpleNamespace(
        id=task_id,
        status="pending",
        payload={
            "repeat_on": True,
            "repeat_group_id": 77,
            "repeat_seconds": 3600,
            "autodelete_seconds": 60,
            "autodelete_views": 10,
        },
    )


@pytest.mark.asyncio
async def test_linked_supported_started_row_is_not_legacy_mutated(monkeypatch):
    task = _task(1)
    before_payload = dict(task.payload)
    admission = CanonicalSchedulerAdmission(
        CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED,
        publication_id=101,
        profile="plain",
        repeat=True,
    )

    result, session = await _execute(
        monkeypatch,
        tasks=[task],
        admissions={1: admission},
    )

    assert task.status == "pending"
    assert task.payload == before_payload
    assert task.payload["repeat_on"] is True
    assert task.payload["repeat_seconds"] == 3600
    assert task.payload["autodelete_seconds"] == 60
    assert task.payload["autodelete_views"] == 10
    assert result.removed_pending == 0
    assert result.disabled_flags == 0
    assert result.cleared_autodelete == 0
    assert result.protected_canonical == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_ambiguous_or_drifted_link_fails_closed_without_mutation(monkeypatch):
    task = _task(2)
    before_payload = dict(task.payload)
    admission = CanonicalSchedulerAdmission(
        CanonicalSchedulerAdmissionKind.FAIL_CLOSED,
        publication_id=202,
    )

    result, _session = await _execute(
        monkeypatch,
        tasks=[task],
        admissions={2: admission},
    )

    assert task.status == "pending"
    assert task.payload == before_payload
    assert result.protected_canonical == 1


@pytest.mark.asyncio
async def test_unlinked_legacy_row_keeps_existing_bulk_cleanup_semantics(monkeypatch):
    task = _task(3)
    admission = CanonicalSchedulerAdmission(
        CanonicalSchedulerAdmissionKind.LEGACY_UNLINKED
    )

    result, session = await _execute(
        monkeypatch,
        tasks=[task],
        admissions={3: admission},
    )

    assert task.status == "skipped"
    assert task.payload["repeat_on"] is False
    assert "repeat_seconds" not in task.payload
    assert task.payload["repeat_group_id"] == 77
    assert "autodelete_seconds" not in task.payload
    assert "autodelete_views" not in task.payload
    assert task.payload["autodeleted"] is True
    assert task.payload["autodeleted_at"]
    assert result.removed_pending == 1
    assert result.disabled_flags == 1
    assert result.cleared_autodelete == 1
    assert result.protected_canonical == 0
    assert session.commits == 1


@pytest.mark.asyncio
async def test_time_views_fallback_stays_legacy_without_opening_canonical_row(monkeypatch):
    legacy_fallback = _task(4)
    canonical = _task(5)
    canonical_before = dict(canonical.payload)
    admissions = {
        4: CanonicalSchedulerAdmission(
            CanonicalSchedulerAdmissionKind.LEGACY_TIME_VIEWS,
            publication_id=404,
            profile="time",
            repeat=True,
        ),
        5: CanonicalSchedulerAdmission(
            CanonicalSchedulerAdmissionKind.CANONICAL_PROOF_REQUIRED,
            publication_id=505,
            profile="time",
            repeat=True,
        ),
    }

    result, _session = await _execute(
        monkeypatch,
        tasks=[legacy_fallback, canonical],
        admissions=admissions,
    )

    assert legacy_fallback.status == "skipped"
    assert legacy_fallback.payload["repeat_on"] is False
    assert "autodelete_seconds" not in legacy_fallback.payload
    assert canonical.status == "pending"
    assert canonical.payload == canonical_before
    assert result.removed_pending == 1
    assert result.disabled_flags == 1
    assert result.cleared_autodelete == 1
    assert result.protected_canonical == 1


def test_guarded_admin_router_precedes_legacy_main_router():
    import app.bot.routers as routers

    guarded_index = routers.main_router.sub_routers.index(
        routers.admin_remove_allrepeat_commands
    )
    legacy_index = routers.main_router.sub_routers.index(routers.main_commands)

    assert guarded_index < legacy_index
