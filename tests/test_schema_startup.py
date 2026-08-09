from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.schema import (
    DatabaseSchemaOutOfDate,
    bootstrap_database_schema,
    inspect_alembic_schema,
)


def _upgrade(repo_root: Path, database_path: Path, target: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "BOT_TOKEN": env.get("BOT_TOKEN", "123456:test-token-placeholder"),
            "API_ID": env.get("API_ID", "123456"),
            "API_HASH": env.get("API_HASH", "0123456789abcdef0123456789abcdef"),
            "DB_URL": f"sqlite+aiosqlite:///{database_path}",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", target],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unmanaged_database_uses_legacy_initializer(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "unmanaged.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        calls = 0

        async def initialize() -> None:
            nonlocal calls
            calls += 1

        try:
            state = await bootstrap_database_schema(
                engine,
                unmanaged_initializer=initialize,
            )
            assert state.managed is False
            assert state.current_heads == ()
            assert state.expected_heads == ("20260809_0002",)
            assert state.at_head is False
            assert calls == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_managed_database_at_head_skips_runtime_schema_initializer(tmp_path) -> None:
    async def run() -> None:
        repo_root = Path(__file__).resolve().parents[1]
        database_path = tmp_path / "managed-head.db"
        _upgrade(repo_root, database_path, "head")
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        calls = 0

        async def initialize() -> None:
            nonlocal calls
            calls += 1

        try:
            inspected = await inspect_alembic_schema(engine)
            assert inspected.managed is True
            assert inspected.at_head is True
            assert inspected.current_heads == ("20260809_0002",)

            state = await bootstrap_database_schema(
                engine,
                unmanaged_initializer=initialize,
            )
            assert state.at_head is True
            assert calls == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_managed_database_behind_head_fails_closed_before_initializer(tmp_path) -> None:
    async def run() -> None:
        repo_root = Path(__file__).resolve().parents[1]
        database_path = tmp_path / "managed-behind.db"
        _upgrade(repo_root, database_path, "20260809_0001")
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        calls = 0

        async def initialize() -> None:
            nonlocal calls
            calls += 1

        try:
            with pytest.raises(
                DatabaseSchemaOutOfDate,
                match=r"current=20260809_0001, expected=20260809_0002",
            ):
                await bootstrap_database_schema(
                    engine,
                    unmanaged_initializer=initialize,
                )
            assert calls == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
