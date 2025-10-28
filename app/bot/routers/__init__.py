from aiogram import Router
from .main import router as main_commands
from .start import router as start_commands
from .chats import router as chats_commands
from .post_editor import router as post_editor_commands
from .content_plan import router as content_plan_commands
from .settings import router as settings_commands
from .admin import router as admin_commands

# Реестр роутеров: start/menu отдельно, остальное в main.py
main_router = Router()
main_router.include_router(start_commands)
main_router.include_router(chats_commands)
main_router.include_router(post_editor_commands)
main_router.include_router(content_plan_commands)
main_router.include_router(settings_commands)
main_router.include_router(admin_commands)
main_router.include_router(main_commands)