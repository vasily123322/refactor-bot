from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run_alembic(
    repo_root: Path,
    database_path: Path,
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
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            "upgrade",
            target,
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def _version(database_path: Path) -> tuple[str] | None:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()


def test_alembic_baseline_is_frozen_and_followup_revision_is_idempotent(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "baseline.db"

    baseline = _run_alembic(repo_root, database_path, "20260809_0001")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr

    baseline_tables = _table_names(database_path)
    assert {
        "channels",
        "post_tasks",
        "content_items",
        "content_revisions",
        "media_assets",
        "schedule_entries",
        "publications",
        "publication_attempts",
        "source_connectors",
        "source_documents",
        "source_ingestion_leases",
        "content_candidates",
        "candidate_enrichment_runs",
        "candidate_rewrite_runs",
        "ai_auto_tasks",
        "alembic_version",
    }.issubset(baseline_tables)
    assert "scheduler_task_leases" not in baseline_tables
    assert _version(database_path) == ("20260809_0001",)

    head = _run_alembic(repo_root, database_path, "head")
    assert head.returncode == 0, head.stdout + head.stderr
    assert "scheduler_task_leases" in _table_names(database_path)
    assert _version(database_path) == ("20260809_0002",)

    repeated = _run_alembic(repo_root, database_path, "head")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert _version(database_path) == ("20260809_0002",)
