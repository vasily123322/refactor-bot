from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app.services.posting import PostingService


@dataclass(frozen=True, slots=True)
class ManualPublishResult:
    sent: bool
    primary_message_ids: tuple[int, ...] = ()
    forwarded_targets: tuple[int, ...] = ()
    repeat_post_task_id: int | None = None


class ManualPublishService:
    """Execute one manual provider send and optionally bridge into one repeat root.

    The immediate provider send has no canonical source Publication today. Therefore
    this service may create exactly one future repeat root *after* a proven provider
    success. That root is created through ``PostingService.schedule()``, so supported
    work is atomically linked before its first committed executable state and all later
    successors remain canonical-continuation owned.

    Provider failure/ambiguity is fail-closed for repeat creation: no future root is
    scheduled unless ``send_now`` returns concrete Telegram message ids.
    """

    def __init__(self, bot, session_factory) -> None:
        self.bot = bot
        self.session_factory = session_factory

    @staticmethod
    def _targets(primary: int, forward_to: Iterable[int] | None) -> tuple[int, ...]:
        ordered: list[int] = []
        for raw in (primary, *(forward_to or ())):
            try:
                target = int(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if target <= 0 or target in ordered:
                continue
            ordered.append(target)
        return tuple(ordered)

    async def publish(
        self,
        *,
        channel_id: int,
        payload: dict,
        forward_to: Iterable[int] | None = None,
        notify_on: bool = True,
        repeat_on: bool = False,
        repeat_seconds: int = 0,
        repeat_allowed: bool = False,
        now: datetime | None = None,
    ) -> ManualPublishResult:
        primary = int(channel_id)
        if primary <= 0 or not isinstance(payload, dict) or not payload:
            return ManualPublishResult(sent=False)

        posting = PostingService(self.bot, self.session_factory)
        outbound = dict(payload)
        outbound["silent"] = not bool(notify_on)

        primary_ids = await posting.send_now(primary, outbound)
        if not primary_ids:
            return ManualPublishResult(sent=False)

        forwarded: list[int] = []
        targets = self._targets(primary, forward_to)
        for target in targets[1:]:
            ids = await posting.send_now(target, outbound)
            if ids:
                forwarded.append(target)

        repeat_task_id: int | None = None
        try:
            seconds = int(repeat_seconds or 0)
        except (TypeError, ValueError, OverflowError):
            seconds = 0
        if repeat_allowed and repeat_on and seconds > 0:
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            repeat_payload = dict(outbound)
            repeat_payload["repeat_on"] = True
            repeat_payload["repeat_seconds"] = seconds
            repeat_payload.pop("autodelete_at", None)
            task = await posting.schedule(
                primary,
                repeat_payload,
                current + timedelta(seconds=seconds),
            )
            repeat_task_id = int(task.id)

        return ManualPublishResult(
            sent=True,
            primary_message_ids=tuple(int(value) for value in primary_ids),
            forwarded_targets=tuple(forwarded),
            repeat_post_task_id=repeat_task_id,
        )
