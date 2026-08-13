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
    The profile still has its own fact and depends on the plain primary fact rather than
    being inferred from any future forward/delete sibling.
    """

    return canonical_publication_delivery_nonrepeat_plain_started()
