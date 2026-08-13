from __future__ import annotations

from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_primary_started,
    canonical_publication_delivery_time_autodelete_started,
    canonical_publication_delivery_views_autodelete_started,
)

# Profile-local admission bits are intentionally code-owned. A bit only means that this
# exact scheduler proof slice has been reviewed; the functions below additionally require
# the real successfully-started runtime dependency. Future bits stay False until their
# strict child retirement slice is opened.
_PLAIN = True
_PIN = True
_FORWARD = True
_PIN_FORWARD = True
_TIME = True
_TIME_PIN = True
_TIME_FORWARD = False
_TIME_PIN_FORWARD = False
_VIEWS = False
_VIEWS_PIN = False
_VIEWS_FORWARD = False
_VIEWS_PIN_FORWARD = False


def canonical_publication_delivery_nonrepeat_plain_started() -> bool:
    return bool(_PLAIN and canonical_publication_delivery_primary_started())


def canonical_publication_delivery_nonrepeat_pin_started() -> bool:
    # Pin action execution is intrinsic to the successfully started primary runtime.
    return bool(_PIN and canonical_publication_delivery_primary_started())


def canonical_publication_delivery_nonrepeat_forward_started() -> bool:
    # Forward action execution is intrinsic to the successfully started primary runtime.
    return bool(_FORWARD and canonical_publication_delivery_primary_started())


def canonical_publication_delivery_nonrepeat_pin_forward_started() -> bool:
    return bool(
        _PIN_FORWARD
        and canonical_publication_delivery_nonrepeat_pin_started()
        and canonical_publication_delivery_nonrepeat_forward_started()
    )


def canonical_publication_delivery_nonrepeat_time_started() -> bool:
    return bool(
        _TIME
        and canonical_publication_delivery_primary_started()
        and canonical_publication_delivery_time_autodelete_started()
    )


def canonical_publication_delivery_nonrepeat_time_pin_started() -> bool:
    return bool(
        _TIME_PIN
        and canonical_publication_delivery_nonrepeat_time_started()
        and canonical_publication_delivery_nonrepeat_pin_started()
    )


def canonical_publication_delivery_nonrepeat_time_forward_started() -> bool:
    return bool(
        _TIME_FORWARD
        and canonical_publication_delivery_nonrepeat_time_started()
        and canonical_publication_delivery_nonrepeat_forward_started()
    )


def canonical_publication_delivery_nonrepeat_time_pin_forward_started() -> bool:
    return bool(
        _TIME_PIN_FORWARD
        and canonical_publication_delivery_nonrepeat_time_pin_started()
        and canonical_publication_delivery_nonrepeat_time_forward_started()
        and canonical_publication_delivery_nonrepeat_pin_forward_started()
    )


def canonical_publication_delivery_nonrepeat_views_started() -> bool:
    return bool(
        _VIEWS
        and canonical_publication_delivery_primary_started()
        and canonical_publication_delivery_views_autodelete_started()
    )


def canonical_publication_delivery_nonrepeat_views_pin_started() -> bool:
    return bool(
        _VIEWS_PIN
        and canonical_publication_delivery_nonrepeat_views_started()
        and canonical_publication_delivery_nonrepeat_pin_started()
    )


def canonical_publication_delivery_nonrepeat_views_forward_started() -> bool:
    return bool(
        _VIEWS_FORWARD
        and canonical_publication_delivery_nonrepeat_views_started()
        and canonical_publication_delivery_nonrepeat_forward_started()
    )


def canonical_publication_delivery_nonrepeat_views_pin_forward_started() -> bool:
    return bool(
        _VIEWS_PIN_FORWARD
        and canonical_publication_delivery_nonrepeat_views_pin_started()
        and canonical_publication_delivery_nonrepeat_views_forward_started()
        and canonical_publication_delivery_nonrepeat_pin_forward_started()
    )
