from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import app.domain  # noqa: F401 register complete ORM metadata
from app.core.db import Base


PREVIOUS_HEAD = "20260919_0018"
HEAD = "20260919_0022"


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
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "next_run_at",
            "last_scheduled_for",
            "claim_token",
            "claimed_at",
            "request_id",
            "definition_fingerprint",
            "created_at",
            "updated_at",
        } <= automation_columns
        automation_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(admin_agent_automations)"
            )
        }
        assert automation_indexes["sqlite_autoindex_admin_agent_automations_1"] is True
        assert "ix_admin_agent_automations_due" in automation_indexes

        run_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(admin_agent_runs)")
        }
        assert {"automation_id", "scheduled_for"} <= run_columns
        run_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list(admin_agent_runs)")
        }
        assert run_indexes["sqlite_autoindex_admin_agent_runs_1"] is True or any(
            unique and "automation_occurrence" in name
            for name, unique in run_indexes.items()
        )
        assert connection.execute(
            """
            SELECT automation_id, scheduled_for
            FROM admin_agent_runs
            WHERE request_id = 'historical-0021-run'
            """
        ).fetchone() == (None, None)


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
