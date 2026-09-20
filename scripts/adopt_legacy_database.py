from __future__ import annotations

import asyncio

from app.core.db import engine
from app.core.schema import adopt_legacy_database_schema


async def _run() -> None:
    try:
        state = await adopt_legacy_database_schema(engine)
        print(
            "Legacy database adopted into Alembic authority at head(s): "
            + ",".join(state.current_heads)
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
