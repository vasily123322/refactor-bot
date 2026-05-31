import asyncio
from aiogram import Dispatcher
from loguru import logger
from aiogram.fsm.storage.memory import MemoryStorage
from app.core.config import settings
from app.core.logging import setup_logging
from app.bot.bot_instance import bot
from app.core.errors import ErrorsMiddleware
from app.bot.routers import main_router
from app.core.db import AsyncSessionLocal, engine, Base, init_db_if_needed_sync
from app.services.posting import PostingService
from app.userbot.client import app as userbot
from app.core.bg_tasks import cancel_all as cancel_bg_tasks

try:
    import app.userbot.listener  # noqa: F401 ensure userbot handlers are registered
except Exception as e:
    logger.warning(f"Userbot listener не загружен: {e}")

# NEW
from app.services.external_bots import ExternalBotsManager

# Optional workers (модули могут отсутствовать в сборке)
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


async def create_dispatcher() -> Dispatcher:
    setup_logging(settings.log_level)
    # Всегда используем память, чтобы избежать сбоев без Redis
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    # Глобальный error middleware
    dp.message.middleware(ErrorsMiddleware())
    dp.callback_query.middleware(ErrorsMiddleware())
    return dp


async def _warm_up_userbot_peers() -> None:
    try:
        # Прогружаем список диалогов для заполнения peer-кэша (исправляет "Peer id invalid")
        async for _ in userbot.get_dialogs(limit=200):
            pass
        logger.info("Boot: userbot peers warmed up")
    except Exception as e:
        logger.warning(f"Userbot warm-up skipped: {e}")


async def run_bot() -> None:
    # init logging before any Boot messages
    setup_logging(settings.log_level)
    try:
        from app.core.db import engine as _eng

        pool_name = getattr(getattr(_eng, "sync_engine", None), "pool", None)
        logger.info(
            f"DB: engine initialized, pool={type(pool_name).__name__ if pool_name else 'unknown'} staticpool={getattr(settings, 'sqla_staticpool', False)} nullpool={getattr(settings, 'sqla_nullpool', False)}"
        )
    except Exception:
        pass
    # auto-init sqlite and backup legacy schema
    init_db_if_needed_sync()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    dp = await create_dispatcher()
    dp.include_router(main_router)

    # NEW: внешние боты (приём заявок)
    ext_mgr = ExternalBotsManager()
    await ext_mgr.start_all()

    # Стартуем userbot раньше, чтобы исключить проблемы с event loop при остановке
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
        if (
            Scheduler is None
            and "_scheduler_import_err" in globals()
            and _scheduler_import_err is not None
        ):
            logger.warning(f"Scheduler не загружен: {_scheduler_import_err}")
        if (
            GrabPoller is None
            and "_grab_import_err" in globals()
            and _grab_import_err is not None
        ):
            logger.warning(f"GrabPoller не загружен: {_grab_import_err}")
        # Передаём фабрику сессий в PostingService, чтобы он создавал короткие сессии
        posting = PostingService(bot, AsyncSessionLocal)
        scheduler = None
        if Scheduler is not None:
            # Передаём фабрику в шедулер
            scheduler = Scheduler(AsyncSessionLocal, posting)
            await scheduler.start()
        poller = None
        if GrabPoller is not None:
            poller = GrabPoller(interval_seconds=5)
            await poller.start()
        try:
            logger.info("Boot: starting aiogram polling...")
            # Уменьшаем количество запросов getUpdates и нагрузку: long polling + только используемые типы
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                polling_timeout=50,
            )
        except Exception as e:
            # На остановке aiohttp может ронять ServerDisconnectedError — не шумим
            from aiogram.exceptions import TelegramNetworkError

            if not isinstance(e, TelegramNetworkError):
                raise
        finally:
            if poller is not None:
                logger.info("Boot: stopping grab poller...")
                await poller.stop()
            if scheduler is not None:
                logger.info("Boot: stopping scheduler...")
                await scheduler.stop()
            # Отмена всех фоновых задач (удаления и т.п.) для корректного завершения
            try:
                await cancel_bg_tasks()
            except Exception:
                pass
    finally:
        # NEW: останавливаем внешние боты
        try:
            await ext_mgr.stop_all()
        except Exception:
            pass
        # Корректно закрыть пул соединений
        try:
            await engine.dispose()
        except Exception:
            pass
        # Останавливаем userbot в самом конце, если он был запущен
        if userbot_started:
            try:
                logger.info("Boot: stopping userbot...")
                await userbot.stop()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(run_bot())
