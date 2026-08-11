from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services import canonical_publication_delivery_live_auxiliary_hook as module
from app.services.canonical_publication_delivery_live_auxiliary_executor import (
    CanonicalPublicationDeliveryLiveAuxiliaryExecution,
)
from app.services.canonical_publication_delivery_live_auxiliary_hook import (
    CanonicalPublicationDeliveryLiveAuxiliaryHook,
)
from app.services.canonical_publication_delivery_live_auxiliary_planner import (
    CanonicalPublicationDeliveryLiveAuxiliaryPlan,
    CanonicalPublicationLiveOwnerNoticePlan,
)


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _session_factory():
    return _SessionContext()


class _Plan:
    def __init__(self, repeat_rule: dict) -> None:
        self._repeat_rule = dict(repeat_rule)

    def repeat_rule(self) -> dict:
        return dict(self._repeat_rule)

    def runtime_options(self) -> dict:
        return {}


class _RecordingExecutor:
    def __init__(self) -> None:
        self.owner_calls = 0

    async def execute(self, plan):
        if plan.owner_notice is not None:
            self.owner_calls += 1
        return CanonicalPublicationDeliveryLiveAuxiliaryExecution(
            publication_id=int(plan.publication_id),
            owner_attempted=1 if plan.owner_notice is not None else 0,
            owner_sent=1 if plan.owner_notice is not None else 0,
        )


def _owner_plan(publication_id: int) -> CanonicalPublicationDeliveryLiveAuxiliaryPlan:
    return CanonicalPublicationDeliveryLiveAuxiliaryPlan(
        publication_id=publication_id,
        admin_log=None,
        owner_notice=CanonicalPublicationLiveOwnerNoticePlan(
            publication_id=publication_id,
            owner_tg_user_id=7001,
            owner_username=None,
            channel_title="Repeat policy proof",
            source_telegram_chat_id=-1007001,
            result_link=None,
            delivered_count=1,
            timezone_code="UTC",
            local_date_iso="2026-08-11",
            local_date_text="11.08.2026",
            local_time_text="12:00",
            callback_data=f"cp_open_pub:{publication_id}:2026-08-11",
        ),
    )


async def _execute_with_rule(monkeypatch, *, rule: dict, enforced: bool) -> int:
    publication_id = 71

    class FakePlanner:
        def __init__(self, session) -> None:
            pass

        async def plan(self, context):
            # Deliberately return an owner plan even for repeat/malformed rules. This
            # proves the final provider boundary is independently fail-closed and does
            # not rely on the older planner's permissive non-repeat helper.
            return _owner_plan(publication_id)

    monkeypatch.setattr(module, "CanonicalPublicationDeliveryLiveAuxiliaryPlanner", FakePlanner)
    executor = _RecordingExecutor()
    hook = CanonicalPublicationDeliveryLiveAuxiliaryHook(
        executor=executor,
        session_factory=_session_factory,  # type: ignore[arg-type]
        repeat_owner_policy_enforced=enforced,
    )
    context = SimpleNamespace(
        publication_id=publication_id,
        plan=_Plan(rule),
    )
    await hook.execute(context)  # type: ignore[arg-type]
    return executor.owner_calls


def test_repeat_owner_policy_suppresses_fixed_delay_repeat_at_provider_boundary(
    monkeypatch,
) -> None:
    assert asyncio.run(
        _execute_with_rule(
            monkeypatch,
            rule={"enabled": True, "seconds": 3600},
            enforced=True,
        )
    ) == 0


def test_repeat_owner_policy_suppresses_malformed_disabled_cadence_fail_closed(
    monkeypatch,
) -> None:
    # The historical planner helper treats enabled=False as non-repeat regardless of
    # cadence. The strict provider-boundary policy must still suppress this malformed
    # future/drifted shape rather than sending an owner notice.
    assert asyncio.run(
        _execute_with_rule(
            monkeypatch,
            rule={"enabled": False, "seconds": 3600},
            enforced=True,
        )
    ) == 0


def test_repeat_owner_policy_allows_exact_nonrepeat_and_is_default_off(monkeypatch) -> None:
    assert asyncio.run(
        _execute_with_rule(
            monkeypatch,
            rule={},
            enforced=True,
        )
    ) == 1
    assert asyncio.run(
        _execute_with_rule(
            monkeypatch,
            rule={"enabled": False, "seconds": 3600},
            enforced=False,
        )
    ) == 1
