from __future__ import annotations

from app.services.canonical_publication_delivery_authority import (
    canonical_publication_delivery_primary_started,
)


def canonical_publication_delivery_nonrepeat_plain_started() -> bool:
    """Return whether the successfully started canonical primary owns plain non-repeat."""

    return canonical_publication_delivery_primary_started()


def canonical_publication_delivery_nonrepeat_pin_started() -> bool:
    """Return whether exact non-repeat pin can use the started canonical primary.

    Pin execution is an intrinsic durable post-action capability of the primary runtime.
    This profile fact is resolved directly from successful primary startup, not inferred
    from plain or any forward/delete sibling fact.
    """

    return canonical_publication_delivery_primary_started()
