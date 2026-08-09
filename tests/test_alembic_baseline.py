from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run_alembic(repo_root: Path, database_path: Path) -> subprocess.CompletedProcess[str]:
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
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_alembic_baseline_creates_current_schema_and_is_idempotent(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "baseline.db"

    first = _run_alembic(repo_root, database_path)
    assert first.returncode == 0, first.stdout + first.stderr

    with sqlite3.connect(database_path) as connection:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "channels",
            "post_tasks",
            "content_items",
            "content_revisions",
            "schedule_entries",
            "publications",
            "publication_attempts",
            "source_connectors",
            "source_ingestion_leases",
            "content_candidates",
            "candidate_enrichment_runs",
            "ai_auto_tasks",
            "alembic_version",
        }.issubset(table_names)
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        assert version == ("20260809_0001",)

    second = _run_alembic(repo_root, database_path)
    assert second.returncode == 0, second.stdout + second.stderr
    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        assert version == ("20260809_0001",)
