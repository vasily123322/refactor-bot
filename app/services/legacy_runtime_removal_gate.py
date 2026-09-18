from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class LegacyRuntimeRemovalState:
    active_post_tasks: int
    active_linked_publications: int
    active_intentional_legacy: int
    scheduler_leases: int

    @property
    def drained(self) -> bool:
        return (
            self.active_post_tasks == 0
            and self.active_linked_publications == 0
            and self.active_intentional_legacy == 0
            and self.scheduler_leases == 0
        )


class LegacyRuntimeNotDrainedError(RuntimeError):
    pass


async def legacy_runtime_removal_state(
    session: AsyncSession,
) -> LegacyRuntimeRemovalState:
    active_post_tasks = int(
        (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM post_tasks "
                    "WHERE status IN ('pending', 'processing')"
                )
            )
        ).scalar_one()
        or 0
    )
    active_linked_publications = int(
        (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM publications "
                    "WHERE legacy_post_task_id IS NOT NULL "
                    "AND status IN ('queued', 'sending')"
                )
            )
        ).scalar_one()
        or 0
    )
    active_intentional_legacy = int(
        (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM publications "
                    "WHERE execution_mode = 'intentional_legacy' "
                    "AND status IN ('queued', 'sending')"
                )
            )
        ).scalar_one()
        or 0
    )
    scheduler_leases = int(
        (
            await session.execute(
                text("SELECT COUNT(*) FROM scheduler_task_leases")
            )
        ).scalar_one()
        or 0
    )
    return LegacyRuntimeRemovalState(
        active_post_tasks=active_post_tasks,
        active_linked_publications=active_linked_publications,
        active_intentional_legacy=active_intentional_legacy,
        scheduler_leases=scheduler_leases,
    )


async def assert_legacy_runtime_drained(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        state = await legacy_runtime_removal_state(session)
    if state.drained:
        return
    raise LegacyRuntimeNotDrainedError(
        "legacy PostTask runtime is not drained: "
        f"active_post_tasks={state.active_post_tasks} "
        f"active_linked_publications={state.active_linked_publications} "
        f"active_intentional_legacy={state.active_intentional_legacy} "
        f"scheduler_leases={state.scheduler_leases}"
    )
