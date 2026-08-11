from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.canonical_publication_delivery_live_auxiliary_hook import (
    CanonicalPublicationDeliveryLiveAuxiliaryHook,
)


class _AuxExecutor:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def execute(self, plan):
        if plan.admin_log is not None:
            self.events.append("admin")
        if plan.owner_notice is not None:
            self.events.append("owner")
        return SimpleNamespace(
            admin_failed=0,
            owner_failed=0,
            invalid_plans=0,
        )


class _PostActionExecutor:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def execute(self, context):
        self.events.append("post-actions")
        if self.fail:
            raise RuntimeError("post-action-internal-secret")
        return SimpleNamespace(unknown=0, suppressed=0, conflicts=0)


class _Hook(CanonicalPublicationDeliveryLiveAuxiliaryHook):
    def __init__(self, *, events: list[str], post_fail: bool = False) -> None:
        super().__init__(
            executor=_AuxExecutor(events),
            post_action_executor=_PostActionExecutor(events, fail=post_fail),
            session_factory=object(),  # type: ignore[arg-type]
        )
        self._passes = 0

    async def _plan(self, context):
        self._passes += 1
        if self._passes == 1:
            return SimpleNamespace(
                publication_id=601,
                admin_log="admin-plan",
                owner_notice="stale-owner-must-not-be-used",
            )
        if self._passes == 2:
            return SimpleNamespace(
                publication_id=601,
                admin_log="stale-admin-must-not-be-used",
                owner_notice="fresh-owner-plan",
            )
        raise AssertionError("hook must perform exactly two auxiliary authorization passes")


def test_live_hook_orders_admin_then_pin_forward_then_fresh_owner() -> None:
    async def run() -> None:
        events: list[str] = []
        hook = _Hook(events=events)
        await hook.execute(SimpleNamespace(publication_id=601))  # type: ignore[arg-type]
        assert events == ["admin", "post-actions", "owner"]
        assert hook._passes == 2

    asyncio.run(run())


def test_generic_post_action_coordinator_failure_still_reauthorizes_owner() -> None:
    async def run() -> None:
        events: list[str] = []
        hook = _Hook(events=events, post_fail=True)
        await hook.execute(SimpleNamespace(publication_id=601))  # type: ignore[arg-type]
        assert events == ["admin", "post-actions", "owner"]
        assert hook._passes == 2

    asyncio.run(run())
