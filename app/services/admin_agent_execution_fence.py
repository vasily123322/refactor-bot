from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.content.models import ContentRevision


async def fence_execution_claim(
    session: AsyncSession,
    *,
    owner_model: Any,
    owner_id: int,
    claim_token: str,
    content_item_id: int | None = None,
    content_revision: int | None = None,
) -> bool:
    """Fence one durable execution claim and, optionally, one canonical target.

    The guarded owner UPDATE is intentionally left uncommitted. When a content
    target is supplied, the same transaction also performs a no-op UPDATE on the
    immutable ContentRevision row identified by (content_item_id, revision).
    That existing durable row is the shared cross-mode authority: competing
    single/series executors serialize there before inspecting or creating a
    canonical scheduling outcome.

    Callers must commit or roll back the transaction after the protected
    canonical side effect/finalization. No schema-specific lock table is needed.
    """
    has_item = content_item_id is not None
    has_revision = content_revision is not None
    if has_item != has_revision:
        raise ValueError(
            "content_item_id and content_revision must be supplied together"
        )

    result = await session.execute(
        update(owner_model)
        .where(
            owner_model.id == int(owner_id),
            owner_model.state == "executing",
            owner_model.execution_claim_token == str(claim_token),
        )
        .values(execution_claim_token=str(claim_token))
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        return False

    if not has_item:
        return True

    target = await session.execute(
        update(ContentRevision)
        .where(
            ContentRevision.content_item_id == int(content_item_id),
            ContentRevision.revision == int(content_revision),
        )
        .values(revision=int(content_revision))
        .execution_options(synchronize_session=False)
    )
    return int(target.rowcount or 0) == 1
