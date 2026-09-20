from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base


PREVIOUS_HEAD = "20260919_0018"
HEAD = "20260920_0025"


def _run_upgrade(
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
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", target],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _upgrade(repo_root: Path, database_path: Path, target: str) -> None:
    result = _run_upgrade(repo_root, database_path, target)
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


def test_admin_agent_resumable_migration_upgrades_existing_0018_schema(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "existing-0018.db"
    _upgrade(repo_root, database_path, PREVIOUS_HEAD)

    assert {"admin_agent_runs", "admin_agent_events"} <= _tables(database_path)
    with sqlite3.connect(database_path) as connection:
        before_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(admin_agent_runs)")
        }
        assert "request_id" in before_columns
        assert "skill_id" not in before_columns
        assert "admin_agent_approvals" in _tables(database_path)
        connection.executemany(
            """
            INSERT INTO admin_agent_runs
                (owner_tg_user_id, channel_id, scenario, request_id, status, tokens_used)
            VALUES
                (?, ?, ?, ?, ?, ?)
            """,
            [
                (777, 999999, "attention_today", "historical-a-run", "completed", 0),
                (777, 999999, "drafts_tomorrow", "historical-b-run", "completed", 0),
                # Slice C does not introduce a new run scenario; its approval is
                # attached to the durable drafts_tomorrow run from B.
                (777, 999999, "drafts_tomorrow", "historical-c-source-run", "completed", 0),
            ],
        )
        connection.commit()

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
            "request_id",
            "operator_input",
            "skill_id",
            "skill_version",
            "workflow_phase",
            "checkpoint",
            "execution_claim_token",
            "execution_claimed_at",
            "status",
            "model",
            "tokens_used",
            "result",
            "error",
            "started_at",
            "finished_at",
            "created_at",
        }
        indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list(admin_agent_runs)")
        }
        assert indexes["uq_admin_agent_run_idempotency"] is True
        assert "admin_agent_approvals" in _tables(database_path)
        approval_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(admin_agent_approvals)")
        }
        assert {
            "owner_tg_user_id",
            "channel_id",
            "action_type",
            "state",
            "content_item_id",
            "content_revision",
            "timezone",
            "target_local_date",
            "local_time",
            "resolved_scheduled_at",
            "action_fingerprint",
            "execution_key",
            "request_id",
            "schedule_entry_id",
            "publication_id",
            "reviewer_tg_user_id",
            "failure_reason",
            "reviewed_at",
            "executed_at",
        } <= approval_columns
        approval_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list(admin_agent_approvals)")
        }
        assert approval_indexes["ix_admin_agent_approvals_execution_key"] is True
        assert "admin_agent_run_artifacts" in _tables(database_path)
        artifact_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(admin_agent_run_artifacts)")
        }
        assert {
            "run_id",
            "artifact_type",
            "ordinal",
            "content_item_id",
            "content_revision",
            "created_at",
        } <= artifact_columns
        historical = connection.execute(
            """
            SELECT request_id, operator_input, skill_id, skill_version, workflow_phase, checkpoint
            FROM admin_agent_runs
            WHERE request_id IN (
                'historical-a-run',
                'historical-b-run',
                'historical-c-source-run'
            )
            ORDER BY request_id
            """
        ).fetchall()
        assert historical == [
            ("historical-a-run", None, None, None, None, None),
            ("historical-b-run", None, None, None, None, None),
            ("historical-c-source-run", None, None, None, None, None),
        ]
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


def test_admin_agent_operator_input_migration_upgrades_existing_0019_schema(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "existing-0019.db"
    _upgrade(repo_root, database_path, "20260919_0019")

    with sqlite3.connect(database_path) as connection:
        before_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(admin_agent_runs)")
        }
        assert "operator_input" not in before_columns
        connection.execute(
            """
            INSERT INTO admin_agent_runs
                (owner_tg_user_id, channel_id, scenario, request_id, status, tokens_used)
            VALUES
                (?, ?, ?, ?, ?, ?)
            """,
            (778, 999998, "drafts_tomorrow", "historical-0019-run", "completed", 0),
        )
        connection.commit()

    _upgrade(repo_root, database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD,)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(admin_agent_runs)")
        }
        assert "operator_input" in columns
        historical = connection.execute(
            """
            SELECT operator_input
            FROM admin_agent_runs
            WHERE request_id = 'historical-0019-run'
            """
        ).fetchone()
        assert historical == (None,)



def test_series_approval_batch_migration_upgrades_existing_0020_schema(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "existing-0020.db"
    _upgrade(repo_root, database_path, "20260919_0020")

    with sqlite3.connect(database_path) as connection:
        assert "admin_agent_approval_batches" not in _tables(database_path)
        connection.execute(
            """
            INSERT INTO admin_agent_runs
                (
                    owner_tg_user_id, channel_id, scenario, request_id,
                    operator_input, status, tokens_used
                )
            VALUES
                (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                779,
                999997,
                "drafts_tomorrow",
                "historical-0020-run",
                None,
                "completed",
                0,
            ),
        )
        connection.execute(
            """
            INSERT INTO admin_agent_approvals
                (
                    owner_tg_user_id, channel_id, action_type, state,
                    content_item_id, content_revision, timezone,
                    target_local_date, local_time, resolved_scheduled_at,
                    action_fingerprint, execution_key, request_id
                )
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                779,
                999997,
                "schedule_draft_tomorrow",
                "pending_review",
                123,
                1,
                "UTC+3",
                "2099-09-20",
                "14:30",
                "2099-09-20 11:30:00+00:00",
                "a" * 64,
                "b" * 64,
                "historical-c-approval-0020",
            ),
        )
        connection.commit()

    _upgrade(repo_root, database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD,)
        assert {
            "admin_agent_approval_batches",
            "admin_agent_approval_batch_items",
        } <= _tables(database_path)
        batch_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(admin_agent_approval_batches)"
            )
        }
        assert {
            "owner_tg_user_id",
            "channel_id",
            "source_run_id",
            "action_type",
            "state",
            "request_id",
            "timezone",
            "item_count",
            "series_title",
            "source_plan_fingerprint",
            "action_fingerprint",
            "execution_key",
            "reviewer_tg_user_id",
            "execution_claim_token",
            "execution_claimed_at",
            "failure_reason",
            "reviewed_at",
            "executed_at",
        } <= batch_columns
        item_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(admin_agent_approval_batch_items)"
            )
        }
        assert {
            "batch_id",
            "ordinal",
            "content_item_id",
            "captured_content_revision",
            "content_title",
            "local_date",
            "local_time",
            "resolved_scheduled_at",
            "item_fingerprint",
            "execution_key",
            "state",
            "schedule_entry_id",
            "publication_id",
            "failure_reason",
            "execution_started_at",
            "executed_at",
        } <= item_columns
        batch_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(admin_agent_approval_batches)"
            )
        }
        item_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(admin_agent_approval_batch_items)"
            )
        }
        assert batch_indexes["ix_admin_agent_approval_batches_execution_key"] is True
        assert item_indexes["sqlite_autoindex_admin_agent_approval_batch_items_3"] is True
        assert connection.execute(
            """
            SELECT request_id, operator_input, status
            FROM admin_agent_runs
            WHERE request_id = 'historical-0020-run'
            """
        ).fetchone() == ("historical-0020-run", None, "completed")
        assert connection.execute(
            """
            SELECT action_type, state, request_id
            FROM admin_agent_approvals
            WHERE request_id = 'historical-c-approval-0020'
            """
        ).fetchone() == (
            "schedule_draft_tomorrow",
            "pending_review",
            "historical-c-approval-0020",
        )


def test_recurring_automation_migration_upgrades_existing_0021_schema(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "existing-0021.db"
    _upgrade(repo_root, database_path, "20260919_0021")

    with sqlite3.connect(database_path) as connection:
        assert "admin_agent_automations" not in _tables(database_path)
        connection.execute(
            """
            INSERT INTO admin_agent_runs
                (
                    owner_tg_user_id, channel_id, scenario, request_id,
                    operator_input, skill_id, skill_version, status, tokens_used
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                780,
                999996,
                "attention_today",
                "historical-0021-run",
                None,
                "attention_today",
                "1",
                "completed",
                0,
            ),
        )
        connection.commit()

    _upgrade(repo_root, database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD,)
        assert "admin_agent_automations" in _tables(database_path)
        automation_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(admin_agent_automations)"
            )
        }
        assert {
            "owner_tg_user_id",
            "channel_id",
            "skill_id",
            "skill_version",
            "operator_input",
            "cadence_kind",
            "local_time",
            "weekday",
            "timezone",
            "enabled",
            "disabled_reason",
            "disabled_at",
            "last_outcome",
            "last_outcome_at",
            "next_run_at",
            "last_scheduled_for",
            "claim_token",
            "claimed_at",
            "request_id",
            "definition_fingerprint",
            "created_at",
            "updated_at",
        } <= automation_columns
        assert "ix_admin_agent_automations_due" in {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(admin_agent_automations)"
            )
        }

        run_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(admin_agent_runs)")
        }
        assert {"automation_id", "scheduled_for"} <= run_columns
        assert connection.execute(
            """
            SELECT automation_id, scheduled_for
            FROM admin_agent_runs
            WHERE request_id = 'historical-0021-run'
            """
        ).fetchone() == (None, None)


def test_automation_observability_migration_upgrades_0022_with_nullable_metadata(
    tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "existing-0022.db"
    _upgrade(repo_root, database_path, "20260919_0022")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO admin_agent_automations
                (
                    owner_tg_user_id, channel_id, skill_id, skill_version,
                    operator_input, cadence_kind, local_time, weekday, timezone,
                    enabled, next_run_at, last_scheduled_for, claim_token, claimed_at,
                    request_id, definition_fingerprint
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                780,
                999996,
                "attention_today",
                "1",
                "{}",
                "daily",
                "09:00",
                None,
                "UTC",
                1,
                "2026-09-20 09:00:00+00:00",
                None,
                None,
                None,
                "historical-0022-automation",
                "a" * 64,
            ),
        )
        connection.commit()

    _upgrade(repo_root, database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD,)
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(admin_agent_automations)"
            )
        }
        assert {
            "disabled_reason",
            "disabled_at",
            "last_outcome",
            "last_outcome_at",
        } <= columns
        assert connection.execute(
            """
            SELECT disabled_reason, disabled_at, last_outcome, last_outcome_at
            FROM admin_agent_automations
            WHERE request_id = 'historical-0022-automation'
            """
        ).fetchone() == (None, None, None, None)


def test_active_approval_uniqueness_migration_creates_partial_indexes(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "active-approval-unique.db"
    _upgrade(repo_root, database_path, "20260919_0023")
    _upgrade(repo_root, database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD,)
        indexes = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT name, sql
                FROM sqlite_master
                WHERE type = 'index'
                  AND name IN (
                    'uq_admin_agent_approval_active_target',
                    'uq_admin_agent_approval_batch_active_source_run'
                  )
                """
            ).fetchall()
        }
        assert set(indexes) == {
            "uq_admin_agent_approval_active_target",
            "uq_admin_agent_approval_batch_active_source_run",
        }
        assert all(sql is not None and "CREATE UNIQUE INDEX" in sql for sql in indexes.values())
        assert all(
            "WHERE state IN ('pending_review','executing')" in str(sql)
            for sql in indexes.values()
        )


def test_active_approval_uniqueness_migration_fails_on_duplicate_single_target(
    tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "duplicate-active-single.db"
    _upgrade(repo_root, database_path, "20260919_0023")

    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO admin_agent_approvals
                (
                    owner_tg_user_id, channel_id, action_type, state,
                    content_item_id, content_revision, timezone,
                    target_local_date, local_time, resolved_scheduled_at,
                    action_fingerprint, request_id
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    8801,
                    99001,
                    "schedule_draft_tomorrow",
                    "pending_review",
                    501,
                    3,
                    "UTC",
                    "2026-09-21",
                    "14:00",
                    "2026-09-21 14:00:00+00:00",
                    "a" * 64,
                    "duplicate-single-a",
                ),
                (
                    8801,
                    99001,
                    "schedule_draft_tomorrow",
                    "executing",
                    501,
                    3,
                    "UTC",
                    "2026-09-21",
                    "15:00",
                    "2026-09-21 15:00:00+00:00",
                    "b" * 64,
                    "duplicate-single-b",
                ),
            ],
        )
        connection.commit()

    result = _run_upgrade(repo_root, database_path, "head")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "duplicate active admin_agent_approvals target blocks migration" in output
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("20260919_0023",)


def test_active_approval_uniqueness_migration_fails_on_duplicate_series_target(
    tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "duplicate-active-series.db"
    _upgrade(repo_root, database_path, "20260919_0023")

    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO admin_agent_approval_batches
                (
                    owner_tg_user_id, channel_id, source_run_id, action_type, state,
                    request_id, timezone, item_count, series_title,
                    source_plan_fingerprint, action_fingerprint, execution_key
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    8802,
                    99002,
                    601,
                    "schedule_content_series",
                    "pending_review",
                    "duplicate-series-a",
                    "UTC",
                    3,
                    "Duplicate series",
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                ),
                (
                    8802,
                    99002,
                    601,
                    "schedule_content_series",
                    "executing",
                    "duplicate-series-b",
                    "UTC",
                    3,
                    "Duplicate series",
                    "c" * 64,
                    "f" * 64,
                    "1" * 64,
                ),
            ],
        )
        connection.commit()

    result = _run_upgrade(repo_root, database_path, "head")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "duplicate active admin_agent_approval_batches source run blocks migration" in output
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("20260919_0023",)


def test_execution_fence_migration_backfills_active_claims(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "execution-fence.db"
    _upgrade(repo_root, database_path, "20260920_0024")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO admin_agent_approvals
                (
                    owner_tg_user_id, channel_id, action_type, state,
                    content_item_id, content_revision, timezone,
                    target_local_date, local_time, resolved_scheduled_at,
                    action_fingerprint, execution_key, request_id,
                    execution_claim_token, execution_claimed_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                8803,
                99003,
                "schedule_draft_tomorrow",
                "executing",
                701,
                2,
                "UTC",
                "2026-09-21",
                "14:00",
                "2026-09-21 14:00:00+00:00",
                "a" * 64,
                "b" * 64,
                "fence-single",
                "c" * 64,
                "2026-09-20 10:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO admin_agent_approval_batches
                (
                    owner_tg_user_id, channel_id, source_run_id, action_type, state,
                    request_id, timezone, item_count, series_title,
                    source_plan_fingerprint, action_fingerprint, execution_key,
                    execution_claim_token, execution_claimed_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                8804,
                99004,
                702,
                "schedule_content_series",
                "executing",
                "fence-series",
                "UTC",
                2,
                "Fence series",
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "1" * 64,
                "2026-09-20 10:00:00+00:00",
            ),
        )
        connection.commit()

    _upgrade(repo_root, database_path, "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD,)
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(admin_agent_execution_fences)"
            )
        }
        assert {"fence_key", "claim_token", "created_at", "updated_at"} <= columns
        rows = connection.execute(
            """
            SELECT fence_key, claim_token
            FROM admin_agent_execution_fences
            ORDER BY fence_key
            """
        ).fetchall()
        assert rows == [
            ("approval-execution:" + "b" * 64, "c" * 64),
            ("approval-execution:" + "f" * 64, "1" * 64),
        ]


def test_fresh_head_contains_agent_tables_and_matches_registered_orm(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "fresh-head.db"
    _upgrade(repo_root, database_path, "head")

    tables = _tables(database_path)
    assert {
        "admin_agent_runs",
        "admin_agent_events",
        "admin_agent_approvals",
        "admin_agent_run_artifacts",
        "admin_agent_approval_batches",
        "admin_agent_approval_batch_items",
        "admin_agent_execution_fences",
    } <= tables
    assert tables == set(Base.metadata.tables) | {"alembic_version"}


def test_admin_agent_approval_regression_suite_is_mandatory() -> None:
    """Keep MVP-C approval regressions inside the existing blocking CI gate."""

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_admin_agent_approvals.py",
            "tests/test_admin_agent_series_approvals.py",
            "tests/test_studio_admin_agent_approval_api.py",
            "tests/test_studio_admin_agent_series_approval_api.py",
            "tests/test_admin_agent_resumable.py",
            "tests/test_studio_admin_agent_resume_api.py",
        ],
        cwd=repo_root,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
