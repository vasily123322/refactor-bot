from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Channel


_MAX_FORWARD_TARGETS = 100
_ALLOWED_RUNTIME_KEYS = frozenset(
    {
        "silent",
        "pin_on",
        "forward_to",
        "autodelete_seconds",
        "autodelete_effective_seconds",
        "autodelete_views",
        "autodelete_report",
    }
)
_NEUTRAL_NUMBER_VALUES = (None, False, 0, "0", "")


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryRuntimeCapability:
    silent: bool | None = None
    pin_on: bool = False
    forward_to: tuple[int, ...] = ()
    time_autodelete_seconds: int | None = None
    views_autodelete_threshold: int | None = None
    autodelete_report: bool = False

    @property
    def forward_silent(self) -> bool:
        return self.silent is True

    @property
    def time_autodelete_requested(self) -> bool:
        return self.time_autodelete_seconds is not None

    @property
    def views_autodelete_requested(self) -> bool:
        return self.views_autodelete_threshold is not None


@dataclass(frozen=True, slots=True)
class CanonicalPublicationDeliveryForwardTarget:
    channel_id: int
    telegram_chat_id: int


def _strict_optional_positive_int(value: Any) -> tuple[bool, int | None]:
    if value in _NEUTRAL_NUMBER_VALUES:
        return True, None
    if isinstance(value, bool):
        return False, None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return False, None
    if parsed <= 0:
        return False, None
    return True, parsed


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

    views_ok, views = _strict_optional_positive_int(options.get("autodelete_views"))
    if not views_ok:
        return None

    report = options.get("autodelete_report", False)
    if type(report) is not bool:
        return None

    effective_ok, effective_seconds = _strict_optional_positive_int(
        options.get("autodelete_effective_seconds")
    )
    base_ok, base_seconds = _strict_optional_positive_int(
        options.get("autodelete_seconds")
    )
    if not effective_ok or not base_ok:
        return None
    time_autodelete_seconds = effective_seconds or base_seconds
    if time_autodelete_seconds is not None and views is not None:
        # Historical runtime treats time- and views-based deletion as separate modes.
        # Refuse ambiguous dual authority instead of letting one mechanism shadow the
        # other after canonical cutover.
        return None
    if report and time_autodelete_seconds is None and views is None:
        # A deletion report has no independent execution meaning. Reject it instead of
        # silently dropping requested semantics when no delete trigger exists.
        return None

    return CanonicalPublicationDeliveryRuntimeCapability(
        silent=silent,
        pin_on=pin_on,
        forward_to=forward_to,
        time_autodelete_seconds=time_autodelete_seconds,
        views_autodelete_threshold=views,
        autodelete_report=report,
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
