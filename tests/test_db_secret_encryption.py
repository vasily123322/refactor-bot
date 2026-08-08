from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.secrets import (
    SecretDecryptionError,
    SecretKeyNotConfigured,
    decrypt_secret,
    encrypt_secret,
    is_encrypted_secret,
)
from app.domain.models import ExternalBot
from app.repositories.external_bots import ExternalBotsRepo


_TEST_KEY = "test-database-secret-key-0123456789abcdef"


def test_secret_roundtrip_and_ciphertext_does_not_contain_plaintext(monkeypatch):
    monkeypatch.setenv("DB_SECRET_KEY", _TEST_KEY)
    plaintext = "123456:telegram-secret-token"

    encrypted = encrypt_secret(plaintext)

    assert encrypted.startswith("enc:v1:")
    assert plaintext not in encrypted
    assert is_encrypted_secret(encrypted)
    assert decrypt_secret(encrypted) == plaintext


def test_legacy_plaintext_remains_readable_without_key(monkeypatch):
    monkeypatch.delenv("DB_SECRET_KEY", raising=False)
    assert decrypt_secret("legacy-plaintext-token") == "legacy-plaintext-token"


def test_new_secret_write_requires_database_key(monkeypatch):
    monkeypatch.delenv("DB_SECRET_KEY", raising=False)
    with pytest.raises(SecretKeyNotConfigured):
        encrypt_secret("new-token")


def test_encrypted_secret_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("DB_SECRET_KEY", _TEST_KEY)
    encrypted = encrypt_secret("sensitive-token")
    monkeypatch.setenv("DB_SECRET_KEY", "different-secret-key-0123456789abcdef")

    with pytest.raises(SecretDecryptionError):
        decrypt_secret(encrypted)


async def _raw_token(session, external_bot_id: int) -> str:
    result = await session.execute(
        text("select token from external_bots where id = :id"),
        {"id": external_bot_id},
    )
    return str(result.scalar_one())


def test_repository_stores_ciphertext_and_returns_plaintext(monkeypatch):
    monkeypatch.setenv("DB_SECRET_KEY", _TEST_KEY)

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(ExternalBot.__table__.create)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                repo = ExternalBotsRepo(session)
                created = await repo.create_or_update(
                    "123456:telegram-secret-token",
                    owner_client_id=None,
                    bot_user_id=123456,
                    bot_username="external_test_bot",
                )
                stored = await _raw_token(session, int(created.id))
                assert stored.startswith("enc:v1:")
                assert "telegram-secret-token" not in stored

                runtime = await repo.get_by_id(int(created.id))
                assert runtime is not None
                assert runtime.token == "123456:telegram-secret-token"

                # An unrelated commit must not write the exposed plaintext token back.
                runtime.is_active = False
                await session.commit()
                assert (await _raw_token(session, int(created.id))).startswith("enc:v1:")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_repository_lazily_migrates_legacy_plaintext(monkeypatch):
    monkeypatch.setenv("DB_SECRET_KEY", _TEST_KEY)

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(ExternalBot.__table__.create)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as session:
                await session.execute(
                    text(
                        "insert into external_bots "
                        "(token, bot_user_id, bot_username, owner_client_id, is_active) "
                        "values (:token, :uid, :username, null, 1)"
                    ),
                    {
                        "token": "legacy-telegram-token",
                        "uid": 777,
                        "username": "legacy_bot",
                    },
                )
                await session.commit()

                repo = ExternalBotsRepo(session)
                runtime = await repo.get_by_id(1)
                assert runtime is not None
                assert runtime.token == "legacy-telegram-token"

                stored = await _raw_token(session, 1)
                assert stored.startswith("enc:v1:")
                assert "legacy-telegram-token" not in stored
        finally:
            await engine.dispose()

    asyncio.run(run())
