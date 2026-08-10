from aiogram import Router
from .main import router as main_commands
from .ai_editor import router as ai_editor_commands
from .start import router as start_commands
from .commands import router as command_shortcuts
from .navigation import router as navigation_commands
from .chats import router as chats_commands
from .post_editor import router as post_editor_commands
from .posting_publish import router as posting_publish_commands
from .content_plan_publication import router as content_plan_publication_commands
from .content_plan import router as content_plan_commands
from .settings import router as settings_commands
from .admin import router as admin_commands
from .tz import router as tz_commands
from .sources import router as sources_commands
from .moderation import router as moderation_commands
from .ai_result_actions import router as ai_result_actions_commands
from app.core.settings_channel_access import SettingsChannelOwnerMiddleware

# Реестр роутеров: start/menu отдельно, остальное в main.py
main_router = Router()
main_router.callback_query.outer_middleware(SettingsChannelOwnerMiddleware())
main_router.include_router(start_commands)
main_router.include_router(command_shortcuts)
main_router.include_router(navigation_commands)
main_router.include_router(chats_commands)
main_router.include_router(post_editor_commands)
main_router.include_router(posting_publish_commands)
main_router.include_router(content_plan_publication_commands)
main_router.include_router(content_plan_commands)
main_router.include_router(settings_commands)
main_router.include_router(admin_commands)
main_router.include_router(tz_commands)
main_router.include_router(sources_commands)
main_router.include_router(moderation_commands)
main_router.include_router(ai_editor_commands)
main_router.include_router(ai_result_actions_commands)
main_router.include_router(main_commands)
