from aiogram import Router
from .main import router as main_commands
from .start import router as start_commands
from .chats import router as chats_commands
from .post_editor import router as post_editor_commands
from .posting_publish import router as posting_publish_commands
from .content_plan import router as content_plan_commands
from .settings import router as settings_commands
from .admin import router as admin_commands
from .tz import router as tz_commands
from . import sources as sources_module
from .moderation import router as moderation_commands
from app.core.settings_channel_access import SettingsChannelOwnerMiddleware
from app.services.source_fetch import fetch_public_source_text

# sources.py still contains a legacy aiohttp helper. Bind the runtime digest path to
# the central SSRF-safe fetcher without rewriting the large router module.
sources_module._fetch_url_source_text = fetch_public_source_text
sources_commands = sources_module.router

# Реестр роутеров: start/menu отдельно, остальное в main.py
main_router = Router()
main_router.callback_query.outer_middleware(SettingsChannelOwnerMiddleware())
main_router.include_router(start_commands)
main_router.include_router(chats_commands)
main_router.include_router(post_editor_commands)
main_router.include_router(posting_publish_commands)
main_router.include_router(content_plan_commands)
main_router.include_router(settings_commands)
main_router.include_router(admin_commands)
main_router.include_router(tz_commands)
main_router.include_router(sources_commands)
main_router.include_router(moderation_commands)
main_router.include_router(main_commands)
