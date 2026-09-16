from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


HEAD = "20260818_0013"
OTHER_PARENT = "20260811_0006"
TABLE = "publication_autodelete_actions"


def _run_alembic(
    repo_root: Path,
    database_path: Path,
    command: str,
    target: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "BOT_TOKEN": env.get("BOT_TOKEN", "123456:test-token-placeholder"),
            "API_ID": env.get("API_ID", "123456"),
            "API_HASH": env.get("API_HASH", "0123456789abcdef0123456789abcdef"),
            "DB_URL": f"sqlite+aiosqlite:///{database_path}",
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, target],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _version(database_path: Path) -> str | None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return None if row is None else str(row[0])


def _table_exists(database_path: Path, table_name: str) -> bool:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    return row is not None


def test_empty_action_ledger_supports_branch_downgrade_upgrade_round_trip(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "empty-round-trip.db"

    upgraded = _run_alembic(repo_root, database_path, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert _version(database_path) == HEAD
    assert _table_exists(database_path, TABLE)

    # From the merge head, selecting the delivery-actions parent removes only the
    # time-autodelete branch. This isolates the destructive guard under test.
    downgraded = _run_alembic(repo_root, database_path, "downgrade", OTHER_PARENT)
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    assert _version(database_path) == OTHER_PARENT
    assert not _table_exists(database_path, TABLE)

    reupgraded = _run_alembic(repo_root, database_path, "upgrade", "head")
    assert reupgraded.returncode == 0, reupgraded.stdout + reupgraded.stderr
    assert _version(database_path) == HEAD
    assert _table_exists(database_path, TABLE)


def test_nonempty_action_ledger_refuses_unsafe_branch_downgrade(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "nonempty-downgrade.db"

    upgraded = _run_alembic(repo_root, database_path, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert _version(database_path) == HEAD

    # sqlite3 connections do not enable FK enforcement by default. That lets this test
    # install a minimal safety-evidence row without constructing the entire publication
    # graph; the migration guard only cares that irreversible evidence exists.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"""
            INSERT INTO {TABLE} (
                publication_id,
                telegram_message_id,
                telegram_chat_id,
                authority_fingerprint,
                reservation_token,
                reserved_by_lease_token,
                state
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                999999,
                1001,
                -1001234567890,
                "a" * 64,
                "reservation-token",
                "lease-token",
                "reserved",
            ),
        )
        connection.commit()

    downgraded = _run_alembic(repo_root, database_path, "downgrade", OTHER_PARENT)
    assert downgraded.returncode != 0
    output = downgraded.stdout + downgraded.stderr
    assert "refusing unsafe downgrade" in output
    assert _version(database_path) == HEAD
    assert _table_exists(database_path, TABLE)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            f"SELECT state FROM {TABLE} WHERE publication_id = 999999"
        ).fetchone()
    assert row == ("reserved",)
