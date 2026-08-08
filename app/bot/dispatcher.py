import asyncio
from aiogram import Dispatcher
from loguru import logger
from aiogram.fsm.storage.memory import MemoryStorage
from app.core.config import settings
from app.core.logging import setup_logging
from app.bot.bot_instance import bot
from app.core.errors import ErrorsMiddleware
from app.core.channel_access import ChannelOwnerMiddleware
from app.bot.routers import main_router
from app.core.db import AsyncSessionLocal, engine, Base, init_db_if_needed_sync
from app.services.posting import PostingService
from app.userbot.client import app as userbot
from app.core.bg_tasks import cancel_all as cancel_bg_tasks

try:
    import app.userbot.listener  # noqa: F401 ensure userbot handlers are registered
except Exception as e:
    logger.warning(f"Userbot listener не загружен: {e}")

from app.services.external_bots import ExternalBotsManager

try:
    from app.workers.scheduler import Scheduler
except Exception as _e_sched:
    Scheduler = None  # type: ignore
    _scheduler_import_err = _e_sched
else:
    _scheduler_import_err = None

try:
    from app.workers.grab_poll import GrabPoller
except Exception as _e_grab:
    GrabPoller = None  # type: ignore
    _grab_import_err = _e_grab
else:
    _grab_import_err = None

try:
    from app.workers.ai_auto_tasks import AIAutoTasksWorker
except Exception as _e_ai_auto:
    AIAutoTasksWorker = None  # type: ignore
    _ai_auto_import_err = _e_ai_auto
else:
    _ai_auto_import_err = None


async def create_dispatcher() -> Dispatcher:
    setup_logging(settings.log_level)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.message.middleware(ErrorsMiddleware())
    dp.callback_query.middleware(ErrorsMiddleware())
    dp.callback_query.middleware(ChannelOwnerMiddleware())
    return dp


async def _warm_up_userbot_peers() -> None:
    try:
        async for _ in userbot.get_dialogs(limit=200):
            pass
        logger.info("Boot: userbot peers warmed up")
    except Exception as e:
        logger.warning(f"Userbot warm-up skipped: {e}")


async def run_bot() -> None:
    setup_logging(settings.log_level)
    try:
        from app.core.db import engine as _eng

        pool_name = getattr(getattr(_eng, "sync_engine", None), "pool", None)
        logger.info(
            f"DB: engine initialized, pool={type(pool_name).__name__ if pool_name else 'unknown'} staticpool={getattr(settings, 'sqla_staticpool', False)} nullpool={getattr(settings, 'sqla_nullpool', False)}"
        )
    except Exception:
        pass

    init_db_if_needed_sync()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    dp = await create_dispatcher()
    dp.include_router(main_router)

    ext_mgr = ExternalBotsManager()
    await ext_mgr.start_all()

    logger.info("Boot: starting userbot...")
    userbot_started = False
    try:
        await userbot.start()
        userbot_started = True
        logger.info("Boot: userbot started")
        await _warm_up_userbot_peers()
    except Exception as e:
        logger.warning(
            f"Userbot не запущен: {e}. Клонирование временно отключено. Выполните авторизацию userbot."
        )

    try:
        logger.info("Boot: creating services...")
        config_models = settings.get_ai_models_config()
        if config_models is not None:
            logger.info(f"Boot: AI models config loaded: {len(config_models)} entries")
        if Scheduler is None and _scheduler_import_err is not None:
            logger.warning(f"Scheduler не загружен: {_scheduler_import_err}")
        if GrabPoller is None and _grab_import_err is not None:
            logger.warning(f"GrabPoller не загружен: {_grab_import_err}")
        if AIAutoTasksWorker is None and _ai_auto_import_err is not None:
            logger.warning(f"AI auto tasks worker не загружен: {_ai_auto_import_err}")

        posting = PostingService(bot, AsyncSessionLocal)
        scheduler = None
        if Scheduler is not None:
            scheduler = Scheduler(AsyncSessionLocal, posting)
            await scheduler.start()

        poller = None
        if GrabPoller is not None:
            poller = GrabPoller(interval_seconds=5)
            await poller.start()

        ai_auto_worker = None
        if AIAutoTasksWorker is not None:
            ai_auto_worker = AIAutoTasksWorker()
            await ai_auto_worker.start()

        try:
            logger.info("Boot: starting aiogram polling...")
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                polling_timeout=50,
            )
        except Exception as e:
            from aiogram.exceptions import TelegramNetworkError

            if not isinstance(e, TelegramNetworkError):
                raise
        finally:
            if ai_auto_worker is not None:
                logger.info("Boot: stopping AI auto tasks worker...")
                await ai_auto_worker.stop()
            if poller is not None:
                logger.info("Boot: stopping grab poller...")
                await poller.stop()
            if scheduler is not None:
                logger.info("Boot: stopping scheduler...")
                await scheduler.stop()
            try:
                await cancel_bg_tasks()
            except Exception:
                pass
    finally:
        try:
            await ext_mgr.stop_all()
        except Exception:
            pass
        try:
            await engine.dispose()
        except Exception:
            pass
        if userbot_started:
            try:
                logger.info("Boot: stopping userbot...")
                await userbot.stop()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(run_bot())
