import asyncio
from collections.abc import Awaitable, Callable

from aiogram import Dispatcher
from loguru import logger

import app.domain  # noqa: F401 register complete ORM metadata before schema bootstrap
from app.api.studio.server import StudioServer
from app.bot.bot_instance import bot
from app.bot.commands import register_bot_commands
from app.bot.routers import main_router
from app.core.bg_tasks import cancel_all as cancel_bg_tasks
from app.core.canonical_publication_delivery_primary_config import (
    CanonicalPublicationDeliveryPrimarySettings,
    load_canonical_publication_delivery_primary_settings,
)
from app.core.channel_access import ChannelOwnerMiddleware, ChannelOwnerStateMiddleware
from app.core.config import settings
from app.core.db import (
    AsyncSessionLocal,
    Base,
    engine,
    init_db_if_needed_sync,
    prepare_db_storage_sync,
)
from app.core.errors import ErrorsMiddleware
from app.core.fsm_storage import build_fsm_storage
from app.core.logging import setup_logging
from app.core.runtime_configuration import (
    validate_retention_executor_availability,
    validate_runtime_configuration,
)
from app.core.schema import bootstrap_database_schema
from app.services.canonical_publication_delivery_runtime_control import (
    start_canonical_publication_delivery_primary_if_enabled,
    stop_canonical_publication_delivery_workers,
)
from app.services.document_posting import DocumentPostingService as PostingService
from app.services.external_bots import ExternalBotsManager
from app.services.llm.openrouter_client import OpenRouterClient
from app.userbot.client import app as userbot
from app.workers.ai_auto_tasks import AIAutoTasksWorker
from app.workers.candidate_enrichment import LocalCandidateEnrichmentWorker
from app.workers.canonical_publication_delivery_recovery import (
    CanonicalPublicationDeliveryRecoveryWorker,
)
from app.workers.canonical_repeat_continuation_scheduler import Scheduler
from app.workers.grab_poll import GrabPoller
from app.workers.post_task_retention import PostTaskRetentionWorker
from app.workers.publication_autodelete import PublicationAutodeleteWorker
from app.workers.publication_autodelete_views import PublicationAutodeleteViewsWorker
from app.workers.publication_reconciler import PublicationReconcilerWorker
from app.workers.scheduler_recovery import SchedulerRecoveryWorker
from app.workers.source_ingestion import SourceIngestionWorker

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


async def _legacy_schema_bootstrap() -> None:
    init_db_if_needed_sync()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _start_canonical_publication_delivery_recovery_worker_if_enabled():
    if not settings.canonical_publication_delivery_recovery_worker_enabled:
        logger.info("Boot: canonical publication delivery recovery worker disabled")
        return None

    worker = CanonicalPublicationDeliveryRecoveryWorker(
        session_factory=AsyncSessionLocal,
        interval_seconds=(
            settings.canonical_publication_delivery_recovery_worker_interval_seconds
        ),
        batch_size=settings.canonical_publication_delivery_recovery_worker_batch_size,
    )
    try:
        await worker.start()
    except BaseException:
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "Boot: failed to clean up canonical publication delivery recovery worker after startup failure"
            )
        raise
    return worker


async def _start_canonical_publication_delivery_workers(
    primary_config: CanonicalPublicationDeliveryPrimarySettings,
    *,
    time_autodelete_executor_available: bool = False,
    views_autodelete_executor_available: bool = False,
):
    recovery_worker = (
        await _start_canonical_publication_delivery_recovery_worker_if_enabled()
    )
    try:
        primary_worker = (
            await start_canonical_publication_delivery_primary_if_enabled(
                config=primary_config,
                recovery_worker=recovery_worker,
                bot=bot,
                session_factory=AsyncSessionLocal,
                time_autodelete_executor_available=(
                    time_autodelete_executor_available
                ),
                views_autodelete_executor_available=(
                    views_autodelete_executor_available
                ),
            )
        )
    except BaseException:
        await stop_canonical_publication_delivery_workers(
            primary_worker=None,
            recovery_worker=recovery_worker,
        )
        raise
    return primary_worker, recovery_worker


async def _start_publication_autodelete_worker_if_enabled():
    if not settings.publication_autodelete_worker_enabled:
        logger.info("Boot: canonical publication autodelete worker disabled")
        return None

    worker = PublicationAutodeleteWorker(
        provider=bot,
        session_factory=AsyncSessionLocal,
        interval_seconds=settings.publication_autodelete_worker_interval_seconds,
        batch_size=settings.publication_autodelete_worker_batch_size,
        lease_ttl_seconds=settings.publication_autodelete_worker_lease_ttl_seconds,
    )
    await worker.start()
    return worker


async def _start_publication_autodelete_views_worker_if_enabled(
    *,
    userbot_available: bool,
):
    if not settings.publication_autodelete_views_worker_enabled:
        logger.info("Boot: canonical views autodelete worker disabled")
        return None
    if not userbot_available:
        logger.warning(
            "Boot: canonical views autodelete worker requested but userbot is unavailable"
        )
        return None

    worker = PublicationAutodeleteViewsWorker(
        view_source=userbot,
        delete_provider=bot,
        session_factory=AsyncSessionLocal,
        interval_seconds=settings.publication_autodelete_views_worker_interval_seconds,
        batch_size=settings.publication_autodelete_views_worker_batch_size,
        lease_ttl_seconds=(
            settings.publication_autodelete_views_worker_lease_ttl_seconds
        ),
        next_check_seconds=settings.publication_autodelete_views_worker_next_check_seconds,
        ineligible_backoff_seconds=(
            settings.publication_autodelete_views_worker_ineligible_backoff_seconds
        ),
    )
    await worker.start()
    return worker


async def run_bot() -> None:
    setup_logging(settings.log_level)
    primary_delivery_config = load_canonical_publication_delivery_primary_settings()
    validate_runtime_configuration(settings)

    pool_name = getattr(getattr(engine, "sync_engine", None), "pool", None)
    logger.info(
        "DB: engine initialized, pool={} staticpool={} nullpool={}",
        type(pool_name).__name__ if pool_name else "unknown",
        getattr(settings, "sqla_staticpool", False),
        getattr(settings, "sqla_nullpool", False),
    )

    # Ensure a relative SQLite directory can be opened before inspecting whether the
    # database has adopted Alembic. This step never mutates schema.
    prepare_db_storage_sync()
    schema_state = await bootstrap_database_schema(
        engine,
        unmanaged_initializer=_legacy_schema_bootstrap,
    )
    if schema_state.managed:
        logger.info(
            "DB: Alembic managed schema at heads={}",
            ",".join(schema_state.current_heads),
        )
    else:
        logger.warning(
            "DB: legacy unmanaged schema bootstrap active; run `alembic upgrade head` to adopt managed migrations"
        )

    dp = await create_dispatcher()
    dp.include_router(main_router)

    ext_mgr = ExternalBotsManager()
    studio_server = StudioServer()
    userbot_started = False
    scheduler = None
    scheduler_recovery = None
    canonical_publication_delivery = None
    canonical_publication_delivery_recovery = None
    publication_reconciler = None
    publication_autodelete = None
    publication_autodelete_views = None
    post_task_retention = None
    source_ingestion = None
    poller = None
    ai_auto_worker = None
    local_enrichment_worker = None

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

        # Keep the historical monkeypatch seam name while the concrete implementation
        # is the PostDocument-aware service used by rich and classic publications.
        posting = PostingService(bot, AsyncSessionLocal)

        # This wrapper preserves the historical Scheduler behavior and, under the
        # existing successful-repeat opt-in, starts the provider-free continuation
        # recovery worker before canonical primary delivery can start.
        scheduler = Scheduler(AsyncSessionLocal, posting)
        await scheduler.start()

        scheduler_recovery = SchedulerRecoveryWorker(
            session_factory=AsyncSessionLocal,
            interval_seconds=60,
            batch_size=100,
        )
        await scheduler_recovery.start()

        publication_reconciler = PublicationReconcilerWorker(interval_seconds=5)
        await publication_reconciler.start()

        # Start optional destructive consumers before any delete-capable canonical
        # primary loop. Availability below is based on successful start, never config.
        publication_autodelete = await _start_publication_autodelete_worker_if_enabled()
        publication_autodelete_views = (
            await _start_publication_autodelete_views_worker_if_enabled(
                userbot_available=userbot_started,
            )
        )

        # Run all executor-availability interlocks before canonical primary delivery is
        # allowed to execute provider side effects.
        validate_retention_executor_availability(
            settings,
            publication_autodelete_worker_started=publication_autodelete is not None,
            publication_autodelete_views_worker_started=(
                publication_autodelete_views is not None
            ),
        )

        (
            canonical_publication_delivery,
            canonical_publication_delivery_recovery,
        ) = await _start_canonical_publication_delivery_workers(
            primary_delivery_config,
            time_autodelete_executor_available=publication_autodelete is not None,
            views_autodelete_executor_available=(
                publication_autodelete_views is not None
            ),
        )

        if settings.post_task_retention_enabled:
            post_task_retention = PostTaskRetentionWorker(
                session_factory=AsyncSessionLocal,
                interval_seconds=settings.post_task_retention_interval_seconds,
                retention_days=settings.post_task_retention_days,
                batch_size=settings.post_task_retention_batch_size,
                retire_successful=settings.post_task_retention_successful_enabled,
                retire_successful_pending_autodelete=(
                    settings.post_task_retention_successful_pending_autodelete_enabled
                ),
                retire_successful_repeat_occurrences=(
                    settings.post_task_retention_successful_repeat_occurrences_enabled
                ),
            )
            await post_task_retention.start()
        else:
            logger.info("Boot: PostTask retention worker disabled")

        source_ingestion = SourceIngestionWorker(
            interval_seconds=60,
            session_factory=AsyncSessionLocal,
        )
        await source_ingestion.start()

        if settings.local_enrichment_worker_enabled:
            local_enrichment_worker = LocalCandidateEnrichmentWorker(
                session_factory=AsyncSessionLocal,
                interval_seconds=settings.local_enrichment_worker_interval_seconds,
                batch_size=settings.local_enrichment_worker_batch_size,
                candidate_timeout_seconds=(
                    settings.local_enrichment_worker_candidate_timeout_seconds
                ),
            )
            await local_enrichment_worker.start()
        else:
            logger.info("Boot: local enrichment worker disabled")

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
        if local_enrichment_worker is not None:
            await _safe_stop("local enrichment worker", local_enrichment_worker.stop)
        if source_ingestion is not None:
            await _safe_stop("source ingestion", source_ingestion.stop)
        if post_task_retention is not None:
            await _safe_stop("PostTask retention", post_task_retention.stop)

        # Stop the canonical producer/recovery pair before dependent delete consumers and
        # before the scheduler wrapper stops repeat continuation recovery.
        await stop_canonical_publication_delivery_workers(
            primary_worker=canonical_publication_delivery,
            recovery_worker=canonical_publication_delivery_recovery,
        )
        if publication_autodelete_views is not None:
            await _safe_stop(
                "canonical views publication autodelete",
                publication_autodelete_views.stop,
            )
        if publication_autodelete is not None:
            await _safe_stop("canonical publication autodelete", publication_autodelete.stop)
        if publication_reconciler is not None:
            await _safe_stop("publication reconciler", publication_reconciler.stop)
        if scheduler_recovery is not None:
            await _safe_stop("scheduler recovery", scheduler_recovery.stop)
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
