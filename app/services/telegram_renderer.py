from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaVoiceNote,
    InputRichBlockAnchor,
    InputRichBlockAnimation,
    InputRichBlockAudio,
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockMap,
    InputRichBlockMathematicalExpression,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockPullQuotation,
    InputRichBlockSectionHeading,
    InputRichBlockSlideshow,
    InputRichBlockVideo,
    InputRichBlockVoiceNote,
    InputRichMessage,
    Location,
    RichBlockCaption,
    RichTextBold,
    RichTextCode,
    RichTextItalic,
    RichTextStrikethrough,
    RichTextUnderline,
    RichTextUrl,
)

from app.domain.content import PostDocument
from app.services.content import LegacyPayloadError, legacy_payload_from_document


class TelegramRenderError(ValueError):
    """Raised when a PostDocument cannot be rendered without data loss."""


@dataclass(slots=True)
class TelegramRenderPlan:
    kind: Literal["classic", "rich"]
    classic_payload: dict[str, Any] | None = None
    rich_message: InputRichMessage | None = None
    reply_markup: InlineKeyboardMarkup | None = None
    disable_notification: bool = False
    protect_content: bool = False


_RICH_STRUCTURAL_TYPES = frozenset(
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

_RICH_MEDIA_TYPES = frozenset(
    {
        "image",
        "media",
        "gallery",
        "collage",
        "slideshow",
        "map",
    }
)

_MEDIA_KINDS = frozenset(
    {
        "photo",
        "video",
        "animation",
        "audio",
        "voice_note",
    }
)


def _button_markup(value: object) -> InlineKeyboardMarkup | None:
    if not isinstance(value, list) or not value:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for raw_row in value:
        if not isinstance(raw_row, list):
            raise TelegramRenderError("telegram.buttons rows must be arrays")
        row: list[InlineKeyboardButton] = []
        for raw_button in raw_row:
            if not isinstance(raw_button, Mapping):
                raise TelegramRenderError("telegram.buttons entries must be objects")
            text = str(raw_button.get("text") or "").strip()
            if not text:
                raise TelegramRenderError("telegram button text is required")
            url = raw_button.get("url")
            callback_data = raw_button.get("callback_data")
            if url:
                row.append(InlineKeyboardButton(text=text, url=str(url)))
            elif callback_data:
                row.append(
                    InlineKeyboardButton(text=text, callback_data=str(callback_data))
                )
            else:
                raise TelegramRenderError(
                    "telegram button requires url or callback_data"
                )
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _plain_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def _apply_marks(text: object, marks: object) -> object:
    rendered: object = _plain_text(text)
    if not isinstance(marks, list):
        return rendered
    for raw_mark in marks:
        mark_type: str
        attrs: Mapping[str, Any]
        if isinstance(raw_mark, str):
            mark_type = raw_mark
            attrs = {}
        elif isinstance(raw_mark, Mapping):
            mark_type = str(raw_mark.get("type") or "")
            attrs = raw_mark
        else:
            continue

        if mark_type == "bold":
            rendered = RichTextBold(text=rendered)
        elif mark_type == "italic":
            rendered = RichTextItalic(text=rendered)
        elif mark_type == "underline":
            rendered = RichTextUnderline(text=rendered)
        elif mark_type in {"strike", "strikethrough"}:
            rendered = RichTextStrikethrough(text=rendered)
        elif mark_type == "code":
            rendered = RichTextCode(text=rendered)
        elif mark_type in {"link", "url"}:
            url = str(attrs.get("url") or attrs.get("href") or "").strip()
            if not url:
                raise TelegramRenderError("rich text link mark requires a URL")
            rendered = RichTextUrl(text=rendered, url=url)
        elif mark_type:
            raise TelegramRenderError(f"unsupported rich text mark: {mark_type!r}")
    return rendered


def _rich_text(value: object) -> object:
    if isinstance(value, str) or value is None:
        return _plain_text(value)
    if not isinstance(value, list):
        return _plain_text(value)

    parts: list[object] = []
    for item in value:
        if isinstance(item, Mapping):
            parts.append(_apply_marks(item.get("text"), item.get("marks")))
        else:
            parts.append(str(item))
    return parts


def _positive_size(value: object, *, field: str, default: int) -> int:
    if value is None:
        return default
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise TelegramRenderError(f"{field} must be an integer from 1 to 6") from exc
    if not 1 <= size <= 6:
        raise TelegramRenderError(f"{field} must be from 1 to 6")
    return size


def _bounded_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
    default: int | None = None,
) -> int | None:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TelegramRenderError(f"{field} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise TelegramRenderError(
            f"{field} must be from {minimum} to {maximum}"
        )
    return parsed


def _paragraph_for(value: object) -> InputRichBlockParagraph:
    return InputRichBlockParagraph(text=_rich_text(value))


def _list_item(raw: object) -> InputRichBlockListItem:
    if isinstance(raw, Mapping):
        label = raw.get("label")
        content = raw.get("content", raw.get("text", ""))
    else:
        label = None
        content = raw
    return InputRichBlockListItem(
        blocks=[_paragraph_for(content)],
        label=(str(label) if label is not None else None),
    )


def _rich_caption(block: Mapping[str, Any]) -> RichBlockCaption | None:
    raw_caption = block.get("caption")
    if raw_caption is None:
        return None
    if isinstance(raw_caption, Mapping):
        text = raw_caption.get("text", "")
        credit = raw_caption.get("credit")
    else:
        text = raw_caption
        credit = block.get("credit")
    return RichBlockCaption(
        text=_rich_text(text),
        credit=(_rich_text(credit) if credit is not None else None),
    )


def _media_reference(block: Mapping[str, Any]) -> str:
    for field in ("media", "telegram_file_id", "storage_url", "url"):
        raw = block.get(field)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    if block.get("asset_id") is not None or block.get("media_asset_id") is not None:
        raise TelegramRenderError(
            "rich media asset id must be resolved to a Telegram file id or URL before rendering"
        )
    raise TelegramRenderError("rich media block requires media reference")


def _media_kind(block: Mapping[str, Any]) -> str:
    block_type = str(block.get("type") or "")
    if block_type == "image":
        return "photo"
    raw = str(block.get("kind") or block.get("media_type") or block_type).lower()
    if raw == "image":
        raw = "photo"
    if raw == "voice":
        raw = "voice_note"
    if raw not in _MEDIA_KINDS:
        raise TelegramRenderError(f"unsupported rich media kind: {raw!r}")
    return raw


def _render_single_media_block(block: Mapping[str, Any]) -> object:
    kind = _media_kind(block)
    media = _media_reference(block)
    caption = _rich_caption(block)

    duration = _bounded_int(
        block.get("duration"), field="media.duration", minimum=0, maximum=86400
    )
    width = _bounded_int(
        block.get("width"), field="media.width", minimum=1, maximum=10000
    )
    height = _bounded_int(
        block.get("height"), field="media.height", minimum=1, maximum=10000
    )
    has_spoiler = bool(block.get("has_spoiler")) if "has_spoiler" in block else None

    if kind == "photo":
        return InputRichBlockPhoto(
            photo=InputMediaPhoto(media=media, has_spoiler=has_spoiler),
            caption=caption,
        )
    if kind == "video":
        return InputRichBlockVideo(
            video=InputMediaVideo(
                media=media,
                width=width,
                height=height,
                duration=duration,
                supports_streaming=(
                    bool(block.get("supports_streaming"))
                    if "supports_streaming" in block
                    else None
                ),
                has_spoiler=has_spoiler,
            ),
            caption=caption,
        )
    if kind == "animation":
        return InputRichBlockAnimation(
            animation=InputMediaAnimation(
                media=media,
                width=width,
                height=height,
                duration=duration,
                has_spoiler=has_spoiler,
            ),
            caption=caption,
        )
    if kind == "audio":
        return InputRichBlockAudio(
            audio=InputMediaAudio(
                media=media,
                duration=duration,
                performer=(str(block.get("performer")) if block.get("performer") else None),
                title=(str(block.get("title")) if block.get("title") else None),
            ),
            caption=caption,
        )
    if kind == "voice_note":
        return InputRichBlockVoiceNote(
            voice_note=InputMediaVoiceNote(media=media, duration=duration),
            caption=caption,
        )
    raise TelegramRenderError(f"unhandled rich media kind: {kind!r}")


def _render_media_collection(block: Mapping[str, Any]) -> object:
    items = block.get("items", block.get("blocks"))
    if not isinstance(items, list) or not items:
        raise TelegramRenderError("rich media collection requires non-empty items")
    rendered: list[object] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise TelegramRenderError("rich media collection items must be objects")
        rendered.append(_render_single_media_block(item))
    caption = _rich_caption(block)
    if str(block.get("type") or "") == "slideshow":
        return InputRichBlockSlideshow(blocks=rendered, caption=caption)
    return InputRichBlockCollage(blocks=rendered, caption=caption)


def _render_map_block(block: Mapping[str, Any]) -> InputRichBlockMap:
    try:
        latitude = float(block.get("latitude", block.get("lat")))
        longitude = float(block.get("longitude", block.get("lon", block.get("lng"))))
    except (TypeError, ValueError) as exc:
        raise TelegramRenderError("rich map requires numeric latitude and longitude") from exc
    if not -90 <= latitude <= 90:
        raise TelegramRenderError("map.latitude must be from -90 to 90")
    if not -180 <= longitude <= 180:
        raise TelegramRenderError("map.longitude must be from -180 to 180")
    zoom = _bounded_int(
        block.get("zoom"), field="map.zoom", minimum=0, maximum=24, default=13
    )
    width = _bounded_int(
        block.get("width"), field="map.width", minimum=1, maximum=10000, default=640
    )
    height = _bounded_int(
        block.get("height"), field="map.height", minimum=1, maximum=10000, default=360
    )
    assert zoom is not None and width is not None and height is not None
    if width + height > 10000:
        raise TelegramRenderError("map width + height must not exceed 10000")
    ratio = max(width / height, height / width)
    if ratio > 20:
        raise TelegramRenderError("map width/height ratio must not exceed 20")
    return InputRichBlockMap(
        location=Location(latitude=latitude, longitude=longitude),
        zoom=zoom,
        width=width,
        height=height,
        caption=_rich_caption(block),
    )


def _render_rich_media_block(block: Mapping[str, Any]) -> object:
    block_type = str(block.get("type") or "")
    if block_type in {"image", "media"}:
        return _render_single_media_block(block)
    if block_type in {"gallery", "collage", "slideshow"}:
        return _render_media_collection(block)
    if block_type == "map":
        return _render_map_block(block)
    raise TelegramRenderError(f"unsupported rich media block type: {block_type!r}")


def _render_rich_block(block: Mapping[str, Any]) -> object:
    block_type = str(block.get("type") or "")
    if block_type in _RICH_MEDIA_TYPES:
        return _render_rich_media_block(block)
    if block_type not in _RICH_STRUCTURAL_TYPES:
        raise TelegramRenderError(f"unsupported rich block type: {block_type!r}")

    if block_type == "paragraph":
        return InputRichBlockParagraph(text=_rich_text(block.get("content")))
    if block_type == "heading":
        return InputRichBlockSectionHeading(
            text=_rich_text(block.get("content")),
            size=_positive_size(block.get("size"), field="heading.size", default=2),
        )
    if block_type == "divider":
        return InputRichBlockDivider()
    if block_type == "quote":
        content_blocks = block.get("blocks")
        if isinstance(content_blocks, list) and content_blocks:
            rendered_blocks = [
                _render_rich_block(child)
                for child in content_blocks
                if isinstance(child, Mapping)
            ]
            if len(rendered_blocks) != len(content_blocks):
                raise TelegramRenderError("quote.blocks must contain block objects")
        else:
            rendered_blocks = [_paragraph_for(block.get("content"))]
        credit = block.get("credit")
        return InputRichBlockBlockQuotation(
            blocks=rendered_blocks,
            credit=(_rich_text(credit) if credit is not None else None),
        )
    if block_type == "pull_quote":
        credit = block.get("credit")
        return InputRichBlockPullQuotation(
            text=_rich_text(block.get("content")),
            credit=(_rich_text(credit) if credit is not None else None),
        )
    if block_type == "list":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise TelegramRenderError("rich list requires non-empty items")
        return InputRichBlockList(items=[_list_item(item) for item in items])
    if block_type == "details":
        summary = block.get("summary")
        if summary is None:
            raise TelegramRenderError("rich details requires summary")
        children = block.get("blocks")
        if isinstance(children, list) and children:
            rendered_children = [
                _render_rich_block(child)
                for child in children
                if isinstance(child, Mapping)
            ]
            if len(rendered_children) != len(children):
                raise TelegramRenderError("details.blocks must contain block objects")
        else:
            rendered_children = [_paragraph_for(block.get("content"))]
        return InputRichBlockDetails(
            summary=_rich_text(summary),
            blocks=rendered_children,
            is_open=(bool(block.get("is_open")) if "is_open" in block else None),
        )
    if block_type == "math":
        expression = str(
            block.get("formula")
            or block.get("expression")
            or block.get("content")
            or ""
        ).strip()
        if not expression:
            raise TelegramRenderError("rich math block requires expression")
        return InputRichBlockMathematicalExpression(expression=expression)
    if block_type == "anchor":
        name = str(block.get("name") or "").strip()
        if not name:
            raise TelegramRenderError("rich anchor block requires name")
        return InputRichBlockAnchor(name=name)

    raise TelegramRenderError(f"unhandled rich block type: {block_type!r}")


def _telegram_bool(document: PostDocument, *names: str) -> bool:
    for name in names:
        if name in document.telegram:
            return bool(document.telegram.get(name))
    return False


class TelegramRenderer:
    """Compile PostDocument into a transport-edge Telegram render plan."""

    def render(self, document: PostDocument | Mapping[str, Any]) -> TelegramRenderPlan:
        doc = (
            document
            if isinstance(document, PostDocument)
            else PostDocument.from_dict(document)
        )
        doc.validate()
        reply_markup = _button_markup(doc.telegram.get("buttons"))
        disable_notification = _telegram_bool(doc, "silent", "disable_notification")
        protect_content = _telegram_bool(doc, "protect_content")

        if doc.mode == "classic":
            try:
                payload = legacy_payload_from_document(doc)
            except LegacyPayloadError as exc:
                raise TelegramRenderError(str(exc)) from exc
            return TelegramRenderPlan(
                kind="classic",
                classic_payload=payload,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
                protect_content=protect_content,
            )

        if doc.mode != "rich":
            raise TelegramRenderError(f"unsupported document mode: {doc.mode!r}")
        if not doc.blocks:
            raise TelegramRenderError("rich document requires at least one block")

        blocks = [_render_rich_block(block) for block in doc.blocks]
        return TelegramRenderPlan(
            kind="rich",
            rich_message=InputRichMessage(blocks=blocks),
            reply_markup=reply_markup,
            disable_notification=disable_notification,
            protect_content=protect_content,
        )
