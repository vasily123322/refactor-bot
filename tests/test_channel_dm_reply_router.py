from __future__ import annotations

from app.api.studio.channel_dm_replies import router


def test_channel_dm_reply_router_owns_its_studio_prefix() -> None:
    paths = {route.path for route in router.routes}
    assert "/api/studio/candidates/{candidate_id}/channel-dm-reply" in paths
    assert "/candidates/{candidate_id}/channel-dm-reply" not in paths
