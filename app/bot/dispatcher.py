import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

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
from app.core.db import AsyncSessionLocal, engine, prepare_db_storage_sync
from app.core.errors import ErrorsMiddleware
from app.core.fsm_storage import build_fsm_storage
from app.core.logging import setup_logging
from app.core.runtime_configuration import validate_runtime_configuration
from app.core.runtime_readiness import runtime_readiness
from app.core.schema import bootstrap_database_schema
from app.services.canonical_publication_delivery_runtime_control import (
    stop_canonical_publication_delivery_workers,
)
from app.services.canonical_publication_safe_repeat_runtime_control import (
    start_canonical_publication_safe_repeat_primary_if_enabled,
)
from app.services.external_bots import ExternalBotsManager
from app.services.llm.openrouter_client import OpenRouterClient
from app.userbot.client import app as userbot
from app.workers.ai_auto_tasks import AIAutoTasksWorker
from app.workers.ai_run_retention import AIRunRetentionWorker
from app.workers.candidate_enrichment import LocalCandidateEnrichmentWorker
from app.workers.canonical_publication_delivery_recovery import (
    CanonicalPublicationDeliveryRecoveryWorker,
)
from app.workers.canonical_repeat_continuation import CanonicalRepeatContinuationWorker
from app.workers.canonical_repeat_time_autodelete import (
    CanonicalRepeatTimeAutodeleteWorker,
)
from app.workers.canonical_repeat_time_forward_autodelete import (
    CanonicalRepeatTimeForwardAutodeleteWorker,
)
from app.workers.canonical_repeat_time_pin_autodelete import (
    CanonicalRepeatTimePinAutodeleteWorker,
)
from app.workers.canonical_repeat_time_pin_forward_autodelete import (
    CanonicalRepeatTimePinForwardAutodeleteWorker,
)
from app.workers.grab_poll import GrabPoller
from app.workers.publication_autodelete import PublicationAutodeleteWorker
from app.workers.publication_autodelete_views_pin_forward import (
    PublicationAutodeleteViewsPinForwardWorker,
)
from app.workers.source_ingestion import SourceIngestionWorker

try:
    import app.userbot.listener  # noqa: F401 ensure userbot handlers are registered
except Exception as exc:
    logger.warning("Userbot listener не загружен: {!r}", exc)


async def create_dispatcher() -> Dispatcher:
    setup_logging(
        settings.log_level,
        file_enabled=settings.log_file_enabled,
        file_retention=settings.log_file_retention,
    )
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


async def _run_polling_with_studio_supervision(
    dp: Dispatcher,
    studio_server: StudioServer,
) -> None:
    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            polling_timeout=50,
        ),
        name="aiogram-polling",
    )
    if not studio_server.enabled:
        await polling_task
        return

    studio_watch = asyncio.create_task(
        studio_server.wait_for_termination(),
        name="studio-api-supervision",
    )
    try:
        done, _ = await asyncio.wait(
            {polling_task, studio_watch},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if studio_watch in done:
            try:
                await studio_watch
            finally:
                if not polling_task.done():
                    polling_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await polling_task
            raise RuntimeError("Studio API supervision ended unexpectedly")
        await polling_task
    finally:
        if not studio_watch.done():
            studio_watch.cancel()
            with suppress(asyncio.CancelledError):
                await studio_watch


async def _start_canonical_repeat_continuation_worker_if_enabled():
    if not settings.canonical_repeat_successful_planning_enabled:
        logger.info("Boot: canonical repeat continuation worker disabled")
        return None

    worker = CanonicalRepeatContinuationWorker(session_factory=AsyncSessionLocal)
    try:
        await worker.start()
    except BaseException:
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "Boot: failed to clean up canonical repeat continuation worker after startup failure"
            )
        raise
    return worker


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
    repeat_continuation_executor_available: bool = False,
    repeat_time_executor_available: bool = False,
    repeat_time_pin_executor_available: bool = False,
    repeat_time_forward_executor_available: bool = False,
    repeat_time_pin_forward_executor_available: bool = False,
    repeat_views_executor_available: bool = False,
    repeat_views_pin_executor_available: bool = False,
    repeat_views_forward_executor_available: bool = False,
    repeat_views_pin_forward_executor_available: bool = False,
):
    recovery_worker = (
        await _start_canonical_publication_delivery_recovery_worker_if_enabled()
    )
    try:
        primary_worker = (
            await start_canonical_publication_safe_repeat_primary_if_enabled(
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
                repeat_continuation_available=(
                    repeat_continuation_executor_available
                ),
                repeat_time_executor_available=repeat_time_executor_available,
                repeat_time_pin_executor_available=(
                    repeat_time_pin_executor_available
                ),
                repeat_time_forward_executor_available=(
                    repeat_time_forward_executor_available
                ),
                repeat_time_pin_forward_executor_available=(
                    repeat_time_pin_forward_executor_available
                ),
                repeat_views_executor_available=repeat_views_executor_available,
                repeat_views_pin_executor_available=(
                    repeat_views_pin_executor_available
                ),
                repeat_views_forward_executor_available=(
                    repeat_views_forward_executor_available
                ),
                repeat_views_pin_forward_executor_available=(
                    repeat_views_pin_forward_executor_available
                ),
                repeat_owner_policy_enforced=True,
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


async def _start_canonical_repeat_time_autodelete_worker_if_enabled(
    *,
    repeat_continuation_available: bool,
):
    if not settings.publication_autodelete_worker_enabled:
        logger.info("Boot: canonical repeat time autodelete worker disabled with time worker")
        return None
    if not repeat_continuation_available:
        logger.info(
            "Boot: canonical repeat time autodelete worker disabled without repeat continuation"
        )
        return None

    worker = CanonicalRepeatTimeAutodeleteWorker(
        provider=bot,
        session_factory=AsyncSessionLocal,
        interval_seconds=settings.publication_autodelete_worker_interval_seconds,
        batch_size=settings.publication_autodelete_worker_batch_size,
        lease_ttl_seconds=settings.publication_autodelete_worker_lease_ttl_seconds,
        allow_repeat_time=True,
    )
    try:
        await worker.start()
    except BaseException:
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "Boot: failed to clean up canonical repeat time autodelete worker after startup failure"
            )
        raise
    return worker


async def _start_canonical_repeat_time_pin_autodelete_worker_if_enabled(
    *,
    repeat_continuation_available: bool,
    repeat_time_available: bool,
):
    if not settings.publication_autodelete_worker_enabled:
        logger.info(
            "Boot: canonical repeat time pin autodelete worker disabled with time worker"
        )
        return None
    if not repeat_continuation_available or not repeat_time_available:
        logger.info(
            "Boot: canonical repeat time pin autodelete worker disabled without exact dependencies"
        )
        return None

    worker = CanonicalRepeatTimePinAutodeleteWorker(
        provider=bot,
        session_factory=AsyncSessionLocal,
        interval_seconds=settings.publication_autodelete_worker_interval_seconds,
        batch_size=settings.publication_autodelete_worker_batch_size,
        lease_ttl_seconds=settings.publication_autodelete_worker_lease_ttl_seconds,
        allow_repeat_time_pin=True,
    )
    try:
        await worker.start()
    except BaseException:
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "Boot: failed to clean up canonical repeat time pin autodelete worker after startup failure"
            )
        raise
    return worker


async def _start_canonical_repeat_time_forward_autodelete_worker_if_enabled(
    *,
    repeat_continuation_available: bool,
    repeat_time_available: bool,
):
    if not settings.publication_autodelete_worker_enabled:
        logger.info(
            "Boot: canonical repeat time forward autodelete worker disabled with time worker"
        )
        return None
    if not repeat_continuation_available or not repeat_time_available:
        logger.info(
            "Boot: canonical repeat time forward autodelete worker disabled without exact dependencies"
        )
        return None

    worker = CanonicalRepeatTimeForwardAutodeleteWorker(
        provider=bot,
        session_factory=AsyncSessionLocal,
        interval_seconds=settings.publication_autodelete_worker_interval_seconds,
        batch_size=settings.publication_autodelete_worker_batch_size,
        lease_ttl_seconds=settings.publication_autodelete_worker_lease_ttl_seconds,
        allow_repeat_time_forward=True,
    )
    try:
        await worker.start()
    except BaseException:
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "Boot: failed to clean up canonical repeat time forward autodelete worker after startup failure"
            )
        raise
    return worker


async def _start_canonical_repeat_time_pin_forward_autodelete_worker_if_enabled(
    *,
    repeat_continuation_available: bool,
    repeat_time_pin_available: bool,
    repeat_time_forward_available: bool,
):
    if not settings.publication_autodelete_worker_enabled:
        logger.info("Boot: combined repeat time autodelete worker disabled with time worker")
        return None
    if not (
        repeat_continuation_available
        and repeat_time_pin_available
        and repeat_time_forward_available
    ):
        logger.info("Boot: combined repeat time autodelete worker lacks exact dependencies")
        return None

    worker = CanonicalRepeatTimePinForwardAutodeleteWorker(
        provider=bot,
        session_factory=AsyncSessionLocal,
        interval_seconds=settings.publication_autodelete_worker_interval_seconds,
        batch_size=settings.publication_autodelete_worker_batch_size,
        lease_ttl_seconds=settings.publication_autodelete_worker_lease_ttl_seconds,
        allow_repeat_time_pin_forward=True,
    )
    try:
        await worker.start()
    except BaseException:
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "Boot: failed to clean up combined repeat time autodelete worker after startup failure"
            )
        raise
    return worker


async def _start_publication_autodelete_views_worker_if_enabled(
    *,
    userbot_available: bool,
    repeat_continuation_available: bool = False,
):
    if not settings.publication_autodelete_views_worker_enabled:
        logger.info("Boot: canonical views autodelete worker disabled")
        return None
    if not userbot_available:
        logger.warning(
            "Boot: canonical views autodelete worker requested but userbot is unavailable"
        )
        return None

    worker = PublicationAutodeleteViewsPinForwardWorker(
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
        allow_repeat_views=bool(repeat_continuation_available),
        allow_repeat_views_pin=True,
        allow_repeat_views_forward=True,
        allow_repeat_views_pin_forward=True,
    )
    try:
        await worker.start()
    except BaseException:
        try:
            await worker.stop()
        except Exception:
            logger.exception(
                "Boot: failed to clean up repeat views combined autodelete worker after startup failure"
            )
        raise
    return worker


async def run_bot() -> None:
    runtime_readiness.mark_not_ready()
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

    prepare_db_storage_sync()
    schema_state = await bootstrap_database_schema(engine)
    logger.info(
        "DB: Alembic managed schema at heads={}",
        ",".join(schema_state.current_heads),
    )

    dp = await create_dispatcher()
    dp.include_router(main_router)

    ext_mgr = ExternalBotsManager()
    studio_server = StudioServer()
    userbot_started = False
    canonical_repeat_continuation = None
    canonical_publication_delivery = None
    canonical_publication_delivery_recovery = None
    publication_autodelete = None
    canonical_repeat_time_autodelete = None
    canonical_repeat_time_pin_autodelete = None
    canonical_repeat_time_forward_autodelete = None
    canonical_repeat_time_pin_forward_autodelete = None
    publication_autodelete_views = None
    source_ingestion = None
    poller = None
    ai_auto_worker = None
    ai_run_retention_worker = None
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

        canonical_repeat_continuation = (
            await _start_canonical_repeat_continuation_worker_if_enabled()
        )

        publication_autodelete = await _start_publication_autodelete_worker_if_enabled()
        continuation_available = canonical_repeat_continuation is not None
        canonical_repeat_time_autodelete = (
            await _start_canonical_repeat_time_autodelete_worker_if_enabled(
                repeat_continuation_available=continuation_available,
            )
        )
        repeat_time_started = bool(
            canonical_repeat_time_autodelete is not None
            and canonical_repeat_time_autodelete.repeat_time_available
        )
        canonical_repeat_time_pin_autodelete = (
            await _start_canonical_repeat_time_pin_autodelete_worker_if_enabled(
                repeat_continuation_available=continuation_available,
                repeat_time_available=repeat_time_started,
            )
        )
        canonical_repeat_time_forward_autodelete = (
            await _start_canonical_repeat_time_forward_autodelete_worker_if_enabled(
                repeat_continuation_available=continuation_available,
                repeat_time_available=repeat_time_started,
            )
        )
        repeat_time_pin_started = bool(
            canonical_repeat_time_pin_autodelete is not None
            and canonical_repeat_time_pin_autodelete.repeat_time_pin_available
        )
        repeat_time_forward_started = bool(
            canonical_repeat_time_forward_autodelete is not None
            and canonical_repeat_time_forward_autodelete.repeat_time_forward_available
        )
        canonical_repeat_time_pin_forward_autodelete = (
            await _start_canonical_repeat_time_pin_forward_autodelete_worker_if_enabled(
                repeat_continuation_available=continuation_available,
                repeat_time_pin_available=repeat_time_pin_started,
                repeat_time_forward_available=repeat_time_forward_started,
            )
        )
        publication_autodelete_views = (
            await _start_publication_autodelete_views_worker_if_enabled(
                userbot_available=userbot_started,
                repeat_continuation_available=continuation_available,
            )
        )

        repeat_time_pin_forward_started = bool(
            canonical_repeat_time_pin_forward_autodelete is not None
            and canonical_repeat_time_pin_forward_autodelete.repeat_time_pin_forward_available
        )
        repeat_views_executor_available = bool(
            publication_autodelete_views is not None
            and publication_autodelete_views.repeat_views_available
        )
        repeat_views_pin_executor_available = bool(
            publication_autodelete_views is not None
            and publication_autodelete_views.repeat_views_pin_available
        )
        repeat_views_forward_executor_available = bool(
            publication_autodelete_views is not None
            and publication_autodelete_views.repeat_views_forward_available
        )
        repeat_views_pin_forward_executor_available = bool(
            publication_autodelete_views is not None
            and publication_autodelete_views.repeat_views_pin_forward_available
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
            repeat_continuation_executor_available=continuation_available,
            repeat_time_executor_available=repeat_time_started,
            repeat_time_pin_executor_available=repeat_time_pin_started,
            repeat_time_forward_executor_available=repeat_time_forward_started,
            repeat_time_pin_forward_executor_available=(
                repeat_time_pin_forward_started
            ),
            repeat_views_executor_available=repeat_views_executor_available,
            repeat_views_pin_executor_available=(
                repeat_views_pin_executor_available
            ),
            repeat_views_forward_executor_available=(
                repeat_views_forward_executor_available
            ),
            repeat_views_pin_forward_executor_available=(
                repeat_views_pin_forward_executor_available
            ),
        )

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

        ai_run_retention_worker = AIRunRetentionWorker(
            session_factory=AsyncSessionLocal,
        )
        ai_run_retention_worker.start()

        await studio_server.start()
        runtime_readiness.mark_ready()

        logger.info("Boot: starting aiogram polling...")
        await _run_polling_with_studio_supervision(dp, studio_server)
    except Exception:
        logger.exception("Bot runtime failed")
        raise
    finally:
        runtime_readiness.mark_not_ready()
        await _safe_stop("Studio API", studio_server.stop)
        if ai_run_retention_worker is not None:
            await _safe_stop(
                "AI run retention worker",
                ai_run_retention_worker.stop,
            )
        if ai_auto_worker is not None:
            await _safe_stop("AI auto tasks worker", ai_auto_worker.stop)
        if poller is not None:
            await _safe_stop("grab poller", poller.stop)
        if local_enrichment_worker is not None:
            await _safe_stop("local enrichment worker", local_enrichment_worker.stop)
        if source_ingestion is not None:
            await _safe_stop("source ingestion", source_ingestion.stop)
        await stop_canonical_publication_delivery_workers(
            primary_worker=canonical_publication_delivery,
            recovery_worker=canonical_publication_delivery_recovery,
        )
        if publication_autodelete_views is not None:
            await _safe_stop(
                "canonical views publication autodelete",
                publication_autodelete_views.stop,
            )
        if canonical_repeat_time_pin_forward_autodelete is not None:
            await _safe_stop(
                "combined repeat time publication autodelete",
                canonical_repeat_time_pin_forward_autodelete.stop,
            )
        if canonical_repeat_time_forward_autodelete is not None:
            await _safe_stop(
                "canonical repeat time forward publication autodelete",
                canonical_repeat_time_forward_autodelete.stop,
            )
        if canonical_repeat_time_pin_autodelete is not None:
            await _safe_stop(
                "canonical repeat time pin publication autodelete",
                canonical_repeat_time_pin_autodelete.stop,
            )
        if canonical_repeat_time_autodelete is not None:
            await _safe_stop(
                "canonical repeat time publication autodelete",
                canonical_repeat_time_autodelete.stop,
            )
        if publication_autodelete is not None:
            await _safe_stop("canonical publication autodelete", publication_autodelete.stop)
        if canonical_repeat_continuation is not None:
            await _safe_stop(
                "canonical repeat continuation",
                canonical_repeat_continuation.stop,
            )
        await _safe_stop("background tasks", cancel_bg_tasks)
        await _safe_stop("external bots", ext_mgr.stop_all)
        await _safe_stop("OpenRouter HTTP pool", OpenRouterClient.close_shared_http_clients)
        await _safe_stop("database engine", engine.dispose)

        if userbot_started:
            await _safe_stop("userbot", userbot.stop)


if __name__ == "__main__":
    asyncio.run(run_bot())
