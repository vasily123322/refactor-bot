from app.bot.routers import main_router
from app.bot.routers.content_plan import router as legacy_content_plan_router
from app.bot.routers.content_plan_publication import router as canonical_publication_router


def test_canonical_repeat_control_router_precedes_legacy_content_plan() -> None:
    assert canonical_publication_router in main_router.sub_routers
    assert legacy_content_plan_router in main_router.sub_routers
    assert main_router.sub_routers.index(
        canonical_publication_router
    ) < main_router.sub_routers.index(legacy_content_plan_router)
