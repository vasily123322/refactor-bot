"""Drop retired PostTask schema after durable safety-evidence proof.

Revision ID: 20260919_0015
Revises: 20260918_0014
Create Date: 2026-09-19
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping

from alembic import op
import sqlalchemy as sa


revision = "20260919_0015"
down_revision = "20260918_0014"
branch_labels = None
depends_on = None


_ACTIVE_TASK_STATUSES = frozenset({"pending", "processing"})
_NO_REPLAY_STATE = "terminal_no_replay"
_ARCHIVED_STATES = frozenset({"terminal_archived", _NO_REPLAY_STATE})
_LEGACY_LINK = "legacy_post_task_id"


def _mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if isinstance(decoded, Mapping):
            return {str(key): item for key, item in decoded.items()}
    return None


def _sequence(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if isinstance(decoded, list):
            return list(decoded)
    return None


def _source_fingerprint(task_id: int) -> str:
    return sha256(f"legacy-post-task:{int(task_id)}".encode("utf-8")).hexdigest()


def _abort(reason: str) -> None:
    raise RuntimeError(f"refusing unsafe PostTask schema drop: {reason}")


def _audit_table() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        "canonical_runtime_safety_audits",
        metadata,
        sa.Column("id", sa.Integer()),
        sa.Column("publication_id", sa.Integer()),
        sa.Column("source_fingerprint", sa.String(length=64)),
        sa.Column("state", sa.String(length=32)),
        sa.Column("evidence", sa.JSON()),
    )


def _post_task_table() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        "post_tasks",
        metadata,
        sa.Column("id", sa.Integer()),
        sa.Column("channel_id", sa.Integer()),
        sa.Column("status", sa.String(length=32)),
        sa.Column("payload", sa.JSON()),
        sa.Column("dedupe_key", sa.String(length=255)),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
    )


def _legacy_action_table() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        "legacy_time_views_delete_actions",
        metadata,
        sa.Column("id", sa.Integer()),
        sa.Column("post_task_id", sa.Integer()),
        sa.Column("chat_id", sa.Integer()),
        sa.Column("message_ids", sa.JSON()),
        sa.Column("target_fingerprint", sa.String(length=64)),
        sa.Column("reservation_token", sa.String(length=64)),
        sa.Column("state", sa.String(length=16)),
        sa.Column("reserved_at", sa.DateTime(timezone=True)),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
    )


def _load_audits(bind: sa.Connection) -> dict[str, Mapping[str, Any]]:
    audit = _audit_table()
    rows = bind.execute(
        sa.select(
            audit.c.publication_id,
            audit.c.source_fingerprint,
            audit.c.state,
            audit.c.evidence,
        )
    ).mappings()
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        fingerprint = str(row["source_fingerprint"] or "")
        if not fingerprint:
            _abort("canonical runtime safety audit has an empty source fingerprint")
        result[fingerprint] = row
    return result


def _validate_task_evidence(
    *,
    row: Mapping[str, Any],
    audit_row: Mapping[str, Any] | None,
) -> None:
    task_id = int(row["id"])
    status = str(row["status"] or "")
    if status in _ACTIVE_TASK_STATUSES:
        _abort(f"PostTask {task_id} is still active with status={status!r}")
    if audit_row is None:
        _abort(f"PostTask {task_id} has no canonical runtime safety audit")

    audit_state = str(audit_row["state"] or "")
    if audit_state not in _ARCHIVED_STATES:
        _abort(f"PostTask {task_id} audit is not terminal: state={audit_state!r}")

    evidence = _mapping(audit_row["evidence"])
    if evidence is None:
        _abort(f"PostTask {task_id} audit evidence is malformed")
    transport = _mapping(evidence.get("legacy_transport"))
    if transport is None:
        _abort(f"PostTask {task_id} audit lacks legacy transport provenance")

    if str(transport.get("status") or "") != status:
        _abort(f"PostTask {task_id} audit status does not match source")
    try:
        evidence_channel_id = int(transport.get("channel_id"))
    except (TypeError, ValueError, OverflowError):
        _abort(f"PostTask {task_id} audit has invalid channel provenance")
    if evidence_channel_id != int(row["channel_id"]):
        _abort(f"PostTask {task_id} audit channel does not match source")

    source_payload = _mapping(row["payload"])
    archived_payload = _mapping(transport.get("payload"))
    if source_payload is None or archived_payload is None or source_payload != archived_payload:
        _abort(f"PostTask {task_id} payload provenance is not durably preserved")

    source_error = row["error"]
    archived_error = transport.get("error")
    if archived_error != source_error:
        _abort(f"PostTask {task_id} error provenance does not match source")

    if str(source_error or "") == "UNKNOWN_DELIVERY_ERROR":
        if (
            audit_state != _NO_REPLAY_STATE
            or transport.get("unknown_delivery_no_replay") is not True
            or audit_row["publication_id"] is None
        ):
            _abort(
                f"PostTask {task_id} UNKNOWN_DELIVERY_ERROR lacks a canonical no-replay barrier"
            )


def _validate_destructive_evidence(
    *,
    row: Mapping[str, Any],
    audit_row: Mapping[str, Any] | None,
) -> None:
    task_id = int(row["post_task_id"])
    state = str(row["state"] or "")
    if audit_row is None:
        _abort(f"legacy destructive action for PostTask {task_id} has no safety audit")
    if str(audit_row["state"] or "") != _NO_REPLAY_STATE:
        _abort(
            f"legacy destructive action for PostTask {task_id} is not a no-replay audit"
        )
    if audit_row["publication_id"] is None:
        _abort(
            f"legacy destructive action for PostTask {task_id} is not mapped to canonical identity"
        )

    evidence = _mapping(audit_row["evidence"])
    destructive = _mapping(evidence.get("destructive_action")) if evidence else None
    if destructive is None:
        _abort(f"legacy destructive action for PostTask {task_id} is not preserved")

    archived_message_ids = _sequence(destructive.get("message_ids"))
    source_message_ids = _sequence(row["message_ids"])
    exact = (
        str(destructive.get("state") or "") == state
        and int(destructive.get("chat_id") or 0) == int(row["chat_id"])
        and archived_message_ids == source_message_ids
        and str(destructive.get("target_fingerprint") or "")
        == str(row["target_fingerprint"] or "")
        and str(destructive.get("reservation_token") or "")
        == str(row["reservation_token"] or "")
        and destructive.get("automatic_replay_forbidden") is True
    )
    if not exact:
        _abort(
            f"legacy destructive action for PostTask {task_id} does not match preserved evidence"
        )
    if state in {"reserved", "unknown"} and str(audit_row["state"]) != _NO_REPLAY_STATE:
        _abort(f"unmapped {state} destructive evidence for PostTask {task_id}")


def _validate_drop_preconditions(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "canonical_runtime_safety_audits" not in tables:
        _abort("canonical_runtime_safety_audits is missing")

    publication_columns = {
        str(column["name"]) for column in inspector.get_columns("publications")
    }
    if _LEGACY_LINK in publication_columns:
        linked = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM publications "
                "WHERE legacy_post_task_id IS NOT NULL"
            )
        ).scalar_one()
        if int(linked or 0) != 0:
            _abort(f"{int(linked)} Publication legacy links remain")

    if "execution_mode" in publication_columns:
        intentional_legacy = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM publications "
                "WHERE execution_mode = 'intentional_legacy'"
            )
        ).scalar_one()
        if int(intentional_legacy or 0) != 0:
            _abort(f"{int(intentional_legacy)} intentional-legacy Publications remain")

    if "scheduler_task_leases" in tables:
        leases = bind.execute(
            sa.text("SELECT COUNT(*) FROM scheduler_task_leases")
        ).scalar_one()
        if int(leases or 0) != 0:
            _abort(f"{int(leases)} SchedulerTaskLease rows remain")

    active_audits = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM canonical_runtime_safety_audits "
            "WHERE state = 'active'"
        )
    ).scalar_one()
    if int(active_audits or 0) != 0:
        _abort(f"{int(active_audits)} active legacy runtime audits remain")

    audits = _load_audits(bind)

    if "post_tasks" in tables:
        tasks = _post_task_table()
        for row in bind.execute(sa.select(tasks)).mappings():
            _validate_task_evidence(
                row=row,
                audit_row=audits.get(_source_fingerprint(int(row["id"]))),
            )

    if "legacy_time_views_delete_actions" in tables:
        actions = _legacy_action_table()
        for row in bind.execute(sa.select(actions)).mappings():
            _validate_destructive_evidence(
                row=row,
                audit_row=audits.get(
                    _source_fingerprint(int(row["post_task_id"]))
                ),
            )


def _drop_publication_legacy_link(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    columns = {str(column["name"]) for column in inspector.get_columns("publications")}
    if _LEGACY_LINK not in columns:
        return

    indexes = [
        item
        for item in inspector.get_indexes("publications")
        if _LEGACY_LINK in [str(name) for name in item.get("column_names") or []]
    ]
    uniques = [
        item
        for item in inspector.get_unique_constraints("publications")
        if _LEGACY_LINK in [str(name) for name in item.get("column_names") or []]
    ]
    foreign_keys = [
        item
        for item in inspector.get_foreign_keys("publications")
        if _LEGACY_LINK
        in [str(name) for name in item.get("constrained_columns") or []]
    ]

    with op.batch_alter_table("publications") as batch_op:
        for item in indexes:
            name = item.get("name")
            if name:
                batch_op.drop_index(str(name))
        for item in uniques:
            name = item.get("name")
            if name:
                batch_op.drop_constraint(str(name), type_="unique")
        for item in foreign_keys:
            name = item.get("name")
            if name:
                batch_op.drop_constraint(str(name), type_="foreignkey")
        batch_op.drop_column(_LEGACY_LINK)


def upgrade() -> None:
    bind = op.get_bind()
    _validate_drop_preconditions(bind)

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # Child/runtime schema first.
    if "scheduler_task_leases" in tables:
        op.drop_table("scheduler_task_leases")

    # Remove the Publication FK/index/column before the parent PostTask table.
    _drop_publication_legacy_link(bind)

    # The legacy destructive ledger is dropped only after exact durable preservation
    # was proved above. Canonical runtime safety audits remain permanently.
    inspector = sa.inspect(bind)
    if inspector.has_table("legacy_time_views_delete_actions"):
        op.drop_table("legacy_time_views_delete_actions")

    # PostTask is last: every remaining row has already been proved terminal and
    # externally archived, including repeat/autodelete payload/error provenance.
    inspector = sa.inspect(bind)
    if inspector.has_table("post_tasks"):
        op.drop_table("post_tasks")


def downgrade() -> None:
    raise RuntimeError(
        "20260919_0015 is intentionally irreversible: recreating retired PostTask "
        "schema would reintroduce an execution identity after durable no-replay cutover"
    )
