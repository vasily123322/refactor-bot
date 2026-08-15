from pathlib import Path


def test_content_plan_delete_is_owned_by_canonical_wrapper_only() -> None:
    wrapper = Path("app/bot/routers/content_plan_cancellation.py").read_text(
        encoding="utf-8"
    )
    legacy = Path("app/bot/routers/content_plan.py").read_text(encoding="utf-8")

    handler_registration = '@router.callback_query(F.data.startswith("cp_delete_post:"))'
    nested_registration = "router.include_router(legacy_content_plan_router)"

    assert handler_registration in wrapper
    assert "ContentPlanCancellationService" in wrapper
    assert wrapper.index(handler_registration) < wrapper.index(nested_registration)

    assert "async def cb_cp_delete_post(" not in legacy
    assert handler_registration not in legacy
    assert "await session.delete(post)" not in legacy

    # Delete buttons keep the stable callback-data contract consumed by the wrapper.
    assert 'callback_data=f"cp_delete_post:{post.id}:{date_iso}"' in legacy
