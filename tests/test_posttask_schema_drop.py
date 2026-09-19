from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services.canonical_runtime_safety import has_no_replay_barrier
from app.services.publication_autodelete_action_ledger import (
    PublicationAutodeleteActionLedger,
)
from app.services.publication_autodelete_lease import PublicationAutodeleteLeaseHandle


PRE_DROP = "20260918_0014"
HEAD = "20260919_0022"


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
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", target],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _upgrade_to_pre_drop(repo_root: Path, database_path: Path) -> None:
    result = _run_alembic(repo_root, database_path, PRE_DROP)
    assert result.returncode == 0, result.stdout + result.stderr


def _fingerprint(task_id: int) -> str:
    return sha256(f"legacy-post-task:{int(task_id)}".encode("utf-8")).hexdigest()


def _task_payload() -> dict:
    return {"type": "text", "text": "historical"}


def _evidence(
    *,
    status: str = "done",
    channel_id: int = 7,
    error: str | None = None,
    unknown_delivery_no_replay: bool = False,
    destructive_action: dict | None = None,
    publication_id: int | None = None,
) -> dict:
    return {
        "version": 1,
        "legacy_transport": {
            "status": status,
            "channel_id": channel_id,
            "scheduled_at": None,
            "dedupe_key": None,
            "error": error,
            "payload": _task_payload(),
            "unknown_delivery_no_replay": unknown_delivery_no_replay,
        },
        "destructive_action": destructive_action,
        "publication_id": publication_id,
        "scheduler_lease_present": False,
    }


def _insert_task(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    status: str = "done",
    error: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO post_tasks
            (id, channel_id, status, payload, dedupe_key, scheduled_at, error)
        VALUES (?, ?, ?, ?, NULL, NULL, ?)
        """,
        (
            task_id,
            7,
            status,
            json.dumps(_task_payload(), ensure_ascii=False),
            error,
        ),
    )


def _insert_publication(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    legacy_post_task_id: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO publications
            (
                id, content_item_id, content_revision, channel_id, status,
                legacy_post_task_id, attempt_count, metadata, execution_mode
            )
        VALUES (?, 1, 1, 7, 'published', ?, 0, '{}', 'canonical')
        """,
        (publication_id, legacy_post_task_id),
    )


def _insert_audit(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    state: str,
    evidence: dict,
    publication_id: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO canonical_runtime_safety_audits
            (publication_id, source_fingerprint, state, evidence)
        VALUES (?, ?, ?, ?)
        """,
        (
            publication_id,
            _fingerprint(task_id),
            state,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        ),
    )


def test_safe_drained_fixture_upgrades_and_drops_legacy_schema(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "safe-upgrade.db"
    _upgrade_to_pre_drop(repo_root, database_path)

    with sqlite3.connect(database_path) as connection:
        _insert_task(connection, task_id=101)
        evidence = _evidence()
        _insert_audit(
            connection,
            task_id=101,
            state="terminal_archived",
            evidence=evidence,
        )
        connection.commit()

    result = _run_alembic(repo_root, database_path, "head")
    assert result.returncode == 0, result.stdout + result.stderr

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "post_tasks" not in tables
        assert "scheduler_task_leases" not in tables
        assert "legacy_time_views_delete_actions" not in tables
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(publications)")
        }
        assert "legacy_post_task_id" not in columns
        row = connection.execute(
            """
            SELECT state, evidence
            FROM canonical_runtime_safety_audits
            WHERE source_fingerprint = ?
            """,
            (_fingerprint(101),),
        ).fetchone()
        assert row is not None
        assert row[0] == "terminal_archived"
        assert json.loads(row[1]) == evidence
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD,)


def test_non_null_publication_legacy_link_blocks_drop(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "linked.db"
    _upgrade_to_pre_drop(repo_root, database_path)

    with sqlite3.connect(database_path) as connection:
        _insert_task(connection, task_id=102)
        _insert_publication(
            connection,
            publication_id=502,
            legacy_post_task_id=102,
        )
        connection.commit()

    result = _run_alembic(repo_root, database_path, "head")
    assert result.returncode != 0
    assert "Publication legacy links remain" in result.stdout + result.stderr


def test_scheduler_lease_blocks_drop(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "lease.db"
    _upgrade_to_pre_drop(repo_root, database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO scheduler_task_leases
                (task_id, lease_token, holder, expires_at)
            VALUES (999, 'lease-token', 'test', CURRENT_TIMESTAMP)
            """
        )
        connection.commit()

    result = _run_alembic(repo_root, database_path, "head")
    assert result.returncode != 0
    assert "SchedulerTaskLease rows remain" in result.stdout + result.stderr


def test_unmapped_reserved_destructive_evidence_blocks_drop(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "reserved.db"
    _upgrade_to_pre_drop(repo_root, database_path)

    with sqlite3.connect(database_path) as connection:
        _insert_task(connection, task_id=103)
        _insert_audit(
            connection,
            task_id=103,
            state="terminal_archived",
            evidence=_evidence(),
        )
        connection.execute(
            """
            INSERT INTO legacy_time_views_delete_actions
                (
                    post_task_id, chat_id, message_ids, target_fingerprint,
                    reservation_token, state
                )
            VALUES (?, ?, ?, ?, ?, 'reserved')
            """,
            (
                103,
                -100123,
                json.dumps([44]),
                "b" * 64,
                "reservation-103",
            ),
        )
        connection.commit()

    result = _run_alembic(repo_root, database_path, "head")
    assert result.returncode != 0
    assert "is not a no-replay audit" in result.stdout + result.stderr


def test_unknown_delivery_without_mapped_no_replay_blocks_drop(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "unknown-delivery.db"
    _upgrade_to_pre_drop(repo_root, database_path)

    with sqlite3.connect(database_path) as connection:
        _insert_task(
            connection,
            task_id=104,
            error="UNKNOWN_DELIVERY_ERROR",
        )
        _insert_audit(
            connection,
            task_id=104,
            state="terminal_archived",
            evidence=_evidence(
                error="UNKNOWN_DELIVERY_ERROR",
                unknown_delivery_no_replay=True,
            ),
        )
        connection.commit()

    result = _run_alembic(repo_root, database_path, "head")
    assert result.returncode != 0
    assert "UNKNOWN_DELIVERY_ERROR lacks a canonical no-replay barrier" in (
        result.stdout + result.stderr
    )


def test_preserved_no_replay_survives_drop_and_blocks_new_delete_reservation(
    tmp_path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "no-replay.db"
    _upgrade_to_pre_drop(repo_root, database_path)

    publication_id = 505
    task_id = 105
    with sqlite3.connect(database_path) as connection:
        _insert_publication(connection, publication_id=publication_id)
        _insert_task(
            connection,
            task_id=task_id,
            error="UNKNOWN_DELIVERY_ERROR",
        )
        evidence = _evidence(
            error="UNKNOWN_DELIVERY_ERROR",
            unknown_delivery_no_replay=True,
            publication_id=publication_id,
        )
        _insert_audit(
            connection,
            task_id=task_id,
            state="terminal_no_replay",
            evidence=evidence,
            publication_id=publication_id,
        )
        connection.commit()

    result = _run_alembic(repo_root, database_path, "head")
    assert result.returncode == 0, result.stdout + result.stderr

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO publication_autodelete_leases
                (publication_id, lease_token, holder, expires_at)
            VALUES (?, 'lease-505', 'test', datetime('now', '+1 hour'))
            """,
            (publication_id,),
        )
        connection.commit()

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            async with AsyncSession(engine) as session:
                assert await has_no_replay_barrier(
                    session,
                    publication_id=publication_id,
                )
                handle = PublicationAutodeleteLeaseHandle(
                    publication_id=publication_id,
                    lease_token="lease-505",
                    holder="test",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
                reserved = await PublicationAutodeleteActionLedger(session).reserve(
                    handle,
                    telegram_chat_id=-100123,
                    telegram_message_id=44,
                    authority_fingerprint="a" * 64,
                )
                assert reserved.outcome == "ambiguous"
        finally:
            await engine.dispose()

    asyncio.run(run())

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM publication_autodelete_actions"
        ).fetchone() == (0,)
        row = connection.execute(
            """
            SELECT state, evidence
            FROM canonical_runtime_safety_audits
            WHERE publication_id = ?
            """,
            (publication_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "terminal_no_replay"
        assert json.loads(row[1])["legacy_transport"][
            "unknown_delivery_no_replay"
        ] is True
