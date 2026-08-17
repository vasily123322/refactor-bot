from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .document import PostDocument, PostDocumentError


NATIVE_RICH_STRUCTURAL_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "divider",
        "quote",
        "pull_quote",
        "list",
        "details",
        "math",
        "anchor",
    }
)
NATIVE_RICH_MEDIA_TYPES = frozenset(
    {"image", "media", "gallery", "collage", "slideshow", "map"}
)
NATIVE_RICH_BLOCK_TYPES = NATIVE_RICH_STRUCTURAL_TYPES | NATIVE_RICH_MEDIA_TYPES
NATIVE_NESTED_BLOCK_TYPES = frozenset({"quote", "details"})
NATIVE_RICH_MARK_TYPES = frozenset(
    {"bold", "italic", "underline", "strike", "strikethrough", "code", "link", "url"}
)
NATIVE_MEDIA_KINDS = frozenset({"photo", "video", "animation", "audio", "voice_note"})
NATIVE_TELEGRAM_OPTION_KEYS = frozenset(
    {"buttons", "silent", "disable_notification", "protect_content"}
)

# Width/height/duration can be transport metadata supplied by the media resolver even
# when a particular InputMedia subtype does not consume every field. Keep them
# preservable; only kind-gate options whose presence changes native Telegram behavior.
_KIND_GATED_MEDIA_OPTIONS = frozenset(
    {"supports_streaming", "has_spoiler", "performer", "title"}
)
NATIVE_MEDIA_OPTION_KEYS_BY_KIND: dict[str, frozenset[str]] = {
    "photo": frozenset({"has_spoiler"}),
    "video": frozenset({"width", "height", "duration", "supports_streaming", "has_spoiler"}),
    "animation": frozenset({"width", "height", "duration", "has_spoiler"}),
    "audio": frozenset({"duration", "performer", "title"}),
    "voice_note": frozenset({"duration"}),
}


class UnsupportedPostDocumentCapabilityError(PostDocumentError):
    """Document is schema-readable but cannot be emitted by the native publisher."""


def _mark_type(raw_mark: object) -> tuple[str, Mapping[str, Any]]:
    if isinstance(raw_mark, str):
        return raw_mark, {}
    if isinstance(raw_mark, Mapping):
        return str(raw_mark.get("type") or ""), raw_mark
    return "", {}


def _validate_rich_text(value: object, *, field: str) -> None:
    if not isinstance(value, list):
        return
    for segment in value:
        if not isinstance(segment, Mapping):
            continue
        marks = segment.get("marks")
        if not isinstance(marks, list):
            continue
        for raw_mark in marks:
            mark_type, attrs = _mark_type(raw_mark)
            if not mark_type:
                continue
            if mark_type not in NATIVE_RICH_MARK_TYPES:
                raise UnsupportedPostDocumentCapabilityError(
                    f"unsupported rich mark capability in {field}: {mark_type!r}"
                )
            if mark_type in {"link", "url"}:
                url = str(attrs.get("url") or attrs.get("href") or "").strip()
                if not url:
                    raise UnsupportedPostDocumentCapabilityError(
                        f"rich link capability in {field} requires url/href"
                    )


def _validate_caption(block: Mapping[str, Any], *, field: str) -> None:
    caption = block.get("caption")
    if isinstance(caption, Mapping):
        _validate_rich_text(caption.get("text"), field=f"{field}.caption.text")
        _validate_rich_text(caption.get("credit"), field=f"{field}.caption.credit")
    else:
        _validate_rich_text(caption, field=f"{field}.caption")
        _validate_rich_text(block.get("credit"), field=f"{field}.credit")


def _media_kind(block: Mapping[str, Any]) -> str:
    block_type = str(block.get("type") or "")
    if block_type == "image":
        return "photo"
    raw = str(block.get("kind") or block.get("media_type") or block_type).lower()
    if raw == "image":
        raw = "photo"
    if raw == "voice":
        raw = "voice_note"
    return raw


def _validate_single_media(block: Mapping[str, Any], *, field: str) -> None:
    kind = _media_kind(block)
    if kind not in NATIVE_MEDIA_KINDS:
        raise UnsupportedPostDocumentCapabilityError(
            f"unsupported rich media capability in {field}: {kind!r}"
        )
    allowed = NATIVE_MEDIA_OPTION_KEYS_BY_KIND[kind]
    for option in _KIND_GATED_MEDIA_OPTIONS:
        if option in block and option not in allowed:
            raise UnsupportedPostDocumentCapabilityError(
                f"unsupported {kind} media option capability: {option!r}"
            )
    _validate_caption(block, field=field)


def _validate_collection(block: Mapping[str, Any], *, field: str) -> None:
    items = block.get("items", block.get("blocks"))
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        _validate_single_media(item, field=f"{field}.items[{index}]")
    _validate_caption(block, field=field)


def _validate_list(block: Mapping[str, Any], *, field: str) -> None:
    items = block.get("items")
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        _validate_rich_text(item.get("label"), field=f"{field}.items[{index}].label")
        _validate_rich_text(
            item.get("content", item.get("text")),
            field=f"{field}.items[{index}].content",
        )


def _validate_rich_block(block: Mapping[str, Any], *, field: str) -> None:
    block_type = str(block.get("type") or "")
    if block_type not in NATIVE_RICH_BLOCK_TYPES:
        raise UnsupportedPostDocumentCapabilityError(
            f"unsupported rich block capability in {field}: {block_type!r}"
        )

    if block_type in {"paragraph", "heading", "quote", "pull_quote"}:
        _validate_rich_text(block.get("content"), field=f"{field}.content")
    if block_type in {"quote", "pull_quote"}:
        _validate_rich_text(block.get("credit"), field=f"{field}.credit")
    if block_type == "list":
        _validate_list(block, field=field)
    if block_type == "details":
        _validate_rich_text(block.get("summary"), field=f"{field}.summary")
        _validate_rich_text(block.get("content"), field=f"{field}.content")

    if block_type in NATIVE_NESTED_BLOCK_TYPES:
        children = block.get("blocks")
        if isinstance(children, list) and children:
            for index, child in enumerate(children):
                if not isinstance(child, Mapping):
                    continue
                _validate_rich_block(child, field=f"{field}.blocks[{index}]")

    if block_type in {"image", "media"}:
        _validate_single_media(block, field=field)
    elif block_type in {"gallery", "collage", "slideshow"}:
        _validate_collection(block, field=field)
    elif block_type == "map":
        _validate_caption(block, field=field)


def _valid_button_url(value: str) -> bool:
    target = value.strip()
    return any(
        target.startswith(prefix) and len(target) > len(prefix)
        for prefix in ("http://", "https://", "tg://")
    )


def _validate_telegram_options(document: PostDocument) -> None:
    unknown = set(document.telegram) - NATIVE_TELEGRAM_OPTION_KEYS
    if unknown:
        first = sorted(unknown)[0]
        raise UnsupportedPostDocumentCapabilityError(
            f"unsupported Telegram document option capability: {first!r}"
        )
    for name in ("silent", "disable_notification", "protect_content"):
        if name in document.telegram and not isinstance(document.telegram[name], bool):
            raise UnsupportedPostDocumentCapabilityError(
                f"Telegram option {name!r} must be boolean"
            )

    buttons = document.telegram.get("buttons")
    if buttons is None:
        return
    if not isinstance(buttons, list):
        raise UnsupportedPostDocumentCapabilityError("telegram.buttons must be an array")
    for row_index, row in enumerate(buttons):
        if not isinstance(row, list):
            raise UnsupportedPostDocumentCapabilityError(
                f"telegram.buttons[{row_index}] must be an array"
            )
        for button_index, button in enumerate(row):
            field = f"telegram.buttons[{row_index}][{button_index}]"
            if not isinstance(button, Mapping):
                raise UnsupportedPostDocumentCapabilityError(f"{field} must be an object")
            text = str(button.get("text") or "").strip()
            if not text:
                raise UnsupportedPostDocumentCapabilityError(f"{field}.text is required")
            raw_url = button.get("url")
            raw_callback = button.get("callback_data")
            if raw_url:
                url = str(raw_url).strip()
                if not _valid_button_url(url):
                    raise UnsupportedPostDocumentCapabilityError(
                        f"{field}.url must use http://, https://, or tg://"
                    )
                continue
            if raw_callback:
                callback = str(raw_callback).strip()
                size = len(callback.encode("utf-8"))
                if not 1 <= size <= 64:
                    raise UnsupportedPostDocumentCapabilityError(
                        f"{field}.callback_data must be from 1 to 64 UTF-8 bytes"
                    )
                continue
            raise UnsupportedPostDocumentCapabilityError(
                f"{field} requires url or callback_data"
            )


def validate_native_document_capabilities(document: PostDocument) -> None:
    """Fail writes/delivery that exceed the native Telegram publisher surface.

    `PostDocument.validate()` intentionally remains broader so unsupported/future stored
    payloads can still be read and displayed losslessly. This validator is for mutation
    and delivery boundaries only.
    """

    _validate_telegram_options(document)
    if document.mode != "rich":
        return
    for index, block in enumerate(document.blocks):
        _validate_rich_block(block, field=f"blocks[{index}]")
