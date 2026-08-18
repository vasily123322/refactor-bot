from __future__ import annotations

from app.api.studio.channel_dm_replies import router


def test_channel_dm_reply_router_owns_its_studio_prefix() -> None:
    paths = {route.path for route in router.routes}
    assert "/api/studio/candidates/{candidate_id}/channel-dm-reply" in paths
    assert "/api/studio/candidates/{candidate_id}/channel-dm-reply-lifecycle" in paths
    assert "/api/studio/channel-dm-reply-lifecycle/batch" in paths
    assert "/api/studio/candidates/{candidate_id}/channel-dm-reply-proposal" in paths
    assert "/api/studio/candidates/{candidate_id}/channel-dm-reply-intents" in paths
    assert "/api/studio/channel-dm-reply-intents/batch" in paths
    assert "/api/studio/candidates/{candidate_id}/channel-dm-reply-intents/manual" in paths
    assert "/api/studio/candidates/{candidate_id}/channel-dm-reply-intents/ai" in paths
    assert "/api/studio/channel-dm-reply-intents/{intent_id}" in paths
    assert "/api/studio/channel-dm-reply-intents/{intent_id}/dismiss" in paths
    assert "/api/studio/channel-dm-reply-intents/{intent_id}/send" in paths
    assert "/candidates/{candidate_id}/channel-dm-reply" not in paths
    assert "/candidates/{candidate_id}/channel-dm-reply-lifecycle" not in paths
    assert "/channel-dm-reply-intents/{intent_id}/send" not in paths
