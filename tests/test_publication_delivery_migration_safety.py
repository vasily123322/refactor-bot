from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


HEAD = "20260818_0013"
OTHER_PARENT = "20260812_0006"
TABLE = "publication_delivery_actions"


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


def test_empty_delivery_ledger_supports_branch_downgrade_upgrade_round_trip(
    tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "empty-delivery-round-trip.db"

    upgraded = _run_alembic(repo_root, database_path, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert _version(database_path) == HEAD
    assert _table_exists(database_path, TABLE)

    # Selecting the time-autodelete parent removes only the delivery-actions branch.
    downgraded = _run_alembic(repo_root, database_path, "downgrade", OTHER_PARENT)
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    assert _version(database_path) == OTHER_PARENT
    assert not _table_exists(database_path, TABLE)

    reupgraded = _run_alembic(repo_root, database_path, "upgrade", "head")
    assert reupgraded.returncode == 0, reupgraded.stdout + reupgraded.stderr
    assert _version(database_path) == HEAD
    assert _table_exists(database_path, TABLE)


def test_nonempty_delivery_ledger_refuses_unsafe_branch_downgrade(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "nonempty-delivery-downgrade.db"

    upgraded = _run_alembic(repo_root, database_path, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert _version(database_path) == HEAD

    # FK enforcement is disabled for this sqlite3 connection, allowing a minimal
    # durable evidence row without constructing the complete publication graph.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"""
            INSERT INTO {TABLE} (
                publication_id,
                action_key,
                action_type,
                state,
                intent_fingerprint,
                reserved_by_lease_token
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                999999,
                "pin:source",
                "pin",
                "reserved",
                "b" * 64,
                "lease-token",
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
