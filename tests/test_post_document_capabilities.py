from __future__ import annotations

import pytest

from app.domain.content import (
    NATIVE_MEDIA_KINDS,
    NATIVE_RICH_BLOCK_TYPES,
    NATIVE_RICH_MEDIA_TYPES,
    NATIVE_RICH_STRUCTURAL_TYPES,
    NATIVE_TELEGRAM_OPTION_KEYS,
    PostDocument,
    UnsupportedPostDocumentCapabilityError,
    validate_native_document_capabilities,
)
from app.repositories.content import _document_dict
from app.services.telegram_renderer import (
    _MEDIA_KINDS,
    _RICH_MEDIA_TYPES,
    _RICH_STRUCTURAL_TYPES,
    TelegramRenderError,
    TelegramRenderer,
)


def _paragraph(**extra):
    return {"id": "p", "type": "paragraph", "content": "ok", **extra}


def test_schema_preserves_table_and_cta_but_native_contract_rejects_them() -> None:
    for block_type in ("table", "cta"):
        raw = {
            "schema_version": 1,
            "mode": "rich",
            "blocks": [{"id": block_type, "type": block_type, "payload": {"keep": True}}],
            "telegram": {},
            "metadata": {"preserve": True},
        }
        document = PostDocument.from_dict(raw)
        assert document.to_dict() == raw
        with pytest.raises(
            UnsupportedPostDocumentCapabilityError,
            match=f"unsupported rich block capability.*{block_type}",
        ):
            validate_native_document_capabilities(document)
        with pytest.raises(UnsupportedPostDocumentCapabilityError):
            _document_dict(document)


def test_nested_unsupported_block_is_rejected_without_flattening_source() -> None:
    child = {"id": "nested-table", "type": "table", "rows": [["preserve"]]}
    document = PostDocument(
        mode="rich",
        blocks=[{"id": "q", "type": "quote", "blocks": [child], "credit": "source"}],
    )
    assert document.blocks[0]["blocks"][0] == child
    with pytest.raises(UnsupportedPostDocumentCapabilityError, match="nested-table|table"):
        validate_native_document_capabilities(document)
    assert document.blocks[0]["blocks"][0] == child


def test_rich_marks_are_capability_validated_recursively() -> None:
    valid = PostDocument(
        mode="rich",
        blocks=[{
            "id": "p",
            "type": "paragraph",
            "content": [
                {"text": "B", "marks": ["bold", "italic", "underline", "strike", "code"]},
                {"text": "L", "marks": [{"type": "link", "url": "https://example.com"}]},
            ],
        }],
    )
    validate_native_document_capabilities(valid)

    unsupported = PostDocument(
        mode="rich",
        blocks=[{"id": "p", "type": "paragraph", "content": [{"text": "x", "marks": ["spoiler"]}]}],
    )
    with pytest.raises(UnsupportedPostDocumentCapabilityError, match="rich mark.*spoiler"):
        validate_native_document_capabilities(unsupported)

    missing_link = PostDocument(
        mode="rich",
        blocks=[{"id": "p", "type": "paragraph", "content": [{"text": "x", "marks": [{"type": "link"}]}]}],
    )
    with pytest.raises(UnsupportedPostDocumentCapabilityError, match="requires url/href"):
        validate_native_document_capabilities(missing_link)


def test_media_kinds_options_and_resolver_metadata_follow_contract() -> None:
    video = PostDocument(
        mode="rich",
        blocks=[{
            "id": "v",
            "type": "media",
            "kind": "video",
            "asset_id": 1,
            "width": 1280,
            "height": 720,
            "duration": 30,
            "supports_streaming": True,
            "has_spoiler": True,
        }],
    )
    validate_native_document_capabilities(video)

    # Resolver-provided numeric metadata remains preservable even when a subtype does
    # not consume every field. It is not advertised as an authoring option for photo.
    photo_metadata = PostDocument(
        mode="rich",
        blocks=[{"id": "i", "type": "image", "asset_id": 2, "width": 800, "height": 600, "duration": 0}],
    )
    validate_native_document_capabilities(photo_metadata)

    invalid_option = PostDocument(
        mode="rich",
        blocks=[{"id": "a", "type": "media", "kind": "audio", "asset_id": 3, "has_spoiler": True}],
    )
    with pytest.raises(UnsupportedPostDocumentCapabilityError, match="audio media option.*has_spoiler"):
        validate_native_document_capabilities(invalid_option)

    invalid_kind = PostDocument(
        mode="rich",
        blocks=[{"id": "d", "type": "media", "kind": "document", "asset_id": 4}],
    )
    with pytest.raises(UnsupportedPostDocumentCapabilityError, match="media capability.*document"):
        validate_native_document_capabilities(invalid_kind)


def test_keyboard_and_document_telegram_options_fail_before_delivery() -> None:
    valid = PostDocument(
        mode="rich",
        blocks=[_paragraph()],
        telegram={
            "silent": True,
            "protect_content": True,
            "buttons": [
                [{"text": "Open", "url": "https://example.com"}],
                [{"text": "Action", "callback_data": "я" * 32}],
            ],
        },
    )
    validate_native_document_capabilities(valid)

    too_large = PostDocument(
        mode="rich",
        blocks=[_paragraph()],
        telegram={"buttons": [[{"text": "Action", "callback_data": "я" * 33}]]},
    )
    with pytest.raises(UnsupportedPostDocumentCapabilityError, match="1 to 64 UTF-8 bytes"):
        validate_native_document_capabilities(too_large)

    bad_url = PostDocument(
        mode="rich",
        blocks=[_paragraph()],
        telegram={"buttons": [[{"text": "Bad", "url": "javascript:alert(1)"}]]},
    )
    with pytest.raises(UnsupportedPostDocumentCapabilityError, match="http://"):
        validate_native_document_capabilities(bad_url)

    unknown_option = PostDocument(
        mode="rich",
        blocks=[_paragraph()],
        telegram={"future_transport_flag": True},
    )
    with pytest.raises(UnsupportedPostDocumentCapabilityError, match="future_transport_flag"):
        validate_native_document_capabilities(unknown_option)


def test_renderer_keeps_silent_alias_precedence_and_transport_option_contract() -> None:
    document = PostDocument(
        mode="rich",
        blocks=[_paragraph()],
        telegram={"silent": False, "disable_notification": True, "protect_content": True},
    )
    plan = TelegramRenderer().render(document)
    assert plan.disable_notification is False
    assert plan.protect_content is True
    assert NATIVE_TELEGRAM_OPTION_KEYS == {
        "buttons",
        "silent",
        "disable_notification",
        "protect_content",
    }


def test_authoritative_sets_match_renderer_dispatch_sets() -> None:
    assert _RICH_STRUCTURAL_TYPES == NATIVE_RICH_STRUCTURAL_TYPES
    assert _RICH_MEDIA_TYPES == NATIVE_RICH_MEDIA_TYPES
    assert _MEDIA_KINDS == NATIVE_MEDIA_KINDS
    assert NATIVE_RICH_BLOCK_TYPES == NATIVE_RICH_STRUCTURAL_TYPES | NATIVE_RICH_MEDIA_TYPES
    assert "table" not in NATIVE_RICH_BLOCK_TYPES
    assert "cta" not in NATIVE_RICH_BLOCK_TYPES


def test_renderer_reports_capability_error_for_preservable_only_block() -> None:
    document = PostDocument(
        mode="rich",
        blocks=[{"id": "t", "type": "table", "rows": [["keep"]]}],
    )
    with pytest.raises(TelegramRenderError, match="unsupported rich block capability.*table"):
        TelegramRenderer().render(document)
