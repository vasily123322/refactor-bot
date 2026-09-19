from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.schema import (
    DatabaseForeignKeyIntegrityError,
    DatabaseSchemaOutOfDate,
    DatabaseSchemaShapeError,
    bootstrap_database_schema,
    inspect_alembic_schema,
)


HEAD = "20260919_0021"


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
            assert state.expected_heads == (HEAD,)
            assert state.at_head is False
            assert calls == 1

            # The transitional unmanaged path keeps its historical semantics. FK
            # enforcement is adopted only after the DB is Alembic-managed and audited.
            async with engine.connect() as connection:
                enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert int(enabled.scalar_one() or 0) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_unmanaged_static_pool_reopens_database_after_legacy_file_rotation(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "legacy-static.db"
        backup_path = tmp_path / "legacy-static.backup.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE old_marker (id INTEGER PRIMARY KEY)")
            connection.commit()

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

        async def initialize() -> None:
            os.replace(database_path, backup_path)
            with sqlite3.connect(database_path) as connection:
                connection.execute("CREATE TABLE new_marker (id INTEGER PRIMARY KEY)")
                connection.commit()

        try:
            state = await bootstrap_database_schema(
                engine,
                unmanaged_initializer=initialize,
            )
            assert state.managed is False
            assert backup_path.exists()

            async with engine.connect() as connection:
                names = {
                    str(row[0])
                    for row in (
                        await connection.execute(
                            text("SELECT name FROM sqlite_master WHERE type='table'")
                        )
                    ).all()
                }
            assert "new_marker" in names
            assert "old_marker" not in names
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
            assert inspected.current_heads == (HEAD,)

            state = await bootstrap_database_schema(
                engine,
                unmanaged_initializer=initialize,
            )
            assert state.at_head is True
            assert calls == 0
            async with engine.connect() as connection:
                enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert int(enabled.scalar_one() or 0) == 1
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
                match=rf"current=20260809_0001, expected={HEAD}",
            ):
                await bootstrap_database_schema(
                    engine,
                    unmanaged_initializer=initialize,
                )
            assert calls == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_managed_static_pool_enforces_foreign_keys_on_reopened_connection(
    tmp_path,
) -> None:
    async def run() -> None:
        repo_root = Path(__file__).resolve().parents[1]
        database_path = tmp_path / "managed-static-fk.db"
        _upgrade(repo_root, database_path, "head")
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

        async def initialize() -> None:
            raise AssertionError("managed DB must not call legacy initializer")

        try:
            state = await bootstrap_database_schema(
                engine,
                unmanaged_initializer=initialize,
            )
            assert state.at_head is True

            async with engine.connect() as connection:
                enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert int(enabled.scalar_one() or 0) == 1

                with pytest.raises(IntegrityError):
                    await connection.exec_driver_sql(
                        """
                        INSERT INTO publication_delivery_leases
                            (publication_id, lease_token, holder, expires_at, created_at, updated_at)
                        VALUES
                            (999999, 'fk-enforced-test', 'test', CURRENT_TIMESTAMP,
                             CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """
                    )
                await connection.rollback()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_managed_sqlite_with_existing_foreign_key_violation_fails_closed(
    tmp_path,
) -> None:
    async def run() -> None:
        repo_root = Path(__file__).resolve().parents[1]
        database_path = tmp_path / "managed-dirty-fk.db"
        _upgrade(repo_root, database_path, "head")

        # sqlite3 starts with FK enforcement disabled, matching historical app
        # connections. Seed one orphan row to prove startup audits before enabling.
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO publication_delivery_leases
                    (publication_id, lease_token, holder, expires_at, created_at, updated_at)
                VALUES
                    (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (999999, "historical-orphan", "legacy"),
            )
            connection.commit()

        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        calls = 0

        async def initialize() -> None:
            nonlocal calls
            calls += 1

        try:
            with pytest.raises(
                DatabaseForeignKeyIntegrityError,
                match="contains foreign key violations",
            ):
                await bootstrap_database_schema(
                    engine,
                    unmanaged_initializer=initialize,
                )
            assert calls == 0

            async with engine.connect() as connection:
                enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert int(enabled.scalar_one() or 0) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_managed_database_at_head_with_missing_table_fails_shape_guard(tmp_path) -> None:
    async def run() -> None:
        repo_root = Path(__file__).resolve().parents[1]
        database_path = tmp_path / "managed-missing-table.db"
        _upgrade(repo_root, database_path, "head")
        with sqlite3.connect(database_path) as connection:
            connection.execute("DROP TABLE publication_delivery_leases")
            connection.commit()

        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        calls = 0

        async def initialize() -> None:
            nonlocal calls
            calls += 1

        try:
            inspected = await inspect_alembic_schema(engine)
            assert inspected.at_head is True
            with pytest.raises(
                DatabaseSchemaShapeError,
                match=r"missing tables=publication_delivery_leases",
            ):
                await bootstrap_database_schema(
                    engine,
                    unmanaged_initializer=initialize,
                )
            assert calls == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_managed_database_at_head_with_missing_column_fails_before_fk_enable(
    tmp_path,
) -> None:
    async def run() -> None:
        repo_root = Path(__file__).resolve().parents[1]
        database_path = tmp_path / "managed-missing-column.db"
        _upgrade(repo_root, database_path, "head")
        with sqlite3.connect(database_path) as connection:
            connection.execute("ALTER TABLE clients DROP COLUMN ui_settings")
            connection.commit()

        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        calls = 0

        async def initialize() -> None:
            nonlocal calls
            calls += 1

        try:
            inspected = await inspect_alembic_schema(engine)
            assert inspected.at_head is True
            with pytest.raises(
                DatabaseSchemaShapeError,
                match=r"missing columns=clients.ui_settings",
            ):
                await bootstrap_database_schema(
                    engine,
                    unmanaged_initializer=initialize,
                )
            assert calls == 0

            async with engine.connect() as connection:
                enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert int(enabled.scalar_one() or 0) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
