from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.schema import inspect_alembic_schema


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, str]

    def payload(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": dict(self.checks),
        }


class RuntimeReadiness:
    """Process readiness state plus live database/schema verification.

    The boot flag is set only after the supported runtime lifecycle has started.
    Database details and exception text are intentionally not exposed to callers.
    """

    def __init__(self) -> None:
        self._boot_ready = False

    @property
    def boot_ready(self) -> bool:
        return self._boot_ready

    def mark_ready(self) -> None:
        self._boot_ready = True

    def mark_not_ready(self) -> None:
        self._boot_ready = False

    async def check(self, engine: AsyncEngine) -> ReadinessResult:
        checks = {
            "runtime": "ok" if self._boot_ready else "not_ready",
            "database": "unavailable",
            "schema": "unknown",
        }
        try:
            schema_state = await inspect_alembic_schema(engine)
        except Exception:
            return ReadinessResult(ready=False, checks=checks)

        checks["database"] = "ok"
        checks["schema"] = "ok" if schema_state.at_head else "not_ready"
        return ReadinessResult(
            ready=bool(self._boot_ready and schema_state.at_head),
            checks=checks,
        )


runtime_readiness = RuntimeReadiness()
