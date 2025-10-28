import asyncio
import argparse
from datetime import datetime, timezone
from typing import Optional

from app.core.db import AsyncSessionLocal
from app.domain.models import Channel
from app.services.posting import PostingService


async def schedule_post(tg_chat_id: int, text: str, autodelete_seconds: Optional[int], repeat_seconds: Optional[int]) -> None:
    async with AsyncSessionLocal() as session:
        # Resolve Channel by tg_chat_id
        from sqlalchemy import select
        res = await session.execute(select(Channel).where(Channel.tg_chat_id == tg_chat_id))
        ch = res.scalar_one_or_none()
        if ch is None:
            raise RuntimeError(f"Channel with tg_chat_id={tg_chat_id} not found")

        service = PostingService(None, session)  # Bot not used for schedule()
        payload: dict = {
            "type": "text",
            "text": text,
            "silent": False,
        }
        if autodelete_seconds and autodelete_seconds > 0:
            payload["autodelete_seconds"] = int(autodelete_seconds)
        if repeat_seconds and repeat_seconds > 0:
            payload["repeat_on"] = True
            payload["repeat_seconds"] = int(repeat_seconds)

        when = datetime.now(timezone.utc)
        await service.schedule(int(ch.id), payload, when)
        print("Scheduled.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", type=int, required=True, help="Target tg_chat_id (e.g., -100...)")
    parser.add_argument("--text", type=str, required=True)
    parser.add_argument("--autodelete", type=int, default=0, help="Autodelete in seconds (0 to disable)")
    parser.add_argument("--repeat", type=int, default=0, help="Repeat interval in seconds (0 to disable)")
    args = parser.parse_args()

    asyncio.run(schedule_post(
        int(args.chat),
        args.text,
        int(args.autodelete or 0),
        int(args.repeat or 0),
    ))


if __name__ == "__main__":
    main()


