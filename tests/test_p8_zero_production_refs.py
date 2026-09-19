from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
_FORBIDDEN_TABLE_LITERALS = {
    "post_tasks",
    "scheduler_task_leases",
    "legacy_time_views_delete_actions",
}


def _python_sources():
    yield from sorted(APP.rglob("*.py"))


def test_p8_production_has_no_dropped_orm_or_sql_dependency() -> None:
    failures: list[str] = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        relative = path.relative_to(ROOT)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported = {alias.name for alias in node.names}
                if module == "app.domain.models" and "PostTask" in imported:
                    failures.append(f"{relative}:{node.lineno}: imports PostTask")
                if module == "app.domain.scheduler":
                    failures.append(
                        f"{relative}:{node.lineno}: imports retired scheduler ORM"
                    )
                if module == "app.domain.legacy_time_views_delete_action":
                    failures.append(
                        f"{relative}:{node.lineno}: imports retired destructive ledger ORM"
                    )

            if isinstance(node, ast.Attribute) and node.attr == "legacy_post_task_id":
                failures.append(
                    f"{relative}:{node.lineno}: touches Publication.legacy_post_task_id"
                )

            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                for table_name in _FORBIDDEN_TABLE_LITERALS:
                    if table_name in value:
                        failures.append(
                            f"{relative}:{node.lineno}: references dropped table {table_name}"
                        )

    assert failures == [], "\n".join(failures)
