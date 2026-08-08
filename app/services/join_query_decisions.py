from __future__ import annotations

from typing import Literal

from aiogram.types import ChatJoinRequest
from loguru import logger

JoinQueryResult = Literal["approve", "decline", "queue"]


async def answer_join_query(event: ChatJoinRequest, result: JoinQueryResult) -> bool:
    """Answer Bot API 10.1 join-request query when Telegram supplied query_id.

    Returns True only when the query was actually answered. Legacy join requests
    have no query_id and intentionally return False so callers can use the
    traditional approve/decline API or continue their existing challenge flow.
    """
    query_id = getattr(event, "query_id", None)
    if not query_id:
        return False

    try:
        await event.bot.answer_chat_join_request_query(
            chat_join_request_query_id=str(query_id),
            result=result,
        )
        return True
    except Exception:
        logger.exception(
            "Join request query answer failed result={} user_id={} chat_id={}",
            result,
            getattr(getattr(event, "from_user", None), "id", None),
            getattr(getattr(event, "chat", None), "id", None),
        )
        return False


async def resolve_immediate_join_decision(
    event: ChatJoinRequest,
    result: Literal["approve", "decline"],
) -> bool:
    """Resolve an immediate decision through the new query API or legacy API."""
    if await answer_join_query(event, result):
        return True

    chat_id = int(getattr(getattr(event, "chat", None), "id", 0) or 0)
    user_id = int(getattr(getattr(event, "from_user", None), "id", 0) or 0)
    if not chat_id or not user_id:
        return False

    try:
        if result == "approve":
            await event.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        else:
            await event.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        return True
    except Exception:
        logger.exception(
            "Legacy join decision failed result={} user_id={} chat_id={}",
            result,
            user_id,
            chat_id,
        )
        return False
