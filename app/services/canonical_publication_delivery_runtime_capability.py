from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel


_MAX_FORWARD_TARGETS = 100
_ALLOWED_RUNTIME_KEYS = frozenset({"silent", "pin_on", "forward_to"})


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryRuntimeCapability:
    silent: bool | None = None
    pin_on: bool = False
    forward_to: tuple[int, ...] = ()

    @property
    def forward_silent(self) -> bool:
        return self.silent is True


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryForwardTarget:
    channel_id: int
    telegram_chat_id: int


def parse_canonical_publication_delivery_runtime_capability(
    options: dict[str, Any],
) -> CanonicalPublicationDeliveryRuntimeCapability | None:
    if not isinstance(options, dict):
        return None
    if not set(options).issubset(_ALLOWED_RUNTIME_KEYS):
        return None

    silent: bool | None = None
    if "silent" in options:
        if type(options.get("silent")) is not bool:
            return None
        silent = bool(options["silent"])

    pin_on = False
    if "pin_on" in options:
        if type(options.get("pin_on")) is not bool:
            return None
        pin_on = bool(options["pin_on"])

    forward_to: tuple[int, ...] = ()
    if "forward_to" in options:
        raw_targets = options.get("forward_to")
        if not isinstance(raw_targets, list):
            return None
        if len(raw_targets) > _MAX_FORWARD_TARGETS:
            return None
        parsed: list[int] = []
        seen: set[int] = set()
        for raw in raw_targets:
            if isinstance(raw, bool):
                return None
            try:
                channel_id = int(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if channel_id <= 0 or channel_id in seen:
                return None
            seen.add(channel_id)
            parsed.append(channel_id)
        forward_to = tuple(parsed)

    return CanonicalPublicationDeliveryRuntimeCapability(
        silent=silent,
        pin_on=pin_on,
        forward_to=forward_to,
    )


async def resolve_canonical_publication_delivery_forward_targets(
    session: AsyncSession,
    capability: CanonicalPublicationDeliveryRuntimeCapability,
    *,
    lock: bool = False,
) -> tuple[CanonicalPublicationDeliveryForwardTarget, ...] | None:
    if not capability.forward_to:
        return ()

    statement = select(Channel).where(Channel.id.in_(list(capability.forward_to)))
    if lock:
        statement = statement.with_for_update()
    rows = (await session.execute(statement)).scalars().all()
    by_id = {int(channel.id): channel for channel in rows}
    if len(by_id) != len(capability.forward_to):
        return None

    targets: list[CanonicalPublicationDeliveryForwardTarget] = []
    telegram_ids: set[int] = set()
    for channel_id in capability.forward_to:
        channel = by_id.get(int(channel_id))
        if channel is None:
            return None
        try:
            telegram_chat_id = int(channel.tg_chat_id)
        except (TypeError, ValueError, OverflowError):
            return None
        if telegram_chat_id == 0 or telegram_chat_id in telegram_ids:
            return None
        telegram_ids.add(telegram_chat_id)
        targets.append(
            CanonicalPublicationDeliveryForwardTarget(
                channel_id=int(channel.id),
                telegram_chat_id=telegram_chat_id,
            )
        )
    return tuple(targets)
