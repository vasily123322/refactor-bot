from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import app.domain  # noqa: F401 register complete ORM metadata
from app.core import schema as schema_module
from app.core.db import Base
from app.core.schema import (
    DatabaseForeignKeyIntegrityError,
    DatabaseLegacyAdoptionError,
    DatabaseSchemaMigrationError,
    DatabaseSchemaOutOfDate,
    DatabaseSchemaShapeError,
    DatabaseSchemaUnmanaged,
    adopt_legacy_database_schema,
    bootstrap_database_schema,
    inspect_alembic_schema,
)


HEAD = "20260920_0024"


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


def _table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not str(row[0]).startswith("sqlite_")
        }


def _create_current_unmanaged_database(database_path: Path) -> None:
    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(sync_engine)
    finally:
        sync_engine.dispose()
    assert "alembic_version" not in _table_names(database_path)


def test_fresh_database_startup_migrates_to_alembic_head(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "fresh.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            state = await bootstrap_database_schema(engine)
            assert state.managed is True
            assert state.at_head is True
            assert state.current_heads == (HEAD,)
            assert "alembic_version" in state.table_names
            assert set(Base.metadata.tables) <= set(state.table_names)

            async with engine.connect() as connection:
                enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert int(enabled.scalar_one() or 0) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fresh_database_migration_failure_is_not_swallowed(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        database_path = tmp_path / "fresh-failure.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

        def fail_upgrade(*args, **kwargs) -> None:
            raise RuntimeError("migration exploded")

        monkeypatch.setattr(schema_module.command, "upgrade", fail_upgrade)
        try:
            with pytest.raises(
                DatabaseSchemaMigrationError,
                match="no legacy create_all/ad-hoc fallback was attempted",
            ) as exc_info:
                await bootstrap_database_schema(engine)
            assert isinstance(exc_info.value.__cause__, RuntimeError)
            assert "alembic_version" not in _table_names(database_path)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_nonempty_unmanaged_database_fails_closed_with_adoption_instructions(
    tmp_path,
) -> None:
    async def run() -> None:
        database_path = tmp_path / "unsafe-unmanaged.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE old_marker (id INTEGER PRIMARY KEY)")
            connection.commit()

        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            with pytest.raises(
                DatabaseSchemaUnmanaged,
                match=r"python scripts/adopt_legacy_database.py",
            ):
                await bootstrap_database_schema(engine)
            assert _table_names(database_path) == {"old_marker"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_current_orm_legacy_database_has_explicit_safe_adoption_path(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "legacy-current-orm.db"
        _create_current_unmanaged_database(database_path)
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

        try:
            with pytest.raises(DatabaseSchemaUnmanaged):
                await bootstrap_database_schema(engine)

            adopted = await adopt_legacy_database_schema(engine)
            assert adopted.managed is True
            assert adopted.at_head is True
            assert adopted.current_heads == (HEAD,)

            inspected = await inspect_alembic_schema(engine)
            assert inspected.at_head is True

            restarted = await bootstrap_database_schema(engine)
            assert restarted.at_head is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_adoption_rejects_schema_drift_without_stamping(tmp_path) -> None:
    async def run() -> None:
        database_path = tmp_path / "legacy-drift.db"
        _create_current_unmanaged_database(database_path)
        with sqlite3.connect(database_path) as connection:
            connection.execute("DROP TABLE publication_delivery_leases")
            connection.commit()

        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            with pytest.raises(
                DatabaseLegacyAdoptionError,
                match="does not exactly match current ORM metadata",
            ):
                await adopt_legacy_database_schema(engine)
            assert "alembic_version" not in _table_names(database_path)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_managed_database_at_head_skips_schema_mutation_and_enforces_fk(tmp_path) -> None:
    async def run() -> None:
        repo_root = Path(__file__).resolve().parents[1]
        database_path = tmp_path / "managed-head.db"
        _upgrade(repo_root, database_path, "head")
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

        try:
            before = _table_names(database_path)
            state = await bootstrap_database_schema(engine)
            assert state.at_head is True
            assert state.current_heads == (HEAD,)
            assert _table_names(database_path) == before

            async with engine.connect() as connection:
                enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert int(enabled.scalar_one() or 0) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_managed_database_behind_head_fails_closed(tmp_path) -> None:
    async def run() -> None:
        repo_root = Path(__file__).resolve().parents[1]
        database_path = tmp_path / "managed-behind.db"
        _upgrade(repo_root, database_path, "20260809_0001")
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

        try:
            with pytest.raises(
                DatabaseSchemaOutOfDate,
                match=rf"current=20260809_0001, expected={HEAD}",
            ):
                await bootstrap_database_schema(engine)
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

        try:
            state = await bootstrap_database_schema(engine)
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
        try:
            with pytest.raises(
                DatabaseForeignKeyIntegrityError,
                match="contains foreign key violations",
            ):
                await bootstrap_database_schema(engine)

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
        try:
            inspected = await inspect_alembic_schema(engine)
            assert inspected.at_head is True
            with pytest.raises(
                DatabaseSchemaShapeError,
                match=r"missing tables=publication_delivery_leases",
            ):
                await bootstrap_database_schema(engine)
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
        try:
            inspected = await inspect_alembic_schema(engine)
            assert inspected.at_head is True
            with pytest.raises(
                DatabaseSchemaShapeError,
                match=r"missing columns=clients.ui_settings",
            ):
                await bootstrap_database_schema(engine)

            async with engine.connect() as connection:
                enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
                assert int(enabled.scalar_one() or 0) == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
