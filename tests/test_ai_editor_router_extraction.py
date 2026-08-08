from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTERS = ROOT / "app" / "bot" / "routers"


def _read(name: str) -> str:
    return (ROUTERS / name).read_text(encoding="utf-8")


def test_ai_editor_router_is_registered_before_legacy_main() -> None:
    content = _read("__init__.py")
    assert "from .ai_editor import router as ai_editor_commands" in content
    assert content.index("main_router.include_router(ai_editor_commands)") < content.index(
        "main_router.include_router(main_commands)"
    )


def test_ai_editor_handlers_are_not_duplicated_in_main() -> None:
    editor = _read("ai_editor.py")
    legacy_main = _read("main.py")

    signatures = (
        "async def cb_post_ai(",
        "async def cb_ai_back_to_preview(",
        "async def handle_ai_topic_input(",
        "async def handle_ai_link_input(",
        "async def cb_ai_improve_action(",
    )
    for signature in signatures:
        assert signature in editor
        assert signature not in legacy_main


def test_ai_editor_router_keeps_private_chat_scope() -> None:
    editor = _read("ai_editor.py")
    assert 'router.message.filter(F.chat.type == "private")' in editor
    assert 'router.callback_query.filter(F.message.chat.type == "private")' in editor
