from app.bot.routers.shared_join import build_shared_join_router
from aiogram import Router


def build_router_for_external_bot(external_bot_id: int) -> Router:
    # Делегируем создание и обработчики в общий модуль
    return build_shared_join_router(external_bot_id)
