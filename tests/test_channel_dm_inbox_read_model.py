from __future__ import annotations

from datetime import datetime, timezone

from app.api.studio.channel_dm_read_model import project_channel_dm_inbox
from app.api.studio.sources import _candidate_response
from app.domain.sources.models import ContentCandidate, SourceDocument


RECEIVED_AT = datetime(2026, 8, 18, 2, tzinfo=timezone.utc)
EDITED_AT = datetime(2026, 8, 18, 3, tzinfo=timezone.utc)


def _metadata() -> dict:
    return {
        "transport": "telegram_channel_dms",
        "telegram_direct_messages_chat_id": -1009101,
        "telegram_message_id": 81,
        "telegram_direct_messages_topic": {
            "topic_id": 321,
            "user": {
                "id": 601,
                "is_bot": False,
                "username": "bob",
                "first_name": "Bob",
            },
        },
        "telegram_sender": {
            "user": {
                "id": 601,
                "is_bot": False,
                "username": "bob",
                "first_name": "Bob",
            }
        },
        "telegram_reply_to": {"chat_id": -1009101, "message_id": 70},
        "telegram_media_group_id": "album-a",
        "telegram_edit_date": EDITED_AT.isoformat(),
    }


def test_channel_dm_origin_requires_exact_persisted_transport_discriminator() -> None:
    lookalike = _metadata()
    lookalike.pop("transport")
    assert project_channel_dm_inbox(lookalike, received_at=RECEIVED_AT) is None

    suggested = {**_metadata(), "transport": "telegram_suggested_posts"}
    assert project_channel_dm_inbox(suggested, received_at=RECEIVED_AT) is None

    text_heuristic = {
        "sender": "Channel DM from Bob",
        "reply": True,
        "topic": "DM topic",
    }
    assert project_channel_dm_inbox(text_heuristic, received_at=RECEIVED_AT) is None

    projected = project_channel_dm_inbox(_metadata(), received_at=RECEIVED_AT)
    assert projected is not None
    assert projected.transport == "telegram_channel_dms"


def test_channel_dm_projection_is_typed_safe_and_preserves_useful_provenance() -> None:
    projected = project_channel_dm_inbox(_metadata(), received_at=RECEIVED_AT)
    assert projected is not None
    assert projected.sender is not None
    assert projected.sender.id == 601
    assert projected.sender.username == "bob"
    assert projected.sender.display_name == "@bob"
    assert projected.topic_user is not None
    assert projected.topic_user.display_name == "@bob"
    assert projected.topic_id == 321
    assert projected.received_at == RECEIVED_AT
    assert projected.edited_at == EDITED_AT
    assert projected.is_reply is True
    assert projected.reply_to is not None
    assert projected.reply_to.message_id == 70
    assert projected.media_group_id == "album-a"
    assert projected.direct_messages_chat_id == -1009101
    assert projected.message_id == 81


def test_missing_or_malformed_optional_channel_dm_provenance_degrades_safely() -> None:
    projected = project_channel_dm_inbox(
        {
            "transport": "telegram_channel_dms",
            "telegram_sender": "not-a-mapping",
            "telegram_direct_messages_topic": {"topic_id": "not-an-int", "user": {}},
            "telegram_reply_to": {"chat_id": False, "message_id": "70"},
            "telegram_media_group_id": 123,
            "telegram_edit_date": "not-a-date",
        }
    )
    assert projected is not None
    assert projected.sender is None
    assert projected.topic_user is None
    assert projected.topic_id is None
    assert projected.edited_at is None
    assert projected.is_reply is False
    assert projected.reply_to is None
    assert projected.media_group_id is None


def test_candidate_response_exposes_current_source_body_without_raw_meta_authority() -> None:
    document = SourceDocument(
        id=44,
        connector_id=9,
        channel_id=7,
        external_id="dm:-1009101:81",
        source_url=None,
        title=None,
        content="Current native DM body after edit",
        content_hash="current-hash",
        language=None,
        author="@bob",
        published_at=RECEIVED_AT,
        fetched_at=RECEIVED_AT,
        meta={**_metadata(), "unrelated_internal_fact": {"authority": True}},
    )
    candidate = ContentCandidate(
        id=55,
        source_document_id=44,
        channel_id=7,
        status="new",
        score=None,
        topic="older enrichment topic",
        summary="Older enrichment summary based on v1",
        suggested_action="rewrite",
        content_item_id=None,
        meta={"reuse_policy": "rewrite_with_attribution"},
    )

    response = _candidate_response(candidate, document)
    payload = response.model_dump(mode="json")

    assert response.id == 55
    assert response.source_document_id == 44
    assert response.excerpt == "Current native DM body after edit"
    assert response.summary == "Older enrichment summary based on v1"
    assert response.channel_dm is not None
    assert response.channel_dm.edited_at == EDITED_AT
    assert response.suggested_post is None
    assert "meta" not in payload
    assert "unrelated_internal_fact" not in str(payload)
