from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base
from app.domain.publication_autodelete import (
    PublicationAutodeleteAction,
    PublicationAutodeleteLease,
)
from app.services.publication_autodelete import _fingerprint
from app.services.publication_autodelete_action_ledger import (
    PublicationAutodeleteActionLedger,
    PublicationAutodeleteActionReservation,
)
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle


NOW = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64


def test_authority_fingerprint_is_stable_across_mapping_order() -> None:
    left = {
        "version": 1,
        "runtime": {"deleted": False, "effective_seconds": 3600},
        "runtime_options": {"autodelete_seconds": 3600, "autodelete_report": False},
        "telegram_message_ids": [11, 12],
    }
    right = {
        "telegram_message_ids": [11, 12],
        "runtime_options": {"autodelete_report": False, "autodelete_seconds": 3600},
        "runtime": {"effective_seconds": 3600, "deleted": False},
        "version": 1,
    }

    assert _fingerprint(left) == _fingerprint(right)
    assert _fingerprint(left) is not None


def test_stale_lease_token_cannot_finish_reserved_action(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'stale-lease-finish.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                session.add(
                    PublicationAutodeleteAction(
                        publication_id=1,
                        telegram_message_id=101,
                        telegram_chat_id=-1001,
                        authority_fingerprint=FINGERPRINT,
                        reservation_token="reservation-a",
                        reserved_by_lease_token="lease-a",
                        state="reserved",
                    )
                )
                await session.commit()

                stale = PublicationAutodeleteActionReservation(
                    publication_id=1,
                    telegram_message_id=101,
                    telegram_chat_id=-1001,
                    authority_fingerprint=FINGERPRINT,
                    reservation_token="reservation-a",
                    autodelete_lease_token="lease-b",
                )
                ledger = PublicationAutodeleteActionLedger(session)
                assert await ledger.mark_succeeded(stale, finished_at=NOW) is False

                action = (
                    await session.execute(select(PublicationAutodeleteAction))
                ).scalar_one()
                assert action.state == "reserved"

                exact = PublicationAutodeleteActionReservation(
                    publication_id=1,
                    telegram_message_id=101,
                    telegram_chat_id=-1001,
                    authority_fingerprint=FINGERPRINT,
                    reservation_token="reservation-a",
                    autodelete_lease_token="lease-a",
                )
                assert await ledger.mark_succeeded(exact, finished_at=NOW) is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_publication_lease_token_cannot_reserve_delete_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'stale-lease-reserve.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                session.add(
                    PublicationAutodeleteLease(
                        publication_id=1,
                        lease_token="lease-live",
                        holder="exactness-test",
                        expires_at=NOW + timedelta(minutes=1),
                    )
                )
                await session.commit()

                stale = PublicationAutodeleteLeaseHandle(
                    publication_id=1,
                    lease_token="lease-stale",
                    holder="exactness-test",
                    expires_at=NOW + timedelta(minutes=1),
                )
                result = await PublicationAutodeleteActionLedger(session).reserve(
                    stale,
                    telegram_chat_id=-1001,
                    telegram_message_id=101,
                    authority_fingerprint=FINGERPRINT,
                    now=NOW,
                )
                assert result.outcome == "ineligible"
                actions = (
                    await session.execute(select(PublicationAutodeleteAction))
                ).scalars().all()
                assert actions == []
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("state", ["reserved", "unknown"])
def test_existing_ambiguous_action_never_reauthorizes_delete(state, tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / f'ambiguous-{state}.db'}"
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as session:
                session.add_all(
                    [
                        PublicationAutodeleteLease(
                            publication_id=1,
                            lease_token="lease-current",
                            holder="exactness-test",
                            expires_at=NOW + timedelta(minutes=1),
                        ),
                        PublicationAutodeleteAction(
                            publication_id=1,
                            telegram_message_id=101,
                            telegram_chat_id=-1001,
                            authority_fingerprint=FINGERPRINT,
                            reservation_token="reservation-a",
                            reserved_by_lease_token="lease-old",
                            state=state,
                        ),
                    ]
                )
                await session.commit()

                current = PublicationAutodeleteLeaseHandle(
                    publication_id=1,
                    lease_token="lease-current",
                    holder="exactness-test",
                    expires_at=NOW + timedelta(minutes=1),
                )
                result = await PublicationAutodeleteActionLedger(session).reserve(
                    current,
                    telegram_chat_id=-1001,
                    telegram_message_id=102,
                    authority_fingerprint=FINGERPRINT,
                    now=NOW,
                )
                assert result.outcome == "ambiguous"

                actions = (
                    await session.execute(
                        select(PublicationAutodeleteAction).order_by(
                            PublicationAutodeleteAction.telegram_message_id
                        )
                    )
                ).scalars().all()
                assert [(action.telegram_message_id, action.state) for action in actions] == [
                    (101, state)
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())
