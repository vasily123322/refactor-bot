from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content import PostDocument
from app.domain.models import PostTask
from app.domain.publishing.models import Publication
from app.services.canonical_publication_delivery_planner import (
    CanonicalPublicationDeliveryPlan,
)
from app.services.scheduling import as_utc
from app.services.telegram_renderer import TelegramRenderError, TelegramRenderer


CUTOVER_META_KEY = "legacy_transport_cutover"

_IDENTITY_MARKERS = {
    "_publication_id",
    "_content_item_id",
    "_content_revision",
    "_content_channel_id",
}
_FORBIDDEN_EPHEMERAL_KEYS = {
    "_via_scheduler",
    "_post_task_id",
    "_also_schedule_autodelete",
}
_NEUTRAL_BOOL_KEYS = {
    "pin_on",
    "forward_silent",
    "autodelete_report",
}
_NEUTRAL_INT_KEYS = {
    "repeat_seconds",
    "autodelete_seconds",
    "autodelete_effective_seconds",
    "autodelete_views",
}
_NEUTRAL_OPTIONAL_KEYS = {
    "autodelete_at",
    "autodeleted_at",
    "result_link",
}


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): deepcopy(item) for key, item in value.items()}


def _neutral_bool(value: Any) -> bool:
    return value is None or value is False or value == 0


def _neutral_int(value: Any) -> bool:
    return value in (None, False, 0, "0", "")


def _neutral_payload_effects(payload: Mapping[str, Any]) -> bool:
    if payload.get("repeat_on") not in (None, False, 0):
        return False
    if payload.get("repeat_group_id") is not None:
        return False
    if payload.get("forward_to") not in (None, []):
        return False
    if payload.get("result_ids") not in (None, []):
        return False
    if payload.get("autodeleted") not in (None, False):
        return False
    for key in _NEUTRAL_BOOL_KEYS:
        if key in payload and not _neutral_bool(payload.get(key)):
            return False
    for key in _NEUTRAL_INT_KEYS:
        if key in payload and not _neutral_int(payload.get(key)):
            return False
    for key in _NEUTRAL_OPTIONAL_KEYS:
        if payload.get(key) not in (None, ""):
            return False
    return True


def _strip_neutral_effect_fields(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not _neutral_payload_effects(payload):
        return None
    cleaned = deepcopy(payload)
    for key in (
        "repeat_on",
        "repeat_seconds",
        "repeat_group_id",
        "silent",
        "pin_on",
        "forward_to",
        "forward_silent",
        "autodelete_seconds",
        "autodelete_effective_seconds",
        "autodelete_views",
        "autodelete_report",
        "autodelete_at",
        "autodeleted",
        "autodeleted_at",
        "result_ids",
        "result_link",
    ):
        cleaned.pop(key, None)
    return cleaned


def _nonrepeat(plan: CanonicalPublicationDeliveryPlan) -> bool:
    try:
        rule = plan.repeat_rule()
    except (TypeError, ValueError):
        return False
    enabled = rule.get("enabled")
    return enabled is None or enabled is False


def _supported_runtime_options(
    plan: CanonicalPublicationDeliveryPlan,
) -> dict[str, Any] | None:
    try:
        options = plan.runtime_options()
    except (TypeError, ValueError):
        return None
    if not options:
        return {}
    if not set(options).issubset({"silent", "pin_on"}):
        return None
    for key in ("silent", "pin_on"):
        if key in options and type(options.get(key)) is not bool:
            return None
    return options


def _expected_transport_payload(
    plan: CanonicalPublicationDeliveryPlan,
) -> dict[str, Any] | None:
    try:
        document = plan.post_document()
    except (TypeError, ValueError):
        return None

    if document.mode == "rich":
        return {
            "type": "rich_document",
            "post_document": document.to_dict(),
        }

    try:
        rendered = TelegramRenderer().render(document)
    except (TelegramRenderError, TypeError, ValueError):
        return None
    if rendered.kind != "classic" or rendered.classic_payload is None:
        return None
    return deepcopy(dict(rendered.classic_payload))


def _identity_markers_match(
    payload: dict[str, Any],
    *,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
) -> bool:
    expected = {
        "_publication_id": int(publication.id),
        "_content_item_id": int(plan.content_item_id),
        "_content_revision": int(plan.content_revision),
        "_content_channel_id": int(plan.channel_id),
    }
    for key, expected_value in expected.items():
        if key not in payload:
            continue
        raw = payload.get(key)
        if isinstance(raw, bool):
            return False
        try:
            parsed = int(raw)
        except (TypeError, ValueError, OverflowError):
            return False
        if parsed != expected_value:
            return False
    return True


def _transport_effective_silent(payload: Mapping[str, Any]) -> bool | None:
    """Return the silence legacy transport would actually use for this payload."""

    if str(payload.get("type") or "") == "rich_document":
        raw_document = payload.get("post_document")
        if not isinstance(raw_document, Mapping):
            return None
        try:
            document = PostDocument.from_dict(raw_document)
            rendered = TelegramRenderer().render(document)
        except (TelegramRenderError, TypeError, ValueError):
            return None
        if rendered.kind != "rich":
            return None
        return bool(rendered.disable_notification)

    if "silent" in payload and type(payload.get("silent")) is not bool:
        return None
    return bool(payload.get("silent", False))


def _silent_intent_matches(
    current: Mapping[str, Any],
    expected: Mapping[str, Any],
    runtime_options: Mapping[str, Any],
) -> bool:
    current_effective = _transport_effective_silent(current)
    expected_effective = _transport_effective_silent(expected)
    if current_effective is None or expected_effective is None:
        return False

    if "silent" in runtime_options:
        intended = runtime_options.get("silent")
        if type(intended) is not bool:
            return False
        if type(current.get("silent")) is not bool or current.get("silent") is not intended:
            return False
        return current_effective is intended

    for payload in (current, expected):
        if "silent" in payload and not _neutral_bool(payload.get("silent")):
            return False
    return current_effective is expected_effective


def _pin_intent_matches(
    current: Mapping[str, Any],
    runtime_options: Mapping[str, Any],
) -> bool:
    if "pin_on" in runtime_options:
        intended = runtime_options.get("pin_on")
        if type(intended) is not bool:
            return False
        return type(current.get("pin_on")) is bool and current.get("pin_on") is intended

    if "pin_on" in current and not _neutral_bool(current.get("pin_on")):
        return False
    return True


def _legacy_intent_matches(
    *,
    task: PostTask,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
) -> bool:
    if int(task.channel_id) != int(plan.channel_id):
        return False
    if task.scheduled_at is None or as_utc(task.scheduled_at) != as_utc(plan.scheduled_at):
        return False
    if task.error not in (None, ""):
        return False

    runtime_options = _supported_runtime_options(plan)
    current = _mapping(task.payload)
    expected = _expected_transport_payload(plan)
    if runtime_options is None or current is None or expected is None:
        return False
    if any(key in current for key in _FORBIDDEN_EPHEMERAL_KEYS):
        return False
    if not _identity_markers_match(current, publication=publication, plan=plan):
        return False
    if not _silent_intent_matches(current, expected, runtime_options):
        return False
    if not _pin_intent_matches(current, runtime_options):
        return False
    for key in _IDENTITY_MARKERS:
        current.pop(key, None)
    current.pop("pin_on", None)

    current_clean = _strip_neutral_effect_fields(current)
    expected_clean = _strip_neutral_effect_fields(expected)
    if current_clean is None or expected_clean is None:
        return False
    return current_clean == expected_clean


def _authority_intent_matches(
    *,
    task: PostTask,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
    allow_time_autodelete: bool,
    allow_views_autodelete: bool,
) -> bool:
    if _legacy_intent_matches(
        task=task,
        publication=publication,
        plan=plan,
    ):
        return True
    if not allow_time_autodelete and not allow_views_autodelete:
        return False

    from app.services.canonical_publication_delivery_atomic_handoff_claim import (
        _atomic_legacy_intent_matches,
        _atomic_runtime_profile,
    )

    profile = _atomic_runtime_profile(
        plan,
        allow_time_autodelete=allow_time_autodelete,
        allow_views_autodelete=allow_views_autodelete,
    )
    if profile is None:
        return False
    if profile.autodelete_report:
        if profile.timer_requested:
            if profile.views_requested or not allow_time_autodelete:
                return False
        elif profile.views_requested:
            if profile.timer_requested or not allow_views_autodelete:
                return False
        else:
            return False
    if profile.pin_on:
        if profile.timer_requested:
            if not allow_time_autodelete or profile.views_requested:
                return False
        elif profile.views_requested:
            if not allow_views_autodelete or profile.timer_requested:
                return False
        else:
            return False
    elif profile.timer_requested:
        if not allow_time_autodelete or profile.views_requested:
            return False
    elif profile.views_requested:
        if not allow_views_autodelete or profile.timer_requested:
            return False
    else:
        return False
    return _atomic_legacy_intent_matches(
        task=task,
        publication=publication,
        plan=plan,
        profile=profile,
    )


async def _forward_authority_intent_matches(
    session: AsyncSession,
    *,
    task: PostTask,
    publication: Publication,
    plan: CanonicalPublicationDeliveryPlan,
    allow_time_autodelete: bool,
    allow_views_autodelete: bool,
) -> bool:
    """Reuse exact forward parity with independently gated delete/report composition."""

    from app.services.canonical_publication_linked_forward_parity import (
        CanonicalPublicationLinkedForwardParityService,
    )

    parity = await CanonicalPublicationLinkedForwardParityService(session).prove(
        task=task,
        publication=publication,
        plan=plan,
    )
    if parity is None:
        return False
    if parity.autodelete_report:
        if parity.time_autodelete_requested:
            if parity.views_autodelete_requested or not allow_time_autodelete:
                return False
        elif parity.views_autodelete_requested:
            if parity.time_autodelete_requested or not allow_views_autodelete:
                return False
        else:
            return False
    if not parity.delete_requested:
        return not parity.autodelete_report
    if parity.pin_on:
        if parity.time_autodelete_requested:
            return bool(
                allow_time_autodelete and not parity.views_autodelete_requested
            )
        if parity.views_autodelete_requested:
            return bool(
                allow_views_autodelete and not parity.time_autodelete_requested
            )
        return False
    if parity.time_autodelete_requested:
        return bool(allow_time_autodelete and not parity.views_autodelete_requested)
    if parity.views_autodelete_requested:
        return bool(allow_views_autodelete and not parity.time_autodelete_requested)
    return False
