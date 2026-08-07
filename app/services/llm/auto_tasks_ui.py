"""UI helpers for AI auto-tasks settings screen."""

from __future__ import annotations

from app.workers.ai_auto_tasks import ALL_TASK_TYPES, TASK_LABELS, TASK_DEFAULT_TIMES


def build_auto_tasks_menu_text(tasks_enabled: dict[str, bool]) -> str:
    """Build the auto-tasks settings menu text."""
    lines = ["⏰ **Автозадачи**\n"]
    lines.append("ИИ будет автоматически выполнять задачи по расписанию:\n")

    for task_type in ALL_TASK_TYPES:
        label = TASK_LABELS.get(task_type, task_type)
        enabled = tasks_enabled.get(task_type, False)
        status = "✅ вкл" if enabled else "☑️ выкл"
        default_time = TASK_DEFAULT_TIMES.get(task_type, "10:00")
        lines.append(f"  {status} {label} — {default_time}")

    lines.append("")
    lines.append("Нажмите на задачу, чтобы включить/выключить.")
    return "\n".join(lines)


def build_auto_tasks_button_rows(
    tasks_enabled: dict[str, bool],
    cid: int,
) -> list[list[str]]:
    """Return (text, callback_data) pairs for auto-tasks menu buttons."""
    rows: list[list[tuple[str, str]]] = []
    for task_type in ALL_TASK_TYPES:
        label = TASK_LABELS.get(task_type, task_type)
        enabled = tasks_enabled.get(task_type, False)
        status = "✅" if enabled else "☑️"
        rows.append((
            f"{status} {label}",
            f"ai_auto_toggle_{task_type}_{cid}",
        ))
    rows.append(("← Назад", f"neu_text_{cid}"))
    return rows
