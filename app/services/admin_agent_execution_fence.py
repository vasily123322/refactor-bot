from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession


async def fence_execution_claim(
    session: AsyncSession,
    *,
    owner_model: Any,
    owner_id: int,
    claim_token: str,
) -> bool:
    """Fence one durable execution claim at a transaction commit boundary.

    The guarded UPDATE is intentionally left uncommitted. On success it holds
    the database write/row lock until the caller commits or rolls back, so a
    claim takeover cannot interleave between this ownership check and the
    canonical side effect or finalization protected by the same transaction.
    """
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
    return int(result.rowcount or 0) == 1
