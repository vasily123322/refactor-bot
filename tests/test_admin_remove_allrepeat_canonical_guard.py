from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.admin_remove_allrepeat import AdminRemoveAllRepeatService


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.commits = 0

    async def execute(self, _statement):
        return _Result(self.rows)

    async def commit(self):
        self.commits += 1


def _schedule(*, status="pending", repeat=True, meta=None):
    return SimpleNamespace(
        id=1,
        status=status,
        repeat_rule={"enabled": True, "seconds": 3600} if repeat else {},
        meta=dict(meta or {}),
    )


def _publication(*, status="queued", meta=None):
    return SimpleNamespace(
        id=2,
        status=status,
        last_error="old",
        meta=dict(meta or {}),
    )


@pytest.mark.asyncio
async def test_pending_canonical_repeat_is_cancelled_without_posttask_mutation():
    schedule = _schedule(
        meta={
            "repeat_group_id": 10,
            "runtime_options": {
                "autodelete_seconds": 60,
                "autodelete_views": 10,
                "pin_on": True,
            },
        }
    )
    publication = _publication(meta=dict(schedule.meta))

    session = _Session([(schedule, publication)])
    result = await AdminRemoveAllRepeatService(session).execute()

    assert schedule.status == "cancelled"
    assert publication.status == "cancelled"
    assert publication.last_error is None
    assert schedule.repeat_rule == {"enabled": False}
    assert schedule.meta["runtime_options"] == {"pin_on": True}
    assert publication.meta["runtime_options"] == {"pin_on": True}
    assert result.removed_pending == 1
    assert result.disabled_flags == 1
    assert result.cleared_autodelete == 1
    assert result.protected_canonical == 0
    assert session.commits == 1


@pytest.mark.asyncio
async def test_nonmutable_canonical_repeat_is_preserved():
    schedule = _schedule(status="completed", meta={"repeat_group_id": 10})
    publication = _publication(status="published", meta={"repeat_group_id": 10})
    before_rule = dict(schedule.repeat_rule)
    before_meta = dict(schedule.meta)

    result = await AdminRemoveAllRepeatService(_Session([(schedule, publication)])).execute()

    assert schedule.status == "completed"
    assert publication.status == "published"
    assert schedule.repeat_rule == before_rule
    assert schedule.meta == before_meta
    assert result.removed_pending == 0
    assert result.disabled_flags == 0
    assert result.cleared_autodelete == 0
    assert result.protected_canonical == 1


def test_service_has_no_posttask_or_scheduler_admission_dependency():
    from pathlib import Path

    source = Path("app/services/admin_remove_allrepeat.py").read_text(encoding="utf-8")
    assert "PostTask" not in source
    assert "canonical_scheduler_admission" not in source


def test_guarded_admin_router_precedes_legacy_main_router():
    import app.bot.routers as routers

    guarded_index = routers.main_router.sub_routers.index(
        routers.admin_remove_allrepeat_commands
    )
    legacy_index = routers.main_router.sub_routers.index(routers.main_commands)

    assert guarded_index < legacy_index
