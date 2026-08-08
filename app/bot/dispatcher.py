import asyncio
from collections.abc import Awaitable, Callable

from aiogram import Dispatcher
from loguru import logger

from app.api.studio.server import StudioServer
from app.bot.bot_instance import bot
from app.bot.commands import register_bot_commands
from app.bot.routers import main_router
from app.core.bg_tasks import cancel_all as cancel_bg_tasks
from app.core.channel_access import ChannelOwnerMiddleware, ChannelOwnerStateMiddleware
from app.core.config import settings
from app.core.db import AsyncSessionLocal, Base, engine, init_db_if_needed_sync
from app.core.errors import ErrorsMiddleware
from app.core.fsm_storage import build_fsm_storage
from app.core.logging import setup_logging
from app.services.document_posting import DocumentPostingService
from app.services.external_bots import ExternalBotsManager
from app.services.llm.openrouter_client import OpenRouterClient
from app.userbot.client import app as userbot
from app.workers.ai_auto_tasks import AIAutoTasksWorker
from app.workers.grab_poll import GrabPoller
from app.workers.publication_reconciler import PublicationReconcilerWorker
from app.workers.reliable_scheduler import Scheduler

try:
    import app.userbot.listener  # noqa: F401 ensure userbot handlers are registered
except Exception as exc:
    logger.warning("Userbot listener не загружен: {!r}", exc)


async def create_dispatcher() -> Dispatcher:
    setup_logging(settings.log_level)
    dp = Dispatcher(storage=build_fsm_storage())
    dp.message.middleware(ErrorsMiddleware())
    dp.message.middleware(ChannelOwnerStateMiddleware())
    dp.callback_query.middleware(ErrorsMiddleware())
    dp.callback_query.middleware(ChannelOwnerMiddleware())
    return dp


async def _warm_up_userbot_peers() -> None:
    try:
        async for _ in userbot.get_dialogs(limit=200):
            pass
        logger.info("Boot: userbot peers warmed up")
    except Exception as exc:
        logger.warning("Userbot warm-up skipped: {!r}", exc)


async def _safe_stop(name: str, stop: Callable[[], Awaitable[None]]) -> None:
    try:
        await stop()
    except Exception:
        logger.exception("Shutdown: failed to stop {}", name)


async def run_bot() -> None:
    setup_logging(settings.log_level)

    pool_name = getattr(getattr(engine, "sync_engine", None), "pool", None)
    logger.info(
        "DB: engine initialized, pool={} staticpool={} nullpool={}",
        type(pool_name).__name__ if pool_name else "unknown",
        getattr(settings, "sqla_staticpool", False),
        getattr(settings, "sqla_nullpool", False),
    )

    init_db_if_needed_sync()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    dp = await create_dispatcher()
    dp.include_router(main_router)

    ext_mgr = ExternalBotsManager()
    studio_server = StudioServer()
    userbot_started = False
    scheduler = None
    publication_reconciler = None
    poller = None
    ai_auto_worker = None

    try:
        await register_bot_commands(bot)
        await ext_mgr.start_all()

        logger.info("Boot: starting userbot...")
        try:
            await userbot.start()
            userbot_started = True
            logger.info("Boot: userbot started")
            await _warm_up_userbot_peers()
        except Exception as exc:
            logger.warning(
                "Userbot не запущен: {!r}. Клонирование временно отключено. "
                "Выполните авторизацию userbot.",
                exc,
            )

        logger.info("Boot: creating services...")
        config_models = settings.get_ai_models_config()
        if config_models is not None:
            logger.info("Boot: AI models config loaded: {} entries", len(config_models))

        posting = DocumentPostingService(bot, AsyncSessionLocal)

        scheduler = Scheduler(AsyncSessionLocal, posting)
        await scheduler.start()

        publication_reconciler = PublicationReconcilerWorker(interval_seconds=5)
        await publication_reconciler.start()

        poller = GrabPoller(interval_seconds=5)
        await poller.start()

        ai_auto_worker = AIAutoTasksWorker()
        await ai_auto_worker.start()

        await studio_server.start()

        logger.info("Boot: starting aiogram polling...")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            polling_timeout=50,
        )
    except Exception:
        logger.exception("Bot runtime failed")
        raise
    finally:
        await _safe_stop("Studio API", studio_server.stop)
        if ai_auto_worker is not None:
            await _safe_stop("AI auto tasks worker", ai_auto_worker.stop)
        if poller is not None:
            await _safe_stop("grab poller", poller.stop)
        if publication_reconciler is not None:
            await _safe_stop("publication reconciler", publication_reconciler.stop)
        if scheduler is not None:
            await _safe_stop("scheduler", scheduler.stop)

        await _safe_stop("background tasks", cancel_bg_tasks)
        await _safe_stop("external bots", ext_mgr.stop_all)
        await _safe_stop("OpenRouter HTTP pool", OpenRouterClient.close_shared_http_clients)
        await _safe_stop("database engine", engine.dispose)

        if userbot_started:
            await _safe_stop("userbot", userbot.stop)


if __name__ == "__main__":
    asyncio.run(run_bot())
