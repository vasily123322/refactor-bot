from __future__ import annotations

import weakref


_primary_worker_ref: weakref.ReferenceType[object] | None = None
_time_autodelete_available = False
_views_autodelete_available = False
_repeat_continuation_available = False
_repeat_owner_policy_enforced = False
_repeat_time_available = False
_repeat_time_pin_available = False
_repeat_time_forward_available = False
_repeat_time_pin_forward_available = False
_repeat_views_available = False


def canonical_publication_delivery_primary_started() -> bool:
    """Return whether the successfully started canonical primary is still referenced."""

    return _primary_worker_ref is not None and _primary_worker_ref() is not None


def canonical_publication_delivery_time_autodelete_started() -> bool:
    """Return the started primary fact for canonical non-repeat time autodelete."""

    return canonical_publication_delivery_primary_started() and _time_autodelete_available


def canonical_publication_delivery_views_autodelete_started() -> bool:
    """Return the started primary fact for canonical non-repeat views autodelete."""

    return canonical_publication_delivery_primary_started() and _views_autodelete_available


def canonical_publication_delivery_repeat_started() -> bool:
    """Return the live repeat fact only from started continuation and owner policy."""

    return (
        canonical_publication_delivery_primary_started()
        and _repeat_continuation_available
        and _repeat_owner_policy_enforced
    )


def canonical_publication_delivery_repeat_time_started() -> bool:
    """Return the live repeat+time fact only from all started destructive dependencies."""

    return (
        canonical_publication_delivery_repeat_started()
        and canonical_publication_delivery_time_autodelete_started()
        and _repeat_time_available
    )


def canonical_publication_delivery_repeat_time_pin_started() -> bool:
    """Return the dedicated live repeat+time+pin composition fact."""

    return (
        canonical_publication_delivery_repeat_time_started()
        and _repeat_time_pin_available
    )


def canonical_publication_delivery_repeat_time_forward_started() -> bool:
    """Return the dedicated live repeat+time+forward composition fact."""

    return (
        canonical_publication_delivery_repeat_time_started()
        and _repeat_time_forward_available
    )


def canonical_publication_delivery_repeat_time_pin_forward_started() -> bool:
    """Return the dedicated live combined repeat+time+pin+forward composition fact."""

    return (
        canonical_publication_delivery_repeat_time_pin_started()
        and canonical_publication_delivery_repeat_time_forward_started()
        and _repeat_time_pin_forward_available
    )


def canonical_publication_delivery_repeat_views_started() -> bool:
    """Return the dedicated live plain repeat+views composition fact."""

    return (
        canonical_publication_delivery_repeat_started()
        and canonical_publication_delivery_views_autodelete_started()
        and _repeat_views_available
    )


def set_canonical_publication_delivery_primary_worker(
    worker: object | None,
    *,
    time_autodelete_available: bool = False,
    views_autodelete_available: bool = False,
    repeat_continuation_available: bool = False,
    repeat_owner_policy_enforced: bool = False,
    repeat_time_available: bool = False,
    repeat_time_pin_available: bool = False,
    repeat_time_forward_available: bool = False,
    repeat_time_pin_forward_available: bool = False,
    repeat_views_available: bool = False,
) -> None:
    """Publish primary authority only with capabilities proven at successful startup."""

    global _primary_worker_ref
    global _time_autodelete_available, _views_autodelete_available
    global _repeat_continuation_available, _repeat_owner_policy_enforced
    global _repeat_time_available, _repeat_time_pin_available
    global _repeat_time_forward_available, _repeat_time_pin_forward_available
    global _repeat_views_available

    _primary_worker_ref = None if worker is None else weakref.ref(worker)
    _time_autodelete_available = bool(
        worker is not None and time_autodelete_available
    )
    _views_autodelete_available = bool(
        worker is not None and views_autodelete_available
    )
    _repeat_continuation_available = bool(
        worker is not None and repeat_continuation_available
    )
    _repeat_owner_policy_enforced = bool(
        worker is not None and repeat_owner_policy_enforced
    )
    _repeat_time_available = bool(worker is not None and repeat_time_available)
    _repeat_time_pin_available = bool(worker is not None and repeat_time_pin_available)
    _repeat_time_forward_available = bool(
        worker is not None and repeat_time_forward_available
    )
    _repeat_time_pin_forward_available = bool(
        worker is not None and repeat_time_pin_forward_available
    )
    _repeat_views_available = bool(worker is not None and repeat_views_available)
