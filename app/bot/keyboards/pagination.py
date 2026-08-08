from __future__ import annotations

import math
from typing import TypeVar

from aiogram.types import InlineKeyboardButton


T = TypeVar("T")


def paginate(items: list[T], page: int, *, page_size: int) -> tuple[list[T], int, int]:
    """Return one clamped page, its zero-based index, and total page count."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    total_pages = max(1, math.ceil(len(items) / page_size))
    safe_page = min(max(0, int(page)), total_pages - 1)
    start = safe_page * page_size
    return items[start : start + page_size], safe_page, total_pages


def page_nav_row(
    *,
    prefix: str,
    page: int,
    total_pages: int,
    noop_callback: str,
) -> list[InlineKeyboardButton] | None:
    """Build compact previous / position / next navigation for inline browsers."""
    if total_pages <= 1:
        return None
    previous = (
        InlineKeyboardButton(text="‹", callback_data=f"{prefix}:{page - 1}")
        if page > 0
        else InlineKeyboardButton(text="·", callback_data=noop_callback)
    )
    position = InlineKeyboardButton(
        text=f"{page + 1} / {total_pages}", callback_data=noop_callback
    )
    next_button = (
        InlineKeyboardButton(text="›", callback_data=f"{prefix}:{page + 1}")
        if page < total_pages - 1
        else InlineKeyboardButton(text="·", callback_data=noop_callback)
    )
    return [previous, position, next_button]
