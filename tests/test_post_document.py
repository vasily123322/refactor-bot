import pytest

from app.domain.content import PostDocument, PostDocumentError
from app.services.content import (
    LegacyPayloadError,
    document_from_legacy_payload,
    legacy_payload_from_document,
)


def test_post_document_round_trip_is_defensive() -> None:
    source = {
        "schema_version": 1,
        "mode": "classic",
        "blocks": [{"id": "b1", "type": "text", "text": "hello"}],
        "telegram": {"buttons": [[{"text": "Open", "url": "https://example.com"}]]},
        "metadata": {"tag": "news"},
    }

    document = PostDocument.from_dict(source)
    exported = document.to_dict()
    exported["blocks"][0]["text"] = "changed outside"

    assert document.blocks[0]["text"] == "hello"
    assert PostDocument.from_dict(document.to_dict()).to_dict() == document.to_dict()


def test_post_document_rejects_duplicate_block_ids() -> None:
    with pytest.raises(PostDocumentError, match="duplicate block id"):
        PostDocument(
            mode="rich",
            blocks=[
                {"id": "same", "type": "paragraph", "content": "one"},
                {"id": "same", "type": "heading", "content": "two"},
            ],
        )


def test_post_document_rejects_unknown_schema_version() -> None:
    with pytest.raises(PostDocumentError, match="unsupported PostDocument schema_version"):
        PostDocument(schema_version=99)


def test_primary_text_supports_classic_and_rich_blocks() -> None:
    document = PostDocument(
        mode="rich",
        blocks=[
            {"id": "a", "type": "paragraph", "content": [{"text": "Hello "}, {"text": "world"}]},
            {"id": "b", "type": "quote", "content": "Quoted"},
        ],
    )

    assert document.primary_text() == "Hello world\n\nQuoted"


def test_legacy_text_payload_round_trip_preserves_runtime_fields() -> None:
    payload = {
        "type": "text",
        "text": "Hello",
        "buttons": [[{"text": "Site", "url": "https://example.com"}]],
        "repeat_on": True,
        "repeat_seconds": 3600,
        "autodelete_seconds": 600,
        "custom_future_field": {"keep": True},
    }

    document = document_from_legacy_payload(payload)

    assert document.blocks == [{"id": "b_1", "type": "text", "text": "Hello"}]
    assert document.telegram["buttons"] == payload["buttons"]
    assert document.metadata["legacy_payload_extra"]["repeat_seconds"] == 3600
    assert legacy_payload_from_document(document) == payload


def test_legacy_media_payload_round_trip_preserves_editor_fields() -> None:
    payload = {
        "type": "video",
        "file_id": "telegram-file-id",
        "caption": "Caption",
        "media_spoiler": True,
        "media_pos": "bottom",
        "notify": False,
    }

    document = document_from_legacy_payload(payload)

    assert document.blocks[0]["file_id"] == "telegram-file-id"
    assert document.blocks[0]["caption"] == "Caption"
    assert document.primary_text() == "Caption"
    assert legacy_payload_from_document(document) == payload


def test_legacy_adapter_rejects_rich_document_instead_of_dropping_blocks() -> None:
    document = PostDocument(
        mode="rich",
        blocks=[{"id": "p1", "type": "paragraph", "content": "Hello"}],
    )

    with pytest.raises(LegacyPayloadError, match="new Telegram renderer"):
        legacy_payload_from_document(document)


def test_legacy_adapter_rejects_multiblock_classic_document() -> None:
    document = PostDocument(
        mode="classic",
        blocks=[
            {"id": "a", "type": "text", "text": "one"},
            {"id": "b", "type": "photo", "file_id": "file"},
        ],
    )

    with pytest.raises(LegacyPayloadError, match="exactly one"):
        legacy_payload_from_document(document)
