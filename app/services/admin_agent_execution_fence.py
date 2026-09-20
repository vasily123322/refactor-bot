from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.admin_agent import AdminAgentExecutionFence


_FENCE_PREFIX = "approval-execution:"


def approval_execution_fence_key(execution_key: str) -> str:
    key = str(execution_key or "").strip()
    if not key:
        raise ValueError("execution_key is required for approval execution fencing")
    return f"{_FENCE_PREFIX}{key}"


async def set_execution_fence(
    session: AsyncSession,
    *,
    fence_key: str,
    claim_token: str,
    previous_claim_token: str | None,
) -> bool:
    """Install or rotate one durable execution fence inside the caller transaction."""
    if previous_claim_token is not None:
        transition = await session.execute(
            update(AdminAgentExecutionFence)
            .where(
                AdminAgentExecutionFence.fence_key == str(fence_key),
                AdminAgentExecutionFence.claim_token == str(previous_claim_token),
            )
            .values(claim_token=str(claim_token))
        )
        if int(transition.rowcount or 0) == 1:
            return True

    current = (
        await session.execute(
            select(AdminAgentExecutionFence).where(
                AdminAgentExecutionFence.fence_key == str(fence_key)
            )
        )
    ).scalar_one_or_none()
    if current is None:
        session.add(
            AdminAgentExecutionFence(
                fence_key=str(fence_key),
                claim_token=str(claim_token),
            )
        )
        await session.flush()
        return True
    return str(current.claim_token) == str(claim_token)


async def hold_execution_fence(
    session: AsyncSession,
    *,
    fence_key: str,
    claim_token: str,
) -> bool:
    """Lock the durable fence until the caller commits its protected side effect."""
    transition = await session.execute(
        update(AdminAgentExecutionFence)
        .where(
            AdminAgentExecutionFence.fence_key == str(fence_key),
            AdminAgentExecutionFence.claim_token == str(claim_token),
        )
        .values(claim_token=str(claim_token))
    )
    return int(transition.rowcount or 0) == 1
