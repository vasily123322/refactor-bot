from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base


PREVIOUS_HEAD = "20260919_0015"
HEAD = "20260919_0016"


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


def _tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not str(row[0]).startswith("sqlite_")
        }


def test_admin_agent_migration_upgrades_existing_0015_schema(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "existing-0015.db"
    _upgrade(repo_root, database_path, PREVIOUS_HEAD)

    assert "admin_agent_runs" not in _tables(database_path)
    assert "admin_agent_events" not in _tables(database_path)

    _upgrade(repo_root, database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD,)
        assert {
            row[1]
            for row in connection.execute("PRAGMA table_info(admin_agent_runs)")
        } >= {
            "id",
            "owner_tg_user_id",
            "channel_id",
            "scenario",
            "status",
            "model",
            "tokens_used",
            "result",
            "error",
            "started_at",
            "finished_at",
            "created_at",
        }
        assert {
            row[1]
            for row in connection.execute("PRAGMA table_info(admin_agent_events)")
        } >= {
            "id",
            "run_id",
            "sequence",
            "event_type",
            "tool_name",
            "payload",
            "created_at",
        }


def test_fresh_head_contains_agent_tables_and_matches_registered_orm(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "fresh-head.db"
    _upgrade(repo_root, database_path, "head")

    tables = _tables(database_path)
    assert {"admin_agent_runs", "admin_agent_events"} <= tables
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
