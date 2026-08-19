from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.publishing.models import PostingDedupeLock


async def acquire_posting_dedupe_lock(
    session: AsyncSession,
    dedupe_key: str,
) -> None:
    """Serialize one posting dedupe identity inside the caller transaction.

    The stable row is only a database mutex. It intentionally outlives any particular
    PostTask or Publication so future reuse can lock the same identity and then decide
    ownership from the current owner stores. The caller must re-check both stores after
    this function returns and hold the surrounding transaction/savepoint until the owner
    creation is durable.
    """

    key = str(dedupe_key)
    existing = (
        await session.execute(
            select(PostingDedupeLock)
            .where(PostingDedupeLock.dedupe_key == key)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if existing is None:
        try:
            # Two first users of a key may both observe no stable mutex row. Isolate the
            # insert race so the loser can continue in the caller transaction after the
            # winner commits its row.
            async with session.begin_nested():
                session.add(PostingDedupeLock(dedupe_key=key))
                await session.flush()
        except IntegrityError:
            pass

    # A no-op UPDATE provides a real write lock even on backends where SELECT FOR UPDATE
    # is ignored (notably SQLite test databases). On PostgreSQL it also keeps the mutex
    # row locked until the caller transaction ends.
    locked = await session.execute(
        update(PostingDedupeLock)
        .where(PostingDedupeLock.dedupe_key == key)
        .values(dedupe_key=key)
        .execution_options(synchronize_session=False)
    )
    if int(locked.rowcount or 0) != 1:
        raise RuntimeError("posting_dedupe_lock_unavailable")
