from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine

import app.domain  # noqa: F401 register the complete ORM schema
from app.core.db import Base
from migration_snapshots.schema_20260809 import Base as FrozenBaselineBase


HEAD = "20260919_0017"


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
            if not str(row[0]).startswith("sqlite_")
        }


def _version(database_path: Path) -> tuple[str] | None:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()


def test_alembic_baseline_is_frozen_and_followup_revisions_are_idempotent(
    tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "baseline.db"
    current_orm_tables = set(Base.metadata.tables)
    frozen_baseline_tables = set(FrozenBaselineBase.metadata.tables)
    retained_followup_tables = {
        "publication_autodelete_leases",
        "publication_autodelete_view_states",
        "publication_delivery_leases",
        "publication_delivery_actions",
        "publication_autodelete_actions",
        "posting_dedupe_locks",
        "studio_channel_onboarding_requests",
        "channel_dm_reply_commands",
        "channel_dm_reply_intents",
        "canonical_runtime_safety_audits",
    }
    dropped_legacy_tables = {
        "post_tasks",
        "scheduler_task_leases",
        "legacy_time_views_delete_actions",
    }

    assert "post_tasks" in frozen_baseline_tables
    assert "post_tasks" not in current_orm_tables
    assert "legacy_post_task_id" in FrozenBaselineBase.metadata.tables[
        "publications"
    ].columns
    assert "legacy_post_task_id" not in Base.metadata.tables["publications"].columns
    assert retained_followup_tables <= current_orm_tables
    assert dropped_legacy_tables.isdisjoint(current_orm_tables)

    baseline = _run_alembic(repo_root, database_path, "20260809_0001")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr

    assert _table_names(database_path) == frozen_baseline_tables | {"alembic_version"}
    assert _version(database_path) == ("20260809_0001",)

    head = _run_alembic(repo_root, database_path, "head")
    assert head.returncode == 0, head.stdout + head.stderr
    assert _table_names(database_path) == current_orm_tables | {"alembic_version"}
    assert dropped_legacy_tables.isdisjoint(_table_names(database_path))
    assert _version(database_path) == (HEAD,)

    repeated = _run_alembic(repo_root, database_path, "head")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert _table_names(database_path) == current_orm_tables | {"alembic_version"}
    assert _version(database_path) == (HEAD,)


def test_followup_revisions_adopt_tables_precreated_by_legacy_create_all(
    tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "legacy-precreated.db"
    followup_tables = (
        "publication_autodelete_leases",
        "publication_autodelete_view_states",
        "publication_delivery_leases",
        "publication_delivery_actions",
        "publication_autodelete_actions",
        "posting_dedupe_locks",
        "studio_channel_onboarding_requests",
        "channel_dm_reply_commands",
        "channel_dm_reply_intents",
        "canonical_runtime_safety_audits",
    )

    baseline = _run_alembic(repo_root, database_path, "20260809_0001")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    for table_name in followup_tables:
        assert table_name not in _table_names(database_path)

    sync_engine = create_engine(f"sqlite:///{database_path}")
    try:
        for table_name in followup_tables:
            Base.metadata.tables[table_name].create(
                bind=sync_engine,
                checkfirst=True,
            )
    finally:
        sync_engine.dispose()
    for table_name in followup_tables:
        assert table_name in _table_names(database_path)
    assert _version(database_path) == ("20260809_0001",)

    adopted = _run_alembic(repo_root, database_path, "head")
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    assert _version(database_path) == (HEAD,)
    assert _table_names(database_path) == set(Base.metadata.tables) | {"alembic_version"}
