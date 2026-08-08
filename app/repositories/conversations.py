from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AIConversation, AIConversationMessage


_MAX_PROMPT_KEY_LEN = 64


def scoped_prompt_key(prompt_key: str, channel_id: int | None) -> str:
    """Return a channel-scoped key while staying within the DB column limit."""
    key = str(prompt_key)
    if channel_id is None:
        return key[:_MAX_PROMPT_KEY_LEN]

    prefix = f"ch:{int(channel_id)}:"
    candidate = f"{prefix}{key}"
    if len(candidate) <= _MAX_PROMPT_KEY_LEN:
        return candidate

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    room = _MAX_PROMPT_KEY_LEN - len(prefix) - len(digest) - 1
    return f"{prefix}{key[:max(0, room)]}:{digest}"


class ConversationsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(
        self, user_id: int, prompt_key: str, channel_id: int | None = None
    ) -> int:
        effective_key = scoped_prompt_key(prompt_key, channel_id)
        stmt = select(AIConversation).where(
            AIConversation.user_id == user_id,
            AIConversation.prompt_key == effective_key,
        )
        res = await self.session.execute(stmt)
        conv = res.scalar_one_or_none()
        if conv:
            return int(conv.id)

        # Backward compatibility: reuse an old unscoped conversation only when it
        # already belongs to the same channel. Never reuse another channel's row.
        if channel_id is not None and effective_key != prompt_key:
            legacy_stmt = select(AIConversation).where(
                AIConversation.user_id == user_id,
                AIConversation.prompt_key == prompt_key,
                AIConversation.channel_id == channel_id,
            )
            legacy_res = await self.session.execute(legacy_stmt)
            legacy = legacy_res.scalar_one_or_none()
            if legacy:
                return int(legacy.id)

        conv = AIConversation(
            user_id=user_id,
            prompt_key=effective_key,
            channel_id=channel_id,
        )
        self.session.add(conv)
        await self.session.flush()
        return int(conv.id)

    async def list_recent_by_tokens(
        self, conversation_id: int, max_tokens: int
    ) -> list[dict]:
        stmt = (
            select(AIConversationMessage)
            .where(AIConversationMessage.conversation_id == conversation_id)
            .order_by(AIConversationMessage.created_at.desc())
        )
        res = await self.session.execute(stmt)
        rows = list(res.scalars())
        total = 0
        selected: list[AIConversationMessage] = []
        for m in rows:
            if total + int(m.tokens or 0) > max_tokens:
                break
            selected.append(m)
            total += int(m.tokens or 0)
        selected = list(reversed(selected))
        return [{"role": m.role, "content": m.content} for m in selected]

    async def append(
        self,
        conversation_id: int,
        role: str,
        content: str,
        tokens: int = 0,
        meta: dict | None = None,
    ) -> None:
        msg = AIConversationMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tokens=int(tokens or 0),
            meta=meta or None,
        )
        self.session.add(msg)
        await self.session.flush()

    async def get_summary(self, conversation_id: int) -> tuple[Optional[str], int]:
        stmt = select(AIConversation).where(AIConversation.id == conversation_id)
        res = await self.session.execute(stmt)
        conv = res.scalar_one_or_none()
        if not conv:
            return None, 0
        return conv.last_summary, int(conv.summary_tokens or 0)

    async def upsert_summary(
        self, conversation_id: int, content: str, tokens: int
    ) -> None:
        stmt = select(AIConversation).where(AIConversation.id == conversation_id)
        res = await self.session.execute(stmt)
        conv = res.scalar_one_or_none()
        if not conv:
            return
        conv.last_summary = content
        conv.summary_tokens = int(tokens or 0)
        await self.session.flush()

    async def delete_all_by_user(self, user_id: int) -> int:
        res = await self.session.execute(
            select(AIConversation).where(AIConversation.user_id == user_id)
        )
        rows = list(res.scalars().all())
        count = len(rows)
        for row in rows:
            await self.session.delete(row)
        await self.session.commit()
        return count

    async def delete_by_user_and_prompt_key(
        self,
        user_id: int,
        prompt_key: str,
        channel_id: int | None = None,
    ) -> int:
        """Delete legacy/scoped conversations matching the logical prompt key."""
        stmt = select(AIConversation).where(AIConversation.user_id == user_id)
        if channel_id is not None:
            stmt = stmt.where(AIConversation.channel_id == channel_id)
        res = await self.session.execute(stmt)
        rows = list(res.scalars().all())

        matches = [
            row
            for row in rows
            if row.prompt_key == str(prompt_key)
            or row.prompt_key == scoped_prompt_key(prompt_key, row.channel_id)
        ]
        for row in matches:
            await self.session.delete(row)
        if matches:
            await self.session.commit()
        return len(matches)

    async def delete_all_by_channel(self, channel_id: int) -> int:
        res = await self.session.execute(
            select(AIConversation).where(AIConversation.channel_id == channel_id)
        )
        rows = list(res.scalars().all())
        count = len(rows)
        for row in rows:
            await self.session.delete(row)
        await self.session.commit()
        return count
